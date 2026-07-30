#!/usr/bin/env python3
"""Generate the Application Contract v1 Python and TypeScript artifacts (ADR-038).

The checked-in JSON Schema documents under ``contracts/application/v1/schemas``
are the single canonical wire source. This script reads *only* those files and
deterministically emits:

- ``src/omnivia_core/contracts/v1/generated.py``
- ``generated/typescript/application/v1/index.ts``

Both outputs are committed, so a reviewer can read the wire shape without
running anything, and ``--check`` fails when they drift from the schemas.

Scope of the supported schema subset
------------------------------------

This is deliberately *not* a general code-generation framework. It understands
exactly the shapes the v1 contract uses and raises :class:`UnsupportedSchemaError`
on anything else, so an unsupported schema edit fails loudly at generation time
instead of silently producing wrong types:

- a ``$defs`` entry that is ``type: string`` or ``type: integer`` becomes a
  type alias (plus a pattern constant, when the schema constrains one);
- a ``$defs`` entry that is ``type: object`` with ``properties`` becomes a
  frozen dataclass / TypeScript interface;
- a ``$defs`` entry that is ``type: object`` with neither ``properties`` nor
  ``additionalProperties`` becomes the opaque JSON-object type;
- a ``$defs`` entry that is ``oneOf`` of ``$ref`` members becomes a union,
  discriminated by a required property unique to one member;
- inside a property: ``$ref``, ``string``, ``integer``, ``boolean``, ``array``
  with ``items``, and ``object`` with ``additionalProperties`` (an open map).

Everything the generator emits is structural. Pattern, length, and range
constraints are *not* re-implemented in the decoders: strict conformance is
JSON Schema's job (see ``scripts/check-application-contracts.py``), while the
generated ``from_wire`` functions are the tolerant production path that ignores
unknown fields and preserves unknown open string values.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
PYTHON_TARGET = REPO_ROOT / "src" / "omnivia_core" / "contracts" / "v1" / "generated.py"
TYPESCRIPT_TARGET = REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"

BASE_URI = "https://contracts.omnivia.dev/application/v1/"

#: Canonical source documents, in the order their definitions are emitted.
SOURCE_SCHEMAS: tuple[str, ...] = (
    "common",
    "compatibility",
    "errors",
    "envelopes",
    "service",
    "records",
    "jobs",
    "operations",
    "compatibility-matrix",
)
#: The reference-only registry. It contributes annotations, never definitions.
REGISTRY_SCHEMA = "application-v1"

#: The opaque JSON object definition, handled specially by both emitters.
JSON_OBJECT_DEFINITION = "JsonObject"

_REF_RE = re.compile(rf"^{re.escape(BASE_URI)}(?P<file>[a-z0-9-]+)\.schema\.json#/\$defs/(?P<name>\w+)$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")

MAX_LINE_LENGTH = 100


class UnsupportedSchemaError(Exception):
    """Raised when a schema uses a shape this generator deliberately does not support."""


# --------------------------------------------------------------------------
# Intermediate representation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeRef:
    """A resolved property type.

    ``kind`` is one of ``definition``, ``string``, ``integer``, ``boolean``,
    ``array``, ``map``, or ``json_object``.
    """

    kind: str
    name: str | None = None
    item: TypeRef | None = None

    @property
    def inner(self) -> TypeRef:
        if self.item is None:
            raise UnsupportedSchemaError(f"{self.kind} type is missing an element type")
        return self.item


@dataclass(frozen=True)
class Property:
    """One property of an object definition."""

    name: str
    type: TypeRef
    required: bool
    description: str


@dataclass(frozen=True)
class Definition:
    """One ``$defs`` entry, classified into the supported subset."""

    name: str
    kind: str
    source: str
    order: int
    description: str
    properties: tuple[Property, ...] = ()
    pattern: str | None = None
    members: tuple[str, ...] = ()
    discriminators: tuple[tuple[str, str], ...] = ()
    dependencies: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Contract:
    """Everything the emitters need, derived from the checked-in schemas."""

    contract_version: str
    definitions: tuple[Definition, ...]
    retry_classes: tuple[tuple[str, str], ...]
    retryable_retry_classes: tuple[str, ...]
    error_catalogue: tuple[tuple[str, str], ...]
    compatibility_statuses: tuple[str, ...]
    upgrade_states: tuple[str, ...]


# --------------------------------------------------------------------------
# Schema loading and parsing
# --------------------------------------------------------------------------


def load_schema(name: str) -> dict[str, Any]:
    """Load one checked-in schema document by base name."""
    path = SCHEMA_DIR / f"{name}.schema.json"
    with path.open(encoding="utf-8") as handle:
        document: Any = json.load(handle)
    if not isinstance(document, dict):
        raise UnsupportedSchemaError(f"{path}: expected a JSON object at the document root")
    return document


def _string_list(document: dict[str, Any], key: str) -> tuple[str, ...]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UnsupportedSchemaError(f"{key!r} must be an array of strings")
    return tuple(str(item) for item in value)


def _string_map(document: dict[str, Any], key: str) -> tuple[tuple[str, str], ...]:
    value = document.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        raise UnsupportedSchemaError(f"{key!r} must be an object mapping strings to strings")
    return tuple((str(name), str(item)) for name, item in value.items())


def parse_ref(ref: str) -> str:
    """Resolve an absolute in-contract ``$ref`` to a definition name."""
    match = _REF_RE.match(ref)
    if match is None:
        raise UnsupportedSchemaError(
            f"$ref {ref!r} is not an absolute reference to a v1 contract definition"
        )
    if match.group("file") not in SOURCE_SCHEMAS:
        raise UnsupportedSchemaError(f"$ref {ref!r} points outside the canonical source schemas")
    return match.group("name")


def parse_type(node: dict[str, Any], location: str) -> TypeRef:
    """Resolve a property schema into a :class:`TypeRef`."""
    if "$ref" in node:
        extra = set(node) - {"$ref", "description"}
        if extra:
            raise UnsupportedSchemaError(
                f"{location}: a $ref may only carry a description, found {sorted(extra)}"
            )
        ref = node["$ref"]
        if not isinstance(ref, str):
            raise UnsupportedSchemaError(f"{location}: $ref must be a string")
        return TypeRef("definition", name=parse_ref(ref))

    node_type = node.get("type")
    if node_type == "string":
        return TypeRef("string")
    if node_type == "integer":
        return TypeRef("integer")
    if node_type == "boolean":
        return TypeRef("boolean")
    if node_type == "array":
        items = node.get("items")
        if not isinstance(items, dict):
            raise UnsupportedSchemaError(f"{location}: an array must declare an `items` schema")
        return TypeRef("array", item=parse_type(items, f"{location}.items"))
    if node_type == "object":
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            return TypeRef("map", item=parse_type(additional, f"{location}.additionalProperties"))
        if "properties" in node:
            raise UnsupportedSchemaError(
                f"{location}: inline object properties are not supported; "
                "promote the shape to a named $defs entry"
            )
        return TypeRef("json_object")

    raise UnsupportedSchemaError(f"{location}: unsupported schema shape (type={node_type!r})")


def _description(node: dict[str, Any], location: str) -> str:
    value = node.get("description")
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedSchemaError(f"{location}: every definition and property needs a description")
    if '"""' in value or "*/" in value:
        raise UnsupportedSchemaError(f"{location}: description may not contain a comment terminator")
    return value.strip()


