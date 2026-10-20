#!/usr/bin/env python3
"""Verify the WP-15 acceptance and qualification ledger against this checkout.

``qualification/wp15/qualification-ledger.v1.json`` records one entry per
behaviour gate in section 20 of the complete native macOS document intake design
v2.0. This script is the gate that keeps it honest. It runs five independent
checks, in order, and exits non-zero with one diagnostic per problem found:

1. **The schema.** ``qualification-ledger.schema.json`` is meta-validated as a
   Draft 2020-12 schema and then applied to the committed document with
   ``jsonschema``, so the structural half of the contract is checked by a
   third-party implementation of a published standard rather than by the same
   code that reads the ledger.
2. **Gate-set completeness.** The design states complete-solution acceptance
   contains 130 identified behaviour gates: 50 AT, 17 NAT, 19 DOC, 38 MON and
   6 END. ``EXPECTED_FAMILIES`` below holds those counts as literals, so the
   ledger cannot redefine the gate set it is measured against. Every id from
   ``AT-01`` to ``END-06`` must appear exactly once, in family then ordinal
   order, with no extras -- which is what rejects a missing gate, a duplicate
   gate and a gate that does not exist in the design.
3. **Status rules, fail-closed.** ``passed`` is permitted only for an
   evidence class this repository can actually produce, and only with at least
   one evidence reference and no missing condition. Every other status must
   name a concrete missing condition. This is the rule that makes it impossible
   to record a passed signing, notarisation, Intel, older-macOS, VoiceOver,
   visual, fixture or performance gate from a test run: those gates declare an
   evidence class no test run can satisfy, and the check refuses the combination
   rather than trusting the author. A real-host gate passes only while it cites a
   measurement record retained under ``docs/development/qualification/``, and an
   external-fixture gate only while it cites digest-bound receipts under
   ``qualification/wp15/fixtures/`` from at least two distinct producers whose
   pinned fixtures still hash to the recorded digests.
4. **Evidence freshness.** Every referenced path must exist in this checkout,
   and every named symbol must still occur in that file. A renamed or deleted
   test is therefore a ledger failure rather than silent staleness.
5. **Report agreement.** ``docs/quality/wp15-qualification-status.md`` is
   rendered from the ledger. Without ``--write`` a drift is a failure; with
   ``--write`` the file is regenerated.

The checker takes no path argument and reads no oracle from the command line: a
checker handed its own expected answer proves whatever it is handed.

``jsonschema`` is the only third-party import. Nothing here imports
``omnivia_core`` or any package under test, so no check can be satisfied by a
stale installed copy of anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

import jsonschema

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
LEDGER_PATH: Final = REPO_ROOT / "qualification/wp15/qualification-ledger.v1.json"
SCHEMA_PATH: Final = REPO_ROOT / "qualification/wp15/qualification-ledger.schema.json"
REPORT_PATH: Final = REPO_ROOT / "docs/quality/wp15-qualification-status.md"

# Pinned from the design's own count: "Complete-solution acceptance contains 130
# identified behaviour gates: 50 AT, 17 NAT, 19 DOC, 38 MON and 6 END." Held here
# as literals, independently of the ledger's own expected_counts object, so that
# editing the ledger cannot shrink the gate set it is judged against. Order is
# the family order the design presents.
EXPECTED_FAMILIES: Final[tuple[tuple[str, int, str], ...]] = (
    ("AT", 50, "20"),
    ("NAT", 17, "20.1"),
    ("DOC", 19, "20.2"),
    ("MON", 38, "20.3"),
    ("END", 6, "20.4"),
)

STATUSES: Final = (
    "passed",
    "failed",
    "blocked_external",
    "not_executed",
    "not_implemented",
)

EVIDENCE_TYPES: Final = (
    "automated_repository_test",
    "repository_checker",
    "installed_signed_build",
    "real_host_measurement",
    "accessibility_review",
    "external_fixture_corpus",
)

# The two classes of evidence this repository and its CI can actually produce.
# Any other class names evidence that only an installed signed build, a measured
# host, a reviewer or an external fixture corpus can supply, so a gate declaring
# one of those can never be recorded as passed from here.
REPOSITORY_PRODUCIBLE: Final = frozenset(
    {"automated_repository_test", "repository_checker"}
)

# The one other class a gate may pass on. A measured host cannot be repeated from
# here, so the measurement is committed as a JSON record under this directory,
# re-verified offline on every run, and cited as evidence. Without such a cited
# record a real_host_measurement gate still cannot be passed.
RETAINED_RECORD_DIR: Final = "docs/development/qualification/"

# The third: an external fixture corpus. The fixtures are committed beside digest-
# bound receipts naming the application that produced each one, and a gate passes
# only while it cites receipts from at least two distinct producers whose pinned
# files still hash to the recorded digests.
CORPUS_RECEIPT_DIR: Final = "qualification/wp15/fixtures/"
CORPUS_MINIMUM_PRODUCERS: Final = 2

# A status that asserts something was observed needs something to have observed
# it with. `blocked_external`, `not_executed` and `not_implemented` may carry
# partial evidence or none.
EVIDENCE_REQUIRED: Final = frozenset({"passed", "failed"})

REPORT_HEADER: Final = """<!-- GENERATED FILE - DO NOT EDIT.
     Source of truth: qualification/wp15/qualification-ledger.v1.json
     Generator: scripts/check-wp15-qualification.py
     Regenerate: python scripts/check-wp15-qualification.py --write
     Verify:     python scripts/check-wp15-qualification.py
