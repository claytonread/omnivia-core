"""Executable gate for the deliberately unexposed Phase 2 mutation seam."""

from __future__ import annotations

from pathlib import Path

from omnivia_core_runtime.service.semantic_phase2 import (
    PHASE2_EXTERNAL_MUTATION_ADAPTERS,
    PHASE2_MUTATING_SERVICE_OPERATIONS,
    SemanticPhase2Service,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
PHASE2_SERVICE = (
    REPO_ROOT
    / "packages/omnivia-core-runtime/src/omnivia_core_runtime/service/semantic_phase2.py"
)
EXTERNAL_SOURCE_ROOTS = (
    REPO_ROOT / "packages/omnivia-core-cli/src",
    REPO_ROOT / "packages/omnivia-core-client/src",
    REPO_ROOT / "packages/omnivia-core-mcp/src",
    REPO_ROOT / "packages/omnivia-core-runtime/src/omnivia_core_runtime/service",
)
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".mjs"})


def _adapter_sources() -> tuple[Path, ...]:
    return tuple(
        path
        for root in EXTERNAL_SOURCE_ROOTS
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and path.resolve() != PHASE2_SERVICE.resolve()
    )


def test_phase2_mutation_inventory_names_real_service_methods() -> None:
    assert PHASE2_MUTATING_SERVICE_OPERATIONS == {
        "register_evidence",
        "create_observation",
        "aggregate_candidate",
        "reject_candidate",
        "reconsider_candidate",
        "convert_candidate",
    }
    assert all(
        callable(getattr(SemanticPhase2Service, operation, None))
        for operation in PHASE2_MUTATING_SERVICE_OPERATIONS
    )


def test_no_external_phase2_mutation_adapter_is_registered_or_coupled() -> None:
    assert PHASE2_EXTERNAL_MUTATION_ADAPTERS == ()
    offenders = {
        str(path.relative_to(REPO_ROOT)): marker
        for path in _adapter_sources()
        for marker in ("SemanticPhase2Service", "semantic_phase2")
        if marker in path.read_text(encoding="utf-8")
    }
    assert offenders == {}, (
        "Phase 2 service coupling was added outside its in-process module. Any public "
        "mutation adapter must first compose through governed caller-scoped "
        f"idempotency and register that adapter explicitly: {offenders}"
    )
