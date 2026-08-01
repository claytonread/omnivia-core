# OmniVia Core Stream B — B0 Independent Review

Date: 2026-07-30
Reviewer: Claude (Stream B implementation agent, per AGENTS.md "Claude builds. Codex manages.")
Review type: independent, read-only. No repository file was modified by this review.
Reviewed repository: `/Users/claytonread/Projects/omnivia-core`
Review branch for this evidence: `agent/omnivia-core-stream-b` (worktree
`/Users/claytonread/Projects/worktree-omnivia-core-stream-b`)

Plan under review:
`docs/development/omnivia-core-remaining-development-project-plan-and-stream-b-handoff-2026-07-30.md`

## 0. Summary and recommendation

**Recommendation: the foundation is sound and Stream B may proceed with read-only
T-0629 preparation. B0 cannot be signed off against a stable checkpoint yet, and
operational T-0629 implementation remains correctly blocked.**

Two things, neither of which is a defect in the completed work:

1. **The checkpoint named in the plan is already superseded, and the working tree
   was being actively written by Stream A during this review.** The plan names
   `a7cd551` as "current reviewed checkpoint". By the time this review ran, HEAD
   had advanced twice and Stream A had five uncommitted modified files in the
   legacy tree. A B0 sign-off has no meaning against a tree that is changing
   under it. Details in §2.
2. **The T-0628 closeout checkpoint is not recorded.** Verified: no commit in
   `--all` matches T-0628/closeout/facade. Therefore, per the plan §8 rule 1 and
   the handoff brief, Stream B must not begin operational T-0629 code. This review
   respects that gate.

Every mechanical gate the plan claims in §3.4 reproduces on this host. One claim
is stale and two are imprecise. One architectural gap was found in the parity gate
and Stream A resolved it in-flight during this review, by the approach recommended
here. No blocking defect was found in the accepted work.

| Outcome | Count |
|---|---:|
| Gates reproduced successfully | 11 of 11 |
| Blocking findings against the accepted foundation | 0 |
| Findings requiring Stream A action before A1 closeout | 0 (F-01 resolved by Stream A in-flight) |
| Non-blocking findings / corrections | 6 (F-02 … F-07) |
| Phase 3+ scope violations found | 0 |

---

## 1. Pinned review subject

```text
Reviewed commit:   55f248945ecee5bb4c36cf7ffd4c155b550cc09e
Short:             55f2489  test(core): cover control plane behavior
Branch:            codex/ui-residual-risk-closure
Interpreter:       Python 3.11.15 (.venv)
Ruff:              0.16.0
mypy:              2.3.0
```

Lineage inspected — `117cf83` through `a7cd551` is **14 commits inclusive**
(`git rev-list --count 117cf83..a7cd551` = 13 exclusive of the base). The plan's
"14-commit lineage" and "commits `117cf83` through `a7cd551`" are consistent.

```text
117cf83 test(core): freeze phase zero migration baseline
e93fb12 build(core): establish package boundaries
f6e84fe feat(core): add provider-neutral application contracts
509b0f0 feat(core): add canonical domain model contracts
f50e928 feat(core): add canonical knowledge helpers
6f2b123 feat(core): add canonical app contract validators
f968f4e feat(core): add canonical memory graph contracts
12a8fce feat(core): add canonical component and module validators
68eadc3 feat(core): add portable contract barrels
5288573 feat(core): add canonical run ledger contracts
d866854 feat(core): add canonical control plane models
09e87a4 feat(core): add canonical control plane imports
db13b58 feat(core): add canonical control plane validation
a7cd551 feat(core): add canonical control plane barrel
```

Two commits landed after the plan was written:

```text
5679e3c 08:46:41  docs(core): add remaining development stream plan   (the plan itself)
55f2489 08:53:02  test(core): cover control plane behavior            (A1 work items 1-2)
```

---

## 2. F-01 — The review subject is moving (highest-priority finding)

**Severity: process-blocking for B0 sign-off. Not a code defect.**

### What was observed

At 08:5x during this review, `git status` in the primary checkout reported five
modified, uncommitted files:

```text
 M services/omnivia-memory/src/omnivia_memory/_shared/validation.py
 M services/omnivia-memory/src/omnivia_memory/lifecycle/models.py
 M services/omnivia-memory/src/omnivia_memory/lifecycle/rules.py
 M services/omnivia-memory/src/omnivia_memory/memory/models.py
 M services/omnivia-memory/src/omnivia_memory/provenance/models.py
```

