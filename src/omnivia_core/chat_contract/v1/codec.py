"""Bounded public codec for the Chat Runtime Contract v1 W2/F2 seam.

This is deliberately not a generated, exhaustive dataclass mirror of every
``$def`` across the 13 schemas (234 definitions total) -- see
``scripts/generate-chat-contract.py``'s module docstring. It covers exactly:

- deterministic canonical JSON emission;
- one-version negotiation for wire ``protocolVersion`` major ``1``
  (:data:`~omnivia_core.chat_contract.v1.generated.PROTOCOL_VERSION`);
- :class:`CommandResultEnvelope` (``commands.schema.json``), the one stable
  result envelope every Chat command uses;
- :class:`ChatEvent` and :class:`ResnapshotResponse` (``events.schema.json``),
  the transport event envelope and the cursor/gap/resnapshot shapes a
  snapshot/suffix read needs;
- :class:`ProviderInvocationRequest` and :class:`ProviderInvocationRecord`
  (``provider.schema.json``), the F2a request and durable-record projections.

Every decoded value is an immutable, frozen dataclass; every nested JSON
object or array a record carries is frozen too (:func:`_freeze_json`), whether
it arrived through ``from_wire`` or through direct construction, so nothing a
caller shares can be mutated out from under it.

**No record can exist whose emitted document its own canonical ``$def``
rejects.** Every record's ``__post_init__`` ends by evaluating exactly what
``to_wire`` would emit against that record's subschema
(:func:`_require_governed_emission`), from the generated
:data:`~omnivia_core.chat_contract.v1.generated.VALIDATION_SCHEMAS` -- the
approved ``contracts/chat/v1/schemas`` bytes, machine-read by
``scripts/generate-chat-contract.py``. So the whole approved rule is enforced
-- required primitive types, ``const``/``enum`` membership, patterns, RFC 3339
timestamps, string lengths, numeric bounds, array cardinality and uniqueness,
closed field sets, and the nested ``if``/``then``/``else`` shapes of every
message part, response format and route-evidence object -- without one of those
rules being restated here, where it could drift from the bytes it claims to
follow. Because the record is frozen and everything it carries was deep-frozen
on the way in, passing construction is a permanent property, not a moment.

What remains hand-written are the checks JSON Schema cannot express: the JSON
data model itself, since a keyword can only decide a value JSON admits and a
``NaN`` or an integer object key is not one (``_validation.json_domain_violation``,
also applied by the two canonical serializers); the
route-evidence relational invariant (:func:`_validate_route_evidence`), which
compares two sibling objects, and :class:`ChatEvent`'s rule that an envelope
field may not be restated as a type-specific one, which is about this codec's
own governed/inert split rather than about the wire document.

**Tolerant decoding and strict emission are separate directions.** Decoding is
where compatibility lives: with ``strict=False`` an unknown additive optional
field is accepted and an unknown ``ChatEvent.eventType`` still decodes, so a
v1 consumer keeps the event's cursor position instead of losing the stream
(compatibility doc §3; freeze §9 rule 6). But what a tolerant decode admits, it
holds *inert*: an unrecognised field is kept out of the governed field set and
is never re-emitted, so it can never be mistaken for execution authority on the
way back out. Emission is strict in every direction -- construction validates,
so a directly built dataclass is as governed as a decoded one, and ``to_wire``
emits only fields the exact governed shape allows and refuses outright to emit
an event type this version does not define. ``strict=True`` on a decode simply
moves the emission rules forward to the decode, mirroring the schema's own
``additionalProperties: false``.

No diagnostic raised here reproduces a rejected value's content: a message
names the field path and the rule it broke, never the value itself, since a
refused document may be carrying exactly the kind of content this contract
forbids on the wire.

Standard library only. Nothing here may depend on runtime, storage, HTTP,
MCP, CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.chat_contract.v1 import _validation
from omnivia_core.chat_contract.v1.generated import (
    CHAT_EVENT_FIELDS,
    CHAT_EVENT_SCHEMA_REFS,
    CHAT_EVENT_TYPES,
    PROTOCOL_VERSION,
    RECORD_VALIDATION_REFS,
)

__all__ = [
    "ChatContractDecodeError",
    "ChatEvent",
    "CommandError",
    "CommandResultEnvelope",
    "ProviderInvocationRecord",
    "ProviderInvocationRequest",
    "ResnapshotResponse",
    "UnsupportedProtocolVersionError",
    "negotiate_protocol_version",
    "to_canonical_json",
    "to_canonical_json_document",
]

class ChatContractDecodeError(ValueError):
    """A bounded Chat Runtime Contract v1 decode or emission failure.

    Carries the ``path`` (a dotted field path) and the ``rule`` that was
    broken, and nothing else: the message never reproduces a rejected
    field's own value.
    """

    def __init__(self, path: str, rule: str) -> None:
        self.path = path
        self.rule = rule
        super().__init__(f"{path}: {rule}")


class UnsupportedProtocolVersionError(ChatContractDecodeError):
    """Raised when a wire ``protocolVersion`` is not the one this package supports."""

    def __init__(self, path: str = "protocolVersion") -> None:
        super().__init__(path, f"unsupported protocolVersion; this package supports exactly {PROTOCOL_VERSION!r}")


# --------------------------------------------------------------------------
# Deterministic JSON
# --------------------------------------------------------------------------


def _require_json_domain(payload: Mapping[str, Any]) -> None:
    """Refuse a payload carrying a value JSON has no counterpart for.

    ``json.dumps`` would coerce a non-string key to a string and quietly emit a
    document nobody wrote, and ``sort_keys`` over mixed key types raises a bare
    :class:`TypeError`. Both become the same path+rule diagnostic every other
    refusal in this codec uses.
    """
    violation = _validation.json_domain_violation(payload)
    if violation is not None:
        path, rule = violation
        raise ChatContractDecodeError(path or "payload", rule)


def to_canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize a wire mapping to the compact canonical JSON form.

    Keys are sorted and separators are fixed, so the same value always
    produces the same bytes regardless of how the mapping was built. ``NaN``,
    the infinities and non-string keys are rejected rather than emitted as the
    non-standard literals or stringified names Python would otherwise produce.
    """
    _require_json_domain(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def to_canonical_json_document(payload: Mapping[str, Any]) -> str:
    """Serialize a wire mapping to the canonical on-disk JSON form.

    The same ordering guarantees as :func:`to_canonical_json`, indented and
    newline-terminated so a checked-in fixture built from it diffs cleanly.
    """
    _require_json_domain(payload)
    text = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    )
    return f"{text}\n"


