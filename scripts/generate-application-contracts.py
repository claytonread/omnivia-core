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
- inside a property: ``$ref``, ``string``, ``integer``, ``number``, ``boolean``,
  ``array`` with ``items``, and ``object`` with ``additionalProperties`` (an
  open map).

Pattern, length, and range constraints are *not* applied by the decoders: strict
conformance is JSON Schema's job (see ``scripts/check-application-contracts.py``),
while the generated ``from_wire`` functions are the tolerant production path that
ignores unknown fields and preserves unknown open string values. That posture is
about decoders and is unchanged.

What the generator does emit, beside the structural types, is one *value-domain
guard* per patterned scalar ``$defs`` entry -- ``is_<snake>`` in Python,
``is<Name>`` in TypeScript -- applying the declared ``pattern`` as a full match
together with the declared ``minLength``/``maxLength``, and, for a declared
``format: date-time``, the calendar the pattern cannot express. The guards are
emitted from the declaration rather than from a list of definition names, in both
languages from the same loop, so a value one binding publishes and the other
refuses is not a state this generator can reach. A ``$defs`` entry declaring a
closed ``enum`` gets the same treatment in TypeScript: its values and an
``is<Name>`` membership guard, emitted from the declaration, so a vocabulary joins
or leaves by being edited in the schema.

Nothing calls a guard from ``from_wire``. A guard is a primitive for a caller that
needs the declared domain -- a public boundary, a publication path -- so that such
a caller has one function to call instead of compiling the published pattern
constant a fourth time. Composite and cross-field invariants ("this must not be
after that") are not derivable from the schema and stay hand-maintained in
``src/omnivia_core/contracts/v1/semantics_*.py``.
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
    "workspace",
    "memory",
    "evidence",
    "knowledge",
    "graph",
    "context-pack",
    "compatibility-matrix",
)
#: The reference-only registry. It contributes annotations, never definitions.
REGISTRY_SCHEMA = "application-v1"

#: The opaque JSON object definition, handled specially by both emitters.
JSON_OBJECT_DEFINITION = "JsonObject"

