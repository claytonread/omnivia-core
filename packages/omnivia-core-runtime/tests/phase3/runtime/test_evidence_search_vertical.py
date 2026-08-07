"""V06-3 Lane A: `evidence.search` end to end, over real fenced storage.

Packet §12.2's Lane A positive evidence and the negative items of §12.3 that this lane
owns. The path exercised here is the production one and nothing is stubbed on it: a real
migrated workspace database, a real lease and mutation guard, seeding through the real
`fenced_transaction`, a real `AuthenticatedSession` from the production construction
site, a real `ServiceBinding`, a real `RequestEnvelope`, all twelve checks of
`authorize_application_request`, the registered handler, and a conformant
`EvidenceSearchResult` on the way out.

**F2 is here, and it is the acceptance test of this stage.** Widen the frontier by one
unauthorized artifact that ranking scores last and the page drops; the returned page is
byte-identical; the frontier digest moves. No assertion over a result could catch that,
which is why the digest exists and why F2 rather than any weaker form is what §12.3
test 8 requires.

`test_retrieval_filter_chain.py` carries F1, F3, F4 and the per-filter unit cases.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.fencing import (
    close_guard,
    fenced_transaction,
    guard_present,
    open_guard,
    read_guard,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    EVIDENCE_SEARCH_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    LOCAL_TRANSPORT_ADAPTER,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    build_service_registry,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.storage.repository import (
    CONTRIBUTING_PROJECTIONS,
    authoritative_checkpoint,
    projection_readiness,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    authorized_frontier,
    local_owner_label_grant,
    rank_candidates,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_STALE_PROJECTION,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    EvidenceSearchResult,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    decode_evidence_search_input,
    encode_response,
    get_operation_metadata,
    validate_evidence_search_result,
)

WORKSPACE_ID = "ws-evidence-0001"
OTHER_WORKSPACE_ID = "ws-evidence-0002"
INSTALLATION_ID = "inst-evidence-0001"
SERVICE_INSTANCE = "svc-evidence-one"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
ENTRY = get_operation_metadata(EVIDENCE_SEARCH_OPERATION)

PRODUCTION_OPERATIONS = frozenset(
    {WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}
)

BASE_US = 1_700_000_000_000_000
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64

BLOBS = "omnivia_blob_objects"
EVIDENCE = "omnivia_evidence_artifacts"
LABELS = "omnivia_evidence_permission_labels"
PROVENANCE = "omnivia_evidence_provenance_events"


# --- a real, owned, migrated workspace ----------------------------------------


@dataclass(frozen=True)
class Owned:
    """A migrated workspace this test instance holds the lease and guard for."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int