# --------------------------------------------------------------------------
# One-version negotiation
# --------------------------------------------------------------------------

_PROTOCOL_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*\.(0|[1-9][0-9]*)$")


def negotiate_protocol_version(offered: str) -> str:
    """Return the supported ``protocolVersion`` if ``offered`` is it, else raise.

    This package supports exactly one wire protocol version
    (:data:`~omnivia_core.chat_contract.v1.generated.PROTOCOL_VERSION`). A v1
    decoder rejects anything else -- an unsupported major, an unsupported
    minor of the supported major, or a syntactically malformed value -- and
    never silently downgrades
    (CHAT-RUNTIME-CONTRACT-V1-COMPATIBILITY-AND-MIGRATION.md §1, §3). Widening
    this to genuine minor-range negotiation is future work for the day a
    second minor of major ``PROTOCOL_MAJOR`` is actually defined; there is
    only one today, so there is nothing to range over yet.
    """
    if not isinstance(offered, str) or not _PROTOCOL_VERSION_PATTERN.match(offered):
        raise UnsupportedProtocolVersionError()
    if offered != PROTOCOL_VERSION:
        raise UnsupportedProtocolVersionError()
    return PROTOCOL_VERSION


# --------------------------------------------------------------------------
# Shared decode helpers
# --------------------------------------------------------------------------


def _freeze_json(value: Any) -> Any:
    """Recursively freeze a decoded JSON value: dict -> MappingProxyType, list -> tuple."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _require_mapping(payload: object, path: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ChatContractDecodeError(path, "expected a JSON object")
    return payload


def _require_field(mapping: Mapping[str, Any], name: str, path: str) -> Any:
    if name not in mapping:
        raise ChatContractDecodeError(f"{path}.{name}", "required field is missing")
    return mapping[name]


def _require_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChatContractDecodeError(path, "expected a non-empty string")
    return value


def _optional_str(mapping: Mapping[str, Any], name: str, path: str) -> str | None:
    if name not in mapping:
        return None
    return _require_str(mapping[name], f"{path}.{name}")


def _require_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChatContractDecodeError(path, "expected an integer")
    return value


def _require_dict(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChatContractDecodeError(path, "expected a JSON object")
    return value


def _require_str_list(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ChatContractDecodeError(path, "expected an array of strings")
    return tuple(_require_str(item, f"{path}[{index}]") for index, item in enumerate(value))


def _reject_unknown_fields(mapping: Mapping[str, Any], known: frozenset[str], path: str) -> None:
    extra = sorted(set(mapping) - known)
    if extra:
        raise ChatContractDecodeError(path, f"{len(extra)} field(s) not in the closed known set")


def _require_strs(pairs: Sequence[tuple[str, object]]) -> None:
    """Assert every ``(wire_field_name, value)`` pair is a non-empty string.

    Used from ``__post_init__`` so a directly constructed dataclass is held to
    the same field shapes a decode enforces.
    """
    for path, value in pairs:
        _require_str(value, path)


def _frozen_object(value: object, path: str) -> Mapping[str, Any]:
    """Require a JSON object and return it deeply frozen."""
    frozen: Mapping[str, Any] = _freeze_json(_require_dict(value, path))
    return frozen


def _frozen_object_tuple(value: object, path: str) -> tuple[Mapping[str, Any], ...]:
    """Require an array of JSON objects and return them deeply frozen."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, Mapping)):
        raise ChatContractDecodeError(path, "expected an array of JSON objects")
    return tuple(_frozen_object(item, f"{path}[{index}]") for index, item in enumerate(value))


