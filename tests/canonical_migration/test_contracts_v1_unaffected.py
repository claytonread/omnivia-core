"""Slice A must not change the behavior of omnivia_core.contracts.v1.

This is a light-touch smoke check: the exhaustive behavioral coverage for
contracts.v1 already lives in tests/contracts and runs alongside this suite
(see the task's verification command). This test exists to make the "did I
break the existing provider-neutral contract tree" question explicit and
self-contained within canonical_migration, without duplicating that coverage.

The question it answers is deliberately behavioral -- does contracts.v1 still
import and still expose everything it advertises -- and not "is the working
tree clean under that path". Shelling out to ``git status``/``git diff`` would
make a *test* fail on the state of the checkout rather than on the state of
the code: it goes red on an unrelated staged edit, on a rebase in progress,
or anywhere the suite runs from an export with no git metadata, and it goes
green on a change that was already committed. Whether a slice's diff stayed
inside its allowed paths is a review and CI concern, not something a unit
test can observe.
"""

from __future__ import annotations

import importlib

#: Every module in the contracts.v1 package. Each must still import, and each
#: name it advertises in ``__all__`` must still resolve -- a canonical leaf
#: that shadowed or broke a shared dependency shows up here.
CONTRACTS_V1_MODULES: tuple[str, ...] = (
    "omnivia_core.contracts",
    "omnivia_core.contracts.v1",
    "omnivia_core.contracts.v1.codec",
    "omnivia_core.contracts.v1.compatibility",
    "omnivia_core.contracts.v1.generated",
    "omnivia_core.contracts.v1.resources",
)


def test_contracts_v1_still_imports_and_exposes_its_public_api() -> None:
    module = importlib.import_module("omnivia_core.contracts.v1")
    assert hasattr(module, "__all__")
    assert module.__all__, "contracts.v1 must still advertise a public API"

    # resources.list_schema_names() reads through importlib.resources against
    # the wheel's force-included copy, which only exists once built (see
    # tests/contracts/test_resources.py) -- not against this raw source tree.
    # Importing the module and confirming its accessor API is still present
    # is enough of a smoke check here; the exhaustive behavioral check for
    # contracts.v1 lives in tests/contracts, run alongside this suite.
    resources = importlib.import_module("omnivia_core.contracts.v1.resources")
    for name in resources.__all__:
        assert hasattr(resources, name)


def test_every_contracts_v1_module_still_resolves_everything_it_advertises() -> None:
    for module_name in CONTRACTS_V1_MODULES:
        module = importlib.import_module(module_name)
        exported = getattr(module, "__all__", ())
        assert exported, f"{module_name} must still advertise a public API"
        for name in exported:
            assert hasattr(module, name), f"{module_name}.{name} no longer resolves"
