"""Fixture conformance for the governed Chat Runtime Contract v1 fixtures.

Preloads all 13 canonical schemas (``contracts/chat/v1/schemas``) into one
Draft 2020-12 ``referencing`` registry so cross-file ``$ref``s resolve, then
evaluates every fixture ``FIXTURE-MANIFEST.json`` names exactly the way the
approved offline validator does
(``architecture-v1.4.0:tools/chat-runtime-contract-v1/validate_contract_freeze.py``,
``evaluate_fixture``): schema conformance against its declared ``schema_ref``
*plus* every relational check the manifest entry itself declares in ``checks``.
A fixture is valid only when both hold; the manifest's ``expect`` must match,
and where it names an ``expected_error_contains`` substring, some produced
diagnostic must contain it.

A fixture is one of three manifest ``kind``s:

- a ``record``, validated against its single ``schema_ref``;
- a ``trace`` (``{"events": [...]}`` or ``{"messages": [...]}``), whose every
  item validates against ``schema_ref``, and which additionally gets the three
  per-Attempt F2a checks in :data:`_AUTO_PROVIDER_CHECKS` when its
  ``schema_ref`` is the ``ProviderEvent`` union;
- a ``relational_pair`` (named parts, each with its own entry in
  ``partSchemaRefs``), where each part validates against its own ref.

:data:`_CHECKS` below is a behaviour-for-behaviour port of that validator's
``CHECK_FUNCTIONS`` registry -- the F2a per-Attempt stream rules, the
``attemptMeta`` silent-fallback denial, the route-evidence relational
invariant, and the F2b hosted-bridge lifecycle state machine, paging
discipline, backpressure watermark/overflow, cursor continuity, negotiated
bounds, duplicate-event and dispose-cleanup rules (freeze §9, §11,
§12.0.1-§12.9). Each function keeps the validator's own ``(ok, detail)``
signature and diagnostic wording, because the manifest's
``expected_error_contains`` needles are substrings of exactly those messages.
Nothing here is invented: a check runs only where the manifest names it.

``jsonschema``/``referencing`` are development-only dependencies: a
conformance gate, not part of the standard-library-only contract package.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_ROOT = REPO_ROOT / "contracts" / "chat" / "v1"
SCHEMAS_DIR = CANONICAL_ROOT / "schemas"
FIXTURES_DIR = CANONICAL_ROOT / "fixtures"

#: A ported check's result: ``(ok, detail)``, detail empty when ``ok``.
CheckResult = tuple[bool, str]
Document = Mapping[str, Any]


def _load_schemas() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMAS_DIR.glob("*.schema.json"))
    }


SCHEMAS = _load_schemas()
REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
    for schema in SCHEMAS.values()
)
MANIFEST: dict[str, Any] = json.loads((FIXTURES_DIR / "FIXTURE-MANIFEST.json").read_text(encoding="utf-8"))


def load(relative_path: str) -> Any:
    return json.loads((CANONICAL_ROOT / relative_path).read_text(encoding="utf-8"))


def validator_for(schema_ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": schema_ref}, registry=REGISTRY)


def _errors(schema_ref: str, document: Any) -> list[Any]:
    return list(validator_for(schema_ref).iter_errors(document))


# --------------------------------------------------------------------------
# Ported relational / sequence checks -- see the module docstring for the
# authority and for why the diagnostic wording is kept verbatim.
# --------------------------------------------------------------------------

_CHECKS: dict[str, Callable[[Document], CheckResult]] = {}


def _check(name: str) -> Callable[[Callable[[Document], CheckResult]], Callable[[Document], CheckResult]]:
    def register(fn: Callable[[Document], CheckResult]) -> Callable[[Document], CheckResult]:
        _CHECKS[name] = fn
        return fn

    return register


def _problems(problems: Sequence[str]) -> CheckResult:
    return (False, "; ".join(problems)) if problems else (True, "")


def _serialized_byte_size(obj: Any) -> int:
    """The exact byte length of ``obj``'s canonical UTF-8 JSON serialization."""
    return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


# -- D07 graph relational rules ---------------------------------------------


@_check("derivation_acyclic")
def _derivation_acyclic(content: Document) -> CheckResult:
    if content.get("sourceMessageId") == content.get("derivedMessageId"):
        return False, "REF042-INV-004: derivation sourceMessageId equals derivedMessageId (self-cycle)"
    return True, ""


@_check("workspace_scope_enforced")
def _workspace_scope_enforced(content: Document) -> CheckResult:
    message, context = content["message"], content["trustedContext"]
    if message.get("workspaceId") != context.get("workspaceId"):
        return False, (
            "WORKSPACE_BOUNDARY: message.workspaceId differs from the trusted session "
            "workspaceId (REF042-RULE-001, REF042-RULE-012)"
        )
    return True, ""


@_check("cross_conversation_parent_rejected")
def _cross_conversation_parent(content: Document) -> CheckResult:
    parent, child = content["parentMessage"], content["childMessage"]
    if (
        child.get("parentMessageId") == parent.get("messageId")
        and child.get("conversationId") != parent.get("conversationId")
    ):
        return False, "REF042-RULE-004: child Message references a parent from a different Conversation"
    return True, ""


@_check("amendment_source_role_check")
def _amendment_source_role(content: Document) -> CheckResult:
    source, derivation = content["sourceMessage"], content["derivation"]
    kind = derivation.get("kind")
    if kind == "amendment" and source.get("role") != "user":
        return False, "REF042-INV-008: amendment source Message must be user-role"
    if kind == "regeneration" and source.get("role") != "assistant":
        return False, "REF042-INV-009: regeneration source Message must be assistant-role"
    return True, ""


@_check("no_duplicate_chat_event_ids")
def _no_duplicate_chat_event_ids(content: Document) -> CheckResult:
    """freeze §9 rule 2: a repeated eventId is a duplicate delivery, never a new fact."""
    seen: dict[str, int] = {}
    problems = []
    for i, message in enumerate(content["messages"]):
        event_id = message.get("eventId")
        if event_id in seen:
            problems.append(f"duplicate eventId {event_id!r} delivered at positions {seen[event_id]} and {i}")
        else:
            seen[event_id] = i
    return _problems(problems)


# -- F2a per-Attempt stream rules (freeze §11) -------------------------------


def _group_by_attempt(events: Sequence[Document]) -> dict[tuple[Any, Any], list[Document]]:
    groups: dict[tuple[Any, Any], list[Document]] = {}
    for event in events:
        groups.setdefault((event.get("invocationId"), event.get("attemptId")), []).append(event)
    return groups


@_check("f2a_exactly_one_terminal_per_attempt")
def _f2a_exactly_one_terminal(content: Document) -> CheckResult:
    problems = []
    for key, events in _group_by_attempt(content["events"]).items():
        terminals = [e for e in events if e.get("eventType") in ("finish", "error")]
        if len(terminals) == 0:
            problems.append(f"{key}: exactly one terminal required, found 0 (end-without-terminal)")
        elif len(terminals) > 1:
            problems.append(
                f"{key}: exactly one terminal required, found {len(terminals)} (duplicate terminal)"
            )
        else:
            last = max(events, key=lambda e: e.get("ordinal", -1))
            if last is not terminals[0]:
                problems.append(f"{key}: terminal event is not the last event by ordinal")
    return _problems(problems)


@_check("f2a_ordinal_monotonic_per_attempt")
def _f2a_ordinal_monotonic(content: Document) -> CheckResult:
    problems = []
    for key, events in _group_by_attempt(content["events"]).items():
        ordinals = [e.get("ordinal") for e in events]
        for i in range(1, len(ordinals)):
            if ordinals[i] <= ordinals[i - 1]:
                relation = "duplicate" if ordinals[i] == ordinals[i - 1] else "descending"
                problems.append(
                    f"{key}: ordinal is not strictly increasing ({relation} at position {i}): {ordinals}"
                )
    return _problems(problems)


@_check("f2a_no_duplicate_provider_event_id_per_attempt")
def _f2a_no_duplicate_provider_event_id(content: Document) -> CheckResult:
    problems = []
    for key, events in _group_by_attempt(content["events"]).items():
        seen: dict[str, int] = {}
        for i, event in enumerate(events):
            provider_event_id = event.get("providerEventId")
            if provider_event_id is None:
                continue
            if provider_event_id in seen:
                problems.append(
                    f"{key}: duplicate providerEventId {provider_event_id!r} at positions "
                    f"{seen[provider_event_id]} and {i}"
                )
            else:
                seen[provider_event_id] = i
    return _problems(problems)


