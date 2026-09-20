# OmniVia Core MCP standalone authoring and ingestion requirements

**Revision:** 1.3
**Date:** 2026-09-12
**Status:** specification complete; implementation not started by this document
**Supersedes:** revisions 1.0, 1.1, and 1.2 of this requirement
**Repository baseline:** `990b0f980c633840922170976c73b8f966361eab`

## 1. Decision

OmniVia Core shall support two explicit MCP exposure profiles:

- `restricted`: the existing six read-only tools;
- `authoring`: those six tools plus `memory_create`, `evidence_capture`,
  `import_start`, `job_get`, and `job_events`.

The authoring profile is intended to make Core usable as a standalone local
product from MCP hosts such as Claude Code and Codex. It permits a trusted,
workspace-bound MCP principal to submit evidence, create proposed memory, start
imports from already-staged content, and observe the resulting jobs. It does
not grant governance authority, expose arbitrary filesystem or network access,
or let a model manage its own grants.

`evidence.capture` is a new, provider-neutral Core application operation. All
three MCP mutation tools use the same canonical Core mutation path as other
adapters. MCP is an adapter, not an alternate ingestion or persistence system.

This document is the complete implementation specification. It closes the
open design questions from revision 1.2. It does not itself implement the
feature, alter contracts, create a production workspace, issue a real grant,
change host configuration, or publish a release.

## 2. Normative language and product boundary

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.

The feature boundary is:

1. A human owner or administrator installs Core and selects or creates the
   workspace outside the MCP model-call surface.
2. The same human explicitly enables the `authoring` profile and grants the
   dedicated MCP principal the bounded contributor and job-observation rights.
3. The MCP server derives workspace and principal identity from its private
   configuration and service session. Tool arguments never select either.
4. The model calls MCP tools. The adapter validates the MCP wrapper, translates
   it to the canonical application request, and calls the Core service.
5. Core performs authorization, audit, idempotency, persistence, projection,
   and job execution under its existing service invariants.

The MCP server MUST NOT become a second database writer, ingestion coordinator,
or policy engine. It MUST NOT call the maintenance-only source-capture helper
as if that helper were an application operation.

## 3. Profiles and exact tool inventory

### 3.1 Restricted profile

The restricted profile exposes exactly:

1. `workspace_inspect`
2. `evidence_search`
3. `knowledge_search`
4. `memory_search`
5. `graph_traverse`
6. `context_pack_build`

Its behavior remains read-only. An upgrade from the current installation MUST
preserve this profile unless a human explicitly enables authoring.

### 3.2 Authoring profile

The authoring profile exposes exactly the six restricted tools plus:

7. `memory_create`
8. `evidence_capture`
9. `import_start`
10. `job_get`
11. `job_events`

The list returned by MCP `tools/list` MUST be determined from the validated
profile at server startup. It MUST NOT vary by model prompt or by arguments to
an individual tool call. Invalid, missing, or unsafe configuration MUST fail
closed before the server advertises authoring tools.

### 3.3 Excluded operations

This milestone MUST NOT expose:

- workspace creation, deletion, selection, or enumeration;
- grant creation, renewal, revocation, or inspection;
- candidate approval or rejection, publication, supersession, or other
  governance decisions;
- job cancellation or retry;
- chat, workflow, or connector mutation operations;
- arbitrary filesystem paths, URLs, credentials, connector configuration,
  parser choices, storage locations, or runtime flags.

## 4. Canonical operation bindings

Each MCP tool MUST be a thin binding to the operation shown below. The Core
catalogue remains the authority for schemas, scopes, capabilities, purposes,
audit metadata, errors, and completion behavior.

| MCP tool | Core operation | Side effect | Scope | Capability | Purpose | Audit | Completion |
|---|---|---|---|---|---|---|---|
| `memory_create` | `memory.create` | create | `memory:write` | `memory.write@1.0` | `memory_authoring` | mutation | synchronous |
| `evidence_capture` | `evidence.capture` | create | `memory:write` | `evidence.write@1.0` | `content_ingestion` | mutation | synchronous |
| `import_start` | `import.start` | create | `memory:write` | `ingestion.import@1.0` | `content_ingestion` | mutation | always returns job |
| `job_get` | `job.get` | read | `job:read` | `job.read@1.0` | `job_observation` | read | synchronous |
| `job_events` | `job.events` | read | `job:read` | `job.read@1.0` | `job_observation` | read | synchronous, paginated |

`evidence.write@1.0` is the required new capability identifier. It MUST be
added to the same shared catalogue and generated projections as the operation;
the MCP package MUST NOT privately invent or override operation metadata.

The proposed catalogue entry uses input schema reference
`https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceCaptureInput`
and result schema reference
`https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceCaptureResult`.
It is workspace-scoped, non-paginated, supports and requires an idempotency
key, has `safe_to_retry: false` under the existing catalogue meaning, supports
no mutation precondition, and is audited in category `mutation`.

The existing six read operations retain their current catalogue bindings.
Every advertised tool MUST pass the same exposure-manifest admission checks.
The manifest must therefore be revised to admit only the three named mutations
when the selected profile is `authoring`, while continuing to reject every
other side-effecting operation.

## 5. MCP call contract

### 5.1 Mutation wrapper

Every MCP mutation tool accepts this outer shape:

```json
{
  "input": {},
  "idempotency_key": "host-generated-stable-key"
}
```

`input` is exactly the canonical Core operation input. `idempotency_key` is a
non-empty opaque string satisfying the canonical request-envelope constraint.
Both properties are required and additional outer properties are forbidden.

The adapter MUST:

1. validate the outer wrapper;
2. validate `input` against the generated operation input schema;
3. put `idempotency_key` in the canonical application request envelope;
4. obtain a fresh server-issued mutation grant for that attempt;
5. send the fixed operation purpose and required capability from the catalogue;
6. return the canonical operation result without changing its meaning.

The adapter MUST NOT place the key inside operation input, synthesize authority
fields, accept a workspace identifier, or accept a caller-selected purpose.

### 5.2 Read tools

`job_get` accepts the canonical `JobGetInput` directly:

```json
{"job_id":"job_01HXYZ"}
```

`job_events` accepts the canonical `JobEventsInput` directly:

```json
{"job_id":"job_01HXYZ","limit":100}
```

Continuation requests use the canonical page metadata returned by the prior
response. The server MUST preserve the existing snapshot-stable ordering and
the catalogue maximum of 1,000 events per page. Neither tool is a subscription
or transport stream.

### 5.3 MCP result encoding

Successful calls MUST continue to return both canonical `structuredContent`
and the canonical JSON text representation expected by current clients.
Failures MUST set `isError: true`, return no `structuredContent`, and expose the
stable, sanitized Core error representation through the existing text error
form. No failure may leak paths, raw SQL, stack traces, credentials, private
configuration, grants, or unredacted source content.

## 6. New `evidence.capture` operation

### 6.1 Purpose and semantics

`evidence.capture` synchronously records one caller-supplied UTF-8 text or
Markdown artifact as immutable L0 evidence in the selected workspace. It is
for small direct submissions such as notes, excerpts, and model-visible source
material. Bulk files and archives use staging plus `import.start`.

Capture does not execute or render Markdown, follow references, verify that a
model-generated statement is externally true, or create a candidate, proposed
memory record, or accepted knowledge as a side effect.

The operation MUST be added as a first-class application contract, catalogue
entry, service handler, generated schema, client method, CLI binding if the
shared adapter policy requires one, MCP binding, audit category, and test
fixture. It MUST use the normal service runner and fenced transaction.

### 6.2 Canonical input

The normative input is:

```json
{
  "source_native_id": "submission-2026-09-12-001",
  "media_type": "text/markdown",
  "text": "The warranty period is two years.",
  "source_version": "v1",
  "event_at": "2026-09-11T23:30:00Z",
  "observed_at": "2026-09-12T00:00:00Z"
}
```

Properties:

| Property | Requirement |
|---|---|
| `source_native_id` | Required caller-chosen opaque identifier; non-empty; subject to the existing source-identity domain. |
| `media_type` | Required; exactly `text/plain` or `text/markdown`. Parameters such as `charset=` are forbidden. |
| `text` | UTF-8 text form. Exactly one of `text` and `content_base64` is required. |
| `content_base64` | Strict RFC 4648 base64 of UTF-8 bytes. Exactly one content form is required. |
| `source_version` | Optional provenance claim in the existing identifier domain; it does not permit overwrite or create a second identity version. |
| `event_at` | Optional canonical RFC 3339 UTC event-time claim. |
| `observed_at` | Optional canonical RFC 3339 UTC observation-time claim; when both times exist, `event_at` must not be later. |

The decoded byte length MUST be between 1 and 1,048,576 bytes inclusive.
`content_base64` MUST decode strictly and the decoded bytes MUST be valid UTF-8.
The service MUST hash the decoded bytes before persistence. The operation MUST
reject unknown fields and any path, URL, credential, workspace, principal,
grant, parser, layer, governance, or storage option. It MUST apply an encoded
request bound before unbounded JSON unescaping or base64 allocation. Content
values are inert: reserved-looking JSON, paths, and URLs inside submitted text
are neither interpreted nor followed.

The schemas MUST be closed Draft 2020-12 objects in the existing application
v1 evidence contract. Generated language models and semantic validation must
enforce the exclusive content choice, identifier/timestamp domains, media
allowlist, and decoded-byte bound. Search normalization may derive projection
text, but the preserved blob bytes are never rewritten.

### 6.3 Canonical result

The result MUST identify the stored evidence artifact and its stable source:

```json
{
  "evidence_id": "ev_01HXYZ",
  "source": {
    "kind": "direct_submission",
    "source_id": "submission-2026-09-12-001"
  },
  "media_type": "text/markdown",
  "content_checksum": "sha256:bd80e192e6ed8eab5a14fa6b60ac785722aecbe19c49d9056f31020922fd616f",
  "content_length_bytes": 33,
  "capture_disposition": "created"
}
```

`capture_disposition` is `created` for the first committed capture and
`already_captured` for a same-source, identical-claims capture resolved under
the rules below. A replay of the original idempotency key returns its stored
canonical result and marks replay only in existing envelope/execution metadata;
it does not rewrite the original result body. The checksum and byte count
describe decoded UTF-8 bytes.

### 6.4 Source identity and collision rules

The persisted source reference is exactly:

```json
{
  "kind": "direct_submission",
  "source_id": "submission-2026-09-12-001"
}
```

In storage, identity is the null-safe tuple:

`(workspace_id, source_kind, source_native_id, locator, retrieved_at)`

For direct submission, `source_kind` is `direct_submission`,
`source_native_id` is the input `source_native_id`, and both `locator` and
`retrieved_at` are absent/NULL. Principal identity is recorded in provenance
and audit data, not folded into source identity. Thus two principals in the
same workspace cannot independently claim the same direct-submission source
identifier for different bytes.

| Existing identity | Submitted claims | Required outcome |
|---|---|---|
| none | valid | create artifact |
| one exact artifact | same checksum, length, media type, source version, event time, and observed time | return existing artifact as `already_captured` |
| one exact artifact | any of those claims differs | canonical `conflict` |
| more than one exact artifact | any | invariant failure; write nothing |

Implementation MUST enforce uniqueness for new direct-submission rows at the
database boundary and must detect legacy duplicates explicitly. It MUST NOT
silently choose one row from a non-unique legacy result.

Full workspace authorization is evaluated before source lookup. A collision
returned to a principal who may create but cannot read the existing artifact
must not disclose its evidence identifier, checksum, metadata, or content.

The operation's allowed-error set is the current `memory.create` mutation set
plus `conflict`, `projection_unavailable`, `stale_projection`, and
`size_limit_exceeded`: `authentication_required`, `authorization_denied`,
`cancelled`, `capability_not_granted`, `conflict`, `deadline_exceeded`,
`dependency_unavailable`, `idempotency_conflict`, `incompatible_version`,
`internal_non_recoverable`, `internal_recoverable`, `invalid_purpose`,
`invalid_request`, `projection_unavailable`, `rate_limited`,
`size_limit_exceeded`, `stale_projection`, `upgrade_required`, `workspace_busy`,
`workspace_lease_unavailable`, `workspace_migration_required`, and
`workspace_not_granted`. Source-identity disagreement uses `conflict`; this
revision does not invent a second operation-specific error code. Error messages
and details MUST preserve existing non-disclosure rules.

