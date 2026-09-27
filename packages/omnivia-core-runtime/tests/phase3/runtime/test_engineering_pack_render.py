"""The pure engineering pack stage: replay, counting, budgets, partitions, checksum."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from omnivia_core_runtime.service import engineering_pack
from omnivia_core_runtime.service.engineering_pack import (
    BYTE_ONLY_COUNTING_MODE,
    BuildContext,
    MandatoryContextTooLarge,
    PackRecord,
    WorkingItem,
    build_pack,
    build_pack_byte_only,
)
from omnivia_core_runtime.storage.context_pack import (
    CONTEXT_PACK_TOKENIZER_ID,
    CONTEXT_PACK_TOKENIZER_VERSION,
)
from referencing import Registry, Resource

from omnivia_core.contracts.v1 import to_canonical_json

NOTICE = "Target applicability is not evaluated in this build."
FIXTURE_DIR = Path(__file__).with_name("fixtures")

CTX = BuildContext(
    resolved_at_us=1_700_000_000_000_000,
    workspace_id="ws-1",
    principal_id="p-1",
    query="auth",
    profile="investigate",
    mode="diagnostic",
    targets=({"snapshot_id": "esnap-a", "snapshot_kind": "git_commit"},),
    source_coverage=(),
    requested_budget=None,
    effective_tokens=4000,
    effective_bytes=16384,
    projection_version="proj-1",
    applicability_evaluator="eval-1",
)
ACCEPTED = PackRecord(
    "rec-b", "ver-1", "accepted_knowledge", "Auth", "Provider A, naïve 認証 `f(x)`;"
)
CANDIDATE = PackRecord("rec-a", "ver-2", "candidate_findings", "Guess", "Maybe provider B.")
V2_WORKING = (
    WorkingItem("ck-1", 3, "Resume ‘auth’", ("call a.b()", "終わり!")),
)
V2_CTX = dataclasses.replace(
    CTX,
    requested_budget={
        "model_bytes": 16384,
        "hydrations": 16,
        "evidence_bytes": 524288,
    },
    counting_mode=BYTE_ONLY_COUNTING_MODE,
    effective_hydrations=8,
    effective_evidence_bytes=262144,
)


def _build(
    ctx: BuildContext = CTX, records: Any = (CANDIDATE, ACCEPTED), working: Any = ()
) -> Any:
    return build_pack(
        ctx,
        tuple(records),
        tuple(working),
        notice=NOTICE,
        uncertainties=[NOTICE],
        omissions=[],
    )


def _tokens(text: str) -> int:
    # Independent restatement of context-pack.tokenizer.v1.
    return len(re.findall(r"[^\W_]+|[^\s]", text))


def _build_v2(
    ctx: BuildContext = V2_CTX,
    records: Any = (CANDIDATE, ACCEPTED),
    working: Any = V2_WORKING,
) -> Any:
    return build_pack_byte_only(
        ctx,
        tuple(records),
        tuple(working),
        notice=NOTICE,
        uncertainties=[NOTICE],
        omissions=[],
    )


def test_rendered_pack_matches_the_published_result_schema() -> None:
    schema_dir = (
        Path(__file__).resolve().parents[5]
        / "contracts"
        / "application"
        / "v1"
        / "schemas"
    )
    resources = []
    for path in sorted(schema_dir.glob("*.schema.json")):
        resource = Resource.from_contents(json.loads(path.read_text(encoding="utf-8")))
        resource_id = resource.id()
        assert resource_id is not None
        resources.append((resource_id, resource))
    validator = Draft202012Validator(
        {
            "$ref": "https://contracts.omnivia.dev/application/v1/engineering.schema.json"
            "#/$defs/EngineeringContextBuildResult"
        },
        registry=Registry().with_resources(resources),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    for pack in (
        _build(),
        _build(dataclasses.replace(CTX, requested_budget={"model_tokens": 1000})),
        _build_v2(),
    ):
        assert [
            (list(error.absolute_path), error.message)
            for error in validator.iter_errors({"pack": pack})
        ] == []


def test_legacy_v1_matches_the_prechange_canonical_golden() -> None:
    expected = (FIXTURE_DIR / "engineering_context_v1_golden.json").read_text(
        encoding="utf-8"
    ).strip()
    actual = to_canonical_json(_build())
    assert actual == expected
    assert (
        _build()["pack_id"]
        == "sha256:270959fb56b2e5f863e521ee67dfe4c27409e9175fe0b94761e558165d4550f8"
    )


def test_byte_only_v2_matches_its_canonical_golden_and_exact_utf8_count() -> None:
    expected = (FIXTURE_DIR / "engineering_context_v2_byte_only_golden.json").read_text(
        encoding="utf-8"
    ).strip()
    pack = _build_v2()
    text = pack["rendering"]["text"]
    assert to_canonical_json(pack) == expected
    assert pack["format_version"] == "engineering_context.v2"
    assert pack["rendering"]["byte_count"] == len(text.encode("utf-8"))
    assert pack["rendering"]["byte_count"] <= pack["budget"]["effective"]["model_bytes"]
    assert NOTICE in text
    assert "[accepted_knowledge]" in text
    assert "[candidate_findings]" in text
    assert "[working_context]" in text
    assert "[cite-1]" in text
    assert "[uncited checkpoint ck-1#3]" in text
    assert "naïve 認証 `f(x)`" in text
    assert "終わり!" in text


def test_byte_only_v2_contains_no_token_or_tokenizer_fields() -> None:
    pack = _build_v2()
    forbidden = {
        "model_tokens",
        "rendered_tokens",
        "token_count",
        "tokenizer_id",
        "tokenizer_version",
    }

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert forbidden.isdisjoint(keys(pack))
    assert "token estimate" not in to_canonical_json(pack).lower()
    assert pack["normalized_request"]["counting_mode"] == BYTE_ONLY_COUNTING_MODE
    assert pack["reproducibility"]["counting_mode"] == BYTE_ONLY_COUNTING_MODE


def test_byte_only_v2_replay_and_digest_inputs_are_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _build_v2()
    assert to_canonical_json(first) == to_canonical_json(_build_v2())
    assert first["pack_id"] == _build_v2()["pack_id"]
    assert _build_v2(dataclasses.replace(V2_CTX, resolved_at_us=1))["pack_id"] != first[
        "pack_id"
    ]
    assert _build_v2(dataclasses.replace(V2_CTX, effective_bytes=16000))[
        "pack_id"
    ] != first["pack_id"]
    assert _build()["pack_id"] != first["pack_id"]

    monkeypatch.setattr(engineering_pack, "BYTE_ONLY_RENDERER_VERSION", "eng-render-test")
    renderer_changed = _build_v2()["pack_id"]
    assert renderer_changed != first["pack_id"]
    monkeypatch.setattr(engineering_pack, "BYTE_ONLY_BUILDER_VERSION", "eng-build-test")
    assert _build_v2()["pack_id"] != renderer_changed


def test_same_frozen_context_replays_to_identical_bytes_and_checksum() -> None:
    first, second = _build(), _build(records=(ACCEPTED, CANDIDATE))
    assert to_canonical_json(first) == to_canonical_json(second)
    assert first["pack_id"] == second["pack_id"]


def test_a_different_evaluation_instant_changes_the_checksum() -> None:
    later = dataclasses.replace(CTX, resolved_at_us=CTX.resolved_at_us + 1)
    assert _build(later)["pack_id"] != _build()["pack_id"]


def test_checksum_is_independently_recomputable() -> None:
    pack = json.loads(json.dumps(_build()))
    pack["reproducibility"].pop("artifact_checksum")
    root = pack.pop("pack_id")
    digest = "sha256:" + hashlib.sha256(to_canonical_json(pack).encode()).hexdigest()
    assert root == digest


def test_counts_cover_the_whole_rendering_under_the_named_tokenizer() -> None:
    pack = _build(
        working=(WorkingItem("ck-1", 3, "Resume ‘auth’", ("a.b()", "終わり!")),)
    )
    text = pack["rendering"]["text"]
    assert (
        NOTICE in text and "[cite-1]" in text and "[uncited checkpoint ck-1#3]" in text
    )
    assert pack["rendering"]["token_count"] == _tokens(text)
    assert pack["rendering"]["byte_count"] == len(text.encode("utf-8")) > len(text)
    repro = pack["reproducibility"]
    assert repro["tokenizer_id"] == CONTEXT_PACK_TOKENIZER_ID
    assert repro["tokenizer_version"] == CONTEXT_PACK_TOKENIZER_VERSION
    assert "not a model tokenizer" in repro["tokenizer_note"]
    assert pack["budget"]["rendered_tokens"] == _tokens(text)


def test_partitions_stay_separate_and_accepted_renders_first() -> None:
    pack = _build()
    assert [s["partition"] for s in pack["sections"]] == [
        "accepted_knowledge",
        "candidate_findings",
    ]
    assert pack["citations"][0]["record_ref"] == {"record_id": "rec-b", "version": "ver-1"}


def test_tight_token_budget_drops_optional_sections_but_keeps_notice_and_accepted() -> (
    None
):
    mandatory = _build(records=(ACCEPTED,))["rendering"]["token_count"]
    tight = dataclasses.replace(CTX, effective_tokens=mandatory)
    pack = _build(tight, working=(WorkingItem("ck-1", 1, "Obj", ("x",)),))
    assert [s["partition"] for s in pack["sections"]] == ["accepted_knowledge"]
    assert {o["reason"] for o in pack["omissions"]} == {"budget"}
    assert [c["citation_id"] for c in pack["citations"]] == ["cite-1"]
    assert pack["rendering"]["token_count"] <= mandatory
    assert NOTICE in pack["rendering"]["text"]


def test_tight_byte_budget_is_enforced_simultaneously() -> None:
    mandatory_bytes = _build(records=(ACCEPTED,))["rendering"]["byte_count"]
    tight = dataclasses.replace(CTX, effective_bytes=mandatory_bytes)
    pack = _build(tight)
    assert pack["rendering"]["byte_count"] <= mandatory_bytes
    assert [s["partition"] for s in pack["sections"]] == ["accepted_knowledge"]


def test_an_oversized_optional_section_is_dropped_whole() -> None:
    big = PackRecord("rec-z", "ver-1", "candidate_findings", "Big", "word " * 5000)
    pack = _build(records=(ACCEPTED, big))
    assert [s["partition"] for s in pack["sections"]] == ["accepted_knowledge"]
    assert pack["omissions"] == [{"field": "sec-2", "reason": "budget"}]


def test_a_mandatory_rendering_that_cannot_fit_is_refused() -> None:
    with pytest.raises(MandatoryContextTooLarge):
        _build(dataclasses.replace(CTX, effective_tokens=1))


def test_byte_only_drops_optional_sections_whole_and_refuses_too_small_mandatory() -> (
    None
):
    mandatory = _build_v2(records=(ACCEPTED,), working=())
    cap = mandatory["rendering"]["byte_count"]
    tight = dataclasses.replace(V2_CTX, effective_bytes=cap)
    packed = _build_v2(tight, records=(ACCEPTED, CANDIDATE), working=V2_WORKING)
    assert [section["partition"] for section in packed["sections"]] == [
        "accepted_knowledge"
    ]
    assert packed["citations"] == [
        {
            "citation_id": "cite-1",
            "record_ref": {"record_id": "rec-b", "version": "ver-1"},
        }
    ]
    assert all(omission["reason"] == "budget" for omission in packed["omissions"])
    with pytest.raises(MandatoryContextTooLarge):
        _build_v2(dataclasses.replace(V2_CTX, effective_bytes=cap - 1), records=(ACCEPTED,), working=())


def test_authorized_selection_order_survives_within_a_partition() -> None:
    # Two candidates in the same partition: the frozen upstream order
    # (relevance/priority), not record_id, must decide render order.
    first = PackRecord("rec-z", "ver-1", "candidate_findings", "First", "picked first")
    second = PackRecord("rec-a", "ver-1", "candidate_findings", "Second", "picked second")
    pack = _build(records=(ACCEPTED, first, second))
    assert [c["record_ref"]["record_id"] for c in pack["citations"]] == [
        "rec-b",
        "rec-z",
        "rec-a",
    ]


_COVERED = (
    {"snapshot_id": "esnap-a", "stream_id": "s", "sequence": 1, "manifest_digest": "d"},
)


def test_applicability_is_not_evaluated_when_every_candidate_is_dropped() -> None:
    ctx = dataclasses.replace(CTX, source_coverage=_COVERED)
    big = PackRecord("rec-z", "ver-1", "candidate_findings", "Big", "word " * 5000)
    pack = _build(ctx, records=(big,))
    assert pack["sections"] == []
    assert pack["citations"] == []
    assert {a["status"] for a in pack["applicability"]} == {"not_evaluated"}


def test_applicability_is_matched_when_a_record_survives() -> None:
    ctx = dataclasses.replace(CTX, source_coverage=_COVERED)
    pack = _build(ctx, records=(ACCEPTED,))
    assert {a["status"] for a in pack["applicability"]} == {"matched"}
