# Baseline

Reproducible behavioural and data baseline for OmniVia Core, captured for
T-0627 before the package, contract, and workspace-ownership migrations.

This package adds no runtime behaviour. Every module observes existing Core
code, writes deterministic artifacts, and fails with the exact symbol, column,
or field that moved.

Start with [the Phase 0 freeze document](../docs/baseline/phase-0-baseline-freeze.md)
for the scope, the clean environment setup, and the verification commands.

## Commands

```bash
# Verify the working tree against the frozen baseline.
PYTHONPATH=services/omnivia-memory/src python3 -m baseline verify

# Regenerate every tracked artifact. A deliberate act; review the diff.
PYTHONPATH=services/omnivia-memory/src python3 -m baseline capture

# Print the recorded evidence gaps, their owners, and what closes them.
PYTHONPATH=services/omnivia-memory/src python3 -m baseline list-gaps

# Validate a capture artifact produced by Platform or Dev.
PYTHONPATH=services/omnivia-memory/src python3 -m baseline verify-external \
  --surface platform_http --artifact <file>.json
```

`scripts/check-core-baseline.sh` runs `verify` plus the baseline test suite and
handles `PYTHONPATH` itself.

## Layout

| Module | Responsibility |
|---|---|
| `determinism.py` | Normalisation, redaction, canonical JSON, and leaf-level diffs. |
| `slices.py` | The nineteen baseline slices the freeze must cover. |
| `inventory.py` | Public Python export inventory and its drift check. |
| `storage.py` | Local storage schema snapshot and its drift check. |
| `dependencies.py` | Third-party allowlist, banned prefixes, and the transitional contract → runtime import allowlist. |
| `surfaces.py` | Neutral HTTP / base MCP / base CLI inventories, their exclusion rules, the anti-invention guard, the evidence gap register, and external capture validation. |
| `scenarios.py` | Golden fixtures captured from real Core code paths. |
| `legacy_db.py` | Legacy database backup, read-only capture, checksum, restore, rollback, and redaction. |
| `cli.py` | `python -m baseline` entry point. |

Artifacts live in `inventories/` and `fixtures/`. Both are generated; edit the
declaration in code and re-run `capture` rather than editing JSON by hand.

## Rules this package enforces

- A surface descriptor may never be recorded without evidence. Descriptors here
  come from a read-only review of the owning repository and say so
  (`capture_kind: reviewed_static_source`, `live_response_captured: false`); a
  route, tool name, or command with no such evidence stays null and carries an
  open gap.
- A recorded descriptor must be a member of the frozen neutral or base
  inventory for its surface, and nothing in that inventory — or in a later
  capture — may match an exclusion rule. That is what keeps `/local/**`,
  `/dev/**`, and the Dev-only MCP and CLI extensions out of the Core-relevant
  surface by check rather than by convention.
- A closed gap must record what closed it, and hand any remainder to a gap that
  is still open. An operation or fixture with no evidence may only defer to an
  open gap.
- A tracked artifact may never contain a machine-specific absolute path.
- The real `~/.omnivia/memories.db` is never opened, copied, or inspected. All
  legacy database work runs against generated fixtures.
- A contract-layer module importing a runtime-layer module must be allowlisted
  with a stated reason, and an allowlist entry with no matching import is stale.