### 6.5 Retrieval barrier

Success means more than durable blob storage. Before returning a successful
result, the service MUST ensure that a fresh authorized `evidence.search` can
find the new artifact through the normal lexical projection using a term in
the submitted content. The business row, blob reference, application audit,
idempotency outcome, and projection publication or durable projection-recovery
intent MUST be committed consistently.

If the projection cannot be made queryable or durably recoverable, the call
MUST fail closed and MUST NOT report success. Recovery after process failure
must rebuild or finish publication without creating a second artifact.

The existing search contract has no exact evidence-ID selector. Qualification
therefore performs a content query and matches `evidence_id` in the returned
hits:

```json
{"query":"warranty period","limit":20}
```

Semantic embeddings and graph projections are not part of this synchronous
barrier. Capture grants no additional read authority.

## 7. Shared mutation replay and atomicity

All three mutations—`evidence.capture`, `memory.create`, and `import.start`—MUST
use the existing mutation coordinator. The atomic fence contains:

- the business mutation or durable job creation;
- the application audit record;
- the idempotency claim and canonical outcome;
- the execution/settlement record; and
- any durable projection-publication or recovery record required by the
  operation.

The idempotency scope is the existing unique tuple:

`(workspace_id, principal_id, operation, idempotency_key)`

The request digest MUST cover the canonical operation input. A repeated key
with the same digest returns the stored canonical result; a repeated key with
a different digest fails with `idempotency_conflict`. Replay never repeats the
business mutation, creates another job, or emits a second business audit.

Replay is not an authorization bypass. Before revealing a stored result, Core
MUST re-evaluate the principal's current workspace membership, required scope,
capability, purpose, and a fresh server-issued grant. A revoked or downgraded
principal therefore cannot use an old key to recover a result it may no longer
observe. The fresh grant is durably spent on a successful replay attempt in the
same way it is spent on a fresh mutation attempt.

Idempotency claims and outcomes are append-only and retained for the lifetime
of the workspace in this milestone. The short lifetime of an individual grant
(currently 60 seconds) does not limit the replay window. No cleanup or expiry
policy is introduced by this feature.

For an ambiguous transport outcome, a host MUST retry the same operation with
the same input and key. It MUST NOT invent a new key merely because the first
response was lost. A new key is a new mutation request.

## 8. Existing mutation bindings

### 8.1 `memory_create`

The tool input is the mutation wrapper whose `input` is exactly the current
`MemoryCreateInput`. It can create only a proposed record. It cannot assert
accepted authority, currentness, approval, publication, supersession, or a
server-owned record identifier.

The canonical result remains proposed-only. `memory.search` without an
explicit view remains `current_canonical` and therefore does not reveal the
new proposal. A caller authorized for candidate views can retrieve it using:

```json
{"query":"warranty period","view":"candidates","limit":20}
```

This asymmetry is intentional and MUST be documented in tool descriptions so
hosts do not mistake absence from the default view for mutation failure.

### 8.2 Evidence-backed proposed memory

Evidence-backed authoring is supported without a second milestone operation.
`MemoryCreateInput.sources` carries canonical `SourceReference` values and
`assertion.evidence` carries canonical `EvidenceReference` values. The service
already resolves each reference using the exact null-safe source tuple,
requires exactly one accessible artifact, enforces its ACL, and persists the
resolved evidence link.

The MCP schema MUST expose these existing fields unchanged. It MUST NOT add a
private `evidence_id` shortcut. A complete call is:

```json
{
  "input": {
    "record_type": "fact",
    "domain_scope": "workspace",
    "content": {
      "statement": "The warranty period is two years."
    },
    "evidence_disposition": "supported",
    "sources": [
      {
        "kind": "direct_submission",
        "source_id": "submission-2026-09-12-001"
      }
    ],
    "assertion": {
      "actor_id": "mcp-author",
      "actor_kind": "agent",
      "actor_role": "workspace_contributor",
      "asserted_at": "2026-09-12T00:00:00Z",
      "evidence": [
        {
          "source": {
            "kind": "direct_submission",
            "source_id": "submission-2026-09-12-001"
          },
          "excerpt": "The warranty period is two years."
        }
      ]
    }
  },
  "idempotency_key": "memory-warranty-001"
}
```

The actor fields are claim provenance, not authorization. Core MUST bind the
actual principal independently and MUST reject prohibited or contradictory
actor claims under the existing contract rules.

### 8.3 `import_start`

The tool accepts the mutation wrapper whose `input` is the existing
`ImportStartInput`. It names one server-issued, immutable staged source:

```json
{
  "input": {
    "source": {
      "staged_source_ref": "stage_01HXYZ",
      "source_kind": "archive",
      "content_checksum": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "content_length_bytes": 4096,
      "media_type": "application/zip"
    }
  },
  "idempotency_key": "import-product-manuals-001"
}
```

MCP does not stage content in this milestone. The staging handle must already
have been produced by an installed, trusted Core path. `import_start` MUST NOT
accept inline archives, paths, URLs, credentials, connector settings, parser
names, or storage options. A successful fresh call or replay returns the same
durable job handle.

### 8.4 Job observation and in-flight work

`job_get` and `job_events` are read-only observations authorized by `job:read`
and `job.read@1.0`. They do not grant permission to start, cancel, retry, or
alter a job.

Revocation affects later calls and replays; it does not roll back an already
committed mutation. An import job that has been durably created continues
under Core's service-owned worker identity and fencing after the requesting
MCP principal is revoked. Revocation MUST NOT be described as cancellation.
An owner can use the installed operator path, including the canonical CLI job
reads, to observe work after revocation. Job cancellation remains outside this
MCP milestone.

