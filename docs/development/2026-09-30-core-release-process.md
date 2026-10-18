# The omnivia-core release process (v0.4 §13)

**Date:** 2026-09-30 · **Scope:** the Core lane's publication workflow. Desktop
products publish through their own lanes; the shared publication *validation*
is what this doc shares.

## The workflow, in order (each step gates the next)

1. **Build the candidate.** `python scripts/build-standard-candidate.py --output <dir> --allow-dirty`
   — five first-party wheels, the reviewed wheelhouse closure, checksums, SBOM,
   licences, and the journey/lifecycle evidence run in the same invocation.
   Verdict must be `pass`; an incomplete build is never a candidate.
2. **Produce the bundle.** The candidate's `omnivia-core-standard-candidate.zip`
   (checksums + wheels + licences + metadata + evidence inside — the exact
   archive the update coordinator's executor stages and verifies).
3. **Verify the build inventory.** `python scripts/publish-update-channel.py --candidate <dir>`
   (prepare mode): validates the release manifest's format/profile, the
   five-wheel single-version rule, and computes the bundle digest. This is the
   recommendation the channel will carry.
4. **Publish the GitHub Release.** Tag `core-v<version>`; upload the bundle zip
   + the checksum file; write the release notes (journey verdict, known
   limitations, supported platforms). Assets are immutable: changed bytes are a
   new version, never a re-upload.
5. **Verify the published identity.** Re-download the bundle from the release
   URL and re-check its digest against the prepared recommendation. A published
   asset that does not re-verify is never recommended.
6. **Publish the recommendation last.**
   `python scripts/publish-update-channel.py --candidate <dir> --apply` stages
   the `channel.json` update in a clone of `omnivia-core-updates`; the commit +
   push to `main` is the operator's act — publication is owner-gated (D-1's
   trust label stands until D-2/D-4). The recommendation goes live last, after
   the assets it recommends are verified in place (§13's promotion order).
7. **Smoke-test discovery.** `omnivia service update-check --json` against the
   live channel must answer `update_available` (or `up_to_date` from an
   installation already at that version), with the release URL resolvable.

## Standing rules

- **Never clobber**: a published release asset's bytes are immutable; a
  channel already recommending `X` is never re-recommended.
- **Never downgrade**: the channel's recommendation never moves below the live
  recommendation; a defective release stops being recommended by publishing a
  corrected higher version (or `release: null` in an emergency, which is a
  "no recommendation", never a rollback of installed software).
- **Stale-recommendation guard**: the publisher refuses to recommend a version
  lower than the live one.
- **Credentials**: publication runs from the operator's authenticated `gh`;
  no tokens in metadata, feed, or the candidate.
- **Trust label**: until D-2/D-4 land, the feed is digest-only (HTTPS +
  checksums), explicitly not publisher-authenticated. A signed channel becomes
  possible only through the anchor machinery (#141's format, one key
  ceremony).

## What "shipped" means here

A release that has passed 1–7 has: qualified build evidence, verified published
assets, a live recommendation, and a smoke-tested discovery path. The update
path itself (`service update`) is exercised end to end only after a *second*
release exists — its first real customer is the 0.1.1 release, whenever the
owner approves publication.
