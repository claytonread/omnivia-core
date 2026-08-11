"""Permanent guards for the P1b local-Windows-VM qualification lane.

Two artefacts are protected here: the producer
`scripts/run-p1b-windows-qualification.ps1`, which runs the discovery suite in
native Windows PowerShell on a local Windows VM -- Parallels Desktop, or another
provider the checker recognizes -- and the checker
`scripts/check-p1b-windows-qualification.py`, which builds the evidence there
and verifies it offline anywhere.

The lane is hypervisor-neutral by construction, and the guards below hold it
that way from both ends: the producer collects generic CIM identity strings and
a generic guest-tools signal without naming an acceptable hypervisor, and the
checker decides which providers those strings may name. So a guard that passes
only because the guest happened to be one vendor's is a guard this file is
written to catch.

Neither artefact touches GitHub Actions. There is no workflow, no hosted runner
and nothing that bills: the guards below assert that as a property of the runner
rather than leaving it to a reviewer to notice.

The runner guards are written as `_assert_*` functions over the script's *text*,
so the same function proves the real file correct and proves a deliberately
corrupted copy of it wrong. A guard that has only ever been run against a passing
input is not evidence that it fails.

The checker guards work the same way one level down. A synthetic JUnit record in
the qualifying shape -- 79 cases, 64 passed, the 15 allowlisted skips, the three
native Windows cases -- is the green baseline, and every corruption below mutates
exactly one normalized input and requires a finding that names it. The integrity
digest is re-stamped after each mutation on purpose: without that, every mutation
would be caught by the digest alone and no individual check would be proven.

That baseline is not green on its own, and deliberately so. A synthetic record
reproduces every count this lane pins, so counts alone cannot separate a real
Windows guest from this file; the `windows_vm` fixture supplies the host context
the checker also requires. Only the host context is simulated -- counts, node
ids, code identity, package inventory and every digest stay real.

What these tests deliberately do not do: run the discovery suite, or assert
anything about a Windows result. This file runs on whatever platform collects
it, and the qualifying result is a property of a real run on the guest.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "run-p1b-windows-qualification.ps1"
CHECKER = REPO_ROOT / "scripts" / "check-p1b-windows-qualification.py"
ALIGNMENT = REPO_ROOT / "scripts" / "check-import-install-alignment.py"

DISCOVERY_TEST = "packages/omnivia-core-client/tests/test_discovery.py"
DISCOVERY_CONFTEST = "packages/omnivia-core-client/tests/conftest.py"

#: The challenge a verifier hands the producer, and one it did not. Both are 64
#: lowercase hex characters, which is what the checker and the runner accept.
CHALLENGE = "9f2c1b7e" * 8
OTHER_CHALLENGE = "1a3d5f70" * 8

#: Every way a pytest run can be narrowed. The qualification depends on the whole
#: file being collected, so a green summary from a filtered run proves nothing.
FILTERS = ("-k", "-m", "--deselect", "--ignore", "--exitfirst", "-x", "--last-failed")

#: Anything that would make this lane depend on GitHub, a hosted runner or
#: someone's billing account. None of it may appear in an active line.
HOSTED_CI_MARKERS = ("github", "windows-latest", "runner.temp", "uses:", "actions/")

#: Anything that would fetch something the install list did not name. The whole
#: point of pinning `$InstallCommands` is that the qualifying environment is
#: those commands and the interpreter that was already on the guest.
DOWNLOAD_MARKERS = (
    "invoke-webrequest",
    "invoke-restmethod",
    "start-bitstransfer",
    "iwr ",
    "curl",
    "wget",
    "winget",
    "choco",
    "nuget",
    "--upgrade",
)

#: The install list, in order. `omnivia-core` is unpublished and
#: `omnivia-core-client` depends on it, so the client installed first sends pip
#: to an index that does not have the dependency.
#:
#: Written as whole command lines and split, rather than as tuples of tokens.
#: `tests/compatibility/test_root_facade_distribution.py` fails any collected
#: test module carrying a literal sequence with `pip` and `install` in it and no
#: `--no-index`, and it is right to: a test that reaches an index is network work
#: nobody asked for. This file runs no install at all -- these are the expected
#: *contents of another file*, compared against its text -- so the shape is
#: changed rather than the guard weakened or a flag added that would make the
#: assertion describe a runner that does not exist.
EXPECTED_INSTALL_COMMANDS = tuple(
    tuple(line.split())
    for line in (
        "-m pip install pytest",
        "-m pip install -e .",
        "-m pip install -e packages/omnivia-core-client",
    )
)

#: Any full commit SHA written into an active line, which would freeze this lane
#: to one commit. The header's `.EXAMPLE` block carries one on purpose, which is
#: why the guard reads active lines only.
FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")


def _load(path: Path, name: str) -> ModuleType:
    """A `scripts/*.py` file as a module.

    Their filenames are not valid Python identifiers, so they cannot be
    imported; this is the same loader `tests/test_import_install_alignment.py`
    uses.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load(CHECKER, "check_p1b_windows_qualification")
alignment = _load(ALIGNMENT, "check_import_install_alignment_for_p1b")

ARTIFACT_DIR = checker.ARTIFACT_DIR
SANITIZED_JUNIT = f"{ARTIFACT_DIR}/{checker.SANITIZED_JUNIT_NAME}"
EVIDENCE_JSON = f"{ARTIFACT_DIR}/{checker.EVIDENCE_NAME}"


def _text() -> str:
    return RUNNER.read_text(encoding="utf-8")


def _active(text: str) -> str:
    """The runner with its block comment and its full-line comments removed.

    Every guard that asserts an *absence* runs against this rather than the raw
    file. The header explains at length that this lane has no GitHub Actions
    workflow and shows an example command with a literal commit in it; both are
    the explanation rather than the thing being forbidden.
    """
    without_block = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(
        line for line in without_block.splitlines() if not line.strip().startswith("#")
    )


def _head_commit() -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# The PowerShell runner -- one asserter per property, over the script's text
# ---------------------------------------------------------------------------


def _assert_windows_only(text: str) -> None:
    """The producer refuses to run anywhere but Windows.

    PowerShell 7 runs on macOS and Linux, so being in PowerShell is not being on
    Windows, and a producer that ran on the verifier's laptop would collect the
    POSIX half of the suite while reporting it as the Windows qualification.
    """
    active = _active(text)
    assert "[System.PlatformID]::Win32NT" in active, "the runner does not check the platform"
    guard = active.split("[System.PlatformID]::Win32NT", 1)[1]
    assert guard.lstrip().startswith(")"), "the platform check is not the condition of anything"
    assert "-ne [System.PlatformID]::Win32NT" in active, "the refusal is not on the non-Windows side"
    assert "throw" in guard.split("}", 1)[0], "a non-Windows platform does not stop the run"


def _assert_mandatory_parameters(text: str) -> None:
    """Both inputs are required, and both are shape-checked before anything runs.

    Neither has a default. A commit that defaulted to `HEAD` would qualify
    whatever happened to be checked out, and a challenge that defaulted to
    anything would let a record be produced before the verifier chose one --
    which is the whole of what the challenge is for.
    """
    parameters = _active(text).split("param(", 1)[1].split("\n)", 1)[0]
    for name, pattern in (("$ExpectedCommit", "{40}"), ("$Challenge", "{64}")):
        declaration = parameters.split(name, 1)
        assert len(declaration) == 2, f"the runner declares no {name} parameter"
        # This parameter's own attribute block: everything from the last
        # `[Parameter(` before the name. Scoped rather than searched across the
        # whole `param()` list, because the other parameter's attributes would
        # otherwise satisfy this one's assertions.
        preceding = declaration[0].rsplit("[Parameter(", 1)
        assert len(preceding) == 2, f"{name} carries no [Parameter()] attribute"
        attributes = "[Parameter(" + preceding[1]
        assert "[Parameter(Mandatory = $true)]" in attributes, f"{name} is not mandatory"
        assert f"[ValidatePattern('^[0-9a-f]{pattern}$')]" in attributes, (
            f"{name} is not pinned to exactly {pattern} lowercase hex characters"
        )
        assert "[string]" in attributes, f"{name} is not typed"


def _assert_clean_checkout_at_the_expected_commit(text: str) -> None:
    """HEAD is the requested commit, the tree is clean, and both are read first.

    Order is the property, not decoration. This script writes into
    `artifacts/p1b-windows-qualification`, so a status taken after the run would
    describe its own output rather than the tree that was tested.
    """
    active = _active(text)
    assert "git rev-parse HEAD" in active
    assert "$HeadCommit -ne $ExpectedCommit" in active, "the runner does not pin HEAD"
    assert "git status --porcelain" in active
    assert "$CheckoutStatus -ne 'clean'" in active, "the runner accepts a dirty tree"
    assert active.index("git status --porcelain") < active.index("-m pytest"), (
        "the working tree is measured after the run rather than before it"
    )
    for guard in ("$HeadCommit -ne $ExpectedCommit", "$CheckoutStatus -ne 'clean'"):
        assert "throw" in active.split(guard, 1)[1].split("}", 1)[0], f"{guard} does not stop"


def _assert_the_interpreter_is_cpython_of_the_pinned_series(text: str) -> None:
    """3.11, and the interpreter is made to say so itself.

    79/64/15/0/0 is what the discovery file collects under 3.11. Selecting an
    interpreter by name is not the same as getting one: a launcher that resolved
    something else, or a `python` on PATH that is 3.12, would otherwise qualify a
    baseline nobody counted.
    """
    active = _active(text)
    assert f"$PythonSeries = '{checker.PYTHON_SERIES}'" in active
    assert "platform.python_implementation()" in active, "the interpreter is never asked"
    assert "^CPython $([regex]::Escape($PythonSeries))\\.\\d+$" in active, (
        "the reported interpreter is not matched against the pinned series"
    )
    assert "throw" in active.split("$Reported -notmatch", 1)[1].split("}", 1)[0]


def _install_commands(text: str) -> tuple[tuple[str, ...], ...]:
    block = re.search(r"\$InstallCommands = @\((.*?)\n\)", _active(text), re.DOTALL)
    assert block is not None, "the runner declares no $InstallCommands list"
    return tuple(
        tuple(re.findall(r"'([^']*)'", entry))
        for entry in re.findall(r"@\(([^)]*)\)", block.group(1))
    )


def _assert_exactly_three_installs_and_no_other_download(text: str) -> None:
    """The whole install list, in order, and nothing else fetched.

    An install nothing imports is an unreviewed dependency on the qualifying run;
    a missing one takes the whole file down at collection. Anything fetched
    outside this list is neither, and is exactly what "no hidden dependencies"
    forbids -- including `pip install --upgrade pip`, which would let the
    qualifying environment move on its own.
    """
    assert _install_commands(text) == EXPECTED_INSTALL_COMMANDS

    active = _active(text).lower()
    assert active.count("'pip'") == len(EXPECTED_INSTALL_COMMANDS), (
        "pip is invoked outside the pinned install list"
    )
    for marker in DOWNLOAD_MARKERS:
        assert marker not in active, f"the runner fetches something outside its install list: {marker}"


def _assert_unfiltered_pytest(text: str) -> None:
    """The whole file, once, with no way to narrow it.

    A narrowed run could report a green summary while the three native Windows
    cases never ran, which is the exact defect this lane exists to rule out.
    """
    active = _active(text)
    lines = [line.strip() for line in active.splitlines() if "-m pytest" in line]
    assert len(lines) == 1, f"the runner invokes pytest {len(lines)} times"
    assert DISCOVERY_TEST in active
    # The arguments pytest itself receives. `-m pytest` is the invocation rather
    # than an argument, so the scan starts after it -- otherwise its own `-m`
    # reads as a marker filter.
    arguments = lines[0].split("pytest", 1)[1].split()
    for flag in FILTERS:
        assert flag not in arguments, f"the run is narrowed by {flag}"
    assert "--strict-markers" in arguments, "a mistyped marker would be a silent no-op"
    assert "$DiscoveryTest" in arguments


def _assert_the_raw_record_is_private_and_deleted(text: str) -> None:
    """pytest's own report never enters the artifact tree, and does not survive.

    It is the one document in this lane that quotes the outside world without
    limit -- assertion text, tracebacks, captured output, absolute guest paths,
    and whatever a failing case happened to be holding. Redacting it after the
    fact is a losing game, so it is written to a per-user temporary directory
    with an unpredictable name and removed in `finally`, which is the branch that
    also runs when the qualification fails.
    """
    active = _active(text)
    raw_root = re.search(r"\$RawRoot = ([^\n]+)", active)
    assert raw_root is not None, "the runner declares no private directory"
    assert "$env:TEMP" in raw_root.group(1), "the raw record is not under the per-user temp root"
    assert "NewGuid" in raw_root.group(1), "the raw record's directory name is predictable"
    assert ARTIFACT_DIR not in raw_root.group(1), "the raw record is inside the artifact tree"

    assert "$RawJunit = Join-Path $RawRoot" in active
    assert '"--junitxml=$RawJunit"' in active, "the suite writes its report elsewhere"
    assert '"--junit=$RawJunit"' in active, "the checker does not read the raw record"

    cleanup = active.rsplit("finally", 1)
    assert len(cleanup) == 2, "the runner has no finally block"
    assert "Remove-Item -LiteralPath $RawRoot -Recurse -Force" in cleanup[1], (
        "the raw record survives the run"
    )


