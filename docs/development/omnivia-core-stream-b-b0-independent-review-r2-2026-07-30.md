# OmniVia Core Stream B — B0 Independent Review, Round 2

Date: 2026-07-30
Reviewer: Claude (Stream B implementation agent)
Supersedes: `omnivia-core-stream-b-b0-independent-review-2026-07-30.md` (round 1,
which could not be signed off because the tree was changing under it)

Review subject: **`a1b1466`** — "feat(core): complete compatibility root facade"
Review method: read-only, in a **clean detached worktree at `a1b1466`** containing
no Stream B code, so the subject is Stream A's foundation alone.

## 0. Recommendation

**The foundation is sound and Phase 2 may build on it. One finding should be
resolved before T-0628 is formally closed, and it is not a defect in the code — it is
a gap in what the evidence now proves.**

| Outcome | Count |
|---|---|
| Mechanical gates reproduced | 10 of 10 |
| Blocking findings | 0 |
| Findings requiring action before T-0628 closeout | 1 (R2-01) |
| Round-1 findings now resolved | 4 of 7 |
| Phase 3+ scope violations | 0 |

Round 1's headline problem is gone: the working tree is clean, the subject is
stable, and the review reproduced end to end without the tree moving.

## 1. Independence statement

Round 1 recorded `participatedInCandidateAuthoring: false`. That remains true **of
this subject**, and deliberately so:

- The review ran in a detached worktree at `a1b1466`. `packages/omnivia-core-runtime`
  contains only its skeleton `__init__.py`; `src/omnivia_core/workspace/` contains
  only `__init__.py` and `models.py`. None of my Stream B work is present.
- Every verification below was written by me for this review and does not call the
  project's own parity helpers.

One boundary must be stated plainly. I am **not** independent of
`services/omnivia-memory/src/omnivia_memory/persistence/database.py` or its test —
I changed both for the T-0629F cutover on the Stream B branch. Those changes are not
in this subject, so they do not affect this review, but I could not review them
independently and have not tried to.

## 2. Mechanical evidence

All run from the clean worktree with `PYTHONPATH` shadowing the shared editable
install, and verified to be importing this checkout.

```text
Full repository suite       3,271 passed, 2 skipped, 0 failed   (was 2,104)
Compatibility suite         1,170 passed
Canonical migration         483 passed, 2 skipped               (was 541)
Phase 0 drift checks        6 of 6 ok
Phase 0 baseline tests      746 passed                          (was 163)
Package boundaries          passed
Application contracts       passed
Facade routes               0 leaves, 0 barrels, 0 roots remaining to convert
Ruff                        clean
Strict mypy                 clean, 53 source files
```

The canonical-migration count *fell* by 58 while the total rose by 1,167. That is
consistent with tests moving from `tests/canonical_migration` to
`tests/compatibility` as leaves converted, and is not itself a concern — but it is
the thread that leads to R2-01.

## 3. Round-1 findings, re-checked

| # | Round-1 finding | Status now |
|---|---|---|
| F-01 | AST-port gate and facade are mutually exclusive | **Resolved, and better than I proposed** — see §4 |
| F-02 | §3.4 evidence counts stale | Resolved — the plan now pins counts to `a1b1466` |
| F-03 | Package-boundary count reconciliation | Resolved, no longer applicable |
| F-04 | "14 fixtures" conflates the manifest | Not re-checked; cosmetic and unchanged |
| F-05 | `ruff format` undecided | Open — still no `[tool.ruff]` config |
| F-06 | Public contracts have filesystem coupling | Open — `workspace/models.py:113-129` unchanged |
| F-07 | Implied `generated/python/**` path | Not re-checked; cosmetic |

## 4. F-01 resolved, and the resolution is better than my recommendation

I recommended a two-way split: AST-ported leaves versus faced leaves. Stream A
implemented a **three-way** split, which is more correct:

```text
CANONICAL_TO_LEGACY                  0 leaves   (source-parity: full AST equality)
FACADE_CANONICAL_TO_LEGACY          29 leaves   (symbol identity)
SPLIT_FACADE_CANONICAL_TO_LEGACY     1 leaf     (keeps some definitions of its own)
                                    ──
                                    30 of 31 registered leaves
```

The three sets are disjoint — I verified this by measuring set intersections
directly, not by reading the test that asserts it. The 31st is
`omnivia_core._shared`, the re-export barrel documented as an exception since round 1.

The `split_facade` category is the part I did not anticipate: a leaf that is
*partially* faced is neither a pure port nor a pure facade, and treating it as
either would have been wrong. Coverage is enforced by
`test_ast_gate_covers_every_leaf_but_the_declared_split_facades`.

**Identity preservation, verified independently.** I walked all 47 declared routes,
imported both sides, and compared with `is`:

```text
routes checked        47
identical (`is`)     845
NOT identical          0
absent from legacy     0
canonical lacks name 217   (declared runtime-only; 21 runtime-only modules
                            plus per-name ROOT_FACADE_RUNTIME_IMPORTS and
                            ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS)
```

Round 1 found **zero** identical domain symbols because no facade existed. This is a
complete reversal, and it is the strongest form of the A1 requirement: not "equal",
but literally the same object.

## 5. R2-01 — Behavioural parity is now self-comparison

**Severity: not a defect; the evidence no longer proves what it states. Resolve
before T-0628 is formally closed.**

### What is true

`tests/canonical_migration/test_behavioral_parity.py` (35 tests) opens with:

> Legacy-vs-canonical behavior must match, not just shape. […] This module runs the
> same deterministic inputs through both trees and compares outputs.

Its helper resolves both sides:

```python
def _pair(canonical_name: str) -> tuple[Any, Any]:
    return importlib.import_module(canonical_name), importlib.import_module(legacy_name)
```

and a representative test then calls each side and compares:

```python
canonical.scan_sensitive_fields(payload, canonical_errors)
legacy.scan_sensitive_fields(payload, legacy_errors)
assert canonical_errors == legacy_errors
```

But after the facade cutover there is only one implementation. Measured:

```text
canonical.scan_sensitive_fields is legacy.scan_sensitive_fields -> True
  id(canonical) = 4434489280
  id(legacy)    = 4434489280
```

The comparison calls one function twice and asserts its output equals itself. It
cannot fail, whatever the function does.

This is not confined to one test. Every leaf is now a facade
(`CANONICAL_TO_LEGACY` is empty), so every `_pair()`-based comparison in the module
is in the same position.

### Why this matters more than it looks

Two independent lines of behavioural evidence existed before the cutover, and the
cutover removed both at once:

1. **Full-module AST equality** proved canonical's *source* matched the frozen
   legacy source. It now covers 0 leaves.
2. **Behavioural parity** proved canonical's *outputs* matched legacy's. It now
   compares one object with itself.

What remains pins the **surface**: the Phase 0 export inventory, the barrel
namespace gates, and symbol identity. Nothing now compares canonical *behaviour*
against the frozen Phase 0 behaviour, because after the cutover there is no
independent copy left to compare against.

Concretely: a change to a canonical method body today passes the full 3,271-test
suite. Identity still holds (both names point at the changed object), the AST gate
is empty, and behavioural parity compares the new behaviour with itself.

### What is not affected

The 35 tests are not worthless. They exercise the canonical code with real inputs
and would catch a crash, an exception-type change or a non-deterministic function,
and several carry genuine sanity assertions — `assert canonical_errors` verifies the
fixture actually triggers findings. The *comparison* half is what became vacuous, not
the whole module.

Nor is this an argument against the facade. Identity is the right end state and is
strictly better for consumers. The gap is that the parity evidence was load-bearing
during the migration and quietly stopped bearing load when the migration completed.

### Recommended correction

The cheapest honest fix is to stop claiming what the module no longer proves, and
replace the lost evidence with something that does not depend on two live trees:

1. **Correct the docstring and the assertions.** A test that cannot fail should not
   read as though it can. Either drop the `legacy` half of each comparison and keep
   the input/output assertions against canonical directly, or assert identity
   explicitly and say so.
2. **Pin behaviour against a frozen oracle rather than a second tree.** The Phase 0
   fixtures already exist. Recording expected outputs for the representative cases —
   the same deterministic inputs, with results captured once and checked in — gives
   back the property the AST gate used to provide, and survives the legacy package
   being deleted entirely, which is the declared end state.

Option 2 is the one I would argue for, because option 1 alone leaves the migration
with no behavioural evidence at all.

I have not implemented either: `tests/canonical_migration/` is Stream A's, and
choosing what replaces the evidence is a decision about how much proof the
programme wants, not a mechanical fix.

## 6. Answers to the plan §10 checklist

1. **Do the commits match PM ADR-036 and the packets?** Yes. The 22 commits since
   `55f2489` are the facade cutover A1 describes, plus the acceptance-CI gate and
   status records. §3.6 of the plan now defines the closeout boundary explicitly.
2. **Are canonical contracts behaviourally different from frozen legacy source?**
   Not detectably — and per R2-01, "not detectably" is now weaker than it was.
   Nothing found; the means of finding it has narrowed.
3. **Does every supported compatibility export preserve object identity?** **Yes,
   verified independently:** 845 identical, 0 divergent, across all 47 routes. The
   217 unported names are covered by a declared runtime-only inventory.
4. **Do all four packages build and install independently?** Yes — unchanged, and
   the boundary check passes.
5. **Are dependency guardrails structural?** Yes, unchanged from round 1: real `ast`
   and `tomllib` parsing. The round-1 caveat still stands — `importlib.import_module`
   and `__import__` evade it, so it is a lint-time guard, not a barrier.
6. **Are generated artifacts deterministic from one schema source?** Yes; the
   application-contract gate passes and round 1 verified determinism empirically.
7. **Is tolerant decoding distinct from strict validation?** Yes, unchanged.
8. **Runtime or storage leaking into public contracts?** No new leakage. F-06 stands:
   `workspace/models.py` still resolves real paths and defaults to
   `~/.omnivia/workspaces/<id>`.
9. **Does the proposed T-0629 work protect the real write seam?** Yes — and it is now
   implemented on the Stream B branch, which is outside this subject.
10. **Any Phase 3+ features without approval?** No. `omnivia-core-runtime`, `-mcp`
    and `-cli` remain skeletons in this subject.

## 7. Gate status

```text
Subject reviewed                   a1b1466, clean tree, reproducible
T-0628 formally closed             NO — local implementation complete;
                                   publication, hosted acceptance run, branch
                                   rule and PM evidence remain (plan §3.6, M1)
Blocking findings                  0
Findings before formal closeout    1 (R2-01)
Phase 2 may build on this          YES
```

## 8. Authority statement

```text
This is independent technical evidence produced by the Stream B implementation
agent, reviewing Stream A's foundation at a1b1466 in a worktree containing no
Stream B code. It is not a PM disposition and certifies nothing.

The reviewer authored omnivia_memory/persistence/database.py changes on the
Stream B branch. Those changes are not present in this subject and were not
reviewed here.

Unlike round 1, the tree did not change during this review, so these numbers
describe one commit and can be reproduced from it.
```
