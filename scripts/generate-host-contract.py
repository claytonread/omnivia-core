#!/usr/bin/env python3
"""Generate and gate the OmniVia Host Contract v1 Python artifact (DOC-004 §AA).

``contracts/host/v1/schemas/host-contract-v1.schema.json`` is the normative
JSON Schema 2020-12 representation of Host Contract v1.0.0. It is not authored
here: it is a verbatim copy of the exact bytes DOC-004 §AA incorporates from
proposal commit ``2568f0049256b86fa58dc2387e8302aa4da94ae1``, approved by
``GOV-HOST-CONTRACT-APPROVAL-001``. Same for the twenty canonical fixtures.

This script does two things, both entirely offline and standard-library only:

- **authority gate** -- every checked-in canonical resource is compared against
  the SHA-256 digest of its approved bytes. Editing a canonical byte without a
  new exact-byte approval is a finding, not a silent pass;
- **generation** -- ``src/omnivia_core/host_contract/v1/generated.py`` is
  emitted deterministically from that schema, so the frozen vocabulary,
  record field sets, canonical wire spellings and structural decoders cannot
  drift from the governed schema.

``--check`` runs both gates without writing anything.

Scope of the supported schema subset
------------------------------------

This is deliberately not a general code generator. It understands exactly the
shapes Host Contract v1 uses and raises :class:`UnsupportedSchemaError` on
anything else, so an unsupported schema edit fails loudly instead of silently
emitting wrong types:

- a ``$defs`` entry that is ``type: string`` becomes a type alias, plus a
  pattern constant when the schema constrains one;
- a ``$defs`` entry that is a bare ``enum`` becomes a frozen value tuple plus
  one named constant per member;
- a ``$defs`` entry that is ``type: array`` becomes a tuple alias;
- a ``$defs`` entry that is ``type: object`` with ``properties`` becomes a
  frozen dataclass;
- inside a property: ``$ref``, inline ``enum``, ``string``, ``integer``,
  ``boolean``, ``array`` with ``items``, and ``object`` without ``properties``
  (an opaque JSON object).

Every constraint the governed schema states is re-implemented in the emitted
``validate()`` method: ``pattern``, ``minLength``/``maxLength``,
``format: date-time``, ``minimum``/``maximum``, ``minItems``, ``uniqueItems``,
``required``, ``additionalProperties: false``, ``enum`` and the record-level
``allOf`` branch invariants. ``from_wire`` decodes and then validates, and
``to_wire`` validates before it emits, so the strict governed path is the only
public path in or out. A schema keyword this generator does not understand
raises :class:`UnsupportedSchemaError` rather than being silently dropped,
which is what keeps "generated" and "enforced" the same set.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_ROOT = REPO_ROOT / "contracts" / "host" / "v1"
SCHEMA_PATH = CANONICAL_ROOT / "schemas" / "host-contract-v1.schema.json"
PYTHON_TARGET = REPO_ROOT / "src" / "omnivia_core" / "host_contract" / "v1" / "generated.py"

SCHEMA_ID = "urn:omnivia:host-contract:1.0.0"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

MAX_LINE_LENGTH = 100

#: The exact-byte approval DOC-004 §AA incorporates. Every value here is
#: transcribed from ``GOV-HOST-CONTRACT-APPROVAL-001`` and DOC-004 §AA.1.
APPROVED_AUTHORITY: dict[str, Any] = {
    "approvalId": "GOV-HOST-CONTRACT-APPROVAL-001",
    "proposalCommit": "2568f0049256b86fa58dc2387e8302aa4da94ae1",
    "proposalContentSetSha256": "31da9251944079ea1bc8db61ed0a393a05e0ce18602f31f6771def4ab243f0ac",
    "proposalEntryCount": 31,
    "hostContractVersion": "1.0.0",
}

#: SHA-256 of the approved bytes of every canonical resource, keyed by its path
#: relative to ``contracts/host/v1``. These are the same digests
#: ``PROPOSAL-SHA256SUMS.txt`` records at the approved proposal commit.
APPROVED_RESOURCE_DIGESTS: dict[str, str] = {
    "fixtures/degradation/optional-capability-missing.json": "9f14dcbd531cd234e613c0c2d0ca061fb15b7a0ab0d06d4729234bfdad2341f9",
    "fixtures/denial/permission-denied.json": "5292c50ce8cb4e9d82563a57f678afcee28f00560c507c4a1f101efd2e4e1615",
    "fixtures/invalid/development-profile-cloud-target.json": "5716622134045e9bfcba5fce90a7e4aeb7770bca62e78c19cdfc83882fa0cc2e",
    "fixtures/invalid/environment-binding-inline-secret.json": "b6e0c39843f2d8338b4f356b530113214bf46fd87b1e01c744d8de7c906149c7",
    "fixtures/invalid/forged-shell-context-request.json": "24c45b2878692a804e4011f0acd3dcdd441ccfca55094abba41b5cb5d375c71c",
    "fixtures/invalid/result-envelope-conflict.json": "c639e6ef0d15d2e286e47a7e578ab882dd1acb184e1258f0236185b63f380d6d",
    "fixtures/invalid/unknown-namespace.json": "bbdc2a3c25219d24ae5d99c5fe375dfe87a56c353d4b27f6bc0575eb277fbfbf",
    "fixtures/migration/v0-display-context-to-v1.json": "30fc3b90a581b600f194b18ac8d442f660e2bb03bd6fb53e19119928a0c8420f",
    "fixtures/valid/development-profile.json": "65681f26f5425134a05c98071db841aac046926e4e29614ccd7d1b427bf148c5",
    "fixtures/valid/environment-binding.json": "06f0e726b76f0b3a27aedf0f3f1c598e274bb06fdeb0644c0e14b355c5e85377",
    "fixtures/valid/host-request.json": "d676f1d009b40e09e8d222a0905e930ac2c1329eac8c2b1eb371cbf14f826b7b",
    "fixtures/valid/host-result.json": "86fd37df835a7e18203f3b02a2ebb2541055e846700fa34a2bb84933356c581c",
    "fixtures/valid/module-contribution.json": "f6b0baabc0ec11fa59e77cdc9bb83b0b53ec481c3929963c035a1cec69b66cad",
    "fixtures/valid/promotion-record.json": "cc8119460ea25851ea47ccea5a22297abb8454b994e29373b15ef3a815de6bee",
    "fixtures/valid/release-package-identity.json": "8d86eb0b7f4636117732923fafe4eefe45292e73b644cfc2a613c10ed084595a",
    "fixtures/valid/rollback-record.json": "90fc04531230fa88e496f4227c54af48bd5b79af5e96bf308281b9d96a1a1358",
    "fixtures/valid/shell-context.json": "81d528ef1976988e41d1111d81e8df67de56006cb4b7ba2d10ae5157872fd4df",
    "fixtures/valid/shell-scenario.json": "53a229757c26d3ca6aea098ebbc5fc92753a2be9064f66c09931e930c7ba8b73",
    "fixtures/valid/test-driver-request.json": "3d1c5804b0b6f370181b299583e13f7c7b3fb3c1d288ec36f87a73621cf262e3",
    "fixtures/valid/test-driver-result.json": "613b7da06ef6fbe00a98a53b4f4dd4ffeae25782b46a43102d88ac2c0c3ff051",
    "schemas/host-contract-v1.schema.json": "cfba6a5608cf4f068894096288d1d9222e619172c3e443dbd5304b7b6a7ff08a",
}


class UnsupportedSchemaError(Exception):
    """Raised when the schema uses a shape this generator deliberately does not support."""


# --------------------------------------------------------------------------
# Authority gate
# --------------------------------------------------------------------------


def check_authority_digests(root: Path | None = None) -> list[str]:
    """Return one finding per canonical resource that is missing, altered, or unapproved.

    ``root`` defaults to :data:`CANONICAL_ROOT` at *call* time rather than at
    definition time, so a caller that redirects the constant is honoured.
    """
    if root is None:
        root = CANONICAL_ROOT
    findings: list[str] = []
    for relative, expected in sorted(APPROVED_RESOURCE_DIGESTS.items()):
        path = root / relative
        if not path.is_file():
            findings.append(f"{relative}: approved canonical resource is missing")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            findings.append(f"{relative}: bytes differ from the approved proposal (got {actual})")
    if root.is_dir():
        present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        for extra in sorted(present - set(APPROVED_RESOURCE_DIGESTS)):
            findings.append(f"{extra}: file has no approved digest in this change set")
    return findings


# --------------------------------------------------------------------------
# Intermediate representation
# --------------------------------------------------------------------------


#: ``$defs`` key -> the record name the approved specification gives it. The
#: schema's keys are lower-camel and deliberately shorter than the governed
#: record names (``request`` is ``HostRequest``, ``availability`` is
#: ``CapabilityAvailability``), so the mapping is declared rather than guessed.
#: :func:`build_model` fails if it does not cover the schema's ``$defs`` exactly.
DEFINITION_NAMES: dict[str, str] = {
    "identifier": "Identifier",
    "semver": "SemVer",
    "contractVersion": "ContractVersion",
    "sha256": "Sha256",
    "target": "Target",
    "namespace": "HostNamespace",
    "availability": "CapabilityAvailability",
    "remediation": "Remediation",
    "denial": "CapabilityDenial",
    "result": "HostResult",
    "request": "HostRequest",
    "shellContext": "ShellContext",
    "compatibilityResult": "CompatibilityResult",
    "moduleContributionDeclaration": "ModuleContributionDeclaration",
    "releasePackageIdentity": "ReleasePackageIdentity",
    "promotionRecord": "PromotionRecord",
    "rollbackRecord": "RollbackRecord",
    "environmentBinding": "EnvironmentBinding",
    "referenceArray": "LogicalReferences",
    "developmentProfile": "DevelopmentProfile",
    "shellScenario": "ShellScenario",
    "testDriverRequest": "TestDriverRequest",
    "testDriverResult": "TestDriverResult",
}

#: JSON pointer of an inline object -> the record name the specification gives
#: it. The schema inlines these rather than naming them in ``$defs``, so the
#: governed names come from the specification's record listing.
INLINE_OBJECT_NAMES: dict[str, str] = {
    "result.failure": "HostFailure",
    "moduleContributionDeclaration.contributions[]": "Contribution",
    "releasePackageIdentity.targetEntries[]": "TargetEntry",
    "developmentProfile.sourceMounts[]": "SourceMount",
    "shellScenario.assertions[]": "ScenarioAssertion",
}

#: Property JSON pointer -> the vocabulary constant prefix for its inline enum.
#: Declared for the same reason as the record names: ``result.outcome`` is
#: ``OUTCOME_*`` and ``denial.category`` is ``DENIAL_CATEGORY_*``, and a rule
#: derived from the property path alone would collide or read badly.
INLINE_ENUM_NAMES: dict[str, str] = {
    "availability.state": "AVAILABILITY_STATE",
    "remediation.kind": "REMEDIATION_KIND",
    "denial.category": "DENIAL_CATEGORY",
    "result.outcome": "OUTCOME",
    "compatibilityResult.status": "COMPATIBILITY_STATUS",
    "moduleContributionDeclaration.contributions[].kind": "CONTRIBUTION_KIND",
    "promotionRecord.result": "PROMOTION_RESULT",
    "developmentProfile.target": "DEVELOPMENT_TARGET",
    "testDriverRequest.operation": "TEST_DRIVER_OPERATION",
    "testDriverResult.outcome": "TEST_DRIVER_OUTCOME",
}

_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")
_LOCAL_REF_RE = re.compile(r"^#/\$defs/(?P<name>\w+)$")


def to_snake(name: str) -> str:
    """Convert a lower-camel wire spelling to the snake_case Python attribute name."""
    return _CAMEL_BOUNDARY_RE.sub("_", name).lower()


def export_sort_key(name: str) -> tuple[int, str]:
    """Order an ``__all__`` entry the way the rest of this repository orders one.

    Constants first, then classes, then functions, each ASCII-sorted. That is
    what ``ruff``'s ``RUF022`` expects and what the sibling Application
    Contract v1 package already publishes, so a generated surface and a
    hand-written one read the same way.
    """
    if name.upper() == name:
        return (0, name)
    if name[:1].isupper():
        return (1, name)
    return (2, name)


@dataclass(frozen=True)
class TypeRef:
    """A resolved property type and every constraint the schema states about it.

    ``kind`` is one of ``definition``, ``enum``, ``string``, ``integer``,
    ``boolean``, ``array`` or ``json_object``. ``alias`` names the ``$defs``
    entry a value came through, when one exists, so the emitted annotation
    reads as the governed alias rather than its expansion.

    The constraint fields carry the schema's own keywords through to the
    emitted ``validate()`` call unchanged. ``pattern_constant`` names the
    generated ``*_PATTERN`` constant rather than inlining the expression, so
    the enforced pattern and the published one cannot diverge.
    """

    kind: str
    definition: str = ""
    item: TypeRef | None = None
    enum_prefix: str = ""
    enum_values: tuple[str, ...] = ()
    alias: str = ""
    pattern_constant: str = ""
    min_length: int | None = None
    max_length: int | None = None
    date_time: bool = False
    minimum: int | None = None
    maximum: int | None = None
    min_items: int | None = None
    unique_items: bool = False

    @property
    def check_arguments(self) -> str:
        """Return the keyword arguments the emitted ``_check_*`` call needs."""
        parts: list[str] = []
        if self.pattern_constant:
            parts.append(f"pattern={self.pattern_constant}")
        if self.min_length is not None:
            parts.append(f"min_length={self.min_length}")
        if self.max_length is not None:
            parts.append(f"max_length={self.max_length}")
        if self.date_time:
            parts.append("date_time=True")
        if self.minimum is not None:
            parts.append(f"minimum={self.minimum}")
        if self.maximum is not None:
            parts.append(f"maximum={self.maximum}")
        return "".join(f", {part}" for part in parts)

    @property
    def sequence_arguments(self) -> str:
        """Return the keyword arguments the emitted ``_check_sequence`` call needs."""
        parts: list[str] = []
        if self.min_items is not None:
            parts.append(f"min_items={self.min_items}")
        if self.unique_items:
            parts.append("unique=True")
        return "".join(f", {part}" for part in parts)

    @property
    def annotation(self) -> str:
        if self.alias:
            return self.alias
        if self.kind == "definition":
            return self.definition
        if self.kind in {"enum", "string"}:
            return "str"
        if self.kind == "integer":
            return "int"
        if self.kind == "boolean":
            return "bool"
        if self.kind == "json_object":
            return "JsonObject"
        if self.kind == "array":
            assert self.item is not None
            return f"tuple[{self.item.annotation}, ...]"
        raise UnsupportedSchemaError(f"no annotation for type kind {self.kind!r}")


@dataclass(frozen=True)
class Property:
    """One property of a generated record."""

    wire_name: str
    attribute: str
    type_ref: TypeRef
    required: bool
    docs: str = ""

    @property
    def annotation(self) -> str:
        if self.required:
            return self.type_ref.annotation
        return f"{self.type_ref.annotation} | None"


@dataclass(frozen=True)
class BranchRule:
    """One ``allOf`` conditional the schema states about a whole record.

    ``discriminator`` is the property the ``if`` tests, ``value`` the constant
    it must equal, and ``required``/``forbidden`` the optional properties the
    ``then`` requires and refuses. This is how the total result envelope stops
    being a hand-written rule that could drift from the approved schema.
    """

    discriminator: str
    value: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    """A generated frozen dataclass."""

    name: str
    pointer: str
    properties: tuple[Property, ...]
    closed: bool
    docs: str
    branch_rules: tuple[BranchRule, ...] = ()


@dataclass(frozen=True)
class Alias:
    """A generated scalar or array type alias."""

    name: str
    annotation: str
    pattern: str | None
    docs: str
    #: What a value of this alias actually decodes as. An alias is a
    #: ``TypeAlias`` declaration, not a runtime class, so a property that
    #: ``$ref``s one decodes through this rather than a ``from_wire`` call.
    resolved: TypeRef = field(default_factory=lambda: TypeRef(kind="string"))


@dataclass(frozen=True)
class Vocabulary:
    """A generated closed enum: a value tuple plus one named constant per member."""

    prefix: str
    plural: str
    values: tuple[str, ...]
    docs: str


@dataclass
class Model:
    """Everything the emitter needs, resolved from the schema."""

    aliases: list[Alias] = field(default_factory=list)
    vocabularies: list[Vocabulary] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    branch_names: list[str] = field(default_factory=list)
    alias_index: dict[str, Alias] = field(default_factory=dict)

    def add_alias(self, alias: Alias) -> None:
        self.aliases.append(alias)
        self.alias_index[alias.name] = alias


# --------------------------------------------------------------------------
# Schema -> model
# --------------------------------------------------------------------------


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise UnsupportedSchemaError(f"{path.name}: expected a JSON object at the document root")
    if document.get("$schema") != JSON_SCHEMA_DIALECT:
        raise UnsupportedSchemaError(f"{path.name}: unexpected JSON Schema dialect")
    if document.get("$id") != SCHEMA_ID:
        raise UnsupportedSchemaError(f"{path.name}: unexpected $id {document.get('$id')!r}")
    return document


def _plural_constant(prefix: str) -> str:
    """Return the constant name holding every member of a vocabulary."""
    if prefix.endswith("Y"):
        return f"{prefix[:-1]}IES"
    if prefix.endswith(("S", "X", "Z", "CH", "SH")):
        return f"{prefix}ES"
    return f"{prefix}S"


def _resolve_ref(ref: str) -> str:
    match = _LOCAL_REF_RE.match(ref)
    if match is None:
        raise UnsupportedSchemaError(f"unsupported $ref {ref!r}")
    key = match.group("name")
    if key not in DEFINITION_NAMES:
        raise UnsupportedSchemaError(f"$ref to undeclared definition {key!r}")
    return DEFINITION_NAMES[key]


def _enum_values(schema: dict[str, Any], pointer: str) -> tuple[str, ...]:
    values = schema["enum"]
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise UnsupportedSchemaError(f"{pointer}: only string enums are supported")
    if len(set(values)) != len(values):
        raise UnsupportedSchemaError(f"{pointer}: enum repeats a value")
    return tuple(values)


#: The keywords this generator understands, per resolved kind. Anything else in
#: a property schema is a constraint that would be emitted as documentation and
#: enforced nowhere, so it raises instead.
_SUPPORTED_KEYWORDS: dict[str, frozenset[str]] = {
    "root": frozenset({"$schema", "$id", "title", "type", "$defs", "oneOf"}),
    "$ref": frozenset({"$ref"}),
    "enum": frozenset({"enum"}),
    "string": frozenset({"type", "pattern", "minLength", "maxLength", "format"}),
    "integer": frozenset({"type", "minimum", "maximum"}),
    "boolean": frozenset({"type"}),
    "array": frozenset({"type", "items", "uniqueItems", "minItems"}),
    "record": frozenset({"type", "properties", "required", "additionalProperties", "allOf"}),
    "json_object": frozenset({"type"}),
}

#: The string ``format`` values this generator enforces. An unlisted format
#: would otherwise be accepted and never checked.
_SUPPORTED_FORMATS: frozenset[str] = frozenset({"date-time"})


def _require_supported_keywords(schema: dict[str, Any], kind: str, pointer: str) -> None:
    unsupported = sorted(set(schema) - _SUPPORTED_KEYWORDS[kind])
    if unsupported:
        raise UnsupportedSchemaError(
            f"{pointer}: unsupported schema keyword(s) {unsupported} for a {kind}"
        )


def _bounded_int(schema: dict[str, Any], keyword: str, pointer: str) -> int | None:
    value = schema.get(keyword)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedSchemaError(f"{pointer}: {keyword} must be an integer")
    if keyword in {"minLength", "maxLength", "minItems"} and value < 0:
        raise UnsupportedSchemaError(f"{pointer}: {keyword} must be non-negative")
    return value


def _resolve_property_type(schema: dict[str, Any], pointer: str, model: Model) -> TypeRef:
    if "$ref" in schema:
        _require_supported_keywords(schema, "$ref", pointer)
        name = _resolve_ref(schema["$ref"])
        alias = model.alias_index.get(name)
        if alias is None:
            # A record reference: decoded by its own `from_wire`.
            return TypeRef(kind="definition", definition=name)
        if alias.resolved.kind == "array":
            return dataclasses.replace(alias.resolved, alias=name)
        return TypeRef(kind="definition", definition=name)
    if "enum" in schema:
        _require_supported_keywords(schema, "enum", pointer)
        prefix = INLINE_ENUM_NAMES.get(pointer)
        if prefix is None:
            raise UnsupportedSchemaError(f"{pointer}: inline enum has no declared constant prefix")
        values = _enum_values(schema, pointer)
        model.vocabularies.append(
            Vocabulary(
                prefix=prefix,
                plural=_plural_constant(prefix),
                values=values,
                docs=f"Closed v1 vocabulary for `{pointer}`. An unknown value fails closed.",
            )
        )
        return TypeRef(kind="enum", enum_prefix=prefix, enum_values=values)
    declared = schema.get("type")
    if declared == "string":
        _require_supported_keywords(schema, "string", pointer)
        pattern = schema.get("pattern")
        if pattern is not None and type(pattern) is not str:
            raise UnsupportedSchemaError(f"{pointer}: pattern must be a string")
        declared_format = schema.get("format")
        if declared_format is not None and declared_format not in _SUPPORTED_FORMATS:
            raise UnsupportedSchemaError(f"{pointer}: unsupported string format {declared_format!r}")
        return TypeRef(
            kind="string",
            pattern_constant="",
            min_length=_bounded_int(schema, "minLength", pointer),
            max_length=_bounded_int(schema, "maxLength", pointer),
            date_time=declared_format == "date-time",
        )
    if declared == "integer":
        _require_supported_keywords(schema, "integer", pointer)
        return TypeRef(
            kind="integer",
            minimum=_bounded_int(schema, "minimum", pointer),
            maximum=_bounded_int(schema, "maximum", pointer),
        )
    if declared == "boolean":
        _require_supported_keywords(schema, "boolean", pointer)
        return TypeRef(kind="boolean")
    if declared == "array":
        _require_supported_keywords(schema, "array", pointer)
        if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
            raise UnsupportedSchemaError(f"{pointer}: uniqueItems must be a boolean")
        items = schema.get("items")
        if not isinstance(items, dict):
            raise UnsupportedSchemaError(f"{pointer}: array without an object `items` schema")
        return TypeRef(
            kind="array",
            item=_resolve_property_type(items, f"{pointer}[]", model),
            min_items=_bounded_int(schema, "minItems", pointer),
            unique_items=schema.get("uniqueItems") is True,
        )
    if declared == "object":
        if "properties" in schema:
            inline_name = INLINE_OBJECT_NAMES.get(pointer)
            if inline_name is None:
                raise UnsupportedSchemaError(f"{pointer}: inline object has no declared record name")
            _build_record(inline_name, pointer, schema, model)
            return TypeRef(kind="definition", definition=inline_name)
        if "additionalProperties" in schema:
            raise UnsupportedSchemaError(f"{pointer}: constrained open maps are not supported")
        _require_supported_keywords(schema, "json_object", pointer)
        return TypeRef(kind="json_object")
    raise UnsupportedSchemaError(f"{pointer}: unsupported property schema")


def _build_branch_rules(schema: dict[str, Any], pointer: str) -> tuple[BranchRule, ...]:
    """Resolve a record's ``allOf`` conditionals into branch rules.

    Exactly the one shape Host Contract v1 uses is understood -- ``if`` on a
    single property ``const``, ``then`` naming ``required`` plus a ``not`` over
    an ``anyOf`` of single-property ``required`` clauses. Anything else raises
    rather than being emitted as a record with no enforced invariant.
    """
    conditionals = schema.get("allOf")
    if conditionals is None:
        return ()
    if not isinstance(conditionals, list) or not conditionals:
        raise UnsupportedSchemaError(f"{pointer}: allOf must be a non-empty list")
    rules: list[BranchRule] = []
    for index, conditional in enumerate(conditionals):
        where = f"{pointer}.allOf[{index}]"
        if not isinstance(conditional, dict) or set(conditional) != {"if", "then"}:
            raise UnsupportedSchemaError(f"{where}: only if/then conditionals are supported")
        condition = conditional["if"]
        if not isinstance(condition, dict) or set(condition) != {"properties"}:
            raise UnsupportedSchemaError(f"{where}: `if` must test properties only")
        tested = condition["properties"]
        if not isinstance(tested, dict) or len(tested) != 1:
            raise UnsupportedSchemaError(f"{where}: `if` must test exactly one property")
        ((discriminator, constraint),) = tested.items()
        if (
            not isinstance(constraint, dict)
            or set(constraint) != {"const"}
            or type(constraint["const"]) is not str
        ):
            raise UnsupportedSchemaError(f"{where}: `if` must test a single `const`")
        consequence = conditional["then"]
        if not isinstance(consequence, dict) or not set(consequence) <= {"required", "not"}:
            raise UnsupportedSchemaError(f"{where}: `then` supports `required` and `not` only")
        required_names = consequence.get("required", [])
        if not isinstance(required_names, list) or not all(
            isinstance(item, str) for item in required_names
        ):
            raise UnsupportedSchemaError(f"{where}: `then.required` must be a list of names")
        forbidden: list[str] = []
        refusal = consequence.get("not")
        if refusal is not None:
            if not isinstance(refusal, dict) or set(refusal) != {"anyOf"}:
                raise UnsupportedSchemaError(f"{where}: `then.not` supports `anyOf` only")
            clauses = refusal["anyOf"]
            if not isinstance(clauses, list):
                raise UnsupportedSchemaError(f"{where}: `then.not.anyOf` must be a list")
            for clause in clauses:
                if not isinstance(clause, dict) or set(clause) != {"required"}:
                    raise UnsupportedSchemaError(f"{where}: `anyOf` clauses must name `required`")
                names = clause["required"]
                if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
                    raise UnsupportedSchemaError(f"{where}: `anyOf` clauses name one property")
                forbidden.append(names[0])
        rules.append(
            BranchRule(
                discriminator=discriminator,
                value=str(constraint["const"]),
                required=tuple(required_names),
                forbidden=tuple(forbidden),
            )
        )
    discriminators = {rule.discriminator for rule in rules}
    if len(discriminators) != 1:
        raise UnsupportedSchemaError(f"{pointer}: allOf must test one discriminator, got {sorted(discriminators)}")
    return tuple(rules)


def _build_record(name: str, pointer: str, schema: dict[str, Any], model: Model) -> Record:
    _require_supported_keywords(schema, "record", pointer)
    required_names = schema.get("required", [])
    if not isinstance(required_names, list) or not all(
        type(item) is str for item in required_names
    ) or len(set(required_names)) != len(required_names):
        raise UnsupportedSchemaError(f"{pointer}: required must be a unique list of names")
    additional = schema.get("additionalProperties")
    if additional is not None and type(additional) is not bool:
        raise UnsupportedSchemaError(f"{pointer}: additionalProperties must be a boolean")
    required = set(required_names)
    properties: list[Property] = []
    declared = schema.get("properties")
    if not isinstance(declared, dict):
        raise UnsupportedSchemaError(f"{pointer}: object record without `properties`")
    unknown_required = required - set(declared)
    if unknown_required:
        raise UnsupportedSchemaError(f"{pointer}: required names no property declares: {sorted(unknown_required)}")
    for wire_name, property_schema in declared.items():
        if not isinstance(property_schema, dict):
            raise UnsupportedSchemaError(f"{pointer}.{wire_name}: property is not a schema object")
        type_ref = _resolve_property_type(property_schema, f"{pointer}.{wire_name}", model)
        properties.append(
            Property(
                wire_name=wire_name,
                attribute=to_snake(wire_name),
                type_ref=type_ref,
                required=wire_name in required,
            )
        )
    # Required fields first, then optional ones, so the dataclass signature is
    # valid: a defaulted field can never precede a non-defaulted one.
    ordered = tuple(
        sorted(properties, key=lambda item: (not item.required, properties.index(item)))
    )
    branch_rules = _build_branch_rules(schema, pointer)
    declared_names = {prop.wire_name for prop in properties}
    for rule in branch_rules:
        unknown = sorted({rule.discriminator, *rule.required, *rule.forbidden} - declared_names)
        if unknown:
            raise UnsupportedSchemaError(f"{pointer}: allOf names undeclared propert(ies) {unknown}")
    record = Record(
        name=name,
        pointer=pointer,
        properties=ordered,
        closed=schema.get("additionalProperties") is False,
        docs=RECORD_DOCS.get(name, f"Host Contract v1 `{pointer}` record."),
        branch_rules=branch_rules,
    )
    model.records.append(record)
    return record


#: One governed sentence per record, transcribed from the approved
#: specification. Documentation only; nothing here changes wire behaviour.
RECORD_DOCS: dict[str, str] = {
    "CapabilityAvailability": (
        "Whether a capability can be used on a target, and why not when it cannot. "
        "An unknown state fails closed and is never treated as available."
    ),
    "Remediation": "The safe next step a denial or availability answer offers the user.",
    "CapabilityDenial": (
        "A refusal record whose closed field set constrains shape but does not prove display "
        "content safety. A trusted host validator or redactor must admit `safeReason`; otherwise "
        "the public display boundary returns a fixed generic correlation-bound refusal."
    ),
    "HostFailure": "A safe description of an operation that failed for neither denial nor availability reasons.",
    "HostResult": (
        "The total result envelope. Exactly one of `data`, `denial`, `availability` or "
        "`failure` matches `outcome`; a degraded success additionally carries "
        "`availability` alongside `data`."
    ),
    "HostRequest": (
        "One cross-host operation attempt. `contextId` is a lookup into the trusted "
        "host's ShellContext store: `payload` carries operation input only and never "
        "establishes principal, Workspace, permission, entitlement or environment authority."
    ),
    "ShellContext": (
        "The immutable trusted-host authority record. A Workspace, principal, "
        "entitlement, permission, runtime or environment change creates a new context "
        "ID rather than mutating this one."
    ),
    "CompatibilityResult": (
        "The structured outcome of evaluating a declaration against a target. A missing "
        "required capability can never yield `compatible`."
    ),
    "Contribution": "One registry entry a Module release contributes to the shell.",
    "ModuleContributionDeclaration": (
        "Everything one Module release contributes. Registration is all-or-nothing: "
        "partial activation of a declaration is prohibited."
    ),
    "TargetEntry": "The entry point a Release Package exposes for one target.",
    "ReleasePackageIdentity": (
        "The immutable identity of a Release Package. It carries digests and publisher "
        "identity only: there is no field for an environment value or a secret."
    ),
    "PromotionRecord": (
        "Evidence that promotion copied the same verified immutable Release Package. "
        "Promotion never rebuilds a package or injects environment data."
    ),
    "RollbackRecord": "Evidence that rollback selected a previously verified Release, mutating neither.",
    "EnvironmentBinding": (
        "Workspace-scoped, separately versioned references that bind a Release to an "
        "environment. References only: never a raw credential, token, private key or "
        "provider secret."
    ),
    "SourceMount": "One development source mount. Compiled out of production customer builds.",
    "DevelopmentProfile": (
        "A local development configuration. Its target is restricted to `desktop` or "
        "`conformance`: a production Cloud target is not expressible."
    ),
    "ScenarioAssertion": "One deterministic assertion a shell scenario makes.",
    "ShellScenario": "A deterministic fixture-driven scenario for conformance runs.",
    "TestDriverRequest": "One conformance test-driver instruction. Absent from production customer builds.",
    "TestDriverResult": "The outcome of one conformance test-driver instruction.",
}


def build_model(document: dict[str, Any]) -> Model:
    """Resolve the canonical schema into the emitter's intermediate representation."""
    _require_supported_keywords(document, "root", "schema root")
    if document.get("type") != "object":
        raise UnsupportedSchemaError("schema root: type must be object")
    if type(document.get("title")) is not str:
        raise UnsupportedSchemaError("schema root: title must be a string")
    defs = document.get("$defs")
    if not isinstance(defs, dict):
        raise UnsupportedSchemaError("schema has no $defs table")
    if set(defs) != set(DEFINITION_NAMES):
        missing = sorted(set(defs) - set(DEFINITION_NAMES))
        extra = sorted(set(DEFINITION_NAMES) - set(defs))
        raise UnsupportedSchemaError(f"$defs disagrees with the declared names (missing={missing}, extra={extra})")

    model = Model()

    # Pass 1 -- scalar and closed-vocabulary aliases. These have no dependency
    # on anything else, so they resolve first regardless of $defs order.
    for key, schema in defs.items():
        if not isinstance(schema, dict):
            raise UnsupportedSchemaError(f"$defs.{key}: definition is not a schema object")
        name = DEFINITION_NAMES[key]
        if "enum" in schema:
            _require_supported_keywords(schema, "enum", f"$defs.{key}")
            values = _enum_values(schema, key)
            prefix = to_snake(name).upper()
            plural = _plural_constant(prefix)
            model.vocabularies.append(
                Vocabulary(
                    prefix=prefix,
                    plural=plural,
                    values=values,
                    docs=f"The fixed v1 `{key}` vocabulary. An unknown value fails closed.",
                )
            )
            model.add_alias(
                Alias(
                    name=name,
                    annotation="str",
                    pattern=None,
                    docs=f"A `{key}` value. Closed: see {plural}.",
                    resolved=TypeRef(kind="enum", enum_prefix=prefix, enum_values=values),
                )
            )
            continue
        if schema.get("type") == "string":
            scalar = _resolve_property_type(schema, f"$defs.{key}", model)
            pattern = schema.get("pattern")
            model.add_alias(
                Alias(
                    name=name,
                    annotation="str",
                    pattern=pattern,
                    docs=f"A `{key}` value.",
                    resolved=dataclasses.replace(
                        scalar,
                        pattern_constant=f"{to_snake(name).upper()}_PATTERN" if pattern else "",
                    ),
                )
            )

    # Pass 2 -- array aliases, which reference the scalar aliases above.
    for key, schema in defs.items():
        assert isinstance(schema, dict)
        if schema.get("type") != "array":
            continue
        array_ref = _resolve_property_type(schema, f"$defs.{key}", model)
        assert array_ref.item is not None
        name = DEFINITION_NAMES[key]
        model.add_alias(
            Alias(
                name=name,
                annotation=f"tuple[{array_ref.item.annotation}, ...]",
                pattern=None,
                docs=f"A `{key}` value. Present-and-empty is not the same as absent.",
                resolved=array_ref,
            )
        )

    # Pass 3 -- records, which reference both alias groups and each other.
    for key, schema in defs.items():
        assert isinstance(schema, dict)
        if schema.get("type") != "object":
            if "enum" in schema or schema.get("type") in {"string", "array"}:
                continue
            raise UnsupportedSchemaError(f"$defs.{key}: unsupported definition shape")
        if DEFINITION_NAMES[key] in {record.name for record in model.records}:
            continue
        _build_record(DEFINITION_NAMES[key], key, schema, model)

    branches = document.get("oneOf")
    if not isinstance(branches, list) or not branches:
        raise UnsupportedSchemaError("schema root has no oneOf branch list")
    for branch in branches:
        if not isinstance(branch, dict) or set(branch) != {"$ref"}:
            raise UnsupportedSchemaError("schema root oneOf accepts $ref branches only")
        model.branch_names.append(_resolve_ref(branch["$ref"]))
    return model


