"""Durable Chat turn, step and governed tool-call lifecycle.

This is the first T-0660 slice: it gives Core durable identities for a Chat turn,
its ordered steps and one governed tool call without adopting provider SDK or
external Harness types. The execution seam accepts a small in-process registry for
tests and future adapters; it persists the post-policy argument record before
calling the tool and stores the result before marking the call terminal.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "MAX_SUMMARY_PARAMETERS",
    "ChatToolLifecycleConflict",
    "ChatToolLifecycleError",
    "ChatToolNotFound",
    "ChatTurnReplay",
    "GovernedTool",
    "MalformedToolProposal",
    "ToolActionSummary",
    "approve_tool_call",
    "deny_tool_call",
    "derive_action_summary",
    "execute_governed_tool_once",
    "open_chat_turn",
    "record_tool_proposal",
    "replay_chat_turn",
    "validate_action_summary",
]

_TERMINAL_TOOL_STATES: Final = frozenset({"succeeded", "failed", "denied", "cancelled"})
_SENSITIVE_MARKERS: Final = (
    "authorization",
    "bearer",
    "credential",
    "endpoint",
    "header",
    "secret",
    "token",
)

ToolHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class ChatToolLifecycleError(Exception):
    """Base class for durable Chat tool lifecycle refusals."""


class ChatToolLifecycleConflict(ChatToolLifecycleError):
    """A requested transition would violate the durable state machine."""


class ChatToolNotFound(ChatToolLifecycleError):
    """A proposed tool is not present in the governed registry."""


class MalformedToolProposal(ChatToolLifecycleError):
    """A tool proposal or result is not a safe canonical JSON object."""


#: How many post-policy arguments one action summary may name, and how long a
#: shown value may be. A summary is something a person reads before approving an
#: action, not a dump of the call.
MAX_SUMMARY_PARAMETERS: Final = 8
_MAX_SUMMARY_VALUE: Final = 256
_MAX_SUMMARY_NAME: Final = 64
#: Largest magnitude every JSON consumer can represent as an exact integer.
#: Action summaries cross into JavaScript, so accepting a larger Python int
#: could show a reviewer a rounded value that is not the argument being run.
_MAX_SUMMARY_NUMBER: Final = 9_007_199_254_740_991


@dataclass(frozen=True, slots=True)
class ToolActionSummary:
    """What the registry permits a Review to say about one call of this tool.

    DECLARATIVE, not a callable. A projector the tool supplied could return any
    prose it liked, and the reviewer would then be approving the tool's claim
    about itself. This names argument KEYS instead, and
    :func:`derive_action_summary` reads their values out of the exact
    post-policy arguments, so the only thing the registry can influence is
    WHICH of the call's own arguments are visible -- never what the summary says
    they are.

    `operation` is the action in the registry's own words. `resource_argument`
    names the argument carrying the resource the action affects;
    `parameter_arguments` names the further arguments that are material and safe
    to show. Everything else the call carries is counted and withheld.
    """

    operation: str
    resource_argument: str
    parameter_arguments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GovernedTool:
    name: str
    version: str
    registry_ref: str
    handler: ToolHandler
    #: Required of a tool that can ever reach a Review. `None` is a tool that is
    #: never reviewable: `request_tool_review` refuses it rather than opening a
    #: wait whose reviewer would have nothing to read.
    summary: ToolActionSummary | None = None


@dataclass(frozen=True, slots=True)
class ChatTurnReplay:
    turn: chat.ChatTurn
    steps: tuple[chat.ChatTurnStep, ...]
    tool_calls: tuple[chat.ChatToolCall, ...]
    tool_results: tuple[chat.ChatToolResult, ...]


def _digest_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise MalformedToolProposal("chat tool JSON must be an object")
    try:
        document = to_canonical_json(dict(value))
    except ChatContractDecodeError as error:
        raise MalformedToolProposal(
            f"chat tool JSON is not representable as canonical JSON: {error}"
        ) from error
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _assert_safe_json(value: Mapping[str, Any], *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise MalformedToolProposal(f"{label} must be a JSON object")
    try:
        to_canonical_json(dict(value))
    except ChatContractDecodeError as error:
        raise MalformedToolProposal(
            f"{label} is not representable as canonical JSON"
        ) from error
    _assert_no_sensitive_values(value, label=label)


def _assert_no_sensitive_values(value: Any, *, label: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if any(marker in str(key).lower() for marker in _SENSITIVE_MARKERS):
                raise MalformedToolProposal(f"{label} contains protected credential fields")
            _assert_no_sensitive_values(nested, label=label)
        return
    if isinstance(value, list | tuple):
        for nested in value:
            _assert_no_sensitive_values(nested, label=label)
        return
    if isinstance(value, str) and any(
        marker in value.lower() for marker in _SENSITIVE_MARKERS
    ):
        raise MalformedToolProposal(f"{label} contains protected credential text")


#: The closed action-summary field set. Exactly these four, always.
_SUMMARY_FIELDS: Final = frozenset({"operation", "parameters", "resource", "withheldParameters"})
_PARAMETER_FIELDS: Final = frozenset({"name", "value"})


def _summary_name(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_SUMMARY_NAME:
        raise MalformedToolProposal(f"{label} must be a string of 1 to {_MAX_SUMMARY_NAME} characters")
    return value


def _summary_value(value: Any, *, label: str) -> Any:
    """One material parameter value: a bounded scalar, never nested JSON.

    A dict or a list here would be the whole argument object arriving under a
    name that says it is safe, which is the leak this shape exists to refuse.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) <= _MAX_SUMMARY_NUMBER:
        return value
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and abs(value) <= _MAX_SUMMARY_NUMBER
    ):
        return value
    if isinstance(value, str) and 0 < len(value) <= _MAX_SUMMARY_VALUE:
        return value
    raise MalformedToolProposal(f"{label} must be a bounded string, number or boolean")


