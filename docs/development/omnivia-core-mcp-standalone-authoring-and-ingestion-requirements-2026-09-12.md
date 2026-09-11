# OmniVia Core MCP standalone authoring and ingestion requirements

Date: 2026-09-12

Status: proposed product requirement and implementation specification

Owner: Core

Applies to: `omnivia-core-mcp`, the Core application contract, Core Service,
the standalone installer/setup flow, and MCP qualification

Supersedes: the read-only-only product assumption in MCP manifest `1.1`; it
does not weaken the workspace, service, or storage authority boundaries

## 1. Decision

OmniVia Core's standalone MCP product MUST be bidirectional.

A user who installs Core, creates a workspace, and connects Claude Desktop,
Claude Code, Codex, or another conforming MCP host MUST be able to:

1. submit new information to that workspace;
2. create proposed memory without making it accepted canonical knowledge;
3. start and observe an authorised import of an already-staged source;
4. retrieve the submitted information through the same MCP connection; and
5. disable all MCP mutation without losing the existing retrieval tools.

An MCP-only host MUST NOT require Desktop, Dev, direct database access, a
Runtime import, or an undocumented maintenance command to complete that
journey.

The current six-tool, read-only server is a valid restricted profile. It is not
the complete standalone MCP product.

## 2. Why this requirement exists

MCP manifest `1.1` exposes six reads from the 27-operation Core catalogue. Core
already implements mutations such as `memory.create` and `import.start`, but
MCP deliberately omits them. Consequently, Claude or Codex connected only by
MCP can query a populated workspace but cannot populate an empty one.

The present MCP end-to-end fixture creates evidence, records, and graph state
through test-only Runtime code before it launches MCP. That proves retrieval;
it does not prove standalone ingestion.

There is a second gap: `import.start` accepts only an immutable descriptor for a
source that Core already staged. It intentionally accepts no path, URL,
credential, or inline content. Exposing `import.start` alone therefore does not
give an MCP-only host a complete ingestion path.

This specification closes both gaps while retaining Core as the sole workspace
writer and authority.

## 3. Normative language

`MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT`, and `MAY` are normative.

"MCP host" means the application running the MCP client, including Claude
Desktop, Claude Code, and Codex. "Model-callable" means a tool the host may
offer to a model. "Authoring" means additive creation of proposed memory,
staged content, or L0 evidence. Authoring does not mean approval, rejection,
supersession, workspace administration, or service administration.

## 4. Product boundary

The intended ownership remains:

```text
Claude / Codex
      |
      | MCP tool call
      v
omnivia-core-mcp       configuration, tool policy, schema projection
      |
      | authenticated Application Contract request
      v
omnivia-core-client    discovery, transport, negotiation, credentials
      |
      v
Core Service           authorization, validation, audit, lease and fencing
      |
      v
Workspace              authoritative storage; never opened by MCP
```

MCP MUST NOT import Runtime, open the workspace database, hold the authoritative
workspace lease, stage files by direct workspace access, or construct a second
write path. Every accepted mutation MUST travel through the shared client and
the Core Service application boundary.

## 5. Required tool profiles

### 5.1 Restricted profile

When `mutation_enabled` is `false` or absent, MCP MUST expose the existing six
tools and no mutation tool:

| MCP tool | Core operation |
|---|---|
| `workspace_inspect` | `workspace.inspect` |
| `evidence_search` | `evidence.search` |
| `knowledge_search` | `knowledge.search` |
| `memory_search` | `memory.search` |
| `graph_traverse` | `graph.traverse` |
| `context_pack_build` | `context_pack.build` |

This is the safe default for an installation that has not explicitly enabled
MCP authoring.

### 5.2 Authoring profile

When `mutation_enabled` is `true`, MCP MUST expose the restricted profile plus
the following tools:

| MCP tool | Core operation | Purpose | Required scope | Effect |
|---|---|---|---|---|
| `memory_create` | `memory.create` | `memory_authoring` | `memory:write` | Creates a proposed-only governed record |
| `evidence_capture` | new `evidence.capture` | `content_ingestion` | `memory:write` | Captures explicitly supplied bytes as immutable L0 evidence |
| `import_start` | `import.start` | `content_ingestion` | `memory:write` | Starts an import of an already-staged source |
| `job_get` | `job.get` | `job_observation` | catalogue-defined read scope | Reads current import status and terminal result |
| `job_events` | `job.events` | `job_observation` | catalogue-defined read scope | Reads the bounded import event history |

