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
  Answers from the published descriptor alone; it dials nothing.
- `omnivia --runtime-state <dir> health|readiness` — call `core.health` /
  `core.readiness` on the running service over the installation-local OVC1
  endpoint and render what it answers. Pass `--json` to print the request
  envelope instead of sending it.
- `omnivia --runtime-state <dir> workspace show` — call `workspace.inspect` on
  the running service over the installation-local OVC1 endpoint and render the
  workspace descriptor.

A command that dials exits non-zero when the service cannot be reached. None of
them reports on a service it did not contact.

## Dependency direction

```text
omnivia-core-cli  -->  omnivia-core-client  -->  omnivia-core
```

- `omnivia-core-cli` depends on `omnivia-core` and on `omnivia-core-client`.
- `omnivia-core` must never depend on or import `omnivia_core_cli`.
- `omnivia-core-cli` must never depend on or import `omnivia_core_runtime`.

## Status

The calling subcommands are local IPC only: a `pipe://` endpoint is refused
rather than dialled, so `health`, `readiness` and `workspace show` do not work
on Windows yet. `discover` reads the descriptor and is unaffected.

The product operation catalogue remains out of scope here, per the B9/B10
completion record: `workspace.inspect` and the three service-lifecycle
operations are what this CLI can reach.
