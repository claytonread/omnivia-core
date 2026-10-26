"""The Chat Part-type registry: the shapes it closes, and the openness it keeps.

Two properties matter here and they pull in opposite directions, so both are
asserted rather than one being assumed from the other:

1. A REGISTERED family's payload is bounded. That is what gives a consumer
   something to decode against, and it is what closes the write path's
   previously unbounded passthrough for those families.
2. An UNREGISTERED family is still passed through untouched. `MessagePart.type`
   is an open code by contract and an unrecognised one decodes to an inert
   fallback, so a registry that refused unknown types would narrow the
   contract rather than implement it.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnivia_core_runtime.service.chat_attachment_authority import (
    ATTACHMENT_PART_TYPE,
    PinnedAttachment,
    attachment_part_payload,
)
from omnivia_core_runtime.service.chat_part_registry import (
    ACTIVITY_SERIES_PART_TYPE,
    ACTIVITY_SERIES_RESPONSE_SCHEMA_REF,
    CHART_PART_TYPE,
    CHART_RESPONSE_SCHEMA_REF,
    CODE_DIFF_PART_TYPE,
    CODE_DIFF_RESPONSE_SCHEMA_REF,
    COMPARISON_MATRIX_PART_TYPE,
    COMPARISON_MATRIX_RESPONSE_SCHEMA_REF,
    CORE_REPORT_PART_TYPE,
    DATA_TABLE_RESPONSE_SCHEMA_REF,
    DIAGRAM_FLOW_PART_TYPE,
    DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
    FILE_TREE_PART_TYPE,
    FILE_TREE_RESPONSE_SCHEMA_REF,
    GENERATIVE_UI_PART_TYPE,
    GUARDRAIL_NOTICE_PART_TYPE,
    GUARDRAIL_NOTICE_REASON,
    GUARDRAIL_NOTICE_SAFE_MESSAGE,
    MATH_STEPS_PART_TYPE,
    MATH_STEPS_RESPONSE_SCHEMA_REF,
    METRIC_NUMBER_PART_TYPE,
    NUMBER_TICKER_RESPONSE_SCHEMA_REF,
    PART_REGISTRY_VERSION,
    REASONING_SUMMARY_PART_TYPE,
    REGISTERED_PART_TYPES,
    RESPONSE_FORMAT_PART_TYPE,
    RICH_PROSE_PART_TYPE,
    SCORE_BREAKDOWN_PART_TYPE,
    SCORE_BREAKDOWN_RESPONSE_SCHEMA_REF,
    SERVER_PANEL_PART_TYPE,
    SPEC_SHEET_PART_TYPE,
    SPEC_SHEET_RESPONSE_SCHEMA_REF,
    TEXT_PART_TYPE,
    TIMELINE_PART_TYPE,
    TIMELINE_RESPONSE_SCHEMA_REF,
    TOOL_CALL_PART_TYPE,
    WEB_SEARCH_PART_TYPE,
    PartPayloadInvalid,
    is_registered_part_type,
    validate_registered_payload,
)

REVISION = "sha256:" + "a" * 64


def _reference(**overrides: Any) -> dict[str, Any]:
    return {"referenceId": "file-a", "sourceRevision": REVISION, **overrides}


def test_the_registry_states_only_the_families_core_produces() -> None:
    # Producer-first, and the whole point of the registry's restraint: Platform
    # decodes seventeen display-only part types that nothing produces, and the
    # provider event layer declares more event types than it emits today
    # emitted. Registering a shape for a family with no producer would be that
    # same defect a third time.
    #
    # Each family here has one, named so this list cannot grow quietly:
    #   attachment  `chat_submit`'s staged-attachment commit
    #   text        `chat_generation_executor._materialize_assistant` and `chat_submit`
    #   reasoning.summary  explicit provider `reasoning-summary` events only
    #   tool.call   `chat_generation_executor._record_tool_proposal` (C9), which
    #               records a provider `tool-call` event into C8's ledger, and
    #               `_materialize_assistant`, which emits one part per row
    #   web.search  one successful `codex.web_search` provider-reported row,
    #               projected by `_materialize_assistant` without result claims
    #   guardrail.notice  one exact provider `content-filter` finish, projected
    #               with fixed Core-authored display-safe copy
    #   response.format  explicit internal submit directive for one allow-listed schema
    #   rich.prose  validated structured data-table output materialised by the executor
    #   metric.number  validated number-ticker output materialised by the executor
    #   chart       validated structured chart output materialised by the executor
    #   diagram.flow validated structured directed graph materialised by the executor
    #   activity.series validated structured UTC calendar series materialised by the executor
    #   math.steps  validated structured-math derivation materialised by the executor
    #   spec.sheet  validated text-only specification sheet materialised by the executor
    #   comparison.matrix validated option-by-trait matrix materialised by the executor
    #   timeline    validated past/now/future event sequence materialised by the executor
    #   score.breakdown validated weighted criterion score materialised by the executor
    #   code.diff   validated single-file line diff materialised by the executor
    #   file.tree   validated relative path tree materialised by the executor
    #   server.panel  `chat_submit`, on the user's turn, from only the App Shell's
    #               read-only Dev MCP observation (initialize and tools/list)
    #   core.report  explicit internal submit directive naming one fixed Core report
    #   generative.ui  `chat_submit`, on the user's turn, computed by
    #               `chat_core_reports` from Core's own chat records (charts only)
    assert REGISTERED_PART_TYPES == (
        ACTIVITY_SERIES_PART_TYPE,
        ATTACHMENT_PART_TYPE,
        CHART_PART_TYPE,
        CODE_DIFF_PART_TYPE,
        COMPARISON_MATRIX_PART_TYPE,
        CORE_REPORT_PART_TYPE,
        DIAGRAM_FLOW_PART_TYPE,
        FILE_TREE_PART_TYPE,
        GENERATIVE_UI_PART_TYPE,
        GUARDRAIL_NOTICE_PART_TYPE,
        MATH_STEPS_PART_TYPE,
        METRIC_NUMBER_PART_TYPE,
        REASONING_SUMMARY_PART_TYPE,
        RESPONSE_FORMAT_PART_TYPE,
        RICH_PROSE_PART_TYPE,
        SCORE_BREAKDOWN_PART_TYPE,
        SERVER_PANEL_PART_TYPE,
        SPEC_SHEET_PART_TYPE,
        TEXT_PART_TYPE,
        TIMELINE_PART_TYPE,
        TOOL_CALL_PART_TYPE,
        WEB_SEARCH_PART_TYPE,
    )
    assert is_registered_part_type(TEXT_PART_TYPE)
    assert is_registered_part_type(ATTACHMENT_PART_TYPE)
    assert is_registered_part_type(TOOL_CALL_PART_TYPE)
    assert is_registered_part_type(REASONING_SUMMARY_PART_TYPE)
    assert is_registered_part_type(WEB_SEARCH_PART_TYPE)
    assert is_registered_part_type(GUARDRAIL_NOTICE_PART_TYPE)
    assert is_registered_part_type(RESPONSE_FORMAT_PART_TYPE)
    assert is_registered_part_type(RICH_PROSE_PART_TYPE)
    assert is_registered_part_type(METRIC_NUMBER_PART_TYPE)
    assert is_registered_part_type(CHART_PART_TYPE)
    assert is_registered_part_type(COMPARISON_MATRIX_PART_TYPE)
    assert is_registered_part_type(DIAGRAM_FLOW_PART_TYPE)
    assert is_registered_part_type(ACTIVITY_SERIES_PART_TYPE)
    assert is_registered_part_type(MATH_STEPS_PART_TYPE)
    assert is_registered_part_type(SPEC_SHEET_PART_TYPE)
    assert is_registered_part_type(TIMELINE_PART_TYPE)
    assert is_registered_part_type(SCORE_BREAKDOWN_PART_TYPE)
    assert is_registered_part_type(CODE_DIFF_PART_TYPE)
    assert is_registered_part_type(FILE_TREE_PART_TYPE)
    # Adding a family does NOT bump the version: a consumer that has never seen
    # it decodes it to the contract's inert fallback, which is the defined
    # behaviour for an unrecognised type and needs no negotiation.
    assert PART_REGISTRY_VERSION == 1


def test_an_unregistered_family_is_passed_through_and_never_refused() -> None:
    # The contract's vocabulary is OPEN. These are exactly the kinds of type a
    # future minor release may add, and an unrecognised one must reach the
    # renderer's inert fallback rather than being rejected at the wire.
    for part_type in ("markdown", "citation", "table", "app.output", "workflow.status"):
        assert not is_registered_part_type(part_type)
        # Any payload at all, including ones a registered family would refuse.
        validate_registered_payload(part_type, {"anything": {"nested": [1, 2, 3]}})
        validate_registered_payload(part_type, {})


def test_text_carries_exactly_its_own_text() -> None:
    validate_registered_payload(TEXT_PART_TYPE, {"text": "hello"})
    validate_registered_payload(TEXT_PART_TYPE, {"text": ""})
    for bad, field in (
        ({}, "text"),
        ({"text": 7}, "text"),
        ({"text": None}, "text"),
        ({"text": "hello", "markdown": True}, "markdown"),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(TEXT_PART_TYPE, bad)
        assert refused.value.field == field


def test_reasoning_summary_is_an_exact_display_safe_shape() -> None:
    validate_registered_payload(
        REASONING_SUMMARY_PART_TYPE,
        {"title": "Reasoning summary", "detail": "Compared the two supported routes."},
    )
    for bad, field in (
        ({"title": "Reasoning summary"}, "detail"),
        ({"title": "Reasoning", "detail": "safe"}, "title"),
        ({"title": "Reasoning summary", "detail": ""}, "detail"),
        ({"title": "Reasoning summary", "detail": "safe", "content": "hidden"}, "content"),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(REASONING_SUMMARY_PART_TYPE, bad)
        assert refused.value.field == field


def test_web_search_is_an_exact_completed_query_without_result_claims() -> None:
    validate_registered_payload(
        WEB_SEARCH_PART_TYPE,
        {"query": "site:openai.com official homepage", "state": "complete"},
    )
    for bad, field in (
        ({"state": "complete"}, "query"),
        ({"query": "", "state": "complete"}, "query"),
        ({"query": " leading", "state": "complete"}, "query"),
        ({"query": "line\nbreak", "state": "complete"}, "query"),
        ({"query": "x" * 4_097, "state": "complete"}, "query"),
        ({"query": "safe", "state": "searching"}, "state"),
        ({"query": "safe", "state": "complete", "results": []}, "results"),
        ({"query": "safe", "state": "complete", "url": "https://example.invalid"}, "url"),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(WEB_SEARCH_PART_TYPE, bad)
        assert refused.value.field == field


def test_guardrail_notice_is_an_exact_fixed_content_filter_fact() -> None:
    validate_registered_payload(
        GUARDRAIL_NOTICE_PART_TYPE,
        {
            "reasonCode": GUARDRAIL_NOTICE_REASON,
            "safeMessage": GUARDRAIL_NOTICE_SAFE_MESSAGE,
        },
    )
    for bad, field in (
        ({"safeMessage": GUARDRAIL_NOTICE_SAFE_MESSAGE}, "reasonCode"),
        ({"reasonCode": "policy", "safeMessage": GUARDRAIL_NOTICE_SAFE_MESSAGE}, "reasonCode"),
        ({"reasonCode": GUARDRAIL_NOTICE_REASON, "safeMessage": "Provider policy text"}, "safeMessage"),
        (
            {
                "reasonCode": GUARDRAIL_NOTICE_REASON,
                "safeMessage": GUARDRAIL_NOTICE_SAFE_MESSAGE,
                "category": "provider-secret",
            },
            "category",
        ),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(GUARDRAIL_NOTICE_PART_TYPE, bad)
        assert refused.value.field == field


def test_response_format_is_one_exact_allow_listed_internal_directive() -> None:
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": DATA_TABLE_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": NUMBER_TICKER_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": CHART_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": DIAGRAM_FLOW_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": ACTIVITY_SERIES_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": MATH_STEPS_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": SPEC_SHEET_RESPONSE_SCHEMA_REF},
    )
    validate_registered_payload(
        RESPONSE_FORMAT_PART_TYPE,
        {"schemaRef": COMPARISON_MATRIX_RESPONSE_SCHEMA_REF},
    )
    for schema_ref in (
        TIMELINE_RESPONSE_SCHEMA_REF,
        SCORE_BREAKDOWN_RESPONSE_SCHEMA_REF,
        CODE_DIFF_RESPONSE_SCHEMA_REF,
        FILE_TREE_RESPONSE_SCHEMA_REF,
    ):
        validate_registered_payload(RESPONSE_FORMAT_PART_TYPE, {"schemaRef": schema_ref})
    for bad, field in (
        ({}, "schemaRef"),
        ({"schemaRef": "arbitrary.schema"}, "schemaRef"),
        ({"schemaRef": DATA_TABLE_RESPONSE_SCHEMA_REF, "schema": {}}, "schema"),
    ):
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(RESPONSE_FORMAT_PART_TYPE, bad)
        assert refused.value.field == field


def test_metric_number_is_exact_bounded_finite_and_control_free() -> None:
    validate_registered_payload(
        METRIC_NUMBER_PART_TYPE,
        {"label": "Monthly active users", "value": 12_500, "unit": "users"},
    )
    validate_registered_payload(
        METRIC_NUMBER_PART_TYPE,
        {"label": "Conversion rate", "value": 0.125},
    )
    cases = (
        ({"value": 1}, "label"),
        ({"label": "", "value": 1}, "label"),
        ({"label": " padded", "value": 1}, "label"),
        ({"label": "line\nbreak", "value": 1}, "label"),
        ({"label": "x" * 257, "value": 1}, "label"),
        ({"label": "Metric", "value": True}, "value"),
        ({"label": "Metric", "value": float("inf")}, "value"),
        ({"label": "Metric", "value": float("nan")}, "value"),
        ({"label": "Metric", "value": -0.0}, "value"),
        ({"label": "Metric", "value": 10**1_000}, "value"),
        ({"label": "Metric", "value": 1_000_000_000_000_001}, "value"),
        ({"label": "Metric", "value": 1, "unit": ""}, "unit"),
        ({"label": "Metric", "value": 1, "unit": "x" * 65}, "unit"),
        ({"label": "Metric", "value": 1, "html": "<script>x</script>"}, "html"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(METRIC_NUMBER_PART_TYPE, bad)
        assert refused.value.field == field


def test_chart_is_exact_bounded_and_finite() -> None:
    valid = {
        "title": "Latency",
        "chart": "line",
        "detail": "Provider-reported latency",
        "unit": "ms",
        "series": [
            {
                "name": "Core",
                "points": [
                    {"label": "p50", "value": 8},
                    {"label": "p95", "value": 12.5},
                ],
            }
        ],
    }
    validate_registered_payload(CHART_PART_TYPE, valid)
    cases = (
        ({**valid, "title": ""}, "title"),
        ({**valid, "chart": "scatter"}, "chart"),
        ({**valid, "chart": []}, "chart"),
        ({**valid, "detail": "x" * 4_097}, "detail"),
        ({**valid, "unit": "x" * 65}, "unit"),
        ({**valid, "series": []}, "series"),
        (
            {
                **valid,
                "series": [{"name": "", "points": [{"label": "p50", "value": 8}]}],
            },
            "series[0].name",
        ),
        (
            {**valid, "series": [{"name": "Core", "points": []}]},
            "series[0].points",
        ),
        (
            {
                **valid,
                "series": [
                    {"name": "Core", "points": [{"label": "", "value": 8}]}
                ],
            },
            "series[0].points[0].label",
        ),
        (
            {
                **valid,
                "series": [
                    {"name": "Core", "points": [{"label": "p50", "value": True}]}
                ],
            },
            "series[0].points[0].value",
        ),
        (
            {
                **valid,
                "series": [
                    {
                        "name": "Core",
                        "points": [{"label": "p50", "value": float("inf")}],
                    }
                ],
            },
            "series[0].points[0].value",
        ),
        ({**valid, "html": "<script>x</script>"}, "html"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(CHART_PART_TYPE, bad)
        assert refused.value.field == field


def test_diagram_flow_is_exact_bounded_and_topologically_closed() -> None:
    node = {"id": "plan", "label": "Plan", "column": 0, "row": 0, "state": "done"}
    other = {"id": "ship", "label": "Ship", "column": 1, "row": 0, "state": "active"}
    valid = {
        "title": "Release flow",
        "nodes": [node, other],
        "edges": [{"from": "plan", "to": "ship"}],
    }
    validate_registered_payload(DIAGRAM_FLOW_PART_TYPE, valid)
    cases = (
        ({**valid, "title": ""}, "title"),
        ({**valid, "title": " padded"}, "title"),
        ({**valid, "title": "line\nbreak"}, "title"),
        ({**valid, "title": "é" * 129}, "title"),
        ({**valid, "nodes": []}, "nodes"),
        ({**valid, "nodes": [node, {**node}]}, "nodes[1].id"),
        ({**valid, "nodes": [{**node, "id": "bad/id"}]}, "nodes[0].id"),
        ({**valid, "nodes": [{**node, "label": " label"}]}, "nodes[0].label"),
        ({**valid, "nodes": [{**node, "column": True}]}, "nodes[0].column"),
        ({**valid, "nodes": [{**node, "column": 16}]}, "nodes[0].column"),
        ({**valid, "nodes": [{**node, "row": -1}]}, "nodes[0].row"),
        ({**valid, "nodes": [{**node, "state": "running"}]}, "nodes[0].state"),
        ({**valid, "nodes": [{**node, "state": []}]}, "nodes[0].state"),
        ({**valid, "nodes": [{**node, "command": "run"}]}, "nodes[0].command"),
        ({**valid, "edges": [{"from": "missing", "to": "ship"}]}, "edges[0].from"),
        ({**valid, "edges": [{"from": "plan", "to": "missing"}]}, "edges[0].to"),
        ({**valid, "edges": [{"from": "plan", "to": "plan"}]}, "edges[0]"),
        ({**valid, "edges": [{"from": "plan", "to": "ship"}] * 2}, "edges[1]"),
        ({**valid, "edges": [{"from": "plan", "to": "ship", "action": "open"}]}, "edges[0].action"),
        ({**valid, "html": "<script>x</script>"}, "html"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(DIAGRAM_FLOW_PART_TYPE, bad)
        assert refused.value.field == field


def test_activity_series_is_exact_bounded_and_internally_consistent() -> None:
    valid = {
        "title": "Contributions",
        "start": "2024-02-26",
        "end": "2024-03-03",
        "total": 5,
        "unit": "changes",
        "points": [
            {"date": "2024-02-27", "count": 2},
            {"date": "2024-03-01", "count": 3},
        ],
    }
    validate_registered_payload(ACTIVITY_SERIES_PART_TYPE, valid)
    validate_registered_payload(
        ACTIVITY_SERIES_PART_TYPE,
        {"title": "Quiet", "start": "2024-01-01", "end": "2024-01-01", "total": 0, "points": []},
    )
    cases = (
        ({**valid, "title": ""}, "title"),
        ({**valid, "unit": " changes"}, "unit"),
        ({**valid, "start": "1969-12-31"}, "start"),
        ({**valid, "start": "2024-02-30"}, "start"),
        ({**valid, "start": "20240301"}, "start"),
        ({**valid, "start": "2024-03-04"}, "end"),
        ({**valid, "start": "2023-01-01", "end": "2024-01-02"}, "end"),
        ({**valid, "total": True}, "total"),
        ({**valid, "total": 4}, "total"),
        ({**valid, "points": [{"date": "2024-02-27", "count": 0}], "total": 0}, "points[0].count"),
        ({**valid, "points": list(reversed(valid["points"]))}, "points[1].date"),
        ({**valid, "points": [valid["points"][0], valid["points"][0]]}, "points[1].date"),
        ({**valid, "points": [{"date": "2024-02-25", "count": 5}]}, "points[0].date"),
        ({**valid, "points": [{"date": "2024-02-27", "count": 5, "action": "open"}]}, "points[0].action"),
        ({**valid, "html": "<script>x</script>"}, "html"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(ACTIVITY_SERIES_PART_TYPE, bad)
        assert refused.value.field == field


def test_math_steps_is_exact_bounded_and_recursively_closed() -> None:
    valid = {
        "label": "Quadratic derivation",
        "steps": [
            {
                "tokens": [
                    {"kind": "text", "text": "x"},
                    {"kind": "sup", "tokens": [{"kind": "text", "text": "2"}]},
                    {"kind": "text", "text": " = "},
                    {
                        "kind": "frac",
                        "over": [{"kind": "text", "text": "-b ± √Δ"}],
                        "under": [{"kind": "text", "text": "2a"}],
                    },
                ],
                "note": "Apply the quadratic formula",
            }
        ],
    }
    validate_registered_payload(MATH_STEPS_PART_TYPE, valid)
    validate_registered_payload(
        MATH_STEPS_PART_TYPE,
        {"steps": [{"tokens": [{"kind": "text", "text": "1 + 1 = 2"}]}]},
    )

    nested: dict[str, Any] = {"kind": "text", "text": "x"}
    for _ in range(5):
        nested = {"kind": "sup", "tokens": [nested]}
    too_many_nodes = [
        {"tokens": [{"kind": "text", "text": "x"} for _ in range(64)]}
        for _ in range(5)
    ]
    cases = (
        ({**valid, "label": " leading"}, "label"),
        ({"steps": []}, "steps"),
        ({"steps": [{"tokens": [], "note": "empty"}]}, "steps[0].tokens"),
        ({"steps": [{"tokens": [{"kind": "text", "text": ""}]}]}, "steps[0].tokens[0].text"),
        ({"steps": [{"tokens": [{"kind": "text", "text": "\u0000"}]}]}, "steps[0].tokens[0].text"),
        ({"steps": [{"tokens": [{"kind": "sqrt", "tokens": []}]}]}, "steps[0].tokens[0].kind"),
        ({"steps": [{"tokens": [{"kind": "frac", "over": [], "under": [{"kind": "text", "text": "1"}]}]}]}, "steps[0].tokens[0].over"),
        ({"steps": [{"tokens": [nested]}]}, "steps[0].tokens[0].tokens[0].tokens[0].tokens[0].tokens[0].tokens"),
        ({"steps": too_many_nodes}, "steps[4].tokens"),
        ({"steps": [{"tokens": [{"kind": "text", "text": "x"}], "note": None}]}, "steps[0].note"),
        ({"steps": [{"tokens": [{"kind": "text", "text": "x"}], "html": "<b>x</b>"}]}, "steps[0].html"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(MATH_STEPS_PART_TYPE, bad)
        assert refused.value.field == field


def test_spec_sheet_is_exact_text_only_and_bounded() -> None:
    valid = {
        "title": "Runtime specification",
        "detail": "Observed configuration",
        "items": [
            {"label": "Provider", "value": "OpenAI"},
            {"label": "Model", "value": "gpt-5"},
        ],
    }
    validate_registered_payload(SPEC_SHEET_PART_TYPE, valid)
    validate_registered_payload(
        SPEC_SHEET_PART_TYPE,
        {"title": "Runtime", "items": [{"label": "Empty", "value": ""}]},
    )
    cases = (
        ({"title": "Runtime", "items": []}, "items"),
        ({"title": "", "items": [{"label": "A", "value": "B"}]}, "title"),
        ({"title": "é" * 129, "items": [{"label": "A", "value": "B"}]}, "title"),
        ({**valid, "detail": "é" * 2_049}, "detail"),
        ({**valid, "items": [{"label": "", "value": "B"}]}, "items[0].label"),
        ({**valid, "items": [{"label": "é" * 129, "value": "B"}]}, "items[0].label"),
        ({**valid, "items": [{"label": "A", "value": "é" * 2_049}]}, "items[0].value"),
        ({**valid, "items": [{"label": "A", "value": 1}]}, "items[0].value"),
        ({**valid, "items": [{"label": "A", "value": "B", "action": "run"}]}, "items[0].action"),
        ({**valid, "controls": []}, "controls"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(SPEC_SHEET_PART_TYPE, bad)
        assert refused.value.field == field


def test_comparison_matrix_is_exact_bounded_and_rectangular() -> None:
    valid = {
        "title": "Plans",
        "detail": "Choose for the workload",
        "columns": ["Standard", "Pro"],
        "items": [
            {"label": "Price", "values": ["$10", "$20"]},
            {"label": "Support", "values": ["", "Priority"]},
        ],
    }
    validate_registered_payload(COMPARISON_MATRIX_PART_TYPE, valid)
    cases = (
        ({**valid, "title": ""}, "title"),
        ({**valid, "title": "é" * 129}, "title"),
        ({**valid, "detail": "é" * 2_049}, "detail"),
        ({**valid, "columns": ["Only"]}, "columns"),
        ({**valid, "columns": ["A", "B", "C", "D", "E", "F", "G"]}, "columns"),
        ({**valid, "columns": ["", "Pro"]}, "columns[0]"),
        ({**valid, "columns": ["é" * 129, "Pro"]}, "columns[0]"),
        ({**valid, "items": []}, "items"),
        ({**valid, "items": [{"label": "", "values": ["a", "b"]}]}, "items[0].label"),
        ({**valid, "items": [{"label": "é" * 129, "values": ["a", "b"]}]}, "items[0].label"),
        ({**valid, "items": [{"label": "Price", "values": ["a"]}]}, "items[0].values"),
        ({**valid, "items": [{"label": "Price", "values": ["a", 2]}]}, "items[0].values[1]"),
        ({**valid, "items": [{"label": "Price", "values": ["a", "é" * 2_049]}]}, "items[0].values[1]"),
        ({**valid, "items": [{"label": "Price", "values": ["a", "b"], "recommended": True}]}, "items[0].recommended"),
        ({**valid, "recommendation": 1}, "recommendation"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(COMPARISON_MATRIX_PART_TYPE, bad)
        assert refused.value.field == field


def _refused(part_type: str, valid: dict[str, Any], cases: Any) -> None:
    validate_registered_payload(part_type, valid)
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(part_type, bad)
        assert refused.value.field == field, (bad, refused.value)


def test_timeline_is_exact_bounded_and_state_closed() -> None:
    item = {"label": "GA", "value": "2026-10-01", "state": "now"}
    valid = {
        "title": "Release",
        "detail": "Quarter plan",
        "items": [
            {"label": "Beta", "value": "September", "state": "past", "detail": ""},
            item,
            {"label": "LTS", "value": "2027", "state": "future"},
        ],
    }
    _refused(
        TIMELINE_PART_TYPE,
        valid,
        (
            ({**valid, "title": ""}, "title"),
            ({**valid, "title": "é" * 129}, "title"),
            ({**valid, "title": None}, "title"),
            ({**valid, "detail": "é" * 2_049}, "detail"),
            ({**valid, "detail": 1}, "detail"),
            ({**valid, "items": []}, "items"),
            ({**valid, "items": [item] * 65}, "items"),
            ({**valid, "items": ["GA"]}, "items[0]"),
            ({**valid, "items": [{**item, "label": ""}]}, "items[0].label"),
            ({**valid, "items": [{**item, "value": "é" * 129}]}, "items[0].value"),
            ({**valid, "items": [{**item, "state": "later"}]}, "items[0].state"),
            ({**valid, "items": [{**item, "state": ["now"]}]}, "items[0].state"),
            ({**valid, "items": [{**item, "detail": None}]}, "items[0].detail"),
            ({**valid, "items": [{**item, "detail": "é" * 2_049}]}, "items[0].detail"),
            ({**valid, "items": [{"label": "GA", "value": "Now"}]}, "items[0].state"),
            ({**valid, "items": [{**item, "icon": "x"}]}, "items[0].icon"),
            ({**valid, "kind": "vertical"}, "kind"),
        ),
    )


def test_score_breakdown_is_exact_weighted_and_consistent() -> None:
    fit = {"label": "Fit", "score": 8, "weightBps": 5_000, "note": "Covers the core"}
    cost = {"label": "Cost", "score": 7, "weightBps": 5_000}
    valid = {"verdict": "Strong fit", "total": 7.5, "outOf": 10, "criteria": [fit, cost]}
    _refused(
        SCORE_BREAKDOWN_PART_TYPE,
        valid,
        (
            ({**valid, "verdict": ""}, "verdict"),
            ({**valid, "verdict": " Strong"}, "verdict"),
            ({**valid, "verdict": "a\x01b"}, "verdict"),
            ({**valid, "verdict": "é" * 129}, "verdict"),
            ({**valid, "outOf": 0}, "outOf"),
            ({**valid, "outOf": True}, "outOf"),
            ({**valid, "outOf": 1_000_001}, "outOf"),
            ({**valid, "outOf": float("inf")}, "outOf"),
            ({**valid, "total": -0.0}, "total"),
            ({**valid, "total": True}, "total"),
            ({**valid, "total": 10.5}, "total"),
            ({**valid, "total": 7.4}, "total"),
            ({**valid, "criteria": []}, "criteria"),
            ({**valid, "criteria": [cost] * 17}, "criteria"),
            ({**valid, "criteria": [fit, {**cost, "label": "Fit"}]}, "criteria[1].label"),
            ({**valid, "criteria": [fit, {**cost, "label": "Cost "}]}, "criteria[1].label"),
            ({**valid, "criteria": [{**fit, "note": ""}, cost]}, "criteria[0].note"),
            ({**valid, "criteria": [{**fit, "note": "a\x7f"}, cost]}, "criteria[0].note"),
            ({**valid, "criteria": [{**fit, "note": " a"}, cost]}, "criteria[0].note"),
            ({**valid, "criteria": [{**fit, "note": "é" * 513}, cost]}, "criteria[0].note"),
            ({**valid, "criteria": [{**fit, "note": None}, cost]}, "criteria[0].note"),
            ({**valid, "criteria": [{**fit, "score": 11}, cost]}, "criteria[0].score"),
            ({**valid, "criteria": [{**fit, "score": -0.0}, cost]}, "criteria[0].score"),
            ({**valid, "criteria": [{**fit, "score": False}, cost]}, "criteria[0].score"),
            ({**valid, "criteria": [{**fit, "weightBps": 0}, cost]}, "criteria[0].weightBps"),
            ({**valid, "criteria": [{**fit, "weightBps": 5_000.0}, cost]}, "criteria[0].weightBps"),
            ({**valid, "criteria": [{**fit, "weightBps": True}, cost]}, "criteria[0].weightBps"),
            ({**valid, "criteria": [{**fit, "weightBps": 4_999}, cost]}, "criteria"),
            ({**valid, "criteria": [{**fit, "weight": 1}, cost]}, "criteria[0].weight"),
            ({**valid, "rank": 1}, "rank"),
        ),
    )


def test_code_diff_is_exact_bounded_and_change_closed() -> None:
    line = {"change": "add", "text": "x = 2"}
    hunk = {"header": "@@ -1 +1 @@", "lines": [{"change": "context", "text": ""}, line]}
    valid = {"file": "src/app.py", "detail": "Fix", "hunks": [hunk, {"lines": [line]}]}
    _refused(
        CODE_DIFF_PART_TYPE,
        valid,
        (
            ({**valid, "file": ""}, "file"),
            ({**valid, "file": "é" * 513}, "file"),
            ({**valid, "detail": 1}, "detail"),
            ({**valid, "detail": "é" * 2_049}, "detail"),
            ({**valid, "hunks": []}, "hunks"),
            ({**valid, "hunks": [hunk] * 65}, "hunks"),
            ({**valid, "hunks": [{**hunk, "header": None}]}, "hunks[0].header"),
            ({**valid, "hunks": [{**hunk, "header": "é" * 513}]}, "hunks[0].header"),
            ({**valid, "hunks": [{**hunk, "start": 1}]}, "hunks[0].start"),
            ({**valid, "hunks": [{"lines": []}]}, "hunks[0].lines"),
            ({**valid, "hunks": [{"lines": [line] * 501}]}, "hunks[0].lines"),
            ({**valid, "hunks": [{"lines": [line] * 401}] * 5}, "hunks"),
            ({**valid, "hunks": [{"lines": [{**line, "change": "modify"}]}]}, "hunks[0].lines[0].change"),
            ({**valid, "hunks": [{"lines": [{**line, "text": 1}]}]}, "hunks[0].lines[0].text"),
            ({**valid, "hunks": [{"lines": [{**line, "text": "é" * 2_049}]}]}, "hunks[0].lines[0].text"),
            ({**valid, "hunks": [{"lines": [{**line, "number": 3}]}]}, "hunks[0].lines[0].number"),
            ({**valid, "language": "python"}, "language"),
        ),
    )


def test_file_tree_is_exact_relative_and_parent_ordered() -> None:
    folder = {"path": "src", "name": "src", "depth": 0, "kind": "folder"}
    child = {"path": "src/app.py", "name": "app.py", "depth": 1, "kind": "file"}
    valid = {
        "title": "Changes",
        "detail": "Two files",
        "items": [
            folder,
            {**child, "additions": 3, "deletions": 0},
            {"path": "README.md", "name": "README.md", "depth": 0, "kind": "file"},
        ],
    }

    def path(value: str) -> dict[str, Any]:
        name = value.split("/")[-1]
        return {**valid, "items": [{**child, "path": value, "name": name, "depth": 0}]}

    _refused(
        FILE_TREE_PART_TYPE,
        valid,
        (
            ({**valid, "title": ""}, "title"),
            ({**valid, "title": "é" * 129}, "title"),
            ({**valid, "detail": "é" * 2_049}, "detail"),
            ({**valid, "items": []}, "items"),
            ({**valid, "items": [folder] * 257}, "items"),
            (path(""), "items[0].path"),
            (path("~x"), "items[0].path"),
            (path("a\\b"), "items[0].path"),
            (path("C:x"), "items[0].path"),
            (path("a\x00b"), "items[0].path"),
            (path("é" * 513), "items[0].path"),
            (path("/a"), "items[0].path"),
            (path("a//b"), "items[0].path"),
            (path("./a"), "items[0].path"),
            (path("a/.."), "items[0].path"),
            ({**valid, "items": [folder, folder]}, "items[1].path"),
            ({**valid, "items": [folder, {**child, "name": "other.py"}]}, "items[1].name"),
            ({**valid, "items": [folder, {**child, "depth": 0}]}, "items[1].depth"),
            ({**valid, "items": [folder, {**child, "depth": True}]}, "items[1].depth"),
            ({**valid, "items": [{**folder, "depth": 9}]}, "items[0].depth"),
            ({**valid, "items": [{**folder, "kind": "dir"}]}, "items[0].kind"),
            ({**valid, "items": [{**folder, "additions": 1}]}, "items[0].additions"),
            ({**valid, "items": [folder, {**child, "deletions": -1}]}, "items[1].deletions"),
            ({**valid, "items": [folder, {**child, "additions": True}]}, "items[1].additions"),
            ({**valid, "items": [folder, {**child, "additions": 1_000_001}]}, "items[1].additions"),
            ({**valid, "items": [folder, {**child, "additions": None}]}, "items[1].additions"),
            ({**valid, "items": [child, folder]}, "items[0].path"),
            ({**valid, "items": [{**folder, "kind": "file"}, child]}, "items[1].path"),
            ({**valid, "items": [{**folder, "status": "added"}]}, "items[0].status"),
            ({**valid, "root": "/"}, "root"),
        ),
    )


def test_rich_prose_table_is_exact_bounded_and_rectangular() -> None:
    valid = {
        "blocks": [
            {
                "kind": "table",
                "cols": ["Name", "Value"],
                "rows": [["alpha", "1"], ["beta", "2"]],
            }
        ]
    }
    validate_registered_payload(RICH_PROSE_PART_TYPE, valid)
    cases = (
        ({}, "blocks"),
        ({"blocks": []}, "blocks"),
        ({"blocks": [{"kind": "paragraph", "cols": ["A"], "rows": []}]}, "blocks[0].kind"),
        ({"blocks": [{"kind": "table", "cols": [], "rows": []}]}, "blocks[0].cols"),
        (
            {"blocks": [{"kind": "table", "cols": ["A", "B"], "rows": [["one"]]}]},
            "blocks[0].rows[0]",
        ),
        (
            {
                "blocks": [
                    {"kind": "table", "cols": ["A"], "rows": [["ok"]], "html": "<b>x</b>"}
                ]
            },
            "blocks[0].html",
        ),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(RICH_PROSE_PART_TYPE, bad)
        assert refused.value.field == field


def test_the_attachment_shape_is_exactly_what_the_authority_writes() -> None:
    # The registry and the producer must agree, so the producer's own output is
    # the fixture. A drift between them is the defect this test exists for.
    pinned = PinnedAttachment(
        reference_id="file-a",
        source_revision=REVISION,
        media_type="application/pdf",
        size_bytes=2048,
    )
    validate_registered_payload(ATTACHMENT_PART_TYPE, attachment_part_payload(pinned))
    # And the older shape, from before the display facts were carried.
    validate_registered_payload(
        ATTACHMENT_PART_TYPE, {"attachmentReference": _reference()}
    )


def test_the_attachment_shape_refuses_a_smuggled_field_or_a_bad_fact() -> None:
    cases: tuple[tuple[dict[str, Any], str], ...] = (
        ({}, "attachmentReference"),
        ({"attachmentReference": "file-a"}, "attachmentReference"),
        # A path or URL beside the reference: identity only, by 0070's rule.
        ({"attachmentReference": _reference(), "path": "/tmp/x"}, "path"),
        ({"attachmentReference": _reference(), "name": "secrets.pdf"}, "name"),
        # ... and one level in, which is where it would actually be hidden.
        ({"attachmentReference": _reference(path="/tmp/x")}, "attachmentReference.path"),
        ({"attachmentReference": {"referenceId": "file-a"}}, "attachmentReference.sourceRevision"),
        (
            {"attachmentReference": _reference(referenceId=7)},
            "attachmentReference.referenceId",
        ),
        (
            {"attachmentReference": _reference(referenceId="")},
            "attachmentReference.referenceId",
        ),
        ({"attachmentReference": _reference(), "mediaType": 7}, "mediaType"),
        ({"attachmentReference": _reference(), "sizeBytes": -1}, "sizeBytes"),
        ({"attachmentReference": _reference(), "sizeBytes": "big"}, "sizeBytes"),
        # `True` is an `int` in Python and is not a byte count.
        ({"attachmentReference": _reference(), "sizeBytes": True}, "sizeBytes"),
    )
    for bad, field in cases:
        with pytest.raises(PartPayloadInvalid) as refused:
            validate_registered_payload(ATTACHMENT_PART_TYPE, bad)
        assert refused.value.field == field, bad


def test_a_refusal_names_the_field_and_the_rule_and_never_the_value() -> None:
    # A part payload on the write path is untrusted input. An error that echoed
    # it would turn the refusal into an oracle for what was sent.
    secret = "a-value-that-must-not-be-echoed"
    with pytest.raises(PartPayloadInvalid) as refused:
        validate_registered_payload(TEXT_PART_TYPE, {"text": "ok", secret: 1})
    assert refused.value.field == secret, "the FIELD name is named, which is the caller's own"
    assert secret not in refused.value.rule
    with pytest.raises(PartPayloadInvalid) as other:
        validate_registered_payload(TEXT_PART_TYPE, {"text": secret, "extra": 1})
    assert secret not in str(other.value)


def test_a_non_object_payload_is_refused_for_a_registered_family_only() -> None:
    with pytest.raises(PartPayloadInvalid) as refused:
        validate_registered_payload(TEXT_PART_TYPE, "not an object")  # type: ignore[arg-type]
    assert refused.value.field == "payload"
    # Unregistered stays untouched, even for a non-object.
    validate_registered_payload("markdown", "not an object")  # type: ignore[arg-type]
