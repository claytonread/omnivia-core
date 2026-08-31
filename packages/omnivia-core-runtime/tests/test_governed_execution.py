"""Conformance oracles for the T-0688 IP-11 governed execution seam.

Clean-room throughout: every value below is authored here, and the seam is a pure in-memory
oracle, so every assertion is made against its own returned values, refusals and Evidence.
Nothing here reads a database, a scheduler, a transport or another project's source.

What is asserted:

- a settlement grants nothing by possession -- Permission and eligibility are rechecked on
  every submission including a replay, a stale request version and an expired request both
  refuse, an identical replay is a no-op returning the accepted result, a conflicting second
  answer refuses with ``HUMAN_INTERACTION_OUTCOME_CONFLICT`` and leaves Evidence, and exactly
  one accepted result ever yields a Wait continuation;
- all four interaction kinds preserve their declared owner independently of the responder;
- autonomy resolution intersects all four scope dimensions, takes the minimum of all four
  numeric bounds, unions the approval requirements, supplies an explicit bounded default for
  every absent source kind, freezes seven ordered contributions and a resolution digest, and
  refuses an empty intersection, an exceeded bound and -- always -- a Permission denial;
- plane ownership issues strictly increasing never-reused fencing tokens across all five
  plane roles, leaves Evidence on takeover, refuses a stale dispatch and a stale write with
  ``EXECUTION_PLANE_STALE_AUTHORITY``, records a late completion as
  ``EXECUTION_PLANE_LATE_RESULT`` Evidence without applying it, deduplicates listener
  deliveries and sheds at bounded capacity with ``LISTENER_SATURATED``, and requires a
  current lease and a fresh observation to reconcile;
- and every one of those answers is identical for LOCAL and DISTRIBUTED inputs.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from omnivia_core_runtime.execution import (
    AUTONOMY_PROFILE_REFUSED,
    AUTONOMY_SOURCE_ORDER,
    BOUND_DIMENSIONS,
    DEFAULT_MAXIMUM_COST_UNITS,
    DEFAULT_MAXIMUM_STEPS,
    DEFAULT_MAXIMUM_TOKENS,
    DEFAULT_MAXIMUM_WALL_CLOCK_MS,
    DELIVERY_DELIVERED,
    DELIVERY_DUPLICATE,
    DEPLOYMENT_DISTRIBUTED,
    DEPLOYMENT_LOCAL,
    DEPLOYMENT_MODES,
    EFFECT_EXTERNAL_EFFECT,
    EFFECT_INTERNAL_WRITE,
    EXECUTION_PLANE_LATE_RESULT,
    EXECUTION_PLANE_OWNERSHIP_TAKEOVER,
    EXECUTION_PLANE_STALE_AUTHORITY,
    HUMAN_INTERACTION_OUTCOME_CONFLICT,
    INTERACTION_APPROVAL,
    INTERACTION_KINDS,
    INTERACTION_REVIEW,
    LISTENER_SATURATED,
    OUTCOME_SUCCEEDED,
    PLANE_CAPABILITY_GATEWAY,
    PLANE_LISTENER,
    PLANE_RESOURCE_SUPERVISOR,
    PLANE_ROLES,
    PLANE_SCHEDULER,
    PLANE_WORKER,
    SOURCE_POLICY,
    SOURCE_WORKFLOW_SETTINGS,
    AutonomySource,
    CompletionOutcome,
    DeliveryDecision,
    DispatchRecord,
    Evidence,
    ExecutionContractError,
    ExecutionRefused,
    HumanInteractionSettlement,
    InteractionRequest,
    LeaseGrant,
    OutcomeSubmission,
    PlaneOwnership,
    ReconciliationDecision,
    RequestedSpend,
    default_autonomy_source,
    resolve_autonomy,
)

_DIGEST_A = "sha256:" + "1" * 64
_DIGEST_B = "sha256:" + "2" * 64
_PAYLOAD = "sha256:" + "3" * 64


def _request(**overrides: object) -> InteractionRequest:
    fields: dict[str, object] = {
        "request_id": "req.harbour.1",
        "request_version": 4,
        "idempotency_key": "idempotency.harbour.1",
        "interaction_kind": INTERACTION_APPROVAL,
        "owner": "task.harbour.moorings",
        "wait_ref": "wait.harbour.1",
        "expires_at_tick": 100,
    }
    fields.update(overrides)
    return InteractionRequest(**fields)  # type: ignore[arg-type]


def _submission(**overrides: object) -> OutcomeSubmission:
    fields: dict[str, object] = {
        "request_id": "req.harbour.1",
        "request_version": 4,
        "idempotency_key": "idempotency.harbour.1",
        "actor": "actor.harbour.pell",
        "device_session": "device.harbour.pell",
        "response_digest": _DIGEST_A,
        "at_tick": 20,
    }
    fields.update(overrides)
    return OutcomeSubmission(**fields)  # type: ignore[arg-type]


def _source(kind: str, **overrides: object) -> AutonomySource:
    fields: dict[str, object] = {
        "source_kind": kind,
        "source_ref": f"src.{kind.lower()}",
        "snapshot_ref": f"snap.{kind.lower()}",
        "snapshot_digest": _DIGEST_A,
    }
    fields.update(overrides)
    return AutonomySource(**fields)  # type: ignore[arg-type]


def _wide_source(kind: str, **overrides: object) -> AutonomySource:
    """A source that narrows every dimension to a known superset."""
    fields: dict[str, object] = {
        "action_scope": ("draft", "publish", "retire"),
        "resource_scope": ("index", "store", "cache"),
        "model_scope": ("slate", "tallow", "birch"),
        "knowledge_scope": ("corpus.a", "corpus.b"),
    }
    fields.update(overrides)
    return _source(kind, **fields)


# --- Human Interaction settlement -----------------------------------------------------


def test_a_first_submission_is_accepted_and_yields_one_continuation() -> None:
    settlement = HumanInteractionSettlement(_request())
    result = settlement.submit(_submission(), permission_granted=True, eligible=True)

    assert result.replayed is False
    assert result.continuation_issued is True
    assert result.continuation_token is not None
    assert result.bundle_input_digest is not None
    assert settlement.settled == result
    assert settlement.evidence == ()


def test_possession_grants_nothing_permission_is_rechecked_every_submission() -> None:
    settlement = HumanInteractionSettlement(_request())
    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(_submission(), permission_granted=False, eligible=True)
    assert settlement.settled is None

    settlement.submit(_submission(), permission_granted=True, eligible=True)
    # Settled once; Permission is still rechecked, so the replay of the very same
    # submission refuses once the responder's Permission is gone.
    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(_submission(), permission_granted=False, eligible=True)


def test_eligibility_is_rechecked_every_submission_including_the_replay() -> None:
    settlement = HumanInteractionSettlement(_request())
    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(_submission(), permission_granted=True, eligible=False)

    settlement.submit(_submission(), permission_granted=True, eligible=True)
    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(_submission(), permission_granted=True, eligible=False)


def test_a_stale_request_version_refuses() -> None:
    settlement = HumanInteractionSettlement(_request(request_version=4))
    for version in (3, 5):
        with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
            settlement.submit(
                _submission(request_version=version),
                permission_granted=True,
                eligible=True,
            )
    assert settlement.settled is None


def test_an_expired_request_refuses_and_the_expiry_boundary_is_inclusive() -> None:
    settlement = HumanInteractionSettlement(_request(expires_at_tick=100))
    for at_tick in (100, 101):
        with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
            settlement.submit(
                _submission(at_tick=at_tick), permission_granted=True, eligible=True
            )
    accepted = settlement.submit(
        _submission(at_tick=99), permission_granted=True, eligible=True
    )
    assert accepted.continuation_issued is True


def test_a_request_without_an_expiry_never_expires() -> None:
    settlement = HumanInteractionSettlement(_request(expires_at_tick=None))
    result = settlement.submit(
        _submission(at_tick=10**6), permission_granted=True, eligible=True
    )
    assert result.continuation_issued is True


def test_a_settlement_without_a_wait_never_issues_a_continuation() -> None:
    settlement = HumanInteractionSettlement(_request(wait_ref=None))
    result = settlement.submit(_submission(), permission_granted=True, eligible=True)

    assert result.continuation_issued is False
    assert result.continuation_token is None
    assert result.bundle_input_digest is None


def test_a_mismatched_idempotency_key_refuses_before_settlement() -> None:
    settlement = HumanInteractionSettlement(_request())
    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(
            _submission(idempotency_key="idempotency.harbour.other"),
            permission_granted=True,
            eligible=True,
        )
    assert settlement.settled is None


def test_an_identical_replay_is_a_no_op_that_issues_no_second_continuation() -> None:
    settlement = HumanInteractionSettlement(_request())
    first = settlement.submit(_submission(), permission_granted=True, eligible=True)
    replay = settlement.submit(
        _submission(at_tick=55), permission_granted=True, eligible=True
    )

    assert replay.replayed is True
    assert replay.accepted_actor == first.accepted_actor
    assert replay.response_digest == first.response_digest
    assert replay.owner == first.owner
    assert replay.continuation_issued is False
    assert replay.continuation_token is None
    assert replay.bundle_input_digest is None
    # The settled result itself is untouched by the replay.
    assert settlement.settled == first
    assert settlement.evidence == ()


def test_a_conflicting_second_responder_refuses_and_leaves_evidence() -> None:
    settlement = HumanInteractionSettlement(_request())
    accepted = settlement.submit(_submission(), permission_granted=True, eligible=True)

    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(
            _submission(actor="actor.harbour.wren", response_digest=_DIGEST_B),
            permission_granted=True,
            eligible=True,
        )

    assert settlement.settled == accepted
    assert len(settlement.evidence) == 1
    evidence = settlement.evidence[0]
    assert isinstance(evidence, Evidence)
    assert evidence.code == HUMAN_INTERACTION_OUTCOME_CONFLICT
    assert evidence.subject == "req.harbour.1"
    assert evidence.applied is False


def test_the_same_actor_answering_differently_is_also_a_conflict() -> None:
    settlement = HumanInteractionSettlement(_request())
    settlement.submit(_submission(), permission_granted=True, eligible=True)
    with pytest.raises(ExecutionRefused, match=HUMAN_INTERACTION_OUTCOME_CONFLICT):
        settlement.submit(
            _submission(response_digest=_DIGEST_B),
            permission_granted=True,
            eligible=True,
        )


def test_a_multi_responder_race_settles_exactly_one_continuation() -> None:
    settlement = HumanInteractionSettlement(
        _request(interaction_kind=INTERACTION_REVIEW)
    )
    contenders = (
        _submission(actor="actor.harbour.pell", response_digest=_DIGEST_A),
        _submission(actor="actor.harbour.wren", response_digest=_DIGEST_B),
        _submission(actor="actor.harbour.ives", response_digest=_DIGEST_B),
    )
    issued = []
    conflicts = 0
    for contender in contenders:
        try:
            result = settlement.submit(
                contender, permission_granted=True, eligible=True
            )
        except ExecutionRefused as refusal:
            assert refusal.reason == HUMAN_INTERACTION_OUTCOME_CONFLICT
            conflicts += 1
            continue
        if result.continuation_issued:
            issued.append(result.continuation_token)

    assert len(issued) == 1
    assert conflicts == 2
    assert len(settlement.evidence) == 2


@pytest.mark.parametrize("kind", sorted(INTERACTION_KINDS))
def test_all_interactions_preserve_the_declared_owner(kind: str) -> None:
    settlement = HumanInteractionSettlement(
        _request(interaction_kind=kind, owner="task.harbour.moorings")
    )
    result = settlement.submit(_submission(), permission_granted=True, eligible=True)
    assert result.owner == "task.harbour.moorings"
    assert result.accepted_actor == "actor.harbour.pell"


def test_the_four_interaction_kinds_are_closed_and_the_records_are_frozen() -> None:
    assert INTERACTION_KINDS == {"INPUT", "APPROVAL", "REVIEW", "HANDOFF"}
    with pytest.raises(ExecutionContractError, match="unknown_vocabulary_member"):
        _request(interaction_kind="CONSULTATION")
    with pytest.raises(FrozenInstanceError):
        request = _request()
        request.request_version = 9  # type: ignore[misc]


def test_a_submission_for_another_request_is_a_contract_error() -> None:
    settlement = HumanInteractionSettlement(_request())
    with pytest.raises(ExecutionContractError, match="request_mismatch"):
        settlement.submit(
            _submission(request_id="req.harbour.2"),
            permission_granted=True,
            eligible=True,
        )


def test_settlement_inputs_are_validated_eagerly() -> None:
    with pytest.raises(ExecutionContractError, match="invalid_digest"):
        _submission(response_digest="sha256:short")
    with pytest.raises(ExecutionContractError, match="invalid_ordinal"):
        _submission(at_tick=-1)
    with pytest.raises(ExecutionContractError, match="invalid_identifier"):
        _request(owner="Task Harbour")
    with pytest.raises(ExecutionContractError, match="invalid_request"):
        HumanInteractionSettlement("req.harbour.1")  # type: ignore[arg-type]
    settlement = HumanInteractionSettlement(_request())
    with pytest.raises(ExecutionContractError, match="invalid_flag"):
        settlement.submit(_submission(), permission_granted="yes", eligible=True)  # type: ignore[arg-type]


# --- autonomy resolution ---------------------------------------------------------------


def test_all_four_scope_dimensions_are_intersected() -> None:
    narrow = _source(
        SOURCE_POLICY,
        action_scope=("draft", "publish"),
        resource_scope=("index", "store"),
        model_scope=("slate", "tallow"),
        knowledge_scope=("corpus.a",),
    )
    narrower = _source(
        SOURCE_WORKFLOW_SETTINGS,
        action_scope=("publish", "retire"),
        resource_scope=("store", "cache"),
        model_scope=("tallow",),
        knowledge_scope=("corpus.a", "corpus.b"),
    )
    effective = resolve_autonomy((narrow, narrower), permission_granted=True)

    assert effective.action_scope == ("publish",)
    assert effective.resource_scope == ("store",)
    assert effective.model_scope == ("tallow",)
    assert effective.knowledge_scope == ("corpus.a",)


def test_the_numeric_bounds_take_the_minimum_across_every_source() -> None:
    effective = resolve_autonomy(
        (
            _wide_source(
                SOURCE_POLICY,
                maximum_wall_clock_ms=30_000,
                maximum_tokens=5_000,
                maximum_cost_units=900,
                maximum_steps=25,
            ),
            _wide_source(
                SOURCE_WORKFLOW_SETTINGS,
                maximum_wall_clock_ms=45_000,
                maximum_tokens=1_000,
                maximum_cost_units=50,
                maximum_steps=90,
            ),
        ),
        permission_granted=True,
    )
    assert effective.maximum_wall_clock_ms == 30_000
    assert effective.maximum_tokens == 1_000
    assert effective.maximum_cost_units == 50
    assert effective.maximum_steps == 25


def test_the_approval_requirements_are_unioned_and_canonically_ordered() -> None:
    effective = resolve_autonomy(
        (
            _wide_source(SOURCE_POLICY, approval_required=(EFFECT_EXTERNAL_EFFECT,)),
            _wide_source(
                SOURCE_WORKFLOW_SETTINGS,
                approval_required=(EFFECT_INTERNAL_WRITE, EFFECT_EXTERNAL_EFFECT),
            ),
        ),
        permission_granted=True,
    )
    assert effective.approval_required == (
        EFFECT_EXTERNAL_EFFECT,
        EFFECT_INTERNAL_WRITE,
    )


def test_every_absent_source_kind_contributes_an_explicit_bounded_default() -> None:
    effective = resolve_autonomy(
        (_wide_source(SOURCE_POLICY),), permission_granted=True
    )
    kinds = tuple(entry[0] for entry in effective.source_contributions)
    assert kinds == AUTONOMY_SOURCE_ORDER
    assert len(effective.source_contributions) == 7

    # The six defaults bound the numeric dimensions even though the one real source
    # declared none of them.
    assert effective.maximum_wall_clock_ms == DEFAULT_MAXIMUM_WALL_CLOCK_MS
    assert effective.maximum_tokens == DEFAULT_MAXIMUM_TOKENS
    assert effective.maximum_cost_units == DEFAULT_MAXIMUM_COST_UNITS
    assert effective.maximum_steps == DEFAULT_MAXIMUM_STEPS
    # And they narrow no scope, so the one real source's scopes survive intact.
    assert effective.action_scope == ("draft", "publish", "retire")


def test_a_default_source_declares_no_narrowing_and_a_finite_bound() -> None:
    default = default_autonomy_source(SOURCE_POLICY)
    assert default.action_scope is None
    assert default.resource_scope is None
    assert default.model_scope is None
    assert default.knowledge_scope is None
    assert default.approval_required == ()
    assert all(getattr(default, name) is not None for name in BOUND_DIMENSIONS)


def test_the_provenance_is_frozen_in_canonical_order_with_a_resolution_digest() -> None:
    sources = (_wide_source(SOURCE_POLICY), _wide_source(SOURCE_WORKFLOW_SETTINGS))
    first = resolve_autonomy(sources, permission_granted=True)
    # Supplied in the other order, the resolution is identical: the seam orders by the
    # canonical source order, not by the caller's argument order.
    second = resolve_autonomy(tuple(reversed(sources)), permission_granted=True)

    assert first.source_contributions == second.source_contributions
    assert all(len(entry) == 3 for entry in first.source_contributions)
    assert first.resolution_digest == second.resolution_digest
    assert first.resolution_digest.startswith("sha256:")

    changed = resolve_autonomy(
        (
            _wide_source(SOURCE_POLICY, snapshot_digest=_DIGEST_B),
            _wide_source(SOURCE_WORKFLOW_SETTINGS),
        ),
        permission_granted=True,
    )
    assert changed.resolution_digest != first.resolution_digest


def test_a_resolution_never_grants_permission() -> None:
    effective = resolve_autonomy(
        (_wide_source(SOURCE_POLICY),), permission_granted=True
    )
    assert effective.grants_permission is False


def test_an_empty_intersection_refuses() -> None:
    disjoint = (
        _wide_source(SOURCE_POLICY, action_scope=("draft",)),
        _wide_source(SOURCE_WORKFLOW_SETTINGS, action_scope=("retire",)),
    )
    with pytest.raises(ExecutionRefused, match=AUTONOMY_PROFILE_REFUSED):
        resolve_autonomy(disjoint, permission_granted=True)

    # A source that narrows a dimension to nothing is the same refusal.
    with pytest.raises(ExecutionRefused, match=AUTONOMY_PROFILE_REFUSED):
        resolve_autonomy(
            (_wide_source(SOURCE_POLICY, resource_scope=()),), permission_granted=True
        )


def test_a_dimension_no_source_ever_narrowed_grants_nothing_rather_than_everything() -> (
    None
):
    with pytest.raises(ExecutionRefused, match=AUTONOMY_PROFILE_REFUSED):
        resolve_autonomy((_source(SOURCE_POLICY),), permission_granted=True)


@pytest.mark.parametrize(
    ("field_name", "asked"),
    [
        ("wall_clock_ms", 40_000),
        ("tokens", 9_000),
        ("cost_units", 400),
        ("steps", 31),
    ],
)
def test_a_requested_spend_beyond_the_resolved_bound_refuses(
    field_name: str, asked: int
) -> None:
    sources = (
        _wide_source(
            SOURCE_POLICY,
            maximum_wall_clock_ms=30_000,
            maximum_tokens=8_000,
            maximum_cost_units=300,
            maximum_steps=30,
        ),
    )
    within = RequestedSpend(wall_clock_ms=10, tokens=10, cost_units=10, steps=10)
    resolve_autonomy(sources, permission_granted=True, requested=within)

    with pytest.raises(ExecutionRefused, match=AUTONOMY_PROFILE_REFUSED):
        resolve_autonomy(
            sources,
            permission_granted=True,
            requested=replace(within, **{field_name: asked}),
        )


def test_a_requested_spend_exactly_at_the_bound_is_admitted() -> None:
    sources = (_wide_source(SOURCE_POLICY, maximum_steps=30),)
    effective = resolve_autonomy(
        sources, permission_granted=True, requested=RequestedSpend(steps=30)
    )
    assert effective.maximum_steps == 30


def test_permission_denial_always_wins_even_over_a_perfectly_wide_resolution() -> None:
    sources = (_wide_source(SOURCE_POLICY), _wide_source(SOURCE_WORKFLOW_SETTINGS))
    resolve_autonomy(sources, permission_granted=True)
    with pytest.raises(ExecutionRefused, match=AUTONOMY_PROFILE_REFUSED):
        resolve_autonomy(sources, permission_granted=False)


def test_a_duplicate_source_kind_is_refused_rather_than_merged() -> None:
    with pytest.raises(ExecutionContractError, match="duplicate_source_kind"):
        resolve_autonomy(
            (_wide_source(SOURCE_POLICY), _wide_source(SOURCE_POLICY)),
            permission_granted=True,
        )


def test_autonomy_sources_validate_eagerly() -> None:
    with pytest.raises(ExecutionContractError, match="unknown_vocabulary_member"):
        _source("QUARRY_DEFAULTS")
    with pytest.raises(ExecutionContractError, match="invalid_digest"):
        _source(SOURCE_POLICY, snapshot_digest="sha1:abc")
    with pytest.raises(ExecutionContractError, match="unknown_vocabulary_member"):
        _source(SOURCE_POLICY, approval_required=("quarryEffect",))
    with pytest.raises(ExecutionContractError, match="duplicate_entry"):
        _source(SOURCE_POLICY, action_scope=("draft", "draft"))
    with pytest.raises(ExecutionContractError, match="invalid_ordinal"):
        _source(SOURCE_POLICY, maximum_steps=-1)


# --- plane ownership -------------------------------------------------------------------


def _acquire(plane: PlaneOwnership, **fields: object) -> LeaseGrant:
    return plane.acquire(  # type: ignore[arg-type]
        acquired_at_tick=0, expires_at_tick=100, **fields
    )


def _renew(plane: PlaneOwnership, **fields: object) -> LeaseGrant:
    return plane.renew(  # type: ignore[arg-type]
        renewed_at_tick=10, expires_at_tick=100, **fields
    )


def _take_over(plane: PlaneOwnership, **fields: object) -> LeaseGrant:
    return plane.take_over(at_tick=100, expires_at_tick=200, **fields)  # type: ignore[arg-type]


def _dispatch(plane: PlaneOwnership, **fields: object) -> DispatchRecord:
    return plane.dispatch(at_tick=20, **fields)  # type: ignore[arg-type]


def _apply_write(plane: PlaneOwnership, **fields: object) -> str:
    return plane.apply_write(at_tick=20, **fields)  # type: ignore[arg-type]


def _complete(plane: PlaneOwnership, **fields: object) -> CompletionOutcome:
    return plane.complete(at_tick=20, **fields)  # type: ignore[arg-type]


def _deliver(plane: PlaneOwnership, **fields: object) -> DeliveryDecision:
    return plane.deliver(at_tick=20, **fields)  # type: ignore[arg-type]


def _reconcile(plane: PlaneOwnership, **fields: object) -> ReconciliationDecision:
    return plane.reconcile(at_tick=20, **fields)  # type: ignore[arg-type]


def _listener_lease(plane: PlaneOwnership) -> tuple[str, int]:
    grant = _acquire(
        plane, plane_role=PLANE_LISTENER, scope="stream.harbour", owner="node.one"
    )
    return grant.lease_id, grant.fencing_token


def test_all_five_plane_roles_lease_independently_over_the_same_scope() -> None:
    plane = PlaneOwnership()
    grants = [
        _acquire(plane, plane_role=role, scope="partition.a", owner="node.one")
        for role in sorted(PLANE_ROLES)
    ]
    assert len(grants) == 5
    assert {grant.plane_role for grant in grants} == PLANE_ROLES
    assert len({grant.lease_id for grant in grants}) == 5
    assert len({grant.fencing_token for grant in grants}) == 5


def test_the_five_plane_roles_are_exactly_these() -> None:
    assert PLANE_ROLES == {
        PLANE_SCHEDULER,
        PLANE_LISTENER,
        PLANE_RESOURCE_SUPERVISOR,
        PLANE_WORKER,
        PLANE_CAPABILITY_GATEWAY,
    }


def test_fencing_tokens_are_strictly_increasing_and_never_reused() -> None:
    plane = PlaneOwnership()
    tokens: list[int] = []
    first = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    tokens.append(first.fencing_token)
    tokens.append(
        _renew(plane, lease_id=first.lease_id, owner="node.one").fencing_token
    )
    tokens.append(
        _renew(plane, lease_id=first.lease_id, owner="node.one").fencing_token
    )
    second = _take_over(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two"
    )
    tokens.append(second.fencing_token)
    tokens.append(
        _acquire(
            plane, plane_role=PLANE_SCHEDULER, scope="partition.b", owner="node.three"
        ).fencing_token
    )
    tokens.append(
        _renew(plane, lease_id=second.lease_id, owner="node.two").fencing_token
    )

    assert tokens == sorted(tokens)
    assert len(set(tokens)) == len(tokens)


def test_a_takeover_supersedes_the_holder_and_yields_evidence() -> None:
    plane = PlaneOwnership()
    first = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    second = _take_over(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two"
    )

    assert second.superseded_lease_id == first.lease_id
    assert second.superseded_fencing_token == first.fencing_token
    assert second.fencing_token > first.fencing_token

    assert len(plane.evidence) == 1
    evidence = plane.evidence[0]
    assert evidence.code == EXECUTION_PLANE_OWNERSHIP_TAKEOVER
    assert evidence.subject == second.lease_id
    assert evidence.applied is False


def test_a_live_lease_must_expire_or_be_revoked_before_takeover() -> None:
    plane = PlaneOwnership()
    first = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        plane.take_over(
            plane_role=PLANE_WORKER,
            scope="partition.a",
            owner="node.two",
            at_tick=99,
            expires_at_tick=200,
        )

    plane.revoke(lease_id=first.lease_id, fencing_token=first.fencing_token, at_tick=20)
    replacement = plane.take_over(
        plane_role=PLANE_WORKER,
        scope="partition.a",
        owner="node.two",
        at_tick=21,
        expires_at_tick=200,
    )
    assert replacement.superseded_lease_id == first.lease_id


def test_expired_or_revoked_authority_refuses_every_write_path() -> None:
    plane = PlaneOwnership()
    grant = plane.acquire(
        plane_role=PLANE_WORKER,
        scope="partition.a",
        owner="node.one",
        acquired_at_tick=0,
        expires_at_tick=10,
    )
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        plane.dispatch(
            lease_id=grant.lease_id,
            fencing_token=grant.fencing_token,
            dispatch_id="dispatch.expired",
            payload_digest=_PAYLOAD,
            at_tick=10,
        )
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        plane.apply_write(
            lease_id=grant.lease_id,
            fencing_token=grant.fencing_token,
            payload_digest=_PAYLOAD,
            at_tick=10,
        )


def test_an_unheld_scope_cannot_be_taken_over_and_a_held_one_cannot_be_acquired() -> (
    None
):
    plane = PlaneOwnership()
    with pytest.raises(ExecutionRefused, match="EXECUTION_PLANE_NOT_OWNED"):
        _take_over(
            plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two"
        )
    _acquire(plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one")
    with pytest.raises(ExecutionRefused, match="EXECUTION_PLANE_ALREADY_OWNED"):
        _acquire(plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two")


def test_a_superseded_holder_cannot_renew() -> None:
    plane = PlaneOwnership()
    first = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    _take_over(plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two")
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        _renew(plane, lease_id=first.lease_id, owner="node.one")


def test_a_stale_dispatch_refuses_with_stale_authority() -> None:
    plane = PlaneOwnership()
    first = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    _take_over(plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two")

    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        _dispatch(
            plane,
            lease_id=first.lease_id,
            fencing_token=first.fencing_token,
            dispatch_id="dispatch.one",
            payload_digest=_PAYLOAD,
        )


def test_a_stale_write_refuses_with_stale_authority() -> None:
    plane = PlaneOwnership()
    grant = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    _apply_write(
        plane,
        lease_id=grant.lease_id,
        fencing_token=grant.fencing_token,
        payload_digest=_PAYLOAD,
    )
    renewed = _renew(plane, lease_id=grant.lease_id, owner="node.one")

    # The pre-renewal token is no longer the lease's current token.
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        _apply_write(
            plane,
            lease_id=grant.lease_id,
            fencing_token=grant.fencing_token,
            payload_digest=_PAYLOAD,
        )
    _apply_write(
        plane,
        lease_id=grant.lease_id,
        fencing_token=renewed.fencing_token,
        payload_digest=_PAYLOAD,
    )


def test_an_unknown_lease_is_stale_authority_rather_than_a_silent_pass() -> None:
    plane = PlaneOwnership()
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        _apply_write(
            plane,
            lease_id="sha256:" + "9" * 64,
            fencing_token=1,
            payload_digest=_PAYLOAD,
        )


def test_a_current_completion_is_applied() -> None:
    plane = PlaneOwnership()
    grant = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    _dispatch(
        plane,
        lease_id=grant.lease_id,
        fencing_token=grant.fencing_token,
        dispatch_id="dispatch.one",
        payload_digest=_PAYLOAD,
    )
    outcome = _complete(
        plane,
        dispatch_id="dispatch.one",
        fencing_token=grant.fencing_token,
        outcome=OUTCOME_SUCCEEDED,
    )
    assert outcome.applied is True
    assert outcome.evidence is None
    assert plane.evidence == ()


def test_a_late_completion_is_evidence_only_and_is_never_applied() -> None:
    plane = PlaneOwnership()
    grant = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    _dispatch(
        plane,
        lease_id=grant.lease_id,
        fencing_token=grant.fencing_token,
        dispatch_id="dispatch.one",
        payload_digest=_PAYLOAD,
    )
    _take_over(plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two")

    outcome = _complete(
        plane,
        dispatch_id="dispatch.one",
        fencing_token=grant.fencing_token,
        outcome=OUTCOME_SUCCEEDED,
    )
    assert outcome.applied is False
    assert outcome.evidence is not None
    assert outcome.evidence.code == EXECUTION_PLANE_LATE_RESULT
    assert outcome.evidence.applied is False

    codes = [entry.code for entry in plane.evidence]
    assert codes == [EXECUTION_PLANE_OWNERSHIP_TAKEOVER, EXECUTION_PLANE_LATE_RESULT]


def test_a_completion_is_recorded_once_and_an_unknown_dispatch_is_refused() -> None:
    plane = PlaneOwnership()
    grant = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    _dispatch(
        plane,
        lease_id=grant.lease_id,
        fencing_token=grant.fencing_token,
        dispatch_id="dispatch.one",
        payload_digest=_PAYLOAD,
    )
    _complete(
        plane,
        dispatch_id="dispatch.one",
        fencing_token=grant.fencing_token,
        outcome=OUTCOME_SUCCEEDED,
    )
    with pytest.raises(ExecutionContractError, match="duplicate_completion"):
        _complete(
            plane,
            dispatch_id="dispatch.one",
            fencing_token=grant.fencing_token,
            outcome=OUTCOME_SUCCEEDED,
        )
    with pytest.raises(ExecutionContractError, match="unknown_dispatch"):
        _complete(
            plane,
            dispatch_id="dispatch.two",
            fencing_token=grant.fencing_token,
            outcome=OUTCOME_SUCCEEDED,
        )


def test_a_listener_deduplicates_a_redelivered_source_event() -> None:
    plane = PlaneOwnership()
    lease_id, token = _listener_lease(plane)

    first = _deliver(
        plane,
        lease_id=lease_id,
        fencing_token=token,
        delivery_id="delivery.one",
        dedupe_key="event.one",
    )
    second = _deliver(
        plane,
        lease_id=lease_id,
        fencing_token=token,
        delivery_id="delivery.two",
        dedupe_key="event.one",
    )

    assert first.disposition == DELIVERY_DELIVERED
    assert second.disposition == DELIVERY_DUPLICATE
    assert second.delivery_id == "delivery.one"
    assert second.in_flight == 1


def test_a_listener_sheds_new_work_at_bounded_capacity() -> None:
    plane = PlaneOwnership(listener_capacity=2)
    lease_id, token = _listener_lease(plane)
    for index in range(2):
        decision = _deliver(
            plane,
            lease_id=lease_id,
            fencing_token=token,
            delivery_id=f"delivery.{index}",
            dedupe_key=f"event.{index}",
        )
        assert decision.disposition == DELIVERY_DELIVERED
    assert decision.in_flight == decision.capacity == 2

    with pytest.raises(ExecutionRefused, match=LISTENER_SATURATED):
        _deliver(
            plane,
            lease_id=lease_id,
            fencing_token=token,
            delivery_id="delivery.2",
            dedupe_key="event.2",
        )
    assert plane.evidence[-1].code == LISTENER_SATURATED
    assert plane.evidence[-1].applied is False

    # A duplicate is still answered while saturated: a retry must not become a loss.
    duplicate = _deliver(
        plane,
        lease_id=lease_id,
        fencing_token=token,
        delivery_id="delivery.repeat",
        dedupe_key="event.0",
    )
    assert duplicate.disposition == DELIVERY_DUPLICATE

    assert plane.settle_delivery(dedupe_key="event.0") == 1
    admitted = _deliver(
        plane,
        lease_id=lease_id,
        fencing_token=token,
        delivery_id="delivery.2",
        dedupe_key="event.2",
    )
    assert admitted.disposition == DELIVERY_DELIVERED


def test_a_settled_delivery_is_still_remembered_for_deduplication() -> None:
    plane = PlaneOwnership(listener_capacity=2)
    lease_id, token = _listener_lease(plane)
    _deliver(
        plane,
        lease_id=lease_id,
        fencing_token=token,
        delivery_id="delivery.one",
        dedupe_key="event.one",
    )
    plane.settle_delivery(dedupe_key="event.one")
    repeat = _deliver(
        plane,
        lease_id=lease_id,
        fencing_token=token,
        delivery_id="delivery.two",
        dedupe_key="event.one",
    )
    assert repeat.disposition == DELIVERY_DUPLICATE
    with pytest.raises(ExecutionContractError, match="unknown_delivery"):
        plane.settle_delivery(dedupe_key="event.absent")


def test_delivery_and_reconciliation_require_their_own_plane_role() -> None:
    plane = PlaneOwnership()
    worker = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    with pytest.raises(ExecutionContractError, match="wrong_plane_role"):
        _deliver(
            plane,
            lease_id=worker.lease_id,
            fencing_token=worker.fencing_token,
            delivery_id="delivery.one",
            dedupe_key="event.one",
        )
    with pytest.raises(ExecutionContractError, match="wrong_plane_role"):
        _reconcile(
            plane,
            lease_id=worker.lease_id,
            fencing_token=worker.fencing_token,
            resource="pool.harbour",
            observation_digest=_DIGEST_A,
            observation_age=10,
            maximum_observation_age=1_000,
        )


def test_reconciliation_requires_a_current_lease_and_a_fresh_observation() -> None:
    plane = PlaneOwnership()
    grant = _acquire(
        plane,
        plane_role=PLANE_RESOURCE_SUPERVISOR,
        scope="pool.harbour",
        owner="node.one",
    )
    decision = _reconcile(
        plane,
        lease_id=grant.lease_id,
        fencing_token=grant.fencing_token,
        resource="pool.harbour",
        observation_digest=_DIGEST_A,
        observation_age=1_000,
        maximum_observation_age=1_000,
    )
    assert decision.observation_age == 1_000
    assert decision.fencing_token == grant.fencing_token

    with pytest.raises(ExecutionRefused, match="EXECUTION_PLANE_STALE_OBSERVATION"):
        _reconcile(
            plane,
            lease_id=grant.lease_id,
            fencing_token=grant.fencing_token,
            resource="pool.harbour",
            observation_digest=_DIGEST_A,
            observation_age=1_001,
            maximum_observation_age=1_000,
        )

    _take_over(
        plane,
        plane_role=PLANE_RESOURCE_SUPERVISOR,
        scope="pool.harbour",
        owner="node.two",
    )
    with pytest.raises(ExecutionRefused, match=EXECUTION_PLANE_STALE_AUTHORITY):
        _reconcile(
            plane,
            lease_id=grant.lease_id,
            fencing_token=grant.fencing_token,
            resource="pool.harbour",
            observation_digest=_DIGEST_A,
            observation_age=10,
            maximum_observation_age=1_000,
        )


def test_the_oracle_validates_its_own_construction() -> None:
    with pytest.raises(ExecutionContractError, match="unknown_vocabulary_member"):
        PlaneOwnership(deployment_mode="KUBERNETES")
    with pytest.raises(ExecutionContractError, match="invalid_capacity"):
        PlaneOwnership(listener_capacity=0)


# --- local and distributed answer identically -----------------------------------------


def _plane_transcript(mode: str) -> list[object]:
    """Drive one identical scenario through every plane behaviour and record the answers."""
    plane = PlaneOwnership(deployment_mode=mode, listener_capacity=2)
    transcript: list[object] = [plane.deployment_mode]

    first = _acquire(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.one"
    )
    transcript.append(first.fencing_token)
    renewed_grant = _renew(plane, lease_id=first.lease_id, owner="node.one")
    transcript.append(renewed_grant.fencing_token)
    renewed = renewed_grant.fencing_token
    _dispatch(
        plane,
        lease_id=first.lease_id,
        fencing_token=renewed,
        dispatch_id="dispatch.one",
        payload_digest=_PAYLOAD,
    )
    second = _take_over(
        plane, plane_role=PLANE_WORKER, scope="partition.a", owner="node.two"
    )
    transcript.append(second.superseded_fencing_token)
    transcript.append(tuple(entry.code for entry in plane.evidence))

    try:
        _dispatch(
            plane,
            lease_id=first.lease_id,
            fencing_token=renewed,
            dispatch_id="dispatch.two",
            payload_digest=_PAYLOAD,
        )
    except ExecutionRefused as refusal:
        transcript.append(refusal.reason)

    late = _complete(
        plane,
        dispatch_id="dispatch.one",
        fencing_token=renewed,
        outcome=OUTCOME_SUCCEEDED,
    )
    transcript.append((late.applied, late.evidence.code if late.evidence else None))

    listener = _acquire(
        plane, plane_role=PLANE_LISTENER, scope="stream.harbour", owner="node.one"
    )
    transcript.append(
        _deliver(
            plane,
            lease_id=listener.lease_id,
            fencing_token=listener.fencing_token,
            delivery_id="delivery.one",
            dedupe_key="event.one",
        ).disposition
    )
    transcript.append(
        _deliver(
            plane,
            lease_id=listener.lease_id,
            fencing_token=listener.fencing_token,
            delivery_id="delivery.two",
            dedupe_key="event.one",
        ).disposition
    )
    _deliver(
        plane,
        lease_id=listener.lease_id,
        fencing_token=listener.fencing_token,
        delivery_id="delivery.three",
        dedupe_key="event.two",
    )
    try:
        _deliver(
            plane,
            lease_id=listener.lease_id,
            fencing_token=listener.fencing_token,
            delivery_id="delivery.four",
            dedupe_key="event.three",
        )
    except ExecutionRefused as refusal:
        transcript.append(refusal.reason)

    supervisor = _acquire(
        plane,
        plane_role=PLANE_RESOURCE_SUPERVISOR,
        scope="pool.harbour",
        owner="node.one",
    )
    transcript.append(
        _reconcile(
            plane,
            lease_id=supervisor.lease_id,
            fencing_token=supervisor.fencing_token,
            resource="pool.harbour",
            observation_digest=_DIGEST_A,
            observation_age=5,
            maximum_observation_age=10,
        ).observation_age
    )
    return transcript


def test_the_plane_answers_identically_for_local_and_distributed_inputs() -> None:
    local = _plane_transcript(DEPLOYMENT_LOCAL)
    distributed = _plane_transcript(DEPLOYMENT_DISTRIBUTED)

    assert DEPLOYMENT_MODES == {DEPLOYMENT_LOCAL, DEPLOYMENT_DISTRIBUTED}
    assert local[0] == DEPLOYMENT_LOCAL
    assert distributed[0] == DEPLOYMENT_DISTRIBUTED
    assert local[1:] == distributed[1:]


def test_autonomy_resolves_identically_for_local_and_distributed_inputs() -> None:
    sources = (_wide_source(SOURCE_POLICY), _wide_source(SOURCE_WORKFLOW_SETTINGS))
    local = resolve_autonomy(
        sources, permission_granted=True, deployment_mode=DEPLOYMENT_LOCAL
    )
    distributed = resolve_autonomy(
        sources, permission_granted=True, deployment_mode=DEPLOYMENT_DISTRIBUTED
    )
    assert local == distributed


def test_the_seam_names_no_deployment_realisation_concept() -> None:
    """No Kubernetes, process, pod, broker, queue or partition vocabulary leaks in here."""
    import omnivia_core_runtime.execution.governed as module

    forbidden = ("KUBERNETES", "POD", "BROKER", "QUEUE", "PROCESS", "CONTAINER")
    exported = set(module.__all__)
    assert not any(any(word in name.upper() for word in forbidden) for name in exported)
