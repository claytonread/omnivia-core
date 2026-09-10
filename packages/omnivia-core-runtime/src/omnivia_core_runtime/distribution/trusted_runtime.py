"""Runtime payload trust: verify one installed Core payload, then name its pair.

The property this module exists for is not "find `omnivia-core-service`". It is
**proving, before the first process launch, that two executable paths came from one
approved, untampered Core release**. `shared_runtime.py` above already owns
deterministic selection and consumer bookkeeping; what it could not do is tell an
adapter that the payload it selected is the one the release actually signed.

Four things are therefore fixed here rather than left to metadata.

**Identity is computed, never accepted.** ``payload_identity`` is the SHA-256 of a
domain-separated canonical identity claim over every manifest member *except*
``payload_identity`` -- which is why the manifest can declare it without the hash
being self-referential, and why the declared value is compared rather than
believed. The detached signature covers a *second* domain-separated canonicalisation
of the manifest *including* the computed identity, so a signature cannot be lifted
onto a manifest whose identity differs. Neither digest is caller authority: a
caller-supplied digest is at most an equality guard.

**The executable pair is a layout constant.** ``bin/omnivia`` and
``bin/omnivia-core-service`` on POSIX, ``Scripts/omnivia.exe`` and
``Scripts/omnivia-core-service.exe`` on Windows. The manifest carries the two paths
so the document is self-describing, and a manifest naming anything else is refused.
There is no argv, no working directory, no environment and no command in this
contract: it is not a signed-command framework and cannot become one by adding a
field, because an unknown field is refused rather than ignored.

**Discovery is not part of trust.** Nothing here reads ``HOME``, consults ``PATH``,
runs ``which``, searches a filesystem recursively, opens a socket, or executes a
byte of the payload. The installation root is an explicit absolute argument, the
approved keys are an explicit argument, and the verification instant is an explicit
argument -- so a test and a production launch differ only in what they pass.

**A refusal carries one closed code and nothing else.** :class:`RuntimeRefusal` is
the whole of what a failed resolution says. That is a deliberate ceiling on the
exfiltration surface rather than terseness for its own sake: with no message, no
path and no offending value in the document, no file content, key material or
absolute payload path can leave through it. Human diagnostics belong on stderr.

The language-neutral form of all of this is
``contracts/runtime/v1/schemas/trusted-runtime-v1.schema.json`` with its fixtures
and vectors; Platform implements the same verifier in TypeScript against those and
must agree on every case. This module is the reference implementation, not the
contract.

**What v1 does not claim.** The Ed25519 release signature plus the full inventory is
the trust root on every supported operating system. Operating-system package or
code identity -- an Apple Team ID and designated requirement, an Authenticode
signer -- is an *additional* product policy that v1 neither verifies nor asserts.
That residual is stated in ``docs/distribution/shared-core-installation.md`` rather
than papered over with evidence this code does not gather.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from platform import machine as _reported_machine
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: Version of the payload manifest contract. Bumped when a consumer would have to
#: change to keep reading it. Unknown members are refused rather than ignored, so
#: there is no additive direction inside a major: a new member changes the signed
#: bytes and therefore the payload identity it was absent from.
PAYLOAD_MANIFEST_VERSION: Final = "1.0"

#: Version of the detached signature document and of the descriptor and refusal
#: documents a consumer reads back.
RELEASE_SIGNATURE_VERSION: Final = "1.0"
RUNTIME_DESCRIPTOR_VERSION: Final = "1.0"

#: The bootstrap seam this build implements: the `--init` and `--managed-start`
#: machine-readable contracts a verified payload is expected to serve. A consumer
#: states the version it implements and is refused outside the payload's window.
BOOTSTRAP_CONTRACT_VERSION: Final = "1.0"

#: The two metadata documents inside a candidate payload. They are deliberately
#: outside the inventory -- a manifest cannot contain its own digest -- and are
#: instead covered by the signature over the manifest itself.
RUNTIME_MANIFEST_NAME: Final = "omnivia-runtime-manifest.json"
RUNTIME_SIGNATURE_NAME: Final = "omnivia-runtime-manifest.sig.json"

#: Domain separation. Two different questions are asked of nearly identical bytes
#: -- "what is this payload's identity" and "did the release sign this manifest" --
#: so each gets its own prefix and a NUL terminator, and an answer to one can never
#: be replayed as an answer to the other.
IDENTITY_DOMAIN: Final = "omnivia.runtime-payload-identity.v1"
SIGNATURE_DOMAIN: Final = "omnivia.runtime-payload-signature.v1"

#: The only signature algorithm v1 admits.
SIGNATURE_ALGORITHM: Final = "ed25519"

#: The fixed executable layout per operating system, as `(cli, service)`. Metadata
#: cannot alter these; a manifest declaring anything else is `runtime_layout_invalid`.
EXECUTABLE_LAYOUT: Final[Mapping[str, tuple[str, str]]] = {
    "macos": ("bin/omnivia", "bin/omnivia-core-service"),
    "linux": ("bin/omnivia", "bin/omnivia-core-service"),
    "windows": ("Scripts/omnivia.exe", "Scripts/omnivia-core-service.exe"),
}

#: The operating system this process is running as, in the manifest's spelling. A
#: payload built for another operating system is refused: its fixed executable
#: layout is not the one this host can run.
#:
#: Every entry point takes it as an explicit argument defaulting to this value,
#: which is what lets one set of conformance fixtures produce the same verdict on
#: macOS, Linux and Windows. It is a *compatibility* input and never a trust input:
#: passing the wrong one cannot make an unsigned payload verify, only make a
#: correctly signed payload for another operating system pass a check it should
#: have failed -- and its executables still would not run.
HOST_OPERATING_SYSTEM: Final = (
    "windows" if os.name == "nt" else "macos" if os.uname().sysname == "Darwin" else "linux"
)

#: The two architectures v1 admits, in the manifest's spelling.
PAYLOAD_ARCHITECTURES: Final = ("arm64", "x86_64")

#: One host calls the same silicon ``aarch64`` and another ``arm64``; ``AMD64`` and
#: ``x86_64`` likewise. A reported machine outside this table passes through
#: unchanged, which cannot equal either admitted spelling and is therefore refused
#: as incompatible rather than guessed at.
_ARCHITECTURE_ALIASES: Final[Mapping[str, str]] = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "x86_64",
    "x86_64": "x86_64",
}
_MACHINE: Final = _reported_machine().lower()

#: The architecture this process is running as, in the manifest's spelling, and the
#: other half of `HOST_OPERATING_SYSTEM`. The manifest declares one; without this
#: nothing compared it, so a correctly signed payload built for the *other*
#: architecture resolved and produced a pair the host cannot execute.
#:
#: An explicit argument on every entry point, defaulting to this value, for the same
#: reason: it is what lets one corpus produce the same verdicts on arm64 and x86_64.
#: A *compatibility* input and never a trust input -- passing the wrong one cannot
#: make an unsigned payload verify, only let an authentic payload for the other
#: architecture pass a check it should have failed, and its executables still would
#: not run. That bound is worth stating because on Windows the standard library
#: derives the machine name from ``PROCESSOR_ARCHITECTURE``: a caller that needs the
#: value not to come from the environment passes ``host_architecture`` explicitly.
HOST_ARCHITECTURE: Final = _ARCHITECTURE_ALIASES.get(_MACHINE, _MACHINE)

#: Bounded reads, every one of them. An unbounded read of a file an attacker can
#: grow is a denial of service against the launcher, and a launcher that will not
#: start is as unavailable as one that starts the wrong binary.
MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_SIGNATURE_BYTES: Final = 16 * 1024
MAX_SELECTION_BYTES: Final = 64 * 1024
MAX_INVENTORY_ENTRIES: Final = 4096
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_PAYLOAD_BYTES: Final = 512 * 1024 * 1024
_READ_CHUNK: Final = 1024 * 1024

#: Windows marks symlinks, junctions and mount points with this attribute. Checked
#: explicitly rather than relying on `is_symlink()` alone, because a junction is not
#: a symlink and is just as good a way out of the candidate root.
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400

_HEX64 = tuple("0123456789abcdef")

#: The published schema's `keyId` and `relativePath` character policies, restated as
#: code. They are here because a *looser* reference verifier is the dangerous
#: direction: a document the schema rejects but this module accepts is a document
#: Platform's verifier refuses and this one runs, which is the two implementations
#: disagreeing about the same bytes -- exactly what the corpus exists to prevent.
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

#: Excluded from a payload member path, together with every character below U+0020.
#: The backslash and the colon are the ones with teeth -- ``C:`` and ``..\\`` are how a
#: relative name stops being one on Windows -- and the rest are the reserved set the
#: schema names, kept identical so neither side admits what the other refuses.
_RESERVED_IN_MEMBER: Final = '\\:*?"<>|'


class RuntimeRefusal(str, Enum):
    """The closed, payload-free vocabulary a failed resolution answers with.

    Fixed strings, one per class of remedy, because a consumer branches on them and
    a ninth name appearing unannounced is a breaking change to the contract rather
    than a detail. Every value is a literal for the same reason
    `WorkspaceInitRefusal`'s are: a code must never depend on declaration position.

    These are distinct from `WorkspaceInitRefusal` and `ManagedStartFailure` on
    purpose. Resolution answers "may this runtime be executed at all"; those two
    answer what happened once it was.
    """

    #: No active installed candidate exists. Retry only after installation or repair.
    NOT_INSTALLED = "runtime_not_installed"
    #: A selection or manifest document is malformed, oversized, or an unsupported
    #: version. Repair required.
    METADATA_INVALID = "runtime_metadata_invalid"
    #: The signature, the key, or the key's window is not approved. Never execute.
    UNTRUSTED = "runtime_untrusted"
    #: A payload identity or a file digest does not match. Never execute.
    TAMPERED = "runtime_tampered"
    #: The pair is missing, split, escaping, symlinked, or of the wrong mode or
    #: owner. Never execute.
    LAYOUT_INVALID = "runtime_layout_invalid"
    #: An authentic release that cannot serve the consumer's contract. Install a
    #: compatible Core.
    INCOMPATIBLE = "runtime_incompatible"
    #: The installation selection is being changed. Bounded retry.
    BUSY = "runtime_busy"
    #: A bounded local read failed. Bounded retry or repair.
    IO_FAILURE = "runtime_io_failure"


class RuntimeResolutionError(Exception):
    """One closed refusal, and nothing else.

    The exception carries the code and the code alone. `str()` of it is the wire
    value, so even a caller that logs the exception carelessly cannot leak a path,
    a file's contents, or key material through this type.
    """

    def __init__(self, refusal: RuntimeRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal

    def to_dict(self) -> dict[str, Any]:
        """The versioned machine-readable refusal document."""
        return {
            "runtime_descriptor_version": RUNTIME_DESCRIPTOR_VERSION,
            "refusal": self.refusal.value,
        }


def _refuse(refusal: RuntimeRefusal) -> RuntimeResolutionError:
    return RuntimeResolutionError(refusal)


# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------


class CanonicalJsonError(ValueError):
    """A value outside the canonical JSON subset this contract admits."""


def canonical_json(value: Any) -> bytes:
    """Serialise `value` in the canonical subset, as ASCII bytes.

    The subset is deliberately narrower than JSON, and each restriction removes one
    way two implementations could disagree about the same document:

    - object members are sorted by the **UTF-8 bytes** of their names, not by code
      unit, so a UTF-16 language sorts them identically;
    - no insignificant whitespace, so there is one byte string per value;
    - arrays keep their order, which the contract fixes per member rather than
      leaving to the serialiser;
    - **no floats.** ``1.0``, ``1``, ``1e0`` and ``0.1 + 0.2`` are one value to some
      languages and several to others. Integers only, and a float raises;
    - non-ASCII is escaped, so the output is ASCII whatever the transport does.

    Duplicate names cannot arise here -- a Python mapping has none -- and are
    refused on the way *in* by :func:`parse_canonical_document`.
    """
    return _canonical(value).encode("ascii")


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        # Checked after the two singletons above: `bool` is an `int` subclass, and
        # `True` serialised as `1` would make two distinct documents canonicalise
        # to the same bytes.
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, Mapping):
        members = sorted(value.items(), key=lambda item: _name(item[0]))
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=True)}:{_canonical(v)}" for k, v in members) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(member) for member in value) + "]"
    raise CanonicalJsonError(f"value of type {type(value).__name__} is outside the canonical subset")


def _name(key: Any) -> bytes:
    if not isinstance(key, str):
        raise CanonicalJsonError("object names must be strings")
    try:
        return key.encode("utf-8")
    except UnicodeEncodeError as failure:  # a lone surrogate has no UTF-8 form
        raise CanonicalJsonError("object name is not encodable as UTF-8") from failure


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise CanonicalJsonError(f"duplicate object name {key!r}")
        document[key] = value
    return document


def _reject_float(literal: str) -> Any:
    raise CanonicalJsonError(f"floating point literal {literal!r} is outside the canonical subset")


def _reject_constant(literal: str) -> Any:
    raise CanonicalJsonError(f"{literal} is not JSON")


def parse_canonical_document(raw: bytes, *, limit: int) -> dict[str, Any]:
    """Parse one bounded JSON object, failing closed on everything ambiguous.

    Size first, then strict UTF-8, then a parse that refuses a duplicate name, a
    floating-point literal and the ``NaN``/``Infinity`` extensions -- each of which
    is a place where "the same document" means different things to different
    parsers, and one of which (the duplicate name) is a place where it means
    different things to the *same* parser depending on which value it kept.
    """
    if len(raw) > limit:
        raise CanonicalJsonError("document exceeds its bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as failure:
        raise CanonicalJsonError("document is not UTF-8") from failure
    document = json.loads(
        text,
        object_pairs_hook=_reject_duplicates,
        parse_float=_reject_float,
        parse_constant=_reject_constant,
    )
    if not isinstance(document, dict):
        raise CanonicalJsonError("document root is not an object")
    return document


# --------------------------------------------------------------------------
# Trust anchors
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrustAnchor:
    """One approved release key and the window it is approved in.

    An explicit resolver input, never read from the installation being verified: a
    payload that could nominate the key that verifies it is self-certifying.

    Rotation is expressed entirely by the windows. Two anchors may both be approved
    while their windows overlap, which is what makes a transition possible without a
    flag day; outside its window a key is refused whether it is not yet valid,
    expired, or retired early, and an unknown ``key_id`` is refused with the same
    code. Every direction fails closed.
    """

    key_id: str
    public_key: bytes
    not_before: datetime
    not_after: datetime
    retired_at: datetime | None = None
    algorithm: str = SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        if self.algorithm != SIGNATURE_ALGORITHM:
            raise ValueError(f"unsupported signature algorithm {self.algorithm!r}")
        if len(self.public_key) != 32:
            raise ValueError("an Ed25519 public key is 32 bytes")
        for moment in (self.not_before, self.not_after, self.retired_at):
            if moment is not None and moment.tzinfo is None:
                raise ValueError("trust anchor instants must be timezone-aware")
        if self.not_before >= self.not_after:
            raise ValueError("trust anchor window is empty")

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> TrustAnchor:
        """Build one anchor from its published JSON shape.

        A malformed anchor raises ``ValueError`` rather than producing a refusal
        code: anchors are the *caller's* authority, not installation metadata, so a
        broken one is a defect in the calling program and must be loud.
        """
        expected = {"key_id", "algorithm", "public_key", "not_before", "not_after"}
        unknown = set(document) - (expected | {"retired_at"})
        if unknown or not expected <= set(document):
            raise ValueError(f"trust anchor document has unexpected members: {sorted(document)}")
        retired = document.get("retired_at")
        return cls(
            key_id=str(document["key_id"]),
            public_key=base64.b64decode(str(document["public_key"]), validate=True),
            not_before=_instant(str(document["not_before"])),
            not_after=_instant(str(document["not_after"])),
            retired_at=None if retired is None else _instant(str(retired)),
            algorithm=str(document["algorithm"]),
        )

    def usable_at(self, moment: datetime) -> bool:
        """Whether this key may verify a release at `moment`."""
        if not self.not_before <= moment < self.not_after:
            return False
        return self.retired_at is None or moment < self.retired_at


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} carries no timezone")
    return parsed


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One security-relevant payload file, as the signed manifest declares it."""

    path: PurePosixPath
    sha256: str
    size: int
    executable: bool


