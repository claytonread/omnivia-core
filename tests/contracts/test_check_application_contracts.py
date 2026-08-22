"""Tests for the Application Contract v1 conformance gate (ADR-038).

Loads ``scripts/check-application-contracts.py`` as a module (its filename is
not a valid Python identifier, so it cannot be imported normally) and
exercises its individual checks against the real repository layout, the same
pattern ``tests/test_package_boundaries.py`` uses for the sibling gate.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-application-contracts.py"

# The checker is loaded from a path, so every attribute read off it is untyped.
# Each one this module actually calls is bound through a declared signature
# instead, so the tests type-check as strictly as the code they exercise.
Check = Callable[[], list[str]]
SchemaLoader = Callable[[str], dict[str, Any]]


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_application_contracts", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_check_module()


def test_no_conformance_violations() -> None:
    assert checker.run_checks() == []


def test_schemas_are_valid_draft_2020_12() -> None:
    assert checker.check_schemas_are_valid_draft_2020_12() == []


def test_all_refs_resolve() -> None:
    assert checker.check_all_refs_resolve() == []


def test_registry_matches_source_schemas() -> None:
    assert checker.check_registry_matches_source_schemas() == []


def test_fixture_manifest_is_complete() -> None:
    assert checker.check_fixture_manifest_is_complete() == []


def test_fixtures_match_declared_schema_validity() -> None:
    assert checker.check_fixtures_match_declared_schema_validity() == []


def test_fixture_semantics() -> None:
    assert checker.check_fixture_semantics() == []


def test_contract_package_has_no_forbidden_imports() -> None:
    assert checker.check_contract_package_has_no_forbidden_imports() == []


def test_generated_artifacts_match_schemas() -> None:
    assert checker.check_generated_artifacts_match_schemas() == []


def test_schema_identity() -> None:
    assert checker.check_schema_identity() == []


def test_registry_sources_are_exact() -> None:
    assert checker.check_registry_sources_are_exact() == []


def test_fixture_manifest_entries_are_well_formed() -> None:
    assert checker.check_fixture_manifest_entries_are_well_formed() == []


def test_fixture_manifest_matches_frozen_mapping() -> None:
    assert checker.check_fixture_manifest_matches_frozen_mapping() == []


def test_fixture_tolerant_decode_matches_declared() -> None:
    assert checker.check_fixture_tolerant_decode_matches_declared() == []


def test_fixture_semantics_rejects_unknown_semantic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checker,
        "_load_manifest",
        lambda: [
            {"id": "bogus", "file": "compatible-negotiation.json", "semantic": "not_a_real_check"}
        ],
    )
    assert checker.check_fixture_semantics() == [
        "fixture 'bogus': unknown semantic key 'not_a_real_check'"
    ]


def test_fixtures_match_declared_schema_validity_catches_a_wrong_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checker,
        "_load_manifest",
        lambda: [
            {
                "id": "compatible-negotiation",
                "file": "compatible-negotiation.json",
                "envelope": "ResponseEnvelope",
                "schema_valid": False,
            }
        ],
    )
    findings = checker.check_fixtures_match_declared_schema_validity()
    assert len(findings) == 1
    assert "compatible-negotiation" in findings[0]


def test_fixture_tolerant_decode_matches_declared_catches_a_wrong_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checker,
        "_load_manifest",
        lambda: [
            {
                "id": "compatible-negotiation",
                "file": "compatible-negotiation.json",
                "envelope": "ResponseEnvelope",
                "tolerant_decode": False,
            }
        ],
    )
    findings = checker.check_fixture_tolerant_decode_matches_declared()
    assert len(findings) == 1
    assert "compatible-negotiation" in findings[0]


def test_fixture_tolerant_decode_matches_declared_reports_a_definition_that_is_not_a_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest entry naming a scalar is a finding, not an `AttributeError`.

    `definition` is resolved by name off the generated module, so a name that resolves to a
    scalar alias -- `AttemptStatus` is `str` -- has no `from_wire` to reach through. The gate
    exists to report malformed manifests, and one that dies on the way to reporting one
    reports nothing at all, including everything it had not checked yet.
    """
    monkeypatch.setattr(
        checker,
        "_load_manifest",
        lambda: [
            {
                "id": "runtime-run-replay",
                "file": "runtime-run-replay.json",
                "definition": "AttemptStatus",
                "tolerant_decode": True,
            }
        ],
    )
    findings = checker.check_fixture_tolerant_decode_matches_declared()
    assert len(findings) == 1
    assert "AttemptStatus" in findings[0]


@pytest.mark.parametrize("envelope", [[], {}, 7, True])
def test_fixture_target_ref_reports_a_malformed_envelope_rather_than_raising(
    envelope: Any,
) -> None:
    """A manifest entry whose `envelope` is not a string is a finding, not a `TypeError`.

    The manifest is untrusted input to this gate, and an unhashable value like `[]` makes a
    bare `envelope not in _ENVELOPE_REFS` raise. A gate that dies while reporting one
    malformed entry reports nothing at all, including everything it had not checked yet.
    """
    ref, reason = checker._fixture_target_ref({"envelope": envelope})
    assert ref is None
    assert "unknown envelope" in reason


def test_registry_sources_are_exact_catches_a_reordered_list(monkeypatch: pytest.MonkeyPatch) -> None:
    real_load_schema = cast("SchemaLoader", checker._load_schema)

    def _load_schema(name: str) -> dict[str, Any]:
        document = real_load_schema(name)
        if name == checker.REGISTRY_SCHEMA:
            document = dict(document)
            document["x-omnivia-schema-sources"] = list(reversed(document["x-omnivia-schema-sources"]))
        return document

    monkeypatch.setattr(checker, "_load_schema", _load_schema)
    findings = checker.check_registry_sources_are_exact()
    assert len(findings) == 1
    assert "x-omnivia-schema-sources" in findings[0]


def test_schema_identity_catches_a_wrong_id(monkeypatch: pytest.MonkeyPatch) -> None:
    real_load_schema = cast("SchemaLoader", checker._load_schema)

    def _load_schema(name: str) -> dict[str, Any]:
        document = real_load_schema(name)
        if name == "common":
            document = dict(document)
            document["$id"] = "https://contracts.omnivia.dev/application/v1/wrong.schema.json"
        return document

    monkeypatch.setattr(checker, "_load_schema", _load_schema)
    findings = checker.check_schema_identity()
    assert len(findings) == 1
    assert "$id" in findings[0]


def _real_fixture_entries() -> list[dict[str, Any]]:
    return copy.deepcopy(cast("list[dict[str, Any]]", checker._load_manifest()))


def test_fixture_manifest_entries_are_well_formed_catches_a_missing_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _real_fixture_entries()
    del entries[0]["id"]

    findings = _with_manifest(
        monkeypatch, entries, checker.check_fixture_manifest_entries_are_well_formed
    )
    assert any("missing a nonempty string 'id'" in finding for finding in findings)


def test_fixture_manifest_entries_are_well_formed_catches_a_duplicate_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _real_fixture_entries()
    entries[1]["id"] = entries[0]["id"]

    findings = _with_manifest(
        monkeypatch, entries, checker.check_fixture_manifest_entries_are_well_formed
    )
    assert any("duplicate fixture id" in finding for finding in findings)


