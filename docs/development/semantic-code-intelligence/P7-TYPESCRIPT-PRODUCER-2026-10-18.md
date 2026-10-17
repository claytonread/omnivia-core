# TECH-CORE-RETRIEVAL-CODE-001: P7 TypeScript/JavaScript code-analysis producer (W11)

Recorded 2026-10-18.

## Scope: this is the TypeScript/JavaScript producer slice only

This slice delivers work item **W11**: a deterministic, non-executing TypeScript and
JavaScript analyser over one sealed `captured_v1` Engineering Memory snapshot, built on the
pinned TypeScript compiler's own `Program` and `TypeChecker`, and packaged so an installed
distribution runs it without any repository-local dependency. With W10
(`P7-PYTHON-PRODUCER-2026-10-18.md`) both P7 producers now exist. P7 is still **not**
complete:

| Still open | Why it is open |
| --- | --- |
| **Governed import and snapshot activation** | `QUALIFICATION-PLAN.json` QP-04 still requires binding candidate import and activation to the existing graph/governance authority by concrete symbol. Like the Python producer, this one returns values and binds to nothing. |
| **Integrated producer delivery** | No catalogue operation, handler, adapter, CLI, MCP, HTTP surface or scheduler reaches this module. There is no production caller. |
| **Shipped transport (QP-05)**, **P8**, **P9** | Untouched. |

`QUALIFICATION-PLAN.json` is unchanged: nothing in it ran and no verdict moved. QP-04
stays `NOT_RUN` — it needs the import binding, which no producer supplies.

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-settings` |
| Branch | `codex/core-tech-settings-integration` |
| Baseline commit | `27fbe9c3` (P7 dropped-file staging repair) |
| Environment | main checkout's `.venv` (CPython 3.11.15, Ruff 0.16.1, mypy) with this worktree's seven source trees on `PYTHONPATH` plus the OCR overlay the capture suites import at collection; Node v22.23.2; TypeScript 5.9.3 from `npm ci` |

## What was added

| File | Change |
| --- | --- |
| `storage/code_analysis_typescript.cts` | New. The analyser's canonical source: one request on stdin, one answer on stdout, in-memory `CompilerHost`, one `Program` per owning project. |
| `storage/code_analysis_typescript.cjs` | New, generated. The deterministic `--strict` compile of the `.cts`; what actually runs. Marked `-text` in `.gitattributes`. |
| `storage/code_analysis_typescript.py` | New. The adapter: profile, sealed-snapshot binding, blob re-hash, confined Node run, fail-closed answer validation, UTF-16 → byte-span conversion, batch assembly. |
| `scripts/generate-code-analysis-typescript.py` | New. Compiles the `.cts` with the pinned compiler and writes the `.cjs` **and** its `ANALYSER_DIGEST` pin; `--check` fails if either differs. |
| `packages/omnivia-core-runtime/hatch_build.py` + `pyproject.toml` | New wheel build hook: copies the pinned compiler (`lib/typescript.js`, the 100 `lib*.d.ts`, `package.json`, licence and notice) into `omnivia_core_runtime/storage/typescript_compiler/`, after verifying it against the adapter's `TYPESCRIPT_VERSION` and `TYPESCRIPT_DIGEST`. Editable installs are left alone. |
| `scripts/check-package-builds.sh` | The installed runtime wheel, from a temporary cwd with `PYTHONPATH` unset, verifies its packaged compiler against its pins and runs one real analysis on it. |
| `.github/workflows/phase2-platform.yml` | `Set up Node` + `npm ci` steps, because both of its wheel-building steps build the runtime wheel, which now needs the compiler. No job name, matrix row or existing step changed. |
| `storage/code_analysis_candidates.py` | Two additive extensions, below. |
| `tests/phase3/runtime/test_code_analysis_typescript.py` | New. **19 test functions, 32 collected cases** (one test is parametrised over 14 tampered answers). |
| `tests/phase3/runtime/test_code_analysis_candidates.py` | **+2 test functions** (9 functions, 9 cases). |

No migration, contract, generated schema, catalogue, adapter, CLI, MCP or HTTP file
changed. Nothing in the repository imports the new module outside its test file and the
package-build gate.

### The two candidate-contract extensions

Both are strictly additive; every pre-existing candidate test passes unchanged.

1. **The identifier pattern admits `$` and `#`**: `[\w$#][\w._:/@+$#-]{0,255}`. They are
   ECMAScript identifier characters (`$store`, a private `#count`), so a declaration keys
   by its own spelling. Neither quotes, separates nor escapes anything; every previously
   valid value stays valid.