@dataclass(frozen=True, slots=True)
class VerifiedRuntime:
    """A completely verified payload and the exact pair it resolves to.

    Returned on success only. Every field is derived from the signed manifest or
    from the explicit installation root; nothing in it was taken from an untrusted
    record. It is a cache hint for a consumer and never continuing authority: the
    next launch resolves again, because a file on disk can change between two
    launches and a descriptor cannot notice.
    """

    release_version: str
    payload_identity: str
    runtime_root: Path
    cli_path: Path
    service_path: Path
    inventory: tuple[InventoryEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        """The versioned machine-readable descriptor, and the whole of it.

        The inventory is deliberately not in it. A consumer binds two paths; it has
        no use for the file list, and publishing one would put a payload's entire
        layout into whatever the consumer logs.
        """
        return {
            "runtime_descriptor_version": RUNTIME_DESCRIPTOR_VERSION,
            "release_version": self.release_version,
            "payload_identity": self.payload_identity,
            "runtime_root": str(self.runtime_root),
            "cli_path": str(self.cli_path),
            "service_path": str(self.service_path),
        }


def payload_identity(manifest: Mapping[str, Any]) -> str:
    """The identity of the payload `manifest` describes.

    ``payload_identity`` is dropped before canonicalising, which is what keeps the
    hash out of its own input. Every other member is covered, so moving one byte of
    a declared digest, a path, the release version or the compatibility window
    produces a different identity -- and therefore a different candidate directory
    name and a signature that no longer verifies.
    """
    claim = {name: value for name, value in manifest.items() if name != "payload_identity"}
    digest = hashlib.sha256(IDENTITY_DOMAIN.encode("ascii") + b"\x00" + canonical_json(claim))
    return f"sha256:{digest.hexdigest()}"


def signed_manifest_bytes(manifest: Mapping[str, Any], identity: str) -> bytes:
    """The exact bytes a release signature covers.

    The manifest *including* the computed identity, under the signature domain. A
    signature therefore attests to the identity as well as to the members it was
    derived from, so one cannot be lifted onto a manifest with a different identity.
    """
    signed = {name: value for name, value in manifest.items() if name != "payload_identity"}
    signed["payload_identity"] = identity
    return SIGNATURE_DOMAIN.encode("ascii") + b"\x00" + canonical_json(signed)


def _sha256_hex(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX64 for c in value):
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    return value


def _key_id(value: Any) -> str:
    """One signing key identifier, or a refusal.

    Bounded and charactered exactly as the schema's ``keyId``. Previously this was
    ``isinstance(value, str)`` and nothing else, so an empty, unbounded or
    whitespace-carrying identifier reached the anchor lookup here and was refused by
    the published schema -- a disagreement rather than a shared verdict.
    """
    if not isinstance(value, str) or _KEY_ID.fullmatch(value) is None:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    return value


def _semver(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    parts = value.split(".")
    if len(parts) != 3:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    for part in parts:
        if not part.isdigit() or (part != "0" and part.startswith("0")):
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
    first, second, third = parts
    return int(first), int(second), int(third)


def _contract(value: Any) -> tuple[int, int]:
    if not isinstance(value, str):
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    parts = value.split(".")
    if len(parts) != 2:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    for part in parts:
        if not part.isdigit() or (part != "0" and part.startswith("0")):
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
    first, second = parts
    return int(first), int(second)


def _member(value: Any) -> PurePosixPath:
    """One normalised relative POSIX path inside a payload, or a refusal.

    Everything that is not plainly a relative name sequence is refused rather than
    normalised into one: an absolute path, a drive letter, a backslash, a ``.`` or
    ``..`` component, an empty component from a doubled separator, a NUL. Refusing
    is what makes "every resolved path stays beneath the candidate root" a property
    of the parse rather than of a later comparison somebody could forget.
    """
    if not isinstance(value, str) or not value or len(value) > 512:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if value.startswith("/"):
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    if any(character in _RESERVED_IN_MEMBER or character < "\x20" for character in value):
        # One test rather than a list of special cases: the drive letter in ``C:\\x``,
        # the separator in ``..\\escape``, a NUL, and the rest of the reserved set the
        # schema excludes are all here, and adding one is adding a character.
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    return PurePosixPath(value)


def parse_manifest(document: Mapping[str, Any]) -> tuple[str, tuple[InventoryEntry, ...]]:
    """Validate one manifest document strictly and return its release and inventory.

    Strict in both directions: a missing member and an unknown member are equally
    refused. The second half is the one worth stating -- an unknown member is inside
    the signed bytes and inside the identity claim, so "ignore what you do not
    understand" would mean verifying a signature over a document you did not read.
    """
    required = {
        "manifest_version",
        "release_version",
        "platform",
        "executables",
        "inventory",
        "compatibility",
        "signing",
        "payload_identity",
    }
    if set(document) != required:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if document["manifest_version"] != PAYLOAD_MANIFEST_VERSION:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)

    release_version = document["release_version"]
    _semver(release_version)

    platform = document["platform"]
    if not isinstance(platform, dict) or set(platform) != {"os", "arch"}:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if platform["os"] not in EXECUTABLE_LAYOUT or platform["arch"] not in PAYLOAD_ARCHITECTURES:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)

    compatibility = document["compatibility"]
    if not isinstance(compatibility, dict) or set(compatibility) != {
        "minimum_bootstrap_contract",
        "maximum_bootstrap_contract",
    }:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if _contract(compatibility["minimum_bootstrap_contract"]) > _contract(
        compatibility["maximum_bootstrap_contract"]
    ):
        raise _refuse(RuntimeRefusal.METADATA_INVALID)

    signing = document["signing"]
    if not isinstance(signing, dict) or set(signing) != {"key_id", "algorithm"}:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if signing["algorithm"] != SIGNATURE_ALGORITHM:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    _key_id(signing["key_id"])

    identity = document["payload_identity"]
    if not isinstance(identity, str) or not identity.startswith("sha256:"):
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    _sha256_hex(identity.removeprefix("sha256:"))

    inventory = _parse_inventory(document["inventory"])
    _parse_executables(document["executables"], platform["os"], inventory)
    return str(release_version), inventory


