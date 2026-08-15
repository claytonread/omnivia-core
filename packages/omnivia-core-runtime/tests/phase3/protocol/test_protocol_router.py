"""P2.1: structural routing of one decoded protocol document.

The router's whole job is choosing a branch. These tests pin that it chooses it
*structurally* -- on the presence of `operation` or `probe` and nothing else --
that a document claiming both or neither is refused before anything is dispatched,
and that the choosing is the end of its responsibilities: it decodes through the
public `from_wire` decoders rather than a second copy of the schema, it hands
application requests to an injected dispatcher rather than authorizing anything
itself, and it never turns a raised exception into an application error envelope.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service.operations import success
from omnivia_core_runtime.service.probes import (
    PROBE_DISCOVER,
    PROBE_HEALTH,
    ProbeError,
    ProbeRouter,
    ServiceFacts,
)
from omnivia_core_runtime.service.protocol import (
    OPERATION_FIELD,
    PROBE_FIELD,
    DocumentRouter,
    ProtocolError,
)
from omnivia_core_runtime.service.versions import API_VERSION

from omnivia_core.contracts.v1 import (
    OPERATION_CATALOGUE,
    ApiError,
    ClientIdentity,
    ContractDecodeError,
    ErrorResponseEnvelope,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    ServiceComponentStatus,
    ServiceProbeResult,
    SuccessResponseEnvelope,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"
OPERATION = min(entry.name for entry in OPERATION_CATALOGUE)


def _facts() -> ServiceFacts:
    return ServiceFacts(
        observed_at=OBSERVED_AT,
        health_status="pass",
        readiness_status="pass",
        discovery_status="pass",
    )


def _probe_router() -> ProbeRouter:
    return ProbeRouter(facts=_facts, capabilities=tuple, clock=lambda: 0)


def _request(
    *,
    operation: str = OPERATION,
    request_id: str = "req-1",
    correlation_id: str = "corr-1",
) -> RequestEnvelope:
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=correlation_id,
            trace_id="trace-1",
            api_version=API_VERSION,
            client=ClientIdentity(id="test-client", version="0.1.0"),
            scopes=("workspace:read",),
            purpose="test",
            required_capabilities=(),
            workspace_id="ws-1",
        ),
        input={},
    )


class RecordingDispatch:
    """An injected application dispatcher. A placeholder, exactly as this slice allows.

    It records what it was handed so a test can prove the router passed the decoded
    request through untouched, and it constructs no grant of any kind.
    """

    def __init__(self, response: ResponseEnvelope | None = None) -> None:
        self.response = response
        self.seen: list[RequestEnvelope] = []

    def __call__(self, request: RequestEnvelope) -> ResponseEnvelope:
        self.seen.append(request)
        if self.response is not None:
            return self.response
        return success(request, {"ok": True}, principal="test-principal")


def _router(dispatch: RecordingDispatch | None = None) -> DocumentRouter:
    return DocumentRouter(
        probes=_probe_router(), dispatch=dispatch or RecordingDispatch()
    )


def _succeeded(request: RequestEnvelope) -> SuccessResponseEnvelope:
    """`success()` answering `request`, narrowed to the branch it actually returns.

    Its declared return type is the `ResponseEnvelope` union, which is right for the
    dispatch contract and wrong for a test that needs to read `result` off the
    answer. Narrowed by assertion rather than by an ignore, so the day `success`
    starts returning an error envelope this fails where the mistake is.
    """
    answer = success(request, {"ok": True})
    assert isinstance(answer, SuccessResponseEnvelope)
    return answer


# --- exactly one branch -------------------------------------------------------


def test_an_operation_document_routes_to_application_dispatch() -> None:
    dispatch = RecordingDispatch()
    request = _request()

    result = _router(dispatch).route(request.to_wire())

    assert isinstance(result, SuccessResponseEnvelope)
    assert [seen.operation for seen in dispatch.seen] == [OPERATION]
    assert dispatch.seen[0] == request


def test_a_probe_document_routes_to_the_canonical_probe_router() -> None:
    dispatch = RecordingDispatch()

    result = _router(dispatch).route({PROBE_FIELD: PROBE_HEALTH})

    assert isinstance(result, ServiceProbeResult)
    assert result.probe == PROBE_HEALTH
    assert result.api_version == API_VERSION
    # A probe is answered outside the application path entirely.
    assert dispatch.seen == []


def test_a_probe_document_carrying_a_request_id_and_deadline_is_routed_whole() -> None:
    result = _router().route(
        {PROBE_FIELD: PROBE_DISCOVER, "request_id": "req-9", "deadline_ms": 500}
    )
    assert isinstance(result, ServiceProbeResult)
    assert result.probe == PROBE_DISCOVER
    assert result.details == {"request_id": "req-9"}


def test_a_document_claiming_both_branches_is_refused_before_dispatch() -> None:
    dispatch = RecordingDispatch()
    document = dict(_request().to_wire())
    document[PROBE_FIELD] = PROBE_HEALTH

    with pytest.raises(ProtocolError, match="both"):
        _router(dispatch).route(document)
    assert dispatch.seen == []


def test_a_document_claiming_neither_branch_is_refused_before_dispatch() -> None:
    dispatch = RecordingDispatch()

    documents: tuple[dict[str, object], ...] = (
        {},
        {"metadata": {}, "input": {}},
        {"status": "pass"},
    )
    for document in documents:
        with pytest.raises(ProtocolError, match="neither"):
            _router(dispatch).route(document)
    assert dispatch.seen == []


def test_the_branch_is_chosen_on_field_presence_not_on_field_value() -> None:
    """A `probe` field present but null still selects the probe branch.

    Selection has to be structural: deciding on truthiness would let a document
    with an empty or null `operation` slide into the probe branch and be answered
    under rules it was never checked against.
    """
    with pytest.raises(ContractDecodeError, match="probe"):
        _router().route({PROBE_FIELD: None})
    with pytest.raises(ContractDecodeError, match="operation"):
        _router().route({OPERATION_FIELD: None, "metadata": {}, "input": {}})


@pytest.mark.parametrize(
    "document", [None, [], ["operation"], "operation", 7, 1.5, True, b"{}", set()]
)
def test_a_document_that_is_not_a_json_object_is_refused(document: object) -> None:
    with pytest.raises(ProtocolError, match="must be a JSON object"):
        _router().route(document)


def test_non_string_keys_are_refused_rather_than_coerced() -> None:
    with pytest.raises(ProtocolError, match="keys must be strings"):
        _router().route({1: "operation"})


# --- decoding is the public contract's job ------------------------------------


def test_decoding_uses_the_public_decoder_and_tolerates_unknown_fields() -> None:
    """A newer peer's additive field decodes here, because the contract says so."""
    document = dict(_request().to_wire())
    document["a_field_from_a_newer_peer"] = {"anything": True}

    result = _router().route(document)
    assert isinstance(result, SuccessResponseEnvelope)