def _require_governed_emission(document: Mapping[str, Any], ref: str, record: str) -> None:
    """Refuse a record whose emitted document its own canonical subschema rejects.

    ``ref`` is a key of the generated
    :data:`~omnivia_core.chat_contract.v1.generated.VALIDATION_SCHEMAS`, read
    from the approved ``contracts/chat/v1/schemas`` bytes. Every value rule
    this codec holds a record to -- required primitive types, ``const``/``enum``
    membership, patterns, RFC 3339 timestamps, lengths, numeric bounds,
    cardinality, closed field sets and the nested conditional shapes -- is
    enforced from there, so none of them is restated by hand and none can go
    stale against a schema this repository cannot change without failing the
    generator's digest gate.

    Called last in every ``__post_init__``, so it holds a directly constructed
    record to exactly what a decoded one is held to: an object whose
    ``to_wire`` the contract would reject never comes into existence, and there
    is no later moment at which it could, because every record is frozen and
    every nested structure it carries was deep-frozen on the way in.
    """
    violation = _validation.first_violation(document, ref)
    if violation is not None:
        path, rule = violation
        raise ChatContractDecodeError(path or record, rule)


# --------------------------------------------------------------------------
# CommandResultEnvelope (commands.schema.json)
# --------------------------------------------------------------------------

_COMMAND_ERROR_FIELDS: Final[frozenset[str]] = frozenset({"code", "message"})
_COMMAND_RESULT_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset(
    {"commandId", "status", "resultRef", "error", "currentVersion"}
)


