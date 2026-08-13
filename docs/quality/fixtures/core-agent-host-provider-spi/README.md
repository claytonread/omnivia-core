# OmniVia Core agent-host provider SPI preparation vectors

- Status: `Candidate — preparation only`; inert fixture material
- Lane: A9 (agent-host provider SPI)
- PM base commit: `a28d2c51493a1fcdd527ef1539d1b598164adf86`
- Core read-only checkpoint: `0278d3cb1da3199dd116ed0710a7459599e142df`
- Candidate document:
  `docs/tasks/2026-08-03-omnivia-core-agent-host-provider-spi-preparation.md`
- A8 candidate consumed for reconciliation:
  `docs/tasks/2026-08-03-omnivia-core-workflow-run-memory-boundary-preparation.md`,
  from maker commit `75d2ff15a21e51e23a18057c51dc8b2818e7cb8a`

## Purpose

This corpus turns the A9 candidate SPI requirements into stable,
machine-readable conformance vectors. It is preparation evidence for a future
agent-host adapter packet.

It is not a product contract, not an acceptance record, not a test suite and
not an authorization to implement an adapter. Nothing here freezes the SPI.

The directory has exactly five files:

- `README.md` — this file: authority, safety, replay and validation;
- `schema.json` — strict JSON Schema 2020-12 for the data file;
- `provider-spi-cases.json` — 42 vectors and their required outcomes;
- `SHA256SUMS` — SHA-256 of the three files above; and
- `manifest.json` — pins, sizes and hashes, including `SHA256SUMS`.

## Workflow Run boundary

This revision reconciles the corpus against the A8 Workflow Run memory boundary
candidate named above. **A8 was consumed for reconciliation only. It is not
accepted, not frozen and not authoritative**, which the corpus records as
`boundary.reconciled_against_lane_a8_candidate: true` alongside
`boundary.accepts_lane_a8_as_frozen: false`.

A8 owns the Workflow Run and run-state boundary; A9 owns host-provider lifecycle
composition and does not redefine A8. Four consequences shape the vectors:

1. **A turn is not a run.** A run holds many turns or task nodes, so
   `turn.complete` and `turn.cancel` are terminal for one identified turn only.
   Turn-scoped cases carry `turn_ordinal`, a non-authoritative lifecycle
   coordinate that is not a sixth identity and never an authorization input. A
   late effect for a completed turn fails closed (`SPI-V-009`); a later turn in
   the same run stays legal (`SPI-V-039`). The coordinate is present on **all 34**
   turn-scoped vectors and absent from **all 8** run-level ones, and the schema
   requires and forbids it accordingly — see "Hook taxonomy" below.
2. **Compaction persists nothing in Core.** `context.compact` is a host-local
   lifecycle notification the provider acknowledges or drops. It writes no Core
   run-state record and no Core audit entry (`SPI-V-008`), Core holds no run
   state on request (`SPI-V-036`), and a lossy summary reaches Core only as a
   candidate through capture (`SPI-V-037`).
3. **Turn control never composes Core job control.** `turn.cancel` never
   composes `job.cancel` and `turn.retry` never composes `job.retry`
   (`SPI-V-017`, `SPI-V-020`, `SPI-V-031`, `SPI-V-040`). Outstanding job handles
   are returned so the host can make a separate, explicitly authorized and
   idempotent application call. No hook requires `job.control`.
4. **Host identity stays out of the frozen envelope.** Agent, session, run and
   turn values are SPI-local provenance in the host-provider wrapper; the frozen
   `RequestMetadata` defines no such field and is not widened (`SPI-V-042`).

## Hook taxonomy

The ten candidate hooks partition into exactly two classes. Every hook is in one
of them, and the classes decide whether a case carries the turn coordinate.

| Class | Hooks | `given.turn_ordinal` |
| --- | --- | --- |
| Turn-scoped | `recall.before_turn`, `memory.search`, `capture.after_turn`, `tool_result.persist`, `approval.request`, `turn.complete`, `turn.cancel`, `turn.retry` | required |
| Run-level | `spi.negotiate`, `context.compact` | forbidden |

A turn-scoped case without the coordinate states no turn position, so a late
effect for a completed turn could not be distinguished from a legal effect in a
later turn of the same run. A run-level case with the coordinate implies a turn
position the hook does not have, which is the turn-is-a-run confusion the
Workflow Run boundary exists to prevent.

`schema.json` enforces both directions through `$defs/TurnScopedHook` and
`$defs/RunLevelHook`, so neither can erode by hand-editing a single case. Where
`when.repeat_of` links two turn-scoped cases, both carry the same
`turn_ordinal`, because the second delivery belongs to the same turn as the
first (`SPI-V-001`/`SPI-V-002`, `SPI-V-017`/`SPI-V-018`,
`SPI-V-022`/`SPI-V-031`).

