# Core Settings v2 integration record (onto the repaired P1-P10 head)

- Date: 2026-10-18
- Branch: `codex/core-tech-settings-integration`
- Baseline: `a7454199` ("docs(core): record the static gate repair evidence"), the P1-P10 head with
  the Waves 1-4, semantic/code, update-trust, migration 0069-0073 and static-gate-repair work
  already integrated.
- Source: the Settings v2 packet on `codex/core-settings-screen-20260921`, exactly the ten commits
  `git log b0dace0b..codex/core-settings-screen-20260921` lists above their merge base `b0dace0b`,
  which `a7454199` already contains.
- Scope: the native Core Settings window's v2 six-pane information architecture, and
  `settings.list` / `settings.set` as Application Contract v1 operations with CLI commands and
  server-owned mutation entries. The migration catalogue, the update owner, the semantic/code lane
  and the MCP exposure manifest were not touched.

This record uses the verification levels of `OMNIVIA-CORE-TECH-SPECS-IMPLEMENTATION-PLAN-001` §10.
It is **not** release qualification and it moves no A-scenario, work-ID or acceptance-case status.
It makes no `L5`/`L6` claim: nothing here was built as a signed or notarised package, installed,
activated or run on a packaged host. The strongest evidence is `L3 component`, plus one
real-process observation (§4.3) that runs from the checkout and is not a packaged install.

## 1. The commits

The ten source commits were cherry-picked with `-x`, oldest to newest. "Textual conflicts" counts
files git could not merge. The semantic work in §2 is larger than that column, because git put
the packet's two operations in its alphabetical slot in four transcriptions of the catalogue
without a conflict hunk (§2.2).

| # | Source | Integrated | Subject | Textual conflicts |
|---|---|---|---|---|
| 1 | `c6c64b16` | `c9ec1229` | the three glyphs Core Settings v2 adds | none |
| 2 | `a6d6b4e6` | `2a667bed` | the v2 six-pane information architecture | 1 file: `SettingsWindowController.swift`, one hunk (§2.1) |
| 3 | `e9661331` | `8f2e5ce8` | the Sources overview | none |
| 4 | `98f5ca1a` | `3f9a124f` | v2 grouping, scope labels and copy for the four kept panes | none |
| 5 | `8b470bf5` | `35e620b4` | the route is remembered, and a v1 one is migrated | none |
| 6 | `1b41d9f7` | `c9a972d7` | Escape, innermost first | none |
| 7 | `106c6a0b` | `58be4b31` | every v2 detail is reachable and says what it waits for | none |
| 8 | `b0c6a658` | `97c1ea99` | `settings.list` and `settings.set` (Application Contract v1) | 14 files (§2.2, §2.4, §2.5) |
| 9 | `df151a2c` | `4512cf19` | `settings list` and `settings set` (CLI) | 3 files: a docstring count and two test counts |
| 10 | `51e98684` | `b33eda29` | `settings.set` in the server-owned mutation maps | 1 file, two hunks: `mutation.py` (ordering only) |

Two commits follow the ten, then this record:

- `470f5959` fix(runtime): serve settings.list and settings.set as typed refusals
- `cf248b71` chore(core): carry the 88-operation catalogue through the ledgers and pins

The integrated diff of the ten against the baseline is 25 files, +1808 -113. The packet's own diff
against its merge base is the same 25 files, +1803 -107. Per file, 20 of the 25 have **identical**
insertion and deletion counts on both sides, and the five that differ are accounted for:

| File | Packet | Integrated | Why it differs |
|---|---|---|---|
| `README.md` | +3 -3 | +10 -9 | the baseline's operation list is three times longer, so reflowing its tail moves more lines |
| `SettingsWindowController.swift` | +568 -75 | +570 -77 | two comment headings renamed in the conflict resolution (§2.1) |
| `scripts/check-application-contracts.py` | +16 -2 | +10 -1 | the packet reformatted `FROZEN_PRECONDITION_OPERATIONS`; the baseline's larger set stands untouched |
| `src/omnivia_core/contracts/v1/__init__.py` | +14 -2 | +12 -0 | the packet's malformed edit is repaired (§2.5) |
| `tests/contracts/test_operation_catalogue.py` | +9 -9 | +13 -10 | two tests renamed to their true counts, and a comment recording why the new operations are last |

