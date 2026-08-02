"""The scalar pattern compatibility classifier (A2.7).

A canonical `pattern` *is* the wire language, so narrowing one silently breaks
producers that conformed yesterday. The gate therefore defaults every pattern
change to major and manual, and grants exactly one exception: appending the
terminal end-of-input guard to an already-anchored pattern, which removes only
values this contract's own semantics and every ECMAScript client already
refused.

These tests exercise the classifier against synthetic changes, because the
interesting cases are the ones the accepted contract state deliberately does not
contain.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKER = REPO_ROOT / "scripts" / "check-application-contracts.py"


def _checker() -> Any:
    spec = importlib.util.spec_from_file_location("a27_checker", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _checker()
GUARD = checker.TERMINAL_GUARD
PARITY = checker.CLASSIFICATION_PARITY
MAJOR = checker.CLASSIFICATION_MAJOR

#: A small, explicit probe population. The classifier uses the frozen corpus;
#: here the values are named so each assertion says what it depends on.
VALUES = ["rec-1", "rec-1\n", "rec-1\r", "rec-1 ", "REC-1", "rec 1", "", "rec-1x"]


def _ecmascript(patterns: list[str]) -> dict[str, list[bool]] | None:
    evaluated = checker._ecmascript_accepts(patterns, VALUES)
    return dict(zip(patterns, evaluated, strict=True)) if evaluated is not None else None


def _classify(old_pattern: str, new_pattern: str, **new_keywords: Any) -> tuple[str, str | None]:
    old = {"pattern": old_pattern, "keywords": {"type": "string"}}
    new = {"pattern": new_pattern, "type": "string", **new_keywords}
    # The checker is loaded dynamically, so its return type is opaque here;
    # narrow it once rather than letting `Any` leak into every assertion below.
    classification, reason = cast(
        "tuple[str, str | None]",
        checker._classify("probe", old, new, VALUES, _ecmascript([old_pattern, new_pattern])),
    )
    return classification, reason


requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")


# --------------------------------------------------------------------------
# The exception, and its exact limits
# --------------------------------------------------------------------------


@requires_node
def test_guarding_an_anchored_pattern_is_a_parity_correction() -> None:
    """The only change this gate may wave through."""
    classification, reason = _classify("^[a-z-]+[0-9]$", f"^[a-z-]+[0-9]${GUARD}")
    assert classification == PARITY, reason


@requires_node
def test_guarding_an_unanchored_pattern_is_major() -> None:
    """The condition that carries the argument.

    Appending the guard to a pattern with no terminal anchor genuinely shrinks
    the language in every runtime -- ECMAScript included -- so it is a real
    change, not a parity correction, even though the edit looks identical.
    """
    classification, reason = _classify("^[a-z-]+", f"^[a-z-]+{GUARD}")
    assert classification == MAJOR
    assert "ECMAScript" in (reason or "")


@requires_node
def test_any_other_pattern_change_is_major() -> None:
    classification, reason = _classify("^[a-z-]+[0-9]$", "^[A-Za-z-]+[0-9]$")
    assert classification == MAJOR
    assert "more than the terminal guard" in (reason or "")


@requires_node
def test_a_guard_plus_another_keyword_change_is_major() -> None:
    """The exception requires everything else to be untouched.

    A tightened bound riding along with a parity correction would be published
    as a patch, which is how a breaking change reaches clients labelled safe.
    """
    classification, reason = _classify(
        "^[a-z-]+[0-9]$", f"^[a-z-]+[0-9]${GUARD}", maxLength=8
    )
    assert classification == MAJOR
    assert "another schema keyword" in (reason or "")


def test_an_unevaluable_change_is_indeterminate_not_optimistic() -> None:
    """Indeterminate must fail, never fall through to the exception.

    A parity claim that cannot be checked against the other runtime is not a
    parity claim, so an absent ECMAScript evaluation is refused rather than
    assumed to agree.
    """
    old = {"pattern": "^[a-z-]+[0-9]$", "keywords": {"type": "string"}}
    new = {"pattern": f"^[a-z-]+[0-9]${GUARD}", "type": "string"}
    classification, reason = checker._classify("probe", old, new, VALUES, None)
    assert classification == "indeterminate"
    assert "ECMAScript" in (reason or "")
    assert classification not in {PARITY, checker.CLASSIFICATION_UNCHANGED}


# --------------------------------------------------------------------------
# The gate as wired, against the real accepted state
# --------------------------------------------------------------------------


def test_the_baseline_records_the_accepted_checkpoint() -> None:
    document = json.loads(checker.PATTERN_BASELINE.read_text(encoding="utf-8"))
    assert document["format"] == checker.PATTERN_BASELINE_FORMAT
    assert document["accepted_checkpoint"]
    assert len(document["definitions"]) == 56


@requires_node
def test_every_definition_classifies_as_unchanged() -> None:
    """The rollover's own postcondition: nothing is left to classify.

    The baseline now records the accepted checkpoint that already carries the
    guarded patterns, so the recorded definitions and the current schemas are
    the same 56 definitions. Every one must classify `unchanged` -- and, just as
    importantly, *nothing* may classify parity, major or indeterminate: a
    surviving parity verdict would mean the baseline was not actually rolled
    forward, and either of the other two would mean a pattern moved after the
    checkpoint this baseline claims to be.
    """
    document = json.loads(checker.PATTERN_BASELINE.read_text(encoding="utf-8"))
    recorded = document["definitions"]
    current = checker._current_patterned_nodes()
    values = checker._corpus_probe_values()
    patterns = sorted(
        {entry["pattern"] for entry in recorded.values()}
        | {node["pattern"] for node in current.values()}
    )
    evaluated = checker._ecmascript_accepts(patterns, values)
    assert evaluated is not None
    ecmascript = dict(zip(patterns, evaluated, strict=True))

    tally: dict[str, int] = {}
    for name in sorted(set(recorded) & set(current)):
        classification, _ = checker._classify(
            name, recorded[name], current[name], values, ecmascript
        )
        tally[classification] = tally.get(classification, 0) + 1
    assert tally == {checker.CLASSIFICATION_UNCHANGED: 56}, tally
    # Stated separately from the tally above so a failure names the outcome that
    # appeared rather than only that the totals differed.
    assert not {PARITY, MAJOR, "indeterminate"} & set(tally), tally


@requires_node
def test_the_wired_gate_reports_nothing_for_the_accepted_checkpoint() -> None:
    assert checker.check_scalar_pattern_compatibility() == []


# --------------------------------------------------------------------------
# The baseline is a claim about a commit, and is checked against it
# --------------------------------------------------------------------------


def _findings_for(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[str]:
    """Run the gate against a baseline written to disk, as it reads it in CI."""
    forged = tmp_path / "pattern-baseline.json"
    forged.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(checker, "PATTERN_BASELINE", forged)
    return cast("list[str]", checker.check_pattern_baseline_is_the_accepted_checkpoint())


def test_the_baseline_matches_the_accepted_checkpoint() -> None:
    assert checker.check_pattern_baseline_is_the_accepted_checkpoint() == []


def _git(repository: Path, *arguments: str) -> str:
    finished = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return finished.stdout.strip()


def _self_authored_repository(tmp_path: Path, *, name_head: bool) -> tuple[Path, Path]:
    """A candidate that writes its own baseline and then points the gate at it.

    Two spellings of one attack. With ``name_head=True`` the baseline says
    ``HEAD`` and everything lands in a single commit. Otherwise the schemas are
    committed first, that commit's full 40-character id is captured into the
    baseline, and the baseline is committed second -- which satisfies any
    ancestry test while remaining entirely self-authored.
    """
    repository = tmp_path / ("head" if name_head else "two-commit")
    shutil.copytree(REPO_ROOT / "contracts", repository / "contracts")
    repository.mkdir(exist_ok=True)
    _git(repository.parent, "init", "-q", str(repository))
    _git(repository, "config", "user.email", "candidate@example.invalid")
    _git(repository, "config", "user.name", "candidate")

    baseline_path = repository / "contracts" / "application" / "v1" / "pattern-baseline.json"
    document = json.loads(baseline_path.read_text(encoding="utf-8"))
    document["definitions"] = {
        name: {
            "pattern": node["pattern"],
            "keywords": {k: v for k, v in node.items() if k != "pattern"},
        }
        for path in sorted((repository / "contracts/application/v1/schemas").glob("*.schema.json"))
        for name, node in checker._patterned_nodes(
            json.loads(path.read_text(encoding="utf-8")), path.stem
        ).items()
    }

    if name_head:
        document["accepted_checkpoint"] = "HEAD"
        baseline_path.write_text(json.dumps(document), encoding="utf-8")
        _git(repository, "add", "-A")
        _git(repository, "commit", "-qm", "candidate schemas and its own baseline")
    else:
        _git(repository, "add", "-A")
        _git(repository, "commit", "-qm", "candidate schemas")
        document["accepted_checkpoint"] = _git(repository, "rev-parse", "HEAD")
        baseline_path.write_text(json.dumps(document), encoding="utf-8")
        _git(repository, "add", "-A")
        _git(repository, "commit", "-qm", "baseline naming the previous commit")
    return repository, baseline_path


@pytest.mark.parametrize("name_head", [True, False], ids=["symbolic-head", "two-commit-full-hash"])
def test_a_self_authored_checkpoint_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name_head: bool
) -> None:
    """A candidate may not certify itself, however it spells the checkpoint.

    Resolving to a commit is not acceptance, and neither is being an ancestor:
    every earlier commit on a branch is an ancestor of its tip. Both properties
    are things the candidate can arrange for itself, so acceptance has to be
    asserted from outside the change under review.
    """
    repository, baseline_path = _self_authored_repository(tmp_path, name_head=name_head)
    monkeypatch.setattr(checker, "REPO_ROOT", repository)
    monkeypatch.setattr(checker, "SCHEMA_DIR", repository / "contracts/application/v1/schemas")
    monkeypatch.setattr(checker, "PATTERN_BASELINE", baseline_path)
    findings = checker.check_pattern_baseline_is_the_accepted_checkpoint()
    assert findings, "a self-authored checkpoint was accepted"
    expected = (
        "must be a full 40-character object id"
        if name_head
        else "is not the accepted contract checkpoint"
    )
    assert any(expected in finding for finding in findings), findings


def test_candidate_modified_fallback_is_rejected_under_the_external_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full attack: a candidate rewrites *both* halves of the self-certification
    described on :data:`ACCEPTED_CONTRACT_CHECKPOINT` -- the in-tree fallback
    constant, moved to name its own two-commit, self-authored baseline (the same
    construction :func:`_self_authored_repository` builds for the ancestry attack
    above), so that the local-development path would accept it outright.

    It must still be refused, because hosted acceptance never consults that
    constant: :func:`_resolve_accepted_checkpoint` reads the anchor only from
    :data:`ACCEPTED_CHECKPOINT_ENV`, which here is supplied the real accepted A2.7.1
    checkpoint from outside the candidate's tree. The candidate's forged checkpoint
    disagreeing with that external anchor is exactly the check that matters --
    without it, a candidate who reads this checker's own source could defeat the
    ancestry test above by patching the one constant it still trusted.
    """
    real_accepted_checkpoint = checker.ACCEPTED_CONTRACT_CHECKPOINT
    repository, baseline_path = _self_authored_repository(tmp_path, name_head=False)
    forged_checkpoint = json.loads(baseline_path.read_text(encoding="utf-8"))["accepted_checkpoint"]
    assert forged_checkpoint != real_accepted_checkpoint

    monkeypatch.setattr(checker, "REPO_ROOT", repository)
    monkeypatch.setattr(checker, "SCHEMA_DIR", repository / "contracts/application/v1/schemas")
    monkeypatch.setattr(checker, "PATTERN_BASELINE", baseline_path)
    # The candidate's half of the attack: the in-tree fallback constant now agrees
    # with its own self-authored, matching two-commit baseline.
    monkeypatch.setattr(checker, "ACCEPTED_CONTRACT_CHECKPOINT", forged_checkpoint)
    # The reviewer's half: repository-external configuration still names the real
    # accepted checkpoint, unreachable from inside the candidate's tree.
    monkeypatch.setenv(checker.ACCEPTED_CHECKPOINT_ENV, real_accepted_checkpoint)

    findings = checker.check_pattern_baseline_is_the_accepted_checkpoint()
    assert findings, "a candidate-modified fallback was accepted under the external anchor"
    assert any("is not the accepted contract checkpoint" in f for f in findings), findings


