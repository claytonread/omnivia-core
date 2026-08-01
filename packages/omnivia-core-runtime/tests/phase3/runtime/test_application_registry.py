"""R1a: the catalogue-backed application registry guardrail.

This proves registry *structure* only: an application registry derives its
expected operation names from the accepted `OPERATION_CATALOGUE` rather than a
transcribed list, keeps that set distinct from the service-lifecycle probes, fails
closed on unknown or duplicate registration, and never calls itself complete
unless every catalogue name has a handler. It registers no product handlers --
dispatch, authorization, storage and handlers are later, separately approved
slices.
"""

from __future__ import annotations

import pytest
from omnivia_core_runtime.service.operations import (
    APPLICATION_OPERATIONS,
    SERVICE_OPERATIONS,
    ApplicationOperationRegistry,
)

from omnivia_core.contracts.v1 import OPERATION_CATALOGUE

CATALOGUE_NAMES = tuple(entry.name for entry in OPERATION_CATALOGUE)


def _noop(_context: object) -> dict[str, object]:
    return {}


def test_application_operations_equal_every_catalogue_entry_exactly_once() -> None:
    assert APPLICATION_OPERATIONS == frozenset(CATALOGUE_NAMES)
    # A frozenset silently dedupes, so pin the count too: if the catalogue ever
    # gained a duplicate name, the set comparison above would not catch it.
    assert len(APPLICATION_OPERATIONS) == len(CATALOGUE_NAMES)


def test_application_and_service_operations_are_disjoint() -> None:
    assert APPLICATION_OPERATIONS.isdisjoint(frozenset(SERVICE_OPERATIONS))


def test_unknown_operation_registration_fails_closed() -> None:
    registry = ApplicationOperationRegistry()
    with pytest.raises(ValueError, match="not part of the accepted"):
        registry.register("core.health", _noop)
    with pytest.raises(ValueError, match="not part of the accepted"):
        registry.register("not.a.catalogue.operation", _noop)


def test_duplicate_operation_registration_fails_closed() -> None:
    registry = ApplicationOperationRegistry()
    name = CATALOGUE_NAMES[0]
    registry.register(name, _noop)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(name, _noop)


def test_partial_registry_fails_completeness_with_a_deterministic_report() -> None:
    registry = ApplicationOperationRegistry()
    registered = sorted(CATALOGUE_NAMES)[:3]
    for name in registered:
        registry.register(name, _noop)

    report = registry.completeness()
    assert report.is_complete is False
    assert list(report.missing) == sorted(APPLICATION_OPERATIONS - set(registered))
    assert report.unexpected == ()

    with pytest.raises(ValueError, match="incomplete"):
        registry.assert_complete()


def test_exact_registry_with_every_catalogue_name_passes_completeness() -> None:
    registry = ApplicationOperationRegistry()
    for name in CATALOGUE_NAMES:
        registry.register(name, _noop)

    report = registry.completeness()
    assert report.is_complete is True
    assert report.missing == ()
    assert report.unexpected == ()
    registry.assert_complete()  # must not raise


def test_registry_knows_names_without_holding_a_product_handler() -> None:
    """Knowing `APPLICATION_OPERATIONS` is not the same as implementing it.

    An empty registry is a valid, permitted construction state: this slice changes
    registry structure only, and a real handler is a separately approved packet.
    """
    registry = ApplicationOperationRegistry()
    assert registry.operations == frozenset()
    for name in CATALOGUE_NAMES:
        assert name not in registry
        assert registry.get(name) is None
