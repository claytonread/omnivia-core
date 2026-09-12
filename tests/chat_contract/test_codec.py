"""Unit tests for the bounded Chat Runtime Contract v1 W2/F2 codec.

Two directions are held apart deliberately and tested apart: a *tolerant*
decode admits an unknown additive optional field and an unknown
``ChatEvent.eventType`` and holds both inert, while *strict emission* -- direct
dataclass construction and every ``to_wire`` path -- refuses to produce a
document this version does not govern.

The strict-emission direction is tested against an *oracle*, not against the
codec's own opinion: ``jsonschema`` evaluating the real
``contracts/chat/v1/schemas`` bundle. Every invalid case in
:data:`_INVALID_EMISSIONS` asserts both halves -- the canonical ``$def``
rejects the document, and direct construction of the record that would emit it
raises -- so a case can never pass by the codec rejecting something the
contract actually permits, nor by the corpus drifting into documents the schema
allows. ``jsonschema``/``referencing`` are development-only: the codec itself is
standard library only and never imports them.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from omnivia_core.chat_contract.v1 import codec
from omnivia_core.chat_contract.v1.generated import (
    CHAT_EVENT_FIELDS,
    CHAT_EVENT_REQUIRED_FIELDS,
    CHAT_EVENT_TYPES,
    ERROR_CODES,
    PROTOCOL_VERSION,
    RECORD_VALIDATION_REFS,
    SCHEMA_IDS,
)

SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "contracts" / "chat" / "v1" / "schemas"
_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
    for schema in (
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(SCHEMAS_DIR.glob("*.schema.json"))
    )
)


def _canonical_ref(record: str) -> str:
    """The absolute ``$ref`` of the exact canonical subschema ``record`` emits.

    Derived from the generated ``RECORD_VALIDATION_REFS`` rather than written
    out here, so the oracle can only ever be pointed at the same subschema the
    codec validates against.
    """
    file_name, _, pointer = RECORD_VALIDATION_REFS[record].partition("#")
    return f"{SCHEMA_IDS[file_name.removesuffix('.schema.json')]}#{pointer}"


def _oracle_errors(record: str, document: Any) -> list[str]:
    """Every diagnostic the canonical schema raises against ``document``."""
    validator = Draft202012Validator(
        {"$ref": _canonical_ref(record)},
        registry=_REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    return [error.message for error in validator.iter_errors(document)]

# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_uses_compact_separators() -> None:
    assert codec.to_canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


@pytest.mark.parametrize("serialize", [codec.to_canonical_json, codec.to_canonical_json_document])
@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("nan", {"x": float("nan")}),
        ("infinity", {"x": float("inf")}),
        ("negative-infinity", {"x": [{"deep": float("-inf")}]}),
        ("integer-key", {"a": {1: "x"}}),
        ("integer-key-at-the-root", {1: "x"}),
    ],
)
def test_the_canonical_serializers_refuse_a_value_outside_the_json_data_model(
    serialize: Any, case: str, payload: dict[Any, Any]
) -> None:
    """``json.dumps`` would stringify a non-string key into a document nobody
    wrote, and ``sort_keys`` over mixed key types raises a bare ``TypeError``.
    Both are the codec's own path+rule refusal instead."""
    with pytest.raises(codec.ChatContractDecodeError):
        serialize(payload)


def test_canonical_json_document_is_indented_and_newline_terminated() -> None:
    text = codec.to_canonical_json_document({"a": 1})
    assert text == '{\n  "a": 1\n}\n'


def test_encoding_a_value_twice_produces_identical_bytes() -> None:
    payload = {"z": [1, 2, 3], "a": {"nested": True}}
    assert codec.to_canonical_json(payload) == codec.to_canonical_json(dict(reversed(payload.items())))


# --------------------------------------------------------------------------
# One-version negotiation
# --------------------------------------------------------------------------


def test_negotiate_accepts_the_exact_supported_version() -> None:
    assert codec.negotiate_protocol_version("1.0") == PROTOCOL_VERSION


def test_negotiate_rejects_an_unsupported_major() -> None:
    with pytest.raises(codec.UnsupportedProtocolVersionError):
        codec.negotiate_protocol_version("2.0")


def test_negotiate_rejects_an_unsupported_minor_of_the_supported_major() -> None:
    with pytest.raises(codec.UnsupportedProtocolVersionError):
        codec.negotiate_protocol_version("1.1")


def test_negotiate_rejects_a_malformed_version_string() -> None:
    with pytest.raises(codec.UnsupportedProtocolVersionError):
        codec.negotiate_protocol_version("not-a-version")


def test_negotiate_never_downgrades_silently() -> None:
    """A rejected negotiation never returns a value; it always raises."""
    with pytest.raises(codec.UnsupportedProtocolVersionError):
        codec.negotiate_protocol_version("0.9")


# --------------------------------------------------------------------------
# CommandResultEnvelope
# --------------------------------------------------------------------------


def test_accepted_envelope_round_trips() -> None:
    envelope = codec.CommandResultEnvelope(command_id="cmd-1", status="accepted")
    wire = envelope.to_wire()
    assert wire == {"commandId": "cmd-1", "status": "accepted"}
    assert codec.CommandResultEnvelope.from_wire(wire) == envelope


def test_rejected_envelope_requires_error() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandResultEnvelope(command_id="cmd-1", status="rejected")


def test_accepted_envelope_forbids_error() -> None:
    error = codec.CommandError(code="internal_recoverable", message="try again")
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandResultEnvelope(command_id="cmd-1", status="accepted", error=error)


