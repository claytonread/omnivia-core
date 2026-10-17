# Update trust and staging integration record (P6/P10 update owner)

- Date: 2026-10-18
- Branch: `codex/core-tech-update-integration`
- Integration commit: `fe3304e3` ("Harden Core update trust and staging"),
  cherry-picked with `-x` from `09f19f99` on `codex/core-update-hardening-v1`
- Target baseline: `93e5d014` ("chore(core): reconcile the semantic lane with the
  Waves 1-4 head")
- Scope: the update contract, channel trust, journal/state machine, safe archive
  staging, publication tooling and client check/apply surfaces. Nothing native,
  no Settings UI, no semantic/analyser/graph follow-on.

This record uses the verification levels of
`OMNIVIA-CORE-TECH-SPECS-IMPLEMENTATION-PLAN-001` §10. It is **not** release
qualification. The companion record
`2026-10-01-update-qualification-record.md` still governs the A01–A21 scenario
ledger, and nothing below raises any A-scenario's status.

## 1. What the integration actually was

The source branch's parent `006caf77` is a direct ancestor of the target
baseline `93e5d014`, 291 commits behind it. The slice therefore replayed as one
linear cherry-pick rather than a merge, and the "old baseline" risk reduced to
the files both sides had touched.

Of the 19 files the source commit modified, 16 were byte-identical between
`006caf77` and `93e5d014`. Three had diverged:

| File | Baseline change | Source change | Resolution |
|---|---|---|---|
| `README.md` | operation catalogue 69 → 86 (§ "application operations") | `update-trust` extra prose (§ "package boundaries") | disjoint sections; both retained |
| `packages/omnivia-core-cli/.../main.py` | `--require-running`, stdin input, `--payload-file` staging | `_run_update` fetch routed to `default_fetch_bundle` | disjoint regions; both retained |
| `packages/omnivia-core-client/tests/test_package_isolation.py` | `local_ipc.py` added to the `hashlib` shared set | `update_trust.py` module pin, `base64`/`cryptography` pins | disjoint hunks; both retained |

The cherry-pick reported **zero conflicts**, and
`git diff --numstat 93e5d014 fe3304e3` is byte-identical to
`git diff --numstat 006caf77 09f19f99` (85 files, +8722, -274). Equal numstats
on both sides of a 291-commit gap is the evidence that nothing stale was
imported and nothing newer was reverted; the three rows above were then
confirmed by reading the merged files, not inferred from the numstat.

### Deliberate omissions

None. Every hunk of `09f19f99` was applicable. The source commit changed no
dependency version and no CI pin, so there was nothing stale to decline: its
edits to `.github/workflows/core-acceptance.yml`, `scripts/preflight`,
`pyproject.toml` and `scripts/check-package-boundaries.py` are purely additive —
one `--check` step added in the two places that must stay in step, two
`force-include` rows, and comments.

### Preserved authorities

- **Migration catalogue untouched.** The diff contains no migration path and no
  `contracts/migrations/v1/allocations.json` change. `0069_document_intake`,
  `0070_app_settings`, `0071_capture_processing_settings`,
  `0072_capture_processing_resources` and `0073_semantic_vector_projection`
  stand as the baseline left them, and
  `scripts/check-migration-allocations.py` passes.
- **One update authority.** The slice adds no daemon, no settings store and no
  second updater. `update_trust.py` is a new module inside the existing
  `omnivia-core-client` distribution, and `tests/test_package_isolation.py`
  pins it as the only module in that package permitted to name a cryptography
  library — the same shape as the existing `socket` and HTTP pins.
- **Portable/runtime boundary.** `contracts/update/v1` ships as
  `omnivia_core.update_contract.v1.resources` through the root distribution's
  `force-include`, which is now exactly ten canonical directories, matching the
  exact-equality assertion in
  `tests/test_package_boundaries.py::test_core_wheel_force_includes_exactly_the_canonical_contract_resources`.
  Reading the corpus requires no verifier and no cryptography library.
- **`cryptography` is an optional extra, not a dependency.** The client still
  declares exactly `omnivia-core>=0.1.0,<0.2.0`, so the three places that pin
  that single edge are unchanged. The extra is reachable in practice because
  `omnivia-core-runtime` requires `cryptography>=50.0.0,<51` outright and the
  update path refuses to run unless all five first-party distributions are
  installed.

## 2. Contract and component evidence (L1/L3)

All commands run from this worktree on macOS 27.0.0 (Darwin, arm64) against a
worktree-local `.venv` provisioned per the `scripts/preflight` header:
CPython 3.11.15, ruff 0.16.1, mypy 2.3.0, pytest 9.1.1, cryptography 50.0.2.

| Level | Check | Command | Result |
|---|---|---|---|
| L1 | update corpus drift | `python scripts/generate-update-contract.py --check` | `update contract corpus check passed (57 files).` |
| L1 | whitespace | `git diff --check 93e5d014 HEAD` | clean |
| L1 | contract conformance | `pytest tests/update_contract/test_update_conformance.py` | 70 passed |
| L1 | publication tooling | `pytest tests/update_contract/test_update_publication.py` | 14 passed |
| L1 | packaged resources | `pytest tests/update_contract/test_update_resources.py` | 26 passed |
| L3 | client check surface | `pytest .../test_service_update_check.py` | 42 passed |
| L3 | client apply surface | `pytest .../test_service_update_apply.py` | 20 passed |
| L1 | package topology | `pytest tests/test_package_boundaries.py` + `scripts/check-package-boundaries.py` | passed |
| L1 | gate/preflight parity | `pytest tests/test_core_acceptance_workflow.py` | passed |
| L1 | import confinement | `pytest .../test_package_isolation.py` | passed |

The ten rows above were run together as one focused batch: **339 passed in
12.50s**. The full `omnivia-core-cli` and `omnivia-core-client` suites were then
run end to end: **2635 passed, 8 skipped, 4 failed** — see §4 for the four.

The published corpus is 1 schema plus 57 documents: 26 `envelopes`, 19
`archives`, 11 `states` and 1 `vectors`. The directories are *subjects*, not
verdicts, because each case states its own expected verdict. What the corpus
pins, and what the conformance suite executes against it:

- HTTPS-only release URLs; product, domain-prefixed product, and channel
  mismatch; unsupported schema version; unknown and missing envelope members
  refused symmetrically.
- Signing-key trust windows: unapproved, not-yet-valid, expired, retired, and
  two simultaneously approved keys, so rotation needs no flag day.
- Canonicalisation: non-canonical member order, non-canonical number spelling
  and duplicate member names, each refused before a signature is verified, so a
  signature cannot be lifted onto a re-spelled copy.
- Replayed sequence, downgrade recommendation and partial package map.
- Unsigned v1 refused under `PRODUCTION_TRUST_POLICY` and admitted only under
  an explicit development policy.
- Archive staging: absolute, backslash, parent-traversal, reserved-character and
  symlink members; duplicate members; member-count bound; expansion bound;
  members the inventory does not cover; inventory digest mismatch; inventory
  rows that escape the root; per-package wheel versions, foreign distributions
  and unparseable wheel names.
- Journal durability: truncated, non-object and unknown-phase journals, and the
  legal/illegal transition pairs including skipping `stopping` and leaving a
  terminal phase.

Offline and fail-closed behaviour is exercised through injected fetch/installed/
probe seams in every one of those tests; no test opens a network socket.

## 3. Packaged and release evidence (L5/L6)

**None, and none is claimed.** Specifically:

- No signed or notarised package was built, installed or activated in this lane.
  No observed activation, no real relaunch, no readiness check against a live
  installation.
- `PACKAGED_TRUSTED_KEYS` is `()` on purpose: no production channel-signing key
  exists, so production verification refuses every envelope with
  `channel_untrusted_key`. Production channel verification is therefore
  **unreachable by design** until a signing authority is bound — the same shape
  as the release-eligibility gate in the WP-14 signing records.
- Nothing in the repository builds `omnivia-core-standard-candidate.zip`, which
  `scripts/publish-update-channel.py` requires and the client fetches. The
  staging and verification paths are proven against generated bundles only.
- No durable last-accepted-sequence or version floor exists. `minimum_sequence`
  and `version_floor` are published and caller-supplied; replay protection is
  therefore only as durable as the caller.
- `OUTCOMES` still lists `restart_required`, which no writer in this slice
  produces.

`scripts/check-package-builds.sh` does supply one genuinely useful L3 fact: its
isolated venv installs `omnivia-core-client` **without** the `update-trust`
extra and then imports `omnivia_core_client.update_trust` and
`omnivia_core_client.updates`. That import succeeding is the proof the extra is
optional — a module-level `cryptography` import would resolve in a developer
environment and fail exactly there. It runs as a preflight step; see §5.

## 4. Pre-existing failures inherited from the baseline

None of these are in this diff, and none is caused by it. Each was reproduced on
an untouched `git archive 93e5d014` tree.

| Count | Where | Cause | Blocks |
|---|---|---|---|
| 4 | `packages/omnivia-core-cli/tests/test_lifecycle.py` | environmental: a stray `~/.local/bin/omnivia-core-service` symlink serving another checkout wins a PATH-first spawn, so the CLI reports `endpoint_unreachable` | local runs only; mitigated under preflight by putting `.venv/bin` first on PATH |
| 2 | `tests/test_packaged_runtime.py:705,717` | real: Ruff `SIM117` (nested `with`). Introduced at `32c2015d`, the Waves 1-4 head both this lane and the semantic lane branched from | the `Run Ruff` step, and so `Core acceptance` |
| 10 | `.../service/ooxml_extractors.py` (9), `.../service/document_extractors.py` (1) | real: strict-mypy `arg-type`, `valid-type`, `var-annotated`, `misc`, `return-value`, `dict-item` | the `Run strict mypy` step, and so `Core acceptance` |

The 12 Ruff/mypy findings are **gate-blocking and inherited**, not
environmental: ruff 0.16.1 and mypy 2.3.0 here are the exact versions
`services/omnivia-memory/pyproject.toml` pins for CI. Running strict mypy on the
borrowed main-checkout interpreter reports 19 rather than 10; the extra 9 are
artefacts of that interpreter missing `Pillow`, `pytesseract`, `pypdfium2` and
`pillow-heif`, which `omnivia-core-runtime` declares and the provisioned `.venv`
installs. The honest number is 10.

They belong to the document-intake vertical, not this one, and were left for a
packet that owns those files. This lane adds **zero** new Ruff or mypy findings:
with the catalogue error sets identical before and after, its four new source
modules (`update_contract/{__init__,v1/__init__,v1/resources}.py` and
`update_trust.py`) are clean under both tools.

## 5. Preflight

`./scripts/preflight` ordering matters for reading its result: `Run Ruff` and
`Run strict mypy` are the **last** two steps, after package boundaries, the
distribution build/install check, every contract drift check and the full
repository suite. The script is `set -euo pipefail`, so it stops at the first
failure.

The consequence is stated rather than worked around: **a clean preflight is not
obtainable on this branch**, because it must reach the two inherited steps in §4
and they fail on files this lane does not own. A run on this exact commit is
therefore expected to validate everything this slice touches and then stop at
`Run Ruff`. The run's exact command, log path and final exit code are reported
with the lane hand-back; nothing here is recorded as passed on the strength of
the steps that precede the failure.

## 6. Summary line

Contract and component evidence for the update seam is real and executed: 57
generated fixtures held equal to their generator, 110 contract tests and 62
client surface tests passing, the package topology and the portable/runtime
boundary intact, and the migration catalogue untouched. Everything packaged is
absent: no signing authority is bound, so production channel verification
refuses every envelope, and no candidate bundle is built anywhere in this
repository. Per §10 of the plan, this slice is `L3 component` and no higher.