# --------------------------------------------------------------------------
# Model -> Python
# --------------------------------------------------------------------------

_PRELUDE = '''# GENERATED FILE - DO NOT EDIT.
#
# Source of truth:
#   contracts/host/v1/schemas/host-contract-v1.schema.json
# Governed by:
#   DOC-004 section AA; approval GOV-HOST-CONTRACT-APPROVAL-001;
#   proposal commit {commit};
#   proposal content-set SHA-256 {content_set}.
# Generator:
#   scripts/generate-host-contract.py
#
# Regenerate: python scripts/generate-host-contract.py
# Verify:     python scripts/generate-host-contract.py --check
#
# Frozen dataclasses, type aliases, and closed vocabulary for the OmniVia Host
# Contract v1. Standard library only: this module must never depend on runtime,
# storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework.

"""Generated Host Contract v1 types (DOC-004 section AA).

Every record enforces the canonical schema, not a structural subset of it.
``validate()`` re-implements the governed patterns, string lengths, integer
bounds, RFC 3339 timestamps, array cardinality and uniqueness, closed
vocabularies, closed field sets and record-level branch invariants;
``from_wire`` decodes and then validates, and ``to_wire`` validates before it
emits. There is therefore no public way into or out of a record that skips the
governed constraints, and a directly constructed record with hostile values is
refused at the moment it would become a wire document -- a type annotation is
documentation, not a runtime guarantee.

No diagnostic raised here reproduces a rejected value. A refused document may
be carrying exactly the credential, token or private key the contract forbids,
so a message names the field path and the rule it broke and nothing else.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, ClassVar, Final, TypeAlias, cast

'''

