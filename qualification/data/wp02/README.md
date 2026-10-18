# WP02 -- structured-data golden calculations

## Authority

Part of `PLAN-CORE-DATA-001 / WP02`. This directory holds an engine-independent,
exact-arithmetic fixture suite that freezes the expected structured-data
outcomes for a fixed set of join/aggregation hazards, before any engine, SQL
compiler or live source adapter is admitted.

Evidence type: **independent fixture**. Production qualification: `not_executed`.
Integration qualification: `not_executed`. These three facts are also asserted
as data in `golden.py` (`EVIDENCE_LABEL`, `PRODUCTION_QUALIFICATION_STATUS`,
`INTEGRATION_QUALIFICATION_STATUS`) so this documentation cannot drift from the
module without the test suite failing.

## Scope

- `golden.py` -- the fixtures and oracle functions, one section per case.
- `fixtures/` -- raw JSON evidence files and their integrity manifest (see
  below). Not generated at test time; checked in as committed bytes.
- No DuckDB, SQLGlot, PostgreSQL driver or new dependency.
- No SQL generation, engine execution or connector dispatch.
- Standard library only: `dataclasses`, `datetime`, `decimal`, `enum`,
  `hashlib`, `json`, `pathlib`.

## Cases

1. **Finance/Cashflow baseline** -- an AUD accounts-receivable overdue-balance
   oracle (`overdue_balance`, `average_overdue_amount`), Australia/Sydney
   calendar dates, over five immutable invoices (INV-A..E) and five event
   overlays. Named rule coverage:
   - INV-A: gross 110,000; payments of 30,000 and 20,000 on/before
     2026-09-12 leave 60,000 outstanding at that date; a further 60,000
     payment on 2026-09-15 clears it to 0 by 2026-09-19.
   - INV-B: gross 55,000, due 2026-09-05 (the authoritative due date); a
     5,000 credit on/before 2026-09-12 leaves 50,000 outstanding, unchanged
     at both as-of dates.
   - INV-C: due exactly 2026-09-12 (due-today is excluded, `due_date <
     as_of_date` not `<=`) and paid in full (22,000) on 2026-09-13, so it
     contributes 0 at both as-of dates.
   - INV-D: gross 40,000, status Void; contributes 0 regardless of due date.
   - INV-E: gross 10,000, status Disputed; still included (the named sample
     rule for disputed items).
   - Two frozen totals: 120,000 cents (A$1,200.00) as of 2026-09-12
     (INV-A + INV-B + INV-E), 60,000 cents (A$600.00) as of 2026-09-19
     (INV-B + INV-E) -- a 50% reduction.
   - An event of any kind other than `payment` or `credit`, and an
     over-applied event total that would drive an invoice's residual balance
     negative, each fail closed (`Unavailable`) rather than silently
     skewing a total.
   - `average_overdue_amount` averages only invoices with a positive
     outstanding balance: an overdue-by-due-date invoice that is already
     fully settled (residual 0, e.g. INV-A and INV-C as of 2026-09-19) does
     not count toward the denominator. It also fails closed (`Unavailable`)
     the moment any overdue-by-status invoice in scope has a null required
     `due_date`, rather than silently excluding that invoice and averaging
     the rest.
2. **Research fanout** (Q14, Q29, Q30) -- two customers, each with invoices
   and three active projects, exercising a safe unique-customer relation
   against two forbidden repairs: a naive cross-join fanout, and
   `SUM(DISTINCT amount)`. CUST-1's two invoices (INV-J, INV-K) are frozen as
   AUD and overdue as of `FANOUT_AS_OF_DATE`. CUST-2 holds two distinct
   invoices of the same 50,000 amount: the correct total is 100,000, the
   naive cross-join fanout total is 300,000, and `SUM(DISTINCT amount)`
   wrongly collapses this to 50,000. Q14's safe path,
   `q14_safe_customer_exposure_via_active_project_semijoin`, semijoins the
   unique-customer exposure relation against a unique active-customer
   qualifier relation derived from active projects before returning
   exposure; for CUST-1 the accepted total is 120,000 cents. Every
   fanout/join helper ignores an inactive project entirely -- it
   contributes no row, and is never joined to. `safe_customer_exposure`
   fails closed (`Unavailable`) for a customer with no invoice link at all,
   never a silent zero. Q30 additionally covers re-summing a customer-grain
   value across the project rows it was preaggregated then rejoined to:
   `rejoin_and_sum` propagates an `Unavailable` input unchanged, rejects with
   `GRAIN_VIOLATION` only where a customer-grain value repeats across more
   than one project row for the same customer, and returns the exact sum for
   a non-fanout control (at most one row per customer). The rejoin-to-project
   rows case (three active projects per customer) stays rejected.
