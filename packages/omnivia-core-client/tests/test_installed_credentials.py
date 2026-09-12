"""The protected installed-credential store: private, atomic, fresh, and silent.

Four properties, and every test below is one of them:

* **private** -- the file is owner-only from creation, and a file that is not is
  never read whatever else is true of it;
* **atomic** -- a replacement is a rename, so a concurrent reader sees one whole
  credential or the other and never a fragment;
* **fresh** -- nothing is cached, so a rotation or a revocation lands on the next
  call rather than at the next restart;
* **silent** -- no refusal, repr, str, argument tuple or exception chain carries
  the material, the reference, or the path.

The protected *configuration* store is the same four properties over the same
walk, and its section at the foot of this file asserts them again rather than
trusting that sharing the walk made them true. Every adversarial case there runs
twice: once over the descriptor-anchored form this host uses, and once with
``_ANCHORED`` forced off, which is the pathname form Windows takes. That second
pass is not a Windows test -- the native owner-and-DACL call cannot run here --
it is the decision logic of the pathname form: what it proves, what it refuses to
create, and what it refuses to unlink.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
from omnivia_core_client import (
    CONFIGURATION_HOSTS,
    CONFIGURATION_STORE_DIRECTORY,
    MAXIMUM_CONFIGURATION_BYTES,
    STORE_DIRECTORY,
    Credential,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialReference,
    CredentialUnavailableError,
    InstalledConfigStore,
    InstalledCredentialStore,
    installed_credentials,
)

REFERENCE = CredentialReference("omcp-0123456789abcdef")
OTHER = CredentialReference("omcp-fedcba9876543210")
SECRET = "omcp_live_9f3a2b1c4d5e6f708192a3b4c5d6e7f8"
ROTATED = "omcp_live_0000111122223333444455556666777"


def store(root: Path) -> InstalledCredentialStore:
    return InstalledCredentialStore(root)


def directory(root: Path) -> Path:
    return root.joinpath(*STORE_DIRECTORY)


def stored_file(root: Path) -> Path:
    """The one file in the store, found by looking rather than by being told."""
    files = sorted(path for path in directory(root).iterdir() if path.is_file())
    assert len(files) == 1, files
    return files[0]


def rendered(error: BaseException) -> str:
    """Everything a caller, a logger or a traceback renderer can reach."""
    return " ".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(error.__cause__),
            repr(error.__context__),
        )
    )


# --- private ---------------------------------------------------------------


def test_a_stored_credential_comes_back_exactly(tmp_path: Path) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    assert keeper.resolve(REFERENCE).reveal() == SECRET


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
def test_the_file_and_its_directory_are_owner_only_from_creation(
    tmp_path: Path,
) -> None:
    store(tmp_path).store(REFERENCE, Credential(SECRET))
    assert stat.S_IMODE(directory(tmp_path).stat().st_mode) == 0o700
    assert stat.S_IMODE(stored_file(tmp_path).stat().st_mode) == 0o600


def test_the_location_is_fixed_and_derived_rather_than_chosen(tmp_path: Path) -> None:
    """The caller names an installation root and nothing below it.

    The filename is a digest of the reference, so it is neither the reference
    itself -- which could spell a reserved Windows device name -- nor anything an
    attacker could predict a *path* from without already knowing the root.
    """
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    keeper.store(OTHER, Credential(ROTATED))
    held = sorted(path.name for path in directory(tmp_path).iterdir())
    assert len(held) == 2 and len(set(held)) == 2
    for name in held:
        assert name.endswith(".credential")
        assert REFERENCE.value not in name and OTHER.value not in name
    assert directory(tmp_path).parent == tmp_path / STORE_DIRECTORY[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666, 0o660])
def test_a_group_or_world_readable_file_is_not_a_credential(
    tmp_path: Path, mode: int
) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    stored_file(tmp_path).chmod(mode)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)
    assert keeper.health(REFERENCE) == "unusable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
def test_a_group_or_world_reachable_directory_is_not_a_store(tmp_path: Path) -> None:
    """Checked one level up, because a readable directory is a listable one."""
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    directory(tmp_path).chmod(0o755)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)
    directory(tmp_path).chmod(0o700)
    assert keeper.resolve(REFERENCE).reveal() == SECRET


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership uses the effective uid")
def test_a_file_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    mine = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: mine + 1)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)


# --- path substitution -----------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_credential_file_is_refused(tmp_path: Path) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    target = stored_file(tmp_path)
    elsewhere = tmp_path / "attacker.txt"
    elsewhere.write_text(ROTATED, encoding="ascii")
    elsewhere.chmod(0o600)
    target.unlink()
    target.symlink_to(elsewhere)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_store_directory_is_refused(tmp_path: Path) -> None:
    """The directory is proved as well as the file, or the file proves nothing.

    A substituted directory can hold a file this process owns with owner-only
    permission -- the attacker only has to have been given one -- so a proof that
    stops at the file would accept a credential from a store nobody installed.
    """
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    real = directory(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    for path in real.iterdir():
        path.rename(elsewhere / path.name)
    real.rmdir()
    real.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)
    assert keeper.health(REFERENCE) == "unusable"


# --- the walk itself: a swapped parent may not reach outside the store -----
#
# Every test below builds the attacker's directory *as a working store* -- the
# same digest filename, a different credential in it, owner-only -- so that an
# implementation resolving `installation_state / "runtime" /
# ".installed-credentials" / leaf` as a pathname would succeed and return the
# attacker's bytes, or write this installation's bearer into the attacker's
# directory. Refusing is the whole assertion; the second half of each is that
# nothing outside the real store was created, read, overwritten or deleted.

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")


def leaf_name(root: Path) -> str:
    """The digest filename the store gives `REFERENCE`, found by storing one."""
    keeper = store(root)
    keeper.store(REFERENCE, Credential(SECRET))
    name = stored_file(root).name
    keeper.remove(REFERENCE)
    return name


def decoy(root: Path, name: str, secret: str = ROTATED) -> Path:
    """A whole installation the attacker owns, and the credential planted in it.

    Laid out exactly as this store lays one out -- ``.installed-credentials``
    under the returned file's grandparent -- so that either half of the layout can
    be substituted: the store directory, or the ``runtime/`` above it.
    """
    elsewhere = root / "elsewhere"
    planted = elsewhere / STORE_DIRECTORY[1] / name
    planted.parent.mkdir(mode=0o700, parents=True)
    elsewhere.chmod(0o700)
    planted.write_text(secret, encoding="ascii")
    planted.chmod(0o600)
    return planted


@POSIX_ONLY
def test_a_symlinked_runtime_directory_creates_nothing_on_the_other_side(
    tmp_path: Path,
) -> None:
    """The component above the store is walked no-follow too, or it is a way in.

    ``runtime/`` is the one part of the layout this store will create, so a
    symlink standing there would otherwise be followed and the store directory --
    holding this installation's bearer -- made wherever it pointed.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / STORE_DIRECTORY[0]).symlink_to(elsewhere, target_is_directory=True)
    keeper = store(tmp_path)

    with pytest.raises(CredentialUnavailableError):
        keeper.store(REFERENCE, Credential(SECRET))

    assert list(elsewhere.rglob("*")) == []


