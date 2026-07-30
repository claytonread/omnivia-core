# OmniVia Core

OmniVia Core is a public, local-first, backend-neutral portable knowledge
substrate.

It is not a note app, graph UI, scanner, sync service, hosted service, provider
router, MCP server, CLI runtime, or assistant installer. Core owns portable
contracts, validation, normalization, extension semantics, public API exports,
static fixtures/examples, and documentation that other repositories or tools
can build on safely.

## Positioning

Core is designed for:

- developers building graph-backed or knowledge-backed applications
- AI agent builders who need typed, source-grounded, reviewable context
- researchers working with claims, citations, and evidence strength
- personal knowledge builders modeling notes, links, tags, and tasks
- team knowledge builders modeling decisions, workflows, and risks
- Obsidian-like tool builders who need a portable contract surface
- Graphify-like codebase-map builders who need a portable graph fragment shape
- future OmniVia Platform, Dev, and Apps implementers

Core alone is not designed for:

- a complete note editor or publish/sync workflow
- a vault scanner, repo scanner, parser, or importer runtime
- a visual graph explorer or query runtime
- provider enrichment, model routing, or hosted storage
- direct CLI, MCP, or assistant-install surfaces

## Principles

- `local-first`: contract shapes assume local artifacts and local provenance.
- `backend-neutral`: contracts do not assume SQLite, Neo4j, vector DBs, or any
  specific storage/query engine.
- `developer-first`: exports are explicit, typed, reviewable, and easy to
  validate in tests and fixtures.
- `agent-safe`: confidence, review status, evidence strength, sensitivity, and
  missing-evidence markers stay first-class.
- `portable`: the same contract layer can represent vaults, codebases, research
  corpora, team workspaces, workflow systems, and agent memory.

## Repository Boundary

`omnivia-core` owns:

- portable knowledge contracts
- graph fragments, source refs, and schema version helpers
- validation helpers and normalization helpers
- extension manifests and namespace rules
- static examples, fixtures, adapter docs, and public-safe documentation

`omnivia-core` also still ships a small set of repo-local reference
implementations for memory, persistence, ingestion, search, and graph assembly.
Treat those as transitional code that currently lives here, not as a claim that
Core is the long-term runtime owner for those surfaces.

`omnivia-core` does not own:

- long-term ownership of ingestion, indexing, parsing, scanning, or watcher lifecycle
- long-term ownership of persistence lifecycle, caches, sync, or background jobs
- long-term ownership of query runtime, UI runtime, desktop runtime, or hosted runtime
- provider/model calls or assistant installation
- MCP serving, CLI runtime, or repo-specific tool workflows

## Comparison

Obsidian-like tools:

- Core can represent notes, wikilinks, derived backlinks, tags,
  frontmatter-derived properties, canvas/card-like objects, embedded files, and
  note-to-task links.
- Core does not try to become a note editor, plugin runtime, publish flow, or
  sync layer.

Graphify-like tools:

- Core can represent portable graph fragments, extracted/inferred/ambiguous
  confidence, source-backed code/document links, and bounded extension
  annotations such as `graphify:god_node` and `graphify:surprise_edge`.
- Graphify remains reference-only. Do not add `graphifyy` as a dependency.
- Core does not become a scanner, cache, query CLI, MCP server, or installer.

## Dependency Posture

The following are reference-only or future integration concerns, not default
Core dependencies:

- Graphify and other code-graph tools
- Obsidian and note-app/plugin runtimes
- tree-sitter language packages
- Markdown parser runtimes
- graph databases and vector databases
- model/provider SDKs
- MCP servers and CLI runtimes

## Package Topology

The repository root is the canonical `omnivia-core` distribution
(import package `omnivia_core`, under `src/`). Three sibling skeleton
distributions live under `packages/` and depend on `omnivia-core`:

```text
                    omnivia-core
                  ^      ^      ^
                  |      |      |
omnivia-core-runtime   omnivia-core-mcp   omnivia-core-cli
```

