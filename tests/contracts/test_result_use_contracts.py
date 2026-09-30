"""`decision.result_use.evaluate` evidence (T-0716, SPEC-CORE-DATA-001 §13.3)."""

from __future__ import annotations

import pytest

from omnivia_core.contracts.v1 import evaluate_result_use
from omnivia_core.contracts.v1.semantics_operations import get_operation_metadata


def _valid(**overrides):  # type: ignore[no-untyped-def]
    request = {
        "request_version": "1.0",
        "use_class": "current_publication",
        "subject_digest": "result-digest-1",
        "completeness": "complete",
        "continuity": "verified",
        "freshness_ok": True,
        "schema_compatible": True,
        "evidence_available": True,
        "policy_permits_partial_or_stale": False,
        "authority_epoch": "epoch-1",
    }
    request.update(overrides)
    return request


def test_perfect_current_publication_is_allowed() -> None:
    assert evaluate_result_use(_valid())["outcome"] == "allow"


def test_action_input_is_denied_until_a_policy_exists() -> None:
    decision = evaluate_result_use(_valid(use_class="action_input"))
    assert decision["outcome"] == "deny"
    assert "action_input_policy_missing" in decision["reasons"]


def test_unknown_completeness_denies() -> None:
    decision = evaluate_result_use(_valid(completeness="unknown"))
    assert decision["outcome"] == "deny"
    assert "completeness_unknown" in decision["reasons"]


def test_partial_denies_unless_the_policy_permits_it() -> None:
    denied = evaluate_result_use(_valid(completeness="partial"))
    assert denied["outcome"] == "deny"
    permitted = evaluate_result_use(
        _valid(
            completeness="partial",
            use_class="exploration",
            policy_permits_partial_or_stale=True,
        )
    )
    assert permitted["outcome"] == "allow_with_warning"


def test_a_detected_gap_denies() -> None:
    decision = evaluate_result_use(_valid(continuity="gap_detected"))
    assert decision["outcome"] == "deny"
    assert "continuity_gap_detected" in decision["reasons"]


def test_exploration_never_certifies() -> None:
    decision = evaluate_result_use(_valid(use_class="exploration"))
    assert decision["outcome"] == "allow_with_warning"


def test_stale_historical_display_is_a_warning_not_a_denial() -> None:
    decision = evaluate_result_use(
        _valid(use_class="historical_display", freshness_ok=False)
    )
    assert decision["outcome"] == "allow_with_warning"
    assert "freshness_stale_display" in decision["reasons"]


def test_incompatible_schema_denies_every_use() -> None:
    for use_class in ("exploration", "historical_display", "current_publication"):
        decision = evaluate_result_use(
            _valid(use_class=use_class, schema_compatible=False)
        )
        assert decision["outcome"] == "deny"


def test_the_catalogue_posture_is_a_synchronous_workspace_read() -> None:
    metadata = get_operation_metadata("decision.result_use.evaluate")
    assert metadata.scope.side_effect == "none"
    assert metadata.scope.required_scopes == ("decision:read",)
    assert metadata.required_capability.id == "decision.read"
    assert metadata.job.completion_mode == "synchronous"


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        {"request_version": "2.0", "use_class": "exploration"},
        dict(_valid(), unrecognised=True),
        dict(_valid(), subject_digest="-bad"),
    ],
)
def test_malformed_requests_raise_for_the_handler(document) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((ValueError, TypeError)):
        evaluate_result_use(document)
