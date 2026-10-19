"""`code.query` end to end (TECH-CORE-RETRIEVAL-CODE-001, W13-A).

The real Python and TypeScript producers analyse a really sealed captured snapshot, the W12
seam imports each batch under a registered owner policy into a real migrated workspace, and
every query below goes through the production read dispatcher -- the registry and the
local-owner session `service.main` wires -- and, where named, through the real local-IPC and
HTTP listeners. Nothing is mocked.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_code_analysis_import as w12
import test_graph_traverse as gt
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.service.application import (
    CODE_QUERY_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    LOCAL_TRANSPORT_ADAPTER,
    OPERATION_PURPOSES,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage.code_analysis_typescript import TypeScriptRuntime
from omnivia_core_runtime.storage.graph import read_graph_snapshot

from omnivia_core.contracts.v1 import (
    OPERATION_CATALOGUE,
    ErrorResponseEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

PY, TS = w12.PY, w12.TS
NOW = w12.NOW

_JOBS = b'"""Jobs."""\n\nfrom .models import make\n\n\ndef run(key: str) -> object:\n    return make(key)\n'
_APP = b'import { main } from "./main";\n\nexport function start(): number {\n  return 0;\n}\n'

#: W12's mixed repository plus a second Python `run` (same display name, another module)
#: and a TypeScript module that imports `main`, so dependencies have a second step.
REPOSITORY: dict[str, bytes] = {**w12.REPOSITORY, "pkg/jobs.py": _JOBS, "src/app.ts": _APP}
#: The second revision changes `pkg/service.py` only: `run` calls `make` twice.
REVISION: dict[str, bytes] = {
    **REPOSITORY,
    "pkg/service.py": w12.REVISION["pkg/service.py"],
}

runtime = w12.runtime


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Any]:
    opened = w12.esc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


@pytest.fixture
def blobs_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


class Imported:
    """Both producers' batches of one snapshot, imported under one owner policy."""

    def __init__(
        self, workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, *, files: dict[str, bytes],
        snapshot_id: str, base_us: int,
    ) -> None:
        self.snapshot = w12.Snapshot(
            workspace, blobs_root, runtime, files=files, snapshot_id=snapshot_id, base_us=base_us
        )
        self.python = self.snapshot.python()
        self.typescript = self.snapshot.typescript()


@pytest.fixture
def first(workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime) -> Imported:
    return Imported(
        workspace, blobs_root, runtime, files=REPOSITORY, snapshot_id="snap-1",
        base_us=1_700_000_000_000_000,
    )


@pytest.fixture
def imported(workspace: Any, blobs_root: Path, first: Imported) -> Imported:
    chosen = w12.policy(first.python, first.typescript)
    w12.register(workspace, chosen)
    w12.run(workspace, blobs_root, first.python, chosen, key="py-1", expected=None, now=NOW)
    w12.run(workspace, blobs_root, first.typescript, chosen, key="ts-1", expected=None, now=NOW + 1)
    return first


# --- the production read path --------------------------------------------------------


def read_operations() -> frozenset[str]:
    """Exactly what `service.main` grants the local owner: every registered read."""
    registry = build_application_registry()
    return frozenset(
        name for name in registry.operations if get_operation_metadata(name).scope.side_effect == "none"
    )


def dispatcher(service: object) -> ApplicationDispatcher:
    registry = build_application_registry()
    return ApplicationDispatcher(
        registry=registry,
        session=local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=gt.INSTALLATION_ID,
            workspace_id=gt.WORKSPACE_ID,
            operations=read_operations(),
        ),
        binding=ServiceBinding(installation_id=gt.INSTALLATION_ID, workspace_id=gt.WORKSPACE_ID),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({gt.WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            service,
        ),
        record=None,
        service=service,
    )


def payload(verified: Any, intent: str, **fields: Any) -> dict[str, Any]:
    batch = verified.batch
    return {
        "intent": intent,
        "repository_id": batch.repository_id,
        "snapshot_id": batch.snapshot_id,
        "producer_id": batch.producer_id,
        "profile_digest": batch.profile_digest,
        **fields,
    }


def call(workspace: Any, body: dict[str, Any], adapter: str = "in-process") -> ResponseEnvelope:
    request = gt.request_for(body, operation=CODE_QUERY_OPERATION)
    return s2._unrecorded_transport_call(adapter, dispatcher(workspace.holder), request)


