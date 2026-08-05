# omnivia-core-cli

`omnivia-core-cli` is the `omnivia` CLI distribution in the OmniVia Core
package topology. It is a Core Service client: it discovers a running service,
builds contract envelopes, calls, and reports what it is told. It never owns a
workspace, holds no lease, takes no lock and opens no database.

The compile-time dependency boundary is the one PM ADR-036 defines: this
surface depends on the public `omnivia-core` contracts, and nothing in
`omnivia-core` may depend back on it.

## Commands

- `omnivia --runtime-state <dir> discover` — show the advertised service, if any.
- `omnivia --runtime-state <dir> health|readiness` — emit the request envelope.
- `omnivia --runtime-state <dir> workspace show` — call `workspace.inspect` on
  the running service over the installation-local OVC1 endpoint and render the
  workspace descriptor.

## Dependency direction

```text
omnivia-core-cli  -->  omnivia-core-client  -->  omnivia-core
```

- `omnivia-core-cli` depends on `omnivia-core` and on `omnivia-core-client`.
- `omnivia-core` must never depend on or import `omnivia_core_cli`.
- `omnivia-core-cli` must never depend on or import `omnivia_core_runtime`.

## Status

`workspace show` is the first subcommand that calls a service rather than
printing the envelope it would have sent. It is local IPC only: a `pipe://`
endpoint is refused rather than dialled, so the command does not work on
Windows yet.
