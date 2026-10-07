"""Shared completion-gate configuration for scheduler tests (DEV-REQ-137).

The accepted criteria and the independent evidence are configured the way a composition would
configure them: the gate is given to the scheduler, and the reader supplies what it observed. A
test that completes a run therefore proves it through the same rule production uses, rather than
through a bypass.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from hashlib import sha256

from omnivia_core_runtime.service.completion_gate import (
    AcceptedCompletion,
    CompletionGate,
    EvidenceItem,
    EvidenceReadout,
    SelfEvidenceException,
)

IMPLEMENTER = "implementer-agent"
COLLECTOR = "independent-collector"
REVIEWER = "independent-reviewer"
CRITERIA = ("artefact-verified", "tests-pass")
DEFINITION_DIGEST = "sha256:" + sha256(b"accepted-definition-v1").hexdigest()

#: What a scripted reader answers for (workspace_id, run_id, fencing_generation).
Answer = Callable[[str, str, int], EvidenceReadout]


def digest_for(*parts: str) -> str:
    return "sha256:" + sha256("|".join(parts).encode("utf-8")).hexdigest()


def accepted_for(
    run_id: str,
    *,
    criteria: tuple[str, ...] = CRITERIA,
    self_evidence: SelfEvidenceException | None = None,
) -> AcceptedCompletion:
    return AcceptedCompletion(
        run_id=run_id,
        candidate_id=f"candidate-{run_id}",
        binding_id=f"binding-{run_id}",
        definition_digest=DEFINITION_DIGEST,
        aggregate_id="aggregate-review",
        package_id="package-review",
        application_id="application-review",
        implementer_id=IMPLEMENTER,
        criteria=criteria,
        self_evidence=self_evidence,
    )


def current_generation(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1"
    ).fetchone()
    return int(row[0])


def item_for(
    run_id: str,
    criterion: str,
    *,
    outcome: str = "proven",
    collected_by: str = COLLECTOR,
    reviewed_by: str = REVIEWER,
) -> EvidenceItem:
    return EvidenceItem(
        criterion=criterion,
        outcome=outcome,
        evidence_id=f"evidence-{run_id}-{criterion}",
        content_digest=digest_for(run_id, criterion),
        collected_by=collected_by,
        reviewed_by=reviewed_by,
    )


def proven_readout(
    accepted: AcceptedCompletion, *, generation: int, workspace_id: str
) -> EvidenceReadout:
    """A complete, fully proven observation of `accepted`, under `generation`."""
    return EvidenceReadout(
        workspace_id=workspace_id,
        run_id=accepted.run_id,
        candidate_id=accepted.candidate_id,
        binding_id=accepted.binding_id,
        definition_digest=accepted.definition_digest,
        aggregate_id=accepted.aggregate_id,
        package_id=accepted.package_id,
        application_id=accepted.application_id,
        fencing_generation=generation,
        complete=True,
        items=tuple(item_for(accepted.run_id, name) for name in accepted.criteria),
    )


class ScriptedReader:
    """Reports proven evidence for every run unless a test scripts a different answer.

    It is handed the identifiers and the fence and nothing else, as the production boundary is, and it
    records each call so a test can prove what it was given.
    """

    def __init__(self) -> None:
        self.answer: Answer | None = None
        self.calls: list[tuple[str, str, int]] = []

    def read(self, *, workspace_id: str, run_id: str, fencing_generation: int) -> EvidenceReadout:
        self.calls.append((workspace_id, run_id, fencing_generation))
        if self.answer is not None:
            return self.answer(workspace_id, run_id, fencing_generation)
        return proven_readout(
            accepted_for(run_id), generation=fencing_generation, workspace_id=workspace_id
        )


def reader_answering(
    change: Callable[[EvidenceReadout], EvidenceReadout],
) -> ScriptedReader:
    """A reader whose honest proven observation is altered by `change`."""
    reader = ScriptedReader()
    reader.answer = lambda workspace_id, run_id, generation: change(
        proven_readout(
            accepted_for(run_id), generation=generation, workspace_id=workspace_id
        )
    )
    return reader


def gate(reader: ScriptedReader | None = None) -> CompletionGate:
    return CompletionGate(reader=reader or ScriptedReader(), accepted=accepted_for)


__all__ = [
    "COLLECTOR",
    "CRITERIA",
    "DEFINITION_DIGEST",
    "IMPLEMENTER",
    "REVIEWER",
    "ScriptedReader",
    "accepted_for",
    "current_generation",
    "gate",
    "item_for",
    "proven_readout",
    "reader_answering",
]
