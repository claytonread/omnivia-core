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

- additive migrations `0018`–`0023` for `Run`, `RunStep`, `Attempt`, `Wait`,
  `RuntimeEvent`, `Artifact`, `EvidenceItem`, `CleanupReceipt`, the rebuildable
  run-summary projection, `PolicySnapshot`/`BudgetSnapshot`,
  `Approval`/`CapabilityGrant`, and `EffectIntent`/dispatch outbox/`EffectReceipt`/
  `EffectSettlement`;
- append/read repositories with immutable content references and degraded missing-
  blob reads;
- transactional runtime commands with aggregate sequence expectations, application
  audit records, idempotency claims, and replayed outcomes;
- incremental materialisation and full replay of the run-summary projection;
- fenced scheduling over `omnivia_durable_jobs`, including bounded stranded-claim
  recovery;
- durable, policy-checked wait opening and single-use resolution that resumes the
  same running attempt rather than inventing `job.resume`; and
- content-addressed, hash-verified persistence of accepted `PolicySnapshot` and
  `BudgetSnapshot` decisions, immutable and monotonic per run; and
- durable `Approval` and `CapabilityGrant` records: an approval request and its
  one decision are separate append-only facts, so a second decision is
  structurally impossible rather than merely refused, and a grant is stored as
  the canonical wire document backed by the exact `PolicySnapshot` it names; and
- a pure, fail-closed capability gateway: a proposed action, the run's persisted
  authority and a deterministic binding resolver decide one `AuthorizedInvocation`,
  which carries authority and never an adapter handle. It reads no database, holds
  no adapter and looks nothing up; the binding inventory, the records and the
  instant are all arguments. Discovery stays out of authority here too -- a
  discovered binding is excluded before selection rather than ranked below an
  approved one, and two approved bindings tying at the highest satisfying version
  are refused as ambiguous rather than resolved by inventory order; and
- a durable effect transaction -- intent, outbox, receipt, settlement -- whose
  central invariant is that **no dispatch request can be produced before its intent
  is durably committed**. A dispatch record, a receipt and a settlement each name
  their `EffectIntent` by foreign key, so no half of an effect can exist without
  one; and because the outbox row *is* how a dispatch request is recorded, a request
  produced before its intent was durable has nowhere to be written down. The one
  rule SQL cannot see -- *when* the intent's transaction committed -- is closed by
  refusing to publish from inside the caller's own open transaction and reading the
  intent in a fresh fenced one, so a rolled-back declaration leaves nothing to
  dispatch. Intent creation is idempotent at the effect level: the same
  `idempotency_key` over the same `request_digest` is one effect however often it is
  delivered, and over a different digest it is a conflict rather than a replay.
  Settlement is deterministic and fails closed -- a receipt is `committed`, an effect
  never dispatched is `not_committed`, and one dispatched without a receipt is
  `unknown`; the outcome is never a caller's argument, so no path can fabricate a
  success, and a `committed` settlement must name a stored receipt for its own
  intent. Uncertainty is not failure: an `unknown` settlement stands, a late receipt
  is retained beside it as the evidence a reconciliation is owed, and a receipt that
  contradicts a `not_committed` settlement is refused rather than resolved.

Two limits of accepted v1 shape what is stored. It records no requester identity
and gives an `Approval` no field naming a grant it authorised, so neither is
persisted. Authorising who may decide remains the wait-resolution policy seam;
persistence checks identifier shape, the immutable correlation to the request and
its wait, and the deadlines a decision must fall inside. The exact action and
state an approval is granted for stays bound by the existing `Wait.resume_digest`,
which `ResolveWait` already checks; RT-203 adds no second digest.

The gateway authorises; it does not dispatch. No accepted contract states which
effect classes require an approval, gives an `Approval` a field naming a grant, or
makes evidence mandatory for a class of action, so it invents none of them: a
supplied approval must actually authorise, and the evidence a proposal names must
be the runtime's own retained record. Those open questions are asserted as open in
the RT-204 tests rather than closed by a local rule.

The effect transaction authorises and records; it does not dispatch. The
`DispatchRequest` it produces carries the intent's authority and correlation and
never an endpoint, credential, adapter handle or request payload -- the same
boundary `AuthorizedInvocation` holds one seam earlier. Core owns the typed pure
port and the durable transaction semantics; the Platform adapter that performs the
invocation is not implemented here and is not implied by this record.

These seams are private service implementation today; no new public runtime
operation has been added to the frozen application catalogue. The following later
milestones are intentionally not claimed by this package metadata: WorkerAdapter
hosting, startup/orphan recovery, capability *dispatch* -- the gateway and the
effect transaction above decide and record, and neither holds an adapter -- and
uncertain-effect reconciliation, which is what an `unknown` settlement and its
retained late receipt are left waiting for.

The accepted substrate ownership and migration decisions are recorded in
`docs/specs/agent-runtime-substrate-reconciliation.md`.
