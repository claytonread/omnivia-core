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

The final section holds the v1.4 completion addendum and its reference chain. The
addendum is a dated snapshot: its inventory, classification and version statements
are held to its own values (manifest 2.3, thirteen and eighteen tools, 57 operations,
four mutations) and to the catalogue entries they name. The live contract -- manifest
2.7, 77 operations, fourteen restricted and thirty-three authoring tools, fourteen
admitted mutations -- is held to the current manifest source, the catalogue and the current
traceability record, so a later version cannot leave this module green by drifting.
"""

from __future__ import annotations

import ast
import json
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
    "pass",
    "complete",
    "completed",
    "passes",
    "passed",
    "passing",
    "qualifies",
    "qualified",
    "accepted",
    "closed",
    "verified",
    "satisfied",
    "green",
    "done",
)
NEGATIONS = frozenset(
    {
        "no",
        "not",
        "never",
        "cannot",
        "without",
        "neither",
        "nor",
        "yet",
        "un",
    }
)

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FENCED_BLOCK = re.compile(r"^```[a-z]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_FAIL_CLOSED = re.compile(r"\bfail(?:s|ed|ing)?(?:\s+|-)closed\b")
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
    f"{_STEM}-requirements-2026-10-03-v1.4-addendum.md",
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
            parent = scope[segment]
            assert isinstance(
                parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ), node_id
            scope = _scope(parent.body)
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
    prose = _INLINE_CODE.sub("", row.lower())
    # "Fail closed" describes a refusal invariant, not completion.  Remove only
    # that phrase; a generic "fails" must not suppress a later completion claim
    # in the same clause (for example, "the run fails but the gate passed").
    prose = _FAIL_CLOSED.sub("", prose)
    for clause in re.split(r"[.,;|]", prose):
        words = re.findall(r"[a-z0-9-]+", clause)
        if NEGATIONS.isdisjoint(words):
            claims += [word for word in words if word in COMPLETION_WORDS]
    return claims


def test_the_completion_word_detector_sees_a_claim_and_allows_a_denial() -> None:
    """Anti-vacuous: the rule below is only worth as much as this detector."""
    assert _completion_claims("real-host qualification passed | pending-phase-8")
    assert _completion_claims("real-host qualification pass | pending-phase-8")
    assert _completion_claims("the host gate is accepted")
    assert _completion_claims("the evidence row is closed")
    assert _completion_claims("the packaging gate is complete")
    assert _completion_claims("no host ran it | the wheelhouse gate passed")
    assert _completion_claims("the run fails but the gate passed")
    assert not _completion_claims("no real-host run has passed | pending-phase-8")
    assert not _completion_claims("not yet qualified against the pinned wheelhouse")
    assert not _completion_claims("the mutation must fail closed")
    assert not _completion_claims("the mutation failed closed")
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


# --------------------------------------------------------------------------
# The v1.4 completion addendum: inventories, classification, version, chain
# --------------------------------------------------------------------------

ADDENDUM_NAME = (
    "omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-10-03-v1.4-addendum.md"
)
ADDENDUM_PATH = REPO_ROOT / "docs" / "development" / ADDENDUM_NAME
ADDENDUM = ADDENDUM_PATH.read_text(encoding="utf-8")
V13_NAME = "omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md"
V13 = (REPO_ROOT / "docs" / "development" / V13_NAME).read_text(encoding="utf-8")
IMPLEMENTATION_PLAN = (
    REPO_ROOT
    / "docs"
    / "development"
    / "omnivia-core-mcp-standalone-authoring-and-ingestion-implementation-plan-2026-09-12.md"
).read_text(encoding="utf-8")
COMPLETION_PLAN = (
    REPO_ROOT / "docs" / "development" / "omnivia-core-mcp-authoring-phase-8-completion-plan-2026-10-03.md"
).read_text(encoding="utf-8")
MANIFEST_SOURCE = (
    REPO_ROOT / "packages" / "omnivia-core-mcp" / "src" / "omnivia_core_mcp" / "manifest.py"
).read_text(encoding="utf-8")
WHEELHOUSE = (REPO_ROOT / "scripts" / "mcp-wheelhouse-constraints.txt").read_text(encoding="utf-8")
CATALOGUE_ENTRIES = json.loads(
    (REPO_ROOT / "contracts" / "application" / "v1" / "schemas" / "operations.schema.json").read_text(
        encoding="utf-8"
    )
)["x-omnivia-operation-catalogue"]
CATALOGUE = {entry["name"]: entry for entry in CATALOGUE_ENTRIES}

#: The reviewed inventories, in manifest order, as the addendum names them.
RESTRICTED_INVENTORY = (
    ("workspace_inspect", "workspace.inspect"),
    ("evidence_search", "evidence.search"),
    ("knowledge_search", "knowledge.search"),
    ("memory_search", "memory.search"),
    ("graph_traverse", "graph.traverse"),
    ("context_pack_build", "context_pack.build"),
    ("engineering_search", "engineering.search"),
    ("engineering_expand", "engineering.expand"),
    ("engineering_context_build", "engineering.context.build"),
    ("decision_evaluate", "decision.evaluate"),
    ("decision_record_get", "decision.record.get"),
    ("decision_record_list", "decision.record.list"),
    ("decision_status", "decision.status"),
)
ADDITIONS_INVENTORY = (
    ("memory_create", "memory.create"),
    ("evidence_capture", "evidence.capture"),
    ("import_start", "import.start"),
    ("job_get", "job.get"),
    ("job_events", "job.events"),
)
EXCLUDED_OPERATIONS = ("job.cancel", "job.retry")
MODEL_SELECTS_NOTHING = (
    "workspace, principal, purpose, scope, capability, credential, grant, endpoint or profile"
)


def _assigned(name: str) -> ast.expr:
    """The value assigned to one module-level name in ``manifest.py``."""
    for node in ast.parse(MANIFEST_SOURCE).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return node.value
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
    raise AssertionError(f"{name} is not assigned in manifest.py")


def _exposed(name: str) -> list[dict[str, str]]:
    """Each ``ExposedOperation(...)`` literal in one manifest tuple, in order."""
    tuple_value = _assigned(name)
    assert isinstance(tuple_value, ast.Tuple), name
    rows: list[dict[str, str]] = []
    for call in tuple_value.elts:
        assert isinstance(call, ast.Call), name
        row: dict[str, str] = {}
        for keyword in call.keywords:
            assert keyword.arg is not None, name
            value = ast.literal_eval(keyword.value)
            assert isinstance(value, str), name
            row[keyword.arg] = value
        rows.append(row)
    return rows


MANIFEST_VERSION = ast.literal_eval(_assigned("MANIFEST_VERSION"))
_ADMITTED = _assigned("ADMITTED_MUTATIONS")
assert isinstance(_ADMITTED, ast.Call), "ADMITTED_MUTATIONS is no longer frozenset({...})"
ADMITTED_MUTATIONS = ast.literal_eval(_ADMITTED.args[0])
RESTRICTED = _exposed("RESTRICTED_MANIFEST")
ADDITIONS = _exposed("_AUTHORING_ADDITIONS")
AUTHORING = RESTRICTED + ADDITIONS

#: The v1.4 addendum's own inventory: the 2.3 snapshot it was written against. It is
#: historical text, so the addendum is checked against these values, not the live manifest.
ADDENDUM_MANIFEST_VERSION = "2.3"
ADDENDUM_CATALOGUE_COUNT = 57
ADDENDUM_RESTRICTED_TOOLS = frozenset(tool for tool, _ in RESTRICTED_INVENTORY)
ADDENDUM_AUTHORING = RESTRICTED_INVENTORY + ADDITIONS_INVENTORY
ADDENDUM_MUTATIONS = frozenset(
    {"memory.create", "evidence.capture", "import.start", "decision.evaluate"}
)
#: The addendum's eighteen rows, each read from the live manifest entry that carries the
#: same tool name, so the operation facts they state come from the catalogue.
ADDENDUM_ENTRIES = [
    next(entry for entry in AUTHORING if entry["tool_name"] == tool) for tool, _ in ADDENDUM_AUTHORING
]

#: The live contract, as the current manifest source and traceability record state it.
CURRENT_MANIFEST_VERSION = "2.8"
CURRENT_CATALOGUE_COUNT = 79
CURRENT_RESTRICTED_INVENTORY = (*RESTRICTED_INVENTORY, ("trigger_health", "trigger.health"))
CURRENT_ADDITIONS_INVENTORY = (
    ("memory_create", "memory.create"),
    ("evidence_capture", "evidence.capture"),
    ("import_start", "import.start"),
    ("trigger_declare", "trigger.declare"),
    ("trigger_lifecycle", "trigger.lifecycle"),
    ("trigger_ingest", "trigger.ingest"),
    ("job_get", "job.get"),
    ("job_events", "job.events"),
    ("skills_draft_create", "skills.draft.create"),
    ("skills_draft_update", "skills.draft.update"),
    ("skills_proposal_submit", "skills.proposal.submit"),
    ("knowledge_share_propose", "knowledge.share.propose"),
    ("knowledge_share_decide", "knowledge.share.decide"),
    ("knowledge_share_read", "knowledge.share.read"),
    ("knowledge_share_lineage", "knowledge.share.lineage"),
    ("task_context_export", "task_context.export"),
    ("task_context_export_read", "task_context.export.read"),
    ("outcome_request_create", "outcome.request.create"),
    ("outcome_request_read", "outcome.request.read"),
    ("project_context_read", "project.context.read"),
    ("project_context_switch", "project.context.switch"),
)
CURRENT_MUTATIONS = frozenset(
    {
        "memory.create",
        "evidence.capture",
        "import.start",
        "decision.evaluate",
        "trigger.declare",
        "trigger.lifecycle",
        "trigger.ingest",
        "skills.draft.create",
        "skills.draft.update",
        "skills.proposal.submit",
        "knowledge.share.propose",
        "knowledge.share.decide",
        "task_context.export",
        "outcome.request.create",
        "project.context.switch",
    }
)
#: The section-7 sentinels the real-host harness probes, and the exclusion arithmetic that
#: follows from them. The interoperability guide must state these live numbers.
SECTION7_SENTINEL_COUNT = 18


def _cells(text: str) -> list[list[str]]:
    """Every table row of ``text`` as its stripped cells."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]


