"""Tests for the Host Contract v1 generator and its drift gate.

``scripts/generate-host-contract.py`` emits
``src/omnivia_core/host_contract/v1/generated.py`` from the canonical schema and
gates both the approved canonical bytes and the committed artifact. These tests
exercise those checks against the real repository layout, the same pattern
``tests/test_package_boundaries.py`` uses for the sibling gate.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate-host-contract.py"
GENERATED = REPO_ROOT / "src" / "omnivia_core" / "host_contract" / "v1" / "generated.py"
CORE_ACCEPTANCE = REPO_ROOT / ".github" / "workflows" / "core-acceptance.yml"


def _load_generator_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_host_contract_gen", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator_module()


# --------------------------------------------------------------------------
# The committed artifact is current
# --------------------------------------------------------------------------


def test_check_reports_no_drift_on_the_committed_tree() -> None:
    assert generator.check_all(generator.render_all()) == []


def test_the_check_entry_point_exits_zero() -> None:
    assert generator.main(["--check"]) == 0


def test_core_acceptance_runs_the_host_contract_generator_drift_gate() -> None:
    workflow = CORE_ACCEPTANCE.read_text(encoding="utf-8")
    assert "python scripts/generate-host-contract.py --check" in workflow


def test_the_committed_artifact_is_exactly_what_the_schema_renders() -> None:
    rendered = generator.render_python(generator.build_model(generator.load_schema()))
    assert GENERATED.read_text(encoding="utf-8") == rendered


def test_rendering_is_deterministic() -> None:
    first = generator.render_python(generator.build_model(generator.load_schema()))
    second = generator.render_python(generator.build_model(generator.load_schema()))
    assert first == second


# --------------------------------------------------------------------------
# Drift is a finding
# --------------------------------------------------------------------------


def test_a_hand_edited_artifact_is_reported_as_drifted(tmp_path: Path) -> None:
    target = tmp_path / "generated.py"
    target.write_text("# hand edited\n", encoding="utf-8")
    findings = generator.check_all({target: "the rendered content\n"})
    assert len(findings) == 1
    assert "out of date with the schema" in findings[0]


def test_a_missing_artifact_is_reported(tmp_path: Path) -> None:
    findings = generator.check_all({tmp_path / "absent.py": "content\n"})
    assert len(findings) == 1
    assert "missing" in findings[0]


def test_check_returns_non_zero_when_the_canonical_bytes_were_altered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "v1"
    shutil.copytree(REPO_ROOT / "contracts" / "host" / "v1", staged)
    schema = staged / "schemas" / "host-contract-v1.schema.json"
    schema.write_text(schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    monkeypatch.setattr(generator, "CANONICAL_ROOT", staged)
    assert generator.main(["--check"]) == 1


def test_check_returns_non_zero_for_a_file_with_no_approved_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "v1"
    shutil.copytree(REPO_ROOT / "contracts" / "host" / "v1", staged)
    (staged / "fixtures" / "valid" / "smuggled.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(generator, "CANONICAL_ROOT", staged)
    findings = generator.check_authority_digests(staged)
    assert any("smuggled.json" in finding for finding in findings)
    assert generator.main(["--check"]) == 1


# --------------------------------------------------------------------------
# The supported schema subset is enforced, not guessed
# --------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "contracts" / "host" / "v1" / "schemas" / "host-contract-v1.schema.json")
        .read_text(encoding="utf-8")
    )
    return document


def test_an_undeclared_definition_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    defs["surpriseRecord"] = {"type": "object", "properties": {}}
    with pytest.raises(generator.UnsupportedSchemaError, match="disagrees with the declared names"):
        generator.build_model(document)


def test_a_removed_definition_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    del defs["denial"]
    with pytest.raises(generator.UnsupportedSchemaError, match="disagrees with the declared names"):
        generator.build_model(document)


def test_an_unsupported_property_shape_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    defs["denial"]["properties"]["safeReason"] = {"type": "number"}
    with pytest.raises(generator.UnsupportedSchemaError, match="unsupported property schema"):
        generator.build_model(document)


def test_a_property_const_semantic_change_fails_loudly() -> None:
    document = _schema()
    operation = document["$defs"]["request"]["properties"]["operation"]
    operation["const"] = "only_this_operation"
    with pytest.raises(generator.UnsupportedSchemaError, match="const"):
        generator.build_model(document)


def test_unknown_keywords_fail_at_the_root_and_inside_nested_items() -> None:
    root = _schema()
    root["unevaluatedProperties"] = False
    with pytest.raises(generator.UnsupportedSchemaError, match="unevaluatedProperties"):
        generator.build_model(root)

    nested = _schema()
    nested["$defs"]["referenceArray"]["items"]["minLength"] = 1
    with pytest.raises(generator.UnsupportedSchemaError, match="minLength"):
        generator.build_model(nested)


def test_the_approved_schema_passes_the_recursive_keyword_audit() -> None:
    generator.build_model(_schema())


def test_an_undeclared_inline_enum_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    defs["denial"]["properties"]["severity"] = {"enum": ["low", "high"]}
    with pytest.raises(generator.UnsupportedSchemaError, match="no declared constant prefix"):
        generator.build_model(document)


def test_an_undeclared_inline_object_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    defs["denial"]["properties"]["context"] = {
        "type": "object",
        "properties": {"id": {"$ref": "#/$defs/identifier"}},
    }
    with pytest.raises(generator.UnsupportedSchemaError, match="no declared record name"):
        generator.build_model(document)


def test_a_foreign_ref_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    defs["denial"]["properties"]["capabilityId"] = {"$ref": "https://example.test/other.json#/x"}
    with pytest.raises(generator.UnsupportedSchemaError, match="unsupported \\$ref"):
        generator.build_model(document)


def test_a_required_name_no_property_declares_fails_loudly() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    defs["denial"]["required"].append("absentField")
    with pytest.raises(generator.UnsupportedSchemaError, match="required names no property"):
        generator.build_model(document)


def test_a_wrong_schema_dialect_is_refused(tmp_path: Path) -> None:
    document = _schema()
    document["$schema"] = "http://json-schema.org/draft-07/schema#"
    path = tmp_path / "host-contract-v1.schema.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(generator.UnsupportedSchemaError, match="dialect"):
        generator.load_schema(path)


def test_a_wrong_schema_id_is_refused(tmp_path: Path) -> None:
    document = _schema()
    document["$id"] = "urn:omnivia:host-contract:2.0.0"
    path = tmp_path / "host-contract-v1.schema.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(generator.UnsupportedSchemaError, match="unexpected \\$id"):
        generator.load_schema(path)


# --------------------------------------------------------------------------
# The model mirrors the schema
# --------------------------------------------------------------------------


def test_every_schema_definition_is_named() -> None:
    document = _schema()
    defs = document["$defs"]
    assert isinstance(defs, dict)
    assert set(generator.DEFINITION_NAMES) == set(defs)


def test_the_model_resolves_every_top_level_branch() -> None:
    model = generator.build_model(_schema())
    assert len(model.branch_names) == 15
    record_names = {record.name for record in model.records}
    assert set(model.branch_names) <= record_names
