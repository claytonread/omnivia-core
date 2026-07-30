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
export const CONTRACT_VERSION = "1.1" as const;

/**
 * Base URI every canonical v1 schema `$id` is rooted at.
 */
export const SCHEMA_BASE_URI = "https://contracts.omnivia.dev/application/v1/" as const;

/**
 * A `major.minor` contract version. Major changes are breaking; minor changes are additive and
 * forward compatible.
 */
export type ContractVersion = string;
export const CONTRACT_VERSION_PATTERN: string = "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$";

/**
 * A SemVer 2.0.0 release string identifying a concrete build, not a contract.
 */
export type ReleaseVersion = string;
export const RELEASE_VERSION_PATTERN: string =
  "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0" +
  "-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)(?:\\.(?:0|[1-9][0-9]*|[0-9]*[a-zA" +
  "-Z-][0-9a-zA-Z-]*))*))?(?:\\+([0-9a-zA-Z-]+(?:\\.[0-9a-zA-Z-]+)*))?$";

/**
 * Bounded, non-empty caller-assigned identifier for a single request attempt.
 */
export type RequestId = string;
export const REQUEST_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Bounded, non-empty identifier grouping related requests into one logical operation.
 */
export type CorrelationId = string;
export const CORRELATION_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Bounded, non-empty distributed-trace identifier. Diagnostic only; never an authorization
 * input.
 */
export type TraceId = string;
export const TRACE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Bounded, non-empty identifier of the workspace a request is scoped to.
 */
export type WorkspaceId = string;
export const WORKSPACE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Bounded, non-empty server-issued reference to the audit record for a completed operation.
 */
export type AuditReference = string;
export const AUDIT_REFERENCE_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.
 */
export type Identifier = string;
export const IDENTIFIER_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Stable namespaced capability identifier such as `memory.read`. At least one dot is required so
 * capability names always carry a namespace.
 */
export type CapabilityId = string;
export const CAPABILITY_ID_PATTERN: string =
  "^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*(?:" +
  "\\.[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*)+$";

/**
 * An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible
 * minor releases can add vocabulary; consumers must preserve values they do not recognize.
 */
export type OpenCode = string;
export const OPEN_CODE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * An open scope token such as `memory:read` requested by the caller. Scopes narrow a request;
 * they never widen granted authority.
 */
export type Scope = string;
export const SCOPE_PATTERN: string = "^[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*$";

/**
 * An open purpose-limitation token stating why the caller is making this request.
 */
export type Purpose = string;
export const PURPOSE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never
 * parse it.
 */
export type OpaqueToken = string;
export const OPAQUE_TOKEN_PATTERN: string = "^[!-~]+$";

/**
 * Caller-assigned key making a mutation safe to retry. Equal keys with different inputs are an
 * `idempotency_conflict`.
 */
export type IdempotencyKey = string;
export const IDEMPOTENCY_KEY_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * An RFC 3339 timestamp in UTC with a literal `Z` offset.
 */
export type Timestamp = string;
export const TIMESTAMP_PATTERN: string =
  "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:" +
  "[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$";

/**
 * A bounded non-negative duration in milliseconds.
 */
export type DurationMs = number;

/**
 * An opaque per-projection version marker used to reason about read staleness.
 */
export type ProjectionVersion = string;
export const PROJECTION_VERSION_PATTERN: string = "^[!-~]+$";

/**
 * An opaque JSON object. The application contract carries domain payloads without inspecting
 * them; per-operation payload schemas are out of scope for v1 foundations.
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
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming how thoroughly a release or capability combination has
 * actually been verified, such as `development` or `unverified` or `qualified` or `supported`.
 * Open by design so a compatible minor release can add states without breaking existing
 * decoders. A combination absent this state, or carrying anything other than an explicitly
 * verified state, must never be treated as supported: an empty or `unverified` entry is not
 * evidence of support.
 */
export type QualificationState = string;
export const QUALIFICATION_STATE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming which component a compatibility entry describes, such as
 * `core` or `runtime` or `cli` or `mcp` or `sdk`. Open by design so a compatible minor release
 * can add components without breaking existing decoders.
 */
export type ComponentKind = string;
export const COMPONENT_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Dot-namespaced operation identifier such as `memory.get`. The per-operation payload catalogue
 * is out of scope for v1 foundations; only the name is contractual here.
 */
export type OperationName = string;
export const OPERATION_NAME_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)+$";

/**
 * Stable machine-readable failure code. OPEN by design: this is a patterned string, not an enum,
 * so compatible minor releases can add codes. Decoders must preserve unknown codes and must not
 * map them onto a known code.
 */
export type ErrorCode = string;
export const ERROR_CODE_PATTERN: string = "^[a-z][a-z0-9_]*$";

/**
 * How a caller may retry. OPEN by design, for the same reason as `ErrorCode`. An unrecognized
 * retry class MUST fail safe as non-retryable: never infer that an unknown class is retryable.
 */
export type RetryClass = string;
export const RETRY_CLASS_PATTERN: string = "^[a-z][a-z0-9_]*$";

