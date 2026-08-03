"""V06-1 M5 acceptance for durable projection lifecycle evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_durable_job_history_migration as m4
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import close_guard, fenced_transaction
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 11
MIGRATION_NAME = "0011_projection_lifecycle.sql"
WORKSPACE_ID = m2.WORKSPACE_ID
BASE_US = m4.BASE_US + 1000
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64

TABLES = {
    "omnivia_projection_runs",
    "omnivia_projection_run_checkpoints",
    "omnivia_projection_record_failures",
    "omnivia_projection_validations",
    "omnivia_projection_activations",
}
INDEXES = {
    "omnivia_idx_projection_runs_state_epoch",
    "omnivia_idx_projection_checkpoints_latest",
    "omnivia_idx_projection_failures_record_code",
    "omnivia_idx_projection_validations_outcome",
    "omnivia_idx_projection_activations_history",
}
TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{verb}"
    for table in TABLES
    for verb in ("insert", "update", "delete")
}
TRIGGERS.update(
    {
        "omnivia_apply_projection_activation",
        "omnivia_guard_projection_ledger_active_pointer_update",
        "omnivia_guard_projection_ledger_identity_insert",
    }
)
SCHEMA_SQL_SHA256 = {
    "omnivia_apply_projection_activation": "64c9f5a56f26cd5ced352a45ff50ed24d7cb04f3e85c26ea9479cd5e4ef2e57f",
    "omnivia_guard_projection_activations_delete": "0573f9e0ecd794ebcbd348f87e5728f85e96bd8aa6cb752d05e5c60fe279b03a",
    "omnivia_guard_projection_activations_insert": "5542b3cb4310371d267336ff28aaf1925351839dd0b567edf77a9f92cc47dffb",
    "omnivia_guard_projection_activations_update": "b1fc98488b99710680fd79059fa56f8ba66e91ea184a3e3fe84a295e156a3e91",
    "omnivia_guard_projection_ledger_active_pointer_update": "81746d218464c0447c3f62ebb30357ee7d83317f50606b5866672385ac6865ab",
    "omnivia_guard_projection_ledger_identity_insert": "6749cd4628e8f9391b6b51d18c9eef76c3b2967ccf4d3d430052ea39a0369774",
    "omnivia_guard_projection_record_failures_delete": "e982c3d0342fdf78e23dfd9a2c7c6551f0239c03db4f248b3ce75fd1ac2a3e31",
    "omnivia_guard_projection_record_failures_insert": "2600c39b61e81e49a1b4cc6215a2786fbe3847a3ced508ac1abbd14d5a78b12d",
    "omnivia_guard_projection_record_failures_update": "11fe3647bafde8811964a7b0e11388564cfb2801b66d2130dc8f0b42640fcc1b",
    "omnivia_guard_projection_run_checkpoints_delete": "6634e17e52f59ea99ad78eaca8ea850649f3291a783eb668259ac603731073f0",
    "omnivia_guard_projection_run_checkpoints_insert": "3a0f927cf2cfbbfaceb6c6581806cc8efca1fcd04693b7b93942e58822b12cca",
    "omnivia_guard_projection_run_checkpoints_update": "e45dfdf043e41f8095be73d9745b717af59ce730b6c887d1eb6995d86eb824d3",
    "omnivia_guard_projection_runs_delete": "529313ec2746cf8d9c5636829db093f39ef0a2e204431d93115cd37815e7b498",
    "omnivia_guard_projection_runs_insert": "92a089814777c1307efab8ea2aa8bdd85afbe627a9ba95bafdcb5a4a1696ad39",
    "omnivia_guard_projection_runs_update": "f339e00c200bc1acfe264d2e8dc1519589b6fd850d1e3832c1a4b17d15b1de1e",
    "omnivia_guard_projection_validations_delete": "ea203cc5b5270583702c80866da09f88a37c1749cd43cca47e89e91cde041c7a",
    "omnivia_guard_projection_validations_insert": "7512f549a34fe94714c0b206080b8c833f79ab760b7a86fee73e722c3a8d3bf7",
    "omnivia_guard_projection_validations_update": "34d4ad4d5ec7489688d89c4c90b0f5e014f3e8812aabc700a5c40e98e84d618a",
    "omnivia_idx_projection_activations_history": "3b1f81fc167d733b227ff7fbde7f8364d4f04850040c7c1a77c50aa0b6f36549",
    "omnivia_idx_projection_checkpoints_latest": "1de8f61ea0551606364789a7165403a819d06a0838a7dabfc0b844d102a4ecaf",
    "omnivia_idx_projection_failures_record_code": "196931c99f1a493de79c72670bb9a16b350791268c9023a7dfdf4aec3cd25502",
    "omnivia_idx_projection_runs_state_epoch": "38f3be0afc1c8a6e3464d95337f61878623e857aa02fb35418e83d0b3bfcf934",
    "omnivia_idx_projection_validations_outcome": "d6b6482d7159299dad367eac13763bd96ffe7862b05a64b1fb58ecbe8272a78d",
    "omnivia_projection_activations": "a70a2734a96131be5db6ae835eaf33f9abd674b4551adcb54aa8899ce19dbade",
    "omnivia_projection_ledger": "798becc74c03525f5984a9e9110176fc07226ff85759dbeb7518f3c0a81fbf2c",
    "omnivia_projection_record_failures": "639e1b45cc7e0ad754c7ca08a746606d394959cb532bdbb1870d07ec77e7a9c5",
    "omnivia_projection_run_checkpoints": "31c10ce338bbd0e2fcb26c6d358b881617df10f504de3b7e1eafaa56df08d13b",
    "omnivia_projection_runs": "229ae685fe32113a6e8e7ba35ce6b4b931038beacee57330d7a3f86223aaeac4",
    "omnivia_projection_validations": "82808fd551971b2c2ae00ea7d524c101e2c731b6c1a085eab0451f87587d06fd",
}
PRAGMA_SHAPE_SHA256 = {
    "table_xinfo:omnivia_projection_runs": "2d92ff4fd5872a3e5085df65fc6968ee18402acd9dc074decac67549a1de14b7",
    "foreign_key_list:omnivia_projection_runs": "109d912da93809b5c5b4dd11b948a20f915633602a1c3a43e24c15033d507ce2",
    "table_xinfo:omnivia_projection_run_checkpoints": "084e7c96a380147df163487523258126fa61698eed3c59574bb32aaebdcc51a7",
    "foreign_key_list:omnivia_projection_run_checkpoints": "edd0732de63febda3be65cc08f9cb321e7989ee2a8c16c4b1a847f4b7388ad9d",
    "table_xinfo:omnivia_projection_record_failures": "985792c63540f2987f39016a59ab64e14b435f3c5f053d8ef97ebdfe04d74722",
    "foreign_key_list:omnivia_projection_record_failures": "edd0732de63febda3be65cc08f9cb321e7989ee2a8c16c4b1a847f4b7388ad9d",
    "table_xinfo:omnivia_projection_validations": "57ba85287746a8a361bb890b57e22eb1294075a46214fae788666d3c4a7de3ed",
    "foreign_key_list:omnivia_projection_validations": "edd0732de63febda3be65cc08f9cb321e7989ee2a8c16c4b1a847f4b7388ad9d",
    "table_xinfo:omnivia_projection_activations": "cadd309fb3e491e158baa1fd76d17f98f0126fe2cf0c8be6c98b88eefbfb9cdf",
    "foreign_key_list:omnivia_projection_activations": "edd0732de63febda3be65cc08f9cb321e7989ee2a8c16c4b1a847f4b7388ad9d",
    "table_xinfo:omnivia_projection_ledger": "55c8d91a2628981649f9bcedd86fa736c2f7972a71f7b0900d5e470471bb71dd",
    "foreign_key_list:omnivia_projection_ledger": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    "index_xinfo:omnivia_idx_projection_runs_state_epoch": "6cb483797e06353fc833707d715d9fceb0ea5b7a2fb5a389543288c9e239e4ad",
    "index_xinfo:omnivia_idx_projection_checkpoints_latest": "9fed7aa000dfd8d099177e85753fef3a5604ff1bbb8e104653f0bb804b400b9b",
    "index_xinfo:omnivia_idx_projection_failures_record_code": "c66c94bba4513e09ae4a3211d496f951c6a4ee299b5f36e76bd2760ad26fe3ce",
    "index_xinfo:omnivia_idx_projection_validations_outcome": "43c66904d6f0d64b78a3637ae2f888475ea9b198e00f8f79210823053b40c6ae",
    "index_xinfo:omnivia_idx_projection_activations_history": "87c9ea3a58ac383608abbf898a520970b3602add785201ce89a59cc2e160bdaf",
}
LEDGER_COLUMN_SIGNATURE = (
    ("projection_id", "TEXT", 0, 1),
    ("version", "TEXT", 1, 0),
    ("rebuildable", "INTEGER", 1, 0),
    ("state", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
    ("fencing_generation", "INTEGER", 0, 0),
    ("projection_kind", "TEXT", 0, 0),
    ("schema_version", "TEXT", 0, 0),
    ("profile_version", "TEXT", 0, 0),
    ("active_run_id", "TEXT", 0, 0),
    ("active_epoch", "INTEGER", 0, 0),
    ("active_source_checkpoint", "TEXT", 0, 0),
    ("active_build_digest", "TEXT", 0, 0),
    ("active_validation_digest", "TEXT", 0, 0),
    ("activated_at_us", "INTEGER", 0, 0),
)
INVALID_REQUIRED_IDENTIFIERS = (None, "", "x" * 129, "bad space", "nul\x00id", b"id")
INVALID_OPTIONAL_IDENTIFIERS = ("", "x" * 129, "bad space", "nul\x00id", b"id")
INVALID_SEMANTIC_NAMES = (
    None,
    "",
    "x" * 129,
    "Bad.Name",
    "bad space",
    "nul\x00name",
    b"name",
)
INVALID_VERSION_TOKENS = (
    None,
    "",
    "x" * 129,
    "bad space",
    "nul\x00version",
    b"version",
)
INVALID_REQUIRED_TEXT = (None, "", "x" * 513, "nul\x00text", b"text")
INVALID_POSITIVE_INTEGERS = (None, -1, 0, 1.5, b"1")
INVALID_NONNEGATIVE_INTEGERS = (None, -1, 1.5, b"1")
INVALID_DIGESTS = (
    None,
    "",
    "md5:" + "a" * 32,
    "SHA256:" + "a" * 64,
    "sha256:" + "a" * 63,
    "sha256:" + "g" * 64,
    f"{DIGEST_A}\x00hidden",
    7,
    b"sha256:" + b"a" * 64,
)
INVALID_CANONICAL_JSON = (None, "", "{", '{"x": 1}', "[] ", b"{}")
INVALID_ACCEPTED = (None, -1, 2, 1.5, b"1")
INVALID_SANITIZED_DETAIL = ("x" * 4097, "bad\x00detail", b"detail")
RUN_SCALAR_CASES = [
    *(
        (field, value)
        for field in ("workspace_id", "projection_id", "run_id")
        for value in INVALID_REQUIRED_IDENTIFIERS
    ),
    *(("projection_kind", value) for value in INVALID_SEMANTIC_NAMES),
    *(
        (field, value)
        for field in ("schema_version", "profile_version")
        for value in INVALID_VERSION_TOKENS
    ),
    *(("source_checkpoint", value) for value in INVALID_REQUIRED_TEXT),
    *(
        (field, value)
        for field in ("target_epoch", "started_at_us")
        for value in INVALID_POSITIVE_INTEGERS
    ),
    *(("build_digest", value) for value in INVALID_DIGESTS if value is not None),
    ("state", "unknown"),
]
TABLE_SCALAR_CASES = [
    *(
        (table, field, value)
        for table in (
            "omnivia_projection_run_checkpoints",
            "omnivia_projection_record_failures",
            "omnivia_projection_validations",
            "omnivia_projection_activations",
        )
        for field in ("workspace_id", "projection_id", "run_id")
        for value in INVALID_REQUIRED_IDENTIFIERS
    ),
    *(
        ("omnivia_projection_run_checkpoints", field, value)
        for field in (
            "checkpoint_sequence",
            "input_record_count",
            "output_record_count",
        )
        for value in INVALID_NONNEGATIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_run_checkpoints", "created_at_us", value)
        for value in INVALID_POSITIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_run_checkpoints", "source_checkpoint", value)
        for value in INVALID_REQUIRED_TEXT
    ),
    *(
        ("omnivia_projection_run_checkpoints", "cursor_json", value)
        for value in INVALID_CANONICAL_JSON
    ),
    *(
        ("omnivia_projection_run_checkpoints", "checkpoint_digest", value)
        for value in INVALID_DIGESTS
    ),
    *(
        ("omnivia_projection_record_failures", "failure_sequence", value)
        for value in INVALID_NONNEGATIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_record_failures", "record_id", value)
        for value in INVALID_REQUIRED_IDENTIFIERS
    ),
    *(
        ("omnivia_projection_record_failures", field, value)
        for field in ("phase", "error_code")
        for value in INVALID_SEMANTIC_NAMES
    ),
    *(
        ("omnivia_projection_record_failures", "sanitized_detail", value)
        for value in INVALID_SANITIZED_DETAIL
    ),
    *(
        ("omnivia_projection_record_failures", "occurred_at_us", value)
        for value in INVALID_POSITIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_validations", "validated_at_us", value)
        for value in INVALID_POSITIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_validations", "accepted", value)
        for value in INVALID_ACCEPTED
    ),
    *(
        ("omnivia_projection_validations", field, value)
        for field in ("input_record_count", "output_record_count")
        for value in INVALID_NONNEGATIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_validations", field, value)
        for field in ("build_digest", "validation_digest")
        for value in INVALID_DIGESTS
    ),
    *(
        ("omnivia_projection_validations", "report_json", value)
        for value in INVALID_CANONICAL_JSON
    ),
    *(
        ("omnivia_projection_activations", "activation_sequence", value)
        for value in INVALID_NONNEGATIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_activations", "target_epoch", value)
        for value in INVALID_POSITIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_activations", "previous_run_id", value)
        for value in INVALID_OPTIONAL_IDENTIFIERS
    ),
    *(
        ("omnivia_projection_activations", "activated_at_us", value)
        for value in INVALID_POSITIVE_INTEGERS
    ),
    *(
        ("omnivia_projection_activations", "source_checkpoint", value)
        for value in INVALID_REQUIRED_TEXT
    ),
    *(
        ("omnivia_projection_activations", field, value)
        for field in ("build_digest", "validation_digest")
        for value in INVALID_DIGESTS
    ),
]
LEDGER_SCALAR_CASES = [
    *(
        ("projection_kind", value)
        for value in INVALID_SEMANTIC_NAMES
        if value is not None
    ),
    *(
        (field, value)
        for field in ("schema_version", "profile_version")
        for value in INVALID_VERSION_TOKENS
        if value is not None
    ),
    *(("active_run_id", value) for value in INVALID_OPTIONAL_IDENTIFIERS),
    *(
        ("active_epoch", value)
        for value in INVALID_POSITIVE_INTEGERS
        if value is not None
    ),
    *(
        ("active_source_checkpoint", value)
        for value in INVALID_REQUIRED_TEXT
        if value is not None
    ),
    *(
        (field, value)
        for field in ("active_build_digest", "active_validation_digest")
        for value in INVALID_DIGESTS
        if value is not None
    ),
    *(
        ("activated_at_us", value)
        for value in INVALID_POSITIVE_INTEGERS
        if value is not None
    ),
]
ACCEPTED_PREDECESSOR_HASHES = {
    **m4.ACCEPTED_PREDECESSOR_HASHES,
    10: "e5b4d7a27f5421a35b426e6da0ef8aacfe3f13c59ae2434bc9814b18bcd39dbe",
}


def migration_under_test() -> Any:
    found = [
        migration
        for migration in load_migrations()
        if migration.version == MIGRATION_VERSION
    ]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))

M5_RETRY_CHILD = """
import json
import sys
from pathlib import Path
import omnivia_core_runtime.storage.migrations as migration_module
from omnivia_core_runtime.storage.connection import (
    OpenMode, foreign_key_check, integrity_check, open_database,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations, load_migrations, read_workspace_state,
)

