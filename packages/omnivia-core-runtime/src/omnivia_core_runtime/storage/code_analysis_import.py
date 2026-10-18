"""Governed import and atomic repository applicability for verified code analysis (W12).

The producers in `code_analysis_python` and `code_analysis_typescript` return values and
bind to nothing. This module is the one place those values become governed knowledge,
and it adds no authority of its own: every node it accepts is an ordinary 0009
`knowledge.entity` record and every edge an ordinary `knowledge.relation` record with a
sealed `omnivia_governed_relation_endpoints` row, written into the same tables, under the
same seal trigger and the same evidence rules every other governed writer uses. 0074
adds only the bounded `code.*` relation namespace and the applicability log.

**The analyser decides nothing.** Which facts are accepted is decided by an explicit,
versioned :class:`CodeAnalysisImportPolicy` that an owner authorised beforehand through
:func:`register_import_policy`. There is no default policy: enabling analysis records no
consent. Who authorised a policy is not part of the policy: the document is only the rules,
and the authoriser is the authenticated principal of the executor's settlement that
registers it, bound by 0074 to that settlement's own successful audit event. The policy can accept only what this module's own vocabulary names -- the node
kinds the two real producers emit and the relationships they emit, mapped once onto the
twelve `code.*` relation types 0074 lets seal -- and only `syntactic` or
`resolved_static` evidence. A producer string outside that mapping is never copied into
the ontology; it becomes a diagnostic. So does every `unresolved`, `ambiguous`,
`external` or `inferred_dynamic` edge, every relationship or node kind the policy did not
accept, every edge with an endpoint the policy did not accept, and every self-relation
(0009 refuses an endpoint row whose two records are one).

**Nothing the caller hands over is trusted.** :func:`import_code_analysis` rebuilds the
batch through the candidate constructors (so every structural invariant runs again),
re-runs `verify_batch_binding` against the sealed snapshot and requires the same stream,
sequence, manifest digest and manifest evidence it was handed, holds the producer and
profile to a lock the policy names, re-reads every captured blob, re-hashes it and checks
every locator against those exact bytes, and refuses the whole batch on any failure.

**Two versions per fact, because 0009 says so.** A governed accepted version needs a
sealed candidate predecessor of the same record. Each accepted fact is therefore an
`extracted`/`deterministic` candidate correlated to the import's extraction run, then a
governed version decided by the `policy_evaluator` -- the policy's id and version, never
an actor -- correlated to the import's audit. A governed relation's endpoints are the
governed entity versions; its candidate's are the candidate entity versions.

**Every fact cites the file it was read from as well as the snapshot.** Each provenance
event links two evidence artifacts: the snapshot's sealed manifest, which every fact of
the import shares, and the captured file its own source binding names, which only the
facts read out of that file share. The file artifact is ordinary 0008 L0 evidence over
the blob the capture already registered, one per repository path and content digest, so
its permission labels reach exactly that file's facts: a version's labels are the union
of what it cites, and a restriction on one file hides that file's definitions and the
relations asserted in it without touching any other file's. Nothing about a file
artifact is decided here beyond its identity; the labels are the label stream's.

**Staging then activation, in one transaction the caller owns.** The import rows, the
coverage manifest, the members and the diagnostics are staged first; the activation row
is written last, and 0074's trigger refuses it unless the fence, the staged counts and the
coverage decision all hold. The whole call runs inside a SAVEPOINT, so a refusal at any
point leaves the caller's transaction exactly as it was and the previous applicability
untouched. The caller must already hold a `fenced_transaction`.

**Identity is content.** The import id is derived from the repository, snapshot,
producer, profile, batch and policy; every record, version, assembly, event and seal id
from the import id and the candidate key. The same verified batch under the same policy
is therefore one import, and re-activating it -- a rollback to an earlier snapshot, say --
writes an activation and nothing else. A replay of the same idempotency key with the same
request returns the stored receipt; the same key with a different request is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage import code_analysis_python, code_analysis_typescript
from omnivia_core_runtime.storage.code_analysis_candidates import (
    MAX_CANDIDATES,
    CodeAnalysisBatch,
    CodeAnalysisCandidateError,
    CodeEdgeCandidate,
    CodeLocator,
    CodeNodeCandidate,
    SourceBinding,
    UnresolvedEndpoint,
    VerifiedCodeAnalysisBatch,
    revision_instance_id,
    verify_batch_binding,
)
from omnivia_core_runtime.storage.code_analysis_python import _captured_bytes
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest

IMPORT_FORMAT: Final = "code_analysis_import.v1"
POLICY_FORMAT: Final = "code_analysis_import_policy.v1"
DOMAIN_SCOPE: Final = "engineering.code"

#: The evidence kind and metadata format of one captured file a code fact cites.
FILE_EVIDENCE_KIND: Final = "engineering.captured_file"
FILE_EVIDENCE_FORMAT: Final = "code_analysis_file.v1"
#: Media types by suffix, for the files the two producers admit. None of them is a type
#: `evidence.search` reads content for, so a file artifact is findable by its identity
#: and path only: code content search is not something this module turns on.
_FILE_MEDIA_TYPES: Final[Mapping[str, str]] = {
    ".py": "text/x-python",
    ".pyi": "text/x-python",
    ".js": "text/javascript",
    ".cjs": "text/javascript",
    ".mjs": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/x-typescript",
    ".cts": "text/x-typescript",
    ".mts": "text/x-typescript",
    ".tsx": "text/x-typescript",
}

#: The relation types 0074's seal trigger admits beyond 0009's five, spelled exactly as
#: it spells them. A test seals each one; anything else cannot seal.
CODE_RELATION_TYPES: Final = frozenset(
    {
        "code.aliases",
        "code.calls",
        "code.constructs",
        "code.defines",
        "code.extends",
        "code.exports",
        "code.groups",
        "code.implements",
        "code.imports",
        "code.references",
        "code.reexports",
        "code.renders",
    }
)

#: Producer relationship -> (governed relation type, type-only). Keyed by producer, so a
#: string means what its own producer says it means and nothing a different producer
#: happens to spell the same way. Type-only and runtime edges share one relation type and
#: keep the distinction in content, which is where `producer_relationship` also survives.
RELATIONSHIPS: Final[Mapping[str, Mapping[str, tuple[str, bool]]]] = {
    code_analysis_python.PRODUCER_ID: {
        "calls": ("code.calls", False),
        "defines": ("code.defines", False),
        "imports": ("code.imports", False),
    },
    code_analysis_typescript.PRODUCER_ID: {
        "aliases": ("code.aliases", False),
        "calls": ("code.calls", False),
        "constructs": ("code.constructs", False),
        "defines": ("code.defines", False),
        "dynamic_imports": ("code.imports", False),
        "exports": ("code.exports", False),
        "extends": ("code.extends", False),
        "groups": ("code.groups", False),
        "implements": ("code.implements", False),
        "imports": ("code.imports", False),
        "reexports": ("code.reexports", False),
        "references": ("code.references", False),
        "renders": ("code.renders", False),
        "type_exports": ("code.exports", True),
        "type_imports": ("code.imports", True),
        "type_reexports": ("code.reexports", True),
        "type_references": ("code.references", True),
    },
}

NODE_KINDS: Final[Mapping[str, frozenset[str]]] = {
    code_analysis_python.PRODUCER_ID: code_analysis_python.NODE_KINDS,
    code_analysis_typescript.PRODUCER_ID: code_analysis_typescript.NODE_KINDS,
}

#: The only evidence a policy may bulk-accept. `inferred_dynamic` is a guess about which
#: override runs, and no policy can make a guess an applicable edge.
ACCEPTABLE_STRENGTHS: Final = frozenset({"syntactic", "resolved_static"})

#: Ceiling on one captured file re-read for span checks; the producers' own profiles cap
#: their files well below it.
MAX_FILE_BYTES: Final = 33_554_432

_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LINE_BREAK: Final = re.compile(rb"\r\n|\r|\n|\xe2\x80\xa8|\xe2\x80\xa9")
_ACTOR_KIND: Final = "service"
_ACTOR_ROLE: Final = "code_analysis_producer"
_IMPORT_PRINCIPAL: Final = "core-service"
_ACCEPTED_REASON: Final = "code_analysis.policy_accepted"


class CodeAnalysisImportError(CodeAnalysisCandidateError):
    """The import was refused; nothing it would have written exists."""


class CodeAnalysisImportConflict(CodeAnalysisImportError):
    """A reused idempotency key, a stale activation fence or a contradicting policy."""


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise CodeAnalysisImportError(f"{name} is not a bounded identifier")
    return value


def _derived(prefix: str, *parts: str) -> str:
    return f"{prefix}-" + hashlib.sha256(canonical_document(list(parts)).encode()).hexdigest()


def file_evidence_id(repository_id: str, relative_path: str, digest: str) -> str:
    """The evidence id of one captured file of one repository, as every import cites it.

    A pure function of the repository, the path and the bytes, so the same unchanged file
    is one artifact across snapshots and imports, and a reader can name the artifact a
    fact's own source binding cites without a second lookup.
    """
    return _derived("cfe", repository_id, relative_path, digest)


@dataclass(frozen=True, slots=True)
class CodeAnalysisImportPolicy:
    """What an owner authorised the import to accept, as a digestable value.

    It names no authoriser on purpose: a field here would be the caller's own claim.
    """

    policy_id: str
    policy_version: str
    #: `(producer_id, producer_version, profile_digest)` locks. A batch from any other
    #: producer build or profile is refused, not partly accepted.
    producers: tuple[tuple[str, str, str], ...]
    #: `(producer_id, node_kind)` pairs whose nodes may be accepted.
    accepted_node_kinds: tuple[tuple[str, str], ...]
    #: `(producer_id, relationship, evidence_strength)` triples whose resolved edges may
    #: be accepted.
    accepted_relationships: tuple[tuple[str, str, str], ...] = ()
    allow_reduced_coverage: bool = False
    max_candidates: int = MAX_CANDIDATES

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _identifier(self.policy_version, "policy_version")
        if not self.producers or not self.accepted_node_kinds:
            raise CodeAnalysisImportError("a policy must lock a producer and accept a node kind")
        for producer_id, producer_version, profile_digest in self.producers:
            if producer_id not in RELATIONSHIPS:
                raise CodeAnalysisImportError("policy names a producer this build does not know")
            _identifier(producer_version, "producer_version")
            if not isinstance(profile_digest, str) or re.fullmatch(
                r"sha256:[0-9a-f]{64}", profile_digest
            ) is None:
                raise CodeAnalysisImportError("policy producer lock needs a profile digest")
        for producer_id, kind in self.accepted_node_kinds:
            if kind not in NODE_KINDS.get(producer_id, ()):
                raise CodeAnalysisImportError("policy accepts a node kind no producer emits")
        for producer_id, relationship, strength in self.accepted_relationships:
            if relationship not in RELATIONSHIPS.get(producer_id, {}):
                raise CodeAnalysisImportError("policy accepts an unmapped relationship")
            if strength not in ACCEPTABLE_STRENGTHS:
                raise CodeAnalysisImportError("policy may accept only syntactic or resolved_static")
        for name in ("producers", "accepted_node_kinds", "accepted_relationships"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise CodeAnalysisImportError(f"policy {name} repeats an entry")
        if not isinstance(self.allow_reduced_coverage, bool):
            raise CodeAnalysisImportError("allow_reduced_coverage must be a boolean")
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or not 1 <= self.max_candidates <= MAX_CANDIDATES
        ):
            raise CodeAnalysisImportError("max_candidates is outside the contract's bounds")

    def as_document(self) -> dict[str, object]:
        return {
            "format": POLICY_FORMAT,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "producers": sorted(list(item) for item in self.producers),
            "accepted_node_kinds": sorted(list(item) for item in self.accepted_node_kinds),
            "accepted_relationships": sorted(
                list(item) for item in self.accepted_relationships
            ),
            "allow_reduced_coverage": self.allow_reduced_coverage,
            "max_candidates": self.max_candidates,
        }

    @property
    def digest(self) -> str:
        return content_digest(canonical_document(self.as_document()))


@dataclass(frozen=True, slots=True)
class CodeAnalysisActivationReceipt:
    """One activation, exactly as the applicability log and its import record it."""

    idempotency_key: str
    import_id: str
    activation_sequence: int
    previous_import_id: str | None
    repository_id: str
    snapshot_id: str
    producer_id: str
    profile_digest: str
    batch_digest: str
    policy_id: str
    policy_version: str
    policy_digest: str
    entity_count: int
    relation_count: int
    diagnostic_count: int
    file_count: int
    coverage_decision: str
    reduced_file_count: int
    audit_ref: str
    activated_at_us: int


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise CodeAnalysisImportError("the caller must hold the fenced write transaction")


def _audit(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    audit_ref: str,
    principal_id: str,
    operation: str,
    authority: Mapping[str, object],
    now_us: int,
) -> None:
    connection.execute(
        "INSERT INTO omnivia_application_audit_events "
        "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
        "correlation_id, trace_id, granted_authority_json, outcome_class, error_code, "
        "recorded_at_us) VALUES (?, ?, ?, ?, 'code_analysis.applicability', ?, ?, ?, ?, "
        "'succeeded', NULL, ?)",
        (
            audit_ref,
            workspace_id,
            principal_id,
            operation,
            audit_ref,
            audit_ref,
            audit_ref,
            canonical_document(dict(authority)),
            now_us,
        ),
    )


def register_import_policy(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    principal_id: str,
    policy: CodeAnalysisImportPolicy,
) -> str:
    """Record `principal_id`'s authorisation of `policy` under `settlement`; return its digest.

    The executor has already written the settlement's audit event from its server-issued
    grant, so this writes none: the row cites `settlement.audit_ref` at
    `settlement.settled_at_us`, and 0074 refuses it unless that audit succeeded at that
    instant for `principal_id`. Idempotent for the same policy; a different policy under
    the same id and version is a conflict, because a version identifies one set of rules
    forever.
    """
    _require_transaction(connection)
    _identifier(principal_id, "principal_id")
    row = connection.execute(
        "SELECT policy_digest FROM omnivia_code_analysis_import_policies "
        "WHERE workspace_id = ? AND policy_id = ? AND policy_version = ?",
        (workspace_id, policy.policy_id, policy.policy_version),
    ).fetchone()
    if row is not None:
        if str(row[0]) != policy.digest:
            raise CodeAnalysisImportConflict("that policy version is already a different policy")
        return policy.digest
    connection.execute(
        "INSERT INTO omnivia_code_analysis_import_policies "
        "(workspace_id, policy_id, policy_version, policy_digest, policy_json, "
        "authorised_by, allow_reduced_coverage, audit_ref, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            policy.policy_id,
            policy.policy_version,
            policy.digest,
            canonical_document(policy.as_document()),
            principal_id,
            int(policy.allow_reduced_coverage),
            settlement.audit_ref,
            settlement.settled_at_us,
        ),
    )
    return policy.digest


def active_import_id(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    producer_id: str,
    profile_digest: str,
    instant_us: int | None = None,
) -> str | None:
    """The import that is the current code graph for one scope, at `instant_us` or now."""
    row = connection.execute(
        "SELECT import_id FROM omnivia_code_analysis_activations "
        "WHERE workspace_id = ? AND repository_id = ? AND producer_id = ? "
        "AND profile_digest = ? AND (? IS NULL OR activated_at_us <= ?) "
        "ORDER BY activation_sequence DESC LIMIT 1",
        (workspace_id, repository_id, producer_id, profile_digest, instant_us, instant_us),
    ).fetchone()
    return None if row is None else str(row[0])


def read_import_policy(
    connection: sqlite3.Connection, *, workspace_id: str, policy_id: str, policy_version: str
) -> CodeAnalysisImportPolicy | None:
    """The registered policy of that id and version, rebuilt from its stored document.

    Rebuilt through the constructor, so every rule is checked again, and refused unless it
    digests to the digest the owner's registration recorded: a stored document that no
    longer is the policy it was is not a policy anything may import under.
    """
    row = connection.execute(
        "SELECT policy_json, policy_digest FROM omnivia_code_analysis_import_policies "
        "WHERE workspace_id = ? AND policy_id = ? AND policy_version = ?",
        (workspace_id, policy_id, policy_version),
    ).fetchone()
    if row is None:
        return None
    document = json.loads(str(row[0]))
    policy = CodeAnalysisImportPolicy(
        policy_id=document["policy_id"],
        policy_version=document["policy_version"],
        producers=tuple(tuple(item) for item in document["producers"]),
        accepted_node_kinds=tuple(tuple(item) for item in document["accepted_node_kinds"]),
        accepted_relationships=tuple(
            tuple(item) for item in document["accepted_relationships"]
        ),
        allow_reduced_coverage=document["allow_reduced_coverage"],
        max_candidates=document["max_candidates"],
    )
    if policy.digest != str(row[1]):
        raise CodeAnalysisImportError("a registered policy no longer digests to its record")
    return policy


def read_import_policies(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[tuple[CodeAnalysisImportPolicy, int], ...]:
    """Every registered policy with the instant it was recorded, most recent first."""
    rows = connection.execute(
        "SELECT policy_id, policy_version, recorded_at_us "
        "FROM omnivia_code_analysis_import_policies WHERE workspace_id = ? "
        "ORDER BY recorded_at_us DESC, policy_id DESC, policy_version DESC",
        (workspace_id,),
    ).fetchall()
    found = []
    for policy_id, policy_version, recorded_at_us in rows:
        policy = read_import_policy(
            connection,
            workspace_id=workspace_id,
            policy_id=str(policy_id),
            policy_version=str(policy_version),
        )
        assert policy is not None
        found.append((policy, int(recorded_at_us)))
    return tuple(found)


def current_activation_sequence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    producer_id: str,
    profile_digest: str,
) -> int:
    """The newest activation sequence of one scope, 0 when it has never been activated.

    The compare-and-swap token of the next activation: 0074 admits only the sequence one
    past this, naming the import this one retires.
    """
    row = connection.execute(
        "SELECT COALESCE(MAX(activation_sequence), 0) FROM omnivia_code_analysis_activations "
        "WHERE workspace_id = ? AND repository_id = ? AND producer_id = ? AND profile_digest = ?",
        (workspace_id, repository_id, producer_id, profile_digest),
    ).fetchone()
    return int(row[0])


def import_file_evidence_ids(
    connection: sqlite3.Connection, *, workspace_id: str, import_id: str
) -> tuple[str, ...]:
    """The file evidence every fact of one import cites, one artifact per covered file."""
    rows = connection.execute(
        "SELECT i.repository_id, f.path, f.content_digest "
        "FROM omnivia_code_analysis_import_files f "
        "JOIN omnivia_code_analysis_imports i "
        "  ON i.workspace_id = f.workspace_id AND i.import_id = f.import_id "
        "WHERE f.workspace_id = ? AND f.import_id = ? ORDER BY f.path",
        (workspace_id, import_id),
    ).fetchall()
    return tuple(file_evidence_id(str(repo), str(path), str(digest)) for repo, path, digest in rows)


def import_receipt(
    connection: sqlite3.Connection, *, workspace_id: str, idempotency_key: str
) -> CodeAnalysisActivationReceipt | None:
    """The receipt an activation under `idempotency_key` recorded, if one did."""
    stored = _receipt(connection, workspace_id=workspace_id, idempotency_key=idempotency_key)
    return None if stored is None else stored[1]


# --- re-verification ---------------------------------------------------------------


def _binding(binding: SourceBinding) -> SourceBinding:
    if type(binding) is not SourceBinding or type(binding.locator) is not CodeLocator:
        raise CodeAnalysisImportError("candidate source binding is not the contract's type")
    return SourceBinding(
        relative_path=binding.relative_path,
        content_digest=binding.content_digest,
        locator=CodeLocator(**binding.locator.as_document()),
    )


def _rebuilt(batch: CodeAnalysisBatch) -> CodeAnalysisBatch:
    """`batch`, reconstructed so every candidate invariant is checked again here."""
    if type(batch) is not CodeAnalysisBatch:
        raise CodeAnalysisImportError("the batch is not the contract's type")
    nodes = []
    for node in batch.nodes:
        if type(node) is not CodeNodeCandidate:
            raise CodeAnalysisImportError("a node is not the contract's type")
        nodes.append(
            CodeNodeCandidate(node.semantic_key, node.kind, node.display_name, _binding(node.source))
        )
    edges = []
    for edge in batch.edges:
        if type(edge) is not CodeEdgeCandidate:
            raise CodeAnalysisImportError("an edge is not the contract's type")
        unresolved = edge.unresolved_target
        edges.append(
            CodeEdgeCandidate(
                edge_key=edge.edge_key,
                source_key=edge.source_key,
                target_key=edge.target_key,
                unresolved_target=(
                    None if unresolved is None else UnresolvedEndpoint(unresolved.descriptor)
                ),
                relationship_type=edge.relationship_type,
                evidence_strength=edge.evidence_strength,
                resolution_status=edge.resolution_status,
                source=_binding(edge.source),
            )
        )
    return CodeAnalysisBatch(
        repository_id=batch.repository_id,
        snapshot_id=batch.snapshot_id,
        snapshot_kind=batch.snapshot_kind,
        producer_id=batch.producer_id,
        producer_version=batch.producer_version,
        nodes=tuple(nodes),
        edges=tuple(edges),
        profile_digest=batch.profile_digest,
    )


def _check_spans(batch: CodeAnalysisBatch, blobs_root: Path) -> None:
    """Every locator against the exact captured bytes its digest names."""
    bindings = tuple(node.source for node in batch.nodes) + tuple(e.source for e in batch.edges)
    files: dict[str, tuple[bytes, int, bool]] = {}
    for digest in sorted({binding.content_digest for binding in bindings}):
        content = _captured_bytes(blobs_root, digest, max_bytes=MAX_FILE_BYTES)
        if content is None:
            raise CodeAnalysisImportError("a captured source file exceeds the span-check ceiling")
        try:
            content.decode("utf-8")
            utf8 = True
        except UnicodeDecodeError:
            utf8 = False
        files[digest] = (content, len(_LINE_BREAK.findall(content)) + 1, utf8)
    for binding in bindings:
        content, lines, utf8 = files[binding.content_digest]
        locator = binding.locator
        if locator.end_line > lines:
            raise CodeAnalysisImportError("a locator names a line past the end of its file")
        if locator.start_byte is None or locator.end_byte is None:
            continue
        if locator.end_byte > len(content):
            raise CodeAnalysisImportError("a locator byte span runs past the end of its file")
        if utf8 and any(
            offset < len(content) and 0x80 <= content[offset] <= 0xBF
            for offset in (locator.start_byte, locator.end_byte)
        ):
            raise CodeAnalysisImportError("a locator byte span splits a UTF-8 character")


def _reverified(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    verified: VerifiedCodeAnalysisBatch,
    policy: CodeAnalysisImportPolicy,
    blobs_root: Path,
) -> VerifiedCodeAnalysisBatch:
    if type(verified) is not VerifiedCodeAnalysisBatch:
        raise CodeAnalysisImportError("the verified batch is not the contract's type")
    batch = _rebuilt(verified.batch)
    if batch.profile_digest is None:
        raise CodeAnalysisImportError("an import requires a profile-bound batch")
    lock = (batch.producer_id, batch.producer_version, batch.profile_digest)
    if lock not in policy.producers:
        raise CodeAnalysisImportError("the batch's producer or profile is not locked by the policy")
    if len(batch.nodes) + len(batch.edges) > policy.max_candidates:
        raise CodeAnalysisImportError("the batch exceeds the policy's output limit")
    proven = verify_batch_binding(connection, workspace_id=workspace_id, batch=batch)
    if (proven.stream_id, proven.sequence, proven.manifest_digest, proven.manifest_evidence_id) != (
        verified.stream_id,
        verified.sequence,
        verified.manifest_digest,
        verified.manifest_evidence_id,
    ):
        raise CodeAnalysisImportError("the verified batch disagrees with its sealed snapshot")
    _check_spans(batch, blobs_root)
    return proven


# --- classification ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    nodes: tuple[CodeNodeCandidate, ...]
    edges: tuple[tuple[CodeEdgeCandidate, str, bool], ...]
    diagnostics: tuple[tuple[str, str, str, dict[str, object]], ...]
    files: tuple[tuple[str, str], ...]


def _plan(batch: CodeAnalysisBatch, policy: CodeAnalysisImportPolicy) -> _Plan:
    kinds = {kind for producer, kind in policy.accepted_node_kinds if producer == batch.producer_id}
    accepted_edges = {
        (relationship, strength)
        for producer, relationship, strength in policy.accepted_relationships
        if producer == batch.producer_id
    }
    mapping = RELATIONSHIPS.get(batch.producer_id, {})
    diagnostics: list[tuple[str, str, str, dict[str, object]]] = []
    nodes: list[CodeNodeCandidate] = []
    for node in sorted(batch.nodes, key=lambda item: item.semantic_key):
        if node.kind in kinds:
            nodes.append(node)
        else:
            diagnostics.append(("node", node.semantic_key, "node_kind_not_accepted", node.as_document()))
    accepted_keys = {node.semantic_key for node in nodes}
    edges: list[tuple[CodeEdgeCandidate, str, bool]] = []
    for edge in sorted(batch.edges, key=lambda item: item.edge_key):
        mapped = mapping.get(edge.relationship_type)
        if edge.resolution_status != "resolved":
            reason = edge.resolution_status
        elif edge.evidence_strength == "inferred_dynamic":
            reason = "inferred_dynamic"
        elif mapped is None:
            reason = "relationship_unmapped"
        elif (edge.relationship_type, edge.evidence_strength) not in accepted_edges:
            reason = "policy_not_accepted"
        elif edge.source_key not in accepted_keys or edge.target_key not in accepted_keys:
            reason = "endpoint_not_accepted"
        elif edge.source_key == edge.target_key:
            reason = "self_relation"
        else:
            edges.append((edge, *mapped))
            continue
        diagnostics.append(("edge", edge.edge_key, reason, edge.as_document()))
    bindings = [node.source for node in batch.nodes] + [edge.source for edge in batch.edges]
    files = tuple(sorted({(b.relative_path, b.content_digest) for b in bindings}))
    return _Plan(tuple(nodes), tuple(edges), tuple(diagnostics), files)


# --- governed writes ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Import:
    workspace_id: str
    import_id: str
    run_id: str
    audit_ref: str
    evidence_id: str
    policy: CodeAnalysisImportPolicy
    now_us: int
    repository_id: str


def _ids(imp: _Import, kind: str, key: str) -> dict[str, str]:
    base = (imp.import_id, kind, key)
    return {
        "record": _derived("cgr", *base),
        "candidate_version": _derived("cgv-c", *base),
        "governed_version": _derived("cgv-g", *base),
        "candidate_assembly": _derived("cga-c", *base),
        "governed_assembly": _derived("cga-g", *base),
    }


def _insert_assembly(
    connection: sqlite3.Connection,
    imp: _Import,
    *,
    governed: bool,
    record_id: str,
    version_id: str,
    assembly_id: str,
    record_type: str,
    content_json: str,
    actor_id: str,
    ordinal: int,
) -> None:
    policy = imp.policy
    connection.execute(
        "INSERT INTO omnivia_governed_version_assemblies "
        "(workspace_id, assembly_id, governed_record_id, governed_record_version_id, "
        "record_type, domain_scope, layer, authority_level, governance_disposition, "
        "candidate_origin, extraction_kind, decision_source_kind, decision_source_id, "
        "authority_policy_id, authority_policy_version, policy_decision_ref, "
        "content_schema_version, content_json, content_digest, evidence_disposition, "
        "confidence_ppm, assertion_actor_id, assertion_actor_kind, assertion_actor_role, "
        "reason_code, reason_comment, valid_from_us, valid_to_us, recorded_at_us, "
        "append_ordinal, correlation_kind, correlation_id, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '1.0', ?, ?, 'available', "
        "NULL, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)",
        (
            imp.workspace_id,
            assembly_id,
            record_id,
            version_id,
            record_type,
            DOMAIN_SCOPE,
            "governed" if governed else "candidate",
            "canonical" if governed else "proposed",
            "accepted" if governed else None,
            None if governed else "extracted",
            None if governed else "deterministic",
            "policy_evaluator" if governed else None,
            policy.policy_id if governed else None,
            policy.policy_id if governed else None,
            policy.policy_version if governed else None,
            imp.import_id if governed else None,
            content_json,
            content_digest(content_json),
            actor_id,
            _ACTOR_KIND,
            _ACTOR_ROLE,
            imp.now_us,
            imp.now_us,
            ordinal,
            "m1_audit" if governed else "extraction_run",
            imp.audit_ref if governed else imp.run_id,
            imp.audit_ref if governed else None,
        ),
    )


def _insert_event(
    connection: sqlite3.Connection,
    imp: _Import,
    *,
    governed: bool,
    assembly_id: str,
    version_id: str,
    sequence: int,
    action: str,
    actor_id: str,
    source: SourceBinding,
    predecessor: tuple[str, str] | None = None,
) -> str:
    """One provenance event and its two evidence links: the manifest, then the file."""
    event_id = _derived("cge", assembly_id, str(sequence))
    policy = imp.policy
    connection.execute(
        "INSERT INTO omnivia_governed_provenance_events "
        "(workspace_id, provenance_event_id, assembly_id, governed_record_version_id, "
        "provenance_sequence, action, actor_id, actor_kind, actor_role, policy_id, "
        "policy_version, occurred_at_us, recorded_at_us, reason_code, reason_comment, "
        "audit_ref, correlation_kind, correlation_id, predecessor_record_id, "
        "predecessor_version_id, evidence_disposition) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 'available')",
        (
            imp.workspace_id,
            event_id,
            assembly_id,
            version_id,
            sequence,
            action,
            None if governed else actor_id,
            None if governed else _ACTOR_KIND,
            None if governed else _ACTOR_ROLE,
            policy.policy_id if governed else None,
            policy.policy_version if governed else None,
            imp.now_us,
            imp.now_us,
            _ACCEPTED_REASON if action == "governance.accepted" else None,
            imp.audit_ref if governed else None,
            "m1_audit" if governed else "extraction_run",
            imp.audit_ref if governed else imp.run_id,
            None if predecessor is None else predecessor[0],
            None if predecessor is None else predecessor[1],
        ),
    )
    file_evidence = file_evidence_id(
        imp.repository_id, source.relative_path, source.content_digest
    )
    connection.executemany(
        "INSERT INTO omnivia_governed_version_evidence_links "
        "(workspace_id, assembly_id, provenance_event_id, link_ordinal, evidence_id, "
        "normalized_record_id, normalized_span_id, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
        [
            (imp.workspace_id, assembly_id, event_id, ordinal, evidence_id, imp.now_us)
            for ordinal, evidence_id in enumerate((imp.evidence_id, file_evidence), 1)
        ],
    )
    return event_id


def _ensure_file_evidence(
    connection: sqlite3.Connection, imp: _Import, *, relative_path: str, digest: str
) -> None:
    """The L0 artifact of one captured file, written once and reused after that.

    Over the blob the capture registered: a file whose blob the capture never registered
    is refused rather than registered here, because registering captured bytes is the
    capture's act and an import that did it would be vouching for bytes it did not admit.
    """
    evidence_id = file_evidence_id(imp.repository_id, relative_path, digest)
    if connection.execute(
        "SELECT 1 FROM omnivia_evidence_artifacts WHERE evidence_id = ?", (evidence_id,)
    ).fetchone() is not None:
        return
    if connection.execute(
        "SELECT 1 FROM omnivia_blob_objects WHERE workspace_id = ? AND content_digest = ?",
        (imp.workspace_id, digest),
    ).fetchone() is None:
        raise CodeAnalysisImportError("a captured file's blob is not registered")
    metadata = canonical_document(
        {
            "format": FILE_EVIDENCE_FORMAT,
            "repository_id": imp.repository_id,
            "relative_path": relative_path,
        }
    )
    connection.execute(
        "INSERT INTO omnivia_evidence_artifacts "
        "(evidence_id, workspace_id, source_kind, source_native_id, source_locator, "
        "source_retrieved_at_us, event_at_us, observed_at_us, ingested_at_us, "
        "recorded_at_us, content_checksum, blob_content_digest, media_type, "
        "original_metadata_json, original_metadata_digest, sensitivity, parser_status, "
        "ingestion_status, staged_source_ref, import_run_id) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, 'private', "
        "'not_parsed', 'ingested', NULL, NULL)",
        (
            evidence_id,
            imp.workspace_id,
            FILE_EVIDENCE_KIND,
            evidence_id,
            relative_path,
            imp.now_us,
            imp.now_us,
            digest,
            digest,
            _FILE_MEDIA_TYPES.get(PurePosixPath(relative_path).suffix, "application/octet-stream"),
            metadata,
            content_digest(metadata),
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_evidence_provenance_events "
        "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, actor_id, "
        "actor_kind, action, occurred_at_us, reason_code, reason_comment, parser_status, "
        "ingestion_status, tombstoned_observation, source_kind, source_native_id, "
        "audit_ref) VALUES (?, ?, ?, 1, ?, 'service', 'captured', ?, NULL, NULL, "
        "'not_parsed', 'ingested', 0, ?, ?, ?)",
        (
            _derived("cfp", evidence_id),
            evidence_id,
            imp.workspace_id,
            _IMPORT_PRINCIPAL,
            imp.now_us,
            FILE_EVIDENCE_KIND,
            evidence_id,
            imp.audit_ref,
        ),
    )


def _insert_lineage(
    connection: sqlite3.Connection,
    imp: _Import,
    batch: CodeAnalysisBatch,
    *,
    assembly_id: str,
    event_id: str,
    ordinal: int,
) -> None:
    connection.execute(
        "INSERT INTO omnivia_governed_extraction_lineage "
        "(workspace_id, assembly_id, provenance_event_id, correlation_kind, correlation_id, "
        "extraction_kind, extraction_run_id, extractor_id, extractor_version, "
        "pipeline_version, configuration_version, configuration_digest, algorithm_id, "
        "algorithm_version, randomness_seed, model_id, model_version, "
        "inference_configuration_digest, prompt_template_id, prompt_template_version, "
        "prompt_template_digest, actor_id, actor_kind, actor_role, evidence_disposition, "
        "occurred_at_us, recorded_at_us, append_ordinal) "
        "VALUES (?, ?, ?, 'extraction_run', ?, 'deterministic', ?, ?, ?, ?, ?, ?, ?, ?, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, 'available', ?, ?, ?)",
        (
            imp.workspace_id,
            assembly_id,
            event_id,
            imp.run_id,
            imp.run_id,
            batch.producer_id,
            batch.producer_version,
            IMPORT_FORMAT,
            "code_analysis.v1",
            batch.profile_digest,
            batch.producer_id,
            batch.producer_version,
            batch.producer_id,
            _ACTOR_KIND,
            _ACTOR_ROLE,
            imp.now_us,
            imp.now_us,
            ordinal,
        ),
    )


def _insert_endpoints(
    connection: sqlite3.Connection,
    imp: _Import,
    *,
    governed: bool,
    assembly_id: str,
    event_id: str,
    relation_type: str,
    source: tuple[str, str],
    target: tuple[str, str],
) -> None:
    connection.execute(
        "INSERT INTO omnivia_governed_relation_endpoints "
        "(workspace_id, assembly_id, provenance_event_id, correlation_kind, correlation_id, "
        "relation_type, source_record_id, source_version_id, target_record_id, "
        "target_version_id, recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            imp.workspace_id,
            assembly_id,
            event_id,
            "m1_audit" if governed else "extraction_run",
            imp.audit_ref if governed else imp.run_id,
            relation_type,
            *source,
            *target,
            imp.now_us,
        ),
    )


def _seal(
    connection: sqlite3.Connection, imp: _Import, *, governed: bool, assembly_id: str, version_id: str
) -> None:
    connection.execute(
        "INSERT INTO omnivia_governed_version_seals "
        "(workspace_id, seal_id, assembly_id, governed_record_version_id, correlation_kind, "
        "correlation_id, sealed_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            imp.workspace_id,
            _derived("cgs", assembly_id),
            assembly_id,
            version_id,
            "m1_audit" if governed else "extraction_run",
            imp.audit_ref if governed else imp.run_id,
            imp.now_us,
        ),
    )


def _common_content(imp: _Import, batch: CodeAnalysisBatch, source: SourceBinding) -> dict[str, Any]:
    return {
        "format": IMPORT_FORMAT,
        "import_id": imp.import_id,
        "repository_id": batch.repository_id,
        "snapshot_id": batch.snapshot_id,
        "snapshot_kind": batch.snapshot_kind,
        "producer_id": batch.producer_id,
        "producer_version": batch.producer_version,
        "profile_digest": batch.profile_digest,
        "batch_digest": batch.digest,
        "source": source.as_document(),
    }


def _write_fact(
    connection: sqlite3.Connection,
    imp: _Import,
    batch: CodeAnalysisBatch,
    *,
    kind: str,
    key: str,
    record_type: str,
    content: dict[str, Any],
    ordinal: int,
    binding: SourceBinding,
    relation: tuple[str, dict[str, str], dict[str, str]] | None = None,
) -> dict[str, str]:
    """One accepted fact: its record, sealed candidate, then sealed governed version."""
    ids = _ids(imp, kind, key)
    content_json = to_canonical_json(content)
    connection.execute(
        "INSERT INTO omnivia_governed_records "
        "(workspace_id, governed_record_id, record_type, domain_scope, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?)",
        (imp.workspace_id, ids["record"], record_type, DOMAIN_SCOPE, imp.now_us),
    )
    for governed in (False, True):
        assembly_id = ids["governed_assembly" if governed else "candidate_assembly"]
        version_id = ids["governed_version" if governed else "candidate_version"]
        _insert_assembly(
            connection,
            imp,
            governed=governed,
            record_id=ids["record"],
            version_id=version_id,
            assembly_id=assembly_id,
            record_type=record_type,
            content_json=content_json,
            actor_id=batch.producer_id,
            ordinal=ordinal,
        )
        first = _insert_event(
            connection,
            imp,
            governed=governed,
            assembly_id=assembly_id,
            version_id=version_id,
            sequence=1,
            action="governance.accepted" if governed else "candidate.extracted",
            actor_id=batch.producer_id,
            source=binding,
            predecessor=(ids["record"], ids["candidate_version"]) if governed else None,
        )
        if not governed:
            _insert_lineage(
                connection, imp, batch, assembly_id=assembly_id, event_id=first, ordinal=ordinal
            )
        if relation is not None:
            relation_type, source, target = relation
            asserted = _insert_event(
                connection,
                imp,
                governed=governed,
                assembly_id=assembly_id,
                version_id=version_id,
                sequence=2,
                action="relation.asserted",
                actor_id=batch.producer_id,
                source=binding,
            )
            side = "governed_version" if governed else "candidate_version"
            _insert_endpoints(
                connection,
                imp,
                governed=governed,
                assembly_id=assembly_id,
                event_id=asserted,
                relation_type=relation_type,
                source=(source["record"], source[side]),
                target=(target["record"], target[side]),
            )
        _seal(connection, imp, governed=governed, assembly_id=assembly_id, version_id=version_id)
    return ids


def _stage(
    connection: sqlite3.Connection,
    imp: _Import,
    verified: VerifiedCodeAnalysisBatch,
    plan: _Plan,
) -> None:
    batch = verified.batch
    connection.execute(
        "INSERT INTO omnivia_code_analysis_imports "
        "(workspace_id, import_id, repository_id, snapshot_id, producer_id, producer_version, "
        "profile_digest, batch_digest, policy_id, policy_version, policy_digest, "
        "manifest_digest, manifest_evidence_id, extraction_run_id, audit_ref, entity_count, "
        "relation_count, diagnostic_count, file_count, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            imp.workspace_id,
            imp.import_id,
            batch.repository_id,
            batch.snapshot_id,
            batch.producer_id,
            batch.producer_version,
            batch.profile_digest,
            batch.digest,
            imp.policy.policy_id,
            imp.policy.policy_version,
            imp.policy.digest,
            verified.manifest_digest,
            verified.manifest_evidence_id,
            imp.run_id,
            imp.audit_ref,
            len(plan.nodes),
            len(plan.edges),
            len(plan.diagnostics),
            len(plan.files),
            imp.now_us,
        ),
    )
    connection.executemany(
        "INSERT INTO omnivia_code_analysis_import_files "
        "(workspace_id, import_id, snapshot_id, path, content_digest) VALUES (?, ?, ?, ?, ?)",
        [(imp.workspace_id, imp.import_id, batch.snapshot_id, path, digest) for path, digest in plan.files],
    )
    for path, digest in plan.files:
        _ensure_file_evidence(connection, imp, relative_path=path, digest=digest)
    connection.executemany(
        "INSERT INTO omnivia_code_analysis_import_diagnostics "
        "(workspace_id, import_id, diagnostic_ordinal, candidate_kind, candidate_key, "
        "reason_code, candidate_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (imp.workspace_id, imp.import_id, ordinal, kind, key, reason, canonical_document(document))
            for ordinal, (kind, key, reason, document) in enumerate(plan.diagnostics, 1)
        ],
    )
    members: list[tuple[str, str, dict[str, str]]] = []
    entities: dict[str, dict[str, str]] = {}
    for ordinal, node in enumerate(plan.nodes, 1):
        content = _common_content(imp, batch, node.source)
        content.update(
            name=node.display_name,
            candidate_kind="node",
            semantic_key=node.semantic_key,
            node_kind=node.kind,
            instance_id=revision_instance_id(
                batch.repository_id, batch.snapshot_id, node.semantic_key, node.source
            ),
        )
        ids = _write_fact(
            connection,
            imp,
            batch,
            kind="entity",
            key=node.semantic_key,
            record_type="knowledge.entity",
            content=content,
            ordinal=ordinal,
            binding=node.source,
        )
        entities[node.semantic_key] = ids
        members.append(("entity", node.semantic_key, ids))
    for ordinal, (edge, relation_type, type_only) in enumerate(plan.edges, len(plan.nodes) + 1):
        assert edge.target_key is not None
        content = _common_content(imp, batch, edge.source)
        content.update(
            statement=f"{edge.source_key} {relation_type} {edge.target_key}",
            candidate_kind="edge",
            edge_key=edge.edge_key,
            relation_type=relation_type,
            producer_relationship=edge.relationship_type,
            type_only=type_only,
            evidence_strength=edge.evidence_strength,
            resolution_status=edge.resolution_status,
            source_key=edge.source_key,
            target_key=edge.target_key,
            instance_id=revision_instance_id(
                batch.repository_id, batch.snapshot_id, edge.edge_key, edge.source
            ),
        )
        ids = _write_fact(
            connection,
            imp,
            batch,
            kind="relation",
            key=edge.edge_key,
            record_type="knowledge.relation",
            content=content,
            ordinal=ordinal,
            binding=edge.source,
            relation=(relation_type, entities[edge.source_key], entities[edge.target_key]),
        )
        members.append(("relation", edge.edge_key, ids))
    connection.executemany(
        "INSERT INTO omnivia_code_analysis_import_members "
        "(workspace_id, import_id, member_ordinal, member_kind, candidate_key, "
        "governed_record_id, governed_record_version_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (imp.workspace_id, imp.import_id, ordinal, kind, key, ids["record"], ids["governed_version"])
            for ordinal, (kind, key, ids) in enumerate(members, 1)
        ],
    )


# --- the seam ------------------------------------------------------------------------


def _receipt(
    connection: sqlite3.Connection, *, workspace_id: str, idempotency_key: str
) -> tuple[str, CodeAnalysisActivationReceipt] | None:
    row = connection.execute(
        "SELECT a.request_digest, a.import_id, a.activation_sequence, a.previous_import_id, "
        "i.repository_id, i.snapshot_id, i.producer_id, i.profile_digest, i.batch_digest, "
        "i.policy_id, i.policy_version, i.policy_digest, i.entity_count, i.relation_count, "
        "i.diagnostic_count, i.file_count, a.coverage_decision, a.reduced_file_count, "
        "a.audit_ref, a.activated_at_us "
        "FROM omnivia_code_analysis_activations a "
        "JOIN omnivia_code_analysis_imports i "
        "  ON i.workspace_id = a.workspace_id AND i.import_id = a.import_id "
        "WHERE a.workspace_id = ? AND a.idempotency_key = ?",
        (workspace_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), CodeAnalysisActivationReceipt(idempotency_key, *row[1:])


def _reduced_file_count(
    connection: sqlite3.Connection, *, workspace_id: str, import_id: str, previous: str | None
) -> int:
    """Files the retired import covered that the new snapshot still holds but the new
    import does not cover -- the same count 0074's activation trigger recomputes."""
    if previous is None:
        return 0
    row = connection.execute(
        "SELECT COUNT(*) FROM omnivia_code_analysis_import_files p "
        "JOIN omnivia_code_analysis_imports i ON i.workspace_id = ? AND i.import_id = ? "
        "JOIN omnivia_engineering_snapshot_files s ON s.workspace_id = i.workspace_id "
        " AND s.snapshot_id = i.snapshot_id AND s.path = p.path "
        "WHERE p.workspace_id = ? AND p.import_id = ? AND NOT EXISTS ("
        " SELECT 1 FROM omnivia_code_analysis_import_files n "
        " WHERE n.workspace_id = p.workspace_id AND n.import_id = i.import_id "
        " AND n.path = p.path)",
        (workspace_id, import_id, workspace_id, previous),
    ).fetchone()
    return int(row[0])


