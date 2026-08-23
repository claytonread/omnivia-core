# GENERATED FILE - DO NOT EDIT.
#
# Source of truth:
#   contracts/chat/v1/schemas/*.schema.json (13 files)
#   contracts/chat/v1/fixtures/**           (159 files)
# Governed by:
#   Approval GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001;
#   proposal commit 04c0b2f768b8a74c515936e548c4a28fa4af514d;
#   proposal content-set inventory SHA-256 521893fefc9d33f5507e5bde84be12713359fe1c4ec041164280096b797e2bf2;
#   fixture manifest SHA-256 7936bf32da76a66c7d479217588f56c4af20b0ed01001330dd6bc2a6d1329a54;
#   effective Architecture release tag architecture-v1.4.0;
#   effective payload commit eb14159d73c8d9339cfeb347f8de61bd67497974.
# Generator:
#   scripts/generate-chat-contract.py
#
# Regenerate: python scripts/generate-chat-contract.py
# Verify:     python scripts/generate-chat-contract.py --check
#
# Approval/provenance constants, version constants, schema names/IDs, the
# resource inventory digest, the closed Chat command registry, the closed
# vocabularies the bounded W2/F2 codec decodes, and the schema-derived
# validation metadata that codec's strict emission is checked against, for the
# OmniVia Chat Runtime Contract v1. Standard library only: this module must
# never depend on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a
# validation framework. This is a separately versioned public family from
# Application Contract v1 (:mod:`omnivia_core.contracts.v1`) and changes
# nothing there.

