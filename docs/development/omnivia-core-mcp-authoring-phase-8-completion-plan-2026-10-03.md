# OmniVia Core MCP authoring Phase 8 completion plan

**Date:** 2026-10-03
**Status:** Historical 13/18 closeout evidence at `0d8cf362`; the live 14/35 candidate is not qualified; exact-head acceptance pending
**Owner:** Codex (orchestration, review, acceptance); Claude Code (bounded implementation)
**Target repository:** `omnivia-core`
**Working branch:** `codex/core-mcp-authoring-phase8-closeout`
**Reviewed predecessor checkpoint:** `9022e2aa` (the independent-review hardening
of the harness, checkpointed on the working branch)
**Qualified runtime commit:**
`0d8cf362d15b43077a744542974b6160c283e1dc`.
**Candidate key:**
`857a914d1f98f4111019bed2de1a5a4ed0325f19152a847f1d3968897f98009b`.
The exact candidate passed its restricted, authoring and lifecycle journeys and
both pinned real hosts produced schema-valid passing records. Those records are
historical: they carry the 13/18-tool inventories and superseded harness and
schema digests. They do not qualify the live candidate, which is manifest 2.8
with 14 restricted and 35 authoring tools. Current-candidate installed-wheel and
real-host qualification is an external credentialed residual. Full preflight and
hosted checks remain before exact-head acceptance.

## 1. Objective

Close the remaining release evidence for the standalone MCP authoring and
ingestion feature without changing its provider- and model-agnostic design.
Completion means that installed-wheel authoring, real Claude Code and Codex CLI
operation, recovery, restart, revocation, redaction, and exact-tip acceptance
are all evidenced at one frozen release-candidate commit.

This plan does not add provider or model fields to Core. Claude Code and Codex
CLI are qualification hosts that connect to Core through the MCP adapter; the
authority for every write remains Core's dedicated, workspace-bounded MCP
principal and protected authoring grant.

## 2. Authoritative inputs

- `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-10-03-v1.4-addendum.md`
  (normative completion baseline; it supersedes the v1.3 inventory and read-only
  restricted statements as section 2 of the addendum records, and it marks no gate green)
- `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md`
- `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-implementation-plan-2026-09-12.md`
- `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-traceability-2026-09-12.md`
- `docs/distribution/mcp-host-interoperability.md`
- `packages/omnivia-core-mcp/README.md`
- PR #107, merged as `ad5c05c2c644d53015bab285123cf1fea5304887`

## 3. Current evidence and remaining gates

PR #107 proved the reviewed wheel closure at its exact head, including
`mcp==2.0.0`, `mcp-types==2.0.0`, all five distribution builds, and the full
preflight. That evidence is historical: it covered H-5 and H-7 at that accepted
revision only and closes no current row. H-5 through H-7 and B-12 stay partial
until the current release candidate is retested at one exact frozen commit,
because the repository has changed since the merge.

The live candidate is manifest 2.8, with fourteen restricted and thirty-five
authoring tools and fifteen admitted mutations. Every gate below applies to that
candidate. The 13/18 evidence at `0d8cf362` is historical: it satisfied the
13/18 rows at that snapshot and satisfies none of them for the live candidate.

The remaining functional and release gates for the live candidate are:

| Gate | Required outcome |
|---|---|
| H-6 | An installed-wheel journey exercises the live thirty-five-tool authoring profile, not only the restricted profile. The eighteen-tool run at `0d8cf362` is historical and does not close this row. |
| B-12 | A retained qualification record for the live candidate contains only the approved redacted fields. The `0d8cf362` records are historical, bound to superseded digests, and are not a live-candidate record. |
| I-1 | Install the exact live release artifact on the supported macOS qualification account/environment. |
| I-2 | Configure restricted and authoring profiles using native Claude Code and Codex CLI settings. |
| I-3 | Prove initialization and exact tool discovery under each real host. |
| I-4 | Run the empty-workspace authoring journey under each real host. |
| I-5 | Run the staged-import and job-observation journey under each real host. |
| I-6 | Prove same-key recovery after an intentionally interrupted response. |
| I-7 | Prove protocol-only stdout, host restart, Core service restart, and continued observation. |
| I-8 | Revoke authoring and prove writes fail closed under the documented restart model. |

## 4. Frozen qualification baseline

At execution start the approved replacement baseline was (the same pins apply to
the live candidate, which still needs its own qualification records):