3. **Q15** -- a declared-unique customer key whose row count is anything
   other than exactly one: `KEY_CONSTRAINT_FAILED`, naming the contradicting
   rows' own stable identities (`CustomerMasterRow.row_id`), never a silent
   total. This fires even when the duplicate rows carry identical values --
   the row count itself is the violation, not merely a values disagreement.
4. **Q16** -- two overlapping half-open validity windows for the same
   entity: `TEMPORAL_JOIN_AMBIGUOUS` naming the conflicting row identities.
   Zero matching validity rows is a distinct diagnostic,
   `TEMPORAL_JOIN_MISSING`, not the same code as an overlap.
5. **Q17** -- AUD/USD/EUR/GBP invoices: conversion before summing (never a
   raw cross-currency blend), a missing required rate is `Unavailable`, not
   zero, and no rounding rule is invented: when `amount_minor` times an exact
   `Decimal` rate is not itself an integral minor-unit value, the result is
   `Unavailable`.
6. **Unnumbered adversarial case** -- null required fields (amount, currency,
   due date, business key) each fail closed with `NULL_REQUIRED_FIELD`,
   naming the missing field. This is not a WP02 implementation of canonical
   Q19: canonical Q19 is `not_executed` and out of WP02's scope.
7. **Unnumbered adversarial case** -- a duplicate `(source_system,
   source_record_id)` within one ingestion batch fails closed with
   `DUPLICATE_SOURCE_IDENTITY`, even when the repeated payloads agree.
   Distinct from Q15's duplicate *identity mapping* (a declared-unique
   downstream key contradicted by conflicting master-row values): this case
   is a duplicate at the source, before any mapping is attempted. This is
   not a WP02 implementation of canonical Q20: canonical Q20 is
   `not_executed` and out of WP02's scope.
8. **Q18** -- a `Completeness` scope (`complete` / `outage` / `incomplete` /
   `unknown`, standing in for an unavailable source and an unknown/incomplete
   state): only a proved-complete empty scope may be a genuine zero; every
   other empty scope is `Unavailable`. This is the empty-vs-unavailable
   distinction the rest of the module also preserves (`Unavailable` is never
   coerced to `0`).

Every fail-closed path returns one of two sentinels defined in `golden.py`:
`Unavailable(reason)` for values that are not yet known, and
`Rejected(code, detail, identities)` for a plan, declaration or row that must
be refused outright (`KEY_CONSTRAINT_FAILED`, `TEMPORAL_JOIN_AMBIGUOUS`,
`TEMPORAL_JOIN_MISSING`, `GRAIN_VIOLATION`, `NULL_REQUIRED_FIELD`,
`DUPLICATE_SOURCE_IDENTITY`). Neither sentinel is ever coerced to `0`.

## Determinism and raw-artifact evidence

Every fixture is a frozen dataclass or a tuple of them; no case mutates
another case's inputs. There is no producer-defined canonical JSON encoding
and no canonical digest anywhere in this directory. Instead, the cashflow
case and the fanout case are each checked in as a raw JSON file under
`fixtures/` (`cashflow.json`, `fanout.json`) -- hand-authored, not generated
at test time. `fixtures/SHA256SUMS` is a checked-in manifest, in the standard
`sha256sum` format, of those files' exact committed bytes; it can be verified
independently of this test suite with `sha256sum -c fixtures/SHA256SUMS` (or
`shasum -a 256 -c`) from this directory. The test suite recomputes each
file's SHA-256 from disk on every run and compares it against the manifest --
a raw-artifact integrity check, not a re-encoding.

Separately, `golden.py` exposes `_local_noncanonical_snapshot` (explicitly
named non-canonical) and its callers `_cashflow_raw_snapshot` /
`_fanout_raw_snapshot`, which convert the *live* Python fixture data to plain
JSON-safe values so it can be compared by `==` against the parsed content of
the checked-in JSON file. That drift check catches a fixture number edited in
one place and not the other; it plays no part in the hash check above.

## Money

All money is an integer count of minor units (cents). Where a caller's amount
arrives as a decimal string instead, `parse_exact_decimal_cents` converts it
exactly and fails closed (`ValueError`) rather than silently rounding a
fractional cent. There is no float anywhere in this module.
