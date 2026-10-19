"""Domain rules for explicit cross-Project knowledge sharing (DEV-REQ-081).

An owner of a source Project proposes sharing one sealed, canonical governed version, a different
owner of that Project accepts it, and a member of the one recipient Project reads it. An owner can
revoke it, and the lineage stays readable to the source owners afterwards.

These are domain rules, not an entry point. They are served only through the registered
`knowledge.share.*` operations in `service/handlers/knowledge_sharing.py`, which authenticate the
caller, open the fenced mutation and hand the authenticated principal in here.

Project authority is a server fact. `ProjectAuthority` is composed once by the service from the
Projects it binds: each `ProjectBinding` states the one domain scope the Project owns, the principals
that own it and the principals that are its members. Nothing in a request can add to it. The source
Project of a proposal is derived from the shared record's own domain scope, so a caller cannot become
the source owner by naming a Project, and a record's scope that no bound Project owns cannot be
shared. A recipient read is allowed only to a member of the Project the share names, so a workspace
grant, a knowledge-read grant, a share identifier, a prior read or the source's visibility never
establishes recipient eligibility.

Eligibility is derived from the stored accepted and revoked decisions and from the shared governed
version on every read. Nothing here caches a result, so a revocation or a superseded version is
effective on the next call. Refusals name a closed reason and carry no caller or stored value.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import is_identifier, is_record_domain_scope
from omnivia_core_runtime.storage.knowledge_shares import (
    DECISION_ACCEPTED,
    DECISION_REVOKED,
    KnowledgeShare,
    ShareDecision,
    read_decisions,
    read_share,
    record_decision,
    record_share,
)

OPERATION_PROPOSE: Final = "knowledge.share.propose"
OPERATION_DECIDE: Final = "knowledge.share.decide"
OPERATION_READ: Final = "knowledge.share.read"
OPERATION_LINEAGE: Final = "knowledge.share.lineage"

REFUSED_NOT_FOUND: Final = "not_found"
REFUSED_UNKNOWN_RECORD: Final = "unknown_record"
REFUSED_UNKNOWN_RECIPIENT: Final = "unknown_recipient"
REFUSED_NOT_OWNER: Final = "not_owner"
REFUSED_SELF_SHARE: Final = "self_share"
REFUSED_SELF_DECISION: Final = "self_decision"
REFUSED_NOT_AUTHORITATIVE: Final = "not_authoritative"
REFUSED_NOT_ELIGIBLE: Final = "not_eligible"
REFUSED_STALE_SOURCE: Final = "stale_source"
REFUSED_WRONG_STATE: Final = "wrong_state"

STATE_PROPOSED: Final = "proposed"
STATE_ACCEPTED: Final = "accepted"
STATE_REVOKED: Final = "revoked"


class KnowledgeShareRefused(Exception):
    """A sharing operation was refused for one closed, named reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProjectBinding:
    """One Project as the server binds it: the domain scope it owns and who acts for it.

    `owners` may propose, decide and read the lineage of shares from this Project. `members` may read
    what other Projects share with it. The two sets are independent: owning a Project does not make
    its owner a reader of what is shared to it, and membership does not let a reader share.
    """

    project_id: str
    domain_scope: str
    owners: frozenset[str]
    members: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        owners = frozenset(self.owners)
        members = frozenset(self.members)
        object.__setattr__(self, "owners", owners)
        object.__setattr__(self, "members", members)
        if not is_identifier(self.project_id) or not is_record_domain_scope(
            self.domain_scope
        ):
            raise ValueError("a Project binding is outside its closed shape")
        if not owners or not all(is_identifier(value) for value in owners | members):
            raise ValueError("a Project binding needs owners that are identifiers")


