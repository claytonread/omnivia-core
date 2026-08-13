"""V06-3 Lane D: `context_pack.build` end to end, through the production dispatcher.

`test_context_pack_determinism.py` is the pure half of this lane and states plainly what it
cannot show: that the handler performs its reads in one coherent database snapshot, that
the L0/L2 filter chain actually *executes* before the freeze, that a real grant propagates
from the request envelope into the build context, where `retrieval_version` comes from,
that the dispatcher propagates the pack, that `ContextPackFrontierTooLarge` maps to
`size_limit_exceeded`, and that anything is registered in the production operation
registry. This file is those integration requirements, and nothing in it is stubbed:

* a real workspace database taken to the canonical schema through the real migrator, with
  0009's governed tables and 0011/0012's projection lifecycle both present;
* every row written through the accepted `fenced_transaction` writer and 0009's own seal
  trigger;
* the real `evidence.search` FTS projection, built and activated by the same two calls
  `service.main.serve` makes;
* the real `AuthenticatedSession` from the production construction site, the real
  `ServiceBinding`, real `RequestEnvelope`s and all twelve checks of
  `authorize_application_request`;
* the real `ApplicationDispatcher`, the registered production handler, and a
  `ContextPackBuildResult` the handler has already put through the contract's own
  `validate_context_pack_build_result` before this file sees it.

**Three observation devices, and what each is for.** A response cannot show where its
material came from, so three production callables are wrapped in place -- the registered
handler (to read the `OperationContext` the dispatcher built), `freeze_context_pack_frontier`
(to read the frozen frontier and the manifest produced at the freeze) and
`build_context_pack` (to read what the builder was actually handed). Each wrapper calls the
production article and returns its value unchanged; none replaces behaviour. Without them
"the ACL stage ran before the freeze" and "no unauthorized candidate reached ranking" would
be inferences from a page that by construction cannot show either.

**Amendment 009, approved 2026-08-10, is what makes this operation possible**, and it is
tested as a pass-through rather than as a feature. The authority, scopes and purpose the
seam computed reach the handler unchanged; the pack and the out-of-band validator call use
those same values; and a handler invoked without any one of them refuses before it reads a
row or builds a value. The last is the one that matters: those three are written verbatim
into a content-addressed artifact, so a build that proceeded without them would attest an
authority nobody granted.

**F2 is here in its production form.** The honest run is the real dispatcher under a
session whose principal is not the configured local owner, so the evidence-label ACL stage
removes one labelled artifact *before* the freeze. The widened run is the same request over
the same storage under the local-owner session, which admits it. The artifact scores, is
ranked last of everything in contention, and is dropped on a budget set to exactly the
honest pack's own `tokens_used` -- so `sections`, `citations`, `evidence` and `records` are
byte-identical between the two, and only the independently held narrow manifest refuses.

**What this file does not claim.** The sensitivity stage runs -- it is inside
`authorized_frontier`, which this handler calls -- but `ContextPackBuildInput` declares no
sensitivity selector, so it admits every candidate and no test here can make it narrow one.
That is stated rather than papered over; the ACL, tombstone and temporal stages are the
three whose narrowing is observable for this operation, and each is exercised below.

Every test states its falsifier.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sqlite3
import textwrap
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
import test_graph_traverse as gt
import test_knowledge_search_vertical as kv
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import application as application_module
from omnivia_core_runtime.service.application import (
    CONTEXT_PACK_BUILD_OPERATION,
    EVIDENCE_SEARCH_OPERATION,
    GRAPH_TRAVERSE_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    KNOWLEDGE_SEARCH_OPERATION,
    LOCAL_TRANSPORT_ADAPTER,
    MEMORY_SEARCH_OPERATION,
    OPERATION_PURPOSES,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import context_pack as handler_module
from omnivia_core_runtime.service.handlers.context_pack import (
    CONTEXT_PACK_POLICY_VERSIONS,
    CONTEXT_PACK_RETRIEVAL_VERSION,
    context_pack_build,
)
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    OperationContext,
    OperationError,
    build_service_registry,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage.context_pack import (
    CONTEXT_PACK_BUILDER_VERSION,
    CONTEXT_PACK_FRESHNESS_PROJECTION_ID,
    CONTEXT_PACK_FRONTIER_FILTERS,
    CONTEXT_PACK_NORMALIZATION_VERSION,
    CONTEXT_PACK_RANKING_VERSION,
    CONTEXT_PACK_RERANKER_DISABLED,
    CONTEXT_PACK_SELECTION_VERSION,
    CONTEXT_PACK_TOKENIZER_ID,
    CONTEXT_PACK_TOKENIZER_VERSION,
    OMISSION_NOT_MATCHED,
    OMISSION_SUPERSEDED,
    OMISSION_TOKEN_BUDGET,
    ContextPackFreeze,
    ContextPackFrozenFrontier,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import EVIDENCE_SEARCH_PROJECTION_ID, fts
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER

from omnivia_core.contracts.v1 import (
    CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
    CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
    CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
    CONTEXT_PACK_SUMMARIZER_DISABLED,
    CONTRACT_VERSION,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_STALE_PROJECTION,
    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    ContextPackBuildResult,
    ContractSemanticError,
    ErrorResponseEnvelope,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    compute_context_pack_artifact_digest,
    encode_response,
    get_operation_metadata,
    validate_context_pack_build_result,
)

#: The workspace every fixture below writes to. M2's and M3's helpers both address it, and
#: both are used here: this operation is the only one that reads L0 evidence and L2
#: governed truth in one answer, so its fixture needs both families of writer.
WORKSPACE_ID = m2.WORKSPACE_ID
INSTALLATION_ID = "inst-pack-0001"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
ENTRY = get_operation_metadata(CONTEXT_PACK_BUILD_OPERATION)

#: Every instant a fixture stamps sits inside the window 0009's seal trigger accepts for
#: this workspace: at or after `m2.BASE_US` and before `m3.BASE_US + 50`, which is the
#: `sealed_at_us` `m3.seal_row` writes. A record stamped after its own seal is refused by
#: the trigger, which is the schema declining to seal a version that did not yet exist.
BASE_US = m2.BASE_US

#: The query every positive build below asks. Short, and present in the fixture's locators
#: and record content in known multiplicities, because the production ranker's first key is
#: how many times the normalized query occurs in a candidate's own canonical content.
QUERY = "alpha"

#: A distinctive value that appears nowhere in this build, so its absence from a refusal
#: can only mean the refusal never carried it. Two spellings, because the fields it is
#: planted in have different value domains: free text for a query, `OpenCode` for a domain
#: scope and a record type.
SENTINEL_TEXT = "zz-caller-sentinel-9f3c1d"
SENTINEL_CODE = "zzcallersentinel.deadbeef"

#: The production grant as `service.main.serve` wires it after this lane's additive edit:
#: the five operations Lane E left it holding, plus `context_pack.build`. Stated as the
#: literal `serve` states rather than derived from the registry --
#: `test_workspace_inspect_refusals.py` is what pins it against `main.py`'s own syntax.
PRODUCTION_OPERATIONS = frozenset(
    {
        WORKSPACE_INSPECT_OPERATION,
        EVIDENCE_SEARCH_OPERATION,
        KNOWLEDGE_SEARCH_OPERATION,
        MEMORY_SEARCH_OPERATION,
        GRAPH_TRAVERSE_OPERATION,
        CONTEXT_PACK_BUILD_OPERATION,
    }
)

#: A principal that is not the configured local owner. `local_owner_label_grant` hands it
#: the empty evidence-label grant, so every labelled artifact is refused by the ACL stage --
#: which is the only lever this build has for making that stage narrow something.
FOREIGN_PRINCIPAL = "other-user"

#: The label the ACL cases attach. An `OpenCode`, as 0008's column domain requires.
RESTRICTED_LABEL = "group.restricted"


# --- a real, owned, fully migrated workspace ----------------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    """A workspace at the canonical schema, carrying M2's chain and nothing else.

    The chain is not this file's choice: 0009's governed evidence links point at
    `evd-0001`, `nrc-0001` and `nsp-0001` by foreign key, so a governed version cannot be
    sealed in a workspace that does not hold them.

    `evd-0001` is then **tombstoned**, in its own fenced write, and that is the whole
    reason this fixture exists rather than `m2.owned`. Left alive it would sit on every
    frontier below scoring nothing, which turns "one unauthorized candidate" into "one plus
    whatever the shared fixture left behind" and puts an unscored candidate *after* the
    ranked ones in every ordering claim -- exactly where F2 needs nothing to be.
    Tombstoned, it is removed by the tombstone stage of the same chain every other
    exclusion below runs through, identically in every run, so every candidate any test
    reasons about is one that test wrote.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m2.insert(
            holder.connection,
            m2.PROVENANCE,
            m2.row_for(
                m2.PROVENANCE,
                provenance_event_id="prv-0001-tombstone",
                provenance_sequence=2,
                action="tombstoned",
                tombstoned_observation=1,
            ),
        )
    yield holder
    holder.connection.close()


def seed_audit(holder: m2.Owned, audit_ref: str) -> None:
    """The M1 audit event one sealed lineage correlates to.

    0009's requirement rather than this file's: the seal trigger refuses an assembly whose
    correlation names no audit row, so a governed seeder that skipped this would be
    writing a shape the accepted writer cannot produce.
    """
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m2.insert(
            holder.connection,
            "omnivia_application_audit_events",
            m3.audit_row(audit_ref),
        )


