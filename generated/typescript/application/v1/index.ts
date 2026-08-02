// GENERATED FILE - DO NOT EDIT.
//
// Source of truth:
//   contracts/application/v1/schemas/common.schema.json
//   contracts/application/v1/schemas/compatibility.schema.json
//   contracts/application/v1/schemas/errors.schema.json
//   contracts/application/v1/schemas/envelopes.schema.json
//   contracts/application/v1/schemas/service.schema.json
//   contracts/application/v1/schemas/records.schema.json
//   contracts/application/v1/schemas/jobs.schema.json
//   contracts/application/v1/schemas/operations.schema.json
//   contracts/application/v1/schemas/workspace.schema.json
//   contracts/application/v1/schemas/memory.schema.json
//   contracts/application/v1/schemas/evidence.schema.json
//   contracts/application/v1/schemas/knowledge.schema.json
//   contracts/application/v1/schemas/graph.schema.json
//   contracts/application/v1/schemas/context-pack.schema.json
//   contracts/application/v1/schemas/compatibility-matrix.schema.json
// Generator:
//   scripts/generate-application-contracts.py
//
// Regenerate: python scripts/generate-application-contracts.py
// Verify:     python scripts/generate-application-contracts.py --check
//
// Type declarations for the OmniVia Core Application Contract v1. This module is
// declaration-only: it has no imports, no runtime dependencies, and no behaviour
// beyond the frozen vocabulary constants below.

/**
 * Any value expressible in JSON. Used only inside opaque contract payloads.
 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

/**
 * Contract version of this generated module.
 */
export const CONTRACT_VERSION = "1.2" as const;

/**
 * Base URI every canonical v1 schema `$id` is rooted at.
 */
export const SCHEMA_BASE_URI = "https://contracts.omnivia.dev/application/v1/" as const;

/**
 * A `major.minor` contract version. Major changes are breaking; minor changes are additive and
 * forward compatible.
 */
export type ContractVersion = string;
export const CONTRACT_VERSION_PATTERN: string = "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$(?![\\s\\S])";

/**
 * A SemVer 2.0.0 release string identifying a concrete build, not a contract.
 */
export type ReleaseVersion = string;
export const RELEASE_VERSION_PATTERN: string =
  "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]" +
  "*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)(?:\\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0" +
  "-9a-zA-Z-]*))*))?(?:\\+([0-9a-zA-Z-]+(?:\\.[0-9a-zA-Z-]+)*))?$(?![\\s\\S])";

/**
 * Bounded, non-empty caller-assigned identifier for a single request attempt.
 */
export type RequestId = string;
export const REQUEST_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Bounded, non-empty identifier grouping related requests into one logical operation.
 */
export type CorrelationId = string;
export const CORRELATION_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Bounded, non-empty distributed-trace identifier. Diagnostic only; never an authorization
 * input.
 */
export type TraceId = string;
export const TRACE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Bounded, non-empty identifier of the workspace a request is scoped to.
 */
export type WorkspaceId = string;
export const WORKSPACE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Bounded, non-empty server-issued reference to the audit record for a completed operation.
 */
export type AuditReference = string;
export const AUDIT_REFERENCE_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.
 */
export type Identifier = string;
export const IDENTIFIER_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Stable namespaced capability identifier such as `memory.read`. At least one dot is required so
 * capability names always carry a namespace.
 */
export type CapabilityId = string;
export const CAPABILITY_ID_PATTERN: string =
  "^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*(?:\\.[a-z" +
  "][a-z0-9]*(?:[_-][a-z0-9]+)*)+$(?![\\s\\S])";

/**
 * An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible
 * minor releases can add vocabulary; consumers must preserve values they do not recognize.
 */
export type OpenCode = string;
export const OPEN_CODE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * An open scope token such as `memory:read` requested by the caller. Scopes narrow a request;
 * they never widen granted authority.
 */
export type Scope = string;
export const SCOPE_PATTERN: string = "^[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * An open purpose-limitation token stating why the caller is making this request.
 */
export type Purpose = string;
export const PURPOSE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never
 * parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a
 * widening of the character domain: a bare `$` matches before a final line terminator in some
 * conforming regex engines, so a token spelled with a trailing newline would be schema-valid
 * while the semantic validators -- which match the whole string -- refuse it. The lookahead pins
 * the anchor to absolute end of input, so strict schema and semantic validation accept exactly
 * the same tokens.
 */
export type OpaqueToken = string;
export const OPAQUE_TOKEN_PATTERN: string = "^[!-~]+$(?![\\s\\S])";

/**
 * Caller-assigned key making a mutation safe to retry. Equal keys with different inputs are an
 * `idempotency_conflict`.
 */
export type IdempotencyKey = string;
export const IDEMPOTENCY_KEY_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * An RFC 3339 timestamp in UTC with a literal `Z` offset.
 */
export type Timestamp = string;
export const TIMESTAMP_PATTERN: string =
  "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]" +
  "{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])";

/**
 * A bounded non-negative duration in milliseconds.
 */
export type DurationMs = number;

/**
 * An opaque per-projection version marker used to reason about read staleness.
 */
export type ProjectionVersion = string;
export const PROJECTION_VERSION_PATTERN: string = "^[!-~]+$(?![\\s\\S])";

/**
 * An opaque JSON object. The envelope carries domain payloads without inspecting them, which is
 * a statement about the envelope rather than about the payload: an operation's `input` and
 * `result` are each bound to their own definition by `operations.schema.json`'s `x-omnivia-
 * operation-catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload
 * against that binding is a separate step from decoding the envelope carrying it.
 */
export type JsonObject = { readonly [key: string]: JsonValue };

/**
 * A bounded positive page size a caller requests for a paginated read.
 */
export type PageLimit = number;

/**
 * Open, dot-namespaced code naming an operation's lifecycle state, such as `stable` or
 * `experimental` or `deprecated` or `removed`. Open by design so a compatible minor release can
 * add states without breaking existing decoders.
 */
export type OperationCompatibilityState = string;
export const OPERATION_COMPATIBILITY_STATE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming how thoroughly a release or capability combination has
 * actually been verified, such as `development` or `unverified` or `qualified` or `supported`.
 * Open by design so a compatible minor release can add states without breaking existing
 * decoders. A combination absent this state, or carrying anything other than an explicitly
 * verified state, must never be treated as supported: an empty or `unverified` entry is not
 * evidence of support.
 */
export type QualificationState = string;
export const QUALIFICATION_STATE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming which component a compatibility entry describes, such as
 * `core` or `runtime` or `cli` or `mcp` or `sdk`. Open by design so a compatible minor release
 * can add components without breaking existing decoders.
 */
export type ComponentKind = string;
export const COMPONENT_KIND_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming how a Context Pack was produced. Wire-open by shape so a
 * compatible minor release can add vocabulary, but trust-sensitive: v1 recognizes exactly one
 * value, `deterministic_view` (a regenerated, non-persisted, deterministic view), and semantic
 * validation fails closed on every other value rather than guessing at it. `immutable_snapshot`
 * is deliberately not a v1 mode -- persistence is an operation posture this read does not have
 * -- and `returned_artifact` was never a wire mode at all.
 */
export type ContextPackMode = string;
export const CONTEXT_PACK_MODE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * A bounded, non-negative count of tokens actually observed: the tokens one section's model-
 * facing content occupies, or the total a whole pack consumed. Distinct from
 * `ContextPackTokenBudget`, which is what a caller asked for: zero is a meaningful observation
 * (an empty pack consumed nothing) but never a meaningful request.
 */
export type ContextPackTokenCount = number;

/**
 * A bounded, strictly positive token budget a caller asks a pack to be built against. Zero is
 * excluded rather than merely discouraged: a pack built against no budget at all can carry no
 * content, so a zero budget states a request no build could usefully answer.
 */
export type ContextPackTokenBudget = number;

/**
 * A SHA-256 content digest, spelled `sha256:` followed by exactly 64 lowercase hexadecimal
 * characters. Deliberately narrower than the general `EvidenceChecksum`: this is not an opaque
 * server token a client round-trips but a value an independent implementation must be able to
 * recompute and compare byte for byte, so exactly one algorithm, one length, and one letter case
 * are admitted.
 */
export type ContextPackDigest = string;
export const CONTEXT_PACK_DIGEST_PATTERN: string = "^sha256:[0-9a-f]{64}$(?![\\s\\S])";

/**
 * Dot-namespaced operation identifier such as `memory.get`. The name is all this shape states;
 * what each name binds to -- its input and result schemas, and its scope, capability,
 * completion, pagination, idempotency, mutation-precondition, audit and allowed-error posture --
 * is published per operation by `operations.schema.json`'s `x-omnivia-operation-catalogue`. The
 * pattern admits any well-formed name, including ones no catalogue entry defines: whether a name
 * is a v1 application operation is a semantic question (see
 * `omnivia_core.contracts.v1.semantics_operations`), not a wire-shape one.
 */
export type OperationName = string;
export const OPERATION_NAME_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)+$(?![\\s\\S])";

/**
 * Stable machine-readable failure code. OPEN by design: this is a patterned string, not an enum,
 * so compatible minor releases can add codes. Decoders must preserve unknown codes and must not
 * map them onto a known code.
 */
export type ErrorCode = string;
export const ERROR_CODE_PATTERN: string = "^[a-z][a-z0-9_]*$(?![\\s\\S])";

/**
 * How a caller may retry. OPEN by design, for the same reason as `ErrorCode`. An unrecognized
 * retry class MUST fail safe as non-retryable: never infer that an unknown class is retryable.
 */
export type RetryClass = string;
export const RETRY_CLASS_PATTERN: string = "^[a-z][a-z0-9_]*$(?![\\s\\S])";

/**
 * Stable identifier of one L0 evidence artifact, constant across its append-only provenance
 * history. Distinct from `RecordId`: an evidence artifact is never itself a governed record.
 */
export type EvidenceId = string;
export const EVIDENCE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * A caller-supplied, normalized search query for `evidence.search`. Normalization (case-folding,
 * whitespace, tokenization) is caller-side; this document defines no normalization algorithm.
 */
export type EvidenceQuery = string;

/**
 * A content checksum, spelled `algorithm:hex-digest` (such as `sha256:9f86d0...`) so the digest
 * is never ambiguous about which algorithm produced it. Provider-neutral: this contract does not
 * mandate a specific algorithm.
 */
export type EvidenceChecksum = string;
export const EVIDENCE_CHECKSUM_PATTERN: string = "^[a-z][a-z0-9_]*:[A-Za-z0-9+/=_-]+$(?![\\s\\S])";

/**
 * An IANA-style `type/subtype` media type string, such as `text/plain` or `application/json`.
 */
export type MediaType = string;
export const MEDIA_TYPE_PATTERN: string =
  "^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za" +
  "-z0-9][A-Za-z0-9!#$&^_.+-]*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming which direction a traversal follows relations in: `outbound`,
 * `inbound`, or `both`. Wire-open by shape, but trust-sensitive: only the known values are
 * accepted by semantic validation, and an unrecognized value fails closed rather than being
 * guessed at.
 */
export type GraphDirection = string;
export const GRAPH_DIRECTION_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming a kind of relation between governed records, such as
 * `relates_to` or `derived_from`. Open by design so a compatible minor release can add relation
 * types without breaking existing decoders.
 */
export type GraphRelationType = string;
export const GRAPH_RELATION_TYPE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * A bounded traversal depth a caller may request, or the server states it actually applied. Zero
 * means the seeds themselves with no traversal beyond them; absent on input means the server's
 * default depth of 1.
 */
export type GraphDepthLimit = number;

/**
 * Open, dot-namespaced code naming the deterministic key a traversal result was ordered by, such
 * as `record_id_asc`, so identical inputs against an unchanged projection reproduce identical
 * node/edge ordering.
 */
export type GraphOrderingBasis = string;
export const GRAPH_ORDERING_BASIS_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code justifying why one endpoint of an edge is absent from a traversal
 * result: `page_boundary` when the traversal stopped at the node limit and offers a continuation
 * token, or `depth_boundary` when the present endpoint sits exactly at the applied depth limit.
 * Wire-open by shape, but trust-sensitive: an absent endpoint is a claim that the projection
 * stopped, not that the relation lost an end, so only the recognized values are accepted by
 * semantic validation and an unrecognized reason fails closed rather than being guessed at.
 */
export type GraphBoundaryReason = string;
export const GRAPH_BOUNDARY_REASON_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming where a job stands in its lifecycle, such as `queued` or
 * `running` or `succeeded` or `failed` or `cancelled`. Open by design so a compatible minor
 * release can add states without breaking existing decoders.
 */
export type JobState = string;
export const JOB_STATE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming what `JobProgress.completed_units`/`total_units` count, such
 * as `item` or `byte` or `document`. Open by design so a compatible minor release can add units
 * without breaking existing decoders.
 */
export type JobProgressUnit = string;
export const JOB_PROGRESS_UNIT_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming, on a `JobHandle`, whether this job may be cancelled right
 * now and where an already-requested cancellation stands, with four known values: `cancellable`
 * (a `job.cancel` would be accepted), `cancellation_pending` (a cancellation is already
 * requested and has not yet taken effect), `cancelled` (the job is already cancelled), and
 * `not_cancellable` (a `job.cancel` would be refused). This is an availability statement about
 * the job as observed, not the outcome of a control call: what a particular `job.cancel` did is
 * reported by `JobCancellationDisposition`. Open by design; an unrecognized value decodes and is
 * preserved but never implies cancellation is permitted, and carries no scheduler, worker,
 * lease, or persistence detail.
 */
export type JobCancellationAvailability = string;
export const JOB_CANCELLATION_AVAILABILITY_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming, on a `JobHandle`, whether this job may be recovered right
 * now, with three known values: `retryable` (a failed job that `job.retry` would run again),
 * `resumable` (a cancelled job that `job.retry` would continue from its checkpoint), and
 * `not_retryable` (a `job.retry` would be refused). `job.retry` is the single recovery operation
 * and carries no action selector, so this code reports which recovery server state would choose
 * rather than offering the caller a choice; what a particular `job.retry` did is reported by
 * `JobRecoveryDisposition`. Open by design; an unrecognized value decodes and is preserved but
 * never implies recovery is permitted, and carries no scheduler, worker, lease, checkpoint, or
 * persistence detail.
 */
export type JobRecoveryAvailability = string;
export const JOB_RECOVERY_AVAILABILITY_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming what one `job.cancel` call actually did, with three known
 * values: `cancellation_requested` (cancellation was accepted and the job will stop),
 * `cancelled` (the job is already cancelled, so the call changed nothing), and `not_cancellable`
 * (the call was refused and the job is unchanged). A state-based refusal is a successful,
 * idempotent control result rather than an API error: `not_cancellable` is returned alongside
 * the current unchanged handle, not raised as `conflict`. Open by design; an unrecognized value
 * decodes and is preserved but never implies cancellation was accepted, and carries no
 * scheduler, worker, lease, or persistence detail.
 */
export type JobCancellationDisposition = string;
export const JOB_CANCELLATION_DISPOSITION_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming what one `job.retry` call actually did, with three known
 * values: `retry_scheduled` (a failed job was scheduled to run again, from the beginning or from
 * a supported checkpoint), `resume_scheduled` (a cancelled resumable job was scheduled to
 * continue from its checkpoint), and `not_retryable` (no recovery was scheduled and the job is
 * unchanged). `job.retry` is the single recovery operation and carries no action selector:
 * server state, not the caller, decides between retrying and resuming, so this code reports that
 * decision rather than accepting it. A state-based refusal is a successful, idempotent control
 * result rather than an API error. Open by design; an unrecognized value decodes and is
 * preserved but never implies recovery was accepted, and carries no scheduler, worker, lease,
 * checkpoint, or persistence detail.
 */
export type JobRecoveryDisposition = string;
export const JOB_RECOVERY_DISPOSITION_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * A SHA-256 content digest, spelled `sha256:` followed by exactly 64 lowercase hexadecimal
 * characters. Deliberately narrower than the general `EvidenceChecksum`: this is not an opaque
 * server token a client round-trips but a value the caller and the server must be able to
 * recompute and compare byte for byte over the same staged bytes, so exactly one algorithm, one
 * length, and one letter case are admitted. Stated as what v1 initially requires: admitting a
 * further algorithm later is an additive widening of this pattern, not a redefinition of what a
 * checksum means.
 */
export type ContentChecksum = string;
export const CONTENT_CHECKSUM_PATTERN: string = "^sha256:[0-9a-f]{64}$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact`
 * or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add
 * record types without breaking existing decoders.
 */
export type GovernedRecordType = string;
export const GOVERNED_RECORD_TYPE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code selecting which slice of a governed record's versions a read
 * considers: `current_canonical` (the single active accepted version, the default when this
 * field is absent), `candidates` (proposed/candidate versions not yet accepted), or `history`
 * (every version, including superseded ones). Open by design so a compatible minor release can
 * add views without breaking existing decoders. Default resolution when absent is a semantic
 * concern (see `omnivia_core.contracts.v1.semantics`), not a wire-shape one.
 */
export type GovernedRecordView = string;
export const GOVERNED_RECORD_VIEW_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed
 * record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the
 * caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or
 * checks a permission, it only classifies what the record is about. Open by design so a
 * compatible minor release can add classifications without breaking existing decoders.
 */
export type RecordDomainScope = string;
export const RECORD_DOMAIN_SCOPE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * A caller-supplied, normalized search query for `memory.search`. Normalization (case-folding,
 * whitespace, tokenization) is caller-side; this document defines no normalization algorithm.
 */
export type MemoryQuery = string;

/**
 * Open, dot-namespaced code naming how `memory.search` results are ordered, such as `relevance`
 * or `recency`. Open by design so a compatible minor release can add orders without breaking
 * existing decoders.
 */
export type MemorySearchOrder = string;
export const MEMORY_SEARCH_ORDER_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming whether invoking an operation mutates state, such as `none`
 * or `create` or `update` or `delete`. Open by design so a compatible minor release can add
 * classifications without breaking existing decoders.
 */
export type OperationSideEffect = string;
export const OPERATION_SIDE_EFFECT_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming the kind of scope an operation carries, such as
 * `installation` or `workspace`. Open by design so a compatible minor release can add scope
 * kinds without breaking existing decoders. A given operation's scope metadata carries exactly
 * one kind.
 */
export type OperationScopeKind = string;
export const OPERATION_SCOPE_KIND_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming how an operation completes, such as `synchronous` (no durable
 * job, ever), `may_return_job` (a response may carry a `JobReference`), or `always_returns_job`
 * (every invocation starts a durable job). Independent of `OperationSideEffect`: an operation
 * like `import.start` is representable as a mutation (`side_effect`) that always returns a
 * durable job (`completion_mode`). Open by design so a compatible minor release can add modes
 * without breaking existing decoders.
 */
export type OperationCompletionMode = string;
export const OPERATION_COMPLETION_MODE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Whether and how an operation's results are paginated.
 */
export interface OperationPaginationMetadata {
  /**
   * Whether this operation's result may span more than one page.
   */
  readonly paginated: boolean;
  /**
   * Largest page size this operation accepts, when paginated.
   */
  readonly max_page_size?: number;
}

