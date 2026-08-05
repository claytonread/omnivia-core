"""M2: the accepted client discovers the endpoint the Runtime actually publishes.

The one test that demonstrates the descriptor publication lane achieved its
purpose. Everything on the path is production code: the real `ServiceRunner`
publishes as the last step of a real startup, and the accepted P1b client's real
`discover_endpoint` derives its own path from the installation root, checks the
file's provenance and the mode of both directories above it, decodes the public
descriptor, refuses anything that is not this platform's local IPC endpoint,
negotiates all three versions, and verifies the result against a live
`service.discover` probe carried over the service's own socket.

It lives in phase3 rather than beside the permission evidence in phase2 because it
imports `omnivia_core_client`. `phase2-platform.yml` installs the runtime, the CLI
and MCP but not the client package, so a phase2 module importing it fails to
collect on every row of that matrix -- which is exactly what happened. phase3 is
collected by `core-acceptance.yml`'s broad run, which does install the client, and
is not collected by the platform matrix at all.

Splitting by dependency rather than marking the import optional is deliberate. A
`pytest.importorskip` here would have turned the same CI failure into a silent
skip, and an end-to-end proof that quietly does not run is the failure mode this
whole lane exists to prevent.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.discovery import discover_endpoint
from omnivia_core_client.transport import enforce_send_preconditions
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import _router_for
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import (
    LocalEndpoint,
    LocalSocketServer,
    endpoint_for_path,
)
from omnivia_core_runtime.service.versions import API_VERSION, PROTOCOL_VERSION
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    ServiceProbeRequest,
    ServiceProbeResult,
    decode_service_probe_result,
)
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

WORKSPACE_ID = "ws-m2-discovery-0001"
SERVICE_INSTANCE = "svc-m2-1"
WORKSPACE_FORMAT_ORDINAL = "1"


class LocalProbeTransport:
    """The accepted client's transport seam, wired to the real service socket.

    Injected exactly as `ClientTransport` intends. It carries the client's own
    `ServiceProbeRequest` over the service's real endpoint as a real OVC1 frame and
    decodes the answer with the public contract decoder, so the probe the client
    verifies against is the running service's answer and not this test's.
    """

    def __init__(self, endpoint: LocalEndpoint) -> None:
        self.endpoint = endpoint

    def call(self, request: object, **_: object) -> object:  # pragma: no cover
        raise NotImplementedError("discovery uses the probe branch only")

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        remaining = enforce_send_preconditions(
            deadline=deadline, cancellation=cancellation, operation=request.probe
        )
        payload: dict[str, Any] = {"probe": request.probe}
        if request.deadline_ms is not None:
            payload["deadline_ms"] = request.deadline_ms

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(max(remaining, 0.001))
        try:
            connection.connect(self.endpoint.name)
            connection.sendall(encode_frame(payload))
            header = connection.recv(8)
            assert len(header) == 8
            length = int.from_bytes(header[4:], "big")
            body = b""
            while len(body) < length:
                chunk = connection.recv(length - len(body))
                assert chunk
                body += chunk
        finally:
            connection.close()
        return decode_service_probe_result(decode_frame(header + body))


def serve_probes(endpoint: LocalEndpoint):
    """The production serve hook: the real router on the real local socket."""

    def serve(started: ServiceRunner) -> None:
        assert started.workspace_id is not None
        dispatcher = Dispatcher.for_service_operations(
            Grant(
                principal="local-user",
                workspaces=frozenset({started.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            started,
        )
        server = LocalSocketServer(
            router=_router_for(started, dispatcher), endpoint=endpoint
        )
        server.start()
        started.lifecycle.resources.push("socket_server", server.stop)

    return serve


def migrated_workspace(tmp_path: Path) -> tuple[WorkspaceLayout, InstallationLayout]:
    """A workspace this Runtime can own, built from the frozen Phase 0 baseline."""
    legacy = tmp_path / "legacy" / "source.sqlite"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(legacy)

    workspace = WorkspaceLayout(root=tmp_path / "workspace")
    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    migrate_legacy_database(
        legacy,
        workspace,
        installation,
        WorkspaceManifest(
            workspace_id=WORKSPACE_ID,
            created_at="2026-07-30T00:00:00+00:00",
            name="M2",
            compatibility=CoreCompatibility(
                workspace_format_version=WORKSPACE_FORMAT_ORDINAL,
                min_core_version="0.1.0",
            ),
        ),
        service_instance_id=SERVICE_INSTANCE,
    )
    return workspace, installation


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix domain socket"
)
@pytest.mark.skipif(
    os.name == "nt", reason="the client dials pipe:// on Windows, which this omits"
)
def test_the_accepted_client_discovers_what_the_runtime_publishes(
    tmp_path: Path,
) -> None:
    """M2: the real writer's file is read and accepted by the real client.

    Nothing is normalised. This test used to patch `ownership.identity._boot_id` to
    a valid `Identifier` because the real one was not always one: on macOS it
    returned the raw `sysctl kern.boottime` output, `'{ sec = ... } Sat Jul 25
    19:34:27 2026'`, and the Runtime's own probe router holds `process.boot_id` to
    the public `Identifier` grammar, so `service.discover` could not be answered on
    that platform at all. That defect is repaired --
    `tests/phase2/test_process_identity.py` holds `_boot_id()` to the generated
    pattern on whichever platform the row is running -- so the value the writer
    publishes and the client verifies here is now this host's real one.
    """
    workspace, installation = migrated_workspace(tmp_path)

    # AF_UNIX paths are capped well below pytest's tmp_path length.
    socket_directory = Path(tempfile.mkdtemp(prefix="ovm2-", dir=tempfile.gettempdir()))
    endpoint = endpoint_for_path(socket_directory / "s.sock")
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace.root,
            installation_root=installation.root,
            core_version="0.1.0",
            endpoint=endpoint.url,
        )
    )
    try:
        report = runner.start(serve=serve_probes(endpoint))
        assert report.ready, report.to_dict()

        # The client is handed the installation root, nothing else. It finds the
        # file the Runtime just wrote by deriving the one path it accepts.
        published = installation.runtime_for(WORKSPACE_ID) / "service.json"
        assert published.is_file()

        discovered = discover_endpoint(
            installation.root,
            WORKSPACE_ID,
            transport=LocalProbeTransport(endpoint),
            deadline=Deadline.after(30.0),
        )

        assert discovered is not None
        assert discovered.descriptor.endpoint_uri == endpoint.url
        assert discovered.descriptor.workspace_id == WORKSPACE_ID
        assert discovered.descriptor.service_instance_id == report.service_instance_id
        assert discovered.descriptor.fencing_generation == report.fencing_generation
        assert discovered.descriptor.ready
        assert discovered.negotiated.api_version == API_VERSION
        assert discovered.negotiated.protocol_version == PROTOCOL_VERSION
        assert discovered.negotiated.descriptor_version == API_VERSION
    finally:
        runner.stop()
        shutil.rmtree(socket_directory, ignore_errors=True)
