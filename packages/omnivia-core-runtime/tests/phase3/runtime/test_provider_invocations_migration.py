"""Acceptance for migration 0028's durable Provider Invocation foundation.

What 0028 is: a unique consecutive successor to 0027, pinned by content checksum,
whose objects are exactly four append-only tables, four named indexes and twelve
statement triggers. One logical Provider Invocation, its ordered transport attempts,
the one complete terminal route evidence fact, and a lifecycle that is a chain of
append-only events rather than a column somebody overwrites.

The invariants checked here are the ones the F2a `ProviderInvocationRecord` fixtures
in `contracts/chat/v1` state: a `requested` invocation may have no transport attempt
and every other lifecycle state requires one, attempts are contiguous oldest-first and
carry a per-invocation unique provider attempt identity, a terminal state requires a
terminal time and complete route evidence, `indeterminate` requires a reconciliation
state and forbids route evidence, and the three route decisions agree with the routes
and the retry count they are recorded beside.

What it is not: no adapter, no transport, no repository, no service and no renderer.
Nothing here invokes a Provider; it only records that one was invoked.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 28
PREDECESSOR_VERSION = 27
MIGRATION_NAME = "0028_provider_invocations.sql"
WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = 2_400_000_000_000_000

INVOCATIONS = "omnivia_provider_invocations"
ATTEMPTS = "omnivia_provider_invocation_attempts"
EVIDENCE = "omnivia_provider_invocation_route_evidence"
LIFECYCLE = "omnivia_provider_invocation_lifecycle_events"

TABLES = (INVOCATIONS, ATTEMPTS, EVIDENCE, LIFECYCLE)

INDEXES = {
    "omnivia_idx_provider_invocations_job",
    "omnivia_idx_provider_invocations_generation_attempt",
    "omnivia_idx_provider_invocation_attempts_identity",
    "omnivia_idx_provider_invocation_lifecycle_events_state",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

#: Nothing this family stores may be named for a thing it must never hold.
FORBIDDEN_STORAGE_NAMES = (
    "credential",
    "secret",
    "sdk",
    "url",
    "header",
    "body",
    "prompt",
    "renderer",
)

INVOCATION_ID = "invocation-rec-02"
CONVERSATION_ID = "conv-0001"
JOB_ID = "job-0002"
GENERATION_ATTEMPT_ID = "attempt-0002"
CONNECTION_ID = "conn-primary-anthropic"
MODEL_ID = "claude-sonnet-5"
PROVIDER_ATTEMPT_ID = "attempt-0002a"
FALLBACK_CONNECTION_ID = "conn-fallback-openai"
FALLBACK_MODEL_ID = "gpt-4.1"

USAGE_JSON = '{"reported":{"inputTokens":100,"outputTokens":50,"totalTokens":150}}'


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(holder: m1.Owned, table: str, row: dict[str, object]) -> None:
    holder.connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def invocation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "invocation_id": INVOCATION_ID,
        "conversation_id": CONVERSATION_ID,
        "job_id": JOB_ID,
        "generation_attempt_id": GENERATION_ATTEMPT_ID,
        "operation": "language.stream",
        "configured_connection_id": CONNECTION_ID,
        "configured_model_id": MODEL_ID,
        "created_at_us": BASE_US,
    }
    values.update(overrides)
    return values


def attempt_row(sequence: int = 1, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "invocation_id": INVOCATION_ID,
        "attempt_sequence": sequence,
        "provider_attempt_id": f"{PROVIDER_ATTEMPT_ID}-{sequence}",
        "admitted_at_us": BASE_US + sequence,
    }
    values.update(overrides)
    return values


def evidence_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "invocation_id": INVOCATION_ID,
        "configured_connection_id": CONNECTION_ID,
        "configured_model_id": MODEL_ID,
        "admitted_connection_id": CONNECTION_ID,
        "admitted_model_id": MODEL_ID,
        "adapter_name": "anthropic-adapter",
        "adapter_version": "1.4.0",
        "route_decision": "configured",
        "same_route_retry_count": 0,
        "fallback_authorised": 0,
        "attempt_started_at_us": BASE_US + 1,
        "attempt_ended_at_us": BASE_US + 10,
        "terminal_reason": "stop",
        "usage_json": USAGE_JSON,
        "estimated_cost_json": None,
        "reconciliation_state": "reconciled",
        "recorded_at_us": BASE_US + 10,
    }
    values.update(overrides)
    return values


def lifecycle_row(
    sequence: int,
    from_state: str | None,
    to_state: str,
    **overrides: object,
) -> dict[str, object]:
    terminal = to_state in ("succeeded", "failed", "cancelled")
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "invocation_id": INVOCATION_ID,
        "event_sequence": sequence,
        "from_state": from_state,
        "to_state": to_state,
        "terminal_at_us": BASE_US + 10 if terminal else None,
        "reconciliation_state": (
            "pending_reconciliation" if to_state == "indeterminate" else None
        ),
        "occurred_at_us": BASE_US + sequence,
    }
    values.update(overrides)
    return values


def seed_requested(holder: m1.Owned) -> None:
    with guarded(holder):
        insert(holder, INVOCATIONS, invocation_row())
        insert(holder, LIFECYCLE, lifecycle_row(1, None, "requested"))


def seed_in_progress(holder: m1.Owned) -> None:
    seed_requested(holder)
    with guarded(holder):
        insert(holder, ATTEMPTS, attempt_row())
        insert(holder, LIFECYCLE, lifecycle_row(2, "requested", "in_progress"))


def seed_every_table(holder: m1.Owned) -> None:
    seed_in_progress(holder)
    with guarded(holder):
        insert(holder, EVIDENCE, evidence_row())
        insert(holder, LIFECYCLE, lifecycle_row(3, "in_progress", "succeeded"))


def test_0028_is_the_unique_consecutive_successor_to_0027() -> None:
    versions = [migration.version for migration in load_migrations()]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_the_ledger_records_this_exact_migration_text(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        recorded = applied_migrations(connection)
    finally:
        connection.close()
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_schema_inventory_contains_the_expected_new_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        named = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= named["table"]
        assert INDEXES <= named["index"]
        assert TRIGGERS <= named["trigger"]
        assert len(TRIGGERS) == 12
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_no_new_object_is_named_for_something_it_must_never_hold(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = list(TABLES)
        for table in TABLES:
            names.extend(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
    finally:
        connection.close()

    offending = [
        (name, forbidden)
        for name in names
        for forbidden in FORBIDDEN_STORAGE_NAMES
        if forbidden in name.lower()
    ]
    assert offending == []


def test_integrity_and_foreign_keys_stay_clean_once_populated(
    owned: m1.Owned,
) -> None:
    seed_every_table(owned)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


@pytest.mark.parametrize("table", TABLES)
def test_inserts_require_the_fenced_owner(owned: m1.Owned, table: str) -> None:
    row: dict[str, object]
    if table == INVOCATIONS:
        row = invocation_row()
    elif table == ATTEMPTS:
        seed_requested(owned)
        row = attempt_row()
    elif table == EVIDENCE:
        seed_in_progress(owned)
        row = evidence_row()
    else:
        with guarded(owned):
            insert(owned, INVOCATIONS, invocation_row())
        row = lifecycle_row(1, None, "requested")

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        insert(owned, table, row)


def test_a_requested_invocation_may_have_no_transport_attempt(owned: m1.Owned) -> None:
    seed_requested(owned)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {ATTEMPTS}").fetchone() == (
        0,
    )
    assert owned.connection.execute(
        f"SELECT to_state FROM {LIFECYCLE} WHERE event_sequence = 1"
    ).fetchone() == ("requested",)


def test_a_non_requested_state_before_any_attempt_is_refused(owned: m1.Owned) -> None:
    seed_requested(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="attempt"):
        insert(owned, LIFECYCLE, lifecycle_row(2, "requested", "in_progress"))


def test_transport_attempts_must_be_contiguous_oldest_first(owned: m1.Owned) -> None:
    seed_requested(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(owned, ATTEMPTS, attempt_row(2))
    with guarded(owned):
        insert(owned, ATTEMPTS, attempt_row(1))
        insert(owned, ATTEMPTS, attempt_row(2))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(owned, ATTEMPTS, attempt_row(4))


def test_a_provider_attempt_identity_is_unique_within_its_invocation(
    owned: m1.Owned,
) -> None:
    seed_requested(owned)
    with guarded(owned):
        insert(owned, ATTEMPTS, attempt_row(1))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(
            owned,
            ATTEMPTS,
            attempt_row(2, provider_attempt_id=f"{PROVIDER_ATTEMPT_ID}-1"),
        )


def test_a_terminal_state_requires_terminal_time_and_route_evidence(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    with guarded(owned):
        insert(owned, EVIDENCE, evidence_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(3, "in_progress", "succeeded", terminal_at_us=None),
        )

    no_evidence_id = "invocation-no-evidence"
    with guarded(owned):
        insert(owned, INVOCATIONS, invocation_row(invocation_id=no_evidence_id))
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(1, None, "requested", invocation_id=no_evidence_id),
        )
        insert(owned, ATTEMPTS, attempt_row(invocation_id=no_evidence_id))
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(2, "requested", "in_progress", invocation_id=no_evidence_id),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="route evidence"):
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(3, "in_progress", "succeeded", invocation_id=no_evidence_id),
        )

    with guarded(owned):
        insert(owned, LIFECYCLE, lifecycle_row(3, "in_progress", "succeeded"))
    assert owned.connection.execute(
        f"SELECT terminal_at_us FROM {LIFECYCLE} WHERE event_sequence = 3"
    ).fetchone() == (BASE_US + 10,)


def test_a_non_terminal_state_carries_no_terminal_time(owned: m1.Owned) -> None:
    seed_in_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(3, "in_progress", "indeterminate", terminal_at_us=BASE_US),
        )


def test_configured_route_evidence_must_match_the_route_it_names(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            EVIDENCE,
            evidence_row(
                admitted_connection_id=FALLBACK_CONNECTION_ID,
                admitted_model_id=FALLBACK_MODEL_ID,
                adapter_name="openai-adapter",
                adapter_version="3.2.0",
            ),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="configured"):
        insert(
            owned,
            EVIDENCE,
            evidence_row(
                configured_connection_id=FALLBACK_CONNECTION_ID,
                admitted_connection_id=FALLBACK_CONNECTION_ID,
            ),
        )


def test_a_same_route_retry_keeps_its_route_and_counts_its_retries(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            EVIDENCE,
            evidence_row(route_decision="same_route_retry", same_route_retry_count=0),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="retry count"):
        insert(
            owned,
            EVIDENCE,
            evidence_row(route_decision="same_route_retry", same_route_retry_count=1),
        )

    with guarded(owned):
        insert(owned, ATTEMPTS, attempt_row(2, admitted_at_us=BASE_US + 2))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            EVIDENCE,
            evidence_row(
                route_decision="same_route_retry",
                same_route_retry_count=1,
                admitted_model_id=FALLBACK_MODEL_ID,
            ),
        )

    with guarded(owned):
        insert(
            owned,
            EVIDENCE,
            evidence_row(route_decision="same_route_retry", same_route_retry_count=1),
        )
    assert owned.connection.execute(
        f"SELECT admitted_connection_id, admitted_model_id, same_route_retry_count "
        f"FROM {EVIDENCE}"
    ).fetchone() == (CONNECTION_ID, MODEL_ID, 1)


def test_a_fallback_requires_an_authorised_and_different_route(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    fallback = evidence_row(
        route_decision="fallback",
        fallback_authorised=1,
        admitted_connection_id=FALLBACK_CONNECTION_ID,
        admitted_model_id=FALLBACK_MODEL_ID,
        adapter_name="openai-adapter",
        adapter_version="3.2.0",
    )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, EVIDENCE, {**fallback, "fallback_authorised": 0})
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            EVIDENCE,
            {
                **fallback,
                "admitted_connection_id": CONNECTION_ID,
                "admitted_model_id": MODEL_ID,
            },
        )

    with guarded(owned):
        insert(owned, EVIDENCE, fallback)
    assert owned.connection.execute(
        f"SELECT fallback_authorised, admitted_connection_id FROM {EVIDENCE}"
    ).fetchone() == (1, FALLBACK_CONNECTION_ID)


def test_indeterminate_requires_reconciliation_and_forbids_route_evidence(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(
                3, "in_progress", "indeterminate", reconciliation_state=None
            ),
        )

    with guarded(owned):
        insert(owned, LIFECYCLE, lifecycle_row(3, "in_progress", "indeterminate"))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="indeterminate"):
        insert(owned, EVIDENCE, evidence_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, LIFECYCLE, lifecycle_row(4, "indeterminate", "in_progress"))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="closed"):
        insert(owned, ATTEMPTS, attempt_row(2))


def test_an_invocation_holding_route_evidence_is_never_indeterminate(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    with guarded(owned):
        insert(owned, EVIDENCE, evidence_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="indeterminate"):
        insert(owned, LIFECYCLE, lifecycle_row(3, "in_progress", "indeterminate"))


def test_lifecycle_events_must_continue_the_current_state(owned: m1.Owned) -> None:
    seed_in_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="current state"):
        insert(owned, LIFECYCLE, lifecycle_row(4, "in_progress", "indeterminate"))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="current state"):
        insert(owned, LIFECYCLE, lifecycle_row(3, "requested", "indeterminate"))


def test_a_terminal_invocation_admits_no_further_lifecycle_event(
    owned: m1.Owned,
) -> None:
    seed_every_table(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, LIFECYCLE, lifecycle_row(4, "succeeded", "failed"))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="closed"):
        insert(owned, ATTEMPTS, attempt_row(2))


def test_an_unknown_operation_or_lifecycle_vocabulary_is_refused(
    owned: m1.Owned,
) -> None:
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, INVOCATIONS, invocation_row(operation="language.generate"))
    seed_requested(owned)
    with guarded(owned):
        insert(owned, ATTEMPTS, attempt_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(owned, LIFECYCLE, lifecycle_row(2, "requested", "maybe"))


def test_route_evidence_documents_must_be_exact_canonical_json_objects(
    owned: m1.Owned,
) -> None:
    seed_in_progress(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="canonical"):
        insert(owned, EVIDENCE, evidence_row(usage_json='{"reported": {} }'))
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="canonical"):
        insert(owned, EVIDENCE, evidence_row(usage_json='["reported"]'))


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_are_refused_even_for_the_current_owner(
    owned: m1.Owned,
    table: str,
) -> None:
    seed_every_table(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")