_HELPERS = '''

class HostContractDecodeError(Exception):
    """Raised when a payload is not a valid Host Contract v1 value.

    Carries the payload's ``correlationId`` when the document had a usable one,
    so a caller can display a generic safe denial keyed by the correlation ID
    without reading anything else out of the rejected document.
    """

    def __init__(self, message: str, correlation_id: str | None = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id


def _require_mapping(payload: object, path: str) -> Mapping[str, Any]:
    if type(payload) not in (dict, MappingProxyType):
        raise HostContractDecodeError(f"{path}: expected an object")
    mapping = cast(Mapping[str, Any], payload)
    for key in mapping:
        if type(key) is not str:
            raise HostContractDecodeError(f"{path}: object keys must be strings")
    return mapping


def _require_field(mapping: Mapping[str, Any], name: str, path: str) -> Any:
    if name not in mapping:
        raise HostContractDecodeError(f"{path}: missing required field {name!r}")
    return mapping[name]


#: A field name safe to name back to the caller. A rejected document controls
#: its own key spellings, so a key that is not a plain identifier is reported
#: as a placeholder rather than echoed.
_SAFE_FIELD_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _safe_field_name(name: str) -> str:
    return name if _SAFE_FIELD_NAME_RE.fullmatch(name) else "<unprintable field name>"


def _reject_unknown_fields(mapping: Mapping[str, Any], known: frozenset[str], path: str) -> None:
    """Reject any field the closed record does not declare.

    This is the structural half of the governed redaction and secret-exclusion
    invariants: a closed record cannot carry an inline secret, an environment
    value or an unapproved policy detail, because it cannot carry any field the
    approved schema does not name.
    """
    unknown = sorted(_safe_field_name(name) for name in set(mapping) - known)
    if unknown:
        raise HostContractDecodeError(f"{path}: unknown field(s) {unknown} in a closed record")


def _decode_str(value: object, path: str) -> str:
    if type(value) is not str:
        raise HostContractDecodeError(f"{path}: expected a string")
    return value


def _decode_enum(value: object, allowed: tuple[str, ...], path: str) -> str:
    """Decode a closed vocabulary member, failing closed on anything else.

    An unknown value is never downgraded to a usable one and is never treated
    as available: it raises, and the refused value is not echoed back.
    """
    text = _decode_str(value, path)
    if text not in allowed:
        raise HostContractDecodeError(f"{path}: unsupported_contract_value")
    return text


def _decode_int(value: object, path: str) -> int:
    if type(value) is not int:
        raise HostContractDecodeError(f"{path}: expected an integer")
    return value


def _decode_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise HostContractDecodeError(f"{path}: expected a boolean")
    return value


def _decode_sequence(value: object, path: str) -> Sequence[Any]:
    if type(value) not in (list, tuple):
        raise HostContractDecodeError(f"{path}: expected an array")
    return cast(Sequence[Any], value)


def _unique_key(value: Any) -> Any:
    """Return a hashable key that compares two JSON values structurally.

    ``uniqueItems`` compares whole JSON values, so two equal objects repeat
    even when they are distinct Python objects. Booleans are keyed apart from
    the integers they equal, because JSON `true` is not JSON `1`.
    """
    if type(value) in (dict, MappingProxyType):
        return ("object", tuple(sorted((key, _unique_key(item)) for key, item in value.items())))
    if type(value) in (str, bytes, bytearray):
        return ("scalar", value)
    if type(value) is bool:
        return ("bool", value)
    if type(value) in (list, tuple):
        return ("array", tuple(_unique_key(item) for item in value))
    return ("scalar", value)


def _require_unique(values: Sequence[Any], path: str) -> None:
    """Raise on a repeated item, comparing JSON values structurally.

    The diagnostic names the index and nothing else: a repeated value can be
    exactly the credential-shaped string the contract refuses to reproduce.
    """
    seen: set[Any] = set()
    for index, value in enumerate(values):
        try:
            key = _unique_key(value)
            repeated = key in seen
        except TypeError as error:
            raise HostContractDecodeError(f"{path}[{index}]: value is not JSON") from error
        if repeated:
            raise HostContractDecodeError(f"{path}[{index}]: duplicate value")
        seen.add(key)


def _freeze_json(value: object, path: str) -> Any:
    """Return a deeply immutable view of an opaque JSON value."""
    if type(value) in (dict, MappingProxyType):
        mapping = cast(Mapping[str, Any], value)
        for key in mapping:
            if type(key) is not str:
                raise HostContractDecodeError(f"{path}: object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{path}.{key}") for key, item in mapping.items()}
        )
    if type(value) in (str, bool, int) or value is None:
        return value
    if type(value) is float:
        return value
    if type(value) in (list, tuple):
        return tuple(
            _freeze_json(item, f"{path}[]") for item in cast(Sequence[Any], value)
        )
    raise HostContractDecodeError(f"{path}: value is not JSON")


def _decode_json_object(value: object, path: str) -> JsonObject:
    mapping = _require_mapping(value, path)
    frozen: JsonObject = _freeze_json(mapping, path)
    return frozen


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _encode_json_object(value: JsonObject) -> dict[str, Any]:
    thawed: Any = _thaw_json(value)
    return dict(thawed)


#: An opaque JSON object whose shape a namespace schema owns, not this contract.
JsonObject: TypeAlias = Mapping[str, Any]


#: RFC 3339 `date-time`. A time zone is mandatory: an authority record whose
#: validity window depends on the reader's clock is not a fixed instant. The
#: leap second RFC 3339 permits is accepted and validated against `:59`.
_DATE_TIME_RE: Final[re.Pattern[str]] = re.compile(
    r"(\\d{4})-(\\d{2})-(\\d{2})[Tt](\\d{2}):(\\d{2}):([0-5]\\d|60)(?:\\.\\d+)?"
    r"(?:[Zz]|[+-](?:[01]\\d|2[0-3]):[0-5]\\d)"
)


def _check_date_time(value: str, path: str) -> None:
    match = _DATE_TIME_RE.fullmatch(value)
    if match is None:
        raise HostContractDecodeError(f"{path}: expected an RFC 3339 date-time with a time zone")
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        datetime(year, month, day, hour, minute, min(second, 59), tzinfo=UTC)
    except ValueError as error:
        raise HostContractDecodeError(f"{path}: is not a real calendar instant") from error


def _check_str(
    value: object,
    path: str,
    *,
    pattern: str | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    date_time: bool = False,
) -> None:
    """Raise unless the value is a string meeting every governed constraint."""
    if type(value) is not str:
        raise HostContractDecodeError(f"{path}: expected a string")
    if min_length is not None and len(value) < min_length:
        raise HostContractDecodeError(f"{path}: expected at least {min_length} character(s)")
    if max_length is not None and len(value) > max_length:
        raise HostContractDecodeError(f"{path}: expected at most {max_length} character(s)")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise HostContractDecodeError(f"{path}: does not match the governed pattern")
    if date_time:
        _check_date_time(value, path)


def _check_enum(value: object, allowed: tuple[str, ...], path: str) -> None:
    if type(value) is not str or value not in allowed:
        raise HostContractDecodeError(f"{path}: unsupported_contract_value")


def _check_int(
    value: object, path: str, *, minimum: int | None = None, maximum: int | None = None
) -> None:
    if type(value) is not int:
        raise HostContractDecodeError(f"{path}: expected an integer")
    if minimum is not None and value < minimum:
        raise HostContractDecodeError(f"{path}: expected an integer of at least {minimum}")
    if maximum is not None and value > maximum:
        raise HostContractDecodeError(f"{path}: expected an integer of at most {maximum}")


def _check_bool(value: object, path: str) -> None:
    if type(value) is not bool:
        raise HostContractDecodeError(f"{path}: expected a boolean")


def _check_json_value(value: object, path: str) -> None:
    """Raise unless the value is representable as JSON, all the way down."""
    if type(value) in (dict, MappingProxyType):
        for key, item in cast(Mapping[str, Any], value).items():
            if type(key) is not str:
                raise HostContractDecodeError(f"{path}: object keys must be strings")
            _check_json_value(item, f"{path}.{_safe_field_name(key)}")
        return
    if value is None or type(value) in (bool, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise HostContractDecodeError(f"{path}: expected a finite JSON number")
        return
    if type(value) is int:
        return
    if type(value) in (bytes, bytearray):
        raise HostContractDecodeError(f"{path}: value is not JSON")
    if type(value) in (list, tuple):
        for index, item in enumerate(cast(Sequence[Any], value)):
            _check_json_value(item, f"{path}[{index}]")
        return
    raise HostContractDecodeError(f"{path}: value is not JSON")


def _check_json_object(value: object, path: str) -> None:
    if type(value) not in (dict, MappingProxyType):
        raise HostContractDecodeError(f"{path}: expected an object")
    _check_json_value(value, path)


def _check_record(value: object, expected: type[Any], path: str) -> None:
    """Raise unless the value is that record *and* is itself valid."""
    if type(value) is not expected:
        raise HostContractDecodeError(f"{path}: expected a {expected.__name__}")
    cast(Any, value).validate(path)


def _check_sequence(
    value: object, path: str, *, min_items: int | None = None, unique: bool = False
) -> Sequence[Any]:
    if type(value) not in (list, tuple):
        raise HostContractDecodeError(f"{path}: expected an array")
    sequence = cast(Sequence[Any], value)
    if min_items is not None and len(sequence) < min_items:
        raise HostContractDecodeError(f"{path}: expected at least {min_items} item(s)")
    if unique:
        _require_unique(sequence, path)
    return sequence


def _check_branches(
    discriminator: object,
    branches: Mapping[str, Any],
    rules: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]],
    path: str,
) -> None:
    """Raise unless the present optional branches match the discriminator's rule.

    Transcribed from the canonical schema's `allOf`, so the envelope is total
    by construction: there is no discriminator value without a rule, and no
    rule that tolerates a branch belonging to a different one.
    """
    if not isinstance(discriminator, str) or discriminator not in rules:
        raise HostContractDecodeError(f"{path}: unsupported_contract_value")
    required, forbidden = rules[discriminator]
    present = tuple(name for name, value in branches.items() if value is not None)
    if [name for name in required if name not in present] or [
        name for name in forbidden if name in present
    ]:
        raise HostContractDecodeError(
            f"{path}: this outcome requires exactly one branch ({list(required)}); "
            f"got {list(present)}"
        )
'''


