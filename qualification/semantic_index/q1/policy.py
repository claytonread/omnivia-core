"""Frozen, synthetic Q1 authorization policy and exhaustive frontier evaluator.

This module is qualification-only authority. It does not define a production
policy model and intentionally has no dependency on an engine, adapter, product
package, or workspace database.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


def _load_sibling(module_name: str, file_name: str) -> Any:
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).with_name(file_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {file_name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


canonical = _load_sibling("q1_canonical", "canonical.py")

SCHEMA_ID = "omnivia.semantic-index.fixture-policy.v1"
POLICY_INPUT_SCHEMA_ID = (
    "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/"
    "policy-input.schema.json"
)
POLICY_SET_ID = "q1.synthetic.current-canonical.v1"
WORKSPACE_ID = "workspace.alpha"
REQUIRED_SCOPE = "memory:read"
PURPOSE = "user_initiated"
MINIMUM_CAPABILITY_VERSION = "1.0"
TOMBSTONE_RULE_VERSION = "q1.evidence-tombstone.v1"

OPERATIONS: tuple[str, ...] = (
    "evidence.search",
    "knowledge.search",
    "memory.search",
)
CAPABILITY_MAP: dict[str, str] = {
    "evidence.search": "evidence.read",
    "knowledge.search": "knowledge.read",
    "memory.search": "memory.read",
}
OPERATION_CATALOGUE: dict[str, Any] = {
    "operations": list(OPERATIONS),
    "capability_map": CAPABILITY_MAP,
}
PRINCIPAL_SUFFIXES: dict[str, str] = {
    "other_workspace": "principal.other_workspace",
    "reader_internal": "principal.reader_internal",
    "reader_public": "principal.reader_public",
    "restricted": "principal.restricted",
    "reviewer_confidential": "principal.reviewer_confidential",
}
ROLE_SUFFIXES: tuple[str, ...] = ("reader", "reviewer")
SUBJECT_CATALOGUE: dict[str, dict[str, Any]] = {
    "principal.reader_public": {
        "roles": ("reader",),
        "workspace_id": "workspace.alpha",
        "clearance": "public",
    },
    "principal.reader_internal": {
        "roles": ("reader",),
        "workspace_id": "workspace.alpha",
        "clearance": "internal",
    },
    "principal.reviewer_confidential": {
        "roles": ("reader", "reviewer"),
        "workspace_id": "workspace.alpha",
        "clearance": "confidential",
    },
    "principal.restricted": {
        "roles": ("reader",),
        "workspace_id": "workspace.alpha",
        "clearance": "restricted",
    },
    "principal.other_workspace": {
        "roles": ("reader",),
        "workspace_id": "workspace.beta",
        "clearance": "restricted",
    },
}
CLEARANCE_LEVELS: tuple[str, ...] = (
    "public",
    "internal",
    "confidential",
    "restricted",
)
_HASH_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXTERNAL_ID = re.compile(r"^q1-record-(?:evidence|memory|knowledge)-[0-9]{6}$")


class PolicyEvaluationError(ValueError):
    """The supplied policy input cannot produce any frontier object."""


def _condition(operator: str, **operands: Any) -> dict[str, Any]:
    return {"operator": operator, "operands": operands}


def _build_rule(operation: str, effect: str, kind: str, suffix: str) -> dict[str, Any]:
    label = f"acl.{effect}.{kind}.{suffix}"
    if kind == "principal":
        subject = _condition("principal_eq", principal_id=PRINCIPAL_SUFFIXES[suffix])
    else:
        subject = _condition("role_any", role_ids=[suffix])
    conditions = [
        subject,
        _condition("workspace_eq", workspace_id=WORKSPACE_ID),
        _condition("scope_all", scopes=[REQUIRED_SCOPE]),
        _condition("purpose_in", purposes=[PURPOSE]),
        _condition(
            "capability_at_least",
            capability_id=CAPABILITY_MAP[operation],
            minimum_version=MINIMUM_CAPABILITY_VERSION,
        ),
        _condition("permission_label_active", label=label),
        _condition("clearance_at_least"),
        _condition("recorded_at_or_before"),
    ]
    if operation != "evidence.search":
        conditions.extend(
            (
                _condition("valid_at"),
                _condition(
                    "governance_eq",
                    layer="governed",
                    disposition="accepted",
                    displaced=False,
                ),
            )
        )
    return {
        "rule_id": f"q1.{operation}.{effect}.{kind}.{suffix}",
        "effect": effect,
        "operation": operation,
        "all": conditions,
    }


def _expected_rules() -> tuple[dict[str, Any], ...]:
    rules = []
    for operation in OPERATIONS:
        for effect in ("allow", "deny"):
            for suffix in PRINCIPAL_SUFFIXES:
                rules.append(_build_rule(operation, effect, "principal", suffix))
            for suffix in ROLE_SUFFIXES:
                rules.append(_build_rule(operation, effect, "role", suffix))
    return tuple(
        sorted(rules, key=lambda item: cast(str, item["rule_id"]).encode("utf-8"))
    )


RULES: tuple[dict[str, Any], ...] = _expected_rules()
POLICY_DIGEST: str = canonical.canonical_sha256_ref(list(RULES))
OPERATION_CATALOGUE_DIGEST: str = canonical.canonical_sha256_ref(OPERATION_CATALOGUE)


def validate_rules(rules: Sequence[Mapping[str, Any]]) -> None:
    """Reject any shape, ordering, operator, operand, or value mutation."""
    if list(rules) != list(_expected_rules()):
        raise PolicyEvaluationError(
            "policy rule inventory is not the exact frozen 42-rule set"
        )


@dataclass(frozen=True)
class FrontierDecision:
    operation: str
    ids: tuple[str, ...]
    frontier_digest: str


@dataclass(frozen=True)
class _Context:
    principal_id: str
    role_ids: tuple[str, ...]
    grant_workspace_id: str
    grant_scopes: frozenset[str]
    capability_versions: Mapping[str, str]
    clearance: str
    request_workspace_id: str
    request_scopes: frozenset[str]
    purpose: str
    v_us: int
    t_us: int
    policy_epoch: int


@dataclass(frozen=True)
class _ResourceState:
    record_id: str
    workspace_id: str
    active_labels: frozenset[str]
    sensitivity: str
    recorded_at_us: int
    valid_from_us: int | None = None
    valid_to_us: int | None = None
    governance: Mapping[str, Any] | None = None


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyEvaluationError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise PolicyEvaluationError(f"{field} contains a non-string key")
    return cast(Mapping[str, Any], value)


def _exact_keys(value: Mapping[str, Any], required: set[str], field: str) -> None:
    actual = set(value)
    if actual != required:
        raise PolicyEvaluationError(
            f"{field} fields mismatch: missing={sorted(required - actual)} "
            f"unknown={sorted(actual - required)}"
        )


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyEvaluationError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PolicyEvaluationError(f"{field} must be an integer")
    if value < (1 if positive else 0):
        raise PolicyEvaluationError(f"{field} is outside its admitted range")
    return value


def _string_list(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PolicyEvaluationError(f"{field} must be a list of non-empty strings")
    result = cast(list[str], value)
    if nonempty and not result:
        raise PolicyEvaluationError(f"{field} must not be empty")
    if result != sorted(set(result), key=lambda item: item.encode("utf-8")):
        raise PolicyEvaluationError(f"{field} must be sorted and unique")
    return result


def _hash_ref(value: Any, field: str) -> str:
    result = _string(value, field)
    if _HASH_REF.fullmatch(result) is None:
        raise PolicyEvaluationError(f"{field} is not a canonical SHA-256 reference")
    return result


def _external_id(value: Any, field: str) -> str:
    result = _string(value, field)
    if _EXTERNAL_ID.fullmatch(result) is None:
        raise PolicyEvaluationError(f"{field} is not a fixed-width Q1 external ID")
    return result


def _external_id_list(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    result = _string_list(value, field, nonempty=nonempty)
    for index, item in enumerate(result):
        _external_id(item, f"{field}[{index}]")
    return result


def _canonical_ref(value: Any, field: str) -> str:
    try:
        return cast(str, canonical.canonical_sha256_ref(value))
    except (TypeError, ValueError) as exc:
        raise PolicyEvaluationError(f"{field} is not canonically hashable") from exc


def _version(value: Any, field: str) -> tuple[int, int]:
    text = _string(value, field)
    parts = text.split(".")
    if len(parts) != 2 or any(
        not part.isascii() or not part.isdigit() for part in parts
    ):
        raise PolicyEvaluationError(f"{field} is not canonical major.minor")
    if any(len(part) > 1 and part.startswith("0") for part in parts):
        raise PolicyEvaluationError(f"{field} contains a leading zero")
    return int(parts[0]), int(parts[1])


def _parse_context(
    raw: Mapping[str, Any],
) -> tuple[str, _Context, list[Mapping[str, Any]]]:
    required = {
        "schema_id",
        "schema_version",
        "noncanonical",
        "policy_input_id",
        "fixture_policy_schema_id",
        "policy_set_id",
        "policy_epoch",
        "expected_policy_epoch",
        "policy_digest",
        "source_digests",
        "operation",
        "authority",
        "request",
        "resources",
    }
    _exact_keys(raw, required, "policy_input")
    if (
        raw["schema_id"] != POLICY_INPUT_SCHEMA_ID
        or raw["schema_version"] != "1.0"
        or raw["noncanonical"] is not True
        or raw["fixture_policy_schema_id"] != SCHEMA_ID
        or raw["policy_set_id"] != POLICY_SET_ID
    ):
        raise PolicyEvaluationError(
            "unknown policy-input schema, fixture policy, or set"
        )
    _hash_ref(raw["policy_input_id"], "policy_input_id")
    if raw["policy_digest"] != POLICY_DIGEST:
        raise PolicyEvaluationError("policy digest mismatch")
    epoch = _integer(raw["policy_epoch"], "policy_epoch")
    expected_epoch = _integer(raw["expected_policy_epoch"], "expected_policy_epoch")
    if epoch != expected_epoch:
        raise PolicyEvaluationError("stale policy epoch")
    source_digests = _as_mapping(raw["source_digests"], "source_digests")
    _exact_keys(
        source_digests,
        {"authority", "operation_catalogue", "resources"},
        "source_digests",
    )
    for name, digest in source_digests.items():
        _hash_ref(digest, f"source_digests.{name}")
    expected_source_digests = {
        "authority": _canonical_ref(raw["authority"], "authority"),
        "operation_catalogue": OPERATION_CATALOGUE_DIGEST,
        "resources": _canonical_ref(raw["resources"], "resources"),
    }
    if dict(source_digests) != expected_source_digests:
        raise PolicyEvaluationError(
            "source_digests do not bind the complete authority, operation catalogue, "
            "and resource values"
        )

    operation = _string(raw["operation"], "operation")
    if operation not in OPERATIONS:
        raise PolicyEvaluationError(f"unknown operation: {operation!r}")

    authority = _as_mapping(raw["authority"], "authority")
    _exact_keys(
        authority,
        {
            "principal_id",
            "role_ids",
            "workspace_grant",
            "effective_capabilities",
            "clearance",
        },
        "authority",
    )
    principal_id = _string(authority["principal_id"], "authority.principal_id")
    if principal_id not in SUBJECT_CATALOGUE:
        raise PolicyEvaluationError("unknown synthetic principal")
    expected_subject = SUBJECT_CATALOGUE[principal_id]
    role_ids = tuple(_string_list(authority["role_ids"], "authority.role_ids"))
    if role_ids != expected_subject["roles"]:
        raise PolicyEvaluationError(
            "authority roles do not match the synthetic catalogue"
        )
    clearance = _string(authority["clearance"], "authority.clearance")
    if clearance != expected_subject["clearance"]:
        raise PolicyEvaluationError(
            "authority clearance does not match the synthetic catalogue"
        )

    grant = _as_mapping(authority["workspace_grant"], "authority.workspace_grant")
    _exact_keys(grant, {"workspace_id", "scopes"}, "authority.workspace_grant")
    grant_workspace = _string(
        grant["workspace_id"], "authority.workspace_grant.workspace_id"
    )
    if grant_workspace != expected_subject["workspace_id"]:
        raise PolicyEvaluationError(
            "workspace grant does not match the synthetic catalogue"
        )
    grant_scopes = frozenset(
        _string_list(grant["scopes"], "authority.workspace_grant.scopes")
    )

    capabilities_raw = authority["effective_capabilities"]
    if not isinstance(capabilities_raw, list):
        raise PolicyEvaluationError("authority.effective_capabilities must be a list")
    capabilities: dict[str, str] = {}
    ordered_capabilities: list[str] = []
    for index, item in enumerate(capabilities_raw):
        capability = _as_mapping(item, f"authority.effective_capabilities[{index}]")
        _exact_keys(
            capability,
            {"capability_id", "version"},
            f"authority.effective_capabilities[{index}]",
        )
        capability_id = _string(capability["capability_id"], "capability_id")
        version = _string(capability["version"], "capability.version")
        _version(version, "capability.version")
        if capability_id in capabilities:
            raise PolicyEvaluationError("duplicate effective capability")
        capabilities[capability_id] = version
        ordered_capabilities.append(capability_id)
    if ordered_capabilities != sorted(
        ordered_capabilities, key=lambda item: item.encode("utf-8")
    ):
        raise PolicyEvaluationError(
            "effective capabilities must be sorted by capability_id"
        )

    request = _as_mapping(raw["request"], "request")
    _exact_keys(
        request,
        {"request_id", "workspace_id", "scopes", "purpose", "v_us", "t_us"},
        "request",
    )
    _hash_ref(request["request_id"], "request.request_id")
    request_workspace = _string(request["workspace_id"], "request.workspace_id")
    request_scopes = frozenset(_string_list(request["scopes"], "request.scopes"))
    purpose = _string(request["purpose"], "request.purpose")
    if purpose != PURPOSE:
        raise PolicyEvaluationError("unknown or unadmitted purpose")
    v_us = _integer(request["v_us"], "request.v_us")
    t_us = _integer(request["t_us"], "request.t_us")

    resources = raw["resources"]
    if not isinstance(resources, list):
        raise PolicyEvaluationError("resources must be a list")
    resource_list = [
        _as_mapping(item, f"resources[{index}]") for index, item in enumerate(resources)
    ]
    context = _Context(
        principal_id=principal_id,
        role_ids=role_ids,
        grant_workspace_id=grant_workspace,
        grant_scopes=grant_scopes,
        capability_versions=capabilities,
        clearance=clearance,
        request_workspace_id=request_workspace,
        request_scopes=request_scopes,
        purpose=purpose,
        v_us=v_us,
        t_us=t_us,
        policy_epoch=epoch,
    )
    return operation, context, resource_list


def _ordered_events(
    value: Any, field: str, required_keys: set[str]
) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise PolicyEvaluationError(f"{field} must be a list")
    events = []
    event_ids = set()
    previous = 0
    for index, item in enumerate(value):
        event = _as_mapping(item, f"{field}[{index}]")
        _exact_keys(event, required_keys, f"{field}[{index}]")
        event_id = _string(event["event_id"], f"{field}[{index}].event_id")
        sequence = _integer(
            event["sequence"], f"{field}[{index}].sequence", positive=True
        )
        if event_id in event_ids or sequence <= previous:
            raise PolicyEvaluationError(
                f"{field} has duplicate or reordered identity/sequence"
            )
        event_ids.add(event_id)
        previous = sequence
        events.append(event)
    return events


def _verify_stream_digest(
    events: Sequence[Mapping[str, Any]], supplied: Any, field: str
) -> None:
    digest = _hash_ref(supplied, field)
    if digest != canonical.canonical_sha256_ref(list(events)):
        raise PolicyEvaluationError(
            f"{field} does not bind the complete ordered stream"
        )


def _known_label(label: str) -> bool:
    parts = label.split(".")
    if len(parts) != 4 or parts[0] != "acl" or parts[1] not in {"allow", "deny"}:
        return False
    if parts[2] == "principal":
        return parts[3] in PRINCIPAL_SUFFIXES
    if parts[2] == "role":
        return parts[3] in ROLE_SUFFIXES
    return False


def _resolve_labels(resource: Mapping[str, Any]) -> frozenset[str]:
    events = _ordered_events(
        resource["label_events"],
        "label_events",
        {"event_id", "sequence", "label", "action"},
    )
    _verify_stream_digest(
        events, resource["label_events_digest"], "label_events_digest"
    )
    active: dict[str, bool] = {}
    for event in events:
        label = _string(event["label"], "label_events.label")
        action = _string(event["action"], "label_events.action")
        if action == "corrected":
            raise PolicyEvaluationError("M2 corrected has no Q1 normalized meaning")
        if action not in {"attached", "withdrawn"}:
            raise PolicyEvaluationError(f"unknown M2 label action: {action!r}")
        active[label] = action == "attached"
    labels = frozenset(label for label, is_active in active.items() if is_active)
    if any(not _known_label(label) for label in labels):
        raise PolicyEvaluationError(
            "an unknown or malformed permission label remains active"
        )
    return labels


def _is_tombstoned(resource: Mapping[str, Any]) -> bool:
    events = _ordered_events(
        resource["provenance_events"],
        "provenance_events",
        {"event_id", "sequence", "action", "tombstoned_observation"},
    )
    _verify_stream_digest(
        events, resource["provenance_events_digest"], "provenance_events_digest"
    )
    tombstoned = False
    for event in events:
        action = _string(event["action"], "provenance_events.action")
        observation = event["tombstoned_observation"]
        if action not in {"created", "restored", "tombstoned"}:
            raise PolicyEvaluationError(f"unknown provenance action: {action!r}")
        if action == "tombstoned":
            if observation != 1 or isinstance(observation, bool):
                raise PolicyEvaluationError(
                    "tombstoned action requires observation integer 1"
                )
            tombstoned = True
        elif observation is not None:
            raise PolicyEvaluationError(
                "tombstone observation is invalid under another action"
            )
    return tombstoned


_EVIDENCE_KEYS = {
    "record_id",
    "kind",
    "workspace_id",
    "sensitivity",
    "recorded_at_us",
    "label_events",
    "label_events_digest",
    "provenance_events",
    "provenance_events_digest",
}
_GOVERNED_KEYS = {
    "record_id",
    "kind",
    "workspace_id",
    "sensitivity",
    "recorded_at_us",
    "label_events",
    "label_events_digest",
    "valid_from_us",
    "valid_to_us",
    "governance",
    "supporting_evidence_ids",
}
_RELATION_KEYS = _GOVERNED_KEYS | {"endpoint_ids"}


def _base_state(resource: Mapping[str, Any], labels: frozenset[str]) -> _ResourceState:
    sensitivity = _string(resource["sensitivity"], "resource.sensitivity")
    if sensitivity not in CLEARANCE_LEVELS:
        raise PolicyEvaluationError("unknown resource sensitivity")
    return _ResourceState(
        record_id=_external_id(resource["record_id"], "resource.record_id"),
        workspace_id=_string(resource["workspace_id"], "resource.workspace_id"),
        active_labels=labels,
        sensitivity=sensitivity,
        recorded_at_us=_integer(resource["recorded_at_us"], "resource.recorded_at_us"),
    )


def _evidence_state(
    resource: Mapping[str, Any], context: _Context
) -> _ResourceState | None:
    _exact_keys(resource, _EVIDENCE_KEYS, "evidence resource")
    if resource["kind"] != "evidence":
        raise PolicyEvaluationError("evidence resource has the wrong kind")
    labels = _resolve_labels(resource)
    state = _base_state(resource, labels)
    if _is_tombstoned(resource):
        return None
    if state.recorded_at_us > context.t_us:
        return None
    return state


def _governed_state(resource: Mapping[str, Any]) -> _ResourceState:
    labels = _resolve_labels(resource)
    state = _base_state(resource, labels)
    valid_from = _integer(resource["valid_from_us"], "resource.valid_from_us")
    valid_to_raw = resource["valid_to_us"]
    valid_to = (
        None if valid_to_raw is None else _integer(valid_to_raw, "resource.valid_to_us")
    )
    if valid_to is not None and valid_to <= valid_from:
        raise PolicyEvaluationError(
            "resource valid interval is not half-open and increasing"
        )
    governance = _as_mapping(resource["governance"], "resource.governance")
    _exact_keys(
        governance, {"layer", "disposition", "displaced"}, "resource.governance"
    )
    if governance["layer"] not in {"governed", "staged"}:
        raise PolicyEvaluationError("unknown governance layer")
    if governance["disposition"] not in {"accepted", "corrected", "rejected"}:
        raise PolicyEvaluationError("unknown governance disposition")
    if not isinstance(governance["displaced"], bool):
        raise PolicyEvaluationError("governance displaced must be boolean")
    return _ResourceState(
        record_id=state.record_id,
        workspace_id=state.workspace_id,
        active_labels=state.active_labels,
        sensitivity=state.sensitivity,
        recorded_at_us=state.recorded_at_us,
        valid_from_us=valid_from,
        valid_to_us=valid_to,
        governance=governance,
    )


def _same_major_at_least(effective: str, minimum: str) -> bool:
    effective_major, effective_minor = _version(
        effective, "effective capability version"
    )
    minimum_major, minimum_minor = _version(minimum, "minimum capability version")
    return effective_major == minimum_major and effective_minor >= minimum_minor


def _condition_matches(
    condition: Mapping[str, Any], state: _ResourceState, context: _Context
) -> bool:
    operator = condition["operator"]
    operands = cast(Mapping[str, Any], condition["operands"])
    if operator == "principal_eq":
        return context.principal_id == cast(str, operands["principal_id"])
    if operator == "role_any":
        return bool(set(context.role_ids) & set(cast(list[str], operands["role_ids"])))
    if operator == "workspace_eq":
        expected = cast(str, operands["workspace_id"])
        return (
            context.grant_workspace_id
            == context.request_workspace_id
            == state.workspace_id
            == expected
        )
    if operator == "scope_all":
        effective = context.grant_scopes & context.request_scopes & {REQUIRED_SCOPE}
        return set(cast(list[str], operands["scopes"])).issubset(effective)
    if operator == "purpose_in":
        return context.purpose in operands["purposes"]
    if operator == "capability_at_least":
        capability_id = cast(str, operands["capability_id"])
        effective_version = context.capability_versions.get(capability_id)
        return effective_version is not None and _same_major_at_least(
            effective_version, cast(str, operands["minimum_version"])
        )
    if operator == "permission_label_active":
        return operands["label"] in state.active_labels
    if operator == "clearance_at_least":
        return CLEARANCE_LEVELS.index(state.sensitivity) <= CLEARANCE_LEVELS.index(
            context.clearance
        )
    if operator == "recorded_at_or_before":
        return state.recorded_at_us <= context.t_us
    if operator == "valid_at":
        return (
            state.valid_from_us is not None
            and state.valid_from_us <= context.v_us
            and (state.valid_to_us is None or context.v_us < state.valid_to_us)
        )
    if operator == "governance_eq":
        return state.governance is not None and all(
            state.governance.get(name) == value for name, value in operands.items()
        )
    raise PolicyEvaluationError(f"unknown condition operator: {operator!r}")


def _rule_decision(
    operation: str,
    state: _ResourceState,
    context: _Context,
    rules: Sequence[Mapping[str, Any]],
) -> bool:
    effects = {
        rule["effect"]
        for rule in rules
        if rule["operation"] == operation
        and all(
            _condition_matches(condition, state, context)
            for condition in cast(list[Mapping[str, Any]], rule["all"])
        )
    }
    return "deny" not in effects and "allow" in effects


def _matching_effect(
    operation: str,
    effect: str,
    labels: frozenset[str],
    state: _ResourceState,
    context: _Context,
    rules: Sequence[Mapping[str, Any]],
) -> bool:
    narrowed = _ResourceState(
        record_id=state.record_id,
        workspace_id=state.workspace_id,
        active_labels=labels,
        sensitivity=state.sensitivity,
        recorded_at_us=state.recorded_at_us,
        valid_from_us=state.valid_from_us,
        valid_to_us=state.valid_to_us,
        governance=state.governance,
    )
    return any(
        rule["effect"] == effect
        and rule["operation"] == operation
        and all(
            _condition_matches(condition, narrowed, context)
            for condition in cast(list[Mapping[str, Any]], rule["all"])
        )
        for rule in rules
    )


def _max_sensitivity(states: Sequence[_ResourceState]) -> str:
    return max(
        states, key=lambda state: CLEARANCE_LEVELS.index(state.sensitivity)
    ).sensitivity


def _authorize_governed(
    operation: str,
    resource: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    context: _Context,
    rules: Sequence[Mapping[str, Any]],
) -> tuple[bool, _ResourceState]:
    state = _governed_state(resource)
    if state.recorded_at_us > context.t_us:
        return False, state
    if state.valid_from_us is None or state.valid_from_us > context.v_us:
        return False, state
    if state.valid_to_us is not None and context.v_us >= state.valid_to_us:
        return False, state
    assert state.governance is not None
    if not (
        state.governance["layer"] == "governed"
        and state.governance["disposition"] == "accepted"
        and state.governance["displaced"] is False
    ):
        return False, state

    supporting_ids = _external_id_list(
        resource["supporting_evidence_ids"],
        "resource.supporting_evidence_ids",
        nonempty=True,
    )
    support_states = []
    for evidence_id in supporting_ids:
        support = evidence.get(evidence_id)
        if support is None:
            raise PolicyEvaluationError("supporting evidence identity is absent")
        support_state = _evidence_state(support, context)
        if support_state is None:
            return False, state
        support_states.append(support_state)

    effective_state = _ResourceState(
        record_id=state.record_id,
        workspace_id=state.workspace_id,
        active_labels=state.active_labels,
        sensitivity=_max_sensitivity([state, *support_states]),
        recorded_at_us=state.recorded_at_us,
        valid_from_us=state.valid_from_us,
        valid_to_us=state.valid_to_us,
        governance=state.governance,
    )
    for support_state in support_states:
        inherited_state = _ResourceState(
            record_id=effective_state.record_id,
            workspace_id=effective_state.workspace_id,
            active_labels=support_state.active_labels,
            sensitivity=effective_state.sensitivity,
            recorded_at_us=max(
                effective_state.recorded_at_us, support_state.recorded_at_us
            ),
            valid_from_us=effective_state.valid_from_us,
            valid_to_us=effective_state.valid_to_us,
            governance=effective_state.governance,
        )
        if not _rule_decision(operation, inherited_state, context, rules):
            return False, effective_state

    if _matching_effect(
        operation, "deny", state.active_labels, effective_state, context, rules
    ):
        return False, effective_state
    version_allows = frozenset(
        label for label in state.active_labels if label.startswith("acl.allow.")
    )
    if version_allows:
        common_support_labels = frozenset.intersection(
            *(support_state.active_labels for support_state in support_states)
        )
        admitted = version_allows & common_support_labels
        if not admitted or not _matching_effect(
            operation, "allow", admitted, effective_state, context, rules
        ):
            return False, effective_state
    return True, effective_state


def evaluate_frontier(
    policy_input: Mapping[str, Any],
    *,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> FrontierDecision:
    """Return a complete authorized ID frontier or raise before returning one."""
    selected_rules = RULES if rules is None else rules
    validate_rules(selected_rules)
    operation, context, resources = _parse_context(
        _as_mapping(policy_input, "policy_input")
    )

    by_id: dict[str, Mapping[str, Any]] = {}
    evidence: dict[str, Mapping[str, Any]] = {}
    for index, resource in enumerate(resources):
        record_id = _external_id(
            resource.get("record_id"), f"resources[{index}].record_id"
        )
        if record_id in by_id:
            raise PolicyEvaluationError("duplicate external record ID")
        kind = _string(resource.get("kind"), f"resources[{index}].kind")
        if kind == "evidence":
            _exact_keys(resource, _EVIDENCE_KEYS, f"resources[{index}]")
            evidence[record_id] = resource
        elif kind in {"memory", "knowledge"}:
            _exact_keys(resource, _GOVERNED_KEYS, f"resources[{index}]")
        elif kind == "relation":
            _exact_keys(resource, _RELATION_KEYS, f"resources[{index}]")
        else:
            raise PolicyEvaluationError(f"unknown resource kind: {kind!r}")
        by_id[record_id] = resource

    ids = []
    for record_id, resource in by_id.items():
        kind = cast(str, resource["kind"])
        if operation == "evidence.search":
            if kind != "evidence":
                continue
            state = _evidence_state(resource, context)
            if state is not None and _rule_decision(
                operation, state, context, selected_rules
            ):
                ids.append(record_id)
            continue

        expected_kind = "memory" if operation == "memory.search" else "knowledge"
        if kind not in {
            expected_kind,
            "relation" if operation == "knowledge.search" else "",
        }:
            continue
        allowed, _ = _authorize_governed(
            operation, resource, evidence, context, selected_rules
        )
        if not allowed:
            continue
        if kind == "relation":
            endpoint_ids = _external_id_list(
                resource["endpoint_ids"], "resource.endpoint_ids"
            )
            if len(endpoint_ids) != 2:
                raise PolicyEvaluationError(
                    "relation must name exactly two endpoint versions"
                )
            for endpoint_id in endpoint_ids:
                endpoint = by_id.get(endpoint_id)
                if endpoint is None:
                    raise PolicyEvaluationError("relation endpoint identity is absent")
                if endpoint.get("kind") not in {"memory", "knowledge"}:
                    raise PolicyEvaluationError(
                        "relation endpoint is not an exact governed version"
                    )
                endpoint_allowed, _ = _authorize_governed(
                    operation, endpoint, evidence, context, selected_rules
                )
                if not endpoint_allowed:
                    allowed = False
                    break
        if allowed:
            ids.append(record_id)

    ordered_ids = tuple(sorted(ids, key=lambda item: item.encode("utf-8")))
    digest = canonical.canonical_sha256_ref(
        {
            "operation": operation,
            "policy_epoch": context.policy_epoch,
            "authorized_external_ids": list(ordered_ids),
        }
    )
    return FrontierDecision(
        operation=operation,
        ids=ordered_ids,
        frontier_digest=digest,
    )
