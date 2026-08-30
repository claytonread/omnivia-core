#!/usr/bin/env python3
# GENERATED-FILE GENERATOR - the module this script writes is GENERATED. This
# script itself is hand-maintained.
#
# Source of truth:
#   contracts/chat/v1/schemas/*.schema.json (13 files)
#   contracts/chat/v1/fixtures/**           (169 files: FIXTURE-MANIFEST.json
#                                             plus 168 governed fixtures)
# Governed by:
#   Approval GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001;
#   proposal commit 04c0b2f768b8a74c515936e548c4a28fa4af514d;
#   proposal content-set inventory SHA-256
#     521893fefc9d33f5507e5bde84be12713359fe1c4ec041164280096b797e2bf2;
#   fixture manifest SHA-256
#     7936bf32da76a66c7d479217588f56c4af20b0ed01001330dd6bc2a6d1329a54;
#   effective Architecture release tag architecture-v1.4.0;
#   effective payload commit eb14159d73c8d9339cfeb347f8de61bd67497974.
#
# Regenerate: python scripts/generate-chat-contract.py
# Verify:     python scripts/generate-chat-contract.py --check
#
# This never reads the Masterdocs checkout: the 13 schemas and 165 fixture
# files under contracts/chat/v1 are this repository's own checked-in copy of
# the approved bytes, and are the only source this script reads.
#
# Authorized widening on top of those bytes, pinned by the digest below:
#   commands.schema.json#/$defs/SubmitMessageRequest -- the expected-head group
#     (branchId, expectedHeadMessageId, expectedHeadVersion) moves out of
#     `required` into an all-or-none `oneOf`, so a first send into a
#     Conversation that REF-042 §9.1 leaves branchless can be stated without a
#     fabricated head. Every document that validated before still validates.
#   queries.schema.json#/$defs/ConversationSnapshotResult -- an optional
#     `branch` member carrying the exact branch.schema.json MessageBranch the
#     `path` was taken from, so a hosted F2b snapshot carries the `headVersion`
#     a later optimistic submit must state. Required members are unchanged.
#   events.schema.json#/$defs/GenerationTextAppendedEvent -- a new closed
#     transport event `chat.generation.text_appended` carrying one live
#     in-flight text delta for an in-progress Generation Attempt, added as a
#     further member of the `ChatEvent` union with
#     common.schema.json#/$defs/ChatEventType widened by that one member. No
#     existing event $def changes, so every document that validated before
#     still validates; six governed fixtures (one valid, five invalid) are
#     added with it.
#   queries.schema.json#/$defs/QueuedSubmissionProjection -- a new $def (the
#     actor-scoped active-queue row joined to its order projection), and
#     queries.schema.json#/$defs/ConversationSnapshotResult gets one new
#     optional `queuedSubmissions` member typed by it. Required members of
#     ConversationSnapshotResult are unchanged, so every document that
#     validated before still validates.
#   bridge.schema.json#/$defs/BridgeSnapshotItem -- `itemKind` widens with one
#     further member, `queued_submission`, bound to the new
#     QueuedSubmissionProjection payload schema; the existing five members and
#     their payload bindings are unchanged. Four governed fixtures (two valid,
#     two invalid) are added with it: a valid and a mismatched-payload
#     BridgeSnapshotItem, and a valid and an oversize-rejected
#     host/chat.snapshot-page F2bCarrierEnvelope each carrying one
#     queued_submission item.
# All four are additive-compatible under CHAT-RUNTIME-CONTRACT-V1-
# COMPATIBILITY-AND-MIGRATION.md §3, and all four must be carried back to the
# Masterdocs authority before the next tag: until then this tree is
# deliberately not byte-identical to architecture-v1.4.0, and the digest is
# what says so.
"""Generate ``omnivia_core.chat_contract.v1.generated`` from the checked-in
Chat Runtime Contract v1 schemas and fixtures.

Verifies, deterministically:

- the exact packaged resource *count* (13 schemas, 165 fixture-tree files);
- a single pinned SHA-256 digest over every relative resource path and its
  exact byte payload, so a changed, missing or extra resource under
  ``contracts/chat/v1`` fails closed rather than silently regenerating a
  different module;

and then emits ``src/omnivia_core/chat_contract/v1/generated.py``: approval
and provenance constants, version constants, schema names/IDs, the resource
inventory digest, the closed Chat command registry, and the closed
vocabularies the bounded W2/F2 codec (``omnivia_core.chat_contract.v1.codec``)
actually decodes, plus which of those vocabularies are additive-decode points
under ``CHAT-RUNTIME-CONTRACT-V1-COMPATIBILITY-AND-MIGRATION.md`` §3
(``ChatEventType``, ``ProviderEventType``): an unrecognised member there is a
compatible minor extension a v1 decoder tolerates (safe diagnostic, then
resnapshot), never a hard decode failure the way an unrecognised member of any
other closed vocabulary in this bundle is; the per-event-type allowed and
required field sets read straight off ``events.schema.json``'s ``ChatEvent``
union, so the codec's strict emission of a known event is single-sourced on the
approved schema bytes rather than a hand-maintained second registry; and the
validation metadata that emission is actually checked against -- the transitive
``$ref`` closure of the six bounded record roots, annotation keywords dropped
and references flattened to lookup keys (:func:`extract_validation_schemas`).

That closure is what makes the codec's strict emission fail-closed on the whole
approved rule -- required primitive types, ``const``/``enum``, patterns and
formats, lengths, numeric bounds, cardinality, closed field sets and the nested
``if``/``then``/``else`` shapes -- without transcribing a single rule by hand. A
keyword the runtime validator does not implement fails generation rather than
shipping a rule that would be silently skipped.

This deliberately does not generate a full record/dataclass surface for every
``$def`` in the 13 schemas (234 definitions across the bundle) -- W2-C's
public codec is the bounded seam ``codec.py`` hand-implements over these
generated registries, not a generated 1:1 dataclass mirror of the whole
bundle.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_ROOT = REPO_ROOT / "contracts" / "chat" / "v1"
SCHEMAS_ROOT = CANONICAL_ROOT / "schemas"
FIXTURES_ROOT = CANONICAL_ROOT / "fixtures"
PYTHON_TARGET = REPO_ROOT / "src" / "omnivia_core" / "chat_contract" / "v1" / "generated.py"

# --------------------------------------------------------------------------
# Pinned provenance (task authority; never read from the Masterdocs checkout)
# --------------------------------------------------------------------------

APPROVAL_ID = "GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001"
PROPOSAL_COMMIT = "04c0b2f768b8a74c515936e548c4a28fa4af514d"
PROPOSAL_CONTENT_SET_INVENTORY_SHA256 = (
    "521893fefc9d33f5507e5bde84be12713359fe1c4ec041164280096b797e2bf2"
)
FIXTURE_MANIFEST_SHA256 = "7936bf32da76a66c7d479217588f56c4af20b0ed01001330dd6bc2a6d1329a54"
EFFECTIVE_ARCHITECTURE_TAG = "architecture-v1.4.0"
EFFECTIVE_PAYLOAD_COMMIT = "eb14159d73c8d9339cfeb347f8de61bd67497974"
CONTRACT_VERSION = "1.0.0-rc.1"
PROTOCOL_VERSION = "1.0"
PROTOCOL_MAJOR = "1"

EXPECTED_SCHEMA_COUNT = 13
EXPECTED_FIXTURE_TREE_COUNT = 169  # FIXTURE-MANIFEST.json + 168 governed fixtures
EXPECTED_FIXTURE_CASE_COUNT = 168
EXPECTED_FIXTURE_VALID_COUNT = 78
EXPECTED_FIXTURE_INVALID_COUNT = 90

#: Pinned over the approved bytes verified byte-for-byte against the tagged
#: Masterdocs authority (``architecture-v1.4.0``) at copy time, plus the four
#: authorized additive widenings the header names. Recomputed on every run by
#: :func:`compute_resource_inventory_digest` and compared; any changed, missing
#: or extra file under ``contracts/chat/v1`` changes this digest and fails
#: ``--check`` closed. Repinned deliberately, never to make a check pass.
EXPECTED_RESOURCE_INVENTORY_DIGEST = (
    "02fa6d29ae0f7a72bee5fe5f71e1fe460f1c3c5f694b0b96bd8ad806fd46095d"
)


class ChatContractError(Exception):
    """Raised when the checked-in Chat Runtime Contract resources fail verification."""


def _iter_resource_files(root: Path, label: str) -> list[Path]:
    if not root.is_dir():
        raise ChatContractError(f"{label} directory is missing: {root}")
    return sorted(p for p in root.rglob("*") if p.is_file())


def compute_resource_inventory_digest() -> tuple[str, int, int]:
    """Return ``(digest_hex, schema_file_count, fixture_tree_file_count)``.

    The digest is a single SHA-256 over every ``(relative_path, byte_length,
    byte_payload)`` triple, sorted by relative path and null-separated so
    a path/length collision cannot forge a matching digest for different
    bytes. Both ``schemas/`` and ``fixtures/`` contribute, each entry
    namespaced by its top-level directory so a schema and a fixture that
    happened to share a relative path could never collide either.
    """
    schema_files = _iter_resource_files(SCHEMAS_ROOT, "schemas")
    fixture_files = _iter_resource_files(FIXTURES_ROOT, "fixtures")

    entries: list[tuple[str, bytes]] = []
    for base, root, files in (
        ("schemas", SCHEMAS_ROOT, schema_files),
        ("fixtures", FIXTURES_ROOT, fixture_files),
    ):
        for path in files:
            relative = f"{base}/{path.relative_to(root).as_posix()}"
            entries.append((relative, path.read_bytes()))
    entries.sort(key=lambda entry: entry[0])

    hasher = hashlib.sha256()
    for relative, data in entries:
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(len(data)).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(data)
        hasher.update(b"\x00")
    return hasher.hexdigest(), len(schema_files), len(fixture_files)


def check_resource_inventory() -> list[str]:
    """Return a list of findings; empty means the packaged resources verify clean."""
    findings: list[str] = []
    try:
        digest, schema_count, fixture_count = compute_resource_inventory_digest()
    except ChatContractError as exc:
        return [str(exc)]

    if schema_count != EXPECTED_SCHEMA_COUNT:
        findings.append(
            f"schema count mismatch: found {schema_count}, expected {EXPECTED_SCHEMA_COUNT}"
        )
    if fixture_count != EXPECTED_FIXTURE_TREE_COUNT:
        findings.append(
            f"fixture tree file count mismatch: found {fixture_count}, "
            f"expected {EXPECTED_FIXTURE_TREE_COUNT}"
        )
    if digest != EXPECTED_RESOURCE_INVENTORY_DIGEST:
        findings.append(
            "resource inventory digest mismatch: a schema or fixture byte, path, "
            "or the resource count changed under contracts/chat/v1 "
            f"(found {digest}, expected {EXPECTED_RESOURCE_INVENTORY_DIGEST})"
        )
    return findings


# --------------------------------------------------------------------------
# Parsing the checked-in schemas and fixture manifest
# --------------------------------------------------------------------------


def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMAS_ROOT / f"{name}.schema.json"
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ChatContractError(f"schema {name!r}: expected a JSON object at the document root")
    return document


def schema_names_and_ids() -> tuple[tuple[str, ...], dict[str, str]]:
    names = sorted(p.stem.removesuffix(".schema") for p in SCHEMAS_ROOT.glob("*.schema.json"))
    ids: dict[str, str] = {}
    for name in names:
        schema_id = load_schema(name).get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise ChatContractError(f"schema {name!r} has no string $id")
        ids[name] = schema_id
    return tuple(names), ids


def _enum_at(document: dict[str, Any], *pointer: str) -> tuple[str, ...]:
    node: Any = document
    for key in pointer:
        if not isinstance(node, dict) or key not in node:
            raise ChatContractError(f"expected pointer {'/'.join(pointer)!r} to exist")
        node = node[key]
    if not isinstance(node, list) or not all(isinstance(item, str) for item in node):
        raise ChatContractError(f"expected a string enum at {'/'.join(pointer)!r}")
    return tuple(node)


def extract_vocabularies() -> dict[str, tuple[str, ...]]:
    """Extract exactly the closed vocabularies the bounded W2/F2 codec decodes.

    Every value is read from the checked-in schema bundle by exact JSON
    pointer, never transcribed by hand, so a future schema edit that moves or
    renames one of these members changes generation output rather than
    silently going stale.
    """
    common = load_schema("common")
    commands = load_schema("commands")
    events = load_schema("events")
    provider = load_schema("provider")

    return {
        "CHAT_COMMAND_NAMES": _enum_at(commands, "$defs", "ChatCommandName", "enum"),
        "ERROR_CODES": _enum_at(common, "$defs", "ErrorCode", "enum"),
        "CHAT_EVENT_TYPES": _enum_at(common, "$defs", "ChatEventType", "enum"),
        "RESNAPSHOT_REASONS": _enum_at(
            events, "$defs", "ResnapshotResponse", "properties", "reason", "enum"
        ),
        "COMMAND_RESULT_STATUSES": _enum_at(
            commands, "$defs", "CommandResultEnvelope", "properties", "status", "enum"
        ),
        "F2A_FINISH_REASONS": _enum_at(provider, "$defs", "FinishReason", "enum"),
        "F2A_PROVIDER_ERROR_CODES": _enum_at(provider, "$defs", "ProviderErrorCode", "enum"),
        "F2A_PROVIDER_EVENT_TYPES": _enum_at(provider, "$defs", "ProviderEventType", "enum"),
        "F2A_INVOCATION_LIFECYCLE_STATES": _enum_at(
            provider, "$defs", "ProviderInvocationLifecycleState", "enum"
        ),
        "F2A_RECONCILIATION_STATES": _enum_at(provider, "$defs", "ReconciliationState", "enum"),
        "F2A_ROUTE_DECISIONS": _enum_at(
            provider, "$defs", "RouteEvidence", "properties", "routeDecision", "enum"
        ),
        "F2A_ESTIMATED_COST_PRICING_SOURCES": _enum_at(
            provider, "$defs", "EstimatedCost", "properties", "pricingSource", "enum"
        ),
    }


def extract_chat_event_field_metadata() -> tuple[
    dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], dict[str, str]
]:
    """Return ``(allowed_by_event_type, required_by_event_type, schema_ref_by_event_type)``.

    Read straight off ``events.schema.json#/$defs/ChatEvent``'s ``oneOf``
    branches: each branch is one closed (``additionalProperties: false``) event
    ``$def`` whose ``eventType`` is a ``const`` or, for ``GenerationEvent``, an
    ``enum`` covering all five ``chat.generation.*`` discriminators. The codec
    needs to know, per exact event type, which fields the contract allows,
    which it requires, and which branch ``$def`` an emitted event of that type
    must validate against; deriving all three here keeps that single-sourced on
    the approved schema bytes instead of a hand-maintained second registry.
    """
    events = load_schema("events")
    defs = events.get("$defs", {})
    branches = defs.get("ChatEvent", {}).get("oneOf")
    if not isinstance(branches, list) or not branches:
        raise ChatContractError("events.schema.json: ChatEvent has no oneOf branches")

    allowed: dict[str, tuple[str, ...]] = {}
    required: dict[str, tuple[str, ...]] = {}
    schema_refs: dict[str, str] = {}
    for branch in branches:
        reference = str(branch.get("$ref", ""))
        name = reference.split("/$defs/")[-1]
        definition = defs.get(name)
        if not isinstance(definition, dict):
            raise ChatContractError(f"events.schema.json: ChatEvent branch {name!r} is not a $def")
        if definition.get("additionalProperties") is not False:
            raise ChatContractError(f"events.schema.json: {name} is not a closed object")
        properties = definition.get("properties", {})
        discriminator = properties.get("eventType", {})
        if "const" in discriminator:
            event_types = [discriminator["const"]]
        else:
            event_types = list(discriminator.get("enum", []))
        if not event_types:
            raise ChatContractError(f"events.schema.json: {name} has no eventType discriminator")
        for event_type in event_types:
            if event_type in allowed:
                raise ChatContractError(f"events.schema.json: duplicate eventType {event_type!r}")
            allowed[event_type] = tuple(sorted(properties))
            required[event_type] = tuple(sorted(definition.get("required", [])))
            schema_refs[event_type] = _reference_key(reference, events["$id"])
    return allowed, required, schema_refs


# --------------------------------------------------------------------------
# Schema-derived validation metadata for the bounded codec records
# --------------------------------------------------------------------------

#: Each bounded public codec record -> the exact canonical subschema whose
#: document it emits, as a ``<file>#<json-pointer>`` key. ``CommandError`` has
#: no ``$def`` of its own: the contract states its shape inline as
#: ``CommandResultEnvelope.properties.error``, so that is the pointer used.
VALIDATION_ROOTS: dict[str, str] = {
    "ChatEvent": "events.schema.json#/$defs/ChatEvent",
    "CommandError": "commands.schema.json#/$defs/CommandResultEnvelope/properties/error",
    "CommandResultEnvelope": "commands.schema.json#/$defs/CommandResultEnvelope",
    "ProviderInvocationRecord": "provider.schema.json#/$defs/ProviderInvocationRecord",
    "ProviderInvocationRequest": "provider.schema.json#/$defs/ProviderInvocationRequest",
    "ResnapshotResponse": "events.schema.json#/$defs/ResnapshotResponse",
}

#: Keywords that assert nothing about an instance. They are dropped, so the
#: generated metadata is exactly the assertion set the runtime validator runs.
ANNOTATION_KEYWORDS = frozenset(
    {"title", "description", "$comment", "examples", "default", "deprecated", "readOnly", "writeOnly"}
)

#: The exact JSON Schema assertion keywords
#: ``omnivia_core.chat_contract.v1._validation`` implements. A keyword outside
#: this set anywhere under one of the six roots fails generation closed rather
#: than shipping metadata whose rule the runtime validator would silently skip
#: -- an under-validating codec is precisely the defect this metadata exists to
#: prevent, so it must never be reachable by adding a schema keyword.
SUPPORTED_VALIDATION_KEYWORDS = frozenset(
    {
        "$ref", "additionalProperties", "allOf", "anyOf", "const", "else", "enum", "format", "if",
        "items", "maxItems", "maxLength", "maxProperties", "maximum", "minItems", "minLength",
        "minProperties", "minimum", "not", "oneOf", "pattern", "properties", "propertyNames",
        "required", "then", "type", "uniqueItems",
    }
)

#: Keywords whose value is one subschema, and whose value is an array of them.
_SUBSCHEMA_KEYWORDS = frozenset(
    {"additionalProperties", "else", "if", "items", "not", "propertyNames", "then"}
)
_SUBSCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})


def _reference_key(reference: str, base_id: str) -> str:
    """Return a ``$ref`` as the ``<file>#<json-pointer>`` key the metadata uses."""
    document_id, _, pointer = (base_id, "#", reference[1:]) if reference.startswith("#") else reference.partition("#")
    return f"{document_id.rsplit('/', 1)[-1]}#{pointer}"


