"""The `workspace.inspect` handler -- the first application handler (V06-2 Lane A).

This is the whole of what a catalogue handler is allowed to be. It receives an
`OperationContext` the production application path built from an
`AuthorizedApplicationContext`, and it reads *nothing* else: no request field, no
caller-supplied path, no storage handle of its own, and no second opinion about
authority. Whatever it can see, the authorization seam already decided it may see.

Two properties follow from that and are worth stating because a later handler will
be tempted out of both.

**The workspace is the authorized one, never the requested one.** The workspace id
used below is `context.workspace_id`, which the seam took from the session's grant
and the endpoint binding after refusing every workspace they did not agree on. The
request's own `workspace_id` field is not read here, and `WorkspaceInspectInput` is
`{}` by contract, so there is no second, independent workspace identifier anywhere
on this path.

**Nothing is opened.** The facts come from `manifest_store.inspect_workspace`, which
is a zero-write inspection: it reads the manifest and validates the layout, and it
takes no lock, no lease and no database connection. Authoritative storage stays with
the service that owns it, which is the property the CLI-side boundary guards protect
from the other end.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    VersionWindow,
    WorkspaceCompatibility,
    WorkspaceDescriptor,
    WorkspaceInspectResult,
    classify_version_compatibility,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.service.versions import (
    SUPPORTED_WORKSPACE_ORDINALS,
    WorkspaceVersionError,
    build_version_window,
    workspace_contract_version,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import (
    ManifestStoreError,
    WorkspaceInspection,
    inspect_workspace,
)

#: The lifecycle status of a workspace this endpoint is serving. One value, because
#: this service owns exactly one workspace and refuses to advertise readiness unless
#: it opened it: a workspace a request can reach through this handler is, by
#: construction, the open and owned one. `WorkspaceStatus` is an open code, so a
#: later lifecycle (archiving, provisioning) adds a value rather than changing this
#: one's meaning.
WORKSPACE_STATUS_ACTIVE: Final = "active"

#: The workspace-format window *this build* reads and writes, derived from the frozen
#: ordinal table rather than restated. Deriving it is what keeps the descriptor's
#: `status` a real comparison: a window computed from the workspace's own ordinal
#: would classify every workspace as compatible with itself and say nothing.
_SUPPORTED_WORKSPACE_VERSIONS: Final[VersionWindow] = build_version_window(
    workspace_contract_version(min(SUPPORTED_WORKSPACE_ORDINALS)),
    workspace_contract_version(max(SUPPORTED_WORKSPACE_ORDINALS)),
)

#: Refusal messages, frozen as constants for the same reason the authorization seam's
#: are: a handler failure is rendered into a wire `ApiError` a caller reads, and
#: nothing about this server's own state or this caller's own values may travel there.
_MESSAGE_NO_WORKSPACE: Final = "this service instance is not serving a workspace"
_MESSAGE_UNREADABLE_WORKSPACE: Final = (
    "the served workspace cannot be described by this build"
)


def workspace_inspect(context: OperationContext) -> Mapping[str, Any]:
    """Describe the served workspace, read-only.

    The result is a `WorkspaceInspectResult`, so the wire shape is
    `{"workspace": {...}}` and not a bare descriptor -- the contract wraps it, and a
    handler that returned the descriptor alone would encode into a document no
    published decoder accepts.
    """
    service = context.service
    layout = getattr(service, "layout", None)
    settings = getattr(service, "settings", None)
    if not isinstance(layout, WorkspaceLayout) or settings is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_WORKSPACE)

    # Read failures are contained rather than raised through the transport. The
    # originals quote a filesystem path and a workspace-format ordinal, so neither is
    # chained on and neither reaches the message; the sentinel is set inside the
    # handler and the refusal raised after it exits, exactly as the authorization
    # seam drops the contract validator's messages.
    inspection: WorkspaceInspection | None
    try:
        inspection = inspect_workspace(layout, str(settings.core_version))
    except (ManifestStoreError, OSError):
        inspection = None
    if inspection is None:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_UNREADABLE_WORKSPACE
        )

    descriptor: WorkspaceDescriptor | None
    try:
        descriptor = _descriptor(context.workspace_id, inspection)
    except WorkspaceVersionError:
        descriptor = None
    if descriptor is None:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_UNREADABLE_WORKSPACE
        )

    return WorkspaceInspectResult(workspace=descriptor).to_wire()


def _descriptor(
    workspace_id: str, inspection: WorkspaceInspection
) -> WorkspaceDescriptor:
    """The served workspace as the contract's own descriptor.

    `workspace_id` is the *authorized* identifier the path passed in, not the
    manifest's. They agree on any workspace this endpoint can serve -- the runner
    takes the served workspace id from this same manifest -- and where authority and
    storage could ever disagree, the authorized fact is the one a caller is told.
    """
    manifest = inspection.manifest
    version = workspace_contract_version(
        manifest.compatibility.workspace_format_version
    )
    return WorkspaceDescriptor(
        workspace_id=workspace_id,
        # `display_name` has a `minLength` of 1, so an unnamed workspace falls back to
        # its identifier rather than encoding an empty string the schema refuses.
        display_name=manifest.name or workspace_id,
        status=WORKSPACE_STATUS_ACTIVE,
        compatibility=WorkspaceCompatibility(
            workspace_format_version=version,
            supported_workspace_versions=_SUPPORTED_WORKSPACE_VERSIONS,
            # The contract's own answer about the two values published beside it, so
            # the descriptor cannot state a status that contradicts its own fields.
            status=classify_version_compatibility(
                version, _SUPPORTED_WORKSPACE_VERSIONS
            ),
        ),
        created_at=manifest.created_at,
    )


__all__ = ["WORKSPACE_STATUS_ACTIVE", "workspace_inspect"]
