"""The Decision Runtime core: definitions, policy composition and the
deterministic route (ADR-042, plan PR-3; spec §7, §8, §12.1).

This module is pure: it holds no connection, no session and no clock. Everything
that touches authoritative storage happens in `handlers.decisions` inside the
fenced mutation transaction; everything that authorises happens in the existing
authorization seam before this code runs. What lives here is the part of §8 that
is *decision logic* rather than service plumbing:

- definition document validation (§7.1): the shape a `decision.definition.publish`
  call may carry, canonicalised and digested so identity is content, not spelling;
- policy composition (§7.2): installation/workspace settings, the definition's own
  limits and the caller's constraints combine most-restrictive-wins, and the
  composition can only ever narrow;
- route resolution (§12.1): a conclusive deterministic rule records a result;
  a model route in this build is *unavailable* — recorded as a failed attempt
  with `model_not_installed`, never a silent fallback and never a fabrication;
- the deterministic engine itself: ordered rules over the caller's inline state,
  evaluated in order, first match wins, and an unconclusive evaluation abstains
  with `evidence_incomplete` rather than guessing.

A model probability never constitutes authority here: every disposition this
module produces states `authorises_action: false`, and the deterministic
denial path (§22, AT-49) cannot be overridden by any prediction.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core_runtime.storage.decisions import canonical_document, content_digest

SCHEMA_VERSION: Final = "decision.1"

_KINDS: Final = ("boolean", "choice", "ordinal")
_ROUTES: Final = ("deterministic", "model")
_RECIPE_MODES: Final = ("deterministic", "model")
_PROCESSING_STATES: Final = ("off", "advisory", "paused", "blocked")
_VERSION_RE: Final = re.compile(r"^\d+\.\d+\.\d+$")
_OPTION_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DEFINITION_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{0,126}[a-z0-9]$")
_MAX_OPTIONS: Final = 32
_MAX_RULES: Final = 64

#: The disposition codes the decision.1 contract's `DecisionDisposition` allows,
#: with the spec §23 reason codes that may accompany them.
DISPOSITION_ADVISORY_ONLY: Final = "advisory_only"
DISPOSITION_ABSTAINED: Final = "abstained"
DISPOSITION_FAILED: Final = "failed"
REASON_TASK_NOT_QUALIFIED: Final = "TASK_NOT_QUALIFIED_FOR_AUTOMATION"
REASON_EVIDENCE_INCOMPLETE: Final = "EVIDENCE_INCOMPLETE"
REASON_MODEL_NOT_INSTALLED: Final = "MODEL_NOT_INSTALLED"
REASON_DECISION_DISABLED: Final = "DECISION_DISABLED"
REASON_HOST_UNSUPPORTED: Final = "HOST_UNSUPPORTED"

#: The execution facts a deterministic route states. There is no provider,
#: no profile and no forward pass: the route ran inside the Core host.
DETERMINISTIC_EXECUTION: Final = {
    "provider_id": "deterministic_rules",
    "profile_id": "deterministic.v1",
    "execution_location": "core_host",
    "remote_processing_used": False,
    "provider_forward_passes": 0,
    "output_tokens": 0,
}

UNAVAILABLE_MODEL_EXECUTION: Final = {
    "provider_id": "unavailable",
    "profile_id": "unavailable",
    "execution_location": "core_host",
    "remote_processing_used": False,
    "provider_forward_passes": 0,
    "output_tokens": 0,
}


class DecisionDefinitionError(ValueError):
    """A definition document is not a valid decision.1 definition."""


class DecisionPolicyDenied(PermissionError):
    """Admission is refused by policy, before any evaluation work runs.

    `reason` is the spec §23 reason code the refusal carries; no sensitive
    context snapshot is materialised for a denial (§8).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_definition_document(document: Any) -> dict[str, Any]:
    """Validate one published definition document and return its normalised form.

    The shape is this build's definition v1: identity, title, decision kind, the
    ordered option rubric (§7.1: order is part of identity), and the recipe that
    routes it — deterministic rules or a model requirement. Everything is
    data-only (§6.3): no URLs, paths, expressions or tool names are executable
    inputs, and nothing here is interpreted as code.
    """
    if not isinstance(document, Mapping):
        raise DecisionDefinitionError("definition must be a JSON object")
    unknown = set(document) - {
        "id",
        "version",
        "title",
        "purpose",
        "kind",
        "options",
        "recipe",
        "required_sources",
        "min_source_count",
    }
    if unknown:
        raise DecisionDefinitionError(f"unknown definition fields: {sorted(unknown)}")
    definition_id = document.get("id")
    if not isinstance(definition_id, str) or not _DEFINITION_ID_RE.fullmatch(
        definition_id
    ):
        raise DecisionDefinitionError("definition.id is not a bounded identifier")
    version = document.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise DecisionDefinitionError("definition.version is not semantic")
    title = document.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= 256:
        raise DecisionDefinitionError("definition.title is not a bounded string")
    kind = document.get("kind")
    if kind not in _KINDS:
        raise DecisionDefinitionError("definition.kind is not a decision kind")
    purpose = document.get("purpose")
    if not isinstance(purpose, str) or not re.fullmatch(
        r"[a-z][a-z0-9_.]{0,126}[a-z0-9]", purpose
    ):
        raise DecisionDefinitionError("definition.purpose is not a bounded purpose")
    options = document.get("options")
    if kind == "boolean":
        if options is not None:
            raise DecisionDefinitionError("a boolean definition takes no options")
        options = [
            {"id": "true", "label": "true", "description": "true"},
            {"id": "false", "label": "false", "description": "false"},
        ]
    if not isinstance(options, (list, tuple)) or not 1 <= len(options) <= _MAX_OPTIONS:
        raise DecisionDefinitionError(
            "definition.options is not a bounded, ordered, non-empty list"
        )
    seen: set[str] = set()
    for option in options:
        if (
            not isinstance(option, Mapping)
            or not isinstance(option.get("id"), str)
            or not _OPTION_ID_RE.fullmatch(str(option["id"]))
            or not isinstance(option.get("label"), str)
            or not option.get("label")
        ):
            raise DecisionDefinitionError(
                "a definition option is not an {id, label, description} entry"
            )
        if option["id"] in seen:
            raise DecisionDefinitionError("duplicate option ids are refused")
        seen.add(str(option["id"]))
    options = [dict(option) for option in options]
    recipe = document.get("recipe")
    if not isinstance(recipe, Mapping) or recipe.get("mode") not in _RECIPE_MODES:
        raise DecisionDefinitionError("definition.recipe.mode is not a route mode")
    rules = recipe.get("rules")
    if recipe["mode"] == "deterministic":
        if kind == "boolean":
            raise DecisionDefinitionError(
                "a boolean definition cannot carry deterministic rules in this build"
            )
        if not isinstance(rules, (list, tuple)) or not 1 <= len(rules) <= _MAX_RULES:
            raise DecisionDefinitionError("deterministic rules are missing or too many")
        for rule in rules:
            if (
                not isinstance(rule, Mapping)
                or not isinstance(rule.get("when"), Mapping)
                or set(rule) - {"when", "option"}
                or not isinstance(rule.get("option"), str)
                or rule["option"] not in seen
            ):
                raise DecisionDefinitionError(
                    "a deterministic rule must name a state path and a published option"
                )
            when = dict(rule["when"])
            if (
                set(when) - {"key", "equals"}
                or not isinstance(when.get("key"), str)
                or not re.fullmatch(r"[a-z][a-z0-9_.]{0,126}", str(when["key"]))
                or "equals" not in when
            ):
                raise DecisionDefinitionError(
                    "a rule condition is not a bounded state path with an equals value"
                )
    else:
        if rules:
            raise DecisionDefinitionError("a model recipe takes no deterministic rules")
    required_sources = document.get("required_sources", 0)
    min_source_count = document.get("min_source_count", required_sources)
    if not isinstance(required_sources, int) or not 0 <= required_sources <= 16:
        raise DecisionDefinitionError("definition.required_sources is not bounded")
    if not isinstance(min_source_count, int) or min_source_count < required_sources:
        raise DecisionDefinitionError(
            "definition.min_source_count may not fall below required_sources"
        )
    normalised = {
        "id": definition_id,
        "version": version,
        "title": title,
        "purpose": purpose,
        "kind": kind,
        "options": options,
        "recipe": {"mode": recipe["mode"], "rules": rules or []},
        "required_sources": required_sources,
        "min_source_count": min_source_count,
    }
    normalised["digest"] = content_digest(canonical_document(normalised))
    return normalised


