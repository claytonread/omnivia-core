"""`code.query` code context end to end (TECH-CORE-RETRIEVAL-CODE-001, W13-B).

Everything W13-A's suite runs on, plus the material a context needs: the real Python and
TypeScript producers over a really sealed captured snapshot, the W12 import seam, a real
`evidence.search` projection and a real governed knowledge graph with accepted
`reconciliation.supports` relations and per-record evidence. Every query goes through the
production read dispatcher, and the architecture component through the production Context
Pack authority. Nothing is mocked; the one monkeypatch in this file wraps a production
function to commit a real revocation between its two real calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_code_analysis_import as w12
import test_code_query as w13a
import test_governed_truth_and_relations_migration as m3
import test_graph_traverse as gt
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import knowledge_sharing
from omnivia_core_runtime.service.application import (
    CODE_QUERY_OPERATION,
    CONTEXT_PACK_BUILD_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import code as code_handler
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage import memory as memory_storage
from omnivia_core_runtime.storage.code_analysis_import import DOMAIN_SCOPE
from omnivia_core_runtime.storage.code_analysis_typescript import TypeScriptRuntime
from omnivia_core_runtime.storage.projections import fts
from omnivia_core_runtime.storage.retrieval import local_owner_label_grant

from omnivia_core.contracts.v1 import (
    CODE_CONTEXT_MAX_SPAN_BYTES,
    ErrorResponseEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
)

WS = gt.WORKSPACE_ID
PY, TS = w12.PY, w12.TS

#: Well after every instant the W12 fixture and the graph fixture stamp, so the knowledge
#: seeded here is current at the query instant and the projection is built over all of it.
ARCH_US = 1_760_000_000_000_000

#: The label one artifact carries in the hidden cases. Any string: `permits` is a subset
#: test, and the non-owner grant below holds no labels at all.
RESTRICTED = "restricted"

#: A second real principal of this endpoint. `local_owner_label_grant` grants every label
#: to the configured local owner and none to anyone else, so this is the principal every
#: confidentiality claim below is made as; as `LOCAL_PRINCIPAL` the ACL stage still runs
#: but admits everything, which proves nothing about hiding.
READER = "reader-principal"

#: The query the task-context request carries. It matches `gt.CONTENT`'s own statement, so
#: a pack built from the admitted records has sections rather than only omissions.
CONTEXT_QUERY = "governed"

#: A test module that really calls the subject, so "a static reference out of a test file
#: is still a static reference and never runtime coverage" is a claim about real producer
#: output rather than about a hand-written edge.
_TEST_MODULE = b'"""Tests."""\n\nfrom .models import make\n\n\ndef test_make() -> None:\n    make("k")\n'

#: W13-A's repository plus that test module. A private copy, because this file's
#: `code_context` answers count neighbours and W13-A's own expectations count the same
#: ones under a different file set.
REPOSITORY: dict[str, bytes] = {**w13a.REPOSITORY, "pkg/test_models.py": _TEST_MODULE}
REVISION: dict[str, bytes] = {**REPOSITORY, "pkg/service.py": w12.REVISION["pkg/service.py"]}

runtime = w13a.runtime
workspace = w13a.workspace
blobs_root = w13a.blobs_root


@pytest.fixture
def first(workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime) -> w13a.Imported:
    return w13a.Imported(
        workspace, blobs_root, runtime, files=REPOSITORY, snapshot_id="snap-1",
        base_us=1_700_000_000_000_000,
    )


@pytest.fixture
def imported(workspace: Any, blobs_root: Path, first: w13a.Imported) -> w13a.Imported:
    chosen = w12.policy(first.python, first.typescript)
    w12.register(workspace, chosen)
    w12.run(workspace, blobs_root, first.python, chosen, key="py-1", expected=None, now=w12.NOW)
    w12.run(
        workspace, blobs_root, first.typescript, chosen, key="ts-1", expected=None,
        now=w12.NOW + 1,
    )
    return first


def cutover(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, previous: str
) -> tuple[w13a.Imported, Any]:
    """A second snapshot imported over the first: the old import's facts are retired."""
    second = w13a.Imported(
        workspace, blobs_root, runtime, files=REVISION, snapshot_id="snap-2",
        base_us=1_710_000_000_000_000,
    )
    chosen = w12.policy(second.python, second.typescript)
    receipt = w12.run(
        workspace, blobs_root, second.python, chosen, key="py-2", expected=previous,
        now=w12.NOW + 100,
    )
    return second, receipt


# --- the workspace's architecture knowledge -------------------------------------------


def audit(holder: Any, ref: str) -> None:
    with fenced_transaction(
        holder.connection, holder.identity, workspace_id=WS, fencing_generation=holder.generation
    ):
        m3.insert(holder.connection, "omnivia_application_audit_events", m3.audit_row(ref))


def artifact(holder: Any, evidence_id: str, *, at: int) -> None:
    """One L0 artifact and its capture provenance, through the fenced writer."""
    m2.write(
        holder, m2.EVIDENCE, evidence_id=evidence_id, source_native_id=evidence_id,
        source_locator=f"/{evidence_id}", ingested_at_us=at - 1, recorded_at_us=at,
    )
    m2.write(
        holder, m2.PROVENANCE, provenance_event_id=f"prv-{evidence_id}-1",
        evidence_id=evidence_id, source_native_id=evidence_id, provenance_sequence=1,
    )