def _assert_checker_decides_the_verdict(text: str) -> None:
    """The checker's exit code is the runner's, and pytest's decides nothing.

    A pass/fail code says whether cases failed, not which cases ran, and the
    defect this lane exists to close is a case that never runs.
    """
    active = _active(text)
    assert "$CheckerScript produce" in active, "the runner does not invoke the producer mode"
    for argument in (
        '"--junit=$RawJunit"',
        '"--facts=$FactsFile"',
        f'"--sanitized-junit=$ArtifactDir/{checker.SANITIZED_JUNIT_NAME}"',
        f'"--evidence=$ArtifactDir/{checker.EVIDENCE_NAME}"',
        '"--expected-commit=$ExpectedCommit"',
        '"--challenge=$Challenge"',
    ):
        assert argument in active, f"the checker is not given {argument}"
    assert "$Verdict = $LASTEXITCODE" in active
    assert "exit $Verdict" in active, "the runner does not return the checker's verdict"
    assert active.index("-m pytest") < active.index("$CheckerScript produce")


def _assert_the_artifact_boundary(text: str) -> None:
    """Exactly two files, at exactly two relative paths, from exactly this run.

    The comparison has to be on paths rather than on names. `Get-ChildItem
    -Recurse` walks subdirectories, so a run that left `nested/evidence.json`
    beside the two artifacts would satisfy a name-only comparison while having
    written a file this lane never creates.
    """
    active = _active(text)
    assert f"$ArtifactDir = '{ARTIFACT_DIR}'" in active
    assert "Remove-Item -LiteralPath $ArtifactDir -Recurse -Force" in active, (
        "an earlier run's evidence could survive into this one"
    )
    assert (
        f"'{checker.EVIDENCE_NAME},{checker.SANITIZED_JUNIT_NAME}'" in active
    ), "the runner does not check what it left behind"
    assert active.index("Remove-Item -LiteralPath $ArtifactDir") < active.index(
        "$CheckerScript produce"
    )

    assert "$ArtifactRoot = (Get-Item -LiteralPath $ArtifactDir).FullName" in active, (
        "the relative paths are not measured from the artifact directory"
    )
    written = active.split("$Written = ", 1)
    assert len(written) == 2, "the runner never lists what it wrote"
    listing = written[1].split("if (", 1)[0]
    assert "-Recurse -File" in listing, "the listing does not reach into subdirectories"
    assert "$_.FullName.Substring($ArtifactRoot.Length + 1)" in listing, (
        "the runner compares file names rather than exact relative paths"
    )
    assert "$_.Name" not in listing, "the runner still projects bare file names"
    assert ".Replace('\\', '/')" in listing, "the compared paths are separator-dependent"


def _assert_no_hosted_ci_dependency(text: str) -> None:
    """No workflow, no hosted runner, no billing -- as a property of the file."""
    active = _active(text).lower()
    for marker in HOSTED_CI_MARKERS:
        assert marker not in active, f"the runner depends on hosted CI: {marker}"


def _assert_no_frozen_commit(text: str) -> None:
    """No candidate SHA is written down: a literal would pin this lane forever."""
    for line in _active(text).splitlines():
        assert not FULL_SHA.search(line), f"a candidate SHA is written into: {line.strip()}"


def _assert_the_facts_cover_every_key_the_checker_requires(text: str) -> None:
    """The runner writes the facts file the checker's contract names, in full."""
    active = _active(text)
    facts = active.split("$Facts = [ordered] @{", 1)
    assert len(facts) == 2, "the runner assembles no facts"
    body = facts[1].split("\n    }", 1)[0]
    written = {match.group(1) for match in re.finditer(r"^\s*(\w+)\s*=", body, re.MULTILINE)}
    assert written == set(checker.FACT_KEYS), (
        f"the runner writes {sorted(written)}, and the checker requires "
        f"{sorted(checker.FACT_KEYS)}"
    )
    assert "Win32_OperatingSystem" in active
    assert "Win32_ComputerSystem" in active
    assert "Win32_BIOS" in active


def _assert_the_generic_identity_facts_are_collected_raw(text: str) -> None:
    """The four facts the provider rules read, each straight off CIM.

    Raw and unfiltered on purpose. The manufacturer, model and BIOS strings are
    what a guest reports about itself, and the hypervisor flag is the CIM boolean
    lowercased -- nothing here maps them onto a vendor, because which vendor is
    acceptable is `RECOGNIZED_PROVIDERS`' decision and a collector that pre-judged
    it would have to be edited to qualify a guest on a different hypervisor.
    """
    active = _active(text)
    for fact, source in (
        ("computer_manufacturer", "$ComputerSystem.Manufacturer"),
        ("computer_model", "$ComputerSystem.Model"),
        ("bios_vendor", "$Bios.Manufacturer"),
    ):
        line = next(
            (entry for entry in active.splitlines() if entry.strip().startswith(f"{fact} ")),
            "",
        )
        assert source in line, f"{fact} does not come from {source}"
    assert "([string] $ComputerSystem.HypervisorPresent).ToLowerInvariant()" in active, (
        "the hypervisor flag is not the CIM property, folded to the case the checker reads"
    )


def _assert_the_guest_tools_signal_is_generic_and_optional(text: str) -> None:
    """A guest-tools signal collected without betting on one service name.

    Parallels ships its tools service as `prl_tools_service` on some builds and
    `prl_tools` on others, VMware ships `VMTools`, and a lookup of any single one
    of those makes a real guest look unidentified when it happens to be running
    another. So the whole service list is scanned and matched on a pattern, and
    the *display* name is recorded, because that is the string that names the
    vendor.

    Optional is the other half: nothing in the runner may make the run depend on
    finding tools, since a guest whose CIM identity already names its provider
    qualifies with none installed.
    """
    active = _active(text)
    assert "Win32_Service" in active, "the runner never looks for a guest-tools service"
    assert "$GuestToolsPattern" in active, "the tools lookup is not pattern-based"
    pattern = re.search(r"\$GuestToolsPattern = '([^']*)'", active)
    assert pattern is not None, "the runner declares no guest-tools pattern"
    for provider in checker.RECOGNIZED_PROVIDERS:
        assert provider in pattern.group(1).lower(), (
            f"the tools pattern cannot match a {provider} guest"
        )
    assert "|" in pattern.group(1), "the pattern names a single service and nothing else"
    assert "$_.DisplayName -match $GuestToolsPattern" in active, (
        "the display name -- the one that names the vendor -- is not matched"
    )
    assert "$ToolsName = [string] $ToolsService.DisplayName" in active, (
        "the recorded service is not the display name"
    )
    assert "throw" not in active.split("$GuestToolsPattern", 1)[1].split("$Facts", 1)[0], (
        "a guest without tools cannot finish the run"
    )


ASSERTERS = (
    _assert_windows_only,
    _assert_mandatory_parameters,
    _assert_clean_checkout_at_the_expected_commit,
    _assert_the_interpreter_is_cpython_of_the_pinned_series,
    _assert_exactly_three_installs_and_no_other_download,
    _assert_unfiltered_pytest,
    _assert_the_raw_record_is_private_and_deleted,
    _assert_checker_decides_the_verdict,
    _assert_the_artifact_boundary,
    _assert_no_hosted_ci_dependency,
    _assert_no_frozen_commit,
    _assert_the_facts_cover_every_key_the_checker_requires,
    _assert_the_generic_identity_facts_are_collected_raw,
    _assert_the_guest_tools_signal_is_generic_and_optional,
)


def test_the_runner_and_the_checker_exist() -> None:
    assert RUNNER.is_file(), f"missing runner: {RUNNER}"
    assert CHECKER.is_file(), f"missing checker: {CHECKER}"


def test_the_lane_declares_no_github_workflow() -> None:
    """The redesign's premise: Core qualifies P1b without Actions or billing."""
    workflows = REPO_ROOT / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")) if workflows.is_dir() else ():
        assert "p1b" not in path.name, f"a P1b workflow is back: {path}"
        assert "p1b" not in path.read_text(encoding="utf-8").lower(), (
            f"a workflow reaches into this lane: {path}"
        )


@pytest.mark.parametrize("asserter", ASSERTERS, ids=lambda function: function.__name__)
def test_the_real_runner_satisfies_every_guard(asserter: Any) -> None:
    asserter(_text())


#: (guard, substring to replace, replacement). Each entry breaks exactly one
#: property, so the guard that must catch it is named beside it.
RUNNER_CORRUPTIONS: tuple[tuple[Any, str, str], ...] = (
    (_assert_windows_only, "-ne [System.PlatformID]::Win32NT", "-eq [System.PlatformID]::Win32NT"),
    (
        _assert_windows_only,
        "if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {\n    throw",
        "if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {\n    Write-Host",
    ),
    # Either input made optional, or its shape left unpinned.
    (_assert_mandatory_parameters, "    [Parameter(Mandatory = $true)]\n    [ValidatePattern('^[0-9a-f]{64}$')]", "    [Parameter(Mandatory = $false)]\n    [ValidatePattern('^[0-9a-f]{64}$')]"),
    (_assert_mandatory_parameters, "[ValidatePattern('^[0-9a-f]{40}$')]\n", ""),
    (_assert_mandatory_parameters, "[ValidatePattern('^[0-9a-f]{64}$')]\n", ""),
    # The checkout: HEAD unpinned, a dirty tree accepted, and the status moved
    # after the run it is supposed to describe.
    (_assert_clean_checkout_at_the_expected_commit, "$HeadCommit -ne $ExpectedCommit", "$HeadCommit -eq $null"),
    (
        _assert_clean_checkout_at_the_expected_commit,
        "if ($CheckoutStatus -ne 'clean') {\n    throw",
        "if ($CheckoutStatus -ne 'clean') {\n    Write-Host",
    ),
    (_assert_clean_checkout_at_the_expected_commit, "$Porcelain = & git status --porcelain\n", ""),
    # The interpreter: the next series, and the self-report dropped.
    (_assert_the_interpreter_is_cpython_of_the_pinned_series, "$PythonSeries = '3.11'", "$PythonSeries = '3.12'"),
    (
        _assert_the_interpreter_is_cpython_of_the_pinned_series,
        "$Reported -notmatch \"^CPython $([regex]::Escape($PythonSeries))\\.\\d+$\"",
        "$Reported -eq ''",
    ),
    # The install list: an extra install, a reordering, a self-upgrade, and a
    # pip invocation outside the list.
    (
        _assert_exactly_three_installs_and_no_other_download,
        "    @('-m', 'pip', 'install', 'pytest'),",
        "    @('-m', 'pip', 'install', 'pytest'),\n    @('-m', 'pip', 'install', 'requests'),",
    ),
    (
        _assert_exactly_three_installs_and_no_other_download,
        "    @('-m', 'pip', 'install', '-e', '.'),\n    @('-m', 'pip', 'install', '-e', 'packages/omnivia-core-client')",
        "    @('-m', 'pip', 'install', '-e', 'packages/omnivia-core-client'),\n    @('-m', 'pip', 'install', '-e', '.')",
    ),
    (
        _assert_exactly_three_installs_and_no_other_download,
        "$InstallCommands = @(",
        "& $PythonExe @PythonArgs -m pip install --upgrade pip\n$InstallCommands = @(",
    ),
    (
        _assert_exactly_three_installs_and_no_other_download,
        "New-Item -ItemType Directory -Path $RawRoot -Force | Out-Null",
        "Invoke-WebRequest -Uri https://example.invalid/wheel -OutFile w.whl\nNew-Item -ItemType Directory -Path $RawRoot -Force | Out-Null",
    ),
    # The run: narrowed four ways, and split into two invocations.
    (_assert_unfiltered_pytest, "-m pytest $DiscoveryTest -q -rs", "-m pytest $DiscoveryTest -k windows -q -rs"),
    (_assert_unfiltered_pytest, "-q -rs --strict-markers", "-q -rs --strict-markers --deselect x"),
    (_assert_unfiltered_pytest, "-q -rs --strict-markers", "-q -rs"),
    (
        _assert_unfiltered_pytest,
        '-m pytest $DiscoveryTest -q -rs --strict-markers "--junitxml=$RawJunit"',
        '-m pytest $DiscoveryTest -q -rs --strict-markers "--junitxml=$RawJunit"\n    & $PythonExe @PythonArgs -m pytest $DiscoveryTest -q',
    ),
    # The raw record: moved into the artifact tree, made predictable, and left
    # behind by dropping the cleanup.
    (
        _assert_the_raw_record_is_private_and_deleted,
        '$RawRoot = Join-Path $env:TEMP ("p1b-raw-junit-" + [guid]::NewGuid().ToString(\'N\'))',
        "$RawRoot = Join-Path $ArtifactDir 'raw'",
    ),
    (
        _assert_the_raw_record_is_private_and_deleted,
        '$RawRoot = Join-Path $env:TEMP ("p1b-raw-junit-" + [guid]::NewGuid().ToString(\'N\'))',
        "$RawRoot = Join-Path $env:TEMP 'p1b-raw-junit'",
    ),
    (
        _assert_the_raw_record_is_private_and_deleted,
        "    Remove-Item -LiteralPath $RawRoot -Recurse -Force -ErrorAction SilentlyContinue",
        "    Write-Host $RawRoot",
    ),
    # The verdict: swallowed, and the checker never invoked at all.
    (_assert_checker_decides_the_verdict, "exit $Verdict", "exit 0"),
    (_assert_checker_decides_the_verdict, '"--challenge=$Challenge"', '"--challenge=0"'),
    (_assert_checker_decides_the_verdict, "$CheckerScript produce", "$CheckerScript check"),
    # The artifact boundary: a stale directory reused, and the check on what was
    # written removed.
    (
        _assert_the_artifact_boundary,
        "        Remove-Item -LiteralPath $ArtifactDir -Recurse -Force\n",
        "",
    ),
    (
        _assert_the_artifact_boundary,
        "'evidence.json,junit.sanitized.xml'",
        "'evidence.json'",
    ),
    (_assert_the_artifact_boundary, "$ArtifactDir = 'artifacts/p1b-windows-qualification'", "$ArtifactDir = 'artifacts'"),
    # Hosted CI, smuggled back in.
    (
        _assert_no_hosted_ci_dependency,
        "$ArtifactDir = 'artifacts/p1b-windows-qualification'",
        "$ArtifactDir = $env:GITHUB_WORKSPACE",
    ),
    (_assert_no_hosted_ci_dependency, "$RepoRoot = Split-Path -Parent $PSScriptRoot", "$RepoRoot = '${{ runner.temp }}'"),
    # A candidate frozen into the script.
    (
        _assert_no_frozen_commit,
        "if ($HeadCommit -ne $ExpectedCommit) {",
        "if ($HeadCommit -ne 'e515ad5351498c1e44b99f4a2e74207d7aed1c04') {",
    ),
    # The boundary compared on file names again, so a nested file could pass, and
    # the relative-path root taken from somewhere that is not the artifact tree.
    (
        _assert_the_artifact_boundary,
        "ForEach-Object { $_.FullName.Substring($ArtifactRoot.Length + 1).Replace('\\', '/') }",
        "ForEach-Object { $_.Name }",
    ),
    (
        _assert_the_artifact_boundary,
        "$ArtifactRoot = (Get-Item -LiteralPath $ArtifactDir).FullName",
        "$ArtifactRoot = (Get-Item -LiteralPath $RepoRoot).FullName",
    ),
    # A fact the checker requires, dropped from the file the runner writes.
    (_assert_the_facts_cover_every_key_the_checker_requires, "        bios_vendor            = [string] $Bios.Manufacturer\n", ""),
    (_assert_the_facts_cover_every_key_the_checker_requires, "$Bios = Get-CimInstance -ClassName Win32_BIOS", "$Bios = $null"),
    # An identity fact no longer read off CIM, and the hypervisor flag left in
    # whatever case the property happened to have.
    (
        _assert_the_generic_identity_facts_are_collected_raw,
        "computer_model         = [string] $ComputerSystem.Model",
        "computer_model         = 'Parallels Virtual Platform'",
    ),
    (
        _assert_the_generic_identity_facts_are_collected_raw,
        "([string] $ComputerSystem.HypervisorPresent).ToLowerInvariant()",
        "[string] $ComputerSystem.HypervisorPresent",
    ),
    # The guest-tools signal, narrowed back to one guessed service name, matched
    # on the service name alone, recorded as the name that does not identify the
    # vendor, and made mandatory.
    (
        _assert_the_guest_tools_signal_is_generic_and_optional,
        "$GuestToolsPattern = 'parallels|vmware|virtualbox|^prl_|^vm3dservice|^vmtools|^vboxservice'",
        "$GuestToolsPattern = '^prl_tools_service'",
    ),
    (
        _assert_the_guest_tools_signal_is_generic_and_optional,
        " -or $_.DisplayName -match $GuestToolsPattern",
        "",
    ),
    (
        _assert_the_guest_tools_signal_is_generic_and_optional,
        "$ToolsName = [string] $ToolsService.DisplayName",
        "$ToolsName = [string] $ToolsService.Name",
    ),
    (
        _assert_the_guest_tools_signal_is_generic_and_optional,
        "    $ToolsName = ''\n",
        "    if ($null -eq $ToolsService) { throw 'no guest tools' }\n",
    ),
)


