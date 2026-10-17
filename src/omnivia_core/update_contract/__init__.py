"""Public Core update channel, trust and operation contract.

The versioned contract itself lives under :mod:`omnivia_core.update_contract.v1`;
this package exposes that version namespace so a caller can depend on
``omnivia_core.update_contract.v1`` without an extra import step. Nothing is
re-exported at this level: a version boundary should always be named explicitly,
since a future v2 will live alongside v1 here rather than replacing it.

This is the language-neutral half of the update trust seam. The reference
implementations are ``omnivia_core_client.update_trust`` for the channel document
and ``omnivia_core_client.updates_apply`` for the candidate bundle and the
operation journal; a consumer that decides independently whether a Core channel
may be believed -- in another language, in a desktop shell, in a different
product -- consumes the schema, the corpus and the vectors published here and
imports no implementation at all.

Standard library only. Nothing under ``omnivia_core.update_contract`` may depend
on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework --
including any cryptography library. Reading the corpus must never require the
ability to verify a signature.
"""

from __future__ import annotations

from . import v1

__all__: list[str] = ["v1"]