def test_a_malformed_document_raises_the_contracts_own_decode_error() -> None:
    """Not re-raised as a ProtocolError, and never turned into an error envelope."""
    dispatch = RecordingDispatch()
    with pytest.raises(ContractDecodeError) as raised:
        _router(dispatch).route({OPERATION_FIELD: OPERATION, "input": {}})

    assert not isinstance(raised.value, ProtocolError)
    assert "metadata" in str(raised.value)
    assert dispatch.seen == []


def test_a_malformed_probe_document_raises_the_contracts_own_decode_error() -> None:
    with pytest.raises(ContractDecodeError, match="probe"):
        _router().route({PROBE_FIELD: 7})


def test_an_unknown_probe_kind_stays_a_probe_error_not_an_error_envelope() -> None:
    with pytest.raises(ProbeError, match="unknown probe"):
        _router().route({PROBE_FIELD: "core.health"})


# --- injected application responses -------------------------------------------


def test_a_dispatch_result_that_is_not_a_response_envelope_is_refused() -> None:
    class BadDispatch:
        def __call__(self, request: RequestEnvelope) -> Any:
            return {"metadata": {}, "result": {}}

    router = DocumentRouter(probes=_probe_router(), dispatch=BadDispatch())
    with pytest.raises(ProtocolError, match="not a ResponseEnvelope"):
        router.route(_request().to_wire())


def test_a_dispatch_result_answering_a_different_request_id_is_refused() -> None:
    request = _request()
    answer = _succeeded(request)
    mismatched = SuccessResponseEnvelope(
        metadata=replace(answer.metadata, request_id="req-someone-else"),
        result=answer.result,
    )

    router = _router(RecordingDispatch(mismatched))
    with pytest.raises(ProtocolError, match="request_id"):
        router.route(request.to_wire())


