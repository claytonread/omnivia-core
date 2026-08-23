"""Public Chat Runtime Contract wire contract for OmniVia Core.

The versioned contract itself lives under
:mod:`omnivia_core.chat_contract.v1` (approval
``GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001``); this package deliberately
exposes that version namespace so a caller can depend on
``omnivia_core.chat_contract.v1`` without an extra import step. Nothing is
re-exported at this level: a version boundary should always be named
explicitly, since a future v2 Chat Runtime Contract will live alongside v1
here rather than replacing it.

This is a separate, separately versioned surface from Application Contract v1
under :mod:`omnivia_core.contracts` (ADR-038) and from Host Contract v1 under
:mod:`omnivia_core.host_contract`, which it neither replaces nor changes.

Standard library only. Nothing under ``omnivia_core.chat_contract`` may depend
on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

from . import v1

__all__: list[str] = ["v1"]