@pytest.mark.parametrize(
    ("asserter", "original", "replacement"),
    RUNNER_CORRUPTIONS,
    ids=[f"{index}-{entry[0].__name__}" for index, entry in enumerate(RUNNER_CORRUPTIONS)],
)
def test_each_runner_guard_is_red_when_its_property_is_violated(
    asserter: Any, original: str, replacement: str
) -> None:
    text = _text()
    assert original in text, f"the corruption no longer applies: {original!r}"
    corrupted = text.replace(original, replacement, 1)
    assert corrupted != text

    with pytest.raises(AssertionError):
        asserter(corrupted)


def test_the_install_list_is_exactly_what_the_collected_file_imports() -> None:
    """The runner's install targets, derived from the suite rather than declared.

    Both halves are read out of the tree. The imports come from the AST of the
    collected file and of the `conftest.py` pytest loads beside it -- a conftest
    that fails to import fails collection just as hard -- and the install targets
    come from parsing the runner. Writing either down here would let them drift
    apart while this test kept passing.
    """
    providers = {
        package: distribution.install_target
        for distribution in alignment.local_distributions(REPO_ROOT)
        for package in distribution.import_packages
    }
    imported = {
        providers[name]
        for source in (DISCOVERY_TEST, DISCOVERY_CONFTEST)
        for name, _ in alignment.top_level_imports(REPO_ROOT / source)
        if name in providers
    }
    assert imported == {".", "packages/omnivia-core-client"}

    installed = {
        command[command.index("-e") + 1]
        for command in _install_commands(_text())
        if "-e" in command
    }
    assert installed == imported


def test_the_runner_and_the_checker_agree_about_every_path_and_pin() -> None:
    """The two files are one contract; a rename in either has to break here."""
    active = _active(_text())
    assert checker.RUNNER_SCRIPT == RUNNER.relative_to(REPO_ROOT).as_posix()
    assert checker.CHECKER_SCRIPT == CHECKER.relative_to(REPO_ROOT).as_posix()
    assert f"$CheckerScript = '{checker.CHECKER_SCRIPT}'" in active
    assert f"$DiscoveryTest = '{checker.DISCOVERY_TEST}'" in active
    assert checker.DISCOVERY_TEST == DISCOVERY_TEST
    assert (REPO_ROOT / checker.DISCOVERY_TEST).is_file()
    assert checker.PYTHON_SERIES == "3.11"
    assert len(checker.ALLOWED_SKIPPED_NODE_IDS) == checker.EXPECTED_COUNTS["skipped"]
    assert len(set(checker.ALLOWED_SKIPPED_NODE_IDS)) == 15
    assert (
        checker.EXPECTED_COUNTS["passed"] + checker.EXPECTED_COUNTS["skipped"]
        == checker.EXPECTED_COUNTS["collected"]
    )


def test_the_schema_version_moved_for_the_hypervisor_neutral_shape() -> None:
    """Each redesign changed what a record means, so a stored one has to say which.

    `3` and earlier described a hosted GitHub Actions job and required fields --
    the run id, the workflow reference, the pull request -- that no longer exist.
    `4` was the two-phase local-VM lane with VMware-named fields and an
    uninterpreted hypervisor flag. `5` is the hypervisor-neutral shape: generic
    guest-tools fields, a hypervisor flag that must be true, and a provider drawn
    from `RECOGNIZED_PROVIDERS`. None of those is a later record with fields
    missing, which is why the version is compared by value and why this pins that
    it moved.
    """
    assert checker.CHECKER_VERSION == "5"
    assert int(checker.CHECKER_VERSION) > 4
    for retired in ("vmware_tools_service", "vmware_tools_version"):
        assert retired not in checker.FACT_KEYS, f"{retired} is a version-4 field"
        assert not any(retired in field for field in checker.REQUIRED_FIELDS)


# ---------------------------------------------------------------------------
# The checker -- one mutated normalized input at a time
# ---------------------------------------------------------------------------


def _render_junit(
    cases: Sequence[tuple[str, str]],
    *,
    failures: int = 0,
    errors: int = 0,
    skipped: int | None = None,
    tests: int | None = None,
    classname: str | None = None,
    suite_time: str = "42.5",
    detail: str = "boom",
    system_out: str = "",
) -> str:
    """A JUnit record in pytest's shape.

    `detail` and `system_out` are where a real record quotes the outside world --
    the failure body and the captured output -- so they are parameters here: the
    redaction tests plant a credential, a path or a traceback in them and require
    it absent from the record that is kept.
    """
    classname = checker.DISCOVERY_CLASSNAME if classname is None else classname
    skipped = sum(1 for _, status in cases if status == "skipped") if skipped is None else skipped
    tests = len(cases) if tests is None else tests
    captured = f"<system-out>{system_out}</system-out>" if system_out else ""
    body = []
    for name, status in cases:
        child = {
            "passed": "",
            "skipped": '<skipped type="pytest.skip" message="platform" />',
            "failed": f'<failure message="assertion failed">{detail}</failure>',
            "errored": f'<error message="collection failed">{detail}</error>',
        }[status]
        body.append(
            f'<testcase classname="{classname}" name="{name}" time="0.25">'
            f"{child}{captured}</testcase>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{tests}" time="{suite_time}">'
        f"{''.join(body)}</testsuite></testsuites>"
    )


def _qualifying_cases() -> list[tuple[str, str]]:
    """The shape a passing Windows run produces: 79/64/15/0/0."""
    passed = [
        *checker.REQUIRED_WINDOWS_CASES,
        *(f"test_platform_neutral_case_{index}" for index in range(61)),
    ]
    skipped = [node.split("::", 1)[1] for node in checker.ALLOWED_SKIPPED_NODE_IDS]
    assert len(passed) == 64
    assert len(skipped) == 15
    return [(name, "passed") for name in passed] + [(name, "skipped") for name in skipped]


#: What `platform` reports on a Windows guest, and the volume the checkout and
#: the temp directory share there.
#:
#: The two interpreter strings are here rather than left to the local ones
#: because the checker requires CPython 3.11 by value, and this file is collected
#: by whatever interpreter runs it. Both are patched, so the guard is exercised
#: against a fixed pair rather than against whichever one happens to be present.
WINDOWS_PLATFORM = {
    "system": "Windows",
    "release": "11",
    "version": "10.0.26100",
    "machine": "AMD64",
    "python_version": f"{checker.PYTHON_SERIES}.9",
    "python_implementation": "CPython",
}
WINDOWS_VOLUME = ("C:/", "NTFS")

#: What PowerShell hands the checker on a Parallels Desktop guest -- the provider
#: this lane is run on. Shaped like the real thing and belonging to no real run:
#: these values exist to exercise the checker's host rules, not to stand in for a
#: result. The other recognized provider's strings are exercised beside them,
#: from `PROVIDER_IDENTITIES` rather than by swapping this baseline.
VM_FACTS: dict[str, str] = {
    "windows_caption": "Microsoft Windows 11 Pro",
    "windows_version": "10.0.26100",
    "windows_build": "26100",
    "computer_manufacturer": "Parallels International GmbH",
    "computer_model": "Parallels Virtual Platform",
    "bios_vendor": "Parallels Software International Inc.",
    "hypervisor_present": "true",
    "guest_tools_service": "Parallels Tools Service",
    "guest_tools_version": "20.2.2.55879",
    "powershell_edition": "Desktop",
    "powershell_executable": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "powershell_version": "5.1.26100.2161",
    "checkout_status": "clean",
}


def test_the_synthetic_facts_cover_the_checkers_contract_exactly() -> None:
    """A fact added to the checker and not to these tests would go unexercised."""
    assert set(VM_FACTS) == set(checker.FACT_KEYS)


