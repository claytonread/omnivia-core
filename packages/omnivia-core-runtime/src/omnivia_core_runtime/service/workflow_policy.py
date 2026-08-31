"""The production policy and budget decision authority for a new Workflow Run.

``storage.agent_runtime`` can *record* a ``PolicySnapshot``, a ``BudgetSnapshot`` and a
``CapabilityGrant``; nothing in this build *decided* one. This module is that decision,
and it is deliberately not a merge engine. Effective policy is resolved field by field
from an explicitly configured, ordered list of authoritative sources:

    platform safety boundary, organisation, workspace, environment, workflow settings,
    component contract, component instance override, authorised run override

Each field combines by the rule that field's meaning allows, never by a generic deep
merge, because a deep merge over runtime-significant policy silently lets a later source
widen what an earlier one narrowed:

* set-valued **allowed capabilities** combine by **intersection** -- a source that does
  not state the field abstains, and a source that states it can only take away;
* **offered** capabilities combine by union and are *discovery, not authority*: they are
  carried into ``discovered_capabilities``, which grants nothing, and a grant is issued
  only against the intersected allowed set;
* numeric **maximum ceilings** combine by the **minimum** applicable maximum;
* required **evidence** sets combine by **union**, because a later source may demand
  more evidence and may not excuse an earlier source's demand;
* **side-effect permission** is **deny wins**: one source saying no ends the question.

Everything else fails closed. No configured source, no safety boundary, an unknown
source kind, sources out of precedence order or repeated, no source stating the
capability set, no source stating a required ceiling, a negative or non-integer ceiling,
and a required capability the intersection does not grant are each a
:class:`DecisionRefused` rather than a decision. :func:`resolve_effective_policy` needs
no run, so a caller can establish that a coherent decision *exists* before it admits
anything; :meth:`EffectivePolicy.decide` materialises the immutable contract records for
one run, and every one of them is put through its accepted-contract validator before it
is returned.

Identifiers are derived, never allocated: a snapshot id is a digest of the workspace,
the run, the revision and the resolved source trace, so replaying the same decision
computes the same id rather than minting a second record of one decision.

**Re-pinning cannot broaden.** ``decide`` takes the run's current decision when there is
one, advances the revision from it, and runs the accepted contract's progression
validators, so a mid-flight re-pin that would re-grant a capability or raise a ceiling is
refused rather than written.

Nothing here writes. The records are returned for the admission transaction to persist
inside its own fence, because a decision recorded outside the mutation that admits the
run is a decision about a run that may never exist.

:func:`load_policy_sources` is how a real deployment states those sources.
``service/runner.py`` reads :data:`AUTHORITY_FILENAME` out of this workspace's
installation-local runtime directory at startup and hands the document here; absent
means nothing is configured and ``workflow.start`` refuses, and present-but-unusable
stops the service rather than serving under an authority nobody can read. The decoding
is deliberately strict -- an unknown member, a non-string capability, a non-integer
ceiling and a non-boolean side-effect permission are each a :class:`DecisionRefused` --
because a policy document that is quietly half-understood grants a run something no
operator wrote down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, NoReturn

from omnivia_core.contracts.v1 import (
    BudgetSnapshot,
    CapabilityGrant,
    ContractSemanticError,
    PolicySnapshot,
    validate_budget_snapshot,
    validate_budget_snapshot_progression,
    validate_capability_grant,
    validate_policy_snapshot,
    validate_policy_snapshot_progression,
)

#: The authoritative source kinds, in the order they resolve. Position is precedence for
#: the fields where precedence applies, and the order a configuration must be given in:
#: a list that jumps around is a trace nobody can read as a resolution.
SOURCE_KINDS: Final = (
    "platform_safety_boundary",
    "organisation",
    "workspace",
    "environment",
    "workflow_settings",
    "component_contract",
    "component_instance_override",
    "authorised_run_override",
)

#: The floor. Every other source can only narrow it, so a resolution without it is a
#: resolution with no floor at all.
SAFETY_BOUNDARY_KIND: Final = "platform_safety_boundary"

#: The file a service reads its configured authority from, in this workspace's
#: installation-local runtime directory beside the discovery descriptor.
#:
#: Installation-local and per-workspace, because that is what the statement is: a
#: portable workspace's layout is closed and must not carry one machine's opinion about
#: what its runs may spend, and a command-line flag would not reach the service a
#: managed start spawns, which is the process the CLI path actually talks to.
AUTHORITY_FILENAME: Final = "workflow-policy.json"

#: `PolicySnapshot.decision_reason` for a decision this resolver made. The resolved
#: source trace is on `EffectivePolicy.source_trace`, which is where it fits: an
#: `OpenCode` is bounded at 128 characters and eight source ids do not fit in one.
DECISION_REASON: Final = "effective_policy_resolution"


class DecisionRefused(Exception):
    """No coherent decision can be made from the configured sources."""


@dataclass(frozen=True)
class PolicySource:
    """One authoritative source's statement about effective policy.

    Every field is optional and ``None``/empty means *this source states nothing about
    that field*, which is different from stating an empty set: abstaining leaves the
    field to the other sources, whereas an empty ``allowed_capabilities`` intersects
    everything away. Keeping the two apart is what makes field-specific resolution
    possible without a merge that guesses.
    """

    kind: str
    source_id: str
    allowed_capabilities: tuple[str, ...] | None = None
    offered_capabilities: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    max_cost_units: int | None = None
    max_token_units: int | None = None
    max_wall_clock_ms: int | None = None
    required_evidence: tuple[str, ...] = ()
    side_effects_allowed: bool | None = None


@dataclass(frozen=True)
class EffectivePolicy:
    """What the configured sources resolve to, before any run exists.

    ``side_effects_allowed`` and ``required_evidence`` have no field on the accepted
    ``PolicySnapshot`` and are carried here rather than folded into one: a decision the
    contract has no place for is still a decision, and inventing a place for it on the
    wire would be this module extending v1.
    """

    granted_capabilities: tuple[str, ...]
    discovered_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    required_evidence: tuple[str, ...]
    side_effects_allowed: bool
    max_cost_units: int
    max_token_units: int
    max_wall_clock_ms: int | None
    source_trace: tuple[str, ...]

    def decide(
        self,
        *,
        workspace_id: str,
        run_id: str,
        pinned_at: str,
        audit_reference: str,
        scopes: tuple[str, ...],
        purpose: str,
        previous: RunDecision | None = None,
    ) -> RunDecision:
        """This effective policy as the immutable records one run is admitted under."""
        revision = 1 if previous is None else previous.policy.revision + 1
        trace = (workspace_id, run_id, str(revision), *self.source_trace)
        policy = PolicySnapshot(
            workspace_id=workspace_id,
            policy_snapshot_id=_derived_id("workflow_policy_snapshot", *trace),
            run_id=run_id,
            revision=revision,
            pinned_at=pinned_at,
            granted_capabilities=self.granted_capabilities,
            discovered_capabilities=self.discovered_capabilities,
            decision_reason=DECISION_REASON,
            audit_reference=audit_reference,
        )
        budget = BudgetSnapshot(
            workspace_id=workspace_id,
            budget_snapshot_id=_derived_id("workflow_budget_snapshot", *trace),
            run_id=run_id,
            revision=revision,
            pinned_at=pinned_at,
            max_cost_units=self.max_cost_units,
            # Consumption is carried forward, never reset: a re-pin that zeroed the
            # counters would report a run had spent nothing since it started.
            consumed_cost_units=(
                0 if previous is None else previous.budget.consumed_cost_units
            ),
            max_token_units=self.max_token_units,
            consumed_token_units=(
                0 if previous is None else previous.budget.consumed_token_units
            ),
            max_wall_clock_ms=self.max_wall_clock_ms,
        )
        grants = tuple(
            CapabilityGrant(
                workspace_id=workspace_id,
                capability_grant_id=_derived_id(
                    "workflow_capability_grant",
                    workspace_id,
                    run_id,
                    policy.policy_snapshot_id,
                    capability,
                ),
                run_id=run_id,
                capability_id=capability,
                policy_snapshot_id=policy.policy_snapshot_id,
                granted_at=pinned_at,
                scopes=scopes,
                purpose=purpose,
            )
            for capability in self.granted_capabilities
        )
        scope = {"run_id": run_id, "workspace_id": workspace_id}
        try:
            validate_policy_snapshot(policy, **scope)
            validate_budget_snapshot(budget, **scope)
            for grant in grants:
                validate_capability_grant(grant, policy=policy, **scope)
            if previous is not None:
                validate_policy_snapshot_progression(previous.policy, policy)
                validate_budget_snapshot_progression(previous.budget, budget)
        except ContractSemanticError as error:
            raise DecisionRefused(
                f"the resolved decision is not a valid one for this run: {error}"
            ) from error
        return RunDecision(policy=policy, budget=budget, grants=grants, effective=self)


@dataclass(frozen=True)
class RunDecision:
    """One run's decision as records, ready for the admission transaction to persist."""

    policy: PolicySnapshot
    budget: BudgetSnapshot
    grants: tuple[CapabilityGrant, ...]
    effective: EffectivePolicy


