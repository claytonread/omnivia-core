# omnivia-core-mcp

The Model Context Protocol server for OmniVia Core: a stdio MCP server that
gives an AI host access to one local OmniVia Core workspace, under one of two
fixed profiles. `restricted` is six read tools. `authoring` is those six plus
three writes and two job reads, and it is reached only by an explicit,
separately recorded human decision — never by default, never by upgrade, and
never by anything a model can say.

Built on the official Model Context Protocol Python SDK v2 (owner resolution
004, R004-05). There is no bespoke JSON-RPC or MCP stack in this package, and
no FastMCP dependency — the official SDK is the sole MCP framework dependency.

## The two profiles

`tools/list` is a curated, versioned allow-list (R004-06), not a projection of
the operation catalogue. `OPERATION_CATALOGUE` holds twenty-eight operations;
`manifest.py` names six of them in `restricted` and eleven in `authoring`. A
newly registered Core operation stays absent from MCP until somebody adds it
there and tests it.

Manifest version is `2.0` (`MANIFEST_VERSION`), and it is the exposure
surface's version rather than the distribution's. `1.1` was the six reads and
had no notion of a profile, so a host that cached a `1.1` listing has cached
the whole surface — the major bump says so.

`server.EXPECTED_TOOL_COUNT` fixes the count per profile: `restricted` 6,
`authoring` 11. A session advertises one profile's whole inventory, in order,
and never a filtered one.

### `restricted` — the six reads

| # | Tool | Operation | Purpose | Scopes | Capability |
|---|---|---|---|---|---|
| 1 | `workspace_inspect` | `workspace.inspect` | `workspace_inspection` | `workspace:read` | `workspace.read` ≥ 1.0 |
| 2 | `evidence_search` | `evidence.search` | `knowledge_retrieval` | `memory:read` | `evidence.read` ≥ 1.0 |
| 3 | `knowledge_search` | `knowledge.search` | `knowledge_retrieval` | `memory:read` | `knowledge.read` ≥ 1.0 |
| 4 | `memory_search` | `memory.search` | `knowledge_retrieval` | `memory:read` | `memory.read` ≥ 1.0 |
| 5 | `graph_traverse` | `graph.traverse` | `knowledge_retrieval` | `graph:read` | `graph.read` ≥ 1.0 |
| 6 | `context_pack_build` | `context_pack.build` | `knowledge_retrieval` | `memory:read` | `context_pack.build` ≥ 1.0 |

Every one declares `side_effect: none` and `audit_category: read` in the
operation catalogue. This profile is read-only, and it is the default and the
fallback: every manifest function defaults its `profile` argument to
`restricted`, so a caller that has not been taught about profiles gets the six.

### `authoring` — the same six, then five

| # | Tool | Operation | Purpose | Scopes | Capability | Side effect |
|---|---|---|---|---|---|---|
| 7 | `memory_create` | `memory.create` | `memory_authoring` | `memory:write` | `memory.write` ≥ 1.0 | create |
| 8 | `evidence_capture` | `evidence.capture` | `content_ingestion` | `memory:write` | `evidence.write` ≥ 1.0 | create |
| 9 | `import_start` | `import.start` | `content_ingestion` | `memory:write` | `ingestion.import` ≥ 1.0 | create, always returns a job |
| 10 | `job_get` | `job.get` | `job_observation` | `job:read` | `job.read` ≥ 1.0 | none |
| 11 | `job_events` | `job.events` | `job_observation` | `job:read` | `job.read` ≥ 1.0 | none |

`AUTHORING_MANIFEST` is `RESTRICTED_MANIFEST` concatenated with these five, so
the two profiles cannot drift in the operations they share. The three mutations
are a literal set (`ADMITTED_MUTATIONS`), not a rule over catalogue metadata: a
fourth mutation cannot arrive because a contract gained a field or an operation
changed its audit category.

