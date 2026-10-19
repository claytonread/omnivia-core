"""The reference implementation, run against the published update corpus.

This is where the two halves of the contract meet. ``scripts/generate-update-contract.py``
declares each case's verdict by hand and derives its bytes; this module runs
``omnivia_core_client``'s own implementation over those bytes and asserts the
verdict it reaches is the declared one. A disagreement is a real failure in one
of the two, which is the only arrangement in which either is worth anything: a
corpus whose expectations were read out of the implementation would record its
bugs and prove nothing.

Three corpora, three subjects:

* ``envelopes`` -- the fetched channel document. Judged as the whole admission a
  consumer performs: whether the bytes may be believed, then whether what they
  recommend is a shape this installation can act on. Both halves answer with a
  code from the published vocabulary, which is why the case's ``expected.refusal``
  is compared against a code and never against prose.
* ``states`` -- the operation journal and its phase machine.
* ``archives`` -- the candidate bundle. Each case describes an archive by its
  members rather than carrying one, so this builds the ZIP and runs the real
  extractor against a real staging directory.

The canonical tree is read directly rather than through the packaged resource
accessors, because the force-included copy exists only once a wheel is built.
``tests/update_contract/test_update_resources.py`` covers the accessors, and
``scripts/check-package-builds.sh`` reads the corpus out of a real wheel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client.update_trust import (
    TrustedUpdateKey,
    UpdateTrustPolicy,
    decode_base64url,
    signing_input,
)
from omnivia_core_client.updates import UpdateCheckError, validate_channel_document
from omnivia_core_client.updates_apply import (
    INITIAL_PHASE,
    LEGAL_TRANSITIONS,
    TERMINAL_PHASES,
    OperationRecord,
    UpdateApplyError,
    _extract_safely,
    _verify_inventory,
    read_operation_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ROOT = REPO_ROOT / "contracts" / "update" / "v1"
SCHEMA_PATH = CANONICAL_ROOT / "schemas" / "core-update-v1.schema.json"
FIXTURES_ROOT = CANONICAL_ROOT / "fixtures"

SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _cases(category: str) -> list[pytest.param]:
    """Every case in one category, as pytest parameters named by case id."""
    directory = FIXTURES_ROOT / category
    return [
        pytest.param(path, id=path.stem) for path in sorted(directory.glob("*.json"))
    ]


def _case(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    # The filename is part of the case's identity: a case renamed on disk but not
    # inside stops being findable by the name a failure reports.
    assert document["case_id"] == path.stem, path
    assert document["case_version"] == "1.0", path
    return document


ENVELOPE_CASES = _cases("envelopes")
STATE_CASES = _cases("states")
ARCHIVE_CASES = _cases("archives")


# --------------------------------------------------------------------------
# The corpus is not empty and its vocabularies are the schema's
# --------------------------------------------------------------------------


def test_each_corpus_carries_cases() -> None:
    """A corpus that quietly emptied would make every parametrized test vacuous."""
    assert len(ENVELOPE_CASES) >= 20
    assert len(STATE_CASES) >= 8
    assert len(ARCHIVE_CASES) >= 12


@pytest.mark.parametrize(
    ("category", "cases", "kind"),
    [
        ("envelopes", ENVELOPE_CASES, "envelope"),
        ("states", STATE_CASES, "state"),
        ("archives", ARCHIVE_CASES, "archive"),
    ],
)
def test_every_case_declares_its_own_subject(
    category: str, cases: list[pytest.param], kind: str
) -> None:
    """The directory a case is in and the kind it declares are one fact."""
    for parameter in cases:
        document = _case(parameter.values[0])
        assert document["case_kind"] == kind, (category, document["case_id"])


def test_every_declared_refusal_is_in_the_published_vocabulary() -> None:
    """A case expecting a code the schema does not publish is a case nobody can
    implement against: a consumer branching on the published enum would never see
    it."""
    envelope_codes = set(SCHEMA["$defs"]["envelopeRefusal"]["enum"])
    archive_codes = set(SCHEMA["$defs"]["archiveRefusal"]["enum"])
    for parameter in ENVELOPE_CASES:
        document = _case(parameter.values[0])
        refusal = document["expected"]["refusal"]
        assert refusal is None or refusal in envelope_codes, document["case_id"]
    for parameter in ARCHIVE_CASES:
        document = _case(parameter.values[0])
        refusal = document["expected"]["refusal"]
        assert refusal is None or refusal in archive_codes, document["case_id"]


def test_the_transition_table_the_schema_requires_is_the_one_the_worker_uses() -> None:
    """One table, published and enforced.

    The schema names the eight phases it requires a table to cover; the worker
    holds the table. If they drift, a consumer reading the contract reasons about
    a machine Core does not run.
    """
    published = set(SCHEMA["$defs"]["updatePhase"]["enum"])
    assert published == set(LEGAL_TRANSITIONS)
    assert set(SCHEMA["$defs"]["updateTransitionTable"]["properties"]["transitions"][
        "required"
    ]) == published
    assert INITIAL_PHASE in published
    assert TERMINAL_PHASES < published
    for phase in TERMINAL_PHASES:
        assert LEGAL_TRANSITIONS[phase] == frozenset(), phase
    # Every phase that is not terminal must be able to reach a terminal one, or
    # an operation could enter a phase it can never leave.
    for phase, successors in LEGAL_TRANSITIONS.items():
        if phase not in TERMINAL_PHASES:
            assert successors, phase


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------


def _policy(declared: dict[str, Any]) -> UpdateTrustPolicy:
    floor = declared.get("installed") or {}
    return UpdateTrustPolicy(
        trusted_keys=tuple(
            TrustedUpdateKey.from_document(key) for key in declared["trusted_keys"]
        ),
        expected_product_id=declared["expected_product_id"],
        expected_channel=declared["expected_channel"],
        accept_unsigned_v1=declared["accept_unsigned_v1"],
        minimum_sequence=declared.get("minimum_sequence"),
        version_floor=tuple(sorted(floor.items())),
    )


@pytest.mark.parametrize("path", ENVELOPE_CASES)
def test_the_envelope_corpus_agrees_with_the_reference_implementation(
    path: Path,
) -> None:
    """One channel document, one policy, one verdict."""
    case = _case(path)
    declared = case["policy"]
    expected = case["expected"]
    try:
        release = validate_channel_document(
            case["document_text"],
            policy=_policy(declared),
            verified_at=declared["verified_at"],
        )
    except UpdateCheckError as failure:
        assert expected["outcome"] == "refused", (case["case_id"], failure.reason)
        assert failure.refusal == expected["refusal"], case["case_id"]
        # Every refusal is one of the six honest statuses and never an escape.
        assert failure.status in {"check_failed", "unsupported_install"}
        return
    assert expected["outcome"] == "accepted", case["case_id"]
    assert expected["refusal"] is None, case["case_id"]
    if expected["release"] is None:
        assert release is None, case["case_id"]
        return
    assert release is not None, case["case_id"]
    assert release.version == expected["release"]["version"]
    assert release.release_url == expected["release"]["release_url"]
    assert release.packages == expected["release"]["packages"]
    assert release.bundle_sha256 == expected["release"]["bundle_sha256"]


def test_the_corpus_covers_every_published_envelope_refusal_it_can_reach() -> None:
    """A published code with no case is a rule this repository does not test.

    Two are deliberately unreachable from a corpus of documents and are named
    here rather than silently missing: ``channel_verifier_unavailable`` is about
    the *consumer's* installation rather than the document, and
    ``channel_malformed`` covers a family the corpus samples rather than
    enumerates.
    """
    published = set(SCHEMA["$defs"]["envelopeRefusal"]["enum"])
    covered = {
        _case(parameter.values[0])["expected"]["refusal"]
        for parameter in ENVELOPE_CASES
    } - {None}
    unreachable = {"channel_verifier_unavailable"}
    assert published - covered == unreachable, sorted(published - covered - unreachable)


# --------------------------------------------------------------------------
# Journals and transitions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", STATE_CASES)
def test_the_journal_corpus_agrees_with_the_reference_implementation(
    path: Path, tmp_path: Path
) -> None:
    """One record on disk, read and then advanced the way the worker does."""
    case = _case(path)
    expected = case["expected"]
    (tmp_path / "update-operation.json").write_text(
        case["record_text"], encoding="utf-8"
    )
    document = read_operation_record(tmp_path)

    if expected["outcome"] == "record_corrupt":
        assert document == {"corrupt": True}, case["case_id"]
        return

    assert document is not None and "corrupt" not in document, case["case_id"]
    if expected["outcome"] == "record_admitted":
        assert document["phase"] == expected["phase"], case["case_id"]
        return

    record = OperationRecord(
        operation_id=document["operation_id"],
        installation_ref=document["installation_ref"],
        approved_version=document["approved_release_ref"]["version"],
        approved_bundle_sha256=document["approved_release_ref"]["bundle_sha256"],
        started_at=document["started_at"],
        phase=document["phase"],
    )
    proposed = case["proposed_phase"]
    if expected["outcome"] == "transition_legal":
        record.advance(proposed, tmp_path)
        assert read_operation_record(tmp_path)["phase"] == proposed  # type: ignore[index]
        return
    assert expected["outcome"] == "transition_illegal", case["case_id"]
    with pytest.raises(UpdateApplyError) as refused:
        record.advance(proposed, tmp_path)
    assert refused.value.code == "illegal_transition", case["case_id"]
    # The refused transition is not written: a journal that records an
    # impossible move is worse than one that refuses to.
    assert read_operation_record(tmp_path)["phase"] == document["phase"]  # type: ignore[index]


# --------------------------------------------------------------------------
# Candidate bundles
# --------------------------------------------------------------------------


def _build_archive(case: dict[str, Any], destination: Path) -> Path:
    """Build the ZIP one archive case describes, member by member, in order."""
    path = destination / "candidate.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for member in case["members"]:
            name = member["name"]
            # A member with no `unix_mode` states no POSIX attributes at all,
            # which is a shape a real archive takes and a distinct case from a
            # member that states a mode.
            declared = member.get("unix_mode")
            if member["kind"] == "directory":
                info = zipfile.ZipInfo(name if name.endswith("/") else f"{name}/")
                if declared is not None:
                    info.external_attr = (stat.S_IFDIR | int(declared, 8)) << 16
                info.external_attr |= 0x10
                archive.writestr(info, b"")
                continue
            content = base64.b64decode(member.get("content_base64", ""))
            info = zipfile.ZipInfo(name)
            if declared is not None:
                kind = stat.S_IFLNK if member["kind"] == "symlink" else stat.S_IFREG
                info.external_attr = (kind | int(declared, 8)) << 16
            archive.writestr(info, content)
    return path


def _refuse_or_extract(case: dict[str, Any], root: Path) -> str | None:
    """Run the real extractor and inventory check; return the refusal code."""
    monkeypatched_bounds = case["bounds"]
    archive_path = _build_archive(case, root)
    unpacked = root / "bundle"
    import omnivia_core_client.updates_apply as apply_module

    original_members = apply_module.MAX_BUNDLE_MEMBERS
    original_bytes = apply_module.MAX_EXPANDED_BYTES
    apply_module.MAX_BUNDLE_MEMBERS = monkeypatched_bounds["max_members"]
    apply_module.MAX_EXPANDED_BYTES = monkeypatched_bounds["max_expanded_bytes"]
    try:
        with zipfile.ZipFile(archive_path) as archive:
            _extract_safely(archive, unpacked)
        _verify_inventory(
            unpacked, expected_packages=dict(case["approved"]["packages"])
        )
    except UpdateApplyError as failure:
        return failure.code
    finally:
        apply_module.MAX_BUNDLE_MEMBERS = original_members
        apply_module.MAX_EXPANDED_BYTES = original_bytes
    return None


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
@pytest.mark.parametrize("path", ARCHIVE_CASES)
def test_the_archive_corpus_agrees_with_the_reference_implementation(
    path: Path, tmp_path: Path
) -> None:
    """One candidate bundle, unpacked for real into a real staging directory."""
    case = _case(path)
    expected = case["expected"]
    refusal = _refuse_or_extract(case, tmp_path)
    if expected["outcome"] == "refused":
        assert refusal == expected["refusal"], case["case_id"]
        return
    assert refusal is None, (case["case_id"], refusal)
    assert expected["refusal"] is None, case["case_id"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@pytest.mark.parametrize("path", [p for p in ARCHIVE_CASES if p.id == "archive-baseline"])
def test_the_staged_tree_is_reachable_by_its_owner_alone(
    path: Path, tmp_path: Path
) -> None:
    """Owner-only staging, asserted on the tree rather than on the call.

    The wheels pip is about to install sit here between verification and
    installation. A group- or world-writable staging directory is a window in
    which a verified wheel can be replaced by an unverified one, and the digest
    check has already happened by then.
    """
    case = _case(path)
    assert _refuse_or_extract(case, tmp_path) is None
    unpacked = tmp_path / "bundle"
    for entry in [unpacked, *unpacked.rglob("*")]:
        mode = entry.stat().st_mode
        assert mode & 0o077 == 0, f"{entry} is reachable by another principal"
        assert entry.stat().st_uid == os.geteuid(), entry


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_refused_member_leaves_nothing_of_itself_behind(tmp_path: Path) -> None:
    """The refusal ends the extraction; the member that caused it is not on disk.

    ``extractall`` would have rewritten a traversing name into a harmless one and
    unpacked it, which is the behaviour this extractor exists not to have: an
    archive that tried to escape must not become an archive that quietly got a
    different layout than its own inventory describes.
    """
    case = _case(FIXTURES_ROOT / "archives" / "archive-parent-traversal.json")
    assert _refuse_or_extract(case, tmp_path) == "archive_traversal_member"
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------


VECTORS: dict[str, Any] = json.loads(
    (FIXTURES_ROOT / "vectors" / "canonicalisation-and-signatures.json").read_text(
        encoding="utf-8"
    )
)


def test_the_canonicalisation_vectors_hold() -> None:
    """Core's canonical JSON, over raw text so a duplicate name survives the read."""
    from omnivia_core.contracts.v1 import ContractSemanticError
    from omnivia_core.contracts.v1.canonical_json import (
        canonical_bytes,
        parse_json_document,
    )

    assert VECTORS["canonicalisation"]
    for vector in VECTORS["canonicalisation"]:
        if not vector["accepted"]:
            with pytest.raises(ContractSemanticError):
                canonical_bytes(parse_json_document(vector["json"]))
            continue
        canonical = canonical_bytes(parse_json_document(vector["json"]))
        assert canonical.decode("utf-8") == vector["canonical"], vector["name"]
        assert hashlib.sha256(canonical).hexdigest() == vector["sha256"], vector["name"]