#: The three per-Attempt F2a checks every ``ProviderEvent``-union trace gets
#: whether or not the manifest entry names them.
_AUTO_PROVIDER_CHECKS = (
    "f2a_exactly_one_terminal_per_attempt",
    "f2a_ordinal_monotonic_per_attempt",
    "f2a_no_duplicate_provider_event_id_per_attempt",
)


def _route_admitted_differs(evidence: Document) -> bool:
    """Whether ``admittedRoute`` names a different Provider route than ``configuredPreference``.

    Compares only the ``connectionId``/``modelId`` tuple each names -- never
    full-dict equality: ``admittedRoute`` always carries required
    ``adapterName``/``adapterVersion`` fields ``configuredPreference`` never
    has, so the two dicts can never be equal even for the identical route.
    """
    configured = evidence.get("configuredPreference") or {}
    admitted = evidence.get("admittedRoute") or {}
    return (configured.get("connectionId"), configured.get("modelId")) != (
        admitted.get("connectionId"),
        admitted.get("modelId"),
    )


def _route_evidence_relational_problems(evidence: Document, label: str) -> list[str]:
    """freeze §11 rule 7, shared by the F2a trace check and the durable-record check.

    ``fallback`` requires ``fallbackAuthorised: true`` and a changed
    ``admittedRoute``; ``configured``/``same_route_retry`` require
    ``fallbackAuthorised: false`` and an unchanged ``admittedRoute``.
    """
    problems = []
    decision = evidence.get("routeDecision")
    authorised = evidence.get("fallbackAuthorised")
    changed = _route_admitted_differs(evidence)
    if decision == "fallback":
        if authorised is not True:
            problems.append(f"{label}: routeDecision fallback requires fallbackAuthorised true")
        if not changed:
            problems.append(f"{label}: routeDecision fallback requires a changed admitted route")
    else:
        if authorised is not False:
            problems.append(f"{label}: routeDecision {decision} requires fallbackAuthorised false")
        if changed:
            problems.append(f"{label}: a changed admitted route may not be labelled {decision}")
    return problems


@_check("route_evidence_internally_consistent")
def _route_evidence_internally_consistent(content: Document) -> CheckResult:
    """(a) silent fallback denial -- a declared route change with no ``routeEvidence``
    at all is refused; (b) ``routeDecision``/``fallbackAuthorised``/route-change
    self-consistency.

    The ``attemptMeta`` block is the fixture's own declaration of what the
    caller *configured* versus what the invocation actually *resolved to*; the
    manifest names this check on exactly the fixtures that carry it, so the
    cross-reference is governed, not inferred.
    """
    meta = content.get("attemptMeta", {})
    problems = []
    for (invocation_id, attempt_id), events in _group_by_attempt(content["events"]).items():
        terminal = next((e for e in events if e.get("eventType") in ("finish", "error")), None)
        evidence = terminal.get("routeEvidence") if terminal else None

        attempt_meta = meta.get(invocation_id)
        if attempt_meta and attempt_meta.get("configuredRoute") != attempt_meta.get("resolvedRoute") and not evidence:
            problems.append(
                f"{invocation_id}/{attempt_id}: resolvedRoute differs from configuredRoute but the "
                "terminal event carries no route evidence (silent fallback denied)"
            )

        if evidence:
            problems.extend(
                _route_evidence_relational_problems(evidence, f"{invocation_id}/{attempt_id}")
            )
    return _problems(problems)


@_check("provider_invocation_record_route_evidence_consistent")
def _provider_invocation_record_route_evidence_consistent(content: Document) -> CheckResult:
    """The same invariant applied to a terminal ``ProviderInvocationRecord``'s own
    ``routeEvidence``, so a durable record cannot evade what its trace-projected
    events prove."""
    evidence = content.get("routeEvidence")
    if not evidence:
        return True, ""
    return _problems(
        _route_evidence_relational_problems(evidence, content.get("invocationId", "<record>"))
    )


# -- F2b hosted-bridge rules (freeze §12.0.1-§12.9) --------------------------

#: The frozen pending-event overflow threshold (freeze §12.8), also frozen as
#: ``bridge.schema.json#/$defs/BackpressureOverflowThreshold``'s ``const``.
_BACKPRESSURE_OVERFLOW_THRESHOLD = 1000
#: A pause is cleared only by an explicit ``resume``, by a ``bridge.resnapshot``
#: (which restarts the subscription outright), or by disposal/rebind.
_F2B_PAUSE_CLEARING_TYPES = {"bridge.resnapshot", "bridge.context-disposed", "bridge.context-bound"}
_F2B_MODULE_TO_HOST_TYPES = {"bridge.snapshot-request", "bridge.command", "bridge.backpressure"}
_F2B_HOST_DATA_TYPES = {
    "bridge.snapshot-page",
    "bridge.event-batch",
    "bridge.command-result",
    "bridge.resnapshot",
}
#: The precise closed lifecycle reason for module traffic that arrives after
#: ``bridge.context-bound`` but before the module opened its snapshot handshake.
_F2B_NOT_READY_REASON = "not_ready"
#: F2b's own outer-carrier ceiling: the evidenced Platform Module Host Contract
#: ``MODULE_HOST_MAX_MESSAGE_BYTES``, minus the reserve for the ``v``/``dir``/
#: ``kind`` wrapper it adds around F2b's own payload (freeze §12.0.2).
_F2B_PLATFORM_CARRIER_MAX_BYTES = 65536
_F2B_OUTER_ENVELOPE_OVERHEAD_RESERVE_BYTES = 4096


@_check("f2b_context_mismatch_refused")
def _f2b_context_mismatch_refused(content: Document) -> CheckResult:
    """Also catches pre-bind traffic: before the first ``bridge.context-bound``
    this trace has ever seen, any message carrying a ``contextId`` is
    definitionally mismatched and must be refused exactly as a genuine mismatch
    would be. Traffic after a *later* ``bridge.context-disposed`` is owned by
    ``f2b_late_command_refused``, not demanded a second refusal here."""
    messages = content["messages"]
    bound = None
    ever_bound = False
    problems = []
    for i, message in enumerate(messages):
        message_type = message.get("messageType")
        if message_type == "bridge.context-bound":
            bound = message.get("contextId")
            ever_bound = True
            continue
        if message_type == "bridge.context-disposed":
            bound = None
            continue
        context_id = message.get("contextId")
        if context_id and context_id != bound and (bound is not None or not ever_bound):
            following = messages[i + 1] if i + 1 < len(messages) else None
            if not (
                following
                and following.get("messageType") == "bridge.refusal"
                and following.get("reason") == "context_mismatch"
            ):
                problems.append(
                    f"message[{i}] ({message_type}) carries mismatched or pre-bind contextId "
                    f"{context_id!r} with no immediately following context_mismatch refusal"
                )
    return _problems(problems)


@_check("f2b_no_duplicate_bind")
def _f2b_no_duplicate_bind(content: Document) -> CheckResult:
    """A second ``bridge.context-bound`` with no intervening
    ``bridge.context-disposed`` is a protocol violation, never a silent rebind."""
    bound = False
    problems = []
    for i, message in enumerate(content["messages"]):
        message_type = message.get("messageType")
        if message_type == "bridge.context-bound":
            if bound:
                problems.append(
                    f"message[{i}] is a duplicate bridge.context-bound with no intervening "
                    "bridge.context-disposed"
                )
            bound = True
        elif message_type == "bridge.context-disposed":
            bound = False
    return _problems(problems)


@_check("f2b_late_command_refused")
def _f2b_late_command_refused(content: Document) -> CheckResult:
    """Every module->host message received after ``bridge.context-disposed`` is a
    late command and is refused as ``late_command``, never queued and replayed
    against a new context."""
    messages = content["messages"]
    disposed = False
    problems = []
    for i, message in enumerate(messages):
        message_type = message.get("messageType")
        if message_type == "bridge.context-disposed":
            disposed = True
            continue
        if disposed and message_type in _F2B_MODULE_TO_HOST_TYPES:
            following = messages[i + 1] if i + 1 < len(messages) else None
            if not (
                following
                and following.get("messageType") == "bridge.refusal"
                and following.get("reason") == "late_command"
            ):
                problems.append(
                    f"message[{i}] ({message_type}) arrived after context-disposed with no "
                    "following late_command refusal"
                )
    return _problems(problems)


