"""Compatibility facade for Component Contract validation.

Deprecated: import ``ComponentContractValidationError`` /
``validate_component_contract`` / ``validate_agent_run_record`` from
``omnivia_core.component_contract.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings (`TYPE_CHECKING`, `Any`, `Dict`, `Enum`, `List`, `Optional`) -- names
# the canonical module's own imports left at its module scope and that this
# leaf's historical namespace still has to resolve. No other code is suppressed.
from omnivia_core.component_contract.validation import (  # type: ignore[attr-defined]
    TYPE_CHECKING,
    Any,
    ComponentContractValidationError,
    Dict,
    Enum,
    List,
    Optional,
    validate_agent_run_record,
    validate_component_contract,
)
