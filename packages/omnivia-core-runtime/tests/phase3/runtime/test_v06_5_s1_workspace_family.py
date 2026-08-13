"""V06-5 CP-S1 workspace-family production acceptance controls."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    ApplicationDispatcher,
    build_installation_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    Grant,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    HttpListener,
)
from omnivia_core_runtime.service.installation import (
    WORKSPACE_CREATE_OPERATION,
    WORKSPACE_LIST_OPERATION,
    WORKSPACE_LIST_PURPOSE,
    InstallationApplicationService,
)
from omnivia_core_runtime.service.installation_host import (
    InstallationAuthorityCoordinator,
)
from omnivia_core_runtime.service.mutation import WORKSPACE_ADMINISTRATION_PURPOSE
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import (
    LocalSocketServer,
    LocalSocketTransport,
    endpoint_for_path,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    initialise_allocated_workspace,
    initialise_workspace,
)
from omnivia_core_runtime.storage.installation_store import (
    AllocationState,
    InstallationStore,
    open_installation_store,
)
from v06_5_c1_evidence import semantic_execution

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_BOOTSTRAP_IN_PROGRESS,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    RequestEnvelope,
    RequestMetadata,
    SuccessResponseEnvelope,
    WorkspaceCreateResult,
    WorkspaceListResult,
    decode_response,
    encode_request,
    get_operation_metadata,
)

PRINCIPAL = "principal-s1-owner"
INSTALLATION_ID = "inst-s1"
CLIENT = ClientIdentity(id="s1-client", version="0.1.0")


class TickClock:
    def __init__(self) -> None:
        self.value = 1_800_000_000_000_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _request(
    operation: str,
    input_: dict[str, Any],
    *,
    request_id: str,
    idempotency_key: str | None = None,
) -> RequestEnvelope:
    entry = get_operation_metadata(operation)
    purpose = (
        WORKSPACE_ADMINISTRATION_PURPOSE
        if operation == WORKSPACE_CREATE_OPERATION
        else WORKSPACE_LIST_PURPOSE
    )
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=f"cor-{request_id}",
            trace_id=f"trc-{request_id}",
            api_version=CONTRACT_VERSION,
            client=CLIENT,
            scopes=tuple(entry.scope.required_scopes),
            purpose=purpose,
            required_capabilities=(
                CapabilityRequirement(
                    id=entry.required_capability.id,
                    minimum_version=entry.required_capability.minimum_version,
                    required=True,
                ),
            ),
            idempotency_key=idempotency_key,
        ),
        input=input_,
    )


def _path(
    tmp_path: Path,
    *,
    bootstrapper: Callable[..., WorkspaceInitResult] = initialise_allocated_workspace,
) -> tuple[ApplicationDispatcher, InstallationStore]:
    installation_root = (tmp_path / "installation").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="s1-owner",
        clock_us=TickClock(),
        installation_id_factory=lambda: INSTALLATION_ID,
    )
    service = InstallationApplicationService(
        store=store,
        installation_root=installation_root,
        workspace_storage_root=(tmp_path / "workspaces").resolve(),
        core_version="0.1.0",
        clock=FakeClock(),
        bootstrapper=bootstrapper,
    )
    dispatcher = build_installation_application_dispatcher(
        service=service,
        principal_id=PRINCIPAL,
        fallback=Dispatcher.for_service_operations(
            Grant(
                principal=PRINCIPAL,
                workspaces=frozenset({"ws-fallback"}),
                operations=frozenset(SERVICE_OPERATIONS),
            )
        ),
    )
    return dispatcher, store


def _created(
    dispatcher: ApplicationDispatcher, name: str, key: str
) -> WorkspaceCreateResult:
    response = dispatcher.dispatch(
        _request(
            WORKSPACE_CREATE_OPERATION,
            {"display_name": name},
            request_id=f"req-{key}",
            idempotency_key=key,
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    return WorkspaceCreateResult.from_wire(response.result)


def _router(dispatcher: ApplicationDispatcher) -> DocumentRouter:
    return DocumentRouter(
        probes=ProbeRouter(
            facts=lambda: ServiceFacts(
                observed_at="2026-08-12T00:00:00Z",
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            ),
            capabilities=tuple,
            clock=lambda: 0,
        ),
        dispatch=dispatcher.dispatch,
    )


def _unrecorded_transport_call(
    adapter: str,
    dispatcher: ApplicationDispatcher,
    request: RequestEnvelope,
    root: Path,
) -> SuccessResponseEnvelope | ErrorResponseEnvelope:
    if adapter == "in-process":
        return dispatcher.dispatch(request)
    router = _router(dispatcher)
    if adapter == "local-ipc":
        with tempfile.TemporaryDirectory(prefix="ovs1-", dir="/tmp") as directory:
            endpoint = endpoint_for_path(Path(directory) / "s.sock")
            server = LocalSocketServer(router=router, endpoint=endpoint)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(10)
                    client.connect(endpoint.address)
                    client.sendall(encode_frame(encode_request(request)))
                    header = b""
                    while len(header) < 8:
                        header += client.recv(8 - len(header))
                    length = int.from_bytes(header[4:], "big")
                    body = b""
                    while len(body) < length:
                        body += client.recv(length - len(body))
                return decode_response(decode_frame(header + body))
            finally:
                server.stop()
    assert adapter == "http"
    credential = "s1-credential"
    server = HttpListener(
        router=router,
        principal=PRINCIPAL,
        resolver=lambda value: dispatcher.session if value == credential else None,
        authenticated_dispatch=dispatcher.dispatch_for_session,
    )
    server.start()
    try:
        port = int(server.url.rsplit(":", 1)[1])
        body = json.dumps(
            encode_request(request), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            client.request(
                "POST",
                APPLICATION_PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": CONTENT_TYPE,
                },
            )
            response = client.getresponse()
            response_body = response.read()
        finally:
            client.close()
        assert response.status == 200
        return decode_response(json.loads(response_body))
    finally:
        server.stop()


def _transport_call(
    adapter: str,
    dispatcher: ApplicationDispatcher,
    request: RequestEnvelope,
    root: Path,
    *,
    case_id: str | None = None,
) -> SuccessResponseEnvelope | ErrorResponseEnvelope:
    response = semantic_execution(
        case_id=case_id,
        adapter=adapter,
        route=dispatcher,
        request=request,
        invoke=lambda: _unrecorded_transport_call(adapter, dispatcher, request, root),
    )
    assert isinstance(response, (SuccessResponseEnvelope, ErrorResponseEnvelope))
    return response


def test_v06_5_s1_workspace_create_primary_success(tmp_path: Path) -> None:
    dispatcher, store = _path(tmp_path)
    try:
        result = _created(dispatcher, "Primary S1", "s1-primary")
        assert result.workspace.workspace_id.startswith("ws-")
        assert result.workspace.display_name == "Primary S1"
        assert result.workspace.status == "active"
        assert store.list_workspace_ids() == (result.workspace.workspace_id,)
    finally:
        store.close()


def test_v06_5_s1_workspace_create_honest_replay(tmp_path: Path) -> None:
    dispatcher, store = _path(tmp_path)
    try:
        first = _created(dispatcher, "Replay S1", "s1-replay")
        second = _created(dispatcher, "Replay S1", "s1-replay")
        assert second == first
        assert store.list_workspace_ids() == (first.workspace.workspace_id,)
    finally:
        store.close()


def test_v06_5_s1_workspace_create_idempotency_conflict(tmp_path: Path) -> None:
    dispatcher, store = _path(tmp_path)
    try:
        first = _created(dispatcher, "First S1", "s1-conflict")
        refused = dispatcher.dispatch(
            _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "Different S1"},
                request_id="req-s1-conflict",
                idempotency_key="s1-conflict",
            )
        )
        assert isinstance(refused, ErrorResponseEnvelope)
        assert refused.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
        assert store.list_workspace_ids() == (first.workspace.workspace_id,)
    finally:
        store.close()


def test_v06_5_s1_workspace_create_bootstrap_in_progress(tmp_path: Path) -> None:
    def busy(**_kwargs: Any) -> WorkspaceInitResult:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.WORKSPACE_BUSY,
            reason="test-only contention",
        )

    dispatcher, store = _path(tmp_path, bootstrapper=busy)
    try:
        refused = dispatcher.dispatch(
            _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "Busy S1"},
                request_id="req-s1-busy",
                idempotency_key="s1-busy",
            )
        )
        assert isinstance(refused, ErrorResponseEnvelope)
        assert refused.error.code == ERROR_CODE_BOOTSTRAP_IN_PROGRESS
        assert store.list_workspace_ids() == ()
    finally:
        store.close()


def test_v06_5_s1_workspace_list_primary_and_page_2(tmp_path: Path) -> None:
    dispatcher, store = _path(tmp_path)
    try:
        created = [
            _created(dispatcher, f"Workspace {i}", f"s1-list-{i}") for i in range(3)
        ]
        first_response = dispatcher.dispatch(
            _request(
                WORKSPACE_LIST_OPERATION,
                {"limit": 2},
                request_id="req-s1-list-first",
            )
        )
        assert isinstance(first_response, SuccessResponseEnvelope)
        first = WorkspaceListResult.from_wire(first_response.result)
        assert len(first.workspaces) == 2
        assert first.page.continuation_token is not None

        second_response = dispatcher.dispatch(
            _request(
                WORKSPACE_LIST_OPERATION,
                {
                    "limit": 2,
                    "page": {"continuation_token": first.page.continuation_token},
                },
                request_id="req-s1-list-second",
            )
        )
        assert isinstance(second_response, SuccessResponseEnvelope)
        second = WorkspaceListResult.from_wire(second_response.result)
        assert len(second.workspaces) == 1
        assert second.page.continuation_token is None
        assert {
            item.workspace_id for item in (*first.workspaces, *second.workspaces)
        } == {item.workspace.workspace_id for item in created}
    finally:
        store.close()


def test_v06_5_s1_workspace_create_requires_s0_grant(tmp_path: Path) -> None:
    dispatcher, store = _path(tmp_path)
    try:
        # Replacing the server-owned role grant causes the twelve-check seam to
        # refuse before the S0 installation service can allocate a target.
        no_role = AuthenticatedSession(
            principal_id=dispatcher.session.principal_id,
            roles=frozenset(),
            installations=dispatcher.session.installations,
            workspaces=dispatcher.session.workspaces,
            operations=dispatcher.session.operations,
            scopes=dispatcher.session.scopes,
            purposes=dispatcher.session.purposes,
            capabilities=dispatcher.session.capabilities,
        )
        refused = replace(dispatcher, session=no_role).dispatch(
            _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "No grant S1"},
                request_id="req-s1-no-grant",
                idempotency_key="s1-no-grant",
            )
        )
        assert isinstance(refused, ErrorResponseEnvelope)
        assert refused.error.code == ERROR_CODE_AUTHORIZATION_DENIED
        assert store.list_workspace_ids() == ()
        assert not (tmp_path / "workspaces").exists()
    finally:
        store.close()


def test_v06_5_s1_workspace_creation_cleanup_after_failure(tmp_path: Path) -> None:
    def failed(**_kwargs: Any) -> WorkspaceInitResult:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.WRITE_FAILURE,
            reason="test-only write failure",
        )

    dispatcher, store = _path(tmp_path, bootstrapper=failed)
    closed = False
    try:
        refused = dispatcher.dispatch(
            _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "Failed S1"},
                request_id="req-s1-failed",
                idempotency_key="s1-failed",
            )
        )
        assert isinstance(refused, ErrorResponseEnvelope)
        assert refused.error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
        assert store.list_workspace_ids() == ()
        assert not (tmp_path / "workspaces").exists()
        # The failed allocation remains recoverable and cannot be mistaken for
        # active inventory; this is the durable half of cleanup.
        database_path = store.database_path
        store.close()
        closed = True
        connection = sqlite3.connect(database_path)
        try:
            row = connection.execute(
                "SELECT state FROM omnivia_installation_allocations"
            ).fetchone()
            assert row == (AllocationState.FAILED_RECOVERABLE.value,)
        finally:
            connection.close()
    finally:
        if not closed:
            store.close()


def test_v06_5_s1_legacy_workspace_is_not_implicitly_adopted(tmp_path: Path) -> None:
    """Only a fenced S1 allocation enters the installation-owned inventory.

    Legacy ``--init`` predates installation grants and therefore cannot silently
    grant the new catalogue authority over a workspace. A later cutover may define
    an explicit adoption protocol; S1 must neither infer one nor enumerate the
    filesystem as a substitute for authorized inventory.
    """
    initialized = initialise_workspace(
        workspace_root=tmp_path / "legacy-workspace",
        installation_root=tmp_path / "installation",
        core_version="0.1.0",
    )
    assert initialized.status is WorkspaceInitStatus.INITIALISED

    dispatcher, store = _path(tmp_path)
    try:
        response = dispatcher.dispatch(
            _request(
                WORKSPACE_LIST_OPERATION,
                {"limit": 100},
                request_id="req-s1-no-implicit-adoption",
            )
        )
        assert isinstance(response, SuccessResponseEnvelope)
        assert WorkspaceListResult.from_wire(response.result).workspaces == ()
        assert store.list_workspace_ids() == ()
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="uses real Unix service sockets")
def test_v06_5_s1_two_legacy_workspace_services_still_coexist() -> None:
    """Two workspace services share one fenced S1 catalogue authority."""
    root = Path(tempfile.mkdtemp(prefix="ovs1-main-", dir="/tmp"))
    processes: list[subprocess.Popen[str]] = []
    try:
        installation_root = root / "installation"
        workspaces = (root / "workspace-a", root / "workspace-b")
        sockets = (root / "a.sock", root / "b.sock")
        for workspace in workspaces:
            initialized = initialise_workspace(
                workspace_root=workspace,
                installation_root=installation_root,
                core_version="0.1.0",
            )
            assert initialized.status is WorkspaceInitStatus.INITIALISED

        executable = Path(sys.executable).parent / "omnivia-core-service"
        assert executable.is_file(), executable
        for workspace, socket_path in zip(workspaces, sockets, strict=True):
            processes.append(
                subprocess.Popen(
                    [
                        str(executable),
                        "--workspace",
                        str(workspace),
                        "--installation-state",
                        str(installation_root),
                        "--endpoint",
                        f"unix://{socket_path}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if all(path.exists() for path in sockets) or any(
                process.poll() is not None for process in processes
            ):
                break
            time.sleep(0.02)

        diagnostics = []
        for process in processes:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                diagnostics.append((process.returncode, stdout, stderr))
        assert diagnostics == []
        assert all(path.exists() for path in sockets)
        assert all(process.poll() is None for process in processes)

        created_response = LocalSocketTransport(path=sockets[0]).call(
            _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "Shared authority"},
                request_id="req-s1-shared-create",
                idempotency_key="s1-shared-create",
            )
        )
        assert isinstance(created_response, SuccessResponseEnvelope), created_response
        created = WorkspaceCreateResult.from_wire(created_response.result)

        listed_response = LocalSocketTransport(path=sockets[1]).call(
            _request(
                WORKSPACE_LIST_OPERATION,
                {"limit": 100},
                request_id="req-s1-shared-list",
            )
        )
        assert isinstance(listed_response, SuccessResponseEnvelope), listed_response
        listed = WorkspaceListResult.from_wire(listed_response.result)
        assert tuple(item.workspace_id for item in listed.workspaces) == (
            created.workspace.workspace_id,
        )
        assert (
            listed_response.metadata.authority.principal_id
            == created_response.metadata.authority.principal_id
            == "local-user"
        )
        assert (
            listed_response.metadata.authority.roles
            == created_response.metadata.authority.roles
        )
        assert (installation_root / "catalogue" / "installation.sqlite").is_file()
        assert (
            installation_root / "runtime" / "installation-authority" / "authority.json"
        ).is_file()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        shutil.rmtree(root, ignore_errors=True)


def test_v06_5_s1_proxy_takes_over_after_installation_owner_exits() -> None:
    """The surviving proxy advances fencing before serving another S1 call."""
    root = Path(tempfile.mkdtemp(prefix="ovs1-failover-", dir="/tmp"))
    installation_root = (root / "installation").resolve()
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset(),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )
    facts = SimpleNamespace(
        probe_facts=lambda: ServiceFacts(
            observed_at="2026-08-12T00:00:00Z",
            health_status="pass",
            readiness_status="pass",
            discovery_status="pass",
        )
    )
    shared = {
        "installation_root": installation_root,
        "workspace_storage_root": (root / "workspaces").resolve(),
        "core_version": "0.1.0",
        "clock": FakeClock(),
        "principal_id": PRINCIPAL,
        "probe": probe,
        "facts": facts,
    }
    first = InstallationAuthorityCoordinator(
        owner_instance_id="installation-host-a", **shared
    )
    second = InstallationAuthorityCoordinator(
        owner_instance_id="installation-host-b", **shared
    )
    first_closed = False
    second_closed = False
    try:
        first_route = first.start()
        second_route = second.start()
        created_response = first_route.dispatch(
            _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "Failover"},
                request_id="req-s1-failover-create",
                idempotency_key="s1-failover-create",
            )
        )
        assert isinstance(created_response, SuccessResponseEnvelope), created_response
        created = WorkspaceCreateResult.from_wire(created_response.result)

        first.close()
        first_closed = True
        listed_response = second_route.dispatch(
            _request(
                WORKSPACE_LIST_OPERATION,
                {"limit": 100},
                request_id="req-s1-failover-list",
            )
        )
        assert isinstance(listed_response, SuccessResponseEnvelope), listed_response
        listed = WorkspaceListResult.from_wire(listed_response.result)
        assert tuple(item.workspace_id for item in listed.workspaces) == (
            created.workspace.workspace_id,
        )

        second.close()
        second_closed = True
        connection = sqlite3.connect(
            installation_root / "catalogue" / "installation.sqlite"
        )
        try:
            authority = connection.execute(
                "SELECT owner_instance_id, fencing_generation "
                "FROM omnivia_installation_state WHERE singleton = 1"
            ).fetchone()
            assert authority == ("installation-host-b", 2)
        finally:
            connection.close()
    finally:
        if not first_closed:
            first.close()
        if not second_closed:
            second.close()
        shutil.rmtree(root, ignore_errors=True)


def test_v06_5_s1_workspace_family_harness_executes_exact_18_adapter_cases(
    tmp_path: Path,
) -> None:
    """The frozen six S1 cases execute through all three production adapters."""
    expected_cases = {
        "workspace.create/primary-success",
        "workspace.create/honest-replay",
        "workspace.create/idempotency-conflict",
        "error/bootstrap_in_progress",
        "workspace.list/primary-success",
        "workspace.list/page-2",
    }
    ledger: list[tuple[str, str]] = []
    for adapter in ("in-process", "local-ipc", "http"):
        root = tmp_path / adapter
        dispatcher, store = _path(root)
        try:
            primary_request = _request(
                WORKSPACE_CREATE_OPERATION,
                {"display_name": "Alpha"},
                request_id=f"req-{adapter}-primary",
                idempotency_key=f"idem-{adapter}-workspace-create",
            )
            primary_response = _transport_call(
                adapter,
                dispatcher,
                primary_request,
                root,
                case_id="workspace.create/primary-success",
            )
            assert isinstance(primary_response, SuccessResponseEnvelope)
            primary = WorkspaceCreateResult.from_wire(primary_response.result)
            assert primary.workspace.display_name == "Alpha"
            ledger.append(("workspace.create/primary-success", adapter))

            replay_response = _transport_call(
                adapter,
                dispatcher,
                replace(
                    primary_request,
                    metadata=replace(
                        primary_request.metadata,
                        request_id=f"req-{adapter}-replay",
                    ),
                ),
                root,
                case_id="workspace.create/honest-replay",
            )
            assert isinstance(replay_response, SuccessResponseEnvelope)
            assert WorkspaceCreateResult.from_wire(replay_response.result) == primary
            ledger.append(("workspace.create/honest-replay", adapter))

            conflict_response = _transport_call(
                adapter,
                dispatcher,
                replace(
                    primary_request,
                    metadata=replace(
                        primary_request.metadata,
                        request_id=f"req-{adapter}-conflict",
                    ),
                    input={"display_name": "Alpha (second request)"},
                ),
                root,
                case_id="workspace.create/idempotency-conflict",
            )
            assert isinstance(conflict_response, ErrorResponseEnvelope)
            assert conflict_response.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
            ledger.append(("workspace.create/idempotency-conflict", adapter))

            for index in (2, 3):
                seeded = _transport_call(
                    adapter,
                    dispatcher,
                    _request(
                        WORKSPACE_CREATE_OPERATION,
                        {"display_name": f"Alpha {index}"},
                        request_id=f"req-{adapter}-seed-{index}",
                        idempotency_key=f"idem-{adapter}-seed-{index}",
                    ),
                    root,
                )
                assert isinstance(seeded, SuccessResponseEnvelope)

            first_page_response = _transport_call(
                adapter,
                dispatcher,
                _request(
                    WORKSPACE_LIST_OPERATION,
                    {"limit": 2},
                    request_id=f"req-{adapter}-list-first",
                ),
                root,
                case_id="workspace.list/primary-success",
            )
            assert isinstance(first_page_response, SuccessResponseEnvelope)
            first_page = WorkspaceListResult.from_wire(first_page_response.result)
            assert len(first_page.workspaces) == 2
            assert first_page.page.continuation_token is not None
            ledger.append(("workspace.list/primary-success", adapter))

            second_page_response = _transport_call(
                adapter,
                dispatcher,
                _request(
                    WORKSPACE_LIST_OPERATION,
                    {
                        "limit": 2,
                        "page": {
                            "continuation_token": first_page.page.continuation_token
                        },
                    },
                    request_id=f"req-{adapter}-list-second",
                ),
                root,
                case_id="workspace.list/page-2",
            )
            assert isinstance(second_page_response, SuccessResponseEnvelope)
            second_page = WorkspaceListResult.from_wire(second_page_response.result)
            assert len(second_page.workspaces) == 1
            assert second_page.page.continuation_token is None
            ledger.append(("workspace.list/page-2", adapter))
        finally:
            store.close()

        def busy(**_kwargs: Any) -> WorkspaceInitResult:
            return WorkspaceInitResult(
                status=WorkspaceInitStatus.REFUSED,
                refusal=WorkspaceInitRefusal.WORKSPACE_BUSY,
                reason="test-only contention",
            )

        busy_root = tmp_path / f"{adapter}-busy"
        busy_dispatcher, busy_store = _path(busy_root, bootstrapper=busy)
        try:
            busy_response = _transport_call(
                adapter,
                busy_dispatcher,
                _request(
                    WORKSPACE_CREATE_OPERATION,
                    {"display_name": "Alpha"},
                    request_id=f"req-{adapter}-busy",
                    idempotency_key=f"idem-{adapter}-busy",
                ),
                busy_root,
                case_id="error/bootstrap_in_progress",
            )
            assert isinstance(busy_response, ErrorResponseEnvelope)
            assert busy_response.error.code == ERROR_CODE_BOOTSTRAP_IN_PROGRESS
            ledger.append(("error/bootstrap_in_progress", adapter))
        finally:
            busy_store.close()

    assert len(ledger) == 18
    assert len(set(ledger)) == 18
    assert {case for case, _adapter in ledger} == expected_cases
    assert {adapter for _case, adapter in ledger} == {
        "in-process",
        "local-ipc",
        "http",
    }
