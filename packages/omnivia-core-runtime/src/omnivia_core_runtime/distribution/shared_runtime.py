"""Deterministic shared-runtime selection and consumer-safe bookkeeping.

The installation manager owns executable payload selection, never Workspace
data. Standalone Core and Platform write independent receipts and converge on
the highest installed runtime that satisfies every live consumer's minimum
version. Removing one receipt cannot remove a payload referenced by another,
the active payload, or the previous known-good payload.

All paths are under an explicit installation root. This module never reads a
home directory, follows a consumer-provided symlink, or executes a payload.

Selection is deterministic; it is not trust. ``trusted_runtime`` beside this
module owns that half -- it verifies the release signature and the complete file
inventory of a payload -- and the two meet in exactly two places: installation
verifies and hardens a candidate before publishing it, and
``trusted_runtime.resolve_runtime`` re-reads the ``active.json`` this module
writes as an untrusted selector and reverifies the candidate it names.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from omnivia_core_runtime.distribution.trusted_runtime import (
    RuntimeResolutionError,
    TrustAnchor,
    harden_payload,
    unharden_tree,
    verify_payload,
)

COMPANION_BUNDLE_ID: Final = "com.omnivia.core.status"
_SCHEMA_VERSION: Final = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_AT_FDCWD: Final = -100
_RENAME_NOREPLACE: Final = 1
_RENAME_EXCL: Final = 0x00000004


class DistributionError(Exception):
    """A fixed, payload-free distribution refusal."""


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    schema_version: int
    release_version: str
    payload_digest: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class ConsumerReceipt:
    schema_version: int
    consumer_id: str
    consumer_payload_digest: str
    minimum_core_version: str


def canonical_macos_paths(user_home: Path) -> tuple[Path, Path]:
    """Return the frozen per-user Core root and companion app paths."""
    if not user_home.is_absolute():
        raise DistributionError("distribution path refused")
    return (
        user_home / "Library" / "Application Support" / "OmniVia" / "Core",
        user_home / "Applications" / "OmniVia Core.app",
    )


def _version(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise DistributionError("distribution version refused")
    return tuple(int(member) for member in match.groups())  # type: ignore[return-value]


def _digest(value: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise DistributionError("distribution digest refused")
    return value


def _identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise DistributionError("distribution identity refused")
    return value


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically move a directory only when `destination` is absent.

    Plain POSIX ``rename``/``os.replace`` may delete an existing empty directory,
    so a check before the call cannot uphold the no-overwrite invariant: another
    process can create the destination between the check and the rename. The three
    supported systems each provide a no-clobber primitive. An unsupported system or
    missing primitive fails closed rather than falling back to a replacing rename.
    """
    if os.name == "nt":
        # Windows os.rename uses MoveFileExW without MOVEFILE_REPLACE_EXISTING and
        # therefore refuses any existing destination.
        os.rename(source, destination)
        return

    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    try:
        if sys.platform == "darwin":
            operation = library.renamex_np
            operation.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            operation.restype = ctypes.c_int
            result = operation(source_bytes, destination_bytes, _RENAME_EXCL)
        elif sys.platform.startswith("linux"):
            operation = library.renameat2
            operation.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            operation.restype = ctypes.c_int
            result = operation(
                _AT_FDCWD,
                source_bytes,
                _AT_FDCWD,
                destination_bytes,
                _RENAME_NOREPLACE,
            )
        else:
            raise AttributeError
    except AttributeError as failure:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory rename is unavailable",
            destination,
        ) from failure
    if result != 0:
        failure_number = ctypes.get_errno()
        raise OSError(failure_number, os.strerror(failure_number), destination)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise DistributionError("distribution record refused")
        value = json.loads(raw)
    except (OSError, ValueError, RecursionError) as error:
        raise DistributionError("distribution record refused") from error
    if not isinstance(value, dict):
        raise DistributionError("distribution record refused")
    return value


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _refuse_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise DistributionError("distribution payload refused")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise DistributionError("distribution payload refused")


