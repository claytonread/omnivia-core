"""The update coordinator and its detached worker (spec v0.4 §7, §9–§11).

The coordinator owns one update per installation: it holds a kernel-level
``flock`` on the installation's update lock (atomic across processes, released
by the kernel if the holder dies — never a PID/age marker), stages and verifies
the approved candidate **before** touching anything, binds the user's
confirmation to the exact release and verified artifact identity, and hands
execution to a short-lived detached worker that inherits the lock through the
hand-off — exclusive ownership survives the parent's exit (§7.2, A05/A06).

The worker is stdlib-only and runs from the installation's own interpreter: it
stops the affected services, installs the staged wheels with the
installation's own pip (no new installation technology, §9.1), restarts only
the services that were running, verifies the intended release and readiness,
and records the outcome. Its phase machine writes the operation record at
every transition; ``updated`` requires readiness, not an installer exit code.

Everything mutable is injected for tests: the fetcher, the installed-version
provider, the confirm callable, the service probe/stop/start primitives and
the command runner. No test here touches a live feed or a real service.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Self

from omnivia_core_client.errors import ClientError
from omnivia_core_client.managed_local import run_first_party_command
from omnivia_core_client.owner_private import (
    owner_private_directory_metadata,
    restrict_to_owner,
)
from omnivia_core_client.update_trust import (
    PRODUCTION_TRUST_POLICY,
    UpdateTrustPolicy,
)
from omnivia_core_client.updates import (
    FIRST_PARTY_PACKAGES,
    _version_key,
)

PRODUCT_ID: Final = "omnivia-core"
#: The built candidate bundle the release publishes (the publication workflow's
#: pinned asset name; U3's publisher uploads exactly this name per release).
BUNDLE_ASSET_NAME: Final = "omnivia-core-standard-candidate.zip"
RECORD_NAME: Final = "update-operation.json"
LOCK_NAME: Final = "update.lock"
STAGING_ROOT_NAME: Final = "update-staging"

#: The candidate's own checksum inventory, as `build-standard-candidate.py`
#: writes it: one row per regular candidate file except itself, each
#: ``<64 hex>  <path relative to the candidate root>``.
CHECKSUMS_MEMBER: Final = "checksums-sha256.txt"
WHEELS_DIRECTORY: Final = "wheels"

PHASES: Final = ("preparing", "stopping", "installing", "restarting", "verifying")
OUTCOMES: Final = ("updated", "blocked", "failed", "restart_required")

#: The whole phase machine, published rather than implied, and the same table
#: ``contracts/update/v1`` publishes. A record that moves outside it is a defect
#: in this writer rather than an outcome of the update, so the writer refuses
#: rather than recording it: a journal that can say an impossible thing is a
#: journal a reader cannot reason about.
LEGAL_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "preparing": frozenset({"stopping", "blocked", "failed"}),
    "stopping": frozenset({"installing", "failed"}),
    "installing": frozenset({"restarting", "failed"}),
    "restarting": frozenset({"verifying", "failed"}),
    "verifying": frozenset({"complete", "failed"}),
    "complete": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
}
INITIAL_PHASE: Final = "preparing"
TERMINAL_PHASES: Final = frozenset({"complete", "blocked", "failed"})

#: Archive bounds. The real bundle is the whole standard candidate -- the five
#: first-party wheels, their pinned third-party closure (which is where the tens
#: of megabytes are), the licence tree, the evidence and the metadata -- so these
#: are generous rather than tight. Being generous is the point: a bound exists so
#: a hostile archive is refused rather than unpacked, not to second-guess the
#: builder. Expanded bytes are counted **while writing**, never read from the
#: member's declared size: a declared size is a number the archive chose.
MAX_BUNDLE_MEMBERS: Final = 1024
MAX_EXPANDED_BYTES: Final = 384 * 1024 * 1024
MAX_MEMBER_BYTES: Final = 64 * 1024 * 1024
_EXTRACT_CHUNK: Final = 1024 * 1024

#: Excluded from an archive member path, together with every character below
#: U+0020. The backslash and the colon are the ones with teeth -- ``C:`` and
#: ``..\\`` are how a relative name stops being one on Windows, and a backslash
#: is an ordinary filename character on POSIX, so one archive name means two
#: different things depending on who unpacks it. Kept identical to the payload
#: rule the trusted-runtime contract publishes, so neither side admits what the
#: other refuses.
_RESERVED_IN_MEMBER: Final = '\\:*?"<>|'

#: A PEP 427 wheel filename for a first-party pure-Python wheel, as the standard
#: candidate builds them. Anchored and exact: a name this does not match is
#: refused rather than approximately parsed.
_WHEEL_NAME_RE: Final = re.compile(
    r"\A(omnivia_[a-z0-9_]+)-((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*)-py3-none-any\.whl\Z"
)

#: One inventory row: the digest, exactly two spaces, the path.
_CHECKSUM_ROW_RE: Final = re.compile(r"\A([0-9a-f]{64})  (\S.*)\Z")

#: The five first-party distributions by the name their wheel filename spells.
_WHEEL_DISTRIBUTIONS: Final = {
    name.replace("-", "_"): name for name in FIRST_PARTY_PACKAGES
}


class UpdateApplyError(Exception):
    """A bounded coordinator failure with its failure code."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _tag_from_release_url(release_url: str) -> str:
    """The release tag is first-party published inside ``release_url``."""
    marker = "/releases/tag/"
    if marker not in release_url:
        raise UpdateApplyError("release_identity_unresolved", "the release URL carries no tag")
    return release_url.rsplit(marker, 1)[1]