@dataclass(frozen=True, slots=True)
class ProjectAuthority:
    """The Projects a service instance binds, fixed when the service is composed.

    Empty by default, which refuses every sharing operation: a build that was handed no bindings has
    no Project anyone can own or read for. One Project per id and one Project per domain scope, so a
    record's scope names exactly one source Project.
    """

    bindings: tuple[ProjectBinding, ...] = ()

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        object.__setattr__(self, "bindings", bindings)
        ids = [binding.project_id for binding in bindings]
        scopes = [binding.domain_scope for binding in bindings]
        if len(set(ids)) != len(ids) or len(set(scopes)) != len(scopes):
            raise ValueError("a Project and a domain scope are each bound at most once")

    @classmethod
    def of(cls, bindings: Iterable[ProjectBinding]) -> ProjectAuthority:
        return cls(tuple(bindings))

    def project(self, project_id: str) -> ProjectBinding | None:
        return next((b for b in self.bindings if b.project_id == project_id), None)

    def for_scope(self, domain_scope: str) -> ProjectBinding | None:
        return next((b for b in self.bindings if b.domain_scope == domain_scope), None)


#: No Project bound, which refuses every sharing operation.
NO_PROJECTS: Final = ProjectAuthority()


@dataclass(frozen=True, slots=True)
class SharedLesson:
    """The one governed version a recipient may see, bound to the share that made it eligible."""

    share: KnowledgeShare
    content_json: str


@dataclass(frozen=True, slots=True)
class ShareLineage:
    """A share and every decision recorded against it, including revoked ones."""

    share: KnowledgeShare
    state: str
    decisions: tuple[ShareDecision, ...]


def share_state(decisions: dict[str, ShareDecision]) -> str:
    if DECISION_REVOKED in decisions:
        return STATE_REVOKED
    return STATE_ACCEPTED if DECISION_ACCEPTED in decisions else STATE_PROPOSED


def propose_share(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    *,
    principal: str,
    workspace_id: str,
    generation: int,
    share_id: str,
    record_id: str,
    recipient_project_id: str,
    proposed_at_us: int,
) -> KnowledgeShare:
    """Propose sharing a record's current sealed version from the Project that owns its scope.

    The source Project comes from the record's own domain scope and the server's bindings, and the
    caller must own it. The version's identity, scope and digest are read from the workspace's sealed
    record, so a proposal cannot name a candidate, a superseded version or a digest it does not hold.
    Runs inside the caller's fenced transaction.
    """
    if (
        connection.execute(
            "SELECT 1 FROM omnivia_governed_records "
            "WHERE workspace_id = ? AND governed_record_id = ?",
            (workspace_id, record_id),
        ).fetchone()
        is None
    ):
        raise KnowledgeShareRefused(REFUSED_UNKNOWN_RECORD, "the governed record is unknown")
    version = _authoritative_version(connection, workspace_id, record_id=record_id)
    if version is None:
        raise KnowledgeShareRefused(
            REFUSED_NOT_AUTHORITATIVE,
            "the governed record has no sealed, canonical, unsuperseded version",
        )
    source = authority.for_scope(version.domain_scope)
    if source is None or principal not in source.owners:
        raise KnowledgeShareRefused(
            REFUSED_NOT_OWNER, "the caller does not own the Project that holds this record"
        )
    recipient = authority.project(recipient_project_id)
    if recipient is None:
        raise KnowledgeShareRefused(
            REFUSED_UNKNOWN_RECIPIENT, "the recipient Project is not bound"
        )
    if recipient.project_id == source.project_id:
        raise KnowledgeShareRefused(
            REFUSED_SELF_SHARE, "a share must name a recipient other than its source"
        )
    return record_share(
        connection,
        KnowledgeShare(
            workspace_id=workspace_id,
            share_id=share_id,
            source_project_id=source.project_id,
            recipient_project_id=recipient.project_id,
            governed_record_id=record_id,
            governed_assembly_id=version.assembly_id,
            governed_record_version_id=version.governed_record_version_id,
            domain_scope=version.domain_scope,
            content_digest=version.content_digest,
            proposed_by=principal,
            proposed_under_generation=generation,
            proposed_at_us=proposed_at_us,
        ),
    )