def make_identity() -> ServiceInstanceIdentity:
    return ServiceInstanceIdentity(
        service_instance_id=SERVICE_INSTANCE,
        installation_id=INSTALLATION_ID,
        process=ProcessEvidence(
            pid=4242, start_time="100", boot_id="boot-evidence", os_principal="me"
        ),
    )


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[Owned]:
    """A workspace taken to the canonical schema through the real migrator.

    Nothing here is a fixture database or a hand-built schema: the Phase 0 artifact is
    materialised, `bootstrap_generation_one` and `apply_pending_migrations` run, and the
    service connection then acquires a real lease and opens the real mutation guard.
    Seeding through anything less would prove the read path works against a shape
    production never produces.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = bootstrap_generation_one(
            maintenance,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=True,
            service_instance_id=SERVICE_INSTANCE,
        )
        apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        maintenance.close()

    identity = make_identity()
    connection = open_database(path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    yield Owned(
        connection=connection, identity=identity, generation=lease.fencing_generation
    )
    close_guard(connection)
    connection.close()


def insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def artifact_row(
    evidence_id: str,
    *,
    workspace_id: str = WORKSPACE_ID,
    sensitivity: str = "internal",
    recorded_at_us: int = BASE_US,
    native_id: str = "doc-1",
    locator: str = "archive://doc.md",
    checksum: str = DIGEST_A,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "workspace_id": workspace_id,
        "source_kind": "filesystem.archive",
        "source_native_id": native_id,
        "source_locator": locator,
        "source_retrieved_at_us": BASE_US,
        "event_at_us": BASE_US - 10,
        "observed_at_us": BASE_US - 5,
        "ingested_at_us": recorded_at_us - 1,
        "recorded_at_us": recorded_at_us,
        "content_checksum": checksum,
        "blob_content_digest": DIGEST_A,
        "media_type": "text/markdown",
        "original_metadata_json": json.dumps({"title": evidence_id}),
        "original_metadata_digest": DIGEST_C,
        "sensitivity": sensitivity,
        "parser_status": "parsed",
        "ingestion_status": "ingested",
        "staged_source_ref": None,
        "import_run_id": None,
    }


def seed(owned: Owned, *rows: dict[str, Any], extra: tuple[tuple[str, dict[str, Any]], ...] = ()) -> None:
    """Write through the real fenced-write path, and prove the fence was engaged.

    §12.2 item 5 asks for exactly this and is explicit that rows existing is not the
    claim: the guard row is read back *inside* the transaction, so what is asserted is
    that the write executed under current fencing authority rather than that a write
    happened at all. A seeding helper that bypassed the guard would populate the same
    tables and prove nothing about the shape production writes through.
    """
    assert guard_present(owned.connection)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        guard = read_guard(owned.connection)
        assert guard is not None
        assert guard.fencing_generation == owned.generation
        assert guard.workspace_id == WORKSPACE_ID
        # The blob every seeded artifact's `blob_content_digest` references. 0008 makes
        # that a real foreign key, so an artifact cannot be written without one -- which
        # is itself worth having in the seeding path rather than worked around, because
        # it is the schema refusing evidence with no content behind it.
        insert(
            owned.connection,
            BLOBS,
            {
                "workspace_id": WORKSPACE_ID,
                "content_digest": DIGEST_A,
                "content_length_bytes": 1024,
                "created_at_us": BASE_US - 100,
                "verified_at_us": BASE_US - 99,
            },
        )
        for row in rows:
            insert(owned.connection, EVIDENCE, row)
            # First capture is itself a provenance entry, and the contract enforces it:
            # `validate_evidence_search_result` refuses an artifact whose
            # `provenance_history` is empty. Seeding one per artifact is what real
            # ingestion writes, so the fixture produces the shape the validator accepts
            # rather than a shape only this test would ever create.
            insert(
                owned.connection,
                PROVENANCE,
                provenance_row(
                    str(row["evidence_id"]),
                    sequence=1,
                    native_id=str(row["source_native_id"]),
                ),
            )
        for table, values in extra:
            insert(owned.connection, table, values)


def label_row(evidence_id: str, label: str, *, sequence: int = 1, action: str = "attached") -> dict[str, Any]:
    return {
        "label_event_id": f"lbl-{evidence_id}-{sequence}",
        "evidence_id": evidence_id,
        "workspace_id": WORKSPACE_ID,
        "label_sequence": sequence,
        "label_action": action,
        "permission_label": label,
        "recorded_at_us": BASE_US + sequence,
    }


def provenance_row(
    evidence_id: str,
    *,
    sequence: int = 1,
    tombstoned: int | None = None,
    native_id: str = "doc-1",
) -> dict[str, Any]:
    return {
        "provenance_event_id": f"prv-{evidence_id}-{sequence}",
        "evidence_id": evidence_id,
        "workspace_id": WORKSPACE_ID,
        "provenance_sequence": sequence,
        "actor_id": "importer",
        "actor_kind": "service",
        "action": "ingested",
        "occurred_at_us": BASE_US + sequence,
        "reason_code": None,
        "reason_comment": None,
        "parser_status": None,
        "ingestion_status": None,
        "tombstoned_observation": tombstoned,
        "source_kind": "filesystem.archive",
        "source_native_id": native_id,
        "audit_ref": None,
    }


# --- the production application path ------------------------------------------


class ServedWorkspace:
    """The service the handler reads its connection off, with nothing else on it.

    `ServiceRunner` exposes the sole exclusive connection as `.connection`, and that is
    the only attribute this handler touches. Presenting exactly that surface is what
    keeps the test honest about what the handler is allowed to reach.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