@_check("f2b_dispose_cleanup_no_further_host_traffic")
def _f2b_dispose_cleanup(content: Document) -> CheckResult:
    """After ``bridge.context-disposed`` the host sends no further data for that
    context. ``bridge.refusal`` is excluded (refusing late module traffic is the
    expected response) and so is a fresh ``bridge.context-bound`` (a legitimate
    context rotation)."""
    disposed = False
    problems = []
    for i, message in enumerate(content["messages"]):
        message_type = message.get("messageType")
        if message_type == "bridge.context-disposed":
            disposed = True
            continue
        if message_type == "bridge.context-bound":
            disposed = False
            continue
        if disposed and message_type in _F2B_HOST_DATA_TYPES:
            problems.append(
                f"message[{i}] ({message_type}) is host-issued traffic after context-disposed "
                "(dispose cleanup violated)"
            )
    return _problems(problems)


@_check("f2b_backpressure_honoured")
def _f2b_backpressure_honoured(content: Document) -> CheckResult:
    """``action: pause`` is honoured before the host's next ``bridge.event-batch``.
    The pause persists until a valid ``resume``, a ``bridge.resnapshot`` or
    disposal/rebind; nothing else lifts it."""
    paused = False
    problems = []
    for i, message in enumerate(content["messages"]):
        message_type = message.get("messageType")
        if message_type == "bridge.backpressure":
            paused = message.get("action") == "pause"
            continue
        if message_type in _F2B_PAUSE_CLEARING_TYPES:
            paused = False
            continue
        if message_type == "bridge.event-batch" and paused:
            problems.append(
                f"message[{i}] is an event-batch sent while paused (backpressure not honoured), "
                "with no intervening resume, resnapshot or disposal"
            )
    return _problems(problems)


def _f2b_host_issued_cursor(message: Document) -> str | None:
    """The event-stream position a host message advances the module's applied
    watermark to, or ``None`` if it advances nothing."""
    message_type = message.get("messageType")
    if message_type == "bridge.snapshot-page" and message.get("hasMore") is False:
        return message.get("snapshotBoundaryCursor")
    if message_type == "bridge.event-batch":
        return message.get("nextCursor")
    return None


@_check("f2b_backpressure_pause_watermark_and_overflow")
def _f2b_backpressure_overflow(content: Document) -> CheckResult:
    """Two invariants on one ``action: pause`` (freeze §12.8):

    1. ``watermarkCursor`` must equal the module's actual last applied position.
       A *stale* watermark (a position already passed) and a *future or unknown*
       one (a position the host never issued) are both rejected: an
       acknowledgement that does not name the exact applied position
       acknowledges nothing.
    2. A ``pendingEvents`` count at or beyond the overflow threshold is answered
       with ``bridge.resnapshot`` (``reason: backpressure_overflow``)
       immediately; a plain ``resume`` at or above the threshold is forbidden.
    """
    messages = content["messages"]
    problems = []
    last_applied: str | None = None
    issued: set[str] = set()
    for i, message in enumerate(messages):
        advanced = _f2b_host_issued_cursor(message)
        if advanced is not None:
            last_applied = advanced
            issued.add(advanced)
        if message.get("messageType") != "bridge.backpressure" or message.get("action") != "pause":
            continue

        watermark = message.get("watermarkCursor")
        if watermark is not None and watermark != last_applied:
            kind = "stale" if watermark in issued else "future or unknown"
            problems.append(
                f"message[{i}] pause watermarkCursor {watermark!r} is {kind}: it does not equal "
                f"the last applied position {last_applied!r}"
            )

        pending = message.get("pendingEvents")
        if pending is not None and pending >= _BACKPRESSURE_OVERFLOW_THRESHOLD:
            following = messages[i + 1] if i + 1 < len(messages) else None
            if not (
                following
                and following.get("messageType") == "bridge.resnapshot"
                and following.get("reason") == "backpressure_overflow"
            ):
                answered_with = (
                    "a plain resume"
                    if (
                        following
                        and following.get("messageType") == "bridge.backpressure"
                        and following.get("action") == "resume"
                    )
                    else "no backpressure_overflow resnapshot"
                )
                problems.append(
                    f"message[{i}] pause pendingEvents={pending} at/above the overflow threshold "
                    f"({_BACKPRESSURE_OVERFLOW_THRESHOLD}) answered with {answered_with}; a "
                    "backpressure_overflow resnapshot is required and a normal resume is forbidden"
                )
    return _problems(problems)


@_check("f2b_snapshot_event_cursor_continuity")
def _f2b_snapshot_event_cursor_continuity(content: Document) -> CheckResult:
    """A snapshot's final page sets the last known event-stream position; each
    ``bridge.event-batch.fromCursor`` must continue from it, else it is a cursor
    gap that forces ``bridge.resnapshot``, never a guessed continuation."""
    messages = content["messages"]
    known = None
    problems = []
    for i, message in enumerate(messages):
        message_type = message.get("messageType")
        if message_type == "bridge.snapshot-page" and message.get("hasMore") is False:
            known = message.get("snapshotBoundaryCursor")
        elif message_type == "bridge.event-batch":
            from_cursor = message.get("fromCursor")
            if known is not None and from_cursor != known:
                following = messages[i + 1] if i + 1 < len(messages) else None
                if not (following and following.get("messageType") == "bridge.resnapshot"):
                    problems.append(
                        f"message[{i}] event-batch fromCursor {from_cursor!r} does not continue "
                        f"from the last known position {known!r} (cursor gap) with no following "
                        "resnapshot"
                    )
            known = message.get("nextCursor")
        elif message_type == "bridge.resnapshot":
            known = None
    return _problems(problems)


@_check("f2b_resnapshot_triggers_snapshot_request")
def _f2b_resnapshot_triggers_request(content: Document) -> CheckResult:
    """A ``bridge.resnapshot`` is followed by a matching ``bridge.snapshot-request``
    carrying the exact same ``resnapshotCursor`` -- the explicit host-mediated
    resnapshot flow, never a renderer-side fetch or a guessed continuation."""
    messages = content["messages"]
    problems = []
    for i, message in enumerate(messages):
        if message.get("messageType") != "bridge.resnapshot":
            continue
        expected_cursor = message.get("resnapshotCursor")
        found = False
        for following in messages[i + 1:]:
            following_type = following.get("messageType")
            if following_type == "bridge.snapshot-request":
                found = following.get("cursor") == expected_cursor
                break
            if following_type in ("bridge.command", "bridge.event-batch", "bridge.snapshot-page"):
                break
        if not found:
            problems.append(
                f"message[{i}] bridge.resnapshot (cursor {expected_cursor!r}) is not followed by a "
                "matching bridge.snapshot-request"
            )
    return _problems(problems)


@_check("f2b_command_context_consistent")
def _f2b_command_context_consistent(content: Document) -> CheckResult:
    """``command.contextId`` must match the bound context; ``payload.commandId``
    must equal the bridge command's own ``commandId``; and any
    ``workspaceId``/``conversationId``/``actorId`` claim carried in the D07
    request payload must match the bound context's own claims."""
    bound, command = content["boundContext"], content["command"]
    payload = command.get("payload", {})
    problems = []
    if command.get("contextId") != bound.get("contextId"):
        problems.append("command.contextId does not match boundContext.contextId")
    if payload.get("commandId") != command.get("commandId"):
        problems.append("payload.commandId does not match the bridge command's own commandId")
    for claim in ("workspaceId", "conversationId", "actorId"):
        if claim in payload and payload[claim] != bound.get(claim):
            problems.append(f"payload.{claim} does not match the bound context's {claim}")
    return _problems(problems)


#: Snapshot items that are the bound actor's own authored state, so their
#: ``actorId`` is held to the bound context's, not merely workspace/conversation.
_F2B_ACTOR_SCOPED_ITEM_KINDS = ("composer_draft", "direction")


