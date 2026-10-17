# TECH-CORE-RETRIEVAL-CODE-001: P7 Python code-analysis producer

Recorded 2026-10-18. Revised the same day after Codex review; the review repairs and the
evidence they corrected are in *Review repairs* below.

## Scope: this is the Python producer slice only

P7 is **not** complete and is not claimed complete. This slice delivers the first real
code-analysis producer — a deterministic, non-executing Python source analyser over one
sealed `captured_v1` Engineering Memory snapshot — and nothing else. Work item **W10**
("Python producer") is what moved. Everything below stays open:

| Still open | Why it is open |
| --- | --- |
| **W11, the TypeScript/JavaScript producer** | Not started. P7's exit gate requires it, so P7 cannot close on this slice. |
| **Governed import and snapshot activation** | `QUALIFICATION-PLAN.json` QP-04 still requires binding candidate import and activation to the *existing* graph/governance authority by concrete symbol. This producer deliberately has no such binding: it returns values. |
| **Integrated producer delivery** | No catalogue operation, handler, adapter, CLI, MCP, HTTP surface or scheduler reaches this module. There is no production caller. |
| **Shipped transport (QP-05)** | Unchanged and NOT_RUN. No wire shape was decided or added. |
| **P8 and P9** | Untouched by this slice. |

