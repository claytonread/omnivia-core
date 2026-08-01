# Portable Knowledge Launch Packet

## Goal

Ship the portable knowledge substrate slice through the smallest coherent
implementation lanes while blocking runtime creep.

## Allowed Areas

- `README.md`
- `docs/**`
- `services/omnivia-memory/src/omnivia_memory/__init__.py`
- `services/omnivia-memory/src/omnivia_memory/knowledge/**`
- `services/omnivia-memory/tests/**`
- `services/omnivia-memory/README.md`
- `services/omnivia-memory/pyproject.toml`

## Forbidden Areas

- scanner, watcher, parser, importer, cache, or sync implementation
- provider/model, MCP, CLI, assistant installer, or hosted runtime code
- graph database or vector database clients
- UI/runtime shell behavior
- Graphify or Obsidian runtime dependencies

## Ordered Lanes

1. docs and ADR positioning confirmation
2. contract definitions
3. source ref, confidence, review, evidence, sensitivity, and version contracts
4. extension manifest and namespace rules
5. validation helpers
6. normalization helpers
7. public API exports and export-surface tests
8. positive fixtures
9. negative fixtures
10. adapter docs and examples
11. final boundary and dependency checks

## Verification Commands

```bash
PYTHONPATH=services/omnivia-memory/src python3 -m pytest \
  services/omnivia-memory/tests/test_public_api.py \
  services/omnivia-memory/tests/test_knowledge_contract.py
```

```bash
PYTHONPATH=services/omnivia-memory/src python3 -m pytest services/omnivia-memory/tests
```

## Acceptance Checks

- no Graphify dependency
- no Obsidian dependency
- no scanner, watcher, cache, importer, MCP, CLI, UI, or provider/runtime creep
- package root exports contracts and helpers only
- positive fixtures validate
- negative fixtures fail for expected reasons
- schema version checks reject unknown major versions
- decision-influencing claims and links require provenance or explicit
  `missing_evidence`
- extension namespaces reject reserved `omnivia:*` use outside official manifests
- docs still describe Core as a substrate, not an app or runtime

## Future Work Boundaries

Deferred to later repo-owned work:

- Platform lifecycle and storage/query runtime behavior
- Dev query, MCP, and CLI tooling
- App/UI knowledge experiences
- adapter importers and scanners for Obsidian-like or Graphify-like sources
