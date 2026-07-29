"""Public wire contracts for OmniVia Core.

The versioned contract itself lives under :mod:`omnivia_core.contracts.v1`
(ADR-038); this package deliberately exposes that version namespace so a
caller can depend on ``omnivia_core.contracts.v1`` without an extra import
step. Nothing is re-exported at this level: a version boundary should always
be named explicitly, since a future v2 contract will live alongside v1 here
rather than replacing it.

Standard library only. Nothing under ``omnivia_core.contracts`` may depend on
runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

from . import v1

__all__: list[str] = ["v1"]
