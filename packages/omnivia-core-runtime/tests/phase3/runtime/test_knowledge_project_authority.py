"""DEV-REQ-081: the server's Project document, and the production paths that compose it.

The bindings a service serves come from one operator-owned document in the installation's catalogue
directory (`service/knowledge_projects.py`). These tests load that document, compose the production
surface with it, and drive it over the real local socket. Two kinds of caller reach it: the configured
`local-user` owner for plain requests, and installed-MCP principals whose bearer credentials are provisioned
by `InstalledMcpAuthority.configure` under the installation administrator and resolved on every call by
`OwnedInstalledMcp`, the seam the service serves, through `AuthenticatedApplicationDispatch`. The document is
read by a direct `main()` start and by a managed start's child, and both refuse an unusable document before
they serve.

The positive flow uses two installed-MCP principals, one per supported host, both authoring setups. Each
holds exactly the four sharing operations their profile grants, and the server's Project document decides
which of them owns the source Project and which is a member of the recipient Project.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import test_dev_req_081_knowledge_sharing as c16
import test_installed_mcp_local_control as ilc
import test_managed_start as tms
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service import main as service_main
from omnivia_core_runtime.service.application import KNOWLEDGE_SHARING_FAMILY_PURPOSES
from omnivia_core_runtime.service.installed_mcp import InstalledMcpAuthority
from omnivia_core_runtime.service.knowledge_projects import (
    KNOWLEDGE_PROJECTS_FILE,
    KNOWLEDGE_PROJECTS_SCHEMA,
    KnowledgeProjectsRefused,
    load_project_authorities,
)
from omnivia_core_runtime.service.knowledge_sharing import (
    NO_PROJECTS,
    OPERATION_DECIDE,
    OPERATION_LINEAGE,
    OPERATION_PROPOSE,
    OPERATION_READ,
    ProjectAuthority,
)
from omnivia_core_runtime.service.local_control import (
    LOCAL_CONTROL_FIELD,
    LOCAL_CONTROL_VERSION,
    LocalControlError,
    LocalControlKind,
)
from omnivia_core_runtime.service.mcp_control import (
    AuthenticatedApplicationDispatch,
    OwnedInstalledMcp,
)
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import (
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
    LocalSocketTransport,
)
from omnivia_core_runtime.storage.installation_store import (
    InstallationStore,
    McpHost,
    McpProfile,
    open_installation_store,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ErrorResponseEnvelope,
    PrincipalClaim,
    RequestEnvelope,
    SuccessResponseEnvelope,
    decode_response,
    encode_request,
    get_operation_metadata,
)

#: The two fixtures this file uses: a migrated workspace with one accepted governed version (C16), and a
#: bootstrapped managed-start installation. Aliased rather than imported, so pytest finds them by name.
owned = c16.owned
home = tms.home

WS = c16.WS
SHARE = c16.SHARE
RECORD = c16.RECORD
CONTENT = c16.CONTENT
SOURCE = c16.SOURCE
RECIPIENT = c16.RECIPIENT
RECIPIENT_OWNER = c16.RECIPIENT_OWNER
PROPOSER = c16.PROPOSER
APPROVER = c16.APPROVER
RECIPIENT_READER = c16.RECIPIENT_READER
OTHER_OWNER = c16.OTHER_OWNER
OTHER_READER = c16.OTHER_READER
LOCAL_OWNER = c16.PRINCIPAL
OTHER_WORKSPACE = "ws-elsewhere"
OBSERVED_AT = "2026-09-12T00:00:00Z"
_ABSENT = object()
_REQUESTS = itertools.count(1)


# --- the document ------------------------------------------------------------------


def _project(
    project_id: str,
    domain_scope: str,
    owners: list[str],
    members: list[str] | None = None,
    *,
    workspace: str = WS,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace,
        "project_id": project_id,
        "domain_scope": domain_scope,
        "owners": owners,
        "members": [] if members is None else members,
    }


def _document(*projects: Any) -> str:
    return json.dumps({"schema": KNOWLEDGE_PROJECTS_SCHEMA, "projects": list(projects)})


def _bound_document(workspace: str = WS) -> str:
    """The C16 authority, written the way an operator would state it."""
    return _document(
        _project(SOURCE, "product.core", [PROPOSER, APPROVER], workspace=workspace),
        _project(
            RECIPIENT,
            "product.recipient",
            [RECIPIENT_OWNER],
            [RECIPIENT_READER],
            workspace=workspace,
        ),
        _project(
            "project-other",
            "product.other",
            [OTHER_OWNER],
            [OTHER_READER],
            workspace=workspace,
        ),
    )


def _installation(tmp_path: Path) -> Path:
    root = tmp_path / "installation-state"
    (root / "catalogue").mkdir(parents=True)
    return root


def _write_document(root: Path, payload: str | bytes, *, mode: int = 0o600) -> Path:
    path = root / "catalogue" / KNOWLEDGE_PROJECTS_FILE
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    path.chmod(mode)
    return path


# --- loading the document ----------------------------------------------------------


def test_no_document_binds_no_project(tmp_path: Path) -> None:
    assert load_project_authorities(tmp_path / "installation-state") == {}


def test_the_document_states_the_authority_the_c16_tests_compose(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path)
    _write_document(root, _bound_document())
    assert load_project_authorities(root)[WS] == c16.AUTHORITY


def test_each_workspace_is_bound_on_its_own(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write_document(
        root,
        _document(
            _project(SOURCE, "product.core", [PROPOSER]),
            _project(SOURCE, "product.core", [OTHER_OWNER], workspace=OTHER_WORKSPACE),
        ),
    )
    authorities = load_project_authorities(root)
    here = authorities[WS].project(SOURCE)
    there = authorities[OTHER_WORKSPACE].project(SOURCE)
    assert here is not None and here.owners == frozenset({PROPOSER})
    assert there is not None and there.owners == frozenset({OTHER_OWNER})
    assert authorities[WS].project("project-elsewhere") is None


def _one(**changes: Any) -> str:
    """One Project binding with its members replaced, or removed where the value is `_ABSENT`."""
    project = _project(SOURCE, "product.core", [PROPOSER])
    for key, value in changes.items():
        if value is _ABSENT:
            del project[key]
        else:
            project[key] = value
    return _document(project)


_UNUSABLE = [
    pytest.param("{not json", id="not-json"),
    pytest.param(b"\xff\xfe\x00", id="not-utf8"),
    pytest.param("[]", id="not-an-object"),
    pytest.param(
        '{"schema": "'
        + KNOWLEDGE_PROJECTS_SCHEMA
        + '", "schema": "'
        + KNOWLEDGE_PROJECTS_SCHEMA
        + '", "projects": []}',
        id="duplicate-member",
    ),
    pytest.param(
        '{"schema": "'
        + KNOWLEDGE_PROJECTS_SCHEMA
        + '", "projects": [], "surprise": 1}',
        id="extra-member",
    ),
    pytest.param(
        '{"schema": "omnivia.knowledge-projects.v2", "projects": []}',
        id="other-version",
    ),
    pytest.param(_one(owners=[float("nan")]), id="non-standard-number"),
    pytest.param(_one(project_id=17), id="project-id-not-text"),
    pytest.param(_one(domain_scope="Product.Core"), id="domain-scope-malformed"),
    pytest.param(_one(workspace_id="not a workspace!"), id="workspace-malformed"),
    pytest.param(_one(owners=[]), id="no-owner"),
    pytest.param(_one(owners=[PROPOSER, PROPOSER]), id="owner-listed-twice"),
    pytest.param(_one(members="reader"), id="members-not-a-list"),
    pytest.param(_one(members=[1]), id="member-not-text"),
    pytest.param(_one(members=_ABSENT), id="member-missing"),
    pytest.param(_one(surprise=True), id="binding-extra-member"),
    pytest.param(
        _document(
            _project(SOURCE, "product.core", [PROPOSER]),
            _project(SOURCE, "product.other", [OTHER_OWNER]),
        ),
        id="project-bound-twice",
    ),
    pytest.param(
        _document(
            _project(SOURCE, "product.core", [PROPOSER]),
            _project(RECIPIENT, "product.core", [OTHER_OWNER]),
        ),
        id="domain-scope-bound-twice",
    ),
    pytest.param(
        _document(
            *[_project(f"project-{n}", f"product.p{n}", [PROPOSER]) for n in range(65)]
        ),
        id="too-many-projects",
    ),
]


@pytest.mark.parametrize("payload", _UNUSABLE)
def test_an_unusable_document_refuses_rather_than_binding_less(
    tmp_path: Path, payload: str | bytes
) -> None:
    root = _installation(tmp_path)
    _write_document(root, payload)
    with pytest.raises(KnowledgeProjectsRefused):
        load_project_authorities(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and symlinks")
def test_a_document_other_principals_can_write_is_refused(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write_document(root, _bound_document(), mode=0o664)
    with pytest.raises(KnowledgeProjectsRefused, match="writable by other principals"):
        load_project_authorities(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_document_reached_through_a_symlink_is_refused(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(_bound_document(), encoding="utf-8")
    target.chmod(0o600)
    (root / "catalogue" / KNOWLEDGE_PROJECTS_FILE).symlink_to(target)
    with pytest.raises(KnowledgeProjectsRefused, match="cannot be read"):
        load_project_authorities(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory handling")
def test_a_directory_in_the_document_place_is_refused(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    (root / "catalogue" / KNOWLEDGE_PROJECTS_FILE).mkdir()
    with pytest.raises(KnowledgeProjectsRefused, match="not a regular file"):
        load_project_authorities(root)


def test_an_oversized_document_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    _write_document(root, _bound_document() + " " * 70_000)
    with pytest.raises(KnowledgeProjectsRefused, match="too large"):
        load_project_authorities(root)


# --- the production socket, with installed-MCP principals -------------------------


@dataclass(frozen=True)
class _Installation:
    """One installation: its workspace registered, and its installed-MCP setups provisioned.

    `principals` and `bearers` are keyed by host name. A bearer is the secret `configure` returned
    once, and a principal is the id the setup minted, which is what the Project document names.
    """

    root: Path
    store: InstallationStore
    principals: Mapping[str, str]
    bearers: Mapping[str, str]


@contextmanager
def _installation_with(
    tmp_path: Path, *setups: tuple[McpHost, McpProfile]
) -> Iterator[_Installation]:
    """An installation with `WS` registered and one installed-MCP setup per (host, profile)."""
    root = (tmp_path / "installation").resolve()
    store = open_installation_store(
        root,
        owner_instance_id="installation-service",
        clock_us=ilc.TickClock(),
        installation_id_factory=lambda: ilc.INSTALLATION_ID,
    )
    try:
        assert ilc.register_workspace(store, WS.removeprefix("ws-")) == WS
        authority = InstalledMcpAuthority(store)
        principals: dict[str, str] = {}
        bearers: dict[str, str] = {}
        for host, profile in setups:
            provisioning = authority.configure(
                ilc.administrator(),
                host=host,
                workspace_id=WS,
                profile=profile,
                authoring_intent=profile is McpProfile.AUTHORING,
            )
            assert provisioning.secret is not None
            principals[host.value] = provisioning.setup.principal_id
            bearers[host.value] = provisioning.secret.reveal()
        yield _Installation(
            root=root, store=store, principals=principals, bearers=bearers
        )
    finally:
        store.close()


def _probes() -> ProbeRouter:
    return ProbeRouter(
        facts=lambda: ServiceFacts(
            observed_at=OBSERVED_AT,
            health_status="pass",
            readiness_status="pass",
            discovery_status="pass",
        ),
        capabilities=tuple,
        clock=lambda: 0,
    )


@contextmanager
def _socket(
    holder: Any, authority: ProjectAuthority, installation: _Installation
) -> Iterator[LocalEndpoint]:
    """The production surface behind a real local socket, authenticating through the real seam.

    The seam is `OwnedInstalledMcp` over the installation's own store, the same object the service
    serves. Nothing here stands in for credential resolution.
    """
    surface = c16._surface(holder, authority)
    seam = OwnedInstalledMcp(
        authority=InstalledMcpAuthority(installation.store),
        administrator=ilc.administrator(),
    )
    directory = tempfile.mkdtemp(prefix="kpa-", dir="/tmp")
    try:
        endpoint = LocalEndpoint(EndpointScheme.UNIX, str(Path(directory) / "s.sock"))
        server = LocalSocketServer(
            router=DocumentRouter(probes=_probes(), dispatch=surface.dispatch),
            authenticated=AuthenticatedApplicationDispatch(
                seam=seam, dispatcher=surface
            ),
            mcp_administration=seam,
            endpoint=endpoint,
            timeout=5.0,
            gate=threading.RLock(),
        )
        with server:
            yield endpoint
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _envelope(
    operation: str, payload: Mapping[str, Any], **metadata: Any
) -> RequestEnvelope:
    entry = get_operation_metadata(operation)
    request_id = f"req-kpa-{next(_REQUESTS)}"
    overrides: dict[str, Any] = {
        "request_id": request_id,
        "correlation_id": f"cor-{request_id}",
        "trace_id": f"trc-{request_id}",
        "purpose": KNOWLEDGE_SHARING_FAMILY_PURPOSES[operation],
        "workspace_id": WS,
    }
    if entry.idempotency.supports_idempotency_key:
        overrides["idempotency_key"] = f"idem-{request_id}"
    overrides.update(metadata)
    return s0.envelope_for(entry, operation_input=dict(payload), **overrides)


def _send(
    endpoint: LocalEndpoint, envelope: RequestEnvelope, *, credential: str | None = None
) -> Any:
    """One request over the socket: plain, as the configured owner, or resolved from a bearer."""
    transport = LocalSocketTransport(endpoint=endpoint, timeout=5.0)
    if credential is None:
        return transport.call(envelope)
    answer = transport.exchange(
        {
            LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
            "kind": LocalControlKind.APPLICATION_CALL.value,
            "credential": credential,
            "request": encode_request(envelope),
        }
    )
    if "error" in answer:
        return answer["error"]
    result = answer["result"]
    assert isinstance(result, dict)
    return decode_response(result["response"])


def _code(outcome: Any) -> str:
    if isinstance(outcome, Mapping):
        return str(outcome["code"])
    assert isinstance(outcome, ErrorResponseEnvelope), outcome
    return str(outcome.error.code)


def _result(outcome: Any) -> dict[str, Any]:
    assert isinstance(outcome, SuccessResponseEnvelope), outcome
    return dict(outcome.to_wire()["result"])


def _propose(**extra: Any) -> dict[str, Any]:
    return {
        "share_id": SHARE,
        "record_id": RECORD,
        "recipient_project_id": RECIPIENT,
        **extra,
    }


def _accept() -> dict[str, Any]:
    return {"share_id": SHARE, "decision": "accepted"}


def _revoke() -> dict[str, Any]:
    return {"share_id": SHARE, "decision": "revoked"}


def test_the_positive_flow_runs_over_the_socket_for_two_installed_owners(
    owned: Any, tmp_path: Path
) -> None:
    with _installation_with(
        tmp_path,
        (McpHost.CLAUDE_CODE, McpProfile.AUTHORING),
        (McpHost.CODEX, McpProfile.AUTHORING),
    ) as installed:
        proposer = installed.principals["claude-code"]
        approver = installed.principals["codex"]
        proposer_token = installed.bearers["claude-code"]
        approver_token = installed.bearers["codex"]
        _write_document(
            installed.root,
            _document(
                _project(SOURCE, "product.core", [proposer, approver]),
                _project(RECIPIENT, "product.recipient", [RECIPIENT_OWNER], [approver]),
            ),
        )
        authority = load_project_authorities(installed.root)[WS]
        with _socket(owned, authority, installed) as endpoint:
            proposed = _result(
                _send(
                    endpoint,
                    _envelope(OPERATION_PROPOSE, _propose()),
                    credential=proposer_token,
                )
            )
            assert proposed["state"] == "proposed"

            # The proposer owns the source but may not accept its own share.
            self_accept = _send(
                endpoint,
                _envelope(OPERATION_DECIDE, _accept()),
                credential=proposer_token,
            )
            assert _code(self_accept) == ERROR_CODE_AUTHORIZATION_DENIED

            accepted = _result(
                _send(
                    endpoint,
                    _envelope(OPERATION_DECIDE, _accept()),
                    credential=approver_token,
                )
            )
            assert accepted["state"] == "accepted"

            served = _result(
                _send(
                    endpoint,
                    _envelope(OPERATION_READ, {"share_id": SHARE}),
                    credential=approver_token,
                )
            )
            assert served["content"] == CONTENT

            # The proposer owns the source, not the recipient, so it reads nothing.
            outsider = _send(
                endpoint,
                _envelope(OPERATION_READ, {"share_id": SHARE}),
                credential=proposer_token,
            )
            assert _code(outsider) == ERROR_CODE_NOT_FOUND

            revoked = _result(
                _send(
                    endpoint,
                    _envelope(OPERATION_DECIDE, _revoke()),
                    credential=approver_token,
                )
            )
            assert revoked["state"] == "revoked"

            # The very next read is refused, because nothing about the share was remembered.
            after = _send(
                endpoint,
                _envelope(OPERATION_READ, {"share_id": SHARE}),
                credential=approver_token,
            )
            assert _code(after) == ERROR_CODE_CONFLICT

            # The source owner keeps the lineage after revocation, with both principals named.
            lineage = _result(
                _send(
                    endpoint,
                    _envelope(OPERATION_LINEAGE, {"share_id": SHARE}),
                    credential=proposer_token,
                )
            )
            assert lineage["state"] == "revoked"
            assert lineage["proposed_by"] == proposer
            assert [
                (entry["decision"], entry["decided_by"])
                for entry in lineage["decisions"]
            ] == [("accepted", approver), ("revoked", approver)]


def test_a_bearer_cannot_claim_another_installed_principal(
    owned: Any, tmp_path: Path
) -> None:
    with _installation_with(
        tmp_path,
        (McpHost.CLAUDE_CODE, McpProfile.AUTHORING),
        (McpHost.CODEX, McpProfile.AUTHORING),
    ) as installed:
        proposer = installed.principals["claude-code"]
        approver = installed.principals["codex"]
        _write_document(
            installed.root,
            _document(_project(SOURCE, "product.core", [proposer, approver])),
        )
        authority = load_project_authorities(installed.root)[WS]
        with _socket(owned, authority, installed) as endpoint:
            proposer_token = installed.bearers["claude-code"]
            _send(
                endpoint,
                _envelope(OPERATION_PROPOSE, _propose()),
                credential=proposer_token,
            )
            forged = _envelope(
                OPERATION_DECIDE,
                _accept(),
                principal_claim=PrincipalClaim(claimed_principal_id=approver),
            )
            assert _code(_send(endpoint, forged, credential=proposer_token)) == (
                ERROR_CODE_AUTHORIZATION_DENIED
            )


def test_a_payload_cannot_name_the_source_project(owned: Any, tmp_path: Path) -> None:
    with _installation_with(
        tmp_path, (McpHost.CLAUDE_CODE, McpProfile.AUTHORING)
    ) as installed:
        proposer = installed.principals["claude-code"]
        _write_document(
            installed.root, _document(_project(SOURCE, "product.core", [proposer]))
        )
        authority = load_project_authorities(installed.root)[WS]
        with _socket(owned, authority, installed) as endpoint:
            smuggled = _envelope(
                OPERATION_PROPOSE, _propose(source_project_id="project-other")
            )
            assert (
                _code(
                    _send(
                        endpoint, smuggled, credential=installed.bearers["claude-code"]
                    )
                )
                == ERROR_CODE_INVALID_REQUEST
            )


def test_an_unknown_bearer_is_refused_before_any_sharing_operation(
    owned: Any, tmp_path: Path
) -> None:
    with (
        _installation_with(
            tmp_path, (McpHost.CLAUDE_CODE, McpProfile.AUTHORING)
        ) as installed,
        _socket(owned, NO_PROJECTS, installed) as endpoint,
    ):
        refused = _send(
            endpoint,
            _envelope(OPERATION_PROPOSE, _propose()),
            credential="a-bearer-that-was-never-issued",
        )
        assert _code(refused) == LocalControlError.UNAUTHENTICATED.value


def test_without_a_document_every_sharing_call_refuses_over_the_socket(
    owned: Any, tmp_path: Path
) -> None:
    with (
        _installation_with(
            tmp_path, (McpHost.CLAUDE_CODE, McpProfile.AUTHORING)
        ) as installed,
        _socket(owned, NO_PROJECTS, installed) as endpoint,
    ):
        bearer_refusal = _send(
            endpoint,
            _envelope(OPERATION_PROPOSE, _propose()),
            credential=installed.bearers["claude-code"],
        )
        assert _code(bearer_refusal) == ERROR_CODE_AUTHORIZATION_DENIED
        plain_refusal = _send(endpoint, _envelope(OPERATION_PROPOSE, _propose()))
        assert _code(plain_refusal) == ERROR_CODE_AUTHORIZATION_DENIED


def test_a_restricted_installed_principal_holds_no_sharing_operation(
    owned: Any, tmp_path: Path
) -> None:
    """Least privilege through the real path: the document names this principal as owner and member,
    and the restricted profile still grants neither sharing mutation nor sharing read."""
    with _installation_with(
        tmp_path, (McpHost.CLAUDE_CODE, McpProfile.RESTRICTED)
    ) as installed:
        principal = installed.principals["claude-code"]
        token = installed.bearers["claude-code"]
        _write_document(
            installed.root,
            _document(
                _project(SOURCE, "product.core", [principal]),
                _project(
                    RECIPIENT, "product.recipient", [RECIPIENT_OWNER], [principal]
                ),
            ),
        )
        authority = load_project_authorities(installed.root)[WS]
        with _socket(owned, authority, installed) as endpoint:
            assert (
                _code(
                    _send(
                        endpoint,
                        _envelope(OPERATION_PROPOSE, _propose()),
                        credential=token,
                    )
                )
                == ERROR_CODE_AUTHORIZATION_DENIED
            )
            assert (
                _code(
                    _send(
                        endpoint,
                        _envelope(OPERATION_READ, {"share_id": SHARE}),
                        credential=token,
                    )
                )
                == ERROR_CODE_AUTHORIZATION_DENIED
            )


def test_the_local_owner_binds_by_document_and_serves_propose_and_lineage_only(
    owned: Any, tmp_path: Path
) -> None:
    """The configured local owner can propose and read lineage, and never accept its own share."""
    with _installation_with(tmp_path) as installed:
        _write_document(
            installed.root,
            _document(
                _project(SOURCE, "product.core", [LOCAL_OWNER]),
                _project(RECIPIENT, "product.recipient", [RECIPIENT_OWNER]),
            ),
        )
        authority = load_project_authorities(installed.root)[WS]
        with _socket(owned, authority, installed) as endpoint:
            proposed = _result(
                _send(endpoint, _envelope(OPERATION_PROPOSE, _propose()))
            )
            assert proposed["state"] == "proposed"
            assert (
                _code(_send(endpoint, _envelope(OPERATION_DECIDE, _accept())))
                == ERROR_CODE_AUTHORIZATION_DENIED
            )
            lineage = _result(
                _send(endpoint, _envelope(OPERATION_LINEAGE, {"share_id": SHARE}))
            )
            assert lineage["state"] == "proposed"


# --- the startup paths -------------------------------------------------------------


def test_direct_startup_refuses_an_unusable_document_before_it_serves(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path)
    _write_document(root, "{not json")
    socket_dir = Path(tempfile.mkdtemp(prefix="kpd-", dir="/tmp"))
    try:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = service_main.main(
                [
                    "--workspace",
                    str(tmp_path / "workspace"),
                    "--installation-state",
                    str(root),
                    "--endpoint",
                    f"unix://{socket_dir / 's.sock'}",
                ]
            )
        assert status == 2
        assert "not a valid version 1 document" in stderr.getvalue()
        assert not (socket_dir / "s.sock").exists()
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


class _ComposedWithoutServing(Exception):
    """Raised by the composition spy once the startup has reached the point under test."""


@pytest.mark.parametrize(
    "bound",
    [
        pytest.param(True, id="document-binds-the-workspace"),
        pytest.param(False, id="no-document"),
    ],
)
def test_the_startup_path_composes_its_workspace_with_the_document_it_read(
    home: Path, monkeypatch: pytest.MonkeyPatch, bound: bool
) -> None:
    """The real startup, up to composition, hands the surface this workspace's own authority."""
    if bound:
        _place(home, _bound_document(tms.WORKSPACE_ID))
    composed: list[ProjectAuthority] = []

    def compose_then_stop(**kwargs: Any) -> Any:
        composed.append(kwargs["project_authority"])
        raise _ComposedWithoutServing

    monkeypatch.setattr(
        service_main, "_build_production_application_surface", compose_then_stop
    )
    socket_dir = Path(tempfile.mkdtemp(prefix="kps-", dir="/tmp"))
    try:
        with redirect_stderr(io.StringIO()):
            status = service_main.main(
                [
                    "--workspace",
                    str(home / "workspace"),
                    "--installation-state",
                    str(home / "installation-state"),
                    "--endpoint",
                    f"unix://{socket_dir / 's.sock'}",
                ]
            )
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)
    # Startup stopped at composition by design, so it did not become ready.
    assert status == 1
    assert composed == [c16.AUTHORITY if bound else NO_PROJECTS]


def _place(home: Path, payload: str) -> None:
    """The document in the installation the managed-start fixture bootstrapped."""
    installation = home / "installation-state"
    (installation / "catalogue").mkdir(parents=True, exist_ok=True)
    _write_document(installation, payload)


def test_a_managed_child_refuses_an_unusable_document_and_the_launcher_reports_why(
    home: Path,
) -> None:
    _place(home, "{not json")
    status, document, _stderr = tms._run(home)
    assert status == 1
    assert document["status"] == "failed"
    assert document["failure"] == "spawn_failure"
    assert "not a valid version 1 document" in document["child_output"]


def test_a_started_service_keeps_the_document_it_started_with(home: Path) -> None:
    _place(home, _bound_document(tms.WORKSPACE_ID))
    status, started, _stderr = tms._run(home)
    assert status == 0
    assert started["status"] == "started"

    # Attaching to the running service does not read the document again, so a later change to it
    # cannot replace the authority that service was started with.
    _place(home, "{not json")
    status, attached, _stderr = tms._run(home)
    assert status == 0
    assert attached["status"] == "attached"
