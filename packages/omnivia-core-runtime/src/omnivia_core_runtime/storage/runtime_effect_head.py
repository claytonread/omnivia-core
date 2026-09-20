"""Where one material effect's settlement chain currently ends, and when nowhere.

Migration 0023 records an audited answer for an effect intent, and 0024 records
that a later answer *supersedes* an earlier `unknown` one -- as a link from a
`source_effect_settlement_id` to a `resulting_effect_settlement_id`. Neither
migration stores "the current answer", because neither may: 0024 keeps history
rather than rewriting the settlement reconciliation was owed for, and 0023 puts no
uniqueness on how many settlements one intent may carry. So "what does this effect
say now" is a read over the links, and this module is that read.

The shape of the link graph is what makes the read non-trivial. 0024 declares
`UNIQUE (workspace_id, resulting_effect_settlement_id)`, so a settlement has at
most one *incoming* link. It declares nothing of the kind on the source side, so a
settlement may have several *outgoing* ones: two reconcilers, or one reconciler
run twice, may each append their own later answer for the same unknown. That is
a branch, and a branch is exactly where a reader is tempted to do the one thing it
must not.

*Nothing here is chosen by timestamp.* Two terminal answers that disagree -- an
`APPLIED` on one arm and a `NOT_APPLIED` on the other -- are two claims about
whether a real external effect happened, and the later of two wall-clock instants
is not evidence about which one is true. Picking one would publish a guess as a
fact, so a branched chain resolves to :data:`EFFECT_HEAD_BRANCHED` and the caller
is told it has no head, not given one. Unlinked alternatives -- an intent carrying
two settlements that no reconciliation joins -- are the same situation without the
links, and get the same answer.

*`committed` and `not_committed` are settled; `unknown` is not.* In 0024's
vocabulary `APPLIED` and `NOT_APPLIED` are answers, and their resulting
settlements are `committed` and `not_committed`; `PARTIAL` and `UNKNOWN` both
result in a further `unknown` settlement, which is not an answer and which a later
reconciliation may still supersede. A head whose outcome is `unknown` is therefore
reported as :data:`EFFECT_HEAD_UNRESOLVED` -- it is the end of the chain *so far*,
not the end of the question.

An intent with no settlement at all is unresolved too, with no head to name.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "EFFECT_HEAD_BRANCHED",
    "EFFECT_HEAD_RESOLUTIONS",
    "EFFECT_HEAD_SETTLED",
    "EFFECT_HEAD_UNRESOLVED",
    "SETTLED_EFFECT_OUTCOMES",
    "EffectHead",
    "read_effect_head",
    "read_effect_heads",
]

_SETTLEMENTS: Final = "omnivia_runtime_effect_settlements"
_RECONCILIATIONS: Final = "omnivia_runtime_effect_reconciliations"

#: The chain ends in an answer: 0024's `APPLIED` or `NOT_APPLIED`.
EFFECT_HEAD_SETTLED: Final = "settled"

#: The chain ends in an `unknown` settlement, or has no settlement at all. A later
#: reconciliation may still append an answer, so this is not a terminal reading.
EFFECT_HEAD_UNRESOLVED: Final = "unresolved"

#: The chain has more than one end. No head is reported, and none is chosen.
EFFECT_HEAD_BRANCHED: Final = "branched"

EFFECT_HEAD_RESOLUTIONS: Final[tuple[str, ...]] = (
    EFFECT_HEAD_SETTLED,
    EFFECT_HEAD_UNRESOLVED,
    EFFECT_HEAD_BRANCHED,
)

#: 0023's settlement outcomes that are an answer. `unknown` is deliberately absent.
SETTLED_EFFECT_OUTCOMES: Final[frozenset[str]] = frozenset({"committed", "not_committed"})

#: SQLite's default bound on host parameters in one statement is 999. Reads here are
#: chunked well under it so a caller's intent list never decides whether a query runs.
_CHUNK: Final = 256


@dataclass(frozen=True, slots=True)
class EffectHead:
    """The current end of one effect intent's settlement chain.

    `effect_settlement_id` and `outcome` are `None` for a branched chain, because
    there is no single end to name, and for an intent that carries no settlement.
    """

    effect_intent_id: str
    resolution: str
    effect_settlement_id: str | None
    outcome: str | None

    @property
    def settled(self) -> bool:
        """Whether this effect has an answer, as opposed to no end or no answer yet."""
        return self.resolution == EFFECT_HEAD_SETTLED


def read_effect_head(
    connection: sqlite3.Connection, *, workspace_id: str, effect_intent_id: str
) -> EffectHead:
    """Where one effect intent's settlement chain currently ends."""
    return read_effect_heads(
        connection, workspace_id=workspace_id, effect_intent_ids=(effect_intent_id,)
    )[effect_intent_id]