@_check("f2b_event_and_snapshot_context_consistent")
def _f2b_event_and_snapshot_context_consistent(content: Document) -> CheckResult:
    """Every carried D07 record's ``workspaceId``/``conversationId`` inside a
    ``bridge.event-batch`` or ``bridge.snapshot-page`` is cross-checked against
    the currently bound context. A ``composer_draft`` or ``direction`` item, and a
    ``chat.direction.state_changed`` event, are the bound actor's own, so their
    ``actorId`` is cross-checked too."""
    bound = None
    problems = []
    for i, message in enumerate(content["messages"]):
        message_type = message.get("messageType")
        if message_type == "bridge.context-bound":
            bound = message
            continue
        if message_type == "bridge.context-disposed":
            bound = None
            continue
        if bound is None:
            continue
        if message_type == "bridge.event-batch":
            carried = [(j, event) for j, event in enumerate(message.get("events", []))]
            label = "events"
        elif message_type == "bridge.snapshot-page":
            carried = [(j, item.get("payload", {})) for j, item in enumerate(message.get("items", []))]
            label = "items"
        else:
            continue
        for j, record in carried:
            claims = ("workspaceId", "conversationId")
            if (
                label == "items" and message["items"][j].get("itemKind") in _F2B_ACTOR_SCOPED_ITEM_KINDS
            ) or record.get("eventType") == "chat.direction.state_changed":
                claims += ("actorId",)
            for claim in claims:
                if claim in record and record[claim] != bound.get(claim):
                    suffix = f"payload.{claim}" if label == "items" else claim
                    problems.append(
                        f"message[{i}].{label}[{j}] {suffix} does not match the bound context"
                    )
    return _problems(problems)


@_check("f2b_direction_requires_capability")
def _f2b_direction_requires_capability(content: Document) -> CheckResult:
    """Directions exist in a bound context only if its ``bridge.context-bound``
    declared ``supportsDirection: true`` (bridge protocol 2.0, C6). Absence and
    ``false`` are the same posture: unavailable. There, a ``SubmitSteeringInput``
    command must be refused ``capability_unavailable`` (never ``forbidden_capability``),
    and the host must not project a ``direction`` snapshot item or deliver a
    ``chat.direction.state_changed`` event at all."""
    messages = content["messages"]
    bound = None
    problems = []
    for i, message in enumerate(messages):
        message_type = message.get("messageType")
        if message_type == "bridge.context-bound":
            bound = message
            continue
        if message_type == "bridge.context-disposed":
            bound = None
            continue
        if bound is None or bound.get("supportsDirection") is True:
            continue
        if message_type == "bridge.command" and message.get("commandKind") == "SubmitSteeringInput":
            following = messages[i + 1] if i + 1 < len(messages) else None
            if not (
                following
                and following.get("messageType") == "bridge.refusal"
                and following.get("reason") == "capability_unavailable"
            ):
                problems.append(
                    f"message[{i}] SubmitSteeringInput arrived in a context whose supportsDirection "
                    "is not true with no immediately following capability_unavailable refusal"
                )
        elif message_type == "bridge.snapshot-page":
            for j, item in enumerate(message.get("items", [])):
                if item.get("itemKind") == "direction":
                    problems.append(
                        f"message[{i}].items[{j}] is a direction item delivered into a context whose "
                        "supportsDirection is not true"
                    )
        elif message_type == "bridge.event-batch":
            for j, event in enumerate(message.get("events", [])):
                if event.get("eventType") == "chat.direction.state_changed":
                    problems.append(
                        f"message[{i}].events[{j}] is a chat.direction.state_changed event delivered "
                        "into a context whose supportsDirection is not true"
                    )
    return _problems(problems)


@_check("direction_application_attempt_differs_from_target")
def _direction_application_attempt_differs_from_target(content: Document) -> CheckResult:
    """C6: an applied Direction's ``application.attemptId`` is the successor
    continuation Attempt that bound it; ``targetAttemptId`` is the running Attempt
    that accepted it. JSON Schema cannot compare the two members, so the package
    codec's ``validate_direction`` states the rule and this check delegates to it."""
    application = content.get("application")
    if application is not None and application.get("attemptId") == content.get("targetAttemptId"):
        return (False, "application.attemptId must differ from targetAttemptId (applied by a successor Attempt)")
    return (True, "")


@_check("direction_content_hash_matches_content")
def _direction_content_hash_matches_content(content: Document) -> CheckResult:
    """C6: ``contentHash`` is the bare SHA-256 of the UTF-8 bytes of the canonical
    ``{"editableParts": [...]}`` object. A pattern cannot recompute a hash, so the
    package codec states the rule and this check delegates to it."""
    from omnivia_core.chat_contract.v1 import codec

    try:
        codec.validate_direction_content_hash(content)
    except codec.ChatContractDecodeError as exc:
        return (False, f"contentHash {exc.rule}")
    return (True, "")


@_check("direction_content_within_canonical_bound")
def _direction_content_within_canonical_bound(content: Document) -> CheckResult:
    """C6: the canonical ``{"editableParts": [...]}`` object is at most
    ``DIRECTION_CONTENT_MAX_UTF8_BYTES`` UTF-8 bytes (the durable row cap)."""
    from omnivia_core.chat_contract.v1 import codec

    try:
        codec.validate_direction_content(content["editableParts"])
    except codec.ChatContractDecodeError as exc:
        return (False, str(exc))
    return (True, "")


@_check("snapshot_generation_projection_consistent")
def _snapshot_generation_projection_consistent(content: Document) -> CheckResult:
    """C6: ``ConversationSnapshotResult.generationJobs`` and ``directions`` are
    both required (empty when there is nothing to state); ``generationJobs`` is the complete job
    projection of the selected branch (the same set ``path.generationJobIds``
    names) and ``directions`` is the viewing actor's own, each on a listed Job
    and in the snapshot's own scope. JSON Schema cannot compare one member of a
    document to another, so the relation is checked here."""
    jobs = content.get("generationJobs")
    directions = content.get("directions")
    path, viewer = content["path"], content["viewState"]
    problems = []
    # Both members are required, even when empty: omission is never "none".
    if jobs is None:
        problems.append("generationJobs is missing (required, an empty array when the branch has no Job)")
    if directions is None:
        problems.append("directions is missing (required, an empty array when the actor has none)")
    if jobs is None or directions is None:
        return _problems(problems)
    listed = {job["jobId"] for job in jobs}
    if set(path.get("generationJobIds", [])) != listed:
        problems.append("path.generationJobIds does not equal the jobIds listed in generationJobs (partial projection)")
    for job in jobs:
        for claim in ("workspaceId", "conversationId", "branchId"):
            if job.get(claim) != path.get(claim):
                problems.append(f"generationJobs job {job['jobId']!r} {claim} does not match the snapshot path")
    for direction in directions:
        label = f"directions {direction.get('directionId')!r}"
        if direction.get("actorId") != viewer.get("actorId"):
            problems.append(f"{label} actorId does not match viewState.actorId (another actor's Direction)")
        for claim in ("workspaceId", "conversationId", "branchId"):
            if direction.get(claim) != path.get(claim):
                problems.append(f"{label} {claim} does not match the snapshot path")
        if direction.get("jobId") not in listed:
            problems.append(f"{label} jobId {direction.get('jobId')!r} is not listed in generationJobs")
        for check in (
            "direction_application_attempt_differs_from_target",
            "direction_content_hash_matches_content",
            "direction_content_within_canonical_bound",
        ):
            ok, detail = _CHECKS[check](direction)
            if not ok:
                problems.append(f"{label} {detail}")
    return _problems(problems)


@_check("f2b_no_duplicate_event_ids_across_batches")
def _f2b_no_duplicate_event_ids(content: Document) -> CheckResult:
    """The same ``eventId`` delivered in two different ``bridge.event-batch``
    messages within one trace is a duplicate, never a new fact."""
    seen: dict[str, int] = {}
    problems = []
    for i, message in enumerate(content["messages"]):
        if message.get("messageType") != "bridge.event-batch":
            continue
        for event in message.get("events", []):
            event_id = event.get("eventId")
            if event_id in seen:
                problems.append(
                    f"duplicate eventId {event_id!r} delivered in batch {i} "
                    f"(previously in batch {seen[event_id]})"
                )
            else:
                seen[event_id] = i
    return _problems(problems)


