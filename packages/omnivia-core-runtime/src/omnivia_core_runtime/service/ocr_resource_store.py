"""Identity of one installable OCR resource set, and the store's state manifest.

An identity is the five values that together decide whether two resource sets
are the same bytes for the same purpose: the language, the engine and model
revisions they were built for, the size, and the content digest. It is validated
at construction so a malformed token can never reach a key, and it is frozen and
hashable so it can be used directly as a dictionary key or set member.

The state manifest records what the store currently holds: which identities are
installed, and which operations have pinned one of them. It is a single
versioned, canonically serialised JSON document under a caller-provided root,
written temp-file → fsync → atomic rename → directory fsync so a crash leaves
either the whole old document or the whole new one. The temporary is created
exclusively under a fresh unguessable name, so no path already sitting in the
root -- a symlink out of it least of all -- is ever opened or truncated.

Still out of scope here: installs, eviction, capacity planning, catalogue
integration, networking and update logic. Nothing in this module fetches or
deletes resource bytes; it only describes the state those operations will later
agree on. Selection of already-installed models stays in
:mod:`omnivia_core_runtime.service.ocr_resources`.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from omnivia_core_runtime.ownership.discovery import restrict_to_owner
from omnivia_core_runtime.workspace.filesystem import fsync_directory

#: Bytes the installed OCR resource sets may occupy in total. Exactly 512 MiB.
DEFAULT_OCR_RESOURCE_CAPACITY_BYTES: Final = 512 * 1024 * 1024

#: Longest accepted language or version token. Generous for a tag such as
#: ``chi_tra_vert`` or a revision such as ``5.5.3-rc1`` and short enough that a
#: stable key stays bounded.
MAX_TOKEN_LENGTH: Final = 64

#: A language or version token: printable ASCII, no separators that could make a
#: joined key ambiguous, and nothing a path or shell argument would reinterpret.
_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._+-]*[A-Za-z0-9])?\Z")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")

#: Separator for :attr:`OcrResourceSetIdentity.stable_key`. The token pattern
#: excludes it, so no field value can contain it and no two distinct identities
#: can produce the same key.
_KEY_SEPARATOR: Final = "|"


class OcrResourceIdentityInvalid(ValueError):
    """An OCR resource set identity field is missing or malformed."""


def _validated_token(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise OcrResourceIdentityInvalid(f"{name} must be a string")
    if not value:
        raise OcrResourceIdentityInvalid(f"{name} must not be empty")
    if len(value) > MAX_TOKEN_LENGTH:
        raise OcrResourceIdentityInvalid(
            f"{name} must be at most {MAX_TOKEN_LENGTH} characters"
        )
    if not _TOKEN_PATTERN.match(value):
        raise OcrResourceIdentityInvalid(
            f"{name} must be an ASCII token of letters, digits, '.', '_', '+' or '-'"
        )
    return value


@dataclass(frozen=True, slots=True)
class OcrResourceSetIdentity:
    """The five values that identify one installable OCR resource set.

    Two identities are equal when all five agree, so the same language built
    from a different model revision, or the same digest claimed at a different
    size, is a different resource set.
    """

    language: str
    engine_version: str
    model_version: str
    byte_size: int
    sha256: str

    def __post_init__(self) -> None:
        _validated_token("language", self.language)
        _validated_token("engine_version", self.engine_version)
        _validated_token("model_version", self.model_version)
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise OcrResourceIdentityInvalid("byte_size must be an integer")
        if self.byte_size <= 0:
            raise OcrResourceIdentityInvalid("byte_size must be positive")
        if not isinstance(self.sha256, str) or not _SHA256_PATTERN.match(self.sha256):
            raise OcrResourceIdentityInvalid(
                "sha256 must be exactly 64 lowercase hexadecimal characters"
            )

    @property
    def stable_key(self) -> str:
        """A deterministic key carrying all five fields unambiguously.

        The separator cannot occur inside any field, so the key is reversible
        and distinct identities never collide.
        """
        return _KEY_SEPARATOR.join(
            (
                self.language,
                self.engine_version,
                self.model_version,
                str(self.byte_size),
                self.sha256,
            )
        )


#: Schema version of the state document this module reads and writes. A document
#: carrying any other version is refused and left untouched: a newer store was
#: written by a newer build, and rewriting it at this version would discard
#: whatever that build recorded.
OCR_RESOURCE_STORE_SCHEMA_VERSION: Final = 1

#: The single state document under the store root.
OCR_RESOURCE_STORE_STATE_FILENAME: Final = "state.json"

#: Bytes of randomness in a temporary document's name. The file is created with
#: ``O_EXCL``, so a guessed name loses the race rather than winning it, and an
#: unguessable one means there is no race to run.
_TEMPORARY_NAME_BYTES: Final = 16

#: Names tried before a save gives up. A random name can only collide with one
#: something else pre-created, and ``O_EXCL`` turns that into another attempt
#: rather than a write through whatever is sitting there.
_TEMPORARY_ATTEMPTS: Final = 8

#: Create the temporary document and nothing else. ``O_CREAT | O_EXCL`` refuses
#: every name that already exists -- a stale ``.state.json.tmp`` symlink left by
#: an older build among them -- so no pre-existing path is opened, followed or
#: truncated, and ``O_NOFOLLOW`` refuses a symlink besides, where it exists.
_TEMPORARY_FLAGS: Final = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)

_DOCUMENT_KEYS: Final = frozenset({"schemaVersion", "entries", "pins"})
_IDENTITY_KEYS: Final = frozenset(
    {"language", "engineVersion", "modelVersion", "byteSize", "sha256"}
)
_PIN_KEYS: Final = frozenset({"operationId", "identity"})


class OcrResourceStoreError(Exception):
    """The OCR resource store state could not be read, validated or written."""


class OcrResourceStoreVersionUnsupported(OcrResourceStoreError):
    """The stored state document carries a schema version this build cannot read.

    Raised instead of parsing, and never accompanied by a write, so a store
    written by a newer build survives being opened by an older one.
    """


@dataclass(frozen=True, slots=True)
class OcrResourcePin:
    """One operation's claim on one installed resource set.

    A pin is the record that stops a later eviction pass from removing bytes an
    in-flight operation still needs. Nothing consumes it yet; this slice only
    makes it storable.
    """

    operation_id: str
    identity: OcrResourceSetIdentity

    def __post_init__(self) -> None:
        _validated_token("operation_id", self.operation_id)
        if not isinstance(self.identity, OcrResourceSetIdentity):
            raise OcrResourceIdentityInvalid(
                "identity must be an OcrResourceSetIdentity"
            )

    @property
    def stable_key(self) -> str:
        """Deterministic key for this exact (operation, resource set) claim."""
        return _KEY_SEPARATOR.join((self.operation_id, self.identity.stable_key))


@dataclass(frozen=True, slots=True)
class OcrResourceStoreState:
    """What the store holds: installed resource sets and the pins against them.

    Order is not part of the state. Both tuples are sorted by stable key at
    construction, so two states assembled in different orders are equal and
    serialise to identical bytes.
    """

    entries: tuple[OcrResourceSetIdentity, ...] = ()
    pins: tuple[OcrResourcePin, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entries",
            _sorted_unique("entry", self.entries, OcrResourceSetIdentity),
        )
        object.__setattr__(self, "pins", _sorted_unique("pin", self.pins, OcrResourcePin))


def _sorted_unique(label: str, values: object, expected: type) -> tuple[Any, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise OcrResourceStoreError(f"{label}s must be a sequence")
    if not all(isinstance(value, expected) for value in values):
        raise OcrResourceStoreError(f"every {label} must be a {expected.__name__}")
    ordered = tuple(sorted(values, key=lambda value: value.stable_key))
    keys = [value.stable_key for value in ordered]
    if len(set(keys)) != len(keys):
        raise OcrResourceStoreError(f"duplicate {label} in store state")
    return ordered


def _identity_document(identity: OcrResourceSetIdentity) -> dict[str, object]:
    return {
        "language": identity.language,
        "engineVersion": identity.engine_version,
        "modelVersion": identity.model_version,
        "byteSize": identity.byte_size,
        "sha256": identity.sha256,
    }


def _require_object(label: str, value: object, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OcrResourceStoreError(f"{label} must be a JSON object")
    present = set(value)
    unknown = sorted(present - allowed)
    if unknown:
        raise OcrResourceStoreError(f"{label} has unknown keys: {', '.join(unknown)}")
    missing = sorted(allowed - present)
    if missing:
        raise OcrResourceStoreError(f"{label} is missing keys: {', '.join(missing)}")
    return value


def _identity_from_document(label: str, value: object) -> OcrResourceSetIdentity:
    document = _require_object(label, value, _IDENTITY_KEYS)
    try:
        # Construction is the validation: the identity's own checks are the only
        # definition of a well-formed resource set, so a stored document cannot
        # be admitted by a looser rule than a constructed one.
        return OcrResourceSetIdentity(
            language=document["language"],
            engine_version=document["engineVersion"],
            model_version=document["modelVersion"],
            byte_size=document["byteSize"],
            sha256=document["sha256"],
        )
    except OcrResourceIdentityInvalid as exc:
        raise OcrResourceStoreError(f"{label} is invalid: {exc}") from exc


def _require_sequence(label: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise OcrResourceStoreError(f"{label} must be a JSON array")
    return value


def _temporary_name() -> str:
    """A name beside the state document that nobody could have pre-created."""
    token = os.urandom(_TEMPORARY_NAME_BYTES).hex()
    return f".{OCR_RESOURCE_STORE_STATE_FILENAME}.{token}.tmp"


def _write_all(descriptor: int, payload: bytes) -> None:
    """Put the whole payload in the file, however little one write accepts.

    ``os.write`` is the raw system call: it may accept less than it was handed
    and reports that only in its return value, which raises nothing. Writing
    once and moving on would rename a *truncated* document over the target.
    Zero accepted ends it rather than spinning, because a descriptor that took
    none of what it was given is not one a further attempt makes progress on.
    """
    written = 0
    while written < len(payload):
        progress = os.write(descriptor, payload[written:])
        if progress <= 0:
            raise OSError("temporary state document accepted no bytes")
        written += progress


class OcrResourceStore:
    """The managed OCR resource store's state document under one root.

    The root is supplied by the caller; this class never chooses a location. It
    is created owner-only on first write and is refused outright if it is a
    symlink or not a directory, because a store that can be redirected by an
    entry in its own path is not owner-only in any useful sense.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def state_path(self) -> Path:
        return self._root / OCR_RESOURCE_STORE_STATE_FILENAME

    def load(self) -> OcrResourceStoreState:
        """Read the state document, or return the empty state if there is none.

        Writes nothing, including when the store does not exist yet: a read must
        never be the thing that creates a store.
        """
        path = self.state_path
        if self._root.exists() or self._root.is_symlink():
            self._check_root()
        self._check_state_path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return OcrResourceStoreState()
        except OSError as exc:
            raise OcrResourceStoreError(f"store state is unreadable: {exc}") from exc
        return self._decode(raw)

    def save(self, state: OcrResourceStoreState) -> None:
        """Atomically replace the state document with ``state``.

        The existing document's schema version is checked first, so a store
        written by a newer build is refused rather than overwritten.
        """
        if not isinstance(state, OcrResourceStoreState):
            raise OcrResourceStoreError("state must be an OcrResourceStoreState")
        self._refuse_unsupported_existing_version()

        self._root.mkdir(parents=True, exist_ok=True)
        restrict_to_owner(self._root, directory=True)
        self._check_root()

        target = self.state_path
        payload = self._encode(state)
        temporary, descriptor = self._create_temporary()
        try:
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # Redundant with the open mode on POSIX, load-bearing on Windows,
            # where a new file inherits its parent's DACL and ignores the mode.
            restrict_to_owner(temporary, directory=False)
            os.replace(temporary, target)
        except BaseException as exc:
            # The target still holds the previous document; drop the exact file
            # this call created -- never a resolved name -- so a failed write
            # leaves no partial state behind to be adopted later. A cleanup that
            # cannot run must not replace the failure that caused it.
            with suppress(OSError):
                temporary.unlink()
            if isinstance(exc, OSError):
                raise OcrResourceStoreError(
                    f"store state could not be written: {exc}"
                ) from exc
            raise
        try:
            fsync_directory(self._root)
        except OSError as exc:
            raise OcrResourceStoreError(
                f"store state could not be made durable: {exc}"
            ) from exc

    def _create_temporary(self) -> tuple[Path, int]:
        """Create an unused owner-only temporary beside the state document.

        In the root this call has just validated, so the rename stays within one
        filesystem, and under a fresh unguessable name each attempt: nothing that
        is already there is reused, followed or truncated.
        """
        refusal: OSError | None = None
        for _attempt in range(_TEMPORARY_ATTEMPTS):
            temporary = self._root / _temporary_name()
            try:
                return temporary, os.open(temporary, _TEMPORARY_FLAGS, 0o600)
            except FileExistsError as exc:
                # Something already holds that name -- including a symlink, which
                # O_EXCL reports this way rather than following. Try another.
                refusal = exc
            except OSError as exc:
                raise OcrResourceStoreError(
                    f"store state could not be written: {exc}"
                ) from exc
        raise OcrResourceStoreError(
            f"store state could not be written: {refusal}"
        ) from refusal

    def _encode(self, state: OcrResourceStoreState) -> bytes:
        document = {
            "schemaVersion": OCR_RESOURCE_STORE_SCHEMA_VERSION,
            "entries": [_identity_document(entry) for entry in state.entries],
            "pins": [
                {
                    "operationId": pin.operation_id,
                    "identity": _identity_document(pin.identity),
                }
                for pin in state.pins
            ],
        }
        text = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return (text + "\n").encode("utf-8")

    def _decode(self, raw: bytes) -> OcrResourceStoreState:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OcrResourceStoreError(f"store state is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise OcrResourceStoreError("store state must be a JSON object")
        self._require_supported_version(data.get("schemaVersion"))

        document = _require_object("store state", data, _DOCUMENT_KEYS)
        entries = tuple(
            _identity_from_document(f"entry {index}", entry)
            for index, entry in enumerate(_require_sequence("entries", document["entries"]))
        )
        pins = []
        for index, value in enumerate(_require_sequence("pins", document["pins"])):
            pin = _require_object(f"pin {index}", value, _PIN_KEYS)
            identity = _identity_from_document(f"pin {index} identity", pin["identity"])
            try:
                pins.append(OcrResourcePin(pin["operationId"], identity))
            except OcrResourceIdentityInvalid as exc:
                raise OcrResourceStoreError(f"pin {index} is invalid: {exc}") from exc
        return OcrResourceStoreState(entries=entries, pins=tuple(pins))

    def _require_supported_version(self, version: object) -> None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise OcrResourceStoreError("schemaVersion must be an integer")
        if version != OCR_RESOURCE_STORE_SCHEMA_VERSION:
            raise OcrResourceStoreVersionUnsupported(
                f"store state schema version {version} is not supported; "
                f"this build reads version {OCR_RESOURCE_STORE_SCHEMA_VERSION}"
            )

    def _refuse_unsupported_existing_version(self) -> None:
        """Refuse to replace a document whose schema version is not ours."""
        if self._root.exists() or self._root.is_symlink():
            self._check_root()
        self._check_state_path()
        try:
            raw = self.state_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise OcrResourceStoreError(f"store state is unreadable: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Unreadable bytes carry no version to preserve, so replacing them is
            # a repair rather than a downgrade.
            return
        if isinstance(data, dict) and "schemaVersion" in data:
            self._require_supported_version(data["schemaVersion"])

    def _check_root(self) -> None:
        if self._root.is_symlink():
            raise OcrResourceStoreError(f"store root must not be a symlink: {self._root}")
        if not self._root.is_dir():
            raise OcrResourceStoreError(f"store root must be a directory: {self._root}")

    def _check_state_path(self) -> None:
        path = self.state_path
        if path.is_symlink():
            raise OcrResourceStoreError(f"store state must not be a symlink: {path}")


__all__ = [
    "DEFAULT_OCR_RESOURCE_CAPACITY_BYTES",
    "MAX_TOKEN_LENGTH",
    "OCR_RESOURCE_STORE_SCHEMA_VERSION",
    "OCR_RESOURCE_STORE_STATE_FILENAME",
    "OcrResourceIdentityInvalid",
    "OcrResourcePin",
    "OcrResourceSetIdentity",
    "OcrResourceStore",
    "OcrResourceStoreError",
    "OcrResourceStoreState",
    "OcrResourceStoreVersionUnsupported",
]
