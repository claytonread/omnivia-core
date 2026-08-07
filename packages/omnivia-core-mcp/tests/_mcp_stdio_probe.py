"""A real `omnivia-core-mcp` stdio server, driven by a stand-in transport.

Run as a subprocess by `test_mcp_stdio_end_to_end.py` so a real MCP client can
speak the real protocol to the real server over real pipes. Everything in the
path under test is production code: `build_server`, the exposure manifest, the
request builder and the SDK's stdio transport.

**`StandInTransport` stands in for the pending client transport, and for nothing
else.** `omnivia-core-client` exports the `ClientTransport` protocol but no
concrete local transport, and this package may not import the CLI's -- so a
double is the only way to exercise the call path at all. It satisfies
`ClientTransport` exactly, so the day `LocalIpcTransport` lands in the client
package it is deleted and the real one is constructed in its place. It is not a
second implementation of anything: it does not dial, frame, or connect.

`--contaminate` makes the server write to `sys.stdout` from inside a handler, so
the test can prove that stray output cannot reach the protocol stream.
"""

from __future__ import annotations

import sys
from typing import Any

import anyio
from omnivia_core_client import CancellationToken, ClientTransport, Deadline
from omnivia_core_mcp import server
from omnivia_core_mcp.managed_start import Attachment

from omnivia_core.contracts.v1 import (
    CapabilityRef,
    CapabilitySet,
    CompatibilityMetadata,
    GrantedAuthority,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    ServiceProbeRequest,
    ServiceProbeResult,
    SuccessResponseEnvelope,
    VersionCapabilityEnvelope,
    VersionWindow,
)

#: What the stand-in answers `workspace.inspect` with. Shaped like a workspace
#: descriptor so the test can assert the result travels intact, not so it is a
#: fixture of anything.
INSPECT_RESULT: dict[str, Any] = {
    "workspace_id": "ws-probe",
    "display_name": "Probe workspace",
    "status": "ready",
}

ATTACHMENT = Attachment(
    status="attached",
    endpoint_uri="unix:///tmp/omnivia-probe/run/s.sock",
    workspace_id="ws-probe",
)


def _version_envelope() -> VersionCapabilityEnvelope:
    """The version block every response carries. Constant, and not under test."""
    capability = CapabilityRef(id="workspace.read", version="1.0")
    window = VersionWindow(minimum="1.0", maximum="1.4")
    return VersionCapabilityEnvelope(
        api_version="1.4",
        server_version="0.1.0",
        workspace_format_version="1.0",
        compatibility=CompatibilityMetadata(
            selected_api_version="1.4",
            selected_workspace_version="1.0",
            supported_api_versions=window,
            supported_workspace_versions=window,
            status="ok",
            upgrade_state="current",
            deprecations=(),
        ),
        capabilities=CapabilitySet(
            supported=(capability,), granted=(capability,), effective=(capability,)
        ),
    )


class StandInTransport:
    """A `ClientTransport` that answers from memory instead of from a socket.

    Stands in for the concrete local transport `omnivia-core-client` does not yet
    export. It carries no framing, no connection and no dial loop -- it exists so
    the server's call path can be executed end to end while that decision is
    pending.
    """

    def __init__(self, endpoint_uri: str) -> None:
        self.endpoint_uri = endpoint_uri

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        return SuccessResponseEnvelope(
            metadata=ResponseMetadata(
                request_id=request.metadata.request_id,
                correlation_id=request.metadata.correlation_id,
                version=_version_envelope(),
                authority=GrantedAuthority(
                    principal_id="local-owner",
                    roles=("owner",),
                    capabilities=(CapabilityRef(id="workspace.read", version="1.0"),),
                ),
                page=None,
                job=None,
                freshness=None,
                canonical_resolution_time=None,
                warnings=None,
                omissions=None,
                partial=None,
                audit_reference=None,
            ),
            # Echo the operation back so the test can prove the *allow-listed*
            # operation is what reached the transport, not the tool name.
            result={**INSPECT_RESULT, "echoed_operation": request.operation},
        )

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:  # pragma: no cover - the server makes no probes
        raise NotImplementedError("the MCP server makes no runtime probes")


def _factory(contaminate: bool) -> server.TransportFactory:
    def build(endpoint_uri: str) -> ClientTransport:
        if contaminate:
            # Deliberate protocol vandalism, from inside a handler on the hot
            # path. The SDK's stdio transport has pointed fd 1 at stderr for the
            # duration, so this misses the wire instead of tearing a frame.
            sys.stdout.write("CONTAMINATION-FROM-A-HANDLER\n")
            sys.stdout.flush()
            print("CONTAMINATION-VIA-PRINT")
        return StandInTransport(endpoint_uri)

    return build


def main() -> None:
    contaminate = "--contaminate" in sys.argv
    if contaminate:
        # And once before the transport is claimed, which the SDK cannot help
        # with -- proving the server itself writes nothing to stdout at startup
        # is the other half of the same claim.
        sys.stderr.write("probe: starting with deliberate stdout contamination\n")
    anyio.run(
        lambda: server.serve(
            attachment=ATTACHMENT, transport_factory=_factory(contaminate)
        )
    )


if __name__ == "__main__":
    main()
