"""omnivia_core.memory_graph must be an explicit portable barrel: exact
ordered ``__all__`` parity with the legacy memory_graph package (once its
seven runtime-only exports are filtered out), every exported name bound to
its canonical owner leaf (models/assembly/fixtures/validation/_shared), a
star import exposing exactly what ``__all__`` advertises, no ``__getattr__``
escape hatch, and a fresh-process import that never touches
``omnivia_memory`` or a runtime/store/adapter module.

``test_parity.py`` already proves each owner leaf (``memory_graph.models``,
``.assembly``, ``.fixtures``, ``.validation``) is an exact port of its legacy
counterpart. This module is only concerned with the barrel's own re-export
contract and the generated-fixture/assembly behavior that sits on top of it,
neither of which any per-leaf gate observes.
"""

from __future__ import annotations

import copy
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
PUBLIC_EXPORTS_INVENTORY = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"
FIXTURE_BUNDLE_JSON = (
    REPO_ROOT
    / "services"
    / "omnivia-memory"
    / "tests"
    / "fixtures"
    / "memory_graph"
    / "fixture_bundle.json"
)
#: The interpreter running this suite -- see test_fresh_process_imports.py
#: for why this must not be a hardcoded ``.venv/bin/python``.
PYTHON = sys.executable

with PUBLIC_EXPORTS_INVENTORY.open(encoding="utf-8") as inventory_file:
    FROZEN_MODULES = json.load(inventory_file)["modules"]

#: Explicit, source-preserving canonical export tuple. Order matters: it is
#: the historical export order, matching both
#: ``omnivia_core.memory_graph.__all__`` and the legacy ``__all__`` once the
#: seven runtime-only names below are filtered out.
CANONICAL_MEMORY_GRAPH_ALL: tuple[str, ...] = (
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
)

#: Legacy-only exports: the durable-store/ingestion-adapter surface that
#: stays runtime-owned and must never enter the canonical barrel.
RUNTIME_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "IngestionGraphAdapterError",
        "IngestionGraphWriteResult",
        "MemoryGraphStore",
        "MemoryGraphStoreError",
        "chunk_to_memory_segment",
        "source_to_memory_source",
        "write_ingestion_records_to_graph",
    }
)

FROZEN_MEMORY_GRAPH_ALL: frozenset[str] = frozenset(
    FROZEN_MODULES["omnivia_memory.memory_graph"]["all"]
)

LEGACY_TO_CANONICAL_OWNER: dict[str, str] = {
    "omnivia_memory._shared.validation": "omnivia_core._shared.validation",
    "omnivia_memory.memory_graph.assembly": "omnivia_core.memory_graph.assembly",
    "omnivia_memory.memory_graph.fixtures": "omnivia_core.memory_graph.fixtures",
    "omnivia_memory.memory_graph.models": "omnivia_core.memory_graph.models",
    "omnivia_memory.memory_graph.validation": "omnivia_core.memory_graph.validation",
}

#: ``Confidence`` (a ``types.UnionType`` alias) and ``FIXTURE_TIME`` (a bare
#: ``str`` constant) both have a runtime ``__module__`` that does not resolve
#: back to their defining module, so the frozen inventory's ``defines``
#: provenance omits them (see ``_strict.EXTRA_MODULE_CONSTANTS`` for the same
#: exclusion documented against the parity gate). Pinned explicitly here.
EXPLICIT_OWNER_PINS: dict[str, str] = {
    "Confidence": "omnivia_core.memory_graph.models",
    "FIXTURE_TIME": "omnivia_core.memory_graph.fixtures",
}


def _expected_owner_by_export() -> dict[str, str]:
    owners: dict[str, str] = {}
    for legacy_owner, canonical_owner in LEGACY_TO_CANONICAL_OWNER.items():
        for name in FROZEN_MODULES[legacy_owner]["defines"]:
            if name in CANONICAL_MEMORY_GRAPH_ALL:
                assert name not in owners, f"multiple frozen owners found for {name}"
                owners[name] = canonical_owner
    for name, owner in EXPLICIT_OWNER_PINS.items():
        assert name not in owners, f"{name} is no longer omitted from frozen 'defines'"
        owners[name] = owner
    assert set(owners) == set(CANONICAL_MEMORY_GRAPH_ALL), (
        "frozen owner map does not cover the complete memory_graph barrel: "
        f"missing={sorted(set(CANONICAL_MEMORY_GRAPH_ALL) - set(owners))}, "
        f"extra={sorted(set(owners) - set(CANONICAL_MEMORY_GRAPH_ALL))}"
    )
    return owners