def _wrap_docstring(text: str, indent: str) -> list[str]:
    body = textwrap.wrap(text, width=MAX_LINE_LENGTH - len(indent) - 2)
    if len(body) == 1 and len(f'{indent}"""{body[0]}"""') <= MAX_LINE_LENGTH:
        return [f'{indent}"""{body[0]}"""']
    lines = [f'{indent}"""{body[0]}']
    lines.extend(f"{indent}{line}" for line in body[1:])
    lines.append(f'{indent}"""')
    return lines


def _constant_name(prefix: str, value: str) -> str:
    return f"{prefix}_{value.upper()}"


def _decoder_expression(type_ref: TypeRef, source: str, path: str) -> str:
    if type_ref.kind == "definition":
        return f"{type_ref.definition}.from_wire({source}, {path})"
    if type_ref.kind == "enum":
        return f"_decode_enum({source}, {_plural_constant(type_ref.enum_prefix)}, {path})"
    if type_ref.kind == "string":
        return f"_decode_str({source}, {path})"
    if type_ref.kind == "integer":
        return f"_decode_int({source}, {path})"
    if type_ref.kind == "boolean":
        return f"_decode_bool({source}, {path})"
    if type_ref.kind == "json_object":
        return f"_decode_json_object({source}, {path})"
    raise UnsupportedSchemaError(f"no decoder for type kind {type_ref.kind!r}")