def query(workspace: Any, body: dict[str, Any]) -> dict[str, Any]:
    response = call(workspace, body)
    assert isinstance(response, SuccessResponseEnvelope), response
    return dict(response.result)


def refusal(workspace: Any, body: dict[str, Any]) -> ErrorResponseEnvelope:
    response = call(workspace, body)
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def plain(value: Any) -> Any:
    """A decoded wire value (read-only mappings, tuples) as plain JSON values."""
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def stable(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result without the one clock read: the query instant."""
    copied: dict[str, Any] = plain(result)
    copied["freshness"].pop("as_of")
    return copied


def keys(items: list[dict[str, Any]]) -> list[str]:
    return [item["semantic_key"] for item in items]


def edges(result: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (r["depth"], r["source_key"], r["relation_type"], r["target_key"], r["type_only"])
        for r in result["relations"]
    ]


def members(workspace: Any, import_id: str) -> set[tuple[str, str]]:
    return {
        (str(record), str(version))
        for record, version in workspace.holder.connection.execute(
            "SELECT governed_record_id, governed_record_version_id "
            "FROM omnivia_code_analysis_import_members WHERE import_id = ?",
            (import_id,),
        )
    }


def records(result: dict[str, Any]) -> set[tuple[str, str]]:
    found = set()
    for name in ("definitions", "symbols", "relations"):
        for item in result[name]:
            found.add((item["record"]["record_id"], item["record"]["version"]))
    return found


# --- registration ---------------------------------------------------------------------


def test_code_query_is_a_shipped_read_beside_graph_traverse() -> None:
    entry = get_operation_metadata(CODE_QUERY_OPERATION)
    # Registered once, and appended after graph.traverse rather than slotted among the
    # frozen operations; later appended operations do not move it relative to that.
    names = [operation.name for operation in OPERATION_CATALOGUE]
    assert names.count(CODE_QUERY_OPERATION) == 1
    assert names.index(CODE_QUERY_OPERATION) > names.index("graph.traverse")
    assert entry.scope.side_effect == "none" and entry.scope.required_scopes == ("graph:read",)
    assert entry.required_capability.id == "graph.read"
    assert CODE_QUERY_OPERATION in build_application_registry().operations
    assert OPERATION_PURPOSES[CODE_QUERY_OPERATION] == KNOWLEDGE_RETRIEVAL_PURPOSE
    assert CODE_QUERY_OPERATION in read_operations()


# --- definitions ----------------------------------------------------------------------


def test_exact_and_same_name_definitions_are_deterministic_and_qualified(
    workspace: Any, imported: Imported
) -> None:
    py = imported.python
    exact = query(workspace, payload(py, "definition", semantic_key="pkg.models.make"))
    assert exact["resolution"] == "semantic_key"
    assert keys(exact["definitions"]) == ["pkg.models.make"]
    subject = exact["definitions"][0]
    assert subject["node_kind"] == "function" and subject["display_name"] == "make"
    assert subject["source"]["relative_path"] == "pkg/models.py"
    assert subject["source"]["content_digest"] == imported.snapshot.digests["pkg/models.py"]
    assert (subject["source"]["start_line"], subject["source"]["end_line"]) == (14, 15)
    assert exact["symbols"] == exact["relations"] == exact["diagnostics"] == []
    assert exact["coverage"] == {
        "analysis": "static", "complete": True, "truncated": False, "depth_bounded": False,
        "unattributed_inbound": False, "applied_depth": 0, "applied_limit": 100,
        "graph_expansion": "not_requested",
    }

    unique = query(workspace, payload(py, "definition", name="make"))
    assert unique["resolution"] == "unique_name"
    assert stable(unique)["definitions"] == stable(exact)["definitions"]

    ambiguous = query(workspace, payload(py, "callers", name="run"))
    assert ambiguous["resolution"] == "ambiguous"
    assert keys(ambiguous["definitions"]) == ["pkg.jobs.run", "pkg.service.run"]
    assert ambiguous["symbols"] == ambiguous["relations"] == []
    assert ambiguous["coverage"]["complete"] is False
    assert stable(query(workspace, payload(py, "callers", name="run"))) == stable(ambiguous)
    limited = query(workspace, payload(py, "definition", name="run", limit=1))
    assert keys(limited["definitions"]) == ["pkg.jobs.run"]
    assert limited["coverage"]["truncated"] is True

    # A key that names another definition than the name does, or a name nobody has.
    for body in (
        payload(py, "definition", semantic_key="pkg.models.make", name="run"),
        payload(py, "definition", name="no_such_thing"),
        payload(py, "definition", semantic_key="src/lib.ts:helper"),
    ):
        assert refusal(workspace, body).error.code == "not_found"


# --- references, callers, callees, dependencies ----------------------------------------


def test_resolved_references_carry_the_exact_occurrence(workspace: Any, imported: Imported) -> None:
    ts = imported.typescript
    result = query(workspace, payload(ts, "references", semantic_key="src/lib.ts:Shape"))
    assert edges(result) == [(1, "src/main.ts:main", "code.references", "src/lib.ts:Shape", True)]
    relation = result["relations"][0]
    assert relation["producer_relationship"] == "type_references"
    assert relation["evidence_strength"] == "resolved_static"
    source = relation["source"]
    main = REPOSITORY["src/main.ts"]
    assert source["relative_path"] == "src/main.ts"
    assert source["content_digest"] == imported.snapshot.digests["src/main.ts"]
    assert main[source["start_byte"] : source["end_byte"]] == b"Shape"
    assert keys(result["symbols"]) == ["src/main.ts:main"]
    assert result["coverage"]["unattributed_inbound"] is True
    assert result["coverage"]["complete"] is False
    # The type-only import of `Shape` is a `code.imports` fact, not a reference.
    assert all(r["relation_type"] == "code.references" for r in result["relations"])


def test_callers_and_callees_follow_static_calls_in_their_own_direction(
    workspace: Any, imported: Imported
) -> None:
    py = imported.python
    callers = query(workspace, payload(py, "callers", semantic_key="pkg.models.make"))
    assert edges(callers) == [
        (1, "pkg.jobs.run", "code.calls", "pkg.models.make", False),
        (1, "pkg.service.run", "code.calls", "pkg.models.make", False),
    ]
    assert keys(callers["symbols"]) == ["pkg.jobs.run", "pkg.service.run"]

    callees = query(workspace, payload(py, "callees", semantic_key="pkg.service.run", depth=2))
    assert edges(callees) == [
        (1, "pkg.service.run", "code.calls", "pkg.models.make", False),
        (2, "pkg.models.make", "code.calls", "pkg.models.Record", False),
    ]
    assert [(s["semantic_key"], s["depth"]) for s in callees["symbols"]] == [
        ("pkg.models.make", 1), ("pkg.models.Record", 2),
    ]
    direct = query(workspace, payload(py, "callees", semantic_key="pkg.service.run"))
    assert edges(direct) == edges(callees)[:1]
    assert direct["coverage"]["depth_bounded"] is True
    assert direct["coverage"]["applied_depth"] == 1
    assert callees["coverage"]["analysis"] == "static"


def test_dependencies_honour_depth_and_type_only_semantics(workspace: Any, imported: Imported) -> None:
    ts = imported.typescript
    module = query(workspace, payload(ts, "dependencies", semantic_key="src/main.ts"))
    assert edges(module) == [
        (1, "src/main.ts", "code.imports", "src/lib.ts:Base", False),
        (1, "src/main.ts", "code.imports", "src/lib.ts:Shape", True),
        (1, "src/main.ts", "code.imports", "src/lib.ts:helper", False),
    ]
    runtime_only = query(
        workspace, payload(ts, "dependencies", semantic_key="src/main.ts", runtime_only=True)
    )
    assert [edge[3] for edge in edges(runtime_only)] == ["src/lib.ts:Base", "src/lib.ts:helper"]
    # The external `fs` import is coverage, not a dependency edge.
    assert [d["target_descriptor"] for d in module["diagnostics"]] == ["package:fs#readFileSync"]
    assert module["diagnostics"][0]["reason"] == "external"

    direct = query(workspace, payload(ts, "dependencies", semantic_key="src/app.ts"))
    assert edges(direct) == [(1, "src/app.ts", "code.imports", "src/main.ts:main", False)]
    assert direct["coverage"]["depth_bounded"] is True
    transitive = query(workspace, payload(ts, "dependencies", semantic_key="src/app.ts", depth=2))
    assert edges(transitive) == [
        (1, "src/app.ts", "code.imports", "src/main.ts:main", False),
        (2, "src/main.ts:main", "code.references", "src/lib.ts:Base", True),
        (2, "src/main.ts:main", "code.references", "src/lib.ts:Shape", True),
    ]
    assert transitive["coverage"]["depth_bounded"] is False
    # Runtime-only walks no type-only step, so the second step disappears entirely.
    runtime_walk = query(
        workspace,
        payload(ts, "dependencies", semantic_key="src/app.ts", depth=2, runtime_only=True),
    )
    assert edges(runtime_walk) == edges(transitive)[:1]


def test_dynamic_and_unresolved_calls_are_only_coverage_diagnostics(
    workspace: Any, imported: Imported
) -> None:
    py, ts = imported.python, imported.typescript
    callees = query(workspace, payload(py, "callees", semantic_key="pkg.service.run"))
    assert {d["target_descriptor"] for d in callees["diagnostics"]} == {
        "dynamic-binding:record", "dynamic-call:Subscript",
    }
    assert {d["reason"] for d in callees["diagnostics"]} == {"unresolved"}
    assert all(r["target_key"] == "pkg.models.make" for r in callees["relations"])
    assert callees["coverage"]["complete"] is False

    # TypeScript resolved `main -> Base.run` only by dynamic dispatch: never a caller edge,
    # but disclosed against the subject it names.
    callers = query(workspace, payload(ts, "callers", semantic_key="src/lib.ts:Base.run"))
    assert callers["relations"] == [] and callers["symbols"] == []
    assert [(d["source_key"], d["target_key"], d["reason"]) for d in callers["diagnostics"]] == [
        ("src/main.ts:main", "src/lib.ts:Base.run", "inferred_dynamic")
    ]
    assert callers["coverage"]["unattributed_inbound"] is True
    # `main`'s own dynamic calls are call coverage; they say nothing about its dependencies.
    dependencies = query(workspace, payload(ts, "dependencies", semantic_key="src/main.ts:main"))
    assert [edge[3] for edge in edges(dependencies)] == ["src/lib.ts:Base", "src/lib.ts:Shape"]
    assert dependencies["diagnostics"] == []
    static = query(workspace, payload(ts, "callers", semantic_key="src/lib.ts:helper"))
    assert edges(static) == [(1, "src/main.ts:Derived.run", "code.calls", "src/lib.ts:helper", False)]


# --- binding, readiness and isolation --------------------------------------------------


def test_missing_or_mismatched_analysis_is_not_ready_never_empty(
    workspace: Any, blobs_root: Path, first: Imported
) -> None:
    py, ts = first.python, first.typescript
    body = payload(py, "definition", name="make")
    before = refusal(workspace, body).error
    assert before.code == "projection_unavailable"
    assert before.retry_class == "retryable_after_delay"
    assert dict(before.details or {}) == {"readiness": "not_ready", "reason": "no_active_import"}

    chosen = w12.policy(py, ts)
    w12.register(workspace, chosen)
    w12.run(workspace, blobs_root, py, chosen, key="py-1", expected=None, now=NOW)
    assert query(workspace, body)["binding"]["snapshot_id"] == "snap-1"
    other_profile = {**body, "profile_digest": "sha256:" + "0" * 64}
    other_producer = {**body, "producer_id": TS}
    unimported = payload(ts, "definition", name="helper")
    for wrong in (other_profile, other_producer, unimported):
        assert dict(refusal(workspace, wrong).error.details or {})["reason"] == "no_active_import"
    later = {**body, "snapshot_id": "snap-2"}
    assert dict(refusal(workspace, later).error.details or {})["reason"] == "snapshot_not_active"


def test_cutover_changes_the_answer_atomically_and_replay_adds_nothing(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, imported: Imported
) -> None:
    py = imported.python
    body = payload(py, "callees", semantic_key="pkg.service.run")
    old = query(workspace, body)
    assert old["binding"]["snapshot_id"] == "snap-1"
    assert len(old["relations"]) == 1

    second = Imported(
        workspace, blobs_root, runtime, files=REVISION, snapshot_id="snap-2",
        base_us=1_710_000_000_000_000,
    )
    chosen = w12.policy(second.python, second.typescript)
    receipt = w12.run(
        workspace, blobs_root, second.python, chosen, key="py-2",
        expected=old["binding"]["import_id"], now=NOW + 100,
    )
    new_body = payload(second.python, "callees", semantic_key="pkg.service.run")
    new = query(workspace, new_body)
    assert new["binding"]["snapshot_id"] == "snap-2"
    assert new["binding"]["import_id"] == receipt.import_id != old["binding"]["import_id"]
    assert [r["target_key"] for r in new["relations"]] == ["pkg.models.make", "pkg.models.make"]
    assert records(new) <= members(workspace, receipt.import_id)
    assert not records(new) & members(workspace, old["binding"]["import_id"])
    # The old binding is not ready any more; nothing of either import is mixed into it.
    assert dict(refusal(workspace, body).error.details or {})["reason"] == "snapshot_not_active"

    replayed = w12.run(
        workspace, blobs_root, second.python, chosen, key="py-2",
        expected=old["binding"]["import_id"], now=NOW + 100,
    )
    assert replayed == receipt
    assert stable(query(workspace, new_body)) == stable(new)


def test_manual_facts_and_the_other_producer_stay_out(workspace: Any, imported: Imported) -> None:
    w12.seed_manual_facts(workspace)
    py, ts = imported.python, imported.typescript
    connection = workspace.holder.connection
    import_of = {
        producer: str(row[0])
        for producer in (PY, TS)
        for row in connection.execute(
            "SELECT import_id FROM omnivia_code_analysis_imports WHERE producer_id = ?", (producer,)
        )
    }
    snapshot = read_graph_snapshot(
        connection, workspace_id=gt.WORKSPACE_ID, resolution_instant_us=NOW + 10**9
    )
    manual = {key for key, record in snapshot.records.items() if record.domain_scope != "engineering.code"}
    assert manual
    for verified, producer, body in (
        (py, PY, payload(py, "callers", semantic_key="pkg.models.make")),
        (ts, TS, payload(ts, "dependencies", semantic_key="src/main.ts")),
    ):
        result = query(workspace, body)
        assert result["binding"]["producer_id"] == producer
        assert records(result) <= members(workspace, import_of[producer])
        assert not records(result) & manual
        other = TS if producer == PY else PY
        assert not records(result) & members(workspace, import_of[other])
    # The other producer's key is not a definition of this one's analysis.
    assert refusal(workspace, payload(py, "definition", semantic_key="src/main.ts")).error.code == "not_found"


# --- the wire ---------------------------------------------------------------------------


def test_invalid_requests_are_refused_without_echoing_them(workspace: Any, imported: Imported) -> None:
    py = imported.python
    sentinel = "zzcallersentinel"
    for body in (
        payload(py, sentinel, name="make"),
        payload(py, "definition"),
        payload(py, "definition", name="make", depth=2),
        payload(py, "references", name="make", depth=1),
        payload(py, "callers", name="make", runtime_only=True),
        payload(py, "callers", name="make", depth=9),
        payload(py, "callers", semantic_key="has space"),
        {**payload(py, "definition", name="make"), "profile_digest": sentinel},
    ):
        error = refusal(workspace, body).error
        assert error.code == "invalid_request"
        assert sentinel not in error.message and "has space" not in error.message


@pytest.mark.parametrize("adapter", ("local-ipc", "http"))
def test_every_transport_returns_the_same_answer(
    workspace: Any, imported: Imported, adapter: str
) -> None:
    py = imported.python
    cases = (
        payload(py, "callers", semantic_key="pkg.models.make"),
        {**payload(py, "definition", name="make"), "snapshot_id": "snap-9"},
        payload(py, "teleport", name="make"),
    )
    for body in cases:
        local = call(workspace, body)
        remote = call(workspace, body, adapter)
        assert type(remote) is type(local)
        if isinstance(local, SuccessResponseEnvelope):
            assert isinstance(remote, SuccessResponseEnvelope)
            assert stable(remote.result) == stable(local.result)
        else:
            assert isinstance(local, ErrorResponseEnvelope) and isinstance(remote, ErrorResponseEnvelope)
            assert remote.error.to_wire() == local.error.to_wire()
