"""Workflow Run ingress conformance (V06-8, A8-N1).

Executable conformance for the accepted A8 boundary fixture, and nothing else.
This package implements no Workflow Runtime, no host adapter and no run-state
storage; it adds no operation, capability, error code or envelope field. It
turns the twenty-four accepted `RUN-V-*` boundary cases into observations
derived from the frozen operation catalogue and driven across the existing
application-contract adapter seam.

- :mod:`~omnivia_core.workflow_run.boundary` -- the shared vocabulary.
- :mod:`~omnivia_core.workflow_run.conformance` -- loading the accepted corpus.
- :mod:`~omnivia_core.workflow_run.ingress` -- the answer-blind input recipes.
- :mod:`~omnivia_core.workflow_run.runner` -- the join, the invariants, the report.

Standard library only.
"""

from __future__ import annotations

from omnivia_core.workflow_run.boundary import (
    BranchId,
    Disposition,
    MutationAuthority,
    PrincipalKind,
)
from omnivia_core.workflow_run.conformance import (
    EXPECTED_CASE_IDS,
    CorpusError,
    WorkflowRunCase,
    WorkflowRunCorpus,
    load_corpus,
)
from omnivia_core.workflow_run.ingress import (
    RECIPES,
    IngressObservation,
    IngressRecipe,
    execute_recipe,
)
from omnivia_core.workflow_run.runner import (
    CORPUS_RELATIVE_PATH,
    ConformanceReport,
    run_conformance,
    run_corpus,
)

__all__ = [
    "CORPUS_RELATIVE_PATH",
    "EXPECTED_CASE_IDS",
    "RECIPES",
    "BranchId",
    "ConformanceReport",
    "CorpusError",
    "Disposition",
    "IngressObservation",
    "IngressRecipe",
    "MutationAuthority",
    "PrincipalKind",
    "WorkflowRunCase",
    "WorkflowRunCorpus",
    "execute_recipe",
    "load_corpus",
    "run_conformance",
    "run_corpus",
]
