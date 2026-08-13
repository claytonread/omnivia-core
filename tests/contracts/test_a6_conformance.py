"""V06-8 packet A6-N1: the corpus-driven conformance kit over all 65 cases.

The exit gate this module holds open, stated as the packet states it: every
case executes with a recorded disposition; no case is silently skipped; the
storage-boundary and credential cases run with no workspace present and record
their limits rather than a containment claim; `CON-C058` records that the host
accepted a chained cursor that skipped observations and reports the omission
from the fake's known expected set; `CON-C059` recomputes all three positive
links and `CON-C060`-`CON-C065` refuse a repeated baseline successor after any
one parent field or binding changes; the migration vectors compare the witness
and predecessor digest exactly; the packaging assertion passes; and the fake
replays byte-identically twice.

The last test is the one that keeps the rest honest. It breaks the digest
construction and requires the lineage cases to *fail*: a kit that merely echoed
each case's `expected_outcome` would stay green through that, and this one does
not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnivia_core.connector.conformance import (
    FORBIDDEN_IMPORT_ROOTS,
    SDK_MODULE_NAMES,
    ConformanceReport,
    Corpus,
    connector_import_defects,
    format_report,
    load_corpus,
    run_conformance,
)
from omnivia_core.connector.spi import ConformanceDisposition

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "docs" / "quality" / "fixtures" / "core-connectors"

#: The cases this kit cannot execute, each with the discovery-declared
#: limitation it is recorded under. Enumerated rather than counted, so a case
#: that quietly stopped executing is a diff on this list.
EXPECTED_NOT_APPLICABLE = {
    "CON-C012": "durable_persistence",
    "CON-C013": "durable_persistence",
    "CON-C014": "durable_persistence",
    "CON-C019": "durable_persistence",
    "CON-C020": "durable_persistence",
    "CON-C021": "acl_withdrawal",
    "CON-C023": "durable_persistence",
    "CON-C024": "durable_persistence",
    "CON-C025": "durable_persistence",
    "CON-C026": "durable_persistence",
    "CON-C027": "durable_persistence",
    "CON-C028": "durable_persistence",
    "CON-C033": "durable_persistence",
    "CON-C038": "durable_persistence",
    "CON-C039": "durable_persistence",
    "CON-C040": "durable_persistence",
    "CON-C047": "durable_persistence",
}


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


@pytest.fixture(scope="module")
def report(corpus: Corpus) -> ConformanceReport:
    return run_conformance(corpus)


# --- the exit gate ------------------------------------------------------------


def test_every_case_executes_with_a_recorded_disposition(
    corpus: Corpus, report: ConformanceReport
) -> None:
    assert [item.case_id for item in report.results] == [
        case.id for case in corpus.cases
    ]
    assert len(report.results) == 65
    for item in report.results:
        assert isinstance(item.disposition, ConformanceDisposition)
        assert item.reason, item.case_id


def test_no_case_fails(report: ConformanceReport) -> None:
    failures = [f"{item.case_id}: {item.reason}" for item in report.failed]
    assert failures == [], "\n".join(failures)


def test_every_not_applicable_names_a_declared_limitation(
    report: ConformanceReport,
) -> None:
    """An undeclared limitation is a failure, not a not-applicable."""
    from omnivia_core.connector.conformance import FAKE_DECLARED_LIMITATIONS

    recorded = {
        item.case_id: item.declared_limitation for item in report.not_applicable
    }
    assert recorded == EXPECTED_NOT_APPLICABLE
    for item in report.not_applicable:
        assert item.declared_limitation in FAKE_DECLARED_LIMITATIONS
        assert len(item.reason) > 20, item.case_id


def test_the_executed_cases_carry_evidence_rather_than_a_bare_verdict(
    report: ConformanceReport,
) -> None:
    for item in report.passed:
        assert item.evidence, item.case_id
        assert all(line.strip() for line in item.evidence), item.case_id


def test_the_report_renders_one_line_per_case(report: ConformanceReport) -> None:
    rendered = format_report(report).splitlines()
    assert len(rendered) == 66
    assert rendered[-1] == (
        f"passed={len(report.passed)} failed=0 "
        f"declared_not_applicable={len(report.not_applicable)}"
    )


# --- the cases the packet names explicitly ---------------------------------------


def test_con_c058_records_acceptance_and_a_completeness_failure(
    report: ConformanceReport,
) -> None:
    result = report.result("CON-C058")
    assert result.disposition is ConformanceDisposition.PASSED
    evidence = " | ".join(result.evidence)
    assert "advanced the cursor from witness 9 to 100" in evidence
    assert "91 observations are lost, not deferred" in evidence
    assert "corpus-fixed expected set" in evidence
    assert "never from the cursor check" in evidence
    assert "CON-R35" in evidence
    assert "duplication and corruption only" in evidence
    # Nothing anywhere in the evidence may read as a completeness claim.
    assert "proves completeness" not in evidence
    assert "no observation was skipped" not in evidence


def test_con_c059_recomputes_every_displayed_frame(report: ConformanceReport) -> None:
    result = report.result("CON-C059")
    assert result.disposition is ConformanceDisposition.PASSED
    recomputed = [line for line in result.evidence if line.startswith("vector ")]
    assert len(recomputed) == 4
    assert "all three links accepted" in result.evidence[-1]


@pytest.mark.parametrize(
    "case_id, changed, error",
    [
        ("CON-C060", "state_version", "connector_state_invalid"),
        ("CON-C061", "payload", "connector_state_invalid"),
        ("CON-C062", "witness_seq", "connector_state_invalid"),
        ("CON-C063", "predecessor_digest", "connector_state_invalid"),
        ("CON-C064", "workspace_id", "connector_cursor_foreign"),
        ("CON-C065", "connector_id", "connector_cursor_foreign"),
    ],
)
def test_each_differential_refuses_the_repeated_baseline_successor(
    report: ConformanceReport, case_id: str, changed: str, error: str
) -> None:
    result = report.result(case_id)
    assert result.disposition is ConformanceDisposition.PASSED
    evidence = " | ".join(result.evidence)
    assert f"differ in exactly {changed}" in evidence
    assert "independent recomputation produced" in evidence
    assert f"reported as {error}" in evidence


def test_the_migration_cases_compare_witness_and_predecessor_exactly(
    report: ConformanceReport,
) -> None:
    accepted = " | ".join(report.result("CON-C007").evidence)
    assert "witness preserved exactly" in accepted
    assert "predecessor digest byte-identical" in accepted
    assert "byte-identical output" in accepted
    assert "pre- and post-migration canonical digests recorded as a pair" in accepted
    assert "connector_state_invalid" not in accepted

    witness = " | ".join(report.result("CON-C052").evidence)
    assert "forward witness was refused" in witness
    predecessor = " | ".join(report.result("CON-C053").evidence)
    assert "present-to-absent and absent-to-present" in predecessor
    resync = " | ".join(report.result("CON-C008").evidence)
    assert "explicit resynchronization" in resync


def test_the_storage_and_credential_cases_state_their_limits(
    report: ConformanceReport,
) -> None:
    storage = " | ".join(report.result("CON-C032").evidence)
    assert "no workspace present" in storage
    assert "API ownership boundary only" in storage
    assert "CON-P09" in storage
    assert "sandbox" not in storage.lower()

    credentials = " | ".join(report.result("CON-C030").evidence)
    assert "transformed form was NOT caught" in credentials
    assert "defence in depth, never information-flow enforcement" in credentials
    assert "CON-P06/CON-P09" in credentials


def test_the_fake_case_records_a_byte_identical_replay(
    report: ConformanceReport,
) -> None:
    evidence = " | ".join(report.result("CON-C050").evidence)
    assert "identical observation sequences and cursors" in evidence
    assert "no network access, no credential resolution, no workspace path" in evidence


# --- packaging ---------------------------------------------------------------------


def test_the_connector_sdk_declares_no_forbidden_import() -> None:
    assert connector_import_defects() == ()


def test_the_scan_covers_every_module_in_the_package() -> None:
    package = REPO_ROOT / "src" / "omnivia_core" / "connector"
    on_disk = sorted(path.stem for path in package.glob("*.py"))
    assert on_disk == sorted(SDK_MODULE_NAMES)


def test_importing_the_sdk_in_a_fresh_interpreter_pulls_in_nothing_forbidden() -> None:
    """Measured out of process, because this one has other reasons to hold them."""
    probe = (
        "import sys, json;"
        "import omnivia_core.connector.spi;"
        "import omnivia_core.connector.host;"
        "import omnivia_core.connector.fake;"
        "import omnivia_core.connector.conformance;"
        "print(json.dumps(sorted(sys.modules)))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    loaded = set(json.loads(completed.stdout))
    for name in (*sorted(FORBIDDEN_IMPORT_ROOTS), "sqlite3", "sqlalchemy", "jsonschema"):
        assert name not in loaded, name


def test_the_contract_and_host_modules_reach_for_no_filesystem() -> None:
    """`conformance` reads the corpus off disk and is allowed a path; the
    contract, the host checks and the fake are not."""
    package = REPO_ROOT / "src" / "omnivia_core" / "connector"
    for name in ("spi", "host", "fake"):
        source = (package / f"{name}.py").read_text(encoding="utf-8")
        assert "pathlib" not in source, name
        assert "sqlite3" not in source, name
        assert "open(" not in source, name


# --- the kit is not echoing the corpus ----------------------------------------------


@pytest.fixture
def broken_digest() -> Iterator[None]:
    """Drop the parent's own predecessor digest from the preimage.

    That is the exact A6-R2 defect: two states with identical values for the
    remaining fields but different parents hash alike.
    """
    from omnivia_core.connector import host

    original = host.cursor_digest_preimage

    def truncated(binding: object, state: object) -> bytes:
        full = original(binding, state)  # type: ignore[arg-type]
        predecessor = getattr(state, "predecessor_digest", None)
        trailer = 4 + (0 if predecessor is None else 32)
        return full[:-trailer]

    host.cursor_digest_preimage = truncated  # type: ignore[assignment]
    yield
    host.cursor_digest_preimage = original  # type: ignore[assignment]


@pytest.mark.usefixtures("broken_digest")
def test_a_broken_preimage_fails_the_lineage_cases(corpus: Corpus) -> None:
    broken = run_conformance(corpus)
    failed = {item.case_id for item in broken.failed}
    assert {"CON-C059", "CON-C063"} <= failed, sorted(failed)
    assert broken.result("CON-C063").disposition is ConformanceDisposition.FAILED


def test_a_case_with_no_assertion_binding_is_a_failure(corpus: Corpus) -> None:
    """A missing binding must not read as a pass or as a not-applicable."""
    from omnivia_core.connector import conformance as kit

    handlers = dict(kit._HANDLERS)
    del handlers["CON-C001"]
    original = kit._HANDLERS
    kit._HANDLERS = handlers  # type: ignore[assignment]
    try:
        result = run_conformance(corpus).result("CON-C001")
    finally:
        kit._HANDLERS = original  # type: ignore[assignment]
    assert result.disposition is ConformanceDisposition.FAILED
    assert "no assertion binding" in result.reason