## Authority

Interpret the vectors under these sources, in descending authority:

1. `docs/specs/omnivia-core-architecture-spec-v0.6-2026-07-29.md`, sections 12,
   14, 15 and 16;
2. `docs/tasks/2026-08-03-omnivia-core-independent-assurance-and-v06-8-preparation-plan.md`,
   lane A9;
3. the candidate document named above; and
4. the frozen Core application contract at `contracts/application/v1` in the
   read-only Core checkpoint.

The Core checkpoint was inspected read-only and never modified. Where a vector
names an error code or a retry class, the frozen Core catalogue is the
authority, not this corpus.

## Hard boundary

Every vector fixes the same facts as machine-checkable `false` constants, both
corpus-wide in `boundary` and per case in `expect`:

- no product behaviour is implemented;
- no acceptance is claimed, and A8 is not treated as frozen;
- no canonical mutation is applied;
- no agent-host source tree is patched;
- no direct workspace-storage access occurs;
- no granted capability is expanded;
- no Core run-state record is persisted;
- no Core governance decision is recorded;
- no turn cancellation implies a Core `job.cancel`;
- no turn retry implies a Core `job.retry`; and
- no host identifier is placed in the frozen `RequestMetadata`.

A future edit that needs any of these to become true is outside A9 and must be
rejected rather than accommodated. The last five are what keep the Workflow Run
boundary from eroding one vector at a time.

## Safety

The corpus is inert data.

- All identity values are opaque synthetic labels such as `caller-alpha` and
  `run-beta-0001`. None is a real principal, token, account or workspace path.
- There are no credentials, API keys, signing or key material, private content,
  or customer data.
- There are no URLs, hostnames or IP addresses, so nothing can be dereferenced
  and no vector depends on network access.
- There is no executable payload, script, archive, symlink, binary blob or
  encoded content. Every field is a bounded JSON scalar, object or array.
- String and array lengths are capped in the schema, so the corpus cannot grow
  into an oversized or decompression-hazardous artefact.
- Hostile behaviour, such as the prompt-injection vector `SPI-V-014`, is
  described in prose only. No injected instruction text is reproduced.

## Replay

These vectors are a specification of required outcomes, not an executable
harness. Replay means the same thing each time it is read: for a given `given`
and `when`, an SPI implementation must produce the stated `expect`.

- Vectors are deterministic. There is no clock, seed, randomness or ordering
  dependence beyond the explicit `sequence`, `turn_ordinal` and `elapsed_ms`
  fields.
- `when.repeat_of` names an earlier vector when the case only makes sense as a
  second delivery of that one. Apply the referenced vector first.
- Vector IDs are stable. An ID is never reused or renumbered; a withdrawn
  vector is removed and its number retired.
- A future executable conformance kit consumes this file. It must not restate
  the outcomes in code, or the two copies will drift.

## Validation

Run from the PM repository root, with a Python that has `jsonschema` installed:

```bash
python - <<'PY'
import json
from jsonschema import Draft202012Validator
root = "docs/quality/fixtures/core-agent-host-provider-spi"
schema = json.load(open(f"{root}/schema.json"))
data = json.load(open(f"{root}/provider-spi-cases.json"))
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(data)
ids = [c["id"] for c in data["cases"]]
assert len(ids) == len(set(ids)), "duplicate case id"
print(f"ok: {len(ids)} cases")
PY
```

Checksums, from inside this directory:

```bash
shasum -a 256 -c SHA256SUMS
```

`SHA256SUMS` covers `README.md`, `provider-spi-cases.json` and `schema.json` in
lexical filename order. `manifest.json` records those three files plus
`SHA256SUMS`; it carries no hash of itself, so verifying the manifest means
recomputing the four entries it lists.

Two further checks are worth running whenever the corpus changes:

1. every `expect.error_code` appears in `x-omnivia-error-catalogue` in
   `contracts/application/v1/schemas/errors.schema.json` at the Core
   checkpoint, and its `expect.retry_class` equals the catalogue's class for
   that code; and
2. every `requirements` entry resolves to an `SPI-R-*` heading in the candidate
   document, and every `when.repeat_of` resolves to a case in this file.

Four boundary assertions are worth running as standalone checks, because they
are the invariants this revision exists to protect:

```bash
python - <<'PY'
import json
root = "docs/quality/fixtures/core-agent-host-provider-spi"
data = json.load(open(f"{root}/provider-spi-cases.json"))
cases = data["cases"]

# 1. No turn cancellation or retry vector implies automatic Core job control.
turn = [c for c in cases if c["hook"] in ("turn.cancel", "turn.retry")]
assert turn and all(
    c["expect"]["implies_core_job_cancel"] is False
    and c["expect"]["implies_core_job_retry"] is False for c in turn)

# 2. Compaction vectors require no Core persistence and no Core audit entry.
comp = [c for c in cases if c["hook"] == "context.compact"]
assert comp and all(
    c["expect"]["core_run_state_persisted"] is False
    and c["expect"]["core_governance_decision_recorded"] is False
    and c["expect"]["audit_record_required"] is False for c in comp)

# 3. Host identifiers are SPI-local provenance, never frozen envelope fields.
assert all(c["expect"]["host_identity_in_request_metadata"] is False for c in cases)

# 4. Turn coordinates follow the hook taxonomy exhaustively.
TURN_SCOPED = {"recall.before_turn", "memory.search", "capture.after_turn",
               "tool_result.persist", "approval.request", "turn.complete",
               "turn.cancel", "turn.retry"}
RUN_LEVEL = {"spi.negotiate", "context.compact"}
assert TURN_SCOPED | RUN_LEVEL == set(data["hooks"])
ts = [c for c in cases if c["hook"] in TURN_SCOPED]
rl = [c for c in cases if c["hook"] in RUN_LEVEL]
assert len(ts) + len(rl) == len(cases)
assert all("turn_ordinal" in c["given"] for c in ts), "turn-scoped case without a turn"
assert all("turn_ordinal" not in c["given"] for c in rl), "run-level case with a turn"

print(f"ok: {len(turn)} turn-control, {len(comp)} compaction, "
      f"{len(ts)} turn-scoped, {len(rl)} run-level, {len(cases)} total")
PY
```

The taxonomy is worth probing as well as asserting: a schema that merely allows
the right shape would pass assertion 4 while still accepting the wrong one.
Mutate each case and require rejection.

```bash
python - <<'PY'
import copy, json
from jsonschema import Draft202012Validator
root = "docs/quality/fixtures/core-agent-host-provider-spi"
validator = Draft202012Validator(json.load(open(f"{root}/schema.json")))
data = json.load(open(f"{root}/provider-spi-cases.json"))
TURN_SCOPED = {"recall.before_turn", "memory.search", "capture.after_turn",
               "tool_result.persist", "approval.request", "turn.complete",
               "turn.cancel", "turn.retry"}

def mutate(case_id, fn):
    m = copy.deepcopy(data)
    for c in m["cases"]:
        if c["id"] == case_id:
            fn(c["given"])
    return validator.is_valid(m)

def drop(g): g.pop("turn_ordinal", None)
def insert(g): g["turn_ordinal"] = 1

removals = [c["id"] for c in data["cases"] if c["hook"] in TURN_SCOPED]
insertions = [c["id"] for c in data["cases"] if c["hook"] not in TURN_SCOPED]
assert not [i for i in removals if mutate(i, drop)], "removal accepted"
assert not [i for i in insertions if mutate(i, insert)], "insertion accepted"
print(f"ok: {len(removals)} removal probes and {len(insertions)} insertion "
      f"probes all rejected")
PY
```

## Coverage

| Family | Vectors |
| --- | --- |
| `duplicate_callback` | 5 |
| `reordered_callback` | 5 |
| `denial` | 8 |
| `cancellation` | 4 |
| `deadline` | 4 |
| `version_capability` | 6 |
| `retry_recovery` | 4 |
| `boundary` | 6 |

The vectors reference 35 of the 41 candidate requirements. The six without a
vector are structural properties of the interface shape itself — its version
identity, its hook set, the direction of calls, the request algebra and the
exactly-one-of-result-or-error rule — which are settled by reading the
candidate document rather than by exercising a case. They are listed as such in
the candidate document's traceability table rather than padded with vectors
that would assert nothing.

The four vectors added by the previous revision are `SPI-V-039` (a later turn
opens in a run whose earlier turn completed), `SPI-V-040` (turn retry asked to
retry the Core job), `SPI-V-041` (run-local approval offered as governance
authority) and `SPI-V-042` (host identity injected into the frozen envelope).
All other IDs are stable: repaired scenarios kept their numbers, and no number
was reused or retired.

This revision adds no vector and changes no requirement reference, family count
or disposition. It completes the turn coordinate the previous revision described
but did not apply everywhere: `turn_ordinal` was added to the 19 turn-scoped
vectors that lacked it and removed from the two `context.compact` vectors that
carried it, and the schema was tightened so the taxonomy is enforced rather than
observed. Coverage, IDs and outcomes are otherwise byte-for-byte the same
claims.
