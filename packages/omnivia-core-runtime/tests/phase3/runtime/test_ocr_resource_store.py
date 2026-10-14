"""OCR resource set identity and the store's byte capacity.

The properties proved are the ones a later change could silently break: that the
capacity is exactly 512 MiB, that an identity is frozen, hashable and equal only
when all five fields agree, that the stable key carries all five without
ambiguity, and that every malformed field is refused at construction rather than
reaching a key.
"""

from __future__ import annotations

import dataclasses

import pytest
from omnivia_core_runtime.service.ocr_resource_store import (
    DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
    MAX_TOKEN_LENGTH,
    OcrResourceIdentityInvalid,
    OcrResourceSetIdentity,
)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _identity(**overrides: object) -> OcrResourceSetIdentity:
    fields: dict[str, object] = {
        "language": "eng",
        "engine_version": "5.5.3",
        "model_version": "tessdata_fast-4.1.0",
        "byte_size": 4 * 1024 * 1024,
        "sha256": DIGEST,
    }
    fields.update(overrides)
    return OcrResourceSetIdentity(**fields)  # type: ignore[arg-type]


def test_capacity_is_exactly_512_mib() -> None:
    assert DEFAULT_OCR_RESOURCE_CAPACITY_BYTES == 536_870_912


def test_valid_construction_keeps_every_field() -> None:
    identity = _identity()
    assert identity.language == "eng"
    assert identity.engine_version == "5.5.3"
    assert identity.model_version == "tessdata_fast-4.1.0"
    assert identity.byte_size == 4 * 1024 * 1024
    assert identity.sha256 == DIGEST


@pytest.mark.parametrize("language", ["eng", "chi_tra", "chi_tra_vert", "a"])
def test_accepts_real_language_tags(language: str) -> None:
    assert _identity(language=language).language == language


def test_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _identity().language = "deu"  # type: ignore[misc]


def test_equal_identities_share_hash_and_stable_key() -> None:
    first, second = _identity(), _identity()
    assert first == second
    assert hash(first) == hash(second)
    assert first.stable_key == second.stable_key
    assert len({first, second}) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"language": "deu"},
        {"engine_version": "5.5.4"},
        {"model_version": "tessdata_fast-4.1.1"},
        {"byte_size": 4 * 1024 * 1024 + 1},
        {"sha256": OTHER_DIGEST},
    ],
)
def test_any_differing_field_is_a_different_identity(override: dict[str, object]) -> None:
    other = _identity(**override)
    assert other != _identity()
    assert other.stable_key != _identity().stable_key


def test_stable_key_carries_all_five_fields() -> None:
    identity = _identity()
    assert identity.stable_key == "|".join(
        ("eng", "5.5.3", "tessdata_fast-4.1.0", str(4 * 1024 * 1024), DIGEST)
    )


def test_stable_key_cannot_be_made_ambiguous_by_field_contents() -> None:
    """A value that would run two fields together is refused, not joined."""
    with pytest.raises(OcrResourceIdentityInvalid):
        _identity(language="eng|5.5.3")


@pytest.mark.parametrize("field", ["language", "engine_version", "model_version"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "eng ",
        " eng",
        "en g",
        "-eng",
        "eng-",
        "eng/deu",
        "../eng",
        "eng\n",
        "eng\x00",
        "日本語",
        "a" * (MAX_TOKEN_LENGTH + 1),
        None,
        5,
    ],
)
def test_malformed_tokens_are_refused(field: str, value: object) -> None:
    with pytest.raises(OcrResourceIdentityInvalid):
        _identity(**{field: value})


def test_longest_accepted_token_is_at_the_bound() -> None:
    assert _identity(language="a" * MAX_TOKEN_LENGTH).language == "a" * MAX_TOKEN_LENGTH


@pytest.mark.parametrize("byte_size", [0, -1, True, False, 1.0, "4", None])
def test_malformed_byte_size_is_refused(byte_size: object) -> None:
    with pytest.raises(OcrResourceIdentityInvalid):
        _identity(byte_size=byte_size)


@pytest.mark.parametrize(
    "sha256",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "a" * 63 + "g",
        "a" * 63 + " ",
        " " + "a" * 63,
        None,
        0,
    ],
)
def test_malformed_digest_is_refused(sha256: object) -> None:
    with pytest.raises(OcrResourceIdentityInvalid):
        _identity(sha256=sha256)