Equal counts across a 683-commit gap (the baseline side changed 681 files since the merge base) are
the evidence that nothing stale was imported and nothing newer was reverted. Every row above was
then read in the merged file rather than inferred.

## 2. Conflicts and semantic adaptations

### 2.1 Native Settings window (`SettingsWindowController.swift`)

One conflict, in commit 2 (`a6d6b4e6`). The baseline added two model properties (`languages`,
`processing`) directly above `enum Pane`, and the packet replaced the comment above that enum. Both
are kept: the two properties stay, the packet's *Routes* comment and six-pane enum replace the
baseline's now-false "seven items" comment, and the two properties' headings now say "detail"
instead of "pane".

The structural change under the conflict is that v1's seven flat panes become six (`general`,
`sources`, `processing`, `storage`, `access`, `maintenance`), with Capture and Document processing
demoted to details. The baseline's real Waves 1-4 controls survive that demotion because the packet
routes the details to the **existing** render functions:

| Baseline pane | Where it lives in v2 | Mechanism |
|---|---|---|
| Document processing (recognition mode, embedded-image OCR, installed languages, language selection, repair) | Processing, then Document processing | `renderDetail` maps `(.processing, "docproc")` to the unchanged `renderDocumentProcessing` |
| Capture (defaults, limits) | Sources, then Capture defaults | `(.sources, "capture-defaults")` maps to the unchanged `renderCapture` |
| General, Data, Access, Maintenance | General, Data & storage, Access, Maintenance | regrouped under v2 headings and scope labels by the packet's commit 4; none of the 77 lines the merge removes from the baseline's file is a control |

The two models that back Document processing (`OcrLanguageModel`, `DocumentProcessingModel`) are
still loaded from the same two call sites at window open (`loadOcrInventory()`,
`loadProcessingSettings()`), so reaching the detail does not depend on having visited it first.

Two things the packet leaves behind are reported rather than changed:

- v1's `renderProcessing` is now unreferenced, in the packet as authored and in the merge. It held
  seven placeholder rows and no control, so no behaviour is lost; deleting it is the packet
  author's call.
- The Files and folders manager (Waves 1-4 `FolderManagementWindowController`) is opened from the
  status-menu item "Files and folders…", as before. It is not linked from the v2 Sources overview,
  which still says Core does not advertise a source manager (§6.1).

### 2.2 The catalogue is frozen in insertion order, not alphabetical order

The packet was written against a 28-operation catalogue sorted by code point, and it put
`settings.list` and `settings.set` between `record.supersede` and `workflow.control`. The baseline
catalogue is 86 operations: the original 28 in code-point order, then every later operation
appended in amendment order (`decision.*`, `continuity.*`, ... `document.original.relink`), which the
checker's own comment calls "the frozen insertion order". An alphabetical slot would have
renumbered 65 operations and changed the wire-form digest of the first 69 entries that
`test_dev_req_081_knowledge_sharing.py::test_appending_the_family_changed_no_earlier_operation_contract`
pins. Computed against the baseline, the appended placement matches that pin and the packet's
placement does not.

So the two operations are **appended** (positions 87 and 88) everywhere the catalogue is
transcribed: the catalogue annotation in `operations.schema.json`, its independent fixture
`operation-catalogue-v1.json`, `FROZEN_OPERATIONS` in the contract checker, the registry's
definitions, and the wire-adapter corpus (the packet's four cases appended verbatim after the
baseline's 227). Git placed four of these in the alphabetical slot without a conflict hunk (the
catalogue annotation, its fixture, the two `FROZEN_OPERATIONS` entries and one registry definition),
which is why they were found by reading the merged lists and not by the conflict markers.

### 2.3 The service refused to start: `settings.*` had no handler

This is the one adaptation that is more than bookkeeping. Merged exactly as the packet wrote it, the
real service exits at transport start:

```
TRANSPORT_START_ERROR: ValueError: application registry is incomplete:
missing=['settings.list', 'settings.set'] unexpected=[]
```