def decide_share(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    *,
    principal: str,
    workspace_id: str,
    generation: int,
    share_id: str,
    decision: str,
    decided_at_us: int,
) -> ShareDecision:
    """Accept or revoke one share as an owner of its source Project.

    An acceptance must come from an owner other than the proposer and only while the shared version
    is still the sealed, canonical, unsuperseded one the share was proposed under. A revocation needs
    no such check, because it only withdraws eligibility, but it needs an earlier acceptance, and a
    revoked share cannot be accepted again. Runs inside the caller's fenced transaction.
    """
    share, _source = _owned_share(connection, authority, principal, workspace_id, share_id)
    decisions = read_decisions(connection, workspace_id=workspace_id, share_id=share_id)
    if decision == DECISION_ACCEPTED:
        if DECISION_REVOKED in decisions:
            raise KnowledgeShareRefused(
                REFUSED_WRONG_STATE, "a revoked share cannot be accepted"
            )
        if DECISION_ACCEPTED not in decisions:
            if share.proposed_by == principal:
                raise KnowledgeShareRefused(
                    REFUSED_SELF_DECISION,
                    "a share must be accepted by an owner other than its proposer",
                )
            _require_current(connection, authority, share)
    elif decision == DECISION_REVOKED:
        if DECISION_ACCEPTED not in decisions:
            raise KnowledgeShareRefused(
                REFUSED_WRONG_STATE, "a share can be revoked only after it is accepted"
            )
    else:
        raise KnowledgeShareRefused(REFUSED_WRONG_STATE, "the decision is not known")
    return record_decision(
        connection,
        ShareDecision(
            workspace_id=workspace_id,
            share_id=share_id,
            decision=decision,
            decided_by=principal,
            decided_under_generation=generation,
            decided_at_us=decided_at_us,
        ),
    )


def read_shared_lesson(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    *,
    principal: str,
    workspace_id: str,
    share_id: str,
) -> SharedLesson:
    """Return the shared version to a member of its recipient Project, only while it is eligible.

    The recipient Project is read from the share and the caller must be bound to it; a caller that is
    not sees the share as absent. The share must carry an accepted decision and no revocation, and
    still point at the sealed version, digest and domain scope it was proposed under, which the
    source Project must still own. Every call re-derives all of it and nothing is cached.
    """
    share = read_share(connection, workspace_id=workspace_id, share_id=share_id)
    recipient = None if share is None else authority.project(share.recipient_project_id)
    if share is None or recipient is None or principal not in recipient.members:
        raise KnowledgeShareRefused(REFUSED_NOT_FOUND, "no such share is visible to the caller")
    decisions = read_decisions(connection, workspace_id=workspace_id, share_id=share_id)
    if DECISION_ACCEPTED not in decisions or DECISION_REVOKED in decisions:
        raise KnowledgeShareRefused(
            REFUSED_NOT_ELIGIBLE, "the share is not currently accepted for this recipient"
        )
    version = _require_current(connection, authority, share)
    return SharedLesson(share=share, content_json=version.content_json)


def is_share_eligible(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    *,
    principal: str,
    workspace_id: str,
    share_id: str,
) -> bool:
    """Whether `principal` may be served this share right now.

    This is the check a context-pack or cache layer must run for each use of a shared lesson instead
    of remembering an earlier answer. It is `read_shared_lesson` without the content, so a revoked
    share, a superseded source or a principal whose recipient binding is gone is `False` on the next
    call. A row that fails its digest still raises: corruption is not ineligibility.
    """
    try:
        read_shared_lesson(
            connection,
            authority,
            principal=principal,
            workspace_id=workspace_id,
            share_id=share_id,
        )
    except KnowledgeShareRefused:
        return False
    return True


