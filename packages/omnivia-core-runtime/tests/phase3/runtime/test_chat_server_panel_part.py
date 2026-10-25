"""An observed MCP server status on the user's turn is model-visible context."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import omnivia_core_runtime.service.chat_compaction as compaction_module
import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.service.chat_part_registry import (
    SERVER_PANEL_PART_TYPE,
    PartPayloadInvalid,
    validate_registered_payload,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from test_chat_request_manifest import WORKSPACE_ID

from omnivia_core.chat_contract.v1 import to_canonical_json

m1 = base.m1

SERVER_PANEL = {
    "title": "Dev MCP server",
    "detail": "Observed read-only with initialize and tools/list.",
    "items": [
        {
            "label": "omnivia-memory",
            "detail": "stdio · omnivia-dev-mcp",
            "state": "complete",
            "values": ["memory_store", "memory_list"],
        }
    ],
}


def _digest(value: Mapping[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(to_canonical_json(dict(value)).encode("utf-8")).hexdigest()
    )


@pytest.fixture
def seeded(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    base.seed_conversation_and_queue(holder)
    with base.writer(holder) as writer:
        writer.append_message_part(
            part_id="trigger-server-panel",
            message_id=base.TRIGGER_MESSAGE_ID,
            conversation_id=base.CONVERSATION_ID,
            part_index=1,
            part_type=SERVER_PANEL_PART_TYPE,
            schema_version=1,
            visibility="standard",
            payload=SERVER_PANEL,
            provenance="human",
            content_hash=_digest(SERVER_PANEL),
            created_at_us=base.BASE_US + 102,
        )
    yield holder
    holder.connection.close()


def test_observed_server_status_is_model_visible_in_ordinary_and_compacted_projection(
    seeded: m1.Owned,
) -> None:
    expected = {
        "kind": "text",
        "text": "MCP server status (observed read-only by the App Shell):\n"
        + to_canonical_json(SERVER_PANEL),
    }
    parts = compaction_module._message_text_parts(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        message_id=base.TRIGGER_MESSAGE_ID,
    )
    assert parts[-1] == expected
    assert executor_module._server_panel_provider_text(SERVER_PANEL) == expected["text"]


@pytest.mark.parametrize(
    "payload",
    (
        {**SERVER_PANEL, "controls": []},
        {**SERVER_PANEL, "state": "connected"},
        {**SERVER_PANEL, "items": [{**SERVER_PANEL["items"][0], "detail": ""}]},  # type: ignore[index]
        {
            **SERVER_PANEL,
            "items": [{**SERVER_PANEL["items"][0], "values": [f"t{i}" for i in range(65)]}],  # type: ignore[index]
        },
        {**SERVER_PANEL, "items": [SERVER_PANEL["items"][0]] * 33},  # type: ignore[index]
        {**SERVER_PANEL, "detail": "x" * 513},
    ),
)
def test_server_panel_rejects_shapes_platform_would_not_mount(payload: Mapping[str, Any]) -> None:
    with pytest.raises(PartPayloadInvalid):
        validate_registered_payload(SERVER_PANEL_PART_TYPE, payload)