def _resolve_key(key: str, documents_by_file: dict[str, dict[str, Any]]) -> tuple[Any, str]:
    """Return ``(subschema, document_id)`` for a ``<file>#<json-pointer>`` key."""
    file_name, _, pointer = key.partition("#")
    document = documents_by_file.get(file_name)
    if document is None:
        raise ChatContractError(f"validation metadata: unknown schema file {file_name!r}")
    node: Any = document
    for token in pointer.split("/"):
        if not token:
            continue
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise ChatContractError(f"validation metadata: unresolvable pointer {key!r}")
        node = node[token]
    return node, str(document["$id"])


def extract_validation_schemas() -> dict[str, Any]:
    """Return the transitive ``$ref`` closure of the six bounded record roots.

    Every entry is the approved subschema with the annotation-only keywords
    dropped and every ``$ref`` rewritten to a flat ``<file>#<json-pointer>``
    key, so the runtime validator resolves references by dictionary lookup
    without a URL resolver and without re-reading the packaged bytes (which the
    source tree does not have -- they are force-included into the wheel only).
    The closure is flat rather than inlined so a shared ``$def`` such as
    ``Timestamp`` is carried once, not once per use site.
    """
    documents_by_file = {f"{name}.schema.json": load_schema(name) for name in schema_names_and_ids()[0]}
    resolved: dict[str, Any] = {}

    def convert(node: Any, base_id: str) -> Any:
        if isinstance(node, list):
            return [convert(item, base_id) for item in node]
        if not isinstance(node, dict):
            return node
        converted: dict[str, Any] = {}
        for keyword, value in node.items():
            if keyword in ANNOTATION_KEYWORDS:
                continue
            if keyword not in SUPPORTED_VALIDATION_KEYWORDS:
                raise ChatContractError(
                    f"validation metadata: keyword {keyword!r} is not one the runtime validator "
                    "implements; implement it in omnivia_core.chat_contract.v1._validation and add "
                    "it to SUPPORTED_VALIDATION_KEYWORDS before regenerating"
                )
            if keyword == "$ref":
                converted[keyword] = register(_reference_key(str(value), base_id))
            elif keyword == "properties":
                converted[keyword] = {name: convert(sub, base_id) for name, sub in value.items()}
            elif keyword in _SUBSCHEMA_LIST_KEYWORDS:
                converted[keyword] = [convert(sub, base_id) for sub in value]
            elif keyword in _SUBSCHEMA_KEYWORDS and isinstance(value, (dict, list)):
                converted[keyword] = convert(value, base_id)
            else:
                converted[keyword] = value
        return converted

    def register(key: str) -> str:
        if key not in resolved:
            resolved[key] = None  # placeholder: a recursive $ref resolves to this key
            node, document_id = _resolve_key(key, documents_by_file)
            resolved[key] = convert(node, document_id)
        return key

    for key in sorted(VALIDATION_ROOTS.values()):
        register(key)
    return resolved


