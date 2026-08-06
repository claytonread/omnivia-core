"""M2 evidence: the access the Runtime publishes the descriptor with.

Seven facts about bytes on disk that the existing suites cannot establish.

The permission assertions read the real access control of the real published file
and of the directory chain above it. "Not world readable" would pass on `0640`,
which the accepted client refuses; only the exact evidence counts. What "exact"
means is the one thing that is platform-shaped: on POSIX it is the `st_mode`, and
on Windows -- where `os.chmod` only toggles the read-only attribute and the mode
carries no information at all -- it is the set of trustees the DACL names. Each
case therefore asserts through `owner_only_evidence`, and each case starts from a
chain `open_up` has left reachable by more than its owner, because a publication
into an already-restricted chain proves only that nothing widened it.

Access is not the whole of what the client asks, so it is not the whole of what is
read back. The client refuses an object whose *owner* is not itself, and ownership
is carried by neither the mode nor the DACL, so the first case reads the owner
separately -- `st_uid` on POSIX, the owner SID on Windows.

That read is only worth making from a state it can fail in. A process owns what it
creates, so an owner assertion made against a chain this process built is green
whether the writer's `/setowner` runs or not -- which is why a green Windows row
was, until this seeding, no evidence that it ran. The first case therefore hands
the two directories the client validates to `BUILTIN\\Administrators` before
publishing, which is the shape an elevated installer leaves behind, and the owner
assertions then hold only because `/setowner` repaired them. POSIX has neither a
foreign owner a test may produce nor an ownership step in the writer to falsify;
see `leave_owned_by_another_principal`.

The umask-hostile case publishes under `umask(0o000)`. Without it the whole repair
passes on any machine whose umask happens to be restrictive and fails in the field,
because the defect was never the mode that was asked for -- it was that no mode was
asked for at all.

The no-window case proves the restriction is on the bytes *before* they are
reachable at the published path. A restriction applied after the rename would
satisfy every assertion above and still leave the descriptor world-readable for as
long as the two syscalls are apart.

The stale-temporary case is the only one that reaches the explicit restriction of
the temporary file; the created-ancestor case is the only one that reaches the
directories `publish()` builds above the two the reader validates; the
recreated-ancestor case is the only one that reaches an ancestor which existed when
the publication started and was built by the publication anyway; and the
failed-publication case is the only one that reaches what a publication leaves
behind when it does not finish, which is permanent -- a retry finds those levels
already present, and a directory this code did not create is one it will not
re-permission.

The end-to-end proof that closes M2 -- the real writer's file read and accepted by
the real P1b client -- is deliberately *not* here. It lives in
`tests/phase3/protocol/test_client_endpoint_discovery.py`, and that file's own
docstring states why. The reason used to be that `phase2-platform.yml` did not
install `omnivia_core_client`; packet section 17b.2 adds it, so the reason is now
qualification rather than installability -- this is a three-operating-system
matrix and nothing on that path is qualified beyond Ubuntu. Keep the proof there.
Nothing in this module needs that package either way. The
Windows helpers below are that constraint's other consequence -- the client already
reads a DACL and this module may not borrow it, so the oracle here is `icacls` for
the DACL and the object's own security descriptor for the owner -- neither of which
is the mechanism the writer uses to set them.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
import subprocess
from ctypes import wintypes
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.discovery import (
    DESCRIPTOR_MODE,
    DESCRIPTOR_NAME,
    RUNTIME_DIRECTORY_MODE,
    descriptor_path,
    publish,
)
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)

from omnivia_core.contracts.v1 import (
    ServiceEndpointDescriptor,
    ServiceProcessEvidence,
)

from .conftest import SERVICE_INSTANCE, WORKSPACE_ID

WORKSPACE_FORMAT_ORDINAL = "1"

WINDOWS = os.name == "nt"

#: The mode the installation layout leaves the runtime chain at under a default
#: `022` umask, and the one the accepted client refuses.
INHERITED_DIRECTORY_MODE = 0o755

#: What a temporary file left behind by a killed run carries under a permissive
#: umask, and what the rename would publish if nothing reset it.
INHERITED_DESCRIPTOR_MODE = 0o666

#: `Everyone`, numerically. Windows' side of `INHERITED_DIRECTORY_MODE`: a trustee
#: that is not this account, granted in a form the publication has to remove. Spelt
#: as a SID so no localized account name has to be resolved to seed it.
EVERYONE = "S-1-1-0"

#: `BUILTIN\\Administrators`, numerically. The owner an elevated Windows process
#: leaves on everything it creates, which is the field failure `/setowner` exists
#: for, and the one foreign owner a test can produce on demand: an owner may be set
#: only to a SID already carried by the setting process's own token, absent
#: `SeRestorePrivilege`. So a member of that group may hand an object to it, and a
#: process outside it has no foreign owner available at all.
ADMINISTRATORS = "S-1-5-32-544"

#: A SID in the string form `whoami` and `ConvertSidToStringSidW` both report it in.
SID_RE = re.compile(r"S-1-[0-9-]+")

#: `SE_FILE_OBJECT` and `OWNER_SECURITY_INFORMATION`: name a filesystem object, ask
#: for its owner and nothing else. Spelt here rather than imported -- the client
#: package has its own copies and this module may not import that package.
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def system32(program: str) -> str:
    """Absolute path to a Windows system tool, not resolved through `PATH`.

    Spelt out here rather than imported from the writer on purpose: this module is
    the writer's oracle, and an oracle that shares the writer's idea of which
    program to run tests one less thing.
    """
    return str(Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", program))


class OracleUnavailable(AssertionError):
    """A reader this module asks its questions through could not answer.

    Deliberately a different failure from "the answer was wrong". Both are red --
    an unrunnable check is not a passing one, and skipping it would leave the
    property proved by nothing, which is what adding it was for -- but a row that
    cannot tell "the owner is another principal" from "the tool did not launch"
    costs a CI round-trip to find out which, and Windows is the platform where a
    round-trip is the only instrument there is.
    """


def run_tool(argv: list[str], *, timeout: int) -> str:
    """Run one oracle and return its stdout, or fail carrying what it actually said.

    `check=True` raises a `CalledProcessError` that reports the argv and the exit
    status and **discards both output streams**. That is how the first hosted
    Windows row reported `exit status 1` and nothing else: the one line that would
    have explained it was captured and thrown away. Nothing here is sanitized --
    this is a test oracle reading its own `tmp_path`, not a service refusal that
    could quote a caller.
    """
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    if completed.returncode != 0:
        raise OracleUnavailable(
            f"{Path(argv[0]).name} exited {completed.returncode}\n"
            f"  argv:   {argv}\n"
            f"  stderr: {completed.stderr.strip()}\n"
            f"  stdout: {completed.stdout.strip()}"
        )
    return completed.stdout


def icacls(path: Path, *arguments: str) -> str:
    return run_tool([system32("icacls.exe"), str(path), *arguments], timeout=60)


@cache
def windows_account_sid() -> str:
    """This account's SID, the form the accepted client compares an owner against."""
    output = run_tool(
        [system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], timeout=15
    )
    found = SID_RE.search(output)
    if found is None:
        raise OracleUnavailable(f"no SID in whoami output: {output!r}")
    return found.group()