@_check("f2b_lifecycle_state_machine")
def _f2b_lifecycle_state_machine(content: Document) -> CheckResult:
    """Walks one trace through the deterministic F2b lifecycle (freeze §12.0.1)
    and fails closed on traffic not admissible in the state it arrives in:

    - **before_bind**: host data delivery is an unrefusable protocol violation.
      (Module traffic before any bind is owned by
      ``f2b_context_mismatch_refused``.)
    - **bound_not_ready**: ``bridge.snapshot-request`` is the one admissible
      module message and is the transition to readiness. A ``bridge.command`` or
      ``bridge.backpressure`` sent first must be answered with the precise
      closed reason ``not_ready``. Host data delivery before the module has
      asked for anything is a violation.
    - **ready_snapshotting / resnapshotting**: ``bridge.event-batch`` before the
      snapshot boundary has been set is a violation: the module has no position
      to apply it from.
    - **disposed**: both directions are owned by ``f2b_late_command_refused``
      and ``f2b_dispose_cleanup_no_further_host_traffic``.

    Delivery while **paused** is owned by ``f2b_backpressure_honoured``, so a
    paused context is treated as subscribed here rather than double-reported.
    """
    messages = content["messages"]
    state = "before_bind"
    problems = []
    for i, message in enumerate(messages):
        message_type = message.get("messageType")
        if message_type == "bridge.context-bound":
            state = "bound_not_ready"
            continue
        if message_type == "bridge.context-disposed":
            state = "disposed"
            continue
        if message_type == "bridge.refusal" or state == "disposed":
            continue

        if state == "before_bind":
            if message_type in _F2B_HOST_DATA_TYPES:
                problems.append(
                    f"message[{i}] ({message_type}) is host data delivery before any "
                    "bridge.context-bound admission (pre-bind host traffic is a protocol "
                    "violation, not a refusable message)"
                )
            continue

        if state == "bound_not_ready":
            if message_type == "bridge.snapshot-request":
                state = "ready_snapshotting"
            elif message_type in _F2B_MODULE_TO_HOST_TYPES:
                following = messages[i + 1] if i + 1 < len(messages) else None
                if not (
                    following
                    and following.get("messageType") == "bridge.refusal"
                    and following.get("reason") == _F2B_NOT_READY_REASON
                ):
                    problems.append(
                        f"message[{i}] ({message_type}) is pre-ready module traffic in state "
                        f"bound_not_ready with no immediately following "
                        f"{_F2B_NOT_READY_REASON} refusal"
                    )
            elif message_type in _F2B_HOST_DATA_TYPES:
                problems.append(
                    f"message[{i}] ({message_type}) is host data delivery in state "
                    "bound_not_ready, before the module opened its snapshot handshake"
                )
            continue

        # ready_snapshotting / subscribed / paused / resnapshotting
        if message_type == "bridge.snapshot-request":
            state = "ready_snapshotting"
        elif message_type == "bridge.snapshot-page":
            state = "subscribed" if message.get("hasMore") is False else "ready_snapshotting"
        elif message_type == "bridge.resnapshot":
            state = "resnapshotting"
        elif message_type == "bridge.backpressure":
            if message.get("action") == "pause":
                state = "paused"
            elif state == "paused":
                state = "subscribed"
        elif message_type == "bridge.event-batch" and state not in ("subscribed", "paused"):
            problems.append(
                f"message[{i}] (bridge.event-batch) delivered in state {state}: no snapshot "
                "boundary cursor has been set, so there is no position to apply it from"
            )
    return _problems(problems)


@_check("f2b_snapshot_paging_discipline")
def _f2b_snapshot_paging_discipline(content: Document) -> CheckResult:
    """One request per snapshot page (freeze §12.5):

    - every ``bridge.snapshot-page`` answers exactly one outstanding
      ``bridge.snapshot-request``;
    - a request carrying no ``cursor`` opens a fresh generation whose first page
      is ``pageIndex: 0`` (as does the request echoing a ``bridge.resnapshot``'s
      exact ``resnapshotCursor``);
    - a continuation request must carry the exact ``nextCursor`` the preceding
      ``hasMore: true`` page issued -- never guessed;
    - each next page of one generation keeps ``snapshotId`` and
      ``snapshotGraphRevision`` and increments ``pageIndex`` by exactly one, so
      identity drift, revision drift and page-index discontinuity are all
      caught rather than silently accepted as a new generation.
    """
    pending_cursor = None
    have_pending = False
    generation: dict[str, Any] | None = None
    fresh_cursor = None
    problems = []
    for i, message in enumerate(content["messages"]):
        message_type = message.get("messageType")
        if message_type in ("bridge.context-bound", "bridge.context-disposed"):
            have_pending, pending_cursor, generation, fresh_cursor = False, None, None, None
            continue
        if message_type == "bridge.resnapshot":
            have_pending, pending_cursor, generation = False, None, None
            fresh_cursor = message.get("resnapshotCursor")
            continue
        if message_type == "bridge.snapshot-request":
            if have_pending:
                problems.append(
                    f"message[{i}] is a second bridge.snapshot-request with a page still "
                    "outstanding (exactly one request per snapshot page)"
                )
            have_pending, pending_cursor = True, message.get("cursor")
            continue
        if message_type != "bridge.snapshot-page":
            continue

        if not have_pending:
            problems.append(
                f"message[{i}] bridge.snapshot-page was not requested by a preceding "
                "bridge.snapshot-request (exactly one request per snapshot page)"
            )
        if generation is None:
            if pending_cursor is not None and pending_cursor != fresh_cursor:
                problems.append(
                    f"message[{i}] opens a snapshot generation from continuation cursor "
                    f"{pending_cursor!r}, but no snapshot generation is in progress"
                )
            if message.get("pageIndex") != 0:
                problems.append(
                    f"message[{i}] is the first page of a snapshot generation but has pageIndex "
                    f"{message.get('pageIndex')!r}, not 0"
                )
        else:
            if pending_cursor != generation["nextCursor"]:
                problems.append(
                    f"message[{i}] continues snapshot {generation['snapshotId']!r} from cursor "
                    f"{pending_cursor!r}, but the preceding page issued nextCursor "
                    f"{generation['nextCursor']!r} (wrong or missing continuation cursor)"
                )
            if message.get("snapshotId") != generation["snapshotId"]:
                problems.append(
                    f"message[{i}] snapshotId {message.get('snapshotId')!r} drifts from the "
                    f"generation's {generation['snapshotId']!r} mid-sequence (snapshot identity "
                    "drift)"
                )
            if message.get("snapshotGraphRevision") != generation["snapshotGraphRevision"]:
                problems.append(
                    f"message[{i}] snapshotGraphRevision "
                    f"{message.get('snapshotGraphRevision')!r} drifts from the generation's "
                    f"{generation['snapshotGraphRevision']!r} mid-sequence (snapshot revision "
                    "drift)"
                )
            if message.get("pageIndex") != generation["pageIndex"] + 1:
                problems.append(
                    f"message[{i}] pageIndex {message.get('pageIndex')!r} does not increment the "
                    f"previous page's {generation['pageIndex']!r} by exactly one (page-index "
                    "discontinuity)"
                )

        have_pending, pending_cursor = False, None
        if message.get("hasMore") is True:
            generation = {
                "snapshotId": message.get("snapshotId"),
                "snapshotGraphRevision": message.get("snapshotGraphRevision"),
                "pageIndex": message.get("pageIndex"),
                "nextCursor": message.get("nextCursor"),
            }
        else:
            generation, fresh_cursor = None, None
    return _problems(problems)


@_check("f2b_negotiated_bounds_enforced")
def _f2b_negotiated_bounds_enforced(content: Document) -> CheckResult:
    """Every page/batch count and every encoded message size is measured against
    the bound context's *negotiated* ``maxSnapshotItems``/``maxBatchEvents``/
    ``maxMessageBytes``, not merely against the schema's global maxima: JSON
    Schema can express a global maximum but cannot compare one message against
    another message's declared value."""
    bound = None
    problems = []
    for i, message in enumerate(content["messages"]):
        message_type = message.get("messageType")
        if message_type == "bridge.context-bound":
            bound = message
        if bound is None:
            continue
        max_bytes = bound.get("maxMessageBytes")
        size = _serialized_byte_size(message)
        if isinstance(max_bytes, int) and size > max_bytes:
            problems.append(
                f"message[{i}] ({message_type}) encodes to {size} UTF-8 bytes, above the bound "
                f"context's negotiated maxMessageBytes {max_bytes}"
            )
        if message_type == "bridge.snapshot-page":
            limit = bound.get("maxSnapshotItems")
            count = len(message.get("items", []))
            if isinstance(limit, int) and count > limit:
                problems.append(
                    f"message[{i}] carries {count} snapshot items, above the bound context's "
                    f"negotiated maxSnapshotItems {limit}"
                )
        elif message_type == "bridge.event-batch":
            limit = bound.get("maxBatchEvents")
            count = len(message.get("events", []))
            if isinstance(limit, int) and count > limit:
                problems.append(
                    f"message[{i}] carries {count} events, above the bound context's negotiated "
                    f"maxBatchEvents {limit}"
                )
        if message_type == "bridge.context-disposed":
            bound = None
    return _problems(problems)