def _import(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    verified: VerifiedCodeAnalysisBatch,
    policy: CodeAnalysisImportPolicy,
    blobs_root: Path,
    idempotency_key: str,
    expected_active_import_id: str | None,
    now_us: int,
) -> CodeAnalysisActivationReceipt:
    batch = verified.batch
    request_digest = content_digest(
        canonical_document(
            {
                "format": IMPORT_FORMAT,
                "repository_id": batch.repository_id,
                "snapshot_id": batch.snapshot_id,
                "producer_id": batch.producer_id,
                "profile_digest": batch.profile_digest,
                "batch_digest": batch.digest,
                "policy_digest": policy.digest,
                "expected_active_import_id": expected_active_import_id,
            }
        )
    )
    stored = _receipt(connection, workspace_id=workspace_id, idempotency_key=idempotency_key)
    if stored is not None:
        if stored[0] != request_digest:
            raise CodeAnalysisImportConflict("that idempotency key names a different import request")
        return stored[1]

    registered = connection.execute(
        "SELECT policy_digest FROM omnivia_code_analysis_import_policies "
        "WHERE workspace_id = ? AND policy_id = ? AND policy_version = ?",
        (workspace_id, policy.policy_id, policy.policy_version),
    ).fetchone()
    if registered is None or str(registered[0]) != policy.digest:
        raise CodeAnalysisImportError("the import policy has not been authorised")

    proven = _reverified(
        connection, workspace_id=workspace_id, verified=verified, policy=policy, blobs_root=blobs_root
    )
    batch = proven.batch
    assert batch.profile_digest is not None
    scope = (batch.repository_id, batch.producer_id, batch.profile_digest)
    previous = connection.execute(
        "SELECT import_id, activation_sequence, activated_at_us "
        "FROM omnivia_code_analysis_activations "
        "WHERE workspace_id = ? AND repository_id = ? AND producer_id = ? "
        "AND profile_digest = ? ORDER BY activation_sequence DESC LIMIT 1",
        (workspace_id, *scope),
    ).fetchone()
    previous_import = None if previous is None else str(previous[0])
    if previous_import != expected_active_import_id:
        raise CodeAnalysisImportConflict("stale activation fence")
    if previous is not None and int(previous[2]) > now_us:
        raise CodeAnalysisImportConflict("stale activation fence")

    import_id = _derived(
        "cai",
        batch.repository_id,
        batch.snapshot_id,
        batch.producer_id,
        batch.profile_digest,
        batch.digest,
        policy.digest,
    )
    audit_ref = _derived("aud-code-import", workspace_id, idempotency_key)
    _audit(
        connection,
        workspace_id=workspace_id,
        audit_ref=audit_ref,
        principal_id=_IMPORT_PRINCIPAL,
        operation="code_analysis.import",
        authority={
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "policy_digest": policy.digest,
        },
        now_us=now_us,
    )
    staged = connection.execute(
        "SELECT 1 FROM omnivia_code_analysis_imports WHERE workspace_id = ? AND import_id = ?",
        (workspace_id, import_id),
    ).fetchone()
    if staged is None:
        imp = _Import(
            workspace_id=workspace_id,
            import_id=import_id,
            run_id=_derived("car", import_id),
            audit_ref=audit_ref,
            evidence_id=proven.manifest_evidence_id,
            policy=policy,
            now_us=now_us,
            repository_id=batch.repository_id,
        )
        _stage(connection, imp, proven, _plan(batch, policy))

    reduced = _reduced_file_count(
        connection, workspace_id=workspace_id, import_id=import_id, previous=previous_import
    )
    if reduced and not policy.allow_reduced_coverage:
        raise CodeAnalysisImportError(
            "the cutover drops coverage of files the snapshot still holds; "
            "the policy does not allow reduced coverage"
        )
    decision = "initial" if previous is None else ("reduced_authorised" if reduced else "complete")
    connection.execute(
        "INSERT INTO omnivia_code_analysis_activations "
        "(workspace_id, repository_id, producer_id, profile_digest, activation_sequence, "
        "import_id, previous_import_id, idempotency_key, request_digest, coverage_decision, "
        "reduced_file_count, audit_ref, activated_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            *scope,
            1 if previous is None else int(previous[1]) + 1,
            import_id,
            previous_import,
            idempotency_key,
            request_digest,
            decision,
            reduced,
            audit_ref,
            now_us,
        ),
    )
    receipt = _receipt(connection, workspace_id=workspace_id, idempotency_key=idempotency_key)
    assert receipt is not None
    return receipt[1]