def _side_effect(operation: str) -> str:
    value = CATALOGUE[operation]["scope"]["side_effect"]
    assert isinstance(value, str), operation
    return value


def test_the_addendum_snapshot_is_its_reviewed_thirteen_and_eighteen() -> None:
    assert len(ADDENDUM_RESTRICTED_TOOLS) == 13
    assert len(ADDENDUM_AUTHORING) == 18
    assert "## 3. Restricted profile: thirteen tools" in ADDENDUM
    assert [tool for tool, _ in ADDENDUM_AUTHORING] == [entry["tool_name"] for entry in ADDENDUM_ENTRIES]


def test_the_live_inventories_are_fourteen_and_thirty_five() -> None:
    assert [(entry["tool_name"], entry["operation"]) for entry in RESTRICTED] == list(
        CURRENT_RESTRICTED_INVENTORY
    )
    assert [(entry["tool_name"], entry["operation"]) for entry in ADDITIONS] == list(
        CURRENT_ADDITIONS_INVENTORY
    )
    assert len(RESTRICTED) == 14
    assert len(AUTHORING) == 35
    assert "exactly fourteen restricted tools" in DOCUMENT
    # Deliberately the dated 2026-09-12 record's A-2 row (manifest 2.5), not a live count.
    assert "exactly twenty-five tools" in DOCUMENT


