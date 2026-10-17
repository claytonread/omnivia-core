"""Platform-neutral seams for document-domain behaviour tests.

The shipped document route is deliberately macOS-only: it refuses to parse when
the host cannot establish both the Seatbelt confinement and the Darwin process-
tree accounting required by DOC-16.  Most tests in this directory exercise the
domain behaviour *after* extraction, though, and running those through a worker
that must refuse on Linux turns every downstream assertion into the same platform
failure.

On non-macOS hosts those tests therefore bind the two production call sites to
the deterministic in-process adapters.  The worker's protocol, fail-closed
platform behaviour, confinement and supervision retain their own direct tests;
tests marked ``production_document_worker`` also retain the real binding.  No
runtime switch or unconfined production route is introduced by this test-only
fixture.
"""

from __future__ import annotations

import os
import sys

import pytest
from omnivia_core_runtime.service import document_processing
from omnivia_core_runtime.service.document_extractors import extract_document
from omnivia_core_runtime.service.handlers import document_capture


@pytest.fixture(autouse=True)
def portable_document_execution(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep domain tests executable where the product worker must refuse."""
    forced = os.environ.get("OMNIVIA_TEST_PORTABLE_DOCUMENT_EXECUTION") == "1"
    if (sys.platform == "darwin" and not forced) or request.node.get_closest_marker(
        "production_document_worker"
    ):
        return
    monkeypatch.setattr(document_processing, "extract_document", extract_document)
    monkeypatch.setattr(document_capture, "extract_document", extract_document)
