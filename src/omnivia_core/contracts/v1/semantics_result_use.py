"""Deterministic result-use evaluation (SPEC-CORE-DATA-001 §13.3, T-0716).

The one shared evaluator: `decision.result_use.evaluate` renders it, and a later
admission, publication or retrieval checkpoint calls it directly with inputs it
derived itself. A pure, standard-library-only classifier: given the declared
facts of one pinned analytical subject, the requested use class and a trusted
evaluation instant, it returns the `allow | allow_with_warning | deny` outcome
with every applicable reason. It never touches storage, credentials, workers or
the network, and its outcome is evidence bound to that one instant -- never an
effect grant and never a reusable one. The public operation evaluates
caller-supplied claims, so its answer is not authority for anything.

Decoding is strict and ordered:

1. the trusted `evaluation_instant` must be a timezone-aware `datetime`; anything
   else is a programmer error (`TypeError`), raised before any request data is
   read;
2. the document must be a JSON object whose `request_version` is a canonical
   contract version, else `invalid_request`; a major other than 1 is
   `incompatible_version` and 1.x other than 1.0 is
   `unsupported_minor_version`, decided before the body is examined;
3. a 1.0 body carries exactly the frozen field set, exact vocabulary values,
   real booleans and canonical identifiers, else `invalid_request`.

Default denials are the point: unknown completeness or continuity, a detected
gap, an incompatible schema, unavailable evidence, any partial or stale
certified use and every `action_input` deny; a partial or stale exploration or
historical display only warns when the controlling policy explicitly permits
it; exploration never certifies.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from .generated import (
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
    is_contract_version,
    is_identifier,
)

OUTCOME_ALLOW: Final = "allow"
OUTCOME_ALLOW_WITH_WARNING: Final = "allow_with_warning"
OUTCOME_DENY: Final = "deny"

USE_EXPLORATION: Final = "exploration"
USE_HISTORICAL_DISPLAY: Final = "historical_display"
USE_CURRENT_PUBLICATION: Final = "current_publication"
USE_ACTION_INPUT: Final = "action_input"

# Tuples rather than sets: membership of an unhashable JSON value (an array or
# an object) must answer False, not raise.
_USE_CLASSES: Final = (
    USE_EXPLORATION,
    USE_HISTORICAL_DISPLAY,
    USE_CURRENT_PUBLICATION,
    USE_ACTION_INPUT,
)
_COMPLETENESS: Final = ("complete", "partial", "unknown")
_CONTINUITY: Final = ("verified", "gap_detected", "unknown", "not_applicable")
_FLAGS: Final = (
    "freshness_ok",
    "schema_compatible",
    "evidence_available",
    "policy_permits_partial_or_stale",
)
_FIELDS: Final = frozenset(
    {
        "request_version",
        "use_class",
        "subject_digest",
        "completeness",
        "continuity",
        "authority_epoch",
        *_FLAGS,
    }
)

#: The reasons that only warn. Every other reason denies.
_WARNINGS: Final = frozenset(
    {
        "completeness_partial_permitted",
        "freshness_stale_display",
        "freshness_stale_exploration",
        "exploration_non_certifying",
    }
)


class ResultUseRequestError(ValueError):
    """A refused result-use request, carrying only its frozen contract error code.

    The code is `invalid_request`, `incompatible_version` or
    `unsupported_minor_version`. No caller value reaches the message or the
    representation: `str()` is the code and `repr()` is
    `ResultUseRequestError('<code>')`.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def evaluate_result_use(
    document: object, *, evaluation_instant: datetime
) -> dict[str, Any]:
    """Evaluate one `decision.result_use.evaluate` request at one trusted instant.

    Returns the bare `ResultUseEvaluateResult` mapping, whose `valid_until` is
    `evaluation_instant` itself in canonical UTC. Reasons follow the fixed
    category order -- completeness, continuity, freshness, schema, evidence,
    action policy, exploration label -- so `allow` always has none and every
    warning or denial names at least one. The document is never mutated.
    """
    if (
        not isinstance(evaluation_instant, datetime)
        or evaluation_instant.utcoffset() is None
    ):
        raise TypeError("evaluation_instant must be a timezone-aware datetime")
    valid_until = evaluation_instant.astimezone(UTC).isoformat().replace("+00:00", "Z")

    if not isinstance(document, Mapping):
        raise ResultUseRequestError(ERROR_CODE_INVALID_REQUEST)
    version = document.get("request_version")
    if not is_contract_version(version):
        raise ResultUseRequestError(ERROR_CODE_INVALID_REQUEST)
    assert isinstance(version, str)
    major, minor = version.split(".")
    if major != "1":
        raise ResultUseRequestError(ERROR_CODE_INCOMPATIBLE_VERSION)
    if minor != "0":
        raise ResultUseRequestError(ERROR_CODE_UNSUPPORTED_MINOR_VERSION)
    if (
        set(document) != _FIELDS
        or document["use_class"] not in _USE_CLASSES
        or document["completeness"] not in _COMPLETENESS
        or document["continuity"] not in _CONTINUITY
        or not is_identifier(document["subject_digest"])
        or not is_identifier(document["authority_epoch"])
        or any(type(document[name]) is not bool for name in _FLAGS)
    ):
        raise ResultUseRequestError(ERROR_CODE_INVALID_REQUEST)

    use_class = document["use_class"]
    permitted = document["policy_permits_partial_or_stale"]
    certified = use_class in (USE_CURRENT_PUBLICATION, USE_ACTION_INPUT)
    reasons: list[str] = []

    # Completeness: unknown denies every use; partial denies a certified use
    # outright, and any other use unless the controlling policy permits it.
    if document["completeness"] == "unknown":
        reasons.append("completeness_unknown")
    elif document["completeness"] == "partial":
        reasons.append(
            "completeness_partial_permitted"
            if permitted and not certified
            else "completeness_partial_not_permitted"
        )

    # Continuity: a gap or an unknown denies every use; not_applicable is neutral.
    if document["continuity"] == "gap_detected":
        reasons.append("continuity_gap_detected")
    elif document["continuity"] == "unknown":
        reasons.append("continuity_unknown")

    # Freshness: stale denies a certified use outright, and a display or an
    # exploration unless the controlling policy permits the stale class.
    if not document["freshness_ok"]:
        if certified:
            reasons.append("freshness_requirement_failed")
        elif not permitted:
            reasons.append("freshness_stale_not_permitted")
        elif use_class == USE_HISTORICAL_DISPLAY:
            reasons.append("freshness_stale_display")
        else:
            reasons.append("freshness_stale_exploration")

    if not document["schema_compatible"]:
        reasons.append("schema_incompatible")
    if not document["evidence_available"]:
        reasons.append("evidence_unavailable")

    # Action input is denied until an accepted action policy exists (D-0028),
    # and exploration never certifies, so even a perfect request only warns.
    if use_class == USE_ACTION_INPUT:
        reasons.append("action_input_policy_missing")
    if use_class == USE_EXPLORATION:
        reasons.append("exploration_non_certifying")

    if not reasons:
        outcome = OUTCOME_ALLOW
    elif _WARNINGS.issuperset(reasons):
        outcome = OUTCOME_ALLOW_WITH_WARNING
    else:
        outcome = OUTCOME_DENY
    return {
        "outcome": outcome,
        "reasons": reasons,
        "subject_digest": document["subject_digest"],
        "authority_epoch": document["authority_epoch"],
        "valid_until": valid_until,
    }
