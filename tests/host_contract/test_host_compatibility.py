"""Tests for Host Contract v1 compatibility, negotiation and migration.

Covers the approved compatibility-and-migration policy: SemVer negotiation
within one major, additive-minor limits, structured target compatibility
outcomes, degradation rules, and the bounded v0 display-context migration that
may never synthesize authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract.v1 import compatibility as compat
from omnivia_core.host_contract.v1 import generated as gen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Version parsing and negotiation
# --------------------------------------------------------------------------


def test_contract_version_parses_into_its_three_parts() -> None:
    assert compat.parse_contract_version("1.4.2") == (1, 4, 2)


@pytest.mark.parametrize("text", ["1.0", "1.0.0.0", "v1.0.0", "1.0.0-rc.1", "01.0.0", ""])
def test_a_non_semver_contract_version_is_refused(text: str) -> None:
    with pytest.raises(compat.ContractVersionError):
        compat.parse_contract_version(text)


def test_this_build_speaks_host_contract_1_0_0() -> None:
    assert compat.SUPPORTED_CONTRACT.major == 1
    assert compat.SUPPORTED_CONTRACT.minimum_minor == 0
    assert compat.SUPPORTED_CONTRACT.maximum_minor == 0
    assert gen.HOST_CONTRACT_VERSION == "1.0.0"


def test_negotiation_selects_the_highest_mutually_supported_minor() -> None:
    host = compat.ContractSupport(major=1, minimum_minor=0, maximum_minor=7)
    consumer = compat.ContractSupport(major=1, minimum_minor=2, maximum_minor=4)
    assert compat.negotiate_contract_version(host, consumer) == "1.4.0"


def test_negotiation_without_a_mutual_major_fails_before_the_operation() -> None:
    host = compat.ContractSupport(major=1, minimum_minor=0, maximum_minor=3)
    consumer = compat.ContractSupport(major=2, minimum_minor=0, maximum_minor=1)
    with pytest.raises(compat.ContractNegotiationError) as excinfo:
        compat.negotiate_contract_version(host, consumer)
    assert excinfo.value.code == compat.UNSUPPORTED_CONTRACT_VERSION


def test_negotiation_without_an_overlapping_minor_fails_closed() -> None:
    host = compat.ContractSupport(major=1, minimum_minor=5, maximum_minor=7)
    consumer = compat.ContractSupport(major=1, minimum_minor=0, maximum_minor=2)
    with pytest.raises(compat.ContractNegotiationError) as excinfo:
        compat.negotiate_contract_version(host, consumer)
    assert excinfo.value.code == compat.UNSUPPORTED_CONTRACT_VERSION


def test_a_support_window_must_not_be_inverted() -> None:
    with pytest.raises(compat.ContractVersionError):
        compat.ContractSupport(major=1, minimum_minor=3, maximum_minor=1).validate()


@pytest.mark.parametrize(
    "window",
    [
        compat.ContractSupport(True, False, False),
        compat.ContractSupport(1.0, 0, 0),  # type: ignore[arg-type]
        compat.ContractSupport(1, "0", 0),  # type: ignore[arg-type]
        compat.ContractSupport(1, 0, object()),  # type: ignore[arg-type]
        compat.ContractSupport(-1, 0, 0),
        compat.ContractSupport(1, -1, 0),
        compat.ContractSupport(1, 0, -1),
    ],
)
def test_support_window_components_are_exact_nonnegative_integers(
    window: compat.ContractSupport,
) -> None:
    with pytest.raises(compat.ContractVersionError):
        window.validate()


def test_a_request_from_another_major_is_refused() -> None:
    with pytest.raises(compat.ContractNegotiationError) as excinfo:
        compat.require_supported_version("2.0.0")
    assert excinfo.value.code == compat.UNSUPPORTED_CONTRACT_VERSION


def test_a_newer_supported_minor_is_accepted_by_this_major() -> None:
    assert compat.is_same_major("1.9.3") is True
    assert compat.is_same_major("2.0.0") is False


# --------------------------------------------------------------------------
# Additive-minor limits
# --------------------------------------------------------------------------


def test_a_minor_release_may_add_an_optional_capability() -> None:
    previous = compat.TargetDeclaration(
        supported_targets=("desktop",), required_capabilities=("files.read",)
    )
    following = compat.TargetDeclaration(
        supported_targets=("desktop",),
        required_capabilities=("files.read",),
        optional_capabilities=("notifications.native",),
    )
    compat.validate_additive_minor(previous, following)


def test_a_minor_release_may_not_promote_an_optional_capability_to_required() -> None:
    previous = compat.TargetDeclaration(
        supported_targets=("desktop",), optional_capabilities=("notifications.native",)
    )
    following = compat.TargetDeclaration(
        supported_targets=("desktop",), required_capabilities=("notifications.native",)
    )
    with pytest.raises(compat.ContractCompatibilityError, match="notifications.native"):
        compat.validate_additive_minor(previous, following)


def test_a_minor_release_may_not_newly_require_a_capability() -> None:
    previous = compat.TargetDeclaration(supported_targets=("desktop",))
    following = compat.TargetDeclaration(
        supported_targets=("desktop",), required_capabilities=("files.export",)
    )
    with pytest.raises(compat.ContractCompatibilityError, match="files.export"):
        compat.validate_additive_minor(previous, following)


def test_a_minor_release_may_not_drop_a_supported_target() -> None:
    previous = compat.TargetDeclaration(supported_targets=("desktop", "cloud"))
    following = compat.TargetDeclaration(supported_targets=("desktop",))
    with pytest.raises(compat.ContractCompatibilityError, match="cloud"):
        compat.validate_additive_minor(previous, following)


def test_a_minor_release_preserves_every_previously_declared_surface() -> None:
    previous = compat.TargetDeclaration(
        supported_targets=("desktop",),
        required_capabilities=("files.read",),
        optional_capabilities=("notifications.native",),
        degradation={"notifications.native": "Use the in-App inbox."},
        classification="desktop",
    )
    removals = (
        compat.TargetDeclaration(
            supported_targets=("desktop",),
            optional_capabilities=("notifications.native",),
            degradation={"notifications.native": "Use the in-App inbox."},
            classification="desktop",
        ),
        compat.TargetDeclaration(
            supported_targets=("desktop",),
            required_capabilities=("files.read",),
            classification="desktop",
        ),
        compat.TargetDeclaration(
            supported_targets=("desktop",),
            required_capabilities=("files.read",),
            optional_capabilities=("notifications.native",),
            classification="desktop",
        ),
        compat.TargetDeclaration(
            supported_targets=("desktop",),
            required_capabilities=("files.read",),
            optional_capabilities=("notifications.native",),
            degradation={"notifications.native": "A different behaviour."},
            classification="desktop",
        ),
        compat.TargetDeclaration(
            supported_targets=("desktop",),
            required_capabilities=("files.read",),
            optional_capabilities=("notifications.native",),
            degradation={"notifications.native": "Use the in-App inbox."},
            classification="universal",
        ),
    )
    for following in removals:
        with pytest.raises(compat.ContractCompatibilityError):
            compat.validate_additive_minor(previous, following)


@pytest.mark.parametrize(
    "declaration",
    [
        compat.TargetDeclaration(supported_targets=["desktop"]),  # type: ignore[arg-type]
        compat.TargetDeclaration(supported_targets=("desktop", "desktop")),
        compat.TargetDeclaration(supported_targets=("desktop",), required_capabilities=("bad id",)),
        compat.TargetDeclaration(supported_targets=("desktop",), optional_capabilities=("x", "x")),
        compat.TargetDeclaration(supported_targets=("desktop",), degradation={1: "safe"}),  # type: ignore[dict-item]
        compat.TargetDeclaration(supported_targets=("desktop",), degradation={"x": 1}),  # type: ignore[dict-item]
    ],
)
def test_target_declarations_require_exact_governed_containers_and_members(
    declaration: compat.TargetDeclaration,
) -> None:
    with pytest.raises(compat.ContractCompatibilityError):
        declaration.validate()


@pytest.mark.parametrize(
    "offering",
    [
        compat.TargetOffering(target=1),  # type: ignore[arg-type]
        compat.TargetOffering(target="desktop", available={"files.read"}),  # type: ignore[arg-type]
        compat.TargetOffering(target="desktop", available=frozenset({"bad id"})),
        compat.TargetOffering(target="desktop", after_configuration=[("files.read", "configure")]),  # type: ignore[arg-type]
        compat.TargetOffering(target="desktop", through_companion={"files.read": object()}),  # type: ignore[dict-item]
        compat.TargetOffering(target="desktop", policy_blocked=frozenset({1})),  # type: ignore[arg-type]
    ],
)
def test_target_offerings_require_exact_governed_containers_and_members(
    offering: compat.TargetOffering,
) -> None:
    with pytest.raises(compat.ContractCompatibilityError):
        offering.validate()


# --------------------------------------------------------------------------
# Target compatibility
# --------------------------------------------------------------------------


def test_target_classifications_are_the_governed_set() -> None:
    assert compat.TARGET_CLASSIFICATIONS == (
        "universal",
        "desktop",
        "cloud",
        "hybrid",
        "solution_multi_target",
    )


def test_an_unknown_target_classification_is_refused() -> None:
    with pytest.raises(compat.ContractCompatibilityError, match="unsupported_contract_value"):
        compat.TargetDeclaration(supported_targets=("desktop",), classification="mobile").validate()


def test_everything_present_is_compatible() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("desktop",), required_capabilities=("files.read",)
    )
    offering = compat.TargetOffering(target="desktop", available=frozenset({"files.read"}))
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.status == "compatible"
    assert result.required_missing == ()
    assert result.optional_missing == ()


def test_a_missing_required_capability_can_never_be_compatible() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("desktop",), required_capabilities=("files.read", "files.export")
    )
    offering = compat.TargetOffering(target="desktop", available=frozenset({"files.read"}))
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.status == "incompatible"
    assert result.required_missing == ("files.export",)


def test_an_unsupported_target_is_incompatible() -> None:
    declaration = compat.TargetDeclaration(supported_targets=("desktop",))
    offering = compat.TargetOffering(target="cloud")
    assert compat.evaluate_compatibility(declaration, offering).status == "incompatible"


def test_a_policy_block_outranks_every_other_outcome() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",), required_capabilities=("credentials.read",)
    )
    offering = compat.TargetOffering(
        target="cloud",
        available=frozenset({"credentials.read"}),
        policy_blocked=frozenset({"credentials.read"}),
    )
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.status == "blocked_by_policy"
    assert result.policy_blocks == ("credentials.read",)


def test_a_required_capability_behind_configuration_needs_configuration() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",), required_capabilities=("integrations.index",)
    )
    offering = compat.TargetOffering(
        target="cloud", after_configuration={"integrations.index": "configure.index"}
    )
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.status == "compatible_after_configuration"
    assert result.required_configuration == ("configure.index",)
    assert result.required_missing == ()


def test_a_required_capability_behind_a_companion_needs_the_companion() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",), required_capabilities=("files.local_scan",)
    )
    offering = compat.TargetOffering(
        target="cloud", through_companion={"files.local_scan": "companion.desktop-bridge"}
    )
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.status == "requires_companion"
    assert result.companions == ("companion.desktop-bridge",)


def test_one_companion_supplying_several_capabilities_is_named_once() -> None:
    """``companions`` names routes, and the canonical schema makes it ``uniqueItems``.

    One companion routinely supplies several capabilities, so listing it per
    capability would emit a duplicate and produce a result that cannot be encoded
    -- the failure would surface later, at ``to_wire``, as a duplicate index
    rather than as anything about the offering.
    """
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",),
        required_capabilities=("files.local_scan", "files.local_watch"),
    )
    offering = compat.TargetOffering(
        target="cloud",
        through_companion={
            "files.local_scan": "companion.desktop-bridge",
            "files.local_watch": "companion.desktop-bridge",
        },
    )
    result = compat.evaluate_compatibility(declaration, offering)

    assert result.status == "requires_companion"
    assert result.companions == ("companion.desktop-bridge",)
    result.validate()
    assert result.to_wire()["companions"] == ["companion.desktop-bridge"]


def test_one_configuration_route_covering_several_capabilities_is_named_once() -> None:
    """The same ``uniqueItems`` rule holds for ``requiredConfiguration``."""
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",),
        required_capabilities=("integrations.index", "integrations.sync"),
    )
    offering = compat.TargetOffering(
        target="cloud",
        after_configuration={
            "integrations.index": "configure.integrations",
            "integrations.sync": "configure.integrations",
        },
    )
    result = compat.evaluate_compatibility(declaration, offering)

    assert result.status == "compatible_after_configuration"
    assert result.required_configuration == ("configure.integrations",)
    result.validate()


def test_distinct_routes_keep_first_reached_order() -> None:
    """De-duplication must not reorder or drop a genuinely distinct route."""
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",),
        required_capabilities=("files.b", "files.a", "files.c"),
    )
    offering = compat.TargetOffering(
        target="cloud",
        through_companion={
            "files.a": "companion.second",
            "files.b": "companion.first",
            "files.c": "companion.second",
        },
    )
    result = compat.evaluate_compatibility(declaration, offering)

    assert result.companions == ("companion.first", "companion.second")
    result.validate()


def test_a_missing_optional_capability_degrades_only_with_declared_behaviour() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("conformance",),
        optional_capabilities=("notifications.native",),
        degradation={
            "notifications.native": (
                "Native notifications are unavailable; in-App notifications remain available."
            )
        },
    )
    offering = compat.TargetOffering(target="conformance")
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.to_wire() == load("degradation/optional-capability-missing.json")


def test_a_missing_optional_capability_without_declared_degradation_is_incompatible() -> None:
    declaration = compat.TargetDeclaration(
        supported_targets=("conformance",), optional_capabilities=("notifications.native",)
    )
    offering = compat.TargetOffering(target="conformance")
    result = compat.evaluate_compatibility(declaration, offering)
    assert result.status == "incompatible"
    assert result.optional_missing == ("notifications.native",)


def test_launch_re_evaluation_reuses_the_declaration_without_rewriting_it() -> None:
    """Compatibility is re-evaluated at launch against current policy and health;
    the immutable declaration is not rewritten to record the answer.
    """
    declaration = compat.TargetDeclaration(
        supported_targets=("cloud",), required_capabilities=("data.read",)
    )
    healthy = compat.TargetOffering(target="cloud", available=frozenset({"data.read"}))
    blocked = compat.TargetOffering(
        target="cloud",
        available=frozenset({"data.read"}),
        policy_blocked=frozenset({"data.read"}),
    )
    assert compat.evaluate_compatibility(declaration, healthy).status == "compatible"
    assert compat.evaluate_compatibility(declaration, blocked).status == "blocked_by_policy"
    assert declaration == compat.TargetDeclaration(
        supported_targets=("cloud",), required_capabilities=("data.read",)
    )


def test_compatibility_evaluation_stages_are_the_governed_lifecycle() -> None:
    assert compat.COMPATIBILITY_STAGES == (
        "authoring",
        "validation",
        "packaging",
        "installation",
        "launch",
    )


# --------------------------------------------------------------------------
# Contract rollback
# --------------------------------------------------------------------------


def test_contract_rollback_requires_a_shared_major() -> None:
    adapter = compat.ContractSupport(major=1, minimum_minor=0, maximum_minor=4)
    assert compat.can_roll_back_to("1.2.0", adapter) is True
    assert compat.can_roll_back_to("2.0.0", adapter) is False


def test_contract_rollback_refuses_a_minor_outside_the_adapter_window() -> None:
    adapter = compat.ContractSupport(major=1, minimum_minor=2, maximum_minor=4)
    assert compat.can_roll_back_to("1.1.0", adapter) is False
    assert compat.can_roll_back_to("1.4.9", adapter) is True


# --------------------------------------------------------------------------
# v0 display-context migration
# --------------------------------------------------------------------------


def test_the_migration_fixture_loads_as_a_plan() -> None:
    plan = compat.load_migration_plan(load("migration/v0-display-context-to-v1.json"))
    assert plan.source_contract == "app-shell-host-context.v0"
    assert plan.target_contract == "1.0.0"
    assert plan.result == "requires_trusted_context_service"
    assert plan.permitted_mapping == (
        "app_id",
        "app_name",
        "runtime_state",
        "sources",
        "last_updated",
    )
    assert plan.prohibited_synthesis == (
        "principalRef",
        "organisationRef",
        "workspaceRef",
        "permissionSummaryRef",
        "entitlementSummaryRef",
        "environmentId",
    )


def test_a_migration_plan_is_never_a_host_contract_instance() -> None:
    document = load("migration/v0-display-context-to-v1.json")
    assert compat.is_host_contract_instance(document) is False
    for record in gen.HOST_CONTRACT_RECORDS:
        with pytest.raises(gen.HostContractDecodeError):
            getattr(gen, record).from_wire(document)


def test_a_valid_host_contract_record_is_recognised_as_an_instance() -> None:
    assert compat.is_host_contract_instance(load("valid/shell-context.json")) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [("contextId", ""), ("contextVersion", 0), ("issuedAt", "not-a-timestamp")],
)
def test_schema_invalid_shell_contexts_are_not_host_contract_instances(
    field: str, value: object
) -> None:
    document = load("valid/shell-context.json")
    document[field] = value
    assert compat.is_host_contract_instance(document) is False


def test_malformed_nested_and_direct_values_are_not_host_contract_instances() -> None:
    result = load("valid/host-result.json")
    result["data"] = {"nested": object()}
    direct_context = gen.ShellContext(
        context_id="",
        context_version=0,
        principal_ref="principal-local-001",
        organisation_ref="org-001",
        workspace_ref="workspace-001",
        entitlement_summary_ref="entitlements-001",
        permission_summary_ref="permissions-001",
        runtime_id="desktop-runtime-001",
        environment_id="local-environment-001",
        target="desktop",
        issued_at="not-a-timestamp",
    )
    plan = compat.MigrationPlan(
        source_contract=compat.V0_DISPLAY_CONTRACT,
        target_contract="1.0.0",
        permitted_mapping=compat.V0_PERMITTED_MAPPING,
        prohibited_synthesis=(),
        result="requires_trusted_context_service",
    )

    assert compat.is_host_contract_instance(result) is False
    assert compat.is_host_contract_instance(direct_context) is False
    assert compat.is_host_contract_instance(plan) is False


def test_v0_display_fields_map_through_to_presentation_data() -> None:
    migrated = compat.migrate_v0_display_context(
        {
            "app_id": "documents",
            "app_name": "Documents",
            "runtime_state": "ready",
            "sources": ["local"],
            "last_updated": "2026-08-03T00:00:00Z",
        }
    )
    assert migrated == {
        "app_id": "documents",
        "app_name": "Documents",
        "runtime_state": "ready",
        "sources": ("local",),
        "last_updated": "2026-08-03T00:00:00Z",
    }


def test_v0_migration_drops_a_field_the_plan_does_not_permit() -> None:
    migrated = compat.migrate_v0_display_context({"app_id": "documents", "vendorExtra": "x"})
    assert migrated == {"app_id": "documents"}


def test_v0_migration_refuses_to_synthesize_authority() -> None:
    with pytest.raises(compat.ContractCompatibilityError, match="workspaceRef"):
        compat.migrate_v0_display_context({"app_id": "documents", "workspaceRef": "workspace-other"})


def test_v0_migration_output_cannot_be_used_as_a_shell_context() -> None:
    migrated = compat.migrate_v0_display_context({"app_id": "documents", "app_name": "Documents"})
    with pytest.raises(gen.HostContractDecodeError):
        gen.ShellContext.from_wire(dict(migrated))
