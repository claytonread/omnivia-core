"""Tests for Workspace selection -- the "current Workspace" authority.

The load-bearing assertion in this file is the identity binding: a
``workspaceRef`` is the **portable** ``WorkspaceManifest.workspace_id``, never
the local ``Workspace`` record. ``workspace/manifest.py`` states outright that
the two models must not be mixed, and the consequence of mixing them is not
cosmetic: the local record's identity travels with ``root_path`` and
``storage_path``, which the portable manifest forbids precisely because they
describe the machine. An authority reference decided by machine-local state
would mean a handle's validity depended on where a Workspace happened to sit.

The rest of the file pins the switch verb itself, and then the end-to-end path
through the issuer -- a switch really does rotate the ``ShellContext`` and
really does invalidate handles bound to the superseded one.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.session.context import ContextIssuer
from omnivia_core.workspace import lifecycle
from omnivia_core.workspace import manifest as manifest_module
from omnivia_core.workspace.models import WorkspaceCreate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHELL_CONTEXT = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures" / "valid" / "shell-context.json"

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def portable_manifest(workspace_id: str = "workspace-001") -> manifest_module.WorkspaceManifest:
    return manifest_module.WorkspaceManifest(
        workspace_id=workspace_id,
        created_at=NOW.isoformat(),
        compatibility=manifest_module.CoreCompatibility(
            workspace_format_version="1",
            min_core_version="0.1.0",
        ),
        name="Example workspace",
    )


def shell_context(**overrides: Any) -> gen.ShellContext:
    document = json.loads(SHELL_CONTEXT.read_text(encoding="utf-8"))
    document.update(overrides)
    return gen.ShellContext.from_wire(document)


# --------------------------------------------------------------------------
# The identity binding
# --------------------------------------------------------------------------


def test_workspace_ref_is_the_portable_manifest_identity() -> None:
    assert lifecycle.workspace_ref(portable_manifest()) == "workspace-001"


def test_the_local_workspace_record_is_not_accepted_as_an_authority_identity() -> None:
    """The two models must not be mixed (workspace/manifest.py:1-14). A local
    record is refused outright rather than having its ``id`` read, so there is no
    path by which a machine-local record silently becomes a ``workspaceRef``."""
    local = WorkspaceCreate(name="Local", root_path=Path("/tmp/example")).to_workspace()
    with pytest.raises(lifecycle.WorkspaceLifecycleError, match="portable"):
        lifecycle.workspace_ref(local)  # type: ignore[arg-type]


def test_the_authority_identity_carries_none_of_the_machine_local_fields() -> None:
    """Stated as a property of the model rather than of one instance: nothing a
    ``workspaceRef`` is derived from may be a non-portable key, and the local
    record demonstrably holds two of them."""
    manifest_fields = {field.name for field in fields(manifest_module.WorkspaceManifest)}
    assert manifest_fields & manifest_module.NON_PORTABLE_KEYS == set()

    local_fields = {
        field.name
        for field in fields(WorkspaceCreate(name="L", root_path=Path("/tmp/x")).to_workspace())
    }
    assert {"root_path", "storage_path"} <= local_fields
    assert local_fields & manifest_module.NON_PORTABLE_KEYS == {"root_path", "storage_path"}


def test_an_identity_the_host_contract_would_refuse_is_refused_here() -> None:
    with pytest.raises(lifecycle.WorkspaceLifecycleError):
        lifecycle.workspace_ref(portable_manifest(workspace_id="not a valid ref"))


def test_a_valid_workspace_ref_is_admissible_in_a_shell_context() -> None:
    """The point of the pattern check: what this module mints must be usable
    where the frozen contract expects an Identifier."""
    reference = lifecycle.workspace_ref(portable_manifest(workspace_id="workspace-002"))
    shell_context(contextId="ctx-002", contextVersion=2, workspaceRef=reference).validate()


def test_workspace_ref_pattern_matches_the_host_contract_identifier_pattern() -> None:
    assert lifecycle.WORKSPACE_REF_PATTERN == gen.IDENTIFIER_PATTERN


# --------------------------------------------------------------------------
# The switch verb
# --------------------------------------------------------------------------


def test_selecting_from_nothing_produces_a_selection() -> None:
    selection = lifecycle.select(portable_manifest(), now=NOW)
    assert selection.workspace_ref == "workspace-001"
    assert selection.selected_at == NOW.isoformat()


def test_switching_produces_a_new_selection_and_leaves_the_old_one_alone() -> None:
    first = lifecycle.select(portable_manifest("workspace-001"), now=NOW)
    second = lifecycle.select(portable_manifest("workspace-002"), now=LATER, current=first)
    assert second.workspace_ref == "workspace-002"
    assert second.selected_at == LATER.isoformat()
    assert first.workspace_ref == "workspace-001"


def test_reselecting_the_current_workspace_returns_the_same_selection() -> None:
    """A no-op switch must not mint a new selection: the caller rotates the
    ``ShellContext`` when the selection changes, and rotating for nothing would
    invalidate every live handle."""
    first = lifecycle.select(portable_manifest(), now=NOW)
    again = lifecycle.select(portable_manifest(), now=LATER, current=first)
    assert again is first


def test_a_switch_requires_a_time_zone_aware_instant() -> None:
    with pytest.raises(lifecycle.WorkspaceLifecycleError, match="time-zone-aware"):
        lifecycle.select(portable_manifest(), now=NOW.replace(tzinfo=None))


def test_a_selection_cannot_be_constructed_in_an_invalid_state() -> None:
    with pytest.raises(lifecycle.WorkspaceLifecycleError):
        lifecycle.WorkspaceSelection(workspace_ref="workspace-001", selected_at="2026-08-04")
    with pytest.raises(lifecycle.WorkspaceLifecycleError):
        lifecycle.WorkspaceSelection(workspace_ref="not a ref", selected_at=NOW.isoformat())
    with pytest.raises(lifecycle.WorkspaceLifecycleError, match="time zone"):
        lifecycle.WorkspaceSelection(
            workspace_ref="workspace-001", selected_at="2026-08-04T12:00:00"
        )


def test_is_selected_answers_against_the_portable_identity() -> None:
    selection = lifecycle.select(portable_manifest("workspace-001"), now=NOW)
    assert lifecycle.is_selected(selection, portable_manifest("workspace-001")) is True
    assert lifecycle.is_selected(selection, portable_manifest("workspace-002")) is False
    assert lifecycle.is_selected(None, portable_manifest("workspace-001")) is False


# --------------------------------------------------------------------------
# End to end: selection -> rotation -> handle invalidation
# --------------------------------------------------------------------------


def test_a_workspace_switch_rotates_the_context_and_invalidates_old_handles() -> None:
    issuer = ContextIssuer(current=shell_context())
    assert issuer.handle_is_valid("ctx-001") is True

    selection = lifecycle.select(portable_manifest("workspace-002"), now=NOW)
    rotated = issuer.switch_workspace(selection.workspace_ref, issued_at=selection.selected_at)

    assert rotated.current.workspace_ref == "workspace-002"
    assert rotated.changed_since(issuer.current) == ("workspaceRef",)
    assert rotated.handle_is_valid("ctx-001") is False
    assert rotated.handle_is_valid(rotated.current.context_id) is True


def test_a_no_op_switch_end_to_end_leaves_live_handles_alone() -> None:
    issuer = ContextIssuer(current=shell_context())
    selection = lifecycle.select(portable_manifest("workspace-001"), now=NOW)
    unchanged = issuer.switch_workspace(selection.workspace_ref, issued_at=selection.selected_at)

    assert unchanged is issuer
    assert unchanged.handle_is_valid("ctx-001") is True


def test_the_switch_never_touches_the_organisation_reference() -> None:
    issuer = ContextIssuer(current=shell_context())
    rotated = issuer.switch_workspace("workspace-002", issued_at=NOW.isoformat())
    assert rotated.current.organisation_ref == issuer.current.organisation_ref


def test_the_lifecycle_surface_exposes_no_organisation_verb() -> None:
    """Workspace-only scope, asserted: this module owns the only switch in the
    lane, and it is a Workspace switch."""
    assert all("organisation" not in name.lower() for name in lifecycle.__all__)


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_the_lifecycle_module_performs_no_filesystem_access() -> None:
    """Selection is decided from declared values alone, matching
    ``workspace/compatibility.py``: an authority decision that needs the disk
    cannot be made before the Workspace is open."""
    source = (
        REPO_ROOT / "src" / "omnivia_core" / "workspace" / "lifecycle.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("import os", "import pathlib", "from pathlib", "open("):
        assert forbidden not in source, f"lifecycle.py must not reference {forbidden!r}"
