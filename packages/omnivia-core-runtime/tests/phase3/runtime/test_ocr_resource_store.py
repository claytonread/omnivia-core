"""OCR resource set identity, the store's byte capacity, and its state manifest.

The properties proved are the ones a later change could silently break: that the
capacity is exactly 512 MiB, that an identity is frozen, hashable and equal only
when all five fields agree, that the stable key carries all five without
ambiguity, and that every malformed field is refused at construction rather than
reaching a key.

For the state manifest: that a round trip is lossless and its bytes depend only
on the state and not on assembly order, that the root and document are
owner-only, that a symlinked or non-directory path is refused, that a document
from a newer build is refused and left byte-for-byte intact, that malformed and
duplicated data is refused rather than partially adopted, that a missing
document reads as the empty state without creating anything, and that a failed
write, fsync or rename leaves the previous document in place with no temporary
beside it.

For the temporary document: that the predictable ``.state.json.tmp`` an older
build used is no longer opened at all, that a name already taken -- by a symlink
pointing out of the root, say -- is retried rather than written through, and that
a short ``os.write`` still stores the whole document rather than a prefix of it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from pathlib import Path

import pytest
from omnivia_core_runtime.service import ocr_resource_store as store_module
from omnivia_core_runtime.service.ocr_resource_store import (
    DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
    MAX_TOKEN_LENGTH,
    OCR_RESOURCE_STORE_SCHEMA_VERSION,
    OCR_RESOURCE_STORE_STATE_FILENAME,
    OcrResourceIdentityInvalid,
    OcrResourcePin,
    OcrResourceSetIdentity,
    OcrResourceStore,
    OcrResourceStoreError,
    OcrResourceStoreState,
    OcrResourceStoreVersionUnsupported,
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


# --- state records -----------------------------------------------------------


def _pin(operation_id: str = "op-1", **overrides: object) -> OcrResourcePin:
    return OcrResourcePin(operation_id, _identity(**overrides))


def test_state_defaults_to_empty() -> None:
    state = OcrResourceStoreState()
    assert state.entries == ()
    assert state.pins == ()


def test_state_sorts_so_assembly_order_is_not_state() -> None:
    first, second = _identity(language="deu"), _identity(language="eng")
    assert OcrResourceStoreState(entries=(first, second)) == OcrResourceStoreState(
        entries=(second, first)
    )


def test_state_refuses_duplicate_entries() -> None:
    with pytest.raises(OcrResourceStoreError, match="duplicate entry"):
        OcrResourceStoreState(entries=(_identity(), _identity()))


def test_state_refuses_duplicate_pins() -> None:
    with pytest.raises(OcrResourceStoreError, match="duplicate pin"):
        OcrResourceStoreState(pins=(_pin(), _pin()))


def test_one_operation_may_pin_two_resource_sets() -> None:
    state = OcrResourceStoreState(pins=(_pin(), _pin(language="deu")))
    assert len(state.pins) == 2


def test_pin_refuses_a_malformed_operation_id() -> None:
    with pytest.raises(OcrResourceIdentityInvalid):
        OcrResourcePin("op 1", _identity())


def test_pin_refuses_a_non_identity() -> None:
    with pytest.raises(OcrResourceIdentityInvalid):
        OcrResourcePin("op-1", "eng")  # type: ignore[arg-type]


@pytest.mark.parametrize("entries", ["eng", 5, None, {"a": 1}])
def test_state_refuses_a_non_sequence(entries: object) -> None:
    with pytest.raises(OcrResourceStoreError):
        OcrResourceStoreState(entries=entries)  # type: ignore[arg-type]


def test_state_refuses_wrongly_typed_members() -> None:
    with pytest.raises(OcrResourceStoreError, match="must be a"):
        OcrResourceStoreState(entries=(_pin(),))  # type: ignore[arg-type]


# --- the store ---------------------------------------------------------------


def _store(tmp_path: Path) -> OcrResourceStore:
    return OcrResourceStore(tmp_path / "ocr")


def _write_document(store: OcrResourceStore, document: object) -> bytes:
    store.root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document).encode("utf-8")
    store.state_path.write_bytes(payload)
    return payload


def test_missing_state_loads_as_empty_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load() == OcrResourceStoreState()
    assert not store.root.exists()


def test_missing_state_in_an_existing_root_loads_as_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.root.mkdir()
    assert store.load() == OcrResourceStoreState()
    assert not store.state_path.exists()


def test_round_trip_preserves_entries_and_pins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = OcrResourceStoreState(
        entries=(_identity(), _identity(language="deu")),
        pins=(_pin("op-2", language="deu"), _pin("op-1")),
    )
    store.save(state)
    assert store.load() == state


def test_serialisation_is_deterministic(tmp_path: Path) -> None:
    """Identical state must produce identical bytes, whatever order it arrived in."""
    first, second = _store(tmp_path), OcrResourceStore(tmp_path / "other")
    entries = (_identity(), _identity(language="deu"))
    first.save(OcrResourceStoreState(entries=entries, pins=(_pin(),)))
    second.save(OcrResourceStoreState(entries=entries[::-1], pins=(_pin(),)))
    assert first.state_path.read_bytes() == second.state_path.read_bytes()


def test_saving_twice_rewrites_the_same_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = OcrResourceStoreState(entries=(_identity(),))
    store.save(state)
    before = store.state_path.read_bytes()
    store.save(store.load())
    assert store.state_path.read_bytes() == before


def test_stored_document_carries_the_current_schema_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    document = json.loads(store.state_path.read_text("utf-8"))
    assert document["schemaVersion"] == OCR_RESOURCE_STORE_SCHEMA_VERSION
    assert document["entries"] == [
        {
            "language": "eng",
            "engineVersion": "5.5.3",
            "modelVersion": "tessdata_fast-4.1.0",
            "byteSize": 4 * 1024 * 1024,
            "sha256": DIGEST,
        }
    ]
    assert document["pins"] == []
    assert store.state_path.name == OCR_RESOURCE_STORE_STATE_FILENAME


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_root_and_state_file_are_owner_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState())
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.state_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_permissive_root_is_reduced_on_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.root.mkdir(mode=0o777)
    os.chmod(store.root, 0o777)
    store.save(OcrResourceStoreState())
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    assert [path.name for path in store.root.iterdir()] == [
        OCR_RESOURCE_STORE_STATE_FILENAME
    ]


# --- refused paths -----------------------------------------------------------


def test_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    store = OcrResourceStore(link)
    with pytest.raises(OcrResourceStoreError, match="root must not be a symlink"):
        store.load()
    with pytest.raises(OcrResourceStoreError, match="root must not be a symlink"):
        store.save(OcrResourceStoreState())
    assert not (real / OCR_RESOURCE_STORE_STATE_FILENAME).exists()


def test_broken_symlinked_root_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "absent", target_is_directory=True)
    with pytest.raises(OcrResourceStoreError, match="root must not be a symlink"):
        OcrResourceStore(link).load()


def test_non_directory_root_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ocr"
    path.write_text("not a directory", encoding="utf-8")
    store = OcrResourceStore(path)
    with pytest.raises(OcrResourceStoreError, match="root must be a directory"):
        store.load()
    with pytest.raises(OcrResourceStoreError):
        store.save(OcrResourceStoreState())


def test_symlinked_state_path_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.root.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(b"{}")
    store.state_path.symlink_to(elsewhere)
    with pytest.raises(OcrResourceStoreError, match="state must not be a symlink"):
        store.load()
    with pytest.raises(OcrResourceStoreError, match="state must not be a symlink"):
        store.save(OcrResourceStoreState())
    assert elsewhere.read_bytes() == b"{}"


def test_save_refuses_a_foreign_state_object(tmp_path: Path) -> None:
    with pytest.raises(OcrResourceStoreError, match="OcrResourceStoreState"):
        _store(tmp_path).save({"entries": []})  # type: ignore[arg-type]


# --- unknown future schema ---------------------------------------------------


def _future_document() -> dict[str, object]:
    return {
        "schemaVersion": OCR_RESOURCE_STORE_SCHEMA_VERSION + 1,
        "entries": [],
        "pins": [],
        "quarantine": ["something this build knows nothing about"],
    }


def test_unknown_future_schema_is_refused_on_load(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _write_document(store, _future_document())
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        store.load()
    assert store.state_path.read_bytes() == payload


def test_unknown_future_schema_is_not_overwritten_on_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _write_document(store, _future_document())
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        store.save(OcrResourceStoreState(entries=(_identity(),)))
    assert store.state_path.read_bytes() == payload
    assert [path.name for path in store.root.iterdir()] == [
        OCR_RESOURCE_STORE_STATE_FILENAME
    ]


def test_version_is_checked_before_the_rest_of_the_document(tmp_path: Path) -> None:
    """A future document's unknown keys must not be reported as malformed."""
    store = _store(tmp_path)
    _write_document(store, {"schemaVersion": 99, "whatever": True})
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        store.load()