def test_conflict_envelope_permits_optional_error_and_current_version() -> None:
    envelope = codec.CommandResultEnvelope(
        command_id="cmd-1", status="conflict", current_version=3
    )
    assert envelope.to_wire()["currentVersion"] == 3


def test_error_code_must_be_from_the_governed_registry() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandError.from_wire(
            {"code": "totally_made_up_code", "message": "x"}, strict=False, path="error"
        )


def test_every_error_code_constructs() -> None:
    for code in ERROR_CODES:
        codec.CommandError(code=code, message="ok")


def test_negative_current_version_is_rejected() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandResultEnvelope.from_wire(
            {"commandId": "cmd-1", "status": "conflict", "currentVersion": -1}
        )


def test_strict_decode_rejects_an_unknown_field() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandResultEnvelope.from_wire(
            {"commandId": "cmd-1", "status": "accepted", "unknownField": True}, strict=True
        )


def test_compatible_decode_ignores_an_unknown_field() -> None:
    envelope = codec.CommandResultEnvelope.from_wire(
        {"commandId": "cmd-1", "status": "accepted", "unknownField": True}, strict=False
    )
    assert envelope.command_id == "cmd-1"


def test_a_tolerated_unknown_envelope_field_is_never_re_emitted() -> None:
    """An additive field this version does not govern stays inert: accepted on
    the way in, absent on the way out, so it can never travel on as authority."""
    envelope = codec.CommandResultEnvelope.from_wire(
        {"commandId": "cmd-1", "status": "accepted", "unknownField": True}, strict=False
    )
    assert envelope.to_wire() == {"commandId": "cmd-1", "status": "accepted"}


def test_direct_construction_rejects_an_ungoverned_error_code() -> None:
    """Construction, not just decoding, is the guard: an invalid dataclass can
    never exist to be emitted later."""
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandError(code="totally_made_up_code", message="x")


def test_direct_construction_rejects_an_empty_command_id() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandResultEnvelope(command_id="", status="accepted")


def test_direct_construction_rejects_a_negative_current_version() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.CommandResultEnvelope(command_id="cmd-1", status="conflict", current_version=-1)


# --------------------------------------------------------------------------
# ChatEvent / ResnapshotResponse
# --------------------------------------------------------------------------

#: The `chat.message.committed` fields that are specific to that event type.
_BASE_EVENT_SPECIFIC: dict[str, Any] = {
    "messageId": "msg-1",
    "role": "user",
    "graphRevision": 3,
    "conversationSequence": 1,
}

_BASE_EVENT: dict[str, Any] = {
    "eventId": "evt-1",
    "eventType": "chat.message.committed",
    "schemaVersion": 1,
    "workspaceId": "ws-1",
    "conversationId": "conv-1",
    "occurredAt": "2026-01-01T00:00:00Z",
    "cursor": "cursor-1",
    **_BASE_EVENT_SPECIFIC,
}


def test_known_event_type_decodes_with_type_specific_fields_preserved() -> None:
    event = codec.ChatEvent.from_wire(_BASE_EVENT)
    assert event.is_known_event_type is True
    assert event.fields["messageId"] == "msg-1"
    assert isinstance(event.fields, MappingProxyType)


def test_chat_event_round_trips_to_wire() -> None:
    event = codec.ChatEvent.from_wire(_BASE_EVENT)
    assert event.to_wire() == _BASE_EVENT


def test_unrecognised_event_type_is_preserved_as_additive_not_rejected() -> None:
    payload = {**_BASE_EVENT, "eventType": "chat.something.future"}
    event = codec.ChatEvent.from_wire(payload)
    assert event.is_known_event_type is False
    assert event.event_type == "chat.something.future"
    assert event.cursor == "cursor-1", "the cursor position survives an additive decode"


def test_chat_event_fields_are_immutable() -> None:
    event = codec.ChatEvent.from_wire(_BASE_EVENT)
    with pytest.raises(TypeError):
        event.fields["messageId"] = "different"  # type: ignore[index]


def test_chat_event_freezes_fields_passed_to_the_constructor_directly() -> None:
    """A caller that keeps a reference to the dict it constructed with cannot
    mutate the event afterwards."""
    mutable: dict[str, Any] = {
        "messageId": "msg-1",
        "role": "user",
        "graphRevision": 3,
        "conversationSequence": 1,
    }
    event = codec.ChatEvent(
        event_id="evt-1",
        event_type="chat.message.committed",
        schema_version=1,
        workspace_id="ws-1",
        conversation_id="conv-1",
        occurred_at="2026-01-01T00:00:00Z",
        cursor="cursor-1",
        fields=mutable,
    )
    mutable["messageId"] = "tampered"
    assert event.fields["messageId"] == "msg-1"
    with pytest.raises(TypeError):
        event.fields["messageId"] = "tampered"  # type: ignore[index]


# -- Strict emission of an event this version does govern -------------------


def test_the_generated_event_metadata_covers_every_governed_event_type() -> None:
    """The codec's strict emission is only as complete as what the generator
    derived from the schemas; if these drift, an event type would silently lose
    its shape."""
    assert set(CHAT_EVENT_FIELDS) == set(CHAT_EVENT_TYPES)
    assert set(CHAT_EVENT_REQUIRED_FIELDS) == set(CHAT_EVENT_TYPES)
    for event_type in CHAT_EVENT_TYPES:
        assert set(CHAT_EVENT_REQUIRED_FIELDS[event_type]) <= set(CHAT_EVENT_FIELDS[event_type])


def test_the_envelope_field_set_is_exactly_what_every_event_type_requires() -> None:
    """``ChatEvent``'s seven envelope attributes are declared explicitly because
    they are dataclass fields, so this pins them to the generated truth: were a
    schema change to drop one from some event type's ``required``, the codec
    would otherwise keep treating it as universal."""
    intersection = frozenset.intersection(
        *(frozenset(required) for required in CHAT_EVENT_REQUIRED_FIELDS.values())
    )
    assert codec._CHAT_EVENT_COMMON_FIELDS == intersection


