# OmniVia Core MCP standalone authoring and ingestion completion plan

**Date:** 2026-10-03

**Status:** in progress — execution baseline for PR #167

**Active branch:** `codex/core-mcp-authoring-phase8-closeout`

**Active pull request:** #167

**Requirements baseline:**
`docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md`

**Original implementation plan:**
`docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-implementation-plan-2026-09-12.md`

**Current traceability record:**
`docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-traceability-2026-09-12.md`

**Accepted implementation:** PR #107, merge commit
`ad5c05c2c644d53015bab285123cf1fea5304887`

**Historical, superseded qualification work:** PR #108, closed without merge.
It is reference material only and must not be merged or cherry-picked wholesale.

---

## 1. Outcome

Close the remaining acceptance and documentation gap without reimplementing the
authoring capability already merged in PR #107.

Completion means:

1. the currently supported MCP inventories are explicitly accepted and
   documented;
2. release-form wheels are installed in isolation using the reviewed SDK pins;
3. real supported Claude Code and Codex binaries execute the complete authoring
   and ingestion journey against those installed artifacts;
4. a bounded, redacted and reproducible qualification record is retained;
5. requirements, traceability, package documentation and host documentation all
   describe the same product behavior;
6. exact-tip preflight, independent review and required hosted checks pass;
7. the accepted work is merged and temporary completion state is retired.

Release publication and modification of a user's normal Claude, Codex or
production Core configuration remain separate, explicitly authorized actions.

---

## 2. Current state

### 2.1 Complete and not to be rebuilt

PR #107 already delivered and verified:

- canonical `evidence.capture` contracts and runtime handling;
- direct-source identity, deduplication, conflicts, byte bounds and audit;
- durable idempotency and replay authorization;
- searchable projection readiness and crash recovery;
- proposed-memory creation;
- staged import execution and job observation;
- MCP profile enforcement and canonical dispatch;
- installed `mcp configure`, `mcp status` and `mcp revoke` administration;
- dedicated principals, protected credentials, bounded grants and immediate
  revocation;
- owner-private configuration on supported platforms;
- source-tree end-to-end, concurrency, recovery, redaction and security tests;
- a complete local preflight and seven green hosted checks on final PR head
  `d79895d28f041f5184b3369f1374246b6ae3c273`.

The completion lane must preserve those guarantees and avoid broad rewrites.

### 2.2 Execution snapshot — 2026-10-04

The active closeout branch is based on current `origin/main`. Its pushed
checkpoints include `72d5a4ff` for the documentation/conformance repairs,
`cf061a09` for the seventeen-step harness, tests and record schema, and
`960ed703` for the initial PR #108 disposition map. The latest reviewed and
pushed predecessor is `36fa677f9fb57ca37661d33d998d1f326a7db874`. Independent
review found qualification gaps and an incomplete paginated PR #108 inventory;
that checkpoint corrected the full 121-path map, deterministic excluded-tool
dispatch proof, same-session revocation, structured-result validation and
related coverage. A final focused review then required the revocation gate to
distinguish the exact installed-credential-missing outcome from a generic
client failure; that hardening is checkpointed at `9022e2aa`. A final
independent review of `9022e2aa` then returned eleven actionable findings. The
repair round for them (recorded in section 10 of the Phase 8 completion plan)
is not yet checkpointed. It creates a later candidate, so no commit is the final
immutable qualification candidate, and no exact-tip acceptance is claimed. The
historical Codex CLI run at `4ec9fa17` is diagnostic only and unauditable under
the current closed record schema; it closes no current I row and cannot satisfy
I-1 through I-8 at this or any later tip.

Completed on the branch:

- a v1.4 addendum accepts and classifies the current thirteen-tool restricted
  and eighteen-tool authoring inventories;
- installed-wheel restricted and authoring journeys, a closed redacted record
  schema, and deterministic conformance tests exist locally. The retained-record
  gate is not current frozen-candidate evidence, so B-12 and H-5 through H-7
  stay partial;
- the real-host harness installs release-form artifacts in isolation and checks
  exact inventories. A historical Codex CLI run at clean tip `4ec9fa17` is
  diagnostic only: its record is unauditable under the current closed schema,
  it closes no current I row, and it is not acceptance evidence for any tip;
- Claude Code portable-token support exists without reading or changing the
  operator's normal host configuration;
