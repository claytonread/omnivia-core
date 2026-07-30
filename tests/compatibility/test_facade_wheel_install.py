"""Packaging-metadata proof for the omnivia-memory compatibility distribution.

The production repair this test covers makes ``omnivia-memory`` declare
``omnivia-core`` as a real package dependency
(``services/omnivia-memory/pyproject.toml``), not only via a ``PYTHONPATH``
trick that happens to work inside this checkout. This module proves that the
declaration survives the whole build-and-install path, using only locally
built artifacts:

1. build the ``omnivia-core`` and ``omnivia-memory`` wheels from this checkout
   with build isolation disabled (no index, no environment provisioning);
2. read ``Requires-Dist`` out of the compatibility wheel's own ``METADATA``
   and require its unconditional requirements to be exactly two -- one
   ``omnivia-core>=0.1.0,<0.2.0`` and one ``sqlalchemy>=2.0.0`` -- with every
   other entry gated on an ``extra``;
3. create a throwaway virtual environment and install both locally built
   wheels out of the wheelhouse with ``--no-index --no-deps``;
4. inside that environment, use ``importlib.metadata`` to confirm both
   distributions are installed at 0.1.0 and that the *installed*
   ``omnivia-memory`` metadata still carries the exact Core requirement;
5. read the Core wheel's own file list and confirm the runtime-owned Graph
   modules were never packaged into it;
6. in a second child process inside that environment, import the Graph facade
   leaves out of the *installed* artifacts and confirm the split holds there:
   the eleven portable definitions are the exact canonical objects, the four
   relevance-scoring helpers still work and are still owned by the legacy
   module, and Core has neither those helpers nor the Graph runtime;
7. in a third child process, do the same for the two ingestion facade leaves
   under their hybrid barrels: the seven ingestion and ten watcher routed
   definitions are the exact canonical objects, the ``IngestSource`` alias is
   still the same object as ``Source``, the barrels' runtime-owned exports are
   still legacy-only, and the watcher tracker's same-named ``SourceReference``
   is still its own distinct class;
8. in a fourth child process, do the same for the workspace facade leaf under
   its hybrid barrel: the five routed definitions are the exact canonical
   objects, the barrel's ``WorkspaceRepository``/``WorkspaceService`` exports
   are still legacy-only, the repository and service hold the canonical models,
   and Core packages neither of them;
9. in a fifth child process, import all six ``hybrid_facade`` barrels together
   and confirm the whole accounting the state claims: 62 portable exports are
   the exact canonical objects, 31 runtime exports are the exact legacy objects
   of their declared owners, no runtime name reaches a canonical barrel, and no
   runtime owner has a canonical twin to have come from;
10. install the Core wheel alone into a *second* throwaway environment and
    confirm the canonical barrels stand up with ``omnivia-memory`` absent
    entirely, exposing every portable name and no runtime one; in the same
    environment, confirm the canonical package *root* is version-only, publishes
    none of the compatibility root's 186 advertised-or-hidden names, and names
    the legacy package nowhere -- and that the Core wheel declares no reverse
    dependency on the compatibility distribution.

Scope limits, stated explicitly so this evidence is not over-read:

* This is **not** a dependency-resolution proof. ``--no-deps`` deliberately
  disables resolution, and ``--no-index`` means no index is consulted; step 3
  proves the two artifacts install, not that pip would resolve
  ``omnivia-core`` for a user installing ``omnivia-memory`` from an index.
  The test asserts that SQLAlchemy is absent from the environment to keep
  that boundary visible rather than implied.
* Steps 6, 7 and 8 are **not** a full runtime-root import proof either. They
  import the Graph, ingestion and workspace compatibility leaves, their
  barrels, and their canonical counterparts, which is possible precisely
  because those closures need no third-party package -- the Graph and workspace
  runtimes reach SQLite through the standard library, and the ingestion
  extractors import PyMuPDF and ``python-docx`` lazily inside the methods that
  use them. Nothing that
  would need the uninstalled SQLAlchemy dependency is imported. Broader facade
  import behaviour and the rest of the canonical object identities are covered
  by ``tests/compatibility/test_facade_foundation.py``, in the source tree.

Every step is offline and fail-closed: no index access, no pip cache
dependency, no ``pytest.skip`` path. All work happens outside the repository,
in a temporary directory removed on exit (success or failure); nothing is
written under the repo tree, and ``PYTHONPATH`` is stripped from the child
environments so no source tree leaks into the installed-artifact checks.

This intentionally does not touch the four-package topology check in
``scripts/check-package-boundaries.py`` / ``scripts/check-package-builds.sh`` /
``tests/test_package_boundaries.py``: ``omnivia-memory`` is not one of the four
``omnivia-core``/``omnivia-core-runtime``/``omnivia-core-mcp``/``omnivia-core-cli``
distributions in that topology (ADR-036). It is the separate, transitional
compatibility distribution this task is repairing, so it gets its own,
narrowly scoped packaging proof here instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

from packaging.requirements import Requirement

from baseline.facade_manifest import (
    CANONICAL_ROOT_ALL,
    ROOT_FACADE_ALL,
    ROOT_FACADE_HIDDEN_BINDINGS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = REPO_ROOT
MEMORY_DIR = REPO_ROOT / "services" / "omnivia-memory"
PYTHON = sys.executable

EXPECTED_VERSION = "0.1.0"
REQUIRED_CORE_DEPENDENCY = "omnivia-core>=0.1.0,<0.2.0"
REQUIRED_SQLALCHEMY_DEPENDENCY = "sqlalchemy>=2.0.0"


def _module_available(name: str) -> bool:
    result = subprocess.run([PYTHON, "-c", f"import {name}"], capture_output=True, check=False)
    return result.returncode == 0


def _child_env() -> dict[str, str]:
    """The repo suite runs with ``PYTHONPATH=.:src:services/omnivia-memory/src``.
    Drop it for every child process here so builds and installed-artifact
    checks see only their own environment, never this source tree."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _build_wheel(project_dir: Path, wheelhouse: Path) -> Path:
    """Build ``project_dir``'s wheel into ``wheelhouse`` without build
    isolation, so no build environment is provisioned from an index."""
    before = set(wheelhouse.glob("*.whl"))
    if _module_available("build"):
        command = [
            PYTHON, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(wheelhouse), str(project_dir),
        ]
    else:
        command = [
            PYTHON, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(wheelhouse), str(project_dir),
        ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False, env=_child_env()
    )
    assert result.returncode == 0, (
        f"failed to build a wheel for {project_dir}:\n{result.stdout}\n{result.stderr}"
    )
    (built,) = set(wheelhouse.glob("*.whl")) - before
    return built


