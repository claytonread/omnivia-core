"""Deterministic shared-runtime selection and consumer-safe bookkeeping.

The installation manager owns executable payload selection, never Workspace
data. Standalone Core and Platform write independent receipts and converge on
the highest installed runtime that satisfies every live consumer's minimum
version. Removing one receipt cannot remove a payload referenced by another,
the active payload, or the previous known-good payload.

All paths are under an explicit installation root. This module never reads a
home directory, follows a consumer-provided symlink, or executes a payload.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

COMPANION_BUNDLE_ID: Final = "com.omnivia.core.status"
_SCHEMA_VERSION: Final = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


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
        self, source: Path, *, release_version: str, payload_digest: str
    ) -> CandidateRecord:
        """Atomically install one already-qualified immutable payload directory."""
        self.initialise()
        _version(release_version)
        _digest(payload_digest)
        if not source.is_absolute() or not source.is_dir():
            raise DistributionError("distribution payload refused")
        _refuse_symlinks(source)

        relative = Path("runtimes") / release_version / payload_digest
        destination = self.root / relative
        record = CandidateRecord(
            schema_version=_SCHEMA_VERSION,
            release_version=release_version,
            payload_digest=payload_digest,
            relative_path=relative.as_posix(),
        )
        index = self.candidates / f"{payload_digest}.json"
        if destination.exists() or index.exists():
            existing = self._candidate(index)
            if existing != record or not destination.is_dir() or destination.is_symlink():
                raise DistributionError("distribution candidate conflict")
            return existing

        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = destination.parent / f".{payload_digest}.{uuid.uuid4().hex}.staging"
        try:
            shutil.copytree(source, staging, symlinks=False)
            _refuse_symlinks(staging)
            os.replace(staging, destination)
            _atomic_json(index, asdict(record))
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return record

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
        protected.update(receipt.consumer_payload_digest for receipt in self.list_receipts())
        return tuple(
            candidate
            for candidate in self._installed_candidates()
            if candidate.payload_digest not in protected
        )

    def _select(self, receipts: list[ConsumerReceipt]) -> CandidateRecord:
        minimum = max((_version(receipt.minimum_core_version) for receipt in receipts), default=(0, 0, 0))
        eligible = [
            candidate
            for candidate in self._installed_candidates()
            if _version(candidate.release_version) >= minimum
        ]
        if not eligible:
            raise DistributionError("no compatible shared runtime")
        return max(eligible, key=lambda value: (_version(value.release_version), value.payload_digest))

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
        return [self._candidate(path) for path in sorted(self.candidates.glob("*.json"))]

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
        expected = (Path("runtimes") / record.release_version / record.payload_digest).as_posix()
        if record.relative_path != expected:
            raise DistributionError("distribution candidate refused")
        payload = self.root / record.relative_path
        if not payload.is_dir() or payload.is_symlink():
            raise DistributionError("distribution candidate refused")
        return record
