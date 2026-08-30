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
//   contracts/application/v1/schemas/runtime.schema.json
//   contracts/application/v1/schemas/chat.schema.json
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
export const CONTRACT_VERSION = "1.3" as const;

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
 * Return whether a value is a well-formed `ContractVersion`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isContractVersion(value: unknown): value is ContractVersion {
  return (
    typeof value === "string" &&
    value.length <= 32 &&
    new RegExp(CONTRACT_VERSION_PATTERN).test(value)
  );
}

/**
 * A SemVer 2.0.0 release string identifying a concrete build, not a contract.
 */
export type ReleaseVersion = string;
export const RELEASE_VERSION_PATTERN: string =
  "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]" +
  "*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)(?:\\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0" +
  "-9a-zA-Z-]*))*))?(?:\\+([0-9a-zA-Z-]+(?:\\.[0-9a-zA-Z-]+)*))?$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `ReleaseVersion`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isReleaseVersion(value: unknown): value is ReleaseVersion {
  return (
    typeof value === "string" &&
    value.length <= 128 &&
    new RegExp(RELEASE_VERSION_PATTERN).test(value)
  );
}

/**
 * Bounded, non-empty caller-assigned identifier for a single request attempt.
 */
export type RequestId = string;
export const REQUEST_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `RequestId`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isRequestId(value: unknown): value is RequestId {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(REQUEST_ID_PATTERN).test(value)
  );
}

/**
 * Bounded, non-empty identifier grouping related requests into one logical operation.
 */
export type CorrelationId = string;
export const CORRELATION_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `CorrelationId`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isCorrelationId(value: unknown): value is CorrelationId {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(CORRELATION_ID_PATTERN).test(value)
  );
}

/**
 * Bounded, non-empty distributed-trace identifier. Diagnostic only; never an authorization
 * input.
 */
export type TraceId = string;
export const TRACE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `TraceId`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isTraceId(value: unknown): value is TraceId {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(TRACE_ID_PATTERN).test(value)
  );
}

/**
 * Bounded, non-empty identifier of the workspace a request is scoped to.
 */
export type WorkspaceId = string;
export const WORKSPACE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `WorkspaceId`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isWorkspaceId(value: unknown): value is WorkspaceId {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(WORKSPACE_ID_PATTERN).test(value)
  );
}

/**
 * Bounded, non-empty server-issued reference to the audit record for a completed operation.
 */
export type AuditReference = string;
export const AUDIT_REFERENCE_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `AuditReference`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isAuditReference(value: unknown): value is AuditReference {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(AUDIT_REFERENCE_PATTERN).test(value)
  );
}

/**
 * Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.
 */
export type Identifier = string;
export const IDENTIFIER_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `Identifier`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isIdentifier(value: unknown): value is Identifier {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(IDENTIFIER_PATTERN).test(value)
  );
}

/**
 * Stable namespaced capability identifier such as `memory.read`. At least one dot is required so
 * capability names always carry a namespace.
 */
export type CapabilityId = string;
export const CAPABILITY_ID_PATTERN: string =
  "^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*(?:\\.[a-z" +
  "][a-z0-9]*(?:[_-][a-z0-9]+)*)+$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `CapabilityId`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isCapabilityId(value: unknown): value is CapabilityId {
  return (
    typeof value === "string" &&
    value.length >= 3 &&
    value.length <= 128 &&
    new RegExp(CAPABILITY_ID_PATTERN).test(value)
  );
}

/**
 * An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible
 * minor releases can add vocabulary; consumers must preserve values they do not recognize.
 */
export type OpenCode = string;
export const OPEN_CODE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `OpenCode`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isOpenCode(value: unknown): value is OpenCode {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(OPEN_CODE_PATTERN).test(value)
  );
}

/**
 * An open scope token such as `memory:read` requested by the caller. Scopes narrow a request;
 * they never widen granted authority.
 */
export type Scope = string;
export const SCOPE_PATTERN: string = "^[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `Scope`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isScope(value: unknown): value is Scope {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(SCOPE_PATTERN).test(value)
  );
}

/**
 * An open purpose-limitation token stating why the caller is making this request.
 */
export type Purpose = string;
export const PURPOSE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `Purpose`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isPurpose(value: unknown): value is Purpose {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(PURPOSE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `OpaqueToken`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isOpaqueToken(value: unknown): value is OpaqueToken {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 512 &&
    new RegExp(OPAQUE_TOKEN_PATTERN).test(value)
  );
}

/**
 * Caller-assigned key making a mutation safe to retry. Equal keys with different inputs are an
 * `idempotency_conflict`.
 */
export type IdempotencyKey = string;
export const IDEMPOTENCY_KEY_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `IdempotencyKey`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isIdempotencyKey(value: unknown): value is IdempotencyKey {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(IDEMPOTENCY_KEY_PATTERN).test(value)
  );
}

/**
 * An RFC 3339 timestamp in UTC with a literal `Z` offset.
 */
export type Timestamp = string;
export const TIMESTAMP_PATTERN: string =
  "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]" +
  "{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `Timestamp`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with. `TIMESTAMP_PATTERN` fixes the spelling and
 * cannot fix the calendar: `2026-13-01T00:00:00Z` satisfies it character for character.
 * `Date.parse` is not the missing half -- it accepts `2024-02-30T00:00:00Z` and
 * `2026-02-29T00:00:00Z` by rolling them forward into March, so a guard that trusted it would
 * admit values this contract's other bindings refuse. The date is built from the literal fields
 * instead, and every field is compared back: any value the constructor had to normalize
 * disagrees with the literal it came from and is refused.
 */
