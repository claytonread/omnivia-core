"""Focused acceptance for ``omnivia_core.control_plane.models``.

The shared migration gates (``_leaves.py``, ``_strict.py``, ``test_parity.py``,
``test_fresh_process_imports.py``) already cover AST shape, namespace purity,
per-symbol annotation/enum/dataclass-description parity against the legacy
module, and isolated-import cleanliness for every registered leaf, including
this one. This module adds the narrow assertions the task package calls out
by name: the two module-level constants, the ``ContractVersion`` identity of
``CONTROL_PLANE_CONTRACT_VERSION``, the control-plane ``LifecycleState``'s
distinctness from ``omnivia_core.lifecycle.models.LifecycleState``, full
53-class coverage anchored to the frozen Phase 0 export baseline, frozen/field
fidelity for every dataclass, representative non-shared mutable default
factories, and an isolated fresh-process import with the exact expected
module closure.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
PYTHON = sys.executable

BASELINE_EXPORTS_PATH = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"
LEGACY_MODULE = "omnivia_memory.control_plane.models"


def _baseline_defines() -> dict[str, dict[str, Any]]:
    inventory: dict[str, Any] = json.loads(BASELINE_EXPORTS_PATH.read_text(encoding="utf-8"))
    return inventory["modules"][LEGACY_MODULE]["defines"]  # type: ignore[no-any-return]


def test_control_plane_constants_are_exact() -> None:
    from omnivia_core.control_plane.models import (
        CONTROL_PLANE_CONTRACT_VERSION,
        CONTROL_PLANE_SCHEMA_VERSION,
    )

    assert CONTROL_PLANE_SCHEMA_VERSION == "omnivia.control_plane_manifest.v1"
    assert isinstance(CONTROL_PLANE_SCHEMA_VERSION, str)
    assert (CONTROL_PLANE_CONTRACT_VERSION.major, CONTROL_PLANE_CONTRACT_VERSION.minor) == (
        1,
        0,
    )


def test_control_plane_contract_version_is_the_knowledge_domain_type_not_a_string_alias() -> None:
    from omnivia_core.control_plane.models import CONTROL_PLANE_CONTRACT_VERSION
    from omnivia_core.knowledge.models import ContractVersion

    assert type(CONTROL_PLANE_CONTRACT_VERSION) is ContractVersion
    assert dataclasses.is_dataclass(ContractVersion)
    assert {f.name for f in dataclasses.fields(ContractVersion)} >= {"major", "minor"}
    assert not isinstance(CONTROL_PLANE_CONTRACT_VERSION, str)
    assert not isinstance("1.0", ContractVersion)


def test_control_plane_lifecycle_state_is_distinct_from_core_lifecycle_lifecycle_state() -> None:
    from omnivia_core.control_plane.models import (
        LifecycleState as ControlPlaneLifecycleState,
    )
    from omnivia_core.lifecycle.models import LifecycleState as CoreLifecycleState

    control_plane_type: type[object] = ControlPlaneLifecycleState
    assert control_plane_type is not CoreLifecycleState
    assert not issubclass(ControlPlaneLifecycleState, CoreLifecycleState)
    assert not issubclass(CoreLifecycleState, ControlPlaneLifecycleState)

    assert [m.name for m in ControlPlaneLifecycleState] == [
        "DRAFT",
        "CANDIDATE",
        "VALIDATED",
        "REVIEWED",
        "ACTIVE",
        "DISABLED",
        "SUSPENDED",
        "DEPRECATED",
        "ARCHIVED",
        "REJECTED",
    ]
    assert [m.value for m in ControlPlaneLifecycleState] == [
        "draft",
        "candidate",
        "validated",
        "reviewed",
        "active",
        "disabled",
        "suspended",
        "deprecated",
        "archived",
        "rejected",
    ]
    assert issubclass(ControlPlaneLifecycleState, str)


def test_all_53_baseline_classes_are_present_with_canonical_module() -> None:
    import omnivia_core.control_plane.models as canonical

    defines = _baseline_defines()
    assert len(defines) == 53

    for name in defines:
        member = getattr(canonical, name, None)
        assert member is not None, f"{name} missing from canonical control_plane.models"
        assert member.__module__ == "omnivia_core.control_plane.models"


def test_every_baseline_dataclass_is_frozen_with_exact_ordered_fields() -> None:
    import omnivia_core.control_plane.models as canonical

    defines = _baseline_defines()
    for name, descriptor in defines.items():
        member = getattr(canonical, name)
        if descriptor.get("kind") != "dataclass":
            continue
        assert dataclasses.is_dataclass(member), f"{name} is expected to be a dataclass"
        dataclass_params: Any = vars(member)["__dataclass_params__"]
        assert dataclass_params.frozen is True, f"{name} must be frozen"
        actual_fields = [f.name for f in dataclasses.fields(member)]
        assert actual_fields == descriptor["fields"], (
            f"{name} field order/membership drifted from baseline:\n"
            f"actual: {actual_fields}\nexpected: {descriptor['fields']}"
        )


def test_every_baseline_enum_is_a_str_enum() -> None:
    import enum

    import omnivia_core.control_plane.models as canonical

    defines = _baseline_defines()
    for name, descriptor in defines.items():
        if descriptor.get("kind") != "enum":
            continue
        member = getattr(canonical, name)
        assert issubclass(member, enum.Enum)
        assert issubclass(member, str)


def test_representative_mutable_default_factories_do_not_share_state_between_instances() -> None:
    from omnivia_core.control_plane.models import (
        Capability,
        CapabilityType,
        Policy,
        PolicyDecision,
        ValidationResult,
    )

    left = ValidationResult(valid=True)
    right = ValidationResult(valid=True)
    left.errors.append("boom")
    assert right.errors == []
    assert left.errors is not right.errors
    assert left.warnings is not right.warnings

    left_capability = Capability(id="a", capability_type=CapabilityType.ACTION, connection_id="c")
    right_capability = Capability(id="b", capability_type=CapabilityType.ACTION, connection_id="c")
    left_capability.metadata["k"] = "v"
    assert right_capability.metadata == {}
    assert left_capability.input_schema is not right_capability.input_schema
    assert left_capability.output_schema is not right_capability.output_schema

    left_policy = Policy(id="p1", decision=PolicyDecision.ALLOW)
    right_policy = Policy(id="p2", decision=PolicyDecision.ALLOW)
    left_policy.capability_ids.append("cap-1")
    left_policy.required_actor_attributes["role"] = "admin"
    assert right_policy.capability_ids == []
    assert right_policy.required_actor_attributes == {}


# ---------------------------------------------------------------------------
# Fresh `python -I -S` import from `src` alone: exact expected Core closure.
# ---------------------------------------------------------------------------

#: Importing ``omnivia_core.knowledge.models`` necessarily executes
#: ``omnivia_core.knowledge``'s package ``__init__.py`` in full (ordinary
#: Python submodule-import semantics), which in turn imports
#: ``omnivia_core._shared.validation`` and its sibling leaves
#: ``omnivia_core.knowledge.normalize``/``omnivia_core.knowledge.validation``.
#: The canonical control-plane barrel is eager by contract, so importing this
#: leaf through the regular package also loads the sibling imports and
#: validation leaves. The barrel acceptance suite owns the same final package
#: closure.
EXPECTED_CORE_CLOSURE = frozenset(
    {
        "omnivia_core",
        "omnivia_core._shared",
        "omnivia_core._shared.validation",
        "omnivia_core.control_plane",
        "omnivia_core.control_plane.imports",
        "omnivia_core.control_plane.models",
        "omnivia_core.control_plane.validation",
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
            "import omnivia_core.control_plane.models",
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


def test_control_plane_models_imports_in_isolation_with_exact_core_closure() -> None:
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
        "isolated import of omnivia_core.control_plane.models failed\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"