owned = tuple(migration for migration in load_migrations() if migration.version <= 11)
migration_module.load_migrations = lambda: owned
path, service, workspace_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
try:
    state = read_workspace_state(connection)
    applied = apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=service,
        fencing_generation=state.fencing_generation,
        workspace_id=workspace_id,
    )
    result = {
        "applied": [migration.version for migration in applied],
        "integrity": integrity_check(connection),
        "foreign_keys": foreign_key_check(connection),
    }
finally:
    connection.close()
print(json.dumps(result, sort_keys=True))
"""


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    seed_ledger(holder)
    yield holder
    holder.connection.close()


def guarded(holder: m2.Owned):
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def seed_ledger(holder: m2.Owned, projection_id: str = "projection-1") -> None:
    with guarded(holder):
        holder.connection.execute(
            "INSERT INTO omnivia_projection_ledger "
            "(projection_id, version, rebuildable, state, updated_at, "
            "fencing_generation) VALUES (?, 'legacy-v1', 1, 'ready', "
            "'2026-08-03T00:00:00Z', ?)",
            (projection_id, holder.generation),
        )


def run_row(
    run_id: str = "run-1",
    *,
    projection_id: str = "projection-1",
    epoch: int = 1,
    checkpoint: str = "source:1",
    started_at: int = BASE_US,
    schema_version: str = "schema-v1",
    profile_version: str = "profile-v1",
    resumed_from: tuple[str, int] | None = None,
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "projection_id": projection_id,
        "run_id": run_id,
        "projection_kind": "knowledge.search",
        "schema_version": schema_version,
        "profile_version": profile_version,
        "source_checkpoint": checkpoint,
        "target_epoch": epoch,
        "started_at_us": started_at,
        "validation_started_at_us": None,
        "finished_at_us": None,
        "state": "running",
        "input_record_count": None,
        "output_record_count": None,
        "build_digest": None,
        "error_json": None,
        "resumed_from_run_id": None if resumed_from is None else resumed_from[0],
        "resumed_from_checkpoint_sequence": (
            None if resumed_from is None else resumed_from[1]
        ),
    }


def checkpoint_row(
    run_id: str = "run-1",
    *,
    sequence: int = 0,
    at: int = BASE_US + 1,
    input_count: int = 2,
    output_count: int = 2,
    source_checkpoint: str = "source:1",
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "projection_id": "projection-1",
        "run_id": run_id,
        "checkpoint_sequence": sequence,
        "created_at_us": at,
        "source_checkpoint": source_checkpoint,
        "cursor_json": json.dumps({"offset": sequence}, separators=(",", ":")),
        "checkpoint_digest": DIGEST_A,
        "input_record_count": input_count,
        "output_record_count": output_count,
    }


def failure_row(
    run_id: str = "run-1", *, sequence: int = 0, at: int = BASE_US + 1
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "projection_id": "projection-1",
        "run_id": run_id,
        "failure_sequence": sequence,
        "record_id": f"record-{sequence}",
        "phase": "build.parse",
        "error_code": "record.invalid",
        "sanitized_detail": "invalid source record",
        "occurred_at_us": at,
    }


def start_run(holder: m2.Owned, **kwargs: Any) -> None:
    with guarded(holder):
        insert(holder.connection, "omnivia_projection_runs", run_row(**kwargs))


def begin_validation(
    holder: m2.Owned, run_id: str = "run-1", *, at: int = BASE_US + 2
) -> None:
    with guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'validating', "
            "validation_started_at_us = ? WHERE workspace_id = ? "
            "AND projection_id = 'projection-1' AND run_id = ?",
            (at, WORKSPACE_ID, run_id),
        )


def validate(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    accepted: int = 1,
    at: int = BASE_US + 3,
    build_digest: str = DIGEST_A,
    validation_digest: str = DIGEST_B,
    input_count: int = 2,
    output_count: int = 2,
) -> None:
    with guarded(holder):
        insert(
            holder.connection,
            "omnivia_projection_validations",
            {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "run_id": run_id,
                "validated_at_us": at,
                "accepted": accepted,
                "input_record_count": input_count,
                "output_record_count": output_count,
                "build_digest": build_digest,
                "report_json": '{"ok":true}' if accepted else '{"ok":false}',
                "validation_digest": validation_digest,
            },
        )


def finish_success(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    at: int = BASE_US + 4,
    build_digest: str = DIGEST_A,
    input_count: int = 2,
    output_count: int = 2,
) -> None:
    with guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'succeeded', "
            "finished_at_us = ?, input_record_count = ?, output_record_count = ?, "
            "build_digest = ? WHERE workspace_id = ? "
            "AND projection_id = 'projection-1' AND run_id = ?",
            (at, input_count, output_count, build_digest, WORKSPACE_ID, run_id),
        )


def complete_run(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    epoch: int = 1,
    checkpoint: str = "source:1",
    start: int = BASE_US,
    build_digest: str = DIGEST_A,
    validation_digest: str = DIGEST_B,
    schema_version: str = "schema-v1",
    profile_version: str = "profile-v1",
) -> None:
    start_run(
        holder,
        run_id=run_id,
        epoch=epoch,
        checkpoint=checkpoint,
        started_at=start,
        schema_version=schema_version,
        profile_version=profile_version,
    )
    begin_validation(holder, run_id, at=start + 2)
    validate(
        holder,
        run_id,
        at=start + 3,
        build_digest=build_digest,
        validation_digest=validation_digest,
    )
    finish_success(holder, run_id, at=start + 4, build_digest=build_digest)


def activate(
    holder: m2.Owned,
    run_id: str = "run-1",
    *,
    sequence: int = 0,
    epoch: int = 1,
    previous: str | None = None,
    at: int = BASE_US + 5,
    checkpoint: str = "source:1",
    build_digest: str = DIGEST_A,
    validation_digest: str = DIGEST_B,
) -> None:
    with guarded(holder):
        insert(
            holder.connection,
            "omnivia_projection_activations",
            {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "activation_sequence": sequence,
                "run_id": run_id,
                "target_epoch": epoch,
                "previous_run_id": previous,
                "activated_at_us": at,
                "source_checkpoint": checkpoint,
                "build_digest": build_digest,
                "validation_digest": validation_digest,
            },
        )


def test_m5_01_lineage_and_immutable_predecessors() -> None:
    migrations = [
        migration for migration in load_migrations() if migration.version <= 11
    ]
    assert [migration.version for migration in migrations] == list(range(1, 12))
    assert migrations[-1].name == MIGRATION_NAME
    assert {m.version: m.checksum for m in migrations[:-1]} == (
        ACCEPTED_PREDECESSOR_HASHES
    )
    assert MIGRATION.checksum == hashlib.sha256(MIGRATION.sql.encode()).hexdigest()


def test_m5_02_pristine_convergence_and_exact_inventory(owned: m2.Owned) -> None:
    assert max(applied_migrations(owned.connection)) == MIGRATION_VERSION
    assert owned.connection.execute("PRAGMA user_version").fetchone() == (11,)
    before = sqlite3.connect(":memory:")
    try:
        before.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version <= 10:
                before.executescript(migration.sql)
        assert (
            m2.object_names(owned.connection, "table")
            - m2.object_names(before, "table")
            == TABLES
        )
        assert (
            m2.object_names(owned.connection, "index")
            - m2.object_names(before, "index")
            == INDEXES
        )
        assert (
            m2.object_names(owned.connection, "trigger")
            - m2.object_names(before, "trigger")
            == TRIGGERS
        )
        assert m2.object_names(owned.connection, "view") == m2.object_names(
            before, "view"
        )
    finally:
        before.close()
    exact_schema_names = {*TABLES, *INDEXES, *TRIGGERS, "omnivia_projection_ledger"}
    actual_schema_hashes = {}
    for name in exact_schema_names:
        row = owned.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = ?", (name,)
        ).fetchone()
        assert row is not None and row[0] is not None, name
        actual_schema_hashes[name] = hashlib.sha256(str(row[0]).encode()).hexdigest()
    assert actual_schema_hashes == SCHEMA_SQL_SHA256
    actual_pragma_hashes = {}
    for table in (*sorted(TABLES), "omnivia_projection_ledger"):
        for pragma in ("table_xinfo", "foreign_key_list"):
            rows = owned.connection.execute(f'PRAGMA {pragma}("{table}")').fetchall()
            actual_pragma_hashes[f"{pragma}:{table}"] = hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode()
            ).hexdigest()
    for index in sorted(INDEXES):
        rows = owned.connection.execute(f"PRAGMA index_xinfo('{index}')").fetchall()
        actual_pragma_hashes[f"index_xinfo:{index}"] = hashlib.sha256(
            json.dumps(rows, separators=(",", ":")).encode()
        ).hexdigest()
    assert actual_pragma_hashes == PRAGMA_SHAPE_SHA256
    signature = tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in owned.connection.execute(
            "PRAGMA table_xinfo(omnivia_projection_ledger)"
        )
    )
    assert signature == LEDGER_COLUMN_SIGNATURE


def test_m5_03_adopted_rows_are_preserved_without_synthetic_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(10):
        m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    seed_ledger(holder, "legacy-projection")
    excluded = {"omnivia_schema_migrations", "omnivia_migration_attempts"}
    old_tables = m2.object_names(holder.connection, "table") - excluded
    old_columns = {
        table: [
            str(row[1])
            for row in holder.connection.execute(f'PRAGMA table_info("{table}")')
        ]
        for table in old_tables
    }

    def rows(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
        columns = old_columns[table]
        quoted = ", ".join(f'"{column}"' for column in columns)
        return connection.execute(
            f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}'
        ).fetchall()

    before = {table: rows(holder.connection, table) for table in old_tables}
    holder.connection.close()
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = m2.read_workspace_state(connection)
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m2.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    assert {table: rows(connection, table) for table in old_tables} == before
    assert (
        connection.execute(
            "SELECT projection_kind, schema_version, profile_version, active_run_id, "
            "active_epoch, active_source_checkpoint, active_build_digest, "
            "active_validation_digest, activated_at_us "
            "FROM omnivia_projection_ledger WHERE projection_id = 'legacy-projection'"
        ).fetchone()
        == (None,) * 9
    )
    for table in TABLES:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    connection.close()


@pytest.mark.parametrize(("field", "value"), RUN_SCALAR_CASES)
def test_m5_04_run_scalar_matrices_fail_closed(
    owned: m2.Owned, field: str, value: object
) -> None:
    row = run_row()
    row[field] = value
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned.connection, "omnivia_projection_runs", row)


@pytest.mark.parametrize(
    "surface",
    [
        "run_projection_kind",
        "run_schema_version",
        "run_profile_version",
        "checkpoint_digest",
        "failure_record_id",
        "failure_phase",
        "failure_error_code",
        "validation_build_digest",
        "validation_digest",
    ],
)
def test_m5_04a_embedded_nul_text_fields_fail_closed(
    owned: m2.Owned, surface: str
) -> None:
    hidden_suffix = "\x00hidden"
    if surface.startswith("run_"):
        field = surface.removeprefix("run_")
        row = run_row()
        row[field] = f"{row[field]}{hidden_suffix}"
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_runs", row)
        return

    start_run(owned)
    if surface == "checkpoint_digest":
        row = checkpoint_row()
        row["checkpoint_digest"] = f"{DIGEST_A}{hidden_suffix}"
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_run_checkpoints", row)
        return

    if surface.startswith("failure_"):
        field = surface.removeprefix("failure_")
        row = failure_row()
        row[field] = f"{row[field]}{hidden_suffix}"
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_record_failures", row)
        return

    begin_validation(owned)
    if surface == "validation_build_digest":
        with pytest.raises(sqlite3.IntegrityError):
            validate(owned, build_digest=f"{DIGEST_A}{hidden_suffix}")
        return
    with pytest.raises(sqlite3.IntegrityError):
        validate(owned, validation_digest=f"{DIGEST_B}{hidden_suffix}")


@pytest.mark.parametrize(("table", "field", "value"), TABLE_SCALAR_CASES)
def test_m5_04b_all_table_scalar_matrices_fail_closed(
    owned: m2.Owned, table: str, field: str, value: object
) -> None:
    if table in {
        "omnivia_projection_run_checkpoints",
        "omnivia_projection_record_failures",
        "omnivia_projection_validations",
    }:
        start_run(owned)
    if table == "omnivia_projection_validations":
        begin_validation(owned)
    if table == "omnivia_projection_activations":
        complete_run(owned)

    rows: dict[str, dict[str, object]] = {
        "omnivia_projection_run_checkpoints": checkpoint_row(),
        "omnivia_projection_record_failures": failure_row(),
        "omnivia_projection_validations": {
            "workspace_id": WORKSPACE_ID,
            "projection_id": "projection-1",
            "run_id": "run-1",
            "validated_at_us": BASE_US + 3,
            "accepted": 1,
            "input_record_count": 2,
            "output_record_count": 2,
            "build_digest": DIGEST_A,
            "report_json": '{"ok":true}',
            "validation_digest": DIGEST_B,
        },
        "omnivia_projection_activations": {
            "workspace_id": WORKSPACE_ID,
            "projection_id": "projection-1",
            "activation_sequence": 0,
            "run_id": "run-1",
            "target_epoch": 1,
            "previous_run_id": None,
            "activated_at_us": BASE_US + 5,
            "source_checkpoint": "source:1",
            "build_digest": DIGEST_A,
            "validation_digest": DIGEST_B,
        },
    }
    row = rows[table]
    row[field] = value
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned.connection, table, row)


@pytest.mark.parametrize(("field", "value"), LEDGER_SCALAR_CASES)
def test_m5_04c_ledger_pointer_scalar_constraints_are_independent(
    owned: m2.Owned, field: str, value: object
) -> None:
    trigger = "omnivia_guard_projection_ledger_active_pointer_update"
    trigger_sql = owned.connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
        (trigger,),
    ).fetchone()[0]
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(f'DROP TRIGGER "{trigger}"')
    with authorised(owned.connection, mutations=True):
        owned.connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(
                f'UPDATE omnivia_projection_ledger SET "{field}" = ? '
                "WHERE projection_id = 'projection-1'",
                (value,),
            )
        owned.connection.execute("ROLLBACK")
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(trigger_sql)


@pytest.mark.parametrize(
    ("surface", "byte_limit"),
    [
        ("error_json", 65_536),
        ("cursor_json", 1_048_576),
        ("report_json", 1_048_576),
    ],
)
@pytest.mark.parametrize("over_limit", [False, True])
def test_m5_04d_json_byte_boundaries_have_positive_and_negative_controls(
    owned: m2.Owned, surface: str, byte_limit: int, over_limit: bool
) -> None:
    target_size = byte_limit + int(over_limit)
    value = '"' + ("x" * (target_size - 2)) + '"'
    assert len(value.encode()) == target_size

    start_run(owned)
    if surface == "report_json":
        begin_validation(owned)

    def write() -> None:
        if surface == "error_json":
            owned.connection.execute(
                "UPDATE omnivia_projection_runs SET state = 'failed', "
                "finished_at_us = ?, error_json = ? WHERE run_id = 'run-1'",
                (BASE_US + 1, value),
            )
        elif surface == "cursor_json":
            row = checkpoint_row()
            row["cursor_json"] = value
            insert(owned.connection, "omnivia_projection_run_checkpoints", row)
        else:
            row = {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "run_id": "run-1",
                "validated_at_us": BASE_US + 3,
                "accepted": 1,
                "input_record_count": 2,
                "output_record_count": 2,
                "build_digest": DIGEST_A,
                "report_json": value,
                "validation_digest": DIGEST_B,
            }
            insert(owned.connection, "omnivia_projection_validations", row)

    if over_limit:
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            write()
    else:
        with guarded(owned):
            write()
        table, field = {
            "error_json": ("omnivia_projection_runs", "error_json"),
            "cursor_json": (
                "omnivia_projection_run_checkpoints",
                "cursor_json",
            ),
            "report_json": ("omnivia_projection_validations", "report_json"),
        }[surface]
        assert owned.connection.execute(
            f"SELECT length(CAST({field} AS BLOB)) FROM {table}"
        ).fetchone() == (byte_limit,)


def test_m5_05_run_insert_shape_and_lifecycle(owned: m2.Owned) -> None:
    start_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="start"):
        row = run_row("run-bad", epoch=2)
        row.update(
            {
                "state": "validating",
                "validation_started_at_us": BASE_US + 1,
            }
        )
        insert(owned.connection, "omnivia_projection_runs", row)
    begin_validation(owned)
    validate(owned)
    finish_success(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="transition"):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            "error_json = '{\"code\":\"late\"}' WHERE run_id = 'run-1'"
        )


def test_m5_06_failure_closeout_requires_canonical_error(owned: m2.Owned) -> None:
    start_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            "finished_at_us = ?, error_json = ? WHERE run_id = 'run-1'",
            (BASE_US + 1, '{ "code": "bad" }'),
        )
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            "finished_at_us = ?, error_json = ? WHERE run_id = 'run-1'",
            (BASE_US + 1, '{"code":"bad"}'),
        )


def test_m5_07_resume_pair_requires_same_projection_checkpoint(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', "
            'finished_at_us = ?, error_json = \'{"code":"retry"}\' '
            "WHERE run_id = 'run-1'",
            (BASE_US + 2,),
        )
    start_run(
        owned,
        run_id="run-2",
        epoch=1,
        started_at=BASE_US + 3,
        resumed_from=("run-1", 0),
    )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="resume"):
        insert(
            owned.connection,
            "omnivia_projection_runs",
            run_row(
                "run-3",
                epoch=2,
                checkpoint="source:2",
                started_at=BASE_US + 4,
                resumed_from=("run-1", 0),
            ),
        )


def test_m5_08_checkpoint_contiguity_time_and_counts(owned: m2.Owned) -> None:
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(
                sequence=1,
                at=BASE_US + 2,
                input_count=3,
                output_count=2,
            ),
        )
    for bad in (
        checkpoint_row(sequence=3, at=BASE_US + 3),
        checkpoint_row(sequence=2, at=BASE_US, input_count=3),
        checkpoint_row(sequence=2, at=BASE_US + 3, input_count=1),
    ):
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, "omnivia_projection_run_checkpoints", bad)


def test_m5_09_record_failures_are_contiguous_and_state_bound(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    with guarded(owned):
        insert(owned.connection, "omnivia_projection_record_failures", failure_row())
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned.connection,
            "omnivia_projection_record_failures",
            failure_row(sequence=2, at=BASE_US + 2),
        )
    begin_validation(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_record_failures",
            failure_row(sequence=1, at=BASE_US + 2),
        )
    validate(owned, accepted=0, at=BASE_US + 3)
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', finished_at_us = ?, "
            "error_json = '{\"code\":\"validation\"}' WHERE run_id = 'run-1'",
            (BASE_US + 4,),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="requires"):
        insert(
            owned.connection,
            "omnivia_projection_record_failures",
            failure_row(sequence=2, at=BASE_US + 5),
        )


def test_m5_10_validation_timing_uniqueness_and_report(owned: m2.Owned) -> None:
    start_run(owned)
    with pytest.raises(sqlite3.IntegrityError, match="requires"):
        validate(owned)
    begin_validation(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(
            owned.connection,
            "omnivia_projection_validations",
            {
                "workspace_id": WORKSPACE_ID,
                "projection_id": "projection-1",
                "run_id": "run-1",
                "validated_at_us": BASE_US + 3,
                "accepted": 1,
                "input_record_count": 2,
                "output_record_count": 2,
                "build_digest": DIGEST_A,
                "report_json": '{ "ok": true }',
                "validation_digest": DIGEST_B,
            },
        )
    validate(owned)
    with pytest.raises(sqlite3.IntegrityError):
        validate(owned)


def test_m5_11_success_requires_accepted_matching_validation(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    begin_validation(owned)
    validate(owned, accepted=0)
    with pytest.raises(sqlite3.IntegrityError, match="accepted"):
        finish_success(owned)


@pytest.mark.parametrize("accepted", [0, 1])
@pytest.mark.parametrize("evidence", ["checkpoint", "failure"])
@pytest.mark.parametrize("at", [BASE_US + 1, BASE_US + 3, BASE_US + 4])
def test_m5_11a_validation_closes_the_evidence_frontier(
    owned: m2.Owned, accepted: int, evidence: str, at: int
) -> None:
    start_run(owned)
    begin_validation(owned)
    validate(owned, accepted=accepted)
    run_before = owned.connection.execute(
        "SELECT * FROM omnivia_projection_runs WHERE run_id = 'run-1'"
    ).fetchone()
    counts_before = (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_projection_run_checkpoints"
        ).fetchone()[0],
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_projection_record_failures"
        ).fetchone()[0],
    )
    table, row = (
        ("omnivia_projection_run_checkpoints", checkpoint_row(at=at))
        if evidence == "checkpoint"
        else ("omnivia_projection_record_failures", failure_row(at=at))
    )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="validation"):
        insert(owned.connection, table, row)
    assert (
        owned.connection.execute(
            "SELECT * FROM omnivia_projection_runs WHERE run_id = 'run-1'"
        ).fetchone()
        == run_before
    )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_projection_run_checkpoints"
        ).fetchone()[0],
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_projection_record_failures"
        ).fetchone()[0],
    ) == counts_before
    assert owned.connection.execute(
        "SELECT active_run_id FROM omnivia_projection_ledger"
    ).fetchone() == (None,)


@pytest.mark.parametrize("terminal_state", ["succeeded", "failed"])
def test_m5_11b_validation_start_time_is_immutable_at_closeout(
    owned: m2.Owned, terminal_state: str
) -> None:
    start_run(owned)
    begin_validation(owned)
    validate(owned, accepted=int(terminal_state == "succeeded"))
    if terminal_state == "succeeded":
        assignment = (
            "state = 'succeeded', validation_started_at_us = ?, finished_at_us = ?, "
            "input_record_count = 2, output_record_count = 2, build_digest = ?"
        )
        parameters: tuple[object, ...] = (BASE_US + 1, BASE_US + 4, DIGEST_A)
    else:
        assignment = (
            "state = 'failed', validation_started_at_us = ?, finished_at_us = ?, "
            'error_json = \'{"code":"validation"}\''
        )
        parameters = (BASE_US + 1, BASE_US + 4)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="validation"):
        owned.connection.execute(
            f"UPDATE omnivia_projection_runs SET {assignment} WHERE run_id = 'run-1'",
            parameters,
        )


@pytest.mark.parametrize("accepted", [0, 1])
@pytest.mark.parametrize("closeout_delta", [-1, 0, 1])
def test_m5_11c_failed_closeout_follows_validation_decision(
    owned: m2.Owned, accepted: int, closeout_delta: int
) -> None:
    start_run(owned)
    begin_validation(owned, at=BASE_US + 2)
    validate(owned, accepted=accepted, at=BASE_US + 3)
    statement = (
        "UPDATE omnivia_projection_runs SET state = 'failed', finished_at_us = ?, "
        "error_json = '{\"code\":\"validation\"}' WHERE run_id = 'run-1'"
    )
    parameters = (BASE_US + 3 + closeout_delta,)
    if closeout_delta < 0:
        with (
            guarded(owned),
            pytest.raises(sqlite3.IntegrityError, match="validation decision"),
        ):
            owned.connection.execute(statement, parameters)
        assert owned.connection.execute(
            "SELECT state, finished_at_us FROM omnivia_projection_runs "
            "WHERE run_id = 'run-1'"
        ).fetchone() == ("validating", None)
    else:
        with guarded(owned):
            owned.connection.execute(statement, parameters)
        assert owned.connection.execute(
            "SELECT state, finished_at_us FROM omnivia_projection_runs "
            "WHERE run_id = 'run-1'"
        ).fetchone() == ("failed", BASE_US + 3 + closeout_delta)


@pytest.mark.parametrize("path", ["validating", "running_invents_validation_start"])
def test_m5_11d_failed_closeout_requires_decision_after_validation_start(
    owned: m2.Owned, path: str
) -> None:
    start_run(owned)
    if path == "validating":
        begin_validation(owned, at=BASE_US + 2)
        assignment = (
            "state = 'failed', finished_at_us = ?, "
            'error_json = \'{"code":"missing_decision"}\''
        )
        expected_state = "validating"
    else:
        assignment = (
            "state = 'failed', validation_started_at_us = ?, finished_at_us = ?, "
            'error_json = \'{"code":"invented_validation_start"}\''
        )
        expected_state = "running"

    parameters = (BASE_US + 3,) if path == "validating" else (BASE_US + 2, BASE_US + 3)
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="requires validation decision"),
    ):
        owned.connection.execute(
            f"UPDATE omnivia_projection_runs SET {assignment} WHERE run_id = 'run-1'",
            parameters,
        )
    assert owned.connection.execute(
        "SELECT state, validation_started_at_us, finished_at_us "
        "FROM omnivia_projection_runs WHERE run_id = 'run-1'"
    ).fetchone() == (
        expected_state,
        BASE_US + 2 if path == "validating" else None,
        None,
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "late_evidence",
        "finished_before_validation",
        "count_mismatch",
    ],
)
def test_m5_11e_activation_revalidates_persisted_history(
    owned: m2.Owned, tamper: str
) -> None:
    start_run(owned)
    begin_validation(owned)
    validate(owned)
    trigger_names = ["omnivia_guard_projection_runs_update"]
    if tamper == "late_evidence":
        trigger_names.append("omnivia_guard_projection_run_checkpoints_insert")
    trigger_sql = {
        name: owned.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()[0]
        for name in trigger_names
    }
    with authorised(owned.connection, ddl=True):
        for name in trigger_names:
            owned.connection.execute(f'DROP TRIGGER "{name}"')
    try:
        with authorised(owned.connection, mutations=True):
            owned.connection.execute("BEGIN IMMEDIATE")
            try:
                validation_start = BASE_US + 2
                finished_at = BASE_US + 4
                input_count = 2
                if tamper == "late_evidence":
                    insert(
                        owned.connection,
                        "omnivia_projection_run_checkpoints",
                        checkpoint_row(at=BASE_US + 4),
                    )
                    finished_at = BASE_US + 5
                elif tamper == "finished_before_validation":
                    finished_at = BASE_US + 2
                else:
                    input_count = 3
                owned.connection.execute(
                    "UPDATE omnivia_projection_runs SET state = 'succeeded', "
                    "validation_started_at_us = ?, finished_at_us = ?, "
                    "input_record_count = ?, output_record_count = 2, build_digest = ? "
                    "WHERE run_id = 'run-1'",
                    (validation_start, finished_at, input_count, DIGEST_A),
                )
                owned.connection.execute("COMMIT")
            except BaseException:
                if owned.connection.in_transaction:
                    owned.connection.execute("ROLLBACK")
                raise
    finally:
        with authorised(owned.connection, ddl=True):
            for name in trigger_names:
                owned.connection.execute(trigger_sql[name])
    with pytest.raises(sqlite3.IntegrityError, match="activation requires"):
        activate(owned, at=max(finished_at, BASE_US + 3) + 1)
    assert owned.connection.execute(
        "SELECT active_run_id FROM omnivia_projection_ledger"
    ).fetchone() == (None,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_projection_activations"
    ).fetchone() == (0,)


def test_m5_12_first_activation_populates_exact_pointer(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    assert owned.connection.execute(
        "SELECT projection_kind, schema_version, profile_version, active_run_id, "
        "active_epoch, active_source_checkpoint, active_build_digest, "
        "active_validation_digest, activated_at_us FROM omnivia_projection_ledger "
        "WHERE projection_id = 'projection-1'"
    ).fetchone() == (
        "knowledge.search",
        "schema-v1",
        "profile-v1",
        "run-1",
        1,
        "source:1",
        DIGEST_A,
        DIGEST_B,
        BASE_US + 5,
    )


def test_m5_13_later_activation_atomically_supersedes(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    run_columns = [
        str(row[1])
        for row in owned.connection.execute(
            "PRAGMA table_info(omnivia_projection_runs)"
        )
    ]
    run_one_before = dict(
        zip(
            run_columns,
            owned.connection.execute(
                "SELECT * FROM omnivia_projection_runs WHERE run_id = 'run-1'"
            ).fetchone(),
            strict=True,
        )
    )
    complete_run(
        owned,
        "run-2",
        epoch=2,
        checkpoint="source:2",
        start=BASE_US + 10,
        build_digest=DIGEST_B,
        validation_digest=DIGEST_C,
        schema_version="schema-v2",
        profile_version="profile-v2",
    )
    activate(
        owned,
        "run-2",
        sequence=1,
        epoch=2,
        previous="run-1",
        at=BASE_US + 15,
        checkpoint="source:2",
        build_digest=DIGEST_B,
        validation_digest=DIGEST_C,
    )
    run_one_after = dict(
        zip(
            run_columns,
            owned.connection.execute(
                "SELECT * FROM omnivia_projection_runs WHERE run_id = 'run-1'"
            ).fetchone(),
            strict=True,
        )
    )
    expected_run_one = dict(run_one_before)
    expected_run_one["state"] = "superseded"
    assert run_one_after == expected_run_one
    assert owned.connection.execute(
        "SELECT run_id, state FROM omnivia_projection_runs ORDER BY run_id"
    ).fetchall() == [("run-1", "superseded"), ("run-2", "succeeded")]
    assert owned.connection.execute(
        "SELECT active_run_id, active_epoch, schema_version, profile_version "
        "FROM omnivia_projection_ledger"
    ).fetchone() == ("run-2", 2, "schema-v2", "profile-v2")


def test_m5_14_rejected_failed_partial_never_replace_pointer(
    owned: m2.Owned,
) -> None:
    complete_run(owned)
    activate(owned)
    start_run(
        owned,
        run_id="run-2",
        epoch=2,
        checkpoint="source:2",
        started_at=BASE_US + 10,
    )
    begin_validation(owned, "run-2", at=BASE_US + 12)
    validate(owned, "run-2", accepted=0, at=BASE_US + 13)
    with pytest.raises(sqlite3.IntegrityError, match="succeeded"):
        activate(
            owned,
            "run-2",
            sequence=1,
            epoch=2,
            previous="run-1",
            at=BASE_US + 15,
            checkpoint="source:2",
        )
    assert owned.connection.execute(
        "SELECT active_run_id, active_epoch FROM omnivia_projection_ledger"
    ).fetchone() == ("run-1", 1)


def test_m5_15_direct_pointer_and_duplicate_activation_refused(
    owned: m2.Owned,
) -> None:
    complete_run(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="activation"):
        owned.connection.execute(
            "UPDATE omnivia_projection_ledger SET active_run_id = 'run-1', "
            "active_epoch = 1 WHERE projection_id = 'projection-1'"
        )
    activate(owned)
    with pytest.raises(sqlite3.IntegrityError):
        activate(owned)


def test_m5_16_all_tables_refuse_same_value_update_and_delete(
    owned: m2.Owned,
) -> None:
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
        insert(owned.connection, "omnivia_projection_record_failures", failure_row())
    begin_validation(owned)
    validate(owned)
    finish_success(owned)
    activate(owned)
    keys = {
        "omnivia_projection_runs": "run_id = 'run-1'",
        "omnivia_projection_run_checkpoints": "run_id = 'run-1'",
        "omnivia_projection_record_failures": "run_id = 'run-1'",
        "omnivia_projection_validations": "run_id = 'run-1'",
        "omnivia_projection_activations": "run_id = 'run-1'",
    }
    for table, where in keys.items():
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(
                f"UPDATE {table} SET workspace_id = workspace_id WHERE {where}"
            )
        with guarded(owned), pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(f"DELETE FROM {table} WHERE {where}")


@pytest.mark.parametrize("verb", ["INSERT OR REPLACE INTO", "REPLACE INTO"])
@pytest.mark.parametrize(
    "table",
    [
        "omnivia_projection_runs",
        "omnivia_projection_run_checkpoints",
        "omnivia_projection_record_failures",
        "omnivia_projection_validations",
        "omnivia_projection_activations",
        "omnivia_projection_ledger",
    ],
)
def test_m5_16a_all_durable_identities_refuse_replace_algorithms(
    owned: m2.Owned, table: str, verb: str
) -> None:
    assert owned.connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    start_run(owned)
    with guarded(owned):
        insert(
            owned.connection,
            "omnivia_projection_run_checkpoints",
            checkpoint_row(),
        )
        insert(owned.connection, "omnivia_projection_record_failures", failure_row())
    begin_validation(owned)
    validate(owned)
    finish_success(owned)
    activate(owned)

    columns = [
        str(row[1])
        for row in owned.connection.execute(f'PRAGMA table_xinfo("{table}")')
        if int(row[6]) == 0
    ]
    quoted = ", ".join(f'"{column}"' for column in columns)
    before = owned.connection.execute(f'SELECT {quoted} FROM "{table}"').fetchone()
    assert before is not None
    placeholders = ", ".join("?" for _ in columns)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="identity"):
        owned.connection.execute(
            f'{verb} "{table}" ({quoted}) VALUES ({placeholders})', before
        )
    assert (
        owned.connection.execute(f'SELECT {quoted} FROM "{table}"').fetchone() == before
    )


def test_m5_16b_replace_cannot_turn_rejected_validation_into_authority(
    owned: m2.Owned,
) -> None:
    assert owned.connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    start_run(owned)
    begin_validation(owned)
    validate(owned, accepted=0)
    rejected = owned.connection.execute(
        "SELECT * FROM omnivia_projection_validations WHERE run_id = 'run-1'"
    ).fetchone()
    assert rejected is not None and rejected[4] == 0
    accepted = list(rejected)
    accepted[4] = 1
    columns = [
        str(row[1])
        for row in owned.connection.execute(
            "PRAGMA table_xinfo(omnivia_projection_validations)"
        )
        if int(row[6]) == 0
    ]
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="identity"):
        owned.connection.execute(
            "INSERT OR REPLACE INTO omnivia_projection_validations "
            f"({quoted}) VALUES ({placeholders})",
            accepted,
        )
    assert owned.connection.execute(
        "SELECT accepted FROM omnivia_projection_validations WHERE run_id = 'run-1'"
    ).fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError, match="succeeded"):
        finish_success(owned)
    with pytest.raises(sqlite3.IntegrityError, match="succeeded"):
        activate(owned)
    assert owned.connection.execute(
        "SELECT active_run_id, active_epoch FROM omnivia_projection_ledger"
    ).fetchone() == (None, None)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_projection_activations"
    ).fetchone() == (0,)


def test_m5_16c_replace_cannot_reset_failed_run_history(owned: m2.Owned) -> None:
    assert owned.connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
    start_run(owned)
    with guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'failed', finished_at_us = ?, "
            "error_json = '{\"code\":\"first\"}' WHERE run_id = 'run-1'",
            (BASE_US + 1,),
        )
    columns = [
        str(row[1])
        for row in owned.connection.execute(
            "PRAGMA table_xinfo(omnivia_projection_runs)"
        )
        if int(row[6]) == 0
    ]
    quoted = ", ".join(f'"{column}"' for column in columns)
    before = owned.connection.execute(
        f"SELECT {quoted} FROM omnivia_projection_runs WHERE run_id = 'run-1'"
    ).fetchone()
    assert before is not None
    rewritten = run_row()
    rewritten["started_at_us"] = BASE_US + 10
    rewritten["source_checkpoint"] = "source:rewritten"
    placeholders = ", ".join("?" for _ in columns)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="identity"):
        owned.connection.execute(
            "INSERT OR REPLACE INTO omnivia_projection_runs "
            f"({quoted}) VALUES ({placeholders})",
            tuple(rewritten[column] for column in columns),
        )
    assert (
        owned.connection.execute(
            f"SELECT {quoted} FROM omnivia_projection_runs WHERE run_id = 'run-1'"
        ).fetchone()
        == before
    )


@pytest.mark.parametrize("table", sorted(TABLES))
@pytest.mark.parametrize("authority", ["stale", "closed", "stock"])
def test_m5_17_all_tables_refuse_invalid_authority(
    owned: m2.Owned, table: str, authority: str
) -> None:
    if table in {
        "omnivia_projection_run_checkpoints",
        "omnivia_projection_record_failures",
        "omnivia_projection_validations",
    }:
        start_run(owned)
    if table == "omnivia_projection_validations":
        begin_validation(owned)
    if table == "omnivia_projection_activations":
        complete_run(owned)

    rows: dict[str, dict[str, object]] = {
        "omnivia_projection_runs": run_row(),
        "omnivia_projection_run_checkpoints": checkpoint_row(),
        "omnivia_projection_record_failures": failure_row(),
        "omnivia_projection_validations": {
            "workspace_id": WORKSPACE_ID,
            "projection_id": "projection-1",
            "run_id": "run-1",
            "validated_at_us": BASE_US + 3,
            "accepted": 1,
            "input_record_count": 2,
            "output_record_count": 2,
            "build_digest": DIGEST_A,
            "report_json": '{"ok":true}',
            "validation_digest": DIGEST_B,
        },
        "omnivia_projection_activations": {
            "workspace_id": WORKSPACE_ID,
            "projection_id": "projection-1",
            "activation_sequence": 0,
            "run_id": "run-1",
            "target_epoch": 1,
            "previous_run_id": None,
            "activated_at_us": BASE_US + 5,
            "source_checkpoint": "source:1",
            "build_digest": DIGEST_A,
            "validation_digest": DIGEST_B,
        },
    }
    row = rows[table]

    if authority == "stale":

        class RollbackProbe(Exception):
            pass

        row_count_before = owned.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        pointer_before = owned.connection.execute(
            "SELECT projection_kind, schema_version, profile_version, active_run_id, "
            "active_epoch, active_source_checkpoint, active_build_digest, "
            "active_validation_digest, activated_at_us "
            "FROM omnivia_projection_ledger WHERE projection_id = 'projection-1'"
        ).fetchone()
        with (
            pytest.raises(RollbackProbe),
            fenced_transaction(
                owned.connection,
                owned.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=owned.generation,
            ),
        ):
            with authorised(owned.connection, mutations=True):
                owned.connection.execute(
                    "UPDATE omnivia_mutation_guard SET fencing_generation = "
                    "fencing_generation + 1 WHERE singleton = 1"
                )
                assert owned.connection.execute(
                    "SELECT fencing_generation FROM omnivia_mutation_guard "
                    "WHERE singleton = 1"
                ).fetchone() == (owned.generation + 1,)
                with pytest.raises(sqlite3.IntegrityError, match="unguarded"):
                    insert(owned.connection, table, row)
            raise RollbackProbe
        assert owned.connection.execute(
            "SELECT fencing_generation FROM omnivia_mutation_guard WHERE singleton = 1"
        ).fetchone() == (owned.generation,)
        assert owned.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone() == (row_count_before,)
        assert (
            owned.connection.execute(
                "SELECT projection_kind, schema_version, profile_version, active_run_id, "
                "active_epoch, active_source_checkpoint, active_build_digest, "
                "active_validation_digest, activated_at_us "
                "FROM omnivia_projection_ledger WHERE projection_id = 'projection-1'"
            ).fetchone()
            == pointer_before
        )
        return

    if authority == "closed":
        close_guard(owned.connection)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded"):
            insert(owned.connection, table, row)
        return

    owned.connection.close()
    stock = sqlite3.connect(owned.path)
    stock.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            insert(stock, table, row)
    finally:
        stock.close()


def test_m5_17a_pointer_trigger_binds_direct_authority(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    pointer_trigger = "omnivia_guard_projection_ledger_active_pointer_update"
    pointer_sql = owned.connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
        (pointer_trigger,),
    ).fetchone()[0]
    ledger_triggers = [
        str(row[0])
        for row in owned.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' "
            "AND tbl_name = 'omnivia_projection_ledger'"
        )
    ]
    with authorised(owned.connection, ddl=True):
        for trigger in ledger_triggers:
            owned.connection.execute(f'DROP TRIGGER "{trigger}"')
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_projection_ledger SET projection_kind = NULL, "
            "schema_version = NULL, profile_version = NULL, active_run_id = NULL, "
            "active_epoch = NULL, active_source_checkpoint = NULL, "
            "active_build_digest = NULL, active_validation_digest = NULL, "
            "activated_at_us = NULL WHERE projection_id = 'projection-1'"
        )
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(pointer_sql)
    owned.connection.close()
    stock = sqlite3.connect(owned.path)
    stock.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            stock.execute(
                "UPDATE omnivia_projection_ledger SET projection_kind = ?, "
                "schema_version = ?, profile_version = ?, active_run_id = ?, "
                "active_epoch = ?, active_source_checkpoint = ?, active_build_digest = ?, "
                "active_validation_digest = ?, activated_at_us = ? "
                "WHERE projection_id = 'projection-1'",
                (
                    "knowledge.search",
                    "schema-v1",
                    "profile-v1",
                    "run-1",
                    1,
                    "source:1",
                    DIGEST_A,
                    DIGEST_B,
                    BASE_US + 5,
                ),
            )
    finally:
        stock.close()


def test_m5_18_activation_failure_rolls_back_all_effects(owned: m2.Owned) -> None:
    complete_run(owned)
    activate(owned)
    complete_run(
        owned,
        "run-2",
        epoch=2,
        checkpoint="source:2",
        start=BASE_US + 10,
        build_digest=DIGEST_B,
        validation_digest=DIGEST_C,
    )
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "CREATE TEMP TRIGGER inject_pointer_failure BEFORE UPDATE ON "
            "omnivia_projection_ledger BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        activate(
            owned,
            "run-2",
            sequence=1,
            epoch=2,
            previous="run-1",
            at=BASE_US + 15,
            checkpoint="source:2",
            build_digest=DIGEST_B,
            validation_digest=DIGEST_C,
        )
    assert owned.connection.execute(
        "SELECT state FROM omnivia_projection_runs WHERE run_id = 'run-1'"
    ).fetchone() == ("succeeded",)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_projection_activations"
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT active_run_id FROM omnivia_projection_ledger"
    ).fetchone() == ("run-1",)


def test_m5_19_interruption_and_fresh_process_retry(tmp_path: Path) -> None:
    base = tmp_path / "through-0010.sqlite"
    materialise_phase0_baseline(base)
    with m2.migration_catalogue_through(10):
        m2.bootstrap_and_migrate(base)
    for stop_after in range(1, len(MIGRATION_STATEMENTS) + 1):
        path = tmp_path / f"interrupted-{stop_after}.sqlite"
        shutil.copy2(base, path)
        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        state = m2.read_workspace_state(connection)
        crashing = m2.FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with (
            m2.migration_catalogue_through(MIGRATION_VERSION),
            pytest.raises(m2.MigrationInterrupted),
        ):
            apply_pending_migrations(
                crashing,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
        connection.close()
        check = open_database(path, OpenMode.READ_ONLY)
        assert MIGRATION_VERSION not in applied_migrations(check)
        assert TABLES.isdisjoint(m2.object_names(check, "table"))
        check.close()
        assert m2.run_child(
            M5_RETRY_CHILD, str(path), m2.SERVICE_INSTANCE, WORKSPACE_ID
        ) == {"applied": [11], "foreign_keys": [], "integrity": []}


def test_m5_20_backup_fingerprint_integrity_and_foreign_keys(
    owned: m2.Owned, tmp_path: Path
) -> None:
    complete_run(owned)
    activate(owned)
    source_fingerprint = fingerprint_schema(owned.connection)
    source_rows = {
        table: owned.connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in TABLES
    }
    owned.connection.close()
    installation = InstallationLayout(root=tmp_path / "installation")
    installation.create(WORKSPACE_ID)
    result = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert result.verified
    backup = open_database(result.path, OpenMode.READ_ONLY)
    assert fingerprint_schema(backup) == source_fingerprint
    assert integrity_check(backup) == []
    assert foreign_key_check(backup) == []
    for table in TABLES:
        assert backup.execute(f"SELECT * FROM {table}").fetchall() == source_rows[table]
    backup.close()


def test_m5_21_projection_loss_rebuild_oracle_is_deterministic(
    owned: m2.Owned, tmp_path: Path
) -> None:
    m2.seed_chain(owned)
    with guarded(owned):
        for audit_ref in ("audit-1", "audit-2"):
            m3.insert(
                owned.connection,
                "omnivia_application_audit_events",
                m3.audit_row(audit_ref),
            )
    m3.seed_accepted_version(owned)

    query = """
        SELECT
            v.workspace_id,
            v.governed_record_id,
            v.governed_record_version_id,
            v.record_type,
            v.domain_scope,
            v.content_schema_version,
            v.content_json,
            v.content_digest,
            v.valid_from_us,
            v.valid_to_us,
            v.audit_ref,
            l.provenance_event_id,
            l.link_ordinal,
            e.evidence_id,
            e.source_kind,
            e.source_native_id,
            e.content_checksum,
            e.media_type,
            n.normalized_record_id,
            n.record_sequence,
            n.record_type AS normalized_record_type,
            n.schema_version AS normalized_schema_version,
            n.content_json AS normalized_content_json,
            n.content_digest AS normalized_content_digest,
            s.normalized_span_id,
            s.span_sequence,
            s.span_kind,
            s.span_pointer,
            s.span_start_offset,
            s.span_end_offset,
            a.principal_id,
            a.operation,
            a.purpose,
            a.outcome_class
        FROM omnivia_authoritative_governed_versions v
        JOIN omnivia_governed_version_evidence_links l
          ON l.workspace_id = v.workspace_id AND l.assembly_id = v.assembly_id
        JOIN omnivia_evidence_artifacts e
          ON e.workspace_id = l.workspace_id AND e.evidence_id = l.evidence_id
        LEFT JOIN omnivia_normalized_source_records n
          ON n.workspace_id = l.workspace_id AND n.evidence_id = l.evidence_id
         AND n.normalized_record_id = l.normalized_record_id
        LEFT JOIN omnivia_normalized_source_spans s
          ON s.workspace_id = l.workspace_id AND s.evidence_id = l.evidence_id
         AND s.normalized_record_id = l.normalized_record_id
         AND s.normalized_span_id = l.normalized_span_id
        JOIN omnivia_application_audit_events a
          ON a.workspace_id = v.workspace_id AND a.audit_ref = v.audit_ref
        WHERE v.layer = 'governed'
          AND v.governance_disposition = 'accepted'
          AND v.authority_level = 'canonical'
        ORDER BY v.governed_record_id, v.governed_record_version_id,
                 l.provenance_event_id, l.link_ordinal
    """

    def source_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
        cursor = connection.execute(query)
        columns = [str(column[0]) for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def rebuild(
        source: sqlite3.Connection, path: Path
    ) -> tuple[str, str, int, int, list[str]]:
        facts = source_rows(source)
        assert facts
        frontier_document = {
            "authority": {
                "layer": "governed",
                "governance_disposition": "accepted",
                "authority_level": "canonical",
            },
            "rows": facts,
        }
        frontier_bytes = json.dumps(
            frontier_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        checkpoint = "m1m2m3:sha256:" + hashlib.sha256(frontier_bytes).hexdigest()
        projected_rows = [
            json.dumps(
                {
                    "record_id": fact["governed_record_id"],
                    "version_id": fact["governed_record_version_id"],
                    "record_type": fact["record_type"],
                    "domain_scope": fact["domain_scope"],
                    "content": json.loads(str(fact["content_json"])),
                    "evidence_id": fact["evidence_id"],
                    "normalized_record_id": fact["normalized_record_id"],
                    "normalized_content": (
                        None
                        if fact["normalized_content_json"] is None
                        else json.loads(str(fact["normalized_content_json"]))
                    ),
                    "normalized_span_id": fact["normalized_span_id"],
                    "span_pointer": fact["span_pointer"],
                    "audit_ref": fact["audit_ref"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for fact in facts
        ]
        output_bytes = json.dumps(
            projected_rows, separators=(",", ":"), ensure_ascii=False
        ).encode()
        digest = "sha256:" + hashlib.sha256(output_bytes).hexdigest()
        projection = sqlite3.connect(path)
        try:
            projection.executescript(
                "CREATE TABLE projection_rows (ordinal INTEGER PRIMARY KEY, "
                "payload_json TEXT NOT NULL);"
                "CREATE TABLE rebuild_manifest (source_checkpoint TEXT NOT NULL, "
                "target_epoch INTEGER NOT NULL, input_count INTEGER NOT NULL, "
                "output_count INTEGER NOT NULL, build_digest TEXT NOT NULL);"
            )
            projection.executemany(
                "INSERT INTO projection_rows VALUES (?, ?)",
                enumerate(projected_rows, start=1),
            )
            projection.execute(
                "INSERT INTO rebuild_manifest VALUES (?, 1, ?, ?, ?)",
                (checkpoint, len(facts), len(projected_rows), digest),
            )
            projection.commit()
        finally:
            projection.close()
        return checkpoint, digest, len(facts), len(projected_rows), projected_rows

    derived = tmp_path / "disposable-projection.sqlite"
    baseline = rebuild(owned.connection, derived)
    with guarded(owned):
        m3.insert(
            owned.connection,
            "omnivia_application_audit_events",
            m3.audit_row("audit-3"),
        )
    m3.seed_human_candidate(
        owned,
        record_id="record-2",
        assembly_id="assembly-2",
        version_id="version-2",
        audit_ref="audit-3",
    )
    m3.seed_accepted_version(
        owned,
        record_id="record-2",
        candidate_assembly="assembly-2",
        candidate_version="version-2",
        assembly_id="assembly-accepted-2",
        version_id="version-accepted-2",
        audit_ref="audit-3",
    )
    sensitivity_derived = tmp_path / "sensitivity-projection.sqlite"
    sensitive = rebuild(owned.connection, sensitivity_derived)
    assert sensitive[0] != baseline[0]
    assert sensitive[1] != baseline[1]
    assert sensitive[2] > baseline[2]
    assert sensitive[3] > baseline[3]
    assert sensitive[4] != baseline[4]
    derived.unlink()
    sensitivity_derived.replace(derived)
    checkpoint, digest, input_count, output_count, rows = sensitive

    start_run(owned, checkpoint=checkpoint)
    begin_validation(owned)
    validate(
        owned,
        build_digest=digest,
        input_count=input_count,
        output_count=output_count,
    )
    finish_success(
        owned,
        build_digest=digest,
        input_count=input_count,
        output_count=output_count,
    )
    activate(owned, checkpoint=checkpoint, build_digest=digest)

    derived.unlink()
    owned.connection.close()
    source = open_database(owned.path, OpenMode.READ_ONLY)
    try:
        rebuilt = rebuild(source, derived)
    finally:
        source.close()
    assert rebuilt == (checkpoint, digest, input_count, output_count, rows)
    projection = sqlite3.connect(derived)
    try:
        assert projection.execute(
            "SELECT source_checkpoint, target_epoch, input_count, output_count, "
            "build_digest FROM rebuild_manifest"
        ).fetchone() == (checkpoint, 1, input_count, output_count, digest)
    finally:
        projection.close()
    source = open_database(owned.path, OpenMode.READ_ONLY)
    try:
        assert source.execute(
            "SELECT active_source_checkpoint, active_epoch, active_build_digest "
            "FROM omnivia_projection_ledger"
        ).fetchone() == (checkpoint, 1, digest)
    finally:
        source.close()


def test_m5_22_schema_is_healthy(owned: m2.Owned) -> None:
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []
    assert fingerprint_schema(owned.connection)
