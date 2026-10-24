"""The Chat Part-type registry: the closed payload shape of each registered family.

The Chat Runtime Contract defers this deliberately. `MessagePart.type` is an
`OpenCode` and `payload` is `additionalProperties: true`, described as "an
opaque, schema-versioned object boundary rather than an invented catalogue",
with the per-type shapes owned by a "Chat Part-type registry (out of scope for
REF-042 and for this contract)". That registry is named twice in the whole
repository, both times in the sentence deferring to it. This module is it.

**Open stays open.** Registering a family does NOT close the vocabulary. The
contract says an unrecognised `type` decodes to an inert, display-safe fallback
and is never executed, and that remains true: a part type absent from this
registry is passed through exactly as before. What registration adds is a
stated, bounded shape for the families Core itself writes, so that a consumer
has something to decode against and a producer has something to be held to.

**Producer-first.** A family is registered here only when something actually
produces it. The families below are written by Core today:

* `text` -- `service/chat_generation_executor.py` (assistant) and the user side
  via `editableParts`.
* `attachment` -- `service/chat_submit.py`, through
  `service/chat_attachment_authority.attachment_part_payload`.
* `reasoning.summary` -- `service/chat_generation_executor.py`, from only an
  explicit provider `reasoning-summary` event; never from raw reasoning content.
* `tool.call` -- `service/chat_generation_executor.py`, from the durable
  provider-reported or governed tool ledger.
* `web.search` -- `service/chat_generation_executor.py`, from a completed,
  provider-reported `codex.web_search` lifecycle carrying only its query.

Nothing else is registered, on purpose. `packages/chat/src/self-rendered` in
Platform decodes seventeen display-only part types that nothing produces, and
the provider event layer declares twenty event types of which seven are ever
emitted. Adding a schema here for a family with no producer would be the same
defect a third time.

**Why this is not a validator for the whole write path.** `SubmitMessage`
accepts `editableParts` whose `type` is any open code and whose payload is
unbounded, and passes both through to durable storage. Bounding a REGISTERED
family's payload there is compatible with the contract; refusing an
unregistered one is not, because the contract says the vocabulary is open.
:func:`validate_registered_payload` is written for exactly that distinction:
it answers "this payload is wrong for a family I know" and never "I do not
know this family".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

#: An accepted attachment's part type is defined with the authority that writes
#: it, and imported rather than restated so the constant has one home.
from omnivia_core_runtime.service.chat_attachment_authority import ATTACHMENT_PART_TYPE

__all__ = [
    "PART_REGISTRY_VERSION",
    "PRESENTATION_ONLY_PART_TYPES",
    "REASONING_SUMMARY_PART_TYPE",
    "REGISTERED_PART_TYPES",
    "TEXT_PART_TYPE",
    "TOOL_CALL_PART_TYPE",
    "WEB_SEARCH_PART_TYPE",
    "PartPayloadInvalid",
    "is_registered_part_type",
    "validate_registered_payload",
]

#: The registry's own version, independent of `MessagePart.schemaVersion`.
#:
#: Bumped when a registered family's shape changes in a way a consumer must
#: notice. Adding a NEW family does not bump it: a consumer that has never seen
#: the family decodes it to the contract's inert fallback, which is the defined
#: behaviour for an unrecognised type and needs no version negotiation.
PART_REGISTRY_VERSION: Final = 1

#: The assistant's and the composer's text. The one family that predates this
#: registry on both sides.
TEXT_PART_TYPE: Final = "text"

#: C9: one governed tool call the provider proposed during this generation.
#:
#: The family is named for what it states -- a CALL that happened -- not for the
#: card a consumer draws it as.
TOOL_CALL_PART_TYPE: Final = "tool.call"

#: One provider-issued display-safe reasoning summary. This is deliberately
#: distinct from protected reasoning content, which never crosses F2a.
REASONING_SUMMARY_PART_TYPE: Final = "reasoning.summary"

#: One completed search that the connected provider reported executing. The
#: public Codex protocol currently exposes the query but no trustworthy result
#: URLs or snippets, so this family deliberately carries neither.
WEB_SEARCH_PART_TYPE: Final = "web.search"

#: Exact Core-authored families that are transcript presentation, not F2a
#: provider prompt content. Keeping this taxonomy beside the part registry gives
#: both the ordinary and compacted context builders the same closed decision.
#: Unknown open-code parts are deliberately absent and therefore still refused.
PRESENTATION_ONLY_PART_TYPES: Final = frozenset(
    {
        REASONING_SUMMARY_PART_TYPE,
        TOOL_CALL_PART_TYPE,
        WEB_SEARCH_PART_TYPE,
    }
)

class PartPayloadInvalid(Exception):
    """A payload that does not match the registered shape of its own family.

    Carries the field and the rule, never the rejected value: a part payload on
    the write path is untrusted input, and echoing it is how an error message
    becomes an oracle. Same discipline as `chat_submit`'s own refusals.
    """

    def __init__(self, field: str, rule: str) -> None:
        super().__init__(f"{field} {rule}")
        self.field = field
        self.rule = rule


def _require_exact_keys(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    prefix: str = "",
) -> None:
    """Exactly `required`, optionally `optional`, and nothing else.

    `prefix` qualifies the reported field for a nested object, so a smuggled
    `attachmentReference.path` is named in full rather than as a bare `path`
    that a caller cannot locate.
    """
    present = set(payload)
    missing = required - present
    if missing:
        raise PartPayloadInvalid(f"{prefix}{min(missing)}", "is required for this part type")
    unknown = present - required - optional
    if unknown:
        raise PartPayloadInvalid(f"{prefix}{min(unknown)}", "is not a field of this part type")


def _require_text(payload: Mapping[str, Any], field: str, *, maximum: int) -> None:
    value = payload.get(field)
    if not isinstance(value, str):
        raise PartPayloadInvalid(field, "must be a string")
    # Counted in UTF-8 bytes, as every other bound in this contract is: a
    # character count would admit a payload the storage column refuses.
    if len(value.encode("utf-8")) > maximum:
        raise PartPayloadInvalid(field, f"must be at most {maximum} UTF-8 bytes")


#: `text` carries exactly its own text. The contract's `DirectionTextPart`
#: closes the same family at 1..4000 characters for the C6 steering path; a
#: committed Message part is not bounded there, and the Message's own
#: `content_hash` plus the transport's envelope bound are what hold it, so this
#: states the generous storage-side ceiling rather than inventing a stricter one.
_TEXT_MAX_UTF8_BYTES: Final = 1_048_576


def _validate_text(payload: Mapping[str, Any]) -> None:
    _require_exact_keys(payload, required=frozenset({"text"}), optional=frozenset())
    _require_text(payload, "text", maximum=_TEXT_MAX_UTF8_BYTES)


def _validate_attachment(payload: Mapping[str, Any]) -> None:
    """The shape `attachment_part_payload` writes.

    The opaque reference, plus the two display facts migration 0070's header
    names as the service's own: media type and byte size. Both optional,
    because a part committed before they were carried has neither. No name:
    Core stores none, and 0070 forbids a path, URL or handle in every column.
    """
    _require_exact_keys(
        payload,
        required=frozenset({"attachmentReference"}),
        optional=frozenset({"mediaType", "sizeBytes"}),
    )
    reference = payload["attachmentReference"]
    if not isinstance(reference, Mapping):
        raise PartPayloadInvalid("attachmentReference", "must be an object")
    _require_exact_keys(
        reference,
        required=frozenset({"referenceId", "sourceRevision"}),
        optional=frozenset(),
        prefix="attachmentReference.",
    )
    for field in ("referenceId", "sourceRevision"):
        value = reference[field]
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise PartPayloadInvalid(
                f"attachmentReference.{field}", "must be a string of 1 to 128 characters"
            )
    if "mediaType" in payload:
        _require_text(payload, "mediaType", maximum=255)
    if "sizeBytes" in payload:
        size = payload["sizeBytes"]
        # `bool` is an `int` in Python, and `True` is not a byte count.
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PartPayloadInvalid("sizeBytes", "must be a non-negative integer")


def _validate_tool_call(payload: Mapping[str, Any]) -> None:
    """The shape :func:`tool_call_part_payload` writes.

    THE TOOL'S ARGUMENTS ARE NOT A FIELD OF THIS FAMILY, and closing the shape
    is how that is enforced. A Message part is read by anyone who may read the
    conversation, and C8 already settled that this bundle publishes no tool
    arguments in any form -- `ToolCallProjection` refuses to project them even
    to the reviewer who has to decide on the call. A part carrying them would be
    the first outbound disclosure of tool input, reached through the transcript
    rather than through a query. The contract's `MessagePart.payload` is
    `additionalProperties: true`, so nothing but this registry would stop it.
    A producer that wants to show arguments needs a new decision, not a spare
    field.

    `state` is Core's own tool-call state, stated verbatim and NOT translated
    into any consumer's display vocabulary. The contract makes an unrecognised
    state draw inertly, which is the right outcome and keeps Core from having to
    know which chips a renderer owns.
    """
    _require_exact_keys(
        payload,
        required=frozenset({"name"}),
        optional=frozenset({"id", "state", "detail"}),
    )
    _require_text(payload, "name", maximum=128)
    for field in ("id", "state", "detail"):
        if field in payload:
            _require_text(payload, field, maximum=512)
            if payload[field] == "":
                raise PartPayloadInvalid(field, "must not be empty when present")


def _validate_reasoning_summary(payload: Mapping[str, Any]) -> None:
    """The exact display payload emitted for explicit provider summaries only."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "detail"}),
        optional=frozenset(),
    )
    _require_text(payload, "title", maximum=64)
    _require_text(payload, "detail", maximum=262_144)
    if payload["title"] != "Reasoning summary":
        raise PartPayloadInvalid("title", "must identify the reasoning summary")
    if payload["detail"] == "" or "\x00" in payload["detail"]:
        raise PartPayloadInvalid("detail", "must be non-empty and NUL-free")


