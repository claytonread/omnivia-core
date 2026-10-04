"""Strict request classification for `analysis.start` (structured-data milestone 1).

Milestone 1 of SPEC-CORE-DATA-001 is a refusal contract, not an executor: the
operation decodes strictly, classifies the request, and every well-formed
request is answered with the typed outcome ``dependency_unavailable``. This
module owns that classification as a pure, standard-library-only function so
the contract package stays free of runtime, storage and transport concerns
(the same boundary every other ``semantics_*`` module holds).

**Why a hand-written classifier beside the generated codec.** The generated
``from_wire`` decoder is deliberately tolerant: it preserves unknown fields and
unknown open-string values so a compatible minor release can add vocabulary.
D-0028 (CO-3, decoder-order clarification) requires the opposite posture at
this boundary, in a fixed order:

1. the document must be a JSON object, and its ``request_version`` a
   well-formed ``major.minor`` string -- anything else is ``invalid_request``;
2. the version is classified *before* the body schema is applied, so a
   well-formed unsupported version is never masked into ``invalid_request`` by
   a current-version unknown-field check: an unsupported major is
   ``incompatible_version`` and a supported major with an unsupported minor is
   ``unsupported_minor_version``;
3. only a supported version reaches the strict shape boundary: unknown fields,
   missing required fields, wrong types, an out-of-vocabulary ``use_class``
   (including ``action_input``, which is deliberately outside the milestone-1
   admitted set), a target carrying both or neither reference branch, both or
   neither temporal scope, duplicate parameter names, or an unresolvable
   business timezone are ``invalid_request``;
4. a request that passes every check is ``dependency_unavailable``: the
   contract is understood, which is not a statement that the referenced
   analysis is authorised, resolvable or executable.

Classification is total over any JSON document a transport could deliver: a
crash is a failure of the refusal boundary, so every malformed input maps to a
typed outcome rather than an exception escaping to the caller. Scalars are gated
by exact type and object keys are checked before any set or lookup, so a
str/int/float subclass supplied in process is refused without its operators
running. Totality is scoped to JSON-origin values and well-behaved abstract
containers, not to hostile container protocol methods.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .generated import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
)

#: The only payload version milestone 1 supports. Later compatible minors join
#: by widening this constant and the classification below together.
SUPPORTED_ANALYSIS_PAYLOAD_VERSION: Final = "1.0"

#: Supported major for the analysis payload. Any other major is a breaking
#: boundary this contract does not negotiate.
SUPPORTED_ANALYSIS_PAYLOAD_MAJOR: Final = 1

#: The milestone-1 admitted use classes. ``action_input`` is deliberately
#: absent (UDL-D06 keeps action consumption denied): a request naming it is
#: rejected at the strict shape boundary as ``invalid_request``, per D-0028's
#: clarification, and never reaches the generic dependency refusal.
ADMITTED_ANALYSIS_USE_CLASSES: Final = frozenset(
    {"exploration", "historical_display", "current_publication"}
)

_ANALYSIS_INPUT_FIELDS: Final = frozenset(
    {
        "request_version",
        "target",
        "as_of_date",
        "period_start",
        "period_end",
        "business_timezone",
        "use_class",
        "parameters",
        "output_bounds",
        "purpose_reference",
    }
)

_ANALYSIS_METRIC_FIELDS: Final = frozenset({"kind", "metric_revision_id"})
_ANALYSIS_DATA_VIEW_FIELDS: Final = frozenset({"kind", "data_view_revision_id"})
_PARAMETER_FIELDS: Final = frozenset({"name", "value"})
_OUTPUT_BOUNDS_FIELDS: Final = frozenset({"max_rows"})

_VERSION_PATTERN: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_DATE_PATTERN: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIMEZONE_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_+/~-]*$")
_IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

_IDENTIFIER_MAX: Final = 128
_VERSION_MAX: Final = 32
_TIMEZONE_MAX: Final = 64

_INVALID_REQUEST_DETAIL: Final = (
    "the analysis request payload is not valid for this contract version"
)


#: Scalar and byte-like ancestry. A value with any of these in its MRO is never a
#: JSON container, even when it also mixes in a Mapping or Sequence ABC, so the
#: scalar barrier runs before any container protocol is entered.
_NON_CONTAINER_ANCESTORS: Final = (str, int, float, bytes, bytearray, memoryview)


def _is_array(value: Any) -> bool:
    """A JSON array: any non-text, non-bytes ``Sequence`` (list, tuple, custom)."""
    return not isinstance(value, _NON_CONTAINER_ANCESTORS) and isinstance(
        value, Sequence
    )


def _mapping_keys(value: Any) -> frozenset[str] | None:
    """The keys of a Mapping as a built-in frozenset, or None when ``value`` is
    not a Mapping or any key is not an exact ``str``.

    Each key's type is checked before anything hashes or compares it, so a
    hostile ``str`` subclass key is refused without entering a set or lookup.
    A scalar or byte-like hybrid is refused before its Mapping protocol runs.
    """
    if isinstance(value, _NON_CONTAINER_ANCESTORS) or not isinstance(value, Mapping):
        return None
    keys: list[str] = []
    for key in value:
        if type(key) is not str:
            return None
        keys.append(key)
    return frozenset(keys)


def _identifier_ok(value: Any) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= _IDENTIFIER_MAX
        and _IDENTIFIER_PATTERN.fullmatch(value) is not None
    )


def _strict_fields(document: Any, allowed: frozenset[str]) -> bool:
    """Every present key is declared and every value is JSON data."""
    keys = _mapping_keys(document)
    if keys is None or not keys <= allowed:
        return False
    return all(_json_data(value) for value in document.values())


def _json_data(value: Any, depth: int = 0) -> bool:
    """A JSON value: null, bool, int, float, str, or a (bounded-depth)
    composition of those. Bounded depth keeps a hostile nesting bomb from
    turning the strict decode into unbounded work.

    Scalars are gated by exact type, so a subclass is refused before any of its
    methods run; the NaN/infinity comparisons only ever see an exact float. A
    scalar or byte-like subclass that also mixes in a container ABC is refused by
    the ancestry barrier before its Mapping or Sequence branch is entered.
    """
    if depth > 32:
        return False
    kind = type(value)
    if value is None or kind is bool or kind is str or kind is int:
        return True
    if kind is float:
        return value == value and value not in (float("inf"), float("-inf"))  # noqa: PLR0124 - NaN check
    if isinstance(value, _NON_CONTAINER_ANCESTORS):
        return False
    if isinstance(value, Mapping):
        keys = _mapping_keys(value)
        return keys is not None and all(
            _json_data(item, depth + 1) for item in value.values()
        )
    if _is_array(value):
        return all(_json_data(item, depth + 1) for item in value)
    return False


def _business_date_ok(value: Any) -> bool:
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        return False
    year, month, day = (int(part) for part in value.split("-"))
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= _days_in_month(year, month)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


def _timezone_ok(value: Any) -> bool:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _TIMEZONE_MAX
        or _TIMEZONE_PATTERN.fullmatch(value) is None
    ):
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
    except Exception:  # noqa: BLE001 - any resolver failure is a refusal
        return False
    return True


def _target_ok(target: Any) -> bool:
    keys = _mapping_keys(target)
    if keys is None:
        return False
    kind = target.get("kind")
    if type(kind) is not str:
        return False
    if kind == "metric":
        if not keys <= _ANALYSIS_METRIC_FIELDS:
            return False
        return _identifier_ok(target.get("metric_revision_id"))
    if kind == "data_view":
        if not keys <= _ANALYSIS_DATA_VIEW_FIELDS:
            return False
        return _identifier_ok(target.get("data_view_revision_id"))
    return False


def _parameters_ok(parameters: Any) -> bool:
    """A present ``parameters`` value: an array of exact ``{name, value}`` pairs.

    Callers check presence first, so an explicit null is refused here as a
    non-array, matching the generated decoder.
    """
    if not _is_array(parameters):
        return False
    names: set[str] = set()
    for parameter in parameters:
        if _mapping_keys(parameter) != _PARAMETER_FIELDS:
            return False
        name = parameter["name"]
        # The identifier gate runs before set membership, so a hostile name is
        # never hashed or compared.
        if not _identifier_ok(name) or name in names:
            return False
        value = parameter["value"]
        # A parameter value is a JSON object (the generated ``JsonObject``), so
        # its top level must be a Mapping before the nested JSON data is checked.
        if not isinstance(value, Mapping) or not _json_data(value):
            return False
        names.add(name)
    return True


def _output_bounds_ok(bounds: Any) -> bool:
    """A present ``output_bounds`` value: an object whose ``max_rows``, when
    present, is a positive integer. An omitted ``max_rows`` is valid; a present
    null is not."""
    keys = _mapping_keys(bounds)
    if keys is None or not keys <= _OUTPUT_BOUNDS_FIELDS:
        return False
    if "max_rows" not in keys:
        return True
    max_rows = bounds["max_rows"]
    return type(max_rows) is int and max_rows >= 1


def classify_analysis_start_request(document: Any) -> tuple[str, str]:
    """Classify one `analysis.start` request document.

    Returns ``(outcome_code, detail)`` where ``outcome_code`` is one of the
    four typed outcomes CO-3 fixes: ``invalid_request``,
    ``incompatible_version``, ``unsupported_minor_version`` or
    ``dependency_unavailable``. It never raises for JSON-origin documents or the
    guarded hostile scalar/key cases; arbitrary hostile Mapping/Sequence protocol
    implementations are outside that guarantee. It never touches storage, the
    network, credentials or a worker: classification is the whole of milestone
    1, and the caller's only job is to render the outcome.
    """
    # The document must be a JSON object: a non-Mapping, or a Mapping with any
    # non-str key, is refused before its keys are looked up or compared.
    keys = _mapping_keys(document)
    if keys is None:
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    version = document.get("request_version")
    if (
        type(version) is not str
        or len(version) > _VERSION_MAX
        or _VERSION_PATTERN.fullmatch(version) is None
    ):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL
    major_text, minor_text = version.split(".")
    major, minor = int(major_text), int(minor_text)
    if major != SUPPORTED_ANALYSIS_PAYLOAD_MAJOR:
        return ERROR_CODE_INCOMPATIBLE_VERSION, (
            f"analysis request payload major {major} is not supported; "
            f"supported major is {SUPPORTED_ANALYSIS_PAYLOAD_MAJOR}"
        )
    if version != SUPPORTED_ANALYSIS_PAYLOAD_VERSION:
        return ERROR_CODE_UNSUPPORTED_MINOR_VERSION, (
            f"analysis request payload minor {minor} of major {major} is not "
            f"supported; supported version is {SUPPORTED_ANALYSIS_PAYLOAD_VERSION}"
        )

    # Supported version: the strict shape boundary applies now. Unknown fields
    # are refused here, never silently preserved the way the tolerant
    # production decoder would preserve them.
    if not _strict_fields(document, _ANALYSIS_INPUT_FIELDS):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    use_class = document.get("use_class")
    if type(use_class) is not str or use_class not in ADMITTED_ANALYSIS_USE_CLASSES:
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    if not _target_ok(document.get("target")):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    has_as_of = "as_of_date" in keys
    has_period = "period_start" in keys or "period_end" in keys
    if has_period and not (
        "period_start" in keys
        and "period_end" in keys
        and _business_date_ok(document["period_start"])
        and _business_date_ok(document["period_end"])
        and document["period_start"] <= document["period_end"]
    ):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL
    if has_as_of == has_period:
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL
    temporal_values = [
        document[key]
        for key in ("as_of_date", "period_start", "period_end")
        if key in keys
    ]
    if not all(_business_date_ok(value) for value in temporal_values):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    if not _timezone_ok(document.get("business_timezone")):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    # Optional fields: omitted is valid, but a present value (including null)
    # must satisfy its schema, so presence is tested rather than ``.get()``.
    if "parameters" in keys and not _parameters_ok(document["parameters"]):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL
    if "output_bounds" in keys and not _output_bounds_ok(document["output_bounds"]):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    purpose = document.get("purpose_reference")
    if not _identifier_ok(purpose):
        return ERROR_CODE_INVALID_REQUEST, _INVALID_REQUEST_DETAIL

    return ERROR_CODE_DEPENDENCY_UNAVAILABLE, (
        "the analysis request is understood; no analytical executor is "
        "admitted in this milestone, so no job was started and none will be "
        "started by retrying"
    )
