"""Tests for the Chat Runtime Contract v1 generator and its drift gate.

``scripts/generate-chat-contract.py`` verifies the checked-in
``contracts/chat/v1`` resources against a pinned inventory digest and emits
``src/omnivia_core/chat_contract/v1/generated.py``. These tests exercise that
against the real repository layout (the committed tree has no drift) and
against a tampered copy (the digest check fails closed), the same pattern
``tests/host_contract/test_host_generator.py`` uses for the sibling gate.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from omnivia_core.chat_contract.v1 import _validation

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate-chat-contract.py"
GENERATED = REPO_ROOT / "src" / "omnivia_core" / "chat_contract" / "v1" / "generated.py"


def _load_generator_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_chat_contract_gen", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator_module()


# --------------------------------------------------------------------------
# The committed tree and artifact are current
# --------------------------------------------------------------------------


def test_check_reports_no_findings_on_the_committed_tree() -> None:
    assert generator.check_resource_inventory() == []
    assert generator.check_fixture_manifest_counts() == []


def test_the_check_entry_point_exits_zero() -> None:
    assert generator.main(["--check"]) == 0


def test_the_committed_artifact_is_exactly_what_the_tree_renders() -> None:
    assert GENERATED.read_text(encoding="utf-8") == generator.generate()


def test_the_pinned_digest_matches_the_currently_computed_one() -> None:
    digest, schema_count, fixture_count = generator.compute_resource_inventory_digest()
    assert digest == generator.EXPECTED_RESOURCE_INVENTORY_DIGEST
    assert schema_count == generator.EXPECTED_SCHEMA_COUNT == 13
    assert fixture_count == generator.EXPECTED_FIXTURE_TREE_COUNT == 159


# --------------------------------------------------------------------------
# Fail-closed on a tampered, missing or extra resource
# --------------------------------------------------------------------------


def test_a_single_changed_byte_fails_the_digest_check(tmp_path: Path, monkeypatch) -> None:
    tampered = tmp_path / "chat" / "v1"
    shutil.copytree(REPO_ROOT / "contracts" / "chat" / "v1", tampered)
    target = tampered / "schemas" / "common.schema.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")

    monkeypatch.setattr(generator, "CANONICAL_ROOT", tampered)
    monkeypatch.setattr(generator, "SCHEMAS_ROOT", tampered / "schemas")
    monkeypatch.setattr(generator, "FIXTURES_ROOT", tampered / "fixtures")

    findings = generator.check_resource_inventory()
    assert findings != []
    assert any("digest mismatch" in finding for finding in findings)


def test_a_missing_schema_fails_the_count_and_digest_check(tmp_path: Path, monkeypatch) -> None:
    tampered = tmp_path / "chat" / "v1"
    shutil.copytree(REPO_ROOT / "contracts" / "chat" / "v1", tampered)
    (tampered / "schemas" / "common.schema.json").unlink()

    monkeypatch.setattr(generator, "CANONICAL_ROOT", tampered)
    monkeypatch.setattr(generator, "SCHEMAS_ROOT", tampered / "schemas")
    monkeypatch.setattr(generator, "FIXTURES_ROOT", tampered / "fixtures")

    findings = generator.check_resource_inventory()
    assert any("schema count mismatch" in finding for finding in findings)


def test_an_extra_fixture_fails_the_count_and_digest_check(tmp_path: Path, monkeypatch) -> None:
    tampered = tmp_path / "chat" / "v1"
    shutil.copytree(REPO_ROOT / "contracts" / "chat" / "v1", tampered)
    (tampered / "fixtures" / "valid" / "extra-not-approved.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(generator, "CANONICAL_ROOT", tampered)
    monkeypatch.setattr(generator, "SCHEMAS_ROOT", tampered / "schemas")
    monkeypatch.setattr(generator, "FIXTURES_ROOT", tampered / "fixtures")

    findings = generator.check_resource_inventory()
    assert any("fixture tree file count mismatch" in finding for finding in findings)


# --------------------------------------------------------------------------
# Rendered content sanity
# --------------------------------------------------------------------------


def test_schema_names_and_ids_are_exact() -> None:
    names, ids = generator.schema_names_and_ids()
    assert len(names) == 13
    assert names == tuple(sorted(names))
    for name in names:
        assert ids[name] == f"https://contracts.omnivia.dev/chat/v1/{name}.schema.json"


def test_extract_vocabularies_returns_the_governed_counts() -> None:
    vocab = generator.extract_vocabularies()
    assert len(vocab["CHAT_COMMAND_NAMES"]) == 30
    assert len(vocab["ERROR_CODES"]) == 24
    assert len(vocab["CHAT_EVENT_TYPES"]) == 15
    assert len(vocab["F2A_PROVIDER_EVENT_TYPES"]) == 20
    assert len(vocab["F2A_FINISH_REASONS"]) == 7
    assert len(vocab["F2A_PROVIDER_ERROR_CODES"]) == 16


# --------------------------------------------------------------------------
# Schema-derived validation metadata
# --------------------------------------------------------------------------

_ASSERTION_HOLDERS = ("properties", "items", "not", "if", "then", "else", "propertyNames",
                      "additionalProperties", "allOf", "anyOf", "oneOf")


def _walk(node: object) -> Iterator[dict[str, object]]:
    """Every subschema object reachable inside one metadata entry."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for keyword, value in node.items():
            if keyword in _ASSERTION_HOLDERS:
                yield from _walk(list(value.values()) if keyword == "properties" else value)


