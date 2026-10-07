"""Storage-bound plan admission (SPEC-CORE-DATA-001 WP07): the DatasetState binding adapter.

The database is a real migration-0062 workspace: the migrator runs through the accepted 0062
catalogue only, rows are written the way a mutation settles them (audit event, then
`record_observation`, inside one fence), and the context comes from the production
authorizer. The resolver is a real `AnalysisUseAuthorityResolver` shape answering with the
real snapshot type, and the shared evaluator runs for real.

The properties proved: the context is validated before any storage access and supplies the
only workspace; a hostile dataset id is refused before SQLite and without its equality hook;
the current state is read exactly once and the exact record storage returned reaches the
accepted checkpoint unchanged; every binding failure is the one fixed refusal with no storage
text or cause behind it and never reaches the resolver; deny and warning outcomes return
normally. Monkeypatching replaces a name only where real storage cannot produce the shape
(a wrong-type or mismatched record, a failing read) or to observe what the checkpoint got.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.analysis import dataset_state_binding as binding_module
from omnivia_core_runtime.analysis.authority import (
    REFUSE_ANALYSIS_USE_AUTHORITY,
    AnalysisUseAuthorityQuery,
    AnalysisUseAuthorityRefused,
    AnalysisUseAuthoritySnapshot,
    analysis_use_authority_subject_from_context,
)
from omnivia_core_runtime.analysis.dataset_state_binding import (
    evaluate_storage_bound_plan_admission_checkpoint,
)
from omnivia_core_runtime.analysis.result_use_checkpoints import (
    AnalysisResultUseCheckpoint,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationContext
from omnivia_core_runtime.storage import dataset_state
from omnivia_core_runtime.storage.connection import split_sql_statements
from omnivia_core_runtime.storage.decisions import content_digest
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    to_canonical_json,
)
from omnivia_core.contracts.v1.semantics_result_use import (
    OUTCOME_ALLOW,
    OUTCOME_ALLOW_WITH_WARNING,
    OUTCOME_DENY,
    USE_CURRENT_PUBLICATION,
    USE_EXPLORATION,
)

#: The raw text a failing read or resolver would carry. Never printed or compared.
SENTINEL = "do not echo: 3f9a"

MIGRATION_VERSION = 62
TABLE = "omnivia_analysis_dataset_state_observations"
CURRENT_VIEW = "omnivia_analysis_dataset_state_current"

WORKSPACE = m2.WORKSPACE_ID
OTHER_WORKSPACE = "ws-other-0002"
BASE_US = m2.BASE_US
OPERATION = "memory.get"
PRINCIPAL = "principal-1"
INSTALLATION = "inst-0001"
PURPOSE = "operations.read"
SCOPE = "memory:read"
SUPPORTED = (CapabilityRef(id="memory.read", version="1.4"),)

DATASET_ID = "dataset-invoices"
OTHER_DATASET_ID = "dataset-orders"
SUBJECT_DIGEST = "subject-1"
SCOPE_DIGEST = "sha256:" + "5" * 64
MANIFEST_DIGEST = "sha256:" + "6" * 64
POLICY_DIGEST = "sha256:" + "7" * 64
EPOCH_CURRENT = "epoch-current-2"
INSTANT = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
REFUSAL = REFUSE_ANALYSIS_USE_AUTHORITY

#: Every hostile equality that ran, across one test.
EQUALITY_CALLS: list[str] = []


class _Spoof(str):
    """A `str` whose equality answers True and records that it was asked."""

    def __eq__(self, other: object) -> bool:
        EQUALITY_CALLS.append("eq")
        return True

    def __ne__(self, other: object) -> bool:
        EQUALITY_CALLS.append("ne")
        return False

    def __hash__(self) -> int:
        return str.__hash__(self)


class _Ctx(OperationContext):
    """An `OperationContext` subclass, to show the exact-type gate."""


class _RecordSubclass(dataset_state.DatasetStateRecord):
    """A record subclass with valid values, to show the exact-type gate."""


class _ObservationSubclass(dataset_state.DatasetStateObservation):
    """An observation subclass with valid values, to show the exact-type gate."""


@pytest.fixture(autouse=True)
def _no_hostile_equality_ran() -> Iterator[None]:
    EQUALITY_CALLS.clear()
    yield
    assert EQUALITY_CALLS == []


# --- harness: a real 0062 workspace, a real context, a real resolver shape -----------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    """A workspace migrated through 0062 only, under current write authority."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        m2.bootstrap_and_migrate(path)
        holder = m2.take_ownership(path)
        try:
            assert max(applied_migrations(holder.connection)) == MIGRATION_VERSION
            yield holder
        finally:
            holder.connection.close()


