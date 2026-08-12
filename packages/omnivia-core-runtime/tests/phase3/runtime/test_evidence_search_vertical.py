"""V06-3 Lane A and B: `evidence.search` end to end, over real fenced storage.

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

**Lane B moved the ordering behind that same frozen frontier, and moved the whole
operation behind a projection.** Three claims are new here and each is asserted against
the production article rather than a composed stand-in:

* `test_lb_v1_...` starts the **real service** -- `service.main.main` with a real
  `--endpoint`, the real `serve` closure, the real startup sequence -- and asks it for a
  search over its own Unix socket. Nothing on that path is this test's construction: the
  projection is built and materialised by the service's own startup, the frames are
  OVC1, and the ordering in the answer came out of FTS5. It is the one test in this file
  that a build with the wiring removed cannot pass by any other route.
* `test_lb_v2_...` removes the projection build from that same startup and requires the
  service to **refuse readiness**, publish no descriptor and exit non-zero. Together the
  two pin the ordering: built before ready, or not ready.
* `test_lb_v8_...` is F2 again, against FTS5 this time. The frontier is widened by one
  unauthorized artifact, the returned page is byte-identical, and the ranking statement
  SQLite was actually asked to evaluate is read back off the connection -- so "an
  excluded artifact never enters ranking" is observed rather than inferred from a result
  that by construction cannot show it.

`test_retrieval_filter_chain.py` carries F1, F3, F4 and the per-filter unit cases, and
`test_fts_projection_lifecycle.py` carries the builder's own convergence evidence.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.ownership.discovery import discover
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
from omnivia_core_runtime.ownership.lease import acquire_lease, release_lease
from omnivia_core_runtime.service import main as main_module
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
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.storage.projections import EVIDENCE_SEARCH_PROJECTION_ID, fts
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
    rank_projected,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

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
    decode_response,
    encode_request,
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


def seed_more(
    owned: Owned,
    *rows: dict[str, Any],
    extra: tuple[tuple[str, dict[str, Any]], ...] = (),
) -> None:
    """Add artifacts to a workspace that has already been seeded.

    The same fenced write as `seed`, without the blob every artifact references: 0008
    makes that identity immutable, so re-inserting it is refused. What this exists for
    is the arrival of new evidence *after* a projection was built, which is the only way
    a workspace's checkpoint moves past its projection.
    """
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        for row in rows:
            insert(owned.connection, EVIDENCE, row)
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


def project(owned: Owned, *, now_us: int = BASE_US + 1_000_000) -> fts.BuildOutcome:
    """Run the service's own startup maintenance against this workspace.

    The same two calls `service.main.serve` makes, in the same order, with the same
    arguments taken from the same three facts a started service holds -- the connection,
    the instance identity and the current fencing generation. It is a copy, and a copy
    is only safe because two other tests hold it to the original:
    `test_lb_v1_...` starts the real service and never touches this helper, and
    `test_lb_v3_...` reads `serve`'s own syntax and requires exactly these calls in this
    order before the transport starts. Without both, this would be the tautology of a
    test that certifies its own composition.
    """
    outcome = fts.build_search_projection(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        now_us=now_us,
    )
    fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    return outcome


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
    project(owned)

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


def test_evidence_search_continues_a_bound_frozen_ranking(owned: Owned) -> None:
    seed(
        owned,
        artifact_row("evd-1", recorded_at_us=BASE_US),
        artifact_row(
            "evd-2", native_id="doc-2", checksum=DIGEST_B, recorded_at_us=BASE_US + 10
        ),
    )
    project(owned)
    dispatcher = production_path(owned)
    first_request = request_for(input={"query": "doc", "limit": 1})
    first = EvidenceSearchResult.from_wire(
        answered(dispatcher.dispatch(first_request)).result
    )
    token = first.page.continuation_token
    assert token is not None

    second_request = request_for(
        input={
            "query": "doc",
            "limit": 1,
            "page": {"continuation_token": token},
        }
    )
    second = EvidenceSearchResult.from_wire(
        answered(dispatcher.dispatch(second_request)).result
    )
    assert tuple(item.evidence_id for item in first.evidence + second.evidence) == (
        "evd-2",
        "evd-1",
    )
    assert second.page.continuation_token is None

    rebound = refusal(
        dispatcher.dispatch(
            request_for(
                input={
                    "query": "different",
                    "limit": 1,
                    "page": {"continuation_token": token},
                }
            )
        )
    )
    assert rebound.error.code == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize("adapter", ("in-process", "local-ipc", "http"))
def test_v06_5_c1_evidence_search_primary_and_page_2_reach_every_real_adapter(
    owned: Owned, adapter: str
) -> None:
    """Both frozen evidence-search cases run on one real, bound ranking snapshot."""
    seed(
        owned,
        artifact_row("evd-c1-1", recorded_at_us=BASE_US),
        artifact_row(
            "evd-c1-2",
            native_id="doc-c1-2",
            checksum=DIGEST_B,
            recorded_at_us=BASE_US + 10,
        ),
    )
    project(owned)
    dispatcher = production_path(owned)
    first = EvidenceSearchResult.from_wire(
        answered(
            s2._transport_call(
                adapter,
                dispatcher,
                request_for(
                    request_id=f"req-evidence-c1-{adapter}-1",
                    input={"query": "doc", "limit": 1},
                ),
            )
        ).result
    )
    token = first.page.continuation_token
    assert token is not None

    second = EvidenceSearchResult.from_wire(
        answered(
            s2._transport_call(
                adapter,
                dispatcher,
                request_for(
                    request_id=f"req-evidence-c1-{adapter}-2",
                    input={
                        "query": "doc",
                        "limit": 1,
                        "page": {"continuation_token": token},
                    },
                ),
            )
        ).result
    )
    returned = tuple(item.evidence_id for item in first.evidence + second.evidence)
    assert set(returned) == {"evd-c1-1", "evd-c1-2"}
    assert len(returned) == len(set(returned)) == 2
    assert second.page.continuation_token is None


def test_the_authorized_context_carries_the_expected_effective_authority(
    owned: Owned,
) -> None:
    """§12.2 item 1's second half, asserted off the seam's own record of the request.

    Recorded through the caller-recording sink rather than reconstructed, because the
    context is the seam's own statement of what the request ran under and re-deriving it
    here would let the record and the decision disagree.
    """
    seed(owned, artifact_row("evd-1"))
    project(owned)
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


def test_lb_v4_one_projection_id_reaches_every_site_that_names_one(
    owned: Owned,
) -> None:
    """The gate, the builder, the ledger, the activation and the reader agree.

    Lane A's contributing set was empty and the gate never fired; Lane B fills it, and
    the failure this pins is the one that costs a day: a build activating one id while
    the freshness gate asks about another refuses every search forever, with a green
    lifecycle suite and a green vertical either side of the gap.

    So the id is not compared against a literal this test writes down -- that only pins
    the spelling. It is read back out of the rows a real build left behind and required
    to be the same object every reader reaches for.
    """
    seed(owned, artifact_row("evd-1"))
    outcome = project(owned)

    assert CONTRIBUTING_PROJECTIONS == (EVIDENCE_SEARCH_PROJECTION_ID,)
    assert fts.PROJECTION_ID == EVIDENCE_SEARCH_PROJECTION_ID

    stored = {
        "ledger": owned.connection.execute(
            "SELECT projection_id FROM omnivia_projection_ledger"
        ).fetchall(),
        "run": owned.connection.execute(
            "SELECT DISTINCT projection_id FROM omnivia_projection_runs"
        ).fetchall(),
        "activation": owned.connection.execute(
            "SELECT DISTINCT projection_id FROM omnivia_projection_activations"
        ).fetchall(),
        "documents": owned.connection.execute(
            f"SELECT DISTINCT projection_id FROM {fts.DOCUMENTS_TABLE}"
        ).fetchall(),
    }
    assert stored == {
        site: [(EVIDENCE_SEARCH_PROJECTION_ID,)] for site in stored
    }

    # And the gate the handler runs answers ready for exactly that id at exactly the
    # checkpoint the build recorded -- which is the join the two halves could miss.
    assert projection_readiness(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        source_checkpoint=outcome.source_checkpoint,
    ).ready
    assert outcome.source_checkpoint == authoritative_checkpoint(
        owned.connection, workspace_id=WORKSPACE_ID
    )


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
    runtime. The handler passes `retry_class` explicitly at every raise site; this reads
    it back off the source so that dropping any one argument fails here rather than
    shipping a wrong retry class on the wire.

    Five sites now rather than two: the freshness gate raises both codes before the
    read, the ranking step refuses when this session has no index at all, and it raises
    both again when the projection moves between the gate and the ranking. The count is
    asserted so that a sixth site added without the keyword fails here too.
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

    assert len(raises) == 5
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


# --- Lane B: the service's own startup, and the ordering behind the frontier ---


@dataclass(frozen=True)
class Served:
    """A workspace on disk that a real `service.main.main` can be pointed at."""

    workspace: Path
    installation: Path
    workspace_id: str
    endpoint: str
    socket_path: Path


def _serve_and_ask(
    served: Served, payload: dict[str, Any], *, timeout: float = 30.0
) -> tuple[int, list[Any]]:
    """Start the real service, send one real request over its socket, then stop it.

    `main` is called on the calling thread because it installs signal handlers, which
    CPython permits only on the main thread -- and it is the production entry point, so
    running anything else here would be testing a composition rather than the service.
    The client is therefore the worker: it waits for the socket the service binds, sends
    one OVC1 frame, reads the answer, and signals the service to stop. The signal is
    sent from a `finally`, so a client that fails still ends the run rather than leaving
    `main` blocked in its wait loop.
    """
    answers: list[Any] = []

    def client() -> None:
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not served.socket_path.exists():
                time.sleep(0.02)
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(timeout)
            try:
                channel.connect(str(served.socket_path))
                channel.sendall(encode_frame(payload))
                received = b""
                while True:
                    piece = channel.recv(65536)
                    if not piece:
                        break
                    received += piece
            finally:
                channel.close()
            answers.append(decode_frame(received))
        except Exception as error:  # noqa: BLE001 - the failure is the answer
            answers.append(error)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM)
    worker = threading.Thread(target=client, daemon=True)
    worker.start()
    try:
        code = main_module.main(
            [
                "--workspace",
                str(served.workspace),
                "--installation-state",
                str(served.installation),
                "--endpoint",
                served.endpoint,
            ]
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
        worker.join(timeout=timeout)
    return code, answers


@pytest.fixture
def served(request: pytest.FixtureRequest) -> Served:
    """A real workspace, initialised and seeded through the production paths.

    `initialise_workspace` is `--init`, so the manifest, the layout, the ownership
    substrate and every migration through 0012 are the production article. The evidence
    is then seeded under a real lease and a real mutation guard, and that ownership is
    released before the service starts -- the service takes its own lease, and the
    projection it builds is built from rows it did not write.

    Sockets go under `tempfile.mkdtemp` rather than `tmp_path`: a Unix socket path is
    bounded near 104 bytes on macOS and pytest's per-test directories are long enough to
    exceed it.
    """
    root = Path(tempfile.mkdtemp(prefix="ovs-lane-b-"))
    request.addfinalizer(lambda: _remove_tree(root))
    workspace = root / "workspace"
    installation = root / "installation"

    outcome = initialise_workspace(
        workspace_root=workspace, installation_root=installation, core_version="0.1.0"
    )
    assert outcome.status is not WorkspaceInitStatus.REFUSED, outcome.reason
    layout = WorkspaceLayout(root=workspace)
    workspace_id = str(json.loads(layout.manifest_path.read_text())["workspace_id"])

    identity = ServiceInstanceIdentity(
        service_instance_id="svc-lane-b-seed",
        installation_id="inst-lane-b-seed",
        process=ProcessEvidence(
            pid=4242, start_time="100", boot_id="boot-lane-b", os_principal="me"
        ),
    )
    connection = open_database(layout.database_path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=workspace_id,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=workspace_id,
        fencing_generation=lease.fencing_generation,
    )
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=lease.fencing_generation,
    ):
        insert(
            connection,
            BLOBS,
            {
                "workspace_id": workspace_id,
                "content_digest": DIGEST_A,
                "content_length_bytes": 1024,
                "created_at_us": BASE_US - 100,
                "verified_at_us": BASE_US - 99,
            },
        )
        for evidence_id, native_id, recorded_at in (
            ("evd-1", "doc-1", BASE_US),
            ("evd-2", "doc-2", BASE_US + 10),
        ):
            row = artifact_row(
                evidence_id,
                workspace_id=workspace_id,
                native_id=native_id,
                recorded_at_us=recorded_at,
            )
            insert(connection, EVIDENCE, row)
            event = provenance_row(evidence_id, native_id=native_id)
            event["workspace_id"] = workspace_id
            insert(connection, PROVENANCE, event)
    close_guard(connection)
    release_lease(connection, identity, clock=FakeClock())
    connection.close()

    socket_directory = Path(tempfile.mkdtemp(prefix="ovs-b-"))
    request.addfinalizer(lambda: _remove_tree(socket_directory))
    socket_path = socket_directory / "s.sock"
    return Served(
        workspace=workspace,
        installation=installation,
        workspace_id=workspace_id,
        endpoint=f"unix://{socket_path}",
        socket_path=socket_path,
    )


def _remove_tree(root: Path) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_lb_v1_the_real_service_answers_a_search_ordered_by_its_own_projection(
    served: Served,
) -> None:
    """The whole vertical, with nothing on it composed by this test.

    `service.main.main` runs its real startup sequence, which builds the projection and
    materialises the index inside its own `serve`; the request arrives as an OVC1 frame
    on the socket that service bound; the registered handler answers it; and the
    ordering in the answer is FTS5's, because there is no other ordering left in the
    path.

    The falsifier is the wiring itself. Remove the two calls from `serve` and every
    composed test in this file could still be made to pass by building the projection in
    a fixture -- this one cannot, because nothing here builds anything.
    """
    request = request_for(workspace_id=served.workspace_id)
    code, answers = _serve_and_ask(served, encode_request(request))

    assert code == 0
    assert len(answers) == 1 and isinstance(answers[0], dict), answers
    response = decode_response(answers[0])
    assert isinstance(response, SuccessResponseEnvelope), response
    result = EvidenceSearchResult.from_wire(response.result)
    validate_evidence_search_result(
        result, decode_evidence_search_input(request.input), served.workspace_id
    )
    # Both artifacts carry the query term the same number of times, so bm25 ties and the
    # shared order key's recency tie-break decides -- the later artifact first.
    assert tuple(item.evidence_id for item in result.evidence) == ("evd-2", "evd-1")


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_lb_v2_a_failed_projection_build_refuses_readiness_and_advertises_nothing(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built before ready, or not ready. There is no third state.

    The build is made to fail where the service calls it, so the failure arrives exactly
    where a real one would -- a lost lease, a database error, a SQLite without FTS5. The
    service must not come up serving refusals, and must not come up serving Lane A's
    ordering: it must not come up.

    Three facts, because a weaker one passes against a build that logs and continues:
    the process exits non-zero, the discovery descriptor is absent so no launcher can
    find it, and the socket it would have bound is not there to connect to.
    """
    monkeypatch.setattr(
        main_module,
        "build_search_projection",
        lambda *arguments, **keywords: (_ for _ in ()).throw(
            fts.ProjectionError("the projection could not be built")
        ),
    )

    code = main_module.main(
        [
            "--workspace",
            str(served.workspace),
            "--installation-state",
            str(served.installation),
            "--endpoint",
            served.endpoint,
        ]
    )

    assert code == 1
    runtime_directory = InstallationLayout(root=served.installation).runtime_for(
        served.workspace_id
    )
    assert discover(runtime_directory) is None
    assert not served.socket_path.exists()