/**
 * How this operation may safely be retried. The three fields are not independent: `required`
 * entails `supports_idempotency_key` (an operation cannot demand a key it does not honour),
 * `safe_to_retry` excludes `required` (a request that is safe to repeat without a key cannot
 * also be rejected for lacking one), and an operation that does not support keys cannot require
 * them. A combination breaking any of those is a metadata statement no implementation can
 * satisfy.
 */
export interface OperationIdempotencyMetadata {
  /**
   * Whether this operation honours `RequestMetadata.idempotency_key`.
   */
  readonly supports_idempotency_key: boolean;
  /**
   * Whether omitting the idempotency key is itself rejected. A mutation that starts durable
   * work or changes a job's control state requires one, so a network-level repeat can never
   * become a second mutation; omitting it is a non-retryable invalid request, not a silently
   * accepted single-shot call.
   */
  readonly required: boolean;
  /**
   * Whether an identical request may be retried without an idempotency key, such as a plain
   * read.
   */
  readonly safe_to_retry: boolean;
}

/**
 * How this operation uses optimistic-concurrency preconditions.
 */
export interface OperationPreconditionMetadata {
  /**
   * Whether this operation honours `RequestMetadata.mutation_precondition`.
   */
  readonly supports_mutation_precondition: boolean;
  /**
   * Whether omitting the precondition is itself rejected for this mutation.
   */
  readonly required: boolean;
}

/**
 * A URI reference to the JSON Schema document governing an operation's input or result payload.
 */
export type SchemaReference = string;

/**
 * Stable identifier of a governed record, constant across every version of that record.
 */
export type RecordId = string;
export const RECORD_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Opaque, server-issued version marker of one specific revision of a record. Clients must round-
 * trip it verbatim and must never parse it.
 */
export type RecordVersion = string;
export const RECORD_VERSION_PATTERN: string = "^[!-~]+$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw
 * evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3`
 * (context models), or `l4` (organisational model). Distinct from workspace scope, which is a
 * caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a
 * compatible minor release can add layers without breaking existing decoders.
 */
export type GovernanceLayer = string;
export const GOVERNANCE_LAYER_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming whether a record version is the active one, such as `current`
 * or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not
 * coerced to a known one.
 */
export type RecordCurrentness = string;
export const RECORD_CURRENTNESS_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming a record's position in its own governance workflow, such as
 * `proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which
 * namespace a record belongs to) and `RecordCurrentness` (whether this version is the active
 * one): a record can be `accepted` and still later superseded, or `proposed` and never adopted.
 * Open by design so a compatible minor release can add states without breaking existing
 * decoders.
 */
export type GovernanceState = string;
export const GOVERNANCE_STATE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming the kind of thing a source reference points at, such as
 * `document` or `conversation` or `api_response`.
 */
export type SourceKind = string;
export const SOURCE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * An addressable position within a source: a pointer plus an optional character span, so
 * evidence can be pinpointed within a source rather than only referencing the source as a whole.
 */
export interface SourceSpan {
  /**
   * Locator within the source, such as a JSON Pointer, XPath, byte offset path, or line
   * reference.
   */
  readonly pointer: string;
  /**
   * Start of the span, in characters from the start of the pointed-at unit, when known.
   */
  readonly start_offset?: number;
  /**
   * End of the span, in characters from the start of the pointed-at unit, when known.
   */
  readonly end_offset?: number;
}

/**
 * Open, dot-namespaced code stating whether concrete evidence is actually available for a
 * record, such as `available` or `unavailable` or `redacted`. Open by design; an unrecognized
 * value must be preserved, not coerced to a known one.
 */
export type EvidenceDisposition = string;
export const EVIDENCE_DISPOSITION_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming which runtime probe is being requested or answered. The
 * frozen, currently known probe kinds are exactly `service.health`, `service.readiness`, and
 * `service.discover`. Open by design so a compatible minor release can add probe kinds without
 * breaking existing callers.
 */
export type ProbeKind = string;
export const PROBE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming the outcome of a probe or one of its components, such as
 * `pass` or `warn` or `fail`. Open by design; an unrecognized status must be preserved and
 * surfaced, not coerced to a known one.
 */
export type ProbeStatus = string;
export const PROBE_STATUS_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Open, dot-namespaced code naming a workspace's lifecycle status, such as `active` or
 * `provisioning` or `archived`. Open by design so a compatible minor release can add statuses
 * without breaking existing decoders.
 */
export type WorkspaceStatus = string;
export const WORKSPACE_STATUS_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Input for `workspace.create`. Installation-scoped: carries only installation-level creation
 * data, and never a caller-supplied workspace identifier.
 */
export interface WorkspaceCreateInput {
  /**
   * Human-readable name for the new workspace.
   */
  readonly display_name: string;
}

/**
 * Input for `workspace.inspect`. Workspace-scoped: the workspace to inspect is the request
 * envelope's selected workspace; this payload never carries a second, independent workspace
 * identifier.
 */
export interface WorkspaceInspectInput {
}

/**
 * Self-declared identity of the calling client. Diagnostic and compatibility input only; never
 * an authorization input.
 */
export interface ClientIdentity {
  /**
   * Stable client identifier such as `omnivia.desktop`.
   */
  readonly id: Identifier;
  /**
   * Client build version.
   */
  readonly version: ReleaseVersion;
}

/**
 * UNTRUSTED. A claim made by the caller about who it is acting as. Authentication credentials
 * stay transport-owned; a principal claim never becomes a GrantedAuthority without server-side
 * validation.
 */
export interface PrincipalClaim {
  /**
   * Principal the caller claims to act as. Unvalidated.
   */
  readonly claimed_principal_id?: Identifier;
  /**
   * Roles the caller claims to hold. Unvalidated.
   */
  readonly claimed_roles?: readonly Identifier[];
}

/**
 * Optimistic-concurrency precondition for a mutation. A mismatch is a
 * `mutation_precondition_failed` error.
 */
export interface MutationPrecondition {
  /**
   * Opaque version of the record the caller intends to modify.
   */
  readonly record_version: OpaqueToken;
}

/**
 * A non-fatal advisory attached to a successful or failed response.
 */
export interface Warning {
  /**
   * Open warning code. Unknown codes must be preserved and surfaced, not dropped.
   */
  readonly code: OpenCode;
  /**
   * Human-readable warning text. Not a stable interface.
   */
  readonly message: string;
  /**
   * Optional structured detail.
   */
  readonly details?: JsonObject;
}

/**
 * A statement that something the caller asked for was deliberately not returned.
 */
export interface Omission {
  /**
   * Open omission code.
   */
  readonly code: OpenCode;
  /**
   * Optional JSON Pointer into the result identifying what was omitted.
   */
  readonly path?: string;
  /**
   * Human-readable explanation. Not a stable interface.
   */
  readonly message?: string;
}

/**
 * Marks a result as incomplete. A partial result is still a success; callers must not treat it
 * as a full answer.
 */
export interface PartialResult {
  /**
   * True when the result is known to be incomplete.
   */
  readonly is_partial: boolean;
  /**
   * Open codes explaining why the result is partial.
   */
  readonly reasons: readonly OpenCode[];
}

/**
 * Staleness statement for reads served from a projection rather than the write model. Every
 * projection this read was served from must be named in both `projection_versions` and
 * `projection_watermarks`: the two maps are one statement about the same set of projections, so
 * their key sets are required to be identical and neither may be empty. A read served from no
 * named projection cannot state its own staleness, and a projection that states a version but no
 * watermark (or the reverse) leaves the caller unable to tell how far behind the write model it
 * actually is.
 */
export interface ProjectionFreshness {
  /**
   * Point in time the projection reflects.
   */
  readonly as_of: Timestamp;
  /**
   * Open map of projection name to the opaque projection version this read was actually served
   * from. Projection names are an open vocabulary, so new projections may appear in compatible
   * minor releases; at least one must be named, and the key set must equal
   * `projection_watermarks`'.
   */
  readonly projection_versions: Readonly<Record<string, ProjectionVersion>>;
  /**
   * Open map of projection name to the opaque write-model version each projection has consumed
   * up to -- how far the projection has caught up, as opposed to which version served this
   * read. Keyed by exactly the same projection names as `projection_versions`; at least one
   * must be named.
   */
  readonly projection_watermarks: Readonly<Record<string, ProjectionVersion>>;
  /**
   * True when the server knows the projection lags the write model.
   */
  readonly stale: boolean;
}

/**
 * A pagination position. Direction-neutral: the same shape is read differently on a request than
 * on a result, and neither reading is the other's default. On a request, an absent `page` asks
 * for the first page, and a present `page` must actually name a continuation token -- `{}`
 * states nothing to continue from and is invalid. On a result, `page` is always present and
 * states the position this read reached: a continuation token means more remains, and `{}` means
 * the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one
 * spelling on every paginated result, so a caller never has to know which result type it is
 * holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the
 * bindings a token proves are deliberately out of scope here; a token is opaque, and a reader
 * that needs to prove what one was bound to takes that binding as separate trusted input rather
 * than parsing the token.
 */
export interface PageMetadata {
  /**
   * Opaque cursor. On a request, the position to continue from; on a result, the position the
   * next page starts at. Absent on a result means the read is exhausted, which is why an
   * exhausted result still carries `page` as `{}` rather than dropping the field.
   */
  readonly continuation_token?: OpaqueToken;
}

/**
 * Reference to asynchronous work started by an operation. The reference carries the identifier
 * only, deliberately: the job's own lifecycle -- its states, progress, attempts, events,
 * cancellation and retry -- is published separately in `jobs.schema.json`, and is read and
 * controlled through the `job.get`, `job.events`, `job.cancel` and `job.retry` operations rather
 * than by widening the handle a response hands back.
 */
export interface JobReference {
  /**
   * Opaque job identifier.
   */
  readonly job_id: OpaqueToken;
}

/**
 * An inclusive range of contract versions a peer supports.
 */
export interface VersionWindow {
  /**
   * Lowest supported version, inclusive.
   */
  readonly minimum: ContractVersion;
  /**
   * Highest supported version, inclusive.
   */
  readonly maximum: ContractVersion;
}

/**
 * A stable, citable notice that something in the contract is going away.
 */
export interface Deprecation {
  /**
   * Stable deprecation identifier. Never reused for a different deprecation.
   */
  readonly id: Identifier;
  /**
   * Contract version in which the deprecation was announced.
   */
  readonly since: ContractVersion;
  /**
   * Contract version in which removal is planned, when known.
   */
  readonly removal?: ContractVersion;
  /**
   * What to use instead.
   */
  readonly replacement?: string;
  /**
   * Human-readable explanation. Not a stable interface.
   */
  readonly message?: string;
}

/**
 * Where the peer stands relative to a required or offered upgrade.
 */
export interface UpgradeState {
  /**
   * Open upgrade-state code. Known values are listed in `x-omnivia-upgrade-states`; unknown
   * values must be preserved.
   */
  readonly value: OpenCode;
  /**
   * Contract version the upgrade targets, when one is known.
   */
  readonly target_version?: ContractVersion;
  /**
   * Human-readable explanation. Not a stable interface.
   */
  readonly reason?: string;
}

/**
 * A concrete capability at a concrete version.
 */
export interface CapabilityRef {
  /**
   * Stable namespaced capability identifier.
   */
  readonly id: CapabilityId;
  /**
   * Capability contract version.
   */
  readonly version: ContractVersion;
}

/**
 * A capability the caller needs, at or above a minimum version.
 */
export interface CapabilityRequirement {
  /**
   * Stable namespaced capability identifier.
   */
  readonly id: CapabilityId;
  /**
   * Lowest capability version the caller can work with.
   */
  readonly minimum_version: ContractVersion;
  /**
   * When true, an unmet requirement fails the request with `capability_not_granted`. When
   * false, the caller degrades.
   */
  readonly required: boolean;
}

/**
 * A precise pointer to one exact L0 evidence artifact: which artifact, and the content checksum
 * that artifact carried. Both are required, so the pointer names a specific immutable content
 * state rather than whatever the identifier resolves to later. Distinct from
 * `records.EvidenceReference`, which points at a source a record drew on rather than at a
 * captured L0 artifact.
 */
export interface ContextPackEvidenceReference {
  /**
   * Identifier of the referenced L0 evidence artifact.
   */
  readonly evidence_id: EvidenceId;
  /**
   * The exact content checksum the referenced artifact carried, so this reference names one
   * immutable content state rather than an identifier whose content may since have been
   * recaptured.
   */
  readonly content_checksum: EvidenceChecksum;
}

/**
 * One model-facing section of a Context Pack: its identity, what kind of section it is, its
 * content, the citations that content rests on, and the tokens that content occupies. Every
 * section is substantiated: `citation_ids` is never empty, so no part of a pack's model-facing
 * content is unattributable.
 */
export interface ContextPackSection {
  /**
   * Identifier of this section within this pack, unique across `sections`. Section identifiers
   * and citation identifiers are independent namespaces; the same string may appear in both.
   */
  readonly section_id: Identifier;
  /**
   * Open code naming what kind of section this is, such as `summary` or `evidence_digest`.
   * Open by design; an unrecognized kind must be preserved, not coerced.
   */
  readonly kind: OpenCode;
  /**
   * Optional human-readable heading for this section. Not a stable interface, and not counted
   * by `token_count`.
   */
  readonly title?: string;
  /**
   * The exact model-facing content of this section. Never empty: a section that contributes no
   * content contributes nothing a caller could act on.
   */
  readonly content: string;
  /**
   * The citations this section's content rests on, by `citation_id`, in deterministic
   * ascending order and free of duplicates. Never empty, and every identifier must resolve to
   * a citation this pack actually returned.
   */
  readonly citation_ids: readonly Identifier[];
  /**
   * Tokens the exact `content` string occupies under the tokenizer named by
   * `reproducibility.tokenizer_id` at `reproducibility.tokenizer_version`. Covers `content`
   * and nothing else: `title` and any excerpt carried by a cited citation are deliberately
   * excluded, so a caller can reconcile this count against the string it actually sends to a
   * model.
   */
  readonly token_count: ContextPackTokenCount;
}

/**
 * A stated conflict between two or more citations this pack returned, which the pack surfaces
 * rather than resolving on the caller's behalf. Stated in citation identifiers rather than
 * record references so evidence and governed records are addressed by the one reference system
 * this pack already publishes, instead of a second, competing one that could name something the
 * pack never cited.
 */
export interface ContextPackConflict {
  /**
   * Human-readable statement of the conflict. Not a stable interface.
   */
  readonly description: string;
  /**
   * The citations that conflict with one another, by `citation_id`, in deterministic ascending
   * order and free of duplicates. At least two, since a conflict needs two sides, and every
   * identifier must resolve to a citation this pack actually returned.
   */
  readonly conflicting_citation_ids: readonly Identifier[];
}

/**
 * A stated uncertainty this pack surfaces rather than silently resolving or hiding, anchored to
 * the citations it concerns.
 */
export interface ContextPackUncertainty {
  /**
   * Human-readable statement of the uncertainty. Not a stable interface.
   */
  readonly description: string;
  /**
   * The citations this uncertainty concerns, by `citation_id`, in deterministic ascending
   * order and free of duplicates. At least one: an uncertainty anchored to nothing this pack
   * returned cannot be acted on or checked, and every identifier must resolve to a citation
   * this pack actually returned.
   */
  readonly related_citation_ids: readonly Identifier[];
}

/**
 * Token budget accounting for one Context Pack: the positive budget it was built against, and
 * the non-negative amount its sections actually consumed.
 */
export interface ContextPackBudget {
  /**
   * The token budget this pack was built against, exactly as requested.
   */
  readonly token_budget: ContextPackTokenBudget;
  /**
   * Tokens actually consumed, exactly the sum of every section's `token_count`.
   */
  readonly tokens_used: ContextPackTokenCount;
}

/**
 * One L0 evidence artifact on the authorized candidate frontier, named by immutable identity
 * alone. Exactly three members and nothing else: the partition it was authorized in, the
 * artifact, and the exact content state that artifact carried. Content, excerpts, provenance,
 * spans, scores, distances, ranks, tie-breaks, selection flags, citations, sections, query and
 * normalization state, and every authority, policy, configuration, or projection version are all
 * deliberately absent -- a candidate-set digest must depend on which authorized material existed
 * and on nothing a later ranking or selection step could change.
 */
export interface ContextPackAuthorizedEvidenceCandidate {
  /**
   * Always `evidence`: the L0 partition this candidate was authorized in. Carried explicitly
   * rather than inferred from the member names so the digest preimage separates the four
   * partitions itself, and so an unknown partition fails closed instead of being guessed at.
   */
  readonly partition: string;
  /**
   * Identifier of the authorized L0 evidence artifact. Exactly an `evidence.EvidenceId` -- the
   * same domain the artifact it names is drawn from, not a widened one. A digest preimage that
   * admitted identities the items themselves cannot carry would attest to a frontier no
   * artifact could be a member of, so the domain is shared rather than restated.
   */
  readonly evidence_id: EvidenceId;
  /**
   * The exact content checksum that artifact carried, so the candidate names one immutable
   * content state rather than an identifier whose content may since have been recaptured.
   * Exactly an `evidence.EvidenceChecksum`, for the reason `evidence_id` states.
   */
  readonly content_checksum: EvidenceChecksum;
}

/**
 * One governed record version on the authorized candidate frontier, named by immutable identity
 * alone. Exactly three members and nothing else: which governed partition it was authorized in,
 * the record, and the version. The same exclusions `ContextPackAuthorizedEvidenceCandidate`
 * states apply here for the same reason. Two different versions of one record are two
 * independent candidates whenever both were independently eligible; the same version twice, in
 * one partition or across two, is a contradiction rather than a set.
 */
export interface ContextPackAuthorizedRecordCandidate {
  /**
   * Which governed partition this candidate was authorized in: `records` for a current
   * canonical L2 version, `history` for a historical canonical L2 version, `context_models`
   * for a current canonical L3 context model. Closed to exactly these three; an unknown
   * partition fails closed.
   */
  readonly partition: string;
  /**
   * Identifier of the authorized governed record. Exactly a `records.RecordId`, the same
   * domain the record it names is drawn from, for the reason
   * `ContextPackAuthorizedEvidenceCandidate.evidence_id` states.
   */
  readonly record_id: RecordId;
  /**
   * The exact version of that record, so the candidate names one immutable governed version
   * rather than whatever the record identifier resolves to later. Exactly a
   * `records.RecordVersion`, for the same reason.
   */
  readonly version: RecordVersion;
}

/**
 * The exact normalized request one Context Pack was built from: the server-produced normalized
 * query and the version of the normalization that produced it, the mode, the resolved record
 * view, the token budget, and any selection filters. The single normalized form of a request --
 * the original caller query stays on the result's own `query` field, and nothing else restates
 * it. Query normalization itself is server-owned and versioned: this contract requires the
 * normalized query to be non-empty and pins which normalization produced it, and deliberately
 * specifies no normalization algorithm of its own.
 */
