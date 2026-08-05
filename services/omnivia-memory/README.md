# OmniVia Portable Knowledge Contracts (deprecated compatibility facade)

> **`omnivia-memory` is deprecated.** It is a compatibility facade over
> `omnivia-core`. New code should depend on `omnivia-core` and import from
> `omnivia_core`. See [Migration](#migration) below.

Every one of the 183 names `omnivia_memory`'s package root advertises is
imported, unchanged, from `omnivia_core` — from the approved canonical owner or,
where the contract is published through one, the canonical barrel that re-exports
it. Either way the object is the same one: `omnivia_memory.X is
omnivia_core.<the module named below>.X` holds for all of them, and switching an
import changes nothing about the objects your code receives. Importing this package emits no
warning and writes nothing to stdout, stderr, or a logger: the deprecation notice
lives in this file and in the release metadata (PM ADR-036), never in runtime
behaviour.

## Migration

### Dependency

```diff
-dependencies = ["omnivia-memory>=0.1.0,<0.2.0"]
+dependencies = ["omnivia-core>=0.1.0,<0.2.0"]
```

### Imports

Replace the legacy root path with the canonical owner. The objects are identical,
so no other change is needed:

```diff
-from omnivia_memory import KnowledgeObject, KnowledgeSpace, KnowledgeSource, SourceRef
+from omnivia_core.knowledge import (
+    KnowledgeObject,
+    KnowledgeSpace,
+    KnowledgeSource,
+    SourceRef,
+)

-from omnivia_memory import AppManifest, validate_app_manifest
+from omnivia_core.app_manifest import AppManifest, validate_app_manifest

-from omnivia_memory import ControlPlaneManifest, LifecycleState, Policy
+from omnivia_core.control_plane import ControlPlaneManifest, LifecycleState, Policy

-from omnivia_memory import ModuleManifest, validate_module_manifest
+from omnivia_core.module_manifest import ModuleManifest, validate_module_manifest

-from omnivia_memory import RunLedgerEntry, validate_run_ledger_entry
+from omnivia_core.run_ledger import RunLedgerEntry, validate_run_ledger_entry

-from omnivia_memory import Source, SourceType
+from omnivia_core.provenance import Source, SourceType

-from omnivia_memory import ValidationResult
+from omnivia_core._shared.validation import ValidationResult

-from omnivia_memory import EvidenceGraphResponse, build_memory_graph_fixture
+from omnivia_core.memory_graph import EvidenceGraphResponse, build_memory_graph_fixture

-from omnivia_memory import MemoryCreate, MemoryUpdate
+from omnivia_core.memory.models import MemoryCreate, MemoryUpdate
```

Note the collisions: several names are published under the same spelling by more
than one domain, and the legacy root binds exactly one of each. `ValidationResult`
is the shared record, not any domain's local one; `SourceRef` is knowledge's, not
the memory graph's; `ProvenanceRequirement` is the component contract's, not the
app manifest's; `LifecycleState` is the control plane's, not the lifecycle
domain's; and `Source`/`SourceType` are provenance's, not ingestion's. Migrate
each to the owner listed above rather than to whichever module happens to export
the name.

### What is not moving yet

`Database` and `MemoryService` are importable from `omnivia_memory` but are
deliberately **not** in `__all__`, and Core does not own them. They stay in
`omnivia_memory.persistence` and `omnivia_memory.memory.service` respectively;
there is no `omnivia_core` equivalent to migrate to yet, and this facade's
removal is not what will provide one.

### Support window and removal

- This facade is supported for **at least two scheduled release trains** after
  the release that introduced this notice.
- It will be **removed only in a major release**, never in a minor or patch one.
- Removal additionally requires release-authority sign-off, published migration
  guidance, and export-drift proof that the canonical surface still covers every
  name this root advertises.

### Updating a consumer

1. Add `omnivia-core` to your dependencies (the range above) and remove
   `omnivia-memory` once no import references it.
2. Rewrite each `omnivia_memory` import to its canonical owner, using the table
   above and the collision notes.
3. Re-run your type checker. `omnivia-core` ships `py.typed`, and the canonical
   paths expose the same precise types the facade did, so a clean run before the
   change should stay clean after it.
4. If you import `Database` or `MemoryService`, keep those two imports pointed at
   `omnivia_memory` for now and track the runtime-ownership work separately.

## Included contracts

This package exposes a contract-level public API for portable knowledge
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

## Legacy Import Example

Kept for reference while the facade is supported; new code should use the
canonical form shown under [Migration](#migration) instead.

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
