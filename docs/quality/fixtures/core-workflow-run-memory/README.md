# OmniVia Core Workflow Run memory boundary fixtures

Status: **Candidate — preparation only**. Not an accepted product contract and
not acceptance evidence.

This inert fixture set records the boundary between workflow-run memory and
OmniVia Core canonical knowledge as machine-checkable cases. It implements no
product behaviour, ships no runtime, and asserts no acceptance.

The companion decision document is
`docs/tasks/2026-08-03-omnivia-core-workflow-run-memory-boundary-preparation.md`.
Where the two disagree, the accepted architecture and completion authority in
the PM base win, then that document, then this fixture set.

## Contents

| File | Role |
| --- | --- |
| `schema.json` | Strict JSON Schema 2020-12 for `workflow-run-cases.json`. |
| `workflow-run-cases.json` | 24 `RUN-V-*` boundary cases. |
| `SHA256SUMS` | SHA-256 of README, schema and data, in lexical filename order. |
| `manifest.json` | Authority pins plus path, byte count and digest per file. |

## Authority pins

- PM base commit: `a28d2c51493a1fcdd527ef1539d1b598164adf86`
- Core read-only checkpoint: `0278d3cb1da3199dd116ed0710a7459599e142df`

Every operation name, error code, capability id and identifier bound used here
was read from the Core checkpoint, not invented:

- `src/omnivia_core/contracts/v1/generated.py#OPERATION_CATALOGUE` — the frozen
  20-operation catalogue, and each operation's side effect, scope kind,
  required capability, idempotency requirement and audit category.
- `src/omnivia_core/contracts/v1/generated.py#FROZEN_ERROR_CODES` — the 26
  frozen error codes.
- `contracts/application/v1/schemas/common.schema.json` — the `Identifier`,
  `WorkspaceId`, `IdempotencyKey` and `Purpose` shapes and bounds.
- `contracts/application/v1/schemas/records.schema.json` — `CandidateAssertion`,
  `CandidateExtractionMetadata` and `SourceReference`, the only lineage
  carriers available in the frozen v1 contract.

## Safety

This directory is inert by construction.

- Every identifier is synthetic (`run.a8-*`, `ws.a8-*`, `agent.synthetic.a8`).
  No real workspace, principal, tenant, customer or run is named.
- No credentials, tokens, key material, signing material or private content.
- No executable payload, archive, symlink or binary artefact. Both data files
  are plain JSON.
- No live URL. The schema `$id` is a stable non-resolving identifier and every
  `$ref` is internal, so validation runs fully offline with no network access.
- Out-of-bounds values are **described, not embedded**. `RUN-V-22` needs a
  129-octet identifier to probe the 128-octet frozen bound; it records the
  length in a `boundary_probe` object rather than carrying an invalid value, so
  the fixture itself stays schema-valid.
- Every case asserts `fail_closed: true`. There is no case whose expected
  outcome is a silent pass, a partial write or a degraded-but-accepted result.
- Total size is a few tens of kilobytes. Nothing here needs a fetch, a build
  step or a fixture generator.

## Replay

Cases are declarative expectations, not a runnable suite. Replay means
re-deriving the same expectations from the same pins.

1. Fix the two authority pins above. A different Core checkpoint may move the
   operation catalogue or the error taxonomy, which invalidates the enums in
   `schema.json`.
2. Re-read the catalogue and taxonomy from the checkpoint and confirm the
   schema enums still match exactly — 20 operations, 26 error codes.
3. Re-run validation and the invariant checks below.
4. Recompute the digests and compare against `SHA256SUMS` and `manifest.json`.

Replay is deterministic: no timestamps, no random identifiers, no ordering
dependence, no host or platform input.

`RUN-V-10` and `RUN-V-13` describe replay *of a workflow run* against Core.
That is the modelled behaviour, not the replay of this fixture set.

## Validation

Schema validation, offline, using an installed Draft 2020-12 validator:

```bash
python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
schema = json.load(open("schema.json"))
data = json.load(open("workflow-run-cases.json"))
Draft202012Validator.check_schema(schema)
errors = list(Draft202012Validator(schema).iter_errors(data))
print("PASS" if not errors else [e.message for e in errors])
PY
```

Checksum verification:

```bash
shasum -a 256 -c SHA256SUMS
```

`SHA256SUMS` covers `README.md`, `schema.json` and `workflow-run-cases.json` in
lexical filename order. `manifest.json` additionally records the digest of
`SHA256SUMS` itself and carries no self-hash, so verify in that order: the
files against `SHA256SUMS`, then `SHA256SUMS` against `manifest.json`.

### Invariants the schema enforces

The schema is deliberately strict rather than descriptive. Every object sets
`unevaluatedProperties: false`, and the following are structural, not prose:

1. **An agent-actor run can never mutate canonical knowledge.** Any case whose
   `run_context.caller_principal_kind` is `agent` is forced to
   `canonical_mutation: false` and `mutation_authority: "none"`, and so is
   every branch of an indeterminate outcome.
2. **Canonical mutation requires a validated human governance principal.**
   `canonical_mutation: true` forces `mutation_authority` to
   `validated_human_principal` and `core_mutation` to `true`.
3. **No invented surface.** Operation names and error codes are closed enums
   drawn from the frozen catalogue and taxonomy. The product contract treats
   `ErrorCode` as an open patterned string so compatible minor releases can add
   codes; this fixture closes it so a preparation expectation cannot name a code
   that does not exist today.
4. **Idempotency matches the catalogue.** The nine mutating operations require
   an `idempotency_key`; the eleven read operations must not carry one.
5. **Scope matches the catalogue.** `workspace.create` and `workspace.list` are
   installation-scoped and must omit `requested_workspace_id`; every other
   operation must carry it.