def _parse_inventory(value: Any) -> tuple[InventoryEntry, ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_INVENTORY_ENTRIES:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    entries: list[InventoryEntry] = []
    total = 0
    for member in value:
        if not isinstance(member, dict) or set(member) != {"path", "sha256", "size", "executable"}:
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
        size = member["size"]
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_FILE_BYTES:
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
        if not isinstance(member["executable"], bool):
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
        total += size
        if total > MAX_PAYLOAD_BYTES:
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
        entries.append(
            InventoryEntry(
                path=_member(member["path"]),
                sha256=_sha256_hex(member["sha256"]),
                size=size,
                executable=member["executable"],
            )
        )
    names = [entry.path.as_posix() for entry in entries]
    if len(set(names)) != len(names):
        # Two entries for one path is not a drift the verifier can adjudicate: the
        # digests may differ, and picking either would be inventing an answer.
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if names != sorted(names, key=lambda name: name.encode("utf-8")):
        # A fixed order is what makes the canonical bytes -- and therefore the
        # identity -- reproducible from the same set of files in any language.
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if RUNTIME_MANIFEST_NAME in names or RUNTIME_SIGNATURE_NAME in names:
        # A manifest cannot contain its own digest. The two metadata documents are
        # covered by the signature over the manifest instead.
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    return tuple(entries)


def _parse_executables(
    value: Any, operating_system: str, inventory: tuple[InventoryEntry, ...]
) -> None:
    if not isinstance(value, dict) or set(value) != {"cli", "service"}:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    fixed = EXECUTABLE_LAYOUT[operating_system]
    declared: list[str] = []
    for name, expected in zip(("cli", "service"), fixed, strict=True):
        entry = value[name]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise _refuse(RuntimeRefusal.METADATA_INVALID)
        _sha256_hex(entry["sha256"])
        if _member(entry["path"]).as_posix() != expected:
            # The layout is a constant. Metadata may restate it and may not move it.
            raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
        declared.append(expected)

    by_path = {entry.path.as_posix(): entry for entry in inventory}
    for name, path in zip(("cli", "service"), declared, strict=True):
        entry = by_path.get(path)
        if entry is None or entry.sha256 != value[name]["sha256"] or not entry.executable:
            # Both executables must be inventoried, agree with the inventory's own
            # digest, and be the ones the inventory marks executable. "One payload,
            # one pair" is decided here, before a byte is read.
            raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    if {entry.path.as_posix() for entry in inventory if entry.executable} != set(declared):
        # Nothing but the product pair may be executable. A third executable file in
        # a payload is a command this contract never admitted.
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)


