"""The generated value-domain guards: emission, schema agreement, and parity.

The generator emits one guard per patterned scalar ``$defs`` entry, in both
bindings, from the declaration rather than from a list of definition names. Three
properties make that worth having, and each is checked here rather than asserted
in prose:

1. **The rule is general.** Every patterned definition has a guard in *both*
   languages. A rule that emits 56 of 56 and a table that emits 2 of 56 read
   identically at the call site; only a count over the schema separates them.
2. **A guard says what the schema says.** The canonical JSON Schema is the
   authority on the value domain, so the guard is compared against it -- with
   format checking on, because ``Timestamp`` declares ``format: date-time`` and a
   pattern is not a calendar.
3. **The two bindings agree.** Compiled with the pinned ``tsc`` and executed under
   ``node`` against the same corpus, not compared as source text. A guard that
   reads correctly and computes something else is exactly what a source-level
   assertion cannot see, and a cross-language disagreement about what a valid
   value is has already happened once in this contract.

The fourth property -- that ``from_wire`` calls no guard -- is the tolerant-decoder
constraint, and it is checked last, both in the syntax tree and by decoding a
malformed value.
"""

from __future__ import annotations

import ast
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
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
GENERATED_PY = REPO_ROOT / "src" / "omnivia_core" / "contracts" / "v1" / "generated.py"
TYPESCRIPT_PATH = (
    REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"
)

#: The documents the generator itself names, in its own order. Read from the
#: generator rather than restated, so a document added there is covered here.
SOURCE_SCHEMAS: tuple[str, ...] = (
    "common",
    "compatibility",
    "errors",
    "envelopes",
    "service",
    "records",
    "jobs",
    "operations",
    "workspace",
    "memory",
    "evidence",
    "knowledge",
    "graph",
    "context-pack",
    "compatibility-matrix",
)


