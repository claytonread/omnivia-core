"""omnivia-core-mcp: the Model Context Protocol server for OmniVia Core.

A stdio MCP server that gives an AI host read-only access to one local OmniVia
Core workspace, over the official Model Context Protocol Python SDK v2.

**Three modules, three separable decisions:**

- :mod:`~omnivia_core_mcp.manifest` -- the curated, versioned exposure allow-list
  (R004-06) and the tool schemas projected from the public operation contracts.
  Adding a line here is the whole act of exposing an operation; nothing
  enumerates the catalogue.
- :mod:`~omnivia_core_mcp.managed_start` -- resolving the installation and asking
  `omnivia-core-service --managed-start` to make a service exist (R004-07). A
  subprocess and a JSON document; no second launcher.
- :mod:`~omnivia_core_mcp.server` -- the SDK server, the stdio session, and the
  seam where a concrete `ClientTransport` will be constructed.

**The dependency boundary (ADR-036, R004-05).** This distribution depends on the
public ``omnivia-core`` contracts, on ``omnivia-core-client`` for service calls,
and on the official ``mcp`` SDK. It does not depend on ``omnivia-core-cli``, on
``omnivia_core_runtime``, on any Desktop or Platform package, or on a database
implementation -- and it owns no workspace lease and opens no storage.

**What this server will not do**, by construction rather than by policy: create a
workspace, stop a service it started, expose a lifecycle or administrative
operation, accept a caller-supplied filesystem path, or call anything absent from
the manifest.

**What is not finished.** No concrete local ``ClientTransport`` exists in
``omnivia-core-client`` yet, so tools list and describe themselves correctly but a
call answers with a stated, non-fatal refusal. See
:mod:`omnivia_core_mcp.server` for the single seam that closes when one lands.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
