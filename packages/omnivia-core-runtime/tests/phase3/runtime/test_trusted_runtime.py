"""Unit properties of the runtime payload verifier that no fixture can carry.

The cross-language conformance corpus lives in ``contracts/runtime/v1/fixtures`` and
is run by ``tests/runtime_contract/test_runtime_conformance.py``. A case there describes an
installation tree, so it can express every refusal that is a property of a *tree* --
and none that are properties of the *host* or of the module's own shape. Those are
here: the bounded reads, the IO failures, the canonical subset's edges, the absence
of ambient discovery, and what a hardened installation actually looks like on disk.
"""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _signed_payload import NOW, OTHER_KEY_ID, TEST_KEY_ID, anchor, write_payload
from omnivia_core_runtime.distribution import trusted_runtime
from omnivia_core_runtime.distribution.trusted_runtime import (
    IDENTITY_DOMAIN,
    MAX_MANIFEST_BYTES,
    RUNTIME_MANIFEST_NAME,
    SIGNATURE_DOMAIN,
    CanonicalJsonError,
    RuntimeRefusal,
    RuntimeResolutionError,
    canonical_json,
    harden_payload,
    parse_canonical_document,
    parse_manifest,
    payload_identity,
    resolve_runtime,
    signed_manifest_bytes,
    unharden_tree,
    verify_payload,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX mode and owner policy")


def _verify(root: Path, **overrides: object) -> object:
    arguments: dict[str, object] = {
        "trust_anchors": [anchor()],
        "verification_time": NOW,
    }
    arguments.update(overrides)
    return verify_payload(root, **arguments)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------


def test_object_names_sort_by_utf8_bytes_and_not_by_python_default() -> None:
    """A UTF-16 language sorts these differently unless the rule names the encoding.

    ``"\\uff1a"`` (fullwidth colon) is above ``"\\uffff"`` in code-point order and in
    UTF-8 byte order alike, but a naive UTF-16 code-unit sort puts a surrogate pair
    below both. Stating the encoding is what makes one order.
    """
    assert canonical_json({"b": 1, "a": 2, "C": 3}) == b'{"C":3,"a":2,"b":1}'
    assert canonical_json({"￿": 1, "：": 2}) == b'{"\\uff1a":2,"\\uffff":1}'


def test_the_canonical_subset_admits_no_float_and_no_foreign_type() -> None:
    for value in (1.5, 1.0, float("nan")):
        with pytest.raises(CanonicalJsonError):
            canonical_json({"a": value})
    with pytest.raises(CanonicalJsonError):
        canonical_json({"a": {1, 2}})
    with pytest.raises(CanonicalJsonError):
        canonical_json({1: "a"})


def test_a_boolean_never_canonicalises_as_its_integer_value() -> None:
    """`bool` is an `int` subclass, so the order of the type tests is load-bearing:
    `True` emitted as `1` would give two distinct documents one identity."""
    assert canonical_json({"a": True, "b": 1}) == b'{"a":true,"b":1}'


def test_a_parsed_document_is_bounded_utf8_and_free_of_duplicate_names() -> None:
    assert parse_canonical_document(b'{"a":1}', limit=16) == {"a": 1}
    with pytest.raises(CanonicalJsonError):
        parse_canonical_document(b'{"a":1}', limit=3)
    with pytest.raises(CanonicalJsonError):
        parse_canonical_document(b'{"a":1,"a":2}', limit=64)
    with pytest.raises(CanonicalJsonError):
        parse_canonical_document(b'{"a":1.5}', limit=64)
    with pytest.raises(CanonicalJsonError):
        parse_canonical_document(b'{"a":NaN}', limit=64)
    with pytest.raises(CanonicalJsonError):
        parse_canonical_document(b'{"a":"\xff"}', limit=64)
    with pytest.raises(CanonicalJsonError):
        parse_canonical_document(b"[1,2]", limit=64)


#: Documents `json` refuses with an exception that is not this module's: an integer
#: above the interpreter's 4300-digit string-conversion limit (`ValueError`), nesting
#: deeper than the stack (`RecursionError`) and a plain syntax error
#: (`json.JSONDecodeError`). Every one of them fits inside `MAX_MANIFEST_BYTES`, so
#: the byte bound never reached them.
_UNREADABLE_DOCUMENTS = (
    b'{"a":' + b"1" * 4400 + b"}",
    b'{"a":' + b"[" * 60_000 + b"]" * 60_000 + b"}",
    b'{"a":',
)


def test_a_document_json_cannot_read_refuses_and_never_raises_through() -> None:
    """The parse's own failures are answers here, not exceptions the caller sees."""
    for raw in _UNREADABLE_DOCUMENTS:
        assert len(raw) <= MAX_MANIFEST_BYTES
        with pytest.raises(CanonicalJsonError):
            parse_canonical_document(raw, limit=MAX_MANIFEST_BYTES)


def test_an_unreadable_manifest_reaches_the_closed_refusal_vocabulary(
    tmp_path: Path,
) -> None:
    """And end to end: each one used to leave `verify_payload` as itself.

    A `RecursionError` out of a resolver is not one of the eight codes a consumer
    branches on, so it was an unhandled crash on the launch path rather than
    `runtime_metadata_invalid` and a repair.
    """
    payload = tmp_path / "payload"
    write_payload(payload)
    for raw in _UNREADABLE_DOCUMENTS:
        (payload / RUNTIME_MANIFEST_NAME).write_bytes(raw)
        with pytest.raises(RuntimeResolutionError) as raised:
            _verify(payload, enforce_installation_policy=False)
        assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID


def test_identity_excludes_itself_and_is_separated_from_the_signature_domain(
    tmp_path: Path,
) -> None:
    """The two digests over one manifest must never be interchangeable."""
    write_payload(tmp_path / "payload")
    manifest = json.loads((tmp_path / "payload" / RUNTIME_MANIFEST_NAME).read_text())
    identity = payload_identity(manifest)

    assert manifest["payload_identity"] == identity
    # Declaring a different identity cannot change the computed one.
    assert payload_identity({**manifest, "payload_identity": "sha256:" + "0" * 64}) == identity
    assert signed_manifest_bytes(manifest, identity).startswith(SIGNATURE_DOMAIN.encode())
    assert IDENTITY_DOMAIN.encode() not in signed_manifest_bytes(manifest, identity)


# --------------------------------------------------------------------------
# Bounds and IO
# --------------------------------------------------------------------------


def test_an_oversized_manifest_is_refused_rather_than_read(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_payload(payload)
    (payload / RUNTIME_MANIFEST_NAME).write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(RuntimeResolutionError) as raised:
        _verify(payload)
    assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID


@POSIX_ONLY
def test_an_unreadable_payload_file_is_a_bounded_io_failure(tmp_path: Path) -> None:
    """The one refusal no conformance case can express: the tree is correct and the
    host refused the read. A bounded retry, not a tamper report."""
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file, so the failure is unreachable")
    payload = tmp_path / "payload"
    write_payload(payload)
    (payload / "lib" / "omnivia" / "release.txt").chmod(0o000)
    try:
        with pytest.raises(RuntimeResolutionError) as raised:
            _verify(payload, enforce_installation_policy=False)
    finally:
        (payload / "lib" / "omnivia" / "release.txt").chmod(0o600)
    assert raised.value.refusal is RuntimeRefusal.IO_FAILURE


def test_a_file_that_grew_since_the_manifest_was_written_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_payload(payload)
    member = payload / "lib" / "omnivia" / "release.txt"
    member.write_bytes(member.read_bytes() + b"appended\n")
    with pytest.raises(RuntimeResolutionError) as raised:
        _verify(payload, enforce_installation_policy=False)
    assert raised.value.refusal is RuntimeRefusal.TAMPERED


def test_an_inventory_naming_an_escaping_path_is_refused_before_any_read() -> None:
    manifest = {
        "manifest_version": "1.0",
        "release_version": "0.6.5",
        "platform": {"os": "linux", "arch": "arm64"},
        "executables": {
            "cli": {"path": "bin/omnivia", "sha256": "0" * 64},
            "service": {"path": "bin/omnivia-core-service", "sha256": "0" * 64},
        },
        "inventory": [
            {"path": "../escape", "sha256": "0" * 64, "size": 1, "executable": False},
            {"path": "bin/omnivia", "sha256": "0" * 64, "size": 1, "executable": True},
        ],
        "compatibility": {
            "minimum_bootstrap_contract": "1.0",
            "maximum_bootstrap_contract": "1.0",
        },
        "signing": {"key_id": "k", "algorithm": "ed25519"},
        "payload_identity": "sha256:" + "0" * 64,
    }
    with pytest.raises(RuntimeResolutionError) as raised:
        parse_manifest(manifest)
    assert raised.value.refusal is RuntimeRefusal.LAYOUT_INVALID


def test_an_unsorted_inventory_is_refused_so_the_identity_stays_reproducible(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload"
    write_payload(payload)
    manifest = json.loads((payload / RUNTIME_MANIFEST_NAME).read_text())
    manifest["inventory"].reverse()
    with pytest.raises(RuntimeResolutionError) as raised:
        parse_manifest(manifest)
    assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID


# --------------------------------------------------------------------------
# No ambient discovery
# --------------------------------------------------------------------------


def test_the_verifier_reaches_for_no_path_no_home_and_no_network() -> None:
    """Security invariant 4, held over the source rather than over one call.

    A resolver that consulted `PATH` on some branch would pass every behavioural test
    in this file, because none of them has a `PATH` worth consulting. What is checked
    instead is that the module imports nothing that could look, and names no
    environment lookup at all.
    """
    source = Path(trusted_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".", 1)[0])
    assert imported.isdisjoint(
        {"shutil", "subprocess", "socket", "urllib", "http", "requests", "glob"}
    ), sorted(imported)

    attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    assert attributes.isdisjoint({"environ", "getenv", "expanduser", "home", "which"})
    assert "rglob" not in {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def test_a_descriptor_publishes_the_pair_and_not_the_payload_layout(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_payload(payload)
    verified = _verify(payload, enforce_installation_policy=False)
    document = verified.to_dict()  # type: ignore[attr-defined]
    assert set(document) == {
        "runtime_descriptor_version",
        "release_version",
        "payload_identity",
        "runtime_root",
        "cli_path",
        "service_path",
    }
    assert "inventory" not in json.dumps(document)
    assert document["cli_path"].startswith(document["runtime_root"])
    assert document["service_path"].startswith(document["runtime_root"])


# --------------------------------------------------------------------------
# Host compatibility
# --------------------------------------------------------------------------


def test_the_host_architecture_is_normalised_to_the_manifest_spelling() -> None:
    """`uname` says `aarch64` where the manifest says `arm64`, and Windows reports
    `AMD64` where it says `x86_64`. An unnormalised value equals neither, so on those
    hosts *every* authentic payload would refuse as incompatible -- which is the
    failure mode a derived constant like this one has, and no payload can show it."""
    assert trusted_runtime.HOST_ARCHITECTURE in trusted_runtime.PAYLOAD_ARCHITECTURES


def test_a_payload_built_for_the_other_architecture_is_incompatible(tmp_path: Path) -> None:
    """Authentic, correctly signed, and unrunnable here.

    `platform.arch` was declared in the manifest, parsed, and then compared with
    nothing, so this payload verified all the way to a descriptor and handed the
    launcher a pair the host cannot execute. It is a compatibility refusal and not a
    trust one: nothing is wrong with the payload, it is for another machine.
    """
    payload = tmp_path / "payload"
    write_payload(payload, architecture="x86_64")

    with pytest.raises(RuntimeResolutionError) as raised:
        _verify(payload, enforce_installation_policy=False, host_architecture="arm64")
    assert raised.value.refusal is RuntimeRefusal.INCOMPATIBLE

    # The architecture and nothing else: against the one it declares, it verifies.
    verified = _verify(payload, enforce_installation_policy=False, host_architecture="x86_64")
    assert verified.release_version == "0.6.5"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Trust anchors and rotation
# --------------------------------------------------------------------------


def test_a_key_is_usable_only_inside_its_window_and_before_its_retirement() -> None:
    key = anchor(
        not_before=datetime(2026, 1, 1, tzinfo=UTC),
        not_after=datetime(2027, 1, 1, tzinfo=UTC),
        retired_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert key.usable_at(datetime(2026, 3, 1, tzinfo=UTC))
    assert not key.usable_at(datetime(2025, 12, 31, tzinfo=UTC))
    assert not key.usable_at(datetime(2026, 6, 1, tzinfo=UTC))
    assert not key.usable_at(datetime(2027, 1, 1, tzinfo=UTC))


def test_two_anchors_sharing_a_key_id_are_a_caller_defect_not_a_refusal(
    tmp_path: Path,
) -> None:
    """A refusal code would tell a consumer the *release* was wrong. It was not."""
    payload = tmp_path / "payload"
    write_payload(payload)
    with pytest.raises(ValueError, match="share the key id"):
        _verify(payload, trust_anchors=[anchor(), anchor()])


def test_a_naive_verification_time_is_refused_rather_than_assumed_utc(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_payload(payload)
    with pytest.raises(ValueError, match="timezone-aware"):
        # Naive on purpose: the refusal under test is that this is not accepted.
        _verify(payload, verification_time=datetime(2026, 9, 10, 12, 0))  # noqa: DTZ001


def test_a_trust_anchor_document_round_trips_and_refuses_a_widened_shape() -> None:
    key = anchor()
    document = {
        "key_id": key.key_id,
        "algorithm": "ed25519",
        "public_key": __import__("base64").b64encode(key.public_key).decode("ascii"),
        "not_before": "2026-01-01T00:00:00Z",
        "not_after": "2027-01-01T00:00:00Z",
        "retired_at": None,
    }
    assert trusted_runtime.TrustAnchor.from_document(document) == key
    with pytest.raises(ValueError):
        trusted_runtime.TrustAnchor.from_document({**document, "trust_everything": True})


# --------------------------------------------------------------------------
# Hardening
# --------------------------------------------------------------------------


@POSIX_ONLY
def test_hardening_takes_the_executable_bit_from_the_signed_inventory(
    tmp_path: Path,
) -> None:
    """A source tree with everything at 0755 must not harden into a payload of
    executables: the pair would stop being a pair, and the extra ones are commands
    this contract never admitted."""
    payload = tmp_path / "payload"
    write_payload(payload)
    for path in payload.rglob("*"):
        if path.is_file():
            path.chmod(0o755)

    verified = _verify(payload, enforce_installation_policy=False)
    harden_payload(payload, verified.inventory)  # type: ignore[attr-defined]
    try:
        modes = {
            path.relative_to(payload).as_posix(): path.stat().st_mode & 0o777
            for path in sorted(payload.rglob("*"))
        }
        assert modes["lib/omnivia/release.txt"] == 0o400
        assert modes[RUNTIME_MANIFEST_NAME] == 0o400
        assert modes["lib"] == 0o500
        executables = {
            name for name, mode in modes.items() if mode & 0o111 and not (payload / name).is_dir()
        }
        assert executables == {
            entry.path.as_posix()
            for entry in verified.inventory  # type: ignore[attr-defined]
            if entry.executable
        }
        # And a hardened payload verifies under the full installation policy.
        _verify(payload)
    finally:
        unharden_tree(payload)


@POSIX_ONLY
def test_a_hardened_payload_admits_no_new_entry(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_payload(payload)
    verified = _verify(payload, enforce_installation_policy=False)
    harden_payload(payload, verified.inventory)  # type: ignore[attr-defined]
    try:
        if os.geteuid() == 0:
            pytest.skip("root writes into a mode-500 directory")
        with pytest.raises(OSError):
            (payload / "lib" / "omnivia" / "injected").write_bytes(b"x")
    finally:
        unharden_tree(payload)


@POSIX_ONLY
def test_a_payload_owned_by_another_user_is_refused_however_well_signed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is half the installation policy, and the half no fixture can carry.

    Every mode case in the conformance corpus is expressible as a tree, so it lives
    there. Ownership is not: a case document cannot `chown`, and a test that could
    would need a second uid on the host and root to hand a file to it. What can be
    moved instead is the *other* side of the comparison -- who "the effective user"
    is -- which drives the identical branch and needs no privilege at all.

    It matters because the mode policy alone says nothing about a payload that
    somebody else installed and still owns. That payload can be perfectly signed,
    fully intact and mode 0500, and every write bit in the world can be off while
    its owner remains free to `chmod` it back before the next launch.
    """
    payload = tmp_path / "payload"
    write_payload(payload)
    verified = _verify(payload, enforce_installation_policy=False)
    harden_payload(payload, verified.inventory)  # type: ignore[attr-defined]
    try:
        # A hardened payload is otherwise acceptable; the *only* thing changed below
        # is whose it is.
        _verify(payload)
        # `trusted_runtime.os` *is* the `os` module, so the replacement cannot call
        # through to the real `geteuid`; the answer is read once, first.
        somebody_else = os.geteuid() + 1
        monkeypatch.setattr(trusted_runtime.os, "geteuid", lambda: somebody_else)
        with pytest.raises(RuntimeResolutionError) as raised:
            _verify(payload)
    finally:
        unharden_tree(payload)
    assert raised.value.refusal is RuntimeRefusal.LAYOUT_INVALID


def test_resolution_needs_an_absolute_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_runtime(
            installation_root=Path("relative"),
            trust_anchors=[anchor()],
            verification_time=NOW,
        )


def _refusal_for(root: Path) -> RuntimeRefusal:
    with pytest.raises(RuntimeResolutionError) as raised:
        resolve_runtime(
            installation_root=root,
            trust_anchors=[anchor()],
            verification_time=NOW,
        )
    return raised.value.refusal


def test_an_absent_installation_root_is_not_installed_rather_than_an_io_failure(
    tmp_path: Path,
) -> None:
    """The first-run answer, and the one the root's own `lstat` used to swallow.

    Nothing installed is the *normal* state before a first install, and it has a code
    of its own that tells a consumer to install. `runtime_io_failure` told it to
    retry a bounded read instead -- a retry that can only keep failing, because the
    directory it names is not going to appear on its own.

    A root that exists and is not a plain directory keeps refusing at the layout
    boundary: "not installed" must not become the answer for a root somebody
    replaced with a file or a link out of the installation.
    """
    assert _refusal_for(tmp_path / "absent") is RuntimeRefusal.NOT_INSTALLED

    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"")
    assert _refusal_for(a_file) is RuntimeRefusal.LAYOUT_INVALID


@POSIX_ONLY
def test_an_installation_root_the_host_will_not_stat_is_still_an_io_failure(
    tmp_path: Path,
) -> None:
    """The other half: a genuine local read failure keeps its own code."""
    if os.geteuid() == 0:
        pytest.skip("root traverses a mode-000 directory, so the failure is unreachable")
    closed = tmp_path / "closed"
    (closed / "installation").mkdir(parents=True)
    closed.chmod(0o000)
    try:
        assert _refusal_for(closed / "installation") is RuntimeRefusal.IO_FAILURE
    finally:
        closed.chmod(0o700)


def test_an_unapproved_key_never_reaches_the_digest_stage(tmp_path: Path) -> None:
    """Order matters: a payload signed by nobody approved must be refused as untrusted
    even when every digest in it is correct."""
    payload = tmp_path / "payload"
    write_payload(payload, key_id=TEST_KEY_ID, sign_with=OTHER_KEY_ID)
    with pytest.raises(RuntimeResolutionError) as raised:
        _verify(payload, enforce_installation_policy=False)
    assert raised.value.refusal is RuntimeRefusal.UNTRUSTED
