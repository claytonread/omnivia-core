"""Wire codec and retry semantics for the Application Contract v1 (ADR-038).

The generated dataclasses in :mod:`omnivia_core.contracts.v1.generated` own the
per-type ``to_wire`` / ``from_wire`` pair. This module owns the three things
that sit around them:

- **deterministic serialization** - a canonical JSON form, so two encoders that
  agree on the value agree byte for byte;
- **response-branch dispatch** - decoding a response envelope into exactly one
  of its success or error branches;
- **retry semantics** - the frozen classification of every v1 error code, and
  the fail-safe rule for anything outside it.

The generated structural pair (``response_envelope_from_wire`` /
``response_envelope_to_wire``) is re-exported here explicitly, for the callers
that need to inspect a payload's *shape* without the semantic layer
:func:`decode_response` adds on top of it. Production decoding is
:func:`decode_response`; the structural decoder is the deliberate exception,
not the default.

The decoding posture is deliberately asymmetric to the schemas. Strict JSON
Schema conformance rejects unknown fields, because that is what a conformance
gate is for. Production decoding here *ignores* unknown fields and *preserves*
unknown open string values, because that is what forward compatibility with a
newer peer requires. Both behaviours are part of the contract; neither is a
relaxation of the other.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from omnivia_core.contracts.v1.compatibility import (
    validate_granted_authority,
    validate_version_capability_envelope,
)
from omnivia_core.contracts.v1.generated import (
    CONTRACT_VERSION,
    DEFAULT_RETRY_CLASSIFICATION,
    FROZEN_ERROR_CODES,
    FROZEN_RETRY_CLASSES,
    RETRY_CLASS_NON_RETRYABLE,
    RETRYABLE_RETRY_CLASSES,
    SCHEMA_BASE_URI,
    ApiError,
    ContractDecodeError,
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    response_envelope_from_wire,
    response_envelope_to_wire,
)

__all__ = [
    "CONTRACT_VERSION",
    "FROZEN_ERROR_CODES",
    "FROZEN_RETRY_CLASSES",
    "SCHEMA_BASE_URI",
    "ContractDecodeError",
    "RetryClassMismatchError",
    "decode_error_response",
    "decode_json_document",
    "decode_request",
    "decode_response",
    "decode_success_response",
    "encode_error_response",
    "encode_request",
    "encode_response",
    "encode_success_response",
    "is_error_retryable",
    "is_retryable",
    "response_envelope_from_wire",
    "response_envelope_to_wire",
    "response_error",
    "response_result",
    "retry_after_ms",
    "retry_class_for",
    "to_canonical_json",
    "to_canonical_json_document",
    "validate_error_retry_class",
    "validate_error_value_domain",
]


# --------------------------------------------------------------------------
# Deterministic JSON
# --------------------------------------------------------------------------


def to_canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize a wire mapping to the compact canonical JSON form.

    Keys are sorted and separators are fixed, so the same value always produces
    the same bytes regardless of how the mapping was built. ``NaN`` and the
    infinities are rejected rather than emitted as the non-standard literals
    Python would otherwise produce.
    """
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
    newline-terminated so checked-in fixtures diff cleanly.
    """
    text = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    )
    return f"{text}\n"


def decode_json_document(text: str) -> Any:
    """Parse a JSON document, rejecting the non-standard ``NaN``/``Infinity`` literals."""
    return json.loads(text, parse_constant=_reject_json_constant)


def _reject_json_constant(name: str) -> Any:
    raise ContractDecodeError(f"{name!r} is not valid JSON")


# --------------------------------------------------------------------------
# Envelope codec
# --------------------------------------------------------------------------


def decode_request(payload: object) -> RequestEnvelope:
    """Decode a request envelope, ignoring unknown fields."""
    return RequestEnvelope.from_wire(payload)


def encode_request(envelope: RequestEnvelope) -> dict[str, Any]:
    """Encode a request envelope to its wire mapping."""
    return envelope.to_wire()


def decode_response(payload: object) -> ResponseEnvelope:
    """Decode a response envelope into exactly one of its two branches.

    A payload carrying both ``result`` and ``error``, or neither, is rejected.
    The exclusivity is never resolved by preferring one branch.

    Beyond that structural dispatch, this production decode path also enforces
    the semantic invariants a caller must not be able to forget:

    - the envelope's version windows and capability set are validated
      (:func:`~omnivia_core.contracts.v1.compatibility.validate_version_capability_envelope`),
      so a capability set can never assert wider authority than ``supported``
      and ``granted`` actually agree on;
    - the trusted ``authority`` is validated against that negotiated result
      (:func:`~omnivia_core.contracts.v1.compatibility.validate_granted_authority`),
      so the one authority statement a client may trust can never exceed what
      the exchange made effective -- validating only the capability set would
      close the widening path on ``effective`` while leaving it open on the very
      field callers act on;
    - on the error branch, the error's open string and duration values are held
      to the schema's value domain (:func:`validate_error_value_domain`), so an
      unbounded message, an out-of-range backoff, or a code that is not a
      well-formed open token cannot reach a caller;
    - on the error branch, the stated ``retry_class`` is checked against the
      frozen classification for known error codes
      (:func:`validate_error_retry_class`), so a peer cannot smuggle in a
      mismatched retry class for a code this build already knows.
    """
    envelope = response_envelope_from_wire(payload)
    metadata = envelope.metadata
    validate_version_capability_envelope(metadata.version)
    validate_granted_authority(metadata.authority, metadata.version.capabilities)
    if isinstance(envelope, ErrorResponseEnvelope):
        validate_error_value_domain(envelope.error)
        validate_error_retry_class(envelope.error)
    return envelope


def encode_response(envelope: ResponseEnvelope) -> dict[str, Any]:
    """Encode either response branch to its wire mapping."""
    return response_envelope_to_wire(envelope)


def decode_success_response(payload: object) -> SuccessResponseEnvelope:
    """Decode a payload that must be the success branch."""
    envelope = decode_response(payload)
    if not isinstance(envelope, SuccessResponseEnvelope):
        raise ContractDecodeError("ResponseEnvelope: expected the success branch, got an error")
    return envelope


def decode_error_response(payload: object) -> ErrorResponseEnvelope:
    """Decode a payload that must be the error branch."""
    envelope = decode_response(payload)
    if not isinstance(envelope, ErrorResponseEnvelope):
        raise ContractDecodeError("ResponseEnvelope: expected the error branch, got a success")
    return envelope


def encode_success_response(envelope: SuccessResponseEnvelope) -> dict[str, Any]:
    """Encode a success response to its wire mapping."""
    return envelope.to_wire()


def encode_error_response(envelope: ErrorResponseEnvelope) -> dict[str, Any]:
    """Encode an error response to its wire mapping."""
    return envelope.to_wire()


def response_result(envelope: ResponseEnvelope) -> Mapping[str, Any] | None:
    """Return the result payload, or ``None`` when the response is an error."""
    if isinstance(envelope, SuccessResponseEnvelope):
        return envelope.result
    return None


def response_error(envelope: ResponseEnvelope) -> ApiError | None:
    """Return the error, or ``None`` when the response is a success."""
    if isinstance(envelope, ErrorResponseEnvelope):
        return envelope.error
    return None


# --------------------------------------------------------------------------
# Retry semantics
# --------------------------------------------------------------------------


class RetryClassMismatchError(ContractDecodeError):
    """Raised when a known v1 error code's stated ``retry_class`` contradicts its frozen
    classification.

    ``ErrorCode`` and ``RetryClass`` are independently open strings on the
    wire, so nothing at the structural-decoding layer stops a peer from
    pairing a *known* code with the *wrong* class. A known code's retry
    behaviour is frozen by ADR-038 precisely so callers do not have to
    re-derive it per response; a peer contradicting that freeze is a protocol
    violation, not a value this build should trust.
    """


def retry_class_for(code: str) -> str:
    """Return the frozen retry classification for an error code.

    ``ErrorCode`` is an open string, so a newer peer may send a code this build
    has never seen. There is no safe way to guess how such a failure behaves,
    so an unknown code classifies as non-retryable.
    """
    return DEFAULT_RETRY_CLASSIFICATION.get(code, RETRY_CLASS_NON_RETRYABLE)


def is_retryable(retry_class: str) -> bool:
    """Return whether a retry class permits retrying the identical request.

    Fails safe in both directions of ignorance: an unrecognized class is not
    retryable, and a recognized class that requires the caller to rebuild state
    first (rather than resend as-is) is not retryable either.
    """
    return retry_class in RETRYABLE_RETRY_CLASSES


# An ``ErrorCode``/``RetryClass`` open token: 1..128 chars, ``^[a-z][a-z0-9_]*$``.
# Matched with ``fullmatch``, which anchors both ends without Python's ``$``
# also accepting a trailing newline -- the case the schema pattern's
# ``$(?![\s\S])`` tail exists to exclude.
_OPEN_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}")
_MAX_MESSAGE_LENGTH = 2048
_MAX_DURATION_MS = 86_400_000


def validate_error_value_domain(error: ApiError) -> None:
    """Raise :class:`ContractDecodeError` if an error's values fall outside the
    schema's value domain.

    ``from_wire`` is deliberately structural: it checks that ``code`` is a
    string, not that it is a *well-formed* one. Openness of ``ErrorCode`` and
    ``RetryClass`` is about unknown vocabulary, not about arbitrary text, so
    the bounds and pattern still apply to values this build has never seen.
    Diagnostics name the field and the rule but never echo the value, which is
    untrusted peer input.
    """
    _validate_open_token("code", error.code)
    _validate_open_token("retry_class", error.retry_class)
    if len(error.message) > _MAX_MESSAGE_LENGTH:
        raise ContractDecodeError(
            f"ApiError.message: exceeds the maximum of {_MAX_MESSAGE_LENGTH} characters"
        )
    delay = error.retry_after_ms
    if delay is not None and not 0 <= delay <= _MAX_DURATION_MS:
        raise ContractDecodeError(
            f"ApiError.retry_after_ms: must be between 0 and {_MAX_DURATION_MS}"
        )


def _validate_open_token(field: str, value: str) -> None:
    if _OPEN_TOKEN_PATTERN.fullmatch(value) is None:
        raise ContractDecodeError(
            f"ApiError.{field}: must be 1-128 characters matching ^[a-z][a-z0-9_]*$"
        )


def validate_error_retry_class(error: ApiError) -> None:
    """Raise :class:`RetryClassMismatchError` if a known error code's stated
    ``retry_class`` contradicts its frozen v1 classification.

    An error code outside :data:`FROZEN_ERROR_CODES` is exempt: its retry
    behaviour is not frozen by this build, so any stated class is the peer's
    to define.
    """
    frozen_class = DEFAULT_RETRY_CLASSIFICATION.get(error.code)
    if frozen_class is not None and frozen_class != error.retry_class:
        raise RetryClassMismatchError(
            f"error code {error.code!r} is frozen to retry_class {frozen_class!r}, "
            f"got {error.retry_class!r}"
        )


def is_error_retryable(error: ApiError) -> bool:
    """Return whether an error may be retried by resending the identical request.

    A known v1 error code's retry behaviour is frozen and cannot be overridden
    by the wire value: if ``error.retry_class`` contradicts this build's frozen
    classification for ``error.code``, this fails safe as non-retryable rather
    than trusting either value. Only for a code outside the frozen vocabulary
    does the server's stated ``retry_class`` win, and even then an unrecognized
    class is still decided conservatively (see :func:`is_retryable`).
    """
    frozen_class = DEFAULT_RETRY_CLASSIFICATION.get(error.code)
    if frozen_class is not None and frozen_class != error.retry_class:
        return False
    return is_retryable(error.retry_class)


def retry_after_ms(error: ApiError) -> int | None:
    """Return the minimum backoff the server asked for, if it stated one."""
    return error.retry_after_ms
