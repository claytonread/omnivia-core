"""OmniVia Chat Runtime Contract v1 (approval GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001).

Re-exports the generated registries/metadata (:mod:`.generated`), packaged
resource access (:mod:`.resources`) and the bounded W2/F2 codec
(:mod:`.codec`) at this version's top level, so a caller depends on
``omnivia_core.chat_contract.v1`` without reaching into a submodule by name.

Standard library only.
"""

from __future__ import annotations

from omnivia_core.chat_contract.v1 import codec, generated, resources
from omnivia_core.chat_contract.v1.codec import (
    ChatContractDecodeError,
    ChatEvent,
    CommandError,
    CommandResultEnvelope,
    ProviderInvocationRecord,
    ProviderInvocationRequest,
    ResnapshotResponse,
    UnsupportedProtocolVersionError,
    negotiate_protocol_version,
    to_canonical_json,
    to_canonical_json_document,
)
from omnivia_core.chat_contract.v1.generated import (
    APPROVAL_ID,
    CHAT_COMMAND_NAMES,
    CONTRACT_VERSION,
    PROTOCOL_MAJOR,
    PROTOCOL_VERSION,
)

__all__ = [
    "APPROVAL_ID",
    "CHAT_COMMAND_NAMES",
    "CONTRACT_VERSION",
    "PROTOCOL_MAJOR",
    "PROTOCOL_VERSION",
    "ChatContractDecodeError",
    "ChatEvent",
    "CommandError",
    "CommandResultEnvelope",
    "ProviderInvocationRecord",
    "ProviderInvocationRequest",
    "ResnapshotResponse",
    "UnsupportedProtocolVersionError",
    "codec",
    "generated",
    "negotiate_protocol_version",
    "resources",
    "to_canonical_json",
    "to_canonical_json_document",
]
