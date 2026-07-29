"""Compatibility facade for provenance source models.

Deprecated: import ``Source`` / ``SourceType`` from
``omnivia_core.provenance.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.provenance.models import Any, Enum, Source, SourceType, annotations
