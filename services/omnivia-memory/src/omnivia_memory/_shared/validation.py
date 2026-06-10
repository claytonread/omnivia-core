"""Shared validation primitives for public contract surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)


@dataclass(frozen=True)
class ValidationResult:
    """Validation output for public contract helpers."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_iso_timestamp(field_name: str, value: str, errors: list[str]) -> None:
    """Validate a required ISO 8601 timestamp string."""

    if not value:
        errors.append(f"{field_name} is required")
        return
    validate_optional_iso_timestamp(field_name, value, errors)


def validate_optional_iso_timestamp(
    field_name: str,
    value: str | None,
    errors: list[str],
) -> None:
    """Validate an optional ISO 8601 timestamp string."""

    if value is None:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field_name} must be an ISO timestamp")


def scan_sensitive_fields(
    data: dict[str, Any] | list[Any] | Any,
    errors: list[str],
    prefix: str = "",
) -> None:
    """Recursively reject sensitive field names in public-safe payloads."""

    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if key.lower() in SENSITIVE_KEYS:
                errors.append(f"{path} must not expose sensitive fields")
            scan_sensitive_fields(value, errors, path)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            scan_sensitive_fields(value, errors, f"{prefix}[{index}]")