def compose_policy(
    *,
    processing_state: str,
    caller_deadline_ms: int,
    caller_attempts: int,
    required_sources: int,
) -> dict[str, Any]:
    """Combine the workspace settings and the caller's constraints, §7.2.

    Most restrictive wins: the processing state is the workspace's own (a caller
    cannot enable anything), the deadline can only shrink to the policy floor,
    and the attempt budget starts at the specification's default of one — a
    retry is a separately enabled policy this build does not enable, so the
    caller's ask can never raise it.
    """
    if processing_state not in _PROCESSING_STATES:
        raise DecisionPolicyDenied(REASON_DECISION_DISABLED)
    if processing_state != "advisory":
        raise DecisionPolicyDenied(REASON_DECISION_DISABLED)
    return {
        "mode": "advisory",
        "privacy": "local_only",
        "deadline_ms": min(caller_deadline_ms, 5000),
        "maximum_provider_attempts": min(caller_attempts, 1),
        "required_sources": required_sources,
    }


def _lookup(state: Mapping[str, Any], path: str) -> Any:
    """Resolve one dotted path over the inline state, without evaluation."""
    current: Any = state
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def evaluate_deterministic(
    definition: dict[str, Any],
    *,
    inline_state: Mapping[str, Any],
    resolved_sources: int,
) -> dict[str, Any]:
    """Run one deterministic route (§12.1) and return the terminal record body.

    First-match-wins over the definition's ordered rules; a rule matches when
    the state value at its path equals its value. No match, an exhausted
    rule set, or a source floor that was not met is an *abstention* — a normal
    product outcome (§11.4) — never a guess. The prediction a rule produces is
    deterministic and carries the contract's `deterministic_rule` probability
    semantics, so no model confidence is implied anywhere.
    """
    if resolved_sources < int(definition["min_source_count"]):
        return _abstention([REASON_EVIDENCE_INCOMPLETE])
    for rule in definition["recipe"]["rules"]:
        if _lookup(dict(inline_state), str(rule["when"]["key"])) == rule["when"][
            "equals"
        ]:
            return _prediction(definition, str(rule["option"]))
    return _abstention([REASON_EVIDENCE_INCOMPLETE])