def windows_owner_sid(path: Path) -> str:
    """The owner SID of `path`, read from the object's own security descriptor.

    Three readers were tried and the two that failed are why this one is native.
    `icacls` prints ACEs and never the owner. `Get-Acl` prints it and does not run
    on the hosted image at all -- `The 'Get-Acl' command was found in the module
    'Microsoft.PowerShell.Security', but the module could not be loaded` -- so every
    PowerShell route inherits that image's module system, and behind it the
    execution policy and the language mode, none of which this repository controls.
    `dir /q` avoids PowerShell but prints a localized fixed-width table that
    truncates long account names, and for a directory lists its contents rather than
    itself.

    `GetNamedSecurityInfoW` asks the object. No process launch, no module, no
    locale, no parsing, and the answer arrives already in the form the accepted
    client compares against its own `TokenUser`. `ctypes` is standard library, and
    the reason it was refused for the *writer* does not carry here: a wrong call
    there silently mis-secures a real file, while a wrong call in an oracle fails a
    test loudly and secures nothing.

    `WinDLL` rather than `windll`, deliberately. `windll` is a process-wide cache,
    so the argtypes set here would be shared with every other caller in the
    interpreter -- including the accepted client, which configures this same entry
    point for its own reader.
    """
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL

    owner = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    # `GetNamedSecurityInfoW` returns a Win32 error code rather than setting one,
    # so the failure carries the code itself.
    result = advapi.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION,
        ctypes.pointer(owner),
        None,
        None,
        None,
        ctypes.pointer(descriptor),
    )
    if result != 0 or not owner:
        raise OracleUnavailable(
            f"GetNamedSecurityInfoW({path}) returned {result}, owner {owner}"
        )
    try:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(owner, ctypes.pointer(text)):
            raise OracleUnavailable(
                f"ConvertSidToStringSidW failed: {ctypes.get_last_error()}"
            )
        try:
            sid = str(text.value)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(descriptor)
    if not SID_RE.fullmatch(sid):
        raise OracleUnavailable(f"the owner of {path} did not read as a SID: {sid!r}")
    return sid