EXPECTED_OWNER_BY_EXPORT = _expected_owner_by_export()
EXPECTED_CANONICAL_MODULE_CLOSURE = {
    "omnivia_core",
    "omnivia_core._shared",
    "omnivia_core._shared.validation",
    "omnivia_core.memory_graph",
    "omnivia_core.memory_graph.models",
    "omnivia_core.memory_graph.assembly",
    "omnivia_core.memory_graph.fixtures",
    "omnivia_core.memory_graph.validation",
}

FORBIDDEN_MODULE_ROOTS = {
    "omnivia_memory",
    "sqlalchemy",
    "sqlite3",
    "fastapi",
    "starlette",
    "httpx",
    "requests",
    "omnivia_core_runtime",
    "omnivia_core_mcp",
    "omnivia_core_cli",
    "omnivia_platform",
    "omnivia_dev",
    "omnivia_cloud",
}


def test_memory_graph_barrel_all_matches_pinned_canonical_tuple_exactly() -> None:
    canonical = importlib.import_module("omnivia_core.memory_graph")
    assert tuple(canonical.__all__) == CANONICAL_MEMORY_GRAPH_ALL, (
        "omnivia_core.memory_graph.__all__ drifted from the frozen source-preserving tuple:\n"
        f"canonical: {canonical.__all__}\nexpected: {list(CANONICAL_MEMORY_GRAPH_ALL)}"
    )


def test_memory_graph_barrel_all_matches_live_legacy_all_minus_runtime_names() -> None:
    """Legacy ``__all__``, with the seven runtime-only names filtered out (in
    place, preserving order), must equal the canonical tuple exactly -- not
    merely as a set. This is checked against the *live* legacy module, not
    the frozen inventory, per the order-oracle caveat below.
    """
    legacy = importlib.import_module("omnivia_memory.memory_graph")
    filtered = tuple(name for name in legacy.__all__ if name not in RUNTIME_EXCLUSIONS)
    assert filtered == CANONICAL_MEMORY_GRAPH_ALL, (
        "live omnivia_memory.memory_graph.__all__ (minus runtime exclusions) drifted "
        f"from the canonical tuple:\nlegacy filtered: {list(filtered)}\n"
        f"canonical: {list(CANONICAL_MEMORY_GRAPH_ALL)}"
    )


def test_frozen_inventory_membership_equals_contract_plus_runtime_exclusions() -> None:
    """The frozen Phase 0 inventory is a membership oracle only (it records no
    stable order for a barrel with no symbols of its own -- ``defines`` is
    empty), so this compares as sets: the 31 canonical names plus the 7
    documented runtime exclusions, nothing more and nothing less.
    """
    assert FROZEN_MEMORY_GRAPH_ALL == set(CANONICAL_MEMORY_GRAPH_ALL) | RUNTIME_EXCLUSIONS, (
        "frozen omnivia_memory.memory_graph 'all' no longer equals the 31 canonical "
        f"contract names plus the 7 runtime exclusions: {sorted(FROZEN_MEMORY_GRAPH_ALL)}"
    )


def test_memory_graph_barrel_all_has_no_duplicates() -> None:
    canonical = importlib.import_module("omnivia_core.memory_graph")
    assert len(canonical.__all__) == len(set(canonical.__all__)), (
        f"omnivia_core.memory_graph.__all__ contains duplicates: {canonical.__all__}"
    )


def test_memory_graph_barrel_star_import_exposes_exactly_the_31_names() -> None:
    namespace: dict[str, object] = {}
    exec("from omnivia_core.memory_graph import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(CANONICAL_MEMORY_GRAPH_ALL), (
        f"star import of omnivia_core.memory_graph exposed {sorted(exported)}, "
        f"expected exactly {sorted(CANONICAL_MEMORY_GRAPH_ALL)}"
    )


def test_memory_graph_barrel_bindings_match_their_canonical_owner_leaf() -> None:
    """Every advertised name is identical to its exact frozen owner binding."""
    canonical = importlib.import_module("omnivia_core.memory_graph")

    for name in canonical.__all__:
        owner_name = EXPECTED_OWNER_BY_EXPORT[name]
        owner_module = importlib.import_module(owner_name)
        assert getattr(canonical, name) is getattr(owner_module, name), (
            f"omnivia_core.memory_graph.{name} is not identical to "
            f"its frozen owner {owner_name}.{name}"
        )