/**
 * Open, dot-namespaced code naming where a job stands in its lifecycle, such as `queued` or
 * `running` or `succeeded` or `failed` or `cancelled`. Open by design so a compatible minor
 * release can add states without breaking existing decoders.
 */
export type JobState = string;
export const JOB_STATE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming what `JobProgress.completed_units`/`total_units` count, such
 * as `item` or `byte` or `document`. Open by design so a compatible minor release can add units
 * without breaking existing decoders.
 */
export type JobProgressUnit = string;
export const JOB_PROGRESS_UNIT_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming whether a job may be cancelled and where a requested
 * cancellation stands, such as `not_cancellable` or `cancellable` or `cancellation_requested` or
 * `cancelled`. Open by design; carries no scheduler, worker, lease, or persistence detail.
 */
export type JobCancellationDisposition = string;
export const JOB_CANCELLATION_DISPOSITION_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming whether a job may be retried and where a requested retry
 * stands, such as `not_retryable` or `retryable` or `retry_scheduled`. Open by design; carries
 * no scheduler, worker, lease, or persistence detail.
 */
export type JobRetryDisposition = string;
export const JOB_RETRY_DISPOSITION_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming whether a suspended or cancelled job may be resumed, such as
 * `not_resumable` or `resumable` or `resume_requested`. Open by design; carries no scheduler,
 * worker, lease, or persistence detail.
 */
export type JobResumeDisposition = string;
export const JOB_RESUME_DISPOSITION_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact`
 * or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add
 * record types without breaking existing decoders.
 */
export type GovernedRecordType = string;
export const GOVERNED_RECORD_TYPE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code selecting which slice of a governed record's versions a read
 * considers: `current_canonical` (the single active accepted version, the default when this
 * field is absent), `candidates` (proposed/candidate versions not yet accepted), or `history`
 * (every version, including superseded ones). Open by design so a compatible minor release can
 * add views without breaking existing decoders. Default resolution when absent is a semantic
 * concern (see `omnivia_core.contracts.v1.semantics`), not a wire-shape one.
 */
export type GovernedRecordView = string;
export const GOVERNED_RECORD_VIEW_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed
 * record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the
 * caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or
 * checks a permission, it only classifies what the record is about. Open by design so a
 * compatible minor release can add classifications without breaking existing decoders.
 */
export type RecordDomainScope = string;
export const RECORD_DOMAIN_SCOPE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

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
export const MEMORY_SEARCH_ORDER_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming whether invoking an operation mutates state, such as `none`
 * or `create` or `update` or `delete`. Open by design so a compatible minor release can add
 * classifications without breaking existing decoders.
 */
export type OperationSideEffect = string;
export const OPERATION_SIDE_EFFECT_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming the kind of scope an operation carries, such as
 * `installation` or `workspace`. Open by design so a compatible minor release can add scope
 * kinds without breaking existing decoders. A given operation's scope metadata carries exactly
 * one kind.
 */
export type OperationScopeKind = string;
export const OPERATION_SCOPE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming how an operation completes, such as `synchronous` (no durable
 * job, ever), `may_return_job` (a response may carry a `JobReference`), or `always_returns_job`
 * (every invocation starts a durable job). Independent of `OperationSideEffect`: an operation
 * like `import.start` is representable as a mutation (`side_effect`) that always returns a
 * durable job (`completion_mode`). Open by design so a compatible minor release can add modes
 * without breaking existing decoders.
 */
export type OperationCompletionMode = string;
export const OPERATION_COMPLETION_MODE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

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
 * How this operation may safely be retried.
 */
export interface OperationIdempotencyMetadata {
  /**
   * Whether this operation honours `RequestMetadata.idempotency_key`.
   */
  readonly supports_idempotency_key: boolean;
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
export const RECORD_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$";

/**
 * Opaque, server-issued version marker of one specific revision of a record. Clients must round-
 * trip it verbatim and must never parse it.
 */
export type RecordVersion = string;
export const RECORD_VERSION_PATTERN: string = "^[!-~]+$";

/**
 * Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw
 * evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3`
 * (context models), or `l4` (organisational model). Distinct from workspace scope, which is a
 * caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a
 * compatible minor release can add layers without breaking existing decoders.
 */
export type GovernanceLayer = string;
export const GOVERNANCE_LAYER_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming whether a record version is the active one, such as `current`
 * or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not
 * coerced to a known one.
 */
