"""The one selected local target, and the safe status a UI may see of it.

`start|stop|status --json` used to publish a `service` object -- workspace id,
service instance id, the runtime's raw lifecycle name, its raw `unmet` list --
and a free-form `reason`. Every one of those is a fact about *this installation's
internals* leaking to a surface that has authenticated nothing: a menu bar
process reading the adapter is the least-privileged consumer Core has. ADR-038's
`CoreTargetV1`/`CoreSafeStatusV1` are the shape that surface is allowed to see,
and this module is the only place the CLI projects into it.

**No database, manifest, or descriptor is opened to resolve the target.** The
selection is the explicit installation-state root and workspace id required by
every V06-6 invocation. A malformed workspace id resolves no target; there is no
ambient home, runtime-directory scan, or guessed identity.

**The two published references are opaque digests, not paths.** `target_ref` and
`endpoint_profile_ref` are SHA-256 over the installation selection and the
workspace identity, under two distinct domain-separation prefixes, so they are
stable for one installation across runs and machines-with-the-same-copy, differ
from each other by construction rather than by label, and reveal neither the home
directory nor the endpoint. The selection is `realpath`'d, so two aliases of an
installation-state root converge on one target rather than presenting as two
authorities over one writable workspace -- the collision
`validate_core_target_authorities` exists to refuse.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CoreSafeStatusV1,
    CoreTargetV1,
    is_contract_version,
    is_release_version,
    is_workspace_id,
)

__all__ = [
    "DISPLAY_NAME",
    "ENDPOINT_PROFILE_REF_PREFIX",
    "TARGET_REF_PREFIX",
    "degraded_status",
    "incompatible_status",
    "live_status",
    "normalized_lifecycle",
    "resolve_local_target",
    "stopped_status",
    "unreachable_status",
]

#: The fixed, non-sensitive name a UI shows for the installation-local target.
#: Not the home directory, not the workspace's own `name`: both are the user's
#: words about their own machine and neither belongs on an unauthenticated
#: surface.
DISPLAY_NAME: Final = "Local Core"

#: Distinct fixed prefixes, so the two references are never mistaken for one
#: another by a reader, and distinct digest domains below so they cannot collide
#: even for one installation.
TARGET_REF_PREFIX: Final = "core-target-"
ENDPOINT_PROFILE_REF_PREFIX: Final = "core-endpoint-profile-"

_TARGET_DOMAIN: Final = b"omnivia.core.target.v1"
_ENDPOINT_PROFILE_DOMAIN: Final = b"omnivia.core.endpoint-profile.v1"

#: The runtime's nine `ServiceState` names, narrowed to `CoreLifecycleState`.
#: `maintenance` maps to `running` because the process is up and serving --
#: `live_status` is what says it is not *ready*. Anything unrecognised, including
#: a name a newer runtime invents, is `unknown` rather than passed through.
_NORMALIZED_LIFECYCLE: Final = {
    "starting": "starting",
    "recovering": "starting",
    "migrating": "starting",
    "ready": "running",
    "running": "running",
    "maintenance": "running",
    "draining": "stopping",
    "stopped": "stopped",
    "failed": "failed",
}


def normalized_lifecycle(state: object) -> str:
    """One runtime `ServiceState` name as a `CoreLifecycleState`, or `unknown`."""
    if not isinstance(state, str):
        return "unknown"
    return _NORMALIZED_LIFECYCLE.get(state, "unknown")


def _digest(domain: bytes, selection: str, workspace_ref: str) -> str:
    """A stable opaque reference over one installation selection and workspace.

    `os.fsencode` rather than `str.encode`: a home directory is whatever bytes
    the filesystem holds, and a path that does not decode as UTF-8 must still
    produce a reference rather than an exception.
    """
    hasher = hashlib.sha256()
    hasher.update(domain)
    hasher.update(b"\x00")
    hasher.update(os.fsencode(selection))
    hasher.update(b"\x00")
    hasher.update(workspace_ref.encode("utf-8"))
    return hasher.hexdigest()[:32]


def resolve_local_target(
    installation_state: Path, workspace_id: str
) -> CoreTargetV1 | None:
    """The explicit installation/workspace as a `CoreTargetV1`, if admissible.

    `kind` and `management` are fixed rather than inferred: this CLI can only
    ever address the installation it was pointed at, on this host, whose service
    process it starts and signals itself. A remote or externally-managed target
    is not reachable from here at all, which is what makes the local-only
    `start`/`stop` actions safe to offer.
    """
    if not isinstance(installation_state, Path) or not is_workspace_id(workspace_id):
        return None
    selection = os.path.realpath(installation_state)
    return CoreTargetV1(
        contract_version=CONTRACT_VERSION,
        target_ref=TARGET_REF_PREFIX + _digest(_TARGET_DOMAIN, selection, workspace_id),
        display_name=DISPLAY_NAME,
        kind="local",
        workspace_ref=workspace_id,
        management="locally_managed",
        endpoint_profile_ref=(
            ENDPOINT_PROFILE_REF_PREFIX
            + _digest(_ENDPOINT_PROFILE_DOMAIN, selection, workspace_id)
        ),
    )


def live_status(
    target: CoreTargetV1,
    *,
    state: object,
    ready: object,
    server_version: object = None,
    protocol_version: object = None,
) -> CoreSafeStatusV1:
    """A target that was discovered, dialled and answered.

    The readiness answer is live, so `connection_state` is `connected` and
    `compatibility_state` is `compatible`: discovery negotiated all three
    versions before the call was made, and a service that answered it is one this
    build can talk to.

    Only the two actions this adapter actually performs are offered, and only in
    the phase where each is meaningful. `restart`, `open` and `reconnect` are
    absent because `omnivia` implements none of them; advertising an action a
    caller cannot perform is the same defect as advertising a stale state.

    The raw `unmet` list is not projected in any form. It names this build's
    internal readiness preconditions, which is exactly the sort of internal a
    safe status may not carry; `degraded` is the closed advisory that says the
    same thing to a caller who cannot act on the detail anyway.
    """
    lifecycle = normalized_lifecycle(state)
    is_ready = ready is True and state != "maintenance"
    if lifecycle == "running":
        permitted: tuple[str, ...] = ("stop",)
    elif lifecycle == "stopped":
        permitted = ("start",)
    else:
        permitted = ()
    return CoreSafeStatusV1(
        contract_version=target.contract_version,
        target=target,
        lifecycle_state=lifecycle,
        readiness_state="ready" if is_ready else "not_ready",
        compatibility_state="compatible",
        connection_state="connected",
        warning_codes=() if is_ready else ("degraded",),
        permitted_actions=permitted,
        server_version=(
            server_version
            if isinstance(server_version, str) and is_release_version(server_version)
            else None
        ),
        protocol_version=(
            protocol_version
            if isinstance(protocol_version, str) and is_contract_version(protocol_version)
            else None
        ),
    )


def stopped_status(target: CoreTargetV1) -> CoreSafeStatusV1:
    """A valid selected target with nothing advertised and nothing answering."""
    return CoreSafeStatusV1(
        contract_version=target.contract_version,
        target=target,
        lifecycle_state="stopped",
        readiness_state="not_ready",
        compatibility_state="unknown",
        connection_state="disconnected",
        warning_codes=(),
        permitted_actions=("start",),
    )


def incompatible_status(target: CoreTargetV1) -> CoreSafeStatusV1:
    """An incompatible service owns this workspace, and it is not ours to move.

    No action at all. The owner is somebody's authoritative service: stopping it
    is not this build's call, and starting a second one is what the launcher
    already refused to do.
    """
    return CoreSafeStatusV1(
        contract_version=target.contract_version,
        target=target,
        lifecycle_state="unknown",
        readiness_state="not_ready",
        compatibility_state="incompatible",
        connection_state="disconnected",
        warning_codes=("version_incompatible",),
        permitted_actions=(),
    )


def unreachable_status(target: CoreTargetV1, *, may_start: bool) -> CoreSafeStatusV1:
    """Nothing usable answered: unreachable, stale, or an ordinary failed start.

    `may_start` is never a guess about whether a start would succeed. It is
    whether the attach-first managed-start path may safely be *attempted* from
    here, which it may exactly when this command's own start path is the one that
    would run it -- and a stop is never offered, because no ownership was
    established to stop.
    """
    return CoreSafeStatusV1(
        contract_version=target.contract_version,
        target=target,
        lifecycle_state="unknown",
        readiness_state="not_ready",
        compatibility_state="unknown",
        connection_state="unreachable",
        warning_codes=("endpoint_unreachable",),
        permitted_actions=("start",) if may_start else (),
    )


def degraded_status(target: CoreTargetV1, *, lifecycle_state: str) -> CoreSafeStatusV1:
    """Reachable, and in a phase this command may not act on.

    Two branches use it: a stop that was accepted and has not completed, and a
    live service whose publishing process this host cannot corroborate. Neither
    offers an action -- the first because the requested one is already under way,
    the second because ownership was never established.
    """
    return CoreSafeStatusV1(
        contract_version=target.contract_version,
        target=target,
        lifecycle_state=lifecycle_state,
        readiness_state="not_ready",
        compatibility_state="unknown",
        connection_state="connected",
        warning_codes=("degraded",),
        permitted_actions=(),
    )