@pytest.fixture
def windows_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checker as it runs on the Windows guest.

    Only the host context is simulated: the four platform strings and the two
    volume identities. Everything the verdict actually turns on stays real -- the
    counts and node ids come from the record the test wrote, the commit, tree and
    blob identities from this checkout, the installed distributions from this
    interpreter, and every digest from the bytes involved.

    The CIM facts are not patched here: they arrive as `VM_FACTS`, through the
    same parameter the runner's facts file feeds, so the tests exercise the real
    ingestion path rather than a shortcut around it.
    """
    for attribute, reported in WINDOWS_PLATFORM.items():
        monkeypatch.setattr(checker.platform, attribute, lambda reported=reported: reported)
    monkeypatch.setattr(checker, "_volume", lambda path: WINDOWS_VOLUME)


@pytest.fixture
def qualifying_junit(windows_vm: None, tmp_path: Path) -> Path:
    """The qualifying record, in the host context a qualifying record needs.

    The two are one fixture because they are one claim: a record with these
    counts is a qualifying result only if it came off a Windows guest, and a test
    that took the record without the context would be asserting against a
    baseline the checker rightly refuses.
    """
    path = tmp_path / "junit.xml"
    path.write_text(_render_junit(_qualifying_cases()), encoding="utf-8")
    return path


def _evidence(
    junit: Path,
    *,
    challenge: str = CHALLENGE,
    facts: dict[str, str] | None = None,
    facts_error: str = "",
) -> dict[str, Any]:
    # `checker` is loaded from a path, so every attribute is `Any` to mypy. The
    # casts below are the annotation the loader cannot supply.
    return cast(
        "dict[str, Any]",
        checker.build_evidence(
            root=REPO_ROOT,
            junit=junit,
            expected_commit=_head_commit(),
            challenge=challenge,
            facts=VM_FACTS if facts is None else facts,
            facts_error=facts_error,
        ),
    )


def _findings(
    evidence: dict[str, Any],
    junit: Path | None,
    *,
    expected_commit: str | None = None,
    challenge: str = CHALLENGE,
    sanitized: str | None = None,
) -> list[str]:
    observed = checker.observe(
        REPO_ROOT,
        expected_commit=_head_commit() if expected_commit is None else expected_commit,
        challenge=challenge,
        sanitized_junit=(
            checker.sanitize_junit(junit) if sanitized is None and junit else str(sanitized)
        ),
        raw_junit=junit,
    )
    return cast("list[str]", checker.validate(evidence, observed=observed))


def _restamp(evidence: dict[str, Any]) -> dict[str, Any]:
    """Recompute the payload digest, so a mutation is caught by its own check.

    Without this every corruption below would trip `integrity.evidence_sha256`
    and nothing else would be proven.
    """
    evidence["integrity"]["evidence_sha256"] = checker.evidence_digest(evidence)
    return evidence


def _mutate(evidence: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    copy = json.loads(json.dumps(evidence))
    parts = path.split(".")
    node = copy
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return _restamp(copy)


def _without(evidence: dict[str, Any], path: str) -> dict[str, Any]:
    copy = json.loads(json.dumps(evidence))
    parts = path.split(".")
    node = copy
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]
    # Re-stamping would put the digest field back, so the one field that cannot
    # be re-stamped after deletion is the digest itself.
    return copy if path == "integrity.evidence_sha256" else _restamp(copy)


def test_the_qualifying_shape_is_green(qualifying_junit: Path) -> None:
    """The baseline. Every corruption below is measured against this."""
    evidence = _evidence(qualifying_junit)

    assert _findings(evidence, qualifying_junit) == []
    assert evidence["outcome"]["collected"] == 79
    assert evidence["outcome"]["passed"] == 64
    assert evidence["outcome"]["skipped"] == 15
    assert evidence["outcome"]["failed"] == 0
    assert evidence["outcome"]["errors"] == 0
    assert evidence["outcome"]["xfail_markers_or_calls_in_source"] == 0
    assert evidence["outcome"]["unexpected_skipped_node_ids"] == []
    assert evidence["outcome"]["missing_skipped_node_ids"] == []
    assert sorted(evidence["outcome"]["skipped_node_ids"]) == sorted(
        checker.ALLOWED_SKIPPED_NODE_IDS
    )
    for case in checker.REQUIRED_WINDOWS_CASES:
        assert evidence["named_cases"][case]["collected"] == 1
        assert evidence["named_cases"][case]["status"] == "passed"


# --- the host context a GO rests on -----------------------------------------

LOCAL_VM_PATHS = tuple(path for path, _, _ in checker._LOCAL_VM_CONTEXT)

#: Every field above except the one whose qualifying value *is* empty. Emptying
#: `host.facts_error` is what a good run looks like, so it gets its own test
#: rather than a place in the emptied parametrization.
EMPTIABLE_PATHS = tuple(path for path in LOCAL_VM_PATHS if path != "host.facts_error")

#: (field, a value that is materially wrong rather than merely absent). Every
#: entry is a run that could really happen and must not qualify: another
#: producer, another script, a dirty tree, a Linux or macOS host, a malformed
#: digest, and an interpreter that is not the qualified one.
WRONG_HOST_VALUES = (
    ("run.qualification_id", "phase2-platform"),
    ("run.producer", "github-actions"),
    ("run.producer", "local-macos"),
    ("run.producer", "windows"),
    ("run.runner_script", "scripts/run-something-else.ps1"),
    ("run.checker_script", "scripts/check-import-install-alignment.py"),
    ("run.requested_commit", "not-a-sha"),
    ("run.requested_commit", "0" * 39),
    ("run.challenge_sha256", "0" * 63),
    ("run.challenge_sha256", "z" * 64),
    ("run.binding_sha256", "not-a-digest"),
    ("code_identity.tested_status", "dirty"),
    ("code_identity.tested_status", "unknown"),
    ("host.os_name", "Linux"),
    ("host.os_name", "Darwin"),
    ("host.windows_caption", "Ubuntu 24.04 LTS"),
    ("host.windows_caption", "macOS 15.0"),
    ("host.facts_error", "the PowerShell facts record no 'bios_vendor'"),
    # Physical Windows, and a flag that answers something other than the
    # question. Only `true` is a guest of any hypervisor.
    ("host.hypervisor_present", "false"),
    ("host.hypervisor_present", "unknown"),
    ("host.hypervisor_present", "0"),
    # Not the qualified interpreter: the next series, the previous one, a bare
    # series no interpreter reports, and three implementations that are not
    # CPython -- including its own name in the wrong case.
    ("toolchain.python_version", "3.12.4"),
    ("toolchain.python_version", "3.13.0"),
    ("toolchain.python_version", "3.10.14"),
    ("toolchain.python_version", "3.11"),
    ("toolchain.python_version", "3.11.x"),
    ("toolchain.python_implementation", "PyPy"),
    ("toolchain.python_implementation", "IronPython"),
    ("toolchain.python_implementation", "cpython"),
)


def test_the_host_context_covers_every_material_fact_and_no_github_one() -> None:
    """The rule table is the claim; this pins what it is claimed to cover.

    The second half is the redesign's point: nothing in this lane may require a
    run id, a workflow reference, a pull request or any other field only a hosted
    GitHub job can produce, because there is no hosted job.
    """
    covered = set(LOCAL_VM_PATHS)
    assert covered <= set(checker.REQUIRED_FIELDS), "a host rule names an unrequired field"
    for family in ("run.", "code_identity.", "host.", "toolchain.", "filesystem."):
        assert any(path.startswith(family) for path in covered), f"nothing checked under {family}"
    for path in (
        "run.producer",
        "run.requested_commit",
        "run.challenge_sha256",
        "run.binding_sha256",
        "code_identity.tested_status",
        "host.os_name",
        "host.windows_caption",
        "host.computer_manufacturer",
        "host.computer_model",
        "host.bios_vendor",
        "host.hypervisor_present",
        "toolchain.python_version",
        "toolchain.python_implementation",
        "toolchain.powershell_version",
        "filesystem.checkout_filesystem",
    ):
        assert path in covered, f"{path} is material and unchecked"

    for field in checker.REQUIRED_FIELDS:
        assert not any(
            marker in field.lower()
            for marker in ("github", "workflow_ref", "pull_request", "run_id", "run_attempt")
        ), f"{field} is a hosted-CI field, and this lane has no hosted CI"


@pytest.mark.parametrize("path", EMPTIABLE_PATHS)
def test_every_material_host_field_must_be_materially_filled_in(
    path: str, qualifying_junit: Path
) -> None:
    """Empty is not recorded: each field emptied on its own, and named."""
    findings = _findings(_mutate(_evidence(qualifying_junit), path, ""), qualifying_junit)

    assert any(path in finding and "a qualifying run records" in finding for finding in findings)


@pytest.mark.parametrize(
    ("path", "wrong"),
    WRONG_HOST_VALUES,
    ids=[f"{path}={wrong}" for path, wrong in WRONG_HOST_VALUES],
)
def test_a_wrong_host_value_is_a_finding(
    path: str, wrong: str, qualifying_junit: Path
) -> None:
    """Present, plausible, and not this lane's run."""
    findings = _findings(_mutate(_evidence(qualifying_junit), path, wrong), qualifying_junit)

    assert any(path in finding and "a qualifying run records" in finding for finding in findings)


def test_the_qualified_toolchain_is_cpython_of_the_pinned_series(
    qualifying_junit: Path,
) -> None:
    """What the evidence records for the interpreter, not merely that it recorded.

    The counterpart to the mutations above: the qualifying payload carries a real
    `<series>.<patch>` version and `CPython`, so the pinned predicate is proven
    against the value a run actually writes rather than only against wrong ones.
    """
    toolchain = _evidence(qualifying_junit)["toolchain"]

    assert toolchain["python_implementation"] == "CPython"
    assert toolchain["python_version"].startswith(f"{checker.PYTHON_SERIES}.")
    assert checker._python_series(toolchain["python_version"])


# --- the local VM provider identity -----------------------------------------

#: The marker every provider finding carries. Asserted through this name so the
#: tests below say which rule they are about rather than quoting a sentence.
NO_PROVIDER = "recognized local VM provider"

#: A machine that names no provider at all: real hardware, no guest tools. Every
#: identity case below starts from this and turns exactly one signal back on, so
#: what is proven is that signal and not the baseline's leftovers.
#:
#: `hypervisor_present` is set to `true` here on purpose. It is a separate rule
#: with its own tests below, and holding it true keeps these cases about the
#: provider alone rather than about two rules firing together.
UNIDENTIFIED_HOST: dict[str, str] = {
    "computer_manufacturer": "Dell Inc.",
    "computer_model": "OptiPlex 7090",
    "bios_vendor": "Dell Inc.",
    "hypervisor_present": "true",
    "guest_tools_service": "",
    "guest_tools_version": "",
}

#: Every string a recognized provider writes into a guest's identity, one signal
#: at a time. Parallels first: it is the provider this lane runs on, and it
#: reports itself through all three CIM properties and through its tools service.
#: VMware is retained because a guest reporting it is as unambiguously a local VM
#: -- `VMware20,1` on current hardware versions, `VMware Virtual Platform` on
#: older ones.
PROVIDER_IDENTITIES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("parallels-manufacturer", "parallels", {"computer_manufacturer": "Parallels International GmbH"}),
    ("parallels-model", "parallels", {"computer_model": "Parallels Virtual Platform"}),
    ("parallels-bios", "parallels", {"bios_vendor": "Parallels Software International Inc."}),
    ("parallels-tools", "parallels", {"guest_tools_service": "Parallels Tools Service"}),
    ("vmware-manufacturer", "vmware", {"computer_manufacturer": "VMware, Inc."}),
    ("vmware-model-modern", "vmware", {"computer_model": "VMware20,1"}),
    ("vmware-model-legacy", "vmware", {"computer_model": "VMware Virtual Platform"}),
    ("vmware-bios", "vmware", {"bios_vendor": "VMware, Inc."}),
    ("vmware-tools", "vmware", {"guest_tools_service": "VMware Tools"}),
)

#: Windows machines this lane has qualified nothing on. Physical hardware is the
#: first; the rest are real hypervisors whose identity strings name a provider
#: `RECOGNIZED_PROVIDERS` does not list. Both kinds must be a finding: "some
#: hypervisor" is not the claim, "this local VM provider" is.
UNRECOGNIZED_HOSTS: tuple[tuple[str, dict[str, str]], ...] = (
    # Real hardware's identity strings, with the hypervisor flag left true, so
    # what this case proves is the provider rule and not the flag beside it.
    ("hardware-strings", {}),
    ("hyper-v", {"computer_manufacturer": "Microsoft Corporation", "computer_model": "Virtual Machine"}),
    ("virtualbox", {"computer_manufacturer": "innotek GmbH", "computer_model": "VirtualBox",
                    "bios_vendor": "innotek GmbH", "guest_tools_service": "VirtualBox Guest Additions Service"}),
    ("qemu-kvm", {"computer_manufacturer": "QEMU", "computer_model": "Standard PC (Q35 + ICH9, 2009)",
                  "bios_vendor": "SeaBIOS"}),
    ("xen", {"computer_manufacturer": "Xen", "computer_model": "HVM domU", "bios_vendor": "Xen"}),
    ("aws-nitro", {"computer_manufacturer": "Amazon EC2", "computer_model": "c5.xlarge",
                   "bios_vendor": "Amazon EC2"}),
    # Tools present but naming nothing: a service string that identifies no
    # vendor is not an identity, however filled in it looks.
    ("tools-naming-nothing", {"guest_tools_service": "Guest Agent", "guest_tools_version": "1.2.3"}),
)


def _with_host(evidence: dict[str, Any], **overrides: str) -> dict[str, Any]:
    return _mutate(evidence, "host", {**evidence["host"], **overrides})


@pytest.mark.parametrize(
    ("label", "provider", "signal"),
    PROVIDER_IDENTITIES,
    ids=[entry[0] for entry in PROVIDER_IDENTITIES],
)
def test_any_one_provider_signal_establishes_the_guest_identity(
    label: str, provider: str, signal: dict[str, str], qualifying_junit: Path
) -> None:
    """Each provider reports itself differently, and any one way is enough.

    Parallels writes its name into the manufacturer, the model and the BIOS
    vendor, and a guest running its tools says so a fourth time; VMware does the
    same with its own strings, and spells the model differently across hardware
    versions. Requiring all four would fail real configurations, so any one is
    enough -- and each one is proven enough here, on its own, against a host that
    otherwise names nothing.
    """
    host = {**UNIDENTIFIED_HOST, **signal}
    evidence = _with_host(_evidence(qualifying_junit), **host)

    findings = _findings(evidence, qualifying_junit)

    assert not any(NO_PROVIDER in finding for finding in findings), findings
    assert checker.recognized_provider(host) == provider


