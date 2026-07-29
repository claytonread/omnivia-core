// GENERATED FILE - DO NOT EDIT.
//
// Source of truth:
//   contracts/application/v1/schemas/common.schema.json
//   contracts/application/v1/schemas/compatibility.schema.json
//   contracts/application/v1/schemas/errors.schema.json
//   contracts/application/v1/schemas/envelopes.schema.json
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
export const CONTRACT_VERSION = "1.0" as const;

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
   * Workspace the request is scoped to.
   */
  readonly workspace_id: WorkspaceId;
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
 * Exactly one of a success or an error response, never both. Both branches close their property
 * set, so a document carrying `result` and `error` together matches neither branch and is
 * invalid.
 */
export type ResponseEnvelope = SuccessResponseEnvelope | ErrorResponseEnvelope;

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