def _validate_web_search(payload: Mapping[str, Any]) -> None:
    """The exact completed-search display fact produced from the tool ledger."""
    _require_exact_keys(
        payload,
        required=frozenset({"query", "state"}),
        optional=frozenset(),
    )
    _require_text(payload, "query", maximum=4_096)
    query = payload["query"]
    if query == "" or query.strip() != query or any(ord(character) < 32 or ord(character) == 127 for character in query):
        raise PartPayloadInvalid("query", "must be non-empty, trimmed and control-free")
    if payload["state"] != "complete":
        raise PartPayloadInvalid("state", "must be complete")


#: Every registered family, and the validator for its payload. Closed over the
#: families Core produces -- see the module docstring on producer-first.
_VALIDATORS: Final[Mapping[str, Any]] = {
    TEXT_PART_TYPE: _validate_text,
    ATTACHMENT_PART_TYPE: _validate_attachment,
    REASONING_SUMMARY_PART_TYPE: _validate_reasoning_summary,
    TOOL_CALL_PART_TYPE: _validate_tool_call,
    WEB_SEARCH_PART_TYPE: _validate_web_search,
}

#: The registered family names, for a caller that wants to report the registry
#: rather than validate against it.
REGISTERED_PART_TYPES: Final = tuple(sorted(_VALIDATORS))


def is_registered_part_type(part_type: str) -> bool:
    """Whether this registry states a shape for `part_type`.

    `False` is not a refusal. The contract's vocabulary is open, and an
    unregistered type is passed through and decoded to the inert fallback, as it
    always has been.
    """
    return part_type in _VALIDATORS


def validate_registered_payload(part_type: str, payload: Mapping[str, Any]) -> None:
    """Prove `payload` matches the registered shape of `part_type`, or raise.

    A no-op for an unregistered `part_type`, deliberately: this function's
    question is "is this payload wrong for a family I know", never "do I know
    this family". Raises :class:`PartPayloadInvalid` naming the field and the
    rule, with the offending value left out.
    """
    validator = _VALIDATORS.get(part_type)
    if validator is None:
        return
    if not isinstance(payload, Mapping):
        raise PartPayloadInvalid("payload", "must be an object")
    validator(payload)
