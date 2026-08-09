"""Tests for the baseline command line entry point."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from baseline import ARTIFACT_ROOT_ENV, CORE_SRC, PACKAGE_ROOT, REPO_ROOT
from baseline import cli as baseline_cli
from baseline.cli import CHECKS, WRITE_ONLY_ENV, _promote_atomically, main


def test_verify_passes_on_a_clean_tree(capsys) -> None:
    exit_code = main(["verify"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.count("ok   ") == len(CHECKS)
    assert "FAIL" not in output


def test_verify_reports_every_check_by_name(capsys) -> None:
    main(["verify"])
    output = capsys.readouterr().out

    for name, _ in CHECKS:
        assert name in output


def test_verify_fails_loudly_when_a_check_reports_drift(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "baseline.cli.CHECKS",
        (("public exports", lambda: ["KnowledgeSpace moved to omnivia.contracts"]),),
    )

    exit_code = main(["verify"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "FAIL public exports" in output
    assert "KnowledgeSpace moved" in output


def test_list_gaps_prints_owner_and_closing_condition(capsys) -> None:
    exit_code = main(["list-gaps"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "GAP-001" in output
    assert "omnivia-platform" in output
    assert "closes:" in output


def test_verify_external_requires_a_known_surface() -> None:
    with pytest.raises(SystemExit):
        main(["verify-external", "--surface", "grpc", "--artifact", "missing.json"])


def test_verify_external_fails_on_a_missing_artifact(capsys, tmp_path) -> None:
    exit_code = main(
        ["verify-external", "--surface", "mcp_tools", "--artifact", str(tmp_path / "nope.json")]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "FAIL external capture for mcp_tools" in output


# --- transactional capture (R006-05) ---------------------------------------
#
# ``capture`` must be all-or-nothing: it may replace the accepted baseline only
# when a freshly generated candidate passes the complete verification, and it
# must leave the accepted baseline byte-for-byte unchanged on any failure. The
# tree currently carries transitional facade-move evidence, so a fresh capture
# is not a fixed point of ``verify`` and every candidate is red by construction.
# That is exactly the unsafe case, and it is what these tests exercise.


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _copy_accepted_baseline(destination: Path) -> Path:
    shutil.copytree(PACKAGE_ROOT / "inventories", destination / "inventories")
    shutil.copytree(PACKAGE_ROOT / "fixtures", destination / "fixtures")
    return destination


def _child_env(artifact_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env[ARTIFACT_ROOT_ENV] = str(artifact_root)
    env.pop(WRITE_ONLY_ENV, None)
    # ``ensure_core_importable`` prepends CORE_SRC itself, but naming it (and the
    # canonical root) keeps the child independent of how the parent was launched.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(CORE_SRC), str(REPO_ROOT / "src"), existing) if part
    )
    return env


def test_failed_candidate_leaves_accepted_baseline_unchanged(tmp_path) -> None:
    """The guarded safety property: a red candidate must not touch the accepted
    baseline. Mutate step 5 to promote unconditionally and this test goes red."""
    accepted = _copy_accepted_baseline(tmp_path / "accepted")
    before = _tree_hash(accepted)

    result = subprocess.run(
        [sys.executable, "-m", "baseline", "capture"],
        cwd=REPO_ROOT,
        env=_child_env(accepted),
        capture_output=True,
        text=True,
        check=False,
    )

    # The transitional facade evidence makes the candidate fail verification, so
    # capture must refuse rather than overwrite the valid accepted baseline.
    assert result.returncode == 1, result.stdout + result.stderr
    assert "byte-for-byte unchanged" in result.stdout
    assert _tree_hash(accepted) == before


def test_promote_atomically_replaces_added_and_removed_artifacts(tmp_path) -> None:
    """The promotion step moves candidate bytes over the accepted baseline and
    drops artifacts the candidate no longer has."""
    accepted = tmp_path / "accepted"
    candidate = tmp_path / "candidate"
    (accepted / "inventories").mkdir(parents=True)
    (accepted / "fixtures").mkdir(parents=True)
    (candidate / "inventories").mkdir(parents=True)
    (candidate / "fixtures").mkdir(parents=True)

    (accepted / "inventories" / "a.json").write_text("OLD", encoding="utf-8")
    (accepted / "inventories" / "stale.json").write_text("DROP", encoding="utf-8")
    (accepted / "fixtures" / "b.json").write_text("KEEP", encoding="utf-8")

    (candidate / "inventories" / "a.json").write_text("NEW", encoding="utf-8")
    (candidate / "inventories" / "added.json").write_text("ADDED", encoding="utf-8")
    (candidate / "fixtures" / "b.json").write_text("KEEP", encoding="utf-8")

    _promote_atomically(candidate, accepted)

    assert (accepted / "inventories" / "a.json").read_text(encoding="utf-8") == "NEW"
    assert (accepted / "inventories" / "added.json").read_text(encoding="utf-8") == "ADDED"
    assert (accepted / "fixtures" / "b.json").read_text(encoding="utf-8") == "KEEP"
    assert not (accepted / "inventories" / "stale.json").exists()
    # os.replace consumes the candidate files it moved.
    assert not (candidate / "inventories" / "a.json").exists()


def test_green_candidate_is_reviewed_and_atomically_promoted(tmp_path, monkeypatch, capsys) -> None:
    """A genuinely valid candidate is promoted end to end.

    Only the candidate-verification verdict is simulated green -- that is the one
    transitional facade guard the pending ``omnivia_core`` migration will make
    pass. Generation, the review diff, and the atomic promotion all run for real,
    against a redirected artifact root so the committed baseline is never touched.
    """
    accepted = _copy_accepted_baseline(tmp_path / "accepted")
    original = _tree_hash(accepted)
    monkeypatch.setattr(baseline_cli, "INVENTORY_DIR", accepted / "inventories")
    monkeypatch.setattr(baseline_cli, "FIXTURE_DIR", accepted / "fixtures")

    real_child = baseline_cli._run_baseline_child

    def fake_child(args, candidate_root, *, write_only=False):
        if args == ["verify"]:
            return subprocess.CompletedProcess(args, 0, stdout="All checks passed.\n", stderr="")
        return real_child(args, candidate_root, write_only=write_only)

    monkeypatch.setattr(baseline_cli, "_run_baseline_child", fake_child)

    exit_code = baseline_cli._capture_transactional()
    output = capsys.readouterr().out

    assert exit_code == 0, output
    assert "candidate diff" in output
    assert "Accepted baseline replaced with the verified candidate." in output
    # The promotion physically replaced the accepted artifacts with the candidate.
    assert _tree_hash(accepted) != original