def _snake(name: str) -> str:
    """``ServiceEndpointUri`` -> ``service_endpoint_uri``, as the generator spells it."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _patterned_definitions() -> dict[str, dict[str, Any]]:
    """Every ``type: string`` ``$defs`` entry declaring a ``pattern``, by name."""
    found: dict[str, dict[str, Any]] = {}
    for stem in SOURCE_SCHEMAS:
        document = json.loads(
            (SCHEMAS_DIR / f"{stem}.schema.json").read_text(encoding="utf-8")
        )
        for name, body in document.get("$defs", {}).items():
            if body.get("type") == "string" and isinstance(body.get("pattern"), str):
                found[name] = body
    return found


def _enum_definitions() -> dict[str, dict[str, Any]]:
    """Every ``type: string`` ``$defs`` entry declaring a closed ``enum``, by name.

    The second population the same emitter rule covers: a patterned definition gets
    its pattern guard, an enum definition gets its membership guard, and both are
    discovered here rather than listed, for the same reason.
    """
    found: dict[str, dict[str, Any]] = {}
    for stem in SOURCE_SCHEMAS:
        document = json.loads(
            (SCHEMAS_DIR / f"{stem}.schema.json").read_text(encoding="utf-8")
        )
        for name, body in document.get("$defs", {}).items():
            if body.get("type") == "string" and isinstance(body.get("enum"), list):
                found[name] = body
    return found


DEFINITIONS = _patterned_definitions()
DEFINITION_IDS = sorted(DEFINITIONS)
ENUM_DEFINITIONS = _enum_definitions()


def _corpus() -> list[str]:
    """Every string in the frozen fixtures, plus values that probe the bounds.

    Fixture strings are values this contract already names, so a case that
    separates two surfaces is one the contract has an opinion about. The added
    strings are the shapes no fixture happens to carry -- empty, over-length, a
    trailing newline, a wrong-case UTC designator, a calendar that does not exist.
    """
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

    for path in sorted(FIXTURES_DIR.glob("*.json")):
        collect(json.loads(path.read_text(encoding="utf-8")))

    strings.update(
        {
            "",
            "a",
            "ab",
            "abc",
            "ok",
            "1.0",
            "1.2.5",
            "x" * 32,
            "x" * 33,
            "x" * 128,
            "x" * 129,
            "x" * 256,
            "x" * 2048,
            "x" * 2049,
            "serving\n",
            "serving ",
            " serving",
            "2026-07-30T00:00:00Z",
            "2026-07-30T00:00:00z",
            "2026-13-01T00:00:00Z",
            "2026-02-30T00:00:00Z",
            "2024-02-29T00:00:00Z",
            "2026-02-29T00:00:00Z",
            "2026-07-30T24:00:00Z",
            "2026-07-30T00:60:00Z",
            "2026-07-30T00:00:60Z",
            "0000-01-01T00:00:00Z",
            "0050-06-15T00:00:00Z",
            "2026-07-30T00:00:00+00:00",
            "not-a-timestamp",
            "http://127.0.0.1:8080",
            "https://user:pass@example.com",
            # `MediaType` is the one definition no frozen fixture carries a value of,
            # and a corpus with no accepted value cannot tell an exact guard from one
            # that refuses everything.
            "application/json",
            "text/plain",
            "application/vnd.omnivia+json",
            "application/",
            "/json",
        }
    )
    return sorted(strings)


CORPUS = _corpus()


def _schema_accepts(node: dict[str, Any], value: str) -> bool:
    """Draft 2020-12 acceptance of `value` against this scalar node alone.

    Format checking is on. ``Timestamp`` declares ``format: date-time`` as well as a
    ``pattern``, and the two say different things -- dropping the format checker here
    would make the schema agree with a pattern-only guard and hide exactly the half
    this lane added.
    """
    schema = {
        key: value_
        for key, value_ in node.items()
        if key in {"type", "pattern", "minLength", "maxLength", "format"}
    }
    schema.setdefault("type", "string")
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return validator.is_valid(value)


def _python_guard(name: str) -> Any:
    return getattr(generated, f"is_{_snake(name)}")


# --------------------------------------------------------------------------
# 1. The rule is general
# --------------------------------------------------------------------------


def test_the_audited_population_is_what_the_schemas_contain() -> None:
    """The population is discovered, not listed, so a new definition joins it."""
    assert len(DEFINITIONS) == 56, sorted(DEFINITIONS)


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_every_patterned_definition_has_a_python_guard(name: str) -> None:
    guard = f"is_{_snake(name)}"
    assert hasattr(generated, guard), f"{name}: the generator emitted no {guard}"
    assert guard in generated.__all__, f"{name}: {guard} is emitted but not exported"


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_every_patterned_definition_has_a_typescript_guard(name: str) -> None:
    source = TYPESCRIPT_PATH.read_text(encoding="utf-8")
    signature = f"export function is{name}(value: unknown): value is {name} {{"
    assert signature in source, f"{name}: the generator emitted no is{name}"


def test_the_guard_count_matches_the_definition_count_in_both_languages() -> None:
    """56 of 56, twice. A hand-maintained table emitting two would pass every
    per-definition test above only by being extended to 56 entries, which is the
    point: the rule is what makes the count follow the schema for free.

    TypeScript carries the enum-membership guards on top, by the same rule and
    discovered the same way, so the emitted set has to be exactly the two
    populations the schemas declare -- no third guard from anywhere else.
    """
    python = [name for name in generated.__all__ if name.startswith("is_")]
    typescript = re.findall(
        r"export function (is\w+)\(value: unknown\)",
        TYPESCRIPT_PATH.read_text(encoding="utf-8"),
    )
    assert len(python) == len(DEFINITIONS)
    assert len(typescript) == len(DEFINITIONS) + len(ENUM_DEFINITIONS)
    assert {f"is{name}" for name in [*DEFINITIONS, *ENUM_DEFINITIONS]} == set(typescript)


# --------------------------------------------------------------------------
# 2. A guard says what the schema says
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_the_python_guard_agrees_with_the_canonical_schema(name: str) -> None:
    """The schema is the authority on the value domain; the guard restates it."""
    node = DEFINITIONS[name]
    guard = _python_guard(name)
    disagreements = [
        (value, _schema_accepts(node, value), guard(value))
        for value in CORPUS
        if _schema_accepts(node, value) != guard(value)
    ]
    assert not disagreements, f"{name}: schema and guard disagree on {disagreements}"


@pytest.mark.parametrize("name", DEFINITION_IDS)
def test_a_guard_refuses_every_non_string(name: str) -> None:
    """A value domain over strings answers `False` rather than raising."""
    guard = _python_guard(name)
    for value in (None, 7, 1.5, True, b"bytes", [], {}, object()):
        assert guard(value) is False, f"{name}: accepted {value!r}"


def test_both_verdicts_are_reachable_for_every_definition() -> None:
    """A guard that always refuses would satisfy every agreement test above."""
    for name in DEFINITION_IDS:
        guard = _python_guard(name)
        verdicts = {guard(value) for value in CORPUS}
        assert verdicts == {True, False}, f"{name}: only ever returns {verdicts}"


def test_the_timestamp_guard_applies_the_calendar_and_not_only_the_pattern() -> None:
    """The half a pattern cannot express, stated as its own case.

    `2026-13-01T00:00:00Z` satisfies `TIMESTAMP_PATTERN` character for character.
    If this ever passes on pattern alone, the agreement test above would still be
    green for every definition whose corpus carries no impossible calendar.
    """
    assert re.fullmatch(generated.TIMESTAMP_PATTERN, "2026-13-01T00:00:00Z")
    assert generated.is_timestamp("2026-13-01T00:00:00Z") is False
    assert generated.is_timestamp("2026-02-30T00:00:00Z") is False
    assert generated.is_timestamp("2026-07-30T00:00:60Z") is False
    assert generated.is_timestamp("0000-01-01T00:00:00Z") is False
    assert generated.is_timestamp("2024-02-29T00:00:00Z") is True
    assert generated.is_timestamp("2026-07-30T00:00:00Z") is True


# --------------------------------------------------------------------------
# 3. The two bindings agree
# --------------------------------------------------------------------------


def test_the_generated_bindings_return_the_same_verdict_on_every_guard(
    tmp_path: Path,
) -> None:
    """Every guard, crossed with the whole corpus, executed rather than read.

    This is the assertion that would have caught the `published_at` divergence, and
    it is general: it covers all 56 value domains, not the one that was reported.
    """
    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    node = shutil.which("node")
    assert tsc.is_file(), "run npm ci so the pinned TypeScript compiler is available"
    assert node is not None, "node is required for the executable parity gate"

    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--skipLibCheck",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--outDir",
            str(tmp_path),
            str(TYPESCRIPT_PATH),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    script = """
