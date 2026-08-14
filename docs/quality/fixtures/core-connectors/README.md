# OmniVia Core connector preparation corpus

- Lane: A6
- Revision: A6-R3 — canonical full-state lineage repair
- Status: preparation only; inert fixture material
- PM lane base: `a28d2c51493a1fcdd527ef1539d1b598164adf86`
- Core read-only checkpoint: `0278d3cb1da3199dd116ed0710a7459599e142df`
- Candidate document:
  `docs/tasks/2026-08-03-omnivia-core-connector-sdk-incremental-sync-contract.md`

## Purpose

This corpus turns the candidate `SourceConnector` SPI and source
incremental-sync contract into stable, machine-readable requirements,
decisions, provisional dependencies and conformance cases.

It is preparation evidence for a future connector implementation and
conformance packet. It authorizes no product behaviour, reserves no migration
number, changes no operation catalogue and does not claim acceptance.

## Files

The directory has exactly five files:

- `README.md` explains authority, safety, replay and validation;
- `schema.json` is a strict JSON Schema (2020-12) for the corpus envelope and
  its closed vocabularies;
- `connector-cases.json` holds 35 requirements, 18 decisions, 9 provisional
  dependencies and 65 cases;
- `SHA256SUMS` fixes the exact bytes of the three files above; and
- `manifest.json` records lane, pins, per-file sizes and digests, and the size
  and digest of `SHA256SUMS`.

`manifest.json` is deliberately absent from `SHA256SUMS` and does not hash
itself.

## Authority

Interpret the corpus under these sources, in descending authority:

1. `docs/specs/omnivia-core-architecture-spec-v0.6-2026-07-29.md`;
2. `docs/tasks/2026-08-02-omnivia-core-v0.6-completion-plan.md`;
3. `docs/tasks/2026-08-03-omnivia-core-independent-assurance-and-v06-8-preparation-plan.md`;
   and
4. the candidate document named above.

Core storage, contract and migration seams at the read-only Core checkpoint
were inspected without modification. They remain the product authority; this
corpus is not a substitute contract.

## Identifier scheme

All identifiers are stable and share the `CON-` prefix:

| Prefix | Meaning | Count |
| --- | --- | --- |
| `CON-Rnn` | requirement | 35 |
| `CON-Dnn` | decision | 18 |
| `CON-Pnn` | provisional dependency | 9 |
| `CON-Cnnn` | conformance case | 65 |

Identifiers are append-only. A superseded identifier is marked superseded in a
later revision rather than reused for different content.

## Safety

Every value in this directory is synthetic and inert.

- No credential, token, key, certificate or signing material appears anywhere.
- No private workspace content, customer data or real source record appears
  anywhere.
- All locators use the reserved-invalid `fake://` scheme. There is no live URL,
  no hostname, no IP address and no network dependency of any kind.
- All checksums and cursor predecessor digests are synthetic. Readability-only
  cases use repeated-character placeholders; `CON-C059` through `CON-C065` use
  real SHA-256 outputs over displayed synthetic inputs so the canonical lineage
  framing is independently recomputable. None is a digest of private content or
  a real cursor state.
- All cursor payload values are short synthetic labels that happen to satisfy
  the unpadded base64url alphabet and length rule. Payloads that must violate
  that rule, and payloads that must exceed the byte bound, are named as
  classifications or byte counts rather than embedded, so the corpus exercises
  the encoding and size refusals without carrying a malformed or oversized
  value.
- Hostile vectors are described as text classifications. `CON-C037` names its
  four vectors — a control character in an identifier, a JSON-escaped NUL, a
  parent-directory-shaped locator and over-deep metadata nesting — in prose
  rather than embedding them, so the corpus stays readable, diffable and safe
  to open in any tool.
- Nothing here is executable. There is no archive, no script, no binary, no
  symlink and no compressed member, so no decompression bomb or traversal
  payload is possible.
- The corpus is small and bounded: three hashed text files, each well under a
  megabyte.

The prose is written plainly and is not obfuscated to please a scanner. Where a
policy example must name a dangerous shape, it names it in words.

## Replay

Cases are declarative, not executable. A future conformance kit replays them:

1. read `connector-cases.json` and validate it against `schema.json`;
2. instantiate the connector under test with the case `input`, including
   `identity_stability`, any prior cursor and any declared `limits`;
3. drive the declared vector;
4. compare the observed result to `expected_outcome` and `expected_error`; and
5. check each string in `assertions` against a kit-owned assertion binding.

Replay is deterministic. Given one connector version and one scripted source
state, a case yields the same outcome on every run. Cases carry no timestamps
of their own beyond the synthetic `observed_at_us` values in their inputs, and
the kit supplies run and attempt identity from outside the corpus.

Each case reports pass, fail or declared-not-applicable. A
declared-not-applicable result is legitimate only when the connector declared
the matching limitation at discovery and the kit records the reason.
Undeclared limitations are failures.

## Validation

From this directory:

```sh
python3 -c 'import json;[json.load(open(f)) for f in ("schema.json","connector-cases.json","manifest.json")]'
python3 -c 'import json;from jsonschema import Draft202012Validator as V;s=json.load(open("schema.json"));V.check_schema(s);V(s).validate(json.load(open("connector-cases.json")))'
shasum -a 256 -c SHA256SUMS
```

`manifest.json` additionally records each hashed file's byte size, so a
recompute-and-compare of both size and digest detects any drift that a
same-length edit might hide.

## Boundaries

`connector-cases.json` fixes these as machine-checkable `false` values in its
`boundaries` object:

- the SDK opens no storage connection;
- the SDK resolves no credential directly;
- the SDK owns no durable run state;
- the SDK owns no deletion authority;
- the SDK schedules no execution of its own;
- the SDK writes nothing back to a source;
- the corpus changes no operation catalogue;
- the corpus reserves no migration number;
- the corpus authorizes no product write;
- the corpus contains no live endpoint;
- the corpus claims no connector process sandbox;
- the corpus claims no proof of remote completeness; and
- the corpus claims no information-flow enforcement.

The last three are limits, not safety properties. Withholding storage handles is
an API ownership boundary: it decides what the host hands a connector, not what
connector code can reach through platform APIs. Nothing the host can check about
a cursor establishes that the connector reported every observation in the window
it advanced across. And scanning connector-authored bytes cannot enforce
non-disclosure while connector code sees raw credential material, because it can
encode that material in a form no scan decides.

`CON-P09` owns all three: the choice between a named isolation mechanism and an
explicit in-process trust posture with a package qualification gate, the
credential-broker boundary that would make non-disclosure enforceable, and the
trust posture under which per-connector completeness evidence is accepted.
Sections 6.4, 7.2 and 7.5 of the candidate document state the same limits in
prose, and `CON-C058` records the counterexample that makes the completeness
limit concrete.

## Cursor semantics

`connector-cases.json` carries a `cursor_contract` object whose values are
schema constants, so a contradicting claim fails validation rather than drifting
in prose:

| Key | Value | Meaning |
| --- | --- | --- |
| `host_interprets_opaque_payload` | `false` | The host never decodes, parses or orders the payload. |
| `successor_witness` | `monotonic_unsigned_integer` | The only ordering signal the host compares. |
| `successor_lineage` | `canonical_full_parent_cursor_state_digest` | A successor names the complete four-field parent state under its frozen bindings. |
| `host_check_scope` | `connector_attested_lineage_and_witness` | What the check establishes — not source-side ordering. |
| `residual_risk_bounded_by` | `deterministic_replay_and_idempotent_upsert_for_duplication_and_corruption_only` | Exactly which residual replay and idempotency bound. Omission is not among them. |
| `witness_is_connector_attested` | `true` | Stated plainly so the check is not read as proof. |
| `max_opaque_payload_bytes` | `4096` | Capacity bound, enforceable without interpretation. |
| `opaque_payload_encoding` | `base64url_no_padding` | Alphabet and length bound, enforceable without interpretation. |
| `resolved_secret_comparison` | `exact_match_against_host_resolved_material_and_declared_encodings` | The one decidable disclosure check: verbatim and declared-encoding copies only. |
| `credential_shape_scan_is_proof` | `false` | Shape scanners are defence in depth, never proof of absence. |
| `host_verifiable_properties` | `state_chain_integrity_rollback_refusal_and_reordering_refusal` | The complete list of what the cursor check buys. |
| `no_skipped_evidence_is_host_verifiable` | `false` | The host cannot check that a poll skipped nothing. |
| `no_skipped_evidence_classification` | `connector_specific_conformance_provisional_under_CON-P09` | Where the obligation actually lives instead. |
| `predecessor_digest_algorithm` | `sha256` | Fixed, so lineage is computable rather than described. |
| `predecessor_digest_domain_tag` | `omnivia.connector.cursor-state.v2` | Domain separation for the full-state preimage. |
| `predecessor_digest_preimage` | `frame(domain_tag) \|\| frame(workspace_id_ascii) \|\| frame(connector_id_ascii) \|\| frame(decimal(state_version)) \|\| frame(payload_base64url_ascii) \|\| frame(decimal(witness_seq)) \|\| frame(predecessor_digest_raw_32_or_empty)` | The exact complete-parent preimage. |
| `predecessor_digest_computed_by` | `host_only` | The connector never authors its own lineage. |
| `predecessor_digest_framing` | `u32be_length_prefixed_fields_in_fixed_order` | Every field is framed as four-byte unsigned big-endian length plus bytes. |
| `predecessor_digest_field_order` | `domain_tag_workspace_id_connector_id_state_version_payload_witness_seq_predecessor_digest` | No implementation may reorder the fields. |
| `predecessor_digest_text_encoding` | `ascii_domain_bindings_decimals_and_base64url_payload` | Text fields contribute exact ASCII bytes; integers are shortest unsigned decimal. |
| `predecessor_digest_binary_encoding` | `present_digest_is_raw_32_bytes` | A present parent digest contributes 32 raw bytes, not 64 hexadecimal text bytes. |
| `predecessor_digest_genesis` | `null_predecessor_encoded_as_zero_length_final_field` | Genesis is one explicit zero-length final frame; no sentinel is accepted. |
| `cursor_bindings` | `frozen_workspace_id_and_connector_id` | Persisted bindings are reused for recomputation and cannot be caller-replaced. |
| `migration_input` | `whole_cursor_state` | Migration takes and returns a whole `CursorState`, so witness and digest are inside its scope. |
| `migration_preserves_witness` | `exact_equality` | Migration is a re-encoding, not progress. |
| `migration_preserves_predecessor_digest` | `byte_identical_including_absence` | Migration never re-parents the chain. |
| `migration_state_version_direction` | `strictly_increasing` | Forward only, into the supported set. |
| `cursor_non_disclosure_enforced_in_process` | `false` | In-process scanning is not enforcement. |
| `cursor_non_disclosure_status` | `defence_in_depth_pending_CON-P09_credential_broker_boundary` | What would make it enforcement, and who owns that. |

The schema also rejects any decision whose `provisional` flag disagrees with its
`depends_on` list, in both directions, so a decision cannot name a dependency
while presenting itself as settled.

Six further case-level obligations are schema-enforced rather than left to
review: a monotonicity refusal must state both witnesses it compared; an
unmigratable-cursor refusal must state the state version it could not migrate; a
migration vector must be a `cursor_state` case stating both the whole input state
and the whole migration output; a refused migration vector may claim only
`connector_cursor_unmigratable` or `connector_state_invalid`; a declared encoding
violation must be refused as `connector_state_invalid`; and a payload above the
byte bound must be refused as `size_limit_exceeded`.

Two migration obligations are not expressible in JSON Schema, because they compare two
fields of the same object: exact witness equality and byte-identical digest
carry-through across a migration. Those are oracle checks, exercised by the
mutation probes rather than by validation, and this limit is stated here rather
than left for a reader to discover.

The R3 lineage vectors are structurally closed by the schema and semantically
recomputed by the oracle. `CON-C059` contains four golden states forming three
positive links from the exact genesis value. `CON-C060` through `CON-C065` each
contain exactly two vectors differing in one declared field: all four parent
fields, workspace binding and connector binding are covered once. The oracle
requires every displayed digest to match the exact framing, each positive link
to carry its parent's raw digest, every differential to change the digest, and
the successor that repeats the baseline digest against the changed parent to be
refused. `CON-C063` is the explicit `aaaa...` versus `bbbb...` reparenting probe.