def test_past_schema_version_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_document(store, {"schemaVersion": 0, "entries": [], "pins": []})
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        store.load()


# --- malformed documents -----------------------------------------------------


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ({"entries": [], "pins": []}, "schemaVersion must be an integer"),
        ({"schemaVersion": "1", "entries": [], "pins": []}, "must be an integer"),
        ({"schemaVersion": True, "entries": [], "pins": []}, "must be an integer"),
        ({"schemaVersion": 1.0, "entries": [], "pins": []}, "must be an integer"),
        ({"schemaVersion": 1, "pins": []}, "missing keys: entries"),
        ({"schemaVersion": 1, "entries": []}, "missing keys: pins"),
        (
            {"schemaVersion": 1, "entries": [], "pins": [], "extra": 1},
            "unknown keys: extra",
        ),
        ({"schemaVersion": 1, "entries": {}, "pins": []}, "entries must be a JSON array"),
        ({"schemaVersion": 1, "entries": [], "pins": {}}, "pins must be a JSON array"),
        ({"schemaVersion": 1, "entries": ["eng"], "pins": []}, "entry 0 must be a JSON"),
    ],
)
def test_malformed_document_is_refused(
    tmp_path: Path, document: object, match: str
) -> None:
    store = _store(tmp_path)
    _write_document(store, document)
    with pytest.raises(OcrResourceStoreError, match=match):
        store.load()


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"3", b"null", b"\xff\xfe"])
def test_unparseable_state_is_refused(tmp_path: Path, raw: bytes) -> None:
    store = _store(tmp_path)
    store.root.mkdir(parents=True)
    store.state_path.write_bytes(raw)
    with pytest.raises(OcrResourceStoreError):
        store.load()


