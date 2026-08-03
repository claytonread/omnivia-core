"""Read-only access to the packaged Host Contract v1 schema and fixtures.

``contracts/host/v1/{schemas,fixtures}`` is the single checked-in canonical
copy of the exact bytes DOC-004 §AA incorporates; the wheel force-includes it
under ``omnivia_core/host_contract/v1/resources`` (see ``pyproject.toml``).
This module is the only supported way to read that packaged copy at runtime:
callers never construct a resource path themselves, so the on-disk layout
stays free to change without breaking anyone downstream.

Every read is by exact packaged name. A name that is not verbatim one of the
entries :func:`list_schema_names` / :func:`list_fixture_paths` returns raises
``ValueError`` before any filesystem access, so a caller-supplied string can
never resolve to a path outside the packaged resource directories.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from importlib.resources.abc import Traversable
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "FIXTURE_EXPECTATIONS",
    "FIXTURE_OUTCOMES",
    "list_fixture_paths",
    "list_schema_names",
    "read_fixture",
    "read_fixture_text",
    "read_schema",
    "read_schema_text",
]

_PACKAGE = "omnivia_core.host_contract.v1"
_SCHEMA_SUFFIX = ".schema.json"

#: Every outcome the governed fixture-expectations table assigns.
FIXTURE_OUTCOMES: Final[tuple[str, ...]] = (
    "schema_valid",
    "schema_valid_display_safe",
    "rejected",
    "envelope_accepted_operation_denied",
    "migration_plan_only",
)

#: The governed expectation for every packaged fixture, transcribed from
#: ``FIXTURE-EXPECTATIONS.md`` at the approved proposal commit.
#:
#: Schema acceptance alone never grants capability authority: the two outcomes
#: that are not plain schema verdicts say so explicitly. A forged request has an
#: acceptable *envelope* and an unacceptable *operation*, and a migration plan is
#: never a Host Contract instance at all.
FIXTURE_EXPECTATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "degradation/optional-capability-missing.json": "schema_valid",
        "denial/permission-denied.json": "schema_valid_display_safe",
        "invalid/development-profile-cloud-target.json": "rejected",
        "invalid/environment-binding-inline-secret.json": "rejected",
        "invalid/forged-shell-context-request.json": "envelope_accepted_operation_denied",
        "invalid/result-envelope-conflict.json": "rejected",
        "invalid/unknown-namespace.json": "rejected",
        "migration/v0-display-context-to-v1.json": "migration_plan_only",
        "valid/development-profile.json": "schema_valid",
        "valid/environment-binding.json": "schema_valid",
        "valid/host-request.json": "schema_valid",
        "valid/host-result.json": "schema_valid",
        "valid/module-contribution.json": "schema_valid",
        "valid/promotion-record.json": "schema_valid",
        "valid/release-package-identity.json": "schema_valid",
        "valid/rollback-record.json": "schema_valid",
        "valid/shell-context.json": "schema_valid",
        "valid/shell-scenario.json": "schema_valid",
        "valid/test-driver-request.json": "schema_valid",
        "valid/test-driver-result.json": "schema_valid",
    }
)


def _schemas_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("resources", "schemas")


def _fixtures_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("resources", "fixtures")


def _require_packaged_name(name: str, allowed: tuple[str, ...], kind: str) -> str:
    """Return ``name`` when it is exactly one of ``allowed``, otherwise raise ``ValueError``.

    Exact membership in the packaged listing is the whole check, and it is
    deliberately the *only* check. Anything that is not a verbatim entry -- an
    absolute path, a name containing ``.`` or ``..``, a percent-encoded or
    otherwise decorated lookalike -- simply is not in ``allowed``, so one
    equality test keeps every read inside the intended resource directory
    without having to enumerate the ways out of it.
    """
    if name in allowed:
        return name
    raise ValueError(f"unknown {kind} {name!r}; packaged {kind}s are {list(allowed)}")


def list_schema_names() -> tuple[str, ...]:
    """Return the packaged schema's base name (without ``.schema.json``), sorted."""
    return tuple(
        sorted(
            entry.name.removesuffix(_SCHEMA_SUFFIX)
            for entry in _schemas_root().iterdir()
            if entry.name.endswith(_SCHEMA_SUFFIX)
        )
    )


def read_schema_text(name: str) -> str:
    """Return the raw JSON text of the packaged schema, by base name.

    ``name`` must be exactly one of the base names :func:`list_schema_names`
    returns; anything else raises ``ValueError`` without touching the
    filesystem.
    """
    _require_packaged_name(name, list_schema_names(), "schema name")
    return _schemas_root().joinpath(f"{name}{_SCHEMA_SUFFIX}").read_text(encoding="utf-8")


def read_schema(name: str) -> dict[str, Any]:
    """Parse the packaged schema document, by base name."""
    document: Any = json.loads(read_schema_text(name))
    if not isinstance(document, dict):
        raise TypeError(f"schema {name!r}: expected a JSON object at the document root")
    return document


def list_fixture_paths() -> tuple[str, ...]:
    """Return every packaged fixture path, ``category/name.json``, sorted.

    Fixtures keep the governed category directories (``valid``, ``invalid``,
    ``denial``, ``degradation``, ``migration``) because the category is part of
    what the approved expectations table says about each file.
    """
    root = _fixtures_root()
    return tuple(
        sorted(
            f"{category.name}/{entry.name}"
            for category in root.iterdir()
            if category.is_dir()
            for entry in category.iterdir()
            if entry.name.endswith(".json")
        )
    )


def read_fixture_text(relative_path: str) -> str:
    """Return the raw JSON text of one packaged fixture, by ``category/name.json``."""
    _require_packaged_name(relative_path, list_fixture_paths(), "fixture path")
    category, _, name = relative_path.partition("/")
    return _fixtures_root().joinpath(category, name).read_text(encoding="utf-8")


def read_fixture(relative_path: str) -> Any:
    """Parse one packaged fixture document, by ``category/name.json``."""
    return json.loads(read_fixture_text(relative_path))