def test_fixture_manifest_is_complete_catches_a_duplicate_file(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _real_fixture_entries()
    entries[1]["file"] = entries[0]["file"]

    findings = _with_manifest(monkeypatch, entries, checker.check_fixture_manifest_is_complete)
    assert any("listed more than once" in finding for finding in findings)


def test_fixture_manifest_is_complete_catches_an_unlisted_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [entry for entry in _real_fixture_entries() if entry["id"] != "success-response"]

    findings = _with_manifest(monkeypatch, entries, checker.check_fixture_manifest_is_complete)
    assert any("on disk but not in manifest" in finding for finding in findings)
    assert any("success-response.json" in finding for finding in findings)


@pytest.mark.parametrize("flag_name", ["schema_valid", "tolerant_decode"])
def test_fixture_manifest_entries_are_well_formed_catches_a_non_boolean_flag(
    flag_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _real_fixture_entries()
    entries[0][flag_name] = "true"

    findings = _with_manifest(
        monkeypatch, entries, checker.check_fixture_manifest_entries_are_well_formed
    )
    assert any(f"{flag_name!r} must be an explicit boolean" in finding for finding in findings)


def test_fixture_manifest_entries_are_well_formed_catches_a_missing_semantic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _real_fixture_entries()
    del entries[0]["semantic"]

    findings = _with_manifest(
        monkeypatch, entries, checker.check_fixture_manifest_entries_are_well_formed
    )
    assert any("'semantic' must be a nonempty string" in finding for finding in findings)


def test_fixture_manifest_entries_are_well_formed_catches_a_non_string_semantic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _real_fixture_entries()
    entries[0]["semantic"] = 12345

    findings = _with_manifest(
        monkeypatch, entries, checker.check_fixture_manifest_entries_are_well_formed
    )
    assert any("'semantic' must be a nonempty string" in finding for finding in findings)


def test_fixture_manifest_matches_frozen_mapping_catches_a_swapped_semantic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _real_fixture_entries()
    for entry in entries:
        if entry["id"] == "compatible-negotiation":
            entry["semantic"] = "plain_not_found"

    findings = _with_manifest(monkeypatch, entries, checker.check_fixture_manifest_matches_frozen_mapping)
    assert any("compatible-negotiation" in finding for finding in findings)


def test_fixture_manifest_matches_frozen_mapping_catches_a_deleted_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [entry for entry in _real_fixture_entries() if entry["id"] != "capability-denial"]

    findings = _with_manifest(monkeypatch, entries, checker.check_fixture_manifest_matches_frozen_mapping)
    assert any(
        "missing from the manifest" in finding and "capability-denial" in finding for finding in findings
    )


def _with_manifest(
    monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]], check: Check
) -> list[str]:
    monkeypatch.setattr(checker, "_load_manifest", lambda: entries)
    return check()


# --------------------------------------------------------------------------
# compatible-negotiation: every frozen negotiated fact must be checked, so
# each independently mutated fact has to be detected on its own.
# --------------------------------------------------------------------------

COMPATIBLE_NEGOTIATION_FILE = "compatible-negotiation.json"


def _compatible_negotiation_findings(
    monkeypatch: pytest.MonkeyPatch, mutate: Any
) -> list[str]:
    """Run the ``effective_capabilities_match`` check over a mutated copy of the fixture."""
    from omnivia_core.contracts.v1 import codec, compatibility

    document = checker._fixture_document(COMPATIBLE_NEGOTIATION_FILE)
    mutate(document)
    monkeypatch.setattr(checker, "_fixture_document", lambda file_name: document)
    check = checker._semantic_checks(codec, compatibility)["effective_capabilities_match"]
    return cast("list[str]", check(COMPATIBLE_NEGOTIATION_FILE))


