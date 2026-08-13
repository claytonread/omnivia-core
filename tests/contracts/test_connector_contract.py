"""V06-8 acceptance for the public connector contract.

The models here are a trust boundary: a connector is code this repository did
not write, and every value it hands over ends up in a column with a domain. So
these tests are mostly about refusal -- a value the workspace schema would abort
on has to be refused here, where the caller can still see why, rather than four
layers down as an opaque `IntegrityError`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnivia_core.connector import (
    MAX_BATCH_CHANGES,
    ChangeKind,
    ConnectorContractError,
    ConnectorCursor,
    ConnectorError,
    ConnectorFailure,
    DeadLetter,
    HealthState,
    SourceBatch,
    SourceChange,
    SourceConnector,
    SourceHealth,
    SyncOutcome,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKSUM = "sha256:" + "ab" * 32
OTHER_CHECKSUM = "sha256:" + "cd" * 32


def upsert(**overrides: object) -> SourceChange:
    values: dict[str, object] = {
        "change_kind": ChangeKind.UPSERTED,
        "native_id": "doc-1",
        "locator": "/drive/a.txt",
        "checksum": CHECKSUM,
        "media_type": "text/plain",
        "content_length_bytes": 11,
    }
    values.update(overrides)
    return SourceChange(**values)  # type: ignore[arg-type]


# --- the package boundary -------------------------------------------------------


def test_importing_the_contract_pulls_in_no_runtime_and_no_database() -> None:
    """Measured in a fresh interpreter, because this process has other reasons
    to be holding any of these already."""
    probe = (
        "import sys, json;"
        "import omnivia_core.connector;"
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
    for name in (
        "omnivia_core_runtime",
        "omnivia_core_cli",
        "omnivia_core_mcp",
        "omnivia_core_client",
        "omnivia_memory",
        "sqlite3",
        "sqlalchemy",
        "jsonschema",
    ):
        assert name not in loaded, name


def test_every_public_model_is_frozen_and_slotted() -> None:
    """Immutability is the contract; a mutable value could drift after validation."""
    for model in (
        ConnectorCursor,
        ConnectorFailure,
        SourceChange,
        SourceBatch,
        SourceHealth,
        DeadLetter,
        SyncOutcome,
    ):
        assert dataclasses.is_dataclass(model)
        assert model.__dataclass_params__.frozen, model.__name__  # type: ignore[attr-defined]
        assert "__slots__" in vars(model), model.__name__


def test_a_validated_value_cannot_be_mutated_afterwards() -> None:
    cursor = ConnectorCursor(state_version=1, token="page-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cursor.token = "page-2"  # type: ignore[misc]


# --- cursors ---------------------------------------------------------------------


def test_a_cursor_round_trips_through_its_wire_form() -> None:
    cursor = ConnectorCursor(state_version=3, token="opaque-token")
    assert ConnectorCursor.from_wire(cursor.to_wire()) == cursor


@pytest.mark.parametrize(
    "state_version, token",
    [
        (0, "t"),
        (-1, "t"),
        (True, "t"),
        (1, ""),
        (1, "x" * 4097),
        (1, "with\x00nul"),
    ],
)
def test_a_cursor_outside_its_domain_is_refused(
    state_version: object, token: object
) -> None:
    with pytest.raises(ConnectorContractError):
        ConnectorCursor(state_version=state_version, token=token)  # type: ignore[arg-type]


def test_a_cursor_document_with_extra_or_missing_fields_is_refused() -> None:
    with pytest.raises(ConnectorContractError):
        ConnectorCursor.from_wire({"state_version": 1, "token": "t", "extra": 1})
    with pytest.raises(ConnectorContractError):
        ConnectorCursor.from_wire({"state_version": 1})


# --- changes ----------------------------------------------------------------------


def test_a_present_item_carries_content_identity_and_a_locator() -> None:
    change = upsert(permission_labels=("team.a", "team.b"))
    assert change.to_wire()["permission_labels"] == ["team.a", "team.b"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"checksum": None},
        {"checksum": "md5:" + "ab" * 16},
        {"checksum": "sha256:" + "AB" * 32},
        {"checksum": "sha256:" + "ab" * 31},
        {"media_type": None},
        {"media_type": "text"},
        {"media_type": "text/plain/extra"},
        {"content_length_bytes": None},
        {"content_length_bytes": -1},
        {"content_length_bytes": True},
        {"locator": None},
        {"locator": "x" * 2049},
        {"native_id": ""},
        {"native_id": ".leading-dot"},
        {"native_id": "has space"},
        {"native_id": "x" * 129},
        {"previous_locator": "/somewhere"},
    ],
)
def test_a_change_outside_the_durable_domain_is_refused(
    overrides: dict[str, object],
) -> None:
    """Each of these would abort on a CHECK constraint if it got that far."""
    with pytest.raises(ConnectorContractError):
        upsert(**overrides)


@pytest.mark.parametrize(
    "labels",
    [
        ("team.b", "team.a"),
        ("team.a", "team.a"),
        ("Team.A",),
        ("team.",),
        ("team..a",),
        ("1team",),
        ("x" * 129,),
    ],
)
def test_permission_labels_must_be_deterministic_and_in_domain(
    labels: tuple[str, ...],
) -> None:
    """Unordered ACLs would make the same source state produce different records."""
    with pytest.raises(ConnectorContractError):
        upsert(permission_labels=labels)


def test_a_rename_must_actually_move_the_item() -> None:
    with pytest.raises(ConnectorContractError):
        SourceChange(
            change_kind=ChangeKind.RENAMED,
            native_id="doc-1",
            locator="/same.txt",
            previous_locator="/same.txt",
            checksum=CHECKSUM,
            media_type="text/plain",
            content_length_bytes=3,
        )
    with pytest.raises(ConnectorContractError):
        SourceChange(
            change_kind=ChangeKind.RENAMED,
            native_id="doc-1",
            locator="/new.txt",
            checksum=CHECKSUM,
            media_type="text/plain",
            content_length_bytes=3,
        )


def test_a_rename_keeps_the_native_id_and_records_both_locators() -> None:
    change = SourceChange(
        change_kind=ChangeKind.RENAMED,
        native_id="doc-1",
        locator="/new.txt",
        previous_locator="/old.txt",
        checksum=CHECKSUM,
        media_type="text/plain",
        content_length_bytes=3,
    )
    wire = change.to_wire()
    assert (wire["native_id"], wire["locator"], wire["previous_locator"]) == (
        "doc-1",
        "/new.txt",
        "/old.txt",
    )


def test_a_deletion_carries_no_content_facts() -> None:
    deletion = SourceChange(change_kind=ChangeKind.DELETED, native_id="doc-1")
    assert deletion.checksum is None
    for overrides in (
        {"checksum": CHECKSUM},
        {"media_type": "text/plain"},
        {"content_length_bytes": 0},
        {"previous_locator": "/old.txt"},
    ):
        with pytest.raises(ConnectorContractError):
            SourceChange(
                change_kind=ChangeKind.DELETED,
                native_id="doc-1",
                **overrides,  # type: ignore[arg-type]
            )


# --- batches -----------------------------------------------------------------------


def test_a_batch_may_not_report_one_item_twice() -> None:
    """Two verdicts on one item in one batch have no defined application order."""
    with pytest.raises(ConnectorContractError):
        SourceBatch(
            changes=(upsert(), upsert(checksum=OTHER_CHECKSUM)),
            cursor=ConnectorCursor(state_version=1, token="1"),
        )


def test_an_empty_batch_is_legitimate() -> None:
    """A poll that found nothing still advances the cursor."""
    batch = SourceBatch(
        changes=(), cursor=ConnectorCursor(state_version=1, token="1")
    )
    assert batch.to_wire()["changes"] == []


# --- failures, health and outcomes ---------------------------------------------------


def test_a_failure_reuses_the_frozen_retry_vocabulary() -> None:
    failure = ConnectorFailure(
        code="source_throttled", message="slow down", retry_class="retryable"
    )
    assert failure.retryable
    assert ConnectorFailure.from_wire(failure.to_wire()) == failure
    assert not ConnectorFailure(code="gone", message="deleted upstream").retryable


@pytest.mark.parametrize(
    "overrides",
    [
        {"retry_class": "maybe"},
        {"retry_class": ""},
        {"code": "Source_Throttled"},
        {"code": ""},
        {"message": ""},
        {"message": "x" * 2049},
    ],
)
def test_an_unclassifiable_failure_is_refused(overrides: dict[str, str]) -> None:
    values = {"code": "source_throttled", "message": "slow down"} | overrides
    with pytest.raises(ConnectorContractError):
        ConnectorFailure(**values)  # type: ignore[arg-type]


def test_a_connector_error_carries_its_classification() -> None:
    failure = ConnectorFailure(
        code="source_unreachable", message="timeout", retry_class="retryable"
    )
    error = ConnectorError(failure)
    assert error.failure is failure
    assert "source_unreachable" in str(error)


def test_health_states_are_the_three_the_schema_accepts() -> None:
    assert {state.value for state in HealthState} == {
        "healthy",
        "degraded",
        "unavailable",
    }
    assert SourceHealth(state=HealthState.DEGRADED).usable
    assert not SourceHealth(state=HealthState.UNAVAILABLE).usable


def test_a_dead_letter_records_how_many_times_it_was_tried() -> None:
    letter = DeadLetter(
        native_id="doc-1",
        failure=ConnectorFailure(code="gone", message="deleted upstream"),
        attempts=2,
    )
    assert letter.to_wire()["attempts"] == 2
    with pytest.raises(ConnectorContractError):
        dataclasses.replace(letter, attempts=0)
    with pytest.raises(ConnectorContractError):
        dataclasses.replace(letter, attempts=257)


def test_an_outcome_carries_a_failure_exactly_when_it_failed() -> None:
    failure = ConnectorFailure(code="gone", message="source went away")
    assert SyncOutcome(run_id="job-1", state="succeeded").failure is None
    assert SyncOutcome(run_id="job-1", state="failed", failure=failure).failure is failure
    with pytest.raises(ConnectorContractError):
        SyncOutcome(run_id="job-1", state="failed")
    with pytest.raises(ConnectorContractError):
        SyncOutcome(run_id="job-1", state="succeeded", failure=failure)
    with pytest.raises(ConnectorContractError):
        SyncOutcome(run_id="job-1", state="finished")
    with pytest.raises(ConnectorContractError):
        SyncOutcome(run_id="job-1", state="succeeded", ingested=-1)


def test_an_outcome_refuses_nested_values_that_are_not_the_models_it_claims() -> None:
    """The outcome is the run's public word, so its parts are validated as parts.

    A string in either slot is accepted by every check that only asks whether
    the field is present, and then fails at `to_wire` -- inside whatever called
    `synchronise`, long after the caller who could have fixed it.
    """
    for cursor in ("bad", 1, {"state_version": 1, "token": "t"}):
        with pytest.raises(ConnectorContractError):
            SyncOutcome(run_id="job-1", state="succeeded", cursor=cursor)  # type: ignore[arg-type]
    for failure in ("bad", 1, {"code": "gone", "message": "m"}):
        with pytest.raises(ConnectorContractError):
            SyncOutcome(run_id="job-1", state="failed", failure=failure)  # type: ignore[arg-type]


def test_an_outcome_with_valid_nested_values_still_serialises() -> None:
    outcome = SyncOutcome(
        run_id="job-1",
        state="failed",
        cursor=ConnectorCursor(state_version=1, token="page-2"),
        failure=ConnectorFailure(code="gone", message="source went away"),
    )
    wire = outcome.to_wire()
    assert wire["cursor"] == {"state_version": 1, "token": "page-2"}
    assert wire["failure"] == {
        "code": "gone",
        "message": "source went away",
        "retry_class": "non_retryable",
    }
    assert SyncOutcome(run_id="job-1", state="succeeded").to_wire()["cursor"] is None


# --- the protocol -------------------------------------------------------------------


def test_the_protocol_accepts_a_plain_object_with_the_three_methods() -> None:
    """Structural, so a connector inherits nothing and registers nowhere."""

    class Minimal:
        connector_id = "conn-alpha"
        source_kind = "document"
        state_version = 1

        def health(self) -> SourceHealth:
            return SourceHealth(state=HealthState.HEALTHY)

        def fetch(self, cursor: object, *, cancelled: object) -> SourceBatch:
            del cursor, cancelled
            return SourceBatch(
                changes=(), cursor=ConnectorCursor(state_version=1, token="1")
            )

        def content(self, change: SourceChange) -> bytes:
            del change
            return b""

    assert isinstance(Minimal(), SourceConnector)

    class Incomplete:
        connector_id = "conn-beta"

    assert not isinstance(Incomplete(), SourceConnector)


def test_a_batch_may_not_be_unbounded() -> None:
    """A page is held whole in memory and applied in one transaction.

    An unbounded page is therefore an unbounded transaction, and a connector
    with more to say already has `has_more` to say it with.
    """
    cursor = ConnectorCursor(state_version=1, token="1")
    within = tuple(
        upsert(native_id=f"doc-{index}") for index in range(MAX_BATCH_CHANGES)
    )
    assert len(SourceBatch(changes=within, cursor=cursor).changes) == MAX_BATCH_CHANGES
    with pytest.raises(ConnectorContractError):
        SourceBatch(
            changes=(*within, upsert(native_id="doc-over")), cursor=cursor
        )


def test_a_connector_error_must_actually_carry_a_classification() -> None:
    """The classification is the payload, so an error without one is not one."""
    with pytest.raises(ConnectorContractError):
        ConnectorError("source unreachable")  # type: ignore[arg-type]


def test_an_outcome_counts_every_verdict_it_can_reach() -> None:
    """The counters are the run's state machine, so each verdict has its own.

    `unchanged` means nothing durable was written; an ACL change and a
    reappearance after a tombstone both write, so neither may hide inside it.
    """
    outcome = SyncOutcome(
        run_id="job-1",
        state="succeeded",
        ingested=1,
        unchanged=2,
        relabelled=3,
        restored=4,
        renamed=5,
        deleted=6,
        dead_lettered=7,
        truncated=True,
    )
    wire = outcome.to_wire()
    assert [wire[name] for name in ("relabelled", "restored", "truncated")] == [
        3,
        4,
        True,
    ]
    for field_name in ("relabelled", "restored"):
        with pytest.raises(ConnectorContractError):
            dataclasses.replace(outcome, **{field_name: -1})
    with pytest.raises(ConnectorContractError):
        dataclasses.replace(outcome, truncated="yes")  # type: ignore[arg-type]
