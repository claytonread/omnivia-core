"""Acceptance for migration 0069's Chat generation Directions and attempt continuations.

What 0069 is: a unique consecutive successor to 0068 that adds two guarded tables and
widens one. `omnivia_chat_directions` holds one durable Direction per row -- bounded
canonical content aimed at one Attempt of one Job, at most one `pending` per Job, with a
single versioned transition out of `pending` that records its own evidence. A Direction
is inserted only while the 0030 job status projection is `running` on its target Attempt,
and becomes `applied` only with a complete, matching proof: the next Attempt, its provider
invocation, a committed 0031 request manifest carrying the exact `direction` binding, the
zero-based application iteration and the `next_iteration` boundary.
`omnivia_chat_generation_continuations` is the append-only record that one Attempt
continued as the next under one applied Direction, backed by the prior Attempt's committed
`chat.generation.iteration_completed` event. `omnivia_chat_generation_events` is rebuilt so
its event-kind rules also admit that kind and `chat.direction.state_changed`, each with a
closed identity-only payload.

What it is not: any runtime persistence, a relaxation of an existing guard, or storage of
raw provider content. The rebuilt events table, its index and its three guards are pinned
against 0034's own text, so "unchanged apart from the two widened kinds and their shape
rules" is checkable.

What the database cannot prove: SQLite has no SHA-256, so `content_digest`, the manifest
`contentDigest` and `priorOutputDigest` are digest-shaped strings compared by equality.
The tests show the strongest honest layer: SQL refuses a manifest whose binding differs
from the stored digest, accepts a self-consistent but wrong digest, and the reader contract
(`verify_direction_digests`, recomputing SHA-256 over the stored UTF-8 bytes) flags it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_foundation_migration as foundation
import test_chat_text_transport_migration as t34
from omnivia_core_runtime.ownership.fencing import assert_guards_intact
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    Migration,
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 69
PREDECESSOR_VERSION = 68
MIGRATION_NAME = "0069_chat_generation_directions.sql"
EXPECTED_STATEMENT_COUNT = 17

WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = foundation.BASE_US
EVENTS = foundation.EVENTS
ATTEMPTS = foundation.ATTEMPTS
JOBS = foundation.JOBS
PROVIDER_INVOCATIONS = foundation.PROVIDER_INVOCATIONS
STATUS = "omnivia_chat_generation_job_status_projection"
MANIFESTS = "omnivia_chat_request_manifests"
DIRECTIONS = "omnivia_chat_directions"
CONTINUATIONS = "omnivia_chat_generation_continuations"
ONE_PENDING_INDEX = "omnivia_idx_chat_directions_one_pending"

TABLES = (DIRECTIONS, CONTINUATIONS)
TRIGGERS = (
    "omnivia_guard_chat_directions_insert",
    "omnivia_guard_chat_directions_update",
    "omnivia_guard_chat_directions_delete",
    "omnivia_guard_chat_generation_continuations_insert",
    "omnivia_guard_chat_generation_continuations_update",
    "omnivia_guard_chat_generation_continuations_delete",
)

ITERATION_COMPLETED = "chat.generation.iteration_completed"
DIRECTION_STATE_CHANGED = "chat.direction.state_changed"

JOB_ID = foundation.GENERATION_JOB_ID
ATTEMPT_1 = foundation.GENERATION_ATTEMPT_ID
ATTEMPT_2 = "generation-attempt-2"
ATTEMPT_3 = "generation-attempt-3"
ATTEMPT_4 = "generation-attempt-4"
OTHER_JOB_ID = "generation-job-2"
OTHER_ATTEMPT_1 = "generation-attempt-other-1"
OTHER_ATTEMPT_2 = "generation-attempt-other-2"
INVOCATIONS = {
    ATTEMPT_1: foundation.PROVIDER_INVOCATION_ID,
    ATTEMPT_2: "provider-invocation-2",
    ATTEMPT_3: "provider-invocation-3",
    ATTEMPT_4: "provider-invocation-4",
    OTHER_ATTEMPT_1: "provider-invocation-other-1",
    OTHER_ATTEMPT_2: "provider-invocation-other-2",
}
MANIFEST_IDS = {
    ATTEMPT_2: "request-manifest-2",
    ATTEMPT_3: "request-manifest-3",
    ATTEMPT_4: "request-manifest-4",
    OTHER_ATTEMPT_2: "request-manifest-other-2",
}


def sha256_digest(text: str) -> str:
    """The reader-side digest: SHA-256 over the exact UTF-8 bytes, `sha256:` + lower hex."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def direction_content(text: str) -> str:
    """The exact stored Direction content: the closed `editableParts` object, canonical JSON."""
    return json.dumps(
        {"editableParts": [{"payload": {"text": text}, "type": "text"}]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


CONTENT_JSON = direction_content("Focus on evidence.")
CONTENT_DIGEST = sha256_digest(CONTENT_JSON)
OTHER_DIGEST = sha256_digest(direction_content("Something else."))
PRIOR_DIGEST = sha256_digest("the prior iteration output, never stored in 0069")
REASON = '{"code":"direction.closed"}'


def migration_under_test() -> Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))
M0034 = t34.MIGRATION


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


def maintenance_apply(path: Path) -> list[Migration]:
    """Advance an already-bootstrapped workspace through the real migrator."""
    return t34.apply_remaining_migrations(path)


def object_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'omnivia_%'"
        )
    }


def compact(**fields: object) -> str:
    return json.dumps(fields, separators=(",", ":"))


# --- rows ---------------------------------------------------------------------


def direction_row(sequence: int = 1, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": foundation.CONVERSATION_ID,
        "branch_id": foundation.BRANCH_ID,
        "generation_job_id": JOB_ID,
        "target_attempt_id": ATTEMPT_1,
        "direction_id": f"direction-{sequence}",
        "direction_sequence": sequence,
        "actor_id": foundation.ACTOR_ID,
        "state": "pending",
        "version": 1,
        "content_json": CONTENT_JSON,
        "content_digest": CONTENT_DIGEST,
        "requested_boundary": "earliest_safe",
        "effective_boundary": None,
        "supersedes_direction_id": None,
        "superseded_by_direction_id": None,
        "applied_attempt_id": None,
        "applied_provider_invocation_id": None,
        "applied_request_manifest_id": None,
        "applied_iteration": None,
        "terminal_reason_json": None,
        "schema_version": 1,
        "created_at_us": BASE_US + 300 + sequence,
        "updated_at_us": BASE_US + 300 + sequence,
        "terminal_at_us": None,
    }
    values.update(overrides)
    return values


def invocation_row(attempt_id: str, job_id: str = JOB_ID) -> dict[str, object]:
    return foundation.provider_invocation_row(
        invocation_id=INVOCATIONS[attempt_id],
        job_id=job_id,
        generation_attempt_id=attempt_id,
    )


def attempt_row(number: int, attempt_id: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "generation_attempt_id": attempt_id,
        "attempt_number": number,
        "retry_of_attempt_id": None
        if number == 1
        else f"generation-attempt-{number - 1}",
        "provider_invocation_id": INVOCATIONS[attempt_id],
        "started_at_us": BASE_US + 200 + number,
    }
    values.update(overrides)
    return foundation.generation_attempt_row(**values)


def status_row(
    job_id: str = JOB_ID, attempt_id: str | None = ATTEMPT_1, **overrides: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": foundation.CONVERSATION_ID,
        "generation_job_id": job_id,
        "state": "running",
        "current_attempt_id": attempt_id,
        "result_message_id": None,
        "sanitized_error_code": None,
        "sanitized_error_detail": None,
        "version": 1,
        "schema_version": 1,
        "updated_at_us": BASE_US + 210,
        "finished_at_us": None,
    }
    values.update(overrides)
    return values


def move_status(
    holder: m1.Owned,
    state: str,
    attempt_id: str = ATTEMPT_1,
    *,
    job_id: str = JOB_ID,
    version: int = 2,
    **columns: object,
) -> None:
    """One guarded 0030 projection transition, version advanced by one."""
    assignments = {
        "state": state,
        "current_attempt_id": attempt_id,
        "version": version,
        "updated_at_us": BASE_US + 250,
        **columns,
    }
    with foundation.guarded(holder):
        holder.connection.execute(
            f"UPDATE {STATUS} SET "
            + ", ".join(f"{name} = ?" for name in assignments)
            + " WHERE workspace_id = ? AND generation_job_id = ?",
            (*assignments.values(), WORKSPACE_ID, job_id),
        )


FAILED = {
    "sanitized_error_code": "provider_failed",
    "finished_at_us": BASE_US + 250,
}


def binding(
    direction_id: str = "direction-1",
    version: object = 1,
    content_digest: object = CONTENT_DIGEST,
) -> dict[str, object]:
    return {
        "contentDigest": content_digest,
        "directionId": direction_id,
        "directionVersion": version,
    }


def manifest_body(direction: object = ..., **extra: object) -> str:
    """A 0031-shaped body; `direction=...` omits the key, anything else is its value."""
    body: dict[str, object] = {
        "schemaVersion": 1,
        "generationJobId": JOB_ID,
        "retention": {"class": "chat_request_manifest_v1"},
        **extra,
    }
    if direction is not ...:
        body["direction"] = direction
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def manifest_row(
    attempt_id: str = ATTEMPT_2, *, direction: object = None, **overrides: object
) -> dict[str, object]:
    body = manifest_body(binding() if direction is None else direction)
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": foundation.CONVERSATION_ID,
        "branch_id": foundation.BRANCH_ID,
        "generation_job_id": JOB_ID,
        "generation_attempt_id": attempt_id,
        "trigger_message_id": foundation.TRIGGER_MESSAGE_ID,
        "provider_invocation_id": INVOCATIONS[attempt_id],
        "request_manifest_id": MANIFEST_IDS[attempt_id],
        "idempotency_key": f"manifest-key-{attempt_id}",
        "schema_version": 1,
        "manifest_digest": sha256_digest(body),
        "request_manifest_body_json": body,
        "created_at_us": BASE_US + 350,
    }
    values.update(overrides)
    return values


