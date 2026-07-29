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
