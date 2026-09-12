"""The owner-private file proof, including the Windows owner-and-DACL reader.

These tests were the MCP configuration reader's while the proof lived in that
package. They moved with it, unchanged apart from the module they name: the
proof is now shared, because the installed credential store reads its files
under exactly the same rules and a second copy of a security check is a second
copy to keep correct.

The Windows reader is exercised on every platform, because the part that is easy
to get wrong is not the native call -- it is the ACE walk and the owner-only
policy over the facts that call returns. `FakeSecurityApi` stands in for the
Win32 entry points with real `ctypes` memory, so the SID copying, the declared-
size checks and the handle releases run here as they would there.
"""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client import owner_private

#: Every access right in this file's ACEs unless a test asks for another:
#: `FILE_ALL_ACCESS`, which is what a real owner ACE on a private file carries.
FULL_ACCESS = 0x1F01FF

#: What an inherited profile ACE routinely grants SYSTEM or the administrators on
#: a *shared* installation directory: read and execute, and nothing that could
#: change what the directory holds. `FILE_GENERIC_READ | FILE_EXECUTE`.
READ_ACCESS = 0x1200A9


@pytest.mark.parametrize(
    ("owner_matches", "aces", "owner_only"),
    [
        # An owner-only file: every access-allowed ACE names the owner.
        (True, ((0, True, FULL_ACCESS),), True),
        (True, ((1, False, 0), (0, True, FULL_ACCESS)), True),
        # A present but empty DACL denies everyone, which is narrower still.
        (True, (), True),
        # An absent or NULL DACL grants everyone everything.
        (True, None, False),
        # The file is owner-only for somebody who is not this process's user.
        (False, ((0, True, FULL_ACCESS),), False),
        (False, None, False),
        # An access-allowed ACE naming another principal widens the reach --
        # whatever it grants. This is the policy for a directory holding bearers,
        # so read is as unacceptable as write.
        (True, ((0, False, FULL_ACCESS),), False),
        (True, ((0, False, READ_ACCESS),), False),
        (True, ((0, True, FULL_ACCESS), (0, False, READ_ACCESS)), False),
        # Unrecognised access-allowed forms: object, callback, callback-object,
        # compound. Their grantee is not where this reader looks for it.
        (True, ((5, True, FULL_ACCESS),), False),
        (True, ((9, True, FULL_ACCESS),), False),
        (True, ((11, True, FULL_ACCESS),), False),
        (True, ((4, True, FULL_ACCESS),), False),
    ],
)
def test_the_windows_owner_only_dacl_policy(
    owner_matches: bool,
    aces: tuple[tuple[int, bool, int], ...] | None,
    owner_only: bool,
) -> None:
    assert owner_private._owner_only_dacl(owner_matches, aces) is owner_only


@pytest.mark.parametrize(
    ("owner_matches", "aces", "admitted"),
    [
        # The owner alone, as strict as the owner-only policy.
        (True, ((0, True, FULL_ACCESS),), True),
        # What a real shared installation directory looks like: somebody else may
        # read it, and nobody else may change what it holds. Refusing this is what
        # would refuse every real installation, and it is the whole difference
        # between this policy and the one above.
        (True, ((0, True, FULL_ACCESS), (0, False, READ_ACCESS)), True),
        (True, (), True),
        # Every individual right that lets a holder replace a component, one ACE
        # at a time: nobody else may hold any of them.
        *(
            (True, ((0, False, right),), False)
            for right in (
                0x00000002,  # FILE_ADD_FILE
                0x00000004,  # FILE_ADD_SUBDIRECTORY
                0x00000010,  # FILE_WRITE_EA
                0x00000040,  # FILE_DELETE_CHILD
                0x00000100,  # FILE_WRITE_ATTRIBUTES
                0x00010000,  # DELETE
                0x00040000,  # WRITE_DAC
                0x00080000,  # WRITE_OWNER
                0x10000000,  # GENERIC_ALL
                0x40000000,  # GENERIC_WRITE
            )
        ),
        # A NULL DACL grants everyone everything, and somebody else owning it is
        # the substitution this policy exists to refuse.
        (True, None, False),
        (False, ((0, True, FULL_ACCESS),), False),
        (False, ((0, False, READ_ACCESS),), False),
        # Unrecognised access-allowed forms are refused here too: the mask is not
        # where this reader would find their grantee.
        (True, ((9, False, READ_ACCESS),), False),
    ],
)
def test_the_windows_parent_dacl_policy_admits_readers_and_no_writer(
    owner_matches: bool,
    aces: tuple[tuple[int, bool, int], ...] | None,
    admitted: bool,
) -> None:
    """A shared component is proved unswappable rather than proved private.

    An installation root and its `runtime/` are legitimately readable by SYSTEM
    and the local administrators on Windows. What makes a component substitutable
    is somebody else being able to *write* the name, so that -- and only that --
    is what the parent policy refuses.
    """
    assert owner_private._owner_writable_dacl(owner_matches, aces) is admitted