- local transport shutdown/restart defects found by qualification were fixed
  and stress-tested;
- active MCP, CLI, installation, service-lifetime, staged-import, Apple
  permission, and host-interoperability documentation has been reconciled with
  the implemented behavior;
- the real-host harness and closed record schema represent every step in
  the section 8 journey, including excluded-tool refusal, replay/conflict,
  stable event paging, revocation and post-host Core health. Before the review
  repairs, 358 focused tests and a 1,754-test MCP/CLI/traceability integration
  gate passed. The first repair checkpoint reached 454 focused tests and a
  1,796-test integration gate. Both sets are historical and superseded; the
  current focused and four-directory integration results are recorded in the
  Phase 8 plan and the section 11 execution runbook;
- the 121 paths changed by historical PR #108 are classified in
  `omnivia-core-pr-108-file-disposition-map-2026-10-04.md`: 2 already ported,
  12 rewritten for the current architecture, 96 already superseded and 11
  rejected.

Remaining before completion:

1. checkpoint the focused-reviewed harness hardening, complete full exact-tip
   independent review, and freeze the resulting clean source tip;
2. build and verify release-form artifacts from that exact tip;
3. run both real hosts against the frozen artifacts;
4. obtain a token-only Claude credential outside the repository from
   `claude setup-token`; the token must never be pasted into the repository or
   retained evidence;
5. update traceability and completion status only from the new exact-tip host
   records;
6. complete exact-tip full preflight and all hosted
   checks at the same pushed tip;
7. merge only with explicit user authorization, record the release decision,
   and archive temporary worktrees only after separate cleanup authorization.

### 2.3 Critical path

| Order | Work package | Status | Exit condition |
|---|---|---|---|
| 1 | Stabilize current branch | focused patch review complete; full exact-tip review pending | Final hardening passes full independent review and is committed and pushed |
| 2 | Close planning evidence | complete through reviewed predecessor; this update pending | This plan and the complete 121-path PR #108 file-disposition map are tracked |
| 3 | Complete harness coverage | complete locally; checkpoint pending | Automated tests prove every section 8 case and reject incomplete evidence |
| 4 | Freeze candidate | pending | Clean source tip, release wheels, SDK pins, and host versions are immutable |
| 5 | Run real hosts | blocked on Claude token and frozen tip | Claude Code and Codex CLI each produce schema-valid passing records for the frozen tip |
| 6 | Reconcile records | pending | Traceability and docs cite the exact qualified source tree without overstating historical evidence |
| 7 | Accept exact tip | pending | Independent review, focused suites, full preflight, and hosted checks are green |
| 8 | Integrate and retire | authorization required | Authorized merge, explicit release decision, and separately authorized cleanup are complete |

---

## 3. Scope

### In scope

- a small requirements completion addendum or successor baseline;
- reconciliation of the current thirteen/eighteen-tool MCP inventories;
- selective porting and adaptation of the useful PR #108 qualification harness;
- installed-wheel qualification using the reviewed MCP SDK pins;
- isolated real-host qualification for Claude Code and Codex;
- redacted qualification evidence and machine validation of that evidence;
- correction of MCP, CLI, installation and host-interoperability documentation;
- completion of the v1.3 requirement-to-evidence traceability record;
- exact-tip review, preflight, PR, hosted CI, merge and scoped cleanup.

### Out of scope

- native mailbox capture;
- Add UI or Share extension work;
- background filesystem watchers;
- arbitrary path or URL staging through MCP;
- remote MCP transport;
- governance approval tools, grant administration, `job.cancel` or `job.retry`;
- user production workspaces or normal host profiles;
- release publication, signing or distribution unless separately authorized;
- wholesale integration of PR #108.

---

## 4. Execution rules

1. Continue the active `codex/core-mcp-authoring-phase8-closeout` lane, which
   was created from current `origin/main`; do not restart from the historical
   MCP completion worktree or either old PR tip.
2. Keep all closeout writes in the dedicated active worktree. Preserve the
   unrelated changes in the canonical checkout and do not reuse the stale
   completion worktree as a base.
3. Use one writable implementation lane. Claude implements; Codex prepares the
   bounded task, reviews every diff, runs authoritative checks and controls
   integration.
4. Classify the Claude lane under the current trust policy before launch and
   keep its writable scope inside this repository.
