"""The managed Skills registry over migration 0064, and nothing above it.

0064 adds nine append-only tables: drafts, draft revisions, proposals, published versions,
deprecations, install events, per-Run bindings, the seals that close them and the accepted
amendments that open later generations. This module is their writer and their bounded reads.
It executes no skill, selects nothing for a Run on its own account and grants nothing: a
manifest is inert data (:mod:`semantics_skills`), and the only authority a skill version has
is what the bound role's envelope already grants.

Writes
------

Every write is issued into a transaction the caller already fenced, through
:func:`transaction_local_skills_writer`. Revision numbers, event sequences and binding
positions are allocated here and enforced by 0064's triggers; a caller never names its own
position in a durable history. Who did each thing is recorded on its own row, because
authorship never implies publication and publication never implies installation.

A version publishes exactly the draft revision its proposal submitted. The same
`(skill_name, version)` is never published twice, whether the content is identical or
different, and each case refuses as its own exception so a caller can say which it was.

Reads
-----

Every read is scoped to a workspace in SQL and bounded. A manifest is *recomputed* on read:
its stored canonical text must hash to its id, so a row edited outside this database raises
:class:`StorageError` instead of reading as another manifest. A Run's bindings are verified
the same way, row by row and as a set, and a set with no seal is refused rather than served.

*Resolution is a read.* :func:`resolve_role_selection` applies the precedence the contract
fixes -- an explicit manifest reference, then the role-compatibility filter, then the highest
compatible published version -- over what the workspace has installed, and then walks the
pinned dependencies to a bounded closure. A deprecated or uninstalled version is never newly
selected. A Run's own binding is read back by :func:`read_run_skill_bindings`, which never
consults install or deprecation state, because removing or deprecating a skill must not
change what an in-flight Run was admitted with.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1.canonical_json import canonical_bytes, canonicalize
from omnivia_core.contracts.v1.generated import (
    is_content_checksum,
    is_identifier,
    is_open_code,
)
from omnivia_core.contracts.v1.semantics_skills import (
    CODE_ROLE_INCOMPATIBLE,
    MAX_EVIDENCE_REFS,
    MAX_RUN_ROLES,
    MAX_SELECTIONS_PER_ROLE,
    SKILL_SELECTION_EXPLICIT,
    SKILL_SELECTION_HIGHEST_COMPATIBLE,
    ClosureEntry,
    SkillClosureError,
    SkillManifestError,
    canonical_skill_manifest,
    is_skill_manifest_id,
    resolve_closure,
    skill_manifest_from_canonical,
    skill_manifest_id,
    skill_version_key,
    validate_skill_manifest,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "MAX_BINDING_GENERATIONS",
    "MAX_INSTALLED_PER_SKILL",
    "ManagedSkillsWriter",
    "RoleClosure",
    "RunSkillBindings",
    "SkillDeprecation",
    "SkillDraft",
    "SkillDraftHead",
    "SkillDraftRevision",
    "SkillInstallEvent",
    "SkillProposal",
    "SkillResolutionError",
    "SkillRunBinding",
    "SkillVersion",
    "SkillVersionConflict",
    "installed_state",
    "managed_skills_writer",
    "read_draft_head",
    "read_proposal",
    "read_run_skill_binding_generations",
    "read_run_skill_bindings",
    "read_skill_version",
    "read_skill_version_by_name",
    "resolve_role_selection",
    "transaction_local_skills_writer",
]

_DRAFTS: Final = "omnivia_skill_drafts"
_REVISIONS: Final = "omnivia_skill_draft_revisions"
_PROPOSALS: Final = "omnivia_skill_proposals"
_VERSIONS: Final = "omnivia_skill_versions"
_DEPRECATIONS: Final = "omnivia_skill_deprecations"
_INSTALLS: Final = "omnivia_skill_install_events"
_BINDINGS: Final = "omnivia_skill_run_bindings"
_SEALS: Final = "omnivia_skill_run_binding_seals"
_AMENDMENTS: Final = "omnivia_skill_binding_amendments"

#: Distinct versions of one skill that may be installed at once. An update is an explicit
#: install of a newer version beside the old one, so this bounds how many accumulate before an
#: operator removes one.
MAX_INSTALLED_PER_SKILL: Final = 32
#: Binding generations one Run may hold: its admission, then at most this many minus one
#: accepted amendments. Bounds every read of a Run's history; migration 0064 enforces it too.
MAX_BINDING_GENERATIONS: Final = 32

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PRINCIPAL = re.compile(r"[^\x00]{1,128}")

#: Refusal codes of a resolution, beside the closure's own.
CODE_NOT_FOUND: Final = "skill_not_found"
CODE_NOT_INSTALLED: Final = "skill_not_installed"
CODE_DEPRECATED: Final = "skill_deprecated"
CODE_MISMATCH: Final = "selection_mismatch"
CODE_NO_COMPATIBLE: Final = "no_compatible_version"
CODE_DUPLICATE: Final = "duplicate_selection"


class SkillVersionConflict(StorageError):
    """`(skill_name, version)` is already published. `identical` says whether to the byte."""

    def __init__(self, message: str, *, identical: bool) -> None:
        super().__init__(message)
        self.identical = identical


class SkillResolutionError(StorageError):
    """A selection that cannot be resolved. `code` names the rule that refused it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- records -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillDraft:
    draft_id: str
    skill_name: str
    source_work_ref: str | None
    created_by: str
    created_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SkillDraftRevision:
    draft_revision_id: str
    draft_id: str
    skill_name: str
    draft_revision: int
    version: str
    manifest_id: str
    manifest: Mapping[str, Any]
    updated_by: str
    created_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SkillProposal:
    proposal_id: str
    draft_id: str
    draft_revision: int
    skill_name: str
    evidence: tuple[Mapping[str, str], ...]
    submitted_by: str
    submitted_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SkillDeprecation:
    deprecation_id: str
    manifest_id: str
    reason: str
    deprecated_by: str
    deprecated_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SkillVersion:
    """One published version, its manifest recomputed, and whether it is now deprecated."""

    manifest_id: str
    skill_name: str
    version: str
    manifest: Mapping[str, Any]
    draft_id: str
    draft_revision: int
    proposal_id: str
    review_evidence: tuple[Mapping[str, str], ...]
    published_by: str
    published_at_us: int
    audit_ref: str
    deprecation: SkillDeprecation | None = None