# --------------------------------------------------------------------------
# Filesystem inspection
# --------------------------------------------------------------------------


def _lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as failure:
        raise _refuse(RuntimeRefusal.IO_FAILURE) from failure


def _refuse_reparse(status: os.stat_result) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    if getattr(status, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        # Windows: a junction or mount point is not a symlink and leaves the
        # candidate root just as effectively.
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)


def require_directory(path: Path) -> None:
    """Refuse `path` unless it is a real directory reached through no reparse point."""
    status = _lstat(path)
    _refuse_reparse(status)
    if not stat.S_ISDIR(status.st_mode):
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)


def _scan(root: Path) -> tuple[list[tuple[PurePosixPath, Path]], list[Path]]:
    """Every file and directory beneath `root`, refusing anything that is not one.

    Symlinks, junctions, devices, sockets and FIFOs are refusals rather than skips:
    the inventory comparison below is an equality, so a skipped entry would be an
    entry the manifest never had to declare.
    """
    files: list[tuple[PurePosixPath, Path]] = []
    directories: list[Path] = []
    pending: list[tuple[PurePosixPath, Path]] = [(PurePosixPath(), root)]
    seen = 0
    while pending:
        relative, directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as failure:
            raise _refuse(RuntimeRefusal.IO_FAILURE) from failure
        for child in children:
            seen += 1
            if seen > MAX_INVENTORY_ENTRIES:
                raise _refuse(RuntimeRefusal.METADATA_INVALID)
            status = _lstat(Path(child.path))
            _refuse_reparse(status)
            member = relative / child.name
            if stat.S_ISDIR(status.st_mode):
                directories.append(Path(child.path))
                pending.append((member, Path(child.path)))
            elif stat.S_ISREG(status.st_mode):
                files.append((member, Path(child.path)))
            else:
                raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    return files, directories


