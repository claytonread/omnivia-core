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

The mock is re-exported because developing against it is the point of it
shipping at all. It holds no durable state: everything it keeps between calls
is dropped by :meth:`MockProvider.reset`.
"""

from __future__ import annotations

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
    "SPI_VERSION",
    "SPI_VERSION_MAXIMUM",
    "SPI_VERSION_MINIMUM",
    "ApprovalKind",
    "CaseComparison",
    "CaseReport",
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
    "Mismatch",
    "MockProvider",
    "ProviderProfile",
    "Reason",
    "SpiContractError",
    "SpiProvenance",
    "SpiRequest",
    "VersionAxes",
    "compare_case",
    "execute_case",
    "load_corpus",
    "run_corpus",
    "run_corpus_file",
]
