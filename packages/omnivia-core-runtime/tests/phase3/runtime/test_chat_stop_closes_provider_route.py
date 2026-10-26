"""A StopGeneration must close the provider route connection the worker is blocked on.

The worker waits on the Platform bridge's NDJSON stream outside the gate. While the
provider is busy (a Codex turn mid-command) no event arrives, so a stop that is only a
durable row is never seen and the bridge -- and Codex behind it -- runs to completion.
The bridge cancels the provider when Core closes this connection; this proves Core does.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest
from omnivia_core_runtime.service.chat_provider_route import LocalProviderRouteClient
from test_chat_generation_worker import (
    _WAIT_SECONDS,
    Workspace,
    _serve,
    _until,
    workspace,  # noqa: F401 - the shared fixture
)

_STOP_TO_CLOSE_SECONDS = 5


class HeldBridge:
    """A loopback bridge that sends stream-start, then holds until Core closes its end."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.held = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(65_536)
            head, body = data.split(b"\r\n\r\n", 1)
            length = next(
                int(line.split(b":", 1)[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            while len(body) < length:
                body += connection.recv(65_536)
            invocation_id = json.loads(body)["invocationId"]
            start = {
                "invocationId": invocation_id,
                "attemptId": f"{invocation_id}.transport-1",
                "schemaVersion": 1,
                "occurredAt": "2052-05-23T00:00:01Z",
                "receivedAt": "2052-05-23T00:00:01Z",
                "ordinal": 0,
                "eventType": "stream-start",
            }
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                b"Connection: close\r\n\r\n" + json.dumps(start).encode() + b"\n"
            )
            self.held.set()
            connection.settimeout(_WAIT_SECONDS)
            try:
                if connection.recv(1) == b"":
                    self.closed.set()
            except OSError:
                self.closed.set()

    def close(self) -> None:
        self.server.close()
        self.thread.join(_WAIT_SECONDS + 1)


def test_stop_generation_closes_the_held_provider_route_connection(
    workspace: Workspace,  # noqa: F811 - pytest injects the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = HeldBridge()
    client = LocalProviderRouteClient(
        host="127.0.0.1",
        port=bridge.port,
        path="/f2a/provider-stream",
        token="route-token",
        timeout_seconds=_WAIT_SECONDS,
    )
    outcome: dict[str, Any] = {}

    def scenario() -> None:
        conversation = workspace.create("stop-route")
        job_id = workspace.send(conversation, "stop-route")
        assert bridge.held.wait(_WAIT_SECONDS)
        _until(lambda: workspace.job(conversation, job_id, "stop-route-poll")["state"] == "running")

        workspace.command(
            "StopGeneration",
            workspace._base("stop-route-stop") | {"conversationId": conversation, "jobId": job_id},
        )
        stopped_at = time.monotonic()
        outcome["closed"] = bridge.closed.wait(_STOP_TO_CLOSE_SECONDS)
        outcome["seconds"] = time.monotonic() - stopped_at
        outcome["state"] = workspace.job(conversation, job_id, "stop-route-after")["state"]

    try:
        _serve(workspace, monkeypatch, client, scenario)  # type: ignore[arg-type]
    finally:
        bridge.close()

    assert outcome["state"] == "cancelled"
    assert outcome["closed"], (
        f"the provider route connection was still open {outcome['seconds']:.1f}s after Stop"
    )