def assert_owned_by_this_account(path: Path) -> None:
    """The client refuses an object owned by anyone else, on either platform."""
    if WINDOWS:
        assert windows_owner_sid(path) == windows_account_sid(), (
            f"{path} is owned by another principal, and the client refuses it"
        )
    else:
        assert path.stat().st_uid == os.getuid(), (
            f"{path} is owned by another principal, and the client refuses it"
        )


@cache
def windows_account() -> str:
    """This process's account, as `whoami` spells it.

    The comparison target for every trustee assertion below, which is why nothing
    here has to know how this installation spells `Everyone` or
    `BUILTIN\\Administrators`.
    """
    return run_tool([system32("whoami.exe")], timeout=15).strip().casefold()


def windows_trustees(path: Path) -> frozenset[str]:
    """Every trustee `path`'s DACL names.

    `icacls` prints the object's name and then one `TRUSTEE:(rights)` entry per
    ACE, so the entries are the lines carrying `:(`. A listing this fails to parse
    yields the empty set, which no assertion here accepts -- it can produce a
    failure to look at, never a pass.
    """
    listing = icacls(path).removeprefix(str(path))
    return frozenset(
        line.strip().split(":(")[0].casefold()
        for line in listing.splitlines()
        if ":(" in line
    )


def owner_only_evidence(path: Path) -> object:
    """The exact access `path` currently grants, in this platform's terms."""
    return windows_trustees(path) if WINDOWS else mode_of(path)


def owner_only(*, directory: bool) -> object:
    """The value `owner_only_evidence` has when nothing but the owner may reach it.

    The literals are pinned here rather than at each assertion. On POSIX the
    expected value *is* the module's own constant, so a constant widened to `0o644`
    would agree with a file published at `0o644` and every case would stay green.
    Pinning at the one point every POSIX comparison passes through covers more
    sites than the six that spelt the octal inline, and covers them by
    construction.
    """
    if WINDOWS:
        return frozenset({windows_account()})
    assert (DESCRIPTOR_MODE, RUNTIME_DIRECTORY_MODE) == (0o600, 0o700), (
        "the published constants no longer name owner-only access"
    )
    return RUNTIME_DIRECTORY_MODE if directory else DESCRIPTOR_MODE


def assert_owner_only(path: Path, *, directory: bool) -> None:
    assert owner_only_evidence(path) == owner_only(directory=directory), (
        f"{path} is reachable by more than its owner"
    )


def open_up(path: Path, *, directory: bool) -> None:
    """Leave `path` reachable by more than its owner: what each case starts from."""
    if WINDOWS:
        rights = "(OI)(CI)F" if directory else "F"
        icacls(path, "/grant", f"*{EVERYONE}:{rights}", "/q")
    else:
        os.chmod(
            path, INHERITED_DIRECTORY_MODE if directory else INHERITED_DESCRIPTOR_MODE
        )