5. Treat PR #108 as research evidence. Port individual ideas only after
   comparing them with current architecture and tests.
6. Use isolated temporary installation roots, workspaces, credentials and host
   profiles. Never read or alter normal Claude/Codex configuration.
7. Do not retain submitted content, credentials, absolute private paths,
   process identifiers, raw model output or host transcripts in qualification
   evidence.

Active branch:

```text
codex/core-mcp-authoring-phase8-closeout
```

---

## 5. Phase 0 — freeze the completion baseline

### Work

1. Fetch current `origin/main` and record:
   - source commit and tree;
   - manifest version;
   - restricted and authoring tool inventories;
   - operation catalogue and migration heads;
   - SDK wheelhouse pins;
   - supported Python, macOS and architecture baselines;
   - installed Claude Code and Codex versions available for qualification.
2. Confirm PR #107 is an ancestor of the baseline.
3. Compare the useful PR #108 commits without merging them, concentrating on:
   - `scripts/run-host-qualification.py`;
   - `scripts/qualification-stage-import-source.py`;
   - real-host qualification tests;
   - the redacted evidence schema;
   - restart/recovery helpers;
   - host and package documentation;
   - the authoring extension to the Standard journey.
4. Produce a file-level port map with one of four dispositions for each PR #108
   change: `port`, `rewrite for current architecture`, `already superseded`, or
   `reject`.
5. Confirm no current requirement depends on the stale
   `codex/core-mcp-completion` branch.

### Exit gate

- one current baseline is named;
- the implementation boundary is known;
- no historical branch is being treated as authoritative;
- no generated file, migration or tool inventory ambiguity remains.

---

## 6. Phase 1 — reconcile the specification with the current MCP inventory

This is a product-security gate and must precede host qualification.

### Recommended decision

Retain the current thirteen/eighteen inventories and issue a completion addendum
that supersedes only the frozen inventory counts and restricted-profile wording
in v1.3. Do not roll back later accepted Engineering Memory or decision-runtime
tools merely to reproduce the September six/eleven count.

The addendum must:

1. list every tool in both profiles in deterministic order;
2. identify the five authoring additions separately;
3. classify every side effect and audit category;
4. state that `restricted` means the bounded default profile, not necessarily a
   wholly read-only profile, if `decision.evaluate` remains present;
5. explain why `decision.evaluate` is admitted and which grants, idempotency and
   audit rules constrain it;
6. preserve the excluded-operation list;
7. preserve the rule that models cannot select workspace, principal, purpose,
   scope, capability, credential or grant;
8. record the current manifest version and the compatibility rule for future
   inventory growth;
9. define the current real-host acceptance matrix.

If the bounded-default interpretation is not accepted, stop before Phase 2 and
prepare a separate architecture plan to move `decision.evaluate` out of the
restricted profile. Do not silently call the profile read-only.

### Deliverables

- a v1.4 completion addendum or an equivalently explicit successor document;
- updated plan/traceability references to that addendum;
- exact inventory tests generated from or checked against the manifest;
- removal of incorrect six/eleven and read-only claims from active docs.

### Exit gate

- requirements, manifest and traceability agree on profile semantics and exact
  inventories;
- every mutation has an explicit acceptance basis;
- no test is being changed merely to bless unexplained drift.

---

## 7. Phase 2 — port the qualification harness onto current architecture

### Work

1. Rebuild the PR #108 harness as a current-main change rather than cherry-pick
   the divergent branch.
2. Keep the harness outside production runtime paths. Expected files are:
   - `scripts/run-host-qualification.py`;
   - `scripts/qualification-stage-import-source.py`;
   - a narrowly scoped interrupted-response relay helper;
   - `tests/service_conformance/test_mcp_real_host_qualification.py`;
   - a versioned evidence fixture under
     `docs/development/qualification-evidence/`.
3. Make expected inventories explicit and validate them independently against
   manifest 2.3. A host exposing fewer, more or reordered tools must fail.
4. Build all Core distributions and acquire the reviewed wheelhouse.
5. Install into a fresh environment with:
   - `--no-index`;
   - `--only-binary=:all:`;
   - the reviewed wheelhouse only.
6. Prove runtime imports resolve from the installed environment, not the source
   checkout.