class SharedRuntimeInstallation:
    """One explicit shared Core installation root."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise DistributionError("distribution path refused")
        self.root = root
        self.runtimes = root / "runtimes"
        self.candidates = root / "candidates"
        self.receipts = root / "receipts"
        self.active_record = root / "active.json"
        self.previous_record = root / "previous-known-good.json"

    def initialise(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise DistributionError("distribution path refused")
        os.chmod(self.root, 0o700)
        for directory in (self.runtimes, self.candidates, self.receipts):
            directory.mkdir(exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise DistributionError("distribution path refused")
            os.chmod(directory, 0o700)

    def install_candidate(
        self,
        source: Path,
        *,
        trust_anchors: Sequence[TrustAnchor],
        verification_time: datetime,
        release_version: str | None = None,
        payload_digest: str | None = None,
    ) -> CandidateRecord:
        """Verify, harden and atomically publish one immutable payload directory.

        **The release version and the payload identity are read out of the signed
        manifest, not out of the arguments.** That inversion is the point of this
        change: the previous signature took ``payload_digest`` and placed the tree
        at ``runtimes/<version>/<that digest>/`` without ever computing it, so an
        installer that lied about the digest produced a candidate whose directory
        name said one thing and whose contents were another -- and every later
        consumer inherited that claim. ``release_version`` and ``payload_digest``
        survive as *optional equality guards* for callers that already knew what
        they were installing; a mismatch is a refusal, and neither is ever the
        computed value.

        The sequence is fixed, and each step is where it is because the one before
        it must have held:

        1. copy the source into a staging directory beside the runtimes, refusing
           every symlink on the way in and out;
        2. verify the staged payload completely -- signature, identity, inventory,
           digests -- and learn the release version and identity from it;
        3. check the caller's guards against what was computed;
        4. move the staged tree into place, having first refused to move onto
           anything already at that content-derived name -- and, when something is
           there, **verify those bytes too** before adopting them;
        5. harden it to non-writable;
        6. only then publish the candidate index, unless one is already published,
           in which case it must name exactly this record.

        **Nothing existing is ever adopted on the strength of its name.** Step 4 is
        where the previous version was wrong twice. A *published* destination was
        accepted after ``destination.is_dir()`` and an index comparison, so bytes
        that had rotted or been rewritten under a correct directory name installed
        cleanly and every later consumer inherited them. An *unindexed* destination
        was accepted after the same one-line check, so a half-written or corrupt
        orphan tree was hardened and then published as if this call had staged it.
        Both now go through :func:`verify_payload` against the identity and release
        this call computed, and a tree that fails is a conflict: never adopted, never
        overwritten and never removed, because bytes nobody can explain are evidence.

        **The move itself was the third way.** Adoption was reached only from a
        *failed* replacing rename, and on POSIX renaming a directory onto an **empty**
        one does not fail -- ``rename(2)`` removes it. So an empty directory at the
        content-derived name, which is what an interrupted removal or a restored
        backup leaves, was silently deleted and taken over on POSIX while the
        identical call refused on Windows. The move now uses the host's atomic
        no-clobber primitive; its conflict path covers both a durable occupant and a
        concurrent installer without a check-then-rename window.

        **Step 6 is the publication, and that is why hardening can follow the move.**
        A candidate is selectable only through ``candidates/<identity>.json``:
        ``_installed_candidates`` reads that directory and nothing else, so until the
        index is written the tree under ``runtimes/`` is unreachable by every path in
        this class and by ``resolve_runtime``, which reaches a candidate only through
        the ``active.json`` those records produce. The window between the move and the
        hardening exposes nothing.

        Hardening *before* the move was tried first and does not work: renaming a
        directory into a new parent updates its ``..`` entry, so POSIX requires write
        permission on the directory being moved, and a payload root already chmodded
        to ``0500`` cannot be renamed at all.

        **A failure removes this call's staging tree and nothing else.** It used to
        also remove the destination whenever the rename had succeeded and publication
        had not, on the reasoning that no index named it yet -- but the index is
        shared, and another installer of this same identity converges on exactly that
        tree and publishes it. Deleting it on the way out of a local failure deleted a
        payload another process had already told its consumers about. What is left
        behind instead is an unindexed tree whose contents this call verified, which
        is the orphan the next run reconciles by writing the record that is missing.
        """
        self.initialise()
        if release_version is not None:
            _version(release_version)
        if payload_digest is not None:
            _digest(payload_digest)
        if not source.is_absolute() or not source.is_dir():
            raise DistributionError("distribution payload refused")
        _refuse_symlinks(source)

        staging = self.runtimes / f".staging.{uuid.uuid4().hex}"
        try:
            shutil.copytree(source, staging, symlinks=False)
            _refuse_symlinks(staging)
            verified = verify_payload(
                staging,
                trust_anchors=trust_anchors,
                verification_time=verification_time,
                enforce_installation_policy=False,
            )
            computed = verified.payload_identity.removeprefix("sha256:")
            if (
                release_version is not None
                and release_version != verified.release_version
            ):
                raise DistributionError("distribution candidate refused")
            if payload_digest is not None and payload_digest != computed:
                raise DistributionError("distribution candidate refused")

            relative = Path("runtimes") / verified.release_version / computed
            destination = self.root / relative
            record = CandidateRecord(
                schema_version=_SCHEMA_VERSION,
                release_version=verified.release_version,
                payload_digest=computed,
                relative_path=relative.as_posix(),
            )
            index = self.candidates / f"{computed}.json"

            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # The native no-clobber move is the arbitration point: it either moves
            # this complete staged tree or leaves every occupant untouched, including
            # an empty directory or one created at the same instant by another
            # process. Only an actual name conflict enters the convergence path;
            # storage and permission failures retain their original OSError.
            occupied = False
            try:
                _rename_no_replace(staging, destination)
            except OSError as failure:
                if failure.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                if not os.path.lexists(destination):
                    raise
                occupied = True
            if occupied:
                # A name derived from content is a *claim* about those bytes and not
                # proof of them, so whatever is there is verified against the identity
                # and release this call just computed before any of it is adopted.
                self._verified_destination(
                    destination,
                    identity=verified.payload_identity,
                    release_version=verified.release_version,
                    trust_anchors=trust_anchors,
                    verification_time=verification_time,
                )
            # Unconditionally, and idempotently: chmod is the same work whichever
            # installer moved the tree, and a converging caller that skipped it would
            # publish an index for a payload whose hardening the other one had not
            # finished. Resolution enforces the mode policy either way, so the cost of
            # skipping is a refusal at launch rather than a silent hole -- but a
            # refusal nobody can repair without reinstalling is worth this much.
            harden_payload(destination, verified.inventory)

            if index.exists():
                # Published already -- by an earlier install of this payload or by an
                # installer that converged while this call was between its own two
                # steps. The tree behind it has been verified above, so all that is
                # left is that the record agrees; one that does not is a conflict and
                # is refused rather than rewritten.
                existing = self._candidate(index)
                if existing != record:
                    raise DistributionError("distribution candidate conflict")
                return existing
            _atomic_json(index, asdict(record))
        finally:
            # This call's staging copy, and only ever that -- gone already if the
            # rename took it. `finally` rather than `except` because the branch above
            # *returns* rather than raising and would otherwise leave a staging copy
            # behind on every repeat installation. The destination is deliberately not
            # here: see the note about the shared index in the docstring.
            if staging.exists():
                unharden_tree(staging)
                shutil.rmtree(staging, ignore_errors=True)
        return record

    def _verified_destination(
        self,
        destination: Path,
        *,
        identity: str,
        release_version: str,
        trust_anchors: Sequence[TrustAnchor],
        verification_time: datetime,
    ) -> None:
        """Refuse unless `destination` is exactly the payload this call verified.

        The full verifier, not a directory check: signature, computed identity,
        complete inventory and every digest, pinned to the identity and release the
        staged copy produced. Anything else -- different bytes under a correct name,
        a truncated orphan, an empty directory, a symlink pointing out of the
        installation -- is a conflict, and a conflict is left exactly where it is. Overwriting it would
        destroy the evidence, and removing it would race whichever installer is
        publishing it right now.

        The installation mode policy is deliberately not enforced here. A converging
        installer reaches this while the winner is still chmodding, so a half-hardened
        tree is an expected state rather than a fault; `harden_payload` runs
        immediately after and resolution enforces the policy at every launch.
        """
        try:
            verify_payload(
                destination,
                trust_anchors=trust_anchors,
                verification_time=verification_time,
                expected_payload_identity=identity,
                expected_release_version=release_version,
                enforce_installation_policy=False,
            )
        except RuntimeResolutionError as refusal:
            raise DistributionError("distribution candidate conflict") from refusal

    def register_consumer(
        self,
        *,
        consumer_id: str,
        consumer_payload_digest: str,
        minimum_core_version: str,
    ) -> CandidateRecord:
        """Write one consumer receipt and activate the deterministic selection."""
        self.initialise()
        receipt = ConsumerReceipt(
            schema_version=_SCHEMA_VERSION,
            consumer_id=_identifier(consumer_id),
            consumer_payload_digest=_digest(consumer_payload_digest),
            minimum_core_version=minimum_core_version,
        )
        _version(receipt.minimum_core_version)
        prospective = self.list_receipts(excluding=receipt.consumer_id) + [receipt]
        selected = self._select(prospective)
        _atomic_json(self.receipts / f"{receipt.consumer_id}.json", asdict(receipt))
        self._activate(selected)
        return selected

    def unregister_consumer(self, consumer_id: str) -> CandidateRecord | None:
        """Remove only this consumer's receipt; never remove payload or Workspace data."""
        self.initialise()
        receipt_path = self.receipts / f"{_identifier(consumer_id)}.json"
        try:
            receipt_path.unlink()
        except FileNotFoundError:
            pass
        receipts = self.list_receipts()
        if not receipts:
            return self.active()
        selected = self._select(receipts)
        self._activate(selected)
        return selected

    def reconcile(self) -> CandidateRecord | None:
        """Repair active selection after an interrupted receipt/activation sequence."""
        self.initialise()
        receipts = self.list_receipts()
        if not receipts:
            return self.active()
        selected = self._select(receipts)
        self._activate(selected)
        return selected

    def active(self) -> CandidateRecord | None:
        if not self.active_record.exists():
            return None
        return self._candidate_document(_read_json(self.active_record))

    def previous_known_good(self) -> CandidateRecord | None:
        if not self.previous_record.exists():
            return None
        return self._candidate_document(_read_json(self.previous_record))

    def list_receipts(self, *, excluding: str | None = None) -> list[ConsumerReceipt]:
        if not self.receipts.exists():
            return []
        found: list[ConsumerReceipt] = []
        for path in sorted(self.receipts.glob("*.json")):
            document = _read_json(path)
            if set(document) != {
                "schema_version",
                "consumer_id",
                "consumer_payload_digest",
                "minimum_core_version",
            }:
                raise DistributionError("distribution receipt refused")
            receipt = ConsumerReceipt(
                schema_version=document["schema_version"],
                consumer_id=_identifier(document["consumer_id"]),
                consumer_payload_digest=_digest(document["consumer_payload_digest"]),
                minimum_core_version=document["minimum_core_version"],
            )
            if receipt.schema_version != _SCHEMA_VERSION:
                raise DistributionError("distribution receipt refused")
            _version(receipt.minimum_core_version)
            if path.stem != receipt.consumer_id:
                raise DistributionError("distribution receipt refused")
            if receipt.consumer_id != excluding:
                found.append(receipt)
        return found

    def garbage_collectable_candidates(self) -> tuple[CandidateRecord, ...]:
        """Return unreferenced payloads; deletion is a separate explicit operation."""
        protected = {
            candidate.payload_digest
            for candidate in (self.active(), self.previous_known_good())
            if candidate is not None
        }
        protected.update(
            receipt.consumer_payload_digest for receipt in self.list_receipts()
        )
        return tuple(
            candidate
            for candidate in self._installed_candidates()
            if candidate.payload_digest not in protected
        )

    def _select(self, receipts: list[ConsumerReceipt]) -> CandidateRecord:
        minimum = max(
            (_version(receipt.minimum_core_version) for receipt in receipts),
            default=(0, 0, 0),
        )
        eligible = [
            candidate
            for candidate in self._installed_candidates()
            if _version(candidate.release_version) >= minimum
        ]
        if not eligible:
            raise DistributionError("no compatible shared runtime")
        return max(
            eligible,
            key=lambda value: (_version(value.release_version), value.payload_digest),
        )

    def _activate(self, selected: CandidateRecord) -> None:
        current = self.active()
        if current == selected:
            return
        if current is not None:
            _atomic_json(self.previous_record, asdict(current))
        _atomic_json(self.active_record, asdict(selected))

    def _installed_candidates(self) -> list[CandidateRecord]:
        if not self.candidates.exists():
            return []
        return [
            self._candidate(path) for path in sorted(self.candidates.glob("*.json"))
        ]

    def _candidate(self, path: Path) -> CandidateRecord:
        return self._candidate_document(_read_json(path))

    def _candidate_document(self, document: dict[str, Any]) -> CandidateRecord:
        if set(document) != {
            "schema_version",
            "release_version",
            "payload_digest",
            "relative_path",
        }:
            raise DistributionError("distribution candidate refused")
        record = CandidateRecord(
            schema_version=document["schema_version"],
            release_version=document["release_version"],
            payload_digest=_digest(document["payload_digest"]),
            relative_path=document["relative_path"],
        )
        if record.schema_version != _SCHEMA_VERSION:
            raise DistributionError("distribution candidate refused")
        _version(record.release_version)
        expected = (
            Path("runtimes") / record.release_version / record.payload_digest
        ).as_posix()
        if record.relative_path != expected:
            raise DistributionError("distribution candidate refused")
        payload = self.root / record.relative_path
        if not payload.is_dir() or payload.is_symlink():
            raise DistributionError("distribution candidate refused")
        return record
