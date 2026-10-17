"""The Python code-analysis producer over one sealed captured snapshot
(TECH-CORE-RETRIEVAL-CODE-001, work item W10).

Every test here runs against a real migrated workspace, a really sealed `captured_v1`
snapshot and really published content-addressed blobs. There is no checkout on disk at
any point in this module -- the fixture repository exists only as bytes under a blob root
and rows in the capture index -- which is what makes "the producer never reads the live
checkout" an observable property of the suite rather than a claim in a docstring.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import test_engineering_captured_source_coverage as captured
import test_engineering_source_coverage as esc
from omnivia_core_runtime.storage import code_analysis_python as subject
from omnivia_core_runtime.storage.code_analysis_candidates import (
    CodeAnalysisBindingError,
    CodeAnalysisCandidateError,
    CodeEdgeCandidate,
    CodeNodeCandidate,
    revision_instance_id,
    to_memory_create_inputs,
)
from omnivia_core_runtime.storage.code_analysis_python import (
    DEFAULT_PYTHON_PROFILE,
    FILE_OUTCOMES,
    NODE_KINDS,
    RELATIONSHIP_TYPES,
    PythonAnalyserProfile,
    PythonAnalysisRefused,
    PythonAnalysisResult,
    analyse_python_snapshot,
    analysis_manifest,
)
from omnivia_core_runtime.workspace.blob_publication import blob_path, publish_blob

WORKSPACE_ID = esc.WORKSPACE_ID
SENTINEL = "PRODUCER_EXECUTED_SENTINEL"

# --- the fixture repository ---------------------------------------------------------
#
# A small but genuinely multi-file package: a re-exporting `__init__`, a model module
# with an async factory and a non-ASCII class, a service with a method chain and a
# dynamic dispatch, a helper that imports the service back (a cycle), a module that
# shadows and redefines names, a module whose relative import climbs past the root, and
# four files that are deliberately not analysable.

_INIT = '''"""Package surface."""

from . import service
from .models import Record

__all__ = ["Record", "service"]
'''

_MODELS = '''"""Stored records."""


class Record:
    """One stored record."""

    def __init__(self, key: str) -> None:
        self.key = key

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key}


class Café:
    """A class whose name is not ASCII."""

    def préparer(self) -> str:
        return "prêt"


async def load(key: str) -> Record:
    return Record(key)
'''

_SERVICE = '''"""The service."""

import json

from .models import Record
from .util import helper as aid


class Service:
    """Runs one step at a time."""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table

    def run(self, key: str) -> str:
        record = Record(key)
        self.step(record)
        return json.dumps(record.to_dict())

    def step(self, record: Record) -> None:
        aid(record)

    def nested(self) -> None:
        def inner() -> None:
            aid(None)

        inner()

    async def streamed(self, key: str) -> str:
        return self.run(key)


def dispatch(table: dict[str, object], key: str) -> object:
    return table[key]()
'''

_UTIL = '''"""Helpers."""

from . import service


def helper(record: object) -> None:
    """Reaches back into the service, which imports this module."""
    service.Service({}).run("k")
'''

_SHADOW = '''"""Shadowing and overload-like redefinition."""

import json


def json() -> str:
    return "shadowed"


def twice() -> str:
    return "first"


def twice() -> str:
    return "second"


def outer() -> str:
    def twice() -> str:
        return "inner"

    return twice()


def ambiguous_user() -> str:
    return twice()


def json_user() -> str:
    return json()
'''

#: Every Python binder that is *not* a definition or an import, each one shadowing a
#: module-level `def target` of the same name. Nothing here may resolve to that `def`.
_BINDERS = '''"""Binders that are not definitions, each shadowing an outer definition."""


def target() -> object:
    return "the outer definition"


class Holder:
    """Its methods reach their own class through the receiver -- until one rebinds it."""

    def kept(self) -> object:
        return self.target()

    def rebound(self, other: object) -> object:
        self = other
        return self.target()

    def target(self) -> object:
        return "a member"

    def missing(self) -> object:
        return self.absent()


def parameter_shadow(target: object) -> object:
    return target()


def assignment_shadow() -> object:
    target = object()
    return target()


def annotated_shadow() -> object:
    target: object = object()
    return target()


def augmented_shadow(seed: object) -> object:
    target = seed
    target += 1
    return target()


def loop_shadow(items: list[object]) -> list[object]:
    collected = []
    for target in items:
        collected.append(target())
    return collected


def comprehension_shadow(items: list[object]) -> list[object]:
    return [target() for target in items]


def lambda_shadow() -> object:
    return lambda target: target()


def with_shadow(opened: object) -> object:
    with opened as target:
        return target()


def except_shadow() -> object:
    try:
        return None
    except ValueError as target:
        return target()


def match_shadow(given: object) -> object:
    match given:
        case [target]:
            return target()
    return None


def walrus_shadow(seed: object) -> object:
    if (target := seed):
        return target()
    return None


def global_shadow() -> object:
    global target
    return target()


def comprehension_keeps_the_outer_name(items: list[object]) -> list[object]:
    return [target() for _ in items]


def lambda_keeps_the_outer_name() -> object:
    return lambda _: target()
'''

#: Every scope above whose `target()` must be dynamic, and the binder that makes it so.
BINDER_SHADOWS: dict[str, str] = {
    "pkg.binders.parameter_shadow": "a parameter",
    "pkg.binders.assignment_shadow": "an assignment",
    "pkg.binders.annotated_shadow": "an annotated assignment",
    "pkg.binders.augmented_shadow": "an augmented assignment",
    "pkg.binders.loop_shadow": "a loop target",
    "pkg.binders.comprehension_shadow": "a comprehension target",
    "pkg.binders.lambda_shadow": "a lambda parameter",
    "pkg.binders.with_shadow": "a with alias",
    "pkg.binders.except_shadow": "an except alias",
    "pkg.binders.match_shadow": "a pattern capture",
    "pkg.binders.walrus_shadow": "a walrus target",
    "pkg.binders.global_shadow": "a global declaration",
}

_DEEP = '''"""A relative import past the root, and a wildcard."""

from ... import nothing
from .models import *


def use() -> object:
    return Record("k")
'''

_SIDE_EFFECT = f'''"""If this module is ever imported or executed it leaves a sentinel behind."""

import pathlib

pathlib.Path("{SENTINEL}").write_text("executed", encoding="utf-8")

raise SystemExit("this module must never run")


def never() -> None:
    return None
'''

#: A latin-1 source with a PEP 263 cookie and a non-ASCII identifier: legal Python that
#: is not UTF-8, and therefore only analysable through `tokenize.detect_encoding`.
_LATIN = b'# -*- coding: latin-1 -*-\n\n\ndef caf\xe9() -> str:\n    return "ok"\n'
#: Undeclared, undecodable bytes: the failure the cookie above is not.
_MOJIBAKE = b'"""Doc."""\n\n\ndef broken() -> str:\n    return "\xff\xfe"\n'
_LEGACY = b"def broken(:\n    pass\n"

#: A module whose own *file name* is not ASCII, so the module path itself is Unicode.
_NON_ASCII_MODULE = '''"""A module whose own file name is not ASCII."""


def 服務() -> None:
    return None
'''

REPOSITORY: dict[str, bytes] = {
    "README.md": b"# not source\n",
    "pkg/__init__.py": _INIT.encode("utf-8"),
    "pkg/__pycache__/service.py": b"# a cache artefact that is not this repository's code\n",
    "pkg/deep.py": _DEEP.encode("utf-8"),
    "pkg/latin.py": _LATIN,
    "pkg/legacy.py": _LEGACY,
    "pkg/models.py": _MODELS.encode("utf-8"),
    "pkg/mojibake.py": _MOJIBAKE,
    "pkg/服務.py": _NON_ASCII_MODULE.encode("utf-8"),
    "pkg/service.py": _SERVICE.encode("utf-8"),
    "pkg/shadow.py": _SHADOW.encode("utf-8"),
    "pkg/sideeffect.py": _SIDE_EFFECT.encode("utf-8"),
    "pkg/util.py": _UTIL.encode("utf-8"),
}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class Snapshot:
    """One sealed captured snapshot whose bytes are really in a blob store."""

    def __init__(
        self,
        workspace: Any,
        blobs_root: Path,
        *,
        files: dict[str, bytes],
        snapshot_id: str = "snap-python-1",
        capture_status: str = "complete",
        base_us: int = 1_700_000_000_000_000,
        publish: bool = True,
    ) -> None:
        self.workspace = workspace
        self.blobs_root = blobs_root
        self.contents = dict(files)
        self.digests = {path: _digest(content) for path, content in files.items()}
        if publish:
            for path, content in files.items():
                publish_blob(blobs_root, self.digests[path], content)
        self.sealed = captured._seal(
            workspace,
            repository_id="repo-python",
            stream_id="stream-python",
            principal_id="core-service",
            checkout_id="checkout-python",
            snapshot_id=snapshot_id,
            files=self.digests,
            capture_status=capture_status,
            base_us=base_us,
        )

    @property
    def connection(self) -> Any:
        return self.workspace.holder.connection

    def analyse(self, **overrides: Any) -> PythonAnalysisResult:
        arguments: dict[str, Any] = {
            "workspace_id": WORKSPACE_ID,
            "repository_id": self.sealed.repository_id,
            "snapshot_id": self.sealed.snapshot_id,
            "blobs_root": self.blobs_root,
        }
        arguments.update(overrides)
        return analyse_python_snapshot(self.connection, **arguments)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Any]:
    opened = esc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


@pytest.fixture
def blobs_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


@pytest.fixture
def snapshot(workspace: Any, blobs_root: Path) -> Snapshot:
    return Snapshot(workspace, blobs_root, files=REPOSITORY)


def _nodes(result: PythonAnalysisResult) -> dict[str, CodeNodeCandidate]:
    batch = result.batch
    assert batch is not None
    return {node.semantic_key: node for node in batch.nodes}


def _edges(result: PythonAnalysisResult) -> tuple[CodeEdgeCandidate, ...]:
    batch = result.batch
    assert batch is not None
    return batch.edges


def _outcomes(result: PythonAnalysisResult) -> dict[str, str]:
    return {report.relative_path: report.outcome for report in result.reports}


def _edge(
    result: PythonAnalysisResult,
    *,
    relationship: str,
    source_key: str,
    predicate: Callable[[CodeEdgeCandidate], bool],
) -> CodeEdgeCandidate:
    matches = [
        edge
        for edge in _edges(result)
        if edge.relationship_type == relationship
        and edge.source_key == source_key
        and predicate(edge)
    ]
    assert len(matches) == 1, [edge.as_document() for edge in matches]
    return matches[0]


def _targets(result: PythonAnalysisResult, *, relationship: str, source_key: str) -> set[str]:
    """Every endpoint one scope asserts, resolved keys and descriptors alike."""
    return {
        edge.target_key
        if edge.target_key is not None
        else f"{edge.resolution_status}/{edge.unresolved_target.descriptor}"  # type: ignore[union-attr]
        for edge in _edges(result)
        if edge.relationship_type == relationship and edge.source_key == source_key
    }


# --- nodes --------------------------------------------------------------------------


def test_every_named_lexical_scope_becomes_a_revision_bound_node(snapshot: Snapshot) -> None:
    result = snapshot.analyse()
    nodes = _nodes(result)
    batch = result.batch
    assert batch is not None

    # Modules, classes, functions, async functions, methods, async methods and a
    # function nested inside a method all appear, each keyed by its lexical path.
    assert nodes["pkg.models"].kind == "module"
    assert nodes["pkg.models.Record"].kind == "class"
    assert nodes["pkg.models.Record.to_dict"].kind == "method"
    assert nodes["pkg.models.load"].kind == "async_function"
    assert nodes["pkg.service.Service.streamed"].kind == "async_method"
    assert nodes["pkg.service.dispatch"].kind == "function"
    assert nodes["pkg.service.Service.nested.inner"].kind == "function"
    assert {node.kind for node in batch.nodes} <= NODE_KINDS

    # Unicode identifiers are carried by their own names, not by a mangling of them,
    # in both the key and the display name -- and so is a non-ASCII module path.
    assert nodes["pkg.models.Café"].display_name == "Café"
    assert nodes["pkg.models.Café.préparer"].kind == "method"
    assert nodes["pkg.服務.服務"].display_name == "服務"
    assert nodes["pkg.latin.café"].display_name == "café"

    # The locator is exact, and column coordinates are one-based UTF-8 byte offsets.
    source = _MODELS.encode("utf-8").split(b"\n")
    to_dict = nodes["pkg.models.Record.to_dict"]
    assert source[to_dict.source.locator.start_line - 1].startswith(b"    def to_dict")
    assert to_dict.source.locator.start_column == 5
    accent = nodes["pkg.models.Café.préparer"].source.locator
    assert source[accent.end_line - 1][accent.end_column - 2 : accent.end_column - 1] == b'"'

    # Every node is bound to the exact captured bytes it was read from.
    for node in batch.nodes:
        assert node.source.content_digest == snapshot.digests[node.source.relative_path]

    # The profile's identity travels on the batch, and inside its digest.
    assert batch.profile_digest == DEFAULT_PYTHON_PROFILE.digest
    assert batch.producer_id == "analyzer.python.stdlib"


def test_the_batch_is_proven_against_the_sealed_snapshot_it_names(snapshot: Snapshot) -> None:
    result = snapshot.analyse()
    verified = result.verified
    assert verified is not None
    assert verified.stream_id == snapshot.sealed.stream_id
    assert verified.manifest_digest == snapshot.sealed.rich_manifest_digest
    assert verified.manifest_evidence_id == snapshot.sealed.evidence_id
    assert verified.batch.snapshot_kind == "working_tree"
    # Proof is over the sealed manifest, so every bound path is a captured path.
    assert {node.source.relative_path for node in verified.batch.nodes} <= set(snapshot.digests)


def test_candidate_identities_are_bound_to_the_revision(workspace: Any, blobs_root: Path) -> None:
    first = Snapshot(workspace, blobs_root, files=REPOSITORY, snapshot_id="snap-a")
    second = Snapshot(
        workspace,
        blobs_root,
        files={**REPOSITORY, "pkg/extra.py": b'"""Extra."""\n\n\ndef extra() -> None:\n    return None\n'},
        snapshot_id="snap-b",
        base_us=1_700_000_100_000_000,
    )
    one, two = first.analyse(), second.analyse()
    assert one.batch is not None and two.batch is not None
    # Same producer, same profile, different revision: different batch identity.
    assert one.batch.digest != two.batch.digest
    assert "pkg.extra" in _nodes(two) and "pkg.extra" not in _nodes(one)
    # The facts the two snapshots share are byte-identical, so a consumer can diff them.
    shared = set(_nodes(one)) & set(_nodes(two))
    assert len(shared) > 20
    for key in shared:
        assert _nodes(one)[key].as_document() == _nodes(two)[key].as_document()


def test_one_symbol_whose_bytes_changed_is_a_different_bound_instance(
    workspace: Any, blobs_root: Path
) -> None:
    # The same definition at the same place in two revisions of one file. The semantic key
    # is the stable identity; the bound digest and the revision instance are not, which is
    # what makes a candidate a statement about bytes rather than about a name.
    edited = _MODELS.replace('return {"key": self.key}', 'return {"key": self.key, "v": "2"}')
    before = Snapshot(workspace, blobs_root, files={"pkg/models.py": _MODELS.encode("utf-8")})
    after = Snapshot(
        workspace,
        blobs_root,
        files={"pkg/models.py": edited.encode("utf-8")},
        snapshot_id="snap-edited",
        base_us=1_700_000_400_000_000,
    )
    one, two = _nodes(before.analyse()), _nodes(after.analyse())
    key = "pkg.models.Record.to_dict"
    assert key in one and key in two
    assert one[key].source.content_digest != two[key].source.content_digest
    assert one[key].source.content_digest == before.digests["pkg/models.py"]
    assert two[key].source.content_digest == after.digests["pkg/models.py"]
    assert revision_instance_id(
        "repo-python", before.sealed.snapshot_id, key, one[key].source
    ) != revision_instance_id("repo-python", after.sealed.snapshot_id, key, two[key].source)
    # A definition that does not *contain* the edit keeps its exact locator across the
    # two; `Record`'s own span legitimately moves, because the edited line is inside it.
    assert one["pkg.models.Café"].source.locator == two["pkg.models.Café"].source.locator
    assert one["pkg.models.Record"].source.locator != two["pkg.models.Record"].source.locator


# --- determinism --------------------------------------------------------------------


def test_output_is_identical_across_runs_and_independent_of_stored_row_order(
    workspace: Any, blobs_root: Path
) -> None:
    snapshot = Snapshot(workspace, blobs_root, files=REPOSITORY)
    first, second = snapshot.analyse(), snapshot.analyse()
    assert first.batch is not None and second.batch is not None
    assert first.batch.digest == second.batch.digest
    assert analysis_manifest(first) == analysis_manifest(second)

    # A second snapshot of the same bytes whose index rows were written in reverse path
    # order. The producer sorts the index itself, so the candidate documents agree
    # exactly; only the snapshot identity differs.
    reversed_files = dict(reversed(list(REPOSITORY.items())))
    mirrored = Snapshot(
        workspace,
        blobs_root,
        files=reversed_files,
        snapshot_id="snap-mirror",
        base_us=1_700_000_200_000_000,
    )
    third = mirrored.analyse()
    assert third.batch is not None
    assert sorted(
        node.as_document()["semantic_key"] for node in first.batch.nodes  # type: ignore[misc]
    ) == sorted(node.as_document()["semantic_key"] for node in third.batch.nodes)  # type: ignore[misc]
    assert [report.as_document() for report in first.reports] == [
        report.as_document() for report in third.reports
    ]
    assert {edge.as_document()["edge_key"] for edge in first.batch.edges} == {
        edge.as_document()["edge_key"] for edge in third.batch.edges
    }


# --- containment, imports, calls ----------------------------------------------------


def test_containment_edges_follow_the_lexical_scope_chain(snapshot: Snapshot) -> None:
    result = snapshot.analyse()
    defines = {
        (edge.source_key, edge.target_key)
        for edge in _edges(result)
        if edge.relationship_type == "defines"
    }
    assert ("pkg.models", "pkg.models.Record") in defines
    assert ("pkg.models.Record", "pkg.models.Record.to_dict") in defines
    assert ("pkg.service.Service.nested", "pkg.service.Service.nested.inner") in defines
    # A module is nobody's child, so it is never a containment target.
    assert not any(target == "pkg.models" for _source, target in defines)
    assert all(
        edge.resolution_status == "resolved" and edge.evidence_strength == "syntactic"
        for edge in _edges(result)
        if edge.relationship_type == "defines"
    )
    assert {edge.relationship_type for edge in _edges(result)} <= RELATIONSHIP_TYPES


def test_imports_resolve_through_aliases_relative_paths_and_cycles(snapshot: Snapshot) -> None:
    result = snapshot.analyse()

    # `from .models import Record` reaches the class itself, not merely its module.
    assert (
        _edge(
            result,
            relationship="imports",
            source_key="pkg.service",
            predicate=lambda edge: edge.target_key == "pkg.models.Record",
        ).evidence_strength
        == "resolved_static"
    )
    # `from .util import helper as aid` resolves past the alias to the definition.
    _edge(
        result,
        relationship="imports",
        source_key="pkg.service",
        predicate=lambda edge: edge.target_key == "pkg.util.helper",
    )
    # `from . import service` in a package's own `__init__` means `pkg.service`.
    _edge(
        result,
        relationship="imports",
        source_key="pkg",
        predicate=lambda edge: edge.target_key == "pkg.service",
    )
    # A cycle is two ordinary resolved edges; nothing recurses and nothing is dropped.
    _edge(
        result,
        relationship="imports",
        source_key="pkg.util",
        predicate=lambda edge: edge.target_key == "pkg.service",
    )
    # A module outside the snapshot is external and named, never invented.
    external = _edge(
        result,
        relationship="imports",
        source_key="pkg.service",
        predicate=lambda edge: edge.resolution_status == "external",
    )
    assert external.target_key is None
    assert external.unresolved_target is not None
    assert external.unresolved_target.descriptor == "external-reference:json"

    # A relative import that climbs past the snapshot root, and a wildcard, are both
    # refused explicitly rather than resolved to whatever happens to be at the top.
    assert "unresolved/relative-import-beyond-root:...nothing" in _targets(
        result, relationship="imports", source_key="pkg.deep"
    )
    assert "unresolved/wildcard-import:pkg.models" in _targets(
        result, relationship="imports", source_key="pkg.deep"
    )


def test_calls_resolve_only_where_the_source_statically_supports_it(snapshot: Snapshot) -> None:
    result = snapshot.analyse()

    # `self.step(...)`: the receiver is this method's own first parameter and the method
    # is written in a class body, so the target is that class's member.
    assert (
        _edge(
            result,
            relationship="calls",
            source_key="pkg.service.Service.run",
            predicate=lambda edge: edge.target_key == "pkg.service.Service.step",
        ).evidence_strength
        == "resolved_static"
    )
    # An imported name, an aliased import, and a closure-visible nested definition.
    assert "pkg.models.Record" in _targets(
        result, relationship="calls", source_key="pkg.service.Service.run"
    )
    assert "pkg.util.helper" in _targets(
        result, relationship="calls", source_key="pkg.service.Service.step"
    )
    assert "pkg.util.helper" in _targets(
        result, relationship="calls", source_key="pkg.service.Service.nested.inner"
    )
    assert "pkg.service.Service.nested.inner" in _targets(
        result, relationship="calls", source_key="pkg.service.Service.nested"
    )
    # A module imported as a whole, then called through: `service.Service({})`.
    assert "pkg.service.Service" in _targets(
        result, relationship="calls", source_key="pkg.util.helper"
    )

    # Everything the source does not statically determine is labelled, never guessed.
    run_targets = _targets(result, relationship="calls", source_key="pkg.service.Service.run")
    assert "external/external-reference:json.dumps" in run_targets
    # `record` is a local, and a local is not the outer definition it shares a spelling
    # with. The producer follows no value, so it says the name is bound and unknown.
    assert "unresolved/dynamic-binding:record" in run_targets
    assert "unresolved/dynamic-call:Subscript" in _targets(
        result, relationship="calls", source_key="pkg.service.dispatch"
    )
    assert "unresolved/dynamic-call:Attribute.run" in _targets(
        result, relationship="calls", source_key="pkg.util.helper"
    )
    dynamic = _edge(
        result,
        relationship="calls",
        source_key="pkg.service.dispatch",
        predicate=lambda edge: edge.resolution_status == "unresolved",
    )
    assert dynamic.evidence_strength == "inferred_dynamic"
    assert dynamic.target_key is None


def test_shadowing_and_overload_like_redefinition_are_stated_not_guessed(
    snapshot: Snapshot,
) -> None:
    result = snapshot.analyse()
    nodes = _nodes(result)

    # Two `def twice` in one scope are two source-supported definitions, so both are
    # emitted; the second takes an ordinal so the batch keeps unique identities.
    assert nodes["pkg.shadow.twice"].source.locator.start_line < (
        nodes["pkg.shadow.twice+2"].source.locator.start_line
    )
    # ... and a call to that name cannot say which, so it is ambiguous rather than
    # silently bound to the first or last definition.
    assert _targets(result, relationship="calls", source_key="pkg.shadow.ambiguous_user") == {
        "ambiguous/ambiguous-definition:pkg.shadow.twice"
    }
    # The same name redefined in a nested scope shadows it, and resolves there.
    assert _targets(result, relationship="calls", source_key="pkg.shadow.outer") == {
        "pkg.shadow.outer.twice"
    }
    # An import and a definition binding one name in one scope is ambiguous too.
    assert _targets(result, relationship="calls", source_key="pkg.shadow.json_user") == {
        "ambiguous/ambiguous-name:json"
    }
    # A name only a wildcard import could have provided is ambiguous, not unbound.
    assert _targets(result, relationship="calls", source_key="pkg.deep.use") == {
        "ambiguous/wildcard-shadowed-name:Record"
    }


def test_a_name_bound_by_anything_but_a_definition_never_resolves_to_an_outer_one(
    workspace: Any, blobs_root: Path
) -> None:
    """Python makes an assigned name local to the whole of its scope, at compile time.

    So a call through one is never the outer definition it happens to share a spelling
    with, whatever order the two appear in. What the name *does* hold needs value flow
    this producer has not got, so every such call is `inferred_dynamic` and unresolved
    rather than resolved to the definition that was easiest to reach.
    """
    result = Snapshot(workspace, blobs_root, files={"pkg/binders.py": _BINDERS.encode()}).analyse()
    assert _outcomes(result)["pkg/binders.py"] == "analysed"
    assert "pkg.binders.target" in _nodes(result)

    for scope, binder in BINDER_SHADOWS.items():
        assert "unresolved/dynamic-binding:target" in _targets(
            result, relationship="calls", source_key=scope
        ), binder
        assert "pkg.binders.target" not in _targets(
            result, relationship="calls", source_key=scope
        ), binder

    # Every one of them carries the strength that says why, and names no target.
    dynamic = [
        edge
        for edge in _edges(result)
        if edge.unresolved_target is not None
        and edge.unresolved_target.descriptor.startswith("dynamic-binding:")
    ]
    assert len(dynamic) >= len(BINDER_SHADOWS)
    assert {edge.evidence_strength for edge in dynamic} == {"inferred_dynamic"}
    assert {edge.resolution_status for edge in dynamic} == {"unresolved"}
    assert all(edge.target_key is None for edge in dynamic)
    assert all(len(edge.unresolved_target.descriptor) <= 1024 for edge in dynamic)  # type: ignore[union-attr]

    # A name a builtin provides is still `unbound-name`: it genuinely is not in the
    # snapshot, which is a different fact from a local this analyser cannot follow.
    assert "unresolved/unbound-name:object" in _targets(
        result, relationship="calls", source_key="pkg.binders.assignment_shadow"
    )


def test_a_transient_scope_shadows_only_what_it_binds(
    workspace: Any, blobs_root: Path
) -> None:
    # The other half of the rule: a lambda and a comprehension are scopes, so they must
    # not leak *out* either. A name neither of them binds still reaches the module.
    result = Snapshot(workspace, blobs_root, files={"pkg/binders.py": _BINDERS.encode()}).analyse()
    for scope in (
        "pkg.binders.comprehension_keeps_the_outer_name",
        "pkg.binders.lambda_keeps_the_outer_name",
    ):
        assert "pkg.binders.target" in _targets(result, relationship="calls", source_key=scope)

    # And neither is a node or an edge endpoint of its own: they define nothing, so the
    # edges written inside them belong to the named scope that owns the text.
    assert not [key for key in _nodes(result) if "\x00" in key]
    assert {edge.source_key for edge in _edges(result)} <= set(_nodes(result))
    assert "pkg.binders.lambda_shadow" in {
        edge.source_key
        for edge in _edges(result)
        if edge.unresolved_target is not None
        and edge.unresolved_target.descriptor == "dynamic-binding:target"
    }


def test_the_method_receiver_resolves_until_the_body_rebinds_it(
    workspace: Any, blobs_root: Path
) -> None:
    # The receiver is the one parameter whose value is lexically knowable: it is the
    # instance of the class the method is written in. That is a binding, not a guess --
    # and it is the binding only while the body leaves it alone.
    result = Snapshot(workspace, blobs_root, files={"pkg/binders.py": _BINDERS.encode()}).analyse()
    assert _targets(result, relationship="calls", source_key="pkg.binders.Holder.kept") == {
        "pkg.binders.Holder.target"
    }
    assert _targets(result, relationship="calls", source_key="pkg.binders.Holder.rebound") == {
        "unresolved/dynamic-binding:self"
    }
    # Base classes are not walked -- that needs an MRO -- so a member the lexically
    # enclosing class does not define is inside the snapshot and not found, which is a
    # different and more useful fact than a name nothing binds.
    assert _targets(result, relationship="calls", source_key="pkg.binders.Holder.missing") == {
        "unresolved/unresolved-in-snapshot:pkg.binders.Holder.absent"
    }


def test_a_file_the_contract_cannot_key_contributes_nothing_at_all(
    workspace: Any, blobs_root: Path
) -> None:
    """The per-file outcome is all-or-nothing, including what parsed before the break.

    `pkg/wide.py` holds a perfectly ordinary definition and then one whose logical path
    is past the identifier budget. Reporting the file as `identity_unsupported` while
    keeping the facts gathered before that point would describe a file the report says
    contributed none -- and would leave another file resolving an import to a node that
    is not in the batch.
    """
    long_name = "n" * 300
    files = {
        "pkg/__init__.py": b'"""Package."""\n',
        "pkg/wide.py": (
            '"""Wide."""\n\n\nimport json\n\n\ndef fine() -> None:\n    return None\n\n\n'
            f'def {long_name}() -> None:\n    fine()\n'
        ).encode(),
        "pkg/caller.py": (
            b'"""Caller."""\n\nfrom .wide import fine\n\n\ndef call() -> None:\n    fine()\n'
        ),
    }
    result = Snapshot(workspace, blobs_root, files=files).analyse()
    assert _outcomes(result)["pkg/wide.py"] == "identity_unsupported"

    # Not one node, and not one edge of any relationship, is sourced from that file.
    nodes = _nodes(result)
    assert "pkg.wide" not in nodes and "pkg.wide.fine" not in nodes
    assert not [key for key in nodes if key.startswith("pkg.wide")]
    assert not [edge for edge in _edges(result) if edge.source.relative_path == "pkg/wide.py"]
    assert not [edge for edge in _edges(result) if edge.source_key.startswith("pkg.wide")]
    # Which includes the containment and import facts it had already gathered.
    assert not [
        edge for edge in _edges(result) if edge.relationship_type in {"defines", "imports"}
        and edge.source.relative_path == "pkg/wide.py"
    ]

    # Every other file survives it, and its references into the dropped file say exactly
    # what happened rather than resolving to something that is no longer there.
    assert set(nodes) == {"pkg", "pkg.caller", "pkg.caller.call"}
    assert _targets(result, relationship="imports", source_key="pkg.caller") == {
        "unresolved/unsupported-identity:pkg.wide.fine"
    }
    assert _targets(result, relationship="calls", source_key="pkg.caller.call") == {
        "unresolved/unsupported-identity:pkg.wide.fine"
    }


def test_resolved_edges_only_ever_name_a_node_in_their_own_batch(snapshot: Snapshot) -> None:
    result = snapshot.analyse()
    keys = set(_nodes(result))
    for edge in _edges(result):
        if edge.resolution_status == "resolved":
            assert edge.target_key in keys
            assert edge.unresolved_target is None
        else:
            assert edge.target_key is None
            assert edge.unresolved_target is not None
            assert edge.unresolved_target.descriptor
            assert len(edge.unresolved_target.descriptor) <= 1024


# --- unanalysable files -------------------------------------------------------------


def test_unanalysable_files_are_reported_without_discarding_other_files(
    snapshot: Snapshot,
) -> None:
    result = snapshot.analyse()
    outcomes = _outcomes(result)

    assert outcomes["pkg/legacy.py"] == "syntax_invalid"
    assert outcomes["pkg/mojibake.py"] == "decode_failed"
    # A PEP 263 cookie makes a non-UTF-8 source analysable rather than a failure.
    assert outcomes["pkg/latin.py"] == "analysed"
    assert set(outcomes.values()) <= FILE_OUTCOMES

    # Both failures are bounded and source-located.
    reports = {report.relative_path: report for report in result.reports}
    legacy = reports["pkg/legacy.py"]
    assert legacy.locator is not None and legacy.locator.start_line == 1
    assert legacy.detail is not None and len(legacy.detail) <= 200
    mojibake = reports["pkg/mojibake.py"]
    assert mojibake.locator is not None and mojibake.locator.start_line == 5
    assert mojibake.detail is not None and "offset" in mojibake.detail

    # And every valid fact from every other file survives both of them.
    assert "pkg.models.Record.to_dict" in _nodes(result)
    assert "pkg.latin.café" in _nodes(result)

    # Only admitted paths are reported; exclusion is counted, not enumerated.
    assert "README.md" not in outcomes
    assert "pkg/__pycache__/service.py" not in outcomes
    assert result.indexed_file_count == len(REPOSITORY)
    assert result.excluded_file_count == 2
    assert len(result.reports) == len(REPOSITORY) - 2


def test_an_identity_outside_the_contract_is_reported_rather_than_mangled(
    workspace: Any, blobs_root: Path
) -> None:
    # A definition whose logical path is past the identifier budget: real source, and an
    # identity this contract cannot carry. It is reported, the file's other facts are
    # dropped with it, and the reference to it is unresolved rather than resolved to
    # something else.
    long_name = "n" * 300
    files = {
        "pkg/__init__.py": b'"""Package."""\n',
        "pkg/wide.py": f'"""Wide."""\n\n\ndef {long_name}() -> None:\n    return None\n'.encode(),
        "pkg/caller.py": f'"""Caller."""\n\nfrom .wide import {long_name}\n'.encode(),
    }
    result = Snapshot(workspace, blobs_root, files=files).analyse()
    assert _outcomes(result)["pkg/wide.py"] == "identity_unsupported"
    report = next(item for item in result.reports if item.relative_path == "pkg/wide.py")
    assert report.detail is not None and "identifier bounds" in report.detail
    assert report.locator is not None
    assert f"pkg.wide.{long_name}" not in _nodes(result)
    assert _targets(result, relationship="imports", source_key="pkg.caller") == {
        f"unresolved/unsupported-identity:pkg.wide.{long_name}"
    }


def test_a_snapshot_whose_every_module_path_is_unnameable_yields_no_batch(
    workspace: Any, blobs_root: Path
) -> None:
    # Real, parseable Python whose *module* identity is past the identifier budget: the
    # files are read and parsed and still produce no node, which is the same answer as a
    # snapshot with no Python in it rather than a malformed batch.
    deep = "/".join("d" * 40 for _ in range(8)) + "/mod.py"
    result = Snapshot(workspace, blobs_root, files={deep: b'"""Deep."""\n'}).analyse()
    assert result.verified is None and result.batch is None
    assert _outcomes(result) == {deep: "identity_unsupported"}


def test_a_snapshot_with_no_python_source_yields_no_batch_rather_than_a_failure(
    workspace: Any, blobs_root: Path
) -> None:
    result = Snapshot(
        workspace, blobs_root, files={"README.md": b"# prose\n", "data/x.json": b"{}\n"}
    ).analyse()
    assert result.verified is None
    assert result.batch is None
    assert result.reports == ()
    assert result.excluded_file_count == 2
    assert analysis_manifest(result)["batch_digest"] is None


# --- fail-closed binding ------------------------------------------------------------


def test_a_missing_or_changed_blob_fails_the_whole_batch(
    workspace: Any, blobs_root: Path
) -> None:
    files = {
        "pkg/__init__.py": b'"""Package."""\n',
        "pkg/models.py": _MODELS.encode("utf-8"),
    }
    snapshot = Snapshot(workspace, blobs_root, files=files)
    assert snapshot.analyse().batch is not None

    digest = snapshot.digests["pkg/models.py"]
    object_path = blob_path(blobs_root, digest)

    # Bytes that no longer hash to the address the snapshot recorded are not this
    # snapshot's source, whatever else they are.
    object_path.write_bytes(b'"""Replaced after capture."""\n')
    with pytest.raises(CodeAnalysisBindingError, match="recorded digest"):
        snapshot.analyse()

    # And neither is an absent object.
    object_path.unlink()
    with pytest.raises(CodeAnalysisBindingError, match="cannot be opened"):
        snapshot.analyse()


def test_a_truncated_object_is_refused_rather_than_parsed(
    workspace: Any, blobs_root: Path
) -> None:
    files = {"pkg/__init__.py": _MODELS.encode("utf-8")}
    snapshot = Snapshot(workspace, blobs_root, files=files)
    blob_path(blobs_root, snapshot.digests["pkg/__init__.py"]).write_bytes(
        _MODELS.encode("utf-8")[:40]
    )
    with pytest.raises(CodeAnalysisBindingError, match="recorded digest"):
        snapshot.analyse()


def test_a_snapshot_or_profile_mismatch_fails_closed(workspace: Any, blobs_root: Path) -> None:
    snapshot = Snapshot(workspace, blobs_root, files={"pkg/__init__.py": b'"""P."""\n'})

    with pytest.raises(CodeAnalysisBindingError, match="expected profile"):
        snapshot.analyse(expected_profile_digest="sha256:" + "0" * 64)
    assert snapshot.analyse(expected_profile_digest=DEFAULT_PYTHON_PROFILE.digest) is not None

    with pytest.raises(CodeAnalysisBindingError, match="covered source history"):
        snapshot.analyse(snapshot_id="snap-absent")
    with pytest.raises(CodeAnalysisBindingError, match="covered source history"):
        snapshot.analyse(repository_id="repo-other")

    incomplete = Snapshot(
        workspace,
        blobs_root,
        files={"pkg/__init__.py": b'"""P."""\n'},
        snapshot_id="snap-incomplete",
        capture_status="incomplete",
        base_us=1_700_000_300_000_000,
    )
    with pytest.raises(CodeAnalysisBindingError, match="capture is incomplete"):
        incomplete.analyse()


def test_a_flat_v1_snapshot_is_not_analysable_source(workspace: Any, blobs_root: Path) -> None:
    # A `flat_v1` snapshot records its manifest inline and seals no capture, so there is
    # no indexed byte for a candidate to bind to. Recorded here through the production
    # application surface, which is the only way one is ever created.
    content = b'"""Flat."""\n'
    digest = _digest(content)
    publish_blob(blobs_root, digest, content)
    workspace.record(esc._source(1, "snap-flat", {"pkg/__init__.py": digest}))
    with pytest.raises(CodeAnalysisBindingError, match="sealed captured snapshot"):
        analyse_python_snapshot(
            workspace.holder.connection,
            workspace_id=WORKSPACE_ID,
            repository_id=esc.REPOSITORY,
            snapshot_id="snap-flat",
            blobs_root=blobs_root,
        )


def test_a_captured_path_outside_the_repository_fails_closed(
    workspace: Any, blobs_root: Path
) -> None:
    # The capture index is server-owned and sealed, and a file cannot be indexed after
    # the header is sealed -- so the escaping entry is part of the capture itself. A path
    # that is not repository-relative means the row this analysis would bind to is not
    # the row the capture wrote, so it refuses rather than skipping the entry.
    snapshot = Snapshot(
        workspace,
        blobs_root,
        files={"pkg/__init__.py": b'"""P."""\n', "../outside/evil.py": b'"""Outside."""\n'},
    )
    with pytest.raises(CodeAnalysisBindingError, match="outside the repository"):
        snapshot.analyse()


# --- bounds -------------------------------------------------------------------------


def test_snapshot_level_bounds_refuse_rather_than_analyse_a_prefix(snapshot: Snapshot) -> None:
    # Analysing part of a repository would produce a batch that reads like a description
    # of the whole snapshot, so each snapshot-level budget refuses the batch instead.
    for overrides, match in (
        ({"max_files": 2}, "more analysable files"),
        ({"max_file_bytes": 512, "max_total_bytes": 512}, "total byte budget"),
        ({"max_candidates": 5}, "more candidates"),
        ({"max_file_reports": 1}, "more unanalysable files"),
    ):
        with pytest.raises(PythonAnalysisRefused, match=match):
            snapshot.analyse(profile=PythonAnalyserProfile(**overrides))


def test_per_file_bounds_report_one_file_and_keep_every_other_fact(
    snapshot: Snapshot,
) -> None:
    oversize = snapshot.analyse(
        profile=PythonAnalyserProfile(max_file_bytes=200, max_file_reports=50)
    )
    outcomes = _outcomes(oversize)
    assert outcomes["pkg/models.py"] == "oversize"
    assert outcomes["pkg/__init__.py"] == "analysed"
    assert "pkg" in _nodes(oversize)
    assert oversize.analysed_bytes < sum(len(value) for value in REPOSITORY.values())

    budgeted = snapshot.analyse(
        profile=PythonAnalyserProfile(max_ast_nodes_per_file=40, max_file_reports=50)
    )
    assert _outcomes(budgeted)["pkg/service.py"] == "ast_budget_exceeded"
    report = next(item for item in budgeted.reports if item.relative_path == "pkg/service.py")
    assert report.detail is not None and "nodes" in report.detail
    assert "pkg" in _nodes(budgeted)


def test_the_profiles_grammar_version_gates_the_admitted_language(
    workspace: Any, blobs_root: Path
) -> None:
    # `except*` is 3.11 syntax. The profile's grammar is passed to `ast.parse` as
    # `feature_version`, so it decides what the analyser accepts -- the admitted language
    # is a property of the declared profile, not of whichever interpreter is running.
    source = (
        b'"""Exception groups."""\n\n\ndef guarded() -> None:\n    try:\n        pass\n'
        b"    except* ValueError:\n        pass\n"
    )
    snapshot = Snapshot(workspace, blobs_root, files={"pkg/groups.py": source})

    assert _outcomes(snapshot.analyse())["pkg/groups.py"] == "analysed"
    assert "pkg.groups.guarded" in _nodes(snapshot.analyse())

    older = snapshot.analyse(profile=PythonAnalyserProfile(grammar_version="3.10"))
    assert _outcomes(older)["pkg/groups.py"] == "syntax_invalid"
    report = next(item for item in older.reports if item.relative_path == "pkg/groups.py")
    assert report.detail is not None and "profile's grammar" in report.detail
    # Located inside the guarded block rather than at an exact line: the diagnostic
    # carries CPython's own reported position, which is not this suite's to pin.
    assert report.locator is not None and 5 <= report.locator.start_line <= 8
    assert older.verified is None


def test_the_profile_refuses_its_own_malformed_configuration() -> None:
    for overrides in (
        {"grammar_version": "3"},
        {"grammar_version": "three.eleven"},
        {"include_suffixes": ()},
        {"include_suffixes": ("py",)},
        {"max_files": 0},
        {"max_candidates": 10_001},
        {"max_file_bytes": 2, "max_total_bytes": 1},
        {"producer_id": ""},
    ):
        with pytest.raises(CodeAnalysisCandidateError):
            PythonAnalyserProfile(**overrides)


def test_the_profile_refuses_a_grammar_this_parser_cannot_provide() -> None:
    """A declared grammar has to be one `ast.parse` on this interpreter can be held to.

    `feature_version` only admits major 3 -- anything else trips CPython's own assertion
    rather than a refusal -- and a *newer* minor than the running interpreter is accepted
    in silence while the parser goes on parsing the grammar it actually has, so a batch
    produced under it would name a grammar nothing enforced.
    """
    running = sys.version_info[:2]
    for version in ("2.7", "99.99", "4.0", f"{running[0]}.{running[1] + 1}"):
        with pytest.raises(CodeAnalysisCandidateError, match="this parser can provide"):
            PythonAnalyserProfile(grammar_version=version)

    # The running grammar and every older one are admitted, and the default is unmoved.
    assert PythonAnalyserProfile(grammar_version=f"{running[0]}.{running[1]}") is not None
    assert PythonAnalyserProfile(grammar_version="3.9") is not None
    assert DEFAULT_PYTHON_PROFILE.grammar_version == "3.11"
    assert DEFAULT_PYTHON_PROFILE.feature_version == (3, 11)
    assert PythonAnalyserProfile(grammar_version="3.11").digest == DEFAULT_PYTHON_PROFILE.digest


def test_the_profile_digest_covers_the_whole_configuration() -> None:
    baseline = DEFAULT_PYTHON_PROFILE.digest
    for overrides in (
        # An *older* grammar: a newer one than the running parser is refused outright,
        # which is its own test below.
        {"grammar_version": "3.10"},
        {"include_suffixes": (".py",)},
        {"exclude_path_segments": ()},
        {"exclude_path_prefixes": ("vendor/",)},
        {"max_files": 499},
        {"max_ast_nodes_per_file": 199_999},
        {"producer_version": "1.0.1"},
        {"locator_basis": "character_columns.v1"},
    ):
        assert PythonAnalyserProfile(**overrides).digest != baseline, overrides
    assert PythonAnalyserProfile().digest == baseline


def test_exclusion_rules_are_explicit_and_configurable(workspace: Any, blobs_root: Path) -> None:
    files = {
        "pkg/__init__.py": b'"""P."""\n',
        "pkg/keep.py": b'"""Keep."""\n',
        "pkg/keep.pyi": b"def keep() -> None: ...\n",
        "vendor/dep.py": b'"""Vendored."""\n',
        ".venv/lib/thing.py": b'"""Virtual environment."""\n',
    }
    snapshot = Snapshot(workspace, blobs_root, files=files)

    default = snapshot.analyse()
    assert set(_outcomes(default)) == {
        "pkg/__init__.py",
        "pkg/keep.py",
        "pkg/keep.pyi",
        "vendor/dep.py",
    }
    assert default.excluded_file_count == 1

    narrowed = snapshot.analyse(
        profile=PythonAnalyserProfile(
            include_suffixes=(".py",), exclude_path_prefixes=("vendor/",)
        )
    )
    assert set(_outcomes(narrowed)) == {"pkg/__init__.py", "pkg/keep.py"}
    assert narrowed.excluded_file_count == 3


# --- heartbeat and cancellation -----------------------------------------------------


def test_the_heartbeat_fires_once_per_admitted_file_and_can_cancel(
    snapshot: Snapshot,
) -> None:
    beats: list[int] = []
    result = snapshot.analyse(heartbeat=lambda: beats.append(1))
    assert len(beats) == len(REPOSITORY) - 2 == len(result.reports)

    class Cancelled(RuntimeError):
        pass

    def cancel_after_two() -> None:
        beats.append(1)
        if len(beats) > len(REPOSITORY) + 2:
            raise Cancelled("cancelled")

    with pytest.raises(Cancelled):
        snapshot.analyse(heartbeat=cancel_after_two)


# --- no execution, no live checkout, no graph authority -----------------------------


def test_the_producer_never_executes_the_source_it_analyses(
    snapshot: Snapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = snapshot.analyse()
    # `pkg/sideeffect.py` writes a sentinel and raises at module level. It is analysed
    # into candidates and never runs.
    assert _outcomes(result)["pkg/sideeffect.py"] == "analysed"
    assert "pkg.sideeffect.never" in _nodes(result)
    assert not (tmp_path / SENTINEL).exists()
    assert not list(tmp_path.glob(f"**/{SENTINEL}"))


def test_the_producer_imports_no_execution_network_or_graph_machinery() -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "src/omnivia_core_runtime/storage/code_analysis_python.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = (
        "importlib",
        "runpy",
        "subprocess",
        "multiprocessing",
        "socket",
        "ssl",
        "urllib",
        "http",
        "ftplib",
        "pickle",
        "shutil",
        "site",
        "sysconfig",
        "venv",
        "pip",
        "setuptools",
        "ctypes",
        "tomllib",
        "configparser",
    )
    assert not [name for name in imported if name.split(".")[0] in forbidden]
    # No graph or governance writer, and no second store, server or transport.
    assert not [
        name
        for name in imported
        if any(
            part in name
            for part in ("graph", "governance", "governed", "service.", "transport", "memory")
        )
    ]
    # The analysis surface is the standard library's parser and its encoding reader.
    assert {"ast", "tokenize"} <= imported

    # Nothing compiles, evaluates or imports by name at runtime either.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"eval", "exec", "compile", "__import__", "open", "input"}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not attributes & {"import_module", "system", "popen", "spawn", "urlopen"}


def test_the_producer_reads_nothing_outside_the_blob_root(
    snapshot: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # There is no checkout anywhere in this suite, so the only way an analysis can
    # succeed is by reading the blob store. Pin that: every path this run opens must be
    # under the blob root it was given.
    opened: list[str] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        opened.append(os.fsdecode(path))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(subject.os, "open", recording_open)
    assert snapshot.analyse().batch is not None
    assert opened
    root = str(snapshot.blobs_root)
    assert [name for name in opened if not name.startswith(root)] == []


def test_the_producer_returns_candidates_and_writes_nothing(snapshot: Snapshot) -> None:
    def counts() -> dict[str, int]:
        rows = snapshot.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE 'omnivia_%'"
        ).fetchall()
        return {
            str(name): int(
                snapshot.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            )
            for (name,) in rows
        }

    before = counts()
    result = snapshot.analyse()
    assert result.batch is not None
    assert counts() == before

    # The only adaptation offered is to the existing proposed-only memory inputs, and it
    # carries the profile identity with every candidate.
    inputs = to_memory_create_inputs(
        result.verified,  # type: ignore[arg-type]
        actor_id="service-code-analysis",
        asserted_at="2026-10-18T00:00:00.000Z",
    )
    assert len(inputs) == len(result.batch.nodes) + len(result.batch.edges)
    assert {item.record_type for item in inputs} == {"memory.entity", "memory.relation"}
    assert all(item.extraction.reconciliation_state == "unreviewed" for item in inputs)
    assert all(
        item.content["profile_digest"] == DEFAULT_PYTHON_PROFILE.digest for item in inputs
    )
    # A Unicode identity survives the contract decoder intact.
    assert any(item.content.get("display_name") == "Café" for item in inputs)
    assert counts() == before
