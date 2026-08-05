# omnivia-core-cli

`omnivia-core-cli` is the skeleton CLI surface distribution in the OmniVia
Core package topology.

This package currently has **no operational behavior**. It exists to
establish the compile-time dependency boundary defined by PM ADR-036: the CLI
surface depends on the public `omnivia-core` contracts, and nothing in
`omnivia-core` may depend back on this package.

## Dependency direction

```text
omnivia-core-cli  -->  omnivia-core
```

- `omnivia-core-cli` depends on `omnivia-core`.
- `omnivia-core` must never depend on or import `omnivia_core_cli`.
- `omnivia-core-cli` must never depend on or import `omnivia_core_runtime`.

## Status

Skeleton only. No CLI entry point or command behavior has been added yet.
Do not depend on this package for CLI functionality.