`QUALIFICATION-PLAN.json` is unchanged: nothing in it ran and no verdict in it moved.
QP-04 remains `NOT_RUN` — it requires *both* producers plus the import binding, and this
slice supplies one producer and no binding.

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-settings` |
| Branch | `codex/core-tech-settings-integration` |
| Baseline commit | `6620bcae9bc459816be64078af8a9f373ea77b6b` (Settings v2 integration evidence) |
| Initial state | clean |
| Environment | `/Users/claytonread/Projects/omnivia-core/.venv/bin/python` 3.11.15, Ruff 0.16.1, mypy 2.3.0, with this worktree's seven source trees on `PYTHONPATH` plus a throwaway overlay carrying `pytesseract`, `pillow`, `pypdfium2` and `pillow-heif`, which the shared venv lacks and which the capture suites import at collection |

## What was added

| File | Change |
| --- | --- |
| `storage/code_analysis_python.py` | New. The analyser: profile, blob reader, parse pass, cross-file resolver, batch assembly. |
| `tests/phase3/runtime/test_code_analysis_python.py` | New. **35 test functions, 35 collected pytest cases** (no parametrisation in this file), over a real migrated workspace, a really sealed capture and really published blobs. |
| `storage/code_analysis_candidates.py` | Two narrow extensions, below. |
| `tests/phase3/runtime/test_code_analysis_candidates.py` | **+2 test functions** (7 functions, 7 cases). The five pre-existing tests are unchanged and pass; `WORK-STATUS.json` pins all five by name, and all five are still there. |

No migration, contract, generated schema, application catalogue, adapter, CLI, MCP, HTTP
or native file changed. The producer adds no operation and no durable table. **Nothing in
the repository imports either changed module** outside those two test files — verified by
`grep -rn 'code_analysis_candidates\|code_analysis_python'` over every source tree — which
is both why the blast radius is two files and why the broad-suite failures recorded below
cannot be this slice's.

### The two foundation-contract extensions

Both preserve every existing caller and all five existing foundation tests, which pass
unchanged.

1. **The candidate identifier pattern was widened from `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}`
   to `\w[\w._:/@+-]{0,255}`.** Required by the specification's Unicode-identifier
   obligation: `café`, `日本語` and a `_private` module could not be keyed by their own
   names under the narrower pattern. `\w` on a `str` pattern is Unicode-aware and still
   admits no whitespace, no control character, no quote and no separator beyond the `/`
   the punctuation class already carried, so the change is *strictly* a widening — every
   previously valid value is still valid, and nothing newly valid can escape a path, a log
   line or a JSON string.

2. **`CodeAnalysisBatch` gained an optional `profile_digest`**, defaulted to `None`, placed
   last, and validated as a `sha256:` digest when present. **It affects identity only when
   it is present.** A key spelled `null` is still a key, so writing one unconditionally
   would have moved the digest of every batch that predates the field while the format
   version went on claiming `code_analysis.v1` meant the same thing. The field is therefore
   omitted from the digest preimage, and from the adapted candidate content, when it is
   `None`. A profile-less batch digests to exactly what it digested to before the field
   existed; a batch that carries a profile has an identity distinct from the same
   candidates produced under a different configuration.

   Proved two ways. A regression reconstructs the pre-extension canonical digest document
   by hand and asserts equality. Independently, the `6620bcae` copy of the module was
   loaded beside the repaired one and handed identical candidates:

   ```
   pre-extension (6620bcae) digest: sha256:c57d4b43f2180304b7b975f3d605fe84608f0b7a3d450547bae42d3dcc29bf55
   repaired, profile-less digest:   sha256:c57d4b43f2180304b7b975f3d605fe84608f0b7a3d450547bae42d3dcc29bf55
   ```

## Review repairs

Five repairs after Codex review, all inside the two modules and their tests.

| # | Defect | Repair | Proof |
| --- | --- | --- | --- |
| 1 | A file reported `identity_unsupported` still contributed every node and edge gathered *before* the name that broke, and other files resolved imports to those leaked nodes. | The analyser for such a file is dropped whole, and every logical path it claimed joins the `unnameable` set, so another file's reference to one states `unsupported-identity`. | Pre-repair, the fixture emitted `pkg.wide` and `pkg.wide.fine` plus 2 edges sourced from the dropped file, and `pkg.caller` resolved to `pkg.wide.fine`. After: 0 nodes, 0 edges, `unsupported-identity:pkg.wide.fine`. |
| 2 | Only definitions and imports bound a name. Every other binder — parameters, assignments, annotations, augmented assignments, walrus, loop targets, `with`/`except` aliases, pattern captures, `global`/`nonlocal`, lambda parameters, comprehension targets — was ignored, so a call through such a name resolved to whatever outer definition shared its spelling. | A per-scope `dynamic` table, consulted before the static one at every level of the LEGB walk. Lambdas and comprehensions get a transient lookup scope parented to the enclosing one; the edges written inside them are attributed to the named scope that owns the text. Calls through a dynamic name are `unresolved` / `inferred_dynamic` with a bounded `dynamic-binding:<name>` descriptor. | The binder fixture produced **16 resolved call edges pre-repair, of which 13 were false**; after the repair it produces **3**, and all three are correct (the receiver case and the two scopes that genuinely do reach the outer definition). |
| 3 | `grammar_version` reached `ast.parse`'s own `assert major == 3` for `2.7`/`99.99`, and a minor newer than the running interpreter was accepted in silence while the parser went on parsing the grammar it actually has. | Both refused during profile construction with `CodeAnalysisCandidateError`. | Regressions for `2.7`, `99.99`, `4.0` and `3.<running+1>`; the running minor and `3.9` are admitted, and `PythonAnalyserProfile(grammar_version="3.11").digest == DEFAULT_PYTHON_PROFILE.digest`. |
| 4 | `profile_digest=None` entered the canonical digest document, silently changing `code_analysis.v1` identity for legacy batches. | Present-only, in both the batch digest and the adapted candidate content. | The two digests above, plus a regression that three profile states (none / A / B) give three distinct identities. |
| 5 | Evidence overstated test counts, the descriptor vocabulary, the per-file contract and the broad-suite result. | This document, rewritten against re-measured output. | Every number below was produced by a command run after the repair. |

The receiver case survives repair 2 deliberately: the first parameter of a function
written in a class body is lexically that class, which is a binding and not an inference.
It stops being one the moment the body rebinds it — `self = other` puts `self` in the
dynamic table and the call becomes `dynamic-binding:self`.

## The profile

Immutable, frozen, digestable. Its digest is the batch's `profile_digest`, and
`analyse_python_snapshot` takes an optional `expected_profile_digest` that refuses rather
than analyse under a configuration the caller did not ask for.

| Field | Default | Why it is in the identity |
| --- | --- | --- |
| `producer_id` / `producer_version` | `analyzer.python.stdlib` / `1.0.0` | Producer identity. |
| `profile_version` | `python_analysis.v1` | The profile schema itself. |
| `grammar_version` | `3.11` | Passed to `ast.parse` as `feature_version`, so the admitted language is a property of the profile rather than of whichever interpreter the service happens to run. Confirmed to have teeth: `except*` is `analysed` under `3.11` and `syntax_invalid` under `3.10`. **Bounded by the running parser:** a major other than 3, and any minor newer than the running interpreter, are refused at construction. |
| `locator_basis` | `utf8_byte_columns.v1` | CPython reports `col_offset` as a **UTF-8 byte** offset, not a character offset. Stating the basis is what lets a consumer resolve a span against the captured bytes instead of guessing which convention produced it. |
| `include_suffixes` | `.py`, `.pyi` | |
| `exclude_path_segments` | 12 cache/vendor/VCS/venv segments | Vendored and cache trees are source by suffix and nobody's code. |
| `exclude_path_prefixes` | `()` | |
| `max_files` | 500 | **Not arbitrary:** `verify_batch_binding` proves a batch by folding every distinct candidate path into one `IN (...)`, so the admitted file count *is* a host-parameter count. 500 stays inside SQLite's historic 999 ceiling — below `sql_in.SQL_VARIABLE_CHUNK`, this repository's own stated safe fold — on every build, without reaching into a shared read path to chunk it. |
| `max_file_bytes` | 1 MiB | |
| `max_total_bytes` | 32 MiB | |
| `max_ast_nodes_per_file` | 200 000 | |
| `max_candidates` | 10 000 (the contract's `MAX_CANDIDATES`) | |
| `max_file_reports` | 250 | How many admitted files may fail before the batch is refused. |

Eight single-field changes were each confirmed to move the digest, and a malformed profile
refuses its own construction (bad grammar string, a grammar the parser cannot provide,
empty or dotless suffixes, non-positive limits, `max_candidates` past the contract ceiling,
`max_file_bytes` above `max_total_bytes`, empty labels).

The default profile's digest is **unchanged by the repairs**:
`sha256:c61f5515e55325fabda8abcd2fd20b80097b069435dc43976fe439fe46248eef`.

## The byte path

Only two things are ever read: `omnivia_engineering_snapshot_files` for paths and content
digests, and the content-addressed blob store for bytes. There is no checkout anywhere in
the test suite — the fixture repository exists only as published blobs and index rows —
so "the producer never reads the live checkout" is a property the suite observes rather
than a claim in prose. One test additionally intercepts `os.open` for a whole analysis
and asserts every opened path is under the blob root it was given.

Each object is opened with `O_NOFOLLOW` and re-checked through the descriptor, exactly as
`blob_publication._verify` opens the same objects; the read is bounded by the initial
`fstat` and again cumulatively, so an object that grows between the two cannot evade the
ceiling; and the bytes are **re-hashed and compared against the address they were read
from** before they are parsed. Unlike the search projection, which may fall back to an
identity-only document, there is no weaker correct answer here, so every such failure
refuses the batch.

## What fails closed, and what does not

| Condition | Result |
| --- | --- |
| Snapshot absent, uncovered, or of another repository | `CodeAnalysisBindingError` |
| Snapshot not `captured_v1` (a real `flat_v1` snapshot recorded through the production surface) | `CodeAnalysisBindingError` |
| `capture_status` not `complete` | `CodeAnalysisBindingError` |
| `expected_profile_digest` mismatch | `CodeAnalysisBindingError` |
| A captured index path that is not repository-relative | `CodeAnalysisBindingError` |
| Blob missing, unreadable, truncated, or no longer hashing to its recorded digest | `CodeAnalysisBindingError` |
| A `grammar_version` the running parser cannot provide, or any other malformed profile field | `CodeAnalysisCandidateError`, at profile construction — before a snapshot is touched |
| Snapshot-level budget exceeded (files, total bytes, candidates, file reports) | `PythonAnalysisRefused` — analysing a prefix would produce a batch that reads like a description of the whole snapshot |
| Per-file trouble: oversize, undecodable, syntactically invalid, over the AST budget, or an identity outside the contract's bounds | one bounded, source-located `PythonFileReport`. **All-or-nothing for that file** — it contributes no node and no edge, not even what was gathered before the trouble, and the rest of the snapshot stops resolving to its identities. **Every valid fact from every other file survives.** |
| No admitted Python source, or no nameable node in any of it | `verified is None`, which is an answer rather than a failure |

## Resolution is stated, never guessed

An edge is `resolved` only when the analyser can name the exact candidate in the same
batch that the source refers to; every resolved target was additionally confirmed to be a
node in its own batch. Everything else carries an explicit descriptor, bounded to 1024
characters, and one of `unresolved`, `ambiguous` or `external`. **The descriptor
vocabulary is eleven classes, and every one of them is exercised by a test:**

| Class | Where it is exercised |
| --- | --- |
| `ambiguous-definition`, `ambiguous-name`, `dynamic-call`, `external-reference`, `relative-import-beyond-root`, `unbound-name`, `wildcard-import`, `wildcard-shadowed-name` | the multi-file fixture (nine classes in all, with `dynamic-binding`) |
| `dynamic-binding` | the multi-file fixture *and* the binder fixture |
| `unresolved-in-snapshot` | the binder fixture (`self.absent()` on a class that does not define it) |
| `unsupported-identity` | the overlong-identity fixture |

Covered behaviours, each pinned by a test:

- **Aliases.** `from .util import helper as aid` resolves past the alias to the
  definition; `import a.b` binds the top name `a` and `import a.b as c` binds `c` to the
  full path, so `a.b.f()` and `c.f()` reach the same definition.
- **Relative imports.** `from . import service` in a package's own `__init__` means
  `pkg.service` — `pkg/__init__.py` is the module `pkg` *and* is the package `pkg`. A
  level that climbs past the snapshot root is refused per requested name, never silently
  resolved to whatever sits at the top of the tree.
- **Cycles.** `util` imports `service` and `service` imports `util`: two ordinary resolved
  edges. The resolver is a table lookup, not a graph walk, so nothing recurses.
- **Lexical shadowing.** Lookup is LEGB and skips class scopes above the innermost one,
  which is Python's actual rule: a method does not see its class's attributes as bare
  names. A nested `def twice` shadows the module-level one and resolves there.
- **Value shadowing — the conservative half.** A name any scope binds by something that is
  not a definition or an import is local to the whole of that scope at compile time, so it
  is never the outer definition it shares a spelling with. Twelve binder forms are pinned
  by name: a parameter, an assignment, an annotated assignment, an augmented assignment, a
  loop target, a comprehension target, a lambda parameter, a `with` alias, an `except`
  alias, a pattern capture, a walrus target and a `global` declaration. Each yields
  `unresolved` / `inferred_dynamic` / `dynamic-binding:<name>` and names no target.
  `global` and `nonlocal` are not followed to the scope they name — a declared name is one
  this scope assigns somewhere, and an unresolved call is the honest answer.
- **Transient scopes.** A lambda and a comprehension are real Python scopes that are never
  named, so they get a lookup scope of their own and no candidate. They shadow exactly what
  they bind and nothing else: `[target() for target in items]` is dynamic, while
  `[target() for _ in items]` still reaches the module-level `def target`. The edges written
  inside them belong to the named scope that owns the text, so no edge ever names a scope
  that is not a node in the batch.
- **Overload-like redefinitions.** Two `def twice` in one scope are two source-supported
  definitions, so both are emitted; the second takes a `+2` ordinal so the batch keeps
  unique identities, and a *call* to that name is `ambiguous` rather than silently bound
  to the first or last. An `import json` and a `def json` in one scope are ambiguous too.
- **Statically resolvable attribute calls.** `self.step(...)` resolves because the
  receiver is the enclosing function's own first parameter and that function is written in
  a class body. The parameter is read from the signature rather than assumed to be `self`,
  so `cls` on a classmethod resolves the same way — and a body that rebinds it gets
  `dynamic-binding:self` instead. Base classes are **not** walked: that needs an MRO, and
  an unfound member on the lexically enclosing class is `unresolved-in-snapshot`.
- **Dynamic callees.** `table[key]()` is `dynamic-call:Subscript`;
  `service.Service({}).run()` is `dynamic-call:Attribute.run`. Both carry
  `inferred_dynamic` strength and no target.
- **Unicode.** `Café`, `préparer`, a latin-1-declared `café` behind a PEP 263 cookie, and
  a module whose own *file name* is `服務` are all keyed by their own names, and survive
  the contract decoder intact on the way to proposed memory inputs.
- **Digest binding.** One definition at the same place in two revisions of one file keeps
  its semantic key and changes both its bound `content_digest` and its
  `revision_instance_id`, which is what makes a candidate a statement about bytes rather
  than about a name. A definition that does not lexically contain the edit keeps its exact
  locator across the two; the enclosing class's own span correctly moves.

## Fixture analysis, for the record

Thirteen indexed files, two excluded by the profile, eleven admitted:

| Fact | Value |
| --- | --- |
| Profile digest | `sha256:c61f5515e55325fabda8abcd2fd20b80097b069435dc43976fe439fe46248eef` |
| Batch digest (fixture snapshot `snap-python-1`) | `sha256:838c615ad3c52946fd18def40ca118a051fc09f17a4400efcbc6336bcbfd226e` |
| Bytes read | 2 368 |
| Nodes | 35 — 9 module, 3 class, 14 function, 7 method, 1 async function, 1 async method |
| Edges | 55 — 26 `defines`, 19 `calls`, 10 `imports` |
| Resolution | 40 resolved, 7 unresolved, 5 external, 3 ambiguous |
| Strength | 37 syntactic, 14 resolved_static, 4 inferred_dynamic |
| File outcomes | 9 analysed, 1 `syntax_invalid`, 1 `decode_failed` |
| Determinism | two runs produce an identical `analysis_manifest` document |

The batch digest moved from the pre-repair record
(`sha256:12820b4745…`) by exactly one edge: `record.to_dict()` in
`pkg.service.Service.run` was `unbound-name:record` / `syntactic` and is now
`dynamic-binding:record` / `inferred_dynamic`. No node, edge or relationship count changed.

The binder fixture, for its own record: 21 nodes, 40 edges, 23 resolved / 17 unresolved,
3 `resolved_static` / 23 `syntactic` / 14 `inferred_dynamic`.

## Real repository behaviour, and the limit that actually binds

The fixtures are deliberately small, so the producer was additionally run over a real
slice of this repository's own `omnivia_core_runtime/storage` tree, captured into a sealed
snapshot the same way. Re-measured after the repair, against the repaired source (so the
byte counts differ slightly from the pre-repair record — the analyser's own file grew):

| Real files | Bytes | Time | Nodes | Edges | Candidates | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 15 | 617 934 | 0.14 s | 619 | 3 614 | 4 233 | analysed, all 15 `analysed`, deterministic across two runs |
| 30 | 1 110 521 | 0.25 s | 941 | 6 526 | 7 467 | analysed, all 30 `analysed`, deterministic across two runs |
| 45 | 1 518 542 | — | — | — | > 10 000 | **`PythonAnalysisRefused`** at the contract's candidate ceiling |
| 194 (the whole runtime package) | 6 654 712 | — | — | — | > 10 000 | **`PythonAnalysisRefused`** |

**The binding limit on real Python is `MAX_CANDIDATES`, not `max_files`.** Real modules
produce roughly 250 candidates each, so the contract's 10 000-candidate ceiling is reached
at about 35–40 files — well before the 500-file host-parameter bound, which can only bind
on a repository of many small files. Both bounds are real and guard different things; this
records which one a caller will actually meet. The refusal is correct and deliberate — a
partial batch would read like a description of the whole snapshot — but it means **this
producer cannot analyse a repository of this one's size in a single batch.**

Resolving that is not a change to this module. It needs either a raised contract ceiling
or a batching scheme, and a batching scheme has to answer what a batch *means* when
several of them describe one snapshot — which is a question for the governed import and
activation binding (QP-04), not something to invent inside a producer. Recorded here as
the first thing that work has to decide.

Resolution quality on real source, for calibration. At 30 files: 2 264 resolved, 3 046
unresolved, 1 216 external, split by descriptor as `unbound-name` 1 685,
`dynamic-binding` 903, `dynamic-call` 363, `unresolved-in-snapshot` 95.

**What repair 2 changed on real source, measured.** The pre-repair analyser was loaded
beside the repaired one and both were run over an identical 25-module snapshot of
`storage/`:

```
resolved call edges before the repair: 1191
resolved call edges after the repair : 1191
withdrawn (were resolved, now stated): 0
newly resolved (must be 0)           : 0
edges whose label moved: 789 of 5384
   unresolved/unbound-name -> unresolved/dynamic-binding: 789