def _render_record(record: Record, model: Model) -> str:
    aliases = {alias.name: alias for alias in model.aliases}
    lines = ["@dataclass(frozen=True, slots=True)", f"class {record.name}:"]
    lines.extend(_wrap_docstring(record.docs, "    "))
    lines.append("")
    for prop in record.properties:
        suffix = "" if prop.required else " = None"
        lines.append(f"    {prop.attribute}: {prop.annotation}{suffix}")
    lines.append("")
    # ClassVar, not Final: a `slots=True` dataclass turns every other annotated
    # class attribute into a slot descriptor, which would shadow the value.
    known = ", ".join(f'"{prop.wire_name}"' for prop in record.properties)
    lines.append("    #: Every canonical wire field spelling this record declares.")
    lines.append(f"    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({{{known}}})")
    lines.append("    #: Whether the canonical schema refuses fields it does not declare.")
    lines.append(f"    CLOSED: ClassVar[bool] = {record.closed}")
    lines.append("")

    # --- validate ---
    lines.append(f'    def validate(self, path: str = "{record.name}") -> None:')
    lines.extend(
        _wrap_docstring(
            "Raise unless every field satisfies the canonical schema. Runtime values are "
            "checked, not annotations: a record built by calling this constructor with "
            "hostile values is refused here exactly as a hostile wire document is. No "
            "diagnostic reproduces a rejected value.",
            "        ",
        )
    )
    for prop in record.properties:
        lines.extend(_render_property_validate(prop, aliases))
    if record.branch_rules:
        lines.append("        self.validate_branches(path)")
    if not record.properties and not record.branch_rules:  # pragma: no cover - no such record
        lines.append("        return None")
    lines.append("")

    # --- validate_branches ---
    if record.branch_rules:
        discriminator = record.branch_rules[0].discriminator
        branch_names: list[str] = []
        for rule in record.branch_rules:
            for name in (*rule.required, *rule.forbidden):
                if name not in branch_names:
                    branch_names.append(name)
        attributes = {prop.wire_name: prop.attribute for prop in record.properties}
        lines.append(f'    def validate_branches(self, path: str = "{record.name}") -> None:')
        lines.extend(
            _wrap_docstring(
                f"Raise unless exactly the branch `{discriminator}` names is present. "
                "Transcribed from the canonical schema's `allOf`, so the envelope stays "
                "total: an outcome without its branch, or carrying one that belongs to a "
                "different outcome, is a protocol violation rather than something to "
                "resolve by preferring one side.",
                "        ",
            )
        )
        lines.append("        _check_branches(")
        lines.append(f"            self.{attributes[discriminator]},")
        lines.append("            {")
        for name in branch_names:
            lines.append(f'                "{name}": self.{attributes[name]},')
        lines.append("            },")
        lines.append("            {")
        for rule in record.branch_rules:
            lines.append(
                f'                "{rule.value}": ('
                f"{_tuple_literal(rule.required)}, {_tuple_literal(rule.forbidden)}),"
            )
        lines.append("            },")
        lines.append("            path,")
        lines.append("        )")
        lines.append("")

    # --- to_wire ---
    lines.append("    def to_wire(self) -> dict[str, Any]:")
    lines.extend(
        _wrap_docstring(
            "Validate this value and render it as a JSON-compatible mapping using the "
            "canonical field spelling. An absent optional field is omitted rather than "
            "emitted as null, so a decode/encode round trip reproduces "
            + (
                "the original document exactly."
                if record.closed
                else (
                    "every field this record declares exactly. It reproduces nothing "
                    "else: this record is open, so a decode kept only the declared "
                    "fields and there is no unknown remainder left to re-emit."
                )
            )
            + " Emission is validated so a record that was built rather "
            "than decoded cannot become an invalid wire document.",
            "        ",
        )
    )
    lines.append("        self.validate()")
    lines.append("        wire: dict[str, Any] = {}")
    for prop in record.properties:
        indent = "        "
        source = f"self.{prop.attribute}"
        if not prop.required:
            lines.append(f"        if {source} is not None:")
            indent = "            "
        lines.append(f"{indent}{_wire_expression(prop.type_ref, source, prop.wire_name, aliases)}")
    lines.append("        return wire")
    lines.append("")

    # --- from_wire ---
    lines.append(
        f"    @classmethod\n"
        f"    def from_wire(cls, payload: object, path: str = \"{record.name}\") -> {record.name}:"
    )
    doc = (
        "Decode a wire payload into a "
        + record.name
        + ". "
        + (
            "Unknown fields are refused: this record is closed by the canonical schema."
            if record.closed
            else "Unknown fields are ignored, so a newer additive minor release still decodes here."
        )
        + " The decoded value is then validated against every governed constraint, so this "
        "is the strict admission path rather than a structural one: a missing required "
        "field, a wrongly typed value, an unknown closed-vocabulary member, a pattern, "
        "length, range, timestamp, cardinality or uniqueness violation all raise "
        "HostContractDecodeError."
    )
    lines.extend(_wrap_docstring(doc, "        "))
    lines.append("        mapping = _require_mapping(payload, path)")
    if record.closed:
        lines.append("        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)")
    for prop in record.properties:
        lines.extend(_render_property_decode(prop, aliases))
    lines.append("        decoded = cls(")
    for prop in record.properties:
        lines.append(f"            {prop.attribute}=field_{prop.attribute},")
    lines.append("        )")
    lines.append("        decoded.validate(path)")
    lines.append("        return decoded")
    return "\n".join(lines)