Read-first is enforced at import. `_admit` refuses any entry that is neither a
catalogue read (`side_effect="none"` **and** `audit_category="read"`) nor one of
those three names, and the package fails to load rather than shipping a
destructive tool with a reassuring comment. Both profiles are built whichever
one a server selects, so a broken authoring binding cannot hide behind a
restricted install.

Scopes, the capability identifier and its minimum version, and the idempotency
hint are read off the catalogue entry rather than restated here. A model can
neither supply nor override the principal, the workspace, the scopes, the
purpose, the capability, or the service endpoint. `tools/list` is deterministic
for a given package version and profile.

### Never exposed as model-callable tools

Service start, stop, health, readiness, status and discovery; bootstrap,
workspace creation, selection, deletion and enumeration; grant administration;
candidate approval or rejection, publication, supersession and every other
governance decision; `job.cancel` and `job.retry`; chat, workflow and connector
mutation; unrestricted filesystem path selection; and administrative
configuration.

These are not merely unadvertised — the allow-list is the only lookup the call
path has, so an operation absent from it is not callable.

## How a profile is chosen

`configuration.effective_profile` decides once, at startup, from two
independent conditions. Both are required and neither is sufficient.

- **The ceiling** is `mutation_enabled` in the trusted configuration document.
  Absent or `false` is `restricted`, always. No argument, prompt, purpose or
  host setting widens it.
- **The floor** is the protected authoring admission, which only durable,
  installation-owned state can raise. It answers one question: did a human
  owner or administrator explicitly record informed authoring intent for
  *exactly* this principal and this workspace, and does that authority hold
  right now?

So `mutation_enabled: true` on its own selects nothing — an editor who flips
that byte has raised a ceiling over an empty room. It is a local authority
ceiling and not a scope, a capability, a server grant, an owner credential or a
host approval.

Everything fails closed: no admission, an admission that answers anything but
`True`, and an admission that raises all give `restricted`. The decision is
made after the service is connected and after its descriptor is proved to name
the selected workspace, and before `stdio_server()` is entered and before one
tool is advertised — so `tools/list` and the call path read one frozen decision
that no prompt and no argument can reach.

## Setup, without a UI

Core has no UI requirement. Three installed commands are the whole owner-facing
surface, and there is no fourth: no `mcp list`, no `mcp rotate`, no `mcp grant`.

```text
omnivia --installation-state <ABSOLUTE_PATH> mcp configure --host <claude-code|codex> \
                                                           --workspace <ID> \
                                                           --profile <restricted|authoring>
omnivia --installation-state <ABSOLUTE_PATH> mcp status [--host <claude-code|codex>] [--json]
omnivia --installation-state <ABSOLUTE_PATH> mcp revoke [--host <claude-code|codex>]
```

`--installation-state` is the installation root whose service is to be called
and is required for every `omnivia` command. The root `--workspace-id` is not:
this family administers an installation rather than calling one workspace's
service, and `configure` binds its workspace with its own `--workspace`.

`--host` and `--profile` are closed vocabularies refused during the parse —
`claude-code` and `codex` are the only hosts this command configures, and the
same two are enforced by a `CHECK` constraint on the service's own setup table.
There is no scope, capability, purpose, principal, path, endpoint or credential
flag, and there must not be: each of those is the service's to derive or the
command's to place.

All three are owner/administrator commands. The administrator is the *service's*
determination and never the caller's claim: an administration control carries no
credential and no field a caller could put a role in — reaching the local
endpoint is the operating system's owner-private proof. A caller the service
does not recognise as a local installation administrator gets one fixed sentence
on stderr, `installed MCP administration requires a local installation
administrator`, and exit 3, with nothing written to either store.

The dedicated principal is minted by the service, never named by the caller. Its
identifier is required to carry an `mcp-` prefix by a database constraint, so
the service worker identity, an installation secret and a human's own identifier
are refused structurally rather than by the care of whoever calls it — R004
section 9.1's "MUST NOT reuse" as a constraint. The bearer itself exists once,
is handed to `configure` once, and is filed in this installation's protected
store under an opaque reference; no later call returns it again.

The order is:

1. **Have a workspace.** MCP creates nothing. A server attached to an
   installation with no workspace refuses with an instruction to run
   `omnivia init`, and creates nothing on the way out. (`omnivia init` is a
   separate authorised command and is not part of this frozen CLI surface yet;
   today an installation's workspace comes from the installed bootstrap path.)
2. **Have a service, or let the command start one.** `configure` and `revoke`
   change durable authority, so they start a service if none is running.
   `status` reports and starts nothing — a service that is not running is part
   of what it reports. `omnivia service start|stop|status` is the explicit
   lifecycle surface.
3. **`omnivia mcp configure`.** It resolves the workspace through the installed
   service, checks the caller is its owner or an authorised administrator,
   creates or rotates a dedicated MCP principal and its private credential,
   issues only the selected profile's rights, writes the owner-private
   `omnivia.mcp-config.v1` document atomically at an installation-chosen
   absolute path, and validates a real handshake and `tools/list` before it
   reports success. Then it prints the host snippet and exits 0.
4. **Paste the snippet into your host's own configuration.** `configure` prints
   it rather than writing it: that file is yours, its other servers are yours,
   and a setup command that rewrote it could break everything else in it.

Repeat per host. Setup for a second host does not create broader workspace
authority.

### What `configure` prints

The snippet is two members and no third — the installed MCP executable and the
absolute path to the protected configuration. **No credential**, by
construction: there is no parameter here a secret could be passed through.

Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "omnivia-core": {
      "command": "omnivia-core-mcp",
      "args": [
        "--config",
        "<ABSOLUTE_PATH_TO/omnivia-mcp.json>"
      ]
    }
  }
}
```

Codex (`config.toml`):

```toml
[mcp_servers.omnivia-core]
command = "omnivia-core-mcp"
args = ["--config", "<ABSOLUTE_PATH_TO/omnivia-mcp.json>"]
```

`<ABSOLUTE_PATH_TO/omnivia-mcp.json>` is a placeholder for the path the real
command prints. That path is installation-owned and deterministic — a function
of the trusted installation root and the closed host word — and there is no
flag that chooses it, because a configuration path a caller could name is a
configuration this installation did not place.

`command` and `args` are the whole of an accepted entry. This server reads no
environment variable and accepts no URL, header, bearer token, `env`, `cwd` or
transport selector in a host configuration, and has nothing to add to one. A
client written directly against the official Python SDK passes the same command
and arguments as stdio server parameters and needs no file at all.

### What `status` prints

One redacted row per host, as `name=value` pairs sorted by name, or the same
rows as a document under `--json` (`{"hosts": [...], "mcp_status_version": 1}`).
Each row carries exactly ten members and no others:

| Member | Values |
|---|---|
| `host` | `claude-code` or `codex` |
| `service` | `reachable`, `unreachable` |
| `configuration` | `absent`, `present`, `unusable`, `mismatched` |
| `credential` | `absent`, `present`, `unusable` |
| `grant` | `active`, `revoked`, `absent`, `unknown` |
| `profile` | `restricted`, `authoring`, absent |
| `authoring_intent` | true, false, absent |
| `advertised_tool_count` | 6, 11, or absent |
| `principal_id` | the dedicated principal's identifier, or absent |
| `workspace_id` | the bound workspace's identifier, or absent |

Nothing printed is a credential, a raw grant, a salt, a digest, a path, an
endpoint, a peer's words or any workspace content. What cannot be answered
without a running service — whether a grant is live, what the server would
advertise — is `unknown` or absent rather than guessed. Exit status is 0 when
the service was reachable and 1 when it was not, independently of any one host's
health.

The `--json` form renders absent values as `null` and booleans as `true`/`false`;
the plain form is Python's own `str()`, so the same members read `None`, `True`
and `False` there.

### Rotation and reconfigure

Running `configure` again is the rotation path; that is why there is no
`mcp rotate`. A repeated `configure` rotates the dedicated principal's bearer,
removes superseded authority, and rewrites the local half. A `configure` that
changes nothing prints the same snippet and is still a success: the requested
state is live and the caller asked what to put in their host configuration.

Partial failure is compensated in one order and only that order: invalidate the
authority that was just minted **first**, then remove the local half, and
remove nothing at all when the invalidation was not confirmed. A local half
removed under a live grant is the state R004 section 9.2 forbids. When a
revocation could not be confirmed, every safe artifact stays where it is, the
command exits non-zero having printed nothing to stdout, and `status` reports
what is actually there.

### Upgrade

- A configuration with **no** `mutation_enabled` member is `restricted`; the
  member defaults to `false` when absent.
- `mutation_enabled: false` is `restricted`.
- A legacy or hand-edited `mutation_enabled: true` is **still** `restricted`
  unless the protected authority independently admits this principal and this
  workspace on this startup. Installation and upgrade never silently enable
  authoring.
- Authoring requires explicit intent, and choosing `--profile authoring` *is*
  that explicit act — it is what the command records as the protected
  authoring-intent decision, which is why there is no second flag saying the
  same thing.
- A pre-setup managed-local document that carries no `credential_reference` is
  refused at startup with one fixed sentence rather than downgraded. Re-run the
  installed setup for that host to write the reference.

### Revocation

`omnivia mcp revoke` invalidates the dedicated principal's authority at the
service, and only then drops the local half — never the reverse, because a local
half removed under a live grant is the state that must not exist. It runs for
every host unless `--host` narrows it, prints `revoked <host>` per host, and
exits 0 when every named host's control was answered and 1 when one could not
be reached (that host's local half is then left exactly as it stands). It is
idempotent: a second run over an already-revoked or never-configured host prints
the same line and changes nothing.

It preserves workspace data, audits, committed mutations, replay records and
service-owned jobs — it revokes a principal rather than deleting anything. It
also never edits a host's own configuration file; only this installation's
protected copy is removed, so remove the `omnivia-core` entry from `.mcp.json`
or `config.toml` yourself if you want the host to stop launching the server.

**Revocation is not cancellation.** An import job that was durably created
before the revoke continues under Core's own service-owned worker identity and
fencing. The owner observes it afterwards through the installed operator path,
including the canonical CLI job reads. Job cancellation is outside this
milestone in every surface MCP has.

What revoke does to a running MCP session, precisely:

- **Calls stop working on the next call, not at the next restart.** The bearer
  is resolved once at startup as a check and then *not kept*: the session
  carries the store and the opaque name and asks again on every call.
- **The refusal happens here, before dispatch.** Once this installation no
  longer holds the material the session would have to present, the call is
  refused locally and never reaches Core — a refusal carrying a service envelope
  would mean the call had still been dispatched, which is the thing revocation
  must stop. Same-key replay is refused on the same terms: R004 section 7 is
  explicit that replay is not an authorization bypass.
- **The advertised inventory does not change until a restart.** The profile is
  frozen before the first tool is advertised, so a session that was already
  running keeps listing the eleven and fails each authoring call closed.
- **The restart refuses.** Once the reference no longer resolves to a usable
  credential, the server exits non-zero at startup rather than falling back to
  an unauthenticated session — the local endpoint would admit one as the
  service's own principal, and that is the wrong identity to run a model's
  calls under.

## Calling an authoring tool

Every mutation tool takes one closed outer object:

```json
{"input": {}, "idempotency_key": "host-generated-stable-key"}
```

`input` is exactly the canonical Core operation input; `idempotency_key` is the
canonical request-envelope key. Both are required and no third outer property
is accepted. The unwrapped input goes through the *public* decoder
`omnivia_core.contracts.v1` publishes for that operation, and the key through
`is_idempotency_key`, before the service client is reached — so a refusal is
the canonical contract's, not a bound transcribed here that could go stale.

### Fresh authorization and same-key replay

Each attempt obtains a fresh, server-issued mutation grant. A grant is bound to
the principal, workspace, operation, purpose, scopes, capabilities, key and
input fingerprint of the request it was issued for, is single-use, pins a
fencing generation, and expires on a short monotonic window (60 seconds today).
A leadership change — a service restart that re-acquires the workspace at a new
generation — invalidates every outstanding grant regardless.

The idempotency scope is the existing four-part tuple of principal, workspace,
operation and key, and the request digest covers the canonical operation input
(plus purpose, scopes and required capabilities) and deliberately not the
request, correlation or trace identifiers, the deadline, or the client:

- **same key, same input** → the stored canonical result. The business mutation
  is not repeated, no second job is created, and no second business audit is
  emitted.
- **same key, different input** → `idempotency_conflict`.

Replay is not an authorization bypass. Before a stored result is revealed, Core
re-evaluates current workspace membership, scope, capability and purpose and
durably spends a fresh grant, exactly as a first attempt does — an honest replay
runs no domain code but is not free. A revoked or downgraded principal cannot
use an old key to recover a result it may no longer observe. The grant's
60-second lifetime does not bound the replay window: claims and outcomes are
append-only and retained for the lifetime of the workspace, and they survive a
service restart and a brand-new MCP session because they are durable rows rather
than session state.

A *different* key carrying the same content is a distinct request, not a replay,
and settles on its own. Content-addressed identity still applies underneath it:
a second `evidence_capture` of byte-identical content under a new key reports
`already_captured` rather than `created`, and writes no second artifact.

**On an ambiguous outcome, replay the same key.** A lost response is not a
reason to invent a new one — a new key is a new mutation request. The adapter
never retries a mutation automatically under a new key, and returns an
ambiguous outcome telling the host to replay rather than guessing.

### Proposed memory is invisible by default, and that is not a failure

`memory_create` can create only a **proposed** record. It cannot assert
accepted authority, currentness, approval, publication, supersession or a
server-owned record identifier.

`memory_search` without an explicit `view` returns `current_canonical`, so the
record it just created is **not** in that answer. A caller authorised for
candidate views retrieves it explicitly:

```json
{"query": "warranty period", "view": "candidates", "limit": 20}
```

The asymmetry is intentional. Absence from the default view is governance
working, not a mutation that failed.

Evidence-backed authoring needs no second operation: `MemoryCreateInput.sources`
carries canonical `SourceReference` values and `assertion.evidence` carries
canonical `EvidenceReference` values, exposed unchanged with no private
`evidence_id` shortcut. The actor fields in an assertion are claim provenance,
never authorization — Core binds the real principal independently.

### `import_start` names a staged descriptor, and cannot name anything else

`ImportStartInput` carries exactly one member, `source`, an
`ImportSourceDescriptor` of five required fields and one optional
`source_version`:

```json
{
  "input": {
    "source": {
      "staged_source_ref": "<SERVER_ISSUED_STAGING_HANDLE>",
      "source_kind": "archive",
      "content_checksum": "sha256:<64 hex characters>",
      "content_length_bytes": 4096,
      "media_type": "application/zip"
    }
  },
  "idempotency_key": "<HOST_CHOSEN_STABLE_KEY>"
}
```

The handle must already have been produced by an installed, trusted Core
staging path. **MCP does not stage content.** The descriptor is
provider-neutral by construction and carries no filesystem path, URL, inline
archive, credential, connector configuration, parser implementation name, or
runtime/storage option, so an import cannot be steered from the wire into
reading something the server did not already stage. It is immutable: the
descriptor `import.start` accepted is the exact descriptor the completion
result reports back.

This is the general rule and not a property of one tool. **MCP accepts no
arbitrary path, URL, credential, connector setting, parser choice, storage
location or runtime flag anywhere on its surface.** `evidence_capture` carries
its content in the call for the same reason.

Authority arguments are refused by name rather than silently dropped:
`principal_id`, `workspace_id`, `purpose`, `scopes`, `grants`,
`required_capabilities`, `mutation_enabled`, `endpoint`, `credential_reference`
and the rest of `server.RESERVED_ARGUMENTS` are checked on the outer call
object and again inside a mutation's unwrapped `input`, so hiding one a level
down is not a bypass. Any other key the advertised schema does not declare is
refused too — the advertised schemas are closed, not a minimum.

### Jobs

`import_start` always answers with a job rather than a finished result. Follow
it with `job_get` (the current state of one job, by identifier) and
`job_events` (one page of the ordered event history, oldest first, continuing
from the page metadata the previous response returned — snapshot-stable,
bounded by the catalogue's page maximum, and not a transport stream).

Job states are `queued`, `running`, `succeeded`, `failed`, `cancelled`. Both
job tools are read-only observations authorised by `job:read` and
`job.read@1.0`. They confer no permission to start, cancel, retry or alter a
job, and neither `job.cancel` nor `job.retry` is exposed in any profile.

## Trusted configuration

`omnivia_core_mcp.configuration` implements the immutable
`omnivia.mcp-config.v1` model and its explicit-path reader. The reader admits at
most 65,536 bytes, rejects duplicate or unknown fields, follows no symlink, and
requires a regular owner-private file whose identity stays unchanged throughout
the bounded read. Configuration supplies only an opaque credential reference;
it is never a credential store.

Owner-private is proved from the open descriptor on both platform families.
POSIX checks the owner and the group/other mode bits. Windows converts the
descriptor to a handle and proves, through `advapi32` alone, that the file's
owner SID is this process's token user and that the DACL is present and grants
no other principal; an unrecognised access-allowed ACE form or any API
inconsistency refuses the file. The server reads this document before opening
stdio and uses it as the only source of principal, workspace, allowed purposes,
service location, mutation ceiling, and (for remote mode) credential reference.

The document `configure` writes carries exactly these members:
`allowed_purposes`, `allowed_workspace_ids`, `credential_reference`,
`default_workspace_id`, `format`, `installation_state`, `mutation_enabled`,
`principal_id`, `service_mode`. `credential_reference` is the opaque *name* the
service filed the bearer under, never the bearer. There is no profile member,
and there must not be one: R004 section 9.3 introduces no new unversioned
profile field, and `mutation_enabled` is the ceiling.

`allowed_purposes` is a separate call-time check and does not determine
`tools/list`. Its value is fixed by the profile: `restricted` allows
`knowledge_retrieval` and `workspace_inspection`; `authoring` allows those plus
`content_ingestion`, `job_observation` and `memory_authoring`.
`verify_installed_setup` compares a written configuration's purposes against the
running profile's manifest before `configure` may report success, so the two
lists cannot drift.

There is no default configuration path, environment lookup, or `--home`
fallback:

```bash
omnivia-core-mcp --config /absolute/path/to/omnivia-mcp.json
```

A managed-local document names an absolute `installation_state`; the server
delegates the whole attach/start/reconnect decision to the shared
`connect_managed_local` client operation. Only that client operation may invoke
`omnivia-core-service --managed-start`, and only when no descriptor is
published. The MCP package owns no launcher, path convention, or service argv;
it requires a live connection before advertising any tool.

A managed-local document written by the installed setup path additionally names
an opaque `credential_reference`: the name under which that installation filed
the dedicated MCP principal's bearer in its own protected store. The document
carries the name and never the material, and never the store's location — the
store derives that from `installation_state` alone. The server resolves the
bearer through the shared client's `InstalledCredentialStore` and issues every
application request over the local endpoint's authenticated control, so calls
are dispatched under that dedicated principal rather than the service's own.

A managed-local document with **no** reference is the pre-setup shape, and the
server refuses to start on it with a fixed sentence: an installed server
presents its own bearer or does not run. There is no unauthenticated
managed-local session, because the local endpoint would admit one as the
service's own principal. The configuration reader still parses such a document;
it is `connect` that refuses, before a service is started and before MCP
initialization.

`server.verify_installed_setup(path)` is the check R004 section 9.2 step 7
requires an installed setup to pass before it reports success, and it lives here
because every part of it does. It is a **real MCP exchange with a real child**:
it runs this package's own entry point as a subprocess — this interpreter,
`-m omnivia_core_mcp.server --config <absolute path>`, and nothing else on the
command line — drives it with the official SDK's `stdio_client` and
`ClientSession`, and completes `initialize` and `tools/list` over the transport
a host would use. The peer must identify itself as `omnivia-core` at this
package's version; the advertised inventory must be exactly one profile's own
tools, in order, at the `EXPECTED_TOOL_COUNT` that profile fixes — six or
eleven; and the document's `allowed_purposes` must be exactly that profile's
manifest purposes. Which profile is in force is read off the inventory the child
advertised, never assumed from the document, so a `mutation_enabled: true`
configuration the protected authority declines to admit is refused here.

No credential appears in the child's argument vector or its environment: it is
told a path and resolves its own bearer from this installation's protected
store, exactly as it does under a host. The whole exchange is bounded, the
child's stderr is discarded at the descriptor, and the child is terminated on
success and on every failure. Every refusal is one of this module's fixed,
payload-free sentences — nothing a peer, an exception, a path or a credential
contributed. `omnivia mcp configure` calls this and holds none of it. The CLI
distribution does not depend on this one, so it resolves this function by name
at the moment it is needed and fails closed when it is absent.

Remote `service_client` mode names an HTTPS origin and an opaque credential
reference. The console entry point has no ambient credential resolver and
therefore refuses remote mode. An embedding host may inject its trusted resolver
into `connect`; the resulting credential is origin-bound, cached only by the
shared client cache, and cleared on failed startup and session shutdown. A
remote configuration is `restricted` whatever its `mutation_enabled` byte says:
its credential is the injecting host's rather than this installation's, so it
has no way to ask the protected authority anything.

## Schemas

Each tool advertises both an `inputSchema` and an `outputSchema`, and both are
**self-contained**: the canonical Application Contract v1 definition verbatim,
plus its complete transitive `$defs` closure, with every
`https://contracts.omnivia.dev/...` reference rewritten to a local `#/$defs/...`
one. No advertised schema needs network resolution. A mutation tool's advertised
input is the one shape the manifest *composes* rather than projects — the closed
outer `{"input": ..., "idempotency_key": ...}` — and both halves are still
generated, so the wrapper adds a shape and transcribes no constraint.