export interface ContextPackNormalizedRequest {
  /**
   * The normalized form of the caller's query that this build actually ran. Never empty: a
   * build that normalized a query to nothing has no request left to reproduce.
   */
  readonly normalized_query: string;
  /**
   * The mode this build ran in, bound exactly to the validated request's mode.
   */
  readonly mode: ContextPackMode;
  /**
   * The governed-record view the build resolved to. In v1 this is always `current_canonical`:
   * a Context Pack selects current canonical knowledge plus the history and context models
   * that support it, and the request carries no view selector that could widen that.
   */
  readonly view: GovernedRecordView;
  /**
   * The token budget this build ran against, bound exactly to the validated request's budget.
   */
  readonly token_budget: ContextPackTokenBudget;
  /**
   * Version of the query normalization that produced `normalized_query`. Without it the
   * normalized query is unreproducible, since a later normalization of the same caller query
   * may differ.
   */
  readonly normalization_version: Identifier;
  /**
   * The domain-scope filter the build applied, present exactly when the request carried one.
   */
  readonly domain_scope?: RecordDomainScope;
  /**
   * The record-type filter the build applied, present exactly when the request carried one.
   */
  readonly record_type?: GovernedRecordType;
}

/**
 * Input for `context_pack.build`. Workspace-scoped: the workspace, principal, scopes, and
 * purpose are the request envelope's; this payload never carries a second, independent copy of
 * any of them, and selecting content never grants new authority beyond what the envelope already
 * carries. Deliberately minimal: no view selector, no point-in-time selector, no pagination, and
 * no persistence, expiry, retention, snapshot, or job control. The v1 operation resolves the
 * current canonical view synchronously and persists nothing, so none of those controls has a
 * meaning here, and a payload that smuggles one in is rejected rather than ignored.
 */
export interface ContextPackBuildInput {
  /**
   * Original caller query this pack is built for. The server normalizes it; the normalized
   * form appears only on the result's `reproducibility.normalized_request`.
   */
  readonly query: MemoryQuery;
  /**
   * How to build the pack. v1 recognizes only `deterministic_view`.
   */
  readonly mode: ContextPackMode;
  /**
   * Bounded, strictly positive maximum token budget for the built pack.
   */
  readonly token_budget: ContextPackTokenBudget;
  /**
   * Restrict governed-record selection to this domain scope, when set.
   */
  readonly domain_scope?: RecordDomainScope;
  /**
   * Restrict governed-record selection to this record type, when set.
   */
  readonly record_type?: GovernedRecordType;
}

/**
 * A single typed failure. The code and retry class are the contract; the message is not.
 */
export interface ApiError {
  /**
   * Stable failure code.
   */
  readonly code: ErrorCode;
  /**
   * Human-readable explanation. Never parse it and never branch on it.
   */
  readonly message: string;
  /**
   * Retry semantics for this failure. Unknown values are treated as non-retryable.
   */
  readonly retry_class: RetryClass;
  /**
   * Minimum backoff before a retry is worth attempting, when the server can state one.
   */
  readonly retry_after_ms?: DurationMs;
  /**
   * Optional structured detail. Must never carry credentials.
   */
  readonly details?: JsonObject;
}

/**
 * The identity of one asynchronous job: what it is, which application operation started it, its
 * immutable audit linkage, and, when applicable, which workspace it runs against.
 */
export interface JobIdentity {
  /**
   * Opaque, server-issued identifier of this job.
   */
  readonly job_id: OpaqueToken;
  /**
   * Open code naming the kind of work this job performs, such as `ingestion.import`.
   */
  readonly job_kind: OpenCode;
  /**
   * The application operation whose invocation started this job.
   */
  readonly originating_operation: OperationName;
  /**
   * Immutable reference to the audit record for the operation invocation that started this
   * job.
   */
  readonly audit_reference: AuditReference;
  /**
   * Workspace this job runs against, when the job is workspace-scoped.
   */
  readonly workspace_id?: WorkspaceId;
}

/**
 * A point-in-time progress statement for a running job.
 */
export interface JobProgress {
  /**
   * What `completed_units`/`total_units` count, so the counters are interpretable without job-
   * kind-specific knowledge.
   */
  readonly unit: JobProgressUnit;
  /**
   * Units of work completed so far, counted in `unit`.
   */
  readonly completed_units: number;
  /**
   * Total units of work expected, when known in advance, counted in `unit`.
   */
  readonly total_units?: number;
  /**
   * Human-readable progress note. Not a stable interface.
   */
  readonly message?: string;
}

/**
 * The control actions a caller may take on a job right now: cancellation and recovery. There are
 * exactly two, because there are exactly two control operations -- `job.cancel` and `job.retry`.
 * There is deliberately no `resume` member and no `job.resume` operation: retrying a failed job
 * and resuming a cancelled resumable one are two readings of the same single recovery operation,
 * chosen from server state rather than selected by the caller, so a separate resume disposition
 * would have offered a control the contract does not have. Deliberately exposes only these
 * caller-facing availabilities, never scheduler, worker, lease, checkpoint, or persistence
 * detail.
 */
export interface JobControl {
  /**
   * Whether this job may be cancelled and where an already-requested cancellation stands.
   */
  readonly cancellation: JobCancellationAvailability;
  /**
   * Whether this job may be recovered by `job.retry`, and which recovery that would be.
   */
  readonly recovery: JobRecoveryAvailability;
}

/**
 * One entry in a job's ordered event stream.
 */
export interface JobEvent {
  /**
   * Monotonically increasing ordinal of this event within the job.
   */
  readonly sequence: number;
  /**
   * When this event occurred.
   */
  readonly occurred_at: Timestamp;
  /**
   * State the job was in when this event was recorded.
   */
  readonly state: JobState;
  /**
   * Human-readable event note. Not a stable interface.
   */
  readonly message?: string;
  /**
   * Optional structured detail.
   */
  readonly details?: JsonObject;
}

/**
 * The explicit outcome recorded when a job's terminal state is cancellation, distinguishing it
 * from an ordinary success or failure.
 */
export interface JobCancellationOutcome {
  /**
   * Open code naming why the job was cancelled, such as `caller_requested` or
   * `deadline_exceeded`.
   */
  readonly reason: OpenCode;
}

/**
 * The immutable description of one already-staged import source. Provider-neutral by
 * construction: it names a server-issued staging handle and the content facts that handle
 * resolves to, and nothing about how the content got there or how it will be read. It carries no
 * filesystem path, URL, inline archive, credential, connector configuration, parser
 * implementation name, or runtime/storage option, so an import cannot be steered from the wire
 * into reading something the server did not already stage. Immutable: the descriptor accepted by
 * `import.start` is the exact descriptor the resulting `ImportCompletionResult` reports back, so
 * what was imported is never in question after the fact.
 */
export interface ImportSourceDescriptor {
  /**
   * Server-issued, immutable handle naming the already-staged content. Clients round-trip it
   * verbatim and never parse it; it is the only locator this contract accepts.
   */
  readonly staged_source_ref: OpaqueToken;
  /**
   * Open code naming what kind of source was staged, such as `archive` or `document`.
   * Descriptive only: it never selects a parser implementation.
   */
  readonly source_kind: OpenCode;
  /**
   * Digest of the staged content, so the import and the caller can prove they are talking
   * about the same bytes.
   */
  readonly content_checksum: ContentChecksum;
  /**
   * Length of the staged content in bytes. Zero is valid: empty staged content is a
   * legitimate, checksummable import source.
   */
  readonly content_length_bytes: number;
  /**
   * Media type of the staged content. The same `MediaType` the resulting L0 evidence carries,
   * so one type describes one concept wherever it is reached from.
   */
  readonly media_type: MediaType;
  /**
   * Caller-meaningful version of the staged source, when the source has one. Never an
   * authorization input and never a locator.
   */
  readonly source_version?: Identifier;
}

/**
 * Input for `job.get`. Names one job. Workspace-scoped through the request envelope's selected
 * workspace, so this payload never carries a second, independent workspace identifier.
 */
export interface JobGetInput {
  /**
   * Opaque identifier of the job to read.
   */
  readonly job_id: OpaqueToken;
}

/**
 * Input for `job.cancel`. Names one job and, optionally, why. Workspace-scoped through the
 * request envelope's selected workspace, so this payload never carries a second, independent
 * workspace identifier.
 */
export interface JobCancelInput {
  /**
   * Opaque identifier of the job to cancel.
   */
  readonly job_id: OpaqueToken;
  /**
   * Open code naming why the caller is cancelling, such as `caller_requested`. Recorded on the
   * job's cancellation outcome; never an authorization input.
   */
  readonly reason?: OpenCode;
}

/**
 * Input for `job.retry`, the single recovery operation. Names one job and nothing else: there is
 * deliberately no action selector and no checkpoint reference, because whether recovery is a
 * retry from the beginning or a resume from a supported checkpoint is chosen from server state,
 * not requested by the caller. Workspace-scoped through the request envelope's selected
 * workspace, so this payload never carries a second, independent workspace identifier.
 */
export interface JobRetryInput {
  /**
   * Opaque identifier of the job to recover.
   */
  readonly job_id: OpaqueToken;
}

/**
 * A caller-supplied, auditable reason for a governance decision or transition: an open reason
 * code plus an optional bounded human-readable comment. Carries no reviewer identity, authority
 * level, or governance-state field of its own -- those are server-owned and never asserted
 * through this shape.
 */
export interface GovernanceRationale {
  /**
   * Open code naming why this governance decision or transition was made.
   */
  readonly reason_code: OpenCode;
  /**
   * Optional bounded human-readable elaboration. Not a stable interface.
   */
  readonly comment?: string;
}

/**
 * Input for `memory.get`. Workspace-scoped: the workspace is the request envelope's selected
 * workspace; this payload never carries a second, independent workspace identifier.
 */
export interface MemoryGetInput {
  /**
   * Stable identifier of the record to fetch.
   */
  readonly record_id: RecordId;
  /**
   * Specific opaque version to fetch, when known; absent means the latest version for `view`.
   */
  readonly version?: RecordVersion;
  /**
   * Which slice of this record's versions to consider. Absent defaults to `current_canonical`.
   */
  readonly view?: GovernedRecordView;
}

/**
 * What an operation itself declares it needs and touches, independent of any single caller's
 * request.
 */
export interface OperationScope {
  /**
   * Scopes a caller must hold to invoke this operation.
   */
  readonly required_scopes: readonly Scope[];
  /**
   * Whether invoking this operation mutates state.
   */
  readonly side_effect: OperationSideEffect;
  /**
   * The single scope kind this operation requires, such as `installation` or `workspace`.
   */
  readonly scope_kind: OperationScopeKind;
}

/**
 * How an operation completes and, when durable work is involved, what kind of job it starts.
 */
export interface OperationJobMetadata {
  /**
   * How this operation completes: synchronously, optionally through a job, or always through a
   * job.
   */
  readonly completion_mode: OperationCompletionMode;
  /**
   * Open code naming the kind of job this operation starts, when `completion_mode` entails
   * one.
   */
  readonly job_kind?: OpenCode;
  /**
   * Reference to the JSON Schema governing `JobTerminalSuccess.result` for the job this
   * operation starts. This is what makes terminal success typed rather than opaque:
   * `result_kind` names the shape and this reference resolves it. Present exactly when
   * `completion_mode` entails a durable job: in the v1 catalogue that is `import.start` alone,
   * bound to `ImportCompletionResult`. A synchronous operation omits it, along with
   * `job_kind`.
   */
  readonly terminal_result_schema_ref?: SchemaReference;
}

/**
 * Whether and how this operation is audited.
 */
export interface OperationAuditMetadata {
  /**
   * Whether invoking this operation produces an audit record.
   */
  readonly audited: boolean;
  /**
   * Open code categorizing this operation's audit records, when audited.
   */
  readonly audit_category?: OpenCode;
}

/**
 * A pointer to the external or internal thing a record's claim came from.
 */
export interface SourceReference {
  /**
   * What kind of thing this reference points at.
   */
  readonly kind: SourceKind;
  /**
   * Identifier of the source within its own system of record.
   */
  readonly source_id: Identifier;
  /**
   * Optional locator within the source, such as a path, offset, or message id.
   */
  readonly locator?: string;
  /**
   * When the source was read to produce the record it supports.
   */
  readonly retrieved_at?: Timestamp;
}

/**
 * Optional provenance about the automated extractor that produced a governed record's claim,
 * when one did. Absent entirely for a claim a human asserted directly. Defined here rather than
 * in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json`
 * depending on a document that already depends on it.
 */
export interface CandidateExtractionMetadata {
  /**
   * Identifier of the extractor that produced this candidate.
   */
  readonly extractor_id: Identifier;
  /**
   * Version of the extractor that produced this candidate, when known.
   */
  readonly extractor_version?: Identifier;
  /**
   * Version of the model the extractor used, when known.
   */
  readonly model_version?: Identifier;
  /**
   * Version of the prompt the extractor used, when known.
   */
  readonly prompt_version?: Identifier;
  /**
   * When the extractor produced this candidate.
   */
  readonly extracted_at: Timestamp;
  /**
   * The extractor's self-reported confidence in this candidate, on a 0-1 scale, when known.
   */
  readonly confidence?: number;
  /**
   * Open code naming this candidate's reconciliation/deduplication state against prior
   * extractions, such as `novel` or `duplicate` or `merged`, when the extractor determined
   * one. Open by design; an unrecognized value must be preserved, not coerced to a known one,
   * and must never widen this candidate's authority.
   */
  readonly reconciliation_state?: OpenCode;
}

/**
 * The distinct instants a governed record's lifecycle turns on: when the underlying fact
 * occurred in the world, when it was observed, when the system ingested it, when this version
 * was persisted, the window it is asserted valid for, and when it was superseded.
 */
export interface RecordTemporalMetadata {
  /**
   * When the underlying fact occurred in the world (source/event time), when distinguishable
   * from `observed_at`.
   */
  readonly event_at?: Timestamp;
  /**
   * When the underlying fact was observed to be true in the world, when known.
   */
  readonly observed_at?: Timestamp;
  /**
   * When the system first ingested the fact behind this record.
   */
  readonly ingested_at: Timestamp;
  /**
   * When this specific version was persisted.
   */
  readonly recorded_at: Timestamp;
  /**
   * Start of the window this record is asserted valid for, when bounded.
   */
  readonly valid_from?: Timestamp;
  /**
   * End of the window this record is asserted valid for, when bounded.
   */
  readonly valid_until?: Timestamp;
  /**
   * When this record version was superseded by a newer version, present only once superseded.
   */
  readonly superseded_at?: Timestamp;
}

/**
 * A direction-neutral pointer from one record version to another related record version. The
 * direction of the relationship comes entirely from which field on `RecordIdentity` carries it
 * (`supersedes` vs `superseded_by`); this DTO itself states only which record and version, and
 * why.
 */
export interface SupersessionReference {
  /**
   * Identifier of the related record.
   */
  readonly record_id: RecordId;
  /**
   * The specific related version, when known.
   */
  readonly version?: RecordVersion;
  /**
   * Open code naming why this supersession relationship exists.
   */
  readonly reason?: OpenCode;
}

/**
 * A precise, non-directional pointer to one exact record version: both `record_id` and `version`
 * are always required. Distinct from `SupersessionReference`, which is direction-bearing (its
 * meaning comes from which `RecordIdentity` field carries it) and whose `version` is optional.
 * Used wherever a payload must name a specific existing record version -- a graph traversal
 * start point or edge endpoint, a context pack citation or source-version list -- without
 * asserting any supersession relationship.
 */
export interface RecordVersionReference {
  /**
   * Identifier of the referenced record.
   */
  readonly record_id: RecordId;
  /**
   * The exact referenced version.
   */
  readonly version: RecordVersion;
}

/**
 * A request to answer one runtime probe. Deliberately distinct from `RequestEnvelope`: it
 * carries no `operation`, no `input`, and no workspace or authority scoping, because a probe
 * must be answerable before those concepts apply.
 */
export interface ServiceProbeRequest {
  /**
   * Which probe is being requested.
   */
  readonly probe: ProbeKind;
  /**
   * Identifies this probe attempt, for correlating logs across a health check.
   */
  readonly request_id?: RequestId;
  /**
   * Relative time budget for answering the probe.
   */
  readonly deadline_ms?: DurationMs;
}

/**
 * The health of one subsystem a readiness or health probe inspected.
 */
export interface ServiceComponentStatus {
  /**
   * Stable identifier of the subsystem, such as `storage` or `search_index`.
   */
  readonly id: Identifier;
  /**
   * Outcome for this subsystem.
   */
  readonly status: ProbeStatus;
  /**
   * When this subsystem was last checked.
   */
  readonly observed_at: Timestamp;
  /**
   * Human-readable explanation. Not a stable interface.
   */
  readonly message?: string;
  /**
   * Optional structured detail.
   */
  readonly details?: JsonObject;
}

/**
 * Evidence naming the operating-system process that published a descriptor, so a stale
 * descriptor left behind by a crashed instance can be recognized rather than trusted. All three
 * facts are required together: a pid alone is ambiguous once the host reuses it, and it is the
 * start time and boot identifier that make the triple a stable identity. This names a process;
 * it does not grant access to one, and it never carries a filesystem or database location.
 */
export interface ServiceProcessEvidence {
  /**
   * Operating-system process identifier of the running service instance.
   */
  readonly pid: number;
  /**
   * Opaque, platform-defined process start time, compared only for equality against a later
   * reading of the same pid. Never parsed: its spelling is whatever the host platform reports.
   */
  readonly start_time: string;
  /**
   * Identifies the host boot during which the pid and start time were read, so the pair is
   * never matched against a reading taken after a restart.
   */
  readonly boot_id: Identifier;
}

/**
 * Server-produced, validated authority actually applied to a request. This is the only authority
 * statement a client may trust.
 */
export interface GrantedAuthority {
  /**
   * Validated principal the operation executed as.
   */
  readonly principal_id: Identifier;
  /**
   * Validated roles held by the principal.
   */
  readonly roles: readonly Identifier[];
  /**
   * Capability references actually granted for this request.
   */
  readonly capabilities: readonly CapabilityRef[];
}

/**
 * The outcome of version negotiation: what was selected, what is supported, and what the caller
 * must do next.
 */
export interface CompatibilityMetadata {
  /**
   * API contract version the server applied to this request.
   */
  readonly selected_api_version: ContractVersion;
  /**
   * Workspace format version the server applied to this request.
   */
  readonly selected_workspace_version: ContractVersion;
  /**
   * Inclusive API contract version window the server supports.
   */
  readonly supported_api_versions: VersionWindow;
  /**
   * Inclusive workspace format version window the server supports.
   */
  readonly supported_workspace_versions: VersionWindow;
  /**
   * Open compatibility status. Known values are listed in `x-omnivia-compatibility-statuses`;
   * unknown values must be preserved.
   */
  readonly status: OpenCode;
  /**
   * Upgrade posture implied by the negotiated versions.
   */
  readonly upgrade_state: UpgradeState;
  /**
   * Deprecations that apply to the negotiated versions.
   */
  readonly deprecations: readonly Deprecation[];
}

/**
 * The three capability views a caller needs: what the server can do, what this caller is
 * allowed, and the intersection actually usable. `effective` is always `supported` intersected
 * with `granted`; it is never widened by a claim.
 */
export interface CapabilitySet {
  /**
   * Capabilities this server build implements.
   */
  readonly supported: readonly CapabilityRef[];
  /**
   * Capabilities policy grants to this caller and workspace.
   */
  readonly granted: readonly CapabilityRef[];
  /**
   * Capabilities actually usable on this request: `supported` intersected with `granted`.
   */
  readonly effective: readonly CapabilityRef[];
}