def event(sequence: int, event_type: str, **kwargs: object) -> dict[str, object]:
    return foundation.generation_event_row(sequence, event_type, **kwargs)  # type: ignore[arg-type]


def iteration_event(
    sequence: int,
    attempt_id: str,
    finish_id: str,
    iteration: int,
    digest: str = PRIOR_DIGEST,
    version: int = 1,
    **overrides: object,
) -> dict[str, object]:
    return event(
        sequence,
        ITERATION_COMPLETED,
        generation_attempt_id=attempt_id,
        provider_event_id=finish_id,
        payload_json=compact(
            generationJobVersion=version, iteration=iteration, priorOutputDigest=digest
        ),
        **overrides,
    )


def continuation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": foundation.CONVERSATION_ID,
        "generation_job_id": JOB_ID,
        "continuation_id": "continuation-1",
        "prior_attempt_id": ATTEMPT_1,
        "next_attempt_id": ATTEMPT_2,
        "direction_id": "direction-1",
        "direction_version": 2,
        "provider_finish_id": "provider-finish-1",
        "prior_output_digest": PRIOR_DIGEST,
        "schema_version": 1,
        "created_at_us": BASE_US + 500,
    }
    values.update(overrides)
    return values


APPLIED = {
    "applied_attempt_id": ATTEMPT_2,
    "applied_provider_invocation_id": INVOCATIONS[ATTEMPT_2],
    "applied_request_manifest_id": MANIFEST_IDS[ATTEMPT_2],
    "applied_iteration": 1,
    "effective_boundary": "next_iteration",
}
SUPERSEDED = {
    "superseded_by_direction_id": "direction-2",
    "terminal_reason_json": REASON,
}
TERMINAL_EVIDENCE: dict[str, dict[str, object]] = {
    "applied": APPLIED,
    "superseded": SUPERSEDED,
    "rejected": {"terminal_reason_json": REASON},
    "cancelled": {"terminal_reason_json": REASON},
}


def transition(
    holder: m1.Owned,
    direction_id: str,
    state: str,
    *,
    version: int = 2,
    at: int = BASE_US + 400,
    **columns: object,
) -> None:
    """One guarded UPDATE out of `pending`; evidence defaults to the state's own."""
    evidence = {**TERMINAL_EVIDENCE.get(state, {}), **columns}
    assignments = {
        "state": state,
        "version": version,
        "updated_at_us": at,
        "terminal_at_us": at,
        **evidence,
    }
    holder.connection.execute(
        f"UPDATE {DIRECTIONS} SET "
        + ", ".join(f"{name} = ?" for name in assignments)
        + " WHERE workspace_id = ? AND direction_id = ?",
        (*assignments.values(), WORKSPACE_ID, direction_id),
    )


def seed_job(
    holder: m1.Owned,
    *,
    status: str | None = "running",
    iteration_evidence: bool = True,
) -> None:
    """A running job (attempt 1 current) with attempts 1..3, one more job, evidence events.

    Events: 2 attempt-1 iteration 0 at job version 1, 3 attempt-1 text. An iteration event
    can only be recorded while the projection runs on its own attempt, so the evidence
    event needs a running projection; any other seeding records a Direction event instead.
    """
    foundation.seed_running_generation_job(holder)
    with foundation.guarded(holder):
        for number, attempt_id in ((2, ATTEMPT_2), (3, ATTEMPT_3)):
            foundation.insert(holder, PROVIDER_INVOCATIONS, invocation_row(attempt_id))
            foundation.insert(holder, ATTEMPTS, attempt_row(number, attempt_id))
        foundation.insert(
            holder,
            JOBS,
            foundation.generation_job_row(
                generation_job_id=OTHER_JOB_ID,
                idempotency_key="generation-key-2",
                created_at_us=BASE_US + 101,
                updated_at_us=BASE_US + 101,
            ),
        )
        for number, attempt_id in ((1, OTHER_ATTEMPT_1), (2, OTHER_ATTEMPT_2)):
            foundation.insert(
                holder, PROVIDER_INVOCATIONS, invocation_row(attempt_id, OTHER_JOB_ID)
            )
            foundation.insert(
                holder,
                ATTEMPTS,
                attempt_row(
                    number,
                    attempt_id,
                    generation_job_id=OTHER_JOB_ID,
                    retry_of_attempt_id=None if number == 1 else OTHER_ATTEMPT_1,
                ),
            )
        if status is not None:
            foundation.insert(
                holder,
                STATUS,
                status_row(
                    attempt_id=None if status == "queued" else ATTEMPT_1, state=status
                ),
            )
            foundation.insert(holder, STATUS, status_row(OTHER_JOB_ID, OTHER_ATTEMPT_1))
        if iteration_evidence and status == "running":
            foundation.insert(
                holder, EVENTS, iteration_event(2, ATTEMPT_1, "provider-finish-1", 0)
            )
        else:
            foundation.insert(
                holder,
                EVENTS,
                event(2, DIRECTION_STATE_CHANGED, payload_json=state_payload()),
            )
        foundation.insert(
            holder,
            EVENTS,
            event(
                3,
                "chat.generation.text_appended",
                generation_attempt_id=ATTEMPT_1,
                provider_event_id="provider-text-1",
            ),
        )


def state_payload(
    direction_id: str = "direction-1", version: int = 2, state: str = "applied"
) -> str:
    return compact(directionId=direction_id, directionVersion=version, state=state)


def add_direction(holder: m1.Owned, sequence: int = 1, **overrides: object) -> None:
    with foundation.guarded(holder):
        foundation.insert(holder, DIRECTIONS, direction_row(sequence, **overrides))


def add_manifest(
    holder: m1.Owned, attempt_id: str = ATTEMPT_2, **kwargs: object
) -> None:
    with foundation.guarded(holder):
        foundation.insert(holder, MANIFESTS, manifest_row(attempt_id, **kwargs))


def applied_direction(holder: m1.Owned) -> None:
    """direction-1 targeting attempt 1, applied by attempt 2 with its bound manifest."""
    add_direction(holder)
    add_manifest(holder)
    with foundation.guarded(holder):
        transition(holder, "direction-1", "applied")


def terminate(
    holder: m1.Owned, state: str, sequence: int = 1, **columns: object
) -> None:
    """One committed transaction out of `pending`; supersession adds its successor in it."""
    with foundation.guarded(holder):
        transition(holder, f"direction-{sequence}", state, **columns)
        if state == "superseded":
            foundation.insert(
                holder,
                DIRECTIONS,
                direction_row(
                    sequence + 1, supersedes_direction_id=f"direction-{sequence}"
                ),
            )


def count(connection: sqlite3.Connection, table: str, where: str = "1") -> int:
    return int(
        connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    )


def verify_direction_digests(connection: sqlite3.Connection) -> list[str]:
    """The reader contract: recompute SHA-256 over stored bytes; return the mismatches."""
    return [
        str(row[0])
        for row in connection.execute(
            f"SELECT direction_id, content_json, content_digest FROM {DIRECTIONS}"
        )
        if sha256_digest(str(row[1])) != row[2]
    ]


# --- identity, upgrade and inventory ------------------------------------------


def test_0069_is_the_unique_consecutive_successor_to_0068() -> None:
    migrations = load_migrations()
    versions = [migration.version for migration in migrations]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert versions.count(MIGRATION_VERSION) == 1
    assert [m.name for m in migrations if m.version == MIGRATION_VERSION] == [
        MIGRATION_NAME
    ]
    assert MIGRATION.version == PREDECESSOR_VERSION + 1


def test_the_statement_count_is_pinned_for_failure_injection() -> None:
    assert len(MIGRATION_STATEMENTS) == EXPECTED_STATEMENT_COUNT


