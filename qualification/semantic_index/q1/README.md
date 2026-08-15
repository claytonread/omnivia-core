# Q1 semantic-index qualification — schema and parameterized generator

## Authority

This directory implements Q1-A of the Core semantic-index qualification: the
exact evidence-schema set, frozen synthetic policy/frontier evaluator, and a
deterministic standard-library-only generator for the **noncanonical synthetic**
campaign used to qualify vector search behavior. It does not define the public
contract or acceptance criteria, and it does not itself assert pass/fail.
Frozen identities referenced here:

- Contract: `omnivia.semantic-index.v1`
- Dataset schema: `omnivia.semantic-index-dataset.v1`
- Fixture policy: `omnivia.semantic-index.fixture-policy.v1`
- Policy set: `q1.synthetic.current-canonical.v1`
- Profile: `q1.synthetic-768-cosine-v1`

## Parameters

- Vectors: 768 dimensions, scalar encoding `float32`, byte order `little`,
  metric `cosine_similarity`, normalized with `core_l2_unit_v1`.
- Scale: at most 10,000 active records per campaign, batch size 1..1,000.
- `k`: one of `{1, 10, 50}`.
- Operation lanes: `evidence.search`, `memory.search`, `knowledge.search`.
- Purpose: `user_initiated`. Scope: `memory:read`.

## Noncanonical boundary

Everything this generator produces is **synthetic and noncanonical**. It is
qualification input, not a canonical fixture, dataset, or contract example.
Every generated record and campaign manifest carries `noncanonical: true` so
downstream consumers cannot mistake it for canonical data.

## Deterministic reproduction

All output is derived from one root seed:

```
omnivia-core-semantic-index-qualification-2026-08-02-v1
```

There is no language RNG anywhere in this module — no `random.Random`, no
seedable global state. The only pseudo-random source is
`generate.CounterStream`: each draw hashes

```
ROOT_SEED \x1f label \x1f ... \x1f zero-padded counter
```

with SHA-256 and advances the counter. The preimage is unambiguous: every
label must be a non-empty string that cannot itself contain the `\x1f`
separator (rejected as separator injection, since a label containing it
could otherwise forge a different label/counter split hashing to the same
bytes), and the counter is a fixed-width zero-padded ASCII decimal
(`COUNTER_WIDTH` digits) that fails closed with `OverflowError` instead of
silently widening once it would exceed that width. Record streams are
labelled with the operation and a zero-padded ASCII record ordinal
(`zero_padded_ordinal`, which itself rejects indexes that don't fit its
width). The same root seed, labels, and draw order always reproduce
byte-identical output.

Every generated vector round-trips through explicit little-endian float32
bytes (`canonical.float32_le_bytes` / `float32_le_round_trip`, built on
`struct.pack("<Nf", ...)`), which reject non-finite values, negative zero,
and values that overflow float32 range. External `record_id` values use the
fixed-width ASCII form `q1-record-<lane>-<six-digit ordinal>`, so unsigned UTF-8
tie-breaking is stable across languages. Each `campaign_id` is a
`sha256:<64 lowercase hex>` reference (`canonical.canonical_sha256_ref`) over
the campaign's RFC 8785 canonical JSON bytes: unsigned UTF-16 member ordering,
ECMAScript number serialization, exact string escaping, UTF-8, and rejection
of non-string keys, NaN/Infinity, inexact binary64 integers, lone surrogates,
duplicate parsed members, and unsupported values
(`canonical.canonical_json_bytes`). Re-running
`generate.generate_campaign(...)` with the same arguments on any machine
reproduces the same campaign, byte for byte.

The Python modules load by file path with no package markers: `generate.py` loads
`canonical.py` from its own directory via `importlib.util.spec_from_file_location`,
so either file can be imported standalone (e.g. by a test that loads it by
path) without an `__init__.py`.

## Files

- `canonical.py` — standalone standard-library RFC 8785 encoding and strict
  duplicate-member-rejecting parsing, SHA-256 digests and `sha256:` references,
  and little-endian float32 packing.
- `generate.py` — the labelled SHA-256 counter stream, parameter validation,
  complete explicit `CampaignParameters`, fixed-width external IDs, and the
  deterministic record/campaign generator.
- `policy.py` — the exact frozen 42-rule synthetic ACL policy, fail-closed
  trusted-input evaluator, and pre-engine authorized-frontier derivation.
- `schemas/` — the exact nine Draft 2020-12 evidence schemas: campaign,
  dataset manifest, policy input, policy decision, frontier, ground truth,
  operation trace, reproduction, and root manifest. Every evidence document
  declares its schema identity and version, rejects unknown semantic fields,
  uses `sha256:<64 lowercase hex>` references, and binds its ordered children.
- `examples/noncanonical/README.md` — the explicit noncanonical boundary. No
  generated JSON is checked in before the later materialization gate.
- `tests/test_canonical_encoding.py` — canonical JSON determinism, UTF-8,
  RFC 8785 UTF-16 ordering and ECMAScript number vectors, duplicate-member
  parsing, I-JSON rejection, `sha256:` reference format, float32 little-endian
  encoding, negative zero, and float32 overflow.
- `tests/test_generator_parameters.py` — counter-stream determinism, label
  differentiation, separator-injection rejection, counter-overflow
  rejection, explicit frozen-parameter bounds/types, frozen metric and byte
  order, fixed-width external IDs, digest-reference syntax, and end-to-end
  campaign reproduction.
- `tests/test_policy_frontier.py` — exact rule inventory/digest, policy-input
  validation, ACL precedence, temporal/governance gates, label correction,
  tombstones, all-support authorization, relations, and frontier digests.
- `tests/test_schemas.py` — exact inventory, schema self-validation, strict JSON
  loading, minimal positive records, closed-world/negative cases, private-data
  exclusions, and the noncanonical examples boundary.

## Deliberate absences

Q1-A intentionally does not include:

- `__init__.py` — not requested for this part.
- Generated example output or canonical evidence — the later, separately
  frozen materialization lane owns those bytes.
- Q1-B exhaustive scoring/ranking or Q1-C independent scalar cross-check code.
- Any dependency, workflow, lock file, or product-code change — this part is
  qualification-only code and schemas.
