"""C6: generation runs after `chat.command` commits, off the serialized transport.

Through the real `main()` serve path: an initialised managed-local workspace, the
production surface, and every call over the one serialized local socket. A held fake
provider stands in for the route `provider_route_from_env` would resolve; nothing reaches
a real provider or the network.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import FakeProcessEvidence, ProcessEvidence
from omnivia_core_runtime.service import main as service_main
from omnivia_core_runtime.service import runner as service_runner
from omnivia_core_runtime.service.application import CHAT_FAMILY_PURPOSES
from omnivia_core_runtime.service.chat_generation_executor import (
    GenerationExecutorConfig,
)
from omnivia_core_runtime.service.installation_bootstrap import (
    initialise_and_register_managed_local_workspace,
)
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.transport import (
    LocalEndpoint,
    LocalSocketTransport,
    endpoint_for_path,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.workspace.layout import DATABASE_NAME

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION, ProviderInvocationRequest
from omnivia_core.contracts.v1 import SuccessResponseEnvelope, get_operation_metadata

_CONFIG = GenerationExecutorConfig(
    connection_id="provider-connection-1",
    model_id="provider-model-1",
    policy_ref="policy-1",
    classification_ref="classification-1",
    residency_ref="residency-1",
    service_actor_id="core.chat.generation",
)
_WAIT_SECONDS = 30
#: Taken before any test patches it, so a second `_serve` in one test wraps the real loop.
_SERVE_UNTIL_STOPPED = service_main._serve_until_stopped


class HeldProvider:
    """A fake F2a stream that holds one invocation (the first by default) open until released."""

    def __init__(self, *, hold_first: bool, hold_number: int = 1) -> None:
        self.hold_first = hold_first
        self.hold_number = hold_number
        self.held = threading.Event()
        self.release = threading.Event()
        self.requests: list[ProviderInvocationRequest] = []

    def __call__(self, request: ProviderInvocationRequest) -> Iterator[Mapping[str, Any]]:
        self.requests.append(request)
        number = len(self.requests)
        common = {
            "invocationId": request.invocation_id,
            "attemptId": f"{request.invocation_id}.transport-1",
            "schemaVersion": 1,
            "occurredAt": "2052-05-23T00:00:01Z",
            "receivedAt": "2052-05-23T00:00:01Z",
        }
        yield {**common, "ordinal": 0, "eventType": "stream-start"}
        if self.hold_first and number == self.hold_number:
            self.held.set()
            assert self.release.wait(_WAIT_SECONDS), "the test never released the stream"
        yield {
            **common,
            "ordinal": 1,
            "eventType": "text-delta",
            "partId": "provider-part-1",
            "stepId": "provider-step-1",
            "delta": f"reply {number}",
        }
        yield {**common, "ordinal": 2, "eventType": "finish", "finishReason": "stop"}


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root / "workspace"
        self.installation = root / "installation-state"
        result = initialise_and_register_managed_local_workspace(
            workspace_root=self.root, installation_root=self.installation
        )
        assert result.workspace_id is not None, result.reason
        self.workspace_id = result.workspace_id
        # AF_UNIX paths are capped well below pytest's tmp_path length.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="ovs-", dir="/tmp"))
        self.endpoint: LocalEndpoint = endpoint_for_path(self.socket_dir / "s.sock")

    def call(self, operation: str, operation_input: Mapping[str, Any], tag: str) -> Any:
        entry = get_operation_metadata(operation)
        fields: dict[str, Any] = {
            "request_id": f"req-{tag}",
            "correlation_id": f"cor-{tag}",
            "trace_id": f"trc-{tag}",
            "purpose": CHAT_FAMILY_PURPOSES[operation],
            "workspace_id": self.workspace_id,
        }
        if entry.idempotency.supports_idempotency_key:
            fields["idempotency_key"] = tag  # a Direction's must be its `commandId`
        response = LocalSocketTransport(endpoint=self.endpoint).call(
            s0.envelope_for(entry, operation_input=operation_input, **fields)
        )
        assert isinstance(response, SuccessResponseEnvelope), response
        return response.result

    def command(self, name: str, command: Mapping[str, Any], **expected: Any) -> Any:
        operation_input: dict[str, Any] = {"command_name": name, "command": dict(command)}
        if expected:
            operation_input["expected_conversation"] = expected
        return self.call("chat.command", operation_input, str(command["commandId"]))

    def create(self, tag: str) -> str:
        self.command("CreateConversation", self._base(f"{tag}-create") | {
            "requestedAt": "2052-05-22T14:13:20Z"
        })
        return f"cmd-{tag}-create.conv"

    def send(self, conversation_id: str, tag: str) -> str:
        """The first `SubmitMessage` on a fresh conversation; returns its job id."""
        command_id = f"cmd-{tag}-send"
        self.command(
            "SubmitMessage",
            self._base(f"{tag}-send")
            | {
                "conversationId": conversation_id,
                "editableParts": [{"type": "text", "payload": {"text": "hello"}}],
                "attachmentReferences": [],
                "contextReferences": [],
            },
            conversation_id=conversation_id,
            graph_revision=1,
            latest_conversation_sequence=0,
        )
        return f"{command_id}.gen"

    def snapshot(self, conversation_id: str, tag: str) -> Mapping[str, Any]:
        query = {
            "requestId": f"req-{tag}",
            "workspaceId": self.workspace_id,
            "conversationId": conversation_id,
            "actorId": LOCAL_PRINCIPAL,
        }
        result = self.call(
            "chat.snapshot",
            {"conversation_id": conversation_id, "snapshot_query": query},
            tag,
        )
        return result["snapshot"]  # type: ignore[no-any-return]

    def job(self, conversation_id: str, job_id: str, tag: str) -> Mapping[str, Any]:
        jobs = self.snapshot(conversation_id, tag)["generationJobs"]
        return next(job for job in jobs if job["jobId"] == job_id)

    def _base(self, tag: str) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "commandId": f"cmd-{tag}",
            "workspaceId": self.workspace_id,
            "actorId": LOCAL_PRINCIPAL,
        }

    def read(self) -> sqlite3.Connection:
        """The durable rows, read once no service holds the workspace."""
        path = (self.root / DATABASE_NAME).resolve()
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workspace]:
    # `main()` installs stop handlers for a console process; pytest keeps its own.
    monkeypatch.setattr(service_main.signal, "signal", lambda *_args: None)
    holder = Workspace(tmp_path)
    try:
        yield holder
    finally:
        shutil.rmtree(holder.socket_dir, ignore_errors=True)


def _serve(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    provider: HeldProvider,
    scenario: Callable[[], None],
) -> None:
    """Run `main()` until `scenario` (on this thread, as a client) returns, then stop it."""
    monkeypatch.setattr(
        service_runner,
        "SystemProcessEvidence",
        lambda: FakeProcessEvidence(
            ProcessEvidence(
                pid=os.getpid(),
                start_time="test-process-start",
                boot_id="test-boot",
                os_principal="test-user",
            )
        ),
    )
    monkeypatch.setattr(service_main, "provider_route_from_env", lambda _env: (provider, _CONFIG))

    def serve_scenario(
        runner: ServiceRunner, stopping: threading.Event, **kwargs: Any
    ) -> int:
        try:
            scenario()
        finally:
            stopping.set()
            code = _SERVE_UNTIL_STOPPED(runner, stopping, **kwargs)
        return code

    monkeypatch.setattr(service_main, "_serve_until_stopped", serve_scenario)
    argv = [
        "--workspace", str(workspace.root),
        "--installation-state", str(workspace.installation),
        "--endpoint", workspace.endpoint.url,
    ]  # fmt: skip
    assert service_main.main(argv) == 0


def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the generation"
        time.sleep(0.02)


def _worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "omnivia-chat-generation"]


def _event_types(connection: sqlite3.Connection, workspace_id: str, job_id: str) -> list[str]:
    return [
        event.event_type
        for event in chat.read_generation_events(
            connection, workspace_id=workspace_id, generation_job_id=job_id
        )
    ]


def test_a_held_stream_leaves_the_service_answering_and_then_settles(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = HeldProvider(hold_first=True)
    outcome: dict[str, Any] = {}

    def scenario() -> None:
        conversation = workspace.create("held")
        job_id = workspace.send(conversation, "held")
        # SubmitMessage answered; the provider is now held open on the worker.
        assert provider.held.wait(_WAIT_SECONDS)
        assert not provider.release.is_set()

        # Another chat operation is answered on the same serialized transport while the
        # stream stays held: the snapshot, then a Direction aimed at the running attempt.
        running = workspace.job(conversation, job_id, "held-snap-1")
        assert running["state"] == "running"
        steer = workspace.command(
            "SubmitSteeringInput",
            workspace._base("held-steer")
            | {
                "conversationId": conversation,
                "targetGenerationJobId": job_id,
                "expectedGenerationVersion": running["version"],
                "expectedActiveState": "running",
                "requestedBoundary": "earliest_safe",
                "editableParts": [{"type": "text", "payload": {"text": "shorter"}}],
                "attachmentReferences": [],
                "contextReferences": [],
            },
        )
        direction_id = steer["command_result"]["resultRef"].removeprefix("direction:")
        assert workspace.snapshot(conversation, "held-snap-2")["directions"][0][
            "directionId"
        ] == direction_id
        assert not provider.release.is_set() and len(provider.requests) == 1

        provider.release.set()
        _until(
            lambda: workspace.job(conversation, job_id, "held-poll")["state"] == "succeeded"
        )
        outcome.update(conversation=conversation, job_id=job_id, direction_id=direction_id)

    _serve(workspace, monkeypatch, provider, scenario)

    # The Direction was taken in at the boundary: one continuation invocation, caused by it.
    assert len(provider.requests) == 2
    assert provider.requests[1].causation_id == outcome["direction_id"]
    connection = workspace.read()
    try:
        job = chat.read_generation_job(
            connection,
            workspace_id=workspace.workspace_id,
            generation_job_id=outcome["job_id"],
        )
        assert job is not None and job.state == "succeeded"
        assert job.result_message_id is not None
        parts = chat.read_message_parts(
            connection, workspace_id=workspace.workspace_id, message_id=job.result_message_id
        )
        assert [dict(part.payload) for part in parts] == [{"text": "reply 1reply 2"}]
        direction = chat.read_direction(
            connection,
            workspace_id=workspace.workspace_id,
            direction_id=outcome["direction_id"],
        )
        assert direction is not None and direction.state == "applied"
    finally:
        connection.close()


def test_a_restart_picks_up_committed_work_and_never_reinvokes_a_held_stream(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = HeldProvider(hold_first=True)
    jobs: dict[str, str] = {}

    def before_restart() -> None:
        held = workspace.create("held")
        waiting = workspace.create("waiting")
        jobs["held"] = workspace.send(held, "held")
        assert first.held.wait(_WAIT_SECONDS)
        # Committed and answered while the worker is still held on the first stream.
        jobs["waiting"] = workspace.send(waiting, "waiting")
        assert workspace.job(waiting, jobs["waiting"], "waiting-snap")["state"] == "queued"

    # The service stops with the stream still held, and stops cleanly.
    _serve(workspace, monkeypatch, first, before_restart)
    assert len(first.requests) == 1
    # Released after shutdown, the abandoned stream writes nothing and its thread ends.
    first.release.set()
    for thread in _worker_threads():
        thread.join(_WAIT_SECONDS)
    assert _worker_threads() == []

    connection = workspace.read()
    try:
        assert _event_types(connection, workspace.workspace_id, jobs["held"]) == [
            "chat.generation.queued",
            "chat.generation.started",
        ]
        # Committed, never claimed: the claim is what writes a job's first event.
        waiting = chat.read_generation_job(
            connection, workspace_id=workspace.workspace_id, generation_job_id=jobs["waiting"]
        )
        assert waiting is not None and waiting.state == "queued"
        assert _event_types(connection, workspace.workspace_id, jobs["waiting"]) == []
    finally:
        connection.close()

    second = HeldProvider(hold_first=False)

    def after_restart() -> None:
        _until(
            lambda: workspace.job(
                "cmd-waiting-create.conv", jobs["waiting"], "waiting-poll"
            )["state"]
            == "succeeded"
        )

    _serve(workspace, monkeypatch, second, after_restart)

    # Only the work that never reached a provider was picked up; the interrupted stream
    # was not invoked again.
    assert [request.job_id for request in second.requests] == [jobs["waiting"]]
    connection = workspace.read()
    try:
        assert _event_types(connection, workspace.workspace_id, jobs["held"]) == [
            "chat.generation.queued",
            "chat.generation.started",
        ]
        waiting = chat.read_generation_job(
            connection, workspace_id=workspace.workspace_id, generation_job_id=jobs["waiting"]
        )
        assert waiting is not None and waiting.state == "succeeded"
    finally:
        connection.close()


def _select_branch(workspace: Workspace, conversation_id: str, branch_id: str) -> None:
    """Point the snapshot at a fork's branch, which is where its job is published."""
    workspace.command(
        "SelectConversationPath",
        workspace._base(f"select-{branch_id}")
        | {"conversationId": conversation_id, "branchId": branch_id},
    )