@dataclass(frozen=True, slots=True)
class SkillInstallEvent:
    install_event_id: str
    manifest_id: str
    skill_name: str
    event_sequence: int
    event_kind: str
    actor: str
    occurred_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SkillRunBinding:
    run_binding_id: str
    run_id: str
    binding_generation: int
    binding_position: int
    role_id: str
    manifest_id: str
    skill_name: str
    selection: str
    binding_digest: str
    bound_at_us: int
    audit_ref: str
    #: Read from the immutable published version, not stored on the binding row.
    version: str = ""


@dataclass(frozen=True, slots=True)
class SkillDraftHead:
    """A draft, its latest revision and, once submitted, its proposal."""

    draft: SkillDraft
    latest: SkillDraftRevision
    proposal: SkillProposal | None


@dataclass(frozen=True, slots=True)
class RoleClosure:
    """One role's resolved selections and the closure they pull in, dependencies first."""

    role_id: str
    entries: tuple[ClosureEntry, ...]


@dataclass(frozen=True, slots=True)
class RunSkillBindings:
    """One sealed generation of a Run's bindings, verified, in the order it was bound.

    Generation 1 is what the Run was admitted with. A later generation was opened by one
    accepted amendment, named in `amendment_id`; every earlier generation stays readable.
    """

    run_id: str
    bindings: tuple[SkillRunBinding, ...]
    set_digest: str
    sealed_at_us: int
    binding_generation: int = 1
    amendment_id: str | None = None

    @property
    def roles(self) -> tuple[RoleClosure, ...]:
        order: list[str] = []
        grouped: dict[str, list[ClosureEntry]] = {}
        for binding in self.bindings:
            if binding.role_id not in grouped:
                order.append(binding.role_id)
                grouped[binding.role_id] = []
            grouped[binding.role_id].append(
                ClosureEntry(
                    binding.manifest_id,
                    binding.skill_name,
                    binding.version,
                    binding.selection,
                )
            )
        return tuple(RoleClosure(role, tuple(grouped[role])) for role in order)


# --- validation ----------------------------------------------------------------------