7. Verify the installed SDK versions exactly match the reviewed constraints.
8. Bound every subprocess, frame, page, input, wait and retained record.
9. Fail closed when a host version, inventory, configuration, response or
   evidence field differs from the accepted baseline.
10. Add unit tests for evidence redaction, schema closure, version checks,
    installed-origin checks and cleanup on failure.

### Evidence-record rules

The retained JSON record may contain only:

- qualification format version;
- qualified source tree or source commit;
- operating-system product/version/build and architecture;
- host names and versions;
- distribution and SDK versions;
- wheel count and bounded digest;
- profile and exact tool names;
- fixed case identifiers, counts, dispositions and verdicts;
- a final pass/fail verdict.

It must not contain credentials, workspace content, prompts, model responses,
absolute private paths, endpoints, PIDs, raw stderr/stdout, configuration bodies
or arbitrary exception text.

### Exit gate

- harness tests pass without installed hosts;
- a deliberately wrong inventory, SDK pin, source import, evidence field or
  host version fails closed;
- the harness cannot touch normal host configuration or production workspaces.

---

## 8. Phase 3 — run installed real-host qualification

### Environment

Use a clean supported macOS arm64 account or isolated equivalent. Record the
exact OS build and host versions. Prefer currently supported installed versions;
if they differ from the versions frozen in v1.3, record the replacements and
the approval basis in the completion addendum.

### Required journey for each host

Run the same release-form installed artifacts through real Claude Code and real
Codex processes. Each host must:

1. initialize successfully;
2. discover the exact current authoring inventory;
3. prove excluded tools are absent and undispatchable;
4. capture evidence into a new empty workspace;
5. search and retrieve that evidence before capture reports success;
6. create a proposed memory from canonical evidence/source references;
7. prove the proposal is hidden from the default view and visible in the
   candidate view;
8. replay capture and memory creation with the same key and obtain stable
   canonical results;
9. change each payload under the same key and receive
   `idempotency_conflict`;
10. start an import from an already staged descriptor;
11. observe the job through `job_get` and stable paginated `job_events`;
12. retrieve imported evidence;
13. interrupt one committed response, restart Core and the host, and recover
    the result using the same key without a duplicate effect;
14. prove MCP stdout is protocol-only;
15. revoke the installed MCP principal and prove subsequent calls and replays
    fail closed;
16. prove the already committed import remains complete and owner-observable;
17. prove the independently owned Core service remains healthy after the host
    session exits.

The additional restricted-profile decision behavior introduced after v1.3 must
also receive the exact acceptance cases required by the Phase 1 addendum.

### Exit gate

- both real hosts pass every required case;
- the installed package and SDK origins are proven;
- the evidence record is schema-valid, redacted and committed;
- temporary profiles, credentials, workspaces and processes are removed.

---

## 9. Phase 4 — reconcile active documentation and traceability

Update, at minimum:

- `packages/omnivia-core-mcp/README.md`;
- `packages/omnivia-core-cli/README.md`;
- `docs/distribution/mcp-host-interoperability.md`;
- `docs/distribution/shared-core-installation.md`;
- the v1.3 completion addendum or successor requirement;
- the original implementation plan status;
- the requirement traceability record;
- `CHANGELOG.md` if repository release policy requires it.

Documentation must cover:

- restricted versus authoring selection;
- exact current inventories and manifest version;
- setup for Claude Code and Codex;
- status, reconfiguration, credential rotation and revoke;
- restart/caching behavior;
- the staged-only import boundary;
- why arbitrary paths and URLs are not MCP inputs;
- Apple permissions: Core/MCP itself needs no protected-folder permission, while
  a separate staging or connector process may;
- installation/workspace ownership and service lifetime;
- the difference between SDK simulation, configuration-shape testing and real
  host qualification;
- the retained qualification baseline and its limitations.

Update H-5 through H-7 and I-1 through I-8 from `pending-phase-8` only when the
new evidence exists. Record PR #107 as the accepted feature implementation and
the closeout PR as the accepted qualification/documentation completion.

### Exit gate

- repository search finds no active six-tool, eleven-tool or purely read-only
  claim that contradicts the accepted current profiles;
- every completion claim links to automated, installed-wheel or real-host
  evidence of the correct type;
- no historical evidence is presented as current-tip evidence.

---

## 10. Phase 5 — review and verification

### Targeted checks

Run the focused suites for:

- manifest and schema projection;
- MCP configuration and authority;
- standalone authoring and import journeys;
- restart and ambiguous-response recovery;
- installed setup verification;
- CLI MCP administration;
- package qualification and Standard journey;
- qualification harness and evidence schema;
- requirement traceability;
- migration allocation and generated-artifact cleanliness.

### Independent review

Review the complete diff against current `origin/main`, emphasizing:

- profile semantics and side-effect classification;
- authority derivation and confused-deputy resistance;
- replay after revocation;
- content, credential and path redaction;
- subprocess and input bounds;
- source-tree leakage into installed qualification;
- real-host versus SDK evidence classification;
- temporary-state cleanup;
- documentation truthfulness.

Resolve every actionable correctness or security finding before final
qualification. Rerun any host evidence affected by a production, harness,
inventory, configuration or qualification change.

### Full gate

On the exact reviewed candidate:

```text
./scripts/preflight
```

Then confirm:

- clean working tree;
- generated files unchanged after regeneration;
- no untracked credential, profile, workspace or evidence file;
- qualification record validates;
- package builds and installed smoke pass.

### Exit gate

- independent review has no unresolved actionable finding;
- full preflight passes on the exact pushed commit;
- retained host evidence still corresponds to the same code tree.

---

## 11. Phase 6 — integration and closeout

### 11.1 Current execution runbook — 2026-10-04

The earlier phases describe the required outcome and controls. From the current
branch state, complete the work in the following order. Do not skip ahead from a
failed gate, and do not treat a reported Claude check as Codex-accepted evidence
until Codex has rerun it.

Current local status at the time of this plan update:

| Area | State | Evidence or next action |
|---|---|---|
| Eleven-finding repair round | implemented, uncommitted | Complete diff remains to receive one final independent read-only review |
| Relay fail-closed/deadlock repair | implemented and locally verified | Three new real-proxy regressions are included in the 553-test focused gate |
| Focused qualification/traceability gate | passing | 553 tests pass (515 at the Gate A review baseline, plus 31 completed-review and seven final-review regressions); strict mypy for both qualification scripts, Ruff and `git diff --check` pass |
| Phase 2 shared-launcher failure | corrected and locally verified | The complete local Phase 2 suite passes after the final-review repair with 596 tests and four expected platform-specific skips; require fresh Linux, macOS and Windows results after push |
| Broader integration gate | passing | The four named directories pass 2,659 tests after the final-review repair. The earlier 2,652, 2,645, 1,902 and 1,796 results are historical; 1,902 covered only the MCP package, CLI package and three focused files |
| Qualified runtime commit and candidate | not frozen | Freeze only after the final independent read-only review, reviewed checkpoint commit and push |
| Real-host records | not current | Historical Codex records are diagnostic only; both hosts must run against the same new candidate |
| Exact-head preflight and hosted checks | pending | Run only after the evidence closeout head is clean and pushed |
| Merge and cleanup | not authorized | Request each authorization only at Gate G |

#### Gate A — accept or repair the uncommitted review response

1. Review the complete uncommitted repair against `9022e2aa`, with
   particular attention to:
   - the complete excluded-operation set and its v1.4 addendum basis;
   - `decision.evaluate` refusal and owner-observation semantics;
   - wheel closure, hashes and source-isolation guarantees;
   - MCP protocol negotiation, pagination and frame bounds;
   - digest canonicalization, cleanup and child-process reaping;
   - exact revocation classification and truthful evidence booleans.
