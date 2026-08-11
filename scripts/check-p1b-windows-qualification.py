#!/usr/bin/env python3
"""Build and check the P1b native-Windows qualification evidence.

`packages/omnivia-core-client/tests/test_discovery.py` carries three cases
guarded by `os.name != "nt"`, and there is no accepted native-Windows
qualification of the current baseline for them. Every run of that file on a
non-Windows machine skips them, where a skip reads exactly like a pass in a
summary line.

This lane closes that with two phases and no dependency on GitHub Actions,
GitHub billing, or any hosted runner:

* **produce** runs on a local Windows VM -- Parallels Desktop, or any other
  provider in `RECOGNIZED_PROVIDERS` -- driven by
  `scripts/run-p1b-windows-qualification.ps1` in native Windows PowerShell. It
  reads the raw JUnit record the suite wrote to a private temporary directory
  and the CIM facts PowerShell collected, and writes exactly two files into
  `artifacts/p1b-windows-qualification/`: a sanitized JUnit record and the
  evidence JSON.
* **verify** runs anywhere -- macOS included -- offline, against those two files
  plus the expected commit and the challenge. It re-derives every digest and
  identity it can and reports the rest as recorded-but-unwitnessed.

It measures the tree. It does not change it: no product code and no discovery
test is touched by this lane, and a red result here is a finding to be reported
rather than something this script may work around.

Absence is failure. Every code path that cannot establish a fact records the
reason and returns non-zero; there is no path that treats "could not tell" as a
pass. The counts, the exact skip set and the three native Windows cases are
pinned by value, and anything else -- a missing record, an unparseable one, a
case collected twice, an `xfail` added to the source to make a failure look like
a pass -- is a finding.

Counts alone prove nothing about *where* a record came from: a hand-written
JUnit file reproduces 79/64/15/0/0 exactly. `_LOCAL_VM_CONTEXT` is why that is
not enough. A GO additionally requires the material facts of a real Windows
guest -- Windows by name from `platform` and from `Win32_OperatingSystem`,
CPython of the pinned series, PowerShell, a hypervisor actually present, and a
recognized local VM provider named by the manufacturer, the model, the BIOS
vendor or the guest tools -- each present, nonempty and, where it names a
platform, actually Windows.

The provider rules are hypervisor-neutral by construction. The facts PowerShell
collects are the generic CIM identity strings and an optional generic guest-tools
signal; which hypervisor those strings name is decided here, in
`RECOGNIZED_PROVIDERS`, rather than baked into the collector. Parallels Desktop
and VMware are what this lane recognizes today, and adding a third is one entry
in that tuple.

What the challenge does, and what it does not.
----------------------------------------------
The verifier picks a fresh 64-hex challenge and hands it to the producer before
the run. The evidence records `sha256(challenge)` rather than the challenge, and
`run.binding_sha256` digests that together with the tested commit and both JUnit
digests. Offline verification recomputes both from the challenge it chose, so a
record built before the challenge existed, or built for a different challenge,
cannot be presented for this one.

That is **local attestation and nothing more**. The producer owns the machine
this ran on, so it could in principle have fabricated every fact below and
digested the result honestly. What the binding establishes is that these
artifacts were produced by a run that held this challenge, and that neither file
has moved since. It is not a remote attestation, not a hardware root of trust,
and not a proof that the VM was real. Do not read it as one.

Safety of what is written.
--------------------------
The raw record pytest writes carries failure messages, tracebacks, captured
output and absolute paths, so it is written to a private temporary directory the
runner deletes, and never into the artifact tree. What is written is
`sanitize_junit`'s document, built by construction from the parsed structure --
node ids, status, timing, counts and the raw record's digest, and nothing that
came out of a test's free text. It is produced for a missing, malformed or
failing record too, so the artifact is never the thing that is absent.

The evidence carries no token, no credential and no user-specific absolute path:
`_normalize_path` rewrites the checkout and the home directory to markers, and
`_leaks` re-checks the finished payload before the verdict is attached.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Bumped whenever the shape of the evidence or the meaning of a check changes,
#: so a stored record says which rules produced its verdict.
#:
#: `5` is the hypervisor-neutral shape. `4` carried VMware-named fields --
#: `host.vmware_tools_service` and `host.vmware_tools_version` -- and treated
#: `host.hypervisor_present` as recorded-but-uninterpreted; `5` carries the
#: generic `host.guest_tools_service` and `host.guest_tools_version`, requires
#: the hypervisor flag to be true, and requires a provider in
#: `RECOGNIZED_PROVIDERS`. `3` and earlier described a hosted GitHub Actions job
#: and required fields -- the run id, the workflow reference, the pull request --
#: that no longer exist. None of those is a later record with fields missing, so
#: the version is compared by value rather than by range.
CHECKER_VERSION = "5"

#: This lane's own identity, and the producer that is allowed to have made the
#: evidence. `local-windows-vm` is the only value: a record claiming any other
#: producer is describing a run these rules were not written for.
QUALIFICATION_ID = "p1b-windows-qualification"
PRODUCER = "local-windows-vm"

DISCOVERY_TEST = "packages/omnivia-core-client/tests/test_discovery.py"
RUNNER_SCRIPT = "scripts/run-p1b-windows-qualification.ps1"
CHECKER_SCRIPT = "scripts/check-p1b-windows-qualification.py"

#: The only directory this lane writes, and the two files it may leave there.
ARTIFACT_DIR = "artifacts/p1b-windows-qualification"
SANITIZED_JUNIT_NAME = "junit.sanitized.xml"
EVIDENCE_NAME = "evidence.json"

#: The interpreter series the qualifying result belongs to. 79/64/15/0/0 is what
#: this file collects under 3.11; 3.12 and 3.13 are separate baselines that have
#: not been qualified. `scripts/run-p1b-windows-qualification.ps1` requires the
#: same series of the interpreter it selects, and
#: `tests/test_p1b_windows_qualification.py` holds the two together.
PYTHON_SERIES = "3.11"

#: Prefixed into the binding digest so the value cannot be confused with, or
#: replayed from, any other digest this repository computes.
BINDING_LABEL = "p1b-windows-qualification-binding"

#: pytest's JUnit writer carries no file attribute; `classname` is the module
#: path with separators as dots and the suffix dropped. One file is collected,
#: so the mapping back to node ids is exact rather than a guess -- and a case
#: reported under any other `classname` means the record is not this suite's.
DISCOVERY_CLASSNAME = DISCOVERY_TEST[: -len(".py")].replace("/", ".")

#: The qualifying result, by value. 79 is what the file collects; 15 is the
#: POSIX-only set below; 64 is the remainder, which includes the three native
#: Windows cases this lane exists to qualify.
EXPECTED_COUNTS: dict[str, int] = {
    "collected": 79,
    "passed": 64,
    "skipped": 15,
    "failed": 0,
    "errors": 0,
}

#: Every node the Windows run may skip, byte-pinned by full node id. A skip that
#: is not on this list, and an allowlisted node that did not skip, are both
#: findings: the first is a case silently not running, the second means the
#: platform predicate moved and the counts above no longer describe the suite.
ALLOWED_SKIPPED_NODE_IDS: tuple[str, ...] = (
    f"{DISCOVERY_TEST}::test_posix_refuses_a_non_regular_descriptor_without_waiting_for_a_writer[fifo]",
    f"{DISCOVERY_TEST}::test_posix_refuses_a_non_regular_descriptor_without_waiting_for_a_writer[socket]",
    f"{DISCOVERY_TEST}::test_posix_rejects_symlinks_wrong_file_kinds_and_accessible_or_foreign_files",
    f"{DISCOVERY_TEST}::test_posix_rejects_group_or_other_access_on_each_derived_parent[0o750]",
    f"{DISCOVERY_TEST}::test_posix_rejects_group_or_other_access_on_each_derived_parent[0o701]",
    f"{DISCOVERY_TEST}::test_posix_rejects_group_or_other_access_on_each_derived_parent[0o711]",
    f"{DISCOVERY_TEST}::test_posix_rejects_any_group_or_other_access_on_the_descriptor_file[0o640]",
    f"{DISCOVERY_TEST}::test_posix_rejects_any_group_or_other_access_on_the_descriptor_file[0o644]",
    f"{DISCOVERY_TEST}::test_posix_rejects_any_group_or_other_access_on_the_descriptor_file[0o666]",
    f"{DISCOVERY_TEST}::test_posix_refuses_a_real_foreign_owned_directory_and_accepts_an_owned_one",
    f"{DISCOVERY_TEST}::test_posix_real_foreign_owner_is_refused_through_the_public_discovery_path",
    f"{DISCOVERY_TEST}::test_posix_mocked_owner_mismatch_is_rejected_through_the_public_discovery_path",
    f"{DISCOVERY_TEST}::test_posix_rejects_symlinked_runtime_and_workspace_parents",
    f"{DISCOVERY_TEST}::test_real_posix_descriptor_provenance_uses_native_owner_mode_and_file_kind",
    f"{DISCOVERY_TEST}::test_descriptor_replacement_during_the_bounded_read_fails_closed",
)

#: The three cases with no accepted native-Windows qualification of the current
#: baseline. Each must be collected exactly once and pass. Collected zero times
#: is the original defect; collected more than once means something duplicated
#: the node and one of the copies could be the one that passed.
REQUIRED_WINDOWS_CASES: tuple[str, ...] = (
    "test_windows_accepts_owner_equivalent_dacl_and_refuses_foreign_or_null_dacl",
    "test_windows_refuses_wrong_file_kind_and_reparse_point_via_public_discovery",
    "test_windows_refuses_descriptor_replacement_race_via_public_discovery",
)

#: What PowerShell must hand over, because CIM and the Windows service database
#: are the two things Python on the guest cannot read for itself. Every value is
#: a string: `load_facts` refuses anything else rather than coercing it, so a
#: `null` from `ConvertTo-Json` for an absent property is a malformed facts file
#: and not an empty fact.
#:
#: Every name here is generic. The identity strings are the raw CIM properties as
#: reported, and `guest_tools_service`/`guest_tools_version` describe whichever
#: guest-tools service the guest happens to run -- Parallels Tools, VMware Tools
#: or none. Both may legitimately be empty: a guest whose CIM identity already
#: names a recognized provider does not need tools installed, so the key must be
#: present and the value may be "".
FACT_KEYS: tuple[str, ...] = (
    "windows_caption",
    "windows_version",
    "windows_build",
    "computer_manufacturer",
    "computer_model",
    "bios_vendor",
    "hypervisor_present",
    "guest_tools_service",
    "guest_tools_version",
    "powershell_edition",
    "powershell_executable",
    "powershell_version",
    "checkout_status",
)

#: Every field the evidence must carry, as a dotted path. Presence is checked
#: separately from value, so a field that is absent is reported as absent rather
#: than as an empty string. `named_cases` and `timing.named_case_seconds` are
#: keyed by case name rather than fixed, so their leaves are generated from the
#: tuple above.
_BASE_REQUIRED_FIELDS: tuple[str, ...] = (
    "checker_version",
    "run.qualification_id",
    "run.producer",
    "run.runner_script",
    "run.checker_script",
    "run.requested_commit",
    "run.challenge_sha256",
    "run.binding_sha256",
    "code_identity.tested_commit",
    "code_identity.tested_tree",
    "code_identity.tested_status",
    "code_identity.discovery_test_blob",
    "code_identity.runner_script_blob",
    "code_identity.checker_script_blob",
    "host.facts_error",
    "host.os_name",
    "host.os_release",
    "host.os_version",
    "host.architecture",
    "host.windows_caption",
    "host.windows_version",
    "host.windows_build",
    "host.computer_manufacturer",
    "host.computer_model",
    "host.bios_vendor",
    "host.hypervisor_present",
    "host.guest_tools_service",
    "host.guest_tools_version",
    "toolchain.python_executable",
    "toolchain.python_version",
    "toolchain.python_implementation",
    "toolchain.sqlite_version",
    "toolchain.powershell_edition",
    "toolchain.powershell_executable",
    "toolchain.powershell_version",
    "filesystem.checkout_root",
    "filesystem.checkout_filesystem",
    "filesystem.temp_root",
    "filesystem.temp_filesystem",
    "packages",
    "outcome.junit_error",
    "outcome.collected",
    "outcome.passed",
    "outcome.failed",
    "outcome.errors",
    "outcome.skipped",
    "outcome.skipped_node_ids",
    "outcome.unexpected_skipped_node_ids",
    "outcome.missing_skipped_node_ids",
    "outcome.xfail_markers_or_calls_in_source",
    "outcome.raw_junit_sensitive_shapes",
    "named_cases",
    "timing.total_seconds",
    "timing.named_case_seconds",
    "integrity.junit_sha256",
    "integrity.sanitized_junit_sha256",
    "integrity.evidence_sha256",
)

REQUIRED_FIELDS: tuple[str, ...] = (
    *_BASE_REQUIRED_FIELDS,
    *(
        f"named_cases.{case}.{leaf}"
        for case in REQUIRED_WINDOWS_CASES
        for leaf in ("collected", "status", "duration_seconds")
    ),
    *(f"timing.named_case_seconds.{case}" for case in REQUIRED_WINDOWS_CASES),
)

#: The distributions the qualifying run must have had installed, with a version.
#: The two local ones are what the discovery suite imports -- an inventory that
#: does not list them describes an environment that could not have collected the
#: file -- and `pytest` is what produced the record being read.
REQUIRED_PACKAGES: tuple[str, ...] = ("omnivia-core", "omnivia-core-client", "pytest")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _commit_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _exactly(expected: str) -> Callable[[Any], bool]:
    return lambda value: value == expected


def _python_series(value: Any) -> bool:
    """A released `PYTHON_SERIES` patch version, and nothing else.

    Nonempty is not enough: `3.12.4`, `3.10.14` and a bare `3.11` are all
    nonempty strings, and the first two are a different baseline while the third
    is not a version an interpreter reports.
    """
    return (
        isinstance(value, str)
        and re.fullmatch(rf"{re.escape(PYTHON_SERIES)}\.\d+", value) is not None
    )


def _windows_caption(value: Any) -> bool:
    """`Win32_OperatingSystem.Caption`, which names Windows on a Windows guest.

    Nonempty would accept `Ubuntu 24.04` from a facts file assembled by hand.
    The edition is deliberately not pinned: Home, Pro, Enterprise and Server all
    run the three cases identically, and pinning one would make the lane fail on
    a VM that differs only in the licence.
    """
    return isinstance(value, str) and "windows" in value.lower()


def _hypervisor_present(value: Any) -> bool:
    """`Win32_ComputerSystem.HypervisorPresent`, as the guest reports it.

    The runner lowercases the CIM boolean, so `'true'` is what a guest writes;
    the strip and the fold here are for a facts file assembled another way rather
    than a licence to be vague. `'false'` is what physical Windows reports, and
    the three cases would run natively there -- but what was authorised is a
    local VM, so it is a NO-GO rather than a different kind of pass. Anything
    else is a fact this checker cannot read, which is also not a pass.

    It is one half of the rule and not the whole of it: the flag says a
    hypervisor is present, `recognized_provider` says which, and a guest of an
    unqualified hypervisor sets this exactly as a qualifying guest does.
    """
    return isinstance(value, str) and value.strip().lower() == "true"


#: The material facts a GO must rest on, each as (dotted path, what a qualifying
#: run records there, the predicate that decides it).
#:
#: Counts alone cannot distinguish a real Windows guest from a hand-written JUnit
#: file fed to this script on a laptop -- both reproduce 79/64/15/0/0 exactly.
#: What separates them is everything below: the producer marker and the two
#: script paths say which runner made the record, the challenge and binding
#: digests say it was made for this verification, the commit and the clean-tree
#: status say what was tested, the operating system, toolchain and drive
#: identities say it happened on Windows, and the hypervisor flag says it
#: happened in a virtual machine. Every one is required to be present
#: *and* materially filled in, because an empty string is precisely what a
#: fabricated or partially-collected record produces, and "recorded as empty"
#: must not read as "recorded".
#:
#: The provider identity is not here: it is a disjunction over five fields rather
#: than a predicate on one, so `_provider_findings` decides it.
_LOCAL_VM_CONTEXT: tuple[tuple[str, str, Callable[[Any], bool]], ...] = (
    ("run.qualification_id", f"'{QUALIFICATION_ID}'", _exactly(QUALIFICATION_ID)),
    ("run.producer", f"'{PRODUCER}'", _exactly(PRODUCER)),
    ("run.runner_script", f"'{RUNNER_SCRIPT}'", _exactly(RUNNER_SCRIPT)),
    ("run.checker_script", f"'{CHECKER_SCRIPT}'", _exactly(CHECKER_SCRIPT)),
    ("run.requested_commit", "a 40-character commit sha", _commit_sha),
    ("run.challenge_sha256", "a 64-character sha-256 digest", _sha256_hex),
    ("run.binding_sha256", "a 64-character sha-256 digest", _sha256_hex),
    ("code_identity.tested_status", "'clean'", _exactly("clean")),
    ("host.facts_error", "no error reading the PowerShell facts", _exactly("")),
    ("host.os_name", "'Windows'", _exactly("Windows")),
    ("host.os_release", "a nonempty release", _nonempty),
    ("host.os_version", "a nonempty version", _nonempty),
    ("host.architecture", "a nonempty architecture", _nonempty),
    ("host.windows_caption", "a Windows edition caption", _windows_caption),
    ("host.windows_version", "a nonempty Windows version", _nonempty),
    ("host.windows_build", "a nonempty Windows build number", _nonempty),
    ("host.computer_manufacturer", "a nonempty system manufacturer", _nonempty),
    ("host.computer_model", "a nonempty system model", _nonempty),
    ("host.bios_vendor", "a nonempty BIOS vendor", _nonempty),
    # Pinned to `true`, which is what a guest of any hypervisor reports and what
    # physical Windows does not. It is still the guest's report of its own host
    # rather than an independent measurement, so it is one half of the rule: the
    # provider identity below is the other, and both are required.
    ("host.hypervisor_present", "'true'", _hypervisor_present),
    ("toolchain.python_executable", "a nonempty interpreter path", _nonempty),
    ("toolchain.python_version", f"a {PYTHON_SERIES}.x version", _python_series),
    ("toolchain.python_implementation", "'CPython'", _exactly("CPython")),
    ("toolchain.sqlite_version", "a nonempty SQLite version", _nonempty),
    ("toolchain.powershell_edition", "a nonempty PowerShell edition", _nonempty),
    ("toolchain.powershell_executable", "a nonempty PowerShell path", _nonempty),
    ("toolchain.powershell_version", "a nonempty PowerShell version", _nonempty),
    ("filesystem.checkout_root", "a nonempty checkout volume", _nonempty),
    ("filesystem.checkout_filesystem", "a nonempty checkout filesystem", _nonempty),
    ("filesystem.temp_root", "a nonempty temp volume", _nonempty),
    ("filesystem.temp_filesystem", "a nonempty temp filesystem", _nonempty),
)

#: The local VM providers this lane recognizes, lowercase, each being both the
#: provider's name and the token it writes into the guest's identity strings.
#:
#: Parallels Desktop is the provider the qualifying guest runs on: it reports
#: `Parallels International GmbH` as the manufacturer, `Parallels Virtual
#: Platform` as the model and `Parallels Software International Inc.` as the BIOS
#: vendor, so any one of the three carries the token. VMware is kept because a
#: guest reporting it is as unambiguously a local VM and the strings are as
#: distinctive -- `VMware, Inc.`, `VMware20,1` on current hardware versions,
#: `VMware Virtual Platform` on older ones.
#:
#: A hypervisor this tuple does not name is a NO-GO rather than a silent pass:
#: Hyper-V's `Microsoft Corporation` / `Virtual Machine`, VirtualBox, QEMU and
#: Xen are all real hypervisors this lane has not qualified anything on.
RECOGNIZED_PROVIDERS: tuple[str, ...] = ("parallels", "vmware")

#: The five places a guest may name its provider. The three CIM identity strings
#: are what a stock guest reports with nothing installed, and the two guest-tools
#: strings are a supporting signal for a guest that happens to run tools --
#: `guest_tools_service` is the service's *display* name, which names the vendor
#: where the service name (`prl_tools`, `VMTools`) does not.
#:
#: Any one of the five naming a provider is enough, and none of them is
#: individually required. Requiring the tools would fail a perfectly real guest
#: that has none installed; requiring none of the five would accept a physical
#: Windows box, which is not what was authorised.
_PROVIDER_IDENTITY_FIELDS: tuple[str, ...] = (
    "computer_manufacturer",
    "computer_model",
    "bios_vendor",
)
_GUEST_TOOLS_FIELDS: tuple[str, ...] = ("guest_tools_service", "guest_tools_version")

#: Shapes that are a credential wherever they appear. The evidence is assembled
#: field by field from values chosen here, so this is a backstop on the two
#: places that quote the outside world -- the package list and the CIM facts.
#: It is also the first of the three rules the raw JUnit record is scanned
#: against before anything derived from it is written out.
_CREDENTIAL_PATTERN = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abprs]-[A-Za-z0-9-]{10,}"
)

#: An absolute path in any of the three shapes a Windows guest produces: a
#: drive-letter path, a UNC share, and the POSIX roots the verifying machine uses.
_ABSOLUTE_PATH = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'<>|]*"
    r"|\\\\[^\s\"'<>|]+"
    r"|(?<![\w~.-])/(?:Users|home|root|tmp|var|private|mnt)(?:/[^\s\"'<>|]*)?"
)

#: Free text a failing pytest run puts into a JUnit record: the assertion
#: rewrite, the traceback and the exception line. None of it may reach the
#: artifact, and its absence there is asserted rather than assumed.
_FAILURE_TEXT = re.compile(r"Traceback|Exception|Error:|\bassert\b|^E\s", re.MULTILINE)

#: The written record's suite name, distinct from pytest's so a reader cannot
#: mistake the sanitized document for the raw one.
SANITIZED_SUITE_NAME = f"{QUALIFICATION_ID}-sanitized"

#: The status-to-element mapping for the sanitized record. The elements are
#: written empty: a `<failure>`'s message and text are exactly what may not be
#: published, while the element's presence is what carries the outcome.
_SANITIZED_CHILD = {"failed": "failure", "errored": "error", "skipped": "skipped"}

#: Returned by `_at` for an absent path, so `None` stays a legal recorded value.
_ABSENT = object()


class QualificationError(Exception):
    """Input that cannot be read as this lane's result."""