def _text(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StorageError(f"{label} is malformed")
    return value


def _time(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageError(f"{label} must be a positive integer of microseconds")
    return value


def _evidence(
    value: object, label: str, *, at_least: int
) -> tuple[dict[str, str], ...]:
    """Evidence references in their one canonical order. Recorded, never dereferenced."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StorageError(f"{label} must be an array")
    if not at_least <= len(value) <= MAX_EVIDENCE_REFS:
        raise StorageError(
            f"{label} must hold {at_least} to {MAX_EVIDENCE_REFS} references"
        )
    refs: list[dict[str, str]] = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"evidence_id", "content_digest"}
            or not is_identifier(item["evidence_id"])
            or not is_content_checksum(item["content_digest"])
        ):
            raise StorageError(f"{label} has a malformed reference")
        refs.append(
            {
                "evidence_id": item["evidence_id"],
                "content_digest": item["content_digest"],
            }
        )
    if len({ref["evidence_id"] for ref in refs}) != len(refs):
        raise StorageError(f"{label} repeats a reference")
    return tuple(sorted(refs, key=lambda ref: ref["evidence_id"]))


def _evidence_from_text(text: str) -> tuple[Mapping[str, str], ...]:
    try:
        return _evidence(json.loads(text), "stored evidence", at_least=0)
    except ValueError as error:
        raise StorageError("stored evidence is not JSON") from error


def _manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return validate_skill_manifest(manifest)
    except SkillManifestError as error:
        raise StorageError(str(error)) from error


def _require_audit(
    connection: sqlite3.Connection, workspace_id: str, audit_ref: str
) -> None:
    found = connection.execute(
        "SELECT 1 FROM omnivia_application_audit_events WHERE audit_ref = ? AND workspace_id = ?",
        (audit_ref, workspace_id),
    ).fetchone()
    if found is None:
        raise StorageError(f"audit_ref {audit_ref!r} is not recorded in this workspace")


def _insert(
    connection: sqlite3.Connection,
    table: str,
    workspace_id: str,
    values: Mapping[str, object],
) -> None:
    columns = ["workspace_id", *values]
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        (workspace_id, *values.values()),
    )


def _binding_digest(
    *,
    run_id: str,
    binding_generation: int,
    binding_position: int,
    role_id: str,
    manifest_id: str,
    skill_name: str,
    selection: str,
    bound_at_us: int,
) -> str:
    return (
        "sha256:"
        + sha256(
            canonical_bytes(
                {
                    "run_id": run_id,
                    "binding_generation": binding_generation,
                    "binding_position": binding_position,
                    "role_id": role_id,
                    "manifest_id": manifest_id,
                    "skill_name": skill_name,
                    "selection": selection,
                    "bound_at_us": bound_at_us,
                }
            )
        ).hexdigest()
    )


def _set_digest(digests: Sequence[str]) -> str:
    return "sha256:" + sha256(canonical_bytes({"bindings": list(digests)})).hexdigest()


# --- writer --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManagedSkillsWriter:
    """The registry writes, issued into a transaction that is already open."""

    connection: sqlite3.Connection
    workspace_id: str

    def create_draft(
        self,
        *,
        draft_id: str,
        draft_revision_id: str,
        manifest: Mapping[str, Any],
        source_work_ref: str | None,
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillDraftHead:
        """Open a draft at revision 1. The skill name is fixed here for the draft's life."""
        valid = _manifest(manifest)
        draft = SkillDraft(
            draft_id=_text(draft_id, _ID, "draft_id"),
            skill_name=valid["skill_name"],
            source_work_ref=None
            if source_work_ref is None
            else _text(source_work_ref, _ID, "source_work_ref"),
            created_by=_text(actor, _PRINCIPAL, "actor"),
            created_at_us=_time(at_us, "at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        _require_audit(self.connection, self.workspace_id, draft.audit_ref)
        _insert(self.connection, _DRAFTS, self.workspace_id, _fields(draft))
        revision = self._append_revision(
            draft_revision_id=draft_revision_id,
            draft_id=draft.draft_id,
            skill_name=draft.skill_name,
            manifest=valid,
            actor=actor,
            at_us=at_us,
            audit_ref=audit_ref,
        )
        return SkillDraftHead(draft, revision, None)

    def revise_draft(
        self,
        *,
        draft_revision_id: str,
        draft_id: str,
        expected_revision: int,
        manifest: Mapping[str, Any],
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillDraftRevision:
        """Append one revision, only on top of the revision the caller last saw.

        A stale `expected_revision` refuses, so two authors never silently overwrite each
        other. A submitted draft is closed, a changed skill name is refused, and a revision
        that changes nothing is refused, so every revision is a real step.
        """
        valid = _manifest(manifest)
        head = read_draft_head(
            self.connection, workspace_id=self.workspace_id, draft_id=draft_id
        )
        if head is None:
            raise StorageError(f"draft {draft_id!r} is not recorded in this workspace")
        if head.proposal is not None:
            raise StorageError(
                f"draft {draft_id!r} was submitted and is closed to revision"
            )
        if head.latest.draft_revision != expected_revision:
            raise StorageError(
                f"draft {draft_id!r} is at revision {head.latest.draft_revision}, "
                f"not the revision {expected_revision} this update was made against"
            )
        if valid["skill_name"] != head.draft.skill_name:
            raise StorageError("a draft keeps the skill name it was created with")
        if skill_manifest_id(valid) == head.latest.manifest_id:
            raise StorageError("the update changes nothing in the manifest")
        return self._append_revision(
            draft_revision_id=draft_revision_id,
            draft_id=draft_id,
            skill_name=head.draft.skill_name,
            manifest=valid,
            actor=actor,
            at_us=at_us,
            audit_ref=audit_ref,
        )

    def _append_revision(
        self,
        *,
        draft_revision_id: str,
        draft_id: str,
        skill_name: str,
        manifest: Mapping[str, Any],
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillDraftRevision:
        previous = self.connection.execute(
            f"SELECT COALESCE(MAX(draft_revision), 0) FROM {_REVISIONS} "
            "WHERE workspace_id = ? AND draft_id = ?",
            (self.workspace_id, draft_id),
        ).fetchone()
        revision = SkillDraftRevision(
            draft_revision_id=_text(draft_revision_id, _ID, "draft_revision_id"),
            draft_id=draft_id,
            skill_name=skill_name,
            draft_revision=int(previous[0]) + 1,
            version=manifest["version"],
            manifest_id=skill_manifest_id(manifest),
            manifest=manifest,
            updated_by=_text(actor, _PRINCIPAL, "actor"),
            created_at_us=_time(at_us, "at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        _require_audit(self.connection, self.workspace_id, revision.audit_ref)
        row = _fields(revision)
        row["manifest_json"] = canonical_skill_manifest(manifest)
        del row["manifest"]
        _insert(self.connection, _REVISIONS, self.workspace_id, row)
        return revision

    def submit_proposal(
        self,
        *,
        proposal_id: str,
        draft_id: str,
        expected_revision: int,
        evidence: Sequence[Mapping[str, str]],
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillProposal:
        """Submit the draft's latest revision to the publisher queue, once."""
        head = read_draft_head(
            self.connection, workspace_id=self.workspace_id, draft_id=draft_id
        )
        if head is None:
            raise StorageError(f"draft {draft_id!r} is not recorded in this workspace")
        if head.proposal is not None:
            raise StorageError(f"draft {draft_id!r} was already submitted")
        if head.latest.draft_revision != expected_revision:
            raise StorageError(
                f"draft {draft_id!r} is at revision {head.latest.draft_revision}, "
                f"not the revision {expected_revision} this submission was made against"
            )
        proposal = SkillProposal(
            proposal_id=_text(proposal_id, _ID, "proposal_id"),
            draft_id=draft_id,
            draft_revision=head.latest.draft_revision,
            skill_name=head.draft.skill_name,
            evidence=_evidence(evidence, "evidence_refs", at_least=0),
            submitted_by=_text(actor, _PRINCIPAL, "actor"),
            submitted_at_us=_time(at_us, "at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        _require_audit(self.connection, self.workspace_id, proposal.audit_ref)
        row = _fields(proposal)
        row["evidence_json"] = canonicalize(list(proposal.evidence))
        del row["evidence"]
        _insert(self.connection, _PROPOSALS, self.workspace_id, row)
        return proposal

    def publish_version(
        self,
        *,
        proposal_id: str,
        review_evidence: Sequence[Mapping[str, str]],
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillVersion:
        """Mint the immutable version a proposal submitted.

        Refuses a proposal already published, a `(skill_name, version)` already published
        (as :class:`SkillVersionConflict`, identical or not), a dependency that is not a
        published manifest of another skill, and a closure beyond the bounds. Names the
        draft, the submitted revision, the reviewing evidence and the publisher.
        """
        proposal = read_proposal(
            self.connection, workspace_id=self.workspace_id, proposal_id=proposal_id
        )
        if proposal is None:
            raise StorageError(
                f"proposal {proposal_id!r} is not recorded in this workspace"
            )
        already = self.connection.execute(
            f"SELECT manifest_id FROM {_VERSIONS} WHERE workspace_id = ? AND proposal_id = ?",
            (self.workspace_id, proposal_id),
        ).fetchone()
        if already is not None:
            raise StorageError(f"proposal {proposal_id!r} was already published")
        revision = _revision(
            self.connection,
            self.workspace_id,
            proposal.draft_id,
            proposal.draft_revision,
        )
        manifest_id = revision.manifest_id
        existing = read_skill_version_by_name(
            self.connection,
            workspace_id=self.workspace_id,
            skill_name=revision.skill_name,
            version=revision.version,
        )
        if existing is not None:
            identical = existing.manifest_id == manifest_id
            raise SkillVersionConflict(
                f"version {revision.version} of skill {revision.skill_name!r} is already "
                + (
                    "published with identical content: change the content and the version"
                    if identical
                    else "published with different content: publish changed content as a new version"
                ),
                identical=identical,
            )
        try:
            resolve_closure(
                [(manifest_id, SKILL_SELECTION_EXPLICIT)],
                role_id=None,
                load=lambda pinned: self._pinned(
                    pinned, revision.manifest, manifest_id
                ),
            )
        except SkillClosureError as error:
            raise SkillResolutionError(error.code, str(error)) from error
        version = SkillVersion(
            manifest_id=manifest_id,
            skill_name=revision.skill_name,
            version=revision.version,
            manifest=revision.manifest,
            draft_id=proposal.draft_id,
            draft_revision=proposal.draft_revision,
            proposal_id=proposal_id,
            review_evidence=_evidence(
                review_evidence, "review_evidence_refs", at_least=1
            ),
            published_by=_text(actor, _PRINCIPAL, "actor"),
            published_at_us=_time(at_us, "at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        _require_audit(self.connection, self.workspace_id, version.audit_ref)
        row = _fields(version)
        row["manifest_json"] = canonical_skill_manifest(version.manifest)
        row["review_evidence_json"] = canonicalize(list(version.review_evidence))
        for name in ("manifest", "review_evidence", "deprecation"):
            del row[name]
        _insert(self.connection, _VERSIONS, self.workspace_id, row)
        return version

    def _pinned(
        self, manifest_id: str, candidate: Mapping[str, Any], candidate_id: str
    ) -> Mapping[str, Any] | None:
        """A pinned manifest as publication sees it: the one being published, else published."""
        if manifest_id == candidate_id:
            return candidate
        stored = read_skill_version(
            self.connection, workspace_id=self.workspace_id, manifest_id=manifest_id
        )
        return None if stored is None else stored.manifest

    def deprecate_version(
        self,
        *,
        deprecation_id: str,
        manifest_id: str,
        reason: str,
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillDeprecation:
        """Mark one published version deprecated, once. The version itself is never deleted."""
        if not is_skill_manifest_id(manifest_id):
            raise StorageError("manifest_id is malformed")
        if not is_open_code(reason):
            raise StorageError("reason is malformed")
        found = read_skill_version(
            self.connection, workspace_id=self.workspace_id, manifest_id=manifest_id
        )
        if found is None:
            raise StorageError(
                f"manifest {manifest_id!r} is not published in this workspace"
            )
        if found.deprecation is not None:
            raise StorageError(f"manifest {manifest_id!r} is already deprecated")
        deprecation = SkillDeprecation(
            deprecation_id=_text(deprecation_id, _ID, "deprecation_id"),
            manifest_id=manifest_id,
            reason=reason,
            deprecated_by=_text(actor, _PRINCIPAL, "actor"),
            deprecated_at_us=_time(at_us, "at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        _require_audit(self.connection, self.workspace_id, deprecation.audit_ref)
        _insert(self.connection, _DEPRECATIONS, self.workspace_id, _fields(deprecation))
        return deprecation

    def record_install_event(
        self,
        *,
        install_event_id: str,
        manifest_id: str,
        event_kind: str,
        actor: str,
        at_us: int,
        audit_ref: str,
    ) -> SkillInstallEvent:
        """Install or remove one published version. The caller decides idempotency.

        Refuses an install past :data:`MAX_INSTALLED_PER_SKILL`, an install of a deprecated
        version, and any event that does not alternate with the one before it.
        """
        if event_kind not in {"install", "remove"}:
            raise StorageError("event_kind must be install or remove")
        found = read_skill_version(
            self.connection, workspace_id=self.workspace_id, manifest_id=manifest_id
        )
        if found is None:
            raise StorageError(
                f"manifest {manifest_id!r} is not published in this workspace"
            )
        if event_kind == "install":
            if found.deprecation is not None:
                raise StorageError(
                    f"manifest {manifest_id!r} is deprecated and cannot be installed"
                )
            installed = _installed_manifest_ids(
                self.connection,
                self.workspace_id,
                found.skill_name,
                MAX_INSTALLED_PER_SKILL + 1,
            )
            if len(installed) >= MAX_INSTALLED_PER_SKILL:
                raise StorageError(
                    f"skill {found.skill_name!r} already has {MAX_INSTALLED_PER_SKILL} "
                    "installed versions; remove one first"
                )
        previous = self.connection.execute(
            f"SELECT COALESCE(MAX(event_sequence), 0) FROM {_INSTALLS} "
            "WHERE workspace_id = ? AND manifest_id = ?",
            (self.workspace_id, manifest_id),
        ).fetchone()
        event = SkillInstallEvent(
            install_event_id=_text(install_event_id, _ID, "install_event_id"),
            manifest_id=manifest_id,
            skill_name=found.skill_name,
            event_sequence=int(previous[0]) + 1,
            event_kind=event_kind,
            actor=_text(actor, _PRINCIPAL, "actor"),
            occurred_at_us=_time(at_us, "at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        _require_audit(self.connection, self.workspace_id, event.audit_ref)
        _insert(self.connection, _INSTALLS, self.workspace_id, _fields(event))
        return event

    def bind_run(
        self,
        *,
        run_id: str,
        roles: Sequence[RoleClosure],
        bound_at_us: int,
        audit_ref: str,
        allocate_binding_id: Any,
    ) -> RunSkillBindings:
        """Bind a Run's resolved closures at its admission and seal them, in one step.

        Written only under the Run's own admission audit event and at its admission instant,
        which 0064's triggers enforce, so this is not a way to change a Run that already
        exists. `allocate_binding_id` is called once per row and names the row.
        """
        return _bind_generation(
            self.connection,
            workspace_id=self.workspace_id,
            run_id=_text(run_id, _ID, "run_id"),
            generation=1,
            roles=roles,
            bound_at_us=_time(bound_at_us, "bound_at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
            amendment_id=None,
            allocate_binding_id=allocate_binding_id,
        )


def _bind_generation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    generation: int,
    roles: Sequence[RoleClosure],
    bound_at_us: int,
    audit_ref: str,
    amendment_id: str | None,
    allocate_binding_id: Any,
) -> RunSkillBindings:
    """Write one generation's rows under one audit event and instant, then seal the set.

    Every row carries the audit event and instant that 0064's triggers require of the
    generation's admission or amendment. The caller has validated `run_id`, `audit_ref` and
    `bound_at_us`.
    """
    if not 1 <= len(roles) <= MAX_RUN_ROLES:
        raise StorageError(f"a Run binds between 1 and {MAX_RUN_ROLES} roles")
    if len({role.role_id for role in roles}) != len(roles):
        raise StorageError("a role is bound once per Run")
    bindings: list[SkillRunBinding] = []
    for role in roles:
        if not is_identifier(role.role_id) or not role.entries:
            raise StorageError("a bound role needs an identifier and a closure")
        for entry in role.entries:
            position = len(bindings) + 1
            bindings.append(
                SkillRunBinding(
                    run_binding_id=_text(allocate_binding_id(), _ID, "run_binding_id"),
                    run_id=run_id,
                    binding_generation=generation,
                    binding_position=position,
                    role_id=role.role_id,
                    manifest_id=entry.manifest_id,
                    skill_name=entry.skill_name,
                    selection=entry.selection,
                    binding_digest=_binding_digest(
                        run_id=run_id,
                        binding_generation=generation,
                        binding_position=position,
                        role_id=role.role_id,
                        manifest_id=entry.manifest_id,
                        skill_name=entry.skill_name,
                        selection=entry.selection,
                        bound_at_us=bound_at_us,
                    ),
                    bound_at_us=bound_at_us,
                    audit_ref=audit_ref,
                    version=entry.version,
                )
            )
    _require_audit(connection, workspace_id, audit_ref)
    for binding in bindings:
        row = _fields(binding)
        del row["version"]
        _insert(connection, _BINDINGS, workspace_id, row)
    set_digest = _set_digest([binding.binding_digest for binding in bindings])
    _insert(
        connection,
        _SEALS,
        workspace_id,
        {
            "run_id": run_id,
            "binding_generation": generation,
            "binding_count": len(bindings),
            "set_digest": set_digest,
            "sealed_at_us": bound_at_us,
            "audit_ref": audit_ref,
        },
    )
    return RunSkillBindings(
        run_id, tuple(bindings), set_digest, bound_at_us, generation, amendment_id
    )


def append_run_binding_generation(
    writer: ManagedSkillsWriter,
    *,
    run_id: str,
    accepted_amendment_id: str,
    roles: Sequence[RoleClosure],
    rebound_at_us: int,
    audit_ref: str,
    allocate_binding_id: Any,
) -> RunSkillBindings:
    """Open and seal the next binding generation of a sealed Run, under one accepted amendment.

    This is the registry's only way to append a generation, and it is closed on purpose. It is
    not in `__all__` and is not a method of :class:`ManagedSkillsWriter`, so no public surface
    reaches it. The one intended caller is the accepted-amendment owner (C18a), which holds the
    fenced transaction and supplies the identity of the amendment it accepted. Core has no
    public operation that amends a Run, so no current public caller exists, and Runs stay
    immutable until that owner invokes this seam.

    A Run with no sealed generation has nothing to amend and is refused, as is a Run already at
    :data:`MAX_BINDING_GENERATIONS`. The amendment row, the generation's bindings and its seal
    share one audit event and one instant, which 0064's triggers check. Resolution stays with
    the caller: `roles` are closures already resolved against what is installed now, so a
    removed or deprecated skill cannot be bound by this seam.
    """
    connection, workspace_id = writer.connection, writer.workspace_id
    run_id = _text(run_id, _ID, "run_id")
    amendment_id = _text(accepted_amendment_id, _ID, "accepted_amendment_id")
    audit_ref = _text(audit_ref, _ID, "audit_ref")
    rebound_at_us = _time(rebound_at_us, "rebound_at_us")
    sealed = connection.execute(
        f"SELECT COALESCE(MAX(binding_generation), 0) FROM {_SEALS} "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    generation = int(sealed[0]) + 1
    if generation < 2:
        raise StorageError(f"run {run_id!r} has no sealed skill bindings to amend")
    if generation > MAX_BINDING_GENERATIONS:
        raise StorageError(
            f"run {run_id!r} already holds {MAX_BINDING_GENERATIONS} binding generations"
        )
    _require_audit(connection, workspace_id, audit_ref)
    _insert(
        connection,
        _AMENDMENTS,
        workspace_id,
        {
            "amendment_id": amendment_id,
            "run_id": run_id,
            "binding_generation": generation,
            "audit_ref": audit_ref,
            "accepted_at_us": rebound_at_us,
        },
    )
    return _bind_generation(
        connection,
        workspace_id=workspace_id,
        run_id=run_id,
        generation=generation,
        roles=roles,
        bound_at_us=rebound_at_us,
        audit_ref=audit_ref,
        amendment_id=amendment_id,
        allocate_binding_id=allocate_binding_id,
    )


def transaction_local_skills_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> ManagedSkillsWriter:
    """The registry writes, for a caller that already holds a fenced transaction.

    Opens no transaction and validates no authority; 0064's triggers refuse an unguarded
    insert whichever object issued it.
    """
    return ManagedSkillsWriter(connection, workspace_id)


@contextmanager
def managed_skills_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[ManagedSkillsWriter]:
    """One fenced transaction, and the registry writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_skills_writer(connection, workspace_id=workspace_id)


# --- reads ---------------------------------------------------------------------------

_REVISION_COLUMNS: Final = (
    "draft_revision_id, draft_id, skill_name, draft_revision, version, manifest_id, "
    "manifest_json, updated_by, created_at_us, audit_ref"
)
_VERSION_COLUMNS: Final = (
    "manifest_id, skill_name, version, manifest_json, draft_id, draft_revision, proposal_id, "
    "review_evidence_json, published_by, published_at_us, audit_ref"
)


def _fields(record: object) -> dict[str, object]:
    return {name: getattr(record, name) for name in record.__slots__}  # type: ignore[attr-defined]


def _revision_from_row(row: tuple[Any, ...]) -> SkillDraftRevision:
    try:
        manifest = skill_manifest_from_canonical(row[6], row[5])
    except SkillManifestError as error:
        raise StorageError(f"draft revision {row[0]!r} is corrupt: {error}") from error
    if manifest["skill_name"] != row[2] or manifest["version"] != row[4]:
        raise StorageError(f"draft revision {row[0]!r} disagrees with its own manifest")
    return SkillDraftRevision(
        draft_revision_id=row[0],
        draft_id=row[1],
        skill_name=row[2],
        draft_revision=row[3],
        version=row[4],
        manifest_id=row[5],
        manifest=manifest,
        updated_by=row[7],
        created_at_us=row[8],
        audit_ref=row[9],
    )


def _revision(
    connection: sqlite3.Connection,
    workspace_id: str,
    draft_id: str,
    draft_revision: int,
) -> SkillDraftRevision:
    row = connection.execute(
        f"SELECT {_REVISION_COLUMNS} FROM {_REVISIONS} "
        "WHERE workspace_id = ? AND draft_id = ? AND draft_revision = ?",
        (workspace_id, draft_id, draft_revision),
    ).fetchone()
    if row is None:
        raise StorageError(f"draft {draft_id!r} has no revision {draft_revision}")
    return _revision_from_row(row)


def read_draft_head(
    connection: sqlite3.Connection, *, workspace_id: str, draft_id: str
) -> SkillDraftHead | None:
    """A draft with its latest revision and proposal, or `None` in this workspace."""
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    draft_id = _text(draft_id, _ID, "draft_id")
    row = connection.execute(
        f"SELECT draft_id, skill_name, source_work_ref, created_by, created_at_us, audit_ref "
        f"FROM {_DRAFTS} WHERE workspace_id = ? AND draft_id = ?",
        (workspace_id, draft_id),
    ).fetchone()
    if row is None:
        return None
    latest = connection.execute(
        f"SELECT {_REVISION_COLUMNS} FROM {_REVISIONS} "
        "WHERE workspace_id = ? AND draft_id = ? ORDER BY draft_revision DESC LIMIT 1",
        (workspace_id, draft_id),
    ).fetchone()
    if latest is None:
        raise StorageError(f"draft {draft_id!r} has no revision")
    proposal_row = connection.execute(
        f"SELECT proposal_id FROM {_PROPOSALS} WHERE workspace_id = ? AND draft_id = ?",
        (workspace_id, draft_id),
    ).fetchone()
    return SkillDraftHead(
        SkillDraft(*row),
        _revision_from_row(latest),
        None
        if proposal_row is None
        else read_proposal(
            connection, workspace_id=workspace_id, proposal_id=proposal_row[0]
        ),
    )


def read_proposal(
    connection: sqlite3.Connection, *, workspace_id: str, proposal_id: str
) -> SkillProposal | None:
    row = connection.execute(
        "SELECT proposal_id, draft_id, draft_revision, skill_name, evidence_json, submitted_by, "
        f"submitted_at_us, audit_ref FROM {_PROPOSALS} WHERE workspace_id = ? AND proposal_id = ?",
        (
            _text(workspace_id, _ID, "workspace_id"),
            _text(proposal_id, _ID, "proposal_id"),
        ),
    ).fetchone()
    if row is None:
        return None
    return SkillProposal(
        proposal_id=row[0],
        draft_id=row[1],
        draft_revision=row[2],
        skill_name=row[3],
        evidence=_evidence_from_text(row[4]),
        submitted_by=row[5],
        submitted_at_us=row[6],
        audit_ref=row[7],
    )


def _version_from_row(
    connection: sqlite3.Connection, workspace_id: str, row: tuple[Any, ...]
) -> SkillVersion:
    try:
        manifest = skill_manifest_from_canonical(row[3], row[0])
    except SkillManifestError as error:
        raise StorageError(f"skill version {row[0]!r} is corrupt: {error}") from error
    if manifest["skill_name"] != row[1] or manifest["version"] != row[2]:
        raise StorageError(f"skill version {row[0]!r} disagrees with its own manifest")
    deprecation = connection.execute(
        "SELECT deprecation_id, manifest_id, reason, deprecated_by, deprecated_at_us, audit_ref "
        f"FROM {_DEPRECATIONS} WHERE workspace_id = ? AND manifest_id = ?",
        (workspace_id, row[0]),
    ).fetchone()
    return SkillVersion(
        manifest_id=row[0],
        skill_name=row[1],
        version=row[2],
        manifest=manifest,
        draft_id=row[4],
        draft_revision=row[5],
        proposal_id=row[6],
        review_evidence=_evidence_from_text(row[7]),
        published_by=row[8],
        published_at_us=row[9],
        audit_ref=row[10],
        deprecation=None if deprecation is None else SkillDeprecation(*deprecation),
    )


def read_skill_version(
    connection: sqlite3.Connection, *, workspace_id: str, manifest_id: str
) -> SkillVersion | None:
    """One published version of this workspace, recomputed, or `None`."""
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    if not is_skill_manifest_id(manifest_id):
        raise StorageError("manifest_id is malformed")
    row = connection.execute(
        f"SELECT {_VERSION_COLUMNS} FROM {_VERSIONS} WHERE workspace_id = ? AND manifest_id = ?",
        (workspace_id, manifest_id),
    ).fetchone()
    return None if row is None else _version_from_row(connection, workspace_id, row)


def read_skill_version_by_name(
    connection: sqlite3.Connection, *, workspace_id: str, skill_name: str, version: str
) -> SkillVersion | None:
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    row = connection.execute(
        f"SELECT {_VERSION_COLUMNS} FROM {_VERSIONS} "
        "WHERE workspace_id = ? AND skill_name = ? AND version = ?",
        (workspace_id, _text(skill_name, _ID, "skill_name"), version),
    ).fetchone()
    return None if row is None else _version_from_row(connection, workspace_id, row)


def _installed_manifest_ids(
    connection: sqlite3.Connection, workspace_id: str, skill_name: str, limit: int
) -> list[str]:
    """The manifests of one skill whose latest install event is an install, at most `limit`."""
    rows = connection.execute(
        f"SELECT e.manifest_id FROM {_INSTALLS} e "
        "WHERE e.workspace_id = ? AND e.skill_name = ? AND e.event_kind = 'install' "
        f"AND e.event_sequence = (SELECT MAX(event_sequence) FROM {_INSTALLS} "
        "WHERE workspace_id = e.workspace_id AND manifest_id = e.manifest_id) "
        "ORDER BY e.manifest_id LIMIT ?",
        (workspace_id, skill_name, limit),
    ).fetchall()
    return [row[0] for row in rows]


def installed_state(
    connection: sqlite3.Connection, *, workspace_id: str, manifest_id: str
) -> SkillInstallEvent | None:
    """The latest install event of a manifest. It is installed when that event is an install."""
    row = connection.execute(
        "SELECT install_event_id, manifest_id, skill_name, event_sequence, event_kind, actor, "
        f"occurred_at_us, audit_ref FROM {_INSTALLS} WHERE workspace_id = ? AND manifest_id = ? "
        "ORDER BY event_sequence DESC LIMIT 1",
        (_text(workspace_id, _ID, "workspace_id"), manifest_id),
    ).fetchone()
    return None if row is None else SkillInstallEvent(*row)


def resolve_role_selection(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    role_id: str,
    requests: Sequence[tuple[str, str | None]],
) -> RoleClosure:
    """One role's selection, resolved to exact manifest ids and a bounded closure.

    `requests` is `(skill_name, manifest_id | None)` in the caller's order. Precedence, per
    skill: (1) an explicit manifest reference, which must still be published of that skill,
    installed, not deprecated and compatible with the role -- naming a manifest never widens
    what the role may use; (2) otherwise the role-compatibility filter over what is installed
    and not deprecated; (3) otherwise the highest version among those. Versions order as
    integers, and two manifests cannot share a version, so there is never a tie. A request
    that nothing satisfies refuses with a :class:`SkillResolutionError`; it is never skipped.
    The closure is then walked over the pinned dependencies, which need only be published.
    """
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    if not is_identifier(role_id):
        raise StorageError("role_id is malformed")
    if not 1 <= len(requests) <= MAX_SELECTIONS_PER_ROLE:
        raise StorageError(
            f"a role selects between 1 and {MAX_SELECTIONS_PER_ROLE} skills"
        )
    names = [name for name, _manifest in requests]
    if len(set(names)) != len(names):
        raise SkillResolutionError(CODE_DUPLICATE, "a skill is selected once per role")
    roots: list[tuple[str, str]] = []
    for skill_name, manifest_id in requests:
        if not is_identifier(skill_name):
            raise StorageError("skill_name is malformed")
        if manifest_id is not None:
            roots.append(
                (
                    _explicit(
                        connection, workspace_id, role_id, skill_name, manifest_id
                    ),
                    SKILL_SELECTION_EXPLICIT,
                )
            )
        else:
            roots.append(
                (
                    _highest_compatible(connection, workspace_id, role_id, skill_name),
                    SKILL_SELECTION_HIGHEST_COMPATIBLE,
                )
            )

    def load(pinned: str) -> Mapping[str, Any] | None:
        stored = read_skill_version(
            connection, workspace_id=workspace_id, manifest_id=pinned
        )
        return None if stored is None else stored.manifest

    try:
        entries = resolve_closure(roots, role_id=role_id, load=load)
    except SkillClosureError as error:
        raise SkillResolutionError(error.code, str(error)) from error
    return RoleClosure(role_id, entries)


def _explicit(
    connection: sqlite3.Connection,
    workspace_id: str,
    role_id: str,
    skill_name: str,
    manifest_id: str,
) -> str:
    if not is_skill_manifest_id(manifest_id):
        raise StorageError("manifest_id is malformed")
    found = read_skill_version(
        connection, workspace_id=workspace_id, manifest_id=manifest_id
    )
    if found is None:
        raise SkillResolutionError(
            CODE_NOT_FOUND, "that manifest is not published in this workspace"
        )
    if found.skill_name != skill_name:
        raise SkillResolutionError(
            CODE_MISMATCH,
            f"that manifest is a version of {found.skill_name!r}, not {skill_name!r}",
        )
    if found.deprecation is not None:
        raise SkillResolutionError(
            CODE_DEPRECATED, "that manifest is deprecated and is not selected"
        )
    event = installed_state(
        connection, workspace_id=workspace_id, manifest_id=manifest_id
    )
    if event is None or event.event_kind != "install":
        raise SkillResolutionError(
            CODE_NOT_INSTALLED, "that manifest is not installed in this workspace"
        )
    if role_id not in found.manifest["compatible_roles"]:
        raise SkillResolutionError(
            CODE_ROLE_INCOMPATIBLE,
            f"skill {skill_name!r} is not compatible with the role",
        )
    return manifest_id


def _highest_compatible(
    connection: sqlite3.Connection, workspace_id: str, role_id: str, skill_name: str
) -> str:
    best: tuple[tuple[int, int, int], str] | None = None
    for manifest_id in _installed_manifest_ids(
        connection, workspace_id, skill_name, MAX_INSTALLED_PER_SKILL
    ):
        found = read_skill_version(
            connection, workspace_id=workspace_id, manifest_id=manifest_id
        )
        if found is None or found.deprecation is not None:
            continue
        if role_id not in found.manifest["compatible_roles"]:
            continue
        key = skill_version_key(found.version)
        if best is None or key > best[0]:
            best = (key, manifest_id)
    if best is None:
        raise SkillResolutionError(
            CODE_NO_COMPATIBLE,
            f"no installed, non-deprecated version of skill {skill_name!r} is compatible with the role",
        )
    return best[1]


def read_run_skill_binding_generations(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[int, ...]:
    """The sealed binding generations of a Run, oldest first. Empty when it bound no skills."""
    rows = connection.execute(
        f"SELECT binding_generation FROM {_SEALS} WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY binding_generation LIMIT ?",
        (
            _text(workspace_id, _ID, "workspace_id"),
            _text(run_id, _ID, "run_id"),
            MAX_BINDING_GENERATIONS,
        ),
    ).fetchall()
    return tuple(row[0] for row in rows)


def read_run_skill_bindings(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    binding_generation: int | None = None,
) -> RunSkillBindings | None:
    """One sealed generation of what a Run is bound to, verified, or `None` when it bound none.

    The latest sealed generation unless `binding_generation` names another, so an earlier
    generation stays readable after an amendment. Never consults installation or deprecation: a
    Run keeps what it was bound with after the skill is removed or deprecated. Every row's
    digest and the seal over the set are recomputed. Bindings with no seal, a seal over a
    different set, or a later generation that no amendment opened are each refused.
    """
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    run_id = _text(run_id, _ID, "run_id")
    if binding_generation is None:
        latest = connection.execute(
            f"SELECT MAX(binding_generation) FROM {_SEALS} WHERE workspace_id = ? AND run_id = ?",
            (workspace_id, run_id),
        ).fetchone()
        generation = latest[0]
    else:
        if not 1 <= binding_generation <= MAX_BINDING_GENERATIONS:
            raise StorageError("binding_generation is out of range")
        generation = binding_generation
    if generation is None:
        unsealed = connection.execute(
            f"SELECT 1 FROM {_BINDINGS} WHERE workspace_id = ? AND run_id = ? LIMIT 1",
            (workspace_id, run_id),
        ).fetchone()
        if unsealed is not None:
            raise StorageError(f"run {run_id!r} has skill bindings that were never sealed")
        return None
    seal = connection.execute(
        f"SELECT binding_count, set_digest, sealed_at_us, audit_ref FROM {_SEALS} "
        "WHERE workspace_id = ? AND run_id = ? AND binding_generation = ?",
        (workspace_id, run_id, generation),
    ).fetchone()
    rows = connection.execute(
        "SELECT b.run_binding_id, b.run_id, b.binding_generation, b.binding_position, "
        "b.role_id, b.manifest_id, b.skill_name, b.selection, b.binding_digest, "
        "b.bound_at_us, b.audit_ref, v.version "
        f"FROM {_BINDINGS} b JOIN {_VERSIONS} v "
        "ON v.workspace_id = b.workspace_id AND v.manifest_id = b.manifest_id "
        "WHERE b.workspace_id = ? AND b.run_id = ? AND b.binding_generation = ? "
        "ORDER BY b.binding_position LIMIT 1025",
        (workspace_id, run_id, generation),
    ).fetchall()
    if seal is None and not rows:
        return None
    if seal is None:
        raise StorageError(f"run {run_id!r} has skill bindings that were never sealed")
    bindings = tuple(SkillRunBinding(*row) for row in rows)
    if len(bindings) != seal[0] or [b.binding_position for b in bindings] != list(
        range(1, seal[0] + 1)
    ):
        raise StorageError(f"run {run_id!r} skill bindings do not match their seal")
    for binding in bindings:
        expected = _binding_digest(
            run_id=binding.run_id,
            binding_generation=binding.binding_generation,
            binding_position=binding.binding_position,
            role_id=binding.role_id,
            manifest_id=binding.manifest_id,
            skill_name=binding.skill_name,
            selection=binding.selection,
            bound_at_us=binding.bound_at_us,
        )
        if expected != binding.binding_digest:
            raise StorageError(
                f"run {run_id!r} skill binding {binding.run_binding_id!r} is tampered"
            )
    if _set_digest([binding.binding_digest for binding in bindings]) != seal[1]:
        raise StorageError(f"run {run_id!r} skill binding set is tampered")
    amendment_id: str | None = None
    if generation > 1:
        amended = connection.execute(
            f"SELECT amendment_id, accepted_at_us, audit_ref FROM {_AMENDMENTS} "
            "WHERE workspace_id = ? AND run_id = ? AND binding_generation = ?",
            (workspace_id, run_id, generation),
        ).fetchone()
        if amended is None or amended[1] != seal[2] or amended[2] != seal[3]:
            raise StorageError(
                f"run {run_id!r} binding generation {generation} was not opened by an amendment"
            )
        amendment_id = amended[0]
    return RunSkillBindings(
        run_id, bindings, seal[1], seal[2], generation, amendment_id
    )