/**
 * One concrete component release, the contract and workspace-format version windows it supports,
 * and how thoroughly that support has been qualified. An entry's mere presence is not itself
 * support evidence; `qualification_state` is.
 */
export interface ReleaseCompatibilityEntry {
  /**
   * Which component this release identity names, such as `core` or `runtime` or `cli` or `mcp`
   * or `sdk`.
   */
  readonly component: ComponentKind;
  /**
   * Concrete build version of this release.
   */
  readonly release_version: ReleaseVersion;
  /**
   * Contract version this release applies by default.
   */
  readonly api_version: ContractVersion;
  /**
   * Inclusive API contract version window this release supports.
   */
  readonly supported_api_versions: VersionWindow;
  /**
   * Inclusive workspace format version window this release supports.
   */
  readonly supported_workspace_versions: VersionWindow;
  /**
   * How thoroughly this release's declared support windows have actually been verified. A
   * development or unverified state must never be read as supported.
   */
  readonly qualification_state: QualificationState;
}

/**
 * One capability's compatibility posture: the concrete capability and version, when it was
 * introduced, and how thoroughly it has been qualified. Mirrors `ReleaseCompatibilityEntry`'s
 * qualification discipline: presence in this list is not itself support evidence.
 */
export interface CapabilityCompatibilityEntry {
  /**
   * The concrete capability and version this entry describes.
   */
  readonly capability: CapabilityRef;
  /**
   * Contract version in which this capability first appeared.
   */
  readonly introduced_in: ContractVersion;
  /**
   * How thoroughly this capability has actually been verified. A development or unverified
   * state must never be read as supported.
   */
  readonly qualification_state: QualificationState;
}

/**
 * One operation's compatibility posture: when it was introduced, its current lifecycle state,
 * and how thoroughly that lifecycle state has actually been verified. `state` and
 * `qualification_state` are deliberately separate axes: an operation's mere presence in this
 * list, or its lifecycle being `stable`, must never be read as evidence that it has been
 * qualified -- only `qualification_state` is that evidence.
 */
export interface OperationCompatibilityEntry {
  /**
   * Operation this entry describes.
   */
  readonly operation: OperationName;
  /**
   * Contract version in which this operation first appeared.
   */
  readonly introduced_in: ContractVersion;
  /**
   * This operation's current lifecycle state (`stable`, `experimental`, `deprecated`,
   * `removed`). Independent of `qualification_state`: a `stable` lifecycle state is not itself
   * qualification evidence.
   */
  readonly state: OperationCompatibilityState;
  /**
   * How thoroughly this operation has actually been verified, independent of its lifecycle
   * `state`. A development or unverified state must never be read as supported.
   */
  readonly qualification_state: QualificationState;
  /**
   * Deprecation notice, present when `state` marks this operation as deprecated.
   */
  readonly deprecation?: Deprecation;
}

/**
 * One citation binding a pack section to an exact L0 evidence artifact, optionally at a precise
 * location inside it. Possessing this citation grants no access on its own: following it always
 * requires fresh authorization against the cited evidence.
 */
export interface ContextPackEvidenceCitation {
  /**
   * Identifier of this citation within this pack, unique across `citations` and referenced by
   * section, conflict, and uncertainty citation-id lists.
   */
  readonly citation_id: Identifier;
  /**
   * The exact evidence artifact and content checksum this citation points at.
   */
  readonly evidence_reference: ContextPackEvidenceReference;
  /**
   * Optional bounded, opaque locator within the cited artifact's content, such as a JSON
   * Pointer. v1 states its length bound and nothing about its syntax: resolving a locator is
   * target-specific and is not guessed at by this provider-neutral layer.
   */
  readonly content_pointer?: string;
  /**
   * Optional addressable position within the cited artifact, reusing the same structural and
   * semantic rules `records.SourceSpan` already carries rather than restating them as an
   * unvalidated string.
   */
  readonly source_span?: SourceSpan;
  /**
   * Optional short excerpt substantiating what this citation supports. Not a stable interface.
   */
  readonly excerpt?: string;
}

/**
 * One citation binding a pack section to an exact governed record version, optionally at a
 * precise location inside it. Possessing this citation grants no access on its own: following it
 * always requires fresh authorization against the cited record.
 */
export interface ContextPackRecordCitation {
  /**
   * Identifier of this citation within this pack, unique across `citations` and referenced by
   * section, conflict, and uncertainty citation-id lists.
   */
  readonly citation_id: Identifier;
  /**
   * The exact governed record version this citation points at.
   */
  readonly record_reference: RecordVersionReference;
  /**
   * Optional bounded, opaque locator within the cited record's opaque content, such as a JSON
   * Pointer. v1 states its length bound and nothing about its syntax: resolving a locator is
   * target-specific and is not guessed at by this provider-neutral layer.
   */
  readonly content_pointer?: string;
  /**
   * Optional addressable position within the cited record, reusing the same structural and
   * semantic rules `records.SourceSpan` already carries rather than restating them as an
   * unvalidated string.
   */
  readonly source_span?: SourceSpan;
  /**
   * Optional short excerpt substantiating what this citation supports. Not a stable interface.
   */
  readonly excerpt?: string;
}

/**
 * One member of the authorized candidate frontier: either an L0 evidence candidate or a governed
 * record candidate, never both and never neither. Two distinct object shapes rather than one
 * shape with optional pointers, so what a candidate names is settled structurally by the
 * document instead of by a later agreement check.
 */
export type ContextPackAuthorizedCandidate = ContextPackAuthorizedEvidenceCandidate | ContextPackAuthorizedRecordCandidate;

/**
 * Everything the server needs to route, scope, bound, and audit a request, independent of the
 * operation payload.
 */
export interface RequestMetadata {
  /**
   * Identifies this request attempt.
   */
  readonly request_id: RequestId;
  /**
   * Groups retries and related requests into one logical operation.
   */
  readonly correlation_id: CorrelationId;
  /**
   * Distributed-trace identifier. Diagnostic only.
   */
  readonly trace_id: TraceId;
  /**
   * API contract version the caller is written against.
   */
  readonly api_version: ContractVersion;
  /**
   * Self-declared calling client. Not an authorization input.
   */
  readonly client: ClientIdentity;
  /**
   * Workspace the request is scoped to. Present only for workspace-scoped operations; an
   * installation-scoped operation must never carry it. Whether a given operation requires or
   * forbids it is a semantic concern (see `omnivia_core.contracts.v1.semantics`), not a wire-
   * shape one.
   */
  readonly workspace_id?: WorkspaceId;
  /**
   * Scopes the caller is exercising. Scopes narrow authority; they never widen it.
   */
  readonly scopes: readonly Scope[];
  /**
   * Why the caller is making this request. A rejected purpose is `invalid_purpose`.
   */
  readonly purpose: Purpose;
  /**
   * Relative time budget for the request. Expressed as a budget, not an absolute instant, so
   * no clock synchronization is assumed.
   */
  readonly deadline_ms?: DurationMs;
  /**
   * Makes a mutation safe to retry.
   */
  readonly idempotency_key?: IdempotencyKey;
  /**
   * Optimistic-concurrency precondition for a mutation.
   */
  readonly mutation_precondition?: MutationPrecondition;
  /**
   * Capabilities this request depends on, with the minimum version the caller can work with.
   */
  readonly required_capabilities: readonly CapabilityRequirement[];
  /**
   * UNTRUSTED caller claim about the acting principal. The server must validate it before it
   * becomes authority.
   */
  readonly principal_claim?: PrincipalClaim;
}

/**
 * Input for `evidence.search`. Workspace-scoped: the workspace is the request envelope's
 * selected workspace; this payload never carries a second, independent workspace identifier.
 */
export interface EvidenceSearchInput {
  /**
   * Normalized search query.
   */
  readonly query: EvidenceQuery;
  /**
   * Restrict results to this sensitivity classification, when set.
   */
  readonly sensitivity?: OpenCode;
  /**
   * Whether tombstoned evidence artifacts may be included. Absent means the server's default
   * (excluded).
   */
  readonly include_tombstoned?: boolean;
  /**
   * Bounded maximum number of evidence artifacts to return in this page.
   */
  readonly limit?: PageLimit;
  /**
   * Continuation position from a prior page, when paging.
   */
  readonly page?: PageMetadata;
}

/**
 * Input for `graph.traverse`. Workspace-scoped: the workspace is the request envelope's selected
 * workspace; this payload never carries a second, independent workspace identifier. Absent
 * `view` defaults to `current_canonical`; only an explicit `view` selector may request
 * `candidates` or `history`. Absent `direction` defaults to `outbound`.
 */
export interface GraphTraversalInput {
  /**
   * One or more starting record versions to traverse from. Every one of them is returned at
   * depth 0 on a first page, and depth 0 is exactly this set: no other node ever carries it.
   */
  readonly start: readonly RecordVersionReference[];
  /**
   * Which direction to follow relations in. Absent means `outbound`.
   */
  readonly direction?: GraphDirection;
  /**
   * Restrict traversal to these relation types, when set. Absent means every relation type;
   * present means a bounded, non-empty set of distinct types, since an empty filter would ask
   * for nothing and a repeated type states the same restriction twice.
   */
  readonly relation_types?: readonly GraphRelationType[];
  /**
   * Restrict traversal to this domain scope, when set.
   */
  readonly domain_scope?: RecordDomainScope;
  /**
   * Which slice of records' versions to consider. Absent defaults to `current_canonical`;
   * requesting `candidates` or `history` requires this field to be set explicitly.
   */
  readonly view?: GovernedRecordView;
  /**
   * Caller-requested point in time for a reproducible historical traversal, when set. Distinct
   * from the response's own `canonical_resolution_time`: this is what the caller asked for,
   * not what the server actually used.
   */
  readonly as_of?: Timestamp;
  /**
   * Bounded maximum traversal depth requested. Absent means the server's default depth.
   */
  readonly depth_limit?: GraphDepthLimit;
  /**
   * Bounded maximum number of nodes requested. Absent means the server's default node limit.
   * When set it must be at least the number of `start` seeds: a first page owes every seed at
   * depth 0 and may return no more nodes than the limit, so a smaller one asks for a result no
   * traversal could return.
   */
  readonly node_limit?: PageLimit;
  /**
   * Bounded maximum number of edges requested. Absent means the server's default edge limit.
   */
  readonly edge_limit?: PageLimit;
  /**
   * Continuation position from a prior page, when paging a traversal whose ordering can be
   * deterministically continued. Present states that this is a continuation page: its seeds
   * were already returned by an earlier page, so they are not returned again at any depth and
   * every node it carries sits at depth 1 or deeper.
   */
  readonly page?: PageMetadata;
}

/**
 * One execution attempt of a job. A job that is retried has more than one attempt. An attempt
 * exists because execution started, so `queued` is not an attempt state: waiting to run is a
 * state of the *job*, not of an execution of it, and an attempt numbered against a job that
 * never ran would make the attempt history unreadable. Within one job's history attempts are
 * numbered `1..N` contiguously, never overlap, and only a `failed` or `cancelled` attempt may be
 * followed by another one -- a `succeeded` attempt is final.
 */
export interface JobAttempt {
  /**
   * 1-based ordinal of this attempt.
   */
  readonly attempt_number: number;
  /**
   * When this attempt started.
   */
  readonly started_at: Timestamp;
  /**
   * When this attempt finished, when it has.
   */
  readonly finished_at?: Timestamp;
  /**
   * State this attempt reached.
   */
  readonly state: JobState;
  /**
   * The failure this attempt ended with, when it failed.
   */
  readonly error?: ApiError;
}

/**
 * Input for `import.start`. Carries exactly one thing: the immutable descriptor of an already-
 * staged source. Workspace-scoped through the request envelope's selected workspace, so this
 * payload never carries a second, independent workspace identifier, and it accepts no path, URL,
 * inline archive, credential, parser implementation name, or runtime/storage option.
 */
export interface ImportStartInput {
  /**
   * The immutable staged source this import reads.
   */
  readonly source: ImportSourceDescriptor;
}

/**
 * The typed terminal result of a successful `ingestion.import` job: what was imported, and what
 * it produced. It reports the creation of L0 evidence only -- candidate extraction and any
 * governed record that later cites this evidence are separate, later operations, so nothing here
 * asserts that knowledge was proposed, approved, or accepted. `discovered_items` is the total
 * the import saw and must equal `evidence_records_created + skipped_items + failed_items`: an
 * item is accounted for exactly once, and a count that does not add up is a report of an import
 * nobody can audit. `partial` is not an independent claim either; it is exactly `failed_items >
 * 0`. `source` is byte-for-byte the descriptor `import.start` accepted.
 */
export interface ImportCompletionResult {
  /**
   * Identifier of this import run, equal to the job's own `job_id`. Typed as `OpaqueToken` for
   * exactly that reason: `JobIdentity.job_id` is an opaque server-issued token, and a run id
   * spelled in any narrower vocabulary could not state the equality for every job id the
   * contract admits. The same run the L0 evidence this run produced points back to through
   * `EvidenceArtifact.import_run_id`, so evidence traces back to the run that created it.
   */
  readonly import_run_id: OpaqueToken;
  /**
   * The immutable staged source this run read, exactly as accepted by `import.start`.
   */
  readonly source: ImportSourceDescriptor;
  /**
   * Total items the run discovered in the staged source.
   */
  readonly discovered_items: number;
  /**
   * Discovered items that became L0 evidence artifacts. Never a count of governed records.
   */
  readonly evidence_records_created: number;
  /**
   * Discovered items the run deliberately did not import, such as an unchanged item already
   * present.
   */
  readonly skipped_items: number;
  /**
   * Discovered items the run tried and failed to import.
   */
  readonly failed_items: number;
  /**
   * True when the run completed with failures. Exactly `failed_items > 0`: a successful job
   * may still be a partial import, and a caller must not read success as completeness.
   */
  readonly partial: boolean;
}

/**
 * Input for `job.events`: a bounded, snapshot-stable read of one job's ordered event stream. A
 * request carrying no `page` starts a new pagination session and captures the job's current
 * event count as that session's snapshot; a request carrying one continues the session that
 * token names and can never widen it. Transport-level streaming is out of scope: this is a paged
 * read, not a subscription. Workspace-scoped through the request envelope's selected workspace,
 * so this payload never carries a second, independent workspace identifier.
 */
export interface JobEventsInput {
  /**
   * Opaque identifier of the job whose events to read.
   */
  readonly job_id: OpaqueToken;
  /**
   * Bounded maximum number of events to return in this page.
   */
  readonly limit?: PageLimit;
  /**
   * Continuation position from a prior page of the same pagination session. Absent means start
   * a new session at the first page.
   */
  readonly page?: PageMetadata;
}

/**
 * Result of `job.events`: one page of a snapshot-stable event read. `snapshot_event_count` is
 * the event count captured when this pagination session began, so the session's sequences are
 * exactly `0 .. snapshot_event_count - 1` and events recorded after the snapshot never appear in
 * it; the same count is repeated on every page of the session and never changes within one. A
 * fresh tokenless request captures a new snapshot and may see more. Page events are strictly
 * increasing, duplicate-free, and contiguous from the position the request continued from.
 * `page` is always present: a continuation token means more of the snapshot remains, and no
 * token means the snapshot is exhausted.
 */
export interface JobEventsResult {
  /**
   * Opaque identifier of the job these events belong to. Echoes the request.
   */
  readonly job_id: OpaqueToken;
  /**
   * Events in this page, in ascending sequence order.
   */
  readonly events: readonly JobEvent[];
  /**
   * Number of events captured when this pagination session began. Constant across every page
   * of one session.
   */
  readonly snapshot_event_count: number;
  /**
   * Position within this pagination session: a continuation token when more of the captured
   * snapshot remains, and no token when it is exhausted.
   */
  readonly page: PageMetadata;
}

/**
 * Input for `knowledge.search`. Workspace-scoped: the workspace is the request envelope's
 * selected workspace; this payload never carries a second, independent workspace identifier.
 * Absent `view` defaults to `current_canonical`; only an explicit `view` selector may request
 * `candidates` or `history`, so a caller can never receive candidate, rejected, superseded, or
 * otherwise non-canonical governed knowledge by omission.
 */
export interface KnowledgeSearchInput {
  /**
   * Normalized search query.
   */
  readonly query: MemoryQuery;
  /**
   * Requested result order. Absent means the server's default order.
   */
  readonly order?: MemorySearchOrder;
  /**
   * Which slice of records' versions to consider. Absent defaults to `current_canonical`;
   * requesting `candidates` or `history` requires this field to be set explicitly.
   */
  readonly view?: GovernedRecordView;
  /**
   * Restrict results to this record type, when set.
   */
  readonly record_type?: GovernedRecordType;
  /**
   * Restrict results to this domain scope, when set.
   */
  readonly domain_scope?: RecordDomainScope;
  /**
   * Bounded maximum number of records to return in this page.
   */
  readonly limit?: PageLimit;
  /**
   * Continuation position from a prior page, when paging.
   */
  readonly page?: PageMetadata;
}

/**
 * Input for `knowledge.propose`: transitions an existing proposed (`l1`/`proposed`) record into
 * a candidate (`l1`/`candidate`) awaiting a governance decision. Never duplicates
 * `memory.create`: it identifies an already-existing record by `record_id` rather than proposing
 * new content, and never carries content, evidence, or assertion fields. The target version is
 * the envelope's `MutationPrecondition.record_version`, not duplicated here. Like every other
 * governance transition, the rationale is required: no governance decision on this contract is
 * ever recorded without an explicit, auditable reason.
 */
export interface KnowledgeProposeInput {
  /**
   * Identifier of the existing proposed record to transition into a candidate.
   */
  readonly record_id: RecordId;
  /**
   * Reason this record is being put forward as a candidate.
   */
  readonly rationale: GovernanceRationale;
}

/**
 * Input for `candidate.approve`: creates a new accepted (`l2`/`accepted`) governed version of an
 * existing candidate record, with reviewer authority attributed server-side. The target version
 * is the envelope's `MutationPrecondition.record_version`, not duplicated here. Carries no
 * authority-level, reviewer identity, or governance-state field of its own -- only the explicit,
 * auditable rationale for the decision.
 */
export interface CandidateApproveInput {
  /**
   * Identifier of the candidate record to approve.
   */
  readonly record_id: RecordId;
  /**
   * Reason this candidate was approved.
   */
  readonly rationale: GovernanceRationale;
}

/**
 * Input for `candidate.reject`: creates a new rejected (`l1`/`rejected`) governed version of an
 * existing candidate record, with reviewer authority attributed server-side. The target version
 * is the envelope's `MutationPrecondition.record_version`, not duplicated here. `rejected` is
 * never treated as a favourable or accepted authority decision.
 */
export interface CandidateRejectInput {
  /**
   * Identifier of the candidate record to reject.
   */
  readonly record_id: RecordId;
  /**
   * Reason this candidate was rejected.
   */
  readonly rationale: GovernanceRationale;
}

/**
 * Input for `memory.list`. Workspace-scoped: the workspace is the request envelope's selected
 * workspace; this payload never carries a second, independent workspace identifier.
 */
export interface MemoryListInput {
  /**
   * Which slice of records' versions to consider. Absent defaults to `current_canonical`.
   */
  readonly view?: GovernedRecordView;
  /**
   * Restrict results to this record type, when set.
   */
  readonly record_type?: GovernedRecordType;
  /**
   * Bounded maximum number of records to return in this page.
   */
  readonly limit?: PageLimit;
  /**
   * Continuation position from a prior page, when paging.
   */
  readonly page?: PageMetadata;
}