def _regenerate(workspace: Workspace, conversation_id: str, job_id: str, tag: str) -> str:
    """Fork the reply `job_id` produced; returns the new generation job's id."""
    source = workspace.job(conversation_id, job_id, f"{tag}-source")["resultMessageId"]
    workspace.command(
        "RegenerateResponse",
        workspace._base(tag)
        | {"conversationId": conversation_id, "sourceMessageId": source, "newBranchId": f"br-{tag}"},
        conversation_id=conversation_id,
        graph_revision=1,
        latest_conversation_sequence=2,
    )
    return f"cmd-{tag}.gen"


def _amend(workspace: Workspace, conversation_id: str, send_tag: str, tag: str) -> str:
    """Amend the user message `send_tag` sent; returns the new generation job's id."""
    workspace.command(
        "CommitMessageAmendment",
        workspace._base(tag)
        | {
            "conversationId": conversation_id,
            "sourceMessageId": f"cmd-{send_tag}-send.msg",
            "newMessageId": f"msg-{tag}",
            "newBranchId": f"br-{tag}",
            "editableParts": [{"type": "text", "payload": {"text": "hello again"}}],
            "attachmentReferences": [],
            "contextReferences": [],
            "requestedAt": "2052-05-23T00:00:00Z",
        },
        conversation_id=conversation_id,
        graph_revision=1,
        latest_conversation_sequence=2,
    )
    return f"cmd-{tag}.gen"