def _tuple_literal(names: tuple[str, ...]) -> str:
    if len(names) == 1:
        return f'("{names[0]}",)'
    return "(" + ", ".join(f'"{name}"' for name in names) + ")"


def _check_expression(type_ref: TypeRef, source: str, path: str, aliases: dict[str, Alias]) -> str:
    """Return the ``_check_*`` call that enforces one resolved type."""
    resolved = _resolve_through_alias(type_ref, aliases)
    if resolved.kind == "definition":
        return f"_check_record({source}, {resolved.definition}, {path})"
    if resolved.kind == "enum":
        return f"_check_enum({source}, {_plural_constant(resolved.enum_prefix)}, {path})"
    if resolved.kind == "string":
        return f"_check_str({source}, {path}{resolved.check_arguments})"
    if resolved.kind == "integer":
        return f"_check_int({source}, {path}{resolved.check_arguments})"
    if resolved.kind == "boolean":
        return f"_check_bool({source}, {path})"
    if resolved.kind == "json_object":
        return f"_check_json_object({source}, {path})"
    raise UnsupportedSchemaError(f"no checker for type kind {resolved.kind!r}")


def _render_property_validate(prop: Property, aliases: dict[str, Alias]) -> list[str]:
    """Emit the validation statements for one property."""
    path = 'f"{path}.' + prop.wire_name + '"'
    item_path = 'f"{path}.' + prop.wire_name + '[{index}]"'
    source = f"self.{prop.attribute}"
    type_ref = _resolve_through_alias(prop.type_ref, aliases)
    indent = "        "
    lines: list[str] = []
    if not prop.required:
        lines.append(f"        if {source} is not None:")
        indent = "            "
    if type_ref.kind == "array":
        assert type_ref.item is not None
        entry_name = f"entry_{prop.attribute}"
        lines.append(
            f"{indent}_check_sequence({source}, {path}{type_ref.sequence_arguments})"
        )
        lines.append(f"{indent}for index, {entry_name} in enumerate({source}):")
        lines.append(
            f"{indent}    {_check_expression(type_ref.item, entry_name, item_path, aliases)}"
        )
        return lines
    lines.append(f"{indent}{_check_expression(type_ref, source, path, aliases)}")
    return lines