-->
"""


class LedgerError(Exception):
    """A fact the checker needs could not be read."""


def _relative(path: Path) -> str:
    """Repository-relative where possible, absolute otherwise.

    The report path is redirected by the tests, so this must not assume every
    path it is handed lies under the repository root.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LedgerError(f"cannot read {_relative(path)}: {error}") from error


def expected_gate_ids() -> tuple[str, ...]:
    """Every gate id the design defines, in family then ordinal order."""
    return tuple(
        f"{family}-{ordinal:02d}"
        for family, count, _section in EXPECTED_FAMILIES
        for ordinal in range(1, count + 1)
    )


def check_schema(document: object, schema: object) -> list[str]:
    """Meta-validate the schema, then validate the document against it."""
    validator_class = jsonschema.validators.Draft202012Validator
    try:
        validator_class.check_schema(cast("bool | Mapping[str, Any]", schema))
    except jsonschema.exceptions.SchemaError as error:
        return [
            (
                f"{_relative(SCHEMA_PATH)} is not a valid Draft 2020-12 schema: "
                f"{error.message}"
            )
        ]
    return [
        f"{_relative(LEDGER_PATH)} at $"
        f"{''.join(f'[{part!r}]' for part in error.absolute_path)}: {error.message}"
        for error in sorted(
            validator_class(cast("bool | Mapping[str, Any]", schema)).iter_errors(
                document
            ),
            key=lambda error: (list(map(str, error.absolute_path)), error.message),
        )
    ]


