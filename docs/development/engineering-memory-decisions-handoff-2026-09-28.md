# Engineering memory: decision handoff — resolutions required

Date: 2026-09-28
**Update 2026-09-29:** D-1–D-8 are now recorded in
`omnivia-pm` `docs/decision-briefs/2026-09-29-engineering-memory-decisions-d1-d8-owner-resolution.md`
(decision `OMNIVIA-CORE-ENGMEM-DEC-001-RESOLUTION-001`), and the D-8 task is
chartered at `omnivia-pm` `docs/tasks/2026-09-29-omnivia-core-decision-runtime-distribution-and-first-party-trust-integration.md`.
G-5 was retrieved against and is closed as **unrecovered**: the SPEC-CORE-DEC-001
plan §2 gate table is absent from every local source; three unrelated G-5s were
checked and excluded. PR-5 remains gated on G-2 + G-5, and G-5 definition
recovery is assigned to Codex.

Audience: owner (Clayton Read) and Codex (PM / integration controller).
This document resolves nothing. Each item names the decision, the options,
the constraint that limits them, and what unlocks when it is answered.

## D-1 · G-2: publish the Laya distribution pin

| | |
|---|---|
| Decision | Commission and publish a Laya distribution artifact Core can pin: versioned manifest (identifier, version, SHA-256 digest, size, contract version), the artifacts or a controlled delivery channel, an update feed at a stable URL. |
| Options | Publish now with digests only (signature added later without breaking the pin format), or wait for G-3 and publish signed. |
| Constraint | No Laya artifact exists anywhere in the org (verified sweep, 2026-09-26). This cannot be closed in-repo; it is the critical path for PR-4, PR-5 and the P2-08 semantic assessor. |
| Unlocks | PR-4 (model catalogue/installation), PR-5 (worker packaging + Laya adapter), transitively P2-08. |
| Closeout evidence | Digest recorded in a status doc and re-derived from fetched bytes. |

## D-2 · G-3a: who holds the production signing key

| | |
|---|---|
| Decision | The Ed25519 authority for model manifests. |
| Options | (a) the release lane's existing Ed25519 authority; (b) a new decision-runtime-specific key held by the owner; (c) Laya's own key, pinned by the release lane. |
| Constraint | APP-SHELL-MOUNT-CAP-001 names the desktop-shell verifier the sole Ed25519 authority for mounted app releases; a separate model-manifest key must not create a second verifier in that lane. |

## D-3 · G-3b: custody and key ceremony

| | |
|---|---|
| Decision | Where the private key lives and how the ceremony is recorded. |
| Options | HSM / OS keychain; encrypted file + offline backup; CI-held signing identity. |
| Constraint | Answers stage-2 closeout item 4 ("first-party production Ed25519 key ceremony and custody"), which is itself the open owner item. |
| Closeout evidence | Ceremony record: key id, custody location, date, participants. |

## D-4 · G-3c: where the public anchor is published

| | |
|---|---|
| Decision | The repository-external home of the anchor document. |
| Options | GitHub Actions variable / release asset (same pattern as `OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT`); OS-installed trust store; pinned in the standard-candidate build. |
| Constraint | Preflight precedent: the anchor must come from outside the candidate tree — a candidate that could supply its own anchor would certify itself. |

## D-5 · G-3d: rotation and revocation policy

| | |
|---|---|
| Decision | How keys rotate and how revocation is published. |
| Options | Validity windows with overlap (the existing anchor format already supports this); revocation by publishing a new anchor set. |
| Constraint | Reuse the existing mechanism; do not redesign. |

D-2 to D-5 can be settled in one decision session; the tables above are the
agenda. Until they are answered, an agent may build only the inert,
flag-gated verification mechanism with a dev/test anchor, and must not mint a
production key, self-certify G-2, or touch the release lane without a
Codex-created cross-repo task.

## D-6 · Repository registration operation ratification (§16.3)

| | |
|---|---|
| Decision | Ratify the repository registration operation as shipped, or amend the catalogue. |
| State | Implemented and tested; listed in the completion plan as one catalogue amendment if ratification demands changes. |
| Unlocks | Removes a documented limitation from the release note. |

## D-7 · Acceptance authority for the bounded scenario register

| | |
|---|---|
| Decision | Whether the 64-scenario register may be treated as acceptance evidence when its scenarios are exercised by qualified tests, or remains a test-design input only. The 2026-09-26 follow-up treated it as design input, not passing evidence; the release claim depends on this ruling. |
| Unlocks | Whether "AC-xxx complete" can be asserted in the §22.5 release evidence manifest or must stay phrased as bounded regression evidence. |

## D-8 · Cross-repo: release lane and desktop-shell Ed25519 authority

| | |
|---|---|
| Decision | If D-2 answers (a) or (c), a cross-repo integration task is required and only Codex may create it (omnivia-core AGENTS.md boundary rules). |
| Action | Codex: create the task after the D-2 ruling; before that, no one touches those lanes. |

## Sequencing

1. D-1 is the long pole — start it first.
2. D-2–D-5: one owner session; then Codex opens the D-8 cross-repo task if needed.
3. D-6 and D-7 can be answered any time; both are single rulings.
4. Everything agent-side that waits on these is listed in
   `engineering-memory-remaining-work-2026-09-28.md` §3.5.