def _resolve_through_alias(ref: TypeRef, aliases: dict[str, Alias]) -> TypeRef:
    """Return what a ``$ref`` actually decodes and encodes as.

    ``$defs`` aliases are emitted as ``TypeAlias`` declarations, not runtime
    classes, so a property that references one has no ``from_wire``/``to_wire``
    to call. Resolving through the alias also keeps a closed vocabulary closed:
    ``target`` and ``namespace`` are ``$defs`` enums, and a value reaching them
    must still fail closed rather than decode as a plain string.
    """
    if ref.kind == "definition" and ref.definition in aliases:
        return aliases[ref.definition].resolved
    return ref


def _wire_expression(
    type_ref: TypeRef, source: str, wire_name: str, aliases: dict[str, Alias]
) -> str:
    resolved = _resolve_through_alias(type_ref, aliases)
    if resolved.kind == "definition":
        return f'wire["{wire_name}"] = {source}.to_wire()'
    if resolved.kind == "json_object":
        return f'wire["{wire_name}"] = _encode_json_object({source})'
    if resolved.kind == "array":
        assert resolved.item is not None
        if _resolve_through_alias(resolved.item, aliases).kind == "definition":
            return f'wire["{wire_name}"] = [item.to_wire() for item in {source}]'
        return f'wire["{wire_name}"] = list({source})'
    return f'wire["{wire_name}"] = {source}'


