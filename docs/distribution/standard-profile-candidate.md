# Standard-profile wheel candidate

## Profile and channel

The V06-7 Standard profile is exactly five first-party Python distributions:

1. `omnivia-core`
2. `omnivia-core-runtime`
3. `omnivia-core-client`
4. `omnivia-core-cli`
5. `omnivia-core-mcp`

The first qualified distribution channel is wheels only. A candidate includes
those five wheels plus the exact third-party closure pinned by
`scripts/mcp-wheelhouse-constraints.txt`. It is installed offline in a clean
virtual environment before any product assertion is accepted.

## Executed journey

`scripts/run-standard-journey.py` imports no OmniVia package and finds only the
installed executables beside its interpreter. It performs this sequence:

1. initialize a portable workspace;
2. capture one bounded local regular file through the service-owned maintenance
   path, which acquires the ordinary lifetime lock, lease, guard, and fence;
3. start the real local service and prove health;
4. prove a second owner is refused and cleans up;
5. create a source-cited memory record, propose it, approve it, and search it
   through the installed CLI;
6. initialize the installed MCP server with the official SDK, verify the exact
   six-tool manifest, run governed knowledge search, and build a cited Context
   Pack;
7. kill the original service without cleanup and prove the next CLI call retires
   its stale descriptor, starts a new owner, and returns healthy.

The same installed-wheel environment also migrates a seeded frozen Phase 0
database into workspace format 1, verifies the pre-upgrade backup and preserved
identity/history, proves a corrupt upgrade cannot publish, refuses writable use
by an older Core, refuses an unknown workspace format, and restores the verified
pre-upgrade inventory to a separate recovery target without downgrading the live
workspace.

The retained journey result contains no source path, workspace path, credential,
or source content.

## Candidate artifacts

`scripts/build-standard-candidate.py --output <directory>` produces and verifies:

- the complete platform wheelhouse;
- a standalone-journey result;
- an upgrade, failed-upgrade, rollback-boundary, and recovery result;
- a release manifest and whole-candidate SHA-256 index;
- SPDX 2.3 package SBOM;
- copied embedded license files, third-party license inventory, and NOTICE;
- source/build/dependency provenance;
- the current platform result and the required Linux/macOS/Windows matrix,
  including exact first-party, Core, Client, workspace-format, protocol, and API
  versions;
- an explicit signature-verification document.

The existing `Phase 2 platform (<os>)` required-check names are unchanged. Each
Linux, macOS, and Windows row builds its own platform candidate and retains it as
a short-lived workflow artifact.

## Release boundary

These are qualification candidates, not production releases. They state
`unsigned` and `production_release_eligible: false`. No private key, signing
identity, package-index credential, upload, tag, or release publication occurs
on the pull-request path. A production release requires separate signing and
release authorization after all three platform rows pass for the exact source
revision.