def test_upgrade_creates_the_tables_index_and_triggers(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        assert applied_migrations(connection)[MIGRATION_VERSION] == MIGRATION.checksum
        named = object_names(connection)
        assert {*TABLES, ONE_PENDING_INDEX, *TRIGGERS} <= named
        assert not [name for name in named if name.endswith("_0069")]
        assert_guards_intact(connection)
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        index_sql = " ".join(t34.stored_sql(connection, ONE_PENDING_INDEX).split())
        assert index_sql.endswith(
            "(workspace_id, generation_job_id) WHERE state = 'pending'"
        )
        assert index_sql.startswith("CREATE UNIQUE INDEX")
    finally:
        connection.close()


def test_the_new_tables_pin_their_constraints_in_stored_ddl(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        directions = " ".join(t34.stored_sql(connection, DIRECTIONS).split())
        for fragment in (
            "UNIQUE (workspace_id, generation_job_id, direction_sequence)",
            "UNIQUE (workspace_id, generation_job_id, direction_id, supersedes_direction_id)",
            "UNIQUE (workspace_id, generation_job_id, applied_attempt_id)",
            "UNIQUE (workspace_id, applied_request_manifest_id)",
            "CHECK (state IN ('pending', 'applied', 'superseded', 'rejected', 'cancelled'))",
            "requested_boundary = 'earliest_safe'",
            "effective_boundary = 'next_iteration'",
            "applied_iteration BETWEEN 1 AND 1000000",
            "REFERENCES omnivia_provider_invocations (workspace_id, invocation_id)",
            "REFERENCES omnivia_chat_request_manifests (workspace_id, request_manifest_id)",
            "(workspace_id, generation_job_id, superseded_by_direction_id, direction_id)",
            "DEFERRABLE INITIALLY DEFERRED",
            "BETWEEN 2 AND 16384",
            "WITHOUT ROWID",
        ):
            assert fragment in directions, fragment
        assert directions.count("DEFERRABLE INITIALLY DEFERRED") == 1
        continuations = " ".join(t34.stored_sql(connection, CONTINUATIONS).split())
        for fragment in (
            "UNIQUE (workspace_id, generation_job_id, next_attempt_id)",
            "UNIQUE (workspace_id, generation_job_id, direction_id)",
            "WITHOUT ROWID",
        ):
            assert fragment in continuations, fragment
        columns = {
            row[1]
            for table in TABLES
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        assert not [
            c for c in columns if "provider_content" in c or "raw" in c or "prompt" in c
        ]
    finally:
        connection.close()


def test_no_version_column_is_added_to_the_foundation_tables(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        for table in (JOBS, ATTEMPTS, EVENTS):
            names = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
            assert "version" not in names, table
        status = [r[1] for r in connection.execute(f"PRAGMA table_info({STATUS})")]
        assert "version" in status
    finally:
        connection.close()


def test_a_populated_0068_workspace_reaches_0069_with_every_prior_fact_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace-through-0068.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
        holder = m1.take_ownership(path)
        try:
            foundation.seed_every_table(holder)
            before = t34.event_rows(holder.connection)
            jobs_before = holder.connection.execute(f"SELECT * FROM {JOBS}").fetchall()
        finally:
            holder.connection.close()
    assert len(before) == 3

    applied = maintenance_apply(path)
    assert [m.version for m in applied] == [MIGRATION_VERSION]

    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        assert applied_migrations(connection)[MIGRATION_VERSION] == MIGRATION.checksum
        assert t34.event_rows(connection) == before
        assert connection.execute(f"SELECT * FROM {JOBS}").fetchall() == jobs_before
        for table in TABLES:
            assert count(connection, table) == 0
        assert {
            *TABLES,
            ONE_PENDING_INDEX,
            *TRIGGERS,
            *t34.EVENTS_TRIGGERS,
        } <= object_names(connection)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()

    assert maintenance_apply(path) == []


# --- generation event kinds ---------------------------------------------------

PAYLOAD_SHAPE_CHECK = """    CHECK (event_type NOT IN ('chat.generation.iteration_completed', 'chat.direction.state_changed')
           OR COALESCE(CASE WHEN json_valid(payload_json) IS NOT 1 THEN 0
                            WHEN event_type = 'chat.generation.iteration_completed' THEN
                                json_type(payload_json) = 'object'
                                AND json_type(payload_json, '$.generationJobVersion') = 'integer'
                                AND json_extract(payload_json, '$.generationJobVersion') >= 1
                                AND json_type(payload_json, '$.iteration') = 'integer'
                                AND json_extract(payload_json, '$.iteration') BETWEEN 0 AND 1000000
                                AND json_type(payload_json, '$.priorOutputDigest') = 'text'
                                AND length(json_extract(payload_json, '$.priorOutputDigest')) = 71
                                AND substr(json_extract(payload_json, '$.priorOutputDigest'), 1, 7) = 'sha256:'
                                AND substr(json_extract(payload_json, '$.priorOutputDigest'), 8) NOT GLOB '*[^0-9a-f]*'
                                AND json_remove(payload_json, '$.generationJobVersion', '$.iteration',
                                                '$.priorOutputDigest') = '{}'
                            ELSE
                                json_type(payload_json) = 'object'
                                AND json_type(payload_json, '$.directionId') = 'text'
                                AND length(json_extract(payload_json, '$.directionId')) BETWEEN 1 AND 128
                                AND json_extract(payload_json, '$.directionId') GLOB '[A-Za-z0-9]*'
                                AND json_extract(payload_json, '$.directionId') NOT GLOB '*[^A-Za-z0-9._:-]*'
                                AND json_type(payload_json, '$.directionVersion') = 'integer'
                                AND json_extract(payload_json, '$.directionVersion') BETWEEN 1 AND 1000000
                                AND json_type(payload_json, '$.state') = 'text'
                                AND json_extract(payload_json, '$.state')
                                    IN ('pending', 'applied', 'superseded', 'rejected', 'cancelled')
                                AND json_remove(payload_json, '$.directionId', '$.directionVersion', '$.state') = '{}'
                            END, 0) = 1),
"""


def test_the_events_table_is_0034s_table_plus_exactly_the_two_new_kinds(
    migrated: Path,
) -> None:
    original = t34.statement(
        M0034.sql,
        "CREATE TABLE omnivia_chat_generation_events_0034 (",
        ") WITHOUT ROWID",
    )
    old_types = "                          'chat.generation.cancelled')),"
    new_types = (
        "                          'chat.generation.cancelled',\n"
        "                          'chat.generation.iteration_completed',\n"
        "                          'chat.direction.state_changed')),"
    )
    old_shape = (
        "           OR (event_type IN ('chat.generation.failed', 'chat.generation.cancelled')\n"
        "               AND result_message_id IS NULL)),"
    )
    new_shape = (
        "           OR (event_type IN ('chat.generation.failed', 'chat.generation.cancelled')\n"
        "               AND result_message_id IS NULL)\n"
        "           OR (event_type = 'chat.generation.iteration_completed'\n"
        "               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL\n"
        "               AND provider_event_id IS NOT NULL)\n"
        "           OR (event_type = 'chat.direction.state_changed'\n"
        "               AND result_message_id IS NULL)),"
    )
    old_payload = (
        "    CHECK (typeof(payload_json) = 'text'\n"
        "           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 65536\n"
        "           AND instr(payload_json, char(0)) = 0),\n"
    )
    assert original.count(old_types) == 1 and original.count(old_shape) == 1
    assert original.count(old_payload) == 1
    expected = (
        original.replace(
            "CREATE TABLE omnivia_chat_generation_events_0034",
            'CREATE TABLE "omnivia_chat_generation_events"',
        )
        .replace(old_types, new_types)
        .replace(old_shape, new_shape)
        .replace(old_payload, old_payload + PAYLOAD_SHAPE_CHECK)
    )
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        assert t34.stored_sql(connection, EVENTS) == expected
    finally:
        connection.close()


ITERATION_VERSION_GUARD = """
    SELECT RAISE(ABORT, 'omnivia: an iteration_completed event must carry the job status projection version while it is running on the event attempt')
    WHERE NEW.event_type = 'chat.generation.iteration_completed'
      AND NEW.generation_attempt_id IS NOT NULL
      AND json_type(NEW.payload_json, '$.generationJobVersion') IS 'integer'
      AND json_extract(NEW.payload_json, '$.generationJobVersion') >= 1
      AND NOT EXISTS (
            SELECT 1 FROM omnivia_chat_generation_job_status_projection p
            WHERE p.workspace_id = NEW.workspace_id
              AND p.conversation_id = NEW.conversation_id
              AND p.generation_job_id = NEW.generation_job_id
              AND p.state = 'running'
              AND p.current_attempt_id IS NEW.generation_attempt_id
              AND p.version = json_extract(NEW.payload_json, '$.generationJobVersion'));"""


def test_the_events_index_and_three_guards_are_recreated_verbatim_from_0034(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        index = t34.statement(
            M0034.sql,
            f"CREATE INDEX IF NOT EXISTS {t34.EVENTS_INDEX}",
            "(workspace_id, generation_job_id, generation_event_sequence)",
        )
        assert t34.stored_sql(
            connection, t34.EVENTS_INDEX
        ) == t34.without_if_not_exists(index)
        for trigger in t34.EVENTS_TRIGGERS:
            expected = t34.statement(
                M0034.sql, f"CREATE TRIGGER IF NOT EXISTS {trigger}", "\nEND"
            )
            if trigger.endswith("_insert"):
                expected = expected[: -len("\nEND")] + ITERATION_VERSION_GUARD + "\nEND"
            assert t34.stored_sql(connection, trigger) == t34.without_if_not_exists(
                expected
            )
    finally:
        connection.close()


def test_the_two_new_event_kinds_are_accepted_in_sequence(owned: m1.Owned) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row())
        foundation.insert(
            owned, EVENTS, iteration_event(2, ATTEMPT_1, "provider-event-finish", 0)
        )
        foundation.insert(
            owned,
            EVENTS,
            event(3, DIRECTION_STATE_CHANGED, payload_json=state_payload()),
        )
        foundation.insert(
            owned,
            EVENTS,
            event(
                4,
                DIRECTION_STATE_CHANGED,
                generation_attempt_id=ATTEMPT_1,
                payload_json=state_payload("direction-2", 1, "pending"),
            ),
        )
        foundation.insert(
            owned,
            EVENTS,
            event(5, "chat.generation.text_appended", generation_attempt_id=ATTEMPT_1),
        )
    kinds = [
        row[0]
        for row in owned.connection.execute(
            f"SELECT event_type FROM {EVENTS} ORDER BY generation_event_sequence"
        )
    ]
    assert kinds == [
        "chat.generation.queued",
        ITERATION_COMPLETED,
        DIRECTION_STATE_CHANGED,
        DIRECTION_STATE_CHANGED,
        "chat.generation.text_appended",
    ]
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


GOOD_ITERATION = {
    "generationJobVersion": 1,
    "iteration": 0,
    "priorOutputDigest": PRIOR_DIGEST,
}
GOOD_STATE = {"directionId": "direction-1", "directionVersion": 2, "state": "applied"}
ATTEMPT_AND_FINISH = {
    "generation_attempt_id": ATTEMPT_1,
    "provider_event_id": "provider-finish-x",
}


def raw(fields: dict[str, object]) -> str:
    return json.dumps(fields, separators=(",", ":"))


@pytest.mark.parametrize(
    ("case", "event_type", "overrides"),
    [
        (
            "iteration without attempt",
            ITERATION_COMPLETED,
            {"payload_json": raw(GOOD_ITERATION), "provider_event_id": "finish-x"},
        ),
        (
            "iteration without provider finish id",
            ITERATION_COMPLETED,
            {"payload_json": raw(GOOD_ITERATION), "generation_attempt_id": ATTEMPT_1},
        ),
        (
            "iteration with result message",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw(GOOD_ITERATION),
                "result_message_id": foundation.TRIGGER_MESSAGE_ID,
            },
        ),
        (
            "iteration carrying raw provider output",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "text": "model said hi"}),
            },
        ),
        (
            "iteration without digest",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({"generationJobVersion": 1, "iteration": 0}),
            },
        ),
        (
            "iteration without index",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw(
                    {"generationJobVersion": 1, "priorOutputDigest": PRIOR_DIGEST}
                ),
            },
        ),
        (
            "iteration without the job version",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw(
                    {"iteration": 0, "priorOutputDigest": PRIOR_DIGEST}
                ),
            },
        ),
        (
            "job version zero",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "generationJobVersion": 0}),
            },
        ),
        (
            "job version negative",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "generationJobVersion": -1}),
            },
        ),
        (
            "job version as text",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "generationJobVersion": "1"}),
            },
        ),
        (
            "job version as real",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": '{"generationJobVersion":1.0,"iteration":0,'
                '"priorOutputDigest":"' + PRIOR_DIGEST + '"}',
            },
        ),
        (
            "job version null",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "generationJobVersion": None}),
            },
        ),
        (
            "iteration negative",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "iteration": -1}),
            },
        ),
        (
            "iteration as text",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "iteration": "0"}),
            },
        ),
        (
            "iteration as real",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": '{"generationJobVersion":1,"iteration":0.0,'
                '"priorOutputDigest":"' + PRIOR_DIGEST + '"}',
            },
        ),
        (
            "digest short",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw(
                    {**GOOD_ITERATION, "priorOutputDigest": "sha256:ab"}
                ),
            },
        ),
        (
            "digest upper case",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw(
                    {
                        **GOOD_ITERATION,
                        "priorOutputDigest": "sha256:" + "A" * 64,
                    }
                ),
            },
        ),
        (
            "digest not text",
            ITERATION_COMPLETED,
            {
                **ATTEMPT_AND_FINISH,
                "payload_json": raw({**GOOD_ITERATION, "priorOutputDigest": 7}),
            },
        ),
        (
            "direction change with result message",
            DIRECTION_STATE_CHANGED,
            {
                "payload_json": raw(GOOD_STATE),
                "result_message_id": foundation.TRIGGER_MESSAGE_ID,
            },
        ),
        (
            "direction change with the content or reason",
            DIRECTION_STATE_CHANGED,
            {"payload_json": raw({**GOOD_STATE, "reason": "no longer wanted"})},
        ),
        (
            "direction change without state",
            DIRECTION_STATE_CHANGED,
            {
                "payload_json": raw(
                    {"directionId": "direction-1", "directionVersion": 2}
                )
            },
        ),
        (
            "direction change with unknown state",
            DIRECTION_STATE_CHANGED,
            {"payload_json": raw({**GOOD_STATE, "state": "queued"})},
        ),
        (
            "direction change with version zero",
            DIRECTION_STATE_CHANGED,
            {"payload_json": raw({**GOOD_STATE, "directionVersion": 0})},
        ),
        (
            "direction change with a malformed identity",
            DIRECTION_STATE_CHANGED,
            {"payload_json": raw({**GOOD_STATE, "directionId": "direction 1"})},
        ),
        (
            "direction change with an empty default payload",
            DIRECTION_STATE_CHANGED,
            {},
        ),
        ("unknown direction kind", "chat.direction.created", {}),
    ],
)
def test_wrongly_shaped_or_unknown_event_kinds_are_rejected(
    owned: m1.Owned, case: str, event_type: str, overrides: dict[str, object]
) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row())
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        foundation.insert(owned, EVENTS, event(2, event_type, **overrides))
    assert count(owned.connection, EVENTS) == 1, case