2. Rerun the three focused qualification/traceability files and record the
   actual count from the current diff; the current local result is 553 passing
   tests (515 at the Gate A review baseline, before the completed review's 31
   and the final review's seven regressions), and historical counts must not be
   carried forward.
3. Run Ruff, strict mypy for both qualification scripts and `git diff --check`.
4. Run the broader four-directory gate. The current post-final-review result is
   2,659 passing tests. The 1,902-test figure covered only the MCP package, CLI
   package and three focused files and was not this gate; the earlier 2,652,
   2,645 and 1,796 results are historical and superseded.
5. Obtain a fresh independent read-only correctness/security review of the
   entire current diff. Resolve every actionable finding and repeat the affected
   checks.
6. Commit and push one reviewed repair checkpoint only after Gates A.1-A.5 pass.

Exit condition: the working tree is clean, the repair is independently accepted,
and the pushed checkpoint has reproducible local results.

#### Gate B — clear the known hosted-platform failure

The current pushed PR tip `9022e2aa` is blocked on all three Phase 2 platform
jobs. The shared-launcher ownership test found the literal
`omnivia-core-service` in `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py`.
The smallest architecture-correct correction is now present in the uncommitted
repair and its targeted ownership regression passes. Do not weaken the test.
The complete local Phase 2 suite passes after the final-review repair with 596
tests and four expected
platform-specific skips. Include the correction in the reviewed candidate and
require fresh Linux, macOS and Windows results after the next push.

Exit condition: the local Phase 2 suite passes and the correction has been
reviewed together with the qualification changes.

#### Gate C — freeze and build the runtime candidate

1. Freeze a clean commit, called the **qualified runtime commit**, containing all
   production, harness, schema, test and substantive documentation changes.
2. Build the Standard candidate from that clean commit without `--allow-dirty`.
3. Verify the candidate manifest, every first- and third-party wheel digest,
   the reviewed SDK pins, installed import origins and offline installation.
4. Run the installed restricted, authoring and lifecycle journeys from that
   candidate.

Exit condition: one immutable candidate directory and one qualified runtime
commit are named; no code or harness change is permitted without discarding the
candidate and restarting Gate C.

#### Gate D — qualify both real hosts

1. Before either run, obtain explicit user authorization for the external data
   flow. Each real host sends fixed qualification prompts and bounded,
   service-derived results through its selected provider. Authorization to
   implement the harness, a local login, or a supplied credential file does not
   authorize that transfer.
2. Run Codex CLI first against the frozen candidate in an isolated profile.
3. Run Claude Code against the same candidate and schema. This remains blocked
   until the operator creates the token-only, owner-protected file outside the
   repository using `claude setup-token`. The token must not be pasted into chat,
   committed or retained in evidence.
4. Validate both records against the closed schema and confirm every I-1 through
   I-8 case and the restricted decision cases pass.
5. Confirm temporary profiles, credentials, processes, roots and workspaces were
   removed. A cleanup failure is a failed qualification.

Exit condition: two schema-valid passing records identify the same qualified
runtime commit, candidate digest set, SDK pins and accepted host versions.

#### Gate E — land evidence without changing the qualified runtime

1. Commit only the two redacted records and the resulting traceability/status
   updates in an **evidence closeout commit**.
2. Review the diff from the qualified runtime commit to the evidence closeout
   commit and prove that it contains no production, harness, schema, manifest,
   configuration or test-behavior change.
3. If any such behavior changes, discard the host records, return to Gate C and
   requalify both hosts. Evidence-only prose corrections may proceed only when
   they do not change the qualified behavior or acceptance rules.

Exit condition: the PR head truthfully cites the qualified runtime commit while
the executable behavior remains identical to the frozen candidate.

#### Gate F — exact-head acceptance

1. Run `./scripts/preflight` on the clean evidence closeout commit.
2. Confirm regeneration is clean and no credential, temporary profile,
   workspace, process or unredacted evidence remains.
3. Push the exact reviewed head and require every hosted check on that SHA,
   including `Core acceptance` and all three required Phase 2 platform jobs.
4. Perform one final independent review of the PR diff and the two retained
   records. Do not change the head after the green/check-reviewed state without
   rerunning the affected gates.

Exit condition: PR #167 is merge-ready on one exact green head with no unresolved
review finding.

#### Gate G — authorized integration and retirement

1. Ask for explicit merge authorization. Do not merge merely because Gate F is
   green.
2. After authorization, merge PR #167 and verify the merge on `origin/main`.
3. Record the PR, reviewed head, qualified runtime commit, evidence closeout
   commit and merge commit. Record the release decision as either published by
   a separately authorized release plan or `release-ready, publication deferred`.
4. Ask separately for cleanup authorization before archiving this worktree or
   the historical MCP completion worktree.

Exit condition: merge, release disposition and any authorized cleanup are
recorded; no implicit release or worktree deletion has occurred.

### 11.2 Remaining external decisions and blockers

| Item | Current state | Required action |
|---|---|---|
| Claude Code qualification credential | blocked; token file absent | Operator runs `claude setup-token` and supplies a token-only owner-protected file outside the repository |
| Real-host external data flow | blocked; no specific approval recorded | Obtain explicit user approval to send fixed qualification prompts and bounded service-derived results through the Codex and Claude providers |
| PR #167 hosted checks | pushed tip is blocked; local launcher correction awaits full Phase 2 verification | Pass the full local Phase 2 suite, push the reviewed candidate, and obtain fresh exact-head checks |
| Merge | not authorized | Request explicit authorization only after Gate F |
| Release publication | not authorized and out of scope | Record publication as deferred unless separately authorized |
| Worktree cleanup | not authorized | Preserve both completion worktrees until separately authorized |

### 11.3 Normative integration sequence

1. Push the reviewed exact tip and open one focused completion PR.
2. Attach the PR to the active Codex task.
3. Wait for every required hosted check on that exact tip:
   - `Core acceptance`;
   - `Phase 2 platform (ubuntu-latest)`;
   - `Phase 2 platform (macos-latest)`;
   - `Phase 2 platform (windows-latest)`;
   - all other checks registered by the branch.
4. Merge only when the exact tip is green and every review finding is resolved.
5. Confirm the merge is present in `origin/main`.
6. Record the PR URL, reviewed head, qualified source/tree and merge commit in
   the closeout record. If a literal merge SHA must live in the repository,
   land that value through a subsequent documentation-only PR rather than
   pretending it was knowable before merge.
7. Make the release decision explicit:
   - `release-ready, publication deferred`; or
   - execute a separately authorized release plan.
8. After merge and only with the user's cleanup authorization:
   - archive the temporary closeout worktree;
   - archive the historical
     `/Users/claytonread/Projects/worktree-omnivia-core-mcp-completion`
     worktree as superseded;
   - remove obsolete branch labels after verifying recoverable Git history;
   - leave unrelated active worktrees untouched.

---

## 12. Responsibility model

| Responsibility | Owner |
|---|---|
| Baseline, scope, task packet and integration decisions | Codex |
| Bounded harness, tests and documentation implementation | Claude |
| Diff review and source-truth verification | Codex |
| Real-host execution and redacted evidence collection | Codex-controlled local lane |
| Independent correctness/security review | separate read-only review lane |
| Full preflight, PR, hosted-check monitoring and merge | Codex |
| Release publication decision | user |
| Worktree retirement authorization | user/Codex under explicit cleanup scope |

Do not split tightly coupled manifest, harness and evidence-schema writes across
parallel writers. Read-only review and documentation inventory may run in
parallel after the profile baseline is frozen.

---

## 13. Risks and controls

| Risk | Control |
|---|---|
| Old PR #108 code overwrites accepted PR #107 fixes | selective port map; no wholesale cherry-pick |
| Qualification blesses stale six/eleven inventory | freeze and assert current manifest before harness work |
| Restricted profile is falsely described as read-only | explicit Phase 1 decision and side-effect table |
| SDK client is presented as a real host | separate evidence types; require actual host processes |
| Source checkout leaks into installed qualification | installed-origin assertions and isolated cwd/environment |
| Qualification modifies normal host configuration | isolated profiles and before/after configuration hashes |
| Evidence leaks content, secrets or paths | closed evidence schema and redaction tests |
| Host upgrade invalidates evidence | record exact versions; fail closed on unsupported drift |
| Final code changes invalidate host evidence | rerun affected host qualification after relevant changes |
| New worktree becomes hidden source of truth | one temporary lane, prompt integration and archival |
| Release publication is conflated with implementation completion | explicit release-ready/deferred decision |

---

## 14. Definition of done

The closeout is complete only when all of the following are true:

- the current MCP profile semantics and inventories are approved in a normative
  document;
- code, requirements, traceability and active documentation agree;
- no active document claims the current MCP is six-tool or read-only when it is
  not;
- all authoring, import, replay, restart, revocation and exclusion guarantees
  remain covered by automated tests;
- release-form packages install offline from the reviewed wheelhouse and use the
  reviewed SDK pins;
- real supported Claude Code and Codex hosts pass the full isolated workflow;
- the retained qualification record is redacted, bounded and machine-checked;
- independent review is clean;
- exact-tip `./scripts/preflight` passes;
- required hosted checks are green on the merged candidate;
- the completion PR is merged into `main`;
- release publication is explicitly completed or explicitly deferred;
- no live test credential, host profile, process or workspace remains;
- temporary and historical completion worktrees are archived when cleanup is
  authorized.
