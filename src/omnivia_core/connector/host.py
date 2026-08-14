"""Host-side connector checks: lineage, migration and batch admission (A6-N1).

Standard library only, and storage-free. Everything here is what the
*coordinator* decides before anything becomes durable; none of it opens a
connection, resolves a path or writes a row, which is why it can live beside
the contract rather than inside the runtime.

Three claims are made and no more, in the candidate's own words:

- the cursor check establishes **state-chain integrity** -- a persisted chain
  cannot be rolled back and its states cannot be reordered -- and nothing about
  the remote source follows from it (candidate section 6.4);
- withholding storage handles is an **API ownership boundary**, not a sandbox
  (section 7.2);
- known-material comparison and encoding bounds are **defence in depth**, not
  information-flow enforcement (section 7.5).

In particular, nothing here establishes that a connector reported every
observation in the window its cursor advanced across. `CON-C058` is the
counterexample and the conformance kit detects it from the fake's independently
known expected set, never from these checks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.connector.models import ConnectorContractError
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_CURSOR_FOREIGN,
    ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC,
    ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
    ERROR_CONNECTOR_IDENTITY_UNSTABLE,
    ERROR_CONNECTOR_SCHEDULING_DENIED,
    ERROR_CONNECTOR_SECRET_EXPOSED,
    ERROR_CONNECTOR_STATE_INVALID,
    HOST_SPI_VERSION,
    MAX_METADATA_DEPTH,
    POLL_CONTEXT_FORBIDDEN_MEMBERS,
    Batch,
    ConnectorDescriptor,
    ConnectorRefused,
    CredentialHandle,
    CursorBinding,
    CursorRecord,
    CursorState,
    DeletionSignal,
    IdentityStability,
    ItemFailure,
    Observation,
    PollContext,
    SpiVersion,
)
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
)

# --- the canonical successor-lineage digest ----------------------------------

#: Domain separation for the full-state preimage. Fixed by the contract, not
#: chosen here.
CURSOR_DIGEST_DOMAIN_TAG: Final = b"omnivia.connector.cursor-state.v2"

#: The field order the preimage frames, in the order it frames them. Restated as
#: data so a reordering is a diff on a constant rather than a subtle edit in the
#: middle of a concatenation.
CURSOR_DIGEST_FIELD_ORDER: Final[tuple[str, ...]] = (
    "domain_tag",
    "workspace_id",
    "connector_id",
    "state_version",
    "payload",
    "witness_seq",
    "predecessor_digest",
)


def _frame(value: bytes) -> bytes:
    """Four-byte unsigned big-endian length, then exactly those bytes."""
    return len(value).to_bytes(4, "big") + value


def cursor_digest_preimage(binding: CursorBinding, state: CursorState) -> bytes:
    """The exact seven framed fields the digest covers.

    Exposed rather than inlined because `CON-C059` requires the displayed frames
    to be independently recomputable: a test that could only compare the final
    digest would not be able to say *which* frame it disagreed about.
    """
    predecessor = b"" if state.predecessor_digest is None else state.predecessor_digest
    return b"".join(
        (
            _frame(CURSOR_DIGEST_DOMAIN_TAG),
            _frame(binding.workspace_id.encode("ascii")),
            _frame(binding.connector_id.encode("ascii")),
            _frame(str(state.state_version).encode("ascii")),
            _frame(state.payload),
            _frame(str(state.witness_seq).encode("ascii")),
            _frame(predecessor),
        )
    )


def canonical_cursor_digest(binding: CursorBinding, state: CursorState) -> bytes:
    """SHA-256 over the complete parent state and its frozen bindings.

    Host-computed and host-computed only. A connector declares which complete
    state it follows; it never authors its own lineage, so cursor binding falls
    out of this same check rather than needing a separate trusted field.
    """
    return hashlib.sha256(cursor_digest_preimage(binding, state)).digest()


# --- registration -------------------------------------------------------------


def validate_registration(
    descriptor: ConnectorDescriptor,
    *,
    granted_capabilities: frozenset[str],
    host_spi_version: SpiVersion = HOST_SPI_VERSION,
) -> None:
    """Fail closed before any poll is scheduled.

    An unknown required *major* is rejected; an unknown additive *minor* is
    tolerated, which mirrors the accepted application wire-compatibility rule so
    that an additive connector change cannot become an accidental production
    break.
    """
    if descriptor.spi_version.major != host_spi_version.major:
        raise ConnectorRefused(
            ERROR_CODE_INCOMPATIBLE_VERSION,
            f"host supports SPI major {host_spi_version.major} only",
        )
    missing = tuple(
        sorted(set(descriptor.required_capabilities) - set(granted_capabilities))
    )
    if missing:
        raise ConnectorRefused(
            ERROR_CODE_CAPABILITY_NOT_GRANTED,
            f"required capabilities not granted: {', '.join(missing)}",
        )
    if descriptor.scheduling_declaration is not None:
        raise ConnectorRefused(
            ERROR_CONNECTOR_SCHEDULING_DENIED,
            "the coordinator is the only execution owner",
        )


# --- credentials ---------------------------------------------------------------


class ScopedCredentialResolver:
    """Resolves one handle for exactly one poll invocation.

    The material is invalidated when the invocation ends, so a connector that
    kept it fails the *next* poll rather than succeeding from cache
    (`CON-C031`). That bounds standing access across polls and says nothing at
    all about material already resolved: once resolved, the secret is an
    ordinary value in the connector's own process.
    """

    def __init__(self, handle: CredentialHandle, material: bytes) -> None:
        if not isinstance(handle, CredentialHandle):
            raise ConnectorContractError("handle must be a CredentialHandle")
        if not isinstance(material, bytes) or not material:
            raise ConnectorContractError("credential material must be non-empty bytes")
        self._handle = handle
        self._material = material
        self._valid = True
        self._resolved: list[bytes] = []

    def __call__(self, handle: CredentialHandle) -> bytes:
        if not self._valid:
            raise ConnectorRefused(
                ERROR_CODE_AUTHORIZATION_DENIED,
                "the handle was invalidated at the end of its poll invocation",
            )
        if handle != self._handle:
            raise ConnectorRefused(
                ERROR_CODE_AUTHORIZATION_DENIED, "unknown credential handle"
            )
        self._resolved.append(self._material)
        return self._material

    def invalidate(self) -> None:
        self._valid = False

    @property
    def valid(self) -> bool:
        return self._valid

    @property
    def resolved_material(self) -> tuple[bytes, ...]:
        """Exactly what this run handed out. The known-material comparison is
        decidable *because* this list exists."""
        return tuple(self._resolved)


def declared_encodings(material: bytes) -> tuple[bytes, ...]:
    """The forms of `material` the host declares it will compare against.

    This list is the whole of what the comparison decides. A connector that
    applies any other transformation -- an encoding never declared, a keyed
    transform, a split across polls, a value *derived* from the secret rather
    than containing it -- is not caught, and `CON-C030` exercises exactly that
    so the gap is recorded rather than implied absent.
    """
    return (
        material,
        base64.urlsafe_b64encode(material).rstrip(b"="),
        base64.b64encode(material),
        binascii.hexlify(material),
        binascii.hexlify(material).upper(),
    )


def contains_known_material(haystack: bytes, resolved: Sequence[bytes]) -> bool:
    """True when `haystack` carries a verbatim or declared-encoding copy."""
    for material in resolved:
        for form in declared_encodings(material):
            if form and form in haystack:
                return True
    return False


# --- hostile input --------------------------------------------------------------


def metadata_depth(document: str) -> int:
    """Maximum nesting depth of a JSON document, by scanning rather than parsing.

    Parsing is what the depth ceiling exists to avoid: a document nested a few
    thousand levels deep exhausts the interpreter stack inside `json.loads`
    before any ceiling written in terms of the parsed value could be consulted.
    Scanning bracket tokens outside string literals decides the same question in
    constant stack.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
            deepest = max(deepest, depth)
        elif character in "}]":
            depth -= 1
    return deepest


