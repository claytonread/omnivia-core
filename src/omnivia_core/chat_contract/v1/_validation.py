"""Internal JSON Schema evaluator for the bounded Chat Runtime Contract v1 codec.

:mod:`omnivia_core.chat_contract.v1.codec` emits a document only if that exact
document satisfies the record's own canonical subschema. This module is how
that check runs: it evaluates the generated
:data:`~omnivia_core.chat_contract.v1.generated.VALIDATION_SCHEMAS` -- the
transitive ``$ref`` closure of the six bounded record roots, read from the
approved ``contracts/chat/v1/schemas`` bytes by
``scripts/generate-chat-contract.py`` -- against an instance.

It is not a general JSON Schema implementation and must not be used as one. It
implements exactly the assertion keywords that closure actually contains, and
the generator refuses to emit a keyword this module does not implement, so the
two cannot drift into a codec that silently skips an approved rule. The import
below fails closed on that contract rather than deferring the mismatch to a
document that quietly passes.

JSON Schema is defined over a JSON instance, so a keyword can only decide a
value the JSON data model already admits. A Python ``float('nan')`` or a
mapping keyed by ``1`` is not such a value: no keyword in the closure rejects
it, ``sorted`` over mixed keys raises :class:`TypeError`, and ``json.dumps``
would stringify the key rather than refuse it. :func:`json_domain_violation`
therefore runs before any schema keyword and refuses those outright.

Two deliberate deviations from a full implementation, both in the fail-closed
direction, both consequences of this being an *emission* gate:

- ``format: date-time`` is asserted, not annotated. JSON Schema leaves
  ``format`` advisory; a governed ``Timestamp`` that is not an RFC 3339
  instant is not a document this contract should put on the wire. RFC 3339's
  leap-second minute (``:60``) is rejected, since no governed producer here
  emits one.
- ``type: integer`` accepts only a JSON integer, never the mathematically
  equal ``1.0``.

Diagnostics name a field path and the rule it broke, never the rejected value,
and never a field name outside the schema's own closed ``properties`` set -- a
free-form namespace or option name is caller content like any other, so a
violation under one is reported at its parent path (matching ``codec``'s own
rule that a refused document may be carrying exactly what this contract
forbids on the wire).

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from math import isfinite
from typing import Any, Final

from omnivia_core.chat_contract.v1.generated import (
    VALIDATION_KEYWORDS,
    VALIDATION_SCHEMAS,
)

__all__ = ["first_violation", "json_domain_violation"]

#: Every keyword this module asserts. Held against the generator's own claim at
#: import: metadata carrying a rule nothing evaluates is an under-validating
#: codec, which is the exact defect this module exists to close.
_IMPLEMENTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$ref", "additionalProperties", "allOf", "anyOf", "const", "else", "enum", "format", "if",
        "items", "maxItems", "maxLength", "maxProperties", "maximum", "minItems", "minLength",
        "minProperties", "minimum", "not", "oneOf", "pattern", "properties", "propertyNames",
        "required", "then", "type", "uniqueItems",
    }
)

_UNIMPLEMENTED: Final[tuple[str, ...]] = tuple(sorted(set(VALIDATION_KEYWORDS) - _IMPLEMENTED_KEYWORDS))
if _UNIMPLEMENTED:  # pragma: no cover - a generator/validator mismatch, not a reachable state
    raise RuntimeError(
        f"generated Chat Runtime Contract v1 validation metadata carries keyword(s) {list(_UNIMPLEMENTED)} "
        "that omnivia_core.chat_contract.v1._validation does not implement"
    )


def _is_object(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


_TYPE_CHECKS: Final[Mapping[str, Callable[[Any], bool]]] = {
    "array": _is_array,
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "null": lambda value: value is None,
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "object": _is_object,
    "string": lambda value: isinstance(value, str),
}

#: RFC 3339 §5.6 ``date-time``. The grammar is matched here and the calendar
#: date is proven by :meth:`datetime.fromisoformat` on the fraction-free form,
#: so an impossible day such as ``2026-02-30`` is refused too.
_DATE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")

_PATTERN_CACHE: Final[dict[str, re.Pattern[str]]] = {}


def _is_date_time(value: str) -> bool:
    matched = _DATE_TIME.match(value)
    if matched is None:
        return False
    date, clock, offset = matched.groups()
    try:
        datetime.fromisoformat(f"{date}T{clock}{'+00:00' if offset.upper() == 'Z' else offset}")
    except ValueError:
        return False
    return True


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality: ``true`` is never the number ``1``."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def _identity(value: Any) -> Any:
    """A hashable stand-in for a JSON value, for ``uniqueItems``."""
    if _is_object(value):
        return ("object", tuple(sorted((key, _identity(item)) for key, item in value.items())))
    if _is_array(value):
        return ("array", tuple(_identity(item) for item in value))
    if isinstance(value, bool):
        return ("boolean", value)
    return ("scalar", value)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def json_domain_violation(value: Any, path: str = "") -> tuple[str, str] | None:
    """Return the first ``(path, rule)`` where ``value`` leaves the JSON data model.

    Two Python values have no JSON counterpart and so cannot be decided by a
    schema keyword: a non-finite float (``NaN``, ``±Infinity``), and a mapping
    key that is not a string. Both are refused here, at any depth, including
    inside an object whose schema deliberately permits arbitrary properties.

    Member names are caller content (see the module docstring), so a violation
    under one is reported at its parent path and never named; array indices are
    this contract's own structure and are kept.
    """
    if isinstance(value, float) and not isfinite(value):
        return path, "expected a finite JSON number"
    if _is_object(value):
        for key, item in value.items():
            if not isinstance(key, str):
                return path, "carries a field name that is not a JSON string"
            violation = json_domain_violation(item, path)
            if violation is not None:
                return violation
    elif _is_array(value):
        for index, item in enumerate(value):
            violation = json_domain_violation(item, f"{path}[{index}]")
            if violation is not None:
                return violation
    return None


def first_violation(document: Any, ref: str) -> tuple[str, str] | None:
    """Return the first ``(path, rule)`` ``document`` breaks under ``ref``, or ``None``.

    ``ref`` is a key of
    :data:`~omnivia_core.chat_contract.v1.generated.VALIDATION_SCHEMAS`; the
    returned path is dotted and relative to the document root (``""`` when the
    violation is the root value itself).
    """
    return json_domain_violation(document) or _check(document, VALIDATION_SCHEMAS[ref], "")


def _check(value: Any, schema: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    reference = schema.get("$ref")
    if reference is not None:
        violation = _check(value, VALIDATION_SCHEMAS[reference], path)
        if violation is not None:
            return violation

    declared = schema.get("type")
    if declared is not None:
        names = (declared,) if isinstance(declared, str) else tuple(declared)
        if not any(_TYPE_CHECKS[name](value) for name in names):
            return path, f"expected a JSON {' or '.join(names)}"

    if "const" in schema and not _json_equal(value, schema["const"]):
        return path, "expected the one governed constant value"

    permitted = schema.get("enum")
    if permitted is not None and not any(_json_equal(value, member) for member in permitted):
        return path, f"expected one of {len(permitted)} governed values"

    for check in (_check_string, _check_number, _check_array, _check_object):
        violation = check(value, schema, path)
        if violation is not None:
            return violation

    return _check_applicators(value, schema, path)


def _check_string(value: Any, schema: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    minimum = schema.get("minLength")
    if minimum is not None and len(value) < minimum:
        return path, f"expected at least {minimum} character(s)"
    maximum = schema.get("maxLength")
    if maximum is not None and len(value) > maximum:
        return path, f"expected at most {maximum} character(s)"
    pattern = schema.get("pattern")
    if pattern is not None:
        compiled = _PATTERN_CACHE.get(pattern)
        if compiled is None:
            compiled = _PATTERN_CACHE.setdefault(pattern, re.compile(pattern))
        if compiled.search(value) is None:
            return path, "expected a string matching the governed pattern"
    if schema.get("format") == "date-time" and not _is_date_time(value):
        return path, "expected an RFC 3339 date-time"
    return None


def _check_number(value: Any, schema: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        return path, f"expected a value of at least {minimum}"
    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        return path, f"expected a value of at most {maximum}"
    return None


def _check_array(value: Any, schema: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    if not _is_array(value):
        return None
    minimum = schema.get("minItems")
    if minimum is not None and len(value) < minimum:
        return path, f"expected at least {minimum} item(s)"
    maximum = schema.get("maxItems")
    if maximum is not None and len(value) > maximum:
        return path, f"expected at most {maximum} item(s)"
    if schema.get("uniqueItems") is True:
        identities = [_identity(item) for item in value]
        if len(set(identities)) != len(identities):
            return path, "expected unique items"
    items = schema.get("items")
    if items is not None:
        for index, item in enumerate(value):
            violation = _check(item, items, f"{path}[{index}]")
            if violation is not None:
                return violation
    return None


def _check_object(value: Any, schema: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    if not _is_object(value):
        return None
    for name in schema.get("required", ()):
        if name not in value:
            return _join(path, name), "required field is missing"
    minimum = schema.get("minProperties")
    if minimum is not None and len(value) < minimum:
        return path, f"expected at least {minimum} field(s)"
    maximum = schema.get("maxProperties")
    if maximum is not None and len(value) > maximum:
        return path, f"expected at most {maximum} field(s)"

    properties = schema.get("properties", {})
    names = schema.get("propertyNames")
    additional = schema.get("additionalProperties")
    for name in sorted(value):
        governed = properties.get(name)
        if governed is not None:
            violation = _check(value[name], governed, _join(path, name))
            if violation is not None:
                return violation
            continue
        # Not in the closed set, so the name is caller content: report at the
        # parent path and never name it.
        if names is not None and _check(name, names, path) is not None:
            return path, "a field name is not a governed property name"
        if additional is False:
            return path, "carries a field the governed shape does not define"
        if isinstance(additional, Mapping) and _check(value[name], additional, path) is not None:
            return path, "carries a field whose value is not the governed shape"
    return None


def _check_applicators(value: Any, schema: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    for subschema in schema.get("allOf", ()):
        violation = _check(value, subschema, path)
        if violation is not None:
            return violation

    alternatives = schema.get("anyOf")
    if alternatives is not None and all(_check(value, sub, path) is not None for sub in alternatives):
        return path, f"matched none of the {len(alternatives)} permitted shapes"

    exclusive = schema.get("oneOf")
    if exclusive is not None:
        matched = sum(1 for sub in exclusive if _check(value, sub, path) is None)
        if matched != 1:
            return path, f"expected exactly one of {len(exclusive)} permitted shapes to match, matched {matched}"

    forbidden = schema.get("not")
    if forbidden is not None and _check(value, forbidden, path) is None:
        return path, "matched a shape the governed shape forbids"

    condition = schema.get("if")
    if condition is not None:
        branch = schema.get("then") if _check(value, condition, path) is None else schema.get("else")
        if branch is not None:
            return _check(value, branch, path)
    return None
