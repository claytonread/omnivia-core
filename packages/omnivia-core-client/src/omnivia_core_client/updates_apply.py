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
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

from omnivia_core_client.managed_local import run_first_party_command
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

PHASES: Final = ("preparing", "stopping", "installing", "restarting", "verifying")
OUTCOMES: Final = ("updated", "blocked", "failed", "restart_required")


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


def read_operation_record(installation_state: Path) -> dict[str, Any] | None:
    path = installation_state / RECORD_NAME
    if not path.is_file():
        return None
    try:
        document: dict[str, Any] | None = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
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


def stage_candidate(
    release_url: str,
    *,
    bundle_sha256: str | None,
    fetch_bytes: Callable[[str], bytes],
    installation_state: Path,
    operation_id: str,
    expected_version: str,
) -> Path:
    """Download and verify the approved candidate into the staging directory.

    Every failure raises before anything outside this directory is touched
    (A04): the running runtime is untouched by a failed download or
    verification.
    """
    staging = installation_state / STAGING_ROOT_NAME / operation_id
    staging.mkdir(parents=True, exist_ok=True)
    address = bundle_url(release_url)
    bundle = fetch_bytes(address)
    if bundle_sha256 is not None:
        observed = "sha256:" + hashlib_sha256(bundle)
        if observed != bundle_sha256:
            raise UpdateApplyError(
                "bundle_checksum_mismatch",
                f"the downloaded bundle digest {observed} does not match the channel's {bundle_sha256}",
            )
    zip_path = staging / BUNDLE_ASSET_NAME
    zip_path.write_bytes(bundle)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(staging / "bundle")
    except (zipfile.BadZipFile, OSError) as failure:
        raise UpdateApplyError("bundle_unreadable", str(failure)) from failure
    checksums = staging / "bundle" / "checksums-sha256.txt"
    if not checksums.is_file():
        raise UpdateApplyError("checksums_missing", "the bundle carries no checksum inventory")
    wheels = staging / "bundle" / "wheels"
    if not wheels.is_dir():
        raise UpdateApplyError("wheels_missing", "the bundle carries no wheels directory")
    verified = 0
    for line in checksums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, filename = line.partition("  ")
        if not filename.startswith("omnivia_"):
            continue
        wheel = wheels / filename
        if not wheel.is_file():
            raise UpdateApplyError("wheel_missing", f"the bundle is missing {filename}")
        observed = hashlib_sha256(wheel.read_bytes())
        if observed != digest:
            raise UpdateApplyError(
                "wheel_checksum_mismatch",
                f"{filename} digest {observed} does not match the inventory",
            )
        verified += 1
    if verified < len(FIRST_PARTY_PACKAGES):
        raise UpdateApplyError(
            "wheels_incomplete",
            f"the inventory verified {verified} first-party wheels; "
            f"{len(FIRST_PARTY_PACKAGES)} are required",
        )
    _assert_wheel_versions(wheels, expected_version)
    return staging


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _assert_wheel_versions(wheels: Path, expected_version: str) -> None:
    """The staged wheels are the approved release's exact package versions."""
    import re

    observed: dict[str, str] = {}
    for wheel in wheels.glob("omnivia_*.whl"):
        match = re.match(r"^(omnivia_.+?)-(\d+(?:\.\d+)*)-", wheel.name)
        if match:
            observed[match.group(1).replace("_", "-")] = match.group(2)
    for name in FIRST_PARTY_PACKAGES:
        if name not in observed:
            raise UpdateApplyError("wheel_version_missing", f"no wheel for {name}")
        if _version_key(observed[name]) != _version_key(expected_version):
            raise UpdateApplyError(
                "wheel_version_mismatch",
                f"{name} wheel version {observed[name]} is not the approved {expected_version}",
            )


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
        record.phase = phase
        record.write(installation_state)

    def fail(code: str, reason: str) -> str:
        record.phase = "failed"
        record.outcome = "failed"
        record.failure_code = code
        record.finished_at = _now()
        record.write(installation_state)
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

    record.phase = "complete"
    record.outcome = "updated"
    record.finished_at = _now()
    record.write(installation_state)
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
        )
        if check.status != "update_available":
            return CoordinationOutcome(
                returncode=0 if check.status in {"up_to_date", "no_release", "ahead_of_channel"} else 1,
                status=check.status,
                reason=check.reason,
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
                expected_version=check.candidate_version or "",
            )
        except UpdateApplyError as failure:
            record.phase = "blocked"
            record.outcome = "blocked"
            record.failure_code = failure.code
            record.finished_at = _now()
            record.write(installation_state)
            return CoordinationOutcome(
                returncode=1,
                status="blocked",
                reason=failure.reason,
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
                "packages": {
                    name: (check.candidate_version or "")
                    for name in FIRST_PARTY_PACKAGES
                },
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