def _entry_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "language": "eng",
        "engineVersion": "5.5.3",
        "modelVersion": "tessdata_fast-4.1.0",
        "byteSize": 4 * 1024 * 1024,
        "sha256": DIGEST,
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize(
    "entry",
    [
        _entry_document(language="eng deu"),
        _entry_document(language=""),
        _entry_document(byteSize=0),
        _entry_document(byteSize="4"),
        _entry_document(sha256="A" * 64),
        {**_entry_document(), "extra": 1},
        {key: value for key, value in _entry_document().items() if key != "sha256"},
        {**_entry_document(), "engine_version": "5.5.3"},
    ],
)
def test_malformed_entry_is_refused(tmp_path: Path, entry: object) -> None:
    store = _store(tmp_path)
    _write_document(store, {"schemaVersion": 1, "entries": [entry], "pins": []})
    with pytest.raises(OcrResourceStoreError, match="entry 0"):
        store.load()


@pytest.mark.parametrize(
    "pin",
    [
        {"operationId": "op 1", "identity": _entry_document()},
        {"operationId": "", "identity": _entry_document()},
        {"operationId": 1, "identity": _entry_document()},
        {"operationId": "op-1", "identity": "eng"},
        {"operationId": "op-1", "identity": _entry_document(sha256="nope")},
        {"operationId": "op-1"},
        {"operationId": "op-1", "identity": _entry_document(), "extra": 1},
    ],
)
def test_malformed_pin_is_refused(tmp_path: Path, pin: object) -> None:
    store = _store(tmp_path)
    _write_document(store, {"schemaVersion": 1, "entries": [], "pins": [pin]})
    with pytest.raises(OcrResourceStoreError, match="pin 0"):
        store.load()


