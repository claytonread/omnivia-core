"""Scalar anchor hardening: schema, semantic and ECMAScript agreement (A2.7).

Python's ``$`` matches at the end of the string *or* immediately before a
trailing newline, and JSON Schema ``pattern`` is a search rather than a full
match. A definition written as ``^[a-z-]+$`` therefore accepted ``"rec-1\\n"``
under Draft 2020-12 validation, while this contract's own semantic layer
(``re.fullmatch``) and every ECMAScript ``RegExp`` rejected it. The published
wire language never meant to admit a trailing line feed, so that gap was a
validator-parity defect rather than a language the contract intended.

Every terminal anchor now carries ``$(?![\\s\\S])`` -- "and nothing at all
follows". ``OpaqueToken`` already used exactly this guard before the migration,
so the idiom is the contract's own.

Everything here discovers its subjects by walking the packaged schemas. A
handwritten list of productions would drift out of step with the schemas and
leave a definition unguarded with nothing to say so, which is the failure this
migration exists to remove.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from omnivia_core.contracts.v1 import generated

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"

#: The guard appended to every terminal anchor.
GUARD = "(?![\\s\\S])"

#: Suffixes a bare terminal ``$`` used to admit, plus ones no anchor ever did.
#: ``\n`` is the defect itself; the rest prove the guard did not stop at it.
TRAILING_REJECTS = {
    "line feed": "\n",
    "carriage return": "\r",
    "crlf": "\r\n",
    "form feed": "\f",
    "vertical tab": "\v",
    "line separator": " ",
    "paragraph separator": " ",
    "next line": "",
    "null": "\x00",
    "tab": "\t",
    "space": " ",
    "non-ascii": "é",
}


def _patterned_definitions() -> dict[str, dict[str, Any]]:
    """Every schema node carrying a ``pattern``, keyed by document and pointer."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))

        def walk(node: Any, trail: list[str], source: str = path.stem) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("pattern"), str):
                    found[f"{source}:{'.'.join(trail)}"] = node
                for key, value in node.items():
                    walk(value, [*trail, key])
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, [*trail, str(index)])

        walk(document, [])
    return found


DEFINITIONS = _patterned_definitions()
DEFINITION_IDS = sorted(DEFINITIONS)


def _corpus_strings() -> set[str]:
    """Every string in the frozen fixtures, as a source of known-good values."""
    strings: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, str):
            strings.add(node)

    for path in FIXTURES_DIR.glob("*.json"):
        collect(json.loads(path.read_text(encoding="utf-8")))
    return strings


CORPUS_STRINGS = _corpus_strings()


def _accepts(node: dict[str, Any], value: str) -> bool:
    """Draft 2020-12 acceptance of `value` against this scalar node alone."""
    schema = {k: v for k, v in node.items() if k in {"pattern", "minLength", "maxLength", "type"}}
    schema.setdefault("type", "string")
    return Draft202012Validator(schema).is_valid(value)


def _first_matching_corpus_string(node: dict[str, Any]) -> str | None:
    """The first corpus string this node accepts, in a fixed, deterministic order.

    ``CORPUS_STRINGS`` is a set; iterating it directly means the value a bare
    ``next()`` lands on depends on Python's per-process string hash seed.
    Sorting first pins the order so sample choice -- and anything built from
    that sample -- no longer varies across ``PYTHONHASHSEED``.
    """
    return next((s for s in sorted(CORPUS_STRINGS) if _accepts(node, s)), None)


def _sample(name: str) -> str:
    value = _first_matching_corpus_string(DEFINITIONS[name])
    if value is None:
        pytest.skip(f"{name}: the frozen corpus carries no value of this shape")
    return value