def test_a_known_event_may_not_carry_a_field_that_event_type_does_not_define() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent(
            event_id="evt-1",
            event_type="chat.message.committed",
            schema_version=1,
            workspace_id="ws-1",
            conversation_id="conv-1",
            occurred_at="2026-01-01T00:00:00Z",
            cursor="cursor-1",
            fields={**_BASE_EVENT_SPECIFIC, "generationJobId": "job-1"},
        )


def test_a_known_event_must_carry_that_event_types_required_fields() -> None:
    missing_role = {k: v for k, v in _BASE_EVENT_SPECIFIC.items() if k != "role"}
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent(
            event_id="evt-1",
            event_type="chat.message.committed",
            schema_version=1,
            workspace_id="ws-1",
            conversation_id="conv-1",
            occurred_at="2026-01-01T00:00:00Z",
            cursor="cursor-1",
            fields=missing_role,
        )


def test_an_envelope_field_may_not_be_restated_as_a_type_specific_field() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent(
            event_id="evt-1",
            event_type="chat.message.committed",
            schema_version=1,
            workspace_id="ws-1",
            conversation_id="conv-1",
            occurred_at="2026-01-01T00:00:00Z",
            cursor="cursor-1",
            fields={**_BASE_EVENT_SPECIFIC, "workspaceId": "ws-other"},
        )


def test_chat_event_rejects_an_out_of_range_schema_version() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent.from_wire({**_BASE_EVENT, "schemaVersion": 0})


def test_strict_emission_refuses_an_event_type_this_version_does_not_define() -> None:
    """The additive decode keeps the cursor; it never licenses re-emitting an
    event whose meaning this version cannot state (freeze §9 rules 5-6)."""
    event = codec.ChatEvent.from_wire({**_BASE_EVENT, "eventType": "chat.something.future"})
    with pytest.raises(codec.ChatContractDecodeError, match="eventType"):
        event.to_wire()


def test_an_unrecognised_event_types_fields_are_inert_not_governed() -> None:
    event = codec.ChatEvent.from_wire({**_BASE_EVENT, "eventType": "chat.something.future"})
    assert event.fields == {}
    assert event.additive_fields["messageId"] == "msg-1"


def test_a_tolerated_additive_field_on_a_known_event_is_never_re_emitted() -> None:
    event = codec.ChatEvent.from_wire({**_BASE_EVENT, "futureField": "ignored"})
    assert "futureField" not in event.fields
    assert event.additive_fields["futureField"] == "ignored"
    assert event.to_wire() == _BASE_EVENT


def test_strict_decode_rejects_an_unrecognised_event_type() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent.from_wire({**_BASE_EVENT, "eventType": "chat.something.future"}, strict=True)


def test_strict_decode_rejects_a_field_the_event_type_does_not_define() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent.from_wire({**_BASE_EVENT, "futureField": "ignored"}, strict=True)


def test_resnapshot_response_round_trips() -> None:
    response = codec.ResnapshotResponse(
        workspace_id="ws-1",
        conversation_id="conv-1",
        reason="gap_detected",
        graph_revision=42,
        resnapshot_cursor="cursor-2",
    )
    assert codec.ResnapshotResponse.from_wire(response.to_wire()) == response


def test_resnapshot_response_reason_is_closed() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ResnapshotResponse(
            workspace_id="ws-1",
            conversation_id="conv-1",
            reason="made_up_reason",
            graph_revision=0,
            resnapshot_cursor="cursor-2",
        )


def test_unrecognised_event_type_signals_the_resnapshot_reason_it_requires() -> None:
    payload = {**_BASE_EVENT, "eventType": "chat.something.future"}
    event = codec.ChatEvent.from_wire(payload)
    assert not event.is_known_event_type
    # The caller acts on the additive-decode policy: an unknown eventType
    # requires exactly the 'unrecognised_event_type' resnapshot reason.
    response = codec.ResnapshotResponse(
        workspace_id=event.workspace_id,
        conversation_id=event.conversation_id,
        reason="unrecognised_event_type",
        graph_revision=0,
        resnapshot_cursor=event.cursor,
    )
    assert response.reason == "unrecognised_event_type"


# --------------------------------------------------------------------------
# F2a: ProviderInvocationRequest / ProviderInvocationRecord
# --------------------------------------------------------------------------

_REQUEST_PAYLOAD = {
    "invocationId": "inv-1",
    "workspaceId": "ws-1",
    "conversationId": "conv-1",
    "jobId": "job-1",
    "attemptId": "att-1",
    "connectionId": "conn-1",
    "modelId": "anthropic/claude-sonnet-5",
    "operation": "language.stream",
    "messages": [{"role": "user", "parts": [{"kind": "text", "text": "hi"}]}],
    "responseFormat": {"kind": "text"},
    "policyRef": "pol-1",
    "classificationRef": "cls-1",
    "residencyRef": "res-1",
    "idempotencyKey": "idem-1",
    "correlationId": "corr-1",
    "deadlineAt": "2026-01-01T00:00:00Z",
    "requestedAt": "2026-01-01T00:00:00Z",
}


def test_provider_invocation_request_round_trips() -> None:
    request = codec.ProviderInvocationRequest.from_wire(_REQUEST_PAYLOAD)
    assert request.to_wire() == _REQUEST_PAYLOAD


def test_provider_invocation_request_rejects_a_non_streaming_operation() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRequest.from_wire({**_REQUEST_PAYLOAD, "operation": "language.batch"})


