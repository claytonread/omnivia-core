"""Engineering repository identity (SPEC-CORE-ENGMEM-001, plan PR-C; spec §6).

The stable logical identity layer: repositories, their installation-local
checkouts, and their immutable snapshots (migration 0047). Every write here
runs inside the fenced mutation transaction the mutation coordinator opens, as
in the other engineering modules; the guard triggers of migration 0047 make an
unguarded write impossible.

Resolution rules (§6.2), enforced here rather than by callers:

- an explicit `repository_id` resolves when it exists and is fail-closed
  (`RepositoryNotFound`) when it does not;
- a label alone resolves only when exactly one registered repository carries
  it — two registrations sharing a basename stay distinct by design, so a
  label-only match raises `RepositoryAmbiguous` rather than guessing;
- a checkout hint resolves only within one installation's own mapping, and a
  moved checkout re-points the mapping under audit without changing the
  logical repository identity;
- a snapshot reference resolves through its repository and never inherits
  applicability from its base commit.

Groundwork boundary (spec §24.4): no catalogue operation registers any of this
yet — registration is exercised through the storage module and validated
against continuity references — so this slice is groundwork, not a shipped
feature, until the registration surface is ratified (spec §16.3).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core_runtime.storage.decisions import canonical_document

_REPOSITORIES_TABLE: Final = "omnivia_engineering_repositories"
_CHECKOUTS_TABLE: Final = "omnivia_engineering_checkouts"
_SNAPSHOTS_TABLE: Final = "omnivia_engineering_snapshots"


class RepositoryNotFound(LookupError):
    """The named repository is not registered in this workspace."""


class RepositoryAmbiguous(RuntimeError):
    """A label-only resolution matched more than one registered repository."""

    def __init__(self, label: str, matches: int) -> None:
        super().__init__(
            f"{matches} registered repositories carry the label {label!r}; "
            "resolve by repository id after explicit selection"
        )
        self.label = label
        self.matches = matches


class SnapshotNotFound(LookupError):
    """The named snapshot is not registered for its repository."""


def register_repository(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    repository_id: str,
    display_name: str,
    provider_hint: str | None,
    registered_at_us: int,
) -> None:
    connection.execute(
        f"INSERT INTO {_REPOSITORIES_TABLE} "
        "(workspace_id, repository_id, display_name, provider_hint, "
        "registered_at_us, audit_ref) VALUES (?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            repository_id,
            display_name,
            provider_hint,
            int(registered_at_us),
            settlement.audit_ref,
        ),
    )


def register_checkout(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    checkout_id: str,
    repository_id: str,
    installation_id: str,
    checkout_hint: str,
    registered_at_us: int,
) -> None:
    """Register one installation-local materialisation of a repository.

    The UNIQUE (workspace, installation, hint) key is the identity of the
    mapping: re-registering the same path on the same installation re-points the
    existing row's repository under a fresh audit event (a moved or re-cloned
    checkout), which is exactly the audited-revision behaviour §6.2 requires.
    """
    existing = connection.execute(
        f"SELECT checkout_id FROM {_CHECKOUTS_TABLE} "
        "WHERE workspace_id = ? AND installation_id = ? AND checkout_hint = ?",
        (workspace_id, installation_id, checkout_hint),
    ).fetchone()
    if existing is not None:
        connection.execute(
            f"UPDATE {_CHECKOUTS_TABLE} SET repository_id = ?, audit_ref = ?, "
            "last_seen_at_us = ? WHERE workspace_id = ? AND checkout_id = ?",
            (
                repository_id,
                settlement.audit_ref,
                int(registered_at_us),
                workspace_id,
                existing[0],
            ),
        )
        return
    connection.execute(
        f"INSERT INTO {_CHECKOUTS_TABLE} "
        "(workspace_id, checkout_id, repository_id, installation_id, checkout_hint, "
        "registered_at_us, last_seen_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            checkout_id,
            repository_id,
            installation_id,
            checkout_hint,
            int(registered_at_us),
            int(registered_at_us),
            settlement.audit_ref,
        ),
    )


def record_snapshot(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    snapshot_id: str,
    repository_id: str,
    snapshot_kind: str,
    manifest: Mapping[str, Any],
    base_commit: str | None,
    capture_status: str,
    captured_at_us: int,
) -> str:
    manifest_json = canonical_document(dict(manifest))
    connection.execute(
        f"INSERT INTO {_SNAPSHOTS_TABLE} "
        "(workspace_id, snapshot_id, repository_id, snapshot_kind, manifest_digest, "
        "base_commit, capture_status, captured_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            snapshot_id,
            repository_id,
            snapshot_kind,
            _digest(manifest_json),
            base_commit,
            capture_status,
            int(captured_at_us),
            settlement.audit_ref,
        ),
    )
    return _digest(manifest_json)


def _digest(document: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def resolve_repository(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str | None = None,
    label: str | None = None,
    checkout_hint: str | None = None,
    installation_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one repository, fail-closed, in the precedence order of §6.2.

    Explicit id first; then a checkout hint scoped to one installation's own
    mapping; then a label, which must match exactly one registration. A
    label-only match against several registrations raises
    `RepositoryAmbiguous` — two unrelated repositories sharing a basename stay
    distinct, and nothing here picks one silently.
    """
    if repository_id is not None:
        row = connection.execute(
            f"SELECT repository_id, display_name, provider_hint, registered_at_us "
            f"FROM {_REPOSITORIES_TABLE} "
            "WHERE workspace_id = ? AND repository_id = ?",
            (workspace_id, repository_id),
        ).fetchone()
        return None if row is None else _repository_row(row)
    if checkout_hint is not None:
        if installation_id is None:
            return None
        row = connection.execute(
            f"SELECT r.repository_id, r.display_name, r.provider_hint, "
            f"r.registered_at_us FROM {_REPOSITORIES_TABLE} r "
            f"JOIN {_CHECKOUTS_TABLE} c ON c.workspace_id = r.workspace_id "
            "AND c.repository_id = r.repository_id "
            "WHERE r.workspace_id = ? AND c.installation_id = ? AND c.checkout_hint = ?",
            (workspace_id, installation_id, checkout_hint),
        ).fetchone()
        return None if row is None else _repository_row(row)
    if label is not None:
        rows = connection.execute(
            f"SELECT repository_id, display_name, provider_hint, registered_at_us "
            f"FROM {_REPOSITORIES_TABLE} WHERE workspace_id = ? AND display_name = ?",
            (workspace_id, label),
        ).fetchall()
        if len(rows) > 1:
            raise RepositoryAmbiguous(label, len(rows))
        return None if not rows else _repository_row(rows[0])
    return None


def _repository_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "repository_id": row[0],
        "display_name": row[1],
        "provider_hint": row[2],
        "registered_at_us": row[3],
    }


def validate_snapshot_ref(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str | None,
    snapshot_id: str | None,
) -> None:
    """Fail-closed validation of one continuity snapshot reference (§6.3).

    A stated repository id must be registered; a stated snapshot id must exist
    for its repository. Nothing here grants access or applicability: a snapshot
    row is identity and capture status, nothing more.
    """
    if repository_id is not None:
        repository = resolve_repository(
            connection, workspace_id=workspace_id, repository_id=repository_id
        )
        if repository is None:
            raise RepositoryNotFound(repository_id)
    if snapshot_id is not None:
        row = connection.execute(
            f"SELECT repository_id FROM {_SNAPSHOTS_TABLE} "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (workspace_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(snapshot_id)
        if repository_id is not None and row[0] != repository_id:
            raise SnapshotNotFound(snapshot_id)