def test_runtime_exclusions_are_absent_from_barrel_all_and_attributes() -> None:
    canonical = importlib.import_module("omnivia_core.memory_graph")
    for name in RUNTIME_EXCLUSIONS:
        assert name not in canonical.__all__, (
            f"{name} is a runtime-only export and must not be in omnivia_core.memory_graph.__all__"
        )
        assert not hasattr(canonical, name), (
            f"{name} is a runtime-only export and must not be an attribute of "
            "omnivia_core.memory_graph"
        )


def test_memory_graph_barrel_has_no_getattr_escape_hatch() -> None:
    canonical = importlib.import_module("omnivia_core.memory_graph")
    assert "__getattr__" not in vars(canonical), (
        "omnivia_core.memory_graph must not define module __getattr__ -- every "
        "export must be a real, statically-visible binding"
    )


def _isolated_barrel_import_script(import_order: tuple[str, ...]) -> str:
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            "import importlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {import_order!r}:",
            "    importlib.import_module(module_name)",
            'barrel = sys.modules["omnivia_core.memory_graph"]',
            f"expected_all = {CANONICAL_MEMORY_GRAPH_ALL!r}",
            "if tuple(barrel.__all__) != expected_all:",
            '    raise SystemExit(f"__all__ drifted: {tuple(barrel.__all__)!r}")',
            f"owner_by_export = {EXPECTED_OWNER_BY_EXPORT!r}",
            "for export_name in expected_all:",
            "    owner_module = importlib.import_module(owner_by_export[export_name])",
            "    if getattr(barrel, export_name) is not getattr(owner_module, export_name):",
            '        raise SystemExit(f"{export_name} is not identical to its canonical owner")',
            f"runtime_exclusions = {sorted(RUNTIME_EXCLUSIONS)!r}",
            "for excluded_name in runtime_exclusions:",
            "    if excluded_name in barrel.__all__ or hasattr(barrel, excluded_name):",
            '        raise SystemExit(f"runtime exclusion {excluded_name} leaked into the barrel")',
            'if "__getattr__" in vars(barrel):',
            '    raise SystemExit("omnivia_core.memory_graph must not define __getattr__")',
            f"core_src = pathlib.Path({str(CORE_SRC)!r}).resolve()",
            "outside = []",
            "for name, module in sorted(sys.modules.items()):",
            '    if name != "omnivia_core" and not name.startswith("omnivia_core."):',
            "        continue",
            '    path = getattr(module, "__file__", None)',
            "    if path is None:",
            "        continue",
            "    resolved = pathlib.Path(path).resolve()",
            "    try:",
            "        resolved.relative_to(core_src)",
            "    except ValueError:",
            '        outside.append(f"{name} -> {resolved}")',
            "if outside:",
            '    raise SystemExit("omnivia_core modules loaded from outside src: " + ", ".join(outside))',
            f"expected_core = {EXPECTED_CANONICAL_MODULE_CLOSURE!r}",
            'loaded_core = {name for name in sys.modules if name == "omnivia_core" or name.startswith("omnivia_core.")}',
            "if loaded_core != expected_core:",
            '    raise SystemExit(f"unexpected canonical module closure: {sorted(loaded_core)}")',
            f"forbidden = {sorted(FORBIDDEN_MODULE_ROOTS)!r}",
            'leaked = sorted(set(forbidden) & {m.split(".")[0] for m in sys.modules})',
            "if leaked:",
            '    raise SystemExit(f"forbidden modules loaded: {leaked}")',
            'print("OK")',
        ]
    )


@pytest.mark.parametrize(
    "import_order",
    (
        ("omnivia_core.memory_graph",),
        (
            "omnivia_core._shared.validation",
            "omnivia_core.memory_graph.models",
            "omnivia_core.memory_graph.assembly",
            "omnivia_core.memory_graph.fixtures",
            "omnivia_core.memory_graph.validation",
            "omnivia_core.memory_graph",
        ),
        (
            "omnivia_core.memory_graph.validation",
            "omnivia_core.memory_graph.fixtures",
            "omnivia_core.memory_graph.assembly",
            "omnivia_core.memory_graph.models",
            "omnivia_core._shared.validation",
            "omnivia_core.memory_graph",
        ),
    ),
    ids=("barrel-first", "dependency-order", "reverse-requested-order"),
)
def test_memory_graph_barrel_imports_in_isolation_from_src_alone(
    import_order: tuple[str, ...],
) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", _isolated_barrel_import_script(import_order)],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated import of omnivia_core.memory_graph failed\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"