def test_provider_invocation_request_rejects_an_empty_messages_array() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRequest.from_wire({**_REQUEST_PAYLOAD, "messages": []})


def test_provider_invocation_request_treats_classification_and_residency_as_opaque() -> None:
    request = codec.ProviderInvocationRequest.from_wire(_REQUEST_PAYLOAD)
    assert request.classification_ref == "cls-1"
    assert request.residency_ref == "res-1"
    assert request.policy_ref == "pol-1"


@pytest.mark.parametrize(
    ("name", "malformed"),
    [
        ("tools", "not-an-array"),
        ("tools", [{"name": "ok"}, "not-an-object"]),
        ("toolChoice", "not-an-object"),
        ("generationOptions", 5),
        ("providerOptionsByNamespace", ["not-an-object"]),
        ("messages", [{"role": "user"}, 7]),
        ("responseFormat", "not-an-object"),
    ],
)
def test_a_present_field_of_the_wrong_shape_is_rejected_not_dropped(name: str, malformed: Any) -> None:
    """Presence, never shape, decides whether an optional field is decoded.
    Silently dropping a malformed ``tools`` array would turn a tool-enabled
    request into a tool-free one the caller never asked for."""
    with pytest.raises(codec.ChatContractDecodeError, match=name):
        codec.ProviderInvocationRequest.from_wire({**_REQUEST_PAYLOAD, name: malformed})


def test_provider_invocation_request_freezes_nested_structures_on_construction() -> None:
    request = codec.ProviderInvocationRequest.from_wire(_REQUEST_PAYLOAD)
    with pytest.raises(TypeError):
        request.response_format["kind"] = "json"  # type: ignore[index]


_RECORD_REQUESTED = {
    "invocationId": "inv-1",
    "workspaceId": "ws-1",
    "conversationId": "conv-1",
    "jobId": "job-1",
    "generationAttemptId": "gen-att-1",
    "connectionId": "conn-1",
    "modelId": "anthropic/claude-sonnet-5",
    "operation": "language.stream",
    "attemptIds": [],
    "lifecycleState": "requested",
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-01T00:00:00Z",
}


def test_requested_record_permits_zero_attempts() -> None:
    record = codec.ProviderInvocationRecord.from_wire(_RECORD_REQUESTED)
    assert record.attempt_ids == ()


def test_in_progress_record_requires_at_least_one_attempt() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRecord.from_wire(
            {**_RECORD_REQUESTED, "lifecycleState": "in_progress", "attemptIds": []}
        )


def test_terminal_record_requires_terminal_at_and_route_evidence() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRecord.from_wire(
            {**_RECORD_REQUESTED, "lifecycleState": "succeeded", "attemptIds": ["att-1"]}
        )


def _route_evidence(**overrides: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "configuredPreference": {"connectionId": "conn-1", "modelId": "m-1"},
        "admittedRoute": {
            "connectionId": "conn-1", "modelId": "m-1", "adapterName": "a", "adapterVersion": "1"
        },
        "routeDecision": "configured",
        "sameRouteRetryCount": 0,
        "fallbackAuthorised": False,
        "attemptStartedAt": "2026-01-01T00:00:00Z",
        "attemptEndedAt": "2026-01-01T00:00:01Z",
        "terminalReason": "stop",
        "usage": {"reported": {"inputTokens": 1}},
        "reconciliationState": "reconciled",
    }
    evidence.update(overrides)
    return evidence


def _terminal_record(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        **_RECORD_REQUESTED,
        "lifecycleState": "succeeded",
        "attemptIds": ["att-1"],
        "terminalAt": "2026-01-01T00:00:01Z",
        "routeEvidence": evidence,
    }


def test_terminal_record_accepts_complete_evidence() -> None:
    route_evidence = _route_evidence()
    record = codec.ProviderInvocationRecord.from_wire(_terminal_record(route_evidence))
    assert record.route_evidence["routeDecision"] == "configured"
    assert record.to_wire()["routeEvidence"] == route_evidence


def test_terminal_record_accepts_an_explicit_authorised_fallback() -> None:
    record = codec.ProviderInvocationRecord.from_wire(
        _terminal_record(
            _route_evidence(
                routeDecision="fallback",
                fallbackAuthorised=True,
                admittedRoute={
                    "connectionId": "conn-2",
                    "modelId": "m-2",
                    "adapterName": "a",
                    "adapterVersion": "1",
                },
            )
        )
    )
    assert record.route_evidence["routeDecision"] == "fallback"


@pytest.mark.parametrize(
    "overrides",
    [
        # A substituted route labelled as the configured one: the silent
        # fallback REF-019 §5.9 forbids.
        {
            "admittedRoute": {
                "connectionId": "conn-2", "modelId": "m-2", "adapterName": "a", "adapterVersion": "1"
            }
        },
        # A fallback that never changed route, and one that was never authorised.
        {"routeDecision": "fallback", "fallbackAuthorised": True},
        {
            "routeDecision": "fallback",
            "fallbackAuthorised": False,
            "admittedRoute": {
                "connectionId": "conn-2", "modelId": "m-2", "adapterName": "a", "adapterVersion": "1"
            },
        },
        # An unchanged route claiming authorised fallback.
        {"fallbackAuthorised": True},
        {"routeDecision": "not_a_governed_decision"},
    ],
    ids=["changed-route-labelled-configured", "fallback-same-route", "fallback-unauthorised",
         "configured-claims-authorised", "ungoverned-decision"],
)
def test_a_terminal_record_may_not_emit_inconsistent_route_evidence(overrides: dict[str, Any]) -> None:
    """A durable terminal record is held to the same relational invariant its
    trace-projected events prove (freeze §11 rule 7)."""
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRecord.from_wire(_terminal_record(_route_evidence(**overrides)))


