# OmniVia Portable Knowledge Contracts

This package now exposes a contract-level public API for portable knowledge
spaces, graph fragments, source refs, extension manifests, validation helpers,
and normalization helpers.

The package root intentionally stays narrow. Runtime scanners, caches,
persistence layers, search services, MCP surfaces, CLI runtimes, provider
clients, and installers must not be exported from `omnivia_memory.__all__`.
Those concerns belong in later Platform or Dev work, not in the Core root API.

## Included Areas

- knowledge spaces, objects, collections, links, and claims
- graph nodes, edges, fragments, and source refs
- confidence, review, evidence, visibility, and sensitivity concepts
- schema version helpers and namespace-safe extension manifests
- public-safe validation helpers and normalization helpers
- static fixtures and portable compatibility examples

## Out Of Scope

- scanners, watchers, parsers, or import runtimes
- caches, sync, persistence lifecycle, or hosted behavior
- graph query runtime, UI runtime, or desktop runtime
- CLI, MCP, provider/model, or assistant-install surfaces

## Development Install

From the `omnivia-core` repository root:

```bash
python3 -m pip install -e services/omnivia-memory[dev]
```

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
)

source = KnowledgeSource(
    id="source-01",
    space_id="example-space",
    source_type=GraphSourceType.NOTE,
    title="Example Note",
    relative_path="notes/example.md",
)
note = KnowledgeObject(
    id="note-01",
    space_id="example-space",
    kind="note",
    title="Example Note",
    tags=["example-note"],
    source_refs=[
        SourceRef(
            source_id="source-01",
            source_type=GraphSourceType.NOTE,
            path="notes/example.md",
            confidence=GraphConfidence.EXTRACTED,
        )
    ],
)
space = KnowledgeSpace(
    id="example-space",
    title="Example Space",
    space_type="personal vault",
    contract_version=KNOWLEDGE_CONTRACT_VERSION,
    sources=[source],
    objects=[note],
)
```
