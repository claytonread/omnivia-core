# C5A groundwork trace — durable drafts in Core

Prepared: 7 October 2026, Australia/Sydney
Lane: `codex/c5a-draft-groundwork` (worktree `worktree-omnivia-core-c5a-groundwork`), off `origin/main` at `291d401d` (PR #183).
Authority: `HANDOFF-chat-programme-plan-revision-2026-10-06.md` §3 step 3 (C5A).
Labels: **Inspected** (source read at the trace commit), **Executed** (command run), **Not traced** (explicitly unproven here).

---

## 1. The headline

**The Draft storage groundwork already exists and nothing consumes it.** The
C0-plan assumption that C5A must "trace the Draft groundwork" resolves to:
the durable substrate (table + writer primitives) landed with the Chat
foundation (migration 0029) and has had zero consumers since. C5A's
implementation work is the layer *above* storage — command resolution, CLI
surface, snapshot inclusion — not the storage layer.

## 2. What exists (Inspected)

### 2.1 The table — `omnivia_chat_drafts` (migration `0029_chat_foundation.sql:503`)

- Identity: `PRIMARY KEY (workspace_id, draft_id)`, and one active draft per
  actor/device/mode: `UNIQUE (workspace_id, conversation_id, actor_id, device_id, mode)`.
- Scope: conversation + actor + `device_id` (default `''`), with `mode` in
  `('normal', 'edit_message', 'reuse_message')`; the edit/reuse modes require a
  `source_message_id` and normal forbids one (CHECK-enforced).
- Content: `text_content` (≤ 256 KiB, no NUL), `references_json` (2 B–64 KiB,
  i.e. always a JSON document — C7's extension point), `target_json`
  (optional, ≤ 4 KiB), and `stashed_from_draft_id` (the stash/restore link C4's
  "Restore unsent draft" will want preserved).
- Versioning: `schema_version` CHECKed to 1, monotone integer `version`,
  `updated_at_us`, optional `expires_at_us`.
- Everything is CHECK-constrained at the SQL layer; the table is `IF NOT EXISTS`
  so the migration is re-runnable.

### 2.2 The writer primitives — `storage/chat.py` (ChatWriter)

- `insert_draft` (`:982`): "First write of one actor/device/mode's draft row;
  later writes are `update_draft`" — the insert/update split is the storage-level
  shape of idempotent resolution: a caller that retries an insert it is unsure
  landed must be able to fall through to update (or be refused as duplicate) —
  the exact semantics C5A must pin at the command layer.
- `update_draft` (`:1712`), `read_active_draft` (`:112`, exported in the
  module's `__all__`).
- Both are inside the fenced-writer pattern the Chat command seam already uses
  (`transaction_local_writer`, `ChatWriter` bound to `execute_mutation`'s
  connection — `service/chat_command.py`).

### 2.3 What does NOT exist (grep-verified empty)

- No service operation: `SaveDraft`/`DiscardDraft` appear nowhere under
  `service/`; no command module consumes `insert_draft`/`update_draft`/
  `read_active_draft` (`grep -l` across the runtime package returns only
  `storage/chat.py` itself).
- No CLI leaf: `surface.py` publishes exactly three Chat leaves —
  `('chat','command')`, `('chat','events')`, `('chat','snapshot')`
  (`surface.py:132-134`). There is no `('chat','draft')` group.
- No snapshot inclusion: `service/chat_snapshot.py` contains no draft reference.
- No outbox announcement path for drafts (drafts are not semantic Messages;
  whether a draft save announces at all is a C5A design decision — see §4.3).

## 3. The Core bootstrap recipe (deliverable, Inspected + Executed-as-implemented)

The plan revision's step-1 gate ("a written recipe that takes an empty temp dir
to a startable service") is satisfied twice over:

1. **The service-owned procedure** is documented at
   `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/workspace_init.py`
   (module docstring, R004-10 / owner resolution 004 Packet B): DB first,
   filesystem last — storage lock → `open_database(OpenMode.EXCLUSIVE_MAINTENANCE)`
   twice (refusal-vetting pass, then real) → `bootstrap_generation_one(...,
   expect_phase0_baseline=False)` → `apply_pending_migrations()` →
   `create_workspace(root, manifest)` → `InstallationLayout.create`.
   Idempotence is read out of the database, never off `workspace.json`.
   Exposed as `omnivia-core-service --init`; legal nowhere else (R004-10).
2. **The launcher's authorization side** is `managed_local.py`
   (`_resolve_installation`, `:513`): exactly two admitted layouts —
   registered `<state-root's parent>/workspaces/<workspace_id>` (tried first)
   and legacy `<home>/workspace`; an uninitialised root is REFUSED, never
   bootstrapped; every directory is proved owner-controlled before the manifest
   is read no-follow under a byte bound; a manifest's `workspace_id` must name
   the requested workspace exactly.
3. **A working, executable instance of the recipe** already exists in Platform:
   `scripts/smoke-chat-persistent-core.mjs` + `chat-persistent-core-harness.ts`
   (merged with PR #98, `smoke:chat-persistent-core`, 11/11) mints an isolated
   installation, starts the real service, runs the real controller, restarts it
   gracefully and replays. C5A's test harness should reuse that harness's
   installation-minting code rather than re-derive it.

## 4. What C5A must build (the revised task list, from this trace)

1. **The command module** (`service/chat_draft.py` or a slice of a new module):
   resolve `SaveDraft`/`DiscardDraft` through the same `execute_mutation`
   composition `chat_command.py` uses — one fenced transaction, outbox row in
   the same commit, idempotency handled by the seam's stored-outcome replay.
   The `UNIQUE (workspace, conversation, actor, device, mode)` constraint means
   a SaveDraft to an existing active draft is an UPDATE under the same draft
   identity, with the request's expected `version` checked (the C5B revision
   guard's Core side; conflict is `ERROR_CODE_CONFLICT`).
2. **Durable acknowledgement semantics** (never claim durability from
   `accepted`): mirror chat_command.py's distinction — the command answer
   status is NOT queue/storage truth; the draft is "durable" only when a fresh
   authoritative read (or the snapshot projection) shows it. C5B's honest
   saving/saved states derive from this.
3. **Snapshot inclusion**: extend `chat_snapshot.py` to carry the active draft
   (per actor/device/mode) in the projection so C5B can hydrate on load —
   including `version` and `updated_at_us` for the revision guard.
4. **CLI leaf + catalogue**: add `ApplicationCommand(("chat", "draft"), ...)`
   to `surface.py`, which unblocks the Platform side's already-registered
   forward operation (`CHAT_OPERATIONS.draft`, committed on Platform lane
   `codex/chat-c5-draft-prep` as `a912d79` — carried from the post-C4 session's
   uncommitted line, test expectation updated).
5. **The test slice**: stale/duplicate/conflict/restart — reuse the
   persistent-core harness pattern for the restart case.

## 5. Not traced here (explicitly)

- The chat contract's wire schema for a draft command body (`chat_contract`
  v1 types hold drafts' `references_json`/`target_json` shape only via the
  storage CHECKs; the wire envelope extension is a C5A design decision to
  record before implementation).
- Whether draft saves consume a durable grant spend (the catalogue's
  operation class decides; check `chat_authoring` classification when
  registering).
- Platform-side C5B behaviour (debounced saves, offline states) — separate
  packet, separate PR, Core pin moved only after the Core PR merges.

## 6. Execution trail

- `grep` sweeps across `packages/omnivia-core-runtime/src`,
  `packages/omnivia-core-cli/src`, `packages/omnivia-core/src` — no draft
  consumer above storage (run at `291d401d`).
- Read: `managed_local.py` (module docstring, `_resolve_installation`),
  `workspace_init.py` (module docstring), `chat_command.py` (module docstring),
  `chat.py` writer primitives, migration `0029` drafts table.
- No Core or Platform behaviour changed by this trace; the only commits are
  documentation (this file) and the Platform lane noted in §4.4.

---

## 7. Addendum — the implementation slice landed on this lane (same day)

Written after the trace, on the same branch:

1. **§4.1 done, in `service/chat_submit.py`** (the established single home for
   the served Chat commands): `SaveDraft` and `DiscardDraft` decoded field-by-field
   to `commands.schema.json#/$defs/{SaveDraftRequest,DiscardDraftRequest}` (closed
   field sets, protocol negotiation, mode/source pairing mirroring 0029's own
   CHECKs, references held to the `referenceId`/`sourceRevision` grammar, target
   kinds, bounded text in BOTH characters and bytes, optional `expectedVersion`),
   actor bound to the authenticated principal, workspace bound to the authorized
   one — then settled through `execute_chat_command` unchanged (seam replay, grant,
   fence, audit, stored outcome all inherited, no new durable statement).
2. **Slot semantics** (the design the schema admits): one draft per
   `(conversation, actor, device='', mode)` — empty slot inserts (named `draftId`
   or derived `{commandId}.draft`), the slot's own row CAS-updates in place,
   an occupied slot with a foreign identity is a `conflict`, a stale
   `expectedVersion` conflicts without writing.
3. **§4.1's DiscardDraft, resolved honestly under 0029's delete guard:** the
   migration's trigger forbids DELETE outright, so discard is the CAS update that
   empties the row in place (text `""`, references `[]`, target cleared) — the
   schema carries no discarded state, and inventing one would be a contract
   change. A later save overwrites the same row; a client hydrating an emptied
   draft composes an empty composer. Recorded as a design note for the owner.
4. **§2.3's reader gap:** `storage.chat.read_draft` added (identity read, used by
   DiscardDraft, which must not guess the slot).
5. **§4.3/§4.4 NOT done — blocked, not skipped:** `ConversationSnapshotResult`
   is `additionalProperties: false` with no `composerDraft` member, and a new
   `chat draft` CLI leaf means a new `OPERATION_CATALOGUE` entry in the
   generated Application Contract. Both are governance-bound contract changes
   (`GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001` binds the schema bytes); they
   need the owner decision the plan reserves to the owner, and C5B should not
   proceed against an unpacked wire. The draft's `version` is therefore not
   echoed in the command result either — the frozen envelope has no place for
   the typed `DraftSavedResult` — and `resultRef` is `draft:<draftId>`, the
   same opaque convention `queued-submission:<id>` established.
6. **Tests:** `tests/phase3/runtime/test_chat_drafts.py`, 11 tests, all passing:
   insert + idempotent replay; in-place version advance; stale `expectedVersion`
   conflict without writes; same idempotency key with different content →
   `idempotency_conflict`; foreign actor, malformed mode/source pairings, bad
   reference shape refused before writing; discard in place + discard replay;
   save-after-discard identity continuity; discard of a foreign/missing draft →
   conflict; and the GB-05-style real restart (fresh bootstrap, close, re-own,
   read back, seam replay after restart with no second row).
7. **Regression slice** (the C0 plan's baseline): `test_chat_offline_queue.py` +
   `test_chat_submit_message_resolver.py` + `test_chat_snapshot.py` — 134 passed
   after the change.
8. **Verification environment note:** the shared venv's editable installs point
   at `/Users/claytonread/Projects/omnivia-core`; running this worktree's tests
   needs the `PYTHONPATH` shadow the pytest harness itself prints. Not run yet:
   the full `./scripts/preflight` (Core merges on GitHub-CI green per AGENTS.md,
   and opening the PR is the owner's step).
