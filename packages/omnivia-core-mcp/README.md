# omnivia-core-mcp

The Model Context Protocol server for OmniVia Core: a stdio MCP server that
gives an AI host **read-only** access to one local OmniVia Core workspace.

Built on the official Model Context Protocol Python SDK v2 (owner resolution
004, R004-05). There is no bespoke JSON-RPC or MCP stack in this package, and
no FastMCP dependency — the official SDK is the sole MCP framework dependency.

## Trusted configuration (V06-6 checkpoint A)

`omnivia_core_mcp.configuration` implements the immutable
`omnivia.mcp-config.v1` model and its explicit-path reader. The reader admits at
most 65,536 bytes, rejects duplicate or unknown fields, follows no symlink, and
requires a regular owner-private file whose identity stays unchanged throughout
the bounded read. Configuration supplies only an opaque credential reference;
it is never a credential store.

Owner-private is proved from the open descriptor on both platform families.
POSIX checks the owner and the group/other mode bits. Windows converts the
descriptor to a handle and proves, through `advapi32` alone, that the file's
owner SID is this process's token user and that the DACL is present and grants
no other principal; an unrecognised access-allowed ACE form or any API
inconsistency refuses the file. The server does not consume this model yet;
server-mode and authority integration follows.

## Running it

```bash
omnivia-core-mcp             # uses the fixed ~/.omnivia convention
omnivia-core-mcp --home /path/to/installation
```

On startup the server resolves the installation, then invokes
`omnivia-core-service --managed-start` as a subprocess. It attaches to a
compatible service if one is ready, has exactly one started if none is, and
waits for live readiness before advertising any tool.

There is **no environment variable** for the home directory, and there will not
be one (R004-11): an environment lookup is an unrestricted filesystem path
arriving under another name. Only an explicit `--home` overrides the convention.

If the workspace has not been initialised, the server refuses with an
instruction to run `omnivia init` and **creates nothing**.

## The exposed surface

`tools/list` is a curated, versioned allow-list (R004-06), not a projection of
the operation catalogue. A newly registered Core operation stays absent from MCP
until somebody adds it to `manifest.py` and tests it.

Manifest version `1.1` advertises six tools, in this order:

| Tool | Operation | Purpose | Scopes | Capability |
|---|---|---|---|---|
| `workspace_inspect` | `workspace.inspect` | `workspace_inspection` | `workspace:read` | `workspace.read` ≥ 1.0 |
| `evidence_search` | `evidence.search` | `knowledge_retrieval` | `memory:read` | `evidence.read` ≥ 1.0 |
| `knowledge_search` | `knowledge.search` | `knowledge_retrieval` | `memory:read` | `knowledge.read` ≥ 1.0 |
| `memory_search` | `memory.search` | `knowledge_retrieval` | `memory:read` | `memory.read` ≥ 1.0 |
| `graph_traverse` | `graph.traverse` | `knowledge_retrieval` | `graph:read` | `graph.read` ≥ 1.0 |
| `context_pack_build` | `context_pack.build` | `knowledge_retrieval` | `memory:read` | `context_pack.build` ≥ 1.0 |

Every one of them declares `side_effect: none` and `audit_category: read` in the
operation catalogue, and the manifest refuses at import to admit anything else.
Scopes, the capability identifier and its minimum version, and the idempotency
hint are read off the catalogue entry rather than restated here — a model can
neither supply nor override the principal, the workspace, the scopes, the
purpose, the capability, or the service endpoint. `tools/list` is deterministic
for a given package version.

### Schemas

Each tool advertises both an `inputSchema` and an `outputSchema`, and both are
**self-contained**: the canonical Application Contract v1 definition verbatim,
plus its complete transitive `$defs` closure, with every
`https://contracts.omnivia.dev/...` reference rewritten to a local `#/$defs/...`
one. No advertised schema needs network resolution.

They are generated, never transcribed:

```bash
python scripts/generate-mcp-exposure-schemas.py           # regenerate
python scripts/generate-mcp-exposure-schemas.py --check    # gate (preflight, Core acceptance)
```

`src/omnivia_core_mcp/generated_schema_projection.py` is the emitted artifact and
is generator-owned; edit the canonical schemas and regenerate instead. It is a
checked-in module rather than a read of the packaged canonical schemas because
those are force-included into the `omnivia-core` *wheel* and absent from an
editable install — reading them would make `tools/list` depend on how Core was
installed. The generated module is present, and identical, in both.

### Results

A successful call returns `structuredContent` equal to the contract-encoded
operation result, plus exactly one JSON text item whose parsed value equals that
same document. A failure — an unknown tool, an unadvertised argument, an
unreachable service, a refusal from the service — is an MCP tool error carrying a
readable message and **no** `structuredContent`.

### Never exposed as model-callable tools

Service start, stop, health, readiness, status and discovery; bootstrap and
workspace initialisation; unrestricted filesystem path selection; administrative
configuration; and every destructive or persistent mutation. These are not
merely unadvertised — the allow-list is the only lookup the call path has, so an
operation absent from it is not callable.

Read-first is enforced at import: an entry whose catalogue metadata is not
`side_effect="none"` and `audit_category="read"` makes the package fail to load.

## Lifecycle

- The MCP process does **not** own the workspace lease.
- The MCP process does **not stop** a service it started when the session ends.
  A service started here is an independent Core service, stopped only by
  `omnivia stop` or an authorised platform lifecycle action.
- **stdout is protocol-only.** Diagnostics and child-process output go to stderr.
  A startup failure writes not one byte of protocol and exits non-zero.

## Dependency direction

```text
omnivia-core-mcp  -->  omnivia-core
                  -->  omnivia-core-client
                  -->  mcp (>=2,<3)
```

R004-05 fixes that list. This package must never depend on or import
`omnivia-core-cli`, `omnivia_core_runtime`, a Desktop or Platform package, or a
database implementation — and `omnivia-core` must never depend back on it.

## Status

The tool surface, the exposure manifest, managed start, the stdio server and the
call path are complete and tested end to end against a real MCP client and a real
`omnivia-core-service`. `tests/test_mcp_stdio_end_to_end.py` calls all six tools
over stdio against one governed workspace whose evidence, governed records and
sealed relations were written through the accepted fenced Runtime writers in
`tests/_mcp_v06_3_fixture.py` — the only place in this package's tests that
imports the runtime at all.

**The transport dependency is closed.** Owner resolution 005 R005-01 moved
`LocalIpcTransport` into `omnivia-core-client`, and `server._default_transport_factory`
constructs it. A tool call now travels an OVC1 frame over the installation-local
socket and answers with the service's own result.
`packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py` proves it against a
service the test starts; no stand-in transport remains anywhere in this package.

This package still holds no dial loop of its own and still must never depend on or
import `omnivia-core-cli`. `server.TransportFactory` survives as a test seam.