def _locator_defect(locator: str) -> str | None:
    """Why a locator is refused, or `None`.

    A locator is stored as opaque text and is never resolved as a filesystem
    path -- nothing here calls `Path`. A parent-directory-shaped segment is
    still refused at the boundary, because a locator that reads as a traversal
    is a fact about the source worth refusing before it is durable rather than
    a shape to be normalised afterwards.
    """
    if any(segment == ".." for segment in locator.replace("\\", "/").split("/")):
        return "locator carries a parent-directory segment"
    return None


def admit_observation(document: Mapping[str, Any]) -> Observation:
    """Turn one corpus-shaped observation document into a validated value.

    Every rejection is `invalid_request` and names the field without echoing the
    value: a hostile identity copied into an exception is exactly how it reaches
    a log.
    """
    # Unknown keys are read past rather than rejected: an additive optional
    # minor field is tolerated in production posture (`CON-C045`), and strict
    # unknown-field rejection belongs to a version-pinned conformance run.
    signal_text = document.get("deletion_signal", "none")
    signal: DeletionSignal | None = None
    for member in DeletionSignal:
        if member.value == signal_text:
            signal = member
    if signal is None:
        raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "deletion_signal")

    labels_value = document.get("permission_labels") or ()
    labels = tuple(labels_value) if isinstance(labels_value, (list, tuple)) else None
    if labels is None or not all(isinstance(label, str) for label in labels):
        raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "permission_labels")

    metadata_json = document.get("metadata_json", "")
    if not isinstance(metadata_json, str):
        raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "metadata_json")
    if metadata_depth(metadata_json) > MAX_METADATA_DEPTH:
        raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "metadata_json")

    locator = document.get("source_locator")
    if not isinstance(locator, str) or _locator_defect(locator) is not None:
        raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "source_locator")

    contract_error = False
    observation: Observation | None = None
    try:
        observation = Observation(
            source_native_id=str(document.get("source_native_id", "")),
            source_locator=locator,
            observed_at_us=int(document.get("observed_at_us", 0)),
            metadata_bytes=int(document.get("metadata_bytes", 0)),
            content_checksum=document.get("content_checksum"),
            media_type=document.get("media_type"),
            source_version=document.get("source_version"),
            permission_labels=tuple(sorted(labels)),
            deletion_signal=signal,
            metadata_json=metadata_json,
        )
    except (ConnectorContractError, TypeError, ValueError):
        contract_error = True
    if contract_error or observation is None:
        raise ConnectorRefused(
            ERROR_CODE_INVALID_REQUEST, "observation is outside the accepted domain"
        )
    return observation


