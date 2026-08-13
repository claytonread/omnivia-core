"""The copied A6 connector fixture corpus is the accepted corpus, byte for byte.

The corpus is authority, not input: every conformance case, every boundary
value and every golden lineage digest the kit checks comes out of these bytes.
So the first thing worth testing is that they are the bytes the A6-R3 lane
published -- a corpus that had drifted would validate happily against itself
and against a kit written to match its drift.

`jsonschema` lives here rather than in the kit on purpose. `omnivia-core`
declares no third-party dependency, and a loader that imported an analyser would
put it on the import path of every consumer of the public connector contract.
The kit does its own structural and digest validation; this module does the
JSON Schema validation, which is where a third-party analyser belongs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from omnivia_core.connector.conformance import (
    EXPECTED_CASE_IDS,
    CorpusError,
    load_corpus,
    verify_corpus_digests,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "docs" / "quality" / "fixtures" / "core-connectors"

#: The exact digests `manifest.json` records for the A6-R3 corpus. Restated here
#: so a manifest edited in step with the file it describes still fails.
ACCEPTED_DIGESTS = {
    "README.md": "f6cce438505871712d6d9b1984e7875cd1af9ec83492600cfb1095d8e032d084",
    "connector-cases.json": (
        "1ee69e0403076be2a062d865be437957339e81ae581a0ddef16bcfb2f1416d41"
    ),
    "schema.json": "3faee5d833e3e3f0f06a0c53286d5885c8eeaffb60e538901866d099d1af0559",
    "SHA256SUMS": "2dcee89819a474272962bb72d8b5677e6a8f88059a69c1cf3989ee6abbec201a",
}


def _digest(name: str) -> str:
    return hashlib.sha256((CORPUS_DIR / name).read_bytes()).hexdigest()


def test_the_directory_holds_exactly_the_five_accepted_files() -> None:
    assert sorted(item.name for item in CORPUS_DIR.iterdir()) == [
        "README.md",
        "SHA256SUMS",
        "connector-cases.json",
        "manifest.json",
        "schema.json",
    ]


@pytest.mark.parametrize("name", sorted(ACCEPTED_DIGESTS))
def test_each_copied_file_carries_its_accepted_digest(name: str) -> None:
    assert _digest(name) == ACCEPTED_DIGESTS[name]


def test_the_manifest_agrees_with_the_bytes_on_disk() -> None:
    """Size *and* digest, so a same-length edit cannot hide."""
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text("utf-8"))
    entries = [*manifest["files"], manifest["checksum_file"]]
    for entry in entries:
        path = CORPUS_DIR / entry["path"]
        assert path.stat().st_size == entry["bytes"], entry["path"]
        assert _digest(entry["path"]) == entry["sha256"], entry["path"]
    assert manifest["self_hashed"] is False
    listed = {entry["path"] for entry in manifest["files"]}
    assert "manifest.json" not in listed


def test_sha256sums_verifies_and_the_kit_recomputes_it() -> None:
    assert set(verify_corpus_digests(CORPUS_DIR)) == {
        "README.md",
        "connector-cases.json",
        "schema.json",
    }


def test_a_drifted_corpus_is_refused_rather_than_loaded(tmp_path: Path) -> None:
    """The digest check is load-bearing, not decorative."""
    for item in CORPUS_DIR.iterdir():
        (tmp_path / item.name).write_bytes(item.read_bytes())
    cases = tmp_path / "connector-cases.json"
    cases.write_bytes(cases.read_bytes().replace(b"CON-C001", b"CON-C999", 1))
    with pytest.raises(CorpusError):
        load_corpus(tmp_path)


def test_the_corpus_validates_against_its_own_schema() -> None:
    schema = json.loads((CORPUS_DIR / "schema.json").read_text("utf-8"))
    Draft202012Validator.check_schema(schema)
    document = json.loads((CORPUS_DIR / "connector-cases.json").read_text("utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document), key=lambda e: e.path
    )
    assert errors == []


def test_the_corpus_carries_exactly_the_sixty_five_accepted_cases() -> None:
    corpus = load_corpus(CORPUS_DIR)
    assert len(EXPECTED_CASE_IDS) == 65
    assert tuple(case.id for case in corpus.cases) == EXPECTED_CASE_IDS
    assert len(corpus.requirement_ids) == 35
    assert len(corpus.decision_ids) == 18
    assert len(corpus.dependency_ids) == 9


def test_every_boundary_the_corpus_fixes_is_false() -> None:
    """The three limits are machine-checkable rather than re-argued: the corpus
    claims no sandbox, no remote completeness proof and no information-flow
    enforcement, and this repository may not start claiming them either."""
    corpus = load_corpus(CORPUS_DIR)
    assert len(corpus.boundaries) == 13
    assert set(corpus.boundaries.values()) == {False}
    for key in (
        "corpus_claims_connector_process_sandbox",
        "corpus_claims_remote_completeness_proof",
        "corpus_claims_information_flow_enforcement",
    ):
        assert corpus.boundaries[key] is False


def test_the_cursor_contract_constants_are_the_ones_the_host_implements() -> None:
    from omnivia_core.connector.host import (
        CURSOR_DIGEST_DOMAIN_TAG,
        CURSOR_DIGEST_FIELD_ORDER,
    )
    from omnivia_core.connector.spi import MAX_CURSOR_PAYLOAD_BYTES

    contract = load_corpus(CORPUS_DIR).cursor_contract
    assert len(contract) == 29
    assert contract["predecessor_digest_algorithm"] == "sha256"
    assert contract["predecessor_digest_domain_tag"] == CURSOR_DIGEST_DOMAIN_TAG.decode()
    assert contract["max_opaque_payload_bytes"] == MAX_CURSOR_PAYLOAD_BYTES
    assert contract["opaque_payload_encoding"] == "base64url_no_padding"
    assert contract["predecessor_digest_computed_by"] == "host_only"
    assert contract["predecessor_digest_field_order"] == "_".join(
        CURSOR_DIGEST_FIELD_ORDER
    )
    assert contract["host_interprets_opaque_payload"] is False
    assert contract["no_skipped_evidence_is_host_verifiable"] is False
    assert contract["cursor_non_disclosure_enforced_in_process"] is False
    assert contract["migration_preserves_witness"] == "exact_equality"
    assert (
        contract["migration_preserves_predecessor_digest"]
        == "byte_identical_including_absence"
    )
    assert contract["migration_state_version_direction"] == "strictly_increasing"
