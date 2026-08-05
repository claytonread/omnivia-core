"""Workspace selection: the "current Workspace" authority Core owns.

Which Workspace identity ``workspaceRef`` is
--------------------------------------------

``ShellContext.workspaceRef`` is the **portable** ``WorkspaceManifest.workspace_id``
from :mod:`omnivia_core.workspace.manifest`. It is never the local
:class:`omnivia_core.workspace.models.Workspace` record.

That choice is forced, not stylistic. ``workspaceRef`` is a governed rotation
trigger: it is the value a handle's continued validity is decided against, and
it has to mean the same Workspace after the Workspace is copied to another
machine, or a rotation on one machine says nothing on the other. The manifest's
``workspace_id`` is defined to be exactly that -- identity independent of where
the Workspace sits on disk. The local ``Workspace`` record is the opposite kind
of thing: it carries ``root_path`` and ``storage_path``, both of which
``manifest.NON_PORTABLE_KEYS`` forbids from a portable document precisely
because they describe the machine rather than the Workspace. Binding an
authority reference to a record whose identity travels with machine-local paths
would let installation state decide who may hold a handle.

``manifest.py`` states outright that the two models must not be mixed, so this
module takes only a :class:`WorkspaceManifest`. A caller holding the local
record resolves its manifest first; that resolution is a filesystem concern and
does not belong in a pure module.

What this module adds
---------------------

``workspace/models.py`` has create, update and index-status verbs but no notion
of a *selected* Workspace, so nothing in Core could previously say which
Workspace an installation is acting in. :class:`WorkspaceSelection` is that
missing authority, and :func:`select` is the switch verb. Selection is a value,
not a mutation: switching produces a new selection exactly as rotating produces
a new ``ShellContext``, and for the same reason -- an authority record that can
be edited in place has no moment at which the old authority stopped.

Nothing here touches the Host Contract. :func:`select` decides *what* the next
``workspaceRef`` is; ``omnivia_core.session.context.ContextIssuer`` is what
turns that into a rotation, and the frozen codec is what decides whether the
rotation is lawful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from omnivia_core.workspace.manifest import WorkspaceManifest

#: The shape a ``workspaceRef`` must satisfy to be admissible where the Host
#: Contract expects an ``Identifier``. A manifest's ``workspace_id`` is normally
#: a UUID and satisfies this trivially; the check exists so a Workspace carrying
#: a hand-edited identity cannot become an authority reference by accident.
WORKSPACE_REF_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

_REF_RE: Final[re.Pattern[str]] = re.compile(WORKSPACE_REF_PATTERN)


class WorkspaceLifecycleError(Exception):
    """Raised when a Workspace selection would carry authority it cannot back."""


@dataclass(frozen=True)
class WorkspaceSelection:
    """The Workspace an installation is currently acting in.

    ``selected_at`` records when this selection began, which is what makes a
    sequence of switches reconstructable after the fact. It is supplied by the
    caller rather than read from a clock here, so a selection can be replayed.
    """

    workspace_ref: str
    selected_at: str

    def __post_init__(self) -> None:
        if type(self.workspace_ref) is not str or _REF_RE.fullmatch(self.workspace_ref) is None:
            raise WorkspaceLifecycleError("workspace_ref must be a valid authority reference")
        if type(self.selected_at) is not str:
            raise WorkspaceLifecycleError("selected_at must be an RFC 3339 timestamp")
        _parse_instant(self.selected_at)


def workspace_ref(manifest: WorkspaceManifest) -> str:
    """Return the authority reference for ``manifest``'s Workspace.

    The single place the ``WorkspaceManifest`` -> ``workspaceRef`` binding is
    made. Callers go through it rather than reading ``workspace_id`` directly,
    so the decision recorded in this module's docstring has exactly one
    implementation to change if it is ever revisited.
    """
    if type(manifest) is not WorkspaceManifest:
        raise WorkspaceLifecycleError("workspace_ref requires a portable WorkspaceManifest")
    reference = manifest.workspace_id
    if type(reference) is not str or _REF_RE.fullmatch(reference) is None:
        raise WorkspaceLifecycleError("workspace_id is not a valid authority reference")
    return reference


def select(
    manifest: WorkspaceManifest,
    *,
    now: datetime,
    current: WorkspaceSelection | None = None,
) -> WorkspaceSelection:
    """Return the selection that results from switching to ``manifest``.

    Re-selecting the Workspace that is already selected returns ``current``
    unchanged rather than a fresh record. That is not an optimisation: a new
    selection is what makes the caller rotate the ``ShellContext``, and
    rotating on a no-op switch would invalidate every live handle for nothing.

    ``now`` must be time-zone aware, for the same reason the Host Contract's
    authority checks require it: a floating local clock makes the record's own
    timeline unreproducible.
    """
    reference = workspace_ref(manifest)
    if type(now) is not datetime or now.utcoffset() is None:
        raise WorkspaceLifecycleError("a Workspace switch requires a time-zone-aware instant")
    if current is not None:
        if type(current) is not WorkspaceSelection:
            raise WorkspaceLifecycleError("current must be a WorkspaceSelection")
        if current.workspace_ref == reference:
            return current
    return WorkspaceSelection(workspace_ref=reference, selected_at=_format_instant(now))


def is_selected(selection: WorkspaceSelection | None, manifest: WorkspaceManifest) -> bool:
    """Whether ``manifest``'s Workspace is the one currently selected."""
    if selection is None:
        return False
    if type(selection) is not WorkspaceSelection:
        raise WorkspaceLifecycleError("is_selected requires a WorkspaceSelection")
    return selection.workspace_ref == workspace_ref(manifest)


def _parse_instant(value: str) -> datetime:
    text = f"{value[:-1]}+00:00" if value[-1:] in ("Z", "z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise WorkspaceLifecycleError("selected_at must be an RFC 3339 timestamp") from None
    if parsed.utcoffset() is None:
        raise WorkspaceLifecycleError("selected_at must carry a time zone")
    return parsed


def _format_instant(now: datetime) -> str:
    """Render an instant in the spelling the Host Contract's ``date-time`` accepts."""
    return now.isoformat()


__all__ = [
    "WORKSPACE_REF_PATTERN",
    "WorkspaceLifecycleError",
    "WorkspaceSelection",
    "is_selected",
    "select",
    "workspace_ref",
]
