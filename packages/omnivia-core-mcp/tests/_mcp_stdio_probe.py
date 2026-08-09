"""A real `omnivia-core-mcp` stdio server, dialling a real Core service.

Run as a subprocess by `test_mcp_stdio_end_to_end.py` so a real MCP client can
speak the real protocol to the real server over real pipes. Everything in the
path under test is production code: `build_server`, the exposure manifest, the
request builder, the SDK's stdio transport -- and, since owner resolution 005
R005-01, the transport itself.

**Nothing stands in for the transport any more.** Packet C shipped a
`StandInTransport` here because `omnivia-core-client` exported the
`ClientTransport` protocol but no concrete implementation, and this package may
not import the CLI's. R005-01 moved `LocalIpcTransport` into the client package,
so this probe passes no `transport_factory` at all: `build_server` falls through
to `server._default_transport_factory`, which constructs the real client-owned
transport and dials the endpoint named on the command line. A test that drives
this probe is therefore exercising the production dial path end to end, against a
service the test started.

`--contaminate` makes the server write to `sys.stdout` from inside a handler, so
the test can prove that stray output cannot reach the protocol stream. It wraps
the real default factory rather than replacing it -- the contamination is a side
effect on the way to the same transport every other run uses.
"""

from __future__ import annotations

import argparse
import sys

import anyio
from omnivia_core_client import ClientTransport
from omnivia_core_mcp import server
from omnivia_core_mcp.managed_start import Attachment


def _contaminating_factory(endpoint_uri: str) -> ClientTransport:
    """The real factory, with deliberate protocol vandalism on the way through.

    From inside a handler on the hot path. The SDK's stdio transport has pointed
    fd 1 at stderr for the duration, so this misses the wire instead of tearing a
    frame -- which is the claim the test using it is checking.
    """
    sys.stdout.write("CONTAMINATION-FROM-A-HANDLER\n")
    sys.stdout.flush()
    print("CONTAMINATION-VIA-PRINT")
    return server._default_transport_factory(endpoint_uri)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--contaminate", action="store_true")
    arguments = parser.parse_args()

    if arguments.contaminate:
        # And once before the transport is claimed, which the SDK cannot help
        # with -- proving the server itself writes nothing to stdout at startup
        # is the other half of the same claim.
        sys.stderr.write("probe: starting with deliberate stdout contamination\n")

    attachment = Attachment(
        status="attached",
        endpoint_uri=arguments.endpoint,
        workspace_id=arguments.workspace_id,
    )
    # `transport_factory` is left unset on the ordinary path on purpose: that is
    # what puts `server._default_transport_factory` -- the thing R005-01 changed
    # -- on the path under test.
    factory = _contaminating_factory if arguments.contaminate else None
    anyio.run(lambda: server.serve(attachment=attachment, transport_factory=factory))


if __name__ == "__main__":
    main()
