"""Portable workspace manifest contracts (T-0629A, ADR-037).

Public Core owns only portable values here. The manifest carries stable identity
and format information and nothing else: no absolute path, no installation or
process identity, no open history, no endpoint, no credential, secret or key
material. Those live in installation-local runtime state, not in the portable
workspace, because a workspace must survive being copied to another machine
without carrying anything about the machine that made it.

This module is pure. It performs no filesystem access. `WorkspaceCreate` in
`omnivia_core.workspace.models` is the older non-portable compatibility model and
resolves real paths; the two are deliberately separate and must not be mixed.
See the Stream B B0 review, finding F-06.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import fields as _dataclass_fields
from typing import Any

from omnivia_core._shared.validation import (
    ValidationResult,
    scan_sensitive_fields,
    validate_iso_timestamp,
    validate_optional_iso_timestamp,
)

#: Schema version of the manifest document itself. Distinct from the workspace
#: format version: the manifest envelope and the on-disk workspace layout evolve
#: independently.
MANIFEST_VERSION = "1"

#: Checksum algorithm for `IntegrityMetadata.canonical_checksum`.
CHECKSUM_ALGORITHM = "sha256"

#: Field names the portable manifest must never carry, beyond the shared
#: sensitive-key set. These are machine-local or history values that would make a
#: copied workspace describe the machine that created it.
NON_PORTABLE_KEYS = frozenset(
    {
        "endpoint",
        "hostname",
        "installation_id",
        "last_opened_at",
        "open_history",
        "os_principal",
        "pid",
        "process_start",
        "root_path",
        "service_instance_id",
        "storage_path",
    }
)


def canonical_json(payload: Any) -> str:
    """Canonical JSON for checksum and byte-stability purposes.

    Sorted keys, no insignificant whitespace, UTF-8 preserved. Two manifests with
    equal values must produce equal bytes on every machine, or the checksum in
    `IntegrityMetadata` means nothing.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksum_of(payload: Any) -> str:
    """SHA-256 over the canonical JSON encoding of `payload`."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoreCompatibility:
    """The Core version window a workspace declares it can be opened by."""

    workspace_format_version: str
    min_core_version: str
    #: Exclusive upper bound. `None` means "no known upper bound".
    max_core_version_exclusive: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_format_version": self.workspace_format_version,
            "min_core_version": self.min_core_version,
            "max_core_version_exclusive": self.max_core_version_exclusive,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoreCompatibility:
        return cls(
            workspace_format_version=str(data["workspace_format_version"]),
            min_core_version=str(data["min_core_version"]),
            max_core_version_exclusive=(
                None
                if data.get("max_core_version_exclusive") is None
                else str(data["max_core_version_exclusive"])
            ),
        )


@dataclass(frozen=True)
class ProjectionDeclaration:
    """A derived index or projection held inside the workspace.

    `rebuildable` is the load-bearing field: a projection that can be rebuilt from
    canonical data may be discarded on an incompatible open, and one that cannot
    must block it. ADR-037's requirement that projection loss never causes
    canonical-data loss depends on this being declared, not inferred.
    """

    projection_id: str
    version: str
    rebuildable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "version": self.version,
            "rebuildable": self.rebuildable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectionDeclaration:
        return cls(
            projection_id=str(data["projection_id"]),
            version=str(data["version"]),
            rebuildable=bool(data["rebuildable"]),
        )


@dataclass(frozen=True)
class EncryptionMetadata:
    """How a workspace is encrypted — never the material needed to decrypt it.

    Algorithm and key-derivation identifiers are portable facts about the format.
    Keys, passphrases, salts and wrapped key blobs are not carried here at all,
    and `validate_manifest` rejects a manifest that tries to.
    """

    algorithm: str
    key_derivation: str
    #: Opaque, non-secret identifier for the key the workspace expects, so a
    #: reader can tell "wrong key" from "corrupt data" without holding any key.
    key_reference: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "key_derivation": self.key_derivation,
            "key_reference": self.key_reference,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EncryptionMetadata:
        return cls(
            algorithm=str(data["algorithm"]),
            key_derivation=str(data["key_derivation"]),
            key_reference=(
                None if data.get("key_reference") is None else str(data["key_reference"])
            ),
        )


@dataclass(frozen=True)
class MigrationSummary:
    """What the last format migration did, as a portable record.

    The legacy source is recorded as a *fingerprint*, never as a path. A path
    would describe the machine that ran the migration and would break portability;
    the fingerprint is what actually identifies the source data.
    """

    from_format_version: str
    to_format_version: str
    migrated_at: str
    attempt_id: str
    source_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_format_version": self.from_format_version,
            "to_format_version": self.to_format_version,
            "migrated_at": self.migrated_at,
            "attempt_id": self.attempt_id,
            "source_fingerprint": self.source_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationSummary:
        return cls(
            from_format_version=str(data["from_format_version"]),
            to_format_version=str(data["to_format_version"]),
            migrated_at=str(data["migrated_at"]),
            attempt_id=str(data["attempt_id"]),
            source_fingerprint=(
                None
                if data.get("source_fingerprint") is None
                else str(data["source_fingerprint"])
            ),
        )


@dataclass(frozen=True)
class IntegrityMetadata:
    """Checksum covering the manifest's own meaningful content.

    `canonical_checksum` is computed over the canonical JSON of the manifest with
    the `integrity` block removed. Including the block would be self-referential:
    writing the checksum would change the bytes the checksum covers. This mirrors
    the decision taken for the Foundation pack manifest, which declares no digest
    for its own entry for the same reason.
    """

    canonical_checksum: str
    algorithm: str = CHECKSUM_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return {"canonical_checksum": self.canonical_checksum, "algorithm": self.algorithm}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntegrityMetadata:
        return cls(
            canonical_checksum=str(data["canonical_checksum"]),
            algorithm=str(data.get("algorithm", CHECKSUM_ALGORITHM)),
        )


@dataclass(frozen=True)
class WorkspaceManifest:
    """The portable `workspace.json` document.

    `workspace_id` is the workspace's identity and is independent of where the
    workspace sits on disk, which is what makes identity stable across a move.
    """

    workspace_id: str
    created_at: str
    compatibility: CoreCompatibility
    manifest_version: str = MANIFEST_VERSION
    name: str | None = None
    projections: tuple[ProjectionDeclaration, ...] = ()
    encryption: EncryptionMetadata | None = None
    migration: MigrationSummary | None = None
    integrity: IntegrityMetadata | None = None

    def to_dict(self, *, include_integrity: bool = True) -> dict[str, Any]:
        """Serialise to a plain mapping.

        `include_integrity=False` yields exactly the payload the checksum covers.
        """
        payload: dict[str, Any] = {
            "manifest_version": self.manifest_version,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at,
            "name": self.name,
            "compatibility": self.compatibility.to_dict(),
            "projections": [p.to_dict() for p in self.projections],
            "encryption": None if self.encryption is None else self.encryption.to_dict(),
            "migration": None if self.migration is None else self.migration.to_dict(),
        }
        if include_integrity:
            payload["integrity"] = (
                None if self.integrity is None else self.integrity.to_dict()
            )
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceManifest:
        projections = tuple(
            ProjectionDeclaration.from_dict(item) for item in data.get("projections") or ()
        )
        return cls(
            workspace_id=str(data["workspace_id"]),
            created_at=str(data["created_at"]),
            compatibility=CoreCompatibility.from_dict(data["compatibility"]),
            manifest_version=str(data.get("manifest_version", MANIFEST_VERSION)),
            name=None if data.get("name") is None else str(data["name"]),
            projections=projections,
            encryption=(
                None
                if data.get("encryption") is None
                else EncryptionMetadata.from_dict(data["encryption"])
            ),
            migration=(
                None
                if data.get("migration") is None
                else MigrationSummary.from_dict(data["migration"])
            ),
            integrity=(
                None
                if data.get("integrity") is None
                else IntegrityMetadata.from_dict(data["integrity"])
            ),
        )

    def compute_checksum(self) -> str:
        """Checksum over this manifest's content, excluding the integrity block."""
        return checksum_of(self.to_dict(include_integrity=False))

    def with_integrity(self) -> WorkspaceManifest:
        """Return a copy carrying a freshly computed integrity block."""
        return WorkspaceManifest(
            workspace_id=self.workspace_id,
            created_at=self.created_at,
            compatibility=self.compatibility,
            manifest_version=self.manifest_version,
            name=self.name,
            projections=self.projections,
            encryption=self.encryption,
            migration=self.migration,
            integrity=IntegrityMetadata(canonical_checksum=self.compute_checksum()),
        )

    def canonical_bytes(self) -> bytes:
        """Byte-stable encoding, suitable for atomic write."""
        return (canonical_json(self.to_dict()) + "\n").encode("utf-8")

    def integrity_matches(self) -> bool:
        """Whether the declared checksum matches the manifest's own content."""
        if self.integrity is None:
            return False
        return self.integrity.canonical_checksum == self.compute_checksum()


