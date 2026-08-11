"""V06-5 checkpoint S0: mutation authority, the execution seam, and its durable record.

S0 registers no operation handler and makes nothing new reachable. What it establishes
is the foundation S1-S4 will consume, and the properties below are the reason it is
established first rather than alongside the first handler that needs it.

**Authority is issued, never claimed.** A `MutationGrant` binds the authenticated
principal, the workspace, the required role, the effective scopes and capabilities, one
declared purpose, the live fencing generation and a validity window -- and
`issue_mutation_grant` is the only way to obtain one. Every field it fills comes from
the session the server built, the context the twelve-check seam returned, the guard row
read out of the database, or the frozen catalogue. A request can narrow all of that and
can widen none of it, which is what the identity tests here hold it to.

**There is no implicit local-owner mutation.** `local_owner_session` still refuses to
grant a side-effecting operation at all, and one layer down a session holding no role
cannot be issued a grant either -- so the refusal survives a wiring that widened the
operation set without noticing what it had done.

**Every accepted mutation is one transaction or none of it.** The domain write, 0007's
audit event, claim and terminal outcome, and 0013's execution record commit together
under a validated lease, or the whole thing rolls back. An honest replay is answered
from the stored canonical bytes without the domain mutation running again, and one
idempotency scope reused for a different canonical request is a typed conflict rather
than a second execution.

Everything here runs against real SQLite, the real migrator, the real ownership
substrate and the real guard triggers. The fixtures come from the accepted 0007
acceptance module rather than being re-derived, so "a migrated, owned workspace" means
the same thing in both files.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    StaleGeneration,
    fenced_transaction,
    guarded_tables,
    open_guard,
    read_guard,
    trigger_names,
    verify_fingerprint,
)
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import (
    ApplicationAuthorizationError,
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.mutation import (
    MUTATING_OPERATIONS,
    MUTATION_PURPOSES,
    MUTATION_ROLES,
    MutationDenied,
    MutationGrant,
    MutationIdempotencyConflict,
    MutationPreconditionFailed,
    MutationSeamFault,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import APPLICATION_OPERATIONS
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    GENERATION_ONE,
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    OPERATION_CATALOGUE,
    PURPOSE_PATTERN,
    SCOPE_KIND_WORKSPACE,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    MutationPrecondition,
    OperationMetadata,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    get_operation_metadata,
    idempotency_equivalence,
)

# --- the S0 migration under test ----------------------------------------------

MIGRATION_VERSION = 13
MIGRATION_NAME = "0013_mutation_execution_records.sql"
PREDECESSOR_NAME = "0012_evidence_search_projection.sql"

EXECUTIONS_TABLE = "omnivia_mutation_executions"
S0_TABLES = (EXECUTIONS_TABLE,)
S0_INDEXES = (
    "omnivia_idx_mutation_executions_claim",
    "omnivia_idx_mutation_executions_grant",
    "omnivia_idx_mutation_executions_workspace_time",
)
S0_TRIGGERS = tuple(
    f"omnivia_guard_mutation_executions_{statement}"
    for statement in ("insert", "authority", "update", "delete")
) + ("omnivia_guard_idempotency_outcomes_audit_link",)

#: Everything one accepted mutation writes, in the order 0007's foreign keys admit.
DURABLE_TABLES = (
    "omnivia_application_audit_events",
    "omnivia_idempotency_claims",
    "omnivia_idempotency_outcomes",
    EXECUTIONS_TABLE,
)


def migration_under_test() -> m1.Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


# --- the request, the session and the domain this file mutates ----------------
#
# `candidate.approve` is the worked example: workspace-scoped, side-effecting, and the
# catalogue requires both an idempotency key and a mutation precondition for it, so
# every property the seam owns is exercised by one operation rather than assembled from
# several. The other eight are covered by the purpose-mapping test, which builds a
# session and an authorized context for each of them.

OPERATION = "candidate.approve"
ENTRY = get_operation_metadata(OPERATION)
PURPOSE = MUTATION_PURPOSES[OPERATION]

WORKSPACE_ID = m1.WORKSPACE_ID
INSTALLATION_ID = "inst-s0-0001"
PRINCIPAL = "principal-s0"
#: Not chosen here: the server's own policy for this operation, read rather than named.
ROLE = MUTATION_ROLES[OPERATION]
CLIENT = ClientIdentity(id="s0-client", version="0.1.0")
BINDING = ServiceBinding(installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID)

IDEMPOTENCY_KEY = "s0-idem-0001"
RECORD_VERSION = "rv-s0-0001"

#: The seeded record's version, and the domain row a mutation adds. The precondition
#: reads the first; the mutation writes the second.
SEED_MEMORY_ID = "mem-s0-seed"
CREATED_MEMORY_ID = "mem-s0-created"

#: A notional server supporting every catalogue capability above its floor, so the
#: effective version in the common case is the session's grant.
SUPPORTED = tuple(
    CapabilityRef(id=capability_id, version="1.4")
    for capability_id in sorted(
        {entry.required_capability.id for entry in OPERATION_CATALOGUE}
    )
)

#: The server clock this file injects. Monotonic and wall move independently, which is
#: the whole reason the seam takes a `Clock` rather than a moment: every expiry decision
#: is made against the monotonic component, and every durable timestamp is the wall
#: component read at settlement.
MONOTONIC_BASE = 1_000.0
WALL_BASE = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
WALL_BASE_US = int(WALL_BASE.timestamp() * 1_000_000)

OPERATION_INPUT: Mapping[str, Any] = {"candidate_id": "cand-s0-1"}


def clock_at(monotonic: float = MONOTONIC_BASE, *, wall: datetime = WALL_BASE) -> FakeClock:
    return FakeClock(monotonic=monotonic, wall=wall)


def accept_any(_result: Mapping[str, Any]) -> bool:
    """The permissive validator the tests that are not about validation supply.

    The seam has no default validator -- that is the point of it being required -- so the
    call site states one, and the tests that *are* about validation state a real one.
    """
    return True


def metadata_for(entry: OperationMetadata, **overrides: Any) -> RequestMetadata:
    """Request metadata the catalogue validator accepts for `entry`.

    Derived from the catalogue entry -- scope kind, required scopes, required
    capability, idempotency and precondition postures -- so nothing here is a second
    opinion about what a well-formed request looks like.
    """
    workspace_id = overrides.pop("workspace_id", _UNSET)
    if workspace_id is _UNSET:
        workspace_id = (
            WORKSPACE_ID if entry.scope.scope_kind == SCOPE_KIND_WORKSPACE else None
        )
    required = entry.required_capability
    fields: dict[str, Any] = {
        "request_id": "req-s0-1",
        "correlation_id": "cor-s0-1",
        "trace_id": "trc-s0-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": workspace_id,
        "scopes": tuple(entry.scope.required_scopes),
        "purpose": MUTATION_PURPOSES.get(entry.name, PURPOSE),
        "required_capabilities": (
            CapabilityRequirement(
                id=required.id,
                minimum_version=required.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": (
            IDEMPOTENCY_KEY if entry.idempotency.supports_idempotency_key else None
        ),
        "mutation_precondition": (
            MutationPrecondition(record_version=RECORD_VERSION)
            if entry.precondition.supports_mutation_precondition
            else None
        ),
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestMetadata(**fields)


_UNSET = object()


def envelope_for(
    entry: OperationMetadata,
    *,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> RequestEnvelope:
    return RequestEnvelope(
        operation=entry.name,
        metadata=metadata_for(entry, **overrides),
        input=dict(operation_input),
    )


def session_for(entry: OperationMetadata, **overrides: Any) -> AuthenticatedSession:
    """A server-side session granting exactly what `entry` needs, and one role.

    This is the shape a future mutation wiring builds: a session with a role model
    behind it. `local_owner_session` cannot produce one -- it refuses a side-effecting
    operation outright -- which is precisely the point the first test makes.
    """
    required = entry.required_capability
    fields: dict[str, Any] = {
        "principal_id": PRINCIPAL,
        "roles": frozenset({MUTATION_ROLES.get(entry.name, ROLE)}),
        "installations": frozenset({INSTALLATION_ID}),
        "workspaces": frozenset({WORKSPACE_ID}),
        "operations": frozenset({entry.name}),
        "scopes": frozenset(entry.scope.required_scopes),
        "purposes": frozenset(MUTATION_PURPOSES.values()),
        "capabilities": (
            CapabilityRef(id=required.id, version=required.minimum_version),
        ),
    }
    fields.update(overrides)
    return AuthenticatedSession(**fields)


def authorize(
    entry: OperationMetadata = ENTRY,
    *,
    session: AuthenticatedSession | None = None,
    binding: ServiceBinding | None = None,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> AuthorizedApplicationContext:
    """The real twelve-check seam. Nothing in this file fabricates a context."""
    return authorize_application_request(
        envelope_for(entry, operation_input=operation_input, **overrides),
        session=session_for(entry) if session is None else session,
        binding=BINDING if binding is None else binding,
        supported_capabilities=SUPPORTED,
    )


def equivalence_for(
    entry: OperationMetadata = ENTRY,
    *,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> Any:
    """The accepted contract function's own equivalence value for this request."""
    return idempotency_equivalence(
        entry.name,
        metadata_for(entry, **overrides),
        dict(operation_input),
        principal_id=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
    )


# --- an owned, migrated workspace with a record to mutate ---------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    """A workspace at the full canonical schema, owned, with one seeded record."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    seed_record(holder)
    yield holder
    holder.connection.close()


def guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def seed_record(holder: m1.Owned) -> None:
    """The record a `candidate.approve` precondition is stated against.

    An ordinary guarded legacy table, written through the real fenced path -- so the
    domain half of "one transaction" below is a genuine mutation of authoritative
    storage rather than a stand-in.
    """
    with guarded(holder):
        holder.connection.execute(
            "INSERT INTO memories (id, workspace_id, content, source_type, "
            "source_reference, lifecycle_state, memory_type, created_by, created_at, "
            "updated_at) VALUES (?, ?, 'seed', 'note', 'ref', 'proposed', 'general', "
            "'s0', 'created', ?)",
            (SEED_MEMORY_ID, WORKSPACE_ID, RECORD_VERSION),
        )


