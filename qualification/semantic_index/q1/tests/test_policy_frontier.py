"""Permanent cases for the frozen Q1 policy and pre-engine frontier."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

_DIR = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "q1_policy_under_test", _DIR / "policy.py"
)
assert _SPEC is not None and _SPEC.loader is not None
policy: Any = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = policy
_SPEC.loader.exec_module(policy)

_ZERO_HASH = "sha256:" + "0" * 64
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _label_events(*pairs: tuple[str, str]) -> tuple[list[dict[str, Any]], str]:
    events = [
        {
            "event_id": f"label-{index:03d}",
            "sequence": index,
            "label": label,
            "action": action,
        }
        for index, (label, action) in enumerate(pairs, 1)
    ]
    return events, policy.canonical.canonical_sha256_ref(events)


def _provenance(*pairs: tuple[str, int | None]) -> tuple[list[dict[str, Any]], str]:
    events = [
        {
            "event_id": f"provenance-{index:03d}",
            "sequence": index,
            "action": action,
            "tombstoned_observation": observation,
        }
        for index, (action, observation) in enumerate(pairs, 1)
    ]
    return events, policy.canonical.canonical_sha256_ref(events)


def _evidence(
    record_id: str = "q1-record-evidence-000001",
    *,
    label: str = "acl.allow.principal.reader_internal",
    sensitivity: str = "internal",
    recorded_at_us: int = 100,
) -> dict[str, Any]:
    labels, labels_digest = (
        _label_events((label, "attached")) if label else _label_events()
    )
    provenance, provenance_digest = _provenance(("created", None))
    return {
        "record_id": record_id,
        "kind": "evidence",
        "workspace_id": "workspace.alpha",
        "sensitivity": sensitivity,
        "recorded_at_us": recorded_at_us,
        "label_events": labels,
        "label_events_digest": labels_digest,
        "provenance_events": provenance,
        "provenance_events_digest": provenance_digest,
    }


def _governed(
    record_id: str = "q1-record-memory-000001",
    *,
    kind: str = "memory",
    supports: list[str] | None = None,
    label: str = "",
) -> dict[str, Any]:
    labels, labels_digest = (
        _label_events((label, "attached")) if label else _label_events()
    )
    return {
        "record_id": record_id,
        "kind": kind,
        "workspace_id": "workspace.alpha",
        "sensitivity": "public",
        "recorded_at_us": 100,
        "label_events": labels,
        "label_events_digest": labels_digest,
        "valid_from_us": 100,
        "valid_to_us": 200,
        "governance": {
            "layer": "governed",
            "disposition": "accepted",
            "displaced": False,
        },
        "supporting_evidence_ids": supports or ["q1-record-evidence-000001"],
    }


def _authority(principal_id: str = "principal.reader_internal") -> dict[str, Any]:
    frozen = policy.SUBJECT_CATALOGUE[principal_id]
    return {
        "principal_id": principal_id,
        "role_ids": list(frozen["roles"]),
        "workspace_grant": {
            "workspace_id": frozen["workspace_id"],
            "scopes": ["memory:read"],
        },
        "effective_capabilities": [],
        "clearance": frozen["clearance"],
    }


def _input(
    operation: str = "evidence.search",
    *,
    resources: list[dict[str, Any]] | None = None,
    principal_id: str = "principal.reader_internal",
) -> dict[str, Any]:
    authority = _authority(principal_id)
    authority["effective_capabilities"] = [
        {"capability_id": policy.CAPABILITY_MAP[operation], "version": "1.0"}
    ]
    value = {
        "schema_id": policy.POLICY_INPUT_SCHEMA_ID,
        "schema_version": "1.0",
        "noncanonical": True,
        "policy_input_id": _ZERO_HASH,
        "fixture_policy_schema_id": policy.SCHEMA_ID,
        "policy_set_id": policy.POLICY_SET_ID,
        "policy_epoch": 7,
        "expected_policy_epoch": 7,
        "policy_digest": policy.POLICY_DIGEST,
        "source_digests": {},
        "operation": operation,
        "authority": authority,
        "request": {
            "request_id": _ZERO_HASH,
            "workspace_id": "workspace.alpha",
            "scopes": ["memory:read"],
            "purpose": "user_initiated",
            "v_us": 150,
            "t_us": 150,
        },
        "resources": resources if resources is not None else [_evidence()],
    }
    _rebind_sources(value)
    return value


def _rebind_sources(value: dict[str, Any]) -> None:
    value["source_digests"] = {
        "authority": policy.canonical.canonical_sha256_ref(value["authority"]),
        "operation_catalogue": policy.OPERATION_CATALOGUE_DIGEST,
        "resources": policy.canonical.canonical_sha256_ref(value["resources"]),
    }


def _evaluate(value: dict[str, Any], *, rules: Any = policy.RULES) -> Any:
    _rebind_sources(value)
    return policy.evaluate_frontier(value, rules=rules)


def _rebind_labels(resource: dict[str, Any]) -> None:
    resource["label_events_digest"] = policy.canonical.canonical_sha256_ref(
        resource["label_events"]
    )


def _rebind_provenance(resource: dict[str, Any]) -> None:
    resource["provenance_events_digest"] = policy.canonical.canonical_sha256_ref(
        resource["provenance_events"]
    )


def test_exact_rule_inventory_shape_order_and_digest() -> None:
    assert len(policy.RULES) == 42
    ids = [rule["rule_id"] for rule in policy.RULES]
    assert ids == sorted(ids, key=lambda item: item.encode("utf-8"))
    assert all(
        set(rule) == {"rule_id", "effect", "operation", "all"} for rule in policy.RULES
    )
    assert all(
        set(condition) == {"operator", "operands"}
        for rule in policy.RULES
        for condition in rule["all"]
    )
    assert any(
        rule["rule_id"].startswith("q1.evidence.search.") for rule in policy.RULES
    )
    assert any(
        condition["operands"].get("capability_id") == "knowledge.read"
        for rule in policy.RULES
        for condition in rule["all"]
    )
    assert _HASH_RE.fullmatch(policy.POLICY_DIGEST)
    assert policy.POLICY_DIGEST == policy.canonical.canonical_sha256_ref(
        list(policy.RULES)
    )


def test_policy_input_schema_and_evaluator_accept_the_same_complete_input() -> None:
    schema = json.loads((_DIR / "schemas" / "policy-input.schema.json").read_text())
    value = _input()
    Draft202012Validator(schema).validate(value)
    assert _evaluate(value).ids == ("q1-record-evidence-000001",)


def test_rule_mutation_and_unknown_operator_fail_without_frontier() -> None:
    mutated = copy.deepcopy(list(policy.RULES))
    mutated[0]["all"][0]["operator"] = "unknown"
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(), rules=mutated)


def test_allow_success_sorting_digest_and_empty_success() -> None:
    first = _evidence("q1-record-evidence-000010")
    second = _evidence("q1-record-evidence-000002")
    result = _evaluate(_input(resources=[first, second]))
    assert result.ids == tuple(
        sorted(
            ("q1-record-evidence-000010", "q1-record-evidence-000002"),
            key=lambda item: item.encode(),
        )
    )
    assert _HASH_RE.fullmatch(result.frontier_digest)
    assert result == _evaluate(_input(resources=[first, second]))

    empty = _evaluate(_input(resources=[_evidence(label="")]))
    assert empty.ids == ()
    assert _HASH_RE.fullmatch(empty.frontier_digest)


def test_deny_precedence() -> None:
    resource = _evidence()
    resource["label_events"], resource["label_events_digest"] = _label_events(
        ("acl.allow.principal.reader_internal", "attached"),
        ("acl.deny.role.reader", "attached"),
    )
    assert _evaluate(_input(resources=[resource])).ids == ()


def test_workspace_scope_and_grant_gates() -> None:
    wrong_workspace = _input()
    wrong_workspace["request"]["workspace_id"] = "workspace.beta"
    assert _evaluate(wrong_workspace).ids == ()

    no_scope = _input()
    no_scope["request"]["scopes"] = []
    assert _evaluate(no_scope).ids == ()

    absent_grant = _input()
    del absent_grant["authority"]["workspace_grant"]
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(absent_grant)


@pytest.mark.parametrize(
    "capabilities",
    [
        [],
        [{"capability_id": "evidence.read", "version": "0.9"}],
        [{"capability_id": "memory.read", "version": "1.0"}],
        [{"capability_id": "evidence.read", "version": "2.0"}],
    ],
)
def test_capability_fail_closed_cases(capabilities: list[dict[str, str]]) -> None:
    value = _input()
    value["authority"]["effective_capabilities"] = capabilities
    assert _evaluate(value).ids == ()


@pytest.mark.parametrize(
    "field", ["schema_id", "fixture_policy_schema_id", "policy_digest"]
)
def test_policy_identity_mismatch_is_error(field: str) -> None:
    value = _input()
    value[field] = "wrong"
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(value)


def test_stale_epoch_invalid_purpose_and_bad_source_digest_are_errors() -> None:
    stale = _input()
    stale["expected_policy_epoch"] = 8
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(stale)

    purpose = _input()
    purpose["request"]["purpose"] = "background"
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(purpose)

    digest = _input()
    digest["source_digests"]["resources"] = "bad"
    with pytest.raises(policy.PolicyEvaluationError):
        policy.evaluate_frontier(digest)

    unbound = _input()
    unbound["source_digests"]["resources"] = "sha256:" + "f" * 64
    with pytest.raises(policy.PolicyEvaluationError, match="do not bind"):
        policy.evaluate_frontier(unbound)


@pytest.mark.parametrize(
    "principal_id,sensitivity,allowed",
    [
        ("principal.reader_public", "public", True),
        ("principal.reader_public", "internal", False),
        ("principal.reader_internal", "internal", True),
        ("principal.reader_internal", "confidential", False),
        ("principal.reviewer_confidential", "confidential", True),
        ("principal.reviewer_confidential", "restricted", False),
        ("principal.restricted", "restricted", True),
    ],
)
def test_every_clearance_boundary(
    principal_id: str, sensitivity: str, allowed: bool
) -> None:
    suffix = principal_id.removeprefix("principal.")
    resource = _evidence(label=f"acl.allow.principal.{suffix}", sensitivity=sensitivity)
    result = _evaluate(_input(resources=[resource], principal_id=principal_id))
    assert bool(result.ids) is allowed


def test_transaction_time_boundary() -> None:
    resource = _evidence(recorded_at_us=150)
    exact = _input(resources=[resource])
    assert _evaluate(exact).ids == ("q1-record-evidence-000001",)
    before = copy.deepcopy(exact)
    before["request"]["t_us"] = 149
    assert _evaluate(before).ids == ()


def test_label_withdrawal_corrected_and_unknown_behavior() -> None:
    withdrawn = _evidence()
    withdrawn["label_events"], withdrawn["label_events_digest"] = _label_events(
        ("acl.allow.principal.reader_internal", "attached"),
        ("acl.allow.principal.reader_internal", "withdrawn"),
    )
    assert _evaluate(_input(resources=[withdrawn])).ids == ()

    corrected = _evidence()
    corrected["label_events"][0]["action"] = "corrected"
    _rebind_labels(corrected)
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[corrected]))

    unknown_active = _evidence(label="acl.allow.role.unknown")
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[unknown_active]))

    unknown_inactive = _evidence()
    unknown_inactive["label_events"], unknown_inactive["label_events_digest"] = (
        _label_events(
            ("unknown.raw.label", "withdrawn"),
            ("acl.allow.principal.reader_internal", "attached"),
        )
    )
    assert _evaluate(_input(resources=[unknown_inactive])).ids == (
        "q1-record-evidence-000001",
    )


def test_label_stream_order_and_digest_are_bound() -> None:
    resource = _evidence()
    resource["label_events_digest"] = _ZERO_HASH
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[resource]))

    reordered = _evidence()
    reordered["label_events"], _ = _label_events(
        ("acl.allow.role.reader", "attached"),
        ("acl.allow.principal.reader_internal", "attached"),
    )
    reordered["label_events"][1]["sequence"] = 1
    _rebind_labels(reordered)
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[reordered]))


def test_tombstone_exactness_digest_and_non_restoration() -> None:
    tombstoned = _evidence()
    tombstoned["provenance_events"], tombstoned["provenance_events_digest"] = (
        _provenance(("tombstoned", 1), ("restored", None))
    )
    assert _evaluate(_input(resources=[tombstoned])).ids == ()

    invalid = _evidence()
    invalid["provenance_events"], invalid["provenance_events_digest"] = _provenance(
        ("tombstoned", None)
    )
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[invalid]))

    wrong_action = _evidence()
    wrong_action["provenance_events"], wrong_action["provenance_events_digest"] = (
        _provenance(("created", 1))
    )
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[wrong_action]))

    mismatch = _evidence()
    mismatch["provenance_events_digest"] = _ZERO_HASH
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[mismatch]))

    unknown = _evidence()
    unknown["provenance_events"][0]["action"] = "unknown"
    _rebind_provenance(unknown)
    with pytest.raises(policy.PolicyEvaluationError, match="unknown provenance action"):
        _evaluate(_input(resources=[unknown]))


def test_governed_valid_time_and_governance_boundaries() -> None:
    evidence = _evidence()
    governed = _governed()
    value = _input("memory.search", resources=[evidence, governed])
    assert _evaluate(value).ids == ("q1-record-memory-000001",)

    at_end = copy.deepcopy(value)
    at_end["request"]["v_us"] = 200
    assert _evaluate(at_end).ids == ()

    before_start = copy.deepcopy(value)
    before_start["request"]["v_us"] = 99
    assert _evaluate(before_start).ids == ()

    displaced = copy.deepcopy(value)
    displaced["resources"][1]["governance"]["displaced"] = True
    assert _evaluate(displaced).ids == ()

    corrected = copy.deepcopy(value)
    corrected["resources"][1]["governance"]["disposition"] = "corrected"
    assert _evaluate(corrected).ids == ()


def test_all_supporting_evidence_not_any_and_effective_sensitivity() -> None:
    first = _evidence("q1-record-evidence-000001")
    second = _evidence("q1-record-evidence-000002", label="")
    governed = _governed(
        supports=["q1-record-evidence-000001", "q1-record-evidence-000002"]
    )
    value = _input("memory.search", resources=[first, second, governed])
    assert _evaluate(value).ids == ()

    second["label_events"], second["label_events_digest"] = _label_events(
        ("acl.allow.principal.reader_internal", "attached")
    )
    assert _evaluate(value).ids == ("q1-record-memory-000001",)

    second["sensitivity"] = "confidential"
    assert _evaluate(value).ids == ()


def test_governed_labels_only_narrow_inherited_access() -> None:
    evidence = _evidence()
    denied = _governed(label="acl.deny.principal.reader_internal")
    assert _evaluate(_input("memory.search", resources=[evidence, denied])).ids == ()

    invented_allow = _governed(label="acl.allow.principal.restricted")
    assert (
        _evaluate(_input("memory.search", resources=[evidence, invented_allow])).ids
        == ()
    )

    inherited_allow = _governed(label="acl.allow.principal.reader_internal")
    assert _evaluate(
        _input("memory.search", resources=[evidence, inherited_allow])
    ).ids == ("q1-record-memory-000001",)


def test_relation_requires_own_version_and_both_endpoint_versions() -> None:
    evidence = _evidence()
    left = _governed("q1-record-knowledge-000001", kind="knowledge")
    right = _governed("q1-record-knowledge-000002", kind="knowledge")
    relation = _governed("q1-record-knowledge-000003", kind="relation")
    relation["endpoint_ids"] = [
        "q1-record-knowledge-000001",
        "q1-record-knowledge-000002",
    ]
    value = _input("knowledge.search", resources=[evidence, left, right, relation])
    assert _evaluate(value).ids == (
        "q1-record-knowledge-000001",
        "q1-record-knowledge-000002",
        "q1-record-knowledge-000003",
    )

    right["label_events"], right["label_events_digest"] = _label_events(
        ("acl.deny.principal.reader_internal", "attached")
    )
    assert _evaluate(value).ids == ("q1-record-knowledge-000001",)

    relation["endpoint_ids"] = [
        "q1-record-knowledge-000001",
        "q1-record-knowledge-999999",
    ]
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(value)


def test_duplicate_ids_missing_support_and_content_field_are_errors() -> None:
    duplicate = _evidence()
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[duplicate, copy.deepcopy(duplicate)]))

    governed = _governed(supports=["q1-record-evidence-999999"])
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input("memory.search", resources=[governed]))

    content = _evidence()
    content["content"] = "must not enter the pre-frontier evaluator"
    with pytest.raises(policy.PolicyEvaluationError):
        _evaluate(_input(resources=[content]))