`job_get` and `job_events` are included in the authoring profile because an
asynchronous ingestion operation without an observation path is not a complete
product journey. They remain read operations.

The authoring profile MAY later add `knowledge_propose` after its mutation
precondition is represented safely in MCP tool input. It is not required for
the first standalone ingestion milestone.

### 5.3 Operations excluded from this milestone

The following MUST remain absent from the initial authoring profile:

- `candidate.approve`, `candidate.reject`, and `record.supersede`;
- `workspace.create` and `workspace.list`;
- service start, stop, status, discovery, health, and readiness as model tools;
- `job.cancel` and `job.retry`;
- unrestricted local path, URL, credential, connector-configuration, or
  filesystem-selection tools; and
- chat and workflow mutations, which require their own product and authority
  decision.

Exclusion is a milestone boundary, not a claim that MCP cannot support those
operations. Canonical governance transitions require a Core-verifiable human
approval design before they become model-callable.

## 6. New `evidence.capture` application operation

Core MUST add a provider-neutral, workspace-scoped `evidence.capture`
operation. It is needed because `import.start` cannot accept content and an MCP
server cannot safely infer or directly read a path from the host machine.

### 6.1 Semantics

`evidence.capture` MUST:

1. accept content transferred explicitly in the request;
2. create immutable L0 evidence, never accepted canonical knowledge;
3. compute the content checksum and length in Core rather than trusting caller
   values;
4. attribute submission to the authenticated principal and selected workspace;
5. record a server-owned `direct_submission` source kind, plus the media type
   and caller-supplied source identity as provenance claims rather than
   authorization inputs;
6. publish content through the existing content-addressed blob and fenced-write
   path;
7. return the evidence identifier, computed checksum, byte length, media type,
   and whether the call created or replayed the capture; and
8. be audited as a mutation.

It MUST NOT:

- accept a filesystem path, file URL, network URL, credential, parser name,
  workspace identifier, principal, scope, capability, or storage option in the
  operation payload;
- execute, render, import, or follow references contained in submitted content;
- treat model-generated text as externally verified evidence; or
- create a candidate or accepted record as a side effect.

### 6.2 Input shape

The canonical contract MUST define one closed input object containing:

- `source_native_id`: required stable caller-visible identifier, bounded to the
  existing source-identity domain;
- `media_type`: required validated media type;
- exactly one of `text` or `content_base64`;
- optional `source_version`;
- optional `event_at`; and
- optional `observed_at`.

The encoded request MUST be bounded before any unbounded decoding or
allocation. The first release MUST accept no more than 1 MiB of decoded content
per call. A larger document must use a separately authorised connector or
staging path. Invalid base64, invalid UTF-8 for `text`, a dual/empty content
choice, and a size overflow MUST be refused without writing.

The exact JSON Schema, generated language models, semantic validator, operation
metadata, allowed-error set, and conformance vectors are contract-owned and
MUST land before the MCP mapping.

### 6.3 Replay and source identity

`evidence.capture` MUST require an idempotency key. Replaying the same key and
same request MUST return the first settled result and MUST NOT create another
artifact. Reusing the key with different input MUST return
`idempotency_conflict`.

Reusing one `source_native_id` with different content MUST fail closed unless a
future version defines an explicit source-version rule. The first release MUST
not silently replace evidence.

## 7. Mutation-call contract

Canonical application-operation inputs do not contain request-envelope
metadata. MCP mutation tools therefore MUST advertise an MCP adapter wrapper
with two fields:

```json
{
  "input": { "...": "canonical operation input" },
  "idempotency_key": "caller-stable opaque key"
}
```

`input` MUST be the generated, self-contained canonical input schema. The MCP
adapter MUST remove `idempotency_key` from the operation payload and place it in
`RequestMetadata.idempotency_key`. It MUST NOT transcribe or independently
redefine the canonical operation fields.