def _coverage() -> dict[str, Any]:
    return {
        "scope_digest": SCOPE_DIGEST,
        "accepted_rows": 12,
        "rejected_rows": 0,
        "conflicting_rows": 0,
        "deduplicated_rows": 0,
        "expected_source_rows": 12,
        "proof_kind": "complete_enumeration",
        "proof_refs": ["listing-2026-10-04"],
    }


def _source() -> dict[str, Any]:
    return {
        "source_ref": {"id": "source-erp", "revision_id": "source-erp-r4"},
        "source_incarnation": "source-incarnation-1",
        "observation_interval": {
            "start_inclusive_at_us": BASE_US - 60_000_000,
            "end_exclusive_at_us": BASE_US,
        },
        "source_cutoff_at_us": BASE_US,
        "verification_at_us": BASE_US,
        "evidence_kind": "snapshot",
        "snapshot_token_ref": "snapshot-token-1",
        "applied_checkpoint_ref": None,
        "scope_digest": SCOPE_DIGEST,
        "evidence_refs": ["source-evidence-1"],
    }


def _observation(**overrides: Any) -> dataset_state.DatasetStateObservation:
    fields_: dict[str, Any] = {
        "dataset_id": DATASET_ID,
        "dataset_revision": "dataset-invoices-r1",
        "dataset_incarnation": "incarnation-1",
        "initial_readiness": "ready",
        "completeness": "complete",
        "continuity": "verified",
        "operational_health": "healthy",
        "schema_compatibility": "compatible",
        "content_observation": "nonempty",
        "evidence_availability": "available",
        "observed_authority_epoch": "authority-epoch-7",
        "scope_digest": SCOPE_DIGEST,
        "coverage": _coverage(),
        "source_observation": _source(),
        "verified_at_us": BASE_US,
        "freshness_deadline_at_us": BASE_US + 3_600_000_000,
        "manifest_id": "manifest-invoices",
        "manifest_revision": "manifest-invoices-r3",
        "manifest_digest": MANIFEST_DIGEST,
    }
    fields_.update(overrides)
    return dataset_state.DatasetStateObservation(**fields_)


def _store(holder: m2.Owned, *, at_us: int, **overrides: Any) -> int:
    """Settle one observation as a mutation does: its audit, then the write, one fence."""
    ref = f"aud-bind-{at_us}"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE,
        fencing_generation=holder.generation,
    ) as fenced:
        fenced.execute(
            "INSERT INTO omnivia_application_audit_events (audit_ref, workspace_id, "
            "principal_id, operation, purpose, request_id, correlation_id, trace_id, "
            "granted_authority_json, outcome_class, error_code, recorded_at_us) "
            "VALUES (?, ?, 'core-service', 'test.dataset_state', 'p', ?, ?, ?, '{}', "
            "'succeeded', NULL, ?)",
            (ref, WORKSPACE, ref, ref, ref, at_us),
        )
        settlement = MutationSettlementContext(
            audit_ref=ref, claim_id=f"clm-{ref}", outcome_id=f"out-{ref}", settled_at_us=at_us
        )
        return dataset_state.record_observation(
            fenced,
            settlement,
            workspace_id=WORKSPACE,
            observation=_observation(**overrides),
        )