def resolve_effective_policy(
    sources: tuple[PolicySource, ...] | None,
) -> EffectivePolicy:
    """Resolve the configured sources field by field, or refuse.

    Called before a run exists, so a caller can prove a coherent decision is available
    without admitting anything.
    """
    if not sources:
        return _refuse(
            "no policy and budget decision sources are configured, so there is no "
            "authority to resolve an effective policy from"
        )
    _check_trace(sources)

    granted = _intersection(sources)
    if granted is None:
        return _refuse(
            "no configured source states allowed_capabilities, so nothing decides "
            "which capabilities a run may invoke"
        )
    required = _union(sources, "required_capabilities")
    ungranted = tuple(sorted(set(required) - set(granted)))
    if ungranted:
        return _refuse(
            f"the workflow requires capabilities the resolved policy does not grant: "
            f"{list(ungranted)}"
        )

    max_cost_units = _minimum(sources, "max_cost_units")
    max_token_units = _minimum(sources, "max_token_units")
    for field, ceiling in (
        ("max_cost_units", max_cost_units),
        ("max_token_units", max_token_units),
    ):
        if ceiling is None:
            _refuse(
                f"no configured source states {field}, so nothing decides what this "
                "run is admitted to spend"
            )
    assert max_cost_units is not None and max_token_units is not None
    return EffectivePolicy(
        granted_capabilities=granted,
        discovered_capabilities=_union(sources, "offered_capabilities"),
        required_capabilities=required,
        required_evidence=_union(sources, "required_evidence"),
        # Deny wins: one source refusing side effects ends the question, and no source
        # having an opinion is not permission.
        side_effects_allowed=all(
            source.side_effects_allowed is not False for source in sources
        )
        and any(source.side_effects_allowed is True for source in sources),
        max_cost_units=max_cost_units,
        max_token_units=max_token_units,
        max_wall_clock_ms=_minimum(sources, "max_wall_clock_ms"),
        source_trace=tuple(f"{s.kind}:{s.source_id}" for s in sources),
    )


