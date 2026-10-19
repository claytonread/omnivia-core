"""Core update channel, trust and operation contract v1.

Read-only access to the packaged canonical schema, the three conformance
corpora -- channel envelopes, operation journals, candidate bundles -- and the
canonicalisation, base64url and signature vectors. The submodule
:mod:`resources` remains importable directly for callers that prefer explicit
provenance.

Standard library only. See the package docstring for why that bound includes
cryptography.
"""

from __future__ import annotations

from . import resources
from .resources import (
    CASE_CATEGORIES,
    VECTORS_NAME,
    list_case_paths,
    list_schema_names,
    read_case,
    read_case_text,
    read_schema,
    read_schema_text,
    read_vectors,
)

__all__: list[str] = [
    "CASE_CATEGORIES",
    "VECTORS_NAME",
    "list_case_paths",
    "list_schema_names",
    "read_case",
    "read_case_text",
    "read_schema",
    "read_schema_text",
    "read_vectors",
    "resources",
]
