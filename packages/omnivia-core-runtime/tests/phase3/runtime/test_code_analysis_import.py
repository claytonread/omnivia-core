"""Governed code-analysis import and atomic repository applicability
(TECH-CORE-RETRIEVAL-CODE-001, work item W12; migration 0074).

Everything here runs the real producers over a really sealed `captured_v1` snapshot whose
bytes are really in a blob store, then imports through the one storage seam into a real
migrated workspace under `fenced_transaction` and 0009/0074's triggers. The graph is read
back through `read_graph_snapshot`, the production reader. Nothing is mocked.
"""

from __future__ import annotations

import copy
import dataclasses
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_captured_source_coverage as captured
import test_engineering_source_coverage as esc
import test_governed_truth_and_relations_migration as m3
import test_graph_traverse as graph_fixture
from omnivia_core_runtime.ownership.fencing import assert_guards_intact, guarded_tables
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage import code_analysis_import as subject
from omnivia_core_runtime.storage import code_analysis_python, code_analysis_typescript
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.backup import (
    backup_database,
    restore_backup,
    verify_backup,
)
from omnivia_core_runtime.storage.code_analysis_candidates import (
    CodeAnalysisCandidateError,
    CodeLocator,
    SourceBinding,
    VerifiedCodeAnalysisBatch,
)
from omnivia_core_runtime.storage.code_analysis_typescript import TypeScriptRuntime
from omnivia_core_runtime.storage.connection import (
    authorised,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
)
from omnivia_core_runtime.storage.governed import read_governed_record_values
from omnivia_core_runtime.storage.graph import GraphSnapshot, read_graph_snapshot
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    canonical_schema_tables,
    load_migrations,
)
from omnivia_core_runtime.workspace.blob_publication import publish_blob

WORKSPACE_ID = esc.WORKSPACE_ID
REPO_ROOT = Path(__file__).resolve().parents[5]
REPOSITORY_ID = "repo-code"
PY = code_analysis_python.PRODUCER_ID
TS = code_analysis_typescript.PRODUCER_ID
NOW = 1_750_000_000_000_000

_MODELS = '''"""Models."""

import json


class Record:
    def __init__(self, key: str) -> None:
        self.key = key

    def dump(self) -> str:
        return json.dumps({"key": self.key})


def make(key: str) -> Record:
    return Record(key)


def countdown(n: int) -> int:
    return countdown(n - 1) if n else 0
'''

_SERVICE = '''"""Service."""

from .models import make


def run(table: dict[str, object], key: str) -> object:
    record = make(key)
    record.dump()
    return table[key]()
'''

_LIB = """// café
export interface Shape {
  area(): number;
}

export class Base {
  run(): number {
    return 1;
  }
}

export function helper(x: number): number {
  return x + 1;
}
"""

_MAIN = """import { helper, Base } from "./lib";
import type { Shape } from "./lib";
import { readFileSync } from "fs";

class Derived extends Base {
  run(): number {
    return helper(2);
  }
}

export function main(shape: Shape, base: Base): number {
  void readFileSync;
  return base.run() + shape.area() + new Derived().run();
}
"""

REPOSITORY: dict[str, bytes] = {
    "pkg/__init__.py": b'"""Package."""\n\nfrom .models import Record\n',
    "pkg/models.py": _MODELS.encode(),
    "pkg/service.py": _SERVICE.encode(),
    "pkg/extra.py": b'"""Extra."""\n\n\ndef extra() -> int:\n    return 1\n',
    "src/lib.ts": _LIB.encode(),
    "src/main.ts": _MAIN.encode(),
}

#: The second revision: `pkg/extra.py` is deleted and `pkg/service.py` changed.
REVISION: dict[str, bytes] = {
    **{path: content for path, content in REPOSITORY.items() if path != "pkg/extra.py"},
    "pkg/service.py": _SERVICE.replace("record.dump()", "record.dump()\n    make(key)").encode(),
}

CODE_TABLES = (
    "omnivia_code_analysis_import_policies",
    "omnivia_code_analysis_imports",
    "omnivia_code_analysis_import_files",
    "omnivia_code_analysis_import_members",
    "omnivia_code_analysis_import_diagnostics",
    "omnivia_code_analysis_activations",
)
COUNTED = (
    *CODE_TABLES,
    "omnivia_application_audit_events",
    "omnivia_governed_records",
    "omnivia_governed_version_assemblies",
    "omnivia_governed_version_seals",
    "omnivia_governed_relation_endpoints",
    "omnivia_governed_extraction_lineage",
)


class Snapshot:
    """One sealed captured snapshot of the mixed repository, bytes in a blob store."""

    def __init__(
        self,
        workspace: Any,
        blobs_root: Path,
        runtime: TypeScriptRuntime,
        *,
        files: dict[str, bytes],
        snapshot_id: str,
        base_us: int,
        capture_status: str = "complete",
    ) -> None:
        self.workspace = workspace
        self.blobs_root = blobs_root
        self.runtime = runtime
        self.digests = {path: m2_digest(content) for path, content in files.items()}
        for path, content in files.items():
            publish_blob(blobs_root, self.digests[path], content)
        register_blobs(workspace, files, at=base_us)
        self.sealed = captured._seal(
            workspace,
            repository_id=REPOSITORY_ID,
            stream_id="stream-code",
            principal_id="core-service",
            checkout_id="checkout-code",
            snapshot_id=snapshot_id,
            files=self.digests,
            capture_status=capture_status,
            base_us=base_us,
        )

    def python(self) -> VerifiedCodeAnalysisBatch:
        verified = code_analysis_python.analyse_python_snapshot(
            self.workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            repository_id=REPOSITORY_ID,
            snapshot_id=self.sealed.snapshot_id,
            blobs_root=self.blobs_root,
        ).verified
        assert verified is not None
        return verified

    def typescript(self) -> VerifiedCodeAnalysisBatch:
        verified = code_analysis_typescript.analyse_typescript_snapshot(
            self.workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            repository_id=REPOSITORY_ID,
            snapshot_id=self.sealed.snapshot_id,
            blobs_root=self.blobs_root,
            runtime=self.runtime,
        ).verified
        assert verified is not None
        return verified