VERSION_REFUSAL = "job status projection version"


def test_an_iteration_event_must_carry_the_current_job_version_on_its_own_attempt(
    owned: m1.Owned,
) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row())
        foundation.insert(owned, EVENTS, iteration_event(2, ATTEMPT_1, "finish-v1", 0))
    stored = owned.connection.execute(
        f"SELECT payload_json FROM {EVENTS} WHERE event_id = 'generation-event-2'"
    ).fetchone()[0]
    assert json.loads(stored)["generationJobVersion"] == 1
    move_status(owned, "running", ATTEMPT_1, version=2)
    with foundation.guarded(owned):
        foundation.insert(
            owned, EVENTS, iteration_event(3, ATTEMPT_1, "finish-v2", 1, version=2)
        )
    assert count(owned.connection, EVENTS, f"event_type = '{ITERATION_COMPLETED}'") == 2
    assert integrity_check(owned.connection) == []


def test_a_job_version_above_a_million_is_legal_and_matches_exactly(
    owned: m1.Owned,
) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row(version=1_000_001))
        foundation.insert(
            owned,
            EVENTS,
            iteration_event(2, ATTEMPT_1, "finish-big", 0, version=1_000_001),
        )
    stored = owned.connection.execute(
        f"SELECT payload_json FROM {EVENTS} WHERE event_id = 'generation-event-2'"
    ).fetchone()[0]
    assert json.loads(stored)["generationJobVersion"] == 1_000_001
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=VERSION_REFUSAL),
    ):
        foundation.insert(
            owned,
            EVENTS,
            iteration_event(3, ATTEMPT_1, "finish-off", 1, version=1_000_000),
        )
    assert integrity_check(owned.connection) == []


@pytest.mark.parametrize("version", [2, 3, 1000])
def test_a_mismatched_job_version_is_refused(owned: m1.Owned, version: int) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row())
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=VERSION_REFUSAL),
    ):
        foundation.insert(
            owned,
            EVENTS,
            iteration_event(2, ATTEMPT_1, "finish-x", 0, version=version),
        )
    assert count(owned.connection, EVENTS) == 1


def test_a_stale_job_version_is_refused_once_the_projection_advanced(
    owned: m1.Owned,
) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row())
    move_status(owned, "running", ATTEMPT_1, version=2)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=VERSION_REFUSAL),
    ):
        foundation.insert(
            owned, EVENTS, iteration_event(2, ATTEMPT_1, "finish-x", 0, version=1)
        )
    assert count(owned.connection, EVENTS) == 1


def test_an_iteration_event_needs_the_projection_running_on_its_own_attempt(
    owned: m1.Owned,
) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, STATUS, status_row())
        foundation.insert(owned, PROVIDER_INVOCATIONS, invocation_row(ATTEMPT_2))
        foundation.insert(owned, ATTEMPTS, attempt_row(2, ATTEMPT_2))
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=VERSION_REFUSAL),
    ):
        foundation.insert(
            owned, EVENTS, iteration_event(2, ATTEMPT_2, "finish-other", 0)
        )
    move_status(owned, "failed", ATTEMPT_1, **FAILED)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=VERSION_REFUSAL),
    ):
        foundation.insert(
            owned, EVENTS, iteration_event(2, ATTEMPT_1, "finish-x", 0, version=2)
        )
    assert count(owned.connection, EVENTS) == 1


def test_an_iteration_event_needs_a_status_projection_row(owned: m1.Owned) -> None:
    foundation.seed_running_generation_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=VERSION_REFUSAL),
    ):
        foundation.insert(owned, EVENTS, iteration_event(2, ATTEMPT_1, "finish-x", 0))
    assert count(owned.connection, EVENTS) == 1


def test_the_new_event_rows_stay_append_only(owned: m1.Owned) -> None:
    foundation.seed_running_generation_job(owned)
    with foundation.guarded(owned):
        foundation.insert(
            owned,
            EVENTS,
            event(2, DIRECTION_STATE_CHANGED, payload_json=state_payload()),
        )
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        owned.connection.execute(f"UPDATE {EVENTS} SET payload_json = '{{}}'")
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"),
    ):
        owned.connection.execute(f"DELETE FROM {EVENTS}")
    assert count(owned.connection, EVENTS) == 2


# --- Directions: insert -------------------------------------------------------


def test_a_pending_direction_is_inserted_and_every_write_needs_the_fenced_owner(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        foundation.insert(owned, DIRECTIONS, direction_row(1))
    status_before = owned.connection.execute(f"SELECT * FROM {STATUS}").fetchall()
    add_direction(owned)
    row = owned.connection.execute(
        f"SELECT state, version, requested_boundary, effective_boundary, terminal_at_us "
        f"FROM {DIRECTIONS}"
    ).fetchone()
    assert tuple(row) == ("pending", 1, "earliest_safe", None, None)
    assert owned.connection.execute(f"SELECT * FROM {STATUS}").fetchall() == (
        status_before
    )
    assert verify_direction_digests(owned.connection) == []
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


def test_a_second_pending_direction_in_one_job_is_refused_but_another_job_may_hold_one(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(2))
    add_direction(
        owned,
        1,
        direction_id="direction-other-1",
        generation_job_id=OTHER_JOB_ID,
        target_attempt_id=OTHER_ATTEMPT_1,
    )
    assert count(owned.connection, DIRECTIONS) == 2


@pytest.mark.parametrize("sequence", [2, 0, 7])
def test_direction_sequence_must_be_contiguous_from_one(
    owned: m1.Owned, sequence: int
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(
            sqlite3.IntegrityError, match="contiguous|CHECK constraint failed"
        ),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(sequence))
    assert count(owned.connection, DIRECTIONS) == 0


def test_a_repeated_sequence_or_identity_is_refused(owned: m1.Owned) -> None:
    seed_job(owned)
    add_direction(owned)
    with foundation.guarded(owned):
        transition(owned, "direction-1", "cancelled")
    with (
        foundation.guarded(owned),
        pytest.raises(
            sqlite3.IntegrityError, match="contiguous|UNIQUE constraint failed"
        ),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, direction_id="direction-again")
        )
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(2, direction_id="direction-1")
        )


@pytest.mark.parametrize(
    "requested",
    [
        "next_iteration",
        "Earliest_Safe",
        "EARLIEST_SAFE",
        "earliest-safe",
        "earliest_safe ",
        "earliest_safe\n",
        "earliest_saf",
        "immediate",
        "",
    ],
)
def test_the_requested_boundary_is_exactly_earliest_safe(
    owned: m1.Owned, requested: str
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, requested_boundary=requested)
        )
    assert count(owned.connection, DIRECTIONS) == 0