def leave_owned_by_another_principal(path: Path) -> None:
    """Leave `path` owned by a principal the accepted client refuses.

    The companion to `open_up`, and there for the same reason: an assertion is worth
    only the state it starts from. A process owns what it creates, so a publication
    into a chain this test built satisfies every ownership assertion below with the
    writer's `/setowner` deleted -- the property would be asserted and never
    exercised, which is what a green Windows row meant until this ran.

    Two things produce a refusable owner in the field. A token whose `TokenOwner` is
    `BUILTIN\\Administrators` -- the ordinary shape of an elevated administrator --
    owns everything it creates that way, and no process can arrange that from the
    inside. A chain built by some *other* principal has the same effect, and an
    installer running elevated is exactly that. Handing the directories to
    `BUILTIN\\Administrators` reproduces the second, so the case does not depend on
    which token the row happens to run under, and does not have to establish it.

    A host that cannot produce that owner fails here rather than skipping. Naming a
    new owner takes a SID the calling token already carries, so a process outside
    the Administrators group cannot make any object foreign-owned -- on such a host
    the property is genuinely not falsifiable, and that is a result to see rather
    than one to pass over. The read-back is the half that matters: `icacls` exiting
    zero and the owner having actually changed are two different facts, and only the
    second one licenses the assertions downstream.

    POSIX needs no counterpart and has none. Ownership is fixed at creation from the
    process's uid, handing a file to another uid is root's to do, and `_restrict` is
    a bare `chmod` there -- there is no POSIX ownership step for a foreign owner to
    falsify, and the mode is what every other case in this module already proves.
    """
    if not WINDOWS:
        return
    try:
        icacls(path, "/setowner", f"*{ADMINISTRATORS}", "/q")
    except OracleUnavailable as unavailable:
        raise OracleUnavailable(
            f"this host cannot give {path} an owner other than itself, so it cannot "
            f"falsify the writer's /setowner: naming {ADMINISTRATORS} as an owner "
            f"needs that SID in this process's own token.\n{unavailable}"
        ) from unavailable
    assert windows_owner_sid(path) != windows_account_sid(), (
        f"{path} is still owned by this account, so /setowner has nothing to repair"
    )


def descriptor() -> ServiceEndpointDescriptor:
    """A valid descriptor: these cases are about the file's access, not its contents."""
    return ServiceEndpointDescriptor(
        descriptor_version=API_VERSION,
        workspace_id=WORKSPACE_ID,
        service_instance_id=SERVICE_INSTANCE,
        installation_id="inst-m2",
        endpoint_uri="unix:///tmp/omnivia-m2.sock",
        protocol_version=PROTOCOL_VERSION,
        server_version=SERVER_VERSION,
        supported_api_versions=supported_api_versions(),
        supported_workspace_versions=supported_workspace_versions(
            WORKSPACE_FORMAT_ORDINAL
        ),
        workspace_format_version=workspace_contract_version(WORKSPACE_FORMAT_ORDINAL),
        ready=True,
        lifecycle_state="ready",
        fencing_generation=2,
        published_at="2026-08-04T00:00:00Z",
        process=ServiceProcessEvidence(
            pid=os.getpid(), start_time="100", boot_id="boot-a"
        ),
    )


def inherited_chain(tmp_path: Path) -> Path:
    """A runtime chain that already exists and is already open, as the layout leaves it.

    `InstallationLayout.create` runs long before publication and creates these
    directories with whatever the umask or the parent's DACL hands down. Publishing
    into a chain this test created itself and restricted itself would prove only
    that `mkdir` was not consulted.
    """
    runtime = tmp_path / "installation-state" / "runtime" / WORKSPACE_ID
    runtime.mkdir(parents=True)
    open_up(runtime.parent, directory=True)
    open_up(runtime, directory=True)
    return runtime


def test_the_descriptor_and_its_directory_chain_are_owner_only(tmp_path: Path) -> None:
    """Exact evidence, on the file and on both directories the client validates.

    Ownership is asserted here and only here. It is set by the same call for every
    object a publication touches, so one case covering the three the client
    validates proves the property, and repeating it in the other six would assert
    the same call six more times. The reason is redundancy rather than cost: the
    owner read is now a native call rather than a process launch, and cheap.

    The two directories start owned by another principal, and that is the whole of
    what makes those assertions evidence rather than decoration -- see
    `leave_owned_by_another_principal`. The published file is not seeded the same
    way and cannot be: it does not exist until `publish` creates it, and a created
    object's owner comes from the token rather than from its directory. So on a row
    whose token owns what it creates, the file's assertion is the weakest of the
    three and the directories are what carries the falsification. They are enough --
    the writer sets the owner of every object it touches from one call site, so an
    owner left unrepaired on a directory is the same defect as one left on the file.
    """
    runtime = inherited_chain(tmp_path)
    for existing in (runtime.parent, runtime):
        leave_owned_by_another_principal(existing)
    assert owner_only_evidence(runtime) != owner_only(directory=True), (
        "the chain starts open"
    )
    assert (DESCRIPTOR_MODE, RUNTIME_DIRECTORY_MODE) == (0o600, 0o700), (
        "the POSIX evidence is the exact mode, not merely a narrower one"
    )

    target = publish(runtime, descriptor())

    for published, directory in (
        (target, False),
        (runtime, True),
        (runtime.parent, True),
    ):
        assert_owner_only(published, directory=directory)
        assert_owned_by_this_account(published)
    # And nothing else is left behind at any mode: the temporary file is gone.
    assert [entry.name for entry in runtime.iterdir()] == ["service.json"]