@POSIX_ONLY
def test_a_symlinked_store_directory_reads_writes_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    """A substituted store directory is refused by all four operations, not three.

    The read was already proved elsewhere; what this adds is that the *writing*
    operations refuse as well. A store that only checked on the way in would let
    a revoke unlink, and a rotation overwrite, a file in a directory somebody else
    chose.
    """
    name = leaf_name(tmp_path)
    planted = decoy(tmp_path, name)
    real = directory(tmp_path)
    real.rmdir()
    real.symlink_to(planted.parent, target_is_directory=True)
    keeper = store(tmp_path)

    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)
    with pytest.raises(CredentialUnavailableError):
        keeper.store(REFERENCE, Credential(SECRET))
    with pytest.raises(CredentialUnavailableError):
        keeper.remove(REFERENCE)

    assert keeper.health(REFERENCE) == "unusable"
    assert planted.read_text(encoding="ascii") == ROTATED
    assert sorted(path.name for path in planted.parent.iterdir()) == [name]


@POSIX_ONLY
def test_a_symlinked_credential_file_is_replaced_rather_than_written_through(
    tmp_path: Path,
) -> None:
    """The leaf is the last name an attacker controls, and it is never followed.

    A rotation writes a fresh private file and renames it *over* the link, so the
    link is what goes and the file it pointed at is untouched -- rather than the
    bearer being written into a file somebody else can read. A revoke removes the
    link for the same reason: ``unlink`` acts on the name, never on the target.
    """
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    outside = tmp_path / "attacker.txt"
    outside.write_text(ROTATED, encoding="ascii")
    outside.chmod(0o600)

    linked = stored_file(tmp_path)
    linked.unlink()
    linked.symlink_to(outside)
    keeper.store(REFERENCE, Credential(SECRET))

    assert outside.read_text(encoding="ascii") == ROTATED
    replaced = stored_file(tmp_path)
    assert not replaced.is_symlink()
    assert keeper.resolve(REFERENCE).reveal() == SECRET

    replaced.unlink()
    replaced.symlink_to(outside)
    keeper.remove(REFERENCE)

    assert outside.read_text(encoding="ascii") == ROTATED
    assert not replaced.is_symlink() and not replaced.exists()


def swap_the_parent(root: Path, planted: Path) -> None:
    """Move the real ``runtime/`` aside and put a symlink to `planted` in its place.

    What an attacker who wins a race gets to do: the pathname the operation began
    with now resolves somewhere else. The descriptors already held do not.
    """
    runtime = root / STORE_DIRECTORY[0]
    if runtime.is_symlink():
        return
    runtime.rename(root / "moved-aside")
    runtime.symlink_to(planted.parent.parent, target_is_directory=True)


@POSIX_ONLY
def test_a_parent_replaced_under_a_store_refuses_and_plants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holding the directory open is what makes this a refusal instead of a write.

    The swap happens mid-write, after every check has passed. The material still
    goes to the directory that was proved -- there is no pathname left in the
    operation for the swap to redirect -- and the final identity comparison turns
    a success that is no longer reachable by the installation into the store's
    fixed refusal.
    """
    name = leaf_name(tmp_path)
    planted = decoy(tmp_path, name)
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    written = installed_credentials._write_all

    def swapping(descriptor: int, material: bytes) -> bool:
        swap_the_parent(tmp_path, planted)
        return written(descriptor, material)

    monkeypatch.setattr(installed_credentials, "_write_all", swapping)

    with pytest.raises(CredentialUnavailableError) as refused:
        keeper.store(REFERENCE, Credential(ROTATED))

    assert planted.read_text(encoding="ascii") == ROTATED
    assert sorted(path.name for path in planted.parent.iterdir()) == [name]
    assert refused.value.__context__ is None
    assert str(tmp_path) not in rendered(refused.value)


@POSIX_ONLY
def test_a_parent_replaced_under_a_resolve_refuses_rather_than_returning_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same swap on the read side, and the same answer.

    Without the comparison this would be the classic outcome: the checks pass
    against the real store, the attacker swaps the parent, and the bytes handed
    back are theirs. Here the read is anchored to the held descriptor, so the
    bytes are this installation's -- and the operation still refuses, because a
    store whose parent was replaced mid-read is not one to answer from.
    """
    name = leaf_name(tmp_path)
    planted = decoy(tmp_path, name)
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    read = installed_credentials.read_owner_private

    def swapping(
        path: Path, *, maximum_bytes: int, dir_fd: int | None = None
    ) -> bytes | None:
        content = read(path, maximum_bytes=maximum_bytes, dir_fd=dir_fd)
        swap_the_parent(tmp_path, planted)
        return content

    monkeypatch.setattr(installed_credentials, "read_owner_private", swapping)

    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)
    assert keeper.health(REFERENCE) == "unusable"
    assert planted.read_text(encoding="ascii") == ROTATED