def seed_artifact(
    holder: m2.Owned,
    evidence_id: str,
    *,
    locator: str,
    at: int,
    label: str | None = None,
    tombstoned: bool = False,
) -> None:
    """One L0 artifact, written through the fenced writer, with its capture event.

    The provenance row is not decoration: `validate_evidence_artifact` refuses an artifact
    whose `provenance_history` is empty, so an artifact seeded without one could never be
    selected into a pack and every claim about it would be vacuous. A tombstone is written
    as a second provenance observation, which is how 0008 records one and therefore how
    `repository.read_evidence_candidates` folds one back out.
    """
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m2.insert(
            holder.connection,
            m2.EVIDENCE,
            m2.row_for(
                m2.EVIDENCE,
                evidence_id=evidence_id,
                source_native_id=evidence_id,
                source_locator=locator,
                ingested_at_us=at - 1,
                recorded_at_us=at,
            ),
        )
        m2.insert(
            holder.connection,
            m2.PROVENANCE,
            m2.row_for(
                m2.PROVENANCE,
                provenance_event_id=f"prv-{evidence_id}-1",
                evidence_id=evidence_id,
                source_native_id=evidence_id,
                provenance_sequence=1,
            ),
        )
        if tombstoned:
            m2.insert(
                holder.connection,
                m2.PROVENANCE,
                m2.row_for(
                    m2.PROVENANCE,
                    provenance_event_id=f"prv-{evidence_id}-2",
                    evidence_id=evidence_id,
                    source_native_id=evidence_id,
                    provenance_sequence=2,
                    action="tombstoned",
                    tombstoned_observation=1,
                ),
            )
        if label is not None:
            m2.insert(
                holder.connection,
                m2.LABELS,
                m2.row_for(
                    m2.LABELS,
                    label_event_id=f"lbl-{evidence_id}-1",
                    evidence_id=evidence_id,
                    permission_label=label,
                ),
            )


def seed_record(holder: m2.Owned, record_id: str, *, text: str, at: int) -> None:
    """One accepted, sealed, canonical governed version, through Lane C's own seeder.

    `kv.seal_record` writes the whole lineage 0009 requires -- a sealed human candidate and
    the governed version promoted from it -- so what this file reads back is the shape the
    accepted writer produces rather than a shape only this test would ever create.
    """
    seed_audit(holder, f"audit-{record_id}")
    kv.seal_record(
        holder,
        record_id=record_id,
        audit_ref=f"audit-{record_id}",
        text=text,
        recorded_at_us=at,
    )


def seed_supersession(holder: m2.Owned, record_id: str) -> None:
    """One corrected record: `-1` superseded by `-2`, through Lane E's own seeder.

    Two versions is the minimum that puts anything in the `history` view, and the history
    view is what makes this operation's frontier accounting non-trivial: a superseded
    version is frozen, digested into the manifest and accounted for by omission, and is
    never content. `gt.chain` writes the supersession row and the `record.superseded`
    provenance event 0009's trigger requires, which is why it is reused rather than
    reimplemented here.
    """
    seed_audit(holder, f"audit-{record_id}")
    gt.chain(
        holder,
        record_id=record_id,
        audit_ref=f"audit-{record_id}",
        length=2,
        first_us=gt.HISTORY_US + 1_000,
    )


def project(holder: m2.Owned) -> fts.BuildOutcome:
    """Build and materialise the `evidence.search` projection, as `serve` does.

    The same two calls `service.main.serve` makes, in the same order, from the same three
    facts a started service holds. `test_evidence_search_vertical` is what holds that copy
    to the original: it starts the real service and reads `serve`'s own syntax.
    """
    outcome = fts.build_search_projection(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        now_us=gt.HISTORY_US + 10_000_000,
    )
    fts.open_search_projection(holder.connection, workspace_id=WORKSPACE_ID)
    return outcome


# --- the production application path ------------------------------------------


