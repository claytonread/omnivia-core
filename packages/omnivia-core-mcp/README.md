# omnivia-core-mcp

The Model Context Protocol server for OmniVia Core: a stdio MCP server that
gives an AI host curated, profile-bound access to one local OmniVia Core
workspace. The default `restricted` profile exposes fourteen reviewed tools;
the explicitly enabled `authoring` profile exposes thirty-five. `restricted` is
bounded and non-authoring, not read-only: `decision_evaluate` writes durable
evaluation, job and audit records, though it never mutates business records or
executes actions.

Built on the official Model Context Protocol Python SDK v2 (owner resolution
004, R004-05). There is no bespoke JSON-RPC or MCP stack in this package, and
no FastMCP dependency — the official SDK is the sole MCP framework dependency.

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
service location, and (for remote mode) credential reference.

## Running it

```bash
omnivia-core-mcp --config /absolute/path/to/omnivia-mcp.json
```

Each supported host profile represents that launch in its native configuration
form. Claude Desktop (`claude_desktop_config.json`) and Claude Code
(`.mcp.json`) name it in an `mcpServers` object:

```json
{"mcpServers": {"omnivia-core": {"command": "<omnivia-core-mcp>",
                                 "args": ["--config", "<omnivia-mcp.json>"]}}}
```

Codex (`config.toml`) names it in a `mcp_servers` table:

```toml
[mcp_servers."omnivia-core"]
command = "<omnivia-core-mcp>"
args = ["--config", "<omnivia-mcp.json>"]
```

A client written directly against the official Python SDK passes the same
command and arguments as stdio server parameters and needs no file at all.
`command` and `args` are the whole of an accepted entry: this server reads no
environment variable, accepts no URL, header or bearer token in a host
configuration, and has nothing to add to one.

The Standard-profile candidate proves this rather than asserting it. For each
host profile — `claude_desktop`, `claude_code`, `codex` and
`official_python_sdk` — it writes that host's native configuration shape, reads
it back, and starts the server from the launch it yields; one fresh stdio
session per profile then initialises, lists exactly the fourteen restricted tools,
calls all fourteen and exercises their accepted success or typed-refusal behavior,
and the four manifests are compared. The client throughout is the official
Python SDK: the Claude Desktop, Claude Code and Codex applications are not
installed and do not run there. A separate installed-wheel authoring
qualification configures the authoring profile and covers capture, proposed
memory, import observation, restart, replay, conflict and revocation. Those
installed SDK-driven checks do not claim that a third-party host binary ran;
real-host evidence is recorded separately. See
[MCP host interoperability](../../docs/distribution/mcp-host-interoperability.md).

There is no default configuration path, environment lookup, or `--home`
fallback. A managed-local document names an absolute `installation_state`; the
server delegates the whole attach/start/reconnect decision to the shared
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
are dispatched under that dedicated principal rather than the service's own. The
bearer is read again for each call, so revoking or rotating it takes effect on
the next call rather than at the next restart, and a reference this installation
cannot produce a usable credential for refuses startup rather than falling back.

A managed-local document with **no** reference is the pre-setup shape. The
console entry point may finish a matching interrupted restricted setup before
MCP initialization, but only when exactly one active restricted setup and its
protected bearer already exist. Migration never creates a grant or guesses a
host. It atomically narrows the document to one workspace and
`mutation_enabled: false`; a legacy true byte never becomes authoring consent.
Otherwise startup refuses with a fixed diagnostic directing the user to the
explicit `omnivia mcp configure --host ...` action. A direct
library caller that bypasses the entry point is still refused by `connect`.
There is no unauthenticated fallback, because the local endpoint would admit one
as the service's own principal.

`server.verify_installed_setup(path)` is the check R004 section 9.2 step 7
requires an installed setup to pass before it reports success, and it lives here
because every part of it does. It is a **real MCP exchange with a real child**:
it runs this package's own entry point as a subprocess — this interpreter,
`-m omnivia_core_mcp.server --config <absolute path>`, and nothing else on the
command line — drives it with the official SDK's `stdio_client` and
`ClientSession`, and completes `initialize` and `tools/list` over the transport
a host would use. The peer must identify itself as `omnivia-core` at this
package's version; the advertised inventory must be exactly one profile's own
tools, in order, at the `EXPECTED_TOOL_COUNT` that profile fixes — fourteen or
thirty-five; and the document's `allowed_purposes` must be exactly that profile's
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
shared client cache, and cleared on failed startup and session shutdown.

If the workspace has not been initialised, the server refuses with an
instruction, and **creates nothing**. The owner chooses an absolute workspace
root and runs the installed `omnivia-core-service --init` maintenance mode with
explicit `--workspace` and this installation's `--installation-state`. Then the
owner runs the installed `omnivia --installation-state ... mcp configure` with
the registered workspace id and profile, and restarts the host. The MCP
configuration carries the workspace id and installation state, not a workspace
path, so the server cannot supply the `--workspace` value itself.