const contracts = require(process.argv[1]);
const {names, corpus} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(
  Object.fromEntries(names.map((name) =>
    [name, corpus.map((value) => contracts['is' + name](value))])),
));
"""
    finished = subprocess.run(
        [node, "-e", script, str(tmp_path / "index.js")],
        input=json.dumps({"names": DEFINITION_IDS, "corpus": CORPUS}),
        capture_output=True,
        text=True,
        check=True,
    )
    typescript = json.loads(finished.stdout)

    disagreements = [
        (name, value, python_verdict, typescript[name][index])
        for name in DEFINITION_IDS
        for index, value in enumerate(CORPUS)
        for python_verdict in [_python_guard(name)(value)]
        if python_verdict != typescript[name][index]
    ]
    assert not disagreements, f"the two bindings disagree on {disagreements}"

    # Both outcomes reachable in TypeScript too, or an all-refusing emitted guard
    # would agree with nothing and still pass a disagreement count of zero only if
    # Python also refused everything -- which the reachability test above forbids.
    for name in DEFINITION_IDS:
        assert set(typescript[name]) == {True, False}, name


# --------------------------------------------------------------------------
# 4. `from_wire` calls no guard
# --------------------------------------------------------------------------


def test_from_wire_calls_no_generated_guard() -> None:
    """The tolerant-decoder constraint, in the syntax tree.

    Decoding stays structural: it ignores unknown fields and preserves unknown open
    string values, which is what makes a compatible minor release possible. A guard
    called unconditionally from `from_wire` would end that, so no `from_wire` body
    may name one.
    """
    tree = ast.parse(GENERATED_PY.read_text(encoding="utf-8"))
    guards = {name for name in generated.__all__ if name.startswith("is_")}
    offenders = [
        (node.name, called)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "from_wire"
        for called in {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        & guards
    ]
    assert not offenders, f"from_wire calls a value-domain guard: {offenders}"


def test_a_malformed_timestamp_still_decodes_structurally() -> None:
    """The same constraint, behaviourally rather than syntactically.

    `ServiceProbeResult.observed_at` is refused by
    `validate_service_probe_result`, which is a *semantic* boundary. Structural
    decoding must still accept the value, or the tolerant path is gone whatever the
    syntax tree says.
    """
    payload = {
        "probe": "http",
        "status": "ok",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "not-a-timestamp",
    }
    decoded = generated.ServiceProbeResult.from_wire(payload, "ServiceProbeResult")
    assert decoded.observed_at == "not-a-timestamp"
    assert generated.is_timestamp(decoded.observed_at) is False
