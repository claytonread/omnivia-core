"""omnivia-core-cli: the ``omnivia`` CLI distribution for OmniVia Core.

A Core Service client. It discovers a running service, builds contract
envelopes, calls, and reports what it is told; ``workspace show`` is the first
subcommand that calls rather than printing the envelope it would have sent. It
never owns a workspace, holds no lease, takes no lock and opens no database.

It keeps the compile-time dependency boundary defined by ADR-036: it depends on
the public ``omnivia-core`` contracts and on ``omnivia-core-client``, and must
never depend on or import ``omnivia_core_runtime``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