/**
 * Input for `memory.search`. Workspace-scoped: the workspace is the request envelope's selected
 * workspace; this payload never carries a second, independent workspace identifier. Carries the
 * normalized query and requested order a later stateful conformance slice will bind an issued
 * continuation token to, alongside principal/workspace/operation binding.
 */
export interface MemorySearchInput {
  /**
   * Normalized search query.
   */
  readonly query: MemoryQuery;
  /**
   * Requested result order. Absent means the server's default order.
   */
  readonly order?: MemorySearchOrder;
  /**
   * Which slice of records' versions to consider. Absent defaults to `current_canonical`.
   */
  readonly view?: GovernedRecordView;
  /**
   * Restrict results to this record type, when set.
   */
  readonly record_type?: GovernedRecordType;
  /**
   * Bounded maximum number of records to return in this page.
   */
  readonly limit?: PageLimit;
  /**
   * Continuation position from a prior page, when paging.
   */
  readonly page?: PageMetadata;
}

/**
 * The full declared contract characteristics of one operation: its scope, payload schemas,
 * required capability, side effects, and job/pagination/idempotency/precondition/audit/error
 * posture. Every entry of this document's `x-omnivia-operation-catalogue` is exactly one of
 * these, so the catalogue is metadata a caller can read rather than behaviour it has to
 * discover. `allowed_errors` is materialized per operation: reusable error postures exist in the
 * specification that froze this catalogue, but never on the wire, so no decoder has to resolve a
 * profile name to know what an operation may fail with.
 */
export interface OperationMetadata {
  /**
   * Operation this metadata describes.
   */
  readonly name: OperationName;
  /**
   * What this operation requires and touches.
   */
  readonly scope: OperationScope;
  /**
   * Reference to the JSON Schema governing this operation's `input` payload.
   */
  readonly input_schema_ref: SchemaReference;
  /**
   * Reference to the JSON Schema governing this operation's `result` payload.
   */
  readonly result_schema_ref: SchemaReference;
  /**
   * The capability, and minimum version, this operation depends on.
   */
  readonly required_capability: CapabilityRequirement;
  /**
   * How this operation completes and whether it may return asynchronous work.
   */
  readonly job: OperationJobMetadata;
  /**
   * Whether and how this operation's results are paginated.
   */
  readonly pagination: OperationPaginationMetadata;
  /**
   * How this operation may safely be retried.
   */
  readonly idempotency: OperationIdempotencyMetadata;
  /**
   * How this operation uses optimistic-concurrency preconditions.
   */
  readonly precondition: OperationPreconditionMetadata;
  /**
   * Whether and how this operation is audited.
   */
  readonly audit: OperationAuditMetadata;
  /**
   * The stable error codes this operation may fail with.
   */
  readonly allowed_errors: readonly ErrorCode[];
}

/**
 * A concrete piece of evidence supporting one claim in a record.
 */
export interface EvidenceReference {
  /**
   * The source this evidence was drawn from.
   */
  readonly source: SourceReference;
  /**
   * Addressable position within the source this evidence was drawn from, when known.
   */
  readonly span?: SourceSpan;
  /**
   * Optional short excerpt from the source substantiating the claim. Not a stable interface.
   */
  readonly excerpt?: string;
}

/**
 * The identity, version, governance layer, governance state, and currentness of one record
 * version.
 */
export interface RecordIdentity {
  /**
   * Identifier stable across every version of this record.
   */
  readonly record_id: RecordId;
  /**
   * Opaque version of this specific revision.
   */
  readonly version: RecordVersion;
  /**
   * Governance layer this record belongs to.
   */
  readonly layer: GovernanceLayer;
  /**
   * This record version's position in its own governance workflow, independent of `layer` and
   * `currentness`.
   */
  readonly governance_state: GovernanceState;
  /**
   * Whether this version is the active one.
   */
  readonly currentness: RecordCurrentness;
  /**
   * The earlier record version this version replaces, when this version is itself the newer
   * one.
   */
  readonly supersedes?: SupersessionReference;
  /**
   * The newer record version that replaced this one, present when `currentness` marks this
   * version as superseded.
   */
  readonly superseded_by?: SupersessionReference;
}

/**
 * The published coordination facts a client needs to find one running service instance and
 * decide whether it can talk to it, before any request is sent. Coordination data only: a
 * descriptor carries no bearer credential or token, no granted or effective capability
 * authority, no lease ownership, and no database or filesystem location. Holding one lets a
 * caller address an endpoint and negotiate versions; it authorizes nothing, and every authority
 * question is still settled by an authenticated request.
 */
export interface ServiceEndpointDescriptor {
  /**
   * Contract version of the descriptor shape itself, so a reader can tell a descriptor it
   * fully understands from a newer one it must read conservatively.
   */
  readonly descriptor_version: ContractVersion;
  /**
   * Workspace this service instance is serving.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifies this running service instance. A restart is a new instance and gets a new
   * identifier.
   */
  readonly service_instance_id: Identifier;
  /**
   * Identifies the installation the instance belongs to, which survives restarts and instance
   * identity changes.
   */
  readonly installation_id: Identifier;
  /**
   * Absolute URI a client connects to, scheme included. An address to dial, not a location to
   * open: it never names a database file or a workspace directory.
   */
  readonly endpoint_uri: string;
  /**
   * Wire protocol version spoken at this endpoint.
   */
  readonly protocol_version: ContractVersion;
  /**
   * Concrete server build version publishing this descriptor.
   */
  readonly server_version: ReleaseVersion;
  /**
   * Inclusive API contract version window this instance supports.
   */
  readonly supported_api_versions: VersionWindow;
  /**
   * Inclusive workspace format version window this instance supports.
   */
  readonly supported_workspace_versions: VersionWindow;
  /**
   * Concrete workspace format version the served workspace is stored at.
   */
  readonly workspace_format_version: ContractVersion;
  /**
   * Whether the instance is ready to serve requests now. A descriptor may be published before
   * readiness, so this stays a fact of its own rather than being implied by the descriptor
   * existing.
   */
  readonly ready: boolean;
  /**
   * Open, dot-namespaced code naming where the instance is in its lifecycle, such as
   * `starting` or `serving` or `draining`. Open by design; an unrecognized state must be
   * preserved and surfaced, not coerced to a known one.
   */
  readonly lifecycle_state: OpenCode;
  /**
   * Monotonic generation of this instance's claim on the workspace. A reader holding a lower
   * generation is looking at a superseded instance. It records which claim is newer; it
   * neither states nor transfers ownership of one.
   */
  readonly fencing_generation: number;
  /**
   * When this descriptor was published.
   */
  readonly published_at: Timestamp;
  /**
   * Local process evidence, when the publisher can observe it. Optional as a whole and
   * complete when present, so a reader never has to reason about a half-identified process.
   */
  readonly process?: ServiceProcessEvidence;
}

/**
 * The concrete workspace-format version a workspace is stored at, and the inclusive version
 * window this server build can read and write. Reuses the same `VersionWindow` and `OpenCode`
 * primitives `VersionCapabilityEnvelope` negotiates with, rather than inventing a second version
 * model.
 */
export interface WorkspaceCompatibility {
  /**
   * Concrete workspace-format version this workspace is currently stored at.
   */
  readonly workspace_format_version: ContractVersion;
  /**
   * Inclusive workspace-format version window this server build can read and write.
   */
  readonly supported_workspace_versions: VersionWindow;
  /**
   * Compatibility status of `workspace_format_version` against `supported_workspace_versions`.
   * Known values are listed in `compatibility.schema.json`'s `x-omnivia-compatibility-
   * statuses`; unknown values must be preserved.
   */
  readonly status: OpenCode;
}

/**
 * Input for `workspace.list`. Installation-scoped: carries no workspace identifier, since it
 * lists every workspace the caller's installation-level authority can see.
 */
export interface WorkspaceListInput {
  /**
   * Bounded maximum number of workspaces to return in this page.
   */
  readonly limit?: PageLimit;
  /**
   * Continuation position from a prior page, when paging.
   */
  readonly page?: PageMetadata;
}

/**
 * Everything a caller needs to reason about what this server accepted and what it can do.
 * Returned on every response, success or error.
 */
export interface VersionCapabilityEnvelope {
  /**
   * API contract version in force for this response.
   */
  readonly api_version: ContractVersion;
  /**
   * Concrete server build version. Diagnostic only; never negotiate on it.
   */
  readonly server_version: ReleaseVersion;
  /**
   * Workspace format version in force for this response.
   */
  readonly workspace_format_version: ContractVersion;
  /**
   * Negotiated version windows, status, upgrade posture, and deprecations.
   */
  readonly compatibility: CompatibilityMetadata;
  /**
   * Supported, granted, and effective capabilities.
   */
  readonly capabilities: CapabilitySet;
}

/**
 * The compatibility matrix foundation: every known release's supported version windows, every
 * known operation's introduction version, lifecycle state, and qualification state, and every
 * known capability's compatibility posture. This shape is not itself evidence that anything it
 * lists is supported -- each entry's own `qualification_state` is the only thing a caller may
 * treat as a support claim; neither an entry's mere presence nor an operation's lifecycle
 * `state` implies qualification, and an empty or unverified matrix must never be read as
 * 'nothing is unsupported'.
 */
export interface CompatibilityMatrix {
  /**
   * Known component releases and the version windows and qualification state they support.
   */
  readonly releases: readonly ReleaseCompatibilityEntry[];
  /**
   * Known operations and their compatibility posture.
   */
  readonly operations: readonly OperationCompatibilityEntry[];
  /**
   * Known capabilities and their compatibility posture.
   */
  readonly capabilities: readonly CapabilityCompatibilityEntry[];
}

/**
 * One exact citation in a pack: either an evidence citation or a governed-record citation, never
 * both and never neither. The two branches are distinct object shapes rather than one shape with
 * two optional pointers, so what a citation points at is settled structurally by the wire
 * document instead of being left to a semantic agreement check.
 */
export type ContextPackCitation = ContextPackEvidenceCitation | ContextPackRecordCitation;

/**
 * The complete authority context one Context Pack was produced under, recorded so the build can
 * be reproduced and audited. Historical reproducibility context only, and never a live grant:
 * possessing it authorizes nothing, and following any citation still requires fresh
 * authorization against the cited evidence or record. Recorded structurally rather than as an
 * opaque fingerprint so a reviewer can actually check which principal, roles, capabilities,
 * scopes, purpose, and policy versions were in force, instead of comparing two hashes and
 * learning only that they differ.
 */
export interface ContextPackAuthorizationContext {
  /**
   * The workspace this pack was produced for.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * The validated principal, roles, and capabilities the build actually executed under. A
   * historical record of that authority, not a restatement of it that a later reader may act
   * on.
   */
  readonly authority: GrantedAuthority;
  /**
   * The scopes in force for the build, in deterministic ascending order and free of
   * duplicates. Never empty: a build that names no scope states nothing checkable about what
   * narrowed it. Scopes narrow a request; recording them never widens what this artifact
   * permits.
   */
  readonly scopes: readonly Scope[];
  /**
   * The purpose-limitation token the build was performed under.
   */
  readonly purpose: Purpose;
  /**
   * Open map of policy or ACL name to the opaque policy version applied while producing this
   * pack. At least one policy must be named: a pack that states no policy version states
   * nothing checkable about what filtered it.
   */
  readonly policy_versions: Readonly<Record<string, OpaqueToken>>;
  /**
   * Always true: ACL and sensitivity authorization were applied to the candidate set before
   * ranking and selection, not merely afterwards. An attestation about how the build ran,
   * never itself a grant, and never an implication that filtering after ranking would have
   * sufficed.
   */
  readonly pre_ranking_authorization_enforced: boolean;
  /**
   * Digest of the authorized candidate set that ranking and selection actually ran over, so a
   * reproduction can prove it started from the same authorized material rather than a wider
   * one. Independently recomputable rather than merely stated: it is exactly the digest
   * `ContextPackAuthorizedCandidateSetManifest` defines, over the complete authorized frontier
   * frozen before the first ranking, reranking, selection, or budget decision. A verifier
   * checks it by recomputing that digest from a manifest supplied out of band and comparing;
   * reading this value back out of the pack and comparing it with itself verifies nothing.
   */
  readonly authorized_candidate_set_checksum: ContextPackDigest;
}

/**
 * The exact preimage `ContextPackAuthorizationContext.authorized_candidate_set_checksum` is a
 * digest of. It names the complete post-retrieval, post-request-scope, post-
 * workspace/scope/purpose/capability/policy/ACL/sensitivity-authorization candidate frontier,
 * frozen before the first ranking, reranking, selection, or budget decision: not the whole
 * workspace, and not merely the items a pack ended up selecting. Unauthorized, request-filtered,
 * tombstoned, and invalid-at-resolution material is absent; nothing may be introduced into a
 * pack after this frontier is frozen, so every selected item is a member of it under its own
 * exact partition. This is trusted in-process input to a verifier, never a response field and
 * never logged: it is the independent statement a checksum copied out of the pack itself could
 * never be. The digest is `sha256:` followed by the lowercase hex SHA-256 of the RFC 8785
 * canonical UTF-8 bytes of this document, with `candidates` sorted by partition and then by the
 * remaining identity members in the order they are declared, comparing each component by
 * unsigned UTF-16 code unit with no Unicode normalization. That sort makes the digest order-
 * insensitive; a duplicate is refused outright rather than collapsed, so sorting never has to
 * decide what a repeated identity meant. RFC 8785 orders object member names but never array
 * elements, so that element sort is part of this definition rather than of the canonicalization.
 * UTF-16 code-unit comparison is normative because it is the ordering RFC 8785 already imposes
 * on member names, and a preimage ordered by one rule and canonicalized under another would be
 * two rules pretending to be one; note that every v1 identity alphabet here is ASCII
 * (`EvidenceId`, `RecordId`), printable ASCII (`RecordVersion`), or an ASCII-restricted checksum
 * (`EvidenceChecksum`), so no valid v1 candidate can distinguish UTF-16 order from code-point
 * order -- the rule is stated for the canonicalizer it must agree with, not because a v1
 * identity could exercise the difference. The empty candidate set is valid and has a well-
 * defined digest.
 */
export interface ContextPackAuthorizedCandidateSetManifest {
  /**
   * Always `omnivia.context-pack.authorized-candidate-set.v1`, and part of the hashed preimage
   * rather than a header around it: a digest that did not cover the name of the thing it
   * digests could be replayed against a differently shaped set of the same members.
   */
  readonly format: string;
  /**
   * The workspace this frontier was authorized in, and the only domain separation the preimage
   * carries. Two workspaces that authorized identical identities must not share a digest.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * The complete authorized frontier, as a set: element order carries no meaning on the wire
   * and is normalized before hashing, so a shuffled manifest digests identically while a
   * changed, added, or removed identity never does. Free of duplicates -- an exactly repeated
   * tuple, one evidence identifier paired with two content checksums, or one governed
   * `(record_id, version)` repeated within a partition or across two governed partitions is a
   * contradiction rather than a set, and is refused. May be empty.
   */
  readonly candidates: readonly ContextPackAuthorizedCandidate[];
}

/**
 * A single application request: what to do, under what conditions, with what payload.
 */
export interface RequestEnvelope {
  /**
   * Operation to invoke.
   */
  readonly operation: OperationName;
  /**
   * Operation-independent request metadata.
   */
  readonly metadata: RequestMetadata;
  /**
   * Opaque operation payload.
   */
  readonly input: JsonObject;
}

/**
 * What a caller holds to track a job over time: its identity, current state, latest known
 * progress and attempt, and which control actions are available. `latest_attempt` is the job's
 * attempt *N*, not a history: a `running` job reports the running attempt it executes under, a
 * `succeeded` or `failed` job reports the finished attempt that produced that outcome, and a
 * `queued` job either has never executed (no attempt at all) or reports the finished
 * `failed`/`cancelled` attempt retained after an accepted `job.retry` scheduled recovery --
 * never a succeeded, running, queued, or unfinished one, since none of those describes a job
 * waiting to start.
 */
export interface JobHandle {
  /**
   * Identity of this job.
   */
  readonly identity: JobIdentity;
  /**
   * Current state of this job.
   */
  readonly state: JobState;
  /**
   * When this job was created.
   */
  readonly created_at: Timestamp;
  /**
   * When this job's state was last observed to change.
   */
  readonly updated_at: Timestamp;
  /**
   * Which control actions a caller may take on this job right now.
   */
  readonly control: JobControl;
  /**
   * Latest known progress, while running.
   */
  readonly progress?: JobProgress;
  /**
   * Most recent execution attempt.
   */
  readonly latest_attempt?: JobAttempt;
}

/**
 * The final outcome of a job that succeeded. Carries `result` and never `error` or
 * `cancellation`. Terminal success is typed rather than opaque: `result_kind` names which frozen
 * result shape `result` carries, so a caller reads a success payload by matching a declared kind
 * instead of guessing from the job kind. `result` stays an opaque JSON object in this document
 * because JSON Schema cannot bind it to a per-kind shape within the subset the generator
 * supports; `result_kind` and `semantics_jobs` (`omnivia_core.contracts.v1.semantics_jobs`)
 * still validate the frozen result-kind mapping. The operation catalogue additionally binds a
 * job-starting operation to its terminal result schema through
 * `OperationJobMetadata.terminal_result_schema_ref`. The one kind frozen in v1 is
 * `import_completion`: `import.start` binds it to `ImportCompletionResult`.
 */
export interface JobTerminalSuccess {
  /**
   * Identity of this job.
   */
  readonly identity: JobIdentity;
  /**
   * Terminal state the job reached, such as `succeeded`.
   */
  readonly state: JobState;
  /**
   * When the job reached its terminal state.
   */
  readonly finished_at: Timestamp;
  /**
   * Every execution attempt this job made, in order.
   */
  readonly attempts: readonly JobAttempt[];
  /**
   * Open code naming which frozen result shape `result` carries, such as `import_completion`.
   * Open by design; an unrecognized kind decodes and is preserved, but a caller must not
   * interpret `result` under a kind it does not know.
   */
  readonly result_kind: OpenCode;
  /**
   * The success payload, in the shape `result_kind` names. Opaque only in this document: a
   * `result_kind` of `import_completion` makes it exactly an `ImportCompletionResult`.
   */
  readonly result: JsonObject;
}

/**
 * The final outcome of a job that failed. Carries `error` and never `result` or `cancellation`.
 * `attempts` is non-empty -- a job cannot fail without having executed -- and `error` is exactly
 * the final attempt's own `error`: the failure that ended the last attempt is the failure that
 * ended the job, and two spellings of it that could disagree would leave a caller unable to say
 * which one is the outcome.
 */
export interface JobTerminalFailure {
  /**
   * Identity of this job.
   */
  readonly identity: JobIdentity;
  /**
   * Terminal state the job reached, such as `failed`.
   */
  readonly state: JobState;
  /**
   * When the job reached its terminal state.
   */
  readonly finished_at: Timestamp;
  /**
   * Every execution attempt this job made, in order.
   */
  readonly attempts: readonly JobAttempt[];
  /**
   * The terminal failure.
   */
  readonly error: ApiError;
}