def _version(document: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", document["metadata"]["version"])


def _negotiated(document: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", _version(document)["compatibility"])


def _capabilities(document: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", _version(document)["capabilities"])


def _make_error_response(document: dict[str, Any]) -> None:
    document.pop("result")
    document["error"] = {
        "code": "not_found",
        "message": "No record with the given id exists in this workspace.",
        "retry_class": "non_retryable",
    }


MUTATIONS: dict[str, Any] = {
    "status": lambda doc: _negotiated(doc).update(status="compatible_with_deprecations"),
    "selected_api_version": lambda doc: _negotiated(doc).update(selected_api_version="1.3"),
    "selected_workspace_version": lambda doc: _negotiated(doc).update(
        selected_workspace_version="1.1"
    ),
    "api_version_in_force": lambda doc: _version(doc).update(api_version="1.3"),
    "workspace_format_version_in_force": lambda doc: _version(doc).update(
        workspace_format_version="1.1"
    ),
    "supported_api_versions_maximum": lambda doc: _negotiated(doc)["supported_api_versions"].update(
        maximum="1.4"
    ),
    "supported_api_versions_minimum": lambda doc: _negotiated(doc)["supported_api_versions"].update(
        minimum="1.1"
    ),
    "supported_workspace_versions_maximum": lambda doc: _negotiated(doc)[
        "supported_workspace_versions"
    ].update(maximum="1.1"),
    "upgrade_state_value": lambda doc: _negotiated(doc)["upgrade_state"].update(value="required"),
    "upgrade_state_target_version": lambda doc: _negotiated(doc)["upgrade_state"].update(
        target_version="1.3"
    ),
    "deprecations": lambda doc: _negotiated(doc)["deprecations"].append(
        {"id": "memory.read.v1", "since": "1.2"}
    ),
    "effective_narrowed": lambda doc: _capabilities(doc)["effective"].pop(),
    "effective_widened": lambda doc: _capabilities(doc)["effective"].append(
        {"id": "memory.export", "version": "1.0"}
    ),
    "effective_duplicated": lambda doc: _capabilities(doc)["effective"].append(
        {"id": "memory.read", "version": "1.1"}
    ),
    "granted_narrowed": lambda doc: _capabilities(doc)["granted"].pop(),
    "error_branch": _make_error_response,
}


def test_compatible_negotiation_passes_unmutated(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _compatible_negotiation_findings(monkeypatch, lambda document: None) == []


@pytest.mark.parametrize("mutation_name", sorted(MUTATIONS))
def test_compatible_negotiation_detects_each_mutated_fact(
    mutation_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _compatible_negotiation_findings(monkeypatch, MUTATIONS[mutation_name])
    assert findings, f"mutating {mutation_name} went undetected"


def test_compatible_negotiation_reports_the_effective_intersection_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The intersection finding must survive as its own message, not be folded into the
    decode failure the production codec would also raise for the same document.
    """
    from omnivia_core.contracts.v1 import codec, compatibility

    document = checker._fixture_document(COMPATIBLE_NEGOTIATION_FILE)
    _capabilities(document)["effective"].pop()
    monkeypatch.setattr(checker, "_fixture_document", lambda file_name: document)
    monkeypatch.setattr(codec, "decode_success_response", codec.response_envelope_from_wire)

    check = checker._semantic_checks(codec, compatibility)["effective_capabilities_match"]
    findings = check(COMPATIBLE_NEGOTIATION_FILE)
    assert findings == [
        f"{COMPATIBLE_NEGOTIATION_FILE}: stated 'effective' does not equal supported ∩ granted"
    ]


# --------------------------------------------------------------------------
# Import-boundary checks: absolute imports, relative-import resolution, and
# constant-string dynamic-import escapes, exercised against synthetic files
# rather than the real repository tree.
# --------------------------------------------------------------------------


def _write_contracts_tree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    src_root = tmp_path / "src"
    contracts_root = src_root / "omnivia_core" / "contracts"
    for relative_path, content in files.items():
        path = contracts_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return src_root, contracts_root


def _check_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str]) -> list[str]:
    src_root, contracts_root = _write_contracts_tree(tmp_path, files)
    monkeypatch.setattr(checker, "SRC_ROOT", src_root)
    monkeypatch.setattr(checker, "CONTRACTS_SRC", contracts_root)
    check = cast("Check", checker.check_contract_package_has_no_forbidden_imports)
    return check()


def test_import_boundary_allows_real_positive_local_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "from . import v1\n",
            "v1/__init__.py": (
                "from . import codec, generated\n"
                "from .codec import decode_response\n"
            ),
            "v1/generated.py": "import json\nfrom math import isfinite\n",
            "v1/codec.py": (
                "import json\n"
                "from omnivia_core.contracts.v1.generated import isfinite\n"
                "from ..v1 import generated\n"
            ),
        },
    )
    assert findings == []


def test_import_boundary_rejects_absolute_runtime_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": "import omnivia_core_runtime\n"},
    )
    assert len(findings) == 1
    assert "omnivia_core_runtime" in findings[0]


def test_import_boundary_rejects_non_stdlib_absolute_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": "import requests\n"},
    )
    assert len(findings) == 1
    assert "requests" in findings[0]


def test_import_boundary_rejects_relative_import_escaping_the_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": "", "v1/generated.py": "from ...omnivia_core_runtime import x\n"},
    )
    assert len(findings) == 1
    assert "relative import" in findings[0] or "forbidden relative import" in findings[0]


def test_import_boundary_rejects_relative_import_climbing_past_the_source_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": "from ........ import x\n"},
    )
    assert len(findings) == 1
    assert "escapes above" in findings[0]


def test_import_boundary_rejects_dynamic_import_escape_via_dunder_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": '__import__("requests")\n'},
    )
    assert len(findings) == 1
    assert "dynamic import escape" in findings[0]


def test_import_boundary_rejects_dynamic_import_escape_via_importlib(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": 'import importlib\nimportlib.import_module("requests")\n',
        },
    )
    assert len(findings) == 1
    assert "dynamic import escape" in findings[0]


def test_import_boundary_rejects_non_literal_dynamic_import_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": 'name = "os"\n__import__(name)\n'},
    )
    assert len(findings) == 1
    assert "non-literal argument" in findings[0]


def test_import_boundary_allows_stdlib_dynamic_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": 'import importlib\nimportlib.import_module("json")\n',
        },
    )
    assert findings == []


# --------------------------------------------------------------------------
# The same escape hatch under a rename: `import importlib as il` and
# `from importlib import import_module as load` are exactly as dynamic as the
# canonical spellings, and must be detected the same way.
# --------------------------------------------------------------------------


def test_import_boundary_rejects_dynamic_import_escape_via_an_aliased_importlib(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": 'import importlib as il\nil.import_module("requests")\n',
        },
    )
    assert len(findings) == 1
    assert "dynamic import escape" in findings[0]
    assert "requests" in findings[0]


def test_import_boundary_rejects_dynamic_import_escape_via_an_aliased_import_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": (
                "from importlib import import_module as load\n" 'load("requests")\n'
            ),
        },
    )
    assert len(findings) == 1
    assert "dynamic import escape" in findings[0]
    assert "requests" in findings[0]


def test_import_boundary_rejects_a_non_literal_argument_to_an_aliased_dynamic_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": 'import importlib as il\nname = "os"\nil.import_module(name)\n',
        },
    )
    assert len(findings) == 1
    assert "non-literal argument" in findings[0]


def test_import_boundary_allows_an_aliased_dynamic_import_of_a_stdlib_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": 'import importlib as il\nil.import_module("json")\n',
        },
    )
    assert findings == []


def test_import_boundary_allows_an_aliased_dynamic_import_of_a_local_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": (
                "from importlib import import_module as load\n"
                'load("omnivia_core.contracts.v1.generated")\n'
            ),
        },
    )
    assert findings == []


def test_import_boundary_leaves_an_unrelated_local_function_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a name actually bound to the dynamic-import escape hatch is treated as one: a
    local helper that happens to share the alias is ordinary code.
    """
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {
            "__init__.py": "",
            "v1/__init__.py": 'def load(name: str) -> str:\n    return name\n\n\nload("requests")\n',
        },
    )
    assert findings == []


# --------------------------------------------------------------------------
# Every statically resolvable spelling of the escape hatch. A dynamic import is
# only recognizable by name, and the name can reach the call site through an
# import alias, `builtins`, a plain assignment (or a chain of them), or a
# literal `getattr` off the module. Each spelling is the same escape, so each
# must be rejected on its own -- and each must leave the corresponding
# *allowed* import (stdlib, or a module inside this package) alone, or the gate
# would be enforcing "do not import dynamically" instead of "do not import
# outside the boundary".
# --------------------------------------------------------------------------

DYNAMIC_IMPORT_ESCAPES: dict[str, str] = {
    "assignment_alias": (
        "import importlib\nload = importlib.import_module\nload(\"requests\")\n"
    ),
    "dunder_import_from_builtins": (
        "from builtins import __import__ as load\nload(\"requests\")\n"
    ),
    "builtins_attribute": 'import builtins\nbuiltins.__import__("requests")\n',
    "aliased_builtins_attribute": 'import builtins as b\nb.__import__("requests")\n',
    "importlib_dunder_import": 'import importlib\nimportlib.__import__("requests")\n',
    "getattr_of_import_module": (
        'import importlib\ngetattr(importlib, "import_module")("requests")\n'
    ),
    "getattr_of_dunder_import": (
        'import builtins\ngetattr(builtins, "__import__")("requests")\n'
    ),
    "chained_assignment_alias": (
        "import importlib\n"
        "first = importlib.import_module\n"
        "then = first\n"
        'then("requests")\n'
    ),
    "assignment_alias_of_a_getattr": (
        "import importlib\n"
        'grab = getattr(importlib, "import_module")\n'
        'grab("requests")\n'
    ),
    "assignment_alias_of_the_module": (
        'import importlib\nil = importlib\nil.import_module("requests")\n'
    ),
}


@pytest.mark.parametrize("form", sorted(DYNAMIC_IMPORT_ESCAPES))
def test_import_boundary_rejects_every_resolvable_dynamic_import_escape(
    form: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": DYNAMIC_IMPORT_ESCAPES[form]},
    )
    assert len(findings) == 1, f"{form!r} produced {findings!r}"
    assert "dynamic import escape" in findings[0]
    assert "requests" in findings[0]


DYNAMIC_IMPORT_ALLOWED: dict[str, str] = {
    "assignment_alias_of_stdlib": (
        'import importlib\nload = importlib.import_module\nload("json")\n'
    ),
    "dunder_import_from_builtins_of_stdlib": (
        'from builtins import __import__ as load\nload("json")\n'
    ),
    "builtins_attribute_of_stdlib": 'import builtins\nbuiltins.__import__("json")\n',
    "getattr_of_import_module_for_stdlib": (
        'import importlib\ngetattr(importlib, "import_module")("json")\n'
    ),
    "getattr_of_dunder_import_for_stdlib": (
        'import builtins\ngetattr(builtins, "__import__")("json")\n'
    ),
    "chained_assignment_alias_of_a_local_module": (
        "import importlib\n"
        "first = importlib.import_module\n"
        "then = first\n"
        'then("omnivia_core.contracts.v1.generated")\n'
    ),
    "getattr_of_import_module_for_a_local_module": (
        "import importlib\n"
        'getattr(importlib, "import_module")("omnivia_core.contracts.v1.generated")\n'
    ),
    "assignment_alias_of_a_local_module": (
        "import importlib\n"
        "load = importlib.import_module\n"
        'load("omnivia_core.contracts.v1")\n'
    ),
    "dunder_import_from_builtins_of_a_local_module": (
        'from builtins import __import__ as load\nload("omnivia_core.contracts.v1")\n'
    ),
    "builtins_attribute_of_a_local_module": (
        'import builtins\nbuiltins.__import__("omnivia_core.contracts.v1")\n'
    ),
}


@pytest.mark.parametrize("form", sorted(DYNAMIC_IMPORT_ALLOWED))
def test_import_boundary_allows_every_resolvable_in_boundary_dynamic_import(
    form: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": DYNAMIC_IMPORT_ALLOWED[form]},
    )
    assert findings == [], f"{form!r} produced {findings!r}"


NON_LITERAL_DYNAMIC_IMPORTS: dict[str, str] = {
    "assignment_alias": (
        'import importlib\nload = importlib.import_module\nname = "os"\nload(name)\n'
    ),
    "chained_assignment_alias": (
        "import importlib\n"
        "first = importlib.import_module\n"
        "then = first\n"
        'name = "os"\n'
        "then(name)\n"
    ),
    "builtins_attribute": 'import builtins\nname = "os"\nbuiltins.__import__(name)\n',
    "getattr_of_import_module": (
        'import importlib\nname = "os"\ngetattr(importlib, "import_module")(name)\n'
    ),
    "computed_module_name": (
        "import importlib\n"
        'def _target(prefix: str) -> str:\n    return prefix + ".client"\n\n\n'
        "importlib.import_module(_target(\"requests\"))\n"
    ),
    "no_argument_at_all": "import importlib\nimportlib.import_module()\n",
}


@pytest.mark.parametrize("form", sorted(NON_LITERAL_DYNAMIC_IMPORTS))
def test_import_boundary_fails_closed_on_a_non_literal_dynamic_import_argument(
    form: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A module name this script cannot read is a module name it cannot vouch for.

    Once the callee is known to be a by-name importer, an argument that is not a
    string literal is rejected rather than assumed in-boundary -- the checker
    never evaluates the inspected code to find out what the name would be.
    """
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": NON_LITERAL_DYNAMIC_IMPORTS[form]},
    )
    assert len(findings) == 1, f"{form!r} produced {findings!r}"
    assert "non-literal argument" in findings[0]


AMBIGUOUS_IMPORT_REBINDINGS: dict[str, str] = {
    "importer_rebound_in_a_branch": (
        "import importlib\n"
        "if _USE_IMPORTLIB:\n"
        "    load = importlib.import_module\n"
        "else:\n"
        "    load = str\n"
        'load("requests")\n'
    ),
    "importer_rebound_by_an_unreadable_binding": (
        "import importlib\n"
        "load = importlib.import_module\n"
        "load, other = _pair\n"
        'load("requests")\n'
    ),
    "module_alias_rebound_in_a_branch": (
        "import importlib\n"
        "if _USE_IMPORTLIB:\n"
        "    il = importlib\n"
        "else:\n"
        "    il = str\n"
        'il.import_module("requests")\n'
    ),
    "getattr_with_a_computed_attribute": (
        'import importlib\nname = "import_module"\ngetattr(importlib, name)("requests")\n'
    ),
}


@pytest.mark.parametrize("form", sorted(AMBIGUOUS_IMPORT_REBINDINGS))
def test_import_boundary_fails_closed_on_an_ambiguous_import_related_name(
    form: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name that *may* still be the importer is treated as if it is.

    This script does no flow analysis, so a name bound to the escape hatch in one
    place and to something else in another cannot be proven safe at the call
    site. Proving it would mean running the file; the only sound alternative is
    to keep treating it as an importer.
    """
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": AMBIGUOUS_IMPORT_REBINDINGS[form]},
    )
    assert len(findings) == 1, f"{form!r} produced {findings!r}"
    assert "dynamic import escape" in findings[0]
    assert "requests" in findings[0]


ORDINARY_CODE_NOT_ABOUT_IMPORTING: dict[str, str] = {
    "getattr_on_an_unrelated_module": 'import json\ngetattr(json, "dumps")({})\n',
    "getattr_with_a_computed_attribute_on_an_unrelated_module": (
        'import json\nname = "dumps"\ngetattr(json, name)({})\n'
    ),
    "getattr_with_a_default": 'import json\ngetattr(json, "dumps", None)({})\n',
    "an_ordinary_rebound_name": (
        'import json\ndump = json.dumps\nif _COMPACT:\n    dump = repr\ndump({})\n'
    ),
    "a_local_helper_sharing_the_module_name": (
        "def importlib(name: str) -> str:\n    return name\n\n\n"
        'importlib("requests")\n'
    ),
}


@pytest.mark.parametrize("form", sorted(ORDINARY_CODE_NOT_ABOUT_IMPORTING))
def test_import_boundary_leaves_code_unrelated_to_importing_alone(
    form: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-closed applies only to names that could actually reach an importer.

    Ordinary reflection and ordinary rebinding are not this gate's business; a
    checker that flagged them would push contract code into contorting around it.
    """
    findings = _check_tree(
        monkeypatch,
        tmp_path,
        {"__init__.py": "", "v1/__init__.py": ORDINARY_CODE_NOT_ABOUT_IMPORTING[form]},
    )
    assert findings == [], f"{form!r} produced {findings!r}"


# --------------------------------------------------------------------------
# The canonical schema directory must hold exactly the frozen schema set: an
# extra document is validated by nothing here, yet the wheel would package it.
# --------------------------------------------------------------------------


def _schema_dir_copy(tmp_path: Path) -> Path:
    schema_dir = tmp_path / "schemas"
    shutil.copytree(checker.SCHEMA_DIR, schema_dir)
    return schema_dir


def test_schema_directory_holds_exactly_the_frozen_schemas() -> None:
    check = cast("Check", checker.check_schema_directory_holds_exactly_the_frozen_schemas)
    assert check() == []


def test_schema_directory_check_rejects_an_extra_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema_dir = _schema_dir_copy(tmp_path)
    (schema_dir / "rogue.schema.json").write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(checker, "SCHEMA_DIR", schema_dir)

    check = cast("Check", checker.check_schema_directory_holds_exactly_the_frozen_schemas)
    findings = check()
    assert len(findings) == 1
    assert "rogue.schema.json" in findings[0]


def test_schema_directory_check_rejects_a_missing_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema_dir = _schema_dir_copy(tmp_path)
    (schema_dir / "errors.schema.json").unlink()
    monkeypatch.setattr(checker, "SCHEMA_DIR", schema_dir)

    check = cast("Check", checker.check_schema_directory_holds_exactly_the_frozen_schemas)
    findings = check()
    assert len(findings) == 1
    assert "errors.schema.json" in findings[0]


def test_run_checks_reports_an_extra_schema_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exactness check is actually wired into the gate, and is the only thing that
    catches an added document: every other check reads schemas by frozen name.
    """
    schema_dir = _schema_dir_copy(tmp_path)
    (schema_dir / "rogue.schema.json").write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(checker, "SCHEMA_DIR", schema_dir)

    run_checks = cast("Check", checker.run_checks)
    findings = run_checks()
    assert len(findings) == 1
    assert "rogue.schema.json" in findings[0]


# --------------------------------------------------------------------------
# Operation catalogue: every category of drift must be detected on its own, so
# the gate cannot pass on a catalogue that has quietly stopped matching A2.5.
# --------------------------------------------------------------------------

CatalogueMutation = Callable[[list[dict[str, Any]]], None]

#: The checks that read the *canonical annotation* and judge it on its own terms.
#:
#: `check_operation_catalogue_fixture_matches` is deliberately excluded. It
#: compares the on-disk fixture against whatever the annotation currently says,
#: so *every* annotation mutation trips it -- including one that no semantic
#: check would catch. Running it alongside the others would make the drift matrix
#: below a tautology: it would stay green even if all five semantic checks
#: regressed to `return []`, which is precisely the failure it exists to rule
#: out. The fixture oracle is real and is tested separately, on its own terms.
_SEMANTIC_CATALOGUE_CHECKS: tuple[str, ...] = (
    "check_operation_catalogue_shape",
    "check_operation_catalogue_entries_are_valid",
    "check_operation_catalogue_schema_refs",
    "check_operation_catalogue_postures",
    "check_operation_catalogue_errors",
)

_CATALOGUE_CHECKS: tuple[str, ...] = (
    *_SEMANTIC_CATALOGUE_CHECKS,
    "check_operation_catalogue_fixture_matches",
)


def _patch_catalogue(monkeypatch: pytest.MonkeyPatch, mutate: CatalogueMutation) -> None:
    real_load_schema = cast("SchemaLoader", checker._load_schema)

    def _load_schema(name: str) -> dict[str, Any]:
        document = real_load_schema(name)
        if name == "operations":
            document = copy.deepcopy(document)
            mutate(document[checker.OPERATION_CATALOGUE_ANNOTATION])
        return document

    monkeypatch.setattr(checker, "_load_schema", _load_schema)


def _with_catalogue(
    monkeypatch: pytest.MonkeyPatch,
    mutate: CatalogueMutation,
    checks: tuple[str, ...] = _SEMANTIC_CATALOGUE_CHECKS,
) -> list[str]:
    """Run the semantic catalogue checks against a mutated canonical annotation."""
    _patch_catalogue(monkeypatch, mutate)
    findings: list[str] = []
    for check_name in checks:
        findings += cast("Check", getattr(checker, check_name))()
    return findings


def _entry(catalogue: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(entry for entry in catalogue if entry["name"] == name)


#: Each case pairs a mutation with the substring the finding it *should* produce
#: must contain. Asserting only that "some finding appeared" would let a mutation
#: be credited to a check that fired for an unrelated reason -- several of these
#: entries do legitimately trip more than one rule, and the point of the matrix is
#: that the rule named by the label is one of them.
CATALOGUE_MUTATIONS: dict[str, tuple[CatalogueMutation, str]] = {
    "removed-operation": (
        lambda c: c.remove(_entry(c, "memory.list")),
        "missing operation(s) ['memory.list']",
    ),
    "added-operation": (
        lambda c: c.append({**copy.deepcopy(c[0]), "name": "memory.zzz"}),
        "unknown operation(s) ['memory.zzz']",
    ),
    "service-probe": (
        lambda c: c.insert(0, {**copy.deepcopy(c[0]), "name": "service.health"}),
        "are not application operations",
    ),
    "job-resume": (
        lambda c: c.insert(10, {**copy.deepcopy(c[0]), "name": "job.resume"}),
        "are not application operations",
    ),
    "reordered": (lambda c: c.insert(0, c.pop()), "entries must be sorted by name"),
    "duplicate-name": (
        lambda c: c.__setitem__(1, copy.deepcopy(c[0])),
        "duplicate operation name(s)",
    ),
    "undeclared-field": (
        lambda c: c[0].__setitem__("surprise", True),
        "not a valid OperationMetadata",
    ),
    "wrong-input-ref": (
        lambda c: _entry(c, "memory.get").__setitem__(
            "input_schema_ref", f"{checker.BASE_URI}memory.schema.json#/$defs/MemoryListInput"
        ),
        "input_schema_ref: must bind",
    ),
    "unresolvable-ref": (
        lambda c: _entry(c, "memory.get").__setitem__(
            "result_schema_ref", f"{checker.BASE_URI}memory.schema.json#/$defs/NoSuchDefinition"
        ),
        "unresolvable reference",
    ),
    "relative-ref": (
        lambda c: _entry(c, "memory.get").__setitem__(
            "input_schema_ref", "#/$defs/MemoryGetInput"
        ),
        "must be an absolute",
    ),
    "wrong-terminal-result-ref": (
        lambda c: _entry(c, "import.start")["job"].__setitem__(
            "terminal_result_schema_ref",
            f"{checker.BASE_URI}jobs.schema.json#/$defs/JobGetResult",
        ),
        "terminal_result_schema_ref: must bind",
    ),
    "unresolvable-terminal-result-ref": (
        lambda c: _entry(c, "import.start")["job"].__setitem__(
            "terminal_result_schema_ref",
            f"{checker.BASE_URI}jobs.schema.json#/$defs/NoSuchTerminalResult",
        ),
        "unresolvable reference",
    ),
    "scope-kind-drift": (
        lambda c: _entry(c, "memory.get")["scope"].__setitem__("scope_kind", "installation"),
        "the installation-scoped operations must be exactly",
    ),
    "required-scope-drift": (
        lambda c: _entry(c, "memory.get")["scope"].__setitem__(
            "required_scopes", ["memory:write"]
        ),
        "[memory.get].scope: must be",
    ),
    "side-effect-drift": (
        lambda c: _entry(c, "memory.get")["scope"].__setitem__("side_effect", "update"),
        "[memory.get].scope: must be",
    ),
    "capability-id-drift": (
        lambda c: _entry(c, "memory.get")["required_capability"].__setitem__("id", "graph.read"),
        "[memory.get].required_capability: must be",
    ),
    "capability-version-drift": (
        lambda c: _entry(c, "memory.get")["required_capability"].__setitem__(
            "minimum_version", "2.0"
        ),
        "[memory.get].required_capability: must be",
    ),
    "capability-optional": (
        lambda c: _entry(c, "memory.get")["required_capability"].__setitem__("required", False),
        "[memory.get].required_capability: must be",
    ),
    "job-fields-on-synchronous": (
        lambda c: _entry(c, "memory.get")["job"].__setitem__("job_kind", "ingestion.import"),
        "a synchronous operation must omit",
    ),
    "may-return-job": (
        lambda c: _entry(c, "memory.get")["job"].__setitem__(
            "completion_mode", "may_return_job"
        ),
        "'may_return_job' does not occur in the v1 catalogue",
    ),
    "missing-job-fields": (
        lambda c: _entry(c, "import.start")["job"].pop("job_kind"),
        "an always_returns_job operation must declare",
    ),
    "second-job-starting-operation": (
        lambda c: _entry(c, "memory.create")["job"].update(
            {
                "completion_mode": "always_returns_job",
                "job_kind": "ingestion.import",
                "terminal_result_schema_ref": (
                    f"{checker.BASE_URI}jobs.schema.json#/$defs/ImportCompletionResult"
                ),
            }
        ),
        "the durable-job-starting operations must be exactly",
    ),
    "page-size-drift": (
        lambda c: _entry(c, "memory.list")["pagination"].__setitem__("max_page_size", 500),
        "max_page_size must be the integer 1000",
    ),
    "page-size-as-float": (
        lambda c: _entry(c, "memory.list")["pagination"].__setitem__("max_page_size", 1000.0),
        "max_page_size must be the integer 1000",
    ),
    "page-size-without-pagination": (
        lambda c: _entry(c, "memory.get")["pagination"].__setitem__("max_page_size", 1000),
        "a non-paginated operation must omit max_page_size",
    ),
    "pagination-added": (
        lambda c: _entry(c, "memory.get")["pagination"].__setitem__("paginated", True),
        "the paginated operations must be exactly",
    ),
    "idempotency-not-required": (
        lambda c: _entry(c, "memory.create")["idempotency"].__setitem__("required", False),
        "[memory.create].idempotency: must be",
    ),
    "idempotency-required-unsupported": (
        lambda c: _entry(c, "memory.create")["idempotency"].__setitem__(
            "supports_idempotency_key", False
        ),
        "a required idempotency key must also be supported",
    ),
    "mutation-marked-safe-to-retry": (
        lambda c: _entry(c, "memory.create")["idempotency"].__setitem__("safe_to_retry", True),
        "is not safe to retry without one",
    ),
    "read-takes-idempotency-key": (
        lambda c: _entry(c, "memory.get")["idempotency"].__setitem__(
            "supports_idempotency_key", True
        ),
        "[memory.get].idempotency: must be",
    ),
    "precondition-added": (
        lambda c: _entry(c, "memory.create")["precondition"].__setitem__(
            "supports_mutation_precondition", True
        ),
        "the mutation-precondition operations must be exactly",
    ),
    "precondition-removed": (
        lambda c: _entry(c, "record.supersede")["precondition"].__setitem__("required", False),
        "[record.supersede].precondition: must be",
    ),
    "precondition-required-unsupported": (
        lambda c: _entry(c, "memory.create")["precondition"].__setitem__("required", True),
        "a required mutation precondition must also be supported",
    ),
    "not-audited": (
        lambda c: _entry(c, "memory.get")["audit"].__setitem__("audited", False),
        "must be audited",
    ),
    "audit-category-drift": (
        lambda c: _entry(c, "memory.get")["audit"].__setitem__("audit_category", "mutation"),
        "[memory.get].audit: must be",
    ),
    "unsorted-errors": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].reverse(),
        "allowed_errors: must be sorted",
    ),
    "duplicate-error": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].append("not_found"),
        "duplicate code(s)",
    ),
    "unknown-error": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].append("zzz_invented"),
        "do not resolve through x-omnivia-error-catalogue",
    ),
    "emptied-errors": (
        lambda c: _entry(c, "memory.get").__setitem__("allowed_errors", []),
        "must be a non-empty array",
    ),
    "missing-invalid-request": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].remove("invalid_request"),
        "must be allowed",
    ),
    "conflict-on-job-cancel": (
        lambda c: _entry(c, "job.cancel")["allowed_errors"].insert(4, "conflict"),
        "[job.cancel].allowed_errors: must not allow 'conflict'",
    ),
    "conflict-on-job-retry": (
        lambda c: _entry(c, "job.retry")["allowed_errors"].insert(4, "conflict"),
        "[job.retry].allowed_errors: must not allow 'conflict'",
    ),
    "swapped-error-code-within-profile": (
        lambda c: _entry(c, "memory.search").__setitem__(
            "allowed_errors",
            sorted(
                set(_entry(c, "memory.search")["allowed_errors"])
                - {"stale_projection"}
                | {"size_limit_exceeded"}
            ),
        ),
        "must be exactly the PROJECTION_READ profile",
    ),
    "wrong-error-profile": (
        lambda c: _entry(c, "memory.get").__setitem__(
            "allowed_errors", list(_entry(c, "memory.list")["allowed_errors"])
        ),
        "must be exactly the POINT_READ profile",
    ),
}


