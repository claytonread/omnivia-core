"""Strict-mypy consumer fixture for the workspace models compatibility facade.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for ``omnivia_memory.workspace.models``: it imports all five
routed symbols from that leaf (never from ``omnivia_core``) and proves the
facade re-exports usefully typed objects -- the exact canonical types, not
``Any`` -- via ``typing.assert_type``.

The workspace domain gets its own fixture rather than joining
``accepted_legacy_facade_consumer.py`` because its barrel stays a *hybrid*: the
leaf's five routed names are the whole portable half of
``omnivia_memory.workspace``, while ``WorkspaceRepository`` and
``WorkspaceService`` are runtime-owned and never enter Core. A consumer that
reaches the routed surface through the leaf's own path is what proves that split
is invisible to a typed caller. The consumer partition is enforced by
``tests/test_typed_facade_consumers.py``.

``WorkspaceCreate`` is the sharpest case here: its ``root_path`` and
``storage_path`` are ``Path``/``Path | None`` while the ``Workspace`` they build
stores both as ``str``. A facade that degraded either side to ``Any`` -- or that
re-exported a lookalike whose fields were all strings -- would still import
cleanly. ``assert_type`` on both sides of ``to_workspace`` is what rejects that.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If the facade ever stopped
explicitly re-exporting these names or degraded them to ``Any``, strict mypy
would fail here.
"""

from pathlib import Path
from typing import Any, assert_type

from omnivia_memory.workspace.models import (
    ImportSummary,
    Workspace,
    WorkspaceCreate,
    WorkspaceIndexStatus,
    WorkspaceUpdate,
)


def build_workspace() -> Workspace:
    """Construct a workspace record through the facade's own types."""
    workspace = Workspace(
        name="My Workspace",
        root_path="/workspace",
        storage_path="/workspace/.omnivia",
        id="workspace-1",
        description=None,
        index_status=WorkspaceIndexStatus.UNINDEXED,
        settings={"theme": "dark"},
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        last_indexed_at=None,
    )
    assert_type(workspace.name, str)
    # The record stores both paths as strings; only the create/update inputs
    # below are ``Path``-typed.
    assert_type(workspace.root_path, str)
    assert_type(workspace.storage_path, str)
    assert_type(workspace.id, str)
    assert_type(workspace.description, str | None)
    assert_type(workspace.index_status, WorkspaceIndexStatus)
    assert_type(workspace.settings, dict[str, Any])
    assert_type(workspace.created_at, str)
    assert_type(workspace.updated_at, str)
    assert_type(workspace.last_indexed_at, str | None)
    assert_type(workspace.to_dict(), dict[str, Any])
    assert_type(Workspace.from_dict(workspace.to_dict()), Workspace)
    assert_type(workspace.touch(), None)
    assert_type(workspace.mark_indexed(), None)
    assert_type(workspace.mark_error(), None)
    return workspace


def create_input(root: Path) -> WorkspaceCreate:
    """The creation input: ``Path``-typed on both of its filesystem fields."""
    create = WorkspaceCreate(
        name="My Workspace",
        root_path=root,
        storage_path=root / ".omnivia",
        description="notes",
        settings={"theme": "dark"},
    )
    assert_type(create.name, str)
    assert_type(create.root_path, Path)
    assert_type(create.storage_path, Path | None)
    assert_type(create.description, str | None)
    assert_type(create.settings, dict[str, Any])
    return create


def created_workspace(root: Path) -> Workspace:
    """``to_workspace`` crosses the ``Path`` -> ``str`` boundary exactly once.

    Annotating the result as ``Workspace`` while the input's fields are ``Path``
    is what a facade that collapsed either type into ``Any`` would fail.
    """
    workspace = create_input(root).to_workspace()
    assert_type(workspace, Workspace)
    assert_type(workspace.root_path, str)
    assert_type(workspace.storage_path, str)
    assert_type(workspace.index_status, WorkspaceIndexStatus)
    # ``storage_path`` is optional on the input and always populated on the
    # record, so the default branch has to type-check too.
    defaulted = WorkspaceCreate(name="Other", root_path=root).to_workspace()
    assert_type(defaulted.storage_path, str)
    return workspace


def apply_update(workspace: Workspace, root: Path) -> bool:
    """Every ``WorkspaceUpdate`` field is optional, and ``apply_to`` returns ``bool``."""
    update = WorkspaceUpdate(
        name="Renamed",
        root_path=root,
        storage_path=None,
        description=None,
        index_status=WorkspaceIndexStatus.STALE,
        settings={"theme": "light"},
    )
    assert_type(update.name, str | None)
    assert_type(update.root_path, Path | None)
    assert_type(update.storage_path, Path | None)
    assert_type(update.description, str | None)
    assert_type(update.index_status, WorkspaceIndexStatus | None)
    assert_type(update.settings, dict[str, Any] | None)
    changed = update.apply_to(workspace)
    assert_type(changed, bool)
    # The empty update is the no-op branch, and is still a ``bool``.
    assert_type(WorkspaceUpdate().apply_to(workspace), bool)
    return changed


def index_states() -> tuple[str, WorkspaceIndexStatus]:
    """The enumeration keeps its ``value`` accessor and its members.

    ``WorkspaceIndexStatus`` subclasses ``str``, so a member is usable wherever a
    ``str`` is -- which a facade that degraded it to a plain ``Enum`` would lose.
    """
    status = WorkspaceIndexStatus.INDEXING
    assert_type(status.value, str)
    assert_type(status, WorkspaceIndexStatus)
    widened: str = WorkspaceIndexStatus.ERROR
    assert_type(widened, str)
    return widened, status


def summarize_import(workspace: Workspace) -> ImportSummary:
    """The import summary: four counters and a list of error strings."""
    summary = ImportSummary(
        workspace_id=workspace.id,
        files_seen=3,
        sources_created=2,
        memories_created=5,
        errors=["boom"],
    )
    assert_type(summary.workspace_id, str)
    assert_type(summary.files_seen, int)
    assert_type(summary.sources_created, int)
    assert_type(summary.memories_created, int)
    assert_type(summary.errors, list[str])
    # ``errors`` defaults to an empty list rather than being optional.
    assert_type(
        ImportSummary(
            workspace_id=workspace.id,
            files_seen=0,
            sources_created=0,
            memories_created=0,
        ).errors,
        list[str],
    )
    return summary


def summarize(root: Path) -> int:
    """One end-to-end pass through every routed name."""
    build_workspace()
    workspace = created_workspace(root)
    apply_update(workspace, root)
    index_states()
    summary = summarize_import(workspace)
    total = summary.files_seen + summary.sources_created + summary.memories_created
    total += len(summary.errors) + len(workspace.settings)
    assert_type(total, int)
    return total