def _context(workspace_id: str = WORKSPACE) -> OperationContext:
    """A context the production authorizer produced, with its authority pass-through."""
    request = RequestEnvelope(
        operation=OPERATION,
        metadata=RequestMetadata(
            request_id="req-1",
            correlation_id="corr-1",
            trace_id="trace-1",
            api_version=CONTRACT_VERSION,
            client=ClientIdentity(id="test-client", version="0.1.0"),
            scopes=(SCOPE,),
            purpose=PURPOSE,
            required_capabilities=(
                CapabilityRequirement(id="memory.read", minimum_version="1.0", required=True),
            ),
            workspace_id=workspace_id,
        ),
        input={},
    )
    authorized = authorize_application_request(
        request,
        session=AuthenticatedSession(
            principal_id=PRINCIPAL,
            roles=frozenset({"reader"}),
            installations=frozenset({INSTALLATION}),
            workspaces=frozenset({workspace_id}),
            operations=frozenset({OPERATION}),
            scopes=frozenset({SCOPE}),
            purposes=frozenset({PURPOSE}),
            capabilities=SUPPORTED,
        ),
        binding=ServiceBinding(installation_id=INSTALLATION),
        supported_capabilities=SUPPORTED,
    )
    return OperationContext(
        request=request,
        principal=authorized.principal_id,
        workspace_id=workspace_id,
        granted_operations=frozenset({OPERATION}),
        authority=authorized.authority,
        scopes=authorized.scopes,
        purpose=authorized.purpose,
        authorization=authorized,
    )


