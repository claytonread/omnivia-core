# omnivia-core-runtime

`omnivia-core-runtime` is the authoritative local service and workspace runtime
in the OmniVia Core package topology. It is operational, not a skeleton: it owns
the fenced SQLite/WAL workspace substrate and its forward-only migrations, the
workspace lease, fencing and mutation guard, crash recovery and discovery, local
IPC and the HTTP/pipe transports, the durable job queue and its append-only job
history, the frozen application catalogue and its operation handlers, and
service-owned maintenance paths such as bounded local evidence capture. It also
hosts the first private service implementation of the canonical Agent Runtime
contracts: durable runtime records, command idempotency, replayable run summaries,
fenced scheduling, and durable wait transitions.

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

The OmniVia **Agent Runtime foundation is implemented here** without creating a
second queue or a second public application catalogue. Canonical, language-neutral
record shapes and semantic validators remain owned by the public `omnivia-core`
contract package. This operational package currently owns:

- additive migrations `0018`–`0020` for `Run`, `RunStep`, `Attempt`, `Wait`,
  `RuntimeEvent`, `Artifact`, `EvidenceItem`, `CleanupReceipt`, and the rebuildable
  run-summary projection;
- append/read repositories with immutable content references and degraded missing-
  blob reads;
- transactional runtime commands with aggregate sequence expectations, application
  audit records, idempotency claims, and replayed outcomes;
- incremental materialisation and full replay of the run-summary projection;
- fenced scheduling over `omnivia_durable_jobs`, including bounded stranded-claim
  recovery; and
- durable, policy-checked wait opening and single-use resolution that resumes the
  same running attempt rather than inventing `job.resume`.

These seams are private service implementation today; no new public runtime
operation has been added to the frozen application catalogue. The following later
milestones are intentionally not claimed by this package metadata: WorkerAdapter
hosting, startup/orphan recovery, policy and budget snapshots, durable approvals and
grants, capability dispatch, effect intent/receipt/settlement, and uncertain-effect
reconciliation.

The accepted substrate ownership and migration decisions are recorded in
`docs/specs/agent-runtime-substrate-reconciliation.md`.