def test_a_permissive_umask_cannot_widen_the_publication(tmp_path: Path) -> None:
    """`umask(0o000)` is the field condition the developer machine hides.

    Both cases are covered: a chain that already exists and is already open, and one
    this publication creates itself while nothing at all is being masked out.

    On Windows the umask is inert -- it reaches nothing a DACL is made of -- and the
    case that carries there is the second chain, which inherits its access from a
    parent this publication did not create.
    """
    existing = inherited_chain(tmp_path)
    fresh = tmp_path / "fresh-state" / "runtime" / WORKSPACE_ID

    previous = os.umask(0o000)
    try:
        existing_target = publish(existing, descriptor())
        fresh_target = publish(fresh, descriptor())
    finally:
        os.umask(previous)

    for target in (existing_target, fresh_target):
        assert_owner_only(target, directory=False)
        assert_owner_only(target.parent, directory=True)
        assert_owner_only(target.parent.parent, directory=True)


def test_the_descriptor_is_never_observable_at_a_wider_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restriction is on the bytes before the rename that publishes them.

    `replace()` carries the temporary file's access to the target -- its mode on
    POSIX, its security descriptor on Windows -- so what the rename is holding *is*
    the only access the published path ever has. Observing it at the instant of the
    rename is therefore the whole of the question.

    Observing it on a *stale* temporary file is what makes the observation bite. A
    temporary file this publication creates gets `0o600` from `os.open`'s own mode
    argument, so it reads owner-only at the rename whether the restriction ran
    before it, after it, or never -- which left the ordering unpinned while
    appearing to prove it. `O_CREAT` does not reset the mode of a file that already
    exists and `O_TRUNC` replaces its contents rather than its permissions, so a
    stale one still carries `0o666` at the rename unless the restriction reached it
    first. On Windows the same case is the only one available at all: `os.open`'s
    mode buys nothing there, and the DACL is whatever the file already had.
    """
    runtime = inherited_chain(tmp_path)
    target = descriptor_path(runtime)
    stale = runtime / f".{DESCRIPTOR_NAME}.{os.getpid()}.tmp"
    stale.write_text("{}", encoding="utf-8")
    open_up(stale, directory=False)
    observed: dict[str, object] = {}
    original_replace = Path.replace

    def recording_replace(self: Path, other: Any) -> Path:
        observed["renamed"] = owner_only_evidence(self)
        observed["target_existed"] = Path(other).exists()
        observed["directory"] = owner_only_evidence(self.parent)
        return original_replace(self, other)

    monkeypatch.setattr(Path, "replace", recording_replace)

    previous = os.umask(0o000)
    try:
        publish(runtime, descriptor())
    finally:
        os.umask(previous)

    assert observed["renamed"] == owner_only(directory=False), (
        "the rename carried wider access"
    )
    assert observed["target_existed"] is False, (
        "something was published before the mode"
    )
    assert observed["directory"] == owner_only(directory=True), (
        "the file was reachable before the mode"
    )
    assert_owner_only(target, directory=False)


def test_a_stale_temporary_file_cannot_publish_its_own_mode(tmp_path: Path) -> None:
    """The one case the explicit restriction of the temporary file is there for.

    A umask cannot reach this line: it only ever clears bits, so it can never widen
    an `O_CREAT` of `0o600`, and every mode case above passes with the restriction
    deleted. A temporary file that already exists can: `O_CREAT` does not reset the
    mode of one and `O_TRUNC` replaces its contents, not its permissions. On Windows
    it is the file's own DACL that survives, for the same reason and with the same
    consequence -- the rename publishes whatever the stale file was carrying.

    The scenario is reachable rather than theoretical. The temporary name carries
    the publishing process's pid, so a run killed between the open and the rename
    leaves one behind at exactly the path the next process given that pid writes to.
    """
    runtime = inherited_chain(tmp_path)
    stale = runtime / f".{DESCRIPTOR_NAME}.{os.getpid()}.tmp"
    stale.write_text("{}", encoding="utf-8")
    open_up(stale, directory=False)

    target = publish(runtime, descriptor())

    assert_owner_only(target, directory=False)


def test_every_directory_the_publication_creates_is_owner_only(tmp_path: Path) -> None:
    """`mkdir(parents=True)` builds ancestors at whatever the platform hands down.

    Publishing into a not-yet-existing installation root under a permissive umask
    left that root at `0o777`. Nothing refuses it -- the client opens the
    installation root as a trusted root and checks only that it is a directory --
    but a world-writable root lets any local user rename the validated `runtime`
    directory out from under the chain, which defeats every mode below it.

    The pre-existing ancestor is asserted too, in the other direction: a directory
    this call did not create is not this call's to re-permission.
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    open_up(outer, directory=True)
    opened = owner_only_evidence(outer)
    root = outer / "installation-state"
    runtime = root / "runtime" / WORKSPACE_ID

    previous = os.umask(0o000)
    try:
        publish(runtime, descriptor())
    finally:
        os.umask(previous)

    for created in (root, root / "runtime", runtime):
        assert_owner_only(created, directory=True)
    assert owner_only_evidence(outer) == opened, (
        "a pre-existing ancestor was re-permissioned"
    )