# --- cursor checks ---------------------------------------------------------------


def validate_successor(
    record: CursorRecord,
    successor: CursorState,
    *,
    current_binding: CursorBinding,
) -> bytes:
    """Check a successor against the exact state the host presented.

    Returns the recomputed parent digest, so a caller that has to record the
    lineage it verified does not recompute it under a second construction.

    The order is the corpus's order and the identifiers are distinct on purpose:
    a payload defect is refused as an encoding failure by `CursorState` itself,
    before any value reaches this function; a binding mismatch is
    `connector_cursor_foreign`; a lineage mismatch is `connector_state_invalid`;
    and a witness regression is `connector_cursor_not_monotonic`. A refusal that
    named the wrong one would misdiagnose the defect.
    """
    if record.binding != current_binding:
        raise ConnectorRefused(
            ERROR_CONNECTOR_CURSOR_FOREIGN,
            "the persisted cursor was frozen under a different workspace or connector",
        )
    expected = canonical_cursor_digest(record.binding, record.state)
    if successor.predecessor_digest != expected:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID,
            "the successor does not name the exact state the host presented",
        )
    if successor.witness_seq < record.state.witness_seq:
        raise ConnectorRefused(
            ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC,
            f"witness regressed from {record.state.witness_seq} "
            f"to {successor.witness_seq}",
        )
    return expected


# --- migration ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MigrationAudit:
    """The pre- and post-migration digest pair recorded with the checkpoint.

    Without both, the chain shows an unexplained break across a version change:
    the successor of a migrated state names a digest no persisted parent
    produces, and nothing says why.
    """

    before: CursorState
    after: CursorState
    predecessor_digest_before: bytes
    predecessor_digest_after: bytes


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """Accepted, refused as an invalid state, or an explicit resynchronization."""

    outcome: str
    error: str | None = None
    audit: MigrationAudit | None = None
    detail: str = ""


def validate_migration(
    record: CursorRecord,
    migrated: CursorState,
    *,
    supported_state_versions: Sequence[int],
) -> MigrationAudit:
    """Enforce the four migration obligations over whole states.

    Payload encoding and the byte bound are already discharged by `CursorState`,
    which cannot hold a value that breaks them.
    """
    before = record.state
    if migrated.state_version <= before.state_version:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID,
            "a migrated state version must strictly increase",
        )
    if migrated.state_version not in set(supported_state_versions):
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID,
            "a migrated state version must land in the supported set",
        )
    if migrated.witness_seq != before.witness_seq:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID,
            "migration re-encodes a known position; the witness must be unchanged",
        )
    if migrated.predecessor_digest != before.predecessor_digest:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID,
            "migration never re-parents the chain; the predecessor must be identical",
        )
    return MigrationAudit(
        before=before,
        after=migrated,
        predecessor_digest_before=canonical_cursor_digest(record.binding, before),
        predecessor_digest_after=canonical_cursor_digest(record.binding, migrated),
    )


