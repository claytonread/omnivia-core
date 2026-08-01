# OmniVia Core Stream B — B1 through B10 Completion Record

Date: 2026-07-30
Author: Claude (Stream B implementation agent)
Branch: `agent/omnivia-core-stream-b`
Worktree: `/Users/claytonread/Projects/worktree-omnivia-core-stream-b`
Base: `55f2489` (Stream A)

Companions:
`omnivia-core-stream-b-b0-independent-review-2026-07-30.md`,
`omnivia-core-stream-b-t0629-preparation-pack-2026-07-30.md`

## 1. Status

| Slice | Task | Status | Commit |
|---|---|---|---|
| B0 | Independent review and Phase 1 gate | Delivered | `4441ed6` |
| B1 | T-0629A portable manifest and atomic layout | Delivered | `69c847d` |
| B2 | T-0629B versioned storage and Generation-1 bootstrap | Delivered | `deb6d43` |
| B3 | T-0629C verified backup and copy-only migration | Delivered | `acedd96` |
| B4 | T-0629D identity, filesystem qualification, locks | Delivered | `b123517` |
| B5 | T-0629E lease, takeover evidence, discovery | Delivered | `c5b27b8` |
| B6 | T-0629F fenced transactions and mutation cutover | Delivered | `662afe0` |
| B7 | T-0629G minimal independently runnable Core Service | Delivered | `9921d8e` |
| B8 | Phase 2 adversarial qualification | Delivered | `4972e7a` |
| B9 | Provider-neutral service | **Partial — awaiting the approved Phase 4 packet** | `54b5a58` |
| B10 | Standalone CLI and MCP | **Partial — awaiting the approved Phase 4 packet; MCP adapter withdrawn** | `54b5a58` |

## 2. Verification

Run from the worktree with `PYTHONPATH` shadowing the shared editable install.

```text
Phase 2 acceptance suite      234 passed, 1 skipped (Windows-only)
Existing repository suite     2,104 passed, 5 known SWIG warnings, 0 failed
Phase 0 drift checks          6 of 6 ok
Phase 0 baseline tests        163 passed
Package boundary tests        24 passed
Wheel builds                  4 of 4 built and installed in isolation
Ruff                          clean
Strict mypy                   clean — 25 runtime, 5 client, 13 contract files
omnivia-core-service --help   runs
omnivia --runtime-state … discover  runs, exits 1 with no service
```

Matrix coverage, all 116 rows named and executable: WM 12/12, BD 12/12, LE 22/22,
FL 8/8, FM 22/22, MB 28/28, LC 12/12. Checked by
`test_matrix_qualification.py`, which fails on a missing row rather than reporting a
percentage.

## 3. What B9 and B10 are missing, and why

B9 begins "after A2 freezes each operation contract". **A2 has since run and been
accepted**, at `380af31c674d08a9db775e95921f604ccdbe4208`; this repair candidate is
based on it. The statement below is retained as the record of why B9/B10 were left
partial at `b9005e1`, and is no longer the current dependency.

What remains is a scope decision, not a missing dependency: the owner approved
Phases 0-2, and the project plan requires separate approval before the full CLI and
MCP are built, so the product operation catalogue is deliberately not implemented in
this Phase 2 candidate. B9 and B10 stay **partial**.

As written at `b9005e1`, the contract directory held five envelope-level schemas —
`common`, `compatibility`, `errors`, `envelopes`, `application` — with no operation
or payload schema for the first product vertical slice: workspace inspect/create/list, memory
create/read/list/search, ingestion/import, evidence and governed-knowledge search,
graph traversal, Context Pack creation.

Implementing those payloads in `omnivia-core-runtime` would create the competing
public domain API that plan §8 rule 3 forbids, so they are not implemented.

Delivered instead, because none of it depends on the catalogue:

- the transport-neutral dispatcher over the existing envelope contracts;
- the authorization boundary — fixed principal, workspace allowlist, granted
  operation allowlist;
- the three service-lifecycle operations ADR-037 keeps distinct from product
  operations: `core.health`, `core.readiness`, `core.discovery`;
- the `omnivia` CLI and the first-party MCP adapter as service clients, with
  AST-enforced proof that neither imports the runtime, opens SQLite, or can acquire
  a lease or lock.

`test_the_registry_holds_no_product_operations` asserts the eight named product
operations are absent from the registry. That test is the durable record that this
is incomplete by dependency rather than by choice, and it will fail the moment
someone adds a product operation to the runtime instead of to A2's contracts.