def test_the_base64url_vectors_hold() -> None:
    """One spelling per byte string, in both directions."""
    assert VECTORS["base64url"]
    for vector in VECTORS["base64url"]:
        if not vector["accepted"]:
            with pytest.raises(ValueError):
                decode_base64url(vector["text"], expected_bytes=vector["expected_bytes"])
            continue
        decoded = decode_base64url(
            vector["text"], expected_bytes=vector["expected_bytes"]
        )
        assert decoded.hex() == vector["decoded_hex"], vector["name"]


def test_the_signing_input_vectors_hold() -> None:
    """The exact bytes a signature covers, including the domain and the product."""
    assert VECTORS["signature_domain"]
    for vector in VECTORS["signing_input"]:
        body = json.loads(vector["envelope_json"])
        digest = hashlib.sha256(
            signing_input(body, product_id=vector["product_id"])
        ).hexdigest()
        assert digest == vector["signing_input_sha256"], vector["name"]


def test_the_signing_input_separates_two_products_that_share_a_key() -> None:
    """The point of the domain prefix, stated as an inequality.

    One body, one key, two products: the bytes signed differ, so a signature over
    one is not a signature over the other. The corpus has the pair of digests;
    this is the sentence they mean.
    """
    by_product = {
        vector["product_id"]: vector["signing_input_sha256"]
        for vector in VECTORS["signing_input"]
        if json.loads(vector["envelope_json"])["release"] is not None
    }
    assert len(by_product) >= 2
    assert len(set(by_product.values())) == len(by_product)


def test_the_signature_vectors_hold() -> None:
    """Ed25519 over the signing input, including the three ways it must fail."""
    cryptography = pytest.importorskip("cryptography")
    assert cryptography is not None
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    assert VECTORS["signatures"]
    for vector in VECTORS["signatures"]:
        key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(vector["public_key"], expected_bytes=32)
        )
        message = signing_input(
            json.loads(vector["envelope_json"]), product_id=vector["product_id"]
        )
        signature = decode_base64url(vector["signature"], expected_bytes=64)
        if vector["verifies"]:
            key.verify(signature, message)
            continue
        with pytest.raises(InvalidSignature):
            key.verify(signature, message)