They are generated, never transcribed:

```bash
python scripts/generate-mcp-exposure-schemas.py           # regenerate
python scripts/generate-mcp-exposure-schemas.py --check    # gate (preflight, Core acceptance)
```

`src/omnivia_core_mcp/generated_schema_projection.py` is the emitted artifact and
is generator-owned; edit the canonical schemas and regenerate instead. It is a
checked-in module rather than a read of the packaged canonical schemas because
those are force-included into the `omnivia-core` *wheel* and absent from an
editable install — reading them would make `tools/list` depend on how Core was
installed. The generated module is present, and identical, in both.

### Results

A successful call returns `structuredContent` equal to the contract-encoded
operation result, plus exactly one JSON text item whose parsed value equals that
same document. A failure — an unknown tool, an unadvertised argument, an
unreachable service, a refusal from the service — is an MCP tool error carrying a
readable message and **no** `structuredContent`.

Tool annotations are descriptive and never an authorization boundary, and each
is read off the catalogue rather than asserted. The three mutations set
`readOnlyHint=false`; the eight reads in `authoring` set `readOnlyHint=true`.
Nothing sets `destructiveHint=true` — none of the eleven deletes or overwrites,
because the three mutations create and supersession and cancellation are not
exposed at all. `openWorldHint` is false throughout: one local workspace this
server is already attached to.

