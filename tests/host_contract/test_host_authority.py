"""Governed-authority parity for the checked-in Host Contract v1 resources.

``contracts/host/v1/{schemas,fixtures}`` is a verbatim copy of the exact bytes
DOC-004 §AA incorporates: proposal commit
``2568f0049256b86fa58dc2387e8302aa4da94ae1``, content-set SHA-256
``31da9251944079ea1bc8db61ed0a393a05e0ce18602f31f6771def4ab243f0ac``, approval
``GOV-HOST-CONTRACT-APPROVAL-001``.

The approved digests live in ``scripts/generate-host-contract.py`` so the same
frozen record gates both the offline check and this permanent test. Editing a
canonical byte without a new exact-byte approval must fail here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate-host-contract.py"
CANONICAL_ROOT = REPO_ROOT / "contracts" / "host" / "v1"


def _load_generator_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_host_contract", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator_module()


def test_approved_authority_record_matches_doc_004_section_aa() -> None:
    assert generator.APPROVED_AUTHORITY == {
        "approvalId": "GOV-HOST-CONTRACT-APPROVAL-001",
        "proposalCommit": "2568f0049256b86fa58dc2387e8302aa4da94ae1",
        "proposalContentSetSha256": (
            "31da9251944079ea1bc8db61ed0a393a05e0ce18602f31f6771def4ab243f0ac"
        ),
        "proposalEntryCount": 31,
        "hostContractVersion": "1.0.0",
    }


def test_canonical_tree_holds_exactly_the_approved_resource_paths() -> None:
    present = sorted(
        path.relative_to(CANONICAL_ROOT).as_posix() for path in CANONICAL_ROOT.rglob("*") if path.is_file()
    )
    assert present == sorted(generator.APPROVED_RESOURCE_DIGESTS)


def test_every_canonical_resource_matches_its_approved_digest() -> None:
    for relative, expected in sorted(generator.APPROVED_RESOURCE_DIGESTS.items()):
        actual = hashlib.sha256((CANONICAL_ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def test_approved_resource_set_covers_the_schema_and_twenty_fixtures() -> None:
    relatives = sorted(generator.APPROVED_RESOURCE_DIGESTS)
    schemas = [name for name in relatives if name.startswith("schemas/")]
    fixtures = [name for name in relatives if name.startswith("fixtures/")]
    assert schemas == ["schemas/host-contract-v1.schema.json"]
    assert len(fixtures) == 20
    assert sorted({name.split("/")[1] for name in fixtures}) == [
        "degradation",
        "denial",
        "invalid",
        "migration",
        "valid",
    ]


def test_authority_check_reports_a_modified_canonical_byte(tmp_path: Path) -> None:
    """A tampered canonical file is a finding, not a silent pass."""
    staged = tmp_path / "v1"
    schemas = staged / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "host-contract-v1.schema.json").write_text("{}\n", encoding="utf-8")
    findings = generator.check_authority_digests(staged)
    assert any("host-contract-v1.schema.json" in finding for finding in findings)


def test_authority_check_passes_on_the_canonical_tree() -> None:
    assert generator.check_authority_digests(CANONICAL_ROOT) == []


def test_every_approved_resource_is_visible_to_source_control() -> None:
    """Existing on this disk is not the same as being tracked.

    ``.gitignore`` carries a broad ``*secret*`` rule, and one governed fixture is
    named ``environment-binding-inline-secret.json`` -- it is the approved
    *negative* case proving a binding carrying an inline secret is rejected, and
    it holds a placeholder, not a credential. Without an explicit narrow
    exception the approved bytes are silently untrackable: every check here
    passes on a working tree that has the file, and a fresh clone has nineteen
    fixtures instead of twenty and fails the authority gate.

    This asserts the whole approved set is git-visible, so that failure mode
    surfaces here rather than at the next clone.
    """
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--stdin",
            "--no-index",
        ],
        input="\n".join(
            f"contracts/host/v1/{relative}" for relative in sorted(generator.APPROVED_RESOURCE_DIGESTS)
        ),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert ignored.stdout.splitlines() == [], (
        "approved canonical resource(s) are excluded by .gitignore and could never be "
        f"committed: {ignored.stdout.split()}"
    )