@pytest.mark.parametrize(
    "checkpoint",
    [
        "HEAD",
        "main",
        "v1.0.0",
        checker.ACCEPTED_CONTRACT_CHECKPOINT[:7],
        checker.ACCEPTED_CONTRACT_CHECKPOINT.upper(),
        f" {checker.ACCEPTED_CONTRACT_CHECKPOINT} ",
        f"{checker.ACCEPTED_CONTRACT_CHECKPOINT}^",
        "HEAD~1",
    ],
    ids=["head", "branch", "tag", "abbreviated", "uppercase", "padded", "expression", "relative"],
)
def test_only_a_canonical_object_id_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, checkpoint: str
) -> None:
    """Every alternate spelling is refused before it is resolved."""
    document = json.loads(checker.PATTERN_BASELINE.read_text(encoding="utf-8"))
    document["accepted_checkpoint"] = checkpoint
    findings = _findings_for(document, monkeypatch, tmp_path)
    assert any("full 40-character object id" in finding for finding in findings), findings


def test_ci_may_supply_the_accepted_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reusable half: the anchor can come from outside the repository.

    A protected base or an acceptance attestation supplies it in CI; the
    reviewed constant applies when nothing does.
    """
    monkeypatch.setenv(checker.ACCEPTED_CHECKPOINT_ENV, "0" * 40)
    findings = checker.check_pattern_baseline_is_the_accepted_checkpoint()
    assert any("is not the accepted contract checkpoint" in f for f in findings), findings


def test_external_accepted_anchor_accepts_the_real_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive case for the reusable half above: supplying the *correct*
    externally-configured anchor -- the accepted A2.7.1 checkpoint -- must pass, not
    merely fail differently. Without this, a broken wiring that always rejects
    every supplied checkpoint would still make
    `test_ci_may_supply_the_accepted_checkpoint` and the malformed/hosted tests
    pass, and only ever be caught by accident.
    """
    monkeypatch.setenv(checker.ACCEPTED_CHECKPOINT_ENV, checker.ACCEPTED_CONTRACT_CHECKPOINT)
    assert checker.check_pattern_baseline_is_the_accepted_checkpoint() == []