def m2_digest(content: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(content).hexdigest()


def register_blobs(workspace: Any, files: dict[str, bytes], *, at: int) -> None:
    """Register each captured file's blob object, as the capture's `_ensure_blob` does.

    `captured._seal` registers only its manifest's blob; a real capture registers every
    file's, and the import's file evidence is written over those registrations.
    """
    connection = workspace.holder.connection
    with captured._fenced(workspace):
        for content in files.values():
            digest = m2_digest(content)
            if connection.execute(
                "SELECT 1 FROM omnivia_blob_objects WHERE workspace_id = ? AND content_digest = ?",
                (WORKSPACE_ID, digest),
            ).fetchone() is None:
                connection.execute(
                    "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
                    "content_length_bytes, created_at_us, verified_at_us) VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE_ID, digest, len(content), at, at),
                )


@pytest.fixture(scope="module")
def runtime() -> TypeScriptRuntime:
    node = shutil.which("node")
    root = REPO_ROOT / "node_modules" / "typescript"
    assert node is not None, "node is required for the TypeScript producer"
    assert (root / "package.json").is_file(), "run npm ci so the pinned TypeScript compiler is available"
    return TypeScriptRuntime(node_executable=Path(node).resolve(), typescript_root=root)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Any]:
    opened = esc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


@pytest.fixture
def blobs_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


@pytest.fixture
def first(workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime) -> Snapshot:
    return Snapshot(
        workspace, blobs_root, runtime, files=REPOSITORY, snapshot_id="snap-1",
        base_us=1_700_000_000_000_000,
    )


def policy(
    *batches: VerifiedCodeAnalysisBatch,
    version: str = "1",
    relationships: bool = True,
    exclude_kinds: frozenset[str] = frozenset(),
    allow_reduced_coverage: bool = False,
) -> subject.CodeAnalysisImportPolicy:
    producers = sorted({verified.batch.producer_id for verified in batches})
    return subject.CodeAnalysisImportPolicy(
        policy_id="owner.code-import",
        policy_version=version,
        producers=tuple(
            sorted(
                {
                    (v.batch.producer_id, v.batch.producer_version, str(v.batch.profile_digest))
                    for v in batches
                }
            )
        ),
        accepted_node_kinds=tuple(
            (producer, kind)
            for producer in producers
            for kind in sorted(subject.NODE_KINDS[producer] - exclude_kinds)
        ),
        accepted_relationships=tuple(
            (producer, relationship, strength)
            for producer in producers
            for relationship in sorted(subject.RELATIONSHIPS[producer])
            for strength in sorted(subject.ACCEPTABLE_STRENGTHS)
        )
        if relationships
        else (),
        allow_reduced_coverage=allow_reduced_coverage,
    )


def settle(workspace: Any, *, principal: str, now: int, ref: str) -> MutationSettlementContext:
    """What `execute_mutation` does before its domain callback: the grant's audit event,
    then the settlement context naming it."""
    captured._audit(
        workspace.holder.connection, principal_id=principal,
        operation="code_analysis.policy.authorise", now_us=now, ref=ref,
    )
    return MutationSettlementContext(
        audit_ref=ref, claim_id=f"clm-{ref}", outcome_id=f"out-{ref}", settled_at_us=now
    )


def register(
    workspace: Any, chosen: subject.CodeAnalysisImportPolicy, now: int = NOW, principal: str = "local-user"
) -> str:
    with captured._fenced(workspace):
        settlement = settle(
            workspace, principal=principal, now=now, ref=f"aud-policy-{chosen.policy_version}-{now}"
        )
        return subject.register_import_policy(
            workspace.holder.connection, settlement, workspace_id=WORKSPACE_ID,
            principal_id=principal, policy=chosen,
        )


def run(
    workspace: Any,
    blobs_root: Path,
    verified: VerifiedCodeAnalysisBatch,
    chosen: subject.CodeAnalysisImportPolicy,
    *,
    key: str,
    expected: str | None,
    now: int,
) -> subject.CodeAnalysisActivationReceipt:
    with captured._fenced(workspace):
        return subject.import_code_analysis(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            verified=verified,
            policy=chosen,
            blobs_root=blobs_root,
            idempotency_key=key,
            expected_active_import_id=expected,
            now_us=now,
        )


def counts(workspace: Any) -> dict[str, int]:
    connection = workspace.holder.connection
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in COUNTED
    }


def graph(workspace: Any, at: int) -> GraphSnapshot:
    return read_graph_snapshot(
        workspace.holder.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=at
    )


def code_records(snapshot: GraphSnapshot, producer: str | None = None) -> dict[Any, Any]:
    return {
        key: record
        for key, record in snapshot.records.items()
        if record.domain_scope == subject.DOMAIN_SCOPE
        and (producer is None or record.content["producer_id"] == producer)
    }


def code_relations(snapshot: GraphSnapshot, producer: str | None = None) -> list[Any]:
    return [
        relation
        for relation in snapshot.relations
        if relation.record.domain_scope == subject.DOMAIN_SCOPE
        and (producer is None or relation.record.content["producer_id"] == producer)
    ]


def other_records(snapshot: GraphSnapshot) -> tuple[Any, ...]:
    return tuple(
        (key, record.to_wire())
        for key, record in sorted(snapshot.records.items())
        if record.domain_scope != subject.DOMAIN_SCOPE
    )


def other_relations(snapshot: GraphSnapshot) -> tuple[Any, ...]:
    return tuple(
        (r.relation_type, r.source, r.target, r.relation)
        for r in snapshot.relations
        if r.record.domain_scope != subject.DOMAIN_SCOPE
    )


def seed_manual_facts(workspace: Any) -> None:
    """Two human-governed claims and a governed relation between them: not code."""
    holder = workspace.holder
    with captured._fenced(workspace):
        for number in (1, 2, 3):
            m3.insert(holder.connection, "omnivia_application_audit_events", m3.audit_row(f"manual-{number}"))
    graph_fixture.seal(holder, record_id="rec-a", audit_ref="manual-1", recorded_at_us=graph_fixture.BASE_US + 1)
    graph_fixture.seal(holder, record_id="rec-b", audit_ref="manual-2", recorded_at_us=graph_fixture.BASE_US + 2)
    graph_fixture.seal(
        holder,
        record_id="rel-ab",
        audit_ref="manual-3",
        record_type="knowledge.relation",
        recorded_at_us=graph_fixture.BASE_US + 3,
        endpoint=("reconciliation.related_to", "rec-a", "rec-b"),
    )


# --- one authority, both real producers ---------------------------------------------