| Component | Qualification value |
|---|---|
| Claude Code | `2.1.289` |
| Codex CLI | `0.146.0` |
| macOS | `27.0` build `26A428`, arm64 |
| MCP SDK | `mcp==2.0.0`, `mcp-types==2.0.0` from `scripts/mcp-wheelhouse-constraints.txt` |

The closeout record must replace the release-candidate base above with the
final tested commit and include the artifact digests. Any host or SDK change
requires repeating the affected qualification rather than carrying evidence
forward by assumption.

## 5. Work packages

### WP1 — Repair documentation and inventory drift

Status: executed for the 13/18 snapshot. For the live candidate the same items
apply to fourteen restricted and thirty-five authoring tools; the v1.4 addendum
records only the 13/18 snapshot.

1. Update the MCP package README to describe the restricted profile and the
   authoring profile, including the authoring additions. At the 13/18 snapshot
   that was thirteen restricted tools and eighteen authoring tools with five
   additions; the live profiles are fourteen and thirty-five, with twenty-one
   additions.
2. Update the interoperability guide so installed-wheel evidence and real-host
   evidence are explicitly separate.
3. Correct the manifest comment from nineteen to eighteen tools (historical,
   13/18 snapshot); the live manifest comment must match the live count.
4. Update the traceability record with PR #107's accepted merge and historical
   H-5/H-7 evidence without claiming that the current candidate has passed.

### WP2 — Add installed-wheel authoring qualification

Status: the journey exists and ran for the 13/18 snapshot. It must run again for
the live candidate.

Add a dedicated authoring qualification journey or a clearly separated
authoring mode beside `scripts/run-standard-journey.py`. It must run only from
installed wheels in an isolated installation and must prove:

1. exact discovery of the authoring profile: eighteen tools at the 13/18
   snapshot, thirty-five for the live candidate;
2. direct evidence capture and immediate evidence search;
3. evidence-backed proposed-memory creation;
4. default invisibility and candidate-view visibility;
5. stable replay and changed-input conflict;
6. staged import plus `job_get` and `job_events` observation;
7. a fresh MCP session and a restarted Core service preserve recovery;
8. authoring revocation blocks the next mutation while Core remains healthy;
9. the retained result is the closed redacted schema defined in WP4.

The restricted Standard journey remains a separate proof and must not silently
gain write authority.

### WP3 — Qualify the two real installed hosts

Use isolated temporary host homes, configurations, workspaces, credentials,
and installation state. Do not alter the operator's normal Claude Code or Codex
configuration. For both Claude Code and Codex CLI:

1. install and use the exact candidate artifacts;
2. configure native stdio MCP settings for restricted and authoring profiles;
3. run real host processes and make the host invoke the MCP tools;
4. cover discovery, empty-workspace authoring, import observation, interrupted
   response replay, host restart, Core restart, stdout integrity, and revoke;
5. retain only the redacted evidence record.

Prompts used for host qualification must direct the host to use the named MCP
tools and return only fixed pass/fail markers. Model output is not accepted as
the evidence source; the qualification harness must independently inspect Core
state and the redacted result schema.

### WP4 — Produce a redacted qualification record

The retained record may contain only:

- final commit and artifact SHA-256 digests;
- OS version, build, and architecture;
- host name and exact host version;
- pinned MCP SDK versions;
- profile, expected tool count, and stable tool names;
- fixed boolean/count outcomes for each gate;
- timestamps and an overall verdict.

It must not contain credentials, bearer or grant material, private paths,
workspace or principal identifiers, submitted content, prompts, transcripts,
endpoints, process identifiers, raw stdout/stderr, or model responses. A schema
validator and negative tests must enforce the closed field set.

The record is attached to acceptance evidence outside the candidate source
tree. It must name the frozen source commit and wheel digests. Committing it
into that same tree would change the commit it names, so a post-qualification
record commit is not accepted as exact-tip evidence.

### WP5 — Exact-tip acceptance

At one frozen commit:

1. build the wheelhouse and install with the reviewed pins;
2. run restricted and authoring installed-wheel journeys;
3. run both real-host qualification lanes and attach their schema-validated
   records to that frozen tip without another source commit;
4. run focused MCP, CLI, runtime, security, recovery, and redaction tests;
5. run `./scripts/preflight`;
6. push a pull request and obtain the required GitHub checks at that exact tip:
   `Core acceptance`, `Phase 2 platform (ubuntu-latest)`,
   `Phase 2 platform (macos-latest)`, and
   `Phase 2 platform (windows-latest)`.