class _Resolver:
    """Counts its calls, keeps every query, and answers with the real snapshot type."""

    def __init__(self, **overrides: Any) -> None:
        self.queries: list[AnalysisUseAuthorityQuery] = []
        self._overrides = overrides

    @property
    def calls(self) -> int:
        return len(self.queries)

    def resolve(self, query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        self.queries.append(query)
        fields_: dict[str, Any] = {
            "query": query,
            "authority_epoch": EPOCH_CURRENT,
            "evidence_access_permitted": True,
            "freshness_ok": True,
            "policy_permits_partial_or_stale": False,
            "policy_ref": "policy-1",
            "policy_digest": POLICY_DIGEST,
        }
        fields_.update(self._overrides)
        return AnalysisUseAuthoritySnapshot(**fields_)


def _run(
    context: Any,
    connection: Any,
    resolver: _Resolver,
    *,
    dataset_id: Any = DATASET_ID,
    use_class: str = USE_CURRENT_PUBLICATION,
) -> AnalysisResultUseCheckpoint:
    return evaluate_storage_bound_plan_admission_checkpoint(
        context,
        connection,
        dataset_id=dataset_id,
        subject_digest=SUBJECT_DIGEST,
        resolved_use_class=use_class,
        evaluation_instant=INSTANT,
        resolver=resolver,
    )


def _assert_plain_refusal(error: AnalysisUseAuthorityRefused) -> None:
    """The exact type, the fixed reason, and nothing chained or quoted."""
    assert type(error) is AnalysisUseAuthorityRefused
    assert error.reason == REFUSAL
    assert error.args == (REFUSAL,)
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "\n".join((str(error), repr(error), repr(error.args)))
    assert SENTINEL not in rendered


def _refused(
    context: Any,
    connection: Any,
    resolver: _Resolver,
    *,
    dataset_id: Any = DATASET_ID,
) -> None:
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _run(context, connection, resolver, dataset_id=dataset_id)
    _assert_plain_refusal(raised.value)
    assert resolver.calls == 0


def _trace(connection: sqlite3.Connection) -> list[str]:
    """Every statement the connection runs from here on."""
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    return statements


class _Seen:
    """What the adapter handed to storage and to the accepted checkpoint."""

    def __init__(self) -> None:
        self.reads: list[dict[str, Any]] = []
        self.read_records: list[Any] = []
        self.checkpoints: list[tuple[tuple[Any, ...], dict[str, Any]]] = []


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> _Seen:
    """Observe the one read and the one delegation, calling through to both."""
    record = _Seen()
    real_read = binding_module.read_current_state
    real_checkpoint = binding_module.evaluate_plan_admission_checkpoint

    def read(connection: Any, **keywords: Any) -> Any:
        record.reads.append({"connection": connection, **keywords})
        result = real_read(connection, **keywords)
        record.read_records.append(result)
        return result

    def checkpoint(*arguments: Any, **keywords: Any) -> Any:
        record.checkpoints.append((arguments, keywords))
        return real_checkpoint(*arguments, **keywords)

    monkeypatch.setattr(binding_module, "read_current_state", read)
    monkeypatch.setattr(binding_module, "evaluate_plan_admission_checkpoint", checkpoint)
    return record


def _stored_record(holder: m2.Owned, dataset_id: str = DATASET_ID) -> Any:
    return dataset_state.read_current_state(
        holder.connection, workspace_id=WORKSPACE, dataset_id=dataset_id
    )


# --- the happy path: a real stored current row ------------------------------------------


def test_a_real_stored_current_row_is_evaluated_through_the_accepted_checkpoint(
    owned: m2.Owned, seen: _Seen
) -> None:
    assert _store(owned, at_us=BASE_US + 1) == 1
    # The repository's normal governed connection, not an exact `sqlite3.Connection`.
    assert type(owned.connection) is not sqlite3.Connection
    context = _context()
    resolver = _Resolver()

    result = _run(context, owned.connection, resolver)

    assert type(result) is AnalysisResultUseCheckpoint
    assert result.checkpoint == "plan_admission"
    assert result.decision.outcome == OUTCOME_ALLOW
    assert resolver.calls == 1
    query = resolver.queries[0]
    assert result.authority.query is query
    assert query.subject == analysis_use_authority_subject_from_context(context)
    assert query.subject.workspace_id == WORKSPACE
    assert (query.dataset_id, query.state_generation) == (DATASET_ID, 1)
    assert query.subject_digest == SUBJECT_DIGEST
    assert query.use_class == USE_CURRENT_PUBLICATION
    assert query.evaluation_instant == INSTANT
    # The stored authority epoch is evidence only: the evaluator saw the resolver's.
    assert query.observed_authority_epoch == "authority-epoch-7"
    assert result.evaluation_input.authority_epoch == EPOCH_CURRENT


def test_the_exact_stored_record_reaches_the_checkpoint_and_resolver_unchanged(
    owned: m2.Owned, seen: _Seen
) -> None:
    _store(owned, at_us=BASE_US + 1, initial_readiness="catching_up")
    _store(owned, at_us=BASE_US + 2, dataset_revision="dataset-invoices-r2")
    context = _context()
    resolver = _Resolver()
    statements = _trace(owned.connection)

    result = _run(context, owned.connection, resolver)

    # One current-state read through the module's own reader, and no history read.
    assert len(seen.reads) == 1
    assert seen.reads[0] == {
        "connection": owned.connection,
        "workspace_id": WORKSPACE,
        "dataset_id": DATASET_ID,
    }
    selects = [text for text in statements if text.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert CURRENT_VIEW in selects[0] and TABLE not in selects[0]

    # The very object storage returned is the one the checkpoint received.
    assert len(seen.checkpoints) == 1
    arguments, keywords = seen.checkpoints[0]
    stored = seen.read_records[0]
    assert type(stored) is dataset_state.DatasetStateRecord
    assert keywords["dataset"] is stored
    assert arguments == (analysis_use_authority_subject_from_context(context),)
    assert keywords["subject_digest"] is SUBJECT_DIGEST
    assert keywords["resolved_use_class"] == USE_CURRENT_PUBLICATION
    assert keywords["evaluation_instant"] is INSTANT
    assert keywords["resolver"] is resolver
    assert set(keywords) == {
        "dataset",
        "subject_digest",
        "resolved_use_class",
        "evaluation_instant",
        "resolver",
    }

    # Its generation, revisions and evidence digests are the stored current row's.
    assert stored == _stored_record(owned)
    assert stored.state_generation == 2
    assert stored.observation.dataset_revision == "dataset-invoices-r2"
    query = result.authority.query
    assert query.state_generation == 2
    assert query.dataset_revision == "dataset-invoices-r2"
    assert query.dataset_incarnation == stored.observation.dataset_incarnation
    assert query.manifest_digest == MANIFEST_DIGEST
    assert query.coverage_digest == stored.coverage_digest == content_digest(
        to_canonical_json(_coverage())
    )
    assert query.source_observation_digest == stored.source_observation_digest
    assert query.recorded_at_us == stored.recorded_at_us == BASE_US + 2
    assert query.evaluation_instant == INSTANT


def test_the_highest_state_generation_is_the_one_evaluated(owned: m2.Owned) -> None:
    _store(owned, at_us=BASE_US + 1, initial_readiness="blocked", dataset_revision="r1")
    _store(owned, at_us=BASE_US + 2, initial_readiness="catching_up", dataset_revision="r2")
    _store(owned, at_us=BASE_US + 3, initial_readiness="ready", dataset_revision="r3")
    resolver = _Resolver()

    result = _run(_context(), owned.connection, resolver)

    assert result.authority.query.state_generation == 3
    assert result.authority.query.dataset_revision == "r3"
    assert result.authority.query.initial_readiness == "ready"

    # A newer generation supersedes it: the superseded `ready` row is never reused.
    _store(owned, at_us=BASE_US + 4, initial_readiness="blocked", dataset_revision="r4")
    blocked = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _run(_context(), owned.connection, blocked)
    _assert_plain_refusal(raised.value)
    assert blocked.queries[0].state_generation == 4


# --- isolation ---------------------------------------------------------------------------


def test_each_dataset_is_read_for_its_own_id_only(owned: m2.Owned) -> None:
    _store(owned, at_us=BASE_US + 1, dataset_revision="invoices-r1", dataset_incarnation="inc-a")
    _store(
        owned,
        at_us=BASE_US + 2,
        dataset_id=OTHER_DATASET_ID,
        dataset_revision="orders-r1",
        dataset_incarnation="inc-b",
    )
    _store(owned, at_us=BASE_US + 3, dataset_revision="invoices-r2", dataset_incarnation="inc-a")

    invoices = _run(_context(), owned.connection, _Resolver())
    orders = _run(_context(), owned.connection, _Resolver(), dataset_id=OTHER_DATASET_ID)

    assert (invoices.authority.query.dataset_id, invoices.authority.query.state_generation) == (
        DATASET_ID,
        2,
    )
    assert invoices.authority.query.dataset_revision == "invoices-r2"
    assert (orders.authority.query.dataset_id, orders.authority.query.state_generation) == (
        OTHER_DATASET_ID,
        1,
    )
    assert orders.authority.query.dataset_revision == "orders-r1"
    assert orders.authority.query.dataset_incarnation == "inc-b"
    # A dataset that was never observed has no current state to bind.
    _refused(_context(), owned.connection, _Resolver(), dataset_id="dataset-never-observed")


def test_the_workspace_comes_only_from_the_validated_context(owned: m2.Owned) -> None:
    _store(owned, at_us=BASE_US + 1)
    # The stored row is this workspace's. A context for another valid workspace asks for
    # that workspace's `dataset-invoices`, finds no row, and is refused.
    _refused(_context(OTHER_WORKSPACE), owned.connection, _Resolver())
    assert _run(_context(), owned.connection, _Resolver()).authority.query.subject.workspace_id == (
        WORKSPACE
    )


def test_an_unobserved_dataset_is_refused_without_reaching_the_resolver(owned: m2.Owned) -> None:
    _refused(_context(), owned.connection, _Resolver())


# --- the context and the dataset id are proven before storage ------------------------------


def _hostile_contexts() -> list[tuple[str, Callable[[], Any]]]:
    return [
        ("none", lambda: None),
        ("plain_object", lambda: object()),
        ("subclass", lambda: _Ctx(**{f.name: getattr(_context(), f.name) for f in _fields()})),
        (
            "workspace_disagrees_with_authorization",
            lambda: dataclasses.replace(_context(), workspace_id=OTHER_WORKSPACE),
        ),
        (
            "spoofed_workspace",
            lambda: dataclasses.replace(_context(), workspace_id=_Spoof(WORKSPACE)),
        ),
        ("legacy_authority_none", lambda: dataclasses.replace(_context(), authority=None)),
        ("legacy_scopes_none", lambda: dataclasses.replace(_context(), scopes=None)),
        ("legacy_authorization_none", lambda: dataclasses.replace(_context(), authorization=None)),
    ]


def _fields() -> tuple[dataclasses.Field[Any], ...]:
    return dataclasses.fields(OperationContext)


@pytest.mark.parametrize(
    "build",
    [pytest.param(build, id=label) for label, build in _hostile_contexts()],
)
def test_an_invalid_or_hostile_context_is_refused_before_storage(
    owned: m2.Owned, seen: _Seen, build: Callable[[], Any]
) -> None:
    _store(owned, at_us=BASE_US + 1)
    statements = _trace(owned.connection)
    resolver = _Resolver()

    _refused(build(), owned.connection, resolver)

    assert statements == []
    assert seen.reads == []
    assert seen.checkpoints == []


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param(_Spoof(DATASET_ID), id="str_subclass_with_equality_hook"),
        pytest.param(None, id="none"),
        pytest.param(7, id="int"),
        pytest.param(DATASET_ID.encode(), id="bytes"),
        pytest.param(["dataset-invoices"], id="list"),
        pytest.param("", id="empty"),
        pytest.param("dataset invoices", id="space"),
        pytest.param("dataset-invoices\x00", id="nul"),
        pytest.param("dataset-invoices\n", id="newline"),
        pytest.param("é-dataset", id="non_ascii"),
        pytest.param("d" * 4096, id="oversized"),
    ],
)
def test_a_hostile_or_noncanonical_dataset_id_is_refused_before_storage(
    owned: m2.Owned, seen: _Seen, hostile: Any
) -> None:
    _store(owned, at_us=BASE_US + 1)
    statements = _trace(owned.connection)
    resolver = _Resolver()

    _refused(_context(), owned.connection, resolver, dataset_id=hostile)

    assert statements == []
    assert seen.reads == []
    assert seen.checkpoints == []