#: Set-valued members, which abstain by being absent rather than by being empty.
_SET_MEMBERS: Final = (
    "offered_capabilities",
    "required_capabilities",
    "required_evidence",
)

#: Numeric ceiling members, each optional and each combining by minimum.
_CEILING_MEMBERS: Final = ("max_cost_units", "max_token_units", "max_wall_clock_ms")

#: Everything a configured source may state. Anything else is refused rather than
#: ignored: a member this decoder does not understand is a decision it did not apply.
_MEMBERS: Final = frozenset(
    ("kind", "source_id", "allowed_capabilities", "side_effects_allowed")
    + _SET_MEMBERS
    + _CEILING_MEMBERS
)


def load_policy_sources(document: object) -> tuple[PolicySource, ...]:
    """One configured authority document as ordered sources, or a refusal.

    Decoding only. Precedence, the safety-boundary floor and every field rule stay in
    :func:`resolve_effective_policy`, so a document that decodes is not yet a document
    that resolves and a caller has to ask for both.
    """
    if not isinstance(document, dict) or set(document) != {"sources"}:
        _refuse(
            "a workflow decision authority document is one object stating exactly "
            "one member, 'sources'"
        )
    entries = document["sources"]
    if not isinstance(entries, list) or not entries:
        _refuse("'sources' must be a non-empty list of authoritative sources")
    return tuple(_source(entry, ordinal) for ordinal, entry in enumerate(entries))


