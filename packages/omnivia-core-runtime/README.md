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

- additive migrations `0018`–`0022` for `Run`, `RunStep`, `Attempt`, `Wait`,
  `RuntimeEvent`, `Artifact`, `EvidenceItem`, `CleanupReceipt`, the rebuildable
  run-summary projection, `PolicySnapshot`/`BudgetSnapshot`, and
  `Approval`/`CapabilityGrant`;
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
  are refused as ambiguous rather than resolved by inventory order.

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

These seams are private service implementation today; no new public runtime
operation has been added to the frozen application catalogue. The following later
milestones are intentionally not claimed by this package metadata: WorkerAdapter
hosting, startup/orphan recovery, capability *dispatch* -- the gateway above
decides authority only and holds no adapter -- effect intent/receipt/settlement,
and uncertain-effect reconciliation.

The accepted substrate ownership and migration decisions are recorded in
`docs/specs/agent-runtime-substrate-reconciliation.md`.

## Runtime Execution Planes

`omnivia_core_runtime.execution` is a package-neutral seam that sits beside the
service plane rather than inside it: three frozen, content-addressed descriptors
(`RuntimeProfileDescriptor`, `ExecutorDescriptor`, `SessionProviderDescriptor`),
the plane behaviours for the reference execution classes, and a static
source-qualified exact-version `RuntimeExecutionRegistry` holding both executor
and session-provider builds.

The reference vocabularies are closed, and their members are spelled in exact
uppercase. Execution classes are `AGENT`, `DETERMINISTIC`, `EFFECT` and `WAIT`;
profile trust is `DRAFT`, `APPROVED`, `QUARANTINED` or `DISABLED`; executor and
provider build trust is `PROTOTYPE`, `APPROVED`, `QUARANTINED` or `DISABLED`;
executor kinds are `BROWSER`, `INTEGRATION`, `FILESYSTEM`, `COMMAND`, `GIT` or
`OTHER`; session kinds are `BROWSER`, `TERMINAL`, `ACP`, `REMOTE_EXECUTION` or
`OTHER`; isolation is an integer ordinal `0..3` (in-process, subprocess,
sandboxed, remote).

An executor kind is not an execution class, and the two vocabularies are
disjoint: an execution class says what shape of work a profile runs, an executor
kind says what a build is, and one `COMMAND` executor can serve capabilities a
deterministic profile and an effect profile both declare.

A profile declares `execution_classes`, `worker_routes`, `capability_sets`,
`session_providers`, a `minimum_isolation` and a `trust_state`, with optional
`policy_defaults_ref`, `evidence_rules_ref` and `completion_rule_ref`. An
executor declares an `executor_kind`, the `capabilities` its build implements,
the `supported_contract_versions` it speaks, its `required_isolation`,
`reconciliation_capabilities`, an optional `supply_chain_ref` and a required
`removal_instructions_ref`. A session provider declares its `supported_kinds`,
whether it supports suspend/resume and control transfer, and its
`required_isolation`. Both builds carry an exact three-part version and a
`sha256:` build hash.

`ComponentPlane` backs `DETERMINISTIC` and treats capability proposals as data
only — it applies none of them. `GovernedDispatchPlane` backs `EFFECT` and is a
**provisional pre-FND-F3** admission gate: it requires an `APPROVED` profile, an
accepted intent id, the exact supported contract version, a declared capability
and the current fence, and every refusal happens before the callback runs. It is
not the authority seam: the FND-F3 capability gateway documented above decides
authority from persisted `PolicySnapshot`, `Approval` and `CapabilityGrant`
records, and this plane neither reads nor replaces those records.
`StatefulSessionPlane` backs `AGENT` with a fenced lease over acquire, inspect,
snapshot, transfer of control, resume, release and reconcile — the operations the
provider build must declare support for — and reconcile answers `ACTIVE`,
`RELEASED`, `ORPHANED` or `UNKNOWN` for one external reference against the
references observably live, with deterministic evidence for each answer.

Resolution fails closed: an executor resolves only on the exact key plus a
declared capability, a listed contract version, `APPROVED` build trust and
sufficient isolation; a provider only on the exact key plus a supported session
kind, the lifecycle support asked for, `APPROVED` build trust and sufficient
isolation. Replacement and removal keep the prior descriptor's content hash and
build hash in history, and a caller pinning an exact identity is refused rather
than silently handed the replacement.

The seam holds no storage, scheduler, recovery, transport or Platform handle and
writes no canonical state; every decision it makes is a returned value or a
raised refusal. A descriptor is non-authoritative by design — declaring a
capability is a necessary and never a sufficient condition for running it — and
FND-F3 remains the owner of real governance. Descriptor identity is a SHA-256
over the public contract's RFC 8785 canonical bytes, so it is reproducible by a
second implementation.

