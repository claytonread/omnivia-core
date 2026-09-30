"""Deterministic result-use evaluation (SPEC-CORE-DATA-001 §13.3, T-0716).

A pure, standard-library-only classifier: given the declared facts of one
pinned analytical subject and the requested use class, it returns the
`allow | allow_with_warning | deny` outcome with every applicable reason, in
the fixed order the specification's ten-step algorithm defines. It never
touches storage, credentials, workers or the network, and its outcome is an
eligibility statement — never an effect grant.

Default denials are the point: `unknown` completeness or continuity denies;
`action_input` denies until an action policy exists; a stale/partial class is
only ever allowed with a warning when the controlling policy explicitly
permits it.
"""

from __future__ import annotations

import re
from typing import Any, Final

OUTCOME_ALLOW: Final = "allow"
OUTCOME_ALLOW_WITH_WARNING: Final = "allow_with_warning"
OUTCOME_DENY: Final = "deny"

USE_EXPLORATION: Final = "exploration"
USE_HISTORICAL_DISPLAY: Final = "historical_display"
USE_CURRENT_PUBLICATION: Final = "current_publication"
USE_ACTION_INPUT: Final = "action_input"

_VERSION_PATTERN: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

_REQUIRED_FIELDS: Final = frozenset(
    {
        "request_version",
        "use_class",
        "subject_digest",
        "completeness",
        "continuity",
        "freshness_ok",
        "schema_compatible",
        "evidence_available",
        "policy_permits_partial_or_stale",
        "authority_epoch",
    }
)
_ALLOWED_FIELDS: Final = _REQUIRED_FIELDS


def _identifier_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and _IDENTIFIER_PATTERN.fullmatch(value) is not None
    )


def evaluate_result_use(document: Any) -> dict[str, Any]:
    """Evaluate one `decision.result_use.evaluate` request.

    Returns the decoded decision document (the wire shape of
    `ResultUseEvaluateResult`) or raises `ValueError` with a machine-readable
    reason when the request itself is malformed (`invalid_request` semantics
    belong to the handler).
    """
    if not isinstance(document, dict):
        raise TypeError("request payload must be a JSON object")
    if not set(document) <= _ALLOWED_FIELDS or not _REQUIRED_FIELDS <= set(document):
        raise ValueError("request payload does not match the v1 field set")
    version = document["request_version"]
    if not isinstance(version, str) or _VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("request_version is not a major.minor version")
    if version != "1.0":
        raise ValueError(f"payload version {version} is not supported")
    if not _identifier_ok(document["subject_digest"]) or not _identifier_ok(
        document["authority_epoch"]
    ):
        raise ValueError("subject_digest and authority_epoch must be identifiers")

    use_class = document["use_class"]
    completeness = document["completeness"]
    continuity = document["continuity"]
    reasons: list[str] = []

    if use_class not in (
        USE_EXPLORATION,
        USE_HISTORICAL_DISPLAY,
        USE_CURRENT_PUBLICATION,
        USE_ACTION_INPUT,
    ):
        raise ValueError("use_class is outside the contract vocabulary")

    # Step 5 of the algorithm: scope completeness. Unknown denies outright;
    # `partial` denies unless the controlling policy explicitly permits the
    # declared partial/stale class, and then only for non-certifying uses.
    if completeness == "unknown":
        reasons.append("completeness_unknown")
    elif completeness == "partial" and not document["policy_permits_partial_or_stale"]:
        reasons.append("completeness_partial_not_permitted")

    # Step 6: continuity. A detected gap denies every certified use; an
    # unknown continuity denies current/action use.
    if continuity == "gap_detected":
        reasons.append("continuity_gap_detected")
    elif continuity == "unknown" and use_class in (
        USE_CURRENT_PUBLICATION,
        USE_ACTION_INPUT,
    ):
        reasons.append("continuity_unknown")

    # Freshness: a failed freshness check denies current and action uses and
    # allows the historical class only with a warning.
    if not document["freshness_ok"] and use_class in (
        USE_CURRENT_PUBLICATION,
        USE_ACTION_INPUT,
    ):
        reasons.append("freshness_requirement_failed")
    if not document["freshness_ok"] and use_class == USE_HISTORICAL_DISPLAY:
        reasons.append("freshness_stale_display")

    # Schema compatibility: an incompatible mapping denies everything.
    if not document["schema_compatible"]:
        reasons.append("schema_incompatible")

    # Evidence availability: needed for every certified use; a warning only
    # for exploration/historical display when the policy permits the class.
    if not document["evidence_available"] and use_class in (
        USE_CURRENT_PUBLICATION,
        USE_ACTION_INPUT,
    ):
        reasons.append("evidence_unavailable")

    # Action input is denied until an accepted action policy exists (D-0028),
    # and the denial is stated even when every other fact is perfect.
    if use_class == USE_ACTION_INPUT:
        reasons.append("action_input_policy_missing")

    certified_use = use_class in (USE_CURRENT_PUBLICATION, USE_ACTION_INPUT)
    if reasons:
        blocking = tuple(reasons)
        if use_class == USE_HISTORICAL_DISPLAY and set(blocking) <= {
            "freshness_stale_display"
        }:
            outcome = OUTCOME_ALLOW_WITH_WARNING
        else:
            outcome = OUTCOME_DENY
    else:
        blocking = ()
        # Exploration never certifies: a perfect exploration request is at
        # most an allow-with-warning, never a silent current-use pass.
        outcome = (
            OUTCOME_ALLOW_WITH_WARNING
            if use_class == USE_EXPLORATION
            else OUTCOME_ALLOW
        )
        _ = certified_use
    return {
        "outcome": outcome,
        "reasons": list(blocking) or list(reasons),
        "subject_digest": document["subject_digest"],
        "authority_epoch": document["authority_epoch"],
        "valid_until": "evaluation-instant-bound",
    }
