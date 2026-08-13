"""omnivia-core-cli: the ``omnivia`` executable for OmniVia Core.

One frozen command per invocation, parsed, called on a running Core Service and
reported. The surface is twenty application commands -- one for each operation
of the frozen ``OPERATION_CATALOGUE``, checked at import -- and three service
probes, each reached by exactly two segments with no alias and no abbreviation.

``--installation-state`` (absolute) and ``--workspace-id`` are required on every
command, probes included: nothing here has a default installation, an ambient
home or an environment fallback. Every call is carried by
:class:`omnivia_core_client.ServiceClient` and by nothing else, so this package
discovers no endpoint, reads no descriptor, constructs no transport and launches
no process, and it owns no workspace, lease, lock or database.

Output is the ``result`` document on stdout, or the whole canonical envelope
under ``--json``; an application error is the service's own ``code: message`` on
stderr. Exit codes cover ``FROZEN_ERROR_CODES`` exactly, also checked at import,
with 1 for an error code this build does not recognise.

It keeps the compile-time dependency boundary defined by ADR-036: it depends on
the public ``omnivia-core`` contracts and on ``omnivia-core-client``, and must
never depend on or import ``omnivia_core_runtime`` or ``omnivia_core_mcp``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
