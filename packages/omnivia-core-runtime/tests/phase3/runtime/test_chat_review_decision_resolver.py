"""C8 step 2: `ReviewDecision` and `SubmitHumanInput` behind the `chat.command` seam.

The tool-review lifecycle has been complete and unreachable: a nine-state
machine with append-only ledgers, DB triggers and 32 passing tests, that nothing
could call over the wire. No resolver, so `chat.command` answered
`dependency_unavailable` for both names.

These tests cover the INTEGRATION and deliberately not the library, which has
its own 32. What is new and can break:

* the resolver reaches the library at all;
* it LENDS its writer rather than letting the library open a second fenced
  transaction inside the seam's -- a nested transaction, which is the defect
  `_fenced` was repaired for earlier on 10 October. A success here is that
  proof: the nesting would fail the dispatch;
* the library's `ChatWaitConflict` becomes the seam's `conflict`, not an
  unexpected error;
* the idempotency key is coupled to `commandId`, as every C10 command is.

Composes the two harnesses: the C8 manifest fixture, which seeds a running
generation attempt a wait can hang off, and the C10 resolver harness's
dispatcher. Both resolve `WORKSPACE_ID` to `m1.WORKSPACE_ID`, so there is one
workspace and no fixture drift between them.
"""

# ruff: noqa: F811

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_interactions import read_chat_wait
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    open_chat_turn,
    record_tool_proposal,
)
from omnivia_core_runtime.service.chat_tool_review import (
    begin_policy_check,
    open_human_input_request,
    request_tool_review,
)
from test_chat_submit_message_resolver import (
    INSTALLATION_ID,
    PRINCIPAL,
    WORKSPACE_ID,
    _fallback,
    _request,
)

from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import build_chat_application_dispatcher

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION
from omnivia_core.contracts.v1 import ErrorResponseEnvelope, SuccessResponseEnvelope

T = manifest.BASE_US
CALL_ID = "toolcall-c8-resolver"
TURN_ID = "turn-c8-resolver"
WAIT_ID = "wait-c8-resolver"
ARGS = {"path": "notes.md", "text": "a line"}
RESOURCE = "resource-c8-resolver"
REVIEW_COMMAND_ID = "cmd-c8-review"
HUMAN_COMMAND_ID = "cmd-c8-human"


