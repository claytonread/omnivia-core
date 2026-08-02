"""Structural routing of one decoded protocol document (P2.1).

A decoded JSON object arriving on the OVC1 protocol is one of exactly two things:
an application request (`RequestEnvelope`, identified by its `operation` field) or
a runtime probe (`ServiceProbeRequest`, identified by its `probe` field). This
module decides which, and nothing else.

The decision is made **structurally and before dispatch**. A document carrying
both fields is refused, and so is one carrying neither: the two shapes are
answered by different subsystems under different rules -- one authorizes a caller
against a workspace, the other answers before authentication exists -- so a
document that could plausibly be either has to be refused rather than resolved by
precedence. Picking a winner would let a caller choose which set of rules their
document is judged under.

Decoding is delegated to the public `from_wire` decoders. This module holds no
schema knowledge of its own: it looks for the presence of two field names and
hands the whole document to the contract, which is what stops a second, drifting
copy of the envelope shape from growing here.

What this module deliberately does **not** do:

* No byte, framing or network I/O. It receives an already-decoded object; the
  OVC1 and HTTP adapters own transport, length prefixes and connection lifetime.
* No authentication and no authority. Application dispatch is an injected
  callable, so this slice constructs no grant and stands up no unauthenticated
  universal dispatcher.
* No exception-to-error translation. A malformed document raises the contract's
  own `ContractDecodeError` and a structural refusal raises `ProtocolError`;
  neither is converted into an `ErrorResponseEnvelope` here. Containing failures
  and shaping them for the wire belongs to the transport that knows what the
  caller can be told.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, TypeAlias

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    SuccessResponseEnvelope,
)
from omnivia_core_runtime.service.probes import ProbeRouter

#: The field whose presence marks a document as an application request.
OPERATION_FIELD: Final = "operation"

#: The field whose presence marks a document as a runtime probe.
PROBE_FIELD: Final = "probe"

#: Application dispatch, injected. A placeholder in this slice: it receives the
#: decoded request and returns an accepted `ResponseEnvelope`. Authorization,
#: workspace selection and the handlers themselves are later, separate work.
ApplicationDispatch: TypeAlias = Callable[[RequestEnvelope], ResponseEnvelope]

#: What routing produces. The union is kept explicit rather than widened to
#: `object`: a probe result is not a response envelope, and a caller must handle
#: both branches knowingly.
RoutedResult: TypeAlias = ResponseEnvelope | ServiceProbeResult


class ProtocolError(Exception):
    """A document could not be routed, or a dispatch result could not be trusted.

    Distinct from the contract's `ContractDecodeError`, which means the document
    was the wrong shape for the branch it selected. This means the branch itself
    could not be chosen, or that what came back from application dispatch does not
    answer the request that was routed to it.
    """


@dataclass(frozen=True)
class DocumentRouter:
    """Routes one decoded document to application dispatch or the probe router."""

    probes: ProbeRouter
    dispatch: ApplicationDispatch

    def route(self, document: object) -> RoutedResult:
        """Route `document`, or raise :class:`ProtocolError`.

        Contract decode failures from the selected branch propagate as the
        contract's own `ContractDecodeError`; they are not re-raised as a protocol
        error, because the caller learns more from the field path the decoder
        already reports than from anything this layer could add.
        """
        mapping = _require_object(document)
        has_operation = OPERATION_FIELD in mapping
        has_probe = PROBE_FIELD in mapping

        if has_operation and has_probe:
            raise ProtocolError(
                f"document carries both {OPERATION_FIELD!r} and {PROBE_FIELD!r}: an "
                "application request and a probe are answered under different rules, "
                "so a document claiming to be both is refused"
            )
        if not has_operation and not has_probe:
            raise ProtocolError(
                f"document carries neither {OPERATION_FIELD!r} nor {PROBE_FIELD!r}: "
                "nothing identifies it as an application request or a probe"
            )

        if has_probe:
            return self.probes.route(ServiceProbeRequest.from_wire(mapping))

        request = RequestEnvelope.from_wire(mapping)
        response = self.dispatch(request)
        _require_answering_response(request, response)
        return response


def _require_object(document: object) -> Mapping[str, object]:
    """Narrow `document` to a JSON object, refusing anything else.

    A protocol document is an object. An array, a string or a number never
    identifies a branch, so it is refused here rather than handed to a decoder
    that would report it as a shape error against whichever branch was guessed.
    """
    if not isinstance(document, Mapping):
        raise ProtocolError(
            f"protocol document must be a JSON object, got {type(document).__name__}"
        )
    for key in document:
        if not isinstance(key, str):
            raise ProtocolError("protocol document keys must be strings")
    return document


def _require_answering_response(
    request: RequestEnvelope, response: ResponseEnvelope
) -> None:
    """Refuse an injected response that does not answer `request`.

    `ResponseMetadata` requires `request_id` and `correlation_id`, and the public
    semantics are that they are the request's own -- that is the whole basis on
    which a caller matches an answer to what it asked. A mismatched pair is a
    dispatch bug, and letting it through would surface as a client correlating an
    answer to the wrong request.

    Checked, never corrected: the envelope is returned exactly as dispatch built
    it. Rewriting the metadata here would hide the bug and forge the correlation
    it was supposed to prove.
    """
    if not isinstance(response, SuccessResponseEnvelope | ErrorResponseEnvelope):
        raise ProtocolError(
            "application dispatch returned "
            f"{type(response).__name__}, not a ResponseEnvelope"
        )
    metadata = response.metadata
    if metadata.request_id != request.metadata.request_id:
        raise ProtocolError(
            "application dispatch returned a response whose request_id does not "
            "match the routed request"
        )
    if metadata.correlation_id != request.metadata.correlation_id:
        raise ProtocolError(
            "application dispatch returned a response whose correlation_id does not "
            "match the routed request"
        )


__all__ = [
    "OPERATION_FIELD",
    "PROBE_FIELD",
    "ApplicationDispatch",
    "DocumentRouter",
    "ProtocolError",
    "RoutedResult",
]