def _digest_file(path: Path, expected_size: int) -> str:
    """The SHA-256 of `path`, read in bounded chunks and never through a symlink.

    ``O_NOFOLLOW`` closes the window between the ``lstat`` that classified this
    entry and the ``open`` that reads it: without it, a symlink swapped in after the
    check is followed, and the digest belongs to a file outside the payload. The
    length is bounded on the way through rather than trusted from ``stat``, so a
    file growing under the reader is a refusal instead of an unbounded read.
    """
    digest = hashlib.sha256()
    read = 0
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                if read > expected_size:
                    raise _refuse(RuntimeRefusal.IO_FAILURE)
                digest.update(chunk)
    except OSError as failure:
        raise _refuse(RuntimeRefusal.IO_FAILURE) from failure
    if read != expected_size:
        raise _refuse(RuntimeRefusal.IO_FAILURE)
    return digest.hexdigest()


def _read_document(path: Path, *, limit: int, absent: RuntimeRefusal) -> dict[str, Any]:
    """One bounded metadata document, or the refusal `absent` when there is none.

    An absent document is its own answer and not an IO failure: a payload with no
    manifest has invalid metadata, and one with no detached signature is untrusted.
    Both are repairs rather than retries, which is why the caller names the code.
    """
    status = _lstat_optional(path)
    if status is None:
        raise _refuse(absent)
    _refuse_reparse(status)
    if not stat.S_ISREG(status.st_mode):
        raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError as failure:
        raise _refuse(absent) from failure
    except OSError as failure:
        raise _refuse(RuntimeRefusal.IO_FAILURE) from failure
    try:
        return parse_canonical_document(raw, limit=limit)
    except CanonicalJsonError as failure:
        raise _refuse(RuntimeRefusal.METADATA_INVALID) from failure