def bundle_url(release_url: str) -> str:
    """The candidate bundle's download address for one approved release."""
    return f"{release_url.rsplit('/tag/', 1)[0]}/download/{_tag_from_release_url(release_url)}/{BUNDLE_ASSET_NAME}"


# --------------------------------------------------------------------------
# Operation record
# --------------------------------------------------------------------------


@dataclass(slots=True)
class OperationRecord:
    """One current/last update operation per installation (v0.4 §10)."""

    operation_id: str
    installation_ref: str
    approved_version: str
    approved_bundle_sha256: str | None
    started_at: str
    phase: str = "preparing"
    outcome: str | None = None
    finished_at: str | None = None
    failure_code: str | None = None

    def to_wire(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "operation_id": self.operation_id,
            "product_id": "omnivia-core",
            "installation_ref": self.installation_ref,
            "approved_release_ref": {
                "version": self.approved_version,
                "bundle_sha256": self.approved_bundle_sha256,
            },
            "started_at": self.started_at,
            "phase": self.phase,
        }
        if self.outcome is not None:
            document["outcome"] = self.outcome
        if self.finished_at is not None:
            document["finished_at"] = self.finished_at
        if self.failure_code is not None:
            document["failure_code"] = self.failure_code
        return document

    def write(self, installation_state: Path) -> None:
        path = installation_state / RECORD_NAME
        path.write_text(json.dumps(self.to_wire(), indent=2) + "\n", encoding="utf-8")

    def advance(self, phase: str, installation_state: Path) -> None:
        """Move to ``phase`` and rewrite the journal, or refuse the transition.

        The table is the contract's, and the refusal is loud rather than
        recorded: an illegal transition is a defect in this writer, and writing
        it would put a sequence of phases in the journal that the machine cannot
        produce -- which is exactly what a reader of a crashed update needs the
        journal not to do.
        """
        permitted = LEGAL_TRANSITIONS.get(self.phase)
        if permitted is None or phase not in permitted:
            raise UpdateApplyError(
                "illegal_transition",
                f"an update may not move from {self.phase!r} to {phase!r}",
            )
        self.phase = phase
        self.write(installation_state)


def read_operation_record(installation_state: Path) -> dict[str, Any] | None:
    """The journal as a document, or ``None`` when there is none.

    ``{"corrupt": True}`` is a *third* answer and never conflated with the other
    two: a reader of a crashed update must be able to tell "no update has run"
    from "an update ran and its journal can no longer be read". Corruption now
    covers a document that parses but is not an object, and one whose ``phase``
    is outside the published vocabulary -- both are a journal a caller cannot
    reason about, and reporting them as a record would hand a caller a phase no
    phase machine has.
    """
    path = installation_state / RECORD_NAME
    if not path.is_file():
        return None
    document: Any = None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"corrupt": True}
    if not isinstance(document, dict):
        return {"corrupt": True}
    if document.get("phase") not in LEGAL_TRANSITIONS:
        return {"corrupt": True}
    return document