2. **`CodeLocator` gained an optional, paired `start_byte`/`end_byte`** — a half-open range
   into the exact captured bytes. Both or neither; integers only, bounded by
   `MAX_LOCATOR_VALUE`. Like `profile_digest`, an absent span is an absent key, so every
   locator built without one documents and digests exactly as before.

## Semantics come from the compiler

Every source file is analysed in exactly one `Program`: its nearest including
`tsconfig.json`/`jsconfig.json` project (deepest config directory that is an ancestor;
ties to the first config by path), else one inferred `Program`
(`allowJs`, JSX preserve, ESNext/Bundler, `types: []`). Every `Program` is built from
scratch over the whole request, so a changed source, config or dependency re-resolves
everything; there is no cache across revisions. Project references are followed for
discovery (bounded by `max_references`) but not passed to `createProgram`, so a reference
resolves to the referenced *source*, which is what a snapshot has.

| Requirement | Behaviour (each pinned by a test) |
| --- | --- |
| TS / TSX / JS / JSX / `.mts` / `.cts` / `.mjs` / `.cjs` | All admitted; `.js` CommonJS `require("x")` is an `imports` edge, and calls through it resolve via the checker. |
| Declarations | Functions, classes, interfaces, type aliases, enums and members, namespaces, ambient `declare module "x"`, `declare global`, top-level variables and destructured bindings, class/interface members (methods, properties, accessors, constructors, parameter properties, `static:` and `#private` members). |
| Identity | `<path>:<qualified name>`, `+N` ordinal for a repeated name in one file, never a compiler symbol or node id: same bytes, same key, in every `Program` and run. |
| Overload and merge groups | `<first key>@group` (`overload_group` when every declaration is the same function-like kind, else `merge_group`), with a `groups` edge to each member that is a node in the batch. |
| Implementation vs signature | A call resolves to the **overload signature the checker chose** (`f(1)` → `f`, `f("…")` → `f+2`), never to the implementation; an import of an overloaded or merged name resolves to its group. |
| Aliases and re-exports | Followed with `getAliasedSymbol`: `import { f as g }`, `export { f as g } from`, `export * from`, `import x = N.y`, `export =`. |
| Type-only vs runtime edges | `type_imports`/`type_reexports` for `import type`, `export type` and inline `type` specifiers **and** for any binding whose target has no value meaning (an interface imported with plain `import { Shape }` is elided at runtime, so it is a type edge). An unresolved binding keeps its syntactic class. |
| Literal modules | `declare module "literal-mod"` keys as `<path>:module:literal-mod`, and imports of it resolve into it. |
| Module options, `baseUrl`, `paths`, JSX, `extends` | Honoured as the compiler honours them; a `paths` remap between revisions moves the resolved target (`one/dep.ts:dep` → `two/dep.ts:dep`). |
| Config containment | An `extends` outside the snapshot (absolute path, `../../..`, an uninstalled package) reads nothing and is reported `config_invalid` (`TS5083`/`TS6053`); `plugins` are reported `config_ignored` and never loaded; a missing project reference is `reference_missing`. |
| Config cycles | An `extends` cycle is reported `TS18000`; a project-reference cycle (including through a config not named `tsconfig.json`) terminates, each config followed once. |
| Unresolved polymorphism | A union receiver whose candidates have different parents is `ambiguous` (`union-dispatch:`); an overridable instance or interface member call is `resolved` with `inferred_dynamic` strength; a call through a parameter, `let` or function-typed property is `unresolved` `indirect-call:`; `any` receivers are `untyped-call:`; computed callees `dynamic-call:`; computed `import()` `dynamic-module-specifier`. |
| Shadowing and cycles | Resolution is by binding, not spelling: a parameter named `helper` shadows the module's `helper`; a two-file import cycle resolves both ways. |
| External | A package not in the snapshot is `external` `package:<name>`; a symbol declared only in the compiler's `lib*.d.ts` is `external` `lib:<file>:…`. |
| Closure | A resolved edge whose target no file contributed (unparseable, unrepresentable) becomes `unresolved` `target-not-analysed:<key>`; a group exists only when its first member is a node. |