def test_the_addendum_names_version_2_3_and_the_live_manifest_is_version_2_7() -> None:
    assert f"`{ADDENDUM_MANIFEST_VERSION}`" in ADDENDUM
    assert MANIFEST_VERSION == CURRENT_MANIFEST_VERSION == "2.8"


def test_the_addendum_names_fifty_seven_and_the_live_catalogue_is_seventy_nine() -> None:
    assert f"{ADDENDUM_CATALOGUE_COUNT} operations" in ADDENDUM
    assert len(CATALOGUE_ENTRIES) == CURRENT_CATALOGUE_COUNT == 79
    assert len(CATALOGUE) == len(CATALOGUE_ENTRIES), "a catalogue operation name repeats"
    assert "fifty-four" not in MANIFEST_SOURCE


def test_restricted_is_bounded_non_authoring_and_not_read_only() -> None:
    """Restricted is bounded non-authoring: thirteen reads and one durable mutation."""
    mutations = [entry["operation"] for entry in RESTRICTED if _side_effect(entry["operation"]) != "none"]
    assert mutations == ["decision.evaluate"]
    assert "bounded non-authoring" in ADDENDUM
    assert "It is not read-only." in ADDENDUM
    assert "gets the read-only surface" not in MANIFEST_SOURCE
    assert "read-only surface" not in MANIFEST_SOURCE
    assert "read-only allow-list" not in MANIFEST_SOURCE