def import_code_analysis(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    verified: VerifiedCodeAnalysisBatch,
    policy: CodeAnalysisImportPolicy,
    blobs_root: Path,
    idempotency_key: str,
    expected_active_import_id: str | None,
    now_us: int,
) -> CodeAnalysisActivationReceipt:
    """Re-verify, stage and atomically activate one batch as its scope's code graph.

    `expected_active_import_id` is the compare-and-swap fence: the import the caller
    believes is active for this repository, producer and profile now (`None` for none).
    The caller must hold the fenced write transaction; everything written here is inside
    a SAVEPOINT that a refusal rolls back.
    """
    _require_transaction(connection)
    _identifier(idempotency_key, "idempotency_key")
    if expected_active_import_id is not None:
        _identifier(expected_active_import_id, "expected_active_import_id")
    connection.execute("SAVEPOINT code_analysis_import")
    try:
        receipt = _import(
            connection,
            workspace_id=workspace_id,
            verified=verified,
            policy=policy,
            blobs_root=blobs_root,
            idempotency_key=idempotency_key,
            expected_active_import_id=expected_active_import_id,
            now_us=now_us,
        )
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT code_analysis_import")
        connection.execute("RELEASE SAVEPOINT code_analysis_import")
        raise
    connection.execute("RELEASE SAVEPOINT code_analysis_import")
    return receipt