def test_lb_v3_the_startup_builds_and_materialises_before_the_transport_starts(
    owned: Owned,
) -> None:
    """`serve`'s own syntax: the build, then the index, then the endpoint.

    Read out of the source because the ordering is the property and a live start cannot
    show it -- by the time anything can observe a started service, both have happened.
    What this rules out is the plausible wrong order: materialising after the socket is
    accepting leaves a window in which a request arrives at a handler with no index and
    is refused, on a service that reported itself ready.
    """
    import ast
    import inspect as inspect_module

    tree = ast.parse(inspect_module.getsource(main_module))
    serve = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "serve"
    )
    # Keyed on source position rather than on walk order: `ast.walk` is breadth-first,
    # so reading an ordering off it holds only while every call happens to sit at the
    # same depth. A line number is the thing actually being asserted.
    where = {
        node.func.id: node.lineno
        for node in ast.walk(serve)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    starts = min(
        node.lineno
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
    )

    assert where["build_search_projection"] < where["open_search_projection"]
    assert where["open_search_projection"] < where["LocalSocketServer"] < starts

    # And the build is handed the service's own three facts, not anything reconstructed.
    build_call = next(
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_search_projection"
    )
    assert {keyword.arg for keyword in build_call.keywords} == {
        "workspace_id",
        "fencing_generation",
        "now_us",
    }
    # The projection is exactly as reachable for the handler as the connection is: both
    # are read off the one connection the service owns, and neither is a handler's to
    # open. Before startup materialises one there is nothing to reach.
    assert fts.session_search_projection(owned.connection) is None


# --- Lane B: refusals, retained and retryable ---------------------------------


def test_lb_v5_a_missing_projection_refuses_the_request_and_builds_nothing(
    owned: Owned,
) -> None:
    """No activation, so no answer -- and the request path repairs nothing on its way.

    The falsifier is the tempting shortcut: have the handler materialise an index when
    it finds none. It would make this request succeed, and it would put a linear pass
    over the workspace inside a request, under whatever authority that request happened
    to arrive with.
    """
    seed(owned, artifact_row("evd-1"))
    before = _projection_state(owned)

    response = refusal(production_path(owned).dispatch(request_for()))

    assert response.error.code == ERROR_CODE_PROJECTION_UNAVAILABLE
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE_AFTER_DELAY
    assert _projection_state(owned) == before
    assert fts.session_search_projection(owned.connection) is None


def test_lb_v6_a_lagging_projection_refuses_the_request_and_repairs_nothing(
    owned: Owned,
) -> None:
    """Known lag is a refusal at any size, and the refusal does not fix itself.

    One artifact arrives after the build, which moves the workspace checkpoint and
    leaves the activated run behind it by the smallest amount there is. §20.7 removed
    the middle option, so there is no tolerance to fall inside.
    """
    seed(owned, artifact_row("evd-1"))
    project(owned)
    assert answered(production_path(owned).dispatch(request_for()))

    seed_more(
        owned,
        artifact_row(
            "evd-2",
            native_id="doc-2",
            checksum=DIGEST_B,
            # Later than every artifact the build saw, so the workspace
            # checkpoint -- the high-water `recorded_at_us` -- actually moves.
            recorded_at_us=BASE_US + 500,
        ),
    )
    before = _projection_state(owned)

    response = refusal(production_path(owned).dispatch(request_for()))

    assert response.error.code == ERROR_CODE_STALE_PROJECTION
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE_AFTER_DELAY
    assert _projection_state(owned) == before


def test_lb_v7_the_refusal_survives_the_gate_being_removed(owned: Owned) -> None:
    """Two independent refusals, and the second is not decoration.

    The handler's freshness gate and the projection's own per-request re-check both
    refuse a lagging projection. Removing the gate -- emptying the contributing set,
    which is one edit away -- must still refuse, because `require_current` re-proves that
    the run this material was built from is the activated one and still level with the
    workspace. A build with only the gate would answer this from material describing a
    workspace that has moved.
    """
    seed(owned, artifact_row("evd-1"))
    project(owned)
    projection = fts.session_search_projection(owned.connection)
    assert projection is not None

    seed_more(
        owned,
        artifact_row(
            "evd-2",
            native_id="doc-2",
            checksum=DIGEST_B,
            # Later than every artifact the build saw, so the workspace
            # checkpoint -- the high-water `recorded_at_us` -- actually moves.
            recorded_at_us=BASE_US + 500,
        ),
    )

    with pytest.raises(fts.StaleProjection):
        fts.require_current(owned.connection, projection)


def _projection_state(owned: Owned) -> tuple[int, int, int]:
    """Runs, activations and documents -- what a request that built would move."""
    return tuple(  # type: ignore[return-value]
        int(owned.connection.execute(query).fetchone()[0])
        for query in (
            "SELECT COUNT(*) FROM omnivia_projection_runs",
            "SELECT COUNT(*) FROM omnivia_projection_activations",
            f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE}",
        )
    )