@POSIX_ONLY
def test_the_credential_store_takes_the_same_pathname_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hardened pathname form is the credential store's too, not only the
    configuration store's.

    Forced on here for the reason it is forced on there: this host has ``dir_fd``
    and would otherwise never take the branch Windows takes. What is exercised is
    that branch's decisions -- a substituted store directory read from, written to
    or unlinked through -- with the same decoy the anchored cases use.
    """
    name = leaf_name(tmp_path)
    planted = decoy(tmp_path, name)
    real = directory(tmp_path)
    real.rmdir()
    real.symlink_to(planted.parent, target_is_directory=True)
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    keeper = store(tmp_path)

    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)
    with pytest.raises(CredentialUnavailableError):
        keeper.store(REFERENCE, Credential(SECRET))
    with pytest.raises(CredentialUnavailableError):
        keeper.remove(REFERENCE)

    assert keeper.health(REFERENCE) == "unusable"
    assert planted.read_text(encoding="ascii") == ROTATED
    assert sorted(path.name for path in planted.parent.iterdir()) == [name]


@POSIX_ONLY
def test_the_credential_store_survives_a_round_trip_through_the_pathname_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What one walk stores, the other resolves, and both leave one file behind."""
    keeper = store(tmp_path)
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    keeper.store(REFERENCE, Credential(SECRET))
    keeper.store(REFERENCE, Credential(ROTATED))
    assert keeper.resolve(REFERENCE).reveal() == ROTATED
    assert [path.name for path in directory(tmp_path).iterdir()] == [
        stored_file(tmp_path).name
    ]
    monkeypatch.undo()
    assert keeper.resolve(REFERENCE).reveal() == ROTATED
    keeper.remove(REFERENCE)
    assert keeper.health(REFERENCE) == "absent"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
def test_a_credential_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    """A FIFO would make a read block on whoever controls the other end."""
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    path = stored_file(tmp_path)
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)


@pytest.mark.parametrize(
    "reference",
    ["../../etc/passwd", "a/b", "..", "x" * 300, "", "ey.header.payload", "has space"],
)
def test_a_reference_outside_the_grammar_never_becomes_a_path(reference: str) -> None:
    """The grammar refuses it before a store is ever asked for a path.

    That is the whole defence and there is no second one here: a store method
    takes a ``CredentialReference``, and a ``CredentialReference`` cannot be
    constructed from anything carrying a separator, a ``..`` or a token.
    """
    with pytest.raises(CredentialInvalidError):
        CredentialReference(reference)


@pytest.mark.parametrize("supplied", ["omcp-plain-string", 7, None, Path("/tmp/x")])
def test_a_reference_that_is_not_a_reference_is_refused(
    tmp_path: Path, supplied: object
) -> None:
    keeper = store(tmp_path)
    with pytest.raises(CredentialInvalidError):
        keeper.resolve(supplied)  # type: ignore[arg-type]
    with pytest.raises(CredentialInvalidError):
        keeper.store(supplied, Credential(SECRET))  # type: ignore[arg-type]
    with pytest.raises(CredentialInvalidError):
        keeper.remove(supplied)  # type: ignore[arg-type]


def test_an_installation_root_must_be_an_absolute_path() -> None:
    for root in (Path("relative/state"), "/absolute/but/a/string", None):
        with pytest.raises(ValueError):
            InstalledCredentialStore(root)  # type: ignore[arg-type]


def test_only_a_credential_may_be_stored(tmp_path: Path) -> None:
    with pytest.raises(CredentialInvalidError):
        store(tmp_path).store(REFERENCE, SECRET)  # type: ignore[arg-type]


# --- bounded ---------------------------------------------------------------


def test_a_file_past_the_bound_is_not_a_credential(tmp_path: Path) -> None:
    """Bounded on the read, so an enormous file costs the bound and not its size."""
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    path = stored_file(tmp_path)
    path.write_bytes(b"a" * (4096 + 1))
    path.chmod(0o600)
    with pytest.raises(CredentialInvalidError):
        keeper.resolve(REFERENCE)
    assert keeper.health(REFERENCE) == "unusable"


def test_exactly_the_longest_admissible_credential_is_still_read(
    tmp_path: Path,
) -> None:
    keeper = store(tmp_path)
    longest = "a" * 4096
    keeper.store(REFERENCE, Credential(longest))
    assert keeper.resolve(REFERENCE).reveal() == longest


@pytest.mark.parametrize(
    "content",
    [b"", b"has space", b"with\nnewline", b"tab\there", b"\xff\xfe", b"trailing\n"],
)
def test_content_that_is_not_an_admissible_secret_is_refused(
    tmp_path: Path, content: bytes
) -> None:
    """Including a trailing newline: this store writes the bytes and nothing else,
    so a file somebody edited by hand is not silently trimmed into a credential."""
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    path = stored_file(tmp_path)
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(CredentialInvalidError):
        keeper.resolve(REFERENCE)


# --- atomic and fresh ------------------------------------------------------