```

So on this repository's own code the repair withdrew no resolved edge and invented none;
it reclassified 789 edges from "nothing binds this name" to "this scope binds it to a
value", which is the more precise of the two. **That is not evidence the defect was
harmless.** A false resolution needs a local binder whose spelling collides with an outer
definition's, and this sample happens not to contain one; the binder fixture contains
thirteen, and pre-repair all thirteen resolved to the wrong node.

## Verification

Every command below was run from `worktree-omnivia-core-tech-settings` on the repaired
tree, with the `PYTHONPATH` and overlay described under *Baseline*.

| Command | Outcome |
| --- | --- |
| `pytest …/test_code_analysis_python.py …/test_code_analysis_candidates.py -q` | **42 passed** (35 + 7) |
| `pytest …/test_code_analysis_python.py …/test_code_analysis_candidates.py …/test_engineering_captured_source_coverage.py …/test_engineering_source_coverage.py -q` | **142 passed** |
| `pytest …/test_semantic_vectors.py …/test_semantic_projection_lifecycle.py …/test_hybrid_semantic_search.py …/test_code_analysis_candidates.py tests/test_migration_allocations.py -q` (the P1 slice's own recorded command) | **107 passed** |
| `pytest tests -q` | **17 711 passed, 24 skipped** in 174.70 s |
| `pytest packages/omnivia-core-runtime/tests -q` | **1 failed, 16 188 passed, 24 skipped** in 2 198.04 s — the one failure is AT09, below |
| `ruff check src packages baseline tests scripts conformance compatibility qualification services` (CI's full scope) | All checks passed |
| `mypy --strict` over CI's exact 47-target list | Success: no issues found in **423** source files |
| `scripts/check-migration-allocations.py` | Migration allocation check passed |
| `scripts/check-package-boundaries.py` | package boundary check passed |
| `scripts/check-raise-discipline.py` | raise discipline check passed |
| `scripts/check-application-contracts.py` | Application contract conformance check passed |
| `git diff --check` and `git diff --check HEAD` | clean |

Test counts, stated unambiguously, because the earlier record conflated the two:

| File | Test functions | Collected pytest cases |
| --- | --- | --- |
| `test_code_analysis_python.py` | 35 | 35 |
| `test_code_analysis_candidates.py` | 7 | 7 |
| `test_engineering_captured_source_coverage.py` | 21 | 21 |
| `test_engineering_source_coverage.py` | 34 | **79** (parametrised) |

The earlier "105 passed" row was a *case* count over three files and was arithmetically
right; it just did not say so, and the matching function count is 60.

Not run: `./scripts/preflight` end to end. Every step of it that this slice could move ran
individually above — the four static checkers, Ruff over CI's full scope, strict mypy over
CI's target list, and the test directories that carry `tests/contracts`,
`tests/canonical_migration`, `tests/compatibility` and `tests/test_package_boundaries.py`.
What did **not** run is the distribution build/install and the isolated MCP wheel
tool-document comparison, neither of which this slice can affect: no packaging, contract,
generated-schema, catalogue or MCP file changed. Nothing in `QUALIFICATION-PLAN.json` ran,
and no verdict in it moved.

### The broad suite

A full repository run completed **before** the repairs, against commit `84ea2dbc`:

```
pytest services tests packages/*/tests
6 failed, 38083 passed, 56 skipped, 7 warnings in 2699.71s (0:44:59)
```

**That run is pre-repair evidence and nothing more.** It is cited here for the 38 083
passing cases around this slice, not as a test of the repaired commit.

The six failures were each re-run in isolation on the repaired tree *and* on a pristine
extraction of the untouched baseline commit `6620bcae` (`git archive 6620bcae | tar -x`
into a scratch directory, same interpreter, same overlay). All six reproduce identically
on the baseline, so none of them is this slice's:

| Failure | Repaired tree | Untouched `6620bcae` |
| --- | --- | --- |
| `test_at09_selected_source_end_to_end.py::test_selected_source_reaches_core_exact_and_is_left_unchanged` | 1 failed, 2 passed | 1 failed, 2 passed |
| `test_mcp_installed_verification.py` (3 cases: the restricted-setup qualification and both `legacy_mutation` parameters) | 3 failed, 17 passed | 3 failed, 17 passed |
| `test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | failed | failed |
| `test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | failed | failed |

The five MCP failures share one cause: `verify_installed_setup` spawns a real child
server, and the exchange either does not qualify (`StartupError: … did not complete an
initialize and tools/list exchange`) or advertises an inventory whose purposes do not match
the manifest (`StartupError: … does not start a server with the exposure this
installation's setup requires`). AT09 fails inside its Swift XCTest with
`XCTAssertEqual failed: ("pending") is not equal to ("admitted")`. Both are environment- or
baseline-level, and both are outside this slice's two files, which nothing else imports.

**The whole-repository suite was not re-run after the repair**, deliberately: 45 minutes
to re-prove 38 083 cases that cannot reach two modules with no importers. What was run
instead, both after the repair, is the whole of `tests/` — **17 711 passed, 24 skipped**
in 174.70 s — and the whole of the runtime package's own suite:

```
pytest packages/omnivia-core-runtime/tests -q
1 failed, 16188 passed, 24 skipped, 2 warnings in 2198.04s (0:36:38)
FAILED …/test_at09_selected_source_end_to_end.py::test_selected_source_reaches_core_exact_and_is_left_unchanged
```

That is the repaired tree, and the single failure is the AT09 row above, which fails
identically on untouched `6620bcae`. Between them those two runs cover 33 899 cases,
including every test that touches this slice. The `services/` and other package suites —
the remainder of the 38 083 — were not re-run; they are where five of the six pre-existing
failures live, each already reproduced on the baseline.

## Proof that the producer is a producer

- **It executes nothing.** A fixture module writes a sentinel file and raises
  `SystemExit` at module level. It is analysed into candidates, and the sentinel is
  absent afterwards. The module's import list is asserted to contain no `importlib`,
  `runpy`, `subprocess`, `multiprocessing`, `socket`, `ssl`, `urllib`, `http`, `ftplib`,
  `pickle`, `shutil`, `site`, `sysconfig`, `venv`, `pip`, `setuptools`, `ctypes`,
  `tomllib` or `configparser`, and no call to `eval`, `exec`, `compile`, `__import__`,
  `open` or `input`, nor any `import_module`, `system`, `popen`, `spawn` or `urlopen`
  attribute. The whole analysis surface is `ast` and `tokenize`. (`sys` was added by
  repair 3, for `sys.version_info` alone.)
- **It writes nothing.** Every `omnivia_*` table's row count is captured before and after
  a full analysis and compared; it is unchanged, including after adapting the batch to
  proposed memory inputs.
- **It owns no authority.** It imports no module whose name contains `graph`,
  `governance`, `governed`, `service.`, `transport` or `memory`. The only adaptation it
  offers is the pre-existing `to_memory_create_inputs`, whose inputs are proposed-only and
  `unreviewed`.
- **It adds no second store, server or API.** No migration, no table, no operation.

## Architecture concerns

1. **The candidate ceiling, not the file ceiling, is what a real repository meets.** See
   *Real repository behaviour* above: ~35–40 real modules exhaust `MAX_CANDIDATES`.
   Single-batch analysis of a repository this size is therefore out of reach, and the fix
   is a contract or batching decision owned by the governed-import work, not a producer
   change.
2. **`max_files` is coupled to another module's parameter fold.** The 500-file ceiling
   exists because `captured_manifest_lookup` folds every requested path into one
   `IN (...)` and its own prose scopes it to "at most the 64 dependency selectors". Raising
   the producer's ceiling means chunking that read through `sql_in.execute_in_rows` first.
   This is recorded rather than fixed, because changing a shared read path is wider than
   this slice.
3. **The module-path convention is self-consistent but not always importable.** A path
   segment containing a dot or a dash still yields a stable, unique dotted key, and
   relative imports resolve with the same function — but `lib.d/mod.py` and `lib/d/mod.py`
   collide on one logical path. The collision is handled (ordinal key, ambiguous
   definition) rather than hidden. One corner of repair 1 meets this: if a file that is
   later dropped had already claimed a logical path that a *surviving* file also claims,
   the surviving file keeps its ordinal key and the bare path stays unresolvable. The
   outcome is conservative in both directions — nothing resolves to the dropped file, and
   nothing resolves to a node by a name the source gave to two files — but it is less
   precise than it would be if the drop also unwound the ordinal. Not fixed, because
   unwinding a claim means a third pass for a collision that needs two files to spell one
   logical path.
4. **No value flow, no inheritance, no type inference.** A call through a local variable is
   `dynamic-binding`, a call to a name nothing in the snapshot binds is `unbound-name`, and
   a `self.m()` whose `m` lives on a base class is `unresolved-in-snapshot`. All three are
   deliberate: resolving them needs an MRO or an inference pass, and an invented edge is
   worse than an absent one.
5. **3.12 binders are not modelled.** `type X = ...` and PEP 695 type parameters bind names
   this producer does not record. They are unreachable today — the profile cannot declare a
   grammar newer than the running parser (repair 3), and the pinned runtime is 3.11, where
   a `type` statement is a syntax error and the file is reported `syntax_invalid`. Moving
   the service to 3.12 needs a `TypeAlias`/`type_params` branch in `_record_binders` before
   `grammar_version` may be raised, or those names will resolve to outer definitions of the
   same spelling. Recorded as a precondition on the interpreter upgrade.
6. **`ast.walk` costs one extra tree pass per file** for the node-budget check, before any
   candidate is emitted. Correct and bounded; not profiled at scale.
7. **Peak memory is one file's tree, not the snapshot's.** Trees are discarded as each file
   is parsed and references are kept as flat records, which is what lets the two-pass
   cross-file resolver stay inside the byte budget.
8. **Builtins are not special-cased.** A call to `len` is `unbound-name:len`, because `len`
   genuinely is not defined in the snapshot. Labelling it `python-builtin:` would have made
   the output depend on the running interpreter's builtins set, which the profile does not
   pin.

## ADR / PM

- **PM update (Codex):** reconcile work item **W10** in `WORK-STATUS.json` — its note still
  reads "No Python producer was implemented or run", which this record supersedes. The
  verdict vocabulary and the P1 slice's own scoping are Codex's to move, so this slice
  added a pointer to this file rather than rewriting that ledger's verdicts.
- **Before QP-04:** bind candidate import and snapshot activation to the existing
  graph/governance authority by concrete symbol, and deliver W11. No ADR is needed for this
  slice: it is internal, adds no public surface and decides no wire contract.
- **No ADR needed** for the two foundation-contract extensions. Both are additive, the
  identifier widening implements an existing specification obligation rather than changing
  a decision, and `profile_digest` leaves the published `code_analysis.v1` identity of
  every profile-less batch exactly where it was.