def windows_sid(*subauthorities: int) -> bytes:
    """One well-formed binary SID: revision, count, authority, subauthorities."""
    return bytes([1, len(subauthorities), 0, 0, 0, 0, 0, 5]) + b"".join(
        value.to_bytes(4, "little") for value in subauthorities
    )


def windows_ace(
    kind: int, sid: bytes, *, size: int | None = None, mask: int = FULL_ACCESS
) -> bytes:
    """One ACE: `ACE_HEADER`, `ACCESS_MASK`, then the trustee SID inline."""
    declared = len(sid) + 8 if size is None else size
    return (
        bytes([kind, 0])
        + declared.to_bytes(2, "little")
        + mask.to_bytes(4, "little")
        + sid
    )


OWNER_SID = windows_sid(21, 1, 2, 3, 1001)
OTHER_SID = windows_sid(21, 1, 2, 3, 1002)


class FakeSecurityApi:
    """A Win32 double that lays its ACL out in real memory.

    Only the API is faked. The decoder under test still walks raw addresses,
    reads `ACE_HEADER` fields at their true offsets, and copies each SID out of
    that memory -- the part that cannot otherwise run off Windows.
    """

    def __init__(
        self,
        aces: list[bytes],
        *,
        owner: bytes = OWNER_SID,
        user: bytes = OWNER_SID,
        dacl_present: bool = True,
        security_error: int = 0,
    ) -> None:
        self.acl = ctypes.create_string_buffer(b"".join(aces) or b"\0")
        self.addresses: list[int] = []
        offset = 0
        for encoded in aces:
            self.addresses.append(ctypes.addressof(self.acl) + offset)
            offset += len(encoded)
        self.owner = ctypes.create_string_buffer(owner)
        self.user = ctypes.create_string_buffer(user)
        self.token_user = owner_private._TokenUser()
        self.token_user.User.Sid = ctypes.addressof(self.user)
        self.dacl_present = dacl_present
        self.security_error = security_error
        self.freed: list[object] = []
        self.closed: list[object] = []

    def get_osfhandle(self, descriptor: int) -> int:
        return 500 + descriptor

    def GetSecurityInfo(
        self,
        handle: int,
        kind: int,
        wanted: int,
        owner: Any,
        group: Any,
        dacl: Any,
        sacl: Any,
        security: Any,
    ) -> int:
        owner.value = ctypes.addressof(self.owner)
        dacl.value = ctypes.addressof(self.acl) if self.dacl_present else None
        security.value = 0xD0D0
        return self.security_error

    def GetNamedSecurityInfoW(
        self,
        name: str,
        kind: int,
        wanted: int,
        owner: Any,
        group: Any,
        dacl: Any,
        sacl: Any,
        security: Any,
    ) -> int:
        """The name form fills the same out-parameters the handle form does.

        That is the whole difference between the two on this side of the seam:
        one is asked about an open handle and the other about a pathname, and
        everything that decides the verdict -- the owner SID, this process's
        token user, the ACE walk and the release -- is one decoder over the
        parameters they both fill in.
        """
        return self.GetSecurityInfo(0, kind, wanted, owner, group, dacl, sacl, security)

    def GetCurrentProcess(self) -> int:
        return 7

    def OpenProcessToken(self, process: int, access: int, token: Any) -> int:
        token.value = 0x7070
        return 1

    def GetTokenInformation(
        self, token: Any, kind: int, buffer: Any, size: int, needed: Any
    ) -> int:
        needed.value = ctypes.sizeof(self.token_user)
        if buffer is None:
            return 0
        ctypes.memmove(buffer, ctypes.byref(self.token_user), needed.value)
        return 1

    def IsValidSid(self, sid: int) -> int:
        return 1

    def GetLengthSid(self, sid: int) -> int:
        return 8 + 4 * ctypes.string_at(sid + 1, 1)[0]

    def GetAclInformation(
        self, acl: int, information: Any, size: int, kind: int
    ) -> int:
        information.AceCount = len(self.addresses)
        return 1

    def GetAce(self, acl: int, index: int, ace: Any) -> int:
        ace.value = self.addresses[index]
        return 1

    def CloseHandle(self, handle: Any) -> int:
        self.closed.append(handle.value)
        return 1

    def LocalFree(self, memory: Any) -> int:
        self.freed.append(memory.value)
        return 0