def test_duplicate_stored_entries_are_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_document(
        store,
        {
            "schemaVersion": 1,
            "entries": [_entry_document(), _entry_document()],
            "pins": [],
        },
    )
    with pytest.raises(OcrResourceStoreError, match="duplicate entry"):
        store.load()


def test_duplicate_stored_pins_are_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pin = {"operationId": "op-1", "identity": _entry_document()}
    _write_document(store, {"schemaVersion": 1, "entries": [], "pins": [pin, dict(pin)]})
    with pytest.raises(OcrResourceStoreError, match="duplicate pin"):
        store.load()


# --- atomic replacement ------------------------------------------------------


def test_failed_replace_leaves_the_previous_document_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    before = store.state_path.read_bytes()

    def _refuse(*args: object, **kwargs: object) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(os, "replace", _refuse)
    with pytest.raises(OcrResourceStoreError, match="could not be written"):
        store.save(OcrResourceStoreState(entries=(_identity(language="deu"),)))

    assert store.state_path.read_bytes() == before
    assert [path.name for path in store.root.iterdir()] == [
        OCR_RESOURCE_STORE_STATE_FILENAME
    ]
    monkeypatch.undo()
    assert store.load() == OcrResourceStoreState(entries=(_identity(),))


def test_failed_write_leaves_the_previous_document_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    before = store.state_path.read_bytes()

    monkeypatch.setattr(os, "write", lambda *args: 0)
    with pytest.raises(OcrResourceStoreError, match="could not be written"):
        store.save(OcrResourceStoreState(entries=(_identity(language="deu"),)))
    monkeypatch.undo()

    assert store.state_path.read_bytes() == before
    assert [path.name for path in store.root.iterdir()] == [
        OCR_RESOURCE_STORE_STATE_FILENAME
    ]


def test_failed_fsync_leaves_the_previous_document_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    before = store.state_path.read_bytes()

    def _refuse(descriptor: int) -> None:
        raise OSError("fsync refused")

    monkeypatch.setattr(os, "fsync", _refuse)
    with pytest.raises(OcrResourceStoreError, match="could not be written"):
        store.save(OcrResourceStoreState(entries=(_identity(language="deu"),)))
    monkeypatch.undo()

    assert store.state_path.read_bytes() == before
    assert [path.name for path in store.root.iterdir()] == [
        OCR_RESOURCE_STORE_STATE_FILENAME
    ]


def test_failed_directory_fsync_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename has already happened, so the new document stays; the loss of
    durability is still the caller's to hear about."""
    store = _store(tmp_path)

    def _refuse(directory: Path) -> None:
        raise OSError("directory fsync refused")

    monkeypatch.setattr(store_module, "fsync_directory", _refuse)
    state = OcrResourceStoreState(entries=(_identity(),))
    with pytest.raises(OcrResourceStoreError, match="could not be made durable"):
        store.save(state)
    monkeypatch.undo()

    assert store.load() == state
    assert [path.name for path in store.root.iterdir()] == [
        OCR_RESOURCE_STORE_STATE_FILENAME
    ]