def test_indeterminate_record_requires_reconciliation_state_and_forbids_route_evidence() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRecord.from_wire(
            {**_RECORD_REQUESTED, "lifecycleState": "indeterminate", "attemptIds": ["att-1"]}
        )


def test_duplicate_attempt_ids_are_rejected() -> None:
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRecord.from_wire(
            {**_RECORD_REQUESTED, "lifecycleState": "in_progress", "attemptIds": ["att-1", "att-1"]}
        )


# --------------------------------------------------------------------------
# Strict emission is the canonical schema, oracle-checked
#
# Direct construction must not be able to produce an object whose `to_wire()`
# the record's own canonical `$def` rejects. Each builder below takes a wire
# document straight to the dataclass constructor -- never through `from_wire`
# -- so what is under test is construction itself.
# --------------------------------------------------------------------------

_ERROR_PAYLOAD: dict[str, Any] = {"code": "rate_limited", "message": "slow down"}
_ENVELOPE_PAYLOAD: dict[str, Any] = {
    "commandId": "cmd-1",
    "status": "rejected",
    "resultRef": "res-ref-1",
    "error": _ERROR_PAYLOAD,
}
_RESNAPSHOT_PAYLOAD: dict[str, Any] = {
    "workspaceId": "ws-1",
    "conversationId": "conv-1",
    "reason": "gap_detected",
    "graphRevision": 42,
    "resnapshotCursor": "cursor-2",
}

#: The second discriminator shape in the ``ChatEvent`` union: ``GenerationEvent``
#: is the one branch whose ``eventType`` is an enum of five rather than a const.
_GENERATION_EVENT: dict[str, Any] = {
    "eventId": "evt-2",
    "eventType": "chat.generation.queued",
    "schemaVersion": 1,
    "workspaceId": "ws-1",
    "conversationId": "conv-1",
    "branchId": "branch-1",
    "generationJobId": "job-1",
    "triggerMessageId": "msg-1",
    "generationEventSequence": 1,
    "occurredAt": "2026-01-01T00:00:00Z",
    "cursor": "cursor-2",
}


def _build_command_error(document: dict[str, Any]) -> codec.CommandError:
    return codec.CommandError(code=document["code"], message=document["message"])


def _build_command_result_envelope(document: dict[str, Any]) -> codec.CommandResultEnvelope:
    error = document.get("error")
    return codec.CommandResultEnvelope(
        command_id=document["commandId"],
        status=document["status"],
        result_ref=document.get("resultRef"),
        error=_build_command_error(error) if error is not None else None,
        current_version=document.get("currentVersion"),
    )


def _build_chat_event(document: dict[str, Any]) -> codec.ChatEvent:
    return codec.ChatEvent(
        event_id=document["eventId"],
        event_type=document["eventType"],
        schema_version=document["schemaVersion"],
        workspace_id=document["workspaceId"],
        conversation_id=document["conversationId"],
        occurred_at=document["occurredAt"],
        cursor=document["cursor"],
        fields={k: v for k, v in document.items() if k not in codec._CHAT_EVENT_COMMON_FIELDS},
    )


def _build_resnapshot_response(document: dict[str, Any]) -> codec.ResnapshotResponse:
    return codec.ResnapshotResponse(
        workspace_id=document["workspaceId"],
        conversation_id=document["conversationId"],
        reason=document["reason"],
        graph_revision=document["graphRevision"],
        resnapshot_cursor=document["resnapshotCursor"],
    )


def _build_provider_invocation_request(document: dict[str, Any]) -> codec.ProviderInvocationRequest:
    return codec.ProviderInvocationRequest(
        invocation_id=document["invocationId"],
        workspace_id=document["workspaceId"],
        conversation_id=document["conversationId"],
        job_id=document["jobId"],
        attempt_id=document["attemptId"],
        connection_id=document["connectionId"],
        model_id=document["modelId"],
        operation=document["operation"],
        messages=tuple(document["messages"]),
        response_format=document["responseFormat"],
        policy_ref=document["policyRef"],
        classification_ref=document["classificationRef"],
        residency_ref=document["residencyRef"],
        idempotency_key=document["idempotencyKey"],
        correlation_id=document["correlationId"],
        deadline_at=document["deadlineAt"],
        requested_at=document["requestedAt"],
        tools=tuple(document["tools"]) if "tools" in document else None,
        tool_choice=document.get("toolChoice"),
        generation_options=document.get("generationOptions"),
        provider_options_by_namespace=document.get("providerOptionsByNamespace"),
        causation_id=document.get("causationId"),
    )


def _build_provider_invocation_record(document: dict[str, Any]) -> codec.ProviderInvocationRecord:
    return codec.ProviderInvocationRecord(
        invocation_id=document["invocationId"],
        workspace_id=document["workspaceId"],
        conversation_id=document["conversationId"],
        job_id=document["jobId"],
        generation_attempt_id=document["generationAttemptId"],
        connection_id=document["connectionId"],
        model_id=document["modelId"],
        operation=document["operation"],
        attempt_ids=tuple(document["attemptIds"]),
        lifecycle_state=document["lifecycleState"],
        created_at=document["createdAt"],
        updated_at=document["updatedAt"],
        terminal_at=document.get("terminalAt"),
        route_evidence=document.get("routeEvidence"),
        reconciliation_state=document.get("reconciliationState"),
    )