def test_the_windows_ace_walk_reads_each_grantee_and_mask_out_of_real_memory() -> None:
    """The mask travels beside the verdict: the two policies want different things.

    The owner-only proof does not care what an outsider's ACE grants and the
    parent proof cares about nothing else, so the walk decodes both and judges
    neither.
    """
    api = FakeSecurityApi(
        [
            windows_ace(0, OWNER_SID),
            windows_ace(1, OTHER_SID),
            windows_ace(0, OTHER_SID, mask=READ_ACCESS),
            windows_ace(9, OWNER_SID),
        ]
    )
    assert owner_private._dacl_aces(api, 1, OWNER_SID) == (
        (0, True, FULL_ACCESS),
        (1, False, 0),
        (0, False, READ_ACCESS),
        (9, False, 0),
    )


def test_a_null_dacl_is_reported_as_no_dacl_rather_than_an_empty_one() -> None:
    assert owner_private._dacl_aces(FakeSecurityApi([]), None, OWNER_SID) is None
    assert owner_private._dacl_aces(FakeSecurityApi([]), 1, OWNER_SID) == ()


def test_an_ace_declaring_less_room_than_its_sid_needs_is_refused() -> None:
    api = FakeSecurityApi([windows_ace(0, OWNER_SID, size=8)])
    with pytest.raises(OSError):
        owner_private._dacl_aces(api, 1, OWNER_SID)


@pytest.mark.parametrize(
    ("api", "owner_only"),
    [
        (FakeSecurityApi([windows_ace(0, OWNER_SID)]), True),
        (
            FakeSecurityApi([windows_ace(0, OWNER_SID), windows_ace(0, OTHER_SID)]),
            False,
        ),
        (FakeSecurityApi([windows_ace(0, OWNER_SID)], user=OTHER_SID), False),
        (FakeSecurityApi([], dacl_present=False), False),
    ],
)
def test_the_windows_verdict_from_end_to_end_native_facts(
    api: FakeSecurityApi, owner_only: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner_private, "_SECURITY_API", api)
    assert owner_private._windows_owner_only(3) is owner_only
    assert api.freed == [0xD0D0]
    assert api.closed == [0x7070]


def test_the_security_descriptor_and_token_are_released_when_the_walk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeSecurityApi([windows_ace(0, OWNER_SID, size=8)])
    monkeypatch.setattr(owner_private, "_SECURITY_API", api)
    with pytest.raises(OSError):
        owner_private._windows_owner_only(3)
    assert api.freed == [0xD0D0]
    assert api.closed == [0x7070]


def test_a_security_info_error_still_releases_an_allocated_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeSecurityApi([], security_error=5)
    monkeypatch.setattr(owner_private, "_SECURITY_API", api)
    with pytest.raises(OSError):
        owner_private._windows_owner_only(3)
    assert api.freed == [0xD0D0]
    assert api.closed == []


def test_the_windows_verifier_decides_only_from_the_native_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for facts, expected in (
        ((True, ((0, True, FULL_ACCESS),)), True),
        ((True, ((0, False, READ_ACCESS),)), False),
        ((True, None), False),
        ((False, ((0, True, FULL_ACCESS),)), False),
    ):
        monkeypatch.setattr(owner_private, "_windows_acl_facts", lambda _d, f=facts: f)
        assert owner_private._windows_owner_only(7) is expected


@pytest.mark.skipif(os.name != "nt", reason="the native proof is Windows-only")
def test_the_native_windows_proof_reads_a_real_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "protected.txt"
    path.write_text("content", encoding="utf-8")
    path.chmod(0o600)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        # The owner verdict is not asserted: an elevated process creates files
        # owned by Administrators, so it is host policy, not a property of this
        # code. What is asserted is that the whole native path completes, hands
        # back well-formed facts, and stays stable when repeated -- a leaked or
        # double-freed handle or descriptor would not survive the second call.
        owner_matches, aces = owner_private._windows_acl_facts(descriptor)
        assert isinstance(owner_matches, bool)
        assert aces is not None and all(
            isinstance(kind, int)
            and isinstance(grants_owner, bool)
            and isinstance(mask, int)
            for kind, grants_owner, mask in aces
        )
        assert owner_private._windows_acl_facts(descriptor) == (owner_matches, aces)
        assert owner_private._windows_owner_only(descriptor) is (
            owner_private._owner_only_dacl(owner_matches, aces)
        )
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="the native proof is Windows-only")
def test_the_native_windows_proof_fails_closed_on_an_unusable_descriptor() -> None:
    with pytest.raises(OSError):
        owner_private._windows_owner_only(-1)


