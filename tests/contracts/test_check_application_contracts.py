"""Tests for the Application Contract v1 conformance gate (ADR-038).

Loads ``scripts/check-application-contracts.py`` as a module (its filename is
not a valid Python identifier, so it cannot be imported normally) and
exercises its individual checks against the real repository layout, the same
pattern ``tests/test_package_boundaries.py`` uses for the sibling gate.
"""

from __future__ import annotations

import copy
import importlib.util
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