`git diff --stat`: **35 insertions, 534 deletions**. These are the legacy sides of
exactly the parity pairs that were failing. This is A1 work item 4 — the
`omnivia_memory` compatibility facade — in progress.

Consecutive full-suite runs minutes apart returned **different results**:

| Time | HEAD | Full suite |
|---|---|---|
| run 1 | `5679e3c` | 2,019 passed, 5 warnings, 0 failed |
| run 2 | `55f2489` | 2,100 passed, 4 failed |
| run 3 | `55f2489` + uncommitted facade edits | 2,091 passed, 13 failed |

### Why the failures are not a defect

The 13 failures are all in `tests/canonical_migration/` and are the expected
consequence of the facade conversion, mid-flight:

```text
test_parity.py::test_canonical_leaf_is_an_exact_ast_port[...]        × 5
test_parity.py::test_canonical_leaf_matches_legacy_contract[...]     × 5
test_behavioral_parity.py::test_memory_*                             × 3
```

The five affected leaves are precisely `_shared.validation`, `lifecycle.models`,
`lifecycle.rules`, `memory.models`, `provenance.models` — the five files Stream A
has open.

I verified the facade is doing the right thing. `lifecycle/rules.py` now reads, in
full:

```python
"""Compatibility facade for memory lifecycle transition rules.

Deprecated: import ``CreatedBy`` / ``LifecycleRules`` from
``omnivia_core.lifecycle.rules`` instead.
"""

from omnivia_core.lifecycle.models import LifecycleState as LifecycleState
from omnivia_core.lifecycle.rules import (
    CreatedBy as CreatedBy,
    LifecycleRules as LifecycleRules,
)
```

And object identity — the actual A1 requirement — **holds**:

```text
CreatedBy:      omnivia_memory.lifecycle.rules.CreatedBy      is omnivia_core...CreatedBy      -> True
LifecycleRules: omnivia_memory.lifecycle.rules.LifecycleRules is omnivia_core...LifecycleRules -> True
```

### The actionable part: the AST-port gate and the facade are mutually exclusive

This is the finding Stream A should act on, and it is structural rather than
transient.

`tests/canonical_migration/test_parity.py::test_canonical_leaf_is_an_exact_ast_port`
asserts that the legacy module's source AST equals the canonical module's source
AST (after the `omnivia_memory`→`omnivia_core` rename). The compatibility facade's
entire purpose is to **delete** the legacy body and replace it with re-exports.

These two cannot both hold for the same module. As each leaf is faced, its
AST-port assertion must fail by design.

The plan's A1 work list does not cover this. A1.7 says "Add object-identity,
import, export-drift and deprecation-metadata tests" — additive. It does not say
"retire or gate the AST-port assertion for faced leaves". Without that, A1 cannot
reach green, and the most likely failure mode is that the AST gate gets weakened
or deleted wholesale, losing the parity evidence for the **29 leaves that are not
yet faced**.

**Recommended correction (Stream A):** make the parity gate facade-aware rather
than removing it. Partition `CANONICAL_TO_LEGACY` in
`tests/canonical_migration/_leaves.py` into an AST-ported set and a faced set,
assert AST equality for the former and object identity plus `__all__` equality for
the latter, and add a test that the two sets are disjoint and together cover every
mapped leaf. That preserves the strongest available evidence for each leaf at its
current migration stage, and makes the migration progress itself machine-checked.

**Update — resolved independently by Stream A during this review.** Minutes after
the above was written, Stream A's working tree showed exactly this partition. A new
`FACADE_CANONICAL_TO_LEGACY` mapping in `tests/canonical_migration/_leaves.py`
names the five faced leaves, documented as:

> a facade's source is an import, not a port — so they are covered instead by
> `tests/compatibility/test_facade_foundation.py`, which asserts symbol identity
> rather than source-level sameness

and a new `tests/compatibility/test_facade_foundation.py` provides the identity
assertions. `tests/canonical_migration/test_parity.py` is modified alongside it.

So the structural gap is being closed, by the recommended approach, without Stream
B intervention. **No Stream A action is outstanding from F-01.** What remains is
purely the process point below: this review's numbers were taken from a tree that
was changing, so they cannot serve as a B0 sign-off.

