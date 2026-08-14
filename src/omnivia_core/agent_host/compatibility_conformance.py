"""Compatibility conformance for the provider SPI (V06-8, A9-P6).

This fourth conformance layer executes the accepted A9 section 7 compatibility
matrix.  Six rows, fixed in order, each one a real ``spi.negotiate`` call
against a fresh :class:`MockProvider` and a fresh :class:`ProviderProfile`, and
each one graded on the four values section 7 fixes: the disposition, the
compatibility status, the error code, and the selected SPI version.

The matrix is data and the executor has no per-row branch, so a row cannot be
graded by code written for it.  It adds no operation, adapter, storage,
migration, or network surface, and it drives negotiation only: the layer reads
what the provider already reports rather than teaching it anything new.

The fifth grade is the version report itself.  Section 7 fixes five axes -- SPI,
API, server, workspace format, client -- and fixes that they travel *together*,
because an adapter told about one axis at a time can be simultaneously supported
and unsupported.  So each row asserts one :class:`VersionAxes` carrying all five
named axes, and the axis names are read off the frozen value type rather than
restated, which is what makes a dropped, renamed, or reordered axis a failure
here rather than a silent narrowing.

The report fails closed.  A row that is missing, duplicated, reordered, or
unexpected is a failure of the matrix as a whole, not a row that quietly does
not report; and a row whose result lacks a version report, or misreports one
axis of it, fails on that axis by name.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Final

from omnivia_core.agent_host.mock import DEFAULT_PROFILE, MockProvider, ProviderProfile
from omnivia_core.agent_host.spi import (
    HOOK_COMPOSITIONS,
    SPI_COMPATIBILITY_STATUSES,
    SPI_ERROR_CODES,
    SPI_VERSION_MAXIMUM,
    SPI_VERSION_MINIMUM,
    Disposition,
    Hook,
    HookOutcome,
    SpiProvenance,
    SpiRequest,
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
    UPGRADE_STATE_REQUIRED,
    CompatibilityMetadata,
)

#: The five axes section 7 reports together, in the order the frozen value type
#: declares them.  Read off :class:`VersionAxes` at grading time, so an axis that
#: is dropped, renamed, or reordered fails the matrix instead of vanishing.
AXIS_NAMES: Final[tuple[str, ...]] = (
    "spi",
    "api",
    "server",
    "workspace_format",
    "client",
)

#: The negotiation call every row makes, apart from the three inputs a row varies:
#: the declared SPI version, the capabilities it requires, and whether its
#: workspace sits below the server's minimum format.
NEGOTIATION_CALLER: Final = "adapter-under-conformance"
NEGOTIATION_WORKSPACE: Final = "workspace-under-negotiation"
NEGOTIATION_PURPOSE: Final = "spi_negotiation"
NEGOTIATION_DEADLINE_MS: Final = 30_000

#: A minor above the server maximum, and a major beside it.  Derived from the
#: frozen window rather than spelled out, so widening the window keeps row 2 a
#: newer minor and row 4 a different major instead of silently making them
#: something else.
_MAX_PARTS: Final[tuple[int, ...]] = tuple(
    int(part) for part in SPI_VERSION_MAXIMUM.split(".")
)
NEWER_MINOR_VERSION: Final = f"{_MAX_PARTS[0]}.{_MAX_PARTS[1] + 1}.0"
DIFFERENT_MAJOR_VERSION: Final = f"{_MAX_PARTS[0] + 1}.0.0"

#: A capability the default profile supports and does not deprecate, one it
#: supports and does deprecate, and one it does not support at all.  Each is
#: literal and each is guarded by a test against the profile, so a profile change
#: fails loudly rather than turning a row into a different row.
HEALTHY_CAPABILITY: Final = "workspace.read@1.0"
DEPRECATED_CAPABILITY: Final = "memory.read@1.0"
UNSUPPORTED_CAPABILITY: Final = "memory.read@2.0"
DEPRECATION_REMOVAL_VERSION: Final = "2.0"


class CompatibilityConformanceError(ValueError):
    """A malformed matrix row, row result, or report input."""


def _non_empty(name: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise CompatibilityConformanceError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class CompatibilityRow:
    """One section 7 row: the three inputs it varies and the five grades it fixes."""

    name: str
    declared_spi_version: str
    required_capabilities: tuple[str, ...]
    workspace_below_minimum: bool
    expected_disposition: Disposition
    expected_status: str
    expected_error_code: str | None
    expected_selected_spi: str | None
    expected_upgrade_state: str
    expected_deprecations: tuple[tuple[str, str, str | None], ...]

    def __post_init__(self) -> None:
        _non_empty("name", self.name)
        _non_empty("declared_spi_version", self.declared_spi_version)
        for token in self.required_capabilities:
            _non_empty("required_capabilities", token)
        if type(self.workspace_below_minimum) is not bool:
            raise CompatibilityConformanceError(
                "workspace_below_minimum must be a boolean"
            )
        if not isinstance(self.expected_disposition, Disposition):
            raise CompatibilityConformanceError(
                "expected_disposition must be a Disposition"
            )
        if self.expected_status not in SPI_COMPATIBILITY_STATUSES:
            raise CompatibilityConformanceError(
                "expected_status must be a frozen status"
            )
        if (
            self.expected_error_code is not None
            and self.expected_error_code not in SPI_ERROR_CODES
        ):
            raise CompatibilityConformanceError(
                "expected_error_code must be a frozen code"
            )
        if (self.expected_disposition is Disposition.REFUSED) != (
            self.expected_error_code is not None
        ):
            raise CompatibilityConformanceError(
                "only a refused row expects an error code"
            )
        if (self.expected_disposition is Disposition.NEGOTIATED) != (
            self.expected_selected_spi is not None
        ):
            raise CompatibilityConformanceError(
                "only a proceeding row selects an SPI version"
            )
        if self.expected_upgrade_state not in (
            UPGRADE_STATE_NONE,
            UPGRADE_STATE_REQUIRED,
        ):
            raise CompatibilityConformanceError(
                "expected_upgrade_state must be a frozen state"
            )
        for identifier, since, removal in self.expected_deprecations:
            _non_empty("expected_deprecations.id", identifier)
            _non_empty("expected_deprecations.since", since)
            if removal is not None:
                _non_empty("expected_deprecations.removal", removal)

    @property
    def proceeds(self) -> bool:
        """Section 7's proceed/refuse column, derived from the disposition."""
        return self.expected_disposition is not Disposition.REFUSED


