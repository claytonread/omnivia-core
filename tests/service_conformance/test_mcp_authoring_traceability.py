"""The R004 v1.3 requirement-traceability record, held to what it references.

``docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-traceability-2026-09-12.md``
is the Phase 7 deliverable of the implementation plan: every R1-R7 rule and every
normative bullet of specification section 13.A-13.I mapped to the file that
implements it and the pytest node, repository script or recorded qualification
that evidences it. A traceability record whose references have rotted is worse
than none, so this module holds it to four properties, all of them decidable
offline from the source tree:

* every requirement id ``R1``-``R7`` and every acceptance subsection ``13.A``-
  ``13.I`` is present;
* every repository-relative path it names exists;
* every pytest node id it names resolves to a real test definition in a real
  file -- checked by parsing the module, in the same way
  ``test_architecture_gate_traceability`` checks its ledger, because running
  pytest from inside pytest is neither deterministic nor cheap;
* no row that is short of its evidence claims to have it. A ``pending-phase-8``
  or ``partial`` row may not use completion language, and a row whose evidence
  type is ``HOST`` -- a session driven by an installed Claude Code or Codex
  binary -- may not be green, because this repository holds no such record and
  the SDK-driven journeys are not one.

Standard library only, like its two neighbours here: nothing in this module may
import a Runtime, Client, MCP or CLI package, and a green row's own evidence is
not run here. This module proves the record's references resolve, not that the
product behind them works.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCUMENT_PATH = (
    REPO_ROOT
    / "docs"
    / "development"
    / "omnivia-core-mcp-standalone-authoring-and-ingestion-traceability-2026-09-12.md"
)
DOCUMENT = DOCUMENT_PATH.read_text(encoding="utf-8")

REQUIREMENT_IDS = tuple(f"R{ordinal}" for ordinal in range(1, 8))
ACCEPTANCE_SECTIONS = tuple(f"13.{letter}" for letter in "ABCDEFGHI")

#: The four evidence types the record is allowed to claim, and nothing else.
EVIDENCE_TYPES = ("AUTO", "WHEEL", "HOST", "REVIEW")
#: The three status values. ``green`` is a claim; the other two are not.
GREEN = "green"
STATUSES = (GREEN, "partial", "pending-phase-8")
UNEVIDENCED_STATUSES = tuple(status for status in STATUSES if status != GREEN)

#: Repository top-level directories. A backticked token whose first segment is
#: one of these is a repository path and has to exist; anything else -- a media
#: type, a scope, a URL, a symbol -- is not a path and is not checked as one.
TOP_LEVEL_DIRECTORIES = frozenset(
    {
        ".github",
        "apps",
        "baseline",
        "benchmarks",
        "compatibility",
        "conformance",
        "contracts",
        "data",
        "docs",
        "generated",
        "packages",
        "qualification",
        "scripts",
        "services",
        "src",
        "tests",
    }
)

#: Words that assert an item is finished. A row that is not green may not use
#: one of them except under a negation, so "no real-host record" passes and
#: "real-host qualification passed" does not.
COMPLETION_WORDS = (
    "complete",
    "completed",
    "passes",
    "passed",
    "passing",
    "qualifies",
    "qualified",
    "verified",
    "satisfied",
    "green",
    "done",
)
NEGATIONS = frozenset(
    {"no", "not", "never", "cannot", "without", "neither", "nor", "yet", "un"}
)

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FENCED_BLOCK = re.compile(r"^```[a-z]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_TRAILING_PUNCTUATION = '.,;:)"\''


def _tokens() -> set[str]:
    """Every backticked span and every whitespace-separated word in a code fence.

    Paths and node ids are written in code spans throughout the record, so the
    document's own typography is what this reads. Prose is never scanned: a
    sentence that happens to contain a slash is not a reference.
    """
    found: set[str] = set()
    for span in _INLINE_CODE.findall(DOCUMENT):
        found.add(span.strip())
        found.update(span.split())
    for block in _FENCED_BLOCK.findall(DOCUMENT):
        found.update(block.split())
    return {token.strip(_TRAILING_PUNCTUATION) for token in found if token.strip()}


TOKENS = _tokens()


def _is_repository_path(token: str) -> bool:
    return (
        "://" not in token
        and "::" not in token
        and "/" in token
        and token.split("/", 1)[0] in TOP_LEVEL_DIRECTORIES
    )


PATH_TOKENS = sorted(token for token in TOKENS if _is_repository_path(token))
NODE_TOKENS = sorted(
    token
    for token in TOKENS
    if "::" in token and token.split("::", 1)[0].split("/", 1)[0] in TOP_LEVEL_DIRECTORIES
)


def _table_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for line in DOCUMENT.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


TABLE_ROWS = _table_rows()
#: ``(status, evidence types, the row as written)`` for every row that states one.
STATED_ROWS = [
    (
        next(cell for cell in row if cell in STATUSES),
        [cell for cell in row if cell in EVIDENCE_TYPES],
        " | ".join(row),
    )
    for row in TABLE_ROWS
    if any(cell in STATUSES for cell in row)
]


# --------------------------------------------------------------------------
# The record exists and covers every requirement and acceptance subsection
# --------------------------------------------------------------------------


_STEM = "docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion"
SOURCES = (
    f"{_STEM}-requirements-2026-09-12-v1.3.md",
    f"{_STEM}-implementation-plan-2026-09-12.md",
)


def test_the_record_names_the_specification_and_the_plan_it_answers() -> None:
    for named in SOURCES:
        assert named in TOKENS, named
        assert (REPO_ROOT / named).is_file(), named


@pytest.mark.parametrize("requirement_id", REQUIREMENT_IDS)
def test_every_requirement_id_leads_a_row_of_its_own(requirement_id: str) -> None:
    """R1-R7 are Appendix E's rules; each has to be the first cell of a row."""
    assert re.search(rf"^\|\s*{requirement_id}\s*\|", DOCUMENT, re.MULTILINE), requirement_id