| Distribution | Import package | Location | Depends on |
|---|---|---|---|
| `omnivia-core` | `omnivia_core` | `src/omnivia_core` | — |
| `omnivia-core-runtime` | `omnivia_core_runtime` | `packages/omnivia-core-runtime` | `omnivia-core` |
| `omnivia-core-mcp` | `omnivia_core_mcp` | `packages/omnivia-core-mcp` | `omnivia-core` |
| `omnivia-core-cli` | `omnivia_core_cli` | `packages/omnivia-core-cli` | `omnivia-core` |

Rules enforced by `scripts/check-package-boundaries.py`:

- `omnivia-core` never depends on or imports any sibling distribution or the
  legacy `omnivia_memory` implementation.
- `omnivia-core-runtime`, `omnivia-core-mcp`, and `omnivia-core-cli` each
  declare a compile-time dependency on `omnivia-core`.
- `omnivia-core-mcp` and `omnivia-core-cli` never depend on or import
  `omnivia_core_runtime`.

All four packages are a **package-boundary skeleton only**: `omnivia_core`,
`omnivia_core_runtime`, `omnivia_core_mcp`, and `omnivia_core_cli` currently
expose package identity/version metadata and nothing else. There is no
runtime, MCP, or CLI implementation yet, and no compatibility facade has been
created for the legacy `omnivia-memory` implementation. The reference
implementation that other tooling should still use today continues to live,
unchanged, at `services/omnivia-memory` (import package `omnivia_memory`) — see
[Repository Split](#repository-split) below.

### Boundary and build checks

Run the boundary checks (manifest and AST-based) and their tests:

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/check-package-boundaries.py
```

Run a clean, isolated build/install check for all four distributions. This
builds a temporary wheelhouse and installs each distribution into its own
temporary virtual environment; it writes nothing under the repository tree:

```bash
PYTHON=.venv/bin/python scripts/check-package-builds.sh
```

## Application Contract v1

`omnivia_core.contracts.v1` is a provider-neutral wire contract for
application-layer request/response negotiation: version and capability
negotiation, request/response envelopes, and typed, retry-classified errors.
It is a foundation only — there is no per-operation payload catalogue, HTTP
binding, or transport implementation yet.

Canonical source and generated artifacts:

- `contracts/application/v1/schemas/*.schema.json` — twelve JSON Schema
  Draft 2020-12 documents (`common`, `compatibility`, `errors`, `envelopes`,
  `service`, `records`, `jobs`, `operations`, `workspace`, `memory`,
  `compatibility-matrix`, and the reference-only `application-v1` registry).
  These are the single source of truth; everything else is derived from them.
- `contracts/application/v1/fixtures/` — thirteen canonical example wire
  documents plus `manifest.json`, covering compatible negotiation, capability
  denial, an incompatible major version, a minimal request, a retryable
  mutation, a rich success response, a plain error response, tolerant decoding
  of an additive unknown field, an unrecognized open vocabulary value, a
  duplicate capability id, a response carrying both `result` and `error`, a
  response carrying neither, and a pattern-compatible but calendar-invalid
  RFC 3339 timestamp.
- `src/omnivia_core/contracts/v1/generated.py` — generated frozen dataclasses,
  type aliases, and frozen vocabulary constants. Standard library only.
- `src/omnivia_core/contracts/v1/codec.py` — tolerant production wire codec
  (canonical JSON, response-branch dispatch, retry semantics). Standard
  library only.
- `src/omnivia_core/contracts/v1/compatibility.py` — pure version-window and
  capability-negotiation semantics (version comparison, effective-capability
  intersection, duplicate-id detection, requirement resolution) plus the
  whole-envelope invariants `decode_response` enforces: every declared version
  parses, the versions in force (`api_version`, `workspace_format_version`)
  equal the ones negotiation selected, and each selected version falls inside
  the window the same envelope publishes as supported. The open
  `CompatibilityStatus` vocabulary is deliberately left unconstrained, so a
  newer peer's unseen status still decodes. Standard library only.
- `src/omnivia_core/contracts/v1/semantics.py` — pure semantic validation for
  the workspace and governed-memory DTOs (A2.2, ADR-038/ADR-039): domain-scope
  and identifier/open-code shape checks, temporal ordering, evidence/
  currentness/supersession/authority coherence, `memory.create` proposed-only
  result-tuple enforcement (including the reserved-field decode guard), and
  candidate evidence/provenance coherence. Standard library only.
- `src/omnivia_core/contracts/v1/resources.py` — standard-library-only
  accessors for the packaged schemas and fixtures (see below).
- `generated/typescript/application/v1/index.ts` — the same contract surface
  as a declaration-only TypeScript module.

`contracts/application/v1/{schemas,fixtures}` stay the only checked-in
canonical copy. The built wheel force-includes them under
`omnivia_core/contracts/v1/resources/{schemas,fixtures}` (see
`[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`), and
`omnivia_core.contracts.v1` exposes `list_schema_names`, `read_schema`,
`read_schema_text`, `list_fixture_files`, `read_fixture`, `read_fixture_text`,
and `read_fixture_manifest` as the only supported way to read that packaged
copy through `importlib.resources`.

Regenerate and verify:

```bash
.venv/bin/python scripts/generate-application-contracts.py         # regenerate
.venv/bin/python scripts/generate-application-contracts.py --check # verify no drift
.venv/bin/python scripts/check-application-contracts.py            # conformance gate
```

The conformance gate checks the canonical schema directory holds exactly the
twelve frozen schema documents (an extra one would be read by no check yet
packaged by the wheel, and a missing one is reported in the same place),
validates every schema against the Draft 2020-12
metaschema and its exact `$schema`/`$id`, resolves every `$ref` offline,
checks the registry publishes exactly the source schemas' definitions and
lists their exact URIs, validates every fixture against its declared
schema-validity (RFC 3339 `format` included, via `jsonschema.FormatChecker`)
and declared `tolerant_decode` outcome by actually running the production
codec, validates the manifest itself (unique nonempty ids, unique existing
files, explicit boolean flags, known semantic keys, and an exact match
against a frozen id/file/semantic mapping so deleting, renaming, or swapping
a fixture's assertion cannot stay green), runs every fixture's semantic
assertion, and checks that `src/omnivia_core/contracts` has no import outside
the standard library or its own package — including relative imports that
resolve elsewhere, and constant-string `__import__`/`importlib.import_module`
escapes under any alias (`import importlib as il`, `from importlib import
import_module as load`), with a non-literal argument failing closed.

The schema-set check complements, rather than replaces, the exact wheel
resource-set assertion in `scripts/check-package-builds.sh`: that one proves
the built wheel packages exactly what the canonical directory holds, which is
only worth having once this one has established that the directory holds
exactly the frozen set.

Run the contract test suite:

```bash
.venv/bin/python -m pytest tests/contracts -q
```

`jsonschema[format]` (plus its `types-jsonschema` stubs) is a
development-only dependency declared under `[dependency-groups]` in
`pyproject.toml`, used only by `scripts/check-application-contracts.py` and
its tests — the `format` extra provides the RFC 3339 calendar validation
`jsonschema.FormatChecker` needs. The contract package itself (`generated.py`,
`codec.py`, `compatibility.py`, `semantics.py`, `resources.py`) has zero
runtime dependencies, including on `jsonschema`.

The generated TypeScript module is regenerated and checked for drift the same
way as the Python module, and is strict-compiled (`--strict --noEmit
--skipLibCheck`, target `ES2022`, module `ESNext`, module resolution
`Bundler`) by `scripts/check-application-typescript.sh`.

The compiler is a repository-local, version-pinned dev dependency: TypeScript
is declared at the exact version `5.9.3` in `package.json` and locked in
`package-lock.json`, so `npm ci` reproduces the same compiler every time. The
check needs no sibling repository and no global install, and adds no runtime
JavaScript dependency — `typescript` is the only entry, and it is a
`devDependencies` one:

```bash
npm ci                                 # install the pinned compiler (once)
npm run check:application-contracts    # strict, no-emit contract compile
```

The npm script runs the shell gate, so the two are never separate copies of
the compiler flags. The shell gate can also be run directly, and resolves a
`tsc` binary in order: the `TSC` environment variable (explicit override), the
repository-local `node_modules/.bin/tsc` (the reproducible default), then
`tsc` on PATH:

```bash
scripts/check-application-typescript.sh
TSC=/path/to/tsc scripts/check-application-typescript.sh   # explicit override
```

## Repository Split

| Repository | Visibility | Purpose |
|---|---|---|
| `omnivia-core` | Public | Portable contracts, validators, normalizers, fixtures, and public docs. |
| `omnivia-platform` | Private | Runtime lifecycle, desktop shell, UI/runtime boundaries, sync/distribution concerns. |
| `omnivia-dev` | Private | Query tooling, MCP/CLI surfaces, repo indexing, and developer workflows. |
| `omnivia-cloud` | Private | Future hosted/cloud implementation placeholder. |
| `omnivia-pm` | Private | Backlog, planning, ADRs, research reviews, and implementation packets. |

## Docs Map

- [Portable Knowledge ADR](docs/adr/portable-knowledge-substrate.md)
- [Portable Knowledge Contract Spec](docs/specs/portable-knowledge-contract.md)
- [Obsidian-like Compatibility](docs/compatibility/obsidian-like.md)
- [Graphify-like Compatibility](docs/compatibility/graphify-like.md)
- [Portable Knowledge Launch Packet](docs/launch/portable-knowledge-launch-packet.md)
- [Examples](docs/examples/README.md)
- [Phase 0 Baseline Freeze](docs/baseline/phase-0-baseline-freeze.md)
- [Legacy memories.db Migration Criteria](docs/baseline/legacy-memories-db-migration.md)

## Checks

Install the public Core package locally for development:

```bash
python3 -m pip install -e services/omnivia-memory[dev]
```

Run the focused contract checks:

```bash
PYTHONPATH=services/omnivia-memory/src python3 -m pytest \
  services/omnivia-memory/tests/test_public_api.py \
  services/omnivia-memory/tests/test_knowledge_contract.py
```

Run the full package suite:

```bash
PYTHONPATH=services/omnivia-memory/src python3 -m pytest services/omnivia-memory/tests
```

Verify the Phase 0 baseline freeze (public exports, storage schema, dependency
drift, and golden fixtures):

```bash
scripts/check-core-baseline.sh
```

The PDF and DOCX ingestion tests need optional extractor dependencies that the
`dev` extra does not install. See the
[Phase 0 baseline freeze](docs/baseline/phase-0-baseline-freeze.md) for the
clean environment recipe.

## Public Import Example

```python
from omnivia_memory import (
    GraphConfidence,
    GraphSourceType,
    KNOWLEDGE_CONTRACT_VERSION,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    validate_knowledge_space,
)

source = KnowledgeSource(
    id="source-daily-note",
    space_id="personal-vault",
    source_type=GraphSourceType.NOTE,
    title="Daily Note",
    relative_path="notes/daily-note.md",
)
note = KnowledgeObject(
    id="daily-note",
    space_id="personal-vault",
    kind="note",
    title="Daily Note",
    tags=["daily-note"],
    source_refs=[
        SourceRef(
            source_id="source-daily-note",
            source_type=GraphSourceType.NOTE,
            path="notes/daily-note.md",
            confidence=GraphConfidence.EXTRACTED,
        )
    ],
    confidence=GraphConfidence.EXTRACTED,
)
space = KnowledgeSpace(
    id="personal-vault",
    title="Personal Vault",
    space_type="personal vault",
    contract_version=KNOWLEDGE_CONTRACT_VERSION,
    sources=[source],
    objects=[note],
)

assert validate_knowledge_space(space).valid
```