The key is required so an MCP host or model can retry an ambiguous call without
creating duplicate data. The same tool arguments, including the same key, MUST
be replay-safe. The server MUST NOT invent a new key on every retry, derive one
only from content, or use an MCP transport request identifier as durable
application identity.

If a later MCP mutation supports `MutationPrecondition`, its wrapper MUST carry
a separate `record_version` field and the adapter MUST map that field to the
request envelope. Authority fields remain forbidden tool arguments.

## 8. Authorization and configuration

The existing trusted `omnivia.mcp-config.v1` field `mutation_enabled` MUST become
an enforced authority ceiling rather than an unused configuration value.

A mutation is callable only when all of the following are true:

1. the operation is in the curated authoring manifest;
2. `mutation_enabled` is exactly `true`;
3. the operation's fixed purpose is present in `allowed_purposes`;
4. the configuration selects exactly one allowed workspace;
5. the authenticated principal has the required server-side scope and
   capability grant;
6. the canonical input and adapter wrapper validate;
7. a valid idempotency key is present; and
8. Core's lease, fencing, migration, and runtime checks permit the mutation.

No one condition implies another. In particular, `mutation_enabled=true` is not
a grant, and an MCP host approval dialog is not Core authorization.

The model MUST NOT be able to supply or override the principal, workspace,
purpose, scopes, grants, capability requirement, mutation flag, endpoint, or
credential reference. The existing reserved-argument refusal MUST apply
recursively to the adapter wrapper and canonical input.

### 8.1 Tool discovery

`tools/list` MUST reflect the trusted profile:

- restricted configuration: restricted tools only;
- authoring configuration: restricted plus authoring tools.

The mutation flag determines discovery; `allowed_purposes` remains a call-time
authorization check as it is today. The result MUST be deterministic for the
tuple of package version and `mutation_enabled` value. It is no longer required
to be identical across configurations with different mutation ceilings.

## 9. Standalone setup without a UI

Core has no required UI, so authoring setup MUST be possible through an
installed command.

The installed product MUST provide one documented setup flow equivalent to:

```text
omnivia mcp configure --host <claude-desktop|claude-code|codex> \
  --workspace <workspace-id> --enable-authoring
```

The command name may change during CLI design, but the released behavior MUST:

1. require explicit user intent before enabling mutation;
2. create or update the owner-private MCP configuration atomically;
3. create the corresponding least-privilege server-side principal grant;
4. select one workspace and no ambient fallback;
5. install or print the exact host-native MCP entry;
6. never put credentials in the host configuration;
7. support a read-only choice; and
8. support revoking authoring without deleting the workspace or MCP connection.

Installation MAY offer this flow, but MUST NOT silently grant authoring merely
because Core was installed. A non-interactive installer MUST default to the
restricted profile unless an explicit, recorded install option enables
authoring.

On macOS, this design MUST NOT require Full Disk Access merely to use MCP
authoring. Content is transferred explicitly to Core. Any future local-file
picker or connector owns its own narrow OS permission and remains outside this
tool contract.

## 10. Host behavior and annotations

All required tools MUST work over the official MCP SDK without host-specific
protocol branches.

Tool annotations MUST accurately describe behavior:

- reads: `readOnlyHint=true`, `destructiveHint=false`;
- `memory_create`, `evidence_capture`, and `import_start`:
  `readOnlyHint=false`, `destructiveHint=false`;
- mutation tools with a required idempotency key MAY set `idempotentHint=true`
  only when repeating the identical complete tool input provably returns the
  settled result; and
- every tool stays closed-world against the selected Core workspace.

Descriptions MUST say that authoring creates proposed memory or L0 evidence,
not accepted facts. They MUST tell the model to reuse the idempotency key after
an ambiguous timeout.

## 11. Failure and audit requirements

An MCP mutation failure MUST be returned as an MCP tool error with no
`structuredContent`, matching the existing failure contract. Diagnostics MUST
not expose submitted content, workspace paths, endpoints, credentials, or
private configuration values.

Every call that reaches Core MUST produce the audit behavior declared by the
operation catalogue. Audit records MUST bind at least the operation, principal,
workspace, purpose, outcome, correlation identifier, and idempotency outcome.
They MUST NOT duplicate the submitted content in diagnostic text.