def load_fixture_manifest() -> dict[str, Any]:
    path = FIXTURES_ROOT / "FIXTURE-MANIFEST.json"
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ChatContractError("FIXTURE-MANIFEST.json: expected a JSON object at the root")
    return document


def check_fixture_manifest_counts() -> list[str]:
    manifest = load_fixture_manifest()
    counts = manifest.get("counts", {})
    findings: list[str] = []
    expected = {
        "total": EXPECTED_FIXTURE_CASE_COUNT,
        "valid": EXPECTED_FIXTURE_VALID_COUNT,
        "invalid": EXPECTED_FIXTURE_INVALID_COUNT,
    }
    for key, expected_value in expected.items():
        if counts.get(key) != expected_value:
            findings.append(
                f"FIXTURE-MANIFEST.json counts.{key} mismatch: "
                f"found {counts.get(key)!r}, expected {expected_value}"
            )
    return findings


# --------------------------------------------------------------------------
# Rendering generated.py
# --------------------------------------------------------------------------


def _render_str_tuple(name: str, values: tuple[str, ...], doc: str) -> str:
    lines = [f"#: {doc}", f"{name}: Final[tuple[str, ...]] = ("]
    for value in values:
        lines.append(f"    {value!r},")
    lines.append(")")
    return "\n".join(lines)


