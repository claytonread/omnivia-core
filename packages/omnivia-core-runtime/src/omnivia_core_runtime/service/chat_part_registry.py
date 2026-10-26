"""The Chat Part-type registry: the closed payload shape of each registered family.

The Chat Runtime Contract defers this deliberately. `MessagePart.type` is an
`OpenCode` and `payload` is `additionalProperties: true`, described as "an
opaque, schema-versioned object boundary rather than an invented catalogue",
with the per-type shapes owned by a "Chat Part-type registry (out of scope for
REF-042 and for this contract)". That registry is named twice in the whole
repository, both times in the sentence deferring to it. This module is it.

**Open stays open.** Registering a family does NOT close the vocabulary. The
contract says an unrecognised `type` decodes to an inert, display-safe fallback
and is never executed, and that remains true: a part type absent from this
registry is passed through exactly as before. What registration adds is a
stated, bounded shape for the families Core itself writes, so that a consumer
has something to decode against and a producer has something to be held to.

**Producer-first.** A family is registered here only when something actually
produces it. The families below are written by Core today:

* `text` -- `service/chat_generation_executor.py` (assistant) and the user side
  via `editableParts`.
* `attachment` -- `service/chat_submit.py`, through
  `service/chat_attachment_authority.attachment_part_payload`.
* `reasoning.summary` -- `service/chat_generation_executor.py`, from only an
  explicit provider `reasoning-summary` event; never from raw reasoning content.
* `tool.call` -- `service/chat_generation_executor.py`, from the durable
  provider-reported or governed tool ledger.
* `web.search` -- `service/chat_generation_executor.py`, from a completed,
  provider-reported `codex.web_search` lifecycle: its query and, when the
  provider's committed tool result reports them, its bounded sources, each
  marked `cited` only when the committed answer links that exact URL.
* `guardrail.notice` -- `service/chat_generation_executor.py`, from only the
  provider's closed `content-filter` finish reason and fixed Core-authored copy.
* `response.format` -- `service/chat_submit.py`, from an explicit internal
  composer directive naming one allow-listed structured response schema.
* `rich.prose` -- `service/chat_generation_executor.py`, currently from only
  the validated structured data-table response producer.
* `metric.number` -- `service/chat_generation_executor.py`, from only the
  validated structured number-ticker response producer.
* `chart` -- `service/chat_generation_executor.py`, from only the validated
  structured chart response producer.
* `diagram.flow` -- `service/chat_generation_executor.py`, from only the
  validated structured diagram-flow response producer.
* `activity.series` -- `service/chat_generation_executor.py`, from only the
  validated structured activity-series response producer.
* `math.steps` -- `service/chat_generation_executor.py`, from only the
  validated structured-math response producer.
* `spec.sheet` -- `service/chat_generation_executor.py`, from only the
  validated structured specification-sheet response producer.
* `comparison.matrix` -- `service/chat_generation_executor.py`, from only the
  validated structured comparison-matrix response producer.
* `timeline` -- `service/chat_generation_executor.py`, from only the
  validated structured timeline response producer.
* `score.breakdown` -- `service/chat_generation_executor.py`, from only the
  validated structured score-breakdown response producer.
* `code.diff` -- `service/chat_generation_executor.py`, from only the
  validated structured code-diff response producer.
* `file.tree` -- `service/chat_generation_executor.py`, from only the
  validated structured file-tree response producer.
* `server.panel` -- `service/chat_submit.py`, on the user's turn, from only the
  App Shell's read-only Dev MCP observation (`initialize` and `tools/list`; no
  tool is ever called). At most one per submission.
* `core.report` -- the closed internal directive a submission may carry (at
  most one) naming a fixed Core report.
* `generative.ui` -- `service/chat_submit.py`, on the user's turn, computed by
  Core from this workspace's own chat records for a `core.report` directive
  (`service/chat_core_reports.py`). Only the three chart variants; never
  accepted from a client.

Nothing else is registered, on purpose. `packages/chat/src/self-rendered` in
Platform decodes seventeen display-only part types that nothing produces, and
the provider event layer declares twenty event types of which seven are ever
emitted. Adding a schema here for a family with no producer would be the same
defect a third time.

**Why this is not a validator for the whole write path.** `SubmitMessage`
accepts `editableParts` whose `type` is any open code and whose payload is
unbounded, and passes both through to durable storage. Bounding a REGISTERED
family's payload there is compatible with the contract; refusing an
unregistered one is not, because the contract says the vocabulary is open.
:func:`validate_registered_payload` is written for exactly that distinction:
it answers "this payload is wrong for a family I know" and never "I do not
know this family".
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Final
from urllib.parse import urlsplit

#: An accepted attachment's part type is defined with the authority that writes
#: it, and imported rather than restated so the constant has one home.
from omnivia_core_runtime.service.chat_attachment_authority import ATTACHMENT_PART_TYPE

__all__ = [
    "ACTIVITY_SERIES_PART_TYPE",
    "ACTIVITY_SERIES_RESPONSE_SCHEMA_REF",
    "CHART_PART_TYPE",
    "CHART_RESPONSE_SCHEMA_REF",
    "CODE_DIFF_PART_TYPE",
    "CODE_DIFF_RESPONSE_SCHEMA_REF",
    "COMPARISON_MATRIX_PART_TYPE",
    "COMPARISON_MATRIX_RESPONSE_SCHEMA_REF",
    "CORE_REPORTS",
    "CORE_REPORT_PART_TYPE",
    "DATA_TABLE_RESPONSE_SCHEMA_REF",
    "DIAGRAM_FLOW_PART_TYPE",
    "DIAGRAM_FLOW_RESPONSE_SCHEMA_REF",
    "FILE_TREE_PART_TYPE",
    "FILE_TREE_RESPONSE_SCHEMA_REF",
    "GENERATIVE_UI_CHART_VARIANTS",
    "GENERATIVE_UI_PART_TYPE",
    "GUARDRAIL_NOTICE_PART_TYPE",
    "GUARDRAIL_NOTICE_REASON",
    "GUARDRAIL_NOTICE_SAFE_MESSAGE",
    "MATH_STEPS_PART_TYPE",
    "MATH_STEPS_RESPONSE_SCHEMA_REF",
    "METRIC_NUMBER_PART_TYPE",
    "NUMBER_TICKER_RESPONSE_SCHEMA_REF",
    "PART_REGISTRY_VERSION",
    "PRESENTATION_ONLY_PART_TYPES",
    "REASONING_SUMMARY_PART_TYPE",
    "REGISTERED_PART_TYPES",
    "RESPONSE_FORMAT_PART_TYPE",
    "RICH_PROSE_PART_TYPE",
    "SCORE_BREAKDOWN_PART_TYPE",
    "SCORE_BREAKDOWN_RESPONSE_SCHEMA_REF",
    "SERVER_PANEL_PART_TYPE",
    "SPEC_SHEET_PART_TYPE",
    "SPEC_SHEET_RESPONSE_SCHEMA_REF",
    "TEXT_PART_TYPE",
    "TIMELINE_PART_TYPE",
    "TIMELINE_RESPONSE_SCHEMA_REF",
    "TOOL_CALL_PART_TYPE",
    "WEB_SEARCH_MAX_SOURCES",
    "WEB_SEARCH_PART_TYPE",
    "PartPayloadInvalid",
    "is_registered_part_type",
    "validate_registered_payload",
]

#: The registry's own version, independent of `MessagePart.schemaVersion`.
#:
#: Bumped when a registered family's shape changes in a way a consumer must
#: notice. Adding a NEW family does not bump it: a consumer that has never seen
#: the family decodes it to the contract's inert fallback, which is the defined
#: behaviour for an unrecognised type and needs no version negotiation.
PART_REGISTRY_VERSION: Final = 1

#: The assistant's and the composer's text. The one family that predates this
#: registry on both sides.
TEXT_PART_TYPE: Final = "text"

#: C9: one governed tool call the provider proposed during this generation.
#:
#: The family is named for what it states -- a CALL that happened -- not for the
#: card a consumer draws it as.
TOOL_CALL_PART_TYPE: Final = "tool.call"

#: One provider-issued display-safe reasoning summary. This is deliberately
#: distinct from protected reasoning content, which never crosses F2a.
REASONING_SUMMARY_PART_TYPE: Final = "reasoning.summary"

#: One completed search that the connected provider reported executing. The
#: public Codex protocol currently exposes the query but no trustworthy result
#: URLs or snippets, so this family deliberately carries neither.
WEB_SEARCH_PART_TYPE: Final = "web.search"

#: One exact provider content-filter terminal fact. The notice carries fixed,
#: Core-authored display copy only: no provider policy prose, rejected content,
#: category labels or hidden moderation metadata cross the transcript boundary.
GUARDRAIL_NOTICE_PART_TYPE: Final = "guardrail.notice"
GUARDRAIL_NOTICE_REASON: Final = "content-filter"
GUARDRAIL_NOTICE_SAFE_MESSAGE: Final = "The provider filtered some or all of this response."

#: One explicit composer-to-Core request for the next assistant response. The
#: schema reference is opaque on F2a but closed here because Core can execute
#: only the exact structured-output producers registered below. An arbitrary
#: schema reference would create provider output Core could neither validate nor
#: materialise.
RESPONSE_FORMAT_PART_TYPE: Final = "response.format"
DATA_TABLE_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.data-table.v1"
NUMBER_TICKER_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.number-ticker.v1"
CHART_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.chart.v1"
DIAGRAM_FLOW_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.diagram-flow.v1"
ACTIVITY_SERIES_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.activity-series.v1"
MATH_STEPS_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.math-steps.v1"
SPEC_SHEET_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.spec-sheet.v1"
COMPARISON_MATRIX_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.comparison-matrix.v1"
TIMELINE_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.timeline.v1"
SCORE_BREAKDOWN_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.score-breakdown.v1"
CODE_DIFF_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.code-diff.v1"
FILE_TREE_RESPONSE_SCHEMA_REF: Final = "omnivia.chat.file-tree.v1"

#: The existing Platform typed-part family that owns the table block. Core emits
#: only the one-block table subset below; registering the whole renderer vocabulary
#: would again describe shapes that have no producer.
RICH_PROSE_PART_TYPE: Final = "rich.prose"

#: One exact numeric answer emitted by the number-ticker structured producer.
METRIC_NUMBER_PART_TYPE: Final = "metric.number"

#: One exact bounded data-series answer emitted by the chart structured producer.
CHART_PART_TYPE: Final = "chart"

#: One exact bounded directed graph emitted by the diagram-flow producer.
DIAGRAM_FLOW_PART_TYPE: Final = "diagram.flow"

#: One exact bounded UTC calendar series emitted by the activity producer.
ACTIVITY_SERIES_PART_TYPE: Final = "activity.series"

#: One exact bounded structured-math derivation emitted by the math producer.
MATH_STEPS_PART_TYPE: Final = "math.steps"

#: One exact bounded text-only specification sheet emitted by its producer.
SPEC_SHEET_PART_TYPE: Final = "spec.sheet"

#: One exact bounded option-by-trait comparison emitted by its producer.
COMPARISON_MATRIX_PART_TYPE: Final = "comparison.matrix"

#: One exact bounded past/now/future event sequence emitted by its producer.
TIMELINE_PART_TYPE: Final = "timeline"

#: One exact weighted per-criterion score whose total is the weighted sum.
SCORE_BREAKDOWN_PART_TYPE: Final = "score.breakdown"

#: One exact bounded single-file line diff emitted by its producer.
CODE_DIFF_PART_TYPE: Final = "code.diff"

#: One exact bounded relative path tree, parents before children.
FILE_TREE_PART_TYPE: Final = "file.tree"

#: One exact bounded MCP server status observed read-only by the App Shell.
SERVER_PANEL_PART_TYPE: Final = "server.panel"

#: A chart Core computed from this workspace's own records. Only the three chart
#: variants of the shared generative renderer are registered; every other
#: variant (weather, flights, carts ...) has no Core data and stays unregistered.
GENERATIVE_UI_PART_TYPE: Final = "generative.ui"
GENERATIVE_UI_CHART_VARIANTS: Final = frozenset({"chart-area", "chart-line", "chart-bars"})
GENERATIVE_UI_MAX_POINTS: Final = 64
GENERATIVE_UI_MAX_TEXT_UTF8_BYTES: Final = 256
GENERATIVE_UI_MAX_ABS_VALUE: Final = 1_000_000_000

#: The internal directive naming one fixed Core report to compute at submit.
CORE_REPORT_PART_TYPE: Final = "core.report"
CORE_REPORTS: Final = frozenset({"chat-activity", "response-time", "answers-by-kind"})

DATA_TABLE_MAX_COLUMNS: Final = 16
DATA_TABLE_MAX_ROWS: Final = 100
DATA_TABLE_MAX_COLUMN_UTF8_BYTES: Final = 256
DATA_TABLE_MAX_CELL_UTF8_BYTES: Final = 4_096
NUMBER_TICKER_MAX_LABEL_UTF8_BYTES: Final = 256
NUMBER_TICKER_MAX_UNIT_UTF8_BYTES: Final = 64
NUMBER_TICKER_MAX_ABS_VALUE: Final = 1_000_000_000_000_000
CHART_KINDS: Final = frozenset({"area", "bar", "donut", "line", "pie"})
CHART_MAX_SERIES: Final = 8
CHART_MAX_POINTS_PER_SERIES: Final = 64
CHART_MAX_LABEL_UTF8_BYTES: Final = 256
CHART_MAX_DETAIL_UTF8_BYTES: Final = 4_096
CHART_MAX_UNIT_UTF8_BYTES: Final = 64
DIAGRAM_FLOW_MAX_NODES: Final = 64
DIAGRAM_FLOW_MAX_EDGES: Final = 128
DIAGRAM_FLOW_MAX_GRID_INDEX: Final = 15
DIAGRAM_FLOW_MAX_TITLE_UTF8_BYTES: Final = 256
DIAGRAM_FLOW_MAX_LABEL_UTF8_BYTES: Final = 128
DIAGRAM_FLOW_NODE_STATES: Final = frozenset({"done", "active", "pending"})
ACTIVITY_SERIES_MAX_DAYS: Final = 366
ACTIVITY_SERIES_MAX_TITLE_UTF8_BYTES: Final = 256
ACTIVITY_SERIES_MAX_UNIT_UTF8_BYTES: Final = 64
ACTIVITY_SERIES_MAX_TOTAL: Final = 1_000_000_000_000
ACTIVITY_SERIES_MAX_COUNT: Final = 1_000_000_000
MATH_STEPS_MAX_STEPS: Final = 16
MATH_STEPS_MAX_NODES: Final = 256
MATH_STEPS_MAX_DEPTH: Final = 4
MATH_STEPS_MAX_TOKENS_PER_SEQUENCE: Final = 64
MATH_STEPS_MAX_LABEL_UTF8_BYTES: Final = 256
MATH_STEPS_MAX_NOTE_UTF8_BYTES: Final = 1_024
MATH_STEPS_MAX_TEXT_UTF8_BYTES: Final = 512
MATH_STEPS_MAX_TOTAL_TEXT_UTF8_BYTES: Final = 8_192
SPEC_SHEET_MAX_ROWS: Final = 64
SPEC_SHEET_MAX_TITLE_UTF8_BYTES: Final = 256
SPEC_SHEET_MAX_DETAIL_UTF8_BYTES: Final = 4_096
SPEC_SHEET_MAX_LABEL_UTF8_BYTES: Final = 256
SPEC_SHEET_MAX_VALUE_UTF8_BYTES: Final = 4_096
COMPARISON_MATRIX_MAX_OPTIONS: Final = 6
COMPARISON_MATRIX_MAX_TRAITS: Final = 12
COMPARISON_MATRIX_MAX_LABEL_UTF8_BYTES: Final = 256
COMPARISON_MATRIX_MAX_DETAIL_UTF8_BYTES: Final = 4_096
COMPARISON_MATRIX_MAX_VALUE_UTF8_BYTES: Final = 4_096
TIMELINE_MAX_ITEMS: Final = 64
TIMELINE_MAX_TEXT_UTF8_BYTES: Final = 256
TIMELINE_MAX_DETAIL_UTF8_BYTES: Final = 4_096
TIMELINE_STATES: Final = frozenset({"past", "now", "future"})
SCORE_BREAKDOWN_MAX_CRITERIA: Final = 16
SCORE_BREAKDOWN_MAX_LABEL_UTF8_BYTES: Final = 256
SCORE_BREAKDOWN_MAX_NOTE_UTF8_BYTES: Final = 1_024
SCORE_BREAKDOWN_MAX_OUT_OF: Final = 1_000_000
SCORE_BREAKDOWN_TOTAL_WEIGHT_BPS: Final = 10_000
CODE_DIFF_MAX_HUNKS: Final = 64
CODE_DIFF_MAX_LINES_PER_HUNK: Final = 500
CODE_DIFF_MAX_TOTAL_LINES: Final = 2_000
CODE_DIFF_MAX_FILE_UTF8_BYTES: Final = 1_024
CODE_DIFF_MAX_HEADER_UTF8_BYTES: Final = 1_024
CODE_DIFF_MAX_DETAIL_UTF8_BYTES: Final = 4_096
CODE_DIFF_MAX_TEXT_UTF8_BYTES: Final = 4_096
CODE_DIFF_CHANGES: Final = frozenset({"add", "remove", "context"})
FILE_TREE_MAX_ITEMS: Final = 256
FILE_TREE_MAX_DEPTH: Final = 8
FILE_TREE_MAX_TITLE_UTF8_BYTES: Final = 256
FILE_TREE_MAX_DETAIL_UTF8_BYTES: Final = 4_096
FILE_TREE_MAX_PATH_UTF8_BYTES: Final = 1_024
FILE_TREE_MAX_NAME_UTF8_BYTES: Final = 256
FILE_TREE_MAX_COUNT: Final = 1_000_000
FILE_TREE_KINDS: Final = frozenset({"folder", "file"})
SERVER_PANEL_MAX_SERVERS: Final = 32
SERVER_PANEL_MAX_TOOLS_PER_SERVER: Final = 64
SERVER_PANEL_MAX_TEXT_UTF8_BYTES: Final = 256
SERVER_PANEL_MAX_DETAIL_UTF8_BYTES: Final = 512
SERVER_PANEL_MAX_TOTAL_UTF8_BYTES: Final = 65_536
#: The closed display states Platform's MCP projection maps to a server status.
SERVER_PANEL_STATES: Final = frozenset(
    {
        "queued",
        "loading",
        "streaming",
        "partial",
        "complete",
        "stopped",
        "failed",
        "stale",
        "permission",
        "unavailable",
        "waiting",
    }
)
_WORKSPACE_SCOPED_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

#: Exact Core-authored families that are transcript presentation, not F2a
#: provider prompt content. Keeping this taxonomy beside the part registry gives
#: both the ordinary and compacted context builders the same closed decision.
#: Unknown open-code parts are deliberately absent and therefore still refused.
PRESENTATION_ONLY_PART_TYPES: Final = frozenset(
    {
        REASONING_SUMMARY_PART_TYPE,
        TOOL_CALL_PART_TYPE,
        WEB_SEARCH_PART_TYPE,
        GUARDRAIL_NOTICE_PART_TYPE,
    }
)

class PartPayloadInvalid(Exception):
    """A payload that does not match the registered shape of its own family.

    Carries the field and the rule, never the rejected value: a part payload on
    the write path is untrusted input, and echoing it is how an error message
    becomes an oracle. Same discipline as `chat_submit`'s own refusals.
    """

    def __init__(self, field: str, rule: str) -> None:
        super().__init__(f"{field} {rule}")
        self.field = field
        self.rule = rule


def _require_exact_keys(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    prefix: str = "",
) -> None:
    """Exactly `required`, optionally `optional`, and nothing else.

    `prefix` qualifies the reported field for a nested object, so a smuggled
    `attachmentReference.path` is named in full rather than as a bare `path`
    that a caller cannot locate.
    """
    present = set(payload)
    missing = required - present
    if missing:
        raise PartPayloadInvalid(f"{prefix}{min(missing)}", "is required for this part type")
    unknown = present - required - optional
    if unknown:
        raise PartPayloadInvalid(f"{prefix}{min(unknown)}", "is not a field of this part type")


def _require_text(payload: Mapping[str, Any], field: str, *, maximum: int) -> None:
    value = payload.get(field)
    if not isinstance(value, str):
        raise PartPayloadInvalid(field, "must be a string")
    # Counted in UTF-8 bytes, as every other bound in this contract is: a
    # character count would admit a payload the storage column refuses.
    if len(value.encode("utf-8")) > maximum:
        raise PartPayloadInvalid(field, f"must be at most {maximum} UTF-8 bytes")


#: `text` carries exactly its own text. The contract's `DirectionTextPart`
#: closes the same family at 1..4000 characters for the C6 steering path; a
#: committed Message part is not bounded there, and the Message's own
#: `content_hash` plus the transport's envelope bound are what hold it, so this
#: states the generous storage-side ceiling rather than inventing a stricter one.
_TEXT_MAX_UTF8_BYTES: Final = 1_048_576


def _validate_text(payload: Mapping[str, Any]) -> None:
    _require_exact_keys(payload, required=frozenset({"text"}), optional=frozenset())
    _require_text(payload, "text", maximum=_TEXT_MAX_UTF8_BYTES)


def _validate_attachment(payload: Mapping[str, Any]) -> None:
    """The shape `attachment_part_payload` writes.

    The opaque reference, plus the two display facts migration 0070's header
    names as the service's own: media type and byte size. Both optional,
    because a part committed before they were carried has neither. No name:
    Core stores none, and 0070 forbids a path, URL or handle in every column.
    """
    _require_exact_keys(
        payload,
        required=frozenset({"attachmentReference"}),
        optional=frozenset({"mediaType", "sizeBytes"}),
    )
    reference = payload["attachmentReference"]
    if not isinstance(reference, Mapping):
        raise PartPayloadInvalid("attachmentReference", "must be an object")
    _require_exact_keys(
        reference,
        required=frozenset({"referenceId", "sourceRevision"}),
        optional=frozenset(),
        prefix="attachmentReference.",
    )
    for field in ("referenceId", "sourceRevision"):
        value = reference[field]
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise PartPayloadInvalid(
                f"attachmentReference.{field}", "must be a string of 1 to 128 characters"
            )
    if "mediaType" in payload:
        _require_text(payload, "mediaType", maximum=255)
    if "sizeBytes" in payload:
        size = payload["sizeBytes"]
        # `bool` is an `int` in Python, and `True` is not a byte count.
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PartPayloadInvalid("sizeBytes", "must be a non-negative integer")


def _validate_tool_call(payload: Mapping[str, Any]) -> None:
    """The shape :func:`tool_call_part_payload` writes.

    THE TOOL'S ARGUMENTS ARE NOT A FIELD OF THIS FAMILY, and closing the shape
    is how that is enforced. A Message part is read by anyone who may read the
    conversation, and C8 already settled that this bundle publishes no tool
    arguments in any form -- `ToolCallProjection` refuses to project them even
    to the reviewer who has to decide on the call. A part carrying them would be
    the first outbound disclosure of tool input, reached through the transcript
    rather than through a query. The contract's `MessagePart.payload` is
    `additionalProperties: true`, so nothing but this registry would stop it.
    A producer that wants to show arguments needs a new decision, not a spare
    field.

    `state` is Core's own tool-call state, stated verbatim and NOT translated
    into any consumer's display vocabulary. The contract makes an unrecognised
    state draw inertly, which is the right outcome and keeps Core from having to
    know which chips a renderer owns.
    """
    _require_exact_keys(
        payload,
        required=frozenset({"name"}),
        optional=frozenset({"id", "state", "detail"}),
    )
    _require_text(payload, "name", maximum=128)
    for field in ("id", "state", "detail"):
        if field in payload:
            _require_text(payload, field, maximum=512)
            if payload[field] == "":
                raise PartPayloadInvalid(field, "must not be empty when present")


def _validate_reasoning_summary(payload: Mapping[str, Any]) -> None:
    """The exact display payload emitted for explicit provider summaries only."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "detail"}),
        optional=frozenset(),
    )
    _require_text(payload, "title", maximum=64)
    _require_text(payload, "detail", maximum=262_144)
    if payload["title"] != "Reasoning summary":
        raise PartPayloadInvalid("title", "must identify the reasoning summary")
    if payload["detail"] == "" or "\x00" in payload["detail"]:
        raise PartPayloadInvalid("detail", "must be non-empty and NUL-free")