def read_effect_heads(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    effect_intent_ids: Sequence[str],
) -> dict[str, EffectHead]:
    """Where each named effect intent's settlement chain currently ends.

    Every requested intent appears in the result, including one this workspace
    holds no settlement for -- which reads as unresolved rather than as a missing
    key, because "no answer has been recorded" is itself the answer.
    """
    wanted = tuple(dict.fromkeys(effect_intent_ids))
    if not wanted:
        return {}

    # Two reads for the whole batch: every settlement of these intents, then every
    # link between those settlements. The link read is restricted to the settlements
    # just read rather than to the intents, because 0024's own guard already binds a
    # reconciliation's source and result to one intent -- so a link whose source is
    # not in this set cannot belong to one of these chains.
    outcomes: dict[str, dict[str, str]] = {intent: {} for intent in wanted}
    owner: dict[str, str] = {}
    for chunk in _chunks(wanted):
        placeholders = ", ".join("?" for _ in chunk)
        for row in connection.execute(
            f"SELECT effect_intent_id, effect_settlement_id, outcome FROM {_SETTLEMENTS} "
            f"WHERE workspace_id = ? AND effect_intent_id IN ({placeholders})",
            (workspace_id, *chunk),
        ):
            intent, settlement, outcome = str(row[0]), str(row[1]), str(row[2])
            outcomes[intent][settlement] = outcome
            owner[settlement] = intent

    links: dict[str, set[str]] = {}
    linked: set[str] = set()
    settlements = tuple(owner)
    for chunk in _chunks(settlements):
        placeholders = ", ".join("?" for _ in chunk)
        for row in connection.execute(
            "SELECT source_effect_settlement_id, resulting_effect_settlement_id "
            f"FROM {_RECONCILIATIONS} WHERE workspace_id = ? "
            f"AND source_effect_settlement_id IN ({placeholders})",
            (workspace_id, *chunk),
        ):
            source, resulting = str(row[0]), str(row[1])
            links.setdefault(source, set()).add(resulting)
            linked.add(resulting)

    return {
        intent: _resolve(intent, outcomes[intent], links, linked) for intent in wanted
    }


def _resolve(
    effect_intent_id: str,
    outcomes: dict[str, str],
    links: dict[str, set[str]],
    linked: set[str],
) -> EffectHead:
    """The one end of this intent's chain, or the refusal to name one.

    Walking forward from every root rather than backward from every leaf is what
    makes a branch visible: a settlement with two outgoing links puts two arms on
    the stack, and both are followed to their own end.
    """
    if not outcomes:
        return EffectHead(effect_intent_id, EFFECT_HEAD_UNRESOLVED, None, None)

    pending = [
        settlement for settlement in sorted(outcomes) if settlement not in linked
    ]
    seen: set[str] = set()
    ends: set[str] = set()
    while pending:
        settlement = pending.pop()
        if settlement in seen:
            continue
        seen.add(settlement)
        successors = links.get(settlement, set())
        if not successors:
            ends.add(settlement)
            continue
        pending.extend(sorted(successors))

    if len(ends) != 1:
        # More than one end is a branch. Zero ends means every settlement of this
        # intent is the result of some link -- a cycle 0024's time and identity
        # guards should make unreachable -- and guessing a head for a graph that
        # cannot be walked is the same error as guessing between two.
        return EffectHead(effect_intent_id, EFFECT_HEAD_BRANCHED, None, None)

    head = ends.pop()
    outcome = outcomes.get(head)
    if outcome is None:
        # The chain left this intent, which 0024's guard forbids. Read fail-closed
        # rather than reporting a head whose answer belongs to something else.
        return EffectHead(effect_intent_id, EFFECT_HEAD_BRANCHED, None, None)
    resolution = (
        EFFECT_HEAD_SETTLED
        if outcome in SETTLED_EFFECT_OUTCOMES
        else EFFECT_HEAD_UNRESOLVED
    )
    return EffectHead(effect_intent_id, resolution, head, outcome)


def _chunks(values: Sequence[str]) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), _CHUNK):
        yield tuple(values[start : start + _CHUNK])
