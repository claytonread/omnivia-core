"""Regression tests for package-root re-exports.

The package root is a flat namespace, so it cannot bind two different objects
to the same name. We therefore verify every unique public symbol exported by
the component, app, module, and run-ledger contracts, and explicitly document
the duplicate-name collisions that cannot be re-exported losslessly.
"""

import importlib

import pytest

ROOT_REEXPORT_MODULES = [
    "omnivia_memory.app_manifest",
    "omnivia_memory.component_contract",
    "omnivia_memory.module_manifest",
    "omnivia_memory.run_ledger",
]

DUPLICATE_ROOT_BINDINGS = {
    "ProvenanceRequirement": {
        "omnivia_memory.app_manifest",
        "omnivia_memory.component_contract",
    },
    "ValidationResult": {
        "omnivia_memory.app_manifest",
        "omnivia_memory.component_contract",
    },
}


def _required_root_exports() -> list[tuple[str, str]]:
    exports: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for module_name in ROOT_REEXPORT_MODULES:
        module = importlib.import_module(module_name)
        for public_name in getattr(module, "__all__", []):
            duplicate_sources = DUPLICATE_ROOT_BINDINGS.get(public_name)
            if duplicate_sources is not None:
                continue
            if public_name in seen_names:
                continue
            exports.append((public_name, module_name))
            seen_names.add(public_name)
    return exports


REQUIRED_EXPORTS = _required_root_exports()


@pytest.mark.parametrize(
    "root_name, module_name",
    REQUIRED_EXPORTS,
    ids=[name for name, _ in REQUIRED_EXPORTS],
)
def test_required_symbol_importable_from_package_root(
    root_name: str, module_name: str
) -> None:
    """Each unique public contract symbol is importable from ``omnivia_memory`` root."""
    root = importlib.import_module("omnivia_memory")
    assert hasattr(root, root_name), f"omnivia_memory.{root_name} is not importable"

    module = importlib.import_module(module_name)
    canonical = getattr(module, root_name)
    assert getattr(root, root_name) is canonical, (
        f"omnivia_memory.{root_name} must re-export "
        f"{module_name}.{root_name}, not a redefinition"
    )


def test_duplicate_public_names_are_explicitly_documented() -> None:
    """Duplicate public names are tracked so the flat root namespace stays intentional."""
    for public_name, module_names in DUPLICATE_ROOT_BINDINGS.items():
        assert len(module_names) > 1
        for module_name in module_names:
            module = importlib.import_module(module_name)
            assert public_name in getattr(module, "__all__", [])


def test_required_symbols_importable_via_from_import() -> None:
    """A representative downstream `from omnivia_memory import ...` line works."""
    from omnivia_memory import (  # noqa: F401
        AppManifest,
        AppManifestValidationError,
        AppState,
        ComponentContract,
        ComponentContractValidationError,
        Database,
        ModuleManifestValidationError,
        MemoryCreate,
        MemoryService,
        MemoryUpdate,
        ModuleKind,
        ModuleManifest,
        RUN_LEDGER_CONTRACT_VERSION,
        RUN_LEDGER_PATH_ENV,
        RunLedgerEntry,
        Source,
        SourceType,
        validate_app_manifest,
        validate_component_contract,
        validate_module_manifest,
        validate_run_ledger_entry,
    )