## 9. Authorization, setup, and installation

### 9.1 Dedicated principal

An MCP host MUST use a dedicated Core principal. It MUST NOT reuse the service
worker identity, a human's interactive token, or a global installation secret.
The principal is bound to one configured workspace and the minimum profile
rights.

For `restricted`, grant only the existing read scopes/capabilities. For
`authoring`, grant the existing read rights plus:

- workspace contributor authority sufficient for `memory:write`;
- `memory.write@1.0`;
- `evidence.write@1.0`;
- `ingestion.import@1.0`;
- `job:read` and `job.read@1.0`.

Grant purpose bindings must include the fixed catalogue purposes used by the
tools. No wildcard operation, scope, capability, workspace, or purpose grant
is permitted.

### 9.2 Installed setup command

Core has no UI requirement. Installation MUST provide an owner/admin command
that performs setup explicitly. The required product surface is:

```text
omnivia mcp configure --host <claude-code|codex> --workspace <workspace-id> --profile restricted|authoring
omnivia mcp revoke
omnivia mcp status
```

These are specified commands, not claims about the current binary. The
implementation may split internal steps, but the installed experience MUST be
equivalent.

`omnivia mcp configure` MUST:

1. resolve the selected workspace using the installed Core service;
2. verify the caller is its owner or an authorized administrator;
3. create or rotate a dedicated MCP principal and private credential;
4. issue only the rights for the selected profile;
5. write an owner-private `omnivia.mcp-config.v1` file atomically at an
   explicit absolute path chosen by the installer or command;
6. optionally print a host configuration snippet with secrets redacted;
7. validate a service handshake and `tools/list` before reporting success.

The host-native entry MUST contain only the installed MCP command and the
explicit path to the protected Core MCP configuration. It MUST NOT contain the
Core credential itself. Setup may be repeated for each supported host without
creating broader workspace authority.

It MUST require explicit `authoring` intent. Installation and upgrade MUST NOT
silently enable authoring. Partial failure MUST leave neither an active grant
without recoverable configuration nor configuration pointing at a nonexistent
principal; compensating revocation or a clearly reported resumable state is
required.

`omnivia mcp revoke` MUST revoke or remove the dedicated principal rights and
invalidate its credential. It MUST be idempotent. It MUST preserve workspace
data, audits, committed mutations, and service-owned jobs.

`omnivia mcp status` MUST report profile, workspace identity, service
reachability, credential/grant validity, and advertised tool count without
printing the credential, raw grant, filesystem internals, or content.

### 9.3 Configuration contract

The existing `omnivia.mcp-config.v1` remains the configuration family and its
existing `mutation_enabled` field becomes the enforced exposure ceiling. The
effective profile is `restricted` when that field is absent or false and is
`authoring` only when it is true **and** the protected setup-intent record has
been confirmed. `allowed_purposes` remains a separate call-time check; it does
not determine `tools/list`. No new unversioned profile field is introduced.
`mutation_enabled: true` is only a local authority ceiling: it is not a scope,
capability, server grant, owner credential, or host approval.

A legacy `mutation_enabled: true` MUST NOT silently activate authoring. Upgrade
must verify a protected prior record of informed authoring intent covering this
expanded surface, workspace, and principal, or obtain fresh confirmation
through the installed setup path. Without that evidence, migration atomically
writes or treats the effective value as false and reports the re-enable step.
Migration does not itself create or widen a grant.

The config file MUST be an owner-private regular file, reject symlinks and
group/world permissions, use an explicit absolute path, and be loaded before
tool advertisement. Updates MUST use atomic replacement with restrictive
permissions from creation. The server MUST refuse unknown profile values,
missing workspace binding, malformed credentials, or unsafe ownership/mode.

## 10. Host and protocol behavior

The release target is the repository-pinned Python SDK set in
`scripts/mcp-wheelhouse-constraints.txt`: `mcp==2.0.0` and
`mcp-types==2.0.0`. The development `.venv` observed during this specification
contained 2.2.0; that mismatch is evidence about the local test environment,
not permission to rely on 2.2.0 behavior. Qualification MUST run against the
pinned release wheelhouse.

The adapter MUST continue to support the repository's pinned MCP protocol
version `2025-06-18`, including initialize negotiation before normal requests.
It MUST preserve newline-delimited JSON-RPC framing for stdio and MUST write
logs only to stderr. stdout is protocol-only.

Tool annotations are descriptive, never an authorization boundary. The three
mutation tools MUST set `readOnlyHint=false` and `destructiveHint=false`; the
eight read tools in the authoring profile (the original six plus two job reads)
MUST set `readOnlyHint=true` and `destructiveHint=false`. A mutation MAY set
`idempotentHint=true` only when repeating the identical complete tool input,
including its stable key, provably returns the settled result. `import_start`
creates durable work and must be described accordingly. Hosts may ignore
annotations, so all security enforcement remains in Core.

Current documentation-only SDK simulations do not qualify real hosts. Release
qualification MUST exercise the installed versions observed at specification
time—Claude Code 2.1.269 and Codex CLI 0.146.0—or the explicitly approved
release replacements on the supported macOS baseline. Current-host docs and
schemas must be captured with their retrieval date because they can change
independently of those installed versions.

No Apple privacy entitlement is inherently required merely to run the local
Core service or stdio MCP server in its own application-support directory.
Permissions may be required by a separate staging or connector path that reads
protected user locations, but those permissions MUST be requested by that
path, not broadened into the MCP mutation surface.

## 11. Failure, audit, and privacy requirements

### 11.1 Pre-commit failures

Invalid input, unavailable workspace, missing scope, missing capability,
purpose mismatch, expired or spent grant, source collision, ACL denial,
idempotency conflict, unsafe configuration, and service-fencing failure MUST
commit no business mutation. Where the canonical mutation coordinator records
a refused execution or audit fact, that record must follow its existing atomic
and privacy rules.

The MCP adapter MUST not retry mutations automatically with a new key. It MAY
retry a clearly pre-dispatch transport connection attempt only when it can
prove Core did not receive the request. Otherwise it must return an ambiguous
outcome and instruct the host to replay the same key.

