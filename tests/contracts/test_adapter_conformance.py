"""Tests for the adapter seam and the shared conformance corpus (A2.6, ADR-038/ADR-039).

The corpus exists so that "this adapter conforms" means the same thing for every
adapter. That claim is worth exactly as much as the runner's willingness to fail,
so most of this file breaks recorded exchanges on purpose and insists the runner
notices -- and notices *for the reason the case names*.

Asserting the reason is the point, not decoration. An earlier version of this
suite reported sixteen green mutations while the runner was not binding payloads
to their declared schemas at all: every probe passed, because each happened to
check something the runner did do. A mutation that trips an unrelated rule proves
nothing about the rule it was written for, so every entry in :data:`MUTATIONS`
states the diagnostic its defect must produce.

Two probing hazards are handled explicitly, both learned the hard way:

* a mutation applied to the cases *and* replayed by an adapter built from those
  same cases cannot diverge -- the adapter agrees with the corrupted expectation
  -- so each entry declares which recording the adapter replays; and
* a mutation that corrupts shared state leaks into later probes, so cases are
  deep-copied before mutation and the corpus is asserted unchanged afterwards.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1 import resources
from omnivia_core.contracts.v1.adapter import (
    ApplicationWireAdapter,
    InProcessFakeAdapter,
)
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.conformance import (
    ADAPTER_CONFORMANCE_CORPUS_FILE,
    ADAPTER_CONFORMANCE_CORPUS_FORMAT,
    AdapterConformanceCase,
    AdapterConformanceError,
    load_adapter_conformance_corpus,
    run_adapter_conformance,
    validate_case_collection,
)
from omnivia_core.contracts.v1.generated import (
    DEFAULT_RETRY_CLASSIFICATION,
    FROZEN_ERROR_CODES,
    OPERATION_CATALOGUE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"
CANONICAL_SCHEMAS_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
CATALOGUE = {entry.name: entry for entry in OPERATION_CATALOGUE}
TARGET = "memory.get/primary-success"


@pytest.fixture(autouse=True)
def _packaged_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the packaged-resource seam at the canonical checked-in directory.

    ``contracts/application/v1/{fixtures,schemas}`` is only materialized under
    ``omnivia_core/contracts/v1/resources`` by the wheel build, so a source
    checkout has nothing to read. ``tests/contracts/test_resources.py`` redirects
    the same seam for the same reason; that the *wheel* carries the corpus is
    asserted by ``scripts/check-package-builds.sh``.
    """
    monkeypatch.setattr(resources, "_fixtures_root", lambda: CANONICAL_FIXTURES_DIR)
    monkeypatch.setattr(resources, "_schemas_root", lambda: CANONICAL_SCHEMAS_DIR)


@pytest.fixture
def corpus() -> tuple[AdapterConformanceCase, ...]:
    return load_adapter_conformance_corpus()


def _fake(cases: tuple[AdapterConformanceCase, ...]) -> InProcessFakeAdapter:
    return InProcessFakeAdapter((case.request_id, case.response) for case in cases)


def _case(cases: tuple[AdapterConformanceCase, ...], case_id: str) -> AdapterConformanceCase:
    return next(case for case in cases if case.id == case_id)


def _corpus_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (CANONICAL_FIXTURES_DIR / ADAPTER_CONFORMANCE_CORPUS_FILE).read_text(encoding="utf-8")
    )
    return document


# --------------------------------------------------------------------------
# The corpus
# --------------------------------------------------------------------------