def test_a_dispatch_result_answering_a_different_correlation_is_refused() -> None:
    request = _request()
    answer = _succeeded(request)
    mismatched = SuccessResponseEnvelope(
        metadata=replace(answer.metadata, correlation_id="corr-someone-else"),
        result=answer.result,
    )

    router = _router(RecordingDispatch(mismatched))
    with pytest.raises(ProtocolError, match="correlation_id"):
        router.route(request.to_wire())


def test_an_error_envelope_from_dispatch_is_returned_unchanged() -> None:
    """An application error is dispatch's answer, not a routing failure."""
    request = _request()
    answer = ErrorResponseEnvelope(
        metadata=success(request, {}).metadata,
        error=ApiError(
            code="not_found", message="nothing here", retry_class="non_retryable"
        ),
    )

    result = _router(RecordingDispatch(answer)).route(request.to_wire())
    assert result is answer


def test_a_matching_response_is_returned_without_being_mutated() -> None:
    request = _request()
    answer = success(request, {"ok": True}, principal="test-principal")
    before = answer.to_wire()

    result = _router(RecordingDispatch(answer)).route(request.to_wire())

    assert result is answer
    assert result.to_wire() == before


def test_the_router_constructs_no_grant_and_authenticates_nothing() -> None:
    """Dispatch is a placeholder here; authority stays entirely on its side."""
    dispatch = RecordingDispatch()
    _router(dispatch).route(_request().to_wire())

    seen = dispatch.seen[0]
    # What went in is what came out of the decoder: no principal was attached, no
    # capability was resolved, and no metadata was rewritten on the way through.
    assert seen == _request()
    assert seen.metadata.principal_claim is None
    assert seen.metadata.required_capabilities == ()


# --- every failure leaves as one of three named errors -------------------------
#
# Routing has exactly three ways to refuse: `ProtocolError` when no branch can be
# chosen or a dispatch result does not answer, `ContractDecodeError` when the
# chosen branch's decoder refuses the document, and `ProbeError` when the probe
# boundary refuses the request or the snapshot behind it. Anything else escaping
# here -- a `TypeError` from a regex handed a non-string, an `AttributeError` from
# a field that was never there -- is the same refusal to the caller and a different
# exception to the transport that has to decide what to send back.

ROUTING_ERRORS = (ProtocolError, ContractDecodeError, ProbeError)

SECRET_PATH = "/Users/someone/Library/omnivia/workspace.sqlite3"


def _poisoned_probe_router() -> ProbeRouter:
    """A probe router whose injected snapshot is exactly what must never travel."""
    facts = ServiceFacts(
        observed_at=OBSERVED_AT,
        health_status="pass",
        readiness_status="pass",
        discovery_status="pass",
        components=(
            ServiceComponentStatus(
                id=SECRET_PATH, status="pass", observed_at=OBSERVED_AT
            ),
        ),
    )
    return ProbeRouter(facts=lambda: facts, capabilities=tuple, clock=lambda: 0)


@pytest.mark.parametrize(
    "document",
    [
        # Structurally decodable, and outside the public grammar of the field.
        {PROBE_FIELD: PROBE_HEALTH, "request_id": "req 42"},
        {PROBE_FIELD: PROBE_HEALTH, "request_id": "r" * 129},
        {PROBE_FIELD: PROBE_HEALTH, "request_id": "../../etc/passwd"},
        {PROBE_FIELD: PROBE_HEALTH, "deadline_ms": 86_400_001},
        {PROBE_FIELD: PROBE_HEALTH, "deadline_ms": 10**30},
        # Refused by the decoder before the probe boundary sees it.
        {PROBE_FIELD: 7},
        {PROBE_FIELD: PROBE_HEALTH, "deadline_ms": "10"},
        {PROBE_FIELD: PROBE_HEALTH, "deadline_ms": True},
        {PROBE_FIELD: PROBE_HEALTH, "request_id": []},
        # No branch to choose.
        {},
        {OPERATION_FIELD: OPERATION, PROBE_FIELD: PROBE_HEALTH},
        {1: "operation"},
        None,
        [],
        "probe",
        7,
    ],
)
def test_no_routing_failure_escapes_as_an_untyped_exception(document: object) -> None:
    with pytest.raises(ROUTING_ERRORS):
        _router().route(document)