# --- restricting a newly created object to its owner alone --------------------
#
# `restrict_to_owner` is what stands in for a POSIX `mkdir`/`mkstemp` mode on
# Windows, where there are no mode bits and creation hands the object whatever
# DACL its parent's inheritance and the caller's token supply. It is a
# `subprocess`/`icacls` writer rather than a native one -- see the module's own
# docstring for why -- so it is exercised here with `subprocess.run` doubled,
# the same way `tests/package_qualification/test_standard_journey.py` exercises
# the identical, independently hosted mechanism.


def _windows_commands(
    monkeypatch: pytest.MonkeyPatch, *results: tuple[int, str]
) -> list[list[str]]:
    """Run the writer with `subprocess.run` doubled, returning the commands issued.

    `results` is one `(returncode, stdout)` pair per expected call, in order.
    """
    commands: list[list[str]] = []
    pending = list(results)

    def fake_run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        code, stdout = pending.pop(0)
        return subprocess.CompletedProcess(list(arguments), code, stdout, "")

    monkeypatch.setattr(owner_private.subprocess, "run", fake_run)
    monkeypatch.setenv("SystemRoot", "D:\\Windows")
    return commands


def _expected_tool(program: str) -> str:
    return str(Path("D:\\Windows", "System32", program))


def _whoami_row() -> str:
    return '"host\\user","S-1-5-21-1111111111-2222222222-3333333333-1001"\n'


_EXPECTED_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"


