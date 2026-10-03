# OmniVia Core MCP authoring Phase 8 completion plan

**Date:** 2026-10-03
**Status:** In progress
**Owner:** Codex (orchestration, review, acceptance); Claude Code (bounded implementation)
**Target repository:** `omnivia-core`
**Working branch:** `codex/core-mcp-authoring-phase8-closeout`
**Implementation checkpoint:** `6ad05f2d525770992b3f05b88fbfdc7bb0007d51`

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
preflight. That historical evidence closes H-5 and H-7 for that accepted
revision, but the current release candidate must still be retested at one exact
commit because the repository has changed since the merge.

The remaining functional and release gates are:

| Gate | Required outcome |
|---|---|
| H-6 | An installed-wheel journey exercises the eighteen-tool authoring profile, not only the restricted profile. |
| B-12 | A retained qualification record contains only the approved redacted fields. |
| I-1 | Install the exact release artifact on the supported macOS qualification account/environment. |
| I-2 | Configure restricted and authoring profiles using native Claude Code and Codex CLI settings. |
| I-3 | Prove initialization and exact tool discovery under each real host. |
| I-4 | Run the empty-workspace authoring journey under each real host. |
| I-5 | Run the staged-import and job-observation journey under each real host. |
| I-6 | Prove same-key recovery after an intentionally interrupted response. |
| I-7 | Prove protocol-only stdout, host restart, Core service restart, and continued observation. |
| I-8 | Revoke authoring and prove writes fail closed under the documented restart model. |

## 4. Frozen qualification baseline

At execution start the approved replacement baseline is:

| Component | Qualification value |
|---|---|
| Claude Code | `2.1.288` |
| Codex CLI | `0.146.0` |
| macOS | `27.0` build `26A428`, arm64 |
| MCP SDK | `mcp==2.0.0`, `mcp-types==2.0.0` from `scripts/mcp-wheelhouse-constraints.txt` |

The closeout record must replace the release-candidate base above with the
final tested commit and include the artifact digests. Any host or SDK change
requires repeating the affected qualification rather than carrying evidence
forward by assumption.

## 5. Work packages

### WP1 — Repair documentation and inventory drift

1. Update the MCP package README to describe the restricted thirteen-tool and
   authoring eighteen-tool profiles, including the five authoring additions.
2. Update the interoperability guide so installed-wheel evidence and real-host
   evidence are explicitly separate.
3. Correct the manifest comment from nineteen to eighteen tools.
4. Update the traceability record with PR #107's accepted merge and historical
   H-5/H-7 evidence without claiming that the current candidate has passed.

### WP2 — Add installed-wheel authoring qualification

Add a dedicated authoring qualification journey or a clearly separated
authoring mode beside `scripts/run-standard-journey.py`. It must run only from
installed wheels in an isolated installation and must prove:

1. exact eighteen-tool discovery;
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

WP1 and WP2 are implemented. The candidate builder now runs both the restricted
installed-wheel journey and a separate eighteen-tool authoring journey, retains
the authoring result, and validates it against a closed redaction schema. The
package README, interoperability guide, manifest commentary, and Phase 7
traceability record now distinguish installed-wheel evidence from real-host
evidence and describe the thirteen-tool restricted and eighteen-tool authoring
profiles.

The first full preflight exposed an intermittent local-socket shutdown race:
closing an accepted socket from another thread did not reliably wake a blocked
read, and the Unix listener's wake method did not wake `accept`. The candidate
now shuts down the stream before close and uses a bounded loopback wake-up. The
focused lifecycle suite and 100 independent repetitions of the partial-client
case pass after the repair.

WP3's executable harness and closed record schema are implemented and covered
by deterministic tests. Against the clean `f576ef3d` diagnostic candidate,
Codex CLI 0.146.0 completed I-1 through I-8: both inventories, authoring,
import observation, response interruption and replay, restart, protocol-only
stdout, and live revocation all passed. This is diagnostic evidence for the
harness, not final exact-tip acceptance.

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
The focused real-host harness and schema suite now contains 219 tests, up from
194.

No Claude real-host record exists yet, so Claude Code has not passed I-1
through I-8, and no I row is green. WP3 remains open for the final Claude record
and for rerunning both hosts at a new frozen final tip. The real-host part of
WP5 and final WP6 closeout also remain open. This documentation edit creates a
new candidate commit, so the current PR tip is not treated as exact after it.
Both hosts must be rerun at the new frozen tip.

### Checks

- installed candidate build including restricted, authoring, and lifecycle
  journeys: pass;
- authoring record positive and negative schema tests: pass;
- focused Phase 8 package/traceability tests: 120 passed;
- local transport lifecycle suite: 36 passed;
- partial-client shutdown stress: 100/100 passed;
- full `PYTHON=.venv/bin/python ./scripts/preflight`: pass, including 28,296
  Python tests, 23 benchmark tests, Ruff, strict mypy, all five wheel builds and
  isolated installs, and 59 Swift tests.
- real-host harness/schema focused suite: 219 focused tests (earlier checkpoint:
  194 passed, with targeted Ruff and strict mypy passing);
- Codex CLI diagnostic real-host journey at `f576ef3d`: pass;
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
so acceptance must attach them externally to the frozen revision.

### Improvements Needed

Portable token support is implemented. The remaining Claude action is external:
provision the token-only file outside the repository, then rerun both hosts at
the new frozen tip. Keep the separate Windows named-pipe hardening follow-up
from section 9.

### Next Step

Provision the Claude token-only file outside the repository, generated from
`claude setup-token` and owner-only. Then build one clean exact candidate at the
new frozen tip, rerun both Codex CLI and Claude Code lanes, retain the closed
redacted records outside the source tree, run full preflight, update PR #167,
and obtain fresh hosted checks. Merge remains blocked until the user explicitly
authorizes it and the hosted checks are green at the latest tip.