### 11.2 Post-commit and ambiguous outcomes

If Core commits but the MCP response is lost, same-key replay returns the
stored canonical result after current authorization succeeds. For
`import.start`, job observation then uses the returned job identifier. For
`evidence.capture`, replay cannot create another evidence artifact. For
`memory.create`, replay cannot create another proposed record.

### 11.3 Audit requirements

Every mutation attempt MUST be attributable to the real dedicated MCP
principal and selected workspace. Audit data MUST distinguish operation,
purpose, outcome, fresh execution versus replay, and stable execution identity.
It MUST NOT store the credential or grant token. Direct-capture audit SHOULD
record checksum, byte count, media type, source identifier, and resulting
evidence identity, but MUST NOT duplicate the submitted body into audit rows.

Read tools retain their current read-audit behavior. Diagnostics and MCP error
data must use stable identifiers and sanitization; detailed private traces stay
in owner-controlled local logs.

## 12. Required implementation changes

The implementation is incomplete until all of the following are delivered in
one compatible release:

1. Add canonical contract schemas and generated models for
   `EvidenceCaptureInput` and `EvidenceCaptureResult`.
2. Add `evidence.capture` and `evidence.write@1.0` to the operation and
   capability catalogues with the binding in section 4.
3. Add the production service handler using the shared mutation coordinator,
   exact source resolution, L0 evidence storage, audit, and retrieval barrier.
4. Add a database migration that enforces direct-submission source uniqueness
   without misclassifying or silently collapsing legacy evidence.
5. Project the new operation through generated application schemas and every
   required shared adapter contract.
6. Bump the MCP exposure manifest major version from `1.1` to `2.0`, revise
   its admission rules, and update the server so tool listing is profile-based
   and the three mutation wrappers and two job reads are dispatched correctly.
7. Install `configure`, `status`, and `revoke` owner/admin lifecycle commands
   and safe configuration migration.
8. Update package and installed-product documentation for Claude Code and
   Codex, including profile choice, replay, proposed-memory visibility, staging,
   revocation, and troubleshooting. Package descriptions and server
   instructions must no longer claim the product is universally read-only,
   and release notes must identify the change as an authority expansion.
9. Add unit, integration, conformance, packaging, upgrade, and real-host tests.

The local-file maintenance source-capture code may be refactored to reuse
provider-neutral hashing or persistence primitives, but its path-reading API
MUST NOT become the MCP or application contract.

## 13. Acceptance requirements

### A. Tool discovery and profile isolation

- A fresh or upgraded default installation advertises exactly six restricted
  tools.
- An explicitly configured authoring installation advertises exactly eleven
  tools.
- Every excluded catalogue mutation remains unadvertised and undispatchable.
- A forged tool name, workspace identifier, purpose, capability, or operation
  name fails without reaching a business handler.
- Changing profile requires an owner/admin action and server restart or an
  equally atomic reload; there is no per-call escalation.

### B. Empty-workspace standalone journey

Using a real supported host and an empty temporary workspace:

1. configure the authoring profile through the installed command;
2. confirm eleven tools are visible;
3. call `evidence_capture` with a unique source ID and distinctive text;
4. immediately find the evidence through `evidence_search`;
5. call `memory_create` with both `sources` and `assertion.evidence` referring
   to that direct-submission source;
6. prove the result is proposed-only;
7. prove default `memory_search` does not publish it;
8. prove `memory_search` with `view: "candidates"` finds it for an authorized
   contributor;
9. repeat both mutations with the same key and prove stable results and no
   duplicate business rows;
10. repeat with changed input and prove `idempotency_conflict`;
11. close the MCP session and prove the independently owned Core service stays
    healthy; and
12. retain only a redacted qualification record containing tool names, result
    identities/counts, replay dispositions, and verdicts.

The journey MUST NOT pre-seed application data through Runtime, direct storage,
fixtures, CLI mutation commands, or maintenance capture. Read-only inspection
by the qualification harness is allowed; it cannot become an installed user
dependency or hidden write path.

### C. Capture validation and collisions

Tests MUST cover both content forms, both media types, UTF-8 rejection, strict
base64 rejection, zero bytes, 1 MiB acceptance, 1 MiB plus one rejection,
unknown fields, forbidden fields, exact repeat, same identity/different bytes,
same identity/different metadata, cross-principal collision in one workspace,
same source ID in different workspaces, and a legacy duplicate invariant
failure.

Both transfer encodings must cover Unicode. A harmless text fixture containing
a URL, a local path, and JSON with reserved-looking keys must be accepted as
inert content while instrumentation proves no fetch, file read, execution, or
authority change. The equivalent keys supplied as actual structured fields
must be rejected.

Each successful test must assert blob checksum, L0 disposition, exact source
tuple, audit attribution, idempotency settlement, and lexical retrievability.

### D. Import and job journey

- A valid staged descriptor starts exactly one durable job.
- Same-key replay returns the same job and does not enqueue again.
- A changed descriptor under the same key conflicts.
- `job_get` observes state and terminal result.
- `job_events` paginates an ordered, snapshot-stable event sequence.
- Terminal accounting is internally consistent and each created evidence item
  is retrievable through MCP.
- The MCP principal cannot cancel or retry the job.
- Revocation prevents later MCP observations but does not cancel committed
  service-owned work; the owner operator path can still observe it.

### E. Negative security matrix

Before commit, test missing and wrong workspace membership; missing scope;
missing capability; wrong purpose; absent, expired, reused, or already-spent
grant; credential mismatch; source ACL denial; malformed configuration; unsafe
file mode; symlinked config; idempotency conflict; projection unavailable; and
service-fence loss. Assert no business mutation and no secret/path/content leak.

### F. Atomicity and recovery

Inject failure at every durable step for each mutation. Assert the documented
all-or-nothing boundary. Terminate the service after commit but before MCP
response, restart, replay the same key, and assert the same canonical result.
For capture, also assert projection recovery and exactly one searchable
artifact. For import, assert exactly one job and one execution chain.