# --------------------------------------------------------------------------
# Installation policy
# --------------------------------------------------------------------------


def _enforce_installation_policy(
    root: Path,
    files: list[tuple[PurePosixPath, Path]],
    directories: list[Path],
    executables: frozenset[str],
) -> None:
    """Per-user immutability, as far as a filesystem can provide it.

    Runtime payload v1 is a per-user installation, and this is what that means on
    POSIX: every component belongs to the effective user, no component is writable
    by anybody, ordinary files carry no execute bit, and exactly the two product
    executables are owner-executable. Group- and world-writable are excluded by the
    same test that excludes owner-writable, so there is one rule rather than three.

    **This is defence in depth and is not the trust root.** The same user can
    ``chmod`` any of it back. What makes tampering unusable is that the signature
    and every digest are reverified at each launch; the mode policy raises the cost
    of a partial write and closes the accidental cases. That order is stated here
    rather than left for someone to infer from a passing test.

    Windows takes the reparse-point and regular-file half above and none of this
    one: its mode bits are an emulation, ``st_uid`` is always zero, and enforcing an
    emulation would report a property nobody has.
    """
    if os.name == "nt":
        return
    user = os.geteuid()
    for path in (root, *directories):
        status = _lstat(path)
        if status.st_uid != user or status.st_mode & 0o222:
            raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    for member, path in files:
        status = _lstat(path)
        if status.st_uid != user or status.st_mode & 0o222:
            raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
        if member.as_posix() in executables:
            if not status.st_mode & 0o100:
                raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
        elif status.st_mode & 0o111:
            raise _refuse(RuntimeRefusal.LAYOUT_INVALID)


def harden_payload(root: Path, inventory: Sequence[InventoryEntry]) -> None:
    """Make a verified payload non-writable, from the inventory rather than from disk.

    The executable bit comes from the *signed* inventory and not from the mode the
    staged copy happened to carry, which is the whole point: a source tree with
    everything at ``0755`` would otherwise harden into a payload full of executable
    files, and the pair would no longer be a pair.

    Directories go last and deepest-first. A directory with no write bit still
    admits ``chmod`` of the children already in it, so the order is for legibility
    rather than for correctness -- but it also means an interrupted hardening leaves
    a tree that is more locked down than it started, never less.
    """
    executable = {entry.path.as_posix() for entry in inventory if entry.executable}
    files, directories = _scan(root)
    for member, path in files:
        _chmod(path, 0o500 if member.as_posix() in executable else 0o400)
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        _chmod(path, 0o500)
    _chmod(root, 0o500)


def _chmod(path: Path, mode: int) -> None:
    try:
        if os.name == "nt":
            # NTFS has no mode bits; this clears the write attribute, which is the
            # nearest equivalent and is what `stat.S_IREAD` means on Windows.
            os.chmod(path, stat.S_IREAD)
        else:
            os.chmod(path, mode)
    except OSError as failure:
        raise _refuse(RuntimeRefusal.IO_FAILURE) from failure


def unharden_tree(root: Path) -> None:
    """Restore owner write on a hardened tree so it can be removed.

    Only ever used to clean up a payload this process staged and then failed to
    publish. A hardened directory cannot have entries unlinked from it, so a plain
    recursive delete of a failed staging tree leaves it behind.
    """
    for path in (root, *_writable_first(root)):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:  # pragma: no cover - best effort on a tree being discarded
            pass


