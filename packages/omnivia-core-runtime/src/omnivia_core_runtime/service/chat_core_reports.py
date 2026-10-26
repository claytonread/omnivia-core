"""Fixed Core reports: charts Core computes from this workspace's own chat records.

A submission may carry one closed `core.report` directive. Submit answers it by
reading Core's own tables -- counts and times only, never message text -- and
committing one `generative.ui` chart on the user's turn. The model never supplies
a number in these charts, and a client can never submit one: only this module
produces them.

Days are UTC days: Core holds no user time zone, and the axis says so.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core_runtime.service.chat_part_registry import (
    ACTIVITY_SERIES_PART_TYPE,
    CHART_PART_TYPE,
    CODE_DIFF_PART_TYPE,
    COMPARISON_MATRIX_PART_TYPE,
    DIAGRAM_FLOW_PART_TYPE,
    FILE_TREE_PART_TYPE,
    MATH_STEPS_PART_TYPE,
    METRIC_NUMBER_PART_TYPE,
    RICH_PROSE_PART_TYPE,
    SCORE_BREAKDOWN_PART_TYPE,
    SPEC_SHEET_PART_TYPE,
    TIMELINE_PART_TYPE,
    TOOL_CALL_PART_TYPE,
    WEB_SEARCH_PART_TYPE,
)

__all__ = ["REPORT_DAYS", "RESPONSE_TIME_ANSWERS", "CoreReportUnavailable", "core_report_chart"]

#: The activity and answers-by-kind window, in UTC days ending today.
REPORT_DAYS: Final = 14
#: How many of the most recent answers the response-time report plots.
RESPONSE_TIME_ANSWERS: Final = 20

_STRUCTURED_CARDS: Final = frozenset(
    {
        RICH_PROSE_PART_TYPE,
        METRIC_NUMBER_PART_TYPE,
        CHART_PART_TYPE,
        DIAGRAM_FLOW_PART_TYPE,
        ACTIVITY_SERIES_PART_TYPE,
        MATH_STEPS_PART_TYPE,
        SPEC_SHEET_PART_TYPE,
        COMPARISON_MATRIX_PART_TYPE,
        TIMELINE_PART_TYPE,
        SCORE_BREAKDOWN_PART_TYPE,
        CODE_DIFF_PART_TYPE,
        FILE_TREE_PART_TYPE,
    }
)


class CoreReportUnavailable(Exception):
    """This workspace does not yet hold enough records for the report."""


def _day_label(day: datetime) -> str:
    return f"{day:%b} {day.day}"


def _window_start_us(now_us: int) -> tuple[datetime, int]:
    today = datetime.fromtimestamp(now_us / 1_000_000, tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    first = today - timedelta(days=REPORT_DAYS - 1)
    return first, int(first.timestamp() * 1_000_000)


def _chat_activity(connection: sqlite3.Connection, workspace_id: str, now_us: int) -> dict[str, Any]:
    first, start_us = _window_start_us(now_us)
    counts = [0] * REPORT_DAYS
    for (created_at_us,) in connection.execute(
        "SELECT created_at_us FROM omnivia_chat_messages "
        "WHERE workspace_id = ? AND visibility = 'standard' AND completion_status = 'complete' "
        "AND tombstoned_at_us IS NULL AND role IN ('user', 'assistant') "
        "AND created_at_us >= ? AND created_at_us <= ?",
        (workspace_id, start_us, now_us),
    ):
        index = (int(created_at_us) - start_us) // 86_400_000_000
        if 0 <= index < REPORT_DAYS:
            counts[index] += 1
    return {
        "variant": "chart-area",
        "title": "Chat activity",
        "xLabel": "Day (UTC)",
        "yLabel": "Messages",
        "points": [
            {"label": _day_label(first + timedelta(days=index)), "value": count}
            for index, count in enumerate(counts)
        ],
    }


def _response_time(connection: sqlite3.Connection, workspace_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT job.created_at_us, message.committed_at_us FROM omnivia_chat_generation_jobs AS job "
        "JOIN omnivia_chat_messages AS message "
        "ON message.workspace_id = job.workspace_id AND message.message_id = job.result_message_id "
        "WHERE job.workspace_id = ? AND job.state = 'succeeded' AND message.committed_at_us >= job.created_at_us "
        "ORDER BY job.created_at_us DESC, job.generation_job_id DESC LIMIT ?",
        (workspace_id, RESPONSE_TIME_ANSWERS),
    ).fetchall()
    if len(rows) < 2:
        raise CoreReportUnavailable("response time needs at least two completed answers")
    oldest_first = list(reversed(rows))
    return {
        "variant": "chart-line",
        "title": "Response time",
        "xLabel": f"Last {len(oldest_first)} answers (oldest to newest)",
        "yLabel": "Seconds",
        "points": [
            {"label": f"#{index}", "value": round((int(done) - int(started)) / 1_000_000, 1)}
            for index, (started, done) in enumerate(oldest_first, start=1)
        ],
    }


def _answers_by_kind(connection: sqlite3.Connection, workspace_id: str, now_us: int) -> dict[str, Any]:
    _, start_us = _window_start_us(now_us)
    kinds_by_message: dict[str, set[str]] = {}
    for message_id, part_type in connection.execute(
        "SELECT message.message_id, part.part_type FROM omnivia_chat_messages AS message "
        "JOIN omnivia_chat_message_parts AS part "
        "ON part.workspace_id = message.workspace_id AND part.message_id = message.message_id "
        "WHERE message.workspace_id = ? AND message.role = 'assistant' AND message.visibility = 'standard' "
        "AND message.completion_status = 'complete' AND message.tombstoned_at_us IS NULL "
        "AND message.created_at_us >= ? AND message.created_at_us <= ?",
        (workspace_id, start_us, now_us),
    ):
        kinds_by_message.setdefault(str(message_id), set()).add(str(part_type))
    counts = {"Text only": 0, "Web search": 0, "Structured card": 0, "Tool call": 0}
    for kinds in kinds_by_message.values():
        if kinds & _STRUCTURED_CARDS:
            counts["Structured card"] += 1
        elif WEB_SEARCH_PART_TYPE in kinds:
            counts["Web search"] += 1
        elif TOOL_CALL_PART_TYPE in kinds:
            counts["Tool call"] += 1
        else:
            counts["Text only"] += 1
    return {
        "variant": "chart-bars",
        "title": f"Answers by kind (last {REPORT_DAYS} days)",
        "xLabel": "Kind",
        "yLabel": "Answers",
        "points": [{"label": label, "value": value} for label, value in counts.items()],
    }


def core_report_chart(
    connection: sqlite3.Connection, *, workspace_id: str, report: str, now_us: int
) -> dict[str, Any]:
    """The `generative.ui` chart payload for one fixed report, read from Core's own tables."""
    if report == "chat-activity":
        return _chat_activity(connection, workspace_id, now_us)
    if report == "response-time":
        return _response_time(connection, workspace_id)
    if report == "answers-by-kind":
        return _answers_by_kind(connection, workspace_id, now_us)
    raise CoreReportUnavailable(f"unknown report {report!r}")