def _render_mapping(name: str, mapping: dict[str, str], doc: str) -> str:
    lines = [f"#: {doc}", f"{name}: Final[Mapping[str, str]] = MappingProxyType(", "    {"]
    for key in sorted(mapping):
        lines.append(f"        {key!r}: {mapping[key]!r},")
    lines.append("    }")
    lines.append(")")
    return "\n".join(lines)


def _render_json_literal(value: Any, indent: int) -> str:
    """Render a JSON value as a deterministic, diff-friendly Python literal.

    Object keys are sorted, so the same schema bytes always render the same
    module text regardless of dictionary insertion order; ``repr`` handles the
    scalars, whose Python literal forms are exact for ``str``/``int``/``bool``.
    """
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = "\n".join(
            f"{pad}    {key!r}: {_render_json_literal(value[key], indent + 4)}," for key in sorted(value)
        )
        return f"{{\n{body}\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        body = "\n".join(f"{pad}    {_render_json_literal(item, indent + 4)}," for item in value)
        return f"[\n{body}\n{pad}]"
    return repr(value)


def _render_mapping_of_tuples(name: str, mapping: dict[str, tuple[str, ...]], doc: str) -> str:
    lines = [
        f"#: {doc}",
        f"{name}: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(",
        "    {",
    ]
    for key in sorted(mapping):
        lines.append(f"        {key!r}: (")
        for value in mapping[key]:
            lines.append(f"            {value!r},")
        lines.append("        ),")
    lines.append("    }")
    lines.append(")")
    return "\n".join(lines)