#: Record name -> (direct constructor, a document the canonical `$def` accepts).
_RECORDS: dict[str, tuple[Any, dict[str, Any]]] = {
    "CommandError": (_build_command_error, _ERROR_PAYLOAD),
    "CommandResultEnvelope": (_build_command_result_envelope, _ENVELOPE_PAYLOAD),
    "ChatEvent": (_build_chat_event, _BASE_EVENT),
    "ResnapshotResponse": (_build_resnapshot_response, _RESNAPSHOT_PAYLOAD),
    "ProviderInvocationRequest": (_build_provider_invocation_request, _REQUEST_PAYLOAD),
    "ProviderInvocationRecord": (_build_provider_invocation_record, _terminal_record(_route_evidence())),
}


def _without(document: dict[str, Any], name: str) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != name}


def _message(**part: Any) -> list[dict[str, Any]]:
    return [{"role": "user", "parts": [part]}]


#: ``(record, case id, a document the canonical `$def` rejects)``. Every rule
#: class the freeze states for an emitted document is represented: required
#: primitive types, const and enum membership, patterns and RFC 3339 formats,
#: string lengths, numeric bounds, array cardinality and uniqueness, the closed
#: field set, and the nested conditional shapes.
_INVALID_EMISSIONS: list[tuple[str, str, dict[str, Any]]] = [
    # -- CommandError --------------------------------------------------------
    ("CommandError", "code-not-a-string", {**_ERROR_PAYLOAD, "code": 7}),
    ("CommandError", "code-outside-the-registry", {**_ERROR_PAYLOAD, "code": "totally_made_up_code"}),
    ("CommandError", "message-below-min-length", {**_ERROR_PAYLOAD, "message": ""}),
    ("CommandError", "message-above-max-length", {**_ERROR_PAYLOAD, "message": "x" * 2049}),
    # -- CommandResultEnvelope ----------------------------------------------
    ("CommandResultEnvelope", "status-outside-the-enum", {**_ENVELOPE_PAYLOAD, "status": "half_done"}),
    ("CommandResultEnvelope", "command-id-breaks-the-pattern", {**_ENVELOPE_PAYLOAD, "commandId": "not a valid id!"}),
    ("CommandResultEnvelope", "result-ref-above-max-length", {**_ENVELOPE_PAYLOAD, "resultRef": "r" * 257}),
    (
        "CommandResultEnvelope",
        "rejected-without-the-required-error",
        _without({**_ENVELOPE_PAYLOAD}, "error"),
    ),
    (
        "CommandResultEnvelope",
        "accepted-with-a-forbidden-error",
        {**_ENVELOPE_PAYLOAD, "status": "accepted"},
    ),
    (
        "CommandResultEnvelope",
        "current-version-below-minimum",
        {**_without(_ENVELOPE_PAYLOAD, "error"), "status": "conflict", "currentVersion": -1},
    ),
    (
        "CommandResultEnvelope",
        "current-version-not-an-integer",
        {**_without(_ENVELOPE_PAYLOAD, "error"), "status": "conflict", "currentVersion": "3"},
    ),
    # -- ChatEvent -----------------------------------------------------------
    ("ChatEvent", "message-id-not-a-string", {**_BASE_EVENT, "messageId": 1}),
    ("ChatEvent", "role-outside-the-enum", {**_BASE_EVENT, "role": "wizard"}),
    ("ChatEvent", "graph-revision-below-minimum", {**_BASE_EVENT, "graphRevision": -1}),
    ("ChatEvent", "conversation-sequence-below-minimum", {**_BASE_EVENT, "conversationSequence": 0}),
    ("ChatEvent", "schema-version-below-minimum", {**_BASE_EVENT, "schemaVersion": 0}),
    ("ChatEvent", "occurred-at-is-not-rfc-3339", {**_BASE_EVENT, "occurredAt": "t"}),
    ("ChatEvent", "event-id-breaks-the-pattern", {**_BASE_EVENT, "eventId": "not a valid id!"}),
    ("ChatEvent", "cursor-above-max-length", {**_BASE_EVENT, "cursor": "c" * 4097}),
    ("ChatEvent", "field-the-event-type-does-not-define", {**_BASE_EVENT, "notGovernedHere": True}),
    ("ChatEvent", "required-field-missing", _without(_BASE_EVENT, "role")),
    (
        "ChatEvent",
        "generation-event-missing-its-own-required-field",
        _without(_GENERATION_EVENT, "generationJobId"),
    ),
    (
        "ChatEvent",
        "generation-event-carrying-another-branchs-field",
        {**_GENERATION_EVENT, "conversationSequence": 1},
    ),
    # -- ResnapshotResponse --------------------------------------------------
    ("ResnapshotResponse", "reason-outside-the-enum", {**_RESNAPSHOT_PAYLOAD, "reason": "made_up_reason"}),
    ("ResnapshotResponse", "graph-revision-below-minimum", {**_RESNAPSHOT_PAYLOAD, "graphRevision": -1}),
    (
        "ResnapshotResponse",
        "conversation-id-breaks-the-pattern",
        {**_RESNAPSHOT_PAYLOAD, "conversationId": "not a valid id!"},
    ),
    (
        "ResnapshotResponse",
        "cursor-above-max-length",
        {**_RESNAPSHOT_PAYLOAD, "resnapshotCursor": "c" * 4097},
    ),
    # -- ProviderInvocationRequest ------------------------------------------
    ("ProviderInvocationRequest", "operation-is-not-the-const", {**_REQUEST_PAYLOAD, "operation": "language.batch"}),
    ("ProviderInvocationRequest", "no-messages", {**_REQUEST_PAYLOAD, "messages": []}),
    (
        "ProviderInvocationRequest",
        "model-id-breaks-the-pattern",
        {**_REQUEST_PAYLOAD, "modelId": "https://provider.example/model"},
    ),
    ("ProviderInvocationRequest", "deadline-is-not-rfc-3339", {**_REQUEST_PAYLOAD, "deadlineAt": "soon"}),
    ("ProviderInvocationRequest", "policy-ref-above-max-length", {**_REQUEST_PAYLOAD, "policyRef": "p" * 129}),
    (
        "ProviderInvocationRequest",
        "response-format-carries-a-forbidden-schema-ref",
        {**_REQUEST_PAYLOAD, "responseFormat": {"kind": "text", "schemaRef": "s"}},
    ),
    (
        "ProviderInvocationRequest",
        "structured-response-format-without-its-schema-ref",
        {**_REQUEST_PAYLOAD, "responseFormat": {"kind": "structured"}},
    ),
    (
        "ProviderInvocationRequest",
        "message-role-outside-the-enum",
        {**_REQUEST_PAYLOAD, "messages": [{"role": "wizard", "parts": [{"kind": "text", "text": "hi"}]}]},
    ),
    (
        "ProviderInvocationRequest",
        "text-part-carries-a-forbidden-tool-name",
        {**_REQUEST_PAYLOAD, "messages": _message(kind="text", text="hi", toolName="t")},
    ),
    (
        "ProviderInvocationRequest",
        "tool-call-part-missing-its-required-fields",
        {**_REQUEST_PAYLOAD, "messages": _message(kind="tool-call", toolCallId="part-1")},
    ),
    (
        "ProviderInvocationRequest",
        "generation-option-above-its-maximum",
        {**_REQUEST_PAYLOAD, "generationOptions": {"temperature": 3}},
    ),
    (
        "ProviderInvocationRequest",
        "option-namespace-breaks-the-pattern",
        {**_REQUEST_PAYLOAD, "providerOptionsByNamespace": {"1-not-a-namespace": {"topK": 5}}},
    ),
    (
        "ProviderInvocationRequest",
        "option-name-is-a-credential-shaped-name",
        {**_REQUEST_PAYLOAD, "providerOptionsByNamespace": {"anthropic": {"apiKey": "x"}}},
    ),
    # -- ProviderInvocationRecord -------------------------------------------
    (
        "ProviderInvocationRecord",
        "operation-is-not-the-const",
        {**_RECORD_REQUESTED, "operation": "language.batch"},
    ),
    ("ProviderInvocationRecord", "created-at-is-not-rfc-3339", {**_RECORD_REQUESTED, "createdAt": "t"}),
    (
        "ProviderInvocationRecord",
        "invocation-id-breaks-the-pattern",
        {**_RECORD_REQUESTED, "invocationId": "not a valid id!"},
    ),
    (
        "ProviderInvocationRecord",
        "lifecycle-state-outside-the-enum",
        {**_RECORD_REQUESTED, "lifecycleState": "half_done"},
    ),
    (
        "ProviderInvocationRecord",
        "in-progress-without-an-attempt",
        {**_RECORD_REQUESTED, "lifecycleState": "in_progress"},
    ),
    (
        "ProviderInvocationRecord",
        "duplicate-attempt-ids",
        {**_RECORD_REQUESTED, "lifecycleState": "in_progress", "attemptIds": ["att-1", "att-1"]},
    ),
    (
        "ProviderInvocationRecord",
        "terminal-without-terminal-at-or-route-evidence",
        {**_RECORD_REQUESTED, "lifecycleState": "succeeded", "attemptIds": ["att-1"]},
    ),
    (
        "ProviderInvocationRecord",
        "requested-with-a-forbidden-terminal-at",
        {**_RECORD_REQUESTED, "terminalAt": "2026-01-01T00:00:01Z"},
    ),
    (
        "ProviderInvocationRecord",
        "indeterminate-with-forbidden-route-evidence",
        {
            **_terminal_record(_route_evidence()),
            "lifecycleState": "indeterminate",
            "reconciliationState": "pending_reconciliation",
        },
    ),
    (
        "ProviderInvocationRecord",
        "route-evidence-missing-its-reconciliation-state",
        _terminal_record(_without(_route_evidence(), "reconciliationState")),
    ),
    (
        "ProviderInvocationRecord",
        "route-evidence-terminal-reason-outside-both-enums",
        _terminal_record(_route_evidence(terminalReason="not-a-terminal-reason")),
    ),
    (
        "ProviderInvocationRecord",
        "route-evidence-retry-count-below-minimum",
        _terminal_record(_route_evidence(sameRouteRetryCount=-1)),
    ),
    (
        "ProviderInvocationRecord",
        "route-evidence-usage-carries-no-counts",
        _terminal_record(_route_evidence(usage={"reported": {}})),
    ),
    (
        "ProviderInvocationRecord",
        "route-evidence-carries-a-field-the-shape-does-not-define",
        _terminal_record(_route_evidence(notGovernedHere=True)),
    ),
]