class Served:
    """The service the handler reads its connection off, and nothing else.

    `ServiceRunner` exposes the sole exclusive connection as `.connection`, and that is the
    only attribute this handler touches. Presenting exactly that surface is what keeps the
    test honest about what a handler is allowed to reach.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


def request_for(
    payload: Any,
    *,
    operation: str = CONTEXT_PACK_BUILD_OPERATION,
    **overrides: Any,
) -> RequestEnvelope:
    """A request the catalogue validator accepts, built from the frozen entry.

    Every field the catalogue decides -- the required scopes, the required capability and
    the idempotency and precondition postures -- is read off the entry, so a refusal below
    is never a malformed request wearing a denial's clothes.
    """
    entry = get_operation_metadata(operation)
    fields: dict[str, Any] = {
        "request_id": "req-pack-1",
        "correlation_id": "corr-pack-1",
        "trace_id": "trace-pack-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": WORKSPACE_ID,
        "scopes": tuple(entry.scope.required_scopes),
        "purpose": KNOWLEDGE_RETRIEVAL_PURPOSE,
        "required_capabilities": (
            CapabilityRequirement(
                id=entry.required_capability.id,
                minimum_version=entry.required_capability.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": None,
        "mutation_precondition": None,
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestEnvelope(
        operation=operation, metadata=RequestMetadata(**fields), input=payload
    )


def build_input(
    *, query: str = QUERY, token_budget: int = 4096, **extra: Any
) -> dict[str, Any]:
    """One `context_pack.build` wire payload, with only the selectors a test names."""
    payload: dict[str, Any] = {
        "query": query,
        "mode": "deterministic_view",
        "token_budget": token_budget,
    }
    payload.update(extra)
    return payload


def production_session(
    *, principal: str = LOCAL_PRINCIPAL, **overrides: Any
) -> Any:
    """The session the production construction site builds, optionally narrowed or widened.

    `operations` is passed because `service.main.serve` passes it: under the accepted
    parameterised shape the operation set is a literal in the constructor's caller, so a
    test that wants *the production session* has to state the production literal.
    """
    session = local_owner_session(
        principal_id=principal,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        operations=PRODUCTION_OPERATIONS,
    )
    return replace(session, **overrides) if overrides else session


def production_path(
    holder: m2.Owned,
    *,
    session: Any = None,
    principal: str = LOCAL_PRINCIPAL,
    service: object | None = None,
) -> ApplicationDispatcher:
    """The application path, composed exactly as `service.main.serve` composes it."""
    registry = build_application_registry()
    served = Served(holder.connection) if service is None else service
    return ApplicationDispatcher(
        registry=registry,
        session=production_session(principal=principal) if session is None else session,
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=(
                    session.principal_id if session is not None else principal
                ),
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            served,
        ),
        record=None,
        service=served,
    )


def answered(response: ResponseEnvelope) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def refused(response: ResponseEnvelope) -> ErrorResponseEnvelope:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def wire_text(response: ResponseEnvelope) -> str:
    """The whole encoded response as one string, for leak assertions.

    Asserting on the message alone would miss a value that reached some other field, and
    the requirement is that the *refusal* discloses nothing.
    """
    return json.dumps(encode_response(response), sort_keys=True)


# --- the observation devices ---------------------------------------------------


class Observed:
    """What one dispatch actually did, read off the production callables it ran.

    Every field here is captured by a wrapper that calls the production article and
    returns its value unchanged. The wrappers add no behaviour and remove none: without
    them, "the ACL stage ran before the freeze" and "no unauthorized candidate reached
    ranking" could only be inferred from a response that by construction cannot show
    either, which is the inference this whole lane exists to replace with a measurement.
    """

    def __init__(self) -> None:
        self.contexts: list[OperationContext] = []
        self.freezes: list[ContextPackFreeze] = []
        self.built: list[ContextPackFrozenFrontier] = []
        self.build_contexts: list[Any] = []
        self.validated: list[dict[str, Any]] = []

    @property
    def context(self) -> OperationContext:
        assert len(self.contexts) == 1, self.contexts
        return self.contexts[0]

    @property
    def freeze(self) -> ContextPackFreeze:
        assert len(self.freezes) == 1, self.freezes
        return self.freezes[0]

    @property
    def build_context(self) -> Any:
        assert len(self.build_contexts) == 1, self.build_contexts
        return self.build_contexts[0]

    @property
    def validation(self) -> dict[str, Any]:
        assert len(self.validated) == 1, self.validated
        return self.validated[0]


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> Observed:
    """Wrap the four production callables this file needs to see the inside of.

    `application.context_pack_build` is patched *before* `build_application_registry` runs,
    which is why the interception is at the registration site rather than at the handler:
    the registry captures the function object it is given, so a later patch of the handler
    module would be invisible to a registry already built. What is registered is a wrapper
    around the production handler, and the production handler is what answers.
    """
    record = Observed()
    real_handler = application_module.context_pack_build
    real_freeze = handler_module.freeze_context_pack_frontier
    real_build = handler_module.build_context_pack
    real_validate = handler_module.validate_context_pack_build_result

    def handler(context: OperationContext) -> Any:
        record.contexts.append(context)
        return real_handler(context)

    def freeze(**kwargs: Any) -> ContextPackFreeze:
        frozen = real_freeze(**kwargs)
        record.freezes.append(frozen)
        return frozen

    def build(frontier: ContextPackFrozenFrontier, build_context: Any) -> Any:
        record.built.append(frontier)
        record.build_contexts.append(build_context)
        return real_build(frontier, build_context)

    def validate(result: Any, **kwargs: Any) -> None:
        record.validated.append({"result": result, **kwargs})
        real_validate(result, **kwargs)

    monkeypatch.setattr(application_module, "context_pack_build", handler)
    monkeypatch.setattr(handler_module, "freeze_context_pack_frontier", freeze)
    monkeypatch.setattr(handler_module, "build_context_pack", build)
    monkeypatch.setattr(handler_module, "validate_context_pack_build_result", validate)
    return record


def frontier_identities(
    manifest: ContextPackAuthorizedCandidateSetManifest,
) -> set[tuple[str, str, str]]:
    """Every identity the frozen frontier held, read off the out-of-band manifest."""
    identities: set[tuple[str, str, str]] = set()
    for candidate in manifest.candidates:
        if isinstance(candidate, ContextPackAuthorizedEvidenceCandidate):
            identities.add(
                (candidate.partition, candidate.evidence_id, candidate.content_checksum)
            )
        else:
            identities.add((candidate.partition, candidate.record_id, candidate.version))
    return identities


def selected_identities(result: ContextPackBuildResult) -> set[tuple[str, str, str]]:
    """Every identity this pack actually returned, partition-tagged."""
    return {
        (
            CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
            item.evidence_id,
            item.content_checksum,
        )
        for item in result.evidence
    } | {
        (
            CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
            item.provenance.identity.record_id,
            item.provenance.identity.version,
        )
        for item in result.records
    }


def accounted_identities(result: ContextPackBuildResult) -> set[tuple[str, str, str]]:
    """Every identity an omission accounts for, parsed back out of its `path`."""
    identities: set[tuple[str, str, str]] = set()
    for omission in result.omissions:
        assert omission.path is not None
        partition, rest = omission.path.split("/", 1)
        first, second = rest.rsplit("@", 1)
        identities.add((partition, first, second))
    return identities


#: The four members of a response that carry what the caller actually received. Every
#: "byte identical" claim below is a claim about exactly these, read off the wire object
#: rather than compared as DTOs, because the wire object is what a caller sees.
SELECTED_CONTENT_MEMBERS = ("sections", "citations", "evidence", "records")


def selected_content(result: ContextPackBuildResult) -> dict[str, Any]:
    wire = result.to_wire()
    return {member: wire[member] for member in SELECTED_CONTENT_MEMBERS}


def artifact_digest(result: ContextPackBuildResult) -> str:
    """The contract's own artifact digest, recomputed over this exact value.

    Every mutation below goes through this helper rather than reading `pack_id`, because
    reading a digest a mutated document still carries would compare a stale value with
    itself. The two members the rule removes are blanked first, so what is hashed is the
    reduction the contract defines and not a document with a foreign digest inside it.
    """
    placeholder = "sha256:" + "0" * 64
    blank = replace(
        result,
        pack_id=placeholder,
        reproducibility=replace(result.reproducibility, artifact_checksum=placeholder),
    )
    return compute_context_pack_artifact_digest(blank.to_wire())


def reseal(result: ContextPackBuildResult) -> ContextPackBuildResult:
    """Recompute a mutated pack's content address, so a refusal is about the mutation.

    Without it, every mutated pack below would be refused for carrying a stale checksum,
    and the assertion would be about the digest rule rather than about the rule under
    test.
    """
    digest = artifact_digest(result)
    return replace(
        result,
        pack_id=digest,
        reproducibility=replace(result.reproducibility, artifact_checksum=digest),
    )


def judge(
    result: ContextPackBuildResult, call: dict[str, Any], **overrides: Any
) -> None:
    """Re-run the contract's judge with exactly the arguments the handler used.

    `call` is one recorded invocation rather than constants restated here: the property
    under test in every D and E case below is that *this pack*, judged the way the
    production handler judged it, is accepted or refused for the stated reason. A
    fixture-built expectation would be a second opinion about what the handler did.
    """
    arguments = {name: value for name, value in call.items() if name != "result"}
    arguments.update(overrides)
    validate_context_pack_build_result(result, **arguments)


def pack(response: ResponseEnvelope) -> ContextPackBuildResult:
    return ContextPackBuildResult.from_wire(answered(response).result)


# --- the standard fixture -----------------------------------------------------


def standard_workspace(holder: m2.Owned) -> None:
    """Five candidates, chosen so every accounting claim below is non-trivial.

    * `evd-alpha` -- matches the query twice, and is selected;
    * `evd-beta` -- matches it not at all, so it is frontier and omission but never
      content;
    * `rec-alpha` -- a current canonical governed version matching once, and selected;
    * `rec-beta` -- a current canonical version matching not at all;
    * `rec-corrected` -- two versions, of which the first is superseded. Its `-2` is in
      `current_canonical` and its `-1` is in `history`, so the frontier carries both
      partitions and the pack carries only one of them.

    Two selected, four accounted for, and the two governed partitions disjoint: that is
    what makes E4's arithmetic a measurement rather than a restatement of an empty set.
    """
    seed_artifact(
        holder, "evd-alpha", locator="archive://alpha-alpha.md", at=BASE_US + 1
    )
    seed_artifact(holder, "evd-beta", locator="archive://beta.md", at=BASE_US + 2)
    seed_record(holder, "rec-alpha", text="alpha statement", at=BASE_US + 3)
    seed_record(holder, "rec-beta", text="beta statement", at=BASE_US + 4)
    seed_supersession(holder, "rec-corrected")
    project(holder)


@pytest.fixture
def standard(owned: m2.Owned) -> m2.Owned:
    standard_workspace(owned)
    return owned


# --- 1. the vertical ----------------------------------------------------------


def test_v1_a_deterministic_view_build_over_a_real_authoritative_snapshot(
    standard: m2.Owned, observed: Observed
) -> None:
    """The whole path, once, with nothing stubbed on it.

    The handler runs the contract's own `validate_context_pack_build_result` before it
    returns, so a successful response has already satisfied every rule that validator
    carries -- mode and request binding, selected-partition membership in the frozen
    manifest, citation resolution and coverage in both directions, section ordering and
    token accounting, the complete reproducibility record, and the content addressing. The
    assertions here are the facts *this* fixture makes checkable on top of that.

    Falsifier: any of them is a fact a build could get wrong while still returning a
    conformant document -- selecting the superseded version, ranking the unmatched
    candidates in, attesting a frontier that is not the one that was frozen, or reporting a
    freshness that names a projection this operation was not served from.
    """
    response = production_path(standard).dispatch(request_for(build_input()))
    result = pack(response)

    # Two selected, and exactly the two that match.
    assert {section.kind for section in result.sections} == {
        "evidence_artifact",
        "governed_record",
    }
    assert [item.evidence_id for item in result.evidence] == ["evd-alpha"]
    assert [item.provenance.identity.record_id for item in result.records] == [
        "rec-alpha"
    ]
    # History is frontier input and never content, whatever it matched.
    assert result.history == ()
    assert result.context_models == ()

    # The frontier the builder ranked over is the one the freeze produced, and the pack
    # attests that freeze's own checksum.
    assert observed.built == [observed.freeze.frontier]
    assert (
        result.reproducibility.authorization_context.authorized_candidate_set_checksum
        == observed.freeze.frontier.candidate_set_checksum
    )
    assert frontier_identities(observed.freeze.manifest) == {
        (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, "evd-alpha", m2.DIGEST_A),
        (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, "evd-beta", m2.DIGEST_A),
        (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-alpha", "ver-rec-alpha"),
        (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-beta", "ver-rec-beta"),
        (
            CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
            "rec-corrected",
            "ver-rec-corrected-2",
        ),
        (
            CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
            "rec-corrected",
            "ver-rec-corrected-1",
        ),
    }

    # The attested chain is the chain this operation runs, and the freshness names the one
    # projection it is served from, with the values that projection's own activated build
    # carries in the same snapshot.
    assert observed.freeze.frontier.filters_applied == CONTEXT_PACK_FRONTIER_FILTERS
    freshness = result.reproducibility.freshness
    assert set(freshness.projection_versions) == {EVIDENCE_SEARCH_PROJECTION_ID}
    assert set(freshness.projection_watermarks) == {EVIDENCE_SEARCH_PROJECTION_ID}
    assert freshness.stale is False
    assert CONTEXT_PACK_FRESHNESS_PROJECTION_ID == EVIDENCE_SEARCH_PROJECTION_ID

    # Possession grants nothing, and the pack is its own address.
    assert result.fresh_authorization_required is True
    assert result.pack_id == artifact_digest(result)
    assert result.pack_id == result.reproducibility.artifact_checksum


def test_v1b_the_judge_is_handed_the_freezes_own_manifest_and_the_snapshots_own_facts(
    standard: m2.Owned, observed: Observed
) -> None:
    """§8.1's binding clause, as object identity rather than as equality.

    The manifest the validator judges against must be the value
    `freeze_context_pack_frontier` produced, before the first ranking decision. Equality
    would not establish that: a manifest rebuilt from the response, from the pack or from
    the selected items would be *equal* to the freeze's for every honest build and would
    agree with a dishonest one by construction, which is exactly the failure F2 exercises.
    Identity is the claim that can only be satisfied by passing the freeze's own value
    through.

    The same reasoning applies one level down to the freshness and the instant: the
    freshness handed to the judge is a value the handler built from the snapshot's own
    projection material, so the validator's "one staleness to the envelope and another to
    the reader" rule compares two values that can disagree instead of the pack with itself.

    Falsifier: replace `expected_authorized_candidate_set=frozen.manifest` with a manifest
    rebuilt from `result`, and every other test in this file still passes.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))
    call = observed.validation

    assert call["expected_authorized_candidate_set"] is observed.freeze.manifest
    assert call["result"] is not None
    assert call["expected_workspace_id"] == WORKSPACE_ID
    assert call["expected_policy_versions"] is CONTEXT_PACK_POLICY_VERSIONS

    # The freshness is the handler's own statement, equal to the pack's rather than read
    # off it -- the validator is what requires the two to agree.
    supplied = call["response_freshness"]
    assert supplied is not result.reproducibility.freshness
    assert supplied == result.reproducibility.freshness
    assert supplied.as_of == call["canonical_resolution_time"]
    assert dict(supplied.projection_versions) == dict(
        observed.freeze.frontier.projection_versions
    )
    assert dict(supplied.projection_watermarks) == dict(
        observed.freeze.frontier.projection_watermarks
    )
    # And the instant the judge binds is the one the build context supplied, not a second
    # reading of anything.
    assert call["canonical_resolution_time"] == (
        observed.build_context.canonical_resolution_time
    )
    assert call["expected_normalized_query"] == observed.build_context.normalized_query


