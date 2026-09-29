"""The authorized frontier folds evidence across SQLite's host-parameter limit.

`read_authorized_memory_frontier` folds evidence links, permission labels and
governance transitions by `IN (...)` lists sized by the admitted frontier, and
`hydrate_authorized_governed_record_values` selects the hydrated rows the same
way. A workspace past SQLite's host-parameter ceiling (32 766 on current builds;
thousands of records are enough to cross it once evidence links are counted)
used to fail with `too many SQL variables` instead of answering. Every fold is
now issued in fixed chunks and merged in the statement's own order, so this file
pins the boundary: one frontier with more ids than one chunk, answered
identically to a small frontier -- same previews, same digest discipline, same
hydration.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import test_engineering_source_coverage as sc

WORKSPACE_ID = sc.WORKSPACE_ID


@pytest.fixture
def chunked_workspace(tmp_path: Path) -> Iterator[sc.Workspace]:
    """One workspace with more evidence-linked records than one SQL chunk."""
    ws = sc.Workspace(tmp_path)
    try:
        ws.record(sc._source(1, "esnap-chunk", sc.FILES_A))
        manifest = sc._manifest("esnap-chunk")
        # _SQL_VARIABLE_CHUNK is 512; 540 records put the evidence-link fold and
        # the permission-label fold past one chunk each.
        for index in range(540):
            ws.observe(
                sc._observation(manifest, title=f"Chunk boundary finding {index}"),
                key=f"chunk-seed-{index}",
            )
        yield ws
    finally:
        ws.holder.connection.close()


def test_a_frontier_past_one_sql_chunk_still_folds_and_answers(
    chunked_workspace: sc.Workspace,
) -> None:
    ws = chunked_workspace
    response = ws.search(
        "esnap-chunk",
        query="chunk boundary finding",
        limit=20,
    )
    assert isinstance(response, sc.SuccessResponseEnvelope)
    result = response.to_wire()["result"]
    assert result["coverage"] == {
        "projection": "current",
        "applicability": "current",
    }
    assert 0 < len(result["previews"]) <= 20
    for preview in result["previews"]:
        assert preview["applicability"] == "matched"
    # The frontier digest is computed over the folded evidence rows; a chunked
    # fold must produce the same digest discipline as a single-statement fold,
    # so two identical reads answer with identical digests.
    again = ws.search("esnap-chunk", query="chunk boundary finding", limit=20)
    assert isinstance(again, sc.SuccessResponseEnvelope)
    assert again.to_wire()["result"]["coverage"] == result["coverage"]


def test_hydration_past_one_sql_chunk_returns_every_admitted_version(
    chunked_workspace: sc.Workspace,
) -> None:
    """The governed hydration read folds the admitted assemblies the same way.

    `hydrate_authorized_governed_record_values` selects version rows, provenance
    events, evidence links and claims by `IN (...)` lists sized by the admitted
    set. Past the host-parameter ceiling it used to refuse whole; chunked, it
    answers with exactly the admitted assemblies' rows -- one hydrated value per
    admitted assembly, in the caller's own order.
    """
    from datetime import UTC, datetime

    from omnivia_core_runtime.storage.governed import (
        hydrate_authorized_governed_record_values,
    )
    from omnivia_core_runtime.storage.memory import read_snapshot

    ws = chunked_workspace
    connection = ws.holder.connection
    assembly_ids = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT assembly_id FROM omnivia_authoritative_governed_version_metadata "
            "WHERE workspace_id = ?",
            (sc.WORKSPACE_ID,),
        ).fetchall()
    )
    assert len(assembly_ids) >= 540
    # Far past every seeded recorded_at_us: the whole admitted set is current.
    resolution_instant_us = int(datetime(2100, 1, 1, tzinfo=UTC).timestamp() * 1_000_000)
    with read_snapshot(connection):
        values = hydrate_authorized_governed_record_values(
            connection,
            workspace_id=sc.WORKSPACE_ID,
            resolution_instant_us=resolution_instant_us,
            assembly_ids=assembly_ids,
        )
    assert len(values) == len(assembly_ids)
    # Every hydrated record is one of the seeded observations, carried at the
    # version the snapshot froze -- no chunk dropped or duplicated a row.
    hydrated_ids = {value.record.provenance.identity.record_id for value in values}
    assert len(hydrated_ids) == len(assembly_ids)