def test_replacement_is_atomic_under_a_concurrent_reader(tmp_path: Path) -> None:
    """A reader racing a rotation sees one whole credential or the other.

    Never a truncated one, and never a refusal: the material is renamed over the
    destination rather than written into it, so there is no instant at which the
    destination is a partial file.
    """
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    seen: list[str] = []
    failures: list[BaseException] = []
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            try:
                seen.append(keeper.resolve(REFERENCE).reveal())
            except Exception as failure:  # noqa: BLE001 -- the point of the test
                failures.append(failure)
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    for _ in range(200):
        keeper.store(REFERENCE, Credential(ROTATED))
        keeper.store(REFERENCE, Credential(SECRET))
    stop.set()
    reader.join(timeout=10)
    assert failures == []
    assert set(seen) <= {SECRET, ROTATED}
    assert seen


def test_nothing_is_left_behind_by_a_replacement(tmp_path: Path) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    keeper.store(REFERENCE, Credential(ROTATED))
    assert sorted(path.name for path in directory(tmp_path).iterdir()) == [
        stored_file(tmp_path).name
    ]
    assert keeper.resolve(REFERENCE).reveal() == ROTATED


def test_rotation_and_revocation_land_on_the_next_call(tmp_path: Path) -> None:
    """No cache, so there is no entry to expire and nothing to clear.

    This is what makes a revoked credential fail the next call rather than at some
    session expiry: the store is read again every time it is asked.
    """
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    assert keeper.resolve(REFERENCE).reveal() == SECRET
    keeper.store(REFERENCE, Credential(ROTATED))
    assert keeper.resolve(REFERENCE).reveal() == ROTATED
    keeper.remove(REFERENCE)
    with pytest.raises(CredentialMissingError):
        keeper.resolve(REFERENCE)


def test_removal_is_idempotent(tmp_path: Path) -> None:
    keeper = store(tmp_path)
    keeper.remove(REFERENCE)
    keeper.store(REFERENCE, Credential(SECRET))
    keeper.remove(REFERENCE)
    keeper.remove(REFERENCE)
    assert keeper.health(REFERENCE) == "absent"


def test_removing_one_reference_leaves_the_others(tmp_path: Path) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    keeper.store(OTHER, Credential(ROTATED))
    keeper.remove(REFERENCE)
    assert keeper.health(REFERENCE) == "absent"
    assert keeper.resolve(OTHER).reveal() == ROTATED


def test_health_is_three_words_and_says_nothing_else(tmp_path: Path) -> None:
    keeper = store(tmp_path)
    assert keeper.health(REFERENCE) == "absent"
    keeper.store(REFERENCE, Credential(SECRET))
    assert keeper.health(REFERENCE) == "present"
    path = stored_file(tmp_path)
    path.write_bytes(b"not a credential")
    path.chmod(0o600)
    assert keeper.health(REFERENCE) == "unusable"


def test_a_store_that_cannot_be_written_refuses_rather_than_half_writes(
    tmp_path: Path,
) -> None:
    """A regular file where the directory should be: nothing is written, and the
    refusal is the store's fixed sentence rather than the operating system's."""
    (tmp_path / STORE_DIRECTORY[0]).mkdir()
    blocked = directory(tmp_path)
    blocked.write_text("not a directory", encoding="ascii")
    with pytest.raises(CredentialUnavailableError):
        store(tmp_path).store(REFERENCE, Credential(SECRET))


def short_writer(
    monkeypatch: pytest.MonkeyPatch, secret: str, *, accepted: int
) -> list[int]:
    """Make ``os.write`` accept at most `accepted` bytes of `secret` per call.

    A short write is legal and reports itself only in the return value, so this
    is the one failure a single ``os.write`` cannot notice. Only the store's own
    writes are shortened -- a slice of the material is a suffix of it, and that
    is how they are recognised -- because pytest's own capture writes through the
    same call and does not expect to be told it wrote less than it did.
    """
    real = os.write
    material = secret.encode("ascii")
    lengths: list[int] = []

    def write(descriptor: int, data: bytes) -> int:  # type: ignore[type-arg]
        payload = bytes(data)
        if payload and material.endswith(payload):
            lengths.append(len(payload))
            return real(descriptor, payload[:accepted])
        return real(descriptor, data)

    monkeypatch.setattr(os, "write", write)
    return lengths


def test_a_short_write_still_publishes_the_whole_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is looped to completion, so a partial one is finished, not filed.

    An ``os.write`` that accepts seven bytes at a time is a correct write. What
    would not be correct is renaming what it managed over the destination: the
    reader would get an owner-private, atomic, fresh file holding some prefix of
    a secret, which is a different secret and fails at the service instead of
    here.
    """
    keeper = store(tmp_path)
    lengths = short_writer(monkeypatch, SECRET, accepted=7)

    keeper.store(REFERENCE, Credential(SECRET))

    assert len(lengths) > 1, "one call took everything; the loop was not exercised"
    assert keeper.resolve(REFERENCE).reveal() == SECRET
    assert stored_file(tmp_path).read_bytes() == SECRET.encode("ascii")


def test_a_write_that_makes_no_progress_keeps_the_credential_that_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero accepted is a refusal: nothing published, nothing left, nothing chained.

    The destination keeps whatever it held -- the replacement is a rename and no
    rename happens -- the temporary is removed rather than left as a fragment of
    a bearer, and the refusal is the store's fixed sentence raised outside the
    handler, so ``__context__`` carries no ``OSError`` quoting the file's path.
    """
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    real = os.write
    material = ROTATED.encode("ascii")

    def stalled(descriptor: int, data: bytes) -> int:  # type: ignore[type-arg]
        payload = bytes(data)
        return 0 if payload and material.endswith(payload) else real(descriptor, data)

    monkeypatch.setattr(os, "write", stalled)

    with pytest.raises(CredentialUnavailableError) as refused:
        keeper.store(REFERENCE, Credential(ROTATED))

    monkeypatch.undo()
    assert keeper.resolve(REFERENCE).reveal() == SECRET
    assert sorted(path.name for path in directory(tmp_path).iterdir()) == [
        stored_file(tmp_path).name
    ]
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    text = rendered(refused.value)
    for secret in (SECRET, ROTATED, REFERENCE.value, str(tmp_path)):
        assert secret not in text