## Positions are exact original bytes

The compiler reports UTF-16 code-unit offsets. The adapter converts each one against the
exact decoded text into a half-open byte range of the **captured bytes**, plus ECMAScript
lines (LF, CR, CRLF as one break after the LF, U+2028, U+2029) with one-based UTF-8 byte
columns (`locator_basis` `ecmascript_lines.utf8_byte_columns.v1`). Pinned by tests over
CRLF, a two-byte character, a combining mark, an astral emoji, U+2028 and a UTF-8 BOM
(three bytes before the first token, column 17 for `marked` after `export const `). An
offset inside a surrogate pair or past the text refuses the batch. Every edge span in the
fixture decodes as UTF-8, so no span splits a character.

## Snapshot-only and non-executing

- **Input is only admitted, re-hashed snapshot bytes.** Paths and digests come from the
  sealed capture index; bytes come from the content-addressed blob store, opened
  `O_NOFOLLOW`, bounded, and re-hashed against their address (the Python producer's
  `_captured_bytes`). Profile budgets bound files, per-file and total bytes, AST nodes,
  Programs, `extends` depth, references, candidates, reports, heap, wall time and answer
  size.
- **The analyser's `CompilerHost` is in-memory.** It serves the request's texts under a
  virtual `/snapshot` root and the compiler's `lib*.d.ts` by validated basename from the
  one compiler directory. It never touches `ts.sys`; `writeFile` throws; `noEmit` is
  forced and `plugins`, declarations, maps, `composite`, `incremental` and build info are
  stripped. No transformer, language-service plugin or project JavaScript config is ever
  loaded, nothing is installed or fetched, and analysed source is never `require`d.
- **Node is confined.** `--permission` with read access to exactly the analyser file and
  the compiler directory (no write, child process, worker or WASI),
  `--disallow-code-generation-from-strings`, `--no-addons`, `--no-experimental-fetch`,
  `--no-experimental-websocket`, `--max-old-space-size`, an empty environment and a
  timeout. A test runs a probe under the same flags and observes `ERR_ACCESS_DENIED`,
  `EvalError` and no `fetch`; another asserts the analyser source `require`s exactly the
  compiler and `fs`, and nothing else.
- **Execution is observed absent.** A fixture module that writes a sentinel file and
  throws at top level is analysed into candidates; no sentinel appears.
- **The pinned build is the only build.** The adapter refuses an analyser file whose bytes
  are not `ANALYSER_DIGEST`, and a compiler whose `package.json` is not 5.9.3 *or* whose
  `lib/typescript.js` plus `lib*.d.ts` do not hash to `TYPESCRIPT_DIGEST` (a 5.9.3 label
  on other bytes is refused). Both digests are in the profile, so a changed analyser or
  compiler is a changed profile and re-identifies every batch.

## Fail-closed at the adapter boundary

