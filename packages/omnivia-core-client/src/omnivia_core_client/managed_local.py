"""Reaching an installation's service, having one started if there is none.

One production implementation of "make this installation's service exist", owned
here because both first-party adapters need it and ADR-036 forbids either
importing the other. The convention and the launcher invocation used to exist
twice, once on each side, kept in step only by a comparison test on the runtime's
side. There is one copy now and nothing to compare.

**Nothing here is a launcher.** R004-08 put the whole of discovery, arbitration,
spawning, readiness polling and failed-child cleanup inside the service package,
behind ``--managed-start``. What is left for a caller is one argv and one bounded
JSON document, which is the whole of this module: no runtime import, no socket of
its own, no descriptor read. :class:`ServiceClient` does all three, before and
after.

**The caller's deadline is the whole budget.** :func:`connect_managed_local`
takes one :class:`~omnivia_core_client.Deadline` and uses that same object for the
first connect, the launcher's bounded wait and the reconnect, so "start a service
and then call it" costs what the caller asked for. There is no timeout argument,
because a second budget beside the one the caller stated is a budget nobody
stated.

**Nothing here creates a workspace** (R004-07, R004-10). A workspace with no
manifest is refused before any process is located or run, and no directory of the
workspace's is made on any path. The run directory -- which holds the socket the
service binds and the log it writes -- is this convention's own, and it is
created only once a start has been authorised. A registered POSIX workspace's
socket lives inside its own owner-private directory under the platform temp
root, created at the same time and proved with
:func:`~omnivia_core_client.owner_private.owner_private_directory` before the
launcher runs, so a symlink, a non-directory or a directory anyone else could
reach refuses the start rather than being bound.

**Two layouts, and only what is already on disk chooses between them.** A
workspace `workspace.create` registered lives at the deterministic
``<installation-state's parent>/workspaces/<workspace_id>``, the same layout
the service mints it at; the one workspace a
pre-registration installation ever bootstrapped still lives at the fixed
``<home>/workspace``, unconditionally. Neither the installation catalogue nor a
caller-supplied path decides which applies -- this module opens neither -- so
the registered layout for ``config.workspace_id`` is tried first and the legacy
layout only if that one has no manifest; an installation with a manifest at
neither is refused exactly as an uninitialised one always was. Every registered
workspace also gets its own run directory, keyed by ``workspace_id`` under the
shared run root, so two workspace IDs never contend for one socket or one log;
the legacy layout keeps the single shared run directory it always had.

**The path and manifest are one authorization snapshot.** Every existing
directory from the installation root down to the chosen workspace must be a
real, owner-controlled directory, and the manifest is opened no-follow, read
through that trusted chain under a byte bound. A manifest carrying
``workspace_id`` must name the requested workspace exactly. The pre-registration
client historically admitted a valid JSON object without that field, so absence
remains compatible for the fixed legacy path; a present mismatch never is.
Re-resolving the two layouts takes one final selection snapshot; the exact
selected path and a SHA-256 binding over the manifest bytes are then carried
through the launcher into the service. A legacy selection also carries the
preferred registered manifest name that must remain absent around each read, so
neither process can silently authorize a later path or later bytes.

**No environment variable** (R004-11). The installation root is derived from the
state root the caller already named, and from nothing ambient.

**One fixed sentence, and it is the whole diagnostic.** Every refusal below is
:data:`_MANAGED_START_FAILED` verbatim. The launcher's ``reason``, its
``failure`` class, the service descriptor it reports, the child's own output, the
executable path, the installation paths, the endpoint and every caught
exception's text are all untrusted diagnostic material, and none of them reaches
a caller -- not in the message, and not through ``__cause__`` or ``__context__``,
because the refusal is raised outside every handler that decides on one.

Standard library plus this package's own parts.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from omnivia_core.contracts.v1 import ServiceProcessEvidence
from omnivia_core_client.deadline import Deadline
from omnivia_core_client.discovery import descriptor_path
from omnivia_core_client.errors import EndpointUnavailableError, ManagedStartError
from omnivia_core_client.owner_private import (
    owner_private_chain,
    owner_private_directory,
    read_owner_writable,
    restrict_to_owner,
    same_file,
)
from omnivia_core_client.service_client import InstallationServiceConfig, ServiceClient

__all__ = [
    "MANAGED_START_RESULT_MAXIMUM_BYTES",
    "MANAGED_START_VERSION",
    "SERVICE_EXECUTABLE",
    "ManagedServiceConnection",
    "StopResult",
    "connect_managed_local",
    "locate_service",
    "stop_managed_local",
]

#: The console script ``omnivia-core-runtime`` installs. Located and launched,
#: never imported: ADR-036 admits exactly that and nothing more.
SERVICE_EXECUTABLE: Final = "omnivia-core-service"

#: The launcher result document version this build reads. Exact, not a floor: a
#: document announcing anything else is one written against a contract this build
#: has not been checked against, and reading it optimistically is how a changed
#: field meaning becomes a service started on the wrong terms.
MANAGED_START_VERSION: Final = "1.0"

#: How much of the launcher's stdout is read as a result document. The document
#: is a handful of scalars; anything past this is not one, and parsing an
#: unbounded child's stdout lets the child choose this process's memory.
MANAGED_START_RESULT_MAXIMUM_BYTES: Final = 64 * 1024

#: The two statuses a successful launcher result carries, and the two
#: :class:`ManagedServiceConnection` reports. ``attached`` after an absent first
#: connect is not a contradiction: another process may have started the same
#: installation's service in between, and the launcher arbitrates that race.
_ATTACHED: Final = "attached"
_STARTED: Final = "started"

#: The workspace manifest, checked before anything is located or run so that an
#: uninitialised installation costs no process.
_MANIFEST_NAME: Final = "workspace.json"

#: How much of either workspace manifest is read before its identity is trusted.
#: A `workspace_id` claim is a handful of bytes; anything past this is not one,
#: and an unbounded read lets a planted file choose this process's memory before
#: a single field of it has been checked.
_MANIFEST_MAXIMUM_BYTES: Final = 64 * 1024

#: Compatibility alias for the private name used by the first registered-layout
#: implementation. Keeping it avoids making downstream white-box checks fail for
#: a rename that changes no contract.
_LEGACY_MANIFEST_MAXIMUM_BYTES: Final = _MANIFEST_MAXIMUM_BYTES

#: The fixed name the installation-state root must carry. Checked against
#: ``config.installation_state`` itself, not merely derived from it, so a
#: configuration naming some other directory is refused rather than answered
#: against a root nobody sanctioned.
_INSTALLATION_STATE_DIRECTORY: Final = "installation-state"

#: Where `workspace.create` mints every workspace it registers. Kept as this
#: convention's own literal because R004-08 forbids this package importing a
#: sibling distribution to read it.
_WORKSPACES_DIRECTORY: Final = "workspaces"

#: The one workspace a pre-registration installation ever bootstrapped.
#: Unconditional and unkeyed by ``workspace_id``, because no such installation
#: had more than one.
_LEGACY_WORKSPACE_DIRECTORY: Final = "workspace"

#: The run directory's own name: this convention's business, never the
#: workspace's, and never a caller-supplied path.
_RUN_DIRECTORY: Final = "run"

#: What a registered POSIX socket directory is created with, and proved to
#: still be before the launcher runs -- see :func:`_prepare_socket_directory`.
_SOCKET_DIRECTORY_MODE: Final = 0o700

#: Every run-directory component this client creates starts owner-only. An
#: existing component may remain readable by others for compatibility, but the
#: chain proof below requires that only its owner can change names beneath it.
_RUN_DIRECTORY_MODE: Final = 0o700

#: Windows device names alias ordinary-looking path components even with a
#: suffix, and its filesystem normally folds case. Registered workspace ids are
#: server-minted lowercase UUIDs; refusing the wider public identifier grammar on
#: that platform keeps one id equal to one path and pipe key.
_WINDOWS_RESERVED_COMPONENTS: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

#: The whole diagnostic. See the module docstring: everything that could make
#: this sentence more specific is material from a child process, a filesystem
#: path or a caught exception, and none of the three may cross this boundary.
_MANAGED_START_FAILED: Final = "the managed service could not be started"

# Native Windows process-probe constants.  Signal zero is not a probe there.
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_ERROR_ACCESS_DENIED: Final = 5
_ERROR_INVALID_PARAMETER: Final = 87

_POLL_SECONDS: Final = 0.05


@dataclass(frozen=True, slots=True)
class StopResult:
    """The closed result of one administrative stop attempt."""

    status: str


@dataclass(frozen=True, slots=True)
class ManagedServiceConnection:
    """A live service for one workspace, and how this process came to have it.

    Built only by :func:`connect_managed_local`, which is what makes the two
    fields mean something together: ``client`` was connected *after* ``status``
    was decided, so ``started`` is never a claim about a service nothing has
    since reached.
    """

    client: ServiceClient
    """The connected client, exactly as :meth:`ServiceClient.connect` built it."""

    status: str
    """``"attached"`` if a compatible service was already serving this
    installation, ``"started"`` if the launcher started one."""


@dataclass(frozen=True, slots=True)
class _ManagedStartAnswer:
    """The launcher verdict and the exact live instance it authorized."""

    status: str
    service_instance_id: str


@dataclass(frozen=True, slots=True)
class _Installation:
    """One resolved installation layout and the paths it derives.

    Built only by :func:`_resolve_installation`, which is what decides
    ``workspace_root`` and ``run_directory`` between the two admitted layouts;
    private itself, because a caller states an installation state root and a
    workspace identifier and this derives the rest. Publishing either would
    re-open the possibility of a second copy of the convention being written
    against it, or of a caller supplying a path this module is meant to derive.
    """

    home: Path
    """The installation root: the parent of ``installation-state``."""

    workspace_root: Path
    """This workspace's own directory: the registered
    ``<home>/workspaces/<workspace_id>`` for a workspace `workspace.create`
    minted, or the legacy ``<home>/workspace`` for the one a pre-registration
    installation bootstrapped."""

    run_directory: Path
    """Where this workspace's managed service binds its endpoint and writes its
    log. Keyed by ``workspace_id`` under the shared run root for a registered
    workspace, so concurrently managed workspaces never share one socket or
    one log; the single shared directory the legacy layout always used
    otherwise."""

    @property
    def installation_state(self) -> Path:
        return self.home / _INSTALLATION_STATE_DIRECTORY

    @property
    def log_path(self) -> Path:
        return self.run_directory / "service.log"

    @property
    def manifest_path(self) -> Path:
        return self.workspace_root / _MANIFEST_NAME

    @property
    def registered(self) -> bool:
        """Whether this is the keyed layout `workspace.create` mints, rather
        than the fixed legacy one every pre-registration installation shares."""
        return self.run_directory != self.home / _RUN_DIRECTORY

    @property
    def socket_directory(self) -> Path | None:
        """The owner-private directory to create and prove before this
        installation's launcher is invoked, or ``None`` when there is none to
        make.

        ``None`` for the legacy layout, whose socket sits directly inside the
        run directory this convention already owns, and for Windows, whose
        endpoint is a named pipe with no directory of its own.
        """
        if os.name == "nt" or not self.registered:
            return None
        return _registered_socket_directory(self.run_directory)

    @property
    def endpoint_uri(self) -> str:
        """The endpoint this platform's service would serve and advertise.

        The legacy layout keeps its historical endpoint exactly. A registered
        POSIX workspace instead gets its ``s.sock`` inside a fixed-length,
        owner-private directory under ``/tmp`` -- see :attr:`socket_directory`,
        created and proved before the launcher runs -- because putting the
        socket below its isolated run directory would let a
        valid installation path plus workspace id exceed the platform's Unix
        socket ceiling. A registered Windows workspace similarly folds its
        isolated run directory into the pipe name. The Windows form is
        unexercised -- no host in this lane can bind a named pipe -- and is
        written to be correct rather than claimed to be tested.
        """
        if os.name == "nt":  # pragma: no cover - POSIX-only suite
            # A digest, not `hash()`: string hashing is salted per interpreter
            # run, so `hash()` would name a different pipe every invocation.
            key = self.run_directory if self.registered else self.home
            digest = hashlib.blake2s(str(key).encode("utf-8")).hexdigest()[:16]
            return f"pipe://omnivia-core-{digest}"
        if self.registered:
            directory = _registered_socket_directory(self.run_directory)
            return f"unix://{directory / 's.sock'}"
        return f"unix://{self.run_directory / 's.sock'}"


@dataclass(frozen=True, slots=True)
class _ManifestEvidence:
    """The authorized bytes and the pathname identity they were read from."""

    content: bytes
    identity: os.stat_result


@dataclass(frozen=True, slots=True)
class _AuthorizedInstallation:
    """A selected layout plus the exact trusted objects that selected it."""

    installation: _Installation
    directory_names: tuple[str, ...]
    directory_proof: tuple[os.stat_result, ...]
    manifest: _ManifestEvidence


#: The fixed POSIX temp root a registered socket directory is named under.
#: ``/tmp`` itself, never :func:`tempfile.gettempdir`: that reads ``TMPDIR``,
#: which on some hosts (notably macOS, per-user) names a path long enough that
#: this directory's own fixed-width name plus ``/s.sock`` would risk the
#: platform's ``sockaddr_un`` ceiling -- the same ceiling a valid installation
#: path plus workspace id was already too long to fit under.
_POSIX_TEMP_ROOT: Final = Path("/tmp")


def _registered_socket_directory(run_directory: Path) -> Path:
    """The owner-private directory a registered POSIX socket's ``s.sock`` sits in.

    Named from a digest of ``run_directory`` -- already unique per
    ``workspace_id`` -- rather than the workspace's own path, for the reason
    the endpoint always folded it in: a valid installation path plus workspace
    id can exceed the platform's socket-path ceiling, while this fixed-width
    name cannot. The directory itself, not merely the socket inside it, is what
    must be owner-private: a shared, world-writable parent would let another
    user plant the socket's name -- as a symlink, or as a directory of their
    own -- before this process gets to it.
    """
    uid = str(os.getuid()) if hasattr(os, "getuid") else "posix"
    key = os.path.normcase(os.path.abspath(str(run_directory)))
    digest = hashlib.sha256(f"{uid}\0{key}".encode()).hexdigest()[:24]
    return _POSIX_TEMP_ROOT / f"omnivia-core-{uid}-{digest}"


def _prepare_socket_directory(path: Path) -> bool:
    """Create ``path`` at :data:`_SOCKET_DIRECTORY_MODE` if absent, then prove it.

    Creation and the proof are deliberately two different questions: a
    directory already there -- planted by another user, or left behind at a
    wider mode -- gets no credit for merely existing, only
    :func:`~omnivia_core_client.owner_private.owner_private_directory`'s
    verdict on what is actually there now. A symlink, a non-directory, a
    directory this process does not own, or one group or world can reach all
    fail that verdict and refuse here, before any launcher is located or run.
    """
    try:
        os.mkdir(path, _SOCKET_DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError:
        return False
    else:
        try:
            os.chmod(path, _SOCKET_DIRECTORY_MODE)
        except OSError:
            return False
    return owner_private_directory(path)


def _windows_unambiguous_workspace_component(
    workspace_id: str, *, windows: bool | None = None
) -> bool:
    """Whether ``workspace_id`` has exactly one Windows path interpretation.

    The public ``WorkspaceId`` grammar is transport-safe but deliberately wider
    than a Windows path component. Registered ids are server-minted lowercase
    UUIDs, so this boundary can fail closed on case aliases, alternate data
    streams, trailing-dot aliases and device names without changing the public
    contract shared by non-filesystem callers.
    """
    on_windows = os.name == "nt" if windows is None else windows
    if not on_windows:
        return True
    if workspace_id != workspace_id.lower():
        return False
    if workspace_id.endswith((".", " ")):
        return False
    if any(
        ord(character) < 32 or character in '<>:"/\\|?*' for character in workspace_id
    ):
        return False
    stem = workspace_id.split(".", 1)[0]
    return stem not in _WINDOWS_RESERVED_COMPONENTS


def _manifest_evidence(path: Path, workspace_id: str) -> _ManifestEvidence | None:
    """Read one trusted manifest and enforce any identity claim it carries.

    A missing ``workspace_id`` remains accepted because the original managed
    client admitted legacy JSON-object manifests without inspecting that field.
    Current manifests always carry the field; when present it must agree exactly.
    """
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    raw = read_owner_writable(path, maximum_bytes=_MANIFEST_MAXIMUM_BYTES + 1)
    if raw is None or len(raw) > _MANIFEST_MAXIMUM_BYTES:
        return None
    try:
        after = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not same_file(before, after):
        return None
    document: Any = None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    if "workspace_id" in document and document["workspace_id"] != workspace_id:
        return None
    return _ManifestEvidence(raw, after)


def _directory_proof(
    root: Path, names: tuple[str, ...]
) -> tuple[os.stat_result, ...] | None:
    """Prove a real owner-controlled chain, permitting non-secret read access."""
    return owner_private_chain(root, names, owner_private_leaf=False)


def _same_directory_proof(
    before: tuple[os.stat_result, ...], after: tuple[os.stat_result, ...] | None
) -> bool:
    return (
        after is not None
        and len(before) == len(after)
        and all(same_file(left, right) for left, right in zip(before, after))
    )


def _path_entry_exists(path: Path) -> bool:
    """Whether a name exists without following a final symlink or junction."""
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable or otherwise undecidable entry exists for authorization
        # purposes: treating it as absent could incorrectly select the fallback.
        return True
    return True


def _authorize_candidate(
    installation: _Installation,
    directory_names: tuple[str, ...],
    workspace_id: str,
) -> _AuthorizedInstallation | None:
    proof = _directory_proof(installation.home, directory_names)
    if proof is None:
        return None
    manifest = _manifest_evidence(installation.manifest_path, workspace_id)
    if manifest is None:
        return None
    after = _directory_proof(installation.home, directory_names)
    if not _same_directory_proof(proof, after):
        return None
    return _AuthorizedInstallation(installation, directory_names, proof, manifest)


def _resolve_installation(config: InstallationServiceConfig) -> _AuthorizedInstallation:
    """Resolve ``config`` to the one on-disk layout it names.

    Two layouts are admitted, and only what is already on disk decides between
    them -- never the installation catalogue, which this module does not open,
    and never a path a caller supplies, which no argument here accepts. The
    registered layout is tried first, because it is what every workspace
    `workspace.create` mints uses; the fixed legacy layout is tried only if the
    registered one has no manifest at ``config.workspace_id``. Every component
    is proved before the next name is inspected, so a symlink or junction cannot
    redirect even an existence check. A candidate that exists but does not prove
    is a refusal, not evidence that the other layout may be selected.
    """
    home = config.installation_state.parent
    if not _windows_unambiguous_workspace_component(config.workspace_id):
        _refuse()
    registered = _Installation(
        home=home,
        workspace_root=home / _WORKSPACES_DIRECTORY / config.workspace_id,
        run_directory=home
        / _RUN_DIRECTORY
        / _WORKSPACES_DIRECTORY
        / config.workspace_id,
    )
    registered_names = (_WORKSPACES_DIRECTORY, config.workspace_id)
    registered_root = home / _WORKSPACES_DIRECTORY
    if _path_entry_exists(registered_root):
        if _directory_proof(home, (_WORKSPACES_DIRECTORY,)) is None:
            _refuse()
        if _path_entry_exists(registered.workspace_root):
            if _directory_proof(home, registered_names) is None:
                _refuse()
            if _path_entry_exists(registered.manifest_path):
                authorized = _authorize_candidate(
                    registered, registered_names, config.workspace_id
                )
                if authorized is None:
                    _refuse()
                return authorized

    legacy = _Installation(
        home=home,
        workspace_root=home / _LEGACY_WORKSPACE_DIRECTORY,
        run_directory=home / _RUN_DIRECTORY,
    )
    authorized = _authorize_candidate(
        legacy, (_LEGACY_WORKSPACE_DIRECTORY,), config.workspace_id
    )
    if authorized is None:
        _refuse()
    return authorized


def _same_authorization(
    before: _AuthorizedInstallation, after: _AuthorizedInstallation
) -> bool:
    """Whether re-resolution selected the same unchanged trusted layout."""
    return (
        before.installation == after.installation
        and before.directory_names == after.directory_names
        and before.manifest.content == after.manifest.content
        and same_file(before.manifest.identity, after.manifest.identity)
        and _same_directory_proof(before.directory_proof, after.directory_proof)
    )


def _prepare_run_directory(
    installation: _Installation, workspace_id: str
) -> tuple[tuple[str, ...], tuple[os.stat_result, ...]] | None:
    """Create and prove the run chain one component at a time, never through links."""
    names = (
        (_RUN_DIRECTORY, _WORKSPACES_DIRECTORY, workspace_id)
        if installation.registered
        else (_RUN_DIRECTORY,)
    )
    for index in range(len(names)):
        prefix = names[:index]
        if _directory_proof(installation.home, prefix) is None:
            return None
        target = installation.home.joinpath(*names[: index + 1])
        created = False
        try:
            os.mkdir(target, _RUN_DIRECTORY_MODE)
            created = True
        except FileExistsError:
            pass
        except OSError:
            return None
        if created and not restrict_to_owner(target, directory=True):
            try:
                target.rmdir()
            except OSError:
                pass
            return None
        if _directory_proof(installation.home, names[: index + 1]) is None:
            return None
    proof = _directory_proof(installation.home, names)
    if proof is None:
        return None
    return names, proof


def connect_managed_local(
    config: InstallationServiceConfig, *, deadline: Deadline
) -> ManagedServiceConnection:
    """Connect to this installation's service, having one started if there is none.

    Attaching is the ordinary case and costs one connect: an installation already
    publishing a live compatible descriptor comes back as ``attached`` and nothing
    is launched. An absent descriptor and a provenance-checked descriptor whose
    endpoint cannot answer both reach the launcher. The latter is the crash path:
    the launcher owns process-evidence validation and compare-and-clean under its
    startup mutex, so it may retire that dead instance safely. Every other descriptor
    refusal still fails closed here.

    **A start is authorised by the layout, not by the request for one.** The state
    root the caller named must be the ``installation-state`` this convention
    derives from its own parent, and that parent must hold an initialised
    workspace at one of the two admitted layouts for ``config.workspace_id`` --
    see :func:`_resolve_installation`. A configuration pointing at a bare state
    directory somewhere else is refused rather than answered with a service
    started against a root nobody sanctioned, and an uninitialised installation
    is refused rather than bootstrapped.

    Then exactly one launcher invocation, one reconnect on the same ``deadline``,
    and a live client required: a launcher reporting success while nothing is
    reachable is a failure here, because what was asked for is a service that can
    be called rather than a process that exists.
    """
    # On Windows the wider public WorkspaceId grammar contains path aliases.
    # Refuse those before even discovery, because two spellings that name one
    # descriptor or workspace directory must not acquire distinct authority.
    if not _windows_unambiguous_workspace_component(config.workspace_id):
        _refuse()
    try:
        attached = ServiceClient.connect(config, deadline=deadline)
    except EndpointUnavailableError:
        # A killed service cannot remove its descriptor. Do not delete it here:
        # the Runtime launcher validates process evidence and cleans only the dead
        # instance under the installation's startup mutex.
        attached = None
    if attached is not None:
        return ManagedServiceConnection(client=attached, status=_ATTACHED)

    home = config.installation_state.parent
    if home / _INSTALLATION_STATE_DIRECTORY != config.installation_state:
        _refuse()
    authorization = _resolve_installation(config)
    installation = authorization.installation
    executable = locate_service()
    if executable is None:
        _refuse()

    # The run chain only: it holds the socket and log, both this convention's
    # business rather than workspace state. Each component is created only below
    # a proved parent, and every existing component is checked no-follow and
    # owner-controlled before a descendant or log can be named through it.
    run_authorization = _prepare_run_directory(installation, config.workspace_id)
    socket_directory = installation.socket_directory
    if (
        run_authorization is not None
        and socket_directory is not None
        and not _prepare_socket_directory(socket_directory)
    ):
        run_authorization = None
    if run_authorization is None:
        _refuse()

    # Re-run layout selection before freezing the authorization snapshot. The
    # manifest digest and, for a legacy fallback, the preferred name whose
    # absence selected it are carried through both later process boundaries.
    current = _resolve_installation(config)
    run_names, run_proof = run_authorization
    current_run = _directory_proof(installation.home, run_names)
    if not _same_authorization(authorization, current):
        _refuse()
    if not _same_directory_proof(run_proof, current_run):
        _refuse()
    if socket_directory is not None and not owner_private_directory(socket_directory):
        _refuse()
    manifest_digest = "sha256:" + hashlib.sha256(current.manifest.content).hexdigest()
    launch = _status(
        _invoke(
            executable,
            current.installation,
            manifest_digest,
            config.workspace_id,
            deadline,
        )
    )

    started = ServiceClient.connect(config, deadline=deadline)
    if started is None:
        _refuse()
    if started.descriptor.service_instance_id != launch.service_instance_id:
        _refuse()
    return ManagedServiceConnection(client=started, status=launch.status)


def _process_start_time(pid: int) -> str | None:
    """Read a process start time in the format Runtime publishes."""
    system = platform.system()
    if system == "Linux":
        try:
            return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
        except (OSError, IndexError):
            return None
    if system in ("Darwin", "FreeBSD"):
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None
        return completed.stdout.strip() or None
    return None  # pragma: no cover - Windows and other hosts


def _same_process(process: ServiceProcessEvidence) -> bool | None:
    """True for a matching process, false for a mismatch, None if unreadable."""
    if process.pid <= 0:
        return False
    observed = _process_start_time(process.pid)
    if observed is not None:
        return observed == process.start_time
    if platform.system() in ("Linux", "Darwin", "FreeBSD"):
        return False
    return None  # pragma: no cover - Windows only


def _request_stop(pid: int) -> None:
    """Use the graceful signal Runtime installs on this platform."""
    break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
    os.kill(pid, signal.SIGTERM if break_event is None else break_event)


def _windows_kernel32() -> Any | None:
    """A minimally configured kernel32 process probe, or ``None`` if unavailable."""
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        kernel32 = loader("kernel32")
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_uint32,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int32
        kernel32.GetLastError.argtypes = ()
        kernel32.GetLastError.restype = ctypes.c_uint32
    except Exception:  # noqa: BLE001 - an unusable API proves no process exit
        return None
    return kernel32


def _windows_process_exists(pid: int, *, api: Any | None = None) -> bool:
    """Probe a Windows PID without sending ``CTRL_C_EVENT`` via ``os.kill(pid, 0)``.

    A handle or access-denied proves the process exists; invalid-parameter proves
    the PID is absent. An unavailable API or unknown error is conservative: stop
    eventually reports a lingering process instead of claiming an unproved exit.
    """
    kernel32 = api if api is not None else _windows_kernel32()
    if kernel32 is None:
        return True
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    error = int(kernel32.GetLastError())
    if error == _ERROR_ACCESS_DENIED:
        return True
    return error != _ERROR_INVALID_PARAMETER


def _process_exists(pid: int) -> bool:
    if platform.system() == "Windows":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pause(deadline: Deadline) -> None:
    time.sleep(min(_POLL_SECONDS, deadline.remaining_seconds()))


def stop_managed_local(
    config: InstallationServiceConfig, *, deadline: Deadline
) -> StopResult:
    """Stop ``config`` only after its live service corroborates process identity.

    A stale descriptor never authorises a signal. Success requires the service
    to withdraw its own descriptor and the corroborated process to exit within
    the caller's original deadline.
    """
    try:
        client = ServiceClient.connect(config, deadline=deadline)
    except EndpointUnavailableError:
        return StopResult("unreachable")
    if client is None:
        return StopResult("not_running")

    process = client.descriptor.process
    if process is None:
        return StopResult("no_process")
    if _same_process(process) is False:
        return StopResult("identity_mismatch")

    try:
        _request_stop(process.pid)
    except (OSError, ValueError):
        return StopResult("identity_mismatch")

    advertised = descriptor_path(config.installation_state, config.workspace_id)
    while advertised.exists() and not deadline.expired:
        _pause(deadline)
    if advertised.exists():
        return StopResult("timeout")

    while _process_exists(process.pid) and not deadline.expired:
        _pause(deadline)
    if _process_exists(process.pid):
        return StopResult("process_lingering")
    return StopResult("stopped")


def _refuse() -> NoReturn:
    """Raise the one sentence this module has.

    Called from outside every ``except`` block that decides on a refusal, never
    inside, so ``__context__`` is genuinely ``None`` and a caught exception's own
    words cannot be recovered from the error a caller holds. Same shape and same
    reason as ``discovery.py``'s and ``service_client.py``'s ``_raise_*``
    helpers, and ``scripts/check-raise-discipline.py`` is what keeps it that way.
    """
    raise ManagedStartError(_MANAGED_START_FAILED)


def locate_service() -> str | None:
    """The service console script: on ``PATH``, or failing that beside this one.

    ``PATH`` alone is not enough. An MCP host spawns its servers with whatever
    environment it happens to hold, and a virtual environment's ``bin`` is on
    ``PATH`` only for a shell that activated it. The two scripts are installed
    side by side by the same installer, so ``sys.executable``'s directory is not
    a guess. ``PATH`` still wins, so a deliberately shadowed build is honoured.
    """
    found = shutil.which(SERVICE_EXECUTABLE)
    if found is not None:
        return found
    beside = Path(sys.executable).parent / SERVICE_EXECUTABLE
    return str(beside) if beside.is_file() and os.access(beside, os.X_OK) else None


def _invoke(
    executable: str,
    installation: _Installation,
    manifest_digest: str,
    workspace_id: str,
    deadline: Deadline,
) -> str:
    """Run ``omnivia-core-service --managed-start`` and return its stdout.

    The result stream is redirected to a temporary file and stderr is discarded,
    and that is load-bearing rather than convenient: the child writes its result
    document to *its* stdout and its human log to *its* stderr, while this
    process's stdout may be an MCP protocol stream. A pipe would keep the stream
    away from MCP too, but ``subprocess.run`` would buffer it without a ceiling;
    this process reads only the admitted result size plus one byte.

    Bounded by what is left of the caller's budget, so a launcher that never
    answers costs the deadline rather than a second timeout nobody asked for.
    """
    completed: subprocess.CompletedProcess[bytes] | None = None
    stdout = b""
    try:
        with tempfile.TemporaryFile() as captured:
            arguments = [
                executable,
                "--managed-start",
                "--workspace",
                str(installation.workspace_root),
                "--installation-state",
                str(installation.installation_state),
                "--endpoint",
                installation.endpoint_uri,
                "--expected-manifest-digest",
                manifest_digest,
                "--managed-start-log",
                str(installation.log_path),
            ]
            if not installation.registered:
                arguments.extend(
                    [
                        "--required-absent-manifest",
                        str(
                            installation.home
                            / _WORKSPACES_DIRECTORY
                            / workspace_id
                            / _MANIFEST_NAME
                        ),
                    ]
                )
            completed = subprocess.run(
                arguments,
                stdout=captured,
                stderr=subprocess.DEVNULL,
                timeout=deadline.remaining_seconds(),
                check=False,
            )
            captured.seek(0)
            stdout = captured.read(MANAGED_START_RESULT_MAXIMUM_BYTES + 1)
    except (OSError, subprocess.SubprocessError):
        # Every way the child fails to be a child: it could not be executed, or
        # it did not answer inside the budget. Both are the one refusal, and the
        # exception itself is dropped here rather than chained -- its text names
        # the executable and, for a timeout, quotes the argv.
        completed = None
    if completed is None:
        _refuse()
    if completed.returncode != 0:
        _refuse()
    if len(stdout) > MANAGED_START_RESULT_MAXIMUM_BYTES:
        _refuse()
    decoded: str | None = None
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError:
        decoded = None
    if decoded is None:
        _refuse()
    return decoded


def _status(stdout: str) -> _ManagedStartAnswer:
    """The launcher's answer and authorized service instance, or a refusal.

    Fails closed on every reading: output past the bound, output that is not
    JSON, a root that is not an object, a version this build was not written
    against, a reported failure, or a status outside the two. The service instance
    is retained only to compare with the verified descriptor
    obtained by the reconnect that follows. Without that equality, the authorized
    winner could exit and a different service could replace its descriptor between
    the launcher result and the client connect.
    """
    if len(stdout.encode("utf-8")) > MANAGED_START_RESULT_MAXIMUM_BYTES:
        _refuse()
    document: Any = None
    malformed = False
    try:
        document = json.loads(stdout)
    except (ValueError, RecursionError):
        malformed = True
    if malformed or not isinstance(document, dict):
        _refuse()
    if document.get("managed_start_version") != MANAGED_START_VERSION:
        _refuse()
    status = document.get("status")
    service = document.get("service")
    service_instance_id = (
        service.get("service_instance_id") if isinstance(service, dict) else None
    )
    if status not in {_ATTACHED, _STARTED}:
        _refuse()
    if not isinstance(service_instance_id, str) or not service_instance_id:
        _refuse()
    return _ManagedStartAnswer(status=status, service_instance_id=service_instance_id)