# --- silent ----------------------------------------------------------------


def test_no_refusal_carries_the_material_the_reference_or_the_path(
    tmp_path: Path,
) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    path = stored_file(tmp_path)

    failures: list[BaseException] = []
    path.chmod(0o644)
    with pytest.raises(CredentialMissingError) as missing:
        keeper.resolve(REFERENCE)
    failures.append(missing.value)
    path.chmod(0o600)
    path.write_bytes(b"\xff\xfe")
    path.chmod(0o600)
    with pytest.raises(CredentialInvalidError) as invalid:
        keeper.resolve(REFERENCE)
    failures.append(invalid.value)

    for failure in failures:
        text = rendered(failure)
        for secret in (SECRET, REFERENCE.value, str(path), str(tmp_path)):
            assert secret not in text, failure
        assert failure.__cause__ is None
        assert failure.__context__ is None


def test_the_store_renders_as_nothing_and_the_credential_redacts(
    tmp_path: Path,
) -> None:
    keeper = store(tmp_path)
    keeper.store(REFERENCE, Credential(SECRET))
    for text in (repr(keeper), str(keeper), f"{keeper}"):
        assert text == "InstalledCredentialStore(<redacted>)"
        assert str(tmp_path) not in text
    credential = keeper.resolve(REFERENCE)
    for text in (repr(credential), str(credential), f"{credential}"):
        assert SECRET not in text
        assert text == "<credential redacted>"


def test_the_material_is_in_the_file_and_in_no_other_file(tmp_path: Path) -> None:
    """Nothing is copied out of the store: one file holds it, and only one."""
    store(tmp_path).store(REFERENCE, Credential(SECRET))
    holders = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and SECRET.encode("ascii") in path.read_bytes()
    ]
    assert holders == [stored_file(tmp_path)]


# --- the protected configuration store ---------------------------------------
#
# One installation's `omnivia.mcp-config.v1` documents: the credential store's
# layout, the credential store's proofs, a closed host word for a key and a
# bounded document for content. Everything adversarial below is parameterized
# over `form`, so each case is asserted against the descriptor-anchored walk and
# against the pathname walk Windows takes.

HOST = "claude-code"
OTHER_HOST = "codex"
DOCUMENT = b'{"format": "omnivia.mcp-config.v1"}\n'
REWRITTEN = b'{"format": "omnivia.mcp-config.v1", "mutation_enabled": true}\n'


