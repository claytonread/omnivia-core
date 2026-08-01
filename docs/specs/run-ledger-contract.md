# Run-Ledger Contract

## Contract Surface

Core defines a versioned run-ledger entry for producer and consumer tooling:

- `RunLedgerEntry`
- `EvidenceFileRef`
- `RunLedgerProvenance`
- `RunLedgerStatus`

## Version

- run-ledger contract: `1.0`

Compatibility policy:

- unknown major versions are invalid
- newer minor versions on the same major are allowed with warnings

## Entry Shape

Each `RunLedgerEntry` carries:

- `run_id`
- `task_id`
- `target_repo`
- `lane_id`
- `status`
- `started_at`
- `updated_at`
- `completed_at` when the run reaches a terminal state
- `evidence_file_refs`
- `provenance`
- `contract_version`

`EvidenceFileRef` is intentionally small:

- `path`
- `kind`
- `description`
- `checksum`

`RunLedgerProvenance` records who wrote the entry:

- `producer`
- `source_ref`
- `producer_version`

## Validation Rules

Validators enforce:

- required run, task, repo, lane, and provenance producer identifiers
- allowed `RunLedgerStatus` values
- ISO 8601 timestamps
- a `completed_at` timestamp for terminal run states
- non-empty evidence file paths and kinds
