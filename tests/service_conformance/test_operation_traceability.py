"""Tests for the C0a operation metadata index foundation.

This is C0a operation-metadata index preparation before Wave 2, not a claim
that Wave 2 has opened. R1a (the catalogue-backed application registry) exists,
but there is no complete application-serving, integrated Runtime, IPC and HTTP
candidate, so ``operation-traceability-v1.json`` records only three
provider-neutral service adapters -- ``in_process``, ``ipc``, ``http`` -- and
states every one of their per-operation evidence states as pending rather than
claiming a result.

MCP and CLI are not service adapters here. Their command/tool mapping is not
yet accepted, and the architecture and newer lane plan are not yet reconciled
on MCP coverage/naming, so the fixture records them as a single top-level
client-surface decision -- mapping and evidence both explicitly not decided --
never as a per-operation applicability claim. This module proves that split
holds and stays in step with the frozen twenty-seven-operation catalogue.

Validator, test-suite and architecture-gate traceability, and live adapter
evidence, are later work; this module proves the index's shape, not those.

Two things this module deliberately does *not* do:

* It restates no per-operation semantics. Whether an operation's contract
  facts are correct was decided by
  ``omnivia_core.contracts.v1.generated.OPERATION_CATALOGUE`` and is checked
  again here only for *equality* with that source, never re-derived.
* It copies no case from the accepted 86-case adapter-wire-conformance corpus.
  It proves that corpus exists and is referenced by name, and nothing more.

Standard library and ``omnivia_core.contracts.v1`` only. Nothing here may
import a Runtime, Client, MCP or CLI package, and nothing here may carry a
skip or xfail marker -- ``test_this_module_imports_no_forbidden_package`` and
``test_this_module_carries_no_skip_or_xfail_marker`` hold both of those
promises, not just declare them.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1.conformance import (
    ADAPTER_CONFORMANCE_CORPUS_FILE,
    ADAPTER_CONFORMANCE_CORPUS_FORMAT,
)
from omnivia_core.contracts.v1.generated import OPERATION_CATALOGUE

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "service_conformance" / "operation-traceability-v1.json"
)
CORPUS_PATH = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures" / ADAPTER_CONFORMANCE_CORPUS_FILE

FIXTURE_FORMAT = "omnivia.operation-traceability.v1"
LIVE_EVIDENCE_STATE = "pending_candidate"
#: Provider-neutral service adapters only. ``mcp`` and ``cli`` are client
#: surfaces, not service adapters -- see ``CLIENT_SURFACE_NAMES`` below.
ADAPTER_CLASSES = ("in_process", "ipc", "http")
CLIENT_SURFACE_NAMES = ("mcp", "cli")
CLIENT_SURFACE_MAPPING_STATE = "pending_decision"
CLIENT_SURFACE_EVIDENCE_STATE = "not_evaluated"

#: Module roots this foundation must never import. The Runtime, MCP and CLI
#: packages already exist in this repo (``packages/omnivia-core-runtime``,
#: ``packages/omnivia-core-mcp``, ``packages/omnivia-core-cli``); the Client
#: package does not exist yet, and both plausible names are listed so that
#: this guard does not need editing the day it appears.
FORBIDDEN_IMPORT_ROOTS = (
    "omnivia_core_runtime",
    "omnivia_core_mcp",
    "omnivia_core_cli",
    "omnivia_core_client",
    "omnivia_client",
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document: dict[str, Any] = json.load(handle)
    return document


TRACEABILITY = _load_json(FIXTURE_PATH)
CATALOGUE_BY_NAME = {entry.name: entry for entry in OPERATION_CATALOGUE}
#: ``(operation name, fixture entry)`` for the data-driven per-operation tests, so a
#: failure names the operation rather than an index into a list of twenty-seven.
FIXTURE_OPERATIONS: list[dict[str, Any]] = TRACEABILITY["operations"]
FIXTURE_NAMES = [op["contract"]["name"] for op in FIXTURE_OPERATIONS]
FIXTURE_CASES = list(zip(FIXTURE_NAMES, FIXTURE_OPERATIONS))


# --------------------------------------------------------------------------
# Fixture shape and format
# --------------------------------------------------------------------------


def test_the_fixture_declares_its_format() -> None:
    assert TRACEABILITY["format"] == FIXTURE_FORMAT


def test_the_fixture_declares_the_three_service_adapter_classes_in_the_stated_order() -> None:
    assert tuple(TRACEABILITY["adapter_classes"]) == ADAPTER_CLASSES


def test_every_fixture_operation_has_exactly_the_two_expected_top_level_keys() -> None:
    """No case data, no extra vocabulary -- just a contract and its adapter posture."""
    for name, entry in FIXTURE_CASES:
        assert set(entry) == {"contract", "adapters"}, name


# --------------------------------------------------------------------------
# Catalogue coverage: no missing, no extra, no duplicate, no drift
# --------------------------------------------------------------------------


def test_the_fixture_covers_exactly_the_frozen_operations_in_catalogue_order() -> None:
    assert len(OPERATION_CATALOGUE) == 27
    assert FIXTURE_NAMES == [entry.name for entry in OPERATION_CATALOGUE]
    assert len(FIXTURE_NAMES) == 27
    assert len(set(FIXTURE_NAMES)) == 27


def test_the_fixture_names_no_operation_outside_the_generated_catalogue() -> None:
    assert set(FIXTURE_NAMES) <= set(CATALOGUE_BY_NAME)


@pytest.mark.parametrize(("name", "entry"), FIXTURE_CASES, ids=FIXTURE_NAMES)
def test_every_fixture_contract_matches_the_generated_catalogue_exactly(
    name: str, entry: dict[str, Any]
) -> None:
    """Whole-object equality against ``OperationMetadata.to_wire()``.

    Generated metadata equality, not a hand-maintained divergent catalogue:
    this fails the moment the fixture's copy of an operation's scope,
    capability, schema references, job/pagination/idempotency/precondition/
    audit posture, or allowed-error list drifts from the one source that
    defines them.
    """
    assert entry["contract"] == CATALOGUE_BY_NAME[name].to_wire()


# --------------------------------------------------------------------------
# Adapter evidence: every service-adapter class, every operation, pending --
# and only pending. mcp and cli are asserted absent here, not pending: they
# are client surfaces, not per-operation service adapters (see the
# "Client surfaces" section below).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "entry"), FIXTURE_CASES, ids=FIXTURE_NAMES)
def test_every_operation_declares_exactly_the_three_service_adapter_classes(
    name: str, entry: dict[str, Any]
) -> None:
    assert set(entry["adapters"]) == set(ADAPTER_CLASSES)


@pytest.mark.parametrize(("name", "entry"), FIXTURE_CASES, ids=FIXTURE_NAMES)
def test_no_operation_asserts_mcp_or_cli_applicability(
    name: str, entry: dict[str, Any]
) -> None:
    """mcp/cli command-tool mapping is not yet accepted, so no operation may

    carry a per-operation mcp or cli entry that would read as an applicability
    claim -- that decision lives only in the single top-level ``client_surfaces``
    block asserted below.
    """
    for surface in CLIENT_SURFACE_NAMES:
        assert surface not in entry["adapters"], f"{name}/{surface}"


@pytest.mark.parametrize(("name", "entry"), FIXTURE_CASES, ids=FIXTURE_NAMES)
def test_every_operation_reports_pending_candidate_for_every_service_adapter_class(
    name: str, entry: dict[str, Any]
) -> None:
    """No live product adapter may be marked passing until a committed candidate
    is independently exercised, and this slice has no candidate at all -- so
    every one of the 22 x 3 service-adapter evidence states must read exactly
    ``pending_candidate``, never a value that could be mistaken for a result.
    """
    for adapter_class in ADAPTER_CLASSES:
        assert entry["adapters"][adapter_class] == LIVE_EVIDENCE_STATE, (
            f"{name}/{adapter_class}"
        )


def test_no_evidence_value_anywhere_in_the_fixture_is_anything_but_pending_candidate() -> None:
    observed = {
        value for op in FIXTURE_OPERATIONS for value in op["adapters"].values()
    }
    assert observed == {LIVE_EVIDENCE_STATE}
    assert TRACEABILITY["live_evidence_state"] == LIVE_EVIDENCE_STATE


# --------------------------------------------------------------------------
# Client surfaces: mcp and cli are one top-level decision each, never a
# per-operation applicability claim
# --------------------------------------------------------------------------


def test_the_fixture_declares_exactly_the_mcp_and_cli_client_surfaces() -> None:
    assert set(TRACEABILITY["client_surfaces"]) == set(CLIENT_SURFACE_NAMES)


@pytest.mark.parametrize("surface", CLIENT_SURFACE_NAMES)
def test_every_client_surface_decision_is_pending_and_unevaluated(surface: str) -> None:
    """mcp/cli command-tool mapping is not yet accepted, and the architecture

    and newer lane plan are not yet reconciled on MCP coverage/naming -- so
    each client surface must state a mapping state of ``pending_decision``
    and an evidence state of ``not_evaluated``, never a value that could be
    mistaken for an accepted mapping or a live result.
    """
    decision = TRACEABILITY["client_surfaces"][surface]
    assert set(decision) == {"mapping_state", "evidence_state"}
    assert decision["mapping_state"] == CLIENT_SURFACE_MAPPING_STATE
    assert decision["evidence_state"] == CLIENT_SURFACE_EVIDENCE_STATE


# --------------------------------------------------------------------------
# The referenced 78-case adapter-wire-conformance corpus
# --------------------------------------------------------------------------


def test_the_fixture_references_the_accepted_corpus_by_name_and_format() -> None:
    reference = TRACEABILITY["adapter_evidence_corpus"]
    assert reference["file"] == (
        "contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json"
    )
    assert reference["file"].endswith(ADAPTER_CONFORMANCE_CORPUS_FILE)
    assert reference["format"] == ADAPTER_CONFORMANCE_CORPUS_FORMAT
    assert reference["case_count"] == 86


def test_the_referenced_corpus_file_exists_and_holds_exactly_86_unique_cases() -> None:
    assert CORPUS_PATH.is_file(), f"referenced corpus is missing at {CORPUS_PATH}"
    document = _load_json(CORPUS_PATH)
    assert document["format"] == ADAPTER_CONFORMANCE_CORPUS_FORMAT
    case_ids = [case["id"] for case in document["cases"]]
    assert len(case_ids) == 86
    assert len(set(case_ids)) == 86


def test_the_fixture_copies_no_case_from_the_referenced_corpus() -> None:
    """The fixture states a reference, never a transcript.

    Every operation entry is exactly ``{"contract", "adapters"}`` (pinned by
    ``test_every_fixture_operation_has_exactly_the_two_expected_top_level_keys``
    above), and neither key can hold a case: a contract is catalogue metadata,
    and an adapters object contains only three fixed service-adapter keys whose
    values are evidence-state strings. There is structurally nowhere in this
    document a corpus case could be copied to.
    """
    corpus_case_ids = {
        case["id"] for case in _load_json(CORPUS_PATH)["cases"]
    }
    fixture_text = json.dumps(TRACEABILITY)
    for case_id in corpus_case_ids:
        assert case_id not in fixture_text, f"corpus case id {case_id!r} leaked into the fixture"


# --------------------------------------------------------------------------
# This foundation itself: no forbidden imports, no skip/xfail
# --------------------------------------------------------------------------


def _imported_module_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def test_this_module_imports_no_runtime_client_mcp_or_cli_package() -> None:
    """This is the whole traceability foundation (fixture plus this test module),
    so proving *this file* imports nothing forbidden proves the foundation does.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    roots = _imported_module_roots(source)
    for forbidden in FORBIDDEN_IMPORT_ROOTS:
        assert forbidden not in roots, f"forbidden import root {forbidden!r} found in {__file__}"


def test_this_module_carries_no_skip_or_xfail_marker() -> None:
    """Checked by parsing actual usage, not by scanning for the marker names as
    text -- this test's own docstrings and this check's own marker names would
    otherwise trip the very rule it enforces.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"skip", "xfail"}
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            pytest.fail(f"pytest.mark.{node.attr} found in {__file__}")
        if isinstance(node, ast.Attribute) and node.attr == "importorskip":
            pytest.fail(f"pytest.importorskip found in {__file__}")