#: The accepted A9 section 7 matrix.  Immutable, and the order is the contract:
#: the report grades the row names against this tuple, so a missing, duplicated,
#: reordered, or extra row fails the report rather than passing a shorter run.
MATRIX: Final[tuple[CompatibilityRow, ...]] = (
    CompatibilityRow(
        name="same-major-older-or-equal-minor",
        declared_spi_version=SPI_VERSION_MINIMUM,
        required_capabilities=(HEALTHY_CAPABILITY,),
        workspace_below_minimum=False,
        expected_disposition=Disposition.NEGOTIATED,
        expected_status=COMPATIBILITY_STATUS_COMPATIBLE,
        expected_error_code=None,
        expected_selected_spi=SPI_VERSION_MINIMUM,
        expected_upgrade_state=UPGRADE_STATE_NONE,
        expected_deprecations=(),
    ),
    CompatibilityRow(
        name="same-major-newer-minor",
        declared_spi_version=NEWER_MINOR_VERSION,
        required_capabilities=(HEALTHY_CAPABILITY,),
        workspace_below_minimum=False,
        expected_disposition=Disposition.NEGOTIATED,
        expected_status=COMPATIBILITY_STATUS_COMPATIBLE,
        expected_error_code=None,
        expected_selected_spi=SPI_VERSION_MAXIMUM,
        expected_upgrade_state=UPGRADE_STATE_NONE,
        expected_deprecations=(),
    ),
    CompatibilityRow(
        name="deprecated-capability",
        declared_spi_version=SPI_VERSION_MINIMUM,
        required_capabilities=(DEPRECATED_CAPABILITY,),
        workspace_below_minimum=False,
        expected_disposition=Disposition.NEGOTIATED,
        expected_status=COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
        expected_error_code=None,
        expected_selected_spi=SPI_VERSION_MINIMUM,
        expected_upgrade_state=UPGRADE_STATE_NONE,
        expected_deprecations=(
            (
                DEPRECATED_CAPABILITY.split("@", 1)[0],
                DEFAULT_PROFILE.axes.api,
                DEPRECATION_REMOVAL_VERSION,
            ),
        ),
    ),
    CompatibilityRow(
        name="different-major",
        declared_spi_version=DIFFERENT_MAJOR_VERSION,
        required_capabilities=(HEALTHY_CAPABILITY,),
        workspace_below_minimum=False,
        expected_disposition=Disposition.REFUSED,
        expected_status=COMPATIBILITY_STATUS_INCOMPATIBLE,
        expected_error_code=ERROR_CODE_INCOMPATIBLE_VERSION,
        expected_selected_spi=None,
        expected_upgrade_state=UPGRADE_STATE_NONE,
        expected_deprecations=(),
    ),
    CompatibilityRow(
        name="required-capability-absent",
        declared_spi_version=SPI_VERSION_MINIMUM,
        required_capabilities=(UNSUPPORTED_CAPABILITY,),
        workspace_below_minimum=False,
        expected_disposition=Disposition.REFUSED,
        expected_status=COMPATIBILITY_STATUS_INCOMPATIBLE,
        expected_error_code=ERROR_CODE_CAPABILITY_NOT_GRANTED,
        expected_selected_spi=None,
        expected_upgrade_state=UPGRADE_STATE_NONE,
        expected_deprecations=(),
    ),
    CompatibilityRow(
        name="workspace-below-minimum-format",
        declared_spi_version=SPI_VERSION_MINIMUM,
        required_capabilities=(HEALTHY_CAPABILITY,),
        workspace_below_minimum=True,
        expected_disposition=Disposition.REFUSED,
        expected_status=COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
        expected_error_code=ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
        expected_selected_spi=None,
        expected_upgrade_state=UPGRADE_STATE_REQUIRED,
        expected_deprecations=(),
    ),
)

