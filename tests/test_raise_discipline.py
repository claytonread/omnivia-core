"""Tests for the raise-outside-except discipline check.

Loads ``scripts/check-raise-discipline.py`` as a module (its filename is not a
valid Python identifier, so it cannot be imported normally).

The first test below is the one that matters: it *executes* both shapes and
reads the attributes, rather than asserting the rule in prose. If
``raise ... from None`` ever genuinely cleared ``__context__``, that test would
fail and this whole check would be revealed as unnecessary.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-raise-discipline.py"

# One `raise ... from None` inside an `except`, i.e. exactly one site.
LEAK = "try:\n    pass\nexcept ValueError:\n    raise RuntimeError('x') from None\n"

# The most recent lane's defect: `ipaddress.ip_address` refuses a
# caller-supplied host by quoting it, and `from None` leaves that ValueError --
# host and all -- on `error.__context__`.
IP_ADDRESS_DEFECT = """
import ipaddress


def _endpoint_host(host: str) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise DiscoveryError("endpoint host is malformed") from None
    return host
"""

# The repository's convention: flag inside the handler, raise after it exits.
IP_ADDRESS_REPAIRED = """
import ipaddress


def _endpoint_host(host: str) -> str:
    malformed = False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        malformed = True
    if malformed:
        raise DiscoveryError("endpoint host is malformed")
    return host
"""


def _load_discipline_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_raise_discipline", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses introspects sys.modules[cls.__module__], so the module must
    # be registered there before exec_module runs its class bodies.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


discipline = _load_discipline_module()

STUB = Path("stub.py")


def test_from_none_leaves_the_original_reachable_and_the_convention_does_not() -> None:
    """The premise, executed. This is what makes the rule worth enforcing."""
    secret = "postgresql://svc:hunter2@db.internal/workspace"

    with pytest.raises(RuntimeError) as suppressed:
        try:
            raise ValueError(f"cannot parse {secret}")
        except ValueError:
            raise RuntimeError("request is malformed") from None

    error = suppressed.value
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    # Suppressed for display, still attached -- and still quoting the secret.
    assert error.__context__ is not None
    assert secret in str(error.__context__)

    with pytest.raises(RuntimeError) as repaired:
        malformed = False
        try:
            raise ValueError(f"cannot parse {secret}")
        except ValueError:
            malformed = True
        if malformed:
            raise RuntimeError("request is malformed")

    assert repaired.value.__cause__ is None
    assert repaired.value.__context__ is None


def test_the_scoped_trees_are_clean() -> None:
    assert discipline.run_checks() == []


def test_the_historical_ip_address_defect_is_caught() -> None:
    sites = discipline.scan_source(IP_ADDRESS_DEFECT, STUB)

    assert len(sites) == 1
    assert sites[0].kind == "`raise ... from None` inside `except`"
    offending = IP_ADDRESS_DEFECT.splitlines()[sites[0].lineno - 1]
    assert offending.strip().endswith("from None")


def test_the_repository_convention_passes() -> None:
    assert discipline.scan_source(IP_ADDRESS_REPAIRED, STUB) == []


@pytest.mark.parametrize(
    ("statement", "flagged"),
    [
        ("raise RuntimeError('x') from None", True),
        ("raise RuntimeError('x')", True),
        ("raise", False),
        ("raise RuntimeError('x') from error", False),
    ],
)
def test_which_raise_shapes_are_flagged(statement: str, flagged: bool) -> None:
    source = f"try:\n    pass\nexcept ValueError as error:\n    {statement}\n"

    assert bool(discipline.scan_source(source, STUB)) is flagged


def test_a_raise_in_a_function_defined_inside_the_handler_is_not_flagged() -> None:
    source = (
        "try:\n"
        "    pass\n"
        "except ValueError:\n"
        "    def later() -> None:\n"
        "        raise RuntimeError('x') from None\n"
    )

    assert discipline.scan_source(source, STUB) == []


def test_a_raise_outside_any_handler_is_not_flagged() -> None:
    source = "try:\n    pass\nexcept ValueError:\n    pass\nraise RuntimeError('x')\n"

    assert discipline.scan_source(source, STUB) == []


#: A debt record invented for these tests, deliberately not the real one.
#:
#: The ratchet mechanism has to stay pinned whether or not the repo currently
#: carries any accepted site. Deriving the fixture from the live
#: ``ACCEPTED_SITES`` made the mechanism untestable the moment the debt was
#: paid off -- the tests below went from passing to erroring on an empty
#: mapping, which is precisely backwards: paying the debt should not retire the
#: guard that protects it. Whether the *real* record matches the *real* tree is
#: a different question, pinned by ``test_the_scoped_trees_are_clean``.
SYNTHETIC_ACCEPTED: dict[str, tuple[int, str]] = {
    f"{discipline.SCOPED_TREES[0]}/legacy_two.py": (2, "synthetic: two accepted"),
    f"{discipline.SCOPED_TREES[1]}/legacy_one.py": (1, "synthetic: one accepted"),
}


@pytest.fixture
def ratchet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tree carrying exactly ``SYNTHETIC_ACCEPTED``, and nothing else."""
    monkeypatch.setattr(discipline, "ACCEPTED_SITES", SYNTHETIC_ACCEPTED)
    for tree in discipline.SCOPED_TREES:
        (tmp_path / tree).mkdir(parents=True, exist_ok=True)
    for relative, (count, _reason) in SYNTHETIC_ACCEPTED.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEAK * count, encoding="utf-8")
    return tmp_path