**Recommended correction (process):** B0 should be re-run and signed off against
the frozen T-0628 closeout commit, per the plan §8 rule 1. This document's
mechanical evidence should be treated as valid for `55f2489` only.

---

## 3. Gate reproduction

All commands run from the repository root on Python 3.11.15. Counts are actual
output, not quoted from the plan.

| Gate | Command | Result | Plan claim | Verdict |
|---|---|---|---|---|
| Full suite (at `5679e3c`) | `PYTHONPATH=. .venv/bin/pytest -q` | **2,019 passed**, 5 warnings | 2,019 | exact match |
| SWIG warnings | same | **5**, all `test_ingestion.py::TestPDFExtractor` | 5 pre-existing | exact match |
| Application-contract tests | `pytest -q tests/contracts` | **587 passed** | 587 | exact match |
| Canonical-migration tests | `pytest -q tests/canonical_migration` | **456** at `5679e3c`; **541** at `55f2489` | 456 | stale, see F-02 |
| Package boundaries | `pytest -q tests/test_package_boundaries.py` | **24 passed** | 22 (T-0628 packet) | reconciled, see F-03 |
| Phase 0 drift checks | `scripts/check-core-baseline.sh` | **6 of 6 ok** | 6 | exact match |
| Phase 0 tests | same | **163 passed** | 163 | exact match |
| Wheel builds + isolated installs | `scripts/check-package-builds.sh` | **4 of 4 built and imported**; exit 0 | 4 | exact match |
| Core wheel resources | same | **5 schemas, 14 fixture files**, no runtime deps | 5 and 14 | match, see F-04 |
| Core wheel isolation | same | none of `omnivia_core_runtime`, `omnivia_core_mcp`, `omnivia_core_cli`, `omnivia_memory`, `jsonschema` importable | implied | verified |
| Ruff | `ruff check src/omnivia_core tests baseline scripts` | **All checks passed** | clean | match, see F-05 |
| Strict mypy | `mypy --strict src/omnivia_core` | **no issues in 53 source files** | clean | exact match |
| Application-contract drift | `scripts/check-application-contracts.py` | passed, exit 0 | implied | verified |
| Static package boundaries | `scripts/check-package-boundaries.py` | passed, exit 0 | implied | verified |
| Generated TypeScript | `scripts/check-application-typescript.sh` | strict compile passed | implied | verified |

Test-collection determinism was checked explicitly: `tests/canonical_migration`
returned 541 on three consecutive runs and 541 from `--collect-only`. Collection
is deterministic; the 456/541 difference is entirely explained by `55f2489`
landing mid-review (+31 policy-compiler tests, +54 validation-contract tests = +85;
456 + 85 = 541).

---

## 4. Answers to the plan §10 review checklist

### 1. Do the 14 commits match ADR-036 and the Phase 0/1 task packets?

**Yes.** The lineage maps cleanly onto T-0628's declared work streams: `117cf83`
= Phase 0 freeze (T-0627 predecessor), `e93fb12` = work stream A (package
boundaries), `f6e84fe` = work stream B (provider-neutral contracts), and
`509b0f0`…`a7cd551` = the canonical domain/contract migration. ADR-036's four
verification expectations are each satisfied or accounted for (§4.4, §4.5, §4.8).

One packet reconciliation: T-0628 records "22 package-boundary tests passed" at
`e93fb12`; HEAD has 24. Verified as legitimate strengthening, not drift — see F-03.

### 2. Are any canonical contracts behaviourally different from their frozen legacy source?

**No, for the 29 AST-gated leaves — and the gate is genuinely strong.**
`tests/canonical_migration/_leaves.py:15-47` registers 31 canonical leaf modules;
`:51-81` maps 29 to legacy. Parity is asserted in four layers
(`test_parity.py:229-306`): public-namespace equality, per-symbol structural
equality including exact type spellings and dataclass field order
(`_strict.py::describe_symbol`), **full `ast.dump()` equality of the entire module
body**, and deterministic input/output comparison through both trees
(`test_behavioral_parity.py`). Full-module AST equality is a real backstop — it
covers private helpers and defaults that the public-symbol layers normalise away.

Two documented held-out leaves rely on hand-maintained comparison instead of the
blanket AST diff:

- `omnivia_core.graph.search_models` — `_leaves.py:83-93` holds out four
  relevance-scoring helpers that stay runtime-owned, enforced by two dedicated
  tests (`test_parity.py:409-478`).
