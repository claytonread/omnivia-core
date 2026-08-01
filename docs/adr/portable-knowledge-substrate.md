# ADR: Portable Knowledge Substrate

## Status

Accepted.

## Decision

`omnivia-core` is positioned as a public portable knowledge substrate rather
than an OmniVia-only graph layer.

Core owns:

- portable knowledge contracts
- graph fragment contracts
- source refs, review state, confidence, evidence strength, visibility, and
  sensitivity concepts
- schema version compatibility helpers
- validation and normalization helpers
- extension manifest rules and namespaced extension semantics
- static fixtures, adapter docs, and public examples

Core does not own:

- ingestion, indexing, parsing, scanning, or watcher lifecycle
- persistence lifecycle, sync, or cache behavior
- query runtime, UI runtime, or desktop/runtime shell behavior
- provider/model calls, assistant installation, MCP serving, or CLI runtime

## Why

The same contract layer must support:

- personal vaults
- Obsidian-like note graphs
- Graphify-like codebase maps
- research corpora
- team workspaces
- workflow systems
- agent memory
- future OmniVia Platform, Dev, and App experiences

That requires a substrate, not a product surface.

## Consequences

- Graphify remains reference-only. `graphifyy` must not be added as a
  dependency.
- Obsidian-like compatibility means representation-only, not runtime
  compatibility.
- The package-root public API should export contracts and helpers only.
- Runtime implementation belongs in later Platform or Dev work.
