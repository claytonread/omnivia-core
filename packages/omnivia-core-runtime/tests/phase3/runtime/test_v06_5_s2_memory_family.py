"""Focused S2 memory-family persistence and projection acceptance."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import omnivia_core_runtime.service.handlers.memory as memory_handlers_module
import omnivia_core_runtime.storage.memory as memory_storage_module
import pytest
import test_application_audit_idempotency_migration as m1
import test_governed_truth_and_relations_migration as m3
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    MEMORY_FAMILY_PURPOSES,
    ApplicationDispatcher,
    build_memory_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.memory import (
    MEMORY_FAMILY_OPERATIONS,
    HmacContinuationTokenCodec,
    MemoryHandlers,
)
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    HttpListener,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import LocalSocketServer, endpoint_for_path
from omnivia_core_runtime.storage.governed import read_governed_records
from omnivia_core_runtime.storage.memory import (
    create_memory_record,
    read_authorized_memory_snapshot,
)
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    EvidenceLabelGrant,
)
from test_v06_5_s2_memory_migration import (
    CLAIMS,
    MIGRATION_VERSION,
    TRANSITIONS,
    _apply_through,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ErrorResponseEnvelope,
    MemoryCreateInput,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    decode_response,
    encode_request,
    get_operation_metadata,
    to_canonical_json,
)

_TRANSITION_ASSERTED_AT = "2023-11-14T22:13:20Z"
_TRANSITION_ASSERTED_AT_US = 1_700_000_000_000_000
_TRANSITION_CONTENT_JSON = '{"fact":"durable memory"}'
_TRANSITION_CONTENT_DIGEST = "sha256:" + hashlib.sha256(
    _TRANSITION_CONTENT_JSON.encode()
).hexdigest()
_TRANSITION_FIRST_SETTLEMENT_US = 1_900_000_000_000_000


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m3.m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    _apply_through(path, MIGRATION_VERSION, workspace_id=m3.WORKSPACE_ID)
    holder = m3.m2.take_ownership(path)
    yield holder
    holder.connection.close()


def _allocator() -> Callable[[str], str]:
    values = iter(("rec-s2", "ver-s2", "asm-s2", "pev-s2", "seal-s2"))

    def allocate(_prefix: str) -> str:
        return next(values)

    return allocate


def _named_allocator(suffix: str) -> Callable[[str], str]:
    return lambda prefix: f"{prefix}-{suffix}"


def _memory_input(fact: str) -> dict[str, object]:
    return {
        "record_type": "memory.fact",
        "domain_scope": "personal.preferences",
        "content": {"fact": fact},
        "evidence_disposition": "unavailable",
        "sources": [],
        "assertion": {
            "actor_id": "actor-1",
            "actor_kind": "user",
            "actor_role": "owner",
            "asserted_at": "2024-01-01T00:00:00Z",
            "evidence": [],
        },
    }


def _available_memory_input(
    fact: str, source: dict[str, object]
) -> dict[str, object]:
    operation_input = _memory_input(fact)
    operation_input.update(
        evidence_disposition="available",
        sources=[source],
        assertion={
            "actor_id": "actor-1",
            "actor_kind": "user",
            "actor_role": "owner",
            "asserted_at": "2024-01-01T00:00:00Z",
            "evidence": [{"source": source}],
        },
    )
    return operation_input


def _seeded_source() -> dict[str, object]:
    return {
        "kind": "filesystem.archive",
        "source_id": "doc-1",
        "locator": "archive://doc.md",
        "retrieved_at": datetime.fromtimestamp(
            m3.m2.BASE_US / 1_000_000, tz=UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _request(
    operation: str,
    operation_input: dict[str, object],
    *,
    request_id: str,
    idempotency_key: str | None = None,
    workspace_id: str = m3.WORKSPACE_ID,
) -> RequestEnvelope:
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=MEMORY_FAMILY_PURPOSES[operation],
        workspace_id=workspace_id,
    )


def _dispatcher(
    owned: m3.m2.Owned,
    *,
    principal: str = CONFIGURED_LOCAL_OWNER,
    workspace_id: str = m3.WORKSPACE_ID,
    token_codec: HmacContinuationTokenCodec | None = None,
    allocator_tag: str = "production",
    allocate_identifier: Callable[[str], str] | None = None,
) -> ApplicationDispatcher:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{allocator_tag}-{counts[prefix]}"

    fallback = Dispatcher.for_service_operations(
        Grant(
            principal=principal,
            workspaces=frozenset({workspace_id}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )
    return build_memory_application_dispatcher(
        service=owned,
        principal_id=principal,
        installation_id=s0.INSTALLATION_ID,
        workspace_id=workspace_id,
        fallback=fallback,
        clock=FakeClock(wall=datetime.fromtimestamp(1_900_000_000, tz=UTC)),
        allocate_identifier=(
            allocate if allocate_identifier is None else allocate_identifier
        ),
        token_codec=(
            HmacContinuationTokenCodec(b"s2-production-token-secret")
            if token_codec is None
            else token_codec
        ),
    )


def _primary_create(
    owned: m3.m2.Owned,
    *,
    suffix: str,
) -> tuple[ApplicationDispatcher, RequestEnvelope, SuccessResponseEnvelope]:
    dispatcher = _dispatcher(owned, allocator_tag=f"parent-{suffix}")
    request = _request(
        "memory.create",
        _memory_input(f"parent {suffix}"),
        request_id=f"req-parent-{suffix}-primary",
        idempotency_key=f"idem-parent-{suffix}",
    )
    response = dispatcher.dispatch(request)
    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.metadata.audit_reference is not None
    assert response.result["record"]["content"] == {"fact": f"parent {suffix}"}
    return dispatcher, request, response


def _router(dispatcher: ApplicationDispatcher) -> DocumentRouter:
    return DocumentRouter(
        probes=ProbeRouter(
            facts=lambda: ServiceFacts(
                observed_at="2026-08-12T00:00:00Z",
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            ),
            capabilities=tuple,
            clock=lambda: 0,
        ),
        dispatch=dispatcher.dispatch,
    )


def _transport_call(
    adapter: str,
    dispatcher: ApplicationDispatcher,
    request: RequestEnvelope,
) -> ResponseEnvelope:
    if adapter == "in-process":
        return dispatcher.dispatch(request)
    router = _router(dispatcher)
    if adapter == "local-ipc":
        with tempfile.TemporaryDirectory(prefix="ov-s2-", dir="/tmp") as directory:
            endpoint = endpoint_for_path(Path(directory) / "service.sock")
            server = LocalSocketServer(router=router, endpoint=endpoint)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(10)
                    client.connect(endpoint.address)
                    client.sendall(encode_frame(encode_request(request)))
                    header = b""
                    while len(header) < 8:
                        header += client.recv(8 - len(header))
                    length = int.from_bytes(header[4:], "big")
                    body = b""
                    while len(body) < length:
                        body += client.recv(length - len(body))
                return decode_response(decode_frame(header + body))
            finally:
                server.stop()

    assert adapter == "http"
    credential = "s2-production-credential"
    server = HttpListener(
        router=router,
        principal=dispatcher.session.principal_id,
        resolver=lambda value: dispatcher.session if value == credential else None,
    )
    server.start()
    try:
        port = int(server.url.rsplit(":", 1)[1])
        body = json.dumps(
            encode_request(request), separators=(",", ":"), sort_keys=True
        ).encode()
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            client.request(
                "POST",
                APPLICATION_PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": CONTENT_TYPE,
                },
            )
            response = client.getresponse()
            response_body = response.read()
        finally:
            client.close()
        assert response.status == 200
        return decode_response(json.loads(response_body))
    finally:
        server.stop()


def _seed_candidate(
    owned: m3.m2.Owned,
    *,
    suffix: str,
    fact: str,
    settled_at_us: int = 1_900_000_000_000_000,
) -> None:
    settlement = MutationSettlementContext(
        audit_ref=f"audit-{suffix}",
        claim_id=f"claim-{suffix}",
        outcome_id=f"outcome-{suffix}",
        settled_at_us=settled_at_us,
    )
    audit = m3.audit_row(settlement.audit_ref)
    audit.update(operation="memory.create", recorded_at_us=settled_at_us)
    m1.insert(owned.connection, "omnivia_application_audit_events", audit)
    create_memory_record(
        owned.connection,
        settlement,
        workspace_id=m3.WORKSPACE_ID,
        claim=MemoryCreateInput.from_wire(_memory_input(fact)),
        label_grant=EvidenceLabelGrant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=m3.WORKSPACE_ID,
            all_labels=True,
            labels=frozenset(),
        ),
        allocate_identifier=_named_allocator(suffix),
    )


def _transition_claim_json(*, source_id: str | None = None) -> str:
    source = (
        None
        if source_id is None
        else {
            "kind": "filesystem.archive",
            "source_id": source_id,
            "locator": "archive://doc.md",
            "retrieved_at": _TRANSITION_ASSERTED_AT,
        }
    )
    claim = MemoryCreateInput.from_wire(
        {
            "record_type": "memory.fact",
            "domain_scope": "personal.preferences",
            "content": {"fact": "durable memory"},
            "evidence_disposition": (
                "unavailable" if source is None else "available"
            ),
            "sources": [] if source is None else [source],
            "assertion": {
                "actor_id": "principal-1",
                "actor_kind": "human",
                "actor_role": "author",
                "asserted_at": _TRANSITION_ASSERTED_AT,
                "evidence": [] if source is None else [{"source": source}],
            },
        }
    )
    return to_canonical_json(claim.to_wire())


def _insert_transition_application_version(
    holder: m3.m2.Owned,
    *,
    operation: str,
    audit_ref: str,
    principal_id: str,
    assembly_id: str,
    version_id: str,
    settled_at_us: int,
    claim_json: str,
    claim_ingested_at_us: int,
    ordinal: int,
    create_record: bool = False,
    accepted: bool = False,
    evidence_link: bool = False,
) -> None:
    audit = m3.audit_row(audit_ref, principal=principal_id)
    audit.update(operation=operation, recorded_at_us=settled_at_us)
    assembly = m3.assembly_row(
        assembly_id,
        version_id,
        "record-memory",
        record_type="memory.fact",
        scope="personal.preferences",
        layer="governed" if accepted else "candidate",
        origin=None if accepted else "human_proposed",
        disposition="accepted" if accepted else None,
        authority="canonical" if accepted else "proposed",
        decision_kind="human_reviewer" if accepted else None,
        decision_id=principal_id if accepted else None,
        audit_ref=audit_ref,
        correlation_id=audit_ref,
        ordinal=ordinal,
        digest=_TRANSITION_CONTENT_DIGEST,
        evidence="available" if evidence_link else "unavailable",
    )
    assembly.update(
        content_json=_TRANSITION_CONTENT_JSON,
        reason_code=("governance.reviewed" if accepted else "evidence.unavailable"),
        valid_from_us=_TRANSITION_ASSERTED_AT_US,
        recorded_at_us=settled_at_us,
    )
    event_id = f"event-{assembly_id}"
    event = m3.event_row(
        event_id,
        assembly_id,
        version_id,
        "governance.accepted" if accepted else "candidate.human_proposed",
        audit_ref=audit_ref,
        correlation_id=audit_ref,
        actor_id=principal_id if accepted else "principal-1",
        actor_kind="human",
        actor_role="reviewer" if accepted else "author",
        predecessor_record_id="record-memory" if accepted else None,
        predecessor_version_id="version-candidate" if accepted else None,
        evidence="available" if evidence_link else "unavailable",
    )
    event.update(
        occurred_at_us=(
            settled_at_us if accepted else _TRANSITION_ASSERTED_AT_US
        ),
        recorded_at_us=settled_at_us,
        reason_code=("governance.reviewed" if accepted else "evidence.unavailable"),
    )
    seal = m3.seal_row(
        assembly_id,
        version_id,
        seal_id=f"seal-{assembly_id}",
        correlation_id=audit_ref,
    )
    seal["sealed_at_us"] = settled_at_us
    claim_digest = "sha256:" + hashlib.sha256(claim_json.encode()).hexdigest()

    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m3.insert(holder.connection, "omnivia_application_audit_events", audit)
        if create_record:
            record = m3.record_row(
                "record-memory",
                record_type="memory.fact",
                scope="personal.preferences",
            )
            record["recorded_at_us"] = settled_at_us
            m3.insert(holder.connection, m3.RECORDS, record)
        m3.insert(holder.connection, m3.ASSEMBLIES, assembly)
        m3.insert(holder.connection, m3.EVENTS, event)
        if evidence_link:
            m3.insert(holder.connection, m3.LINKS, m3.link_row(event_id, assembly_id))
        m3.insert(holder.connection, m3.SEALS, seal)
        m3.insert(
            holder.connection,
            CLAIMS,
            {
                "workspace_id": m3.WORKSPACE_ID,
                "assembly_id": assembly_id,
                "governed_record_version_id": version_id,
                "operation": operation,
                "audit_ref": audit_ref,
                "claim_json": claim_json,
                "claim_digest": claim_digest,
                "claim_byte_length": len(claim_json.encode()),
                "claim_ingested_at_us": claim_ingested_at_us,
                "settled_at_us": settled_at_us,
            },
        )


def _insert_application_transition(
    holder: m3.m2.Owned,
    *,
    transition_id: str,
    source_assembly_id: str,
    source_version_id: str,
    target_assembly_id: str,
    target_version_id: str,
    operation: str,
    reason_code: str,
    actor_id: str,
    audit_ref: str,
    settled_at_us: int,
) -> None:
    rationale = to_canonical_json({"reason_code": reason_code})
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m3.insert(
            holder.connection,
            TRANSITIONS,
            {
                "workspace_id": m3.WORKSPACE_ID,
                "transition_id": transition_id,
                "governed_record_id": "record-memory",
                "source_assembly_id": source_assembly_id,
                "source_record_version_id": source_version_id,
                "target_assembly_id": target_assembly_id,
                "target_record_version_id": target_version_id,
                "operation": operation,
                "rationale_json": rationale,
                "rationale_digest": "sha256:"
                + hashlib.sha256(rationale.encode()).hexdigest(),
                "rationale_byte_length": len(rationale.encode()),
                "reason_code": reason_code,
                "reason_comment": None,
                "actor_id": actor_id,
                "actor_kind": "human",
                "audit_ref": audit_ref,
                "settled_at_us": settled_at_us,
            },
        )


def test_v06_5_s2_transition_chain_multi_hop_projection_is_cumulative(
    owned: m3.m2.Owned,
) -> None:
    claim_json = _transition_claim_json()
    proposed_at = _TRANSITION_FIRST_SETTLEMENT_US
    candidate_at = proposed_at + 100
    accepted_at = candidate_at + 100
    _insert_transition_application_version(
        owned,
        operation="memory.create",
        audit_ref="audit-memory-create",
        principal_id="principal-1",
        assembly_id="assembly-proposed",
        version_id="version-proposed",
        settled_at_us=proposed_at,
        claim_json=claim_json,
        claim_ingested_at_us=proposed_at,
        ordinal=1,
        create_record=True,
    )
    _insert_transition_application_version(
        owned,
        operation="knowledge.propose",
        audit_ref="audit-knowledge-propose",
        principal_id="principal-1",
        assembly_id="assembly-candidate",
        version_id="version-candidate",
        settled_at_us=candidate_at,
        claim_json=claim_json,
        claim_ingested_at_us=proposed_at,
        ordinal=2,
    )
    _insert_application_transition(
        owned,
        transition_id="transition-propose",
        source_assembly_id="assembly-proposed",
        source_version_id="version-proposed",
        target_assembly_id="assembly-candidate",
        target_version_id="version-candidate",
        operation="knowledge.propose",
        reason_code="knowledge.proposed",
        actor_id="principal-1",
        audit_ref="audit-knowledge-propose",
        settled_at_us=candidate_at,
    )
    _insert_transition_application_version(
        owned,
        operation="candidate.approve",
        audit_ref="audit-candidate-approve",
        principal_id="reviewer-1",
        assembly_id="assembly-accepted",
        version_id="version-accepted",
        settled_at_us=accepted_at,
        claim_json=claim_json,
        claim_ingested_at_us=proposed_at,
        ordinal=3,
        accepted=True,
    )
    _insert_application_transition(
        owned,
        transition_id="transition-approve",
        source_assembly_id="assembly-candidate",
        source_version_id="version-candidate",
        target_assembly_id="assembly-accepted",
        target_version_id="version-accepted",
        operation="candidate.approve",
        reason_code="candidate.approved",
        actor_id="reviewer-1",
        audit_ref="audit-candidate-approve",
        settled_at_us=accepted_at,
    )

    candidates = read_governed_records(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=accepted_at + 1,
        view="candidates",
    )
    current = read_governed_records(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=accepted_at + 1,
    )
    by_version = {
        record.provenance.identity.version: record
        for record in (*candidates, *current)
        if record.provenance.identity.record_id == "record-memory"
    }

    assert set(by_version) == {
        "version-proposed",
        "version-candidate",
        "version-accepted",
    }
    proposed = by_version["version-proposed"]
    candidate = by_version["version-candidate"]
    accepted = by_version["version-accepted"]
    assert proposed.provenance.history == ()
    assert proposed.provenance.identity.governance_state == "proposed"
    assert proposed.provenance.identity.currentness == "superseded"
    assert proposed.provenance.identity.superseded_by is not None
    assert proposed.provenance.identity.superseded_by.version == "version-candidate"
    assert [entry.action for entry in candidate.provenance.history] == [
        "knowledge.propose"
    ]
    assert candidate.provenance.identity.governance_state == "candidate"
    assert candidate.provenance.identity.currentness == "superseded"
    assert candidate.provenance.identity.supersedes is not None
    assert candidate.provenance.identity.supersedes.version == "version-proposed"
    assert candidate.provenance.identity.superseded_by is not None
    assert candidate.provenance.identity.superseded_by.version == "version-accepted"
    assert [entry.action for entry in accepted.provenance.history] == [
        "knowledge.propose",
        "candidate.approve",
    ]
    assert accepted.provenance.identity.governance_state == "accepted"
    assert accepted.provenance.identity.currentness == "current"
    assert accepted.provenance.identity.supersedes is not None
    assert accepted.provenance.identity.supersedes.version == "version-candidate"
    assert accepted.reviewer == "reviewer-1"


def test_v06_5_s2_claim_source_mismatch_fails_closed(
    owned: m3.m2.Owned,
) -> None:
    m3.m2.seed_chain(owned)
    _insert_transition_application_version(
        owned,
        operation="memory.create",
        audit_ref="audit-source-mismatch",
        principal_id="principal-1",
        assembly_id="assembly-source-mismatch",
        version_id="version-source-mismatch",
        settled_at_us=_TRANSITION_FIRST_SETTLEMENT_US,
        claim_json=_transition_claim_json(source_id="a-different-source"),
        claim_ingested_at_us=_TRANSITION_FIRST_SETTLEMENT_US,
        ordinal=1,
        create_record=True,
        evidence_link=True,
    )

    with pytest.raises(
        ValueError,
        match="application claim sources contradict governed evidence links",
    ):
        read_governed_records(
            owned.connection,
            workspace_id=m3.WORKSPACE_ID,
            resolution_instant_us=_TRANSITION_FIRST_SETTLEMENT_US + 1,
            view="candidates",
        )


def test_v06_5_s2_creation_event_is_not_public_transition_history(
    owned: m3.m2.Owned,
) -> None:
    settled_at_us = 1_900_000_000_000_000
    settlement = MutationSettlementContext(
        audit_ref="audit-s2",
        claim_id="claim-s2",
        outcome_id="outcome-s2",
        settled_at_us=settled_at_us,
    )
    claim = MemoryCreateInput.from_wire(
        {
            "record_type": "memory.fact",
            "domain_scope": "personal.preferences",
            "content": {"fact": "hello"},
            "evidence_disposition": "unavailable",
            "sources": [],
            "assertion": {
                "actor_id": "actor-1",
                "actor_kind": "user",
                "actor_role": "owner",
                "asserted_at": "2024-01-01T00:00:00Z",
                "evidence": [],
            },
        }
    )
    audit = m3.audit_row(settlement.audit_ref)
    audit.update(operation="memory.create", recorded_at_us=settled_at_us)
    grant = EvidenceLabelGrant(
        principal_id="local-owner",
        workspace_id=m3.WORKSPACE_ID,
        all_labels=True,
        labels=frozenset(),
    )

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        m1.insert(owned.connection, "omnivia_application_audit_events", audit)
        result = create_memory_record(
            owned.connection,
            settlement,
            workspace_id=m3.WORKSPACE_ID,
            claim=claim,
            label_grant=grant,
            allocate_identifier=_allocator(),
        )

    assert result["record"]["provenance"]["identity"] == {
        "record_id": "rec-s2",
        "version": "ver-s2",
        "layer": "l1",
        "governance_state": "proposed",
        "currentness": "current",
    }
    assert result["record"]["provenance"]["temporal"]["ingested_at"] == (
        "2030-03-17T17:46:40Z"
    )
    assert result["record"]["provenance"]["temporal"]["recorded_at"] == (
        "2030-03-17T17:46:40Z"
    )
    assert (
        owned.connection.execute(
            "SELECT audit_ref, claim_json FROM omnivia_application_claim_lineage"
        ).fetchone()[0]
        == settlement.audit_ref
    )

    snapshot = read_authorized_memory_snapshot(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=settled_at_us + 1,
        view="candidates",
        label_grant=grant,
    )
    assert len(snapshot.values) == 1
    projected = snapshot.values[0].record
    assert projected.provenance.identity.governance_state == "candidate"
    assert projected.provenance.history == ()
    assert projected.provenance.assertion == claim.assertion
    assert projected.content == claim.content


def test_v06_5_s2_claim_lineage_is_immutable_and_reconstructs(
    owned: m3.m2.Owned,
) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _seed_candidate(owned, suffix="lineage", fact="lineage fact")

    expected_claim = to_canonical_json(
        MemoryCreateInput.from_wire(_memory_input("lineage fact")).to_wire()
    )
    stored = owned.connection.execute(
        "SELECT claim_json, claim_digest, claim_byte_length "
        "FROM omnivia_application_claim_lineage"
    ).fetchone()
    assert stored == (
        expected_claim,
        "sha256:" + hashlib.sha256(expected_claim.encode()).hexdigest(),
        len(expected_claim.encode()),
    )

    for statement in (
        "UPDATE omnivia_application_claim_lineage SET operation='knowledge.propose'",
        "DELETE FROM omnivia_application_claim_lineage",
    ):
        with (
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
            fenced_transaction(
                owned.connection,
                owned.identity,
                workspace_id=m3.WORKSPACE_ID,
                fencing_generation=owned.generation,
            ),
        ):
            owned.connection.execute(statement)

    snapshot = read_authorized_memory_snapshot(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=1_900_000_000_000_001,
        view="candidates",
        label_grant=EvidenceLabelGrant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=m3.WORKSPACE_ID,
            all_labels=True,
            labels=frozenset(),
        ),
    )
    assert len(snapshot.values) == 1
    reconstructed = snapshot.values[0].record
    assert reconstructed.content == {"fact": "lineage fact"}
    assert reconstructed.provenance.assertion is not None
    assert reconstructed.provenance.assertion.to_wire() == _memory_input(
        "lineage fact"
    )["assertion"]


def test_s2_default_view_does_not_publish_proposed_candidate(
    owned: m3.m2.Owned,
) -> None:
    snapshot = read_authorized_memory_snapshot(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=1_900_000_000_000_001,
        view=None,
        label_grant=EvidenceLabelGrant(
            principal_id="local-owner",
            workspace_id=m3.WORKSPACE_ID,
            all_labels=True,
            labels=frozenset(),
        ),
    )
    assert snapshot.view == "current_canonical"
    assert snapshot.values == ()


def test_v06_5_s2_acl_applies_before_read_materialization(
    owned: m3.m2.Owned,
) -> None:
    m3.m2.seed_chain(owned)
    retrieved = datetime.fromtimestamp(m3.m2.BASE_US / 1_000_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    source = {
        "kind": "filesystem.archive",
        "source_id": "doc-1",
        "locator": "archive://doc.md",
        "retrieved_at": retrieved,
    }
    claim = MemoryCreateInput.from_wire(
        {
            "record_type": "memory.fact",
            "domain_scope": "personal.preferences",
            "content": {"fact": "evidenced"},
            "evidence_disposition": "available",
            "sources": [source],
            "assertion": {
                "actor_id": "actor-1",
                "actor_kind": "user",
                "actor_role": "owner",
                "asserted_at": "2024-01-01T00:00:00Z",
                "evidence": [{"source": source}],
            },
        }
    )
    settled_at_us = 1_900_000_000_000_000
    settlement = MutationSettlementContext(
        audit_ref="audit-evidence",
        claim_id="claim-evidence",
        outcome_id="outcome-evidence",
        settled_at_us=settled_at_us,
    )
    audit = m3.audit_row(settlement.audit_ref)
    audit.update(operation="memory.create", recorded_at_us=settled_at_us)
    all_labels = EvidenceLabelGrant(
        principal_id="local-owner",
        workspace_id=m3.WORKSPACE_ID,
        all_labels=True,
        labels=frozenset(),
    )
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        m1.insert(owned.connection, "omnivia_application_audit_events", audit)
        create_memory_record(
            owned.connection,
            settlement,
            workspace_id=m3.WORKSPACE_ID,
            claim=claim,
            label_grant=all_labels,
            allocate_identifier=_allocator(),
        )

    denied = read_authorized_memory_snapshot(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=settled_at_us + 1,
        view="candidates",
        label_grant=EvidenceLabelGrant(
            principal_id="another-principal",
            workspace_id=m3.WORKSPACE_ID,
            all_labels=False,
            labels=frozenset(),
        ),
    )
    assert denied.values == ()
    allowed = read_authorized_memory_snapshot(
        owned.connection,
        workspace_id=m3.WORKSPACE_ID,
        resolution_instant_us=settled_at_us + 1,
        view="candidates",
        label_grant=all_labels,
    )
    assert [value.record.content["fact"] for value in allowed.values] == ["evidenced"]


def test_v06_5_s2_replay_preserves_record_audit_and_lineage_identity(
    owned: m3.m2.Owned,
) -> None:
    operation_input = {
        "record_type": "memory.fact",
        "domain_scope": "personal.preferences",
        "content": {"fact": "through-handler"},
        "evidence_disposition": "unavailable",
        "sources": [],
        "assertion": {
            "actor_id": "actor-1",
            "actor_kind": "user",
            "actor_role": "owner",
            "asserted_at": "2024-01-01T00:00:00Z",
            "evidence": [],
        },
    }
    entry = get_operation_metadata("memory.create")
    session = s0.session_for(entry, workspaces=frozenset({m3.WORKSPACE_ID}))
    binding = ServiceBinding(
        installation_id=s0.INSTALLATION_ID, workspace_id=m3.WORKSPACE_ID
    )
    request = s0.envelope_for(
        entry, operation_input=operation_input, workspace_id=m3.WORKSPACE_ID
    )
    authorized = s0.authorize(
        entry,
        session=session,
        binding=binding,
        operation_input=operation_input,
        workspace_id=m3.WORKSPACE_ID,
    )
    counter: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counter[prefix] = counter.get(prefix, 0) + 1
        return f"{prefix}-s2-{counter[prefix]}"

    handlers = MemoryHandlers(
        service=owned,
        session=session,
        binding=binding,
        label_grant=EvidenceLabelGrant(
            principal_id=s0.PRINCIPAL,
            workspace_id=m3.WORKSPACE_ID,
            all_labels=True,
            labels=frozenset(),
        ),
        authorized_views=frozenset({"candidates", "history"}),
        clock=FakeClock(
            wall=datetime.fromtimestamp(1_900_000_000, tz=UTC),
        ),
        allocate_identifier=allocate,
        token_codec=HmacContinuationTokenCodec(b"s2-test-secret"),
    )
    context = OperationContext(
        request=request,
        principal=authorized.principal_id,
        workspace_id=m3.WORKSPACE_ID,
        granted_operations=session.operations,
        service=owned,
        authority=authorized.authority,
        scopes=authorized.scopes,
        purpose=authorized.purpose,
        authorization=authorized,
    )
    first = handlers.memory_create(context)
    assert isinstance(first, AuditedOperationResult)
    assert first.audit_reference == "aud-s2-1"
    first_lineage = owned.connection.execute(
        """
        SELECT assembly_id,
               governed_record_version_id,
               operation,
               audit_ref,
               claim_digest,
               claim_ingested_at_us,
               settled_at_us
        FROM omnivia_application_claim_lineage
        """
    ).fetchall()
    first_audit = owned.connection.execute(
        """
        SELECT audit_ref, operation, request_id, claim_id, outcome_id
        FROM omnivia_application_audit_events
        """
    ).fetchall()
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (1,)
    replay = handlers.memory_create(context)
    assert replay.result == first.result
    assert replay.audit_reference == first.audit_reference
    assert owned.connection.execute(
        """
        SELECT assembly_id,
               governed_record_version_id,
               operation,
               audit_ref,
               claim_digest,
               claim_ingested_at_us,
               settled_at_us
        FROM omnivia_application_claim_lineage
        """
    ).fetchall() == first_lineage
    assert owned.connection.execute(
        """
        SELECT audit_ref, operation, request_id, claim_id, outcome_id
        FROM omnivia_application_audit_events
        """
    ).fetchall() == first_audit
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (1,)


def test_v06_5_s2_linked_pages_are_distinct_and_duplicate_free(
    owned: m3.m2.Owned,
) -> None:
    settled_at_us = 1_900_000_000_000_000
    label_grant = EvidenceLabelGrant(
        principal_id=s0.PRINCIPAL,
        workspace_id=m3.WORKSPACE_ID,
        all_labels=True,
        labels=frozenset(),
    )
    for suffix in ("a", "b"):
        settlement = MutationSettlementContext(
            audit_ref=f"audit-{suffix}",
            claim_id=f"claim-{suffix}",
            outcome_id=f"outcome-{suffix}",
            settled_at_us=settled_at_us,
        )
        audit = m3.audit_row(settlement.audit_ref)
        audit.update(operation="memory.create", recorded_at_us=settled_at_us)
        claim = MemoryCreateInput.from_wire(
            {
                "record_type": "memory.fact",
                "domain_scope": "personal.preferences",
                "content": {"fact": suffix},
                "evidence_disposition": "unavailable",
                "sources": [],
                "assertion": {
                    "actor_id": "actor-1",
                    "actor_kind": "user",
                    "actor_role": "owner",
                    "asserted_at": "2024-01-01T00:00:00Z",
                    "evidence": [],
                },
            }
        )
        with fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=m3.WORKSPACE_ID,
            fencing_generation=owned.generation,
        ):
            m1.insert(owned.connection, "omnivia_application_audit_events", audit)
            create_memory_record(
                owned.connection,
                settlement,
                workspace_id=m3.WORKSPACE_ID,
                claim=claim,
                label_grant=label_grant,
                allocate_identifier=_named_allocator(suffix),
            )

    entry = get_operation_metadata("memory.create")
    session = s0.session_for(entry, workspaces=frozenset({m3.WORKSPACE_ID}))
    handlers = MemoryHandlers(
        service=owned,
        session=session,
        binding=ServiceBinding(
            installation_id=s0.INSTALLATION_ID, workspace_id=m3.WORKSPACE_ID
        ),
        label_grant=label_grant,
        authorized_views=frozenset({"candidates", "history"}),
        clock=FakeClock(wall=datetime.fromtimestamp(1_900_000_000, tz=UTC)),
        allocate_identifier=lambda prefix: f"{prefix}-unused",
        token_codec=HmacContinuationTokenCodec(b"s2-list-secret"),
    )

    def context(operation: str, operation_input: dict[str, object]) -> OperationContext:
        request = s0.envelope_for(
            get_operation_metadata(operation),
            operation_input=operation_input,
            workspace_id=m3.WORKSPACE_ID,
            purpose="knowledge_retrieval",
        )
        return OperationContext(
            request=request,
            principal=s0.PRINCIPAL,
            workspace_id=m3.WORKSPACE_ID,
            granted_operations=frozenset({operation}),
            service=owned,
        )

    got = handlers.memory_get(
        context("memory.get", {"record_id": "rec-a", "view": "candidates"})
    )
    assert got["record"]["content"] == {"fact": "a"}
    assert got["record"]["provenance"]["identity"]["governance_state"] == "candidate"

    first = handlers.memory_list(
        context("memory.list", {"view": "candidates", "limit": 1})
    )
    assert [record["content"]["fact"] for record in first["records"]] == ["a"]
    token = first["page"]["continuation_token"]
    second = handlers.memory_list(
        context(
            "memory.list",
            {
                "view": "candidates",
                "limit": 1,
                "page": {"continuation_token": token},
            },
        )
    )
    assert [record["content"]["fact"] for record in second["records"]] == ["b"]
    assert second["page"] == {}


def test_v06_5_s2_memory_create_primary_success(owned: m3.m2.Owned) -> None:
    _, _, response = _primary_create(owned, suffix="primary")
    assert response.result["record"]["provenance"]["identity"]["layer"] == "l1"
    assert response.result["record"]["provenance"]["identity"][
        "governance_state"
    ] == "proposed"
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_claim_lineage"
    ).fetchone() == (1,)


def test_v06_5_s2_memory_create_honest_replay(owned: m3.m2.Owned) -> None:
    dispatcher, request, first = _primary_create(owned, suffix="replay")
    replay = dispatcher.dispatch(
        replace(
            request,
            metadata=replace(request.metadata, request_id="req-parent-replay-second"),
        )
    )
    assert isinstance(replay, SuccessResponseEnvelope)
    assert replay.result == first.result
    assert replay.metadata.audit_reference == first.metadata.audit_reference
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_claim_lineage"
    ).fetchone() == (1,)


def test_v06_5_s2_memory_create_idempotency_conflict(
    owned: m3.m2.Owned,
) -> None:
    dispatcher, request, first = _primary_create(owned, suffix="conflict")
    conflict = dispatcher.dispatch(
        replace(
            request,
            metadata=replace(request.metadata, request_id="req-parent-conflict-second"),
            input=_memory_input("a different canonical claim"),
        )
    )
    assert isinstance(conflict, ErrorResponseEnvelope)
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert conflict.metadata.audit_reference == first.metadata.audit_reference
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (1,)


def test_v06_5_s2_memory_get_primary_success(owned: m3.m2.Owned) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _seed_candidate(owned, suffix="parent-get", fact="get primary")
    dispatcher = _dispatcher(owned, allocator_tag="parent-get")
    response = dispatcher.dispatch(
        _request(
            "memory.get",
            {"record_id": "rec-parent-get", "view": "candidates"},
            request_id="req-parent-get-primary",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.result["record"]["content"] == {"fact": "get primary"}
    assert response.metadata.audit_reference is None


def test_v06_5_s2_memory_list_primary_and_page_2(owned: m3.m2.Owned) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _seed_candidate(owned, suffix="parent-list-a", fact="page one")
        _seed_candidate(owned, suffix="parent-list-b", fact="page two")
    dispatcher = _dispatcher(owned, allocator_tag="parent-list")
    first = dispatcher.dispatch(
        _request(
            "memory.list",
            {"view": "candidates", "limit": 1},
            request_id="req-parent-list-primary",
        )
    )
    assert isinstance(first, SuccessResponseEnvelope)
    token = first.result["page"]["continuation_token"]
    second = dispatcher.dispatch(
        _request(
            "memory.list",
            {
                "view": "candidates",
                "limit": 1,
                "page": {"continuation_token": token},
            },
            request_id="req-parent-list-page-2",
        )
    )
    assert isinstance(second, SuccessResponseEnvelope)
    first_id = first.result["records"][0]["provenance"]["identity"]["record_id"]
    second_id = second.result["records"][0]["provenance"]["identity"]["record_id"]
    assert (first_id, second_id) == ("rec-parent-list-a", "rec-parent-list-b")
    assert second.result["page"] == {}


def test_v06_5_s2_create_atomic_audit_and_rollback(
    owned: m3.m2.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = memory_handlers_module.create_memory_record

    def fail_after_domain_write(*args: Any, **kwargs: Any) -> dict[str, object]:
        original(*args, **kwargs)
        raise RuntimeError("injected failure after governed writes")

    monkeypatch.setattr(
        memory_handlers_module,
        "create_memory_record",
        fail_after_domain_write,
    )
    dispatcher = _dispatcher(owned, allocator_tag="atomic-rollback")
    with pytest.raises(RuntimeError, match="injected failure"):
        dispatcher.dispatch(
            _request(
                "memory.create",
                _memory_input("must roll back"),
                request_id="req-parent-create-rollback",
                idempotency_key="idem-parent-create-rollback",
            )
        )

    for table in (
        "omnivia_application_audit_events",
        "omnivia_idempotency_claims",
        "omnivia_idempotency_outcomes",
        "omnivia_mutation_executions",
        "omnivia_governed_records",
        "omnivia_governed_version_assemblies",
        "omnivia_governed_provenance_events",
        "omnivia_governed_version_seals",
        "omnivia_application_claim_lineage",
    ):
        assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
            0,
        )


def test_v06_5_s2_settlement_context_precedes_governed_fk_without_partial_state(
    owned: m3.m2.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = memory_handlers_module.create_memory_record
    observations: list[tuple[tuple[str] | None, bool]] = []

    def observe_then_fail(
        connection: Any,
        settlement: MutationSettlementContext,
        **kwargs: Any,
    ) -> dict[str, object]:
        audit_row = connection.execute(
            "SELECT audit_ref FROM omnivia_application_audit_events "
            "WHERE audit_ref = ?",
            (settlement.audit_ref,),
        ).fetchone()
        observations.append((audit_row, connection.in_transaction))
        original(connection, settlement, **kwargs)
        raise RuntimeError("injected failure after settlement-dependent writes")

    monkeypatch.setattr(
        memory_handlers_module,
        "create_memory_record",
        observe_then_fail,
    )
    dispatcher = _dispatcher(owned, allocator_tag="settlement-order")
    with pytest.raises(RuntimeError, match="settlement-dependent writes"):
        dispatcher.dispatch(
            _request(
                "memory.create",
                _memory_input("settlement order"),
                request_id="req-settlement-order",
                idempotency_key="idem-settlement-order",
            )
        )

    assert len(observations) == 1
    audit_row, in_transaction = observations[0]
    assert audit_row is not None
    assert audit_row[0].startswith("aud-")
    assert in_transaction is True
    for table in (
        "omnivia_application_audit_events",
        "omnivia_idempotency_claims",
        "omnivia_idempotency_outcomes",
        "omnivia_mutation_executions",
        "omnivia_governed_records",
        "omnivia_governed_version_assemblies",
        "omnivia_application_claim_lineage",
    ):
        assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
            0,
        )


def test_v06_5_s2_production_session_and_grants_are_server_owned(
    owned: m3.m2.Owned,
) -> None:
    dispatcher = _dispatcher(owned)
    assert dispatcher.session.principal_id == CONFIGURED_LOCAL_OWNER
    assert dispatcher.session.installations == frozenset({s0.INSTALLATION_ID})
    assert dispatcher.session.workspaces == frozenset({m3.WORKSPACE_ID})
    assert dispatcher.session.operations == MEMORY_FAMILY_OPERATIONS
    assert dispatcher.session.roles == frozenset({"workspace_contributor"})
    assert dispatcher.registry.operations == MEMORY_FAMILY_OPERATIONS

    handler = dispatcher.registry.get("memory.list")
    assert handler is not None
    handlers = getattr(handler, "__self__", None)
    assert isinstance(handlers, MemoryHandlers)
    assert handlers.label_grant == EvidenceLabelGrant(
        principal_id=CONFIGURED_LOCAL_OWNER,
        workspace_id=m3.WORKSPACE_ID,
        all_labels=True,
        labels=frozenset(),
    )
    assert handlers.authorized_views == frozenset({"candidates", "history"})

    # These fields are deliberately not part of MemoryListInput. Additive input is
    # ignored for compatibility, but it can neither replace nor narrow server policy.
    response = dispatcher.dispatch(
        _request(
            "memory.list",
            {
                "view": "candidates",
                "authorized_views": [],
                "principal": "request-chosen-principal",
            },
            request_id="req-server-owned-policy",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope)
    assert response.metadata.authority.principal_id == CONFIGURED_LOCAL_OWNER
    assert response.metadata.authority.roles == ()
    assert dispatcher.session.operations == MEMORY_FAMILY_OPERATIONS
    assert handlers.authorized_views == frozenset({"candidates", "history"})

    foreign = _dispatcher(
        owned,
        principal="request-cannot-become-local-owner",
        allocator_tag="server-owned-foreign",
    )
    foreign_handler = foreign.registry.get("memory.list")
    assert foreign_handler is not None
    foreign_handlers = getattr(foreign_handler, "__self__", None)
    assert isinstance(foreign_handlers, MemoryHandlers)
    assert foreign_handlers.authorized_views == frozenset()
    refused = foreign.dispatch(
        _request(
            "memory.list",
            {
                "view": "candidates",
                "authorized_views": ["candidates"],
                "principal": CONFIGURED_LOCAL_OWNER,
            },
            request_id="req-cannot-widen-server-owned-policy",
        )
    )
    assert isinstance(refused, ErrorResponseEnvelope)
    assert refused.error.code == "authorization_denied"


def test_v06_5_s2_deterministic_dependencies_use_production_composition(
    owned: m3.m2.Owned,
) -> None:
    codec = HmacContinuationTokenCodec(b"deterministic-s2-token-secret")
    first = _dispatcher(
        owned,
        token_codec=codec,
        allocator_tag="deterministic-a",
    )
    second = _dispatcher(
        owned,
        token_codec=codec,
        allocator_tag="deterministic-b",
    )
    assert first.session == second.session
    assert first.binding == second.binding
    assert first.supported_capabilities == second.supported_capabilities
    assert first.registry.operations == second.registry.operations


def test_v06_5_s2_default_view_remains_current_canonical(
    owned: m3.m2.Owned,
) -> None:
    dispatcher = _dispatcher(
        owned,
        principal="principal-with-no-sensitive-view-grant",
        allocator_tag="default-view-ungated",
    )
    handler = dispatcher.registry.get("memory.list")
    assert handler is not None
    handlers = getattr(handler, "__self__", None)
    assert isinstance(handlers, MemoryHandlers)
    assert handlers.authorized_views == frozenset()
    for suffix, operation_input in (
        ("absent", {}),
        ("explicit", {"view": "current_canonical"}),
    ):
        response = dispatcher.dispatch(
            _request(
                "memory.list",
                operation_input,
                request_id=f"req-current-canonical-{suffix}",
            )
        )
        assert isinstance(response, SuccessResponseEnvelope)
        assert response.result == {"records": [], "page": {}}
        assert response.metadata.canonical_resolution_time == "2030-03-17T17:46:40.000Z"
        assert response.metadata.audit_reference is None
        assert response.metadata.authority.principal_id == (
            "principal-with-no-sensitive-view-grant"
        )


def test_v06_5_s2_optional_assertion_evidence_uses_its_declared_source(
    owned: m3.m2.Owned,
) -> None:
    m3.m2.seed_chain(owned)
    retrieved = datetime.fromtimestamp(m3.m2.BASE_US / 1_000_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    source = {
        "kind": "filesystem.archive",
        "source_id": "doc-1",
        "locator": "archive://doc.md",
        "retrieved_at": retrieved,
    }
    operation_input = _memory_input("production evidence")
    operation_input.update(
        evidence_disposition="available",
        sources=[source],
        assertion={
            "actor_id": "actor-1",
            "actor_kind": "user",
            "actor_role": "owner",
            "asserted_at": "2024-01-01T00:00:00Z",
            "evidence": [{"source": source}],
        },
    )
    dispatcher = _dispatcher(owned, allocator_tag="evidence")
    response = dispatcher.dispatch(
        _request(
            "memory.create",
            operation_input,
            request_id="req-create-with-assertion-evidence",
            idempotency_key="idem-create-with-assertion-evidence",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    record = response.result["record"]
    assert record["provenance"]["sources"] == [source]
    assert record["provenance"]["assertion"]["evidence"] == [{"source": source}]
    assert response.metadata.audit_reference is not None


def test_v06_5_s2_source_resolution_zero_one_many_is_fail_closed(
    owned: m3.m2.Owned,
) -> None:
    source = _seeded_source()
    dispatcher = _dispatcher(owned, allocator_tag="source-cardinality")

    missing = dispatcher.dispatch(
        _request(
            "memory.create",
            _available_memory_input("zero source matches", source),
            request_id="req-source-zero",
            idempotency_key="idem-source-zero",
        )
    )
    assert isinstance(missing, ErrorResponseEnvelope)
    assert missing.error.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (0,)

    m3.m2.seed_chain(owned)
    resolved = dispatcher.dispatch(
        _request(
            "memory.create",
            _available_memory_input("one source match", source),
            request_id="req-source-one",
            idempotency_key="idem-source-one",
        )
    )
    assert isinstance(resolved, SuccessResponseEnvelope)
    assert resolved.result["record"]["provenance"]["sources"] == [source]

    duplicate = m3.m2.unique_row_for(
        m3.m2.EVIDENCE,
        evidence_id="evd-source-duplicate",
        source_native_id="doc-1",
    )
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        m3.m2.insert(owned.connection, m3.m2.EVIDENCE, duplicate)
    ambiguous = dispatcher.dispatch(
        _request(
            "memory.create",
            _available_memory_input("many source matches", source),
            request_id="req-source-many",
            idempotency_key="idem-source-many",
        )
    )
    assert isinstance(ambiguous, ErrorResponseEnvelope)
    assert ambiguous.error.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone() == (1,)


def test_v06_5_s2_source_resolution_requires_null_safe_exact_lineage(
    owned: m3.m2.Owned,
) -> None:
    m3.m2.seed_chain(owned)
    complete = _seeded_source()
    dispatcher = _dispatcher(owned, allocator_tag="null-safe-source")

    for field in ("locator", "retrieved_at"):
        incomplete = dict(complete)
        incomplete.pop(field)
        refused = dispatcher.dispatch(
            _request(
                "memory.create",
                _available_memory_input(f"missing {field}", incomplete),
                request_id=f"req-source-missing-{field}",
                idempotency_key=f"idem-source-missing-{field}",
            )
        )
        assert isinstance(refused, ErrorResponseEnvelope)
        assert refused.error.code == ERROR_CODE_DEPENDENCY_UNAVAILABLE

    null_artifact = m3.m2.unique_row_for(
        m3.m2.EVIDENCE,
        evidence_id="evd-null-lineage",
        source_native_id="doc-null-lineage",
        source_locator=None,
        source_retrieved_at_us=None,
    )
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        m3.m2.insert(owned.connection, m3.m2.EVIDENCE, null_artifact)
    null_source = {
        "kind": "filesystem.archive",
        "source_id": "doc-null-lineage",
    }
    accepted = dispatcher.dispatch(
        _request(
            "memory.create",
            _available_memory_input("exact null lineage", null_source),
            request_id="req-source-exact-null",
            idempotency_key="idem-source-exact-null",
        )
    )
    assert isinstance(accepted, SuccessResponseEnvelope), accepted
    assert accepted.result["record"]["provenance"]["sources"] == [null_source]


def test_v06_5_s2_evidence_acl_precedes_claim_and_record_materialization(
    owned: m3.m2.Owned,
) -> None:
    m3.m2.seed_chain(owned)
    dispatcher = _dispatcher(owned, allocator_tag="acl-order")
    created = dispatcher.dispatch(
        _request(
            "memory.create",
            _available_memory_input("acl ordered", _seeded_source()),
            request_id="req-acl-order-create",
            idempotency_key="idem-acl-order-create",
        )
    )
    assert isinstance(created, SuccessResponseEnvelope)

    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)
    try:
        denied = read_authorized_memory_snapshot(
            owned.connection,
            workspace_id=m3.WORKSPACE_ID,
            resolution_instant_us=1_900_000_000_000_001,
            view="candidates",
            label_grant=EvidenceLabelGrant(
                principal_id="principal-without-evidence-label",
                workspace_id=m3.WORKSPACE_ID,
                all_labels=False,
                labels=frozenset(),
            ),
        )
    finally:
        owned.connection.set_trace_callback(None)
    assert denied.values == ()
    assert any("omnivia_evidence_permission_labels" in sql for sql in statements)
    assert not any("omnivia_application_claim_lineage" in sql for sql in statements)
    assert not any("content_json" in sql for sql in statements)
    assert not any(
        "omnivia_application_governance_transitions" in sql
        and any(
            material in sql
            for material in (
                "rationale_json",
                "rationale_digest",
                "reason_code",
                "reason_comment",
                "actor_id",
                "audit_ref",
            )
        )
        for sql in statements
    )
    assert not any(
        "omnivia_record_supersessions" in sql and "reason_code" in sql
        for sql in statements
    )


def test_v06_5_s2_acl_and_hydration_share_one_snapshot(
    owned: m3.m2.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m3.m2.seed_chain(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _seed_candidate(
            owned,
            suffix="acl-authorized",
            fact="authorized no-link record",
        )

    # A second record has a two-assembly transition chain whose every support
    # assembly links the seeded `group.engineering` evidence.  The narrow grant
    # below must reject this whole chain while still admitting the no-link record.
    # This mixed frontier proves the post-fold SQL predicates, rather than merely
    # observing that the queries happen after the label query.
    denied_source_at = _TRANSITION_FIRST_SETTLEMENT_US
    denied_target_at = denied_source_at + 100
    denied_claim = _transition_claim_json(source_id="doc-1")
    _insert_transition_application_version(
        owned,
        operation="memory.create",
        audit_ref="audit-acl-denied-source",
        principal_id="principal-1",
        assembly_id="assembly-acl-denied-source",
        version_id="version-acl-denied-source",
        settled_at_us=denied_source_at,
        claim_json=denied_claim,
        claim_ingested_at_us=denied_source_at,
        ordinal=1,
        create_record=True,
        evidence_link=True,
    )
    _insert_transition_application_version(
        owned,
        operation="knowledge.propose",
        audit_ref="audit-acl-denied-target",
        principal_id="principal-1",
        assembly_id="assembly-acl-denied-target",
        version_id="version-acl-denied-target",
        settled_at_us=denied_target_at,
        claim_json=denied_claim,
        claim_ingested_at_us=denied_source_at,
        ordinal=2,
        evidence_link=True,
    )
    _insert_application_transition(
        owned,
        transition_id="transition-acl-denied",
        source_assembly_id="assembly-acl-denied-source",
        source_version_id="version-acl-denied-source",
        target_assembly_id="assembly-acl-denied-target",
        target_version_id="version-acl-denied-target",
        operation="knowledge.propose",
        reason_code="governance.proposed",
        actor_id="principal-1",
        audit_ref="audit-acl-denied-target",
        settled_at_us=denied_target_at,
    )

    observed_transactions: list[bool] = []
    original = memory_storage_module.hydrate_authorized_governed_record_values

    def observe_hydration(*args: Any, **kwargs: Any) -> Any:
        connection = args[0]
        observed_transactions.append(connection.in_transaction)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        memory_storage_module,
        "hydrate_authorized_governed_record_values",
        observe_hydration,
    )
    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)
    try:
        allowed = read_authorized_memory_snapshot(
            owned.connection,
            workspace_id=m3.WORKSPACE_ID,
            resolution_instant_us=denied_target_at + 1,
            view="candidates",
            label_grant=EvidenceLabelGrant(
                principal_id=CONFIGURED_LOCAL_OWNER,
                workspace_id=m3.WORKSPACE_ID,
                all_labels=False,
                labels=frozenset(),
            ),
        )
    finally:
        owned.connection.set_trace_callback(None)
    assert [
        value.record.provenance.identity.record_id for value in allowed.values
    ] == ["rec-acl-authorized"]
    assert observed_transactions == [True]
    normalized = [" ".join(statement.split()) for statement in statements]
    assert sum(statement == "BEGIN" for statement in normalized) == 1
    assert sum(statement == "COMMIT" for statement in normalized) == 1
    label_index = next(
        index
        for index, statement in enumerate(normalized)
        if "omnivia_evidence_permission_labels" in statement
    )
    claim_index = next(
        index
        for index, statement in enumerate(normalized)
        if "omnivia_application_claim_lineage" in statement
    )
    transition_material_index = next(
        index
        for index, statement in enumerate(normalized)
        if "omnivia_application_governance_transitions" in statement
        and "rationale_digest" in statement
    )
    supersession_material_index = next(
        index
        for index, statement in enumerate(normalized)
        if "omnivia_record_supersessions" in statement
        and "reason_code" in statement
    )
    assert label_index < claim_index
    assert label_index < transition_material_index
    assert label_index < supersession_material_index

    post_acl_scopes = {
        "content": [sql for sql in normalized if "content_json" in sql],
        "transitions": [
            sql
            for sql in normalized
            if "omnivia_application_governance_transitions" in sql
            and ("rationale_digest" in sql or "rationale_json" in sql)
        ],
        "supersessions": [
            sql
            for sql in normalized
            if "omnivia_record_supersessions" in sql and "reason_code" in sql
        ],
        "history": [
            sql
            for sql in normalized
            if "FROM omnivia_governed_provenance_events" in sql
        ],
        "sources": [
            sql
            for sql in normalized
            if "JOIN omnivia_evidence_artifacts" in sql
        ],
        "claims": [
            sql
            for sql in normalized
            if "FROM omnivia_application_claim_lineage" in sql
        ],
    }
    assert all(post_acl_scopes.values())
    for family, queries in post_acl_scopes.items():
        expected_scope = (
            "rec-acl-authorized"
            if family in {"transitions", "supersessions"}
            else "asm-acl-authorized"
        )
        denied_scope = (
            "record-memory"
            if family in {"transitions", "supersessions"}
            else "assembly-acl-denied"
        )
        assert all(" IN (" in sql for sql in queries), family
        assert all(expected_scope in sql for sql in queries), family
        assert not any(denied_scope in sql for sql in queries), family


def test_v06_5_s2_page_token_binds_principal_query_and_snapshot(
    owned: m3.m2.Owned,
) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _seed_candidate(owned, suffix="token-a", fact="a")
        _seed_candidate(owned, suffix="token-b", fact="b")

    codec = HmacContinuationTokenCodec(b"token-binding-secret")
    dispatcher = _dispatcher(owned, token_codec=codec, allocator_tag="token")
    first_input: dict[str, object] = {
        "view": "candidates",
        "record_type": "memory.fact",
        "limit": 1,
    }
    first = dispatcher.dispatch(
        _request(
            "memory.list",
            first_input,
            request_id="req-token-first",
        )
    )
    assert isinstance(first, SuccessResponseEnvelope)
    token = first.result["page"]["continuation_token"]
    payload = codec.decode(token)
    assert set(payload) == {"b", "k", "o", "s", "t", "v"}
    assert payload["v"] == 1
    assert payload["o"] == 1
    assert payload["t"] == 1_900_000_000_000_000
    assert isinstance(payload["b"], str)
    assert isinstance(payload["k"], str)
    assert isinstance(payload["s"], str)
    assert len(token) <= 512

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    refusals = [
        dispatcher.dispatch(
            _request(
                "memory.list",
                {**first_input, "page": {"continuation_token": tampered}},
                request_id="req-token-tampered",
            )
        ),
        dispatcher.dispatch(
            _request(
                "memory.list",
                {
                    **first_input,
                    "limit": 2,
                    "page": {"continuation_token": token},
                },
                request_id="req-token-limit-mismatch",
            )
        ),
        dispatcher.dispatch(
            _request(
                "memory.list",
                {
                    **first_input,
                    "record_type": "knowledge.claim",
                    "page": {"continuation_token": token},
                },
                request_id="req-token-filter-mismatch",
            )
        ),
        dispatcher.dispatch(
            _request(
                "memory.list",
                {
                    **first_input,
                    "view": "history",
                    "page": {"continuation_token": token},
                },
                request_id="req-token-view-mismatch",
            )
        ),
    ]
    handler = dispatcher.registry.get("memory.list")
    assert handler is not None
    handlers = getattr(handler, "__self__", None)
    assert isinstance(handlers, MemoryHandlers)
    principal_request = _request(
        "memory.list",
        {**first_input, "page": {"continuation_token": token}},
        request_id="req-token-principal-mismatch",
    )
    with pytest.raises(OperationError) as principal_refusal:
        handlers.memory_list(
            OperationContext(
                request=principal_request,
                principal="another-local-principal",
                workspace_id=m3.WORKSPACE_ID,
                granted_operations=MEMORY_FAMILY_OPERATIONS,
                service=owned,
            )
        )
    assert principal_refusal.value.code == ERROR_CODE_INVALID_REQUEST
    other_workspace = _dispatcher(
        owned,
        workspace_id="workspace-token-other",
        token_codec=codec,
        allocator_tag="token-other-workspace",
    )
    refusals.append(
        other_workspace.dispatch(
            _request(
                "memory.list",
                {**first_input, "page": {"continuation_token": token}},
                request_id="req-token-workspace-mismatch",
                workspace_id="workspace-token-other",
            )
        )
    )
    assert all(isinstance(response, ErrorResponseEnvelope) for response in refusals)
    assert all(
        response.error.code == ERROR_CODE_INVALID_REQUEST
        for response in refusals
        if isinstance(response, ErrorResponseEnvelope)
    )

    # A write at the original deterministic resolution instant changes the frozen
    # frontier digest. Page 2 must fail rather than continue under a new snapshot.
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _seed_candidate(owned, suffix="token-concurrent", fact="concurrent")
    stale = dispatcher.dispatch(
        _request(
            "memory.list",
            {**first_input, "page": {"continuation_token": token}},
            request_id="req-token-stale-snapshot",
        )
    )
    assert isinstance(stale, ErrorResponseEnvelope)
    assert stale.error.code == ERROR_CODE_INVALID_REQUEST


def test_v06_5_s2_list_limit_defaults_and_clamps_to_result_maximum(
    owned: m3.m2.Owned,
) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        for index in range(501):
            _seed_candidate(
                owned,
                suffix=f"limit-{index:04d}",
                fact=f"limit {index:04d}",
            )

    codec = HmacContinuationTokenCodec(b"limit-binding-secret")
    dispatcher = _dispatcher(owned, token_codec=codec, allocator_tag="limits")
    defaulted = dispatcher.dispatch(
        _request(
            "memory.list",
            {"view": "candidates"},
            request_id="req-list-default-limit",
        )
    )
    assert isinstance(defaulted, SuccessResponseEnvelope)
    assert len(defaulted.result["records"]) == 50
    default_token = defaulted.result["page"]["continuation_token"]
    assert len(default_token) <= 512

    clamped = dispatcher.dispatch(
        _request(
            "memory.list",
            {"view": "candidates", "limit": 1000},
            request_id="req-list-clamped-limit",
        )
    )
    assert isinstance(clamped, SuccessResponseEnvelope)
    assert len(clamped.result["records"]) == 500
    clamped_token = clamped.result["page"]["continuation_token"]
    assert len(clamped_token) <= 512

    wrong_effective_limit = dispatcher.dispatch(
        _request(
            "memory.list",
            {
                "view": "candidates",
                "limit": 500,
                "page": {"continuation_token": clamped_token},
            },
            request_id="req-list-clamped-token-rebound",
        )
    )
    assert isinstance(wrong_effective_limit, SuccessResponseEnvelope)
    assert len(wrong_effective_limit.result["records"]) == 1


def test_v06_5_s2_mutation_audit_reference_reaches_all_adapters(
    tmp_path: Path,
) -> None:
    expected_cases = {
        "memory.create/primary-success",
        "memory.create/honest-replay",
        "memory.create/idempotency-conflict",
        "memory.get/primary-success",
        "memory.list/primary-success",
        "memory.list/page-2",
    }
    ledger: list[tuple[str, str]] = []
    for adapter in ("in-process", "local-ipc", "http"):
        path = tmp_path / adapter / "workspace.sqlite"
        path.parent.mkdir(parents=True)
        _apply_through(path, MIGRATION_VERSION, workspace_id=m3.WORKSPACE_ID)
        holder = m3.m2.take_ownership(path)
        dispatcher = _dispatcher(holder, allocator_tag=f"h-{adapter[0]}")
        try:
            primary_request = _request(
                "memory.create",
                _memory_input("primary"),
                request_id=f"req-{adapter}-create-primary",
                idempotency_key=f"idem-{adapter}-create-primary",
            )
            primary_response = _transport_call(adapter, dispatcher, primary_request)
            assert isinstance(primary_response, SuccessResponseEnvelope)
            primary_audit = primary_response.metadata.audit_reference
            assert primary_audit is not None
            primary_record = primary_response.result["record"]
            ledger.append(("memory.create/primary-success", adapter))

            replay_response = _transport_call(
                adapter,
                dispatcher,
                replace(
                    primary_request,
                    metadata=replace(
                        primary_request.metadata,
                        request_id=f"req-{adapter}-create-replay",
                    ),
                ),
            )
            assert isinstance(replay_response, SuccessResponseEnvelope)
            assert replay_response.result == primary_response.result
            assert replay_response.metadata.audit_reference == primary_audit
            assert holder.connection.execute(
                "SELECT COUNT(*) FROM omnivia_application_claim_lineage"
            ).fetchone() == (1,)
            ledger.append(("memory.create/honest-replay", adapter))

            conflict_response = _transport_call(
                adapter,
                dispatcher,
                replace(
                    primary_request,
                    metadata=replace(
                        primary_request.metadata,
                        request_id=f"req-{adapter}-create-conflict",
                    ),
                    input=_memory_input("conflicting claim"),
                ),
            )
            assert isinstance(conflict_response, ErrorResponseEnvelope)
            assert conflict_response.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
            assert conflict_response.metadata.audit_reference == primary_audit
            ledger.append(("memory.create/idempotency-conflict", adapter))

            seeded_response = _transport_call(
                adapter,
                dispatcher,
                _request(
                    "memory.create",
                    _memory_input("second"),
                    request_id=f"req-{adapter}-create-second",
                    idempotency_key=f"idem-{adapter}-create-second",
                ),
            )
            assert isinstance(seeded_response, SuccessResponseEnvelope)

            get_response = _transport_call(
                adapter,
                dispatcher,
                _request(
                    "memory.get",
                    {
                        "record_id": primary_record["provenance"]["identity"][
                            "record_id"
                        ],
                        "view": "candidates",
                    },
                    request_id=f"req-{adapter}-get-primary",
                ),
            )
            assert isinstance(get_response, SuccessResponseEnvelope)
            assert get_response.result["record"]["content"] == {"fact": "primary"}
            assert get_response.metadata.audit_reference is None
            ledger.append(("memory.get/primary-success", adapter))

            first_page = _transport_call(
                adapter,
                dispatcher,
                _request(
                    "memory.list",
                    {"view": "candidates", "limit": 1},
                    request_id=f"req-{adapter}-list-primary",
                ),
            )
            assert isinstance(first_page, SuccessResponseEnvelope)
            assert len(first_page.result["records"]) == 1
            assert first_page.metadata.audit_reference is None
            continuation = first_page.result["page"]["continuation_token"]
            ledger.append(("memory.list/primary-success", adapter))

            second_page = _transport_call(
                adapter,
                dispatcher,
                _request(
                    "memory.list",
                    {
                        "view": "candidates",
                        "limit": 1,
                        "page": {"continuation_token": continuation},
                    },
                    request_id=f"req-{adapter}-list-page-2",
                ),
            )
            assert isinstance(second_page, SuccessResponseEnvelope)
            assert len(second_page.result["records"]) == 1
            assert second_page.metadata.audit_reference is None
            assert (
                first_page.result["records"][0]["provenance"]["identity"][
                    "record_id"
                ]
                != second_page.result["records"][0]["provenance"]["identity"][
                    "record_id"
                ]
            )
            ledger.append(("memory.list/page-2", adapter))
        finally:
            holder.connection.close()

    assert len(ledger) == 18
    assert len(set(ledger)) == 18
    assert {case for case, _adapter in ledger} == expected_cases
    assert {adapter for _case, adapter in ledger} == {
        "in-process",
        "local-ipc",
        "http",
    }
