"""The managed Skills operations: authoring, publication, installation, removal and resolution.

Seven mutations and one read, over :mod:`storage.managed_skills`. The mutations each run through
`execute_mutation`, so their grant, idempotency, audit and rollback are the ones every mutation
has. The write happens in the same fenced transaction as its audit event, so a refusal leaves
nothing behind, and a replay of a committed call is answered from its stored result.

What the handlers own, and what they do not:

* A draft is authored, revised and submitted; a submitted draft is closed. Each revision is
  checked against the revision the caller last read, so a stale update is a `conflict`, never a
  silent overwrite.
* Publication mints an immutable version from a proposal. Installation and removal are events on
  one version, recorded as history. Deprecation marks a version and deletes nothing.
* Resolution is a read. It applies the registry's precedence over what is installed and not
  deprecated, and binds nothing.
* Manifests are inert data. A manifest is validated closed, from the raw request rather than the
  decoded wire, because the wire decoder ignores unknown members and a permission stated in a
  manifest must be refused, not dropped.

Roles are not checked here. The grant issued for each operation carries the role its
`MUTATION_ROLES` entry names, so authorship, publication and installation are separate authorities
decided before any handler runs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeGuard, TypeVar

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ContractDecodeError,
    ContractSemanticError,
    SkillDraftCreateInput,
    SkillDraftCreateResult,
    SkillDraftUpdateInput,
    SkillDraftUpdateResult,
    SkillInstallInput,
    SkillInstallResult,
    SkillProposalSubmitInput,
    SkillProposalSubmitResult,
    SkillRemoveInput,
    SkillRemoveResult,
    SkillResolvedEntry,
    SkillResolveInput,
    SkillResolveResult,
    SkillVersionDeprecateInput,
    SkillVersionDeprecateResult,
    SkillVersionPublishInput,
    SkillVersionPublishResult,
    idempotency_equivalence,
    is_content_checksum,
    is_identifier,
    is_open_code,
)
from omnivia_core.contracts.v1.semantics_skills import (
    MAX_EVIDENCE_REFS,
    MAX_RUN_ROLES,
    MAX_SELECTIONS_PER_ROLE,
    SkillManifestError,
    is_skill_manifest_id,
    validate_skill_manifest,
)
from omnivia_core_runtime.ownership.fencing import MutationGuard, read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationGrant,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
    application_refusal,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.managed_skills import (
    CODE_NOT_FOUND,
    RoleClosure,
    SkillDraftHead,
    SkillResolutionError,
    SkillVersion,
    installed_state,
    read_draft_head,
    read_proposal,
    read_skill_version,
    resolve_role_selection,
    transaction_local_skills_writer,
)

SKILL_DRAFT_CREATE_OPERATION: Final = "skills.draft.create"
SKILL_DRAFT_UPDATE_OPERATION: Final = "skills.draft.update"
SKILL_PROPOSAL_SUBMIT_OPERATION: Final = "skills.proposal.submit"
SKILL_VERSION_PUBLISH_OPERATION: Final = "skills.version.publish"
SKILL_VERSION_DEPRECATE_OPERATION: Final = "skills.version.deprecate"
SKILL_INSTALL_OPERATION: Final = "skills.install"
SKILL_REMOVE_OPERATION: Final = "skills.remove"
SKILL_RESOLVE_OPERATION: Final = "skills.resolve"
SKILL_MUTATION_OPERATIONS: Final = frozenset(
    {
        SKILL_DRAFT_CREATE_OPERATION,
        SKILL_DRAFT_UPDATE_OPERATION,
        SKILL_PROPOSAL_SUBMIT_OPERATION,
        SKILL_VERSION_PUBLISH_OPERATION,
        SKILL_VERSION_DEPRECATE_OPERATION,
        SKILL_INSTALL_OPERATION,
        SKILL_REMOVE_OPERATION,
    }
)

_MESSAGE_NO_STORAGE: Final = "the skill registry is not reachable from this service instance"
_MESSAGE_NOT_FOUND: Final = "no skill of this workspace has that identifier"
_MESSAGE_DRAFT_CLOSED: Final = "this draft was submitted and is closed to revision"
_MESSAGE_STALE: Final = "the draft is at a different revision from the one this request read"
_MESSAGE_NOT_INSTALLED: Final = "that skill version is not installed in this workspace"

_ID_FIELDS: Final = frozenset({"evidence_id", "content_digest"})
_SELECTION_FIELDS: Final = frozenset({"skill_name", "manifest_id"})
_ROLE_FIELDS: Final = frozenset({"role_id", "selections"})

#: The lowest revision a draft stands at, and so the lowest a caller can have read.
_FIRST_REVISION: Final = 1

_T = TypeVar("_T")


def _is_array(raw: object) -> TypeGuard[Sequence[Any]]:
    """A JSON array as a transport delivers it: a list or a read-only sequence, never a string."""
    return isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))


def _array(raw: object, *, label: str, minimum: int, maximum: int) -> Sequence[Any]:
    """A JSON array of `minimum` to `maximum` entries, or the refusal that says why not."""
    if not _is_array(raw) or not minimum <= len(raw) <= maximum:
        raise _refuse(
            ERROR_CODE_INVALID_REQUEST, f"{label} must hold {minimum} to {maximum} entries"
        )
    return raw


def _refuse(code: str, message: str) -> OperationError:
    return application_refusal(code, message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _refuse(ERROR_CODE_INVALID_REQUEST, message)


def _timestamp(microseconds: int) -> str:
    """One microsecond instant, spelled as the wire's UTC `Timestamp`."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    moment = epoch + timedelta(microseconds=microseconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}Z"