def read_record_version(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT updated_at FROM memories WHERE id = ?", (SEED_MEMORY_ID,)
    ).fetchone()
    return None if row is None else str(row[0])


class DomainMutationSpy:
    """The domain half of one mutation, counting how many times it actually ran.

    The count is what makes "a replay does not re-run the mutation" a measured fact
    rather than an inference from row counts.
    """

    def __init__(self, *, memory_id: str = CREATED_MEMORY_ID, fail: bool = False) -> None:
        self.memory_id = memory_id
        self.fail = fail
        self.calls = 0
        self.result: Mapping[str, Any] = {"approved": True, "record_id": memory_id}

    def __call__(self, connection: sqlite3.Connection) -> Mapping[str, Any]:
        self.calls += 1
        connection.execute(
            "INSERT INTO memories (id, workspace_id, content, source_type, "
            "source_reference, lifecycle_state, memory_type, created_by, created_at, "
            "updated_at) VALUES (?, ?, 'approved', 'note', 'ref', 'approved', "
            "'general', 's0', 'created', 'rv-s0-0002')",
            (self.memory_id, WORKSPACE_ID),
        )
        if self.fail:
            raise DomainFailure("the domain mutation refused after writing")
        return self.result


class DomainFailure(RuntimeError):
    """Injected domain failure, distinguishable from any refusal the seam raises."""


def issue(
    holder: m1.Owned,
    context: AuthorizedApplicationContext,
    *,
    session: AuthenticatedSession | None = None,
    equivalence: Any = None,
    clock: FakeClock | None = None,
    **overrides: Any,
) -> MutationGrant:
    """Issue a grant from the live guard row, exactly as a server wiring would.

    There is no `required_role` to pass: the role is the server's own policy for the
    operation, so no caller of this helper -- and no future handler -- can name it.
    """
    guard = read_guard(holder.connection)
    assert guard is not None
    return issue_mutation_grant(
        context,
        session=session_for(ENTRY) if session is None else session,
        binding=BINDING,
        guard=guard,
        equivalence=equivalence_for() if equivalence is None else equivalence,
        clock=clock_at() if clock is None else clock,
        **overrides,
    )