def run_cursor_migration(
    connector: Any,
    record: CursorRecord,
    *,
    supported_state_versions: Sequence[int],
) -> MigrationOutcome:
    """Drive `migrate_cursor` and classify the result.

    Called twice, because "byte-identical output on repetition" is an obligation
    and an obligation nothing exercises is a sentence. Purity -- no source
    access and no credential resolution -- is observed by the caller, which owns
    the resolver and can see whether it was used.
    """
    refusal: ConnectorRefused | None = None
    first: CursorState | None = None
    second: CursorState | None = None
    try:
        first = connector.migrate_cursor(record.state)
        second = connector.migrate_cursor(record.state)
    except ConnectorRefused as error:
        refusal = error

    if refusal is not None:
        if refusal.error == ERROR_CONNECTOR_CURSOR_UNMIGRATABLE:
            return MigrationOutcome(
                outcome="resync_required",
                error=ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
                detail=refusal.detail,
            )
        return MigrationOutcome(
            outcome="refused", error=refusal.error, detail=refusal.detail
        )

    if not isinstance(first, CursorState) or first != second:
        return MigrationOutcome(
            outcome="refused",
            error=ERROR_CONNECTOR_STATE_INVALID,
            detail="migration is not deterministic",
        )

    audit_error: ConnectorRefused | None = None
    audit: MigrationAudit | None = None
    try:
        audit = validate_migration(
            record, first, supported_state_versions=supported_state_versions
        )
    except ConnectorRefused as error:
        audit_error = error
    if audit_error is not None:
        return MigrationOutcome(
            outcome="refused", error=audit_error.error, detail=audit_error.detail
        )
    return MigrationOutcome(outcome="accepted", audit=audit)


# --- batch admission ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchVerdict:
    """What the coordinator would commit, decided before anything is persisted."""

    outcome: str
    observations: tuple[Observation, ...]
    item_failures: tuple[ItemFailure, ...]
    successor: CursorRecord
    parent_digest: bytes
    run_bytes_after: int


def validate_batch(
    batch: Batch,
    ctx: PollContext,
    record: CursorRecord,
    *,
    descriptor: ConnectorDescriptor,
    now_us: int,
    run_bytes_spent: int = 0,
    resolved_material: Sequence[bytes] = (),
    accepted_permission_labels: frozenset[str] | None = None,
    reconciliation_authorized: bool = False,
) -> BatchVerdict:
    """Admit or refuse one batch, entirely in memory.

    This is an API-ownership and input-validation boundary. It is not a sandbox,
    it enforces no information flow, and nothing it accepts is evidence that the
    connector reported the whole window.
    """
    binding = CursorBinding(
        workspace_id=ctx.workspace_id, connector_id=descriptor.connector_id
    )

    if ctx.deadline.expired(now_us):
        raise ConnectorRefused(
            ERROR_CODE_DEADLINE_EXCEEDED, "the poll deadline expired before commit"
        )

    # Counted here rather than read off a declaration: a connector that reports
    # its own size is reporting the number it wants believed.
    if len(batch.observations) > ctx.limits.max_batch_items:
        raise ConnectorRefused(
            ERROR_CODE_SIZE_LIMIT_EXCEEDED,
            f"batch declares {len(batch.observations)} items over the "
            f"{ctx.limits.max_batch_items} ceiling",
        )

    for observation in batch.observations:
        if _locator_defect(observation.source_locator) is not None:
            raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "source_locator")
        if metadata_depth(observation.metadata_json) > MAX_METADATA_DEPTH:
            raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "metadata_json")
        if accepted_permission_labels is not None:
            unmapped = set(observation.permission_labels) - accepted_permission_labels
            if unmapped:
                raise ConnectorRefused(
                    ERROR_CODE_INVALID_REQUEST,
                    "a source grant maps to no accepted permission label",
                )

    # Duplicate collapse is the coordinator's, not the connector's: an identity
    # reported twice with the same content is one upsert however many times it
    # arrives.
    admitted: list[Observation] = []
    failures: list[ItemFailure] = list(batch.item_failures)
    seen: set[tuple[str, str | None]] = set()
    for observation in batch.observations:
        if observation.metadata_bytes > ctx.limits.max_item_metadata_bytes:
            failures.append(
                ItemFailure(
                    source_native_id=observation.source_native_id,
                    error=ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                    detail="item metadata exceeds the declared ceiling",
                )
            )
            continue
        key = (observation.source_native_id, observation.content_checksum)
        if key in seen:
            continue
        seen.add(key)
        # Absence is never deletion (`CON-D03`). An absent-from-window signal is
        # a reconciliation input at most, and only under an explicit
        # authorization that declares complete scope coverage.
        if (
            observation.deletion_signal is DeletionSignal.ABSENT_FROM_WINDOW
            and not reconciliation_authorized
        ):
            continue
        admitted.append(observation)

    run_bytes_after = run_bytes_spent + sum(item.metadata_bytes for item in admitted)
    if run_bytes_after > ctx.limits.max_run_bytes:
        raise ConnectorRefused(
            ERROR_CODE_SIZE_LIMIT_EXCEEDED, "the run byte ceiling would be exceeded"
        )

    # Known-material comparison, over every connector-authored byte that becomes
    # durable. Verbatim and declared-encoding copies only; see
    # `declared_encodings`.
    authored = [batch.successor_cursor.payload]
    authored.extend(item.source_locator.encode("utf-8") for item in batch.observations)
    authored.extend(item.detail.encode("utf-8") for item in failures)
    for candidate in authored:
        if contains_known_material(candidate, resolved_material):
            raise ConnectorRefused(
                ERROR_CONNECTOR_SECRET_EXPOSED,
                "connector-authored bytes carry material this run resolved",
            )

    parent_digest = validate_successor(
        record, batch.successor_cursor, current_binding=binding
    )
    successor = CursorRecord(binding=binding, state=batch.successor_cursor)

    if descriptor.identity_stability is IdentityStability.LOCATOR_DERIVED and admitted:
        # A rename and a delete-plus-create are observationally identical here.
        # Guessing would either fabricate continuity the source does not
        # guarantee or destroy continuity it does, so the pair is queued.
        return BatchVerdict(
            outcome="deferred_to_reconciliation",
            observations=tuple(admitted),
            item_failures=tuple(failures),
            successor=successor,
            parent_digest=parent_digest,
            run_bytes_after=run_bytes_after,
        )

    # "No change" means the poll found nothing *and* stood still. An empty batch
    # that advanced the witness is an acceptance, not a no-op: the cursor moved
    # past a window, which is exactly the `CON-C058` shape and must not be
    # reported as though nothing happened.
    if (
        not admitted
        and not failures
        and batch.successor_cursor.witness_seq == record.state.witness_seq
    ):
        return BatchVerdict(
            outcome="no_change",
            observations=(),
            item_failures=(),
            successor=successor,
            parent_digest=parent_digest,
            run_bytes_after=run_bytes_after,
        )

    return BatchVerdict(
        outcome="dead_lettered" if failures and not admitted else "accepted",
        observations=tuple(admitted),
        item_failures=tuple(failures),
        successor=successor,
        parent_digest=parent_digest,
        run_bytes_after=run_bytes_after,
    )


