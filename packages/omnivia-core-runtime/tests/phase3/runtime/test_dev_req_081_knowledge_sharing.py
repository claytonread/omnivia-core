"""DEV-REQ-081: explicit, permission-aware cross-Project knowledge sharing that invalidates on change.

Every behaviour below is driven through the production application surface
(`ProductionApplicationSurface.dispatch_for_session`) composed by `service.main`, over a real migrated
workspace. A sealed, canonical governed version is shared from the Project that owns its domain scope to
one recipient Project only through an owner's proposal and a different owner's acceptance, and a
recipient reads it only while the share stays accepted, unrevoked, and current.

Project authority is a server binding, not a request field. The principal is the session's, so these
tests vary the session's principal and never a payload member, and they show that a payload naming a
Project, a principal or a role is refused outright rather than honoured or ignored.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.application import (
    KNOWLEDGE_SHARING_FAMILY_PURPOSES,
    ProductionApplicationSurface,
    build_installation_application_dispatcher,
    build_task_context_application_dispatcher,
    compose_production_application_surface,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession, Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.knowledge_sharing import (
    OPERATION_DECIDE,
    OPERATION_LINEAGE,
    OPERATION_PROPOSE,
    OPERATION_READ,
    ProjectAuthority,
    ProjectBinding,
    is_share_eligible,
)
from omnivia_core_runtime.service.main import _build_production_application_surface
from omnivia_core_runtime.service.mutation import MUTATION_PURPOSES, MUTATION_ROLES
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage.knowledge_shares import read_share

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import OPERATION_CATALOGUE

WS = m3.WORKSPACE_ID
PRINCIPAL = "local-user"
RECORD = "record-1"
SHARE = "share-1"
CONTENT = {"statement": "A durable claim"}

SHARING_OPERATIONS = (OPERATION_PROPOSE, OPERATION_DECIDE, OPERATION_READ, OPERATION_LINEAGE)

PROPOSER = "owner-proposer"
APPROVER = "owner-approver"
RECIPIENT_READER = "recipient-reader"
RECIPIENT_OWNER = "recipient-owner"
OTHER_OWNER = "other-owner"
OTHER_READER = "other-reader"
BROAD = "workspace-admin"

#: The server's Project binding. `product.core` is the scope the seeded record was created in.
AUTHORITY = ProjectAuthority.of(
    [
        ProjectBinding("project-source", "product.core", frozenset({PROPOSER, APPROVER})),
        ProjectBinding(
            "project-recipient",
            "product.recipient",
            frozenset({RECIPIENT_OWNER}),
            frozenset({RECIPIENT_READER}),
        ),
        ProjectBinding(
            "project-other", "product.other", frozenset({OTHER_OWNER}), frozenset({OTHER_READER})
        ),
    ]
)
SOURCE = "project-source"
RECIPIENT = "project-recipient"

#: The first sixty-nine entries of the catalogue as they stood before the sharing family was added,
#: as canonical JSON of each entry's wire form. Appending operations must not change them.
PRIOR_CATALOGUE_DIGEST = "sha256:2fcf6d30e7f5d3b02e1630a8b8371f1701bf26e5e2ffc66b4ed127cad031b1de"


class _InstallationService:
    """Construction-only shape; its bound production handlers are never invoked."""

    authority = SimpleNamespace(installation_id=s0.INSTALLATION_ID)


def _surface(holder: Any, authority: ProjectAuthority | None) -> ProductionApplicationSurface:
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WS}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        holder,
    )
    started = SimpleNamespace(**vars(holder), workspace_id=WS, clock=SystemClock())
    installation = build_installation_application_dispatcher(
        service=_InstallationService(),  # type: ignore[arg-type]
        principal_id=PRINCIPAL,
        fallback=probe,
    )
    extra = {} if authority is None else {"project_authority": authority}
    return _build_production_application_surface(
        started=started,  # type: ignore[arg-type]
        probe=probe,
        installation=installation,
        **extra,
    )


class Harness:
    """An owned workspace behind the production surface, called as one principal at a time."""

    def __init__(self, holder: m1.Owned, authority: ProjectAuthority | None = AUTHORITY) -> None:
        self.holder = holder
        self.surface = _surface(holder, authority)
        self._requests = 0

    def recompose(self, authority: ProjectAuthority | None) -> Harness:
        """The same workspace under a different server binding, as a restart with new bindings."""
        return Harness(self.holder, authority)

    def session(self, principal: str, *operations: str) -> AuthenticatedSession:
        """A server-shaped session for `principal`; only the principal and the grant differ."""
        base = self.surface.session_for(OPERATION_PROPOSE)
        assert base is not None
        return dataclasses.replace(
            base,
            principal_id=principal,
            operations=frozenset(operations or SHARING_OPERATIONS),
        )

    def call(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        principal: str,
        key: str | None = None,
        session: AuthenticatedSession | None = None,
        **metadata: Any,
    ) -> Any:
        self._requests += 1
        request_id = f"req-share-{self._requests}"
        entry = get_operation_metadata(operation)
        overrides: dict[str, Any] = {
            "request_id": request_id,
            "correlation_id": f"cor-{request_id}",
            "trace_id": f"trc-{request_id}",
            "purpose": KNOWLEDGE_SHARING_FAMILY_PURPOSES[operation],
            "workspace_id": WS,
        }
        if entry.idempotency.supports_idempotency_key:
            overrides["idempotency_key"] = key or f"idem-{request_id}"
        overrides.update(metadata)
        envelope = s0.envelope_for(entry, operation_input=payload, **overrides)
        return self.surface.dispatch_for_session(
            envelope, session or self.session(principal, operation)
        )

    def ok(self, operation: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = self.call(operation, payload, **kwargs)
        assert isinstance(response, SuccessResponseEnvelope), response
        return dict(response.to_wire()["result"])

    def code(self, operation: str, payload: dict[str, Any], **kwargs: Any) -> str:
        response = self.call(operation, payload, **kwargs)
        assert isinstance(response, ErrorResponseEnvelope), response
        return str(response.error.code)

    # -- the four operations -----------------------------------------------------------

    def propose(self, *, principal: str = PROPOSER, share_id: str = SHARE, **extra: Any) -> Any:
        payload = {"share_id": share_id, "record_id": RECORD, "recipient_project_id": RECIPIENT}
        payload.update(extra.pop("payload", {}))
        return self.call(OPERATION_PROPOSE, payload, principal=principal, **extra)

    def decide(
        self, decision: str, *, principal: str = APPROVER, share_id: str = SHARE, **extra: Any
    ) -> Any:
        payload = {"share_id": share_id, "decision": decision}
        payload.update(extra.pop("payload", {}))
        return self.call(OPERATION_DECIDE, payload, principal=principal, **extra)

    def read(self, *, principal: str = RECIPIENT_READER, share_id: str = SHARE, **extra: Any) -> Any:
        payload = {"share_id": share_id}
        payload.update(extra.pop("payload", {}))
        return self.call(OPERATION_READ, payload, principal=principal, **extra)

    def lineage(self, *, principal: str = PROPOSER, share_id: str = SHARE, **extra: Any) -> Any:
        payload = {"share_id": share_id}
        payload.update(extra.pop("payload", {}))
        return self.call(OPERATION_LINEAGE, payload, principal=principal, **extra)

    def accepted(self) -> None:
        """Propose by one owner and accept by the other: the whole route to an eligible share."""
        assert isinstance(self.propose(), SuccessResponseEnvelope)
        assert isinstance(self.decide("accepted"), SuccessResponseEnvelope)

    def count(self, table: str) -> int:
        return int(self.holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def code_of(response: Any) -> str:
    assert isinstance(response, ErrorResponseEnvelope), response
    return str(response.error.code)


def result_of(response: Any) -> dict[str, Any]:
    assert isinstance(response, SuccessResponseEnvelope), response
    return dict(response.to_wire()["result"])


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    m2.seed_chain(holder)
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WS,
        fencing_generation=holder.generation,
    ):
        for number in range(1, 9):
            m3.insert(
                holder.connection,
                "omnivia_application_audit_events",
                m3.audit_row(f"audit-{number}"),
            )
    m3.seed_accepted_version(holder)
    yield holder
    holder.connection.close()


@pytest.fixture
def harness(owned: m1.Owned) -> Harness:
    return Harness(owned)


# -- the family in the exact production registry ----------------------------------------


def test_the_four_operations_sit_at_their_frozen_positions_and_are_distinct() -> None:
    names = [entry.name for entry in OPERATION_CATALOGUE]
    # The task-context family was appended after this one, so the sharing family is no longer last.
    assert tuple(names[69:73]) == (
        "knowledge.share.propose",
        "knowledge.share.decide",
        "knowledge.share.read",
        "knowledge.share.lineage",
    )
    assert SHARING_OPERATIONS == tuple(names[69:73])
    assert len(names) == len(set(names)) == 77


def test_appending_the_family_changed_no_earlier_operation_contract() -> None:
    prior = [entry.to_wire() for entry in OPERATION_CATALOGUE[:69]]
    digest = hashlib.sha256(
        json.dumps(prior, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert len(prior) == 69
    assert f"sha256:{digest}" == PRIOR_CATALOGUE_DIGEST


def test_the_production_registry_holds_the_four_operations_under_one_family(
    harness: Harness,
) -> None:
    surface = harness.surface
    surface.registry.assert_complete()
    assert len(surface.registry.operations) == 77
    assert set(SHARING_OPERATIONS) <= surface.registry.operations
    families = {id(surface._routes[name]) for name in SHARING_OPERATIONS}
    assert len(families) == 1
    for name in SHARING_OPERATIONS:
        handler = surface.registry.get(name)
        assert handler is not None
        assert handler.__module__ == "omnivia_core_runtime.service.handlers.knowledge_sharing"
        assert surface.session_for(name) is not None
    others = surface.registry.operations - set(SHARING_OPERATIONS)
    assert not any(
        id(surface._routes[name]) in families for name in others
    ), "no other operation may route through the sharing family"


def test_the_sharing_family_cannot_be_registered_twice_or_under_another_name(
    harness: Harness,
) -> None:
    routes = dict(harness.surface._routes)
    sharing = routes[OPERATION_PROPOSE]
    kwargs: dict[str, Any] = {
        "installation": routes["workspace.create"],
        "reads": routes["workspace.inspect"],
        "memory": routes["memory.create"],
        "jobs": routes["job.get"],
        "governance": routes["candidate.approve"],
        "chat": routes["chat.command"],
        "workflow": routes["workflow.start"],
        "trigger": routes["trigger.declare"],
        "decision": routes["decision.evaluate"],
        "skill": routes["skills.install"],
        "skill_resolution": routes["skills.resolve"],
        "engineering": routes["engineering.search"],
        "knowledge_sharing": sharing,
        "probe": harness.surface.probe,
    }
    started = SimpleNamespace(**vars(harness.holder), workspace_id=WS, clock=SystemClock())
    kwargs["task_context"] = build_task_context_application_dispatcher(
        service=started,
        principal_id=PRINCIPAL,
        installation_id=s0.INSTALLATION_ID,
        workspace_id=WS,
        fallback=sharing,
        clock=started.clock,
    )
    compose_production_application_surface(**kwargs).registry.assert_complete()
    with pytest.raises(ValueError, match="already registered"):
        compose_production_application_surface(**{**kwargs, "governance": sharing})


def test_an_unregistered_neighbouring_name_fails_closed(harness: Harness) -> None:
    for name in ("knowledge.share.revoke", "knowledge.share", "knowledge.share.read "):
        assert name not in harness.surface.registry.operations
        assert harness.surface.session_for(name) is None


def test_mutation_purpose_and_role_are_declared_for_exactly_the_two_writes() -> None:
    assert MUTATION_PURPOSES[OPERATION_PROPOSE] == MUTATION_PURPOSES[OPERATION_DECIDE]
    assert MUTATION_PURPOSES[OPERATION_PROPOSE] == "knowledge_sharing"
    assert MUTATION_ROLES[OPERATION_PROPOSE] == "workspace_contributor"
    assert OPERATION_READ not in MUTATION_PURPOSES and OPERATION_LINEAGE not in MUTATION_PURPOSES


# -- the route to an eligible share ------------------------------------------------------


def test_proposal_alone_grants_the_recipient_nothing(harness: Harness) -> None:
    proposed = result_of(harness.propose())
    assert proposed["state"] == "proposed"
    assert proposed["source_project_id"] == SOURCE
    assert proposed["recipient_project_id"] == RECIPIENT
    assert code_of(harness.read()) == "conflict"


def test_an_accepted_share_serves_the_sealed_version_to_a_recipient_member(
    harness: Harness,
) -> None:
    proposed = result_of(harness.propose())
    decided = result_of(harness.decide("accepted"))
    assert decided["state"] == "accepted" and decided["decision"] == "accepted"

    served = harness.ok(OPERATION_READ, {"share_id": SHARE}, principal=RECIPIENT_READER)

    assert served["content"] == CONTENT
    assert served["content_digest"] == proposed["content_digest"]
    assert served["governed_record_version_id"] == proposed["governed_record_version_id"]
    assert served["source_project_id"] == SOURCE
    assert served["recipient_project_id"] == RECIPIENT
    assert served["domain_scope"] == "product.core"


def test_the_proposer_cannot_accept_its_own_share(harness: Harness) -> None:
    harness.propose()
    assert code_of(harness.decide("accepted", principal=PROPOSER)) == "authorization_denied"
    assert code_of(harness.read()) == "conflict"
    assert harness.count("omnivia_knowledge_share_decisions") == 0


def test_a_revocation_needs_an_earlier_acceptance(harness: Harness) -> None:
    harness.propose()
    assert code_of(harness.decide("revoked")) == "conflict"


# -- Project authority comes from the server ---------------------------------------------


def test_the_source_project_is_derived_from_the_records_scope_not_the_caller(
    harness: Harness,
) -> None:
    # The record lives in `product.core`, owned by project-source; owners of other Projects are
    # refused however they name themselves, and nothing was written.
    for stranger in (OTHER_OWNER, RECIPIENT_OWNER, RECIPIENT_READER, BROAD):
        assert code_of(harness.propose(principal=stranger)) == "authorization_denied", stranger
    assert harness.count("omnivia_knowledge_shares") == 0


def test_a_record_whose_scope_no_bound_project_owns_cannot_be_shared(owned: m1.Owned) -> None:
    unbound = ProjectAuthority.of(
        [ProjectBinding("project-elsewhere", "product.elsewhere", frozenset({PROPOSER}))]
    )
    harness = Harness(owned, unbound)
    assert code_of(harness.propose()) == "authorization_denied"


def test_a_payload_cannot_name_a_source_recipient_principal_or_role(harness: Harness) -> None:
    harness.accepted()
    spoofs: dict[str, Any] = {
        "source_project_id": SOURCE,
        "project_id": RECIPIENT,
        "recipient_project_id": RECIPIENT,
        "principal_id": RECIPIENT_READER,
        "decided_by": APPROVER,
        "roles": ["knowledge_reviewer"],
    }
    for key, value in spoofs.items():
        extra = {"payload": {key: value}}
        responses = {
            "read": harness.read(principal=RECIPIENT_READER, **extra),
            "lineage": harness.lineage(principal=PROPOSER, **extra),
            "decide": harness.decide("revoked", principal=APPROVER, **extra),
        }
        for name, response in responses.items():
            assert code_of(response) == "invalid_request", (name, key)
    for key in ("source_project_id", "project_id", "principal_id", "roles"):
        response = harness.propose(share_id="share-spoof", payload={key: SOURCE})
        assert code_of(response) == "invalid_request", key
    assert harness.count("omnivia_knowledge_shares") == 1
    assert harness.count("omnivia_knowledge_share_decisions") == 1


def test_the_recipient_must_be_a_bound_project_other_than_the_source(harness: Harness) -> None:
    response = harness.propose(payload={"recipient_project_id": "project-unbound"})
    assert code_of(response) == "not_found"
    assert code_of(harness.propose(payload={"recipient_project_id": SOURCE})) == "invalid_request"
    assert harness.count("omnivia_knowledge_shares") == 0


def test_only_a_member_of_the_recipient_project_reads(harness: Harness) -> None:
    harness.accepted()
    # A member of another Project, an owner of the recipient who is not a member, the source's own
    # owners, an unbound principal and a broad workspace grant all see the share as absent.
    for principal in (OTHER_READER, RECIPIENT_OWNER, PROPOSER, APPROVER, BROAD):
        assert code_of(harness.read(principal=principal)) == "not_found", principal
    assert result_of(harness.read())["content"] == CONTENT


def test_decide_and_lineage_belong_to_source_owners_only(harness: Harness) -> None:
    harness.propose()
    for principal in (RECIPIENT_READER, RECIPIENT_OWNER, OTHER_OWNER, BROAD):
        assert code_of(harness.decide("accepted", principal=principal)) == "not_found", principal
        assert code_of(harness.lineage(principal=principal)) == "not_found", principal
    assert harness.count("omnivia_knowledge_share_decisions") == 0


def test_a_broad_workspace_grant_confers_no_project_authority(harness: Harness) -> None:
    harness.accepted()
    broad = harness.session(BROAD)  # every sharing operation, every scope, the contributor role
    assert set(broad.operations) == set(SHARING_OPERATIONS)
    for operation, payload in (
        (OPERATION_PROPOSE, {"share_id": "share-b", "record_id": RECORD, "recipient_project_id": RECIPIENT}),
        (OPERATION_DECIDE, {"share_id": SHARE, "decision": "revoked"}),
        (OPERATION_READ, {"share_id": SHARE}),
        (OPERATION_LINEAGE, {"share_id": SHARE}),
    ):
        response = harness.call(operation, payload, principal=BROAD, session=broad)
        assert code_of(response) in {"authorization_denied", "not_found"}, operation
    # The local owner the service itself acts as is not a bound Project principal either.
    assert code_of(harness.read(principal=PRINCIPAL)) == "not_found"
    assert harness.count("omnivia_knowledge_shares") == 1
    assert harness.count("omnivia_knowledge_share_decisions") == 1


def test_a_session_without_the_operation_scope_or_purpose_is_refused_before_the_handler(
    harness: Harness,
) -> None:
    harness.accepted()
    only_propose = harness.session(RECIPIENT_READER, OPERATION_PROPOSE)
    assert code_of(harness.read(principal=RECIPIENT_READER, session=only_propose)) in {
        "authorization_denied",
        "capability_not_granted",
        "workspace_not_granted",
    }
    no_scope = dataclasses.replace(harness.session(RECIPIENT_READER, OPERATION_READ), scopes=frozenset())
    assert code_of(harness.read(session=no_scope)) in {"authorization_denied", "capability_not_granted"}
    assert code_of(harness.read(purpose="knowledge_retrieval")) == "invalid_purpose"


def test_the_default_composition_binds_no_project_and_refuses_everything(owned: m1.Owned) -> None:
    unbound = Harness(owned, None)
    assert code_of(unbound.propose()) == "authorization_denied"
    assert code_of(unbound.read()) == "not_found"
    assert code_of(unbound.lineage()) == "not_found"
    assert code_of(unbound.decide("accepted")) == "not_found"


def test_the_binding_refuses_ambiguous_projects() -> None:
    with pytest.raises(ValueError):
        ProjectAuthority.of(
            [
                ProjectBinding("p1", "product.core", frozenset({"a"})),
                ProjectBinding("p2", "product.core", frozenset({"b"})),
            ]
        )
    with pytest.raises(ValueError):
        ProjectAuthority.of(
            [
                ProjectBinding("p1", "product.a", frozenset({"a"})),
                ProjectBinding("p1", "product.b", frozenset({"b"})),
            ]
        )
    with pytest.raises(ValueError):
        ProjectBinding("p1", "product.core", frozenset())


# -- revalidation on every read -----------------------------------------------------------


def test_revocation_invalidates_every_later_read_and_the_eligibility_seam_but_keeps_lineage(
    harness: Harness,
) -> None:
    harness.accepted()
    assert result_of(harness.read())["content"] == CONTENT
    assert is_share_eligible(
        harness.holder.connection, AUTHORITY, principal=RECIPIENT_READER, workspace_id=WS, share_id=SHARE
    )

    revoked = result_of(harness.decide("revoked", principal=PROPOSER))
    assert revoked["state"] == "revoked"

    for _ in range(2):
        assert code_of(harness.read()) == "conflict"
    assert not is_share_eligible(
        harness.holder.connection, AUTHORITY, principal=RECIPIENT_READER, workspace_id=WS, share_id=SHARE
    )
    assert code_of(harness.decide("accepted", principal=APPROVER)) == "conflict"
    lineage = harness.ok(OPERATION_LINEAGE, {"share_id": SHARE}, principal=APPROVER)
    assert lineage["state"] == "revoked"
    assert [d["decision"] for d in lineage["decisions"]] == ["accepted", "revoked"]
    assert lineage["proposed_by"] == PROPOSER
    assert [d["decided_by"] for d in lineage["decisions"]] == [APPROVER, PROPOSER]


def test_a_superseded_source_stales_the_share_for_every_use_but_not_its_lineage(
    harness: Harness,
) -> None:
    harness.accepted()
    assert result_of(harness.read())["content"] == CONTENT

    m3.seed_corrected_version(harness.holder, bootstrap=False)

    assert code_of(harness.read()) == "conflict"
    assert not is_share_eligible(
        harness.holder.connection, AUTHORITY, principal=RECIPIENT_READER, workspace_id=WS, share_id=SHARE
    )
    # A new proposal binds to the version that is now canonical, never to the one it replaced.
    fresh = result_of(harness.propose(share_id="share-2"))
    assert fresh["governed_record_version_id"] == "version-corrected"
    assert code_of(harness.read()) == "conflict", "the old share does not follow the record"
    lineage = harness.ok(OPERATION_LINEAGE, {"share_id": SHARE}, principal=PROPOSER)
    assert lineage["state"] == "accepted"
    # An owner can still withdraw it, so the stale share can never become readable again.
    assert result_of(harness.decide("revoked"))["state"] == "revoked"


def test_a_share_cannot_be_accepted_after_its_version_is_superseded(harness: Harness) -> None:
    harness.propose()
    m3.seed_corrected_version(harness.holder, bootstrap=False)
    assert code_of(harness.decide("accepted")) == "conflict"
    assert harness.count("omnivia_knowledge_share_decisions") == 0


def test_the_recipients_binding_is_checked_on_every_read_not_remembered(
    harness: Harness,
) -> None:
    harness.accepted()
    assert result_of(harness.read())["content"] == CONTENT

    after = harness.recompose(
        ProjectAuthority.of(
            [
                ProjectBinding("project-source", "product.core", frozenset({PROPOSER, APPROVER})),
                ProjectBinding(
                    "project-recipient", "product.recipient", frozenset({RECIPIENT_OWNER})
                ),
            ]
        )
    )
    assert code_of(after.read()) == "not_found"
    assert result_of(harness.read())["content"] == CONTENT, "the earlier composition still binds him"


def test_a_source_projects_scope_binding_is_checked_on_every_read(harness: Harness) -> None:
    harness.accepted()
    rescoped = harness.recompose(
        ProjectAuthority.of(
            [
                ProjectBinding("project-source", "product.moved", frozenset({PROPOSER, APPROVER})),
                ProjectBinding(
                    "project-recipient", "product.recipient", frozenset({RECIPIENT_OWNER}), frozenset({RECIPIENT_READER})
                ),
            ]
        )
    )
    assert code_of(rescoped.read()) == "conflict"


# -- idempotency, replay and conflict -----------------------------------------------------


def test_an_honest_replay_returns_the_stored_result_and_writes_nothing_more(
    harness: Harness,
) -> None:
    first = result_of(harness.propose(key="idem-propose"))
    audits = harness.count("omnivia_application_audit_events")
    replay = result_of(harness.propose(key="idem-propose"))
    assert replay == first
    assert harness.count("omnivia_knowledge_shares") == 1
    assert harness.count("omnivia_application_audit_events") == audits

    accepted = result_of(harness.decide("accepted", key="idem-accept"))
    assert result_of(harness.decide("accepted", key="idem-accept")) == accepted
    assert harness.count("omnivia_knowledge_share_decisions") == 1


def test_the_same_key_for_a_different_request_is_an_idempotency_conflict(harness: Harness) -> None:
    harness.propose(key="idem-one")
    other = harness.propose(
        key="idem-one", share_id="share-2", payload={"recipient_project_id": "project-other"}
    )
    assert code_of(other) == "idempotency_conflict"
    assert harness.count("omnivia_knowledge_shares") == 1


def test_a_different_proposal_under_an_existing_share_id_is_a_conflict(harness: Harness) -> None:
    first = result_of(harness.propose(key="idem-a"))
    again = result_of(harness.propose(key="idem-b"))  # the same body under a fresh key
    assert again == first
    different = harness.propose(key="idem-c", payload={"recipient_project_id": "project-other"})
    assert code_of(different) == "conflict"
    assert harness.count("omnivia_knowledge_shares") == 1


def test_a_second_acceptance_by_another_owner_is_a_conflict(harness: Harness) -> None:
    harness.accepted()
    assert code_of(harness.decide("accepted", principal=PROPOSER)) == "conflict"
    assert harness.count("omnivia_knowledge_share_decisions") == 1


def test_a_replay_is_refused_once_the_owners_binding_is_withdrawn(harness: Harness) -> None:
    harness.propose(key="idem-propose")
    withdrawn = harness.recompose(
        ProjectAuthority.of(
            [
                ProjectBinding("project-source", "product.core", frozenset({APPROVER})),
                ProjectBinding(
                    "project-recipient", "product.recipient", frozenset({RECIPIENT_OWNER}), frozenset({RECIPIENT_READER})
                ),
            ]
        )
    )
    assert code_of(withdrawn.propose(key="idem-propose")) == "not_found"
    assert result_of(harness.propose(key="idem-propose"))["state"] == "proposed"


def test_an_unknown_record_or_share_is_not_found(harness: Harness) -> None:
    assert code_of(harness.propose(payload={"record_id": "record-missing"})) == "not_found"
    assert code_of(harness.read(share_id="share-missing")) == "not_found"
    assert code_of(harness.lineage(share_id="share-missing")) == "not_found"
    assert code_of(harness.decide("accepted", share_id="share-missing")) == "not_found"


def test_a_candidate_version_is_not_authoritative_and_cannot_be_shared(harness: Harness) -> None:
    seeded = harness.holder.connection.execute(
        "SELECT governed_record_id FROM omnivia_governed_records WHERE governed_record_id <> ?",
        (RECORD,),
    ).fetchone()
    assert seeded is None
    assert code_of(harness.propose(payload={"record_id": "record-candidate"})) == "not_found"


# -- tampering, fencing and append-only storage -------------------------------------------


def _fenced(holder: m1.Owned, sql: str, parameters: tuple[Any, ...] = ()) -> None:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WS,
        fencing_generation=holder.generation,
    ) as fenced:
        fenced.execute(sql, parameters)


def test_a_share_row_that_fails_its_digest_is_a_fault_never_a_served_or_ineligible_share(
    harness: Harness,
) -> None:
    harness.accepted()
    _fenced(
        harness.holder,
        "INSERT INTO omnivia_knowledge_shares "
        "(workspace_id, share_id, share_digest, source_project_id, recipient_project_id, "
        "governed_record_id, governed_assembly_id, governed_record_version_id, domain_scope, "
        "content_digest, proposed_by, proposed_under_generation, proposed_at_us) "
        "SELECT workspace_id, 'share-tampered', 'sha256:' || ?, source_project_id, "
        "recipient_project_id, governed_record_id, governed_assembly_id, "
        "governed_record_version_id, domain_scope, content_digest, 'someone-else', "
        "proposed_under_generation, proposed_at_us "
        "FROM omnivia_knowledge_shares WHERE share_id = ?",
        ("a" * 64, SHARE),
    )
    from omnivia_core_runtime.storage.knowledge_shares import KnowledgeShareInvalid

    with pytest.raises(KnowledgeShareInvalid):
        read_share(harness.holder.connection, workspace_id=WS, share_id="share-tampered")
    assert code_of(harness.read(share_id="share-tampered")) == "internal_non_recoverable"
    assert code_of(harness.lineage(share_id="share-tampered")) == "internal_non_recoverable"
    assert code_of(harness.decide("accepted", share_id="share-tampered")) == "internal_non_recoverable"
    assert result_of(harness.read())["content"] == CONTENT, "the honest share is unaffected"


def test_a_stale_fencing_generation_refuses_every_write(harness: Harness) -> None:
    holder = harness.holder
    with pytest.raises(StaleGeneration), fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WS,
        fencing_generation=holder.generation + 1,
    ):
        pass
    assert harness.count("omnivia_knowledge_shares") == 0


def test_writes_outside_the_fence_are_refused(harness: Harness) -> None:
    harness.propose()
    with pytest.raises(sqlite3.DatabaseError):
        harness.holder.connection.execute(
            "INSERT INTO omnivia_knowledge_share_decisions "
            "(workspace_id, share_id, decision, decided_by, decided_under_generation, decided_at_us) "
            "VALUES (?, ?, 'accepted', ?, ?, ?)",
            (WS, SHARE, APPROVER, harness.holder.generation, m3.BASE_US + 1),
        )
    assert harness.count("omnivia_knowledge_share_decisions") == 0


def test_shares_and_decisions_are_append_only_even_inside_the_fence(harness: Harness) -> None:
    harness.accepted()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _fenced(harness.holder, "UPDATE omnivia_knowledge_shares SET proposed_by = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _fenced(harness.holder, "DELETE FROM omnivia_knowledge_share_decisions")
    assert harness.count("omnivia_knowledge_shares") == 1
    assert harness.count("omnivia_knowledge_share_decisions") == 1


def test_a_decision_cannot_be_committed_out_of_order_even_by_a_caller_that_skips_the_service(
    harness: Harness,
) -> None:
    harness.propose()
    insert = (
        "INSERT INTO omnivia_knowledge_share_decisions "
        "(workspace_id, share_id, decision, decided_by, decided_under_generation, decided_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    row = (WS, SHARE, "revoked", APPROVER, harness.holder.generation, m3.BASE_US + 2)
    with pytest.raises(sqlite3.IntegrityError, match="revoked only after it is accepted"):
        _fenced(harness.holder, insert, row)