# --- storage failures: one fixed refusal, no leaked cause -----------------------------------


def test_a_read_that_raises_is_refused_with_no_cause_or_context(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(owned, at_us=BASE_US + 1)
    calls: list[int] = []

    def failing(connection: Any, **keywords: Any) -> Any:
        calls.append(1)
        try:
            raise OSError(SENTINEL)
        except OSError as error:
            raise sqlite3.OperationalError(SENTINEL) from error

    monkeypatch.setattr(binding_module, "read_current_state", failing)
    resolver = _Resolver()

    _refused(_context(), owned.connection, resolver)

    assert calls == [1]


def test_a_real_closed_connection_is_refused_as_the_fixed_reason(owned: m2.Owned) -> None:
    _store(owned, at_us=BASE_US + 1)
    closed = sqlite3.connect(":memory:")
    closed.close()
    resolver = _Resolver()

    _refused(_context(), closed, resolver)


def _raw_row(**overrides: object) -> dict[str, object]:
    """One complete observation in column form, written past the module."""
    coverage = to_canonical_json(_coverage())
    source = to_canonical_json(_source())
    row: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "dataset_id": DATASET_ID,
        "state_generation": 1,
        "dataset_revision": "dataset-invoices-r1",
        "dataset_incarnation": "incarnation-1",
        "manifest_id": None,
        "manifest_revision": None,
        "manifest_digest": None,
        "initial_readiness": "ready",
        "completeness": "complete",
        "continuity": "verified",
        "operational_health": "healthy",
        "schema_compatibility": "compatible",
        "content_observation": "nonempty",
        "evidence_availability": "available",
        "observed_authority_epoch": "authority-epoch-7",
        "scope_digest": SCOPE_DIGEST,
        "coverage_json": coverage,
        "coverage_digest": content_digest(coverage),
        "source_observation_json": source,
        "source_observation_digest": content_digest(source),
        "freshness_deadline_at_us": None,
        "verified_at_us": BASE_US,
        "recorded_at_us": BASE_US,
        "audit_ref": "aud-bind-raw",
    }
    row.update(overrides)
    return row


