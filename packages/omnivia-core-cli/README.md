# omnivia-core-cli

`omnivia-core-cli` is the `omnivia` CLI distribution in the OmniVia Core
package topology. It is a Core Service client: it discovers a running service,
builds contract envelopes, calls, and reports what it is told. It never owns a
workspace, holds no lease, takes no lock and opens no database.

The compile-time dependency boundary is the one PM ADR-036 defines: this
surface depends on the public `omnivia-core` contracts, and nothing in
`omnivia-core` may depend back on it.

## Commands

### Lifecycle

These work with no flags at all, against the default installation at
`~/.omnivia`. Pass `--home <dir>` to use another one.

- `omnivia start` — start the service if it is not already running, and wait for
  it to be genuinely ready. Reports `already running` rather than racing when one
  is up. The service is **launched, never imported**: `omnivia-core-service` is
  located on `PATH` (or beside the running interpreter) and spawned, which is
  what ADR-036 admits — *"MCP and CLI may locate or launch the service
  executable, but communicate only through the application API."*
- `omnivia stop` — signal the service and wait for **both** the descriptor to be
  withdrawn and the process to leave. Only the pair makes "stopped" true.
- `omnivia status` — report what a live call answered.

`status` **dials**, and that is the whole point of it. The descriptor is written
once at startup and never rewritten, so `ready: true` freezes there: a service
killed hard leaves a file still claiming health. Reading that file is not a
status check.

There is no `init` yet. Creating a workspace needs an exclusive database
connection, which this CLI must never open, so `start` refuses an unbootstrapped
workspace with the directory that is missing a manifest rather than creating one.

### Calling

- `omnivia --runtime-state <dir> discover` — show the advertised service, if any.
  Answers from the published descriptor alone; it dials nothing, so it will
  report a crashed service as ready. Use `status` to find out if one is alive.
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

The lifecycle commands are **exercised on POSIX only**. The Windows paths are
written to be correct and are not claimed to be tested: `os.kill(pid, SIGTERM)`
there calls `TerminateProcess`, which runs no handler and would leave a stale
descriptor, so `stop` sends `CTRL_BREAK_EVENT` to a process started in its own
group instead. No host in this lane can bind a named pipe to check any of it.

The product operation catalogue remains out of scope here, per the B9/B10
completion record: `workspace.inspect` and the three service-lifecycle
operations are what this CLI can reach.