@dataclass(frozen=True, slots=True)
class CommandError:
    """``CommandResultEnvelope.error``: a code from :data:`ERROR_CODES` plus a display-safe message."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _require_strs((("code", self.code), ("message", self.message)))
        _require_governed_emission(self.to_wire(), RECORD_VALIDATION_REFS["CommandError"], "CommandError")

    def to_wire(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_wire(cls, payload: object, *, strict: bool, path: str) -> CommandError:
        mapping = _require_mapping(payload, path)
        if strict:
            _reject_unknown_fields(mapping, _COMMAND_ERROR_FIELDS, path)
        code = _require_str(_require_field(mapping, "code", path), f"{path}.code")
        message = _require_str(_require_field(mapping, "message", path), f"{path}.message")
        return cls(code=code, message=message)


@dataclass(frozen=True, slots=True)
class CommandResultEnvelope:
    """``commands.schema.json#/$defs/CommandResultEnvelope``.

    ``status`` is closed to ``accepted | completed | rejected | conflict``.
    ``error`` is required for ``rejected``, optional for ``conflict``, and
    forbidden for ``accepted``/``completed``, exactly matching the schema's
    own ``if``/``then``/``else`` (freeze §7.1).
    """

    command_id: str
    status: str
    result_ref: str | None = None
    error: CommandError | None = None
    current_version: int | None = None

    def __post_init__(self) -> None:
        _require_strs((("commandId", self.command_id),))
        if self.result_ref is not None:
            _require_str(self.result_ref, "resultRef")
        if self.current_version is not None:
            _require_int(self.current_version, "currentVersion")
        _require_governed_emission(
            self.to_wire(), RECORD_VALIDATION_REFS["CommandResultEnvelope"], "CommandResultEnvelope"
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"commandId": self.command_id, "status": self.status}
        if self.result_ref is not None:
            wire["resultRef"] = self.result_ref
        if self.error is not None:
            wire["error"] = self.error.to_wire()
        if self.current_version is not None:
            wire["currentVersion"] = self.current_version
        return wire

    @classmethod
    def from_wire(cls, payload: object, *, strict: bool = False, path: str = "CommandResultEnvelope") -> CommandResultEnvelope:
        """Decode a wire ``CommandResultEnvelope``.

        ``strict=True`` additionally rejects an unknown top-level field
        (mirroring the schema's ``additionalProperties: false``); the default
        ``strict=False`` accepts one, which is the documented compatible
        decode behaviour for a new optional field a future compatible minor
        might add. A tolerated unknown field is dropped here rather than
        carried on the record, so it stays inert and can never be re-emitted
        as if this version had governed it.
        """
        mapping = _require_mapping(payload, path)
        if strict:
            _reject_unknown_fields(mapping, _COMMAND_RESULT_ENVELOPE_FIELDS, path)
        command_id = _require_str(_require_field(mapping, "commandId", path), f"{path}.commandId")
        status = _require_str(_require_field(mapping, "status", path), f"{path}.status")
        result_ref = _optional_str(mapping, "resultRef", path)
        error = None
        if "error" in mapping:
            error = CommandError.from_wire(mapping["error"], strict=strict, path=f"{path}.error")
        current_version = None
        if "currentVersion" in mapping:
            current_version = _require_int(mapping["currentVersion"], f"{path}.currentVersion")
        return cls(
            command_id=command_id,
            status=status,
            result_ref=result_ref,
            error=error,
            current_version=current_version,
        )


# --------------------------------------------------------------------------
# ChatEvent / ResnapshotResponse (events.schema.json)
# --------------------------------------------------------------------------

#: The envelope fields every one of the 15 durable Chat event $defs requires
#: (events.schema.json) -- exactly the intersection of every branch's
#: ``required``. Every other field a decoded event carries is type-specific and
#: is split by :data:`CHAT_EVENT_FIELDS` into the governed
#: :attr:`ChatEvent.fields` and the inert :attr:`ChatEvent.additive_fields`.
_CHAT_EVENT_COMMON_FIELDS: Final[frozenset[str]] = frozenset(
    {"eventId", "eventType", "schemaVersion", "workspaceId", "conversationId", "occurredAt", "cursor"}
)


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """A decoded ``events.schema.json`` transport event envelope.

    :attr:`fields` carries exactly the event-type-specific fields
    (``graphRevision``, ``messageId``, ``generationJobId``, and so on)
    ``CHAT_EVENT_FIELDS`` allows for this exact ``event_type``, frozen and
    immutable, rather than 15 separate generated dataclasses (out of scope for
    W2-C; see ``scripts/generate-chat-contract.py``). One flat mapping is not a
    weaker guarantee than 15 dataclasses would be: construction validates the
    document this event would emit against that event type's own closed
    ``events.schema.json`` branch (:data:`CHAT_EVENT_SCHEMA_REFS`), so a field
    that type does not define, a required field it omits, and a governed field
    of the wrong type, range, pattern or format are all refused here.

    :attr:`is_known_event_type` is ``False`` when ``event_type`` is not a
    member of :data:`CHAT_EVENT_TYPES`: a compatible minor release may add a
    new event type (compatibility doc §4), and a v1 consumer that cannot
    interpret it treats the event as requiring a resnapshot -- exactly rule 6
    of freeze §9 -- rather than guessing its meaning. A tolerant decode
    therefore still decodes the envelope instead of rejecting outright, so a
    caller keeps the event's cursor position; but the event type carries no
    governed shape here, so all of its type-specific fields land in
    :attr:`additive_fields`, and :meth:`to_wire` refuses to emit it at all.

    :attr:`additive_fields` is the inert half of an additive decode: fields a
    tolerant decode accepted but this version does not govern. They are held
    for diagnosis only, never merged into :attr:`fields` and never emitted, so
    nothing unrecognised can be mistaken for execution authority on the way
    back out.
    """

    event_id: str
    event_type: str
    schema_version: int
    workspace_id: str
    conversation_id: str
    occurred_at: str
    cursor: str
    fields: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    additive_fields: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _require_strs(
            (
                ("eventId", self.event_id),
                ("eventType", self.event_type),
                ("workspaceId", self.workspace_id),
                ("conversationId", self.conversation_id),
                ("occurredAt", self.occurred_at),
                ("cursor", self.cursor),
            )
        )
        _require_int(self.schema_version, "schemaVersion")
        object.__setattr__(self, "fields", _frozen_object(self.fields, "fields"))
        object.__setattr__(self, "additive_fields", _frozen_object(self.additive_fields, "additiveFields"))

        restated = sorted(set(self.fields) & _CHAT_EVENT_COMMON_FIELDS)
        if restated:
            raise ChatContractDecodeError(
                "fields", f"envelope field(s) {restated} may not be restated as type-specific fields"
            )

        # An unrecognised eventType has no governed shape to validate against,
        # and never emits (see `to_wire`); its fields are inert, so carrying one
        # as governed would be the additive decode leaking into authority.
        reference = CHAT_EVENT_SCHEMA_REFS.get(self.event_type)
        if reference is None:
            if self.fields:
                raise ChatContractDecodeError(
                    "fields",
                    "an unrecognised eventType has no governed field set; its fields are inert "
                    "and belong in additive_fields",
                )
            return
        _require_governed_emission(self.to_wire(), reference, "ChatEvent")

    @property
    def is_known_event_type(self) -> bool:
        """Whether :attr:`event_type` is a member of this version's closed vocabulary."""
        return self.event_type in CHAT_EVENT_TYPES

    def to_wire(self) -> dict[str, Any]:
        """Emit the governed wire event, or refuse.

        An unrecognised ``event_type`` decodes but never emits: this version
        cannot state what the event means, so re-emitting it would hand a
        consumer a document this contract does not define. The caller answers
        with a ``ResnapshotResponse`` carrying ``unrecognised_event_type``
        instead (freeze §9 rule 5, rule 6). ``additive_fields`` is likewise
        never emitted, so a tolerated field cannot travel on as authority.
        """
        if not self.is_known_event_type:
            raise ChatContractDecodeError(
                "eventType",
                "strict emission refuses an eventType this version does not define; answer with a "
                "ResnapshotResponse reason 'unrecognised_event_type' instead",
            )
        wire: dict[str, Any] = {
            "eventId": self.event_id,
            "eventType": self.event_type,
            "schemaVersion": self.schema_version,
            "workspaceId": self.workspace_id,
            "conversationId": self.conversation_id,
            "occurredAt": self.occurred_at,
            "cursor": self.cursor,
        }
        wire.update(_thaw_json(self.fields))
        return wire

    @classmethod
    def from_wire(cls, payload: object, *, strict: bool = False, path: str = "ChatEvent") -> ChatEvent:
        """Decode a wire Chat event.

        ``strict=True`` mirrors the schema's own closed union: an eventType
        this version does not define, and any field the matched event type does
        not define, are both rejected. The default ``strict=False`` is the
        compatible decode: both are accepted, and both are held inert (see the
        class docstring).
        """
        mapping = _require_mapping(payload, path)
        event_type = _require_str(_require_field(mapping, "eventType", path), f"{path}.eventType")
        allowed = CHAT_EVENT_FIELDS.get(event_type)
        if strict and allowed is None:
            raise ChatContractDecodeError(
                f"{path}.eventType", "a strict decode admits only an eventType this version defines"
            )

        governed_names = set(allowed or ()) - _CHAT_EVENT_COMMON_FIELDS
        specific = {k: v for k, v in mapping.items() if k not in _CHAT_EVENT_COMMON_FIELDS}
        additive = {k: v for k, v in specific.items() if k not in governed_names}
        if strict and additive:
            raise ChatContractDecodeError(
                path, f"{len(additive)} field(s) this eventType does not define"
            )

        return cls(
            event_id=_require_str(_require_field(mapping, "eventId", path), f"{path}.eventId"),
            event_type=event_type,
            schema_version=_require_int(
                _require_field(mapping, "schemaVersion", path), f"{path}.schemaVersion"
            ),
            workspace_id=_require_str(_require_field(mapping, "workspaceId", path), f"{path}.workspaceId"),
            conversation_id=_require_str(
                _require_field(mapping, "conversationId", path), f"{path}.conversationId"
            ),
            occurred_at=_require_str(_require_field(mapping, "occurredAt", path), f"{path}.occurredAt"),
            cursor=_require_str(_require_field(mapping, "cursor", path), f"{path}.cursor"),
            fields={k: v for k, v in specific.items() if k in governed_names},
            additive_fields=additive,
        )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


_RESNAPSHOT_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {"workspaceId", "conversationId", "reason", "graphRevision", "resnapshotCursor"}
)