# ---------------------------------------------------------------------------
# JUnit
# ---------------------------------------------------------------------------


def _integer(element: ET.Element, attribute: str) -> int:
    raw = element.get(attribute)
    if raw is None:
        raise QualificationError(f"testsuite has no {attribute!r} attribute")
    try:
        return int(raw)
    except ValueError as error:
        raise QualificationError(
            f"testsuite {attribute!r} is {raw!r}, which is not an integer"
        ) from error


def _seconds(element: ET.Element, attribute: str) -> float:
    raw = element.get(attribute)
    if raw is None:
        raise QualificationError(f"<{element.tag}> has no {attribute!r} attribute")
    try:
        return float(raw)
    except ValueError as error:
        raise QualificationError(
            f"<{element.tag}> {attribute!r} is {raw!r}, which is not a duration"
        ) from error


def _case_status(case: ET.Element) -> str:
    for child in case:
        if child.tag == "failure":
            return "failed"
        if child.tag == "error":
            return "errored"
        if child.tag == "skipped":
            return "skipped"
    return "passed"


def parse_junit(path: Path) -> dict[str, Any]:
    """The outcome, named cases and timing of one JUnit record.

    Raises `QualificationError` for a record that is missing, empty, unparseable
    or structurally not this suite's. Every one of those is a NO-GO rather than
    an absence of information: the whole point of reading the record instead of
    the exit code is that "no result" and "passed" must not look alike.

    Both documents go through here. `sanitize_junit` writes the same structure
    it reads -- suite counts, `classname`, node names, statuses and times -- so
    offline verification re-derives the outcome from the sanitized record with
    this function rather than trusting the evidence's copy of it.
    """
    if not path.is_file():
        raise QualificationError(f"no JUnit record at {path.name}, so the suite did not run")
    raw = path.read_bytes()
    if not raw.strip():
        raise QualificationError(f"the JUnit record {path.name} is empty")
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except ET.ParseError as error:
        raise QualificationError(f"the JUnit record is not parseable XML: {error}") from error

    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise QualificationError(
            f"the JUnit record's root element is <{root.tag}>, not <testsuites> or <testsuite>"
        )
    if not suites:
        raise QualificationError("the JUnit record carries no <testsuite> element")

    cases = [case for suite in suites for case in suite.findall("testcase")]
    if not cases:
        raise QualificationError("the JUnit record carries no <testcase> element")

    node_ids: list[str] = []
    statuses: list[str] = []
    durations: list[float] = []
    for case in cases:
        name = case.get("name")
        classname = case.get("classname")
        if not name:
            raise QualificationError("a <testcase> has no 'name' attribute")
        if classname != DISCOVERY_CLASSNAME:
            raise QualificationError(
                f"<testcase name={name!r}> reports classname {classname!r}, "
                f"not {DISCOVERY_CLASSNAME!r}"
            )
        node_ids.append(f"{DISCOVERY_TEST}::{name}")
        statuses.append(_case_status(case))
        durations.append(_seconds(case, "time"))

    reported = sum(_integer(suite, "tests") for suite in suites)
    if reported != len(cases):
        raise QualificationError(
            f"the JUnit record reports {reported} tests but carries {len(cases)} <testcase> elements"
        )

    skipped_node_ids = sorted(
        node for node, status in zip(node_ids, statuses, strict=True) if status == "skipped"
    )
    named: dict[str, dict[str, Any]] = {}
    for case_name in REQUIRED_WINDOWS_CASES:
        indexes = [index for index, node in enumerate(node_ids) if node.endswith(f"::{case_name}")]
        named[case_name] = {
            "collected": len(indexes),
            # A case collected zero times has no status to report, and
            # "not collected" is the finding rather than a kind of pass.
            "status": statuses[indexes[0]] if len(indexes) == 1 else "not-collected-once",
            "duration_seconds": round(sum(durations[index] for index in indexes), 6),
        }

    return {
        # One record per `<testcase>`, in report order, carrying only values the
        # parser derived. This is what `sanitize_junit` renders, so the written
        # artifact can only contain what this list holds.
        "cases": [
            {
                "name": node.split("::", 1)[1],
                "status": status,
                "duration_seconds": round(duration, 6),
            }
            for node, status, duration in zip(node_ids, statuses, durations, strict=True)
        ],
        "outcome": {
            "junit_error": "",
            "collected": len(cases),
            "passed": sum(1 for status in statuses if status == "passed"),
            "failed": sum(_integer(suite, "failures") for suite in suites),
            "errors": sum(_integer(suite, "errors") for suite in suites),
            "skipped": len(skipped_node_ids),
            "skipped_node_ids": skipped_node_ids,
            "unexpected_skipped_node_ids": sorted(
                set(skipped_node_ids) - set(ALLOWED_SKIPPED_NODE_IDS)
            ),
            "missing_skipped_node_ids": sorted(
                set(ALLOWED_SKIPPED_NODE_IDS) - set(skipped_node_ids)
            ),
        },
        "named_cases": named,
        "timing": {
            "total_seconds": round(sum(_seconds(suite, "time") for suite in suites), 6),
            "named_case_seconds": {
                case_name: named[case_name]["duration_seconds"]
                for case_name in REQUIRED_WINDOWS_CASES
            },
        },
    }