def test_v2_two_builds_at_one_pinned_instant_are_byte_identical(
    standard: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D10, at the integration level: one instant in, one pack out, twice.

    The pure builder has no clock at all -- `test_context_pack_determinism.py` proves that
    structurally, by mutation -- so this handler's single `time.time_ns` read is the only
    clock on the whole path. Pin it and two dispatches over one unchanged workspace must
    produce the identical document, `pack_id` included.

    Falsifier: a second clock read anywhere below the pin -- for `generated_at`, for the
    freshness `as_of`, or for a per-build nonce -- and these two documents differ while
    every other test in this file stays green.
    """
    monkeypatch.setattr(handler_module.time, "time_ns", lambda: 1_800_000_000_123_456_789)

    first = pack(production_path(standard).dispatch(request_for(build_input())))
    second = pack(production_path(standard).dispatch(request_for(build_input())))

    assert json.dumps(first.to_wire(), sort_keys=True) == json.dumps(
        second.to_wire(), sort_keys=True
    )
    assert first.pack_id == second.pack_id
    # The pinned instant, rendered once and used for all three of the contract's instants.
    reproducibility = first.reproducibility
    assert reproducibility.canonical_resolution_time == "2027-01-15T08:00:00.123Z"
    assert reproducibility.generated_at == reproducibility.canonical_resolution_time
    assert reproducibility.freshness.as_of == reproducibility.canonical_resolution_time


# --- 2. Amendment 009: the effective-authority pass-through -------------------


def narrowed_dispatch(
    holder: m2.Owned, observation: Observed
) -> tuple[ResponseEnvelope, Any]:
    """One dispatch under a session with roles and a raised capability floor.

    Three things are made to differ from the session's own grant so that "the effective
    values" is a claim with content rather than a coincidence:

    * the session holds two roles and the request claims one, so the effective role set is
      strictly narrower than the grant;
    * the session holds three scopes and the request claims one, so the effective scope set
      is strictly narrower than the grant;
    * the session holds `context_pack.build` at `1.4` while this build's own capability
      snapshot supports `1.0`, so the *effective* version is the weaker of the two and is
      neither the session's nor the request's floor.
    """
    session = production_session(
        roles=frozenset({"auditor", "owner"}),
        capabilities=tuple(
            CapabilityRef(id=ref.id, version="1.4")
            if ref.id == ENTRY.required_capability.id
            else ref
            for ref in production_session().capabilities
        ),
    )
    response = production_path(holder, session=session).dispatch(
        request_for(
            build_input(),
            scopes=("memory:read",),
            principal_claim=PrincipalClaim(claimed_roles=("auditor",)),
        )
    )
    return response, session


def test_a009_the_narrowed_authority_reaches_the_handler_unchanged(
    standard: m2.Owned, observed: Observed
) -> None:
    """The seam's effective values arrive at the handler, and they are the effective ones.

    Falsifier: each of the three assertions has a wrong answer that a careless
    pass-through would produce. Roles from the session would be `{'auditor', 'owner'}`;
    scopes from the session would be all three; the capability version from the session
    would be `1.4` and from the request's floor `1.0` *for the wrong reason*. The
    assertions below pin the effective answer and state what it is not.
    """
    response, session = narrowed_dispatch(standard, observed)
    answered(response)
    context = observed.context

    assert context.authority is not None
    assert context.scopes is not None
    assert context.purpose is not None

    assert context.authority.principal_id == LOCAL_PRINCIPAL
    assert context.authority.roles == ("auditor",)
    assert set(context.authority.roles) != session.roles
    assert context.scopes == ("memory:read",)
    assert frozenset(context.scopes) != session.scopes
    assert context.purpose == KNOWLEDGE_RETRIEVAL_PURPOSE

    # The effective capability version: the weaker of the session's grant and this build's
    # own snapshot, and equal to neither side's independent claim about the other.
    assert context.authority.capabilities == (
        CapabilityRef(id=ENTRY.required_capability.id, version="1.0"),
    )
    assert CapabilityRef(id=ENTRY.required_capability.id, version="1.4") in (
        session.capabilities
    )


def test_a009_the_result_and_the_validator_call_use_those_same_effective_values(
    standard: m2.Owned, observed: Observed
) -> None:
    """The pack records them, and the out-of-band judge is handed them -- not copies.

    Identity rather than equality on the authority and the scopes, because equality would
    also hold for a handler that rebuilt an equal value from the session: the assertion
    that matters is that the object the seam produced is the object that was written down
    and the object the validator was given.

    Falsifier: reconstruct the authority in the handler from `session.principal_id`,
    `session.roles` and `session.capabilities` -- a plausible-looking edit that produces a
    *wider* authority than the request ran under -- and both identity assertions fail while
    the pack still validates against itself.
    """
    response, _ = narrowed_dispatch(standard, observed)
    result = pack(response)
    context = observed.context
    call = observed.validation
    recorded = result.reproducibility.authorization_context

    assert observed.build_context.authority is context.authority
    assert observed.build_context.scopes is context.scopes
    assert observed.build_context.purpose is context.purpose

    assert call["expected_authority"] is context.authority
    assert call["expected_purpose"] is context.purpose
    assert call["expected_scopes"] == frozenset(context.scopes)

    assert recorded.authority == context.authority
    assert recorded.scopes == context.scopes
    assert recorded.purpose == context.purpose
    assert recorded.workspace_id == context.workspace_id
    assert dict(recorded.policy_versions) == dict(CONTEXT_PACK_POLICY_VERSIONS)
    assert recorded.pre_ranking_authorization_enforced is True


class Forbidden:
    """A service handle that fails the moment anything reaches for it.

    The refusal cases below claim the handler stopped *before* storage. A service that
    quietly answered `None` would let a handler read storage and still pass, because
    `getattr(..., None)` would have swallowed the reach; raising is what turns the claim
    into a measurement.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the handler reached for service.{name}")


@pytest.mark.parametrize("absent", ["authority", "scopes", "purpose"])
def test_a009_a_missing_effective_value_refuses_before_any_storage_or_builder_call(
    absent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the three, absent, refuses -- and refuses before it could have read a row.

    Directly against the handler, because the production dispatcher always supplies all
    three: this is the wiring fault the refusal exists for, and a test that could only
    reach it through the dispatcher could not reach it at all.

    Three independent traps establish "before": the service raises on any attribute access,
    and the freeze and the builder are replaced with functions that fail if they are
    called. A handler that decoded, opened the connection and only then noticed would trip
    the first; one that froze an empty frontier and refused at the builder would trip the
    other two.

    Falsifier: move the check below `_input`, or below the connection lookup, and the
    corresponding trap fires. Drop the check entirely and the builder produces a pack
    attesting an authority nobody granted.
    """

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the handler reached the freeze or the builder")

    monkeypatch.setattr(handler_module, "freeze_context_pack_frontier", unreachable)
    monkeypatch.setattr(handler_module, "build_context_pack", unreachable)

    supplied: dict[str, Any] = {
        "authority": _authority(),
        "scopes": ("memory:read",),
        "purpose": KNOWLEDGE_RETRIEVAL_PURPOSE,
    }
    supplied[absent] = None

    with pytest.raises(OperationError) as refusal:
        context_pack_build(
            OperationContext(
                request_for(build_input()),
                LOCAL_PRINCIPAL,
                WORKSPACE_ID,
                PRODUCTION_OPERATIONS,
                Forbidden(),
                **supplied,
            )
        )

    assert refusal.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    # One frozen module message, the *same* one for all three parametrisations. It names
    # the rule -- and therefore all three field names -- rather than which of them this
    # wiring omitted, because which one was absent is a fact about this server's own
    # composition and nothing a caller may be told.
    assert refusal.value.message == handler_module._MESSAGE_NO_EFFECTIVE_AUTHORITY
    assert refusal.value.retry_class == "non_retryable"
    assert refusal.value.__context__ is None


def _authority() -> Any:
    """A well-formed `GrantedAuthority`, for the two cases that supply one."""
    from omnivia_core.contracts.v1 import GrantedAuthority

    return GrantedAuthority(
        principal_id=LOCAL_PRINCIPAL,
        roles=(),
        capabilities=(
            CapabilityRef(id=ENTRY.required_capability.id, version="1.0"),
        ),
    )


def test_a009_an_empty_but_valid_grant_is_not_absence(
    standard: m2.Owned, observed: Observed
) -> None:
    """No roles is an answer about a real grant, and the build proceeds on it.

    The production session holds no roles at all, so the ordinary production dispatch is
    already this case: the effective authority carries an empty `roles` tuple and the pack
    records it. Asserted explicitly because the refusal above must key on `None` and
    nothing else -- a check written as `if not context.authority.roles` would refuse every
    request this build actually serves.
    """
    response = production_path(standard).dispatch(request_for(build_input()))
    result = pack(response)

    assert production_session().roles == frozenset()
    assert observed.context.authority is not None
    assert observed.context.authority.roles == ()
    assert result.reproducibility.authorization_context.authority.roles == ()


def test_a009_the_dispatcher_passes_the_seam_values_and_reconstructs_none_of_them(
    standard: m2.Owned,
) -> None:
    """Read off `ApplicationDispatcher.dispatch`'s own syntax, not off a behaviour.

    Behaviour cannot separate "passed through" from "rebuilt from the same session that
    produced it" in the ordinary case, because the two agree whenever the request narrows
    nothing. The syntax can: the three keywords must be exactly `context.<field>`, where
    `context` is the value `authorize_application_request` returned.

    Falsifier: `authority=self.session` anything, `scopes=tuple(self.session.scopes)`,
    `purpose=request.metadata.purpose` or a module constant -- each is a plausible edit,
    each would pass every behavioural test in this file that does not narrow, and each is
    refused here.
    """
    source = textwrap.dedent(inspect.getsource(ApplicationDispatcher._dispatch))
    call = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OperationContext"
    )
    passed = {
        keyword.arg: keyword.value
        for keyword in call.keywords
        if keyword.arg in {"authority", "scopes", "purpose"}
    }

    assert set(passed) == {"authority", "scopes", "purpose"}
    for name, value in passed.items():
        assert isinstance(value, ast.Attribute), name
        assert value.attr == name
        assert isinstance(value.value, ast.Name)
        assert value.value.id == "context"


def test_a009_and_v06_5_the_four_fields_are_optional_and_service_keeps_its_fifth_slot() -> None:
    """The amendment's shape constraint, asserted as the shape.

    `service` stays fifth and the four new fields follow it, so every direct-handler
    construction that predates the amendment -- five positional arguments, or four plus
    `service=` -- still builds. All four default to `None`, which is what lets those
    constructions exist at all.

    Falsifier: insert any of the four before `service` and the positional construction
    below binds a `GrantedAuthority` to `service`; give any of them a non-`None` default
    and absence stops being representable, which is the one thing the refusal above needs.
    """
    fields = dataclasses.fields(OperationContext)

    assert [field.name for field in fields] == [
        "request",
        "principal",
        "workspace_id",
        "granted_operations",
        "service",
        "authority",
        "scopes",
        "purpose",
        "authorization",
    ]
    for field in fields[5:]:
        assert field.default is None

    legacy = OperationContext(
        request_for(build_input()),
        LOCAL_PRINCIPAL,
        WORKSPACE_ID,
        PRODUCTION_OPERATIONS,
        Served,
    )
    assert legacy.service is Served
    assert (
        legacy.authority,
        legacy.scopes,
        legacy.purpose,
        legacy.authorization,
    ) == (None, None, None, None)


# --- 3. authorization before the freeze ---------------------------------------


def test_the_acl_stage_runs_before_the_freeze_and_narrows_the_frontier(
    standard: m2.Owned, observed: Observed
) -> None:
    """A labelled artifact is absent from the frozen frontier under a non-owner principal.

    The negative alone would pass against a build that never froze the artifact for any
    reason at all, so the complement is asserted in the same test: the same request, the
    same storage, the configured local owner instead, and the artifact is on the frontier.
    The difference between the two runs is one grant and nothing else.

    Falsifier: move the ACL stage after the freeze and the excluded run's manifest gains
    the identity, while the response it returns is unchanged -- which is F2, below.
    """
    seed_artifact(
        standard,
        "evd-restricted",
        locator="archive://alpha.md",
        at=BASE_US + 5,
        label=RESTRICTED_LABEL,
    )
    # Level again after the write, so both runs below answer rather than refusing on
    # freshness -- which is a different rule, tested on its own further down.
    project(standard)
    identity = (
        CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
        "evd-restricted",
        m2.DIGEST_A,
    )

    assert FOREIGN_PRINCIPAL != CONFIGURED_LOCAL_OWNER
    answered(
        production_path(
            standard,
            session=production_session(principal=FOREIGN_PRINCIPAL),
            principal=FOREIGN_PRINCIPAL,
        ).dispatch(request_for(build_input()))
    )
    excluded = observed.freezes[0]

    assert identity not in frontier_identities(excluded.manifest)
    assert "evd-restricted" not in {
        item.artifact.evidence_id for item in excluded.frontier.evidence
    }

    # The complement: the same request, the same storage, the configured local owner.
    answered(production_path(standard).dispatch(request_for(build_input())))
    admitted = observed.freezes[1]

    assert identity in frontier_identities(admitted.manifest)
    # And the difference between the two frontiers is exactly that one candidate, so the
    # grant is what moved and nothing else did.
    assert frontier_identities(admitted.manifest) - frontier_identities(
        excluded.manifest
    ) == {identity}


def test_no_unauthorized_candidate_reaches_ranking_or_assembly(
    standard: m2.Owned, observed: Observed
) -> None:
    """Two exclusions, and what the builder was handed, measured rather than inferred.

    A tombstoned artifact and an artifact recorded after the resolution instant are removed
    by two different stages of the chain the frontier attests, and the fixture's own
    tombstoned `evd-0001` is a third instance of the first. What the builder received is
    `observed.built[0]`, which is the freeze's own frontier and the only value
    `build_context_pack` can reach -- that module holds no store, which
    `test_context_pack_determinism.py` proves by mutation rather than by assertion. So
    absence from that value is absence from ranking, from selection and from assembly, not
    merely absence from the page.

    Falsifier: each id below is a perfectly good artifact that would be selected if its
    stage did not run -- both carry the query in their locators, twice and three times
    over.

    **The workspace stage is not exercised here, and it is not silently skipped either.**
    A cross-workspace artifact cannot be written at all on this path: the mutation guard
    binds to the workspace the lease was taken for and refuses an insert naming another,
    so the row this test would need is one the accepted writer cannot produce. That stage
    is proved where it can be: `freeze_context_pack_frontier` refuses a foreign candidate
    outright (G2, in the determinism file) and the seam refuses a foreign workspace two
    checks before any of this runs.
    """
    seed_artifact(
        standard,
        "evd-tombstoned",
        locator="archive://alpha-alpha-alpha.md",
        at=BASE_US + 6,
        tombstoned=True,
    )
    # Far enough ahead that no wall-clock resolution instant reaches it.
    seed_artifact(
        standard,
        "evd-not-yet",
        locator="archive://alpha-alpha.md",
        at=4_000_000_000_000_000,
    )
    # New evidence moves the authoritative checkpoint, so the projection has to be brought
    # level again or this operation refuses before it reaches any of the above -- which is
    # the freshness rule two tests below, and not what this one is measuring.
    project(standard)

    result = pack(production_path(standard).dispatch(request_for(build_input())))

    handed = observed.built[0]
    reachable = {item.artifact.evidence_id for item in handed.evidence}
    assert handed is observed.freeze.frontier
    assert reachable == {"evd-alpha", "evd-beta"}
    for excluded in ("evd-0001", "evd-tombstoned", "evd-not-yet"):
        assert excluded not in reachable
        assert excluded not in json.dumps(observed.freeze.manifest.to_wire())
        assert excluded not in json.dumps(result.to_wire())


# --- F2: the acceptance test of this stage ------------------------------------


def f2_workspace(holder: m2.Owned) -> None:
    """A frontier whose every member scores, with the restricted artifact scoring lowest.

    Relevance is how many times the normalized query occurs in a candidate's own canonical
    content, so the multiplicities below fix the ranked order outright -- four, three, two,
    then one -- and "ranked last" is a statement about the ordering rather than about a
    tie-break. Nothing here scores zero, which is what lets *last in the ranked order* and
    *last in contention* be the same claim.
    """
    seed_record(holder, "rec-top", text="alpha alpha alpha alpha", at=BASE_US + 1)
    seed_artifact(
        holder, "evd-high", locator="archive://alpha-alpha-alpha.md", at=BASE_US + 2
    )
    seed_record(holder, "rec-low", text="alpha alpha", at=BASE_US + 3)
    seed_artifact(
        holder,
        "evd-restricted",
        locator="archive://alpha.md",
        at=BASE_US + 4,
        label=RESTRICTED_LABEL,
    )
    project(holder)


def test_f2_one_unauthorized_candidate_ranked_last_and_dropped_on_the_budget_is_caught(
    owned: m2.Owned, observed: Observed
) -> None:
    """F2, through the production dispatcher. The one test a lane cannot pass cheaply.

    Two dispatches over one unchanged workspace. The honest one runs under a session whose
    principal is not the configured local owner, so the evidence-label ACL stage removes
    `evd-restricted` before the freeze. The widened one is the same request over the same
    storage under the local-owner session, which admits it -- which is exactly the state a
    build with its ACL stage relocated after ranking would be in.

    The budget is set to the honest pack's own `tokens_used`, so the widened build's extra
    candidate scores, competes, is ranked last and is dropped on the budget. Five claims:

    1. it really was on the widened frontier and really was ranked last -- its omission is
       the *budget* code, which only the last-fitting candidate can carry, and it is absent
       from the honest frontier entirely;
    2. it scored, so it was in contention rather than filtered by the query;
    3. selection dropped it: it is in neither pack's selected identities;
    4. the caller cannot tell -- `sections`, `citations`, `evidence` and `records` are
       byte-identical between the two;
    5. the independently held narrow manifest refuses the widened pack, on the candidate
       checksum.

    Falsifier: claim 4 is what makes the rest necessary. Every result-level assertion in
    this file passes on both packs, so a build that ranked material its caller was never
    entitled to see would look correct in all of them.
    """
    f2_workspace(owned)
    foreign = production_session(principal=FOREIGN_PRINCIPAL)

    probe = pack(
        production_path(owned, session=foreign, principal=FOREIGN_PRINCIPAL).dispatch(
            request_for(build_input())
        )
    )
    exact_budget = probe.budget.tokens_used
    honest_freeze = observed.freeze

    honest = pack(
        production_path(owned, session=foreign, principal=FOREIGN_PRINCIPAL).dispatch(
            request_for(build_input(token_budget=exact_budget))
        )
    )
    honest_manifest = observed.freezes[1].manifest

    widened = pack(
        production_path(owned).dispatch(
            request_for(build_input(token_budget=exact_budget))
        )
    )
    widened_freeze = observed.freezes[2]

    identity = (
        CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
        "evd-restricted",
        m2.DIGEST_A,
    )
    path = "{}/{}@{}".format(*identity)

    # 1. On the widened frontier, absent from the honest one.
    assert identity in frontier_identities(widened_freeze.manifest)
    assert identity not in frontier_identities(honest_manifest)
    assert identity not in frontier_identities(honest_freeze.manifest)

    # 2 and 3. It scored, it competed, and selection dropped it on the budget -- which is
    # the omission code no unscored candidate can carry.
    assert {
        omission.code for omission in widened.omissions if omission.path == path
    } == {OMISSION_TOKEN_BUDGET}
    assert identity not in selected_identities(widened)
    assert selected_identities(widened) == selected_identities(honest)
    assert honest.budget.tokens_used == exact_budget
    assert widened.budget.tokens_used == exact_budget

    # 4. Byte-identical to the caller.
    assert selected_content(widened) == selected_content(honest)

    # 5. And caught anyway, by the manifest held independently of both.
    honest_checksum = (
        honest.reproducibility.authorization_context.authorized_candidate_set_checksum
    )
    widened_checksum = (
        widened.reproducibility.authorization_context.authorized_candidate_set_checksum
    )
    assert honest_checksum != widened_checksum
    with pytest.raises(ContractSemanticError, match="authorized_candidate_set_checksum"):
        judge(
            widened,
            observed.validated[2],
            expected_authorized_candidate_set=honest_manifest,
        )


# --- 4. freshness: refused, not reported --------------------------------------


def test_a_lagging_contributing_projection_is_refused_rather_than_reported(
    standard: m2.Owned
) -> None:
    """New evidence after the build moves the checkpoint, and the answer becomes a refusal.

    `ContextPackBuildResult` *has* a freshness field, so this operation could answer and
    say it was stale. It must not: the pack is content-addressed, so a known-invalid answer
    would acquire an identity anyone could go on citing.

    Falsifier: the same workspace answers successfully before the extra artifact is
    written, so the refusal is the checkpoint moving rather than anything about the
    request.
    """
    answered(production_path(standard).dispatch(request_for(build_input())))

    seed_artifact(standard, "evd-late", locator="archive://alpha.md", at=BASE_US + 900)
    response = refused(production_path(standard).dispatch(request_for(build_input())))

    assert response.error.code == ERROR_CODE_STALE_PROJECTION
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE_AFTER_DELAY
    assert response.error.message == handler_module._MESSAGE_STALE_PROJECTION


def test_a_frontier_too_wide_to_account_for_is_a_caller_facing_size_refusal(
    standard: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder's one caller-facing failure, mapped to the contract's own code.

    `ContextPackFrontierTooLarge` is raised by the freeze when a frontier holds more
    candidates than a pack can account for in full, and it is the one builder failure that
    is a caller's request being too large rather than a producer-side invariant. The
    contract's answer for that is `size_limit_exceeded`, and it is in this operation's
    frozen `allowed_errors`.

    The freeze is made to raise rather than fed 257 real candidates, and that is a stated
    limitation rather than a hidden one: what is under test here is the *mapping*, and the
    ceiling itself -- refused rather than truncated, counted across every partition -- is
    proved over real values in `test_context_pack_determinism.py`. Seeding 257 sealed
    governed lineages to re-prove the ceiling would test that file's property again and
    this one's not at all.

    Falsifier: map it to `internal_non_recoverable` alongside the producer-side failures
    and a caller is told this build is broken when its own request was simply too large.
    """
    from omnivia_core_runtime.storage.context_pack import ContextPackFrontierTooLarge

    def too_large(**_kwargs: Any) -> Any:
        raise ContextPackFrontierTooLarge("the frontier holds more than the ceiling")

    monkeypatch.setattr(handler_module, "freeze_context_pack_frontier", too_large)

    response = refused(production_path(standard).dispatch(request_for(build_input())))
    assert response.error.code == "size_limit_exceeded"
    assert response.error.message == handler_module._MESSAGE_FRONTIER_TOO_LARGE
    assert "ceiling" not in wire_text(response)
    assert "size_limit_exceeded" in ENTRY.allowed_errors


def test_a_workspace_with_no_activated_projection_is_refused(owned: m2.Owned) -> None:
    """Nothing activated is not "nothing to compare, so proceed".

    That direction is the one an implementation drifts on, and it is the silent success
    §20.7 forbids: with no activation there is no freshness a pack could truthfully state.

    Falsifier: build the projection and the same request over the same rows succeeds.
    """
    seed_artifact(owned, "evd-alpha", locator="archive://alpha.md", at=BASE_US + 1)
    seed_record(owned, "rec-alpha", text="alpha statement", at=BASE_US + 2)

    response = refused(production_path(owned).dispatch(request_for(build_input())))
    assert response.error.code == ERROR_CODE_PROJECTION_UNAVAILABLE
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE_AFTER_DELAY
    assert response.error.message == handler_module._MESSAGE_PROJECTION_UNAVAILABLE

    project(owned)
    answered(production_path(owned).dispatch(request_for(build_input())))


# --- 5. D6, D9, E1-E4 against the production pack ------------------------------


def test_d6_moving_generated_at_alone_on_a_production_pack_is_refused(
    standard: m2.Owned, observed: Observed
) -> None:
    """D6. The three instants are one string, and a resealed pack cannot break them apart.

    The mutation is the plausible one: a build that stamped a wall-clock generation time
    beside the instant it resolved at. Resealed, so the refusal is about the instants
    rather than about a stale checksum, and judged with exactly the arguments the handler
    used.

    Falsifier: the unmutated pack passes the identical call -- the handler already ran it.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))
    judge(result, observed.validation)

    moved = reseal(
        replace(
            result,
            reproducibility=replace(
                result.reproducibility, generated_at="2027-01-15T08:00:00.124Z"
            ),
        )
    )
    with pytest.raises(ContractSemanticError, match="generated_at"):
        judge(moved, observed.validation)


def test_d9_a_new_supplied_instant_moves_the_pack_identity_and_stays_valid(
    standard: m2.Owned, monkeypatch: pytest.MonkeyPatch, observed: Observed
) -> None:
    """D9. A legal change of the resolution instant: valid, and a different identity.

    The positive complement of D6 and of D10. Two dispatches over one unchanged workspace
    with the clock pinned to two different instants must both produce packs the contract
    accepts, with all three instants moved *together* and a different `pack_id`.

    Falsifier: a builder that ignored the supplied instant, or that took one of the three
    from a second source, produces either the same identity twice or a pack D6's rule
    refuses.
    """
    monkeypatch.setattr(handler_module.time, "time_ns", lambda: 1_800_000_000_123_456_789)
    first = pack(production_path(standard).dispatch(request_for(build_input())))

    observed.validated.clear()
    monkeypatch.setattr(handler_module.time, "time_ns", lambda: 1_800_000_777_456_000_000)
    second = pack(production_path(standard).dispatch(request_for(build_input())))

    assert first.pack_id != second.pack_id
    for produced in (first, second):
        reproducibility = produced.reproducibility
        assert (
            reproducibility.generated_at
            == reproducibility.canonical_resolution_time
            == reproducibility.freshness.as_of
        )
    assert (
        first.reproducibility.canonical_resolution_time
        != second.reproducibility.canonical_resolution_time
    )
    # Accepted against its own instant, and refused against the other's. The second half is
    # what shows the instant is *bound* rather than merely recorded: only the expected
    # resolution time is swapped, so nothing else about the call can account for the
    # refusal.
    judge(second, observed.validation)
    with pytest.raises(ContractSemanticError, match="canonical_resolution_time"):
        judge(
            second,
            observed.validation,
            canonical_resolution_time=first.reproducibility.canonical_resolution_time,
        )


def test_e1_a_production_section_that_attributes_nothing_is_refused(
    standard: m2.Owned, observed: Observed
) -> None:
    """E1. Total attribution, over the pack the production path actually returned.

    Two mutations, because a section can fail to attribute in two ways: naming no citation
    at all, and naming one that does not resolve. Both are refused.

    Falsifier: the unmutated pack passes, so this is a property of the mutation.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))
    first = result.sections[0]

    unattributed = reseal(
        replace(
            result,
            sections=(replace(first, citation_ids=()), *result.sections[1:]),
        )
    )
    with pytest.raises(ContractSemanticError):
        judge(unattributed, observed.validation)

    dangling = reseal(
        replace(
            result,
            sections=(replace(first, citation_ids=("c-9999",)), *result.sections[1:]),
        )
    )
    with pytest.raises(ContractSemanticError):
        judge(dangling, observed.validation)