@pytest.mark.parametrize(("directory", "rights"), [(False, "F"), (True, "(OI)(CI)F")])
def test_windows_restrict_to_owner_issues_the_established_icacls_sequence(
    directory: bool, rights: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _windows_commands(
        monkeypatch, (0, _whoami_row()), (0, ""), (0, ""), (0, "")
    )
    target = Path("/some/object")

    assert (
        owner_private._windows_restrict_to_owner(target, directory=directory) is True
    )

    icacls = _expected_tool("icacls.exe")
    assert commands == [
        [_expected_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        [icacls, str(target), "/setowner", f"*{_EXPECTED_SID}", "/q"],
        [icacls, str(target), "/reset", "/q"],
        [
            icacls,
            str(target),
            "/inheritance:r",
            "/grant:r",
            f"*{_EXPECTED_SID}:{rights}",
            "/q",
        ],
    ]


def test_windows_restrict_to_owner_fails_closed_when_the_sid_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _windows_commands(monkeypatch, (1, _whoami_row()))
    assert (
        owner_private._windows_restrict_to_owner(Path("/x"), directory=False) is False
    )
    assert len(commands) == 1


def test_windows_restrict_to_owner_fails_closed_when_the_sid_is_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _windows_commands(monkeypatch, (0, "no sid on this line"))
    assert (
        owner_private._windows_restrict_to_owner(Path("/x"), directory=False) is False
    )
    assert len(commands) == 1


@pytest.mark.parametrize("failing_step", [0, 1, 2])
def test_windows_restrict_to_owner_short_circuits_on_the_first_failing_step(
    failing_step: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [(0, ""), (0, ""), (0, "")]
    steps[failing_step] = (5, "")
    commands = _windows_commands(monkeypatch, (0, _whoami_row()), *steps)

    assert (
        owner_private._windows_restrict_to_owner(Path("/x"), directory=False) is False
    )
    # The whoami lookup, plus every icacls step up to and including the one that
    # failed -- nothing queued after it was ever run.
    assert len(commands) == failing_step + 2


def test_windows_restrict_to_owner_fails_closed_when_a_command_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaking_run(
        arguments: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise OSError(
            f"{arguments!r} failed for /secret/path as "
            "S-1-5-21-4444444444 with api-key=sk-1234"
        )

    monkeypatch.setattr(owner_private.subprocess, "run", leaking_run)
    assert (
        owner_private._windows_restrict_to_owner(Path("/secret/path"), directory=False)
        is False
    )


@pytest.mark.parametrize(
    "exception",
    [
        subprocess.SubprocessError("boom"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte"),
        ValueError("boom"),
    ],
    ids=["subprocess-error", "unicode-error", "value-error"],
)
def test_windows_restrict_to_owner_fails_closed_on_subprocess_and_unicode_exceptions(
    exception: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising(
        arguments: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise exception

    monkeypatch.setattr(owner_private.subprocess, "run", raising)
    assert (
        owner_private._windows_restrict_to_owner(Path("/x"), directory=False) is False
    )


def test_restrict_to_owner_is_a_no_op_success_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        owner_private,
        "_windows_restrict_to_owner",
        lambda _p, *, directory: pytest.fail(
            "the native path must not run off Windows"
        ),
    )
    assert owner_private.restrict_to_owner(Path("/anywhere"), directory=False) is True


@pytest.mark.parametrize("restricted", [True, False])
@pytest.mark.parametrize("directory", [True, False])
def test_restrict_to_owner_delegates_to_the_windows_path_when_forced(
    restricted: bool, directory: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)
    seen: list[tuple[Path, bool]] = []

    def fake(path: Path, *, directory: bool) -> bool:
        seen.append((path, directory))
        return restricted

    monkeypatch.setattr(owner_private, "_windows_restrict_to_owner", fake)
    target = Path("/anywhere")
    assert owner_private.restrict_to_owner(target, directory=directory) is restricted
    assert seen == [(target, directory)]


@pytest.mark.skipif(os.name != "nt", reason="exercises the real icacls/whoami writer")
def test_the_native_windows_writer_produces_what_the_native_reader_accepts(
    tmp_path: Path,
) -> None:
    """The real writer, proved against the real native reader.

    Every writer test above doubles `subprocess.run`, because a wrong command
    either does nothing or writes a security descriptor nobody intended -- the
    reason this repair uses `icacls` rather than `ctypes` at all. This is the
    one test where nothing is doubled: it restricts a real file and a real
    directory on this host and asks the real native reader whether each one is
    now what `restrict_to_owner` claims.
    """
    target = tmp_path / "file.txt"
    target.write_text("content", encoding="utf-8")
    assert owner_private.restrict_to_owner(target, directory=False) is True
    descriptor = os.open(target, os.O_RDONLY)
    try:
        metadata = os.fstat(descriptor)
        assert owner_private.owner_private_file(metadata, descriptor) is True
    finally:
        os.close(descriptor)

    nested = tmp_path / "nested"
    nested.mkdir()
    assert owner_private.restrict_to_owner(nested, directory=True) is True
    assert owner_private.owner_private_directory(nested) is True


# --- the Windows reparse and named-directory policy ---------------------------
#
# Exercised on every platform, for the reason the ACE walk is: what is easy to
# get wrong is not the native call but the policy over what it returns. A
# junction or a mount point standing where a trusted directory belongs *is* a
# directory to `stat`, so the reparse attribute is the whole of the defence there
# -- and a directory has no descriptor to pin, so the name form is bracketed by
# an identity check instead. `FakeStat` stands in for a stat result carrying
# `st_file_attributes`, which only Windows produces.


class FakeStat:
    """A stat result with the one attribute POSIX does not report."""

    def __init__(
        self,
        *,
        mode: int,
        uid: int | None = None,
        attributes: int = 0,
        device: int = 1,
        inode: int = 1,
    ) -> None:
        self.st_mode = mode
        self.st_uid = os.getuid() if uid is None and os.name != "nt" else (uid or 0)
        self.st_file_attributes = attributes
        self.st_dev = device
        self.st_ino = inode


REPARSE = owner_private._REPARSE_POINT
DIRECTORY = 0o040700
REGULAR = 0o100600


def test_a_reparse_point_is_not_itself_whatever_kind_it_claims_to_be() -> None:
    assert owner_private.not_a_reparse_point(FakeStat(mode=DIRECTORY)) is True
    assert (
        owner_private.not_a_reparse_point(FakeStat(mode=DIRECTORY, attributes=REPARSE))
        is False
    )
    assert (
        owner_private.not_a_reparse_point(
            FakeStat(mode=REGULAR, attributes=REPARSE | 0x20)
        )
        is False
    )


def test_a_directory_that_is_a_reparse_point_is_refused_on_metadata_alone() -> None:
    """A junction is a directory to every other check; this is the one that sees it."""
    assert owner_private.owner_private_directory_metadata(FakeStat(mode=DIRECTORY))
    assert not owner_private.owner_private_directory_metadata(
        FakeStat(mode=DIRECTORY, attributes=REPARSE)
    )
    assert not owner_private.owner_private_directory_metadata(FakeStat(mode=REGULAR))


def test_a_file_that_is_a_reparse_point_is_refused_before_any_native_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kind and reparse checks come first, so no descriptor is consulted."""
    monkeypatch.setattr(
        owner_private,
        "_windows_owner_only",
        lambda _descriptor: pytest.fail("the reparse refusal must come first"),
    )
    assert not owner_private.owner_private_file(
        FakeStat(mode=REGULAR, attributes=REPARSE), 3
    )
    assert not owner_private.owner_private_file(FakeStat(mode=DIRECTORY), 3)


def test_the_named_directory_verdict_fails_closed_when_the_native_call_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its one caller is a predicate, so a refusal it cannot read is a refusal."""

    def unavailable(
        _path: Path,
    ) -> tuple[bool, tuple[tuple[int, bool, int], ...] | None]:
        raise OSError("the security descriptor could not be read")

    monkeypatch.setattr(owner_private, "_windows_acl_facts_by_name", unavailable)
    assert owner_private._windows_owner_only_directory(Path("/anywhere")) is False
    assert (
        owner_private._windows_directory_verdict(Path("/anywhere"), owner_only=False)
        is False
    )

    monkeypatch.setattr(
        owner_private,
        "_windows_acl_facts_by_name",
        lambda _p: (True, ((0, True, FULL_ACCESS),)),
    )
    assert owner_private._windows_owner_only_directory(Path("/anywhere")) is True


def test_the_named_directory_form_reads_owner_and_dacl_from_the_security_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name form and the handle form decode one set of facts the same way."""
    api = FakeSecurityApi([windows_ace(0, OWNER_SID)])
    monkeypatch.setattr(owner_private, "_SECURITY_API", api)
    assert owner_private._windows_acl_facts_by_name(Path("/anywhere")) == (
        True,
        ((0, True, FULL_ACCESS),),
    )
    assert api.freed == [0xD0D0]
    assert api.closed == [0x7070]


@pytest.mark.parametrize(
    ("before", "owner_only", "after", "accepted"),
    [
        (FakeStat(mode=DIRECTORY), True, FakeStat(mode=DIRECTORY), True),
        # A junction standing where the directory belongs, refused before the
        # security call is even made.
        (FakeStat(mode=DIRECTORY, attributes=REPARSE), True, None, False),
        # The security call says somebody else can reach it.
        (FakeStat(mode=DIRECTORY), False, FakeStat(mode=DIRECTORY), False),
        # The name was re-pointed under the security call: a different directory
        # answered it than the one this name resolves to now.
        (FakeStat(mode=DIRECTORY), True, FakeStat(mode=DIRECTORY, inode=2), False),
        # And one that stopped resolving at all.
        (FakeStat(mode=DIRECTORY), True, None, False),
    ],
    ids=["trusted", "reparse-point", "not-owner-only", "re-pointed", "vanished"],
)
def test_the_windows_named_directory_proof_is_bracketed_by_an_identity_check(
    before: FakeStat,
    owner_only: bool,
    after: FakeStat | None,
    accepted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory has no descriptor to pin, so its identity is pinned around it."""
    answers = [before, after]
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)
    monkeypatch.setattr(owner_private, "_lstat", lambda _p, _d: answers.pop(0))
    monkeypatch.setattr(
        owner_private, "_windows_owner_only_directory", lambda _p: owner_only
    )
    assert owner_private.owner_private_directory(Path("/anywhere")) is accepted


def test_a_reparse_point_ends_a_read_even_after_the_bytes_came_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-read stat of the pathname is checked for it too, not only the pre."""
    path = tmp_path / "document.json"
    path.write_bytes(b"content")
    path.chmod(0o600)
    real = owner_private._lstat
    seen: list[int] = []

    def watched(target: Path, dir_fd: int | None) -> Any:
        value = real(target, dir_fd)
        seen.append(1)
        if len(seen) > 1 and value is not None:
            return FakeStat(
                mode=value.st_mode,
                uid=value.st_uid,
                attributes=REPARSE,
                device=value.st_dev,
                inode=value.st_ino,
            )
        return value

    monkeypatch.setattr(owner_private, "_lstat", watched)
    assert owner_private.read_owner_private(path, maximum_bytes=64) is None


# --- the pathname chain, which is the Windows form of the anchored walk --------
#
# Off Windows this exercises the POSIX half of the same policy: a real directory,
# not a symlink, owned by this user, writable by nobody else on the way down and
# owner-only at the leaf. The Windows half -- the owner-and-DACL call each
# component also carries there -- is exercised below with `_IS_WINDOWS` forced
# and the native verdict doubled, because that call cannot run on this host and a
# test that pretended it could would be asserting nothing.


def layout(root: Path, *, runtime: int = 0o755, store: int = 0o700) -> Path:
    """One installation layout, ``root/runtime/.store``, with chosen modes."""
    directory = root / "runtime" / ".store"
    directory.mkdir(parents=True)
    (root / "runtime").chmod(runtime)
    directory.chmod(store)
    return directory


NAMES = ("runtime", ".store")


def test_a_proved_chain_returns_one_stat_per_component(tmp_path: Path) -> None:
    """Not a bare verdict: the caller operates and then compares these again."""
    directory = layout(tmp_path)
    proved = owner_private.owner_private_chain(tmp_path, NAMES)
    assert proved is not None and len(proved) == 3
    for metadata, path in zip(proved, (tmp_path, tmp_path / "runtime", directory)):
        assert owner_private.same_file(metadata, path.stat())


def test_an_absent_component_is_not_a_chain(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    assert owner_private.owner_private_chain(tmp_path, NAMES) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
@pytest.mark.parametrize("linked", ["runtime", ".store"], ids=["parent", "leaf"])
def test_a_symlinked_component_anywhere_in_the_chain_is_refused(
    tmp_path: Path, linked: str
) -> None:
    elsewhere = tmp_path / "elsewhere" / "runtime" / ".store"
    elsewhere.mkdir(parents=True, mode=0o700)
    layout(tmp_path)
    if linked == "runtime":
        target, replaced = tmp_path / "runtime", elsewhere.parent
        for entry in target.iterdir():
            entry.rmdir()
        target.rmdir()
    else:
        target, replaced = tmp_path / "runtime" / ".store", elsewhere
        target.rmdir()
    target.symlink_to(replaced, target_is_directory=True)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_parent_anyone_can_write_is_refused_and_one_anyone_can_read_is_not(
    tmp_path: Path,
) -> None:
    """The two policies differ in exactly one way, and this is it.

    A group- or world-*writable* parent is a directory in which anybody can
    replace `runtime` with a symlink, so it ends the walk. A merely readable one
    does not: an installation root and its `runtime/` are shared, conventionally
    moded directories, and refusing those would refuse every real installation.
    """
    layout(tmp_path, runtime=0o755)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is not None
    (tmp_path / "runtime").chmod(0o777)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_store_directory_itself_must_be_owner_only(tmp_path: Path) -> None:
    """Readable is enough for the two above it and not enough for this one."""
    layout(tmp_path, store=0o750)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is None
    (tmp_path / "runtime" / ".store").chmod(0o700)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership uses the effective uid")
def test_a_component_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout(tmp_path)
    mine = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: mine + 1)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is None


def test_only_an_absolute_root_is_walked() -> None:
    for root in (Path("relative"), "/absolute/but/a/string"):
        assert owner_private.owner_private_chain(root, NAMES) is None  # type: ignore[arg-type]


def test_the_windows_chain_asks_the_native_verdict_of_every_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three components, three security calls, and the leaf's is the strict one.

    Doubled rather than native: `GetNamedSecurityInfoW` does not exist here. What
    is asserted is the decision logic -- that every component is asked, that the
    leaf is asked with the owner-only policy and the parents with the parent one,
    and that one refusal anywhere ends the walk.
    """
    layout(tmp_path)
    asked: list[tuple[str, bool]] = []
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)

    def verdict(path: Path, *, owner_only: bool) -> bool:
        asked.append((path.name, owner_only))
        return True

    monkeypatch.setattr(owner_private, "_windows_directory_verdict", verdict)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is not None
    assert asked == [(tmp_path.name, False), ("runtime", False), (".store", True)]


@pytest.mark.parametrize("refused", [0, 1, 2], ids=["root", "runtime", "store"])
def test_one_refused_native_verdict_ends_the_windows_chain(
    tmp_path: Path, refused: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)

    def verdict(path: Path, *, owner_only: bool) -> bool:
        seen.append(path.name)
        return len(seen) - 1 != refused

    monkeypatch.setattr(owner_private, "_windows_directory_verdict", verdict)
    assert owner_private.owner_private_chain(tmp_path, NAMES) is None
    assert len(seen) == refused + 1, "the walk kept going after a refusal"


def test_the_windows_metadata_policies_are_the_kind_and_reparse_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There are no mode bits there, so this is what metadata alone can answer.

    The owner-and-DACL half of each verdict needs a security call, which
    `owner_private_chain` makes by name -- and which the two tests above cover
    with that call doubled.
    """
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)
    for policy in (
        owner_private.owner_writable_only,
        owner_private.owner_private_directory_metadata,
    ):
        assert policy(FakeStat(mode=DIRECTORY, uid=999)) is True
        assert policy(FakeStat(mode=DIRECTORY, uid=999, attributes=REPARSE)) is False
        assert policy(FakeStat(mode=REGULAR, uid=999)) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_posix_parent_policy_reads_the_write_bits_and_not_the_read_bits() -> None:
    for mode, admitted in (
        (0o040755, True),
        (0o040700, True),
        (0o040775, False),
        (0o040757, False),
        (0o040705, True),
    ):
        assert owner_private.owner_writable_only(FakeStat(mode=mode)) is admitted


# --- the shared owner-private writer ------------------------------------------


def test_a_written_document_is_owner_private_from_creation(tmp_path: Path) -> None:
    path = tmp_path / "private" / "document.json"
    assert owner_private.write_owner_private(path, b"{}\n") is True
    assert path.read_bytes() == b"{}\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [entry.name for entry in path.parent.iterdir()] == ["document.json"]


def test_a_rewrite_replaces_the_whole_document_and_leaves_no_temporary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    assert owner_private.write_owner_private(path, b"first") is True
    assert owner_private.write_owner_private(path, b"second") is True
    assert path.read_bytes() == b"second"
    assert [entry.name for entry in tmp_path.iterdir()] == ["document.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlink_at_the_destination_is_replaced_rather_than_followed(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    path = tmp_path / "document.json"
    path.symlink_to(victim)

    assert owner_private.write_owner_private(path, b"written") is True
    assert victim.read_bytes() == b"untouched"
    assert not path.is_symlink()
    assert path.read_bytes() == b"written"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_directory_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "linked").symlink_to(elsewhere, target_is_directory=True)

    assert (
        owner_private.write_owner_private(tmp_path / "linked" / "x.json", b"{}")
        is False
    )
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_group_or_world_reachable_directory_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "open"
    directory.mkdir(mode=0o755)
    assert owner_private.write_owner_private(directory / "x.json", b"{}") is False
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize(
    "path", [Path("relative/document.json"), Path("/"), "not-a-path"]
)
def test_only_an_absolute_path_naming_a_file_is_written(path: object) -> None:
    assert owner_private.write_owner_private(path, b"{}") is False  # type: ignore[arg-type]


def test_the_owner_only_proof_is_taken_before_a_byte_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform that will not give an owner-only file gets no document in it."""
    monkeypatch.setattr(owner_private, "owner_private_file", lambda _m, _d: False)
    path = tmp_path / "document.json"
    assert owner_private.write_owner_private(path, b"{}") is False
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_new_directory_is_restricted_before_its_own_proof_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows stand-in for a `mkdir` mode runs before the directory is used.

    Forced here because the native call cannot run on this host: what is
    exercised is the ordering and the fail-closed behaviour, not the Win32 call
    itself, which `test_owner_private.py`'s restriction tests cover directly.
    """
    calls: list[Path] = []

    def fake(path: Path, *, directory: bool) -> bool:
        calls.append(path)
        return True

    monkeypatch.setattr(owner_private, "restrict_to_owner", fake)
    path = tmp_path / "private" / "document.json"
    assert owner_private.write_owner_private(path, b"{}") is True
    assert calls[0] == path.parent


def test_a_directory_that_cannot_be_restricted_is_never_written_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory this call created but could not restrict is left empty.

    The same leftover a directory that fails the ordinary owner-private proof
    already produces: no document is ever written into it, whatever else is
    true of the directory itself.
    """
    monkeypatch.setattr(
        owner_private, "restrict_to_owner", lambda _p, *, directory: False
    )
    path = tmp_path / "private" / "document.json"
    assert owner_private.write_owner_private(path, b"{}") is False
    assert list((tmp_path / "private").iterdir()) == []


def test_a_temporary_file_that_cannot_be_restricted_is_removed_unwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restriction is asked of the directory and then, separately, of the file."""
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    calls: list[Path] = []

    def fake(path: Path, *, directory: bool) -> bool:
        calls.append(path)
        return directory

    monkeypatch.setattr(owner_private, "restrict_to_owner", fake)
    path = parent / "document.json"
    assert owner_private.write_owner_private(path, b"{}") is False
    assert not path.exists()
    assert list(parent.iterdir()) == []
    assert len(calls) == 1 and calls[0].suffix == ".partial"


def test_a_short_write_is_a_refusal_rather_than_a_truncated_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.write` may accept less than it was handed and raise nothing."""
    real = os.write
    monkeypatch.setattr(
        owner_private.os, "write", lambda fd, data: real(fd, data[:1]) and 0
    )
    path = tmp_path / "document.json"
    assert owner_private.write_owner_private(path, b"a much longer document") is False
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_replacement_that_fails_leaves_the_previous_document_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "document.json"
    assert owner_private.write_owner_private(path, b"first") is True

    def refuse(source: object, destination: object) -> None:
        raise OSError("the rename did not complete")

    monkeypatch.setattr(owner_private.os, "replace", refuse)
    assert owner_private.write_owner_private(path, b"second") is False
    assert path.read_bytes() == b"first"
    assert [entry.name for entry in tmp_path.iterdir()] == ["document.json"]


def test_the_writer_and_the_reader_agree_on_what_is_trustworthy(
    tmp_path: Path,
) -> None:
    """What this module writes is what this module will read back."""
    path = tmp_path / "private" / "document.json"
    assert owner_private.write_owner_private(path, b'{"format": "x"}') is True
    assert (
        owner_private.read_owner_private(path, maximum_bytes=64) == b'{"format": "x"}'
    )
    assert owner_private.owner_private_directory(path.parent) is True
