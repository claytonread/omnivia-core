"""Operational OmniVia Core service and canonical Agent Runtime substrate.

The package implements fenced workspace storage, service lifecycle and transport,
durable application jobs, and the private persistence/command/replay/scheduling/wait
foundation for the public ``omnivia-core`` Agent Runtime contracts.  The ADR-036
dependency direction remains strict: this package depends on ``omnivia-core`` and
the public contract package never imports this operational implementation.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
