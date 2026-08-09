# omnivia-core-mcp

The Model Context Protocol server for OmniVia Core: a stdio MCP server that
gives an AI host **read-only** access to one local OmniVia Core workspace.

Built on the official Model Context Protocol Python SDK v2 (owner resolution
004, R004-05). There is no bespoke JSON-RPC or MCP stack in this package, and
no FastMCP dependency — the official SDK is the sole MCP framework dependency.

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

| Tool | Operation | Side effect |
|---|---|---|
| `workspace_inspect` | `workspace.inspect` | none (read) |

Tool schemas are projected from the public operation contracts rather than
redefined, and `tools/list` is deterministic for a given package version.

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
`omnivia-core-service`.

**The transport dependency is closed.** Owner resolution 005 R005-01 moved
`LocalIpcTransport` into `omnivia-core-client`, and `server._default_transport_factory`
constructs it. A tool call now travels an OVC1 frame over the installation-local
socket and answers with the service's own result.
`packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py` proves it against a
service the test starts; no stand-in transport remains anywhere in this package.

This package still holds no dial loop of its own and still must never depend on or
import `omnivia-core-cli`. `server.TransportFactory` survives as a test seam.