@pytest.mark.parametrize("record", sorted(_RECORDS))
def test_the_canonical_schema_accepts_what_each_record_emits(record: str) -> None:
    """The baseline every invalid case is a mutation of really is governed: the
    oracle accepts it, construction succeeds, and the emitted document is it."""
    build, document = _RECORDS[record]
    assert _oracle_errors(record, document) == []
    assert build(document).to_wire() == document


@pytest.mark.parametrize(
    ("record", "document"),
    [pytest.param(record, document, id=f"{record}-{case}") for record, case, document in _INVALID_EMISSIONS],
)
def test_direct_construction_cannot_produce_a_document_the_schema_rejects(
    record: str, document: dict[str, Any]
) -> None:
    """Both halves, so neither can pass vacuously: the canonical ``$def``
    rejects the document, and the record that would emit it never exists."""
    assert _oracle_errors(record, document) != [], "corpus drift: the schema accepts this document"
    with pytest.raises(codec.ChatContractDecodeError):
        _RECORDS[record][0](document)


@pytest.mark.parametrize(
    ("record", "document"),
    [pytest.param(record, document, id=f"{record}-{case}") for record, case, document in _INVALID_EMISSIONS],
)
def test_a_strict_decode_also_refuses_a_document_the_schema_rejects(
    record: str, document: dict[str, Any]
) -> None:
    """The same corpus through the decode seam. ``CommandError`` decodes only as
    part of its envelope, so it is exercised there."""
    if record == "CommandError":
        document = {**_ENVELOPE_PAYLOAD, "error": document}
        record = "CommandResultEnvelope"
    decode = getattr(codec, record).from_wire
    with pytest.raises(codec.ChatContractDecodeError):
        decode(document, strict=True)