`idempotentHint` is the catalogue's `safe_to_retry`, so it is **false** on the
three mutations. That is not advice against replay. It says a bare repeat is not
inherently safe; a repeat carrying the *same* `idempotency_key` is settled from
the recorded outcome, which is a different claim, and it remains the correct
response to an ambiguous outcome.

Hosts may ignore annotations, so all enforcement stays in Core.

## Lifecycle

- The MCP process does **not** own the workspace lease.
- The MCP process does **not stop** a service it started when the session ends.
  A service started here is an independent Core service, stopped only by
  `omnivia service stop` or an authorised platform lifecycle action.
- **stdout is protocol-only.** Diagnostics and child-process output go to stderr.
  A startup failure writes not one byte of protocol and exits non-zero.
- If the workspace has not been initialised, the server refuses with an
  instruction to run `omnivia init` and **creates nothing**.

## Apple and macOS permissions

Core and this MCP server need **no** special Apple privacy permission merely to
run: the local service and the stdio server operate inside their own
application-support state, which the user's own account already owns. No
protected-location access is inherent in the design, and nothing here requests
one at install or at startup.

A **separate** staging or connector path that reads protected user locations —
Desktop, Documents, Downloads, an external volume, another application's data —
may need the user to approve that access. That approval belongs to that path,
is requested by that path, and **is not inherited by MCP**. Nothing in the MCP
surface can consume it: `import_start` names a descriptor that a trusted path
already staged, and no tool accepts a path at all. Widening a staging
permission therefore does not widen what a model can reach through MCP.

