"""omnivia-core-cli: skeleton CLI surface distribution for OmniVia Core.

This package establishes the compile-time dependency boundary defined by
ADR-036: it depends on the public ``omnivia-core`` contracts and has no
operational behavior yet. Do not add console entry points or command
behavior in this slice, and do not depend on ``omnivia_core_runtime``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