def _prediction(definition: dict[str, Any], selected: str) -> dict[str, Any]:
    kind = definition["kind"]
    if kind == "ordinal":
        options = [str(option["id"]) for option in definition["options"]]
        index = options.index(selected)
        prediction = {
            "kind": "ordinal",
            "selected_option_id": selected,
            "expected_index": index,
            "normalised_position": (
                index / (len(options) - 1) if len(options) > 1 else 0.0
            ),
            "probability_semantics": "deterministic_rule",
            "provider_decimal_precision": 0,
        }
    else:
        distribution = {
            str(option["id"]): (1.0 if str(option["id"]) == selected else 0.0)
            for option in definition["options"]
        }
        prediction = {
            "kind": kind,
            "selected_option_id": selected,
            "probabilities": distribution,
            "probability_semantics": "deterministic_rule",
            "provider_decimal_precision": 1,
        }
    return {
        "prediction": prediction,
        "disposition": {
            "code": DISPOSITION_ADVISORY_ONLY,
            "reason_codes": [REASON_TASK_NOT_QUALIFIED],
            "authorises_action": False,
        },
        "quality": {
            "calibration_status": "deterministic_no_calibration",
            "input_complete": True,
        },
    }


def _abstention(reasons: list[str]) -> dict[str, Any]:
    return {
        "prediction": None,
        "disposition": {
            "code": DISPOSITION_ABSTAINED,
            "reason_codes": list(reasons),
            "authorises_action": False,
        },
        "quality": {
            "calibration_status": "unvalidated_for_task",
            "input_complete": False,
        },
        "abstention_reasons": list(reasons),
    }


def model_route_unavailable() -> dict[str, Any]:
    """The fail-closed body for a definition that requires a model (AT-36).

    There is no worker in this build: the route is recorded as a failed attempt
    with `model_not_installed`, and nothing downloads, calls out or fabricates.
    """
    return {
        "prediction": None,
        "disposition": {
            "code": DISPOSITION_FAILED,
            "reason_codes": [REASON_MODEL_NOT_INSTALLED],
            "authorises_action": False,
        },
        "quality": {
            "calibration_status": "unvalidated_for_task",
            "input_complete": True,
        },
    }
