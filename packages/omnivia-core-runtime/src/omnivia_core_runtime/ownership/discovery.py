"""Service discovery and readiness advertisement (T-0629E, ADR-037).

Discovery lives in installation-local runtime state, never in the portable
workspace: an endpoint describes the machine, and a copied workspace must not
advertise the machine that made it.

Two properties matter more than the file format.

Publication is atomic. A partially written descriptor would let a client connect to
an endpoint that does not exist yet, so the descriptor is written to a temporary
file and renamed.

Cleanup is compare-by-instance. A failed startup must remove *its own* descriptor
and nothing else. Unconditional deletion is the bug this guards against: a service
that fails while another instance is already ready would otherwise un-advertise the
healthy owner (BD-10).

The document is the public `ServiceEndpointDescriptor`, encoded and decoded through
the contract's own helpers. There is one descriptor file and one shape: the accepted
client reads this exact path, so a private shape here would not be a second format,
it would be a collision that refuses every real descriptor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from omnivia_core.contracts.v1 import (
    ServiceEndpointDescriptor,
    VersionWindow,
    decode_service_endpoint_descriptor,
    encode_service_endpoint_descriptor,
    version_in_window,
)

DESCRIPTOR_NAME = "service.json"

#: The descriptor names a live endpoint on this machine, so it is readable only by
#: the user whose service published it. The accepted client masks `0o077` and refuses
#: anything wider, on the file *and* on the directories above it.
DESCRIPTOR_MODE = 0o600
RUNTIME_DIRECTORY_MODE = 0o700


def descriptor_path(runtime_directory: Path) -> Path:
    return runtime_directory / DESCRIPTOR_NAME


def _make_private(runtime_directory: Path) -> None:
    """Create the runtime directory chain and reduce it to owner-only access.

    `mkdir(mode=...)` would be enough for none of this. Its mode is masked by the
    process umask, it applies only to the leaf, and it does nothing at all to a
    directory that already exists -- which this one does, because the installation
    layout creates the runtime directory long before anything is published there.
    An explicit `chmod` is the only form that holds under every umask.

    Two sets of directories get it, for two different reasons.

    The descriptor's own directory and the `runtime/` parent it sits in are the two
    levels the reader validates, so they are set whether this call created them or
    found them. That is the M2 repair: the installation layout leaves both at the
    umask-derived mode and the accepted client refuses them.

    Every ancestor *this call creates* is set as well. `mkdir(parents=True)` builds
    them at the umask-derived mode, so publishing into a not-yet-existing
    installation root under a permissive umask left that root world-writable. The
    client never looks at it -- it opens the installation root as a trusted root
    and checks only that it is a directory -- but a world-writable root lets any
    local user rename the validated `runtime` directory out from under the chain,
    which defeats every mode below it.

    Ancestors that already existed are left alone. Their permissions are the
    installer's or the user's decision, and silently re-permissioning a directory
    tree this code did not create is its own hazard.
    """
    created = [
        directory
        for directory in (runtime_directory, *runtime_directory.parents)
        if not directory.exists()
    ]
    runtime_directory.mkdir(parents=True, exist_ok=True)
    for directory in dict.fromkeys(
        [*created, runtime_directory.parent, runtime_directory]
    ):
        os.chmod(directory, RUNTIME_DIRECTORY_MODE)


def publish(runtime_directory: Path, descriptor: ServiceEndpointDescriptor) -> Path:
    """Atomically publish a descriptor, readable only by its owner.

    Written to a uniquely named temporary file and renamed, so two instances
    publishing concurrently cannot interleave into one another's bytes and no
    reader ever sees a partial document.

    The mode is established on the temporary file, before the rename. `replace()`
    carries the temporary file's mode to the target, so a `chmod` afterwards would
    leave a window in which the descriptor is readable at its published path by
    anyone -- brief, but exactly long enough, and the window is the whole point.

    The mode is set twice on purpose, and not because of the umask -- a umask only
    ever clears bits, so it cannot widen an `O_CREAT` of `0o600`. The `chmod` is
    there for the file that already exists: `O_CREAT` does not reset the mode of
    one, and `O_TRUNC` replaces its contents and not its permissions. The temporary
    name carries this process's pid, so a run that crashed between the open and the
    rename leaves a temporary file exactly where the next process to be given that
    pid will write -- and without the `chmod` that stale file's own mode is what
    the rename publishes. `os.open`'s mode still matters: it is what keeps the
    ordinary case restrictive from creation rather than from the `chmod`.
    """
    _make_private(runtime_directory)
    target = descriptor_path(runtime_directory)
    temporary = target.with_name(f".{DESCRIPTOR_NAME}.{os.getpid()}.tmp")
    document = (
        json.dumps(
            encode_service_endpoint_descriptor(descriptor), indent=2, sort_keys=True
        )
        + "\n"
    )
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, DESCRIPTOR_MODE), "wb"
    ) as handle:
        handle.write(document.encode("utf-8"))
    os.chmod(temporary, DESCRIPTOR_MODE)
    temporary.replace(target)
    return target


def discover(runtime_directory: Path) -> ServiceEndpointDescriptor | None:
    """Read the current descriptor, or None when absent or unreadable.

    A malformed descriptor is treated as absent rather than raising: a client's
    correct response to garbage is to start a service, not to crash. The contract's
    own decoder decides what "malformed" means, and every way it says so --
    `ContractDecodeError` and `ContractSemanticError` are both `ValueError`s -- is
    the same answer here.
    """
    path = descriptor_path(runtime_directory)
    if not path.is_file():
        return None
    try:
        return decode_service_endpoint_descriptor(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, TypeError, ValueError):
        return None


def compare_and_clean(runtime_directory: Path, service_instance_id: str) -> bool:
    """Remove the descriptor only if it belongs to `service_instance_id`.

    Returns True when a descriptor was removed. A failing instance calls this during
    cleanup; because it compares first, a service that fails while a *different*
    instance is ready cannot un-advertise the healthy owner.
    """
    current = discover(runtime_directory)
    if current is None or current.service_instance_id != service_instance_id:
        return False
    try:
        descriptor_path(runtime_directory).unlink()
    except OSError:  # pragma: no cover - platform dependent
        return False
    return True


def _within(version: str, window: VersionWindow) -> bool:
    """Whether `version` sits inside `window`, treating anything unreadable as no.

    `version_in_window` refuses a malformed version or a reversed window rather
    than answering, and a descriptor that cannot be compared is not a descriptor
    that may be used. Failing closed here keeps that refusal from escaping into a
    launcher, which has no better answer than "not usable" anyway.
    """
    try:
        return version_in_window(version, window)
    except (TypeError, ValueError):
        return False


def is_compatible(
    descriptor: ServiceEndpointDescriptor,
    *,
    api_version: str,
    workspace_format_version: str,
) -> bool:
    """Whether a discovered service is one this client may use.

    A running service of the wrong API or workspace-format version is not a service
    this client can use, so discovery must not treat it as one — otherwise a client
    would connect and then fail on every call.

    Both questions are window containment, not scalar equality. A descriptor
    publishes what the service *supports*, as a range; the caller supplies the one
    version it needs. "Does what I need fall inside what you support" is the
    question a launcher is actually asking, and equality answers it wrongly in one
    direction only -- it refuses a service that supports the caller's version
    perfectly well, merely alongside others.

    The workspace format is compared against `supported_workspace_versions` rather
    than the in-force `workspace_format_version` for the same reason. The in-force
    field states which format this service currently has open, which is a fact
    about the service; the window states which formats it can serve, which is the
    answer to the caller's question. Today the Runtime publishes a one-version-wide
    workspace window derived from the format it opened, so the two agree exactly --
    this is the reading that stays correct if that ever stops being true.
    """
    return _within(api_version, descriptor.supported_api_versions) and _within(
        workspace_format_version, descriptor.supported_workspace_versions
    )


__all__ = [
    "DESCRIPTOR_MODE",
    "DESCRIPTOR_NAME",
    "RUNTIME_DIRECTORY_MODE",
    "compare_and_clean",
    "descriptor_path",
    "discover",
    "is_compatible",
    "publish",
]