def run(
    holder: m1.Owned,
    *,
    grant: MutationGrant,
    context: AuthorizedApplicationContext,
    mutate: DomainMutationSpy,
    equivalence: Any = None,
    precondition: Callable[[sqlite3.Connection], str | None] | None = read_record_version,
    validate_result: Callable[[Mapping[str, Any]], bool] = accept_any,
    clock: FakeClock | None = None,
) -> Any:
    return execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence_for() if equivalence is None else equivalence,
        precondition=precondition,
        mutate=mutate,
        validate_result=validate_result,
        clock=clock_at() if clock is None else clock,
    )


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Every durable row one mutation writes, plus the domain row it created."""
    tallies = {table: m1.count(connection, table) for table in DURABLE_TABLES}
    tallies["memories"] = m1.count(connection, "memories")
    return tallies


# --- S0-01: no implicit local-owner mutation ----------------------------------


def test_v06_5_s0_implicit_local_owner_mutation_denied(owned: m1.Owned) -> None:
    """A local owner cannot mutate, and cannot acquire the ability to by accident.

    Three independent refusals, because one of them alone is a single edit away from
    being untrue. `local_owner_session` refuses to build a session naming a
    side-effecting operation at all, and that refusal is derived from the catalogue
    rather than from a list -- so it covers all nine. A session that *did* grant one --
    the shape a careless widening would produce -- still cannot be issued a grant,
    because it holds no role. And this build registers no handler for any of them, so
    there is nothing for a grant to reach even if one existed.
    """
    for name in sorted(MUTATING_OPERATIONS):
        with pytest.raises(ValueError, match="declare a side effect"):
            local_owner_session(
                principal_id=PRINCIPAL,
                installation_id=INSTALLATION_ID,
                workspace_id=WORKSPACE_ID,
                operations=frozenset({name}),
            )

    # The read-only grant this build actually ships is unaffected.
    read_only = local_owner_session(
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        operations=frozenset({"workspace.inspect"}),
    )
    assert read_only.roles == frozenset()
    assert read_only.operations == frozenset({"workspace.inspect"})

    # One layer down: a session that granted the operation but holds no role -- which
    # is what a widened `local_owner_session` would produce -- is still refused.
    roleless = session_for(ENTRY, roles=frozenset())
    context = authorize(session=roleless)
    with pytest.raises(MutationDenied) as denied:
        issue(owned, context, session=roleless)
    assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED

    # And nothing is registered to serve it.
    assert MUTATING_OPERATIONS.isdisjoint(build_application_registry().operations)


# --- S0-02: the grant is required, and there is no default one ----------------


def test_v06_5_s0_server_issued_grant_required(owned: m1.Owned) -> None:
    """No grant, the wrong grant, or an expired grant: nothing is written.

    `MutationGrant` does not publish its authority fields as constructor inputs, so a
    request-derived mapping cannot be expanded into one and `dataclasses.replace`
    cannot widen an issued value. Execution checks the issuer mark again, making even
    an uninitialised instance a refusal rather than a partially stated grant.
    """
    for field in dataclasses.fields(MutationGrant):
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name

    context = authorize()
    honest = issue(owned, context)
    before = counts(owned.connection)

    with pytest.raises(TypeError, match="issued by the server"):
        MutationGrant()
    with pytest.raises(TypeError):
        MutationGrant(**context.__dict__)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        dataclasses.replace(honest, workspace_id="ws-somewhere-else")

    unissued = object.__new__(MutationGrant)
    spy = DomainMutationSpy()
    with pytest.raises(MutationDenied):
        run(
            owned,
            grant=cast("MutationGrant", unissued),
            context=context,
            mutate=spy,
        )
    assert spy.calls == 0

    mismatches = (
        ("workspace", honest, dataclasses.replace(context, workspace_id="ws-somewhere-else")),
        ("operation", honest, dataclasses.replace(context, operation="record.supersede")),
        ("purpose", honest, dataclasses.replace(context, purpose="job_control")),
        ("principal", honest, dataclasses.replace(context, principal_id="principal-elsewhere")),
        ("role", honest, dataclasses.replace(context, roles=())),
        ("scopes", honest, dataclasses.replace(context, scopes=())),
        ("capabilities", honest, dataclasses.replace(context, capabilities=())),
        # Both windows are stated in monotonic terms, because that is the only clock the
        # seam judges an expiry against: one issued a microsecond of monotonic time ago
        # and already over, and one issued a second of monotonic time from now.
        (
            "expired",
            issue(owned, context, clock=clock_at(MONOTONIC_BASE - 0.000_001), lifetime_us=1),
            context,
        ),
        (
            "not_yet_valid",
            issue(owned, context, clock=clock_at(MONOTONIC_BASE + 1.0)),
            context,
        ),
    )

    for case, grant, presented_context in mismatches:
        spy = DomainMutationSpy()
        with pytest.raises(MutationDenied) as denied:
            run(owned, grant=grant, context=presented_context, mutate=spy)
        assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED, case
        assert spy.calls == 0, case
        assert counts(owned.connection) == before, case

    for absent in (None, object()):
        spy = DomainMutationSpy()
        with pytest.raises(MutationDenied):
            execute_mutation(
                owned.connection,
                owned.identity,
                grant=cast("Any", absent),
                context=context,
                equivalence=equivalence_for(),
                precondition=read_record_version,
                mutate=spy,
                validate_result=accept_any,
                clock=clock_at(),
            )
        assert spy.calls == 0
        assert counts(owned.connection) == before

    # The honest grant, on the same connection, still works -- so the refusals above
    # are about the grants and not about the workspace being wedged.
    spy = DomainMutationSpy()
    outcome = run(owned, grant=honest, context=context, mutate=spy)
    assert spy.calls == 1
    assert outcome.replayed is False
    assert outcome.result == spy.result


# --- S0-03: a request can narrow the grant and can never widen it -------------


def test_v06_5_s0_request_identity_cannot_expand_grant(owned: m1.Owned) -> None:
    """Every field of the grant comes from server state, and this proves each one.

    The two halves matter separately. A request that claims *more* than the session
    holds is refused outright -- by the seam where the seam owns the rule, by issuance
    where issuance does. And a request that claims something admissible produces a
    grant identical to the one an unadorned request produces, so a caller cannot steer
    a field by stating it either.
    """
    baseline = issue(owned, authorize())
    assert baseline.principal_id == PRINCIPAL
    assert baseline.purpose == PURPOSE
    assert baseline.required_role == ROLE
    assert baseline.fencing_generation == owned.generation
    assert baseline.workspace_id == WORKSPACE_ID
    assert baseline.scopes == frozenset(ENTRY.scope.required_scopes)

    # Claims the seam refuses: a different principal, an unheld role, an ungranted
    # scope, an ungranted workspace.
    for case, overrides in (
        (
            "principal",
            {"principal_claim": PrincipalClaim(claimed_principal_id="principal-other")},
        ),
        (
            "role",
            {"principal_claim": PrincipalClaim(claimed_roles=("workspace_admin",))},
        ),
        ("scope", {"scopes": ("memory:write", "workspace:write")}),
        ("workspace", {"workspace_id": "ws-not-granted"}),
    ):
        with pytest.raises(ApplicationAuthorizationError):
            authorize(**overrides)

    # A purpose the session may act for, but not the one declared for this operation.
    # The seam admits it -- purposes are a session allowlist there -- and issuance is
    # what refuses, which is the mapping doing its job.
    wrong_purpose = authorize(purpose="job_control")
    assert wrong_purpose.purpose == "job_control"
    with pytest.raises(MutationDenied):
        issue(owned, wrong_purpose)

    # Admissible claims narrow, and change nothing about the issued grant.
    narrowed = issue(
        owned,
        authorize(
            principal_claim=PrincipalClaim(
                claimed_principal_id=PRINCIPAL, claimed_roles=(ROLE,)
            )
        ),
    )
    assert _grant_facts(narrowed) == _grant_facts(baseline)
    assert narrowed.grant_id != baseline.grant_id

    # The fencing generation is not a request field at all -- the contract's metadata
    # has no such field -- and it tracks the server's own guard row: a byte-identical
    # request issued after a real takeover carries the successor's generation.
    assert not hasattr(metadata_for(ENTRY), "fencing_generation")
    successor = m1.make_identity(instance="svc-s0-two", pid=5252)
    lease = acquire_lease(
        owned.connection,
        successor,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=owned.identity.service_instance_id,
    )
    assert lease.fencing_generation > owned.generation
    open_guard(
        owned.connection,
        successor,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )

    after_takeover = issue(owned, authorize())
    assert after_takeover.fencing_generation == lease.fencing_generation
    assert after_takeover.fencing_generation != baseline.fencing_generation
    # Everything a caller *could* state is unchanged; only the server fact moved.
    assert _grant_facts(after_takeover)[:-1] == _grant_facts(baseline)[:-1]


def _grant_facts(grant: MutationGrant) -> tuple[Any, ...]:
    """Everything about a grant except the fields that are new per issuance."""
    return (
        grant.principal_id,
        grant.workspace_id,
        grant.required_role,
        grant.scopes,
        grant.capabilities,
        grant.operation,
        grant.purpose,
        grant.idempotency_key,
        grant.request_fingerprint,
        grant.required_record_version,
        grant.fencing_generation,
    )


# --- S0-04: all nine mutating operations declare a purpose --------------------


def test_v06_5_s0_all_nine_mutation_purposes_declared(owned: m1.Owned) -> None:
    """Exactly the nine, explicitly, with a mismatch failing closed for each of them."""
    assert set(MUTATION_PURPOSES) == {
        "workspace.create",
        "memory.create",
        "import.start",
        "job.cancel",
        "job.retry",
        "knowledge.propose",
        "candidate.approve",
        "candidate.reject",
        "record.supersede",
    }
    # The same set, derived from the frozen catalogue rather than transcribed.
    assert set(MUTATION_PURPOSES) == MUTATING_OPERATIONS
    assert len(MUTATION_PURPOSES) == 9
    # And no read operation borrowed one.
    for name in APPLICATION_OPERATIONS - MUTATING_OPERATIONS:
        assert name not in MUTATION_PURPOSES

    purpose_pattern = re.compile(PURPOSE_PATTERN)
    for name, purpose in MUTATION_PURPOSES.items():
        assert purpose_pattern.fullmatch(purpose), (name, purpose)
        assert 1 <= len(purpose) <= 128, (name, purpose)

    # Values may be shared by a coherent family, and are.
    assert MUTATION_PURPOSES["job.cancel"] == MUTATION_PURPOSES["job.retry"]
    governance = {
        MUTATION_PURPOSES[name]
        for name in (
            "knowledge.propose",
            "candidate.approve",
            "candidate.reject",
            "record.supersede",
        )
    }
    assert len(governance) == 1
    assert len(set(MUTATION_PURPOSES.values())) == 5

    # Every operation is exercised: the declared purpose is what the grant carries, and
    # any other purpose the session may act for is refused.
    for name in sorted(MUTATION_PURPOSES):
        entry = get_operation_metadata(name)
        declared = MUTATION_PURPOSES[name]
        session = session_for(entry)
        other = min(set(MUTATION_PURPOSES.values()) - {declared})

        equivalence = equivalence_for(entry)
        mismatched = authorize(entry, session=session, purpose=other)
        with pytest.raises(MutationDenied):
            issue(owned, mismatched, session=session, equivalence=equivalence)

        context = authorize(entry, session=session)
        if entry.scope.scope_kind != SCOPE_KIND_WORKSPACE:
            # `workspace.create` is installation-scoped, and 0007's tables bind the
            # singleton workspace, so this seam refuses it rather than inventing one.
            with pytest.raises(MutationDenied):
                issue(owned, context, session=session, equivalence=equivalence)
            continue
        issued = issue(owned, context, session=session, equivalence=equivalence)
        assert issued.purpose == declared
        assert issued.required_role == MUTATION_ROLES[name]


# --- S0-05: the domain mutation and its durable facts are one transaction -----


def test_v06_5_s0_audit_and_idempotency_atomic(owned: m1.Owned) -> None:
    """One accepted mutation writes five rows together; a replay writes none."""
    context = authorize()
    grant = issue(owned, context)
    spy = DomainMutationSpy()

    outcome = run(owned, grant=grant, context=context, mutate=spy)
    assert spy.calls == 1
    assert outcome.replayed is False
    assert outcome.result == {"approved": True, "record_id": CREATED_MEMORY_ID}

    connection = owned.connection
    assert counts(connection) == {
        "omnivia_application_audit_events": 1,
        "omnivia_idempotency_claims": 1,
        "omnivia_idempotency_outcomes": 1,
        EXECUTIONS_TABLE: 1,
        # the seeded record plus the one the mutation created
        "memories": 2,
    }
    assert integrity_check(connection) == []
    assert foreign_key_check(connection) == []

    audit = connection.execute(
        "SELECT workspace_id, principal_id, operation, purpose, outcome_class, "
        "error_code FROM omnivia_application_audit_events"
    ).fetchone()
    assert audit == (WORKSPACE_ID, PRINCIPAL, OPERATION, PURPOSE, "succeeded", None)

    claim = connection.execute(
        "SELECT workspace_id, principal_id, operation, idempotency_key, request_digest "
        "FROM omnivia_idempotency_claims"
    ).fetchone()
    assert claim == (
        WORKSPACE_ID,
        PRINCIPAL,
        OPERATION,
        IDEMPOTENCY_KEY,
        equivalence_for().fingerprint,
    )

    # The execution record binds the answer to the grant it ran under -- which is the
    # fact 0007 has no column for.
    execution = connection.execute(
        "SELECT grant_id, required_role, scopes_json, capabilities_json, "
        "fencing_generation, grant_issued_at_us, grant_issued_monotonic_us, "
        "grant_expires_monotonic_us, settled_monotonic_us, claim_id, audit_ref, "
        "recorded_at_us FROM omnivia_mutation_executions"
    ).fetchone()
    assert execution[:2] == (
        grant.grant_id,
        ROLE,
    )
    assert json.loads(str(execution[2])) == sorted(grant.scopes)
    assert json.loads(str(execution[3])) == [
        capability.to_wire() for capability in grant.capabilities
    ]
    assert execution[4:] == (
        grant.fencing_generation,
        grant.issued_at_us,
        grant.issued_monotonic_us,
        grant.expires_monotonic_us,
        # The settlement reading, proved against the monotonic window rather than
        # against either wall value.
        int(MONOTONIC_BASE * 1_000_000),
        outcome.claim_id,
        outcome.audit_ref,
        WALL_BASE_US,
    )

    settled = counts(connection)

    # An honest replay: same scope, same canonical request. Answered from the stored
    # bytes, with the domain mutation never re-entered. The fresh grant it was presented
    # with is spent -- one more execution row, of kind `replayed` -- and nothing else
    # moves: no second audit event, claim, outcome or domain row.
    replay_spy = DomainMutationSpy()
    replayed = run(owned, grant=issue(owned, authorize()), context=authorize(), mutate=replay_spy)
    assert replay_spy.calls == 0
    assert replayed.replayed is True
    assert replayed.result == outcome.result
    assert replayed.claim_id == outcome.claim_id
    assert counts(connection) == {
        **settled,
        EXECUTIONS_TABLE: settled[EXECUTIONS_TABLE] + 1,
    }
    settled = counts(connection)

    # The same scope with a different canonical request is a typed conflict, and the
    # stored claim is left exactly as it was.
    conflict_spy = DomainMutationSpy(memory_id="mem-s0-conflict")
    changed_input = {"candidate_id": "cand-s0-2"}
    with pytest.raises(MutationIdempotencyConflict) as conflict:
        run(
            owned,
            grant=issue(
                owned,
                authorize(operation_input=changed_input),
                equivalence=equivalence_for(operation_input=changed_input),
            ),
            context=authorize(operation_input=changed_input),
            mutate=conflict_spy,
            equivalence=equivalence_for(operation_input=changed_input),
        )
    assert conflict.value.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert conflict_spy.calls == 0
    assert counts(connection) == settled
    stored = connection.execute(
        "SELECT request_digest FROM omnivia_idempotency_claims"
    ).fetchone()
    assert stored == (equivalence_for().fingerprint,)

    # One server grant cannot authorize a distinct idempotency scope. The refusal is
    # before domain code and the unique index says the same thing structurally.
    second_key = "s0-idem-0002"
    second_context = authorize(idempotency_key=second_key)
    second_spy = DomainMutationSpy(memory_id="mem-s0-second-grant-use")
    with pytest.raises(MutationDenied):
        run(
            owned,
            grant=grant,
            context=second_context,
            mutate=second_spy,
            equivalence=equivalence_for(idempotency_key=second_key),
        )
    assert second_spy.calls == 0
    assert counts(connection) == settled


# --- S0-06: precondition before the mutation, fencing around all of it --------


def test_v06_5_s0_precondition_and_fencing_atomic(owned: m1.Owned) -> None:
    """The precondition is checked first, and lost authority never commits."""
    context = authorize()
    before = counts(owned.connection)

    # A precondition the record does not satisfy: refused before the domain mutation is
    # entered, so `calls` is the proof rather than the absence of a row.
    stale_spy = DomainMutationSpy()
    with pytest.raises(MutationPreconditionFailed) as failed:
        run(
            owned,
            grant=issue(owned, context),
            context=context,
            mutate=stale_spy,
            precondition=lambda _connection: "rv-somewhere-else",
        )
    assert failed.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED
    assert stale_spy.calls == 0
    assert counts(owned.connection) == before

    # A record that has since disappeared reads as no version at all, which is not the
    # version the request requires either.
    missing_spy = DomainMutationSpy()
    with pytest.raises(MutationPreconditionFailed):
        run(
            owned,
            grant=issue(owned, context),
            context=context,
            mutate=missing_spy,
            precondition=lambda _connection: None,
        )
    assert missing_spy.calls == 0
    assert counts(owned.connection) == before

    # A real takeover closes out a grant that was valid when it was issued. This uses
    # only an honestly issued grant; the test does not manufacture a widened grant to
    # simulate fencing, because public grant replacement is itself prohibited.
    grant = issue(owned, context)
    successor = m1.make_identity(instance="svc-s0-three", pid=6363)
    lease = acquire_lease(
        owned.connection,
        successor,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=owned.identity.service_instance_id,
    )
    assert lease.fencing_generation > grant.fencing_generation

    superseded_spy = DomainMutationSpy()
    with pytest.raises(StaleGeneration):
        run(owned, grant=grant, context=context, mutate=superseded_spy)
    assert counts(owned.connection) == before
    assert owned.connection.in_transaction is False


# --- S0-07: one rollback covers the domain and every durable fact -------------


def test_v06_5_s0_rollback_leaves_no_partial_canonical_state(owned: m1.Owned) -> None:
    """A failure anywhere leaves the workspace exactly as it was.

    Two failure points, either side of the durable writes. A domain mutation that
    writes and then raises must leave no row behind -- the case a separate transaction
    per concern would get wrong. And a failure *after* the domain mutation succeeded,
    while the durable facts are being written, must take the domain row with it -- the
    case an "audit afterwards" design would get wrong.
    """
    context = authorize()
    before = counts(owned.connection)

    failing = DomainMutationSpy(fail=True)
    with pytest.raises(DomainFailure):
        run(owned, grant=issue(owned, context), context=context, mutate=failing)
    assert failing.calls == 1
    assert counts(owned.connection) == before
    assert owned.connection.in_transaction is False

    # The domain mutation succeeds, and the durable write is what fails: an outcome
    # this build cannot store. The row the mutation wrote is gone with it.
    oversized = DomainMutationSpy(memory_id="mem-s0-oversized")
    oversized.result = {"blob": "x" * 9000}
    with pytest.raises(MutationSeamFault):
        run(owned, grant=issue(owned, context), context=context, mutate=oversized)
    assert oversized.calls == 1
    assert counts(owned.connection) == before

    # A refusal is survivable: the very next honest mutation commits normally.
    spy = DomainMutationSpy()
    outcome = run(owned, grant=issue(owned, context), context=context, mutate=spy)
    assert outcome.replayed is False
    assert counts(owned.connection) == {
        "omnivia_application_audit_events": 1,
        "omnivia_idempotency_claims": 1,
        "omnivia_idempotency_outcomes": 1,
        EXECUTIONS_TABLE: 1,
        "memories": 2,
    }
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []

    # None of the rolled-back domain rows exists.
    ids = {
        str(row[0])
        for row in owned.connection.execute("SELECT id FROM memories").fetchall()
    }
    assert ids == {SEED_MEMORY_ID, CREATED_MEMORY_ID}


# --- S0-08: the migration itself ----------------------------------------------


def test_v06_5_s0_migration_upgrade_recovery_and_old_version_refusal(
    tmp_path: Path,
) -> None:
    """0013 lands once, resumes after an interruption, and is refused when absent.

    Three obligations in one place because they are one property seen from three
    sides: the schema either carries this migration completely or not at all.
    """
    # Lineage. A unique consecutive successor to the accepted head, pinned by content.
    ordered = load_migrations()
    assert [m.version for m in ordered] == list(range(1, MIGRATION_VERSION + 1))
    assert ordered[-1].name == MIGRATION_NAME
    assert ordered[-2].name == PREDECESSOR_NAME
    assert MIGRATION.checksum == hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest()

    # It is additive DDL over reserved-prefixed objects, so it can rewrite nothing.
    for statement in MIGRATION_STATEMENTS:
        normalised = " ".join(statement.split()).upper()
        assert normalised.startswith(
            ("CREATE TABLE", "CREATE UNIQUE INDEX", "CREATE INDEX", "CREATE TRIGGER")
        ), normalised[:80]
    for pattern in (
        r"\bINSERT\s+INTO\b",
        r"\bUPDATE\s+\w+\s+SET\b",
        r"\bDELETE\s+FROM\b",
        r"\bALTER\s+TABLE\b",
        r"\bDROP\s+(TABLE|INDEX|TRIGGER|VIEW)\b",
        r"\bPRAGMA\b",
    ):
        assert re.search(pattern, MIGRATION.sql, re.IGNORECASE) is None, pattern

    # --- upgrade: a pristine workspace and an adopted one both reach it ---------
    for name, adopt in (("pristine", False), ("adopted", True)):
        path = tmp_path / f"{name}.sqlite"
        if adopt:
            materialise_phase0_baseline(path)
            m1.populate_legacy_corpus(path)
            legacy_before = m1.legacy_inventory(path)
        else:
            # A maintenance open refuses to create a database, so the pristine case
            # supplies an empty file for it to find rather than a missing one.
            path.touch()
        m1.bootstrap_and_migrate(path, adopt_phase0=adopt)

        connection = open_database(path, OpenMode.READ_ONLY)
        try:
            assert max(applied_migrations(connection)) == MIGRATION_VERSION
            present = m1.object_names(connection, "table")
            assert set(S0_TABLES) <= present
            assert set(S0_INDEXES) <= m1.object_names(connection, "index")
            assert set(S0_TRIGGERS) <= m1.object_names(connection, "trigger")
            assert integrity_check(connection) == []
            assert foreign_key_check(connection) == []
            assert m1.count(connection, EXECUTIONS_TABLE) == 0
        finally:
            connection.close()
        if adopt:
            assert m1.legacy_inventory(path) == legacy_before

    # The table is ordinary guarded storage, derived rather than declared exempt.
    assert EXECUTIONS_TABLE in canonical_schema_tables()
    assert EXECUTIONS_TABLE in guarded_tables()

    # --- recovery: interrupt after each statement; a retry converges exactly once --
    for stop_after in range(1, len(MIGRATION_STATEMENTS) + 1):
        path = tmp_path / f"interrupted-{stop_after}.sqlite"
        materialise_phase0_baseline(path)
        _apply_through_predecessor(path)

        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            crashing = m1.FailAfterStatement(
                connection, MIGRATION_STATEMENTS, stop_after
            )
            with pytest.raises(m1.MigrationInterrupted, match=f"statement {stop_after}$"):
                apply_pending_migrations(
                    cast("sqlite3.Connection", crashing),
                    mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                    service_instance_id=m1.SERVICE_INSTANCE,
                    fencing_generation=GENERATION_ONE,
                    workspace_id=WORKSPACE_ID,
                )
            assert crashing.executed == stop_after
        finally:
            connection.close()

        complete = set(S0_TABLES) | set(S0_INDEXES) | set(S0_TRIGGERS)
        resumed = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            # All of it or none of it -- never a table without its guards.
            objects = (
                m1.object_names(resumed, "table")
                | m1.object_names(resumed, "index")
                | m1.object_names(resumed, "trigger")
            )
            assert MIGRATION_VERSION not in applied_migrations(resumed)
            assert objects.isdisjoint(complete), sorted(objects & complete)

            failed = resumed.execute(
                "SELECT detail FROM omnivia_migration_attempts "
                "WHERE version = ? AND outcome = 'failed'",
                (MIGRATION_VERSION,),
            ).fetchall()
            assert len(failed) == 1, failed
            assert f"statement {stop_after}" in str(failed[0][0])
        finally:
            resumed.close()

        retried = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            applied = apply_pending_migrations(
                retried,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=GENERATION_ONE,
                workspace_id=WORKSPACE_ID,
            )
            assert [m.version for m in applied] == [MIGRATION_VERSION]
            rows = retried.execute(
                "SELECT COUNT(*) FROM omnivia_schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()
            assert rows == (1,)
            objects = (
                m1.object_names(retried, "table")
                | m1.object_names(retried, "index")
                | m1.object_names(retried, "trigger")
            )
            assert complete <= objects
            assert fingerprint_schema(retried).matches(canonical_schema_fingerprint())
            assert integrity_check(retried) == []
        finally:
            retried.close()

    # --- old-version refusal: a workspace still at 0012 -------------------------
    old = tmp_path / "old-version.sqlite"
    materialise_phase0_baseline(old)
    _apply_through_predecessor(old)

    stale = open_database(old, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        assert max(applied_migrations(stale)) == MIGRATION_VERSION - 1
        assert m1.object_names(stale, "table").isdisjoint(S0_TABLES)
        # Refused before writable readiness: the canonical oracle carries 0013 and
        # this database does not.
        with pytest.raises(SchemaDrift, match="fingerprint differs"):
            verify_fingerprint(stale, canonical_schema_fingerprint())
    finally:
        stale.close()

    holder = m1.take_ownership(old)
    try:
        seed_record(holder)
        context = authorize()
        before = m1.count(holder.connection, "memories")
        spy = DomainMutationSpy()
        # Fails closed rather than committing a domain mutation whose durable record
        # this workspace has nowhere to put.
        with pytest.raises(sqlite3.DatabaseError):
            run(holder, grant=issue(holder, context), context=context, mutate=spy)
        assert m1.count(holder.connection, "memories") == before
        assert holder.connection.in_transaction is False
        for table in DURABLE_TABLES[:-1]:
            assert m1.count(holder.connection, table) == 0
    finally:
        holder.connection.close()

    # An edited-after-applying 0013 is refused rather than silently accepted. The
    # generation is read rather than assumed: taking ownership above advanced it.
    edited = open_database(old, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(edited)
        assert state is not None
        apply_pending_migrations(
            edited,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m1.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        edited.execute(
            "UPDATE omnivia_schema_migrations SET checksum = ? WHERE version = ?",
            ("0" * 64, MIGRATION_VERSION),
        )
        edited.commit()
        with pytest.raises(StorageError, match="has changed since it was applied"):
            apply_pending_migrations(
                edited,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        edited.close()


def _apply_through_predecessor(path: Path) -> None:
    """Bootstrap and migrate to the accepted head *before* this slice."""
    with m1.migration_catalogue_through(MIGRATION_VERSION - 1):
        m1.bootstrap_and_migrate(path)


def test_v06_5_s0_execution_record_is_append_only_and_guarded(owned: m1.Owned) -> None:
    """0013 carries 0007's enforcement, not a weaker copy of it.

    Named separately from the migration test above because it is about the live
    workspace rather than about applying the migration: the three statement triggers
    exist by name, UPDATE and DELETE abort for the *current fenced owner*, and an
    INSERT outside a fenced transaction is refused.
    """
    present = set(trigger_names(owned.connection))
    assert set(S0_TRIGGERS) <= present

    context = authorize()
    run(owned, grant=issue(owned, context), context=context, mutate=DomainMutationSpy())
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 1

    for statement in (
        f"UPDATE {EXECUTIONS_TABLE} SET required_role = 'root'",
        f"DELETE FROM {EXECUTIONS_TABLE}",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), guarded(owned):
            owned.connection.execute(statement)
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 1

    # Outside a fenced transaction the governed connection refuses the write outright.
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute(
            f"INSERT INTO {EXECUTIONS_TABLE} (execution_id, workspace_id, "
            "principal_id, operation, purpose, grant_id, required_role, scopes_json, "
            "capabilities_json, fencing_generation, grant_issued_at_us, "
            "grant_issued_monotonic_us, grant_expires_monotonic_us, "
            "settled_monotonic_us, claim_id, audit_ref, recorded_at_us) "
            "VALUES ('exe-x', ?, 'p', 'o.p', 'a', 'g', 'r', "
            "'[]', '[]', 1, 1, 1, 2, 1, 'clm-x', 'aud-x', 1)",
            (WORKSPACE_ID,),
        )
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 1


# --- S0-09: registry construction is injectable without global state ----------


def test_v06_5_s0_registry_construction_is_test_injectable() -> None:
    """A test can add a handler; production cannot acquire one by accident."""
    shipped = frozenset(
        {
            "workspace.inspect",
            "evidence.search",
            "knowledge.search",
            "memory.search",
            "graph.traverse",
            "context_pack.build",
        }
    )
    default = build_application_registry()
    assert default.operations == shipped
    assert len(shipped) == 6
    # None of the fourteen unserved operations, mutating or not.
    assert (APPLICATION_OPERATIONS - shipped) & default.operations == frozenset()
    assert len(APPLICATION_OPERATIONS - shipped) == 14

    def stub(_context: object) -> Mapping[str, Any]:
        return {}

    injected = build_application_registry(additional={OPERATION: stub})
    assert injected.operations == shipped | {OPERATION}
    assert injected.get(OPERATION) is stub

    # The injection is per-call: no module state moved, so the default is unchanged
    # both before and after -- which is the property monkeypatching cannot offer.
    assert build_application_registry().operations == shipped
    assert default.get(OPERATION) is None

    # Nothing is relaxed for an injected handler.
    with pytest.raises(ValueError, match="not part of the accepted"):
        build_application_registry(additional={"core.health": stub})
    with pytest.raises(ValueError, match="already registered"):
        build_application_registry(additional={"workspace.inspect": stub})

    # Keyword-only, so the production call site cannot pass one positionally.
    parameters = inspect.signature(build_application_registry).parameters
    assert [p.kind for p in parameters.values()] == [inspect.Parameter.KEYWORD_ONLY]
    assert parameters["additional"].default is None


def test_v06_5_s0_module_declares_no_skip_or_xfail() -> None:
    """Every property above is asserted in this run, or this file is not evidence."""
    source = Path(__file__).read_text(encoding="utf-8")
    decorators = re.findall(r"^@(\S+)", source, re.MULTILINE)
    assert not [name for name in decorators if "skip" in name or "xfail" in name]
    assert not re.search(r"\bpytest\.(skip|xfail)\(", source)


# --- CP-S0-W: the grant names one canonical request, and is spent once --------


def test_v06_5_s0_grant_is_bound_to_canonical_request(owned: m1.Owned) -> None:
    """A grant authorizes one request, not an operation in general.

    Issuance binds the idempotency scope and key, the contract's own fingerprint of the
    request, and the precondition the request requires. Execution refuses anything that
    differs in any of the three -- before the transaction, and therefore before a
    handler could run.
    """
    context = authorize()
    grant = issue(owned, context)
    assert grant.idempotency_key == IDEMPOTENCY_KEY
    assert grant.request_fingerprint == equivalence_for().fingerprint
    assert grant.required_record_version == RECORD_VERSION
    # The scope is the grant's own operation, workspace and principal -- so an
    # equivalence describing a different request cannot be bound in the first place.
    with pytest.raises(MutationDenied):
        issue(
            owned, context, equivalence=equivalence_for(idempotency_key="s0-idem-other")
        )

    before = counts(owned.connection)
    other_key = "s0-idem-other"
    other_input = {"candidate_id": "cand-s0-other"}
    other_version = MutationPrecondition(record_version="rv-s0-other")

    for case, presented_context, presented_equivalence in (
        (
            "body",
            authorize(operation_input=other_input),
            equivalence_for(operation_input=other_input),
        ),
        (
            "key",
            authorize(idempotency_key=other_key),
            equivalence_for(idempotency_key=other_key),
        ),
        (
            "precondition",
            authorize(mutation_precondition=other_version),
            equivalence_for(mutation_precondition=other_version),
        ),
    ):
        spy = DomainMutationSpy(memory_id=f"mem-s0-{case}")
        with pytest.raises(MutationDenied) as denied:
            run(
                owned,
                grant=grant,
                context=presented_context,
                mutate=spy,
                equivalence=presented_equivalence,
            )
        assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED, case
        assert spy.calls == 0, case
        assert counts(owned.connection) == before, case

    # The grant is still good for the one request it was issued for.
    spy = DomainMutationSpy()
    outcome = run(owned, grant=grant, context=context, mutate=spy)
    assert spy.calls == 1
    assert outcome.replayed is False


def test_v06_5_s0_fresh_replay_grant_is_durably_consumed(owned: m1.Owned) -> None:
    """A grant spent on an honest replay is spent for good.

    The replay itself must stay honest -- the stored answer, the domain mutation never
    re-entered -- but the fresh grant it was presented with cannot survive it, or a
    caller could obtain a grant, spend it on a free replay, and keep it to authorize a
    mutation that actually writes. The execution row is what makes the expenditure
    durable, and the unique index on `grant_id` is what makes it structural.
    """
    context = authorize()
    first = run(
        owned, grant=issue(owned, context), context=context, mutate=DomainMutationSpy()
    )
    assert first.replayed is False

    replay_grant = issue(owned, authorize())
    replay_spy = DomainMutationSpy()
    replayed = run(owned, grant=replay_grant, context=authorize(), mutate=replay_spy)
    assert replay_spy.calls == 0
    assert replayed.replayed is True
    assert replayed.result == first.result
    assert replayed.claim_id == first.claim_id

    kinds = owned.connection.execute(
        f"SELECT grant_id, execution_kind, claim_id FROM {EXECUTIONS_TABLE} "
        "ORDER BY recorded_at_us, execution_kind"
    ).fetchall()
    assert [(str(row[1]), str(row[2])) for row in kinds] == [
        ("executed", first.claim_id),
        ("replayed", first.claim_id),
    ]
    assert replay_grant.grant_id in {str(row[0]) for row in kinds}
    settled = counts(owned.connection)

    # The same grant a second time is refused, whether it is offered the request it
    # already answered or a different one. Nothing new is written either way.
    for case, presented_context, presented_equivalence in (
        ("same", authorize(), equivalence_for()),
        (
            "different",
            authorize(idempotency_key="s0-idem-0003"),
            equivalence_for(idempotency_key="s0-idem-0003"),
        ),
    ):
        spy = DomainMutationSpy(memory_id=f"mem-s0-spent-{case}")
        with pytest.raises(MutationDenied) as denied:
            run(
                owned,
                grant=replay_grant,
                context=presented_context,
                mutate=spy,
                equivalence=presented_equivalence,
            )
        assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED, case
        assert spy.calls == 0, case
        assert counts(owned.connection) == settled, case

    # A fresh grant still replays honestly, so the refusals above are about the spent
    # grant rather than about the claim being closed to further replays.
    again = run(
        owned,
        grant=issue(owned, authorize()),
        context=authorize(),
        mutate=DomainMutationSpy(),
    )
    assert again.replayed is True
    assert again.result == first.result
    assert counts(owned.connection) == {
        **settled,
        EXECUTIONS_TABLE: settled[EXECUTIONS_TABLE] + 1,
    }
    # One real execution per claim throughout: the domain mutation ran exactly once.
    executed = owned.connection.execute(
        f"SELECT COUNT(*) FROM {EXECUTIONS_TABLE} WHERE execution_kind = 'executed'"
    ).fetchone()
    assert executed == (1,)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


def test_v06_5_s0_required_roles_are_exact_and_server_selected(owned: m1.Owned) -> None:
    """The role for a mutation is server policy, not an argument anyone supplies."""
    assert dict(MUTATION_ROLES) == {
        "workspace.create": "installation_administrator",
        "memory.create": "workspace_contributor",
        "import.start": "workspace_contributor",
        "job.cancel": "workspace_contributor",
        "job.retry": "workspace_contributor",
        "knowledge.propose": "workspace_contributor",
        "candidate.approve": "knowledge_reviewer",
        "candidate.reject": "knowledge_reviewer",
        "record.supersede": "knowledge_reviewer",
    }
    assert set(MUTATION_ROLES) == MUTATING_OPERATIONS

    # Neither the role nor the purpose is an input to the issuer, so no caller and no
    # future handler can choose the policy its own mutation is checked against.
    parameters = inspect.signature(issue_mutation_grant).parameters
    assert "required_role" not in parameters
    assert "purpose" not in parameters
    assert [
        name
        for name, p in parameters.items()
        if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ] == ["context"]

    # A session holding some other real role is refused: the required role is read from
    # the map by operation rather than taken from whatever the session happens to hold.
    for held in ("workspace_contributor", "installation_administrator"):
        session = session_for(ENTRY, roles=frozenset({held}))
        with pytest.raises(MutationDenied) as denied:
            issue(owned, authorize(session=session), session=session)
        assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED, held

    # The right role is issued, and is what the durable record carries.
    context = authorize()
    grant = issue(owned, context)
    assert grant.required_role == "knowledge_reviewer"
    run(owned, grant=grant, context=context, mutate=DomainMutationSpy())
    stored = owned.connection.execute(
        f"SELECT required_role FROM {EXECUTIONS_TABLE}"
    ).fetchone()
    assert stored == ("knowledge_reviewer",)

    # `workspace.create` carries the installation role for completeness only: this lane
    # refuses it at issuance and nothing is registered to serve it.
    create = get_operation_metadata("workspace.create")
    admin_session = session_for(create)
    assert admin_session.roles == frozenset({"installation_administrator"})
    with pytest.raises(MutationDenied):
        issue(
            owned,
            authorize(create, session=admin_session),
            session=admin_session,
            equivalence=equivalence_for(create),
        )
    assert "workspace.create" not in build_application_registry().operations


# --- CP-S0-W: the clock is the server's, and expiry is proved at settlement ----


def test_v06_5_s0_expiry_is_rechecked_before_commit(owned: m1.Owned) -> None:
    """A grant that expires while the handler works settles nothing.

    Checking the window once, on entry, would make the check a formality: the domain
    mutation runs after it, and a handler that takes longer than the grant lives would
    commit under authority that had already lapsed. So the monotonic clock is read again
    inside the fenced transaction, immediately before the first durable write, and the
    refusal there takes the domain mutation with it.

    Neither reading is a wall reading, and this shows the difference rather than asserting
    it: a wall clock that jumps -- forwards past the expiry, or backwards before the
    issue time -- changes nothing about whether the grant is current, because the two
    clocks share no origin and the seam never compares one to the other.
    """
    context = authorize()
    before = counts(owned.connection)

    # Neither the issuance moment nor the deadline is an argument any caller supplies.
    parameters = inspect.signature(execute_mutation).parameters
    assert "now_us" not in parameters
    assert parameters["clock"].default is inspect.Parameter.empty
    assert "now_us" not in inspect.signature(issue_mutation_grant).parameters

    # Entry: monotonic time has already passed the window, and no domain code runs.
    clock = clock_at()
    early_spy = DomainMutationSpy()
    grant = issue(owned, context, clock=clock, lifetime_us=1_000_000)
    clock.advance_monotonic(2.0)
    with pytest.raises(MutationDenied) as denied:
        run(owned, grant=grant, context=context, mutate=early_spy, clock=clock)
    assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert early_spy.calls == 0
    assert counts(owned.connection) == before

    # The recheck: the window is open on entry, and the handler itself advances the
    # monotonic clock through it. The mutation ran, and none of it survives.
    clock = clock_at()
    grant = issue(owned, context, clock=clock, lifetime_us=1_000_000)

    class SlowMutation(DomainMutationSpy):
        def __call__(self, connection: sqlite3.Connection) -> Mapping[str, Any]:
            result = super().__call__(connection)
            clock.advance_monotonic(5.0)
            return result

    slow = SlowMutation(memory_id="mem-s0-slow")
    with pytest.raises(MutationDenied) as denied:
        run(owned, grant=grant, context=context, mutate=slow, clock=clock)
    assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert slow.calls == 1
    assert counts(owned.connection) == before
    assert owned.connection.in_transaction is False

    # A wall clock that jumps is not an expiry event. The same grant, offered under a
    # wall clock that has moved an hour backwards and then two hours forwards, still
    # commits -- and every durable timestamp is the wall reading taken at settlement,
    # not the one taken at issuance.
    clock = clock_at()
    grant = issue(owned, context, clock=clock, lifetime_us=1_000_000)
    assert grant.issued_at_us == WALL_BASE_US
    clock.advance_wall(-3_600.0)
    clock.advance_wall(7_200.0)
    settled_wall_us = WALL_BASE_US + 3_600 * 1_000_000
    spy = DomainMutationSpy()
    outcome = run(owned, grant=grant, context=context, mutate=spy, clock=clock)
    assert spy.calls == 1
    assert outcome.replayed is False

    stamps = owned.connection.execute(
        "SELECT a.recorded_at_us, c.claimed_at_us, o.settled_at_us, e.recorded_at_us, "
        "e.grant_issued_at_us, e.settled_monotonic_us "
        "FROM omnivia_application_audit_events a "
        "JOIN omnivia_idempotency_claims c ON c.audit_ref = a.audit_ref "
        "JOIN omnivia_idempotency_outcomes o ON o.claim_id = c.claim_id "
        f"JOIN {EXECUTIONS_TABLE} e ON e.claim_id = c.claim_id"
    ).fetchone()
    assert stamps[:4] == (settled_wall_us,) * 4
    assert stamps[4] == WALL_BASE_US
    # The proof of expiry is monotonic on both sides, and is nowhere near the wall value.
    assert stamps[5] == int(MONOTONIC_BASE * 1_000_000)
    assert stamps[5] < grant.expires_monotonic_us


def test_v06_5_s0_replay_verifies_digest_and_ijson(owned: m1.Owned) -> None:
    """A replay proves the stored answer rather than trusting it.

    The bytes have been at rest, so "we wrote them canonically" is worth nothing on the
    way back out. The digest is verified over the exact stored UTF-8 first, the document
    is parsed by the contract's strict parser, and the parse is re-canonicalized and
    required to equal the stored text byte for byte. Each corruption below is written by
    something other than this seam -- which is the only way any of them could occur -- and
    each is refused as a seam fault rather than served.
    """
    context = authorize()
    honest = run(
        owned, grant=issue(owned, context), context=context, mutate=DomainMutationSpy()
    )
    stored_text, stored_digest = owned.connection.execute(
        "SELECT outcome_json, outcome_digest FROM omnivia_idempotency_outcomes"
    ).fetchone()
    assert json.loads(str(stored_text)) == honest.result
    assert str(stored_digest) == "sha256:" + hashlib.sha256(
        str(stored_text).encode("utf-8")
    ).hexdigest()

    # `digest` is `None` where the row is self-consistent -- the digest is recomputed
    # over the stored bytes -- so every case but the first is one a digest check alone
    # would wave through.
    for case, text, digest in (
        # Canonical bytes under a digest that is not theirs: caught before anything
        # decodes them, which is why the digest is verified first rather than after.
        (
            "digest_mismatch",
            str(stored_text),
            "sha256:" + hashlib.sha256(b"some other answer").hexdigest(),
        ),
        # Digest consistent with the bytes, and the bytes are not the canonical form.
        ("whitespace", '{"approved": true, "record_id": "mem-s0-created"}', None),
        ("member_order", '{"record_id":"mem-s0-created","approved":true}', None),
        ("number_form", '{"approved":true,"record_id":1.0e2}', None),
        # Refused by the strict parser, before canonicalization is even reached.
        ("duplicate_key", '{"approved":true,"approved":false}', None),
        ("nan", '{"approved":NaN}', None),
        # Refused by the I-JSON domain: not representable losslessly as binary64.
        ("lossy_integer", '{"approved":12345678901234567890}', None),
        # Refused by canonicalization: a lone surrogate has no canonical escaping.
        ("lone_surrogate", '{"approved":"\\ud800"}', None),
        # A document that is valid, canonical JSON and still not an operation result.
        ("not_an_object", '[{"approved":true}]', None),
    ):
        key = f"s0-idem-{case}"
        seed_settled_claim(owned, key=key, outcome_json=text, digest=digest)
        settled = counts(owned.connection)
        equivalence = equivalence_for(idempotency_key=key)
        spy = DomainMutationSpy(memory_id=f"mem-s0-{case}")
        with pytest.raises(MutationSeamFault) as fault:
            run(
                owned,
                grant=issue(
                    owned, authorize(idempotency_key=key), equivalence=equivalence
                ),
                context=authorize(idempotency_key=key),
                mutate=spy,
                equivalence=equivalence,
            )
        assert fault.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE, case
        # No domain code ran, and the refusal rolled back -- so the fresh grant the
        # replay was offered is not spent either.
        assert spy.calls == 0, case
        assert counts(owned.connection) == settled, case

    # A claim whose stored bytes *are* the canonical document their digest was taken
    # over replays normally, so every refusal above is about the bytes rather than about
    # a replay path that stopped working.
    key = "s0-idem-honest"
    seed_settled_claim(owned, key=key, outcome_json=str(stored_text))
    equivalence = equivalence_for(idempotency_key=key)
    replay_spy = DomainMutationSpy()
    replayed = run(
        owned,
        grant=issue(owned, authorize(idempotency_key=key), equivalence=equivalence),
        context=authorize(idempotency_key=key),
        mutate=replay_spy,
        equivalence=equivalence,
    )
    assert replay_spy.calls == 0
    assert replayed.replayed is True
    assert replayed.result == honest.result

    # And the validator is consulted on the way out, not only on the way in: a stored
    # answer this build will no longer serve is refused rather than re-served.
    rejecting_spy = DomainMutationSpy()
    with pytest.raises(MutationSeamFault):
        run(
            owned,
            grant=issue(owned, authorize()),
            context=authorize(),
            mutate=rejecting_spy,
            validate_result=lambda _result: False,
        )
    assert rejecting_spy.calls == 0


def seed_settled_claim(
    holder: m1.Owned,
    *,
    key: str,
    outcome_json: str,
    digest: str | None = None,
    outcome_audit_ref: str | None = None,
) -> None:
    """A settled idempotency claim in its own scope, carrying exactly these bytes.

    Written through the same fenced INSERT path the seam itself uses -- 0007's tables are
    append-only but not append-nothing, so no guard is dropped and no second connection
    is opened. What this buys is a claim whose stored outcome is whatever the case under
    test needs it to be: the seam has no code path that would write these documents, which
    is the point, and a replay must still prove the bytes rather than assume them.

    `digest` defaults to the digest of `outcome_json`, so the row is self-consistent and
    only the document is wrong -- the case a digest check alone cannot catch.

    `outcome_audit_ref` defaults to the audit event this claim itself names, which is the
    only link the seam ever writes. Naming another mutation's audit event is the
    cross-linked row 0013's outcome trigger refuses and `_replay` refuses again; the three
    inserts share one fenced transaction, so a refused seeding leaves nothing behind.
    """
    if digest is None:
        digest = "sha256:" + hashlib.sha256(outcome_json.encode("utf-8")).hexdigest()
    audit_ref, claim_id, outcome_id = f"aud-{key}", f"clm-{key}", f"out-{key}"
    with guarded(holder):
        holder.connection.execute(
            "INSERT INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, 'req-s0-1', 'cor-s0-1', 'trc-s0-1', '{}', "
            "'succeeded', NULL, ?)",
            (audit_ref, WORKSPACE_ID, PRINCIPAL, OPERATION, PURPOSE, WALL_BASE_US),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_idempotency_claims "
            "(claim_id, workspace_id, principal_id, operation, idempotency_key, "
            "request_digest, audit_ref, claimed_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                WORKSPACE_ID,
                PRINCIPAL,
                OPERATION,
                key,
                # The contract's own fingerprint for this request, so the replay the test
                # provokes is an honest one rather than an idempotency conflict.
                equivalence_for(idempotency_key=key).fingerprint,
                audit_ref,
                WALL_BASE_US,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_idempotency_outcomes "
            "(outcome_id, claim_id, workspace_id, outcome_branch, error_code, "
            "outcome_json, outcome_reference, outcome_digest, audit_ref, settled_at_us) "
            "VALUES (?, ?, ?, 'success', NULL, ?, NULL, ?, ?, ?)",
            (
                outcome_id,
                claim_id,
                WORKSPACE_ID,
                outcome_json,
                digest,
                audit_ref if outcome_audit_ref is None else outcome_audit_ref,
                WALL_BASE_US,
            ),
        )


#: The canonical bytes one honest `candidate.approve` settles on, written literally so a
#: seeded outcome is the same document the seam would have stored.
CANONICAL_OUTCOME = '{"approved":true,"record_id":"' + CREATED_MEMORY_ID + '"}'


def test_v06_5_s0_replay_refuses_a_cross_linked_outcome(
    owned: m1.Owned, tmp_path: Path
) -> None:
    """An outcome may only carry the audit event its own claim named.

    0007 foreign-keys `omnivia_idempotency_outcomes.claim_id` and `.audit_ref`
    independently, by workspace, and never requires the two to agree -- so claim A's
    answer stored under mutation B's audit event satisfies every constraint 0007 states,
    while describing an execution that never happened. 0013's INSERT trigger refuses that
    row structurally from the upgrade onwards, and `_replay` refuses one already at rest
    when the upgrade landed, which is the only way such a row can exist at all.

    Both halves are exercised, and both against a pair of audit events whose authority is
    *identical* -- same workspace, principal, operation and purpose -- so nothing but the
    link itself distinguishes the halves being crossed.
    """
    context = authorize()
    honest = run(
        owned, grant=issue(owned, context), context=context, mutate=DomainMutationSpy()
    )
    stored_text = owned.connection.execute(
        "SELECT outcome_json FROM omnivia_idempotency_outcomes"
    ).fetchone()[0]
    assert str(stored_text) == CANONICAL_OUTCOME

    # --- structural: with 0013 active the cross-linked row cannot be written ----
    before = counts(owned.connection)
    with pytest.raises(sqlite3.DatabaseError) as refused:
        seed_settled_claim(
            owned,
            key="s0-idem-outcome-crossed",
            outcome_json=CANONICAL_OUTCOME,
            outcome_audit_ref=honest.audit_ref,
        )
    assert "omnivia:" in str(refused.value)
    assert counts(owned.connection) == before
    # The control: the same three rows, self-linked, are admitted -- so the refusal is
    # about the crossing rather than about seeding a claim at all.
    seed_settled_claim(
        owned, key="s0-idem-outcome-linked", outcome_json=CANONICAL_OUTCOME
    )
    assert foreign_key_check(owned.connection) == []

    # --- runtime: a row that predates the trigger is refused by `_replay` -------
    # Seeded at 0012, where the trigger does not exist yet, then migrated forward. This
    # is the only way the row can be present, and it is exactly how a workspace written
    # by a pre-0013 build arrives at this seam.
    path = tmp_path / "legacy-crosslink.sqlite"
    materialise_phase0_baseline(path)
    _apply_through_predecessor(path)

    legacy = m1.take_ownership(path)
    try:
        assert "omnivia_guard_idempotency_outcomes_audit_link" not in trigger_names(
            legacy.connection
        )
        seed_record(legacy)
        seed_settled_claim(legacy, key="s0-legacy-other", outcome_json=CANONICAL_OUTCOME)
        seed_settled_claim(
            legacy,
            key="s0-legacy-crossed",
            outcome_json=CANONICAL_OUTCOME,
            outcome_audit_ref="aud-s0-legacy-other",
        )
    finally:
        legacy.connection.close()

    upgraded = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(upgraded)
        assert state is not None
        applied = apply_pending_migrations(
            upgraded,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m1.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        assert [m.version for m in applied] == [MIGRATION_VERSION]
    finally:
        upgraded.close()

    holder = m1.take_ownership(path)
    try:
        # The row is honest in every way but its link: the digest is the digest of the
        # stored bytes, the bytes are the canonical document, and the two audit events
        # carry the same authority, so only the crossing is left to refuse.
        crossed = holder.connection.execute(
            "SELECT o.outcome_json, o.outcome_digest, o.audit_ref, c.audit_ref "
            "FROM omnivia_idempotency_outcomes o "
            "JOIN omnivia_idempotency_claims c ON c.claim_id = o.claim_id "
            "WHERE o.claim_id = 'clm-s0-legacy-crossed'"
        ).fetchone()
        assert str(crossed[0]) == CANONICAL_OUTCOME
        assert str(crossed[1]) == "sha256:" + hashlib.sha256(
            CANONICAL_OUTCOME.encode("utf-8")
        ).hexdigest()
        assert (str(crossed[2]), str(crossed[3])) == (
            "aud-s0-legacy-other",
            "aud-s0-legacy-crossed",
        )
        authorities = holder.connection.execute(
            "SELECT DISTINCT workspace_id, principal_id, operation, purpose "
            "FROM omnivia_application_audit_events "
            "WHERE audit_ref IN ('aud-s0-legacy-other', 'aud-s0-legacy-crossed')"
        ).fetchall()
        assert authorities == [(WORKSPACE_ID, PRINCIPAL, OPERATION, PURPOSE)]

        baseline = counts(holder.connection)
        crossed_key = "s0-legacy-crossed"
        crossed_equivalence = equivalence_for(idempotency_key=crossed_key)
        spy = DomainMutationSpy()
        with pytest.raises(MutationSeamFault) as fault:
            run(
                holder,
                grant=issue(
                    holder,
                    authorize(idempotency_key=crossed_key),
                    equivalence=crossed_equivalence,
                ),
                context=authorize(idempotency_key=crossed_key),
                mutate=spy,
                equivalence=crossed_equivalence,
            )
        assert fault.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
        # No domain code ran and the refusal rolled back, so the grant is unspent too.
        assert spy.calls == 0
        assert counts(holder.connection) == baseline

        # And the self-linked legacy claim in the same database still replays honestly,
        # so the refusal above is about the crossing rather than about legacy rows.
        other_key = "s0-legacy-other"
        other_equivalence = equivalence_for(idempotency_key=other_key)
        other_spy = DomainMutationSpy()
        replayed = run(
            holder,
            grant=issue(
                holder,
                authorize(idempotency_key=other_key),
                equivalence=other_equivalence,
            ),
            context=authorize(idempotency_key=other_key),
            mutate=other_spy,
            equivalence=other_equivalence,
        )
        assert other_spy.calls == 0
        assert replayed.replayed is True
        assert replayed.audit_ref == "aud-s0-legacy-other"
        assert replayed.result == {"approved": True, "record_id": CREATED_MEMORY_ID}
    finally:
        holder.connection.close()


def test_v06_5_s0_execution_links_cannot_contradict_authority(owned: m1.Owned) -> None:
    """An execution row cannot claim authority its claim and audit event did not carry.

    The composite foreign keys prove the claim and the audit event exist in this row's
    workspace and stop there: a row naming a real claim while stating some other
    principal, operation or purpose passes every one of them, and reads as internally
    consistent to anyone who does not join across the tables. The INSERT trigger refuses
    it, so this table's account of who did what agrees with 0007's by construction.
    """
    context = authorize()
    outcome = run(
        owned, grant=issue(owned, context), context=context, mutate=DomainMutationSpy()
    )
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 1

    honest: dict[str, Any] = {
        "execution_id": "exe-s0-link",
        "workspace_id": WORKSPACE_ID,
        "principal_id": PRINCIPAL,
        "operation": OPERATION,
        "purpose": PURPOSE,
        "grant_id": "mgr-s0-link",
        "required_role": ROLE,
        "scopes_json": "[]",
        "capabilities_json": "[]",
        "execution_kind": "replayed",
        "fencing_generation": owned.generation,
        "grant_issued_at_us": WALL_BASE_US,
        "grant_issued_monotonic_us": 1,
        "grant_expires_monotonic_us": 3,
        "settled_monotonic_us": 2,
        "claim_id": outcome.claim_id,
        "audit_ref": outcome.audit_ref,
        "recorded_at_us": WALL_BASE_US,
    }

    def insert(**changes: Any) -> None:
        row = {**honest, **changes}
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        with guarded(owned):
            owned.connection.execute(
                f"INSERT INTO {EXECUTIONS_TABLE} ({columns}) VALUES ({placeholders})", row
            )

    # Each of these names the real claim and the real audit event, and contradicts one
    # field of the authority they recorded.
    for case, changes in (
        ("principal", {"principal_id": "principal-elsewhere"}),
        ("operation", {"operation": "record.supersede"}),
        ("purpose", {"purpose": "job_control"}),
        ("workspace", {"workspace_id": "ws-somewhere-else"}),
    ):
        with pytest.raises(sqlite3.DatabaseError) as refused:
            insert(execution_id=f"exe-s0-{case}", grant_id=f"mgr-s0-{case}", **changes)
        assert "omnivia:" in str(refused.value), case
        assert m1.count(owned.connection, EXECUTIONS_TABLE) == 1, case

    # A row naming an audit event that does not exist at all is refused too -- by the
    # foreign key this time, which is the half the trigger does not have to repeat.
    with pytest.raises(sqlite3.DatabaseError):
        insert(execution_id="exe-s0-absent", grant_id="mgr-s0-absent", audit_ref="aud-nope")
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 1

    # The control: the same row, agreeing with its claim and audit event, is admitted.
    insert()
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 2
    assert foreign_key_check(owned.connection) == []

    # Agreement on the authority fields is not enough on its own: two mutations by the
    # same principal, for the same operation and purpose, in the same workspace record
    # two claims and two audit events that every field-level check above accepts
    # interchangeably. A second honest mutation under a different idempotency key is
    # exactly that pair.
    second_key = "s0-idem-crosslink"
    second_context = authorize(idempotency_key=second_key)
    second_equivalence = equivalence_for(idempotency_key=second_key)
    second = run(
        owned,
        grant=issue(owned, second_context, equivalence=second_equivalence),
        context=second_context,
        mutate=DomainMutationSpy(memory_id="mem-s0-crosslink"),
        equivalence=second_equivalence,
    )
    assert second.claim_id != outcome.claim_id
    assert second.audit_ref != outcome.audit_ref
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 3

    # The two authorities really are identical, so nothing but the link itself
    # distinguishes the halves being crossed below.
    authorities = owned.connection.execute(
        "SELECT DISTINCT a.workspace_id, a.principal_id, a.operation, a.purpose "
        "FROM omnivia_application_audit_events a WHERE a.audit_ref IN (?, ?)",
        (outcome.audit_ref, second.audit_ref),
    ).fetchall()
    assert authorities == [(WORKSPACE_ID, PRINCIPAL, OPERATION, PURPOSE)]

    # Claim A with audit event B, and B with A: both halves are real, both agree with
    # the row on every authority field, and neither combination ever happened.
    for case, claim_id, audit_ref in (
        ("a-claim-b-audit", outcome.claim_id, second.audit_ref),
        ("b-claim-a-audit", second.claim_id, outcome.audit_ref),
    ):
        with pytest.raises(sqlite3.DatabaseError) as crossed:
            insert(
                execution_id=f"exe-s0-{case}",
                grant_id=f"mgr-s0-{case}",
                claim_id=claim_id,
                audit_ref=audit_ref,
            )
        assert "omnivia:" in str(crossed.value), case
        assert m1.count(owned.connection, EXECUTIONS_TABLE) == 3, case

    # And the second mutation's own pair, kept together, is admitted -- so the refusals
    # above are about the crossing rather than about either half.
    insert(
        execution_id="exe-s0-second-link",
        grant_id="mgr-s0-second-link",
        claim_id=second.claim_id,
        audit_ref=second.audit_ref,
    )
    assert m1.count(owned.connection, EXECUTIONS_TABLE) == 4
    assert foreign_key_check(owned.connection) == []


# --- CP-S0-W: the catalogue decides whether there is a precondition at all ------


def test_v06_5_s0_precondition_is_optional_exactly_where_the_catalogue_says(
    owned: m1.Owned,
) -> None:
    """A no-precondition operation needs no reader; a required one cannot omit it.

    `memory.create` supports no mutation precondition, so there is nothing to bind and
    nothing to read, and the seam must serve it without one rather than demanding a
    version the contract forbids the request from stating. `candidate.approve` requires
    one, and the seam must refuse to run it without a reader rather than mutate against
    whatever the record happens to be. Both directions fail closed: a call site whose
    shape contradicts the catalogue is refused either way.
    """
    entry = get_operation_metadata("memory.create")
    assert entry.precondition.required is False
    assert entry.precondition.supports_mutation_precondition is False
    assert ENTRY.precondition.required is True

    session = session_for(entry)
    context = authorize(entry, session=session)
    equivalence = equivalence_for(entry)
    assert context.mutation_precondition is None

    grant = issue(owned, context, session=session, equivalence=equivalence)
    assert grant.required_record_version is None

    spy = DomainMutationSpy(memory_id="mem-s0-no-precondition")
    outcome = run(
        owned,
        grant=grant,
        context=context,
        mutate=spy,
        equivalence=equivalence,
        precondition=None,
    )
    assert spy.calls == 1
    assert outcome.replayed is False
    stored = owned.connection.execute(
        f"SELECT operation FROM {EXECUTIONS_TABLE}"
    ).fetchone()
    assert stored == ("memory.create",)
    settled = counts(owned.connection)

    # Offering a reader for an operation the catalogue says has no precondition is a
    # refusal, not a courtesy: the seam will not read a version nothing declared.
    unwanted = DomainMutationSpy(memory_id="mem-s0-unwanted-reader")
    with pytest.raises(MutationDenied) as denied:
        run(
            owned,
            grant=issue(
                owned,
                authorize(entry, session=session),
                session=session,
                equivalence=equivalence,
            ),
            context=authorize(entry, session=session),
            mutate=unwanted,
            equivalence=equivalence,
            precondition=read_record_version,
        )
    assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert unwanted.calls == 0
    assert counts(owned.connection) == settled

    # And the operation that does require one cannot be run without a reader.
    approve = authorize()
    missing = DomainMutationSpy(memory_id="mem-s0-missing-reader")
    with pytest.raises(MutationDenied) as denied:
        run(
            owned,
            grant=issue(owned, approve),
            context=approve,
            mutate=missing,
            precondition=None,
        )
    assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert missing.calls == 0
    assert counts(owned.connection) == settled

    # `workspace.create` is still refused here, reader or no reader: this lane has no
    # installation authority and registers nothing.
    create = get_operation_metadata("workspace.create")
    admin = session_for(create)
    with pytest.raises(MutationDenied):
        issue(
            owned,
            authorize(create, session=admin),
            session=admin,
            equivalence=equivalence_for(create),
        )
    assert "workspace.create" not in build_application_registry().operations


def test_v06_5_s0_result_validator_is_required_and_rolls_back(owned: m1.Owned) -> None:
    """The server states what a result may be, and an invalid one settles nothing."""
    parameters = inspect.signature(execute_mutation).parameters
    assert parameters["validate_result"].default is inspect.Parameter.empty

    context = authorize()
    before = counts(owned.connection)

    def approved_shape(result: Mapping[str, Any]) -> bool:
        return set(result) == {"approved", "record_id"} and result["approved"] is True

    wrong = DomainMutationSpy(memory_id="mem-s0-wrong-shape")
    wrong.result = {"approved": True, "record_id": CREATED_MEMORY_ID, "extra": 1}
    with pytest.raises(MutationSeamFault) as fault:
        run(
            owned,
            grant=issue(owned, context),
            context=context,
            mutate=wrong,
            validate_result=approved_shape,
        )
    assert fault.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    # The mutation ran and the whole transaction went back, domain row included.
    assert wrong.calls == 1
    assert counts(owned.connection) == before
    assert owned.connection.in_transaction is False

    spy = DomainMutationSpy()
    outcome = run(
        owned,
        grant=issue(owned, context),
        context=context,
        mutate=spy,
        validate_result=approved_shape,
    )
    assert outcome.replayed is False
    assert outcome.result == spy.result


# `json` is imported for the canonical round trip the replay path relies on; assert the
# assumption here rather than leaving it implicit in `_replay`.
def test_v06_5_s0_stored_outcomes_round_trip_canonically(owned: m1.Owned) -> None:
    context = authorize()
    outcome = run(
        owned, grant=issue(owned, context), context=context, mutate=DomainMutationSpy()
    )
    stored = owned.connection.execute(
        "SELECT outcome_json, outcome_digest FROM omnivia_idempotency_outcomes"
    ).fetchone()
    assert json.loads(str(stored[0])) == outcome.result
    assert str(stored[1]).startswith("sha256:")
    assert len(str(stored[1])) == 71