def _dependencies(type_ref: TypeRef) -> set[str]:
    if type_ref.kind == "definition":
        assert type_ref.name is not None
        return {type_ref.name}
    if type_ref.kind in {"array", "map"}:
        return _dependencies(type_ref.inner)
    return set()


def parse_definition(name: str, node: dict[str, Any], source: str, order: int) -> Definition:
    """Classify one ``$defs`` entry into the supported subset."""
    location = f"{source}.schema.json#/$defs/{name}"
    description = _description(node, location)

    if "oneOf" in node:
        raw_members = node["oneOf"]
        if not isinstance(raw_members, list) or len(raw_members) < 2:
            raise UnsupportedSchemaError(f"{location}: oneOf must list at least two members")
        members: list[str] = []
        for index, member in enumerate(raw_members):
            if not isinstance(member, dict) or set(member) != {"$ref"}:
                raise UnsupportedSchemaError(
                    f"{location}: oneOf member {index} must be a bare $ref to a named definition"
                )
            members.append(parse_ref(str(member["$ref"])))
        return Definition(
            name=name,
            kind="union",
            source=source,
            order=order,
            description=description,
            members=tuple(members),
            dependencies=frozenset(members),
        )

    node_type = node.get("type")
    if node_type == "string":
        pattern = node.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            raise UnsupportedSchemaError(f"{location}: pattern must be a string")
        return Definition(
            name=name,
            kind="string",
            source=source,
            order=order,
            description=description,
            pattern=pattern,
        )
    if node_type == "integer":
        return Definition(
            name=name, kind="integer", source=source, order=order, description=description
        )
    if node_type != "object":
        raise UnsupportedSchemaError(f"{location}: unsupported definition type {node_type!r}")

    if "properties" not in node:
        if "additionalProperties" in node:
            raise UnsupportedSchemaError(
                f"{location}: a named open map definition is not supported; "
                "declare the map inline on the property that uses it"
            )
        return Definition(
            name=name, kind="json_object", source=source, order=order, description=description
        )

    raw_properties = node["properties"]
    if not isinstance(raw_properties, dict):
        raise UnsupportedSchemaError(f"{location}: properties must be an object")
    required = node.get("required")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise UnsupportedSchemaError(
            f"{location}: an object definition must declare an explicit `required` array"
        )
    if node.get("unevaluatedProperties") is not False:
        raise UnsupportedSchemaError(
            f"{location}: an object definition must set `unevaluatedProperties: false` "
            "so strict conformance rejects unknown fields"
        )
    unknown_required = sorted(set(required) - set(raw_properties))
    if unknown_required:
        raise UnsupportedSchemaError(f"{location}: required names undeclared properties {unknown_required}")

    properties: list[Property] = []
    dependencies: set[str] = set()
    for property_name, property_node in raw_properties.items():
        if not isinstance(property_node, dict):
            raise UnsupportedSchemaError(f"{location}.{property_name}: property must be an object")
        property_location = f"{location}.{property_name}"
        if not property_name.isidentifier() or keyword.iskeyword(property_name):
            raise UnsupportedSchemaError(
                f"{property_location}: property names must be valid, non-reserved "
                "identifiers in every generated language"
            )
        type_ref = parse_type(property_node, property_location)
        properties.append(
            Property(
                name=property_name,
                type=type_ref,
                required=property_name in required,
                description=_description(property_node, property_location),
            )
        )
        dependencies |= _dependencies(type_ref)

    return Definition(
        name=name,
        kind="object",
        source=source,
        order=order,
        description=description,
        properties=tuple(properties),
        dependencies=frozenset(dependencies),
    )