def _corrupt_table(row: dict[str, object]) -> sqlite3.Connection:
    """0062's table and projection with their CHECKs and no guard trigger, so a row that
    storage's own guards would never admit is stored exactly as given. Only the corrupt-row
    refusals use it: the guarded path is the real fixture above."""
    connection = sqlite3.connect(":memory:")
    migration = next(item for item in load_migrations() if item.version == MIGRATION_VERSION)
    for statement in split_sql_statements(migration.sql)[:2]:
        connection.execute(statement)
    m2.insert(connection, TABLE, row)
    return connection


def _unsorted_coverage() -> str:
    """The coverage fields in reverse order: valid JSON, but not the canonical text."""
    return json.dumps(dict(reversed(list(_coverage().items()))), separators=(",", ":"))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"coverage_digest": "sha256:" + "0" * 64}, id="coverage_digest_mismatch"),
        pytest.param(
            {"source_observation_digest": "sha256:" + "0" * 64}, id="source_digest_mismatch"
        ),
        pytest.param(
            {
                "coverage_json": _unsorted_coverage(),
                "coverage_digest": content_digest(_unsorted_coverage()),
            },
            id="noncanonical_coverage_text",
        ),
    ],
)
def test_a_corrupt_stored_row_is_refused_with_only_the_fixed_reason(
    overrides: dict[str, object],
) -> None:
    connection = _corrupt_table(_raw_row(**overrides))
    try:
        # The control: storage's own reader refuses this row, with text of its own.
        with pytest.raises(dataset_state.DatasetStateInvalid) as storage_error:
            dataset_state.read_current_state(
                connection, workspace_id=WORKSPACE, dataset_id=DATASET_ID
            )
        resolver = _Resolver()

        with pytest.raises(AnalysisUseAuthorityRefused) as raised:
            _run(_context(), connection, resolver)

        _assert_plain_refusal(raised.value)
        assert str(storage_error.value) not in "\n".join((str(raised.value), repr(raised.value)))
        assert resolver.calls == 0
    finally:
        connection.close()