- `omnivia_core._shared` — a 5-line re-export barrel, covered structurally by
  `test_shared_barrel_matches_legacy_all_and_bindings` (`test_parity.py:480-493`).

Both are narrow and specifically tested, but they are qualitatively weaker rigour
than the other 29. `SANCTIONED_IMPORT_REWRITES` (`test_parity.py:86-119`, 2
entries) is self-guarding: an unregistered third rewrite makes AST equality fail
rather than silently pass.

Note the plan's §3.3 phrase "the documented shared barrel/search-model exception"
is singular but covers **two distinct** held-out leaves with two distinct
mechanisms.

### 3. Does every supported compatibility export preserve object identity?

**Not yet — because the facade did not exist when this review began, and is being
written now. Where it does now exist, identity holds.**

At `55f2489`, `grep -rl "from omnivia_core\|import omnivia_core"
services/omnivia-memory/src/` returned no matches: the legacy package was still
fully self-contained. An empirical identity sweep over all 29 mapped pairs found
**0 identical domain symbols** — 397 of 435 compared pairs were not even equal,
because there were genuinely two independent definitions. The only 126 identical
pairs were shared stdlib singletons (`dataclass`, `Enum`, `datetime`), which is
CPython module caching, not a compatibility mechanism.

The four runtime-owned root bindings (`Database`, `MemoryCreate`, `MemoryService`,
`MemoryUpdate`) exist at `omnivia_memory` root and have **no** `omnivia_core`
counterpart, so identity cannot be evaluated for them at all yet.

This exactly matches the plan's own §3.5 disclosure. The disclosure is accurate
and not overstated.

For the five leaves Stream A faced during this review, identity **does** hold —
verified live (§2).

### 4. Do all four packages build and install independently?

**Yes.** `scripts/check-package-builds.sh` exit 0. All four wheels built into a
temporary offline wheelhouse and each installed into its own clean venv and
imported: `omnivia_core 0.1.0`, `omnivia_core_runtime 0.1.0`, `omnivia_core_mcp
0.1.0`, `omnivia_core_cli 0.1.0`. The Core venv resolved the documented public API
and read 5 schemas and 14 fixture files with `PYTHONPATH` unset from an isolated
cwd, and confirmed none of the sibling/legacy/validation modules were importable.

### 5. Are dependency guardrails structural rather than text-only?

**Structural, and stronger than the plan implies — but not airtight.**

`scripts/check-package-boundaries.py` uses real parsers: `tomllib.load()` for
manifests (`:145-147`) and `ast.parse` + `ast.walk` for imports (`:150-163`).
Because it uses `ast.walk` rather than inspecting only module-level nodes, it also
catches function-local, `TYPE_CHECKING`-gated and deeply nested imports. Verified
empirically against the real function with a synthetic tree:

| Evasion attempt | Caught |
|---|---|
| function-local `import omnivia_core_runtime` | yes |
| `if TYPE_CHECKING:` import | yes |
| import nested in `if`/`try` | yes |
| `importlib.import_module("omnivia_core_runtime")` | **no** |
| `__import__("omnivia_core_runtime")` | **no** |
| module `__getattr__` + importlib | **no** |
| `exec("import omnivia_core_runtime …")` | **no** |

Of the 24 boundary tests, 2 are AST-based, 1 combines AST and TOML, 9 are
TOML-metadata-based, 9 are regex over TOML **dependency strings** (never over
Python source), 1 is filesystem-existence, and 2 are aggregate. None execute
imports and introspect `sys.modules`.

This is a lint-time compile-boundary guard, not an architectural barrier. That is
an appropriate tool for the job, but it should be described as such — see F-06.

Note: `scripts/check-application-contracts.py` is stricter for the contracts
package specifically — `check_contract_package_has_no_forbidden_imports()`
(`:1148-1153`) AST-walks `src/omnivia_core/contracts` and does handle aliasing and
`importlib`/`__import__` escapes.

### 6. Are generated artifacts deterministic and derived from one schema source?

**Yes, verified empirically rather than by assertion.** The 5 canonical schemas
under `contracts/application/v1/schemas/` are the single source. Both artifacts
are emitted from one in-memory `Contract` object in `render_all()`
(`generate-application-contracts.py:1276-1279`); neither language has an
independent code path, and both carry an identical generated-file banner naming
the same source schemas.

