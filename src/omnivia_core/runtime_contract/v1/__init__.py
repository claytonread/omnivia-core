"""Trusted runtime payload and resolution contract v1.

Read-only access to the packaged canonical schema, conformance cases and
canonicalisation/signature vectors. The submodule :mod:`resources` remains
importable directly for callers that prefer explicit provenance.

Standard library only. See the package docstring for why that bound includes
cryptography.
"""

from __future__ import annotations

from . import resources
from .resources import (
    list_case_paths,
    list_schema_names,
    read_case,
    read_case_text,
    read_schema,
    read_schema_text,
    read_vectors,
)

__all__: list[str] = [
    "list_case_paths",
    "list_schema_names",
    "read_case",
    "read_case_text",
    "read_schema",
    "read_schema_text",
    "read_vectors",
    "resources",
]
