# omnivia-core-runtime

`omnivia-core-runtime` is the authoritative local service and workspace runtime
in the OmniVia Core package topology. It is operational, not a skeleton: it owns
the fenced SQLite/WAL workspace substrate and its forward-only migrations, the
workspace lease, fencing and mutation guard, crash recovery and discovery, local
IPC and the HTTP/pipe transports, the durable job queue and its append-only job
history, the frozen application catalogue and its operation handlers, and
service-owned maintenance paths such as bounded local evidence capture.

The compile-time dependency boundary defined by PM ADR-036 remains strict:
runtime implementation code depends on the public `omnivia-core` contracts, and
nothing in `omnivia-core` depends back on this package.

## Dependency direction

```text
omnivia-core-runtime  -->  omnivia-core
```

- `omnivia-core-runtime` depends on `omnivia-core`.
- `omnivia-core` must never depend on or import `omnivia_core_runtime`.
- `omnivia-core-mcp` and `omnivia-core-cli` must never depend on or import
  `omnivia_core_runtime`.

## Status

The `omnivia-core-service` executable serves the exact accepted operation and
probe surfaces. The V06-7 Standard profile installs this distribution beside
Core, Client, CLI, and MCP from wheels and qualifies their public process and
wire boundaries through `scripts/run-standard-journey.py`.

Durable work is carried by the job family: `omnivia_durable_jobs` is the fenced
scheduler row, migrations `0010` and `0015` hold its append-only attempt,
progress, checkpoint, event, control and terminal-observation history, and
`job.get`, `job.cancel`, `job.retry` and `job.events` are its public operations.
`job.retry` is the single recovery operation; there is no `job.resume`.

## Agent Runtime

The OmniVia **Agent Runtime** domain is **not implemented in this package.**
There is no canonical `Run`, `RunStep`, `Attempt`, `Wait`, `Approval`,
`CapabilityGrant`, `PolicySnapshot`, `BudgetSnapshot`, `EffectIntent`,
`EffectReceipt`, `EffectSettlement`, `RuntimeEvent`, `Artifact`, `EvidenceItem`
or `CleanupReceipt` record here, and no Runtime schema, migration, repository or
command.

The durable job family above is the substrate that Runtime persistence will
extend additively — not a Runtime implementation, and not a canonical Run model.
The reconciliation between what exists here today and the canonical Runtime
records, including the migration and contract-generation approach, is recorded in
`docs/specs/agent-runtime-substrate-reconciliation.md`.