Generation was run twice in a `/private/tmp` copy of the repo with the artifacts
deleted in between: byte-identical output both times, and both runs matched the
**committed** files exactly (`generated.py` → `a8b0cf7d…`, `index.ts` →
`0b570cdd…`). The generator imports no `datetime`, `uuid` or `random`; every
`set()` is either sorted before emission or used only for membership, and
`topological_order` (`:385-409`) breaks ties explicitly by `(source, order)` with
a comment stating the intent is stability across runs and machines.

Correction to the plan's implied layout: there is no `generated/python/**`. The
Python artifact is `src/omnivia_core/contracts/v1/generated.py`; only TypeScript
lives under `generated/`. See F-07.

### 7. Does tolerant production decoding remain distinct from strict conformance validation?

**Yes, and the separation is clean — but the divergence is wider than "unknown
fields are dropped", and it runs in both directions.**

`src/omnivia_core/contracts/v1/codec.py` imports only `json`,
`collections.abc.Mapping`, `typing.Any` and sibling contract modules — no
`jsonschema`. Repo-wide, only `scripts/check-application-contracts.py` and
`tests/contracts/test_fixtures.py` import `jsonschema`, and it is declared under
PEP 735 `[dependency-groups] dev`, never `[project].dependencies`.

Empirical behaviour matrix:

| Case | Strict (jsonschema) | Tolerant (codec) |
|---|---|---|
| valid payload | accept | accept |
| unknown top-level field | **reject** | **accept** (dropped) |
| unknown field nested in `metadata` | **reject** | **accept** |
| out-of-vocabulary open patterned string | accept | accept |
| missing required field | reject | reject |
| wrong type (top-level and nested) | reject | reject |
| `deadline_ms` below `minimum` / above `maximum` | **reject** | **accept** |
| `request_id` violating `pattern` | **reject** | **accept** |
| `api_version` violating `ContractVersion` pattern | **reject** | **accept** |

The generator documents this deliberately (`:33-37`): pattern, length and range
constraints are not re-implemented in decoders. So "tolerant decode succeeded"
does **not** imply "schema-valid". No type coercion was found — `_decode_int`
explicitly rejects `bool` despite `bool` subclassing `int` (`:620`).

The asymmetry also runs the other way: committed fixture
`duplicate-capability-ids.json` is recorded in `FROZEN_FIXTURE_MAP`
(`check-application-contracts.py:104-109`) as `schema_valid=True,
tolerant_decode=False` — the codec's semantic layer rejects duplicates that JSON
Schema structurally permits.

There are **no closed enums** in this contract — zero `"enum"` keywords across all
5 schemas; every vocabulary field is an open patterned string by design. The
plan's "explicit enum-evolution behavior" (A2.5) has no literal target today and
should be reworded to "open patterned vocabulary evolution".

### 8. Are any runtime or storage dependencies leaking into public Core contracts?

**No dependency leakage. But there is real, unguarded filesystem coupling — see
F-06.** All 53 modules under `src/omnivia_core/` import only stdlib or internal
`omnivia_core.*`. No `sqlite3`, `jsonschema`, `pydantic`, `omnivia_memory`,
`omnivia_core_runtime`, `omnivia_platform`. Occurrences of the string
`omnivia_memory` are docstrings recording migration provenance. All four
distributions declare the correct direction: Core declares no dependencies at all;
runtime, mcp and cli each declare only `omnivia-core>=0.1.0,<0.2.0`.

Two genuine filesystem couplings inside the "standard-library only" package:

- `src/omnivia_core/workspace/models.py:113-129` — `WorkspaceCreate.to_workspace()`
  calls `.expanduser().resolve()` (real filesystem stat/symlink resolution) and
  defaults `storage_path` to `Path.home() / ".omnivia" / "workspaces" / workspace_id`,
  then stores both as absolute path strings on the `Workspace` entity.
- `src/omnivia_core/ingestion/models.py:70` — `FileInventory.from_path()` calls
  `file_path.stat()`.

"Standard-library only" is true (`pathlib` is stdlib) but is not the same property
as "no filesystem coupling", and nothing in `check-package-boundaries.py` or
`tests/test_package_boundaries.py` would catch either pattern.

### 9. Does the proposed T-0629 implementation protect the real current write seam?

