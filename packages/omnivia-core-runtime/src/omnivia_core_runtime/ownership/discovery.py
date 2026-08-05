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
import re
import subprocess
from functools import cache
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

#: The Windows form of the same restriction, spelled for `icacls`.
#:
#: `os.chmod` is close to a no-op there -- it toggles the read-only attribute and
#: nothing else -- so a descriptor published on Windows carries whatever the
#: inherited DACL grants, which on a shared machine is every local user. The
#: accepted client's Windows reader is what this has to satisfy: an object owned by
#: the current user whose DACL names no other trustee and is not inherited.
#:
#: Directories carry `(OI)(CI)` so anything created below them inherits that single
#: entry instead of the parent's, which is the closest Windows has to `0o700`.
_WINDOWS_DIRECTORY_RIGHTS = "(OI)(CI)F"
_WINDOWS_FILE_RIGHTS = "F"

#: `whoami /user` reports the SID in this form, mixed into a CSV row.
_SID_RE = re.compile(r"S-1-[0-9-]+")


def descriptor_path(runtime_directory: Path) -> Path:
    return runtime_directory / DESCRIPTOR_NAME


def _system32(program: str) -> str:  # pragma: no cover - Windows only
    """An absolute path to a Windows system tool.

    Resolving through `PATH` would let any directory earlier on it supply the
    program that sets the descriptor's ACL, which is the wrong way round for a
    restriction. `SystemRoot` is where the documentation says these live and is not
    always `C:\\Windows`.
    """
    return str(Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", program))


@cache
def _windows_owner_sid() -> str:  # pragma: no cover - Windows only
    """This process's token user SID, in the form the accepted client compares.

    The client reads the DACL and compares each allow ACE's SID against its own
    `TokenUser`, so the ACE granted here has to name that same SID. `whoami /user`
    reports exactly it. An account *name* would be a second lookup that can resolve
    to a different principal than the token carries, and the whole point is that
    these two agree.
    """
    completed = subprocess.run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    found = _SID_RE.search(completed.stdout)
    if completed.returncode != 0 or found is None:
        raise OSError(
            f"could not read this process's own user SID: {completed.stderr.strip()[:200]}"
        )
    return found.group()


def _restrict_windows(path: Path, *, directory: bool) -> None:  # pragma: no cover
    """Reduce `path` to an owner-only DACL: the Windows form of `0o600`/`0o700`.

    Three invocations, because no single one does all of it.

    `/setowner` first. The accepted client refuses an object whose owner is not its
    own `TokenUser`, and ownership is *inherited from the token*, not from the DACL:
    a process whose token carries `TokenOwner = BUILTIN\\Administrators` -- the
    normal shape of an elevated administrator -- creates objects owned by
    Administrators, and every DACL below would be correct while the client refused
    the descriptor anyway.

    It can fail on its own, and the rights are not the ones an owner gets for free.
    An owner implicitly holds `READ_CONTROL` and `WRITE_DAC` -- which is what
    `/reset` and `/grant:r` need -- but *not* `WRITE_OWNER`, which is what
    `/setowner` needs and which comes only from the DACL or from
    `SeTakeOwnershipPrivilege` in the token. So a process that already owns the
    object can fail this step while the two below it would have succeeded. That is
    a loud refusal to publish rather than a quiet mis-publication, and it stays
    fatal for that reason: an owner the client will refuse is a service that
    advertises itself and cannot be reached, discovered in the field by nobody.

    Neither ordering dominates and this one is a choice, not a consequence. First
    rescues an object owned by another principal when the token does carry
    `SeTakeOwnershipPrivilege`, since owning it is then what supplies the
    `WRITE_DAC` the rest needs. Last would instead rescue an object this process
    owns whose DACL omits `WRITE_OWNER`, because `F` includes it and `/grant:r`
    would have just granted it. The chain is created by this installation's own
    user, so the first case is the one that shows up.

    `/reset` then drops the *explicit* entries, which `/inheritance:r` does not
    touch -- an installer's or a user's grant to a group would otherwise survive a
    restriction the POSIX `chmod` clears unconditionally. `/inheritance:r` drops the
    *inherited* entries and marks the DACL protected, so a permissive parent cannot
    re-supply them, and `/grant:r` leaves one allow ACE naming this process's own
    SID.

    `icacls` rather than `ctypes`, deliberately. No host in this repository can
    exercise Windows, and a wrong `SetNamedSecurityInfoW` call either does nothing
    or writes a security descriptor nobody intended -- both silent. A wrong `icacls`
    argument is a non-zero exit with a message on stderr. The cost is a process
    launch per path; it is paid once per publication, not per probe.

    Failure raises, carrying what the tool said. A descriptor advertising a live
    endpoint to every local user is worse than no descriptor, so this fails closed --
    and the message is the only diagnostic a host nobody here can reach will send
    back, which is half the reason this is a tool call and not a native one.
    """
    rights = _WINDOWS_DIRECTORY_RIGHTS if directory else _WINDOWS_FILE_RIGHTS
    owner = _windows_owner_sid()
    for arguments in (
        ("/setowner", f"*{owner}"),
        ("/reset",),
        ("/inheritance:r", "/grant:r", f"*{owner}:{rights}"),
    ):
        completed = subprocess.run(
            [_system32("icacls.exe"), str(path), *arguments, "/q"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(
                f"could not restrict {path} to its owner: "
                f"{completed.stderr.strip()[:200]}"
            )


def _restrict(path: Path, *, directory: bool) -> None:
    """Reduce `path` to owner-only access, in whatever form this platform has one."""
    os.chmod(path, RUNTIME_DIRECTORY_MODE if directory else DESCRIPTOR_MODE)
    if os.name == "nt":  # pragma: no cover - proved on the hosted Windows row
        _restrict_windows(path, directory=directory)


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

    Which ancestors *were* created is decided by the creating syscall, not by a
    prior scan. Asking `exists()` first and then calling `mkdir(parents=True)` puts
    a window between the two: an ancestor observed as present, removed, and then
    rebuilt by that `mkdir` was created by this call and is not in the scanned set,
    so it kept the umask-derived mode. Creating the chain a level at a time answers
    the question with the only call that can answer it -- a `mkdir` that returns is
    one this call made, and one that fails on a directory that is there is one it
    did not -- which is why the failure is tolerated only when the directory exists
    afterwards. Anything else propagates, exactly as `mkdir(parents=True)` did: a
    component that is a file, or a chain this process may not build, is not
    something to publish into.

    Each created directory is restricted immediately rather than at the end. A
    publication that fails partway leaves the levels it did build, and a failure
    after the loop would leave them at the umask-derived mode *permanently* -- the
    retry finds them already present, and a directory this call did not create is
    one it will not touch. Restricting in place costs the same syscalls and means a
    failed publication leaves nothing open behind it. On Windows it also means each
    level is owner-only before the next is created inside it, so the child inherits
    that rather than the parent's.
    """
    created: list[Path] = []
    for directory in reversed((runtime_directory, *runtime_directory.parents)):
        try:
            # No `mode=` here. It would narrow the window between creation and the
            # restriction below, and it would also make that restriction
            # unfalsifiable on POSIX -- `mkdir(0o700)` under any umask lands on
            # `0o700`, so deleting the restriction of created ancestors would leave
            # every case green. The restriction is the thing that has to be proved.
            directory.mkdir()
        except OSError:
            if not directory.is_dir():
                raise
            continue
        _restrict(directory, directory=True)
        created.append(directory)
    for directory in (runtime_directory.parent, runtime_directory):
        if directory not in created:
            _restrict(directory, directory=True)


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

    Windows reaches the same property through the same sequence for a different
    reason. `os.open`'s mode buys nothing there, and the DACL the new file inherits
    from its directory is whatever that directory hands down, so the restriction is
    applied to the temporary file and the rename -- within one directory, on one
    volume -- publishes the object already holding it.
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
    _restrict(temporary, directory=False)
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

    This deliberately does not distinguish a malformed `window` (the discovered
    service's own advertisement being untrustworthy) from a malformed `version`
    (the caller's own input being unreadable) -- both become the same `False`.
    That is safe here only because callers that hand a caller-supplied version to
    `is_compatible` are expected to have validated it is a `major.minor`
    ContractVersion first; `service.bootstrap._decide` does exactly that, before
    discovery ever runs, so the only `ValueError` this can still be swallowing in
    practice is a genuinely untrustworthy `window`.
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

    `workspace_format_version` must already be the public `major.minor`
    ContractVersion (e.g. `"1.0"`), not the private workspace manifest's bare
    ordinal (`"1"`) -- see `service.versions.workspace_contract_version` for the
    one place that translation happens. This function does not validate that for
    you: an unreadable version is silently refused rather than raising (see
    `_within`), so a caller that skips the translation gets a `False` that looks
    exactly like a genuine incompatibility. `service.bootstrap.coordinated_startup`
    validates before calling this, precisely to avoid handing out that misleading
    answer.
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