## Dependency direction

```text
omnivia-core-mcp  -->  omnivia-core
                  -->  omnivia-core-client
                  -->  mcp (>=2,<3)
```

R004-05 fixes that list. This package must never depend on or import
`omnivia-core-cli`, `omnivia_core_runtime`, a Desktop or Platform package, or a
database implementation — and `omnivia-core` must never depend back on it. The
authoring surface changed none of that: the CLI administration commands live in
`omnivia-core-cli` and reach this package's `verify_installed_setup` by name
rather than by dependency.

## Status

The two profiles, the exposure manifest, the mutation wrapper, managed start,
the stdio server, the call path and the installed administration commands are
implemented and tested in-tree, end to end against a real MCP client and a real
`omnivia-core-service`.

`tests/test_mcp_stdio_end_to_end.py` calls the restricted six over stdio against
one governed workspace whose evidence, governed records and sealed relations were
written through the accepted fenced Runtime writers in
`tests/_mcp_v06_3_fixture.py` — the only place in this package's tests that
imports the runtime at all.
`tests/test_mcp_standalone_authoring_acceptance.py` runs the empty-workspace
authoring journey, `tests/test_mcp_import_job_acceptance.py` the staged-import
and job journey, and `tests/test_mcp_recovery_acceptance.py` the interrupted
response, the real service restart, the same-key replay from a second session
and the restart recovery matrix. The authority suite proves both service modes
through real `ServiceClient` instances with recording transports, including
purpose and argument refusals, remote credential cleanup, workspace agreement,
and response correlation.