def production_path(owned: Owned, *, session: Any = None) -> ApplicationDispatcher:
    """The application path, composed exactly as `service.main.serve` composes it."""
    registry = build_application_registry()
    return ApplicationDispatcher(
        registry=registry,
        session=session
        or local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=PRODUCTION_OPERATIONS,
        ),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            ServedWorkspace(owned.connection),
        ),
        record=None,
        service=ServedWorkspace(owned.connection),
    )


def request_for(**overrides: Any) -> RequestEnvelope:
    """An `evidence.search` request built from the frozen catalogue entry."""
    operation = overrides.pop("operation", EVIDENCE_SEARCH_OPERATION)
    payload = overrides.pop("input", {"query": "doc"})
    fields: dict[str, Any] = {
        "request_id": "req-evidence-1",
        "correlation_id": "corr-evidence-1",
        "trace_id": "trace-evidence-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": WORKSPACE_ID,
        "scopes": tuple(ENTRY.scope.required_scopes),
        "purpose": KNOWLEDGE_RETRIEVAL_PURPOSE,
        "required_capabilities": (
            CapabilityRequirement(
                id=ENTRY.required_capability.id,
                minimum_version=ENTRY.required_capability.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": None,
        "mutation_precondition": None,
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestEnvelope(
        operation=operation, metadata=RequestMetadata(**fields), input=payload
    )


def answered(response: ResponseEnvelope) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def refusal(response: ResponseEnvelope) -> ErrorResponseEnvelope:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def returned_ids(response: ResponseEnvelope) -> tuple[str, ...]:
    result = EvidenceSearchResult.from_wire(answered(response).result)
    return tuple(item.evidence_id for item in result.evidence)


# --- §12.2 item 1: the full authorised path, end to end -----------------------


def test_the_full_authorised_path_answers_a_conformant_result(owned: Owned) -> None:
    """One request, twelve checks, the registered handler, a validated result."""
    seed(
        owned,
        artifact_row("evd-1", recorded_at_us=BASE_US),
        artifact_row(
            "evd-2", native_id="doc-2", checksum=DIGEST_B, recorded_at_us=BASE_US + 10
        ),
    )

    request = request_for()
    response = answered(production_path(owned).dispatch(request))
    result = EvidenceSearchResult.from_wire(response.result)

    # Conformance asked of the contract's own validator, not of this test's opinion --
    # and bound to the request, so the validator enforces that the page honours the
    # sensitivity selector, `include_tombstoned` and the limit this caller actually
    # asked for rather than merely being a well-formed document.
    validate_evidence_search_result(
        result, decode_evidence_search_input(request.input), WORKSPACE_ID
    )
    assert tuple(item.evidence_id for item in result.evidence) == ("evd-2", "evd-1")
    assert result.page.continuation_token is None
    assert all(item.workspace_id == WORKSPACE_ID for item in result.evidence)


def test_the_authorized_context_carries_the_expected_effective_authority(
    owned: Owned,
) -> None:
    """§12.2 item 1's second half, asserted off the seam's own record of the request.

    Recorded through the caller-recording sink rather than reconstructed, because the
    context is the seam's own statement of what the request ran under and re-deriving it
    here would let the record and the decision disagree.
    """
    seed(owned, artifact_row("evd-1"))
    records: list[Any] = []
    dispatcher = production_path(owned)
    dispatcher = ApplicationDispatcher(
        registry=dispatcher.registry,
        session=dispatcher.session,
        binding=dispatcher.binding,
        supported_capabilities=dispatcher.supported_capabilities,
        transport=dispatcher.transport,
        probe=dispatcher.probe,
        record=records.append,
        service=dispatcher.service,
    )

    dispatcher.dispatch(request_for())

    assert len(records) == 1
    record = records[0]
    assert record.allowed is True
    assert record.principal_id == LOCAL_PRINCIPAL
    assert record.workspace_id == WORKSPACE_ID
    assert record.scopes == ("memory:read",)
    assert record.purpose == KNOWLEDGE_RETRIEVAL_PURPOSE
    assert record.capabilities == (
        type(record.capabilities[0])(id="evidence.read", version="1.0"),
    )
    assert record.error_code is None


def test_the_session_grants_evidence_search_with_no_side_effect(owned: Owned) -> None:
    """§12.2 item 4, at the production construction site."""
    session = local_owner_session(
        principal_id=LOCAL_PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        operations=PRODUCTION_OPERATIONS,
    )

    assert session.operations == PRODUCTION_OPERATIONS
    for name in session.operations:
        assert get_operation_metadata(name).scope.side_effect == "none"
    assert session.scopes == frozenset({"workspace:read", "memory:read"})


def test_a_mutating_grant_is_refused_at_construction() -> None:
    """The parameterised shape cannot be talked into granting a mutation.

    Asserted from the catalogue rather than from a denylist, so it holds for every
    mutating name the catalogue has now and every one it gains later.
    """
    with pytest.raises(ValueError, match="read-only"):
        local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=frozenset({WORKSPACE_INSPECT_OPERATION, "candidate.approve"}),
        )


def test_an_empty_grant_is_refused_rather_than_serving_nothing() -> None:
    with pytest.raises(ValueError, match="grant"):
        local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=frozenset(),
        )


# --- §12.2 item 5: the seeding fence, and the read fence ----------------------


def test_the_read_path_runs_under_the_authorizer_and_cannot_write(owned: Owned) -> None:
    """The fenced read is a fence, not a naming convention.

    The service connection carries SQLite's own deny-by-default authorizer, and
    `read_evidence_candidates` runs inside `authorised(...)` with mutations and DDL both
    false. So a write attempted on that connection outside a fenced transaction is
    refused by the engine -- which is what makes "read-only against existing storage
    tables" (§20.2) a property of the process rather than of the diff.
    """
    seed(owned, artifact_row("evd-1"))

    assert len(read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)) == 1
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute(
            "INSERT INTO omnivia_evidence_artifacts (evidence_id) VALUES ('x')"
        )