/**
 * The final outcome of a job that was cancelled. Carries `cancellation` and never `result` or
 * `error`. `attempts` may be empty, and only here: a job cancelled while still queued never
 * executed, so it has no attempt to report. When it does carry attempts, the final one is the
 * `cancelled` attempt that ended it and finished when the job did.
 */
export interface JobTerminalCancellation {
  /**
   * Identity of this job.
   */
  readonly identity: JobIdentity;
  /**
   * Terminal state the job reached, such as `cancelled`.
   */
  readonly state: JobState;
  /**
   * When the job reached its terminal state.
   */
  readonly finished_at: Timestamp;
  /**
   * Every execution attempt this job made, in order.
   */
  readonly attempts: readonly JobAttempt[];
  /**
   * The explicit cancellation outcome.
   */
  readonly cancellation: JobCancellationOutcome;
}

/**
 * Who is asserting a governed record's claim, when, and on what evidence, plus the validity
 * window they propose for it. This is caller-supplied provenance for the claim -- carried into
 * `memory.create` and `record.supersede` inputs, and preserved on the resulting record's
 * `RecordProvenance` -- not the server-owned governance decision: it never carries authority
 * level, reviewer/policy identity, or any other field a least-authority-escalating mutation
 * input is forbidden from carrying. Defined here rather than in `memory.schema.json` so
 * `RecordProvenance` can preserve it without `records.schema.json` depending on a document that
 * already depends on it.
 */
export interface CandidateAssertion {
  /**
   * Principal or system asserting this candidate.
   */
  readonly actor_id: Identifier;
  /**
   * Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.
   */
  readonly actor_kind: OpenCode;
  /**
   * Open code naming the role the actor asserted this candidate under.
   */
  readonly actor_role: OpenCode;
  /**
   * When the actor asserted this candidate.
   */
  readonly asserted_at: Timestamp;
  /**
   * Start of the validity window the caller proposes for this candidate, when known. The
   * server remains the final authority on the validity window actually stored.
   */
  readonly proposed_valid_from?: Timestamp;
  /**
   * End of the validity window the caller proposes for this candidate, when bounded. The
   * server remains the final authority on the validity window actually stored.
   */
  readonly proposed_valid_until?: Timestamp;
  /**
   * Concrete evidence substantiating this assertion. May be empty only when the enclosing
   * input's `evidence_disposition` explicitly excuses it; enforcing that agreement is a
   * semantic-validation concern, not a wire-shape one.
   */
  readonly evidence: readonly EvidenceReference[];
}

/**
 * One step in a record's history: who or what did what, when, and -- for a governance transition
 * -- the explicit rationale it was taken under.
 */
export interface ProvenanceEntry {
  /**
   * Principal or system that performed this action.
   */
  readonly actor_id: Identifier;
  /**
   * Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.
   */
  readonly actor_kind: OpenCode;
  /**
   * Open code naming what happened, such as `created` or `modified` or `superseded`.
   */
  readonly action: OpenCode;
  /**
   * When this action occurred.
   */
  readonly occurred_at: Timestamp;
  /**
   * Open code naming why this action was taken, carried over verbatim from the
   * `GovernanceRationale.reason_code` the transition was requested under. Absent on ordinary
   * non-governance history, which never carries a rationale; a governance-transition event
   * must carry it, and requiring that is a semantic-validation concern, not a wire-shape one.
   */
  readonly reason_code?: OpenCode;
  /**
   * Bounded human-readable elaboration, carried over verbatim from the requesting
   * `GovernanceRationale.comment`. Absent exactly when that comment was absent. Not a stable
   * interface.
   */
  readonly reason_comment?: string;
  /**
   * Evidence supporting this action, when applicable. Bounded at the same 256 items
   * `CandidateAssertion.evidence` is, and deliberately so: a `record.supersede` transition
   * appends exactly one event whose evidence must equal the replacement claim's complete
   * assertion evidence, so a lower bound here would make an otherwise valid replacement
   * impossible to record.
   */
  readonly evidence?: readonly EvidenceReference[];
}

/**
 * The answer to one runtime probe. Deliberately distinct from `SuccessResponseEnvelope` /
 * `ErrorResponseEnvelope`: it carries no `result`/`error` branch and no negotiated authority,
 * because a probe answers a transport-level question, not an application operation.
 */
export interface ServiceProbeResult {
  /**
   * Echoes the probe that was requested.
   */
  readonly probe: ProbeKind;
  /**
   * Overall outcome for this probe.
   */
  readonly status: ProbeStatus;
  /**
   * Concrete server build version answering the probe.
   */
  readonly server_version: ReleaseVersion;
  /**
   * API contract version this server build implements.
   */
  readonly api_version: ContractVersion;
  /**
   * When this probe was answered.
   */
  readonly observed_at: Timestamp;
  /**
   * Per-subsystem detail, for a health or readiness probe.
   */
  readonly components?: readonly ServiceComponentStatus[];
  /**
   * Capabilities this server build implements, for a discovery probe. This is a runtime
   * discovery fact only: an unauthenticated probe has no caller and no workspace to authorize
   * against, so it must never state `granted` or `effective` authority. A caller must still
   * negotiate `CapabilitySet.granted`/`effective` through an authenticated request.
   */
  readonly supported_capabilities?: readonly CapabilityRef[];
  /**
   * Endpoint descriptor for this service instance, for a discovery probe. Like
   * `supported_capabilities`, it is a runtime discovery fact: it states what this build is and
   * where it can be reached, never what the caller may do. Optional, because a health or
   * readiness probe answers without one.
   */
  readonly descriptor?: ServiceEndpointDescriptor;
  /**
   * Optional structured detail.
   */
  readonly details?: JsonObject;
}

/**
 * A workspace's identity, display name, lifecycle status, format compatibility, and lifecycle
 * timestamps.
 */
export interface WorkspaceDescriptor {
  /**
   * Stable, server-assigned identifier of this workspace.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Human-readable workspace name.
   */
  readonly display_name: string;
  /**
   * Current lifecycle status of this workspace.
   */
  readonly status: WorkspaceStatus;
  /**
   * Concrete workspace-format version and the compatibility window it falls within.
   */
  readonly compatibility: WorkspaceCompatibility;
  /**
   * When this workspace was created.
   */
  readonly created_at: Timestamp;
  /**
   * When this workspace's descriptor was last updated, when known.
   */
  readonly updated_at?: Timestamp;
}

/**
 * Everything a second build needs to reproduce one Context Pack byte for byte: the pack format
 * version, the builder, the normalized request, the authority context the build ran under, the
 * exact evidence and record versions selected, the projection the read was served from, the
 * retrieval/ranking/reranking/selection/tokenizer/summarizer/model versions applied, the instant
 * the canonical knowledge was resolved at, and the canonicalization and checksum that make the
 * result content-addressed. With every one of these unchanged, rebuilding must reproduce the
 * identical pack. Carries no audit reference: the response envelope owns audit linkage, and
 * folding a per-request audit identifier into a content-addressed artifact would make two
 * identical builds hash differently.
 */
export interface ContextPackReproducibility {
  /**
   * The Context Pack artifact format this pack is written in. Frozen at `1.0` for v1: the
   * checksum rule below is defined against exactly this format, so a reader must know which
   * format it is verifying before it verifies anything. Format `1.0` also pins the numeric
   * admission profile the canonicalization runs under -- lossless binary64. Every number in an
   * admitted pack must be a finite IEEE 754 binary64 value, and an integer is admitted only
   * when it converts to binary64 without loss, so `2**53 + 1` is refused rather than silently
   * signed under the name of `2**53`; non-finite values (NaN, the infinities) have no JSON
   * form and are refused outright. This belongs to the format rather than to the checksum
   * because it decides *which documents have a canonical form at all*: two implementations
   * that agreed on the hash but disagreed on whether a given number was admissible would not
   * agree on which packs exist. A future format may widen or narrow that profile, which is
   * exactly why the format is named inside the artifact and read before anything is verified.
   */
  readonly pack_format_version: ContractVersion;
  /**
   * Version of the pack builder that assembled this artifact.
   */
  readonly builder_version: Identifier;
  /**
   * The exact normalized request this pack was built from.
   */
  readonly normalized_request: ContextPackNormalizedRequest;
  /**
   * The complete authority context the build ran under. Historical reproducibility context,
   * never a live grant.
   */
  readonly authorization_context: ContextPackAuthorizationContext;
  /**
   * Exactly the L0 evidence identities this pack selected, in deterministic ascending order by
   * identifier then checksum, with no duplicate, addition, or omission. Not a superset of what
   * was selected and not a summary of it: the set itself, so a reproduction can be checked
   * rather than believed.
   */
  readonly evidence_versions: readonly ContextPackEvidenceReference[];
  /**
   * Exactly the union of the governed-record identities this pack selected across `records`,
   * `history`, and `context_models`, in deterministic ascending order by record identifier
   * then version, with no duplicate, addition, or omission.
   */
  readonly record_versions: readonly RecordVersionReference[];
  /**
   * The projection versions and watermarks this pack was actually served from, under the same
   * strict rule every other projection-served read in this contract states.
   */
  readonly freshness: ProjectionFreshness;
  /**
   * Version of the retrieval configuration that produced the candidate set.
   */
  readonly retrieval_version: Identifier;
  /**
   * Version of the ranking configuration applied to the authorized candidate set.
   */
  readonly ranking_version: Identifier;
  /**
   * Version of the reranking configuration applied after ranking.
   */
  readonly reranking_version: Identifier;
  /**
   * Version of the selection configuration that chose what fit the budget.
   */
  readonly selection_version: Identifier;
  /**
   * Identifier of the tokenizer every `token_count` in this pack was measured with.
   */
  readonly tokenizer_id: Identifier;
  /**
   * Version of that tokenizer. Token counts are only reproducible against an exact tokenizer
   * identity and version.
   */
  readonly tokenizer_version: Identifier;
  /**
   * Version of the summarizer applied while producing this pack, or the literal `disabled`
   * when none was used. Required either way: an absent field would leave a reader unable to
   * tell a build that summarized nothing from one whose summarizer was simply never recorded.
   */
  readonly summarizer_version: Identifier;
  /**
   * Open map of model role to the exact model version used in that role. Required but allowed
   * to be empty, which is how a build that used no model at all states so explicitly rather
   * than by omission.
   */
  readonly model_versions: Readonly<Record<string, Identifier>>;
  /**
   * The instant the canonical knowledge in this pack was resolved at. Every selected item is
   * judged current, historical, valid, or superseded against exactly this instant, and it is
   * additionally an inclusive upper bound on every event and provenance instant a selected
   * item carries: equality passes, strictly later is refused. For a selected
   * `evidence.EvidenceArtifact` that covers `temporal.event_at`, `observed_at`, `ingested_at`,
   * and `recorded_at`, `source.retrieved_at`, every `provenance_history[].occurred_at`, and
   * every `provenance_history[].evidence[].source.retrieved_at`. For a selected
   * `memory.GovernedRecord` in `records`, `history`, or `context_models` it covers
   * `provenance.temporal.event_at`, `observed_at`, `ingested_at`, and `recorded_at`, every
   * `provenance.sources[].retrieved_at`, every `provenance.history[].occurred_at`, every
   * `provenance.history[].evidence[].source.retrieved_at`, `provenance.assertion.asserted_at`,
   * every `provenance.assertion.evidence[].source.retrieved_at`, and
   * `provenance.extraction.extracted_at`. It deliberately does not bound
   * `provenance.assertion.proposed_valid_from` or `proposed_valid_until`: those are proposed
   * effective dates, and an assertion about the future is a claim rather than an act that had
   * not happened. This bound applies to selection into a pack only; the generic record and
   * evidence rules are unchanged, and an out-of-range instant is a refusal to select, never a
   * repair -- provenance is append-only, so nothing is dropped or truncated to make an item
   * selectable.
   */
  readonly canonical_resolution_time: Timestamp;
  /**
   * When this pack was generated. Equal to `canonical_resolution_time`: a deterministic build
   * is logically complete at the instant it resolved at, and letting wall-clock generation
   * time drift from it would make two otherwise identical builds hash differently.
   */
  readonly generated_at: Timestamp;
  /**
   * Open code naming the canonicalization the checksum below was computed over. Frozen at
   * `rfc8785` in v1: a checksum is only checkable against a stated, exactly specified
   * canonical form, and an unrecognized value fails closed rather than being verified under a
   * guessed one. Pack format 1.0 applies RFC 8785 byte serialization to an admitted I-JSON
   * data model: every number must be a finite binary64 before canonicalization, and any number
   * token written in integer form -- and any direct host-language integral value -- is
   * admitted only when converting it to binary64 and back to the mathematical integer is
   * exact. Decimal and exponent tokens are read as finite binary64 under ordinary JCS rules,
   * with no requirement of an exact decimal rational representation. So `9007199254740992` and
   * `1152921504606846976` are admitted while `9007199254740993` and `1152921504606846977` are
   * refused, `1e400` is refused as non-finite, and `0.1` and `1e+21` are admitted. This is an
   * admission rule, not a safe-integer range: exact larger integers such as powers of two stay
   * valid. It refuses silent rounding of an integer identity or count without changing the RFC
   * 8785 bytes any admitted document serializes to.
   */
  readonly artifact_canonicalization: OpenCode;
  /**
   * SHA-256 of the RFC 8785 canonical UTF-8 bytes of this complete result with exactly two
   * members removed: the result's own `pack_id` and this field. Nothing else is excluded --
   * not the generation time, the authority context, the freshness statement, the policy or
   * configuration versions, the budget, the sections, the citations, or any selected content
   * -- so any change to any of them changes the digest. What is hashed is the *admitted v1
   * data model's* RFC 8785 canonical UTF-8 bytes, not the bytes as they happened to arrive:
   * the document is first admitted under the numeric profile `pack_format_version` pins and
   * the rest of the v1 data model (object member names are strings and no member name is
   * duplicated, strings are valid Unicode scalar sequences with no lone surrogate), and only
   * then canonicalized and hashed. So received whitespace, member order, number spelling, and
   * optional string escaping do not reach the digest -- RFC 8785 defines them away -- while a
   * document that is not admissible at all has no digest rather than a digest of some repaired
   * version of itself. Member names are ordered by unsigned UTF-16 code unit and numbers
   * rendered by ECMAScript `Number::toString`, both as RFC 8785 requires.
   */
  readonly artifact_checksum: ContextPackDigest;
}

/**
 * Operation-independent response metadata. Present on both success and error responses so a
 * caller can always negotiate versions and read its own authority, even when the operation
 * failed.
 */
export interface ResponseMetadata {
  /**
   * Echoes the request attempt this response answers.
   */
  readonly request_id: RequestId;
  /**
   * Echoes the logical operation this response belongs to.
   */
  readonly correlation_id: CorrelationId;
  /**
   * Negotiated versions and capabilities.
   */
  readonly version: VersionCapabilityEnvelope;
  /**
   * Server-validated authority actually applied. The only authority statement a caller may
   * trust.
   */
  readonly authority: GrantedAuthority;
  /**
   * Pagination position, for paginated reads.
   */
  readonly page?: PageMetadata;
  /**
   * Reference to asynchronous work this operation started.
   */
  readonly job?: JobReference;
  /**
   * Staleness statement, for reads served from a projection.
   */
  readonly freshness?: ProjectionFreshness;
  /**
   * The instant the server treated as 'now' when resolving this operation, so results are
   * reproducible.
   */
  readonly canonical_resolution_time?: Timestamp;
  /**
   * Non-fatal advisories.
   */
  readonly warnings?: readonly Warning[];
  /**
   * Things the caller asked for that were deliberately not returned.
   */
  readonly omissions?: readonly Omission[];
  /**
   * Marks the result as incomplete.
   */
  readonly partial?: PartialResult;
  /**
   * Reference to the audit record for this operation.
   */
  readonly audit_reference?: AuditReference;
}

/**
 * One complete, append-preserving L0 evidence artifact: stable identity, workspace, exact
 * source/native locator, applicable temporal instants, content checksum and media type, opaque
 * metadata, permission/sensitivity labels, tombstone status, parser/ingestion status, and
 * append-only provenance history. Carries no `GovernanceLayer`, `GovernanceState`,
 * `RecordCurrentness`, or `authority_level` field: an evidence artifact is raw L0 material,
 * never governed knowledge, and this shape must never be mistaken for a `GovernedRecord`.
 */
export interface EvidenceArtifact {
  /**
   * Stable identifier of this evidence artifact.
   */
  readonly evidence_id: EvidenceId;
  /**
   * Workspace this evidence artifact belongs to.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Exact source, native identifier, and locator this evidence was captured from.
   */
  readonly source: SourceReference;
  /**
   * The distinct instants applicable to this evidence: when observed, when ingested, and when
   * this artifact was recorded, without collapsing them into one.
   * `valid_from`/`valid_until`/`superseded_at` are rarely applicable to append-only L0
   * evidence and are typically absent.
   */
  readonly temporal: RecordTemporalMetadata;
  /**
   * Checksum of this evidence artifact's content, proving the content has not been altered
   * since capture.
   */
  readonly content_checksum: EvidenceChecksum;
  /**
   * Media type of this evidence artifact's content.
   */
  readonly media_type: MediaType;
  /**
   * Opaque metadata captured alongside this evidence artifact.
   */
  readonly metadata: JsonObject;
  /**
   * Open codes naming the access/permission labels attached to this evidence artifact, such as
   * `restricted` or `team_only`. May be empty when no label applies.
   */
  readonly permission_labels: readonly OpenCode[];
  /**
   * Open code naming this evidence artifact's sensitivity classification, such as `public` or
   * `confidential`.
   */
  readonly sensitivity: OpenCode;
  /**
   * True when this evidence artifact has been tombstoned. A tombstoned artifact's append-only
   * provenance history is never erased; tombstoning is itself recorded as a provenance entry.
   */
  readonly tombstoned: boolean;
  /**
   * Open code naming the status of parsing this evidence artifact's content, such as `parsed`
   * or `parse_failed`.
   */
  readonly parser_status: OpenCode;
  /**
   * Open code naming the status of ingesting this evidence artifact, such as `ingested` or
   * `quarantined`.
   */
  readonly ingestion_status: OpenCode;
  /**
   * Append-only history of actions taken on this evidence artifact. Never truncated or
   * rewritten; a correction is a new entry, not an edit to a prior one. Never empty: even the
   * first capture of this artifact is itself a provenance entry, so an artifact with no
   * recorded history entry would be an unaudited state.
   */
  readonly provenance_history: readonly ProvenanceEntry[];
  /**
   * Names the import job/run that produced this evidence artifact, when it was produced by
   * one. Typed as `OpaqueToken` because it is drawn from exactly the same opaque token domain
   * as `JobIdentity.job_id` and `ImportCompletionResult.import_run_id`: the completion
   * contract promises this backlink names the run that created this evidence, and spelled in
   * any narrower vocabulary it could not hold every job id the contract admits -- a run such
   * as `job/opaque-token` could complete and then have no writable backlink at all.
   */
  readonly import_run_id?: OpaqueToken;
}

/**
 * The final outcome of a job once it has reached a terminal state, with the complete attempt
 * history that led there. Exactly one of a success, a failure, or a cancellation, never a mix:
 * each branch closes its property set and carries a unique required discriminator (`result`,
 * `error`, or `cancellation`), so a payload combining or omitting all three matches no branch.
 */