def _normalize_fixture_bundle(fixture: dict[str, Any]) -> dict[str, object]:
    """Recursively normalize a ``MemoryGraphFixture`` to plain JSON-shaped data.

    Every value in the bundle is either a dataclass instance with a
    ``to_dict()`` method (source/entities/facts/graph_preview/evidence_graph)
    or a list of them (segments/entities/facts) -- the same ``to_dict()``
    delegation ``test_memory_graph_contract.py`` already relies on for
    round-tripping. This exists only to compare the in-code fixture against
    the tracked ``fixture_bundle.json`` payload; it changes no production
    behavior.
    """
    return {
        "source": fixture["source"].to_dict(),
        "segments": [segment.to_dict() for segment in fixture["segments"]],
        "entities": [entity.to_dict() for entity in fixture["entities"]],
        "facts": [fact.to_dict() for fact in fixture["facts"]],
        "graph_preview": fixture["graph_preview"].to_dict(),
        "evidence_graph": fixture["evidence_graph"].to_dict(),
    }


def test_generated_fixtures_normalize_to_tracked_fixture_bundle_json() -> None:
    canonical_fixtures = importlib.import_module("omnivia_core.memory_graph.fixtures")
    legacy_fixtures = importlib.import_module("omnivia_memory.memory_graph.fixtures")

    expected = json.loads(FIXTURE_BUNDLE_JSON.read_text(encoding="utf-8"))

    canonical_normalized = _normalize_fixture_bundle(canonical_fixtures.build_memory_graph_fixture())
    legacy_normalized = _normalize_fixture_bundle(legacy_fixtures.build_memory_graph_fixture())

    assert canonical_normalized == expected, (
        "omnivia_core.memory_graph.fixtures.build_memory_graph_fixture() no longer "
        "normalizes to the tracked fixture_bundle.json payload"
    )
    assert legacy_normalized == expected, (
        "omnivia_memory.memory_graph.fixtures.build_memory_graph_fixture() no longer "
        "normalizes to the tracked fixture_bundle.json payload"
    )


def test_generated_fixture_is_deterministic_and_shares_no_mutable_state() -> None:
    canonical_fixtures = importlib.import_module("omnivia_core.memory_graph.fixtures")

    first = canonical_fixtures.build_memory_graph_fixture()
    second = canonical_fixtures.build_memory_graph_fixture()

    assert _normalize_fixture_bundle(first) == _normalize_fixture_bundle(second), (
        "build_memory_graph_fixture() is not deterministic across calls"
    )
    second_before = copy.deepcopy(_normalize_fixture_bundle(second))

    assert first["segments"] is not second["segments"]
    assert first["entities"] is not second["entities"]
    assert first["facts"] is not second["facts"]
    first["segments"].append(first["segments"][0])
    first["entities"].append(first["entities"][0])
    first["facts"].append(first["facts"][0])
    assert first["segments"][0].span is not None
    first["segments"][0].span["independence_probe"] = True
    first["entities"][0].aliases.append("independence-probe")
    first["entities"][0].source_refs.append(first["entities"][0].source_refs[0])
    first["facts"][0].source_refs.append(first["facts"][0].source_refs[0])
    first["graph_preview"].nodes.append(first["graph_preview"].nodes[0])
    assert _normalize_fixture_bundle(second) == second_before, (
        "mutating one generated fixture leaked top-level or nested state into another"
    )