@pytest.mark.parametrize(
    ("label", "host"), UNRECOGNIZED_HOSTS, ids=[entry[0] for entry in UNRECOGNIZED_HOSTS]
)
def test_a_host_that_names_no_recognized_provider_is_a_finding(
    label: str, host: dict[str, str], qualifying_junit: Path
) -> None:
    """What was authorised is a local VM of a recognized provider.

    Not any Windows machine, and not any hypervisor: Hyper-V, VirtualBox, QEMU,
    Xen and a cloud instance are all real virtualization this lane has qualified
    nothing on, and each is refused by the same rule that refuses bare hardware.
    """
    merged = {**UNIDENTIFIED_HOST, **host}
    evidence = _with_host(_evidence(qualifying_junit), **merged)

    findings = _findings(evidence, qualifying_junit)

    assert any(NO_PROVIDER in finding for finding in findings)
    assert checker.recognized_provider(merged) == ""


def test_physical_windows_is_refused_by_both_halves_of_the_rule(
    qualifying_junit: Path,
) -> None:
    """A real Windows box: no hypervisor, and no provider to name.

    The three cases would run natively there and the counts would be identical,
    which is exactly why this is checked rather than assumed. What was authorised
    is a local VM, so both halves have to fire: the hypervisor flag the machine
    reports as false, and the identity strings that name hardware.
    """
    evidence = _with_host(
        _evidence(qualifying_junit), **{**UNIDENTIFIED_HOST, "hypervisor_present": "false"}
    )

    findings = _findings(evidence, qualifying_junit)

    assert any(NO_PROVIDER in finding for finding in findings)
    assert any(
        "host.hypervisor_present" in finding and "a qualifying run records" in finding
        for finding in findings
    )


def test_an_unknown_hypervisor_is_refused_even_with_the_flag_set(
    qualifying_junit: Path,
) -> None:
    """Present-and-unrecognized, which is the case the flag alone cannot catch.

    A Hyper-V guest reports `hypervisor_present` exactly as a Parallels guest
    does. Only the provider rule separates them, so this pins that the flag is
    never sufficient on its own.
    """
    hyper_v = {
        **UNIDENTIFIED_HOST,
        "computer_manufacturer": "Microsoft Corporation",
        "computer_model": "Virtual Machine",
        "hypervisor_present": "true",
    }
    evidence = _with_host(_evidence(qualifying_junit), **hyper_v)

    findings = _findings(evidence, qualifying_junit)

    assert any(NO_PROVIDER in finding for finding in findings)
    assert not any("host.hypervisor_present" in finding for finding in findings)


def test_a_guest_with_no_tools_qualifies_on_its_cim_identity_alone(
    qualifying_junit: Path,
) -> None:
    """Tools are a supporting signal, never a requirement.

    A stock Parallels guest with nothing installed still reports its provider
    through CIM, and refusing it for the absence of a tools service would fail a
    perfectly real VM. Asserted as no findings at all rather than as no provider
    finding: the empty strings must also be legal values for the two optional
    fields.
    """
    evidence = _with_host(
        _evidence(qualifying_junit), guest_tools_service="", guest_tools_version=""
    )

    assert _findings(evidence, qualifying_junit) == []


def test_a_host_block_that_is_not_a_record_is_a_finding(qualifying_junit: Path) -> None:
    findings = _findings(_mutate(_evidence(qualifying_junit), "host", "windows"), qualifying_junit)

    assert any("host is not a record" in finding for finding in findings)


# --- the PowerShell facts ---------------------------------------------------


def _facts_file(directory: Path, **overrides: Any) -> Path:
    path = directory / "facts.json"
    payload: dict[str, Any] = {**VM_FACTS, **overrides}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


MALFORMED_FACTS: tuple[tuple[str, str | None, str], ...] = (
    ("missing", None, "no PowerShell facts"),
    ("not-json", "{not json", "not readable JSON"),
    ("not-an-object", '["windows"]', "not a JSON object"),
    ("empty", "{}", "record no"),
)


@pytest.mark.parametrize(
    ("label", "content", "expected"),
    MALFORMED_FACTS,
    ids=[entry[0] for entry in MALFORMED_FACTS],
)
def test_malformed_powershell_facts_fail_closed(
    label: str, content: str | None, expected: str, tmp_path: Path
) -> None:
    """Unreadable facts are a NO-GO that says why, never an empty-but-green run."""
    path = tmp_path / "facts.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    facts, error = checker.load_facts(path)

    assert expected in error
    assert facts == dict.fromkeys(checker.FACT_KEYS, "")


@pytest.mark.parametrize("key", checker.FACT_KEYS)
def test_a_missing_or_non_string_fact_fails_closed(key: str, tmp_path: Path) -> None:
    """Each key dropped on its own, and each replaced by the `null` `ConvertTo-Json`
    writes for an absent CIM property."""
    dropped = {name: value for name, value in VM_FACTS.items() if name != key}
    (tmp_path / "facts.json").write_text(json.dumps(dropped), encoding="utf-8")
    _, error = checker.load_facts(tmp_path / "facts.json")
    assert key in error and "record no" in error

    nulled = _facts_file(tmp_path, **{key: None})
    _, error = checker.load_facts(nulled)
    assert key in error and "not a string" in error


def test_facts_written_by_windows_powershell_5_1_are_readable(tmp_path: Path) -> None:
    """`Set-Content -Encoding UTF8` writes a BOM on 5.1 and not on 7.

    Left as plain `utf-8`, the mark would make `json.loads` fail at column 1 on a
    document that is otherwise perfectly good -- and the run would report
    unreadable facts on exactly the PowerShell edition a stock Windows guest
    ships. Both shapes are proven here rather than only the one this machine
    happens to produce.
    """
    payload = json.dumps(VM_FACTS)
    for label, raw in (("with-bom", "\ufeff" + payload), ("without-bom", payload)):
        path = tmp_path / f"facts-{label}.json"
        path.write_bytes(raw.encode("utf-8"))

        facts, error = checker.load_facts(path)

        assert error == "", label
        assert facts == VM_FACTS, label


def test_readable_facts_round_trip_into_the_evidence(
    windows_vm: None, tmp_path: Path
) -> None:
    """The path the runner actually uses, end to end."""
    facts, error = checker.load_facts(_facts_file(tmp_path))

    assert error == ""
    assert facts == VM_FACTS

    junit = tmp_path / "junit.xml"
    junit.write_text(_render_junit(_qualifying_cases()), encoding="utf-8")
    evidence = _evidence(junit, facts=facts)

    assert evidence["host"]["computer_manufacturer"] == "Parallels International GmbH"
    assert evidence["host"]["guest_tools_service"] == VM_FACTS["guest_tools_service"]
    assert evidence["host"]["guest_tools_version"] == VM_FACTS["guest_tools_version"]
    assert evidence["toolchain"]["powershell_version"] == VM_FACTS["powershell_version"]
    assert evidence["code_identity"]["tested_status"] == "clean"
    assert _findings(evidence, junit) == []


def test_a_facts_error_alone_is_a_finding(qualifying_junit: Path) -> None:
    """Every other field can read as a pass and the run is still not qualifying."""
    findings = _findings(
        _evidence(qualifying_junit, facts_error="the PowerShell facts are not a JSON object"),
        qualifying_junit,
    )

    assert any("host.facts_error" in finding for finding in findings)


# --- the challenge ----------------------------------------------------------


def test_the_challenge_is_recorded_one_way_and_never_in_the_clear(
    qualifying_junit: Path,
) -> None:
    """The artifact names the challenge it answered without carrying it."""
    evidence = _evidence(qualifying_junit)

    assert evidence["run"]["challenge_sha256"] == hashlib.sha256(
        CHALLENGE.encode("utf-8")
    ).hexdigest()
    assert CHALLENGE not in json.dumps(evidence), "the evidence carries the challenge itself"
    assert evidence["run"]["binding_sha256"] == checker.binding_digest(
        challenge_sha256=evidence["run"]["challenge_sha256"],
        tested_commit=evidence["code_identity"]["tested_commit"],
        junit_sha256=evidence["integrity"]["junit_sha256"],
        sanitized_junit_sha256=evidence["integrity"]["sanitized_junit_sha256"],
    )


def test_a_record_produced_for_another_challenge_does_not_qualify(
    qualifying_junit: Path,
) -> None:
    """The binding's whole purpose: this record answered a different question."""
    evidence = _evidence(qualifying_junit, challenge=OTHER_CHALLENGE)

    findings = _findings(evidence, qualifying_junit, challenge=CHALLENGE)

    assert any("run.challenge_sha256" in finding for finding in findings)
    assert any("does not bind this challenge" in finding for finding in findings)


def test_the_binding_covers_the_one_digest_a_verifier_cannot_witness(
    qualifying_junit: Path,
) -> None:
    """The raw record's digest, which is the whole reason the binding exists.

    The runner deletes the raw record, so offline verification has nothing to
    hash it against. The binding is what makes the recorded digest more than a
    free field: the producer committed to it while holding the challenge, so a
    later edit breaks a value neither the editor nor the verifier can recompute
    without that challenge.
    """
    evidence = _mutate(_evidence(qualifying_junit), "integrity.junit_sha256", "0" * 64)

    findings = _findings(evidence, qualifying_junit)

    assert any("does not bind this challenge" in finding for finding in findings)


@pytest.mark.parametrize("field", ["run.challenge_sha256", "code_identity.tested_commit"])
def test_a_binding_component_the_verifier_re_derives_is_caught_by_comparison(
    field: str, qualifying_junit: Path
) -> None:
    """The other components, and why they do not show up as a binding finding.

    The binding is recomputed from ground truth -- the challenge the verifier
    chose and the commit it checked out -- rather than from the record's copies
    of them. So a moved copy leaves the binding intact and is caught one line
    earlier, by comparing the recorded value against the world. Asserting it here
    is what stops that comparison being dropped on the grounds that "the binding
    covers it".
    """
    findings = _findings(_mutate(_evidence(qualifying_junit), field, "0" * 64), qualifying_junit)

    assert any(field in finding and "this checkout has" in finding for finding in findings)


def test_a_binding_that_was_never_computed_is_a_finding(qualifying_junit: Path) -> None:
    findings = _findings(
        _mutate(_evidence(qualifying_junit), "run.binding_sha256", "0" * 64), qualifying_junit
    )

    assert any("does not bind this challenge" in finding for finding in findings)


# --- required fields, counts, skips and named cases -------------------------


@pytest.mark.parametrize("field", checker.REQUIRED_FIELDS)
def test_every_required_evidence_field_is_required(
    field: str, qualifying_junit: Path
) -> None:
    """Each field omitted on its own, and named in the finding."""
    findings = _findings(_without(_evidence(qualifying_junit), field), qualifying_junit)

    assert any("missing required fields" in finding and field in finding for finding in findings)


@pytest.mark.parametrize(
    ("count", "wrong"),
    [("collected", 78), ("collected", 80), ("passed", 63), ("passed", 65),
     ("skipped", 14), ("skipped", 16), ("failed", 1), ("errors", 1)],
)
def test_every_count_mismatch_is_a_finding(
    count: str, wrong: int, qualifying_junit: Path
) -> None:
    """Each of the five totals, moved on its own."""
    findings = _findings(
        _mutate(_evidence(qualifying_junit), f"outcome.{count}", wrong), qualifying_junit
    )

    assert any(f"outcome.{count} is {wrong!r}, not" in finding for finding in findings)


@pytest.mark.parametrize("node", checker.ALLOWED_SKIPPED_NODE_IDS)
def test_every_allowlisted_skip_that_did_not_skip_is_a_finding(
    node: str, qualifying_junit: Path
) -> None:
    """One allowlisted node removed from the skip set, fifteen ways."""
    evidence = _evidence(qualifying_junit)
    remaining = [entry for entry in evidence["outcome"]["skipped_node_ids"] if entry != node]

    findings = _findings(
        _mutate(evidence, "outcome.skipped_node_ids", remaining), qualifying_junit
    )

    assert any("allowlisted cases that did not skip" in finding and node in finding
               for finding in findings)


@pytest.mark.parametrize(
    "addition",
    [f"{checker.DISCOVERY_TEST}::{case}" for case in checker.REQUIRED_WINDOWS_CASES]
    + [f"{checker.DISCOVERY_TEST}::test_platform_neutral_case_0"],
)
def test_every_skip_the_allowlist_does_not_permit_is_a_finding(
    addition: str, qualifying_junit: Path
) -> None:
    """One node added to the skip set, including each required case."""
    evidence = _evidence(qualifying_junit)
    widened = [*evidence["outcome"]["skipped_node_ids"], addition]

    findings = _findings(_mutate(evidence, "outcome.skipped_node_ids", widened), qualifying_junit)

    assert any("does not permit" in finding and addition in finding for finding in findings)