**The plan's T-0629F targets the right seam, and the seam is materially worse than
a single fallback path.** Full inventory in the companion preparation pack; the
load-bearing verifications:

- The implicit writable fallback is real and exactly where the plan says:
  `services/omnivia-memory/src/omnivia_memory/persistence/database.py:587-611`.
  `get_database()` with no argument calls `db_dir.mkdir(parents=True,
  exist_ok=True)` on `Path.home() / ".omnivia"` and opens `memories.db`. It is a
  module-level singleton (`_global_db`, `:584`), so the first bare call in a
  process fixes the path for every later caller regardless of the argument they
  pass.
- **`transaction()` provides no atomicity.** `database.py:511-525` yields and then
  commits, but does not suspend `auto_commit`; `execute()` (`:463-481`) and
  `executemany()` (`:483-501`) each call `self.connection.commit()` whenever
  `config.auto_commit` is true, and the default is `True` (`:26`). So every
  statement inside `with db.transaction():` commits individually. Only
  `immediate_transaction()` (`:527-580`) suspends auto-commit and issues a real
  `BEGIN IMMEDIATE`. Two production call sites rely on the weaker
  `transaction()` — `control_plane/registry.py:4199` and `:4532` — including the
  manifest replace-all sequence that deletes then re-inserts resources.
- DDL runs on **every** connection open: `_init_schema()` (`:75-427`, ~34
  statements) is called unconditionally from `connect()` (`:56`), and
  `_ensure_column()` (`:429-451`) issues `ALTER TABLE ADD COLUMN`. Both tolerate
  a read-only database by swallowing `OperationalError` rather than failing.
- Production DML: 24 sites across `persistence/repositories.py` (3),
  `ingestion/repositories.py` (7), `workspace/repository.py` (3),
  `graph/repository.py` (8) and `control_plane/registry.py` (9). No `executemany`
  call sites in production.
- A second `~/.omnivia` default independent of the DB path:
  `services/omnivia-memory/src/omnivia_memory/workspace/models.py:102-107`
  computes `Path.home()/".omnivia"/"workspaces"/<id>` which flows into
  `INSERT INTO workspaces` at `workspace/repository.py:23-25`. Fencing the
  database file alone leaves this row content pointing at the real home directory.

One scope correction for T-0629B. The packet says "stop the Phase 0 legacy oracle
importing the live runtime database". The oracle does exactly that, via **deferred**
imports that a top-of-file grep misses: `baseline/storage.py:53` and
`baseline/legacy_db.py:388` both do `from omnivia_memory.persistence.database
import Database, DatabaseConfig` inside a function. `build_storage_schema_inventory()`
(`baseline/storage.py:51-73`) derives the frozen schema by instantiating the live
`Database` against a throwaway probe file and recording
`"source": "omnivia_memory.persistence.database.Database._init_schema"`. The
"immutable oracle" is therefore regenerated from live code: if `_init_schema`
changes, the oracle moves with it and the drift becomes invisible. This must be
cut before the oracle can serve as migration evidence.

### 10. Are any Phase 3+ features being attempted without approval?

**No.** `packages/omnivia-core-runtime`, `-mcp` and `-cli` each contain exactly one
`__init__.py`, self-described as skeletons with no operational behaviour. Repo-wide
there are zero hits for `fastapi`, `uvicorn`, `aiohttp`, `starlette`, `flask`,
`grpc` or `httpx`, and no authentication, dispatcher, durable-job or pagination
implementation. `src/omnivia_core/workspace/` contains only `__init__.py` and
`models.py` — the T-0629A files (`manifest.py`, `compatibility.py`,
`schemas/workspace-manifest-v1.schema.json`) do not exist, consistent with the
plan's "Phase 2: 0% operationally". No MCP server or tool implementation exists in
this repository at all.

---

## 5. Findings

### F-01 — Review subject is moving; AST-port gate and facade are mutually exclusive
Severity: **process-blocking for B0 sign-off. No code action outstanding.**
Evidence: §2 above.
The parity/facade gap was resolved by Stream A during this review, using the
recommended partition (`FACADE_CANONICAL_TO_LEGACY` plus
`tests/compatibility/test_facade_foundation.py`). Remaining action is process only:
re-run B0 against the frozen T-0628 closeout commit.