def test_a_malformed_supplied_checkpoint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(checker.ACCEPTED_CHECKPOINT_ENV, "HEAD")
    findings = checker.check_pattern_baseline_is_the_accepted_checkpoint()
    assert any("not a full 40-character object id" in f for f in findings), findings


# --------------------------------------------------------------------------
# Hosted execution must fail closed on a missing, empty or malformed anchor
# --------------------------------------------------------------------------
#
# These exercise `_resolve_accepted_checkpoint` directly, against a synthetic
# `environ` mapping rather than `monkeypatch.setenv`/`os.environ`: the function
# already accepts an explicit mapping for exactly this reason, and driving it
# that way lets each test state the hosted markers it depends on instead of
# inheriting whatever `GITHUB_ACTIONS`/`CI` happen to be set to in the process
# actually running this suite.


def test_hosted_missing_anchor_fails_closed() -> None:
    """An unset anchor on a hosted run must refuse, not fall back to the
    in-tree constant -- that constant is what a self-authored checkpoint would
    move to certify itself."""
    expected, findings = checker._resolve_accepted_checkpoint({"GITHUB_ACTIONS": "true"})
    assert expected is None
    assert any("hosted acceptance must be supplied" in f for f in findings), findings
    assert any("unset" in f for f in findings), findings