# --- impossible read results are refused before the resolver or evaluator --------------------


def _impossible_results(base: Any) -> list[tuple[str, Any]]:
    observation = base.observation
    return [
        ("wrong_type_object", object()),
        ("wrong_type_dict", {"workspace_id": WORKSPACE}),
        ("wrong_type_tuple", (base,)),
        (
            "record_subclass",
            _RecordSubclass(**{f.name: getattr(base, f.name) for f in _record_fields()}),
        ),
        ("other_workspace", dataclasses.replace(base, workspace_id=OTHER_WORKSPACE)),
        ("spoofed_workspace", dataclasses.replace(base, workspace_id=_Spoof(WORKSPACE))),
        ("non_string_workspace", dataclasses.replace(base, workspace_id=7)),
        (
            "other_dataset",
            dataclasses.replace(
                base, observation=dataclasses.replace(observation, dataset_id=OTHER_DATASET_ID)
            ),
        ),
        (
            "spoofed_dataset",
            dataclasses.replace(
                base, observation=dataclasses.replace(observation, dataset_id=_Spoof(DATASET_ID))
            ),
        ),
        ("observation_none", dataclasses.replace(base, observation=None)),
        (
            "observation_subclass",
            dataclasses.replace(
                base,
                observation=_ObservationSubclass(
                    **{f.name: getattr(observation, f.name) for f in _observation_fields()}
                ),
            ),
        ),
    ]


def _record_fields() -> tuple[dataclasses.Field[Any], ...]:
    return dataclasses.fields(dataset_state.DatasetStateRecord)


def _observation_fields() -> tuple[dataclasses.Field[Any], ...]:
    return dataclasses.fields(dataset_state.DatasetStateObservation)


