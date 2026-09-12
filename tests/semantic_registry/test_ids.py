"""UUIDv7 allocator properties."""

from __future__ import annotations

import uuid

from omnivia_core.semantic_registry.ids import DEFAULT_ALLOCATOR, UUIDv7Allocator, uuid7


def test_uuid7_has_version_and_variant_bits() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_is_unique_across_many_calls() -> None:
    values = {uuid7() for _ in range(1000)}
    assert len(values) == 1000


def test_uuid7_string_form_round_trips() -> None:
    value = uuid7()
    assert uuid.UUID(str(value)) == value


def test_uuid7_orders_by_creation_time() -> None:
    # The 48-bit millisecond timestamp occupies the high bits, so two values
    # minted apart by a real time gap compare in creation order as plain UUIDs.
    earlier = uuid7()
    import time

    time.sleep(0.005)
    later = uuid7()
    assert earlier.int < later.int


def test_uuidv7_allocator_new_id_is_a_valid_uuid7_string() -> None:
    allocator = UUIDv7Allocator()
    new_id = allocator.new_id()
    parsed = uuid.UUID(new_id)
    assert parsed.version == 7


def test_default_allocator_is_a_uuidv7_allocator() -> None:
    assert isinstance(DEFAULT_ALLOCATOR, UUIDv7Allocator)