export type JobTerminalResult = JobTerminalSuccess | JobTerminalFailure | JobTerminalCancellation;

/**
 * Result of `import.start`. Carries exactly one thing: the handle for the durable job that was
 * started. `import.start` always returns a job and never a synchronous import outcome, so there
 * is nothing else honest to return here. The response envelope's `ResponseMetadata.job` names
 * the same job as this handle: one operation started one job, and the two statements of that
 * fact must agree.
 */
export interface ImportStartResult {
  /**
   * Handle for the durable import job this call started.
   */
  readonly job: JobHandle;
}

/**
 * Result of `job.cancel`: what the call did, and the handle as it now stands. A state-based
 * refusal is a successful, idempotent control result rather than an API error -- a job that
 * cannot be cancelled returns `not_cancellable` alongside its current unchanged handle, and is
 * never reported as `conflict` merely for being terminal. Authorization failures, a missing job,
 * and workspace failures stay typed API errors.
 */
export interface JobCancelResult {
  /**
   * The job's handle after this call. Unchanged, including its identity, when the call was
   * refused.
   */
  readonly job: JobHandle;
  /**
   * What this call actually did: accepted the cancellation, found it already cancelled, or
   * refused.
   */
  readonly cancellation_disposition: JobCancellationDisposition;
}

/**
 * Result of `job.retry`: which recovery the server chose, and the handle as it now stands. An
 * accepted recovery keeps the same job identity and returns the job to `queued`; it starts no
 * new attempt until execution actually begins, so the previous terminal attempt remains
 * `latest_attempt` while the recovered handle is queued. A state-based refusal is a successful,
 * idempotent control result rather than an API error -- `not_retryable` is returned alongside
 * the current unchanged handle, and is never reported as `conflict` merely for being terminal.
 * Authorization failures, a missing job, and workspace failures stay typed API errors.
 */
export interface JobRetryResult {
  /**
   * The job's handle after this call. Unchanged, including its identity, when the call was
   * refused.
   */
  readonly job: JobHandle;
  /**
   * What this call actually did: scheduled a retry, scheduled a resume, or refused.
   */
  readonly recovery_disposition: JobRecoveryDisposition;
}

/**
 * Input for `memory.create`: the proposed record's type, domain scope, content,
 * evidence/provenance, and assertion. Carries no authority-level, reviewer/policy decision,
 * governance-state, currentness, record id, version, recorded time, or supersession field, so a
 * caller can never assert accepted, current-canonical, superseded, or historical authority
 * through this payload; every `memory.create` result is proposed-only.
 */
export interface MemoryCreateInput {
  /**
   * What kind of governed record is being proposed.
   */
  readonly record_type: GovernedRecordType;
  /**
   * Non-empty domain/record classification the caller proposes for this record; every
   * candidate proposes one. The server remains the final authority on the domain scope
   * actually stored; this field never carries authority level, reviewer/policy decision, or
   * any other server-owned governance field.
   */
  readonly domain_scope: RecordDomainScope;
  /**
   * Opaque proposed content.
   */
  readonly content: JsonObject;
  /**
   * Whether concrete evidence is actually available for this proposal.
   */
  readonly evidence_disposition: EvidenceDisposition;
  /**
   * Sources this proposal draws on. May be empty only when `evidence_disposition` explicitly
   * excuses it.
   */
  readonly sources: readonly SourceReference[];
  /**
   * Who is asserting this candidate, when, on what evidence, and the validity window they
   * propose. Every candidate carries one. Shared with `RecordProvenance.assertion`, so the
   * lineage a proposal supplies is the lineage the resulting record preserves.
   */
  readonly assertion: CandidateAssertion;
  /**
   * Provenance of the automated extractor that produced this candidate, when one did. Absent
   * for a candidate a human asserted directly. Shared with `RecordProvenance.extraction`.
   */
  readonly extraction?: CandidateExtractionMetadata;
  /**
   * When the underlying fact occurred in the world (source/event time), when the caller can
   * supply it.
   */
  readonly event_at?: Timestamp;
  /**
   * When the underlying fact was observed, when the caller can supply it.
   */
  readonly observed_at?: Timestamp;
}

/**
 * The full provenance envelope for one record version: identity, temporal metadata, its
 * authoring history, the sources it draws on, and the caller-supplied assertion/extraction
 * lineage the claim in this version came from. `assertion`/`extraction` are structurally
 * optional so a record written before they existed still decodes, but a governance transition
 * that replaces or carries forward a claim must bind them; enforcing that is a semantic-
 * validation concern, not a wire-shape one.
 */
export interface RecordProvenance {
  /**
   * Identity, version, governance layer, and currentness of this record.
   */
  readonly identity: RecordIdentity;
  /**
   * Observed, ingested, recorded, and valid time for this record.
   */
  readonly temporal: RecordTemporalMetadata;
  /**
   * Ordered, append-only history of actions that produced this record version. Deliberately
   * carries no `maxItems`: history is never erased or rewritten, and every governance
   * transition appends exactly one event, so any finite inline cap would eventually make a
   * previously valid record impossible to transition -- and raising the cap only postpones
   * that contradiction. Bounding a response's size is a transport/operation concern, handled
   * outside this inline provenance invariant, never by dropping, compacting, or summarising
   * audit history.
   */
  readonly history: readonly ProvenanceEntry[];
  /**
   * Whether concrete evidence is actually available for this record. `sources` may be empty
   * only when this disposition explicitly states evidence is unavailable; enforcing that
   * agreement is a semantic-validation concern, not a wire-shape one.
   */
  readonly evidence_disposition: EvidenceDisposition;
  /**
   * Sources this record draws on, independent of any single history entry's evidence. May be
   * empty only when `evidence_disposition` explicitly states evidence is unavailable.
   */
  readonly sources: readonly SourceReference[];
  /**
   * Who asserted the claim this version carries, when, on what evidence, and the validity
   * window they proposed. Preserved verbatim from the `memory.create` or `record.supersede`
   * input that supplied the claim, so candidate/replacement lineage survives every governance
   * transition.
   */
  readonly assertion?: CandidateAssertion;
  /**
   * Provenance of the automated extractor that produced the claim this version carries, when
   * one did. Absent for a claim a human asserted directly.
   */
  readonly extraction?: CandidateExtractionMetadata;
}

/**
 * Result of `workspace.list`.
 */
export interface WorkspaceListResult {
  /**
   * Workspaces visible to the caller's installation-level authority, in this page.
   */
  readonly workspaces: readonly WorkspaceDescriptor[];
  /**
   * Continuation position for the next page, absent on the last page.
   */
  readonly page: PageMetadata;
}

/**
 * Result of `workspace.create`: the concrete created workspace, including its server-assigned
 * identifier and format compatibility. Never a sentinel or placeholder workspace identifier.
 */
export interface WorkspaceCreateResult {
  /**
   * The concrete workspace that was created.
   */
  readonly workspace: WorkspaceDescriptor;
}

/**
 * Result of `workspace.inspect`: the envelope-selected workspace's concrete descriptor.
 */
export interface WorkspaceInspectResult {
  /**
   * The inspected workspace.
   */
  readonly workspace: WorkspaceDescriptor;
}

/**
 * A successful response. Carries `result` and never `error`.
 */
export interface SuccessResponseEnvelope {
  /**
   * Operation-independent response metadata.
   */
  readonly metadata: ResponseMetadata;
  /**
   * Opaque operation result.
   */
  readonly result: JsonObject;
}

/**
 * A failed response. Carries `error` and never `result`.
 */
export interface ErrorResponseEnvelope {
  /**
   * Operation-independent response metadata.
   */
  readonly metadata: ResponseMetadata;
  /**
   * The typed failure.
   */
  readonly error: ApiError;
}

/**
 * Result of `evidence.search`: complete, append-preserving L0 evidence artifacts with exact
 * provenance. Never substitutes a `GovernedRecord` for an evidence artifact.
 */
export interface EvidenceSearchResult {
  /**
   * Evidence artifacts in this page.
   */
  readonly evidence: readonly EvidenceArtifact[];
  /**
   * Continuation position for the next page, absent on the last page.
   */
  readonly page: PageMetadata;
}

/**
 * Result of `job.get`: the current handle, plus the terminal result when the job has one.
 * `terminal_result` is present exactly when `job.state` is a known terminal state, and it is
 * closed against the handle it accompanies: identity and state match exactly, every attempt
 * instant in the terminal history falls inside the handle's own `created_at`/`updated_at`
 * lifetime, and the handle's `latest_attempt` is exactly the final attempt of that history (and
 * is absent exactly when the history is). One read describes one job, never two disagreeing
 * statements about it. An unknown state is preserved but implies nothing: a handle in a state
 * this build has never seen carries no `terminal_result`, because this build cannot know whether
 * that state is terminal.
 */
export interface JobGetResult {
  /**
   * Current handle for this job.
   */
  readonly job: JobHandle;
  /**
   * The job's final outcome and complete attempt history, present exactly when the handle is
   * in a known terminal state.
   */
  readonly terminal_result?: JobTerminalResult;
}

/**
 * Input for `record.supersede`: an explicit, authorized replacement of a current accepted
 * (`l2`/`accepted`/`current`) governed record with a new accepted version. The target version is
 * the envelope's `MutationPrecondition.record_version`, not duplicated here. Exactly three
 * fields: which record, the complete replacement claim, and why. The replacement is a whole
 * `MemoryCreateInput` rather than a loose bag of content/evidence fields, so the new version's
 * content, evidence disposition, sources, assertion, extraction lineage, and proposed validity
 * window are supplied and validated as one coherent claim under exactly the rules
 * `memory.create` already enforces. It inherits that shape's least-authority-escalating
 * guarantee: no governance-state, currentness, authority-level, reviewer, or supersession field
 * is accepted from a caller. The server alone produces the new version's identity, authority,
 * temporal envelope, and the reciprocal `supersedes`/`superseded_by` pointers, and the
 * replacement's `record_type`/`domain_scope` must equal the superseded record's -- superseding
 * replaces a claim, it never silently reclassifies the record.
 */
export interface RecordSupersedeInput {
  /**
   * Identifier of the current accepted record to supersede.
   */
  readonly record_id: RecordId;
  /**
   * The complete replacement claim: content, evidence disposition, sources, assertion,
   * optional extraction lineage, and optional event/observed times, in exactly the shape
   * `memory.create` accepts. Its `record_type`/`domain_scope` must equal the superseded
   * record's.
   */
  readonly replacement: MemoryCreateInput;
  /**
   * Reason this record is being superseded.
   */
  readonly rationale: GovernanceRationale;
}

/**
 * A provider-neutral governed record: which workspace it belongs to, what kind of record it is,
 * its domain scope and authority level, its full L0-L4 governance, temporal, evidence, and
 * provenance envelope, and its opaque JSON content. Carries no reference to, and is not a
 * substitute for, any repo-local `Memory`, `MemoryFact`, or `SourceRef` domain class.
 */
export interface GovernedRecord {
  /**
   * Workspace this record belongs to.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * What kind of governed record this is.
   */
  readonly record_type: GovernedRecordType;
  /**
   * Non-empty domain/record classification this record is filed under. Every governed record
   * carries exactly one; a caller may propose one through `memory.create`, but the server is
   * always the final authority on what is actually stored here. Distinct from caller-
   * authorization `Scope`.
   */
  readonly domain_scope: RecordDomainScope;
  /**
   * Open code naming the authority level this record's governance decision currently carries,
   * such as `proposed` or `reviewed` or `canonical`. Server-owned: no `memory.create` input
   * field lets a caller assert this directly.
   */
  readonly authority_level: OpenCode;
  /**
   * Identifier of the reviewer or policy that produced this record's current governance
   * decision, when one has been recorded. Absent when no reviewer/policy decision applies yet,
   * such as a freshly proposed record.
   */
  readonly reviewer?: Identifier;
  /**
   * Identity, governance layer/state/currentness, temporal metadata, history, and evidence for
   * this record version.
   */
  readonly provenance: RecordProvenance;
  /**
   * Opaque governed content this record carries.
   */
  readonly content: JsonObject;
}

/**
 * Result of `context_pack.build`: the original query, the model-facing sections, the selected L0
 * evidence, current canonical L2 records, supporting history and L3 context models, the exact
 * citations every section and selected item rests on, the conflicts and uncertainties the pack
 * surfaces rather than resolving, the policy and budget omissions, token accounting, and the
 * complete reproducibility record. Selecting and citing this content never grants new authority:
 * `fresh_authorization_required` is always true, and possessing `pack_id` grants nothing on its
 * own -- it is a content digest anyone can recompute, not a capability.
 */
export interface ContextPackBuildResult {
  /**
   * Content-addressed identity of this pack, exactly equal to
   * `reproducibility.artifact_checksum`. Two builds that produce the same content produce the
   * same identity, and a changed pack can never keep an old one.
   */
  readonly pack_id: ContextPackDigest;
  /**
   * The mode this pack was built in, bound exactly to the request's mode.
   */
  readonly mode: ContextPackMode;
  /**
   * The original caller query this pack was built for, bound exactly to the request's query.
   */
  readonly query: MemoryQuery;
  /**
   * The model-facing sections of this pack, ordered deterministically by `section_id`. May be
   * empty for a valid pack that found nothing to say; every section that is present is non-
   * empty and cited.
   */
  readonly sections: readonly ContextPackSection[];
  /**
   * Selected L0 evidence artifacts, ordered deterministically by identifier then content
   * checksum. Every artifact is held to the complete resolution-time closure:
   * `canonical_resolution_time` is an inclusive upper bound on every act and observation the
   * artifact carries, not only on the instants this partition's own rule reads --
   * `temporal.event_at`, `observed_at`, `ingested_at` and `recorded_at`,
   * `source.retrieved_at`, every `provenance_history[].occurred_at`, and every
   * `provenance_history[].evidence[].source.retrieved_at`. Equality passes and only a strictly
   * later instant is refused, and the refusal is a refusal rather than a repair: provenance is
   * append-only, so an out-of-range instant is never dropped or truncated to make the artifact
   * selectable. Its validity must contain the resolution instant inclusively --
   * `temporal.valid_from` no later than it and `temporal.valid_until` no earlier than it, both
   * boundaries accepted at equality. `temporal.superseded_at` must be absent or strictly after
   * the resolution instant: supersession is exclusive where the closure is inclusive, because
   * an artifact replaced *at* the instant a pack resolved was already not the live one, so
   * equality is rejected here rather than accepted.
   */
  readonly evidence: readonly EvidenceArtifact[];
  /**
   * Selected current, canonical L2 governed records, ordered deterministically by record
   * identifier then version. Every record is held to the complete resolution-time closure:
   * `canonical_resolution_time` is an inclusive upper bound on every act and observation
   * nested anywhere in its provenance, not only on the instants this partition's own rule
   * reads -- `provenance.temporal.event_at`, `observed_at`, `ingested_at` and `recorded_at`,
   * every `provenance.sources[].retrieved_at`, every `provenance.history[].occurred_at`, every
   * `provenance.history[].evidence[].source.retrieved_at`, `provenance.assertion.asserted_at`,
   * every `provenance.assertion.evidence[].source.retrieved_at`, and
   * `provenance.extraction.extracted_at`. Equality passes and only a strictly later instant is
   * refused, and the refusal is a refusal rather than a repair. Its validity must contain the
   * resolution instant inclusively -- `provenance.temporal.valid_from` no later than it and
   * `provenance.temporal.valid_until` no earlier than it, both boundaries accepted at
   * equality; a version whose validity begins only afterwards was not yet in force, and one
   * that expired before it was no longer the answer. The version must be current and
   * unsuperseded at that instant: `currentness` exactly `current`, and
   * `provenance.temporal.superseded_at` absent outright, irrespective of timestamp. Not merely
   * absent at or before the resolution instant: a current version records no supersession at
   * all, so a `superseded_at` strictly after the resolution instant is refused exactly as one
   * at or before it is. A version that states when it was replaced belongs to `history`,
   * whichever side of the resolution instant that statement falls on.
   * `provenance.assertion.proposed_valid_from`/`proposed_valid_until` are deliberately not
   * bounded by any of this -- a proposed effective date is a claim about the future rather
   * than an act that had to have happened, and a record valid now may propose taking effect
   * later.
   */
  readonly records: readonly GovernedRecord[];
  /**
   * Selected historical canonical L2 governed record versions -- versions that were canonical
   * knowledge and had already been superseded at the canonical-resolution time -- ordered
   * deterministically by record identifier then version. May be empty. Every version is held
   * to the same complete resolution-time closure `records` states, over exactly the same
   * nested provenance paths, inclusive at equality and refused rather than repaired past it.
   * `provenance.temporal.superseded_at` is required and must be at or before the resolution
   * instant, with equality *accepted*: a version replaced exactly at the instant a pack
   * resolved was already history by it, and one superseded only afterwards was still canonical
   * then and is not this partition's to carry. That is the mirror image of the `evidence`
   * rule, where equality is rejected, and the two differ because they are asking opposite
   * questions about the same boundary. Validity containment is deliberately *not* required
   * here: a historical version's validity window may have closed long before the resolution
   * instant -- that is what makes it historical -- so demanding containment would empty the
   * partition of exactly the versions it exists to carry. Note also that `recorded_at <=
   * superseded_at <= resolution` already follows from the intrinsic record rules composed with
   * the supersession bound above, so a future `ingested_at`/`recorded_at` on a historical
   * version is refused by those before the nested closure is ever consulted.
   */
  readonly history: readonly GovernedRecord[];
  /**
   * Selected current, canonical L3 context-model governed records, ordered deterministically
   * by record identifier then version. May be empty. Held to exactly the same current rules
   * `records` states, at L3 rather than L2: the same complete resolution-time closure over the
   * same nested provenance paths, inclusive at equality and refused rather than repaired past
   * it; the same inclusive validity containment of the resolution instant at both boundaries;
   * and the same requirement to be current and unsuperseded at that instant, with
   * `provenance.temporal.superseded_at` absent outright, irrespective of timestamp -- a value
   * strictly after the resolution instant is refused exactly as one at or before it is. Only
   * the governance layer differs -- `layer` exactly `l3` -- and a context model is otherwise
   * no more selectable than an L2 record would be under the same temporal facts.
   */
  readonly context_models: readonly GovernedRecord[];
  /**
   * Exact citations binding this pack's sections and selected content to immutable evidence or
   * governed record versions, ordered deterministically by `citation_id`. May be empty only
   * when this pack selected nothing and states no section.
   */
  readonly citations: readonly ContextPackCitation[];
  /**
   * Conflicts among cited content this pack surfaces rather than silently resolving, in
   * deterministic order. May be empty.
   */
  readonly conflicts: readonly ContextPackConflict[];
  /**
   * Uncertainties this pack surfaces rather than silently resolving, in deterministic order.
   * May be empty.
   */
  readonly uncertainties: readonly ContextPackUncertainty[];
  /**
   * Policy or budget reasons content the caller might otherwise expect was left out of this
   * pack, in deterministic order. May be empty.
   */
  readonly omissions: readonly Omission[];
  /**
   * Token budget accounting for this pack.
   */
  readonly budget: ContextPackBudget;
  /**
   * Everything a second build needs to reproduce this pack byte for byte, including the
   * checksum that makes it content-addressed.
   */
  readonly reproducibility: ContextPackReproducibility;
  /**
   * Always true: following any citation in this pack always requires fresh authorization
   * against the cited evidence or record. Possessing this pack, or `pack_id`, grants no access
   * on its own.
   */
  readonly fresh_authorization_required: boolean;
}