def validate_action_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse anything that is not exactly one closed action summary.

    Run on the DERIVED document, so :func:`derive_action_summary` cannot drift
    from the shape the contract publishes, and again on the STORED one before a
    snapshot projects it. The second call is the one that matters: a wait row
    written before this shape existed carries whatever prose its asking
    authority wrote, and publishing that under `actionSummary` would present
    someone's words as the derived fact a reviewer is told to trust.
    """
    if not isinstance(document, Mapping) or set(document) != _SUMMARY_FIELDS:
        raise MalformedToolProposal(
            "a chat tool action summary is exactly " + ", ".join(sorted(_SUMMARY_FIELDS))
        )
    _summary_name(document["operation"], label="an action summary operation")
    resource = document["resource"]
    if not isinstance(resource, str) or not 0 < len(resource) <= _MAX_SUMMARY_VALUE:
        raise MalformedToolProposal(
            f"an action summary resource must be a string of 1 to {_MAX_SUMMARY_VALUE} characters"
        )
    parameters = document["parameters"]
    if not isinstance(parameters, list) or len(parameters) > MAX_SUMMARY_PARAMETERS:
        raise MalformedToolProposal(
            f"an action summary names at most {MAX_SUMMARY_PARAMETERS} parameters"
        )
    names: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, Mapping) or set(parameter) != _PARAMETER_FIELDS:
            raise MalformedToolProposal("an action summary parameter is exactly a name and a value")
        names.append(_summary_name(parameter["name"], label="an action summary parameter name"))
        _summary_value(parameter["value"], label="an action summary parameter value")
    if len(set(names)) != len(names):
        raise MalformedToolProposal("an action summary names each parameter once")
    withheld = document["withheldParameters"]
    if not isinstance(withheld, int) or isinstance(withheld, bool) or withheld < 0:
        raise MalformedToolProposal("an action summary withholds a non-negative count of arguments")
    _assert_no_sensitive_values(document, label="chat tool action summary")
    return dict(document)


def derive_action_summary(
    tool: GovernedTool, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """The closed summary of one action, from the registry and the EXACT arguments.

    The registry declares which argument keys are safe to show
    (:class:`ToolActionSummary`); every value comes out of `arguments`, which is
    the post-policy argument record the call will actually execute. So the
    summary cannot say the action is one thing while the call does another, and
    no caller can hand in a sentence and have it published as the action.

    Fails closed, every time: a tool with no descriptor, a descriptor naming an
    argument this call does not carry, a value that is nested JSON rather than a
    scalar, or anything the credential check flags all raise rather than
    producing a summary a reviewer would read as complete.
    """
    descriptor = tool.summary
    if descriptor is None:
        raise MalformedToolProposal(
            f"chat tool {tool.name!r} registers no action summary and cannot be reviewed"
        )
    if not isinstance(arguments, Mapping):
        raise MalformedToolProposal("chat tool post-policy arguments must be a JSON object")
    named = (descriptor.resource_argument, *descriptor.parameter_arguments)
    if len(set(named)) != len(named):
        raise MalformedToolProposal("an action summary descriptor names each argument once")
    resource = arguments.get(descriptor.resource_argument)
    if not isinstance(resource, str) or not 0 < len(resource) <= _MAX_SUMMARY_VALUE:
        raise MalformedToolProposal(
            f"the action's resource argument {descriptor.resource_argument!r} is missing or unshowable"
        )
    return validate_action_summary(
        {
            "operation": descriptor.operation,
            "resource": resource,
            "parameters": [
                {
                    "name": _summary_name(name, label="an action summary parameter name"),
                    "value": _summary_value(
                        arguments.get(name), label=f"the action's {name!r} argument"
                    ),
                }
                for name in descriptor.parameter_arguments
            ],
            # The redaction fact. A reviewer who can see three of seven
            # arguments is told so, rather than left to assume they saw the
            # call -- which is the misreading an incomplete summary invites.
            "withheldParameters": sum(1 for key in arguments if key not in set(named)),
        }
    )


def open_chat_turn(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    generation_job_id: str,
    generation_attempt_id: str,
    turn_id: str,
    now_us: int,
) -> chat.ChatTurn:
    """Open or return the durable turn for one generation attempt."""
    existing = chat.read_chat_turn_by_attempt(
        connection,
        workspace_id=workspace_id,
        generation_job_id=generation_job_id,
        generation_attempt_id=generation_attempt_id,
    )
    if existing is not None:
        if existing.turn_id != turn_id:
            raise ChatToolLifecycleConflict("generation attempt already has another chat turn")
        return existing

    job = chat.read_generation_job(
        connection, workspace_id=workspace_id, generation_job_id=generation_job_id
    )
    attempt = chat.read_generation_attempt(
        connection, workspace_id=workspace_id, generation_attempt_id=generation_attempt_id
    )
    if (
        job is None
        or attempt is None
        or attempt.generation_job_id != generation_job_id
        or attempt.conversation_id != job.conversation_id
    ):
        raise ChatToolLifecycleConflict("chat turn must be linked to an existing generation attempt")

    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_turn(
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=generation_attempt_id,
            turn_id=turn_id,
            created_at_us=now_us,
            updated_at_us=now_us,
        )
    opened = chat.read_chat_turn(connection, workspace_id=workspace_id, turn_id=turn_id)
    if opened is None:  # pragma: no cover - transaction above just committed it
        raise StorageError(f"chat turn {turn_id!r} did not settle")
    return opened


def record_tool_proposal(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    turn_id: str,
    step_id: str,
    step_ordinal: int,
    tool_call_id: str,
    tool_name: str,
    tool_version: str,
    registry_ref: str,
    proposed_arguments: Mapping[str, Any],
    now_us: int,
) -> chat.ChatToolCall:
    """Persist the raw governed proposal before any policy approval or execution."""
    if not tool_name or not tool_version or not registry_ref:
        raise MalformedToolProposal("chat tool proposal is missing tool identity")
    _assert_safe_json(proposed_arguments, label="chat tool proposal")

    existing = chat.read_chat_tool_call(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if existing is not None:
        if (
            existing.turn_id == turn_id
            and existing.step_id == step_id
            and existing.tool_name == tool_name
            and existing.tool_version == tool_version
            and existing.registry_ref == registry_ref
            and existing.proposed_arguments_digest == _digest_json(proposed_arguments)
        ):
            return existing
        raise ChatToolLifecycleConflict("chat tool call id already names another proposal")

    turn = _require_turn(connection, workspace_id=workspace_id, turn_id=turn_id)
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_turn_step(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            step_id=step_id,
            generation_job_id=turn.generation_job_id,
            generation_attempt_id=turn.generation_attempt_id,
            step_ordinal=step_ordinal,
            step_kind="tool_call",
            created_at_us=now_us,
            updated_at_us=now_us,
        )
        writer.append_chat_tool_call(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            step_id=step_id,
            generation_job_id=turn.generation_job_id,
            generation_attempt_id=turn.generation_attempt_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_version=tool_version,
            registry_ref=registry_ref,
            proposed_arguments=proposed_arguments,
            created_at_us=now_us,
            updated_at_us=now_us,
        )
        writer.update_chat_turn(
            turn_id=turn.turn_id,
            expected_version=turn.version,
            state=turn.state,
            current_step_id=step_id,
            updated_at_us=now_us,
        )
    stored = chat.read_chat_tool_call(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if stored is None:  # pragma: no cover - transaction above just committed it
        raise StorageError(f"chat tool call {tool_call_id!r} did not settle")
    return stored


def approve_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    post_policy_arguments: Mapping[str, Any],
    now_us: int,
) -> chat.ChatToolCall:
    """Approve a proposal with the exact arguments later executed."""
    _assert_safe_json(post_policy_arguments, label="chat tool post-policy arguments")
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if call.policy_state == "denied" or call.state in _TERMINAL_TOOL_STATES:
        raise ChatToolLifecycleConflict("terminal or denied tool calls cannot be approved")
    post_policy_digest = _digest_json(post_policy_arguments)
    if call.policy_state == "approved":
        if call.post_policy_arguments_digest == post_policy_digest:
            return call
        raise ChatToolLifecycleConflict("tool call approval arguments cannot change")
    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=call.version,
            state="approved",
            policy_state="approved",
            post_policy_arguments=post_policy_arguments,
            updated_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=step.step_id,
            expected_version=step.version,
            state="approved",
            updated_at_us=now_us,
        )
    return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)


def deny_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    failure_code: str,
    now_us: int,
) -> chat.ChatToolCall:
    """Record a terminal denial. A later approval cannot weaken this decision."""
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if call.state in _TERMINAL_TOOL_STATES:
        if call.state == "denied":
            return call
        raise ChatToolLifecycleConflict("terminal tool calls cannot be denied again")
    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=call.version,
            state="denied",
            policy_state="denied",
            failure_code=failure_code,
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=step.step_id,
            expected_version=step.version,
            state="denied",
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
    return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)


def execute_governed_tool_once(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    tools: Mapping[str, GovernedTool],
    now_us: int,
) -> chat.ChatToolResult:
    """Execute an approved tool call once and return the durable result on replay."""
    existing_result = chat.read_chat_tool_result(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if existing_result is not None:
        return existing_result

    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if call.state == "denied" or call.policy_state == "denied":
        raise ChatToolLifecycleConflict("denied tool calls cannot execute")
    if call.state == "failed":
        raise ChatToolLifecycleConflict("failed tool calls cannot execute")
    if call.policy_state != "approved" or call.post_policy_arguments is None:
        raise ChatToolLifecycleConflict("tool execution requires approved post-policy arguments")
    tool = tools.get(call.tool_name)
    if (
        tool is None
        or tool.version != call.tool_version
        or tool.registry_ref != call.registry_ref
    ):
        _fail_tool_call(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=fencing_generation,
            call=call,
            failure_code="unknown_tool",
            now_us=now_us,
        )
        raise ChatToolNotFound(f"chat tool {call.tool_name!r} is not available")
    if call.state == "executing":
        raise ChatToolLifecycleConflict("tool call is already executing")
    if call.state not in {"approved", "proposed"}:
        raise ChatToolLifecycleConflict("tool call is not executable")

    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    executed_digest = call.post_policy_arguments_digest
    if executed_digest is None:  # pragma: no cover - guarded by state checks above
        raise ChatToolLifecycleConflict("approved tool call has no argument digest")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=call.version,
            state="executing",
            policy_state="approved",
            post_policy_arguments=call.post_policy_arguments,
            executed_arguments_digest=executed_digest,
            updated_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=step.step_id,
            expected_version=step.version,
            state="executing",
            updated_at_us=now_us,
        )

    result_payload = tool.handler(call.post_policy_arguments)
    _assert_safe_json(result_payload, label="chat tool result")
    executing = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    executing_step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    result_id = f"{tool_call_id}.result"
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.append_chat_tool_result(
            conversation_id=executing.conversation_id,
            turn_id=executing.turn_id,
            step_id=executing.step_id,
            generation_job_id=executing.generation_job_id,
            generation_attempt_id=executing.generation_attempt_id,
            tool_call_id=tool_call_id,
            result_id=result_id,
            status="succeeded",
            result_payload=result_payload,
            created_at_us=now_us,
        )
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=executing.version,
            state="succeeded",
            policy_state="approved",
            post_policy_arguments=executing.post_policy_arguments,
            executed_arguments_digest=executing.executed_arguments_digest,
            result_id=result_id,
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=executing_step.step_id,
            expected_version=executing_step.version,
            state="succeeded",
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
    result = chat.read_chat_tool_result(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if result is None:  # pragma: no cover - transaction above just committed it
        raise StorageError(f"chat tool result for {tool_call_id!r} did not settle")
    return result


def replay_chat_turn(
    connection: sqlite3.Connection, *, workspace_id: str, turn_id: str
) -> ChatTurnReplay:
    turn = _require_turn(connection, workspace_id=workspace_id, turn_id=turn_id)
    steps = chat.read_chat_turn_steps(connection, workspace_id=workspace_id, turn_id=turn_id)
    tool_calls = chat.read_chat_tool_calls(connection, workspace_id=workspace_id, turn_id=turn_id)
    results = tuple(
        result
        for call in tool_calls
        for result in [
            chat.read_chat_tool_result(
                connection, workspace_id=workspace_id, tool_call_id=call.tool_call_id
            )
        ]
        if result is not None
    )
    return ChatTurnReplay(turn=turn, steps=steps, tool_calls=tool_calls, tool_results=results)


def _fail_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    call: chat.ChatToolCall,
    failure_code: str,
    now_us: int,
) -> None:
    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.update_chat_tool_call(
            tool_call_id=call.tool_call_id,
            expected_version=call.version,
            state="failed",
            policy_state=call.policy_state,
            post_policy_arguments=call.post_policy_arguments,
            executed_arguments_digest=call.executed_arguments_digest,
            failure_code=failure_code,
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=step.step_id,
            expected_version=step.version,
            state="failed",
            updated_at_us=now_us,
            finished_at_us=now_us,
        )


def _require_turn(
    connection: sqlite3.Connection, *, workspace_id: str, turn_id: str
) -> chat.ChatTurn:
    turn = chat.read_chat_turn(connection, workspace_id=workspace_id, turn_id=turn_id)
    if turn is None:
        raise ChatToolLifecycleConflict(f"chat turn {turn_id!r} is not in this workspace")
    return turn


def _require_step(
    connection: sqlite3.Connection, *, workspace_id: str, step_id: str
) -> chat.ChatTurnStep:
    rows = connection.execute(
        "SELECT turn_id FROM omnivia_chat_turn_steps "
        "WHERE workspace_id = ? AND step_id = ?",
        (workspace_id, step_id),
    ).fetchone()
    if rows is None:
        raise ChatToolLifecycleConflict(f"chat turn step {step_id!r} is not in this workspace")
    steps = chat.read_chat_turn_steps(connection, workspace_id=workspace_id, turn_id=str(rows[0]))
    for step in steps:
        if step.step_id == step_id:
            return step
    raise ChatToolLifecycleConflict(f"chat turn step {step_id!r} is not in this workspace")


def _require_tool_call(
    connection: sqlite3.Connection, *, workspace_id: str, tool_call_id: str
) -> chat.ChatToolCall:
    call = chat.read_chat_tool_call(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if call is None:
        raise ChatToolLifecycleConflict(f"chat tool call {tool_call_id!r} is not in this workspace")
    return call