def test_hosted_empty_anchor_fails_closed() -> None:
    expected, findings = checker._resolve_accepted_checkpoint(
        {"GITHUB_ACTIONS": "true", checker.ACCEPTED_CHECKPOINT_ENV: ""}
    )
    assert expected is None
    assert any("hosted acceptance must be supplied" in f for f in findings), findings
    assert any("empty" in f for f in findings), findings


def test_hosted_malformed_anchor_fails_closed() -> None:
    expected, findings = checker._resolve_accepted_checkpoint(
        {"GITHUB_ACTIONS": "true", checker.ACCEPTED_CHECKPOINT_ENV: "HEAD"}
    )
    assert expected is None
    assert any("not a full 40-character object id" in f for f in findings), findings


def test_hosted_ci_marker_alone_also_triggers_the_missing_anchor_refusal() -> None:
    """`CI` is the other marker `_is_hosted_execution` honours; the refusal must
    not depend on which of the two hosted markers a runner happens to set."""
    expected, findings = checker._resolve_accepted_checkpoint({"CI": "true"})
    assert expected is None
    assert any("hosted acceptance must be supplied" in f for f in findings), findings


def test_unavailable_history_produces_a_finding_rather_than_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must answer "not verified", not raise.

    An environment without a usable git raised `FileNotFoundError` straight out
    of the checker. That is not a false certification -- CI still fails -- but it
    replaces a classifiable verdict with a traceback, and the requirement is a
    deterministic finding for unavailable history.
    """

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(subprocess, "run", unavailable)
    findings = checker.check_pattern_baseline_is_the_accepted_checkpoint()
    assert any("is not a commit in this repository" in f for f in findings), findings


def test_an_unevaluable_classifier_run_reports_indeterminate_not_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rule for the classifier's ECMAScript evidence.

    Driven from a synthetic change rather than from the real definitions: the
    baseline now records the accepted checkpoint the schemas already sit at, so
    every real definition classifies `unchanged` and never reaches the
    ECMAScript evidence at all. The rule under test is about a definition that
    *did* change, so one is introduced here -- an already-guarded pattern with a
    second guard appended, which is the shape that would otherwise be waved
    through as a parity correction. Without usable evidence it must be refused
    as indeterminate instead.
    """
    current = checker._current_patterned_nodes()
    name = min(current)
    current[name] = {**current[name], "pattern": current[name]["pattern"] + GUARD}
    monkeypatch.setattr(checker, "_current_patterned_nodes", lambda: current)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("node is not installed")

    monkeypatch.setattr(subprocess, "run", unavailable)
    findings = checker.check_scalar_pattern_compatibility()
    assert findings, "an unevaluable run reported nothing"
    assert all("indeterminate" in finding for finding in findings), findings


