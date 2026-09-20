"""Read-only access to the packaged Chat Runtime Contract v1 schema and fixtures.

``contracts/chat/v1/{schemas,fixtures}`` is the single checked-in canonical
copy of the exact bytes ``GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001`` binds
(read through Masterdocs tag ``architecture-v1.4.0``); the wheel
force-includes it under ``omnivia_core/chat_contract/v1/resources`` (see
``pyproject.toml``). This module is the only supported way to read that
packaged copy at runtime: callers never construct a resource path themselves,
so the on-disk layout stays free to change without breaking anyone
downstream.

Every read is by exact packaged name. A name that is not verbatim one of the
entries :func:`list_schema_names` / :func:`list_fixture_paths` returns raises
``ValueError`` before any filesystem access, so a caller-supplied string can
never resolve to a path outside the packaged resource directories.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import json
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

__all__ = [
    "list_fixture_paths",
    "list_schema_names",
    "read_fixture",
    "read_fixture_manifest",
    "read_fixture_text",
    "read_schema",
    "read_schema_text",
]

_PACKAGE = "omnivia_core.chat_contract.v1"
_SCHEMA_SUFFIX = ".schema.json"
_FIXTURE_MANIFEST_NAME = "FIXTURE-MANIFEST.json"


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
    """Return every packaged governed-fixture path, ``category/name.json``, sorted.

    The governed categories are ``valid``, ``invalid`` and ``traces``.
    ``FIXTURE-MANIFEST.json`` itself is not a fixture and is not included here
    -- read it with :func:`read_fixture_manifest`.
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


def read_fixture_manifest() -> dict[str, Any]:
    """Parse the packaged ``FIXTURE-MANIFEST.json`` document."""
    text = _fixtures_root().joinpath(_FIXTURE_MANIFEST_NAME).read_text(encoding="utf-8")
    document: Any = json.loads(text)
    if not isinstance(document, dict):
        raise TypeError("FIXTURE-MANIFEST.json: expected a JSON object at the document root")
    return document