@_check("f2b_encoded_envelope_within_carrier_bound")
def _f2b_encoded_envelope_within_carrier_bound(content: Document) -> CheckResult:
    """Measures the *actual* UTF-8 JSON encoded size of one whole F2b carrier
    envelope against the evidenced Platform carrier's ceiling, and the inner F2b
    payload against the ceiling that same bound minus the outer-envelope
    overhead reserve yields. An envelope whose declared bounds are all in range
    but whose encoding exceeds the carrier is refused, never truncated."""
    total = _serialized_byte_size(content)
    inner = _serialized_byte_size(content.get("payload", {}))
    inner_ceiling = _F2B_PLATFORM_CARRIER_MAX_BYTES - _F2B_OUTER_ENVELOPE_OVERHEAD_RESERVE_BYTES
    problems = []
    if total > _F2B_PLATFORM_CARRIER_MAX_BYTES:
        problems.append(
            f"encoded envelope is {total} UTF-8 bytes, above the evidenced Platform carrier's "
            f"{_F2B_PLATFORM_CARRIER_MAX_BYTES}-byte MODULE_HOST_MAX_MESSAGE_BYTES ceiling"
        )
    if inner > inner_ceiling:
        problems.append(
            f"encoded F2b payload is {inner} UTF-8 bytes, above the {inner_ceiling}-byte F2b "
            f"ceiling ({_F2B_PLATFORM_CARRIER_MAX_BYTES} minus the "
            f"{_F2B_OUTER_ENVELOPE_OVERHEAD_RESERVE_BYTES}-byte outer-envelope overhead reserve)"
        )
    return _problems(problems)


# --------------------------------------------------------------------------
# Schema bundle sanity
# --------------------------------------------------------------------------


def test_exactly_thirteen_schemas_are_present() -> None:
    assert len(SCHEMAS) == 13


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_each_schema_is_a_valid_draft_2020_12_schema(name: str) -> None:
    Draft202012Validator.check_schema(SCHEMAS[name])


def test_the_manifest_declares_the_governed_counts() -> None:
    assert MANIFEST["counts"] == {"total": 270, "valid": 105, "invalid": 165}
    assert len(MANIFEST["fixtures"]) == 270


def test_every_check_the_manifest_names_is_implemented() -> None:
    """No governed fixture outcome may go unexercised because a named check is
    missing: an unimplemented name would otherwise silently weaken the gate."""
    named = {name for entry in MANIFEST["fixtures"] for name in entry.get("checks", ())}
    assert named <= set(_CHECKS), sorted(named - set(_CHECKS))
    assert set(_AUTO_PROVIDER_CHECKS) <= set(_CHECKS)


def test_no_implemented_check_is_unused() -> None:
    """The converse: every ported check is reachable from the manifest, so this
    module never grows a rule the governed bundle does not state."""
    named = {name for entry in MANIFEST["fixtures"] for name in entry.get("checks", ())}
    assert set(_CHECKS) == named | set(_AUTO_PROVIDER_CHECKS)


# --------------------------------------------------------------------------
# Fixture evaluation: schema conformance plus every declared relational check
# --------------------------------------------------------------------------


def _schema_errors(entry: dict[str, Any], document: Any) -> list[Any]:
    kind = entry.get("kind", "record")
    if kind == "trace":
        items = document.get("events") if "events" in document else document.get("messages", [])
        errors: list[Any] = []
        for item in items:
            errors.extend(_errors(entry["schema_ref"], item))
        return errors
    if kind == "relational_pair":
        errors = []
        for part_name, schema_ref in entry["partSchemaRefs"].items():
            errors.extend(_errors(schema_ref, document[part_name]))
        return errors
    return _errors(entry["schema_ref"], document)


def _declared_checks(entry: dict[str, Any]) -> tuple[str, ...]:
    auto = (
        _AUTO_PROVIDER_CHECKS
        if entry.get("kind") == "trace"
        and entry["schema_ref"].endswith("provider.schema.json#/$defs/ProviderEvent")
        else ()
    )
    return auto + tuple(entry.get("checks", ()))


def _keyword(contains: str) -> str:
    return contains.split(" ", 1)[0]


@pytest.mark.parametrize("entry", MANIFEST["fixtures"], ids=lambda entry: entry["path"])
def test_every_fixture_meets_its_governed_expectation(entry: dict[str, Any]) -> None:
    document = load(entry["path"])
    errors = _schema_errors(entry, document)
    failures = [
        (name, detail)
        for name, (ok, detail) in ((n, _CHECKS[n](document)) for n in _declared_checks(entry))
        if not ok
    ]

    if entry["expect"] == "valid":
        assert errors == [], [error.message for error in errors]
        assert failures == [], failures
        return

    assert errors or failures, f"expected a schema or relational violation for {entry['path']}"
    contains = entry.get("expected_error_contains")
    if not contains:
        return
    # `expected_error_contains` names either the responsible JSON Schema
    # keyword (`enum`, `const`, `minLength`, optionally followed by the
    # expected value, e.g. "const '1.0'") or a substring of the relational
    # check's own diagnostic; a real validator reports errors keyed by
    # keyword, so check both rather than overfitting to one implementation's
    # message wording.
    schema_match = any(
        contains == error.validator or _keyword(contains) == error.validator or contains in error.message
        for error in errors
    )
    check_match = any(contains in detail for _, detail in failures)
    assert schema_match or check_match, (
        contains,
        [error.message for error in errors],
        failures,
    )


def test_the_governed_relational_pair_fixtures_are_all_present() -> None:
    pairs = [entry for entry in MANIFEST["fixtures"] if entry.get("kind") == "relational_pair"]
    assert len(pairs) == 5
    for entry in pairs:
        assert entry["expect"] == "invalid"
        assert entry["checks"], entry["path"]


def test_the_bridge_protocol_is_3_0_while_the_outer_carrier_stays_1_0() -> None:
    """Each explicit F2b protocol change leaves the outer carrier untouched.

    C6 moved 1.1 -> 2.0 for the Direction surface; C8 moved 2.0 -> 3.0 for two
    hosted commands and the `tool_call` item. The outer Platform carrier version
    has not moved for either, which is the independence both axes claim: a
    bridge major and a carrier major move on their own.
    """
    defs = SCHEMAS["bridge.schema.json"]["$defs"]
    assert defs["SupportedBridgeProtocolVersion"]["allOf"][1] == {"const": "3.0"}
    assert defs["PlatformCarrierContractVersion"]["const"] == "1.0"
    assert defs["BridgeSnapshotItem"]["properties"]["itemKind"]["enum"][-2:] == ["direction", "tool_call"]


# --------------------------------------------------------------------------
# C6 Direction surface: structure the manifest fixtures cannot state alone
# --------------------------------------------------------------------------


def _def(schema: str, name: str) -> dict[str, Any]:
    return SCHEMAS[f"{schema}.schema.json"]["$defs"][name]


def test_steering_has_a_dedicated_request_and_leaves_the_generic_referenced_work_shape() -> None:
    generic = _def("commands", "ReferencedWorkCommandRequest")["properties"]["commandName"]["enum"]
    assert "SubmitSteeringInput" not in generic
    assert len(generic) == 7
    request = _def("commands", "SubmitSteeringInputRequest")
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {
        "actorId", "attachmentReferences", "commandId", "contextReferences", "conversationId", "editableParts",
        "expectedActiveState", "expectedGenerationVersion", "protocolVersion", "requestedBoundary",
        "targetGenerationJobId", "workspaceId",
    }
    assert request["properties"]["expectedActiveState"] == {"const": "running"}
    assert request["properties"]["attachmentReferences"]["maxItems"] == 0
    assert request["properties"]["contextReferences"]["maxItems"] == 0
    assert "SubmitSteeringInput" in _def("commands", "ChatCommandName")["enum"]


def test_the_hosted_command_allowlist_binds_steering_to_the_dedicated_request_only() -> None:
    assert "SubmitSteeringInput" in _def("bridge", "HostedCommandKind")["enum"]
    bindings = {
        branch["if"]["properties"]["commandKind"]["const"]: branch["then"]["properties"]["payload"]["$ref"]
        for branch in _def("bridge", "BridgeCommandMessage")["allOf"]
    }
    assert bindings["SubmitSteeringInput"].endswith("commands.schema.json#/$defs/SubmitSteeringInputRequest")
    assert set(bindings) == set(_def("bridge", "HostedCommandKind")["enum"]), "the binding is total"


