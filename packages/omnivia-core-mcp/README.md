# omnivia-core-mcp

`omnivia-core-mcp` is the skeleton MCP surface distribution in the OmniVia
Core package topology.

This package currently has **no operational behavior**. It exists to
establish the compile-time dependency boundary defined by PM ADR-036: the MCP
surface depends on the public `omnivia-core` contracts, and nothing in
`omnivia-core` may depend back on this package.

## Dependency direction

```text
omnivia-core-mcp  -->  omnivia-core
```

- `omnivia-core-mcp` depends on `omnivia-core`.
- `omnivia-core` must never depend on or import `omnivia_core_mcp`.
- `omnivia-core-mcp` must never depend on or import `omnivia_core_runtime`.

## Status

Skeleton only. No MCP server, protocol, or tool-serving behavior has been
added yet. Do not depend on this package for MCP functionality.