@dataclass(frozen=True, slots=True)
class ResnapshotResponse:
    """``events.schema.json#/$defs/ResnapshotResponse``.

    The stable response a snapshot/suffix reader returns in place of
    continued event delivery when a cursor is unknown, expired, unauthorized
    or gapped, or when :attr:`ChatEvent.is_known_event_type` is ``False``
    (``reason: unrecognised_event_type``) -- never a guessed continuation
    (freeze §9 rule 5).
    """

    workspace_id: str
    conversation_id: str
    reason: str
    graph_revision: int
    resnapshot_cursor: str

    def __post_init__(self) -> None:
        _require_strs(
            (
                ("workspaceId", self.workspace_id),
                ("conversationId", self.conversation_id),
                ("resnapshotCursor", self.resnapshot_cursor),
            )
        )
        _require_int(self.graph_revision, "graphRevision")
        _require_governed_emission(
            self.to_wire(), RECORD_VALIDATION_REFS["ResnapshotResponse"], "ResnapshotResponse"
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "workspaceId": self.workspace_id,
            "conversationId": self.conversation_id,
            "reason": self.reason,
            "graphRevision": self.graph_revision,
            "resnapshotCursor": self.resnapshot_cursor,
        }

    @classmethod
    def from_wire(cls, payload: object, *, strict: bool = False, path: str = "ResnapshotResponse") -> ResnapshotResponse:
        mapping = _require_mapping(payload, path)
        if strict:
            _reject_unknown_fields(mapping, _RESNAPSHOT_RESPONSE_FIELDS, path)
        return cls(
            workspace_id=_require_str(_require_field(mapping, "workspaceId", path), f"{path}.workspaceId"),
            conversation_id=_require_str(
                _require_field(mapping, "conversationId", path), f"{path}.conversationId"
            ),
            reason=_require_str(_require_field(mapping, "reason", path), f"{path}.reason"),
            graph_revision=_require_int(
                _require_field(mapping, "graphRevision", path), f"{path}.graphRevision"
            ),
            resnapshot_cursor=_require_str(
                _require_field(mapping, "resnapshotCursor", path), f"{path}.resnapshotCursor"
            ),
        )


# --------------------------------------------------------------------------
# F2a: ProviderInvocationRequest / ProviderInvocationRecord (provider.schema.json)
# --------------------------------------------------------------------------