6. **Rejections name a code.** `disposition: "rejected"` requires
   `error_code`; any other disposition forbids it.
7. **Run-local cases have no Core effect.** A case with no `ingress` is forced
   to `core_mutation: false` and `canonical_mutation: false`.
8. **Bounded, patterned identifiers** mirroring the frozen `Identifier` shape
   (`^[A-Za-z0-9][A-Za-z0-9._:-]*$`, 128 octets).
9. **Safety flags are constants**, so the safety claim above is validated
   rather than asserted.
10. **An indeterminate outcome fixes no Core-effect Boolean.**
    `disposition: "indeterminate_until_reconciled"` forces `core_mutation` and
    `core_audit_event` to `null` and requires `reconciliation_branches` to
    enumerate both legitimate outcomes, `committed_once` and `not_committed`,
    exactly once each. The `committed_once` branch is forced to
    `core_mutation: true`; the `not_committed` branch to `false`. Any other
    disposition must carry Booleans and must not carry branches.
11. **A conditional case must declare what it is conditional on.** A case whose
    `requirements` include a requirement affected by an unresolved `RUN-P-*`
    dependency is rejected unless it declares that dependency:

    | Dependency | Affected requirements |
    | --- | --- |
    | `RUN-P-01` | `RUN-R-12`, `RUN-R-13` |
    | `RUN-P-02` | `RUN-R-07`, `RUN-R-21` |
    | `RUN-P-03` | `RUN-R-06`, `RUN-R-07` |
    | `RUN-P-04` | `RUN-R-11`, `RUN-R-12` |
    | `RUN-P-05` | `RUN-R-16`, `RUN-R-17` |
    | `RUN-P-06` | `RUN-R-09`, `RUN-R-19` |

These invariants were confirmed non-vacuous by mutation probing: forty-seven
targeted corruptions of a valid instance were each rejected. Twenty-two probe
the standing invariants — an agent run mutating canonical knowledge, an
invented operation, an invented error code, a read carrying an idempotency key,
a flipped safety flag and so on. Twelve probe the indeterminate-outcome model,
including a `RUN-V-15` that fixes `core_mutation: false`, a `committed_once`
branch claiming no Core mutation, and a case modelling only the not-committed
outcome. Thirteen probe the dependency rule, including `RUN-V-08`, `RUN-V-09`
and `RUN-V-13` with their `RUN-P-05` declaration removed.

### Checks the schema cannot express

Run these alongside validation:

- `case_id` uniqueness and contiguity across `RUN-V-01`..`RUN-V-24`.
- Every `RUN-R-01`..`RUN-R-22` in the boundary document is referenced by at
  least one case, and every `requirements` entry resolves to a requirement that
  document defines.
- Every `provisional_dependencies` entry resolves to a `RUN-P-*` row in §9 of
  the boundary document, and every one of the six `RUN-P-*` rows is carried by
  at least one case.
- No agent-actor case reports `canonical_mutation: true` — schema-enforced, but
  worth asserting independently as the load-bearing claim. The population is 20
  agent cases and 4 human cases, 24 in total.

## Case categories

| Category | Cases | What it proves |
| --- | --- | --- |
| `non_mutation` | 6 | Run state cannot reach or alter canonical knowledge. |
| `approval` | 3 | Runtime approval is not a governance decision. |
| `idempotency` | 2 | Keyed retry is a no-op; key reuse with divergent payload fails closed. |
| `replay` | 1 | Whole-run replay adds no canonical mutation. |
| `cross_workspace_denial` | 2 | Run context is never an authorization channel. |
| `compression_invariance` | 2 | Compaction changes no Core-visible outcome or lineage. |
| `cancellation_race` | 2 | Cancellation outcomes are resolved by Core, not assumed. |
| `accepted_outcome_traceability` | 1 | Accepted records trace to source run and evidence. |
| `retention_deletion` | 2 | Run deletion never cascades into Core. |
| `oversized_result` | 2 | Runtime handles are not citations; promotion goes through ingestion. |
| `lineage_bounds` | 1 | Run identifiers must fit the frozen identifier bound. |

## Known limits

- Nineteen of the twenty-four cases carry `provisional_dependencies` and are
  expected to move if the dependency they name resolves differently. All six
  unresolved `RUN-P-*` dependencies are represented: `RUN-P-01` on `RUN-V-01`
  to `RUN-V-04` and `RUN-V-06`; `RUN-P-02` on `RUN-V-07`, `RUN-V-14`,
  `RUN-V-17`, `RUN-V-18` and `RUN-V-22`; `RUN-P-03` on `RUN-V-14`, `RUN-V-17`,
  `RUN-V-22` and `RUN-V-23`; `RUN-P-04` on `RUN-V-02` to `RUN-V-07`;
  `RUN-P-05` on `RUN-V-08`, `RUN-V-09`, `RUN-V-10`, `RUN-V-13` and `RUN-V-15`;
  `RUN-P-06` on `RUN-V-18`, `RUN-V-19` and `RUN-V-21`. In particular the
  idempotency and cancellation expectations are **not** frozen: their canonical
  key bytes await `RUN-P-05`. Only `RUN-V-11`, `RUN-V-12`, `RUN-V-16`,
  `RUN-V-20` and `RUN-V-24` rest solely on resolved observed facts.
- `RUN-V-15` is indeterminate by construction. It reports `core_mutation: null`
  and `core_audit_event: null` and enumerates both legitimate outcomes rather
  than asserting either. Reconciliation observes which occurred; a cancellation
  never rolls back a Core effect that already committed.
- These are boundary expectations, not conformance tests. They name no test
  runner, no adapter and no product module, and they must not be treated as
  coverage for any implementation lane.
