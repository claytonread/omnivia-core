"""Fixed Core reports: charts Core computes from its own chat records at submit.

A `core.report` directive makes Submit commit one `generative.ui` chart on the
user's turn, computed by Core (provenance `system`), never supplied by a client
and never by the model. Too little data refuses the submission before writing.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
from omnivia_core_runtime.service.chat_core_reports import (
    REPORT_DAYS,
    CoreReportUnavailable,
    core_report_chart,
)
from omnivia_core_runtime.service.chat_part_registry import (
    GENERATIVE_UI_PART_TYPE,
    PartPayloadInvalid,
    validate_registered_payload,
)
from omnivia_core_runtime.storage import chat
from test_chat_submit_message_resolver import (
    BRANCH_ID,
    CONVERSATION_ID,
    WORKSPACE_ID,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    _counts,
    _dispatcher,
    _request,
    _submit_command,
    owned,
    seeded,
)

__all__ = ["owned", "seeded"]

DAY_US = 86_400_000_000
NOW_US = 1_793_000_000_000_000  # 2026-10-26 (UTC)


def report_directive(report: str) -> dict[str, Any]:
    return {"type": "core.report", "visibility": "internal", "payload": {"report": report}}


def submit(holder: Any, *parts: dict[str, Any], key: str = "idem-core-report") -> Any:
    return _dispatcher(holder).dispatch(
        _request(_submit_command(editable_parts=parts), idempotency_key=key, request_id=f"req-{key}")
    )


def user_parts(holder: Any) -> list[Any]:
    return list(chat.read_message_parts(holder.connection, workspace_id=WORKSPACE_ID, message_id="msg-gb01-submitted"))


@pytest.mark.parametrize("report", ["chat-activity", "answers-by-kind"])
def test_a_core_report_commits_one_core_computed_chart_on_the_user_turn(seeded: Any, report: str) -> None:
    response = submit(seeded, {"type": "text", "payload": {"text": "Show my report"}}, report_directive(report))
    assert isinstance(response, SuccessResponseEnvelope), response
    parts = user_parts(seeded)
    assert [(part.part_type, part.visibility) for part in parts] == [
        ("text", "standard"),
        ("core.report", "internal"),
        (GENERATIVE_UI_PART_TYPE, "standard"),
    ]
    chart = parts[2]
    assert chart.provenance == "system", "Core computed it, not the person"
    assert [part.provenance for part in parts[:2]] == ["human", "human"]
    validate_registered_payload(GENERATIVE_UI_PART_TYPE, chart.payload)
    expected = {"chat-activity": ("chart-area", REPORT_DAYS), "answers-by-kind": ("chart-bars", 4)}[report]
    assert (chart.payload["variant"], len(chart.payload["points"])) == expected


def test_too_little_data_refuses_the_whole_submission_before_writing(seeded: Any) -> None:
    before = _counts(seeded)
    response = submit(seeded, {"type": "text", "payload": {"text": "Response time?"}}, report_directive("response-time"))
    assert isinstance(response, ErrorResponseEnvelope), response
    assert "not available" in response.error.message
    assert _counts(seeded) == before


@pytest.mark.parametrize(
    "parts",
    [
        # A client can never submit a chart as if Core had computed it.
        ({"type": "generative.ui", "payload": {"variant": "chart-line", "title": "Fake", "xLabel": "x", "yLabel": "y", "points": [{"label": "a", "value": 1}, {"label": "b", "value": 2}]}},),
        ({"type": "core.report", "payload": {"report": "chat-activity"}},),  # must be internal
        (report_directive("weather"),),
        (report_directive("chat-activity"), report_directive("answers-by-kind")),
    ],
)
def test_forged_charts_and_malformed_or_repeated_directives_are_refused(seeded: Any, parts: tuple[dict[str, Any], ...]) -> None:
    before = _counts(seeded)
    response = submit(seeded, {"type": "text", "payload": {"text": "x"}}, *parts, key="idem-bad-core-report")
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


def test_the_chart_reaches_follow_up_context_and_the_directive_does_not(seeded: Any) -> None:
    assert isinstance(submit(seeded, {"type": "text", "payload": {"text": "Report"}}, report_directive("chat-activity")), SuccessResponseEnvelope)
    context = executor_module._provider_context(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        branch_id=BRANCH_ID,
        trigger_message_id="msg-gb01-submitted",
        blobs_root=None,
    )
    texts = [part["text"] for part in context.messages[-1]["parts"]]
    assert texts[0] == "Report"
    assert texts[1].startswith("OmniVia Core report (computed by Core from this workspace's own chat records):\n")
    assert '"variant":"chart-area"' in texts[1]
    assert len(texts) == 2, "the internal directive is not provider content"


def test_the_registry_admits_only_the_three_bounded_chart_variants() -> None:
    ok = {"variant": "chart-bars", "title": "T", "xLabel": "x", "yLabel": "y", "points": [{"label": "a", "value": 1}, {"label": "b", "value": 2.5}]}
    validate_registered_payload(GENERATIVE_UI_PART_TYPE, ok)
    for bad in (
        {**ok, "variant": "weather-current"},
        {**ok, "extra": 1},
        {**ok, "title": " padded"},
        {**ok, "points": ok["points"][:1]},
        {**ok, "points": [{"label": "a", "value": 1}, {"label": "a", "value": 2}]},
        {**ok, "points": [{"label": "a", "value": True}, {"label": "b", "value": 2}]},
        {**ok, "points": [{"label": "a", "value": float("nan")}, {"label": "b", "value": 2}]},
        {**ok, "points": [{"label": "a", "value": 2e9}, {"label": "b", "value": 2}]},
        {**ok, "points": [{"label": f"p{n}", "value": n} for n in range(65)]},
    ):
        with pytest.raises(PartPayloadInvalid):
            validate_registered_payload(GENERATIVE_UI_PART_TYPE, bad)


def tables() -> sqlite3.Connection:
    """Only the columns the report queries read, so the arithmetic is tested exactly."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE omnivia_chat_messages (workspace_id TEXT, message_id TEXT, role TEXT, visibility TEXT,
          completion_status TEXT, tombstoned_at_us INTEGER, created_at_us INTEGER, committed_at_us INTEGER);
        CREATE TABLE omnivia_chat_message_parts (workspace_id TEXT, message_id TEXT, part_type TEXT);
        CREATE TABLE omnivia_chat_generation_jobs (workspace_id TEXT, generation_job_id TEXT, state TEXT,
          result_message_id TEXT, created_at_us INTEGER);
        """
    )
    return connection


def message(db: sqlite3.Connection, message_id: str, role: str, at_us: int, *part_types: str, workspace: str = "ws", **extra: Any) -> None:
    row = {"visibility": "standard", "completion_status": "complete", "tombstoned_at_us": None, **extra}
    db.execute(
        "INSERT INTO omnivia_chat_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (workspace, message_id, role, row["visibility"], row["completion_status"], row["tombstoned_at_us"], at_us, at_us),
    )
    for part_type in part_types:
        db.execute("INSERT INTO omnivia_chat_message_parts VALUES (?, ?, ?)", (workspace, message_id, part_type))


def test_activity_counts_visible_messages_per_utc_day_in_this_workspace_only() -> None:
    db = tables()
    message(db, "today", "user", NOW_US - 1_000)
    message(db, "today-answer", "assistant", NOW_US - 500, "text")
    message(db, "two-days", "user", NOW_US - 2 * DAY_US)
    message(db, "too-old", "user", NOW_US - 20 * DAY_US)
    message(db, "other-ws", "user", NOW_US - 1_000, workspace="other")
    message(db, "hidden", "user", NOW_US - 1_000, visibility="internal")
    message(db, "deleted", "user", NOW_US - 1_000, tombstoned_at_us=1)
    chart = core_report_chart(db, workspace_id="ws", report="chat-activity", now_us=NOW_US)
    values = [point["value"] for point in chart["points"]]
    assert len(values) == REPORT_DAYS and values[-1] == 2 and values[-3] == 1 and sum(values) == 3
    assert chart["points"][-1]["label"] == "Oct 26"


def test_answers_by_kind_classifies_each_answer_once() -> None:
    db = tables()
    message(db, "a1", "assistant", NOW_US - 10, "text")
    message(db, "a2", "assistant", NOW_US - 20, "text", "web.search")
    message(db, "a3", "assistant", NOW_US - 30, "reasoning.summary", "timeline", "web.search")
    message(db, "a4", "assistant", NOW_US - 40, "text", "tool.call")
    message(db, "u1", "user", NOW_US - 50, "text")
    chart = core_report_chart(db, workspace_id="ws", report="answers-by-kind", now_us=NOW_US)
    assert chart["points"] == [
        {"label": "Text only", "value": 1},
        {"label": "Web search", "value": 1},
        {"label": "Structured card", "value": 1},
        {"label": "Tool call", "value": 1},
    ]


def test_response_time_is_submit_to_committed_answer_for_the_latest_answers() -> None:
    db = tables()
    for index, seconds in enumerate([4.0, 6.5, 10.25]):
        start = NOW_US - (10 - index) * 60_000_000
        db.execute("INSERT INTO omnivia_chat_generation_jobs VALUES ('ws', ?, 'succeeded', ?, ?)", (f"j{index}", f"m{index}", start))
        message(db, f"m{index}", "assistant", start + int(seconds * 1_000_000))
    db.execute("INSERT INTO omnivia_chat_generation_jobs VALUES ('ws', 'jf', 'failed', NULL, ?)", (NOW_US,))
    chart = core_report_chart(db, workspace_id="ws", report="response-time", now_us=NOW_US)
    assert chart["points"] == [{"label": "#1", "value": 4.0}, {"label": "#2", "value": 6.5}, {"label": "#3", "value": 10.2}]
    validate_registered_payload(GENERATIVE_UI_PART_TYPE, chart)
    single = tables()
    single.execute("INSERT INTO omnivia_chat_generation_jobs VALUES ('ws', 'j', 'succeeded', 'm', ?)", (NOW_US - 1,))
    message(single, "m", "assistant", NOW_US)
    with pytest.raises(CoreReportUnavailable):
        core_report_chart(single, workspace_id="ws", report="response-time", now_us=NOW_US)