`ApplicationOperationRegistry.assert_complete` and `ProductionApplicationSurface.__post_init__`
require one registered handler per catalogue operation before any transport binds. The packet
publishes the two operations, their CLI commands and their server-owned mutation entries, and
registers no handler. Observed on the merged tree before the repair: the CLI, client and MCP suites
had 10 failures and 84 errors (the four `test_lifecycle.py` tests and the MCP stdio and
installed-setup fixtures all start a real service), and five live-Core Swift tests failed or crashed
the `xctest` process (`start_failed`, `stop_not_running`, `Index out of range`). The packet's commit
messages report only `tests/contracts` (9944 passed, 6 failing for want of `npm ci`) and the CLI
package suite (660 passed, 3 lifecycle tests failing "identically on a clean tree", attributed to the
environment). A stray PATH service produces the same `endpoint_unreachable` on a clean tree (§5.1),
which is how the two causes can be taken for one.

The repair (`handlers/settings.py`, `build_settings_application_dispatcher`) is a new `settings`
authority family, the fifteenth, composed beside `task_context`. It takes the declarations the
packet already made and nothing else:

- the session holds exactly `settings.list` and `settings.set`, under the **workspace contributor**
  role, the purposes the CLI commands declare (`workspace_inspection` for the read,
  `workspace_administration` for the change, as `MUTATION_PURPOSES` already says), and the scopes
  and capabilities the two catalogue entries require, which `_contributor_family_session` derives
  from the catalogue;
- no existing session gained anything: the new operations are in no other family's operation set,
  `OPERATION_PURPOSES` (which feeds the read-only local-owner session) is unchanged, and the family
  count in `ProductionApplicationSurface` moves from fourteen to fifteen *exactly*;
- the handlers **recognise and refuse**, following `analysis.start`: a well-formed request gets
  `dependency_unavailable` (retry class from the frozen classification, `retryable_after_delay`), a
  malformed one `invalid_request`, and neither reads or writes storage, issues a grant, claims an
  idempotency key, allocates an identifier or writes an audit row. The new tests trace every SQL
  statement on the service's connection and require none.

Nothing was relaxed to get here: the completeness guard, the family-count guard and the mutation-map
exactness check are as strict as before. What was **not** decided is what serving the operations
means. A handler that serves them has to settle four things the packet leaves open, recorded in the
module so they are not rediscovered: which keys a generic writer may touch (a typed owner such as
`document.settings.update` validates its own value, and a generic writer over the same table would
bypass that); what to do with a stored key that is not an `OpenCode` (the store admits hyphens and
digit-led segments, `SettingRecord.key` cannot carry them); the value bound (the contract counts
characters in `value_json`, the stored column counts bytes); and whether `value_json` must be JSON
text (the contract states only a length, the store takes a decoded value and refuses what does not
encode, and a NUL).

A fresh read-only review was given the diff and not these conclusions. It found no widening of any
existing session, no storage access on about twenty-five payloads in both dict and decoded-wire
form, and the surface invariants intact. Its two Low findings are documented in the module (the
authorization seam checks purpose per session, not per operation; `value_json` text no store could
hold is admitted), and its three test gaps are closed (the SQL trace on every call, the purposes read
from the CLI table instead of a literal, and a purpose the family does not hold).

### 2.4 Counts, ledgers and partitions that follow the catalogue

None of these is a behaviour change; each is a number or a list that moves with 86 becoming 88. The
packet moved the counts it could see from its 28-operation base; the rest it could not. The pins in
`packages/omnivia-core-runtime/tests` for the mutation maps and the corpus size were found only by
the single-invocation full suite, which is why the focused runs alone were not enough.

