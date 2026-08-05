# Phase 0 Baseline Freeze (T-0627)

This document describes the reproducible behavioural and data baseline captured
for OmniVia Core before the approved package, contract, and workspace-ownership
migrations.

Phase 0 only records what Core does today. It renames nothing, moves no
behaviour, changes no storage schema, and implements no leases or fencing.

## Why a freeze

The migrations that follow will rename packages, move contracts, and change who
owns workspace state. Any of those can silently change a public export, a stored
row, or a response payload. A migration is only safe if "unchanged" is something
a command can prove, so this baseline turns the current behaviour into checked-in
artifacts and a check that fails with the exact symbol, column, or field that
moved.

## What is frozen

| Artifact | What it records |
|---|---|
| `baseline/inventories/public-exports.json` | Root `__all__`, compatibility exports, and for every module its `__all__` plus the public symbols it defines, with enum members, dataclass fields, and callable signatures. |
| `baseline/inventories/storage-schema.json` | Every table, column, index, and foreign key `Database._init_schema` creates. |
| `baseline/inventories/dependencies.json` | Declared dependencies, actual third-party imports, the contract/runtime layer split, and every transitional contract → runtime import with its reason. |
| `baseline/inventories/platform-http-routes.json` | The full neutral Platform HTTP route table, the response envelope, the `/local/**` and `/dev/**` exclusion rules, and the route each required operation maps to. |
| `baseline/inventories/mcp-tools.json` | The full base MCP tool list plus the `tools/list` protocol method, the Dev-only exclusion rules, and the tool each required operation maps to. |
| `baseline/inventories/cli-commands.json` | The full base CLI command list, the Dev-only exclusion rules, and the command each required operation maps to. |
| `baseline/inventories/evidence-gaps.json` | Every piece of external evidence, its owner, its status, what closed it, and where any remainder is tracked. |
| `baseline/inventories/legacy-db-fixture.json` | The value-free inventory of the generated legacy database fixture. |
| `baseline/fixtures/*.json` | One golden fixture per required baseline slice. |

## Baseline slice coverage

Nineteen slices are required. Each has a fixture; the ones Core cannot execute
are recorded as `pending_external_capture` with the gap that blocks them.

| Slice | Status | Notes |
|---|---|---|
| `health.health`, `health.readiness` | pending | Core has no health primitive (GAP-004). |
| `workspace.create`, `workspace.list`, `workspace.get` | captured | `WorkspaceService`. |
| `ingestion.import_directory` | captured | `WorkspaceService.import_path` over three generated documents. |
| `memory.create`, `memory.list`, `memory.get`, `memory.search` | captured | `MemoryService`. |
| `context.search` | captured | Workspace-scoped search plus `SearchService.highlight_matches`. |
| `context.pack` | partial | Storage schema only; Core implements no pack assembly (GAP-005). |
| `graph.preview` | captured | `assemble_graph_preview` from the in-code memory graph fixture. |
| `graph.traversal` | captured | `get_neighbors`, `get_backlinks`, and `find_path`. |
| `mcp.tools_discovery`, `mcp.read`, `mcp.write` | pending | Core ships no MCP surface; the tools are named in the inventory, the responses are not captured (GAP-008). |
| `cli.read`, `cli.write` | pending | Core ships no product CLI; the commands are named in the inventory, the output is not captured (GAP-008). |

Every fixture records how it was produced in its `capture_kind` field. A
captured fixture is `core_code_path`: a deterministic Core-side equivalent
produced by calling Core in this checkout. No fixture is a live Platform, MCP,
or CLI response, and each captured fixture carries a note saying so.

## External surfaces

Core cannot execute the Platform HTTP, MCP, or CLI surfaces, but their shape is
no longer unknown. A read-only review of the owning repositories supplied:

- the **full neutral Platform route table** from
  `omnivia-platform` `.../omnivia_memory_platform/http/server.py`, together with
  the response envelope (`{ok, data}` on success, `{ok, service, message}` for
  health, an HTTP status plus `{ok: false, error}` for failures, and a fixed
  `Internal server error` for unexpected ones);
- the **full base MCP tool list** and the `tools/list` discovery method from
  `omnivia-dev` `.../omnivia_memory_dev/mcp/server.py`;
- the **full base CLI command list** from `omnivia-dev`
  `.../omnivia_memory_dev/cli/commands.py`.

Each inventory carries exclusion rules, and the check rejects any inventory
entry or captured descriptor that matches one:

| Surface | Excluded by rule |
|---|---|
| `platform_http` | every `/local/**` and every `/dev/**` route |
| `mcp_tools` | `pattern_*`, the knowledge-consolidation tools, and `codebase_intelligence_*` |
| `cli_commands` | `codebase-intelligence *`, and the Dev observability and evidence seams |

The required baseline mappings are `tools/list`, `memory_get`, and
`memory_store` for MCP, and `get` and `create` for the CLI. The two
`/agent/context/*` operations freeze the `POST` form, because the `GET` variants
are deprecated in Platform.

Two honest limits are recorded rather than smoothed over. The reviewed neutral
route table has no readiness route, so `health.readiness` keeps a null
descriptor under GAP-004. And the review fixed the *shape* of these surfaces,
not their output: no live response body was captured, which is what GAP-007 and
GAP-008 track.

## How the fixtures stay reproducible

One ephemeral session drives every captured fixture: a temporary directory, a
SQLite database created at an explicit path under that directory, and generated
document content held in `baseline/scenarios.py` next to the scenarios that use
it. Nothing reads the user's home directory, and the default
`~/.omnivia/memories.db` is never opened.

Captured values then pass through `baseline/determinism.py`, which:

- rewrites random UUID identifiers to `<uuid-0001>` tokens numbered in
  first-appearance order, so a workspace id stays recognisable across fixtures
  and inside the paths that embed it;
