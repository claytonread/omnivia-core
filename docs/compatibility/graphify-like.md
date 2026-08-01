# Graphify-like Compatibility

Compatibility here means representation-only.

Core can represent the following Graphify-like concepts without depending on
Graphify itself:

- modules, functions, classes, and documents
- extracted import/call/containment edges
- inferred document-to-code edges
- ambiguous semantic relations
- source-backed graph fragments
- namespaced annotations such as `graphify:god_node`
- namespaced relations such as `graphify:surprise_edge`
- query-result or affected-context style extensions

Boundary:

- Graphify remains reference-only
- do not add `graphifyy` as a dependency
- Core does not become a repo scanner, cache, query CLI, MCP server, or installer

See the static fixture proof:

- [Graphify-like codebase fixture](../../services/omnivia-memory/tests/fixtures/knowledge/positive/graphify_like_codebase.json)