| Where | Change | Commit |
|---|---|---|
| `README.md`; `tests/contracts/*` (6 files) | counts 86 to 88; workspace-scoped 84 to 86; corpus 227 to 231; mutations 49 to 50; schema files 21 to 22; corpus SHA-256 re-pinned to the merged bytes in `test_adapter_conformance.py` | the cherry-pick of `b0c6a658`, values rebased to the baseline |
| `packages/omnivia-core-cli` tests (2); `surface.py` docstring | application commands 86 to 88 | the cherry-pick of `df151a2c` |
| `packages/omnivia-core-runtime` tests (3) | catalogue count 86 to 88; the two surface composers take the new family | the `settings` family commit |
| `tests/fixtures/service_conformance/operation-traceability-v1.json` | two operation blocks, 86 to 88 operations, 227 to 231 corpus cases, two MCP omissions (`settings.list` as `read_not_allow_listed`, `settings.set` as `mutation`) | the pins commit |
| `tests/fixtures/service_conformance/architecture-gate-traceability-v1.json` | the three whole-catalogue gates 86 to 88, the two every-mutation gates 49 to 50 | the pins commit |
| `tests/service_conformance/*` (3 files) | the matching pins | the pins commit |
| `test_v06_5_s0_mutation_foundation.py` | the exact-set pins of `MUTATION_PURPOSES` (49 to 50 names) and `MUTATION_ROLES`, which the cherry-pick of `51e98684` extends | the pins commit |
| `test_v06_5_s5_integrated_registry.py` | corpus size 227 to 231 and 681 to 693 case-adapter pairs; corpus digest | the pins commit |
| `scripts/run-mcp-real-host-qualification.py`; `tests/package_qualification/test_mcp_real_host_qualification.py` | the probe list of catalogue operations outside the MCP manifest 51 to 53; excluded-name counts 90/69 to 92/71 | the pins commit |
| `docs/distribution/mcp-host-interoperability.md`; `packages/omnivia-core-mcp` (manifest docstring, one test message) | the same excluded-name counts; "eighty-six" to "eighty-eight" in prose | the pins commit |

The MCP exposure manifest is **unchanged**: the packet's decision is that administrative
configuration stays unavailable to MCP clients, and the manifest is an allow-list. The two
operations therefore join the unexposed partition, which is what the rows above record.

### 2.5 The packet's edit to the contracts barrel

`src/omnivia_core/contracts/v1/__init__.py` in commit 8 inserted the six `Setting*` names into the
middle of the constants block and glued them to neighbouring lines (`SOURCE_KIND_PATTERN,
SettingRecord,`), out of the import block's sort order. It is valid Python, so nothing failed on it,
but it is not what the isort-ordered import block or the code-point-ordered `__all__` says. The six
names were re-inserted where each list sorts them.

### 2.6 What did not change

- **Migrations.** No migration file, `allocations.json` entry or head pin changed
  (`0069_document_intake` to `0073_semantic_vector_projection` stand), and
  `scripts/check-migration-allocations.py` passes. The packet's contract is consistent with the
  stored table: the baseline's `0070_app_settings.sql` is byte-identical to the
  `0043_app_settings.sql` the packet was written against (renumbered since), and its key grammar and
  16384-byte value bound are the ones the contract's description assumes. `OpenCode` keys are a
  strict subset of what the store accepts, as the packet says.
- **Update, semantic/code, Waves 1-4 runtime, static-gate repair.** No module specific to those
  lanes is in the diff. The shared files touched are the composition roots `service/application.py`
  and `service/main.py`, the mutation maps in `service/mutation.py`, and the registries and ledgers
  in §2.4.
- **Authority.** No existing session, role, purpose, scope or capability changed.

## 3. Generated Application Contract artifacts

Every generated artifact is reproducible from its canonical generator and schemas on the integrated
tree. Each command below is also a preflight step or part of one (§4.1); the application generator's
own `--check` is delegated to by `check-application-contracts.py`:

| Command | Result |
|---|---|
| `python scripts/generate-application-contracts.py --check` | `Application contract artifacts are up to date.` |
| `python scripts/check-application-contracts.py` | `Application contract conformance check passed.` |
| `python scripts/generate-host-contract.py --check` | `Host Contract canonical resources and generated artifacts are up to date.` |
| `python scripts/generate-runtime-contract.py --check` | `30 trusted-runtime fixture(s) match the generator.` |
| `python scripts/generate-update-contract.py --check` | `update contract corpus check passed (57 files).` |
| `python scripts/generate-mcp-exposure-schemas.py --check` | `... is up to date (71 advertised schemas).` |
| `npm run check:application-contracts` | `TypeScript strict compile check passed.` |

`generated.py` and `index.ts` were **regenerated, not merged**: both conflicted, so the baseline's
versions were restored and the canonical generator re-run over the merged schemas. The lines the
regeneration adds (336 in Python, 151 in TypeScript) are, as multisets, exactly the lines the
packet's own generated files add, and it deletes none. The `settings` schema sits last in the
generator's and the checker's `SOURCE_SCHEMAS` and in the registry's `x-omnivia-schema-sources`,
after `decision` and `engineering`, so its definitions emit last.

## 4. Verification