def _render_property_decode(prop: Property, aliases: dict[str, Alias]) -> list[str]:
    """Emit the decode statements for one property."""
    quoted_name = json.dumps(prop.wire_name)
    path = 'f"{path}.' + prop.wire_name + '"'
    item_path = 'f"{path}.' + prop.wire_name + '[]"'
    target = f"field_{prop.attribute}"
    type_ref = _resolve_through_alias(prop.type_ref, aliases)

    def decode_of(ref: TypeRef, source: str, where: str) -> str:
        return _decoder_expression(_resolve_through_alias(ref, aliases), source, where)

    lines: list[str] = []
    required_source = f"_require_field(mapping, {quoted_name}, path)"
    optional_source = f"mapping[{quoted_name}]"

    if type_ref.kind == "array":
        assert type_ref.item is not None
        item_decode = decode_of(type_ref.item, "entry", item_path)
        # Cardinality and uniqueness are governed constraints rather than
        # structure, so they are enforced by `validate`, which the decoded
        # value is put through before it is returned.
        if prop.required:
            lines.append(f"        raw_{prop.attribute} = _decode_sequence({required_source}, {path})")
            lines.append(f"        {target} = tuple({item_decode} for entry in raw_{prop.attribute})")
        else:
            lines.append(f"        {target} = None")
            lines.append(f"        if {quoted_name} in mapping:")
            lines.append(f"            raw_{prop.attribute} = _decode_sequence({optional_source}, {path})")
            lines.append(f"            {target} = tuple({item_decode} for entry in raw_{prop.attribute})")
        return lines

    if prop.required:
        lines.append(f"        {target} = {decode_of(type_ref, required_source, path)}")
    else:
        lines.append(f"        {target} = None")
        lines.append(f"        if {quoted_name} in mapping:")
        lines.append(f"            {target} = {decode_of(type_ref, optional_source, path)}")
    return lines


def render_python(model: Model) -> str:
    parts: list[str] = [
        _PRELUDE.format(
            commit=APPROVED_AUTHORITY["proposalCommit"],
            content_set=APPROVED_AUTHORITY["proposalContentSetSha256"],
        )
    ]

    exported: list[str] = ["HostContractDecodeError", "JsonObject"]

    # Identity constants.
    identity = [
        "#: The governed Host Contract version and the single major this build speaks.",
        f'HOST_CONTRACT_VERSION: Final[str] = "{APPROVED_AUTHORITY["hostContractVersion"]}"',
        "HOST_CONTRACT_MAJOR: Final[int] = 1",
        f'HOST_CONTRACT_SCHEMA_ID: Final[str] = "{SCHEMA_ID}"',
        "",
        "#: The exact-byte approval DOC-004 section AA incorporates.",
        f'HOST_CONTRACT_APPROVAL_ID: Final[str] = "{APPROVED_AUTHORITY["approvalId"]}"',
        f'HOST_CONTRACT_PROPOSAL_COMMIT: Final[str] = "{APPROVED_AUTHORITY["proposalCommit"]}"',
        "HOST_CONTRACT_CONTENT_SET_SHA256: Final[str] = (",
        f'    "{APPROVED_AUTHORITY["proposalContentSetSha256"]}"',
        ")",
    ]
    exported.extend(
        [
            "HOST_CONTRACT_APPROVAL_ID",
            "HOST_CONTRACT_CONTENT_SET_SHA256",
            "HOST_CONTRACT_MAJOR",
            "HOST_CONTRACT_PROPOSAL_COMMIT",
            "HOST_CONTRACT_SCHEMA_ID",
            "HOST_CONTRACT_VERSION",
        ]
    )

    vocabulary_lines: list[str] = []
    for vocabulary in sorted(model.vocabularies, key=lambda item: item.prefix):
        vocabulary_lines.append("")
        vocabulary_lines.append(f"#: {vocabulary.docs}")
        for value in vocabulary.values:
            name = _constant_name(vocabulary.prefix, value)
            vocabulary_lines.append(f'{name}: Final[str] = "{value}"')
            exported.append(name)
        vocabulary_lines.append(f"{vocabulary.plural}: Final[tuple[str, ...]] = (")
        for value in vocabulary.values:
            vocabulary_lines.append(f"    {_constant_name(vocabulary.prefix, value)},")
        vocabulary_lines.append(")")
        exported.append(vocabulary.plural)

    alias_lines: list[str] = []
    for alias in model.aliases:
        alias_lines.append("")
        alias_lines.append(f"#: {alias.docs}")
        alias_lines.append(f"{alias.name}: TypeAlias = {alias.annotation}")
        exported.append(alias.name)
        if alias.pattern:
            pattern_name = f"{to_snake(alias.name).upper()}_PATTERN"
            alias_lines.append(f"{pattern_name}: Final[str] = r\"{alias.pattern}\"")
            exported.append(pattern_name)

    record_lines: list[str] = []
    for record in model.records:
        record_lines.append("")
        record_lines.append("")
        record_lines.append(_render_record(record, model))
        exported.append(record.name)

    branch_lines = [
        "",
        "#: Every governed top-level record, in the canonical schema's `oneOf` order.",
        "HOST_CONTRACT_RECORDS: Final[tuple[str, ...]] = (",
        *[f'    "{name}",' for name in model.branch_names],
        ")",
    ]
    exported.append("HOST_CONTRACT_RECORDS")

    export_lines = ["__all__ = ["]
    export_lines.extend(f'    "{name}",' for name in sorted(set(exported), key=export_sort_key))
    export_lines.append("]")

    parts.append("\n".join(export_lines))
    parts.append("\n")
    parts.append(_HELPERS)
    parts.append("\n")
    parts.append("\n".join(identity))
    parts.append("\n")
    parts.append("\n".join(vocabulary_lines).lstrip("\n"))
    parts.append("\n")
    parts.append("\n".join(alias_lines).lstrip("\n"))
    parts.append("\n")
    parts.append("\n".join(branch_lines).lstrip("\n"))
    parts.append("\n")
    parts.append("\n".join(record_lines).lstrip("\n"))
    return "".join(parts).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def render_all() -> dict[Path, str]:
    """Return every generated artifact's path and content."""
    return {PYTHON_TARGET: render_python(build_model(load_schema()))}


def write_all(artifacts: dict[Path, str]) -> list[Path]:
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
        label = _display_path(path)
        if not path.exists():
            findings.append(f"{label}: generated artifact is missing")
            continue
        if path.read_text(encoding="utf-8") != content:
            findings.append(f"{label}: generated artifact is out of date with the schema")
    return findings


def _display_path(path: Path) -> str:
    """Return a repository-relative label, or the full path when it is outside."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the approved bytes and the committed artifact instead of writing",
    )
    args = parser.parse_args(argv)

    authority_findings = check_authority_digests()
    if authority_findings:
        print("Host Contract canonical resources are not the approved bytes:\n", file=sys.stderr)
        for finding in authority_findings:
            print(f"  - {finding}", file=sys.stderr)
        print(
            "\nDOC-004 section AA incorporates exact bytes. Restore them, or obtain a new"
            "\nexact-byte approval and update APPROVED_RESOURCE_DIGESTS in the same change.",
            file=sys.stderr,
        )
        return 1

    try:
        artifacts = render_all()
    except UnsupportedSchemaError as error:
        print(f"Host Contract generation FAILED: {error}", file=sys.stderr)
        return 1

    if args.check:
        findings = check_all(artifacts)
        if findings:
            print("Host Contract artifacts are out of date:\n", file=sys.stderr)
            for finding in findings:
                print(f"  - {finding}", file=sys.stderr)
            print("\nRegenerate with: python scripts/generate-host-contract.py", file=sys.stderr)
            return 1
        print("Host Contract canonical resources and generated artifacts are up to date.")
        return 0

    changed = write_all(artifacts)
    for path in changed:
        print(f"wrote {_display_path(path)}")
    if not changed:
        print("Host Contract artifacts are already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