WEB_SEARCH_MAX_SOURCES: Final = 20
WEB_SEARCH_MAX_SOURCE_URL_UTF8_BYTES: Final = 2_048
WEB_SEARCH_MAX_SOURCE_TITLE_UTF8_BYTES: Final = 256
WEB_SEARCH_MAX_HOST_UTF8_BYTES: Final = 253
# C1 controls and U+FEFF: Python's str.strip and JavaScript's String.trim
# disagree on them, so a title holding one is refused on both sides alike.
_TITLE_AMBIGUOUS: Final = re.compile("[\u0080-\u009f\ufeff]")
_WEB_SEARCH_SOURCE_KEYS: Final = frozenset({"url", "title", "domain", "opened", "cited"})


def _validate_web_search_source(source: Any, index: int) -> str:
    """One search result as display data: a public https URL, its own host, a title."""
    field = f"sources[{index}]"
    if not isinstance(source, Mapping) or set(source) != _WEB_SEARCH_SOURCE_KEYS:
        raise PartPayloadInvalid(field, "must be exactly url, title, domain, opened and cited")
    url, title, domain = source["url"], source["title"], source["domain"]
    if (
        not isinstance(url, str)
        or len(url.encode("utf-8")) > WEB_SEARCH_MAX_SOURCE_URL_UTF8_BYTES
        or not url.startswith("https://")
        or any(ord(character) <= 32 or ord(character) == 127 for character in url)
        or "\\" in url
    ):
        raise PartPayloadInvalid(f"{field}.url", "must be a bounded https URL without spaces or controls")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 -- raises on a malformed port
    except ValueError as error:
        raise PartPayloadInvalid(f"{field}.url", "must parse as a URL") from error
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or not host:
        raise PartPayloadInvalid(f"{field}.url", "must be https with a host and no credentials")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if (
        is_ip
        or "." not in host
        or len(host.encode("utf-8")) > WEB_SEARCH_MAX_HOST_UTF8_BYTES
        or host == "localhost"
        or host.endswith((".", ".localhost", ".local"))
    ):
        raise PartPayloadInvalid(f"{field}.url", "must name a bounded public domain")
    if domain != host:
        raise PartPayloadInvalid(f"{field}.domain", "must be the URL's own host")
    if (
        not isinstance(title, str)
        or title == ""
        or title.strip() != title
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
        or _TITLE_AMBIGUOUS.search(title) is not None
        or len(title.encode("utf-8")) > WEB_SEARCH_MAX_SOURCE_TITLE_UTF8_BYTES
    ):
        raise PartPayloadInvalid(f"{field}.title", "must be non-empty, trimmed, control-free and bounded")
    if not isinstance(source["opened"], bool) or not isinstance(source["cited"], bool):
        raise PartPayloadInvalid(field, "opened and cited must be booleans")
    return url