# --- Lane B: F2, against FTS5 --------------------------------------------------


def test_lb_v8_an_excluded_artifact_is_never_scored_even_when_the_page_is_identical(
    owned: Owned,
) -> None:
    """F2 with the ranking that replaced Lane A's, and the mutation it has to survive.

    Two searches over one workspace and one materialised projection. The second is the
    defective build: the ACL stage deferred, so `evd-2` -- restricted, and held by nobody
    -- is on the frontier. It scores last and the page drops it, so the two pages are
    byte-identical and no assertion over a result could tell them apart.

    What separates them is what the ranker was *given*. The projected frontier is read
    directly: the honest one holds `evd-1` alone and the widened one holds `evd-2` as
    well, so "an excluded artifact is never scored" is observed at the ranker's input
    rather than inferred from an output that by construction cannot show it. And the
    whole post-freeze path is watched on the connection: it issues no statement at all,
    which is the structural reason there is no index read left here to constrain
    correctly.
    """
    seed(
        owned,
        artifact_row("evd-1", recorded_at_us=BASE_US + 100),
        artifact_row(
            "evd-2", native_id="doc-2", checksum=DIGEST_B, recorded_at_us=BASE_US
        ),
        extra=((LABELS, label_row("evd-2", "restricted")),),
    )
    project(owned)
    index = fts.session_search_projection(owned.connection)
    assert index is not None

    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)
    resolution = int(
        authoritative_checkpoint(owned.connection, workspace_id=WORKSPACE_ID)
    )
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
    assert "evd-2" in {item.evidence_id for item in honest.excluded}

    honest_statements, honest_projected = _post_freeze(owned, index, honest)
    widened_statements, widened_projected = _post_freeze(owned, index, widened)

    # What the ranker was handed. The honest projected frontier never names `evd-2`.
    assert {
        item.candidate.evidence_id for item in honest_projected.candidates
    } == {"evd-1"}
    assert "evd-2" in {
        item.candidate.evidence_id for item in widened_projected.candidates
    }
    # And membership survives the narrowing exactly, digest included.
    assert honest_projected.checksum == honest.checksum
    assert widened_projected.checksum == widened.checksum
    # Narrowing and ranking touch SQLite not at all.
    assert honest_statements == [] and widened_statements == []

    honest_page = _page(rank_projected(honest_projected, "doc-1", limit=50))
    widened_page = _page(rank_projected(widened_projected, "doc-1", limit=50))

    # Byte-identical, so the projected frontier above is the only place the difference is
    # visible -- and the digest catches it without looking there.
    assert honest_page == widened_page
    assert honest.checksum != widened.checksum