MATRIX_ROW_NAMES: Final[tuple[str, ...]] = tuple(row.name for row in MATRIX)


@dataclass(frozen=True, slots=True)
class RowResult:
    """One executed row: what the provider answered, and what it left behind.

    ``workspaces_below_minimum_after`` is the migration probe.  A negotiation
    that refused `workspace_migration_required` must leave the workspace exactly
    as far below the minimum format as it found it, so the layer reads the
    condition back off the profile after the call instead of assuming a provider
    with no migrator cannot have run one.
    """

    row: CompatibilityRow
    request: SpiRequest
    outcome: HookOutcome
    negotiated: bool
    selected_spi_version: str | None
    workspaces_below_minimum_before: tuple[str, ...]
    workspaces_below_minimum_after: tuple[str, ...]
    profile_axes: VersionAxes

    def __post_init__(self) -> None:
        if not isinstance(self.row, CompatibilityRow):
            raise CompatibilityConformanceError("row must be a CompatibilityRow")
        if not isinstance(self.request, SpiRequest):
            raise CompatibilityConformanceError("request must be a SpiRequest")
        if not isinstance(self.outcome, HookOutcome):
            raise CompatibilityConformanceError("outcome must be a HookOutcome")
        if type(self.negotiated) is not bool:
            raise CompatibilityConformanceError("negotiated must be a boolean")
        if not isinstance(self.profile_axes, VersionAxes):
            raise CompatibilityConformanceError("profile_axes must be a VersionAxes")

    @property
    def compatibility(self) -> CompatibilityMetadata:
        """The frozen compatibility metadata the response envelope carries."""
        return self.outcome.response.metadata.version.compatibility


@dataclass(frozen=True, slots=True)
class CompatibilityMismatch:
    """One graded value that came back other than section 7 fixes it."""

    row: str
    field: str
    expected: str
    actual: str

    def __post_init__(self) -> None:
        for name in ("row", "field", "expected", "actual"):
            _non_empty(name, getattr(self, name))

    def __str__(self) -> str:
        return f"{self.row}.{self.field}: expected {self.expected}, got {self.actual}"


def _mismatch(
    row: str, field: str, expected: object, actual: object
) -> tuple[CompatibilityMismatch, ...]:
    if expected == actual:
        return ()
    return (
        CompatibilityMismatch(
            row=row, field=field, expected=repr(expected), actual=repr(actual)
        ),
    )


def _grade_axes(result: RowResult) -> tuple[CompatibilityMismatch, ...]:
    """The five axes, present together and each reporting the expected value."""
    row = result.row
    declared = tuple(item.name for item in fields(VersionAxes))
    if declared != AXIS_NAMES:
        return _mismatch(row.name, "version_axes.names", AXIS_NAMES, declared)
    axes = result.outcome.version_axes
    if axes is None:
        return _mismatch(row.name, "version_axes", "all five axes", None)
    # A refusal reports the server's own SPI axis; a negotiation reports the one
    # it selected.  Every other axis is the profile's, unchanged either way.
    expected = {name: getattr(result.profile_axes, name) for name in AXIS_NAMES}
    if row.expected_selected_spi is not None:
        expected["spi"] = row.expected_selected_spi
    return tuple(
        item
        for name in AXIS_NAMES
        for item in _mismatch(
            row.name, f"version_axes.{name}", expected[name], getattr(axes, name)
        )
    )