def test_a_probe_request_the_public_grammar_refuses_is_a_probe_error() -> None:
    """The decoder proves the shape; the probe boundary proves the value.

    `ServiceProbeRequest.from_wire` accepts any string as a `request_id` -- that is
    what tolerant structural decoding means -- so an id that is not a `RequestId` is
    refused where it would otherwise be echoed back onto the wire.
    """
    with pytest.raises(ProbeError, match="request_id") as raised:
        _router().route({PROBE_FIELD: PROBE_HEALTH, "request_id": f"id={SECRET_PATH}"})
    assert SECRET_PATH not in str(raised.value)
    assert not isinstance(raised.value, ProtocolError)


def test_a_snapshot_that_cannot_be_published_refuses_the_routed_probe() -> None:
    """A component naming itself with a database path never reaches the wire.

    Routed through the document router rather than the probe router directly, so
    what is proved is the whole path an unauthenticated caller actually takes.
    """
    router = DocumentRouter(
        probes=_poisoned_probe_router(), dispatch=RecordingDispatch()
    )
    with pytest.raises(ProbeError) as raised:
        router.route({PROBE_FIELD: PROBE_HEALTH})
    assert SECRET_PATH not in str(raised.value)
    assert str(raised.value) == "service facts component 0 id is malformed"


def test_structural_refusals_stay_structural_and_name_no_value() -> None:
    """The router's own messages describe the document's shape, never its content."""
    document: dict[object, object] = {"probe": PROBE_HEALTH, "database": SECRET_PATH}
    document[1] = SECRET_PATH

    with pytest.raises(ProtocolError) as raised:
        _router().route(document)
    assert str(raised.value) == "protocol document keys must be strings"


# --- forbidden imports --------------------------------------------------------

PROTOCOL_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnivia_core_runtime"
    / "service"
    / "protocol.py"
)

#: This module routes an already-decoded document. Bytes, framing, sockets and
#: connection lifetime belong to the OVC1 and HTTP adapters above it, and storage,
#: lease and lifecycle code belongs below the dispatcher it calls.
FORBIDDEN_IMPORTS = (
    "json",
    "socket",
    "ssl",
    "struct",
    "http",
    "urllib",
    "asyncio",
    "sqlite3",
    "subprocess",
    "threading",
    "omnivia_core_runtime.storage",
    "omnivia_core_runtime.ownership",
    "omnivia_core_runtime.workspace",
    "omnivia_core_runtime.service.transport",
    "omnivia_core_runtime.service.lifecycle",
    "omnivia_core_runtime.service.runner",
    "omnivia_core_runtime.service.bootstrap",
    "omnivia_core_runtime.service.dispatch",
    "omnivia_core_runtime.service.main",
    "omnivia_core_runtime.service.jobs",
    "omnivia_core_runtime.service.authorization",
    "omnivia_core_runtime.service.operations",
    # The private conformance helpers: decoding is the public decoders' job.
    "omnivia_core.contracts.v1.conformance",
    "omnivia_core.contracts.v1.generated",
    "omnivia_core.contracts.v1.codec",
    "omnivia_core.contracts.v1.canonical_json",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_the_document_router_imports_no_transport_storage_or_private_helper() -> None:
    imported = _imported_modules(PROTOCOL_PATH)
    for name in imported:
        root = name.split(".")[0]
        assert not any(
            name == forbidden or name.startswith(f"{forbidden}.") or root == forbidden
            for forbidden in FORBIDDEN_IMPORTS
        ), f"protocol.py imports {name}"


def test_the_document_router_calls_the_public_from_wire_decoders() -> None:
    """Pinned in the syntax tree, not just in behaviour.

    A future edit could keep every behavioural test green while quietly parsing
    the envelope by hand; this is the check that would fail if it did.
    """
    source = PROTOCOL_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    decoded_by = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.attr == "from_wire"
    }
    assert decoded_by == {
        "RequestEnvelope.from_wire",
        "ServiceProbeRequest.from_wire",
    }


def test_the_document_router_performs_no_byte_or_network_io() -> None:
    source = PROTOCOL_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"open", "loads", "dumps", "encode", "decode", "recv"})