MCP_PACKAGE = REPO_ROOT / "packages" / "omnivia-core-mcp"
MCP_MODULES = sorted((MCP_PACKAGE / "src" / "omnivia_core_mcp").glob("*.py"))
INTEROPERABILITY = REPO_ROOT / "docs" / "distribution" / "mcp-host-interoperability.md"
#: A whole server, package or profile called read-only, or every mutation called
#: absent. Both are false of restricted, which carries `decision.evaluate`. A single
#: read tool's own "Read-only." description is true and is not matched.
FALSE_SURFACE_CLAIM = re.compile(
    r"\bread-only (?:access|surface|allow-list|server|profile)\b"
    r"|\bevery mutation (?:is|are) (?:deliberately )?absent\b",
    re.IGNORECASE,
)


def _stated_text(path: Path) -> str:
    """Every string a module states -- docstrings, messages, descriptions -- folded.

    ``ast`` merges implicitly concatenated literals into one constant, so a sentence
    wrapped across source lines is read whole. Comments state nothing to a reader.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return "\n".join(
        " ".join(node.value.split())
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_the_false_surface_claim_detector_sees_both_claims_and_allows_true_text() -> None:
    """Anti-vacuous: the scan below is only worth as much as this detector."""
    assert FALSE_SURFACE_CLAIM.search("gives an AI host read-only access to one workspace")
    assert FALSE_SURFACE_CLAIM.search("workspace creation and every mutation are deliberately absent")
    assert not FALSE_SURFACE_CLAIM.search("every other mutation are deliberately absent")
    assert not FALSE_SURFACE_CLAIM.search("read-only job observation")


def test_no_mcp_module_or_document_calls_a_profile_read_only() -> None:
    """The scan covers every MCP module, not only ``manifest.py``, and the public documents.

    The package docstring and the server's initialize instructions also have to state
    the bounded surface, so a description cannot pass by saying nothing.
    """
    stated = {path.name: _stated_text(path) for path in MCP_MODULES}
    assert {"__init__.py", "configuration.py", "manifest.py", "server.py"} <= set(stated)
    for document in (MCP_PACKAGE / "README.md", INTEROPERABILITY):
        stated[document.name] = " ".join(document.read_text(encoding="utf-8").split())
    for name, text in stated.items():
        assert not FALSE_SURFACE_CLAIM.search(text), name
    for name in ("__init__.py", "server.py"):
        assert "bounded non-authoring" in stated[name].lower(), name
        assert "advisory decision evaluation" in stated[name], name


def test_the_side_effecting_operations_are_exactly_the_admitted_mutations() -> None:
    addendum = {entry["operation"] for entry in ADDENDUM_ENTRIES if _side_effect(entry["operation"]) != "none"}
    assert addendum == ADDENDUM_MUTATIONS
    live = {entry["operation"] for entry in AUTHORING if _side_effect(entry["operation"]) != "none"}
    assert live == ADMITTED_MUTATIONS == CURRENT_MUTATIONS
    assert len(CURRENT_MUTATIONS) == 15


def test_memory_create_is_documented_as_proposed_only() -> None:
    assert "proposed-only governed memory record" in ADDENDUM
    assert "never creates accepted canonical knowledge" in ADDENDUM


def test_every_tool_is_classified_from_the_catalogue_in_manifest_order() -> None:
    rows = [cells for cells in _cells(ADDENDUM) if cells[0].isdigit() and len(cells) == 10]
    assert [row[0] for row in rows] == [str(number) for number in range(1, 19)]
    assert [row[1] for row in rows] == [f"`{entry['tool_name']}`" for entry in ADDENDUM_ENTRIES]
    assert [row[2] for row in rows] == [f"`{entry['operation']}`" for entry in ADDENDUM_ENTRIES]
    for row, exposed in zip(rows, ADDENDUM_ENTRIES, strict=True):
        entry = CATALOGUE[exposed["operation"]]
        capability = entry["required_capability"]
        idempotency = entry["idempotency"]
        assert row[3] == ("both" if exposed["tool_name"] in ADDENDUM_RESTRICTED_TOOLS else "authoring"), exposed["tool_name"]
        assert row[4] == entry["scope"]["side_effect"], exposed["tool_name"]
        assert row[5] == entry["audit"]["audit_category"], exposed["tool_name"]
        assert row[6] == f"`{exposed['purpose']}`", exposed["tool_name"]
        assert row[7] == ", ".join(f"`{scope}`" for scope in entry["scope"]["required_scopes"])
        assert capability["required"] is True
        assert row[8] == f"`{capability['id']}` {capability['minimum_version']}", exposed["tool_name"]
        expected = ("Key required" if idempotency["required"] else "No key") + (
            "; safe to retry" if idempotency["safe_to_retry"] else "; not safe to retry"
        )
        assert row[9] == expected, exposed["tool_name"]
        assert entry["audit"]["audited"] is True


def test_the_prose_lists_name_the_same_eighteen_tools_in_order() -> None:
    listed = re.findall(r"^(\d+)\. `([a-z_]+)` \(`([a-z._]+)`\)", ADDENDUM, re.MULTILINE)
    assert [int(number) for number, _, _ in listed] == list(range(1, 19))
    assert [(tool, operation) for _, tool, operation in listed] == [
        (entry["tool_name"], entry["operation"]) for entry in ADDENDUM_ENTRIES
    ]


def test_decision_evaluate_is_admitted_explicitly_with_its_catalogue_posture() -> None:
    entry = CATALOGUE["decision.evaluate"]
    assert entry["scope"] == {
        "required_scopes": ["decision:invoke"],
        "side_effect": "update",
        "scope_kind": "workspace",
    }
    assert entry["required_capability"] == {
        "id": "decision.invoke",
        "minimum_version": "1.0",
        "required": True,
    }
    assert entry["idempotency"] == {
        "supports_idempotency_key": True,
        "required": True,
        "safe_to_retry": False,
    }
    assert entry["audit"]["audit_category"] == "mutation"
    assert entry["job"]["completion_mode"] == "always_returns_job"
    assert "decision.evaluate" in ADMITTED_MUTATIONS
    for statement in (
        "`decision:invoke`",
        "`decision.invoke` 1.0",
        "`ADMITTED_MUTATIONS`",
        "It never mutates business records",
        "never authorises an action",
    ):
        assert statement in ADDENDUM, statement
    decision_row = next(row for row in _cells(ADDENDUM) if len(row) == 10 and row[1] == "`decision_evaluate`")
    assert decision_row[4:6] == ["update", "mutation"]
    assert decision_row[9] == "Key required; not safe to retry"


def test_the_excluded_operations_and_the_model_selection_rule_are_preserved() -> None:
    authoring_operations = {entry["operation"] for entry in AUTHORING}
    for operation in EXCLUDED_OPERATIONS:
        assert operation in CATALOGUE, operation
        assert operation not in authoring_operations, operation
        assert f"`{operation}`" in ADDENDUM, operation
    assert MODEL_SELECTS_NOTHING in ADDENDUM


def test_v13_is_preserved_unrewritten_and_the_addendum_names_it_as_its_base() -> None:
    flat = " ".join(V13.split())  # v1.3 wraps its sentences; compare the words, not the lines
    assert "the existing six read-only tools" in flat
    assert "advertises exactly eleven tools" in flat
    assert V13_NAME in ADDENDUM


def test_the_addendum_and_its_references_name_each_other_as_normative() -> None:
    assert ADDENDUM_NAME in IMPLEMENTATION_PLAN and "Normative completion baseline" in IMPLEMENTATION_PLAN
    assert ADDENDUM_NAME in DOCUMENT and "Normative completion baseline" in DOCUMENT
    assert ADDENDUM_NAME in COMPLETION_PLAN and "normative completion baseline" in COMPLETION_PLAN


def test_the_addendum_declares_no_completion_and_marks_no_gate_green() -> None:
    status = re.search(r"^\*\*Status:\*\*(.*?)\n\*\*", ADDENDUM, re.MULTILINE | re.DOTALL)
    assert status is not None
    assert not _completion_claims(status.group(1))
    assert "does not mark any gate green" in ADDENDUM


def test_no_plan_declares_completion_while_a_real_host_gate_is_pending() -> None:
    gate_rows = [row for row in TABLE_ROWS if re.match(r"I-\d ", row[0])]
    assert [row[0].split()[0] for row in gate_rows] == [
        f"I-{number}" for number in range(1, 9)
    ]
    assert {row[-1] for row in gate_rows} == {"pending-phase-8"}
    completion_status = re.search(r"^\*\*Status:\*\* (.*)$", COMPLETION_PLAN, re.MULTILINE)
    assert completion_status is not None
    assert not _completion_claims(completion_status.group(1))


def test_the_matrix_is_the_frozen_baseline_and_every_host_gate_is_pending() -> None:
    for value in ("2.1.288", "0.146.0", "27.0", "26A428", "arm64"):
        assert value in ADDENDUM, value
    assert "mcp==2.0.0" in WHEELHOUSE.splitlines()
    assert "mcp-types==2.0.0" in WHEELHOUSE.splitlines()
    gate_rows = [cells for cells in _cells(ADDENDUM) if re.fullmatch(r"I-\d", cells[0])]
    assert [cells[0] for cells in gate_rows] == [f"I-{number}" for number in range(1, 9)]
    for cells in gate_rows:
        assert cells[2] == cells[3] == "pending-phase-8", cells[0]


def test_the_manifest_docstring_names_a_bounded_restricted_surface() -> None:
    assert "bounded non-authoring surface rather than the wider one" in MANIFEST_SOURCE
    assert "fifteen named mutations" in MANIFEST_SOURCE


def test_the_interoperability_guide_states_the_live_profile_and_exclusion_counts() -> None:
    text = " ".join(INTEROPERABILITY.read_text(encoding="utf-8").split())
    unexposed = len(CATALOGUE_ENTRIES) - len(AUTHORING)
    restricted_excluded = unexposed + SECTION7_SENTINEL_COUNT + len(ADDITIONS)
    authoring_excluded = unexposed + SECTION7_SENTINEL_COUNT
    assert (unexposed, restricted_excluded, authoring_excluded) == (44, 83, 62)
    assert "restricted fourteen-tool inventory" in text
    assert "thirty-five-tool inventory: the restricted fourteen plus:" in text
    assert f"has {restricted_excluded} such names and the authoring profile {authoring_excluded}" in text
    assert f"the {unexposed} catalogue operations outside the authoring manifest" in text
    assert "eighteen deterministic qualification sentinels" in text
    assert "the twenty-one authoring additions" in text