@pytest.fixture
def holder(tmp_path: Path) -> Any:
    """The C8 suite's own fixture, verbatim, so a drift between them is impossible."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    owned = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(owned)
    yield owned
    owned.connection.close()


#: The C10 resolver harness's `_dispatcher` hardcodes a clock on ITS base
#: (2_600_000_000_000_000) while the C8 manifest fixture lives on its own
#: (2_700_000_000_000_000) -- a hundred billion microseconds later. Dispatching
#: through that clock would settle a decision BEFORE the wait it decides was
#: created, and 0071's `updated_at_us >= created_at_us` CHECK refuses it. So
#: this suite builds its own dispatcher on the C8 base rather than editing a
#: helper six other suites share.
_WALL = datetime.fromtimestamp((T + 1_000_000) / 1_000_000, tz=UTC)

#: Waits must OUTLIVE `_WALL`, or the decision settles after they expire and the
#: library refuses it as `chat wait has expired` -- which is correct behaviour
#: and a useless test. Ten minutes past the base, against a settlement one
#: second past it.
_EXPIRES_AT = T + 600_000_000


def _c8_dispatcher(owned: Any) -> Any:
    return build_chat_application_dispatcher(
        service=owned,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=FakeClock(wall=_WALL),
        execute_generation=lambda **_fields: None,
    )


def _kw(owned: Any) -> dict[str, Any]:
    return {"workspace_id": WORKSPACE_ID, "fencing_generation": owned.generation}


def _open_review(owned: Any, *, wait_id: str = WAIT_ID, revision: int = 7) -> Any:
    """A tool call waiting on a Review the authenticated principal may decide."""
    open_chat_turn(
        owned.connection,
        owned.identity,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        turn_id=TURN_ID,
        now_us=T + 40,
        **_kw(owned),
    )
    record_tool_proposal(
        owned.connection,
        owned.identity,
        turn_id=TURN_ID,
        step_id="step-c8-resolver",
        step_ordinal=0,
        tool_call_id=CALL_ID,
        tool_name="notes.append",
        tool_version="1",
        registry_ref="registry-c8-v1",
        proposed_arguments=ARGS,
        now_us=T + 41,
        **_kw(owned),
    )
    begin_policy_check(
        owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 50, **_kw(owned)
    )
    return request_tool_review(
        owned.connection,
        owned.identity,
        tool_call_id=CALL_ID,
        post_policy_arguments=ARGS,
        wait_id=wait_id,
        requester_actor_id="actor-core-chat",
        # The AUTHENTICATED principal, because the resolver holds `actorId` to
        # it and the library holds the decision to this policy. A test that
        # named a different reviewer would fail for the wrong reason.
        authorised_responder_policy={"actorId": PRINCIPAL},
        prompt={"title": "Append to notes.md?"},
        resume_token=f"resume-{wait_id}",
        target_resource_ref=RESOURCE,
        target_resource_revision=revision,
        expires_at_us=_EXPIRES_AT,
        now_us=T + 60,
        **_kw(owned),
    )


def _review_command(wait: Any, **extra: Any) -> dict[str, Any]:
    """A decision that ECHOES what the wait recorded.

    `targetActionDigest` and `targetResourceRevision` exist so a decision can
    say what the reviewer was shown, and the library refuses one that does not
    match the wait. So they are read from the wait rather than written as
    literals -- a literal would make these tests pass or fail on a fixture
    detail instead of on the resolver.
    """
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": REVIEW_COMMAND_ID,
        "workspaceId": WORKSPACE_ID,
        "actorId": PRINCIPAL,
        "waitId": wait.wait_id,
        "decision": "approved",
        "expectedRequestVersion": wait.version,
        "targetActionDigest": wait.target_action_digest,
        "targetResourceRevision": wait.target_resource_revision,
    }
    command.update(extra)
    return command


def _dispatch(
    owned: Any,
    command: Mapping[str, Any],
    *,
    command_name: str = "ReviewDecision",
    idempotency_key: str = REVIEW_COMMAND_ID,
) -> Any:
    return _c8_dispatcher(owned).dispatch(
        _request(
            command,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_id=f"req-{idempotency_key}",
            # NO conversation expectation. The shared harness attaches one by
            # default for its own seeded conversation, and these commands
            # address a WAIT rather than a conversation revision -- a Review
            # decision does not become stale because some conversation moved.
            # Left in, it refuses every dispatch here for a reason that has
            # nothing to do with C8.
            expected_conversation=None,
        )
    )


def _wait(owned: Any, wait_id: str = WAIT_ID) -> Any:
    return read_chat_wait(owned.connection, workspace_id=WORKSPACE_ID, wait_id=wait_id)


def test_a_review_decision_reaches_the_library_through_the_lent_writer(holder: Any) -> None:
    _open_review(holder)
    before = _wait(holder)
    assert before is not None and before.decision is None

    response = _dispatch(holder, _review_command(before))
    # THE INTEGRATION. Before the resolver existed this was
    # `dependency_unavailable`; a library opening its own fence inside the
    # seam's transaction would fail here instead.
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _wait(holder)
    assert after is not None
    assert after.decision == "approved"
    assert after.decided_by_actor_id == PRINCIPAL
    assert after.version == before.version + 1


def test_a_human_input_answer_reaches_the_library_the_same_way(holder: Any) -> None:
    open_chat_turn(
        holder.connection,
        holder.identity,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        turn_id=TURN_ID,
        now_us=T + 40,
        **_kw(holder),
    )
    open_human_input_request(
        holder.connection,
        holder.identity,
        conversation_id=manifest.CONVERSATION_ID,
        wait_id="wait-c8-human",
        requester_actor_id="actor-core-chat",
        authorised_responder_policy={"actorId": PRINCIPAL},
        prompt={"title": "Which branch?"},
        resume_token="resume-human",
        expires_at_us=_EXPIRES_AT,
        now_us=T + 60,
        generation_job_id=manifest.JOB_ID,
        turn_id=TURN_ID,
        **_kw(holder),
    )
    response = _dispatch(
        holder,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "commandId": HUMAN_COMMAND_ID,
            "workspaceId": WORKSPACE_ID,
            "actorId": PRINCIPAL,
            "waitId": "wait-c8-human",
            "expectedRequestVersion": 1,
            "answer": {"branch": "main"},
        },
        command_name="SubmitHumanInput",
        idempotency_key=HUMAN_COMMAND_ID,
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    answered = _wait(holder, "wait-c8-human")
    assert answered is not None
    assert answered.decision == "answered"


def test_the_idempotency_key_must_be_the_command_id(holder: Any) -> None:
    # The coupling every C10 command holds. Decoupled, a legitimate retry under
    # a fresh key becomes a SECOND decision on a wait that may be decided once,
    # which the library then refuses -- turning a retry into a conflict.
    _open_review(holder)
    response = _dispatch(holder, _review_command(_wait(holder)), idempotency_key="cmd-c8-something-else")
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    still_open = _wait(holder)
    assert still_open is not None and still_open.decision is None


def test_an_actor_other_than_the_principal_is_refused(holder: Any) -> None:
    _open_review(holder)
    response = _dispatch(holder, _review_command(_wait(holder), actorId="actor-someone-else"))
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _wait(holder).decision is None


def test_a_wait_this_workspace_does_not_hold_is_a_clean_conflict(holder: Any) -> None:
    _open_review(holder)
    response = _dispatch(holder, _review_command(_wait(holder), waitId="wait-c8-absent"))
    assert isinstance(response, ErrorResponseEnvelope), response
    # A conflict, not an unexpected error: the wait is simply not there.
    assert response.error.code == "conflict"


def test_a_superseded_request_version_is_a_conflict_not_an_unexpected_error(
    holder: Any,
) -> None:
    # The library raises `ChatWaitConflict` for this. Unmapped, it would escape
    # the DomainMutation as an unexpected error and the caller would learn
    # nothing it could act on.
    _open_review(holder)
    response = _dispatch(holder, _review_command(_wait(holder), expectedRequestVersion=99))
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _wait(holder).decision is None


def test_a_malformed_decision_is_refused_before_anything_is_written(holder: Any) -> None:
    _open_review(holder)
    open_wait = _wait(holder)
    for command, _reason in (
        (_review_command(open_wait, decision="maybe"), "not a Review decision"),
        (_review_command(open_wait, decision="APPROVED"), "case matters"),
        (_review_command(open_wait, targetActionDigest="sha256:nothex"), "malformed digest"),
        (_review_command(open_wait, expectedRequestVersion=0), "versions start at one"),
        (_review_command(open_wait, expectedRequestVersion=True), "True is not a version"),
        (_review_command(open_wait, targetResourceRevision=-1), "revisions are non-negative"),
        (_review_command(open_wait, smuggled="x"), "closed field set"),
    ):
        response = _dispatch(holder, command)
        assert isinstance(response, ErrorResponseEnvelope), command
        assert response.error.code == "invalid_request", command
    assert _wait(holder).decision is None


def test_a_required_target_field_must_be_stated_even_when_null(holder: Any) -> None:
    # Null is a legitimate value -- a wait carrying no action digest -- but
    # ABSENCE is a malformed document. A decision that cannot say what the
    # reviewer was shown is not a reviewable decision.
    _open_review(holder)
    open_wait = _wait(holder)
    for field in ("targetActionDigest", "targetResourceRevision"):
        command = _review_command(open_wait)
        del command[field]
        response = _dispatch(holder, command)
        assert isinstance(response, ErrorResponseEnvelope), field
        assert response.error.code == "invalid_request", field
    # ... and the decoder admits an explicit null, which is a wait that
    # carries no action digest. The LIBRARY then refuses it as a mismatch
    # against this wait, which does carry one -- a conflict, not a malformed
    # document, and that distinction is the point.
    response = _dispatch(holder, _review_command(open_wait, targetActionDigest=None))
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