All commands ran from this worktree on macOS 27.0.0 (Darwin, arm64) against a worktree-local `.venv`
provisioned per the `scripts/preflight` header: CPython 3.11.15, ruff 0.16.1, mypy 2.3.0, pytest
9.1.1. `.venv/bin` was first on `PATH` (§5.1) and `OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT` was read
from repository configuration with `gh`, as the script reads it.

### 4.1 `./scripts/preflight` on the code commit

`PATH="$PWD/.venv/bin:$PATH" ./scripts/preflight` on `cf248b71`, the last commit before this record
(which adds only this file): one invocation, exit 0, "Core preflight passed", 47 min 53 s. One test
is deselected by node id through `PYTEST_ADDOPTS` (`test_at09_selected_source_end_to_end.py`, which
fails identically at the baseline, §5.5). Nothing else about the script, its steps or their order
differs from a plain run.

| Level | Step | Result |
|---|---|---|
| L1 | package boundaries; their 54 tests | passed; 54 passed |
| L1 | migration allocations | `Migration allocation check passed.` |
| L1 | five distributions built and install-checked; the MCP wheel's tool document against the source tree's; packaged resources | `All five distributions built and installed cleanly.`; byte-identical (8211 lines); 22 schemas and 20 fixtures read |
| L1 | application contracts, Host Contract, trusted-runtime fixtures, update fixtures, MCP exposure schemas (§3) | all passed |
| L3 | `tests/contracts` | 11547 passed, 19 skipped |
| L1 | generated TypeScript contracts | `TypeScript strict compile check passed.` |
| L1 | compatibility facade routes; root resolver and installed-root smoke | passed |
| L3 | canonical migration and compatibility tests | 1686 passed, 2 skipped |
| L3 | Phase 0 baseline | 749 passed |
| L3 | **full repository suite, one invocation** (`services tests` and the runtime, CLI, client and MCP suites) | **38060 passed, 56 skipped, 1 deselected**, 42 min 0 s |
| L3 | benchmark tests | 23 passed |
| L1 | Ruff 0.16.1 over the exact CI target list | `All checks passed!` |
| L1 | strict mypy 2.3.0 over the exact CI target list | `Success: no issues found in 422 source files` (421 at the static-gate record, plus `handlers/settings.py`) |
| L3 | macOS companion `swift build` and `swift test` | 1011 tests, 2 skipped, 0 failures, 230 s |

An earlier run of the same script on the code commit before four pins were corrected reported
`5 failed, 38056 passed, 56 skipped` in 46 min 31 s: the four pins in §2.4 (the mutation maps'
exact-set tests and the integrated-registry corpus size) and the baseline failure in §5.5. It is
the reason for those corrections and is superseded by the run above.

### 4.2 Focused runs of the new code

| Check | Result |
|---|---|
| `test_settings_operations_refused.py`: composition, the family's exact authority, no other session reaching either operation, the purposes read from the CLI table, refusal versus invalid in-process and as a decoded wire request, a purpose the family does not hold, and a SQL trace asserting no statement runs on the service connection during any call | 33 passed |
| six mutations of `handlers/settings.py` (value bound off by one; key grammar unchecked; undeclared members tolerated; a read that succeeds; a handler that reads storage; prefix unchecked) | each caught by that module; the file restored byte for byte |
| `packages/omnivia-core-cli/tests/test_lifecycle.py` (the four existing tests and the new live `settings` test) | 5 passed |
| two mutation-map pins, two integrated-registry pins | passed after correction (37 passed with their modules) |

### 4.3 The real CLI against a real local service

Not a packaged install: a bootstrapped installation under `/tmp`, started through `omnivia service
start`, then the two commands the packet added, through the CLI's real process boundary
(`omnivia settings ...`, local IPC, the production surface):

| Invocation | Exit | Answer |
|---|---|---|
| `settings list` (no input; and `{"prefix": "document"}`) | 7 | `dependency_unavailable`, `retryable_after_delay`: "the settings read is understood, but this build does not serve stored application settings; nothing was read" |
| `settings list` with `{"prefix": "Bad-Key"}` | 2 | `invalid_request`, `non_retryable` |
| `settings set` with a well-formed key and value and an idempotency key | 7 | `dependency_unavailable`, `retryable_after_delay`: "... nothing was changed" |
| `settings set` with `{"key": "Bad-Key", "value_json": "1"}` | 2 | `invalid_request`, `non_retryable` |