_PROVIDER_INVOCATION_REQUEST_REQUIRED: Final[tuple[str, ...]] = (
    "attemptId", "classificationRef", "connectionId", "conversationId", "correlationId", "deadlineAt",
    "idempotencyKey", "invocationId", "jobId", "messages", "modelId", "operation", "policyRef",
    "requestedAt", "residencyRef", "responseFormat", "workspaceId",
)
_PROVIDER_INVOCATION_REQUEST_OPTIONAL: Final[tuple[str, ...]] = (
    "tools", "toolChoice", "generationOptions", "providerOptionsByNamespace", "causationId",
)
_PROVIDER_INVOCATION_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    _PROVIDER_INVOCATION_REQUEST_REQUIRED + _PROVIDER_INVOCATION_REQUEST_OPTIONAL
)
def _validate_route_evidence(evidence: Mapping[str, Any], path: str) -> None:
    """Enforce ``RouteEvidence``'s relational self-consistency (freeze §11 rule 7).

    ``fallback`` requires ``fallbackAuthorised: true`` and an ``admittedRoute``
    that differs from ``configuredPreference``; ``configured`` and
    ``same_route_retry`` require ``fallbackAuthorised: false`` and an
    ``admittedRoute`` that does not differ. JSON Schema cannot compare two
    nested sibling objects, which is why this is the one shape rule in this
    module the generated validation metadata cannot carry: without it a record
    carrying route evidence would be free to label a silently substituted route
    as the configured one -- exactly the undeclared fallback REF-019 §5.9
    forbids. The comparison is on the ``connectionId``/``modelId`` pair each
    names, never full-dict equality: ``admittedRoute`` always carries
    ``adapterName``/``adapterVersion`` that ``configuredPreference`` never has,
    so the two objects can never be equal even for the identical route.

    Runs after the emission gate, so every field it reads is already known to
    be present and of the governed shape.
    """
    decision = evidence["routeDecision"]
    authorised = evidence["fallbackAuthorised"]
    configured = evidence["configuredPreference"]
    admitted = evidence["admittedRoute"]
    changed = (configured["connectionId"], configured["modelId"]) != (
        admitted["connectionId"],
        admitted["modelId"],
    )
    if decision == "fallback":
        if authorised is not True:
            raise ChatContractDecodeError(
                f"{path}.fallbackAuthorised", "a fallback routeDecision requires fallbackAuthorised true"
            )
        if not changed:
            raise ChatContractDecodeError(
                f"{path}.admittedRoute",
                "a fallback routeDecision requires an admitted route that differs from configuredPreference",
            )
        return
    if authorised is not False:
        raise ChatContractDecodeError(
            f"{path}.fallbackAuthorised",
            "a configured or same_route_retry routeDecision requires fallbackAuthorised false",
        )
    if changed:
        raise ChatContractDecodeError(
            f"{path}.admittedRoute",
            "a changed admitted route may not be labelled configured or same_route_retry",
        )