# --------------------------------------------------------------------------
# The installation lock
# --------------------------------------------------------------------------


class InstallationLock:
    """One exclusive update owner per installation, held by the kernel.

    ``flock`` is atomic across processes and released by the kernel when the
    holder's file descriptors all close — including the inherited descriptor
    the worker receives at hand-off. There is no stale state to recover from:
    a dead holder leaves no lock.
    """

    def __init__(self, installation_state: Path) -> None:
        self._path = installation_state / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        os.write(fd, b"")  # keep the fd real; content is not the lock
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exception: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# Staging: download, verify, unpack — before anything is stopped
# --------------------------------------------------------------------------


def _owner_only_directory(path: Path, *, below: Path) -> None:
    """Create ``path`` under ``below``, every component owner-only from creation.

    ``Path.mkdir(parents=True, mode=0o700)`` is deliberately **not** used.
    ``mode`` applies to the leaf only; the missing parents are created with the
    process umask's permissions and the mode is not taken into account. A member
    two directories deep -- ``licenses/<package>/LICENSE``, which the real
    candidate is full of -- would therefore have landed, owner-private, inside a
    world-readable directory. Each component is created in turn here instead.

    ``0o700`` is given to ``mkdir`` rather than applied by a later ``chmod``, so
    there is no instant at which a directory exists and is wider. Windows has no
    mode bits, so :func:`~omnivia_core_client.owner_private.restrict_to_owner`
    stands in for the argument there -- and fails closed, because a staging
    directory this process could not restrict is one another principal may be
    able to swap a wheel inside between verification and installation.

    Every component is then *re-read* with ``lstat`` and held against the shared
    owner-private directory policy, so a component that already existed as a
    symbolic link, a reparse point, a non-directory, or a directory somebody else
    can reach is refused rather than written through. ``below`` is the boundary
    this never narrows or inspects: the installation's own state directory is
    shared and conventionally moded, and not this function's to change.
    """
    try:
        below.mkdir(parents=True, exist_ok=True)
    except OSError as failure:
        raise UpdateApplyError("staging_unavailable", str(failure)) from failure
    current = below
    for part in path.relative_to(below).parts:
        current = current / part
        created = False
        try:
            current.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            created = False
        except OSError as failure:
            raise UpdateApplyError("staging_unavailable", str(failure)) from failure
        if created and not restrict_to_owner(current, directory=True):
            raise UpdateApplyError(
                "staging_not_owner_only",
                "a staging directory could not be made reachable by its owner alone",
            )
        metadata: os.stat_result | None = None
        try:
            metadata = current.lstat()
        except OSError:
            metadata = None
        if metadata is None or not owner_private_directory_metadata(metadata):
            raise UpdateApplyError(
                "staging_not_owner_only",
                "a staging path component is not a directory only its owner can reach",
            )


def _safe_member_path(name: str) -> PurePosixPath:
    """One normalised relative POSIX name inside the bundle, or a refusal.

    Everything that is not plainly a relative name sequence is **refused** rather
    than normalised into one: an absolute path, a drive letter, a backslash, a
    ``.`` or ``..`` component, an empty component from a doubled separator, a
    NUL. Refusing is what makes "every extracted path stays beneath the staging
    root" a property of the parse, rather than of a comparison a later change
    could forget to make.
    """
    if not name or len(name) > 512:
        raise UpdateApplyError("archive_reserved_member", "an archive member name is unusable")
    trimmed = name.removesuffix("/")
    if not trimmed:
        raise UpdateApplyError("archive_reserved_member", "an archive member name is empty")
    if trimmed.startswith("/"):
        raise UpdateApplyError(
            "archive_absolute_member", "an archive member names an absolute path"
        )
    if "\\" in trimmed:
        # Refused on every platform, not only Windows: a backslash is an
        # ordinary filename character on POSIX, so one archive name would mean
        # two different things depending on who unpacked it.
        raise UpdateApplyError(
            "archive_backslash_member", "an archive member name contains a backslash"
        )
    if any(
        character in _RESERVED_IN_MEMBER or character < "\x20" for character in trimmed
    ):
        raise UpdateApplyError(
            "archive_reserved_member", "an archive member name contains a reserved character"
        )
    parts = trimmed.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateApplyError(
            "archive_traversal_member", "an archive member name escapes the archive"
        )
    return PurePosixPath(trimmed)