def _validate_web_search(payload: Mapping[str, Any]) -> None:
    """The exact completed-search display fact produced from the tool ledger."""
    _require_exact_keys(
        payload,
        required=frozenset({"query", "state"}),
        optional=frozenset({"sources"}),
    )
    _require_text(payload, "query", maximum=4_096)
    query = payload["query"]
    if query == "" or query.strip() != query or any(ord(character) < 32 or ord(character) == 127 for character in query):
        raise PartPayloadInvalid("query", "must be non-empty, trimmed and control-free")
    if payload["state"] != "complete":
        raise PartPayloadInvalid("state", "must be complete")
    if "sources" in payload:
        sources = payload["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= WEB_SEARCH_MAX_SOURCES:
            raise PartPayloadInvalid("sources", f"must list 1 to {WEB_SEARCH_MAX_SOURCES} sources")
        urls = [_validate_web_search_source(source, index) for index, source in enumerate(sources)]
        if len(set(urls)) != len(urls):
            raise PartPayloadInvalid("sources", "must not repeat a URL")


def _validate_guardrail_notice(payload: Mapping[str, Any]) -> None:
    """The fixed display-safe notice emitted for an exact filtered finish."""
    _require_exact_keys(
        payload,
        required=frozenset({"reasonCode", "safeMessage"}),
        optional=frozenset(),
    )
    _require_text(payload, "reasonCode", maximum=32)
    _require_text(payload, "safeMessage", maximum=256)
    if payload["reasonCode"] != GUARDRAIL_NOTICE_REASON:
        raise PartPayloadInvalid("reasonCode", "must identify the content-filter finish")
    if payload["safeMessage"] != GUARDRAIL_NOTICE_SAFE_MESSAGE:
        raise PartPayloadInvalid("safeMessage", "must be the fixed display-safe message")


def _validate_response_format(payload: Mapping[str, Any]) -> None:
    """One closed internal directive; never provider-visible message content."""
    _require_exact_keys(
        payload,
        required=frozenset({"schemaRef"}),
        optional=frozenset(),
    )
    _require_text(payload, "schemaRef", maximum=512)
    if payload["schemaRef"] not in {
        CHART_RESPONSE_SCHEMA_REF,
        ACTIVITY_SERIES_RESPONSE_SCHEMA_REF,
        DATA_TABLE_RESPONSE_SCHEMA_REF,
        DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
        MATH_STEPS_RESPONSE_SCHEMA_REF,
        NUMBER_TICKER_RESPONSE_SCHEMA_REF,
        SPEC_SHEET_RESPONSE_SCHEMA_REF,
        COMPARISON_MATRIX_RESPONSE_SCHEMA_REF,
        TIMELINE_RESPONSE_SCHEMA_REF,
        SCORE_BREAKDOWN_RESPONSE_SCHEMA_REF,
        CODE_DIFF_RESPONSE_SCHEMA_REF,
        FILE_TREE_RESPONSE_SCHEMA_REF,
    }:
        raise PartPayloadInvalid("schemaRef", "is not a registered response schema")


def _require_metric_text(payload: Mapping[str, Any], field: str, *, maximum: int) -> None:
    _require_text(payload, field, maximum=maximum)
    value = payload[field]
    if (
        value == ""
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PartPayloadInvalid(field, "must be non-empty, trimmed and control-free")


def _validate_metric_number(payload: Mapping[str, Any]) -> None:
    """The exact finite answer metric emitted by the structured producer."""
    _require_exact_keys(
        payload,
        required=frozenset({"label", "value"}),
        optional=frozenset({"unit"}),
    )
    _require_metric_text(payload, "label", maximum=NUMBER_TICKER_MAX_LABEL_UTF8_BYTES)
    if "unit" in payload:
        _require_metric_text(payload, "unit", maximum=NUMBER_TICKER_MAX_UNIT_UTF8_BYTES)
    value = payload["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PartPayloadInvalid("value", "must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise PartPayloadInvalid("value", "must be finite")
    if value == 0 and isinstance(value, float) and math.copysign(1.0, value) < 0:
        raise PartPayloadInvalid("value", "must not be negative zero")
    if abs(value) > NUMBER_TICKER_MAX_ABS_VALUE:
        raise PartPayloadInvalid(
            "value", "must have absolute value at most 1000000000000000"
        )


def _require_nonempty_text(
    payload: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
    prefix: str = "",
) -> None:
    try:
        _require_text(payload, field, maximum=maximum)
    except PartPayloadInvalid as error:
        raise PartPayloadInvalid(f"{prefix}{error.field}", error.rule) from None
    if payload[field] == "":
        raise PartPayloadInvalid(f"{prefix}{field}", "must be non-empty")


def _validate_chart(payload: Mapping[str, Any]) -> None:
    """The exact bounded finite data-series answer emitted by the chart producer."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "chart", "series"}),
        optional=frozenset({"detail", "unit"}),
    )
    _require_nonempty_text(payload, "title", maximum=CHART_MAX_LABEL_UTF8_BYTES)
    chart = payload["chart"]
    if not isinstance(chart, str) or chart not in CHART_KINDS:
        raise PartPayloadInvalid("chart", "is not an admitted chart kind")
    if "detail" in payload:
        _require_text(payload, "detail", maximum=CHART_MAX_DETAIL_UTF8_BYTES)
    if "unit" in payload:
        _require_text(payload, "unit", maximum=CHART_MAX_UNIT_UTF8_BYTES)
    series = payload["series"]
    if not isinstance(series, list) or not 1 <= len(series) <= CHART_MAX_SERIES:
        raise PartPayloadInvalid("series", "must contain between 1 and 8 series")
    for series_index, candidate in enumerate(series):
        prefix = f"series[{series_index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"name", "points"}),
            optional=frozenset(),
            prefix=prefix,
        )
        _require_nonempty_text(
            candidate,
            "name",
            maximum=CHART_MAX_LABEL_UTF8_BYTES,
            prefix=prefix,
        )
        points = candidate["points"]
        if not isinstance(points, list) or not 1 <= len(points) <= CHART_MAX_POINTS_PER_SERIES:
            raise PartPayloadInvalid(
                f"{prefix}points", "must contain between 1 and 64 points"
            )
        for point_index, point in enumerate(points):
            point_prefix = f"{prefix}points[{point_index}]."
            if not isinstance(point, Mapping):
                raise PartPayloadInvalid(point_prefix[:-1], "must be an object")
            _require_exact_keys(
                point,
                required=frozenset({"label", "value"}),
                optional=frozenset(),
                prefix=point_prefix,
            )
            _require_nonempty_text(
                point,
                "label",
                maximum=CHART_MAX_LABEL_UTF8_BYTES,
                prefix=point_prefix,
            )
            value = point["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PartPayloadInvalid(f"{point_prefix}value", "must be a number")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise PartPayloadInvalid(f"{point_prefix}value", "must be finite")


def _require_diagram_text(
    payload: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
    prefix: str = "",
) -> None:
    """Require the renderer's exact non-empty, trimmed, control-free text."""
    try:
        _require_text(payload, field, maximum=maximum)
    except PartPayloadInvalid as error:
        raise PartPayloadInvalid(f"{prefix}{error.field}", error.rule) from None
    value = payload[field]
    if (
        value == ""
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PartPayloadInvalid(
            f"{prefix}{field}", "must be non-empty, trimmed and control-free"
        )


def _validate_diagram_flow(payload: Mapping[str, Any]) -> None:
    """The exact finite topology accepted by Platform's Diagram / Flow Graph."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "nodes", "edges"}),
        optional=frozenset(),
    )
    _require_diagram_text(
        payload,
        "title",
        maximum=DIAGRAM_FLOW_MAX_TITLE_UTF8_BYTES,
    )
    nodes = payload["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= DIAGRAM_FLOW_MAX_NODES:
        raise PartPayloadInvalid("nodes", "must contain between 1 and 64 nodes")
    node_ids: set[str] = set()
    for index, candidate in enumerate(nodes):
        prefix = f"nodes[{index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"id", "label", "column", "row", "state"}),
            optional=frozenset(),
            prefix=prefix,
        )
        node_id = candidate["id"]
        if not isinstance(node_id, str) or _WORKSPACE_SCOPED_ID.fullmatch(node_id) is None:
            raise PartPayloadInvalid(f"{prefix}id", "must be a workspace-scoped id")
        if node_id in node_ids:
            raise PartPayloadInvalid(f"{prefix}id", "must be unique within the graph")
        node_ids.add(node_id)
        _require_diagram_text(
            candidate,
            "label",
            maximum=DIAGRAM_FLOW_MAX_LABEL_UTF8_BYTES,
            prefix=prefix,
        )
        for field in ("column", "row"):
            coordinate = candidate[field]
            if (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, int)
                or not 0 <= coordinate <= DIAGRAM_FLOW_MAX_GRID_INDEX
            ):
                raise PartPayloadInvalid(
                    f"{prefix}{field}", "must be an integer from 0 through 15"
                )
        state = candidate["state"]
        if not isinstance(state, str) or state not in DIAGRAM_FLOW_NODE_STATES:
            raise PartPayloadInvalid(
                f"{prefix}state", "must be done, active or pending"
            )

    edges = payload["edges"]
    if not isinstance(edges, list) or len(edges) > DIAGRAM_FLOW_MAX_EDGES:
        raise PartPayloadInvalid("edges", "must contain at most 128 edges")
    edge_keys: set[tuple[str, str]] = set()
    for index, candidate in enumerate(edges):
        prefix = f"edges[{index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"from", "to"}),
            optional=frozenset(),
            prefix=prefix,
        )
        source = candidate["from"]
        target = candidate["to"]
        if not isinstance(source, str) or source not in node_ids:
            raise PartPayloadInvalid(f"{prefix}from", "must name an existing node")
        if not isinstance(target, str) or target not in node_ids:
            raise PartPayloadInvalid(f"{prefix}to", "must name an existing node")
        if source == target:
            raise PartPayloadInvalid(prefix[:-1], "must not be a self-edge")
        edge_key = (source, target)
        if edge_key in edge_keys:
            raise PartPayloadInvalid(prefix[:-1], "must not duplicate a directed edge")
        edge_keys.add(edge_key)


def _activity_day(value: Any, field: str) -> date:
    """Require Platform's exact canonical UTC calendar-day spelling."""
    if not isinstance(value, str):
        raise PartPayloadInvalid(field, "must be a canonical UTC YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise PartPayloadInvalid(
            field, "must be a canonical UTC YYYY-MM-DD date"
        ) from None
    if parsed.year < 1970 or parsed.isoformat() != value:
        raise PartPayloadInvalid(
            field, "must be a canonical UTC YYYY-MM-DD date from 1970 through 9999"
        )
    return parsed


def _validate_activity_series(payload: Mapping[str, Any]) -> None:
    """The exact internally consistent series accepted by Activity Graph."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "start", "end", "total", "points"}),
        optional=frozenset({"unit"}),
    )
    _require_diagram_text(
        payload,
        "title",
        maximum=ACTIVITY_SERIES_MAX_TITLE_UTF8_BYTES,
    )
    if "unit" in payload:
        _require_diagram_text(
            payload,
            "unit",
            maximum=ACTIVITY_SERIES_MAX_UNIT_UTF8_BYTES,
        )
    start = _activity_day(payload["start"], "start")
    end = _activity_day(payload["end"], "end")
    if end < start:
        raise PartPayloadInvalid("end", "must not precede start")
    if (end - start).days + 1 > ACTIVITY_SERIES_MAX_DAYS:
        raise PartPayloadInvalid("end", "must span at most 366 inclusive days")
    total = payload["total"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or not 0 <= total <= ACTIVITY_SERIES_MAX_TOTAL
    ):
        raise PartPayloadInvalid(
            "total", "must be an integer from 0 through 1000000000000"
        )
    points = payload["points"]
    if not isinstance(points, list) or len(points) > ACTIVITY_SERIES_MAX_DAYS:
        raise PartPayloadInvalid("points", "must contain at most 366 points")
    previous: date | None = None
    observed_total = 0
    for index, candidate in enumerate(points):
        prefix = f"points[{index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"date", "count"}),
            optional=frozenset(),
            prefix=prefix,
        )
        current = _activity_day(candidate["date"], f"{prefix}date")
        if current < start or current > end:
            raise PartPayloadInvalid(f"{prefix}date", "must be within start and end")
        if previous is not None and current <= previous:
            raise PartPayloadInvalid(
                f"{prefix}date", "must be strictly ascending and unique"
            )
        count = candidate["count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= ACTIVITY_SERIES_MAX_COUNT
        ):
            raise PartPayloadInvalid(
                f"{prefix}count", "must be an integer from 1 through 1000000000"
            )
        previous = current
        observed_total += count
    if observed_total != total:
        raise PartPayloadInvalid("total", "must equal the sum of every sparse point")


def _math_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    trimmed: bool,
) -> int:
    """Validate one renderer-visible math string and return its UTF-8 size."""
    if not isinstance(value, str):
        raise PartPayloadInvalid(field, "must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise PartPayloadInvalid(field, "must be valid UTF-8 text") from None
    if size > maximum:
        raise PartPayloadInvalid(field, f"must be at most {maximum} UTF-8 bytes")
    if (
        value == ""
        or value.strip() == ""
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PartPayloadInvalid(field, "must be non-empty and control-free")
    if trimmed and value.strip() != value:
        raise PartPayloadInvalid(field, "must be trimmed")
    return size


def _validate_math_tokens(
    value: Any,
    *,
    field: str,
    depth: int,
    budget: list[int],
) -> None:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MATH_STEPS_MAX_TOKENS_PER_SEQUENCE
        or depth > MATH_STEPS_MAX_DEPTH
    ):
        raise PartPayloadInvalid(
            field, "must contain between 1 and 64 tokens at depth no greater than 4"
        )
    for index, candidate in enumerate(value):
        prefix = f"{field}[{index}]."
        budget[0] += 1
        if budget[0] > MATH_STEPS_MAX_NODES:
            raise PartPayloadInvalid(field, "must contain at most 256 token nodes")
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        kind = candidate.get("kind")
        if kind == "text":
            _require_exact_keys(
                candidate,
                required=frozenset({"kind", "text"}),
                optional=frozenset(),
                prefix=prefix,
            )
            budget[1] += _math_text(
                candidate["text"],
                field=f"{prefix}text",
                maximum=MATH_STEPS_MAX_TEXT_UTF8_BYTES,
                trimmed=False,
            )
        elif kind == "frac":
            _require_exact_keys(
                candidate,
                required=frozenset({"kind", "over", "under"}),
                optional=frozenset(),
                prefix=prefix,
            )
            _validate_math_tokens(
                candidate["over"],
                field=f"{prefix}over",
                depth=depth + 1,
                budget=budget,
            )
            _validate_math_tokens(
                candidate["under"],
                field=f"{prefix}under",
                depth=depth + 1,
                budget=budget,
            )
        elif kind in ("sup", "sub"):
            _require_exact_keys(
                candidate,
                required=frozenset({"kind", "tokens"}),
                optional=frozenset(),
                prefix=prefix,
            )
            _validate_math_tokens(
                candidate["tokens"],
                field=f"{prefix}tokens",
                depth=depth + 1,
                budget=budget,
            )
        else:
            raise PartPayloadInvalid(f"{prefix}kind", "must be text, frac, sup or sub")
        if budget[1] > MATH_STEPS_MAX_TOTAL_TEXT_UTF8_BYTES:
            raise PartPayloadInvalid(
                field, "label, notes and expressions must total at most 8192 UTF-8 bytes"
            )


def _validate_math_steps(payload: Mapping[str, Any]) -> None:
    """The exact closed structured-math AST accepted by Platform's Math Block."""
    _require_exact_keys(
        payload,
        required=frozenset({"steps"}),
        optional=frozenset({"label"}),
    )
    budget = [0, 0]
    if "label" in payload:
        budget[1] += _math_text(
            payload["label"],
            field="label",
            maximum=MATH_STEPS_MAX_LABEL_UTF8_BYTES,
            trimmed=True,
        )
    steps = payload["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MATH_STEPS_MAX_STEPS:
        raise PartPayloadInvalid("steps", "must contain between 1 and 16 steps")
    for index, candidate in enumerate(steps):
        prefix = f"steps[{index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"tokens"}),
            optional=frozenset({"note"}),
            prefix=prefix,
        )
        if "note" in candidate:
            budget[1] += _math_text(
                candidate["note"],
                field=f"{prefix}note",
                maximum=MATH_STEPS_MAX_NOTE_UTF8_BYTES,
                trimmed=True,
            )
        if budget[1] > MATH_STEPS_MAX_TOTAL_TEXT_UTF8_BYTES:
            raise PartPayloadInvalid(
                prefix[:-1],
                "label, notes and expressions must total at most 8192 UTF-8 bytes",
            )
        _validate_math_tokens(
            candidate["tokens"],
            field=f"{prefix}tokens",
            depth=0,
            budget=budget,
        )


def _validate_spec_sheet(payload: Mapping[str, Any]) -> None:
    """The exact bounded text-only shape mounted by Platform's Spec Sheet."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "items"}),
        optional=frozenset({"detail"}),
    )
    title = payload["title"]
    if not isinstance(title, str) or title == "":
        raise PartPayloadInvalid("title", "must be a non-empty string")
    if len(title.encode("utf-8")) > SPEC_SHEET_MAX_TITLE_UTF8_BYTES:
        raise PartPayloadInvalid("title", "must be at most 256 UTF-8 bytes")
    if "detail" in payload:
        detail = payload["detail"]
        if not isinstance(detail, str):
            raise PartPayloadInvalid("detail", "must be a string")
        if len(detail.encode("utf-8")) > SPEC_SHEET_MAX_DETAIL_UTF8_BYTES:
            raise PartPayloadInvalid("detail", "must be at most 4096 UTF-8 bytes")
    items = payload["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= SPEC_SHEET_MAX_ROWS:
        raise PartPayloadInvalid("items", "must contain between 1 and 64 rows")
    for index, candidate in enumerate(items):
        prefix = f"items[{index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"label", "value"}),
            optional=frozenset(),
            prefix=prefix,
        )
        label = candidate["label"]
        if not isinstance(label, str) or label == "":
            raise PartPayloadInvalid(f"{prefix}label", "must be a non-empty string")
        if len(label.encode("utf-8")) > SPEC_SHEET_MAX_LABEL_UTF8_BYTES:
            raise PartPayloadInvalid(f"{prefix}label", "must be at most 256 UTF-8 bytes")
        value = candidate["value"]
        if not isinstance(value, str):
            raise PartPayloadInvalid(f"{prefix}value", "must be a string")
        if len(value.encode("utf-8")) > SPEC_SHEET_MAX_VALUE_UTF8_BYTES:
            raise PartPayloadInvalid(f"{prefix}value", "must be at most 4096 UTF-8 bytes")


def _validate_comparison_matrix(payload: Mapping[str, Any]) -> None:
    """The exact bounded rectangular shape mounted by Platform's Comparison Card."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "columns", "items"}),
        optional=frozenset({"detail"}),
    )
    title = payload["title"]
    if not isinstance(title, str) or title == "":
        raise PartPayloadInvalid("title", "must be a non-empty string")
    if len(title.encode("utf-8")) > COMPARISON_MATRIX_MAX_LABEL_UTF8_BYTES:
        raise PartPayloadInvalid("title", "must be at most 256 UTF-8 bytes")
    if "detail" in payload:
        detail = payload["detail"]
        if not isinstance(detail, str):
            raise PartPayloadInvalid("detail", "must be a string")
        if len(detail.encode("utf-8")) > COMPARISON_MATRIX_MAX_DETAIL_UTF8_BYTES:
            raise PartPayloadInvalid("detail", "must be at most 4096 UTF-8 bytes")
    columns = payload["columns"]
    if not isinstance(columns, list) or not 2 <= len(columns) <= COMPARISON_MATRIX_MAX_OPTIONS:
        raise PartPayloadInvalid("columns", "must contain between 2 and 6 options")
    for index, column in enumerate(columns):
        if not isinstance(column, str) or column == "":
            raise PartPayloadInvalid(f"columns[{index}]", "must be a non-empty string")
        if len(column.encode("utf-8")) > COMPARISON_MATRIX_MAX_LABEL_UTF8_BYTES:
            raise PartPayloadInvalid(f"columns[{index}]", "must be at most 256 UTF-8 bytes")
    items = payload["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= COMPARISON_MATRIX_MAX_TRAITS:
        raise PartPayloadInvalid("items", "must contain between 1 and 12 traits")
    for index, candidate in enumerate(items):
        prefix = f"items[{index}]."
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(prefix[:-1], "must be an object")
        _require_exact_keys(
            candidate,
            required=frozenset({"label", "values"}),
            optional=frozenset(),
            prefix=prefix,
        )
        label = candidate["label"]
        if not isinstance(label, str) or label == "":
            raise PartPayloadInvalid(f"{prefix}label", "must be a non-empty string")
        if len(label.encode("utf-8")) > COMPARISON_MATRIX_MAX_LABEL_UTF8_BYTES:
            raise PartPayloadInvalid(f"{prefix}label", "must be at most 256 UTF-8 bytes")
        values = candidate["values"]
        if not isinstance(values, list) or len(values) != len(columns):
            raise PartPayloadInvalid(f"{prefix}values", "must match the option count")
        for value_index, value in enumerate(values):
            if not isinstance(value, str):
                raise PartPayloadInvalid(
                    f"{prefix}values[{value_index}]", "must be a string"
                )
            if len(value.encode("utf-8")) > COMPARISON_MATRIX_MAX_VALUE_UTF8_BYTES:
                raise PartPayloadInvalid(
                    f"{prefix}values[{value_index}]", "must be at most 4096 UTF-8 bytes"
                )


def _bounded_text(
    value: Any,
    field: str,
    *,
    maximum: int,
    nonempty: bool = True,
    control_free: bool = False,
    trimmed: bool = False,
) -> str:
    """One string field of the structured families below, at its exact rule."""
    if not isinstance(value, str):
        raise PartPayloadInvalid(field, "must be a string")
    if nonempty and value == "":
        raise PartPayloadInvalid(field, "must be non-empty")
    if len(value.encode("utf-8")) > maximum:
        raise PartPayloadInvalid(field, f"must be at most {maximum} UTF-8 bytes")
    if control_free and any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PartPayloadInvalid(field, "must be control-free")
    if trimmed and value.strip() != value:
        raise PartPayloadInvalid(field, "must be trimmed")
    return value


def _object_list(value: Any, field: str, *, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PartPayloadInvalid(field, f"must contain between {minimum} and {maximum} entries")
    for index, candidate in enumerate(value):
        if not isinstance(candidate, Mapping):
            raise PartPayloadInvalid(f"{field}[{index}]", "must be an object")
    return value


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PartPayloadInvalid(field, "must be an integer")
    if not minimum <= value <= maximum:
        raise PartPayloadInvalid(field, f"must be between {minimum} and {maximum}")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PartPayloadInvalid(field, "must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise PartPayloadInvalid(field, "must be finite")
    if value == 0 and isinstance(value, float) and math.copysign(1.0, value) < 0:
        raise PartPayloadInvalid(field, "must not be negative zero")
    return value


def _validate_titled_detail(
    payload: Mapping[str, Any], *, title_field: str, title_maximum: int, detail_maximum: int
) -> None:
    _bounded_text(payload[title_field], title_field, maximum=title_maximum)
    if "detail" in payload:
        _bounded_text(payload["detail"], "detail", maximum=detail_maximum, nonempty=False)


def _validate_timeline(payload: Mapping[str, Any]) -> None:
    """The exact bounded event sequence mounted by Platform's Timeline."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "items"}),
        optional=frozenset({"detail"}),
    )
    _validate_titled_detail(
        payload,
        title_field="title",
        title_maximum=TIMELINE_MAX_TEXT_UTF8_BYTES,
        detail_maximum=TIMELINE_MAX_DETAIL_UTF8_BYTES,
    )
    items = _object_list(payload["items"], "items", minimum=1, maximum=TIMELINE_MAX_ITEMS)
    for index, candidate in enumerate(items):
        prefix = f"items[{index}]."
        _require_exact_keys(
            candidate,
            required=frozenset({"label", "value", "state"}),
            optional=frozenset({"detail"}),
            prefix=prefix,
        )
        for field in ("label", "value"):
            _bounded_text(
                candidate[field], f"{prefix}{field}", maximum=TIMELINE_MAX_TEXT_UTF8_BYTES
            )
        if not isinstance(candidate["state"], str) or candidate["state"] not in TIMELINE_STATES:
            raise PartPayloadInvalid(f"{prefix}state", "must be past, now or future")
        if "detail" in candidate:
            _bounded_text(
                candidate["detail"],
                f"{prefix}detail",
                maximum=TIMELINE_MAX_DETAIL_UTF8_BYTES,
                nonempty=False,
            )


def _validate_score_breakdown(payload: Mapping[str, Any]) -> None:
    """The exact weighted score whose total is the weighted sum of its criteria."""
    _require_exact_keys(
        payload,
        required=frozenset({"verdict", "total", "outOf", "criteria"}),
        optional=frozenset(),
    )
    _bounded_text(
        payload["verdict"],
        "verdict",
        maximum=SCORE_BREAKDOWN_MAX_LABEL_UTF8_BYTES,
        control_free=True,
        trimmed=True,
    )
    out_of = _finite_number(payload["outOf"], "outOf")
    if not 0 < out_of <= SCORE_BREAKDOWN_MAX_OUT_OF:
        raise PartPayloadInvalid("outOf", "must be greater than 0 and at most 1000000")
    total = _finite_number(payload["total"], "total")
    if not 0 <= total <= out_of:
        raise PartPayloadInvalid("total", "must be between 0 and outOf")
    criteria = _object_list(
        payload["criteria"], "criteria", minimum=1, maximum=SCORE_BREAKDOWN_MAX_CRITERIA
    )
    labels: set[str] = set()
    weight_sum = 0
    weighted_score = 0.0
    for index, candidate in enumerate(criteria):
        prefix = f"criteria[{index}]."
        _require_exact_keys(
            candidate,
            required=frozenset({"label", "score", "weightBps"}),
            optional=frozenset({"note"}),
            prefix=prefix,
        )
        label = _bounded_text(
            candidate["label"],
            f"{prefix}label",
            maximum=SCORE_BREAKDOWN_MAX_LABEL_UTF8_BYTES,
            control_free=True,
            trimmed=True,
        )
        if label in labels:
            raise PartPayloadInvalid(f"{prefix}label", "must be unique")
        labels.add(label)
        if "note" in candidate:
            _bounded_text(
                candidate["note"],
                f"{prefix}note",
                maximum=SCORE_BREAKDOWN_MAX_NOTE_UTF8_BYTES,
                control_free=True,
                trimmed=True,
            )
        score = _finite_number(candidate["score"], f"{prefix}score")
        if not 0 <= score <= out_of:
            raise PartPayloadInvalid(f"{prefix}score", "must be between 0 and outOf")
        weight = _bounded_int(
            candidate["weightBps"],
            f"{prefix}weightBps",
            minimum=1,
            maximum=SCORE_BREAKDOWN_TOTAL_WEIGHT_BPS,
        )
        weight_sum += weight
        weighted_score += score * weight / SCORE_BREAKDOWN_TOTAL_WEIGHT_BPS
    if weight_sum != SCORE_BREAKDOWN_TOTAL_WEIGHT_BPS:
        raise PartPayloadInvalid("criteria", "weightBps must sum to 10000")
    if abs(weighted_score - total) > max(1e-9, out_of * 1e-12):
        raise PartPayloadInvalid("total", "must equal the weighted criterion score")


def _validate_code_diff(payload: Mapping[str, Any]) -> None:
    """The exact bounded single-file diff mounted by Platform's Code Diff."""
    _require_exact_keys(
        payload,
        required=frozenset({"file", "hunks"}),
        optional=frozenset({"detail"}),
    )
    _validate_titled_detail(
        payload,
        title_field="file",
        title_maximum=CODE_DIFF_MAX_FILE_UTF8_BYTES,
        detail_maximum=CODE_DIFF_MAX_DETAIL_UTF8_BYTES,
    )
    hunks = _object_list(payload["hunks"], "hunks", minimum=1, maximum=CODE_DIFF_MAX_HUNKS)
    total_lines = 0
    for index, hunk in enumerate(hunks):
        prefix = f"hunks[{index}]."
        _require_exact_keys(
            hunk,
            required=frozenset({"lines"}),
            optional=frozenset({"header"}),
            prefix=prefix,
        )
        if "header" in hunk:
            _bounded_text(
                hunk["header"],
                f"{prefix}header",
                maximum=CODE_DIFF_MAX_HEADER_UTF8_BYTES,
                nonempty=False,
            )
        lines = _object_list(
            hunk["lines"], f"{prefix}lines", minimum=1, maximum=CODE_DIFF_MAX_LINES_PER_HUNK
        )
        total_lines += len(lines)
        if total_lines > CODE_DIFF_MAX_TOTAL_LINES:
            raise PartPayloadInvalid("hunks", "must contain at most 2000 lines in total")
        for line_index, line in enumerate(lines):
            line_prefix = f"{prefix}lines[{line_index}]."
            _require_exact_keys(
                line,
                required=frozenset({"change", "text"}),
                optional=frozenset(),
                prefix=line_prefix,
            )
            if not isinstance(line["change"], str) or line["change"] not in CODE_DIFF_CHANGES:
                raise PartPayloadInvalid(
                    f"{line_prefix}change", "must be add, remove or context"
                )
            _bounded_text(
                line["text"],
                f"{line_prefix}text",
                maximum=CODE_DIFF_MAX_TEXT_UTF8_BYTES,
                nonempty=False,
            )


def _validate_file_tree(payload: Mapping[str, Any]) -> None:
    """The exact bounded relative path tree mounted by Platform's File Tree."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "items"}),
        optional=frozenset({"detail"}),
    )
    _validate_titled_detail(
        payload,
        title_field="title",
        title_maximum=FILE_TREE_MAX_TITLE_UTF8_BYTES,
        detail_maximum=FILE_TREE_MAX_DETAIL_UTF8_BYTES,
    )
    items = _object_list(payload["items"], "items", minimum=1, maximum=FILE_TREE_MAX_ITEMS)
    kinds: dict[str, str] = {}
    for index, candidate in enumerate(items):
        prefix = f"items[{index}]."
        _require_exact_keys(
            candidate,
            required=frozenset({"path", "name", "depth", "kind"}),
            optional=frozenset({"additions", "deletions"}),
            prefix=prefix,
        )
        path = _bounded_text(
            candidate["path"],
            f"{prefix}path",
            maximum=FILE_TREE_MAX_PATH_UTF8_BYTES,
            control_free=True,
        )
        segments = path.split("/")
        if (
            path.startswith("~")
            or "\\" in path
            or ":" in path
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise PartPayloadInvalid(f"{prefix}path", "must be a normalised relative path")
        if path in kinds:
            raise PartPayloadInvalid(f"{prefix}path", "must be unique")
        name = _bounded_text(
            candidate["name"], f"{prefix}name", maximum=FILE_TREE_MAX_NAME_UTF8_BYTES
        )
        if name != segments[-1]:
            raise PartPayloadInvalid(f"{prefix}name", "must equal the last path segment")
        depth = _bounded_int(
            candidate["depth"], f"{prefix}depth", minimum=0, maximum=FILE_TREE_MAX_DEPTH
        )
        if depth != len(segments) - 1:
            raise PartPayloadInvalid(f"{prefix}depth", "must equal the path depth")
        kind = candidate["kind"]
        if not isinstance(kind, str) or kind not in FILE_TREE_KINDS:
            raise PartPayloadInvalid(f"{prefix}kind", "must be folder or file")
        for field in ("additions", "deletions"):
            if field in candidate:
                if kind != "file":
                    raise PartPayloadInvalid(f"{prefix}{field}", "is allowed only on files")
                _bounded_int(
                    candidate[field],
                    f"{prefix}{field}",
                    minimum=0,
                    maximum=FILE_TREE_MAX_COUNT,
                )
        if depth > 0 and kinds.get("/".join(segments[:-1])) != "folder":
            raise PartPayloadInvalid(f"{prefix}path", "must follow its parent folder")
        kinds[path] = kind


def _panel_text(value: Any, field: str, maximum: int) -> int:
    """Non-empty, trimmed, control-free text; returns its UTF-8 byte length."""
    text = _bounded_text(value, field, maximum=maximum, control_free=True, trimmed=True)
    return len(text.encode("utf-8"))


def _generative_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload[field]
    if (
        not isinstance(value, str)
        or value == ""
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > GENERATIVE_UI_MAX_TEXT_UTF8_BYTES
    ):
        raise PartPayloadInvalid(field, "must be non-empty, trimmed, control-free and bounded")
    return value


def _validate_generative_ui(payload: Mapping[str, Any]) -> None:
    """One Core-computed chart, exactly as Platform's generative renderer decodes it."""
    _require_exact_keys(
        payload,
        required=frozenset({"variant", "title", "xLabel", "yLabel", "points"}),
        optional=frozenset(),
    )
    if payload["variant"] not in GENERATIVE_UI_CHART_VARIANTS:
        raise PartPayloadInvalid("variant", "must be one of the registered chart variants")
    for field in ("title", "xLabel", "yLabel"):
        _generative_text(payload, field)
    points = payload["points"]
    if not isinstance(points, list) or not 2 <= len(points) <= GENERATIVE_UI_MAX_POINTS:
        raise PartPayloadInvalid("points", f"must list 2 to {GENERATIVE_UI_MAX_POINTS} points")
    labels: set[str] = set()
    for index, point in enumerate(points):
        field = f"points[{index}]"
        if not isinstance(point, Mapping) or set(point) != {"label", "value"}:
            raise PartPayloadInvalid(field, "must be exactly label and value")
        label = _generative_text(point, "label")
        value = point["value"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or abs(value) > GENERATIVE_UI_MAX_ABS_VALUE
        ):
            raise PartPayloadInvalid(f"{field}.value", "must be a bounded finite number")
        if label in labels:
            raise PartPayloadInvalid(f"{field}.label", "must be unique")
        labels.add(label)


def _validate_core_report(payload: Mapping[str, Any]) -> None:
    """One closed internal request for a fixed Core report."""
    _require_exact_keys(payload, required=frozenset({"report"}), optional=frozenset())
    if payload["report"] not in CORE_REPORTS:
        raise PartPayloadInvalid("report", "must name one fixed Core report")


def _validate_server_panel(payload: Mapping[str, Any]) -> None:
    """The exact MCP server status Platform's MCP Server Panel projection admits."""
    _require_exact_keys(
        payload,
        required=frozenset({"title", "items"}),
        optional=frozenset({"detail", "state"}),
    )
    total = _panel_text(payload["title"], "title", SERVER_PANEL_MAX_TEXT_UTF8_BYTES)
    if "detail" in payload:
        total += _panel_text(payload["detail"], "detail", SERVER_PANEL_MAX_DETAIL_UTF8_BYTES)
    if "state" in payload and payload["state"] not in SERVER_PANEL_STATES:
        raise PartPayloadInvalid("state", "is not a display state")
    items = _object_list(payload["items"], "items", minimum=0, maximum=SERVER_PANEL_MAX_SERVERS)
    for index, server in enumerate(items):
        prefix = f"items[{index}]."
        _require_exact_keys(
            server,
            required=frozenset({"label", "detail", "state", "values"}),
            optional=frozenset(),
            prefix=prefix,
        )
        total += _panel_text(server["label"], f"{prefix}label", SERVER_PANEL_MAX_TEXT_UTF8_BYTES)
        total += _panel_text(server["detail"], f"{prefix}detail", SERVER_PANEL_MAX_TEXT_UTF8_BYTES)
        if not isinstance(server["state"], str) or server["state"] not in SERVER_PANEL_STATES:
            raise PartPayloadInvalid(f"{prefix}state", "is not a display state")
        tools = server["values"]
        if not isinstance(tools, list) or len(tools) > SERVER_PANEL_MAX_TOOLS_PER_SERVER:
            raise PartPayloadInvalid(f"{prefix}values", "must contain at most 64 tool names")
        seen: set[str] = set()
        for tool_index, tool in enumerate(tools):
            total += _panel_text(tool, f"{prefix}values[{tool_index}]", SERVER_PANEL_MAX_TEXT_UTF8_BYTES)
            if tool in seen:
                raise PartPayloadInvalid(f"{prefix}values[{tool_index}]", "must be unique")
            seen.add(tool)
        if total > SERVER_PANEL_MAX_TOTAL_UTF8_BYTES:
            raise PartPayloadInvalid("items", "must total at most 65536 UTF-8 bytes")


def _validate_rich_prose_table(payload: Mapping[str, Any]) -> None:
    """The exact one-table rich-prose subset emitted by the structured producer."""
    _require_exact_keys(payload, required=frozenset({"blocks"}), optional=frozenset())
    blocks = payload["blocks"]
    if not isinstance(blocks, list) or len(blocks) != 1:
        raise PartPayloadInvalid("blocks", "must contain exactly one table block")
    block = blocks[0]
    if not isinstance(block, Mapping):
        raise PartPayloadInvalid("blocks[0]", "must be an object")
    _require_exact_keys(
        block,
        required=frozenset({"kind", "cols", "rows"}),
        optional=frozenset(),
        prefix="blocks[0].",
    )
    if block["kind"] != "table":
        raise PartPayloadInvalid("blocks[0].kind", "must be table")
    columns = block["cols"]
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= DATA_TABLE_MAX_COLUMNS
    ):
        raise PartPayloadInvalid("blocks[0].cols", "must contain 1 to 16 columns")
    for index, column in enumerate(columns):
        if not isinstance(column, str) or column == "":
            raise PartPayloadInvalid(f"blocks[0].cols[{index}]", "must be a non-empty string")
        if len(column.encode("utf-8")) > DATA_TABLE_MAX_COLUMN_UTF8_BYTES:
            raise PartPayloadInvalid(
                f"blocks[0].cols[{index}]", "must be at most 256 UTF-8 bytes"
            )
    rows = block["rows"]
    if not isinstance(rows, list) or len(rows) > DATA_TABLE_MAX_ROWS:
        raise PartPayloadInvalid("blocks[0].rows", "must contain at most 100 rows")
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise PartPayloadInvalid(
                f"blocks[0].rows[{row_index}]", "must be a rectangular row"
            )
        for column_index, cell in enumerate(row):
            if not isinstance(cell, str):
                raise PartPayloadInvalid(
                    f"blocks[0].rows[{row_index}][{column_index}]", "must be a string"
                )
            if len(cell.encode("utf-8")) > DATA_TABLE_MAX_CELL_UTF8_BYTES:
                raise PartPayloadInvalid(
                    f"blocks[0].rows[{row_index}][{column_index}]",
                    "must be at most 4096 UTF-8 bytes",
                )


#: Every registered family, and the validator for its payload. Closed over the
#: families Core produces -- see the module docstring on producer-first.
_VALIDATORS: Final[Mapping[str, Any]] = {
    ACTIVITY_SERIES_PART_TYPE: _validate_activity_series,
    TEXT_PART_TYPE: _validate_text,
    ATTACHMENT_PART_TYPE: _validate_attachment,
    CHART_PART_TYPE: _validate_chart,
    CODE_DIFF_PART_TYPE: _validate_code_diff,
    COMPARISON_MATRIX_PART_TYPE: _validate_comparison_matrix,
    DIAGRAM_FLOW_PART_TYPE: _validate_diagram_flow,
    FILE_TREE_PART_TYPE: _validate_file_tree,
    REASONING_SUMMARY_PART_TYPE: _validate_reasoning_summary,
    TOOL_CALL_PART_TYPE: _validate_tool_call,
    WEB_SEARCH_PART_TYPE: _validate_web_search,
    GUARDRAIL_NOTICE_PART_TYPE: _validate_guardrail_notice,
    RESPONSE_FORMAT_PART_TYPE: _validate_response_format,
    RICH_PROSE_PART_TYPE: _validate_rich_prose_table,
    METRIC_NUMBER_PART_TYPE: _validate_metric_number,
    MATH_STEPS_PART_TYPE: _validate_math_steps,
    SPEC_SHEET_PART_TYPE: _validate_spec_sheet,
    SCORE_BREAKDOWN_PART_TYPE: _validate_score_breakdown,
    SERVER_PANEL_PART_TYPE: _validate_server_panel,
    GENERATIVE_UI_PART_TYPE: _validate_generative_ui,
    CORE_REPORT_PART_TYPE: _validate_core_report,
    TIMELINE_PART_TYPE: _validate_timeline,
}

#: The registered family names, for a caller that wants to report the registry
#: rather than validate against it.
REGISTERED_PART_TYPES: Final = tuple(sorted(_VALIDATORS))


def is_registered_part_type(part_type: str) -> bool:
    """Whether this registry states a shape for `part_type`.

    `False` is not a refusal. The contract's vocabulary is open, and an
    unregistered type is passed through and decoded to the inert fallback, as it
    always has been.
    """
    return part_type in _VALIDATORS


def validate_registered_payload(part_type: str, payload: Mapping[str, Any]) -> None:
    """Prove `payload` matches the registered shape of `part_type`, or raise.

    A no-op for an unregistered `part_type`, deliberately: this function's
    question is "is this payload wrong for a family I know", never "do I know
    this family". Raises :class:`PartPayloadInvalid` naming the field and the
    rule, with the offending value left out.
    """
    validator = _VALIDATORS.get(part_type)
    if validator is None:
        return
    if not isinstance(payload, Mapping):
        raise PartPayloadInvalid("payload", "must be an object")
    validator(payload)
