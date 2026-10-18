# Update-mechanism qualification record (spec v0.4 §15; A01–A21)

- Date: 2026-10-01
- Scope: the v0.4 shared-update implementation as merged through #158 (check)
  and #161 (update + publication). Per §15: which targets, architectures and
  installation profiles actually ran — with no extrapolation from one to
  another, and fixtures never reported as release qualification.

## Executed evidence (tests actually run, files actually fetched)

| ID | Scenario | Evidence | Status |
|---|---|---|---|
| A01 | equal / newer / older / paused ordering | `test_service_update_check.py`: up_to_date, update_available, ahead_of_channel, no_release; dotted-integer vs string sorting | **executed** |
| A02 | network / malformed / trust / unsupported failures | same file: unreachable fetch, malformed document, unknown schema, unknown member, non-HTTPS release URL, partial package map — all `check_failed`, never "up to date" | **executed** |
| A04 | download/checksum failure → no stop | `test_service_update_apply.py::test_a_checksum_mismatch_refuses_before_any_stop` — corrupted bundle refused; no stop command recorded | **executed** |
| A05 | two Core requests, aliased paths | `test_a_second_coordinator_receives_update_already_running` (kernel flock, same lock file) — the **cross-entry-point half (Platform's Update Core)** is not built (U4) | **partial** |
| A06 | updater crash during hand-off | the flock is kernel-held: `test_a_released_lock_is_acquirable_again` pins reacquisition after release; a mid-install crash E2E is not executed | **partial** |
| A08 | successful installation with readiness | `test_the_worker_runs_the_full_phase_machine_and_writes_each_transition` — stop/install/restart/verify against a recorder runner; the real-relaunch E2E awaits a second release | **fixture-tested** |
| A09 | restart/readiness failure semantics | restart_failed and verify_failed tests; `restart_required` vs `failed` distinction enforced in the worker | **executed (fixture)** |
| A10 | Core-only install, no Node/Git | the standard candidate's offline journey + the pin receipt ran standalone (no Node, no Git in the update path); the update-path E2E on a second release is pending | **partial** |
| A18 | source / externally managed / remote fallbacks | editable checkout → `unsupported_install`; partial install → `unsupported_install`; externally managed and remote targets refuse through the same gate (code path), not executed against real ones | **executed (subset)** |
| A20 | shared publication workflow | `publish-update-channel.py` prepare mode executed against the real built candidate (release manifest validated, bundle digest computed); the clobber/downgrade refusals are implemented in `--apply` | **partial** |
| A21 | no live network or scheduler in tests | every update test uses injected fetch/installed/probe seams; 918 CLI tests pass | **executed** |

## Fixture-tested but awaiting real releases or desktop lanes

| ID | Scenario | What it needs |
|---|---|---|
| A03 | approval binding across a feed change | a real second release so the feed can genuinely change during an install |
| A06 full / A07 full | crash and race E2E on a real installation | the same |
| A08 full | real relaunch + readiness on the user's machine | the same |
| A17 | independent per-product cadence | both desktop products releasing |

## Not started (their lanes are chartered, not built)

| ID | Scenario | Lane |
|---|---|---|
| A11 | Crok Pot update without OmniVia | Crok Pot (adapter chartered: `OMNIVIA-DESKTOP-UPDATE-ADAPTER-001`) |
| A12 | validly signed wrong-product artifact | needs D-2/D-4 signing first |
| A13, A14, A16 | desktop integration + isolation | Crok Pot / Platform adapters |
| A15 | Platform/Core compatibility reporting | Platform's compatibility rules |
| A19 | migration-sensitive release | the products' migration gates |

## The summary line

Three real executed lanes: **discovery honesty** (A01/A02/A21), **no-mutation-on-failure** (A04), and **fallback honesty** (A18 subset). Everything desktop is chartered and not built. Everything requiring a second real release is recorded as pending that release — per §15, this record is not release qualification, and no A-scenario is claimed beyond its executed evidence.