@pytest.fixture(params=["anchored", "pathname"])
def form(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the case over both walks: the one this host uses and the other one.

    The pathname form is what a host with no ``dir_fd`` takes, which in practice
    is Windows. Forcing it here exercises what that form *decides* -- which
    components it proves, what it will not create through an unproved parent, what
    it will not unlink -- on a host where those decisions can actually be
    provoked with a symlink. The native security call it also makes on Windows is
    not faked and not claimed: it does not exist here, and
    ``test_owner_private.py`` covers that half with the call doubled.
    """
    chosen = str(request.param)
    if chosen == "anchored" and not installed_credentials._ANCHORED:
        pytest.skip("this host has no dir_fd and takes the pathname form")
    monkeypatch.setattr(installed_credentials, "_ANCHORED", chosen == "anchored")
    return chosen


def configs(root: Path) -> InstalledConfigStore:
    return InstalledConfigStore(root)


def config_directory(root: Path) -> Path:
    return root.joinpath(*CONFIGURATION_STORE_DIRECTORY)


# --- derived, closed, and public only in the one way it must be ---------------


def test_the_configuration_path_is_derived_from_the_root_and_the_host(
    tmp_path: Path,
) -> None:
    store = configs(tmp_path)
    assert (
        store.path(HOST) == tmp_path / "runtime" / ".installed-mcp" / "claude-code.json"
    )
    assert store.path(OTHER_HOST) == config_directory(tmp_path) / "codex.json"
    assert store.path(HOST).parent == config_directory(tmp_path)


@pytest.mark.parametrize(
    "host",
    [
        "../../etc/passwd",
        "a/b",
        "..",
        "",
        "CLAUDE-CODE",
        "claude-code.json",
        "con",
        "nul",
        "claude-code ",
        "codex/../codex",
    ],
)
def test_only_the_closed_host_vocabulary_names_a_configuration(
    tmp_path: Path, host: str
) -> None:
    """Closed here rather than at the caller, because that is what makes it safe.

    A fixed word cannot traverse out of the store, cannot differ only in case and
    cannot spell a reserved device name -- and every operation asks for it,
    including the one that only computes a path, so there is no method that would
    hand a caller a pathname the rest of the store would refuse.
    """
    store = configs(tmp_path)
    for call in (
        lambda: store.path(host),
        lambda: store.read(host),
        lambda: store.health(host),
        lambda: store.write(host, DOCUMENT),
        lambda: store.remove(host),
    ):
        with pytest.raises(ValueError):
            call()
    assert not config_directory(tmp_path).exists()


def test_the_vocabulary_is_the_two_hosts_and_nothing_else() -> None:
    assert CONFIGURATION_HOSTS == ("claude-code", "codex")


def test_an_installation_root_must_be_an_absolute_path_here_too() -> None:
    for root in (Path("relative/state"), "/absolute/but/a/string", None):
        with pytest.raises(ValueError):
            InstalledConfigStore(root)  # type: ignore[arg-type]


def test_the_configuration_store_renders_as_nothing(tmp_path: Path) -> None:
    store = configs(tmp_path)
    for text in (repr(store), str(store), f"{store}"):
        assert text == "InstalledConfigStore(<redacted>)"
        assert str(tmp_path) not in text


# --- private, atomic, fresh ---------------------------------------------------


def test_a_written_configuration_comes_back_exactly(tmp_path: Path, form: str) -> None:
    store = configs(tmp_path)
    assert store.health(HOST) == "absent"
    assert store.write(HOST, DOCUMENT) is True
    assert store.read(HOST) == DOCUMENT
    assert store.health(HOST) == "present"
    assert store.path(HOST).read_bytes() == DOCUMENT


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
def test_the_configuration_and_its_directory_are_owner_private_from_creation(
    tmp_path: Path, form: str
) -> None:
    assert configs(tmp_path).write(HOST, DOCUMENT) is True
    assert stat.S_IMODE(config_directory(tmp_path).stat().st_mode) == 0o700
    assert stat.S_IMODE(configs(tmp_path).path(HOST).stat().st_mode) == 0o600


def test_each_host_is_one_file_and_removing_one_leaves_the_other(
    tmp_path: Path, form: str
) -> None:
    store = configs(tmp_path)
    assert store.write(HOST, DOCUMENT) is True
    assert store.write(OTHER_HOST, REWRITTEN) is True
    assert sorted(path.name for path in config_directory(tmp_path).iterdir()) == [
        "claude-code.json",
        "codex.json",
    ]
    assert store.remove(HOST) is True
    assert store.health(HOST) == "absent"
    assert store.read(OTHER_HOST) == REWRITTEN


def test_a_replacement_leaves_no_temporary_and_no_previous_document(
    tmp_path: Path, form: str
) -> None:
    store = configs(tmp_path)
    assert store.write(HOST, DOCUMENT) is True
    assert store.write(HOST, REWRITTEN) is True
    assert store.read(HOST) == REWRITTEN
    assert [path.name for path in config_directory(tmp_path).iterdir()] == [
        "claude-code.json"
    ]


def test_replacement_is_atomic_under_a_concurrent_configuration_reader(
    tmp_path: Path,
) -> None:
    """A reader racing a rewrite sees one whole document or the other."""
    store = configs(tmp_path)
    assert store.write(HOST, DOCUMENT) is True
    seen: list[bytes | None] = []
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            seen.append(store.read(HOST))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    for _ in range(200):
        store.write(HOST, REWRITTEN)
        store.write(HOST, DOCUMENT)
    stop.set()
    reader.join(timeout=10)
    assert set(seen) <= {DOCUMENT, REWRITTEN}
    assert seen


def test_removal_is_idempotent_and_an_absent_host_is_already_removed(
    tmp_path: Path, form: str
) -> None:
    store = configs(tmp_path)
    assert store.remove(HOST) is True
    assert store.write(HOST, DOCUMENT) is True
    assert store.remove(HOST) is True
    assert store.remove(HOST) is True
    assert store.health(HOST) == "absent"
    assert store.read(HOST) is None


def test_a_rewrite_lands_on_the_next_read(tmp_path: Path, form: str) -> None:
    """Nothing is cached, so there is no entry to expire and nothing to clear."""
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    assert store.read(HOST) == DOCUMENT
    store.write(HOST, REWRITTEN)
    assert store.read(HOST) == REWRITTEN


# --- bounded ------------------------------------------------------------------


def test_a_document_at_the_bound_is_written_and_read_back(
    tmp_path: Path, form: str
) -> None:
    store = configs(tmp_path)
    longest = b"a" * MAXIMUM_CONFIGURATION_BYTES
    assert store.write(HOST, longest) is True
    assert store.read(HOST) == longest
    assert store.health(HOST) == "present"


def test_a_document_past_the_bound_is_never_written(tmp_path: Path, form: str) -> None:
    """Refused before anything is created: a file this store's own reader would
    refuse is not one worth leaving on disk."""
    store = configs(tmp_path)
    assert store.write(HOST, b"a" * (MAXIMUM_CONFIGURATION_BYTES + 1)) is False
    assert store.health(HOST) == "absent"
    assert (
        not config_directory(tmp_path).exists()
        or list(config_directory(tmp_path).iterdir()) == []
    )


def test_a_file_grown_past_the_bound_is_unusable_rather_than_truncated(
    tmp_path: Path, form: str
) -> None:
    """Bounded on the read, so an enormous file costs the bound and not its size."""
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    path = store.path(HOST)
    path.write_bytes(b"b" * (MAXIMUM_CONFIGURATION_BYTES + 1))
    path.chmod(0o600)
    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"


@pytest.mark.parametrize("content", ["not bytes", 7, None, bytearray(b"{}")])
def test_only_bytes_are_written(tmp_path: Path, content: object) -> None:
    assert configs(tmp_path).write(HOST, content) is False  # type: ignore[arg-type]


# --- private: what is not owner-private is not a configuration ----------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666, 0o660])
def test_a_group_or_world_readable_configuration_is_not_read(
    tmp_path: Path, mode: int, form: str
) -> None:
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    store.path(HOST).chmod(mode)
    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
def test_a_group_or_world_reachable_configuration_directory_is_not_a_store(
    tmp_path: Path, form: str
) -> None:
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    config_directory(tmp_path).chmod(0o755)
    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"
    assert store.write(HOST, REWRITTEN) is False
    assert store.remove(HOST) is False
    config_directory(tmp_path).chmod(0o700)
    assert store.read(HOST) == DOCUMENT


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_world_writable_runtime_directory_is_not_walked_through(
    tmp_path: Path, form: str
) -> None:
    """Who may read `runtime/` changes nothing; who may write it is the whole swap."""
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    runtime = tmp_path / CONFIGURATION_STORE_DIRECTORY[0]
    runtime.chmod(0o777)
    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"
    assert store.write(HOST, REWRITTEN) is False
    assert store.remove(HOST) is False
    runtime.chmod(0o755)
    assert store.read(HOST) == DOCUMENT


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership uses the effective uid")
def test_a_configuration_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    mine = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: mine + 1)
    assert store.read(HOST) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
def test_a_configuration_that_is_not_a_regular_file_is_refused(
    tmp_path: Path, form: str
) -> None:
    """A FIFO would make a read block on whoever controls the other end."""
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    path = store.path(HOST)
    path.unlink()
    os.mkfifo(path, 0o600)
    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"


# --- path substitution --------------------------------------------------------
#
# Each of these builds the attacker's directory as a *working* store, so an
# implementation that resolved `installation_state / "runtime" / ".installed-mcp"
# / "<host>.json"` as a pathname would succeed and read the attacker's document,
# or write this installation's into their directory. Refusing is the assertion;
# the second half of each is that nothing outside the real store was created,
# read, overwritten or deleted.


def config_decoy(root: Path, content: bytes = REWRITTEN) -> Path:
    """A whole installation the attacker owns, and the document planted in it."""
    elsewhere = root / "elsewhere"
    planted = elsewhere / CONFIGURATION_STORE_DIRECTORY[1] / f"{HOST}.json"
    planted.parent.mkdir(mode=0o700, parents=True)
    elsewhere.chmod(0o700)
    planted.write_bytes(content)
    planted.chmod(0o600)
    return planted


@POSIX_ONLY
def test_a_symlinked_runtime_creates_no_configuration_on_the_other_side(
    tmp_path: Path, form: str
) -> None:
    """`runtime/` is the one component this store will create, so it is walked too."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / CONFIGURATION_STORE_DIRECTORY[0]).symlink_to(
        elsewhere, target_is_directory=True
    )
    store = configs(tmp_path)

    assert store.write(HOST, DOCUMENT) is False
    assert store.read(HOST) is None
    assert store.health(HOST) == "absent"
    # Nothing stands where the store belongs, so a removal has nothing to do and
    # says so. What it must not do -- and does not -- is follow the link and
    # unlink whatever it finds; the refusal for that case is the test below,
    # where the attacker's directory does hold a document.
    assert store.remove(HOST) is True

    assert list(elsewhere.rglob("*")) == []