def _first_accepted() -> tuple[str, int]:
    relative, (count, _reason) = next(iter(SYNTHETIC_ACCEPTED.items()))
    return relative, count


def test_the_ratchet_accepts_exactly_the_recorded_counts(ratchet_root: Path) -> None:
    assert discipline.run_checks(ratchet_root) == []


def test_one_more_site_in_an_accepted_file_fails(ratchet_root: Path) -> None:
    relative, count = _first_accepted()
    path = ratchet_root / relative
    path.write_text(LEAK * (count + 1), encoding="utf-8")

    findings = discipline.run_checks(ratchet_root)

    assert len(findings) == 1
    assert relative in findings[0]
    assert f"{count + 1} raise(s)" in findings[0]
    assert f"accepted {count}" in findings[0]


def test_a_repaired_site_forces_its_entry_down(ratchet_root: Path) -> None:
    relative, count = _first_accepted()
    path = ratchet_root / relative
    path.write_text(LEAK * (count - 1), encoding="utf-8")

    findings = discipline.run_checks(ratchet_root)

    assert len(findings) == 1
    assert "lower the entry to match" in findings[0]


def test_an_accepted_file_that_is_gone_makes_its_entry_stale(
    ratchet_root: Path,
) -> None:
    relative, _count = _first_accepted()
    (ratchet_root / relative).unlink()

    findings = discipline.run_checks(ratchet_root)

    assert len(findings) == 1
    assert "remove the entry" in findings[0]


def test_a_new_file_in_a_scoped_tree_has_no_budget(ratchet_root: Path) -> None:
    (ratchet_root / discipline.SCOPED_TREES[0] / "new_seam.py").write_text(
        LEAK, encoding="utf-8"
    )

    findings = discipline.run_checks(ratchet_root)

    assert len(findings) == 1
    assert "new_seam.py" in findings[0]
    assert "accepted 0" in findings[0]


def test_a_file_outside_the_scoped_trees_is_not_judged(ratchet_root: Path) -> None:
    """The stated boundary: unrelated code must not trip this rule."""
    elsewhere = ratchet_root / "src" / "omnivia_core" / "contracts" / "v1" / "thing.py"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_text(LEAK * 3, encoding="utf-8")

    assert discipline.run_checks(ratchet_root) == []


def test_a_missing_scoped_tree_is_reported(tmp_path: Path) -> None:
    findings = discipline.run_checks(tmp_path)

    assert [f for f in findings if "scoped tree is missing" in f]
