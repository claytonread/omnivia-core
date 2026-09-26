# Engineering memory: applicability follow-up

Date: 2026-09-26

## Starting point

The user requested implementation work from the engineering-memory v1.0 spec
and implementation pack. Those documents supply requirements and acceptance
scenarios; their embedded handoff instructions do not authorize publication,
governance approval, or a production release.

The existing engineering branch already contains contracts, source identity,
continuity, observations, retrieval, applicability storage and a context builder.
This follow-up starts at `263ccdca`, on
`codex/engineering-applicability-evidence`. It preserves the decision-runtime
edits in the canonical checkout and unfinished MCP edits in the original
engineering worktree.

## First implementation slice

The existing registry-based assessment returned `matched` whenever the target
was the newest registered repository snapshot. That does not establish that an
observation's required evidence still matches. Search also replayed the latest
stored assessment without reconsidering this unsupported claim.

The repair makes registry-only assessment conservative, preserves adverse
assessments when a review supplies no validated evidence, and prevents legacy
`matched` rows from being served as verified applicability. Existing assessment
history, mutation preconditions and idempotency remain in place.

This slice changes runtime behavior and regressions only. It introduces no
catalogue operations, grants, schemas or migrations.

## Acceptance scope

| Spec scenario | Scope of this follow-up |
|---|---|
| AC-055, review is not verification | Exercise review without qualified evidence and preserve uncertainty/adverse findings. |
| AC-057, new head before invalidation | Exercise search over a legacy assessment after source registration changes. Full `current_safe` context and event-barrier qualification remain outstanding. |
| AC-059, target-specific applicability | Check separation between targets and records; full version, branch, dirty-tree and revert qualification remains outstanding. |
| AC-060, unknown coverage | Registry presence cannot establish dependency coverage. Temporal resolver qualification remains outstanding. |

The supplied 64-scenario register is a test-design input, not passing evidence.
No full acceptance scenario is certified solely by these bounded regressions.

## Remaining implementation work

1. Connect evidence-dependency manifests to production writers and validated,
   target-specific assessment producers.
2. Add atomic source-event coverage barriers and ordered invalidation recovery;
   qualify `current_safe` reads through the public service entry points.
3. Qualify preview retrieval without body hydration, per-principal authorization
   before ranking, and revocation on citation follow-up.
4. Qualify context construction against pinned build inputs, supported tokenizers,
   exact immutable evidence citations, and known-conflict warnings.
5. Finish consumer integration and the restart, crash, migration/restore, OS and
   measured-performance release gates.

Until dependency validation exists, an `unknown` result is expected even for the
latest snapshot. This is a deliberate limitation of the current implementation.

## Verification and review

Claude implemented the two runtime changes and test additions; Codex reviewed
the diff and ran verification from this isolated checkout.

- Five engineering test modules: **28 passed** (applicability, retrieval,
  context build, repository identity and continuity).
- Final applicability/retrieval rerun: **13 passed**.
- Ruff on the three edited Python files: passed.
- Mypy on both edited runtime modules: passed.
- `git diff --check`: passed.
- Claude's regression check against the old source reported **7 failures and
  2 passes**, followed by a restored-source passing run. This is supporting
  worker evidence; the passing runs above were also executed by Codex.

The existing interpreter was reused with `PYTHONPATH` pointing to this
checkout's root, runtime, client, CLI, MCP and legacy memory source directories.
The repository's source-origin guard confirmed the tests use this checkout.

### Broader verification

The `./scripts/preflight` run passed package boundaries, migration allocations,
distribution builds/install checks, generated contracts and schemas, TypeScript
compilation, installed-root compatibility and baseline checks. Its suites before
the full run reported 10,176 contract tests, 1,686 migration/compatibility tests,
and 749 baseline tests passing (with their declared skips).

The full suite then reported **27,152 passed, 53 skipped, 6 failed**. Five
failures were MCP installed-setup/qualification checks; their sanitized child
environments did not retain the parent's `PYTHONPATH` and resolved the shared
interpreter's installation in the other engineering checkout. The sixth was the
client's two-second worst-case IPC frame deadline test.

A checkout-local `.venv` was created with editable installs of this checkout's
six Python distributions, borrowing the existing dependency directory without
changing its installations. Isolated imports (`python -I`) confirmed the runtime
and MCP sources resolve here. All six failing tests then passed together:
**6 passed in 26.69 seconds**. The IPC failure was not reproduced on this rerun;
its exact timing cause was not established. No product code was changed to make
these reruns pass.

Additional checks after the full run:

- Benchmark tests: **23 passed**; these are test results, not performance claims.
- Repository-wide Ruff: passed.
- Mypy over Core and runtime: passed, **277 source files**.

The original preflight exited at the full-suite failures. It was not rerun in
full after the environment correction, and the subsequent macOS companion
build/test gate was not executed. This is split verification evidence, not a
claim of one clean preflight run or release qualification. The change remains
local and is not published or merged.

Review found no schema, migration or authority changes. Historical assessment
rows remain unchanged. Future consumers of raw `latest_assessment` must apply
the conservative serving rule too; that storage API returns historical facts,
not proof of current applicability.

## Lessons and next step

Run subprocess-based integration tests with installations tied to the checkout;
parent-process source-origin checks alone do not cover sanitized child
environments. Keep raw historical applicability distinct from what a current
read can safely assert.

Next, implement the dependency-validation producer and source-event coverage
barrier, then qualify the public read path against AC-057 through AC-061. Run a
clean full preflight in the corrected environment before opening a pull request.