Also cover concurrent identical calls, timeout or connection loss after
possible dispatch, deliberate response-correlation mismatch after commit, and
same-key recovery from a new MCP session. These are ambiguous-outcome cases;
tests must prove the original effect exists rather than incorrectly demanding
zero writes.

### G. Setup, upgrade, and revocation

- Fresh install defaults to restricted.
- Legacy `mutation_enabled: false` migrates to restricted without widening.
- Legacy `mutation_enabled: true` requires explicit confirmation and never
  silently activates authoring.
- A legacy true value with a qualifying protected intent record may preserve
  authoring, but only when the existing bounded server grant independently
  satisfies every authorization check.
- Interrupted configure either rolls back or reports a safe resumable state.
- Reconfigure rotates credentials and removes superseded authority.
- Revoke is idempotent and preserves data, audit, replay records, and jobs.
- Status redacts credentials, grants, paths, and content.
- Configuration permissions and symlink defenses are verified on macOS.

### H. Shared conformance and packaging

Run application-contract generation/checks, client/CLI/MCP conformance,
runtime migrations, wheelhouse installation, offline packaging, and installed
service smoke tests. Generated schema projection must be reproducible and clean
after regeneration. The release artifact must use `mcp==2.0.0` and
`mcp-types==2.0.0`, not whichever versions happen to be in a developer venv.

### I. Real-host qualification

For both Claude Code and Codex CLI on a clean supported macOS account:

- install Core from the release artifact;
- configure each profile using documented host settings;
- verify initialize and tool discovery;
- execute the empty-workspace and import journeys;
- exercise same-key recovery after an intentionally interrupted response;
- verify stdout remains valid protocol traffic;
- restart the host and Core service and repeat observation;
- revoke authoring and prove mutation tools disappear or fail closed according
  to the documented restart model.

SDK-only simulations are supplemental and cannot satisfy this gate.

## 14. Non-goals

This milestone does not let MCP initialize or choose workspaces, manage Core's
service lifecycle, administer grants, read arbitrary host files or URLs,
approve its own proposals, publish accepted knowledge, expose the full
governance lifecycle, stage arbitrary imports, add job cancellation, replace
connectors, introduce a secret scanner, or require Desktop or Dev. It does not
promise binary-document direct capture, semantic indexing before capture
returns, or automatic promotion of submitted text.

## 15. Binding register

Every row below is closed for specification. `new` means the implementation
must add a shared contract; `existing` means the named Core behavior is reused
unchanged; `change specified` means the current product lacks the surface and
must implement it as written.

| Area | Decision | State | Implementation authority |
|---|---|---|---|
| profile inventory | fixed six/eleven lists | change specified | MCP config, exposure manifest, server |
| mutation envelope | `{input,idempotency_key}` | change specified | MCP adapter plus canonical request envelope |
| `memory.create` | exact existing input/result and proposed-only semantics | existing | shared contracts and mutation coordinator |
| `import.start` | exact existing staged descriptor and job result | existing | shared contracts and job handler |
| `job.get` / `job.events` | exact existing job reads | existing | shared contracts and job handlers |
| `evidence.capture` | synchronous direct UTF-8 submission | new | shared contracts, catalogue, runtime handler |
| capture authorization | `memory:write`, `evidence.write@1.0`, `content_ingestion` | new | capability and operation catalogues |
| capture identity | direct-submission null-safe source tuple | new | evidence storage migration and handler |
| collision behavior | exact match reuses; any claim mismatch conflicts | new | handler and database constraint |
| capture size/media | 1..1 MiB; text/plain or text/markdown | new | contract and handler |
| capture retrieval | searchable before success or durably recoverable | new | transaction/projection integration |
| replay | workspace-lifetime, append-only existing coordinator | existing | mutation coordinator and migration 0007 invariants |
| replay authorization | fresh current authority and fresh spent grant | existing | service authorization/mutation coordinator |
| evidence-backed memory | canonical source/evidence references | existing | memory handler/source resolution |
| proposed visibility | explicit `view: candidates`; default remains canonical | existing | memory search contracts/handler |
| installed setup | configure/status/revoke owner path | change specified | CLI and installed service administration |
| legacy config | absent/false -> restricted; true requires protected intent or confirmation | change specified | config migration/setup command |
| revocation | blocks later calls, does not cancel committed work | change specified | grants, docs, tests |
| in-flight import | service-owned continuation | existing | durable job worker/fencing |
| real Claude/Codex tests | required release gate | planned | packaging/host qualification |

No row remains `OPEN`, `PLACEHOLDER`, `UNVERIFIED`, or `NEEDS_DECISION` at the
specification level.

## 16. Evidence baseline and verification record

### 16.1 Repository evidence

This revision was resolved against baseline
`990b0f980c633840922170976c73b8f966361eab`. The decisive source evidence was:

- the canonical operation catalogue and generated v1 contracts;
- the MCP exposure manifest, schema projection, server dispatch, and config;
- the runtime mutation coordinator and migration 0007 idempotency/audit
  constraints;
- evidence storage, source resolution, lexical projection, and maintenance
  source-capture code;
- memory and import handlers, durable job storage, and service fencing;
- current CLI and installed-service commands;
- release wheelhouse constraints and MCP package metadata.

At specification time the catalogue contained 27 operations: 15 reads and 12
mutations. The MCP adapter exposed six reads. The CLI operation map covered all
27 even though an older README count said 22; the catalogue and generated
contracts, not that prose count, are authoritative.

### 16.2 Executed baseline checks

The following checks were executed before this document was finalized:

```text
.venv/bin/python scripts/generate-mcp-exposure-schemas.py --check
  PASS — generated schema projection up to date (12 advertised schemas)

.venv/bin/python scripts/check-application-contracts.py
  PASS

focused mutation replay tests
  PASS — memory atomicity, fresh replay grant spending, memory replay,
         and all three import-start adapter variants

focused retrieval/source tests
  PASS — proposed records absent from default view, evidence-backed assertion,
         exact null-safe source lineage, and service evidence search projection
```

The first sandboxed import test attempt could not bind local IPC/HTTP sockets;
the same three parametrized variants passed outside that restriction. This was
an environment limitation, not a product failure.

