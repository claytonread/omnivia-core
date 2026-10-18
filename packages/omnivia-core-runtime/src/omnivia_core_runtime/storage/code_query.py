"""One applicable code-analysis import, read as a value for `code.query` (W13-A).

`code_analysis_import.py` writes accepted code facts as ordinary 0009 governed entities and
relations and 0074 decides, at read time, which import is applicable for a repository,
producer and profile. This module reads exactly one such import back, and adds no rule of
its own about what is current:

* **The scope is 0074's.** The import is the newest activation of the requested repository,
  producer and profile at the read instant -- the same selection `governed.py` applies. A
  scope with no activation, or whose active import analysed a different snapshot, is
  :class:`CodeAnalysisNotReady`, never an empty graph.
* **Currency is the resolver's.** Facts come from `resolve_governed_versions`, so a retired
  import's versions, a superseded version and anything recorded after the instant are
  simply absent. Membership in the one import is then what separates its facts from manual
  facts, other producers, other profiles and other imports: a version is read here only if
  0074 enrolled it as a member of *this* import.
* **Edges are the sealed endpoints.** A relation's type and its two ends come from its
  `omnivia_governed_relation_endpoints` row, joined on its own assembly, and both ends must
  be current entity members of the same import. Content is read for display and source
  spans, never to decide what an edge connects.
* **Diagnostics are what the import refused.** The candidates its policy did not accept,
  verbatim from 0074's diagnostics table. They are coverage information and never become
  edges.

Everything is read inside one transaction -- the activation, the resolved versions, the
members, the endpoints and the diagnostics -- so a cutover committed mid-read is wholly
visible or wholly invisible. Every statement runs under `authorised(..., mutations=False,
ddl=False)`; there is no writer here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from omnivia_core_runtime.storage.code_analysis_import import (
    CODE_RELATION_TYPES,
    DOMAIN_SCOPE,
)
from omnivia_core_runtime.storage.connection import authorised
from omnivia_core_runtime.storage.governed import resolve_governed_versions

NOT_READY_NO_ACTIVE_IMPORT: Final = "no_active_import"
NOT_READY_SNAPSHOT_NOT_ACTIVE: Final = "snapshot_not_active"

_ACTIVATIONS: Final = "omnivia_code_analysis_activations"

_ACTIVE: Final = (
    "SELECT i.import_id, i.repository_id, i.snapshot_id, i.producer_id, i.producer_version, "
    "       i.profile_digest, i.batch_digest, i.policy_id, i.policy_version, i.policy_digest, "
    "       i.manifest_evidence_id, a.activated_at_us "
    "FROM omnivia_code_analysis_activations a "
    "JOIN omnivia_code_analysis_imports i "
    "  ON i.workspace_id = a.workspace_id AND i.import_id = a.import_id "
    "WHERE a.workspace_id = ? AND a.repository_id = ? AND a.producer_id = ? "
    "  AND a.profile_digest = ? AND a.activated_at_us <= ? "
    "ORDER BY a.activation_sequence DESC LIMIT 1"
)


class CodeAnalysisNotReady(Exception):
    """No import for the requested scope is applicable for the requested snapshot."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CodeImportScope:
    """The one applicable import, exactly as 0074 records it."""

    import_id: str
    repository_id: str
    snapshot_id: str
    producer_id: str
    producer_version: str
    profile_digest: str
    batch_digest: str
    policy_id: str
    policy_version: str
    policy_digest: str
    manifest_evidence_id: str
    activated_at_us: int


@dataclass(frozen=True, slots=True)
class CodeFact:
    """One current governed version of the import and its decoded content."""

    record_id: str
    version_id: str
    content: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CodeEdge:
    """One accepted relation: its sealed type and ends, and its own fact."""

    relation_type: str
    source_key: str
    target_key: str
    fact: CodeFact


@dataclass(frozen=True, slots=True)
class CodeGraph:
    """One import's current facts and diagnostics, from one database state."""

    scope: CodeImportScope
    watermark_us: int
    #: Semantic key -> the definition's fact.
    symbols: Mapping[str, CodeFact]
    #: Every accepted edge, by edge key.
    edges: tuple[CodeEdge, ...]
    #: `(reason, candidate document)` for every edge candidate the policy refused.
    diagnostics: tuple[tuple[str, Mapping[str, Any]], ...]