### F-02 — Plan §3.4 canonical-migration count is stale
Severity: non-blocking (documentation)
Claim: "456 canonical-migration tests passing". Actual at `55f2489`: **541**.
Cause: `55f2489` added `test_control_plane_policy_compiler.py` (31) and
`test_control_plane_validation_contracts.py` (54). 456 + 85 = 541. The 456 figure
was accurate at `5679e3c`.
Action: §3.4 should pin its evidence to a commit, since it is now a moving figure.

### F-03 — T-0628 packet package-boundary count reconciled
Severity: none (resolved)
Packet records 22 at `e93fb12`; HEAD has 24. The two added tests are
`test_core_declares_no_runtime_dependencies_at_all` and
`test_core_wheel_force_includes_exactly_the_canonical_contract_resources` —
legitimate strengthening, not drift.

### F-04 — "14 application-contract fixtures" conflates the manifest with a fixture
Severity: non-blocking (precision)
The directory holds 14 files, one of which is `manifest.json`; there are **13**
fixtures. `tests/contracts/test_fixtures.py:70-71` asserts
`len(MANIFEST) == 13`, and `FROZEN_FIXTURE_MAP` has 13 keys. The wheel check
reporting "14 fixture file(s)" is counting files, which is defensible, but the
plan's prose should say 13 fixtures plus a manifest.

### F-05 — `ruff format` is not clean and is not a declared gate
Severity: non-blocking (observation)
`ruff check` passes cleanly, which is what §3.4 claims. `ruff format --check
src/omnivia_core tests` reports **45 files would be reformatted, 32 already
formatted**. There is no `[tool.ruff]` configuration in `pyproject.toml`, so
formatting is evidently not an intended gate. Worth an explicit decision before
A0 adds merge-blocking CI, so that "Ruff clean" in CI means the same thing it
means in the evidence.

### F-06 — Public contracts have unguarded filesystem coupling
Severity: non-blocking now; **material for T-0629A**
`src/omnivia_core/workspace/models.py:113-129` resolves real filesystem paths and
defaults workspace storage to `~/.omnivia/workspaces/<id>`, storing absolute paths
as workspace identity. `src/omnivia_core/ingestion/models.py:70` calls `.stat()`.

This matters for T-0629A specifically. The T-0629 packet requires the portable
manifest to contain no absolute paths, and the plan §7.2 assigns `workspace/models.py`
to Stream A as a compatibility surface that Stream B must not edit unilaterally.
T-0629A therefore adds a portable, path-free `WorkspaceManifest` **alongside** a
public `Workspace` whose identity is an absolute home-directory path. Both will be
exported from the same public package, and nothing prevents a consumer mixing them.

Action (integration request to Stream A, not a Stream B edit): decide and document
the boundary between `WorkspaceManifest` and the legacy `Workspace` model before
T-0629A lands — either deprecate the `Path.home()` default, or state explicitly in
`workspace/__init__.py` that `Workspace` is a non-portable compatibility model.
Also consider a boundary test asserting no `Path.home()` / `.resolve()` / `.stat()`
in the contracts package, since no current check would catch a new one.

### F-07 — Plan implies a `generated/python/**` path that does not exist
Severity: non-blocking (precision)
The Python artifact is `src/omnivia_core/contracts/v1/generated.py`. Only
TypeScript lives under `generated/`. Also, A2.5's "explicit enum-evolution
behavior" has no target: there are no closed enums in the contract.

---

## 6. Gate status and what Stream B does next

```text
T-0628 closeout checkpoint recorded:   NO  (verified: no matching commit in --all)
Operational T-0629 implementation:     BLOCKED (plan §8 rule 1, handoff brief)
Read-only T-0629 preparation:          AUTHORIZED and delivered
```

Stream B is not starting T-0629 code. The companion document
`omnivia-core-stream-b-t0629-preparation-pack-2026-07-30.md` contains the
authorized concurrent preparation: the mutation call-site inventory, the 116-case
adversarial test plan, the migration fixture oracle design, the fake clock and
process-evidence design, and the multi-process harness design.

## 7. Authority statement

```text
This is independent technical evidence produced by the Stream B implementation
agent. It is not a PM disposition and it certifies nothing.

It reviews commit 55f248945ecee5bb4c36cf7ffd4c155b550cc09e only. Because the
working tree was being actively modified by Stream A during the review, this
document must not be treated as a B0 sign-off. B0 sign-off requires a re-run
against the frozen T-0628 closeout checkpoint.

No repository file outside this document and its companion was modified.
```
