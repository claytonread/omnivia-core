"""The agent-host provider SPI: ten hooks, their DTOs, and a deterministic mock.

This package owns the *contract* an agent host and a provider meet on -- the
hook vocabulary, the SPI-local request wrapper, and the result and error
algebra -- plus one in-process provider that implements it deterministically so
an adapter author can develop without a workspace, a lease or a Core Service.

What is re-exported here is the surface an adapter needs to call a hook and
read its outcome. Deliberately not re-exported, and imported from
:mod:`omnivia_core.agent_host.spi` by name when they are wanted: the envelope
constructors, the hook partitions and the composition table. Those are the
boundary layer's tools for proving what a hook reached in Core, not an
adapter's, and naming the module at the call site says which of the two jobs is
being done.

The conformance corpus loader and its report DTOs are re-exported for the same
reason the mock is: they are what an adapter author runs against. The loader
only reads the accepted corpus into immutable value objects; executing a case
against a provider is
:mod:`omnivia_core.agent_host.conformance_runner`'s job, and its comparator,
one-case executor and corpus runner are re-exported here too. The input recipes
in :mod:`omnivia_core.agent_host.vector_inputs` are not: they are the runner's
own half of the lane, and an adapter author runs the corpus rather than
assembling one call at a time.

The lifecycle layer in :mod:`omnivia_core.agent_host.lifecycle_conformance` is
re-exported to the same depth and no further: its generated sequences, its
executor and the value objects an adapter author reads a run back out of. The
generators, the derived hook sets and the family names stay behind the module,
because selecting or extending a family is the boundary layer's job and naming
the module at the call site says which of the two jobs is being done.

The boundary layer in :mod:`omnivia_core.agent_host.boundary_conformance` is
re-exported more narrowly still: its one entry point and the report an adapter
author reads a verdict out of. Its observation type, its evaluator and the
invariant definitions it derives from the frozen contract stay behind the module,
because constructing an observation is how a violation is *synthesised* rather
than run, which is the boundary layer's own job.

The compatibility layer in
:mod:`omnivia_core.agent_host.compatibility_conformance` is re-exported at the
same depth as the boundary layer: its one entry point, the report an adapter
author reads a verdict out of, and the mismatch it reads a failure out of. The
section 7 matrix, its row and result types and its executor stay behind the
module, because building a row is how a matrix violation is *synthesised*
rather than run.

The mock is re-exported because developing against it is the point of it
shipping at all. It holds no durable state: everything it keeps between calls
is dropped by :meth:`MockProvider.reset`.
"""

from __future__ import annotations

from omnivia_core.agent_host.boundary_conformance import (
    BoundaryConformanceError,
    BoundaryReport,
    BoundaryViolation,
    InvariantResult,
    run_boundary_conformance,
)
from omnivia_core.agent_host.compatibility_conformance import (
    CompatibilityConformanceError,
    CompatibilityMismatch,
    CompatibilityReport,
    run_compatibility_conformance,
)
from omnivia_core.agent_host.conformance import (
    CaseReport,
    ConformanceCase,
    ConformanceCorpus,
    CorpusError,
    CorpusReport,
    ExpectedOutcome,
    load_corpus,
)
from omnivia_core.agent_host.conformance_runner import (
    CaseComparison,
    ConformanceRunError,
    Mismatch,
    compare_case,
    execute_case,
    run_corpus,
    run_corpus_file,
)
from omnivia_core.agent_host.lifecycle_conformance import (
    LIFECYCLE_SEQUENCES,
    LifecycleSequence,
    LifecycleSequenceError,
    LifecycleStep,
    SequenceResult,
    StepResult,
    run_lifecycle_sequences,
    run_sequence,
)
from omnivia_core.agent_host.mock import MockProvider, ProviderProfile
from omnivia_core.agent_host.spi import (
    SPI_VERSION,
    SPI_VERSION_MAXIMUM,
    SPI_VERSION_MINIMUM,
    ApprovalKind,
    Disposition,
    Hook,
    HookIntent,
    HookOutcome,
    Reason,
    SpiContractError,
    SpiProvenance,
    SpiRequest,
    VersionAxes,
)

__all__ = [
    "LIFECYCLE_SEQUENCES",
    "SPI_VERSION",
    "SPI_VERSION_MAXIMUM",
    "SPI_VERSION_MINIMUM",
    "ApprovalKind",
    "BoundaryConformanceError",
    "BoundaryReport",
    "BoundaryViolation",
    "CaseComparison",
    "CaseReport",
    "CompatibilityConformanceError",
    "CompatibilityMismatch",
    "CompatibilityReport",
    "ConformanceCase",
    "ConformanceCorpus",
    "ConformanceRunError",
    "CorpusError",
    "CorpusReport",
    "Disposition",
    "ExpectedOutcome",
    "Hook",
    "HookIntent",
    "HookOutcome",
    "InvariantResult",
    "LifecycleSequence",
    "LifecycleSequenceError",
    "LifecycleStep",
    "Mismatch",
    "MockProvider",
    "ProviderProfile",
    "Reason",
    "SequenceResult",
    "SpiContractError",
    "SpiProvenance",
    "SpiRequest",
    "StepResult",
    "VersionAxes",
    "compare_case",
    "execute_case",
    "load_corpus",
    "run_boundary_conformance",
    "run_compatibility_conformance",
    "run_corpus",
    "run_corpus_file",
    "run_lifecycle_sequences",
    "run_sequence",
]
