# MCP host interoperability

`omnivia-core-mcp` is a stdio MCP server built on the official Model Context
Protocol Python SDK. Its protocol and authority are provider- and
model-agnostic. Claude Code and Codex CLI are qualification hosts; neither is a
source of Core authority.

Two kinds of evidence are deliberately kept separate:

1. installed-wheel SDK journeys prove the host configuration shapes, protocol,
   tool inventories and Core behavior from an isolated release artifact;
2. real-host qualification proves that an installed Claude Code or Codex CLI
   process can use those settings and drive the same workflows.

Configuration-form evidence must never be reported as real-host evidence.

## Installed restricted-profile interoperability

`scripts/run-standard-journey.py`, executed inside the offline installed-wheel
environment by `scripts/build-standard-candidate.py`, writes each supported
host's native configuration document, reads it back, starts the real MCP server
from that launch and drives it with the official Python MCP SDK.

The Claude Desktop, Claude Code and Codex binaries do not run in this journey.
The profile names identify configuration forms, not host processes.

| Profile | `config_format` | Native form |
|---|---|---|
| `claude_desktop` | `claude_desktop_json` | `claude_desktop_config.json` |
| `claude_code` | `claude_code_json` | `.mcp.json` |
| `codex` | `codex_toml` | `config.toml` |
| `official_python_sdk` | `official_python_sdk_stdio` | direct SDK parameters |

Each profile gets a fresh stdio session. It initializes, advertises the exact
restricted fourteen-tool inventory, exercises the accepted success or
typed-refusal behavior, and closes before the next profile starts. All four
inventories must be identical. The SDK decodes stdout as JSON-RPC, so any stray
diagnostic byte fails the journey.

The restricted inventory is:

1. `workspace_inspect`
2. `evidence_search`
3. `knowledge_search`
4. `memory_search`
5. `graph_traverse`
6. `context_pack_build`
7. `engineering_search`
8. `engineering_expand`
9. `engineering_context_build`
10. `decision_evaluate`
11. `decision_record_get`
12. `decision_record_list`
13. `decision_status`
14. `trigger_health`

`decision_evaluate` has durable evaluation/job/audit effects but cannot mutate
business records or authorize an action. The restricted profile is therefore a
bounded non-authoring profile, not a universal read-only claim.

## Installed authoring qualification

`scripts/run-mcp-authoring-qualification.py` runs beside the restricted journey
from the same isolated installed-wheel environment. It configures explicit
authoring authority through `omnivia mcp configure`, then proves the exact
twenty-five-tool inventory: the restricted fourteen plus:

1. `memory_create`
2. `evidence_capture`
3. `import_start`
4. `trigger_declare`
5. `trigger_lifecycle`
6. `trigger_ingest`
7. `job_get`
8. `job_events`
9. `skills_draft_create`
10. `skills_draft_update`
11. `skills_proposal_submit`

The qualification uses two isolated workspaces:

- an empty workspace for capture, immediate search, evidence-backed proposed
  memory, default/candidate visibility, replay, changed-input conflict, service
  restart, fresh-session recovery and revocation;
- a workspace containing one source staged by the installed trusted Core
  maintenance path for import start, job/event observation, replay, conflict,
  revocation and owner observation of the surviving committed job.

The first workspace is checked empty through MCP before its first write. The
import workspace is not described as empty: staging is outside the MCP
milestone, and the qualification reads only the verified staged descriptor
created by the installed Core path so it can name that handle to `import_start`.

The retained record is
`evidence/mcp-authoring/mcp-authoring-qualification.json`. Its closed schema is
`docs/distribution/schemas/mcp-authoring-qualification-record-v1.schema.json`.
The candidate builder rejects unknown fields, a non-passing gate, another tool
inventory, an SDK version other than the reviewed pins, or a redaction flag that
is not exactly false.

## Accepted host configuration

Claude Desktop and Claude Code use an `mcpServers` object:

```json
{
  "mcpServers": {
    "omnivia-core": {
      "command": "<omnivia-core-mcp>",
      "args": ["--config", "<omnivia-mcp.json>"]
    }
  }
}
```