def test_a_short_write_still_stores_the_whole_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.write`` may accept less than it was handed; the rest must follow."""
    reference = OcrResourceStore(tmp_path / "reference")
    state = OcrResourceStoreState(entries=(_identity(), _identity(language="deu")))
    reference.save(state)

    store = _store(tmp_path)
    real_write = os.write
    calls = 0

    def _one_byte(descriptor: int, payload: bytes) -> int:
        nonlocal calls
        calls += 1
        return real_write(descriptor, payload[:1])

    monkeypatch.setattr(os, "write", _one_byte)
    store.save(state)
    monkeypatch.undo()

    assert calls > 1
    assert store.state_path.read_bytes() == reference.state_path.read_bytes()
    assert store.load() == state


# --- the temporary document ---------------------------------------------------


def _victim(tmp_path: Path) -> Path:
    path = tmp_path / "unrelated.txt"
    path.write_bytes(b"not ours to truncate")
    return path


def test_a_planted_legacy_temporary_symlink_is_not_followed(tmp_path: Path) -> None:
    """The old build's predictable ``.state.json.tmp`` is just another name now:
    nothing opens it, so a symlink waiting there truncates nothing."""
    store = _store(tmp_path)
    store.root.mkdir()
    victim = _victim(tmp_path)
    planted = store.root / f".{OCR_RESOURCE_STORE_STATE_FILENAME}.tmp"
    planted.symlink_to(victim)

    state = OcrResourceStoreState(entries=(_identity(),))
    store.save(state)

    assert store.load() == state
    assert victim.read_bytes() == b"not ours to truncate"
    assert planted.is_symlink()


def test_a_taken_candidate_name_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name already in use is retried, not written through -- even when what
    holds it is a symlink pointing somewhere else entirely."""
    store = _store(tmp_path)
    store.root.mkdir()
    victim = _victim(tmp_path)
    taken = ".state.json.taken.tmp"
    (store.root / taken).symlink_to(victim)

    names = iter([taken])
    monkeypatch.setattr(
        store_module,
        "_temporary_name",
        lambda: next(names, ".state.json.fresh.tmp"),
    )
    state = OcrResourceStoreState(entries=(_identity(),))
    store.save(state)
    monkeypatch.undo()

    assert store.load() == state
    assert victim.read_bytes() == b"not ours to truncate"
    assert (store.root / taken).is_symlink()
    assert sorted(path.name for path in store.root.iterdir()) == sorted(
        (OCR_RESOURCE_STORE_STATE_FILENAME, taken)
    )


def test_every_candidate_taken_refuses_and_keeps_the_previous_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    before = store.state_path.read_bytes()
    victim = _victim(tmp_path)
    taken = ".state.json.taken.tmp"
    (store.root / taken).symlink_to(victim)

    monkeypatch.setattr(store_module, "_temporary_name", lambda: taken)
    with pytest.raises(OcrResourceStoreError, match="could not be written"):
        store.save(OcrResourceStoreState(entries=(_identity(language="deu"),)))
    monkeypatch.undo()

    assert store.state_path.read_bytes() == before
    assert victim.read_bytes() == b"not ours to truncate"
    assert (store.root / taken).is_symlink()


def test_each_save_uses_a_fresh_unguessable_temporary_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.root.mkdir()
    names = {store_module._temporary_name() for _ in range(8)}
    assert len(names) == 8
    for name in names:
        assert name.startswith(f".{OCR_RESOURCE_STORE_STATE_FILENAME}.")
        assert name.endswith(".tmp")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_temporary_document_is_owner_only_before_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    modes: list[int] = []
    real_write = os.write

    def _record(descriptor: int, payload: bytes) -> int:
        modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return real_write(descriptor, payload)

    monkeypatch.setattr(os, "write", _record)
    store.save(OcrResourceStoreState(entries=(_identity(),)))
    monkeypatch.undo()

    assert modes == [0o600]