@pytest.mark.parametrize("section", ACCEPTANCE_SECTIONS)
def test_every_acceptance_subsection_has_its_own_heading(section: str) -> None:
    assert re.search(rf"^#+ {re.escape(section)}[ .]", DOCUMENT, re.MULTILINE), section


def test_the_record_declares_exactly_the_four_evidence_types() -> None:
    for evidence_type in EVIDENCE_TYPES:
        assert re.search(rf"^\|\s*`?{evidence_type}`?\s*\|", DOCUMENT, re.MULTILINE), (
            evidence_type
        )


def test_the_record_states_a_status_on_enough_rows_to_be_a_mapping() -> None:
    """Anti-vacuous: an emptied or gutted record fails here rather than passing."""
    assert len(STATED_ROWS) >= 60
    assert len(PATH_TOKENS) >= 30
    assert len(NODE_TOKENS) >= 60


# --------------------------------------------------------------------------
# Every reference resolves
# --------------------------------------------------------------------------


def test_every_repository_path_the_record_names_exists() -> None:
    missing = [token for token in PATH_TOKENS if not (REPO_ROOT / token).exists()]
    assert not missing, f"referenced paths that do not exist: {missing}"


_DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _scope(body: list[ast.stmt]) -> dict[str, ast.stmt]:
    """Definition name -> node, for one scope."""
    return {node.name: node for node in body if isinstance(node, _DEFINITION)}