def grade_row(result: RowResult) -> tuple[CompatibilityMismatch, ...]:
    """Grade one executed row.  No branch here is written for a single row."""
    if not isinstance(result, RowResult):
        raise CompatibilityConformanceError("result must be a RowResult")
    row = result.row
    outcome = result.outcome
    compatibility = result.compatibility
    expected_operations = tuple(
        operation for operation in HOOK_COMPOSITIONS[Hook.NEGOTIATE] if row.proceeds
    )
    return (
        *_mismatch(row.name, "request.hook", Hook.NEGOTIATE, result.request.hook),
        *_mismatch(
            row.name, "request.caller", NEGOTIATION_CALLER, result.request.caller
        ),
        *_mismatch(
            row.name,
            "request.workspace",
            NEGOTIATION_WORKSPACE,
            result.request.workspace,
        ),
        *_mismatch(
            row.name, "request.purpose", NEGOTIATION_PURPOSE, result.request.purpose
        ),
        *_mismatch(
            row.name,
            "request.deadline_ms",
            NEGOTIATION_DEADLINE_MS,
            result.request.deadline_ms,
        ),
        *_mismatch(
            row.name,
            "request.declared_spi_version",
            row.declared_spi_version,
            result.request.declared_spi_version,
        ),
        *_mismatch(
            row.name,
            "request.required_capabilities",
            row.required_capabilities,
            result.request.required_capabilities,
        ),
        *_mismatch(row.name, "request.turn_ordinal", None, result.request.turn_ordinal),
        *_mismatch(row.name, "hook", Hook.NEGOTIATE, outcome.hook),
        *_mismatch(
            row.name, "disposition", row.expected_disposition, outcome.disposition
        ),
        *_mismatch(row.name, "proceeded", row.proceeds, result.negotiated),
        *_mismatch(
            row.name,
            "compatibility_status",
            row.expected_status,
            outcome.compatibility_status,
        ),
        *_mismatch(
            row.name, "envelope_status", row.expected_status, compatibility.status
        ),
        *_mismatch(row.name, "error_code", row.expected_error_code, outcome.error_code),
        *_mismatch(
            row.name,
            "selected_spi_version",
            row.expected_selected_spi,
            result.selected_spi_version,
        ),
        *_mismatch(
            row.name,
            "upgrade_state",
            row.expected_upgrade_state,
            compatibility.upgrade_state.value,
        ),
        *_mismatch(
            row.name,
            "deprecations",
            row.expected_deprecations,
            tuple(
                (item.id, item.since, item.removal)
                for item in compatibility.deprecations
            ),
        ),
        *_mismatch(
            row.name,
            "workspaces_below_minimum",
            result.workspaces_below_minimum_before,
            result.workspaces_below_minimum_after,
        ),
        *_mismatch(
            row.name,
            "composed_operations",
            expected_operations,
            outcome.composed_operations,
        ),
        *_mismatch(
            row.name,
            "nested_envelope_operations",
            expected_operations,
            tuple(envelope.operation for envelope in outcome.nested_envelopes),
        ),
        *_grade_axes(result),
    )