def test_a_duplicated_skip_node_is_a_finding(qualifying_junit: Path) -> None:
    evidence = _evidence(qualifying_junit)
    doubled = [
        *evidence["outcome"]["skipped_node_ids"],
        evidence["outcome"]["skipped_node_ids"][0],
    ]

    findings = _findings(_mutate(evidence, "outcome.skipped_node_ids", doubled), qualifying_junit)

    assert any("duplicated" in finding for finding in findings)


@pytest.mark.parametrize("case", checker.REQUIRED_WINDOWS_CASES)
@pytest.mark.parametrize("multiplicity", [0, 2, 3])
def test_a_required_case_not_collected_exactly_once_is_a_finding(
    case: str, multiplicity: int, qualifying_junit: Path
) -> None:
    """Never collected, and collected more than once."""
    findings = _findings(
        _mutate(_evidence(qualifying_junit), f"named_cases.{case}.collected", multiplicity),
        qualifying_junit,
    )

    assert any(case in finding for finding in findings)
    expected = "never collected" if multiplicity == 0 else f"collected {multiplicity} times"
    assert any(expected in finding for finding in findings)


@pytest.mark.parametrize("case", checker.REQUIRED_WINDOWS_CASES)
@pytest.mark.parametrize("status", ["skipped", "failed", "errored"])
def test_a_required_case_that_did_not_pass_is_a_finding(
    case: str, status: str, qualifying_junit: Path
) -> None:
    findings = _findings(
        _mutate(_evidence(qualifying_junit), f"named_cases.{case}.status", status),
        qualifying_junit,
    )

    assert any(case in finding and status in finding for finding in findings)


@pytest.mark.parametrize("case", checker.REQUIRED_WINDOWS_CASES)
def test_a_required_case_that_the_record_never_carried_is_a_finding(
    case: str, windows_vm: None, tmp_path: Path
) -> None:
    """The same cause, end to end: the record itself is missing the case.

    Proved through the record rather than through the evidence, because the
    original defect is a case that never reached the report at all.
    """
    junit = tmp_path / "junit.xml"
    cases = [entry for entry in _qualifying_cases() if entry[0] != case]
    junit.write_text(_render_junit(cases), encoding="utf-8")

    findings = _findings(_evidence(junit), junit)

    assert any(case in finding and "never collected" in finding for finding in findings)


@pytest.mark.parametrize("case", checker.REQUIRED_WINDOWS_CASES)
def test_a_required_case_the_record_carries_twice_is_a_finding(
    case: str, windows_vm: None, tmp_path: Path
) -> None:
    junit = tmp_path / "junit.xml"
    cases = [*_qualifying_cases(), (case, "passed")]
    junit.write_text(_render_junit(cases), encoding="utf-8")

    findings = _findings(_evidence(junit), junit)

    assert any(case in finding and "collected 2 times" in finding for finding in findings)


# --- the JUnit record itself ------------------------------------------------


JUNIT_CORRUPTIONS: tuple[tuple[str, str | None, str], ...] = (
    ("missing", None, "did not run"),
    ("empty", "", "is empty"),
    ("whitespace", "   \n", "is empty"),
    ("not-xml", "this is not a report", "not parseable XML"),
    ("truncated", '<?xml version="1.0"?><testsuites><testsuite tests="79">', "not parseable XML"),
    ("wrong-root", '<?xml version="1.0"?><report tests="79" />', "root element"),
    ("no-testsuite", '<?xml version="1.0"?><testsuites name="pytest tests" />', "no <testsuite> element"),
    (
        "no-testcase",
        (
            '<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" '
            'failures="0" skipped="0" tests="0" time="1.0" /></testsuites>'
        ),
        "no <testcase> element",
    ),
)


@pytest.mark.parametrize(
    ("label", "content", "expected"),
    JUNIT_CORRUPTIONS,
    ids=[entry[0] for entry in JUNIT_CORRUPTIONS],
)
def test_an_unusable_junit_record_is_a_finding(
    label: str, content: str | None, expected: str, windows_vm: None, tmp_path: Path
) -> None:
    """Missing, empty and malformed records, each proven separately."""
    junit = tmp_path / "junit.xml"
    if content is not None:
        junit.write_text(content, encoding="utf-8")

    with pytest.raises(checker.QualificationError, match=re.escape(expected)):
        checker.parse_junit(junit)

    findings = _findings(_evidence(junit), junit)
    assert any("the JUnit record was not usable" in finding for finding in findings)


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    [
        ("missing-tests-attribute", {"drop": ' tests="79"'}, "no 'tests' attribute"),
        ("non-integer-tests", {"tests": "many"}, "not an integer"),
        ("tests-disagrees-with-cases", {"tests": 80}, "carries 79"),
        ("foreign-classname", {"classname": "tests.test_something_else"}, "reports classname"),
        ("non-numeric-time", {"suite_time": "quickly"}, "not a duration"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_structurally_malformed_junit_record_is_a_finding(
    label: str, kwargs: dict[str, Any], expected: str, windows_vm: None, tmp_path: Path
) -> None:
    junit = tmp_path / "junit.xml"
    drop = kwargs.pop("drop", None)
    document = _render_junit(_qualifying_cases(), **kwargs)
    if drop:
        assert drop in document
        document = document.replace(drop, "", 1)
    junit.write_text(document, encoding="utf-8")

    with pytest.raises(checker.QualificationError, match=re.escape(expected)):
        checker.parse_junit(junit)

    assert any(
        "the JUnit record was not usable" in finding
        for finding in _findings(_evidence(junit), junit)
    )


def test_a_recorded_junit_error_alone_is_a_finding(qualifying_junit: Path) -> None:
    """Absence is failure even when every other field reads as a pass."""
    findings = _findings(
        _mutate(_evidence(qualifying_junit), "outcome.junit_error", "the report vanished"),
        qualifying_junit,
    )

    assert any("the report vanished" in finding for finding in findings)


# --- the safe artifact boundary ---------------------------------------------

#: Every shape that may not leave the guest, each planted in the two places a
#: real record quotes freely: the failure body and the captured output. The last
#: two are this machine's own paths, so the guard is proven against the very
#: values a developer's run would leak rather than only against a synthetic one.
POISON: tuple[tuple[str, str], ...] = (
    ("credential", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----"),
    ("slack-token", "xoxb-1234567890-abcdefghijkl"),
    ("windows-path", "C:\\Users\\qualifier\\AppData\\Local\\Temp\\pytest-of-qualifier"),
    ("unc-path", "\\\\build-share\\drops\\secret.txt"),
    ("posix-path", "/home/qualifier/work/omnivia-core"),
    ("traceback", "Traceback (most recent call last): AssertionError: boom"),
    ("checkout-path", str(REPO_ROOT)),
    ("home-path", str(Path.home())),
)


def _sanitized(junit: Path) -> ET.Element:
    return ET.fromstring(checker.sanitize_junit(junit))


def _suite(junit: Path) -> ET.Element:
    suite = _sanitized(junit).find("testsuite")
    assert suite is not None
    return suite


def _properties(suite: ET.Element) -> dict[str, str]:
    return {
        str(entry.get("name")): str(entry.get("value")) for entry in suite.iter("property")
    }


@pytest.mark.parametrize(("label", "planted"), POISON, ids=[entry[0] for entry in POISON])
def test_no_planted_value_reaches_the_kept_junit_record(
    label: str, planted: str, tmp_path: Path
) -> None:
    """The raw record is required to carry the planted value -- otherwise the test
    proves nothing about redaction -- and the checker is required to have seen it
    there before writing anything, which `sensitive_shapes` reports. The kept
    document must then contain neither that value nor any other shape of the same
    kind.
    """
    cases = [
        (name, "failed" if name == "test_platform_neutral_case_0" else status)
        for name, status in _qualifying_cases()
    ]
    junit = tmp_path / "junit.xml"
    junit.write_text(
        _render_junit(cases, failures=1, detail=planted, system_out=planted), encoding="utf-8"
    )

    assert planted in junit.read_text(encoding="utf-8"), "the poison never reached the record"
    assert checker.sensitive_shapes(junit) > 0, "the raw record was not inspected before redaction"

    sanitized = checker.sanitize_junit(junit)

    assert planted not in sanitized
    assert checker._junit_leaks(sanitized) == []


@pytest.mark.parametrize(("label", "planted"), POISON, ids=[entry[0] for entry in POISON])
def test_a_planted_value_in_a_case_name_is_redacted(
    label: str, planted: str, tmp_path: Path
) -> None:
    """A parametrized id carries whatever the parameter was, including a path."""
    junit = tmp_path / "junit.xml"
    junit.write_text(
        _render_junit([(f"test_parametrized[{planted}]", "passed")], tests=1), encoding="utf-8"
    )

    sanitized = checker.sanitize_junit(junit)

    assert planted not in sanitized
    assert checker._junit_leaks(sanitized) == []


def test_the_kept_record_carries_no_failure_text_at_all(tmp_path: Path) -> None:
    """Not redaction but construction: the message attributes are gone too."""
    cases = [
        (name, {"test_platform_neutral_case_0": "failed", "test_platform_neutral_case_1": "errored"}
         .get(name, status))
        for name, status in _qualifying_cases()
    ]
    junit = tmp_path / "junit.xml"
    junit.write_text(_render_junit(cases, failures=1, errors=1), encoding="utf-8")

    sanitized = checker.sanitize_junit(junit)

    for quoted in ("assertion failed", "collection failed", "boom", "system-out", "pytest.skip"):
        assert quoted not in sanitized, f"the kept record quotes {quoted!r}"
    suite = _suite(junit)
    # The outcome itself survives: the elements are present and empty.
    assert [element.tag for element in suite.findall("testcase/failure")] == ["failure"]
    assert [element.tag for element in suite.findall("testcase/error")] == ["error"]
    assert all(not element.attrib and not element.text for element in suite.iter("failure"))


def test_the_kept_record_still_supports_an_independent_verdict(
    qualifying_junit: Path,
) -> None:
    """What survives redaction has to be enough to re-derive the result.

    Counts, every node id with its status and duration, the total, the checker
    version and the raw record's digest -- so a reviewer holding only the
    artifacts can confirm 79/64/15/0/0, confirm the three required cases ran and
    passed, and tie the document to the raw bytes the runner deleted.
    """
    suite = _suite(qualifying_junit)

    assert suite.get("tests") == "79"
    assert suite.get("failures") == "0"
    assert suite.get("errors") == "0"
    assert suite.get("skipped") == "15"
    assert float(str(suite.get("time"))) == pytest.approx(42.5)

    properties = _properties(suite)
    assert properties["checker_version"] == checker.CHECKER_VERSION
    assert properties["classname"] == checker.DISCOVERY_CLASSNAME
    assert properties["junit_error"] == ""
    assert properties["raw_junit_sha256"] == hashlib.sha256(
        qualifying_junit.read_bytes()
    ).hexdigest()

    cases = suite.findall("testcase")
    assert len(cases) == 79
    outcomes = {
        f"{case.get('classname')}::{case.get('name')}": (
            [child.tag for child in case],
            float(str(case.get("time"))),
        )
        for case in cases
    }
    assert len(outcomes) == 79, "the kept record collapsed two node ids into one"
    for name in checker.REQUIRED_WINDOWS_CASES:
        tags, duration = outcomes[f"{checker.DISCOVERY_CLASSNAME}::{name}"]
        assert tags == [], f"{name} did not pass in the kept record"
        assert duration == pytest.approx(0.25)
    for node in checker.ALLOWED_SKIPPED_NODE_IDS:
        tags, _ = outcomes[f"{checker.DISCOVERY_CLASSNAME}::{node.split('::', 1)[1]}"]
        assert tags == ["skipped"]


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("missing", None),
        ("empty", ""),
        ("not-xml", "this is not a report"),
        ("truncated", '<?xml version="1.0"?><testsuites><testsuite tests="79">'),
        ("wrong-root", '<?xml version="1.0"?><report tests="79" />'),
    ],
    ids=lambda value: value if isinstance(value, str) else "missing",
)
def test_a_sanitized_record_is_produced_even_when_the_raw_one_is_unusable(
    label: str, content: str | None, tmp_path: Path
) -> None:
    """The artifact directory has to hold something, including when nothing ran."""
    junit = tmp_path / "junit.xml"
    if content is not None:
        junit.write_text(content, encoding="utf-8")

    suite = _suite(junit)

    assert suite.get("tests") == "0"
    assert suite.findall("testcase") == []
    properties = _properties(suite)
    assert properties["junit_error"], "the kept record does not say why it is empty"
    assert checker._junit_leaks(checker.sanitize_junit(junit)) == []


def test_the_kept_record_is_reproducible_from_the_raw_one(qualifying_junit: Path) -> None:
    """Digested once and written once; the two calls must agree byte for byte."""
    assert checker.sanitize_junit(qualifying_junit) == checker.sanitize_junit(qualifying_junit)

    evidence = _evidence(qualifying_junit)

    assert evidence["integrity"]["sanitized_junit_sha256"] == hashlib.sha256(
        checker.sanitize_junit(qualifying_junit).encode("utf-8")
    ).hexdigest()
    assert _findings(evidence, qualifying_junit) == []


def test_a_sanitized_digest_that_does_not_cover_the_kept_record_is_a_finding(
    qualifying_junit: Path,
) -> None:
    findings = _findings(
        _mutate(_evidence(qualifying_junit), "integrity.sanitized_junit_sha256", "0" * 64),
        qualifying_junit,
    )

    assert any(
        "integrity.sanitized_junit_sha256" in finding and "this checkout has" in finding
        for finding in findings
    )


def test_a_leaking_kept_record_is_a_finding(qualifying_junit: Path) -> None:
    """The checker's own backstop, proven by feeding it a document that leaks."""
    leaking = (
        '<testsuites><testsuite name="p1b"><testcase name="t">'
        "<failure>Traceback: C:\\Users\\qualifier\\x ghp_0123456789abcdefghijklmnopqrst</failure>"
        "</testcase></testsuite></testsuites>"
    )

    findings = _findings(_evidence(qualifying_junit), qualifying_junit, sanitized=leaking)

    for shape in ("credential", "an absolute path", "failure or exception text"):
        assert any(shape in finding for finding in findings), f"{shape} was not reported"


def test_the_raw_record_is_inspected_before_anything_is_derived_from_it(
    qualifying_junit: Path, tmp_path: Path
) -> None:
    """The count is recorded, not judged: a clean record reads zero."""
    assert _evidence(qualifying_junit)["outcome"]["raw_junit_sensitive_shapes"] == 0

    poisoned = tmp_path / "poisoned.xml"
    poisoned.write_text(
        _render_junit(
            [(name, "failed" if index == 0 else status)
             for index, (name, status) in enumerate(_qualifying_cases())],
            failures=1,
            detail="Traceback (most recent call last): C:\\Users\\qualifier\\x",
        ),
        encoding="utf-8",
    )

    assert checker.sensitive_shapes(poisoned) > 0


def test_the_evidence_carries_no_credential_user_path_or_github_field(
    qualifying_junit: Path,
) -> None:
    evidence = _evidence(qualifying_junit)

    assert _findings(evidence, qualifying_junit) == []
    assert str(Path.home()) not in json.dumps(evidence)
    assert str(REPO_ROOT) not in json.dumps(evidence)
    # The interpreter this checker ran under sits inside the checkout on a
    # developer machine, so its recorded path is the proof the rewrite happened.
    assert str(REPO_ROOT) not in evidence["toolchain"]["python_executable"]
    # `packages` is an inventory of whatever the environment holds, so it is not
    # this lane's vocabulary and is excluded from the marker scan.
    without_inventory = {key: value for key, value in evidence.items() if key != "packages"}
    serialized = json.dumps(without_inventory).lower()
    for marker in ("github", "workflow_ref", "pull_request", "runner_environment"):
        assert marker not in serialized, f"the evidence carries a hosted-CI field: {marker}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("toolchain.python_executable", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"),
        ("toolchain.powershell_executable", str(Path.home() / "bin" / "pwsh")),
        ("host.computer_model", str(REPO_ROOT)),
    ],
)
def test_a_credential_or_local_path_in_the_evidence_is_a_finding(
    field: str, value: str, qualifying_junit: Path
) -> None:
    findings = _findings(_mutate(_evidence(qualifying_junit), field, value), qualifying_junit)

    assert findings