"""Generated Chat Runtime Contract v1 registries and metadata."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "ADDITIVE_DECODE_VOCABULARIES",
    "APPROVAL_ID",
    "CHAT_COMMAND_NAMES",
    "CHAT_EVENT_FIELDS",
    "CHAT_EVENT_REQUIRED_FIELDS",
    "CHAT_EVENT_SCHEMA_REFS",
    "CHAT_EVENT_TYPES",
    "COMMAND_RESULT_STATUSES",
    "CONTRACT_VERSION",
    "EFFECTIVE_ARCHITECTURE_TAG",
    "EFFECTIVE_PAYLOAD_COMMIT",
    "ERROR_CODES",
    "F2A_ESTIMATED_COST_PRICING_SOURCES",
    "F2A_FINISH_REASONS",
    "F2A_INVOCATION_LIFECYCLE_STATES",
    "F2A_PROVIDER_ERROR_CODES",
    "F2A_PROVIDER_EVENT_TYPES",
    "F2A_RECONCILIATION_STATES",
    "F2A_ROUTE_DECISIONS",
    "FIXTURE_MANIFEST_SHA256",
    "PROPOSAL_COMMIT",
    "PROPOSAL_CONTENT_SET_INVENTORY_SHA256",
    "PROTOCOL_MAJOR",
    "PROTOCOL_VERSION",
    "RECORD_VALIDATION_REFS",
    "RESNAPSHOT_REASONS",
    "RESOURCE_FIXTURE_TREE_COUNT",
    "RESOURCE_INVENTORY_DIGEST",
    "RESOURCE_SCHEMA_COUNT",
    "SCHEMA_IDS",
    "SCHEMA_NAMES",
    "VALIDATION_KEYWORDS",
    "VALIDATION_SCHEMAS",
]

# --------------------------------------------------------------------------
# Approval, provenance and version constants
# --------------------------------------------------------------------------

#: Approval identifier binding this exact, unchanged copy to its governance decision.
APPROVAL_ID: Final[str] = 'GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001'
#: The proposal-stage Git commit this copy's bytes were taken from.
PROPOSAL_COMMIT: Final[str] = '04c0b2f768b8a74c515936e548c4a28fa4af514d'
#: SHA-256 of the proposal's own ``PROPOSAL-SHA256SUMS.txt`` inventory file.
PROPOSAL_CONTENT_SET_INVENTORY_SHA256: Final[str] = '521893fefc9d33f5507e5bde84be12713359fe1c4ec041164280096b797e2bf2'
#: SHA-256 of ``fixtures/FIXTURE-MANIFEST.json`` at the approved bytes.
FIXTURE_MANIFEST_SHA256: Final[str] = '7936bf32da76a66c7d479217588f56c4af20b0ed01001330dd6bc2a6d1329a54'
#: The Masterdocs Git tag this copy was read through.
EFFECTIVE_ARCHITECTURE_TAG: Final[str] = 'architecture-v1.4.0'
#: The exact Masterdocs payload commit ``EFFECTIVE_ARCHITECTURE_TAG`` resolves to.
EFFECTIVE_PAYLOAD_COMMIT: Final[str] = 'eb14159d73c8d9339cfeb347f8de61bd67497974'
#: The contract candidate version the approved catalogue encodes.
CONTRACT_VERSION: Final[str] = '1.0.0-rc.1'
#: The one wire protocol version this bundle supports. A v1 decoder rejects an
#: unsupported major and never silently downgrades
#: (CHAT-RUNTIME-CONTRACT-V1-COMPATIBILITY-AND-MIGRATION.md §1, §3).
PROTOCOL_VERSION: Final[str] = '1.0'
#: The major component of :data:`PROTOCOL_VERSION`, the compatibility boundary.
PROTOCOL_MAJOR: Final[str] = '1'

#: Exact packaged schema file count.
RESOURCE_SCHEMA_COUNT: Final[int] = 13
#: Exact packaged fixture-tree file count (FIXTURE-MANIFEST.json plus 158 governed fixtures).
RESOURCE_FIXTURE_TREE_COUNT: Final[int] = 159
#: Pinned SHA-256 over every relative resource path and byte payload under
#: ``contracts/chat/v1``; see ``scripts/generate-chat-contract.py``
#: ``compute_resource_inventory_digest``.
RESOURCE_INVENTORY_DIGEST: Final[str] = '9a633eb9cdb81e6f586f904c493ac405a535d8da2198561358e940ecb60b090a'

#: The 13 packaged schema base names (without .schema.json), sorted.
SCHEMA_NAMES: Final[tuple[str, ...]] = (
    'amendment',
    'branch',
    'bridge',
    'commands',
    'common',
    'events',
    'generation',
    'graph',
    'provider',
    'queries',
    'references',
    'sharing',
    'viewstate',
)

#: Schema base name -> its declared $id.
SCHEMA_IDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        'amendment': 'https://contracts.omnivia.dev/chat/v1/amendment.schema.json',
        'branch': 'https://contracts.omnivia.dev/chat/v1/branch.schema.json',
        'bridge': 'https://contracts.omnivia.dev/chat/v1/bridge.schema.json',
        'commands': 'https://contracts.omnivia.dev/chat/v1/commands.schema.json',
        'common': 'https://contracts.omnivia.dev/chat/v1/common.schema.json',
        'events': 'https://contracts.omnivia.dev/chat/v1/events.schema.json',
        'generation': 'https://contracts.omnivia.dev/chat/v1/generation.schema.json',
        'graph': 'https://contracts.omnivia.dev/chat/v1/graph.schema.json',
        'provider': 'https://contracts.omnivia.dev/chat/v1/provider.schema.json',
        'queries': 'https://contracts.omnivia.dev/chat/v1/queries.schema.json',
        'references': 'https://contracts.omnivia.dev/chat/v1/references.schema.json',
        'sharing': 'https://contracts.omnivia.dev/chat/v1/sharing.schema.json',
        'viewstate': 'https://contracts.omnivia.dev/chat/v1/viewstate.schema.json',
    }
)

#: The closed v1 initial Chat command registry (commands.schema.json ChatCommandName), 30 members.
CHAT_COMMAND_NAMES: Final[tuple[str, ...]] = (
    'CreateConversation',
    'CreateScopedConversation',
    'RenameConversation',
    'ArchiveConversation',
    'DeleteConversation',
    'SaveDraft',
    'DiscardDraft',
    'EnqueueMessage',
    'UpdateQueuedSubmission',
    'ReorderQueuedSubmission',
    'CancelQueuedSubmission',
    'SubmitMessage',
    'StopGeneration',
    'RetryGeneration',
    'RegenerateResponse',
    'CommitMessageAmendment',
    'SelectConversationPath',
    'SetDefaultConversationPath',
    'StageAttachment',
    'RemoveStagedAttachment',
    'RespondToWorkRequest',
    'SubmitSteeringInput',
    'InvokeTool',
    'InvokeApp',
    'StartWorkflow',
    'StartAgentRun',
    'ReviseArtifact',
    'PlaceArtifactVersion',
    'CreateConversationShare',
    'ExportConversation',
)

#: The closed v1 durable Chat transport event-type vocabulary (common.schema.json ChatEventType), 15 members. Additive-decode point.
CHAT_EVENT_TYPES: Final[tuple[str, ...]] = (
    'chat.conversation.created',
    'chat.message.committed',
    'chat.message.derivation_recorded',
    'chat.branch.created',
    'chat.branch.head_advanced',
    'chat.branch.selected',
    'chat.branch.archived',
    'chat.message.tombstoned',
    'chat.generation.queued',
    'chat.generation.started',
    'chat.generation.succeeded',
    'chat.generation.failed',
    'chat.generation.cancelled',
    'chat.share.created',
    'chat.export.created',
)

#: The closed status vocabulary for commands.schema.json CommandResultEnvelope.status, 4 members.
COMMAND_RESULT_STATUSES: Final[tuple[str, ...]] = (
    'accepted',
    'completed',
    'rejected',
    'conflict',
)

#: The closed v1 shared error-code registry (common.schema.json ErrorCode), 24 members.
ERROR_CODES: Final[tuple[str, ...]] = (
    'authentication_required',
    'authorization_denied',
    'workspace_mismatch',
    'conversation_not_found',
    'message_not_found',
    'branch_not_found',
    'not_eligible_for_amendment',
    'source_content_hash_mismatch',
    'idempotency_conflict',
    'idempotent_replay',
    'stale_expected_version',
    'invalid_derivation',
    'derivation_cycle_rejected',
    'reference_revalidation_failed',
    'cursor_unknown_or_expired',
    'unauthorized_cursor',
    'graph_revision_unavailable',
    'resnapshot_required',
    'share_or_export_revoked',
    'share_or_export_expired',
    'rate_limited',
    'deadline_exceeded',
    'internal_recoverable',
    'internal_non_recoverable',
)

#: The closed EstimatedCost.pricingSource vocabulary, 3 members.
F2A_ESTIMATED_COST_PRICING_SOURCES: Final[tuple[str, ...]] = (
    'provider_reported',
    'omnivia_catalogue',
    'estimate',
)

#: The closed v1 F2a normalised finish-reason vocabulary (provider.schema.json FinishReason), 7 members.
F2A_FINISH_REASONS: Final[tuple[str, ...]] = (
    'stop',
    'length',
    'tool-calls',
    'content-filter',
    'error',
    'cancelled',
    'unknown',
)

#: The closed v1 ProviderInvocationRecord lifecycle vocabulary, 6 members.
F2A_INVOCATION_LIFECYCLE_STATES: Final[tuple[str, ...]] = (
    'requested',
    'in_progress',
    'succeeded',
    'failed',
    'cancelled',
    'indeterminate',
)

#: The closed v1 F2a provider error-code vocabulary (provider.schema.json ProviderErrorCode), 16 members.
F2A_PROVIDER_ERROR_CODES: Final[tuple[str, ...]] = (
    'authentication',
    'permission',
    'model-not-found',
    'rate-limited',
    'quota-or-budget',
    'invalid-request',
    'context-window-exceeded',
    'content-policy',
    'endpoint-policy',
    'timeout',
    'cancelled',
    'transport',
    'provider-unavailable',
    'malformed-response',
    'unsupported-operation',
    'unknown',
)

#: The closed v1 F2a provider event-discriminator vocabulary (provider.schema.json ProviderEventType), 20 members. Additive-decode point.
F2A_PROVIDER_EVENT_TYPES: Final[tuple[str, ...]] = (
    'stream-start',
    'text-start',
    'text-delta',
    'text-end',
    'reasoning-start',
    'reasoning-delta',
    'reasoning-end',
    'tool-input-start',
    'tool-input-delta',
    'tool-input-end',
    'tool-call',
    'tool-result',
    'tool-approval-request',
    'structured-output',
    'file',
    'source',
    'warning',
    'provider-metadata',
    'finish',
    'error',
)

#: The closed v1 reconciliation-state vocabulary, 3 members.
F2A_RECONCILIATION_STATES: Final[tuple[str, ...]] = (
    'reconciled',
    'pending_reconciliation',
    'unreconciled',
)

#: The closed RouteEvidence.routeDecision vocabulary, 3 members.
F2A_ROUTE_DECISIONS: Final[tuple[str, ...]] = (
    'configured',
    'same_route_retry',
    'fallback',
)

#: The closed reason vocabulary for events.schema.json ResnapshotResponse.reason, 4 members.
RESNAPSHOT_REASONS: Final[tuple[str, ...]] = (
    'cursor_unknown_or_expired',
    'unauthorized_cursor',
    'gap_detected',
    'unrecognised_event_type',
)

#: Exact ChatEventType -> the closed field set events.schema.json allows for that event type (its $def is additionalProperties: false). Strict emission of a known event may name no other field.
CHAT_EVENT_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        'chat.branch.archived': (
            'branchId',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.branch.created': (
            'branchId',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'forkParentMessageId',
            'forkSourceMessageId',
            'graphRevision',
            'occurredAt',
            'originKind',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.branch.head_advanced': (
            'branchId',
            'cause',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'headVersion',
            'newHeadMessageId',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.branch.selected': (
            'actorId',
            'branchId',
            'conversationId',
            'cursor',
            'deviceId',
            'eventId',
            'eventType',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.conversation.created': (
            'conversationId',
            'conversationSequence',
            'createdBy',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.export.created': (
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'exportId',
            'graphRevision',
            'occurredAt',
            'schemaVersion',
            'scope',
            'workspaceId',
        ),
        'chat.generation.cancelled': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationAttemptId',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'resultMessageId',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.failed': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationAttemptId',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'resultMessageId',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.queued': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationAttemptId',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'resultMessageId',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.started': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationAttemptId',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'resultMessageId',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.succeeded': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationAttemptId',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'resultMessageId',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.message.committed': (
            'branchId',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'messageId',
            'occurredAt',
            'parentMessageId',
            'role',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.message.derivation_recorded': (
            'conversationId',
            'conversationSequence',
            'cursor',
            'derivedMessageId',
            'eventId',
            'eventType',
            'graphRevision',
            'kind',
            'occurredAt',
            'schemaVersion',
            'sourceMessageId',
            'workspaceId',
        ),
        'chat.message.tombstoned': (
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'messageId',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.share.created': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'headMessageId',
            'occurredAt',
            'schemaVersion',
            'shareId',
            'workspaceId',
        ),
    }
)

#: Exact ChatEventType -> the field set events.schema.json requires for that event type. Strict emission of a known event must carry them all.
CHAT_EVENT_REQUIRED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        'chat.branch.archived': (
            'branchId',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.branch.created': (
            'branchId',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'occurredAt',
            'originKind',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.branch.head_advanced': (
            'branchId',
            'cause',
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'headVersion',
            'newHeadMessageId',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.branch.selected': (
            'actorId',
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.conversation.created': (
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.export.created': (
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'exportId',
            'graphRevision',
            'occurredAt',
            'schemaVersion',
            'scope',
            'workspaceId',
        ),
        'chat.generation.cancelled': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.failed': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.queued': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.started': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.generation.succeeded': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'generationEventSequence',
            'generationJobId',
            'occurredAt',
            'schemaVersion',
            'triggerMessageId',
            'workspaceId',
        ),
        'chat.message.committed': (
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'messageId',
            'occurredAt',
            'role',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.message.derivation_recorded': (
            'conversationId',
            'conversationSequence',
            'cursor',
            'derivedMessageId',
            'eventId',
            'eventType',
            'graphRevision',
            'kind',
            'occurredAt',
            'schemaVersion',
            'sourceMessageId',
            'workspaceId',
        ),
        'chat.message.tombstoned': (
            'conversationId',
            'conversationSequence',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'messageId',
            'occurredAt',
            'schemaVersion',
            'workspaceId',
        ),
        'chat.share.created': (
            'branchId',
            'conversationId',
            'cursor',
            'eventId',
            'eventType',
            'graphRevision',
            'headMessageId',
            'occurredAt',
            'schemaVersion',
            'shareId',
            'workspaceId',
        ),
    }
)

#: Exact ChatEventType -> the VALIDATION_SCHEMAS key of the one closed events.schema.json branch an emitted event of that type must satisfy.
CHAT_EVENT_SCHEMA_REFS: Final[Mapping[str, str]] = MappingProxyType(
    {
        'chat.branch.archived': 'events.schema.json#/$defs/BranchArchivedEvent',
        'chat.branch.created': 'events.schema.json#/$defs/BranchCreatedEvent',
        'chat.branch.head_advanced': 'events.schema.json#/$defs/BranchHeadAdvancedEvent',
        'chat.branch.selected': 'events.schema.json#/$defs/BranchSelectedEvent',
        'chat.conversation.created': 'events.schema.json#/$defs/ConversationCreatedEvent',
        'chat.export.created': 'events.schema.json#/$defs/ExportCreatedEvent',
        'chat.generation.cancelled': 'events.schema.json#/$defs/GenerationEvent',
        'chat.generation.failed': 'events.schema.json#/$defs/GenerationEvent',
        'chat.generation.queued': 'events.schema.json#/$defs/GenerationEvent',
        'chat.generation.started': 'events.schema.json#/$defs/GenerationEvent',
        'chat.generation.succeeded': 'events.schema.json#/$defs/GenerationEvent',
        'chat.message.committed': 'events.schema.json#/$defs/MessageCommittedEvent',
        'chat.message.derivation_recorded': 'events.schema.json#/$defs/MessageDerivationRecordedEvent',
        'chat.message.tombstoned': 'events.schema.json#/$defs/MessageTombstonedEvent',
        'chat.share.created': 'events.schema.json#/$defs/ShareCreatedEvent',
    }
)

#: The closed vocabularies where an unrecognised member is a compatible
#: minor extension a v1 decoder tolerates (safe diagnostic, then resnapshot)
#: rather than a hard decode failure
#: (CHAT-RUNTIME-CONTRACT-V1-COMPATIBILITY-AND-MIGRATION.md §3;
#: CHAT-RUNTIME-CONTRACT-V1-FREEZE.md §9 rule 6, §11 rule 6).
ADDITIVE_DECODE_VOCABULARIES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "ChatEventType": CHAT_EVENT_TYPES,
        "ProviderEventType": F2A_PROVIDER_EVENT_TYPES,
    }
)


#: Bounded codec record class name -> the VALIDATION_SCHEMAS key of the exact canonical subschema its emitted document must satisfy.
RECORD_VALIDATION_REFS: Final[Mapping[str, str]] = MappingProxyType(
    {
        'ChatEvent': 'events.schema.json#/$defs/ChatEvent',
        'CommandError': 'commands.schema.json#/$defs/CommandResultEnvelope/properties/error',
        'CommandResultEnvelope': 'commands.schema.json#/$defs/CommandResultEnvelope',
        'ProviderInvocationRecord': 'provider.schema.json#/$defs/ProviderInvocationRecord',
        'ProviderInvocationRequest': 'provider.schema.json#/$defs/ProviderInvocationRequest',
        'ResnapshotResponse': 'events.schema.json#/$defs/ResnapshotResponse',
    }
)

#: Every JSON Schema assertion keyword VALIDATION_SCHEMAS may contain. The runtime validator asserts it implements all of them at import, so a keyword added here without an implementation fails closed.
VALIDATION_KEYWORDS: Final[tuple[str, ...]] = (
    '$ref',
    'additionalProperties',
    'allOf',
    'anyOf',
    'const',
    'else',
    'enum',
    'format',
    'if',
    'items',
    'maxItems',
    'maxLength',
    'maxProperties',
    'maximum',
    'minItems',
    'minLength',
    'minProperties',
    'minimum',
    'not',
    'oneOf',
    'pattern',
    'properties',
    'propertyNames',
    'required',
    'then',
    'type',
    'uniqueItems',
)

#: The transitive $ref closure of the six bounded codec record roots, read
#: from the approved contracts/chat/v1/schemas bytes with the annotation-only
#: keywords dropped and every $ref rewritten to a flat '<file>#<pointer>' key
#: of this same mapping. This is the sole authority the codec's strict
#: emission is checked against; nothing here is transcribed by hand.
VALIDATION_SCHEMAS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        'commands.schema.json#/$defs/CommandResultEnvelope': {
            'additionalProperties': False,
            'else': {
                'else': {
                    'not': {
                        'required': [
                            'error',
                        ],
                    },
                },
                'if': {
                    'properties': {
                        'status': {
                            'const': 'conflict',
                        },
                    },
                    'required': [
                        'status',
                    ],
                },
                'then': {},
            },
            'if': {
                'properties': {
                    'status': {
                        'const': 'rejected',
                    },
                },
                'required': [
                    'status',
                ],
            },
            'properties': {
                'commandId': {
                    '$ref': 'common.schema.json#/$defs/CommandId',
                },
                'currentVersion': {
                    '$ref': 'common.schema.json#/$defs/ResourceVersion',
                },
                'error': {
                    'additionalProperties': False,
                    'properties': {
                        'code': {
                            '$ref': 'common.schema.json#/$defs/ErrorCode',
                        },
                        'message': {
                            'maxLength': 2048,
                            'minLength': 1,
                            'type': 'string',
                        },
                    },
                    'required': [
                        'code',
                        'message',
                    ],
                    'type': 'object',
                },
                'resultRef': {
                    'maxLength': 256,
                    'minLength': 1,
                    'type': 'string',
                },
                'status': {
                    'enum': [
                        'accepted',
                        'completed',
                        'rejected',
                        'conflict',
                    ],
                },
            },
            'required': [
                'commandId',
                'status',
            ],
            'then': {
                'required': [
                    'error',
                ],
            },
            'type': 'object',
        },
        'commands.schema.json#/$defs/CommandResultEnvelope/properties/error': {
            'additionalProperties': False,
            'properties': {
                'code': {
                    '$ref': 'common.schema.json#/$defs/ErrorCode',
                },
                'message': {
                    'maxLength': 2048,
                    'minLength': 1,
                    'type': 'string',
                },
            },
            'required': [
                'code',
                'message',
            ],
            'type': 'object',
        },
        'common.schema.json#/$defs/ActorId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/AttachmentReference': {
            'additionalProperties': False,
            'properties': {
                'referenceId': {
                    '$ref': 'common.schema.json#/$defs/AttachmentReferenceId',
                },
                'sourceRevision': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
            },
            'required': [
                'referenceId',
            ],
            'type': 'object',
        },
        'common.schema.json#/$defs/AttachmentReferenceId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/BranchHeadCause': {
            'enum': [
                'branch_created',
                'user_message_appended',
                'assistant_message_materialised',
                'human_input_appended',
                'imported',
                'recovered_projection',
            ],
        },
        'common.schema.json#/$defs/BranchId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/BranchOriginKind': {
            'enum': [
                'original',
                'message_amendment',
                'assistant_regeneration',
                'explicit_fork',
                'import',
            ],
        },
        'common.schema.json#/$defs/CausationId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/CommandId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ConversationId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ConversationSequence': {
            'maximum': 9007199254740991,
            'minimum': 1,
            'type': 'integer',
        },
        'common.schema.json#/$defs/CorrelationId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/DerivationKind': {
            'enum': [
                'amendment',
                'regeneration',
                'reuse',
                'imported_revision',
            ],
        },
        'common.schema.json#/$defs/DeviceId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ErrorCode': {
            'enum': [
                'authentication_required',
                'authorization_denied',
                'workspace_mismatch',
                'conversation_not_found',
                'message_not_found',
                'branch_not_found',
                'not_eligible_for_amendment',
                'source_content_hash_mismatch',
                'idempotency_conflict',
                'idempotent_replay',
                'stale_expected_version',
                'invalid_derivation',
                'derivation_cycle_rejected',
                'reference_revalidation_failed',
                'cursor_unknown_or_expired',
                'unauthorized_cursor',
                'graph_revision_unavailable',
                'resnapshot_required',
                'share_or_export_revoked',
                'share_or_export_expired',
                'rate_limited',
                'deadline_exceeded',
                'internal_recoverable',
                'internal_non_recoverable',
            ],
        },
        'common.schema.json#/$defs/EvidenceReference': {
            'additionalProperties': False,
            'properties': {
                'digest': {
                    'pattern': '^[a-f0-9]{64}$(?![\\s\\S])',
                    'type': 'string',
                },
                'evidenceId': {
                    '$ref': 'common.schema.json#/$defs/EvidenceReferenceId',
                },
            },
            'required': [
                'evidenceId',
            ],
            'type': 'object',
        },
        'common.schema.json#/$defs/EvidenceReferenceId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ExportId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ExportScope': {
            'enum': [
                'selected_branch_snapshot',
                'all_accessible_branches_snapshot',
            ],
        },
        'common.schema.json#/$defs/GenerationAttemptId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/GenerationEventSequence': {
            'maximum': 9007199254740991,
            'minimum': 1,
            'type': 'integer',
        },
        'common.schema.json#/$defs/GenerationJobId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/GraphRevision': {
            'maximum': 9007199254740991,
            'minimum': 0,
            'type': 'integer',
        },
        'common.schema.json#/$defs/HeadVersion': {
            'maximum': 9007199254740991,
            'minimum': 1,
            'type': 'integer',
        },
        'common.schema.json#/$defs/IdempotencyKey': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/MessageId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/MessageRole': {
            'enum': [
                'user',
                'assistant',
                'system',
                'tool',
            ],
        },
        'common.schema.json#/$defs/OpaqueCursor': {
            'maxLength': 4096,
            'minLength': 1,
            'type': 'string',
        },
        'common.schema.json#/$defs/ProviderAttemptId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ProviderConnectionId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ProviderInvocationId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ProviderModelId': {
            'maxLength': 256,
            'minLength': 1,
            'pattern': '^(?!.*://)[A-Za-z0-9][A-Za-z0-9/._:@-]*$(?![\\s\\S])',
            'type': 'string',
        },
        'common.schema.json#/$defs/ProviderPartId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/ResourceVersion': {
            'maximum': 9007199254740991,
            'minimum': 0,
            'type': 'integer',
        },
        'common.schema.json#/$defs/SchemaVersion': {
            'maximum': 1000000,
            'minimum': 1,
            'type': 'integer',
        },
        'common.schema.json#/$defs/ShareId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/Timestamp': {
            'format': 'date-time',
            'type': 'string',
        },
        'common.schema.json#/$defs/TransportEventId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/WorkspaceId': {
            '$ref': 'common.schema.json#/$defs/WorkspaceScopedId',
        },
        'common.schema.json#/$defs/WorkspaceScopedId': {
            'maxLength': 128,
            'minLength': 1,
            'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$(?![\\s\\S])',
            'type': 'string',
        },
        'events.schema.json#/$defs/BranchArchivedEvent': {
            'additionalProperties': False,
            'properties': {
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.branch.archived',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'branchId',
                'conversationId',
                'conversationSequence',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'occurredAt',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/BranchCreatedEvent': {
            'additionalProperties': False,
            'properties': {
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.branch.created',
                },
                'forkParentMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'forkSourceMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'originKind': {
                    '$ref': 'common.schema.json#/$defs/BranchOriginKind',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'branchId',
                'conversationId',
                'conversationSequence',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'occurredAt',
                'originKind',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/BranchHeadAdvancedEvent': {
            'additionalProperties': False,
            'properties': {
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'cause': {
                    '$ref': 'common.schema.json#/$defs/BranchHeadCause',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.branch.head_advanced',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'headVersion': {
                    '$ref': 'common.schema.json#/$defs/HeadVersion',
                },
                'newHeadMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'branchId',
                'cause',
                'conversationId',
                'conversationSequence',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'headVersion',
                'newHeadMessageId',
                'occurredAt',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/BranchSelectedEvent': {
            'additionalProperties': False,
            'properties': {
                'actorId': {
                    '$ref': 'common.schema.json#/$defs/ActorId',
                },
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'deviceId': {
                    '$ref': 'common.schema.json#/$defs/DeviceId',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.branch.selected',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'actorId',
                'branchId',
                'conversationId',
                'cursor',
                'eventId',
                'eventType',
                'occurredAt',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/ChatEvent': {
            'oneOf': [
                {
                    '$ref': 'events.schema.json#/$defs/ConversationCreatedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/MessageCommittedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/MessageDerivationRecordedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/BranchCreatedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/BranchHeadAdvancedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/BranchSelectedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/BranchArchivedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/MessageTombstonedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/GenerationEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/ShareCreatedEvent',
                },
                {
                    '$ref': 'events.schema.json#/$defs/ExportCreatedEvent',
                },
            ],
        },
        'events.schema.json#/$defs/ConversationCreatedEvent': {
            'additionalProperties': False,
            'properties': {
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'createdBy': {
                    '$ref': 'common.schema.json#/$defs/ActorId',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.conversation.created',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'conversationId',
                'conversationSequence',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'occurredAt',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/ExportCreatedEvent': {
            'additionalProperties': False,
            'properties': {
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.export.created',
                },
                'exportId': {
                    '$ref': 'common.schema.json#/$defs/ExportId',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'scope': {
                    '$ref': 'common.schema.json#/$defs/ExportScope',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'conversationId',
                'cursor',
                'eventId',
                'eventType',
                'exportId',
                'graphRevision',
                'occurredAt',
                'schemaVersion',
                'scope',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/GenerationEvent': {
            'additionalProperties': False,
            'properties': {
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'enum': [
                        'chat.generation.queued',
                        'chat.generation.started',
                        'chat.generation.succeeded',
                        'chat.generation.failed',
                        'chat.generation.cancelled',
                    ],
                },
                'generationAttemptId': {
                    '$ref': 'common.schema.json#/$defs/GenerationAttemptId',
                },
                'generationEventSequence': {
                    '$ref': 'common.schema.json#/$defs/GenerationEventSequence',
                },
                'generationJobId': {
                    '$ref': 'common.schema.json#/$defs/GenerationJobId',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'resultMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'triggerMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'branchId',
                'conversationId',
                'cursor',
                'eventId',
                'eventType',
                'generationEventSequence',
                'generationJobId',
                'occurredAt',
                'schemaVersion',
                'triggerMessageId',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/MessageCommittedEvent': {
            'additionalProperties': False,
            'properties': {
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.message.committed',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'messageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'parentMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'role': {
                    '$ref': 'common.schema.json#/$defs/MessageRole',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'conversationId',
                'conversationSequence',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'messageId',
                'occurredAt',
                'role',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/MessageDerivationRecordedEvent': {
            'additionalProperties': False,
            'properties': {
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'derivedMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.message.derivation_recorded',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'kind': {
                    '$ref': 'common.schema.json#/$defs/DerivationKind',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'sourceMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'conversationId',
                'conversationSequence',
                'cursor',
                'derivedMessageId',
                'eventId',
                'eventType',
                'graphRevision',
                'kind',
                'occurredAt',
                'schemaVersion',
                'sourceMessageId',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/MessageTombstonedEvent': {
            'additionalProperties': False,
            'properties': {
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'conversationSequence': {
                    '$ref': 'common.schema.json#/$defs/ConversationSequence',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.message.tombstoned',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'messageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'conversationId',
                'conversationSequence',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'messageId',
                'occurredAt',
                'schemaVersion',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/ResnapshotResponse': {
            'additionalProperties': False,
            'properties': {
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'reason': {
                    'enum': [
                        'cursor_unknown_or_expired',
                        'unauthorized_cursor',
                        'gap_detected',
                        'unrecognised_event_type',
                    ],
                },
                'resnapshotCursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'conversationId',
                'graphRevision',
                'reason',
                'resnapshotCursor',
                'workspaceId',
            ],
            'type': 'object',
        },
        'events.schema.json#/$defs/ShareCreatedEvent': {
            'additionalProperties': False,
            'properties': {
                'branchId': {
                    '$ref': 'common.schema.json#/$defs/BranchId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'cursor': {
                    '$ref': 'common.schema.json#/$defs/OpaqueCursor',
                },
                'eventId': {
                    '$ref': 'common.schema.json#/$defs/TransportEventId',
                },
                'eventType': {
                    'const': 'chat.share.created',
                },
                'graphRevision': {
                    '$ref': 'common.schema.json#/$defs/GraphRevision',
                },
                'headMessageId': {
                    '$ref': 'common.schema.json#/$defs/MessageId',
                },
                'occurredAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'schemaVersion': {
                    '$ref': 'common.schema.json#/$defs/SchemaVersion',
                },
                'shareId': {
                    '$ref': 'common.schema.json#/$defs/ShareId',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'branchId',
                'conversationId',
                'cursor',
                'eventId',
                'eventType',
                'graphRevision',
                'headMessageId',
                'occurredAt',
                'schemaVersion',
                'shareId',
                'workspaceId',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/AdmittedProviderRoute': {
            'additionalProperties': False,
            'properties': {
                'adapterName': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
                'adapterVersion': {
                    'maxLength': 64,
                    'minLength': 1,
                    'type': 'string',
                },
                'connectionId': {
                    '$ref': 'common.schema.json#/$defs/ProviderConnectionId',
                },
                'modelId': {
                    '$ref': 'common.schema.json#/$defs/ProviderModelId',
                },
            },
            'required': [
                'adapterName',
                'adapterVersion',
                'connectionId',
                'modelId',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/BoundedExtensiblePropertyName': {
            'maxLength': 128,
            'minLength': 1,
            'pattern': '^(?!(?:[aA][pP][iI][kK][eE][yY]|[aA][pP][iI]_[kK][eE][yY]|[aA][uU][tT][hH][oO][rR][iI][zZ][aA][tT][iI][oO][nN]|[pP][aA][sS][sS][wW][oO][rR][dD]|[sS][eE][cC][rR][eE][tT]|[tT][oO][kK][eE][nN]|[cC][rR][eE][dD][eE][nN][tT][iI][aA][lL]|[cC][rR][eE][dD][eE][nN][tT][iI][aA][lL][sS]|[cC][oO][oO][kK][iI][eE]|[hH][eE][aA][dD][eE][rR]|[hH][eE][aA][dD][eE][rR][sS]|[uU][rR][lL]|[eE][nN][dD][pP][oO][iI][nN][tT]|[eE][nN][dD][pP][oO][iI][nN][tT][uU][rR][lL])(?![\\s\\S]))[A-Za-z][A-Za-z0-9_]*(?![\\s\\S])',
            'type': 'string',
        },
        'provider.schema.json#/$defs/ConfiguredRoutePreference': {
            'additionalProperties': False,
            'properties': {
                'connectionId': {
                    '$ref': 'common.schema.json#/$defs/ProviderConnectionId',
                },
                'modelId': {
                    '$ref': 'common.schema.json#/$defs/ProviderModelId',
                },
            },
            'required': [
                'connectionId',
                'modelId',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/EstimatedCost': {
            'additionalProperties': False,
            'properties': {
                'amount': {
                    'maximum': 1000000,
                    'minimum': 0,
                    'type': 'number',
                },
                'calculationVersion': {
                    'maxLength': 64,
                    'minLength': 1,
                    'type': 'string',
                },
                'catalogueRevision': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
                'currency': {
                    'pattern': '^[A-Z]{3}$(?![\\s\\S])',
                    'type': 'string',
                },
                'pricingSource': {
                    'enum': [
                        'provider_reported',
                        'omnivia_catalogue',
                        'estimate',
                    ],
                },
            },
            'required': [
                'amount',
                'calculationVersion',
                'catalogueRevision',
                'currency',
                'pricingSource',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/FinishReason': {
            'enum': [
                'stop',
                'length',
                'tool-calls',
                'content-filter',
                'error',
                'cancelled',
                'unknown',
            ],
        },
        'provider.schema.json#/$defs/GenerationOptions': {
            'additionalProperties': False,
            'properties': {
                'frequencyPenalty': {
                    'maximum': 2,
                    'minimum': -2,
                    'type': 'number',
                },
                'maxOutputTokens': {
                    'maximum': 1000000,
                    'minimum': 1,
                    'type': 'integer',
                },
                'presencePenalty': {
                    'maximum': 2,
                    'minimum': -2,
                    'type': 'number',
                },
                'stopSequences': {
                    'items': {
                        'maxLength': 256,
                        'minLength': 1,
                        'type': 'string',
                    },
                    'maxItems': 16,
                    'type': 'array',
                },
                'temperature': {
                    'maximum': 2,
                    'minimum': 0,
                    'type': 'number',
                },
                'topP': {
                    'maximum': 1,
                    'minimum': 0,
                    'type': 'number',
                },
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderErrorCode': {
            'enum': [
                'authentication',
                'permission',
                'model-not-found',
                'rate-limited',
                'quota-or-budget',
                'invalid-request',
                'context-window-exceeded',
                'content-policy',
                'endpoint-policy',
                'timeout',
                'cancelled',
                'transport',
                'provider-unavailable',
                'malformed-response',
                'unsupported-operation',
                'unknown',
            ],
        },
        'provider.schema.json#/$defs/ProviderInvocationLifecycleState': {
            'enum': [
                'requested',
                'in_progress',
                'succeeded',
                'failed',
                'cancelled',
                'indeterminate',
            ],
        },
        'provider.schema.json#/$defs/ProviderInvocationRecord': {
            'additionalProperties': False,
            'allOf': [
                {
                    'else': {
                        'else': {
                            'not': {
                                'anyOf': [
                                    {
                                        'required': [
                                            'routeEvidence',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'terminalAt',
                                        ],
                                    },
                                ],
                            },
                        },
                        'if': {
                            'properties': {
                                'lifecycleState': {
                                    'const': 'indeterminate',
                                },
                            },
                            'required': [
                                'lifecycleState',
                            ],
                        },
                        'then': {
                            'not': {
                                'required': [
                                    'routeEvidence',
                                ],
                            },
                            'required': [
                                'reconciliationState',
                            ],
                        },
                    },
                    'if': {
                        'properties': {
                            'lifecycleState': {
                                'enum': [
                                    'succeeded',
                                    'failed',
                                    'cancelled',
                                ],
                            },
                        },
                        'required': [
                            'lifecycleState',
                        ],
                    },
                    'then': {
                        'required': [
                            'routeEvidence',
                            'terminalAt',
                        ],
                    },
                },
                {
                    'else': {
                        'properties': {
                            'attemptIds': {
                                'minItems': 1,
                            },
                        },
                    },
                    'if': {
                        'properties': {
                            'lifecycleState': {
                                'const': 'requested',
                            },
                        },
                        'required': [
                            'lifecycleState',
                        ],
                    },
                    'then': {},
                },
            ],
            'properties': {
                'attemptIds': {
                    'items': {
                        '$ref': 'common.schema.json#/$defs/ProviderAttemptId',
                    },
                    'maxItems': 1000,
                    'type': 'array',
                    'uniqueItems': True,
                },
                'connectionId': {
                    '$ref': 'common.schema.json#/$defs/ProviderConnectionId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'createdAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'generationAttemptId': {
                    '$ref': 'common.schema.json#/$defs/GenerationAttemptId',
                },
                'invocationId': {
                    '$ref': 'common.schema.json#/$defs/ProviderInvocationId',
                },
                'jobId': {
                    '$ref': 'common.schema.json#/$defs/GenerationJobId',
                },
                'lifecycleState': {
                    '$ref': 'provider.schema.json#/$defs/ProviderInvocationLifecycleState',
                },
                'modelId': {
                    '$ref': 'common.schema.json#/$defs/ProviderModelId',
                },
                'operation': {
                    'const': 'language.stream',
                },
                'reconciliationState': {
                    '$ref': 'provider.schema.json#/$defs/ReconciliationState',
                },
                'routeEvidence': {
                    '$ref': 'provider.schema.json#/$defs/RouteEvidence',
                },
                'terminalAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'updatedAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'attemptIds',
                'connectionId',
                'conversationId',
                'createdAt',
                'generationAttemptId',
                'invocationId',
                'jobId',
                'lifecycleState',
                'modelId',
                'operation',
                'updatedAt',
                'workspaceId',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderInvocationRequest': {
            'additionalProperties': False,
            'properties': {
                'attemptId': {
                    '$ref': 'common.schema.json#/$defs/GenerationAttemptId',
                },
                'causationId': {
                    '$ref': 'common.schema.json#/$defs/CausationId',
                },
                'classificationRef': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
                'connectionId': {
                    '$ref': 'common.schema.json#/$defs/ProviderConnectionId',
                },
                'conversationId': {
                    '$ref': 'common.schema.json#/$defs/ConversationId',
                },
                'correlationId': {
                    '$ref': 'common.schema.json#/$defs/CorrelationId',
                },
                'deadlineAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'generationOptions': {
                    '$ref': 'provider.schema.json#/$defs/GenerationOptions',
                },
                'idempotencyKey': {
                    '$ref': 'common.schema.json#/$defs/IdempotencyKey',
                },
                'invocationId': {
                    '$ref': 'common.schema.json#/$defs/ProviderInvocationId',
                },
                'jobId': {
                    '$ref': 'common.schema.json#/$defs/GenerationJobId',
                },
                'messages': {
                    'items': {
                        '$ref': 'provider.schema.json#/$defs/ProviderMessage',
                    },
                    'maxItems': 4096,
                    'minItems': 1,
                    'type': 'array',
                },
                'modelId': {
                    '$ref': 'common.schema.json#/$defs/ProviderModelId',
                },
                'operation': {
                    'const': 'language.stream',
                },
                'policyRef': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
                'providerOptionsByNamespace': {
                    '$ref': 'provider.schema.json#/$defs/ProviderOptionsByNamespace',
                },
                'requestedAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'residencyRef': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
                'responseFormat': {
                    '$ref': 'provider.schema.json#/$defs/ResponseFormat',
                },
                'toolChoice': {
                    '$ref': 'provider.schema.json#/$defs/ToolChoice',
                },
                'tools': {
                    'items': {
                        '$ref': 'provider.schema.json#/$defs/ToolDefinition',
                    },
                    'maxItems': 128,
                    'type': 'array',
                },
                'workspaceId': {
                    '$ref': 'common.schema.json#/$defs/WorkspaceId',
                },
            },
            'required': [
                'attemptId',
                'classificationRef',
                'connectionId',
                'conversationId',
                'correlationId',
                'deadlineAt',
                'idempotencyKey',
                'invocationId',
                'jobId',
                'messages',
                'modelId',
                'operation',
                'policyRef',
                'requestedAt',
                'residencyRef',
                'responseFormat',
                'workspaceId',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderMessage': {
            'additionalProperties': False,
            'properties': {
                'parts': {
                    'items': {
                        '$ref': 'provider.schema.json#/$defs/ProviderMessagePart',
                    },
                    'maxItems': 4096,
                    'minItems': 1,
                    'type': 'array',
                },
                'role': {
                    'enum': [
                        'system',
                        'user',
                        'assistant',
                        'tool',
                    ],
                },
            },
            'required': [
                'parts',
                'role',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderMessagePart': {
            'additionalProperties': False,
            'else': {
                'else': {
                    'else': {
                        'else': {
                            'else': {
                                'anyOf': [
                                    {
                                        'required': [
                                            'evidenceReference',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'displayLabel',
                                        ],
                                    },
                                ],
                                'not': {
                                    'anyOf': [
                                        {
                                            'required': [
                                                'text',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'toolCallId',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'toolName',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'input',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'output',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'attachmentReference',
                                            ],
                                        },
                                    ],
                                },
                            },
                            'if': {
                                'properties': {
                                    'kind': {
                                        'const': 'file',
                                    },
                                },
                                'required': [
                                    'kind',
                                ],
                            },
                            'then': {
                                'not': {
                                    'anyOf': [
                                        {
                                            'required': [
                                                'text',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'toolCallId',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'toolName',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'input',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'output',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'evidenceReference',
                                            ],
                                        },
                                        {
                                            'required': [
                                                'displayLabel',
                                            ],
                                        },
                                    ],
                                },
                                'required': [
                                    'attachmentReference',
                                ],
                            },
                        },
                        'if': {
                            'properties': {
                                'kind': {
                                    'const': 'tool-result',
                                },
                            },
                            'required': [
                                'kind',
                            ],
                        },
                        'then': {
                            'not': {
                                'anyOf': [
                                    {
                                        'required': [
                                            'text',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'toolName',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'input',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'attachmentReference',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'evidenceReference',
                                        ],
                                    },
                                    {
                                        'required': [
                                            'displayLabel',
                                        ],
                                    },
                                ],
                            },
                            'required': [
                                'toolCallId',
                                'output',
                            ],
                        },
                    },
                    'if': {
                        'properties': {
                            'kind': {
                                'const': 'tool-call',
                            },
                        },
                        'required': [
                            'kind',
                        ],
                    },
                    'then': {
                        'not': {
                            'anyOf': [
                                {
                                    'required': [
                                        'text',
                                    ],
                                },
                                {
                                    'required': [
                                        'output',
                                    ],
                                },
                                {
                                    'required': [
                                        'attachmentReference',
                                    ],
                                },
                                {
                                    'required': [
                                        'evidenceReference',
                                    ],
                                },
                                {
                                    'required': [
                                        'displayLabel',
                                    ],
                                },
                            ],
                        },
                        'required': [
                            'toolCallId',
                            'toolName',
                            'input',
                        ],
                    },
                },
                'if': {
                    'properties': {
                        'kind': {
                            'const': 'reasoning',
                        },
                    },
                    'required': [
                        'kind',
                    ],
                },
                'then': {
                    'not': {
                        'anyOf': [
                            {
                                'required': [
                                    'text',
                                ],
                            },
                            {
                                'required': [
                                    'toolCallId',
                                ],
                            },
                            {
                                'required': [
                                    'toolName',
                                ],
                            },
                            {
                                'required': [
                                    'input',
                                ],
                            },
                            {
                                'required': [
                                    'output',
                                ],
                            },
                            {
                                'required': [
                                    'attachmentReference',
                                ],
                            },
                            {
                                'required': [
                                    'evidenceReference',
                                ],
                            },
                            {
                                'required': [
                                    'displayLabel',
                                ],
                            },
                        ],
                    },
                },
            },
            'if': {
                'properties': {
                    'kind': {
                        'const': 'text',
                    },
                },
                'required': [
                    'kind',
                ],
            },
            'properties': {
                'attachmentReference': {
                    '$ref': 'common.schema.json#/$defs/AttachmentReference',
                },
                'displayLabel': {
                    'maxLength': 512,
                    'minLength': 1,
                    'type': 'string',
                },
                'evidenceReference': {
                    '$ref': 'common.schema.json#/$defs/EvidenceReference',
                },
                'input': {
                    'additionalProperties': True,
                    'maxProperties': 1024,
                    'type': 'object',
                },
                'kind': {
                    'enum': [
                        'text',
                        'reasoning',
                        'tool-call',
                        'tool-result',
                        'file',
                        'source',
                    ],
                },
                'output': {
                    'additionalProperties': True,
                    'maxProperties': 1024,
                    'type': 'object',
                },
                'text': {
                    'maxLength': 1048576,
                    'type': 'string',
                },
                'toolCallId': {
                    '$ref': 'common.schema.json#/$defs/ProviderPartId',
                },
                'toolName': {
                    'maxLength': 128,
                    'type': 'string',
                },
            },
            'required': [
                'kind',
            ],
            'then': {
                'not': {
                    'anyOf': [
                        {
                            'required': [
                                'toolCallId',
                            ],
                        },
                        {
                            'required': [
                                'toolName',
                            ],
                        },
                        {
                            'required': [
                                'input',
                            ],
                        },
                        {
                            'required': [
                                'output',
                            ],
                        },
                        {
                            'required': [
                                'attachmentReference',
                            ],
                        },
                        {
                            'required': [
                                'evidenceReference',
                            ],
                        },
                        {
                            'required': [
                                'displayLabel',
                            ],
                        },
                    ],
                },
                'required': [
                    'text',
                ],
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderOptionNamespace': {
            'maxLength': 128,
            'minLength': 1,
            'pattern': '^[A-Za-z][A-Za-z0-9._-]*(?![\\s\\S])',
            'type': 'string',
        },
        'provider.schema.json#/$defs/ProviderOptionsByNamespace': {
            'additionalProperties': {
                '$ref': 'provider.schema.json#/$defs/ProviderOptionsNamespaceValue',
            },
            'maxProperties': 16,
            'propertyNames': {
                '$ref': 'provider.schema.json#/$defs/ProviderOptionNamespace',
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderOptionsNamespaceValue': {
            'additionalProperties': {
                'anyOf': [
                    {
                        '$ref': 'provider.schema.json#/$defs/SanitisedScalarValue',
                    },
                    {
                        'items': {
                            '$ref': 'provider.schema.json#/$defs/SanitisedScalarValue',
                        },
                        'maxItems': 64,
                        'type': 'array',
                    },
                ],
            },
            'maxProperties': 64,
            'propertyNames': {
                '$ref': 'provider.schema.json#/$defs/BoundedExtensiblePropertyName',
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/ProviderUsage': {
            'additionalProperties': False,
            'anyOf': [
                {
                    'required': [
                        'reported',
                    ],
                },
                {
                    'required': [
                        'estimated',
                    ],
                },
            ],
            'properties': {
                'estimated': {
                    '$ref': 'provider.schema.json#/$defs/UsageCounts',
                },
                'reported': {
                    '$ref': 'provider.schema.json#/$defs/UsageCounts',
                },
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/ReconciliationState': {
            'enum': [
                'reconciled',
                'pending_reconciliation',
                'unreconciled',
            ],
        },
        'provider.schema.json#/$defs/ResponseFormat': {
            'additionalProperties': False,
            'else': {
                'not': {
                    'required': [
                        'schemaRef',
                    ],
                },
            },
            'if': {
                'properties': {
                    'kind': {
                        'const': 'structured',
                    },
                },
                'required': [
                    'kind',
                ],
            },
            'properties': {
                'kind': {
                    'enum': [
                        'text',
                        'json',
                        'structured',
                    ],
                },
                'schemaRef': {
                    'maxLength': 512,
                    'type': 'string',
                },
            },
            'required': [
                'kind',
            ],
            'then': {
                'required': [
                    'schemaRef',
                ],
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/RouteEvidence': {
            'additionalProperties': False,
            'properties': {
                'admittedRoute': {
                    '$ref': 'provider.schema.json#/$defs/AdmittedProviderRoute',
                },
                'attemptEndedAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'attemptStartedAt': {
                    '$ref': 'common.schema.json#/$defs/Timestamp',
                },
                'configuredPreference': {
                    '$ref': 'provider.schema.json#/$defs/ConfiguredRoutePreference',
                },
                'estimatedCost': {
                    '$ref': 'provider.schema.json#/$defs/EstimatedCost',
                },
                'fallbackAuthorised': {
                    'type': 'boolean',
                },
                'reconciliationState': {
                    '$ref': 'provider.schema.json#/$defs/ReconciliationState',
                },
                'routeDecision': {
                    'enum': [
                        'configured',
                        'same_route_retry',
                        'fallback',
                    ],
                },
                'sameRouteRetryCount': {
                    'maximum': 1000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'terminalReason': {
                    'anyOf': [
                        {
                            '$ref': 'provider.schema.json#/$defs/FinishReason',
                        },
                        {
                            '$ref': 'provider.schema.json#/$defs/ProviderErrorCode',
                        },
                    ],
                },
                'usage': {
                    '$ref': 'provider.schema.json#/$defs/ProviderUsage',
                },
            },
            'required': [
                'admittedRoute',
                'attemptEndedAt',
                'attemptStartedAt',
                'configuredPreference',
                'fallbackAuthorised',
                'reconciliationState',
                'routeDecision',
                'sameRouteRetryCount',
                'terminalReason',
                'usage',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/SanitisedScalarValue': {
            'anyOf': [
                {
                    'maxLength': 4096,
                    'type': 'string',
                },
                {
                    'maximum': 1000000000,
                    'minimum': -1000000000,
                    'type': 'number',
                },
                {
                    'type': 'boolean',
                },
                {
                    'type': 'null',
                },
            ],
        },
        'provider.schema.json#/$defs/ToolChoice': {
            'additionalProperties': False,
            'else': {
                'not': {
                    'required': [
                        'toolName',
                    ],
                },
            },
            'if': {
                'properties': {
                    'mode': {
                        'const': 'named',
                    },
                },
                'required': [
                    'mode',
                ],
            },
            'properties': {
                'mode': {
                    'enum': [
                        'auto',
                        'none',
                        'required',
                        'named',
                    ],
                },
                'toolName': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
            },
            'required': [
                'mode',
            ],
            'then': {
                'required': [
                    'toolName',
                ],
            },
            'type': 'object',
        },
        'provider.schema.json#/$defs/ToolDefinition': {
            'additionalProperties': False,
            'properties': {
                'description': {
                    'maxLength': 4096,
                    'type': 'string',
                },
                'inputSchema': {
                    'additionalProperties': True,
                    'type': 'object',
                },
                'name': {
                    'maxLength': 128,
                    'minLength': 1,
                    'type': 'string',
                },
            },
            'required': [
                'inputSchema',
                'name',
            ],
            'type': 'object',
        },
        'provider.schema.json#/$defs/UsageCounts': {
            'additionalProperties': False,
            'minProperties': 1,
            'properties': {
                'audioTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'cacheReadTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'cacheWriteTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'inputTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'otherTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'outputTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'reasoningTokens': {
                    'maximum': 100000000,
                    'minimum': 0,
                    'type': 'integer',
                },
                'totalTokens': {
                    'maximum': 200000000,
                    'minimum': 0,
                    'type': 'integer',
                },
            },
            'type': 'object',
        },
    }
)
