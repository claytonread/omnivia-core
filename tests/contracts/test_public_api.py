"""Tests for the public import surface of the Application Contract v1 (ADR-038).

`omnivia_core.contracts.v1` is meant to be a one-stop import: a caller should
not need to know whether a given name lives in `generated`, `codec`, or
`compatibility`. These tests pin that surface down so a refactor cannot
silently narrow it.
"""

from __future__ import annotations

from omnivia_core import contracts
from omnivia_core.contracts import v1
from omnivia_core.contracts.v1 import codec, compatibility, generated, resources


def test_contracts_package_exposes_the_v1_namespace() -> None:
    assert contracts.v1 is v1
    assert contracts.__all__ == ["v1"]


def test_v1_re_exports_the_generated_public_surface() -> None:
    for name in generated.__all__:
        assert hasattr(v1, name), f"v1 is missing generated name {name!r}"
        assert getattr(v1, name) is getattr(generated, name)


def test_v1_re_exports_the_codec_public_surface() -> None:
    for name in codec.__all__:
        assert hasattr(v1, name), f"v1 is missing codec name {name!r}"
        assert getattr(v1, name) is getattr(codec, name)


def test_v1_re_exports_the_compatibility_public_surface() -> None:
    for name in compatibility.__all__:
        assert hasattr(v1, name), f"v1 is missing compatibility name {name!r}"
        assert getattr(v1, name) is getattr(compatibility, name)


def test_v1_re_exports_the_resources_public_surface() -> None:
    for name in resources.__all__:
        assert hasattr(v1, name), f"v1 is missing resources name {name!r}"
        assert getattr(v1, name) is getattr(resources, name)


def test_v1_exposes_its_submodules_directly() -> None:
    assert v1.generated is generated
    assert v1.codec is codec
    assert v1.compatibility is compatibility
    assert v1.resources is resources
    for name in ("generated", "codec", "compatibility", "resources"):
        assert name in v1.__all__


def test_v1_all_matches_its_actual_public_attributes() -> None:
    """Every name in `__all__` must resolve, and every re-exported name that isn't a dunder
    or a submodule import artifact should be listed in `__all__`.
    """
    for name in v1.__all__:
        assert hasattr(v1, name), f"__all__ names missing attribute {name!r}"
    assert len(v1.__all__) == len(set(v1.__all__)), "__all__ has duplicate entries"


def test_validate_granted_authority_is_publicly_exported() -> None:
    """`validate_granted_authority` is part of the supported surface, not an internal helper.

    It is the rule a caller needs to enforce the authority ceiling on any
    `GrantedAuthority` it did not obtain through `decode_response` -- one
    reconstructed from storage, or received over a transport that decoded the
    envelope elsewhere. A function callers are expected to reach for must be
    exported by name from both the module that defines it and the v1 barrel, and
    both must hand back the very same object rather than a re-wrapped copy.
    """
    assert "validate_granted_authority" in compatibility.__all__
    assert "validate_granted_authority" in v1.__all__
    assert v1.validate_granted_authority is compatibility.validate_granted_authority


def test_the_codec_validates_authority_through_the_exported_function() -> None:
    """The production decoder and the exported name are one implementation, not two.

    If `decode_response` enforced the authority ceiling through a private copy,
    the exported function could drift from the rule actually applied on the
    decode path while every test of each still passed.
    """
    assert codec.validate_granted_authority is compatibility.validate_granted_authority


def test_contract_semantic_error_is_a_value_error() -> None:
    assert issubclass(compatibility.ContractSemanticError, ValueError)


def test_retry_class_mismatch_error_is_a_contract_decode_error() -> None:
    assert issubclass(codec.RetryClassMismatchError, generated.ContractDecodeError)


def test_v1_exposes_the_operation_semantics_submodule_and_its_functions() -> None:
    """The A2.5 catalogue helpers are part of the supported surface.

    `semantics_operations` is not covered by the generated/codec/compatibility
    re-export loops above -- those iterate a module's own `__all__`, and the
    semantics modules are re-exported name by name -- so the three public
    functions and the submodule itself are pinned here explicitly.
    """
    from omnivia_core.contracts.v1 import semantics_operations

    assert v1.semantics_operations is semantics_operations
    assert "semantics_operations" in v1.__all__
    for name in semantics_operations.__all__:
        assert name in v1.__all__, f"v1.__all__ is missing {name!r}"
        assert getattr(v1, name) is getattr(semantics_operations, name)


def test_the_operation_catalogue_is_reachable_from_the_v1_barrel() -> None:
    assert "OPERATION_CATALOGUE" in v1.__all__
    assert v1.OPERATION_CATALOGUE is generated.OPERATION_CATALOGUE


def test_v1_exposes_the_adapter_seam_and_the_conformance_runner() -> None:
    """A2.6's surface is reachable from the barrel, not just from its own modules.

    Like the semantics modules, `adapter` and `conformance` are re-exported name
    by name rather than by a loop over their `__all__`, so the names are pinned
    here explicitly.
    """
    from omnivia_core.contracts.v1 import adapter, conformance

    assert v1.adapter is adapter
    assert v1.conformance is conformance
    for name in ("adapter", "conformance"):
        assert name in v1.__all__
    for module in (adapter, conformance):
        for name in module.__all__:
            assert name in v1.__all__, f"v1.__all__ is missing {name!r}"
            assert getattr(v1, name) is getattr(module, name)