def _row_id(prefix: str, settlement: MutationSettlementContext) -> str:
    """A row identity taken from this mutation's own claim, so no two settlements share one."""
    return f"{prefix}-{settlement.claim_id}"


def _resolution_refusal(error: SkillResolutionError) -> OperationError:
    """A selection the registry cannot satisfy: absent is `not_found`, anything else a `conflict`."""
    if error.code == CODE_NOT_FOUND:
        return _refuse(ERROR_CODE_NOT_FOUND, str(error))
    return _refuse(ERROR_CODE_CONFLICT, str(error))


def _store(action: Callable[[], _T]) -> _T:
    """Run one registry write, refusing a state the registry will not hold as a `conflict`.

    A write the store refuses is a refusal of the state the caller asked for -- a stale revision,
    a version already published, a deprecated install -- not a fault in this service, so it is a
    `conflict` a caller can act on. A selection that cannot resolve keeps its own refusal.
    """
    try:
        return action()
    except SkillResolutionError as error:
        raise _resolution_refusal(error) from error
    except (StorageError, sqlite3.IntegrityError) as error:
        raise _refuse(ERROR_CODE_CONFLICT, str(error)) from error


def _identifier(value: object, label: str) -> str:
    _require(isinstance(value, str) and is_identifier(value), f"{label} is malformed")
    assert isinstance(value, str)
    return value


def _revision(value: object) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= _FIRST_REVISION,
        "expected_revision is a draft revision, numbered from 1",
    )
    assert isinstance(value, int)
    return value


def _manifest(raw: object) -> dict[str, Any]:
    """A manifest validated closed, as inert data, from the raw request rather than its wire form."""
    try:
        return validate_skill_manifest(raw)
    except SkillManifestError as error:
        raise _refuse(ERROR_CODE_INVALID_REQUEST, str(error)) from error


def _evidence(raw: object, *, label: str, minimum: int) -> list[dict[str, str]]:
    """Evidence references, closed and unique, in the order given. Recorded, never dereferenced."""
    refs: list[dict[str, str]] = []
    for item in _array(raw, label=label, minimum=minimum, maximum=MAX_EVIDENCE_REFS):
        _require(
            isinstance(item, Mapping)
            and set(item) == _ID_FIELDS
            and is_identifier(item["evidence_id"])
            and is_content_checksum(item["content_digest"]),
            f"{label} has a malformed reference",
        )
        refs.append(
            {
                "evidence_id": item["evidence_id"],
                "content_digest": item["content_digest"],
            }
        )
    _require(
        len({ref["evidence_id"] for ref in refs}) == len(refs),
        f"{label} repeats a reference",
    )
    return refs