def _empty_junit_result(reason: str) -> dict[str, Any]:
    """The evidence shape for a record that could not be read, with the reason.

    Every count is `-1` rather than `0`: zero is a legal observation and would
    read as "the suite ran and found nothing", which is exactly the confusion
    this checker exists to prevent.
    """
    return {
        "cases": [],
        "outcome": {
            "junit_error": reason,
            "collected": -1,
            "passed": -1,
            "failed": -1,
            "errors": -1,
            "skipped": -1,
            "skipped_node_ids": [],
            "unexpected_skipped_node_ids": [],
            "missing_skipped_node_ids": sorted(ALLOWED_SKIPPED_NODE_IDS),
        },
        "named_cases": {
            case: {"collected": 0, "status": "not-collected-once", "duration_seconds": -1.0}
            for case in REQUIRED_WINDOWS_CASES
        },
        "timing": {
            "total_seconds": -1.0,
            "named_case_seconds": dict.fromkeys(REQUIRED_WINDOWS_CASES, -1.0),
        },
    }


# ---------------------------------------------------------------------------
# Redaction and the written JUnit record
# ---------------------------------------------------------------------------


def _scrub(text: str) -> str:
    """`text` with the checkout, the home directory, credentials, paths and
    failure text removed.

    Applied to the few strings that reach the written record from outside the
    parser's own vocabulary: a `<testcase name=...>` -- a parametrized id carries
    whatever the parameter was, which can be a path, a token or a captured
    traceback -- and the reason an unreadable record could not be read. It runs
    against exactly the three rules `_junit_leaks` then checks the finished
    document against, so anything it misses is a NO-GO rather than an artifact.
    """
    for prefix, marker in ((str(REPO_ROOT), "<checkout>"), (str(Path.home()), "<home>")):
        for form in (prefix, prefix.replace("\\", "/"), prefix.replace("/", "\\")):
            if form:
                text = text.replace(form, marker)
    text = _CREDENTIAL_PATTERN.sub("<redacted-credential>", text)
    text = _ABSOLUTE_PATH.sub("<redacted-path>", text)
    return _FAILURE_TEXT.sub("<redacted-text>", text)


