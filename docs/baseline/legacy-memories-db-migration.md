# Legacy `memories.db` Migration Criteria (T-0627)

The legacy local database is the only artifact in this migration that cannot be
regenerated: losing it loses the user's memories. This document defines the
backup, capture, checksum, restore, rollback, and redaction rules, and the
zero-data-loss acceptance criteria that must pass before and after any migration
step touches it.

The rules are implemented in `baseline/legacy_db.py` and exercised by
`baseline/tests/test_legacy_db.py`, so each criterion below is a check, not a
convention.

## Safety rules

These are absolute. They are enforced in code, not by process.

1. **The real database is never opened.** `assert_fixture_database` refuses any
   path inside `~/.omnivia`, and any file named `memories.db` directly under a
   home directory. Every entry point calls it first. There is no override flag.
2. **Reads cannot write.** `open_read_only` connects with `mode=ro` through a
   SQLite URI and additionally sets `PRAGMA query_only = ON`. A write attempt
   raises `sqlite3.OperationalError: attempt to write a readonly database`.
3. **Captures contain no user values.** The inventory records table names,
   column names, row counts, and checksums. Row values are hashed and discarded
   inside the capture function; they never reach an artifact or a log.
4. **All rehearsals use generated fixtures.** `build_legacy_fixture_database`
   creates a legacy-shaped database from synthetic content using Core's own
   `Database._init_schema`, so the real schema is exercised without real data.

## Terms

- **File checksum** — SHA-256 of the database file's bytes. Only meaningful
  where a byte copy is expected.
- **Content checksum** — SHA-256 over each table's column list and its rows in a
  canonical order, then over the per-table digests. Independent of SQLite's
  physical page layout, so it is the checksum that means "same data".
- **Inventory** — tables, columns, row counts, and checksums for a database.
  Value-free by construction.

A backup taken with SQLite's online backup API will **not** match the source's
file checksum, because the backup rewrites pages. That is expected. Backups are
verified by content checksum; only restore and rollback, which are byte copies,
are verified by file checksum.

## Procedure

### 1. Backup

```python
from baseline.legacy_db import backup_database

record = backup_database(source, destination)
```

- The source is opened read-only, so the operation cannot modify the database it
  is protecting.
- The destination must not already exist. Overwriting a backup is refused.
- The backup is verified before the call returns: content checksum equal to the
  source, row counts equal to the source, and the source's own content checksum
  unchanged by the operation. Any failure raises `LegacyDatabaseError` with the
  specific check that failed.

Keep the returned `BackupRecord` as evidence. It carries both checksums for the
source and the target, the operation name, and the list of checks performed.

### 2. Pre-migration capture

```python
from baseline.legacy_db import capture_inventory

before = capture_inventory(source)
```

Record `before.content_checksum` and `before.total_rows` in the migration run
record. This is the state the acceptance criteria compare against.

### 3. Migration step

Perform the migration against the live database. The backup is untouched.

### 4. Post-migration verification

```python
from baseline.legacy_db import capture_inventory, verify_zero_data_loss

problems = verify_zero_data_loss(before, capture_inventory(live))
```

An empty list is the pass signal.

### 5. Rollback

```python
from baseline.legacy_db import rollback_from_backup

record = rollback_from_backup(backup, live)
```

Rollback restores the pre-migration backup over the live file by byte copy and
verifies both the file checksum and the content checksum against the backup. The
resulting record is labelled `rollback` rather than `restore`, so the evidence
says which of the two happened.

After rollback, `verify_zero_data_loss(before, capture_inventory(live))` must
return an empty list. That is the rollback acceptance criterion.

## Zero-data-loss acceptance criteria

A migration step is accepted only when every criterion holds.

| # | Criterion | How it is checked |
|---|---|---|
| 1 | A verified backup exists before the step runs | `backup_database` returns `verified=True` with no checks recorded |
| 2 | The backup holds the same content as the source | Content checksums are equal |
| 3 | Taking the backup did not modify the source | The source's content checksum is re-read after the backup and compared |
| 4 | No table disappeared | `verify_zero_data_loss` reports missing tables by name |
| 5 | No table appeared unannounced | `verify_zero_data_loss` reports added tables by name |
| 6 | No column set changed | Column lists are compared per table |
| 7 | No row count changed | Row counts are compared per table |
| 8 | No row value changed | Content checksums are compared per table, which catches an edit that leaves the row count intact |
| 9 | Rollback restores the exact pre-migration state | `rollback_from_backup` verifies file and content checksums, and `verify_zero_data_loss` returns empty afterwards |
| 10 | No user content was written to any artifact or log | Captures record counts and checksums only; `redact_row` is the only path that renders a row |

Criteria 4 to 8 produce a specific message. A row edited in place reports
`table 'memories' has the same row count but a different content checksum; row
values changed`, not a generic mismatch.

Criteria 4 and 5 are equality checks, not one-way ones. A migration that
legitimately adds a table must be accepted deliberately by re-recording the
`before` inventory after review, never by loosening the check.

## Redaction

When a row genuinely has to be looked at, use `redact_row`:

- content columns (`content`, `text_preview`, `description`, `evidence`) are
  replaced with `{"length": n, "sha256": ...}`;
- path columns (`file_path`, `path`, `root_path`, `storage_path`,
  `source_reference`) are reduced to `<redacted>/<basename>`;
- everything else, such as identifiers, lifecycle states, and timestamps, passes
  through.

The length and hash are enough to prove two rows match without disclosing what
either says.

## Fixture rehearsal

The generated fixture is deterministic: fixed identifiers, a fixed timestamp,
and synthetic content, so its content checksum is the same on every machine. It
is frozen in `baseline/inventories/legacy-db-fixture.json`, and
`python -m baseline verify` fails if either the fixture content or Core's
storage schema changes underneath it.

Rehearse the full procedure against a fixture before running it for real:

```bash
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m pytest \
  baseline/tests/test_legacy_db.py -q
```

## Related documents

- [Phase 0 baseline freeze](phase-0-baseline-freeze.md)
