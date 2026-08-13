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
6. write and reparse each named host profile's own native configuration shape —
   `claude_desktop`, `claude_code`, `codex` and `official_python_sdk` — and
   start the installed MCP server from the launch each one yields, driving one
   fresh stdio session per profile with the official Python SDK as the client,
   verifying the exact six-tool manifest and calling every advertised tool end
   to end each time;
7. kill the original service without cleanup and prove the next CLI call retires
   its stale descriptor, starts a new owner, and returns healthy.

## Host interoperability

The four accepted profiles, the configuration form each reads, the retained
evidence and every rejection class are specified in
[MCP host interoperability](mcp-host-interoperability.md).

Each profile's session is driven from the launch its own configuration yields,
so the configuration mechanism itself is under test. The Claude Desktop, Claude
Code and Codex applications are not installed and do not run: the client is
always the official Python SDK, and what each profile proves is that its
host-native configuration shape round-trips to the accepted launch and that the
server that launch starts answers identically. Every profile must
advertise the identical six-tool manifest, and each of the six must return a
populated result: the journey created the evidence, knowledge, memory, graph and
context the six tools read, so an empty answer is a failure rather than an empty
workspace.

`mcp.hosts` in `standalone-journey-result.json` carries one object per profile:

```json
{
  "client": "codex",
  "config_format": "codex_toml",
  "connected": true,
  "session_completed": true,
  "tool_count": 6,
  "tool_calls": 6,
  "tools": ["context_pack_build", "evidence_search", "graph_traverse",
            "knowledge_search", "memory_search", "workspace_inspect"],
  "result_counts": {
    "context_pack_build": 1,
    "evidence_search": 1,
    "graph_traverse": 1,
    "knowledge_search": 1,
    "memory_search": 1,
    "workspace_inspect": 1
  },
  "verdict": "pass"
}
```

Those keys are the whole of it. No executable, configuration path, endpoint,
argument, stdout, stderr, credential, PID or free text is retained.

`scripts/build-standard-candidate.py` refuses a candidate whose journey result
does not carry that evidence, with those values, for all four profiles.

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