| Condition | Result |
| --- | --- |
| Snapshot absent, uncovered, not `captured_v1`, or not `complete`; `expected_profile_digest` mismatch | `CodeAnalysisBindingError` |
| Relative runtime paths, wrong compiler version or bytes, wrong analyser bytes | `TypeScriptAnalysisRefused`, before anything runs |
| Any snapshot-level budget, a timeout, a non-zero exit, non-JSON, an oversize answer | `TypeScriptAnalysisRefused` |
| An answer for another request digest or compiler version; an unknown kind, relationship, status, strength or outcome; a span past its file or inside a surrogate pair; a candidate in a file that was not analysed; file outcomes that are not one per source; Programs that do not partition the sources; a lib name outside `lib*.d.ts`; a key, duplicate or dangling edge the candidate contract rejects | `TypeScriptAnalysisRefused` (contract errors are re-raised as refusals) — 14 tamper cases |
| Per-file trouble: oversize, undecodable, syntax-invalid, over the AST budget, a path the contract cannot key | one bounded `TypeScriptFileReport`; that file contributes nothing, every other file's facts survive |
| Configuration trouble; declarations without a representable identity | explicit coverage reports (`config_invalid`, `config_empty`, `config_ignored`, `config_unsupported`, `reference_missing`, `declaration_unsupported`) beside the file outcomes |
| No admitted source, or no node | `verified is None` — an answer, not a failure |

## Packaging

The runtime wheel carries `storage/code_analysis_typescript.cjs` (hatchling includes it as
package data) and, via `hatch_build.py`, `storage/typescript_compiler/` — 104 files.
`TypeScriptRuntime.typescript_root` defaults to that directory; a source checkout names its
`npm ci` install instead, and both are verified against the same pins. Node is the one
thing the caller still supplies. A wheel build now needs `npm ci` first and refuses
otherwise: `core-acceptance.yml` already ran it before the package gate, and
`phase2-platform.yml` now runs it before its two wheel-building steps.

## Fixture analysis, for the record

| Fact | Value |
| --- | --- |
| Profile digest | `sha256:c76a5c802ad78bcbb96303c9fe49d8b3afecc40d09602a7b0746f48425e1e9b0` |
| Analyser digest | `sha256:fd3a6ba5483cedbae50a37b53f87c713ac2c83997d4bd0cc7f1021b1320c7eae` |
| Compiler digest | `sha256:6ca61284adca4c30165a5df9e3c92ad5e83b506e13b5fa85b689895af6fef279` |
| Batch digest (`snap-ts-1`) | `sha256:af57ef1ff0ef49384672a0455d2d6e2bc5145f465b0c6b5522f56656f1e42040` |
| Files | 19 indexed, 2 excluded (`README.md`, `node_modules/…`), 3 194 bytes read |
| Programs | 2 (`tsconfig.json`: 12 files; `packages/shared/tsconfig.json`: 1) |
| Nodes | 50 — 11 module, 14 function, 6 variable, 5 method, 2 class, 2 interface, 2 property, 2 enum member, 1 enum, 1 namespace, 1 ambient module, 2 merge groups, 1 overload group |
| Edges | 97 — 36 defines, 20 calls, 14 imports, 7 groups, 6 references, 3 constructs, 3 type references, 2 type imports, 2 re-exports, 2 dynamic imports, 1 extends, 1 renders |
| Resolution | 87 resolved, 6 unresolved, 3 external, 1 ambiguous; 52 `resolved_static`, 37 `syntactic`, 8 `inferred_dynamic` |
| File outcomes | 11 analysed, 1 each `syntax_invalid`, `decode_failed`, `path_unsupported`, `config_invalid`, `config_ignored`, `reference_missing` |
| Determinism | two runs give an identical `analysis_manifest` |

## Real source, and the limit that binds

| Input | Bytes | Time | Candidates | Result |
| --- | --- | --- | --- | --- |
| This repository's four real TS files (`generated/typescript/application/v1/index.ts`, the two contract consumers, the analyser's own `.cts`) | 688 711 | 0.64 s | 7 638 (2 518 nodes, 5 120 edges; 4 290 resolved) | all `analysed`, deterministic |
| `typescript/lib/typescript.d.ts` alone | 588 085 | 0.39 s | > 10 000 | `TypeScriptAnalysisRefused` at the candidate ceiling |

As for Python, **the binding limit on real code is the contract's `MAX_CANDIDATES`**, not
`max_files`: one large declaration file exhausts it. Batching or a raised ceiling is the
governed-import work's decision (QP-04), not a producer change.