def test_direction_enumerations_are_the_governed_closed_vocabularies() -> None:
    assert _def("common", "DirectionState")["enum"] == ["pending", "applied", "superseded", "rejected", "cancelled"]
    assert _def("common", "RequestedDirectionBoundary")["const"] == "earliest_safe"
    assert _def("common", "EffectiveDirectionBoundary")["const"] == "next_iteration"
    for retired in ("DirectionRequestedBoundary", "DirectionEffectiveBoundary"):
        assert retired not in SCHEMAS["common.schema.json"]["$defs"]
    assert {"chat.direction.state_changed", "chat.generation.iteration_completed"} <= set(
        _def("common", "ChatEventType")["enum"]
    )


def test_the_direction_state_changed_event_is_identity_only() -> None:
    event = _def("events", "DirectionStateChangedEvent")
    assert event["additionalProperties"] is False
    authored = {"editableParts", "text", "textDelta", "contentHash", "terminalReason", "safeMessage", "payload"}
    assert authored.isdisjoint(event["properties"])
    assert event["properties"]["eventType"] == {"const": "chat.direction.state_changed"}


def test_the_snapshot_projections_are_required_and_bounded() -> None:
    snapshot = _def("queries", "ConversationSnapshotResult")
    assert set(snapshot["required"]) == {"conversation", "directions", "generationJobs", "path", "viewState"}
    for name, target in (("generationJobs", "GenerationJob"), ("directions", "Direction")):
        member = snapshot["properties"][name]
        assert member["type"] == "array" and member["maxItems"] == 200
        assert member["items"]["$ref"].endswith(f"generation.schema.json#/$defs/{target}")
    assert "version" in _def("generation", "GenerationJob")["required"]


def test_the_refusal_vocabulary_keeps_capability_unavailable_distinct_from_forbidden_capability() -> None:
    reasons = _def("bridge", "BridgeRefusalReason")["enum"]
    assert "capability_unavailable" in reasons and "forbidden_capability" in reasons
    copies = _def("bridge", "RefusalSafeMessage")["enum"]
    assert len(copies) == len(reasons) == 9
    bindings = {
        branch["if"]["properties"]["reason"]["const"]: branch["then"]["properties"]["safeMessage"]["const"]
        for branch in _def("bridge", "RefusalMessage")["allOf"]
    }
    assert set(bindings) == set(reasons) and set(bindings.values()) == set(copies), "total and bijective"
    assert bindings["capability_unavailable"] != bindings["forbidden_capability"]


def test_supports_direction_is_an_optional_strict_boolean_and_durable_follow_up_is_preserved() -> None:
    properties = _def("bridge", "ContextBoundMessage")["properties"]
    required = _def("bridge", "ContextBoundMessage")["required"]
    for name in ("supportsDirection", "acceptsDurableFollowUp"):
        assert properties[name]["type"] == "boolean"
        assert name not in required


def test_capability_absent_false_and_true_are_each_valid_and_a_malformed_value_is_not() -> None:
    base = load("fixtures/valid/f2b-context-bound.json")
    ref = "https://contracts.omnivia.dev/chat/v1/bridge.schema.json#/$defs/ContextBoundMessage"
    assert "supportsDirection" not in base and _errors(ref, base) == []
    for value in (True, False):
        assert _errors(ref, {**base, "supportsDirection": value}) == []
    for value in ("true", "yes", 1, 0, None, [], {}):
        assert _errors(ref, {**base, "supportsDirection": value}) != [], value


def test_only_bridge_protocol_3_0_is_accepted() -> None:
    """Every neighbouring version is refused, including the one just superseded.

    `2.0` is in this list BECAUSE it used to be the supported one: a major move
    that kept silently admitting its predecessor would be a downgrade rather
    than a version change, which is exactly what
    `SupportedBridgeProtocolVersion` says a strict decoder must never do.
    """
    ref = "https://contracts.omnivia.dev/chat/v1/bridge.schema.json#/$defs/ContextBoundMessage"
    base = load("fixtures/valid/f2b-context-bound.json")
    assert _errors(ref, base) == []
    for version in ("1.0", "1.1", "2.0", "2.1", "3.1", "4.0", "3", "3.0.0"):
        assert _errors(ref, {**base, "bridgeProtocolVersion": version}) != [], version


_GEN = "https://contracts.omnivia.dev/chat/v1/generation.schema.json#/$defs/"
_CMD = "https://contracts.omnivia.dev/chat/v1/commands.schema.json#/$defs/"
_QRY = "https://contracts.omnivia.dev/chat/v1/queries.schema.json#/$defs/"


def test_applied_evidence_is_complete_and_closed() -> None:
    application = _def("generation", "DirectionApplication")
    assert application["additionalProperties"] is False
    assert set(application["required"]) == {
        "appliedAt", "attemptId", "iteration", "providerInvocationId", "requestManifestId",
    }
    assert application["properties"]["requestManifestId"]["$ref"].endswith("common.schema.json#/$defs/RequestManifestId")
    assert _def("common", "RequestManifestId")["$ref"] == "#/$defs/WorkspaceScopedId"
    for phrase in ("committed request manifest", "identity, version and content digest"):
        assert phrase in application["description"]
    applied = load("fixtures/valid/direction-applied.json")
    assert _errors(_GEN + "Direction", applied) == []
    for member in application["required"]:
        broken = {**applied, "application": {k: v for k, v in applied["application"].items() if k != member}}
        assert _errors(_GEN + "Direction", broken) != [], member
    for value in (7, None, "", [], {}, "bad id!"):
        broken = {**applied, "application": {**applied["application"], "requestManifestId": value}}
        assert _errors(_GEN + "Direction", broken) != [], value
    extra = {**applied, "application": {**applied["application"], "note": "x"}}
    assert _errors(_GEN + "Direction", extra) != []


def test_an_applied_effective_boundary_is_next_iteration_only() -> None:
    applied = load("fixtures/valid/direction-applied.json")
    assert applied["effectiveBoundary"] == "next_iteration"
    for value in ("earliest_safe", "immediate", None):
        assert _errors(_GEN + "Direction", {**applied, "effectiveBoundary": value}) != [], value
    request = load("fixtures/valid/submit-steering-input-request.json")
    assert request["requestedBoundary"] == "earliest_safe"
    assert _errors(_CMD + "SubmitSteeringInputRequest", request) == []
    assert _errors(_CMD + "SubmitSteeringInputRequest", {**request, "requestedBoundary": "next_iteration"}) != []


def test_an_applied_direction_is_applied_by_a_successor_attempt() -> None:
    """``targetAttemptId`` accepted the Direction; ``application.attemptId`` is the
    successor continuation Attempt whose committed request manifest binds it."""
    applied = load("fixtures/valid/direction-applied.json")
    assert applied["application"]["attemptId"] != applied["targetAttemptId"]
    assert _CHECKS["direction_application_attempt_differs_from_target"](applied)[0]
    same = load("fixtures/invalid/direction-applied-same-attempt-rejected.json")
    assert _errors(_GEN + "Direction", same) == [], "schema-valid: only the relational rule rejects it"
    ok, detail = _CHECKS["direction_application_attempt_differs_from_target"](same)
    assert not ok and "must differ" in detail
    for text in (_def("generation", "DirectionApplication")["description"], _def("common", "EffectiveDirectionBoundary")["description"]):
        assert "successor" in text and "targetAttemptId" in text


def test_the_iteration_completed_job_version_is_required_immutable_event_evidence() -> None:
    event = _def("events", "GenerationIterationCompletedEvent")
    assert "generationJobVersion" in event["required"]
    for phrase in ("REQUIRED immutable event-time evidence", "never be reconstructed from a later, mutable Job status projection"):
        assert phrase in event["description"]
    valid = load("fixtures/valid/generation-iteration-completed-event.json")
    assert _errors("https://contracts.omnivia.dev/chat/v1/events.schema.json#/$defs/GenerationIterationCompletedEvent", valid) == []


def test_the_direction_content_hash_is_the_stored_object_digest_in_two_representations() -> None:
    """One byte basis, two spellings: the public bare 64-hex ``contentHash`` and the
    storage / request-manifest ``sha256:<hex>`` are the SHA-256 of the same UTF-8
    bytes -- the canonical stored ``content_json`` object -- differing only by prefix."""
    import hashlib

    from omnivia_core.chat_contract.v1 import codec

    direction = load("fixtures/valid/direction-pending.json")
    stored = codec.to_canonical_json({"editableParts": direction["editableParts"]})
    assert stored == (
        '{"editableParts":[{"payload":{"text":"Please focus the answer on the cost section."},"type":"text"}]}'
    )
    assert len(stored.encode("utf-8")) <= 16384
    bare = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    assert direction["contentHash"] == bare == codec.direction_content_hash(direction["editableParts"])
    assert _errors(_GEN + "Direction", direction) == []
    codec.validate_direction(direction)
    migration_digest = f"sha256:{bare}"  # content_digest / request-manifest contentDigest
    assert migration_digest.removeprefix("sha256:") == direction["contentHash"]
    for description in (_def("generation", "Direction")["properties"]["contentHash"]["description"],):
        for phrase in ("to_canonical_json", "content_json", "sha256:<hex>", "byte basis is identical"):
            assert phrase in description