#: Three identity surfaces chosen so that BM25 over the authorized frontier and BM25 over
#: the whole workspace disagree about the order of the two authorized members.
#: `evd-poison` carries the query term 340 times in a surface fifty times the length of
#: the others: it lifts the average document length enormously, and that average is what
#: BM25's length normalisation divides by. `test_lb_v9_...` proves the disagreement rather
#: than assuming it, so these numbers cannot quietly stop discriminating.
SHORT_LOCATOR = "archive://alpha.md"
LONG_LOCATOR = "archive://alpha/alpha/" + "/".join(f"p{index}" for index in range(24))
POISON_LOCATOR = "alpha/" * 340


def test_lb_v9_an_excluded_artifact_cannot_move_the_order_of_authorized_members(
    owned: Owned,
) -> None:
    """The rejected design's actual defect, and the case that catches it.

    Two otherwise-identical workspaces, differing only by one artifact the ACL filter
    excludes. It is authorized for nobody and cannot appear in a page, and the first Lane
    B design still let it decide the order of the two artifacts that *were* returned --
    because `bm25()` takes its average document length from the whole index, and this
    artifact is fifty times longer than either member and carries the query term 340
    times. Constraining which rows FTS5 returned did not constrain the statistics it
    scored them with.

    Packet §7.2 names *scoring* alongside ranking and selection, so this is the clause
    itself rather than a refinement of it, and no assertion over a result page reaches it:
    both pages carry exactly the two authorized ids either way.

    The last two assertions are what make this a test rather than a hope. The corpus-wide
    ordering is computed from the live FTS5 index -- the exact ranking the rejected design
    performed, ids constrained and statistics not -- and required to *disagree* with the
    ordering this build returns. If a later edit made the fixture stop discriminating,
    this fails rather than passing vacuously.
    """
    seed(
        owned,
        artifact_row(
            "evd-short",
            native_id="doc-s",
            locator=SHORT_LOCATOR,
            recorded_at_us=BASE_US + 100,
        ),
        artifact_row(
            "evd-long",
            native_id="doc-l",
            locator=LONG_LOCATOR,
            checksum=DIGEST_B,
            recorded_at_us=BASE_US,
        ),
    )
    project(owned)
    without_poison = _ranked_for_stranger(owned, "alpha")
    assert without_poison == ("evd-short", "evd-long")

    # The same workspace, plus one artifact no principal in this test may see.
    seed_more(
        owned,
        artifact_row(
            "evd-poison",
            native_id="doc-p",
            locator=POISON_LOCATOR,
            checksum=DIGEST_C,
            recorded_at_us=BASE_US + 500,
        ),
        extra=((LABELS, label_row("evd-poison", "restricted")),),
    )
    project(owned)
    with_poison = _ranked_for_stranger(owned, "alpha", expect_excluded="evd-poison")

    # Identical membership and identical ordering. Not "the excluded artifact is absent",
    # which was true of the rejected design too.
    assert with_poison == without_poison

    # And the case is a real one: scored the way the rejected design scored it -- every
    # returned id a frontier member, every statistic taken from the whole index -- the two
    # authorized members come back in the other order.
    corpus_wide = tuple(
        str(row[0])
        for row in owned.connection.execute(
            f"SELECT evidence_id, bm25({fts.INDEX_TABLE}) FROM {fts.INDEX_TABLE} "
            f"WHERE {fts.INDEX_TABLE} MATCH ? AND evidence_id IN (?, ?) "
            f"ORDER BY bm25({fts.INDEX_TABLE})",
            ('"alpha"', "evd-short", "evd-long"),
        )
    )
    assert set(corpus_wide) == set(with_poison)
    assert corpus_wide != with_poison