## The exposed surface

`tools/list` is a curated, versioned allow-list (R004-06), not a projection of
the operation catalogue. A newly registered Core operation stays absent from MCP
until somebody adds it to `manifest.py` and tests it.

Manifest version `2.8` advertises fourteen tools under the `restricted` profile,
in this order:

| Tool | Operation | Purpose | Scopes | Capability |
|---|---|---|---|---|
| `workspace_inspect` | `workspace.inspect` | `workspace_inspection` | `workspace:read` | `workspace.read` ≥ 1.0 |
| `evidence_search` | `evidence.search` | `knowledge_retrieval` | `memory:read` | `evidence.read` ≥ 1.0 |
| `knowledge_search` | `knowledge.search` | `knowledge_retrieval` | `memory:read` | `knowledge.read` ≥ 1.0 |
| `memory_search` | `memory.search` | `knowledge_retrieval` | `memory:read` | `memory.read` ≥ 1.0 |
| `graph_traverse` | `graph.traverse` | `knowledge_retrieval` | `graph:read` | `graph.read` ≥ 1.0 |
| `context_pack_build` | `context_pack.build` | `knowledge_retrieval` | `memory:read` | `context_pack.build` ≥ 1.0 |
| `engineering_search` | `engineering.search` | `engineering_search` | `engineering:read` | `engineering.read` ≥ 1.0 |
| `engineering_expand` | `engineering.expand` | `engineering_expand` | `engineering:read` | `engineering.read` ≥ 1.0 |
| `engineering_context_build` | `engineering.context.build` | `engineering_context` | `engineering:read` | `engineering.read` ≥ 1.0 |
| `decision_evaluate` | `decision.evaluate` | `decision_evaluation` | `decision:invoke` | `decision.invoke` ≥ 1.0 |
| `decision_record_get` | `decision.record.get` | `decision_record` | `decision:read` | `decision.read` ≥ 1.0 |
| `decision_record_list` | `decision.record.list` | `decision_record` | `decision:read` | `decision.read` ≥ 1.0 |
| `decision_status` | `decision.status` | `decision_status` | `decision:read` | `decision.read` ≥ 1.0 |
| `trigger_health` | `trigger.health` | `trigger_observation` | `trigger:read` | `trigger.read` ≥ 1.0 |

The `authoring` profile advertises those fourteen, then these fifteen, in this order:

| Tool | Operation | Purpose | Scopes | Capability |
|---|---|---|---|---|
| `memory_create` | `memory.create` | `memory_authoring` | `memory:write` | `memory.write` ≥ 1.0 |
| `evidence_capture` | `evidence.capture` | `content_ingestion` | `memory:write` | `evidence.write` ≥ 1.0 |
| `import_start` | `import.start` | `content_ingestion` | `memory:write` | `ingestion.import` ≥ 1.0 |
| `trigger_declare` | `trigger.declare` | `trigger_configuration` | `trigger:configure` | `trigger.configure` ≥ 1.0 |
| `trigger_lifecycle` | `trigger.lifecycle` | `trigger_configuration` | `trigger:configure` | `trigger.configure` ≥ 1.0 |
| `trigger_ingest` | `trigger.ingest` | `trigger_ingestion` | `trigger:invoke` | `trigger.invoke` ≥ 1.0 |
| `job_get` | `job.get` | `job_observation` | `job:read` | `job.read` ≥ 1.0 |
| `job_events` | `job.events` | `job_observation` | `job:read` | `job.read` ≥ 1.0 |
| `skills_draft_create` | `skills.draft.create` | `skill_authoring` | `skill:author` | `skill.author` ≥ 1.0 |
| `skills_draft_update` | `skills.draft.update` | `skill_authoring` | `skill:author` | `skill.author` ≥ 1.0 |
| `skills_proposal_submit` | `skills.proposal.submit` | `skill_authoring` | `skill:author` | `skill.author` ≥ 1.0 |
| `knowledge_share_propose` | `knowledge.share.propose` | `knowledge_sharing` | `knowledge:share` | `knowledge.share` ≥ 1.0 |
| `knowledge_share_decide` | `knowledge.share.decide` | `knowledge_sharing` | `knowledge:share` | `knowledge.share` ≥ 1.0 |
| `knowledge_share_read` | `knowledge.share.read` | `knowledge_share_observation` | `knowledge:share_read` | `knowledge.share_read` ≥ 1.0 |
| `knowledge_share_lineage` | `knowledge.share.lineage` | `knowledge_share_observation` | `knowledge:share` | `knowledge.share` ≥ 1.0 |

