<!-- GENERATED FILE - DO NOT EDIT.
     Source of truth: qualification/wp15/qualification-ledger.v1.json
     Generator: scripts/check-wp15-qualification.py
     Regenerate: python scripts/check-wp15-qualification.py --write
     Verify:     python scripts/check-wp15-qualification.py
-->

# WP-15 acceptance and qualification status

Every behaviour gate in section 20 of `omnivia-core-macos-complete-technical-design-v2.0-2026-10-05.md`
(v2.0 (2026-10-05)), sections 20, 20.1, 20.2, 20.3, 20.4.

This document is generated from
`qualification/wp15/qualification-ledger.v1.json` and is verified against it
by `scripts/check-wp15-qualification.py`. It records what this checkout can
show and what it cannot. **It is not a claim that the release is qualified.**
64 of 130 gates have direct production-path coverage
of every condition they name; the rest carry the exact condition still
missing.

## Counts by status

| Status | Gates | Meaning |
|---|---|---|
| passed | 64 | Direct production-path coverage of every condition named, running unconditionally in this repository's validation. |
| failed | 0 | Evidence exists and shows the required result is not met. |
| blocked_external | 47 | The implementation is present, but the evidence class cannot be produced in this repository or its CI. |
| not_executed | 19 | The evidence class is obtainable in principle, but the required scope has not been demonstrated here. |
| not_implemented | 0 | The production path the gate names does not exist in this checkout. |

## Counts by family and status

| Family | Section | Gates | passed | failed | blocked_external | not_executed | not_implemented |
|---|---|---|---|---|---|---|---|
| AT | 20 | 50 | 25 | 0 | 19 | 6 | 0 |
| NAT | 20.1 | 17 | 0 | 0 | 17 | 0 | 0 |
| DOC | 20.2 | 19 | 11 | 0 | 1 | 7 | 0 |
| MON | 20.3 | 38 | 24 | 0 | 10 | 4 | 0 |
| END | 20.4 | 6 | 4 | 0 | 0 | 2 | 0 |
| **total** | | **130** | 64 | 0 | 47 | 19 | 0 |

## Counts by required evidence class

The evidence class is what the design requires before a gate can be called
met. Only the first two can be produced from this repository, so a gate in
any other class cannot be recorded as passed from a test run -- the checker
refuses that combination.

| Evidence class | Gates | Producible here |
|---|---|---|
| automated_repository_test | 79 | yes |
| repository_checker | 1 | yes |
| installed_signed_build | 39 | no |
| real_host_measurement | 2 | no |
| accessibility_review | 6 | no |
| external_fixture_corpus | 3 | no |

## Gates with direct coverage

Each of these has a test for every condition its required result names,
against the production code path, running unconditionally. The ledger records
each one's scope limits.

| Gate | Gate summary |
|---|---|
| AT-05 | Paste a note, then drop files. |
| AT-08 | An unsupported, oversized or mixed selection. |
| AT-09 | An exact UTF-8, BOM and line-ending fixture. |
| AT-10 | A pasted path, an embedded URL and instruction text. |
| AT-11 | A known eligible local target while offline. |
| AT-12 | A missing, revoked or incompatible target. |
| AT-13 | The principal or the target profile changes before the first send or during recovery, including a cached Share. |
| AT-18 | A crash after the Core commit but before the local receipt. |
| AT-19 | A timeout, an expired replay or an unknown admission. |
| AT-20 | A native queue capacity race across the Share and manual routes. |
| AT-21 | A symlink, a tampered manifest or a tampered payload. |
| AT-24 | A file changes during reading. |
| AT-27 | Locate is used during preparation. |
| AT-28 | Disconnect races a final multi-file publication. |
| AT-29 | Disconnect after durable publication, including before the first admission. |
| AT-30 | A disconnect acknowledgement is lost. |
| AT-31 | Two refreshes finish in reverse order. |
| AT-32 | A refresh fails after a previous success. |
| AT-33 | The same bytes, but the evidence has expired or been revoked. |
| AT-37 | One item fails before local publication, and separately a partial Core processing case. |
| AT-46 | Source and derived claims retrieval. |
| AT-47 | An earlier snapshot is admitted after a newer capture succeeds. |
| AT-48 | A descriptor expires with a known-no-effect outcome, versus an ambiguous admission. |
| AT-49 | An external access-owner call against a candidate or a provisional binding. |
| AT-50 | Evidence is retrievable while the requested linkage is pending. |
| DOC-02 | DOCX body and annotations. |
| DOC-03 | XLSX formulas. |
| DOC-04 | XLSX sparse and hidden content. |
| DOC-07 | HTML. |
| DOC-11 | PDF OCR conflicts. |
| DOC-12 | OCR confidence. |
| DOC-14 | Embedded document image profile. |
| DOC-15 | Resource adversaries. |
| DOC-17 | Reprocess races. |
| DOC-18 | Retrieval publication. |
| DOC-19 | Governance. |
| MON-01 | Create an individual watch. |
| MON-02 | Folder once, with recursion on and off. |
| MON-03 | New files appear during a one-off plan. |
| MON-04 | A one-off run of 1,000 or more files. |
| MON-06 | A new-and-changed baseline. |
| MON-08 | An edit during a baseline or a scan. |
| MON-09 | A burst of saves. |
| MON-10 | A crash after a checkpoint. |
| MON-11 | A crash before the cursor update. |
| MON-12 | Dropped or coalesced events. |
| MON-13 | The event UUID changes or is missing. |
| MON-18 | Symlink, alias, nested mount and path traversal. |
| MON-22 | Stop watching. |
| MON-23 | Source Disconnect versus folder Disconnect. |
| MON-24 | Pause, configuration change or disconnect at the final rename. |
| MON-25 | Core sees a tombstone before the first capture. |
| MON-26 | A principal or target change. |
| MON-29 | A byte or copy quota is reached. |
| MON-30 | A known failed, unsupported or zero-text digest. |
| MON-33 | Reverse job completion and a stale base. |
| MON-34 | An add or change burst threshold. |
| MON-35 | A disappearance threshold. |
| MON-36 | Scan and recursion limits. |
| MON-37 | An unusual review, then more changes. |
| END-01 | Add with Keep updated while the initial association or activation is interrupted. |
| END-03 | A new source capture races reprocessing of an older capture. |
| END-04 | Mixed ordinary files and multiple folder modes. |
| END-05 | Upgrade from v1 native access and the v2 mailbox, then downgrade. |