### WP6 — Closeout

1. Mark H-1 through H-7, I-1 through I-8, and B-12 green only where the
   evidence directly proves the stated gate.
2. Record the final release-candidate commit and qualification artifact digests.
3. Change the feature status to complete only after all rows are green.
4. Commit the accepted changes and open a closeout pull request. Do not merge
   without explicit user authorization and green checks at the latest tip.

## 6. Verification commands

The exact implementation may add narrower commands, but closeout requires at
least:

```bash
PYTHON=.venv/bin/python scripts/check-package-builds.sh
.venv/bin/python -m pytest packages/omnivia-core-mcp/tests -q
.venv/bin/python -m pytest packages/omnivia-core-cli/tests -q
.venv/bin/python -m pytest tests/service_conformance/test_mcp_authoring_traceability.py -q
./scripts/preflight
```

The installed authoring and real-host commands added by WP2 and WP3 must also
run successfully and be named in the final traceability record.

## 7. Concurrency and delegation

The authoring journey, record schema, documentation, and traceability changes
are tightly coupled and use one serial implementation lane. Real-host execution
follows only after the installed-wheel harness passes, because it consumes that
harness and its redaction rules. Independent parallel write lanes are not used:
they would create competing definitions of the retained record and release
candidate.

Codex owns the plan, review, integration, acceptance evidence, commit, and pull
request. Claude Code receives a bounded `omnivia-core` implementation task;
Codex independently reviews its diff and reruns the required checks.

## 8. Definition of done

This plan is complete only when all of the following are true at one immutable
candidate commit:

1. restricted installed-wheel qualification passes;
2. authoring installed-wheel qualification passes;
3. Claude Code and Codex CLI each pass I-1 through I-8;
4. the redacted record exists and passes its positive and negative validators;
5. B-12, H-1 through H-7, and I-1 through I-8 are green with direct evidence;
6. full preflight passes;
7. the required hosted checks pass at the same tip;
8. documentation no longer describes the implemented server as universally
   read-only or as a six-tool server;
9. the accepted implementation is committed and reviewed in a pull request.

## 9. Separate hardening follow-up

`WindowsPipeError` exposes a Windows error in `.code`, while the named-pipe
accept-loop terminal classifier currently considers only `OSError.errno`.
Qualify this separately with a real Windows invalid-listener regression test
and make terminal invalid-handle errors exit rather than retry forever. Keep it
out of the Phase 8 closeout diff unless it blocks the required Windows gate.

## 10. Completion record

### Completion Notes

#### Repair round after independent review of `9022e2aa`

The final independent review of `9022e2aa` returned eleven actionable findings.
The repair was later checkpointed and pushed; it makes these changes, all in the
existing closed, redacted schema:

1. `decision.evaluate` is a real restricted-profile check: the host must be
   refused `capability_not_granted`, and the owner must observe a disabled
   decision surface with no record (gates `i3`:
   `decision_evaluate_refused`, `decision_owner_observed`).
2. Every excluded name is dispatched by the harness itself through the proxy,
   with no model in the loop: 62 names for `restricted` and 57 for `authoring`
  at the 13/18 snapshot.
   The authoring set is the 39 catalogue operations outside the manifest plus
   eighteen qualification sentinels spanning all nine section-7 administrative
   capability categories; restricted adds the five authoring-only tools. The
   sentinels are not catalogue operations. A conformance test ties the unexposed
   catalogue list to the catalogue and manifest. Each profile records its own
   absence and undispatchability booleans, and each probe runs while that
   profile's configuration is current because `configure` rewrites the host's
   one MCP configuration.
3. Capture and search, and default and candidate visibility, pass only when the
   host's canonical result digest equals the owner's digest of the same search.
   Only digests are retained.
4. The authoring revocation accepts only the installed credential store's exact
   sanitized message. Record booleans are set only by checks that completed.
   The real-host revocation classifier was already exact.
5. This section and the traceability and interoperability records are corrected
   to match. The `4ec9fa17` Codex CLI result is diagnostic and unauditable
   under the current schema, and cannot satisfy I-1 through I-8 at any later tip.
6. The whole third-party closure is checked: each manifest entry's path, size
   and SHA-256 must match, and the directory may hold no other wheel. Install
   uses `pip --require-hashes` from a generated file-URL requirements file, with
   no index and no find-links. A portable test invokes real pip against a
   locally generated wheel in a path containing spaces: the correct hash
   installs and a wrong hash is refused. The retained record binds the full
   normalized closure by count and digest and binds the exact harness and
   closed-schema bytes by SHA-256 through the bootstrap receipt.
