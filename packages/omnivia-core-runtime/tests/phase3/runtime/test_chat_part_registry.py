"""The Chat Part-type registry: the shapes it closes, and the openness it keeps.

Two properties matter here and they pull in opposite directions, so both are
asserted rather than one being assumed from the other:

1. A REGISTERED family's payload is bounded. That is what gives a consumer
   something to decode against, and it is what closes the write path's
   previously unbounded passthrough for those families.
2. An UNREGISTERED family is still passed through untouched. `MessagePart.type`
   is an open code by contract and an unrecognised one decodes to an inert
   fallback, so a registry that refused unknown types would narrow the
   contract rather than implement it.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnivia_core_runtime.service.chat_attachment_authority import (
    ATTACHMENT_PART_TYPE,
    PinnedAttachment,
    attachment_part_payload,
)
from omnivia_core_runtime.service.chat_part_registry import (
    PART_REGISTRY_VERSION,
    REASONING_SUMMARY_PART_TYPE,
    REGISTERED_PART_TYPES,
    TEXT_PART_TYPE,
    TOOL_CALL_PART_TYPE,
    PartPayloadInvalid,
    is_registered_part_type,
    validate_registered_payload,
)

REVISION = "sha256:" + "a" * 64


def _reference(**overrides: Any) -> dict[str, Any]:
    return {"referenceId": "file-a", "sourceRevision": REVISION, **overrides}


def test_the_registry_states_only_the_families_core_produces() -> None:
    # Producer-first, and the whole point of the registry's restraint: Platform
    # decodes seventeen display-only part types that nothing produces, and the
    # provider event layer declares more event types than it emits today
    # emitted. Registering a shape for a family with no producer would be that
    # same defect a third time.
    #
    # Each family here has one, named so this list cannot grow quietly:
    #   attachment  `chat_submit`'s staged-attachment commit
    #   text        `chat_generation_executor._materialize_assistant` and `chat_submit`
    #   reasoning.summary  explicit provider `reasoning-summary` events only
    #   tool.call   `chat_generation_executor._record_tool_proposal` (C9), which
    #               records a provider `tool-call` event into C8's ledger, and
    #               `_materialize_assistant`, which emits one part per row
    assert REGISTERED_PART_TYPES == (
        ATTACHMENT_PART_TYPE,
        REASONING_SUMMARY_PART_TYPE,
        TEXT_PART_TYPE,
        TOOL_CALL_PART_TYPE,
    )
    assert is_registered_part_type(TEXT_PART_TYPE)
    assert is_registered_part_type(ATTACHMENT_PART_TYPE)
    assert is_registered_part_type(TOOL_CALL_PART_TYPE)
    assert is_registered_part_type(REASONING_SUMMARY_PART_TYPE)
    # Adding a family does NOT bump the version: a consumer that has never seen
    # it decodes it to the contract's inert fallback, which is the defined
    # behaviour for an unrecognised type and needs no negotiation.
    assert PART_REGISTRY_VERSION == 1


def test_an_unregistered_family_is_passed_through_and_never_refused() -> None:
    # The contract's vocabulary is OPEN. These are exactly the kinds of type a
    # future minor release may add, and an unrecognised one must reach the
    # renderer's inert fallback rather than being rejected at the wire.
    for part_type in ("markdown", "citation", "table", "app.output", "workflow.status"):
        assert not is_registered_part_type(part_type)
        # Any payload at all, including ones a registered family would refuse.
        validate_registered_payload(part_type, {"anything": {"nested": [1, 2, 3]}})
        validate_registered_payload(part_type, {})


def test_text_carries_exactly_its_own_text() -> None:
    validate_registered_payload(TEXT_PART_TYPE, {"text": "hello"})
    validate_registered_payload(TEXT_PART_TYPE, {"text": ""})
    for bad, field in (
        ({}, "text"),
        ({"text": 7}, "text"),
        ({"text": None}, "text"),
        ({"text": "hello", "markdown": True}, "markdown"),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(TEXT_PART_TYPE, bad)
        assert refused.value.field == field


def test_reasoning_summary_is_an_exact_display_safe_shape() -> None:
    validate_registered_payload(
        REASONING_SUMMARY_PART_TYPE,
        {"title": "Reasoning summary", "detail": "Compared the two supported routes."},
    )
    for bad, field in (
        ({"title": "Reasoning summary"}, "detail"),
        ({"title": "Reasoning", "detail": "safe"}, "title"),
        ({"title": "Reasoning summary", "detail": ""}, "detail"),
        ({"title": "Reasoning summary", "detail": "safe", "content": "hidden"}, "content"),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(REASONING_SUMMARY_PART_TYPE, bad)
        assert refused.value.field == field


def test_the_attachment_shape_is_exactly_what_the_authority_writes() -> None:
    # The registry and the producer must agree, so the producer's own output is
    # the fixture. A drift between them is the defect this test exists for.
    pinned = PinnedAttachment(
        reference_id="file-a",
        source_revision=REVISION,
        media_type="application/pdf",
        size_bytes=2048,
    )
    validate_registered_payload(ATTACHMENT_PART_TYPE, attachment_part_payload(pinned))
    # And the older shape, from before the display facts were carried.
    validate_registered_payload(
        ATTACHMENT_PART_TYPE, {"attachmentReference": _reference()}
    )


def test_the_attachment_shape_refuses_a_smuggled_field_or_a_bad_fact() -> None:
    cases: tuple[tuple[dict[str, Any], str], ...] = (
        ({}, "attachmentReference"),
        ({"attachmentReference": "file-a"}, "attachmentReference"),
        # A path or URL beside the reference: identity only, by 0070's rule.
        ({"attachmentReference": _reference(), "path": "/tmp/x"}, "path"),
        ({"attachmentReference": _reference(), "name": "secrets.pdf"}, "name"),
        # ... and one level in, which is where it would actually be hidden.
        ({"attachmentReference": _reference(path="/tmp/x")}, "attachmentReference.path"),
        ({"attachmentReference": {"referenceId": "file-a"}}, "attachmentReference.sourceRevision"),
        (
            {"attachmentReference": _reference(referenceId=7)},
            "attachmentReference.referenceId",
        ),
        (
            {"attachmentReference": _reference(referenceId="")},
            "attachmentReference.referenceId",
        ),
        ({"attachmentReference": _reference(), "mediaType": 7}, "mediaType"),
        ({"attachmentReference": _reference(), "sizeBytes": -1}, "sizeBytes"),
        ({"attachmentReference": _reference(), "sizeBytes": "big"}, "sizeBytes"),
        # `True` is an `int` in Python and is not a byte count.
        ({"attachmentReference": _reference(), "sizeBytes": True}, "sizeBytes"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(ATTACHMENT_PART_TYPE, bad)
        assert refused.value.field == field, bad


def test_a_refusal_names_the_field_and_the_rule_and_never_the_value() -> None:
    # A part payload on the write path is untrusted input. An error that echoed
    # it would turn the refusal into an oracle for what was sent.
    secret = "a-value-that-must-not-be-echoed"
    with pytest.raises(PartPayloadInvalid) as refused:
        validate_registered_payload(TEXT_PART_TYPE, {"text": "ok", secret: 1})
    assert refused.value.field == secret, "the FIELD name is named, which is the caller's own"
    assert secret not in refused.value.rule
    with pytest.raises(PartPayloadInvalid) as other:
        validate_registered_payload(TEXT_PART_TYPE, {"text": secret, "extra": 1})
    assert secret not in str(other.value)


def test_a_non_object_payload_is_refused_for_a_registered_family_only() -> None:
    with pytest.raises(PartPayloadInvalid) as refused:
        validate_registered_payload(TEXT_PART_TYPE, "not an object")  # type: ignore[arg-type]
    assert refused.value.field == "payload"
    # Unregistered stays untouched, even for a non-object.
    validate_registered_payload("markdown", "not an object")  # type: ignore[arg-type]