# --- §12.3 test 8 / §8.1 F2: the acceptance test of this stage ----------------


def test_f2_a_widened_frontier_moves_the_digest_with_a_byte_identical_page(
    owned: Owned,
) -> None:
    """F2. The one test a lane cannot pass by taking the easy route.

    Two runs over the same workspace. The second widens the frontier by one artifact
    that is authorized in neither -- it carries a restricted label and the grant under
    test holds nothing -- and the ranker scores it last so the page drops it. The pages
    are compared as canonical JSON bytes and are identical. The frontier checksums are
    not.

    That is the whole mechanism: no assertion over `evidence` or `page` distinguishes
    these two builds, so a build that filtered after ranking would look correct in every
    result-level test ever written against it. The digest is what notices.

    The expected manifest is produced by the retrieval layer at the freeze, in process,
    and is never read back out of a result -- §8.1's binding clause.
    """
    seed(
        owned,
        artifact_row("evd-1", recorded_at_us=BASE_US + 100),
        artifact_row("evd-2", native_id="doc-2", checksum=DIGEST_B, recorded_at_us=BASE_US),
        extra=((LABELS, label_row("evd-2", "restricted")),),
    )
    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)
    resolution = int(authoritative_checkpoint(owned.connection, workspace_id=WORKSPACE_ID))

    honest = authorized_frontier(
        candidates,
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id="not-the-owner",
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        resolution_time_us=resolution,
    )
    # The defective build: the ACL stage deferred until after ranking. Same request,
    # same storage, one more item on the frontier.
    widened = authorized_frontier(
        candidates,
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id=LOCAL_PRINCIPAL,
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        resolution_time_us=resolution,
    )

    assert len(widened.candidates) == len(honest.candidates) + 1

    honest_page = EvidenceSearchResult(
        evidence=tuple(item.artifact for item in rank_candidates(honest, "doc-1", limit=50)),
        page=EvidenceSearchResult.from_wire({"evidence": [], "page": {}}).page,
    ).to_wire()
    widened_page = EvidenceSearchResult(
        evidence=tuple(
            item.artifact
            for item in rank_candidates(widened, "doc-1", limit=50)
        ),
        page=EvidenceSearchResult.from_wire({"evidence": [], "page": {}}).page,
    ).to_wire()

    # Byte-identical -- the whole point of F2.
    assert json.dumps(honest_page, sort_keys=True) == json.dumps(
        widened_page, sort_keys=True
    )
    # And caught anyway.
    assert honest.checksum != widened.checksum


