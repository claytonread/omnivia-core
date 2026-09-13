"""A real `omnivia-core-mcp` stdio server, started the way a host starts one.

Run as a subprocess by `test_mcp_stdio_end_to_end.py` so a real MCP client can
speak the real protocol to the real server over real pipes. Everything in the
path under test is production code: the trusted configuration reader,
`server.connect` -- which composes `omnivia-core-client`'s `ServiceClient` and
therefore the descriptor read, the transport choice, the version negotiation and
the liveness probe -- `build_server`, the exposure manifest, the request builder
and the SDK's stdio transport.

**Nothing stands in for anything on the ordinary path.** The probe is handed one
path -- the trusted configuration file the test wrote -- and everything else is
derived from that document by production code. One explicit fault mode wraps the
already-connected production transport: after a named mutation has received and
decoded Core's reply, it kills that Core process and raises instead of returning
the reply to MCP. That is the test-only cut needed to prove a commit whose MCP
response is lost; it does not answer or alter the request. Before V06-6 this
passed an endpoint and a workspace id on the command line and `build_server`
dialled them; neither is reachable from here now, because neither is something a
host may state outside the trusted document.

`--contaminate` makes the server write to `sys.stdout` from inside a live
handler, so the test can prove that stray output cannot reach the protocol
stream. It wraps `server._call_tool` rather than replacing it -- the
contamination is a side effect on the way to the same dispatch every other run
uses.

`--authoring` injects :func:`_admit_authoring`, and it is the one thing in this
file that stands in for something: the protected authoring-admission seam, which
production now supplies from the installed setup -- `server._installed_admission`
asks the service with the dedicated bearer, and
`test_mcp_standalone_authoring_acceptance` runs that whole path live. The stand-in
stays because it lets the wider surface be exercised against a configuration this
suite wrote itself, without an installed setup. It is a flag rather than a
configuration field on purpose -- the whole point of the seam is that nothing
readable from the public `omnivia.mcp-config.v1` document can raise the profile,
so a test that could enable authoring by editing that document would be testing
the opposite of the rule. Without the flag, the same configuration serves the
restricted six, which is what production does with it.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from omnivia_core_client import ServiceClient, TransportError
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import read_configuration

_DISPATCH = server._call_tool


@dataclass(frozen=True, slots=True)
class _DropAfterReplyTransport:
    """Drop one reply only after the real transport has decoded it completely."""

    transport: Any
    service_pid: int
    operation: str = "evidence.capture"

    def call(
        self,
        request: Any,
        *,
        deadline: Any,
        cancellation: Any = None,
    ) -> Any:
        response = self.transport.call(
            request, deadline=deadline, cancellation=cancellation
        )
        if request.operation == self.operation:
            os.kill(self.service_pid, signal.SIGKILL)
            raise TransportError(
                "the response was lost after the service completed the call"
            )
        return response

    def probe(
        self,
        request: Any,
        *,
        deadline: Any,
        cancellation: Any = None,
    ) -> Any:
        return self.transport.probe(
            request, deadline=deadline, cancellation=cancellation
        )


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


def _admit_authoring(
    client: ServiceClient, principal_id: str, workspace_id: str
) -> bool:
    """Stand in for the protected record the installed setup path writes.

    The real one -- `server._installed_admission` -- reads durable installation
    state a human owner or administrator authorised, through this already
    connected, already authenticated client, which is why the seam is handed it,
    and answers `False` the moment that authority is revoked. No such record
    exists for the configuration this suite writes for itself, so this is a test
    double and is reachable only through `--authoring` -- never from the
    configuration file, which is the invariant it exists to leave intact.

    What it does assert is the seam's own precondition: it is called with a
    connected client already agreed to serve this workspace.
    """
    return client.descriptor.workspace_id == workspace_id and bool(principal_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--contaminate", action="store_true")
    parser.add_argument("--authoring", action="store_true")
    parser.add_argument("--drop-after-reply-pid", type=int)
    arguments = parser.parse_args()

    if arguments.contaminate:
        # And once before the transport is claimed, which the SDK cannot help
        # with -- proving the server itself writes nothing to stdout at startup
        # is the other half of the same claim.
        sys.stderr.write("probe: starting with deliberate stdout contamination\n")
        server._call_tool = _contaminating_call_tool

    configuration = read_configuration(Path(arguments.config))
    admission = (
        server._installed_admission(configuration)
        if arguments.drop_after_reply_pid is not None
        else (_admit_authoring if arguments.authoring else None)
    )
    session = server.connect(configuration, authoring_admission=admission)
    if arguments.drop_after_reply_pid is not None:
        session = replace(
            session,
            client=replace(
                session.client,
                transport=_DropAfterReplyTransport(
                    session.client.transport, arguments.drop_after_reply_pid
                ),
            ),
        )
    anyio.run(lambda: server.serve(session=session))


if __name__ == "__main__":
    main()