No real Claude Code or Codex feature journey was executed because the feature
does not yet exist. Those checks are explicitly planned acceptance gates, not
evidence of current completion.

### 16.3 External reference baseline

Implementation and qualification must re-check the following primary sources
at execution time:

- MCP 2025-06-18
  [lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle),
  [tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools),
  and [transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports);
- the official [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  and its pinned 2.0.0 release;
- official [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp);
- official [OpenAI Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
  and the current
  [Codex configuration schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json).

These pages were inspected on 2026-09-12. Current web documentation is not a
substitute for testing the pinned installed host versions.

External documentation describes host mechanics only. It does not override
Core's local authorization, audit, replay, or workspace contracts.

## 17. Delivery sequence and completion status

The implementation SHOULD proceed in this order:

1. contracts, capability catalogue, and schema generation;
2. storage uniqueness/projection migration and runtime handler;
3. shared mutation/retrieval conformance;
4. MCP profile/config/dispatch changes;
5. installed configure/status/revoke lifecycle;
6. package, upgrade, and offline-install validation;
7. real Claude Code and Codex qualification;
8. security and release review.

Specification completion criteria are satisfied by this revision: the tool
inventory, new operation, exact bindings, replay, identity, retrieval,
proposal visibility, setup, revocation, in-flight behavior, host qualification,
acceptance matrix, and implementation ownership are decided.

Feature completion is **not** satisfied. The current baseline remains a
read-only six-tool MCP adapter and lacks `evidence.capture`, profile-dependent
advertisement, persistent MCP authoring grants, and the installed setup
lifecycle. No product claim may state that standalone authoring is available
until every acceptance gate in section 13 passes in the release environment.

## Appendix A. Revision 1.1 review traceability

| Review item | Resolution in revision 1.3 | Qualification |
|---|---|---|
| Zero-write inconsistency after dispatch | Sections 7 and 11 separate pre-dispatch, pre-commit, and ambiguous post-dispatch outcomes. | Journeys E and F inject both refusal and committed-but-untrusted outcomes. |
| Forbidden arguments versus ordinary content | Sections 6.2 and 14 keep structured controls forbidden and submitted text inert. | Journey C sends the same reserved-looking material as content and as forbidden fields. |
| Capture/search and proposed visibility | Sections 6.5 and 8 bind lexical evidence retrieval and explicit candidate view. | Journeys B and C match stable evidence/record identities without promotion. |
| Durable replay and source identity | Sections 6.4 and 7 separate operation, source, and byte identity. | Journeys C and F cover collision, concurrency, restart, and no duplicates. |
| Installed enablement and revocation | Section 9 binds explicit owner setup, server grant, cached-session denial, and operator observation. | Journey G proves six-to-eleven-to-six discovery and current authority. |
| Legacy true flag | Section 9.3 requires protected intent or fresh confirmation and never creates grants during migration. | Journey G covers absent, false, qualified true, and unqualified true. |
| Evidence-backed proposed memory | Section 8.2 verifies support through canonical source and evidence references. | Journey B is mandatory, not deferred. |
| Completion honesty | Sections 12, 13, 16, and 17 separate specified changes, actual checks, and release qualification. | Final review preserves the incomplete feature status. |

## Appendix B. Worked direct-capture call

```json
{
  "input": {
    "source_native_id": "meeting-notes-2026-09-12",
    "media_type": "text/markdown",
    "text": "# Decision\n\nUse a two-year warranty.",
    "source_version": "v1",
    "observed_at": "2026-09-12T00:00:00Z"
  },
  "idempotency_key": "capture-meeting-notes-2026-09-12-v1"
}
```

Expected sequence: authenticate the configured principal; validate the closed
wrapper and canonical input; authorize `memory:write`,
`evidence.write@1.0`, and `content_ingestion`; issue and spend a fresh grant;
claim the replay key; resolve source identity; hash and store the bytes; record
L0 metadata, provenance, audit, execution, and outcome atomically; publish or
durably schedule the lexical projection; verify the retrieval barrier; return
the canonical result.

## Appendix C. Example private configuration

This uses the existing managed-local configuration shape. The setup command
must substitute the real explicit installation-state path and identifiers:

```json
{
  "format": "omnivia.mcp-config.v1",
  "principal_id": "mcp-author-ws-01HXYZ",
  "allowed_workspace_ids": ["ws_01HXYZ"],
  "default_workspace_id": "ws_01HXYZ",
  "allowed_purposes": [
    "workspace_inspection",
    "knowledge_retrieval",
    "memory_authoring",
    "content_ingestion",
    "job_observation"
  ],
  "mutation_enabled": true,
  "service_mode": "managed_local",
  "installation_state": "/OWNER_PRIVATE/omnivia-core/installation-state"
}
```

The installed command chooses and protects real paths and credential storage.
Documentation and status output MUST use placeholders or redaction; examples
must not disclose a user's actual home path, socket, workspace, or secret.

## Appendix D. Host and protocol compatibility matrix

| Component | Selected baseline | Evidence in this revision | Required release evidence |
|---|---|---|---|
| MCP protocol | `2025-06-18` | Current MCP end-to-end tests initialize with this version; official versioned lifecycle/tools/transports reviewed. | Installed initialize, tools/list, calls, errors, restart, and stdout framing. |
| Python MCP SDK | release pins `mcp==2.0.0`, `mcp-types==2.0.0` | Constraints verified; local `.venv` was 2.2.0 and is not treated as pinned-runtime proof. | Offline wheelhouse install and full MCP tests with exactly the pinned wheels. |
| Claude Code | 2.1.269 observed installed | Version command and official MCP documentation reviewed. | Real installed restricted and authoring journeys, reconnect, replay, and revocation. |
| Codex CLI | 0.146.0 observed installed | Version command, official MCP documentation, and current config schema reviewed. | Real installed restricted and authoring journeys, reconnect, replay, and revocation. |
| macOS | 26.5.2 build 25F84, arm64 | Host baseline observed; no protected-location access is inherent in the design. | Clean-account install, owner-private config checks, service restart, and permission prompts limited to any separate staging path. |

An implementation may qualify a later host version only when the release record
states the approved replacement and repeats the same evidence. A redirecting
documentation page is not a version-selection decision.

## Appendix E. Requirement-to-acceptance map

| Rule | Acceptance | Expected state/result | Audit and authority | Duplicate guarantee |
|---|---|---|---|---|
| R1 `evidence.capture` | B, C, E, F, H | immutable L0 artifact; lexical hit matches `evidence_id`; validation errors are typed | real MCP principal/workspace; mutation audit contains claims, not body | one artifact/blob reference per identity and replay |
| R2 mutation replay | B, D, F | same key/input returns stored result; changed input conflicts; restart recovers | current authority and a fresh spent grant on every replay | one artifact, record, or job under concurrency and ambiguity |
| R3 source identity | C, E | exact source and claims reuse; disagreement returns canonical `conflict`; legacy duplicates fail invariant | lookup follows workspace authorization and non-disclosure | database uniqueness for new direct submissions |
| R4 retrieval/visibility | B, C | capture is immediately lexically searchable; proposal appears only in candidate view | normal read scopes, capabilities, purpose, and ACL | retrieval never republishes or promotes data |
| R5 setup/grants/migration | A, E, G, I | restricted default; explicit enable; status redacted; revoke confirmed | existing owner/admin path and bounded server grant; host approval is not authority | reconfigure rotates/removes superseded authority rather than adding ambient grants |
| R6 in-flight imports | D, G, I | committed job continues; MCP observation denied after revoke; owner path observes | worker uses service ownership/fencing; revocation is not cancellation | replay returns the same job and never enqueues twice |
| R7 evidence-backed proposal | B, H | canonical source and evidence references survive on proposed record | evidence ACL checked; actor claim cannot replace authenticated authority | no MCP-private evidence link or second memory record |

Here A-I refer to the subsections of section 13. There is no R7 deferral:
repository inspection verified the canonical relationship and made the journey
mandatory.

## Appendix F. Implementation paths and expected tests

Existing paths are evidence anchors; proposed paths are implementation targets
and may be adjusted only to follow the repository's established generator
layout.

| Change | Existing or proposed location | Expected tests |
|---|---|---|
| operation and schemas | existing `contracts/application/v1/schemas/operations.schema.json`, `evidence.schema.json`, generated `src/omnivia_core/contracts/v1/` | schema/meta-schema checks, semantic vectors, application wire conformance |
| capability | existing application capability catalogue and generators | catalogue integrity and adapter capability-denial vectors |
| runtime capture | proposed handler beside `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/`; reuse mutation/source/evidence services | unit validation, fenced atomicity, replay, collision, ACL, recovery |
| source uniqueness | proposed runtime migration after the current migration head; existing evidence table originates in `0008_blobs_staged_sources_and_evidence.sql` | migration upgrade/rollback refusal, legacy duplicate detection, concurrent inserts |
| lexical barrier | existing evidence search projection migration/service code | commit/publication fault injection and immediate content-query match |
| MCP profiles | existing `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py`, `configuration.py`, `server.py`, and generated projection | six/eleven discovery, closed wrappers, dispatch/error encoding, cached-session revocation |
| installed lifecycle | existing `packages/omnivia-core-cli/` and installed service administration | configure/status/revoke, owner authority, partial failure, migration, redaction |
| packaging/hosts | existing wheelhouse constraints and MCP interoperability docs/tests | pinned offline wheelhouse plus real Claude Code and Codex matrices |

New conformance fixtures MUST be generated from canonical contracts. Tests must
not hand-maintain an MCP-only copy of the operation schema or use Runtime
pre-seeding to pass the empty-workspace authoring journey.

## Appendix G. Final review report

| Review category | Result |
|---|---|
| six/eleven inventories and exclusions | PASS — one fixed inventory; three mutations and two job reads added only under the trusted ceiling |
| scopes, capabilities, purposes, audit, completion | PASS — exact existing bindings recorded; complete proposed capture metadata and error set recorded |
| Core sole-writer and inert content | PASS — MCP remains an adapter; maintenance path is not exposed; content cannot select authority or I/O |
| L0/proposed governance | PASS — capture creates only L0; memory create remains proposed; no implicit approval |
| replay, concurrency, retention, authorization | PASS — existing workspace-lifetime append-only coordinator, current authority, fresh grant, and no-duplicate recovery are bound |
| source identity and collisions | PASS — workspace/direct-submission/null locator/null retrieved tuple; exact-claims reuse; canonical conflict; legacy duplicate refusal |
| retrieval and recovery | PASS — content query plus returned evidence identity; candidate view explicit; projection recovery cannot produce false success |
| setup, migration, cached sessions, in-flight work | PASS — installed owner path specified; legacy true requires intent; Core enforcement does not depend on host refresh; jobs continue |
| examples and document checks | PASS — JSON examples parsed; required decision assertions and repository contract/schema checks passed |
| actual versus planned evidence | PASS — baseline checks are listed separately; real-host and feature tests remain planned |
| mandatory blockers | NONE at specification level |

The review corrected two draft inconsistencies before sign-off: capture input
uses canonical `source_native_id` and the optional version/temporal claims, and
the existing `mutation_enabled` configuration field remains the enforced
ceiling instead of adding an unversioned duplicate profile field.

## Appendix H. Revision 1.3 change log

- Executed the revision 1.2 finalisation work order against a pinned repository,
  SDK, protocol, host, and operating-system baseline.
- Closed R1-R7, including a complete proposed `evidence.capture` contract and a
  verified supported disposition for evidence-backed proposed memory.
- Bound replay to existing durable workspace-lifetime machinery and source
  identity to the existing null-safe evidence lineage model.
- Defined installed configure/status/revoke behavior, legacy intent migration,
  cached-session enforcement, and in-flight import observation.
- Added the compatibility matrix, acceptance mapping, implementation paths,
  executed-check record, and final adversarial review.
- Preserved the distinction between specification completion and the currently
  incomplete standalone authoring/ingestion feature.