/**
 * Exactly one of a success or an error response, never both. Both branches close their property
 * set, so a document carrying `result` and `error` together matches neither branch and is
 * invalid.
 */
export type ResponseEnvelope = SuccessResponseEnvelope | ErrorResponseEnvelope;

/**
 * One node in a traversal result: a precise reference to the canonical governed record version
 * it represents, the full governed record it wraps, and the depth at which this traversal
 * reached it. Never carries a competing identity, provenance, lifecycle, authority, or
 * governance state of its own -- `reference` and `record` are the only sources of truth, and
 * they must agree.
 */
export interface GraphNode {
  /**
   * Precise reference to the canonical governed record version this node represents.
   */
  readonly reference: RecordVersionReference;
  /**
   * The full governed record this node wraps.
   */
  readonly record: GovernedRecord;
  /**
   * The traversal depth at which this node was reached, measured from the requested seeds: 0
   * exactly for a node the request named in `start`, and never for any other node.
   */
  readonly depth: GraphDepthLimit;
}

/**
 * One edge in a traversal result: the relation type, its source and target governed-record
 * versions, the relation's own full governed record, and the precise reference identifying that
 * relation record. Never carries a competing identity, provenance, lifecycle, authority, or
 * governance state of its own beyond that wrapped record: `relation_reference` must identify
 * `record.provenance.identity` exactly, so the relation record is referenced, never re-
 * identified. `source` and `target` are structurally optional so a result can represent a
 * justified page/depth boundary where one end of a relation was not reached; at least one must
 * be present, and exactly one may be absent only together with a coherent `boundary_reason`.
 * Both endpoints present means a fully materialized edge and forbids `boundary_reason`; both
 * absent is never representable, since an edge that names no returned node states nothing this
 * result can be trusted about.
 */
export interface GraphEdge {
  /**
   * Kind of relation this edge represents.
   */
  readonly relation_type: GraphRelationType;
  /**
   * The record version this edge originates from. Absent only at a justified page/depth
   * boundary, together with a coherent `boundary_reason`; absence means this traversal did not
   * reach that end, never that the relation has no source.
   */
  readonly source?: RecordVersionReference;
  /**
   * The record version this edge points to. Absent only at a justified page/depth boundary,
   * together with a coherent `boundary_reason`; absence means this traversal did not reach
   * that end, never that the relation has no target.
   */
  readonly target?: RecordVersionReference;
  /**
   * The relation's own full governed record, retaining its own full evidence and provenance.
   */
  readonly record: GovernedRecord;
  /**
   * Precise reference to the canonical governed record version of the relation itself -- the
   * record `record` wraps. Must identify `record.provenance.identity` exactly; it is a pointer
   * to that record's identity, never a second identity the edge owns.
   */
  readonly relation_reference: RecordVersionReference;
  /**
   * Why exactly one endpoint is absent. Required when exactly one of `source`/`target` is
   * absent, forbidden when both are present, and never sufficient on its own: the stated
   * reason must actually hold against this result's page metadata and applied limits.
   */
  readonly boundary_reason?: GraphBoundaryReason;
}

/**
 * Result of `knowledge.search`. When the request's `view` was absent or `current_canonical`,
 * every returned record must be the exact accepted, current, canonical version; no candidate,
 * rejected, superseded, or non-canonical-layer record may appear.
 */
export interface KnowledgeSearchResult {
  /**
   * Governed records in this page, ordered per the request's `order`.
   */
  readonly records: readonly GovernedRecord[];
  /**
   * Continuation position for the next page, absent on the last page.
   */
  readonly page: PageMetadata;
}

/**
 * Result of `knowledge.propose`: both versions of the record as they stand *after* the
 * transition, so a caller can validate the transition and confirm no history or provenance was
 * lost. `previous_record` is the prior version, which the transition has itself marked
 * superseded and pointed at the new one; `updated_record` is the newly current version. Neither
 * is a pre-transition snapshot: `previous_record` is what that version now is, not what it
 * looked like before the call.
 */
export interface KnowledgeProposeResult {
  /**
   * The prior version this transition replaced, as it stands after the transition: still
   * carrying its own `proposed` claim, now marked superseded and pointing at `updated_record`.
   */
  readonly previous_record: GovernedRecord;
  /**
   * The newly current version: the record's new candidate state.
   */
  readonly updated_record: GovernedRecord;
}

/**
 * Result of `candidate.approve`: both versions of the record as they stand *after* approval, so
 * a caller can validate the transition and confirm no history or provenance was lost.
 * `previous_record` is the prior candidate version, which the approval has itself marked
 * superseded and pointed at the new one; `updated_record` is the newly current version. Neither
 * is a pre-transition snapshot: `previous_record` is what that version now is, not what it
 * looked like before the call.
 */
export interface CandidateApproveResult {
  /**
   * The prior candidate version this approval replaced, as it stands after the transition:
   * still carrying its candidate authority, now marked superseded and pointing at
   * `updated_record`.
   */
  readonly previous_record: GovernedRecord;
  /**
   * The newly current version: the record's new accepted, current, canonical state.
   */
  readonly updated_record: GovernedRecord;
}

/**
 * Result of `candidate.reject`: both versions of the record as they stand *after* rejection, so
 * a caller can validate the transition and confirm no history or provenance was lost.
 * `previous_record` is the prior candidate version, which the rejection has itself marked
 * superseded and pointed at the new one; `updated_record` is the newly current version. Neither
 * is a pre-transition snapshot: `previous_record` is what that version now is, not what it
 * looked like before the call.
 */
export interface CandidateRejectResult {
  /**
   * The prior candidate version this rejection replaced, as it stands after the transition:
   * still carrying its candidate authority, now marked superseded and pointing at
   * `updated_record`.
   */
  readonly previous_record: GovernedRecord;
  /**
   * The newly current version: the record's new rejected state. `rejected` carries a reviewer,
   * but is never a favourable or accepted authority decision.
   */
  readonly updated_record: GovernedRecord;
}

/**
 * Result of `record.supersede`: the prior current record (now superseded) and the new current
 * record, so a caller can validate the reciprocal `supersedes`/`superseded_by` pointers, the
 * preserved stable record identity, and the complete, unerased provenance history.
 */
export interface RecordSupersedeResult {
  /**
   * The prior current record, now superseded.
   */
  readonly previous_record: GovernedRecord;
  /**
   * The new current, accepted, canonical record.
   */
  readonly updated_record: GovernedRecord;
}

/**
 * Result of `memory.create`: the resulting proposed governed record.
 * `provenance.identity.governance_state` is always `proposed`; this operation never creates
 * accepted canonical knowledge.
 */
export interface MemoryCreateResult {
  /**
   * The newly proposed governed record.
   */
  readonly record: GovernedRecord;
}

/**
 * Result of `memory.get`: the governed record.
 */
export interface MemoryGetResult {
  /**
   * The fetched governed record.
   */
  readonly record: GovernedRecord;
}

/**
 * Result of `memory.list`.
 */
export interface MemoryListResult {
  /**
   * Governed records in this page.
   */
  readonly records: readonly GovernedRecord[];
  /**
   * Continuation position for the next page, absent on the last page.
   */
  readonly page: PageMetadata;
}

/**
 * Result of `memory.search`.
 */
export interface MemorySearchResult {
  /**
   * Governed records in this page, ordered per the request's `order`.
   */
  readonly records: readonly GovernedRecord[];
  /**
   * Continuation position for the next page, absent on the last page.
   */
  readonly page: PageMetadata;
}

/**
 * Result of `graph.traverse`: the traversed nodes and edges, the traversal limits actually
 * applied (which may be tighter than requested but never looser), the projection
 * metadata/watermark this traversal was served from, and deterministic ordering evidence.
 * Boundaries are stated, never implied: an edge whose source or target this traversal did not
 * reach carries the endpoint absent plus a `boundary_reason` that must actually hold here --
 * `page_boundary` only when `page` offers a continuation token and `nodes` reached
 * `applied_node_limit` exactly, `depth_boundary` only when the endpoint that *is* present is a
 * returned node sitting at `applied_depth_limit`. Projection loss is never canonical-data loss:
 * an absent endpoint says this page stopped, not that the relation lost an end.
 */
export interface GraphTraversalResult {
  /**
   * Nodes reached by this traversal, in the order named by `ordering_basis`: ascending by
   * `(depth, reference.record_id, reference.version)`.
   */
  readonly nodes: readonly GraphNode[];
  /**
   * Edges reached by this traversal, in the order named by `ordering_basis`: ascending by the
   * complete tuple `(source.record_id, source.version, relation_type, target.record_id,
   * target.version, relation_reference.record_id, relation_reference.version)`. All seven
   * fields participate, so two edges sharing the same endpoints but naming different
   * relations, or the same relation type recorded by different relation record versions, still
   * have one reproducible order. An absent endpoint contributes the empty string in both of
   * its positions, which sorts before any present reference.
   */
  readonly edges: readonly GraphEdge[];
  /**
   * The traversal depth actually applied.
   */
  readonly applied_depth_limit: GraphDepthLimit;
  /**
   * The node limit actually applied. May be tighter than a requested `node_limit`, but on a
   * first page never below the number of requested `start` seeds -- including when the request
   * named no `node_limit` and this limit is the server's own choice, since that page still
   * owes every seed.
   */
  readonly applied_node_limit: PageLimit;
  /**
   * The edge limit actually applied.
   */
  readonly applied_edge_limit: PageLimit;
  /**
   * The projection version(s)/watermark this traversal was actually served from.
   */
  readonly freshness: ProjectionFreshness;
  /**
   * The deterministic key `nodes` and `edges` are ordered by.
   */
  readonly ordering_basis: GraphOrderingBasis;
  /**
   * Position this traversal reached. Always present: a continuation token when the traversal's
   * ordering can be deterministically continued and more remains, and `{}` when it is
   * exhausted or cannot be paged. Exhaustion is stated rather than implied by an absent field,
   * so a caller reads the same shape here as on every other paginated result in this contract.
   */
  readonly page: PageMetadata;
}

/**
 * The frozen v1 error-code vocabulary. ErrorCode stays an open string on the wire, so a value
 * outside this list is valid and must be preserved.
 */
export const FROZEN_ERROR_CODES = [
  "authentication_required",
  "authorization_denied",
  "workspace_not_granted",
  "capability_not_granted",
  "invalid_purpose",
  "invalid_request",
  "not_found",
  "conflict",
  "mutation_precondition_failed",
  "idempotency_conflict",
  "workspace_busy",
  "bootstrap_in_progress",
  "workspace_lease_unavailable",
  "workspace_migration_required",
  "incompatible_version",
  "upgrade_required",
  "projection_unavailable",
  "stale_projection",
  "rate_limited",
  "size_limit_exceeded",
  "token_limit_exceeded",
  "deadline_exceeded",
  "cancelled",
  "dependency_unavailable",
  "internal_recoverable",
  "internal_non_recoverable",
] as const;

export type FrozenErrorCode = (typeof FROZEN_ERROR_CODES)[number];

/**
 * The frozen v1 retry-class vocabulary. RetryClass stays an open string on the wire; an
 * unrecognized class must fail safe as non-retryable.
 */
export const FROZEN_RETRY_CLASSES = [
  "non_retryable",
  "retryable",
  "retryable_after_delay",
  "retryable_after_precondition_refresh",
] as const;

export type FrozenRetryClass = (typeof FROZEN_RETRY_CLASSES)[number];

/**
 * The only retry classes a caller may blind-retry.
 */
export const RETRYABLE_RETRY_CLASSES = [
  "retryable",
  "retryable_after_delay",
] as const;

/**
 * Frozen retry classification for every v1 error code.
 */
export const DEFAULT_RETRY_CLASSIFICATION: Readonly<Record<FrozenErrorCode, FrozenRetryClass>> = {
  authentication_required: "non_retryable",
  authorization_denied: "non_retryable",
  workspace_not_granted: "non_retryable",
  capability_not_granted: "non_retryable",
  invalid_purpose: "non_retryable",
  invalid_request: "non_retryable",
  not_found: "non_retryable",
  conflict: "non_retryable",
  mutation_precondition_failed: "retryable_after_precondition_refresh",
  idempotency_conflict: "non_retryable",
  workspace_busy: "retryable_after_delay",
  bootstrap_in_progress: "retryable_after_delay",
  workspace_lease_unavailable: "retryable_after_delay",
  workspace_migration_required: "non_retryable",
  incompatible_version: "non_retryable",
  upgrade_required: "non_retryable",
  projection_unavailable: "retryable_after_delay",
  stale_projection: "retryable_after_delay",
  rate_limited: "retryable_after_delay",
  size_limit_exceeded: "non_retryable",
  token_limit_exceeded: "non_retryable",
  deadline_exceeded: "retryable",
  cancelled: "non_retryable",
  dependency_unavailable: "retryable_after_delay",
  internal_recoverable: "retryable",
  internal_non_recoverable: "non_retryable",
};

/**
 * Known compatibility statuses. The wire field is an open code; preserve unknown values.
 */
export const COMPATIBILITY_STATUSES = [
  "compatible",
  "compatible_with_deprecations",
  "upgrade_required",
  "incompatible",
] as const;

/**
 * Known upgrade states. The wire field is an open code; preserve unknown values.
 */
export const UPGRADE_STATES = [
  "none",
  "optional",
  "required",
  "in_progress",
] as const;

/**
 * The canonical v1 application operation catalogue, in the canonical order. Generated from
 * `x-omnivia-operation-catalogue`, so this is contract metadata a caller can read, not a
 * dispatch table: nothing here routes, authorizes, or executes anything.
 */
export const OPERATION_CATALOGUE: readonly OperationMetadata[] = [
  {
    name: "candidate.approve",
    scope: { required_scopes: ["memory:write"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/CandidateApproveInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/CandidateApproveResult",
    required_capability: { id: "knowledge.govern", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: true, required: true },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "conflict",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "mutation_precondition_failed",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "candidate.reject",
    scope: { required_scopes: ["memory:write"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/CandidateRejectInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/CandidateRejectResult",
    required_capability: { id: "knowledge.govern", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: true, required: true },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "conflict",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "mutation_precondition_failed",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "context_pack.build",
    scope: { required_scopes: ["memory:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/context-pack.schema.json#/$defs/ContextPackBuildInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/context-pack.schema.json#/$defs/ContextPackBuildResult",
    required_capability: { id: "context_pack.build", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "projection_unavailable",
      "rate_limited",
      "size_limit_exceeded",
      "stale_projection",
      "token_limit_exceeded",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "evidence.search",
    scope: { required_scopes: ["memory:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceSearchInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceSearchResult",
    required_capability: { id: "evidence.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "projection_unavailable",
      "rate_limited",
      "stale_projection",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "graph.traverse",
    scope: { required_scopes: ["graph:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/graph.schema.json#/$defs/GraphTraversalInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/graph.schema.json#/$defs/GraphTraversalResult",
    required_capability: { id: "graph.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "projection_unavailable",
      "rate_limited",
      "size_limit_exceeded",
      "stale_projection",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "import.start",
    scope: { required_scopes: ["memory:write"], side_effect: "create", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/ImportStartInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/ImportStartResult",
    required_capability: { id: "ingestion.import", minimum_version: "1.0", required: true },
    job: {
      completion_mode: "always_returns_job",
      job_kind: "ingestion.import",
      terminal_result_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/ImportCompletionResult",
    },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "rate_limited",
      "size_limit_exceeded",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "job.cancel",
    scope: { required_scopes: ["job:control"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobCancelInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobCancelResult",
    required_capability: { id: "job.control", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "job.events",
    scope: { required_scopes: ["job:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobEventsInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobEventsResult",
    required_capability: { id: "job.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "rate_limited",
      "size_limit_exceeded",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "job.get",
    scope: { required_scopes: ["job:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobGetInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobGetResult",
    required_capability: { id: "job.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "job.retry",
    scope: { required_scopes: ["job:control"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobRetryInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/JobRetryResult",
    required_capability: { id: "job.control", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "knowledge.propose",
    scope: { required_scopes: ["memory:write"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/KnowledgeProposeInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/KnowledgeProposeResult",
    required_capability: { id: "knowledge.govern", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: true, required: true },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "conflict",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "mutation_precondition_failed",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "knowledge.search",
    scope: { required_scopes: ["memory:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/KnowledgeSearchInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/KnowledgeSearchResult",
    required_capability: { id: "knowledge.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "projection_unavailable",
      "rate_limited",
      "stale_projection",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "memory.create",
    scope: { required_scopes: ["memory:write"], side_effect: "create", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemoryCreateInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemoryCreateResult",
    required_capability: { id: "memory.write", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "memory.get",
    scope: { required_scopes: ["memory:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemoryGetInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemoryGetResult",
    required_capability: { id: "memory.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "memory.list",
    scope: { required_scopes: ["memory:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemoryListInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemoryListResult",
    required_capability: { id: "memory.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "rate_limited",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "memory.search",
    scope: { required_scopes: ["memory:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemorySearchInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemorySearchResult",
    required_capability: { id: "memory.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "projection_unavailable",
      "rate_limited",
      "stale_projection",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "record.supersede",
    scope: { required_scopes: ["memory:write"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/RecordSupersedeInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/RecordSupersedeResult",
    required_capability: { id: "knowledge.govern", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: true, required: true },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "conflict",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "mutation_precondition_failed",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_busy",
      "workspace_lease_unavailable",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "workspace.create",
    scope: {
      required_scopes: ["workspace:write"],
      side_effect: "create",
      scope_kind: "installation",
    },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceCreateInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceCreateResult",
    required_capability: { id: "workspace.write", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: true, required: true, safe_to_retry: false },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "mutation" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "bootstrap_in_progress",
      "cancelled",
      "capability_not_granted",
      "conflict",
      "deadline_exceeded",
      "dependency_unavailable",
      "idempotency_conflict",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "rate_limited",
      "upgrade_required",
    ],
  },
  {
    name: "workspace.inspect",
    scope: { required_scopes: ["workspace:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceInspectInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceInspectResult",
    required_capability: { id: "workspace.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: false },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "not_found",
      "rate_limited",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "workspace.list",
    scope: { required_scopes: ["workspace:read"], side_effect: "none", scope_kind: "installation" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceListInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceListResult",
    required_capability: { id: "workspace.read", minimum_version: "1.0", required: true },
    job: { completion_mode: "synchronous" },
    pagination: { paginated: true, max_page_size: 1000 },
    idempotency: { supports_idempotency_key: false, required: false, safe_to_retry: true },
    precondition: { supports_mutation_precondition: false, required: false },
    audit: { audited: true, audit_category: "read" },
    allowed_errors: [
      "authentication_required",
      "authorization_denied",
      "bootstrap_in_progress",
      "cancelled",
      "capability_not_granted",
      "deadline_exceeded",
      "dependency_unavailable",
      "incompatible_version",
      "internal_non_recoverable",
      "internal_recoverable",
      "invalid_purpose",
      "invalid_request",
      "rate_limited",
      "upgrade_required",
    ],
  },
] as const;
