"""omnivia-core-runtime: skeleton runtime distribution for OmniVia Core.

This package establishes the compile-time dependency boundary defined by
ADR-036: it depends on the public ``omnivia-core`` contracts and has no
operational behavior yet. Do not add storage, service-launch, lease, or
lifecycle behavior in this slice.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