def _writable_first(root: Path) -> Iterator[Path]:
    for parent, directories, names in os.walk(root):
        for name in (*directories, *names):
            yield Path(parent) / name


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def verify_payload(
    payload_root: Path,
    *,
    trust_anchors: Sequence[TrustAnchor],
    verification_time: datetime,
    bootstrap_contract_version: str = BOOTSTRAP_CONTRACT_VERSION,
    minimum_release_version: str | None = None,
    host_operating_system: str = HOST_OPERATING_SYSTEM,
    host_architecture: str = HOST_ARCHITECTURE,
    expected_payload_identity: str | None = None,
    expected_release_version: str | None = None,
    enforce_installation_policy: bool = True,
) -> VerifiedRuntime:
    """Verify one candidate payload completely, or refuse without naming a path.

    The order below is the contract, because each step is only meaningful once the
    one before it has held:

    1. the root is a real directory reached through no reparse point;
    2. the manifest parses strictly, within its bound, with no duplicate name;
    3. the identity is **computed** and compared with the declared one;
    4. the detached signature verifies against an approved key that is inside its
       window at ``verification_time``;
    5. the caller's expectations -- the directory the payload sits in, the release
       the selector named, the operating system and architecture the host is, the
       contract the consumer implements -- are checked against the now-authenticated
       manifest;
    6. the tree on disk is enumerated and must equal the inventory exactly, with no
       member missing and none extra;
    7. every declared digest is recomputed from a bounded read;
    8. the installation's owner and mode policy holds.

    Steps 3 and 4 are why 5 to 8 are worth doing at all: before them, the manifest
    is an attacker-supplied document and comparing anything against it proves only
    self-consistency.

    ``enforce_installation_policy`` is off for exactly one caller -- installation,
    which verifies a *staged* tree that has not been hardened yet and hardens it
    only once this function has returned. Every resolution path leaves it on.
    """
    require_directory(payload_root)

    document = _read_document(
        payload_root / RUNTIME_MANIFEST_NAME,
        limit=MAX_MANIFEST_BYTES,
        absent=RuntimeRefusal.METADATA_INVALID,
    )
    release_version, inventory = parse_manifest(document)

    identity = payload_identity(document)
    if document["payload_identity"] != identity:
        raise _refuse(RuntimeRefusal.TAMPERED)

    _verify_signature(
        payload_root,
        manifest=document,
        identity=identity,
        trust_anchors=trust_anchors,
        verification_time=verification_time,
    )

    if expected_payload_identity is not None and expected_payload_identity != identity:
        # The candidate directory is named for the identity. A payload that verifies
        # perfectly but sits under another payload's name is refused: the selector
        # asked for a different thing than the one that is here.
        raise _refuse(RuntimeRefusal.TAMPERED)
    if expected_release_version is not None and expected_release_version != release_version:
        raise _refuse(RuntimeRefusal.TAMPERED)

    _check_compatibility(
        document,
        release_version=release_version,
        bootstrap_contract_version=bootstrap_contract_version,
        minimum_release_version=minimum_release_version,
        host_operating_system=host_operating_system,
        host_architecture=host_architecture,
    )

    files, directories = _scan(payload_root)
    present = {member.as_posix(): path for member, path in files}
    for name in (RUNTIME_MANIFEST_NAME, RUNTIME_SIGNATURE_NAME):
        if present.pop(name, None) is None:
            raise _refuse(RuntimeRefusal.LAYOUT_INVALID)
    declared = {entry.path.as_posix(): entry for entry in inventory}
    if set(present) != set(declared):
        # Equality in both directions. A missing member is an incomplete payload; an
        # extra one is a file nobody signed sitting inside a trusted directory.
        raise _refuse(RuntimeRefusal.TAMPERED)
    for name, entry in sorted(declared.items()):
        path = present[name]
        status = _lstat(path)
        if status.st_size != entry.size:
            raise _refuse(RuntimeRefusal.TAMPERED)
        if _digest_file(path, entry.size) != entry.sha256:
            raise _refuse(RuntimeRefusal.TAMPERED)

    executables = frozenset(entry.path.as_posix() for entry in inventory if entry.executable)
    if enforce_installation_policy:
        _enforce_installation_policy(payload_root, files, directories, executables)

    cli_relative, service_relative = EXECUTABLE_LAYOUT[document["platform"]["os"]]
    return VerifiedRuntime(
        release_version=release_version,
        payload_identity=identity,
        runtime_root=payload_root,
        cli_path=payload_root / PurePosixPath(cli_relative),
        service_path=payload_root / PurePosixPath(service_relative),
        inventory=inventory,
    )


def _verify_signature(
    payload_root: Path,
    *,
    manifest: Mapping[str, Any],
    identity: str,
    trust_anchors: Sequence[TrustAnchor],
    verification_time: datetime,
) -> None:
    if verification_time.tzinfo is None:
        raise ValueError("verification_time must be timezone-aware")
    anchors: dict[str, TrustAnchor] = {}
    for anchor in trust_anchors:
        if anchor.key_id in anchors:
            raise ValueError(f"two trust anchors share the key id {anchor.key_id!r}")
        anchors[anchor.key_id] = anchor

    document = _read_document(
        payload_root / RUNTIME_SIGNATURE_NAME,
        limit=MAX_SIGNATURE_BYTES,
        absent=RuntimeRefusal.UNTRUSTED,
    )
    if set(document) != {
        "signature_version",
        "key_id",
        "algorithm",
        "payload_identity",
        "signature",
    }:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if document["signature_version"] != RELEASE_SIGNATURE_VERSION:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if document["algorithm"] != SIGNATURE_ALGORITHM:
        raise _refuse(RuntimeRefusal.UNTRUSTED)
    if document["key_id"] != manifest["signing"]["key_id"]:
        # The manifest names the key inside the signed bytes; the detached document
        # names it outside them. They must agree, or one of the two was substituted.
        raise _refuse(RuntimeRefusal.UNTRUSTED)
    if document["payload_identity"] != identity:
        raise _refuse(RuntimeRefusal.TAMPERED)

    approved = anchors.get(document["key_id"])
    if approved is None or not approved.usable_at(verification_time):
        # Unknown, not yet valid, expired and retired are one answer to a consumer:
        # this release was not issued by anybody currently approved to issue one.
        raise _refuse(RuntimeRefusal.UNTRUSTED)

    try:
        signature = base64.b64decode(str(document["signature"]), validate=True)
    except (ValueError, TypeError) as failure:
        raise _refuse(RuntimeRefusal.METADATA_INVALID) from failure
    try:
        Ed25519PublicKey.from_public_bytes(approved.public_key).verify(
            signature, signed_manifest_bytes(manifest, identity)
        )
    except (InvalidSignature, ValueError) as failure:
        raise _refuse(RuntimeRefusal.UNTRUSTED) from failure