def restrict(holder: Any, evidence_id: str, *, sequence: int = 1) -> None:
    """Attach a permission label to one artifact: 0008's own way to restrict it."""
    m2.write(
        holder, m2.LABELS, label_event_id=f"lbl-{evidence_id}-{sequence}",
        evidence_id=evidence_id, label_sequence=sequence, permission_label=RESTRICTED,
    )


def project(holder: Any, *, now_us: int) -> None:
    """Build and open `evidence.search`, the two calls `service.main.serve` makes."""
    fts.build_search_projection(
        holder.connection, holder.identity, workspace_id=WS,
        fencing_generation=holder.generation, now_us=now_us,
    )
    fts.open_search_projection(holder.connection, workspace_id=WS)


#: Each seeded knowledge record, and the artifact it cites. Separate artifacts are the
#: point: a record's permission labels are the labels of what it cites, so two records can
#: only differ in visibility if they cite different artifacts.
KNOWLEDGE = ("arch-support", "arch-claim", "arch-loose", "arch-link")


@pytest.fixture
def architecture(workspace: Any, imported: w13a.Imported) -> Any:
    """Imported code, plus governed knowledge: one supported claim, one unsupported.

    `arch-support` supports `arch-claim` through an accepted `reconciliation.supports`
    relation. `arch-loose` is the same kind of record with no support at all, which is
    what makes "supported" a filter that does something rather than a word.
    """
    holder = workspace.holder
    for number, record_id in enumerate(KNOWLEDGE, start=1):
        artifact(holder, f"evd-{record_id}", at=ARCH_US + number)
        audit(holder, f"aud-{record_id}")
    for number, record_id in enumerate(("arch-support", "arch-claim", "arch-loose")):
        gt.seal(
            holder, record_id=record_id, audit_ref=f"aud-{record_id}",
            recorded_at_us=ARCH_US + 100 + number, evidence_id=f"evd-{record_id}",
            normalized=False,
        )
    gt.seal(
        holder, record_id="arch-link", audit_ref="aud-arch-link", record_type="knowledge.relation",
        recorded_at_us=ARCH_US + 110, endpoint=(gt.SUPPORTS, "arch-support", "arch-claim"),
        evidence_id="evd-arch-link", normalized=False,
    )
    project(holder, now_us=ARCH_US + 1_000_000)
    return imported


# --- the production read path ----------------------------------------------------------


class Served:
    """The service a code context reads from: the connection and the blob root.

    Two attributes, because those are the two a `code_context` handler touches -- the
    authoritative connection, and the layout's `blobs_path`, which is where the captured
    source it discloses is addressed. Presenting exactly that is what keeps the test
    honest about what a handler may reach.
    """

    def __init__(self, holder: Any, blobs_root: Path | None) -> None:
        self.connection = holder.connection
        self.layout = SimpleNamespace(blobs_path=blobs_root)


def dispatcher(
    service: object,
    *,
    operations: frozenset[str] | None = None,
    principal: str = LOCAL_PRINCIPAL,
) -> Any:
    """The production dispatcher, as `service.main.serve` wires it.

    `principal` is a parameter for one reason: `local_owner_label_grant` holds every
    evidence label for the configured local owner and none for anyone else, so a
    confidentiality claim made only as `LOCAL_PRINCIPAL` is a claim about a grant that
    admits everything. The hidden cases below run as `READER`, which is a real session of
    this endpoint holding a real, empty label grant.
    """
    registry = build_application_registry()
    granted = w13a.read_operations() if operations is None else operations
    return ApplicationDispatcher(
        registry=registry,
        session=local_owner_session(
            principal_id=principal, installation_id=gt.INSTALLATION_ID,
            workspace_id=WS, operations=granted,
        ),
        binding=ServiceBinding(installation_id=gt.INSTALLATION_ID, workspace_id=WS),
        supported_capabilities=server_capability_snapshot(registry),
        transport=w13a.LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=principal, workspaces=frozenset({WS}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            service,
        ),
        record=None,
        service=service,
    )


def payload(verified: Any, **fields: Any) -> dict[str, Any]:
    """One `code_context` request, with the task-context request it requires."""
    body = w13a.payload(verified, "code_context", **fields)
    body.setdefault(
        "context",
        {"query": CONTEXT_QUERY, "mode": "deterministic_view", "token_budget": 4000},
    )
    return body


def seed_of(verified: Any, *keys: str, origin: str = "semantic", **overrides: Any) -> dict[str, Any]:
    batch = verified.batch
    return {
        "origin": origin,
        "repository_id": batch.repository_id,
        "snapshot_id": batch.snapshot_id,
        "producer_id": batch.producer_id,
        "profile_digest": batch.profile_digest,
        "semantic_keys": list(keys),
        **overrides,
    }


def call(
    workspace: Any, body: dict[str, Any], adapter: str = "in-process",
    *, blobs_root: Path | None = None, operations: frozenset[str] | None = None,
    principal: str = LOCAL_PRINCIPAL,
) -> ResponseEnvelope:
    request = gt.request_for(body, operation=CODE_QUERY_OPERATION)
    served = Served(workspace.holder, blobs_root)
    route = dispatcher(served, operations=operations, principal=principal)
    return s2._unrecorded_transport_call(adapter, route, request)


def query(workspace: Any, body: dict[str, Any], **extra: Any) -> dict[str, Any]:
    response = call(workspace, body, **extra)
    assert isinstance(response, SuccessResponseEnvelope), response
    return w13a.plain(dict(response.result))


