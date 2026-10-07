"""The static authority C08 outcome admission checks a request against.

`OutcomeAdmissionAuthority` says what the installation allows: which Projects exist and their lifecycle,
which Works each Project holds, which target and immutable revision pairs each Work may use, which scopes a
Project admits, and which principals are the accountable owner, executor and reviewer. The service builds it
once from the `omnivia.knowledge-projects.v2` document in `service/knowledge_projects.py`, the same document
that binds knowledge sharing, so there is one Project source.

Every decision is pure and deterministic over that snapshot. It never authenticates a caller, and it never
authorizes a request principal. Authentication, current membership, and the context generation a decision is
made against remain handler responsibilities. A declared role is a claim a handler carries into a decision,
not the principal making the request. A success is a verified projection of the bindings. It is not a grant,
a fence, or permission to execute.

Refusals raise `OutcomeAdmissionRefused` with one closed `.reason` and carry no request or stored value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import is_identifier

__all__ = [
    "ACTION_READ",
    "ACTION_REVIEW",
    "ACTION_SUBMIT",
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_ARCHIVED",
    "LIFECYCLE_PAUSED",
    "NO_OUTCOME_ADMISSIONS",
    "REFUSAL_REASONS",
    "SCOPE_EXECUTE",
    "SCOPE_PREPARE",
    "SCOPE_READ",
    "AccountableRoles",
    "AdmissionProjectBinding",
    "AdmissionSourceBinding",
    "AdmissionWorkBinding",
    "AdmittedOutcome",
    "DeclaredRoles",
    "OutcomeAdmissionAuthority",
    "OutcomeAdmissionRefused",
    "is_source_target",
]

LIFECYCLE_ACTIVE: Final = "active"
LIFECYCLE_PAUSED: Final = "paused"
LIFECYCLE_ARCHIVED: Final = "archived"
LIFECYCLES: Final = frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_PAUSED, LIFECYCLE_ARCHIVED})

SCOPE_READ: Final = "read"
SCOPE_PREPARE: Final = "prepare"
SCOPE_EXECUTE: Final = "execute"
#: The closed scope vocabulary, in the canonical order a decision returns scopes.
SCOPES: Final = (SCOPE_READ, SCOPE_PREPARE, SCOPE_EXECUTE)

ACTION_REVIEW: Final = "review_draft"
ACTION_SUBMIT: Final = "submit_outcome"
ACTION_READ: Final = "read"

#: What each lifecycle admits. Archived Projects stay readable and admit nothing that writes.
_PERMITTED: Final = {
    LIFECYCLE_ACTIVE: frozenset({ACTION_REVIEW, ACTION_SUBMIT, ACTION_READ}),
    LIFECYCLE_PAUSED: frozenset({ACTION_REVIEW, ACTION_READ}),
    LIFECYCLE_ARCHIVED: frozenset({ACTION_READ}),
}

REFUSED_UNKNOWN_PROJECT: Final = "unknown_project"
REFUSED_LIFECYCLE: Final = "lifecycle_closed"
REFUSED_UNKNOWN_WORK: Final = "unknown_work"
REFUSED_UNKNOWN_SOURCE: Final = "unknown_source"
REFUSED_UNKNOWN_REVISION: Final = "unknown_revision"
REFUSED_SCOPE: Final = "disallowed_scope"
REFUSED_DUPLICATE_ROLE: Final = "duplicate_role"
REFUSED_MISASSIGNED_ROLE: Final = "misassigned_role"

REFUSAL_REASONS: Final = frozenset(
    {
        REFUSED_UNKNOWN_PROJECT,
        REFUSED_LIFECYCLE,
        REFUSED_UNKNOWN_WORK,
        REFUSED_UNKNOWN_SOURCE,
        REFUSED_UNKNOWN_REVISION,
        REFUSED_SCOPE,
        REFUSED_DUPLICATE_ROLE,
        REFUSED_MISASSIGNED_ROLE,
    }
)


class OutcomeAdmissionRefused(Exception):
    """An admission decision was refused for one closed reason, in `.reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_MAX_TARGET_BYTES: Final = 512
#: C0 and C1 controls (NUL included), DEL, and the Unicode line and paragraph separators.
_TARGET_FORBIDDEN: Final = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def is_source_target(value: object) -> bool:
    """Whether `value` is a well-formed source target. It is an opaque binding, never opened as a path here.

    Non-empty UTF-8 text of at most 512 bytes, with no leading or trailing whitespace, no control characters
    and no line separators. Unlike an identifier, it may hold `/` and `:`, as `services/omnivia-memory-dev`
    and `repo://omnivia/dev` do.
    """
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _TARGET_FORBIDDEN.search(value) is not None
    ):
        return False
    try:
        return 0 < len(value.encode("utf-8")) <= _MAX_TARGET_BYTES
    except UnicodeEncodeError:
        return False


def _identifiers(value: object) -> tuple[str, ...]:
    """The members of a non-empty collection of distinct identifiers, in order. Text is refused, since its
    characters would read as members."""
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError("text and non-iterables are not collections of identifiers")
    members = tuple(value)
    if (
        not members
        or not all(is_identifier(member) for member in members)
        or len(set(members)) != len(members)
    ):
        raise ValueError("a collection is outside its closed shape")
    return members


@dataclass(frozen=True, slots=True)
class AdmissionSourceBinding:
    """One target and the immutable revisions a Work may take from it."""

    target: str
    revisions: tuple[str, ...]

    def __post_init__(self) -> None:
        revisions = _identifiers(self.revisions)
        object.__setattr__(self, "revisions", revisions)
        if not is_source_target(self.target):
            raise ValueError("a source binding is outside its closed shape")


@dataclass(frozen=True, slots=True)
class AdmissionWorkBinding:
    """One Work and the target/revision pairs it may use. Each target is named once."""

    work_id: str
    sources: tuple[AdmissionSourceBinding, ...]

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        object.__setattr__(self, "sources", sources)
        targets = [source.target for source in sources]
        if (
            not is_identifier(self.work_id)
            or not sources
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("a Work binding is outside its closed shape")

    def source(self, target: str) -> AdmissionSourceBinding | None:
        return next((s for s in self.sources if s.target == target), None)


@dataclass(frozen=True, slots=True)
class AccountableRoles:
    """The principals each accountable role may be declared as, for one Project."""

    owner: frozenset[str]
    executor: frozenset[str]
    reviewer: frozenset[str]

    def __post_init__(self) -> None:
        for role in ("owner", "executor", "reviewer"):
            object.__setattr__(self, role, frozenset(_identifiers(getattr(self, role))))


@dataclass(frozen=True, slots=True)
class AdmissionProjectBinding:
    """One Project as admission sees it. Owners and members are the same set knowledge sharing binds.

    `requested_scopes` is the Project's policy, stored in the canonical scope order. Each role's
    principals must be owners (owner role) or owners or members (executor and reviewer role).
    """

    project_id: str
    lifecycle: str
    owners: frozenset[str]
    members: frozenset[str]
    works: tuple[AdmissionWorkBinding, ...]
    requested_scopes: tuple[str, ...]
    roles: AccountableRoles

    def __post_init__(self) -> None:
        owners = frozenset(self.owners)
        members = frozenset(self.members)
        works = tuple(self.works)
        given_scopes = tuple(self.requested_scopes)
        object.__setattr__(self, "owners", owners)
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "works", works)
        object.__setattr__(
            self,
            "requested_scopes",
            tuple(scope for scope in SCOPES if scope in given_scopes),
        )
        current = owners | members
        work_ids = [work.work_id for work in works]
        if (
            not is_identifier(self.project_id)
            or self.lifecycle not in LIFECYCLES
            or not owners
            or not all(is_identifier(value) for value in current)
            or not works
            or len(set(work_ids)) != len(work_ids)
            or not given_scopes
            or len(set(given_scopes)) != len(given_scopes)
            or not set(given_scopes) <= set(SCOPES)
        ):
            raise ValueError("a Project admission binding is outside its closed shape")
        roles = (self.roles.owner, self.roles.executor, self.roles.reviewer)
        if (
            not all(roles)
            or not self.roles.owner <= owners
            or not self.roles.executor <= current
            or not self.roles.reviewer <= current
        ):
            raise ValueError("a role is assigned outside its Project")

    def work(self, work_id: str) -> AdmissionWorkBinding | None:
        return next((w for w in self.works if w.work_id == work_id), None)


@dataclass(frozen=True, slots=True)
class DeclaredRoles:
    """The principals a request declares for the three accountable roles. A claim, not an identity."""

    owner: str
    executor: str
    reviewer: str

    def __post_init__(self) -> None:
        if not all(
            is_identifier(value) for value in (self.owner, self.executor, self.reviewer)
        ):
            raise ValueError("a declared role is outside its closed shape")


@dataclass(frozen=True, slots=True)
class AdmittedOutcome:
    """A verified projection of one admitted Work target. No principal, grant, fence or permission."""

    project_id: str
    work_id: str
    target: str
    revision: str
    scopes: tuple[str, ...]
    owner: str
    executor: str
    reviewer: str


@dataclass(frozen=True, slots=True)
class OutcomeAdmissionAuthority:
    """The Projects an installation admits outcomes for, in one Workspace, fixed at composition.

    Empty by default, which refuses every admission. One Project per id.
    """

    projects: tuple[AdmissionProjectBinding, ...] = ()

    def __post_init__(self) -> None:
        projects = tuple(self.projects)
        object.__setattr__(self, "projects", projects)
        ids = [project.project_id for project in projects]
        if len(set(ids)) != len(ids):
            raise ValueError("a Project is bound at most once")

    @classmethod
    def of(
        cls, projects: Iterable[AdmissionProjectBinding]
    ) -> OutcomeAdmissionAuthority:
        return cls(tuple(projects))

    def project(self, project_id: str, action: str) -> AdmissionProjectBinding:
        """The Project, when it exists and its lifecycle admits `action`. For navigation, use `ACTION_READ`.

        Raises `OutcomeAdmissionRefused` with `unknown_project` or `lifecycle_closed`.
        """
        binding = next((p for p in self.projects if p.project_id == project_id), None)
        if binding is None:
            raise OutcomeAdmissionRefused(REFUSED_UNKNOWN_PROJECT)
        if action not in _PERMITTED[binding.lifecycle]:
            raise OutcomeAdmissionRefused(REFUSED_LIFECYCLE)
        return binding

    def admit(
        self,
        *,
        project_id: str,
        action: str,
        work_id: str,
        target: str,
        revision: str,
        scopes: Iterable[str],
        roles: DeclaredRoles,
    ) -> AdmittedOutcome:
        """Admit one Work target for `action`, or raise with the first closed reason that fails.

        Checks run in order: Project exists and its lifecycle admits `action`; Work exists in it; target is
        bound to that Work; revision is bound to that target; scopes are a non-empty, unique subset of the
        Project's policy; the three declared roles are distinct and each is assigned to its role.
        """
        binding = self.project(project_id, action)
        work = binding.work(work_id)
        if work is None:
            raise OutcomeAdmissionRefused(REFUSED_UNKNOWN_WORK)
        source = work.source(target)
        if source is None:
            raise OutcomeAdmissionRefused(REFUSED_UNKNOWN_SOURCE)
        if revision not in source.revisions:
            raise OutcomeAdmissionRefused(REFUSED_UNKNOWN_REVISION)
        # A string is refused too: its characters are never a scope name, so it fails the subset check.
        unscopeable = False
        try:
            requested = tuple(scopes)
            distinct = set(requested)
        except TypeError:
            unscopeable = True
            requested = ()
            distinct = set()
        if (
            unscopeable
            or not requested
            or len(distinct) != len(requested)
            or not distinct <= set(binding.requested_scopes)
        ):
            raise OutcomeAdmissionRefused(REFUSED_SCOPE)
        if not isinstance(roles, DeclaredRoles):
            raise OutcomeAdmissionRefused(REFUSED_MISASSIGNED_ROLE)
        declared = (roles.owner, roles.executor, roles.reviewer)
        if len(set(declared)) != len(declared):
            raise OutcomeAdmissionRefused(REFUSED_DUPLICATE_ROLE)
        # The owner role is a subset of owners by construction, so only executor and reviewer need the
        # current owner-or-member check here.
        current = binding.owners | binding.members
        if (
            roles.owner not in binding.roles.owner
            or roles.executor not in binding.roles.executor
            or roles.reviewer not in binding.roles.reviewer
            or roles.executor not in current
            or roles.reviewer not in current
        ):
            raise OutcomeAdmissionRefused(REFUSED_MISASSIGNED_ROLE)
        return AdmittedOutcome(
            project_id=binding.project_id,
            work_id=work.work_id,
            target=source.target,
            revision=revision,
            scopes=tuple(
                scope for scope in binding.requested_scopes if scope in requested
            ),
            owner=roles.owner,
            executor=roles.executor,
            reviewer=roles.reviewer,
        )


#: No Project admitted, which refuses every structured outcome request, and cannot be widened by a request.
NO_OUTCOME_ADMISSIONS: Final = OutcomeAdmissionAuthority()
