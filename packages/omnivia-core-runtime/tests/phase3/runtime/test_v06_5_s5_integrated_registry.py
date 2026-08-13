"""CP-S5 controls for the exact production application surface."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import (
    ProductionApplicationSurface,
    build_installation_application_dispatcher,
    compose_production_application_surface,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import _build_production_application_surface
from omnivia_core_runtime.service.operations import (
    APPLICATION_OPERATIONS,
    SERVICE_OPERATIONS,
)
from test_v06_5_s2_memory_migration import _apply_through

from omnivia_core.contracts.v1 import OPERATION_CATALOGUE

REPO_ROOT = Path(__file__).resolve().parents[5]
CORPUS = (
    REPO_ROOT
    / "contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json"
)
OPERATION_TRACEABILITY = (
    REPO_ROOT / "tests/fixtures/service_conformance/operation-traceability-v1.json"
)
ARCHITECTURE_TRACEABILITY = (
    REPO_ROOT
    / "tests/fixtures/service_conformance/architecture-gate-traceability-v1.json"
)
CORPUS_SHA256 = "4725374cf11bf5d444ed249de4c93903a98b800b412db995ea1485dcf1269c07"
ADAPTERS = ("in_process", "ipc", "http")


class _InstallationService:
    """Construction-only shape; its bound production handlers are never invoked."""

    authority = SimpleNamespace(installation_id=s0.INSTALLATION_ID)


@pytest.fixture
def surface(tmp_path: Path) -> Iterator[ProductionApplicationSurface]:
    path = tmp_path / "workspace.sqlite"
    _apply_through(path, 16, workspace_id=m1.WORKSPACE_ID)
    owned = m1.take_ownership(path, workspace_id=m1.WORKSPACE_ID)
    principal = "local-user"
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=principal,
            workspaces=frozenset({m1.WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        owned,
    )
    started = SimpleNamespace(**vars(owned), workspace_id=m1.WORKSPACE_ID)
    installation = build_installation_application_dispatcher(
        service=_InstallationService(),  # type: ignore[arg-type]
        principal_id=principal,
        fallback=probe,
    )
    yield _build_production_application_surface(
        started=started,  # type: ignore[arg-type]
        probe=probe,
        installation=installation,
    )
    owned.connection.close()


def _document(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_v06_5_s5_registry_exactly_matches_catalogue(
    surface: ProductionApplicationSurface,
) -> None:
    catalogue = tuple(entry.name for entry in OPERATION_CATALOGUE)
    assert len(catalogue) == len(set(catalogue)) == 20
    assert surface.registry.operations == APPLICATION_OPERATIONS == frozenset(catalogue)
    assert surface.adapters == frozenset(ADAPTERS)
    surface.registry.assert_complete()


def test_v06_5_s5_duplicate_family_registration_refuses(
    surface: ProductionApplicationSurface,
) -> None:
    routes = dict(surface._routes)
    installation = routes["workspace.create"]
    reads = routes["workspace.inspect"]
    memory = routes["memory.create"]
    jobs = routes["job.get"]
    with pytest.raises(ValueError, match="already registered"):
        compose_production_application_surface(
            installation=installation,
            reads=reads,
            memory=memory,
            jobs=jobs,
            governance=memory,
            probe=surface.probe,
        )


def test_v06_5_s5_every_handler_is_production_callable(
    surface: ProductionApplicationSurface,
) -> None:
    identities: dict[str, str] = {}
    for operation in sorted(APPLICATION_OPERATIONS):
        handler = surface.registry.get(operation)
        assert handler is not None and callable(handler), operation
        identities[operation] = f"{handler.__module__}.{handler.__qualname__}"
        assert handler.__module__.startswith(
            "omnivia_core_runtime.service.handlers."
        ), operation
    assert len(identities) == 20
    assert not any(
        token in identity.lower()
        for identity in identities.values()
        for token in ("stub", "fake", "fixture", "noop")
    )


def test_v06_5_s5_live_entry_owns_installation_and_routes_exact_surface() -> None:
    """The serving path, not only a fixture, builds the qualified surface."""
    from omnivia_core_runtime.service import main as service_main

    tree = ast.parse(inspect.getsource(service_main))
    serve = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "serve"
    )
    calls = {
        node.func.id
        for node in ast.walk(serve)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "InstallationAuthorityCoordinator",
        "_build_production_application_surface",
        "_router_for",
    } <= calls
    routed = next(
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_router_for"
    )
    assert isinstance(routed.args[1], ast.Name)
    assert routed.args[1].id == "application"
    assert "installation_authority" in ast.unparse(serve)


def test_v06_5_s5_no_stub_skip_xfail_or_schema_only_execution(
    surface: ProductionApplicationSurface,
) -> None:
    source = inspect.getsource(type(surface))
    assert "route.dispatch(request)" in source
    assert "schema" not in source.lower()
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr in {"skip", "xfail", "importorskip"}
        for node in ast.walk(tree)
    )


def test_v06_5_s5_operation_traceability_complete() -> None:
    document = _document(OPERATION_TRACEABILITY)
    names = tuple(item["contract"]["name"] for item in document["operations"])
    assert names == tuple(entry.name for entry in OPERATION_CATALOGUE)
    assert all(
        set(item["adapters"]) == set(ADAPTERS) for item in document["operations"]
    )
    corpus = _document(CORPUS)
    case_names = {case["operation"] for case in corpus["cases"]}
    assert case_names == APPLICATION_OPERATIONS
    assert len(corpus["cases"]) == 73


def test_v06_5_s5_architecture_gate_traceability_complete() -> None:
    document = _document(ARCHITECTURE_TRACEABILITY)
    gates = document["gates"]
    assert len(gates) == 34
    assert tuple(gate["ordinal"] for gate in gates) == tuple(range(1, 35))
    assert len({gate["gate_id"] for gate in gates}) == 34
    assert all(gate["state"] == "pending_candidate" for gate in gates)
    assert all(
        set(gate["operation_traceability_refs"]) <= APPLICATION_OPERATIONS
        for gate in gates
    )


def test_v06_5_s5_candidate_head_tree_and_corpus_digest() -> None:
    assert hashlib.sha256(CORPUS.read_bytes()).hexdigest() == CORPUS_SHA256
    operation = _document(OPERATION_TRACEABILITY)
    architecture = _document(ARCHITECTURE_TRACEABILITY)
    assert operation["adapter_evidence_corpus"]["case_count"] * len(ADAPTERS) == 219
    assert architecture["operation_traceability"]["file"] == (
        "tests/fixtures/service_conformance/operation-traceability-v1.json"
    )