### EP2 workflow conformance oracle (provisional, in-memory only)

`omnivia_core_runtime.execution.workflow` is a **provisional in-memory EP2
conformance oracle**, not a second runtime. Like the rest of this seam it holds
no database, scheduler, recovery, transport or Platform handle and writes no
canonical state.

`WorkflowDefinition` and `StepDefinition` are frozen, content-addressed and
author-order-independent: a step's own hash is over its component, execution
class, dependencies and optional shapes, and a workflow's hash sorts its steps
by id before hashing, so two authorings of the same graph seal to the same
hash regardless of declaration order. `materialise_workflow` derives a
`MaterialisedWorkflow` of ordered `MaterialisedStep` values from a sealed
definition using a deterministic (Kahn's-algorithm) topological order, refusing
an unknown dependency or a cycle.

A step may optionally declare a `BranchDefinition` — a tiny `EQUALS` /
`NOT_EQUALS` / `PRESENT` gate whose `evaluate` never raises, returning an
explicit `BLOCKED` result for unavailable or invalid input rather than a
default; a `LoopDefinition` with a `LoopController` that refuses to admit an
iteration once its iteration cap or budget would be exhausted; or a
`ChildWorkflowDefinition` naming another workflow's exact id, version, hash and
budget, which requires the step to declare the `WAIT` execution class.

`StepRouter` turns a materialised step into exactly one of five routes —
`AGENT`, `DETERMINISTIC`, `EFFECT`, `WAIT` (the same closed execution classes
`profile` already defines) or `CHILD_WORKFLOW` — and an `EFFECT` route only
ever carries a `CapabilityProposal` as inert data; the router never dispatches
it. `DeterministicImplementationRegistry` is a static, in-memory, fail-closed
registry for one more kind of build — a deterministic component
implementation, keyed by its own identity and version and resolved only
against the exact component and component version it claims, with replacement
and removal history preserved. `ChildWorkflowCorrelator` fences every open
parent/child correlation and rejects a stale, wrong or late child result.
`CompletionEvaluator` requires both a configured outcome and the evidence
kinds a `CompletionRule` names.

`RunOracle` ties these together for one run: replay-equivalent plan and branch
observations (observing the same step twice returns the prior observation
rather than recording a conflict), a cancellation fence that refuses any
observation once the run is cancelled, and the residual `CapabilityProposal`
values an `EFFECT` route yielded — collected as data and never dispatched.
`RunOracle` is explicitly not canonical state, a scheduler, recovery or
storage.

### Synthetic external-system conformance oracle (provisional, in-memory only)

`omnivia_core_runtime.execution.synthetic` is a deterministic, in-memory,
non-authoritative oracle over five synthetic external systems — CRM, email,
accounting, storage and webhook — proving the effect uncertainty, retry and
duplicate controls the way `planes`, `registry` and `workflow` prove their own
seams. It holds no database, scheduler, transport, credential or Platform
handle, dispatches nothing over a network, and writes no canonical state.

Every dispatch is addressed by a stable logical effect identity —
`EffectIntent.effect_key`, derived from `(system, operation, external_id)` — so
a duplicate dispatch of the same intent never creates a second logical external
effect: a completed one replays, and an uncertain one is refused rather than
repeated. `CRASH_POINTS` names where a dispatch can crash: before the intent is
recorded, after it, during the provider call, after the provider commits, and
before the local receipt is appended. Each crash point resolves to one of four
reconciliation states — `RECONCILE_APPLIED`, `RECONCILE_NOT_APPLIED`,
`RECONCILE_PARTIAL`, `RECONCILE_UNKNOWN` — and only `NOT_APPLIED` is retryable:
it is the one state treated as an explicit absence proof. `PARTIAL` and
`UNKNOWN` fail closed and can only be moved by
`SyntheticExternalOracle.reconcile`, never by a bare retry.

`SyntheticExternalOracle.reconcile` and `receive_webhook` both resolve the
*current* attempt only. A reconcile named against an attempt the oracle has
already superseded is retained as audit evidence and never overwrites the
current attempt's result; a webhook delivered twice under the same external
event id resolves once and replays its cached result on every later delivery,
so a duplicate webhook is exactly as inert as a duplicate dispatch.

`SyntheticBrowserSessionOracle` and `SyntheticNetworkPolicy` extend the same
seam to browser session lifecycle and network egress, sharing nothing with the
five effect systems beyond the vocabulary helpers in `profile`. A browser
session is a single-owner, positively-fenced lease that is either disposed or
reported as leaked, never silently active. A network request is judged against a
closed set of egress controls rather than any live DNS lookup, browser process
or transport. Neither ever stores the raw contents of a denied request — only
the host, kind and reason a caller can act on — so a prompt-injection fixture
that tries to exfiltrate a secret through a denied navigation leaves a denial
record behind and never the secret itself.