def _over_length_witness(name: str) -> str | None:
    """An in-language value exceeding this definition's ``maxLength``, or ``None``.

    Appending a repeated character past ``maxLength`` only stays in-language
    when the run being extended carries no leading-character constraint (for
    example a numeric segment that must not gain a leading zero) -- so the
    same construction can succeed for one corpus value and fail for another
    of the same shape. ``ContractVersion``'s corpus samples split exactly
    this way (``"1.0"`` and ``"2.0"`` cannot be extended; ``"1.1"``, ``"1.2"``
    and ``"1.3"`` can), which is why a single hash-ordered sample pick made
    this definition's maxLength coverage alternate between pass and skip
    across ``PYTHONHASHSEED`` values. Trying every corpus candidate, and
    every distinct character each one contains, in a fixed sorted order,
    makes which witness -- if any -- is found independent of set order.
    """
    node = DEFINITIONS[name]
    maximum = int(node["maxLength"])
    pattern = str(node["pattern"])
    for value in sorted(s for s in CORPUS_STRINGS if _accepts(node, s)):
        for char in sorted(set(value)):
            overlong = value + char * (maximum - len(value) + 1)
            if re.search(pattern, overlong) is not None:
                return overlong
    return None


# --------------------------------------------------------------------------
# The migration is complete and discovered, not listed
# --------------------------------------------------------------------------


def test_the_audited_population_is_what_the_schemas_contain() -> None:
    """Pins the size of the thing everything below asserts about.

    The audit found 54 definitions carrying a bare terminal anchor across 13
    pattern families. `OpaqueToken` was the 55th patterned definition and already
    carried the guard, which is why the idiom used here is the contract's own
    rather than one invented for the migration -- and once the other 54 are
    hardened its pattern is no longer distinct, so those 55 definitions share 13
    families. `ServiceEndpointUri` is the 56th patterned definition and the only
    one added since, contributing the single endpoint-policy family and carrying
    the terminal guard from its first publication.
    """
    assert len(DEFINITIONS) == 56
    families = {node["pattern"] for node in DEFINITIONS.values()}
    assert len(families) == 14, sorted(families)


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_every_terminal_anchor_is_guarded(name: str) -> None:
    """No definition may end in a bare `$`."""
    pattern = DEFINITIONS[name]["pattern"]
    if not pattern.endswith("$") and GUARD not in pattern:
        pytest.skip(f"{name}: not terminally anchored")
    assert pattern.endswith(f"${GUARD}"), pattern


# --------------------------------------------------------------------------
# Python: schema validation and semantic fullmatch now agree
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_schema_validation_and_semantic_fullmatch_agree(name: str) -> None:
    """The parity the migration exists to restore.

    Schema validation is a *search*; the semantic layer uses `fullmatch`. Where
    those two disagree, a document can satisfy the published schema and be
    refused by the library that reads it.
    """
    node, value = DEFINITIONS[name], _sample(name)
    pattern = node["pattern"]
    for suffix in ("", *TRAILING_REJECTS.values()):
        candidate = value + suffix
        assert (re.search(pattern, candidate) is not None) == (
            re.fullmatch(pattern, candidate) is not None
        ), f"{name}: search and fullmatch disagree on {candidate!r}"


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_a_valid_value_is_still_accepted(name: str) -> None:
    """The guard must not narrow the language for well-formed values."""
    assert _accepts(DEFINITIONS[name], _sample(name))


@pytest.mark.parametrize("label", sorted(TRAILING_REJECTS))
@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_no_definition_admits_a_trailing_character(name: str, label: str) -> None:
    """A trailing line feed was accepted by 52 of the 54 audited definitions."""
    node, value = DEFINITIONS[name], _sample(name)
    assert not _accepts(node, value + TRAILING_REJECTS[label])


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_removing_the_guard_readmits_a_trailing_newline(name: str) -> None:
    """Per-definition proof that the guard is what does the work.

    Without this, a definition could be guarded in form while some other
    keyword -- a `maxLength` that a sample happens to sit against -- is what
    actually rejects the trailing byte.
    """
    node, value = DEFINITIONS[name], _sample(name)
    unguarded = {**node, "pattern": node["pattern"].replace(GUARD, "")}
    if _accepts(unguarded, value + "\n"):
        return  # the guard is load-bearing here
    # Otherwise some other keyword rejects it; say which, rather than pass mutely.
    assert node.get("maxLength") == len(value), (
        f"{name}: the guard appears inert and no length bound explains it"
    )


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_length_bounds_still_bind(name: str) -> None:
    """The guard must not have displaced the other scalar keywords.

    A terminal guard is appended to `pattern`, so nothing should have touched
    `minLength` or `maxLength` -- but "should" is what a test is for. Where a
    bound exists it is exercised against a value built to satisfy the pattern and
    violate the bound; where a definition declares no bound there is nothing to
    check, and the test says so rather than passing silently.
    """
    node = DEFINITIONS[name]
    minimum, maximum = node.get("minLength"), node.get("maxLength")
    if minimum is None and maximum is None:
        pytest.skip(f"{name}: declares no length bound")

    if isinstance(minimum, int) and minimum > 0:
        assert not _accepts(node, ""), f"{name}: minLength {minimum} does not reject an empty value"

    if isinstance(maximum, int):
        overlong = _over_length_witness(name)
        if overlong is None:
            pytest.skip(f"{name}: cannot build an over-long value in this language")
        assert len(overlong) > maximum
        assert not _accepts(node, overlong), f"{name}: maxLength {maximum} does not bind"