def test_the_codex_counterexample_chat_event_fails_closed_before_emission() -> None:
    """W2-C review counterexample 1: a directly built event whose type-specific
    fields are all of the wrong type or out of range used to emit."""
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ChatEvent(
            event_id="e",
            event_type="chat.message.committed",
            schema_version=1,
            workspace_id="w",
            conversation_id="c",
            occurred_at="t",
            cursor="x",
            fields={"messageId": 1, "role": 2, "graphRevision": -1, "conversationSequence": -1},
        )


def test_the_codex_counterexample_provider_record_fails_closed_before_emission() -> None:
    """W2-C review counterexample 2: a directly built record whose ``operation``
    is not the contract's const and whose timestamps are not instants."""
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRecord(
            invocation_id="i",
            workspace_id="w",
            conversation_id="c",
            job_id="j",
            generation_attempt_id="g",
            connection_id="conn",
            model_id="m",
            operation="language.batch",
            attempt_ids=(),
            lifecycle_state="requested",
            created_at="t",
            updated_at="t",
        )


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        # W2-C review counterexample 3: no schema keyword rejects a NaN, so a
        # `generationOptions` value that is not a JSON number used to emit.
        ("nan-generation-option", {"generationOptions": {"temperature": float("nan")}}),
        ("infinite-generation-option", {"generationOptions": {"temperature": float("inf")}}),
        # `output` is an object the schema deliberately opens to arbitrary
        # properties, so no keyword in the closure ever looks inside it.
        (
            "non-finite-nested-in-a-free-form-output",
            {"messages": _message(kind="tool-result", toolCallId="part-1", output={"a": [{"b": float("-inf")}]})},
        ),
        # W2-C review counterexample 4: nothing looked at a key's own type.
        (
            "integer-key-in-a-tool-result-output",
            {"messages": _message(kind="tool-result", toolCallId="part-1", output={1: "x"})},
        ),
        (
            "integer-key-nested-in-a-free-form-output",
            {"messages": _message(kind="tool-result", toolCallId="part-1", output={"a": {"b": {2: "x"}}})},
        ),
    ],
)
def test_a_value_outside_the_json_data_model_fails_closed_before_emission(
    case: str, overrides: dict[str, Any]
) -> None:
    """JSON Schema decides a JSON instance. A non-finite float and a non-string
    key are Python values JSON has no counterpart for, so the runtime gate has
    to refuse them itself -- with the same path+rule diagnostic, never a
    ``TypeError`` out of sorting or serialization."""
    with pytest.raises(codec.ChatContractDecodeError):
        codec.ProviderInvocationRequest.from_wire({**_REQUEST_PAYLOAD, **overrides})


def test_a_finite_number_and_a_string_key_are_still_admitted() -> None:
    """The JSON-domain gate refuses only what JSON cannot express: tolerant
    additive decoding of valid JSON values is unchanged."""
    document = {
        **_REQUEST_PAYLOAD,
        "generationOptions": {"temperature": 0.5},
        "messages": _message(kind="tool-result", toolCallId="part-1", output={"1": {"a": [-1.5]}}),
    }
    assert _oracle_errors("ProviderInvocationRequest", document) == []
    assert _build_provider_invocation_request(document).to_wire() == document


def test_every_governed_event_type_in_the_union_emits() -> None:
    """``GenerationEvent`` is the union branch whose discriminator is an enum of
    five, so the same closed shape has to hold for each of them."""
    for event_type in ("chat.generation.queued", "chat.generation.started", "chat.generation.failed"):
        document = {**_GENERATION_EVENT, "eventType": event_type}
        assert _oracle_errors("ChatEvent", document) == []
        assert _build_chat_event(document).to_wire() == document


def test_governed_refs_stay_opaque_strings() -> None:
    """``classificationRef``/``residencyRef``/``policyRef`` are governed opaque
    strings: the codec holds them to the contract's own bounds and invents no
    classification, residency or policy semantics of its own."""
    document = {
        **_REQUEST_PAYLOAD,
        "classificationRef": "anything/at-all:the-contract-does-not-parse",
        "residencyRef": "eu-west-1|whatever",
        "policyRef": "p" * 128,
    }
    assert _oracle_errors("ProviderInvocationRequest", document) == []
    request = _build_provider_invocation_request(document)
    assert request.to_wire() == document


def test_an_additive_optional_field_is_still_tolerated_and_still_inert() -> None:
    """Strict emission got stricter; tolerant decode did not change. An unknown
    optional field is accepted, held off the governed field set, and never
    emitted -- so the emitted document is one the schema accepts."""
    event = codec.ChatEvent.from_wire({**_BASE_EVENT, "futureField": "ignored"})
    assert event.additive_fields["futureField"] == "ignored"
    assert _oracle_errors("ChatEvent", event.to_wire()) == []
