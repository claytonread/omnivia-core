"""The provider-SPI corpus loader, tested against the corpus and against damage.

Two halves. The first reads the accepted corpus and states what it carries --
42 ordered cases and fixed disposition totals -- so a corpus that drifted fails
here rather than silently changing what a later executing lane proves. The
second half is hostile: every rejection the loader owes is provoked by writing a
mutated copy to `tmp_path`. The corpus file itself is only ever read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.agent_host.conformance import (
    EXPECT_FLAGS,
    EXPECTED_CASE_IDS,
    EXPECTED_DISPOSITION_TOTALS,
    EXPECTED_HOOKS,
    CaseReport,
    ConformanceCase,
    ConformanceCorpus,
    CorpusError,
    CorpusReport,
    ExpectedOutcome,
    load_corpus,
)
from omnivia_core.agent_host.spi import SPI_VERSION, Disposition, Hook

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_FILE = (
    REPO_ROOT
    / "docs"
    / "quality"
    / "fixtures"
    / "core-agent-host-provider-spi"
    / "provider-spi-cases.json"
)


@pytest.fixture(scope="module")
def corpus() -> ConformanceCorpus:
    return load_corpus(CORPUS_FILE)


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
    return loaded


def _written(tmp_path: Path, document: object) -> Path:
    target = tmp_path / "provider-spi-cases.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def _outcome(**overrides: Any) -> ExpectedOutcome:
    """A valid :class:`ExpectedOutcome`, with `overrides` substituted."""
    fields: dict[str, Any] = {
        "disposition": Disposition.REFUSED,
        "rationale": "the composition is refused",
        "flags": dict.fromkeys(EXPECT_FLAGS, False),
    }
    return ExpectedOutcome(**{**fields, **overrides})


def _case(**overrides: Any) -> ConformanceCase:
    """A valid :class:`ConformanceCase`, with `overrides` substituted."""
    fields: dict[str, Any] = {
        "id": "SPI-V-001",
        "title": "a case",
        "family": "composition",
        "hook": Hook.TURN_RETRY,
        "requirements": ["SPI-R-001"],
        "given": {"caller": "adapter"},
        "when": {"action": "retry"},
        "expect": _outcome(),
    }
    return ConformanceCase(**{**fields, **overrides})


#: A valid :class:`ConformanceCorpus` keyword set, to be spread and overridden.
_CORPUS_FIELDS: dict[str, Any] = {
    "fixture_id": "core-agent-host-provider-spi",
    "fixture_version": "1.1.0",
    "status": "candidate-preparation-only",
    "generated_date": "2026-08-03",
    "spi_version": SPI_VERSION,
    "authority": {"pm_base_commit": "a28d2c5"},
    "boundary": {"authorises_acceptance": False},
    "hooks": EXPECTED_HOOKS,
    "cases": (),
}


# --- the accepted corpus ---------------------------------------------------------


def test_the_corpus_loads_forty_two_unique_cases_in_order(
    corpus: ConformanceCorpus,
) -> None:
    assert len(corpus.cases) == 42
    assert tuple(case.id for case in corpus.cases) == EXPECTED_CASE_IDS


def test_the_corpus_carries_the_expected_disposition_totals(
    corpus: ConformanceCorpus,
) -> None:
    assert dict(corpus.disposition_totals) == dict(EXPECTED_DISPOSITION_TOTALS)
    assert sum(EXPECTED_DISPOSITION_TOTALS.values()) == len(corpus.cases)


def test_the_corpus_states_its_identity_and_the_ten_hooks(
    corpus: ConformanceCorpus,
) -> None:
    assert corpus.fixture_id == "core-agent-host-provider-spi"
    assert corpus.fixture_version == "1.1.0"
    assert corpus.spi_version == SPI_VERSION
    assert corpus.hooks == EXPECTED_HOOKS
    document = _document()
    assert corpus.status == document["status"]
    assert corpus.generated_date == document["generated_date"]
    assert dict(corpus.authority) == document["authority"]
    assert dict(corpus.boundary) == document["boundary"]
    assert set(EXPECTED_HOOKS) == set(Hook)
    assert len(EXPECTED_HOOKS) == 10


def test_every_case_carries_a_parsed_hook_and_a_complete_expectation(
    corpus: ConformanceCorpus,
) -> None:
    for case in corpus.cases:
        assert isinstance(case.hook, Hook)
        assert isinstance(case.expect.disposition, Disposition)
        assert case.expect.rationale
        assert tuple(case.expect.flags) == EXPECT_FLAGS
        assert all(isinstance(value, bool) for value in case.expect.flags.values())


def test_lookup_by_identifier_finds_a_case_and_refuses_an_unknown_one(
    corpus: ConformanceCorpus,
) -> None:
    assert corpus.case("SPI-V-001").hook is Hook.CAPTURE_AFTER_TURN
    with pytest.raises(KeyError):
        corpus.case("SPI-V-999")


# --- immutability ----------------------------------------------------------------


def test_nothing_loaded_can_be_mutated_by_a_caller(corpus: ConformanceCorpus) -> None:
    case = corpus.cases[0]
    with pytest.raises(AttributeError):
        case.id = "SPI-V-000"  # type: ignore[misc]
    with pytest.raises(TypeError):
        case.given["caller"] = "someone-else"  # type: ignore[index]
    with pytest.raises(TypeError):
        case.when["action"] = "something else"  # type: ignore[index]
    with pytest.raises(TypeError):
        case.expect.flags["audit_record_required"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        corpus.disposition_totals[Disposition.REFUSED] = 0  # type: ignore[index]


def test_nested_corpus_containers_are_frozen_too(corpus: ConformanceCorpus) -> None:
    capabilities = corpus.cases[0].given["granted_capabilities"]
    assert isinstance(capabilities, tuple)
    assert isinstance(corpus.cases[0].requirements, tuple)


def test_a_second_load_is_unaffected_by_the_first_callers_attempts(
    tmp_path: Path,
) -> None:
    document = _document()
    path = _written(tmp_path, document)
    first = load_corpus(path)
    document["cases"][0]["given"]["caller"] = "tampered"
    assert first.cases[0].given["caller"] != "tampered"
    assert load_corpus(path).cases[0].given == first.cases[0].given


# --- hostile input ---------------------------------------------------------------


def test_a_non_object_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="not an object"):
        load_corpus(_written(tmp_path, ["not", "a", "corpus"]))


def test_malformed_json_is_refused_as_a_corpus_error(tmp_path: Path) -> None:
    target = tmp_path / "provider-spi-cases.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorpusError, match="could not be read") as raised:
        load_corpus(target)
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_a_missing_corpus_file_is_refused_as_a_corpus_error(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="could not be read") as raised:
        load_corpus(tmp_path / "absent.json")
    assert isinstance(raised.value.__cause__, FileNotFoundError)


def test_undecodable_bytes_are_refused_as_a_corpus_error(tmp_path: Path) -> None:
    target = tmp_path / "provider-spi-cases.json"
    target.write_bytes(b"\xff\xfe{}")
    with pytest.raises(CorpusError, match="could not be read") as raised:
        load_corpus(target)
    assert isinstance(raised.value.__cause__, UnicodeDecodeError)


def test_an_unknown_root_key_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["expected_answers"] = {"SPI-V-001": "refused"}
    with pytest.raises(CorpusError, match=r"unknown keys: \['expected_answers'\]"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("fixture_id", "core-connectors", "not 'core-agent-host-provider-spi'"),
        ("fixture_version", "9.9.9", "not '1.1.0'"),
        ("spi_version", "0.9.0", "SPI version"),
        ("fixture_id", 7, "no string 'fixture_id'"),
        ("fixture_version", None, "no string 'fixture_version'"),
        ("spi_version", ["1.0.0"], "no string 'spi_version'"),
    ],
)
def test_a_wrong_or_mistyped_identity_field_is_refused(
    tmp_path: Path, key: str, value: object, match: str
) -> None:
    document = _document()
    document[key] = value
    with pytest.raises(CorpusError, match=match):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize(
    "key",
    [
        "fixture_id",
        "fixture_version",
        "status",
        "generated_date",
        "spi_version",
        "authority",
        "boundary",
        "hooks",
        "cases",
    ],
)
def test_a_missing_root_key_is_refused_by_name(tmp_path: Path, key: str) -> None:
    document = _document()
    del document[key]
    with pytest.raises(CorpusError, match=rf"is missing \['{key}'\]"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("status", 1, "no string 'status'"),
        ("status", None, "no string 'status'"),
        ("generated_date", ["2026-08-03"], "no string 'generated_date'"),
        ("authority", "a28d2c5", "authority is not an object"),
        ("authority", [], "authority is not an object"),
        ("boundary", None, "boundary is not an object"),
        ("boundary", ["implements_product_behaviour"], "boundary is not an object"),
    ],
)
def test_a_mistyped_provenance_field_is_refused(
    tmp_path: Path, key: str, value: object, match: str
) -> None:
    document = _document()
    document[key] = value
    with pytest.raises(CorpusError, match=match):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("value", [{}, "spi.negotiate", 10, None])
def test_hooks_that_are_not_a_list_are_refused(tmp_path: Path, value: object) -> None:
    document = _document()
    document["hooks"] = value
    with pytest.raises(CorpusError, match="hooks as a list"):
        load_corpus(_written(tmp_path, document))


def test_an_unknown_hook_in_the_hook_list_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["hooks"] = [*document["hooks"], "turn.teleport"]
    with pytest.raises(CorpusError, match="unknown hook 'turn.teleport'"):
        load_corpus(_written(tmp_path, document))


def test_a_non_string_hook_in_the_hook_list_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["hooks"] = [*document["hooks"], 3]
    with pytest.raises(CorpusError, match="non-string hook"):
        load_corpus(_written(tmp_path, document))


def test_a_duplicated_hook_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["hooks"][1] = document["hooks"][0]
    with pytest.raises(CorpusError, match="ten hooks once each, in order"):
        load_corpus(_written(tmp_path, document))


def test_a_missing_hook_is_refused(tmp_path: Path) -> None:
    document = _document()
    del document["hooks"][4]
    with pytest.raises(CorpusError, match="ten hooks once each, in order"):
        load_corpus(_written(tmp_path, document))


def test_reordered_hooks_are_refused(tmp_path: Path) -> None:
    document = _document()
    document["hooks"][0], document["hooks"][1] = (
        document["hooks"][1],
        document["hooks"][0],
    )
    assert set(document["hooks"]) == {hook.value for hook in EXPECTED_HOOKS}
    with pytest.raises(CorpusError, match="ten hooks once each, in order"):
        load_corpus(_written(tmp_path, document))


def test_an_empty_hook_list_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["hooks"] = []
    with pytest.raises(CorpusError, match="ten hooks once each, in order"):
        load_corpus(_written(tmp_path, document))


def test_a_case_disposition_that_drifted_is_refused(tmp_path: Path) -> None:
    document = _document()
    expect = document["cases"][0]["expect"]
    keys_before, count_before = list(expect), len(document["cases"])
    expect["disposition"] = next(
        member.value for member in Disposition if member.value != expect["disposition"]
    )
    # Structure, count and order are all intact: only the fixed answer moved.
    assert list(expect) == keys_before
    assert len(document["cases"]) == count_before
    assert [case["id"] for case in document["cases"]] == list(EXPECTED_CASE_IDS)
    with pytest.raises(CorpusError, match="accepted disposition totals"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("value", [{}, "cases", None, 42])
def test_cases_that_are_not_a_list_are_refused(tmp_path: Path, value: object) -> None:
    document = _document()
    document["cases"] = value
    with pytest.raises(CorpusError, match="cases as a list"):
        load_corpus(_written(tmp_path, document))


def test_a_case_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][3] = "SPI-V-004"
    with pytest.raises(CorpusError, match="case 3 is not an object"):
        load_corpus(_written(tmp_path, document))


def test_a_case_missing_a_key_is_refused(tmp_path: Path) -> None:
    document = _document()
    del document["cases"][0]["family"]
    with pytest.raises(CorpusError, match="missing \\['family'\\]"):
        load_corpus(_written(tmp_path, document))


def test_a_case_stating_an_unknown_key_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][0]["expected_answer"] = "refused"
    with pytest.raises(
        CorpusError, match=r"case 0 states unknown keys: \['expected_answer'\]"
    ):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("case_id", ["SPI-V-1", "spi-v-001", "SPI-V-0001", ""])
def test_a_malformed_case_identifier_is_refused(tmp_path: Path, case_id: str) -> None:
    document = _document()
    document["cases"][0]["id"] = case_id
    with pytest.raises(CorpusError, match="malformed id"):
        load_corpus(_written(tmp_path, document))


def test_a_non_string_case_identifier_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][0]["id"] = 1
    with pytest.raises(CorpusError, match="no string 'id'"):
        load_corpus(_written(tmp_path, document))


def test_a_duplicate_case_identifier_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][1]["id"] = document["cases"][0]["id"]
    with pytest.raises(CorpusError, match="repeats a case identifier"):
        load_corpus(_written(tmp_path, document))


def test_cases_out_of_order_are_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][0], document["cases"][1] = (
        document["cases"][1],
        document["cases"][0],
    )
    with pytest.raises(CorpusError, match="SPI-V-001..SPI-V-042 in order"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("drop", [1, 41])
def test_a_wrong_case_count_is_refused(tmp_path: Path, drop: int) -> None:
    document = _document()
    del document["cases"][-drop:]
    with pytest.raises(CorpusError, match="cases, not 42"):
        load_corpus(_written(tmp_path, document))


def test_an_extra_case_is_refused(tmp_path: Path) -> None:
    document = _document()
    extra = json.loads(json.dumps(document["cases"][0]))
    extra["id"] = "SPI-V-043"
    document["cases"].append(extra)
    with pytest.raises(CorpusError, match="cases, not 42"):
        load_corpus(_written(tmp_path, document))


def test_an_unknown_hook_on_a_case_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][0]["hook"] = "memory.forget"
    with pytest.raises(CorpusError, match="SPI-V-001 names unknown hook"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("value", ["SPI-R-023", [7], None])
def test_requirements_that_are_not_a_list_of_strings_are_refused(
    tmp_path: Path, value: object
) -> None:
    document = _document()
    document["cases"][0]["requirements"] = value
    with pytest.raises(CorpusError, match="requirements as strings"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("key", ["given", "when", "expect"])
def test_a_case_section_that_is_not_an_object_is_refused(
    tmp_path: Path, key: str
) -> None:
    document = _document()
    document["cases"][0][key] = ["not", "an", "object"]
    with pytest.raises(CorpusError, match="is not an object"):
        load_corpus(_written(tmp_path, document))


def test_an_unknown_disposition_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][0]["expect"]["disposition"] = "deferred"
    with pytest.raises(CorpusError, match="unknown disposition 'deferred'"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("value", [None, 3, ["refused"]])
def test_a_non_string_disposition_is_refused(tmp_path: Path, value: object) -> None:
    document = _document()
    document["cases"][0]["expect"]["disposition"] = value
    with pytest.raises(CorpusError, match="no string 'disposition'"):
        load_corpus(_written(tmp_path, document))


def test_a_missing_rationale_is_refused(tmp_path: Path) -> None:
    document = _document()
    del document["cases"][0]["expect"]["rationale"]
    with pytest.raises(CorpusError, match="no string 'rationale'"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("flag", EXPECT_FLAGS)
def test_a_missing_expectation_flag_is_refused(tmp_path: Path, flag: str) -> None:
    document = _document()
    del document["cases"][0]["expect"][flag]
    with pytest.raises(CorpusError, match=f"no boolean '{flag}'"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_a_non_boolean_expectation_flag_is_refused(
    tmp_path: Path, value: object
) -> None:
    document = _document()
    document["cases"][0]["expect"]["audit_record_required"] = value
    with pytest.raises(CorpusError, match="no boolean 'audit_record_required'"):
        load_corpus(_written(tmp_path, document))


def test_an_unknown_expectation_key_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["cases"][0]["expect"]["expected_answer"] = "refused"
    with pytest.raises(CorpusError, match="unknown keys"):
        load_corpus(_written(tmp_path, document))


@pytest.mark.parametrize("key", ["error_code", "retry_class", "compatibility_status"])
def test_a_non_string_optional_expectation_field_is_refused(
    tmp_path: Path, key: str
) -> None:
    document = _document()
    document["cases"][0]["expect"][key] = 4
    with pytest.raises(CorpusError, match=f"non-string '{key}'"):
        load_corpus(_written(tmp_path, document))


def test_an_absent_optional_expectation_field_reads_as_none(
    corpus: ConformanceCorpus,
) -> None:
    replay = corpus.case("SPI-V-001").expect
    assert (replay.error_code, replay.retry_class, replay.compatibility_status) == (
        None,
        None,
        None,
    )


# --- report DTOs -----------------------------------------------------------------


def test_a_report_records_what_an_executor_observed(corpus: ConformanceCorpus) -> None:
    report = CaseReport(
        case_id="SPI-V-001",
        passed=True,
        observed=Disposition.IDEMPOTENT_REPLAY,
        detail="replay returned the first recorded result",
    )
    assert report.observed is corpus.case("SPI-V-001").expect.disposition
    with pytest.raises(AttributeError):
        report.passed = False  # type: ignore[misc]


def test_a_report_may_record_that_no_disposition_was_reached() -> None:
    assert CaseReport(case_id="SPI-V-002", passed=False).observed is None


def test_an_aggregate_accepts_totals_that_add_up() -> None:
    reports = (
        CaseReport(case_id="SPI-V-001", passed=True),
        CaseReport(case_id="SPI-V-002", passed=False),
    )
    aggregate = CorpusReport(reports=reports, total=2, passed=1, failed=1)
    assert aggregate.reports == reports


@pytest.mark.parametrize(
    ("total", "passed", "failed", "match"),
    [
        (3, 1, 1, "total does not match"),
        (2, 2, 1, "do not sum to total"),
        (2, 0, 2, "does not match the case reports"),
        (2, 2, 0, "does not match the case reports"),
    ],
)
def test_an_aggregate_refuses_totals_that_do_not_add_up(
    total: int, passed: int, failed: int, match: str
) -> None:
    reports = (
        CaseReport(case_id="SPI-V-001", passed=True),
        CaseReport(case_id="SPI-V-002", passed=False),
    )
    with pytest.raises(ValueError, match=match):
        CorpusReport(reports=reports, total=total, passed=passed, failed=failed)


def test_an_aggregate_refuses_a_case_reported_twice() -> None:
    reports = (
        CaseReport(case_id="SPI-V-001", passed=True),
        CaseReport(case_id="SPI-V-001", passed=True),
    )
    with pytest.raises(ValueError, match="reported more than once"):
        CorpusReport(reports=reports, total=2, passed=2, failed=0)


# --- hostile DTO construction ----------------------------------------------------


def test_an_aggregate_canonicalises_a_list_of_reports() -> None:
    reports = [CaseReport(case_id="SPI-V-001", passed=True)]
    aggregate = CorpusReport(reports=reports, total=1, passed=1, failed=0)  # type: ignore[arg-type]
    assert isinstance(aggregate.reports, tuple)
    reports.append(CaseReport(case_id="SPI-V-002", passed=False))
    assert aggregate.reports == (CaseReport(case_id="SPI-V-001", passed=True),)


@pytest.mark.parametrize(
    ("reports", "match"),
    [
        ({"SPI-V-001": True}, "must be a sequence"),
        ("SPI-V-001", "must be a sequence"),
        (("SPI-V-001",), "must hold only CaseReport entries"),
        ((None,), "must hold only CaseReport entries"),
    ],
)
def test_an_aggregate_refuses_reports_that_are_not_case_reports(
    reports: Any, match: str
) -> None:
    with pytest.raises(TypeError, match=match):
        CorpusReport(reports=reports, total=1, passed=1, failed=0)


@pytest.mark.parametrize("field", ["total", "passed", "failed"])
@pytest.mark.parametrize("value", [True, "1", 1.0, None])
def test_an_aggregate_refuses_a_total_that_is_not_an_integer(
    field: str, value: Any
) -> None:
    totals: dict[str, Any] = {"total": 1, "passed": 1, "failed": 0, field: value}
    with pytest.raises(TypeError, match=f"{field} must be an integer"):
        CorpusReport(reports=(CaseReport(case_id="SPI-V-001", passed=True),), **totals)


@pytest.mark.parametrize("field", ["total", "passed", "failed"])
def test_an_aggregate_refuses_a_negative_total(field: str) -> None:
    totals: dict[str, Any] = {"total": 0, "passed": 0, "failed": 0, field: -1}
    with pytest.raises(ValueError, match=f"{field} must not be negative"):
        CorpusReport(reports=(), **totals)


@pytest.mark.parametrize(
    ("overrides", "expected", "match"),
    [
        ({"case_id": 1}, TypeError, "case_id must be a string"),
        ({"case_id": "SPI-V-1"}, ValueError, "case_id is malformed"),
        ({"case_id": "spi-v-001"}, ValueError, "case_id is malformed"),
        ({"passed": 1}, TypeError, "passed must be a boolean"),
        ({"passed": "yes"}, TypeError, "passed must be a boolean"),
        ({"observed": "refused"}, TypeError, "observed must be a Disposition"),
        ({"detail": None}, TypeError, "detail must be a string"),
    ],
)
def test_a_case_report_refuses_a_mistyped_field(
    overrides: dict[str, Any], expected: type[Exception], match: str
) -> None:
    fields: dict[str, Any] = {"case_id": "SPI-V-001", "passed": True, **overrides}
    with pytest.raises(expected, match=match):
        CaseReport(**fields)


def test_an_outcome_canonicalises_a_mutable_flag_mapping() -> None:
    flags = dict.fromkeys(reversed(EXPECT_FLAGS), True)
    outcome = _outcome(flags=flags)
    flags["audit_record_required"] = False
    assert outcome.flags["audit_record_required"] is True
    assert tuple(outcome.flags) == EXPECT_FLAGS
    with pytest.raises(TypeError):
        outcome.flags["audit_record_required"] = False  # type: ignore[index]


@pytest.mark.parametrize(
    ("overrides", "expected", "match"),
    [
        ({"disposition": "refused"}, TypeError, "disposition must be a Disposition"),
        ({"rationale": None}, TypeError, "rationale must be a string"),
        (
            {"flags": [("audit_record_required", True)]},
            TypeError,
            "flags must be a mapping",
        ),
        (
            {"flags": dict.fromkeys(EXPECT_FLAGS[:-1], True)},
            ValueError,
            "exactly the expectation flags",
        ),
        (
            {"flags": {**dict.fromkeys(EXPECT_FLAGS, True), "extra": True}},
            ValueError,
            "exactly the expectation flags",
        ),
        (
            {"flags": {**dict.fromkeys(EXPECT_FLAGS, True), "host_source_patched": 1}},
            TypeError,
            "must be a boolean",
        ),
        ({"error_code": 7}, TypeError, "error_code must be a string"),
        ({"retry_class": []}, TypeError, "retry_class must be a string"),
    ],
)
def test_an_outcome_refuses_a_mistyped_field(
    overrides: dict[str, Any], expected: type[Exception], match: str
) -> None:
    with pytest.raises(expected, match=match):
        _outcome(**overrides)


def test_a_case_freezes_the_containers_it_was_handed() -> None:
    given: dict[str, Any] = {
        "granted_capabilities": ["memory.read"],
        "nested": {"depth": 1},
    }
    case = _case(requirements=["SPI-R-001"], given=given)
    given["granted_capabilities"].append("memory.write")
    given["nested"]["depth"] = 2
    assert case.given["granted_capabilities"] == ("memory.read",)
    assert case.given["nested"]["depth"] == 1
    assert isinstance(case.requirements, tuple)
    with pytest.raises(TypeError):
        case.given["caller"] = "someone-else"  # type: ignore[index]
    with pytest.raises(TypeError):
        case.given["nested"]["depth"] = 3


def test_a_case_freezes_mutable_containers_nested_inside_a_tuple() -> None:
    nested: dict[str, Any] = {"depth": 1}
    entries: list[Any] = ["memory.read"]
    case = _case(given={"mixed": ({"inner": nested}, entries)})
    nested["depth"] = 2
    entries.append("memory.write")
    mixed = case.given["mixed"]
    assert isinstance(mixed, tuple)
    assert mixed[0]["inner"]["depth"] == 1
    assert mixed[1] == ("memory.read",)
    with pytest.raises(TypeError):
        mixed[0]["inner"]["depth"] = 3


@pytest.mark.parametrize("container", [{"memory.read"}, frozenset({"memory.read"})])
@pytest.mark.parametrize("key", ["given", "when"])
def test_a_case_refuses_a_set_because_it_has_no_fixed_order(
    key: str, container: Any
) -> None:
    with pytest.raises(TypeError, match=f"{key} must not hold a set"):
        _case(**{key: {"granted_capabilities": container}})
    with pytest.raises(TypeError, match=f"{key} must not hold a set"):
        _case(**{key: {"nested": {"deep": [container]}}})


def test_an_outcome_refuses_a_container_where_a_flag_belongs() -> None:
    for value in ({"nested": 1}, ["true"], ("true",), {"true"}):
        with pytest.raises(TypeError, match="must be a boolean"):
            _outcome(
                flags={
                    **dict.fromkeys(EXPECT_FLAGS, True),
                    "host_source_patched": value,
                }
            )


@pytest.mark.parametrize(
    ("overrides", "expected", "match"),
    [
        ({"id": "SPI-V-1"}, ValueError, "id is malformed"),
        ({"id": 1}, TypeError, "id must be a string"),
        ({"title": None}, TypeError, "title must be a string"),
        ({"family": 3}, TypeError, "family must be a string"),
        ({"hook": "turn.retry"}, TypeError, "hook must be a Hook"),
        (
            {"expect": {"disposition": "refused"}},
            TypeError,
            "expect must be an ExpectedOutcome",
        ),
        ({"requirements": "SPI-R-001"}, TypeError, "requirements must be a sequence"),
        ({"requirements": [7]}, TypeError, "requirements must hold only str entries"),
        ({"given": ["caller"]}, TypeError, "given must be a mapping"),
        ({"when": None}, TypeError, "when must be a mapping"),
    ],
)
def test_a_case_refuses_a_mistyped_field(
    overrides: dict[str, Any], expected: type[Exception], match: str
) -> None:
    with pytest.raises(expected, match=match):
        _case(**overrides)


@pytest.mark.parametrize(
    ("overrides", "expected", "match"),
    [
        ({"fixture_id": 1}, TypeError, "fixture_id must be a string"),
        ({"fixture_version": None}, TypeError, "fixture_version must be a string"),
        ({"status": 1}, TypeError, "status must be a string"),
        ({"generated_date": None}, TypeError, "generated_date must be a string"),
        ({"spi_version": []}, TypeError, "spi_version must be a string"),
        ({"authority": ["pm_base_commit"]}, TypeError, "authority must be a mapping"),
        ({"boundary": None}, TypeError, "boundary must be a mapping"),
        ({"hooks": Hook.TURN_RETRY}, TypeError, "hooks must be a sequence"),
        ({"hooks": ("turn.retry",)}, TypeError, "hooks must hold only Hook entries"),
        ({"cases": {"SPI-V-001": None}}, TypeError, "cases must be a sequence"),
        (
            {"cases": ("SPI-V-001",)},
            TypeError,
            "cases must hold only ConformanceCase entries",
        ),
    ],
)
def test_a_corpus_refuses_a_mistyped_field(
    overrides: dict[str, Any], expected: type[Exception], match: str
) -> None:
    with pytest.raises(expected, match=match):
        ConformanceCorpus(**{**_CORPUS_FIELDS, "cases": [_case()], **overrides})


def test_a_corpus_canonicalises_the_containers_it_was_handed() -> None:
    hooks = [*EXPECTED_HOOKS]
    cases = [_case()]
    authority: dict[str, Any] = {"pm_base_commit": "a28d2c5"}
    corpus = ConformanceCorpus(
        **{**_CORPUS_FIELDS, "hooks": hooks, "cases": cases, "authority": authority}
    )
    hooks.clear()
    cases.clear()
    authority["pm_base_commit"] = "tampered"
    assert corpus.hooks == EXPECTED_HOOKS
    assert corpus.cases == (_case(),)
    assert corpus.authority["pm_base_commit"] == "a28d2c5"
    with pytest.raises(TypeError):
        corpus.authority["pm_base_commit"] = "tampered"  # type: ignore[index]
    with pytest.raises(TypeError):
        corpus.boundary["authorises_acceptance"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        corpus.status = "accepted"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        corpus.generated_date = "2026-01-01"  # type: ignore[misc]


def test_a_corpus_freezes_nested_provenance_containers() -> None:
    reviewers: list[Any] = ["pm"]
    corpus = ConformanceCorpus(
        **{**_CORPUS_FIELDS, "authority": {"nested": {"reviewers": reviewers}}}
    )
    reviewers.append("core")
    assert corpus.authority["nested"]["reviewers"] == ("pm",)
    with pytest.raises(TypeError):
        corpus.authority["nested"]["reviewers"] = ()


def test_the_corpus_file_was_only_ever_read() -> None:
    document = _document()
    assert document["fixture_id"] == "core-agent-host-provider-spi"
    assert len(document["cases"]) == 42