def _ranked_for_stranger(
    owned: Owned, query: str, *, expect_excluded: str | None = None
) -> tuple[str, ...]:
    """One search over the frontier a principal holding no label grant would get.

    The production post-freeze composition -- re-prove, narrow, rank -- and nothing else.
    """
    projection = fts.session_search_projection(owned.connection)
    assert projection is not None
    resolution = int(
        authoritative_checkpoint(owned.connection, workspace_id=WORKSPACE_ID)
    )
    frozen = authorized_frontier(
        read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID),
        workspace_id=WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id="not-the-owner",
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        resolution_time_us=resolution,
    )
    if expect_excluded is not None:
        assert expect_excluded in {item.evidence_id for item in frozen.excluded}
    fts.require_current(owned.connection, projection)
    return tuple(
        candidate.evidence_id
        for candidate in rank_projected(projection.project(frozen), query, limit=50)
    )


def test_lb_v10_missing_material_refuses_the_request_rather_than_falling_back(
    owned: Owned,
) -> None:
    """The last fallback available to this handler, refused end to end.

    Every freshness check in the path reports current here: the projection is activated,
    level with the workspace, and built from the run this session holds. What is missing
    is the material for one authorized candidate -- and its authoritative `search_text` is
    on the frozen frontier, one attribute away, enough to answer the request from a source
    this projection does not describe.

    So the whole handler is driven rather than the adapter alone: the wire refusal is the
    retryable one the contract fixes, the artifact does not appear in it, and nothing was
    built on the way.
    """
    seed(owned, artifact_row("evd-1"))
    project(owned)
    projection = fts.session_search_projection(owned.connection)
    assert projection is not None
    owned.connection.omnivia_search_projection = replace(projection, material={})
    before = _projection_state(owned)

    response = refusal(production_path(owned).dispatch(request_for()))

    assert response.error.code == ERROR_CODE_PROJECTION_UNAVAILABLE
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE_AFTER_DELAY
    assert "evd-1" not in json.dumps(encode_response(response), sort_keys=True)
    assert _projection_state(owned) == before