- rewrites wall-clock ISO-8601 timestamps to `<timestamp>`, while leaving values
  that are already deterministic (Core's `FIXTURE_TIME`) untouched;
- rewrites absolute paths to a registered `<root>` token plus a POSIX tail.

Content hashes are deliberately preserved. They derive from checked-in text, so
they are stable and are the strongest behavioural signal in the fixtures.

A guard rejects any fixture whose request or response still contains a
machine-specific absolute path, so "no absolute paths in tracked artifacts" is a
check rather than a convention.

Where Core does not guarantee an order, the fixture records the sort key it
applied in its `ordering` field rather than pretending the order is behaviour.
Two cases matter today: `FileScanner` walks directories with `Path.iterdir()`,
which is unordered, and the repositories order by `created_at DESC`, which ties
when rows are written inside the same clock tick.

## Clean test environment

Core requires Python 3.11 or newer. These are the exact commands used to produce
and verify this baseline.

```bash
# From the repository root.
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e "services/omnivia-memory[dev]"
```

`.venv/` is already ignored by `.gitignore`.

The `dev` extra does not include the optional extractor dependencies. Without
them, five PDF/DOCX tests in `services/omnivia-memory/tests/test_ingestion.py`
fail with `PyMuPDF not installed` and `python-docx not installed`. Install them
for a fully green ingestion suite:

```bash
.venv/bin/python -m pip install pymupdf python-docx
```

These are runtime-optional in Core: both extractors import them lazily and
return a failure result when they are absent. They are recorded in the
dependency inventory as allowlisted third-party imports, not as declared
dependencies.

## Verification commands

### Baseline only

```bash
PYTHON=.venv/bin/python scripts/check-core-baseline.sh
```

Equivalently, without the wrapper:

```bash
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m baseline verify
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m pytest baseline/tests -q
```

### Full Core suite

```bash
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m pytest \
  services/omnivia-memory/tests benchmarks/tests baseline/tests -q
```

### Focused contract checks

```bash
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m pytest \
  services/omnivia-memory/tests/test_public_api.py \
  services/omnivia-memory/tests/test_knowledge_contract.py \
  baseline/tests/test_public_exports.py \
  baseline/tests/test_storage_schema.py -q
```

### Regenerating the baseline

```bash
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m baseline capture
```

Regeneration is a deliberate act. A diff in `baseline/` during a migration is
the signal the freeze exists to produce; regenerate only after the change has
been reviewed, and record why in the review note.

## Focused cross-repository verification

The descriptors are frozen from reviewed source, but the live responses are
not. Each owning repository closes its remaining gap by exporting its live
surface in the declared shape and handing the file back to Core:

```bash
# Run from omnivia-core, against an artifact produced by the owning repo.
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m baseline verify-external \
  --surface platform_http --artifact <path-to-platform-capture>.json

PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m baseline verify-external \
  --surface mcp_tools --artifact <path-to-dev-mcp-capture>.json

PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m baseline verify-external \
  --surface cli_commands --artifact <path-to-dev-cli-capture>.json
```

The artifact must have the same shape as the declared inventory, with every
operation carrying `evidence.captured: true`, an `evidence.captured_from`
naming the repo and commit, and a complete `descriptor` that is a non-excluded
member of the frozen inventory. Core's check then reports, by operation id,
anything declared but missing, anything captured but not declared, anything
outside the inventory, and anything an exclusion rule bars.

The in-repo export command for each surface is owned by Platform and Dev. It
could not be observed from Core, so it is recorded as part of GAP-007 and
GAP-008 rather than guessed.

List the gaps and their status at any time:

```bash
PYTHONPATH=services/omnivia-memory/src .venv/bin/python -m baseline list-gaps
```

## Pre-existing failures recorded at freeze time

These were red before this task and are recorded here rather than fixed, because
Phase 0 must not change behaviour and both are decisions for review.

1. `services/omnivia-memory/tests/test_public_api.py::test_contract_surface_has_no_runtime_import_creep`
   fails. It asserts the literal substring `mcp` does not appear in
   `services/omnivia-memory/src/omnivia_memory/__init__.py`. Commit `d42244d`
   ("feat(core): add control plane contract registry") added
   `import_mcp_candidates` and `validate_mcp_import_spec` to the root exports,
   which contain that substring. The root imports no MCP module, so the intent
   of the check still holds; the substring match is too loose. The baseline's own
   dependency check enforces the same intent precisely by banning imports of any
   module under the `mcp` prefix.
2. Five PDF/DOCX tests in `services/omnivia-memory/tests/test_ingestion.py` fail
   when `pymupdf` and `python-docx` are absent, because they are not in the
   `dev` extra. See the clean environment section above.

## Observations carried into Phase 1

These are recorded facts about the current tree, not proposed changes.

- **No HTTP, MCP, or CLI surface exists in Core.** The only `argparse` entry
  points are the benchmark runners. Everything in the external surface
  inventories therefore belongs to Platform or Dev.
- **`sqlalchemy` is a declared dependency that nothing imports.** Persistence is
  raw `sqlite3`. The dependency inventory records this in
  `declared_but_unimported` so it stays visible and cannot quietly change.
- **`GraphConfidence` uses upper-case values** (`EXTRACTED = "EXTRACTED"`), while
  the memory graph contracts carry lower-case confidence strings
  (`"extracted"`). Both are frozen as they are.
- **`context_packs` is a table with no code.** The schema exists in
  `Database._init_schema`; no module reads or writes it.
- **Four transitional contract → runtime import edges exist**, each allowlisted
  with a reason in `baseline/dependencies.py`: the package root re-exporting
  `omnivia_memory.memory` and `omnivia_memory.persistence`, and
  `omnivia_memory.memory_graph` re-exporting its store and ingestion adapter.
- **Ordering is unspecified in two places** (`FileScanner.scan` and the
  `created_at DESC` repository queries), as described above.

## Evidence gaps

| Gap | Scope | Owner | Status | State |
|---|---|---|---|---|
| GAP-001 | surface | omnivia-platform | closed | Neutral route table, envelope, and exclusions recorded from reviewed Platform source. Remainder → GAP-007. |
| GAP-002 | surface | omnivia-dev | closed | Base MCP tool list and baseline mapping recorded from reviewed Dev source. Remainder → GAP-008. |
| GAP-003 | surface | omnivia-dev | closed | Base CLI command list and baseline mapping recorded from reviewed Dev source. Remainder → GAP-008. |
| GAP-004 | capability | omnivia-platform | open | Core has no health primitive, and the neutral route table has no readiness route. |
| GAP-005 | capability | omnivia-platform | open | Context pack assembly is not implemented in Core; `context.pack` freezes the storage schema, not a pack response. |
| GAP-006 | process | omnivia-pm | closed | Codex reviewed and applied the accepted T-0627 task, the readiness assessment, and PM ADR-036, PM ADR-037, PM ADR-038. |
| GAP-007 | evidence | omnivia-platform | open | No live Platform HTTP response body has been captured. |
| GAP-008 | evidence | omnivia-dev | open | No live MCP tool result or CLI command output has been captured. |

`baseline/inventories/evidence-gaps.json` is the machine-readable source. The
surface check fails if an operation references a gap that does not exist, if a
`surface`-scoped gap is referenced by nothing, if a closed gap does not say what
closed it or hands its remainder to a gap that is not open, or if an operation
with no descriptor points at a closed gap.

## Related documents

- [Legacy memories.db migration criteria](legacy-memories-db-migration.md)
- [Baseline package guide](../../baseline/README.md)