def test_python_and_typescript_batches_import_through_one_authority(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    python, typescript = first.python(), first.typescript()
    chosen = policy(python, typescript)
    register(workspace, chosen)
    receipts = {
        PY: run(workspace, blobs_root, python, chosen, key="import-py-1", expected=None, now=NOW),
        TS: run(workspace, blobs_root, typescript, chosen, key="import-ts-1", expected=None, now=NOW + 1),
    }
    snapshot = graph(workspace, NOW + 10)
    for producer, verified in ((PY, python), (TS, typescript)):
        receipt = receipts[producer]
        assert receipt.coverage_decision == "initial" and receipt.previous_import_id is None
        assert receipt.batch_digest == verified.batch.digest
        assert receipt.entity_count + receipt.relation_count + receipt.diagnostic_count == (
            len(verified.batch.nodes) + len(verified.batch.edges)
        )
        entities = code_records(snapshot, producer)
        relations = code_relations(snapshot, producer)
        nodes = [r for r in entities.values() if r.record_type == "knowledge.entity"]
        assert len(nodes) == receipt.entity_count
        assert len(relations) == receipt.relation_count > 0
        assert {r.relation_type for r in relations} <= subject.CODE_RELATION_TYPES
        # Every accepted edge joins two accepted entities of its own import.
        for relation in relations:
            assert relation.source in entities and relation.target in entities
            assert entities[relation.source].content["import_id"] == receipt.import_id
        assert receipt.file_count == len({b.source.relative_path for b in verified.batch.nodes})
    # The 0009 relation endpoints the graph walked are real rows on sealed governed versions.
    connection = workspace.holder.connection
    assert connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_relation_endpoints r "
        "JOIN omnivia_authoritative_governed_versions v "
        "  ON v.workspace_id = r.workspace_id AND v.assembly_id = r.assembly_id "
        "WHERE r.relation_type LIKE 'code.%' AND v.layer = 'governed'"
    ).fetchone()[0] == receipts[PY].relation_count + receipts[TS].relation_count