def test_the_requested_boundary_cannot_be_null_or_a_non_text_value(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    for value in (None, 1):
        with (
            foundation.guarded(owned),
            pytest.raises(sqlite3.IntegrityError, match="constraint failed"),
        ):
            foundation.insert(
                owned, DIRECTIONS, direction_row(1, requested_boundary=value)
            )
    assert count(owned.connection, DIRECTIONS) == 0


@pytest.mark.parametrize(
    "effective",
    ["next_iteration", "earliest_safe"],
)
def test_a_pending_direction_has_no_effective_boundary(
    owned: m1.Owned, effective: str
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, effective_boundary=effective)
        )
    assert count(owned.connection, DIRECTIONS) == 0


@pytest.mark.parametrize(
    "effective",
    [
        "earliest_safe",
        "Next_Iteration",
        "NEXT_ITERATION",
        "next-iteration",
        "next_iteration ",
        "immediate",
        "",
        None,
    ],
)
def test_the_effective_boundary_of_an_applied_direction_is_exactly_next_iteration(
    owned: m1.Owned, effective: str | None
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        transition(owned, "direction-1", "applied", effective_boundary=effective)
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1


@pytest.mark.parametrize("state", ["rejected", "cancelled"])
def test_a_direction_that_was_not_applied_has_no_effective_boundary(
    owned: m1.Owned, state: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        transition(owned, "direction-1", state, effective_boundary="next_iteration")
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1


@pytest.mark.parametrize(
    ("case", "overrides", "message"),
    [
        ("not JSON", {"content_json": "not json"}, "canonical JSON object"),
        ("an array", {"content_json": '["a"]'}, "canonical JSON object"),
        ("whitespace", {"content_json": '{"a": 1}'}, "canonical JSON object"),
        (
            "empty",
            {"content_json": ""},
            "canonical JSON object|CHECK constraint failed",
        ),
        (
            "over the bound",
            {"content_json": '{"a":"' + "x" * 16384 + '"}'},
            "CHECK constraint failed",
        ),
        (
            "short digest",
            {"content_digest": "sha256:" + "a" * 63},
            "CHECK constraint failed",
        ),
        ("bare digest", {"content_digest": "a" * 64}, "CHECK constraint failed"),
        (
            "upper-case digest",
            {"content_digest": "sha256:" + "A" * 64},
            "CHECK constraint failed",
        ),
    ],
)
def test_invalid_content_and_digest_are_refused(
    owned: m1.Owned, case: str, overrides: dict[str, object], message: str
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=message),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(1, **overrides))
    assert count(owned.connection, DIRECTIONS) == 0, case


@pytest.mark.parametrize(
    ("case", "overrides", "message"),
    [
        (
            "terminal on insert",
            {
                "state": "applied",
                "version": 2,
                **APPLIED,
                "terminal_at_us": BASE_US + 301,
                "updated_at_us": BASE_US + 301,
            },
            "pending state",
        ),
        ("version two", {"version": 2}, "pending state|CHECK constraint failed"),
        (
            "evidence on pending",
            {"applied_attempt_id": ATTEMPT_2},
            "CHECK constraint failed",
        ),
        (
            "manifest identity on pending",
            {"applied_request_manifest_id": MANIFEST_IDS[ATTEMPT_2]},
            "CHECK constraint failed",
        ),
        (
            "iteration on pending",
            {"applied_iteration": 1},
            "CHECK constraint failed",
        ),
        (
            "reason on pending",
            {"terminal_reason_json": REASON},
            "CHECK constraint failed",
        ),
        (
            "terminal time on pending",
            {"terminal_at_us": BASE_US + 301},
            "CHECK constraint failed",
        ),
        (
            "self supersession",
            {"supersedes_direction_id": "direction-1"},
            "already superseded by it|CHECK constraint failed",
        ),
        ("unknown state", {"state": "queued"}, "pending state|CHECK constraint failed"),
        (
            "updated before created",
            {"updated_at_us": BASE_US + 300},
            "CHECK constraint failed",
        ),
        ("bad schema version", {"schema_version": 2}, "CHECK constraint failed"),
    ],
)
def test_invalid_insert_shapes_are_refused(
    owned: m1.Owned, case: str, overrides: dict[str, object], message: str
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=message),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(1, **overrides))
    assert count(owned.connection, DIRECTIONS) == 0, case


@pytest.mark.parametrize(
    ("case", "overrides", "message"),
    [
        (
            "branch the job is not on",
            {"branch_id": "branch-elsewhere"},
            "conversation and branch of its job|FOREIGN KEY",
        ),
        (
            "conversation the job is not in",
            {"conversation_id": "conv-elsewhere"},
            "conversation and branch of its job|status projection|FOREIGN KEY",
        ),
        (
            "attempt of another job",
            {"target_attempt_id": OTHER_ATTEMPT_1},
            "status projection|FOREIGN KEY",
        ),
        (
            "unknown attempt",
            {"target_attempt_id": "generation-attempt-missing"},
            "status projection|FOREIGN KEY",
        ),
        (
            "unknown job",
            {"generation_job_id": "generation-job-missing"},
            "conversation and branch of its job|FOREIGN KEY",
        ),
        (
            "created before its job",
            {"created_at_us": BASE_US + 50, "updated_at_us": BASE_US + 50},
            "conversation and branch of its job",
        ),
        (
            "unfinished predecessor missing",
            {"supersedes_direction_id": "direction-9"},
            "FOREIGN KEY|replace",
        ),
    ],
)
def test_direction_scope_must_match_its_job_branch_and_attempt(
    owned: m1.Owned, case: str, overrides: dict[str, object], message: str
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=message),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(1, **overrides))
    assert count(owned.connection, DIRECTIONS) == 0, case


STATUS_REFUSALS = "status projection"


@pytest.mark.parametrize("seeded", [None, "queued"])
def test_a_direction_needs_a_running_status_projection_row(
    owned: m1.Owned, seeded: str | None
) -> None:
    seed_job(owned, status=seeded)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=STATUS_REFUSALS),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(1))
    assert count(owned.connection, DIRECTIONS) == 0


@pytest.mark.parametrize(
    ("state", "columns"),
    [
        ("retryable", {"sanitized_error_code": "rate_limited"}),
        ("failed", FAILED),
        ("cancelled", FAILED),
    ],
)
def test_a_direction_is_refused_once_the_job_left_running(
    owned: m1.Owned, state: str, columns: dict[str, object]
) -> None:
    seed_job(owned)
    move_status(owned, state, **columns)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=STATUS_REFUSALS),
    ):
        foundation.insert(owned, DIRECTIONS, direction_row(1))
    assert count(owned.connection, DIRECTIONS) == 0


def test_a_direction_is_refused_for_a_stale_or_noncurrent_target_attempt(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=STATUS_REFUSALS),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, target_attempt_id=ATTEMPT_2)
        )
    move_status(owned, "running", ATTEMPT_2)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=STATUS_REFUSALS),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, target_attempt_id=ATTEMPT_1)
        )
    add_direction(owned, target_attempt_id=ATTEMPT_2)
    assert count(owned.connection, DIRECTIONS) == 1


def test_another_jobs_running_projection_does_not_authorise_this_job(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=STATUS_REFUSALS),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, target_attempt_id=OTHER_ATTEMPT_2)
        )
    move_status(owned, "failed", OTHER_ATTEMPT_1, job_id=OTHER_JOB_ID, **FAILED)
    add_direction(owned)
    assert count(owned.connection, DIRECTIONS) == 1


def test_a_finished_foundation_job_without_a_projection_row_is_refused(
    owned: m1.Owned,
) -> None:
    foundation.seed_every_table(owned)
    assert count(owned.connection, JOBS, "state = 'succeeded'") == 1
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match=STATUS_REFUSALS),
    ):
        foundation.insert(
            owned, DIRECTIONS, direction_row(1, created_at_us=BASE_US + 300)
        )


# --- Directions: transitions --------------------------------------------------