def read_import_diagnostics(
    connection: sqlite3.Connection, *, workspace_id: str, import_id: str
) -> tuple[tuple[str, str, str], ...]:
    """`(candidate_kind, candidate_key, reason_code)` for every candidate not accepted."""
    rows: Iterable[tuple[Any, ...]] = connection.execute(
        "SELECT candidate_kind, candidate_key, reason_code "
        "FROM omnivia_code_analysis_import_diagnostics "
        "WHERE workspace_id = ? AND import_id = ? ORDER BY diagnostic_ordinal",
        (workspace_id, import_id),
    ).fetchall()
    return tuple((str(kind), str(key), str(reason)) for kind, key, reason in rows)


__all__ = [
    "ACCEPTABLE_STRENGTHS",
    "CODE_RELATION_TYPES",
    "DOMAIN_SCOPE",
    "FILE_EVIDENCE_FORMAT",
    "FILE_EVIDENCE_KIND",
    "IMPORT_FORMAT",
    "NODE_KINDS",
    "POLICY_FORMAT",
    "RELATIONSHIPS",
    "CodeAnalysisActivationReceipt",
    "CodeAnalysisImportConflict",
    "CodeAnalysisImportError",
    "CodeAnalysisImportPolicy",
    "active_import_id",
    "current_activation_sequence",
    "file_evidence_id",
    "import_code_analysis",
    "import_file_evidence_ids",
    "import_receipt",
    "read_import_diagnostics",
    "read_import_policies",
    "read_import_policy",
    "register_import_policy",
]