def test_accepted_facts_carry_exact_provenance_and_source_evidence(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    python, typescript = first.python(), first.typescript()
    chosen = policy(python, typescript)
    register(workspace, chosen)
    run(workspace, blobs_root, python, chosen, key="import-py-1", expected=None, now=NOW)
    run(workspace, blobs_root, typescript, chosen, key="import-ts-1", expected=None, now=NOW + 1)
    snapshot = graph(workspace, NOW + 10)
    by_key = {record.content.get("semantic_key"): record for record in code_records(snapshot).values()}
    node = next(n for n in python.batch.nodes if n.semantic_key == "pkg.models.Record")
    record = by_key["pkg.models.Record"]
    assert record.record_type == "knowledge.entity" and record.content["name"] == "Record"
    assert record.content["source"] == node.source.as_document()
    assert record.content["snapshot_id"] == "snap-1"
    assert record.content["profile_digest"] == python.batch.profile_digest
    assert record.authority_level == "canonical" and record.reviewer == chosen.policy_id
    identity = record.provenance.identity
    assert (identity.layer, identity.governance_state) == ("l2", "accepted")
    (accepted,) = record.provenance.history
    assert (accepted.action, accepted.actor_id, accepted.actor_kind) == (
        "governance.accepted",
        chosen.policy_id,
        "policy_evaluator",
    )
    # Two citations: the snapshot's manifest, which every fact shares, and the captured
    # file this fact was read from, which only that file's facts share.
    file_evidence = subject.file_evidence_id(
        REPOSITORY_ID, node.source.relative_path, node.source.content_digest
    )
    assert [source.source_id for source in record.provenance.sources] == [
        f"working-tree-manifest.{first.sealed.snapshot_id}", file_evidence
    ]
    artifact = workspace.holder.connection.execute(
        "SELECT source_kind, source_locator, blob_content_digest, media_type "
        "FROM omnivia_evidence_artifacts WHERE evidence_id = ?",
        (file_evidence,),
    ).fetchone()
    assert artifact == (
        subject.FILE_EVIDENCE_KIND, "pkg/models.py", first.digests["pkg/models.py"], "text/x-python"
    )
    # A relation cites the file of the occurrence that asserts it, not its target's.
    call = next(
        relation
        for relation in code_relations(snapshot, PY)
        if relation.record.content["producer_relationship"] == "calls"
        and relation.record.content["source"]["relative_path"] == "pkg/service.py"
    )
    assert [source.source_id for source in call.record.provenance.sources][1] == (
        subject.file_evidence_id(REPOSITORY_ID, "pkg/service.py", first.digests["pkg/service.py"])
    )

    # The sealed candidate predecessor is the producer's extraction, never a decision.
    candidates = {
        value.record.provenance.identity.record_id: value.record
        for value in read_governed_record_values(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            resolution_instant_us=NOW + 10,
            view="candidates",
        )
    }
    candidate = candidates[identity.record_id]
    assert candidate.content == record.content and candidate.reviewer is None
    assert [(e.action, e.actor_id) for e in candidate.provenance.history] == [
        ("candidate.extracted", PY)
    ]
    lineage = workspace.holder.connection.execute(
        "SELECT x.extraction_kind, x.extractor_id, x.extractor_version, x.configuration_digest "
        "FROM omnivia_governed_extraction_lineage x JOIN omnivia_governed_version_assemblies a "
        "ON a.workspace_id = x.workspace_id AND a.assembly_id = x.assembly_id "
        "WHERE a.governed_record_id = ?",
        (identity.record_id,),
    ).fetchall()
    assert lineage == [("deterministic", PY, python.batch.producer_version, python.batch.profile_digest)]

    # A type-only TypeScript edge keeps both its logical type and its type-only evidence.
    type_import = next(
        relation
        for relation in code_relations(snapshot, TS)
        if relation.record.content["producer_relationship"] == "type_imports"
    )
    content = type_import.record.content
    assert (type_import.relation_type, content["type_only"]) == ("code.imports", True)
    assert content["resolution_status"] == "resolved"
    assert content["evidence_strength"] in subject.ACCEPTABLE_STRENGTHS
    edge = next(e for e in typescript.batch.edges if e.edge_key == content["edge_key"])
    assert content["source"] == edge.source.as_document()


def test_unaccepted_facts_stay_diagnostics_and_never_inflate_the_graph(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    python, typescript = first.python(), first.typescript()
    chosen = policy(python, typescript)
    register(workspace, chosen)
    receipts = [
        run(workspace, blobs_root, python, chosen, key="import-py-1", expected=None, now=NOW),
        run(workspace, blobs_root, typescript, chosen, key="import-ts-1", expected=None, now=NOW + 1),
    ]
    connection = workspace.holder.connection
    reasons: dict[str, str] = {}
    for receipt in receipts:
        diagnostics = subject.read_import_diagnostics(
            connection, workspace_id=WORKSPACE_ID, import_id=receipt.import_id
        )
        assert len(diagnostics) == receipt.diagnostic_count
        reasons.update({key: reason for _, key, reason in diagnostics})
    accepted_edges = {
        relation.record.content["edge_key"] for relation in code_relations(graph(workspace, NOW + 10))
    }
    for verified in (python, typescript):
        for edge in verified.batch.edges:
            if edge.resolution_status != "resolved" or edge.evidence_strength == "inferred_dynamic":
                assert edge.edge_key not in accepted_edges
                expected = (
                    edge.resolution_status
                    if edge.resolution_status != "resolved"
                    else "inferred_dynamic"
                )
                assert reasons[edge.edge_key] == expected
    observed = set(reasons.values())
    assert {"unresolved", "external", "inferred_dynamic", "self_relation"} <= observed
    # The recursive `countdown` call is the self-relation 0009 cannot represent.
    recursive = next(
        e for e in python.batch.edges
        if e.source_key == "pkg.models.countdown" and e.target_key == "pkg.models.countdown"
    )
    assert reasons[recursive.edge_key] == "self_relation"


def test_policy_decides_and_enabling_analysis_is_not_consent(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    python = first.python()
    unregistered = policy(python)
    before = counts(workspace)
    with pytest.raises(subject.CodeAnalysisImportError, match="not been authorised"):
        run(workspace, blobs_root, python, unregistered, key="import-1", expected=None, now=NOW)
    assert counts(workspace) == before

    nodes_only = policy(python, relationships=False, exclude_kinds=frozenset({"method"}))
    register(workspace, nodes_only)
    receipt = run(workspace, blobs_root, python, nodes_only, key="import-1", expected=None, now=NOW)
    snapshot = graph(workspace, NOW + 10)
    assert receipt.relation_count == 0 and code_relations(snapshot) == []
    kinds = {record.content["node_kind"] for record in code_records(snapshot).values()}
    assert "method" not in kinds and "class" in kinds
    reasons = {
        key: reason
        for _, key, reason in subject.read_import_diagnostics(
            workspace.holder.connection, workspace_id=WORKSPACE_ID, import_id=receipt.import_id
        )
    }
    methods = {n.semantic_key for n in python.batch.nodes if n.kind == "method"}
    assert methods and all(reasons[key] == "node_kind_not_accepted" for key in methods)
    assert all(
        reasons[e.edge_key] in {"policy_not_accepted", "unresolved", "ambiguous", "external"}
        for e in python.batch.edges
    )


def test_the_authoriser_is_the_settlements_principal_never_the_policys_claim(
    workspace: Any, first: Snapshot
) -> None:
    chosen = policy(first.python())
    # The document is only the rules: there is no field a caller could fill with a name.
    assert "authorised_by" not in chosen.as_document()
    assert not hasattr(chosen, "authorised_by")
    connection = workspace.holder.connection
    before = counts(workspace)
    refusals = (
        # The audit names one principal and the caller claims another.
        ("local-user", "someone-else", NOW, NOW),
        # The settlement instant is not its audit's instant.
        ("local-user", "local-user", NOW, NOW + 1),
        # The service's own principal never consents for the owner.
        ("core-service", "core-service", NOW, NOW),
    )
    for number, (audited, claimed, audited_at, settled_at) in enumerate(refusals):
        with pytest.raises(sqlite3.IntegrityError, match="not its authoriser"), captured._fenced(workspace):
            settlement = settle(workspace, principal=audited, now=audited_at, ref=f"aud-bad-{number}")
            subject.register_import_policy(
                connection, dataclasses.replace(settlement, settled_at_us=settled_at),
                workspace_id=WORKSPACE_ID, principal_id=claimed, policy=chosen,
            )
        assert counts(workspace) == before, number
    # A settlement whose audit event does not exist at all.
    with pytest.raises(sqlite3.IntegrityError), captured._fenced(workspace):
        subject.register_import_policy(
            connection,
            MutationSettlementContext(audit_ref="aud-absent", claim_id="c", outcome_id="o", settled_at_us=NOW),
            workspace_id=WORKSPACE_ID, principal_id="local-user", policy=chosen,
        )
    assert counts(workspace) == before

    with captured._fenced(workspace):
        settlement = settle(workspace, principal="local-user", now=NOW, ref="aud-owner")
        audits = counts(workspace)["omnivia_application_audit_events"]
        digest = subject.register_import_policy(
            connection, settlement, workspace_id=WORKSPACE_ID, principal_id="local-user", policy=chosen
        )
        # The seam cites the executor's audit and writes none of its own.
        assert counts(workspace)["omnivia_application_audit_events"] == audits
    assert connection.execute(
        "SELECT policy_digest, authorised_by, audit_ref, recorded_at_us "
        "FROM omnivia_code_analysis_import_policies"
    ).fetchall() == [(digest, "local-user", "aud-owner", NOW)]


def test_policy_shape_is_bounded_to_the_mapped_vocabulary(first: Snapshot) -> None:
    python = first.python()
    base = policy(python)
    lock = base.producers
    for bad in (
        {"accepted_relationships": ((PY, "calls", "inferred_dynamic"),)},
        {"accepted_relationships": ((PY, "summons", "syntactic"),)},
        {"accepted_node_kinds": ((PY, "interface"),)},
        {"producers": (("analyzer.cobol", "1", lock[0][2]),)},
        {"producers": ()},
        {"accepted_node_kinds": ((PY, "class"), (PY, "class"))},
        {"max_candidates": 0},
    ):
        with pytest.raises(subject.CodeAnalysisImportError):
            dataclasses.replace(base, **bad)
    # The mapping covers exactly what each real producer emits, so a producer change
    # cannot silently widen or narrow the ontology.
    assert set(subject.RELATIONSHIPS[PY]) == code_analysis_python.RELATIONSHIP_TYPES
    assert set(subject.RELATIONSHIPS[TS]) == code_analysis_typescript.RELATIONSHIP_TYPES
    assert {mapped for table in subject.RELATIONSHIPS.values() for mapped, _ in table.values()} == (
        subject.CODE_RELATION_TYPES
    )


def test_an_arbitrary_worker_relationship_is_never_copied_into_the_ontology(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    python = first.python()
    edge = next(e for e in python.batch.edges if e.relationship_type == "calls" and e.resolution_status == "resolved"
                and e.source_key != e.target_key)
    forged_edge = dataclasses.replace(edge, relationship_type="summons", edge_key=edge.edge_key + "x")
    forged = dataclasses.replace(
        python, batch=dataclasses.replace(python.batch, edges=(*python.batch.edges, forged_edge))
    )
    chosen = policy(python)
    register(workspace, chosen)
    receipt = run(workspace, blobs_root, forged, chosen, key="import-1", expected=None, now=NOW)
    reasons = {
        key: reason
        for _, key, reason in subject.read_import_diagnostics(
            workspace.holder.connection, workspace_id=WORKSPACE_ID, import_id=receipt.import_id
        )
    }
    assert reasons[forged_edge.edge_key] == "relationship_unmapped"
    types = workspace.holder.connection.execute(
        "SELECT DISTINCT relation_type FROM omnivia_governed_relation_endpoints"
    ).fetchall()
    assert {str(row[0]) for row in types} <= subject.CODE_RELATION_TYPES


# --- identity, replay and the fence --------------------------------------------------


def test_replay_returns_the_same_receipt_and_a_reused_key_or_stale_fence_is_refused(
    workspace: Any, blobs_root: Path, first: Snapshot, runtime: TypeScriptRuntime, tmp_path: Path
) -> None:
    python, typescript = first.python(), first.typescript()
    chosen = policy(python, typescript)
    register(workspace, chosen)
    receipt = run(workspace, blobs_root, python, chosen, key="import-1", expected=None, now=NOW)
    after = counts(workspace)
    assert run(workspace, blobs_root, python, chosen, key="import-1", expected=None, now=NOW + 5) == receipt
    assert counts(workspace) == after
    with pytest.raises(subject.CodeAnalysisImportConflict, match="different import request"):
        run(workspace, blobs_root, typescript, chosen, key="import-1", expected=None, now=NOW + 6)
    with pytest.raises(subject.CodeAnalysisImportConflict, match="stale activation fence"):
        run(workspace, blobs_root, python, chosen, key="import-2", expected=None, now=NOW + 7)
    with pytest.raises(subject.CodeAnalysisImportConflict, match="stale activation fence"):
        run(workspace, blobs_root, python, chosen, key="import-3", expected="cai-other", now=NOW + 8)
    with pytest.raises(subject.CodeAnalysisImportConflict, match="stale activation fence"):
        run(workspace, blobs_root, python, chosen, key="import-4", expected=receipt.import_id, now=NOW - 1)
    assert counts(workspace) == after
    # Identity is content: a fresh workspace derives the very same ids.
    assert receipt.import_id.startswith("cai-") and len(receipt.import_id) == 68
    (tmp_path / "fresh").mkdir()
    fresh = esc.Workspace(tmp_path / "fresh")
    try:
        fresh_blobs = tmp_path / "fresh-blobs"
        fresh_blobs.mkdir()
        again = Snapshot(
            fresh, fresh_blobs, runtime, files=REPOSITORY, snapshot_id="snap-1",
            base_us=1_700_000_000_000_000,
        ).python()
        assert again.batch.digest == python.batch.digest
        register(fresh, chosen)
        replayed = run(fresh, fresh_blobs, again, chosen, key="import-1", expected=None, now=NOW)
        assert replayed == receipt
        assert code_records(graph(fresh, NOW + 10)).keys() == code_records(graph(workspace, NOW + 10)).keys()
    finally:
        fresh.holder.connection.close()


def test_cutover_is_atomic_isolated_and_retires_only_its_own_scope(
    workspace: Any, blobs_root: Path, first: Snapshot, runtime: TypeScriptRuntime
) -> None:
    seed_manual_facts(workspace)
    python, typescript = first.python(), first.typescript()
    second = Snapshot(
        workspace, blobs_root, runtime, files=REVISION, snapshot_id="snap-2",
        base_us=1_700_000_100_000_000,
    )
    python_2 = second.python()
    chosen = policy(python, typescript)
    register(workspace, chosen)
    manual_before = graph(workspace, NOW - 1)
    one = run(workspace, blobs_root, python, chosen, key="py-1", expected=None, now=NOW)
    ts = run(workspace, blobs_root, typescript, chosen, key="ts-1", expected=None, now=NOW + 1)
    at_one = graph(workspace, NOW + 2)
    assert other_records(at_one) == other_records(manual_before)
    assert other_relations(at_one) == other_relations(manual_before) != ()

    two = run(workspace, blobs_root, python_2, chosen, key="py-2", expected=one.import_id, now=NOW + 100)
    # `pkg/extra.py` was deleted, not dropped from coverage: no reduced-coverage receipt.
    assert (two.previous_import_id, two.coverage_decision, two.reduced_file_count) == (
        one.import_id,
        "complete",
        0,
    )
    at_two = graph(workspace, NOW + 200)
    current = code_records(at_two, PY)
    assert {record.content["import_id"] for record in current.values()} == {two.import_id}
    assert {record.content["snapshot_id"] for record in current.values()} == {"snap-2"}
    assert not any(r.content.get("semantic_key", "").startswith("pkg.extra") for r in current.values())
    assert all(
        relation.record.content["import_id"] == two.import_id for relation in code_relations(at_two, PY)
    )
    # The other producer and the manual facts survive the cutover untouched.
    assert {r.content["import_id"] for r in code_records(at_two, TS).values()} == {ts.import_id}
    assert other_records(at_two) == other_records(manual_before)
    assert other_relations(at_two) == other_relations(manual_before)
    assert at_two.watermark_us == NOW + 100
    # An earlier instant still reads the graph that was applicable then.
    assert {r.content["import_id"] for r in code_records(graph(workspace, NOW + 50), PY).values()} == {
        one.import_id
    }
    # Retirement is not supersession: nothing replaced the retired versions version for
    # version, so `history` does not hold them -- the as-of read above is how they are
    # read again -- while `candidates` keeps every staged extraction of both imports.
    def scoped(view: str) -> set[str]:
        return {
            str(value.record.content["import_id"])
            for value in read_governed_record_values(
                workspace.holder.connection, workspace_id=WORKSPACE_ID,
                resolution_instant_us=NOW + 200, view=view,
            )
            if value.record.domain_scope == subject.DOMAIN_SCOPE
            and value.record.content["producer_id"] == PY
        }

    assert scoped("history") == set()
    assert scoped("candidates") == {one.import_id, two.import_id}
    assert subject.active_import_id(
        workspace.holder.connection, workspace_id=WORKSPACE_ID, repository_id=REPOSITORY_ID,
        producer_id=PY, profile_digest=str(python.batch.profile_digest), instant_us=NOW + 50,
    ) == one.import_id

    # Rolling back re-activates the staged import: an activation and nothing else.
    staged = counts(workspace)
    back = run(workspace, blobs_root, python, chosen, key="py-3", expected=two.import_id, now=NOW + 300)
    assert back.import_id == one.import_id and back.activation_sequence == 3
    grown = counts(workspace)
    assert {t: grown[t] - staged[t] for t in COUNTED if grown[t] != staged[t]} == {
        "omnivia_code_analysis_activations": 1,
        "omnivia_application_audit_events": 1,
    }
    rolled = graph(workspace, NOW + 400)
    assert set(code_records(rolled, PY)) == set(code_records(at_one, PY))
    assert rolled.watermark_us == NOW + 300


def test_a_refused_cutover_leaves_the_previous_graph_and_reduced_coverage_needs_consent(
    workspace: Any, blobs_root: Path, first: Snapshot, runtime: TypeScriptRuntime
) -> None:
    python = first.python()
    chosen = policy(python)
    register(workspace, chosen)
    one = run(workspace, blobs_root, python, chosen, key="py-1", expected=None, now=NOW)
    # The second revision breaks `pkg/models.py` without deleting it, so the producer
    # covers one file fewer while the snapshot still holds it.
    broken = Snapshot(
        workspace, blobs_root, runtime,
        files={**REPOSITORY, "pkg/models.py": b"def broken(:\n    pass\n"},
        snapshot_id="snap-broken", base_us=1_700_000_200_000_000,
    )
    python_broken = broken.python()
    assert python_broken.batch.profile_digest == python.batch.profile_digest
    before_graph, before_rows = graph(workspace, NOW + 50), counts(workspace)
    with pytest.raises(subject.CodeAnalysisImportError, match="reduced coverage"):
        run(workspace, blobs_root, python_broken, chosen, key="py-2", expected=one.import_id, now=NOW + 100)
    # The refusal came after the whole import was staged, and rolled every row of it back.
    assert counts(workspace) == before_rows
    after_graph = graph(workspace, NOW + 150)
    assert set(after_graph.records) == set(before_graph.records)
    assert after_graph.watermark_us == before_graph.watermark_us

    consenting = policy(python, version="2", allow_reduced_coverage=True)
    register(workspace, consenting, now=NOW + 160)
    reduced = run(
        workspace, blobs_root, python_broken, consenting, key="py-3", expected=one.import_id, now=NOW + 200
    )
    assert (reduced.coverage_decision, reduced.reduced_file_count) == ("reduced_authorised", 1)
    assert not any(
        r.content["source"]["relative_path"] == "pkg/models.py"
        for r in code_records(graph(workspace, NOW + 300), PY).values()
    )


# --- fail closed ---------------------------------------------------------------------


def _with_locator(verified: VerifiedCodeAnalysisBatch, index: int, **changes: Any) -> VerifiedCodeAnalysisBatch:
    nodes = list(verified.batch.nodes)
    node = nodes[index]
    locator = dataclasses.replace(node.source.locator, **changes)
    nodes[index] = dataclasses.replace(node, source=dataclasses.replace(node.source, locator=locator))
    return dataclasses.replace(verified, batch=dataclasses.replace(verified.batch, nodes=tuple(nodes)))


def _forged_batch(verified: VerifiedCodeAnalysisBatch, **fields: Any) -> VerifiedCodeAnalysisBatch:
    """`verified` with batch fields set past the constructor's own checks."""
    batch = copy.copy(verified.batch)
    for name, value in fields.items():
        object.__setattr__(batch, name, value)
    return dataclasses.replace(verified, batch=batch)


def test_malformed_or_unprovable_input_fails_closed_and_writes_nothing(
    workspace: Any, blobs_root: Path, first: Snapshot, runtime: TypeScriptRuntime
) -> None:
    python, typescript = first.python(), first.typescript()
    chosen = policy(python, typescript)
    register(workspace, chosen)
    incomplete = Snapshot(
        workspace, blobs_root, runtime, files=REPOSITORY, snapshot_id="snap-partial",
        base_us=1_700_000_300_000_000, capture_status="incomplete",
    )
    before = counts(workspace)
    ts_nodes = typescript.batch.nodes
    lib = next(i for i, n in enumerate(ts_nodes) if n.source.relative_path == "src/lib.ts")
    lib_length = len(REPOSITORY["src/lib.ts"])
    py_index = next(i for i, n in enumerate(python.batch.nodes) if n.kind == "module")

    resolved = next(e for e in python.batch.edges if e.resolution_status == "resolved")
    dangling = dataclasses.replace(python.batch.edges[0])
    object.__setattr__(dangling, "target_key", "pkg.nowhere")
    object.__setattr__(dangling, "unresolved_target", None)
    object.__setattr__(dangling, "resolution_status", "resolved")
    other_digest = first.digests["pkg/extra.py"]
    wrong = dataclasses.replace(
        python.batch.nodes[py_index],
        source=SourceBinding(
            python.batch.nodes[py_index].source.relative_path,
            other_digest,
            python.batch.nodes[py_index].source.locator,
        ),
    )
    cases: list[tuple[VerifiedCodeAnalysisBatch, subject.CodeAnalysisImportPolicy]] = [
        # A byte span past the end of the captured file.
        (_with_locator(typescript, lib, end_byte=lib_length + 1), chosen),
        # A byte span starting inside the two-byte `é` (bytes 6-7 of `// café`).
        (_with_locator(typescript, lib, start_byte=7), chosen),
        # A line past the end of the file.
        (_with_locator(python, py_index, end_line=900), chosen),
        # A content digest that is not the one the capture index holds for the path.
        (dataclasses.replace(python, batch=dataclasses.replace(
            python.batch, nodes=tuple(wrong if i == py_index else n for i, n in enumerate(python.batch.nodes))
        )), chosen),
        # A resolved edge whose target is no node of the batch, forged past the batch
        # constructor (which refuses it, asserted below) so the seam's own rebuild has
        # to catch it.
        (_forged_batch(python, edges=(dangling, *python.batch.edges[1:])), chosen),
        # A snapshot whose capture is not complete.
        (dataclasses.replace(python, batch=dataclasses.replace(python.batch, snapshot_id="snap-partial")), chosen),
        # A verified envelope that does not name the snapshot's sealed manifest evidence.
        (dataclasses.replace(python, manifest_evidence_id="evd-forged"), chosen),
        # A producer profile the policy did not lock.
        (dataclasses.replace(python, batch=dataclasses.replace(python.batch, profile_digest="sha256:" + "0" * 64)), chosen),
    ]
    assert incomplete.sealed.snapshot_id == "snap-partial" and resolved is not None
    with pytest.raises(CodeAnalysisCandidateError, match="resolved edge target"):
        dataclasses.replace(python.batch, edges=(dangling, *python.batch.edges[1:]))
    # Each case is refused for its own reason, not an earlier one it happens to trip.
    reasons = (
        "runs past the end of its file",
        "splits a UTF-8 character",
        "line past the end of its file",
        "digest is not in the captured manifest",
        "resolved edge target is not a node",
        "source capture is incomplete",
        "disagrees with its sealed snapshot",
        "producer or profile is not locked",
    )
    for number, ((verified, used), reason) in enumerate(zip(cases, reasons, strict=True)):
        with pytest.raises(CodeAnalysisCandidateError, match=reason):
            run(workspace, blobs_root, verified, used, key=f"bad-{number}", expected=None, now=NOW)
        assert counts(workspace) == before, number
    assert code_records(graph(workspace, NOW + 10)) == {}
    assert isinstance(python.batch.nodes[py_index].source.locator, CodeLocator)


def test_the_seam_requires_the_callers_fenced_transaction(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    python = first.python()
    chosen = policy(python)
    with pytest.raises(subject.CodeAnalysisImportError, match="fenced write transaction"):
        subject.register_import_policy(
            workspace.holder.connection,
            MutationSettlementContext(audit_ref="aud-x", claim_id="c", outcome_id="o", settled_at_us=NOW),
            workspace_id=WORKSPACE_ID, principal_id="local-user", policy=chosen,
        )
    register(workspace, chosen)
    with pytest.raises(subject.CodeAnalysisImportError, match="fenced write transaction"):
        subject.import_code_analysis(
            workspace.holder.connection, workspace_id=WORKSPACE_ID, verified=python, policy=chosen,
            blobs_root=blobs_root, idempotency_key="k", expected_active_import_id=None, now_us=NOW,
        )
    # Inside a caller's transaction a refusal rolls back only the seam's own writes.
    with captured._fenced(workspace):
        captured._audit(
            workspace.holder.connection, principal_id="core-service", operation="caller.own", now_us=NOW,
            ref="aud-caller-own",
        )
        with pytest.raises(subject.CodeAnalysisImportError):
            subject.import_code_analysis(
                workspace.holder.connection, workspace_id=WORKSPACE_ID,
                verified=_with_locator(python, 0, end_line=900), policy=chosen, blobs_root=blobs_root,
                idempotency_key="k", expected_active_import_id=None, now_us=NOW,
            )
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_audit_events WHERE audit_ref = 'aud-caller-own'"
    ).fetchone() == (1,)


# --- migration 0074 ------------------------------------------------------------------


def test_0074_is_the_head_and_adds_six_guarded_tables(workspace: Any) -> None:
    assert max(m.version for m in load_migrations()) == 74
    assert max(applied_migrations(workspace.holder.connection)) == 74
    assert set(CODE_TABLES) <= set(canonical_schema_tables())
    assert set(CODE_TABLES) <= set(guarded_tables())
    assert_guards_intact(workspace.holder.connection)
    assert fingerprint_schema(workspace.holder.connection).matches(canonical_schema_fingerprint())
    seal = workspace.holder.connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'omnivia_guard_governed_version_seals_insert'"
    ).fetchone()[0]
    assert all(f"'{relation_type}'" in seal for relation_type in subject.CODE_RELATION_TYPES)


def _workspace_at_0073(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A workspace migrated through 0073 only, with manual governed facts in it."""
    through_0073 = tuple(m for m in load_migrations() if m.version <= 73)
    with monkeypatch.context() as old_schema:
        old_schema.setattr(migrations_module, "load_migrations", lambda: through_0073)
        canonical_schema_tables.cache_clear()
        canonical_schema_fingerprint.cache_clear()
        guarded_tables.cache_clear()
        old = esc.Workspace(tmp_path)
        assert max(applied_migrations(old.holder.connection)) == 73
        seed_manual_facts(old)
    canonical_schema_tables.cache_clear()
    canonical_schema_fingerprint.cache_clear()
    guarded_tables.cache_clear()
    return old


def _upgrade(old: Any) -> list[Any]:
    return apply_pending_migrations(
        old.holder.connection,
        mode=m2.OpenMode.SERVICE_OWNED,
        service_instance_id=old.holder.identity.service_instance_id,
        fencing_generation=old.holder.generation,
        workspace_id=WORKSPACE_ID,
    )


def test_0074_upgrades_a_0073_workspace_and_keeps_its_governed_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _workspace_at_0073(tmp_path, monkeypatch)
    rows_before = old.holder.connection.execute(
        "SELECT relation_type, source_record_id, target_record_id "
        "FROM omnivia_governed_relation_endpoints ORDER BY assembly_id"
    ).fetchall()
    try:
        applied = _upgrade(old)
        assert [m.version for m in applied] == [74]
        assert fingerprint_schema(old.holder.connection).matches(canonical_schema_fingerprint())
        assert integrity_check(old.holder.connection) == [] or integrity_check(old.holder.connection) == ["ok"]
        assert foreign_key_check(old.holder.connection) == []
        snapshot = graph(old, NOW)
        assert other_relations(snapshot) != ()
        assert old.holder.connection.execute(
            "SELECT relation_type, source_record_id, target_record_id "
            "FROM omnivia_governed_relation_endpoints ORDER BY assembly_id"
        ).fetchall() == rows_before
    finally:
        old.holder.connection.close()


def test_0074_refuses_to_replace_a_tampered_seal_trigger_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _workspace_at_0073(tmp_path, monkeypatch)
    connection = old.holder.connection
    seal = "omnivia_guard_governed_version_seals_insert"
    (original,) = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (seal,)
    ).fetchone()
    # A seal trigger someone already widened: 0074 must not silently layer onto it.
    tampered = original.replace(
        "'reconciliation.supports'\n", "'reconciliation.supports', 'code.launches'\n", 1
    )
    assert tampered != original
    try:
        with authorised(connection, ddl=True):
            connection.execute(f"DROP TRIGGER {seal}")
            connection.execute(tampered)
        with pytest.raises(sqlite3.DatabaseError, match="requires the 0009 governed seal trigger"):
            _upgrade(old)
        # The failed migration rolled back whole: still 0073, no 0074 table, the
        # tampered trigger exactly as it was.
        assert max(applied_migrations(connection)) == 73
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'omnivia_code_analysis_%'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (seal,)
        ).fetchone() == (tampered,)
    finally:
        connection.close()


def test_0074_tables_refuse_update_delete_and_unproven_rows(
    workspace: Any, blobs_root: Path, first: Snapshot
) -> None:
    seed_manual_facts(workspace)
    python = first.python()
    chosen = policy(python)
    register(workspace, chosen)
    receipt = run(workspace, blobs_root, python, chosen, key="py-1", expected=None, now=NOW)
    connection = workspace.holder.connection
    for table in CODE_TABLES:
        with captured._fenced(workspace), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"DELETE FROM {table}")
        with captured._fenced(workspace), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    manual_version = connection.execute(
        "SELECT governed_record_version_id FROM omnivia_authoritative_governed_versions "
        "WHERE governed_record_id = 'rec-a' AND layer = 'governed'"
    ).fetchone()[0]
    activation = dict(
        zip(
            ("repository_id", "producer_id", "profile_digest", "import_id"),
            connection.execute(
                "SELECT repository_id, producer_id, profile_digest, import_id "
                "FROM omnivia_code_analysis_activations"
            ).fetchone(),
            strict=True,
        )
    )
    refusals = [
        # A manual fact cannot be enrolled into (and later retired with) an import.
        ("INSERT INTO omnivia_code_analysis_import_members VALUES (?, ?, 999, 'entity', 'k', 'rec-a', ?)",
         (WORKSPACE_ID, receipt.import_id, manual_version), "already activated"),
        # The staged set of an activated import is closed.
        ("INSERT INTO omnivia_code_analysis_import_diagnostics VALUES (?, ?, 999, 'node', 'k', 'unresolved', '{}')",
         (WORKSPACE_ID, receipt.import_id), "already activated"),
        # A sequence that does not advance by one, or a predecessor that is not current.
        ("INSERT INTO omnivia_code_analysis_activations VALUES (?, ?, ?, ?, 5, ?, ?, 'k-5', ?, 'complete', 0, ?, ?)",
         (WORKSPACE_ID, activation["repository_id"], activation["producer_id"], activation["profile_digest"],
          activation["import_id"], activation["import_id"], "sha256:" + "1" * 64, receipt.audit_ref, NOW + 1),
         "stale code analysis activation fence"),
        ("INSERT INTO omnivia_code_analysis_activations VALUES (?, ?, ?, ?, 2, ?, NULL, 'k-6', ?, 'complete', 0, ?, ?)",
         (WORKSPACE_ID, activation["repository_id"], activation["producer_id"], activation["profile_digest"],
          activation["import_id"], "sha256:" + "1" * 64, receipt.audit_ref, NOW + 1),
         "CHECK constraint failed|stale"),
        # A coverage decision that misstates what the cutover drops.
        ("INSERT INTO omnivia_code_analysis_activations VALUES (?, ?, ?, ?, 2, ?, ?, 'k-7', ?, 'reduced_authorised', 3, ?, ?)",
         (WORKSPACE_ID, activation["repository_id"], activation["producer_id"], activation["profile_digest"],
          activation["import_id"], activation["import_id"], "sha256:" + "1" * 64, receipt.audit_ref, NOW + 1),
         "misstates its coverage"),
        # A diagnostic reason outside the closed vocabulary.
        ("INSERT INTO omnivia_code_analysis_import_diagnostics VALUES (?, 'cai-x', 1, 'node', 'k', 'trusted', '{}')",
         (WORKSPACE_ID,), "CHECK constraint failed"),
    ]
    for statement, parameters, message in refusals:
        with captured._fenced(workspace), pytest.raises(sqlite3.IntegrityError, match=message):
            connection.execute(statement, parameters)
    # Outside a fence every write is refused before any invariant is reached.
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "INSERT INTO omnivia_code_analysis_import_diagnostics VALUES (?, ?, 999, 'node', 'k', 'unresolved', '{}')",
            (WORKSPACE_ID, receipt.import_id),
        )


def test_the_seal_trigger_admits_only_the_bounded_code_namespace(
    workspace: Any, blobs_root: Path, first: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = first.python()
    chosen = policy(python)
    register(workspace, chosen)
    monkeypatch.setitem(
        subject.RELATIONSHIPS, PY, {**subject.RELATIONSHIPS[PY], "calls": ("code.launches", False)}
    )
    before = counts(workspace)
    with pytest.raises(sqlite3.IntegrityError, match="relation type, normalization or endpoint authority"):
        run(workspace, blobs_root, python, chosen, key="py-1", expected=None, now=NOW)
    assert counts(workspace) == before


def test_backup_and_restore_carry_the_applicable_code_graph(
    workspace: Any, blobs_root: Path, first: Snapshot, tmp_path: Path
) -> None:
    python = first.python()
    chosen = policy(python)
    register(workspace, chosen)
    receipt = run(workspace, blobs_root, python, chosen, key="py-1", expected=None, now=NOW)
    expected = graph(workspace, NOW + 10)
    source = workspace.holder.path
    # The online backup API needs no writer holding the lease: release the workspace
    # connection the way a quiesced service would, as `test_engineering_restore` does.
    workspace.holder.connection.close()
    backup = backup_database(source, tmp_path / "backup" / "workspace.sqlite")
    verify_backup(source, backup, "attempt-1")
    restored_path = restore_backup(backup, tmp_path / "restored" / "workspace.sqlite")
    restored = m2.take_ownership(restored_path)
    try:
        assert foreign_key_check(restored.connection) == []
        assert fingerprint_schema(restored.connection).matches(canonical_schema_fingerprint())
        snapshot = read_graph_snapshot(
            restored.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=NOW + 10
        )
        assert set(snapshot.records) == set(expected.records)
        assert snapshot.watermark_us == expected.watermark_us
        assert subject.active_import_id(
            restored.connection, workspace_id=WORKSPACE_ID, repository_id=REPOSITORY_ID,
            producer_id=PY, profile_digest=str(python.batch.profile_digest),
        ) == receipt.import_id
    finally:
        restored.connection.close()
