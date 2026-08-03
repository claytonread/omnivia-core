"""The public surface of ``omnivia_core.host_contract`` is exact and standard-library only.

``__all__`` is the contract with downstream callers, so it is pinned here in
full: adding, removing or renaming a public name has to be a deliberate edit to
this test, never a side effect of an import.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from omnivia_core import host_contract
from omnivia_core.host_contract import v1

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "omnivia_core" / "host_contract"

SUBMODULES = (
    "bound",
    "codec",
    "compatibility",
    "generated",
    "publication",
    "registration",
    "resources",
)


# --------------------------------------------------------------------------
# Version namespace
# --------------------------------------------------------------------------


def test_the_family_package_exposes_the_version_namespace_only() -> None:
    """A version boundary is always named explicitly; a future v2 lives alongside v1."""
    assert host_contract.__all__ == ["v1"]
    assert host_contract.v1 is v1


def test_the_root_package_does_not_re_export_the_host_contract() -> None:
    import omnivia_core

    assert omnivia_core.__all__ == ["__version__"]


# --------------------------------------------------------------------------
# The v1 public surface
# --------------------------------------------------------------------------


def test_every_public_name_resolves() -> None:
    for name in v1.__all__:
        assert hasattr(v1, name), name


def test_the_public_surface_is_ordered_and_unique() -> None:
    """Constants, then classes, then functions, each ASCII-sorted.

    That is the ordering the sibling Application Contract v1 package already
    publishes and the one ``ruff``'s ``RUF022`` enforces, so a hand-written and
    a generated surface in this repository read the same way.
    """

    def group(name: str) -> int:
        if name.upper() == name:
            return 0
        return 1 if name[:1].isupper() else 2

    assert len(v1.__all__) == len(set(v1.__all__))
    assert v1.__all__ == sorted(v1.__all__, key=lambda name: (group(name), name))


def test_every_submodule_is_reachable_by_name() -> None:
    for name in SUBMODULES:
        assert name in v1.__all__
        assert getattr(v1, name).__name__ == f"omnivia_core.host_contract.v1.{name}"


def test_every_submodule_public_name_is_re_exported() -> None:
    """A caller never has to know which submodule a governed name lives in."""
    for name in SUBMODULES:
        for exported in getattr(v1, name).__all__:
            assert exported in v1.__all__, f"{name}.{exported}"
            assert getattr(v1, exported) is getattr(getattr(v1, name), exported)


def test_the_public_surface_adds_nothing_beyond_its_submodules() -> None:
    published = {name for module in SUBMODULES for name in getattr(v1, module).__all__}
    assert set(v1.__all__) - published == set(SUBMODULES)


def test_no_import_leaks_into_the_public_surface() -> None:
    """Module objects other than the seven submodules are import leakage, not API."""
    from types import ModuleType

    leaked = [
        name
        for name in v1.__all__
        if isinstance(getattr(v1, name), ModuleType) and name not in SUBMODULES
    ]
    assert leaked == []


@pytest.mark.parametrize(
    "name",
    [
        # B1 -- identity, Workspace and the immutable trusted context.
        "ShellContext",
        "resolve_trusted_context",
        "validate_context_rotation",
        "changed_authority_fields",
        "is_handle_valid",
        "payload_authority_claims",
        "HostAuthorityError",
        "CONTEXT_AUTHORITY_FIELDS",
        "PROHIBITED_PAYLOAD_AUTHORITY_FIELDS",
        # B2 -- envelopes, namespaces, availability, denial.
        "HostRequest",
        "HostResult",
        "HostFailure",
        "CapabilityAvailability",
        "CapabilityDenial",
        "Remediation",
        "HOST_NAMESPACES",
        "AVAILABILITY_STATES",
        "DENIAL_CATEGORIES",
        "REMEDIATION_KINDS",
        "OUTCOMES",
        "decode_host_request",
        "decode_host_result",
        "decode_capability_denial",
        "decode_capability_availability",
        "encode_host_request",
        "encode_host_result",
        "result_branch",
        "validate_result_totality",
        "is_degraded_success",
        "is_available",
        "retry_after_seconds",
        "HostContractDecodeError",
        # B3 -- contributions, compatibility, Release, binding, development.
        "ModuleContributionDeclaration",
        "Contribution",
        "ContributionRegistrationResult",
        "RegistrationRejection",
        "BoundHost",
        "REJECTION_CODES",
        "CompatibilityResult",
        "TargetDeclaration",
        "TargetOffering",
        "evaluate_compatibility",
        "validate_additive_minor",
        "TARGET_CLASSIFICATIONS",
        "COMPATIBILITY_STAGES",
        "ReleasePackageIdentity",
        "TargetEntry",
        "PromotionRecord",
        "RollbackRecord",
        "validate_promotion",
        "validate_same_package",
        "validate_rollback",
        "ReleaseIntegrityError",
        "EnvironmentBinding",
        "validate_environment_binding",
        "validate_binding_revision",
        "release_archive_findings",
        "DevelopmentProfile",
        "SourceMount",
        "ShellScenario",
        "ScenarioAssertion",
        "TestDriverRequest",
        "TestDriverResult",
        "DEVELOPMENT_ONLY_RECORDS",
        # B4 -- versions, serialization, migration, resources.
        "HOST_CONTRACT_VERSION",
        "HOST_CONTRACT_MAJOR",
        "HOST_CONTRACT_RECORDS",
        "HOST_CONTRACT_APPROVAL_ID",
        "HOST_CONTRACT_PROPOSAL_COMMIT",
        "HOST_CONTRACT_CONTENT_SET_SHA256",
        "ContractSupport",
        "SUPPORTED_CONTRACT",
        "negotiate_contract_version",
        "parse_contract_version",
        "require_supported_version",
        "can_roll_back_to",
        "UNSUPPORTED_CONTRACT_VERSION",
        "UNSUPPORTED_CONTRACT_VALUE",
        "MigrationPlan",
        "load_migration_plan",
        "migrate_v0_display_context",
        "is_host_contract_instance",
        "V0_DISPLAY_CONTRACT",
        "V0_PERMITTED_MAPPING",
        "to_canonical_json",
        "to_canonical_json_document",
        "decode_json_document",
        "FIXTURE_EXPECTATIONS",
        "list_schema_names",
        "list_fixture_paths",
        "read_schema",
        "read_fixture",
    ],
)
def test_the_governed_name_is_public(name: str) -> None:
    assert name in v1.__all__


def test_every_top_level_record_is_public() -> None:
    from omnivia_core.host_contract.v1 import generated

    for record in generated.HOST_CONTRACT_RECORDS:
        assert record in v1.__all__


# --------------------------------------------------------------------------
# Standard library only
# --------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_package_imports_nothing_outside_the_standard_library_or_itself() -> None:
    allowed = set(sys.stdlib_module_names) | {"omnivia_core"}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        outside = sorted(_imported_roots(path) - allowed)
        assert outside == [], f"{path.relative_to(REPO_ROOT)} imports {outside}"


def test_the_package_never_imports_a_forbidden_neighbour() -> None:
    forbidden = {
        "jsonschema",
        "referencing",
        "sqlalchemy",
        "omnivia_memory",
        "omnivia_platform",
        "omnivia_dev",
        "omnivia_cloud",
        "omnivia_core_runtime",
        "omnivia_core_mcp",
        "omnivia_core_cli",
    }
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"{path.name}: {name}"


def test_the_package_only_imports_itself_from_omnivia_core() -> None:
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module and module.startswith("omnivia_core"):
                assert module.startswith("omnivia_core.host_contract"), f"{path.name}: {module}"


def test_the_existing_application_contract_surface_is_untouched() -> None:
    """ADR-038's Application Contract v1 stays intact alongside this new surface."""
    from omnivia_core.contracts import v1 as application_v1

    assert application_v1.CONTRACT_VERSION
    assert "RequestEnvelope" in application_v1.__all__
    assert "HostRequest" not in application_v1.__all__