# --- the source scan --------------------------------------------------------


XFAIL_SOURCES = (
    ("marker", "@pytest.mark.xfail\ndef test_windows() -> None:\n    assert False\n"),
    ("marker-with-reason", '@pytest.mark.xfail(reason="windows")\ndef test_windows() -> None: ...\n'),
    ("call", "def test_windows() -> None:\n    pytest.xfail('not yet')\n"),
    ("param-marks", "pytest.param(1, marks=pytest.mark.xfail)\n"),
    ("imported", "from pytest import xfail\n"),
)


@pytest.mark.parametrize(("label", "source"), XFAIL_SOURCES, ids=[entry[0] for entry in XFAIL_SOURCES])
def test_the_source_scan_finds_every_spelling_of_xfail(
    label: str, source: str, tmp_path: Path
) -> None:
    module = tmp_path / "test_discovery.py"
    module.write_text(source, encoding="utf-8")

    assert checker.scan_xfail(module) >= 1


def test_the_discovery_suite_carries_no_xfail_today() -> None:
    assert checker.scan_xfail(REPO_ROOT / checker.DISCOVERY_TEST) == 0


def test_an_xfail_in_the_source_is_a_finding(qualifying_junit: Path) -> None:
    """The one construct that makes a failure look like a pass."""
    findings = _findings(
        _mutate(_evidence(qualifying_junit), "outcome.xfail_markers_or_calls_in_source", 1),
        qualifying_junit,
    )

    assert any("xfail marker(s) or call(s)" in finding for finding in findings)


# --- immutable code identity ------------------------------------------------


IDENTITY_FIELDS = (
    "run.requested_commit",
    "code_identity.tested_commit",
    "code_identity.tested_tree",
    "code_identity.discovery_test_blob",
    "code_identity.runner_script_blob",
    "code_identity.checker_script_blob",
    "integrity.junit_sha256",
)


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_every_recorded_identity_is_checked_against_the_checkout(
    field: str, qualifying_junit: Path
) -> None:
    """Commit, tree, each of the three blobs, and the raw record's own digest."""
    findings = _findings(
        _mutate(_evidence(qualifying_junit), field, "0" * 40), qualifying_junit
    )

    assert any(field in finding and "this checkout has" in finding for finding in findings)