@pytest.mark.parametrize("state", ["applied", "superseded", "rejected", "cancelled"])
def test_every_legal_transition_out_of_pending_is_accepted(
    owned: m1.Owned, state: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    if state == "applied":
        add_manifest(owned)
    terminate(owned, state)
    row = owned.connection.execute(
        f"SELECT state, version, terminal_at_us, updated_at_us FROM {DIRECTIONS} "
        "WHERE direction_id = 'direction-1'"
    ).fetchone()
    assert tuple(row) == (state, 2, BASE_US + 400, BASE_US + 400)
    if state != "superseded":
        # A terminal row frees the Job's single pending slot.
        add_direction(owned, 2)
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


# --- Directions: application proof -------------------------------------------


def test_an_applied_direction_records_the_complete_application_proof(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    status_before = owned.connection.execute(f"SELECT * FROM {STATUS}").fetchall()
    applied_direction(owned)
    row = owned.connection.execute(
        "SELECT state, version, applied_attempt_id, applied_provider_invocation_id, "
        "applied_request_manifest_id, applied_iteration, requested_boundary, "
        f"effective_boundary, terminal_at_us, updated_at_us FROM {DIRECTIONS}"
    ).fetchone()
    assert tuple(row) == (
        "applied",
        2,
        ATTEMPT_2,
        INVOCATIONS[ATTEMPT_2],
        MANIFEST_IDS[ATTEMPT_2],
        1,
        "earliest_safe",
        "next_iteration",
        BASE_US + 400,
        BASE_US + 400,
    )
    manifest_at = owned.connection.execute(
        f"SELECT created_at_us FROM {MANIFESTS}"
    ).fetchone()[0]
    assert manifest_at <= BASE_US + 400
    assert owned.connection.execute(f"SELECT * FROM {STATUS}").fetchall() == (
        status_before
    )
    assert verify_direction_digests(owned.connection) == []
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


def assert_apply_refused(holder: m1.Owned, message: str, **columns: object) -> None:
    with (
        foundation.guarded(holder),
        pytest.raises(sqlite3.IntegrityError, match=message),
    ):
        transition(holder, "direction-1", "applied", **columns)
    assert count(holder.connection, DIRECTIONS, "state = 'pending'") == 1


@pytest.mark.parametrize(
    "column",
    [
        "applied_attempt_id",
        "applied_provider_invocation_id",
        "applied_request_manifest_id",
        "applied_iteration",
        "effective_boundary",
    ],
)
def test_every_application_proof_column_is_required_to_apply(
    owned: m1.Owned, column: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    assert_apply_refused(owned, ".", **{column: None})


@pytest.mark.parametrize("iteration", [0, -1, 1_000_001, "one", 1.5])
def test_the_application_iteration_is_a_bounded_integer(
    owned: m1.Owned, iteration: object
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    assert_apply_refused(owned, ".", applied_iteration=iteration)


def test_the_application_iteration_must_follow_the_completed_iteration(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    for iteration in (2, 3, 1000):
        assert_apply_refused(
            owned, "iteration_completed event", applied_iteration=iteration
        )


def test_applying_needs_the_target_attempts_committed_iteration_completed_event(
    owned: m1.Owned,
) -> None:
    """A text event, or no iteration event at all, must not stand in for the target's."""
    seed_job(owned, iteration_evidence=False)
    add_direction(owned)
    add_manifest(owned)
    assert_apply_refused(owned, "iteration_completed event")


def test_an_event_recorded_after_the_application_time_does_not_count(
    owned: m1.Owned,
) -> None:
    seed_job(owned, iteration_evidence=False)
    with foundation.guarded(owned):
        foundation.insert(
            owned,
            EVENTS,
            iteration_event(
                4, ATTEMPT_1, "provider-finish-late", 0, occurred_at_us=BASE_US + 900
            ),
        )
    add_direction(owned)
    add_manifest(owned)
    assert_apply_refused(owned, "iteration_completed event")


@pytest.mark.parametrize(
    ("case", "columns"),
    [
        ("by the targeted attempt itself", {"applied_attempt_id": ATTEMPT_1}),
        ("by a later-than-next attempt", {"applied_attempt_id": ATTEMPT_3}),
        ("by another job's attempt", {"applied_attempt_id": OTHER_ATTEMPT_2}),
        ("by an unknown attempt", {"applied_attempt_id": "generation-attempt-nope"}),
        (
            "with another attempt's provider invocation",
            {"applied_provider_invocation_id": INVOCATIONS[ATTEMPT_3]},
        ),
        (
            "with the target's provider invocation",
            {"applied_provider_invocation_id": INVOCATIONS[ATTEMPT_1]},
        ),
        (
            "with an unknown provider invocation",
            {"applied_provider_invocation_id": "provider-invocation-nope"},
        ),
    ],
)
def test_the_applying_attempt_must_be_the_next_one_with_its_own_invocation(
    owned: m1.Owned, case: str, columns: dict[str, object]
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    assert_apply_refused(owned, "applying attempt must directly follow", **columns)


def test_the_applying_attempt_must_retry_the_target_attempt(owned: m1.Owned) -> None:
    """Attempt 4 is number 3+1 but its lineage skips attempt 3."""
    seed_job(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, PROVIDER_INVOCATIONS, invocation_row(ATTEMPT_4))
        foundation.insert(
            owned, ATTEMPTS, attempt_row(4, ATTEMPT_4, retry_of_attempt_id=ATTEMPT_1)
        )
    move_status(owned, "running", ATTEMPT_3)
    add_direction(owned, target_attempt_id=ATTEMPT_3)
    add_manifest(owned, ATTEMPT_4)
    assert_apply_refused(
        owned,
        "applying attempt must directly follow",
        applied_attempt_id=ATTEMPT_4,
        applied_provider_invocation_id=INVOCATIONS[ATTEMPT_4],
        applied_request_manifest_id=MANIFEST_IDS[ATTEMPT_4],
    )


@pytest.mark.parametrize(
    ("case", "state", "attempt", "columns"),
    [
        ("retryable", "retryable", ATTEMPT_1, {"sanitized_error_code": "rate_limited"}),
        ("failed", "failed", ATTEMPT_1, FAILED),
        ("cancelled", "cancelled", ATTEMPT_1, FAILED),
        ("already advanced to the applying attempt", "running", ATTEMPT_2, {}),
    ],
)
def test_applying_needs_the_projection_running_on_the_target_attempt(
    owned: m1.Owned, case: str, state: str, attempt: str, columns: dict[str, object]
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    move_status(owned, state, attempt, **columns)
    assert_apply_refused(owned, "status projection is running on its target attempt")


def test_applying_keeps_the_public_job_cas_token_untouched(owned: m1.Owned) -> None:
    seed_job(owned)
    applied_direction(owned)
    version = owned.connection.execute(
        f"SELECT version, current_attempt_id FROM {STATUS} WHERE generation_job_id = ?",
        (JOB_ID,),
    ).fetchone()
    assert tuple(version) == (1, ATTEMPT_1)
    move_status(owned, "running", ATTEMPT_2, version=2)
    assert (
        owned.connection.execute(
            f"SELECT version, current_attempt_id FROM {STATUS} WHERE generation_job_id = ?",
            (JOB_ID,),
        ).fetchone()[0]
        == 2
    )


def test_applying_needs_a_committed_manifest(owned: m1.Owned) -> None:
    seed_job(owned)
    add_direction(owned)
    assert_apply_refused(owned, "committed request manifest that binds it")


@pytest.mark.parametrize(
    ("case", "direction"),
    [
        ("no direction key", ...),
        ("null binding", None),
        ("string binding", "direction-1"),
        ("array binding", [binding()]),
        ("empty binding", {}),
        ("other direction id", binding("direction-9")),
        ("numeric direction id", binding(1)),
        ("version two", binding(version=2)),
        ("version as text", binding(version="1")),
        ("version as real", binding(version=1.0)),
        ("version zero", binding(version=0)),
        ("other digest", binding(content_digest=OTHER_DIGEST)),
        ("upper-case digest", binding(content_digest=CONTENT_DIGEST.upper())),
        ("digest without prefix", binding(content_digest=CONTENT_DIGEST[7:])),
        ("digest not text", binding(content_digest=7)),
        ("extra key in binding", {**binding(), "note": "x"}),
        ("missing digest", {"directionId": "direction-1", "directionVersion": 1}),
        (
            "missing version",
            {"directionId": "direction-1", "contentDigest": CONTENT_DIGEST},
        ),
        ("missing id", {"directionVersion": 1, "contentDigest": CONTENT_DIGEST}),
        ("misspelled key", {**binding(), "directionID": "direction-1"}),
    ],
)
def test_the_manifest_must_carry_the_exact_direction_binding(
    owned: m1.Owned, case: str, direction: object
) -> None:
    seed_job(owned)
    add_direction(owned)
    body = manifest_body(direction)
    add_manifest(
        owned,
        request_manifest_body_json=body,
        manifest_digest=sha256_digest(body),
    )
    assert_apply_refused(owned, "committed request manifest that binds it")


def test_a_differently_cased_binding_key_or_a_non_json_body_does_not_bind(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    body = json.dumps({"Direction": binding()}, separators=(",", ":"))
    add_manifest(owned, request_manifest_body_json=body)
    assert_apply_refused(owned, "committed request manifest that binds it")


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(json.loads(manifest_body(binding())), indent=2, sort_keys=True),
        manifest_body(binding()).replace(",", ", "),
        " " + manifest_body(binding()),
    ],
)
def test_a_noncanonical_manifest_body_does_not_bind_even_when_semantically_exact(
    owned: m1.Owned, body: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    assert json.loads(body)["direction"] == binding()
    add_manifest(
        owned, request_manifest_body_json=body, manifest_digest=sha256_digest(body)
    )
    assert_apply_refused(owned, "committed request manifest that binds it")
    canonical = manifest_body(binding())
    assert json.loads(body) == json.loads(canonical) and body != canonical


def test_a_manifest_body_that_is_not_json_fails_closed(owned: m1.Owned) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned, request_manifest_body_json="not json at all")
    assert_apply_refused(owned, "committed request manifest that binds it")


@pytest.mark.parametrize(
    ("case", "manifest_overrides", "columns"),
    [
        (
            "manifest of another attempt",
            {},
            {
                "applied_request_manifest_id": MANIFEST_IDS[ATTEMPT_3],
            },
        ),
        (
            "manifest id that does not exist",
            {},
            {"applied_request_manifest_id": "request-manifest-nope"},
        ),
        (
            "manifest naming another provider invocation",
            {"provider_invocation_id": INVOCATIONS[ATTEMPT_3]},
            {},
        ),
        (
            "manifest created before the direction",
            {"created_at_us": BASE_US + 300},
            {},
        ),
        (
            "manifest created after the application time",
            {"created_at_us": BASE_US + 401},
            {},
        ),
        (
            "manifest on another branch",
            {"branch_id": "branch-elsewhere"},
            {},
        ),
    ],
)
def test_the_manifest_row_must_match_every_applied_identity(
    owned: m1.Owned,
    case: str,
    manifest_overrides: dict[str, object],
    columns: dict[str, object],
) -> None:
    seed_job(owned)
    add_direction(owned)
    with foundation.guarded(owned):
        if case == "manifest on another branch":
            foundation.insert(
                owned,
                foundation.BRANCHES,
                foundation.branch_row(branch_id="branch-elsewhere"),
            )
        foundation.insert(owned, MANIFESTS, manifest_row(**manifest_overrides))
    assert_apply_refused(owned, "committed request manifest that binds it", **columns)


def test_a_manifest_of_another_job_cannot_be_applied_here(owned: m1.Owned) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    with foundation.guarded(owned):
        foundation.insert(
            owned,
            MANIFESTS,
            manifest_row(
                OTHER_ATTEMPT_2,
                generation_job_id=OTHER_JOB_ID,
            ),
        )
    assert_apply_refused(
        owned,
        "applying attempt must directly follow",
        applied_attempt_id=OTHER_ATTEMPT_2,
        applied_provider_invocation_id=INVOCATIONS[OTHER_ATTEMPT_2],
        applied_request_manifest_id=MANIFEST_IDS[OTHER_ATTEMPT_2],
    )


def test_an_applied_direction_freezes_its_proof(owned: m1.Owned) -> None:
    seed_job(owned)
    applied_direction(owned)
    for assignment in (
        "applied_iteration = 2",
        "applied_attempt_id = 'generation-attempt-3'",
        "applied_provider_invocation_id = 'provider-invocation-3'",
        "applied_request_manifest_id = 'request-manifest-3'",
        "effective_boundary = 'earliest_safe'",
        "terminal_at_us = terminal_at_us + 1, updated_at_us = updated_at_us + 1",
    ):
        with (
            foundation.guarded(owned),
            pytest.raises(
                sqlite3.IntegrityError, match="terminal direction is immutable"
            ),
        ):
            owned.connection.execute(f"UPDATE {DIRECTIONS} SET {assignment}")


# --- Directions: supersession --------------------------------------------------


def test_a_valid_two_write_supersession_commits_and_reads_back_reciprocally(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    terminate(owned, "superseded")
    rows = owned.connection.execute(
        "SELECT direction_id, state, superseded_by_direction_id, supersedes_direction_id "
        f"FROM {DIRECTIONS} ORDER BY direction_sequence"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("direction-1", "superseded", "direction-2", None),
        ("direction-2", "pending", None, "direction-1"),
    ]
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


def test_an_old_row_superseded_without_any_successor_fails_at_commit(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    before = owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall()
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"),
        foundation.guarded(owned),
    ):
        transition(owned, "direction-1", "superseded")
    assert not owned.connection.in_transaction
    assert owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall() == before


@pytest.mark.parametrize(
    ("case", "successor"),
    [
        ("successor omits the old row", {}),
        (
            "successor is a different identity",
            {"direction_id": "direction-3"},
        ),
    ],
)
def test_a_successor_that_does_not_name_the_old_row_back_fails_at_commit(
    owned: m1.Owned, case: str, successor: dict[str, object]
) -> None:
    seed_job(owned)
    add_direction(owned)
    before = owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall()
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"),
        foundation.guarded(owned),
    ):
        transition(owned, "direction-1", "superseded")
        foundation.insert(owned, DIRECTIONS, direction_row(2, **successor))
    assert not owned.connection.in_transaction
    assert (
        owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall() == before
    ), case


def test_a_successor_that_names_the_old_row_but_is_not_the_named_one_is_refused(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    with (
        pytest.raises(sqlite3.IntegrityError, match="already superseded by it"),
        foundation.guarded(owned),
    ):
        transition(owned, "direction-1", "superseded")
        foundation.insert(
            owned,
            DIRECTIONS,
            direction_row(
                2, direction_id="direction-3", supersedes_direction_id="direction-1"
            ),
        )
    assert not owned.connection.in_transaction
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1
    assert count(owned.connection, DIRECTIONS) == 1


def test_a_second_would_be_successor_cannot_claim_a_superseded_row(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    terminate(owned, "superseded")
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="already superseded by it"),
    ):
        foundation.insert(
            owned,
            DIRECTIONS,
            direction_row(3, supersedes_direction_id="direction-1"),
        )
    assert count(owned.connection, DIRECTIONS) == 2


def test_a_rolled_back_supersession_leaves_the_original_pending_row_intact(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    add_direction(owned)
    before = owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall()
    with (
        pytest.raises(RuntimeError, match="abandoned"),
        foundation.guarded(owned),
    ):
        transition(owned, "direction-1", "superseded")
        foundation.insert(
            owned,
            DIRECTIONS,
            direction_row(2, supersedes_direction_id="direction-1"),
        )
        raise RuntimeError("abandoned")
    assert owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall() == before
    terminate(owned, "superseded")
    assert count(owned.connection, DIRECTIONS, "state = 'superseded'") == 1


@pytest.mark.parametrize(
    ("case", "state", "columns"),
    [
        ("applied without an attempt", "applied", {"applied_attempt_id": None}),
        (
            "applied without an effective boundary",
            "applied",
            {"effective_boundary": None},
        ),
        ("applied with a reason", "applied", {"terminal_reason_json": REASON}),
        (
            "superseded without a successor",
            "superseded",
            {"superseded_by_direction_id": None},
        ),
        (
            "superseded by itself",
            "superseded",
            {"superseded_by_direction_id": "direction-1"},
        ),
        ("superseded without a reason", "superseded", {"terminal_reason_json": None}),
        ("rejected without a reason", "rejected", {"terminal_reason_json": None}),
        ("cancelled without a reason", "cancelled", {"terminal_reason_json": None}),
        (
            "rejected with a successor",
            "rejected",
            {"superseded_by_direction_id": "direction-2"},
        ),
        ("rejected with application evidence", "rejected", APPLIED),
        (
            "rejected with a manifest identity",
            "rejected",
            {"applied_request_manifest_id": MANIFEST_IDS[ATTEMPT_2]},
        ),
        (
            "reason that is not canonical",
            "rejected",
            {"terminal_reason_json": '{"a": 1}'},
        ),
        (
            "reason that is not an object",
            "cancelled",
            {"terminal_reason_json": '"closed"'},
        ),
        ("reason that is not JSON", "cancelled", {"terminal_reason_json": "closed"}),
        (
            "reason over the bound",
            "cancelled",
            {"terminal_reason_json": '{"a":"' + "x" * 4096 + '"}'},
        ),
    ],
)
def test_terminal_evidence_must_match_the_state(
    owned: m1.Owned, case: str, state: str, columns: dict[str, object]
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError),
    ):
        transition(owned, "direction-1", state, **columns)
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1, case


@pytest.mark.parametrize("version", [1, 3, 9])
def test_a_stale_or_skipped_version_is_refused(owned: m1.Owned, version: int) -> None:
    seed_job(owned)
    add_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(
            sqlite3.IntegrityError, match="exactly one|CHECK constraint failed"
        ),
    ):
        transition(owned, "direction-1", "cancelled", version=version)
    assert count(owned.connection, DIRECTIONS, "state = 'pending' AND version = 1") == 1


def test_a_losing_writer_cannot_reuse_the_winning_version(owned: m1.Owned) -> None:
    """Both writers read version 1; the second commits version 2 over a terminal row."""
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    with foundation.guarded(owned):
        transition(owned, "direction-1", "rejected")
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="terminal direction is immutable"),
    ):
        transition(owned, "direction-1", "applied")
    assert count(owned.connection, DIRECTIONS, "state = 'rejected'") == 1


@pytest.mark.parametrize("state", ["pending", "queued", "paused"])
def test_a_pending_direction_cannot_stay_pending_or_enter_an_unknown_state(
    owned: m1.Owned, state: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(
            sqlite3.IntegrityError,
            match="transition is invalid|CHECK constraint failed",
        ),
    ):
        transition(owned, "direction-1", state)


@pytest.mark.parametrize("terminal", ["applied", "superseded", "rejected", "cancelled"])
@pytest.mark.parametrize(
    "target", ["pending", "applied", "superseded", "rejected", "cancelled"]
)
def test_a_terminal_direction_never_changes_again(
    owned: m1.Owned, terminal: str, target: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    add_manifest(owned)
    terminate(owned, terminal)
    before = owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall()
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError),
    ):
        transition(
            owned,
            "direction-1",
            target,
            version=3,
            at=BASE_US + 450,
            **TERMINAL_EVIDENCE.get(target, {}),
        )
    assert owned.connection.execute(f"SELECT * FROM {DIRECTIONS}").fetchall() == before


@pytest.mark.parametrize(
    "assignment",
    [
        "direction_id = 'direction-other'",
        "direction_sequence = 9",
        "generation_job_id = 'generation-job-2'",
        "target_attempt_id = 'generation-attempt-3'",
        "branch_id = 'branch-elsewhere'",
        "actor_id = 'actor-other'",
        "content_json = '{}'",
        f"content_digest = '{OTHER_DIGEST}'",
        "requested_boundary = 'immediate'",
        "supersedes_direction_id = 'direction-0'",
        "created_at_us = created_at_us + 1",
        "workspace_id = 'ws-other'",
    ],
)
def test_direction_identity_scope_and_content_are_immutable(
    owned: m1.Owned, assignment: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.DatabaseError),
    ):
        owned.connection.execute(
            f"UPDATE {DIRECTIONS} SET state = 'cancelled', version = 2, "
            "terminal_reason_json = ?, terminal_at_us = ?, updated_at_us = ?, "
            f"{assignment}",
            (REASON, BASE_US + 400, BASE_US + 400),
        )
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1


def test_an_unfenced_update_and_every_delete_are_refused(owned: m1.Owned) -> None:
    seed_job(owned)
    add_direction(owned)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded UPDATE"):
        transition(owned, "direction-1", "cancelled")
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"),
    ):
        owned.connection.execute(f"DELETE FROM {DIRECTIONS}")
    assert count(owned.connection, DIRECTIONS, "state = 'pending'") == 1


