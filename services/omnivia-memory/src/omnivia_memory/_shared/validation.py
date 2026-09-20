"""Compatibility facade for shared validation primitives.

Deprecated: import these from ``omnivia_core._shared.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings (`Any`, `annotations`, `dataclass`, `datetime`, `field`) -- names the
# canonical module's own imports left at its module scope and that this leaf's
# historical namespace still has to resolve. No other error code is suppressed.
from omnivia_core._shared.validation import (  # type: ignore[attr-defined,unused-ignore]
    SENSITIVE_KEYS,
    Any,
    ValidationResult,
    annotations,
    dataclass,
    datetime,
    field,
    scan_sensitive_fields,
    validate_iso_timestamp,
    validate_optional_iso_timestamp,
)