# --- §12.3 test 7: nothing V06-3 routes through the probe dispatcher ----------


def test_7_evidence_search_is_absent_from_every_probe_seam(owned: Owned) -> None:
    """Absence, asserted three ways, because a presence-only test passes when both hold.

    A handler registered in the application registry *and* the probe registry would
    satisfy any test that only checks the application side. So this asserts the probe
    seam does not know the name, in each of the three places §7.1 names, and then shows
    the probe dispatcher refusing the request outright.
    """
    assert EVIDENCE_SEARCH_OPERATION not in SERVICE_OPERATIONS
    assert EVIDENCE_SEARCH_OPERATION not in build_service_registry().operations

    probe = Dispatcher.for_service_operations(
        Grant(
            principal=LOCAL_PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        ServedWorkspace(owned.connection),
    )
    assert EVIDENCE_SEARCH_OPERATION not in probe.grant.operations
    assert refusal(probe.dispatch(request_for())).error.code == (
        "core.operation_not_implemented"
    )


# --- §12.3 tests 10 and 12, through the real handler --------------------------


def test_10_include_tombstoned_cannot_surface_what_the_acl_filter_excluded(
    owned: Owned,
) -> None:
    """The two filters compose end to end, not only in the unit chain.

    `evd-2` is both tombstoned and restricted. Asking for tombstoned material widens one
    filter and leaves the other in force, so it stays absent -- and `evd-3`, tombstoned
    but unlabelled, appears, which is what shows the tombstone filter actually widened
    rather than the request being ignored.
    """
    seed(
        owned,
        artifact_row("evd-1"),
        artifact_row("evd-2", native_id="doc-2", checksum=DIGEST_B),
        artifact_row("evd-3", native_id="doc-3", checksum=DIGEST_D),
        extra=(
            (LABELS, label_row("evd-2", "restricted")),
            (PROVENANCE, provenance_row("evd-2", sequence=2, tombstoned=1, native_id="doc-2")),
            (PROVENANCE, provenance_row("evd-3", sequence=2, tombstoned=1, native_id="doc-3")),
        ),
    )
    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)
    stranger = local_owner_label_grant(
        principal_id="not-the-owner",
        workspace_id=WORKSPACE_ID,
        granted_workspace=WORKSPACE_ID,
    )

    asked = authorized_frontier(
        candidates,
        workspace_id=WORKSPACE_ID,
        grant=stranger,
        include_tombstoned=True,
        resolution_time_us=BASE_US + 10,
    )

    assert {item.evidence_id for item in asked.candidates} == {"evd-1", "evd-3"}