@POSIX_ONLY
def test_a_symlinked_configuration_directory_reads_writes_and_deletes_nothing(
    tmp_path: Path, form: str
) -> None:
    """All four operations refuse, not three: a revoke must not unlink down it."""
    planted = config_decoy(tmp_path)
    real = config_directory(tmp_path)
    real.parent.mkdir(parents=True, exist_ok=True)
    real.symlink_to(planted.parent, target_is_directory=True)
    store = configs(tmp_path)

    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"
    assert store.write(HOST, DOCUMENT) is False
    assert store.remove(HOST) is False

    assert planted.read_bytes() == REWRITTEN
    assert [path.name for path in planted.parent.iterdir()] == [f"{HOST}.json"]


@POSIX_ONLY
def test_a_symlinked_configuration_is_replaced_rather_than_written_through(
    tmp_path: Path, form: str
) -> None:
    """The leaf is the last name an attacker controls, and it is never followed."""
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    outside = tmp_path / "victim.json"
    outside.write_bytes(b"untouched")
    outside.chmod(0o600)

    linked = store.path(HOST)
    linked.unlink()
    linked.symlink_to(outside)
    assert store.write(HOST, REWRITTEN) is True

    assert outside.read_bytes() == b"untouched"
    assert not linked.is_symlink()
    assert store.read(HOST) == REWRITTEN

    linked.unlink()
    linked.symlink_to(outside)
    assert store.remove(HOST) is True

    assert outside.read_bytes() == b"untouched"
    assert not linked.exists() and not linked.is_symlink()


@POSIX_ONLY
def test_a_symlinked_configuration_is_not_read_through(
    tmp_path: Path, form: str
) -> None:
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    outside = tmp_path / "attacker.json"
    outside.write_bytes(REWRITTEN)
    outside.chmod(0o600)
    linked = store.path(HOST)
    linked.unlink()
    linked.symlink_to(outside)

    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"


@POSIX_ONLY
def test_a_parent_replaced_under_a_configuration_write_refuses_and_plants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """The swap lands mid-write, after every check has passed.

    Anchored, the material still goes to the directory that was proved and the
    final identity comparison turns a success the installation cannot reach into a
    refusal. By pathname, the chain is proved again before the rename and the
    replacement never happens. Either way the attacker's directory is untouched
    and the caller is told the write did not land.
    """
    planted = config_decoy(tmp_path)
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    written = installed_credentials._write_all

    def swapping(descriptor: int, material: bytes) -> bool:
        swap_the_parent(tmp_path, planted)
        return written(descriptor, material)

    monkeypatch.setattr(installed_credentials, "_write_all", swapping)

    assert store.write(HOST, REWRITTEN) is False
    assert planted.read_bytes() == REWRITTEN
    assert [path.name for path in planted.parent.iterdir()] == [f"{HOST}.json"]


@POSIX_ONLY
def test_a_parent_replaced_under_a_configuration_read_refuses_rather_than_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """Without the comparison this is the classic outcome: the attacker's bytes."""
    planted = config_decoy(tmp_path)
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    read = installed_credentials.read_owner_private

    def swapping(
        path: Path, *, maximum_bytes: int, dir_fd: int | None = None
    ) -> bytes | None:
        content = read(path, maximum_bytes=maximum_bytes, dir_fd=dir_fd)
        swap_the_parent(tmp_path, planted)
        return content

    monkeypatch.setattr(installed_credentials, "read_owner_private", swapping)

    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"
    assert planted.read_bytes() == REWRITTEN