def _resolve_discriminators(union: Definition, by_name: dict[str, Definition]) -> Definition:
    """Pick the required property that uniquely identifies each union member."""
    required_by_member: dict[str, set[str]] = {}
    for member in union.members:
        definition = by_name.get(member)
        if definition is None or definition.kind != "object":
            raise UnsupportedSchemaError(
                f"{union.name}: union member {member!r} must be an object definition"
            )
        required_by_member[member] = {
            prop.name for prop in definition.properties if prop.required
        }

    discriminators: list[tuple[str, str]] = []
    for member, required in required_by_member.items():
        others: set[str] = set()
        for other_member, other_required in required_by_member.items():
            if other_member != member:
                others |= other_required
        unique = sorted(required - others)
        if not unique:
            raise UnsupportedSchemaError(
                f"{union.name}: union member {member!r} has no required property that "
                "distinguishes it from the other members"
            )
        discriminators.append((unique[0], member))

    return Definition(
        name=union.name,
        kind=union.kind,
        source=union.source,
        order=union.order,
        description=union.description,
        members=union.members,
        discriminators=tuple(discriminators),
        dependencies=union.dependencies,
    )


def topological_order(definitions: list[Definition]) -> tuple[Definition, ...]:
    """Order definitions so every dependency precedes its dependents.

    Ties are broken by authored order (source document, then position within
    that document) so the emitted files are stable across runs and machines.
    """
    by_name = {definition.name: definition for definition in definitions}
    remaining = {definition.name: set(definition.dependencies) for definition in definitions}
    ordered: list[Definition] = []

    while remaining:
        ready = sorted(
            (name for name, pending in remaining.items() if not pending),
            key=lambda name: (by_name[name].source, by_name[name].order),
        )
        if not ready:
            cycle = sorted(remaining)
            raise UnsupportedSchemaError(f"definitions form a reference cycle: {cycle}")
        for name in ready:
            ordered.append(by_name[name])
            del remaining[name]
        for pending in remaining.values():
            pending -= set(ready)

    return tuple(ordered)


def build_contract() -> Contract:
    """Read the canonical schemas and build the emitter-facing model."""
    definitions: list[Definition] = []
    seen: dict[str, str] = {}

    for source in SOURCE_SCHEMAS:
        document = load_schema(source)
        raw_defs = document.get("$defs")
        if not isinstance(raw_defs, dict):
            raise UnsupportedSchemaError(f"{source}.schema.json: missing a $defs object")
        for order, (name, node) in enumerate(raw_defs.items()):
            if name in seen:
                raise UnsupportedSchemaError(
                    f"definition {name!r} is declared in both {seen[name]!r} and {source!r}; "
                    "definition names must be unique across the contract"
                )
            seen[name] = source
            if not isinstance(node, dict):
                raise UnsupportedSchemaError(f"{source}.schema.json#/$defs/{name}: not an object")
            definitions.append(parse_definition(name, node, source, order))

    by_name = {definition.name: definition for definition in definitions}
    missing = sorted(
        dependency
        for definition in definitions
        for dependency in definition.dependencies
        if dependency not in by_name
    )
    if missing:
        raise UnsupportedSchemaError(f"unresolved definition references: {missing}")
    if JSON_OBJECT_DEFINITION not in by_name:
        raise UnsupportedSchemaError(f"the contract must define {JSON_OBJECT_DEFINITION!r}")

    definitions = [
        _resolve_discriminators(definition, by_name) if definition.kind == "union" else definition
        for definition in definitions
    ]

    errors = load_schema("errors")
    compatibility = load_schema("compatibility")
    registry = load_schema(REGISTRY_SCHEMA)
    contract_version = registry.get("x-omnivia-contract-version")
    if not isinstance(contract_version, str):
        raise UnsupportedSchemaError(
            f"{REGISTRY_SCHEMA}.schema.json: x-omnivia-contract-version must be a string"
        )

    retry_classes = _string_map(errors, "x-omnivia-retry-classes")
    retryable = _string_list(errors, "x-omnivia-retryable-retry-classes")
    catalogue = _string_map(errors, "x-omnivia-error-catalogue")

    known_retry_classes = {name for name, _ in retry_classes}
    unknown = sorted(set(retryable) - known_retry_classes)
    if unknown:
        raise UnsupportedSchemaError(f"x-omnivia-retryable-retry-classes names unknown classes: {unknown}")
    for code, retry_class in catalogue:
        if retry_class not in known_retry_classes:
            raise UnsupportedSchemaError(
                f"x-omnivia-error-catalogue: {code!r} uses unknown retry class {retry_class!r}"
            )

    return Contract(
        contract_version=contract_version,
        definitions=topological_order(definitions),
        retry_classes=retry_classes,
        retryable_retry_classes=retryable,
        error_catalogue=catalogue,
        compatibility_statuses=_string_list(compatibility, "x-omnivia-compatibility-statuses"),
        upgrade_states=_string_list(compatibility, "x-omnivia-upgrade-states"),
    )


# --------------------------------------------------------------------------
# Shared emitter helpers
# --------------------------------------------------------------------------


def screaming_snake(name: str) -> str:
    """``ContractVersion`` -> ``CONTRACT_VERSION``."""
    return _CAMEL_BOUNDARY_RE.sub("_", name).upper()


def snake(name: str) -> str:
    """``ResponseEnvelope`` -> ``response_envelope``."""
    return _CAMEL_BOUNDARY_RE.sub("_", name).lower()


def sort_all_exports(names: list[str]) -> list[str]:
    """Sort an ``__all__`` list the way Ruff's ``RUF022`` expects: SCREAMING_SNAKE_CASE
    constants first (alphabetically), then everything else (alphabetically).
    """
    constants = sorted(name for name in names if name.replace("_", "").isupper())
    others = sorted(name for name in names if not name.replace("_", "").isupper())
    return constants + others


def wrap(text: str, indent: str, width: int = MAX_LINE_LENGTH) -> list[str]:
    """Wrap prose to the shared line budget at a fixed indent."""
    return textwrap.wrap(
        " ".join(text.split()),
        width=width - len(indent),
        initial_indent=indent,
        subsequent_indent=indent,
    )