@dataclass(frozen=True, slots=True)
class ProviderInvocationRequest:
    """``provider.schema.json#/$defs/ProviderInvocationRequest`` (F2a request projection).

    ``messages``, ``tools``, ``toolChoice``, ``responseFormat``,
    ``generationOptions`` and ``providerOptionsByNamespace`` are held as frozen
    JSON structures rather than nested dataclasses -- a *projection* of the
    schema's type surface, not of its rules. The rules still apply in full:
    construction validates the emitted document, so ``messages[].parts[]``'s
    per-kind conditional shape, ``ResponseFormat``'s ``schemaRef`` rule and
    ``ProviderOptionsByNamespace``'s bounded namespace names and sanitised
    scalar values are all enforced before this record exists.

    ``classificationRef``, ``residencyRef`` and ``policyRef`` are opaque
    governed strings only: they are held to the contract's own bounds and
    nothing more. This projection invents no classification, residency or
    policy semantics for them and never parses their content.
    """

    invocation_id: str
    workspace_id: str
    conversation_id: str
    job_id: str
    attempt_id: str
    connection_id: str
    model_id: str
    operation: str
    messages: tuple[Mapping[str, Any], ...]
    response_format: Mapping[str, Any]
    policy_ref: str
    classification_ref: str
    residency_ref: str
    idempotency_key: str
    correlation_id: str
    deadline_at: str
    requested_at: str
    tools: tuple[Mapping[str, Any], ...] | None = None
    tool_choice: Mapping[str, Any] | None = None
    generation_options: Mapping[str, Any] | None = None
    provider_options_by_namespace: Mapping[str, Any] | None = None
    causation_id: str | None = None

    def __post_init__(self) -> None:
        _require_strs(
            (
                ("invocationId", self.invocation_id),
                ("workspaceId", self.workspace_id),
                ("conversationId", self.conversation_id),
                ("jobId", self.job_id),
                ("attemptId", self.attempt_id),
                ("connectionId", self.connection_id),
                ("modelId", self.model_id),
                ("operation", self.operation),
                ("policyRef", self.policy_ref),
                ("classificationRef", self.classification_ref),
                ("residencyRef", self.residency_ref),
                ("idempotencyKey", self.idempotency_key),
                ("correlationId", self.correlation_id),
                ("deadlineAt", self.deadline_at),
                ("requestedAt", self.requested_at),
            )
        )
        if self.causation_id is not None:
            _require_str(self.causation_id, "causationId")
        object.__setattr__(self, "messages", _frozen_object_tuple(self.messages, "messages"))
        object.__setattr__(self, "response_format", _frozen_object(self.response_format, "responseFormat"))
        if self.tools is not None:
            object.__setattr__(self, "tools", _frozen_object_tuple(self.tools, "tools"))
        for attribute, wire_name in (
            ("tool_choice", "toolChoice"),
            ("generation_options", "generationOptions"),
            ("provider_options_by_namespace", "providerOptionsByNamespace"),
        ):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(self, attribute, _frozen_object(value, wire_name))
        _require_governed_emission(
            self.to_wire(),
            RECORD_VALIDATION_REFS["ProviderInvocationRequest"],
            "ProviderInvocationRequest",
        )

    @classmethod
    def from_wire(
        cls, payload: object, *, strict: bool = False, path: str = "ProviderInvocationRequest"
    ) -> ProviderInvocationRequest:
        """Decode a wire ``ProviderInvocationRequest``.

        Every optional field is decided by *presence*, never by shape: a
        present ``tools``/``toolChoice``/``generationOptions``/
        ``providerOptionsByNamespace`` of the wrong JSON shape is a malformed
        request and is rejected here, never quietly dropped to ``None`` as
        though the caller had omitted it. Silently discarding a malformed
        ``tools`` array would turn a tool-enabled request into a tool-free one
        the caller never asked for.
        """
        mapping = _require_mapping(payload, path)
        if strict:
            _reject_unknown_fields(mapping, _PROVIDER_INVOCATION_REQUEST_FIELDS, path)
        for name in _PROVIDER_INVOCATION_REQUEST_REQUIRED:
            _require_field(mapping, name, path)

        return cls(
            invocation_id=_require_str(mapping["invocationId"], f"{path}.invocationId"),
            workspace_id=_require_str(mapping["workspaceId"], f"{path}.workspaceId"),
            conversation_id=_require_str(mapping["conversationId"], f"{path}.conversationId"),
            job_id=_require_str(mapping["jobId"], f"{path}.jobId"),
            attempt_id=_require_str(mapping["attemptId"], f"{path}.attemptId"),
            connection_id=_require_str(mapping["connectionId"], f"{path}.connectionId"),
            model_id=_require_str(mapping["modelId"], f"{path}.modelId"),
            operation=_require_str(mapping["operation"], f"{path}.operation"),
            messages=_frozen_object_tuple(mapping["messages"], f"{path}.messages"),
            response_format=_frozen_object(mapping["responseFormat"], f"{path}.responseFormat"),
            policy_ref=_require_str(mapping["policyRef"], f"{path}.policyRef"),
            classification_ref=_require_str(mapping["classificationRef"], f"{path}.classificationRef"),
            residency_ref=_require_str(mapping["residencyRef"], f"{path}.residencyRef"),
            idempotency_key=_require_str(mapping["idempotencyKey"], f"{path}.idempotencyKey"),
            correlation_id=_require_str(mapping["correlationId"], f"{path}.correlationId"),
            deadline_at=_require_str(mapping["deadlineAt"], f"{path}.deadlineAt"),
            requested_at=_require_str(mapping["requestedAt"], f"{path}.requestedAt"),
            tools=(
                _frozen_object_tuple(mapping["tools"], f"{path}.tools") if "tools" in mapping else None
            ),
            tool_choice=(
                _frozen_object(mapping["toolChoice"], f"{path}.toolChoice")
                if "toolChoice" in mapping
                else None
            ),
            generation_options=(
                _frozen_object(mapping["generationOptions"], f"{path}.generationOptions")
                if "generationOptions" in mapping
                else None
            ),
            provider_options_by_namespace=(
                _frozen_object(
                    mapping["providerOptionsByNamespace"], f"{path}.providerOptionsByNamespace"
                )
                if "providerOptionsByNamespace" in mapping
                else None
            ),
            causation_id=_optional_str(mapping, "causationId", path),
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "invocationId": self.invocation_id,
            "workspaceId": self.workspace_id,
            "conversationId": self.conversation_id,
            "jobId": self.job_id,
            "attemptId": self.attempt_id,
            "connectionId": self.connection_id,
            "modelId": self.model_id,
            "operation": self.operation,
            "messages": [_thaw_json(item) for item in self.messages],
            "responseFormat": _thaw_json(self.response_format),
            "policyRef": self.policy_ref,
            "classificationRef": self.classification_ref,
            "residencyRef": self.residency_ref,
            "idempotencyKey": self.idempotency_key,
            "correlationId": self.correlation_id,
            "deadlineAt": self.deadline_at,
            "requestedAt": self.requested_at,
        }
        if self.tools is not None:
            wire["tools"] = [_thaw_json(item) for item in self.tools]
        if self.tool_choice is not None:
            wire["toolChoice"] = _thaw_json(self.tool_choice)
        if self.generation_options is not None:
            wire["generationOptions"] = _thaw_json(self.generation_options)
        if self.provider_options_by_namespace is not None:
            wire["providerOptionsByNamespace"] = _thaw_json(self.provider_options_by_namespace)
        if self.causation_id is not None:
            wire["causationId"] = self.causation_id
        return wire


_PROVIDER_INVOCATION_RECORD_REQUIRED: Final[tuple[str, ...]] = (
    "attemptIds", "connectionId", "conversationId", "createdAt", "generationAttemptId",
    "invocationId", "jobId", "lifecycleState", "modelId", "operation", "updatedAt", "workspaceId",
)
_PROVIDER_INVOCATION_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    _PROVIDER_INVOCATION_RECORD_REQUIRED + ("terminalAt", "routeEvidence", "reconciliationState")
)