def _selections(raw: object) -> tuple[tuple[str, str | None], ...]:
    """One role's `(skill_name, manifest_id | None)` requests, in the caller's order."""
    requests: list[tuple[str, str | None]] = []
    for item in _array(raw, label="selections", minimum=1, maximum=MAX_SELECTIONS_PER_ROLE):
        _require(
            isinstance(item, Mapping)
            and set(item) <= _SELECTION_FIELDS
            and "skill_name" in item
            and is_identifier(item["skill_name"]),
            "a skill selection names a skill",
        )
        manifest_id = item.get("manifest_id")
        _require(
            manifest_id is None or is_skill_manifest_id(manifest_id),
            "a skill selection's manifest_id is malformed",
        )
        requests.append((item["skill_name"], manifest_id))
    _require(
        len({name for name, _manifest_id in requests}) == len(requests),
        "a skill is selected once per role",
    )
    return tuple(requests)


def role_selections(raw: object) -> tuple[tuple[str, tuple[tuple[str, str | None], ...]], ...]:
    """Role selections as the Run and `skills.resolve` name them, strictly, in the caller's order.

    Closed at every level: a member no selection declares is refused, never ignored, so a field
    that would widen what a role may use cannot pass unnoticed.
    """
    roles: list[tuple[str, tuple[tuple[str, str | None], ...]]] = []
    for item in _array(raw, label="skill_selections", minimum=1, maximum=MAX_RUN_ROLES):
        _require(
            isinstance(item, Mapping) and set(item) == _ROLE_FIELDS,
            "a role selection names a role and its selections",
        )
        role_id = _identifier(item["role_id"], "role_id")
        roles.append((role_id, _selections(item["selections"])))
    _require(
        len({role_id for role_id, _requests in roles}) == len(roles),
        "a role is bound once per Run",
    )
    return tuple(roles)


def skill_selections_input(raw: object) -> object:
    """The `skill_selections` a `workflow.start` request names, as sent, or `None` when it names none."""
    if isinstance(raw, Mapping):
        return raw.get("skill_selections")
    return None


def resolve_run_skills(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    raw: object,
) -> tuple[RoleClosure, ...]:
    """The closures a Run's `skill_selections` resolve to, or the refusal that names why not.

    Called inside `workflow.start`'s fenced transaction, so the resolution is read from the same
    state the Run is admitted into. An absent selection binds no skill.
    """
    if raw is None:
        return ()
    closures: list[RoleClosure] = []
    for role_id, requests in role_selections(raw):
        try:
            closures.append(
                resolve_role_selection(
                    connection,
                    workspace_id=workspace_id,
                    role_id=role_id,
                    requests=requests,
                )
            )
        except SkillResolutionError as error:
            raise _resolution_refusal(error) from error
        except StorageError as error:
            raise _refuse(ERROR_CODE_INVALID_REQUEST, str(error)) from error
    return tuple(closures)


def _servable(decode: Callable[[object], object]) -> Callable[[Mapping[str, Any]], bool]:
    """Whether a result, fresh or replayed, still decodes as the contract's result type."""

    def valid(wire: Mapping[str, Any]) -> bool:
        try:
            decode(wire)
        except (ContractDecodeError, ContractSemanticError):
            return False
        return True

    return valid


_VALID_DRAFT_CREATE = _servable(SkillDraftCreateResult.from_wire)
_VALID_DRAFT_UPDATE = _servable(SkillDraftUpdateResult.from_wire)
_VALID_PROPOSAL = _servable(SkillProposalSubmitResult.from_wire)
_VALID_PUBLISH = _servable(SkillVersionPublishResult.from_wire)
_VALID_DEPRECATE = _servable(SkillVersionDeprecateResult.from_wire)
_VALID_INSTALL = _servable(SkillInstallResult.from_wire)
_VALID_REMOVE = _servable(SkillRemoveResult.from_wire)