def test_e2_a_citation_naming_an_identity_outside_the_frozen_manifest_is_refused(
    standard: m2.Owned, observed: Observed
) -> None:
    """E2. Groundedness: a pack may only cite what its frozen frontier held.

    The evidence artifact carried by the production pack is replaced with one whose id the
    freeze never saw. Everything stays internally consistent -- the citation still
    resolves, the section still attributes, the digest is recomputed -- and the pack is
    still refused, because the manifest held independently does not name it.

    Falsifier: the same mutation judged against a manifest rebuilt from the mutated pack
    would pass, which is precisely why the manifest is the freeze's and never the result's.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))
    artifact = result.evidence[0]
    forged = reseal(
        replace(
            result,
            evidence=(replace(artifact, evidence_id="evd-never-frozen"),),
            citations=tuple(
                replace(
                    citation,
                    evidence_reference=replace(
                        citation.evidence_reference, evidence_id="evd-never-frozen"
                    ),
                )
                if hasattr(citation, "evidence_reference")
                else citation
                for citation in result.citations
            ),
        )
    )

    with pytest.raises(ContractSemanticError):
        judge(forged, observed.validation)


def test_e3_the_retrieval_version_has_a_non_circular_source_at_last(
    standard: m2.Owned, observed: Observed
) -> None:
    """E3's nineteenth field, closed here because only the handler can close it.

    `test_context_pack_determinism.py` states the gap in as many words: the pure slice can
    only show that a fixture literal handed to the builder is stored unchanged, and a
    fixture literal is not a source. The source is this: the handler that performs the
    retrieval publishes the constant, passes it in one place, and the value reaches the
    artifact and the out-of-band validator call.

    Two claims, and the second is what makes the first non-circular. The value is the
    handler module's own published constant rather than anything read from the request or
    the context -- read off the handler's own syntax -- and it is not the determinism
    file's fixture label, which is what a value taken from a caller would still have been.

    Falsifier: make `retrieval_version` a field of the request payload or of
    `OperationContext` and the AST assertion fails while every value-level assertion here
    still passes.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))

    assert result.reproducibility.retrieval_version == CONTEXT_PACK_RETRIEVAL_VERSION
    assert observed.validation["expected_retrieval_version"] == (
        CONTEXT_PACK_RETRIEVAL_VERSION
    )
    assert CONTEXT_PACK_RETRIEVAL_VERSION != "retrieval-1"

    call = next(
        node
        for node in ast.walk(ast.parse(inspect.getsource(handler_module)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ContextPackBuildContext"
    )
    supplied = next(
        keyword.value
        for keyword in call.keywords
        if keyword.arg == "retrieval_version"
    )
    assert isinstance(supplied, ast.Name)
    assert supplied.id == "CONTEXT_PACK_RETRIEVAL_VERSION"


def test_e4_the_frontier_the_selection_and_the_omissions_balance_by_identity(
    standard: m2.Owned, observed: Observed
) -> None:
    """E4. Exact accounting, over a real snapshot rather than over a fixture.

    Frontier minus selected minus accounted is empty, and selected and accounted are
    disjoint. The frontier is read off the manifest the freeze produced, so this is
    accounting against the independently held statement rather than against the pack's own
    view of itself.

    The history member is what makes it non-trivial: it is on the frontier, it is in the
    manifest, it can never be content, and it is accounted for by the superseded code. A
    build that simply dropped history would balance every other way and fail here.

    Falsifier: drop one omission and the frontier stops being covered; account for one
    candidate twice and the two sets stop being disjoint.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))

    frontier = frontier_identities(observed.freeze.manifest)
    selected = selected_identities(result)
    accounted = accounted_identities(result)

    assert selected | accounted == frontier
    assert not (selected & accounted)
    assert len(accounted) == len(result.omissions)

    history = (
        CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
        "rec-corrected",
        "ver-rec-corrected-1",
    )
    assert history in frontier
    assert history in accounted
    assert {
        omission.code
        for omission in result.omissions
        if omission.path == "{}/{}@{}".format(*history)
    } == {OMISSION_SUPERSEDED}
    assert OMISSION_NOT_MATCHED in {omission.code for omission in result.omissions}


# --- 6. the production grant, the registry and the probe seam ------------------


def test_the_production_operation_capability_scope_and_purpose_grant_is_exact() -> None:
    """The widened exact sets, at every place this lane had to touch one.

    Exact rather than membership at all four, because a membership test passes just as
    happily against a build that granted the whole catalogue. The scope set does not move
    at all -- `context_pack.build` requires `memory:read`, which the three searches already
    brought -- and that is asserted rather than assumed, because a lane that added a scope
    here would be widening the local owner's authority beyond what its own operation needs.

    Falsifier: each assertion was true of the five-operation grant and is false now, so a
    lane that added the handler without the grant, or the grant without the purpose, fails
    here.
    """
    session = production_session()
    registry = build_application_registry()

    assert session.operations == PRODUCTION_OPERATIONS
    assert registry.operations == PRODUCTION_OPERATIONS
    assert OPERATION_PURPOSES[CONTEXT_PACK_BUILD_OPERATION] == (
        KNOWLEDGE_RETRIEVAL_PURPOSE
    )
    assert session.purposes == frozenset(
        {"workspace_inspection", "knowledge_retrieval"}
    )
    assert session.scopes == frozenset(
        {"workspace:read", "memory:read", "graph:read"}
    )
    assert tuple(ENTRY.scope.required_scopes) == ("memory:read",)
    assert ENTRY.scope.side_effect == "none"
    assert session.capabilities == (
        CapabilityRef(id="context_pack.build", version="1.0"),
        CapabilityRef(id="evidence.read", version="1.0"),
        CapabilityRef(id="graph.read", version="1.0"),
        CapabilityRef(id="knowledge.read", version="1.0"),
        CapabilityRef(id="memory.read", version="1.0"),
        CapabilityRef(id="workspace.read", version="1.0"),
    )
    assert server_capability_snapshot(registry) == session.capabilities


def test_context_pack_build_is_absent_from_every_probe_seam(owned: m2.Owned) -> None:
    """Absence, asserted four ways, because a presence-only test passes when both hold.

    A handler registered in the application registry *and* in a probe seam would satisfy
    any test that only checks the application side, and that is exactly the binding clause
    this file exists under.

    Falsifier: register the operation on the probe dispatcher and the last assertion turns
    into a successful answer.
    """
    assert CONTEXT_PACK_BUILD_OPERATION not in SERVICE_OPERATIONS
    assert CONTEXT_PACK_BUILD_OPERATION not in build_service_registry().operations

    probe = Dispatcher.for_service_operations(
        Grant(
            principal=LOCAL_PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        Served(owned.connection),
    )
    assert CONTEXT_PACK_BUILD_OPERATION not in probe.grant.operations
    assert refused(probe.dispatch(request_for(build_input()))).error.code == (
        "core.operation_not_implemented"
    )


def test_every_v06_3_application_operation_is_absent_from_the_probe_grant() -> None:
    """The clause for the whole stage, not only for this lane's operation.

    Falsifier: this is the assertion that would fail first if a successor lane took the
    shortcut of registering an application operation on the probe seam because it was
    already wired.
    """
    for operation in PRODUCTION_OPERATIONS:
        assert operation not in SERVICE_OPERATIONS
        assert operation not in build_service_registry().operations


# --- 7. no refusal surface carries a caller value ------------------------------


def test_no_refusal_surface_carries_a_caller_value(standard: m2.Owned) -> None:
    """Every refusal this handler can produce, asked with a caller value planted in it.

    Four surfaces, and the sentinel is planted in the payload each time -- in the query,
    and where the payload admits one, in the two selector fields. What is scanned is the
    whole encoded response rather than the message alone, because the requirement is that
    the refusal discloses nothing, not that one field does.

    Falsifier: format any refusal message with the value it rejected -- the single most
    natural thing to write -- and every one of these fails.
    """
    dispatcher = production_path(standard)

    # An unreadable payload. The decode error quotes what it rejected, so this is also the
    # test that the containment convention is being followed.
    invalid = refused(
        dispatcher.dispatch(
            request_for({"query": SENTINEL_TEXT, "mode": SENTINEL_CODE})
        )
    )
    assert invalid.error.code == ERROR_CODE_INVALID_REQUEST
    assert invalid.error.message == handler_module._MESSAGE_INVALID_INPUT
    assert SENTINEL_TEXT not in wire_text(invalid)
    assert SENTINEL_CODE not in wire_text(invalid)

    # A valid payload carrying the sentinel in all three fields it can reach, refused for
    # a reason that has nothing to do with the payload.
    payload = build_input(
        query=SENTINEL_TEXT, domain_scope=SENTINEL_CODE, record_type=SENTINEL_CODE
    )
    seed_artifact(standard, "evd-late", locator="archive://alpha.md", at=BASE_US + 900)
    stale = refused(dispatcher.dispatch(request_for(payload)))
    assert stale.error.code == ERROR_CODE_STALE_PROJECTION
    assert SENTINEL_TEXT not in wire_text(stale)
    assert SENTINEL_CODE not in wire_text(stale)

    # No storage attached at all.
    unserved = refused(
        production_path(standard, service=object()).dispatch(request_for(payload))
    )
    assert unserved.error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert unserved.error.message == handler_module._MESSAGE_NO_STORAGE
    assert SENTINEL_TEXT not in wire_text(unserved)

    # And the amendment's own refusal, which is only reachable directly.
    with pytest.raises(OperationError) as missing:
        context_pack_build(
            OperationContext(
                request_for(payload),
                LOCAL_PRINCIPAL,
                WORKSPACE_ID,
                PRODUCTION_OPERATIONS,
                Forbidden(),
            )
        )
    assert SENTINEL_TEXT not in missing.value.message
    assert SENTINEL_CODE not in missing.value.message
    assert missing.value.__context__ is None


def test_the_version_and_policy_attestations_are_this_builds_own(
    standard: m2.Owned
) -> None:
    """Every version field in a production pack, tied to the constant that owns it.

    Six of them name algorithms implemented in `storage/context_pack.py` and are that
    module's own published constants; the summarizer and the model versions are the
    disabled and empty spellings; the retrieval version is the handler's. None of them is
    a literal written in this file, because a literal here would agree with a build that
    had changed its ranking and kept its label.

    Falsifier: hand the builder a caller-chosen label for any of the six and it refuses --
    which `test_context_pack_determinism.py` proves -- so what this adds is that the
    production handler supplies exactly those constants and nothing else.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))
    reproducibility = result.reproducibility

    assert reproducibility.builder_version == CONTEXT_PACK_BUILDER_VERSION
    assert reproducibility.ranking_version == CONTEXT_PACK_RANKING_VERSION
    assert reproducibility.reranking_version == CONTEXT_PACK_RERANKER_DISABLED
    assert reproducibility.selection_version == CONTEXT_PACK_SELECTION_VERSION
    assert reproducibility.tokenizer_id == CONTEXT_PACK_TOKENIZER_ID
    assert reproducibility.tokenizer_version == CONTEXT_PACK_TOKENIZER_VERSION
    assert reproducibility.summarizer_version == CONTEXT_PACK_SUMMARIZER_DISABLED
    assert dict(reproducibility.model_versions) == {}
    assert (
        reproducibility.normalized_request.normalization_version
        == CONTEXT_PACK_NORMALIZATION_VERSION
    )
    assert reproducibility.normalized_request.normalized_query == QUERY
    assert reproducibility.retrieval_version == CONTEXT_PACK_RETRIEVAL_VERSION
    assert dict(
        result.reproducibility.authorization_context.policy_versions
    ) == dict(CONTEXT_PACK_POLICY_VERSIONS)