7. Two lifecycles are pinned. Legacy `initialize` must negotiate `2025-06-18`;
   an initialize error, missing or malformed version, or version mismatch is a
   protocol violation. Modern: a valid `server/discover` result for `2026-07-28`
   stands for `initialize`, and a malformed one that claims `2026-07-28` is a
   protocol violation. An error or non-modern discovery is relayed unobserved so
   the host can fall back to legacy. Claude Code 2.1.289 uses modern discovery,
   then `tools/list` and `tools/call`, with no `initialize`.
8. A paginated `tools/list` (`nextCursor` present) is refused.
9. The canonical digest masks only the value of `page.continuation_token`,
   because that token is bound to the principal that issued it. Whether a
   non-empty token was present stays in the digest as a marker, so a continuing
   page and an exhausted page digest differently while two principals' non-empty
   tokens digest the same. Every other field, including the other `page`
   fields, stays in the digest, with drift tests. Pagination chains the token
   returned by the host's preceding successful call, not the owner's token,
   through a private, bounded handoff that is removed on every path. Token
   values do not enter observations, qualification records, durable logs or
   final output.
10. Runtime cleanup is verified, not silent. A root that cannot be removed, or
    that still exists after a deletion reports a vanished nested entry, fails
    the run as `cleanup_incomplete` before any pass record is written.
    A supplied `--runtime-root` must first prove the harness created it: an
    owner-only `ovmcp-real-` directory directly under `/tmp`, not a symlink,
    holding the owner-only bootstrap receipt. A root that cannot is refused as
    `entrypoint_unresolved` and never deleted. After that proof, preflight, the
    candidate reload, the schema digest, and the candidate-runtime and receipt
    validation are all inside the same guaranteed cleanup boundary. Teardown
    gives managed and replacement Core process groups bounded TERM then KILL
    escalation, and the complete group must disappear. The deliberate I-6/I-7
    restart is a crash instead: SIGKILL goes to the whole group with no TERM,
    the child must die of that signal, and a group that survives fails closed
    and is retained. The installed authoring journey signals a descriptor-named
    Core only after its pid, start time and boot id match the system's evidence.
    A failure path keeps its original reason and reports a cleanup failure on
    stderr. A SIGKILL of the harness itself cannot run any of this: the runtime
    root and any Core process it started then survive until removed by hand.
    That limit is documented, not worked around.
11. The proxy bounds every inbound and outbound frame to 1 MiB before it parses
    or forwards it. It stops and reaps the child on every failure path, and an
    observation failure exits `5`, with no frame forwarded. The proxy also
    keeps the child's input open until pending requests are answered, because
    an MCP server that reads end-of-input can exit before it writes an answer.
    After the configured interruption response is withheld, a synchronized
    terminal seal prevents any later host request or queued child response from
    being forwarded. Without the input drain, the excluded-name probe lost
    responses.

The qualification record and validator now also enforce the exact frozen
macOS baseline (27.0 build 26A428, arm64), rather than accepting any
well-formed macOS version/build string.

Checks at that snapshot ran against the installed console scripts in this
worktree (not host-driven): the authoring journey passed end to end, and the
restricted probe (62 names), the authoring probe (57 names) and the restricted
decision refusal with its owner observation all behaved as the harness then
required. These checks predate the final corrections and are not frozen-candidate
evidence. The authoring run then reported `mcp` 2.3.0 in this development venv, not
the reviewed `2.0.0` pin. Real-host records for Claude Code and Codex CLI are not produced
here: no exact-tip host run has happened, so no I row is green.

WP1 and WP2 were implemented at the 13/18 snapshot. The candidate builder then ran
both the restricted installed-wheel journey and a separate eighteen-tool authoring
journey, retained the authoring result, and validated it against a closed redaction
schema. That retained-record gate was implemented locally only. No record exists for
the live 14/35 candidate, so B-12 and H-5 through H-7 stay partial. The package
README, interoperability guide, manifest commentary, and Phase 7 traceability
record distinguished installed-wheel evidence from real-host evidence and described
the thirteen-tool restricted and eighteen-tool authoring profiles of that snapshot.

The first full preflight exposed an intermittent local-socket shutdown race:
closing an accepted socket from another thread did not reliably wake a blocked
read, and the Unix listener's wake method did not wake `accept`. The candidate
now shuts down the stream before close and uses a bounded loopback wake-up. The
focused lifecycle suite and 100 independent repetitions of the partial-client
case pass after the repair.