#: The canonical operation catalogue, and the definition every entry materializes.
OPERATION_CATALOGUE_ANNOTATION = "x-omnivia-operation-catalogue"
OPERATION_METADATA_DEFINITION = "OperationMetadata"

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

    ``kind`` is one of ``definition``, ``string``, ``integer``, ``number``,
    ``boolean``, ``array``, ``map``, or ``json_object``.
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
    #: ``minLength``/``maxLength``/``format`` as the schema declares them, carried
    #: so the value-domain guards are emitted from the declaration rather than from
    #: a table of definition names kept in step by hand.
    min_length: int | None = None
    max_length: int | None = None
    string_format: str | None = None
    #: A declared closed ``enum``, carried for the same reason as ``pattern``: the
    #: value domain is emitted from the declaration, not from a hand-kept table.
    enum_values: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    discriminators: tuple[tuple[str, str], ...] = ()
    dependencies: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class CatalogueValue:
    """One validated value from the canonical operation catalogue annotation.

    The annotation is ordinary JSON, so it has to be checked against the very
    definitions it claims to materialize before either emitter renders it.
    Parsing it into this shape once means both emitters walk the same validated
    tree and reject the same drift, rather than each re-deciding what a
    catalogue entry is allowed to contain.
    """

    kind: str
    name: str | None = None
    scalar: str | int | bool | None = None
    items: tuple[CatalogueValue, ...] = ()
    fields: tuple[tuple[str, CatalogueValue], ...] = ()


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
    operation_catalogue: tuple[CatalogueValue, ...]


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
    if node_type == "number":
        return TypeRef("number")
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
        min_length = node.get("minLength")
        if min_length is not None and not isinstance(min_length, int):
            raise UnsupportedSchemaError(f"{location}: minLength must be an integer")
        max_length = node.get("maxLength")
        if max_length is not None and not isinstance(max_length, int):
            raise UnsupportedSchemaError(f"{location}: maxLength must be an integer")
        string_format = node.get("format")
        if string_format is not None and not isinstance(string_format, str):
            raise UnsupportedSchemaError(f"{location}: format must be a string")
        enum_values: tuple[str, ...] = ()
        if "enum" in node:
            raw_enum = node["enum"]
            if (
                not isinstance(raw_enum, list)
                or not raw_enum
                or not all(isinstance(item, str) for item in raw_enum)
            ):
                raise UnsupportedSchemaError(f"{location}: enum must be a non-empty array of strings")
            enum_values = tuple(str(item) for item in raw_enum)
            if len(set(enum_values)) != len(enum_values):
                raise UnsupportedSchemaError(f"{location}: enum repeats a value")
        return Definition(
            name=name,
            kind="string",
            source=source,
            order=order,
            description=description,
            pattern=pattern,
            min_length=min_length,
            max_length=max_length,
            string_format=string_format,
            enum_values=enum_values,
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


def parse_catalogue_value(
    type_ref: TypeRef, value: object, by_name: dict[str, Definition], location: str
) -> CatalogueValue:
    """Validate one annotation value against the type the contract declares for it.

    The catalogue annotation is the canonical source, but it is hand-authored
    JSON sitting in a schema document that does not validate it. Walking it
    against the definitions it materializes is what makes emission safe: a
    field the contract does not declare, a missing required field, or a value
    of the wrong JSON type fails here rather than becoming wrong generated
    code.
    """
    kind = type_ref.kind
    if kind == "definition":
        assert type_ref.name is not None
        definition = by_name[type_ref.name]
        if definition.kind == "object":
            return _parse_catalogue_object(definition, value, by_name, location)
        if definition.kind not in {"string", "integer"}:
            raise UnsupportedSchemaError(
                f"{location}: a catalogue value cannot be a {definition.kind!r} definition"
            )
        kind = definition.kind

    if kind == "string":
        if not isinstance(value, str):
            raise UnsupportedSchemaError(
                f"{location}: expected a string, got {type(value).__name__}"
            )
        return CatalogueValue("string", scalar=value)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise UnsupportedSchemaError(
                f"{location}: expected a boolean, got {type(value).__name__}"
            )
        return CatalogueValue("boolean", scalar=value)
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnsupportedSchemaError(
                f"{location}: expected an integer, got {type(value).__name__}"
            )
        return CatalogueValue("integer", scalar=value)
    if kind == "array":
        if not isinstance(value, list):
            raise UnsupportedSchemaError(
                f"{location}: expected an array, got {type(value).__name__}"
            )
        return CatalogueValue(
            "array",
            items=tuple(
                parse_catalogue_value(type_ref.inner, item, by_name, f"{location}[{index}]")
                for index, item in enumerate(value)
            ),
        )
    raise UnsupportedSchemaError(f"{location}: a catalogue value cannot be of kind {kind!r}")


def _parse_catalogue_object(
    definition: Definition, value: object, by_name: dict[str, Definition], location: str
) -> CatalogueValue:
    """Validate one annotation object against the object definition it materializes."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise UnsupportedSchemaError(
            f"{location}: expected a {definition.name} object, got {type(value).__name__}"
        )
    unknown = sorted(set(value) - {prop.name for prop in definition.properties})
    if unknown:
        raise UnsupportedSchemaError(f"{location}: {definition.name} declares no field(s) {unknown}")

    fields: list[tuple[str, CatalogueValue]] = []
    for prop in definition.properties:
        if prop.name not in value:
            if prop.required:
                raise UnsupportedSchemaError(
                    f"{location}: {definition.name} is missing required field {prop.name!r}"
                )
            continue
        fields.append(
            (
                prop.name,
                parse_catalogue_value(
                    prop.type, value[prop.name], by_name, f"{location}.{prop.name}"
                ),
            )
        )
    return CatalogueValue("object", name=definition.name, fields=tuple(fields))


def parse_operation_catalogue(
    document: dict[str, Any], by_name: dict[str, Definition]
) -> tuple[CatalogueValue, ...]:
    """Parse the canonical ``x-omnivia-operation-catalogue`` annotation.

    Catalogue order is the annotation's own order and is preserved verbatim, so
    the generated artifacts state the same sequence the canonical document
    does. Whether that order is the frozen sorted one, and whether the entries
    say the right things, is the conformance gate's job
    (``scripts/check-application-contracts.py``); this only has to make sure
    what it emits is a faithful, well typed rendering of what is there.
    """
    entries = document.get(OPERATION_CATALOGUE_ANNOTATION)
    if not isinstance(entries, list):
        raise UnsupportedSchemaError(
            f"operations.schema.json: {OPERATION_CATALOGUE_ANNOTATION!r} must be an array"
        )
    if OPERATION_METADATA_DEFINITION not in by_name:
        raise UnsupportedSchemaError(
            f"the contract must define {OPERATION_METADATA_DEFINITION!r} to carry a catalogue"
        )
    metadata_ref = TypeRef("definition", name=OPERATION_METADATA_DEFINITION)
    return tuple(
        parse_catalogue_value(
            metadata_ref, entry, by_name, f"{OPERATION_CATALOGUE_ANNOTATION}[{index}]"
        )
        for index, entry in enumerate(entries)
    )


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

    operation_catalogue = parse_operation_catalogue(load_schema("operations"), by_name)

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
        operation_catalogue=operation_catalogue,
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


def _decode_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractDecodeError(f"{path}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not isfinite(value):
        raise ContractDecodeError(f"{path}: {value!r} is not representable in JSON")
    try:
        return float(value)
    except OverflowError as error:
        raise ContractDecodeError(
            f"{path}: {value!r} is too large to represent as a JSON number"
        ) from error


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
    if type_ref.kind == "number":
        return "float"
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
        "number": "_decode_number",
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
    if kind in {"string", "integer", "number", "boolean"}:
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
    if definition.properties:
        lines.append("        mapping = _require_mapping(payload, path)")
    else:
        lines.append("        _require_mapping(payload, path)")
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


def python_string_lines(value: str, indent: int) -> list[str]:
    """Render a string literal, splitting to implicit concatenation when it is too long.

    A schema reference is split at its ``#`` fragment boundary when both halves
    fit, so the emitted document and the definition it names each stay readable
    on their own line. Anything else falls back to fixed-width pieces.
    """
    single = json.dumps(value)
    if indent + len(single) <= MAX_LINE_LENGTH:
        return [single]
    document, separator, fragment = value.partition("#")
    pieces = (document, f"{separator}{fragment}")
    if not separator or any(indent + 4 + len(json.dumps(piece)) > MAX_LINE_LENGTH for piece in pieces):
        pieces = chunk_string(value, 72)
    return ["(", *[f"    {json.dumps(piece)}" for piece in pieces], ")"]


def python_catalogue_lines(value: CatalogueValue, indent: int) -> list[str]:
    """Render one validated catalogue value as Python source lines.

    Line 0 opens the expression at column ``indent``; every later line is
    already indented relative to it. Collections render on one line when the
    whole thing fits the shared line budget and one element per line otherwise,
    which is the same rule :func:`call_lines` applies and keeps the output
    stable rather than dependent on how deeply a value happens to nest.
    """
    if value.kind == "boolean":
        return ["True" if value.scalar else "False"]
    if value.kind == "integer":
        return [str(value.scalar)]
    if value.kind == "string":
        assert isinstance(value.scalar, str)
        return python_string_lines(value.scalar, indent)

    if value.kind == "array":
        if not value.items:
            return ["()"]
        rendered = [python_catalogue_lines(item, indent + 4) for item in value.items]
        if all(len(lines) == 1 for lines in rendered):
            inner = ", ".join(lines[0] for lines in rendered)
            # A one-element tuple needs its trailing comma to stay a tuple.
            single = f"({inner},)" if len(rendered) == 1 else f"({inner})"
            if indent + len(single) <= MAX_LINE_LENGTH:
                return [single]
        body: list[str] = []
        for lines in rendered:
            body += [f"    {lines[0]}", *[f"    {line}" for line in lines[1:-1]]]
            if len(lines) > 1:
                body.append(f"    {lines[-1]},")
            else:
                body[-1] += ","
        return ["(", *body, ")"]

    assert value.name is not None
    rendered_fields = [
        (name, python_catalogue_lines(item, indent + 4 + len(name) + 1))
        for name, item in value.fields
    ]
    if all(len(lines) == 1 for _, lines in rendered_fields):
        inner = ", ".join(f"{name}={lines[0]}" for name, lines in rendered_fields)
        single = f"{value.name}({inner})"
        if indent + len(single) <= MAX_LINE_LENGTH:
            return [single]
    field_lines: list[str] = []
    for name, lines in rendered_fields:
        field_lines.append(f"    {name}={lines[0]}")
        field_lines += [f"    {line}" for line in lines[1:-1]]
        if len(lines) > 1:
            field_lines.append(f"    {lines[-1]},")
        else:
            field_lines[-1] += ","
    return [f"{value.name}(", *field_lines, ")"]


def emit_python_catalogue(contract: Contract) -> list[str]:
    """Emit the generated operation catalogue constant."""
    lines = [
        f"OPERATION_CATALOGUE: Final[tuple[{OPERATION_METADATA_DEFINITION}, ...]] = ("
    ]
    for entry in contract.operation_catalogue:
        rendered = python_catalogue_lines(entry, 4)
        lines += [f"    {rendered[0]}", *[f"    {line}" for line in rendered[1:-1]]]
        if len(rendered) > 1:
            lines.append(f"    {rendered[-1]},")
        else:
            lines[-1] += ","
    lines.append(")")
    lines += docstring(
        "The canonical v1 application operation catalogue, in the canonical order. "
        "Generated from `x-omnivia-operation-catalogue`, so this is contract metadata "
        "a caller can read, not a dispatch table: nothing here routes, authorizes, or "
        "executes anything.",
        "",
    )
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


def patterned_strings(contract: Contract) -> list[Definition]:
    """Every patterned scalar definition, in emission order.

    One list, walked by both emitters and by the guard emitters below, so a
    definition cannot acquire a constant in one language and a guard in neither.
    """
    return [
        definition
        for definition in sorted(
            contract.definitions, key=lambda item: (item.source, item.order)
        )
        if definition.kind == "string" and definition.pattern
    ]


def python_length_clause(definition: Definition) -> str | None:
    """Render the declared length bounds as a Python comparison, if any are declared."""
    low, high = definition.min_length, definition.max_length
    if low is not None and high is not None:
        return f"{low} <= len(value) <= {high}"
    if high is not None:
        return f"len(value) <= {high}"
    if low is not None:
        return f"len(value) >= {low}"
    return None


def emit_python_scalar_guards(contract: Contract) -> tuple[list[str], list[str]]:
    """Emit one value-domain guard per patterned scalar definition.

    The rule is the schema, not a list of names: every ``type: string`` definition
    that declares a ``pattern`` gets a guard, and the ``minLength``/``maxLength`` it
    declares are applied where declared. A definition added to the contract acquires
    a guard by being added, which is the property a hand-maintained table cannot have.

    The guards are *not* wired into ``from_wire``. Decoding stays tolerant; these
    exist so a caller who needs the declared domain has one to call rather than
    compiling the published constant a fourth time.
    """
    lines: list[str] = []
    exported: list[str] = []
    for definition in patterned_strings(contract):
        constant = f"{screaming_snake(definition.name)}_PATTERN"
        compiled = f"_{screaming_snake(definition.name)}_RE"
        function = f"is_{snake(definition.name)}"
        calendar = definition.string_format == "date-time"

        lines.append(f"{compiled}: Final = re.compile({constant})")
        lines.append("")
        lines.append("")
        lines.append(f"def {function}(value: object) -> bool:")
        lines.append(f'    """Return whether `value` is a well-formed `{definition.name}`.')
        lines.append("")
        summary = (
            "The declared pattern and length bounds, applied as a full match. The "
            "generated decoders do not call this: it is the primitive a caller "
            "validates with, not a step in the tolerant decode path."
        )
        if calendar:
            summary += (
                " `format: date-time` is the second half -- the pattern fixes the "
                "spelling and cannot fix the calendar, so a pattern-conforming value "
                "that names no instant is refused here."
            )
        lines += wrap(summary, "    ")
        lines.append('    """')

        clauses = ['isinstance(value, str)']
        length = python_length_clause(definition)
        if length is not None:
            clauses.append(length)
        clauses.append(f"{compiled}.fullmatch(value) is not None")
        joined = " and ".join(clauses)
        if calendar:
            if len(f"    if not ({joined}):") <= MAX_LINE_LENGTH:
                lines.append(f"    if not ({joined}):")
            else:
                lines.append("    if not (")
                for index, clause in enumerate(clauses):
                    prefix = "        " if index == 0 else "        and "
                    lines.append(f"{prefix}{clause}")
                lines.append("    ):")
            lines.append("        return False")
            lines.append("    try:")
            lines.append("        datetime.fromisoformat(value)")
            lines.append("    except ValueError:")
            lines.append("        return False")
            lines.append("    return True")
        elif len(f"    return {joined}") <= MAX_LINE_LENGTH:
            lines.append(f"    return {joined}")
        else:
            lines.append("    return (")
            for index, clause in enumerate(clauses):
                prefix = "        " if index == 0 else "        and "
                lines.append(f"{prefix}{clause}")
            lines.append("    )")
        lines.append("")
        lines.append("")
        exported.append(function)
    if lines:
        lines = lines[:-2]
    return lines, exported


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
        "import re",
        "from collections.abc import Mapping, Sequence",
        "from dataclasses import dataclass",
        "from datetime import datetime",
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
    for definition in patterned_strings(contract):
        name = f"{screaming_snake(definition.name)}_PATTERN"
        single = f"{name}: Final = {definition.pattern!r}"
        if len(single) <= MAX_LINE_LENGTH:
            body.append(single)
        else:
            body.append(f"{name}: Final = (")
            for piece in chunk_string(definition.pattern or "", 72):
                body.append(f"    {piece!r}")
            body.append(")")
        exported.append(name)
    body.append("")
    body.append("")

    body.append("# --- value-domain guards ---------------------------------------------------")
    body.append("#")
    body += wrap(
        "One guard per patterned scalar definition, emitted from the declaration "
        "rather than from a list of names. These are not called by `from_wire`: "
        "decoding stays tolerant, and a caller that needs the declared value domain "
        "calls the guard instead of compiling the published pattern again.",
        "# ",
    )
    body.append("")
    guard_lines, guard_exports = emit_python_scalar_guards(contract)
    body += guard_lines
    exported += guard_exports
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

    # The catalogue instantiates the generated dataclasses, so it can only be
    # emitted once every one of them exists.
    body.append("# --- operation catalogue ---------------------------------------------------")
    body.append("")
    body += emit_python_catalogue(contract)
    exported.append("OPERATION_CATALOGUE")

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
    if type_ref.kind in {"integer", "number"}:
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


#: The calendar half of a `format: date-time` guard, appended to the general
#: scalar guard emitted for every patterned definition. Keyed on the declared
#: `format`, not on a definition name: `Timestamp` is the only `date-time` in the
#: v1 contract, and a second one would acquire this tail by declaring the format.
#: Held here as a literal block rather than assembled from `lines.append` calls
#: because the body is prose-heavy and the reason for every clause is the point.
#:
#: `Date.parse` is deliberately absent. It is the obvious way to ask ECMAScript
#: whether a timestamp names an instant and it gives the wrong answer: on the
#: pinned `descriptor-published-at-policy-v1` corpus a bare `Date.parse` accepts
#: `2024-02-30T00:00:00Z` and `2026-02-29T00:00:00Z`, both of which the canonical
#: schema and the Python binding refuse, because the ECMAScript Date constructor
#: normalizes an out-of-range field by rolling forward instead of failing. The
#: parse is therefore used only to *compute* a date, and the verdict comes from
#: comparing that date's UTC fields back to the literals they were built from --
#: any normalization shows up as a field that no longer matches, which is what
#: makes a rolled-forward day, a 24th hour, a 60th minute and a leap second all
#: refusals rather than silent corrections.
#:
#: `Date.UTC` is likewise avoided: it maps years 0-99 onto 1900-1999, so it would
#: refuse `0050-06-15T00:00:00Z`, which the schema and Python both accept.
#: `setUTCFullYear` carries the literal year through unmapped. Year 0000 is the
#: one value the round-trip alone cannot separate -- ECMAScript can represent it
#: and reports it back unchanged -- and the canonical schema's `format: date-time`
#: refuses it, so the floor is stated explicitly.
TYPESCRIPT_DATE_TIME_TAIL = """\
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const hour = Number(value.slice(11, 13));
  const minute = Number(value.slice(14, 16));
  const second = Number(value.slice(17, 19));
  // Year 0000 is representable here and is not a `date-time` the canonical schema accepts.
  if (year < 1) {
    return false;
  }
  // `setUTCFullYear` rather than `Date.UTC`, which would remap years 0-99 onto 1900-1999.
  const at = new Date(0);
  at.setUTCFullYear(year, month - 1, day);
  at.setUTCHours(hour, minute, second, 0);
  return (
    at.getUTCFullYear() === year &&
    at.getUTCMonth() === month - 1 &&
    at.getUTCDate() === day &&
    at.getUTCHours() === hour &&
    at.getUTCMinutes() === minute &&
    at.getUTCSeconds() === second
  );"""


def typescript_scalar_guard(definition: Definition) -> list[str]:
    """Emit one TypeScript value-domain guard for a patterned scalar definition.

    The mirror of :func:`emit_python_scalar_guards`, clause for clause and bound for
    bound, because a value one binding publishes and the other refuses is two
    contracts rather than one. The same schema fields drive both, so the two cannot
    drift without the schema changing under them.
    """
    name = definition.name
    constant = f"{screaming_snake(name)}_PATTERN"
    calendar = definition.string_format == "date-time"

    summary = (
        f"Return whether a value is a well-formed `{name}`: the declared pattern and "
        "length bounds, applied as a full match. The generated decoders do not call "
        "this -- decoding stays tolerant, and this is the primitive a caller "
        "validates with."
    )
    if calendar:
        summary += (
            f" `{constant}` fixes the spelling and cannot fix the calendar: "
            "`2026-13-01T00:00:00Z` satisfies it character for character. `Date.parse` is "
            "not the missing half -- it accepts `2024-02-30T00:00:00Z` and "
            "`2026-02-29T00:00:00Z` by rolling them forward into March, so a guard that "
            "trusted it would admit values this contract's other bindings refuse. The date "
            "is built from the literal fields instead, and every field is compared back: "
            "any value the constructor had to normalize disagrees with the literal it came "
            "from and is refused."
        )

    conditions = ['typeof value === "string"']
    if definition.min_length is not None:
        conditions.append(f"value.length >= {definition.min_length}")
    if definition.max_length is not None:
        conditions.append(f"value.length <= {definition.max_length}")
    conditions.append(f"new RegExp({constant}).test(value)")

    lines = typescript_doc(summary, "")
    lines.append(f"export function is{name}(value: unknown): value is {name} {{")
    if calendar:
        lines.append("  if (")
        lines.append("    !(")
        for index, condition in enumerate(conditions):
            suffix = " &&" if index < len(conditions) - 1 else ""
            lines.append(f"      {condition}{suffix}")
        lines.append("    )")
        lines.append("  ) {")
        lines.append("    return false;")
        lines.append("  }")
        lines += TYPESCRIPT_DATE_TIME_TAIL.split("\n")
    else:
        lines.append("  return (")
        for index, condition in enumerate(conditions):
            suffix = " &&" if index < len(conditions) - 1 else ""
            lines.append(f"    {condition}{suffix}")
        lines.append("  );")
    lines.append("}")
    return lines


def typescript_enum_guard(definition: Definition) -> list[str]:
    """Emit the closed value domain and its guard for a definition declaring an ``enum``.

    The enum counterpart of :func:`typescript_scalar_guard`, and emitted by the same
    rule: any string ``$defs`` entry that declares an ``enum`` gets its values and a
    membership guard, so a vocabulary joins or leaves by being edited in the schema
    rather than by anyone remembering to touch this file.

    The emitted alias stays ``string``: decoding is tolerant and must preserve a value
    it does not recognize. The guard is what a publication boundary calls to refuse one.
    """
    name = definition.name
    constant = f"{screaming_snake(name)}_VALUES"

    lines = typescript_doc(
        f"The closed `{name}` vocabulary, emitted from the schema's `enum`.", ""
    )
    lines.append(f"export const {constant} = [")
    lines += [f"  {json.dumps(value)}," for value in definition.enum_values]
    lines.append("] as const;")
    lines.append("")
    lines += typescript_doc(
        f"Return whether a value is a declared `{name}`. The generated decoders do not "
        "call this -- decoding stays tolerant and preserves an unrecognized value -- and "
        "this is the primitive a caller enforcing the closed domain validates with.",
        "",
    )
    lines.append(f"export function is{name}(value: unknown): value is {name} {{")
    lines.append("  return (")
    lines.append('    typeof value === "string" &&')
    lines.append(f"    ({constant} as readonly string[]).includes(value)")
    lines.append("  );")
    lines.append("}")
    return lines


def typescript_core_target_semantics() -> list[str]:
    """Emit the `CoreTargetV1` semantic helpers, mirroring `validate_core_target`."""
    lines = typescript_doc(
        "Assert target semantics without echoing a rejected value. Checks exactly the "
        "clauses `validate_core_target` checks in Python: `kind` and `management` against "
        "their closed vocabularies, and every scalar against the value domain the schema "
        "`$ref`s it to -- `contract_version` as a `ContractVersion`, `target_ref` and "
        "`endpoint_profile_ref` as `Identifier`s, `workspace_ref` as a `WorkspaceId`, and "
        "`display_name` within its declared 1..256 bound. All of them arrive as tolerant "
        "strings, and this is where the declared domains are enforced on a publication path.",
        "",
    )
    lines.append("export function assertCoreTargetV1Semantics(value: CoreTargetV1): void {")
    lines.append("  if (!isContractVersion(value.contract_version)) {")
    lines.append(
        '    throw new TypeError("contract_version is not a well-formed ContractVersion");'
    )
    lines.append("  }")
    lines.append("  if (!isIdentifier(value.target_ref)) {")
    lines.append('    throw new TypeError("target_ref is not a well-formed Identifier");')
    lines.append("  }")
    lines.append("  if (")
    lines.append('    typeof value.display_name !== "string" ||')
    lines.append("    value.display_name.length < 1 ||")
    lines.append("    value.display_name.length > 256")
    lines.append("  ) {")
    lines.append(
        '    throw new TypeError("display_name is not a string of 1..256 characters");'
    )
    lines.append("  }")
    lines.append("  if (!isCoreTargetKind(value.kind)) {")
    lines.append('    throw new TypeError("kind is not a known CoreTargetKind");')
    lines.append("  }")
    lines.append("  if (!isWorkspaceId(value.workspace_ref)) {")
    lines.append('    throw new TypeError("workspace_ref is not a well-formed WorkspaceId");')
    lines.append("  }")
    lines.append("  if (!isCoreTargetManagement(value.management)) {")
    lines.append('    throw new TypeError("management is not a known CoreTargetManagement");')
    lines.append("  }")
    lines.append("  if (!isIdentifier(value.endpoint_profile_ref)) {")
    lines.append(
        '    throw new TypeError("endpoint_profile_ref is not a well-formed Identifier");'
    )
    lines.append("  }")
    lines.append("}")
    lines.append("")
    lines += typescript_doc(
        "Return whether a structurally decoded target satisfies mandatory semantics. Derived "
        "from the assertion rather than restating its clauses, so the predicate and the "
        "assertion cannot disagree about one field.",
        "",
    )
    lines.append(
        "export function isCoreTargetV1SemanticallyValid(value: CoreTargetV1): boolean {"
    )
    lines.append("  try {")
    lines.append("    assertCoreTargetV1Semantics(value);")
    lines.append("    return true;")
    lines.append("  } catch {")
    lines.append("    return false;")
    lines.append("  }")
    lines.append("}")
    lines.append("")
    lines += typescript_doc(
        "Assert the set-level authority rule `validate_core_target_authorities` enforces in "
        "Python: every target is individually valid, and across the set neither `target_ref` "
        "nor `workspace_ref` repeats. A writable workspace identity belongs to exactly one "
        "target authority, so two descriptors naming the same `workspace_ref` are two "
        "authorities claiming one writable store -- an invariant no single descriptor can "
        "see. No refusal echoes a rejected value.",
        "",
    )
    lines.append(
        "export function assertCoreTargetV1Authorities("
        "value: readonly CoreTargetV1[]): void {"
    )
    lines.append("  const targetRefs = new Set<string>();")
    lines.append("  const workspaceRefs = new Set<string>();")
    lines.append("  for (const target of value) {")
    lines.append("    assertCoreTargetV1Semantics(target);")
    lines.append("    if (targetRefs.has(target.target_ref)) {")
    lines.append('      throw new TypeError("target_ref repeats across the target set");')
    lines.append("    }")
    lines.append("    if (workspaceRefs.has(target.workspace_ref)) {")
    lines.append("      throw new TypeError(")
    lines.append(
        '        "workspace_ref repeats across the target set: '
        'two target authorities " +'
    )
    lines.append('          "may not share one writable workspace identity"')
    lines.append("      );")
    lines.append("    }")
    lines.append("    targetRefs.add(target.target_ref);")
    lines.append("    workspaceRefs.add(target.workspace_ref);")
    lines.append("  }")
    lines.append("}")
    lines.append("")
    lines += typescript_doc(
        "Return whether a set of structurally decoded targets carries non-colliding "
        "authorities. Derived from the assertion rather than restating its clauses.",
        "",
    )
    lines.append(
        "export function areCoreTargetV1AuthoritiesValid("
        "value: readonly CoreTargetV1[]): boolean {"
    )
    lines.append("  try {")
    lines.append("    assertCoreTargetV1Authorities(value);")
    lines.append("    return true;")
    lines.append("  } catch {")
    lines.append("    return false;")
    lines.append("  }")
    lines.append("}")
    return lines


def typescript_core_safe_status_semantics() -> list[str]:
    """Emit the `CoreSafeStatusV1` semantic helpers, mirroring `validate_core_safe_status`.

    The local-action rule is the one clause here no schema field expresses, so the
    action list below is stated rather than derived, exactly as `_LOCAL_ONLY_ACTIONS`
    is in `src/omnivia_core/contracts/v1/semantics_core_target.py`. Both bindings state
    it once; `tests/contracts/test_core_target_semantics.py` holds them to the same
    verdict.
    """
    lines = typescript_doc(
        "Process-lifecycle actions: safe to offer only for a `locally_managed` `local` "
        "target, because no other target's process is this caller's to act on.",
        "",
    )
    lines.append('export const CORE_LOCAL_ONLY_ACTIONS = ["start", "stop", "restart"] as const;')
    lines.append("")
    lines += typescript_doc(
        "Assert safe-status semantics before it reaches a public boundary. Checks exactly "
        "what `validate_core_safe_status` checks in Python: the nested target, its own "
        "`contract_version` and the two optional versions against their value domains, each "
        "of the four normalized states against its closed vocabulary, `warning_codes` and "
        "`permitted_actions` for their declared caps, for duplicates and for undeclared "
        "entries, and then the cross-field invariants the schema cannot express -- the "
        "status is published at the target's `contract_version`, and `start`/`stop`/"
        "`restart` are refused unless the target is both `locally_managed` and `local`. No "
        "refusal includes the rejected value: a safe status is published pre-authentication, "
        "and so is anything thrown while validating one.",
        "",
    )
    lines.append(
        "export function assertCoreSafeStatusV1Semantics(value: CoreSafeStatusV1): void {"
    )
    lines.append("  assertCoreTargetV1Semantics(value.target);")
    lines.append("  if (!isContractVersion(value.contract_version)) {")
    lines.append(
        '    throw new TypeError("contract_version is not a well-formed ContractVersion");'
    )
    lines.append("  }")
    lines.append("  if (value.contract_version !== value.target.contract_version) {")
    lines.append("    throw new TypeError(")
    lines.append('      "contract_version does not match the target\'s contract_version"')
    lines.append("    );")
    lines.append("  }")
    lines.append(
        "  if (value.server_version !== undefined && !isReleaseVersion(value.server_version)) {"
    )
    lines.append('    throw new TypeError("server_version is not a well-formed ReleaseVersion");')
    lines.append("  }")
    lines.append("  if (")
    lines.append("    value.protocol_version !== undefined &&")
    lines.append("    !isContractVersion(value.protocol_version)")
    lines.append("  ) {")
    lines.append(
        '    throw new TypeError("protocol_version is not a well-formed ContractVersion");'
    )
    lines.append("  }")
    for state, vocabulary in (
        ("lifecycle_state", "CoreLifecycleState"),
        ("readiness_state", "CoreReadinessState"),
        ("compatibility_state", "CoreCompatibilityState"),
        ("connection_state", "CoreConnectionState"),
    ):
        lines.append(f"  if (!is{vocabulary}(value.{state})) {{")
        lines.append(f'    throw new TypeError("{state} is not a known {vocabulary}");')
        lines.append("  }")
    for collection, singular, vocabulary, cap in (
        ("warning_codes", "code", "CoreSafeWarningCode", 32),
        ("permitted_actions", "action", "CoreSafeAction", 16),
    ):
        lines.append(f"  if (value.{collection}.length > {cap}) {{")
        lines.append(
            f'    throw new TypeError("{collection} carries more than {cap} entries");'
        )
        lines.append("  }")
        lines.append(f"  if (new Set(value.{collection}).size !== value.{collection}.length) {{")
        lines.append(f'    throw new TypeError("{collection} contains a duplicate {singular}");')
        lines.append("  }")
        lines.append(f"  if (!value.{collection}.every(is{vocabulary})) {{")
        lines.append(
            f'    throw new TypeError("{collection} contains a value that is not a known '
            f'{vocabulary}");'
        )
        lines.append("  }")
    lines.append("  const ownsTheProcess =")
    lines.append(
        '    value.target.management === "locally_managed" && value.target.kind === "local";'
    )
    lines.append("  const localOnly: readonly string[] = CORE_LOCAL_ONLY_ACTIONS;")
    lines.append(
        "  if (!ownsTheProcess && value.permitted_actions.some((a) => localOnly.includes(a))) {"
    )
    lines.append("    throw new TypeError(")
    lines.append(
        '      "start/stop/restart may only be offered for a locally_managed local target"'
    )
    lines.append("    );")
    lines.append("  }")
    lines.append("}")
    lines.append("")
    lines += typescript_doc(
        "Return whether a structurally decoded safe status is safe to publish. Derived from "
        "the assertion rather than restating its clauses, so the predicate and the assertion "
        "cannot disagree about one field.",
        "",
    )
    lines.append(
        "export function isCoreSafeStatusV1SemanticallyValid("
        "value: CoreSafeStatusV1): boolean {"
    )
    lines.append("  try {")
    lines.append("    assertCoreSafeStatusV1Semantics(value);")
    lines.append("    return true;")
    lines.append("  } catch {")
    lines.append("    return false;")
    lines.append("  }")
    lines.append("}")
    return lines


def typescript_catalogue_lines(value: CatalogueValue, indent: int) -> list[str]:
    """Render one validated catalogue value as TypeScript source lines.

    Line 0 opens the expression at column ``indent``; later lines are already
    indented relative to it, in the two-space style the rest of this module
    uses. Property names come from the contract's own definitions, which the
    generator already requires to be valid identifiers in every emitted
    language, so they never need quoting here.
    """
    if value.kind == "boolean":
        return ["true" if value.scalar else "false"]
    if value.kind == "integer":
        return [str(value.scalar)]
    if value.kind == "string":
        return [json.dumps(value.scalar)]

    if value.kind == "array":
        if not value.items:
            return ["[]"]
        rendered = [typescript_catalogue_lines(item, indent + 2) for item in value.items]
        if all(len(lines) == 1 for lines in rendered):
            single = f"[{', '.join(lines[0] for lines in rendered)}]"
            if indent + len(single) <= MAX_LINE_LENGTH:
                return [single]
        body: list[str] = []
        for lines in rendered:
            body += [f"  {lines[0]}", *[f"  {line}" for line in lines[1:-1]]]
            if len(lines) > 1:
                body.append(f"  {lines[-1]},")
            else:
                body[-1] += ","
        return ["[", *body, "]"]

    rendered_fields = [
        (name, typescript_catalogue_lines(item, indent + 2 + len(name) + 2))
        for name, item in value.fields
    ]
    if all(len(lines) == 1 for _, lines in rendered_fields):
        inner = ", ".join(f"{name}: {lines[0]}" for name, lines in rendered_fields)
        single = f"{{ {inner} }}"
        if indent + len(single) <= MAX_LINE_LENGTH:
            return [single]
    field_lines: list[str] = []
    for name, lines in rendered_fields:
        field_lines.append(f"  {name}: {lines[0]}")
        field_lines += [f"  {line}" for line in lines[1:-1]]
        if len(lines) > 1:
            field_lines.append(f"  {lines[-1]},")
        else:
            field_lines[-1] += ","
    return ["{", *field_lines, "}"]


def emit_typescript_catalogue(contract: Contract) -> list[str]:
    """Emit the generated operation catalogue constant."""
    lines = typescript_doc(
        "The canonical v1 application operation catalogue, in the canonical order. "
        "Generated from `x-omnivia-operation-catalogue`, so this is contract metadata "
        "a caller can read, not a dispatch table: nothing here routes, authorizes, or "
        "executes anything.",
        "",
    )
    lines.append(
        f"export const OPERATION_CATALOGUE: readonly {OPERATION_METADATA_DEFINITION}[] = ["
    )
    for entry in contract.operation_catalogue:
        rendered = typescript_catalogue_lines(entry, 2)
        lines += [f"  {rendered[0]}", *[f"  {line}" for line in rendered[1:-1]]]
        if len(rendered) > 1:
            lines.append(f"  {rendered[-1]},")
        else:
            lines[-1] += ","
    lines.append("] as const;")
    return lines


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
                lines += typescript_scalar_guard(definition)
                if definition.name == "ServiceEndpointUri":
                    # The one scalar `assert`, kept named rather than generalized: its
                    # refusal string names `endpoint_uri` and is matched on by tests and
                    # by Runtime's probe boundary, and no rule over the schema can derive
                    # a per-field sentence. The `is` half above is the general rule.
                    lines.append("")
                    lines += typescript_doc(
                        "Assert the endpoint policy without including a rejected value in the error.",
                        "",
                    )
                    lines.append(
                        "export function assertServiceEndpointUri("
                        "value: unknown): asserts value is ServiceEndpointUri {"
                    )
                    lines.append("  if (!isServiceEndpointUri(value)) {")
                    lines.append(
                        '    throw new TypeError("endpoint_uri is not an approved credential-free '
                        'dialable Core transport URI");'
                    )
                    lines.append("  }")
                    lines.append("}")
            if definition.enum_values:
                lines.append("")
                lines += typescript_enum_guard(definition)
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
            if definition.name == "ServiceEndpointDescriptor":
                lines.append("")
                lines += typescript_doc(
                    "Return whether a structurally decoded descriptor satisfies mandatory endpoint "
                    "semantics. Checks exactly the fields `validate_service_endpoint_descriptor` "
                    "checks in Python: a descriptor one binding publishes and the other refuses is "
                    "two contracts, not one.",
                    "",
                )
                lines.append(
                    "export function isServiceEndpointDescriptorSemanticallyValid("
                    "value: ServiceEndpointDescriptor): boolean {"
                )
                lines.append(
                    "  return isServiceEndpointUri(value.endpoint_uri) && "
                    "isTimestamp(value.published_at);"
                )
                lines.append("}")
                lines.append("")
                lines += typescript_doc(
                    "Assert descriptor endpoint semantics without echoing a rejected value.",
                    "",
                )
                lines.append(
                    "export function assertServiceEndpointDescriptorSemantics("
                    "value: ServiceEndpointDescriptor): void {"
                )
                lines.append(
                    "  if (!isServiceEndpointDescriptorSemanticallyValid(value)) {"
                )
                lines.append(
                    '    throw new TypeError("service endpoint descriptor is not safe to publish");'
                )
                lines.append("  }")
                lines.append("}")
            elif definition.name == "ServiceProbeResult":
                lines.append("")
                lines += typescript_doc(
                    "Assert probe-result semantics before it reaches a public boundary. Checks "
                    "exactly what `validate_service_probe_result` checks in Python: its own "
                    "`observed_at`, then the nested descriptor. `observed_at` is refused under "
                    "its own message rather than the descriptor's, because a caller cannot "
                    "otherwise tell which field was unusable.",
                    "",
                )
                lines.append(
                    "export function assertServiceProbeResultSemantics("
                    "value: ServiceProbeResult): void {"
                )
                lines.append("  if (!isTimestamp(value.observed_at)) {")
                lines.append(
                    '    throw new TypeError("observed_at is not a canonical RFC 3339 UTC '
                    'Timestamp");'
                )
                lines.append("  }")
                lines.append("  const descriptor = value.descriptor;")
                lines.append("  if (descriptor !== undefined && descriptor !== null) {")
                lines.append("    assertServiceEndpointDescriptorSemantics(descriptor);")
                lines.append("  }")
                lines.append("}")
            elif definition.name == "CoreTargetV1":
                lines.append("")
                lines += typescript_core_target_semantics()
            elif definition.name == "CoreSafeStatusV1":
                lines.append("")
                lines += typescript_core_safe_status_semantics()
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
    lines.append("")

    # The catalogue's values are typed by the interfaces above, so it can only
    # be emitted once every one of them exists.
    lines += emit_typescript_catalogue(contract)

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
