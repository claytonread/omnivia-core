"""WP02 structured-data golden-calculation fixtures and oracles.

Engine-independent, exact-arithmetic evidence that freezes the expected
structured-data outcomes for a fixed set of join/aggregation hazards, before
any engine, SQL compiler or live source adapter is admitted. Every fixture
below is immutable (frozen dataclasses, tuples) or an explicit event overlay;
no case mutates another case's inputs. Money is always an integer count of
minor units (cents) or, where division is unavoidable, an exact
``decimal.Decimal``. There is no float anywhere in this module.

This module is **independent fixture** evidence only: it does not execute
against any engine, generate SQL, or dispatch to a connector, and it is not
itself the public contract or acceptance authority. Production and
integration qualification of the same scenarios is explicitly not attempted
here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Evidence labeling (documentation requirement, enforced as data rather than
# only as prose so a drifting doc cannot silently misstate it).
# --------------------------------------------------------------------------

EVIDENCE_LABEL = "independent fixture"
PRODUCTION_QUALIFICATION_STATUS = "not_executed"
INTEGRATION_QUALIFICATION_STATUS = "not_executed"


# --------------------------------------------------------------------------
# Shared result sentinels. An oracle returns one of: an exact int (minor
# units), an exact Decimal, or one of these two -- never a silent guess.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Unavailable:
    """A value that is not yet known -- never coerced to numeric zero."""

    reason: str


@dataclass(frozen=True)
class Rejected:
    """A plan or declaration that must fail closed, with a named diagnostic."""

    code: str
    detail: str
    identities: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Raw-fixture-file evidence. There is no producer-defined canonical encoding
# and no canonical digest: the checked-in files under ``fixtures/`` are the
# raw artifact, and their *exact committed bytes* are what is hashed. The
# manifest (``fixtures/SHA256SUMS``, standard ``sha256sum`` format) records
# those hashes; the test suite recomputes them from disk on every run.
# --------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_fixture_bytes(filename: str) -> bytes:
    """Exact raw bytes of a checked-in fixture file under ``fixtures/``."""
    return (FIXTURES_DIR / filename).read_bytes()


def read_fixture_manifest() -> dict[str, str]:
    """Parse ``fixtures/SHA256SUMS`` (``<hex>␠␠<filename>`` per line) into a dict."""
    manifest: dict[str, str] = {}
    for line in (FIXTURES_DIR / "SHA256SUMS").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, filename = line.partition("  ")
        manifest[filename] = digest
    return manifest


def _local_noncanonical_snapshot(value: Any) -> Any:
    """Convert a live fixture snapshot to plain JSON-safe values (dates to
    ISO strings) for drift-comparison against a checked-in raw fixture file.

    This is a repo-local convenience, not a canonical encoding: it exists
    only so ``json.loads`` of the checked-in file can be compared by ``==``
    to the live Python fixture data, never to derive or stand in for the
    raw-artifact hash above.
    """
    return json.loads(json.dumps(value, default=lambda v: v.isoformat()))


# ==========================================================================
# Case 1 -- Finance/Cashflow baseline
#
# Immutable invoice and event fixtures for an AUD accounts-receivable
# overdue-balance oracle. Named rule coverage:
#   - due-today excluded (due_date < as_of_date, not <=)
#   - void excluded regardless of due date
#   - disputed remains included (named sample rule)
#   - payments and credits apply through the as-of date
# ==========================================================================


@dataclass(frozen=True)
class CashflowInvoice:
    invoice_id: str
    amount_cents: int
    due_date: date | None
    status: str  # "open" | "disputed" | "void"


@dataclass(frozen=True)
class CashEvent:
    invoice_id: str
    kind: str  # "payment" | "credit"
    amount_cents: int
    event_date: date


CASHFLOW_CURRENCY = "AUD"
CASHFLOW_TIMEZONE = "Australia/Sydney"

# Normative baseline: 120,000 cents (A$1,200.00) overdue as of 2026-09-12
# (Australia/Sydney), 60,000 cents (A$600.00) as of 2026-09-19 -- a 50%
# reduction, from INV-B and INV-E alone.
CASHFLOW_INVOICES: tuple[CashflowInvoice, ...] = (
    CashflowInvoice("INV-A", 110_000, date(2026, 9, 1), "open"),
    # Authoritative due date is 2026-09-05, not 2026-09-01.
    CashflowInvoice("INV-B", 55_000, date(2026, 9, 5), "open"),
    CashflowInvoice("INV-C", 22_000, date(2026, 9, 12), "open"),
    CashflowInvoice("INV-D", 40_000, date(2026, 9, 1), "void"),
    CashflowInvoice("INV-E", 10_000, date(2026, 9, 1), "disputed"),
)

CASHFLOW_EVENTS: tuple[CashEvent, ...] = (
    # INV-A: two payments on/before 2026-09-12 (leaving 60,000 outstanding),
    # then a third payment on 2026-09-15 that clears it before 09-19.
    CashEvent("INV-A", "payment", 30_000, date(2026, 9, 3)),
    CashEvent("INV-A", "payment", 20_000, date(2026, 9, 7)),
    CashEvent("INV-A", "payment", 60_000, date(2026, 9, 15)),
    # INV-B: a partial credit on/before 2026-09-12; 50,000 remains at both dates.
    CashEvent("INV-B", "credit", 5_000, date(2026, 9, 2)),
    # INV-C is due exactly 2026-09-12 (excluded that day) and is paid in full
    # the next day, so it contributes 0 at both named as-of dates.
    CashEvent("INV-C", "payment", 22_000, date(2026, 9, 13)),
)


_SUPPORTED_CASH_EVENT_KINDS = frozenset({"payment", "credit"})


def _invoice_residual_balance(
    invoice: CashflowInvoice, events: tuple[CashEvent, ...], as_of_date: date
) -> int | Unavailable:
    """Outstanding balance for one invoice as of ``as_of_date``.

    Fails closed on an unsupported event kind or an over-applied (negative)
    residual rather than letting either silently skew a total.
    """
    applied = 0
    for event in events:
        if event.invoice_id != invoice.invoice_id or event.event_date > as_of_date:
            continue
        if event.kind not in _SUPPORTED_CASH_EVENT_KINDS:
            return Unavailable(
                f"invoice {invoice.invoice_id!r} has an unsupported event kind {event.kind!r}"
            )
        applied += event.amount_cents
    residual = invoice.amount_cents - applied
    if residual < 0:
        return Unavailable(
            f"invoice {invoice.invoice_id!r} has over-applied events "
            f"({applied} applied against {invoice.amount_cents} gross)"
        )
    return residual


def overdue_balance(
    invoices: tuple[CashflowInvoice, ...],
    events: tuple[CashEvent, ...],
    as_of_date: date,
) -> int | Unavailable:
    """Sum of remaining balance for invoices strictly overdue as of ``as_of_date``.

    Fails closed (``Unavailable``) on a null required ``due_date``, an
    unsupported event kind or an over-applied residual rather than guessing.
    """
    total = 0
    for invoice in invoices:
        if invoice.status == "void":
            continue
        if invoice.due_date is None:
            return Unavailable(f"invoice {invoice.invoice_id!r} has no due_date")
        if not (invoice.due_date < as_of_date):
            continue
        residual = _invoice_residual_balance(invoice, events, as_of_date)
        if isinstance(residual, Unavailable):
            return residual
        total += residual
    return total


def average_overdue_amount(
    invoices: tuple[CashflowInvoice, ...],
    events: tuple[CashEvent, ...],
    as_of_date: date,
) -> Decimal | Unavailable:
    """Mean overdue balance across invoices with a positive outstanding balance.

    A fully settled overdue invoice (residual 0) does not count toward the
    denominator -- it carries no exposure left to average in. A zero
    denominator (no invoice with a positive overdue balance) fails closed.
    Fails closed on a null required ``due_date`` rather than silently
    excluding that invoice from the average.
    """
    positive_balances: list[int] = []
    for invoice in invoices:
        if invoice.status == "void":
            continue
        if invoice.due_date is None:
            return Unavailable(f"invoice {invoice.invoice_id!r} has no due_date")
        if not (invoice.due_date < as_of_date):
            continue
        residual = _invoice_residual_balance(invoice, events, as_of_date)
        if isinstance(residual, Unavailable):
            return residual
        if residual > 0:
            positive_balances.append(residual)
    if not positive_balances:
        return Unavailable(
            "zero denominator: no invoice has a positive overdue balance as of this date"
        )
    return Decimal(sum(positive_balances)) / Decimal(len(positive_balances))


# A standalone fixture for the null-required-value diagnostic, kept out of
# the named A-G set so it cannot perturb the two frozen headline totals.
NULL_DUE_DATE_INVOICES: tuple[CashflowInvoice, ...] = (
    CashflowInvoice("INV-Z", 10_000, None, "open"),
)

# A mix of one valid overdue invoice and one null-due-date invoice, for
# ``average_overdue_amount``'s fail-closed diagnostic: the valid invoice's
# positive balance must not be silently averaged in isolation once the null
# due date is discovered.
MIXED_VALID_AND_NULL_DUE_DATE_INVOICES: tuple[CashflowInvoice, ...] = (
    CashflowInvoice("INV-Z-VALID", 5_000, date(2026, 9, 1), "open"),
) + NULL_DUE_DATE_INVOICES

# Standalone fixtures for the unsupported-event-kind and over-applied-residual
# diagnostics, kept out of the named A-E set so they cannot perturb the two
# frozen headline totals.
UNSUPPORTED_EVENT_KIND_INVOICES: tuple[CashflowInvoice, ...] = (
    CashflowInvoice("INV-Y1", 10_000, date(2026, 9, 1), "open"),
)
UNSUPPORTED_EVENT_KIND_EVENTS: tuple[CashEvent, ...] = (
    CashEvent("INV-Y1", "writeoff", 1_000, date(2026, 9, 2)),
)

OVER_APPLIED_RESIDUAL_INVOICES: tuple[CashflowInvoice, ...] = (
    CashflowInvoice("INV-Y2", 10_000, date(2026, 9, 1), "open"),
)
OVER_APPLIED_RESIDUAL_EVENTS: tuple[CashEvent, ...] = (
    CashEvent("INV-Y2", "payment", 15_000, date(2026, 9, 2)),
)


def _cashflow_raw_snapshot() -> dict[str, Any]:
    """Live fixture data as plain, JSON-safe values (see
    ``_local_noncanonical_snapshot``) for drift-comparison against the
    checked-in ``fixtures/cashflow.json``. Not itself the raw artifact."""
    return _local_noncanonical_snapshot(
        {
            "currency": CASHFLOW_CURRENCY,
            "timezone": CASHFLOW_TIMEZONE,
            "invoices": [
                {
                    "invoice_id": inv.invoice_id,
                    "amount_cents": inv.amount_cents,
                    "due_date": inv.due_date,
                    "status": inv.status,
                }
                for inv in CASHFLOW_INVOICES
            ],
            "events": [
                {
                    "invoice_id": ev.invoice_id,
                    "kind": ev.kind,
                    "amount_cents": ev.amount_cents,
                    "event_date": ev.event_date,
                }
                for ev in CASHFLOW_EVENTS
            ],
        }
    )


# ==========================================================================
# Case 2 -- Research fanout (customer exposure vs. project fanout)
#
# Covers Q14, Q29 and Q30: a safe unique-customer relation, an accepted
# broadcast join to project rows, and two forbidden repairs -- naive
# cross-join fanout, and SUM(DISTINCT amount). Q14's safe path is a semijoin
# of that relation against a unique active-customer qualifier relation
# derived from active projects; every fanout/join helper below ignores an
# inactive project entirely -- it contributes no row and is never joined to.
# ==========================================================================


@dataclass(frozen=True)
class FanoutInvoice:
    invoice_id: str
    customer_id: str
    amount_cents: int
    currency: str
    due_date: date


@dataclass(frozen=True)
class Project:
    project_id: str
    customer_id: str
    active: bool = True


@dataclass(frozen=True)
class ProjectExposureRow:
    project_id: str
    customer_id: str
    exposure_cents: int


# Q14 / Q30: two unequal overdue invoices, three active projects. Frozen as
# AUD and overdue as of FANOUT_AS_OF_DATE (both due 2026-09-01).
CUST_1 = "CUST-1"
FANOUT_INVOICES_CUST_1: tuple[FanoutInvoice, ...] = (
    FanoutInvoice("INV-J", CUST_1, 50_000, "AUD", date(2026, 9, 1)),
    FanoutInvoice("INV-K", CUST_1, 70_000, "AUD", date(2026, 9, 1)),
)

# Q29: two DISTINCT invoices sharing the same amount -- the SUM(DISTINCT) trap.
CUST_2 = "CUST-2"
FANOUT_INVOICES_CUST_2: tuple[FanoutInvoice, ...] = (
    FanoutInvoice("INV-L", CUST_2, 50_000, "AUD", date(2026, 9, 1)),
    FanoutInvoice("INV-M", CUST_2, 50_000, "AUD", date(2026, 9, 1)),
)

# Reference as-of date against which the fanout case's invoices are overdue.
FANOUT_AS_OF_DATE = date(2026, 9, 20)

# A project referencing a customer absent from any invoice/exposure fixture,
# for the missing-link diagnostic.
MISSING_LINK_CUSTOMER = "CUST-404"


def three_active_projects_for(customer_id: str) -> tuple[Project, ...]:
    return tuple(Project(f"PROJ-{customer_id}-{i}", customer_id) for i in (1, 2, 3))


# An inactive project for CUST_1, for the inactive-project control: it must
# contribute no row to any fanout/join helper below.
INACTIVE_PROJECT_CUST_1 = Project(f"PROJ-{CUST_1}-INACTIVE", CUST_1, active=False)

ALL_FANOUT_INVOICES: tuple[FanoutInvoice, ...] = (
    FANOUT_INVOICES_CUST_1 + FANOUT_INVOICES_CUST_2
)
ALL_PROJECTS: tuple[Project, ...] = (
    three_active_projects_for(CUST_1)
    + (INACTIVE_PROJECT_CUST_1,)
    + three_active_projects_for(CUST_2)
    + (Project("PROJ-404", MISSING_LINK_CUSTOMER),)
)


def unique_customer_relation(invoices: tuple[FanoutInvoice, ...]) -> dict[str, int]:
    """Preaggregate to customer grain: exactly one row per customer_id."""
    exposure: dict[str, int] = {}
    for invoice in invoices:
        exposure[invoice.customer_id] = (
            exposure.get(invoice.customer_id, 0) + invoice.amount_cents
        )
    return exposure


def safe_customer_exposure(
    invoices: tuple[FanoutInvoice, ...], customer_id: str
) -> int | Unavailable:
    """Safe exposure for one customer: sum at customer grain, no project join.

    Fails closed (``Unavailable``) when the customer has no invoice at all in
    scope, rather than coercing an absent link to a numeric zero.
    """
    relation = unique_customer_relation(invoices)
    if customer_id not in relation:
        return Unavailable(f"no exposure link for customer {customer_id!r}")
    return relation[customer_id]


def active_customer_qualifier(projects: tuple[Project, ...]) -> frozenset[str]:
    """Unique active-customer qualifier relation: the distinct customer_ids
    with at least one active project. An inactive project contributes no row."""
    return frozenset(project.customer_id for project in projects if project.active)


def q14_safe_customer_exposure_via_active_project_semijoin(
    invoices: tuple[FanoutInvoice, ...],
    projects: tuple[Project, ...],
    customer_id: str,
) -> int | Unavailable:
    """Q14's safe path: semijoin the unique-customer exposure relation against
    the active-customer qualifier relation before returning exposure.

    A customer with no active project is filtered out here, by the semijoin,
    rather than being joined to a project row at all -- fails closed
    (``Unavailable``), never a silent zero.
    """
    if customer_id not in active_customer_qualifier(projects):
        return Unavailable(f"customer {customer_id!r} has no active project")
    return safe_customer_exposure(invoices, customer_id)


def join_exposure_to_projects(
    exposure_by_customer: dict[str, int], projects: tuple[Project, ...]
) -> list[ProjectExposureRow | Unavailable]:
    """Safe row projection only -- broadcast-join the unique-customer relation
    to project rows for passthrough/display. Must never be re-summed.

    Each row carries the *same* customer-grain exposure value; summing across
    rows for one customer is the fanout trap this join must not invite (see
    ``rejoin_and_sum``). A project referencing an unknown customer fails
    closed instead of a silent zero. An inactive project is ignored entirely
    -- it contributes no row, not even an ``Unavailable`` one.
    """
    rows: list[ProjectExposureRow | Unavailable] = []
    for project in projects:
        if not project.active:
            continue
        if project.customer_id not in exposure_by_customer:
            rows.append(
                Unavailable(
                    f"missing exposure link for customer {project.customer_id!r} "
                    f"referenced by project {project.project_id!r}"
                )
            )
            continue
        rows.append(
            ProjectExposureRow(
                project.project_id,
                project.customer_id,
                exposure_by_customer[project.customer_id],
            )
        )
    return rows


def rejoin_and_sum(
    rows: list[ProjectExposureRow | Unavailable],
) -> int | Rejected | Unavailable:
    """Re-summing exposure across project rows it was broadcast-joined to.

    Propagates a missing-link ``Unavailable`` row unchanged. Rejects with
    ``GRAIN_VIOLATION`` only where a customer-grain value repeats across more
    than one project row for the same customer -- the fanout hazard this
    rejoin invites. A non-fanout control (at most one row per customer) has
    nothing to double-count, so it returns the exact sum.
    """
    for row in rows:
        if isinstance(row, Unavailable):
            return row
    rows_by_customer: dict[str, list[str]] = {}
    for row in rows:
        rows_by_customer.setdefault(row.customer_id, []).append(row.project_id)
    violating_project_ids = tuple(
        sorted(
            project_id
            for project_ids in rows_by_customer.values()
            if len(project_ids) > 1
            for project_id in project_ids
        )
    )
    if violating_project_ids:
        return Rejected(
            code="GRAIN_VIOLATION",
            detail=(
                f"summing a customer-grain measure across {len(violating_project_ids)} project "
                "row(s) it was preaggregated then rejoined to double-counts by that factor"
            ),
            identities=violating_project_ids,
        )
    return sum(
        row.exposure_cents for row in rows if isinstance(row, ProjectExposureRow)
    )


def naive_invoice_project_fanout(
    invoices: tuple[FanoutInvoice, ...], projects: tuple[Project, ...], customer_id: str
) -> Rejected:
    """Forbidden plan: cross-join invoice rows directly to project rows and
    sum amount over the fanned-out result, instead of preaggregating first.
    An inactive project is ignored -- it is never a cross-join partner."""
    matching_invoices = tuple(i for i in invoices if i.customer_id == customer_id)
    matching_projects = tuple(
        p for p in projects if p.customer_id == customer_id and p.active
    )
    return Rejected(
        code="GRAIN_VIOLATION",
        detail=(
            f"cross join of {len(matching_invoices)} invoice row(s) to "
            f"{len(matching_projects)} project row(s) for {customer_id!r} produces "
            f"{len(matching_invoices) * len(matching_projects)} rows; summing amount "
            f"over them multiplies each invoice by {len(matching_projects)}"
        ),
        identities=tuple(sorted(i.invoice_id for i in matching_invoices)),
    )


def forbidden_naive_fanout_total(
    invoices: tuple[FanoutInvoice, ...], projects: tuple[Project, ...], customer_id: str
) -> int:
    """The wrong number a naive cross-join-then-sum would compute. Exists only
    so the test suite can name and reject it -- never call this as an oracle.
    An inactive project is ignored -- it is never a cross-join partner."""
    matching_invoices = tuple(i for i in invoices if i.customer_id == customer_id)
    matching_projects = tuple(
        p for p in projects if p.customer_id == customer_id and p.active
    )
    return sum(i.amount_cents for i in matching_invoices) * len(matching_projects)


def sum_distinct_forbidden(
    invoices: tuple[FanoutInvoice, ...], customer_id: str
) -> int:
    """SUM(DISTINCT amount) -- a forbidden repair that undercounts whenever
    two different rows happen to share the same amount."""
    matching = tuple(i for i in invoices if i.customer_id == customer_id)
    return sum({i.amount_cents for i in matching})


def _fanout_raw_snapshot() -> dict[str, Any]:
    """Live fixture data for drift-comparison against ``fixtures/fanout.json``.
    See the note on ``_cashflow_raw_snapshot``: not the raw artifact itself."""
    return _local_noncanonical_snapshot(
        {
            "invoices": [
                {
                    "invoice_id": i.invoice_id,
                    "customer_id": i.customer_id,
                    "amount_cents": i.amount_cents,
                    "currency": i.currency,
                    "due_date": i.due_date,
                }
                for i in ALL_FANOUT_INVOICES
            ],
            "projects": [
                {
                    "project_id": p.project_id,
                    "customer_id": p.customer_id,
                    "active": p.active,
                }
                for p in ALL_PROJECTS
            ],
        }
    )


# ==========================================================================
# Q15 -- contradicted unique-key declaration
# ==========================================================================


@dataclass(frozen=True)
class CustomerMasterRow:
    customer_id: str
    legal_name: str
    row_id: str


@dataclass(frozen=True)
class KeyedInvoice:
    invoice_id: str
    customer_id: str
    amount_cents: int


# CUST-9 is declared unique on customer_id but has two conflicting names.
CONTRADICTED_MASTER_ROWS: tuple[CustomerMasterRow, ...] = (
    CustomerMasterRow("CUST-9", "Acme Pty Ltd", "ROW-9A"),
    CustomerMasterRow("CUST-9", "Acme Holdings Pty Ltd", "ROW-9B"),
)
CONTRADICTED_INVOICES: tuple[KeyedInvoice, ...] = (
    KeyedInvoice("INV-X", "CUST-9", 40_000),
    KeyedInvoice("INV-Y", "CUST-9", 15_000),
)

# CUST-12 is declared unique on customer_id but has two master rows that
# happen to carry identical values -- the row *count* still violates
# uniqueness even though there is nothing for the values to disagree on.
DUPLICATE_IDENTICAL_MASTER_ROWS: tuple[CustomerMasterRow, ...] = (
    CustomerMasterRow("CUST-12", "Gamma Pty Ltd", "ROW-12A"),
    CustomerMasterRow("CUST-12", "Gamma Pty Ltd", "ROW-12B"),
)
DUPLICATE_IDENTICAL_INVOICES: tuple[KeyedInvoice, ...] = (
    KeyedInvoice("INV-Z2", "CUST-12", 5_000),
)

# CUST-11 is a clean, uncontradicted control case: the "proven alternate plan"
# that must still return a real number.
CLEAN_MASTER_ROWS: tuple[CustomerMasterRow, ...] = (
    CustomerMasterRow("CUST-11", "Beta Co", "ROW-11"),
)
CLEAN_INVOICES: tuple[KeyedInvoice, ...] = (KeyedInvoice("INV-N", "CUST-11", 22_000),)


def customer_exposure_via_declared_unique_master(
    master_rows: tuple[CustomerMasterRow, ...],
    invoices: tuple[KeyedInvoice, ...],
    customer_id: str,
) -> int | Rejected:
    """Fails closed whenever the declared-unique key has any row count other
    than exactly one -- including two rows with identical values, since the
    count itself is the violation, not merely a values disagreement."""
    matches = tuple(r for r in master_rows if r.customer_id == customer_id)
    if not matches:
        return Rejected(
            "KEY_CONSTRAINT_FAILED", f"no master row for {customer_id!r}", ()
        )
    if len(matches) != 1:
        return Rejected(
            "KEY_CONSTRAINT_FAILED",
            f"declared-unique customer {customer_id!r} has {len(matches)} matching master rows",
            tuple(sorted(r.row_id for r in matches)),
        )
    return sum(i.amount_cents for i in invoices if i.customer_id == customer_id)


# ==========================================================================
# Q16 -- overlapping half-open validity windows
# ==========================================================================


@dataclass(frozen=True)
class ValidityRow:
    entity_id: str
    row_id: str
    valid_from: date
    valid_to: date  # half-open: valid_from <= t < valid_to


RATE_VALIDITY_ROWS: tuple[ValidityRow, ...] = (
    ValidityRow("RATE-AUDUSD", "ROW-1", date(2026, 9, 1), date(2026, 9, 20)),
    ValidityRow("RATE-AUDUSD", "ROW-2", date(2026, 9, 15), date(2026, 9, 30)),
)


def resolve_as_of(
    rows: tuple[ValidityRow, ...], entity_id: str, as_of_date: date
) -> str | Rejected:
    matches = tuple(
        r
        for r in rows
        if r.entity_id == entity_id and r.valid_from <= as_of_date < r.valid_to
    )
    if len(matches) > 1:
        return Rejected(
            "TEMPORAL_JOIN_AMBIGUOUS",
            f"{len(matches)} overlapping validity rows for {entity_id!r} at {as_of_date.isoformat()}",
            tuple(sorted(r.row_id for r in matches)),
        )
    if not matches:
        return Rejected(
            "TEMPORAL_JOIN_MISSING",
            f"no validity row for {entity_id!r} at {as_of_date.isoformat()}",
            (),
        )
    return matches[0].row_id


# ==========================================================================
# Q17 -- unlike currencies are never one scalar
# ==========================================================================


@dataclass(frozen=True)
class CurrencyInvoice:
    invoice_id: str
    currency: str
    amount_minor: int
    due_date: date


CURRENCY_INVOICES: tuple[CurrencyInvoice, ...] = (
    CurrencyInvoice("INV-P", "AUD", 40_000, date(2026, 9, 1)),
    CurrencyInvoice("INV-Q", "USD", 25_000, date(2026, 9, 1)),
    # due-today at the test's as-of date: excluded, same due_date < as_of rule.
    CurrencyInvoice("INV-S", "AUD", 99_000, date(2026, 9, 10)),
)

# A currency this fixture deliberately has no rate for.
CURRENCY_INVOICES_MISSING_RATE: tuple[CurrencyInvoice, ...] = (
    CurrencyInvoice("INV-R", "EUR", 10_000, date(2026, 9, 1)),
)

# A currency whose exact rate produces a fractional (sub-minor-unit) result.
CURRENCY_INVOICES_FRACTIONAL_RESULT: tuple[CurrencyInvoice, ...] = (
    CurrencyInvoice("INV-T", "GBP", 100, date(2026, 9, 1)),
)

CONVERSION_RATES: dict[tuple[str, str], Decimal] = {
    ("USD", "AUD"): Decimal("1.50"),
    ("GBP", "AUD"): Decimal("1.505"),
}


def overdue_scalar_total(
    invoices: tuple[CurrencyInvoice, ...],
    as_of_date: date,
    target_currency: str,
    rates: dict[tuple[str, str], Decimal],
) -> int | Unavailable:
    """Convert every overdue invoice into one currency before summing -- unlike
    currencies are never blended into a raw scalar.

    Invents no rounding rule: when the exact-Decimal conversion is not itself
    an integral minor-unit value, fails closed (``Unavailable``) rather than
    rounding to one.
    """
    total_minor = 0
    for invoice in invoices:
        if not (invoice.due_date < as_of_date):
            continue
        if invoice.currency == target_currency:
            total_minor += invoice.amount_minor
            continue
        rate = rates.get((invoice.currency, target_currency))
        if rate is None:
            return Unavailable(
                f"missing conversion rate {invoice.currency}->{target_currency} "
                f"for {invoice.invoice_id!r}"
            )
        converted = Decimal(invoice.amount_minor) * rate
        if converted != converted.to_integral_value():
            return Unavailable(
                f"{invoice.currency}->{target_currency} conversion for {invoice.invoice_id!r} "
                "is not an exact whole minor-unit amount"
            )
        total_minor += int(converted)
    return total_minor


def forbidden_cross_currency_raw_sum(invoices: tuple[CurrencyInvoice, ...]) -> int:
    """The meaningless number you get by summing minor units across currencies
    without converting first. Exists only to be named and rejected."""
    return sum(i.amount_minor for i in invoices)


# ==========================================================================
# Unnumbered adversarial case -- null required fields fail closed (amount,
# currency, due date, business key), distinct from Q16's temporal ambiguity
# and Q17's missing rate: this is a row that should never have been admitted
# at all. Not a WP02 implementation of a canonical plan question: canonical
# Q19 is not_executed / out of WP02's scope.
# ==========================================================================


@dataclass(frozen=True)
class RawCashflowRow:
    invoice_id: str | None
    amount_cents: int | None
    currency: str | None
    due_date: date | None
    status: str = "open"


REQUIRED_ROW_FIELDS: tuple[str, ...] = (
    "invoice_id",
    "amount_cents",
    "currency",
    "due_date",
)

ROW_MISSING_BUSINESS_KEY = RawCashflowRow(None, 10_000, "AUD", date(2026, 9, 1))
ROW_MISSING_AMOUNT = RawCashflowRow("INV-NULL-AMOUNT", None, "AUD", date(2026, 9, 1))
ROW_MISSING_CURRENCY = RawCashflowRow("INV-NULL-CCY", 10_000, None, date(2026, 9, 1))
ROW_MISSING_DUE_DATE = RawCashflowRow("INV-NULL-DATE", 10_000, "AUD", None)
ROW_COMPLETE = RawCashflowRow("INV-COMPLETE", 10_000, "AUD", date(2026, 9, 1))


def validate_required_row_fields(row: RawCashflowRow) -> Rejected | None:
    """Fail closed on any null required field before a row is admitted.

    Checked in a fixed field order so the diagnostic always names the first
    missing field rather than an arbitrary one.
    """
    for field_name in REQUIRED_ROW_FIELDS:
        if getattr(row, field_name) is None:
            return Rejected(
                "NULL_REQUIRED_FIELD",
                f"required field {field_name!r} is null",
                (row.invoice_id or "<unknown business key>",),
            )
    return None


# ==========================================================================
# Unnumbered adversarial case -- duplicate source identity: the same
# (source_system, source_record_id) appearing twice in one ingestion batch,
# regardless of whether the payloads agree. Distinct from Q15's duplicate
# *identity mapping* (a declared-unique downstream key contradicted by
# conflicting master rows) -- this is a duplicate at the source, before any
# mapping is attempted. Not a WP02 implementation of a canonical plan
# question: canonical Q20 is not_executed / out of WP02's scope.
# ==========================================================================


@dataclass(frozen=True)
class SourceRecord:
    source_system: str
    source_record_id: str
    payload_amount_cents: int


DUPLICATE_SOURCE_RECORDS: tuple[SourceRecord, ...] = (
    SourceRecord("ERP-1", "SRC-100", 5_000),
    SourceRecord("ERP-1", "SRC-100", 5_000),
)
CLEAN_SOURCE_RECORDS: tuple[SourceRecord, ...] = (
    SourceRecord("ERP-1", "SRC-100", 5_000),
    SourceRecord("ERP-1", "SRC-101", 7_000),
)


def detect_duplicate_source_identity(
    records: tuple[SourceRecord, ...],
) -> Rejected | None:
    """Fail closed the moment one (source_system, source_record_id) repeats.

    Repetition itself is the hazard -- a re-delivered or re-ingested record
    risks double counting -- independent of whether the repeated payloads
    happen to agree.
    """
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.source_system, record.source_record_id)
        if key in seen:
            return Rejected(
                "DUPLICATE_SOURCE_IDENTITY",
                f"source {record.source_system!r} record {record.source_record_id!r} "
                "appears more than once in the same batch",
                (record.source_record_id,),
            )
        seen.add(key)
    return None


# ==========================================================================
# Q18 -- source completeness: proved-empty is zero, everything else is unavailable
# ==========================================================================


class Completeness(Enum):
    COMPLETE = "complete"
    OUTAGE = "outage"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


def scoped_total(
    rows: tuple[int, ...], completeness: Completeness
) -> int | Unavailable:
    if completeness is not Completeness.COMPLETE:
        return Unavailable(f"source scope is {completeness.value}, not proven complete")
    return sum(rows)


# ==========================================================================
# Money/decimal string helper (exact-decimal alternative to integer cents,
# used only where a caller's amount arrives as a string rather than an int).
# ==========================================================================


def parse_exact_decimal_cents(amount: str) -> int:
    """Parse a decimal-string amount (e.g. "600.00") into exact integer cents.

    Fails closed with ValueError (never silent rounding) if the string encodes
    a fraction of a cent.
    """
    value = Decimal(amount) * 100
    if value != value.to_integral_value():
        raise ValueError(f"{amount!r} does not represent a whole number of cents")
    return int(value)