def refusal(workspace: Any, body: dict[str, Any], **extra: Any) -> ErrorResponseEnvelope:
    response = call(workspace, body, **extra)
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def packed(result: dict[str, Any]) -> dict[str, Any]:
    component = result["context"]["architecture"]
    assert component["availability"] == "available", component
    pack: dict[str, Any] = component["pack"]
    return pack


def record_ids(pack: dict[str, Any]) -> set[str]:
    return {item["provenance"]["identity"]["record_id"] for item in pack["records"]}


def manifest_ids(pack: dict[str, Any]) -> set[str]:
    """Every governed candidate the pack's own reproducibility block names."""
    return {item["record_id"] for item in pack["reproducibility"]["record_versions"]}


def spans_of(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {span["semantic_key"]: span for span in result["context"]["spans"]}


def stable(result: Any) -> dict[str, Any]:
    """The result without the one clock read, through the component as well.

    `w13a.stable` drops the query instant. A pack is content-addressed over that same
    instant, so its own derived instants -- and therefore its digest and its artifact
    checksum -- move with it. Dropping exactly those five and nothing else is what makes
    "every transport returns the same answer" a claim about the answer rather than about
    the clock.
    """
    copied = w13a.stable(result)
    component = (copied.get("context") or {}).get("architecture") or {}
    pack = component.get("pack")
    if pack is not None:
        pack.pop("pack_id")
        reproducibility = pack["reproducibility"]
        for field in ("generated_at", "canonical_resolution_time", "artifact_checksum"):
            reproducibility.pop(field)
        reproducibility["freshness"].pop("as_of")
    return copied


def shape(result: dict[str, Any]) -> dict[str, Any]:
    """Everything a hidden member could move: paths, degree, counts and the flags."""
    return {
        "definitions": w13a.keys(result["definitions"]),
        "symbols": [(s["semantic_key"], s["depth"]) for s in result["symbols"]],
        "relations": w13a.edges(result),
        "diagnostics": [d["edge_key"] for d in result["diagnostics"]],
        "coverage": result["coverage"],
        "spans": sorted(spans_of(result)),
    }



# --- the assembled context -------------------------------------------------------------


def test_a_code_context_is_spans_same_snapshot_neighbours_and_a_labelled_component(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """Requirement 1, in one answer, and all three parts of it at once.

    The spans are the captured bytes of exactly the definitions disclosed, sliced out of
    the blob each one's own `content_digest` names; the neighbours are accepted static
    relations of the one applicable import in both directions; and the architecture
    component is a real Context Pack, content-addressed and built by the context
    authority, carrying the supported claim and not the unsupported one.
    """
    result = query(
        workspace,
        payload(architecture.python, semantic_key="pkg.models.make"),
        blobs_root=blobs_root,
    )
    assert result["resolution"] == "semantic_key"
    assert result["coverage"]["analysis"] == "static"
    assert result["coverage"]["graph_expansion"] == "applied"
    assert result["binding"]["snapshot_id"] == "snap-1"

    # Neighbours, both directions, from this import and no other.
    assert w13a.edges(result) == [
        (1, "pkg.jobs", "code.imports", "pkg.models.make", False),
        (1, "pkg.jobs.run", "code.calls", "pkg.models.make", False),
        (1, "pkg.models.make", "code.calls", "pkg.models.Record", False),
        (1, "pkg.service", "code.imports", "pkg.models.make", False),
        (1, "pkg.service.run", "code.calls", "pkg.models.make", False),
        (1, "pkg.test_models", "code.imports", "pkg.models.make", False),
        (1, "pkg.test_models.test_make", "code.calls", "pkg.models.make", False),
    ]
    assert all(relation["evidence_strength"] in {"syntactic", "resolved_static"}
               for relation in result["relations"])

    # Spans: the exact captured bytes of every definition the answer discloses.
    spans = spans_of(result)
    assert sorted(spans) == sorted(
        [*w13a.keys(result["definitions"]), *w13a.keys(result["symbols"])]
    )
    subject = spans["pkg.models.make"]
    captured = REPOSITORY["pkg/models.py"]
    assert subject["source"]["content_digest"] == architecture.snapshot.digests["pkg/models.py"]
    assert subject["text"] == "def make(key: str) -> Record:\n    return Record(key)"
    assert subject["text"] in captured.decode()
    assert subject["byte_length"] == len(subject["text"].encode()) and not subject["truncated"]
    assert not any("/" == span["source"]["relative_path"][:1] for span in spans.values())

    # The architecture component: a real pack, separately labelled, independently built.
    pack = packed(result)
    assert pack["mode"] == "deterministic_view" and pack["query"] == CONTEXT_QUERY
    assert pack["pack_id"].startswith("sha256:")
    assert record_ids(pack) == {"arch-claim"}
    assert manifest_ids(pack) == {"arch-claim"}
    assert [section["kind"] for section in pack["sections"]] == ["governed_record"]
    reproducibility = pack["reproducibility"]
    assert reproducibility["retrieval_version"] == code_handler.CODE_CONTEXT_RETRIEVAL_VERSION
    assert reproducibility["authorization_context"]["policy_versions"] == dict(
        code_handler.CODE_CONTEXT_POLICY_VERSIONS
    )
    assert reproducibility["authorization_context"]["pre_ranking_authorization_enforced"] is True
    # Nothing of the code analysis crossed into it, in either direction.
    assert all(item["domain_scope"] != DOMAIN_SCOPE for item in pack["records"])
    assert "pkg.models.make" not in json.dumps(pack["sections"])


def test_an_unsupported_assertion_is_not_architecture_knowledge(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """D18's third clause. `arch-loose` is the same record type, the same domain and the
    same content as the admitted claim; the only difference is that nothing supports it,
    and that is the whole difference."""
    pack = packed(
        query(
            workspace,
            payload(architecture.python, semantic_key="pkg.models.make"),
            blobs_root=blobs_root,
        )
    )
    assert "arch-loose" not in record_ids(pack) | manifest_ids(pack)
    # It is really there, and really indistinguishable but for its support.
    assert (
        workspace.holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_governed_version_seals s "
            "JOIN omnivia_governed_version_assemblies a "
            "  ON a.workspace_id = s.workspace_id AND a.assembly_id = s.assembly_id "
            "WHERE a.governed_record_id = 'arch-loose' AND a.layer = 'governed'"
        ).fetchone()[0]
        == 1
    )


def test_same_name_ambiguity_stays_choices_and_assembles_no_context(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """A shared display name is answered with every qualified choice and no context.

    An ambiguous subject walks nothing, so there is nothing whose captured source could
    honestly be called the context of the thing the caller meant.
    """
    result = query(
        workspace, payload(architecture.python, name="run"), blobs_root=blobs_root
    )
    assert result["resolution"] == "ambiguous"
    assert w13a.keys(result["definitions"]) == ["pkg.jobs.run", "pkg.service.run"]
    assert result["symbols"] == result["relations"] == []
    assert result["context"] == {
        "spans": [],
        "architecture": {"availability": "unavailable", "reason": "subject_not_resolved"},
    }
    assert result["coverage"]["graph_expansion"] == "not_requested"
    assert result["coverage"]["complete"] is False


# --- confidentiality (D16) --------------------------------------------------------------


@pytest.mark.parametrize("hidden", ("arch-support", "arch-claim", "arch-link"))
def test_a_hidden_endpoint_or_support_moves_no_disclosed_path_degree_or_count(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported, hidden: str
) -> None:
    """D16. Three runs, three hidden members, one disclosed answer.

    Hiding the supported claim, the record that supports it, or the support relation
    itself all have the same effect and the same *only* effect: the claim stops being
    architecture knowledge for this caller. The code answer -- every path, every depth,
    every count, the truncation flags and the coverage -- is byte-identical to the run
    where nothing is hidden, and the pack's own manifest loses the claim rather than
    keeping it and showing less of it.
    """
    body = payload(architecture.python, semantic_key="pkg.models.make")
    before = query(workspace, body, blobs_root=blobs_root, principal=READER)
    assert record_ids(packed(before)) == {"arch-claim"}

    restrict(workspace.holder, f"evd-{hidden}")
    after = query(workspace, body, blobs_root=blobs_root, principal=READER)

    assert shape(after) == shape(before)
    pack = packed(after)
    assert record_ids(pack) == set()
    assert manifest_ids(pack) == set()
    assert pack["sections"] == [] and pack["citations"] == []
    assert hidden not in json.dumps(pack)
    # The frontier the pack attests is the admitted set, not a wider one shown less of.
    assert pack["reproducibility"]["authorization_context"][
        "authorized_candidate_set_checksum"
    ] != packed(before)["reproducibility"]["authorization_context"][
        "authorized_candidate_set_checksum"
    ]


def test_a_hidden_code_analysis_is_answered_as_a_scope_never_analysed(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """The same rule over the code graph. Every fact of one import cites that snapshot's
    sealed manifest evidence, so restricting it hides the analysis whole -- and the
    answer is then the one a caller gets for a scope nobody ever analysed (W13-C), with no
    path, no degree, no count and no flag distinguishing the two. A `not_found` would say
    there was an analysis to look in; `code.status` reports the same scope as having none."""
    body = payload(architecture.python, semantic_key="pkg.models.make")
    assert query(workspace, body, blobs_root=blobs_root, principal=READER)[
        "resolution"
    ] == "semantic_key"

    restrict(workspace.holder, "evd-snap-1")
    hidden = refusal(workspace, body, blobs_root=blobs_root, principal=READER).error
    absent = refusal(
        workspace,
        payload(architecture.python, semantic_key="pkg.models.no_such_definition"),
        blobs_root=blobs_root, principal=READER,
    ).error
    never_analysed = refusal(
        workspace,
        {**body, "profile_digest": "sha256:" + "0" * 64},
        blobs_root=blobs_root, principal=READER,
    ).error
    assert hidden.code == "projection_unavailable"
    assert dict(hidden.details or {}) == {"readiness": "not_ready", "reason": "no_active_import"}
    assert hidden.to_wire() == absent.to_wire() == never_analysed.to_wire()


def test_revocation_between_selection_and_output_refuses_and_discloses_nothing(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D16's second clause, through the handler's own two reads of the ACL.

    `read_governed_record_labels` is called once before the walk and once at output. The
    wrapper below is the seam: it calls the real function both times and, after the
    first call has returned the labels the selection was made on, commits a real
    permission label through the real fenced writer. So the second call observes a real
    revocation that landed while the answer was being assembled, and the production
    revalidation is what refuses.

    Falsifier: drop the second call and this test fails while every other test passes.
    """
    body = payload(architecture.python, semantic_key="pkg.models.make")
    assert query(workspace, body, blobs_root=blobs_root, principal=READER)[
        "resolution"
    ] == "semantic_key"

    real = code_handler.read_governed_record_labels
    calls: list[int] = []

    def revoke_after_selection(connection: Any, **selectors: Any) -> Any:
        labels = real(connection, **selectors)
        calls.append(len(calls))
        if len(calls) == 1:
            restrict(workspace.holder, "evd-arch-claim", sequence=2)
        return labels

    monkeypatch.setattr(code_handler, "read_governed_record_labels", revoke_after_selection)
    revoked = refusal(workspace, body, blobs_root=blobs_root, principal=READER).error
    assert len(calls) == 2
    monkeypatch.undo()

    absent = refusal(
        workspace,
        payload(architecture.python, semantic_key="pkg.models.no_such_definition"),
        blobs_root=blobs_root, principal=READER,
    ).error
    # Indistinguishable from a subject that was never there: no count, no path, no name.
    assert revoked.code == "not_found"
    assert revoked.to_wire() == absent.to_wire()
    assert "arch-claim" not in json.dumps(revoked.to_wire())


# --- seeds (D17) ------------------------------------------------------------------------


def test_a_seed_of_this_analysis_expands_and_an_older_one_never_does(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, architecture: w13a.Imported
) -> None:
    """D17. A seed carries the analysis it was selected against, and it is compared.

    The first query proves a seed is a real subject selector: two keys, both expanded,
    in key order. Then the repository is re-analysed and the second import activated. The
    same seed -- still naming `snap-1` -- is now a seed of a retired analysis, and the
    answer says so rather than walking its keys against the newer graph.
    """
    seeded = query(
        workspace,
        payload(
            architecture.python,
            seed=seed_of(architecture.python, "pkg.service.run", "pkg.models.make"),
        ),
        blobs_root=blobs_root,
    )
    assert seeded["resolution"] == "semantic"
    assert w13a.keys(seeded["definitions"]) == ["pkg.models.make", "pkg.service.run"]
    assert seeded["coverage"]["graph_expansion"] == "applied"
    assert seeded["relations"] and seeded["context"]["spans"]
    # Both ends of `pkg.service.run -> pkg.models.make` are seeds, and a bidirectional
    # walk reaches that one relationship from either of them. It is still one relation.
    assert len({edge["edge_key"] for edge in seeded["relations"]}) == len(
        seeded["relations"]
    )
    assert [
        edge for edge in w13a.edges(seeded)
        if (edge[1], edge[3]) == ("pkg.service.run", "pkg.models.make")
    ] == [(1, "pkg.service.run", "code.calls", "pkg.models.make", False)]
    stale = seed_of(architecture.python, "pkg.service.run", "pkg.models.make")

    second, _ = cutover(workspace, blobs_root, runtime, seeded["binding"]["import_id"])
    refreshed = query(
        workspace,
        payload(second.python, semantic_key="pkg.models.make"),
        blobs_root=blobs_root,
    )
    assert refreshed["binding"]["snapshot_id"] == "snap-2"

    spliced = query(
        workspace, payload(second.python, seed=stale), blobs_root=blobs_root
    )
    assert spliced["binding"]["snapshot_id"] == "snap-2"
    assert spliced["resolution"] == "seed_not_applicable"
    assert spliced["coverage"]["graph_expansion"] == "unavailable"
    assert spliced["coverage"]["expansion_reason"] == "seed_binding_mismatch"
    assert spliced["coverage"]["complete"] is False
    assert spliced["definitions"] == spliced["symbols"] == spliced["relations"] == []
    assert spliced["context"] == {
        "spans": [],
        "architecture": {"availability": "unavailable", "reason": "subject_not_resolved"},
    }


def test_a_seed_naming_a_definition_this_analysis_does_not_hold_is_not_found(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """A seed that half resolves is a seed selected against other material. The whole
    seed is refused, and with the same code a wholly unknown subject gets."""
    body = payload(
        architecture.python,
        seed=seed_of(architecture.python, "pkg.models.make", "pkg.models.vanished"),
    )
    assert refusal(workspace, body, blobs_root=blobs_root).error.code == "not_found"


# --- static claims stay static (D18) ------------------------------------------------------


def test_a_reference_out_of_a_test_module_is_static_and_never_runtime_coverage(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """D18's first clause. `pkg/test_models.py` really calls `make`, and the producer
    really resolves it. It is therefore a `code.calls` fact of static evidence strength
    like any other -- and the answer says `static` and says nothing else, because a
    static reference from a test file is not a statement that the subject was run."""
    result = query(
        workspace,
        payload(architecture.python, semantic_key="pkg.models.make"),
        blobs_root=blobs_root,
    )
    test_edges = [
        relation
        for relation in result["relations"]
        if relation["source_key"].startswith("pkg.test_models")
    ]
    assert [relation["relation_type"] for relation in test_edges] == [
        "code.imports", "code.calls",
    ]
    assert {relation["evidence_strength"] for relation in test_edges} == {"resolved_static"}
    assert {relation["source"]["relative_path"] for relation in test_edges} == {
        "pkg/test_models.py"
    }
    assert result["coverage"]["analysis"] == "static"
    # Nothing in the answer claims a run happened, under any spelling.
    rendered = json.dumps(result)
    assert "runtime" not in rendered and "executed" not in rendered


def test_supported_prose_never_becomes_a_code_relation(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """D18's second clause, in both directions.

    `arch-support` supports `arch-claim` through `reconciliation.supports` -- a governed
    relation over prose. It is disclosed as architecture knowledge and never as a code
    relationship: no relation in the answer is of any type outside the `code.*`
    namespace, and no knowledge record id appears anywhere among the code facts.
    """
    result = query(
        workspace,
        payload(architecture.python, semantic_key="pkg.models.make"),
        blobs_root=blobs_root,
    )
    assert {relation["relation_type"] for relation in result["relations"]} <= {
        "code.calls", "code.imports", "code.references",
    }
    assert gt.SUPPORTS not in json.dumps(
        {key: result[key] for key in ("definitions", "symbols", "relations", "diagnostics")}
    )
    for key in ("definitions", "symbols", "relations", "diagnostics"):
        assert "arch-" not in json.dumps(result[key])
    assert record_ids(packed(result)) == {"arch-claim"}


# --- independent authorisation of the component -------------------------------------------


def test_the_component_needs_the_context_authority_the_code_read_does_not_carry(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """`code.query` is granted as a graph read. The architecture component is the Context
    Pack authority's material, and a session granted the one and not the other gets the
    code answer whole and no component at all -- not a thinner component, and not a
    refusal that would make the code facts depend on an authority they do not need."""
    body = payload(architecture.python, semantic_key="pkg.models.make")
    # The handler names the operation itself -- `service/application.py` imports the
    # handlers, so it cannot import that module back -- and this is the pin that keeps
    # the two spellings one grant.
    assert code_handler.CONTEXT_PACK_BUILD_OPERATION == CONTEXT_PACK_BUILD_OPERATION
    without = w13a.read_operations() - {CONTEXT_PACK_BUILD_OPERATION}
    assert CODE_QUERY_OPERATION in without

    whole = query(workspace, body, blobs_root=blobs_root)
    narrowed = query(workspace, body, blobs_root=blobs_root, operations=without)

    assert narrowed["context"]["architecture"] == {
        "availability": "unavailable", "reason": "not_authorised",
    }
    assert narrowed["context"]["spans"] == whole["context"]["spans"]
    assert {key: narrowed[key] for key in ("definitions", "symbols", "relations")} == {
        key: whole[key] for key in ("definitions", "symbols", "relations")
    }


# --- the W12 applicability bypasses (requirement 8) -----------------------------------------


def test_imported_code_facts_never_reach_the_memory_or_sharing_reads(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, architecture: w13a.Imported
) -> None:
    """The two folds W12's audit named, closed, and shown closed after a cutover.

    `storage/memory.py` and `service/knowledge_sharing.py` each decide currency for
    themselves, and neither can decide 0074's: an import is retired by an activation,
    which changes no version id, no digest and no supersession row. Rather than teach
    both a second copy of that rule, both exclude the domain outright -- so a code fact
    is unreachable through them whether it is the applicable one or a retired one.
    """
    connection = workspace.holder.connection
    # After everything this fixture records, so the frontier is the whole workspace.
    instant = ARCH_US + 10**9
    grant = local_owner_label_grant(
        principal_id=LOCAL_PRINCIPAL, workspace_id=WS, granted_workspace=WS
    )

    def memory_versions(**extra: Any) -> tuple[Any, ...]:
        return memory_storage.read_authorized_memory_frontier(
            connection, workspace_id=WS, resolution_instant_us=instant, view=None,
            label_grant=grant, body_free=False, **extra,
        ).versions

    def code_record_ids() -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT governed_record_id FROM omnivia_governed_version_assemblies "
                "WHERE domain_scope = ?",
                (DOMAIN_SCOPE,),
            )
        }

    # The exclusion is the one in the production modules, spelled in two places.
    assert memory_storage._CODE_ANALYSIS_DOMAIN == DOMAIN_SCOPE
    assert knowledge_sharing._CODE_ANALYSIS_DOMAIN == DOMAIN_SCOPE

    applicable = code_record_ids()
    assert applicable, "the fixture must really hold imported code facts"

    _, receipt = cutover(
        workspace, blobs_root, runtime,
        query(workspace, payload(architecture.python, semantic_key="pkg.models.make"),
              blobs_root=blobs_root)["binding"]["import_id"],
    )
    retired = code_record_ids() - {
        str(row[0])
        for row in connection.execute(
            "SELECT governed_record_id FROM omnivia_code_analysis_import_members "
            "WHERE import_id = ?",
            (receipt.import_id,),
        )
    }
    assert retired, "the cutover must really have retired facts"

    # Memory: not in the unscoped frontier, and not reachable by asking for the scope.
    assert not any(version.domain_scope == DOMAIN_SCOPE for version in memory_versions())
    assert memory_versions(domain_scope=DOMAIN_SCOPE) == ()
    assert memory_versions(record_ids=sorted(retired)) == ()
    assert memory_versions(record_ids=sorted(code_record_ids())) == ()
    with memory_storage.read_snapshot(connection):
        assert memory_storage.read_memory_record_id_page(
            connection, workspace_id=WS, resolution_instant_us=instant,
            domain_scope=DOMAIN_SCOPE, after_record_id=None,
        ) == ()
    # And the memory family still serves its own records, so the filter is not a mute.
    assert any(version.domain_scope != DOMAIN_SCOPE for version in memory_versions())

    # Sharing: no code version is authoritative, while a knowledge record still is.
    for record_id in sorted(code_record_ids()):
        assert knowledge_sharing._authoritative_version(
            connection, WS, record_id=record_id
        ) is None
    present = knowledge_sharing._authoritative_version(connection, WS, record_id="arch-claim")
    assert present is not None and present.domain_scope == "product.core"
    with (
        w12.captured._fenced(workspace),
        pytest.raises(knowledge_sharing.KnowledgeShareRefused) as refused,
    ):
        knowledge_sharing.propose_share(
            connection, knowledge_sharing.NO_PROJECTS, principal="local-user",
            workspace_id=WS, generation=workspace.holder.generation,
            share_id="shr-code-1", record_id=min(applicable),
            recipient_project_id="proj-other", proposed_at_us=instant,
        )
    assert refused.value.reason == knowledge_sharing.REFUSED_NOT_AUTHORITATIVE


# --- the closed contract and the wire -------------------------------------------------


def test_the_context_fields_are_closed_to_their_own_intent(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported
) -> None:
    """`seed` and `context` belong to `code_context`, `code_context` requires `context`,
    and every refusal is frozen text that quotes nothing the caller sent."""
    py = architecture.python
    sentinel = "zzseedsentinel"
    bad_seed = seed_of(py, "pkg.models.make", origin=sentinel)
    cases = (
        # `context` is required by the intent that acts on it.
        {k: v for k, v in payload(py, semantic_key="pkg.models.make").items() if k != "context"},
        # ... and refused by every intent that would not.
        payload(py, semantic_key="pkg.models.make") | {"intent": "callers"},
        w13a.payload(py, "callers", semantic_key="pkg.models.make", seed=seed_of(py, "x")),
        # A seed is a binding, and every member of it is checked.
        payload(py, seed=bad_seed),
        payload(py, seed=seed_of(py, "pkg.models.make") | {"profile_digest": sentinel}),
        payload(py, seed=seed_of(py, "has space")),
        payload(py, seed=seed_of(py) | {"semantic_keys": []}),
        # The task-context request is the context authority's own shape.
        payload(py, semantic_key="pkg.models.make")
        | {"context": {"query": "x", "mode": sentinel, "token_budget": 10}},
        payload(py, semantic_key="pkg.models.make")
        | {"context": {"query": "", "mode": "deterministic_view", "token_budget": 10}},
    )
    for body in cases:
        error = refusal(workspace, body, blobs_root=blobs_root).error
        assert error.code == "invalid_request", body
        assert sentinel not in error.message and "has space" not in error.message


def test_a_code_context_without_a_blob_root_is_a_wiring_refusal(
    workspace: Any, architecture: w13a.Imported
) -> None:
    """Captured source is the only thing a context cannot be assembled without, and a
    service that is not serving it is this build's fault rather than the caller's."""
    error = refusal(
        workspace, payload(architecture.python, semantic_key="pkg.models.make")
    ).error
    assert error.code == "internal_non_recoverable"


@pytest.mark.parametrize("adapter", ("local-ipc", "http"))
def test_every_transport_returns_the_same_code_context(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported, adapter: str
) -> None:
    """W13-A's parity, over the shapes W13-B adds: an assembled context with a pack in
    it, a seed this graph cannot expand, and a refusal."""
    py = architecture.python
    assembled = payload(py, semantic_key="pkg.models.make")
    cases = (
        assembled,
        payload(py, seed=seed_of(py, "pkg.models.make") | {"snapshot_id": "snap-9"}),
        payload(py, semantic_key="pkg.models.make") | {"context": {"query": "x"}},
    )
    for body in cases:
        local = call(workspace, body, blobs_root=blobs_root)
        remote = call(workspace, body, adapter, blobs_root=blobs_root)
        assert type(remote) is type(local), body
        if isinstance(local, SuccessResponseEnvelope):
            assert isinstance(remote, SuccessResponseEnvelope)
            assert stable(remote.result) == stable(local.result)
        else:
            assert isinstance(local, ErrorResponseEnvelope)
            assert isinstance(remote, ErrorResponseEnvelope)
            assert remote.error.to_wire() == local.error.to_wire()
    # A pack really travelled, rather than parity holding because none was built.
    carried = call(workspace, assembled, adapter, blobs_root=blobs_root)
    assert isinstance(carried, SuccessResponseEnvelope)
    assert packed(w13a.plain(dict(carried.result)))["sections"]


def test_the_span_ceiling_truncates_and_says_so(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A span longer than the ceiling comes back as a prefix of itself that states its
    own full length, rather than being cut silently or returned whole."""
    monkeypatch.setattr(code_handler, "CODE_CONTEXT_MAX_SPAN_BYTES", 12)
    result = query(
        workspace,
        payload(architecture.python, semantic_key="pkg.models.make"),
        blobs_root=blobs_root,
    )
    span = spans_of(result)["pkg.models.make"]
    assert span["truncated"] is True
    assert span["byte_length"] == 52
    assert span["text"] == "def make(key"
    assert CODE_CONTEXT_MAX_SPAN_BYTES == 4096


def test_a_hidden_code_endpoint_is_removed_before_the_walk_not_after_it(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D16 over the code graph itself, at the granularity the writer cannot yet produce.

    Every fact of one import cites that snapshot's single manifest evidence, so no two
    code records differ in visibility in a real workspace today. What differs per record
    is the *input* to the ACL, and that is what is supplied here: one `(record id,
    version)` is given a permission label the caller does not hold, and the production
    grant, the production filter and the production walk do the rest.

    The claim is subtraction and nothing else. The hidden definition is gone, every
    relation touching it is gone, and everything else -- the other paths, the other
    depths, the diagnostics, the truncation flags and the coverage -- is exactly what it
    was. A filter applied after the walk would leave its degree behind in `relations`
    or its step behind in `depth_bounded`.
    """
    body = payload(architecture.python, semantic_key="pkg.models.make")
    before = query(workspace, body, blobs_root=blobs_root, principal=READER)
    hidden_key = "pkg.jobs.run"
    identity = next(
        (symbol["record"]["record_id"], symbol["record"]["version"])
        for symbol in before["symbols"]
        if symbol["semantic_key"] == hidden_key
    )

    real = code_handler.read_governed_record_labels

    def with_one_restricted(connection: Any, **selectors: Any) -> Any:
        return {**real(connection, **selectors), identity: (RESTRICTED,)}

    monkeypatch.setattr(code_handler, "read_governed_record_labels", with_one_restricted)
    after = query(workspace, body, blobs_root=blobs_root, principal=READER)

    expected = shape(before)
    expected["symbols"] = [s for s in expected["symbols"] if s[0] != hidden_key]
    expected["relations"] = [
        edge for edge in expected["relations"] if hidden_key not in (edge[1], edge[3])
    ]
    expected["spans"] = [key for key in expected["spans"] if key != hidden_key]
    assert shape(after) == expected
    assert len(after["relations"]) == len(before["relations"]) - 1
    assert hidden_key not in json.dumps(after)


def test_a_hidden_code_relation_between_visible_endpoints_is_never_walked_or_counted(
    workspace: Any, blobs_root: Path, architecture: w13a.Imported, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D16 for the relation's own governed version, which the visibility of its two ends
    does not decide.

    `pkg -> pkg.models.Record` is the only relation `pkg` has, and the only one a depth-2
    `code_context` of `make` follows past its first step. Its own `(record id, version)` is
    given a label the caller does not hold, the way the test above labels a definition;
    neither end is labelled. Both ends stay visible, and the relation is as absent as one
    the import never accepted: it is no step, so `pkg`, reachable only through it, is no
    neighbour and leaves no span; it takes no slot, so a limit of exactly the visible
    relations truncates nothing; it is no frontier, so a walk with nothing past its last
    step is not `depth_bounded`; and it is no degree, so `pkg` has none.

    Falsifier: admit every relation whenever every definition is admitted. The bounded
    answer then still succeeds -- the limit cuts the relation off, so the output
    revalidation never sees it -- and says `truncated` and `depth_bounded` for a relation
    the caller may not know exists; unbounded, or with either end as the subject, it is
    `not_found`.
    """
    hidden = ("pkg", "code.imports", "pkg.models.Record")
    body = payload(architecture.python, semantic_key="pkg.models.make", depth=2)
    before = query(workspace, body, blobs_root=blobs_root, principal=READER)
    identity = next(
        (relation["record"]["record_id"], relation["record"]["version"])
        for relation in before["relations"]
        if (relation["source_key"], relation["relation_type"], relation["target_key"]) == hidden
    )
    expected = shape(before)
    expected["relations"] = [edge for edge in expected["relations"] if edge[1:4] != hidden]
    expected["symbols"] = [s for s in expected["symbols"] if s[0] != "pkg"]
    expected["spans"] = [key for key in expected["spans"] if key != "pkg"]
    # Without it the second step reaches nothing, so nothing is left past the last one.
    expected["coverage"]["depth_bounded"] = False

    real = code_handler.read_governed_record_labels

    def with_one_restricted(connection: Any, **selectors: Any) -> Any:
        return {**real(connection, **selectors), identity: (RESTRICTED,)}

    monkeypatch.setattr(code_handler, "read_governed_record_labels", with_one_restricted)
    # Room for exactly the visible relations first, then the limit `before` was answered
    # under. `pkg.models.Record`, the other end, is still a neighbour in both.
    for limit in (len(expected["relations"]), before["coverage"]["applied_limit"]):
        expected["coverage"]["applied_limit"] = limit
        after = query(workspace, body | {"limit": limit}, blobs_root=blobs_root, principal=READER)
        assert shape(after) == expected
        assert identity[0] not in json.dumps(after)

    # `pkg` still resolves, and the hidden relation was its whole degree.
    alone = query(
        workspace, payload(architecture.python, semantic_key="pkg"),
        blobs_root=blobs_root, principal=READER,
    )
    assert w13a.keys(alone["definitions"]) == ["pkg"]
    assert alone["symbols"] == alone["relations"] == []
    assert alone["coverage"]["depth_bounded"] is False


def test_only_the_queried_imports_facts_are_ever_disclosed(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, architecture: w13a.Imported
) -> None:
    """Retired, other-producer and other-profile facts stay out of a code context.

    W13-A proves the binding is checked; this proves what the *context* discloses after
    it: every governed version named anywhere in the answer -- definitions, neighbours,
    relation records -- is a member of the one import the binding resolved to, and of no
    other import in the workspace.
    """
    body = payload(architecture.python, semantic_key="pkg.models.make")
    first_import = query(workspace, body, blobs_root=blobs_root)["binding"]["import_id"]
    second, receipt = cutover(workspace, blobs_root, runtime, first_import)

    current = query(
        workspace,
        payload(second.python, semantic_key="pkg.models.make"),
        blobs_root=blobs_root,
    )
    assert current["binding"]["import_id"] == receipt.import_id
    disclosed = w13a.records(current)
    assert disclosed <= w13a.members(workspace, receipt.import_id)
    assert not disclosed & w13a.members(workspace, first_import)
    typescript = [
        row[0]
        for row in workspace.holder.connection.execute(
            "SELECT import_id FROM omnivia_code_analysis_imports WHERE producer_id = ?", (TS,)
        )
    ]
    assert not disclosed & w13a.members(workspace, str(typescript[0]))
    # The retired binding is refused outright rather than answered from either import.
    assert dict(refusal(workspace, body, blobs_root=blobs_root).error.details or {}) == {
        "readiness": "not_ready", "reason": "snapshot_not_active",
    }