The purposes the packet declared in `surface.py` and the grant the new family holds are therefore
compatible end to end (a purpose the session did not hold would have been `invalid_purpose`, not
these answers). The same invocations are held by the new `test_lifecycle.py` test.

### 4.4 Before and after the repair

| Suite | Merged as the packet wrote it | After §2.3 and §2.4 |
|---|---|---|
| `tests/contracts` | 1 failed, 11546 passed | 11547 passed, 19 skipped |
| `omnivia-core-cli`, `-client`, `-mcp` tests | 10 failed, 84 errors, 3145 passed | 3239 passed, 8 skipped |
| `services`, `tests` apart from contracts | 6 failed, 7108 passed | passed inside the single invocation of §4.1 |
| macOS companion, live-Core suites | 4 failed and the `xctest` process crashed | 1011 tests, 0 failures |

These "before" figures are from a four-group discovery pass run in parallel for speed. Only the
single-invocation run in §4.1 is evidence of the whole.

### 4.5 Swift

`swift build` and `swift test` on the merged tree: twice before §2.3, both failing (the service could
not start); after it, one run alongside the Python runtime suite that stalled and was stopped (§5.3),
one isolated full run that ended early inside `CaptureCoreBoundaryTests` (§5.3), and two further full
runs, one isolated and one as the preflight's own step, that executed 1011 tests, 2 skipped, 0
failures. The baseline executes 1000 tests, 2 skipped, 0 failures; the 11 additional tests are the
packet's `RouteTests` (route parsing, aliasing, v1 migration and idempotence). What holds the
window's rendering is the build and those tests; see §6.1, item 5.

## 5. Environment findings

None of these changed production code or a test to accommodate it.

### 5.1 The stray PATH service

`~/.local/bin/omnivia-core-service` is still on this host and still wins a PATH-first lookup. With
it first, the live-Core Swift suites and any PATH-first CLI spawn start the wrong service. Every
result in §4 was taken with this worktree's `.venv/bin` first on `PATH`, which is the known-good
recipe for `scripts/preflight`. Putting `.venv/bin` first did **not** change the Swift result until
§2.3 was repaired, which is how the two causes were told apart: the same five live-Core tests failed
with the stray service first and with the right one first, and passed once the registry was
complete.

### 5.2 The data volume filled during verification

Mid-run the data volume fell to 366 MiB free: the shell tool and the file-write tool failed with
`ENOSPC`, and one pytest run reported a spurious error. `$TMPDIR/pytest-of-<user>` held 86 GB, because four sessions were running
full suites at once and pytest keeps every test's temp directory. Four basetemps dated Oct 16
(`pytest-58`, `-126`, `-132`, `-134`, about 27 GB) had no live owner and were removed; the running
pytest processes' open files were checked first and nothing a live run held was touched. The one
pytest run caught in that window (three runtime modules) reported one error that did not recur when
its modules were re-run with space free. Every later run of this lane used
`-o tmp_path_retention_policy=failed`, which keeps only failed tests' directories and which
`scripts/preflight` inherits.

An over-broad `pkill` pattern of mine, used to stop my own earlier preflight, also matched a long
`pytest services tests ...` process that belonged to the `tech-gate-repair` worktree's interpreter,
already orphaned (its parent was gone) and about an hour old. It was still alive ten seconds later
and is gone now; whether it had finished or was cut short by the SIGTERM could not be established,
so that run should be treated as unverified. Later stops used exact process ids.

### 5.3 Swift baseline, and a stalled concurrent run

- The baseline's Swift suite, run on an extract of the whole of `a7454199` with this worktree's
  `.venv` first on `PATH` and `PYTHONPATH` shadowing the extract's sources, executes 1000 tests, 2
  skipped, 0 failures (231 s). The merged tree executes 1011, 2 skipped, 0 failures (229 s): the 11
  added tests are the packet's `RouteTests`. (A first baseline run on an extract of the Swift
  package alone reported 1 failure and 16 skips; that is an artifact of the missing `packages/`
  tree and `.venv`, not a baseline result.)