@dataclass
class SkillHandlers:
    """The eight skill operations, over one workspace's registry.

    Every mutation names one workspace, one principal and one grant. Nothing here takes a role,
    a purpose or a fencing generation from a request: the grant supplies all three.
    """

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Callable[[str], str]

    # -- the storage authority this instance is serving --

    def _authority(self) -> tuple[Any, Any, MutationGuard]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise _refuse(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection, identity, guard

    def _input(
        self,
        context: OperationContext,
        allowed: frozenset[str],
        decode: Callable[[object], Any],
    ) -> Any:
        """Decode the request, refusing a key the operation does not declare rather than dropping it."""
        raw = context.request.input
        _require(
            isinstance(raw, Mapping) and set(raw) <= allowed,
            "the request names a key this operation does not declare",
        )
        try:
            return decode(raw)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise _refuse(ERROR_CODE_INVALID_REQUEST, str(error)) from error

    def _grant(
        self, context: OperationContext, payload: Mapping[str, Any]
    ) -> tuple[MutationGrant, Any]:
        _connection, _identity, guard = self._authority()
        if context.authorization is None:
            raise _refuse(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            payload,
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        return grant, equivalence

    def _mutate(
        self,
        context: OperationContext,
        payload: Mapping[str, Any],
        mutate: Callable[[Any, MutationSettlementContext], Mapping[str, Any]],
        valid: Callable[[Mapping[str, Any]], bool],
    ) -> AuditedOperationResult:
        """Issue the grant, run one domain write under it, and return its settled answer."""
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, payload)
        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=valid,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    # -- reads a write depends on, each refusing a name this workspace does not hold --

    def _draft(self, connection: Any, workspace_id: str, draft_id: str) -> SkillDraftHead:
        head = read_draft_head(connection, workspace_id=workspace_id, draft_id=draft_id)
        if head is None:
            raise _refuse(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        return head

    def _version(self, connection: Any, workspace_id: str, manifest_id: str) -> SkillVersion:
        version = read_skill_version(
            connection, workspace_id=workspace_id, manifest_id=manifest_id
        )
        if version is None:
            raise _refuse(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        return version

    # -- skills.draft.create ---------------------------------------------------------

    def skills_draft_create(self, context: OperationContext) -> AuditedOperationResult:
        """Open one draft at revision 1. It publishes nothing and installs nothing."""
        request = self._input(
            context,
            frozenset({"manifest", "source_work_ref"}),
            SkillDraftCreateInput.from_wire,
        )
        raw_manifest = context.request.input["manifest"]
        manifest = _manifest(raw_manifest)
        source_work_ref = (
            None
            if request.source_work_ref is None
            else _identifier(request.source_work_ref, "source_work_ref")
        )

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
            head = _store(
                lambda: writer.create_draft(
                    draft_id=_row_id("skdraft", settlement),
                    draft_revision_id=_row_id("skrev", settlement),
                    manifest=manifest,
                    source_work_ref=source_work_ref,
                    actor=context.principal,
                    at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            )
            return SkillDraftCreateResult(
                draft_id=head.draft.draft_id,
                skill_name=head.draft.skill_name,
                draft_revision=head.latest.draft_revision,
                version=head.latest.version,
                manifest_id=head.latest.manifest_id,
                created_at=_timestamp(head.draft.created_at_us),
            ).to_wire()

        return self._mutate(
            context, request.to_wire(), mutate, _VALID_DRAFT_CREATE
        )

    # -- skills.draft.update ---------------------------------------------------------

    def skills_draft_update(self, context: OperationContext) -> AuditedOperationResult:
        """Append one revision, only on top of the revision the caller last read."""
        request = self._input(
            context,
            frozenset({"draft_id", "expected_revision", "manifest"}),
            SkillDraftUpdateInput.from_wire,
        )
        draft_id = _identifier(request.draft_id, "draft_id")
        expected = _revision(request.expected_revision)
        raw_manifest = context.request.input["manifest"]
        manifest = _manifest(raw_manifest)

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            head = self._draft(fenced, context.workspace_id, draft_id)
            if head.proposal is not None:
                raise _refuse(ERROR_CODE_CONFLICT, _MESSAGE_DRAFT_CLOSED)
            if head.latest.draft_revision != expected:
                raise _refuse(ERROR_CODE_CONFLICT, _MESSAGE_STALE)
            _require(
                manifest["skill_name"] == head.draft.skill_name,
                "a draft keeps the skill name it was created with",
            )
            writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
            revision = _store(
                lambda: writer.revise_draft(
                    draft_revision_id=_row_id("skrev", settlement),
                    draft_id=draft_id,
                    expected_revision=expected,
                    manifest=manifest,
                    actor=context.principal,
                    at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            )
            return SkillDraftUpdateResult(
                draft_id=draft_id,
                draft_revision=revision.draft_revision,
                version=revision.version,
                manifest_id=revision.manifest_id,
                updated_at=_timestamp(revision.created_at_us),
            ).to_wire()

        return self._mutate(context, request.to_wire(), mutate, _VALID_DRAFT_UPDATE)

    # -- skills.proposal.submit ------------------------------------------------------

    def skills_proposal_submit(self, context: OperationContext) -> AuditedOperationResult:
        """Send a draft's latest revision to the publisher queue, once."""
        request = self._input(
            context,
            frozenset({"draft_id", "expected_revision", "evidence_refs"}),
            SkillProposalSubmitInput.from_wire,
        )
        draft_id = _identifier(request.draft_id, "draft_id")
        expected = _revision(request.expected_revision)
        evidence = _evidence(
            context.request.input["evidence_refs"],
            label="evidence_refs",
            minimum=0,
        )

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            head = self._draft(fenced, context.workspace_id, draft_id)
            if head.proposal is not None:
                raise _refuse(ERROR_CODE_CONFLICT, "this draft was already submitted")
            if head.latest.draft_revision != expected:
                raise _refuse(ERROR_CODE_CONFLICT, _MESSAGE_STALE)
            writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
            proposal = _store(
                lambda: writer.submit_proposal(
                    proposal_id=_row_id("skprop", settlement),
                    draft_id=draft_id,
                    expected_revision=expected,
                    evidence=evidence,
                    actor=context.principal,
                    at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            )
            return SkillProposalSubmitResult(
                proposal_id=proposal.proposal_id,
                draft_id=draft_id,
                draft_revision=proposal.draft_revision,
                submitted_at=_timestamp(proposal.submitted_at_us),
            ).to_wire()

        return self._mutate(context, request.to_wire(), mutate, _VALID_PROPOSAL)

    # -- skills.version.publish ------------------------------------------------------

    def skills_version_publish(self, context: OperationContext) -> AuditedOperationResult:
        """Mint the immutable version a proposal submitted. Needs the publisher role."""
        request = self._input(
            context,
            frozenset({"proposal_id", "review_evidence_refs"}),
            SkillVersionPublishInput.from_wire,
        )
        proposal_id = _identifier(request.proposal_id, "proposal_id")
        review = _evidence(
            context.request.input["review_evidence_refs"],
            label="review_evidence_refs",
            minimum=1,
        )

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            if read_proposal(fenced, workspace_id=context.workspace_id, proposal_id=proposal_id) is None:
                raise _refuse(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
            writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
            version = _store(
                lambda: writer.publish_version(
                    proposal_id=proposal_id,
                    review_evidence=review,
                    actor=context.principal,
                    at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            )
            return SkillVersionPublishResult(
                manifest_id=version.manifest_id,
                skill_name=version.skill_name,
                version=version.version,
                proposal_id=version.proposal_id,
                draft_id=version.draft_id,
                draft_revision=version.draft_revision,
                published_at=_timestamp(version.published_at_us),
            ).to_wire()

        return self._mutate(context, request.to_wire(), mutate, _VALID_PUBLISH)

    # -- skills.version.deprecate ----------------------------------------------------

    def skills_version_deprecate(self, context: OperationContext) -> AuditedOperationResult:
        """Mark one published version deprecated, once. The version itself is never deleted."""
        request = self._input(
            context,
            frozenset({"manifest_id", "reason"}),
            SkillVersionDeprecateInput.from_wire,
        )
        manifest_id = request.manifest_id
        _require(is_skill_manifest_id(manifest_id), "manifest_id is malformed")
        _require(is_open_code(request.reason), "reason is malformed")

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            self._version(fenced, context.workspace_id, manifest_id)
            writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
            deprecation = _store(
                lambda: writer.deprecate_version(
                    deprecation_id=_row_id("skdep", settlement),
                    manifest_id=manifest_id,
                    reason=request.reason,
                    actor=context.principal,
                    at_us=settlement.settled_at_us,
                    audit_ref=settlement.audit_ref,
                )
            )
            return SkillVersionDeprecateResult(
                manifest_id=manifest_id,
                reason=deprecation.reason,
                deprecated_at=_timestamp(deprecation.deprecated_at_us),
            ).to_wire()

        return self._mutate(context, request.to_wire(), mutate, _VALID_DEPRECATE)

    # -- skills.install and skills.remove --------------------------------------------

    def skills_install(self, context: OperationContext) -> AuditedOperationResult:
        """Bind one published version into this workspace's usable set. Installing twice records once."""
        request = self._input(context, frozenset({"manifest_id"}), SkillInstallInput.from_wire)
        manifest_id = request.manifest_id
        _require(is_skill_manifest_id(manifest_id), "manifest_id is malformed")

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            version = self._version(fenced, context.workspace_id, manifest_id)
            held = installed_state(fenced, workspace_id=context.workspace_id, manifest_id=manifest_id)
            if held is not None and held.event_kind == "install":
                # Already installed: the current state answers, and nothing new is recorded.
                state = held
            else:
                writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
                state = _store(
                    lambda: writer.record_install_event(
                        install_event_id=_row_id("skinst", settlement),
                        manifest_id=manifest_id,
                        event_kind="install",
                        actor=context.principal,
                        at_us=settlement.settled_at_us,
                        audit_ref=settlement.audit_ref,
                    )
                )
            return SkillInstallResult(
                manifest_id=manifest_id,
                skill_name=version.skill_name,
                version=version.version,
                install_state="installed",
                event_sequence=state.event_sequence,
                recorded_at=_timestamp(state.occurred_at_us),
            ).to_wire()

        return self._mutate(context, request.to_wire(), mutate, _VALID_INSTALL)

    def skills_remove(self, context: OperationContext) -> AuditedOperationResult:
        """Unbind one installed version. Removal prevents new selection and deletes nothing."""
        request = self._input(context, frozenset({"manifest_id"}), SkillRemoveInput.from_wire)
        manifest_id = request.manifest_id
        _require(is_skill_manifest_id(manifest_id), "manifest_id is malformed")

        def mutate(fenced: Any, settlement: MutationSettlementContext) -> Mapping[str, Any]:
            version = self._version(fenced, context.workspace_id, manifest_id)
            held = installed_state(fenced, workspace_id=context.workspace_id, manifest_id=manifest_id)
            if held is None:
                # A version never installed has no event to report, so there is no state to answer
                # with. Refusing says so rather than inventing a sequence number.
                raise _refuse(ERROR_CODE_CONFLICT, _MESSAGE_NOT_INSTALLED)
            if held.event_kind == "remove":
                # Already removed: the current state answers, and nothing new is recorded.
                state = held
            else:
                writer = transaction_local_skills_writer(fenced, workspace_id=context.workspace_id)
                state = _store(
                    lambda: writer.record_install_event(
                        install_event_id=_row_id("skrem", settlement),
                        manifest_id=manifest_id,
                        event_kind="remove",
                        actor=context.principal,
                        at_us=settlement.settled_at_us,
                        audit_ref=settlement.audit_ref,
                    )
                )
            return SkillRemoveResult(
                manifest_id=manifest_id,
                skill_name=version.skill_name,
                version=version.version,
                install_state="removed",
                event_sequence=state.event_sequence,
                recorded_at=_timestamp(state.occurred_at_us),
            ).to_wire()

        return self._mutate(context, request.to_wire(), mutate, _VALID_REMOVE)

    # -- skills.resolve --------------------------------------------------------------

    def skills_resolve(self, context: OperationContext) -> Mapping[str, Any]:
        """The closure one role's selections resolve to. A read: it binds nothing and changes nothing."""
        request = self._input(context, frozenset({"role_id", "selections"}), SkillResolveInput.from_wire)
        role_id = _identifier(request.role_id, "role_id")
        requests = _selections(context.request.input["selections"])
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise _refuse(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        try:
            closure = resolve_role_selection(
                connection,
                workspace_id=context.workspace_id,
                role_id=role_id,
                requests=requests,
            )
        except SkillResolutionError as error:
            raise _resolution_refusal(error) from error
        except StorageError as error:
            raise _refuse(ERROR_CODE_INTERNAL_NON_RECOVERABLE, str(error)) from error
        return SkillResolveResult(
            role_id=role_id,
            entries=tuple(
                SkillResolvedEntry(
                    manifest_id=entry.manifest_id,
                    skill_name=entry.skill_name,
                    version=entry.version,
                    selection=entry.selection,
                )
                for entry in closure.entries
            ),
        ).to_wire()


__all__ = [
    "SKILL_DRAFT_CREATE_OPERATION",
    "SKILL_DRAFT_UPDATE_OPERATION",
    "SKILL_INSTALL_OPERATION",
    "SKILL_MUTATION_OPERATIONS",
    "SKILL_PROPOSAL_SUBMIT_OPERATION",
    "SKILL_REMOVE_OPERATION",
    "SKILL_RESOLVE_OPERATION",
    "SKILL_VERSION_DEPRECATE_OPERATION",
    "SKILL_VERSION_PUBLISH_OPERATION",
    "SkillHandlers",
    "resolve_run_skills",
    "role_selections",
    "skill_selections_input",
]