def _wheel_metadata(wheel_path: Path) -> str:
    with zipfile.ZipFile(wheel_path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def _requires_dist(metadata: str) -> list[str]:
    return [
        line.removeprefix("Requires-Dist:").strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]


def _assert_declares_exactly_core_and_sqlalchemy(requires: list[str], *, source: str) -> None:
    """The *unconditional* requirements in ``requires`` must be exactly one
    ``omnivia-core`` requirement carrying the sanctioned range plus one
    ``sqlalchemy`` requirement -- compared by parsed name/specifier, so this is
    independent of however the build backend serialises a specifier, and closed
    against a second, looser Core pin being added alongside the first.

    Anything else in ``Requires-Dist`` must be gated on an ``extra`` (the
    project's ``dev`` optional-dependency group), so nothing that installs by
    default can hide behind a non-extra environment marker.
    """
    parsed = [Requirement(raw) for raw in requires]
    runtime = [req for req in parsed if req.marker is None]
    for req in parsed:
        if req.marker is None:
            continue
        assert "extra" in str(req.marker), (
            f"{source} declares {str(req)!r}, whose marker is not gated on an extra"
        )
    assert sorted(req.name for req in runtime) == ["omnivia-core", "sqlalchemy"], (
        f"{source} declares unconditional requirements {[str(r) for r in runtime]}, "
        "expected exactly one omnivia-core and one sqlalchemy requirement"
    )
    for expected in (REQUIRED_CORE_DEPENDENCY, REQUIRED_SQLALCHEMY_DEPENDENCY):
        wanted = Requirement(expected)
        (actual,) = [req for req in runtime if req.name == wanted.name]
        assert actual.specifier == wanted.specifier, (
            f"{source} declares {str(actual)!r}, expected {expected!r}"
        )


#: Emitted inside the throwaway environment. ``importlib.metadata.version``
#: raises ``PackageNotFoundError`` for a missing distribution, so a failed
#: install surfaces as a non-zero exit with a traceback rather than a pass.
INSTALLED_METADATA_SCRIPT = """
import importlib.metadata as md
import json

try:
    md.distribution("sqlalchemy")
    sqlalchemy_installed = True
except md.PackageNotFoundError:
    sqlalchemy_installed = False

print(json.dumps({
    "core_version": md.version("omnivia-core"),
    "memory_version": md.version("omnivia-memory"),
    "memory_requires": md.requires("omnivia-memory"),
    "sqlalchemy_installed": sqlalchemy_installed,
}))
"""

#: The eight portable definitions the ``graph.models`` facade routes, and the
#: three the ``graph.search_models`` split facade routes. Restated here rather
#: than imported from ``baseline.inventory``: this file checks *installed
#: artifacts*, and reading the expectation out of the source tree it is meant to
#: be independent of would weaken the check.
GRAPH_MODELS_ROUTED = (
    "ApprovalStatus",
    "Entity",
    "EntityCreate",
    "EntityMemoryLink",
    "EntityType",
    "Relationship",
    "RelationshipCreate",
    "RelationshipType",
)
GRAPH_SEARCH_MODELS_ROUTED = (
    "GraphSearchQuery",
    "GraphSearchResult",
    "GraphSearchResultSet",
)
#: The four relevance-scoring helpers that stay owned by the legacy module and
#: must never be packaged into the Core wheel.
GRAPH_RETAINED_HELPERS = (
    "compute_relevance_score",
    "score_name_match",
    "score_neighbor_overlap",
    "score_relationship_count",
)
#: Graph modules that are runtime-owned: legacy-only, never in the Core wheel.
GRAPH_RUNTIME_WHEEL_PATHS = (
    "omnivia_core/graph/repository.py",
    "omnivia_core/graph/search_service.py",
    "omnivia_core/graph/service.py",
)

#: The seven definitions the ``ingestion.models`` facade routes and the ten the
#: ``ingestion.watcher.models`` facade routes. Restated here rather than imported
#: from ``baseline.inventory``, for the same reason the Graph sets above are: this
#: file checks *installed artifacts*, and reading the expectation out of the
#: source tree it is meant to be independent of would weaken the check.
INGESTION_MODELS_ROUTED = (
    "Chunk",
    "ExtractionResult",
    "FileInventory",
    "FileType",
    "IngestSource",
    "ParseStatus",
    "Source",
)
WATCHER_MODELS_ROUTED = (
    "DebounceConfig",
    "FileChange",
    "FileChangeBatch",
    "FileChangeType",
    "IndexerScheduler",
    "IndexerState",
    "IndexerStatus",
    "ScheduledJob",
    "SourceReference",
    "WatchedPath",
)
#: The two hybrid barrels' runtime-owned exports, which stay legacy-only and must
#: never appear on the installed canonical barrels.
INGESTION_BARREL_RUNTIME_EXPORTS = (
    "BaseChunker",
    "BaseExtractor",
    "CharacterChunker",
    "ChunkConfig",
    "ChunkRepository",
    "DOCXExtractor",
    "FileInfo",
    "FileScanner",
    "IngestResult",
    "IngestionPipeline",
    "MarkdownExtractor",
    "PDFExtractor",
    "ParagraphChunker",
    "ScanOptions",
)
WATCHER_BARREL_RUNTIME_EXPORTS = ("Debouncer", "SourceTracker")
#: The two routed names the ``ingestion`` barrel has never re-exported, and must
#: not acquire in the installed artifacts either.
INGESTION_LEAF_ONLY = ("FileInventory", "IngestSource")
#: Ingestion modules that are runtime-owned: legacy-only, never in the Core wheel.
INGESTION_RUNTIME_WHEEL_PATHS = (
    "omnivia_core/ingestion/chunker.py",
    "omnivia_core/ingestion/extractors.py",
    "omnivia_core/ingestion/pipeline.py",
    "omnivia_core/ingestion/repositories.py",
    "omnivia_core/ingestion/scanner.py",
    "omnivia_core/ingestion/watcher/debouncer.py",
    "omnivia_core/ingestion/watcher/tracker.py",
)

#: Run inside the throwaway environment, against the installed wheels only:
#: ``-I`` strips ``PYTHONPATH``, user site and the cwd, so every import below
#: resolves out of site-packages. Any failure raises and exits non-zero.
INSTALLED_GRAPH_FACADE_SCRIPT = f"""
import math
import sys

import omnivia_core.graph
import omnivia_core.graph.models
import omnivia_core.graph.search_models
import omnivia_memory.graph.models
import omnivia_memory.graph.search_models

legacy_models = sys.modules["omnivia_memory.graph.models"]
legacy_records = sys.modules["omnivia_memory.graph.search_models"]
canonical_models = sys.modules["omnivia_core.graph.models"]
canonical_records = sys.modules["omnivia_core.graph.search_models"]

for name in {GRAPH_MODELS_ROUTED!r}:
    assert getattr(legacy_models, name) is getattr(canonical_models, name), name
for name in {GRAPH_SEARCH_MODELS_ROUTED!r}:
    assert getattr(legacy_records, name) is getattr(canonical_records, name), name

# Every module resolved from the installed distributions, not a source tree.
for module in (legacy_models, legacy_records, canonical_models, canonical_records):
    assert "site-packages" in module.__file__, module.__file__

# The retained half is still legacy-owned, still absent from Core, and still works.
for name in {GRAPH_RETAINED_HELPERS!r}:
    helper = getattr(legacy_records, name)
    assert helper.__module__ == "omnivia_memory.graph.search_models", name
    assert not hasattr(canonical_records, name), name
    assert not hasattr(omnivia_core.graph, name), name
    assert name not in omnivia_core.graph.__all__, name

assert legacy_records.score_name_match("alice", "Alice") == 1.0
assert legacy_records.score_name_match("ali", "alice") == 3 / 5
assert legacy_records.score_name_match("", "alice") == 0.0
assert legacy_records.score_relationship_count(0, 0) == 0.0
assert legacy_records.score_relationship_count(6, 4) == min(1.0, math.log2(11) / 10.0)
assert legacy_records.score_neighbor_overlap(["Alice", "Bob"], ["alice", "carol"]) == 0.5
assert legacy_records.score_neighbor_overlap([], ["alice"]) == 0.0
assert legacy_records.compute_relevance_score("alice", "Alice Smith", 3, 2, ["bob"]) == (
    0.2919
)

# The routed records still round-trip through the canonical objects.
entity = canonical_models.Entity(
    id="e1", name="Alice", entity_type=canonical_models.EntityType.PERSON
)
query = legacy_records.GraphSearchQuery(query="alice", depth=2, limit=5)
result = legacy_records.GraphSearchResult(entity=entity, score=0.5, matched_on="name")
payload = legacy_records.GraphSearchResultSet(
    results=[result], total_count=1, query=query
).to_dict()
assert canonical_records.GraphSearchResultSet.from_dict(payload).to_dict() == payload

# The Graph runtime is legacy-only: importable from the compatibility
# distribution, absent from Core.
import omnivia_memory.graph.repository
import omnivia_memory.graph.search_service
import omnivia_memory.graph.service

for name in ("repository", "search_service", "service"):
    assert f"omnivia_core.graph.{{name}}" not in sys.modules, name
    try:
        __import__(f"omnivia_core.graph.{{name}}")
    except ImportError:
        pass
    else:
        raise AssertionError(f"omnivia_core.graph.{{name}} is importable")

search_service = sys.modules["omnivia_memory.graph.search_service"]
assert search_service.compute_relevance_score is legacy_records.compute_relevance_score
assert search_service.GraphSearchQuery is canonical_records.GraphSearchQuery
print("OK")
"""

#: Run inside the throwaway environment, against the installed wheels only. Same
#: ``-I`` isolation as the Graph script above. The ingestion closure needs no
#: third-party package either: the PDF/DOCX extractors import PyMuPDF and
#: ``python-docx`` lazily, inside the methods that use them.
INSTALLED_INGESTION_FACADE_SCRIPT = f"""
import sys

import omnivia_core.ingestion
import omnivia_core.ingestion.models
import omnivia_core.ingestion.watcher
import omnivia_core.ingestion.watcher.models
import omnivia_memory.ingestion
import omnivia_memory.ingestion.models
import omnivia_memory.ingestion.watcher
import omnivia_memory.ingestion.watcher.models

legacy_models = sys.modules["omnivia_memory.ingestion.models"]
legacy_watcher = sys.modules["omnivia_memory.ingestion.watcher.models"]
canonical_models = sys.modules["omnivia_core.ingestion.models"]
canonical_watcher = sys.modules["omnivia_core.ingestion.watcher.models"]

for name in {INGESTION_MODELS_ROUTED!r}:
    assert getattr(legacy_models, name) is getattr(canonical_models, name), name
for name in {WATCHER_MODELS_ROUTED!r}:
    assert getattr(legacy_watcher, name) is getattr(canonical_watcher, name), name

# Every module resolved from the installed distributions, not a source tree.
for module in (legacy_models, legacy_watcher, canonical_models, canonical_watcher):
    assert "site-packages" in module.__file__, module.__file__

# ``IngestSource`` is an identity alias, in both trees and across them.
assert canonical_models.IngestSource is canonical_models.Source
assert legacy_models.IngestSource is legacy_models.Source
assert legacy_models.IngestSource is canonical_models.Source

# The hybrid barrels: the portable half is canonical, the runtime half is
# legacy-only, and the two leaf-only routed names are on neither barrel.
for name in ("Chunk", "ExtractionResult", "FileType", "ParseStatus", "Source"):
    assert getattr(omnivia_memory.ingestion, name) is getattr(canonical_models, name)
    assert getattr(omnivia_core.ingestion, name) is getattr(canonical_models, name)
for name in {WATCHER_MODELS_ROUTED!r}:
    assert getattr(omnivia_memory.ingestion.watcher, name) is (
        getattr(canonical_watcher, name)
    )
    assert getattr(omnivia_core.ingestion.watcher, name) is (
        getattr(canonical_watcher, name)
    )
for name in {INGESTION_BARREL_RUNTIME_EXPORTS!r}:
    assert name in omnivia_memory.ingestion.__all__, name
    assert not hasattr(omnivia_core.ingestion, name), name
    assert name not in omnivia_core.ingestion.__all__, name
for name in {WATCHER_BARREL_RUNTIME_EXPORTS!r}:
    assert name in omnivia_memory.ingestion.watcher.__all__, name
    assert not hasattr(omnivia_core.ingestion.watcher, name), name
    assert name not in omnivia_core.ingestion.watcher.__all__, name
for name in {INGESTION_LEAF_ONLY!r}:
    assert hasattr(legacy_models, name), name
    assert not hasattr(omnivia_memory.ingestion, name), name
    assert not hasattr(omnivia_core.ingestion, name), name

# The routed records still round-trip through the canonical objects.
source = legacy_models.Source(path="/w/a.md", file_type=canonical_models.FileType.MARKDOWN)
assert canonical_models.Source.from_dict(source.to_dict()).to_dict() == source.to_dict()
chunk = legacy_models.Chunk(source_id=source.id, chunk_index=0, content="hello")
assert canonical_models.Chunk.from_dict(chunk.to_dict()) == chunk
assert legacy_models.ExtractionResult.success("hello").status is (
    canonical_models.ParseStatus.SUCCESS
)
change = legacy_watcher.FileChange(
    path="/w/a.md", event_type=canonical_watcher.FileChangeType.MODIFIED
)
assert canonical_watcher.FileChange.from_dict(change.to_dict()).to_dict() == (
    change.to_dict()
)

# The ingestion runtime is legacy-only: importable from the compatibility
# distribution, absent from Core.
import omnivia_memory.ingestion.chunker
import omnivia_memory.ingestion.extractors
import omnivia_memory.ingestion.scanner
import omnivia_memory.ingestion.watcher.debouncer
import omnivia_memory.ingestion.watcher.tracker

for name in ("chunker", "extractors", "pipeline", "repositories", "scanner"):
    assert f"omnivia_core.ingestion.{{name}}" not in sys.modules, name
    try:
        __import__(f"omnivia_core.ingestion.{{name}}")
    except ImportError:
        pass
    else:
        raise AssertionError(f"omnivia_core.ingestion.{{name}} is importable")
for name in ("debouncer", "tracker"):
    try:
        __import__(f"omnivia_core.ingestion.watcher.{{name}}")
    except ImportError:
        pass
    else:
        raise AssertionError(f"omnivia_core.ingestion.watcher.{{name}} is importable")

chunker = sys.modules["omnivia_memory.ingestion.chunker"]
extractors = sys.modules["omnivia_memory.ingestion.extractors"]
scanner = sys.modules["omnivia_memory.ingestion.scanner"]
debouncer = sys.modules["omnivia_memory.ingestion.watcher.debouncer"]
tracker = sys.modules["omnivia_memory.ingestion.watcher.tracker"]
assert chunker.Chunk is canonical_models.Chunk
assert extractors.ExtractionResult is canonical_models.ExtractionResult
assert scanner.FileType is canonical_models.FileType
assert debouncer.FileChange is canonical_watcher.FileChange
assert debouncer.DebounceConfig is canonical_watcher.DebounceConfig

# The tracker's same-named record stays its own distinct class.
assert tracker.SourceReference is not canonical_watcher.SourceReference
assert tracker.SourceReference.__module__ == "omnivia_memory.ingestion.watcher.tracker"
assert omnivia_memory.ingestion.watcher.SourceReference is (
    canonical_watcher.SourceReference
)
print("OK")
"""

#: The five definitions the ``workspace.models`` facade routes. Restated here
#: rather than imported from ``baseline.inventory``, for the same reason the
#: Graph and ingestion sets above are: this file checks *installed artifacts*,
#: and reading the expectation out of the source tree it is meant to be
#: independent of would weaken the check.
WORKSPACE_MODELS_ROUTED = (
    "ImportSummary",
    "Workspace",
    "WorkspaceCreate",
    "WorkspaceIndexStatus",
    "WorkspaceUpdate",
)
#: The hybrid barrel's runtime-owned exports, which stay legacy-only and must
#: never appear on the installed canonical barrel.
WORKSPACE_BARREL_RUNTIME_EXPORTS = ("WorkspaceRepository", "WorkspaceService")
#: Workspace modules that are runtime-owned: legacy-only, never in the Core wheel.
WORKSPACE_RUNTIME_WHEEL_PATHS = (
    "omnivia_core/workspace/repository.py",
    "omnivia_core/workspace/service.py",
)

#: Run inside the throwaway environment, against the installed wheels only. Same
#: ``-I`` isolation as the scripts above. The workspace closure needs no
#: third-party package either: its repository reaches SQLite through the
#: standard library, and the ingestion extractors its service pulls in import
#: PyMuPDF and ``python-docx`` lazily.
INSTALLED_WORKSPACE_FACADE_SCRIPT = f"""
import sys

import omnivia_core.workspace
import omnivia_core.workspace.models
import omnivia_memory.workspace
import omnivia_memory.workspace.models

legacy_models = sys.modules["omnivia_memory.workspace.models"]
canonical_models = sys.modules["omnivia_core.workspace.models"]

for name in {WORKSPACE_MODELS_ROUTED!r}:
    assert getattr(legacy_models, name) is getattr(canonical_models, name), name

# Every module resolved from the installed distributions, not a source tree.
for module in (legacy_models, canonical_models):
    assert "site-packages" in module.__file__, module.__file__

# The hybrid barrel: the portable half is canonical on both barrels, the runtime
# half is legacy-only.
for name in {WORKSPACE_MODELS_ROUTED!r}:
    assert getattr(omnivia_memory.workspace, name) is getattr(canonical_models, name)
    assert getattr(omnivia_core.workspace, name) is getattr(canonical_models, name)
for name in {WORKSPACE_BARREL_RUNTIME_EXPORTS!r}:
    assert name in omnivia_memory.workspace.__all__, name
    assert not hasattr(omnivia_core.workspace, name), name
    assert name not in omnivia_core.workspace.__all__, name

# The routed records still round-trip and mutate through the canonical objects.
payload = {{
    "id": "ws-1",
    "name": "My Workspace",
    "root_path": "/tmp/root",
    "storage_path": "/tmp/storage",
    "created_at": "2024-01-01T00:00:00+00:00",
    "updated_at": "2024-01-01T00:00:00+00:00",
}}
workspace = legacy_models.Workspace.from_dict(payload)
assert canonical_models.Workspace.from_dict(workspace.to_dict()).to_dict() == (
    workspace.to_dict()
)
assert workspace.index_status is canonical_models.WorkspaceIndexStatus.UNINDEXED
workspace.mark_indexed()
assert workspace.index_status is canonical_models.WorkspaceIndexStatus.INDEXED
assert legacy_models.WorkspaceUpdate(name="Renamed").apply_to(workspace) is True
assert workspace.name == "Renamed"
summary = legacy_models.ImportSummary(
    workspace_id="ws-1", files_seen=1, sources_created=1, memories_created=1
)
assert isinstance(summary, canonical_models.ImportSummary)
assert summary.errors == []

# The workspace runtime is legacy-only: importable from the compatibility
# distribution, absent from Core.
import omnivia_memory.workspace.repository
import omnivia_memory.workspace.service

for name in ("repository", "service"):
    assert f"omnivia_core.workspace.{{name}}" not in sys.modules, name
    try:
        __import__(f"omnivia_core.workspace.{{name}}")
    except ImportError:
        pass
    else:
        raise AssertionError(f"omnivia_core.workspace.{{name}} is importable")

repository = sys.modules["omnivia_memory.workspace.repository"]
service = sys.modules["omnivia_memory.workspace.service"]
assert repository.Workspace is canonical_models.Workspace
assert repository.WorkspaceIndexStatus is canonical_models.WorkspaceIndexStatus
assert service.ImportSummary is canonical_models.ImportSummary
assert service.WorkspaceCreate is canonical_models.WorkspaceCreate
assert service.WorkspaceUpdate is canonical_models.WorkspaceUpdate
assert omnivia_memory.workspace.WorkspaceRepository is repository.WorkspaceRepository
assert omnivia_memory.workspace.WorkspaceService is service.WorkspaceService
print("OK")
"""


#: The six ``hybrid_facade`` barrels as the installed artifacts must present
#: them: for each, the portable exports that must be the exact canonical objects,
#: and the runtime exports that must be the exact legacy objects of a named
#: legacy owner. Restated here rather than imported from ``baseline`` or from
#: ``tests/compatibility/test_facade_foundation.py``, for the same reason every
#: other expectation in this file is: it checks *installed artifacts*, and
#: reading the expectation out of the source tree it is meant to be independent
#: of would weaken the check. 62 portable identities, 31 runtime ones.
HYBRID_BARREL_PORTABLE: dict[str, tuple[str, ...]] = {
    "graph": (
        "ApprovalStatus",
        "Entity",
        "EntityType",
        "GraphSearchQuery",
        "GraphSearchResult",
        "GraphSearchResultSet",
        "Relationship",
        "RelationshipType",
    ),
    "ingestion": ("Chunk", "ExtractionResult", "FileType", "ParseStatus", "Source"),
    "ingestion.watcher": WATCHER_MODELS_ROUTED,
    "memory": ("Memory", "MemoryCreate", "MemoryUpdate"),
    "memory_graph": (
        "Confidence",
        "EvidenceGraphResponse",
        "FIXTURE_TIME",
        "GraphPreviewEdge",
        "GraphPreviewKind",
        "GraphPreviewNode",
        "GraphPreviewResponse",
        "GraphPreviewState",
        "MemoryEntity",
        "MemoryFact",
        "MemoryFactStatus",
        "MemoryGraphFixture",
        "MemorySegment",
        "MemorySegmentKind",
        "MemorySource",
        "MemorySourceFreshness",
        "MemorySourceStatus",
        "MemorySourceType",
        "RetrievalTrace",
        "SourceRef",
        "ValidationResult",
        "assemble_evidence_graph",
        "assemble_graph_preview",
        "build_memory_graph_fixture",
        "redact_segment_preview",
        "validate_evidence_graph_response",
        "validate_graph_preview_response",
        "validate_memory_entity",
        "validate_memory_fact",
        "validate_memory_segment",
        "validate_memory_source",
    ),
    "workspace": WORKSPACE_MODELS_ROUTED,
}
HYBRID_BARREL_RUNTIME: dict[str, dict[str, tuple[str, ...]]] = {
    "graph": {
        "omnivia_memory.graph.search_service": (
            "GraphSearchError",
            "GraphSearchService",
        )
    },
    "ingestion": {
        "omnivia_memory.ingestion.chunker": (
            "BaseChunker",
            "CharacterChunker",
            "ChunkConfig",
            "ParagraphChunker",
        ),
        "omnivia_memory.ingestion.extractors": (
            "BaseExtractor",
            "DOCXExtractor",
            "MarkdownExtractor",
            "PDFExtractor",
        ),
        "omnivia_memory.ingestion.pipeline": ("IngestResult", "IngestionPipeline"),
        "omnivia_memory.ingestion.repositories": ("ChunkRepository",),
        "omnivia_memory.ingestion.scanner": ("FileInfo", "FileScanner", "ScanOptions"),
    },
    "ingestion.watcher": {
        "omnivia_memory.ingestion.watcher.debouncer": ("Debouncer",),
        "omnivia_memory.ingestion.watcher.tracker": ("SourceTracker",),
    },
    "memory": {
        "omnivia_memory.memory.service": (
            "InvalidTransitionError",
            "MemoryNotFoundError",
            "MemoryService",
            "MemoryServiceError",
        )
    },
    "memory_graph": {
        "omnivia_memory.memory_graph.ingestion_adapter": (
            "IngestionGraphAdapterError",
            "IngestionGraphWriteResult",
            "chunk_to_memory_segment",
            "source_to_memory_source",
            "write_ingestion_records_to_graph",
        ),
        "omnivia_memory.memory_graph.store": (
            "MemoryGraphStore",
            "MemoryGraphStoreError",
        ),
    },
    "workspace": {
        "omnivia_memory.workspace.repository": ("WorkspaceRepository",),
        "omnivia_memory.workspace.service": ("WorkspaceService",),
    },
}
HYBRID_PORTABLE_COUNT = 62
HYBRID_RUNTIME_COUNT = 31

#: Every runtime-owned module behind the six barrels, as the path it would have
#: in the Core wheel if it had wrongly been packaged there. The graph, ingestion
#: and workspace halves are already pinned above; these are the three the memory
#: and memory-graph barrels add.
HYBRID_RUNTIME_WHEEL_PATHS = (
    "omnivia_core/memory/service.py",
    "omnivia_core/memory_graph/ingestion_adapter.py",
    "omnivia_core/memory_graph/store.py",
)

#: Run inside the throwaway environment, against the installed wheels only. Same
#: ``-I`` isolation as the scripts above; the whole hybrid closure is
#: standard-library only, so no uninstalled third-party dependency is reached.
INSTALLED_HYBRID_BARRELS_SCRIPT = f"""
import importlib
import sys

portable = {HYBRID_BARREL_PORTABLE!r}
runtime = {HYBRID_BARREL_RUNTIME!r}

portable_checked = 0
runtime_checked = 0
for suffix, names in sorted(portable.items()):
    legacy = importlib.import_module("omnivia_memory." + suffix)
    canonical = importlib.import_module("omnivia_core." + suffix)
    assert "site-packages" in legacy.__file__, legacy.__file__
    assert "site-packages" in canonical.__file__, canonical.__file__
    for name in names:
        assert getattr(legacy, name) is getattr(canonical, name), (suffix, name)
        assert name in legacy.__all__ and name in canonical.__all__, (suffix, name)
        portable_checked += 1
    assert "__getattr__" not in vars(legacy) and "__getattr__" not in vars(canonical)
    assert "__dir__" not in vars(legacy) and "__dir__" not in vars(canonical)

for suffix, owners in sorted(runtime.items()):
    legacy = importlib.import_module("omnivia_memory." + suffix)
    canonical = importlib.import_module("omnivia_core." + suffix)
    for owner_name, names in sorted(owners.items()):
        owner = importlib.import_module(owner_name)
        assert "site-packages" in owner.__file__, owner.__file__
        canonical_twin = owner_name.replace("omnivia_memory", "omnivia_core", 1)
        try:
            importlib.import_module(canonical_twin)
        except ImportError:
            pass
        else:
            raise AssertionError(canonical_twin + " is importable from Core")
        for name in names:
            assert getattr(legacy, name) is getattr(owner, name), (suffix, name)
            assert name in legacy.__all__, (suffix, name)
            assert not hasattr(canonical, name), (suffix, name)
            assert name not in canonical.__all__, (suffix, name)
            runtime_checked += 1

assert portable_checked == {HYBRID_PORTABLE_COUNT}, portable_checked
assert runtime_checked == {HYBRID_RUNTIME_COUNT}, runtime_checked
print("OK")
"""

#: Every name the compatibility root either advertises or keeps as a hidden
#: binding, minus ``__version__`` -- the one name both roots legitimately share.
#: The canonical root may publish none of them: it is version-only, and the
#: compatibility root aggregates its surface from eleven owners precisely because
#: there is no single canonical re-export to have taken it from.
ROOT_ADVERTISED_AND_HIDDEN = tuple(
    sorted({*ROOT_FACADE_ALL, *ROOT_FACADE_HIDDEN_BINDINGS} - {"__version__"})
)

#: Run inside a *second* throwaway environment that has only the Core wheel
#: installed. Nothing here may need ``omnivia-memory`` to exist at all.
CORE_ONLY_BARRELS_SCRIPT = f"""
import importlib
import sys

CHECKOUT = {str(REPO_ROOT)!r}

try:
    importlib.import_module("omnivia_memory")
except ImportError:
    pass
else:
    raise AssertionError("omnivia_memory is installed in the Core-only environment")

# The canonical package root, in the installed Core wheel and with the
# compatibility distribution absent. `test_root_facade.py` makes the same claims
# about the source checkout; this is the artifact half, and it is the only place
# the root is checked with nothing of `omnivia_memory` on disk to lean on.
canonical_root = importlib.import_module("omnivia_core")
assert "site-packages" in canonical_root.__file__, canonical_root.__file__
assert not canonical_root.__file__.startswith(CHECKOUT), canonical_root.__file__

# Version-only, and no dynamic hook widening that.
assert list(canonical_root.__all__) == {list(CANONICAL_ROOT_ALL)!r}, list(
    canonical_root.__all__
)
assert canonical_root.__version__ == {EXPECTED_VERSION!r}, canonical_root.__version__
assert "__getattr__" not in vars(canonical_root)
assert "__dir__" not in vars(canonical_root)

# None of the compatibility root's 186 advertised-or-hidden names leaks in --
# neither the 182 portable contracts nor the four hidden bindings, two of which
# (`Database`, `MemoryService`) are runtime-owned and must have no canonical
# counterpart at all.
root_names = {list(ROOT_ADVERTISED_AND_HIDDEN)!r}
assert len(root_names) == 186, len(root_names)
leaked_root = sorted(name for name in root_names if hasattr(canonical_root, name))
assert not leaked_root, leaked_root
public_root = sorted(
    name
    for name, value in vars(canonical_root).items()
    if not name.startswith("_") and type(value).__name__ != "module"
)
assert public_root == ["annotations"], public_root

# ...and it carries no runtime-owned legacy dependency: the installed source
# never names the legacy package, so nothing here could reach back into it.
with open(canonical_root.__file__, encoding="utf-8") as handle:
    assert "omnivia_memory" not in handle.read(), (
        "the canonical root names the legacy package"
    )

runtime_names = set()
for owners in {HYBRID_BARREL_RUNTIME!r}.values():
    for names in owners.values():
        runtime_names.update(names)
assert len(runtime_names) == {HYBRID_RUNTIME_COUNT}, sorted(runtime_names)

for suffix, names in sorted({HYBRID_BARREL_PORTABLE!r}.items()):
    canonical = importlib.import_module("omnivia_core." + suffix)
    assert "site-packages" in canonical.__file__, canonical.__file__
    for name in names:
        assert hasattr(canonical, name), (suffix, name)
        assert name in canonical.__all__, (suffix, name)
    leaked = sorted(name for name in runtime_names if hasattr(canonical, name))
    assert not leaked, (suffix, leaked)

assert "omnivia_memory" not in sys.modules
for name in sorted(sys.modules):
    assert not name.startswith("omnivia_memory"), name
print("OK")
"""


def test_compatibility_wheel_declares_core_requirement_and_both_wheels_install_offline() -> None:
    with tempfile.TemporaryDirectory(prefix="omnivia-facade-wheel-") as raw_workdir:
        workdir = Path(raw_workdir)
        wheelhouse = workdir / "wheelhouse"
        wheelhouse.mkdir()

        core_wheel = _build_wheel(CORE_DIR, wheelhouse)
        memory_wheel = _build_wheel(MEMORY_DIR, wheelhouse)
        assert core_wheel.name.startswith("omnivia_core-")
        assert memory_wheel.name.startswith("omnivia_memory-")

        _assert_declares_exactly_core_and_sqlalchemy(
            _requires_dist(_wheel_metadata(memory_wheel)),
            source=f"the built {memory_wheel.name} METADATA",
        )

        # Offline artifact install: a throwaway venv, no index, no resolution --
        # only the two wheels just built from this checkout.
        venv_dir = workdir / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        venv_python = venv_dir / "bin" / "python"

        install = subprocess.run(
            [
                str(venv_python), "-m", "pip", "install",
                "--no-index", "--no-deps", str(core_wheel), str(memory_wheel),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_child_env(),
        )
        assert install.returncode == 0, f"{install.stdout}\n{install.stderr}"

        # ``-I`` so the child ignores PYTHONPATH/user site and its own cwd:
        # what it reports comes from the installed dist-info, nothing else.
        report = subprocess.run(
            [str(venv_python), "-I", "-c", INSTALLED_METADATA_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert report.returncode == 0, f"{report.stdout}\n{report.stderr}"
        installed = json.loads(report.stdout)

        assert installed["core_version"] == EXPECTED_VERSION
        assert installed["memory_version"] == EXPECTED_VERSION

        # The requirement survives the build-and-install round trip: the
        # installed metadata, not just the wheel's, still carries the pin.
        _assert_declares_exactly_core_and_sqlalchemy(
            installed["memory_requires"],
            source="the installed omnivia-memory metadata",
        )

        # Explicitly not a resolution proof: --no-deps left the declared
        # SQLAlchemy dependency uninstalled, which is why the import check
        # below stays inside the Graph and ingestion closures, which need no
        # third-party package at all.
        assert installed["sqlalchemy_installed"] is False

        # The runtime-owned Graph, ingestion and workspace modules were never
        # packaged into Core.
        with zipfile.ZipFile(core_wheel) as core_archive:
            core_names = set(core_archive.namelist())
            for path in (
                *GRAPH_RUNTIME_WHEEL_PATHS,
                *INGESTION_RUNTIME_WHEEL_PATHS,
                *WORKSPACE_RUNTIME_WHEEL_PATHS,
                *HYBRID_RUNTIME_WHEEL_PATHS,
            ):
                assert path not in core_names, (
                    f"{core_wheel.name} packages the runtime-owned {path}"
                )
            # ...and the three contract leaves really are in it, so the absences
            # above are a partition rather than a missing subtree.
            for path in (
                "omnivia_core/ingestion/models.py",
                "omnivia_core/ingestion/watcher/models.py",
                "omnivia_core/workspace/models.py",
            ):
                assert path in core_names, (
                    f"{core_wheel.name} is missing the canonical {path}"
                )
            canonical_records = core_archive.read(
                "omnivia_core/graph/search_models.py"
            ).decode("utf-8")
        for helper in GRAPH_RETAINED_HELPERS:
            assert f"def {helper}" not in canonical_records, (
                f"{core_wheel.name} packages the runtime-owned helper {helper}"
            )

        # The split facade holds in the installed artifacts, not only in the
        # source tree: exact portable identities, working legacy helpers, and no
        # helper or Graph runtime module inside Core.
        graph = subprocess.run(
            [str(venv_python), "-I", "-c", INSTALLED_GRAPH_FACADE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert graph.returncode == 0, f"{graph.stdout}\n{graph.stderr}"
        assert graph.stdout.strip() == "OK"

        # The same for the ingestion pair: exact routed identities under two
        # hybrid barrels, an intact ``IngestSource`` alias, a legacy-only runtime,
        # and the watcher tracker's distinct same-named record.
        ingestion = subprocess.run(
            [str(venv_python), "-I", "-c", INSTALLED_INGESTION_FACADE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert ingestion.returncode == 0, f"{ingestion.stdout}\n{ingestion.stderr}"
        assert ingestion.stdout.strip() == "OK"

        # The same for the workspace leaf: exact routed identities under the sixth
        # hybrid barrel, a legacy-only repository/service, and both of those still
        # holding the canonical models.
        workspace = subprocess.run(
            [str(venv_python), "-I", "-c", INSTALLED_WORKSPACE_FACADE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert workspace.returncode == 0, f"{workspace.stdout}\n{workspace.stderr}"
        assert workspace.stdout.strip() == "OK"

        # All six hybrid barrels at once, in the installed artifacts: 62 portable
        # identities are the exact canonical objects and 31 runtime identities are
        # the exact legacy ones, with no runtime name on any canonical barrel and
        # no canonical twin for any runtime owner. This is the accounting the
        # ``hybrid_facade`` state claims, checked where it actually ships.
        hybrid = subprocess.run(
            [str(venv_python), "-I", "-c", INSTALLED_HYBRID_BARRELS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert hybrid.returncode == 0, f"{hybrid.stdout}\n{hybrid.stderr}"
        assert hybrid.stdout.strip() == "OK"

        # ...and the canonical half stands on its own. A second environment with
        # only the Core wheel installed: every canonical barrel imports, every
        # portable name is there, no runtime name is, the canonical package root
        # is version-only and publishes none of the compatibility root's 186
        # advertised-or-hidden names, and ``omnivia_memory`` is not merely
        # unimported but absent. The bounded ``omnivia-memory -> omnivia-core``
        # dependency has no reverse.
        core_only_dir = workdir / "core-only-venv"
        venv.EnvBuilder(with_pip=True).create(core_only_dir)
        core_only_python = core_only_dir / "bin" / "python"
        core_install = subprocess.run(
            [
                str(core_only_python), "-m", "pip", "install",
                "--no-index", "--no-deps", str(core_wheel),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_child_env(),
        )
        assert core_install.returncode == 0, (
            f"{core_install.stdout}\n{core_install.stderr}"
        )
        core_only = subprocess.run(
            [str(core_only_python), "-I", "-c", CORE_ONLY_BARRELS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert core_only.returncode == 0, f"{core_only.stdout}\n{core_only.stderr}"
        assert core_only.stdout.strip() == "OK"

        # The Core wheel declares no dependency on the compatibility distribution.
        core_requires = [
            Requirement(raw) for raw in _requires_dist(_wheel_metadata(core_wheel))
        ]
        assert not any(req.name == "omnivia-memory" for req in core_requires), (
            f"{core_wheel.name} declares a reverse dependency on omnivia-memory"
        )