Codex uses a `mcp_servers` TOML table:

```toml
[mcp_servers."omnivia-core"]
command = "<omnivia-core-mcp>"
args = ["--config", "<omnivia-mcp.json>"]
```

That is the complete accepted entry. The installation substitutes absolute
local paths. No credential, bearer, endpoint, environment override, URL,
header, working directory or transport selector is accepted. The protected
`omnivia.mcp-config.v1` file is owner-private and is named only by `--config`.

## Fail-closed configuration parsing

The installed interoperability parser rejects every configuration it cannot
account for field by field, including:

- malformed or non-object JSON/TOML;
- an unknown top-level table, renamed server or second server;
- a missing, wrong-type or unexpected command;
- an argument list other than `--config` plus the installed path;
- any additional entry field, including `env`, `url`, `headers`, `cwd`, bearer
  material or a future field not reviewed by this version.

Rejections are fixed and payload-free.

## Retained installed-wheel evidence

The restricted `standalone-journey-result.json` retains only stable profile
names, configuration formats, booleans, counts, tool names and verdicts. It
retains no executable path, configuration path, endpoint, argument, stdout,
stderr, credential, secret, process identifier or free text.

The authoring record retains only:

- its format, profile, protocol, tool count and stable tool names;
- exact SDK versions;
- bounded OS/Python identity strings;
- fixed boolean outcomes for the two journeys;
- fixed false redaction assertions;
- the final verdict.

It cannot retain a path, principal, workspace, job, evidence or record
identifier, credential, grant, submitted content, prompt, transcript, endpoint,
process identifier, stdout/stderr or model response because its schema contains
no field for one.

## Operating the MCP server

- The host starts one MCP process per configuration. The process reads its
  `--config` document once, before it opens stdio. A changed configuration
  therefore takes effect when the host starts the next process.
- The bearer is not held. The server reads it from the protected store on every
  call, so revocation or rotation applies to the next call, including a replay,
  without a restart.