def test_the_code_identity_is_re_derived_from_the_checkout() -> None:
    """The three files whose bytes decide what this qualification is about."""
    identity = checker.code_identity(REPO_ROOT)

    assert re.fullmatch(r"[0-9a-f]{40}", identity["tested_commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", identity["tested_tree"])
    for name, path in (
        ("discovery_test_blob", REPO_ROOT / checker.DISCOVERY_TEST),
        ("runner_script_blob", RUNNER),
        ("checker_script_blob", CHECKER),
    ):
        expected = subprocess.run(
            ["git", "hash-object", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert identity[name] == expected


def test_a_payload_digest_that_does_not_cover_the_payload_is_a_finding(
    qualifying_junit: Path,
) -> None:
    evidence = _evidence(qualifying_junit)
    evidence["outcome"]["collected"] = 79  # unchanged; only the digest is wrong
    evidence["integrity"]["evidence_sha256"] = "0" * 64

    findings = _findings(evidence, qualifying_junit)

    assert any("does not cover this payload" in finding for finding in findings)


def test_a_requested_commit_that_is_not_the_checked_out_one_is_a_finding(
    windows_vm: None, qualifying_junit: Path
) -> None:
    """The expected commit must equal both the requested commit and `HEAD`."""
    other = "0" * 40
    evidence = checker.build_evidence(
        root=REPO_ROOT,
        junit=qualifying_junit,
        expected_commit=other,
        challenge=CHALLENGE,
        facts=VM_FACTS,
    )

    findings = _findings(evidence, qualifying_junit, expected_commit=other)

    assert any("is not the checked-out HEAD" in finding for finding in findings)


def test_the_recorded_checker_version_is_pinned(qualifying_junit: Path) -> None:
    findings = _findings(
        _mutate(_evidence(qualifying_junit), "checker_version", "4"), qualifying_junit
    )

    assert any("checker_version is '4', not '5'" in finding for finding in findings)


@pytest.mark.parametrize("package", checker.REQUIRED_PACKAGES)
@pytest.mark.parametrize("version", ["", "   "], ids=["absent", "blank"])
def test_a_missing_required_distribution_is_a_finding(
    package: str, version: str, qualifying_junit: Path
) -> None:
    """An inventory without these could not have collected the file at all."""
    evidence = _evidence(qualifying_junit)
    inventory = dict(evidence["packages"])
    if version:
        inventory[package] = version
    else:
        inventory.pop(package, None)

    findings = _findings(_mutate(evidence, "packages", inventory), qualifying_junit)

    assert any(package in finding and "no installed version" in finding for finding in findings)


def test_a_package_inventory_that_is_not_an_inventory_is_a_finding(
    qualifying_junit: Path,
) -> None:
    findings = _findings(
        _mutate(_evidence(qualifying_junit), "packages", ["omnivia-core"]), qualifying_junit
    )

    assert any("not an inventory" in finding for finding in findings)


def test_the_qualifying_inventory_lists_the_distributions_the_suite_imports(
    qualifying_junit: Path,
) -> None:
    """The root and client distributions, by name, off this interpreter."""
    packages = _evidence(qualifying_junit)["packages"]

    for name in ("omnivia-core", "omnivia-core-client"):
        assert packages.get(name), f"the inventory does not record {name}"


# ---------------------------------------------------------------------------
# The two phases, end to end
# ---------------------------------------------------------------------------


def _produce(
    tmp_path: Path,
    junit: Path,
    *,
    challenge: str = CHALLENGE,
    commit: str | None = None,
    facts: Path | None = None,
) -> tuple[int, Path, Path]:
    sanitized = tmp_path / "artifacts" / checker.SANITIZED_JUNIT_NAME
    evidence = tmp_path / "artifacts" / checker.EVIDENCE_NAME
    status = checker.main(
        [
            "produce",
            "--junit", str(junit),
            "--facts", str(_facts_file(tmp_path) if facts is None else facts),
            "--sanitized-junit", str(sanitized),
            "--evidence", str(evidence),
            "--expected-commit", _head_commit() if commit is None else commit,
            "--challenge", challenge,
            "--root", str(REPO_ROOT),
        ]
    )
    return status, sanitized, evidence


#: A complete fact set for the other recognized provider. `PROVIDER_IDENTITIES`
#: proves each VMware string on its own; this proves a whole VMware guest through
#: both phases, so retaining that provider is a working path rather than a rule
#: nothing exercises end to end.
VMWARE_FACTS: dict[str, str] = {
    **VM_FACTS,
    "computer_manufacturer": "VMware, Inc.",
    "computer_model": "VMware20,1",
    "bios_vendor": "VMware, Inc.",
    "guest_tools_service": "VMware Tools",
    "guest_tools_version": "12.5.0.24276846",
}


def test_a_guest_of_either_recognized_provider_qualifies_through_both_phases(
    qualifying_junit: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The VMware path, whole: produced on the guest and verified offline.

    The GO line is asserted too, because it is the one place the lane says out
    loud which provider it accepted -- and a message that named a vendor
    regardless of the facts would be the same defect as a rule that did.
    """
    status, sanitized, evidence = _produce(
        tmp_path, qualifying_junit, facts=_facts_file(tmp_path, **VMWARE_FACTS)
    )

    assert status == 0
    monkeypatch.undo()
    assert _verify(sanitized, evidence) == 0
    assert "'vmware'" in capsys.readouterr().out


@pytest.mark.parametrize("elsewhere", ["Darwin", "Linux"])
def test_producing_off_windows_is_refused_and_writes_nothing(
    elsewhere: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 1 runs on the guest. Anywhere else it must not produce a record.

    PowerShell 7 runs on macOS and Linux, and the runner refuses there too, but
    the checker is the thing that writes the evidence and it refuses on its own
    account -- so a hand-run `produce` on the verifier's laptop cannot manufacture
    an artifact to verify.
    """
    monkeypatch.setattr(checker.platform, "system", lambda: elsewhere)
    junit = tmp_path / "junit.xml"
    junit.write_text(_render_junit(_qualifying_cases()), encoding="utf-8")

    status, sanitized, evidence = _produce(tmp_path, junit)

    assert status == 1
    assert not sanitized.exists(), "a refused run still wrote a JUnit artifact"
    assert not evidence.exists(), "a refused run still wrote evidence"


def test_the_checker_exits_zero_and_writes_exactly_two_artifacts(
    qualifying_junit: Path, tmp_path: Path
) -> None:
    """Phase 1's happy path, through the same entry point the runner calls."""
    status, sanitized, evidence = _produce(tmp_path, qualifying_junit)

    assert status == 0
    assert sorted(path.name for path in sanitized.parent.iterdir()) == [
        checker.EVIDENCE_NAME,
        checker.SANITIZED_JUNIT_NAME,
    ]

    recorded = json.loads(evidence.read_text(encoding="utf-8"))
    assert recorded["verdict"]["result"] == "GO"
    assert recorded["verdict"]["findings"] == []
    assert recorded["verdict"]["expected"]["collected"] == 79
    assert recorded["verdict"]["expected"]["skipped_node_ids"] == list(
        checker.ALLOWED_SKIPPED_NODE_IDS
    )
    assert set(recorded["verdict"]["expected"]["local_vm_context"]) == set(LOCAL_VM_PATHS)
    assert recorded["run"]["producer"] == "local-windows-vm"
    # The verdict states the provider rule it was decided under, so a stored
    # record says which hypervisors were acceptable when it was written.
    provider_rule = recorded["verdict"]["expected"]["local_vm_provider"]
    assert provider_rule["recognized"] == list(checker.RECOGNIZED_PROVIDERS)
    assert provider_rule["named_by_any_of"] == [
        "computer_manufacturer",
        "computer_model",
        "bios_vendor",
        "guest_tools_service",
        "guest_tools_version",
    ]

    uploaded = sanitized.read_text(encoding="utf-8")
    assert checker._junit_leaks(uploaded) == []
    assert recorded["integrity"]["sanitized_junit_sha256"] == hashlib.sha256(
        uploaded.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("label", "cases"),
    [
        ("a required windows case failed", [
            (name, "failed" if name == checker.REQUIRED_WINDOWS_CASES[0] else status)
            for name, status in _qualifying_cases()
        ]),
        ("a required windows case skipped", [
            (name, "skipped" if name == checker.REQUIRED_WINDOWS_CASES[1] else status)
            for name, status in _qualifying_cases()
        ]),
        ("a platform-neutral case failed", [
            (name, "failed" if name == "test_platform_neutral_case_0" else status)
            for name, status in _qualifying_cases()
        ]),
    ],
    ids=["required-case-failed", "required-case-skipped", "neutral-case-failed"],
)
def test_the_checker_exits_non_zero_and_still_writes_evidence_for_a_failing_run(
    windows_vm: None, label: str, cases: list[tuple[str, str]], tmp_path: Path
) -> None:
    """The run that failed is the run whose evidence is worth reading.

    The failing record carries a traceback and a guest path, as a real one would,
    so this is also the end-to-end proof that a failed run's artifacts carry
    neither.
    """
    junit = tmp_path / "junit.xml"
    junit.write_text(
        _render_junit(
            cases,
            failures=1,
            detail="Traceback (most recent call last): C:\\Users\\qualifier\\x",
            system_out="ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        ),
        encoding="utf-8",
    )

    status, sanitized, evidence = _produce(tmp_path, junit)

    assert status == 1
    recorded = json.loads(evidence.read_text(encoding="utf-8"))
    assert recorded["verdict"]["result"] == "NO-GO"
    assert recorded["verdict"]["findings"]

    uploaded = sanitized.read_text(encoding="utf-8")
    assert checker._junit_leaks(uploaded) == []
    assert "qualifier" not in uploaded
    assert "ghp_" not in uploaded
    assert uploaded.count("<testcase") == 79


def test_the_checker_exits_non_zero_when_the_suite_never_ran(
    windows_vm: None, tmp_path: Path
) -> None:
    """The step the runner lets fail leaves no report; the verdict stays red."""
    status, sanitized, evidence = _produce(tmp_path, tmp_path / "absent.xml")

    assert status == 1
    recorded = json.loads(evidence.read_text(encoding="utf-8"))
    assert recorded["verdict"]["result"] == "NO-GO"
    assert any("did not run" in finding for finding in recorded["verdict"]["findings"])
    # And the artifacts still exist, so a run that produced no record at all
    # still leaves something that says so.
    assert sanitized.is_file()
    assert checker._junit_leaks(sanitized.read_text(encoding="utf-8")) == []


# --- phase 2: offline verification ------------------------------------------


def _verify(
    sanitized: Path,
    evidence: Path,
    *,
    challenge: str = CHALLENGE,
    commit: str | None = None,
) -> int:
    return cast(
        "int",
        checker.main(
            [
                "verify",
                "--sanitized-junit", str(sanitized),
                "--evidence", str(evidence),
                "--expected-commit", _head_commit() if commit is None else commit,
                "--challenge", challenge,
                "--root", str(REPO_ROOT),
            ]
        ),
    )


def test_the_artifacts_verify_offline_with_the_windows_simulation_undone(
    qualifying_junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2's whole claim: the two files verify on a machine that is not the guest.

    The `windows_vm` fixture's patches are undone before verification, so what is
    checked afterwards is the artifacts and this checkout -- no simulated
    platform, no simulated volumes, and nothing from the guest beyond the two
    files. `monkeypatch` is function-scoped, so undoing it here undoes exactly
    what the fixture set up.
    """
    status, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    assert status == 0

    monkeypatch.undo()

    findings, recorded = checker.verify(
        root=REPO_ROOT,
        sanitized_junit=sanitized,
        evidence_path=evidence,
        expected_commit=_head_commit(),
        challenge=CHALLENGE,
    )

    # `findings` is this verification's own conclusion; `verdict` is the one the
    # producer stamped into the file. Both are asserted, because a stored GO that
    # a verifier cannot reproduce is exactly the thing phase 2 exists to catch.
    assert findings == []
    assert recorded is not None
    assert recorded["verdict"]["result"] == "GO"
    assert _verify(sanitized, evidence) == 0


def test_offline_verification_recomputes_the_four_things_it_can(
    qualifying_junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sanitized digest, the payload digest, the challenge and the code identity.

    Each is recomputed here the same way the verifier does, from the artifact
    bytes and this checkout, and required to equal what the record claims.
    """
    _, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    monkeypatch.undo()
    recorded = json.loads(evidence.read_text(encoding="utf-8"))

    assert recorded["integrity"]["sanitized_junit_sha256"] == hashlib.sha256(
        sanitized.read_bytes()
    ).hexdigest()
    assert recorded["integrity"]["evidence_sha256"] == checker.evidence_digest(recorded)
    assert recorded["run"]["challenge_sha256"] == checker.challenge_digest(CHALLENGE)
    identity = checker.code_identity(REPO_ROOT)
    for name, value in identity.items():
        assert recorded["code_identity"][name] == value
    assert recorded["run"]["binding_sha256"] == checker.binding_digest(
        challenge_sha256=checker.challenge_digest(CHALLENGE),
        tested_commit=identity["tested_commit"],
        junit_sha256=recorded["integrity"]["junit_sha256"],
        sanitized_junit_sha256=hashlib.sha256(sanitized.read_bytes()).hexdigest(),
    )


def test_offline_verification_refuses_another_verifiers_challenge(
    qualifying_junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record answers the challenge it was given, and no other."""
    _, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    monkeypatch.undo()

    findings, _ = checker.verify(
        root=REPO_ROOT,
        sanitized_junit=sanitized,
        evidence_path=evidence,
        expected_commit=_head_commit(),
        challenge=OTHER_CHALLENGE,
    )

    assert any("run.challenge_sha256" in finding for finding in findings)
    assert any("does not bind this challenge" in finding for finding in findings)
    assert _verify(sanitized, evidence, challenge=OTHER_CHALLENGE) == 1


def test_offline_verification_refuses_another_commit(
    qualifying_junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    monkeypatch.undo()

    findings, _ = checker.verify(
        root=REPO_ROOT,
        sanitized_junit=sanitized,
        evidence_path=evidence,
        expected_commit="0" * 40,
        challenge=CHALLENGE,
    )

    assert any("is not the checked-out HEAD" in finding for finding in findings)


TAMPERINGS: tuple[tuple[str, str, str, str], ...] = (
    # The sanitized record, edited so a required case reads as a skip.
    (
        "sanitized-case-skipped",
        "sanitized",
        (
            f'<testcase classname="{checker.DISCOVERY_CLASSNAME}" '
            f'name="{checker.REQUIRED_WINDOWS_CASES[0]}" time="0.250000" />'
        ),
        (
            f'<testcase classname="{checker.DISCOVERY_CLASSNAME}" '
            f'name="{checker.REQUIRED_WINDOWS_CASES[0]}" time="0.250000"><skipped /></testcase>'
        ),
    ),
    # The sanitized record's headline count, edited to hide a missing case.
    ("sanitized-count", "sanitized", 'tests="79"', 'tests="78"'),
    # A single digit of the raw record's digest, which the binding covers.
    ("evidence-raw-digest", "evidence", '"junit_sha256": "', '"junit_sha256": "0'),
    # The host block, edited after the fact to name a different provider. The
    # payload digest covers it, so the pair stops verifying.
    #
    # What this does *not* establish, and the module docstring says so: a
    # producer who wrote whatever facts it liked *before* the digest was computed
    # is not something a verifier can detect. The host facts are the producer's
    # report, bound and unchanged since -- not witnessed.
    (
        "evidence-host-provider",
        "evidence",
        '"computer_model": "Parallels Virtual Platform"',
        '"computer_model": "VMware20,1"',
    ),
)


@pytest.mark.parametrize(
    ("label", "target", "original", "replacement"),
    TAMPERINGS,
    ids=[entry[0] for entry in TAMPERINGS],
)
def test_a_tampered_artifact_does_not_verify(
    label: str,
    target: str,
    original: str,
    replacement: str,
    qualifying_junit: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either file edited after the fact, with the other left untouched."""
    _, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    monkeypatch.undo()
    assert _verify(sanitized, evidence) == 0, "the untampered pair must verify first"

    path = sanitized if target == "sanitized" else evidence
    text = path.read_text(encoding="utf-8")
    assert original in text, f"the tampering no longer applies: {original!r}"
    path.write_text(text.replace(original, replacement, 1), encoding="utf-8")

    assert _verify(sanitized, evidence) == 1


def test_evidence_re_stamped_to_hide_a_tampered_record_still_does_not_verify(
    qualifying_junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-check between the two files, which no re-stamping can satisfy.

    An editor who can recompute `integrity.evidence_sha256` -- anyone, since the
    payload is public -- still cannot make the two files agree without also
    holding the challenge the binding covers. Here the evidence is edited to say a
    required case skipped and re-stamped honestly; the sanitized record still says
    it passed, and the binding still covers the sanitized digest.
    """
    _, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    monkeypatch.undo()

    recorded = json.loads(evidence.read_text(encoding="utf-8"))
    del recorded["verdict"]
    recorded["named_cases"][checker.REQUIRED_WINDOWS_CASES[0]]["status"] = "skipped"
    recorded["integrity"]["evidence_sha256"] = checker.evidence_digest(recorded)
    evidence.write_text(json.dumps(recorded, indent=2, sort_keys=True), encoding="utf-8")

    findings, _ = checker.verify(
        root=REPO_ROOT,
        sanitized_junit=sanitized,
        evidence_path=evidence,
        expected_commit=_head_commit(),
        challenge=CHALLENGE,
    )

    assert any("must pass" in finding for finding in findings)
    assert any("disagree about the result" in finding for finding in findings)


MISSING_OR_MALFORMED: tuple[tuple[str, str | None, str | None, str], ...] = (
    ("no-sanitized-record", None, "{}", "no sanitized JUnit record"),
    ("no-evidence", "<testsuites />", None, "no evidence"),
    ("evidence-not-json", "<testsuites />", "{not json", "not readable JSON"),
    ("evidence-not-an-object", "<testsuites />", "[1, 2, 3]", "not a JSON object"),
)


@pytest.mark.parametrize(
    ("label", "sanitized_text", "evidence_text", "expected"),
    MISSING_OR_MALFORMED,
    ids=[entry[0] for entry in MISSING_OR_MALFORMED],
)
def test_offline_verification_fails_closed_on_missing_or_malformed_input(
    label: str,
    sanitized_text: str | None,
    evidence_text: str | None,
    expected: str,
    tmp_path: Path,
) -> None:
    """Nothing to read is a NO-GO, never a pass by absence."""
    sanitized = tmp_path / checker.SANITIZED_JUNIT_NAME
    evidence = tmp_path / checker.EVIDENCE_NAME
    if sanitized_text is not None:
        sanitized.write_text(sanitized_text, encoding="utf-8")
    if evidence_text is not None:
        evidence.write_text(evidence_text, encoding="utf-8")

    findings, recorded = checker.verify(
        root=REPO_ROOT,
        sanitized_junit=sanitized,
        evidence_path=evidence,
        expected_commit=_head_commit(),
        challenge=CHALLENGE,
    )

    assert any(expected in finding for finding in findings)
    assert recorded is None
    assert _verify(sanitized, evidence) == 1


def test_a_sanitized_record_that_is_not_readable_as_a_result_is_a_finding(
    qualifying_junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sanitized, evidence = _produce(tmp_path, qualifying_junit)
    monkeypatch.undo()
    sanitized.write_text("<testsuites />", encoding="utf-8")

    findings, _ = checker.verify(
        root=REPO_ROOT,
        sanitized_junit=sanitized,
        evidence_path=evidence,
        expected_commit=_head_commit(),
        challenge=CHALLENGE,
    )

    assert any("not readable as a result" in finding for finding in findings)


@pytest.mark.parametrize("mode", ["produce", "verify"])
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--expected-commit", "not-a-commit"),
        ("--expected-commit", "0" * 39),
        ("--expected-commit", "A" * 40),
        ("--challenge", "short"),
        ("--challenge", "0" * 63),
        ("--challenge", "Z" * 64),
    ],
)
def test_a_malformed_commit_or_challenge_is_rejected_at_the_command_line(
    mode: str, flag: str, value: str, tmp_path: Path
) -> None:
    """Fail closed before anything is read, in both modes."""
    arguments = {
        "--sanitized-junit": str(tmp_path / "junit.sanitized.xml"),
        "--evidence": str(tmp_path / "evidence.json"),
        "--expected-commit": _head_commit(),
        "--challenge": CHALLENGE,
    }
    if mode == "produce":
        arguments["--junit"] = str(tmp_path / "junit.xml")
        arguments["--facts"] = str(tmp_path / "facts.json")
    arguments[flag] = value

    with pytest.raises(SystemExit) as exit_status:
        checker.main([mode, *(item for pair in arguments.items() for item in pair)])

    assert exit_status.value.code == 2