def test_a_structurally_valid_direction_with_a_wrong_content_hash_is_refused() -> None:
    from omnivia_core.chat_contract.v1 import codec

    document = load("fixtures/invalid/direction-content-hash-mismatch-rejected.json")
    assert _errors(_GEN + "Direction", document) == [], "well-formed hex: only the codec can refuse it"
    with pytest.raises(codec.ChatContractDecodeError) as raised:
        codec.validate_direction(document)
    assert raised.value.path == "contentHash" and "does not match" in raised.value.rule
    assert "cost section" not in str(raised.value), "the diagnostic never reproduces authored text"
    good = load("fixtures/valid/direction-pending.json")
    assert document["contentHash"] != good["contentHash"] and document["editableParts"] == good["editableParts"]


def test_direction_bounds_are_honest_against_the_durable_row() -> None:
    from omnivia_core.chat_contract.v1 import generated

    assert generated.DIRECTION_CONTENT_MAX_UTF8_BYTES == 16384
    text = _def("common", "DirectionTextPart")["properties"]["payload"]["properties"]["text"]
    assert text["maxLength"] == 4000
    for schema in (_def("generation", "Direction"), _def("commands", "SubmitSteeringInputRequest")):
        parts = schema["properties"]["editableParts"]
        assert parts["maxItems"] == 16
        assert "16384 UTF-8 bytes" in parts["description"] and "validate_direction_content" in parts["description"]
    request = load("fixtures/valid/submit-steering-input-request.json")
    ref = _CMD + "SubmitSteeringInputRequest"
    assert _errors(ref, {**request, "editableParts": [{"type": "text", "payload": {"text": "x" * 4000}}]}) == []
    assert _errors(ref, {**request, "editableParts": [{"type": "text", "payload": {"text": "x" * 4001}}]}) != []
    part = {"type": "text", "payload": {"text": "x"}}
    assert _errors(ref, {**request, "editableParts": [part] * 16}) == []
    assert _errors(ref, {**request, "editableParts": [part] * 17}) != []


def test_the_d07_schema_closes_the_wire_version_to_exactly_2_0() -> None:
    """The schema itself, not only the codec negotiation helper, refuses every
    other well-formed version -- on every D07 command and result carrying it."""
    syntax = _def("common", "ProtocolVersionSyntax")
    assert "pattern" in syntax and "const" not in syntax
    exact = _def("common", "ProtocolVersion")
    assert exact["allOf"][1] == {"const": "2.0"}
    request = load("fixtures/valid/submit-steering-input-request.json")
    result = load("fixtures/valid/direction-submitted-result.json")
    for ref, document in ((_CMD + "SubmitSteeringInputRequest", request), (_CMD + "DirectionSubmittedResult", result)):
        assert _errors(ref, document) == []
        for version in ("1.0", "1.1", "2.1", "3.0", "02.0", "2", "", None, 2.0):
            assert _errors(ref, {**document, "protocolVersion": version}) != [], (ref, version)


def test_direction_content_is_structurally_text_only() -> None:
    part = _def("common", "DirectionTextPart")
    assert part["additionalProperties"] is False and part["properties"]["payload"]["additionalProperties"] is False
    for schema in (_def("generation", "Direction"), _def("commands", "SubmitSteeringInputRequest")):
        items = schema["properties"]["editableParts"]
        assert items["items"]["$ref"].endswith("common.schema.json#/$defs/DirectionTextPart")
        assert items["minItems"] == 1 and items["maxItems"] == 16
    request = load("fixtures/valid/submit-steering-input-request.json")
    ref = _CMD + "SubmitSteeringInputRequest"
    good = {"type": "text", "payload": {"text": "Focus on cost."}}
    assert _errors(ref, {**request, "editableParts": [good]}) == []
    bad_parts = [
        {"type": "image", "payload": {"url": "https://example.invalid/x.png"}},
        {"type": "image", "payload": {"text": "x"}},
        {"type": "markdown", "payload": {"text": "x"}},
        {"type": "text", "payload": {"text": "x", "url": "https://example.invalid/x"}},
        {"type": "text", "payload": {"url": "https://example.invalid/x"}},
        {"type": "text", "payload": {"text": ""}},
        {"type": "text", "payload": {"text": "x" * 4001}},
        {"type": "text", "payload": {"text": "x"}, "visibility": "standard"},
        {"type": "text", "payload": {"text": "x"}, "extra": 1},
        {"type": "text", "payload": {}},
        {"type": "text"},
        {"payload": {"text": "x"}},
    ]
    for bad in bad_parts:
        assert _errors(ref, {**request, "editableParts": [bad]}) != [], bad
        assert _errors(_GEN + "Direction", {**load("fixtures/valid/direction-pending.json"), "editableParts": [bad]}) != [], bad
    assert _errors(ref, {**request, "editableParts": [good, bad_parts[0]]}) != []
    assert _errors(ref, {**request, "editableParts": []}) != []


def test_a_submitted_direction_is_exactly_pending() -> None:
    assert _def("commands", "DirectionSubmittedResult")["properties"]["state"] == {"const": "pending"}
    result = load("fixtures/valid/direction-submitted-result.json")
    assert result["state"] == "pending" and _errors(_CMD + "DirectionSubmittedResult", result) == []
    for state in ("applied", "superseded", "rejected", "cancelled", "Pending", None):
        assert _errors(_CMD + "DirectionSubmittedResult", {**result, "state": state}) != [], state


def test_generation_job_version_is_positive_and_one_is_the_initial_token() -> None:
    version = _def("common", "GenerationJobVersion")
    assert version["minimum"] == 1 and "initial authoritative token is `1`" in version["description"]
    job = load("fixtures/valid/generation-job-running-versioned.json")
    for good in (1, 2, 9007199254740991):
        assert _errors(_GEN + "GenerationJob", {**job, "version": good}) == [], good
    for bad in (0, -1, 9007199254740992, 1.5, "1", None):
        assert _errors(_GEN + "GenerationJob", {**job, "version": bad}) != [], bad
    assert _errors(_GEN + "GenerationJob", {k: v for k, v in job.items() if k != "version"}) != []


def test_a_snapshot_must_state_jobs_and_directions_even_when_empty() -> None:
    snapshot = load("fixtures/valid/snapshot-composer-drafts-present.json")
    assert snapshot["generationJobs"] == [] and snapshot["directions"] == []
    assert _errors(_QRY + "ConversationSnapshotResult", snapshot) == []
    assert _CHECKS["snapshot_generation_projection_consistent"](snapshot)[0]
    for member in ("generationJobs", "directions"):
        omitted = {k: v for k, v in snapshot.items() if k != member}
        assert _errors(_QRY + "ConversationSnapshotResult", omitted) != [], member
        ok, detail = _CHECKS["snapshot_generation_projection_consistent"](omitted)
        assert not ok and member in detail, member
    full = load("fixtures/valid/snapshot-generation-jobs-and-directions.json")
    assert _errors(_QRY + "ConversationSnapshotResult", full) == []


def test_the_d07_wire_protocol_is_2_0_because_job_version_became_required() -> None:
    from omnivia_core.chat_contract.v1 import generated

    assert (generated.PROTOCOL_VERSION, generated.PROTOCOL_MAJOR) == ("2.0", "2")
    # THREE AXES, and this is where their independence stops being a claim in a
    # description and becomes a fact: D07's wire version is 2.0, the F2b bridge
    # is 3.0 (C8 moved it), and the outer Platform carrier is 1.0. They were all
    # "2.0" and "1.0" before C8, so the assertion could not tell independence
    # from coincidence; now it can.
    assert generated.BRIDGE_PROTOCOL_VERSION == "3.0", "the bridge moved for C8; D07 did not"
    assert generated.PLATFORM_CARRIER_CONTRACT_VERSION == "1.0", "the outer carrier is an independent axis"
    assert "version" in _def("generation", "GenerationJob")["required"]
    assert all(
        load(path)["protocolVersion"] == "2.0"
        for path in (
            "fixtures/valid/submit-steering-input-request.json",
            "fixtures/valid/direction-submitted-result.json",
        )
    )