def _extract_safely(archive: zipfile.ZipFile, destination: Path) -> None:
    """Unpack one archive member by member, refusing everything unsafe.

    ``ZipFile.extractall`` is not used, and not because it fails to sanitise
    names -- it does -- but because sanitising is the wrong answer. It *rewrites*
    an absolute or traversing name into something harmless and unpacks it, so an
    archive that tried to escape becomes an archive that quietly got a different
    layout than the one its checksum inventory names. Here each member is judged
    and a bad one ends the extraction.

    What is refused: an absolute, traversing, backslash-bearing or
    reserved-character name; a symbolic link or any member that is not a regular
    file or a directory; a repeated name; more members than
    :data:`MAX_BUNDLE_MEMBERS`; a member above :data:`MAX_MEMBER_BYTES` or a
    total above :data:`MAX_EXPANDED_BYTES`, both counted from bytes actually
    written rather than from the sizes the archive declares.

    Every file is created ``O_EXCL`` and ``O_NOFOLLOW`` at ``0o600`` under
    directories created at ``0o700``, so nothing here follows a link that was
    already on disk and nothing is readable by another principal even briefly.
    """
    entries = archive.infolist()
    if len(entries) > MAX_BUNDLE_MEMBERS:
        raise UpdateApplyError(
            "archive_too_many_members",
            f"the bundle declares {len(entries)} members, above the {MAX_BUNDLE_MEMBERS} maximum",
        )
    seen: set[str] = set()
    expanded = 0
    _owner_only_directory(destination, below=destination.parent)
    for entry in entries:
        member = _safe_member_path(entry.filename)
        key = member.as_posix()
        if key in seen:
            raise UpdateApplyError(
                "archive_duplicate_member", "the bundle names one member twice"
            )
        seen.add(key)
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            # A symbolic link, a device, a FIFO or a socket. A link is the one
            # with teeth -- it is how a member that passed every name check above
            # still writes outside the root, by being the directory a later
            # member is written through -- and the rest have no business here.
            #
            # Zero is admitted, and it has to be: it means the archive states no
            # file type at all, which is what every member written by
            # ``ZipFile.writestr`` and by most non-POSIX producers looks like.
            # Refusing an absent type would refuse ordinary archives, and the
            # member is created here as a regular file regardless -- the type in
            # the archive is a claim, not an instruction.
            raise UpdateApplyError(
                "archive_special_member",
                "the bundle carries a symbolic link or a special file",
            )
        target = destination.joinpath(*member.parts)
        if entry.is_dir():
            _owner_only_directory(target, below=destination)
            continue
        _owner_only_directory(target.parent, below=destination)
        expanded = _write_member(archive, entry, target, expanded=expanded)


