"""Identity of one installable OCR resource set, and the store's byte capacity.

This is the foundation slice only: it names what a resource set *is* and how
much room the store is allowed to occupy. Nothing here reads a manifest, touches
the filesystem, installs or evicts anything; selection of already-installed
models stays in :mod:`omnivia_core_runtime.service.ocr_resources`.

An identity is the five values that together decide whether two resource sets
are the same bytes for the same purpose: the language, the engine and model
revisions they were built for, the size, and the content digest. It is validated
at construction so a malformed token can never reach a key, and it is frozen and
hashable so it can be used directly as a dictionary key or set member.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

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
