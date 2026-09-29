# Engineering memory: applicability follow-up verification closure

Date: 2026-09-28

Follows `engineering-memory-applicability-followup-2026-09-26.md`. That record
left three verification items open: a clean full preflight after the MCP
environment correction, the macOS companion gate, and the unexplained IPC
frame-deadline failure. All three are now closed. No product code changed.

## Clean preflight

`./scripts/preflight` was rerun in the corrected environment on
`codex/engineering-acceptance-edge-cases` @ `88100e53` and passed completely:

- Full suite: **27,806 passed, 54 skipped** in 23m31s.
- Benchmark tests: 23 passed (test results, not performance claims).
- Repository-wide Ruff: passed.
- Strict Mypy: passed, 351 source files.
- macOS status menu companion: built and **59 tests passed**.
- Final line: `Core preflight passed. Open the pull request.`

The earlier five MCP installed-setup failures are gone (the checkout-local
`.venv` editable installs hold), and the IPC deadline test passed.

## Environment rule discovered on the way

The first corrected-environment run still failed five managed-start and
lifecycle tests with `unmet preconditions: ['exact_schema_and_trigger_fingerprint']`.

Root cause, verified line by line: the migrated workspace database matched the
canonical fingerprint exactly, but `locate_service()` in the shared client
prefers `PATH` over the script beside `sys.executable` (deliberate, to honour a
shadowed build). Invoking `.venv/bin/python` by full path leaves `.venv/bin`
off `PATH`, so `shutil.which` resolved `omnivia-core-service` from an unrelated
editable installation on this machine. That foreign service served a different
migration set, and readiness correctly refused the fingerprint. The readiness
diagnostic is also slightly misleading: `assert_guards_intact` and
`verify_fingerprint` share one `try`, so a guard failure reports as a
fingerprint failure.

Fix: run preflight with `.venv/bin` first on `PATH`
(`export PATH="$PWD/.venv/bin:$PATH"`). All five tests then pass individually
and the full preflight is clean.

Suggested follow-ups (not done here, product-surface decisions):

1. Test helpers that spawn the CLI could pin the subprocess `PATH` to the
   checkout's `.venv/bin`, which is the general form of the 2026-09-26 lesson.
2. `runner.py`'s readiness `try` could separate guard integrity from the
   fingerprint oracle so each unmet precondition names itself.

## IPC deadline failure

The client's two-second worst-case IPC frame deadline test failed once during
the 2026-09-26 run and never since. Source review found the mechanism: the
socket timeout is the remaining whole-call deadline, and connect, `sendall` of
the largest frame and the read all share one 2.0-second budget with no margin.
Under extreme machine contention a descheduled writer hits `TimeoutError`
mid-frame. It is not reproduced: 8/8 passes while a full preflight ran
concurrently on the same machine. The test pins a product requirement, so its
budget was not changed; treat it as a rare contention flake, rerun once before
investigating, same policy as the hosted journey's `initialize` stage.

## Status

The 2026-09-26 record's local verification evidence is now one clean preflight
plus the macOS gate, in the corrected environment, with no product changes.
The branch has since advanced (source-coverage validation merged as PR #129,
follow-on slices in flight on other branches). This record does not qualify
release scenarios and does not publish anything.