export type RecordCurrentness = string;
export const RECORD_CURRENTNESS_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming a record's position in its own governance workflow, such as
 * `proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which
 * namespace a record belongs to) and `RecordCurrentness` (whether this version is the active
 * one): a record can be `accepted` and still later superseded, or `proposed` and never adopted.
 * Open by design so a compatible minor release can add states without breaking existing
 * decoders.
 */
export type GovernanceState = string;
export const GOVERNANCE_STATE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming the kind of thing a source reference points at, such as
 * `document` or `conversation` or `api_response`.
 */
export type SourceKind = string;
export const SOURCE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

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
export const EVIDENCE_DISPOSITION_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming which runtime probe is being requested or answered. The
 * frozen, currently known probe kinds are exactly `service.health`, `service.readiness`, and
 * `service.discover`. Open by design so a compatible minor release can add probe kinds without
 * breaking existing callers.
 */
export type ProbeKind = string;
export const PROBE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming the outcome of a probe or one of its components, such as
 * `pass` or `warn` or `fail`. Open by design; an unrecognized status must be preserved and
 * surfaced, not coerced to a known one.
 */
export type ProbeStatus = string;
export const PROBE_STATUS_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

/**
 * Open, dot-namespaced code naming a workspace's lifecycle status, such as `active` or
 * `provisioning` or `archived`. Open by design so a compatible minor release can add statuses
 * without breaking existing decoders.
 */
export type WorkspaceStatus = string;
export const WORKSPACE_STATUS_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$";

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
 * Staleness statement for reads served from a projection rather than the write model.
 */
export interface ProjectionFreshness {
  /**
   * Point in time the projection reflects.
   */
  readonly as_of: Timestamp;
  /**
   * Open map of projection name to opaque projection version. New projections may appear in
   * compatible minor releases.
   */
  readonly projection_versions: Readonly<Record<string, ProjectionVersion>>;
  /**
   * True when the server knows the projection lags the write model.
   */
  readonly stale: boolean;
}

/**
 * Pagination position. Token issuance semantics are deliberately out of scope for v1
 * foundations.
 */
export interface PageMetadata {
  /**
   * Opaque cursor for the next page. Absent means the last page.
   */
  readonly continuation_token?: OpaqueToken;
}

/**
 * Reference to asynchronous work started by an operation. Job lifecycle, polling, and
 * cancellation are later phases; v1 carries the identifier only.
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
 * The control actions a caller may take on a job: cancellation, retry, and resume. Deliberately
 * exposes only these caller-facing dispositions, never scheduler, worker, lease, or persistence
 * detail.
 */
export interface JobControl {
  /**
   * Whether this job may be cancelled and where a requested cancellation stands.
   */
  readonly cancellation: JobCancellationDisposition;
  /**
   * Whether this job may be retried and where a requested retry stands.
   */
  readonly retry: JobRetryDisposition;
  /**
   * Whether this job may be resumed and where a requested resume stands.
   */
  readonly resume: JobResumeDisposition;
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
 * Optional provenance about the automated extractor that produced a `memory.create` candidate,
 * when one did. Absent entirely for a candidate a human asserted directly.
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
 * One execution attempt of a job. A job that is retried has more than one attempt.
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
 * posture. This is the complete shape a future per-operation catalogue entry will carry, so
 * publishing that catalogue is additive rather than requiring later required-field breaks.
 * Binding this to a concrete request/result payload and publishing a catalogue of operations is
 * out of scope for this document.
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
   * Optional structured detail.
   */
  readonly details?: JsonObject;
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
 * progress and attempt, and which control actions are available.
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
 * `cancellation`.
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
   * Opaque success payload.
   */
  readonly result: JsonObject;
}

/**
 * The final outcome of a job that failed. Carries `error` and never `result` or `cancellation`.
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
 * `error`.
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
 * Who is asserting a `memory.create` candidate, when, and on what evidence, plus the validity
 * window they propose for it. This is caller-supplied provenance for the proposal, not the
 * server-owned governance decision: it never carries authority level, reviewer/policy identity,
 * or any other field `MemoryCreateInput` itself is forbidden from carrying.
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
 * One step in a record's history: who or what did what, and when.
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
   * Evidence supporting this action, when applicable.
   */
  readonly evidence?: readonly EvidenceReference[];
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
 * The final outcome of a job once it has reached a terminal state, with the complete attempt
 * history that led there. Exactly one of a success, a failure, or a cancellation, never a mix:
 * each branch closes its property set and carries a unique required discriminator (`result`,
 * `error`, or `cancellation`), so a payload combining or omitting all three matches no branch.
 */
export type JobTerminalResult = JobTerminalSuccess | JobTerminalFailure | JobTerminalCancellation;

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
   * propose. Every candidate carries one.
   */
  readonly assertion: CandidateAssertion;
  /**
   * Provenance of the automated extractor that produced this candidate, when one did. Absent
   * for a candidate a human asserted directly.
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
 * authoring history, and the sources it draws on.
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
   * Ordered history of actions that produced this record version.
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
 * Exactly one of a success or an error response, never both. Both branches close their property
 * set, so a document carrying `result` and `error` together matches neither branch and is
 * invalid.
 */
export type ResponseEnvelope = SuccessResponseEnvelope | ErrorResponseEnvelope;

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
 * The frozen v1 error-code vocabulary. ErrorCode stays an open string on the wire, so a value
 * outside this list is valid and must be preserved.
 */
export const FROZEN_ERROR_CODES = [
  "authentication_required",
  "authorization_denied",
  "workspace_not_granted",
  "capability_not_granted",
  "invalid_purpose",
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
