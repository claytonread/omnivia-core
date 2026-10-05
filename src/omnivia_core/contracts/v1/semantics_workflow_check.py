"""Schema-aware Workflow Check at the `publication` and `runtime_load` gates.

A pure contract-layer seam: given one `WorkflowValue`, an exact schema pin carried in its
`physicalSchema`, and an injected resolver, decide whether the value's definition is valid
against that exact schema. There is no registry, filesystem, HTTP or packaged-resource
lookup here; the caller supplies the resolver. It is not a Workflow Check operation and
does not compute `runnable` or implementation-binding availability -- those stay with
:func:`semantics_workflow.validate_workflow_check_readiness_extension`.

**Exact reference.** `physicalSchema` is a `JsonSchemaReference`: `schemaId` (an absolute
URI), `schemaVersion` (a `ReleaseVersion`) and an optional `digest` (a `sha256:` digest). It
is a closed record, exact only when it carries a valid `schemaId` and `schemaVersion` and no
member other than those and `digest`; `digest` may strengthen the pin and is compared with the
`sha256:` RFC 8785 digest of the resolved schema document. Any other reference shape,
including one with an unknown member such as `schemaDigest`, is floating and never reaches
the resolver. `schemaId` is an identity, not an endpoint: it is
checked by a small syntactic absolute-URI predicate (scheme present, no whitespace, control
or non-ASCII characters, no relative references) and is never fetched. The broad
`physicalSchema` shape check in `validate_workflow_value` is unchanged.

**Self-contained schemas.** The resolved schema is evaluated alone. The only `$ref` form
accepted is `#/$defs/<name>` into the schema's own root `$defs`; external, absolute and
unresolved references and reference cycles are `SCHEMA_UNSUPPORTED`, as is any schema
artifact the bounded evaluator cannot treat as JSON Schema.

**Presence.** `present`, `null_value` and `empty` carry a concrete `value` (a JSON null or an
empty string/array/object is still a value) and are evaluated against the whole value; the
schema applies to `value` as-is, whatever `cardinality` says. `absent`, `redacted`,
`unavailable` and `failed` carry no inspectable payload: the schema reference is still
checked, but the instance is not evaluated and the result is `deferred_to_runtime=True`.
Deferred is not "schema-valid": it is a statement that this gate did not look.

**Diagnostics** carry a stable code, the subject (`valueId`) and the schema reference only.
They never carry the value or any text derived from it, so identical inputs give identical
results. `SCHEMA_INVALID` reports only how many findings there were. The five codes are the
`FLOATING_REFERENCE_PROHIBITED`, `CONTRACT_VERSION_UNRESOLVED`,
`CONTRACT_VERSION_INCOMPATIBLE`, `SCHEMA_UNSUPPORTED` and `SCHEMA_INVALID` constants.

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1.canonical_json import canonical_bytes
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.conformance import (
    PHYSICAL_SCHEMA_PROFILE,
    PHYSICAL_SCHEMA_PROFILE_VERSION,
    SchemaEvaluationBudgetExceeded,
    SchemaEvaluationError,
    evaluate_physical_json_schema,
    require_supported_physical_json_schema,
)
from omnivia_core.contracts.v1.generated import is_content_checksum, is_release_version
from omnivia_core.contracts.v1.semantics_workflow import validate_workflow_value

__all__ = [
    "CONTRACT_VERSION_INCOMPATIBLE",
    "CONTRACT_VERSION_UNRESOLVED",
    "FLOATING_REFERENCE_PROHIBITED",
    "PHYSICAL_SCHEMA_PROFILE",
    "PHYSICAL_SCHEMA_PROFILE_VERSION",
    "SCHEMA_CHECK_PROFILES",
    "SCHEMA_INVALID",
    "SCHEMA_UNSUPPORTED",
    "ResolvedSchema",
    "SchemaResolver",
    "WorkflowSchemaCheckDiagnostic",
    "WorkflowSchemaCheckResult",
    "check_workflow_value_schema",
]

SCHEMA_CHECK_PROFILES: Final[tuple[str, ...]] = ("publication", "runtime_load")

FLOATING_REFERENCE_PROHIBITED: Final = "FLOATING_REFERENCE_PROHIBITED"
CONTRACT_VERSION_UNRESOLVED: Final = "CONTRACT_VERSION_UNRESOLVED"
CONTRACT_VERSION_INCOMPATIBLE: Final = "CONTRACT_VERSION_INCOMPATIBLE"
SCHEMA_UNSUPPORTED: Final = "SCHEMA_UNSUPPORTED"
SCHEMA_INVALID: Final = "SCHEMA_INVALID"

_REFERENCE_MEMBERS: Final = frozenset({"schemaId", "schemaVersion", "digest"})
_DEFERRED_PRESENCES: Final = frozenset({"absent", "redacted", "unavailable", "failed"})


@dataclass(frozen=True)
class ResolvedSchema:
    """The artifact a resolver returns for one exact `(schema_id, schema_version)`.

    `schema` must be a self-contained JSON Schema document admitted by physical-schema
    profile ``1.0.0``. The identity fields are echoed so the check can refuse a resolver
    that answers for a different pin.
    """

    schema_id: str
    schema_version: str
    schema: Mapping[str, Any]


#: Injected lookup: exact `(schema_id, schema_version)` -> artifact, or `None` when unknown.
#: A resolver that raises is a fault in the seam and propagates; it is not a diagnostic.
SchemaResolver = Callable[[str, str], ResolvedSchema | None]


@dataclass(frozen=True)
class WorkflowSchemaCheckDiagnostic:
    """One stable-coded refusal. Never carries the value or text derived from it."""

    code: str
    subject: str
    schema_id: str | None = None
    schema_version: str | None = None
    finding_count: int = 0


@dataclass(frozen=True)
class WorkflowSchemaCheckResult:
    """Outcome of one schema check at one gate.

    `definition_valid` is true exactly when there are no diagnostics. `deferred_to_runtime`
    is true when the value had no inspectable payload, so the instance was not evaluated
    (a deferred result can still be `definition_valid` about the reference and schema).
    """

    profile: str
    definition_valid: bool
    diagnostics: tuple[WorkflowSchemaCheckDiagnostic, ...]
    deferred_to_runtime: bool


_URI_SCHEME_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_URI_FORBIDDEN_RE: Final = re.compile(r'[^\x21-\x7e]|[<>"\\^`{|}]|%(?![0-9A-Fa-f]{2})')


def _is_absolute_uri(value: object) -> bool:
    """Syntactic absolute URI: scheme, non-empty rest, printable ASCII, valid escapes.

    Not a service-endpoint policy: any scheme is accepted and nothing is ever fetched.
    """
    if not isinstance(value, str):
        return False
    scheme = _URI_SCHEME_RE.match(value)
    rest = value[scheme.end() :] if scheme else ""
    return bool(rest) and rest.count("#") <= 1 and _URI_FORBIDDEN_RE.search(value) is None


def check_workflow_value_schema(
    value_record: object, profile: str, resolver: SchemaResolver
) -> WorkflowSchemaCheckResult:
    """Check one `WorkflowValue` against its exact physical schema at `profile`.

    Raises :class:`ContractSemanticError` for a profile other than `publication` or
    `runtime_load` and for a record the existing WorkflowValue validator refuses; those are
    caller errors, not check outcomes. Every other failure is a diagnostic.
    """
    if profile not in SCHEMA_CHECK_PROFILES:
        raise ContractSemanticError(
            f"WorkflowSchemaCheck: profile must be one of {SCHEMA_CHECK_PROFILES!r}"
        )
    validate_workflow_value(value_record)
    assert isinstance(value_record, Mapping)
    subject = value_record["valueId"]
    reference = value_record["physicalSchema"]
    assert isinstance(reference, Mapping)
    deferred = value_record["presence"] in _DEFERRED_PRESENCES

    def refuse(code: str, ref: tuple[str, str] | None, findings: int = 0) -> WorkflowSchemaCheckResult:
        diagnostic = WorkflowSchemaCheckDiagnostic(
            code, subject, ref[0] if ref else None, ref[1] if ref else None, findings
        )
        return WorkflowSchemaCheckResult(profile, False, (diagnostic,), deferred)

    schema_id = reference.get("schemaId")
    version = reference.get("schemaVersion")
    if not (
        _is_absolute_uri(schema_id)
        and is_release_version(version)
        and reference.keys() <= _REFERENCE_MEMBERS
    ):
        return refuse(FLOATING_REFERENCE_PROHIBITED, None)
    assert isinstance(schema_id, str) and isinstance(version, str)
    pin = (schema_id, version)

    resolved = resolver(schema_id, version)
    if (
        not isinstance(resolved, ResolvedSchema)
        or (resolved.schema_id, resolved.schema_version) != pin
    ):
        return refuse(CONTRACT_VERSION_UNRESOLVED, pin)
    schema = resolved.schema

    if "digest" in reference:
        supplied = reference["digest"]
        try:
            actual = f"sha256:{sha256(canonical_bytes(schema)).hexdigest()}"
        except (ContractSemanticError, TypeError, ValueError, RecursionError):
            return refuse(SCHEMA_UNSUPPORTED, pin)
        if not is_content_checksum(supplied) or supplied != actual:
            return refuse(CONTRACT_VERSION_INCOMPATIBLE, pin)

    try:
        if deferred:
            require_supported_physical_json_schema(schema, schema_id)
            return WorkflowSchemaCheckResult(profile, True, (), True)
        findings = evaluate_physical_json_schema(
            value_record["value"], schema, schema_id
        )
    except SchemaEvaluationBudgetExceeded:
        # A deterministic limit is not an unsupported schema verdict and must
        # not be turned into a completed pass/fail result. The exception text is
        # a stable policy label and contains no schema or instance content.
        raise
    except SchemaEvaluationError:
        return refuse(SCHEMA_UNSUPPORTED, pin)
    if findings:
        return refuse(SCHEMA_INVALID, pin, len(findings))
    return WorkflowSchemaCheckResult(profile, True, (), False)