- `test_at09_selected_source_end_to_end.py` runs `swift test --filter SelectedSourceIngestionTests/...`
  on the same package, and SwiftPM holds the package's `.build` lock for a whole run. A full
  `swift test` started alongside the Python runtime suite stalled for six minutes in the teardown of
  the first `CaptureCoreBoundaryTests` test (`xctest` at 0% CPU in `Process.waitUntilExit`) while the
  nested run waited for the lock. It was stopped, and the same suite passes in isolation (2 tests,
  9 s). One later isolated full run on the merged tree also ended early inside that suite (the
  second `CaptureCoreBoundaryTests` test started, then the `xctest` process exited with status 1 and
  no failure report); the run after it, and a run of the three suites around it, passed in full. Both
  occurrences are in the one live-Core suite that starts its own service, neither reproduced, and
  the cause was not isolated; the baseline was run once, green. The Swift results in §4 are from
  runs with no Python suite in flight.

### 5.4 Whitespace against `origin/main`

`git diff --check a7454199..HEAD` is clean. The CI form, `git diff --check origin/main...HEAD`, exits
non-zero with findings that exist at `a7454199` and in no commit of this lane: lines 1 and 3 of the
deliberate mixed-line-ending fixture
`packages/omnivia-core-runtime/tests/phase3/runtime/fixtures/at09/utf8-bom-mixed-eol.txt` (its bytes
are the test's subject), and a blank line at the end of
`packages/omnivia-core-runtime/tests/phase3/runtime/test_refresh_outcome_gates.py` (line 365). They
will trip the pull-request whitespace step for the whole branch until a lane that owns those files
deals with them.

### 5.5 `test_at09_selected_source_end_to_end.py` fails at the baseline

`test_selected_source_reaches_core_exact_and_is_left_unchanged` fails on this branch and on an
untouched extract of `a7454199` with the same message: the Swift half's delivery ends `pending`
(`XCTAssertEqual failed: ("pending") is not equal to ("admitted") - failure: none`, then
`XCTUnwrap` of the capture id). The extract was run with `PYTHONPATH` shadowing so that its own
`omnivia_core_runtime` and `omnivia_core_cli` were the modules under test (their `__file__` paths
were printed). The cause is visible in the code: the companion's `CLICaptureDelivery` passes
`--require-running`, so it attaches to a service and never starts one (the AT-11 pre-send gate),
while this test, written earlier (`0e7b144c`), bootstraps an installation and never starts the
service. It is unrelated to Settings v2 and was not changed. Because it fails at the baseline, a
clean `./scripts/preflight` is not obtainable on this branch while it stands; the final run in
§4.1 deselects exactly that node id through `PYTEST_ADDOPTS` and changes nothing else.

## 6. Remaining gaps

### 6.1 Settings v2

1. **`settings.list` / `settings.set` are published, composed and refused (§2.3).** Nothing in the
   window or the CLI can read or write a persisted setting through them. Serving them first needs the
   four decisions recorded in `service/handlers/settings.py`, and then a handler through
   `execute_mutation` for the change, since the catalogue declares it audited and
   idempotency-keyed. The native window does not call either operation.
2. **Eight v2 details still say they wait for an owner**: Models, Search and embeddings, Code
   analysis, Local Decisions, Background processing, Processing activity, Source and Repository. The
   packet's copy says Core "advertises none of" what several need. Since the packet was written Core
   gained the Decision Runtime operations (`decision.*`, including `decision.settings.get` and
   `decision.settings.update`), an internal hybrid-retrieval seam with no public operation and no
   embedder, and the semantic projection store (migration 0073). None is bound to the window, and the
   copy is unchanged here: what the window tells a person about Core is a product decision, not an
   integration one.
3. **The Sources overview** says Core does not advertise a source manager and disables Add folder,
   Add files and Add repository, while the Waves 1-4 Files and folders manager ships and is reachable
   from the status-menu item. The two are not linked.
4. **Dead code in the packet:** v1's `renderProcessing` is unreferenced (§2.1).
5. **UI verification is build plus unit.** `RouteTests` covers route parsing, aliasing and migration;
   the rest of the suite is the 1000 tests the baseline already carried. No test renders a v2 pane or
   detail or drives Back and Escape, and no one walked the window.
6. **No real MCP host has been run against the current harness.** The two retained records
   (`mcp-real-host-claude-code-2.1.289.json`, `mcp-real-host-codex-cli-0.146.0.json`) date from
   2026-10-04, against an older manifest (eighteen authoring tools, where the current manifest exposes
   thirty-five) and an older harness (their pinned `harness_sha256` is not the current script's). They
   were not touched and say nothing about `settings.*`. The harness now probes 92 restricted and 71
   authoring excluded names, two more each than before this lane.
7. **Documentation drift that predates the packet:** `packages/omnivia-core-cli/README.md` describes
   "the 61 application commands" in a table that stops at `trigger health`; the baseline has 86
   commands and `settings list` / `settings set` make 88.

### 6.2 P1-P10, carried forward and not closed by this lane

- **Semantic and code intelligence (P1).** In the lane's own `WORK-STATUS.json`: P17 real embedding
  model and backend qualification, P18 semantic input from stored content, P19 Python/TypeScript
  code-analysis producers and code-graph import, P20 any transport, client, native, MCP, CLI or HTTP
  exposure of hybrid search, P21 scale, latency and memory, and P22 packaging and release are all
  `NOT_RUN`; the query side has no embedder, so combined mode is `partial` with reason
  `query_not_embedded`. P23 (repository preflight and full suite) is `FAIL` in that ledger; §4.1 is a
  complete single-invocation preflight on an integrated head that contains it, and that ledger is not
  edited here.
- **Update owner (P6).** No signing authority is bound (`PACKAGED_TRUSTED_KEYS = ()`, so production
  channel verification refuses every envelope); nothing builds `omnivia-core-standard-candidate.zip`;
  there is no durable last-accepted sequence or version floor; `OUTCOMES` still lists
  `restart_required`, which no writer produces.
- **Document intake (Waves 1-4).** The `L5`/`L6` position of the vertical is exactly where the
  Waves 1-4, update-trust and static-gate records left it.
- **Static-gate repair.** The DOCX header/footer refusal it fixed has an `L3` test and no `L4`
  observation through a live Runtime.

### 6.3 Baseline conditions that remain

- `test_at09_selected_source_end_to_end.py` fails at `a7454199` (§5.5). Until its owner makes the
  harness start the service, or the delivery gate and the test agree some other way, a plain
  `./scripts/preflight` stops at the full-suite step on this branch.
- `git diff --check origin/main...HEAD` reports three findings that exist at the baseline (§5.4).
- `CaptureCoreBoundaryTests` is intermittently unreliable inside a full Swift run (§5.3).
- The stray `~/.local/bin/omnivia-core-service` is still on this host (§5.1).

## 7. What this record does not claim

- No `L5`/`L6` evidence. Nothing was built as a signed or notarised package, installed, activated or
  run on a packaged host, and no real MCP host was run.
- No acceptance-case, work-ID or A-scenario status moves. `WORK-STATUS.json`, the WP-15 ledger and
  `2026-10-01-update-qualification-record.md` are untouched.
- Settings v2 is not delivered as a product feature: the two operations are refused and the window is
  not wired to them.
- A clean local preflight is local evidence. The required checks `Core acceptance` and the three
  `Phase 2 platform (<os>)` rows are reported only by the run on the pull request; the Swift steps ran
  here on macOS only.
- The real-process observation in §4.3 ran from the checkout against a bootstrapped installation, not
  a packaged one.

## 8. Summary

The ten-commit Settings v2 packet is on the repaired P1-P10 head (`a7454199`) with its whole
capability intact: six-pane window, the Wave 1-4 Document processing and Capture controls still
reachable as details, and `settings.list` / `settings.set` published in the Application Contract with
CLI commands and server-owned mutation entries. Four adaptations were required and each is narrow:
the two operations are appended to a catalogue that is frozen in insertion order (positions 87 and
88, not the packet's alphabetical slot); a `settings` authority family that recognises and refuses
the two operations restores a service that otherwise refused to start for want of a handler; the
counts, ledgers and partitions that follow 86 becoming 88 were carried through; and the packet's
malformed barrel edit was repaired. The generated contract artifacts reproduce from their generators.
`./scripts/preflight` passes on `cf248b71` (38060 tests passed in one invocation, Ruff and strict
mypy clean, Swift 1011 tests, 0 failures) with one test deselected that fails identically at the
baseline. Per §10 of the plan this is `L3 component`, with one real-process observation, and no
higher: nothing is packaged, no real MCP host was run, and the two settings operations are refused
rather than served.