def test_the_request_selectors_narrow_both_governed_partitions(
    standard: m2.Owned, observed: Observed
) -> None:
    """`record_type` and `domain_scope` are frontier filters, not result filters.

    They are the last two members of the attested chain, so a candidate they exclude is
    absent from the manifest and from the digest rather than merely absent from the page.
    Both partitions are narrowed, `history` included: history is reproducibility input, and
    a history member the request never asked about would put an identity in the manifest
    this request has no business vouching for.

    Falsifier: apply them after the freeze and the two frontiers below become identical
    while the packs stay the same, which is the F2 shape one level down.
    """
    unfiltered = production_path(standard).dispatch(request_for(build_input()))
    answered(unfiltered)
    wide = frontier_identities(observed.freezes[0].manifest)

    answered(
        production_path(standard).dispatch(
            request_for(build_input(domain_scope="product.other"))
        )
    )
    narrow = frontier_identities(observed.freezes[1].manifest)

    governed = {
        identity
        for identity in wide
        if identity[0] != CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE
    }
    assert governed
    assert not {
        identity
        for identity in narrow
        if identity[0] != CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE
    }
    assert narrow < wide


def test_a_second_dispatch_reads_a_snapshot_that_moved(
    standard: m2.Owned, observed: Observed
) -> None:
    """The read is a snapshot of the workspace, not of a cache the handler kept.

    A governed record written between two dispatches must reach the second frontier and
    not the first. That is a weak-looking claim and it is the one that fails if a handler
    memoises its snapshot on the connection, which is the obvious optimisation to reach for
    once six reads are involved.

    Falsifier: memoise, and the second frontier is the first.
    """
    answered(production_path(standard).dispatch(request_for(build_input())))
    before = frontier_identities(observed.freezes[0].manifest)

    seed_record(standard, "rec-later", text="alpha later", at=BASE_US + 7)
    project(standard)
    answered(production_path(standard).dispatch(request_for(build_input())))
    after = frontier_identities(observed.freezes[1].manifest)

    added = (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-later", "ver-rec-later")
    assert added not in before
    assert added in after
    assert before < after


def test_the_sections_and_the_selected_partitions_are_one_reading(
    standard: m2.Owned
) -> None:
    """Every selected item is sectioned and cited, over the production pack.

    The contract's own validator already enforces coverage in both directions, so what
    this adds is the count: one section and one citation per selected item, which is this
    build's own selection shape rather than the contract's minimum.

    Falsifier: emit two sections for one candidate and the counts diverge while the
    validator still passes.
    """
    result = pack(production_path(standard).dispatch(request_for(build_input())))
    selected = len(result.evidence) + len(result.records)

    assert selected == len(result.sections) == len(result.citations)
    assert [section.section_id for section in result.sections] == [
        f"s-{ordinal:04d}" for ordinal in range(1, selected + 1)
    ]
    assert [citation.citation_id for citation in result.citations] == [
        f"c-{ordinal:04d}" for ordinal in range(1, selected + 1)
    ]
    assert result.budget.tokens_used == sum(
        section.token_count for section in result.sections
    )


@pytest.mark.parametrize("adapter", ("in-process", "local-ipc", "http"))
def test_v06_5_c1_context_pack_primary_reaches_every_real_adapter(
    standard: m2.Owned, adapter: str
) -> None:
    """The deterministic Context Pack success branch crosses every adapter."""
    response = s2._transport_call(
        adapter,
        production_path(standard),
        request_for(
            build_input(), request_id=f"req-context-pack-c1-{adapter}-primary"
        ),
        case_id="context_pack.build/primary-success",
    )

    result = pack(response)
    assert result.mode == "deterministic_view"
    assert result.fresh_authorization_required is True


@pytest.mark.parametrize("adapter", ("in-process", "local-ipc", "http"))
def test_v06_5_c1_context_pack_error_family_reaches_every_real_adapter(
    owned: m2.Owned, adapter: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four frozen Context Pack errors run through the production handler."""
    unavailable_response = refused(
        s2._transport_call(
            adapter,
            production_path(owned),
            request_for(
                build_input(),
                request_id=f"req-context-pack-c1-{adapter}-projection",
            ),
            case_id="error/projection_unavailable",
        )
    )
    assert unavailable_response.error.code == ERROR_CODE_PROJECTION_UNAVAILABLE

    standard_workspace(owned)
    dispatcher = production_path(owned)

    token_response = refused(
        s2._transport_call(
            adapter,
            dispatcher,
            request_for(
                build_input(token_budget=1),
                request_id=f"req-context-pack-c1-{adapter}-token",
            ),
            case_id="error/token_limit_exceeded",
        )
    )
    assert token_response.error.code == ERROR_CODE_TOKEN_LIMIT_EXCEEDED

    seed_artifact(
        owned, "evd-c1-stale", locator="archive://alpha.md", at=BASE_US + 900
    )
    stale_response = refused(
        s2._transport_call(
            adapter,
            dispatcher,
            request_for(
                build_input(), request_id=f"req-context-pack-c1-{adapter}-stale"
            ),
            case_id="error/stale_projection",
        )
    )
    assert stale_response.error.code == ERROR_CODE_STALE_PROJECTION
    project(owned)

    def too_large(**_kwargs: Any) -> Any:
        raise handler_module.ContextPackFrontierTooLarge(
            "c1 deterministic ceiling"
        )

    monkeypatch.setattr(handler_module, "freeze_context_pack_frontier", too_large)
    size_response = refused(
        s2._transport_call(
            adapter,
            dispatcher,
            request_for(
                build_input(), request_id=f"req-context-pack-c1-{adapter}-size"
            ),
            case_id="error/size_limit_exceeded",
        )
    )
    assert size_response.error.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