@pytest.mark.parametrize(
    "label",
    [
        "wrong_type_object",
        "wrong_type_dict",
        "wrong_type_tuple",
        "record_subclass",
        "other_workspace",
        "spoofed_workspace",
        "non_string_workspace",
        "other_dataset",
        "spoofed_dataset",
        "observation_none",
        "observation_subclass",
    ],
)
def test_a_wrong_type_or_mismatched_read_result_is_refused_before_resolver_and_evaluator(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    _store(owned, at_us=BASE_US + 1)
    impossible = dict(_impossible_results(_stored_record(owned)))[label]
    reads: list[int] = []
    evaluated: list[int] = []

    def read(connection: Any, **keywords: Any) -> Any:
        reads.append(1)
        return impossible

    def never(*arguments: Any, **keywords: Any) -> Any:
        evaluated.append(1)
        raise AssertionError("the checkpoint must not run")

    monkeypatch.setattr(binding_module, "read_current_state", read)
    monkeypatch.setattr(binding_module, "evaluate_plan_admission_checkpoint", never)
    resolver = _Resolver()

    _refused(_context(), owned.connection, resolver)

    assert reads == [1]
    assert evaluated == []


# --- the downstream checkpoint keeps its own semantics ----------------------------------------


@pytest.mark.parametrize(
    ("use_class", "overrides", "outcome", "reasons"),
    [
        (
            USE_CURRENT_PUBLICATION,
            {"completeness": "unknown"},
            OUTCOME_DENY,
            ("completeness_unknown",),
        ),
        (USE_EXPLORATION, {}, OUTCOME_ALLOW_WITH_WARNING, ("exploration_non_certifying",)),
    ],
)
def test_deny_and_warning_outcomes_return_normally(
    owned: m2.Owned,
    use_class: str,
    overrides: dict[str, Any],
    outcome: str,
    reasons: tuple[str, ...],
) -> None:
    _store(owned, at_us=BASE_US + 1, **overrides)
    resolver = _Resolver()

    result = _run(_context(), owned.connection, resolver, use_class=use_class)

    assert type(result) is AnalysisResultUseCheckpoint
    assert result.decision.outcome == outcome
    assert result.decision.reasons == reasons
    assert resolver.calls == 1


def test_a_resolver_failure_stays_the_checkpoints_refusal(owned: m2.Owned) -> None:
    _store(owned, at_us=BASE_US + 1)

    class _Failing:
        calls = 0

        def resolve(self, query: AnalysisUseAuthorityQuery) -> Any:
            self.calls += 1
            raise RuntimeError(SENTINEL)

    resolver = _Failing()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        evaluate_storage_bound_plan_admission_checkpoint(
            _context(),
            owned.connection,
            dataset_id=DATASET_ID,
            subject_digest=SUBJECT_DIGEST,
            resolved_use_class=USE_CURRENT_PUBLICATION,
            evaluation_instant=INSTANT,
            resolver=resolver,
        )

    _assert_plain_refusal(raised.value)
    assert resolver.calls == 1


# --- the module surface ----------------------------------------------------------------------


def test_the_module_exposes_only_the_one_internal_entrypoint() -> None:
    public = {
        name
        for name, value in inspect.getmembers(binding_module, inspect.isfunction)
        if value.__module__ == binding_module.__name__ and not name.startswith("_")
    }
    assert public == {"evaluate_storage_bound_plan_admission_checkpoint"}
    assert not hasattr(binding_module, "__all__")

    parameters = inspect.signature(evaluate_storage_bound_plan_admission_checkpoint).parameters
    assert [(p.name, p.kind) for p in parameters.values()] == [
        ("context", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("connection", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("dataset_id", inspect.Parameter.KEYWORD_ONLY),
        ("subject_digest", inspect.Parameter.KEYWORD_ONLY),
        ("resolved_use_class", inspect.Parameter.KEYWORD_ONLY),
        ("evaluation_instant", inspect.Parameter.KEYWORD_ONLY),
        ("resolver", inspect.Parameter.KEYWORD_ONLY),
    ]