def require_share_owner(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    *,
    principal: str,
    workspace_id: str,
    share_id: str,
) -> None:
    """Refuse unless `principal` currently owns the source Project of an existing share.

    A replayed mutation is answered from its stored outcome without running the domain write, so this
    is what stops an owner whose binding was withdrawn from re-serving the stored answer.
    """
    _owned_share(connection, authority, principal, workspace_id, share_id)


def read_share_lineage(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    *,
    principal: str,
    workspace_id: str,
    share_id: str,
) -> ShareLineage:
    """Return a share and all of its decisions, revoked ones included, to a source owner.

    Historical records stay retrievable after revocation and after the source version changes. A
    recipient is not a source owner and sees the share as absent.
    """
    share, _source = _owned_share(connection, authority, principal, workspace_id, share_id)
    decisions = read_decisions(connection, workspace_id=workspace_id, share_id=share_id)
    return ShareLineage(
        share=share,
        state=share_state(decisions),
        decisions=tuple(
            sorted(decisions.values(), key=lambda d: (d.decided_at_us, d.decision))
        ),
    )


@dataclass(frozen=True, slots=True)
class _Version:
    assembly_id: str
    governed_record_id: str
    governed_record_version_id: str
    domain_scope: str
    content_json: str
    content_digest: str


def _authoritative_version(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    record_id: str | None = None,
    assembly_id: str | None = None,
) -> _Version | None:
    """The sealed, canonical, accepted and unsuperseded version of a record or assembly, or `None`."""
    column, value = (
        ("governed_record_id", record_id) if assembly_id is None else ("assembly_id", assembly_id)
    )
    row = connection.execute(
        "SELECT assembly_id, governed_record_id, governed_record_version_id, domain_scope, "
        "content_json, content_digest "
        "FROM omnivia_authoritative_governed_versions AS v "
        f"WHERE v.workspace_id = ? AND v.{column} = ? "
        "AND v.authority_level = 'canonical' AND v.governance_disposition = 'accepted' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM omnivia_record_supersessions r "
        "  WHERE r.workspace_id = v.workspace_id "
        "  AND r.source_version_id = v.governed_record_version_id) "
        "ORDER BY v.append_ordinal DESC LIMIT 1",
        (workspace_id, value),
    ).fetchone()
    return None if row is None else _Version(*(str(item) for item in row))


def _require_current(
    connection: sqlite3.Connection, authority: ProjectAuthority, share: KnowledgeShare
) -> _Version:
    """The share's version, only while it is still the one proposed and its scope is still owned.

    The version must be authoritative with the record, version, digest and domain scope the share
    carries, and the source Project the server binds must still own that scope.
    """
    version = _authoritative_version(
        connection, share.workspace_id, assembly_id=share.governed_assembly_id
    )
    source = authority.project(share.source_project_id)
    if (
        version is None
        or source is None
        or version.governed_record_id != share.governed_record_id
        or version.governed_record_version_id != share.governed_record_version_id
        or version.content_digest != share.content_digest
        or version.domain_scope != share.domain_scope
        or source.domain_scope != share.domain_scope
    ):
        raise KnowledgeShareRefused(
            REFUSED_STALE_SOURCE,
            "the shared governed version has changed or is no longer authoritative",
        )
    return version


def _owned_share(
    connection: sqlite3.Connection,
    authority: ProjectAuthority,
    principal: str,
    workspace_id: str,
    share_id: str,
) -> tuple[KnowledgeShare, ProjectBinding]:
    """The share and its source Project binding, for a caller that owns that Project.

    A caller that does not own it, or a share that does not exist, reads the same: no such share.
    """
    share = read_share(connection, workspace_id=workspace_id, share_id=share_id)
    source = None if share is None else authority.project(share.source_project_id)
    if share is None or source is None or principal not in source.owners:
        raise KnowledgeShareRefused(REFUSED_NOT_FOUND, "no such share is visible to the caller")
    return share, source