def test_an_ancestor_rebuilt_by_the_publication_is_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ancestor that is present when publication starts and absent when it builds.

    Deciding which ancestors to restrict by scanning for missing ones and then
    calling `mkdir(parents=True)` leaves a window between the two calls. An ancestor
    seen as present, removed, and then rebuilt by that `mkdir` was created by this
    publication and is in nobody's scanned set, so it keeps whatever the umask or
    the parent's DACL gave it -- which under `umask(0o000)` is `0o777`.

    The window is opened here rather than waited for: the first `mkdir` of the
    publication removes the installation root, which is exactly where a scan's
    answer about it goes stale. Any implementation that decides createdness before
    it creates leaves that root open; one that decides it from the creating call
    cannot, because the call that rebuilt it is the call that reports it.
    """
    root = tmp_path / "installation-state"
    root.mkdir()
    open_up(root, directory=True)
    runtime = root / "runtime" / WORKSPACE_ID
    original_mkdir = Path.mkdir
    removed = False

    def racing_mkdir(self: Path, *arguments: Any, **keywords: Any) -> None:
        nonlocal removed
        if not removed:
            removed = True
            root.rmdir()
        original_mkdir(self, *arguments, **keywords)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    previous = os.umask(0o000)
    try:
        publish(runtime, descriptor())
    finally:
        os.umask(previous)

    assert removed, "the publication never called mkdir, so the window never opened"
    assert_owner_only(root, directory=True)
    assert_owner_only(runtime, directory=True)


def test_a_publication_that_fails_partway_leaves_nothing_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a failed publication leaves behind is permanent, so it has to be closed.

    Restricting the created chain only after the whole of it exists means a failure
    partway leaves the levels already built at the umask-derived mode -- `0o777`
    here -- and nothing ever repairs them. The retry does not: those directories are
    now pre-existing ancestors, and this call does not re-permission what it did not
    create. That is the correct rule and it is what makes the leftover permanent, so
    the restriction has to happen as each level is created rather than at the end.
    """
    root = tmp_path / "installation-state"
    runtime = root / "runtime" / WORKSPACE_ID
    original_mkdir = Path.mkdir

    def refusing_mkdir(self: Path, *arguments: Any, **keywords: Any) -> None:
        if self.name == WORKSPACE_ID:
            raise PermissionError(13, "refused")
        original_mkdir(self, *arguments, **keywords)

    monkeypatch.setattr(Path, "mkdir", refusing_mkdir)

    previous = os.umask(0o000)
    try:
        with pytest.raises(OSError):
            publish(runtime, descriptor())
    finally:
        os.umask(previous)

    assert root.is_dir(), "the publication failed before it built anything"
    for created in (root, root / "runtime"):
        assert_owner_only(created, directory=True)
