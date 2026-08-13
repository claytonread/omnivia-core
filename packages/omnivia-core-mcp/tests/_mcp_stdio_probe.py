"""A real `omnivia-core-mcp` stdio server, started the way a host starts one.

Run as a subprocess by `test_mcp_stdio_end_to_end.py` so a real MCP client can
speak the real protocol to the real server over real pipes. Everything in the
path under test is production code: the trusted configuration reader,
`server.connect` -- which composes `omnivia-core-client`'s `ServiceClient` and
therefore the descriptor read, the transport choice, the version negotiation and
the liveness probe -- `build_server`, the exposure manifest, the request builder
and the SDK's stdio transport.

**Nothing stands in for anything.** The probe declares no class: no transport,
no operation double, no session of its own. It is handed one path -- the trusted
configuration file the test wrote -- and everything else is derived from that
document by production code, which is the point. Before V06-6 this passed an
endpoint and a workspace id on the command line and `build_server` dialled them;
neither is reachable from here now, because neither is something a host may
state outside the trusted document.

`--contaminate` makes the server write to `sys.stdout` from inside a live
handler, so the test can prove that stray output cannot reach the protocol
stream. It wraps `server._call_tool` rather than replacing it -- the
contamination is a side effect on the way to the same dispatch every other run
uses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio
import mcp_types as types
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import read_configuration

_DISPATCH = server._call_tool


def _contaminating_call_tool(
    params: types.CallToolRequestParams, *, session: server.ConnectedSession
) -> types.CallToolResult:
    """The real dispatch, with deliberate protocol vandalism on the way through.

    From inside a handler on the hot path. The SDK's stdio transport has pointed
    fd 1 at stderr for the duration, so this misses the wire instead of tearing a
    frame -- which is the claim the test using it is checking.
    """
    sys.stdout.write("CONTAMINATION-FROM-A-HANDLER\n")
    sys.stdout.flush()
    print("CONTAMINATION-VIA-PRINT")
    return _DISPATCH(params, session=session)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--contaminate", action="store_true")
    arguments = parser.parse_args()

    if arguments.contaminate:
        # And once before the transport is claimed, which the SDK cannot help
        # with -- proving the server itself writes nothing to stdout at startup
        # is the other half of the same claim.
        sys.stderr.write("probe: starting with deliberate stdout contamination\n")
        server._call_tool = _contaminating_call_tool

    session = server.connect(read_configuration(Path(arguments.config)))
    anyio.run(lambda: server.serve(session=session))


if __name__ == "__main__":
    main()