def docstring(text: str, indent: str) -> list[str]:
    """Render a docstring, collapsing to one line when the prose fits."""
    single = f'{indent}"""{" ".join(text.split())}"""'
    if len(single) <= MAX_LINE_LENGTH:
        return [single]
    body = wrap(text, indent, width=MAX_LINE_LENGTH - 3)
    return [f'{indent}"""{body[0].strip()}', *body[1:], f'{indent}"""']


def generated_header(comment: str, extra: str) -> list[str]:
    """The identical provenance banner both emitters carry."""
    lines = [
        f"{comment} GENERATED FILE - DO NOT EDIT.",
        f"{comment}",
        f"{comment} Source of truth:",
    ]
    for name in SOURCE_SCHEMAS:
        lines.append(f"{comment}   contracts/application/v1/schemas/{name}.schema.json")
    lines += [
        f"{comment} Generator:",
        f"{comment}   scripts/generate-application-contracts.py",
        f"{comment}",
        f"{comment} Regenerate: python scripts/generate-application-contracts.py",
        f"{comment} Verify:     python scripts/generate-application-contracts.py --check",
        f"{comment}",
        *[f"{comment} {line}" if line else f"{comment}" for line in extra.splitlines()],
    ]
    return lines


def chunk_string(value: str, width: int) -> tuple[str, ...]:
    """Split a string into fixed-width pieces for multi-line literal emission.

    Splitting the *value* rather than its source representation keeps escaping
    the responsibility of whoever renders each piece, so a split can never land
    inside an escape sequence.
    """
    if len(value) <= width:
        return (value,)
    count = -(-len(value) // width)
    size = -(-len(value) // count)
    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = min(start + size, len(value))
        # Never end a piece on a lone backslash: the split is legal either way,
        # but a piece ending in `\` reads like a broken escape.
        while end - 1 > start and end < len(value) and value[end - 1] == "\\":
            end -= 1
        chunks.append(value[start:end])
        start = end
    return tuple(chunks)


def call_lines(assignment: str, function: str, arguments: list[str], indent: int) -> list[str]:
    """Render a call on one line when it fits the line budget, else one argument per line."""
    single = f"{assignment}{function}({', '.join(arguments)})"
    if indent + len(single) <= MAX_LINE_LENGTH:
        return [single]
    return [
        f"{assignment}{function}(",
        *[f"    {argument}," for argument in arguments],
        ")",
    ]


# --------------------------------------------------------------------------
# Python emitter
# --------------------------------------------------------------------------

PYTHON_PREAMBLE = '''
class ContractDecodeError(ValueError):
    """Raised when a wire payload cannot be decoded into a contract value.

    Decoding is *tolerant about vocabulary and strict about structure*: unknown
    fields are ignored and unknown open string values are preserved, but a
    missing required field or a wrongly typed value is always an error.
    """


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractDecodeError(f"{path}: expected an object, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise ContractDecodeError(f"{path}: object keys must be strings")
    return cast(Mapping[str, Any], value)


def _require_field(mapping: Mapping[str, Any], key: str, path: str) -> object:
    if key not in mapping:
        raise ContractDecodeError(f"{path}: missing required field {key!r}")
    return mapping[key]


def _decode_str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ContractDecodeError(f"{path}: expected a string, got {type(value).__name__}")
    return value


def _decode_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractDecodeError(f"{path}: expected an integer, got {type(value).__name__}")
    return value


def _decode_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractDecodeError(f"{path}: expected a boolean, got {type(value).__name__}")
    return value


def _decode_sequence(value: object, path: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractDecodeError(f"{path}: expected an array, got {type(value).__name__}")
    return tuple(value)


def _decode_json_value(value: object, path: str) -> Any:
    """Recursively copy and validate an opaque JSON value.

    Objects become read-only mappings and arrays become tuples, so an opaque
    payload carried by a frozen dataclass cannot be mutated through the field.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ContractDecodeError(f"{path}: {value!r} is not representable in JSON")
        return value
    if isinstance(value, Mapping):
        mapping = _require_mapping(value, path)
        return MappingProxyType(
            {key: _decode_json_value(item, f"{path}.{key}") for key, item in mapping.items()}
        )
    if isinstance(value, Sequence):
        return tuple(
            _decode_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)
        )
    raise ContractDecodeError(f"{path}: {type(value).__name__} is not a JSON value")


def _decode_json_object(value: object, path: str) -> Mapping[str, Any]:
    mapping = _require_mapping(value, path)
    return MappingProxyType(
        {key: _decode_json_value(item, f"{path}.{key}") for key, item in mapping.items()}
    )


def _encode_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _encode_json_value(item) for key, item in value.items()}
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return value
    return [_encode_json_value(item) for item in value]


def _encode_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _encode_json_value(item) for key, item in value.items()}
'''.strip()


def python_annotation(type_ref: TypeRef, by_name: dict[str, Definition]) -> str:
    """Render the Python annotation for a resolved property type."""
    if type_ref.kind == "definition":
        assert type_ref.name is not None
        return type_ref.name
    if type_ref.kind == "string":
        return "str"
    if type_ref.kind == "integer":
        return "int"
    if type_ref.kind == "boolean":
        return "bool"
    if type_ref.kind == "array":
        return f"tuple[{python_annotation(type_ref.inner, by_name)}, ...]"
    if type_ref.kind == "map":
        return f"Mapping[str, {python_annotation(type_ref.inner, by_name)}]"
    return "JsonObject"


def _scalar_decoder(type_ref: TypeRef, by_name: dict[str, Definition]) -> str | None:
    """Return the single-expression decoder name for a non-collection type."""
    kind = type_ref.kind
    if kind == "definition":
        assert type_ref.name is not None
        kind = by_name[type_ref.name].kind
        if kind == "object":
            return f"{type_ref.name}.from_wire"
        if kind == "union":
            return f"{snake(type_ref.name)}_from_wire"
    return {
        "string": "_decode_str",
        "integer": "_decode_int",
        "boolean": "_decode_bool",
        "json_object": "_decode_json_object",
    }.get(kind)


def python_decode_lines(
    target: str,
    type_ref: TypeRef,
    source_expr: str,
    path_suffix: str,
    by_name: dict[str, Definition],
    indent: int,
) -> list[str]:
    """Emit statements that assign ``target`` from ``source_expr``."""
    path_expr = f'f"{{path}}{path_suffix}"' if path_suffix else "path"

    scalar = _scalar_decoder(type_ref, by_name)
    if scalar is not None:
        return call_lines(f"{target} = ", scalar, [source_expr, path_expr], indent)

    if type_ref.kind == "array":
        item_decoder = _scalar_decoder(type_ref.inner, by_name)
        if item_decoder is None:
            raise UnsupportedSchemaError("nested collections are not supported")
        return [
            *call_lines(f"{target}_items = ", "_decode_sequence", [source_expr, path_expr], indent),
            f"{target} = tuple(",
            f'    {item_decoder}(item, f"{{path}}{path_suffix}[{{index}}]")',
            f"    for index, item in enumerate({target}_items)",
            ")",
        ]

    if type_ref.kind == "map":
        value_decoder = _scalar_decoder(type_ref.inner, by_name)
        if value_decoder is None:
            raise UnsupportedSchemaError("nested collections are not supported")
        return [
            *call_lines(
                f"{target}_entries = ", "_require_mapping", [source_expr, path_expr], indent
            ),
            f"{target} = MappingProxyType(",
            "    {",
            f'        key: {value_decoder}(value, f"{{path}}{path_suffix}.{{key}}")',
            f"        for key, value in {target}_entries.items()",
            "    }",
            ")",
        ]

    raise UnsupportedSchemaError(f"cannot decode type kind {type_ref.kind!r}")


def python_encode_expr(type_ref: TypeRef, accessor: str, by_name: dict[str, Definition]) -> str:
    """Return the expression that renders ``accessor`` back onto the wire."""
    kind = type_ref.kind
    if kind == "definition":
        assert type_ref.name is not None
        kind = by_name[type_ref.name].kind
        if kind == "object":
            return f"{accessor}.to_wire()"
        if kind == "union":
            return f"{snake(type_ref.name)}_to_wire({accessor})"
    if kind == "json_object":
        return f"_encode_json_object({accessor})"
    if kind in {"string", "integer", "boolean"}:
        return accessor
    if kind == "array":
        inner = python_encode_expr(type_ref.inner, "item", by_name)
        if inner == "item":
            return f"list({accessor})"
        return f"[{inner} for item in {accessor}]"
    if kind == "map":
        inner = python_encode_expr(type_ref.inner, "value", by_name)
        if inner == "value":
            return f"dict({accessor})"
        return f"{{key: {inner} for key, value in {accessor}.items()}}"
    raise UnsupportedSchemaError(f"cannot encode type kind {kind!r}")


def emit_python_dataclass(definition: Definition, by_name: dict[str, Definition]) -> list[str]:
    """Emit a frozen dataclass with explicit ``to_wire`` / ``from_wire``."""
    lines = ["@dataclass(frozen=True, slots=True)", f"class {definition.name}:"]
    lines += docstring(definition.description, "    ")
    lines.append("")

    required = [prop for prop in definition.properties if prop.required]
    optional = [prop for prop in definition.properties if not prop.required]
    for prop in required:
        lines.append(f"    {prop.name}: {python_annotation(prop.type, by_name)}")
    for prop in optional:
        lines.append(f"    {prop.name}: {python_annotation(prop.type, by_name)} | None = None")
    lines.append("")

    # to_wire
    lines.append("    def to_wire(self) -> dict[str, Any]:")
    lines.append('        """Render this value as a JSON-compatible mapping.')
    lines.append("")
    lines += wrap(
        "Absent optional fields are omitted rather than emitted as null, so a "
        "decode/encode round trip reproduces the original document exactly.",
        "        ",
    )
    lines.append('        """')
    lines.append("        wire: dict[str, Any] = {}")
    for prop in definition.properties:
        expr = python_encode_expr(prop.type, f"self.{prop.name}", by_name)
        if prop.required:
            lines.append(f'        wire["{prop.name}"] = {expr}')
        else:
            lines.append(f"        if self.{prop.name} is not None:")
            lines.append(f'            wire["{prop.name}"] = {expr}')
    lines.append("        return wire")
    lines.append("")

    # from_wire
    lines.append("    @classmethod")
    signature = (
        f'    def from_wire(cls, payload: object, path: str = "{definition.name}")'
        f" -> {definition.name}:"
    )
    if len(signature) <= MAX_LINE_LENGTH:
        lines.append(signature)
    else:
        lines.append("    def from_wire(")
        lines.append(f'        cls, payload: object, path: str = "{definition.name}"')
        lines.append(f"    ) -> {definition.name}:")
    lines.append(f'        """Decode a wire payload into a {definition.name}.')
    lines.append("")
    lines += wrap(
        "Unknown fields are ignored so a newer peer's additive minor release "
        "still decodes here. Missing required fields and wrongly typed values "
        "raise ContractDecodeError.",
        "        ",
    )
    lines.append('        """')
    lines.append("        mapping = _require_mapping(payload, path)")
    for prop in definition.properties:
        suffix = f".{prop.name}"
        target = f"field_{prop.name}"
        if prop.required:
            source = f'_require_field(mapping, "{prop.name}", path)'
            for line in python_decode_lines(target, prop.type, source, suffix, by_name, 8):
                lines.append(f"        {line}")
        else:
            annotation = f"{python_annotation(prop.type, by_name)} | None"
            lines.append(f"        {target}: {annotation} = None")
            lines.append(f'        if "{prop.name}" in mapping:')
            lines.append(f'            raw_{prop.name} = mapping["{prop.name}"]')
            lines.append(f"            if raw_{prop.name} is None:")
            lines.append("                raise ContractDecodeError(")
            lines.append(f'                    f"{{path}}{suffix}: null is not a valid value"')
            lines.append("                )")
            raw = f"raw_{prop.name}"
            for line in python_decode_lines(target, prop.type, raw, suffix, by_name, 12):
                lines.append(f"            {line}")
    lines.append("        return cls(")
    for prop in definition.properties:
        lines.append(f"            {prop.name}=field_{prop.name},")
    lines.append("        )")
    return lines


def emit_python_union(definition: Definition) -> list[str]:
    """Emit a union alias plus its discriminating codec functions."""
    alias = " | ".join(definition.members)
    function = snake(definition.name)
    keys = ", ".join(f'"{key}"' for key, _ in definition.discriminators)
    lines = [f"{definition.name}: TypeAlias = {alias}"]
    lines += docstring(definition.description, "")
    lines += ["", ""]

    lines.append(f"def {function}_from_wire(")
    lines.append(f'    payload: object, path: str = "{definition.name}"')
    lines.append(f") -> {definition.name}:")
    lines.append(f'    """Decode a wire payload into exactly one {definition.name} branch.')
    lines.append("")
    lines += wrap(
        "The branches are mutually exclusive by construction: a payload carrying more "
        "than one discriminator, or none at all, is rejected rather than guessed at.",
        "    ",
    )
    lines.append('    """')
    lines.append("    mapping = _require_mapping(payload, path)")
    lines.append(f"    discriminators = ({keys})")
    lines.append("    matched = tuple(key for key in discriminators if key in mapping)")
    lines.append("    if len(matched) != 1:")
    lines.append("        raise ContractDecodeError(")
    lines.append(
        '            f"{path}: expected exactly one of {discriminators}, found {matched}"'
    )
    lines.append("        )")
    for key, member in definition.discriminators:
        lines.append(f'    if matched[0] == "{key}":')
        lines.append(f"        return {member}.from_wire(mapping, path)")
    lines.append('    raise ContractDecodeError(f"{path}: unreachable discriminator state")')
    lines.append("")
    lines.append("")
    lines.append(f"def {function}_to_wire(value: {definition.name}) -> dict[str, Any]:")
    lines.append(f'    """Render one {definition.name} branch as a JSON-compatible mapping."""')
    lines.append("    return value.to_wire()")
    return lines


def emit_python(contract: Contract) -> str:
    """Emit the whole generated Python module."""
    by_name = {definition.name: definition for definition in contract.definitions}
    lines: list[str] = []
    lines += generated_header(
        "#",
        "Frozen dataclasses, type aliases, and frozen vocabulary for the OmniVia Core\n"
        "Application Contract v1. Standard library only: this module must never depend\n"
        "on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework.",
    )
    lines += [
        "",
        '"""Generated Application Contract v1 types (ADR-038).',
        "",
    ]
    lines += wrap(
        "Structural decoding lives here; conformance validation does not. `from_wire` "
        "ignores unknown fields and preserves unknown open string values, which is the "
        "production posture. Strict rejection of unknown fields is the job of the "
        "canonical JSON Schemas.",
        "",
    )
    lines += ['"""', "", "from __future__ import annotations", ""]
    lines += [
        "from collections.abc import Mapping, Sequence",
        "from dataclasses import dataclass",
        "from math import isfinite",
        "from types import MappingProxyType",
        "from typing import Any, Final, TypeAlias, cast",
        "",
    ]

    exported: list[str] = ["ContractDecodeError"]

    body: list[str] = []
    body += ["", PYTHON_PREAMBLE, "", ""]

    body.append("# --- contract identity -----------------------------------------------------")
    body.append("")
    body.append(f'CONTRACT_VERSION: Final = "{contract.contract_version}"')
    body.append(f'SCHEMA_BASE_URI: Final = "{BASE_URI}"')
    exported += ["CONTRACT_VERSION", "SCHEMA_BASE_URI"]
    body.append("")

    body.append("# --- frozen vocabulary -----------------------------------------------------")
    body.append("#")
    body += wrap(
        "ErrorCode and RetryClass are open patterned strings on the wire, so these "
        "constants are the frozen v1 vocabulary rather than a closed enumeration. A "
        "value outside them is valid and must be preserved, not coerced.",
        "# ",
    )
    body.append("")
    for code, _ in contract.error_catalogue:
        name = f"ERROR_CODE_{code.upper()}"
        body.append(f'{name}: Final = "{code}"')
        exported.append(name)
    body.append("")
    body.append("FROZEN_ERROR_CODES: Final[tuple[str, ...]] = (")
    for code, _ in contract.error_catalogue:
        body.append(f"    ERROR_CODE_{code.upper()},")
    body.append(")")
    exported.append("FROZEN_ERROR_CODES")
    body.append("")
    for retry_class, description in contract.retry_classes:
        name = f"RETRY_CLASS_{retry_class.upper()}"
        body.append(f'{name}: Final = "{retry_class}"')
        single = f'"""{description}"""'
        if len(single) <= MAX_LINE_LENGTH:
            body.append(single)
        else:
            body.append('"""')
            body += wrap(description, "")
            body.append('"""')
        exported.append(name)
    body.append("")
    body.append("FROZEN_RETRY_CLASSES: Final[tuple[str, ...]] = (")
    for retry_class, _ in contract.retry_classes:
        body.append(f"    RETRY_CLASS_{retry_class.upper()},")
    body.append(")")
    exported.append("FROZEN_RETRY_CLASSES")
    body.append("")
    body.append("RETRYABLE_RETRY_CLASSES: Final[frozenset[str]] = frozenset(")
    body.append("    {")
    for retry_class in contract.retryable_retry_classes:
        body.append(f"        RETRY_CLASS_{retry_class.upper()},")
    body.append("    }")
    body.append(")")
    body.append('"""The only retry classes a caller may blind-retry.')
    body.append("")
    body += wrap(
        "Anything outside this set, including a class introduced by a newer peer, fails "
        "safe as non-retryable.",
        "",
    )
    body.append('"""')
    exported.append("RETRYABLE_RETRY_CLASSES")
    body.append("")
    body.append("DEFAULT_RETRY_CLASSIFICATION: Final[Mapping[str, str]] = MappingProxyType(")
    body.append("    {")
    for code, retry_class in contract.error_catalogue:
        body.append(f"        ERROR_CODE_{code.upper()}: RETRY_CLASS_{retry_class.upper()},")
    body.append("    }")
    body.append(")")
    body.append('"""Frozen retry classification for every v1 error code."""')
    exported.append("DEFAULT_RETRY_CLASSIFICATION")
    body.append("")
    for constant, values in (
        ("COMPATIBILITY_STATUSES", contract.compatibility_statuses),
        ("UPGRADE_STATES", contract.upgrade_states),
    ):
        body.append(f"{constant}: Final[tuple[str, ...]] = (")
        for value in values:
            body.append(f'    "{value}",')
        body.append(")")
        exported.append(constant)
    body.append("")
    for value in contract.compatibility_statuses:
        name = f"COMPATIBILITY_STATUS_{value.upper()}"
        body.append(f'{name}: Final = "{value}"')
        exported.append(name)
    body.append("")
    for value in contract.upgrade_states:
        name = f"UPGRADE_STATE_{value.upper()}"
        body.append(f'{name}: Final = "{value}"')
        exported.append(name)
    body.append("")
    body.append("")

    body.append("# --- wire patterns ---------------------------------------------------------")
    body.append("#")
    body += wrap(
        "Published for callers that need to validate a value without a JSON Schema "
        "library. The generated decoders deliberately do not apply them: structural "
        "decoding and conformance validation are separate concerns.",
        "# ",
    )
    body.append("")
    for definition in sorted(contract.definitions, key=lambda item: (item.source, item.order)):
        if definition.kind == "string" and definition.pattern:
            name = f"{screaming_snake(definition.name)}_PATTERN"
            single = f"{name}: Final = {definition.pattern!r}"
            if len(single) <= MAX_LINE_LENGTH:
                body.append(single)
            else:
                body.append(f"{name}: Final = (")
                for piece in chunk_string(definition.pattern, 72):
                    body.append(f"    {piece!r}")
                body.append(")")
            exported.append(name)
    body.append("")
    body.append("")

    body.append("# --- generated types -------------------------------------------------------")
    body.append("")
    for definition in contract.definitions:
        exported.append(definition.name)
        if definition.kind in {"string", "integer"}:
            python_type = "str" if definition.kind == "string" else "int"
            body.append(f"{definition.name}: TypeAlias = {python_type}")
            body += docstring(definition.description, "")
            body.append("")
        elif definition.kind == "json_object":
            body.append(f"{definition.name}: TypeAlias = Mapping[str, Any]")
            body += docstring(definition.description, "")
            body.append("")
        elif definition.kind == "union":
            body += emit_python_union(definition)
            exported += [f"{snake(definition.name)}_from_wire", f"{snake(definition.name)}_to_wire"]
            body.append("")
            body.append("")
        else:
            body += emit_python_dataclass(definition, by_name)
            body.append("")
            body.append("")

    lines.append("__all__ = [")
    for name in sort_all_exports(exported):
        lines.append(f'    "{name}",')
    lines.append("]")
    lines += body

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# TypeScript emitter
# --------------------------------------------------------------------------


def typescript_annotation(type_ref: TypeRef) -> str:
    """Render the TypeScript type for a resolved property type."""
    if type_ref.kind == "definition":
        assert type_ref.name is not None
        return type_ref.name
    if type_ref.kind == "string":
        return "string"
    if type_ref.kind == "integer":
        return "number"
    if type_ref.kind == "boolean":
        return "boolean"
    if type_ref.kind == "array":
        return f"readonly {typescript_annotation(type_ref.inner)}[]"
    if type_ref.kind == "map":
        return f"Readonly<Record<string, {typescript_annotation(type_ref.inner)}>>"
    return JSON_OBJECT_DEFINITION


def typescript_doc(description: str, indent: str) -> list[str]:
    """Render a TSDoc block at a fixed indent."""
    wrapped = wrap(description, f"{indent} * ")
    return [f"{indent}/**", *wrapped, f"{indent} */"]


def emit_typescript(contract: Contract) -> str:
    """Emit the whole generated TypeScript module."""
    lines: list[str] = []
    lines += generated_header(
        "//",
        "Type declarations for the OmniVia Core Application Contract v1. This module is\n"
        "declaration-only: it has no imports, no runtime dependencies, and no behaviour\n"
        "beyond the frozen vocabulary constants below.",
    )
    lines.append("")

    lines += typescript_doc(
        "Any value expressible in JSON. Used only inside opaque contract payloads.", ""
    )
    lines.append("export type JsonValue =")
    lines.append("  | string")
    lines.append("  | number")
    lines.append("  | boolean")
    lines.append("  | null")
    lines.append("  | readonly JsonValue[]")
    lines.append("  | { readonly [key: string]: JsonValue };")
    lines.append("")

    lines += typescript_doc("Contract version of this generated module.", "")
    lines.append(f'export const CONTRACT_VERSION = "{contract.contract_version}" as const;')
    lines.append("")
    lines += typescript_doc("Base URI every canonical v1 schema `$id` is rooted at.", "")
    lines.append(f'export const SCHEMA_BASE_URI = "{BASE_URI}" as const;')
    lines.append("")

    for definition in contract.definitions:
        if definition.kind == "string":
            lines += typescript_doc(definition.description, "")
            lines.append(f"export type {definition.name} = string;")
            if definition.pattern:
                name = f"{screaming_snake(definition.name)}_PATTERN"
                single = f"export const {name}: string = {json.dumps(definition.pattern)};"
                if len(single) <= MAX_LINE_LENGTH:
                    lines.append(single)
                else:
                    lines.append(f"export const {name}: string =")
                    pieces = chunk_string(definition.pattern, 72)
                    for index, piece in enumerate(pieces):
                        terminator = ";" if index == len(pieces) - 1 else " +"
                        lines.append(f"  {json.dumps(piece)}{terminator}")
            lines.append("")
        elif definition.kind == "integer":
            lines += typescript_doc(definition.description, "")
            lines.append(f"export type {definition.name} = number;")
            lines.append("")
        elif definition.kind == "json_object":
            lines += typescript_doc(definition.description, "")
            lines.append(
                f"export type {definition.name} = {{ readonly [key: string]: JsonValue }};"
            )
            lines.append("")
        elif definition.kind == "union":
            lines += typescript_doc(definition.description, "")
            members = " | ".join(definition.members)
            lines.append(f"export type {definition.name} = {members};")
            lines.append("")
        else:
            lines += typescript_doc(definition.description, "")
            lines.append(f"export interface {definition.name} {{")
            for prop in definition.properties:
                lines += typescript_doc(prop.description, "  ")
                optional = "" if prop.required else "?"
                lines.append(
                    f"  readonly {prop.name}{optional}: {typescript_annotation(prop.type)};"
                )
            lines.append("}")
            lines.append("")

    lines += typescript_doc(
        "The frozen v1 error-code vocabulary. ErrorCode stays an open string on the "
        "wire, so a value outside this list is valid and must be preserved.",
        "",
    )
    lines.append("export const FROZEN_ERROR_CODES = [")
    for code, _ in contract.error_catalogue:
        lines.append(f'  "{code}",')
    lines.append("] as const;")
    lines.append("")
    lines.append("export type FrozenErrorCode = (typeof FROZEN_ERROR_CODES)[number];")
    lines.append("")

    lines += typescript_doc(
        "The frozen v1 retry-class vocabulary. RetryClass stays an open string on the "
        "wire; an unrecognized class must fail safe as non-retryable.",
        "",
    )
    lines.append("export const FROZEN_RETRY_CLASSES = [")
    for retry_class, _ in contract.retry_classes:
        lines.append(f'  "{retry_class}",')
    lines.append("] as const;")
    lines.append("")
    lines.append("export type FrozenRetryClass = (typeof FROZEN_RETRY_CLASSES)[number];")
    lines.append("")

    lines += typescript_doc("The only retry classes a caller may blind-retry.", "")
    lines.append("export const RETRYABLE_RETRY_CLASSES = [")
    for retry_class in contract.retryable_retry_classes:
        lines.append(f'  "{retry_class}",')
    lines.append("] as const;")
    lines.append("")

    lines += typescript_doc("Frozen retry classification for every v1 error code.", "")
    lines.append(
        "export const DEFAULT_RETRY_CLASSIFICATION: "
        "Readonly<Record<FrozenErrorCode, FrozenRetryClass>> = {"
    )
    for code, retry_class in contract.error_catalogue:
        lines.append(f'  {code}: "{retry_class}",')
    lines.append("};")
    lines.append("")

    lines += typescript_doc(
        "Known compatibility statuses. The wire field is an open code; preserve unknown values.",
        "",
    )
    lines.append("export const COMPATIBILITY_STATUSES = [")
    for value in contract.compatibility_statuses:
        lines.append(f'  "{value}",')
    lines.append("] as const;")
    lines.append("")

    lines += typescript_doc(
        "Known upgrade states. The wire field is an open code; preserve unknown values.", ""
    )
    lines.append("export const UPGRADE_STATES = [")
    for value in contract.upgrade_states:
        lines.append(f'  "{value}",')
    lines.append("] as const;")

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def render_all() -> dict[Path, str]:
    """Build every generated artifact in memory."""
    contract = build_contract()
    return {PYTHON_TARGET: emit_python(contract), TYPESCRIPT_TARGET: emit_typescript(contract)}


def write_all(artifacts: dict[Path, str]) -> list[Path]:
    """Write artifacts to disk, returning the ones whose content changed."""
    changed: list[Path] = []
    for path, content in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed.append(path)
    return changed


def check_all(artifacts: dict[Path, str]) -> list[str]:
    """Return one finding per artifact that is missing or has drifted."""
    findings: list[str] = []
    for path, content in artifacts.items():
        relative = path.relative_to(REPO_ROOT)
        if not path.exists():
            findings.append(f"{relative}: generated artifact is missing")
            continue
        if path.read_text(encoding="utf-8") != content:
            findings.append(f"{relative}: generated artifact is out of date with the schemas")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed artifacts match the schemas instead of writing them",
    )
    args = parser.parse_args(argv)

    try:
        artifacts = render_all()
    except UnsupportedSchemaError as error:
        print(f"Application contract generation FAILED: {error}", file=sys.stderr)
        return 1

    if args.check:
        findings = check_all(artifacts)
        if findings:
            print("Application contract artifacts are out of date:\n", file=sys.stderr)
            for finding in findings:
                print(f"  - {finding}", file=sys.stderr)
            print(
                "\nRegenerate with: python scripts/generate-application-contracts.py",
                file=sys.stderr,
            )
            return 1
        print("Application contract artifacts are up to date.")
        return 0

    changed = write_all(artifacts)
    for path in changed:
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    if not changed:
        print("Application contract artifacts are already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