def _write_member(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path, *, expanded: int
) -> int:
    """Write one archive member owner-private and bounded; return the new total."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    written = 0
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as failure:
        raise UpdateApplyError("archive_unreadable", str(failure)) from failure
    try:
        with archive.open(entry) as source:
            while True:
                chunk = source.read(_EXTRACT_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_MEMBER_BYTES or expanded + written > MAX_EXPANDED_BYTES:
                    raise UpdateApplyError(
                        "archive_too_large",
                        "the bundle expands past its permitted size",
                    )
                os.write(descriptor, chunk)
    except (zipfile.BadZipFile, OSError, EOFError) as failure:
        raise UpdateApplyError("archive_unreadable", str(failure)) from failure
    finally:
        os.close(descriptor)
    if not restrict_to_owner(target, directory=False):
        raise UpdateApplyError(
            "staging_not_owner_only",
            "a staged file could not be made reachable by its owner alone",
        )
    return expanded + written


def stage_candidate(
    release_url: str,
    *,
    bundle_sha256: str | None,
    fetch_bytes: Callable[[str], bytes],
    installation_state: Path,
    operation_id: str,
    expected_packages: dict[str, str],
) -> Path:
    """Download and verify the approved candidate into the staging directory.

    Every failure raises before anything outside this directory is touched
    (A04): the running runtime is untouched by a failed download or
    verification.

    ``bundle_sha256`` is **required**. It is typed optional only because the
    legacy unsigned channel a development policy admits may omit it; the
    production path cannot reach here without one, and an archive this
    installation cannot identify before unpacking it is an archive it must not
    unpack.

    ``expected_packages`` is the channel's own per-package version map, not one
    release number repeated five times. The staged wheels are verified against
    the versions the channel actually recommended for each distribution.
    """
    if not bundle_sha256:
        raise UpdateApplyError(
            "bundle_digest_missing",
            "the channel states no bundle digest, so the archive cannot be identified",
        )
    staging = installation_state / STAGING_ROOT_NAME / operation_id
    _owner_only_directory(staging, below=installation_state)
    address = bundle_url(release_url)
    bundle = fetch_bytes(address)
    observed = "sha256:" + hashlib_sha256(bundle)
    if observed != bundle_sha256:
        raise UpdateApplyError(
            "bundle_checksum_mismatch",
            f"the downloaded bundle digest {observed} does not match the channel's {bundle_sha256}",
        )
    zip_path = staging / BUNDLE_ASSET_NAME
    zip_path.write_bytes(bundle)
    unpacked = staging / "bundle"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            _extract_safely(archive, unpacked)
    except (zipfile.BadZipFile, OSError) as failure:
        raise UpdateApplyError("bundle_unreadable", str(failure)) from failure
    _verify_inventory(unpacked, expected_packages=expected_packages)
    return staging


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _verify_inventory(unpacked: Path, *, expected_packages: dict[str, str]) -> None:
    """Hold the unpacked bundle against its own checksum inventory.

    The inventory is the candidate's, so every row is checked rather than only
    the rows that look like wheels: a metadata file whose digest does not match
    is a bundle that was altered, whatever the altered file was for. Rows are
    paths relative to the candidate root -- ``wheels/omnivia_core-…whl``, not a
    bare filename -- and they go through the same
    :func:`_safe_member_path` policy as archive members, because an inventory
    that can name ``../`` is an inventory that can send a digest check at a file
    outside the bundle and pass.

    Then the part the inventory cannot state about itself: that it covers the
    whole bundle. Coverage has to run in **both** directions, and the second one
    is the one with teeth. A file the inventory does not mention is a file
    nothing vouched for, and the bundle holds exactly one directory that is
    about to be handed to ``pip --find-links``: a wheel dropped into
    ``wheels/`` and left out of the inventory would be installable and
    unverified. The candidate builder enforces the same two-way coverage over
    the directory it writes, so a bundle that fails here is one that was altered
    after it was built.

    Last, the part no inventory states at all: that the five first-party wheels
    are present, parse as wheel filenames, name the five expected distributions,
    and carry the versions this channel recommended for each.
    """
    checksums = unpacked / CHECKSUMS_MEMBER
    if not checksums.is_file():
        raise UpdateApplyError(
            "checksums_missing", "the bundle carries no checksum inventory"
        )
    wheels = unpacked / WHEELS_DIRECTORY
    if not wheels.is_dir():
        raise UpdateApplyError("wheels_missing", "the bundle carries no wheels directory")

    observed_wheels: dict[str, str] = {}
    covered: set[str] = {CHECKSUMS_MEMBER}
    for row in checksums.read_text(encoding="utf-8").splitlines():
        if not row or row.startswith("#"):
            continue
        match = _CHECKSUM_ROW_RE.match(row)
        if match is None:
            raise UpdateApplyError(
                "checksums_malformed", "the checksum inventory carries a malformed row"
            )
        digest, relative = match.group(1), match.group(2)
        member = _safe_member_path(relative)
        path = unpacked.joinpath(*member.parts)
        if not path.is_file():
            raise UpdateApplyError(
                "inventory_member_missing", f"the bundle is missing {member.as_posix()}"
            )
        if hashlib_sha256(path.read_bytes()) != digest:
            raise UpdateApplyError(
                "inventory_digest_mismatch",
                f"{member.as_posix()} does not match the inventory digest",
            )
        covered.add(member.as_posix())
        if len(member.parts) == 2 and member.parts[0] == WHEELS_DIRECTORY:
            parsed = _wheel_distribution(member.parts[1])
            if parsed is not None:
                observed_wheels[parsed[0]] = parsed[1]

    unvouched = sorted(
        path.relative_to(unpacked).as_posix()
        for path in unpacked.rglob("*")
        if path.is_file() and path.relative_to(unpacked).as_posix() not in covered
    )
    if unvouched:
        raise UpdateApplyError(
            "inventory_incomplete",
            f"the bundle carries {len(unvouched)} file(s) its inventory does not "
            f"cover, the first being {unvouched[0]}",
        )

    for name in FIRST_PARTY_PACKAGES:
        if name not in observed_wheels:
            raise UpdateApplyError(
                "wheel_missing", f"the verified inventory carries no wheel for {name}"
            )
        approved = expected_packages.get(name)
        if approved is None:
            raise UpdateApplyError(
                "wheel_version_missing", f"the channel recommends no version for {name}"
            )
        if _version_key(observed_wheels[name]) != _version_key(approved):
            raise UpdateApplyError(
                "wheel_version_mismatch",
                f"{name} wheel version {observed_wheels[name]} is not the approved {approved}",
            )


def _wheel_distribution(filename: str) -> tuple[str, str] | None:
    """``(distribution, version)`` for a first-party wheel filename, else ``None``.

    ``None`` for a filename that is not a first-party wheel at all, which is an
    ordinary bundle member and not an error. A filename that *looks* like one and
    names a distribution outside the five is refused rather than ignored: it is
    the shape a wheel smuggled into the wheelhouse takes, and ``pip
    --find-links`` would be pointed at the directory holding it.
    """
    match = _WHEEL_NAME_RE.match(filename)
    if match is None:
        if filename.endswith(".whl"):
            raise UpdateApplyError(
                "wheel_name_unparseable",
                "the wheelhouse carries a wheel whose filename is not the built form",
            )
        return None
    distribution = _WHEEL_DISTRIBUTIONS.get(match.group(1))
    if distribution is None:
        raise UpdateApplyError(
            "wheel_unknown_distribution",
            "the wheelhouse carries a wheel for a distribution outside the five",
        )
    return distribution, match.group(2)


# --------------------------------------------------------------------------
# Affected services
# --------------------------------------------------------------------------


@dataclass(slots=True)
class AffectedService:
    workspace_id: str
    was_running: bool


def affected_services(
    installation_state: Path,
    *,
    probe_running: Callable[[str], bool],
) -> list[AffectedService]:
    """Every workspace served by this installation, and whether it is running.

    ``probe_running`` exists so tests never start a real service. The safe-stop
    gate is installation-scoped, not workspace-scoped (§7.3): stopping the
    displayed workspace's service alone would leave another workspace's
    service running from files being replaced.
    """
    workspaces = installation_state / "workspaces"
    affected: list[AffectedService] = []
    if not workspaces.is_dir():
        return affected
    for child in sorted(workspaces.iterdir()):
        if not child.is_dir():
            continue
        workspace_id = child.name
        affected.append(
            AffectedService(workspace_id=workspace_id, was_running=probe_running(workspace_id))
        )
    return affected


# --------------------------------------------------------------------------
# The worker (stdlib-only phase machine)
# --------------------------------------------------------------------------


def run_worker(instruction: dict[str, Any], *, runner: Callable[..., Any]) -> str:
    """Execute one approved update: stop, install, restart, verify, record.

    ``runner(command, **kwargs)`` is subprocess-shaped and injected; the
    production worker runs real commands. Returns the terminal outcome.
    """
    installation_state = Path(instruction["installation_state"])
    record = OperationRecord(
        operation_id=instruction["operation_id"],
        installation_ref=instruction["installation_ref"],
        approved_version=instruction["approved"]["version"],
        approved_bundle_sha256=instruction["approved"].get("bundle_sha256"),
        started_at=instruction["started_at"],
    )

    def advance(phase: str) -> None:
        record.advance(phase, installation_state)

    def fail(code: str, reason: str) -> str:
        record.outcome = "failed"
        record.failure_code = code
        record.finished_at = _now()
        record.advance("failed", installation_state)
        return "failed"

    advance("stopping")
    affected = [
        AffectedService(**service)
        for service in instruction["affected"]
        if service["was_running"]
    ]
    for service in affected:
        completed = runner(
            [
                instruction["cli_executable"],
                "--installation-state",
                instruction["installation_state"],
                "--workspace-id",
                service.workspace_id,
                "service",
                "stop",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            return fail("stop_failed", f"the {service.workspace_id} service did not stop")

    advance("installing")
    completed = runner(
        [
            instruction["python_executable"],
            "-m",
            "pip",
            "install",
            "--no-index",
            "--only-binary=:all:",
            "--find-links",
            instruction["staging_wheels"],
            *FIRST_PARTY_PACKAGES,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        return fail("install_failed", completed.stderr.strip()[-500:] or "pip failed")

    advance("restarting")
    for service in affected:
        completed = runner(
            [
                instruction["cli_executable"],
                "--installation-state",
                instruction["installation_state"],
                "--workspace-id",
                service.workspace_id,
                "service",
                "start",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            return fail("restart_failed", f"the {service.workspace_id} service did not restart")

    advance("verifying")
    completed = runner(
        [
            instruction["python_executable"],
            "-c",
            (
                "import importlib.metadata as m; import json, sys; "
                "names = tuple(sys.argv[1:]); "
                "print(json.dumps({n: m.version(n) for n in names}))"
            ),
            *FIRST_PARTY_PACKAGES,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        return fail("verify_failed", "the installation's package versions could not be read")
    try:
        observed = json.loads(completed.stdout)
    except ValueError:
        return fail("verify_failed", "the installation's package versions were unreadable")
    for name in FIRST_PARTY_PACKAGES:
        approved = instruction["approved"]["packages"][name]
        if name not in observed or _version_key(observed[name]) != _version_key(approved):
            return fail(
                "verify_failed",
                f"{name} reports {observed.get(name)!r}, not the approved {approved!r}",
            )

    record.outcome = "updated"
    record.finished_at = _now()
    record.advance("complete", installation_state)
    return "updated"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_operation_id() -> str:
    import uuid

    return f"upd-{uuid.uuid4()}"


@dataclass(slots=True)
class CoordinationOutcome:
    """The bounded result the CLI renders after coordinating one update."""

    returncode: int
    status: str
    reason: str | None
    operation: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "update_apply_adapter_version": 1,
            "product_id": PRODUCT_ID,
            "status": self.status,
        }
        if self.reason is not None:
            document["reason"] = self.reason
        if self.operation is not None:
            document["operation"] = self.operation
        return document


def coordinate_update(
    *,
    installation_state: Path,
    installed: dict[str, str],
    fetch_channel: Callable[[str], Any],
    fetch_bytes: Callable[[str], bytes],
    probe_running: Callable[[str], bool],
    confirm: Callable[[str], bool],
    spawn_worker: Callable[..., Any],
    python_executable: str,
    cli_executable: str,
    checked_at: str,
    policy: UpdateTrustPolicy = PRODUCTION_TRUST_POLICY,
) -> CoordinationOutcome:
    """One user-approved update, from check to verified outcome.

    The coordinator holds the installation lock from before the check resolves
    a candidate until the worker has inherited it; the worker's inherited
    descriptor keeps exclusive ownership through installation and verification
    with no hand-off gap (§7.2). Staging and verification happen before any
    service is stopped (§7.4); a refusal after staging is a no-change exit
    (§10) and leaves no operation record claiming failure.
    """
    lock = InstallationLock(installation_state)
    if not lock.acquire():
        existing = read_operation_record(installation_state)
        return CoordinationOutcome(
            returncode=1,
            status="update_already_running",
            reason="one update owns this installation at a time",
            operation=existing,
        )
    try:
        from omnivia_core_client.updates import check_for_updates

        check = check_for_updates(
            fetch_channel=fetch_channel,
            installed=installed,
            checked_at=checked_at,
            policy=policy,
        )
        if check.status != "update_available":
            return CoordinationOutcome(
                returncode=0 if check.status in {"up_to_date", "no_release", "ahead_of_channel"} else 1,
                status=check.status,
                reason=check.reason,
            )
        # The channel's own per-package map, carried through rather than
        # synthesised. A coordinator that wrote five copies of
        # ``candidate_version`` would verify the staged wheels -- and then the
        # installed versions -- against a version the channel never recommended
        # for four of the five distributions, which fails a legitimate release
        # whenever the five do not move in lockstep.
        approved_packages = dict(check.candidate_packages or {})
        if set(approved_packages) != set(FIRST_PARTY_PACKAGES):
            return CoordinationOutcome(
                returncode=1,
                status="check_failed",
                reason="the channel recommendation does not name all five first-party versions",
            )
        operation_id = new_operation_id()
        record = OperationRecord(
            operation_id=operation_id,
            installation_ref=str(installation_state),
            approved_version=check.candidate_version or "",
            approved_bundle_sha256=check.bundle_sha256,
            started_at=checked_at,
        )
        record.write(installation_state)
        try:
            staging = stage_candidate(
                check.release_url or "",
                bundle_sha256=check.bundle_sha256,
                fetch_bytes=fetch_bytes,
                installation_state=installation_state,
                operation_id=operation_id,
                expected_packages=approved_packages,
            )
        except (UpdateApplyError, ClientError, OSError, ValueError) as failure:
            # Everything the download and the unpack can raise, not only this
            # module's own refusal. A ``TransportError`` from the bundle fetch or
            # an ``OSError`` from the filesystem used to propagate out of the
            # coordinator and leave the journal reading ``phase: "preparing"``
            # for ever -- an operation that looks in flight and is not.
            code = failure.code if isinstance(failure, UpdateApplyError) else "bundle_unavailable"
            reason = failure.reason if isinstance(failure, UpdateApplyError) else str(failure)
            record.outcome = "blocked"
            record.failure_code = code
            record.finished_at = _now()
            record.advance("blocked", installation_state)
            return CoordinationOutcome(
                returncode=1,
                status="blocked",
                reason=reason,
                operation=record.to_wire(),
            )

        affected = affected_services(installation_state, probe_running=probe_running)
        summary = (
            f"Update omnivia-core to {check.candidate_version}?\n"
            f"  release: {check.release_url}\n"
            f"  bundle digest: {check.bundle_sha256 or 'not stated in the channel'}\n"
            f"  services to restart: "
            f"{', '.join(s.workspace_id for s in affected if s.was_running) or 'none running'}\n"
            "Proceed? [y/N] "
        )
        if not confirm(summary):
            # Cancellation before mutation: no-change exit, no failure record.
            (installation_state / RECORD_NAME).unlink(missing_ok=True)
            return CoordinationOutcome(
                returncode=0, status="cancelled", reason=None
            )

        instruction = {
            "operation_id": operation_id,
            "installation_ref": str(installation_state),
            "installation_state": str(installation_state),
            "approved": {
                "version": check.candidate_version,
                "bundle_sha256": check.bundle_sha256,
                "packages": approved_packages,
            },
            "affected": [
                {"workspace_id": service.workspace_id, "was_running": service.was_running}
                for service in affected
            ],
            "staging_wheels": str(staging / "bundle" / "wheels"),
            "python_executable": python_executable,
            "cli_executable": cli_executable,
            "started_at": checked_at,
        }
        instruction_path = staging / "instruction.json"
        instruction_path.write_text(json.dumps(instruction, indent=2) + "\n", encoding="utf-8")

        process = spawn_worker(
            [
                python_executable,
                "-m",
                "omnivia_core_client.updates_apply",
                "worker",
                str(instruction_path),
            ],
            pass_fds=(lock._fd,),
        )
        process.wait()
        outcome_record = read_operation_record(installation_state)
        outcome = (outcome_record or {}).get("outcome")
        if outcome == "updated":
            return CoordinationOutcome(
                returncode=0, status="updated", reason=None, operation=outcome_record
            )
        return CoordinationOutcome(
            returncode=1,
            status=outcome if isinstance(outcome, str) else "failed",
            reason=None,
            operation=outcome_record,
        )
    finally:
        lock.release()


def main_worker(argv: list[str]) -> int:
    """The detached worker's entry point: one instruction file, one outcome."""
    instruction = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    outcome = run_worker(instruction, runner=run_first_party_command)
    sys.stdout.write(f"{outcome}\n")
    return 0 if outcome == "updated" else 1


def main(argv: list[str] | None = None) -> int:
    """The worker's console entry: ``python -m … updates_apply worker <file>``."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "worker":
        return main_worker(arguments[1:])
    sys.stderr.write(
        "usage: python -m omnivia_core_client.updates_apply worker <instruction.json>\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
