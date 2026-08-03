"""Publication semantics for the Host Contract v1.

Release Package identity, promotion and rollback; environment binding; and the
development/conformance production-exclusion boundary. Four governed rules:

- **promotion copies** -- promotion moves the same verified immutable Release
  Package between registries. Rebuilding per environment was rejected because
  promotion could then no longer prove that the reviewed bytes are the deployed
  bytes;
- **rollback selects** -- rollback restores a Release that was already
  verified. It mutates neither the failed Release nor the restored one;
- **bindings reference** -- an ``EnvironmentBinding`` holds logical references
  only, never a raw credential, token, private key or provider secret, and it
  stays inside the Workspace it was issued for. A reference is a pointer into
  the platform's approved set, so it is checked by *resolving* it against a
  trusted :class:`ReferenceRegistry`, not by inspecting the shape of the
  string: a bearer token is identifier-shaped, and shape alone would admit it;
- **development is excluded** -- development profiles, source mounts, fixture
  injection and test-driver entry points are compiled out of production
  customer builds. Proving that needs *both* source-graph exclusion and
  packaged-byte absence; a runtime flag proves neither.

This module represents those development and conformance controls as Core
contract decision functions. It deliberately contains no production runtime
that mounts sources, injects fixtures or drives a shell, and it does not prove
that Platform production packaging invokes the scanner. That invocation remains
the downstream G1 integration condition.

Diagnostics here never reproduce a rejected value. The values these functions
refuse are, by construction, the ones that must not appear in a log line.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import io
import re
import stat
import tarfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

from omnivia_core.host_contract.v1.generated import (
    IDENTIFIER_PATTERN,
    EnvironmentBinding,
    HostContractDecodeError,
    PromotionRecord,
    ReleasePackageIdentity,
    RollbackRecord,
)

__all__ = [
    "BINDING_REFERENCE_ERROR_CODES",
    "BINDING_REFERENCE_FIELDS",
    "BINDING_REFERENCE_KINDS",
    "DEVELOPMENT_CONTROL_MARKERS",
    "DEVELOPMENT_ONLY_RECORDS",
    "MAX_ARCHIVE_DEPTH",
    "MAX_SCANNED_MEMBERS",
    "MAX_SCANNED_MEMBER_BYTES",
    "MAX_SCANNED_TOTAL_BYTES",
    "PACKAGE_SCAN_CODES",
    "REFERENCE_KINDS",
    "SECRET_DETECTORS",
    "ApprovedReference",
    "BindingReferenceError",
    "PackageFinding",
    "PackageInventory",
    "PackageMember",
    "ReferenceRegistry",
    "ReleaseIntegrityError",
    "release_archive_findings",
    "scan_release_package",
    "validate_binding_revision",
    "validate_environment_binding",
    "validate_promotion",
    "validate_rollback",
    "validate_same_package",
]

#: What each ``EnvironmentBinding`` reference field must resolve to. The field
#: does not merely constrain the *shape* of what it holds -- it names the kind
#: of thing the reference has to be, so a storage reference in a credential
#: field is refused however well formed it is.
BINDING_REFERENCE_KINDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "credentialRefs": "credential",
        "integrationRefs": "integration",
        "storageRefs": "storage",
        "domainRefs": "domain",
        "dataResidencyPolicyRef": "data_residency_policy",
        "runtimeLimitsRef": "runtime_limits",
        "executionPlacementRef": "execution_placement",
    }
)

#: Every ``EnvironmentBinding`` field that must hold logical references only.
BINDING_REFERENCE_FIELDS: Final[tuple[str, ...]] = tuple(BINDING_REFERENCE_KINDS)

#: The kinds a trusted resolver may classify an approved reference as.
REFERENCE_KINDS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(BINDING_REFERENCE_KINDS.values())
)

#: Every reason a binding reference is refused. The set is fixed and the codes
#: are the whole diagnostic: a refusal reports a code, a field name and an
#: index, because anything more would risk reproducing the value it refused.
BINDING_REFERENCE_ERROR_CODES: Final[tuple[str, ...]] = (
    "malformed_reference_registry",
    "unresolved_reference",
    "wrong_reference_kind",
    "cross_workspace_reference",
    "unverified_reference",
    "revoked_reference",
    "duplicate_reference",
)

#: The records that exist so development and conformance can be *described*.
#: A production customer build contains none of them, in either its source
#: graph or its packaged bytes.
DEVELOPMENT_ONLY_RECORDS: Final[tuple[str, ...]] = (
    "DevelopmentProfile",
    "ShellScenario",
    "TestDriverRequest",
    "TestDriverResult",
)


class ReleaseIntegrityError(Exception):
    """Raised when a publication step would break a governed Release invariant."""


class BindingReferenceError(ReleaseIntegrityError):
    """Raised when a binding reference is not an approved, in-scope reference.

    The refusal is deliberately incurious about *what* was rejected. A value in
    a reference field may be the credential the binding is forbidden to carry,
    so this reports a fixed ``code`` plus the ``field`` it was in and the
    ``index`` within that field -- never the value, never a prefix of it, and
    never a length or character class that would narrow it.
    """

    def __init__(self, code: str, field: str = "", index: int = -1) -> None:
        self.code = code
        self.field = field
        self.index = index
        location = f"{field}[{index}]: " if field else ""
        super().__init__(f"{location}{code}")


# --------------------------------------------------------------------------
# Promotion and rollback
# --------------------------------------------------------------------------


def _require_publication_record(value: object, expected: type[Any]) -> Any:
    """Return one exact, recursively valid generated record or refuse opaquely."""
    invalid = type(value) is not expected
    if not invalid:
        try:
            cast(Any, value).validate()
        except HostContractDecodeError:
            invalid = True
    if invalid:
        raise ReleaseIntegrityError("invalid publication record")
    return value


def validate_promotion(
    record: PromotionRecord, verified: ReleasePackageIdentity
) -> None:
    """Raise unless a promotion is bound to one exact, valid verified Release."""
    record = _require_publication_record(record, PromotionRecord)
    verified = _require_publication_record(verified, ReleasePackageIdentity)
    if record.release_id != verified.release_id:
        raise ReleaseIntegrityError("promotion does not name the verified Release")
    if record.verified_manifest_sha256 != verified.manifest_sha256:
        raise ReleaseIntegrityError(
            "promotion manifest digest does not match the verified Release"
        )


def validate_same_package(
    source: ReleasePackageIdentity, target: ReleasePackageIdentity
) -> None:
    """Raise unless two registries hold the identical valid Release Package."""
    source = _require_publication_record(source, ReleasePackageIdentity)
    target = _require_publication_record(target, ReleasePackageIdentity)
    source_wire = source.to_wire()
    target_wire = target.to_wire()
    differing = sorted(
        name
        for name in ReleasePackageIdentity.WIRE_FIELDS
        if source_wire.get(name) != target_wire.get(name)
    )
    if differing:
        raise ReleaseIntegrityError(
            f"promotion would not move the same package: {differing} differ between registries"
        )


def validate_rollback(
    record: RollbackRecord,
    verified_release_ids: Iterable[str],
    failed_promotion: PromotionRecord | None = None,
) -> None:
    """Raise unless rollback selects a valid, previously verified Release."""
    record = _require_publication_record(record, RollbackRecord)
    valid_verified_ids = type(verified_release_ids) is frozenset and all(
        type(value) is str and re.fullmatch(IDENTIFIER_PATTERN, value) is not None
        for value in verified_release_ids
    )
    if not valid_verified_ids:
        raise ReleaseIntegrityError("invalid verified Release inventory")
    verified = verified_release_ids
    if record.restored_release_id not in verified:
        raise ReleaseIntegrityError(
            "rollback does not restore a previously verified Release"
        )
    if failed_promotion is None:
        return
    failed_promotion = cast(
        PromotionRecord, _require_publication_record(failed_promotion, PromotionRecord)
    )
    if failed_promotion.promotion_id != record.failed_promotion_id:
        raise ReleaseIntegrityError(
            "rollback does not name the supplied failed promotion"
        )
    if failed_promotion.release_id == record.restored_release_id:
        raise ReleaseIntegrityError(
            "rollback would restore the Release whose promotion failed"
        )


# --------------------------------------------------------------------------
# Environment bindings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovedReference:
    """One entry a trusted resolver holds: what a reference actually points at.

    This is a local trust input, not a wire record. It says what the platform
    established for itself -- that this reference exists, what kind of thing it
    is, which Workspace owns it, whether it was verified and whether it has
    since been revoked -- so a binding can be checked against facts rather than
    against the shape of the string it happens to contain.
    """

    reference_id: str
    kind: str
    workspace_id: str
    verified: bool = True
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceRegistry:
    """The trusted resolver an ``EnvironmentBinding`` is validated against.

    A binding that names a reference this resolver does not hold is refused.
    That is the whole point: a reference is a pointer into the platform's own
    approved set, and a string that resolves to nothing is not a reference,
    whatever it looks like.
    """

    references: tuple[ApprovedReference, ...] = ()


def _resolver_index(registry: ReferenceRegistry) -> dict[str, ApprovedReference]:
    """Return the resolver's entries by ID, refusing a resolver that is not usable.

    A resolver that repeats an ID, classifies an entry as something the
    contract does not know, or is not a resolver at all cannot be the
    authority for anything, so validation stops rather than resolving against
    part of it.
    """
    if (
        type(registry) is not ReferenceRegistry
        or type(registry.references) is not tuple
    ):
        raise BindingReferenceError("malformed_reference_registry")
    index: dict[str, ApprovedReference] = {}
    for entry in registry.references:
        if type(entry) is not ApprovedReference:
            raise BindingReferenceError("malformed_reference_registry")
        if (
            type(entry.reference_id) is not str
            or not entry.reference_id
            or type(entry.kind) is not str
            or type(entry.workspace_id) is not str
            or not entry.workspace_id
            or type(entry.verified) is not bool
            or type(entry.revoked) is not bool
            or entry.kind not in REFERENCE_KINDS
        ):
            raise BindingReferenceError("malformed_reference_registry")
        if entry.reference_id in index:
            raise BindingReferenceError("malformed_reference_registry")
        index[entry.reference_id] = entry
    return index


def validate_environment_binding(
    binding: EnvironmentBinding, registry: ReferenceRegistry
) -> None:
    """Raise unless every reference resolves to an approved, in-scope reference.

    Three layers have to hold, and the middle one is the one lexical checking
    cannot supply:

    1. the record itself is valid -- the closed schema has nowhere to put an
       inline secret, and the identifier pattern refuses a PEM block or an
       oversized blob outright;
    2. every reference *resolves*, in ``registry``, to an approved entry of the
       kind its field requires, owned by this binding's Workspace, verified and
       not revoked. A token is identifier-shaped, so shape alone would admit
       precisely the value this rule exists to exclude;
    3. no reference repeats within a field.

    Failures raise :class:`BindingReferenceError` carrying a fixed code, the
    field and the index. The rejected value is never named, because a rejected
    value in a reference field is exactly the material that must not be logged.
    Layer 1 failures raise ``HostContractDecodeError`` instead, and are equally
    value-free.
    """
    if type(binding) is not EnvironmentBinding:
        raise HostContractDecodeError("EnvironmentBinding: invalid record")
    binding.validate()
    index = _resolver_index(registry)
    for field_name in BINDING_REFERENCE_FIELDS:
        _require_no_repeat(_references(binding, field_name), field_name)
    for field_name, required_kind in BINDING_REFERENCE_KINDS.items():
        for position, reference in enumerate(_references(binding, field_name)):
            entry = index.get(reference) if type(reference) is str else None
            if entry is None:
                raise BindingReferenceError(
                    "unresolved_reference", field_name, position
                )
            if entry.kind != required_kind:
                raise BindingReferenceError(
                    "wrong_reference_kind", field_name, position
                )
            if entry.workspace_id != binding.workspace_id:
                raise BindingReferenceError(
                    "cross_workspace_reference", field_name, position
                )
            if not entry.verified:
                raise BindingReferenceError(
                    "unverified_reference", field_name, position
                )
            if entry.revoked:
                raise BindingReferenceError("revoked_reference", field_name, position)


def _references(binding: EnvironmentBinding, field_name: str) -> tuple[Any, ...]:
    value = getattr(binding, _BINDING_ATTRIBUTES[field_name], None)
    if field_name in _OPTIONAL_BINDING_REFERENCE_FIELDS:
        return () if value is None else (value,)
    return value if type(value) is tuple else ()


def _require_no_repeat(references: tuple[Any, ...], field_name: str) -> None:
    """Raise on a repeated reference, naming the index and never the value."""
    seen: set[Any] = set()
    for position, reference in enumerate(references):
        if type(reference) is not str:
            continue
        if reference in seen:
            raise BindingReferenceError("duplicate_reference", field_name, position)
        seen.add(reference)


_BINDING_ATTRIBUTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "credentialRefs": "credential_refs",
        "integrationRefs": "integration_refs",
        "storageRefs": "storage_refs",
        "domainRefs": "domain_refs",
        "dataResidencyPolicyRef": "data_residency_policy_ref",
        "runtimeLimitsRef": "runtime_limits_ref",
        "executionPlacementRef": "execution_placement_ref",
    }
)

_OPTIONAL_BINDING_REFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"dataResidencyPolicyRef", "runtimeLimitsRef", "executionPlacementRef"}
)


def validate_binding_revision(old: EnvironmentBinding, new: EnvironmentBinding) -> None:
    """Raise unless ``new`` is a lawful successor to ``old``.

    A binding migration creates a new immutable version; it never modifies a
    previously issued record. Two records sharing a ``bindingId`` are therefore
    two versions of one binding, and the later one must advance
    ``bindingVersion`` and stay in the same Workspace. Different ``bindingId``
    values are unrelated records, so there is nothing to compare.
    """
    old = _require_publication_record(old, EnvironmentBinding)
    new = _require_publication_record(new, EnvironmentBinding)
    if old.binding_id != new.binding_id:
        return
    if new.workspace_id != old.workspace_id:
        raise ReleaseIntegrityError("binding revision cannot move between Workspaces")
    if new.binding_version <= old.binding_version:
        raise ReleaseIntegrityError("binding revision must advance bindingVersion")


# --------------------------------------------------------------------------
# Release archive checks
# --------------------------------------------------------------------------


def release_archive_findings(members: Mapping[str, Any]) -> tuple[str, ...]:
    """Return one finding per archive member that breaks the environment-neutrality rule.

    ``members`` maps an archive member path to its parsed JSON document. Two
    governed absences are checked, in order of severity per member:

    - an ``EnvironmentBinding`` document -- a portable Release Package binds to
      no environment, so carrying one makes the archive unpromotable;
    - an environment-scoped authority spelling anywhere in the document -- a
      Release must not ship pre-baked principal, Workspace, permission,
      entitlement or environment authority.

    This is the environment-binding-absence half of the governed archive check
    and works over parsed documents. The raw-byte half -- development controls
    and likely secrets inside arbitrary packaged bytes -- is
    :func:`scan_release_package`.

    ``members`` is itself untrusted: it is assembled from an archive this gate
    has not yet cleared. Anything that is not a mapping of member path to parsed
    document is therefore answered with a finding, like every other refusal here,
    rather than with an exception a caller could mistake for a clean archive.
    An empty tuple means *scanned and clean*, so it is never what a malformed
    inventory returns.
    """
    from omnivia_core.host_contract.v1.codec import PROHIBITED_PAYLOAD_AUTHORITY_FIELDS

    if type(members) not in (dict, MappingProxyType) or any(
        type(name) is not str for name in members
    ):
        return ("inventory: Release archive members must be a mapping of member path to document",)

    findings: list[str] = []
    for index, name in enumerate(sorted(members)):
        location = f"member[{index}]"
        document = members[name]
        try:
            EnvironmentBinding.from_wire(document)
        except HostContractDecodeError:
            pass
        else:
            findings.append(
                f"{location}: Release archives must not contain an environment binding"
            )
            continue
        smuggled = sorted(
            _authority_spellings(document, PROHIBITED_PAYLOAD_AUTHORITY_FIELDS)
        )
        if smuggled:
            findings.append(
                f"{location}: Release archives must not carry environment authority {smuggled}"
            )
    return tuple(findings)


def _authority_spellings(document: object, prohibited: frozenset[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(document, Mapping):
        for key, value in document.items():
            if isinstance(key, str) and key in prohibited:
                found.add(key)
            found |= _authority_spellings(value, prohibited)
    elif isinstance(document, Sequence) and not isinstance(document, (str, bytes)):
        for item in document:
            found |= _authority_spellings(item, prohibited)
    return found


# --------------------------------------------------------------------------
# Production exclusion: the packaged bytes themselves
# --------------------------------------------------------------------------

#: The largest member this scanner will read, and the largest package. A member
#: or package past the bound is reported as an incomplete inventory rather than
#: partly scanned, because a partial scan of a production package is a pass it
#: has not earned.
MAX_SCANNED_MEMBER_BYTES: Final[int] = 64 * 1024 * 1024
MAX_SCANNED_TOTAL_BYTES: Final[int] = 512 * 1024 * 1024
MAX_SCANNED_MEMBERS: Final[int] = 100_000

#: How deep the scanner will follow an archive inside an archive.
MAX_ARCHIVE_DEPTH: Final[int] = 3

#: Every reason a Release Package is not shippable to production customers.
PACKAGE_SCAN_CODES: Final[tuple[str, ...]] = (
    "incomplete_inventory",
    "malformed_member",
    "unreadable_member",
    "symlinked_member",
    "duplicate_member",
    "escaping_member",
    "development_control_in_source_graph",
    "development_control_in_packaged_bytes",
    "likely_secret_in_packaged_bytes",
)


def _slug(record: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", record).lower()


def _marker_spellings(record: str) -> tuple[bytes, ...]:
    """Return the lowercase byte spellings one development record survives as.

    Minification renames locals; it does not rewrite string literals, JSON
    keys, source-map ``sources`` entries or exported symbol names. Matching the
    concatenated, hyphenated and underscored spellings against lowercased bytes
    therefore catches ``TestDriverRequest``, ``testDriverRequest``,
    ``TEST_DRIVER_REQUEST`` and ``test-driver-request`` alike.
    """
    slug = _slug(record)
    return tuple(
        spelling.encode("ascii")
        for spelling in dict.fromkeys((record.lower(), slug, slug.replace("-", "_")))
    )


#: Every byte spelling that means a development or conformance control shipped.
DEVELOPMENT_CONTROL_MARKERS: Final[Mapping[str, tuple[bytes, ...]]] = MappingProxyType(
    {record: _marker_spellings(record) for record in DEVELOPMENT_ONLY_RECORDS}
)

#: The conservative raw-byte likely-secret detectors this gate requires. Each
#: needs structure a benign lookalike does not have: a key block's own header, a
#: vendor prefix with its full body length, or a credential-shaped name assigned
#: an opaque literal long enough to be a real one.
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[bytes]], ...]] = (
    ("private_key_block", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("pgp_private_key_block", re.compile(rb"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    ("aws_access_key_id", re.compile(rb"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(rb"\bxox[abposr]-[A-Za-z0-9]{8,}-[A-Za-z0-9-]{8,}\b")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_secret_key", re.compile(rb"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    (
        "json_web_token",
        re.compile(
            rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
        ),
    ),
    (
        "credential_assignment",
        re.compile(
            rb"(?i)\b(?:api[_\-]?key|secret|passwd|password|token|client[_\-]?secret"
            rb"|private[_\-]?key|access[_\-]?key|auth[_\-]?token)\b"
            rb"[\"']?\s*[:=]\s*[\"'][A-Za-z0-9+/_\-.=]{16,}[\"']"
        ),
    ),
    (
        "basic_auth_url",
        re.compile(rb"\b[a-z][a-z0-9+.\-]*://[^\s/:@\"']{1,64}:[^\s/@\"']{1,128}@"),
    ),
)

#: The detector names this gate is pinned to. Adding or removing one is a
#: change to the governed packaging control, not an implementation detail.
SECRET_DETECTORS: Final[tuple[str, ...]] = tuple(name for name, _ in _SECRET_PATTERNS)

@dataclass(frozen=True, slots=True)
class PackageMember:
    """One file or directory candidate from a Release Package inventory."""

    path: str
    data: bytes
    is_symlink: bool = False
    is_regular: bool = True
    is_directory: bool = False
    is_encrypted: bool = False


@dataclass(frozen=True, slots=True)
class PackageInventory:
    """A complete package inventory and exact production source-symbol graph."""

    members: tuple[PackageMember, ...] = ()
    source_symbols: tuple[str, ...] | None = None
    source_graph_exhaustive: bool = False
    exhaustive: bool = False
    development_flag_enabled: bool = False


@dataclass(frozen=True, slots=True)
class PackageFinding:
    """One sanitized reason a Release Package is not shippable."""

    code: str
    member: str = ""
    detail: str = ""
    detector: str = ""
    offset: int = -1


@dataclass(slots=True)
class _ScanState:
    members: int = 0
    total_bytes: int = 0


def scan_release_package(inventory: PackageInventory) -> tuple[PackageFinding, ...]:
    """Recursively inventory and byte-scan a production Release Package.

    Every location is an opaque stable index. Archive member names are untrusted
    inputs and therefore never cross the diagnostic boundary.
    """
    if type(inventory) is not PackageInventory:
        return (_finding("incomplete_inventory", detail="trusted inventory required"),)

    findings: list[PackageFinding] = []
    exact_flags = (
        type(inventory.exhaustive) is bool
        and inventory.exhaustive is True
        and type(inventory.development_flag_enabled) is bool
    )
    if not exact_flags or type(inventory.members) is not tuple:
        findings.append(
            _finding(
                "incomplete_inventory", detail="inventory is not exact and exhaustive"
            )
        )
    findings.extend(
        _source_graph_findings(
            inventory.source_symbols,
            inventory.source_graph_exhaustive,
        )
    )
    if type(inventory.members) is not tuple:
        return tuple(findings)

    state = _ScanState()
    findings.extend(_scan_members(inventory.members, state, depth=0, parent=""))
    return tuple(findings)


def _finding(
    code: str,
    member: str = "",
    detail: str = "",
    detector: str = "",
    offset: int = -1,
) -> PackageFinding:
    return PackageFinding(
        code=code, member=member, detail=detail, detector=detector, offset=offset
    )


def _source_graph_findings(
    source_symbols: object, exhaustive: object
) -> list[PackageFinding]:
    if type(exhaustive) is not bool or exhaustive is not True:
        return [
            _finding(
                "incomplete_inventory",
                detail="source graph evidence is incomplete",
            )
        ]
    if type(source_symbols) is not tuple or any(
        type(symbol) is not str for symbol in source_symbols
    ):
        return [
            _finding(
                "incomplete_inventory",
                detail="source graph must be an exact tuple of governed symbol strings",
            )
        ]
    symbols = set(source_symbols)
    return [
        _finding(
            "development_control_in_source_graph",
            detail=(
                f"{record}: development and conformance records must be absent from the "
                "production source graph"
            ),
        )
        for record in DEVELOPMENT_ONLY_RECORDS
        if record in symbols
    ]


def _escapes(path: str) -> bool:
    """Return whether a member path is anything but a canonical relative path."""
    if path.startswith("/") or "\\" in path or re.match(r"^[A-Za-z]:", path):
        return True
    segments = path.split("/")
    return any(segment in ("", ".", "..") for segment in segments)


def _location(parent: str, index: int) -> str:
    current = f"member[{index}]"
    return f"{parent}!{current}" if parent else current


def _reserve_member(state: _ScanState, location: str) -> PackageFinding | None:
    state.members += 1
    if state.members > MAX_SCANNED_MEMBERS:
        return _finding("incomplete_inventory", location, "member scan bound exceeded")
    return None


def _reserve_bytes(
    state: _ScanState, size: int, location: str
) -> PackageFinding | None:
    if (
        size > MAX_SCANNED_MEMBER_BYTES
        or state.total_bytes + size > MAX_SCANNED_TOTAL_BYTES
    ):
        return _finding(
            "incomplete_inventory", location, "member or aggregate byte bound exceeded"
        )
    state.total_bytes += size
    return None


def _scan_members(
    entries: tuple[PackageMember, ...],
    state: _ScanState,
    depth: int,
    parent: str,
) -> list[PackageFinding]:
    findings: list[PackageFinding] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        location = _location(parent, index)
        bounded_member = _reserve_member(state, location)
        if bounded_member is not None:
            findings.append(bounded_member)
            break
        if type(entry) is not PackageMember:
            findings.append(
                _finding("malformed_member", location, "invalid member record")
            )
            continue
        if (
            type(entry.path) is not str
            or not entry.path
            or "\x00" in entry.path
            or type(entry.data) is not bytes
            or type(entry.is_symlink) is not bool
            or type(entry.is_regular) is not bool
            or type(entry.is_directory) is not bool
            or type(entry.is_encrypted) is not bool
        ):
            findings.append(
                _finding("malformed_member", location, "invalid member record")
            )
            continue
        if _escapes(entry.path):
            findings.append(
                _finding("escaping_member", location, "non-canonical member path")
            )
            continue
        if entry.path in seen:
            findings.append(
                _finding("duplicate_member", location, "duplicate member path")
            )
            continue
        seen.add(entry.path)
        if entry.is_encrypted:
            findings.append(
                _finding(
                    "unreadable_member",
                    location,
                    "encrypted member is unreadable",
                )
            )
            continue
        if entry.is_symlink:
            findings.append(
                _finding("symlinked_member", location, "links are not regular files")
            )
            continue
        if not entry.is_regular:
            findings.append(_finding("malformed_member", location, "special files are not allowed"))
            continue
        if entry.is_directory:
            if entry.data:
                findings.append(
                    _finding(
                        "malformed_member",
                        location,
                        "directory entries must be empty",
                    )
                )
            continue
        bounded = _reserve_bytes(state, len(entry.data), location)
        if bounded is not None:
            findings.append(bounded)
            break
        findings.extend(
            _member_findings(location, entry.path, entry.data, depth=depth, state=state)
        )
    return findings


def _archive_kind(path: str, data: bytes) -> str | None:
    lowered = path.lower()
    if data.startswith(
        (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    ) or lowered.endswith(".zip"):
        return "zip"
    if (
        data.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00"))
        or data[257:262] == b"ustar"
        or lowered.endswith(
            (
                ".tar",
                ".tar.gz",
                ".tgz",
                ".tar.bz2",
                ".tbz",
                ".tbz2",
                ".tar.xz",
                ".txz",
            )
        )
    ):
        return "tar"
    return None


def _member_findings(
    location: str,
    path: str,
    data: bytes,
    depth: int,
    state: _ScanState,
) -> list[PackageFinding]:
    archive_kind = _archive_kind(path, data)
    if archive_kind is not None:
        if depth >= MAX_ARCHIVE_DEPTH:
            return [
                _finding(
                    "incomplete_inventory",
                    location,
                    "nested archive depth bound exceeded",
                )
            ]
        if archive_kind == "zip":
            return _nested_zip_findings(location, data, depth + 1, state)
        return _nested_tar_findings(location, data, depth + 1, state)

    findings: list[PackageFinding] = []
    haystack = data.lower()
    for record, spellings in DEVELOPMENT_CONTROL_MARKERS.items():
        offset = min(
            (
                found
                for found in (haystack.find(spelling) for spelling in spellings)
                if found >= 0
            ),
            default=-1,
        )
        if offset >= 0:
            findings.append(
                _finding(
                    "development_control_in_packaged_bytes",
                    location,
                    (
                        f"{record}: development and conformance controls must be absent from "
                        "production packaged bytes"
                    ),
                    record,
                    offset,
                )
            )
    for detector, pattern in _SECRET_PATTERNS:
        match = pattern.search(data)
        if match is not None:
            findings.append(
                _finding(
                    "likely_secret_in_packaged_bytes",
                    location,
                    "credential material is forbidden in production packaged bytes",
                    detector,
                    match.start(),
                )
            )
    return findings


def _zip_file_kind(info: zipfile.ZipInfo) -> tuple[bool, bool, bool]:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    directory_shape = info.is_dir()
    return (
        file_type == stat.S_IFLNK,
        not directory_shape and file_type in (0, stat.S_IFREG),
        directory_shape and file_type in (0, stat.S_IFDIR),
    )


def _nested_zip_findings(
    parent: str, data: bytes, depth: int, state: _ScanState
) -> list[PackageFinding]:
    findings: list[PackageFinding] = []
    unreadable = False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            seen: set[str] = set()
            for index, info in enumerate(archive.infolist()):
                location = _location(parent, index)
                bounded_member = _reserve_member(state, location)
                if bounded_member is not None:
                    findings.append(bounded_member)
                    break
                name = info.filename
                canonical = name[:-1] if info.is_dir() and name.endswith("/") else name
                if not canonical or _escapes(canonical):
                    findings.append(
                        _finding(
                            "escaping_member", location, "non-canonical member path"
                        )
                    )
                    continue
                if canonical in seen:
                    findings.append(
                        _finding("duplicate_member", location, "duplicate member path")
                    )
                    continue
                seen.add(canonical)
                is_symlink, is_regular, is_ordinary_directory = _zip_file_kind(info)
                if is_symlink:
                    findings.append(
                        _finding(
                            "symlinked_member", location, "links are not regular files"
                        )
                    )
                    continue
                if info.is_dir():
                    if not is_ordinary_directory:
                        findings.append(
                            _finding(
                                "malformed_member",
                                location,
                                "special files are not allowed",
                            )
                        )
                    elif info.flag_bits & 0x1:
                        findings.append(
                            _finding(
                                "unreadable_member",
                                location,
                                "encrypted member is unreadable",
                            )
                        )
                    elif info.file_size != 0 or info.compress_size != 0:
                        findings.append(
                            _finding(
                                "malformed_member",
                                location,
                                "directory entries must be empty",
                            )
                        )
                    continue
                if not is_regular:
                    findings.append(
                        _finding(
                            "malformed_member",
                            location,
                            "special files are not allowed",
                        )
                    )
                    continue
                if info.flag_bits & 0x1:
                    findings.append(
                        _finding(
                            "unreadable_member",
                            location,
                            "encrypted member is unreadable",
                        )
                    )
                    continue
                bounded = _reserve_bytes(state, info.file_size, location)
                if bounded is not None:
                    findings.append(bounded)
                    break
                try:
                    nested = archive.read(info)
                except (
                    zipfile.BadZipFile,
                    OSError,
                    RuntimeError,
                    ValueError,
                    EOFError,
                ):
                    findings.append(
                        _finding(
                            "unreadable_member",
                            location,
                            "archive member could not be read",
                        )
                    )
                    continue
                if len(nested) != info.file_size:
                    findings.append(
                        _finding(
                            "unreadable_member",
                            location,
                            "archive member was not exhaustive",
                        )
                    )
                    continue
                findings.extend(
                    _member_findings(location, name, nested, depth=depth, state=state)
                )
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, EOFError):
        unreadable = True
    if unreadable:
        return [
            _finding("unreadable_member", parent, "nested ZIP archive is unreadable")
        ]
    return findings


def _tar_directory_has_alias(
    archive: tarfile.TarFile, info: tarfile.TarInfo
) -> bool:
    """Inspect the raw tar header before ``tarfile`` normalizes directory names."""
    stream = archive.fileobj
    if stream is None:
        raise OSError("tar stream is unavailable")
    position = stream.tell()
    try:
        stream.seek(info.offset)
        block = stream.read(tarfile.BLOCKSIZE)
    finally:
        stream.seek(position)
    if len(block) != tarfile.BLOCKSIZE:
        raise OSError("tar header is incomplete")
    raw_name = block[:100].split(b"\x00", 1)[0]
    raw_prefix = block[345:500].split(b"\x00", 1)[0]
    if raw_prefix:
        raw_name = raw_prefix + b"/" + raw_name
    pax_path = info.pax_headers.get("path")
    return raw_name.endswith(b"//") or (
        type(pax_path) is str and pax_path.endswith("//")
    )


def _nested_tar_findings(
    parent: str, data: bytes, depth: int, state: _ScanState
) -> list[PackageFinding]:
    findings: list[PackageFinding] = []
    unreadable = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            seen: set[str] = set()
            for index, info in enumerate(archive):
                location = _location(parent, index)
                bounded_member = _reserve_member(state, location)
                if bounded_member is not None:
                    findings.append(bounded_member)
                    break
                name = info.name
                if info.isdir() and _tar_directory_has_alias(archive, info):
                    findings.append(
                        _finding(
                            "escaping_member", location, "non-canonical member path"
                        )
                    )
                    continue
                if not name or _escapes(name):
                    findings.append(
                        _finding(
                            "escaping_member", location, "non-canonical member path"
                        )
                    )
                    continue
                if name in seen:
                    findings.append(
                        _finding("duplicate_member", location, "duplicate member path")
                    )
                    continue
                seen.add(name)
                if info.isdir():
                    if info.size != 0:
                        findings.append(
                            _finding(
                                "malformed_member",
                                location,
                                "directory entries must be empty",
                            )
                        )
                    continue
                if info.issym() or info.islnk():
                    findings.append(
                        _finding(
                            "symlinked_member", location, "links are not regular files"
                        )
                    )
                    continue
                if not info.isfile():
                    findings.append(
                        _finding(
                            "malformed_member",
                            location,
                            "special files are not allowed",
                        )
                    )
                    continue
                bounded = _reserve_bytes(state, info.size, location)
                if bounded is not None:
                    findings.append(bounded)
                    break
                try:
                    handle = archive.extractfile(info)
                    nested = None if handle is None else handle.read()
                except (tarfile.TarError, OSError, EOFError):
                    nested = None
                if nested is None or len(nested) != info.size:
                    findings.append(
                        _finding(
                            "unreadable_member",
                            location,
                            "archive member could not be read",
                        )
                    )
                    continue
                findings.extend(
                    _member_findings(location, name, nested, depth=depth, state=state)
                )
    except (tarfile.TarError, OSError, EOFError):
        unreadable = True
    if unreadable:
        return [
            _finding("unreadable_member", parent, "nested tar archive is unreadable")
        ]
    return findings
