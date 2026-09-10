"""Read-only access to the packaged trusted-runtime v1 schema and corpus.

``contracts/runtime/v1/{schemas,fixtures}`` is the single checked-in canonical
copy; the wheel force-includes it under
``omnivia_core/runtime_contract/v1/resources`` (see ``pyproject.toml``). This
module is the only supported way to read that packaged copy at runtime: callers
never construct a resource path themselves, so the on-disk layout stays free to
change without breaking anyone downstream.

Every read is by exact packaged name. A name that is not verbatim one of the
entries :func:`list_schema_names` / :func:`list_case_paths` returns raises
``ValueError`` before any filesystem access, so a caller-supplied string can never
resolve to a path outside the packaged resource directories.

**A conformance case carries its own expected verdict.** There is no separate
expectations table to keep in step with the corpus, because the thing a case
asserts is part of the case: ``expected.outcome`` and ``expected.refusal`` travel
with the installation tree they are about, in one document, in one language-neutral
file that a TypeScript verifier reads the same way this one does.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, a validation framework, or a cryptography library.
"""

from __future__ import annotations

import json
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, Final

__all__ = [
    "VECTORS_NAME",
    "list_case_paths",
    "list_schema_names",
    "read_case",
    "read_case_text",
    "read_schema",
    "read_schema_text",
    "read_vectors",
]

_PACKAGE = "omnivia_core.runtime_contract.v1"
_SCHEMA_SUFFIX = ".schema.json"

#: The single vectors document, by its packaged path.
VECTORS_NAME: Final = "vectors/canonicalisation-and-signatures.json"


def _schemas_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("resources", "schemas")


def _fixtures_root() -> Traversable:
    return resources.files(_PACKAGE).joinpath("resources", "fixtures")


def _require_packaged_name(name: str, allowed: tuple[str, ...], kind: str) -> str:
    """Return ``name`` when it is exactly one of ``allowed``, otherwise raise.

    Exact membership in the packaged listing is the whole check, and deliberately
    the only one: anything that is not a verbatim entry -- an absolute path, a name
    containing ``.`` or ``..``, a percent-encoded lookalike -- simply is not in
    ``allowed``, so one equality test keeps every read inside the intended resource
    directory without enumerating the ways out of it.
    """
    if name in allowed:
        return name
    raise ValueError(f"unknown {kind} {name!r}; packaged {kind}s are {list(allowed)}")


def list_schema_names() -> tuple[str, ...]:
    """Return the packaged schema base names (without ``.schema.json``), sorted."""
    return tuple(
        sorted(
            entry.name.removesuffix(_SCHEMA_SUFFIX)
            for entry in _schemas_root().iterdir()
            if entry.name.endswith(_SCHEMA_SUFFIX)
        )
    )


def read_schema_text(name: str) -> str:
    """Return the raw JSON text of the packaged schema, by base name."""
    _require_packaged_name(name, list_schema_names(), "schema name")
    return _schemas_root().joinpath(f"{name}{_SCHEMA_SUFFIX}").read_text(encoding="utf-8")


def read_schema(name: str) -> dict[str, Any]:
    """Parse the packaged schema document, by base name."""
    document: Any = json.loads(read_schema_text(name))
    if not isinstance(document, dict):
        raise TypeError(f"schema {name!r}: expected a JSON object at the document root")
    return document


def list_case_paths() -> tuple[str, ...]:
    """Return every packaged corpus path, ``category/name.json``, sorted.

    The categories are ``valid``, ``invalid`` and ``vectors``. The first two are the
    conformance cases and the third holds the canonicalisation and signature
    vectors, which are not cases and carry no installation tree.
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


def read_case_text(relative_path: str) -> str:
    """Return the raw JSON text of one packaged document, by ``category/name.json``."""
    _require_packaged_name(relative_path, list_case_paths(), "case path")
    category, _, name = relative_path.partition("/")
    return _fixtures_root().joinpath(category, name).read_text(encoding="utf-8")


def read_case(relative_path: str) -> dict[str, Any]:
    """Parse one packaged document, by ``category/name.json``."""
    document: Any = json.loads(read_case_text(relative_path))
    if not isinstance(document, dict):
        raise TypeError(f"{relative_path}: expected a JSON object at the document root")
    return document


def read_vectors() -> dict[str, Any]:
    """Parse the canonicalisation and signature vectors document."""
    return read_case(VECTORS_NAME)
