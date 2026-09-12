"""UUIDv7 identity allocation (RFC 9562), standard library only.

`requires-python` for this repo is `>=3.11`, and `uuid.uuid7` only ships in
CPython 3.14, so the layout is implemented directly here: a 48-bit big-endian
millisecond timestamp, a 4-bit version, a 2-bit variant, and 74 random bits
(12 bits `rand_a`, 62 bits `rand_b`), which sorts lexicographically by
creation time -- the property `model_id`/`model_version_id`/`element_id`
allocation depends on.

:class:`UUIDv7Allocator` is the production default. :class:`IdAllocator` is
the seam a later batch (deterministic replay, tests) injects a fixed sequence
through instead.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Protocol, runtime_checkable

_VERSION = 0x7
_VARIANT = 0b10


def uuid7() -> uuid.UUID:
    """Return a new RFC 9562 UUIDv7, time-ordered to millisecond precision."""
    unix_ts_ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (unix_ts_ms & 0xFFFFFFFFFFFF) << 80
    value |= _VERSION << 76
    value |= rand_a << 64
    value |= _VARIANT << 62
    value |= rand_b
    return uuid.UUID(int=value)


@runtime_checkable
class IdAllocator(Protocol):
    """The seam every stable-ID field allocates through."""

    def new_id(self) -> str: ...


class UUIDv7Allocator:
    """Production default: a fresh, unpredictable UUIDv7 per call."""

    def new_id(self) -> str:
        return str(uuid7())


DEFAULT_ALLOCATOR: IdAllocator = UUIDv7Allocator()

__all__ = ["DEFAULT_ALLOCATOR", "IdAllocator", "UUIDv7Allocator", "uuid7"]