**What is not proved here.** No real MCP host application has been qualified
against these artifacts. The Standard-profile journey drives the server with the
official Python SDK as the client and still covers the restricted six only; the
Claude Code and Codex applications are not installed and do not run in it. The
required real-host qualification — installed Claude Code and Codex CLI on a
clean supported macOS account, both profiles, restart and revocation — is
outstanding and has no checked-in record. See
[MCP host interoperability](../../docs/distribution/mcp-host-interoperability.md).

## Release note: authoring is an opt-in authority expansion

This release adds a second MCP profile that **writes**. That is an expansion of
what an AI host may do with a workspace, so it is stated plainly rather than
buried in a tool list:

- Nothing changes for an existing installation. Absent, `false` and legacy
  `mutation_enabled` values all stay `restricted`, and upgrade never enables
  authoring.
- Authoring is reached only by a human owner or administrator running
  `omnivia mcp configure --profile authoring`, which is the explicit informed
  intent the protected authority records and re-checks on every startup.
- What it grants is bounded and enumerated: three mutations (`memory_create`,
  `evidence_capture`, `import_start`) and two job reads, against one workspace,
  under a dedicated principal with least-privilege rights, with no governance,
  approval, publication, cancellation or administrative operation reachable.
- `memory_create` produces proposed records only; approval stays a human
  decision outside MCP.
- `omnivia mcp revoke` removes the authority again, idempotently, without
  touching workspace data, audits, committed mutations or running jobs.