def _source(entry: object, ordinal: int) -> PolicySource:
    where = f"source {ordinal}"
    if not isinstance(entry, dict):
        _refuse(f"{where} is not an object")
    unknown = sorted(set(entry) - _MEMBERS)
    if unknown:
        _refuse(f"{where} states members this build does not apply: {unknown}")
    kind = entry.get("kind")
    source_id = entry.get("source_id")
    if not isinstance(kind, str) or not isinstance(source_id, str):
        _refuse(f"{where} must state a string 'kind' and a string 'source_id'")
    return PolicySource(
        kind=kind,
        source_id=source_id,
        # Absent abstains; present -- including an empty list -- intersects.
        allowed_capabilities=(
            None
            if "allowed_capabilities" not in entry
            else _strings(entry["allowed_capabilities"], where, "allowed_capabilities")
        ),
        offered_capabilities=_strings(
            entry.get("offered_capabilities", ()), where, "offered_capabilities"
        ),
        required_capabilities=_strings(
            entry.get("required_capabilities", ()), where, "required_capabilities"
        ),
        required_evidence=_strings(
            entry.get("required_evidence", ()), where, "required_evidence"
        ),
        max_cost_units=_ceiling(entry.get("max_cost_units"), where, "max_cost_units"),
        max_token_units=_ceiling(entry.get("max_token_units"), where, "max_token_units"),
        max_wall_clock_ms=_ceiling(
            entry.get("max_wall_clock_ms"), where, "max_wall_clock_ms"
        ),
        side_effects_allowed=_flag(entry.get("side_effects_allowed"), where),
    )


def _strings(value: Any, where: str, member: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        _refuse(f"{where} states a {member} that is not a list")
    if not all(isinstance(item, str) and item for item in value):
        _refuse(f"{where} states a {member} that is not a list of non-empty strings")
    return tuple(value)


def _ceiling(value: Any, where: str, member: str) -> int | None:
    if value is None:
        return None
    # `bool` is an `int` in Python, and a ceiling of `True` is not a ceiling.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _refuse(f"{where} states a {member} that is not a non-negative integer")
    return value


def _flag(value: Any, where: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    _refuse(f"{where} states a side_effects_allowed that is not a boolean")


def _check_trace(sources: tuple[PolicySource, ...]) -> None:
    """Refuse a source list that cannot be read as one resolution."""
    positions = []
    for source in sources:
        if source.kind not in SOURCE_KINDS:
            _refuse(f"unknown policy source kind {source.kind!r}")
        # `|` is the digest preimage separator, so a source id containing one could
        # make two different traces derive the same snapshot id.
        if not source.source_id or "|" in source.source_id:
            _refuse(
                f"source {source.kind!r} has no usable source_id, so its decision "
                "cannot be traced"
            )
        positions.append(SOURCE_KINDS.index(source.kind))
    if len(set(positions)) != len(positions):
        _refuse("a policy source kind is configured twice; precedence is undefined")
    if positions != sorted(positions):
        _refuse(
            "the configured policy sources are not in precedence order, so the trace "
            "does not describe the resolution it would produce"
        )
    if SOURCE_KINDS.index(SAFETY_BOUNDARY_KIND) not in positions:
        _refuse(
            f"no {SAFETY_BOUNDARY_KIND} source is configured; every other source can "
            "only narrow it, so a resolution without it has no floor"
        )


def _intersection(sources: tuple[PolicySource, ...]) -> tuple[str, ...] | None:
    """The allowed capability sets, intersected. `None` when no source states one."""
    allowed: set[str] | None = None
    for source in sources:
        if source.allowed_capabilities is None:
            continue
        stated = set(source.allowed_capabilities)
        allowed = stated if allowed is None else allowed & stated
    return None if allowed is None else tuple(sorted(allowed))


def _union(sources: tuple[PolicySource, ...], field: str) -> tuple[str, ...]:
    values: set[str] = set()
    for source in sources:
        values.update(getattr(source, field))
    return tuple(sorted(values))


def _minimum(sources: tuple[PolicySource, ...], field: str) -> int | None:
    """The smallest applicable maximum. `None` when no source states one."""
    stated = []
    for source in sources:
        value = getattr(source, field)
        if value is None:
            continue
        # `bool` is an `int` in Python, and a ceiling of `True` is not a ceiling.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _refuse(f"{source.kind} states an invalid {field}: {value!r}")
        stated.append(value)
    return min(stated) if stated else None


def _refuse(message: str) -> NoReturn:
    raise DecisionRefused(message)


def _derived_id(*parts: str) -> str:
    return f"sha256:{sha256('|'.join(parts).encode('utf-8')).hexdigest()}"


__all__ = [
    "AUTHORITY_FILENAME",
    "DECISION_REASON",
    "SAFETY_BOUNDARY_KIND",
    "SOURCE_KINDS",
    "DecisionRefused",
    "EffectivePolicy",
    "PolicySource",
    "RunDecision",
    "load_policy_sources",
    "resolve_effective_policy",
]