def test_an_accepted_checkpoint_absent_from_the_repository_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A commit nobody can read is not evidence, even when it is the anchor.

    Well-formed and equal to the supplied anchor, but not present here -- which
    is what a shallow clone looks like. It must fail rather than skip.
    """
    absent = "0" * 40
    monkeypatch.setenv(checker.ACCEPTED_CHECKPOINT_ENV, absent)
    document = json.loads(checker.PATTERN_BASELINE.read_text(encoding="utf-8"))
    document["accepted_checkpoint"] = absent
    findings = _findings_for(document, monkeypatch, tmp_path)
    assert any("is not a commit in this repository" in f for f in findings), findings


def test_a_rewritten_baseline_entry_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The blocker: a breaking change that rewrites the baseline to match itself.

    The classifier compares the schemas against a file in the same tree, so
    without this the commit that narrows a pattern can edit the baseline entry to
    agree, the comparison finds "no change", and a breaking change ships labelled
    compatible. Anchoring the baseline to an immutable commit removes the option:
    it can only move to a state that was already reviewed and committed.
    """
    document = json.loads(checker.PATTERN_BASELINE.read_text(encoding="utf-8"))
    name = min(document["definitions"])
    document["definitions"][name]["pattern"] = "^forged$"
    findings = _findings_for(document, monkeypatch, tmp_path)
    assert any("does not match the accepted checkpoint" in f for f in findings), findings


@pytest.mark.parametrize(
    ("checkpoint", "expected"),
    [
        ("0" * 40, "is not the accepted contract checkpoint"),
        ("", "must be a full 40-character object id"),
    ],
    ids=["unknown-commit", "absent"],
)
def test_an_unverifiable_checkpoint_fails_rather_than_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, checkpoint: str, expected: str
) -> None:
    """A claim about a commit nobody can read is not evidence."""
    document = json.loads(checker.PATTERN_BASELINE.read_text(encoding="utf-8"))
    document["accepted_checkpoint"] = checkpoint
    findings = _findings_for(document, monkeypatch, tmp_path)
    assert any(expected in finding for finding in findings), findings


def test_an_unrecorded_definition_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A newly published pattern is a contract decision, not a silent addition."""
    current = checker._current_patterned_nodes()
    current["invented.schema:$defs.Surprise"] = {"pattern": "^x$", "type": "string"}
    monkeypatch.setattr(checker, "_current_patterned_nodes", lambda: current)
    findings = checker.check_scalar_pattern_compatibility()
    assert any("is new and unrecorded" in finding for finding in findings), findings


def test_a_removed_definition_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    current = checker._current_patterned_nodes()
    dropped = min(current)
    del current[dropped]
    monkeypatch.setattr(checker, "_current_patterned_nodes", lambda: current)
    findings = checker.check_scalar_pattern_compatibility()
    assert any("was removed" in finding for finding in findings), findings