Every read declares `side_effect: none` and `audit_category: read` in the operation
catalogue. Twelve operations are side-effecting -- `decision.evaluate`,
`memory.create`, `evidence.capture`, `import.start`, the three trigger
mutations, the three skill authoring mutations and the two knowledge sharing
mutations (`knowledge.share.propose` and `knowledge.share.decide`) -- and the
manifest admits exactly those by name rather than by catalogue metadata,
refusing at import any other entry that is not a read. Each of the twelve
requires a caller-chosen idempotency key, and `import.start` always answers
with a job that `job.get` and the paged `job.events` observe.

The authoring additions are available only after the installed owner path
records explicit authoring intent and Core grants the dedicated MCP principal
the exact workspace-bounded rights. A configuration byte, host approval, model
claim or per-call argument cannot widen that authority. Revocation is checked on
the next call, including a replay.

Scopes, capability identifiers and minimum versions, side effects, audit
categories and idempotency posture are read from the canonical operation
catalogue rather than restated in adapter code. The tables above restate scopes
and capabilities for reference only. A model can neither supply nor override the
principal, workspace, scopes, purpose, capability or service endpoint.
`tools/list` is deterministic for a given package version and admitted profile.

### Schemas

Each tool advertises both an `inputSchema` and an `outputSchema`, and both are
**self-contained**: the canonical Application Contract v1 definition verbatim,
plus its complete transitive `$defs` closure, with every
`https://contracts.omnivia.dev/...` reference rewritten to a local `#/$defs/...`
one. No advertised schema needs network resolution.

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

### Never exposed as model-callable tools

Service start, stop, health, readiness, status and discovery; bootstrap and
workspace initialisation; unrestricted filesystem path selection;
administrative configuration; job cancellation and retry; governance approval;
and every mutation not named by the selected manifest. These are not merely
unadvertised — the allow-list is the only lookup the call path has, so an
operation absent from it is not callable.

Read-first is enforced at import: an entry whose catalogue metadata is not
`side_effect="none"` and `audit_category="read"` makes the package fail to load,
unless it is one of the twelve named mutations.

## Lifecycle

- The MCP process does **not** own the workspace lease.
- The MCP process does **not stop** a service it started when the session ends.
  A service started here is an independent Core service, stopped only by
  `omnivia service stop` or an authorised platform lifecycle action.
- The configuration document is read once, at process start. A changed
  configuration takes effect in the next process the host starts. The bearer is
  read per call, so revocation or rotation applies without a restart.
- Service ownership, the staged-only import boundary and macOS permissions are
  described in [Shared Core installation](../../docs/distribution/shared-core-installation.md#headless-service-operation).
- **stdout is protocol-only.** Diagnostics and child-process output go to stderr.
  A startup failure writes not one byte of protocol and exits non-zero.

## Dependency direction

```text
omnivia-core-mcp  -->  omnivia-core
                  -->  omnivia-core-client
                  -->  mcp (>=2,<3)
```

R004-05 fixes that list. This package must never depend on or import
`omnivia-core-cli`, `omnivia_core_runtime`, a Desktop or Platform package, or a
database implementation — and `omnivia-core` must never depend back on it.

## Status

The two manifest profiles, managed start, the stdio server and the call path are
tested end to end against the official MCP SDK and a real
`omnivia-core-service`. `tests/test_mcp_stdio_end_to_end.py` calls all fourteen
restricted tools over stdio against one governed workspace whose evidence,
governed records and sealed relations were written through the accepted fenced
Runtime writers in `tests/_mcp_v06_3_fixture.py` — the only place in this
package's tests that imports the runtime at all. The source-tree acceptance
suites cover all fourteen restricted tools and the fifteen authoring additions,
including empty-workspace capture, proposed-memory visibility, durable import
observation, replay, conflict, restart, revocation, and knowledge-share
propose/decide/read/lineage. The installed
qualification is driven from a clean wheel-only environment and retains a closed
redacted record. Qualification by actual Claude Code and Codex CLI processes is
tracked separately from those SDK-driven tests and must not be inferred from a
configuration-form round trip.

**The shared-client integration is closed.** `server.connect` composes
`ServiceClient` for both managed-local and remote mode. The shared client owns
descriptor discovery, transport selection, version negotiation, liveness,
framing, and credential presentation; this package has no transport factory or
dial loop of its own. Every call carries the configuration's principal claim and
selected workspace, the manifest's purpose, and the catalogue's scopes and
capability requirement. Reserved authority arguments are refused before the
client is called, and response correlation is checked before any result is
published.

`packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py` proves the complete
managed-local path against a service the test starts. The authority suite proves
both service modes through real `ServiceClient` instances with recording
transports, including purpose and argument refusals, remote credential cleanup,
workspace agreement, and response correlation. This package still must never
depend on or import `omnivia-core-cli`.
