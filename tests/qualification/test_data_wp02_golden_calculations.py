"""WP02 golden-calculation acceptance tests.

Loads ``qualification/data/wp02/golden.py`` by file path (no package marker,
matching the existing ``qualification/semantic_index/q1`` convention) and
checks every frozen fixture against its acceptance value. Independent
fixture evidence only -- no engine, no SQL, no connector.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "qualification"
    / "data"
    / "wp02"
    / "golden.py"
)
_SPEC = importlib.util.spec_from_file_location("wp02_golden_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
golden: Any = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = (
    golden  # dataclasses + `from __future__ import annotations` need this
)
_SPEC.loader.exec_module(golden)


# --------------------------------------------------------------------------
# Documentation labeling
# --------------------------------------------------------------------------


def test_evidence_is_labeled_independent_fixture_only() -> None:
    assert golden.EVIDENCE_LABEL == "independent fixture"
    assert golden.PRODUCTION_QUALIFICATION_STATUS == "not_executed"
    assert golden.INTEGRATION_QUALIFICATION_STATUS == "not_executed"


# --------------------------------------------------------------------------
# Case 1 -- Finance/Cashflow baseline
# --------------------------------------------------------------------------


def test_cashflow_currency_and_timezone_are_the_normative_baseline() -> None:
    assert golden.CASHFLOW_CURRENCY == "AUD"
    assert golden.CASHFLOW_TIMEZONE == "Australia/Sydney"


def _residual_by_invoice_id(as_of: date) -> dict[str, Any]:
    return {
        inv.invoice_id: golden._invoice_residual_balance(
            inv, golden.CASHFLOW_EVENTS, as_of
        )
        for inv in golden.CASHFLOW_INVOICES
    }


def test_cashflow_inv_b_due_date_is_authoritative() -> None:
    inv_b = next(inv for inv in golden.CASHFLOW_INVOICES if inv.invoice_id == "INV-B")
    assert inv_b.due_date == date(2026, 9, 5)


def test_cashflow_invoice_rows_are_frozen_exact() -> None:
    # Direct exact tuple freeze of all five authoritative invoice rows, so a
    # within-row compensating change (e.g. one invoice's cents and another's
    # due date drifting in opposite directions) cannot hide behind an
    # unchanged aggregate total.
    assert tuple(
        (inv.invoice_id, inv.amount_cents, inv.due_date, inv.status)
        for inv in golden.CASHFLOW_INVOICES
    ) == (
        ("INV-A", 110_000, date(2026, 9, 1), "open"),
        ("INV-B", 55_000, date(2026, 9, 5), "open"),
        ("INV-C", 22_000, date(2026, 9, 12), "open"),
        ("INV-D", 40_000, date(2026, 9, 1), "void"),
        ("INV-E", 10_000, date(2026, 9, 1), "disputed"),
    )


def test_cashflow_event_rows_are_frozen_exact() -> None:
    # Direct exact tuple freeze of all five authoritative event rows, for the
    # same reason as the invoice-row freeze above.
    assert tuple(
        (ev.invoice_id, ev.kind, ev.amount_cents, ev.event_date)
        for ev in golden.CASHFLOW_EVENTS
    ) == (
        ("INV-A", "payment", 30_000, date(2026, 9, 3)),
        ("INV-A", "payment", 20_000, date(2026, 9, 7)),
        ("INV-A", "payment", 60_000, date(2026, 9, 15)),
        ("INV-B", "credit", 5_000, date(2026, 9, 2)),
        ("INV-C", "payment", 22_000, date(2026, 9, 13)),
    )


def test_cashflow_overdue_balance_2026_09_12() -> None:
    # INV-A 60,000 (110,000 gross less 50,000 paid on/before 09-12) + INV-B
    # 50,000 (55,000 less a 5,000 credit) + INV-E 10,000 (disputed, still
    # counted). INV-C is due exactly 09-12 (excluded) and INV-D is void.
    total = golden.overdue_balance(
        golden.CASHFLOW_INVOICES, golden.CASHFLOW_EVENTS, date(2026, 9, 12)
    )
    assert total == 120_000
    # Exact per-row residuals, so a compensating drift between two rows
    # cannot hide behind an unchanged aggregate total.
    residuals = _residual_by_invoice_id(date(2026, 9, 12))
    assert residuals["INV-A"] == 60_000
    assert residuals["INV-B"] == 50_000
    assert residuals["INV-C"] == 22_000  # due exactly today: excluded, not paid yet
    assert residuals["INV-D"] == 40_000  # void: excluded regardless of residual
    assert residuals["INV-E"] == 10_000
    assert residuals["INV-A"] + residuals["INV-B"] + residuals["INV-E"] == total


def test_cashflow_overdue_balance_2026_09_19() -> None:
    # INV-A is fully paid (0), INV-C is fully paid the day after it became
    # due (0), leaving INV-B 50,000 + INV-E 10,000 -- a 50% reduction.
    total = golden.overdue_balance(
        golden.CASHFLOW_INVOICES, golden.CASHFLOW_EVENTS, date(2026, 9, 19)
    )
    assert total == 60_000
    assert total * 2 == 120_000
    residuals = _residual_by_invoice_id(date(2026, 9, 19))
    assert residuals["INV-A"] == 0
    assert residuals["INV-B"] == 50_000
    assert residuals["INV-C"] == 0
    assert residuals["INV-E"] == 10_000
    assert residuals["INV-B"] + residuals["INV-E"] == total


def test_cashflow_void_invoice_excluded_at_both_dates() -> None:
    without_void = tuple(
        inv for inv in golden.CASHFLOW_INVOICES if inv.status != "void"
    )
    for as_of in (date(2026, 9, 12), date(2026, 9, 19)):
        assert golden.overdue_balance(
            golden.CASHFLOW_INVOICES, golden.CASHFLOW_EVENTS, as_of
        ) == (golden.overdue_balance(without_void, golden.CASHFLOW_EVENTS, as_of))


def test_cashflow_disputed_invoice_is_included() -> None:
    disputed = next(inv for inv in golden.CASHFLOW_INVOICES if inv.status == "disputed")
    assert disputed.invoice_id == "INV-E"
    without_disputed = tuple(
        inv for inv in golden.CASHFLOW_INVOICES if inv.invoice_id != "INV-E"
    )
    total_with = golden.overdue_balance(
        golden.CASHFLOW_INVOICES, golden.CASHFLOW_EVENTS, date(2026, 9, 12)
    )
    total_without = golden.overdue_balance(
        without_disputed, golden.CASHFLOW_EVENTS, date(2026, 9, 12)
    )
    assert total_with == total_without + disputed.amount_cents


def test_cashflow_due_today_excluded() -> None:
    due_today = next(
        inv for inv in golden.CASHFLOW_INVOICES if inv.due_date == date(2026, 9, 12)
    )
    assert due_today.invoice_id == "INV-C"
    # Not counted at 2026-09-12 precisely because due_date == as_of_date.
    with_it = golden.overdue_balance((due_today,), (), date(2026, 9, 12))
    assert with_it == 0
    # The moment as_of_date advances past it, it would count (before any event).
    with_it_later = golden.overdue_balance((due_today,), (), date(2026, 9, 13))
    assert with_it_later == due_today.amount_cents


def test_cashflow_payments_and_credits_apply_through_as_of_date() -> None:
    paid_invoice = next(
        inv for inv in golden.CASHFLOW_INVOICES if inv.invoice_id == "INV-A"
    )
    payments = tuple(ev for ev in golden.CASHFLOW_EVENTS if ev.invoice_id == "INV-A")
    # 09-14: the first two payments (30,000 + 20,000) have landed, the third
    # (60,000, dated 09-15) has not.
    before_final_payment = golden.overdue_balance(
        (paid_invoice,), payments, date(2026, 9, 14)
    )
    assert before_final_payment == 60_000
    # 09-16: all three payments have landed, fully clearing the invoice.
    after_final_payment = golden.overdue_balance(
        (paid_invoice,), payments, date(2026, 9, 16)
    )
    assert after_final_payment == 0

    credited_invoice = next(
        inv for inv in golden.CASHFLOW_INVOICES if inv.invoice_id == "INV-B"
    )
    credit = next(ev for ev in golden.CASHFLOW_EVENTS if ev.invoice_id == "INV-B")
    # The credit is partial: 55,000 gross less a 5,000 credit leaves 50,000.
    assert (
        golden.overdue_balance((credited_invoice,), (credit,), date(2026, 9, 20))
        == 50_000
    )


def test_cashflow_null_due_date_fails_closed() -> None:
    result = golden.overdue_balance(
        golden.NULL_DUE_DATE_INVOICES, (), date(2026, 9, 20)
    )
    assert isinstance(result, golden.Unavailable)


def test_cashflow_average_overdue_amount_is_exact() -> None:
    # Overdue-by-due-date at 2026-09-12: INV-A, INV-B and INV-E (INV-C is due
    # exactly 09-12, not yet overdue; INV-D is void). 120,000 / 3.
    average = golden.average_overdue_amount(
        golden.CASHFLOW_INVOICES, golden.CASHFLOW_EVENTS, date(2026, 9, 12)
    )
    assert average == Decimal(40_000)


def test_cashflow_average_overdue_amount_zero_denominator_fails_closed() -> None:
    result = golden.average_overdue_amount((), (), date(2026, 9, 12))
    assert isinstance(result, golden.Unavailable)


def test_cashflow_average_overdue_amount_null_due_date_fails_closed() -> None:
    result = golden.average_overdue_amount(
        golden.NULL_DUE_DATE_INVOICES, (), date(2026, 9, 20)
    )
    assert isinstance(result, golden.Unavailable)


def test_cashflow_average_overdue_amount_mixed_valid_and_null_fails_closed() -> None:
    # One invoice has a real positive overdue balance; the other has a null
    # due_date. The null must fail the whole average closed, never let the
    # valid invoice's balance be silently averaged on its own.
    result = golden.average_overdue_amount(
        golden.MIXED_VALID_AND_NULL_DUE_DATE_INVOICES, (), date(2026, 9, 20)
    )
    assert isinstance(result, golden.Unavailable)


def test_cashflow_average_overdue_amount_sep19_excludes_settled_invoices() -> None:
    # At 2026-09-19, INV-A and INV-C are overdue by due date but fully
    # settled (residual 0); only INV-B (50,000) and INV-E (10,000) still
    # carry a positive balance, so the average is 60,000 / 2, not / 4.
    average = golden.average_overdue_amount(
        golden.CASHFLOW_INVOICES, golden.CASHFLOW_EVENTS, date(2026, 9, 19)
    )
    assert average == Decimal(30_000)


def test_cashflow_unsupported_event_kind_fails_closed() -> None:
    result = golden.overdue_balance(
        golden.UNSUPPORTED_EVENT_KIND_INVOICES,
        golden.UNSUPPORTED_EVENT_KIND_EVENTS,
        date(2026, 9, 20),
    )
    assert isinstance(result, golden.Unavailable)


def test_cashflow_over_applied_residual_fails_closed() -> None:
    result = golden.overdue_balance(
        golden.OVER_APPLIED_RESIDUAL_INVOICES,
        golden.OVER_APPLIED_RESIDUAL_EVENTS,
        date(2026, 9, 20),
    )
    assert isinstance(result, golden.Unavailable)


def test_cashflow_raw_fixture_file_hash_matches_manifest() -> None:
    """The checked-in fixture file's exact raw bytes hash to the recorded
    value -- a raw-artifact integrity check, not a canonical re-encoding."""
    raw_bytes = golden.read_fixture_bytes("cashflow.json")
    manifest = golden.read_fixture_manifest()
    assert golden.sha256_hex(raw_bytes) == manifest["cashflow.json"]


def test_cashflow_raw_fixture_content_matches_live_fixture() -> None:
    """Drift check: the live Python fixture and the checked-in raw JSON file
    describe the same data. This is a content comparison, not a hash."""
    from_disk = json.loads(golden.read_fixture_bytes("cashflow.json"))
    assert from_disk == golden._cashflow_raw_snapshot()


# --------------------------------------------------------------------------
# Case 2 / Q14 / Q29 / Q30 -- Research fanout, Q14 safe-path semijoin, and
# inactive-project controls
# --------------------------------------------------------------------------


def test_fanout_cust_1_invoices_are_frozen_aud_and_overdue() -> None:
    for invoice in golden.FANOUT_INVOICES_CUST_1:
        assert invoice.currency == "AUD"
        assert invoice.due_date < golden.FANOUT_AS_OF_DATE


def test_q14_safe_customer_exposure_is_120000() -> None:
    exposure = golden.safe_customer_exposure(
        golden.FANOUT_INVOICES_CUST_1, golden.CUST_1
    )
    assert exposure == 120_000
    assert (
        golden.unique_customer_relation(golden.FANOUT_INVOICES_CUST_1)[golden.CUST_1]
        == 120_000
    )


def test_q14_safe_path_is_a_semijoin_over_active_customer_qualifier() -> None:
    projects = golden.three_active_projects_for(golden.CUST_1)
    qualifier = golden.active_customer_qualifier(projects)
    assert qualifier == frozenset({golden.CUST_1})

    exposure = golden.q14_safe_customer_exposure_via_active_project_semijoin(
        golden.FANOUT_INVOICES_CUST_1, projects, golden.CUST_1
    )
    assert exposure == 120_000


def test_q14_semijoin_rejects_customer_with_only_inactive_projects() -> None:
    only_inactive_projects = (golden.INACTIVE_PROJECT_CUST_1,)
    result = golden.q14_safe_customer_exposure_via_active_project_semijoin(
        golden.FANOUT_INVOICES_CUST_1, only_inactive_projects, golden.CUST_1
    )
    assert isinstance(result, golden.Unavailable)


def test_q14_rejoin_to_project_rows_stays_rejected() -> None:
    # The Q14 safe path never permits re-summing across project rows: the
    # rejoin-to-project-rows plan (Q30) stays rejected regardless.
    exposure = golden.unique_customer_relation(golden.FANOUT_INVOICES_CUST_1)
    projects = golden.three_active_projects_for(golden.CUST_1)
    rows = golden.join_exposure_to_projects(exposure, projects)
    rejection = golden.rejoin_and_sum(rows)
    assert isinstance(rejection, golden.Rejected)
    assert rejection.code == "GRAIN_VIOLATION"


def test_q14_safe_customer_exposure_missing_customer_is_unavailable_not_zero() -> None:
    result = golden.safe_customer_exposure(
        golden.FANOUT_INVOICES_CUST_1, golden.MISSING_LINK_CUSTOMER
    )
    assert isinstance(result, golden.Unavailable)
    assert result != 0


def test_q14_naive_fanout_360000_is_rejected() -> None:
    projects = golden.three_active_projects_for(golden.CUST_1)
    forbidden_value = golden.forbidden_naive_fanout_total(
        golden.FANOUT_INVOICES_CUST_1, projects, golden.CUST_1
    )
    assert forbidden_value == 360_000

    rejection = golden.naive_invoice_project_fanout(
        golden.FANOUT_INVOICES_CUST_1, projects, golden.CUST_1
    )
    assert isinstance(rejection, golden.Rejected)
    assert rejection.code == "GRAIN_VIOLATION"


def test_q29_two_equal_amount_invoices_both_count() -> None:
    exposure = golden.safe_customer_exposure(
        golden.FANOUT_INVOICES_CUST_2, golden.CUST_2
    )
    assert exposure == 100_000


def test_q29_naive_fanout_is_300000() -> None:
    projects = golden.three_active_projects_for(golden.CUST_2)
    forbidden_value = golden.forbidden_naive_fanout_total(
        golden.FANOUT_INVOICES_CUST_2, projects, golden.CUST_2
    )
    assert forbidden_value == 300_000


def test_q29_sum_distinct_is_a_forbidden_undercount() -> None:
    undercount = golden.sum_distinct_forbidden(
        golden.FANOUT_INVOICES_CUST_2, golden.CUST_2
    )
    assert undercount == 50_000
    correct = golden.safe_customer_exposure(
        golden.FANOUT_INVOICES_CUST_2, golden.CUST_2
    )
    assert undercount != correct


def test_q30_preaggregate_rejoin_to_three_rows_is_rejected() -> None:
    exposure = golden.unique_customer_relation(golden.FANOUT_INVOICES_CUST_1)
    projects = golden.three_active_projects_for(golden.CUST_1)
    rows = golden.join_exposure_to_projects(exposure, projects)
    assert len(rows) == 3
    assert all(row.exposure_cents == 120_000 for row in rows)

    rejection = golden.rejoin_and_sum(rows)
    assert isinstance(rejection, golden.Rejected)
    assert rejection.code == "GRAIN_VIOLATION"
    assert rejection.identities == tuple(sorted(p.project_id for p in projects))


def test_q30_join_to_proven_unique_customer_relation_is_accepted() -> None:
    exposure = golden.unique_customer_relation(golden.FANOUT_INVOICES_CUST_1)
    # The relation is proven unique: exactly one row per customer_id, by
    # construction of a dict keyed on customer_id.
    assert list(exposure.keys()).count(golden.CUST_1) == 1
    projects = golden.three_active_projects_for(golden.CUST_1)

    # The accepted result comes from the preaggregated customer-grain
    # exposure semijoined against the active-customer qualifier relation --
    # never from the three-row project projection below, which is a safe row
    # projection only and must never be re-summed (see
    # ``join_exposure_to_projects``'s docstring and ``rejoin_and_sum``).
    assert golden.CUST_1 in golden.active_customer_qualifier(projects)
    accepted_total = golden.q14_safe_customer_exposure_via_active_project_semijoin(
        golden.FANOUT_INVOICES_CUST_1, projects, golden.CUST_1
    )
    assert accepted_total == 120_000

    rows = golden.join_exposure_to_projects(exposure, projects)
    assert all(isinstance(row, golden.ProjectExposureRow) for row in rows)


def test_join_exposure_to_projects_ignores_inactive_project() -> None:
    exposure = golden.unique_customer_relation(golden.FANOUT_INVOICES_CUST_1)
    active_projects = golden.three_active_projects_for(golden.CUST_1)
    rows_without_inactive = golden.join_exposure_to_projects(exposure, active_projects)
    rows_with_inactive = golden.join_exposure_to_projects(
        exposure, active_projects + (golden.INACTIVE_PROJECT_CUST_1,)
    )
    # The inactive project contributes no row at all -- not even Unavailable.
    assert rows_with_inactive == rows_without_inactive


def test_naive_fanout_helpers_ignore_inactive_project() -> None:
    active_projects = golden.three_active_projects_for(golden.CUST_1)
    with_inactive = active_projects + (golden.INACTIVE_PROJECT_CUST_1,)

    assert golden.forbidden_naive_fanout_total(
        golden.FANOUT_INVOICES_CUST_1, with_inactive, golden.CUST_1
    ) == golden.forbidden_naive_fanout_total(
        golden.FANOUT_INVOICES_CUST_1, active_projects, golden.CUST_1
    )

    rejection_with_inactive = golden.naive_invoice_project_fanout(
        golden.FANOUT_INVOICES_CUST_1, with_inactive, golden.CUST_1
    )
    rejection_without_inactive = golden.naive_invoice_project_fanout(
        golden.FANOUT_INVOICES_CUST_1, active_projects, golden.CUST_1
    )
    assert rejection_with_inactive == rejection_without_inactive


def test_fanout_missing_customer_link_fails_closed() -> None:
    exposure = golden.unique_customer_relation(golden.ALL_FANOUT_INVOICES)
    missing_project = next(
        p for p in golden.ALL_PROJECTS if p.customer_id == golden.MISSING_LINK_CUSTOMER
    )
    rows = golden.join_exposure_to_projects(exposure, (missing_project,))
    assert len(rows) == 1
    assert isinstance(rows[0], golden.Unavailable)


def test_rejoin_and_sum_propagates_unavailable_input() -> None:
    exposure = golden.unique_customer_relation(golden.ALL_FANOUT_INVOICES)
    missing_project = next(
        p for p in golden.ALL_PROJECTS if p.customer_id == golden.MISSING_LINK_CUSTOMER
    )
    rows = golden.join_exposure_to_projects(exposure, (missing_project,))
    result = golden.rejoin_and_sum(rows)
    assert result is rows[0]
    assert isinstance(result, golden.Unavailable)


def test_rejoin_and_sum_non_fanout_control_returns_exact_sum() -> None:
    # At most one project row per customer: nothing to double-count, so the
    # sum across rows is exact.
    exposure = golden.unique_customer_relation(golden.ALL_FANOUT_INVOICES)
    one_project_per_customer = (
        golden.Project("PROJ-CUST-1-SOLO", golden.CUST_1),
        golden.Project("PROJ-CUST-2-SOLO", golden.CUST_2),
    )
    rows = golden.join_exposure_to_projects(exposure, one_project_per_customer)
    result = golden.rejoin_and_sum(rows)
    assert result == 220_000


def test_fanout_raw_fixture_file_hash_matches_manifest() -> None:
    raw_bytes = golden.read_fixture_bytes("fanout.json")
    manifest = golden.read_fixture_manifest()
    assert golden.sha256_hex(raw_bytes) == manifest["fanout.json"]


def test_fanout_raw_fixture_content_matches_live_fixture() -> None:
    from_disk = json.loads(golden.read_fixture_bytes("fanout.json"))
    assert from_disk == golden._fanout_raw_snapshot()


# --------------------------------------------------------------------------
# Q15 -- contradicted unique-key declaration
# --------------------------------------------------------------------------


def test_q15_contradicted_key_never_returns_a_silent_total() -> None:
    result = golden.customer_exposure_via_declared_unique_master(
        golden.CONTRADICTED_MASTER_ROWS, golden.CONTRADICTED_INVOICES, "CUST-9"
    )
    assert isinstance(result, golden.Rejected)
    assert result.code == "KEY_CONSTRAINT_FAILED"
    assert result.identities == ("ROW-9A", "ROW-9B")
    assert result != 55_000  # the naive, never-computed silent total


def test_q15_identical_duplicate_rows_still_violate_uniqueness() -> None:
    # Both master rows carry the exact same legal_name -- there is nothing
    # for the values to disagree on -- but the row *count* is still two,
    # which alone violates a declared-unique key.
    result = golden.customer_exposure_via_declared_unique_master(
        golden.DUPLICATE_IDENTICAL_MASTER_ROWS,
        golden.DUPLICATE_IDENTICAL_INVOICES,
        "CUST-12",
    )
    assert isinstance(result, golden.Rejected)
    assert result.code == "KEY_CONSTRAINT_FAILED"
    assert result.identities == ("ROW-12A", "ROW-12B")


def test_q15_proven_alternate_plan_still_returns_a_number() -> None:
    result = golden.customer_exposure_via_declared_unique_master(
        golden.CLEAN_MASTER_ROWS, golden.CLEAN_INVOICES, "CUST-11"
    )
    assert result == 22_000


def test_q15_unknown_key_fails_closed() -> None:
    result = golden.customer_exposure_via_declared_unique_master(
        golden.CLEAN_MASTER_ROWS, golden.CLEAN_INVOICES, "CUST-DOES-NOT-EXIST"
    )
    assert isinstance(result, golden.Rejected)
    assert result.code == "KEY_CONSTRAINT_FAILED"


# --------------------------------------------------------------------------
# Q16 -- overlapping half-open validity windows
# --------------------------------------------------------------------------


def test_q16_overlap_returns_temporal_join_ambiguous_with_identities() -> None:
    result = golden.resolve_as_of(
        golden.RATE_VALIDITY_ROWS, "RATE-AUDUSD", date(2026, 9, 17)
    )
    assert isinstance(result, golden.Rejected)
    assert result.code == "TEMPORAL_JOIN_AMBIGUOUS"
    assert result.identities == ("ROW-1", "ROW-2")


def test_q16_non_overlapping_dates_resolve_unambiguously() -> None:
    assert (
        golden.resolve_as_of(golden.RATE_VALIDITY_ROWS, "RATE-AUDUSD", date(2026, 9, 5))
        == "ROW-1"
    )
    assert (
        golden.resolve_as_of(
            golden.RATE_VALIDITY_ROWS, "RATE-AUDUSD", date(2026, 9, 25)
        )
        == "ROW-2"
    )


def test_q16_half_open_upper_bound_excludes_the_earlier_row() -> None:
    # ROW-1's valid_to is 2026-09-20 (exclusive); ROW-2's valid_from is
    # 2026-09-15 (inclusive) -- at the boundary only ROW-2 is valid.
    assert (
        golden.resolve_as_of(
            golden.RATE_VALIDITY_ROWS, "RATE-AUDUSD", date(2026, 9, 20)
        )
        == "ROW-2"
    )


def test_q16_no_matching_validity_row_is_temporal_join_missing_not_ambiguous() -> None:
    # ROW-2's valid_to is 2026-09-30 (exclusive): zero matches at that date,
    # distinct from the overlap (>1 match) case above.
    result = golden.resolve_as_of(
        golden.RATE_VALIDITY_ROWS, "RATE-AUDUSD", date(2026, 9, 30)
    )
    assert isinstance(result, golden.Rejected)
    assert result.code == "TEMPORAL_JOIN_MISSING"
    assert result.identities == ()


# --------------------------------------------------------------------------
# Q17 -- unlike currencies are never one scalar
# --------------------------------------------------------------------------


def test_q17_converted_total_is_correct_and_not_the_raw_blend() -> None:
    total = golden.overdue_scalar_total(
        golden.CURRENCY_INVOICES, date(2026, 9, 10), "AUD", golden.CONVERSION_RATES
    )
    # AUD 40,000 + (USD 25,000 * 1.50) = 40,000 + 37,500 = 77,500.
    # INV-S is due-today at 2026-09-10 and excluded (due_date < as_of_date).
    assert total == 77_500

    raw_blend = golden.forbidden_cross_currency_raw_sum(golden.CURRENCY_INVOICES)
    assert (
        raw_blend == 164_000
    )  # meaningless: mixes AUD/USD/AUD units, and includes due-today INV-S
    assert total != raw_blend


def test_q17_due_today_excluded_across_currencies() -> None:
    due_today = next(
        inv for inv in golden.CURRENCY_INVOICES if inv.invoice_id == "INV-S"
    )
    assert due_today.due_date == date(2026, 9, 10)
    total = golden.overdue_scalar_total(
        (due_today,), date(2026, 9, 10), "AUD", golden.CONVERSION_RATES
    )
    assert total == 0


def test_q17_missing_conversion_rate_is_unavailable_not_zero() -> None:
    result = golden.overdue_scalar_total(
        golden.CURRENCY_INVOICES_MISSING_RATE,
        date(2026, 9, 10),
        "AUD",
        golden.CONVERSION_RATES,
    )
    assert isinstance(result, golden.Unavailable)


def test_q17_fractional_minor_unit_conversion_is_unavailable_not_rounded() -> None:
    # GBP 100 * 1.505 = 150.500 -- not an integral minor-unit value. No
    # rounding rule is invented; it fails closed instead.
    result = golden.overdue_scalar_total(
        golden.CURRENCY_INVOICES_FRACTIONAL_RESULT,
        date(2026, 9, 10),
        "AUD",
        golden.CONVERSION_RATES,
    )
    assert isinstance(result, golden.Unavailable)


# --------------------------------------------------------------------------
# Unnumbered adversarial case -- null required fields fail closed. Not a
# WP02 implementation of canonical Q19: canonical Q19 is not_executed and
# out of WP02's scope.
# --------------------------------------------------------------------------


def test_adversarial_complete_row_passes_validation() -> None:
    assert golden.validate_required_row_fields(golden.ROW_COMPLETE) is None


@pytest.mark.parametrize(
    "row",
    [
        golden.ROW_MISSING_BUSINESS_KEY,
        golden.ROW_MISSING_AMOUNT,
        golden.ROW_MISSING_CURRENCY,
        golden.ROW_MISSING_DUE_DATE,
    ],
)
def test_adversarial_null_required_field_fails_closed(row: Any) -> None:
    result = golden.validate_required_row_fields(row)
    assert isinstance(result, golden.Rejected)
    assert result.code == "NULL_REQUIRED_FIELD"


def test_adversarial_null_field_diagnostic_names_the_missing_field() -> None:
    assert (
        "amount_cents"
        in golden.validate_required_row_fields(golden.ROW_MISSING_AMOUNT).detail
    )
    assert (
        "currency"
        in golden.validate_required_row_fields(golden.ROW_MISSING_CURRENCY).detail
    )
    assert (
        "due_date"
        in golden.validate_required_row_fields(golden.ROW_MISSING_DUE_DATE).detail
    )
    assert (
        "invoice_id"
        in golden.validate_required_row_fields(golden.ROW_MISSING_BUSINESS_KEY).detail
    )


# --------------------------------------------------------------------------
# Unnumbered adversarial case -- duplicate source identity (distinct from
# Q15's duplicate identity mapping). Not a WP02 implementation of canonical
# Q20: canonical Q20 is not_executed and out of WP02's scope.
# --------------------------------------------------------------------------


def test_adversarial_duplicate_source_identity_fails_closed() -> None:
    result = golden.detect_duplicate_source_identity(golden.DUPLICATE_SOURCE_RECORDS)
    assert isinstance(result, golden.Rejected)
    assert result.code == "DUPLICATE_SOURCE_IDENTITY"
    assert result.identities == ("SRC-100",)


def test_adversarial_clean_batch_has_no_duplicate() -> None:
    assert golden.detect_duplicate_source_identity(golden.CLEAN_SOURCE_RECORDS) is None


def test_adversarial_duplicate_identity_is_distinct_from_q15_identity_mapping() -> None:
    # This case fires on source composite-identity repetition -- the same
    # (source_system, source_record_id) appearing twice in one ingestion
    # batch, before any downstream mapping is attempted. Q15 fires on a
    # different hazard: a declared-unique downstream mapping (customer_id)
    # backed by more than one master row, which it rejects on row count
    # alone -- even when the repeated rows' values agree (see
    # test_q15_identical_duplicate_rows_still_violate_uniqueness). The two
    # never collapse into one diagnostic.
    duplicate_result = golden.detect_duplicate_source_identity(
        golden.DUPLICATE_SOURCE_RECORDS
    )
    mapping_result = golden.customer_exposure_via_declared_unique_master(
        golden.CONTRADICTED_MASTER_ROWS, golden.CONTRADICTED_INVOICES, "CUST-9"
    )
    assert duplicate_result.code != mapping_result.code


# --------------------------------------------------------------------------
# Q18 -- source completeness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "completeness",
    [
        golden.Completeness.OUTAGE,
        golden.Completeness.INCOMPLETE,
        golden.Completeness.UNKNOWN,
    ],
)
def test_q18_non_complete_empty_scope_is_unavailable_never_zero(
    completeness: Any,
) -> None:
    result = golden.scoped_total((), completeness)
    assert isinstance(result, golden.Unavailable)


def test_q18_proved_complete_empty_scope_is_genuinely_zero() -> None:
    assert golden.scoped_total((), golden.Completeness.COMPLETE) == 0


def test_q18_proved_complete_non_empty_scope_sums_normally() -> None:
    assert golden.scoped_total((100, 200), golden.Completeness.COMPLETE) == 300


# --------------------------------------------------------------------------
# Exact-decimal money helper
# --------------------------------------------------------------------------


def test_exact_decimal_string_parses_to_whole_cents() -> None:
    assert golden.parse_exact_decimal_cents("600.00") == 60_000


def test_exact_decimal_string_rejects_fractional_cents() -> None:
    with pytest.raises(ValueError, match="whole number of cents"):
        golden.parse_exact_decimal_cents("600.005")