def sensitive_shapes(path: Path) -> int:
    """How many credential, absolute-path or failure-text shapes the raw record has.

    The raw record is read and counted here *before* anything derived from it is
    written, which is what makes "the sanitized artifact carries none of this" a
    measurement rather than a claim. The count is recorded, not judged: a genuine
    failing run legitimately produces tracebacks and guest paths, and the answer
    to that is to not publish the file, which is what the runner does by keeping
    it in a private temporary directory it deletes.
    """
    if not path.is_file():
        return 0
    text = path.read_bytes().decode("utf-8", errors="replace")
    return sum(
        len(pattern.findall(text))
        for pattern in (_CREDENTIAL_PATTERN, _ABSOLUTE_PATH, _FAILURE_TEXT)
    )


def sanitize_junit(junit: Path) -> str:
    """The JUnit record that is safe to keep, built by construction.

    Nothing is redacted out of the raw document, because nothing is copied from
    it: every value written below came off the parser as a count, a status, a
    duration or a node name. Failure messages, exception text, tracebacks,
    captured stdout, skip reasons and system-out sections have no path here at
    all, which is a stronger statement than any pattern-based scrub of the raw
    file could make.

    What survives is what a reviewer needs to re-derive the verdict without the
    evidence file: the per-suite counts, every node id with its status and time,
    the total, and the raw record's SHA-256 -- so the written document can be
    tied back to the raw one the runner deleted.

    Deterministic for a given input, and so callable twice: `build_evidence`
    digests the text and `main` writes it, and the two agree by construction.
    """
    raw_digest = _digest(junit)
    try:
        measured: dict[str, Any] | None = parse_junit(junit)
        reason = ""
    except QualificationError as error:
        measured, reason = None, str(error)

    outcome: Mapping[str, Any] = measured["outcome"] if measured else {}
    total = measured["timing"]["total_seconds"] if measured else 0.0
    suite = ET.Element(
        "testsuite",
        {
            "name": QUALIFICATION_ID,
            "tests": str(max(int(outcome.get("collected", 0)), 0)),
            "failures": str(max(int(outcome.get("failed", 0)), 0)),
            "errors": str(max(int(outcome.get("errors", 0)), 0)),
            "skipped": str(max(int(outcome.get("skipped", 0)), 0)),
            "time": f"{max(float(total), 0.0):.6f}",
        },
    )
    properties = ET.SubElement(suite, "properties")
    for name, value in (
        ("checker_version", CHECKER_VERSION),
        ("classname", DISCOVERY_CLASSNAME),
        ("raw_junit_sha256", raw_digest),
        ("junit_error", _scrub(reason)),
    ):
        ET.SubElement(properties, "property", {"name": name, "value": value})

    for case in measured["cases"] if measured else ():
        element = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": DISCOVERY_CLASSNAME,
                "name": _scrub(str(case["name"])),
                "time": f"{float(case['duration_seconds']):.6f}",
            },
        )
        child = _SANITIZED_CHILD.get(str(case["status"]))
        if child is not None:
            ET.SubElement(element, child)

    root = ET.Element("testsuites", {"name": SANITIZED_SUITE_NAME})
    root.append(suite)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _junit_leaks(sanitized: str) -> list[str]:
    """Every reason the sanitized record is not safe to keep. Empty means safe.

    Run against the finished document rather than against the values that went
    into it, so a future edit to `sanitize_junit` that starts copying a message
    across is caught here instead of in a review.
    """
    findings: list[str] = []
    for pattern, description in (
        (_CREDENTIAL_PATTERN, "something shaped like a credential"),
        (_ABSOLUTE_PATH, "an absolute path"),
        (_FAILURE_TEXT, "failure or exception text"),
    ):
        # The shape only. Reporting the match would republish it.
        if pattern.search(sanitized):
            findings.append(f"the sanitized JUnit record carries {description}")
    lowered = sanitized.replace("\\", "/").lower()
    for prefix, description in (
        (str(Path.home()), "the home directory"),
        (str(REPO_ROOT), "the checkout root"),
        (tempfile.gettempdir(), "the temp directory"),
    ):
        candidate = str(prefix).replace("\\", "/").rstrip("/").lower()
        if candidate and candidate in lowered:
            findings.append(f"the sanitized JUnit record carries {description}")
    return findings