export function isTimestamp(value: unknown): value is Timestamp {
  if (
    !(
      typeof value === "string" &&
      value.length <= 40 &&
      new RegExp(TIMESTAMP_PATTERN).test(value)
    )
  ) {
    return false;
  }
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const hour = Number(value.slice(11, 13));
  const minute = Number(value.slice(14, 16));
  const second = Number(value.slice(17, 19));
  // Year 0000 is representable here and is not a `date-time` the canonical schema accepts.
  if (year < 1) {
    return false;
  }
  // `setUTCFullYear` rather than `Date.UTC`, which would remap years 0-99 onto 1900-1999.
  const at = new Date(0);
  at.setUTCFullYear(year, month - 1, day);
  at.setUTCHours(hour, minute, second, 0);
  return (
    at.getUTCFullYear() === year &&
    at.getUTCMonth() === month - 1 &&
    at.getUTCDate() === day &&
    at.getUTCHours() === hour &&
    at.getUTCMinutes() === minute &&
    at.getUTCSeconds() === second
  );
}

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
 * Return whether a value is a well-formed `ProjectionVersion`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isProjectionVersion(value: unknown): value is ProjectionVersion {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(PROJECTION_VERSION_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `OperationCompatibilityState`: the declared pattern
 * and length bounds, applied as a full match. The generated decoders do not call this --
 * decoding stays tolerant, and this is the primitive a caller validates with.
 */
export function isOperationCompatibilityState(value: unknown): value is OperationCompatibilityState {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(OPERATION_COMPATIBILITY_STATE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `QualificationState`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isQualificationState(value: unknown): value is QualificationState {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(QUALIFICATION_STATE_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming which component a compatibility entry describes, such as
 * `core` or `runtime` or `cli` or `mcp` or `sdk`. Open by design so a compatible minor release
 * can add components without breaking existing decoders.
 */
export type ComponentKind = string;
export const COMPONENT_KIND_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `ComponentKind`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isComponentKind(value: unknown): value is ComponentKind {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(COMPONENT_KIND_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `ContextPackMode`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isContextPackMode(value: unknown): value is ContextPackMode {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(CONTEXT_PACK_MODE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `ContextPackDigest`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isContextPackDigest(value: unknown): value is ContextPackDigest {
  return (
    typeof value === "string" &&
    value.length >= 71 &&
    value.length <= 71 &&
    new RegExp(CONTEXT_PACK_DIGEST_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `OperationName`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isOperationName(value: unknown): value is OperationName {
  return (
    typeof value === "string" &&
    value.length >= 3 &&
    value.length <= 128 &&
    new RegExp(OPERATION_NAME_PATTERN).test(value)
  );
}

/**
 * Stable machine-readable failure code. OPEN by design: this is a patterned string, not an enum,
 * so compatible minor releases can add codes. Decoders must preserve unknown codes and must not
 * map them onto a known code.
 */
export type ErrorCode = string;
export const ERROR_CODE_PATTERN: string = "^[a-z][a-z0-9_]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `ErrorCode`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isErrorCode(value: unknown): value is ErrorCode {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(ERROR_CODE_PATTERN).test(value)
  );
}

/**
 * How a caller may retry. OPEN by design, for the same reason as `ErrorCode`. An unrecognized
 * retry class MUST fail safe as non-retryable: never infer that an unknown class is retryable.
 */
export type RetryClass = string;
export const RETRY_CLASS_PATTERN: string = "^[a-z][a-z0-9_]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `RetryClass`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isRetryClass(value: unknown): value is RetryClass {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(RETRY_CLASS_PATTERN).test(value)
  );
}

/**
 * Stable identifier of one L0 evidence artifact, constant across its append-only provenance
 * history. Distinct from `RecordId`: an evidence artifact is never itself a governed record.
 */
export type EvidenceId = string;
export const EVIDENCE_ID_PATTERN: string = "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `EvidenceId`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isEvidenceId(value: unknown): value is EvidenceId {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(EVIDENCE_ID_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `EvidenceChecksum`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isEvidenceChecksum(value: unknown): value is EvidenceChecksum {
  return (
    typeof value === "string" &&
    value.length >= 3 &&
    value.length <= 256 &&
    new RegExp(EVIDENCE_CHECKSUM_PATTERN).test(value)
  );
}

/**
 * An IANA-style `type/subtype` media type string, such as `text/plain` or `application/json`.
 */
export type MediaType = string;
export const MEDIA_TYPE_PATTERN: string =
  "^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za" +
  "-z0-9][A-Za-z0-9!#$&^_.+-]*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `MediaType`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isMediaType(value: unknown): value is MediaType {
  return (
    typeof value === "string" &&
    value.length >= 3 &&
    value.length <= 255 &&
    new RegExp(MEDIA_TYPE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `GraphDirection`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGraphDirection(value: unknown): value is GraphDirection {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GRAPH_DIRECTION_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming a kind of relation between governed records, such as
 * `relates_to` or `derived_from`. Open by design so a compatible minor release can add relation
 * types without breaking existing decoders.
 */
export type GraphRelationType = string;
export const GRAPH_RELATION_TYPE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `GraphRelationType`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGraphRelationType(value: unknown): value is GraphRelationType {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GRAPH_RELATION_TYPE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `GraphOrderingBasis`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGraphOrderingBasis(value: unknown): value is GraphOrderingBasis {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GRAPH_ORDERING_BASIS_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `GraphBoundaryReason`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGraphBoundaryReason(value: unknown): value is GraphBoundaryReason {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GRAPH_BOUNDARY_REASON_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming where a job stands in its lifecycle, such as `queued` or
 * `running` or `succeeded` or `failed` or `cancelled`. Open by design so a compatible minor
 * release can add states without breaking existing decoders.
 */
export type JobState = string;
export const JOB_STATE_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `JobState`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isJobState(value: unknown): value is JobState {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(JOB_STATE_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming what `JobProgress.completed_units`/`total_units` count, such
 * as `item` or `byte` or `document`. Open by design so a compatible minor release can add units
 * without breaking existing decoders.
 */
export type JobProgressUnit = string;
export const JOB_PROGRESS_UNIT_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `JobProgressUnit`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isJobProgressUnit(value: unknown): value is JobProgressUnit {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(JOB_PROGRESS_UNIT_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `JobCancellationAvailability`: the declared pattern
 * and length bounds, applied as a full match. The generated decoders do not call this --
 * decoding stays tolerant, and this is the primitive a caller validates with.
 */
export function isJobCancellationAvailability(value: unknown): value is JobCancellationAvailability {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(JOB_CANCELLATION_AVAILABILITY_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `JobRecoveryAvailability`: the declared pattern and
 * length bounds, applied as a full match. The generated decoders do not call this -- decoding
 * stays tolerant, and this is the primitive a caller validates with.
 */
export function isJobRecoveryAvailability(value: unknown): value is JobRecoveryAvailability {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(JOB_RECOVERY_AVAILABILITY_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `JobCancellationDisposition`: the declared pattern and
 * length bounds, applied as a full match. The generated decoders do not call this -- decoding
 * stays tolerant, and this is the primitive a caller validates with.
 */
export function isJobCancellationDisposition(value: unknown): value is JobCancellationDisposition {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(JOB_CANCELLATION_DISPOSITION_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `JobRecoveryDisposition`: the declared pattern and
 * length bounds, applied as a full match. The generated decoders do not call this -- decoding
 * stays tolerant, and this is the primitive a caller validates with.
 */
export function isJobRecoveryDisposition(value: unknown): value is JobRecoveryDisposition {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(JOB_RECOVERY_DISPOSITION_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `ContentChecksum`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isContentChecksum(value: unknown): value is ContentChecksum {
  return (
    typeof value === "string" &&
    value.length >= 71 &&
    value.length <= 71 &&
    new RegExp(CONTENT_CHECKSUM_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact`
 * or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add
 * record types without breaking existing decoders.
 */
export type GovernedRecordType = string;
export const GOVERNED_RECORD_TYPE_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `GovernedRecordType`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGovernedRecordType(value: unknown): value is GovernedRecordType {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GOVERNED_RECORD_TYPE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `GovernedRecordView`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGovernedRecordView(value: unknown): value is GovernedRecordView {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GOVERNED_RECORD_VIEW_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `RecordDomainScope`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isRecordDomainScope(value: unknown): value is RecordDomainScope {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(RECORD_DOMAIN_SCOPE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `MemorySearchOrder`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isMemorySearchOrder(value: unknown): value is MemorySearchOrder {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(MEMORY_SEARCH_ORDER_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming whether invoking an operation mutates state, such as `none`
 * or `create` or `update` or `delete`. Open by design so a compatible minor release can add
 * classifications without breaking existing decoders.
 */
export type OperationSideEffect = string;
export const OPERATION_SIDE_EFFECT_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `OperationSideEffect`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isOperationSideEffect(value: unknown): value is OperationSideEffect {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(OPERATION_SIDE_EFFECT_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `OperationScopeKind`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isOperationScopeKind(value: unknown): value is OperationScopeKind {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(OPERATION_SCOPE_KIND_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `OperationCompletionMode`: the declared pattern and
 * length bounds, applied as a full match. The generated decoders do not call this -- decoding
 * stays tolerant, and this is the primitive a caller validates with.
 */
export function isOperationCompletionMode(value: unknown): value is OperationCompletionMode {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(OPERATION_COMPLETION_MODE_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `RecordId`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isRecordId(value: unknown): value is RecordId {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(RECORD_ID_PATTERN).test(value)
  );
}

/**
 * Opaque, server-issued version marker of one specific revision of a record. Clients must round-
 * trip it verbatim and must never parse it.
 */
export type RecordVersion = string;
export const RECORD_VERSION_PATTERN: string = "^[!-~]+$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `RecordVersion`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isRecordVersion(value: unknown): value is RecordVersion {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 512 &&
    new RegExp(RECORD_VERSION_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `GovernanceLayer`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGovernanceLayer(value: unknown): value is GovernanceLayer {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GOVERNANCE_LAYER_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming whether a record version is the active one, such as `current`
 * or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not
 * coerced to a known one.
 */
export type RecordCurrentness = string;
export const RECORD_CURRENTNESS_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `RecordCurrentness`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isRecordCurrentness(value: unknown): value is RecordCurrentness {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(RECORD_CURRENTNESS_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `GovernanceState`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isGovernanceState(value: unknown): value is GovernanceState {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(GOVERNANCE_STATE_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming the kind of thing a source reference points at, such as
 * `document` or `conversation` or `api_response`.
 */
export type SourceKind = string;
export const SOURCE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `SourceKind`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isSourceKind(value: unknown): value is SourceKind {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(SOURCE_KIND_PATTERN).test(value)
  );
}

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
 * Return whether a value is a well-formed `EvidenceDisposition`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isEvidenceDisposition(value: unknown): value is EvidenceDisposition {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(EVIDENCE_DISPOSITION_PATTERN).test(value)
  );
}

/**
 * Which representation an `ExternalReference` names, so a correlated identifier is never read as
 * an identifier of a different domain. `runtime` is the canonical Runtime itself and the only
 * authoritative kind; `application_job` is the durable job substrate a Run is admitted and
 * claimed through; `control_plane_projection` is the read-only control-plane correlation record;
 * `agent_lane_ledger` is the agent-lane PM run ledger, whose `run_id` is a different identifier
 * in a different domain and must never be joined to a canonical `Run.run_id`; `external_log` is
 * any log or trace captured outside this contract. Closed at the schema and tolerant on the
 * wire: an unrecognized kind decodes and is preserved, but it is never authoritative.
 */
export type RuntimeSourceKind = string;

/**
 * The closed `RuntimeSourceKind` vocabulary, emitted from the schema's `enum`.
 */
export const RUNTIME_SOURCE_KIND_VALUES = [
  "runtime",
  "application_job",
  "control_plane_projection",
  "agent_lane_ledger",
  "external_log",
] as const;

/**
 * Return whether a value is a declared `RuntimeSourceKind`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isRuntimeSourceKind(value: unknown): value is RuntimeSourceKind {
  return (
    typeof value === "string" &&
    (RUNTIME_SOURCE_KIND_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Where a canonical `Run` stands. A Core-owned vocabulary, deliberately neither the scheduler's
 * `JobState` (which cannot express waiting) nor the control plane's run status (which is not
 * durable): `admitted` is accepted with a policy and budget snapshot pinned but not yet
 * executing, `running` is executing, `waiting` is durably suspended on a `Wait`, `succeeded`
 * completed with every step succeeded, `partially_completed` reached the end with some step not
 * succeeded, `failed` ended on a failure, `cancelled` was stopped by request, and `uncertain`
 * means the outcome of at least one effect is not known and has not been reconciled. Closed at
 * the schema and open on the wire: an unrecognized status decodes and is preserved verbatim, but
 * no semantic decision may be taken from it -- it is never terminal, never successful, never a
 * licence to start a new effect.
 */
export type RunStatus = string;

/**
 * The closed `RunStatus` vocabulary, emitted from the schema's `enum`.
 */
export const RUN_STATUS_VALUES = [
  "admitted",
  "running",
  "waiting",
  "succeeded",
  "partially_completed",
  "failed",
  "cancelled",
  "uncertain",
] as const;

/**
 * Return whether a value is a declared `RunStatus`. The generated decoders do not call this --
 * decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isRunStatus(value: unknown): value is RunStatus {
  return (
    typeof value === "string" &&
    (RUN_STATUS_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Where one `RunStep` stands. `pending` has not started, `running` is executing an attempt,
 * `waiting` is suspended on a `Wait`, `succeeded`, `failed` and `cancelled` are terminal
 * outcomes, and `skipped` is a step the run deliberately did not execute. Closed at the schema
 * and open on the wire, with the same fail-safe reading as `RunStatus`.
 */
export type RunStepStatus = string;

/**
 * The closed `RunStepStatus` vocabulary, emitted from the schema's `enum`.
 */
export const RUN_STEP_STATUS_VALUES = [
  "pending",
  "running",
  "waiting",
  "succeeded",
  "failed",
  "cancelled",
  "skipped",
] as const;

/**
 * Return whether a value is a declared `RunStepStatus`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isRunStepStatus(value: unknown): value is RunStepStatus {
  return (
    typeof value === "string" &&
    (RUN_STEP_STATUS_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Where one `Attempt` stands. An attempt exists because execution started, so there is no queued
 * attempt state: waiting to run is a state of the step, not of an execution of it. `uncertain`
 * is an attempt whose effect may or may not have landed; it is not a failure, and reporting it
 * as one would licence a retry that duplicates a committed effect. Closed at the schema and open
 * on the wire, with the same fail-safe reading as `RunStatus`.
 */
export type AttemptStatus = string;

/**
 * The closed `AttemptStatus` vocabulary, emitted from the schema's `enum`.
 */
export const ATTEMPT_STATUS_VALUES = [
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "uncertain",
] as const;

/**
 * Return whether a value is a declared `AttemptStatus`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isAttemptStatus(value: unknown): value is AttemptStatus {
  return (
    typeof value === "string" &&
    (ATTEMPT_STATUS_VALUES as readonly string[]).includes(value)
  );
}

/**
 * What a durable `Wait` is waiting for: an `approval` decision by a human or role, an
 * `external_signal` delivered from outside the run, or a `timer` reaching its deadline. The kind
 * fixes which resolution may resolve it, so a signal can never be accepted as an approval.
 * Closed at the schema and open on the wire, with the same fail-safe reading as `RunStatus`.
 */
export type WaitKind = string;

/**
 * The closed `WaitKind` vocabulary, emitted from the schema's `enum`.
 */
export const WAIT_KIND_VALUES = [
  "approval",
  "external_signal",
  "timer",
] as const;

/**
 * Return whether a value is a declared `WaitKind`. The generated decoders do not call this --
 * decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isWaitKind(value: unknown): value is WaitKind {
  return (
    typeof value === "string" &&
    (WAIT_KIND_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Where one `Wait` stands: `pending` is unresolved and still holding the run, `resolved` was
 * resolved by exactly one `ResolveWait`, `expired` passed its deadline without one, and
 * `cancelled` was released because the run was cancelled. Closed at the schema and open on the
 * wire, with the same fail-safe reading as `RunStatus`.
 */
export type WaitStatus = string;

/**
 * The closed `WaitStatus` vocabulary, emitted from the schema's `enum`.
 */
export const WAIT_STATUS_VALUES = [
  "pending",
  "resolved",
  "expired",
  "cancelled",
] as const;

/**
 * Return whether a value is a declared `WaitStatus`. The generated decoders do not call this --
 * decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isWaitStatus(value: unknown): value is WaitStatus {
  return (
    typeof value === "string" &&
    (WAIT_STATUS_VALUES as readonly string[]).includes(value)
  );
}

/**
 * How one `ResolveWait` proposes to resolve a `Wait`, paired to the wait's own kind:
 * `approval_decision` resolves an `approval` wait and names the recorded `Approval`,
 * `external_signal` resolves an `external_signal` wait, `timer_expiry` resolves a `timer` wait,
 * and `cancelled` releases any wait because the run was cancelled. Deliberately not a job
 * control: none of these requeues a job, and none of them is `job.retry`. Closed at the schema
 * and open on the wire, with the same fail-safe reading as `RunStatus`.
 */
export type WaitResolution = string;

/**
 * The closed `WaitResolution` vocabulary, emitted from the schema's `enum`.
 */
export const WAIT_RESOLUTION_VALUES = [
  "approval_decision",
  "external_signal",
  "timer_expiry",
  "cancelled",
] as const;

/**
 * Return whether a value is a declared `WaitResolution`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isWaitResolution(value: unknown): value is WaitResolution {
  return (
    typeof value === "string" &&
    (WAIT_RESOLUTION_VALUES as readonly string[]).includes(value)
  );
}

/**
 * The decision recorded against an approval request. There are exactly two, because an approval
 * either was or was not given; a request that timed out has no decision at all and is reported
 * by its `Wait` reaching `expired`. Closed at the schema and open on the wire, with the same
 * fail-safe reading as `RunStatus`.
 */
export type ApprovalDecision = string;

/**
 * The closed `ApprovalDecision` vocabulary, emitted from the schema's `enum`.
 */
export const APPROVAL_DECISION_VALUES = [
  "approved",
  "rejected",
] as const;

/**
 * Return whether a value is a declared `ApprovalDecision`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isApprovalDecision(value: unknown): value is ApprovalDecision {
  return (
    typeof value === "string" &&
    (APPROVAL_DECISION_VALUES as readonly string[]).includes(value)
  );
}

/**
 * What an `EffectSettlement` says happened to the effect its intent declared: `committed` is
 * proven by a receipt, `not_committed` is proven not to have happened, and `unknown` is the
 * honest third answer -- the runtime could not establish either. `unknown` is uncertainty, not
 * failure: it must not be reported as a failed effect, and it must not licence a blind retry of
 * a logically identical effect. Closed at the schema and open on the wire, with the same fail-
 * safe reading as `RunStatus`.
 */
export type EffectOutcome = string;

/**
 * The closed `EffectOutcome` vocabulary, emitted from the schema's `enum`.
 */
export const EFFECT_OUTCOME_VALUES = [
  "committed",
  "not_committed",
  "unknown",
] as const;

/**
 * Return whether a value is a declared `EffectOutcome`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isEffectOutcome(value: unknown): value is EffectOutcome {
  return (
    typeof value === "string" &&
    (EFFECT_OUTCOME_VALUES as readonly string[]).includes(value)
  );
}

/**
 * What one recorded cleanup achieved: `released` freed the resource, `not_required` found
 * nothing to free, and `failed` could not free it. Cleanup is observable rather than implied, so
 * a failed cleanup is recorded as a receipt rather than left unsaid. Closed at the schema and
 * open on the wire, with the same fail-safe reading as `RunStatus`.
 */
export type CleanupOutcome = string;

/**
 * The closed `CleanupOutcome` vocabulary, emitted from the schema's `enum`.
 */
export const CLEANUP_OUTCOME_VALUES = [
  "released",
  "not_required",
  "failed",
] as const;

/**
 * Return whether a value is a declared `CleanupOutcome`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isCleanupOutcome(value: unknown): value is CleanupOutcome {
  return (
    typeof value === "string" &&
    (CLEANUP_OUTCOME_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Which kind of executable definition a run executes. The two public product terms are
 * `agent_component` and `workflow`; there is no provider, adapter, worker-host or Harness kind
 * here, because none of those is a definition a run executes. Closed at the schema and open on
 * the wire, with the same fail-safe reading as `RunStatus`.
 */
export type RunDefinitionKind = string;

/**
 * The closed `RunDefinitionKind` vocabulary, emitted from the schema's `enum`.
 */
export const RUN_DEFINITION_KIND_VALUES = [
  "agent_component",
  "workflow",
] as const;

/**
 * Return whether a value is a declared `RunDefinitionKind`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isRunDefinitionKind(value: unknown): value is RunDefinitionKind {
  return (
    typeof value === "string" &&
    (RUN_DEFINITION_KIND_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Open, dot-namespaced code naming which runtime probe is being requested or answered. The
 * frozen, currently known probe kinds are exactly `service.health`, `service.readiness`, and
 * `service.discover`. Open by design so a compatible minor release can add probe kinds without
 * breaking existing callers.
 */
export type ProbeKind = string;
export const PROBE_KIND_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `ProbeKind`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isProbeKind(value: unknown): value is ProbeKind {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(PROBE_KIND_PATTERN).test(value)
  );
}

/**
 * Open, dot-namespaced code naming the outcome of a probe or one of its components, such as
 * `pass` or `warn` or `fail`. Open by design; an unrecognized status must be preserved and
 * surfaced, not coerced to a known one.
 */
export type ProbeStatus = string;
export const PROBE_STATUS_PATTERN: string = "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `ProbeStatus`: the declared pattern and length bounds,
 * applied as a full match. The generated decoders do not call this -- decoding stays tolerant,
 * and this is the primitive a caller validates with.
 */
export function isProbeStatus(value: unknown): value is ProbeStatus {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(PROBE_STATUS_PATTERN).test(value)
  );
}

/**
 * Canonical dialable Core transport endpoint. Lowercase HTTP and HTTPS require a valid host and
 * an optional port in 1-65535 written without leading zeros, and carry no query and no fragment.
 * Local IPC uses an absolute `.sock` Unix-domain socket URI or a safe Windows named-pipe URI.
 * URI userinfo, direct-storage schemes, credential-bearing queries, fragments and unapproved
 * transports are forbidden. This pattern is the single authority for the policy: every generated
 * binding compiles it directly, so no runtime adds acceptance rules of its own.
 */
export type ServiceEndpointUri = string;
export const SERVICE_ENDPOINT_URI_PATTERN: string =
  "^(?:https?://(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?:\\.(?" +
  ":25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}|[A-Za-z0-9](?:[A-Za-" +
  "z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-" +
  "z0-9])?)*|\\[(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-" +
  "f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A" +
  "-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1" +
  ",4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa" +
  "-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|" +
  "[0-9A-Fa-f]{1,4}:(?:(?::[0-9A-Fa-f]{1,4}){1,6})|:(?:(?::[0-9A-Fa-f]{" +
  "1,4}){1,7}|:))\\])(?::(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}" +
  "|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?(?:/(?:[A-Za-z0-9\\-._~!$&" +
  "'()*+,;=:@]|%[0-9A-F]{2})*)*|unix:///(?!\\.{1,2}(?:/|$))(?!.*?/\\.{1,2" +
  "}(?:/|$))(?!.*//)(?!.*%2[EF])(?:[A-Za-z0-9\\-._~!$&'()*+,;=:@]|%[0-9A" +
  "-F]{2})+(?:/(?:[A-Za-z0-9\\-._~!$&'()*+,;=:@]|%[0-9A-F]{2})+)*\\.sock|" +
  "pipe://[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198}[A-Za-z0-9])?)$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `ServiceEndpointUri`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isServiceEndpointUri(value: unknown): value is ServiceEndpointUri {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 2048 &&
    new RegExp(SERVICE_ENDPOINT_URI_PATTERN).test(value)
  );
}

/**
 * Assert the endpoint policy without including a rejected value in the error.
 */
export function assertServiceEndpointUri(value: unknown): asserts value is ServiceEndpointUri {
  if (!isServiceEndpointUri(value)) {
    throw new TypeError("endpoint_uri is not an approved credential-free dialable Core transport URI");
  }
}

/**
 * Closed vocabulary naming how a Core target is reached: `local` for a same-host instance,
 * `private_remote` for an instance reached over a private, self-hosted network, `cloud` for a
 * hosted multi-tenant instance. Closed to exactly these three; an unknown kind fails closed
 * rather than being guessed at.
 */
export type CoreTargetKind = string;

/**
 * The closed `CoreTargetKind` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_TARGET_KIND_VALUES = [
  "local",
  "private_remote",
  "cloud",
] as const;

/**
 * Return whether a value is a declared `CoreTargetKind`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isCoreTargetKind(value: unknown): value is CoreTargetKind {
  return (
    typeof value === "string" &&
    (CORE_TARGET_KIND_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed vocabulary naming who owns a Core target's process lifecycle: `locally_managed` when
 * this client started and owns the instance, `externally_managed` when some other process or
 * operator owns it. Closed to exactly these two; an unknown value fails closed. A lifecycle
 * action such as `start`/`stop`/`restart` is only ever safe to offer for a `locally_managed`
 * target, since only the owner of a process may start or stop it.
 */
export type CoreTargetManagement = string;

/**
 * The closed `CoreTargetManagement` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_TARGET_MANAGEMENT_VALUES = [
  "locally_managed",
  "externally_managed",
] as const;

/**
 * Return whether a value is a declared `CoreTargetManagement`. The generated decoders do not
 * call this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isCoreTargetManagement(value: unknown): value is CoreTargetManagement {
  return (
    typeof value === "string" &&
    (CORE_TARGET_MANAGEMENT_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed, normalized vocabulary for a target's lifecycle phase, deliberately narrower than a
 * provider's raw lifecycle codes so a safe status may publish it pre-authentication. `failed` is
 * a real service failure -- the process reached a terminal error rather than a requested stop --
 * and is distinct from `stopped`, which is the ordinary not-running phase; neither carries a
 * reason, because a safe status may not publish one. Closed to exactly these values; an unknown
 * value fails closed.
 */
export type CoreLifecycleState = string;

/**
 * The closed `CoreLifecycleState` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_LIFECYCLE_STATE_VALUES = [
  "starting",
  "running",
  "stopping",
  "stopped",
  "failed",
  "unknown",
] as const;

/**
 * Return whether a value is a declared `CoreLifecycleState`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isCoreLifecycleState(value: unknown): value is CoreLifecycleState {
  return (
    typeof value === "string" &&
    (CORE_LIFECYCLE_STATE_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed, normalized vocabulary for whether a target is ready to serve requests now. Closed to
 * exactly these values; an unknown value fails closed.
 */
export type CoreReadinessState = string;

/**
 * The closed `CoreReadinessState` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_READINESS_STATE_VALUES = [
  "ready",
  "not_ready",
  "unknown",
] as const;

/**
 * Return whether a value is a declared `CoreReadinessState`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isCoreReadinessState(value: unknown): value is CoreReadinessState {
  return (
    typeof value === "string" &&
    (CORE_READINESS_STATE_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed, normalized vocabulary mirroring `compatibility.schema.json`'s `x-omnivia-
 * compatibility-statuses`, restated here as a closed enum because a safe status is published
 * pre-authentication and must fail closed on a value it does not recognize rather than forward
 * an unrecognized open code to that surface.
 */
export type CoreCompatibilityState = string;

/**
 * The closed `CoreCompatibilityState` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_COMPATIBILITY_STATE_VALUES = [
  "compatible",
  "compatible_with_deprecations",
  "upgrade_required",
  "incompatible",
  "unknown",
] as const;

/**
 * Return whether a value is a declared `CoreCompatibilityState`. The generated decoders do not
 * call this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isCoreCompatibilityState(value: unknown): value is CoreCompatibilityState {
  return (
    typeof value === "string" &&
    (CORE_COMPATIBILITY_STATE_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed, normalized vocabulary for a target's transport connection state.
 * `authentication_required` is the normalized state of a target that is reachable but will not
 * serve this caller until it authenticates -- distinct from `disconnected`, which says nothing
 * about why. The matching `authentication_required` `CoreSafeWarningCode` stays the actionable
 * advisory a caller surfaces; this field is the state. Closed to exactly these values; an
 * unknown value fails closed.
 */
export type CoreConnectionState = string;

/**
 * The closed `CoreConnectionState` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_CONNECTION_STATE_VALUES = [
  "connected",
  "connecting",
  "disconnected",
  "unreachable",
  "authentication_required",
  "unknown",
] as const;

/**
 * Return whether a value is a declared `CoreConnectionState`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isCoreConnectionState(value: unknown): value is CoreConnectionState {
  return (
    typeof value === "string" &&
    (CORE_CONNECTION_STATE_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed vocabulary of non-fatal advisories a safe status may attach. Deliberately a closed enum
 * rather than `common.schema.json`'s open `OpenCode`: a safe status is published pre-
 * authentication and must never carry a free-form reason or an unrecognized code a caller cannot
 * reason about.
 */
export type CoreSafeWarningCode = string;

/**
 * The closed `CoreSafeWarningCode` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_SAFE_WARNING_CODE_VALUES = [
  "endpoint_unreachable",
  "authentication_required",
  "version_incompatible",
  "upgrade_required",
  "workspace_format_incompatible",
  "degraded",
] as const;

/**
 * Return whether a value is a declared `CoreSafeWarningCode`. The generated decoders do not call
 * this -- decoding stays tolerant and preserves an unrecognized value -- and this is the
 * primitive a caller enforcing the closed domain validates with.
 */
export function isCoreSafeWarningCode(value: unknown): value is CoreSafeWarningCode {
  return (
    typeof value === "string" &&
    (CORE_SAFE_WARNING_CODE_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Closed vocabulary of actions a caller may be permitted to attempt against a target, given only
 * what a safe status may say pre-authentication. `start`/`stop`/`restart` are process-lifecycle
 * actions and must only be offered for a `locally_managed` `local` target (see
 * `CoreSafeStatusV1.permitted_actions`); `reconnect` and `open` are safe to offer for any
 * target.
 */
export type CoreSafeAction = string;

/**
 * The closed `CoreSafeAction` vocabulary, emitted from the schema's `enum`.
 */
export const CORE_SAFE_ACTION_VALUES = [
  "start",
  "stop",
  "restart",
  "reconnect",
  "open",
] as const;

/**
 * Return whether a value is a declared `CoreSafeAction`. The generated decoders do not call this
 * -- decoding stays tolerant and preserves an unrecognized value -- and this is the primitive a
 * caller enforcing the closed domain validates with.
 */
export function isCoreSafeAction(value: unknown): value is CoreSafeAction {
  return (
    typeof value === "string" &&
    (CORE_SAFE_ACTION_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Open, dot-namespaced code naming a workspace's lifecycle status, such as `active` or
 * `provisioning` or `archived`. Open by design so a compatible minor release can add statuses
 * without breaking existing decoders.
 */
export type WorkspaceStatus = string;
export const WORKSPACE_STATUS_PATTERN: string =
  "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])";

/**
 * Return whether a value is a well-formed `WorkspaceStatus`: the declared pattern and length
 * bounds, applied as a full match. The generated decoders do not call this -- decoding stays
 * tolerant, and this is the primitive a caller validates with.
 */
export function isWorkspaceStatus(value: unknown): value is WorkspaceStatus {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 128 &&
    new RegExp(WORKSPACE_STATUS_PATTERN).test(value)
  );
}

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
 * The conversation a chat command changes, and the revision the caller believes it is at.
 * Optimistic concurrency for the conversation aggregate, stated as both counters rather than
 * one: a conversation's `graph_revision` and its append position move independently, so
 * expecting only one of them admits a command whose view is stale in exactly the half it did not
 * state. A mismatch is a `conflict` the caller re-reads and re-decides against; it is not a
 * `mutation_precondition_failed`, which names a record version the caller refreshes and retries.
 */
export interface ChatConversationExpectation {
  /**
   * Identifier of the conversation this command expects to change.
   */
  readonly conversation_id: Identifier;
  /**
   * The conversation graph's optimistic revision token as the caller last observed it.
   */
  readonly graph_revision: number;
  /**
   * The conversation's latest append position as the caller last observed it.
   */
  readonly latest_conversation_sequence: number;
}

/**
 * Result of `chat.command`: the settled command's own Chat Contract v1 result envelope, echoed
 * with the command name it answers. The chat result is carried opaquely for the same reason the
 * request is. A replayed submission returns the stored result of the command that already ran,
 * not a second settlement.
 */
export interface ChatCommandResult {
  /**
   * The Chat Contract v1 command name this result answers. Echoes the request.
   */
  readonly command_name: Identifier;
  /**
   * The Chat Contract v1 `CommandResultEnvelope` the command produced, carried verbatim.
   */
  readonly command_result: JsonObject;
  /**
   * The conversation the settled command changed, where it changed one.
   */
  readonly conversation_id?: Identifier;
}

/**
 * One durable generation-lifecycle event, as the workspace recorded it. Provider content is
 * never carried: `payload` holds only the sanitised, closed-vocabulary fields the workspace
 * persisted, and no request body, response body, header, URL or credential has a path into it.
 */
export interface ChatGenerationEvent {
  /**
   * Identifier of this durable event.
   */
  readonly event_id: Identifier;
  /**
   * The durable event type, such as `chat.generation.started`. Open by design so a compatible
   * minor release can add lifecycle vocabulary.
   */
  readonly event_type: OpenCode;
  /**
   * This event's position in its generation's contiguous history, counting from one.
   */
  readonly generation_event_sequence: number;
  /**
   * The server-issued cursor naming this position. Round-tripped verbatim as a later request's
   * `after_cursor`; never parsed.
   */
  readonly cursor: OpaqueToken;
  /**
   * When the workspace recorded this event.
   */
  readonly occurred_at: Timestamp;
  /**
   * The event's sanitised durable payload.
   */
  readonly payload?: JsonObject;
}

/**
 * Input for `chat.events`: replay one generation's durable event history after a cursor. A
 * request carrying no `after_cursor` replays the whole history. Transport-level streaming is out
 * of scope: this is a replay of what was recorded, not a subscription. Workspace-scoped through
 * the request envelope's selected workspace, so this payload never carries a second, independent
 * workspace identifier.
 */
export interface ChatEventsInput {
  /**
   * Identifier of the generation whose events to replay.
   */
  readonly generation_job_id: Identifier;
  /**
   * Replay strictly after this position. Absent replays from the beginning.
   */
  readonly after_cursor?: OpaqueToken;
}

/**
 * Input for `chat.snapshot`: the authoritative selected-path snapshot of one conversation, the
 * answer a caller takes when `chat.events` tells it a cursor can no longer be continued.
 * `snapshot_query` is the Chat Contract v1 `ConversationSnapshotQuery` document, carried
 * verbatim and opaque to this envelope, for the same reason `chat.command` carries its command
 * that way -- Chat's snapshot shape is already frozen in `contracts/chat/v1`, and restating it
 * here would create a second, drifting copy. `conversation_id` is the addressed conversation
 * stated natively, so authorization and audit read one identifier rather than parsing a document
 * this boundary does not validate. Workspace-scoped through the request envelope's selected
 * workspace, so this payload never carries a second, independent workspace identifier.
 */
export interface ChatSnapshotInput {
  /**
   * Identifier of the conversation to snapshot. Must be the conversation `snapshot_query`
   * names.
   */
  readonly conversation_id: Identifier;
  /**
   * The Chat Contract v1 `ConversationSnapshotQuery` document, carried verbatim. Decoded and
   * validated against the chat contract, never against this one.
   */
  readonly snapshot_query: JsonObject;
}

/**
 * Result of `chat.snapshot`: the Chat Contract v1 `ConversationSnapshotResult` document the
 * query produced -- conversation, resolved active branch path and actor view state -- carried
 * opaquely for the same reason the request is, and echoed with the conversation it answers. A
 * snapshot is a complete authoritative read at one revision, not a continuation, so there is no
 * cursor to honour and no resnapshot branch to signal.
 */
export interface ChatSnapshotResult {
  /**
   * Identifier of the conversation this snapshot answers. Echoes the request.
   */
  readonly conversation_id: Identifier;
  /**
   * The Chat Contract v1 `ConversationSnapshotResult` document, carried verbatim.
   */
  readonly snapshot: JsonObject;
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
 * A workspace-scoped, source-qualified pointer at a fact recorded somewhere other than this
 * record. Every correlation states which domain its identifier belongs to, because the same
 * spelling means different things in different domains: an agent-lane ledger `run_id` and a
 * canonical `Run.run_id` are different identifiers that must never be joined. Only a `runtime`
 * reference is authoritative; every other kind is correlation or evidence, never a source of
 * truth, so discovering a fact through one never makes it a fact this run may act on.
 */
export interface ExternalReference {
  /**
   * Which domain `source_id` is an identifier in.
   */
  readonly source_kind: RuntimeSourceKind;
  /**
   * The identifier this reference names, in the domain `source_kind` states. Opaque: it is
   * round-tripped verbatim and never parsed, and never rewritten into an identifier of another
   * domain.
   */
  readonly source_id: OpaqueToken;
  /**
   * Workspace this correlation is scoped to. A reference is never workspace-free: an
   * identifier without the workspace it was issued in cannot be resolved, and could be
   * resolved against the wrong one.
   */
  readonly workspace_id: WorkspaceId;
}

/**
 * The exact executable definition a run was admitted to execute: which kind, which definition,
 * and at which released version. Immutable for the life of the run -- a run does not change what
 * it is running -- so the definition reported on a completed run is the one it was admitted
 * with.
 */
export interface RunDefinitionRef {
  /**
   * Whether this run executes an Agent Component or a Workflow.
   */
  readonly definition_kind: RunDefinitionKind;
  /**
   * Stable identifier of the definition.
   */
  readonly definition_id: Identifier;
  /**
   * The released version of the definition this run executes.
   */
  readonly definition_version: ReleaseVersion;
}

/**
 * The immutable policy a run is pinned to at one revision. It states two capability sets and
 * keeps them apart on purpose: `granted_capabilities` is what this run may actually invoke, and
 * `discovered_capabilities` is what the workspace was found to offer. Discovery is not authority
 * -- a capability that appears only in the discovered set authorizes nothing, and an effect
 * naming one is refused. A run's policy may be re-pinned as it proceeds, but only monotonically:
 * revisions move forward and grants may narrow, never widen, so authority a run once held cannot
 * be silently re-granted mid-flight.
 */
export interface PolicySnapshot {
  /**
   * Workspace this snapshot was pinned in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this snapshot, unique within its workspace.
   */
  readonly policy_snapshot_id: Identifier;
  /**
   * The run this snapshot is pinned to.
   */
  readonly run_id: Identifier;
  /**
   * 1-based revision of this run's policy. Strictly increasing within one run.
   */
  readonly revision: number;
  /**
   * When this revision was pinned.
   */
  readonly pinned_at: Timestamp;
  /**
   * The capabilities this run may invoke. May be empty: a run that invokes nothing is granted
   * nothing.
   */
  readonly granted_capabilities: readonly CapabilityId[];
  /**
   * The capabilities the workspace was found to offer. Informational only; presence here never
   * authorizes an effect.
   */
  readonly discovered_capabilities: readonly CapabilityId[];
  /**
   * Open code naming why the policy resolved as it did, such as `workspace_default` or
   * `operator_narrowed`.
   */
  readonly decision_reason: OpenCode;
  /**
   * Immutable reference to the audit record for pinning this revision.
   */
  readonly audit_reference: AuditReference;
}

/**
 * The immutable budget a run is pinned to at one revision: the ceilings admission accepted, and
 * what has been consumed against them so far. Limits are pinned at admission and may narrow but
 * never widen as a run proceeds, and consumption never decreases: a counter that went backwards
 * is a report nobody can audit. Consumption never exceeds its ceiling -- a run that would exceed
 * one stops rather than reporting a number its own limit forbids.
 */
export interface BudgetSnapshot {
  /**
   * Workspace this snapshot was pinned in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this snapshot, unique within its workspace.
   */
  readonly budget_snapshot_id: Identifier;
  /**
   * The run this snapshot is pinned to.
   */
  readonly run_id: Identifier;
  /**
   * 1-based revision of this run's budget. Strictly increasing within one run.
   */
  readonly revision: number;
  /**
   * When this revision was pinned.
   */
  readonly pinned_at: Timestamp;
  /**
   * Ceiling on cost units for this run.
   */
  readonly max_cost_units: number;
  /**
   * Cost units consumed so far. Never greater than `max_cost_units`.
   */
  readonly consumed_cost_units: number;
  /**
   * Ceiling on token units for this run.
   */
  readonly max_token_units: number;
  /**
   * Token units consumed so far. Never greater than `max_token_units`.
   */
  readonly consumed_token_units: number;
  /**
   * Ceiling on wall-clock duration for this run, when one applies.
   */
  readonly max_wall_clock_ms?: DurationMs;
}

/**
 * One capability issued to one run, for the life of that run. A grant is per-run and backed by
 * the policy revision that issued it: it names the `PolicySnapshot` whose `granted_capabilities`
 * contains it, so a grant can always be traced to the decision that made it and can never
 * outlive a narrowing of that decision. Static manifest wiring is not a grant, and a capability
 * the workspace merely offers is not a grant either.
 */
export interface CapabilityGrant {
  /**
   * Workspace this grant was issued in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this grant, unique within its workspace.
   */
  readonly capability_grant_id: Identifier;
  /**
   * The run this grant was issued to.
   */
  readonly run_id: Identifier;
  /**
   * The capability granted.
   */
  readonly capability_id: CapabilityId;
  /**
   * The policy revision that issued this grant. Its `granted_capabilities` must contain
   * `capability_id`.
   */
  readonly policy_snapshot_id: Identifier;
  /**
   * When this grant was issued.
   */
  readonly granted_at: Timestamp;
  /**
   * When this grant stops being usable, when it is time-bounded.
   */
  readonly expires_at?: Timestamp;
  /**
   * Scope tokens narrowing this grant. Scopes narrow; they never widen granted authority.
   */
  readonly scopes: readonly Scope[];
  /**
   * The purpose limitation this grant was issued under.
   */
  readonly purpose: Purpose;
}

/**
 * One durable suspension of a run: what it is waiting for, whether it is still waiting, and the
 * digest that binds the state it will resume from. First-class rather than a scheduler detail,
 * because a suspended run is a state the contract must be able to state, and because resuming
 * from a checkpoint nobody can identify is indistinguishable from starting again. Resolved by
 * exactly one `ResolveWait`, which is a Runtime command and not `job.retry`: nothing here
 * requeues a job.
 */
export interface Wait {
  /**
   * Workspace this wait was created in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this wait, unique within its workspace.
   */
  readonly wait_id: Identifier;
  /**
   * The run this wait suspends.
   */
  readonly run_id: Identifier;
  /**
   * The step this wait suspends.
   */
  readonly run_step_id: Identifier;
  /**
   * What this wait is waiting for. Fixes which resolution may resolve it.
   */
  readonly kind: WaitKind;
  /**
   * Where this wait stands.
   */
  readonly status: WaitStatus;
  /**
   * When this wait was created.
   */
  readonly created_at: Timestamp;
  /**
   * When this wait stops being resolvable, when it is time-bounded.
   */
  readonly expires_at?: Timestamp;
  /**
   * When this wait stopped being pending. Present exactly when the wait is no longer
   * `pending`.
   */
  readonly resolved_at?: Timestamp;
  /**
   * Open code naming why this wait stopped being pending, such as `approved` or
   * `deadline_exceeded`.
   */
  readonly resolution_reason?: OpenCode;
  /**
   * The approval that resolved this wait, present only on a resolved `approval` wait.
   */
  readonly approval_id?: Identifier;
  /**
   * Digest binding the state this wait resumes from, so a resolution proves it is resuming the
   * state that was suspended rather than some later one. The same canonical `ContentChecksum`
   * the import contract uses: a value both sides recompute and compare byte for byte, never an
   * opaque server token.
   */
  readonly resume_digest: ContentChecksum;
}

/**
 * The immutable request/decision pair for one approval `Wait`: who was asked, who answered, what
 * they answered, and when. The request half is written when the wait is created and never
 * edited; the decision half is written once and never edited either, so an approval cannot be
 * re-decided, only superseded by a new run. A request with no decision is still pending; a
 * decision is complete or absent, never partial -- a recorded decision always carries its
 * decider, its instant and its audit reference, because a decision nobody can attribute is not
 * an approval.
 */
export interface Approval {
  /**
   * Workspace this approval was requested in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this approval, unique within its workspace.
   */
  readonly approval_id: Identifier;
  /**
   * The run this approval belongs to.
   */
  readonly run_id: Identifier;
  /**
   * The wait this approval resolves, or is expected to resolve.
   */
  readonly wait_id: Identifier;
  /**
   * When the approval was requested.
   */
  readonly requested_at: Timestamp;
  /**
   * The role asked to decide.
   */
  readonly approver_role: Identifier;
  /**
   * The principal the request was assigned to, when it was assigned to one rather than left to
   * the role.
   */
  readonly assigned_to?: Identifier;
  /**
   * The principal or role the request was escalated to, when it was escalated.
   */
  readonly escalated_to?: Identifier;
  /**
   * When the request stops being decidable, when it is time-bounded.
   */
  readonly expires_at?: Timestamp;
  /**
   * What was decided. Absent while the request is pending.
   */
  readonly decision?: ApprovalDecision;
  /**
   * When the decision was recorded. Present exactly when `decision` is.
   */
  readonly decided_at?: Timestamp;
  /**
   * The principal who decided. Present exactly when `decision` is.
   */
  readonly decided_by?: Identifier;
  /**
   * Immutable reference to the audit record for the decision. Present exactly when `decision`
   * is.
   */
  readonly audit_reference?: AuditReference;
  /**
   * Human-readable note the decider left. Not a stable interface.
   */
  readonly comment?: string;
}

/**
 * The record written *before* a run acts on the world: which capability it will invoke, under
 * which grant, with which logically idempotent key, over exactly which request bytes. Nothing
 * may be observed or settled that was not first intended -- a receipt or settlement with no
 * matching intent is an effect nobody authorized and nobody can reconcile. The pairing of
 * `idempotency_key` and `request_digest` is what makes idempotency *logical* rather than
 * accidental: the same key over the same digest is one effect however many times it is
 * delivered, and the same key over a different digest is a conflict rather than a replay.
 */
export interface EffectIntent {
  /**
   * Workspace this intent was declared in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this intent, unique within its workspace.
   */
  readonly effect_intent_id: Identifier;
  /**
   * The run that declared this intent.
   */
  readonly run_id: Identifier;
  /**
   * The step that declared this intent.
   */
  readonly run_step_id: Identifier;
  /**
   * The attempt that declared this intent.
   */
  readonly attempt_id: Identifier;
  /**
   * The capability this effect invokes.
   */
  readonly capability_id: CapabilityId;
  /**
   * The grant authorizing this effect. A capability that was merely discovered is not a grant.
   */
  readonly capability_grant_id: Identifier;
  /**
   * Open code naming what kind of effect this is, such as `record_write` or `message_send`.
   */
  readonly effect_kind: OpenCode;
  /**
   * The stable logical key for this effect. Equal keys with different request digests are an
   * `idempotency_conflict`, exactly as on the application wire.
   */
  readonly idempotency_key: IdempotencyKey;
  /**
   * Digest of the exact request bytes this intent will send, so a replay proves it is the same
   * effect rather than asserting it.
   */
  readonly request_digest: ContentChecksum;
  /**
   * When this intent was declared. No receipt or settlement for this effect may precede it.
   */
  readonly declared_at: Timestamp;
}

/**
 * The final, audited answer to what happened to one intended effect. Like a receipt it never
 * stands alone: it names its intent, shares that intent's run and workspace, and never settles
 * before the intent was declared. A `committed` settlement must name the receipt that proves it
 * -- a claim that an effect landed, with no observation of it landing, is an assertion rather
 * than a settlement. An `unknown` settlement is the honest third answer and is not a failure: it
 * says reconciliation is owed, and a run holding one may not call itself succeeded or failed.
 */
export interface EffectSettlement {
  /**
   * Workspace this settlement was recorded in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this settlement, unique within its workspace.
   */
  readonly effect_settlement_id: Identifier;
  /**
   * The run this settlement belongs to.
   */
  readonly run_id: Identifier;
  /**
   * The intent this settlement settles. Required: there is no settlement without an intent.
   */
  readonly effect_intent_id: Identifier;
  /**
   * What the runtime established about the effect.
   */
  readonly outcome: EffectOutcome;
  /**
   * The receipt proving a `committed` outcome. Present exactly when the outcome is
   * `committed`.
   */
  readonly effect_receipt_id?: Identifier;
  /**
   * When this settlement was recorded. Never earlier than the intent's `declared_at`.
   */
  readonly settled_at: Timestamp;
  /**
   * Open code naming how the outcome was established, such as `receipt_observed` or
   * `provider_unreachable`.
   */
  readonly reason: OpenCode;
  /**
   * Immutable reference to the audit record for this settlement.
   */
  readonly audit_reference: AuditReference;
}

/**
 * One entry in a run's ordered event stream. Sequences are contiguous from zero and never
 * renumbered, instants never regress, and each entry states the run status in force when it was
 * recorded, so the stream is a readable history rather than a set of notes. Append-only: an
 * event is never edited or removed, and a correction is a further event.
 */
export interface RuntimeEvent {
  /**
   * Workspace this event was recorded in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this event, unique within its workspace.
   */
  readonly runtime_event_id: Identifier;
  /**
   * The run this event belongs to.
   */
  readonly run_id: Identifier;
  /**
   * 0-based position of this event within its run's stream.
   */
  readonly sequence: number;
  /**
   * When this event occurred. Never earlier than the preceding event's.
   */
  readonly occurred_at: Timestamp;
  /**
   * Open code naming what happened, such as `run_admitted` or `wait_resolved`.
   */
  readonly event_kind: OpenCode;
  /**
   * The run status in force when this event was recorded.
   */
  readonly run_status: RunStatus;
  /**
   * The step this event is about, when it is about one.
   */
  readonly run_step_id?: Identifier;
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
 * One content-addressed output a run produced, bound to the run that produced it. Identified by
 * digest rather than by location: the bytes are the identity, so an artifact can be proven
 * unchanged without knowing where it is stored. Carries no filesystem path, URL, bucket,
 * credential or storage option -- where an artifact lives is a storage decision and never a wire
 * fact.
 */
export interface Artifact {
  /**
   * Workspace this artifact was produced in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this artifact, unique within its workspace.
   */
  readonly artifact_id: Identifier;
  /**
   * The run that produced this artifact.
   */
  readonly run_id: Identifier;
  /**
   * The step that produced this artifact, when one step did.
   */
  readonly run_step_id?: Identifier;
  /**
   * Open code naming what kind of output this is, such as `report` or `transcript`.
   */
  readonly artifact_kind: OpenCode;
  /**
   * Media type of this artifact's content. The same `MediaType` L0 evidence carries, so one
   * type describes one concept wherever it is reached from.
   */
  readonly media_type: MediaType;
  /**
   * Digest of this artifact's content, proving the bytes have not changed since they were
   * produced.
   */
  readonly content_checksum: ContentChecksum;
  /**
   * Length of this artifact's content in bytes. Zero is valid: empty output is still output.
   */
  readonly content_length_bytes: number;
  /**
   * When this artifact was produced.
   */
  readonly produced_at: Timestamp;
}

/**
 * The record that cleanup was attempted, and what it achieved. Cleanup is observable rather than
 * assumed: a run that reached a terminal status states what it released, including cleanup that
 * failed. A receipt is written for the attempt, not for the success, so a failed release is
 * visible instead of being indistinguishable from one that never ran.
 */
export interface CleanupReceipt {
  /**
   * Workspace this cleanup was performed in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this receipt, unique within its workspace.
   */
  readonly cleanup_receipt_id: Identifier;
  /**
   * The run this cleanup was performed for.
   */
  readonly run_id: Identifier;
  /**
   * Open code naming what was cleaned up, such as `capability_grant` or `staged_content`.
   */
  readonly resource_kind: OpenCode;
  /**
   * What this cleanup achieved.
   */
  readonly outcome: CleanupOutcome;
  /**
   * Open code naming why this outcome was reached, such as `run_terminal` or
   * `resource_locked`.
   */
  readonly reason: OpenCode;
  /**
   * When this cleanup was performed.
   */
  readonly performed_at: Timestamp;
  /**
   * Immutable reference to the audit record for this cleanup.
   */
  readonly audit_reference: AuditReference;
}

/**
 * The Runtime command that resolves exactly one durable `Wait` on one canonical `Run`.
 * Deliberately outside the application job family: it is not `job.retry`, there is no
 * `job.resume`, it is not a `JobControl` member, and it neither requeues a job nor names one --
 * it carries no `job_id`, and nothing here selects a recovery. It names the wait it resolves,
 * how it resolves it, and the resume digest the wait published, so a resolution proves it is
 * resuming the state that was suspended. The resolution must match the wait's kind: a signal
 * never resolves an approval, and an approval decision never resolves a timer.
 */
export interface ResolveWait {
  /**
   * Workspace the wait was created in. Stated explicitly: a resolution is never resolved
   * against the wrong workspace.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * The run whose wait is being resolved.
   */
  readonly run_id: Identifier;
  /**
   * The one wait this command resolves.
   */
  readonly wait_id: Identifier;
  /**
   * How the wait is resolved. Must match the wait's own kind.
   */
  readonly resolution: WaitResolution;
  /**
   * The recorded approval carrying the decision. Present exactly when `resolution` is
   * `approval_decision`.
   */
  readonly approval_id?: Identifier;
  /**
   * The digest the wait published. A resolution quoting a different digest is resuming a
   * different state and is refused.
   */
  readonly resume_digest: ContentChecksum;
  /**
   * When this resolution was requested.
   */
  readonly requested_at: Timestamp;
  /**
   * Open code naming why the wait is being resolved, such as `approved` or `signal_received`.
   * Never an authorization input.
   */
  readonly reason: OpenCode;
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
 * The provider-neutral identity of one Core target a client may select and address: what kind of
 * instance it is, which workspace it serves, who manages its process lifecycle, and an opaque
 * reference to its endpoint profile. Deliberately carries no dialable endpoint, credential,
 * process identity, or filesystem path of its own; resolving `endpoint_profile_ref` to a
 * concrete transport address is a separate, authenticated step this descriptor does not perform.
 */
export interface CoreTargetV1 {
  /**
   * Contract version this target descriptor is published at.
   */
  readonly contract_version: ContractVersion;
  /**
   * Stable, caller-opaque identifier of this target. Never a workspace, service instance, or
   * installation identifier -- selecting a target is a separate concept from any of those.
   */
  readonly target_ref: Identifier;
  /**
   * Human-readable name for this target.
   */
  readonly display_name: string;
  /**
   * How this target is reached.
   */
  readonly kind: CoreTargetKind;
  /**
   * Workspace this target serves.
   */
  readonly workspace_ref: WorkspaceId;
  /**
   * Who owns this target's process lifecycle.
   */
  readonly management: CoreTargetManagement;
  /**
   * Opaque reference to this target's endpoint profile. Never a dialable URI, credential, or
   * filesystem path; resolving it to a concrete transport address is a separate, authenticated
   * step this reference does not perform.
   */
  readonly endpoint_profile_ref: Identifier;
}

/**
 * Assert target semantics without echoing a rejected value. Checks exactly the clauses
 * `validate_core_target` checks in Python: `kind` and `management` against their closed
 * vocabularies, and every scalar against the value domain the schema `$ref`s it to --
 * `contract_version` as a `ContractVersion`, `target_ref` and `endpoint_profile_ref` as
 * `Identifier`s, `workspace_ref` as a `WorkspaceId`, and `display_name` within its declared
 * 1..256 bound. All of them arrive as tolerant strings, and this is where the declared domains
 * are enforced on a publication path.
 */
export function assertCoreTargetV1Semantics(value: CoreTargetV1): void {
  if (!isContractVersion(value.contract_version)) {
    throw new TypeError("contract_version is not a well-formed ContractVersion");
  }
  if (!isIdentifier(value.target_ref)) {
    throw new TypeError("target_ref is not a well-formed Identifier");
  }
  if (
    typeof value.display_name !== "string" ||
    value.display_name.length < 1 ||
    value.display_name.length > 256
  ) {
    throw new TypeError("display_name is not a string of 1..256 characters");
  }
  if (!isCoreTargetKind(value.kind)) {
    throw new TypeError("kind is not a known CoreTargetKind");
  }
  if (!isWorkspaceId(value.workspace_ref)) {
    throw new TypeError("workspace_ref is not a well-formed WorkspaceId");
  }
  if (!isCoreTargetManagement(value.management)) {
    throw new TypeError("management is not a known CoreTargetManagement");
  }
  if (!isIdentifier(value.endpoint_profile_ref)) {
    throw new TypeError("endpoint_profile_ref is not a well-formed Identifier");
  }
}

/**
 * Return whether a structurally decoded target satisfies mandatory semantics. Derived from the
 * assertion rather than restating its clauses, so the predicate and the assertion cannot
 * disagree about one field.
 */
export function isCoreTargetV1SemanticallyValid(value: CoreTargetV1): boolean {
  try {
    assertCoreTargetV1Semantics(value);
    return true;
  } catch {
    return false;
  }
}

/**
 * Assert the set-level authority rule `validate_core_target_authorities` enforces in Python:
 * every target is individually valid, and across the set neither `target_ref` nor
 * `workspace_ref` repeats. A writable workspace identity belongs to exactly one target
 * authority, so two descriptors naming the same `workspace_ref` are two authorities claiming one
 * writable store -- an invariant no single descriptor can see. No refusal echoes a rejected
 * value.
 */
export function assertCoreTargetV1Authorities(value: readonly CoreTargetV1[]): void {
  const targetRefs = new Set<string>();
  const workspaceRefs = new Set<string>();
  for (const target of value) {
    assertCoreTargetV1Semantics(target);
    if (targetRefs.has(target.target_ref)) {
      throw new TypeError("target_ref repeats across the target set");
    }
    if (workspaceRefs.has(target.workspace_ref)) {
      throw new TypeError(
        "workspace_ref repeats across the target set: two target authorities " +
          "may not share one writable workspace identity"
      );
    }
    targetRefs.add(target.target_ref);
    workspaceRefs.add(target.workspace_ref);
  }
}

/**
 * Return whether a set of structurally decoded targets carries non-colliding authorities.
 * Derived from the assertion rather than restating its clauses.
 */
export function areCoreTargetV1AuthoritiesValid(value: readonly CoreTargetV1[]): boolean {
  try {
    assertCoreTargetV1Authorities(value);
    return true;
  } catch {
    return false;
  }
}

/**
 * Input for `chat.command`: one Chat Contract v1 command, settled through the workspace's single
 * mutation seam. `command_name` names a member of the Chat Contract's own closed command
 * registry and `command` is that command's request document, carried verbatim and opaque to this
 * envelope. Workspace-scoped through the request envelope's selected workspace, so this payload
 * never carries a second, independent workspace identifier. The envelope's `idempotency_key` is
 * required by the catalogue and is what makes a repeated submission answer from the settled
 * outcome rather than appending a second message.
 */
export interface ChatCommandInput {
  /**
   * The Chat Contract v1 command name, such as `SubmitMessage`. Refused when it is not a
   * member of that contract's closed registry.
   */
  readonly command_name: Identifier;
  /**
   * The Chat Contract v1 request document for `command_name`, carried verbatim. Decoded and
   * validated against the chat contract, never against this one.
   */
  readonly command: JsonObject;
  /**
   * The conversation revision this command expects. Absent for a command that touches no
   * existing conversation.
   */
  readonly expected_conversation?: ChatConversationExpectation;
}

/**
 * Result of `chat.events`: the durable event suffix after the requested cursor, or the demand
 * for a fresh snapshot -- never both. When `requires_resnapshot` is true, both `events` and
 * `transport_events` are empty and `resnapshot_reason` states why the requested position could
 * not be honoured; a fabricated continuation is exactly what that answer exists to prevent.
 * Events are strictly increasing, duplicate-free and contiguous from the position the request
 * continued from. The same suffix is offered in two forms: `events` is the backward-compatible
 * sanitised lifecycle projection this envelope has always returned, and the optional
 * `transport_events` is the exact Chat Contract v1 transport stream for the same positions. A
 * caller that understands the chat contract reads `transport_events`; one that does not keeps
 * reading `events` unchanged.
 */
export interface ChatEventsResult {
  /**
   * Identifier of the generation these events belong to. Echoes the request.
   */
  readonly generation_job_id: Identifier;
  /**
   * The backward-compatible sanitised lifecycle projection of the durable events after the
   * requested cursor, in ascending sequence order. Carries no provider content, and stays
   * exactly what it has always been for callers that do not read `transport_events`. Empty
   * when a resnapshot is required.
   */
  readonly events: readonly ChatGenerationEvent[];
  /**
   * The exact Chat Contract v1 transport stream for the same positions, in ascending sequence
   * order. Each item is an exact Chat Contract v1 `ChatEvent` document, validated and emitted
   * against the separately published Chat contract; this application envelope carries it
   * opaquely and deliberately does not restate that union. Order and cursors match `events`
   * position for position. Optional for compatibility: a result that omits it is a well-formed
   * result. Empty when a resnapshot is required, as `events` is.
   */
  readonly transport_events?: readonly JsonObject[];
  /**
   * Whether the caller must take a fresh snapshot instead of continuing from the cursor it
   * presented.
   */
  readonly requires_resnapshot: boolean;
  /**
   * Why a fresh snapshot is required, such as `cursor_unknown_or_expired`. Present only when
   * `requires_resnapshot` is true.
   */
  readonly resnapshot_reason?: OpenCode;
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
 * One execution attempt of one `RunStep`. Immutable once recorded: identity, step, run,
 * workspace and start instant never change, and an attempt terminalizes exactly once. Within a
 * step, attempts are numbered `1..N` contiguously and never overlap; only a `failed`,
 * `cancelled` or `uncertain` attempt may be followed by another, because a `succeeded` attempt
 * is final. An `uncertain` attempt is the case retrying blindly would corrupt: whether its
 * effect landed is not known, so the next step is reconciliation, not repetition.
 */
export interface Attempt {
  /**
   * Workspace this attempt ran in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this attempt, unique within its workspace.
   */
  readonly attempt_id: Identifier;
  /**
   * The run this attempt belongs to.
   */
  readonly run_id: Identifier;
  /**
   * The step this attempt is an execution of.
   */
  readonly run_step_id: Identifier;
  /**
   * 1-based ordinal of this attempt within its step.
   */
  readonly attempt_number: number;
  /**
   * Status this attempt reached.
   */
  readonly status: AttemptStatus;
  /**
   * When this attempt started.
   */
  readonly started_at: Timestamp;
  /**
   * When this attempt finished. Present exactly when the attempt is no longer running.
   */
  readonly finished_at?: Timestamp;
  /**
   * The failure this attempt ended with, when it failed. Never present on an `uncertain`
   * attempt: uncertainty is not failure, and giving it an error would licence the retry the
   * uncertainty forbids.
   */
  readonly failure?: ApiError;
}

/**
 * Evidence that an intended effect was actually observed to execute, bound to the intent that
 * authorized it. A receipt never stands alone: it names its `EffectIntent`, shares that intent's
 * run and workspace, and is never observed before the intent was declared. A provider-side
 * reference may be attached as correlation, but it is an `ExternalReference` and therefore
 * subordinate evidence -- it identifies the effect somewhere else, and never becomes the
 * authority for whether the effect happened here.
 */
export interface EffectReceipt {
  /**
   * Workspace this receipt was observed in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this receipt, unique within its workspace.
   */
  readonly effect_receipt_id: Identifier;
  /**
   * The run this receipt belongs to.
   */
  readonly run_id: Identifier;
  /**
   * The intent this receipt is evidence for. Required: there is no receipt without an intent.
   */
  readonly effect_intent_id: Identifier;
  /**
   * When the effect was observed to have executed. Never earlier than the intent's
   * `declared_at`.
   */
  readonly observed_at: Timestamp;
  /**
   * Digest of the exact response bytes observed, so two deliveries of one logical effect can
   * be compared rather than assumed equal.
   */
  readonly response_digest: ContentChecksum;
  /**
   * Where this effect is identified outside this contract, when it is. Correlation and
   * subordinate evidence only.
   */
  readonly external_reference?: ExternalReference;
}

/**
 * One piece of evidence a run captured, bound to the run that captured it. `authoritative` is
 * the load-bearing field: only evidence the runtime itself recorded may be authoritative, and
 * evidence drawn from an external log, an agent-lane ledger or a control-plane projection is
 * subordinate however complete it looks -- it corroborates the runtime's own record and never
 * replaces it. `retained` states whether the evidence is still held: cancelling a run stops the
 * work, never the record, so a cancelled run's evidence stays retained.
 */
export interface EvidenceItem {
  /**
   * Workspace this evidence was captured in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this evidence item, unique within its workspace.
   */
  readonly evidence_item_id: Identifier;
  /**
   * The run that captured this evidence.
   */
  readonly run_id: Identifier;
  /**
   * The step that captured this evidence, when one step did.
   */
  readonly run_step_id?: Identifier;
  /**
   * Open code naming what this evidence is, such as `model_invocation` or
   * `capability_response`.
   */
  readonly evidence_kind: OpenCode;
  /**
   * Where this evidence came from, qualified by domain. A non-`runtime` source is subordinate
   * evidence and can never be authoritative.
   */
  readonly source: ExternalReference;
  /**
   * Digest of the captured evidence content, proving it has not been altered since capture.
   */
  readonly content_checksum: ContentChecksum;
  /**
   * The artifact holding this evidence's content, when it was stored as one.
   */
  readonly artifact_id?: Identifier;
  /**
   * When this evidence was captured.
   */
  readonly captured_at: Timestamp;
  /**
   * True only when the runtime itself recorded this evidence. Evidence from any other source
   * is subordinate and must state false.
   */
  readonly authoritative: boolean;
  /**
   * True while this evidence is still held. Cancelling a run never sets it false.
   */
  readonly retained: boolean;
}

/**
 * The published coordination facts a client needs to find one running service instance and
 * decide whether it can talk to it, before any request is sent. Coordination data only: a
 * descriptor carries no bearer credential or token, no granted or effective capability
 * authority, no lease ownership, and no database or workspace storage location. A local IPC
 * transport address may contain its bounded socket path. Holding one lets a caller address an
 * endpoint and negotiate versions; it authorizes nothing, and every authority question is still
 * settled by an authenticated request.
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
   * Approved dialable Core transport URI. It never carries userinfo and never names a database
   * file or workspace directory. A `unix:///` value names only a bounded `.sock` transport
   * address; Runtime publication additionally requires it to match the transport-owned
   * endpoint source.
   */
  readonly endpoint_uri: ServiceEndpointUri;
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
 * Return whether a structurally decoded descriptor satisfies mandatory endpoint semantics.
 * Checks exactly the fields `validate_service_endpoint_descriptor` checks in Python: a
 * descriptor one binding publishes and the other refuses is two contracts, not one.
 */
export function isServiceEndpointDescriptorSemanticallyValid(value: ServiceEndpointDescriptor): boolean {
  return isServiceEndpointUri(value.endpoint_uri) && isTimestamp(value.published_at);
}

/**
 * Assert descriptor endpoint semantics without echoing a rejected value.
 */
export function assertServiceEndpointDescriptorSemantics(value: ServiceEndpointDescriptor): void {
  if (!isServiceEndpointDescriptorSemanticallyValid(value)) {
    throw new TypeError("service endpoint descriptor is not safe to publish");
  }
}

/**
 * A Core target's status, narrowed to values safe to publish before authentication or to any
 * caller regardless of authority. It carries no dialable endpoint, process identity, filesystem
 * path, credential, token, callback, account, entitlement, vault reference, stack trace, raw
 * exception, or free-form reason: every advisory is one of the closed `CoreSafeWarningCode`
 * values, and every offered action is one of the closed `CoreSafeAction` values.
 */
export interface CoreSafeStatusV1 {
  /**
   * Contract version this status is published at.
   */
  readonly contract_version: ContractVersion;
  /**
   * The target this status describes.
   */
  readonly target: CoreTargetV1;
  /**
   * Normalized lifecycle phase.
   */
  readonly lifecycle_state: CoreLifecycleState;
  /**
   * Normalized readiness to serve requests.
   */
  readonly readiness_state: CoreReadinessState;
  /**
   * Normalized version compatibility posture.
   */
  readonly compatibility_state: CoreCompatibilityState;
  /**
   * Normalized transport connection state.
   */
  readonly connection_state: CoreConnectionState;
  /**
   * Concrete server build version, when safe to disclose and known.
   */
  readonly server_version?: ReleaseVersion;
  /**
   * Wire protocol version this target is speaking, when known.
   */
  readonly protocol_version?: ContractVersion;
  /**
   * Non-fatal advisories, each a closed `CoreSafeWarningCode`. No two entries may repeat the
   * same code.
   */
  readonly warning_codes: readonly CoreSafeWarningCode[];
  /**
   * Actions the caller may attempt against this target, each a closed `CoreSafeAction`.
   * `start`/`stop`/`restart` are present only when `target.management` is `locally_managed`
   * and `target.kind` is `local`. No two entries may repeat the same action.
   */
  readonly permitted_actions: readonly CoreSafeAction[];
}

/**
 * Process-lifecycle actions: safe to offer only for a `locally_managed` `local` target, because
 * no other target's process is this caller's to act on.
 */
export const CORE_LOCAL_ONLY_ACTIONS = ["start", "stop", "restart"] as const;

/**
 * Assert safe-status semantics before it reaches a public boundary. Checks exactly what
 * `validate_core_safe_status` checks in Python: the nested target, its own `contract_version`
 * and the two optional versions against their value domains, each of the four normalized states
 * against its closed vocabulary, `warning_codes` and `permitted_actions` for their declared
 * caps, for duplicates and for undeclared entries, and then the cross-field invariants the
 * schema cannot express -- the status is published at the target's `contract_version`, and
 * `start`/`stop`/`restart` are refused unless the target is both `locally_managed` and `local`.
 * No refusal includes the rejected value: a safe status is published pre-authentication, and so
 * is anything thrown while validating one.
 */
export function assertCoreSafeStatusV1Semantics(value: CoreSafeStatusV1): void {
  assertCoreTargetV1Semantics(value.target);
  if (!isContractVersion(value.contract_version)) {
    throw new TypeError("contract_version is not a well-formed ContractVersion");
  }
  if (value.contract_version !== value.target.contract_version) {
    throw new TypeError(
      "contract_version does not match the target's contract_version"
    );
  }
  if (value.server_version !== undefined && !isReleaseVersion(value.server_version)) {
    throw new TypeError("server_version is not a well-formed ReleaseVersion");
  }
  if (
    value.protocol_version !== undefined &&
    !isContractVersion(value.protocol_version)
  ) {
    throw new TypeError("protocol_version is not a well-formed ContractVersion");
  }
  if (!isCoreLifecycleState(value.lifecycle_state)) {
    throw new TypeError("lifecycle_state is not a known CoreLifecycleState");
  }
  if (!isCoreReadinessState(value.readiness_state)) {
    throw new TypeError("readiness_state is not a known CoreReadinessState");
  }
  if (!isCoreCompatibilityState(value.compatibility_state)) {
    throw new TypeError("compatibility_state is not a known CoreCompatibilityState");
  }
  if (!isCoreConnectionState(value.connection_state)) {
    throw new TypeError("connection_state is not a known CoreConnectionState");
  }
  if (value.warning_codes.length > 32) {
    throw new TypeError("warning_codes carries more than 32 entries");
  }
  if (new Set(value.warning_codes).size !== value.warning_codes.length) {
    throw new TypeError("warning_codes contains a duplicate code");
  }
  if (!value.warning_codes.every(isCoreSafeWarningCode)) {
    throw new TypeError("warning_codes contains a value that is not a known CoreSafeWarningCode");
  }
  if (value.permitted_actions.length > 16) {
    throw new TypeError("permitted_actions carries more than 16 entries");
  }
  if (new Set(value.permitted_actions).size !== value.permitted_actions.length) {
    throw new TypeError("permitted_actions contains a duplicate action");
  }
  if (!value.permitted_actions.every(isCoreSafeAction)) {
    throw new TypeError("permitted_actions contains a value that is not a known CoreSafeAction");
  }
  const ownsTheProcess =
    value.target.management === "locally_managed" && value.target.kind === "local";
  const localOnly: readonly string[] = CORE_LOCAL_ONLY_ACTIONS;
  if (!ownsTheProcess && value.permitted_actions.some((a) => localOnly.includes(a))) {
    throw new TypeError(
      "start/stop/restart may only be offered for a locally_managed local target"
    );
  }
}

/**
 * Return whether a structurally decoded safe status is safe to publish. Derived from the
 * assertion rather than restating its clauses, so the predicate and the assertion cannot
 * disagree about one field.
 */
export function isCoreSafeStatusV1SemanticallyValid(value: CoreSafeStatusV1): boolean {
  try {
    assertCoreSafeStatusV1Semantics(value);
    return true;
  } catch {
    return false;
  }
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
 * One step of a run, with the complete attempt history that executed it. Steps are ordered
 * `1..N` contiguously within a run and never renumbered; the history is append-only, so a
 * correction is a further attempt rather than an edit to a recorded one. A step that is
 * `waiting` names the `Wait` holding it, because a suspended step that cannot say what it is
 * suspended on cannot be resolved.
 */
export interface RunStep {
  /**
   * Workspace this step ran in.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this step, unique within its workspace.
   */
  readonly run_step_id: Identifier;
  /**
   * The run this step belongs to.
   */
  readonly run_id: Identifier;
  /**
   * 1-based position of this step within its run.
   */
  readonly ordinal: number;
  /**
   * Open code naming what this step does, such as `plan` or `capability_call` or
   * `approval_wait`. Descriptive: it never selects an implementation.
   */
  readonly step_kind: OpenCode;
  /**
   * Status this step currently reports.
   */
  readonly status: RunStepStatus;
  /**
   * When this step was created.
   */
  readonly created_at: Timestamp;
  /**
   * When this step was last observed to change.
   */
  readonly updated_at: Timestamp;
  /**
   * Every execution attempt of this step, in ascending attempt number. Empty for a step that
   * never executed.
   */
  readonly attempts: readonly Attempt[];
  /**
   * The wait holding this step, present exactly when the step is `waiting`.
   */
  readonly wait_id?: Identifier;
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
 * Assert probe-result semantics before it reaches a public boundary. Checks exactly what
 * `validate_service_probe_result` checks in Python: its own `observed_at`, then the nested
 * descriptor. `observed_at` is refused under its own message rather than the descriptor's,
 * because a caller cannot otherwise tell which field was unusable.
 */
export function assertServiceProbeResultSemantics(value: ServiceProbeResult): void {
  if (!isTimestamp(value.observed_at)) {
    throw new TypeError("observed_at is not a canonical RFC 3339 UTC Timestamp");
  }
  const descriptor = value.descriptor;
  if (descriptor !== undefined && descriptor !== null) {
    assertServiceEndpointDescriptorSemantics(descriptor);
  }
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
 * One canonical execution of an Agent Component or Workflow, with the complete history that
 * produced it. Workspace-scoped by construction: the run and every record hanging off it state
 * the same `workspace_id`, so no part of a run's history can be read against the wrong
 * workspace. `logical_key` is the run's stable idempotency identity -- two admissions carrying
 * the same logical key are the same run replayed, not two runs -- and it is what makes a replay
 * provable rather than assumed. The aggregate is deliberately complete: a run states its policy,
 * its budget, its grants, its steps, its waits and approvals, its intended and settled effects,
 * its event stream, its artifacts, its evidence and its cleanup, because each of those is a
 * question a reader of a finished run has to be able to answer without a second read. Every one
 * of those arrays is stated, empty when there is nothing in it: a run with no waits and a run
 * that forgot to mention its waits are not two readings a caller should have to tell apart, and
 * `finished_at` is the single optional field, absent exactly while the run has not finished.
 */
export interface Run {
  /**
   * Workspace this run belongs to. Every record in this run restates it, and every one must
   * agree.
   */
  readonly workspace_id: WorkspaceId;
  /**
   * Identifier of this run, unique within its workspace. Never joined to an identifier from
   * another domain, however similarly spelled.
   */
  readonly run_id: Identifier;
  /**
   * The executable definition this run executes.
   */
  readonly definition: RunDefinitionRef;
  /**
   * Where this run stands.
   */
  readonly status: RunStatus;
  /**
   * The stable logical identity of the work this run performs. Equal keys over equal
   * definitions are one run replayed; equal keys over different definitions are a conflict.
   */
  readonly logical_key: IdempotencyKey;
  /**
   * The application operation whose invocation admitted this run.
   */
  readonly originating_operation: OperationName;
  /**
   * Immutable reference to the audit record for this run's admission.
   */
  readonly audit_reference: AuditReference;
  /**
   * When this run was admitted.
   */
  readonly created_at: Timestamp;
  /**
   * When this run was last observed to change.
   */
  readonly updated_at: Timestamp;
  /**
   * When this run reached a terminal status, when it has.
   */
  readonly finished_at?: Timestamp;
  /**
   * The policy revision currently in force for this run.
   */
  readonly policy: PolicySnapshot;
  /**
   * The budget revision currently in force for this run.
   */
  readonly budget: BudgetSnapshot;
  /**
   * Every capability issued to this run. May be empty: a run that invokes nothing is granted
   * nothing.
   */
  readonly capability_grants: readonly CapabilityGrant[];
  /**
   * Every step of this run, in ascending ordinal.
   */
  readonly steps: readonly RunStep[];
  /**
   * Every durable wait this run has entered, resolved or otherwise.
   */
  readonly waits: readonly Wait[];
  /**
   * Every approval requested for this run, decided or otherwise.
   */
  readonly approvals: readonly Approval[];
  /**
   * Every effect this run declared before acting. Nothing observed or settled may be absent
   * from here.
   */
  readonly effect_intents: readonly EffectIntent[];
  /**
   * Every observation that an intended effect executed.
   */
  readonly effect_receipts: readonly EffectReceipt[];
  /**
   * Every audited answer to what happened to an intended effect.
   */
  readonly effect_settlements: readonly EffectSettlement[];
  /**
   * This run's ordered event stream, contiguous from sequence zero.
   */
  readonly events: readonly RuntimeEvent[];
  /**
   * Every output this run produced.
   */
  readonly artifacts: readonly Artifact[];
  /**
   * Every piece of evidence this run captured. Cancelling a run never empties it.
   */
  readonly evidence: readonly EvidenceItem[];
  /**
   * Every cleanup this run performed. A terminal run states at least one, because cleanup is
   * observable rather than assumed.
   */
  readonly cleanup_receipts: readonly CleanupReceipt[];
  /**
   * Source-qualified pointers at how this run appears in other domains. Correlation only: none
   * of them is authority over this run's own record.
   */
  readonly correlations: readonly ExternalReference[];
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
   * selectable. Its validity must contain the resolution instant on a half-open `[valid_from,
   * valid_until)` window -- `temporal.valid_from` no later than it, inclusive of equality, and
   * `temporal.valid_until` strictly later than it; a `valid_until` exactly at the resolution
   * instant is refused, not accepted. `temporal.superseded_at` must be absent or strictly
   * after the resolution instant: supersession is exclusive where the closure is inclusive,
   * because an artifact replaced *at* the instant a pack resolved was already not the live
   * one, so equality is rejected here rather than accepted.
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
   * resolution instant on a half-open `[valid_from, valid_until)` window --
   * `provenance.temporal.valid_from` no later than it, inclusive of equality, and
   * `provenance.temporal.valid_until` strictly later than it, so a `valid_until` exactly at
   * the resolution instant is refused; a version whose validity begins only afterwards was not
   * yet in force, and one whose validity ends at or before it was no longer the answer. The
   * version must be current and unsuperseded at that instant: `currentness` exactly `current`,
   * and `provenance.temporal.superseded_at` absent outright, irrespective of timestamp. Not
   * merely absent at or before the resolution instant: a current version records no
   * supersession at all, so a `superseded_at` strictly after the resolution instant is refused
   * exactly as one at or before it is. A version that states when it was replaced belongs to
   * `history`, whichever side of the resolution instant that statement falls on.
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
   * it; the same half-open validity containment of the resolution instant -- `valid_from`
   * inclusive, `valid_until` exclusive; and the same requirement to be current and
   * unsuperseded at that instant, with `provenance.temporal.superseded_at` absent outright,
   * irrespective of timestamp -- a value strictly after the resolution instant is refused
   * exactly as one at or before it is. Only the governance layer differs -- `layer` exactly
   * `l3` -- and a context model is otherwise no more selectable than an L2 record would be
   * under the same temporal facts.
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
    name: "chat.command",
    scope: { required_scopes: ["chat:write"], side_effect: "update", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/chat.schema.json#/$defs/ChatCommandInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/chat.schema.json#/$defs/ChatCommandResult",
    required_capability: { id: "chat.command", minimum_version: "1.0", required: true },
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
      "conflict",
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
    name: "chat.events",
    scope: { required_scopes: ["chat:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/chat.schema.json#/$defs/ChatEventsInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/chat.schema.json#/$defs/ChatEventsResult",
    required_capability: { id: "chat.read", minimum_version: "1.0", required: true },
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
      "size_limit_exceeded",
      "upgrade_required",
      "workspace_migration_required",
      "workspace_not_granted",
    ],
  },
  {
    name: "chat.snapshot",
    scope: { required_scopes: ["chat:read"], side_effect: "none", scope_kind: "workspace" },
    input_schema_ref: "https://contracts.omnivia.dev/application/v1/chat.schema.json#/$defs/ChatSnapshotInput",
    result_schema_ref: "https://contracts.omnivia.dev/application/v1/chat.schema.json#/$defs/ChatSnapshotResult",
    required_capability: { id: "chat.read", minimum_version: "1.0", required: true },
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
