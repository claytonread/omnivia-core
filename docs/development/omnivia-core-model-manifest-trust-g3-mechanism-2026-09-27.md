# OmniVia Core — G-3 mechanism landed (model-manifest trust), inert until anchored

**Date:** 2026-09-27 · **Scope:** decision-runtime gates G-2/G-3, agent-buildable half only · **Handoff:** `omnivia-core-decision-runtime-gates-g2-g3-resolution-handoff-2026-09-26.md`

## What this change is

The sequencing proposal in the G-2/G-3 handoff names exactly one piece of engineering that can be built before the owner resolves the gates: the model-manifest verification *mechanism* on the existing runtime-payload trust format, built inert, fully tested, and documented as production-inert until the real anchor is published. This change is that piece and nothing else.

- **New module:** `omnivia_core_runtime.distribution.model_manifest_trust`. It reuses `trusted_runtime`'s `TrustAnchor` (windows, rotation), canonical-JSON discipline, domain-separated identity (self-excluding preimage), and verify-then-name-the-pair order — pointed at model manifests (identifier, version, artifact digests/sizes, runtime/API contract version) instead of runtime payload trees.
- **Closed refusal vocabulary** (`model_manifest_metadata_invalid` / `_untrusted` / `_tampered` / `_incompatible`), payload-free on failure: a refusal string carries no manifest content, artifact names, or key material.
- **Signed exactly like a release:** the detached signature document has the same five-member shape, the same inside/outside key-id agreement requirement, and the same unknown-anchor/expired-window/wrong-key consolidation into one `untrusted` answer.
- **Inert by construction:** verification demands explicit anchors from the caller; no production anchor source exists in this repository; the only key material the module can produce is `dev_test_anchor`, which refuses without `OMNIVIA_ALLOW_DEV_MODEL_TRUST_ANCHOR=1` and yields an ephemeral, process-local keypair whose verdicts mean "the mechanism works" and nothing more.

## Tests

`test_model_manifest_trust.py` — 30 tests covering the handoff's demanded negative set (tampered manifest → `tampered`; unknown anchor, expired window, not-yet-valid window, retired key, wrong key id, wrong algorithm, forged signature → `untrusted`), the structural gate (wrong schema version, malformed digest, duplicate artifact name, empty artifact list, negative/bool size, traversal artifact name, unknown member, wrong signature version → `metadata_invalid`), the canonical-subset gate (float size refused at identity computation), expected-pair mismatch → `incompatible`, rotation by overlapping windows, self-excluding identity, payload-free refusal strings, and the dev-anchor flag discipline. `ruff` and `mypy --strict` clean.

## What this does NOT do

- It does not mint, embed, or nominate a production trust anchor (G-3a–d remain open).
- It does not verify any real Laya distribution (G-2 remains blocked on the product lane publishing a manifest + artifacts at a stated digest).
- It does not enable any production code path: until PR-4's install path is wired to a published anchor, every caller supplies its own anchors, and a dev/test anchor verdict is dev/test trust.

## Owner decision agenda (unchanged, from the handoff §4)

| # | Decision | Constraint from prior rulings |
|---|---|---|
| G-3a | Who holds the production signing key | Desktop-shell verifier is the sole Ed25519 authority for mounted app releases; a model-manifest authority may be a separate key but must not create a second verifier in that lane |
| G-3b | Custody and ceremony | Answers app-shell stage-2 closeout item 4 |
| G-3c | Where the public anchor is published | Repository-external, per the preflight checkpoint precedent |
| G-3d | Rotation and revocation policy | Reuse the existing anchor-window mechanism — already implemented here |
