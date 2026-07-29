"""omnivia-core: the canonical public OmniVia Core distribution.

This package is the root of the ``omnivia-core`` distribution's compile-time
dependency graph. Sibling distributions (``omnivia-core-runtime``,
``omnivia-core-mcp``, ``omnivia-core-cli``) depend on this package; this
package must never depend on or import any of them.

This module currently exposes only package identity metadata. Public domain
contracts have not been migrated into this distribution yet; the existing
reference implementation continues to live in ``services/omnivia-memory``
during this transitional phase.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