A timeout after dispatch is an ambiguous outcome. The tool error MUST instruct
the caller to retry with the same idempotency key or retrieve the created item;
it MUST NOT recommend a fresh key.

## 12. Acceptance requirements

The milestone is complete only when all of the following pass from installed
candidate artifacts.

### A. Empty-workspace standalone journey

For each supported host profile:

1. initialise an empty workspace using the installed setup path;
2. connect a fresh MCP session with authoring enabled;
3. confirm the authoring tool list;
4. call `memory_create` and retrieve the proposed record through
   `memory_search` using the appropriate explicit view;
5. call `evidence_capture` with unique content and retrieve it through
   `evidence_search`;
6. replay both mutation calls with the same idempotency keys and prove no
   duplicate rows, blobs, records, or evidence artifacts exist;
7. close MCP and prove the independently owned Core service remains healthy;
   and
8. retain a redacted result containing tool names, result counts, replay
   dispositions, and verdicts only.

The test MUST NOT pre-seed application data through Runtime, direct storage,
fixtures, CLI mutation commands, or maintenance capture before the MCP calls.

### B. Import journey

Using an independently authorised staged-source fixture:

1. call `import_start` through MCP;
2. observe it with `job_get` and `job_events` until terminal;
3. verify the terminal counts;
4. retrieve created evidence through MCP; and
5. prove retrying `import_start` with the same key does not create a second job.

This journey proves import dispatch and observation. The empty-workspace
journey independently proves that MCP itself can contribute content without an
out-of-band staging dependency.

### C. Negative security matrix

Tests MUST prove refusal before mutation for:

- `mutation_enabled` absent or false;
- purpose absent from `allowed_purposes`;
- principal without required scope or capability;
- wrong or ambiguous workspace;
- missing, malformed, replay-conflicting, or oversized idempotency key;
- authority-shaped fields at any nesting level;
- local paths, URLs, credentials, and unknown fields;
- invalid/oversized evidence content;
- stale service generation, lost lease, migration required, and workspace busy;
- response-correlation mismatch; and
- an operation absent from the curated manifest.

Every refusal MUST prove zero authoritative writes and no leaked payload.

### D. Real-host qualification

Configuration-shape simulation with the official SDK remains necessary but is
not sufficient. Release evidence MUST include at least one real Claude host and
one real Codex host launching the installed MCP command, performing one
authoring call, and retrieving its result. Host-local approval behavior may be
recorded as defense in depth but MUST NOT be treated as Core authorization.

### E. Shared conformance

MCP mutation calls MUST pass the same canonical application-contract and wire
conformance vectors as CLI, IPC, HTTP, and in-process service paths. MCP-only
happy-path tests are insufficient.

## 13. Versioning and documentation

The change MUST:

- bump the MCP exposure manifest major version from `1.1` to `2.0`;
- update the MCP package description and server instructions so they no longer
  claim the product is universally read-only;
- update operation traceability to include the new catalogue operation and the
  revised MCP mapping;
- distinguish restricted and authoring profiles in host interoperability docs;
- regenerate all advertised schemas from canonical contract sources; and
- record the read-only-to-authoring change in release notes as an explicit
  authority expansion.

Existing `mutation_enabled=false` configurations MUST remain valid and retain
read-only behavior. A release MUST NOT silently reinterpret an absent field as
enabled.

## 14. Non-goals

This milestone does not:

- let MCP initialise or choose arbitrary workspaces;
- let MCP manage the Core service lifecycle;
- let Core read arbitrary host files or URLs;
- make proposed memory canonical automatically;
- let a model approve its own governance proposal;
- replace connectors for large or continuously synchronised sources;
- make MCP the workspace owner; or
- require Desktop or Dev.

## 15. Completion statement

The work may be described as "standalone Core MCP authoring and ingestion
complete" only when an installed Claude or Codex MCP host can start with an
empty workspace, contribute bounded content through MCP, retrieve that content
through MCP, safely replay an ambiguous mutation, and do so without Desktop,
Dev, CLI mutation, direct Runtime use, or direct workspace access.

Until those conditions are met, the accurate status is:

> Core MCP retrieval is implemented. Standalone MCP authoring and ingestion are
> specified but incomplete.