def test_a_regeneration_runs_live_on_the_worker_and_settles_on_its_own_branch(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = HeldProvider(hold_first=False)
    outcome: dict[str, Any] = {}

    def scenario() -> None:
        conversation = workspace.create("fork")
        original = workspace.send(conversation, "fork")
        _until(lambda: workspace.job(conversation, original, "fork-poll-1")["state"] == "succeeded")
        fork = _regenerate(workspace, conversation, original, "fork-regen")
        _select_branch(workspace, conversation, "br-fork-regen")
        _until(lambda: workspace.job(conversation, fork, "fork-poll-2")["state"] == "succeeded")
        outcome.update(fork=fork)

    _serve(workspace, monkeypatch, provider, scenario)

    assert [request.job_id for request in provider.requests][1:] == [outcome["fork"]]
    connection = workspace.read()
    try:
        job = chat.read_generation_job(
            connection, workspace_id=workspace.workspace_id, generation_job_id=outcome["fork"]
        )
        assert job is not None and job.branch_id == "br-fork-regen"
        assert job.result_message_id is not None
        assert _event_types(connection, workspace.workspace_id, outcome["fork"])[:2] == [
            "chat.generation.queued",
            "chat.generation.started",
        ]
        reply = chat.read_message(
            connection,
            workspace_id=workspace.workspace_id,
            conversation_id=job.conversation_id,
            message_id=job.result_message_id,
        )
        assert reply is not None and reply.created_on_branch_id == "br-fork-regen"
    finally:
        connection.close()


def test_a_restart_drains_a_committed_fork_and_never_steals_a_running_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second invocation is held, so the fork below is committed while the worker is busy.
    first = HeldProvider(hold_first=True, hold_number=2)
    jobs: dict[str, str] = {}

    def before_restart() -> None:
        done = workspace.create("done")
        busy = workspace.create("busy")
        jobs["done"] = workspace.send(done, "done")
        _until(lambda: workspace.job(done, jobs["done"], "done-poll")["state"] == "succeeded")
        jobs["busy"] = workspace.send(busy, "busy")
        assert first.held.wait(_WAIT_SECONDS)
        jobs["fork"] = _amend(workspace, done, "done", "fork-amend")
        _select_branch(workspace, done, "br-fork-amend")
        assert workspace.job(done, jobs["fork"], "fork-snap")["state"] == "queued"

    _serve(workspace, monkeypatch, first, before_restart)
    first.release.set()
    for thread in _worker_threads():
        thread.join(_WAIT_SECONDS)
    assert _worker_threads() == []

    connection = workspace.read()
    try:
        # Committed and answered, never claimed: the claim is what writes a job's first event.
        assert _event_types(connection, workspace.workspace_id, jobs["fork"]) == []
        assert _event_types(connection, workspace.workspace_id, jobs["busy"]) == [
            "chat.generation.queued",
            "chat.generation.started",
        ]
    finally:
        connection.close()

    second = HeldProvider(hold_first=False)

    def after_restart() -> None:
        _until(
            lambda: workspace.job("cmd-done-create.conv", jobs["fork"], "fork-poll")["state"]
            == "succeeded"
        )

    _serve(workspace, monkeypatch, second, after_restart)

    # Only the fork was picked up; the job left running mid-stream was not invoked again.
    assert [request.job_id for request in second.requests] == [jobs["fork"]]
    connection = workspace.read()
    try:
        busy = chat.read_generation_job(
            connection, workspace_id=workspace.workspace_id, generation_job_id=jobs["busy"]
        )
        assert busy is not None and busy.state == "running"
        assert _event_types(connection, workspace.workspace_id, jobs["busy"]) == [
            "chat.generation.queued",
            "chat.generation.started",
        ]
    finally:
        connection.close()
