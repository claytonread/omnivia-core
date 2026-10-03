"""Trusted client workflow for continuity-session registration.

``continuity.session.register`` is an adapter/SDK operation. A generic application
response is not a binding merely because it arrived: the response must correlate with
the request, carry the authenticated principal as server-issued authority, and return
the exact active workspace/repository binding the adapter asked for. This module puts
those checks in one shared client path so hosts do not reproduce them in shell code.

The peer's response and transport diagnostics are untrusted. They are inspected only
inside helpers that return a typed binding or a small internal outcome. A public
failure is then raised outside those helpers with a fixed sentence and no exception
chain, so response payloads, redirect headers, credentials, and injected transport
messages cannot become adapter or model-facing diagnostics.

This module follows redirects nowhere, retries nowhere, and discovers no endpoint.
It uses the already-connected :class:`~omnivia_core_client.ServiceClient`, whose
transport retains its existing endpoint and redirect policy.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Final, NoReturn

from omnivia_core.contracts.v1 import (
    IDENTIFIER_PATTERN,
    ClientIdentity,
    ContinuitySessionBinding,
    ContinuitySessionRegisterInput,
    ContinuitySessionRegisterResult,
    GrantedAuthority,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseMetadata,
    SuccessResponseEnvelope,
    canonical_timestamp_nanoseconds,
    codec,
    get_operation_metadata,
    validate_operation_request_metadata,
)
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.errors import (
    ContinuityRegistrationError,
    DeadlineExceededError,
    OperationCancelledError,
)
from omnivia_core_client.service_client import ServiceClient

__all__ = [
    "ContinuityRegistrationRequest",
    "register_continuity_session",
]


_OPERATION: Final = "continuity.session.register"
_PURPOSE: Final = "continuity_session"
_MAXIMUM_IDENTIFIER_CHARACTERS: Final = 128
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_FAILURE_MESSAGE: Final = (
    "the continuity registration did not return a trusted binding"
)
_DEADLINE_MESSAGE: Final = "the deadline passed during continuity registration"
_CANCELLED_MESSAGE: Final = "the continuity registration was cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class ContinuityRegistrationRequest:
    """Everything a trusted adapter expects one registration to bind.

    ``principal_id`` is still only a request claim on the wire. It becomes trusted
    here only when both the response's server-produced authority and the returned
    session binding state that exact principal. ``workspace_id`` is likewise checked
    against the connected descriptor and returned binding rather than trusted because
    this value says so.

    ``request_id`` and ``correlation_id`` remain separate because an adapter may group
    related attempts under one correlation while assigning each attempt its own request
    identity. Both must return unchanged.

    ``repr`` is deliberately disabled: the typed input may carry an opaque host session
    reference or checkout hint that has no reason to appear in a diagnostic.
    """

    input: ContinuitySessionRegisterInput
    principal_id: str
    workspace_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    idempotency_key: str
    client: ClientIdentity


def _raise_registration_failed() -> NoReturn:
    raise ContinuityRegistrationError(_FAILURE_MESSAGE)


def _raise_registration_deadline() -> NoReturn:
    raise DeadlineExceededError(_DEADLINE_MESSAGE)


def _raise_registration_cancelled() -> NoReturn:
    raise OperationCancelledError(_CANCELLED_MESSAGE)


def _identifier_is_valid(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= _MAXIMUM_IDENTIFIER_CHARACTERS
        and _IDENTIFIER_RE.fullmatch(value) is not None
    )


def _prepare_request(
    client: ServiceClient,
    registration: ContinuityRegistrationRequest,
    *,
    deadline: Deadline,
) -> tuple[str, RequestEnvelope | None, ContinuitySessionRegisterInput | None]:
    """Build one canonical request, returning only a payload-free outcome on failure."""
    try:
        if type(client) is not ServiceClient:
            return "invalid", None, None
        if type(registration) is not ContinuityRegistrationRequest:
            return "invalid", None, None
        if type(registration.input) is not ContinuitySessionRegisterInput:
            return "invalid", None, None
        if type(registration.client) is not ClientIdentity:
            return "invalid", None, None
        if not all(
            _identifier_is_valid(value)
            for value in (
                registration.principal_id,
                registration.workspace_id,
                registration.request_id,
                registration.correlation_id,
                registration.trace_id,
            )
        ):
            return "invalid", None, None
        if (
            not _identifier_is_valid(client.descriptor.workspace_id)
            or client.descriptor.workspace_id != registration.workspace_id
        ):
            return "invalid", None, None

        # A hand-built generated dataclass does not enforce its annotations. Decode
        # its own wire form before it reaches the transport, then retain this canonical
        # typed value for the exact repository-target comparison on the answer.
        canonical_input = ContinuitySessionRegisterInput.from_wire(
            registration.input.to_wire()
        )
        entry = get_operation_metadata(_OPERATION)
        metadata = RequestMetadata(
            request_id=registration.request_id,
            correlation_id=registration.correlation_id,
            trace_id=registration.trace_id,
            api_version=client.negotiated.api_version,
            client=registration.client,
            workspace_id=registration.workspace_id,
            scopes=tuple(entry.scope.required_scopes),
            purpose=_PURPOSE,
            deadline_ms=deadline.remaining_ms(),
            idempotency_key=registration.idempotency_key,
            required_capabilities=(entry.required_capability,),
            principal_claim=PrincipalClaim(
                claimed_principal_id=registration.principal_id
            ),
        )
        validate_operation_request_metadata(_OPERATION, metadata)
        request = RequestEnvelope(
            operation=_OPERATION,
            metadata=metadata,
            input=canonical_input.to_wire(),
        )
        # Re-decoding closes the direct-construction hole for the envelope and its
        # nested generated values before any bytes are written.
        request = RequestEnvelope.from_wire(request.to_wire())
    except DeadlineExceededError:
        return "expired", None, None
    except Exception:  # noqa: BLE001 -- caller values must not become diagnostics.
        return "invalid", None, None
    return "ready", request, canonical_input


def _send_registration(
    client: ServiceClient,
    request: RequestEnvelope,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None,
) -> tuple[str, object | None]:
    """Call the injected transport without retaining its exception or diagnostic."""
    answer: object | None = None
    outcome = "answered"
    try:
        answer = client.call(
            request,
            deadline=deadline,
            cancellation=cancellation,
        )
    except DeadlineExceededError:
        outcome = "expired"
    except OperationCancelledError:
        outcome = "cancelled"
    except Exception:  # noqa: BLE001 -- transport diagnostics are peer-controlled.
        outcome = "failed"
    return outcome, answer


def _admit_registration_response(
    response: object,
    registration: ContinuityRegistrationRequest,
    requested_input: ContinuitySessionRegisterInput,
    *,
    expected_api_version: str,
) -> ContinuitySessionBinding | None:
    """Return the binding only when every authority and freshness check agrees.

    The helper absorbs every decode/validation failure and returns ``None``. Its
    response-bearing frame is gone before the public function raises, keeping the raw
    object and any parser diagnostic out of the resulting exception chain.
    """
    try:
        if type(response) is not SuccessResponseEnvelope:
            return None
        # Transport implementations normally decode with the public codec. Repeat
        # that admission here because ``ServiceClient`` also accepts injected
        # transports and generated dataclasses can be built directly with values
        # that violate their annotations. This gives the workflow one invariant
        # regardless of which conforming transport carried the exchange.
        canonical_response = codec.decode_success_response(response.to_wire())
        metadata = canonical_response.metadata
        if type(metadata) is not ResponseMetadata:
            return None
        if (
            not _identifier_is_valid(metadata.request_id)
            or not _identifier_is_valid(metadata.correlation_id)
            or metadata.request_id != registration.request_id
            or metadata.correlation_id != registration.correlation_id
            or metadata.version.api_version != expected_api_version
            or metadata.version.compatibility.selected_api_version
            != expected_api_version
        ):
            return None
        authority = metadata.authority
        if type(authority) is not GrantedAuthority:
            return None
        if authority.principal_id != registration.principal_id:
            return None

        result = ContinuitySessionRegisterResult.from_wire(canonical_response.result)
        binding = result.session
        if not _identifier_is_valid(binding.session_id):
            return None
        if (
            not _identifier_is_valid(binding.principal_id)
            or binding.principal_id != registration.principal_id
            or binding.principal_id != authority.principal_id
        ):
            return None
        if (
            not _identifier_is_valid(binding.workspace_id)
            or binding.workspace_id != registration.workspace_id
        ):
            return None
        if binding.repository_target != requested_input.repository_target:
            return None
        if binding.state != "active":
            return None
        if type(binding.binding_generation) is not int or binding.binding_generation < 1:
            return None
        lease_ns = canonical_timestamp_nanoseconds(
            binding.lease_expires_at, "lease_expires_at"
        )
        if lease_ns <= time.time_ns():
            return None
    except Exception:  # noqa: BLE001 -- response/parser text must not escape.
        return None
    return binding


def register_continuity_session(
    client: ServiceClient,
    registration: ContinuityRegistrationRequest,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> ContinuitySessionBinding:
    """Register and return one fully validated active continuity binding.

    The original ``deadline`` and ``cancellation`` objects are passed to the connected
    client's transport. There is no retry: an uncertain mutation result is not sent a
    second time here, although the required idempotency key lets an adapter deliberately
    reconcile through a later call.

    Every non-authoritative outcome fails closed. Deadline and cancellation retain
    their public error categories; all other request, transport, application-response,
    shape, identity, authority, target, generation, state, and lease failures collapse
    to :class:`ContinuityRegistrationError` with one fixed sentence.
    """
    prepared, request, requested_input = _prepare_request(
        client, registration, deadline=deadline
    )
    if prepared == "expired":
        _raise_registration_deadline()
    if prepared != "ready" or request is None or requested_input is None:
        _raise_registration_failed()

    outcome, response = _send_registration(
        client,
        request,
        deadline=deadline,
        cancellation=cancellation,
    )
    if outcome == "expired":
        _raise_registration_deadline()
    if outcome == "cancelled":
        _raise_registration_cancelled()
    if outcome != "answered" or response is None:
        _raise_registration_failed()

    binding = _admit_registration_response(
        response,
        registration,
        requested_input,
        expected_api_version=request.metadata.api_version,
    )
    # Do not keep the raw response live in the frame that raises a public failure.
    response = None
    if binding is None:
        _raise_registration_failed()
    return binding