def test_canonical_and_legacy_assembly_validation_and_redaction_smoke() -> None:
    """One compact smoke covering both assembly functions, redaction, and all
    six validators, comparing normalized canonical output against legacy
    output and proving neither mutates its caller-supplied inputs.
    """
    canonical = importlib.import_module("omnivia_core.memory_graph")
    legacy = importlib.import_module("omnivia_memory.memory_graph")

    canonical_fixture = canonical.build_memory_graph_fixture()
    legacy_fixture = legacy.build_memory_graph_fixture()

    source = canonical_fixture["source"]
    entities = list(canonical_fixture["entities"])
    facts = list(canonical_fixture["facts"])
    canonical_assembly_before = copy.deepcopy(
        {
            "source": source.to_dict(),
            "entities": [entity.to_dict() for entity in entities],
            "facts": [fact.to_dict() for fact in facts],
        }
    )

    canonical_preview = canonical.assemble_graph_preview(
        workspace_id=source.workspace_id,
        query="smoke:preview",
        source=source,
        entities=entities,
        facts=facts,
        generated_at=canonical.FIXTURE_TIME,
        node_limit=100,
        edge_limit=150,
    )
    canonical_evidence = canonical.assemble_evidence_graph(
        workspace_id=source.workspace_id,
        answer_id="smoke-answer",
        query="smoke:evidence",
        source=source,
        entities=entities,
        facts=facts,
        generated_at=canonical.FIXTURE_TIME,
    )

    legacy_source = legacy_fixture["source"]
    legacy_entities = list(legacy_fixture["entities"])
    legacy_facts = list(legacy_fixture["facts"])
    legacy_assembly_before = copy.deepcopy(
        {
            "source": legacy_source.to_dict(),
            "entities": [entity.to_dict() for entity in legacy_entities],
            "facts": [fact.to_dict() for fact in legacy_facts],
        }
    )
    legacy_preview = legacy.assemble_graph_preview(
        workspace_id=legacy_source.workspace_id,
        query="smoke:preview",
        source=legacy_source,
        entities=legacy_entities,
        facts=legacy_facts,
        generated_at=legacy.FIXTURE_TIME,
        node_limit=100,
        edge_limit=150,
    )
    legacy_evidence = legacy.assemble_evidence_graph(
        workspace_id=legacy_source.workspace_id,
        answer_id="smoke-answer",
        query="smoke:evidence",
        source=legacy_source,
        entities=legacy_entities,
        facts=legacy_facts,
        generated_at=legacy.FIXTURE_TIME,
    )

    assert {
        "source": source.to_dict(),
        "entities": [entity.to_dict() for entity in entities],
        "facts": [fact.to_dict() for fact in facts],
    } == canonical_assembly_before, "canonical assembly mutated caller-owned inputs"
    assert {
        "source": legacy_source.to_dict(),
        "entities": [entity.to_dict() for entity in legacy_entities],
        "facts": [fact.to_dict() for fact in legacy_facts],
    } == legacy_assembly_before, "legacy assembly mutated caller-owned inputs"
    assert canonical_preview.to_dict() == legacy_preview.to_dict(), (
        "canonical assemble_graph_preview output drifted from legacy"
    )
    assert canonical_evidence.to_dict() == legacy_evidence.to_dict(), (
        "canonical assemble_evidence_graph output drifted from legacy"
    )

    segment = canonical_fixture["segments"][0]
    segment_before = copy.deepcopy(segment.to_dict())
    legacy_segment = legacy_fixture["segments"][0]
    legacy_segment_before = copy.deepcopy(legacy_segment.to_dict())
    canonical_redacted = canonical.redact_segment_preview(segment, max_chars=5)
    legacy_redacted = legacy.redact_segment_preview(legacy_segment, max_chars=5)
    assert segment.to_dict() == segment_before, "redact_segment_preview mutated its segment argument"
    assert legacy_segment.to_dict() == legacy_segment_before, (
        "legacy redact_segment_preview mutated its segment argument"
    )
    assert canonical_redacted == legacy_redacted, "canonical redact_segment_preview output drifted from legacy"
    assert canonical_redacted["text_preview"] == segment_before["text_preview"][:5]

    validator_pairs = (
        (canonical.validate_memory_source, legacy.validate_memory_source, canonical_fixture["source"], legacy_fixture["source"]),
        (
            canonical.validate_memory_segment,
            legacy.validate_memory_segment,
            canonical_fixture["segments"][0],
            legacy_fixture["segments"][0],
        ),
        (
            canonical.validate_memory_entity,
            legacy.validate_memory_entity,
            canonical_fixture["entities"][0],
            legacy_fixture["entities"][0],
        ),
        (
            canonical.validate_memory_fact,
            legacy.validate_memory_fact,
            canonical_fixture["facts"][0],
            legacy_fixture["facts"][0],
        ),
        (
            canonical.validate_graph_preview_response,
            legacy.validate_graph_preview_response,
            canonical_fixture["graph_preview"],
            legacy_fixture["graph_preview"],
        ),
        (
            canonical.validate_evidence_graph_response,
            legacy.validate_evidence_graph_response,
            canonical_fixture["evidence_graph"],
            legacy_fixture["evidence_graph"],
        ),
    )
    for canonical_validator, legacy_validator, canonical_value, legacy_value in validator_pairs:
        canonical_before = copy.deepcopy(canonical_value.to_dict())
        legacy_before = copy.deepcopy(legacy_value.to_dict())
        canonical_result = canonical_validator(canonical_value)
        legacy_result = legacy_validator(legacy_value)
        assert canonical_value.to_dict() == canonical_before, (
            f"{canonical_validator.__name__} mutated its canonical input"
        )
        assert legacy_value.to_dict() == legacy_before, (
            f"{legacy_validator.__name__} mutated its legacy input"
        )
        assert (canonical_result.valid, canonical_result.errors, canonical_result.warnings) == (
            legacy_result.valid,
            legacy_result.errors,
            legacy_result.warnings,
        ), f"{canonical_validator.__name__} output drifted from its legacy counterpart"