def read_code_graph(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    snapshot_id: str,
    producer_id: str,
    profile_digest: str,
    instant_us: int,
) -> CodeGraph:
    """The applicable import for one scope at `instant_us`, or :class:`CodeAnalysisNotReady`."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        with authorised(connection, mutations=False, ddl=False) as fenced:
            migrated = fenced.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_ACTIVATIONS,)
            ).fetchone()
            row = (
                None
                if migrated is None
                else fenced.execute(
                    _ACTIVE, (workspace_id, repository_id, producer_id, profile_digest, instant_us)
                ).fetchone()
            )
        if row is None:
            raise CodeAnalysisNotReady(NOT_READY_NO_ACTIVE_IMPORT)
        # 0074's CHECKs fix every column's storage type: eleven texts, then an integer.
        scope = CodeImportScope(*row)
        if scope.snapshot_id != snapshot_id:
            raise CodeAnalysisNotReady(NOT_READY_SNAPSHOT_NOT_ACTIVE)
        # Performance note: resolves the workspace's whole frontier per query, as
        # graph.traverse and context_pack.build do. If profiling shows this path is
        # hot, push the import-member join into the resolver.
        versions = resolve_governed_versions(
            connection, workspace_id=workspace_id, resolution_instant_us=instant_us
        )
        with authorised(connection, mutations=False, ddl=False) as fenced:
            members = fenced.execute(
                "SELECT member_kind, governed_record_version_id "
                "FROM omnivia_code_analysis_import_members "
                "WHERE workspace_id = ? AND import_id = ? ORDER BY member_ordinal",
                (workspace_id, scope.import_id),
            ).fetchall()
            endpoints = fenced.execute(
                "SELECT m.governed_record_version_id, r.relation_type, "
                "       r.source_version_id, r.target_version_id "
                "FROM omnivia_code_analysis_import_members m "
                "JOIN omnivia_authoritative_governed_versions v "
                "  ON v.workspace_id = m.workspace_id "
                " AND v.governed_record_version_id = m.governed_record_version_id "
                "JOIN omnivia_governed_relation_endpoints r "
                "  ON r.workspace_id = v.workspace_id AND r.assembly_id = v.assembly_id "
                "WHERE m.workspace_id = ? AND m.import_id = ? AND m.member_kind = 'relation' "
                "ORDER BY m.member_ordinal",
                (workspace_id, scope.import_id),
            ).fetchall()
            diagnostics = fenced.execute(
                "SELECT reason_code, candidate_json "
                "FROM omnivia_code_analysis_import_diagnostics "
                "WHERE workspace_id = ? AND import_id = ? AND candidate_kind = 'edge' "
                "ORDER BY diagnostic_ordinal",
                (workspace_id, scope.import_id),
            ).fetchall()
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")

    current = {version.governed_record_version_id: version for version in versions}

    def fact(version_id: str, candidate_kind: str) -> CodeFact | None:
        version = current.get(version_id)
        if version is None or version.domain_scope != DOMAIN_SCOPE:
            return None
        content = json.loads(version.content_json)
        if content.get("import_id") != scope.import_id or content.get("candidate_kind") != candidate_kind:
            return None
        return CodeFact(version.governed_record_id, version_id, MappingProxyType(content))

    symbols: dict[str, CodeFact] = {}
    key_of: dict[str, str] = {}
    used = []
    for kind, version_id in members:
        if kind != "entity" or (found := fact(str(version_id), "node")) is None:
            continue
        symbols[str(found.content["semantic_key"])] = found
        key_of[found.version_id] = str(found.content["semantic_key"])
        used.append(current[found.version_id].recorded_at_us)
    edges = []
    for version_id, relation_type, source_version, target_version in endpoints:
        found = fact(str(version_id), "edge")
        if (
            found is None
            or relation_type not in CODE_RELATION_TYPES
            or source_version not in key_of
            or target_version not in key_of
        ):
            continue
        edges.append(CodeEdge(str(relation_type), key_of[source_version], key_of[target_version], found))
        used.append(current[found.version_id].recorded_at_us)
    return CodeGraph(
        scope=scope,
        watermark_us=max(scope.activated_at_us, *used),
        symbols=MappingProxyType(symbols),
        edges=tuple(sorted(edges, key=lambda edge: str(edge.fact.content["edge_key"]))),
        diagnostics=tuple(
            (str(reason), MappingProxyType(json.loads(document))) for reason, document in diagnostics
        ),
    )


__all__ = [
    "NOT_READY_NO_ACTIVE_IMPORT",
    "NOT_READY_SNAPSHOT_NOT_ACTIVE",
    "CodeAnalysisNotReady",
    "CodeEdge",
    "CodeFact",
    "CodeGraph",
    "CodeImportScope",
    "read_code_graph",
]