@pytest.mark.parametrize("label", sorted(CATALOGUE_MUTATIONS), ids=sorted(CATALOGUE_MUTATIONS))
def test_the_gate_detects_every_category_of_catalogue_drift(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each mutation is a distinct way the catalogue could stop stating A2.5.

    Only the five *semantic* checks run here. Including the fixture comparison
    would make this vacuous -- the fixture disagrees with any mutated annotation,
    so the matrix would stay green even if every semantic check were gutted.
    """
    mutate, expected = CATALOGUE_MUTATIONS[label]
    findings = _with_catalogue(monkeypatch, mutate)
    assert findings, f"{label}: catalogue drift went undetected"
    assert any(expected in finding for finding in findings), (
        f"{label}: expected a finding containing {expected!r}, got {findings}"
    )


def test_the_annotation_must_be_an_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one shape the mutation matrix cannot express: no catalogue at all."""
    real_load_schema = cast("SchemaLoader", checker._load_schema)

    def _load_schema(name: str) -> dict[str, Any]:
        document = real_load_schema(name)
        if name == "operations":
            document = copy.deepcopy(document)
            document[checker.OPERATION_CATALOGUE_ANNOTATION] = {"not": "an array"}
        return document

    monkeypatch.setattr(checker, "_load_schema", _load_schema)
    for check_name in _SEMANTIC_CATALOGUE_CHECKS:
        findings = cast("Check", getattr(checker, check_name))()
        assert any("must be an array" in finding for finding in findings), check_name


def test_the_canonical_invalid_request_retry_class_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 24's retry-class clause reads ``errors.schema.json``, not the
    annotation, so no catalogue mutation can reach it.
    """
    real_load_schema = cast("SchemaLoader", checker._load_schema)

    def _load_schema(name: str) -> dict[str, Any]:
        document = real_load_schema(name)
        if name == "errors":
            document = copy.deepcopy(document)
            document["x-omnivia-error-catalogue"]["invalid_request"] = "retryable"
        return document

    monkeypatch.setattr(checker, "_load_schema", _load_schema)
    findings = cast("Check", checker.check_operation_catalogue_errors)()
    assert any("must have retry class 'non_retryable'" in finding for finding in findings)


def test_the_catalogue_checks_pass_on_the_real_contract() -> None:
    for check_name in _CATALOGUE_CHECKS:
        assert cast("Check", getattr(checker, check_name))() == [], check_name


def test_the_catalogue_checks_are_registered_in_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check nobody calls protects nothing, so the wiring is asserted through
    ``run_checks`` itself rather than by calling the catalogue checks directly.
    """
    real_load_schema = cast("SchemaLoader", checker._load_schema)

    def _load_schema(name: str) -> dict[str, Any]:
        document = real_load_schema(name)
        if name == "operations":
            document = copy.deepcopy(document)
            CATALOGUE_MUTATIONS["removed-operation"][0](
                document[checker.OPERATION_CATALOGUE_ANNOTATION]
            )
        return document

    monkeypatch.setattr(checker, "_load_schema", _load_schema)
    assert any(
        checker.OPERATION_CATALOGUE_ANNOTATION in finding
        for finding in cast("Check", checker.run_checks)()
    )


def test_the_fixture_is_compared_against_the_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The independent oracle is a real gate, not a file nothing reads."""
    findings = _with_catalogue(
        monkeypatch,
        lambda c: _entry(c, "memory.get")["scope"].__setitem__("side_effect", "update"),
        checks=("check_operation_catalogue_fixture_matches",),
    )
    assert any(str(checker.OPERATION_CATALOGUE_FIXTURE) in finding for finding in findings)


def test_a_missing_fixture_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(checker, "OPERATION_CATALOGUE_FIXTURE", tmp_path / "absent.json")
    check = cast("Check", checker.check_operation_catalogue_fixture_matches)
    findings = check()
    assert len(findings) == 1
    assert "missing operation-catalogue fixture" in findings[0]


# --------------------------------------------------------------------------
# Operation catalogue: hostile annotation shapes.
#
# The annotation is ordinary JSON living inside a schema document, which means
# nothing validates it before a check reads it: a `name` can be absent, null, a
# number, an array or an object, and `allowed_errors` can hold any of those too.
# A check that sorts, hashes, or looks such a value up without narrowing it
# first does not report a malformed catalogue -- it raises out of the gate. That
# is strictly worse than a missed finding, because a gate that crashes reports
# *nothing*, including every defect it had already found before the bad value.
#
# So each case here asserts two things at once: that the full `run_checks()`
# completes, and that it names the actual defect.
# --------------------------------------------------------------------------

#: The unhashable and unorderable values a JSON document can put anywhere.
_UNHASHABLE_NAME: list[str] = ["memory", "get"]
_MAPPING_NAME: dict[str, str] = {"name": "memory.get"}


def _replace_two_names_with_lists(catalogue: list[dict[str, Any]]) -> None:
    for name in ("memory.get", "memory.list"):
        _entry(catalogue, name)["name"] = [name]

HOSTILE_CATALOGUE_SHAPES: dict[str, tuple[CatalogueMutation, str]] = {
    "missing-name": (
        lambda c: _entry(c, "memory.get").pop("name"),
        "missing or non-string name",
    ),
    "null-name": (
        lambda c: _entry(c, "memory.get").__setitem__("name", None),
        "missing or non-string name",
    ),
    "integer-name": (
        lambda c: _entry(c, "memory.get").__setitem__("name", 7),
        "missing or non-string name",
    ),
    "boolean-name": (
        lambda c: _entry(c, "memory.get").__setitem__("name", True),
        "missing or non-string name",
    ),
    "list-name": (
        lambda c: _entry(c, "memory.get").__setitem__("name", copy.deepcopy(_UNHASHABLE_NAME)),
        "missing or non-string name",
    ),
    "mapping-name": (
        lambda c: _entry(c, "memory.get").__setitem__("name", copy.deepcopy(_MAPPING_NAME)),
        "missing or non-string name",
    ),
    # Two unhashable names at once. One is enough to raise, so one alone cannot
    # distinguish a name list that was genuinely narrowed from one that merely
    # happened to survive a single bad value.
    "two-list-names": (_replace_two_names_with_lists, "missing or non-string name"),
    # A non-string name must not suppress the *other* findings the same entry
    # earns: the operation it should have named is still missing.
    "non-string-name-is-also-a-missing-operation": (
        lambda c: _entry(c, "memory.get").__setitem__("name", 7),
        "missing operation(s) ['memory.get']",
    ),
    "allowed-errors-with-null": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].append(None),
        "every code must be a string",
    ),
    "allowed-errors-with-integer": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].append(7),
        "every code must be a string",
    ),
    "allowed-errors-with-list": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].append(["not_found"]),
        "every code must be a string",
    ),
    "allowed-errors-with-mapping": (
        lambda c: _entry(c, "memory.get")["allowed_errors"].append({"code": "not_found"}),
        "every code must be a string",
    ),
    "allowed-errors-mixed-string-and-non-string": (
        lambda c: _entry(c, "memory.get").__setitem__(
            "allowed_errors", ["invalid_request", None, 7, ["x"], {"y": 1}]
        ),
        "every code must be a string",
    ),
    "allowed-errors-all-non-strings": (
        lambda c: _entry(c, "memory.get").__setitem__("allowed_errors", [1, 2, 3]),
        "every code must be a string",
    ),
    "allowed-errors-as-mapping": (
        lambda c: _entry(c, "memory.get").__setitem__("allowed_errors", {"invalid_request": True}),
        "must be a non-empty array",
    ),
    "allowed-errors-as-integer": (
        lambda c: _entry(c, "memory.get").__setitem__("allowed_errors", 7),
        "must be a non-empty array",
    ),
    "allowed-errors-as-string": (
        lambda c: _entry(c, "memory.get").__setitem__("allowed_errors", "invalid_request"),
        "must be a non-empty array",
    ),
    # `"conflict" in <not a list>` is the specific hazard in the job-control
    # rule: over a scalar it raises, and over a string or a mapping it silently
    # answers a different question (substring, or key membership).
    "job-cancel-allowed-errors-as-integer": (
        lambda c: _entry(c, "job.cancel").__setitem__("allowed_errors", 7),
        "must be a non-empty array",
    ),
    "job-retry-allowed-errors-as-conflict-substring": (
        lambda c: _entry(c, "job.retry").__setitem__("allowed_errors", "xx_conflict_xx"),
        "must be a non-empty array",
    ),
    "job-cancel-allowed-errors-as-mapping-keyed-by-conflict": (
        lambda c: _entry(c, "job.cancel").__setitem__("allowed_errors", {"conflict": True}),
        "must be a non-empty array",
    ),
    "non-object-entry": (
        lambda c: c.__setitem__(0, 7),
        "are not objects",
    ),
    "null-entry": (
        lambda c: c.__setitem__(0, None),
        "are not objects",
    ),
    "list-entry": (
        lambda c: c.__setitem__(0, ["memory.get"]),
        "are not objects",
    ),
}


@pytest.mark.parametrize(
    "label", sorted(HOSTILE_CATALOGUE_SHAPES), ids=sorted(HOSTILE_CATALOGUE_SHAPES)
)
def test_the_whole_gate_reports_hostile_catalogue_shapes_instead_of_crashing(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_checks()` itself, not a hand-picked subset: the property under test is
    that the *gate* survives, and a subset could pass while a check the subset
    leaves out is the one that raises.
    """
    mutate, expected = HOSTILE_CATALOGUE_SHAPES[label]
    _patch_catalogue(monkeypatch, mutate)
    findings = cast("Check", checker.run_checks)()
    assert findings, f"{label}: a malformed catalogue produced no finding"
    assert any(expected in finding for finding in findings), (
        f"{label}: expected a finding containing {expected!r}, got {findings}"
    )


#: Every JSON-representable value class, including the ones that cannot be
#: hashed (list, dict) or ordered against a string (None, int, float, bool).
_HOSTILE_VALUES: tuple[Any, ...] = (
    None,
    0,
    -1,
    1.5,
    True,
    "",
    "   ",
    [],
    [None],
    [1],
    [{}],
    ["invalid_request", 1],
    {},
    {"a": 1},
)

_ENTRY_FIELDS: tuple[str, ...] = (
    "name",
    "scope",
    "input_schema_ref",
    "result_schema_ref",
    "required_capability",
    "job",
    "pagination",
    "idempotency",
    "precondition",
    "audit",
    "allowed_errors",
)


@pytest.mark.parametrize("value", _HOSTILE_VALUES, ids=[repr(v) for v in _HOSTILE_VALUES])
@pytest.mark.parametrize("field", _ENTRY_FIELDS)
def test_no_hostile_value_in_any_entry_field_can_crash_a_catalogue_check(
    field: str, value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole cross product, rather than the shapes someone thought of.

    Each catalogue field is reachable by any JSON value, so the guarantee worth
    asserting is per *value class*, not per plausible mistake: no check may raise,
    and something must still report the drift.
    """
    findings = _with_catalogue(
        monkeypatch,
        lambda c: _entry(c, "memory.get").__setitem__(field, copy.deepcopy(value)),
        checks=_CATALOGUE_CHECKS,
    )
    assert findings, f"{field}={value!r} went undetected"


@pytest.mark.parametrize("field", _ENTRY_FIELDS)
def test_a_dropped_entry_field_is_reported_rather_than_raising(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _with_catalogue(
        monkeypatch,
        lambda c: _entry(c, "memory.get").pop(field),
        checks=_CATALOGUE_CHECKS,
    )
    assert findings, f"a missing {field!r} went undetected"


def test_the_fixture_comparison_survives_a_non_object_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-object entry can reach the entry-by-entry comparison.

    `_entry_names` skips non-objects on both sides, so inserting one leaves the
    two *name* lists identical while the entry lists differ -- which is exactly
    the path that pairs a fixture object against a bare number. The finding has
    to fall back to the position, since a non-object entry has no name to quote.
    """
    findings = _with_catalogue(
        monkeypatch,
        lambda c: c.insert(3, 7),
        checks=("check_operation_catalogue_fixture_matches",),
    )
    assert any("'index 3'" in finding for finding in findings), findings


# --------------------------------------------------------------------------
# Adapter conformance corpus: the coverage gate must notice a corpus that has
# quietly stopped exercising part of the contract.
# --------------------------------------------------------------------------

CorpusMutation = Callable[[dict[str, Any]], None]


def _with_corpus(
    monkeypatch: pytest.MonkeyPatch, mutate: CorpusMutation, tmp_path: Path
) -> list[str]:
    """Run the corpus check against a mutated copy, leaving the repo untouched."""
    source = checker.FIXTURES_DIR / checker.ADAPTER_CONFORMANCE_CORPUS
    document = json.loads(source.read_text(encoding="utf-8"))
    mutate(document)

    fixtures = tmp_path / "fixtures"
    shutil.copytree(checker.FIXTURES_DIR, fixtures)
    (fixtures / checker.ADAPTER_CONFORMANCE_CORPUS).write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(checker, "FIXTURES_DIR", fixtures)
    return cast("Check", checker.check_adapter_conformance_corpus)()


def _corpus_case(document: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(case for case in document["cases"] if case["id"] == case_id)


CORPUS_MUTATIONS: dict[str, tuple[CorpusMutation, str]] = {
    "wrong-format": (
        lambda d: d.__setitem__("format", "omnivia.something-else"),
        "'format' must be",
    ),
    "cases-not-an-array": (lambda d: d.__setitem__("cases", {}), "'cases' must be an array"),
    "duplicate-case-id": (
        lambda d: d["cases"].append(copy.deepcopy(d["cases"][0])),
        "duplicate case id",
    ),
    "duplicate-request-id": (
        lambda d: d["cases"][1]["request"]["metadata"].__setitem__(
            "request_id", d["cases"][0]["request"]["metadata"]["request_id"]
        ),
        "duplicate request id",
    ),
    "non-catalogue-operation": (
        lambda d: d["cases"][0].__setitem__("operation", "service.health"),
        "non-catalogue operation",
    ),
    "missing-primary-success": (
        lambda d: d["cases"].remove(_corpus_case(d, "graph.traverse/primary-success")),
        "no primary success case",
    ),
    "missing-replay": (
        lambda d: d["cases"].remove(_corpus_case(d, "memory.create/honest-replay")),
        "needs an honest-replay case",
    ),
    "missing-conflict": (
        lambda d: d["cases"].remove(_corpus_case(d, "job.cancel/idempotency-conflict")),
        "needs an idempotency-conflict case",
    ),
    "missing-second-page": (
        lambda d: d["cases"].remove(_corpus_case(d, "memory.list/page-2")),
        "needs a two-page scenario",
    ),
    "uncovered-error-code": (
        lambda d: d["cases"].remove(_corpus_case(d, "error/token_limit_exceeded")),
        "no case exercises error code",
    ),
    "error-on-an-operation-that-forbids-it": (
        lambda d: _corpus_case(d, "error/token_limit_exceeded").__setitem__(
            "operation", "memory.get"
        ),
        "does not permit to raise it",
    ),
    "conflict-on-job-cancel": (
        lambda d: _corpus_case(d, "job.cancel/idempotency-conflict")["expect"].__setitem__(
            "error_code", "conflict"
        ),
        "a state-based refusal is a successful disposition",
    ),
}


@pytest.mark.parametrize("label", sorted(CORPUS_MUTATIONS), ids=sorted(CORPUS_MUTATIONS))
def test_the_corpus_gate_detects_every_category_of_coverage_loss(
    label: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mutate, expected = CORPUS_MUTATIONS[label]
    findings = _with_corpus(monkeypatch, mutate, tmp_path)
    assert findings, f"{label}: coverage loss went undetected"
    assert any(expected in finding for finding in findings), (
        f"{label}: expected a finding containing {expected!r}, got {findings}"
    )


def test_the_corpus_gate_passes_on_the_real_corpus() -> None:
    assert cast("Check", checker.check_adapter_conformance_corpus)() == []


def test_a_missing_corpus_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(checker.FIXTURES_DIR, fixtures)
    (fixtures / checker.ADAPTER_CONFORMANCE_CORPUS).unlink()
    monkeypatch.setattr(checker, "FIXTURES_DIR", fixtures)
    findings = cast("Check", checker.check_adapter_conformance_corpus)()
    assert len(findings) == 1
    assert "missing adapter conformance corpus" in findings[0]


def test_the_corpus_is_exempt_from_the_wire_fixture_manifest_rule() -> None:
    """It ships in the fixtures directory but is not a canonical wire document.

    The exemption is narrow and named: only the manifest itself and the corpus.
    Any *other* unlisted file must still be reported, or the manifest would stop
    being the complete account of the wire fixtures it is meant to be.
    """
    assert checker.ADAPTER_CONFORMANCE_CORPUS in checker._NON_WIRE_FIXTURE_FILES
    assert checker._NON_WIRE_FIXTURE_FILES == frozenset(
        {"manifest.json", checker.ADAPTER_CONFORMANCE_CORPUS}
    )
    assert cast("Check", checker.check_fixture_manifest_is_complete)() == []


def test_an_unlisted_wire_fixture_is_still_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(checker.FIXTURES_DIR, fixtures)
    (fixtures / "rogue-fixture.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(checker, "FIXTURES_DIR", fixtures)
    findings = cast("Check", checker.check_fixture_manifest_is_complete)()
    assert any("rogue-fixture.json" in finding for finding in findings)


# --------------------------------------------------------------------------
# Corpus hostile-shape sweep, checked in.
#
# A gate that can be crashed is a gate that can be bypassed, so every field the
# corpus checker consumes is driven with every JSON shape a corpus file could
# carry. This is deliberately a permanent parametrized suite rather than a
# one-off probe: an earlier repair reported a sweep of this scope as evidence
# while only nominal coverage mutations were actually checked in, which is
# exactly the kind of claim a reviewer cannot verify after the fact.
# --------------------------------------------------------------------------

#: Every field `check_adapter_conformance_corpus` reads, as a path into one case.
CORPUS_CONSUMED_FIELDS: tuple[tuple[str, ...], ...] = (
    ("id",),
    ("operation",),
    ("description",),
    ("request",),
    ("response",),
    ("request", "metadata"),
    ("response", "metadata"),
    ("request", "metadata", "request_id"),
    ("expect",),
    ("expect", "branch"),
    ("expect", "error_code"),
    ("expect", "retry_class"),
    ("expect", "replay_of"),
    ("expect", "idempotency"),
    ("expect", "page_of"),
    ("trusted",),
)

#: Every JSON shape a value can take, plus the blank strings that read as values.
HOSTILE_SHAPES: tuple[Any, ...] = (
    [], {}, 0, 1.5, True, False, None, "", "  ", "\n", [{"nested": 1}], {"k": [1]},
)

#: Fields whose *contents* the checker deliberately does not judge: response
#: metadata is the runner's business via decode, and `trusted` is open-keyed
#: because different frozen validators need different facts.
#:
#: Open-keyed means open about *keys*, not about shape. Only an object value is
#: unjudged here; a list, a number or a blank string in one of these fields is
#: still the wrong kind of thing and must be reported. Exempting the whole field
#: waved through 48 combinations, and the checker in fact reports 40 of them --
#: so the sweep was claiming far less coverage than it had, and would not have
#: noticed the other 40 going quiet.
_CORPUS_UNJUDGED_CONTENTS: frozenset[tuple[str, ...]] = frozenset(
    {("response", "metadata"), ("trusted",)}
)


def _contents_are_unjudged(path: tuple[str, ...], shape: Any) -> bool:
    return path in _CORPUS_UNJUDGED_CONTENTS and isinstance(shape, dict)


def _corpus_with(document: dict[str, Any], tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures"
    shutil.rmtree(fixtures, ignore_errors=True)
    shutil.copytree(checker.FIXTURES_DIR, fixtures)
    (fixtures / checker.ADAPTER_CONFORMANCE_CORPUS).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return fixtures


def _real_corpus() -> dict[str, Any]:
    source = checker.FIXTURES_DIR / checker.ADAPTER_CONFORMANCE_CORPUS
    document: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    return document


@pytest.mark.parametrize("path", CORPUS_CONSUMED_FIELDS, ids=[".".join(p) for p in CORPUS_CONSUMED_FIELDS])
@pytest.mark.parametrize("shape", HOSTILE_SHAPES, ids=[repr(s) for s in HOSTILE_SHAPES])
@pytest.mark.parametrize("case_index", [0, -1], ids=["first-case", "last-case"])
def test_the_corpus_checker_is_total_over_hostile_shapes(
    path: tuple[str, ...],
    shape: Any,
    case_index: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No consumed field may crash the checker, whatever a corpus file contains.

    The first and last cases are both driven because they differ in kind: the
    corpus opens with a success exchange and ends with an error one, and several
    rules only apply to one of the two.
    """
    document = _real_corpus()
    node: Any = document["cases"][case_index]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = shape

    monkeypatch.setattr(checker, "FIXTURES_DIR", _corpus_with(document, tmp_path))
    check = cast("Check", checker.check_adapter_conformance_corpus)
    findings = check()  # must not raise, whatever the shape

    if not _contents_are_unjudged(path, shape):
        assert findings, f"{'.'.join(path)} = {shape!r} was accepted in silence"
        assert all(isinstance(finding, str) and finding for finding in findings)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: c.__setitem__("operation", ["memory.get"]), "non-blank string"),
        (lambda c: c.__setitem__("operation", {"name": "memory.get"}), "non-blank string"),
        (lambda c: c.__setitem__("id", ["x"]), "non-blank string"),
        (lambda c: c.__setitem__("request", [1, 2]), "must be an object"),
        (lambda c: c.__setitem__("expect", ["success"]), "must be an object"),
    ],
    ids=["operation-array", "operation-object", "id-array", "request-array", "expect-array"],
)
def test_the_five_originally_reported_hostile_shapes_stay_reported(
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The exact shapes that once crashed the checker or passed unnoticed.

    Two of these raised `TypeError: unhashable type` and three produced no
    finding at all. They are pinned individually so a regression names the shape
    rather than showing up as one row of a sweep.
    """
    document = _real_corpus()
    mutate(document["cases"][0])
    monkeypatch.setattr(checker, "FIXTURES_DIR", _corpus_with(document, tmp_path))
    findings = cast("Check", checker.check_adapter_conformance_corpus)()
    assert any(expected in finding for finding in findings), findings


@pytest.mark.parametrize(
    "shape",
    # Not an object with a non-string key: JSON has no such thing, and
    # `json.dumps` would quietly render it as one that is.
    [["not", "a", "case"], "a-case-id", 7, None, True],
    ids=["array", "string", "int", "null", "bool"],
)
def test_a_case_that_is_not_an_object_is_reported(
    shape: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole-case type guard, which no test observed.

    Every hostile-shape probe replaced a *field* of a case, so the guard on the
    case itself ran on every corpus and was never asked a question: deleting it
    left the suite green. Without it the checker reaches for `raw.get("id")` on
    whatever arrives.
    """
    document = _real_corpus()
    document["cases"][0] = shape
    monkeypatch.setattr(checker, "FIXTURES_DIR", _corpus_with(document, tmp_path))
    findings = cast("Check", checker.check_adapter_conformance_corpus)()
    assert any("cases[0]: must be an object" in finding for finding in findings), findings