**To finish B9 and B10:** the accepted A2 catalogue is now available, so what is
left is the separately approved Phase 4 packet — handlers registered against the
frozen contracts, and CLI/MCP tool lists extended to them. No redesign is required:
the registry refuses unknown operations today and accepts registration without
modification.

The operational MCP adapter that existed at `b9005e1` is **not** part of this
candidate. It imported `omnivia_core_cli.client`, and making that installable meant
declaring a dependency on a sibling distribution, which the approved topology does
not permit — Runtime, MCP and CLI each depend only on `omnivia-core`. Placing shared
service-client primitives in an approved lower-level surface is an ADR-036 decision,
so the adapter waits for the Phase 4 packet rather than shipping with a prohibited
edge.

## 4. Defects found and fixed

Nine, all surfaced by writing the tests rather than by inspection.

| # | Defect | Consequence if unfixed |
|---|---|---|
| 1 | `executescript()` issues an implicit COMMIT | Generation-1 bootstrap was **not atomic**: substrate DDL committed separately from the workspace-state row |
| 2 | Foreign keys never enabled (per-connection, default OFF) | The legacy schema declared referential integrity that nothing enforced |
| 3 | Pre-release versions sorted *above* their release | `1.0.0-rc.1` satisfied a `min_core_version` of `1.0.0`; the gate failed open |
| 4 | SQL splitter cut trigger bodies at their internal semicolons | Guard triggers could not be installed; SQLite reported only "incomplete input" |
| 5 | `assert_guards_intact` required only one trigger per table | A missing DELETE guard passed while its INSERT sibling survived |
| 6 | `stat -f %T` on macOS returns the file-type suffix, not the filesystem type | Every path resolved to "unknown", so default-deny refused every workspace |
| 7 | `qualify_filesystem(filesystem="")` fell through to auto-detection | An unidentifiable filesystem was silently re-detected instead of refused |
| 8 | `acquire_lease`/`read_lease` leaked raw `sqlite3` errors | Expected failures were untyped |
| 9 | Case-id regex used `\b`, which does not match inside snake_case | The matrix qualification under-reported its own coverage by seven rows |

Defects 1, 3, 5 and 6 were each caught by exactly one test. Without those tests the
bootstrap would have been non-atomic, the compatibility gate would have failed open,
a missing delete guard would have passed, and no workspace would have opened at all.

## 5. Legacy cutover (T-0629F)

Files touched outside the runtime package, for the integration file list T-0629F
requires:

```text
services/omnivia-memory/src/omnivia_memory/persistence/database.py
services/omnivia-memory/tests/test_persistence.py
```

`get_database()` no longer has a default path. It previously created `~/.omnivia/`
and opened a writable `memories.db` as a side effect of a getter, and cached the
result in a module-level singleton so the first bare call in a process pinned that
database for every later caller. It now raises `ImplicitDatabasePathRefused`.

The two legacy tests that exercised the fallback asserted the opposite — that a bare
call creates a database "in the default location", which meant the developer's real
home directory — and now pin the refusal.

## 6. Environment note

A git worktree does **not** isolate Python imports. The shared venv holds an
editable install of `omnivia_memory` pointing at the primary checkout, so tests run
from this worktree imported Stream A's uncommitted source until `PYTHONPATH` was set
to shadow it. Every count in §2 was taken with that shadowing in place.

This matters for the plan's §7.1 worktree model, which assumes isolation. Two
concurrent write-capable streams sharing one venv see each other's uncommitted work.
The durable fix is a dedicated venv per worktree; `PYTHONPATH` shadowing is
sufficient and was used here because it changes nothing in Stream A's environment.

## 7. Threat-model boundary

No test claims to prevent tampering by the workspace-owning OS principal. ADR-037
disclaims that guarantee explicitly: that principal can terminate the service, alter
guard triggers offline or rewrite database bytes. Persisted triggers are fail-closed
defence against ordinary unregistered DML, and schema or trigger drift is **detected
before writable readiness**, not prevented.
`test_no_case_claims_to_prevent_same_principal_tampering` enforces this in the suite.

## 8. What this does not establish

```text
Phase 2 exit gate signed off:        no - requires PM/integration review
Windows lock suites passing in CI:   no - 1 test skipped, needs a Windows runner
Real NFS/SMB/SSHFS mounts qualified: no - verdict injected; real mounts not available
B0 signed off:                       no - re-run required against the frozen
                                     T-0628 closeout commit
Production readiness:                not claimed
```

The remaining Phase 2 exit-gate items are CI and environment provisioning, not code:
a Windows runner for FL-02 and FM-14, and real remote mounts for FL-04 through
FL-06. Both are called out as conditionally skipped rather than counted as passing.
