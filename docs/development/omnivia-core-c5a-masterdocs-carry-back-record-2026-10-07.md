# C5A carry-back record — what Masterdocs owes before the next architecture tag

Prepared: 7 October 2026, Australia/Sydney
Lane: `codex/c5a-draft-groundwork`
Authority: owner authorization (Clayton Read, 7 October 2026) for the
governance-controlled C5A contract changes; `AGENTS.md` (masterdocs): architecture
changes require an ADR or owning-DOC revision under the documented governance
process. This record is the repository-approved carry-back record; direct
Masterdocs editing is deliberately NOT done here, because the chat widening is
reflected in REF-042's governed text and belongs in the next architecture
candidate, compiled and validated by the Masterdocs tooling — not hand-patched.

## What this tree changed against architecture-v1.4.0 (per the generator header)

`scripts/generate-chat-contract.py` now records **five** authorized additive
widenings; the first four predate this packet and were already outstanding. This
packet added the fifth:

5. `queries.schema.json#/$defs/ConversationSnapshotResult` — one new optional
   `composerDrafts` member: the authenticated actor's own active
   `ComposerDraft`s for the conversation at the queried device, typed by
   `viewstate.schema.json#/$defs/ComposerDraft`, `maxItems: 3` (the mode
   vocabulary is closed at three, so a snapshot cannot be partial). Two governed
   fixtures added (`snapshot-composer-drafts-present.json`,
   `snapshot-composer-drafts-zero-version-rejected.json`); fixture manifest now
   171 cases (79 valid, 92 invalid); resource inventory digest repinned to
   `83cdd36d6559a8792a36ff932afd79d3be71b36e5423ba0ad1af30137fea826a`.

6. **F2b bridge protocol 1.0 → 1.1** (added 8 October 2026; owner authorization,
   Clayton Read). Unlike widenings 1–5 this is NOT additive-compatible for a 1.0
   peer, by design: `bridge.schema.json#/$defs/SupportedBridgeProtocolVersion`
   is now exactly `"1.1"` and `BridgeSnapshotItem.itemKind` gains `composer_draft`,
   bound to `viewstate.schema.json#/$defs/ComposerDraft` so Platform has a
   contract-valid way to project Core's draft to the hosted renderer. Context
   consistency for that item covers `workspaceId`, `conversationId` and
   `actorId`. The outer Platform carrier `v` stays `"1.0"` (independent axis).
   Every F2b fixture's `bridgeProtocolVersion` moved to `1.1`; eight governed
   fixtures added (three valid, five invalid); manifest now 179 cases (82 valid,
   97 invalid); resource inventory digest repinned to
   `877390ddd69934e26bc8bfc9bdbb9cb8f29d8726b602a58cf0f50bbcf16658b4`.
   Generated: `BRIDGE_PROTOCOL_VERSION`, `PLATFORM_CARRIER_CONTRACT_VERSION`,
   `BRIDGE_SNAPSHOT_ITEM_KINDS`. Platform must move its bridge version to 1.1
   in lockstep; that change is outside this repository.

## What the Application Contract side changed (no Masterdocs schema text)

- `operations.schema.json#/$defs/x-omnivia-operation-catalogue`: one new entry
  `chat.draft` (58 operations; was 57). Read-only: `chat:read`, side effect
  `none`, capability `chat.read`, synchronous, unpaginated, no idempotency key,
  `safe_to_retry`, audited as `read`, the read-posture allowed-error set.
- `chat.schema.json`: new `ChatDraftInput` / `ChatDraftResult` envelope payload
  definitions (the query document and the draft record carried opaque, per the
  family's own convention).
- Published in the reference-only registry (`application-v1.schema.json`), the
  generated Python and TypeScript artifacts, the wire-adapter conformance corpus
  (`chat.draft/primary-success`, 157 cases, corpus digest
  `a1753984b0967e32c697260b7fae9d131aa265eb0db5f58193655828a8b60a4a`), the
  operation-traceability ledger (`chat.draft` joins the MCP-omitted reads,
  reason `read_not_allow_listed`), the MCP real-host qualification's
  unexposed-tools tuple, and the architecture-gate ledger's full-catalogue refs.

## Carry-back obligations (owner/Codex, next Masterdocs candidate)

1. Carry the six chat changes (five widenings plus the F2b 1.1 version change) the generator header names into REF-042's
   governed text and the v1.4.0-lineage chat contract corpus, via the Masterdocs
   architecture compiler — not by hand.
2. Record `chat.draft` in the canonical source-of-truth matrix's operation
   surface (58 application operations), citing this record and PR evidence.
3. Note the C5A runtime facts the contract now implies: `DiscardDraft` empties a
   draft in place (0029's delete trigger forbids row deletion) and a draft save
   announces no outbox fact — both stated in
   `docs/development/omnivia-core-c5a-draft-groundwork-trace-2026-10-07.md` §7.