# --- digests: what SQL can and cannot prove -----------------------------------


def test_the_fixture_digest_is_the_real_sha256_of_the_canonical_content() -> None:
    assert (
        CONTENT_DIGEST
        == "sha256:" + hashlib.sha256(CONTENT_JSON.encode("utf-8")).hexdigest()
    )
    assert CONTENT_JSON == (
        '{"editableParts":[{"payload":{"text":"Focus on evidence."},"type":"text"}]}'
    )
    content = json.loads(CONTENT_JSON)
    assert list(content) == ["editableParts"]
    assert [sorted(part) for part in content["editableParts"]] == [["payload", "type"]]
    assert (
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        == CONTENT_JSON
    )
    assert CONTENT_DIGEST != OTHER_DIGEST
    # Non-ASCII is emitted directly, so the digested bytes are the UTF-8 of the characters.
    assert "é" in direction_content("café")
    assert "\\u" not in direction_content("café")


def test_sql_refuses_a_manifest_binding_that_differs_from_the_stored_digest(
    owned: m1.Owned,
) -> None:
    """The strongest honest SQL layer: string equality of the two digest-shaped values."""
    seed_job(owned)
    add_direction(owned, content_digest=OTHER_DIGEST)
    assert verify_direction_digests(owned.connection) == ["direction-1"]
    add_manifest(owned)
    assert_apply_refused(owned, "committed request manifest that binds it")