## What is structurally out of reach here

These are not gaps in test writing. Each needs something this repository does
not contain:

- **Signing and notarisation.** The packaging script emits `candidate_status: qualification_only`, `signature_status: unsigned` and `production_release_eligible: false`. No `codesign`, `spctl`, `notarytool` or `stapler` invocation exists, so macOS has never registered the Services entry, the Finder Quick Action or the Share extension.
- **Security-scoped bookmarks.** Every test substitutes a stub codec encoding the path as `Data(url.path.utf8)`. `SystemSecurityScopedBookmarkCodec` is referenced by no test, so grant persistence, rename resolution and replacement ambiguity are untested.
- **Peer code-signing verification.** Only the input-validation path is covered. Verifying a genuinely signed peer, and rejecting a forged claim, needs signing identities.
- **The two extensions.** `Extensions/` is outside the Swift test target, has no tests, and CI does not even typecheck it.
- **Accessibility, appearance and displays.** There is no VoiceOver, keyboard-reachability, snapshot, text-scaling or multi-display test anywhere.
- **Performance.** No p95 measurement exists for popover opening, publication, extraction or folder scanning, and no CPU, memory, wakeup or disk peak is recorded.
- **Intel and older macOS.** The package declares macOS 13, but nothing has run below the CI runner's version, and the build targets `arm64` only.
- **Real providers and volumes.** There is no iCloud, File Provider, network or removable-volume fixture, and CI mounts none.
- **Fixtures.** Every Office package is built in memory with `zipfile` and every image with PIL. No application-authored document and no HEIC file exists in the repository, and there is no fixture-hash manifest, which sections 11.15 and 22 require alongside results.
- **The pinned external binaries.** RTF extraction needs `pandoc 3.12.1` and OCR needs `tesseract` with the bundled English and orientation models. Both are now packaged: `scripts/build-core-companion-app.py` stages them, their relocated native library closure and their licence notices into the candidate and digest-pins every byte in the component-set manifest, and production resolves them from the verified installation payload by absolute path. What the repository still cannot do is make those bytes: neither engine is redistributed here, so neither CI workflow nor `scripts/preflight` can assemble a candidate, the real-engine cases skip where the host has no engine, and the one real offline run is the committed record behind `scripts/check-doc13-packaged-runtime.py`.
- **Companion to Core, end to end.** No test has a real companion talking to a real Core service. Swift tests stop at a fake delivery client; Python tests call the service directly.
- **Platform coexistence.** Platform does not exist in this repository, so its four states cannot be produced.

## Reading an entry

Each ledger entry carries the gate's exact id, its transcribed normative summary, the
capability and OS rows it applies to, the evidence class the design requires, the
repository artefacts offered as evidence with the exact condition each one
demonstrates, a status, the exact missing condition when the status is not `passed`,
the honest scope limits of whatever evidence is listed, and the production code the
gate binds to per section 22.

A non-`passed` gate may still cite evidence. That is partial coverage, recorded so the
remaining distance is visible; it is not a coverage claim. Conversely a `passed` gate
still carries limitations, because unit-level coverage of a behaviour is not the
installed, measured, receipted evidence sections 11.15, 17.4, 21.12 and 22 require for
release.
