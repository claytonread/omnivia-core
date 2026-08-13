# MCP host interoperability

`omnivia-core-mcp` is a stdio MCP server built on the official Model Context
Protocol Python SDK. Nothing in it is specific to one host. This document
records what the Standard-profile journey proves about that claim, in what form
each host is configured, and what the candidate retains as evidence.

The executable proof is `scripts/run-standard-journey.py`, run inside the
offline installed-wheel environment by `scripts/build-standard-candidate.py`.

## What this proves, and what it does not

For each named profile the journey writes that host's own native configuration
document, reads it back, parses it, and starts the real server from the launch
that document yields — then drives the session with the official Model Context
Protocol Python SDK as the client.

The Claude Desktop, Claude Code and Codex binaries are not installed and do not
run here. No claim is made that those applications executed a session. What is
proved is narrower and checkable: each host's configuration shape round-trips to
the accepted launch, and the server that launch starts answers a standards-
conformant MCP client with the identical six-tool manifest and populated results
in every case. The profile names below name configuration forms, not processes.

## The four accepted profiles

| Profile | `config_format` | Configured by |
|---|---|---|
| `claude_desktop` | `claude_desktop_json` | `claude_desktop_config.json` |
| `claude_code` | `claude_code_json` | `.mcp.json` |
| `codex` | `codex_toml` | `config.toml` |
| `official_python_sdk` | `official_python_sdk_stdio` | direct code configuration |

`official_python_sdk` writes no file. It constructs the SDK's stdio server
parameters in code, and it is the profile that proves no Claude-specific
assumption is embedded in the server: the same six tools answer a client
configured without any Claude configuration format in the path.

## Accepted configuration

Claude Desktop and Claude Code both read an `mcpServers` object:

```json
{"mcpServers": {"omnivia-core": {"command": "<omnivia-core-mcp>",
                                 "args": ["--config", "<omnivia-mcp.json>"]}}}
```

Codex reads a `mcp_servers` TOML table:

```toml
[mcp_servers."omnivia-core"]
command = "<omnivia-core-mcp>"
args = ["--config", "<omnivia-mcp.json>"]
```

That is the whole of an accepted document. One top-level table, one server named
`omnivia-core`, and exactly the two fields `command` and `args`. The two
placeholders are absolute local paths supplied by the installation; no
credential, bearer token, endpoint or private path appears in a published
example, and the configuration the server itself reads
(`omnivia.mcp-config.v1`) is owner-private and named only by `--config`.

## One fresh session per profile

Each profile gets its own SDK stdio session against the same immutable
candidate: the transport is opened, `initialize` is answered, `tools/list`
returns exactly six tools, all six are called, and the transport is closed
before the next profile starts. Nothing — transport, session, process or cached
manifest — is shared between profiles, so a per-host difference cannot be hidden
by reuse.

The launch used for a session is the one the host's own configuration yields,
read back out of the file that was just written rather than carried over in
memory. Starting the server from what the journey already knew would prove
nothing about a host's configuration mechanism. The client on the other end of
every one of those launches is the official Python SDK, not the host
application.

All four manifests are then compared. A tool list that differs between hosts is
the interoperability failure this evidence exists to exclude, and it fails the
journey.

Every one of the six calls must return a populated result. The journey creates
the evidence, knowledge, memory, graph and context those tools read, so an empty
answer is a failure rather than an accepted empty workspace.

Stdio stdout is protocol-only. The official SDK decodes it as JSON-RPC and
nothing else, so a log line or a stray child-process byte on stdout is a decode
failure and that session does not complete. Server diagnostics go to a
temporary stderr sink that is discarded with the session.

## Fail-closed configuration parsing

A configuration the journey cannot account for field by field has not been
qualified, so the reader admits the accepted document and nothing else. Every
rejection below produces one payload-free message naming only the profile:

- a document that does not parse as JSON or TOML;
- a document that is not an object;
- a top-level key that is not this family's server table, or any additional
  top-level key;
- a server key that is not `omnivia-core`, or a second server;
- `command` absent, of the wrong type, or not the installed executable;
- `args` absent, not a list, holding a non-string element, or not the exact
  accepted argument list;
- any additional entry field, including `env`, `url`, `headers`, `cwd`, a
  bearer token, a transport selector, and any field a later schema adds.

Unsupported transport fields and schema drift are refused by the same rule
rather than ignored: the accepted field set is closed, not a minimum.

## Retained evidence

`mcp.hosts` in `standalone-journey-result.json` carries exactly one object per
profile, and each object carries exactly these keys:

| Key | Value |
|---|---|
| `client` | the profile name |
| `config_format` | the form it read |
| `connected` | `true` |
| `session_completed` | `true` |
| `tool_count` | `6` |
| `tool_calls` | `6` |
| `tools` | the six stable tool names |
| `result_counts` | one non-sensitive count per tool |
| `verdict` | `"pass"` |

Nothing else is retained. No executable path, configuration path, endpoint,
argument, stdout, stderr, credential, secret, PID or free text reaches the
transcript, and no failure message quotes one either.

`scripts/build-standard-candidate.py` refuses a candidate whose journey result
does not carry all four profiles with exactly those nine keys and no others:
`client` matching the profile it is filed under, `config_format` matching that
profile's form, the four fixed proofs and `verdict` at exactly those values and
types, `tools` the exact six sorted names, and `result_counts` exactly those six
names each with a positive integer count. Types are compared exactly, so a
boolean cannot stand in for a count. That gate and the
journey's profile list are separate hard-coded copies — the two scripts share no
module by design — and a test asserts they have not drifted apart.