def check_completeness(gates: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every design gate present exactly once, in order, with nothing extra."""
    findings: list[str] = []
    expected = expected_gate_ids()
    present = [str(gate["gate_id"]) for gate in gates]

    seen: dict[str, int] = {}
    for gate_id in present:
        seen[gate_id] = seen.get(gate_id, 0) + 1
    for gate_id in sorted(identifier for identifier, n in seen.items() if n > 1):
        findings.append(f"gate {gate_id} appears {seen[gate_id]} times; expected once")

    missing = [gate_id for gate_id in expected if gate_id not in seen]
    for gate_id in missing:
        findings.append(
            f"gate {gate_id} is in the design but absent from the ledger"
        )

    unknown = sorted(set(seen) - set(expected))
    for gate_id in unknown:
        findings.append(
            f"gate {gate_id} is not a gate in section 20 of the design; the gate set "
            f"is {', '.join(f'{f}-01..{c:02d}' for f, c, _ in EXPECTED_FAMILIES)}"
        )

    if not findings and present != list(expected):
        findings.append(
            "gates are not in family then ordinal order; expected "
            f"{expected[0]}..{expected[-1]} in the order "
            f"{', '.join(family for family, _c, _s in EXPECTED_FAMILIES)}"
        )
    return findings


def check_counts(document: Mapping[str, Any]) -> list[str]:
    """The ledger's declared counts must agree with the pinned design counts."""
    findings: list[str] = []
    declared = document["expected_counts"]
    total = sum(count for _family, count, _section in EXPECTED_FAMILIES)
    if declared.get("gates") != total:
        findings.append(
            f"expected_counts.gates is {declared.get('gates')!r}; the design "
            f"identifies {total} behaviour gates"
        )
    for family, count, _section in EXPECTED_FAMILIES:
        if declared.get(family) != count:
            findings.append(
                f"expected_counts.{family} is {declared.get(family)!r}; the design "
                f"identifies {count} {family} gates"
            )
    actual = len(document["gates"])
    if actual != total:
        findings.append(f"the ledger holds {actual} gates; the design identifies {total}")
    return findings


def _pinned_files(node: object) -> list[Mapping[str, Any]]:
    """Every ``{"file", "sha256"}`` object anywhere in a receipt."""
    if isinstance(node, Mapping):
        found = [node] if "file" in node and "sha256" in node else []
        return found + [pin for value in node.values() for pin in _pinned_files(value)]
    if isinstance(node, list):
        return [pin for value in node for pin in _pinned_files(value)]
    return []


def check_corpus_receipts(gate_id: str, evidence: Sequence[Mapping[str, Any]]) -> list[str]:
    """A corpus gate passes only on retained receipts from distinct producers.

    Each cited receipt must name its producing application and pin at least one
    fixture, and every pinned fixture beside it must still hash to its digest.
    """
    cited = sorted(
        {
            str(item["path"])
            for item in evidence
            if str(item["path"]).startswith(CORPUS_RECEIPT_DIR)
            and str(item["path"]).endswith("-receipt.json")
        }
    )
    findings: list[str] = []
    producers: set[str] = set()
    for raw in cited:
        path = REPO_ROOT / raw
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            findings.append(f"{gate_id}: corpus receipt {raw} cannot be read: {error}")
            continue
        application = (record.get("authoring") or {}).get("application")
        if not application:
            findings.append(f"{gate_id}: corpus receipt {raw} names no authoring application")
        else:
            producers.add(str(application))
        pins = _pinned_files(record)
        if not pins:
            findings.append(f"{gate_id}: corpus receipt {raw} pins no fixture by digest")
        for pin in pins:
            fixture = path.parent / str(pin["file"])
            try:
                actual = hashlib.sha256(fixture.read_bytes()).hexdigest()
            except OSError:
                findings.append(f"{gate_id}: {raw} pins {pin['file']}, which cannot be read")
                continue
            if actual != pin["sha256"]:
                findings.append(
                    f"{gate_id}: {raw} pins {pin['file']} at {pin['sha256']}, but the "
                    f"file hashes to {actual}"
                )
    if len(producers) < CORPUS_MINIMUM_PRODUCERS:
        findings.append(
            f"{gate_id}: status is passed but the cited corpus receipts under "
            f"{CORPUS_RECEIPT_DIR} name {len(producers)} distinct producing "
            f"application(s); an external fixture corpus needs {CORPUS_MINIMUM_PRODUCERS}"
        )
    return findings


def check_statuses(gates: Sequence[Mapping[str, Any]]) -> list[str]:
    """The fail-closed status rules.

    A ``passed`` gate must rest on evidence this repository can produce, must
    cite at least one artefact, and must claim nothing is missing. Every other
    status must say exactly what is missing. ``failed`` also needs evidence,
    because a failure that was never observed is not a result.
    """
    findings: list[str] = []
    sections = {family: section for family, _count, section in EXPECTED_FAMILIES}

    for gate in gates:
        gate_id = str(gate["gate_id"])
        family = gate_id.split("-", 1)[0]
        status = gate["status"]
        evidence_type = gate["evidence_type"]
        evidence = gate["evidence"]
        missing = gate["missing_condition"]

        if status not in STATUSES:
            findings.append(
                f"{gate_id}: status {status!r} is not one of {', '.join(STATUSES)}"
            )
            continue
        if evidence_type not in EVIDENCE_TYPES:
            findings.append(
                f"{gate_id}: evidence_type {evidence_type!r} is not one of "
                f"{', '.join(EVIDENCE_TYPES)}"
            )
            continue

        if gate["design_section"] != sections[family]:
            findings.append(
                f"{gate_id}: design_section is {gate['design_section']!r}; every "
                f"{family} gate belongs to section {sections[family]}"
            )

        if status == "passed":
            retained_record = evidence_type == "real_host_measurement" and any(
                str(item["path"]).startswith(RETAINED_RECORD_DIR)
                and str(item["path"]).endswith(".json")
                for item in evidence
            )
            if evidence_type == "external_fixture_corpus":
                findings.extend(check_corpus_receipts(gate_id, evidence))
            elif evidence_type not in REPOSITORY_PRODUCIBLE and not retained_record:
                findings.append(
                    f"{gate_id}: status is passed but evidence_type is "
                    f"{evidence_type!r}, which this repository cannot produce. A gate "
                    "needing an installed signed build or an accessibility review cannot "
                    "be passed by a test run, a measured host only through a retained "
                    f"record under {RETAINED_RECORD_DIR}, and an external fixture corpus "
                    f"only through digest-bound receipts under {CORPUS_RECEIPT_DIR}"
                )
            if missing is not None:
                findings.append(
                    f"{gate_id}: status is passed but missing_condition is set; a "
                    "passed gate has nothing missing"
                )
        elif missing is None:
            findings.append(
                f"{gate_id}: status is {status} but missing_condition is null; every "
                "unmet gate must name the exact condition that is absent"
            )

        if status in EVIDENCE_REQUIRED and not evidence:
            findings.append(
                f"{gate_id}: status is {status} but no evidence is cited; a "
                f"{status} result must point at what was observed"
            )
    return findings


def check_evidence_paths(gates: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every cited path exists, and every named symbol still occurs in it.

    The symbol check is a substring match against the file's text rather than a
    parse, because the cited files are Swift, Python, JSON and Markdown. It is
    deliberately the weakest check that still fails when a test is renamed or
    deleted, which is the staleness this guards against.
    """
    findings: list[str] = []
    text_cache: dict[Path, str | None] = {}

    def body(path: Path) -> str | None:
        if path not in text_cache:
            try:
                text_cache[path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text_cache[path] = None
        return text_cache[path]

    for gate in gates:
        gate_id = str(gate["gate_id"])

        for binding in gate["source_bindings"]:
            if not (REPO_ROOT / str(binding)).exists():
                findings.append(
                    f"{gate_id}: source_bindings names {binding}, which does not "
                    "exist in this checkout"
                )

        for index, reference in enumerate(gate["evidence"]):
            raw = str(reference["path"])
            target = REPO_ROOT / raw
            if not target.is_file():
                findings.append(
                    f"{gate_id}: evidence[{index}] names {raw}, which is not a file "
                    "in this checkout"
                )
                continue
            symbol = reference.get("symbol")
            if symbol is None:
                continue
            text = body(target)
            if text is None:
                findings.append(
                    f"{gate_id}: evidence[{index}] names symbol {symbol} in {raw}, "
                    "which cannot be read as UTF-8 text"
                )
            elif str(symbol) not in text:
                findings.append(
                    f"{gate_id}: evidence[{index}] names symbol {symbol}, which no "
                    f"longer occurs in {raw}; the reference is stale"
                )

        if not gate["source_bindings"] and gate["status"] != "not_implemented":
            findings.append(
                f"{gate_id}: source_bindings is empty, which is only honest when the "
                "status is not_implemented"
            )
    return findings


def check(document: object, schema: object) -> list[str]:
    """Every check, in order. Pure: it reads no argument from the command line.

    Schema failures short-circuit, because the later checks index into fields the
    schema is what guarantees are present.
    """
    schema_errors = check_schema(document, schema)
    if schema_errors:
        return schema_errors
    ledger = cast("Mapping[str, Any]", document)
    gates = cast("Sequence[Mapping[str, Any]]", ledger["gates"])
    findings = check_completeness(gates) + check_counts(ledger)
    if findings:
        return findings
    return check_statuses(gates) + check_evidence_paths(gates)


def tally(gates: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Counts by family then status, families and statuses in their fixed order."""
    counts = {
        family: dict.fromkeys(STATUSES, 0)
        for family, _count, _section in EXPECTED_FAMILIES
    }
    for gate in gates:
        counts[str(gate["gate_id"]).split("-", 1)[0]][str(gate["status"])] += 1
    return counts


STATUS_MEANINGS: Final[tuple[tuple[str, str], ...]] = (
    (
        "passed",
        ("Direct production-path coverage of every condition named, running "
        "unconditionally in this repository's validation."),
    ),
    ("failed", "Evidence exists and shows the required result is not met."),
    (
        "blocked_external",
        ("The implementation is present, but the evidence class cannot be produced "
        "in this repository or its CI."),
    ),
    (
        "not_executed",
        ("The evidence class is obtainable in principle, but the required scope has "
        "not been demonstrated here."),
    ),
    (
        "not_implemented",
        "The production path the gate names does not exist in this checkout.",
    ),
)

# Each entry needs something this repository does not contain, so it is a
# structural boundary rather than a gap in test writing. Kept as data so the
# prose cannot drift away from the evidence classes the ledger uses.
OUT_OF_REACH: Final[tuple[tuple[str, str], ...]] = (
    (
        "Signing and notarisation",
        ("The packaging script emits `candidate_status: qualification_only`, "
        "`signature_status: unsigned` and `production_release_eligible: false`. No "
        "`codesign`, `spctl`, `notarytool` or `stapler` invocation exists, so macOS "
        "has never registered the Services entry, the Finder Quick Action or the "
        "Share extension."),
    ),
    (
        "Security-scoped bookmarks",
        ("Every test substitutes a stub codec encoding the path as "
        "`Data(url.path.utf8)`. `SystemSecurityScopedBookmarkCodec` is referenced by "
        "no test, so grant persistence, rename resolution and replacement ambiguity "
        "are untested."),
    ),
    (
        "Peer code-signing verification",
        ("Only the input-validation path is covered. Verifying a genuinely signed "
        "peer, and rejecting a forged claim, needs signing identities."),
    ),
    (
        "The two extensions",
        ("`Extensions/` is outside the Swift test target, has no tests, and CI does "
        "not even typecheck it."),
    ),
    (
        "Accessibility, appearance and displays",
        ("There is no VoiceOver, keyboard-reachability, snapshot, text-scaling or "
        "multi-display test anywhere."),
    ),
    (
        "Performance",
        ("No p95 measurement exists for popover opening, publication, extraction or "
        "folder scanning. The DOC-05/09 receipt records extraction CPU, wall time "
        "and process peak RSS, but no installed UI CPU, memory, wakeup or disk peak "
        "has been measured."),
    ),
    (
        "Intel and older macOS",
        ("The package declares macOS 13, but nothing has run below the CI runner's "
        "version, and the build targets `arm64` only."),
    ),
    (
        "Real providers and volumes",
        ("There is no iCloud, File Provider, network or removable-volume fixture, and "
        "CI mounts none."),
    ),
    (
        "Fixtures",
        ("The repository now holds a Keynote-authored PPTX, three Microsoft Office "
        "Strict-namespace and three Transitional-namespace OOXML packages (Word, "
        "Excel, PowerPoint) with the ONLYOFFICE x2t rewrite of each in both "
        "families and hash-pair conversion receipts, an ONLYOFFICE-produced "
        "nested-table RTF and an Apple text-system RTF, each with a digest-bound "
        "receipt, a macOS Image I/O HEIC and digest-bound qualification receipts. "
        "DOC-01 and DOC-06 are closed on those. Notes and revisions in RTF remain "
        "adversarial scanner fixtures, not application-authored corpus."),
    ),
    (
        "The pinned external binaries",
        ("RTF extraction needs `pandoc 3.12.1` and OCR needs `tesseract` with the "
        "bundled English and orientation models. Both are now packaged: "
        "`scripts/build-core-companion-app.py` stages them, their relocated native "
        "library closure and their licence notices into the candidate and "
        "digest-pins every byte in the component-set manifest, and production "
        "resolves them from the verified installation payload by absolute path. CI "
        "now installs Tesseract and a SHA-verified official Pandoc 3.12.1 build, "
        "and real macOS Tesseract runs are committed, as are the DOC-13 packaged-"
        "runtime measurements of 2026-10-16 and 2026-10-20 on one arm64 host. The "
        "repository still does not redistribute either engine, so "
        "`scripts/preflight` cannot assemble a candidate, repeat that measurement or "
        "build a signed one without separately supplied runtime payloads."),
    ),
    (
        "Production model-manifest authority",
        ("END-02's cache and pinning mechanisms are implemented and tested with the "
        "explicit development anchor, but no product-owned production trust anchor "
        "has been published. The repository cannot invent that release authority."),
    ),
    (
        "Companion to Core, end to end",
        ("No test has a real companion talking to a real Core service. Swift tests "
        "stop at a fake delivery client; Python tests call the service directly."),
    ),
    (
        "Platform coexistence",
        ("Platform does not exist in this repository, so its four states cannot be "
        "produced."),
    ),
)

READING_AN_ENTRY: Final = """\
Each ledger entry carries the gate's exact id, its transcribed normative summary, the
capability and OS rows it applies to, the evidence class the design requires, the
repository artefacts offered as evidence with the exact condition each one
demonstrates, a status, the exact missing condition when the status is not `passed`,
the honest scope limits of whatever evidence is listed, and the production code the
gate binds to per section 22.

A non-`passed` gate may still cite evidence. That is partial coverage, recorded so the
remaining distance is visible; it is not a coverage claim. Conversely a `passed` gate
still carries limitations, because unit-level coverage of a behaviour is not the
installed, measured, receipted evidence sections 11.15, 17.4, 21.12 and 22 require for
release.
"""


def render_report(ledger: Mapping[str, Any]) -> str:
    """The human-readable status document, generated from the ledger.

    Built as whole blocks rather than a list of fragments so the prose stays
    readable and so no implicit concatenation sits inside a collection.
    """
    gates = cast("Sequence[Mapping[str, Any]]", ledger["gates"])
    counts = tally(gates)
    total = len(gates)
    by_status = {
        status: sum(counts[family][status] for family in counts) for status in STATUSES
    }
    source = ledger["design_source"]
    sections = ", ".join(str(part) for part in source["sections"])

    blocks: list[str] = [
        REPORT_HEADER,
        "# WP-15 acceptance and qualification status\n",
        (f"Every behaviour gate in section 20 of `{source['document']}`\n"
        f"({source['revision']}), sections {sections}.\n"),
        ("This document is generated from\n"
        "`qualification/wp15/qualification-ledger.v1.json` and is verified against it\n"
        "by `scripts/check-wp15-qualification.py`. It records what this checkout can\n"
        "show and what it cannot. **It is not a claim that the release is qualified.**\n"
        f"{by_status['passed']} of {total} gates have direct production-path coverage\n"
        "of every condition they name; the rest carry the exact condition still\n"
        "missing.\n"),
        "## Counts by status\n",
    ]

    rows = "\n".join(
        f"| {status} | {by_status[status]} | {meaning} |"
        for status, meaning in STATUS_MEANINGS
    )
    blocks.append(f"| Status | Gates | Meaning |\n|---|---|---|\n{rows}\n")

    blocks.append("## Counts by family and status\n")
    header = "| Family | Section | Gates | " + " | ".join(STATUSES) + " |"
    divider = "|---|---|---|" + "---|" * len(STATUSES)
    family_rows = [
        f"| {family} | {section} | {count} | "
        + " | ".join(str(counts[family][status]) for status in STATUSES)
        + " |"
        for family, count, section in EXPECTED_FAMILIES
    ]
    family_rows.append(
        f"| **total** | | **{total}** | "
        + " | ".join(str(by_status[status]) for status in STATUSES)
        + " |"
    )
    blocks.append("\n".join([header, divider, *family_rows]) + "\n")

    blocks.append("## Counts by required evidence class\n")
    blocks.append(
        "The evidence class is what the design requires before a gate can be called\n"
        "met. Only the first two can be produced from this repository, so a gate in\n"
        "any other class cannot be recorded as passed from a test run -- the checker\n"
        "refuses that combination. There are two exceptions. `real_host_measurement`\n"
        "may pass only while it cites a measurement record retained under\n"
        f"`{RETAINED_RECORD_DIR}`; the repository re-verifies that record but cannot\n"
        "repeat the measurement. `external_fixture_corpus` may pass only while it\n"
        f"cites digest-bound receipts under `{CORPUS_RECEIPT_DIR}` from at least\n"
        f"{CORPUS_MINIMUM_PRODUCERS} distinct producing applications whose pinned\n"
        "fixtures still hash to the recorded digests.\n"
    )
    evidence_rows = "\n".join(
        f"| {evidence_type} | "
        f"{sum(1 for gate in gates if gate['evidence_type'] == evidence_type)} | "
        f"{'yes' if evidence_type in REPOSITORY_PRODUCIBLE else 'no'} |"
        for evidence_type in EVIDENCE_TYPES
    )
    blocks.append(
        f"| Evidence class | Gates | Producible here |\n|---|---|---|\n{evidence_rows}\n"
    )

    blocks.append("## Gates with direct coverage\n")
    blocks.append(
        "Each of these has a test for every condition its required result names,\n"
        "against the production code path, running unconditionally. The ledger records\n"
        "each one's scope limits.\n"
    )
    passed = [gate for gate in gates if gate["status"] == "passed"]
    if passed:
        covered = "\n".join(
            f"| {gate['gate_id']} | {str(gate['normative_summary']).split('.')[0]}. |"
            for gate in passed
        )
        blocks.append(f"| Gate | Gate summary |\n|---|---|\n{covered}\n")
    else:
        blocks.append("None.\n")

    blocks.append("## What is structurally out of reach here\n")
    blocks.append(
        "These are not gaps in test writing. Each needs something this repository does\n"
        "not contain:\n"
    )
    blocks.append(
        "\n".join(f"- **{title}.** {detail}" for title, detail in OUT_OF_REACH) + "\n"
    )

    blocks.append("## Reading an entry\n")
    blocks.append(READING_AN_ENTRY)

    return "\n".join(blocks)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate the status document instead of failing on drift",
    )
    arguments = parser.parse_args(argv)

    try:
        document = _read_json(LEDGER_PATH)
        schema = _read_json(SCHEMA_PATH)
    except LedgerError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    findings = check(document, schema)
    if findings:
        print(f"FAIL: {_relative(LEDGER_PATH)} does not hold:", file=sys.stderr)
        for diagnostic in findings:
            print(f"  - {diagnostic}", file=sys.stderr)
        return 1

    ledger = cast("Mapping[str, Any]", document)
    rendered = render_report(ledger)
    try:
        current = REPORT_PATH.read_text(encoding="utf-8")
    except OSError:
        current = None

    if current != rendered:
        if not arguments.write:
            print(
                f"FAIL: {_relative(REPORT_PATH)} does not match the ledger.\n"
                "Regenerate with: python scripts/check-wp15-qualification.py --write",
                file=sys.stderr,
            )
            return 1
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {_relative(REPORT_PATH)}")

    gates = cast("Sequence[Mapping[str, Any]]", ledger["gates"])
    counts = tally(gates)
    print(f"OK: {_relative(SCHEMA_PATH)} meta-validates and the ledger matches it")
    print(f"OK: {_relative(LEDGER_PATH)}")
    print(f"  gates: {len(gates)} ({', '.join(f'{f} {c}' for f, c, _ in EXPECTED_FAMILIES)})")
    for status in STATUSES:
        total = sum(counts[family][status] for family in counts)
        per_family = ", ".join(
            f"{family} {counts[family][status]}"
            for family, _c, _s in EXPECTED_FAMILIES
            if counts[family][status]
        )
        print(f"  {status}: {total}" + (f" ({per_family})" if per_family else ""))
    print(f"OK: {_relative(REPORT_PATH)} matches the ledger")
    print(
        "\nThis is a coverage ledger, not a release qualification. "
        "The release is not qualified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
