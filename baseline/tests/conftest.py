"""Shared fixtures for the baseline tests."""

from __future__ import annotations

import pytest

from baseline.inventory import ensure_core_importable

# Import Core from this checkout before any test touches it, so a stale editable
# install can never be the thing under baseline.
ensure_core_importable()


@pytest.fixture()
def fixture_root(tmp_path):
    """A temporary directory that the legacy database safety guard accepts."""
    return tmp_path