def _serialised_keys(cls: type) -> frozenset[str]:
    """The keys a model emits, taken from the model itself.

    Derived rather than listed, so adding a field cannot leave a hand-maintained
    allow-list behind: a listed set that drifts would start refusing valid
    manifests, or worse, keep accepting a field the model no longer reads.
    """
    return frozenset(field.name for field in _dataclass_fields(cls))


_MANIFEST_KEYS = _serialised_keys(WorkspaceManifest)
_PROJECTION_KEYS = _serialised_keys(ProjectionDeclaration)
_NESTED_KEYS = {
    "compatibility": _serialised_keys(CoreCompatibility),
    "encryption": _serialised_keys(EncryptionMetadata),
    "migration": _serialised_keys(MigrationSummary),
    "integrity": _serialised_keys(IntegrityMetadata),
}


def validate_raw_manifest(data: Any) -> ValidationResult:
    """Validate the manifest document as it was actually stored.

    `validate_manifest` sees a `WorkspaceManifest`, which `from_dict` builds by
    selecting the keys it recognises. Anything else -- a credential, an absolute
    machine path -- is dropped during construction, so by the time the model is
    validated the offending field no longer exists to be rejected, and it never
    entered `compute_checksum` either, so the integrity block still matches. The
    document on disk is the artifact that has to be trustworthy, so it is the
    artifact that has to be checked.

    The document is closed at every level: an unrecognised key is refused rather
    than ignored. Ignoring it is what let a manifest carry fields no reader
    validates, and "the reader did not understand it" is not a reason to believe
    it is harmless.
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ValidationResult(valid=False, errors=["manifest must be a JSON object"])

    _scan_unknown_keys(data, _MANIFEST_KEYS, errors)
    for name, permitted in _NESTED_KEYS.items():
        value = data.get(name)
        if isinstance(value, dict):
            _scan_unknown_keys(value, permitted, errors, prefix=name)

    projections = data.get("projections")
    if isinstance(projections, list):
        for index, projection in enumerate(projections):
            if isinstance(projection, dict):
                _scan_unknown_keys(
                    projection, _PROJECTION_KEYS, errors, prefix=f"projections[{index}]"
                )

    for required in ("manifest_version", "workspace_id", "created_at", "compatibility"):
        if required not in data:
            errors.append(f"{required} is required")

    # The same prohibitions the model is held to, applied where they can still see
    # the offending field.
    scan_sensitive_fields(data, errors)
    _scan_non_portable(data, errors)

    return ValidationResult(valid=not errors, errors=errors)


def _scan_unknown_keys(
    data: dict[str, Any], permitted: frozenset[str], errors: list[str], prefix: str = ""
) -> None:
    for key in sorted(data):
        if key not in permitted:
            path = f"{prefix}.{key}" if prefix else key
            errors.append(f"{path} is not a recognised manifest field")


def validate_manifest(manifest: WorkspaceManifest) -> ValidationResult:
    """Validate a manifest as a portable, secret-free document.

    Checks identity and required values, that timestamps parse, that the integrity
    checksum matches when present, and that no sensitive or machine-local field
    appears anywhere in the serialised document.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if manifest.manifest_version != MANIFEST_VERSION:
        errors.append(
            f"manifest_version must be {MANIFEST_VERSION!r}, got "
            f"{manifest.manifest_version!r}"
        )
    if not manifest.workspace_id:
        errors.append("workspace_id is required")
    validate_iso_timestamp("created_at", manifest.created_at, errors)

    compatibility = manifest.compatibility
    if not compatibility.workspace_format_version:
        errors.append("compatibility.workspace_format_version is required")
    if not compatibility.min_core_version:
        errors.append("compatibility.min_core_version is required")

    seen: set[str] = set()
    for projection in manifest.projections:
        if not projection.projection_id:
            errors.append("projections[].projection_id is required")
            continue
        if projection.projection_id in seen:
            errors.append(
                f"projections declares {projection.projection_id!r} more than once"
            )
        seen.add(projection.projection_id)

    if manifest.migration is not None:
        validate_optional_iso_timestamp(
            "migration.migrated_at", manifest.migration.migrated_at, errors
        )

    payload = manifest.to_dict()

    # Secret material and machine-local identity must not survive serialisation.
    scan_sensitive_fields(payload, errors)
    _scan_non_portable(payload, errors)

    if manifest.integrity is None:
        warnings.append("integrity block is absent; the manifest is unverifiable")
    else:
        if manifest.integrity.algorithm != CHECKSUM_ALGORITHM:
            errors.append(
                f"integrity.algorithm must be {CHECKSUM_ALGORITHM!r}, got "
                f"{manifest.integrity.algorithm!r}"
            )
        elif not manifest.integrity_matches():
            errors.append("integrity.canonical_checksum does not match manifest content")

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _scan_non_portable(data: Any, errors: list[str], prefix: str = "") -> None:
    """Reject machine-local field names anywhere in the document."""
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if key.lower() in NON_PORTABLE_KEYS:
                errors.append(f"{path} must not appear in a portable manifest")
            _scan_non_portable(value, errors, path)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            _scan_non_portable(value, errors, f"{prefix}[{index}]")


__all__ = [
    "CHECKSUM_ALGORITHM",
    "MANIFEST_VERSION",
    "NON_PORTABLE_KEYS",
    "CoreCompatibility",
    "EncryptionMetadata",
    "IntegrityMetadata",
    "MigrationSummary",
    "ProjectionDeclaration",
    "WorkspaceManifest",
    "canonical_json",
    "checksum_of",
    "validate_manifest",
    "validate_raw_manifest",
]
