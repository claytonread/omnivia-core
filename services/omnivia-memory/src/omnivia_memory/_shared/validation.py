"""Compatibility facade for shared validation primitives.

Deprecated: import these from ``omnivia_core._shared.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core._shared.validation import (
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
