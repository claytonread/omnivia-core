"""A test-only stdio relay that withholds exactly one tool response.

R004 section 13.F asks for an interrupted response: Core commits, and the answer
never reaches the MCP client. Nothing in the product may simulate that -- there
is no drop switch on the adapter and there must not be one -- so the loss is
staged *outside* the server, in the pipe a host owns, which is also where a real
one happens.

**The server under test is the production entry point, unmodified.** This
process spawns `python -m omnivia_core_mcp.server --config <path>` as its own
child and copies bytes in both directions. The child is not told it is being
relayed, is given no flag, and has no code path of its own here: what is under
test is the same module a host launches, with an extra pair of pipes in front of
it.

**What it does, and the whole of it.** Requests are forwarded verbatim. The
first `tools/call` naming `--withhold` has its JSON-RPC id remembered; when the
child answers that id, the answer is dropped, the child is killed, and this
process exits -- so the client sees a closed stream after its call was
dispatched and answered, which is the ambiguous outcome the section is about.
Every other message, in both directions, is passed through untouched.

**Nothing is read out of what it drops.** The withheld message is never read past
its `id`, never logged and never written anywhere. `--marker` receives the tool
name and nothing else, so the test can tell "the interruption happened" from "the
call never got that far" without this file holding a byte of the answer.

**Two additions serve the real-host qualification lane, and neither changes the
default behaviour.** `--server-executable` replaces the spawned child with the
installed `omnivia-core-mcp` console script, so the qualification run relays the
same binary a host would launch rather than this interpreter's module path;
omitted, the child is `python -m omnivia_core_mcp.server` exactly as before.
`--tools-observed` writes the sorted tool names out of the first `tools/list`
result the client is given -- names only, never a schema, description or
annotation -- which is how a host that publishes no tool-inventory event of its
own can still have its inventory observed at its own transport boundary. Both
are optional, both leave stdout protocol-only, and `--withhold` is optional too
so the observing mode need not interrupt anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Any

#: The production server module a host launches. Spawned as a child rather than
#: imported, so the code under test runs in its own process exactly as it does
#: under a real host.
SERVER_MODULE = "omnivia_core_mcp.server"

#: How long to wait for the killed child to be reaped. Only reached on the
#: interrupted path, where the child is already being killed.
_REAP_TIMEOUT_SECONDS = 30.0


def _call_id(line: bytes, tool: str) -> Any | None:
    """The JSON-RPC id of `line`, when it is a `tools/call` naming `tool`.

    `None` for everything else, including a line that is not JSON at all: this
    relay forwards whatever it is given and only has to recognise one message.
    """
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    parameters = message.get("params")
    if not isinstance(parameters, dict) or parameters.get("name") != tool:
        return None
    return message.get("id")


def _answers(line: bytes, request_id: Any) -> bool:
    """Whether `line` is the reply to `request_id`. Nothing else in it is read."""
    try:
        message = json.loads(line)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("id") == request_id


def _listed_tools(line: bytes) -> list[str] | None:
    """The tool names in `line` when it is a `tools/list` result, else `None`.

    Only `result.tools[].name` is read. A schema, description or annotation is
    forwarded like every other byte and never looked at here, so the observed
    inventory can be written into evidence without carrying anything else with
    it.
    """
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    result = message.get("result")
    if not isinstance(result, dict):
        return None
    listed = result.get("tools")
    if not isinstance(listed, list):
        return None
    names = [tool.get("name") for tool in listed if isinstance(tool, dict)]
    if not all(isinstance(name, str) for name in names):
        return None
    return sorted(str(name) for name in names)


def _forward_requests(sink: IO[bytes], tool: str, armed: dict[str, Any]) -> None:
    """Client -> server, verbatim, noting the id of the call to be interrupted.

    A daemon thread, because it ends when the client stops writing and the main
    loop below is what decides when this process is finished.
    """
    source = sys.stdin.buffer
    while True:
        line = source.readline()
        if not line:
            break
        if armed["id"] is None:
            armed["id"] = _call_id(line, tool)
        sink.write(line)
        sink.flush()
    sink.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--withhold", default=None)
    parser.add_argument("--marker", default=None)
    parser.add_argument("--server-executable", default=None)
    parser.add_argument("--tools-observed", default=None)
    arguments = parser.parse_args()
    if (arguments.withhold is None) != (arguments.marker is None):
        parser.error("--withhold and --marker are given together or not at all")

    command = (
        [arguments.server_executable]
        if arguments.server_executable
        else [sys.executable, "-m", SERVER_MODULE]
    )
    child = subprocess.Popen(
        [*command, "--config", arguments.config],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert child.stdin is not None and child.stdout is not None, "no pipes to relay"

    armed: dict[str, Any] = {"id": None}
    threading.Thread(
        target=_forward_requests,
        args=(child.stdin, arguments.withhold or "", armed),
        daemon=True,
    ).start()

    sink = sys.stdout.buffer
    observed = False
    while True:
        line = child.stdout.readline()
        if not line:
            break
        if arguments.tools_observed and not observed:
            names = _listed_tools(line)
            if names is not None:
                Path(arguments.tools_observed).write_text(
                    json.dumps(names, sort_keys=True), encoding="utf-8"
                )
                observed = True
        if armed["id"] is not None and _answers(line, armed["id"]):
            # Recorded before the streams close, so the test that observes the
            # closed stream can already read why it closed.
            Path(arguments.marker).write_text(arguments.withhold, encoding="utf-8")
            break
        sink.write(line)
        sink.flush()

    child.kill()
    child.wait(timeout=_REAP_TIMEOUT_SECONDS)
    # `os._exit`, not a return: the forwarding thread is parked in a blocking
    # read on this process's stdin, which the client still holds open, and
    # interpreter finalization cannot take that stream's lock back from it --
    # CPython aborts with `_enter_buffered_busy` instead. Nothing is pending to
    # flush; every forwarded line was flushed as it was written.
    os._exit(0)


if __name__ == "__main__":
    main()