def render_generated_module(
    schema_names: tuple[str, ...],
    schema_ids: dict[str, str],
    digest: str,
    vocab: dict[str, tuple[str, ...]],
    event_fields: dict[str, tuple[str, ...]],
    event_required_fields: dict[str, tuple[str, ...]],
    event_schema_refs: dict[str, str],
    validation_schemas: dict[str, Any],
) -> str:
    header = f'''# GENERATED FILE - DO NOT EDIT.
#
# Source of truth:
#   contracts/chat/v1/schemas/*.schema.json (13 files)
#   contracts/chat/v1/fixtures/**           (169 files)
# Governed by:
#   Approval {APPROVAL_ID};
#   proposal commit {PROPOSAL_COMMIT};
#   proposal content-set inventory SHA-256 {PROPOSAL_CONTENT_SET_INVENTORY_SHA256};
#   fixture manifest SHA-256 {FIXTURE_MANIFEST_SHA256};
#   effective Architecture release tag {EFFECTIVE_ARCHITECTURE_TAG};
#   effective payload commit {EFFECTIVE_PAYLOAD_COMMIT}.
# Generator:
#   scripts/generate-chat-contract.py
#
# Regenerate: python scripts/generate-chat-contract.py
# Verify:     python scripts/generate-chat-contract.py --check
#
# Approval/provenance constants, version constants, schema names/IDs, the
# resource inventory digest, the closed Chat command registry, the closed
# vocabularies the bounded W2/F2 codec decodes, and the schema-derived
# validation metadata that codec's strict emission is checked against, for the
# OmniVia Chat Runtime Contract v1. Standard library only: this module must
# never depend on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a
# validation framework. This is a separately versioned public family from
# Application Contract v1 (:mod:`omnivia_core.contracts.v1`) and changes
# nothing there.

"""Generated Chat Runtime Contract v1 registries and metadata."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "ADDITIVE_DECODE_VOCABULARIES",
    "APPROVAL_ID",
    "CHAT_COMMAND_NAMES",
    "CHAT_EVENT_FIELDS",
    "CHAT_EVENT_REQUIRED_FIELDS",
    "CHAT_EVENT_SCHEMA_REFS",
    "CHAT_EVENT_TYPES",
    "COMMAND_RESULT_STATUSES",
    "CONTRACT_VERSION",
    "EFFECTIVE_ARCHITECTURE_TAG",
    "EFFECTIVE_PAYLOAD_COMMIT",
    "ERROR_CODES",
    "F2A_ESTIMATED_COST_PRICING_SOURCES",
    "F2A_FINISH_REASONS",
    "F2A_INVOCATION_LIFECYCLE_STATES",
    "F2A_PROVIDER_ERROR_CODES",
    "F2A_PROVIDER_EVENT_TYPES",
    "F2A_RECONCILIATION_STATES",
    "F2A_ROUTE_DECISIONS",
    "FIXTURE_MANIFEST_SHA256",
    "PROPOSAL_COMMIT",
    "PROPOSAL_CONTENT_SET_INVENTORY_SHA256",
    "PROTOCOL_MAJOR",
    "PROTOCOL_VERSION",
    "RECORD_VALIDATION_REFS",
    "RESNAPSHOT_REASONS",
    "RESOURCE_FIXTURE_TREE_COUNT",
    "RESOURCE_INVENTORY_DIGEST",
    "RESOURCE_SCHEMA_COUNT",
    "SCHEMA_IDS",
    "SCHEMA_NAMES",
    "VALIDATION_KEYWORDS",
    "VALIDATION_SCHEMAS",
]

# --------------------------------------------------------------------------
# Approval, provenance and version constants
# --------------------------------------------------------------------------

#: Approval identifier binding this exact, unchanged copy to its governance decision.
APPROVAL_ID: Final[str] = {APPROVAL_ID!r}
#: The proposal-stage Git commit this copy's bytes were taken from.
PROPOSAL_COMMIT: Final[str] = {PROPOSAL_COMMIT!r}
#: SHA-256 of the proposal's own ``PROPOSAL-SHA256SUMS.txt`` inventory file.
PROPOSAL_CONTENT_SET_INVENTORY_SHA256: Final[str] = {PROPOSAL_CONTENT_SET_INVENTORY_SHA256!r}
#: SHA-256 of ``fixtures/FIXTURE-MANIFEST.json`` at the approved bytes.
FIXTURE_MANIFEST_SHA256: Final[str] = {FIXTURE_MANIFEST_SHA256!r}
#: The Masterdocs Git tag this copy was read through.
EFFECTIVE_ARCHITECTURE_TAG: Final[str] = {EFFECTIVE_ARCHITECTURE_TAG!r}
#: The exact Masterdocs payload commit ``EFFECTIVE_ARCHITECTURE_TAG`` resolves to.
EFFECTIVE_PAYLOAD_COMMIT: Final[str] = {EFFECTIVE_PAYLOAD_COMMIT!r}
#: The contract candidate version the approved catalogue encodes.
CONTRACT_VERSION: Final[str] = {CONTRACT_VERSION!r}
#: The one wire protocol version this bundle supports. A v1 decoder rejects an
#: unsupported major and never silently downgrades
#: (CHAT-RUNTIME-CONTRACT-V1-COMPATIBILITY-AND-MIGRATION.md §1, §3).
PROTOCOL_VERSION: Final[str] = {PROTOCOL_VERSION!r}
#: The major component of :data:`PROTOCOL_VERSION`, the compatibility boundary.
PROTOCOL_MAJOR: Final[str] = {PROTOCOL_MAJOR!r}

#: Exact packaged schema file count.
RESOURCE_SCHEMA_COUNT: Final[int] = {EXPECTED_SCHEMA_COUNT!r}
#: Exact packaged fixture-tree file count (FIXTURE-MANIFEST.json plus 168 governed fixtures).
RESOURCE_FIXTURE_TREE_COUNT: Final[int] = {EXPECTED_FIXTURE_TREE_COUNT!r}
#: Pinned SHA-256 over every relative resource path and byte payload under
#: ``contracts/chat/v1``; see ``scripts/generate-chat-contract.py``
#: ``compute_resource_inventory_digest``.
RESOURCE_INVENTORY_DIGEST: Final[str] = {digest!r}

'''

    schema_names_block = _render_str_tuple(
        "SCHEMA_NAMES", schema_names, "The 13 packaged schema base names (without .schema.json), sorted."
    )
    schema_ids_block = _render_mapping(
        "SCHEMA_IDS", schema_ids, "Schema base name -> its declared $id."
    )

    vocab_docs = {
        "CHAT_COMMAND_NAMES": "The closed v1 initial Chat command registry (commands.schema.json ChatCommandName), 30 members.",
        "ERROR_CODES": "The closed v1 shared error-code registry (common.schema.json ErrorCode), 24 members.",
        "CHAT_EVENT_TYPES": "The closed v1 durable Chat transport event-type vocabulary (common.schema.json ChatEventType), 16 members. Additive-decode point.",
        "RESNAPSHOT_REASONS": "The closed reason vocabulary for events.schema.json ResnapshotResponse.reason, 4 members.",
        "COMMAND_RESULT_STATUSES": "The closed status vocabulary for commands.schema.json CommandResultEnvelope.status, 4 members.",
        "F2A_FINISH_REASONS": "The closed v1 F2a normalised finish-reason vocabulary (provider.schema.json FinishReason), 7 members.",
        "F2A_PROVIDER_ERROR_CODES": "The closed v1 F2a provider error-code vocabulary (provider.schema.json ProviderErrorCode), 16 members.",
        "F2A_PROVIDER_EVENT_TYPES": "The closed v1 F2a provider event-discriminator vocabulary (provider.schema.json ProviderEventType), 20 members. Additive-decode point.",
        "F2A_INVOCATION_LIFECYCLE_STATES": "The closed v1 ProviderInvocationRecord lifecycle vocabulary, 6 members.",
        "F2A_RECONCILIATION_STATES": "The closed v1 reconciliation-state vocabulary, 3 members.",
        "F2A_ROUTE_DECISIONS": "The closed RouteEvidence.routeDecision vocabulary, 3 members.",
        "F2A_ESTIMATED_COST_PRICING_SOURCES": "The closed EstimatedCost.pricingSource vocabulary, 3 members.",
    }
    vocab_blocks = [
        _render_str_tuple(name, vocab[name], vocab_docs[name]) for name in sorted(vocab_docs)
    ]

    additive_block = (
        "#: The closed vocabularies where an unrecognised member is a compatible\n"
        "#: minor extension a v1 decoder tolerates (safe diagnostic, then resnapshot)\n"
        "#: rather than a hard decode failure\n"
        "#: (CHAT-RUNTIME-CONTRACT-V1-COMPATIBILITY-AND-MIGRATION.md §3;\n"
        "#: CHAT-RUNTIME-CONTRACT-V1-FREEZE.md §9 rule 6, §11 rule 6).\n"
        "ADDITIVE_DECODE_VOCABULARIES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(\n"
        "    {\n"
        '        "ChatEventType": CHAT_EVENT_TYPES,\n'
        '        "ProviderEventType": F2A_PROVIDER_EVENT_TYPES,\n'
        "    }\n"
        ")\n"
    )

    event_fields_block = _render_mapping_of_tuples(
        "CHAT_EVENT_FIELDS",
        event_fields,
        "Exact ChatEventType -> the closed field set events.schema.json allows "
        "for that event type (its $def is additionalProperties: false). Strict "
        "emission of a known event may name no other field.",
    )
    event_required_block = _render_mapping_of_tuples(
        "CHAT_EVENT_REQUIRED_FIELDS",
        event_required_fields,
        "Exact ChatEventType -> the field set events.schema.json requires for "
        "that event type. Strict emission of a known event must carry them all.",
    )
    event_schema_refs_block = _render_mapping(
        "CHAT_EVENT_SCHEMA_REFS",
        event_schema_refs,
        "Exact ChatEventType -> the VALIDATION_SCHEMAS key of the one closed "
        "events.schema.json branch an emitted event of that type must satisfy.",
    )
    record_refs_block = _render_mapping(
        "RECORD_VALIDATION_REFS",
        VALIDATION_ROOTS,
        "Bounded codec record class name -> the VALIDATION_SCHEMAS key of the "
        "exact canonical subschema its emitted document must satisfy.",
    )
    validation_keywords_block = _render_str_tuple(
        "VALIDATION_KEYWORDS",
        tuple(sorted(SUPPORTED_VALIDATION_KEYWORDS)),
        "Every JSON Schema assertion keyword VALIDATION_SCHEMAS may contain. "
        "The runtime validator asserts it implements all of them at import, so "
        "a keyword added here without an implementation fails closed.",
    )
    validation_block = (
        "#: The transitive $ref closure of the six bounded codec record roots, read\n"
        "#: from the approved contracts/chat/v1/schemas bytes with the annotation-only\n"
        "#: keywords dropped and every $ref rewritten to a flat '<file>#<pointer>' key\n"
        "#: of this same mapping. This is the sole authority the codec's strict\n"
        "#: emission is checked against; nothing here is transcribed by hand.\n"
        "VALIDATION_SCHEMAS: Final[Mapping[str, Any]] = MappingProxyType(\n"
        "    {\n"
        + "\n".join(
            f"        {key!r}: {_render_json_literal(validation_schemas[key], 8)},"
            for key in sorted(validation_schemas)
        )
        + "\n    }\n)\n"
    )

    body = "\n\n".join(
        [
            schema_names_block,
            schema_ids_block,
            *vocab_blocks,
            event_fields_block,
            event_required_block,
            event_schema_refs_block,
            additive_block,
            record_refs_block,
            validation_keywords_block,
            validation_block,
        ]
    )
    return header + body.rstrip("\n") + "\n"