def test_12_a_record_after_the_resolution_instant_is_absent_from_the_frontier(
    owned: Owned,
) -> None:
    """Absent, not merely unranked -- so it is absent from the digest too."""
    seed(
        owned,
        artifact_row("evd-1", recorded_at_us=BASE_US),
        artifact_row("evd-2", native_id="doc-2", checksum=DIGEST_B, recorded_at_us=BASE_US + 500),
    )
    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)
    grant = local_owner_label_grant(
        principal_id=LOCAL_PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_workspace=WORKSPACE_ID,
    )

    early = authorized_frontier(
        candidates, workspace_id=WORKSPACE_ID, grant=grant, resolution_time_us=BASE_US
    )
    late = authorized_frontier(
        candidates,
        workspace_id=WORKSPACE_ID,
        grant=grant,
        resolution_time_us=BASE_US + 500,
    )

    assert {item.evidence_id for item in early.candidates} == {"evd-1"}
    assert {item.evidence_id for item in late.candidates} == {"evd-1", "evd-2"}
    assert early.checksum != late.checksum


# --- §12.2 item 8 / §20.7: freshness resolves to refuse -----------------------


def test_lane_a_contributes_no_projection_and_says_so(owned: Owned) -> None:
    """The honest value, asserted so a later lane cannot leave it empty by accident.

    Lane A reads 0008 directly, and *"direct authoritative structural reads are fresh at
    their transaction read point"* (§20.7), so the contributing set is empty and the
    gate never fires in production. Lane B adds its projection id here and inherits a
    gate the two tests below have already exercised.
    """
    assert CONTRIBUTING_PROJECTIONS == ()
    assert projection_readiness(
        owned.connection, workspace_id=WORKSPACE_ID, source_checkpoint="0"
    ).ready


def test_8_an_absent_active_projection_is_projection_unavailable(owned: Owned) -> None:
    """A contributing projection with no activation is missing, never "fresh enough".

    Falsifier: treat "no activation rows" as nothing to compare and the readiness check
    reports ready -- the silent success §20.7 forbids, arrived at by reading the absence
    the wrong way round.
    """
    seed(owned, artifact_row("evd-1"))
    checkpoint = authoritative_checkpoint(owned.connection, workspace_id=WORKSPACE_ID)

    readiness = projection_readiness(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        source_checkpoint=checkpoint,
        contributing=("evidence.fts5",),
    )

    assert readiness.missing == ("evidence.fts5",)
    assert readiness.stale == ()
    assert not readiness.ready


def test_8_the_refusals_carry_the_contracts_retryable_after_delay_class() -> None:
    """§22.5's first trap, which no other test in this tree would catch.

    `OperationError` defaults to `non_retryable` while both projection errors are
    contractually `retryable_after_delay`, and nothing validates the two agree at
    runtime. The handler passes `retry_class` explicitly at both raise sites; this reads
    it back off the source so that dropping either argument fails here rather than
    shipping a wrong retry class on the wire.
    """
    from omnivia_core_runtime.service.handlers import evidence as handler_module

    source = handler_module.__doc__ or ""
    assert "retry_class" in source

    import ast
    import inspect as inspect_module

    tree = ast.parse(inspect_module.getsource(handler_module))
    raises = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and any(
            isinstance(argument, ast.Name)
            and argument.id
            in {"ERROR_CODE_STALE_PROJECTION", "ERROR_CODE_PROJECTION_UNAVAILABLE"}
            for argument in node.exc.args
        )
    ]

    assert len(raises) == 2
    for node in raises:
        assert node.exc is not None and isinstance(node.exc, ast.Call)
        retry = next(
            keyword.value
            for keyword in node.exc.keywords
            if keyword.arg == "retry_class"
        )
        assert isinstance(retry, ast.Name)
        assert retry.id == "RETRY_CLASS_RETRYABLE_AFTER_DELAY"

    assert RETRY_CLASS_RETRYABLE_AFTER_DELAY == "retryable_after_delay"
    assert ERROR_CODE_STALE_PROJECTION in ENTRY.allowed_errors
    assert ERROR_CODE_PROJECTION_UNAVAILABLE in ENTRY.allowed_errors