def execute_row(row: CompatibilityRow) -> RowResult:
    """Run one row's ``spi.negotiate`` against a fresh provider and profile."""
    if not isinstance(row, CompatibilityRow):
        raise CompatibilityConformanceError("row must be a CompatibilityRow")
    profile = ProviderProfile(
        workspaces_below_minimum_format=(
            frozenset({NEGOTIATION_WORKSPACE})
            if row.workspace_below_minimum
            else frozenset()
        )
    )
    provider = MockProvider(profile)
    before = tuple(sorted(profile.workspaces_below_minimum_format))
    request = SpiRequest(
        hook=Hook.NEGOTIATE,
        caller=NEGOTIATION_CALLER,
        workspace=NEGOTIATION_WORKSPACE,
        purpose=NEGOTIATION_PURPOSE,
        provenance=SpiProvenance(
            agent="agent-under-conformance",
            session=f"session-{row.name}",
            run=f"run-{row.name}",
            sequence=0,
        ),
        granted_capabilities=row.required_capabilities,
        required_capabilities=row.required_capabilities,
        deadline_ms=NEGOTIATION_DEADLINE_MS,
        declared_spi_version=row.declared_spi_version,
    )
    outcome = provider.handle(request)
    return RowResult(
        row=row,
        request=request,
        outcome=outcome,
        negotiated=provider.negotiated,
        selected_spi_version=provider.selected_spi_version,
        workspaces_below_minimum_before=before,
        workspaces_below_minimum_after=tuple(
            sorted(profile.workspaces_below_minimum_format)
        ),
        profile_axes=profile.axes,
    )


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """The six rows and every value that came back other than section 7 fixes it."""

    results: tuple[RowResult, ...]
    mismatches: tuple[CompatibilityMismatch, ...]

    def __post_init__(self) -> None:
        for name, kind in (
            ("results", RowResult),
            ("mismatches", CompatibilityMismatch),
        ):
            value = getattr(self, name)
            if not isinstance(value, Sequence) or isinstance(value, str):
                raise CompatibilityConformanceError(f"{name} must be a sequence")
            items = tuple(value)
            if not all(isinstance(item, kind) for item in items):
                raise CompatibilityConformanceError(
                    f"{name} must contain {kind.__name__}"
                )
            object.__setattr__(self, name, items)

    @property
    def graded_rows(self) -> tuple[str, ...]:
        return tuple(result.row.name for result in self.results)

    @property
    def covered_every_row(self) -> bool:
        """The matrix exactly: no row missing, duplicated, reordered, or extra."""
        return self.graded_rows == MATRIX_ROW_NAMES

    @property
    def reported_every_axis(self) -> bool:
        return all(
            result.outcome.version_axes is not None
            and tuple(item.name for item in fields(VersionAxes)) == AXIS_NAMES
            for result in self.results
        )

    @property
    def passed(self) -> bool:
        return (
            self.covered_every_row and self.reported_every_axis and not self.mismatches
        )

    @property
    def detail(self) -> str:
        parts = [str(item) for item in self.mismatches]
        graded = self.graded_rows
        if not self.covered_every_row:
            missing = tuple(name for name in MATRIX_ROW_NAMES if name not in graded)
            extra = tuple(name for name in graded if name not in MATRIX_ROW_NAMES)
            duplicated = tuple(
                name for name in MATRIX_ROW_NAMES if graded.count(name) > 1
            )
            if missing:
                parts.append(f"rows: missing {missing!r}")
            if extra:
                parts.append(f"rows: unexpected {extra!r}")
            if duplicated:
                parts.append(f"rows: duplicated {duplicated!r}")
            if not (missing or extra or duplicated):
                parts.append(
                    f"rows: expected order {MATRIX_ROW_NAMES!r}, got {graded!r}"
                )
        parts.extend(
            f"{result.row.name}: reported no version axes"
            for result in self.results
            if result.outcome.version_axes is None
        )
        return "; ".join(parts)


def evaluate(results: Sequence[RowResult]) -> CompatibilityReport:
    """Grade executed rows in the order they were executed."""
    items = tuple(results)
    if not all(isinstance(item, RowResult) for item in items):
        raise CompatibilityConformanceError("results must contain RowResult")
    return CompatibilityReport(
        results=items,
        mismatches=tuple(item for result in items for item in grade_row(result)),
    )


def run_compatibility_conformance() -> CompatibilityReport:
    """Execute and grade the whole accepted section 7 matrix."""
    return evaluate(tuple(execute_row(row) for row in MATRIX))


# The matrix is the contract; a duplicate or a short matrix must not be a run
# that quietly grades fewer rows.
if len(set(MATRIX_ROW_NAMES)) != len(
    MATRIX_ROW_NAMES
):  # pragma: no cover - import guard
    raise CompatibilityConformanceError("matrix row names must be unique")
if DEFAULT_PROFILE.deprecated_capabilities and (
    DEPRECATED_CAPABILITY not in DEFAULT_PROFILE.deprecated_capabilities
):  # pragma: no cover - import guard
    raise CompatibilityConformanceError(
        "the deprecated row must name a deprecated capability"
    )


__all__ = [
    "AXIS_NAMES",
    "DEPRECATED_CAPABILITY",
    "DEPRECATION_REMOVAL_VERSION",
    "DIFFERENT_MAJOR_VERSION",
    "HEALTHY_CAPABILITY",
    "MATRIX",
    "MATRIX_ROW_NAMES",
    "NEGOTIATION_CALLER",
    "NEGOTIATION_DEADLINE_MS",
    "NEGOTIATION_PURPOSE",
    "NEGOTIATION_WORKSPACE",
    "NEWER_MINOR_VERSION",
    "UNSUPPORTED_CAPABILITY",
    "CompatibilityConformanceError",
    "CompatibilityMismatch",
    "CompatibilityReport",
    "CompatibilityRow",
    "RowResult",
    "evaluate",
    "execute_row",
    "grade_row",
    "run_compatibility_conformance",
]