# --- the storage boundary, as a surface assertion --------------------------------


def poll_context_surface_defects() -> tuple[str, ...]:
    """Members of `PollContext` that would breach the API ownership boundary.

    Empty means the context hands a connector no connection, no blob store
    handle, no workspace path, no scheduling and no write-back. It does *not*
    mean connector code cannot open files, databases or sockets through platform
    APIs; any stronger claim is gated on `CON-P09`.
    """
    defects: list[str] = []
    for name in dir(PollContext):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        if lowered in POLL_CONTEXT_FORBIDDEN_MEMBERS:
            defects.append(name)
    for name in PollContext.__dataclass_fields__:
        lowered = name.lower()
        if lowered in POLL_CONTEXT_FORBIDDEN_MEMBERS or lowered.endswith(
            ("_path", "_connection", "_conn", "_dsn", "_url")
        ):
            defects.append(name)
    return tuple(sorted(set(defects)))


def spi_writeback_defects(connector: object) -> tuple[str, ...]:
    """Operations on a connector that would mutate a source. Empty by contract."""
    from omnivia_core.connector.spi import SPI_FORBIDDEN_OPERATIONS, spi_surface

    return tuple(
        name for name in spi_surface(connector) if name in SPI_FORBIDDEN_OPERATIONS
    )


def identity_posture_error(descriptor: ConnectorDescriptor) -> str | None:
    """The identifier a locator-derived rename is deferred under, or `None`."""
    if descriptor.identity_stability is IdentityStability.LOCATOR_DERIVED:
        return ERROR_CONNECTOR_IDENTITY_UNSTABLE
    return None


__all__ = [
    "CURSOR_DIGEST_DOMAIN_TAG",
    "CURSOR_DIGEST_FIELD_ORDER",
    "BatchVerdict",
    "MigrationAudit",
    "MigrationOutcome",
    "ScopedCredentialResolver",
    "admit_observation",
    "canonical_cursor_digest",
    "contains_known_material",
    "cursor_digest_preimage",
    "declared_encodings",
    "identity_posture_error",
    "metadata_depth",
    "poll_context_surface_defects",
    "run_cursor_migration",
    "spi_writeback_defects",
    "validate_batch",
    "validate_migration",
    "validate_registration",
    "validate_successor",
]