def test_contract_version_max_length_witness_is_always_found() -> None:
    """A27-R5 pin: ``ContractVersion`` must never fall back to a skip here.

    Unlike ``Timestamp``, ``ContextPackDigest`` and ``ContentChecksum`` --
    whose patterns are fixed-width or bounded tightly enough that no
    in-language value can exceed `maxLength` -- `ContractVersion`'s minor
    version segment is an unbounded numeric run, so a witness must always be
    constructible from the frozen corpus. This turns that requirement into an
    explicit failure rather than a silent, corpus-dependent skip.
    """
    name = "common.schema:$defs.ContractVersion"
    assert name in DEFINITIONS
    assert _over_length_witness(name) is not None


# --------------------------------------------------------------------------
# The generated artifacts carry the same language
# --------------------------------------------------------------------------


def test_every_generated_pattern_constant_is_guarded() -> None:
    """The emitted constants are what runtime callers actually match against."""
    constants = {
        name: getattr(generated, name)
        for name in dir(generated)
        if name.endswith("_PATTERN") and isinstance(getattr(generated, name), str)
    }
    assert constants, "no generated pattern constants found"
    unguarded = {n: p for n, p in constants.items() if p.endswith("$")}
    assert not unguarded, unguarded


def test_generated_constants_match_the_canonical_schemas() -> None:
    """A constant that drifted from its schema would publish a second language."""
    schema_patterns = {node["pattern"] for node in DEFINITIONS.values()}
    for name in dir(generated):
        if not name.endswith("_PATTERN"):
            continue
        value = getattr(generated, name)
        if isinstance(value, str):
            assert value in schema_patterns, f"{name} matches no canonical pattern"


@pytest.mark.parametrize("name", ["PROJECTION_VERSION_PATTERN", "RECORD_VERSION_PATTERN"])
def test_the_named_public_paths_reject_a_trailing_newline(name: str) -> None:
    """Called out by the migration plan, and reachable by any caller."""
    pattern = getattr(generated, name)
    assert re.search(pattern, "v1") is not None
    assert re.search(pattern, "v1\n") is None


# --------------------------------------------------------------------------
# ECMAScript reads the same language
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_ecmascript_and_python_accept_the_same_values() -> None:
    """The contract publishes one wire language, not one per runtime.

    ECMAScript's `$` is already strict without the `m` flag, so before the
    migration Python accepted values that a TypeScript client refused -- the two
    generated artifacts described different contracts.
    """
    cases = []
    for name in DEFINITION_IDS:
        node = DEFINITIONS[name]
        value = _first_matching_corpus_string(node)
        if value is None:
            continue
        for suffix in ("", "\n", "\r", " "):
            cases.append({"name": name, "pattern": node["pattern"], "value": value + suffix})

    script = """
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map(c => {
  let ok;
  try { ok = new RegExp(c.pattern).test(c.value); } catch (e) { ok = 'error: ' + e.message; }
  return ok;
});
process.stdout.write(JSON.stringify(out));
"""
    finished = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
    )
    ecmascript = json.loads(finished.stdout)
    assert len(ecmascript) == len(cases)
    disagreements = [
        (case["name"], case["value"])
        for case, accepted in zip(cases, ecmascript, strict=True)
        if accepted is not (re.search(case["pattern"], case["value"]) is not None)
    ]
    assert not disagreements, disagreements