def test_sql_cannot_prove_sha256_so_a_self_consistent_wrong_digest_needs_the_reader(
    owned: m1.Owned,
) -> None:
    """Limit, pinned on purpose: the row and its manifest agree, the hash is still wrong.

    SQLite accepts it; the documented reader contract recomputes SHA-256 over the stored
    bytes and fails closed. This is not a SQL guarantee and must not be read as one.
    """
    seed_job(owned)
    add_direction(owned, content_digest=OTHER_DIGEST)
    body = manifest_body(binding(content_digest=OTHER_DIGEST))
    add_manifest(
        owned, request_manifest_body_json=body, manifest_digest=sha256_digest(body)
    )
    with foundation.guarded(owned):
        transition(owned, "direction-1", "applied")
    assert count(owned.connection, DIRECTIONS, "state = 'applied'") == 1
    assert verify_direction_digests(owned.connection) == ["direction-1"]


# --- continuations ------------------------------------------------------------


def test_a_continuation_binds_the_applied_direction_both_attempts_and_the_finish_event(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    applied_direction(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, CONTINUATIONS, continuation_row())
    assert count(owned.connection, CONTINUATIONS) == 1
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        foundation.insert(
            owned,
            CONTINUATIONS,
            continuation_row(continuation_id="continuation-unfenced"),
        )


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("wrong version", {"direction_version": 3}),
        ("version one", {"direction_version": 1}),
        ("wrong next attempt", {"next_attempt_id": ATTEMPT_3}),
        ("wrong prior attempt", {"prior_attempt_id": ATTEMPT_3}),
        ("next is prior", {"next_attempt_id": ATTEMPT_1}),
        ("other direction", {"direction_id": "direction-9"}),
        (
            "other job's attempts",
            {
                "generation_job_id": OTHER_JOB_ID,
                "prior_attempt_id": OTHER_ATTEMPT_1,
                "next_attempt_id": OTHER_ATTEMPT_2,
            },
        ),
        ("other job, this job's attempts", {"generation_job_id": OTHER_JOB_ID}),
        ("recorded before the direction was applied", {"created_at_us": BASE_US + 399}),
        ("malformed digest", {"prior_output_digest": "sha256:xyz"}),
        ("blank provider finish", {"provider_finish_id": ""}),
        ("bad provider finish", {"provider_finish_id": "finish id"}),
    ],
)
def test_continuation_identity_and_cross_job_mismatches_are_refused(
    owned: m1.Owned, case: str, overrides: dict[str, object]
) -> None:
    seed_job(owned)
    applied_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError),
    ):
        foundation.insert(owned, CONTINUATIONS, continuation_row(**overrides))
    assert count(owned.connection, CONTINUATIONS) == 0, case


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("finish id no event carries", {"provider_finish_id": "provider-finish-9"}),
        ("digest that differs from the event", {"prior_output_digest": OTHER_DIGEST}),
        ("event of another kind", {"provider_finish_id": "provider-text-1"}),
        (
            "finish event of another attempt",
            {"provider_finish_id": "provider-finish-2"},
        ),
    ],
)
def test_a_continuation_needs_the_prior_attempts_matching_finish_event(
    owned: m1.Owned, case: str, overrides: dict[str, object]
) -> None:
    seed_job(owned)
    applied_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="iteration_completed event"),
    ):
        foundation.insert(owned, CONTINUATIONS, continuation_row(**overrides))
    assert count(owned.connection, CONTINUATIONS) == 0, case


def test_a_finish_event_for_another_iteration_or_recorded_late_does_not_back_it(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    with foundation.guarded(owned):
        foundation.insert(
            owned, EVENTS, iteration_event(4, ATTEMPT_1, "provider-finish-1b", 5)
        )
        foundation.insert(
            owned,
            EVENTS,
            iteration_event(
                5, ATTEMPT_1, "provider-finish-late", 0, occurred_at_us=BASE_US + 900
            ),
        )
    applied_direction(owned)
    for finish in ("provider-finish-1b", "provider-finish-late"):
        with (
            foundation.guarded(owned),
            pytest.raises(sqlite3.IntegrityError, match="iteration_completed event"),
        ):
            foundation.insert(
                owned, CONTINUATIONS, continuation_row(provider_finish_id=finish)
            )
    assert count(owned.connection, CONTINUATIONS) == 0


@pytest.mark.parametrize("state", ["pending", "superseded", "rejected", "cancelled"])
def test_only_an_applied_direction_can_be_continued(
    owned: m1.Owned, state: str
) -> None:
    seed_job(owned)
    add_direction(owned)
    if state != "pending":
        terminate(owned, state)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="applied direction"),
    ):
        foundation.insert(owned, CONTINUATIONS, continuation_row())


def test_a_continuation_must_advance_to_the_very_next_attempt(owned: m1.Owned) -> None:
    seed_job(owned)
    applied_direction(owned)
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError),
    ):
        foundation.insert(
            owned, CONTINUATIONS, continuation_row(next_attempt_id=ATTEMPT_3)
        )


def test_an_attempt_and_a_direction_are_each_continued_at_most_once(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    applied_direction(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, CONTINUATIONS, continuation_row())
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
    ):
        foundation.insert(
            owned, CONTINUATIONS, continuation_row(continuation_id="continuation-2")
        )
    assert count(owned.connection, CONTINUATIONS) == 1


def test_continuations_are_append_only_even_for_the_current_owner(
    owned: m1.Owned,
) -> None:
    seed_job(owned)
    applied_direction(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, CONTINUATIONS, continuation_row())
    for column, value in (
        ("provider_finish_id", "'provider-finish-2'"),
        ("prior_output_digest", f"'{OTHER_DIGEST}'"),
        ("direction_version", "3"),
        ("next_attempt_id", f"'{ATTEMPT_3}'"),
        ("generation_job_id", f"'{OTHER_JOB_ID}'"),
    ):
        with (
            foundation.guarded(owned),
            pytest.raises(sqlite3.IntegrityError, match="append-only"),
        ):
            owned.connection.execute(f"UPDATE {CONTINUATIONS} SET {column} = {value}")
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"),
    ):
        owned.connection.execute(f"DELETE FROM {CONTINUATIONS}")
    assert count(owned.connection, CONTINUATIONS) == 1


def test_a_continued_direction_stays_applied_and_immutable(owned: m1.Owned) -> None:
    seed_job(owned)
    applied_direction(owned)
    with foundation.guarded(owned):
        foundation.insert(owned, CONTINUATIONS, continuation_row())
    with (
        foundation.guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="terminal direction is immutable"),
    ):
        transition(owned, "direction-1", "cancelled", version=3)


# --- failure atomicity --------------------------------------------------------


@pytest.fixture(scope="module")
def baseline_0068(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fully migrated workspace at 0068, closed, copied for each interruption."""
    path = tmp_path_factory.mktemp("through-0068") / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
        holder = m1.take_ownership(path)
        try:
            foundation.seed_every_table(holder)
        finally:
            holder.connection.close()
    return path


@pytest.mark.parametrize("stop_after", list(range(1, EXPECTED_STATEMENT_COUNT + 1)))
def test_failure_after_every_statement_leaves_all_of_0069_or_none_and_retry_lands_once(
    baseline_0068: Path, tmp_path: Path, stop_after: int
) -> None:
    path = tmp_path / "interrupted.sqlite"
    shutil.copyfile(baseline_0068, path)

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        before = t34.event_rows(connection)
        events_ddl = t34.stored_sql(connection, EVENTS)
        crashing = m1.FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with pytest.raises(m1.MigrationInterrupted, match=f"statement {stop_after}$"):
            apply_pending_migrations(
                cast("sqlite3.Connection", crashing),
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
        assert crashing.executed == stop_after
    finally:
        connection.close()

    interrupted = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        assert MIGRATION_VERSION not in applied_migrations(interrupted)
        names = object_names(interrupted)
        assert names.isdisjoint({*TABLES, ONE_PENDING_INDEX, *TRIGGERS}), sorted(names)
        assert not [name for name in names if name.endswith("_0069")]
        assert t34.stored_sql(interrupted, EVENTS) == events_ddl
        assert {t34.EVENTS_INDEX, *t34.EVENTS_TRIGGERS} <= names
        assert t34.event_rows(interrupted) == before
        failed = interrupted.execute(
            "SELECT detail FROM omnivia_migration_attempts "
            "WHERE version = ? AND outcome = 'failed'",
            (MIGRATION_VERSION,),
        ).fetchall()
        assert len(failed) == 1 and f"statement {stop_after}" in str(failed[0][0]), (
            failed
        )
        assert integrity_check(interrupted) == []
        assert foreign_key_check(interrupted) == []
    finally:
        interrupted.close()

    applied = maintenance_apply(path)
    assert [m.version for m in applied] == [MIGRATION_VERSION]
    settled = open_database(path, OpenMode.READ_ONLY)
    try:
        assert {*TABLES, ONE_PENDING_INDEX, *TRIGGERS} <= object_names(settled)
        assert t34.event_rows(settled) == before
        assert (
            settled.execute(
                "SELECT COUNT(*) FROM omnivia_schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()[0]
            == 1
        )
        assert fingerprint_schema(settled).matches(canonical_schema_fingerprint())
        assert integrity_check(settled) == []
        assert foreign_key_check(settled) == []
    finally:
        settled.close()