@dataclass(frozen=True, slots=True)
class ProviderInvocationRecord:
    """``provider.schema.json#/$defs/ProviderInvocationRecord`` (F2a durable-record projection).

    ``routeEvidence``'s deep shape (``ProviderUsage``, ``EstimatedCost``, and
    so on) is preserved as a frozen JSON structure rather than decoded into its
    own nested dataclasses, but is validated in full on construction along with
    the rest of the emitted document: the lifecycle/terminal rule (a
    non-terminal record carries neither ``terminalAt`` nor ``routeEvidence``; a
    terminal record requires both; ``indeterminate`` requires
    ``reconciliationState`` and forbids ``routeEvidence``), the ``attemptIds``
    cardinality and uniqueness rules, and every nested shape inside the
    evidence are the schema's own, enforced from the approved bytes.

    The one rule added on top is route-evidence self-consistency
    (:func:`_validate_route_evidence`), which JSON Schema cannot state. This is
    a durable, terminal record of which Provider route actually ran, so it is
    held to the same invariant its trace-projected events prove: a record whose
    evidence labels a substituted route as the configured one is refused rather
    than written.
    """

    invocation_id: str
    workspace_id: str
    conversation_id: str
    job_id: str
    generation_attempt_id: str
    connection_id: str
    model_id: str
    operation: str
    attempt_ids: tuple[str, ...]
    lifecycle_state: str
    created_at: str
    updated_at: str
    terminal_at: str | None = None
    route_evidence: Mapping[str, Any] | None = None
    reconciliation_state: str | None = None

    def __post_init__(self) -> None:
        _require_strs(
            (
                ("invocationId", self.invocation_id),
                ("workspaceId", self.workspace_id),
                ("conversationId", self.conversation_id),
                ("jobId", self.job_id),
                ("generationAttemptId", self.generation_attempt_id),
                ("connectionId", self.connection_id),
                ("modelId", self.model_id),
                ("operation", self.operation),
                ("createdAt", self.created_at),
                ("updatedAt", self.updated_at),
            )
        )
        if self.terminal_at is not None:
            _require_str(self.terminal_at, "terminalAt")
        if self.reconciliation_state is not None:
            _require_str(self.reconciliation_state, "reconciliationState")
        object.__setattr__(self, "attempt_ids", _require_str_list(self.attempt_ids, "attemptIds"))
        if self.route_evidence is not None:
            object.__setattr__(
                self, "route_evidence", _frozen_object(self.route_evidence, "routeEvidence")
            )
        _require_governed_emission(
            self.to_wire(),
            RECORD_VALIDATION_REFS["ProviderInvocationRecord"],
            "ProviderInvocationRecord",
        )
        if self.route_evidence is not None:
            _validate_route_evidence(self.route_evidence, "routeEvidence")

    @classmethod
    def from_wire(
        cls, payload: object, *, strict: bool = False, path: str = "ProviderInvocationRecord"
    ) -> ProviderInvocationRecord:
        mapping = _require_mapping(payload, path)
        if strict:
            _reject_unknown_fields(mapping, _PROVIDER_INVOCATION_RECORD_FIELDS, path)
        for name in _PROVIDER_INVOCATION_RECORD_REQUIRED:
            _require_field(mapping, name, path)

        return cls(
            invocation_id=_require_str(mapping["invocationId"], f"{path}.invocationId"),
            workspace_id=_require_str(mapping["workspaceId"], f"{path}.workspaceId"),
            conversation_id=_require_str(mapping["conversationId"], f"{path}.conversationId"),
            job_id=_require_str(mapping["jobId"], f"{path}.jobId"),
            generation_attempt_id=_require_str(mapping["generationAttemptId"], f"{path}.generationAttemptId"),
            connection_id=_require_str(mapping["connectionId"], f"{path}.connectionId"),
            model_id=_require_str(mapping["modelId"], f"{path}.modelId"),
            operation=_require_str(mapping["operation"], f"{path}.operation"),
            attempt_ids=_require_str_list(mapping["attemptIds"], f"{path}.attemptIds"),
            lifecycle_state=_require_str(mapping["lifecycleState"], f"{path}.lifecycleState"),
            created_at=_require_str(mapping["createdAt"], f"{path}.createdAt"),
            updated_at=_require_str(mapping["updatedAt"], f"{path}.updatedAt"),
            terminal_at=_optional_str(mapping, "terminalAt", path),
            route_evidence=(
                _frozen_object(mapping["routeEvidence"], f"{path}.routeEvidence")
                if "routeEvidence" in mapping
                else None
            ),
            reconciliation_state=_optional_str(mapping, "reconciliationState", path),
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "invocationId": self.invocation_id,
            "workspaceId": self.workspace_id,
            "conversationId": self.conversation_id,
            "jobId": self.job_id,
            "generationAttemptId": self.generation_attempt_id,
            "connectionId": self.connection_id,
            "modelId": self.model_id,
            "operation": self.operation,
            "attemptIds": list(self.attempt_ids),
            "lifecycleState": self.lifecycle_state,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.terminal_at is not None:
            wire["terminalAt"] = self.terminal_at
        if self.route_evidence is not None:
            wire["routeEvidence"] = _thaw_json(self.route_evidence)
        if self.reconciliation_state is not None:
            wire["reconciliationState"] = self.reconciliation_state
        return wire
