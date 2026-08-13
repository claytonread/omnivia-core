# omnivia-core-cli

`omnivia-core-cli` is the `omnivia` executable in the OmniVia Core package
topology. It parses one frozen command, calls it on a running Core Service
through `omnivia_core_client.ServiceClient`, prints the answer, and exits with
the code that answer maps to.

It owns no state. It does not create, migrate, supervise or stop anything; it
holds no lease, takes no lock, opens no database, reads no descriptor, and
constructs no transport. The shared client is the only way it reaches a service
and owns the complete managed-local attach/start/reconnect decision. The CLI
holds no launcher, path convention or service argv.

The compile-time dependency boundary is the one PM ADR-036 defines: this
surface depends on the public `omnivia-core` contracts and on
`omnivia-core-client`, and neither may depend back on it.

## Invocation

```text
omnivia --installation-state ABSOLUTE_PATH --workspace-id ID [--timeout-ms N] <group> <leaf> [options]
```

`--installation-state` and `--workspace-id` are **required on every command**,
including the probes. There is no default installation, no ambient home
directory and no environment fallback: the two values that decide which service
is called are always stated by the caller. `--installation-state` must be
absolute — a relative path is refused rather than resolved against the working
directory, because a trust anchor that means different directories from
different shells is not one.

`--timeout-ms` is the whole-call budget, covering the connection and the call,
default `10000`. One deadline is built before connecting and reused for the
call, so a slow connect spends the caller's budget rather than being given a
fresh one.

The parser is built from the frozen surface and from nothing else. Every command
is exactly two segments, and there is no alias, no prefix abbreviation and no
path written anywhere but `surface.py` — so a command that is not below cannot
be reached, and cannot be added without adding it to the frozen surface first.

## The 20 application commands

Each reaches exactly one operation of the frozen `OPERATION_CATALOGUE`, one to
one, checked at import. Each declares the purpose it calls under.

| Command | Operation | Purpose |
| --- | --- | --- |
| `workspace list` | `workspace.list` | `workspace_inspection` |
| `workspace create` | `workspace.create` | `workspace_administration` |
| `workspace inspect` | `workspace.inspect` | `workspace_inspection` |
| `memory create` | `memory.create` | `memory_authoring` |
| `memory get` | `memory.get` | `knowledge_retrieval` |
| `memory list` | `memory.list` | `knowledge_retrieval` |
| `memory search` | `memory.search` | `knowledge_retrieval` |
| `import start` | `import.start` | `content_ingestion` |
| `job get` | `job.get` | `job_observation` |
| `job cancel` | `job.cancel` | `job_control` |
| `job retry` | `job.retry` | `job_control` |
| `job events` | `job.events` | `job_observation` |
| `evidence search` | `evidence.search` | `knowledge_retrieval` |
| `knowledge search` | `knowledge.search` | `knowledge_retrieval` |
| `governance propose` | `knowledge.propose` | `knowledge_governance` |
| `governance approve` | `candidate.approve` | `knowledge_governance` |
| `governance reject` | `candidate.reject` | `knowledge_governance` |
| `governance supersede` | `record.supersede` | `knowledge_governance` |
| `graph traverse` | `graph.traverse` | `knowledge_retrieval` |
| `context-pack build` | `context_pack.build` | `knowledge_retrieval` |

Options on every application command:

- `--input-json JSON_OBJECT` — the operation's input document, default `{}`.
  Exactly one JSON object: a duplicated member name, `NaN`, an infinity, an
  array and a scalar are each refused. The document is never echoed in a
  diagnostic.
- `--principal ID` — the principal to claim. A claim, not a grant: the service
  decides authority from its own grant and refuses one it did not give.
- `--idempotency-key KEY` — make this mutation replay-safe.
- `--record-version VERSION` — guard this mutation with the version it expects.
- `--json` — emit the canonical response envelope instead of the human view.

`--idempotency-key` and `--record-version` are held to the catalogue's posture
for the named operation, read off the frozen entry and checked **before the
connection**: one that is required and absent, or supplied where the operation
does not honour it, is a usage error at exit 2 rather than a request sent for
the service to reject.

## The 3 service probes

| Command | Probe |
| --- | --- |
| `service health` | `service.health` |
| `service readiness` | `service.readiness` |
| `service discover` | `service.discover` |

A probe is not a catalogue operation: it carries no purpose, no scope and no
capability, and it takes no input. It goes to the transport the connected client
already composed — there is no second discovery and no second endpoint. A probe
takes `--json` and nothing else.

A `degraded` or `fail` answer exits **0**. The probe was answered, the
answer is printed in full, and what it means is the caller's to decide.

## Output

Two modes, and whenever stdout carries an answer it contains exactly one
document followed by one newline and no other byte.

| | human (default) | `--json` |
| --- | --- | --- |
| application success | the `result` document, indented, on stdout | the whole canonical response envelope on stdout |
| application error | `code: message` on stderr | the whole canonical envelope, error branch included, on stdout |
| probe | the probe result, indented, on stdout | the canonical probe result on stdout |

Local diagnostics are one fixed sentence each, on stderr, and carry nothing from
an exception, an input document, a path, a workspace or a peer. The service's
own `code` and `message` are printed for an application error because those are
the answer rather than a diagnostic about it.

## Frozen exits

`EXIT_CODES` covers `FROZEN_ERROR_CODES` exactly, checked at import, so no error
the service is allowed to return can reach a caller without a defined exit. An
unrecognised code — a service one minor version ahead naming something this
build has not heard of — exits 1 rather than 0.

| Exit | Meaning | Frozen error codes |
| --- | --- | --- |
| 0 | successful application result, or an answered probe | — |
| 1 | unrecoverable, unreachable, or unrecognised | `internal_non_recoverable` |
| 2 | usage, or a call refused locally and never sent | `invalid_request` |
| 3 | authentication, authorization or purpose | `authentication_required`, `authorization_denied`, `workspace_not_granted`, `capability_not_granted`, `invalid_purpose` |
| 4 | version or migration | `workspace_migration_required`, `incompatible_version`, `upgrade_required` |
| 5 | conflict or precondition | `conflict`, `mutation_precondition_failed`, `idempotency_conflict`, `workspace_busy`, `bootstrap_in_progress`, `workspace_lease_unavailable` |
| 6 | out of time | `deadline_exceeded`, `cancelled` |
| 7 | temporarily unavailable | `projection_unavailable`, `stale_projection`, `rate_limited`, `dependency_unavailable`, `internal_recoverable` |
| 8 | not found, or over a limit | `not_found`, `size_limit_exceeded`, `token_limit_exceeded` |

## Modules

```text
surface.py   the frozen commands, probes and exit codes -- data, no behaviour
dispatch.py  one command -> one call on an already-connected ServiceClient
main.py      the executable: parse, connect, dispatch, print, exit
```

## Dependency direction

```text
omnivia-core-cli  -->  omnivia-core-client  -->  omnivia-core
```

- `omnivia-core-cli` depends on `omnivia-core` and on `omnivia-core-client`.
- `omnivia-core` must never depend on or import `omnivia_core_cli`.
- `omnivia-core-cli` must never depend on or import `omnivia_core_runtime` or
  `omnivia_core_mcp`.
