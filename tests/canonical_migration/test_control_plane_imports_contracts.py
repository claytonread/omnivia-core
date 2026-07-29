"""Focused acceptance for ``omnivia_core.control_plane.imports``.

The shared migration gates (``_leaves.py``, ``_strict.py``, ``test_parity.py``,
``test_fresh_process_imports.py``) already cover AST shape, namespace purity,
per-symbol parity against the legacy module, and isolated-import cleanliness
for every registered leaf, including this one. This module adds the
behavioral and boundary acceptance the task package calls out by name,
adapted from ``services/omnivia-memory/tests/test_control_plane_imports.py``
to exercise the owner leaves directly: ``omnivia_core.control_plane.imports``
and ``omnivia_core.control_plane.models``. ``omnivia_core.control_plane.
validation`` and the package barrel do not exist yet, so the legacy helper's
``validate_control_plane_manifest`` dependency is replaced below with direct
structural candidate invariants. The legacy activation/release validator
cases are explicitly deferred to the validation slice, where the enforcing
manifest validator will exist.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.control_plane.imports import (
    CatalogueArtifactVerification,
    ImportedCandidateSet,
    ImportSpecValidation,
    detect_import_source_change,
    import_asyncapi_candidates,
    import_catalogue_candidates,
    import_catalogue_generated_candidates,
    import_mcp_candidates,
    import_openapi_candidates,
    validate_asyncapi_import_spec,
    validate_mcp_import_spec,
    validate_openapi_import_spec,
    verify_catalogue_artifacts,
)
from omnivia_core.control_plane.models import LifecycleState

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
PYTHON = sys.executable


def _assert_candidate_only(candidate_set: ImportedCandidateSet) -> None:
    """Direct structural stand-in for the legacy manifest-validation assert."""

    assert candidate_set.import_record.lifecycle == LifecycleState.CANDIDATE
    assert all(
        connection.lifecycle == LifecycleState.CANDIDATE
        for connection in candidate_set.connections
    )
    assert all(
        capability.lifecycle == LifecycleState.CANDIDATE
        for capability in candidate_set.capabilities
    )
    assert all(
        trigger.lifecycle == LifecycleState.CANDIDATE for trigger in candidate_set.triggers
    )
    assert candidate_set.import_record.candidate_capability_ids == [
        capability.id for capability in candidate_set.capabilities
    ]


def _json_stringify_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_import_mcp_candidates_stays_inactive_and_mediated() -> None:
    candidates = import_mcp_candidates(
        "mcp://local/notion",
        {
            "name": "Notion MCP",
            "client_secret": "must-not-leak",
            "access_token": "must-not-leak",
            "tools": [{"name": "create_page"}],
            "resources": [{"uri": "notion://database/pages"}],
            "prompts": [{"name": "summarize_page"}],
        },
    )

    _assert_candidate_only(candidates)
    assert candidates.connections[0].kind.value == "mcp"
    assert [capability.capability_type.value for capability in candidates.capabilities] == [
        "action",
        "resource",
        "prompt",
    ]
    assert candidates.capabilities[0].side_effect.value == "external_write"
    assert candidates.capabilities[1].side_effect.value == "read"
    assert candidates.capabilities[2].side_effect.value == "none"
    rendered = repr(candidates)
    assert "client_secret" not in rendered
    assert "access_token" not in rendered
    assert "must-not-leak" not in rendered


def test_validate_mcp_import_spec_rejects_empty_or_unnamed_candidates() -> None:
    empty = validate_mcp_import_spec({"name": "Empty MCP"})
    unnamed = validate_mcp_import_spec({"tools": [{}]})

    assert not empty.valid
    assert "at least one tool, resource or prompt" in empty.errors[0]
    assert not unnamed.valid
    assert "tools[0] must include name or id" in unnamed.errors[0]

    with pytest.raises(ValueError, match=r"tools\[0\] must include name or id"):
        import_mcp_candidates("mcp://empty", {"tools": [{}]})


def test_import_openapi_candidates_defaults_write_operations_conservative() -> None:
    candidates = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {
                "/contacts": {
                    "get": {"operationId": "listContacts"},
                    "post": {"operationId": "createContact"},
                },
                "/contacts/{id}": {"delete": {"operationId": "deleteContact"}},
            },
        },
    )

    _assert_candidate_only(candidates)
    effects = {
        capability.display_name: capability.side_effect.value
        for capability in candidates.capabilities
    }
    assert effects["listContacts"] == "read"
    assert effects["createContact"] == "external_write"
    assert effects["deleteContact"] == "destructive"
    assert all(
        capability.metadata["source_protocol"] == "openapi"
        for capability in candidates.capabilities
    )


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        pytest.param(
            {
                "info": {"title": "CRM API"},
                "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
            },
            "OpenAPI 3.x version",
            id="missing-version",
        ),
        pytest.param(
            {"openapi": "3.1.0", "info": {"title": "CRM API"}, "paths": {}},
            "paths must be a non-empty object",
            id="empty-paths",
        ),
        pytest.param(
            {
                "openapi": "3.1.0",
                "info": {"title": "CRM API"},
                "paths": {"/contacts": {"get": {"operationId": ""}}},
            },
            "empty operationId",
            id="empty-operation-id",
        ),
    ],
)
def test_validate_openapi_import_spec_rejects_invalid_contracts(
    spec: dict[str, Any], message: str
) -> None:
    result = validate_openapi_import_spec(spec)

    assert not result.valid
    assert message in result.errors[0]


def test_import_openapi_candidates_fails_closed_on_invalid_version() -> None:
    with pytest.raises(ValueError, match="OpenAPI 3.x version"):
        import_openapi_candidates(
            "openapi://invalid",
            {"openapi": "2.0", "info": {"title": "Old"}, "paths": {}},
        )


def test_import_asyncapi_candidates_creates_event_source_triggers() -> None:
    candidates = import_asyncapi_candidates(
        "asyncapi://events",
        {
            "asyncapi": "3.0.0",
            "info": {"title": "Events"},
            "channels": {
                "crm.contact.updated": {
                    "subscribe": {"message": {"name": "com.crm.contact.updated"}}
                }
            },
        },
    )

    _assert_candidate_only(candidates)
    assert candidates.capabilities[0].capability_type.value == "event_source"
    assert candidates.triggers[0].kind.value == "cloudevent"
    assert candidates.triggers[0].event_type == "com.crm.contact.updated"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        pytest.param(
            {"asyncapi": "3.0.0", "info": {"title": "Events"}},
            "channels must be a non-empty object",
            id="missing-channels",
        ),
        pytest.param(
            {"asyncapi": "3.0.0", "info": {"title": "Events"}, "channels": {"events": {}}},
            "at least one subscribe or publish operation",
            id="no-operations",
        ),
        pytest.param(
            {
                "asyncapi": "3.0.0",
                "info": {"title": "Events"},
                "channels": {"events": {"subscribe": "not-an-object"}},
            },
            "must be an object",
            id="invalid-operation",
        ),
    ],
)
def test_validate_asyncapi_import_spec_rejects_invalid_contracts(
    spec: dict[str, Any], message: str
) -> None:
    result = validate_asyncapi_import_spec(spec)

    assert not result.valid
    assert message in result.errors[0]


def test_import_asyncapi_candidates_fails_closed_on_invalid_version() -> None:
    with pytest.raises(ValueError, match="AsyncAPI 2.x or 3.x version"):
        import_asyncapi_candidates(
            "asyncapi://invalid",
            {"asyncapi": "1.0.0", "info": {"title": "Events"}, "channels": {}},
        )


def test_import_catalogue_connection_and_trigger_candidates() -> None:
    connection_candidates = import_catalogue_candidates(
        "omnivia-catalog:generated/catalog.json#app-hubspot",
        {
            "id": "app-hubspot",
            "component_type": "connection",
            "connection_type": "custom_app",
            "display_name": "HubSpot",
            "capabilities": {"read": ["contacts"], "write": ["contacts"], "send": ["email"]},
            "risk": {"level": "high"},
            "default_permissions": {"needs_approval": ["write", "send"]},
        },
    )
    trigger_candidates = import_catalogue_candidates(
        "omnivia-catalog:generated/catalog.json#trigger-hubspot-contact",
        {
            "id": "trigger-hubspot-contact",
            "component_type": "trigger",
            "trigger_type": "webhook",
            "display_name": "HubSpot contact updated",
            "events": [{"name": "com.hubspot.contact.updated"}],
        },
    )

    _assert_candidate_only(connection_candidates)
    _assert_candidate_only(trigger_candidates)
    assert connection_candidates.connections[0].catalogue_entry_id == "app-hubspot"
    assert all(
        capability.metadata["source_protocol"] == "catalogue"
        for capability in connection_candidates.capabilities
    )
    assert {
        capability.side_effect.value for capability in connection_candidates.capabilities
    } == {"external_write"}
    assert trigger_candidates.capabilities[0].side_effect.value == "read"
    assert trigger_candidates.triggers[0].lifecycle == LifecycleState.CANDIDATE
    assert trigger_candidates.triggers[0].event_type == "com.hubspot.contact.updated"


def _generated_catalog() -> dict[str, Any]:
    return {
        "version": "0.1.0",
        "schema_version": "0.1.0",
        "connections": [
            {
                "id": "app-hubspot",
                "component_type": "connection",
                "connection_type": "custom_app",
                "display_name": "HubSpot",
                "capabilities": {"read": ["contacts"]},
                "risk": {"level": "medium"},
                "default_permissions": {"can": ["Read contacts"]},
            }
        ],
        "triggers": [],
        "templates": [
            {
                "id": "template-review",
                "component_type": "automation_template",
                "display_name": "Review Template",
                "candidate_capabilities": [
                    {
                        "id": "capability.review.read",
                        "side_effect": "read",
                        "approval_required": False,
                    },
                    {
                        "id": "capability.review.send",
                        "side_effect": "send",
                        "approval_required": True,
                    },
                ],
                "default_permissions": {
                    "can": ["Read source metadata"],
                    "needs_approval": ["Send external messages"],
                },
                "marketplace": {"pack_id": "pack-review"},
            }
        ],
        "sources": [{"id": "manual"}],
        "generated_at": "2026-06-21T00:00:00Z",
    }


def _generated_search() -> dict[str, Any]:
    return {"documents": [{"id": "app-hubspot"}, {"id": "template-review"}]}


def _generated_manifest(catalog: dict[str, Any], search: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": catalog["version"],
        "schema_version": catalog["schema_version"],
        "generated_at": catalog["generated_at"],
        "catalog_hash": _json_stringify_hash(catalog),
        "search_index_hash": _json_stringify_hash(search),
        "entry_counts": {
            "connections_total": 1,
            "custom_app_total": 1,
            "custom_api_total": 0,
            "custom_mcp_total": 0,
            "triggers_total": 0,
            "templates_total": 1,
        },
        "sources_included": ["manual"],
    }


def test_verify_catalogue_artifacts_matches_generated_hashes_and_counts() -> None:
    catalog = _generated_catalog()
    search = _generated_search()
    manifest = _generated_manifest(catalog, search)

    result = verify_catalogue_artifacts(catalog, search, manifest)

    assert result.valid, result.errors


def test_verify_catalogue_artifacts_rejects_tampered_catalog_hash() -> None:
    catalog = _generated_catalog()
    search = _generated_search()
    manifest = _generated_manifest(catalog, search)
    catalog["connections"][0]["display_name"] = "Tampered"

    result = verify_catalogue_artifacts(catalog, search, manifest)

    assert not result.valid
    assert "catalog_hash does not match" in result.errors[0]


def test_import_generated_catalogue_artifacts_includes_template_candidates() -> None:
    catalog = _generated_catalog()
    search = _generated_search()
    manifest = _generated_manifest(catalog, search)

    candidate_sets = import_catalogue_generated_candidates(
        "omnivia-catalog:generated/catalog.json", catalog, search, manifest
    )

    assert len(candidate_sets) == 2
    template_set = candidate_sets[1]
    _assert_candidate_only(template_set)
    assert template_set.connections[0].catalogue_entry_id == "template-review"
    assert [capability.id for capability in template_set.capabilities] == [
        "capability.candidate.catalogue.template.capability.review.read",
        "capability.candidate.catalogue.template.capability.review.send",
    ]
    assert [capability.side_effect.value for capability in template_set.capabilities] == [
        "read",
        "send",
    ]


def test_import_generated_catalogue_artifacts_fails_closed_on_hash_mismatch() -> None:
    catalog = _generated_catalog()
    search = _generated_search()
    manifest = _generated_manifest(catalog, search)
    manifest["catalog_hash"] = "wrong"

    with pytest.raises(ValueError, match="catalog_hash does not match"):
        import_catalogue_generated_candidates(
            "omnivia-catalog:generated/catalog.json", catalog, search, manifest
        )


def test_detect_import_source_change_reports_unchanged_sources() -> None:
    candidates = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
        },
    )

    result = detect_import_source_change(candidates.import_record, candidates)

    assert not result.changed
    assert result.reason == "unchanged"
    assert result.previous_source_hash == candidates.import_record.source_hash
    assert result.current_source_hash == candidates.import_record.source_hash


def test_detect_import_source_change_reports_hash_changes() -> None:
    original = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {"/contacts": {"get": {"operationId": "listContacts"}}},
        },
    )
    refreshed = import_openapi_candidates(
        "openapi://crm",
        {
            "openapi": "3.1.0",
            "info": {"title": "CRM API"},
            "paths": {
                "/contacts": {"get": {"operationId": "listContacts"}},
                "/companies": {"get": {"operationId": "listCompanies"}},
            },
        },
    )

    result = detect_import_source_change(original.import_record, refreshed)

    assert result.changed
    assert result.reason == "source_hash_changed"
    assert result.previous_source_hash == original.import_record.source_hash
    assert result.current_source_hash == refreshed.import_record.source_hash


# ---------------------------------------------------------------------------
# Public dataclass result containers: independent mutable default factories.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dataclass_type",
    [ImportedCandidateSet, CatalogueArtifactVerification, ImportSpecValidation],
    ids=lambda cls: cls.__name__,
)
def test_public_result_dataclasses_use_independent_default_factories(
    dataclass_type: type[Any],
) -> None:
    factory_fields = [
        field
        for field in dataclasses.fields(dataclass_type)
        if field.default_factory is not dataclasses.MISSING
    ]
    assert factory_fields, f"{dataclass_type.__name__} must declare at least one default factory"

    for field in factory_fields:
        factory: Any = field.default_factory
        left = factory()
        right = factory()
        assert left is not right
        left.append("mutated")
        assert right == []


# ---------------------------------------------------------------------------
# Fresh `python -I -S` import from `src` alone: exact expected Core closure.
# ---------------------------------------------------------------------------

#: Nine-module control-plane-model closure plus this leaf itself. At this
#: intermediate slice ``omnivia_core.control_plane`` is a namespace package;
#: this leaf imports ``omnivia_core.control_plane.models`` directly and adds
#: no further canonical submodule. The later barrel slice owns the expanded
#: package closure once ``control_plane.__init__`` exists.
EXPECTED_CORE_CLOSURE = frozenset(
    {
        "omnivia_core",
        "omnivia_core._shared",
        "omnivia_core._shared.validation",
        "omnivia_core.control_plane",
        "omnivia_core.control_plane.models",
        "omnivia_core.control_plane.imports",
        "omnivia_core.knowledge",
        "omnivia_core.knowledge.models",
        "omnivia_core.knowledge.normalize",
        "omnivia_core.knowledge.validation",
    }
)

FORBIDDEN_MODULE_ROOTS = frozenset(
    {
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
        "omnivia_apps",
        "omnivia_dev",
        "omnivia_pro",
        "omnivia_cloud",
    }
)


def _isolated_import_script() -> str:
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            "import omnivia_core.control_plane.imports",
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
            f"expected_core = {set(EXPECTED_CORE_CLOSURE)!r}",
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


def test_control_plane_imports_imports_in_isolation_with_exact_core_closure() -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", _isolated_import_script()],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        "isolated import of omnivia_core.control_plane.imports failed\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"