def _parametrized(source: str, node_id_path: list[str]) -> bool:
    """Whether a ``parametrize`` marker reaches the named node.

    Walks module scope, then each enclosing class, then the node's own
    decorators. At each scope the non-definition statements count, because a
    module- or class-level ``pytestmark`` parametrizes everything under it --
    the pattern the runtime authorization suite uses to run every test over the
    whole catalogue. A mention inside a body does not count.
    """
    body = ast.parse(source).body
    scopes: list[ast.AST] = []
    for name in node_id_path:
        scopes += [stmt for stmt in body if not isinstance(stmt, _DEFINITION)]
        node = next(
            stmt for stmt in body if isinstance(stmt, _DEFINITION) and stmt.name == name
        )
        scopes += node.decorator_list
        body = node.body
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == "parametrize"
        for scope in scopes
        for sub in ast.walk(scope)
    )


def test_every_pytest_node_the_record_names_resolves_to_a_real_test() -> None:
    """Resolved by parsing the module, not by running pytest.

    A node id is ``file::name`` or ``file::Class::name``, with an optional
    ``[parameter id]`` on the last segment. The file must exist and be Python,
    and every segment must be a definition in the enclosing scope, so a renamed
    test or a deleted file fails here. A stated parameter id must either appear
    literally in the module or belong to a test that really is parametrized --
    pytest derives some ids from values rather than from source text, so a
    literal match cannot be required of all of them.
    """
    for node_id in NODE_TOKENS:
        file, *path = node_id.split("::")
        module = REPO_ROOT / file
        assert module.is_file() and module.suffix == ".py", node_id
        assert path, node_id
        source = module.read_text(encoding="utf-8")
        scope = _scope(ast.parse(source, filename=str(module)).body)
        for segment in path[:-1]:
            assert segment in scope, node_id
            scope = _scope(scope[segment].body)  # type: ignore[union-attr]
        leaf, _, parameter = path[-1].partition("[")
        assert leaf in scope, node_id
        if parameter:
            identifier = parameter.rstrip("]")
            resolved = [*path[:-1], leaf]
            assert identifier in source or _parametrized(source, resolved), node_id


# --------------------------------------------------------------------------
# No row claims more than it has
# --------------------------------------------------------------------------


def _completion_claims(row: str) -> list[str]:
    """Completion words in ``row`` whose own clause carries no negation.

    Negation is read per clause -- the text between two of ``. , ; |`` -- rather
    than from the one preceding word, so "no real-host run has passed" is a
    denial while "real-host qualification passed" is a claim.
    """
    claims = []
    for clause in re.split(r"[.,;|]", row.lower()):
        words = re.findall(r"[a-z0-9-]+", clause)
        if NEGATIONS.isdisjoint(words):
            claims += [word for word in words if word in COMPLETION_WORDS]
    return claims


def test_the_completion_word_detector_sees_a_claim_and_allows_a_denial() -> None:
    """Anti-vacuous: the rule below is only worth as much as this detector."""
    assert _completion_claims("real-host qualification passed | pending-phase-8")
    assert _completion_claims("the packaging gate is complete")
    assert _completion_claims("no host ran it | the wheelhouse gate passed")
    assert not _completion_claims("no real-host run has passed | pending-phase-8")
    assert not _completion_claims("not yet qualified against the pinned wheelhouse")
    assert not _completion_claims("staged import observed through job_events")


def test_no_row_short_of_its_evidence_uses_completion_language() -> None:
    for status, _, row in STATED_ROWS:
        if status in UNEVIDENCED_STATUSES:
            assert not _completion_claims(row), row


def test_no_row_claims_a_real_host_pass_this_repository_does_not_hold() -> None:
    """``HOST`` is an installed Claude Code or Codex binary driving the server.

    Nothing in this tree is one: the journeys drive a real child process with
    the official SDK's ``stdio_client``, which is a client, not a host. So a
    ``HOST`` row may not be green until a recorded qualification exists, and
    this is the guard that keeps an SDK simulation from being relabelled.
    """
    for status, evidence_types, row in STATED_ROWS:
        if "HOST" in evidence_types:
            assert status != GREEN, row


def test_every_stated_row_names_at_least_one_evidence_type() -> None:
    for status, evidence_types, row in STATED_ROWS:
        assert evidence_types, row
        assert status in STATUSES, row