def _check_compatibility(
    manifest: Mapping[str, Any],
    *,
    release_version: str,
    bootstrap_contract_version: str,
    minimum_release_version: str | None,
    host_operating_system: str,
    host_architecture: str,
) -> None:
    """An authentic release that cannot serve this consumer is still a refusal.

    Separate from every code above it, and deliberately so: nothing is wrong with
    the payload, the remedy is to install a different Core, and telling a consumer
    "tampered" when the answer is "too old" sends them to the wrong repair.

    Both halves of ``platform`` are compared. The architecture used to be parsed and
    then ignored, which meant an arm64 host resolved a correctly signed x86_64
    payload all the way to a descriptor and handed the launcher a pair it cannot
    execute -- an authentic release that cannot serve this consumer, which is exactly
    what this code is for.
    """
    if manifest["platform"]["os"] != host_operating_system:
        raise _refuse(RuntimeRefusal.INCOMPATIBLE)
    if manifest["platform"]["arch"] != host_architecture:
        raise _refuse(RuntimeRefusal.INCOMPATIBLE)
    window = (
        _contract(manifest["compatibility"]["minimum_bootstrap_contract"]),
        _contract(manifest["compatibility"]["maximum_bootstrap_contract"]),
    )
    if not window[0] <= _contract(bootstrap_contract_version) <= window[1]:
        raise _refuse(RuntimeRefusal.INCOMPATIBLE)
    if minimum_release_version is not None and _semver(release_version) < _semver(
        minimum_release_version
    ):
        raise _refuse(RuntimeRefusal.INCOMPATIBLE)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve_runtime(
    *,
    installation_root: Path,
    trust_anchors: Sequence[TrustAnchor],
    verification_time: datetime,
    bootstrap_contract_version: str = BOOTSTRAP_CONTRACT_VERSION,
    minimum_release_version: str | None = None,
    host_operating_system: str = HOST_OPERATING_SYSTEM,
    host_architecture: str = HOST_ARCHITECTURE,
) -> VerifiedRuntime:
    """Resolve the selected installed runtime, or refuse without naming a path.

    ``active.json`` is read as a **selector and nothing more**. Two of its four
    members are used -- the release version and the payload digest -- and the
    candidate directory is derived from them beneath the explicit installation root.
    Its ``relative_path`` is checked against that derived value rather than supplying
    one, so a record rewritten to ``../../../elsewhere`` selects nothing: there is no
    code path in which a value out of that file becomes a path this function opens.

    Everything is redone on every call. A previously returned descriptor is a cache
    hint for the consumer and never continuing authority.
    """
    if not installation_root.is_absolute():
        raise ValueError("installation_root must be absolute")
    require_directory(installation_root)

    # An absent selector is `runtime_not_installed`; a symlinked one is refused at
    # the installation boundary. `_read_document` decides both.
    record = _read_document(
        installation_root / "active.json",
        limit=MAX_SELECTION_BYTES,
        absent=RuntimeRefusal.NOT_INSTALLED,
    )
    if set(record) != {"schema_version", "release_version", "payload_digest", "relative_path"}:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    if record["schema_version"] != 1:
        raise _refuse(RuntimeRefusal.METADATA_INVALID)
    release_version = record["release_version"]
    _semver(release_version)
    digest = _sha256_hex(record["payload_digest"])
    derived = PurePosixPath("runtimes") / str(release_version) / digest
    if record["relative_path"] != derived.as_posix():
        raise _refuse(RuntimeRefusal.METADATA_INVALID)

    runtimes = installation_root / "runtimes"
    release_root = runtimes / str(release_version)
    candidate = release_root / digest
    if any(_lstat_optional(path) is None for path in (runtimes, release_root, candidate)):
        # The selector names a candidate whose directory is not there. That is the
        # transient state an installation or a reconciliation passes through, so it
        # is a bounded retry rather than "nothing is installed" -- which would send
        # a consumer to reinstall over a race.
        raise _refuse(RuntimeRefusal.BUSY)
    require_directory(runtimes)
    require_directory(release_root)

    return verify_payload(
        candidate,
        trust_anchors=trust_anchors,
        verification_time=verification_time,
        bootstrap_contract_version=bootstrap_contract_version,
        minimum_release_version=minimum_release_version,
        host_operating_system=host_operating_system,
        host_architecture=host_architecture,
        expected_payload_identity=f"sha256:{digest}",
        expected_release_version=str(release_version),
        enforce_installation_policy=True,
    )


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as failure:
        raise _refuse(RuntimeRefusal.IO_FAILURE) from failure


__all__ = [
    "BOOTSTRAP_CONTRACT_VERSION",
    "EXECUTABLE_LAYOUT",
    "HOST_ARCHITECTURE",
    "HOST_OPERATING_SYSTEM",
    "IDENTITY_DOMAIN",
    "PAYLOAD_ARCHITECTURES",
    "PAYLOAD_MANIFEST_VERSION",
    "RELEASE_SIGNATURE_VERSION",
    "RUNTIME_DESCRIPTOR_VERSION",
    "RUNTIME_MANIFEST_NAME",
    "RUNTIME_SIGNATURE_NAME",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_DOMAIN",
    "CanonicalJsonError",
    "InventoryEntry",
    "RuntimeRefusal",
    "RuntimeResolutionError",
    "TrustAnchor",
    "VerifiedRuntime",
    "canonical_json",
    "harden_payload",
    "parse_canonical_document",
    "parse_manifest",
    "payload_identity",
    "require_directory",
    "resolve_runtime",
    "signed_manifest_bytes",
    "unharden_tree",
    "verify_payload",
]