## Verification

Every command was run from this worktree with the environment above.

| Command | Outcome |
| --- | --- |
| `pytest …/test_code_analysis_typescript.py …/test_code_analysis_candidates.py …/test_code_analysis_python.py -q` | **77 passed** (32 + 9 + 36) |
| `pytest tests/test_core_acceptance_workflow.py tests/package_qualification tests/test_import_install_alignment.py tests/test_package_boundaries.py -q` | **1300 passed** |
| `scripts/generate-code-analysis-typescript.py --check` (twice, after a regenerate) | current both times — the compile is deterministic, and it is the strict TypeScript check of the canonical source |
| `PYTHON=… scripts/check-package-builds.sh` | **passed**; `packaged analyser OK on TypeScript 5.9.3: 2 node(s)` from the isolated runtime wheel venv |
| `ruff check src packages baseline tests scripts` | All checks passed |
| `mypy --strict packages/omnivia-core-runtime/src/omnivia_core_runtime`; `mypy --strict hatch_build.py scripts/generate-code-analysis-typescript.py` | no issues (195 and 2 source files) |
| `check-package-boundaries.py`, `check-raise-discipline.py`, `check-migration-allocations.py`, `npm run check:application-contracts` | all passed |
| `git diff --check` | clean |
| full repository suite (`pytest -q` from the root) | see *Broad suite* |

### Broad suite

```
pytest -q   (repository root, testpaths = the whole suite)
6 failed, 39296 passed, 56 skipped, 7 warnings in 2792.64s (0:46:32)
```

All six failures are pre-existing. Each was re-run on a pristine `git archive 27fbe9c3`
extraction (same interpreter, overlay and `node_modules`) and **fails identically there**
(`6 failed`); they are the same six the W10 record reproduced on `6620bcae`:

| Failure | Cause, as recorded for W10 |
| --- | --- |
| `test_mcp_installed_verification.py` (3 cases), `test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | the qualification child imports the main checkout's editable install in a worktree, so the server does not pass its startup check |
| `test_at09_selected_source_end_to_end.py::test_selected_source_reaches_core_exact_and_is_left_unchanged` | its Swift XCTest: `("pending") is not equal to ("admitted")` |

None touches a file this slice changed.

## Limitations that belong to later work, not W11

1. **Governed import (QP-04).** Candidates are values; nothing imports or activates them,
   and the single-batch ceiling above is that work's first decision.
2. **Node is caller-supplied and not pinned.** The compiler and analyser are pinned by
   digest; the Node runtime is not packaged, and its version is not part of the profile.
   Node 22's permission model also does not gate the network: the confinement is that the
   analyser `require`s no networking module (asserted) and runs no analysed code. A
   packaged, pinned Node with network denial belongs to the runtime-distribution work.
3. **The Standard candidate's SBOM does not list the vendored compiler.** Its licence and
   notice ship beside it in the wheel, but `build-standard-candidate.py` inventories only
   dist-info licence members. Recording TypeScript (Apache-2.0) as a component is release
   work.
4. **No value flow.** `module.exports =` / `exports.x =` in CommonJS are not export edges;
   JSDoc types in JS are not type references; a call through a value is `indirect-call`.
5. **Unrepresentable names** (computed keys, string-literal members outside the identifier
   pattern, identifiers with combining marks) are counted as `declaration_unsupported`,
   not keyed.
6. **The answer is read whole** (`subprocess.run`) and its size checked after the fact
   against `max_response_bytes`; streaming with an early kill would bound memory tighter.

## ADR / PM

- **PM update (Codex):** reconcile **W11** in `WORK-STATUS.json`. This slice added a
  pointer to this record and left the verdict at `NOT_RUN`, as the W10 slice did, because
  that ledger's status vocabulary is scoped to the P1 slice.
- **No ADR needed:** the module is internal and adds no public surface or wire contract;
  both candidate-contract extensions are additive. Shipping a third-party compiler inside
  the runtime wheel is a packaging decision Codex may want to record.
