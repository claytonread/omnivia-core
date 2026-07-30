"""Compatibility facade for Component Contract validation.

Deprecated: import ``ComponentContractValidationError`` /
``validate_component_contract`` / ``validate_agent_run_record`` from
``omnivia_core.component_contract.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module

from omnivia_core.component_contract.validation import (
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
