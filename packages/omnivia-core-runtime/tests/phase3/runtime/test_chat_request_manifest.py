"""T-0658 acceptance for reconstructable Chat request manifests.

The manifest is not the provider request body. It is the durable, redacted
account of the final post-policy request: ordered Message/Part identities and
hashes, exact route/model/policy references, request option digests and empty
tool definitions until governed tools exist. Text still crosses the provider
boundary in ``ProviderInvocationRequest``; it must not be copied into the
manifest.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.chat_generation import claim_queued_generation
from omnivia_core_runtime.service.chat_generation_executor import (
    ChatGenerationExecutor,
    GenerationExecutorConfig,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import (
    load_migrations,
    materialise_phase0_baseline,
)

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-chat-manifest-nope"
BASE_US = 2_700_000_000_000_000
WALL = datetime.fromtimestamp((BASE_US + 1_000_000) / 1_000_000, tz=UTC)

CONVERSATION_ID = "conv-manifest-01"
BRANCH_ID = "branch-manifest-main"
ACTOR_ID = "actor-manifest-human"
ROOT_MESSAGE_ID = "msg-manifest-root"
ROOT_PART_ID = "part-manifest-root-0"
TRIGGER_MESSAGE_ID = "msg-manifest-trigger"
TRIGGER_PART_ID = "part-manifest-trigger-0"
SIBLING_MESSAGE_ID = "msg-manifest-sibling"
SIBLING_PART_ID = "part-manifest-sibling-0"
QUEUE_ID = "queue-manifest-1"
JOB_ID = "generation-job-manifest-1"
ATTEMPT_ID = "generation-attempt-manifest-1"
LEASE_OWNER = "worker-manifest-1"

TABLE = "omnivia_chat_request_manifests"
INDEXES = {
    "omnivia_idx_chat_request_manifests_job",
    "omnivia_idx_chat_request_manifests_conversation",
}
TRIGGERS = {
    "omnivia_guard_chat_request_manifests_insert",
    "omnivia_guard_chat_request_manifests_update",
    "omnivia_guard_chat_request_manifests_delete",
}


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def writer(holder: m1.Owned):
    return chat.chat_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def digest(character: str) -> str:
    return "sha256:" + character * 64


def _message(
    w: chat.ChatWriter,
    *,
    message_id: str,
    part_id: str,
    sequence: int,
    parent: str | None,
    text: str,
    content: str,
    part_content: str,
) -> None:
    w.append_message(
        message_id=message_id,
        conversation_id=CONVERSATION_ID,
        role="user",
        author_type="human",
        author_id=ACTOR_ID,
        parent_message_id=parent,
        conversation_sequence=sequence,
        schema_version=1,
        content_hash=digest(content),
        completion_status="complete",
        visibility="standard",
        created_on_branch_id=BRANCH_ID if parent is not None else None,
        created_at_us=BASE_US + sequence,
        committed_at_us=BASE_US + sequence,
    )
    w.append_message_part(
        part_id=part_id,
        message_id=message_id,
        conversation_id=CONVERSATION_ID,
        part_index=0,
        part_type="text",
        schema_version=1,
        visibility="standard",
        payload={"text": text},
        provenance="human",
        content_hash=digest(part_content),
        created_at_us=BASE_US + 100 + sequence,
    )


def seed_conversation_and_queue(holder: m1.Owned) -> None:
    with writer(holder) as w:
        w.append_conversation(
            conversation_id=CONVERSATION_ID,
            state="active",
            graph_revision=1,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
        )
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=1,
            graph_revision=1,
            latest_conversation_sequence=3,
            state="active",
            updated_at_us=BASE_US + 1,
        )
        _message(
            w,
            message_id=ROOT_MESSAGE_ID,
            part_id=ROOT_PART_ID,
            sequence=1,
            parent=None,
            text="root text must not be copied",
            content="a",
            part_content="b",
        )
        w.append_branch(
            branch_id=BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="original",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 4,
            created_conversation_sequence=1,
            schema_version=1,
        )
        w.append_branch_head_event(
            event_id="head-event-manifest-1",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=1,
            previous_head_message_id=None,
            new_head_message_id=ROOT_MESSAGE_ID,
            cause="branch_created",
            command_id="command-manifest-seed",
            graph_revision=1,
            conversation_sequence=1,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 4,
            schema_version=1,
        )
        _message(
            w,
            message_id=TRIGGER_MESSAGE_ID,
            part_id=TRIGGER_PART_ID,
            sequence=2,
            parent=ROOT_MESSAGE_ID,
            text="trigger text must not be copied",
            content="c",
            part_content="d",
        )
        _message(
            w,
            message_id=SIBLING_MESSAGE_ID,
            part_id=SIBLING_PART_ID,
            sequence=3,
            parent=ROOT_MESSAGE_ID,
            text="sibling text must not leak",
            content="e",
            part_content="f",
        )
        w.append_queued_submission(
            queued_submission_id=QUEUE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            queue_sequence=1,
            branch_id=BRANCH_ID,
            editable_parts=[{"partType": "text"}],
            references=[],
            idempotency_key="manifest-idem-1",
            created_at_us=BASE_US + 10,
            updated_at_us=BASE_US + 10,
        )


def _config(**overrides: Any) -> GenerationExecutorConfig:
    return GenerationExecutorConfig(
        **{
            "connection_id": "provider-connection-manifest-1",
            "model_id": "provider-model-manifest-1",
            "policy_ref": "policy-manifest-1",
            "classification_ref": "classification-manifest-1",
            "residency_ref": "residency-manifest-1",
            "service_actor_id": "actor-core-chat",
            **overrides,
        }
    )


def _executor(
    holder: m1.Owned,
    provider: Any,
    *,
    clock: FakeClock | None = None,
    config: GenerationExecutorConfig | None = None,
) -> ChatGenerationExecutor:
    return ChatGenerationExecutor(
        connection=holder.connection,
        identity=holder.identity,
        fencing_generation=holder.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock or FakeClock(wall=WALL),
        invoke=provider,
        config=config or _config(),
    )


def _stream(request: Any) -> Iterator[Mapping[str, Any]]:
    common = {
        "invocationId": request.invocation_id,
        "attemptId": request.attempt_id,
        "schemaVersion": 1,
        "occurredAt": "2055-01-01T00:00:01Z",
        "receivedAt": "2055-01-01T00:00:01Z",
    }
    yield {**common, "ordinal": 0, "eventType": "stream-start"}
    yield {
        **common,
        "ordinal": 1,
        "eventType": "text-delta",
        "partId": "provider-part-manifest-1",
        "stepId": "provider-step-manifest-1",
        "delta": "manifest answer",
    }
    yield {**common, "ordinal": 2, "eventType": "finish", "finishReason": "stop"}


def _manifest_body() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "requestManifestId": "request-manifest-storage-1",
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "branchId": BRANCH_ID,
        "generationJobId": JOB_ID,
        "generationAttemptId": ATTEMPT_ID,
        "triggerMessageId": TRIGGER_MESSAGE_ID,
        "providerInvocationId": "provider-invocation-storage-1",
        "idempotencyKey": "manifest-idem-1",
        "modelVisible": {
            "messages": [],
            "renderedPromptContent": "not_stored",
            "contentPolicy": "identifiers_and_hashes_only",
        },
        "promptBundle": {
            "id": "core.chat.default-model-visible-context",
            "version": 1,
            "kind": "message-lineage",
            "digest": digest("9"),
        },
        "toolDefinitions": [],
        "route": {
            "connectionId": "provider-connection-manifest-1",
            "modelId": "provider-model-manifest-1",
            "operation": "language.stream",
        },
        "requestOptions": {
            "responseFormatDigest": digest("8"),
            "generationOptionsDigest": None,
            "providerOptionsByNamespaceDigest": None,
            "deadlineAt": "2055-01-01T00:02:00Z",
            "requestedAt": "2055-01-01T00:00:00Z",
            "causationId": TRIGGER_MESSAGE_ID,
            "correlationId": "correlation-storage-1",
        },
        "policy": {
            "policyRef": "policy-manifest-1",
            "classificationRef": "classification-manifest-1",
            "residencyRef": "residency-manifest-1",
        },
        "authorizedReferences": {"context": [], "attachments": [], "evidence": []},
        "retention": {
            "class": "chat_request_manifest_v1",
            "export": "metadata_and_hashes",
        },
    }


def seed_running_attempt(holder: m1.Owned) -> None:
    seed_conversation_and_queue(holder)
    claim_queued_generation(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        queued_submission_id=QUEUE_ID,
        generation_job_id=JOB_ID,
        generation_attempt_id=ATTEMPT_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
        lease_owner=LEASE_OWNER,
        now_us=BASE_US + 20,
    )


def test_0031_schema_inventory_contains_expected_objects(owned: m1.Owned) -> None:
    migration = [item for item in load_migrations() if item.version == 31]
    assert len(migration) == 1
    assert migration[0].name == "0031_chat_request_manifests.sql"

    objects = {
        (row[0], row[1])
        for row in owned.connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'omnivia_%'"
        )
    }

    assert (TABLE, "table") in objects
    assert {(index, "index") for index in INDEXES}.issubset(objects)
    assert {(trigger, "trigger") for trigger in TRIGGERS}.issubset(objects)
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_request_manifest_is_idempotent_and_append_only(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    body = _manifest_body()

    first = chat.append_request_manifest_once(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        conversation_id=CONVERSATION_ID,
        branch_id=BRANCH_ID,
        generation_job_id=JOB_ID,
        generation_attempt_id=ATTEMPT_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
        provider_invocation_id="provider-invocation-storage-1",
        request_manifest_id="request-manifest-storage-1",
        idempotency_key="manifest-idem-1",
        manifest_body=body,
        created_at_us=BASE_US + 30,
    )
    second = chat.append_request_manifest_once(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        conversation_id=CONVERSATION_ID,
        branch_id=BRANCH_ID,
        generation_job_id=JOB_ID,
        generation_attempt_id=ATTEMPT_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
        provider_invocation_id="provider-invocation-storage-1",
        request_manifest_id="request-manifest-storage-1",
        idempotency_key="manifest-idem-1",
        manifest_body=body,
        created_at_us=BASE_US + 31,
    )

    assert second == first
    assert first.manifest_digest.startswith("sha256:")
    assert first.manifest_body == body

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        w.connection.execute(
            "UPDATE omnivia_chat_request_manifests SET schema_version = 1 "
            "WHERE workspace_id = ? AND generation_attempt_id = ?",
            (WORKSPACE_ID, ATTEMPT_ID),
        )


def test_request_manifest_conflict_fails_closed(owned: m1.Owned) -> None:
    seed_running_attempt(owned)
    body = _manifest_body()
    chat.append_request_manifest_once(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        conversation_id=CONVERSATION_ID,
        branch_id=BRANCH_ID,
        generation_job_id=JOB_ID,
        generation_attempt_id=ATTEMPT_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
        provider_invocation_id="provider-invocation-storage-1",
        request_manifest_id="request-manifest-storage-1",
        idempotency_key="manifest-idem-1",
        manifest_body=body,
        created_at_us=BASE_US + 30,
    )

    changed = {**body, "policy": {**body["policy"], "policyRef": "policy-manifest-2"}}
    with pytest.raises(chat.RequestManifestConflict):
        chat.append_request_manifest_once(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            trigger_message_id=TRIGGER_MESSAGE_ID,
            provider_invocation_id="provider-invocation-storage-1",
            request_manifest_id="request-manifest-storage-1",
            idempotency_key="manifest-idem-1",
            manifest_body=changed,
            created_at_us=BASE_US + 31,
        )


def test_executor_persists_manifest_before_provider_invocation_and_uses_path_refs(
    owned: m1.Owned,
) -> None:
    seed_conversation_and_queue(owned)
    calls: list[Any] = []

    def provider(request: Any) -> Iterator[Mapping[str, Any]]:
        calls.append(request)
        manifest = chat.read_request_manifest(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=request.attempt_id,
        )
        assert manifest is not None
        body = manifest.manifest_body
        assert body["providerInvocationId"] == request.invocation_id
        assert body["generationJobId"] == request.job_id
        assert body["generationAttemptId"] == request.attempt_id
        assert body["triggerMessageId"] == TRIGGER_MESSAGE_ID
        assert body["toolDefinitions"] == []
        assert body["route"] == {
            "connectionId": "provider-connection-manifest-1",
            "modelId": "provider-model-manifest-1",
            "operation": "language.stream",
        }
        message_refs = body["modelVisible"]["messages"]
        assert [item["messageId"] for item in message_refs] == [
            ROOT_MESSAGE_ID,
            TRIGGER_MESSAGE_ID,
        ]
        assert SIBLING_MESSAGE_ID not in json.dumps(message_refs, sort_keys=True)
        yield from _stream(request)

    _executor(owned, provider).execute_submission(
        queued_submission_id=QUEUE_ID,
        generation_job_id=JOB_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
    )

    assert len(calls) == 1
    manifest = chat.read_request_manifest(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        generation_attempt_id=calls[0].attempt_id,
    )
    assert manifest is not None
    serialized = json.dumps(dict(manifest.manifest_body), sort_keys=True)
    assert "root text must not be copied" not in serialized
    assert "trigger text must not be copied" not in serialized
    assert "sibling text must not leak" not in serialized
    assert "endpoint" not in serialized.lower()
    assert "header" not in serialized.lower()
    assert "route-token-secret" not in serialized
    assert "hidden" not in serialized.lower()
    assert (
        chat.read_request_manifest(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            generation_attempt_id=calls[0].attempt_id,
        )
        is None
    )


def test_executor_does_not_fabricate_manifest_when_route_is_unconfigured(
    owned: m1.Owned,
) -> None:
    seed_conversation_and_queue(owned)
    calls = 0

    def provider(request: Any) -> Iterator[Mapping[str, Any]]:
        nonlocal calls
        calls += 1
        yield from _stream(request)

    _executor(owned, provider, config=_config(connection_id="")).execute_submission(
        queued_submission_id=QUEUE_ID,
        generation_job_id=JOB_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
    )

    job = chat.read_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.current_attempt_id is not None
    assert calls == 0
    assert (
        chat.read_request_manifest(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=job.current_attempt_id,
        )
        is None
    )


def test_manifest_survives_provider_failure_after_creation(owned: m1.Owned) -> None:
    seed_conversation_and_queue(owned)

    def provider(_request: Any) -> Iterator[Mapping[str, Any]]:
        raise RuntimeError("raw provider body and endpoint must not leak")
        yield  # pragma: no cover

    _executor(owned, provider).execute_submission(
        queued_submission_id=QUEUE_ID,
        generation_job_id=JOB_ID,
        trigger_message_id=TRIGGER_MESSAGE_ID,
    )

    job = chat.read_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.state == "failed"
    assert job.current_attempt_id is not None
    manifest = chat.read_request_manifest(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        generation_attempt_id=job.current_attempt_id,
    )
    assert manifest is not None
    assert "raw provider body" not in json.dumps(
        dict(manifest.manifest_body), sort_keys=True
    )