WP3's executable harness and closed record schema are implemented and covered
by deterministic tests. A historical Codex CLI 0.146.0 diagnostic run against
the clean `4ec9fa17c447c81e58056d99e703b587fcf0afa3` candidate reported I-1
through I-8 as passing under the record schema of that time. That record is
unauditable under the current closed schema, so the run closes no current I
row. It is diagnostic history, not acceptance at that or any later tip.

Claude Code 2.1.288 is installed and the operator session is authenticated, but
that subscription login is keychain-bound: copying `.credentials.json` into an
isolated home makes Claude's own `auth status` report logged out. Without a
portable credential, the harness fails this condition before Core starts with
`authentication_unavailable`. Using the operator's normal home/configuration is
not an acceptable workaround.

Portable token support is now implemented. For `--host claude-code`,
`--auth-file` names a token-only file holding the OAuth token produced by
`claude setup-token` (optionally ending in one LF). The harness injects that
value only as `CLAUDE_CODE_OAUTH_TOKEN` into the isolated Claude host
environment. `HOME` and `CLAUDE_CONFIG_DIR` stay isolated, the harness does not
copy, read or change the normal host configuration, and the portable token does
not rely on the operator's keychain login. The token value is held in memory
only for the authentication check and Claude host sessions and is never
persisted or recorded, and the MCP server process explicitly receives an empty
value for that variable. For `--host codex-cli`, `--auth-file` remains an
owner-only copy of `auth.json`. The interoperability guide states these
host-specific semantics.
The historical pre-review real-host harness and schema suite contained 358
tests, up from 219. It exercises the complete seventeen-step journey, including a real-host
attempt to dispatch an excluded sentinel, stable canonical replay and conflict
classification for all three mutations, stable paginated events, imported
evidence retrieval, revocation fail-closed behavior, owner observation after
revocation and Core health after every host exit.

The first expanded diagnostic at clean candidate `960ed703` failed closed after
revocation because the harness launched a fresh host process, which correctly
could not initialize after its installed credential was removed. The repaired
harness keeps the already-admitted real host session open, pauses its first
post-revocation request until revocation lands, and requires every later call in
that same session to fail closed. A disposable Codex CLI rerun against that
historical candidate passed the repaired journey; it remains diagnostic rather
than exact-tip acceptance. That single-session model is superseded: each refused
request now has its own paused, admitted session, with a fresh configure between
them, as described in the Gate D status of the standalone completion plan.

Independent review then found four acceptance weaknesses: the historical PR
#108 disposition inventory had stopped at GitHub's first 100 files, excluded
tool absence relied too heavily on model behavior, successful result hashing
could accept missing structured data, and the revocation classifier accepted a
generic `could not be called` phrase. The corrected disposition map now covers
all 121 paths. The harness now probes excluded dispatch deterministically,
rejects every malformed structured-result shape, and accepts revocation only
when the installed credential store emits its exact fixed sanitized missing
message. Generic timeout, transport, cancellation and not-callable failures no
longer satisfy I-8. A focused read-only review of the final two-file hardening
found no code defect; its three test-coverage observations were added. A full
exact-tip independent review is still required before candidate freeze.

Historical, for the 13/18 snapshot only: exact-tip Claude and Codex records were
produced for qualified runtime commit `0d8cf362d15b43077a744542974b6160c283e1dc`.
The operator authorized the fixed
qualification prompts and bounded service-derived results for both providers.
Claude Code 2.1.289 used the selected existing-login mode and the modern MCP
`server/discover` lifecycle; Codex CLI 0.146.0 used the isolated copied
credential and legacy lifecycle. Both records were schema-valid under the closed
schema of that time, bind the same 35-wheel closure and report every I-1 through
I-8 field as `true`. WP3 and the real-host part of WP5 were complete for that
snapshot only; the current schema bytes cannot validate those records, and the live
candidate needs new ones. WP6 remained open for the evidence-only closeout,
exact-head preflight, hosted checks and final review.

### Checks

Counts from before the repair round are historical. The post-repair counts are
given in the next block.