def generate() -> str:
    schema_names, schema_ids = schema_names_and_ids()
    digest, _schema_count, _fixture_count = compute_resource_inventory_digest()
    vocab = extract_vocabularies()
    event_fields, event_required_fields, event_schema_refs = extract_chat_event_field_metadata()
    return render_generated_module(
        schema_names,
        schema_ids,
        digest,
        vocab,
        event_fields,
        event_required_fields,
        event_schema_refs,
        extract_validation_schemas(),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify resources and generated output without writing any file",
    )
    args = parser.parse_args(argv)

    findings = check_resource_inventory() + check_fixture_manifest_counts()
    if findings:
        print("Chat Runtime Contract v1 canonical resources are not the approved bytes:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1

    rendered = generate()

    if args.check:
        existing = PYTHON_TARGET.read_text(encoding="utf-8") if PYTHON_TARGET.is_file() else ""
        if existing != rendered:
            print(
                f"{PYTHON_TARGET} is stale; run `python scripts/generate-chat-contract.py` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print("Chat Runtime Contract v1 canonical resources and generated artefacts are up to date.")
        return 0

    PYTHON_TARGET.parent.mkdir(parents=True, exist_ok=True)
    PYTHON_TARGET.write_text(rendered, encoding="utf-8")
    print(f"Wrote {PYTHON_TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