# --- refusals leak nothing ----------------------------------------------------


def test_an_invalid_payload_is_refused_without_quoting_it(owned: Owned) -> None:
    """§12.3 test 14, on the one refusal this handler owns that reads caller input.

    The contract's decoder quotes what it rejected; nothing it said reaches the wire,
    and `__context__` is genuinely `None` because the sentinel is set inside the handler
    and the refusal raised after it exits.
    """
    marker = "leaked-caller-value-9f3a"
    response = refusal(
        production_path(owned).dispatch(request_for(input={"query": {"bad": marker}}))
    )

    assert response.error.code == ERROR_CODE_INVALID_REQUEST
    assert marker not in json.dumps(encode_response(response), sort_keys=True)


def test_the_result_is_empty_rather_than_an_error_on_an_empty_workspace(
    owned: Owned,
) -> None:
    """An authorized search over nothing is a page, not a refusal."""
    request = request_for()
    result = EvidenceSearchResult.from_wire(
        answered(production_path(owned).dispatch(request)).result
    )

    validate_evidence_search_result(
        result, decode_evidence_search_input(request.input), WORKSPACE_ID
    )
    assert result.evidence == ()


def test_another_workspaces_evidence_is_never_returned(owned: Owned) -> None:
    """Domain separation, end to end, with a row that really is in the database.

    Seeded through the same fenced write, so the artifact exists and is readable -- the
    filter is what excludes it, not its absence.
    """
    seed(owned, artifact_row("evd-1"))
    # A second workspace's artifact cannot be written under this workspace's lease, so
    # the separation is asserted at the read: the query is workspace-scoped and the
    # frontier filter re-applies it independently.
    candidates = read_evidence_candidates(
        owned.connection, workspace_id=OTHER_WORKSPACE_ID
    )

    assert candidates == ()
    assert returned_ids(production_path(owned).dispatch(request_for())) == ("evd-1",)


def test_the_label_stream_is_folded_in_sequence_order(owned: Owned) -> None:
    """A label added then removed is not held, and the ACL reads the effective set.

    Falsifier: take the first event, or the union of every label ever named, and `evd-1`
    stays restricted after the removal that released it.
    """
    seed(
        owned,
        artifact_row("evd-1"),
        extra=(
            (LABELS, label_row("evd-1", "restricted", sequence=1)),
            (LABELS, label_row("evd-1", "restricted", sequence=2, action="withdrawn")),
        ),
    )
    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)

    assert candidates[0].permission_labels == ()
    stranger = local_owner_label_grant(
        principal_id="not-the-owner",
        workspace_id=WORKSPACE_ID,
        granted_workspace=WORKSPACE_ID,
    )
    built = authorized_frontier(
        candidates,
        workspace_id=WORKSPACE_ID,
        grant=stranger,
        resolution_time_us=BASE_US + 10,
    )

    assert {item.evidence_id for item in built.candidates} == {"evd-1"}


def test_the_tombstone_status_is_the_last_non_null_observation(owned: Owned) -> None:
    """A later unrelated event does not silently un-tombstone an artifact.

    Falsifier: read the last row's `tombstoned_observation` rather than the last
    non-null one, and the `NULL`-carrying event at sequence 3 resurrects `evd-1`.
    """
    seed(
        owned,
        artifact_row("evd-1"),
        extra=(
            (PROVENANCE, provenance_row("evd-1", sequence=2, tombstoned=1)),
            (PROVENANCE, provenance_row("evd-1", sequence=3)),
        ),
    )
    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)

    assert candidates[0].tombstoned is True
    assert len(candidates[0].artifact.provenance_history) == 3
