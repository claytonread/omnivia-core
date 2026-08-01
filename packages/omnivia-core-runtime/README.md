# omnivia-core-runtime

`omnivia-core-runtime` is the skeleton runtime distribution in the OmniVia
Core package topology.

This package currently has **no operational behavior**. It exists to
establish the compile-time dependency boundary defined by ADR-036: runtime
implementation code depends on the public `omnivia-core` contracts, and
nothing in `omnivia-core` may depend back on this package.

## Dependency direction

```text
omnivia-core-runtime  -->  omnivia-core
```

- `omnivia-core-runtime` depends on `omnivia-core`.
- `omnivia-core` must never depend on or import `omnivia_core_runtime`.
- `omnivia-core-mcp` and `omnivia-core-cli` must never depend on or import
  `omnivia_core_runtime`.

## Status

Skeleton only. No storage, service-launch, lease, or lifecycle behavior has
been added yet. Do not depend on this package for runtime functionality.
