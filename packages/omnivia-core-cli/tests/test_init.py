"""`omnivia init`, driven as real processes (R004-10, R004-11, Packet B).

The five acceptance items owner resolution 004 §11 lists for `omnivia init` split
in two. The three that are claims about the bootstrap itself -- the substrate is
created, repeating is idempotent, existing content is not overwritten -- are proved
as function calls in
`packages/omnivia-core-runtime/tests/phase3/runtime/test_workspace_init.py`. The
two that are claims about *processes* are here, because neither can be shown any
other way:

- **A fresh default home becomes startable.** `test_a_fresh_home_is_initialised_and_then_starts`
  runs `omnivia init` and then `omnivia start` on a directory that did not exist,
  as the two commands a user would type, and requires a real service to come up
  writable-ready on it. Every other test in this repository that starts a service
  builds its workspace with private Python API first -- `_bootstrap()` in
  `test_lifecycle.py` says so in its own docstring -- so this is the only test that
  proves the shipped commands are sufficient on their own.
- **Missing-workspace startup fails with an actionable message and no persistent
  mutation.** §11 words that against MCP, which does not exist yet;
  `test_start_on_an_uninitialised_home_refuses_and_creates_nothing` proves the
  equivalent for the adapter that does.

`ps -eww` is not decoration. `ps` truncates each line to the terminal width, which
is 80 columns when nothing owns a tty, and the argv searched for here is far longer
than that -- without it these tests pass locally and fail in CI having found no
process for a service that started perfectly well.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

#: Kept short deliberately. R004-15 caps a local endpoint at 86 encoded bytes --
#: what `sockaddr_un`'s 104-byte `sun_path` leaves once the NUL terminator and the
#: runtime's fixed-width staging name are taken -- so pytest's own `tmp_path` is
#: too deep to serve from.
HOME_PREFIX = "ovi-"


def _cli(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnivia_core_cli.main", "--home", str(home), *arguments],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


def _service_pids(home: Path) -> list[int]:
    """Every live `omnivia-core-service` serving this installation, by its own argv.

    Scoped to the home directory rather than to the executable name, so a service
    another test or another checkout left running cannot be counted or killed.
    """
    listing = subprocess.run(
        ["ps", "-eww", "-o", "pid=,args="], capture_output=True, text=True, check=False
    ).stdout
    marker = str(home / "installation-state")
    found = []
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(" ")
        if not pid.isdigit() or marker not in args:
            continue
        if "omnivia-core-service" not in args or "--managed-start" in args:
            continue
        if "--init" in args:
            # An `init` run is not a service and never becomes one, but it does run
            # the same executable against the same installation.
            continue
        found.append(int(pid))
    return found


def _tree(root: Path) -> list[str]:
    """Every path under `root`, as a sorted list of relative names."""
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@pytest.fixture
def home() -> Iterator[Path]:
    """An installation root that does **not** exist yet.

    The opposite of `test_lifecycle.py`'s fixture, and that is the point of this
    file: nothing here is bootstrapped in advance, because what is under test is
    whether the shipped commands can bootstrap it.
    """
    parent = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))
    root = parent / "h"
    try:
        yield root
    finally:
        for pid in _service_pids(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        shutil.rmtree(parent, ignore_errors=True)


def test_a_fresh_home_is_initialised_and_then_starts(home: Path) -> None:
    """§11: a fresh default home becomes startable. The whole point of Packet B.

    Two commands, in the order a user would type them, against a directory that
    does not exist -- and a real service answering `core.readiness` writable-ready
    at the end of it. Before this, `omnivia start` on a fresh machine had nothing
    to start and no shipped command could give it one.
    """
    initialised = _cli(home, "init")
    assert initialised.returncode == 0, initialised.stderr
    assert "initialised" in initialised.stdout
    # `init` establishes state; `start` establishes the process. Nothing yet.
    assert _service_pids(home) == []

    started = _cli(home, "start")
    assert started.returncode == 0, started.stderr + started.stdout
    assert "started" in started.stdout
    assert "writable: yes" in started.stdout
    assert len(_service_pids(home)) == 1

    status = _cli(home, "status")
    assert status.returncode == 0, status.stderr
    assert "running" in status.stdout

    stopped = _cli(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert _service_pids(home) == []


def test_repeating_init_is_safe_and_says_so(home: Path) -> None:
    """§11: repeating init is safe and idempotent, through the shipped command.

    The identity is compared across the two runs. A second run that minted a fresh
    `workspace_id` would leave a workspace whose database state row, runtime
    directory and manifest disagreed, and the exit code alone would not show it.
    """
    first = _cli(home, "init")
    assert first.returncode == 0, first.stderr

    second = _cli(home, "init")
    assert second.returncode == 0, second.stderr
    assert "already initialised" in second.stdout

    def workspace_line(output: str) -> str:
        return next(
            line for line in output.splitlines() if line.startswith("workspace: ")
        )

    assert workspace_line(second.stdout) == workspace_line(first.stdout)

    # And it is still startable after the repeat, which is the property that would
    # break if the second run had half-rewritten anything.
    started = _cli(home, "start")
    assert started.returncode == 0, started.stderr + started.stdout
    assert _cli(home, "stop").returncode == 0


def test_start_on_an_uninitialised_home_refuses_and_creates_nothing(
    home: Path,
) -> None:
    """§11's missing-workspace item, for the adapter that exists today.

    Three things, and the third is the one that is easy to lose: it refuses, the
    refusal names the command that fixes it, and nothing whatsoever is left on
    disk. A `start` that created its run directory or an installation-state tree on
    the way to failing would leave a half-installation behind on every mistyped
    `--home`.
    """
    home.mkdir(parents=True)
    before = _tree(home)

    result = _cli(home, "start")

    assert result.returncode == 1
    assert "omnivia init" in result.stderr
    assert _tree(home) == before == []
    assert _service_pids(home) == []


def test_init_refuses_an_unrelated_directory_through_the_shipped_command(
    home: Path,
) -> None:
    """R004-10's second refusal, end to end and with the file still there.

    The runtime suite proves the refusal; this proves it survives the trip through
    the CLI, the subprocess boundary and the result document, and arrives as a
    non-zero exit with the reason on stderr rather than as an empty failure.
    """
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "taxes.txt").write_text("2025 return", encoding="utf-8")

    result = _cli(home, "init")

    assert result.returncode == 1
    assert "taxes.txt" in result.stderr
    assert (workspace / "taxes.txt").read_text(encoding="utf-8") == "2025 return"
    assert not (workspace / "workspace.json").exists()


def test_the_zero_argument_home_is_the_fixed_convention(tmp_path: Path) -> None:
    """R004-11, and the reason `omnivia init` needs no flags at all.

    The paths are derived, never touched: this asserts the convention itself, and
    creating anything under a real `~/.omnivia` from a test would be initialising a
    workspace on the machine running it. An explicit `--home` still wins, which is
    the other half of R004-11 and the half every other test in this file relies on.

    The environment is deliberately absent from both branches, and
    `test_workspace_show.py::test_the_cli_reads_no_environment_variable_to_find_a_service`
    is what keeps it that way.
    """
    from omnivia_core_cli.lifecycle import (
        DEFAULT_HOME_DIRECTORY,
        Installation,
        home_directory,
    )

    assert home_directory() == Path.home() / DEFAULT_HOME_DIRECTORY
    assert DEFAULT_HOME_DIRECTORY == ".omnivia"

    default = Installation(home_directory())
    assert default.workspace_root == Path.home() / ".omnivia" / "workspace"
    assert default.installation_state == Path.home() / ".omnivia" / "installation-state"
    assert not default.workspace_root.exists() or default.workspace_root.is_dir()

    assert home_directory(tmp_path) == tmp_path


#: The one claim in this packet a source scan cannot finish, run as a program.
#:
#: `test_workspace_show.py`'s AST guards read the CLI tree for literal `import
#: sqlite3` and four call names, and a dynamic import walks past them in one line:
#: `importlib.import_module("sql" + "ite3").connect(...)` passed this suite at its
#: exact baseline and left an 8 KB database on disk. No static scan closes that --
#: the module name is a value, and a value can be computed.
#:
#: So the property is checked at runtime instead, by the interpreter rather than by
#: a reader of the source. `sqlite3.connect` is a CPython audit event raised inside
#: `Modules/_sqlite/`, so it fires whatever route reached *that module*: literal
#: import, computed import, `__import__`, or an `exec` of a string assembled at
#: runtime. The hook is installed before `omnivia_core_cli` is imported, so nothing
#: the CLI does afterwards is outside it.
#:
#: **What this cannot see, stated so the guard is not read as more than it is.**
#:
#: Only this process. `init` and `start` launch `omnivia-core-service`, which opens
#: the database as its whole job, and audit hooks are not inherited across `exec`.
#: That is the division being asserted, not a gap.
#:
#: Only the commands run below, so a database opened by a code path none of them
#: reach is not covered.
#:
#: **And only the stdlib `sqlite3` module -- not "only SQLite", which is what this
#: paragraph used to say and is not true.** The event is raised by CPython's
#: `_sqlite3`, not by libsqlite3, so any route that reaches the C library without
#: going through that module raises nothing at all:
#: `ctypes.CDLL(ctypes.util.find_library("sqlite3")).sqlite3_open(...)` created an
#: 8192-byte database with zero `sqlite3.*` events and left this suite at its exact
#: baseline.
#:
#: The reachable half of that is now watched, and not by looking for the word
#: "sqlite" in a library name -- which would be the same defeated-by-a-value scan
#: one layer down. Loading a shared library is itself audited, so what the hook
#: records is *any* named `ctypes.dlopen`, whatever the library. The unnamed one is
#: allowed through because `import ctypes` performs it on itself:
#: `ctypes/__init__.py` runs `pythonapi = PyDLL(None)`, and
#: `omnivia_core_client.discovery` imports ctypes for the peer-credential syscall.
#: `arguments[0] is None` is exactly that case and nothing else.
#: `test_the_audit_hook_sees_the_route_that_walks_past_the_source_scan` runs the
#: bypass through this same hook and asserts it is caught, so the watch is not
#: itself taken on trust.
#:
#: What stays outside: a compiled extension module linked against libsqlite3 at
#: build time calls it with no `dlopen` and no `sqlite3.*` event, and neither a hook
#: nor a static scan closes that. It is a bound on the claim, not a to-do. A CLI
#: that opened a Postgres socket is outside all of this and is caught, if at all, by
#: the AST scan's import check.
AUDIT_HOOK = '''
import json
import sys

observed = []


def watch(event, arguments):
    """Record every SQLite connection and every forbidden import, and nothing else."""
    if event.startswith("sqlite3.") or event == "ctypes.dlsym":
        observed.append(event)
    elif event == "ctypes.dlopen" and arguments and arguments[0] is not None:
        # Named libraries only. `import ctypes` dlopens the running interpreter
        # with no name, and the CLI reaches that through `omnivia_core_client`.
        observed.append("ctypes.dlopen " + str(arguments[0]))
    elif event == "import" and arguments and str(arguments[0]).split(".")[0] in (
        "sqlite3",
        "omnivia_core_runtime",
    ):
        observed.append("import " + str(arguments[0]))


sys.addaudithook(watch)
'''

AUDIT_PROBE = AUDIT_HOOK + '''
from omnivia_core_cli.main import main

home, report = sys.argv[1], sys.argv[2]
codes = {}
for command in ("init", "start", "status", "stop"):
    codes[command] = main(["--home", home, command])

with open(report, "w", encoding="utf-8") as handle:
    json.dump({"codes": codes, "observed": observed}, handle)
'''

#: The bypass, run through the *same* hook source so the guard above is falsified
#: rather than assumed. See `AUDIT_PROBE`.
AUDIT_BYPASS_PROBE = AUDIT_HOOK + '''
import ctypes
import ctypes.util
import os

database, report = sys.argv[1], sys.argv[2]
library = ctypes.util.find_library("sqlite3")
handle = ctypes.c_void_p()
opened = ctypes.CDLL(library)
opened.sqlite3_open(database.encode(), ctypes.byref(handle))
opened.sqlite3_exec(handle, b"CREATE TABLE t (id INTEGER PRIMARY KEY)", None, None, None)
opened.sqlite3_close(handle)

with open(report, "w", encoding="utf-8") as handle:
    json.dump({"observed": observed, "size": os.path.getsize(database)}, handle)
'''

#: The bypass the guard *allows*, and the one only `ctypes.dlsym` can see.
#:
#: `AUDIT_BYPASS_PROBE` loads libsqlite3 **by name**, so it raises a named
#: `ctypes.dlopen` and a `ctypes.dlsym` both -- which is why deleting the `dlsym`
#: clause from `AUDIT_HOOK` left every audit test green. The test above pins the
#: redundant clause.
#:
#: This probe removes the dlopen. `ctypes.CDLL(None)` is the *unnamed* load the hook
#: lets through on purpose, because `import ctypes` performs one on the running
#: interpreter and `omnivia_core_client.discovery` reaches it -- so `arguments[0] is
#: None` cannot be treated as suspicious. Symbols then resolve through `RTLD_DEFAULT`
#: and reach whatever is already in the process, with no named library anywhere.
AUDIT_UNNAMED_PROBE = AUDIT_HOOK + '''
import ctypes
import os

database, report = sys.argv[1], sys.argv[2]
library = ctypes.CDLL(None)

# Resolvable on every platform this ships to. Resolving *anything* through the
# allowed unnamed handle raises `ctypes.dlsym`, which is the clause under test.
library.getpid

try:
    opener = library.sqlite3_open
except AttributeError:
    available = False
else:
    available = True
    connection = ctypes.c_void_p()
    opener(database.encode(), ctypes.byref(connection))
    library.sqlite3_exec(
        connection, b"CREATE TABLE t (id INTEGER PRIMARY KEY)", None, None, None
    )
    library.sqlite3_close(connection)

with open(report, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "observed": observed,
            "available": available,
            "size": os.path.getsize(database) if available else 0,
        },
        handle,
    )
'''


def test_no_command_opens_a_database_or_imports_the_runtime_in_this_process(
    home: Path,
) -> None:
    """ADR-036's boundary, proved by running rather than by reading. See `AUDIT_PROBE`.

    The four commands are run in one audited interpreter because the boundary is a
    claim about the CLI, not about `init`: the mutation that motivated this went into
    `request_init`, but `start`, `status` and `stop` are the same package and the same
    rule. Their exit codes are asserted too -- an audit trace from four commands that
    all failed early would be a clean bill of health for a process that did nothing.
    """
    workspace = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))
    probe = workspace / "audited.py"
    report = workspace / "audit.json"
    probe.write_text(AUDIT_PROBE, encoding="utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, str(probe), str(home), str(report)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        audited = json.loads(report.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # The commands really ran and really succeeded, so the empty trace below is a
    # fact about a working CLI rather than about one that refused four times.
    assert audited["codes"] == {"init": 0, "start": 0, "status": 0, "stop": 0}
    assert audited["observed"] == [], audited["observed"]


def test_the_audit_hook_sees_the_route_that_walks_past_the_source_scan() -> None:
    """The guard, falsified rather than trusted. See `AUDIT_PROBE`.

    `sqlite3.connect` is raised by CPython's `_sqlite3`, not by libsqlite3, so
    `ctypes.CDLL(...).sqlite3_open()` opens a real database and raises no
    `sqlite3.*` event at all -- it did exactly that, leaving 8192 bytes on disk
    while this suite stayed at its exact baseline. The docstring above used to say
    the guard's third limit was "only SQLite", which reads as all SQLite access
    being covered.

    This runs that bypass through the same `AUDIT_HOOK` source the real probe
    installs, so what is asserted is the hook the CLI is judged by and not a copy of
    it. Without it, watching `ctypes.dlopen` would be one more guard that cannot see
    the thing it guards.

    The database's size is asserted too: a bypass that failed to open anything would
    raise no event either, and would be a clean bill of health for nothing happening.
    """
    workspace = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))
    probe = workspace / "bypass.py"
    report = workspace / "audit.json"
    probe.write_text(AUDIT_BYPASS_PROBE, encoding="utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, str(probe), str(workspace / "bypass.sqlite"), str(report)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        audited = json.loads(report.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    assert audited["size"] > 0, "the bypass has to really open a database"
    # Not one `sqlite3.*` event among them -- which is the limit being stated.
    assert [event for event in audited["observed"] if event.startswith("sqlite3.")] == []
    assert [
        event for event in audited["observed"] if event.startswith("ctypes.dlopen ")
    ], audited["observed"]


def test_the_audit_hook_sees_the_bypass_that_loads_no_named_library() -> None:
    """The clause that does the work, falsified. See `AUDIT_UNNAMED_PROBE`.

    The test above asserts only that a named `ctypes.dlopen` was recorded, and the
    bypass it drives raises **both** events -- so deleting `or event ==
    "ctypes.dlsym"` from `AUDIT_HOOK` left every audit test in this file green. The
    redundant clause was pinned and the load-bearing one was not.

    `ctypes.dlsym` is the load-bearing one because `ctypes.CDLL(None)` is a route the
    hook *deliberately allows*: `import ctypes` dlopens the running interpreter with
    no name and `omnivia_core_client.discovery` reaches it, so `arguments[0] is None`
    cannot be treated as suspicious. Symbols resolved through that handle go via
    `RTLD_DEFAULT` and reach anything already in the process. On macOS that includes
    libsqlite3: this route created an 8192-byte database with no named dlopen and not
    one `sqlite3.*` event. Nothing but `ctypes.dlsym` sees it.

    **The database half is platform-dependent, and the assertions are split so this
    says so rather than pretending otherwise.** Linux resolves `RTLD_DEFAULT` against
    objects already loaded, so `sqlite3_open` is not findable there unless something
    pulled libsqlite3 in first -- and CI is ubuntu-latest, where this may well report
    `available: false` and assert nothing about a database at all. The `dlsym`
    assertion is unconditional and portable, because resolving *any* symbol through
    the allowed handle raises it, and that is what falsifies the clause. The database
    assertion is the consequence, and it runs where the platform permits.
    """
    workspace = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))
    probe = workspace / "unnamed.py"
    report = workspace / "audit.json"
    probe.write_text(AUDIT_UNNAMED_PROBE, encoding="utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, str(probe), str(workspace / "unnamed.sqlite"), str(report)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        audited = json.loads(report.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # No named library was loaded, so the clause the other test asserts is blind
    # here -- this is the route that walks past it.
    assert [
        event for event in audited["observed"] if event.startswith("ctypes.dlopen ")
    ] == [], audited["observed"]
    assert [event for event in audited["observed"] if event.startswith("sqlite3.")] == []
    # The whole of the guard on this route, on every platform.
    assert "ctypes.dlsym" in audited["observed"], audited["observed"]
    if audited["available"]:
        assert audited["size"] > 0, "a resolvable sqlite3_open has to open a database"


def test_the_init_result_document_is_the_whole_of_the_services_stdout(
    home: Path,
) -> None:
    """The output contract R004-10 shares with managed start.

    The service's stdout is one JSON document and nothing else, so an adapter can
    read it without a parser that skips prose. This runs the service mode directly
    -- the same way the CLI does -- because the CLI's own stdout is deliberately
    human text, and the machine-readable half is what an MCP adapter would consume.
    """
    executable = shutil.which("omnivia-core-service") or str(
        Path(sys.executable).parent / "omnivia-core-service"
    )
    if not os.access(executable, os.X_OK):
        pytest.skip("omnivia-core-service is not installed in this environment")

    completed = subprocess.run(
        [
            executable,
            "--init",
            "--workspace",
            str(home / "workspace"),
            "--installation-state",
            str(home / "installation-state"),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["status"] == "initialised"
    assert document["refusal"] is None
    assert document["workspace"]["workspace_id"].startswith("ws-")
    # No endpoint was passed and none was invented: `init` binds nothing.
    assert "endpoint" not in json.dumps(document)