Historical review-closeout diff over `84b1510b`, the pushed reviewed PID-reuse
teardown repair: the three named focused files pass with 562 tests. The Gate A
review baseline was 515, the completed review repair added 31 regressions (546),
the final-review repair added seven more (553), and the teardown correction adds
nine regressions proving that a reaped child's reused numeric PID is never
signalled without complete identity proof while an absent PID still permits
known-group cleanup. Ruff, strict mypy on both qualification scripts and
`git diff --check` are clean. The complete Phase 2 suite passes after the
teardown correction with 596 tests and four expected platform-specific skips,
including the shared-launcher ownership regression.
A live installed authoring journey passed before the final local corrections in
this round; it must be rerun from the frozen candidate. The broader MCP, CLI,
package-qualification and service-conformance gate now passes 2,668 tests after
the teardown correction. The 1,902-test result covered only the MCP package, the
CLI package and the three focused files and was not that four-directory gate;
the earlier 2,659, 2,652, 2,645 and 1,796 results are historical.

Earlier results follow. They are historical and none is current candidate
evidence:

- historical, superseded: an earlier installed candidate build including
  restricted, authoring, and lifecycle journeys passed; it predates the current
  corrections, and the frozen candidate must rebuild and rerun it;
- authoring record positive and negative schema tests: pass;
- historical, superseded: focused Phase 8 package/traceability tests: 120
  passed;
- local transport lifecycle suite: 36 passed;
- partial-client shutdown stress: 100/100 passed;
- historical, superseded: full `PYTHON=.venv/bin/python ./scripts/preflight`:
  pass, including 28,296 Python tests, 23 benchmark tests, Ruff, strict mypy,
  all five wheel builds and isolated installs, and 59 Swift tests; final-tip
  rerun pending;
- historical, superseded: real-host harness/schema focused suite: 358 focused
  tests, with targeted Ruff, strict mypy, schema validation and diff hygiene
  passing;
- historical, superseded: combined MCP, CLI, authoring traceability and
  real-host harness gate: 1,796 passed at the preceding uncommitted checkpoint;
  the 1,902-test MCP package, CLI package and three-file result is not the
  four-directory gate;
- historical, diagnostic only: the Codex CLI real-host journey at clean
  `4ec9fa17` reported a pass, but its record is unauditable under the current
  closed schema and closes no current I row. The repaired same-session journey
  was also reported as passing against historical candidate `960ed703`. Neither
  is exact-tip acceptance;
- Claude Code isolated authentication preflight: correctly fails closed as
  `authentication_unavailable`.

### Lessons Learned

Historical exact-tip evidence and current release-candidate evidence must be
recorded separately; a previously green SDK pin does not qualify a later commit.
An isolated Git worktree also needs its own editable virtual environment: using
another checkout's editable environment makes source-versus-wheel comparison
read the wrong branch. Local IPC shutdown must actively wake both a blocked
frame read and a blocked accept rather than race a timeout boundary.
Core's exclusive database ownership also applies to independent qualification:
durable SQLite inspection must occur across a real Core shutdown, followed by
a fresh service for host and owner observations. Host subscription credentials
may be keychain-bound even when a credential file exists, so presence is not
authentication proof; the isolated host must verify its own login before Core
starts. Exact-tip host records cannot be committed into the commit they name,
so acceptance must attach them externally to the frozen revision. Revocation
evidence must distinguish the exact installed-credential-missing outcome from
generic client failure; otherwise a timeout or transport failure can look like
a security success. GitHub PR file listings are paginated and must be compared
with the full base-to-head Git diff before a disposition map is accepted.

### Improvements Needed

Keep the separate Windows named-pipe hardening follow-up from section 9. The
delegation workflow also needs a single-writer qualification freeze: concurrent
background lanes repeatedly committed and pushed while an exact-tip candidate
was being prepared. A connector/process follow-up should serialize
same-worktree writers, prohibit autonomous commit/push while a candidate is
frozen, and use renewable monitor leases without spawning a second writer.

### Next Step

Historical next step at `0d8cf362`: commit the two closed redacted records and
this evidence-only reconciliation, prove the diff from `0d8cf362` contains no
executable or acceptance-rule change, then run full preflight, push PR #167 and
obtain fresh hosted checks.

Live next step for the manifest 2.8 candidate (fourteen restricted, thirty-five
authoring): freeze a clean exact tip, build and install its release-form wheels,
run the restricted and authoring installed-wheel journeys against them, run both
real hosts against that same tip to retain new records bound to the current
harness and schema digests, update traceability only from those records, then run
exact-head preflight and obtain fresh hosted checks. Merge remains blocked until
the user explicitly authorizes it and the hosted checks are green at the latest
tip.