- The MCP process neither owns the workspace lease nor stops a service it
  started. The service outlives the host session. Service start, stop and
  recovery are described in [Shared Core installation](shared-core-installation.md#headless-service-operation).
- `import.start` takes only a server-issued `staged_source_ref`. No MCP tool
  accepts a filesystem path or URL, so a model cannot choose what Core reads or
  fetches. The staging boundary and the reason for it are in the same section.
- The MCP server needs no Apple privacy entitlement and no protected-folder
  permission for its own state. A protected-folder grant that the host
  application holds for its own file access does not add a path input to any
  tool.

## Real-host qualification

Real-host qualification must run actual installed Claude Code and Codex CLI
processes against an exact candidate artifact. It must use temporary isolated
host configuration and installation/workspace state, and must not modify the
operator's normal host configuration.

For each host the retained record must identify the exact host version and
candidate digest and prove:

1. native restricted and authoring configuration;
2. initialize and exact tool discovery;
3. the empty-workspace authoring journey;
4. the staged-import and job-observation journey;
5. same-key recovery after an intentionally interrupted response;
6. protocol-only stdout, host restart and Core restart;
7. authoring revocation and fail-closed mutation behavior.

The harness also proves the following, from its own observation rather than the
model's report:

- **Restricted `decision.evaluate`:** the host's one restricted mutation is
  refused `capability_not_granted`, and the owner observes a disabled decision
  surface and no decision record.
- **Excluded names:** the harness dispatches every name a profile does not expose
  through the proxy itself, so no model choice is involved. The restricted profile
  has 73 such names and the authoring profile 62. Those sets comprise the 44
  catalogue operations outside the authoring manifest, eighteen deterministic
  qualification sentinels spanning all nine section-7 administrative capability
  categories, and (for restricted only) the eleven authoring additions. Sentinels
  are probe names, not catalogue operations. Each name must be answered
  `not_exposed`, and none may appear in the host's listed inventory. Restricted
  and authoring absence/dispatch results are retained as four separate booleans.
- **Wheel closure:** every wheel in the candidate manifest is checked for path,
  size and SHA-256, and no other wheel may sit in the directory. Installation
  uses `pip --require-hashes` from a generated file-URL list, with no index. The
  record binds the normalized full closure by count and digest, and separately
  binds the exact harness and record-schema bytes by SHA-256.
- **Protocol:** initialize must negotiate `2025-06-18`; an initialize error,
  missing or malformed version, version mismatch, or paginated tool listing is
  a protocol violation. Once the interruption response is withheld, the relay
  is sealed and cannot forward another host or child frame. For qualification
  pagination, the host requests page one and the proxy reads later pages on the
  same MCP child connection, copying only the preceding host-principal token.
  The first answer is released after the final page; tokens and page bodies are
  never retained.
- **Platform:** a passing record is limited to macOS 27.0 build 26A428 on arm64,
  rather than any syntactically valid macOS version/build.
- **Cleanup:** the runtime root is removed and verified before a pass is written.
  Before recursive removal, a supplied root must prove that the harness created
  it: it is a non-symlinked, owner-only `ovmcp-real-*` directory directly under
  `/tmp` with an owner-only bootstrap receipt. An unproved root is refused and
  never deleted. Ordinary teardown terminates managed Core process groups with
  bounded TERM-then-KILL escalation and proves the whole group absent. The
  deliberate I-6/I-7 Core restart is instead a proved crash: the whole group is
  sent SIGKILL without TERM first, and the child must report that signal as its
  exit. A descriptor-named replacement is signalled only when its pid, start time
  and boot id still match current system evidence. An uncertain identity is not
  signalled and cleanup fails closed. A Core process/group or runtime root that
  survives is reported as `cleanup_incomplete`. A SIGKILL of the harness itself
  cannot run any of this, so it leaves the runtime root and any Core process for
  manual removal.

Normal native host configuration points directly at the installed
`omnivia-core-mcp --config <core-generated-config>` entry. The qualification
harness additionally places an ephemeral observer proxy in its isolated temporary
host configuration so it can independently measure protocol traffic and withhold
one response. That proxy is qualification instrumentation only: it is not written
to the operator's normal Claude Code or Codex settings and is not the production
connection contract.

Real-host qualification sends fixed qualification prompts and bounded,
service-derived results through the selected host/provider. Obtain explicit user
authorization for that external data flow before either host run. Authorization
to implement the harness, possession of a local login, or provision of a token
file does not by itself authorize sending those data externally.

Model text is not acceptance evidence. A harness must independently verify Core
state and validate the closed redacted record. Prompts and model responses are
ephemeral and are not retained.

The approved replacement host baseline is Claude Code `2.1.289` (the installed
supported CLI verified on 2026-10-04, replacing the Phase 8 start value
`2.1.288`), Codex CLI `0.146.0`, and macOS `27.0` build `26A428` on arm64. These
values qualify nothing by themselves; they become evidence only after the
corresponding real-host run passes at the frozen candidate commit.

The executable harness is `scripts/run-mcp-real-host-qualification.py`. A run
names one host, its installed binary, one clean candidate directory, one
explicit authentication source (an `--auth-file`, or for Claude Code only
`--use-existing-host-auth`), the closed schema, and an output record:

```bash
.venv/bin/python scripts/run-mcp-real-host-qualification.py \
  --host codex-cli \
  --host-binary /absolute/path/to/codex \
  --candidate /absolute/path/to/candidate \
  --auth-file /absolute/path/to/auth.json \
  --schema docs/distribution/schemas/mcp-real-host-qualification-record-v1.schema.json \
  --output /absolute/path/to/codex-record.json
```

For Claude Code, use `--host claude-code` with the installed Claude Code binary
and a token-only authentication file:

```bash
.venv/bin/python scripts/run-mcp-real-host-qualification.py \
  --host claude-code \
  --host-binary /absolute/path/to/claude \
  --candidate /absolute/path/to/candidate \
  --auth-file /absolute/path/to/claude-token.txt \
  --schema docs/distribution/schemas/mcp-real-host-qualification-record-v1.schema.json \
  --output /absolute/path/to/claude-record.json
```

To use the operator's already logged-in Claude CLI profile instead, pass
`--use-existing-host-auth` in place of `--auth-file`. The two are mutually
exclusive, and Codex CLI does not accept this flag:

```bash
.venv/bin/python scripts/run-mcp-real-host-qualification.py \
  --host claude-code \
  --host-binary /absolute/path/to/claude \
  --candidate /absolute/path/to/candidate \
  --use-existing-host-auth \
  --schema docs/distribution/schemas/mcp-real-host-qualification-record-v1.schema.json \
  --output /absolute/path/to/claude-record.json
```

### `--auth-file` by host

The same flag carries a different kind of credential for each host. For both,
the file must be a regular file owned by the operator's account, not a symbolic
link, with no group or world permission bits. Any other file, or any read
failure, is refused as `authentication_unavailable`, and the file's contents are
never echoed.

- **Codex CLI:** the file is an owner-only copy of Codex's `auth.json`. The
  harness copies it by bytes into the isolated `CODEX_HOME` as a new owner-only
  file. It never parses the file.
- **Claude Code:** the file is token-only. It holds exactly the OAuth token
  produced by `claude setup-token`, optionally followed by one LF, at most 1024
  bytes, and nothing else. The accepted token itself is 16–512 characters from
  the harness's portable-token character set. The harness checks that shape and
  never copies the file. It injects the value only as
  `CLAUDE_CODE_OAUTH_TOKEN` into the isolated Claude host environment, so no
  credential file is written into the isolated home.

In file mode, for both hosts, `HOME` and the host's configuration variable
(`CLAUDE_CONFIG_DIR` for Claude Code, `CODEX_HOME` for Codex CLI) remain
isolated. The normal host configuration is not copied, read or changed by the
harness, and the portable Claude token does not rely on the operator's keychain
login. The token is held in memory only for the authentication check and Claude
host sessions; the harness does not write it to disk or include it in a record.
The MCP server process explicitly receives `CLAUDE_CODE_OAUTH_TOKEN` set to the
empty string, so neither the proxy nor Core inherits the token even though the
Claude host process has it.

With `--use-existing-host-auth`, the Claude host process runs as the operator's
own logged-in CLI profile. The harness captures the invoking `HOME` and `USER`
and passes exactly those two values to the host, so the host selects the same
login. It sets no `CLAUDE_CONFIG_DIR` and injects no token. A missing or
malformed value is refused as `authentication_unavailable`, and neither value
appears in argv, stderr, a record or a retained config. The harness reads,
copies, prints and deletes none of the profile's credentials; it proves the
login only through `claude auth status --json` in the session environment. The
MCP server process is pointed at the harness-owned home and configuration
directories with an empty `CLAUDE_CODE_OAUTH_TOKEN`, so Core never sees the real
profile. This mode is less isolated than file mode, the login is not isolated
per run, and it runs only when the operator passes the flag. Its guardrails are
`--safe-mode`, `--restricted`, a private per-run `TMPDIR`, strict MCP config,
the bounded tool allowlist, and no session persistence.

Before Core starts, the harness provisions the credential and asks that host's
own authentication-status command to prove it works in the session environment.
In file mode that is the isolated home, and a credential that is bound to the
operator's keychain or normal configuration fails as `authentication_unavailable`.
In existing-login mode the same command runs against the operator's profile, as
described above. The harness does not weaken file-mode isolation or point a file
run at the operator's normal host state.

Success and failure records are validated against the closed schema before an
atomic write. Early failures use the schema's minimal failure branch; once the
candidate, OS and host identities are verified, failures also carry the fixed
profiles and independently observed gate booleans. Raw host streams, prompts,
model text, identifiers, paths and credentials have no record field.

An exact-tip host record is retained outside the candidate source tree and is
keyed by its `source.revision` and wheel digests. Committing that record into
the same tree would change the commit it claims to qualify, creating a
self-reference. The pull-request or release acceptance evidence therefore
attaches the schema-validated records to the already-frozen commit instead of
adding a post-qualification source commit.