@POSIX_ONLY
def test_a_parent_replaced_under_a_configuration_removal_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """An unlink is the one operation that cannot be taken back, so it is proved
    on both sides of itself too."""
    planted = config_decoy(tmp_path)
    store = configs(tmp_path)
    store.write(HOST, DOCUMENT)
    unlink = os.unlink

    def swapping(*arguments: object, **keywords: object) -> None:
        unlink(*arguments, **keywords)  # type: ignore[arg-type]
        swap_the_parent(tmp_path, planted)

    monkeypatch.setattr(os, "unlink", swapping)
    monkeypatch.setattr(Path, "unlink", lambda self: swapping(self))

    assert store.remove(HOST) is False
    assert planted.read_bytes() == REWRITTEN


def test_a_regular_file_where_the_configuration_directory_belongs_is_refused(
    tmp_path: Path, form: str
) -> None:
    (tmp_path / CONFIGURATION_STORE_DIRECTORY[0]).mkdir()
    blocked = config_directory(tmp_path)
    blocked.write_bytes(b"not a directory")
    store = configs(tmp_path)

    assert store.write(HOST, DOCUMENT) is False
    assert store.read(HOST) is None
    assert store.health(HOST) == "unusable"
    # Something stands where the store belongs, so this is not the idempotent
    # absence case and a removal must not report one.
    assert store.remove(HOST) is False
    assert blocked.read_bytes() == b"not a directory"


# --- the pathname form's own decisions ----------------------------------------


def test_the_pathname_form_creates_no_component_through_an_unproved_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `mkdir(parents=True)`: each component is made below a proved chain.

    Asserted by watching what is created rather than by reading the source: two
    ``mkdir`` calls, one per component, each with the mode that component is meant
    to have -- and the chain above each one proved immediately before it.
    """
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    made: list[tuple[str, int]] = []
    real = Path.mkdir

    def watched(self: Path, mode: int = 0o777, **keywords: object) -> None:
        assert "parents" not in keywords, "a component was created through a parent"
        made.append((self.name, mode))
        real(self, mode)

    monkeypatch.setattr(Path, "mkdir", watched)
    assert configs(tmp_path).write(HOST, DOCUMENT) is True
    assert made == [
        (CONFIGURATION_STORE_DIRECTORY[0], 0o755),
        (CONFIGURATION_STORE_DIRECTORY[1], 0o700),
    ]


def test_the_pathname_form_refuses_when_the_chain_stops_proving_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the temporary and the publication is a window, and it is closed.

    The chain proved at the start of the write is proved again just before the
    rename. A component that stopped proving in between ends the write with the
    previous document still in place and no temporary left behind.
    """
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    store = configs(tmp_path)
    assert store.write(HOST, DOCUMENT) is True
    real = installed_credentials.owner_private_chain
    calls: list[int] = []
    # Three proofs walk the layout into existence and prove it; the fourth is the
    # one taken between the temporary and the rename, and it is the one refused.
    refused_call = 4

    def failing(root: Path, names: object, **keywords: object) -> object:
        calls.append(1)
        if len(calls) == refused_call:
            return None
        return real(root, names, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(installed_credentials, "owner_private_chain", failing)
    assert store.write(HOST, REWRITTEN) is False

    monkeypatch.undo()
    assert store.read(HOST) == DOCUMENT
    assert [path.name for path in config_directory(tmp_path).iterdir()] == [
        f"{HOST}.json"
    ]


def test_the_pathname_form_will_not_unlink_down_a_chain_it_cannot_prove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    store = configs(tmp_path)
    assert store.write(HOST, DOCUMENT) is True
    monkeypatch.setattr(
        installed_credentials, "owner_private_chain", lambda _root, _names: None
    )
    unlinked: list[object] = []
    monkeypatch.setattr(Path, "unlink", lambda self: unlinked.append(self))

    assert store.remove(HOST) is False
    assert unlinked == []

    monkeypatch.undo()
    assert store.read(HOST) == DOCUMENT


def test_the_pathname_form_reports_an_absent_store_as_already_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent absence and an unprovable chain are told apart by what is there."""
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    assert configs(tmp_path).remove(HOST) is True


def test_the_pathname_form_refuses_a_short_write_rather_than_publishing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.write` may accept less than it was handed and raise nothing."""
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    store = configs(tmp_path)
    real = os.write

    def stalled(descriptor: int, data: bytes) -> int:  # type: ignore[type-arg]
        payload = bytes(data)
        return 0 if payload and DOCUMENT.endswith(payload) else real(descriptor, data)

    monkeypatch.setattr(os, "write", stalled)
    assert store.write(HOST, DOCUMENT) is False

    monkeypatch.undo()
    assert store.health(HOST) == "absent"
    assert list(config_directory(tmp_path).iterdir()) == []


def test_the_pathname_form_takes_the_owner_only_proof_before_a_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform that will not give an owner-only file gets no document in it."""
    monkeypatch.setattr(installed_credentials, "_ANCHORED", False)
    monkeypatch.setattr(
        installed_credentials, "owner_private_file", lambda _m, _d: False
    )
    assert configs(tmp_path).write(HOST, DOCUMENT) is False
    assert list(config_directory(tmp_path).iterdir()) == []


def test_the_two_forms_agree_on_what_is_written(tmp_path: Path) -> None:
    """What one walk writes, the other reads back, and the file is the same file."""
    store = configs(tmp_path)
    for anchored in (True, False):
        if anchored and not installed_credentials._ANCHORED:
            continue
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(installed_credentials, "_ANCHORED", anchored)
            assert store.write(HOST, DOCUMENT) is True
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(installed_credentials, "_ANCHORED", not anchored)
            assert store.read(HOST) == DOCUMENT
            assert store.health(HOST) == "present"
            assert store.remove(HOST) is True
