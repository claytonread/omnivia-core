"""C17 acceptance for the managed Skills operations through the real application dispatcher.

Every request here goes through `ApplicationDispatcher.dispatch` -- the grant, the role and
purpose checks, the mutation seam, the idempotency ledger and the domain writer -- against a
migrated workspace. Nothing calls a handler directly, and nothing builds a grant by hand.

Covered: each of the seven mutations and `skills.resolve`; role separation (authorship, publication
and installation are separate authorities); idempotent replay; stale-revision and closed-draft
conflicts; immutable publication; deprecation, install and removal; deterministic dependency
resolution; and workspace isolation. The `workflow.start` tests at the end prove that role closures
are resolved and bound as generation 1 in the same admitted transaction, and that an unsatisfied
selection refuses without admitting a Run.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_c17_managed_skills_storage as storage
import test_t0693_workflow_application as wf
import test_v06_5_s0_mutation_foundation as s0
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    SKILL_FAMILY_PURPOSES,
    SKILL_RESOLUTION_PURPOSE,
    ApplicationDispatcher,
    build_skill_application_dispatcher,
    build_skill_resolution_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.mutation import (
    SKILL_PUBLISHER_ROLE,
    WORKSPACE_CONTRIBUTOR_ROLE,
    WORKSPACE_OPERATOR_ROLE,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage import managed_skills as store
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    decode_request,
    encode_request,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.semantics_skills import skill_manifest_id

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = m1.OTHER_WORKSPACE_ID
INSTALLATION_ID = s0.INSTALLATION_ID
PRINCIPAL = CONFIGURED_LOCAL_OWNER
WORKFLOW_ID = m27.WORKFLOW_ID
WORKFLOW_VERSION = m27.WORKFLOW_VERSION
ROLE = "reviewer"

EVIDENCE = {"evidence_id": "evidence-1", "content_digest": "sha256:" + "a" * 64}

ALL_ROLES = frozenset(
    {WORKSPACE_CONTRIBUTOR_ROLE, SKILL_PUBLISHER_ROLE, WORKSPACE_OPERATOR_ROLE}
)
AUTHOR_ONLY = frozenset({WORKSPACE_CONTRIBUTOR_ROLE})
PUBLISHER_ONLY = frozenset({SKILL_PUBLISHER_ROLE})
OPERATOR_ONLY = frozenset({WORKSPACE_OPERATOR_ROLE})

#: The tables a skill write can touch. A refused or replayed call must leave every one unchanged.
SKILL_TABLES = (
    "omnivia_skill_drafts",
    "omnivia_skill_draft_revisions",
    "omnivia_skill_proposals",
    "omnivia_skill_versions",
    "omnivia_skill_deprecations",
    "omnivia_skill_install_events",
)

_ids = itertools.count(1)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def allocator(tag: str) -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{tag}-{counts[prefix]}"

    return allocate


def fallback() -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )


def served(
    holder: m1.Owned,
    *,
    roles: frozenset[str] = ALL_ROLES,
    workspace_id: str = WORKSPACE_ID,
    tag: str = "skl",
) -> ApplicationDispatcher:
    """The real skill family, over one owned workspace, holding exactly `roles`."""
    return build_skill_application_dispatcher(
        service=holder,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=workspace_id,
        fallback=fallback(),
        clock=FakeClock(wall=wf.WALL),
        allocate_identifier=allocator(tag),
        roles=roles,
    )


def send(
    dispatcher: ApplicationDispatcher,
    operation: str,
    payload: Mapping[str, object],
    *,
    key: str | None = None,
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    """One request through the dispatcher. Mutations carry an idempotency key; reads do not."""
    purpose = SKILL_FAMILY_PURPOSES.get(operation, SKILL_RESOLUTION_PURPOSE)
    ordinal = next(_ids)
    envelope = s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=payload,
        request_id=f"req-{ordinal}",
        correlation_id=f"cor-{ordinal}",
        trace_id=f"trc-{ordinal}",
        idempotency_key=key,
        purpose=purpose,
        workspace_id=workspace_id,
    )
    return dispatcher.dispatch(envelope)


def result(response: ResponseEnvelope) -> Mapping[str, Any]:
    assert isinstance(response, SuccessResponseEnvelope), _error(response)
    return response.result


def code(response: ResponseEnvelope) -> str:
    assert isinstance(response, ErrorResponseEnvelope), (
        "expected a refusal, got a success"
    )
    return response.error.code


def _error(response: ResponseEnvelope) -> str:
    return (
        f"{response.error.code}: {response.error.message}"
        if isinstance(response, ErrorResponseEnvelope)
        else "success"
    )


def ledger(holder: m1.Owned) -> tuple[int, ...]:
    """Row counts of every skill table: equal before and after means nothing was written."""
    return tuple(
        int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in SKILL_TABLES
    )


def manifest(
    name: str = "triage", version: str = "1.0.0", **overrides: Any
) -> dict[str, Any]:
    body = storage.manifest(name, version)
    body.update(overrides)
    return body


# --- the seven mutations and the read, in one chain --------------------------------------


def create(
    dispatcher: ApplicationDispatcher, body: Mapping[str, Any], *, key: str
) -> ResponseEnvelope:
    return send(dispatcher, "skills.draft.create", {"manifest": dict(body)}, key=key)


def update(
    dispatcher: ApplicationDispatcher,
    draft_id: str,
    expected: int,
    body: Mapping[str, Any],
    *,
    key: str,
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    return send(
        dispatcher,
        "skills.draft.update",
        {"draft_id": draft_id, "expected_revision": expected, "manifest": dict(body)},
        key=key,
        workspace_id=workspace_id,
    )


def propose(
    dispatcher: ApplicationDispatcher,
    draft_id: str,
    expected: int,
    *,
    key: str,
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    return send(
        dispatcher,
        "skills.proposal.submit",
        {
            "draft_id": draft_id,
            "expected_revision": expected,
            "evidence_refs": [EVIDENCE],
        },
        key=key,
        workspace_id=workspace_id,
    )


def publish(
    dispatcher: ApplicationDispatcher, proposal_id: str, *, key: str
) -> ResponseEnvelope:
    return send(
        dispatcher,
        "skills.version.publish",
        {"proposal_id": proposal_id, "review_evidence_refs": [EVIDENCE]},
        key=key,
    )


def deprecate(
    dispatcher: ApplicationDispatcher,
    manifest_id: str,
    *,
    key: str,
    reason: str = "superseded",
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    return send(
        dispatcher,
        "skills.version.deprecate",
        {"manifest_id": manifest_id, "reason": reason},
        key=key,
        workspace_id=workspace_id,
    )


def install(
    dispatcher: ApplicationDispatcher,
    manifest_id: str,
    *,
    key: str,
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    return send(
        dispatcher,
        "skills.install",
        {"manifest_id": manifest_id},
        key=key,
        workspace_id=workspace_id,
    )


def remove(
    dispatcher: ApplicationDispatcher, manifest_id: str, *, key: str
) -> ResponseEnvelope:
    return send(dispatcher, "skills.remove", {"manifest_id": manifest_id}, key=key)


def resolution(
    holder: m1.Owned, *, workspace_id: str = WORKSPACE_ID
) -> ApplicationDispatcher:
    """The restricted read family. `skills.resolve` is served here, not by the mutation family."""
    return build_skill_resolution_application_dispatcher(
        service=holder,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=workspace_id,
        fallback=fallback(),
        clock=FakeClock(wall=wf.WALL),
    )


def resolve(
    holder: m1.Owned,
    selections: list[dict[str, Any]],
    *,
    role_id: str = ROLE,
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    return send(
        resolution(holder, workspace_id=workspace_id),
        "skills.resolve",
        {"role_id": role_id, "selections": selections},
        workspace_id=workspace_id,
    )


def draft_and_propose(
    dispatcher: ApplicationDispatcher, body: Mapping[str, Any], *, tag: str
) -> tuple[str, str]:
    """Open a draft at revision 1 and submit it. Returns `(draft_id, proposal_id)`."""
    draft = result(create(dispatcher, body, key=f"{tag}-create"))
    proposal = result(propose(dispatcher, draft["draft_id"], 1, key=f"{tag}-propose"))
    return str(draft["draft_id"]), str(proposal["proposal_id"])


def publish_skill(
    dispatcher: ApplicationDispatcher, body: Mapping[str, Any], *, tag: str
) -> str:
    """Draft, submit and publish one manifest. Returns its `manifest_id`."""
    _draft_id, proposal_id = draft_and_propose(dispatcher, body, tag=tag)
    published = result(publish(dispatcher, proposal_id, key=f"{tag}-publish"))
    return str(published["manifest_id"])


def selection(name: str, manifest_id: str | None = None) -> dict[str, Any]:
    return (
        {"skill_name": name}
        if manifest_id is None
        else {"skill_name": name, "manifest_id": manifest_id}
    )


# --- the round trip ---------------------------------------------------------------------


def test_a_skill_is_drafted_proposed_published_installed_and_resolved_through_the_dispatcher(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    body = manifest()

    draft = result(create(dispatcher, body, key="k-create"))
    assert draft["draft_revision"] == 1
    assert draft["version"] == "1.0.0"
    assert draft["manifest_id"] == skill_manifest_id(body)

    updated = result(
        update(
            dispatcher,
            draft["draft_id"],
            1,
            manifest(description="triage, revised"),
            key="k-update",
        )
    )
    assert updated["draft_revision"] == 2

    proposal = result(propose(dispatcher, draft["draft_id"], 2, key="k-propose"))
    published = result(publish(dispatcher, proposal["proposal_id"], key="k-publish"))
    assert published["draft_revision"] == 2
    assert published["version"] == "1.0.0"

    installed = result(install(dispatcher, published["manifest_id"], key="k-install"))
    assert (installed["install_state"], installed["event_sequence"]) == ("installed", 1)

    resolved = result(resolve(owned, [selection("triage")]))
    assert resolved["role_id"] == ROLE
    (entry,) = resolved["entries"]
    assert (entry["manifest_id"], entry["skill_name"], entry["version"]) == (
        published["manifest_id"],
        "triage",
        "1.0.0",
    )


# --- role separation -----------------------------------------------------------------


def test_authorship_grants_neither_publication_nor_installation(
    owned: m1.Owned,
) -> None:
    author = served(owned, roles=AUTHOR_ONLY, tag="author")
    _draft_id, proposal_id = draft_and_propose(author, manifest(), tag="author")
    before = ledger(owned)

    assert code(publish(author, proposal_id, key="k-publish")) == "authorization_denied"
    manifest_id = skill_manifest_id(manifest())
    assert code(install(author, manifest_id, key="k-install")) == "authorization_denied"
    assert code(remove(author, manifest_id, key="k-remove")) == "authorization_denied"
    assert (
        code(deprecate(author, manifest_id, key="k-deprecate"))
        == "authorization_denied"
    )
    assert ledger(owned) == before


def test_publication_and_installation_are_separate_authorities(owned: m1.Owned) -> None:
    operator = served(owned, roles=OPERATOR_ONLY, tag="operator")
    publisher = served(owned, roles=PUBLISHER_ONLY, tag="publisher")
    author = served(owned, roles=AUTHOR_ONLY, tag="author")
    _draft_id, proposal_id = draft_and_propose(author, manifest(), tag="sep")

    # An operator cannot publish, and a publisher cannot install.
    assert (
        code(publish(operator, proposal_id, key="k-op-publish"))
        == "authorization_denied"
    )
    published = result(publish(publisher, proposal_id, key="k-publish"))
    assert code(install(publisher, published["manifest_id"], key="k-pub-install")) == (
        "authorization_denied"
    )
    assert result(install(operator, published["manifest_id"], key="k-op-install"))[
        "install_state"
    ] == ("installed")


def test_a_role_held_once_is_not_enough_for_a_different_mutation(
    owned: m1.Owned,
) -> None:
    """Holding the publisher role does not let a publisher author a draft."""
    publisher = served(owned, roles=PUBLISHER_ONLY, tag="publisher-only")
    before = ledger(owned)

    assert code(create(publisher, manifest(), key="k-create")) == "authorization_denied"
    assert ledger(owned) == before


# --- idempotent replay ----------------------------------------------------------------


def test_an_honest_replay_returns_the_stored_answer_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    first = create(dispatcher, manifest(), key="k-create")
    before = ledger(owned)

    again = create(dispatcher, manifest(), key="k-create")

    assert result(again) == result(first)
    assert again.metadata.audit_reference == first.metadata.audit_reference
    assert ledger(owned) == before


def test_an_altered_replay_under_the_same_key_is_an_idempotency_conflict(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    create(dispatcher, manifest(), key="k-create")
    before = ledger(owned)

    refused = create(
        dispatcher, manifest(description="a different request"), key="k-create"
    )

    assert code(refused) == "idempotency_conflict"
    assert ledger(owned) == before


def test_a_replayed_publication_returns_the_first_answer_and_mints_no_second_version(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    _draft_id, proposal_id = draft_and_propose(dispatcher, manifest(), tag="replay")
    first = publish(dispatcher, proposal_id, key="k-publish")
    before = ledger(owned)

    again = publish(dispatcher, proposal_id, key="k-publish")

    assert result(again) == result(first)
    assert ledger(owned) == before


# --- stale revisions and closed drafts -------------------------------------------------


def test_an_update_against_a_stale_revision_is_a_conflict_and_leaves_the_draft(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    draft = result(create(dispatcher, manifest(), key="k-create"))
    result(
        update(
            dispatcher, draft["draft_id"], 1, manifest(description="first"), key="k-u1"
        )
    )
    before = ledger(owned)

    stale = update(
        dispatcher, draft["draft_id"], 1, manifest(description="second"), key="k-u2"
    )

    assert code(stale) == "conflict"
    assert ledger(owned) == before


def test_a_proposal_against_a_stale_revision_is_a_conflict(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    draft = result(create(dispatcher, manifest(), key="k-create"))
    result(
        update(
            dispatcher, draft["draft_id"], 1, manifest(description="newer"), key="k-u1"
        )
    )
    before = ledger(owned)

    assert (
        code(propose(dispatcher, draft["draft_id"], 1, key="k-propose")) == "conflict"
    )
    assert ledger(owned) == before


def test_a_submitted_draft_is_closed_to_revision_and_to_a_second_submission(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    draft = result(create(dispatcher, manifest(), key="k-create"))
    result(propose(dispatcher, draft["draft_id"], 1, key="k-propose"))
    before = ledger(owned)

    assert code(
        update(
            dispatcher, draft["draft_id"], 1, manifest(description="late"), key="k-u"
        )
    ) == ("conflict")
    assert (
        code(propose(dispatcher, draft["draft_id"], 1, key="k-propose-again"))
        == "conflict"
    )
    assert ledger(owned) == before


def test_a_draft_keeps_the_skill_name_it_was_created_with(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    draft = result(create(dispatcher, manifest(), key="k-create"))
    before = ledger(owned)

    refused = update(
        dispatcher, draft["draft_id"], 1, manifest("other-name"), key="k-rename"
    )

    assert code(refused) == "invalid_request"
    assert ledger(owned) == before


# --- immutable publication ------------------------------------------------------------


def test_a_proposal_publishes_once(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    _draft_id, proposal_id = draft_and_propose(dispatcher, manifest(), tag="once")
    result(publish(dispatcher, proposal_id, key="k-publish"))
    before = ledger(owned)

    assert code(publish(dispatcher, proposal_id, key="k-publish-other")) == "conflict"
    assert ledger(owned) == before


def test_changed_content_under_a_published_version_is_refused_and_the_version_stands(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    published_id = publish_skill(dispatcher, manifest(), tag="first")
    # Same skill and version, different instructions: a second version must be a new version.
    _draft_id, proposal_id = draft_and_propose(
        dispatcher, manifest(instructions="Review differently."), tag="second"
    )
    before = ledger(owned)

    assert code(publish(dispatcher, proposal_id, key="k-publish-changed")) == "conflict"
    assert ledger(owned) == before
    assert (
        result(install(dispatcher, published_id, key="k-install"))["manifest_id"]
        == published_id
    )


# --- deprecation, install and removal -------------------------------------------------


def test_a_deprecated_version_cannot_be_installed_and_deprecation_happens_once(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    manifest_id = publish_skill(dispatcher, manifest(), tag="dep")
    deprecated = result(deprecate(dispatcher, manifest_id, key="k-deprecate"))
    assert deprecated["reason"] == "superseded"
    before = ledger(owned)

    assert (
        code(deprecate(dispatcher, manifest_id, key="k-deprecate-again")) == "conflict"
    )
    assert code(install(dispatcher, manifest_id, key="k-install")) == "conflict"
    assert ledger(owned) == before


def test_installing_twice_records_one_event_and_removal_records_one_more(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    manifest_id = publish_skill(dispatcher, manifest(), tag="inst")

    first = result(install(dispatcher, manifest_id, key="k-install-1"))
    before = ledger(owned)
    second = result(install(dispatcher, manifest_id, key="k-install-2"))
    assert (second["install_state"], second["event_sequence"]) == (
        "installed",
        first["event_sequence"],
    )
    assert ledger(owned) == before

    removed = result(remove(dispatcher, manifest_id, key="k-remove-1"))
    assert (removed["install_state"], removed["event_sequence"]) == ("removed", 2)
    before_removed = ledger(owned)
    assert (
        result(remove(dispatcher, manifest_id, key="k-remove-2"))["event_sequence"] == 2
    )
    assert ledger(owned) == before_removed


def test_removing_a_version_never_installed_is_a_conflict(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    manifest_id = publish_skill(dispatcher, manifest(), tag="never")
    before = ledger(owned)

    assert code(remove(dispatcher, manifest_id, key="k-remove")) == "conflict"
    assert ledger(owned) == before


def test_a_removed_version_is_no_longer_selected_but_still_installable(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    manifest_id = publish_skill(dispatcher, manifest(), tag="cycle")
    result(install(dispatcher, manifest_id, key="k-install"))
    assert result(resolve(owned, [selection("triage")]))["entries"]

    result(remove(dispatcher, manifest_id, key="k-remove"))
    assert code(resolve(owned, [selection("triage")])) == "conflict"

    reinstalled = result(install(dispatcher, manifest_id, key="k-reinstall"))
    assert (reinstalled["install_state"], reinstalled["event_sequence"]) == (
        "installed",
        3,
    )
    assert result(resolve(owned, [selection("triage")]))["entries"]


# --- resolution -----------------------------------------------------------------------


def test_resolution_is_deterministic_and_puts_dependencies_first(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    base_id = publish_skill(dispatcher, manifest("base", "1.0.0"), tag="base")
    top_body = manifest(
        "triage", "1.0.0", dependencies=[{"skill_name": "base", "manifest_id": base_id}]
    )
    top_id = publish_skill(dispatcher, top_body, tag="top")
    result(install(dispatcher, base_id, key="k-install-base"))
    result(install(dispatcher, top_id, key="k-install-top"))

    first = result(resolve(owned, [selection("triage")]))
    second = result(resolve(owned, [selection("triage")]))

    assert first == second
    assert [entry["skill_name"] for entry in first["entries"]] == ["base", "triage"]


def test_the_highest_compatible_installed_version_wins_and_an_explicit_reference_beats_it(
    owned: m1.Owned,
) -> None:
    dispatcher = served(owned)
    older = publish_skill(dispatcher, manifest("triage", "1.0.0"), tag="v1")
    newer = publish_skill(dispatcher, manifest("triage", "1.1.0"), tag="v11")
    result(install(dispatcher, older, key="k-install-v1"))
    result(install(dispatcher, newer, key="k-install-v11"))

    (chosen,) = result(resolve(owned, [selection("triage")]))["entries"]
    assert chosen["manifest_id"] == newer

    (pinned,) = result(resolve(owned, [selection("triage", older)]))["entries"]
    assert pinned["manifest_id"] == older


def test_an_unknown_skill_or_an_uninstalled_version_is_refused(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    publish_skill(dispatcher, manifest(), tag="uninstalled")

    assert code(resolve(owned, [selection("nowhere")])) == "conflict"
    assert code(resolve(owned, [selection("triage")])) == "conflict"


def test_resolution_is_a_read_and_writes_nothing(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    manifest_id = publish_skill(dispatcher, manifest(), tag="read")
    result(install(dispatcher, manifest_id, key="k-install"))
    before = ledger(owned)

    result(resolve(owned, [selection("triage")]))

    assert ledger(owned) == before


def test_an_inert_manifest_that_states_a_permission_is_refused(owned: m1.Owned) -> None:
    dispatcher = served(owned)
    before = ledger(owned)

    refused = create(
        dispatcher, manifest(permissions=["repo.write"]), key="k-permission"
    )

    assert code(refused) == "invalid_request"
    assert ledger(owned) == before


def test_a_request_decoded_from_the_wire_is_served_like_an_in_process_one(
    owned: m1.Owned,
) -> None:
    """The wire delivers `input` read-only. A dict-only check would refuse this honest call."""
    dispatcher = served(owned)
    envelope = s0.envelope_for(
        get_operation_metadata("skills.draft.create"),
        operation_input={"manifest": manifest()},
        request_id="req-wire",
        correlation_id="cor-wire",
        trace_id="trc-wire",
        idempotency_key="k-wire",
        purpose=SKILL_FAMILY_PURPOSES["skills.draft.create"],
        workspace_id=WORKSPACE_ID,
    )
    wire = decode_request(encode_request(envelope))

    assert isinstance(dispatcher.dispatch(wire), SuccessResponseEnvelope)


# --- workspace isolation --------------------------------------------------------------


def test_another_workspace_resolves_and_installs_nothing_it_does_not_hold(
    owned: m1.Owned,
) -> None:
    home = served(owned)
    manifest_id = publish_skill(home, manifest(), tag="home")
    result(install(home, manifest_id, key="k-install"))
    before = ledger(owned)

    # A read naming the other workspace reaches its own rows, where the skill does not exist.
    assert (
        code(resolve(owned, [selection("triage")], workspace_id=OTHER_WORKSPACE_ID))
        == "conflict"
    )
    # A mutation naming a workspace this service instance does not hold is refused by the seam.
    foreign = served(owned, workspace_id=OTHER_WORKSPACE_ID, tag="foreign")
    assert (
        code(
            install(
                foreign,
                manifest_id,
                key="k-foreign-install",
                workspace_id=OTHER_WORKSPACE_ID,
            )
        )
        == "authorization_denied"
    )
    assert (
        code(
            deprecate(
                foreign,
                manifest_id,
                key="k-foreign-deprecate",
                workspace_id=OTHER_WORKSPACE_ID,
            )
        )
        == "authorization_denied"
    )
    assert ledger(owned) == before


def test_another_workspace_cannot_revise_or_propose_a_draft_it_does_not_hold(
    owned: m1.Owned,
) -> None:
    home = served(owned)
    draft = result(create(home, manifest(), key="k-create"))
    before = ledger(owned)
    foreign = served(owned, workspace_id=OTHER_WORKSPACE_ID, tag="foreign-draft")

    # A session granted only the other workspace is refused a request that names this one.
    assert code(
        update(foreign, draft["draft_id"], 1, manifest(description="x"), key="k-grant")
    ) == ("workspace_not_granted")
    # Naming the other workspace is refused by the seam, which holds only this one.
    assert (
        code(
            update(
                foreign,
                draft["draft_id"],
                1,
                manifest(description="x"),
                key="k-foreign-u",
                workspace_id=OTHER_WORKSPACE_ID,
            )
        )
        == "authorization_denied"
    )
    assert (
        code(
            propose(
                foreign,
                draft["draft_id"],
                1,
                key="k-foreign-p",
                workspace_id=OTHER_WORKSPACE_ID,
            )
        )
        == "authorization_denied"
    )
    # And at the storage layer the draft is simply not a draft of the other workspace.
    assert (
        store.read_draft_head(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            draft_id=draft["draft_id"],
        )
        is None
    )
    assert ledger(owned) == before


# --- workflow.start: role closures bound in the admitted transaction --------------------


def start_with_skills(
    holder: m1.Owned, selections: list[dict[str, Any]], *, key: str = "idem-skills-run"
) -> ResponseEnvelope:
    payload: dict[str, object] = {
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "skill_selections": [{"role_id": ROLE, "selections": selections}],
    }
    workflow = wf.dispatcher(holder, releases=(wf.release(),), tag="skw")
    ordinal = next(_ids)
    return workflow.dispatch(
        wf.request(
            "workflow.start",
            payload,
            request_id=f"req-start-{ordinal}",
            idempotency_key=key,
        )
    )


def install_dependent_pair(holder: m1.Owned) -> tuple[str, str]:
    """A published `base`, and `triage` pinned to it, both installed. Returns `(base, triage)`."""
    dispatcher = served(holder, tag="pair")
    base_id = publish_skill(dispatcher, manifest("base", "1.0.0"), tag="pair-base")
    triage_id = publish_skill(
        dispatcher,
        manifest(
            "triage",
            "1.0.0",
            dependencies=[{"skill_name": "base", "manifest_id": base_id}],
        ),
        tag="pair-triage",
    )
    result(install(dispatcher, base_id, key="k-pair-base"))
    result(install(dispatcher, triage_id, key="k-pair-triage"))
    return base_id, triage_id


def test_a_start_binds_the_resolved_role_closure_as_generation_one(
    owned: m1.Owned,
) -> None:
    base_id, triage_id = install_dependent_pair(owned)

    started = start_with_skills(owned, [selection("triage")])

    run_id = wf.run_id_of(started)
    assert wf.count(owned, m27.RUNS) == 1
    assert store.read_run_skill_binding_generations(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    ) == (1,)
    bound = store.read_run_skill_bindings(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )
    assert bound is not None
    assert bound.binding_generation == 1
    assert bound.amendment_id is None
    # Dependencies first, the way the closure was resolved; the role is the one the Run named.
    assert [(b.role_id, b.skill_name, b.manifest_id) for b in bound.bindings] == [
        (ROLE, "base", base_id),
        (ROLE, "triage", triage_id),
    ]
    # The binding is written under the admission's own audit event, not a later one.
    assert {b.audit_ref for b in bound.bindings} == {started.metadata.audit_reference}


def test_an_unsatisfied_selection_refuses_the_start_and_admits_no_run(
    owned: m1.Owned,
) -> None:
    publish_skill(served(owned, tag="unsat"), manifest(), tag="unsat-published")
    before = (wf.count(owned, m27.RUNS), wf.count(owned, m27.PLANS), ledger(owned))

    # Published but never installed, so nothing satisfies the selection.
    refused = start_with_skills(owned, [selection("triage")])

    assert isinstance(refused, ErrorResponseEnvelope)
    assert refused.error.code == "conflict"
    assert (
        wf.count(owned, m27.RUNS),
        wf.count(owned, m27.PLANS),
        ledger(owned),
    ) == before
    # No binding or seal row exists for any run: the refusal wrote none.
    assert wf.count(owned, "omnivia_skill_run_bindings") == 0
    assert wf.count(owned, "omnivia_skill_run_binding_seals") == 0


def test_a_selection_naming_no_skill_at_all_refuses_and_admits_no_run(
    owned: m1.Owned,
) -> None:
    before = wf.count(owned, m27.RUNS)

    refused = start_with_skills(owned, [selection("never-published")])

    assert isinstance(refused, ErrorResponseEnvelope)
    assert refused.error.code == "conflict"
    assert wf.count(owned, m27.RUNS) == before