def test_the_corpus_loads_and_is_internally_coherent(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    assert len(corpus) == 73
    assert len(validate_case_collection(corpus)) == len(corpus)
    assert all(case.operation in CATALOGUE for case in corpus)


def test_the_corpus_declares_its_format() -> None:
    assert _corpus_document()["format"] == ADAPTER_CONFORMANCE_CORPUS_FORMAT


def test_the_corpus_is_readable_through_the_packaged_resource_seam() -> None:
    """Every adapter must reach the corpus the same way, wheel or checkout."""
    assert ADAPTER_CONFORMANCE_CORPUS_FILE in resources.list_fixture_files()
    assert (
        resources.read_fixture(ADAPTER_CONFORMANCE_CORPUS_FILE)["format"]
        == ADAPTER_CONFORMANCE_CORPUS_FORMAT
    )


def test_the_corpus_is_not_registered_as_a_wire_fixture() -> None:
    """It ships beside the wire fixtures but is governed separately.

    The manifest describes one envelope per entry -- which branch, whether it is
    schema-valid, which semantic it carries. A transcript of many exchanges has
    no single answer to any of those.
    """
    manifest = json.loads((CANONICAL_FIXTURES_DIR / "manifest.json").read_text())
    assert ADAPTER_CONFORMANCE_CORPUS_FILE not in {e["file"] for e in manifest["fixtures"]}


def test_every_operation_has_a_primary_success_case(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    primary = {
        case.operation
        for case in corpus
        if case.branch == "success" and case.replay_of is None and case.page_of is None
    }
    assert primary == set(CATALOGUE)


def test_every_mutation_has_a_replay_and_a_conflict_case(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    mutations = {n for n, e in CATALOGUE.items() if e.scope.side_effect != "none"}
    assert len(mutations) == 9
    assert {c.operation for c in corpus if c.idempotency == "replay"} == mutations
    assert {c.operation for c in corpus if c.idempotency == "idempotency_conflict"} == mutations


def test_every_paginated_operation_has_a_two_page_scenario(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    paginated = {n for n, e in CATALOGUE.items() if e.pagination.paginated}
    assert len(paginated) == 7
    assert {case.operation for case in corpus if case.page_of is not None} == paginated


def test_a_second_page_returns_different_items_from_the_first(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Otherwise a "two-page scenario" proves only that a token was echoed."""
    for case in corpus:
        if case.page_of is None:
            continue
        assert case.response.get("result") != _case(corpus, case.page_of).response.get(
            "result"
        ), case.id


def test_every_frozen_error_code_is_exercised_where_it_is_permitted(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    covered = {c.error_code: c.operation for c in corpus if c.error_code}
    assert set(covered) == set(FROZEN_ERROR_CODES)
    for code, operation in covered.items():
        assert code in CATALOGUE[operation].allowed_errors, f"{operation} may not raise {code}"


def test_error_cases_carry_the_frozen_retry_class(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    for case in corpus:
        if case.error_code is None:
            continue
        expected = DEFAULT_RETRY_CLASSIFICATION[case.error_code]
        assert case.response["error"]["retry_class"] == expected, case.id


def test_no_job_control_case_ever_emits_conflict(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    for case in corpus:
        if case.operation in {"job.cancel", "job.retry"}:
            assert case.error_code != "conflict", case.id


def test_every_payload_is_a_valid_instance_of_its_declared_type(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The corpus states the payload language, so its own payloads must speak it."""
    assert run_adapter_conformance(_fake(corpus), corpus) == len(corpus)


def test_the_trusted_block_is_declared_where_a_frozen_rule_needs_it(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """`job.events` binds an opaque token; nothing on the wire says what it binds."""
    assert "result_page_binding" in _case(corpus, "job.events/primary-success").trusted
    second = _case(corpus, "job.events/page-2")
    assert second.trusted["request_page_binding"]["next_sequence"] == 2


# --------------------------------------------------------------------------
# The fake adapter
# --------------------------------------------------------------------------


def test_the_fake_adapter_satisfies_the_wire_protocol(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    adapter = _fake(corpus)
    assert isinstance(adapter, ApplicationWireAdapter)
    assert len(adapter) == len(corpus)


def test_run_adapter_conformance_loads_the_corpus_when_none_is_given(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    assert run_adapter_conformance(_fake(corpus)) == len(corpus)


def test_a_returned_response_cannot_alias_the_transcript(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    case = _case(corpus, TARGET)
    adapter = _fake(corpus)
    returned = adapter.call(case.request)
    returned["result"] = {"rewritten": "by the caller"}  # type: ignore[index]
    assert adapter.call(case.request) == case.response


def test_a_constructor_argument_cannot_alias_the_transcript(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A caller editing its own mapping afterwards must not rewrite the transcript."""
    case = _case(corpus, TARGET)
    owned = copy.deepcopy(dict(case.response))
    adapter = InProcessFakeAdapter([(case.request_id, owned)])
    owned["result"] = {"rewritten": "after construction"}
    assert adapter.call(case.request) == case.response


def test_the_fake_adapter_refuses_a_duplicate_recorded_request_id() -> None:
    with pytest.raises(ContractSemanticError, match="duplicate recorded request id"):
        InProcessFakeAdapter([("req-1", {}), ("req-1", {})])


@pytest.mark.parametrize(
    ("exchange", "expected"),
    [
        (("", {}), "non-empty request id"),
        ((None, {}), "non-empty request id"),
        (("req-1", ["not", "a", "mapping"]), "not a response envelope mapping"),
        (("req-1", None), "not a response envelope mapping"),
    ],
)
def test_the_fake_adapter_validates_transcript_entries(
    exchange: tuple[Any, Any], expected: str
) -> None:
    with pytest.raises(ContractSemanticError, match=expected):
        InProcessFakeAdapter([exchange])


@pytest.mark.parametrize(
    ("request_payload", "expected"),
    [
        (["not", "a", "mapping"], "expected a request envelope mapping"),
        ({}, "no 'metadata' object"),
        ({"metadata": {}}, "no string 'request_id'"),
        ({"metadata": {"request_id": 7}}, "no string 'request_id'"),
    ],
)
def test_the_fake_adapter_rejects_a_malformed_request(
    request_payload: object, expected: str
) -> None:
    with pytest.raises(ContractSemanticError, match=expected):
        InProcessFakeAdapter([]).call(request_payload)  # type: ignore[arg-type]


def test_an_unrecorded_request_is_refused_rather_than_improvised(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A fake that invented a plausible answer would make the corpus look complete."""
    with pytest.raises(ContractSemanticError, match="no recorded exchange"):
        InProcessFakeAdapter([]).call(_case(corpus, TARGET).request)


# --------------------------------------------------------------------------
# Case-collection integrity
# --------------------------------------------------------------------------


def test_duplicate_case_ids_are_rejected(corpus: tuple[AdapterConformanceCase, ...]) -> None:
    """An index built by comprehension would let one exchange silently replace another."""
    case = _case(corpus, TARGET)
    with pytest.raises(AdapterConformanceError, match="duplicate case id"):
        validate_case_collection([case, dataclasses.replace(case)])


def test_duplicate_request_ids_are_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    case = _case(corpus, TARGET)
    twin = dataclasses.replace(
        _case(corpus, "memory.list/primary-success"),
        id="a-different-case",
        request=copy.deepcopy(dict(case.request)),
    )
    with pytest.raises(AdapterConformanceError, match="could not tell the two exchanges apart"):
        validate_case_collection([case, twin])


def test_a_blank_case_id_is_rejected(corpus: tuple[AdapterConformanceCase, ...]) -> None:
    with pytest.raises(AdapterConformanceError, match="non-blank string"):
        validate_case_collection([dataclasses.replace(_case(corpus, TARGET), id="  ")])


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"replay_of": TARGET, "idempotency": "replay"}, "refers to itself"),
        ({"replay_of": "no-such-case", "idempotency": "replay"}, "names unknown case"),
        ({"page_of": TARGET}, "refers to itself"),
        ({"page_of": "no-such-case"}, "names unknown case"),
        ({"replay_of": "no-such-case"}, "replay_of and idempotency"),
    ],
)
def test_broken_links_are_rejected_before_execution(
    corpus: tuple[AdapterConformanceCase, ...], changes: dict[str, Any], expected: str
) -> None:
    """A link is resolved before it is used, so a broken one is named as such."""
    case = dataclasses.replace(_case(corpus, TARGET), **changes)
    with pytest.raises(AdapterConformanceError, match=expected):
        validate_case_collection([case])


def test_a_cross_operation_link_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    replay = dataclasses.replace(
        _case(corpus, "memory.create/honest-replay"), replay_of=TARGET
    )
    with pytest.raises(AdapterConformanceError, match="which is 'memory.get'"):
        validate_case_collection(
            [_case(corpus, TARGET), _case(corpus, "memory.create/primary-success"), replay]
        )


def test_links_may_not_chain(corpus: tuple[AdapterConformanceCase, ...]) -> None:
    """A link must resolve to an originating exchange, not to another link."""
    chained = dataclasses.replace(
        _case(corpus, "memory.create/idempotency-conflict"),
        replay_of="memory.create/honest-replay",
    )
    with pytest.raises(AdapterConformanceError, match="which is itself linked"):
        validate_case_collection(
            [
                _case(corpus, "memory.create/primary-success"),
                _case(corpus, "memory.create/honest-replay"),
                chained,
            ]
        )


def test_links_may_not_chain_across_link_kinds(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The two link kinds form one graph, so chaining is forbidden across them.

    Checking only same-kind chaining let `page_of` name a `replay_of` case and
    the reverse, which admitted alternating chains of unbounded depth while every
    individual link looked well formed.
    """
    first = _case(corpus, "memory.list/primary-success")
    second = _case(corpus, "memory.list/page-2")
    third_request = copy.deepcopy(dict(second.request))
    third_request["metadata"]["request_id"] = "req-third-page"
    third = dataclasses.replace(
        second, id="memory.list/page-3", page_of=second.id, request=third_request
    )
    with pytest.raises(AdapterConformanceError, match="which is itself linked"):
        validate_case_collection([first, second, third])


def test_an_invalid_collection_is_rejected_before_the_adapter_is_called(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    calls: list[object] = []

    class Counting:
        def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            calls.append(request)
            raise AssertionError("the adapter must not be reached")

    case = _case(corpus, TARGET)
    # Named, so the probe cannot be satisfied by some later gate rejecting the
    # collection for an unrelated reason and still leaving the adapter untouched.
    with pytest.raises(AdapterConformanceError, match="duplicate case id"):
        run_adapter_conformance(Counting(), [case, dataclasses.replace(case)])
    assert calls == []


# --------------------------------------------------------------------------
# Exception normalization
# --------------------------------------------------------------------------


class _ExplodingAdapter:
    def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("the transport died mid-call")


class _JunkAdapter:
    def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return ["not", "an", "envelope"]  # type: ignore[return-value]


def test_a_raising_adapter_surfaces_as_a_conformance_failure(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    with pytest.raises(AdapterConformanceError, match="adapter raised RuntimeError"):
        run_adapter_conformance(_ExplodingAdapter(), [_case(corpus, TARGET)])


def test_a_non_envelope_response_surfaces_as_a_conformance_failure(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    with pytest.raises(AdapterConformanceError, match="not a response envelope mapping"):
        run_adapter_conformance(_JunkAdapter(), [_case(corpus, TARGET)])


def test_a_malformed_case_is_named_rather_than_crashing(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A stripped request envelope is a corpus defect, reported against its case.

    This probe used to be described as reaching the *referenced*-case decode path
    inside `_check_idempotency`. It never did, and could not: a link may only
    resolve to an exchange earlier in the collection, and that exchange has
    already been decoded as an exchange of its own by the time anything reads it
    again -- so the referencing case is never the first to notice. What the probe
    actually proves is the coarser well-formedness gate, asserted here by name.
    """
    first = _case(corpus, "memory.create/primary-success")
    broken = copy.deepcopy(dict(first.request))
    broken["metadata"] = {"request_id": "req-broken"}
    cases = [
        dataclasses.replace(first, request=broken),
        _case(corpus, "memory.create/honest-replay"),
    ]
    with pytest.raises(AdapterConformanceError, match="but its request selects None") as caught:
        run_adapter_conformance(_fake(corpus), cases)
    assert caught.value.case_id == "memory.create/primary-success"


def test_a_decode_failure_reading_a_linked_case_is_normalized() -> None:
    """The normalization itself, reached directly.

    A raw `ContractDecodeError` escaping the public runner would make a corpus
    defect look like a crash and would carry no case id to act on. Nothing can
    drive this through a full run (see above), so it is exercised against the
    check it guards.
    """
    from omnivia_core.contracts.v1 import conformance

    def envelope(request_id: str) -> dict[str, Any]:
        return {"metadata": {"request_id": request_id}, "input": {}, "operation": "memory.create"}

    origin = AdapterConformanceCase(
        id="origin",
        operation="memory.create",
        description="an origin whose request cannot be decoded",
        request=envelope("req-origin"),
        response={},
        branch="success",
        principal_id="user-42",
    )
    repeat = dataclasses.replace(
        origin, id="repeat", request=envelope("req-repeat"), replay_of="origin",
        idempotency="replay",
    )
    derived = conformance._DerivedRelationships(replay_origin={}, page_origin={})
    with pytest.raises(AdapterConformanceError, match="cannot compute idempotency equivalence"):
        conformance._check_idempotency(repeat, {"origin": origin}, derived)


def test_a_conformance_failure_names_the_case_that_produced_it(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    cases = [dataclasses.replace(_case(corpus, TARGET), error_code="not_found")]
    with pytest.raises(AdapterConformanceError) as caught:
        run_adapter_conformance(_fake(corpus), cases)
    assert caught.value.case_id == TARGET
    assert TARGET in str(caught.value)


# --------------------------------------------------------------------------
# The mutation matrix: every frozen category, each asserting its own gate
# --------------------------------------------------------------------------

Mutator = Callable[[dict[str, Any]], None]
Build = Callable[[tuple[AdapterConformanceCase, ...]], list[AdapterConformanceCase]]


def _on_request(case_id: str, mutate: Mutator) -> Build:
    def apply(corpus: tuple[AdapterConformanceCase, ...]) -> list[AdapterConformanceCase]:
        request = copy.deepcopy(dict(_case(corpus, case_id).request))
        mutate(request)
        return [
            dataclasses.replace(c, request=request) if c.id == case_id else c for c in corpus
        ]

    return apply


def _on_response(case_id: str, mutate: Mutator) -> Build:
    def apply(corpus: tuple[AdapterConformanceCase, ...]) -> list[AdapterConformanceCase]:
        response = copy.deepcopy(dict(_case(corpus, case_id).response))
        mutate(response)
        return [
            dataclasses.replace(c, response=response) if c.id == case_id else c for c in corpus
        ]

    return apply


def _on_fields(case_id: str, **changes: Any) -> Build:
    def apply(corpus: tuple[AdapterConformanceCase, ...]) -> list[AdapterConformanceCase]:
        return [dataclasses.replace(c, **changes) if c.id == case_id else c for c in corpus]

    return apply


def _combine(*builds: Build) -> Build:
    """Apply several case edits together.

    Some rules can only be isolated by moving a case's declaration *and* its wire
    value at once: leave them apart and an earlier, coarser check fires first and
    the probe silently tests the wrong thing.
    """

    def apply(corpus: tuple[AdapterConformanceCase, ...]) -> list[AdapterConformanceCase]:
        cases = list(corpus)
        for build in builds:
            cases = build(tuple(cases))
        return cases

    return apply


def _swap_to_error_branch(response: dict[str, Any]) -> None:
    response.pop("result")
    response["error"] = {"code": "not_found", "message": "x", "retry_class": "non_retryable"}


#: label -> (build mutated cases, required diagnostic, which recording the adapter replays).
#:
#: ``"mutated"`` rebuilds the fake from the mutated cases so adapter and
#: expectation agree and only a semantic gate can fail. ``"original"`` keeps the
#: fake on the untouched recording, which is what makes a drifted expectation or
#: an unrecorded request observable at all.
MUTATIONS: dict[str, tuple[Build, str, str]] = {
    # --- per-operation payload shape --------------------------------------
    "payload: input of the wrong shape": (
        _on_request(TARGET, lambda r: r.__setitem__("input", {"wrong": 1})),
        "request input does not satisfy its canonical schema",
        "mutated",
    ),
    "payload: result of the wrong shape": (
        _on_response(TARGET, lambda r: r.__setitem__("result", {"wrong": 1})),
        "success result does not satisfy its canonical schema",
        "mutated",
    ),
    "payload: another operation's result": (
        _on_response(TARGET, lambda r: r.__setitem__("result", {"records": [], "page": {}})),
        "success result does not satisfy its canonical schema",
        "mutated",
    ),
    "payload: input carries an unrepresentable field": (
        _on_request(TARGET, lambda r: r["input"].__setitem__("x_unrepresentable", 1)),
        "canonical schema",
        "mutated",
    ),
    "payload: semantically invalid input": (
        _on_request(
            "context_pack.build/primary-success",
            lambda r: r["input"].__setitem__("mode", "balanced"),
        ),
        # "request input", not just "fails ... semantics": the shorter phrase also
        # matches the *result* gate's message, so this entry passed with the whole
        # input dispatch removed.
        "request input fails 'context_pack.build' semantics",
        "mutated",
    ),
    # --- request metadata against the catalogue ---------------------------
    "scope: a required scope is missing": (
        _on_request(TARGET, lambda r: r["metadata"].__setitem__("scopes", [])),
        "does not satisfy the catalogue",
        "mutated",
    ),
    "capability: the required declaration is absent": (
        _on_request(TARGET, lambda r: r["metadata"].__setitem__("required_capabilities", [])),
        "does not satisfy the catalogue",
        "mutated",
    ),
    "capability: below the catalogue minimum": (
        _on_request(
            TARGET,
            lambda r: r["metadata"]["required_capabilities"][0].__setitem__(
                "minimum_version", "0.9"
            ),
        ),
        "does not satisfy the catalogue",
        "mutated",
    ),
    "precondition: supplied where none is honoured": (
        _on_request(
            TARGET,
            lambda r: r["metadata"].__setitem__(
                "mutation_precondition", {"record_version": "v1"}
            ),
        ),
        "does not satisfy the catalogue",
        "mutated",
    ),
    "precondition: required but absent": (
        _on_request(
            "record.supersede/primary-success",
            lambda r: r["metadata"].pop("mutation_precondition"),
        ),
        "does not satisfy the catalogue",
        "mutated",
    ),
    "idempotency: a required key is absent": (
        _on_request(
            "memory.create/primary-success", lambda r: r["metadata"].pop("idempotency_key")
        ),
        "does not satisfy the catalogue",
        "mutated",
    ),
    "operation: the request names a different one": (
        _on_request(TARGET, lambda r: r.__setitem__("operation", "memory.list")),
        "names operation",
        "mutated",
    ),
    # --- request/response linkage -----------------------------------------
    "identifier: response answers a different request": (
        _on_response(
            TARGET, lambda r: r["metadata"].__setitem__("request_id", "req-someone-else")
        ),
        "does not match the request",
        "mutated",
    ),
    "identifier: correlation id mismatched": (
        _on_response(TARGET, lambda r: r["metadata"].__setitem__("correlation_id", "corr-x")),
        "does not match the request",
        "mutated",
    ),
    # --- branch and error posture -----------------------------------------
    "branch: error returned where success is declared": (
        # Caught by case well-formedness now, *before* the adapter is called:
        # a case declaring the success branch while recording an error is
        # incoherent on its face, and there is no reason to ask an adapter
        # anything about it.
        _on_response(TARGET, _swap_to_error_branch),
        "a success case must record a result",
        "mutated",
    ),
    "error code: one the operation may not raise": (
        # The case's *declaration* moves with the wire value, so the
        # declared-code check cannot fire first and the allow-list rule is what
        # is actually under test. `memory.get` is a POINT_READ, which does not
        # permit `token_limit_exceeded`.
        _combine(
            _on_response(
                "error/not_found",
                lambda r: r.__setitem__(
                    "error",
                    {
                        "code": "token_limit_exceeded",
                        "message": "x",
                        "retry_class": "non_retryable",
                    },
                ),
            ),
            _on_fields(
                "error/not_found",
                error_code="token_limit_exceeded",
                retry_class="non_retryable",
            ),
        ),
        "not permitted to fail with",
        "mutated",
    ),
    "retry class: the case contradicts the frozen mapping": (
        _on_fields("error/not_found", retry_class="retryable"),
        "is frozen as",
        "mutated",
    ),
    # --- replay classification and outcome --------------------------------
    "replay: declared a replay but the input differs": (
        _on_request(
            "memory.create/honest-replay",
            lambda r: r["input"]["content"].__setitem__("conformance", "different"),
        ),
        "classifies as",
        "mutated",
    ),
    "replay: a conflict declared as a replay": (
        _on_fields("memory.create/idempotency-conflict", idempotency="replay"),
        "classifies as",
        "mutated",
    ),
    "replay: the outcome differs from the original": (
        _on_response(
            "memory.create/honest-replay",
            lambda r: r["result"]["record"].__setitem__("content", {"different": True}),
        ),
        "must return the same result as",
        "mutated",
    ),
    # --- token binding and pagination -------------------------------------
    "token: page 2 presents a token page 1 never issued": (
        _on_request(
            "memory.list/page-2",
            lambda r: r["input"]["page"].__setitem__("continuation_token", "forged"),
        ),
        "must present the continuation token",
        "mutated",
    ),
    # --- event ordering, via the frozen job.events rule --------------------
    "ordering: job.events sequences run backwards": (
        _on_response(
            "job.events/page-2",
            lambda r: r["result"].__setitem__("events", list(reversed(r["result"]["events"]))),
        ),
        "fails 'job.events' semantics",
        "mutated",
    ),
    # --- provenance / temporal loss ---------------------------------------
    "provenance: a value the codec cannot carry": (
        _on_response(TARGET, lambda r: r["metadata"].__setitem__("x_unknown", {"lost": True})),
        "round trip",
        "mutated",
    ),
    "temporal: a governed record loses its temporal block": (
        _on_response(TARGET, lambda r: r["result"]["record"]["provenance"].pop("temporal")),
        "success result does not satisfy its canonical schema",
        "mutated",
    ),
    # --- observable only against the original recording --------------------
    "drift: the recorded result changes": (
        _on_response(TARGET, lambda r: r.__setitem__("result", {"records": []})),
        "does not match the recorded response",
        "original",
    ),
    "request id: the adapter has no such recorded exchange": (
        _on_request(TARGET, lambda r: r["metadata"].__setitem__("request_id", "req-absent")),
        "no recorded exchange",
        "original",
    ),
}


@pytest.mark.parametrize("label", sorted(MUTATIONS), ids=sorted(MUTATIONS))
def test_the_runner_rejects_every_frozen_mutation_category(
    label: str, corpus: tuple[AdapterConformanceCase, ...]
) -> None:
    build, expected, adapter_source = MUTATIONS[label]
    cases = tuple(build(corpus))
    adapter = _fake(corpus if adapter_source == "original" else cases)
    with pytest.raises(AdapterConformanceError) as caught:
        run_adapter_conformance(adapter, cases)
    assert expected in caught.value.reason, (
        f"{label}: expected a failure mentioning {expected!r}, got {caught.value.reason!r}"
    )


def test_the_mutation_matrix_defines_a_probe_for_every_frozen_category() -> None:
    """A completeness check on the matrix *definition*, and only that.

    Stated plainly because the name this test used to carry -- "covers every
    frozen category" -- reads as evidence about the runner, and it is not:
    everything below is derived from `MUTATIONS` itself, so **this test cannot
    fail for a runner defect**. It answers one question only, which no executed
    probe can answer: whether a frozen category has no probe at all.

    What proves the runner rejects them is
    `test_the_runner_rejects_every_frozen_mutation_category`, which is
    parametrized over this same dict -- so every entry here is executed, and each
    asserts its own diagnostic rather than merely that something was raised.
    """
    # Each entry must state a diagnostic and which recording to replay; an entry
    # missing either would be executed and assert nothing useful.
    malformed = [
        label
        for label, entry in MUTATIONS.items()
        if len(entry) != 3 or not entry[1] or entry[2] not in {"mutated", "original"}
    ]
    assert not malformed, malformed
    for category in (
        "payload",
        "scope",
        "capability",
        "precondition",
        "idempotency",
        "operation",
        "identifier",
        "branch",
        "error code",
        "retry class",
        "replay",
        "token",
        "ordering",
        "provenance",
        "temporal",
        "drift",
        "request id",
    ):
        assert any(label.startswith(category) for label in MUTATIONS), category


def _corpus_state(corpus: tuple[AdapterConformanceCase, ...]) -> list[dict[str, Any]]:
    """Both halves of every exchange, plus the facts a case declares.

    Comparing only `case.request` left a shallow-copy leak in `_on_response`
    invisible: a probe that edited a response in place would corrupt its
    neighbours and this guard would still be green.
    """
    return [
        {
            "request": dict(case.request),
            "response": dict(case.response),
            "trusted": dict(case.trusted),
        }
        for case in corpus
    ]


def test_mutating_cases_never_disturbs_the_corpus(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A probe that corrupts shared state silently repairs or breaks its neighbours."""
    before = copy.deepcopy(_corpus_state(corpus))
    for build, _, _ in MUTATIONS.values():
        build(corpus)
    assert _corpus_state(corpus) == before


# --------------------------------------------------------------------------
# Adapter input ownership
# --------------------------------------------------------------------------

_INPUT_MUTATORS: dict[str, Mutator] = {
    "top-level field": lambda r: r.__setitem__("operation", "memory.list"),
    "nested input field": lambda r: r["input"].__setitem__("record_id", "hijacked"),
    "nested metadata list": lambda r: r["metadata"]["scopes"].append("memory:write"),
    "deleted field": lambda r: r["metadata"].pop("trace_id"),
}


@pytest.mark.parametrize("label", sorted(_INPUT_MUTATORS), ids=sorted(_INPUT_MUTATORS))
def test_an_adapter_that_edits_its_request_is_rejected(
    label: str, corpus: tuple[AdapterConformanceCase, ...]
) -> None:
    """A request is the caller's to own.

    An adapter that rewrote one in place would corrupt a retry, a replay
    comparison, or an idempotency fingerprint computed from it -- each of which
    would then measure the adapter's edit rather than the caller's intent.
    """
    recorded = {case.request_id: case.response for case in corpus}
    mutate = _INPUT_MUTATORS[label]

    class Editing:
        def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            mutate(request)  # type: ignore[arg-type]
            return recorded[request["metadata"]["request_id"]]

    with pytest.raises(AdapterConformanceError, match="mutated the request envelope"):
        run_adapter_conformance(Editing(), [_case(corpus, TARGET)])


def test_a_full_run_leaves_the_corpus_untouched(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    before = copy.deepcopy(_corpus_state(corpus))
    run_adapter_conformance(_fake(corpus), corpus)
    assert _corpus_state(corpus) == before


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.__setitem__("format", "something.else"), "format"),
        (lambda d: d.__setitem__("cases", {}), "cases"),
        (lambda d: d["cases"][0]["expect"].__setitem__("branch", "sideways"), "branch"),
        (lambda d: d["cases"][0]["expect"].__setitem__("idempotency", "maybe"), "idempotency"),
        (lambda d: d["cases"][0].pop("operation"), "operation"),
        (lambda d: d["cases"][0].__setitem__("trusted", []), "trusted"),
    ],
)
def test_the_loader_rejects_a_malformed_corpus(
    monkeypatch: pytest.MonkeyPatch, mutate: Callable[[Any], None], expected: str
) -> None:
    document = _corpus_document()
    mutate(document)
    monkeypatch.setattr(resources, "read_fixture", lambda name: document)
    with pytest.raises(ContractSemanticError, match=expected):
        load_adapter_conformance_corpus()


def test_the_loader_and_the_runner_share_one_integrity_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicated case is rejected on the load path exactly as on the run path."""
    document = _corpus_document()
    document["cases"].append(copy.deepcopy(document["cases"][0]))
    monkeypatch.setattr(resources, "read_fixture", lambda name: document)
    with pytest.raises(AdapterConformanceError, match="duplicate case id"):
        load_adapter_conformance_corpus()


def test_the_loader_materializes_the_shared_defaults(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A case is always a complete exchange, never a fragment plus a lookup."""
    defaults = _corpus_document()["defaults"]["request_metadata"]
    case = _case(corpus, TARGET)
    for key, value in defaults.items():
        assert case.request["metadata"][key] == value


# --------------------------------------------------------------------------
# Second-round re-acceptance findings, as permanent regressions.
#
# Each of these was an independently constructed counterexample that the harness
# accepted, or wrongly rejected. They are pinned individually rather than folded
# into the mutation matrix so a regression names the property that broke.
# --------------------------------------------------------------------------


def test_a_calendar_invalid_timestamp_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A pattern is not a calendar.

    `2024-99-99T99:99:99Z` satisfies the `Timestamp` pattern character for
    character and is not a date. `format: date-time` is declared by the canonical
    schema, so it is evaluated rather than merely listed as supported.
    """
    case = _case(corpus, TARGET)
    response = copy.deepcopy(dict(case.response))
    response["result"]["record"]["provenance"]["temporal"]["recorded_at"] = (
        "2024-99-99T99:99:99Z"
    )
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="not a valid RFC 3339 date-time"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_graph_traversal_semantics_are_applied(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """`graph.traverse` has frozen input and result rules, and both are wired."""
    from omnivia_core.contracts.v1 import conformance

    assert "graph.traverse" in conformance._INPUT_SEMANTICS
    assert "graph.traverse" in conformance._RESULT_SEMANTICS

    case = _case(corpus, "graph.traverse/primary-success")
    request = copy.deepcopy(dict(case.request))
    request["input"]["direction"] = "sideways"
    cases = [dataclasses.replace(case, request=request)]
    # Named exactly, and on the *input* side. A bare `pytest.raises` here was
    # satisfied by the result gate, so removing the input dispatch entirely left
    # this test green.
    with pytest.raises(AdapterConformanceError, match="request input fails 'graph.traverse'"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def _semantics_modules() -> tuple[Any, ...]:
    """Every module that can define a frozen validator, not just the obvious four.

    `compatibility` belongs here and was missing. It holds
    `validate_version_window` -- which *is* wired -- alongside
    `validate_capability_set` and `validate_version_capability_envelope`, which
    were not, and the completeness test could not see the omission because it
    never looked in this module. Discovery that skips a module reintroduces the
    hand-maintained list the type dispatch exists to replace.
    """
    from omnivia_core.contracts.v1 import (
        compatibility,
        semantics,
        semantics_evidence,
        semantics_jobs,
        semantics_knowledge,
    )

    return (compatibility, semantics, semantics_evidence, semantics_jobs, semantics_knowledge)


def test_every_operation_with_frozen_semantics_has_them_wired() -> None:
    """No operation may be quietly absent from the operation dispatch tables."""
    from omnivia_core.contracts.v1 import conformance

    modules = _semantics_modules()
    for name, entry in CATALOGUE.items():
        # Derive the validator name from the *definition the catalogue binds*,
        # not from the operation name: `graph.traverse` binds `GraphTraversal`,
        # so its rules are `validate_graph_traversal_*`.
        definition = entry.input_schema_ref.rsplit("/", 1)[-1].removesuffix("Input")
        stem = re.sub(r"(?<!^)(?=[A-Z])", "_", definition).lower()
        for kind, table in (
            ("input", conformance._INPUT_SEMANTICS),
            ("result", conformance._RESULT_SEMANTICS),
        ):
            exists = any(hasattr(m, f"validate_{stem}_{kind}") for m in modules)
            assert exists == (name in table), (
                f"{name}: a frozen {kind} validator exists={exists} but wired={name in table}"
            )


def test_every_frozen_type_validator_is_wired() -> None:
    """No *type* validator may be absent from the nested dispatch table either.

    Operation-name discovery cannot see these. `validate_workspace_compatibility`
    and `validate_governed_record` belong to types that appear *inside* results,
    so the earlier completeness test could not have found them missing -- and it
    did not: `workspace.list` accepted a format version outside its supported
    range, and `memory.get` accepted a proposed record claiming canonical
    authority, both while that test was green.

    Discovering by type closes the class rather than the two instances of it: a
    frozen single-argument validator for a generated type must be wired, and a
    new one cannot be forgotten the same way.
    """
    import dataclasses as dc
    import inspect

    from omnivia_core.contracts.v1 import conformance, generated

    discovered: dict[type, str] = {}
    for module in _semantics_modules():
        for name in dir(module):
            if not name.startswith("validate_"):
                continue
            camel = "".join(part.title() for part in name[len("validate_") :].split("_"))
            declared = getattr(generated, camel, None)
            if declared is None or not dc.is_dataclass(declared):
                continue
            signature = inspect.signature(getattr(module, name))
            required = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind is not inspect.Parameter.VAR_KEYWORD
            ]
            # Single-argument validators need no trusted context, so they apply
            # wherever their type appears. Relational ones are keyed by operation
            # in `_RESULT_SEMANTICS` instead, because they need facts a bare value
            # does not carry.
            if len(required) == 1:
                discovered.setdefault(declared, name)

    # Operation payload types are dispatched by operation, not by type.
    operation_payloads = {
        getattr(generated, ref.rsplit("/", 1)[-1])
        for entry in OPERATION_CATALOGUE
        for ref in (entry.input_schema_ref, entry.result_schema_ref)
    }
    expected = {t for t in discovered if t not in operation_payloads}
    missing = sorted(t.__name__ for t in expected - set(conformance._NESTED_SEMANTICS))
    assert not missing, f"frozen type validator(s) not wired: {missing}"


def test_every_wired_type_rule_is_reachable_or_declared_inert() -> None:
    """A wired rule that no payload can reach does no work; say which, and why.

    `_NESTED_SEMANTICS[OperationIdempotencyMetadata]` never fires: that type is a
    field of `OperationMetadata`, a catalogue entry, so it appears in no input, no
    result and no response envelope. It stays wired because the completeness rule
    is "every frozen single-argument type validator is wired", and an exception
    maintained by hand is exactly how `WorkspaceCompatibility` came to be missed.

    Pinning the inert set here means the entry is a checked fact rather than an
    unexplained one: wire a rule for another unreachable type and this fails, and
    so does putting this type into a payload without noticing the rule woke up.
    """
    import dataclasses as dc
    import typing

    from omnivia_core.contracts.v1 import conformance, generated

    roots = {generated.ResponseMetadata}
    for entry in OPERATION_CATALOGUE:
        for ref in (entry.input_schema_ref, entry.result_schema_ref):
            roots.add(getattr(generated, ref.rsplit("/", 1)[-1]))

    reachable: set[type] = set()

    def visit(declared: Any) -> None:
        if not (isinstance(declared, type) and dc.is_dataclass(declared)):
            for argument in typing.get_args(declared):
                visit(argument)
            return
        if declared in reachable:
            return
        reachable.add(declared)
        for name, annotation in typing.get_type_hints(declared, vars(generated)).items():
            del name
            visit(annotation)

    for root in roots:
        visit(root)

    inert = {t.__name__ for t in conformance._NESTED_SEMANTICS if t not in reachable}
    assert inert == {"OperationIdempotencyMetadata"}, inert


def test_a_case_may_not_declare_a_workspace_its_request_does_not_select(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The declared workspace judges the result, so it may not be invented.

    Left uncorrelated, a case could declare one tenant, send another, and have
    its own tenancy check measured against the declaration rather than the call.
    """
    case = dataclasses.replace(_case(corpus, TARGET), workspace_id="ws-other")
    with pytest.raises(AdapterConformanceError, match="but its request selects"):
        validate_case_collection([case])


def test_opaque_content_carrying_a_workspace_id_key_is_not_tenancy_metadata(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A `workspace_id` key inside an opaque `JsonObject` is caller data.

    A governed record's `content` is a value the contract says nothing about. A
    record legitimately carrying `{"workspace_id": "external-domain-value"}` must
    not be refused for it -- the tenancy rule walks declared fields, not keys
    that happen to share a name.
    """
    case = _case(corpus, TARGET)
    response = copy.deepcopy(dict(case.response))
    response["result"]["record"]["content"]["workspace_id"] = "external-domain-value"
    cases = [dataclasses.replace(case, response=response)]
    assert run_adapter_conformance(_fake(tuple(cases)), cases) == 1


def test_a_malformed_nested_trusted_fact_does_not_leak(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A declared trusted fact of the wrong runtime type is a case defect."""
    case = dataclasses.replace(
        _case(corpus, "knowledge.search/primary-success"), trusted={"authorized_views": 7}
    )
    with pytest.raises(AdapterConformanceError, match="must be list"):
        run_adapter_conformance(_fake((case,)), [case])


def test_a_link_must_resolve_to_an_earlier_exchange(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """An exchange cannot continue one that has not happened.

    Rejecting cycles is not enough: a second page ordered before the request that
    issued its token would "pass" in an order no caller could perform.
    """
    first = _case(corpus, "memory.list/primary-success")
    second = _case(corpus, "memory.list/page-2")
    assert validate_case_collection([first, second])
    with pytest.raises(AdapterConformanceError, match="does not come earlier"):
        validate_case_collection([second, first])


@pytest.mark.parametrize(
    ("case_id", "path"),
    [
        ("workspace.list/primary-success", ("workspaces", 0)),
        ("workspace.create/primary-success", ("workspace",)),
        ("workspace.inspect/primary-success", ("workspace",)),
    ],
)
def test_workspace_compatibility_semantics_are_applied(
    corpus: tuple[AdapterConformanceCase, ...], case_id: str, path: tuple[Any, ...]
) -> None:
    """A workspace may not report a format version outside its supported range.

    `WorkspaceCompatibility` has a frozen validator, but no `workspace.*`
    operation has an operation-level result rule, so nothing applied it until the
    nested type dispatch existed.
    """
    case = _case(corpus, case_id)
    response = copy.deepcopy(dict(case.response))
    node: Any = response["result"]
    for key in path:
        node = node[key]
    node["compatibility"]["workspace_format_version"] = "9.0"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="WorkspaceCompatibility"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    ("case_id", "path"),
    [
        ("memory.get/primary-success", ("record",)),
        ("memory.list/primary-success", ("records", 0)),
        ("memory.search/primary-success", ("records", 0)),
    ],
)
def test_governed_record_semantics_are_applied_to_memory_reads(
    corpus: tuple[AdapterConformanceCase, ...], case_id: str, path: tuple[Any, ...]
) -> None:
    """A proposed record may not claim canonical authority.

    The same `GovernedRecord` invariants hold wherever a record arrives -- alone,
    inside a page, or nested deeper. Dispatching on type is what reaches all
    three; dispatching on operation reached none of them.
    """
    case = _case(corpus, case_id)
    response = copy.deepcopy(dict(case.response))
    node: Any = response["result"]
    for key in path:
        node = node[key]
    node["provenance"]["identity"]["governance_state"] = "proposed"
    node["authority_level"] = "canonical"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="GovernedRecord"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_nested_semantics_reach_values_no_operation_rule_covers(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The point of type dispatch: depth and position do not matter.

    A `RecordProvenance` nested three levels inside a Context Pack is subject to
    the same rule as one at the top of a `memory.get` result.
    """
    from omnivia_core.contracts.v1 import conformance

    assert len(conformance._NESTED_SEMANTICS) >= 11
    case = _case(corpus, "context_pack.build/primary-success")
    response = copy.deepcopy(dict(case.response))
    records = response["result"]["records"]
    assert records, "the pack must carry a record for this to prove anything"
    records[0]["provenance"]["identity"]["governance_state"] = "proposed"
    records[0]["authority_level"] = "canonical"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="GovernedRecord"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


# --------------------------------------------------------------------------
# Adversarial-review findings, as permanent regressions.
# --------------------------------------------------------------------------


def test_a_synchronous_operation_may_not_announce_a_job(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A caller reading a job reference would believe async work had begun.

    The frozen rule existed with no call site anywhere in production code.
    """
    case = _case(corpus, "job.get/primary-success")
    response = copy.deepcopy(dict(case.response))
    response["metadata"]["job"] = {"job_id": "job-never-started"}
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="completes synchronously"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_response_envelope_metadata_is_subject_to_nested_type_rules(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The walk must not stop at the payload boundary.

    Twelve of the corpus's freshness statements live on `response.metadata`, and
    only one operation consumed one, so the rest were unchecked.
    """
    case = _case(corpus, "knowledge.search/primary-success")
    response = copy.deepcopy(dict(case.response))
    response["metadata"]["freshness"]["projection_versions"]["ghost_index"] = "pv-9"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="ProjectionFreshness"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("nan", lambda r: r["metadata"].__setitem__("x", float("nan"))),
        ("infinity", lambda r: r["metadata"].__setitem__("x", float("inf"))),
        ("bytes", lambda r: r["metadata"].__setitem__("x", b"raw")),
        ("set", lambda r: r["metadata"].__setitem__("x", {1, 2})),
        ("object", lambda r: r["metadata"].__setitem__("x", object())),
    ],
)
def test_an_uncanonicalizable_recorded_value_is_a_conformance_failure(
    corpus: tuple[AdapterConformanceCase, ...], label: str, build: Callable[[Any], None]
) -> None:
    """These arrive from a caller's own case, so they name an exchange.

    Previously each raised a bare `TypeError`/`ValueError` from `json.dumps`
    with no case id to act on.
    """
    case = _case(corpus, TARGET)
    response = copy.deepcopy(dict(case.response))
    build(response)
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="cannot be canonicalized"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_circular_document_is_refused_rather_than_exhausting_the_stack(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    case = _case(corpus, TARGET)
    response = copy.deepcopy(dict(case.response))
    response["self"] = response
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="circular reference"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_repeated_sub_object_is_not_mistaken_for_a_cycle(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The guard tracks the containers being walked, not every one seen.

    A document may legitimately carry the same sub-object twice; only a
    container that contains itself is a cycle.
    """
    from omnivia_core.contracts.v1.conformance import _plain

    shared = {"a": 1}
    assert _plain({"x": shared, "y": shared}) == {"x": {"a": 1}, "y": {"a": 1}}


@pytest.mark.parametrize(
    "views", [[["nested"]], [{"a": 1}], [None], [7]], ids=["list", "dict", "none", "int"]
)
def test_a_trusted_string_set_is_guarded_element_by_element(
    corpus: tuple[AdapterConformanceCase, ...], views: list[Any]
) -> None:
    """Checking only the container left an unhashable element to raise from `frozenset`."""
    case = dataclasses.replace(
        _case(corpus, "graph.traverse/primary-success"), trusted={"authorized_views": views}
    )
    with pytest.raises(AdapterConformanceError, match="must be a string"):
        run_adapter_conformance(_fake((case,)), [case])


@pytest.mark.parametrize("cases", [42, None, object()], ids=["int", "none", "object"])
def test_a_non_iterable_cases_argument_is_refused(cases: Any) -> None:
    with pytest.raises(AdapterConformanceError, match="must be iterable"):
        validate_case_collection(cases)


# --------------------------------------------------------------------------
# Checks that could be deleted with the suite green
#
# Each of these gates ran on every case and no test failed when its body was
# removed, so the suite proved only that they did not *crash*. One probe each,
# asserting the gate's own diagnostic.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case_id", "path"),
    [
        ("memory.get/primary-success", ("record",)),
        ("memory.list/primary-success", ("records", 0)),
        ("workspace.inspect/primary-success", ("workspace",)),
    ],
)
def test_a_result_naming_another_workspace_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...], case_id: str, path: tuple[Any, ...]
) -> None:
    """The cross-cutting tenancy rule, on the reads that have no result validator.

    These are exactly the operations where returning another tenant's record
    would be most damaging and least visible, and nothing exercised the check.
    """
    case = _case(corpus, case_id)
    response = copy.deepcopy(dict(case.response))
    node: Any = response["result"]
    for key in path:
        node = node[key]
    node["workspace_id"] = "ws-other"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="but the request selected 'ws-1'"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    ("case_id", "mutate", "expected"),
    [
        (
            "context_pack.build/primary-success",
            lambda i: i.__setitem__("mode", "balanced"),
            "request input fails 'context_pack.build' semantics",
        ),
        (
            "graph.traverse/primary-success",
            lambda i: i.__setitem__("direction", "sideways"),
            "request input fails 'graph.traverse' semantics",
        ),
        (
            "knowledge.search/primary-success",
            lambda i: i.__setitem__("view", "nonsense"),
            "request input fails 'knowledge.search' semantics",
        ),
    ],
    ids=["context_pack.build", "graph.traverse", "knowledge.search"],
)
def test_the_input_dispatch_is_reached_for_every_operation_that_has_one(
    corpus: tuple[AdapterConformanceCase, ...],
    case_id: str,
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """`_check_payload_semantics` -- the whole input table -- was unobserved."""
    case = _case(corpus, case_id)
    request = copy.deepcopy(dict(case.request))
    mutate(request["input"])
    cases = [dataclasses.replace(case, request=request)]
    with pytest.raises(AdapterConformanceError, match=re.escape(expected)):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize("element", [42, None, {"id": "x"}, "a-case-id"])
def test_a_collection_element_that_is_not_a_case_is_refused(
    corpus: tuple[AdapterConformanceCase, ...], element: Any
) -> None:
    """`AdapterConformanceCase` is frozen, not validated; anything may arrive.

    Reaching for `.id` on whatever turns up is how a public API leaks
    `AttributeError` instead of naming the problem.
    """
    with pytest.raises(AdapterConformanceError, match="expected an AdapterConformanceCase"):
        validate_case_collection([_case(corpus, TARGET), element])


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s.__setitem__("citation_ids", []), "fewer than minItems 1"),
        (
            lambda s: s.__setitem__("citation_ids", ["cit-1", "cit-1"]),
            "items must be unique",
        ),
        (lambda s: s.__setitem__("citation_ids", [7]), "expected string, got int"),
    ],
    ids=["minItems", "uniqueItems", "items"],
)
def test_the_canonical_array_keywords_are_enforced(
    corpus: tuple[AdapterConformanceCase, ...],
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """`_validate_array` has 127 live call sites and nothing observed any of them."""
    case = _case(corpus, "context_pack.build/primary-success")
    response = copy.deepcopy(dict(case.response))
    mutate(response["result"]["sections"][0])
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match=re.escape(expected)):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_the_canonical_maximum_item_count_is_enforced() -> None:
    """The one array keyword no corpus payload can currently violate.

    Every `maxItems` this contract declares is 64 or more, and reaching one means
    tripping a uniqueness, ordering or reference rule first, so this branch is
    exercised against the validator directly rather than through a case.
    """
    from omnivia_core.contracts.v1 import conformance

    schemas = conformance._CanonicalSchemas()
    schema = {"type": "array", "maxItems": 2, "items": {"type": "string"}}
    assert conformance._validate_against_schema(["a", "b"], schema, schemas, "x") == []
    assert conformance._validate_against_schema(["a", "b", "c"], schema, schemas, "x") == [
        "x: more than maxItems 2"
    ]


def test_a_request_that_cannot_be_re_encoded_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The request round trip: tolerant decoding drops what the build cannot carry.

    An envelope field the contract cannot represent decodes cleanly and is
    silently lost, so the recorded request and the one an adapter would actually
    be sent are not the same document.
    """
    case = _case(corpus, TARGET)
    request = copy.deepcopy(dict(case.request))
    request["metadata"]["x_unrepresentable"] = "dropped on decode"
    cases = [dataclasses.replace(case, request=request)]
    with pytest.raises(AdapterConformanceError, match="request does not survive a decode/encode"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda r: r["metadata"]["authority"].__setitem__(
                "roles", [f"role-{index:03d}" for index in range(65)]
            ),
            "metadata.authority.roles: more than maxItems 64",
        ),
        (
            lambda r: r["metadata"]["authority"].__setitem__("principal_id", ""),
            "metadata.authority.principal_id",
        ),
    ],
    ids=["maxItems", "minLength"],
)
def test_the_response_envelope_speaks_the_canonical_wire_language(
    corpus: tuple[AdapterConformanceCase, ...],
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """The envelope was only decoded and round-tripped, never schema-checked.

    A decode proves a document is representable, not that it is legal: a response
    attesting 65 roles against a declared maximum of 64 survived both and
    conformed. Every pattern and bound the envelope schema declares went
    unenforced while the payload inside it was checked in full.
    """
    case = _case(corpus, TARGET)
    response = copy.deepcopy(dict(case.response))
    mutate(response)
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match=re.escape(expected)):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_the_request_envelope_speaks_the_canonical_wire_language(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    case = _case(corpus, TARGET)
    request = copy.deepcopy(dict(case.request))
    request["metadata"]["request_id"] = "!! not an identifier !!"
    cases = [dataclasses.replace(case, request=request)]
    with pytest.raises(
        AdapterConformanceError, match="request envelope.metadata.request_id"
    ):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_an_error_envelope_is_named_by_its_own_branch(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Validating against the `ResponseEnvelope` union names nothing.

    A `oneOf` reports only "matched 0 of 2 alternatives", which identifies
    neither the field nor the rule; the branch is already known here, so the
    failing field can be named.
    """
    case = _case(corpus, "error/not_found")
    response = copy.deepcopy(dict(case.response))
    response["error"]["retry_class"] = "sideways"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError) as caught:
        run_adapter_conformance(_fake(tuple(cases)), cases)
    assert "alternatives" not in caught.value.reason, caught.value.reason


def test_every_payload_object_schema_is_closed() -> None:
    """What makes the *payload* round trip unable to fire, stated as a fact.

    `_check_payload` validates against the canonical schema before it round-trips
    the decode, so an undeclared field is refused by the schema and never reaches
    the round trip -- which is why removing that comparison fails no test. That
    holds only while every object schema closes itself. An open one would accept a
    field the dataclass drops, and the round trip would become the only thing
    standing between the corpus and a silently truncated payload.

    Pinned here so the round trip's redundancy is a checked property rather than
    an accident, and so opening an object is a deliberate act with a test to
    answer to.
    """
    open_objects: list[str] = []
    for path in sorted(CANONICAL_SCHEMAS_DIR.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))

        def walk(node: Any, trail: list[str], source: str = path.stem) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    closed = (
                        node.get("unevaluatedProperties") is False
                        or node.get("additionalProperties") is False
                    )
                    if not closed:
                        open_objects.append(f"{source}: {'.'.join(trail)}")
                for key, value in node.items():
                    walk(value, [*trail, key])
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, [*trail, str(index)])

        walk(document, [])
    assert not open_objects, open_objects


# --------------------------------------------------------------------------
# A continuation token keeps the binding it was issued with (R11)
# --------------------------------------------------------------------------


def test_a_continuation_token_keeps_the_binding_it_was_issued_with(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The R11 counterexample: one token, two different snapshots.

    Generic pagination proved page two presents the token page one issued, but
    each page declared its own binding, so the same token could name a four-event
    snapshot on the way out and a five-event snapshot on the way back -- with each
    page internally coherent and both schema-valid.
    """
    first = _case(corpus, "job.events/primary-success")
    second = _case(corpus, "job.events/page-2")
    trusted = copy.deepcopy(dict(second.trusted))
    trusted["request_page_binding"]["snapshot_event_count"] = 5
    response = copy.deepcopy(dict(second.response))
    response["result"]["snapshot_event_count"] = 5
    cases = [first, dataclasses.replace(second, trusted=trusted, response=response)]
    with pytest.raises(AdapterConformanceError, match="when it was issued"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_one_token_may_not_be_issued_with_two_different_bindings(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The same hazard as R11, seen from the issuing side.

    Retaining the first binding and ignoring later ones would let whichever
    exchange the transcript happens to list first decide what an opaque token
    means, while a second issuing exchange said something else about it.
    """
    first = _case(corpus, "job.events/primary-success")
    reissued = _relabel(first, "job.events/reissued", "req-job.events-again")
    reissued.response["result"]["snapshot_event_count"] = 5
    trusted = copy.deepcopy(dict(first.trusted))
    trusted["result_page_binding"]["snapshot_event_count"] = 5
    cases = [first, dataclasses.replace(reissued, trusted=trusted)]
    with pytest.raises(AdapterConformanceError, match="bound differently"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_page_need_not_restate_a_binding_the_collection_already_holds(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The issuing exchange is authoritative, so the declaration is optional."""
    first = _case(corpus, "job.events/primary-success")
    second = _case(corpus, "job.events/page-2")
    cases = [first, dataclasses.replace(second, trusted={})]
    assert run_adapter_conformance(_fake(tuple(cases)), cases) == 2


def test_a_trusted_binding_still_serves_a_token_with_no_issuing_exchange(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A transcript may legitimately begin mid-pagination."""
    second = dataclasses.replace(_case(corpus, "job.events/page-2"), page_of=None)
    assert second.trusted.get("request_page_binding")
    assert run_adapter_conformance(_fake((second,)), [second]) == 1


# --------------------------------------------------------------------------
# Immutable job origin facts survive across exchanges (R10)
# --------------------------------------------------------------------------


def test_a_terminal_import_result_is_bound_to_the_source_import_start_accepted(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The completion must report the import that actually happened."""
    start = _case(corpus, "import.start/primary-success")
    done = _case(corpus, "job.get/succeeded-observation")
    response = copy.deepcopy(dict(done.response))
    response["result"]["terminal_result"]["result"]["source"]["source_version"] = "v9"
    cases = [start, dataclasses.replace(done, response=response)]
    with pytest.raises(AdapterConformanceError, match="not the immutable descriptor"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_trusted_source_may_not_contradict_the_observed_origin(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The R10 counterexample: the drift hid behind a trusted fact moved to match.

    Both documents stayed schema-valid and each object was individually fine.
    Because `accepted_import_source` came from the case rather than from the
    `import.start` that established it, the trusted block was a second, mutable
    statement of an immutable fact, and the two agreed only because one author
    wrote both.
    """
    start = _case(corpus, "import.start/primary-success")
    done = _case(corpus, "job.get/succeeded-observation")
    response = copy.deepcopy(dict(done.response))
    response["result"]["terminal_result"]["result"]["source"]["source_version"] = "v9"
    trusted = copy.deepcopy(dict(done.trusted))
    trusted["accepted_import_source"]["source_version"] = "v9"
    cases = [start, dataclasses.replace(done, response=response, trusted=trusted)]
    with pytest.raises(AdapterConformanceError, match="contradicts the source"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_one_job_may_not_be_started_from_two_different_sources(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The immutable fact is fixed once, so two origins contradict each other.

    The job identity is unchanged, so the handle correlation cannot see this --
    the accepted source is not part of the handle.
    """
    first = _case(corpus, "import.start/primary-success")
    again = _relabel(first, "import.start/again", "req-import-again")
    again.request["metadata"]["idempotency_key"] = "idem-import-again"
    again.request["input"]["source"]["source_version"] = "v9"
    cases = [first, again]
    with pytest.raises(AdapterConformanceError, match="from a different source than"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_trusted_source_still_serves_a_collection_with_no_originating_exchange(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A transcript may legitimately record a later reading without the origin."""
    done = _case(corpus, "job.get/succeeded-observation")
    assert "accepted_import_source" in done.trusted
    assert run_adapter_conformance(_fake((done,)), [done]) == 1


def test_a_pre_call_job_handle_is_server_context_not_a_stale_observation(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Policy, recorded deliberately rather than left implicit.

    A `job.cancel`/`job.retry` trusted `previous_job_handle` states the handle the
    *server* held when it performed the transition. An earlier `job.get` in the
    same collection may report different control availability and still be
    truthful: the client's reading can be stale by the time the control call runs,
    and the contract treats the pre-call handle as server context for exactly that
    reason. So it is *not* reconciled against earlier observations, unlike the
    immutable import source in R10, which can never legitimately differ.

    If that policy is ever reversed, this test is where the decision is recorded.
    """
    retry = _case(corpus, "job.retry/primary-success")
    previous = retry.trusted["previous_job_handle"]
    observed = _case(corpus, "job.get/failed-observation").response["result"]["job"]
    assert previous["identity"]["job_id"] == observed["identity"]["job_id"]
    assert run_adapter_conformance(_fake(corpus), corpus) == len(corpus)


# --------------------------------------------------------------------------
# Cross-cutting response rules cover both branches (R13)
# --------------------------------------------------------------------------


def test_an_error_response_is_subject_to_nested_metadata_semantics(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """These rules were applied only inside the success branch.

    A response envelope is the same document whichever branch it takes, and the
    canonical schema enforces structure rather than relational semantics -- so a
    schema-valid error envelope could contradict the frozen contract freely.
    """
    case = _case(corpus, "error/not_found")
    response = copy.deepcopy(dict(case.response))
    response["metadata"]["freshness"] = {
        "as_of": "2024-01-02T00:00:00Z",
        "projection_versions": {"knowledge_index": "pv-1"},
        "projection_watermarks": {"a_different_projection": "wm-1"},
        "stale": False,
    }
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="ProjectionFreshness"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_synchronous_operation_may_not_announce_a_job_when_it_fails(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A job reference misleads a caller no less on a failure than on a success."""
    case = _case(corpus, "error/not_found")
    assert CATALOGUE[case.operation].job.completion_mode == "synchronous"
    response = copy.deepcopy(dict(case.response))
    response["metadata"]["job"] = {"job_id": "job-never-started"}
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="completes synchronously"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_the_cross_cutting_response_rules_run_on_every_recorded_exchange(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Both branches, not just the 47 success exchanges."""
    errors = [case for case in corpus if case.branch == "error"]
    assert len(errors) >= 20, len(errors)


# --------------------------------------------------------------------------
# Terminal job results (O4)
# --------------------------------------------------------------------------


def test_the_corpus_exercises_both_terminal_result_branches(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """No case carried a `terminal_result` at all, so three frozen rules were dead.

    `validate_job_terminal_result`, `validate_job_attempt_history` and
    `validate_import_completion_result` were never reached by the corpus.
    """
    kinds = {
        next(k for k in ("result", "error", "cancellation") if k in terminal)
        for case in corpus
        if isinstance(terminal := case.response.get("result", {}).get("terminal_result"), Mapping)
    }
    assert {"result", "error"} <= kinds, kinds


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda t: t["result"]["source"].__setitem__("source_version", "v9"),
            "source is not the immutable descriptor import.start accepted",
        ),
        (
            lambda t: t["result"].__setitem__("import_run_id", "job-9"),
            "import_run_id 'job-9' does not equal the job id 'job-1'",
        ),
        (
            lambda t: t["result"].__setitem__("skipped_items", 2),
            "discovered_items 3 does not equal",
        ),
        (
            lambda t: t["result"].__setitem__("partial", True),
            "partial must be exactly failed_items > 0",
        ),
        (
            lambda t: t["attempts"].pop(),
            "final attempt must be 'succeeded' to match the terminal branch",
        ),
    ],
    ids=["source", "run-id", "counts", "partial", "attempt-history"],
)
def test_a_terminal_import_completion_is_checked_against_the_accepted_run(
    corpus: tuple[AdapterConformanceCase, ...],
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """`validate_import_completion_result` had no corpus case to run against.

    Each expectation names the specific diagnostic, so a probe that starts
    failing earlier for an unrelated reason is a test failure rather than a
    silent change of subject.
    """
    case = _case(corpus, "job.get/succeeded-observation")
    response = copy.deepcopy(dict(case.response))
    mutate(response["result"]["terminal_result"])
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match=re.escape(expected)):
        run_adapter_conformance(_fake(tuple(cases)), cases)


# --------------------------------------------------------------------------
# Two exchanges naming the same job must agree (O3)
# --------------------------------------------------------------------------


def test_the_corpus_observes_one_job_across_several_exchanges(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Otherwise the correlation has nothing to correlate."""
    observing = [
        case.id
        for case in corpus
        if case.branch == "success"
        and case.replay_of is None
        and json.dumps(case.response.get("result", {})).count('"job_id": "job-1"') > 0
    ]
    assert len(observing) >= 3, observing


def test_the_corpus_exercises_a_real_progression_not_just_equality(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A timeline of identical handles would satisfy the rule without testing it."""
    states = [
        case.response["result"]["job"]["state"]
        for case in corpus
        if case.branch == "success"
        and case.replay_of is None
        and isinstance(case.response.get("result"), Mapping)
        and isinstance(case.response["result"].get("job"), Mapping)
        and case.response["result"]["job"]["identity"]["job_id"] == "job-1"
    ]
    assert len(set(states)) > 1, states


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("created_at", lambda job: job.__setitem__("created_at", "2026-07-29T00:00:00Z")),
        (
            "audit_reference",
            lambda job: job["identity"].__setitem__("audit_reference", "audit-9"),
        ),
        # Still after its own `created_at`, so only the comparison with the
        # earlier exchange can catch it.
        ("updated_at", lambda job: job.__setitem__("updated_at", "2026-07-30T00:00:30Z")),
    ],
)
def test_two_exchanges_describing_the_same_job_must_agree(
    corpus: tuple[AdapterConformanceCase, ...],
    label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """The O3 counterexample: `job.get` contradicting `import.start`.

    `validate_job_observation_progression` was reachable only through the trusted
    `previous_job_handle` a case declared for itself, so two *recorded* exchanges
    naming `job-1` were never compared and either could say anything.
    """
    first = _case(corpus, "import.start/primary-success")
    later = _case(corpus, "job.get/primary-success")
    response = copy.deepcopy(dict(later.response))
    mutate(response["result"]["job"])
    cases = [first, dataclasses.replace(later, response=response)]
    with pytest.raises(AdapterConformanceError, match="describes the same job"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_job_may_not_run_backwards_across_exchanges(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A job observed failed cannot later be observed running again."""
    failed = _case(corpus, "job.get/failed-observation")
    running = _relabel(
        _case(corpus, "job.get/primary-success"), "job.get/again", "req-job.get-again"
    )
    cases = [failed, running]
    with pytest.raises(AdapterConformanceError, match="describes the same job"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_declaring_a_replay_does_not_buy_an_exemption_from_job_correlation(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Only a classification of `replay` pins a result, so only it may be skipped.

    A case may declare `replay_of` while carrying a *different* idempotency key.
    That classifies as `distinct`, so nothing requires its result to equal the
    original's -- and treating the declaration alone as "this restates an earlier
    answer" let such a case contradict an earlier exchange about the same job for
    free.
    """
    first = _case(corpus, "import.start/primary-success")
    sneaky = _relabel(
        first,
        "import.start/declared-but-distinct",
        "req-import-declared-distinct",
        replay_of=first.id,
        idempotency="distinct",
    )
    sneaky.request["metadata"]["idempotency_key"] = "idem-import-other"
    sneaky.response["result"]["job"]["created_at"] = "2020-01-01T00:00:00Z"
    cases = [first, sneaky]
    with pytest.raises(AdapterConformanceError, match="describes the same job"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    "case_id",
    ["import.start/honest-replay", "job.cancel/honest-replay", "job.retry/honest-replay"],
)
def test_a_job_control_replay_may_carry_a_progressed_handle(
    corpus: tuple[AdapterConformanceCase, ...], case_id: str
) -> None:
    """The frozen rule allows it; byte equality rejected it.

    A replayed control call answers from the recorded outcome, but the job goes on
    evolving between the original call and the replay. `validate_job_control_replay`
    fixes the operation, the job identity and the disposition and checks the handle
    as an observation progression -- so a replay reporting a later `updated_at` is
    conforming, and the harness refusing it was refusing a conforming provider.
    """
    replay = _case(corpus, case_id)
    origin = _case(corpus, replay.replay_of or "")
    response = copy.deepcopy(dict(replay.response))
    response["result"]["job"]["updated_at"] = "2026-07-30T00:02:00Z"
    cases = [origin, dataclasses.replace(replay, response=response)]
    assert run_adapter_conformance(_fake(tuple(cases)), cases) == 2


def test_an_import_start_replay_may_observe_the_job_after_it_finished(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The remaining half of R12: a terminal progression, not just a timestamp.

    `validate_import_start_result` requires a *fresh* `import.start` to describe a
    non-terminal job -- a freshly started job that had already finished never
    existed. An honest replay may observe that same job after it succeeded, so
    running the fresh validator against a replay rejected a conforming provider.
    A timestamp-only probe does not reach this; the state has to become terminal.
    """
    first = _case(corpus, "import.start/primary-success")
    replay = _case(corpus, "import.start/honest-replay")
    response = copy.deepcopy(dict(replay.response))
    response["result"]["job"] = copy.deepcopy(
        _case(corpus, "job.get/succeeded-observation").response["result"]["job"]
    )
    cases = [first, dataclasses.replace(replay, response=response)]
    assert run_adapter_conformance(_fake(tuple(cases)), cases) == 2


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda r: r["metadata"].__setitem__("job", {"job_id": "job-elsewhere"}),
            "references job 'job-elsewhere' but its result describes 'job-1'",
        ),
        (
            lambda r: r["metadata"].pop("job", None),
            "references job None but its result describes 'job-1'",
        ),
    ],
    ids=["different-job", "absent-reference"],
)
def test_an_import_start_replay_still_states_which_job_it_answers_for(
    corpus: tuple[AdapterConformanceCase, ...],
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """Skipping the fresh validator must not lose what else it established.

    `import.start` names the job it started twice -- in the result handle and in
    `ResponseMetadata.job` -- and a caller holding one id while tracking the other
    watches the wrong job. Dropping the whole validator for replays would have
    traded R12's false rejection for a false certification.
    """
    first = _case(corpus, "import.start/primary-success")
    replay = _case(corpus, "import.start/honest-replay")
    response = copy.deepcopy(dict(replay.response))
    mutate(response)
    cases = [first, dataclasses.replace(replay, response=response)]
    with pytest.raises(AdapterConformanceError, match=re.escape(expected)):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_fresh_import_start_may_not_return_a_finished_job(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The fresh constraint still applies where it belongs.

    Relaxing it for replays must not relax it for the call that starts the job.
    """
    first = _case(corpus, "import.start/primary-success")
    response = copy.deepcopy(dict(first.response))
    response["result"]["job"] = copy.deepcopy(
        _case(corpus, "job.get/succeeded-observation").response["result"]["job"]
    )
    cases = [dataclasses.replace(first, response=response)]
    with pytest.raises(AdapterConformanceError, match="has not reached"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    "case_id",
    ["import.start/honest-replay", "job.cancel/honest-replay", "job.retry/honest-replay"],
)
def test_a_job_control_replay_may_not_name_a_different_job(
    corpus: tuple[AdapterConformanceCase, ...], case_id: str
) -> None:
    """Semantic equivalence is not laxity: identity is still compared in full."""
    replay = _case(corpus, case_id)
    origin = _case(corpus, replay.replay_of or "")
    response = copy.deepcopy(dict(replay.response))
    response["result"]["job"]["identity"]["audit_reference"] = "audit-drifted"
    cases = [origin, dataclasses.replace(replay, response=response)]
    with pytest.raises(AdapterConformanceError, match="a replay names the same job"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_an_honest_replay_restates_rather_than_observes(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A replay must answer with the recorded outcome, so it may look "stale".

    `import.start/honest-replay` answers with `job-1` as it was when the original
    call ran. Held to a progression rule it would look like the job running
    backwards, and correctly replaying a finished call would be a conformance
    failure.
    """
    cases = [
        _case(corpus, "import.start/primary-success"),
        _case(corpus, "job.get/failed-observation"),
        _case(corpus, "import.start/honest-replay"),
    ]
    assert run_adapter_conformance(_fake(tuple(cases)), cases) == 3


# --------------------------------------------------------------------------
# The exchange, not the declaration, triggers the check (O2)
# --------------------------------------------------------------------------


def _relabel(
    case: AdapterConformanceCase, case_id: str, request_id: str, **changes: Any
) -> AdapterConformanceCase:
    """Copy a case under a new id, keeping the request/response correlated."""
    request = copy.deepcopy(dict(case.request))
    response = copy.deepcopy(dict(case.response))
    request["metadata"] = {**request["metadata"], "request_id": request_id}
    response["metadata"] = {**response["metadata"], "request_id": request_id}
    return dataclasses.replace(
        case, id=case_id, request=request, response=response, **changes
    )


def test_no_two_unrelated_exchanges_share_an_idempotency_key(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A key is what says "this is the same call again".

    Twenty-two error scenarios reused `candidate.approve`'s key. Nothing objected
    while the comparison fired only on a declared `expect.replay_of`.
    """
    origin: dict[tuple[str, str, str | None, str], str] = {}
    for case in corpus:
        key = case.request.get("metadata", {}).get("idempotency_key")
        if not key:
            continue
        fingerprint = (case.operation, case.principal_id, case.workspace_id, key)
        previous = origin.setdefault(fingerprint, case.id)
        if previous != case.id:
            assert case.replay_of == previous, (
                f"{case.id} shares {key!r} with {previous} without declaring the repeat"
            )


def test_an_undeclared_repeat_is_still_classified_and_held_to_its_outcome(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The O2 counterexample: a network-level repeat that mutated twice.

    Same principal, workspace, idempotency key and byte-identical input -- only
    `request_id` differs, which is exactly what a retried request looks like --
    answered with a different job. Accepted while `expect.replay_of` was the
    trigger; the key alone has to be enough.
    """
    first = _case(corpus, "import.start/primary-success")
    repeat = _relabel(
        first,
        "import.start/network-repeat",
        "req-import-network-repeat",
        replay_of=None,
        idempotency=None,
    )
    repeat.response["result"]["job"]["identity"]["job_id"] = "job-2"
    if repeat.response["metadata"].get("job"):
        repeat.response["metadata"]["job"] = {
            **repeat.response["metadata"]["job"],
            "job_id": "job-2",
        }
    cases = [first, repeat]
    # `import.start` replays are judged by the frozen semantic rule rather than by
    # byte equality, so the duplicate mutation is named precisely: the second call
    # started a second job.
    with pytest.raises(
        AdapterConformanceError, match="a second job or a second mutation was performed"
    ):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_an_undeclared_continuation_is_still_bound_to_its_query(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Presenting page one's token under a changed query, without declaring it."""
    first = _case(corpus, "evidence.search/primary-success")
    page_two = _case(corpus, "evidence.search/page-2")
    request = copy.deepcopy(dict(page_two.request))
    request["input"] = {**request["input"], "query": "a completely different question"}
    cases = [first, dataclasses.replace(page_two, request=request, page_of=None)]
    with pytest.raises(AdapterConformanceError, match="under a different request"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_declared_repeat_that_contradicts_the_key_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The declaration is an assertion about the exchange, so it must agree."""
    first = _case(corpus, "import.start/primary-success")
    unrelated = _relabel(first, "import.start/unrelated", "req-import-unrelated")
    unrelated.request["metadata"]["idempotency_key"] = "idem-import-unrelated"
    repeat = _relabel(
        first,
        "import.start/mislabelled-repeat",
        "req-import-mislabelled",
        replay_of="import.start/unrelated",
        idempotency="replay",
    )
    cases = [first, unrelated, repeat]
    with pytest.raises(AdapterConformanceError, match="the declaration and the exchange disagree"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_declared_continuation_that_contradicts_the_token_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    first = _case(corpus, "evidence.search/primary-success")
    other = _relabel(first, "evidence.search/other-page-one", "req-evidence-other")
    other.response["result"]["page"]["continuation_token"] = "cursor-evidence.search-other"
    page_two = dataclasses.replace(
        _case(corpus, "evidence.search/page-2"), page_of="evidence.search/other-page-one"
    )
    cases = [first, other, page_two]
    with pytest.raises(AdapterConformanceError, match="the token it presents was issued by"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


# --------------------------------------------------------------------------
# The exchange, not the declaration, states the answer (O1)
# --------------------------------------------------------------------------


def test_the_corpus_declares_no_authority_or_actor_identity(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """Identity is a fact the exchange carries, so no case may restate it.

    While these lived in `trusted`, a Context Pack could attest it was built
    under `principal-1` in an exchange the server attested as `user-42`, and the
    check compared the pack against the declaration written beside it.
    """
    restated = {"expected_authority", "expected_reviewer_id", "expected_actor_id"}
    for case in corpus:
        assert not restated & set(case.trusted), f"{case.id} restates {restated & set(case.trusted)}"


def test_a_pack_attesting_a_principal_the_exchange_denies_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """The live false certification: this exact split was accepted before."""
    case = _case(corpus, "context_pack.build/primary-success")
    response = copy.deepcopy(dict(case.response))
    response["result"]["reproducibility"]["authorization_context"]["authority"][
        "principal_id"
    ] = "principal-1"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="authority.principal_id"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_no_exchange_can_attest_a_duplicate_capability_id() -> None:
    """What keeps a laxer frozen rule out of reach, pinned as a property.

    `GrantedAuthority` is judged differently depending on where it sits. On a
    response envelope, duplicate capability ids are refused. Inside a Context
    Pack's `authorization_context`, `validate_context_pack_build_result` compares
    `(id, version)` pairs and accepts `memory.read` at both 1.0 and 1.1 -- the
    corpus carried exactly that until the pack's expectation began coming from
    the exchange.

    Repairing that divergence means changing `semantics_knowledge`, which is
    outside this slice's frozen file list, so it is reported rather than done.
    What closes it *here* is the envelope: a pack's authority must equal the one
    the response attests, and the response cannot attest a duplicate id. This
    test pins that load-bearing half, so relaxing the envelope path fails loudly
    instead of quietly reopening the gap.
    """
    from omnivia_core.contracts.v1.codec import decode_response

    document = _corpus_document()
    metadata = {
        **copy.deepcopy(document["defaults"]["response_metadata"]),
        "request_id": "req-duplicate-capability",
    }
    metadata["authority"]["capabilities"] = [
        {"id": "memory.read", "version": "1.0"},
        {"id": "memory.read", "version": "1.1"},
    ]
    with pytest.raises(Exception, match="duplicate capability id"):
        decode_response({"metadata": metadata, "result": {}})


def test_a_pack_attesting_authority_the_exchange_never_granted_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A pack may not claim a capability the response did not attest."""
    case = _case(corpus, "context_pack.build/primary-success")
    response = copy.deepcopy(dict(case.response))
    response["result"]["reproducibility"]["authorization_context"]["authority"][
        "capabilities"
    ] = [{"id": "memory.read", "version": "1.0"}]
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="capabilit"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


@pytest.mark.parametrize(
    "case_id",
    [
        "candidate.approve/primary-success",
        "candidate.reject/primary-success",
        "record.supersede/primary-success",
        "knowledge.propose/primary-success",
    ],
)
def test_a_transition_attributed_to_someone_other_than_the_caller_is_rejected(
    corpus: tuple[AdapterConformanceCase, ...], case_id: str
) -> None:
    """All four governance operations judged the actor against `trusted`.

    Every one of them recorded `reviewer-1`/`actor-1` in an exchange the server
    attested as `user-42`, and every one of them passed.
    """
    case = _case(corpus, case_id)
    response = copy.deepcopy(dict(case.response))
    for event in response["result"]["updated_record"]["provenance"]["history"]:
        if event.get("action") == case.operation:
            event["actor_id"] = "reviewer-1"
    cases = [dataclasses.replace(case, response=response)]
    with pytest.raises(AdapterConformanceError, match="reviewer-1"):
        run_adapter_conformance(_fake(tuple(cases)), cases)


def test_a_case_may_not_declare_a_principal_its_response_does_not_attest(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """`principal_id` decides comparability, so it may not be invented.

    It selects which exchanges an idempotency comparison may be made between and
    which actor a governance transition is judged against; left uncorrelated, a
    case could declare one principal, run as another, and be measured against the
    identity it made up.
    """
    case = dataclasses.replace(_case(corpus, TARGET), principal_id="someone-else")
    with pytest.raises(AdapterConformanceError, match="but its response attests"):
        run_adapter_conformance(_fake((case,)), [case])


def test_a_base_exception_from_the_adapter_is_normalized(
    corpus: tuple[AdapterConformanceCase, ...],
) -> None:
    """A transport failure that is not an `Exception` is still a seam failure."""

    class Rude:
        def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            raise BaseException("something below Exception")  # noqa: TRY002

    with pytest.raises(AdapterConformanceError, match="adapter raised BaseException"):
        run_adapter_conformance(Rude(), [_case(corpus, TARGET)])
