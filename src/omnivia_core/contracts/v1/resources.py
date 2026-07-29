"""Read-only access to the packaged Application Contract v1 schemas and fixtures.

``contracts/application/v1/{schemas,fixtures}`` is the single checked-in
canonical copy (ADR-038); the wheel force-includes it under
``omnivia_core/contracts/v1/resources`` (see ``pyproject.toml``). This module
is the only supported way to read that packaged copy at runtime: callers
never construct a resource path themselves, so the on-disk layout stays free
to change without breaking anyone downstream.

Every read is by exact packaged name. A name that is not verbatim one of the
entries ``list_schema_names`` / ``list_fixture_files`` returns raises
``ValueError`` before any filesystem access, so a caller-supplied string can
never resolve to a path outside the packaged resource directories.

Standard library only. Nothing here may depend on runtime, storage, HTTP,
MCP, CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import json
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

__all__ = [
    "list_fixture_files",
    "list_schema_names",
    "read_fixture",
    "read_fixture_manifest",
    "read_fixture_text",
    "read_schema",
    "read_schema_text",
]

_PACKAGE = "omnivia_core.contracts.v1"
_SCHEMA_SUFFIX = ".schema.json"
_MANIFEST_FILE = "manifest.json"


def _schemas_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("resources", "schemas")


def _fixtures_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("resources", "fixtures")


def _require_packaged_name(name: str, allowed: tuple[str, ...], kind: str) -> str:
    """Return ``name`` when it is exactly one of ``allowed``, otherwise raise ``ValueError``.

    Exact membership in the packaged listing is the whole check, and it is
    deliberately the *only* check. Anything that is not a verbatim entry --
    an absolute path, a name containing a path separator, ``.`` or ``..``, a
    percent-encoded or otherwise decorated lookalike -- simply is not in
    ``allowed``, so one equality test keeps every read inside the intended
    resource directory without having to enumerate the ways out of it.
    """
    if name in allowed:
        return name
    raise ValueError(f"unknown {kind} {name!r}; packaged {kind}s are {list(allowed)}")


def list_schema_names() -> tuple[str, ...]:
    """Return the packaged schemas' base names (without ``.schema.json``), sorted."""
    return tuple(
        sorted(
            entry.name.removesuffix(_SCHEMA_SUFFIX)
            for entry in _schemas_root().iterdir()
            if entry.name.endswith(_SCHEMA_SUFFIX)
        )
    )


def read_schema_text(name: str) -> str:
    """Return the raw JSON text of one packaged schema, by base name.

    ``name`` must be exactly one of the base names :func:`list_schema_names`
    returns; anything else raises ``ValueError`` without touching the
    filesystem.
    """
    _require_packaged_name(name, list_schema_names(), "schema name")
    return _schemas_root().joinpath(f"{name}{_SCHEMA_SUFFIX}").read_text(encoding="utf-8")


def read_schema(name: str) -> dict[str, Any]:
    """Parse one packaged schema document, by base name.

    Reads through :func:`read_schema_text`, so an unknown name is rejected the
    same way.
    """
    document: Any = json.loads(read_schema_text(name))
    if not isinstance(document, dict):
        raise TypeError(f"schema {name!r}: expected a JSON object at the document root")
    return document


def list_fixture_files() -> tuple[str, ...]:
    """Return every packaged fixture file name, including the manifest, sorted."""
    return tuple(sorted(entry.name for entry in _fixtures_root().iterdir() if entry.name.endswith(".json")))


def read_fixture_text(file_name: str) -> str:
    """Return the raw JSON text of one packaged fixture file, by file name.

    ``file_name`` must be exactly one of the names :func:`list_fixture_files`
    returns; anything else raises ``ValueError`` without touching the
    filesystem.
    """
    _require_packaged_name(file_name, list_fixture_files(), "fixture file name")
    return _fixtures_root().joinpath(file_name).read_text(encoding="utf-8")


def read_fixture(file_name: str) -> Any:
    """Parse one packaged fixture document, by file name.

    Reads through :func:`read_fixture_text`, so an unknown file name is
    rejected the same way.
    """
    return json.loads(read_fixture_text(file_name))


def read_fixture_manifest() -> dict[str, Any]:
    """Parse the packaged fixture manifest."""
    manifest: Any = read_fixture(_MANIFEST_FILE)
    if not isinstance(manifest, dict):
        raise TypeError(f"{_MANIFEST_FILE}: expected a JSON object at the document root")
    return manifest
