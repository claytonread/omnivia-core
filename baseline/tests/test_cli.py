"""Tests for the baseline command line entry point."""

from __future__ import annotations

import pytest

from baseline.cli import CHECKS, main


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
