"""A9 layer 4: the accepted six-row, five-axis compatibility matrix."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from omnivia_core.agent_host.compatibility_conformance import (
    AXIS_NAMES,
    DEPRECATION_REMOVAL_VERSION,
    MATRIX,
    MATRIX_ROW_NAMES,
    NEGOTIATION_CALLER,
    NEGOTIATION_DEADLINE_MS,
    NEGOTIATION_PURPOSE,
    NEGOTIATION_WORKSPACE,
    CompatibilityReport,
    RowResult,
    evaluate,
    grade_row,
    run_compatibility_conformance,
)
from omnivia_core.agent_host.spi import (
    HOOK_COMPOSITIONS,
    Disposition,
    Hook,
    VersionAxes,
)
from omnivia_core.contracts.v1.generated import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
    UPGRADE_STATE_NONE,
)

REPORT = run_compatibility_conformance()
RESULTS = REPORT.results


def _result(name: str) -> RowResult:
    return next(item for item in RESULTS if item.row.name == name)


def _replace_compatibility(result: RowResult, **changes: object) -> RowResult:
    compatibility = replace(result.compatibility, **changes)
    version = replace(
        result.outcome.response.metadata.version,
        compatibility=compatibility,
    )
    metadata = replace(result.outcome.response.metadata, version=version)
    response = replace(result.outcome.response, metadata=metadata)
    return replace(result, outcome=replace(result.outcome, response=response))


def _mismatch_fields(result: RowResult) -> tuple[str, ...]:
    return tuple(item.field for item in grade_row(result))


def test_the_complete_matrix_passes_in_accepted_order() -> None:
    assert REPORT.passed, REPORT.detail
    assert REPORT.mismatches == ()
    assert REPORT.covered_every_row
    assert REPORT.reported_every_axis
    assert REPORT.graded_rows == MATRIX_ROW_NAMES
    assert len(RESULTS) == 6


def test_the_six_rows_fix_the_expected_decisions() -> None:
    observed = tuple(
        (
            item.row.name,
            item.outcome.disposition,
            item.outcome.compatibility_status,
            item.outcome.error_code,
            item.selected_spi_version,
        )
        for item in RESULTS
    )
    assert observed == (
        (
            "same-major-older-or-equal-minor",
            Disposition.NEGOTIATED,
            COMPATIBILITY_STATUS_COMPATIBLE,
            None,
            MATRIX[0].declared_spi_version,
        ),
        (
            "same-major-newer-minor",
            Disposition.NEGOTIATED,
            COMPATIBILITY_STATUS_COMPATIBLE,
            None,
            MATRIX[1].expected_selected_spi,
        ),
        (
            "deprecated-capability",
            Disposition.NEGOTIATED,
            COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
            None,
            MATRIX[2].declared_spi_version,
        ),
        (
            "different-major",
            Disposition.REFUSED,
            COMPATIBILITY_STATUS_INCOMPATIBLE,
            ERROR_CODE_INCOMPATIBLE_VERSION,
            None,
        ),
        (
            "required-capability-absent",
            Disposition.REFUSED,
            COMPATIBILITY_STATUS_INCOMPATIBLE,
            ERROR_CODE_CAPABILITY_NOT_GRANTED,
            None,
        ),
        (
            "workspace-below-minimum-format",
            Disposition.REFUSED,
            COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
            ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
            None,
        ),
    )


def test_every_row_makes_the_same_bounded_negotiation_call() -> None:
    for result in RESULTS:
        request = result.request
        assert request.caller == NEGOTIATION_CALLER
        assert request.workspace == NEGOTIATION_WORKSPACE
        assert request.purpose == NEGOTIATION_PURPOSE
        assert request.deadline_ms == NEGOTIATION_DEADLINE_MS
        assert request.turn_ordinal is None
        assert request.declared_spi_version == result.row.declared_spi_version
        assert request.required_capabilities == result.row.required_capabilities
        assert request.granted_capabilities == result.row.required_capabilities


def test_all_five_version_axes_are_reported_together_on_every_outcome() -> None:
    assert AXIS_NAMES == ("spi", "api", "server", "workspace_format", "client")
    for result in RESULTS:
        axes = result.outcome.version_axes
        assert isinstance(axes, VersionAxes)
        expected_spi = result.row.expected_selected_spi or result.profile_axes.spi
        assert (
            axes.spi,
            axes.api,
            axes.server,
            axes.workspace_format,
            axes.client,
        ) == (
            expected_spi,
            result.profile_axes.api,
            result.profile_axes.server,
            result.profile_axes.workspace_format,
            result.profile_axes.client,
        )


def test_deprecation_reports_its_removal_version() -> None:
    deprecation = _result("deprecated-capability").compatibility.deprecations
    assert len(deprecation) == 1
    assert deprecation[0].removal == DEPRECATION_REMOVAL_VERSION


def test_workspace_upgrade_is_a_refusal_without_a_migration() -> None:
    result = _result("workspace-below-minimum-format")
    assert result.compatibility.upgrade_state.value != UPGRADE_STATE_NONE
    assert result.workspaces_below_minimum_before == (NEGOTIATION_WORKSPACE,)
    assert (
        result.workspaces_below_minimum_after == result.workspaces_below_minimum_before
    )
    assert result.outcome.composed_operations == ()
    assert result.outcome.nested_envelopes == ()
    assert not result.negotiated


def test_all_rows_follow_the_frozen_negotiation_composition() -> None:
    for result in RESULTS:
        expected = HOOK_COMPOSITIONS[Hook.NEGOTIATE] if result.row.proceeds else ()
        assert result.outcome.composed_operations == expected
        assert (
            tuple(envelope.operation for envelope in result.outcome.nested_envelopes)
            == expected
        )


@pytest.mark.parametrize(
    "results",
    [
        RESULTS[:-1],
        (*RESULTS, RESULTS[-1]),
        tuple(reversed(RESULTS)),
        (
            replace(RESULTS[0], row=replace(RESULTS[0].row, name="unexpected-row")),
            *RESULTS[1:],
        ),
    ],
    ids=("missing", "duplicate", "reordered", "unexpected"),
)
def test_report_fails_closed_when_the_row_set_is_not_exact(
    results: tuple[RowResult, ...],
) -> None:
    report = evaluate(results)
    assert not report.passed
    assert not report.covered_every_row
    assert report.detail


def test_request_drift_is_detected_even_if_the_recorded_outcome_still_matches() -> None:
    result = RESULTS[0]
    mutated = replace(
        result,
        request=replace(
            result.request,
            declared_spi_version=MATRIX[1].declared_spi_version,
        ),
    )
    assert "request.declared_spi_version" in _mismatch_fields(mutated)


def test_decision_and_selected_version_mutations_are_detected() -> None:
    result = RESULTS[0]
    changed_decision = replace(
        result,
        outcome=replace(result.outcome, disposition=Disposition.ACCEPTED),
    )
    changed_selection = replace(result, selected_spi_version="1.9.9")
    assert "disposition" in _mismatch_fields(changed_decision)
    assert "selected_spi_version" in _mismatch_fields(changed_selection)


def test_missing_or_changed_version_axes_are_detected() -> None:
    result = RESULTS[0]
    missing = replace(result, outcome=replace(result.outcome, version_axes=None))
    axes = result.outcome.version_axes
    assert isinstance(axes, VersionAxes)
    changed = replace(
        result,
        outcome=replace(result.outcome, version_axes=replace(axes, server="9.9.9")),
    )
    assert "version_axes" in _mismatch_fields(missing)
    assert "version_axes.server" in _mismatch_fields(changed)


def test_envelope_status_and_deprecation_removal_mutations_are_detected() -> None:
    compatible = RESULTS[0]
    status_changed = _replace_compatibility(
        compatible,
        status=COMPATIBILITY_STATUS_INCOMPATIBLE,
    )
    deprecated = _result("deprecated-capability")
    notice = deprecated.compatibility.deprecations[0]
    removal_changed = _replace_compatibility(
        deprecated,
        deprecations=(replace(notice, removal="3.0"),),
    )
    assert "envelope_status" in _mismatch_fields(status_changed)
    assert "deprecations" in _mismatch_fields(removal_changed)


def test_a_workspace_condition_change_is_detected_as_an_implicit_migration() -> None:
    result = _result("workspace-below-minimum-format")
    mutated = replace(result, workspaces_below_minimum_after=())
    assert "workspaces_below_minimum" in _mismatch_fields(mutated)


def test_matrix_rows_and_reports_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        MATRIX[0].name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        REPORT.results = ()  # type: ignore[misc]
    assert isinstance(REPORT, CompatibilityReport)
