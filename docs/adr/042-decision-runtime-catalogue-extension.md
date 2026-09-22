# ADR: Decision Runtime catalogue extension and Local Decisions capability

ADR ID: 042
Specification: `SPEC-CORE-DEC-001` v1.0 (Decision Runtime and Local Decisions)

## Status

Proposed — awaiting owner acceptance. Implementing the accepted catalogue
entries is tracked by the Decision Runtime implementation plan
(`docs/development/omnivia-core-decision-runtime-implementation-plan-v1.0.md`).

## Decision

Standalone Core gains an optional **Local Decisions** capability: a small,
governed decision service (the **Decision Runtime**) composed inside the
authoritative Core Service behind provider-neutral contracts. An optional
supervised local model worker (initially a Laya-CoreML adapter on qualified
Apple Silicon) executes bounded semantic assessments — boolean, choice and
ordinal — as evidence-bearing advisory evaluations. Local Decisions is
disabled until deliberately enabled; the first enabled mode is advisory; a
model probability never constitutes authority, an action grant, or canonical
knowledge.

### Catalogue amendment

The frozen application operation catalogue (`operations.schema.json`,
`x-omnivia-operation-catalogue`) is extended with the following operations,
payload schema version `decision.1`, capability grants and error posture:

| Operation | Purpose | Grant |
|---|---|---|
| `decision.status` | Model/runtime availability for the caller | status projection |
| `decision.evaluate` | Submit one bounded evaluation (durable side effects: audit/evaluation/job records) | `decision:invoke` |
| `decision.record.get` / `decision.record.list` | Inspect authorised records | `decision:read` |
| `decision.definition.list` / `decision.definition.get` | Inspect permitted template versions | `decision:read` |
| `decision.definition.publish` / `decision.definition.disable` | Manage immutable workspace templates | `decision:configure` |
| `decision.outcome.submit` | Append an evidenced outcome/correction | `decision:feedback` |
| `decision.model.list` | Approved model profiles and installation state | status projection |
| `decision.model.install` / `decision.model.remove` / `decision.model.activate` | Manage an approved profile | installation administration |
| `decision.settings.get` / `decision.settings.update` | Read or change applicable configuration (compare-and-swap revision) | read / `decision:configure` |

Ad-hoc inline definitions are available only under a separate
`decision:experimental` grant and are always advisory.

### Constraints carried by this decision

- No model, inference or worker dependency enters the public contracts
  package, CLI, MCP adapter or the default cross-platform installation.
- Deterministic policy, capability checks and action authorisation remain
  outside the model; a prediction cannot execute tools, move money, approve
  changes or publish to the Semantic Model.
- Cloud fallback is off and unimplemented in the first local release; the
  provider interface placeholder reports unavailable.
- Worker network denial and filesystem containment must be demonstrated on a
  signed build before any privacy claim ships.
- Model installation state lives in the installation-owned store, not in
  portable workspace tables.
- The existing Personal-mode local trust limitation
  (`LOCAL-IPC-PEER-IDENTITY-DEFERRED`) is carried forward unchanged.

## Why

The purpose, boundaries, threat model, probability semantics and release
gates are specified in `SPEC-CORE-DEC-001` (§1–§4, §11, §22, §28). This ADR
records only the catalogue-level decision that unblocks contracts work: the
frozen catalogue is the single source for operation names and grants, so the
capability cannot exist without this amendment, and no alternative route
(health probes, chat messages, generic job payloads or a new HTTP endpoint)
may introduce it.