# ---------------------------------------------------------------------------
# Source scan
# ---------------------------------------------------------------------------


def scan_xfail(path: Path) -> int:
    """How many `xfail` markers or `pytest.xfail` calls the source carries.

    An `xfail` is the one construct that turns a failing Windows case into a
    green summary line, so the qualifying result requires zero of them. The scan
    is textual on purpose: a decorator, a `pytest.param(..., marks=...)` entry,
    an `@pytest.mark.xfail`, an imported `xfail` and a runtime `pytest.xfail()`
    call all read the same way here, where an AST rule would need a separate
    case for each and would miss the next spelling.
    """
    text = path.read_text(encoding="utf-8")
    return len(re.findall(r"\bxfail\b", text))


# ---------------------------------------------------------------------------
# Identity, host and toolchain
# ---------------------------------------------------------------------------


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise QualificationError(
            f"`git {' '.join(arguments)}` failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def code_identity(root: Path) -> dict[str, str]:
    """The commit, tree and file blobs of the checkout, re-derived from it.

    `git hash-object` is used for the three blobs rather than `git rev-parse
    HEAD:<path>`: the first hashes the bytes that actually ran, the second the
    bytes that were committed, and only the first is a statement about this run.
    On the runner's clean checkout they agree, and where they do not the working
    tree is what was qualified.

    This is the half of the evidence offline verification can re-derive without
    the guest: a verifier at the same commit computes the same six values, so a
    record that names other code is a finding rather than a claim to be believed.
    """
    return {
        "tested_commit": _git(root, "rev-parse", "HEAD"),
        "tested_tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "discovery_test_blob": _git(root, "hash-object", DISCOVERY_TEST),
        "runner_script_blob": _git(root, "hash-object", RUNNER_SCRIPT),
        "checker_script_blob": _git(root, "hash-object", CHECKER_SCRIPT),
    }


def _digest(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def challenge_digest(challenge: str) -> str:
    """`sha256(challenge)`, which is what the evidence records.

    One-way on purpose. The artifact names the challenge it answered without
    carrying it, so a record cannot be re-presented to a verifier who did not
    already choose that challenge.
    """
    return _text_digest(challenge)


def binding_digest(
    *,
    challenge_sha256: str,
    tested_commit: str,
    junit_sha256: str,
    sanitized_junit_sha256: str,
) -> str:
    """The one value that ties this challenge to these exact artifacts.

    Recomputable offline from the challenge the verifier chose, the commit it
    checked out, and the sanitized record it holds -- with the raw record's
    digest read out of the evidence, since the raw file is deleted by design.
    That last component is therefore bound rather than witnessed: the binding
    proves the producer committed to that digest while holding the challenge, not
    that the digest describes a real file.
    """
    return _text_digest(
        f"{BINDING_LABEL}|{CHECKER_VERSION}|{challenge_sha256}"
        f"|{tested_commit}|{junit_sha256}|{sanitized_junit_sha256}"
    )


def _normalize_path(value: str | os.PathLike[str]) -> str:
    """An absolute path with the checkout and the home directory as markers.

    Both replacements matter, and in this order: on the guest they may name
    different volumes, while on a developer's machine the checkout sits inside
    the home directory and the longer prefix has to win.
    """
    text = str(value).replace("\\", "/")
    for prefix, marker in ((REPO_ROOT, "<checkout>"), (Path.home(), "<home>")):
        candidate = str(prefix).replace("\\", "/").rstrip("/")
        if candidate and text.lower().startswith(candidate.lower()):
            return marker + text[len(candidate) :]
    return text


def _volume(path: Path) -> tuple[str, str]:
    """The drive root of `path` and its filesystem type.

    The filesystem type is read natively on Windows, which is the platform this
    lane qualifies; elsewhere it is left empty rather than shelled out for,
    because no verdict depends on it off Windows.
    """
    resolved = os.path.abspath(path)
    drive = os.path.splitdrive(resolved)[0]
    root = f"{drive}{os.sep}" if drive else os.sep
    if os.name != "nt":
        return root.replace("\\", "/"), ""

    # Imported here rather than at module scope: `ctypes.windll` exists only on
    # Windows, and this is the one branch that reaches it.
    import ctypes

    name = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        ctypes.c_wchar_p(root),
        name,
        ctypes.sizeof(name) // ctypes.sizeof(ctypes.c_wchar),
        None,
        None,
        None,
        filesystem,
        ctypes.sizeof(filesystem) // ctypes.sizeof(ctypes.c_wchar),
    )
    return root, filesystem.value if ok else ""


def load_facts(path: Path) -> tuple[dict[str, str], str]:
    """The PowerShell facts, or the empty facts and the reason there are none.

    Fail closed without exiting: a facts file that is missing, unparseable, not
    an object, short of a key or carrying a non-string leaves every fact empty
    and puts the reason in `host.facts_error`, which `_LOCAL_VM_CONTEXT` requires
    to be empty. So the run is a NO-GO that says why, rather than a crash that
    leaves no evidence -- and never a GO that silently read nothing.
    """
    empty = dict.fromkeys(FACT_KEYS, "")
    if not path.is_file():
        return empty, f"no PowerShell facts at {path.name}"
    try:
        # `utf-8-sig`, not `utf-8`: Windows PowerShell 5.1's `Set-Content
        # -Encoding UTF8` writes a byte-order mark, and a BOM left on the front
        # of the document makes `json.loads` fail at column 1 on a file that is
        # otherwise perfectly good. It decodes plain UTF-8 identically, so this
        # costs nothing on the editions that do not write one.
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return empty, f"the PowerShell facts are not readable JSON: {error}"
    if not isinstance(loaded, Mapping):
        return empty, "the PowerShell facts are not a JSON object"
    facts: dict[str, str] = {}
    for key in FACT_KEYS:
        if key not in loaded:
            return empty, f"the PowerShell facts record no {key!r}"
        value = loaded[key]
        if not isinstance(value, str):
            return empty, f"the PowerShell fact {key!r} is {type(value).__name__}, not a string"
        facts[key] = value
    return facts, ""


def _installed_packages() -> dict[str, str]:
    found: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name:
            found[name] = distribution.version or ""
    return dict(sorted(found.items()))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_evidence(
    *,
    root: Path,
    junit: Path,
    expected_commit: str,
    challenge: str,
    facts: Mapping[str, str],
    facts_error: str = "",
) -> dict[str, Any]:
    """The normalized evidence for this run, without its verdict.

    The verdict is attached by `main` after `validate` has read this payload, so
    `integrity.evidence_sha256` covers everything the checker observed and
    nothing it concluded.
    """
    try:
        measured = parse_junit(junit)
    except QualificationError as error:
        measured = _empty_junit_result(str(error))

    try:
        identity = code_identity(root)
    except QualificationError as error:
        # Recorded as the empty identity it is. `validate` compares these against
        # its own observation and reports the mismatch; a checker that exited
        # here would leave no evidence of why.
        identity = {
            "tested_commit": f"unavailable: {error}",
            "tested_tree": "",
            "discovery_test_blob": "",
            "runner_script_blob": "",
            "checker_script_blob": "",
        }

    checkout_root, checkout_filesystem = _volume(root)
    temp_root, temp_filesystem = _volume(Path(tempfile.gettempdir()))
    junit_sha256 = _digest(junit)
    sanitized_junit_sha256 = _text_digest(sanitize_junit(junit))
    challenge_sha256 = challenge_digest(challenge)

    payload: dict[str, Any] = {
        "checker_version": CHECKER_VERSION,
        "run": {
            "qualification_id": QUALIFICATION_ID,
            "producer": PRODUCER,
            "runner_script": RUNNER_SCRIPT,
            "checker_script": CHECKER_SCRIPT,
            "requested_commit": expected_commit,
            "challenge_sha256": challenge_sha256,
            "binding_sha256": binding_digest(
                challenge_sha256=challenge_sha256,
                tested_commit=identity["tested_commit"],
                junit_sha256=junit_sha256,
                sanitized_junit_sha256=sanitized_junit_sha256,
            ),
        },
        "code_identity": {**identity, "tested_status": facts["checkout_status"]},
        "host": {
            "facts_error": facts_error,
            "os_name": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "windows_caption": facts["windows_caption"],
            "windows_version": facts["windows_version"],
            "windows_build": facts["windows_build"],
            "computer_manufacturer": facts["computer_manufacturer"],
            "computer_model": facts["computer_model"],
            "bios_vendor": facts["bios_vendor"],
            "hypervisor_present": facts["hypervisor_present"],
            "guest_tools_service": facts["guest_tools_service"],
            "guest_tools_version": facts["guest_tools_version"],
        },
        "toolchain": {
            "python_executable": _normalize_path(sys.executable),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "sqlite_version": sqlite3.sqlite_version,
            "powershell_edition": facts["powershell_edition"],
            "powershell_executable": _normalize_path(facts["powershell_executable"]),
            "powershell_version": facts["powershell_version"],
        },
        "filesystem": {
            "checkout_root": checkout_root,
            "checkout_filesystem": checkout_filesystem,
            "temp_root": temp_root,
            "temp_filesystem": temp_filesystem,
        },
        "packages": _installed_packages(),
        "outcome": {
            **measured["outcome"],
            "xfail_markers_or_calls_in_source": scan_xfail(root / DISCOVERY_TEST),
            "raw_junit_sensitive_shapes": sensitive_shapes(junit),
        },
        "named_cases": measured["named_cases"],
        "timing": measured["timing"],
        "integrity": {
            "junit_sha256": junit_sha256,
            "sanitized_junit_sha256": sanitized_junit_sha256,
            "evidence_sha256": "",
        },
    }
    payload["integrity"]["evidence_sha256"] = evidence_digest(payload)
    return payload


def evidence_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the observed payload: no verdict, and no digest of itself."""
    without = {key: value for key, value in payload.items() if key != "verdict"}
    integrity = dict(without.get("integrity", {}))
    integrity.pop("evidence_sha256", None)
    without["integrity"] = integrity
    return hashlib.sha256(_canonical(without)).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _at(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _ABSENT
        current = current[part]
    return current


def _leaks(payload: Mapping[str, Any]) -> list[str]:
    serialized = _canonical(payload).decode("utf-8")
    findings: list[str] = []
    if _CREDENTIAL_PATTERN.search(serialized):
        # The shape only. Reporting the match would republish it.
        findings.append("the evidence carries something shaped like a credential")
    flattened = serialized.replace("\\\\", "/").replace("\\", "/").lower()
    for prefix, description in ((Path.home(), "home"), (REPO_ROOT, "checkout")):
        candidate = str(prefix).replace("\\", "/").rstrip("/").lower()
        if candidate and candidate in flattened:
            findings.append(f"the evidence carries a user-specific absolute {description} path")
    return findings


def observe(
    root: Path,
    *,
    expected_commit: str,
    challenge: str,
    sanitized_junit: str,
    raw_junit: Path | None,
) -> dict[str, Any]:
    """Ground truth, re-derived rather than read back.

    `raw_junit` is the raw record in produce mode and `None` in verify mode,
    where the runner has already deleted it. `raw_junit_sha256` is `None` in that
    case, and `_identity_findings` checks the recorded digest's shape and its
    place in the binding instead of comparing it to a file it cannot see.
    """
    try:
        identity = code_identity(root)
    except QualificationError as error:
        identity = {
            "tested_commit": f"unavailable: {error}",
            "tested_tree": "",
            "discovery_test_blob": "",
            "runner_script_blob": "",
            "checker_script_blob": "",
        }
    return {
        "expected_commit": expected_commit,
        "code_identity": identity,
        "challenge_sha256": challenge_digest(challenge),
        "sanitized_junit": sanitized_junit,
        "sanitized_junit_sha256": _text_digest(sanitized_junit),
        "raw_junit_sha256": None if raw_junit is None else _digest(raw_junit),
    }


def validate(evidence: Mapping[str, Any], *, observed: Mapping[str, Any]) -> list[str]:
    """Every reason this evidence is not a qualifying result. Empty means GO.

    `observed` is ground truth the caller re-derived from the checkout, the
    challenge and the sanitized record, so the identity and digest checks compare
    the record against the world rather than against itself.
    """
    findings: list[str] = []

    missing = [field for field in REQUIRED_FIELDS if _at(evidence, field) is _ABSENT]
    if missing:
        findings.append(f"the evidence is missing required fields: {missing}")

    actual_version = _at(evidence, "checker_version")
    if actual_version is not _ABSENT and actual_version != CHECKER_VERSION:
        findings.append(f"checker_version is {actual_version!r}, not {CHECKER_VERSION!r}")

    findings.extend(_local_vm_findings(evidence))
    findings.extend(_provider_findings(evidence))
    findings.extend(_identity_findings(evidence, observed))
    findings.extend(_outcome_findings(evidence))
    findings.extend(_leaks(evidence))
    findings.extend(_junit_leaks(str(observed["sanitized_junit"])))
    return findings


def _local_vm_findings(evidence: Mapping[str, Any]) -> list[str]:
    """Every material fact a GO needs that this record does not carry.

    An absent field is left to the required-fields check above, so a field that
    was never written is reported once as missing rather than twice.
    """
    findings: list[str] = []
    for path, description, holds in _LOCAL_VM_CONTEXT:
        actual = _at(evidence, path)
        if actual is not _ABSENT and not holds(actual):
            findings.append(f"{path} is {actual!r}, and a qualifying run records {description}")

    packages = _at(evidence, "packages")
    if packages is not _ABSENT:
        if not isinstance(packages, Mapping):
            findings.append("packages is not an inventory of distribution versions")
        else:
            for name in REQUIRED_PACKAGES:
                if not _nonempty(packages.get(name)):
                    findings.append(
                        f"packages records no installed version of {name!r}, "
                        "which the qualifying run must have had"
                    )
    return findings


def recognized_provider(host: Mapping[str, Any]) -> str:
    """The local VM provider this host names, or `''` if it names none.

    A substring match, folded to lowercase, over the five fields a guest can name
    its provider in. That is deliberately loose on the *shape* of the string and
    exact on the *token*: Parallels and VMware each spell their identity several
    ways across versions and across the manufacturer, model and BIOS properties,
    while `parallels` and `vmware` appearing at all in a CIM identity string is
    not something a physical machine or another hypervisor does.

    The tools fields are read on the same terms as the identity fields rather
    than as a separate rule. `guest_tools_service` carries the service's display
    name, so a guest running Parallels Tools or VMware Tools names its provider
    there too -- but a guest with no tools at all is fully identified by CIM
    alone, which is why nothing here requires them.
    """
    for field in (*_PROVIDER_IDENTITY_FIELDS, *_GUEST_TOOLS_FIELDS):
        value = host.get(field)
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        for provider in RECOGNIZED_PROVIDERS:
            if provider in lowered:
                return provider
    return ""


def _provider_findings(evidence: Mapping[str, Any]) -> list[str]:
    """The local VM provider, from any one of the five places it appears."""
    host = _at(evidence, "host")
    if host is _ABSENT or not isinstance(host, Mapping):
        return ["host is not a record of the machine the suite ran on"]
    if recognized_provider(host):
        return []
    return [
        (
            "the host names no recognized local VM provider: none of "
            f"{[*_PROVIDER_IDENTITY_FIELDS, *_GUEST_TOOLS_FIELDS]} names one of "
            f"{list(RECOGNIZED_PROVIDERS)}"
        )
    ]


def _identity_findings(
    evidence: Mapping[str, Any], observed: Mapping[str, Any]
) -> list[str]:
    findings: list[str] = []
    expected_commit = observed["expected_commit"]
    identity: Mapping[str, str] = observed["code_identity"]
    head_commit = identity["tested_commit"]

    if expected_commit != head_commit:
        findings.append(
            f"the requested commit {expected_commit!r} is not the checked-out HEAD {head_commit!r}"
        )

    recorded_junit_sha256 = _at(evidence, "integrity.junit_sha256")
    observed_junit_sha256 = observed["raw_junit_sha256"]
    if observed_junit_sha256 is None:
        # Verify mode: the raw record is gone by design, so its digest can only
        # be checked for shape and for its place in the challenge binding.
        if recorded_junit_sha256 is not _ABSENT and not _sha256_hex(recorded_junit_sha256):
            findings.append(
                f"integrity.junit_sha256 is {recorded_junit_sha256!r}, "
                "and a qualifying run records a 64-character sha-256 digest"
            )
    else:
        if (
            recorded_junit_sha256 is not _ABSENT
            and recorded_junit_sha256 != observed_junit_sha256
        ):
            findings.append(
                f"integrity.junit_sha256 records {recorded_junit_sha256!r}, "
                f"but this checkout has {observed_junit_sha256!r}"
            )

    for path, expected in (
        ("run.requested_commit", expected_commit),
        ("run.challenge_sha256", observed["challenge_sha256"]),
        *((f"code_identity.{name}", value) for name, value in identity.items()),
        ("integrity.sanitized_junit_sha256", observed["sanitized_junit_sha256"]),
    ):
        actual = _at(evidence, path)
        if actual is not _ABSENT and actual != expected:
            findings.append(f"{path} records {actual!r}, but this checkout has {expected!r}")

    expected_binding = binding_digest(
        challenge_sha256=str(observed["challenge_sha256"]),
        tested_commit=head_commit,
        junit_sha256="" if recorded_junit_sha256 is _ABSENT else str(recorded_junit_sha256),
        sanitized_junit_sha256=str(observed["sanitized_junit_sha256"]),
    )
    recorded_binding = _at(evidence, "run.binding_sha256")
    if recorded_binding is not _ABSENT and recorded_binding != expected_binding:
        findings.append(
            "run.binding_sha256 does not bind this challenge to these artifacts"
        )

    recorded_digest = _at(evidence, "integrity.evidence_sha256")
    if recorded_digest is not _ABSENT and recorded_digest != evidence_digest(evidence):
        findings.append("integrity.evidence_sha256 does not cover this payload")
    return findings


def _sorted_strings(value: Any) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    return sorted(str(entry) for entry in value)


def _outcome_findings(evidence: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []

    junit_error = _at(evidence, "outcome.junit_error")
    if junit_error not in (_ABSENT, ""):
        findings.append(f"the JUnit record was not usable: {junit_error}")

    for key, expected in EXPECTED_COUNTS.items():
        actual = _at(evidence, f"outcome.{key}")
        if actual is not _ABSENT and actual != expected:
            findings.append(f"outcome.{key} is {actual!r}, not {expected}")

    xfail = _at(evidence, "outcome.xfail_markers_or_calls_in_source")
    if xfail is not _ABSENT and xfail != 0:
        findings.append(
            f"{DISCOVERY_TEST} carries {xfail} xfail marker(s) or call(s); "
            "a qualifying run has none"
        )

    skipped = _sorted_strings(_at(evidence, "outcome.skipped_node_ids"))
    if skipped is None:
        findings.append("outcome.skipped_node_ids is not a list of node ids")
    else:
        allowlist = sorted(ALLOWED_SKIPPED_NODE_IDS)
        if skipped != allowlist:
            added = sorted(set(skipped) - set(allowlist))
            absent = sorted(set(allowlist) - set(skipped))
            if added:
                findings.append(f"cases skipped that the allowlist does not permit: {added}")
            if absent:
                findings.append(f"allowlisted cases that did not skip: {absent}")
            if not added and not absent:
                findings.append(f"the skipped node ids are duplicated: {skipped}")

    for path in ("outcome.unexpected_skipped_node_ids", "outcome.missing_skipped_node_ids"):
        recorded = _sorted_strings(_at(evidence, path))
        if recorded:
            findings.append(f"{path} is {recorded}, and a qualifying run records none")

    findings.extend(_named_case_findings(evidence))
    return findings


def _named_case_findings(evidence: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    for case in REQUIRED_WINDOWS_CASES:
        collected = _at(evidence, f"named_cases.{case}.collected")
        status = _at(evidence, f"named_cases.{case}.status")
        if collected is _ABSENT or status is _ABSENT:
            continue
        if collected == 0:
            findings.append(f"the required Windows case {case} was never collected")
            continue
        if collected != 1:
            findings.append(f"the required Windows case {case} was collected {collected} times")
            continue
        if status != "passed":
            findings.append(f"the required Windows case {case} {status}, and must pass")
    return findings


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


def _measured_summary(measured: Mapping[str, Any]) -> dict[str, Any]:
    """The count-and-status half of a parsed record, with the timings dropped.

    Durations are what the two documents legitimately round differently, and
    nothing in the verdict rests on them. Everything that does -- the five totals,
    the skip set, and each required case's multiplicity and status -- is here.
    """
    return {
        "outcome": dict(measured["outcome"]),
        "named_cases": {
            case: {
                "collected": measured["named_cases"][case]["collected"],
                "status": measured["named_cases"][case]["status"],
            }
            for case in REQUIRED_WINDOWS_CASES
        },
    }


def verify(
    *,
    root: Path,
    sanitized_junit: Path,
    evidence_path: Path,
    expected_commit: str,
    challenge: str,
) -> tuple[list[str], dict[str, Any] | None]:
    """Check a produced pair of artifacts offline. Returns (findings, evidence).

    Needs no network, no GitHub field and nothing from the guest beyond the two
    files: the sanitized JUnit record and the evidence JSON. Everything else
    comes from the verifier -- the checkout it is run in, the commit it expects
    and the challenge it chose.

    Four things are recomputed rather than read: the sanitized record's digest
    from its bytes, the evidence digest from the normalized payload, the
    challenge digest from the challenge, and the code identity from the checkout.
    A fifth is re-derived: the outcome is parsed out of the sanitized record and
    compared with the evidence's copy of it, so the two files have to tell the
    same story about which cases ran.

    What it cannot do is witness the guest. The Windows, provider and package facts
    are the producer's report, bound to the challenge and unchanged since -- see
    this module's docstring on the limits of that.
    """
    if not sanitized_junit.is_file():
        return [f"no sanitized JUnit record at {sanitized_junit}"], None
    if not evidence_path.is_file():
        return [f"no evidence at {evidence_path}"], None
    try:
        sanitized_text = sanitized_junit.read_text(encoding="utf-8")
        loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"the evidence is not readable JSON: {error}"], None
    if not isinstance(loaded, Mapping):
        return ["the evidence is not a JSON object"], None
    evidence: dict[str, Any] = dict(loaded)

    findings = validate(
        evidence,
        observed=observe(
            root,
            expected_commit=expected_commit,
            challenge=challenge,
            sanitized_junit=sanitized_text,
            raw_junit=None,
        ),
    )

    try:
        recomputed = _measured_summary(parse_junit(sanitized_junit))
    except QualificationError as error:
        findings.append(f"the sanitized JUnit record is not readable as a result: {error}")
        return findings, evidence

    recorded = {
        "outcome": {
            key: _at(evidence, f"outcome.{key}")
            for key in recomputed["outcome"]
            if _at(evidence, f"outcome.{key}") is not _ABSENT
        },
        "named_cases": {
            case: {
                leaf: _at(evidence, f"named_cases.{case}.{leaf}")
                for leaf in ("collected", "status")
                if _at(evidence, f"named_cases.{case}.{leaf}") is not _ABSENT
            }
            for case in REQUIRED_WINDOWS_CASES
        },
    }
    if recorded != recomputed:
        findings.append(
            "the evidence and the sanitized JUnit record disagree about the result: "
            f"the record carries {recomputed}"
        )
    return findings, evidence


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _report(findings: Iterable[str]) -> None:
    for finding in findings:
        print(f"  - {finding}", file=sys.stderr)


def _hex_argument(length: int, label: str) -> Callable[[str], str]:
    def parse(value: str) -> str:
        if re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
            raise argparse.ArgumentTypeError(
                f"{label} must be exactly {length} lowercase hex characters, got {value!r}"
            )
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    commit = _hex_argument(40, "--expected-commit")
    challenge = _hex_argument(64, "--challenge")

    produce = modes.add_parser(
        "produce", help="build and check the evidence on the Windows guest"
    )
    produce.add_argument("--junit", required=True, type=Path)
    produce.add_argument("--facts", required=True, type=Path)
    produce.add_argument(
        "--sanitized-junit",
        required=True,
        type=Path,
        help="where to write the keepable JUnit record; --junit must never be published",
    )
    produce.add_argument("--evidence", required=True, type=Path)
    produce.add_argument("--expected-commit", required=True, type=commit)
    produce.add_argument("--challenge", required=True, type=challenge)
    produce.add_argument("--root", type=Path, default=REPO_ROOT)

    verification = modes.add_parser(
        "verify", help="check a produced pair of artifacts offline, on any platform"
    )
    verification.add_argument("--sanitized-junit", required=True, type=Path)
    verification.add_argument("--evidence", required=True, type=Path)
    verification.add_argument("--expected-commit", required=True, type=commit)
    verification.add_argument("--challenge", required=True, type=challenge)
    verification.add_argument("--root", type=Path, default=REPO_ROOT)
    return parser


def _produce(arguments: argparse.Namespace) -> int:
    if platform.system() != "Windows":
        print(
            "P1b Windows qualification refused: produce runs on the Windows guest, and "
            f"platform.system() reports {platform.system()!r}. Nothing was written.",
            file=sys.stderr,
        )
        return 1

    # Written before anything else can exit, and from a code path that handles a
    # missing, empty and malformed record identically to a failing one, so the
    # artifact directory is never left without it.
    sanitized = sanitize_junit(arguments.junit)
    arguments.sanitized_junit.parent.mkdir(parents=True, exist_ok=True)
    arguments.sanitized_junit.write_text(sanitized, encoding="utf-8")

    facts, facts_error = load_facts(arguments.facts)
    evidence = build_evidence(
        root=arguments.root,
        junit=arguments.junit,
        expected_commit=arguments.expected_commit,
        challenge=arguments.challenge,
        facts=facts,
        facts_error=facts_error,
    )
    findings = validate(
        evidence,
        observed=observe(
            arguments.root,
            expected_commit=arguments.expected_commit,
            challenge=arguments.challenge,
            sanitized_junit=sanitized,
            raw_junit=arguments.junit,
        ),
    )
    _write_verdict(arguments.evidence, evidence, findings)
    return _announce(findings, "produced on the local Windows VM", _provider_of(evidence))


def _verify(arguments: argparse.Namespace) -> int:
    findings, evidence = verify(
        root=arguments.root,
        sanitized_junit=arguments.sanitized_junit,
        evidence_path=arguments.evidence,
        expected_commit=arguments.expected_commit,
        challenge=arguments.challenge,
    )
    return _announce(
        findings, "verified offline against the two artifacts", _provider_of(evidence)
    )


def _write_verdict(
    destination: Path, evidence: dict[str, Any], findings: Sequence[str]
) -> None:
    evidence["verdict"] = {
        "result": "NO-GO" if findings else "GO",
        "checker_version": CHECKER_VERSION,
        "findings": list(findings),
        "expected": {
            **EXPECTED_COUNTS,
            "xfail_markers_or_calls_in_source": 0,
            "unexpected_skipped_node_ids": 0,
            "skipped_node_ids": list(ALLOWED_SKIPPED_NODE_IDS),
            "required_windows_cases": list(REQUIRED_WINDOWS_CASES),
            "local_vm_context": {
                field: description for field, description, _ in _LOCAL_VM_CONTEXT
            },
            "local_vm_provider": {
                "recognized": list(RECOGNIZED_PROVIDERS),
                "named_by_any_of": [*_PROVIDER_IDENTITY_FIELDS, *_GUEST_TOOLS_FIELDS],
            },
            "required_packages": list(REQUIRED_PACKAGES),
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _provider_of(evidence: Mapping[str, Any] | None) -> str:
    """The provider a record names, for the GO line. `''` for anything else."""
    host = _at(evidence or {}, "host")
    return recognized_provider(host) if isinstance(host, Mapping) else ""


def _announce(findings: Sequence[str], where: str, provider: str) -> int:
    if findings:
        print(f"P1b Windows qualification NO-GO ({where}):\n", file=sys.stderr)
        _report(findings)
        return 1
    # `provider` is nonempty on this branch by construction: an unrecognized one
    # is a finding, and a finding is the branch above.
    print(
        f"P1b Windows qualification GO ({where}): 79 collected, 64 passed, 15 skipped, "
        f"0 failed, 0 errored, 3 native Windows cases executed on a Windows guest of "
        f"the local VM provider {provider!r}."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return _produce(arguments) if arguments.mode == "produce" else _verify(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