def test_the_validation_metadata_covers_every_bounded_record() -> None:
    schemas = generator.extract_validation_schemas()
    assert set(generator.VALIDATION_ROOTS) == {
        "ChatEvent", "CommandError", "CommandResultEnvelope", "ProviderInvocationRecord",
        "ProviderInvocationRequest", "ResnapshotResponse",
    }
    for root in generator.VALIDATION_ROOTS.values():
        assert root in schemas


def test_every_reference_in_the_validation_metadata_resolves() -> None:
    """A dangling ``$ref`` would be an emission gate that raises ``KeyError``
    instead of validating, so the closure has to be closed."""
    schemas = generator.extract_validation_schemas()
    for entry in schemas.values():
        for subschema in _walk(entry):
            reference = subschema.get("$ref")
            if reference is not None:
                assert reference in schemas, reference


def test_the_validation_metadata_carries_only_keywords_the_validator_implements() -> None:
    schemas = generator.extract_validation_schemas()
    used = {keyword for entry in schemas.values() for sub in _walk(entry) for keyword in sub}
    assert used <= generator.SUPPORTED_VALIDATION_KEYWORDS
    assert used.isdisjoint(generator.ANNOTATION_KEYWORDS), "annotations are not assertions"
    assert generator.SUPPORTED_VALIDATION_KEYWORDS == set(_validation._IMPLEMENTED_KEYWORDS)


def test_an_unimplemented_keyword_fails_generation_closed(tmp_path: Path, monkeypatch) -> None:
    """The gate that stops the codec from silently under-validating: a schema
    keyword the runtime validator cannot evaluate must not reach the metadata."""
    tampered = tmp_path / "chat" / "v1"
    shutil.copytree(REPO_ROOT / "contracts" / "chat" / "v1", tampered)
    target = tampered / "schemas" / "events.schema.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    document["$defs"]["ResnapshotResponse"]["dependentRequired"] = {"reason": ["graphRevision"]}
    target.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(generator, "SCHEMAS_ROOT", tampered / "schemas")

    with pytest.raises(generator.ChatContractError, match="dependentRequired"):
        generator.extract_validation_schemas()


def test_generation_is_deterministic() -> None:
    """The metadata is rendered as a Python literal, so its text has to be a
    function of the schema bytes alone -- otherwise ``--check`` would flap."""
    assert generator.generate() == generator.generate()