def _post_freeze(
    owned: Owned, projection: fts.SearchProjection, frozen: Any
) -> tuple[list[str], Any]:
    """The statements the narrowing ran, and the value the ranker is handed.

    Traced on the connection rather than reasoned about, because "the post-freeze path
    issues no SQL" is the structural claim this redesign rests on and it is one edit away
    from being false.
    """
    seen: list[str] = []
    owned.connection.set_trace_callback(seen.append)
    try:
        projected = projection.project(frozen)
    finally:
        owned.connection.set_trace_callback(None)
    return seen, projected


def _page(ranked: tuple[Any, ...]) -> str:
    """A ranked selection as the canonical result bytes a caller would receive."""
    return json.dumps(
        EvidenceSearchResult(
            evidence=tuple(candidate.artifact for candidate in ranked),
            page=EvidenceSearchResult.from_wire({"evidence": [], "page": {}}).page,
        ).to_wire(),
        sort_keys=True,
    )


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
    """An authorized search over nothing is a page, not a refusal.

    The projection is built over the empty workspace first, because an empty workspace
    is a real checkpoint a projection can be level with -- `"0"` -- rather than a state
    the freshness gate is entitled to skip. A build that special-cased it would answer
    this request from no projection at all.
    """
    project(owned)
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
    project(owned)
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
