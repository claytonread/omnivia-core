"""Permanent guards for the real-host qualification lane.

Three artefacts are protected here, and none of them is exercised by running a
host: `scripts/run-host-qualification.py`, which drives installed Claude Code
and Codex CLI against installed OmniVia wheels; `scripts/{helper}`, the
qualification-only trusted staging fixture it calls; and
`packages/omnivia-core-mcp/tests/_mcp_interrupted_relay.py`, the test-tree relay
that stages R004 section 13.F's interrupted response. Everything below runs
offline, on whatever platform collects it, and calls no model and no host.

**Why the guards are written over source text and over a synthetic record.** The
properties that matter here are properties of the *producer*: that Claude Code
is given the explicit server and nothing of the user's, that Codex is ephemeral,
that the installation resolves from the wheelhouse and never from this checkout,
that the relay writes nothing but protocol to stdout, and that the staging
helper is not a product surface. A run that passed once is not evidence of any
of those, and a run is not available to a test that must stay offline. So each
is asserted against the artefact itself, and each schema and redaction guard is
put to a synthetic record that is mutated one field at a time -- a guard that
has only ever seen a passing input is not evidence that it fails.

**What these tests deliberately do not do.** They do not start a host, a
service, an MCP server or a model; they do not assert that any qualification
ever passed. Whether a real run happened is a property of the committed record,
and `test_any_committed_record_passes_its_own_guard` is the only test here that
looks at one -- it passes vacuously when none exists, which is exactly the state
the traceability document must show while section 13.I is unevidenced.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_PATH = REPO_ROOT / "scripts" / "run-host-qualification.py"
STAGING_PATH = REPO_ROOT / "scripts" / "qualification-stage-import-source.py"
RELAY_PATH = (
    REPO_ROOT / "packages" / "omnivia-core-mcp" / "tests" / "_mcp_interrupted_relay.py"
)


def _load(path: Path, name: str) -> Any:
    """Import a dashed script by path.

    Registered in `sys.modules` before execution because `@dataclass` resolves
    its own module out of there while the class body is being processed, and a
    module that is not registered yet makes that lookup fail.
    """
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None, path
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


HARNESS = _load(HARNESS_PATH, "_host_qualification_harness")
STAGING = _load(STAGING_PATH, "_host_qualification_staging")

HARNESS_TEXT = HARNESS_PATH.read_text(encoding="utf-8")
STAGING_TEXT = STAGING_PATH.read_text(encoding="utf-8")
RELAY_TEXT = RELAY_PATH.read_text(encoding="utf-8")

HARNESS_TREE = ast.parse(HARNESS_TEXT)
RELAY_TREE = ast.parse(RELAY_TEXT)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined")


def _method(tree: ast.Module, klass: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == klass:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == name:
                    return child
    raise AssertionError(f"{klass}.{name} is not defined")


def _source(node: ast.AST) -> str:
    return ast.get_source_segment(HARNESS_TEXT, node) or ""


# --- the lane's own inventory matches the product's ----------------------------


def test_the_lane_qualifies_the_inventory_the_manifest_actually_advertises() -> None:
    """The eleven names come from the manifest, not from a copy in the harness.

    A harness with its own list would keep passing after the product's inventory
    moved, which is the one failure an inventory check exists to catch.
    """
    from omnivia_core_mcp.manifest import AUTHORING_MANIFEST, RESTRICTED_MANIFEST

    advertised = tuple(sorted(tool.tool_name for tool in AUTHORING_MANIFEST))
    assert HARNESS.AUTHORING_TOOLS == advertised, "the harness advertises its own list"
    assert len(HARNESS.AUTHORING_TOOLS) == 11, "the authoring inventory is not eleven"
    assert HARNESS.RESTRICTED_TOOLS == tuple(
        sorted(tool.tool_name for tool in RESTRICTED_MANIFEST)
    ), "the restricted inventory drifted"
    assert not set(HARNESS.EXCLUDED_TOOLS) & set(HARNESS.AUTHORING_TOOLS), (
        "an excluded operation is also advertised"
    )


def test_the_lane_pins_the_versions_the_specification_names() -> None:
    assert HARNESS.REQUIRED_HOST_VERSIONS == {
        "claude-code": "2.1.269",
        "codex": "0.146.0",
    }, "the qualified host baseline moved"
    assert HARNESS.REQUIRED_SDK_PINS == {"mcp": "2.0.0", "mcp-types": "2.0.0"}, (
        "the qualified SDK pins moved"
    )
    assert HARNESS.REQUIRED_OS == {
        "version": "26.5.2",
        "build": "25F84",
        "arch": "arm64",
    }, "the qualified operating-system baseline moved"
    constraints = (REPO_ROOT / "scripts" / "mcp-wheelhouse-constraints.txt").read_text(
        encoding="utf-8"
    )
    for name, pin in HARNESS.REQUIRED_SDK_PINS.items():
        assert f"{name}=={pin}" in constraints, (
            "the lane's pin is not the reviewed wheelhouse pin"
        )


# --- host isolation ------------------------------------------------------------


def test_the_claude_lane_is_given_the_explicit_server_and_nothing_of_the_users() -> (
    None
):
    """Claude Code runs with the qualification config as its only configuration."""
    for method in ("run_step", "_session"):
        text = _source(_method(HARNESS_TREE, "ClaudeCode", method))
        for required in (
            '"--mcp-config"',
            '"--strict-mcp-config"',
            '"--restricted"',
            '"--no-session-persistence"',
            '"--permission-prompts"',
            '"--output-format"',
            '"stream-json"',
        ):
            assert required in text, f"ClaudeCode.{method} does not pass {required}"
        assert "cwd=str(self.cwd)" in text, (
            f"ClaudeCode.{method} runs in the repository"
        )
        assert "env=scrubbed_environment()" in text, (
            f"ClaudeCode.{method} inherits an unscrubbed environment"
        )
    for forbidden in (
        "--dangerously-skip-permissions",
        "--continue",
        "--resume",
        "mcp add",
        "add-json",
        "--setting-sources user",
    ):
        assert forbidden not in HARNESS_TEXT, f"the lane uses {forbidden}"


def test_the_codex_lane_is_ephemeral_and_ignores_the_users_configuration() -> None:
    text = _source(_method(HARNESS_TREE, "Codex", "run_step"))
    for required in (
        '"--ephemeral"',
        '"--ignore-user-config"',
        '"--ignore-rules"',
        '"--skip-git-repo-check"',
        '"-C"',
        '"--json"',
    ):
        assert required in text, f"Codex.run_step does not pass {required}"
    assert '"read-only"' in text, "Codex is not sandboxed read-only"
    overrides = _source(_method(HARNESS_TREE, "Codex", "_overrides"))
    assert "mcp_servers." in overrides, (
        "Codex is not given the server on the command line"
    )
    assert "codex mcp add" not in HARNESS_TEXT, (
        "the lane writes the user's Codex config"
    )
    assert "CODEX_HOME" not in HARNESS_TEXT, "the lane redirects Codex's home"


def test_no_normal_configuration_path_is_ever_written() -> None:
    """`Path.home()` is reachable from one function, and that function only reads.

    The rule is structural rather than a review habit: any other function that
    wanted a path under the user's home would have to call `Path.home()` to get
    one, so confining the call is confining the capability.
    """
    readers = set()
    for node in ast.walk(HARNESS_TREE):
        if not isinstance(node, ast.FunctionDef):
            continue
        if "Path.home()" in _source(node):
            readers.add(node.name)
    assert readers == {"config_surfaces"}, (
        f"the user's home is reachable from {sorted(readers)}"
    )
    surfaces = _source(_function(HARNESS_TREE, "config_surfaces"))
    for forbidden in (
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "replace",
        "chmod",
    ):
        assert forbidden not in surfaces, f"config_surfaces {forbidden}s a user path"
    assert "read_bytes()" in surfaces and "hashlib.sha256" in surfaces, (
        "config_surfaces does not hash what it promises to compare"
    )


def test_the_run_fails_when_a_configuration_surface_changed() -> None:
    main = _source(_function(HARNESS_TREE, "main"))
    assert "before = config_surfaces()" in main, "no surface snapshot is taken"
    assert "after = config_surfaces()" in main, "no surface comparison is made"
    assert "if before != after:" in main and "raise QualificationError" in main, (
        "a changed configuration surface does not fail the run"
    )


# --- installed, not this checkout ----------------------------------------------


def test_the_installation_resolves_from_the_wheelhouse_with_no_index() -> None:
    install = _source(_function(HARNESS_TREE, "install"))
    for required in ('"--no-index"', '"--only-binary=:all:"', '"--find-links"'):
        assert required in install, f"the install phase omits {required}"
    assert "REPO_ROOT in location.parents" in install, (
        "the install phase does not refuse a module from this checkout"
    )
    assert "resolved_prefix not in location.parents" in install, (
        "the install phase does not require the environment's own modules"
    )
    assert "this checkout is on the environment's import path" in install, (
        "the install phase does not inspect the environment's import path"
    )
    assert (
        "REQUIRED_SDK_PINS" in install and "is not {pin} in the environment" in install
    ), "the install phase does not enforce the reviewed SDK pins"


def test_only_the_acquisition_phase_may_reach_an_index() -> None:
    acquire = _source(_function(HARNESS_TREE, "acquire"))
    assert "pip" in acquire and "download" in acquire, "acquisition stages nothing"
    assert "--constraint" in acquire, "acquisition resolves without the reviewed pins"
    assert "--only-binary=:all:" in acquire, "acquisition would accept a source archive"
    for node in ast.walk(HARNESS_TREE):
        if isinstance(node, ast.FunctionDef) and node.name != "acquire":
            assert '"download"' not in _source(node), (
                f"{node.name} downloads outside the acquisition phase"
            )


def test_every_subprocess_inherits_an_environment_with_no_source_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT / "src"))
    monkeypatch.setenv("PYTHONHOME", "/nowhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(REPO_ROOT / ".venv"))
    environment = HARNESS.scrubbed_environment()
    for removed in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        assert removed not in environment, f"{removed} survives into a subprocess"
    assert environment["PYTHONNOUSERSITE"] == "1", "user site packages stay reachable"
    run = _source(_function(HARNESS_TREE, "run"))
    assert "env=scrubbed_environment()" in run, "the shared runner does not scrub"


# --- the relay, and what it is allowed to be -----------------------------------


def test_the_relay_is_not_in_any_installed_package() -> None:
    assert "tests" in RELAY_PATH.parts, "the relay left the test tree"
    packaged = REPO_ROOT / "packages" / "omnivia-core-mcp" / "src" / "omnivia_core_mcp"
    assert packaged not in RELAY_PATH.parents, "the relay is inside the wheel's package"
    for source_tree in sorted((REPO_ROOT / "packages").glob("*/src")):
        for module in source_tree.rglob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "_mcp_interrupted_relay" not in text, f"{module} names the relay"
            assert HARNESS.STAGING_HELPER not in text, (
                f"{module} names the staging helper"
            )
    for module in (REPO_ROOT / "src").rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "_mcp_interrupted_relay" not in text, f"{module} names the relay"
        assert HARNESS.STAGING_HELPER not in text, f"{module} names the staging helper"


def test_the_relay_keeps_stdout_protocol_only() -> None:
    """Nothing but forwarded bytes reaches stdout, and nothing is read from them."""
    assert "print(" not in RELAY_TEXT, "the relay prints to stdout"
    writes = [
        _node
        for _node in ast.walk(RELAY_TREE)
        if isinstance(_node, ast.Call)
        and isinstance(_node.func, ast.Attribute)
        and _node.func.attr == "write"
    ]
    targets = {
        ast.unparse(call.func.value)
        for call in writes
        if isinstance(call.func, ast.Attribute)
    }
    assert targets <= {
        "sink",
        "Path(arguments.marker)",
        "Path(arguments.tools_observed)",
    }, f"the relay writes through {sorted(targets)}"
    # Asserted over the constants the function actually reads rather than over
    # its text: the docstring says the words "description" and "annotation" in
    # order to promise it does not read them, and a text scan cannot tell a
    # promise from a breach.
    listing = _function(RELAY_TREE, "_listed_tools")
    read = {
        node.value
        for statement in listing.body[1:]  # its docstring is prose, not a key
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert read == {"result", "tools", "name"}, f"the relay reads {sorted(read)}"


def test_the_relay_withholds_only_after_the_server_answered() -> None:
    main = ast.get_source_segment(RELAY_TEXT, _function(RELAY_TREE, "main"))
    assert main is not None
    assert "child.stdout.readline()" in main, "the relay does not read the answer first"
    assert "_answers(line, armed" in main, (
        "the relay drops something other than an answer"
    )
    assert "--server-executable" in RELAY_TEXT, (
        "the relay cannot forward the installed executable"
    )


def test_ordinary_journey_calls_connect_to_the_installed_executable_directly() -> None:
    """The relay is reachable from exactly the two places the sections allow."""
    relayed = {
        node.name
        for node in ast.walk(HARNESS_TREE)
        if isinstance(node, ast.FunctionDef) and "relay_command(" in _source(node)
    }
    assert relayed == {"_ambiguous", "inventory_for", "relay_command"}, (
        f"the relay is used by {sorted(relayed)}"
    )
    server_command = _source(_method(HARNESS_TREE, "Host", "server_command"))
    assert "self.installed.mcp_server" in server_command, (
        "the default server is not the installed executable"
    )
    journey = _source(_function(HARNESS_TREE, "journey"))
    assert "server=" not in journey.replace("server=relay_command", ""), (
        "an ordinary journey step overrides the server it connects to"
    )


# --- the staging helper is a fixture, not a product surface --------------------


def test_the_staging_helper_declares_itself_a_qualification_fixture() -> None:
    assert STAGING.__doc__ is not None
    summary = STAGING.__doc__.lower()
    assert "qualification" in summary and "not a product surface" in summary, (
        "the staging helper does not say what it is"
    )
    assert "run-host-qualification.py" in STAGING.__doc__, (
        "the staging helper does not name its only caller"
    )


def test_the_staging_helper_stages_fixed_data_and_accepts_no_content() -> None:
    """There is no input by which a caller could stage anything of its own."""
    descriptor = STAGING.descriptor()
    assert descriptor == {
        "staged_source_ref": "stg-hostqual-1",
        "source_kind": "archive",
        "content_checksum": "sha256:" + "e" * 64,
        "content_length_bytes": 1024,
        "media_type": "application/zip",
    }, "the staged descriptor is not the fixed one"
    assert "source_version" not in descriptor, (
        "a descriptor carrying a version cannot resolve against a NULL column"
    )
    parser_source = STAGING_TEXT[STAGING_TEXT.index("def main(") :]
    accepted = {
        line.split('"')[1]
        for line in parser_source.splitlines()
        if "add_argument(" in line
    }
    assert accepted == {"--workspace", "--installed-prefix"}, (
        f"the staging helper accepts {sorted(accepted)}"
    )


def test_the_staging_helper_refuses_a_module_from_outside_the_installed_prefix() -> (
    None
):
    assert "_assert_installed" in STAGING_TEXT, "the installed-prefix proof is gone"
    with pytest.raises(SystemExit):
        STAGING._assert_installed(Path("/nonexistent-installed-prefix"))


# --- the evidence record: schema, redaction, and the guard ---------------------


def _every_schema(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if "type" in schema or "enum" in schema or "const" in schema:
            found.append(schema)
        for key, value in schema.items():
            if key in ("properties", "$defs"):
                for child in value.values():
                    found.extend(_every_schema(child))
            elif key == "items":
                found.extend(_every_schema(value))
    return found


def test_the_evidence_schema_is_closed_and_admits_no_free_text() -> None:
    """Redaction is a property of the schema, not of the code that fills it in."""
    Draft202012Validator.check_schema(HARNESS.EVIDENCE_SCHEMA)
    for schema in _every_schema(HARNESS.EVIDENCE_SCHEMA):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, (
                f"an object in the schema accepts unknown properties: {schema}"
            )
        if schema.get("type") == "string":
            assert {"enum", "const", "pattern"} & set(schema), (
                f"a string in the schema accepts free text: {schema}"
            )


def _record() -> dict[str, Any]:
    """One synthetic passing record: the green baseline every mutation starts from."""
    cases = []
    for case_id in HARNESS.CASE_IDS:
        for host in HARNESS.HOSTS:
            cases.append(
                {
                    "case_id": case_id,
                    "host": host,
                    "tools": ["evidence_capture"],
                    "disposition": "observed",
                    "identity_digest": "0123456789abcdef",
                    "count": 1,
                    "verdict": "pass",
                }
            )
    return {
        "format": HARNESS.EVIDENCE_FORMAT,
        "recorded_at": "2026-09-13T00:00:00Z",
        "commit": "a" * 40,
        "specification": {
            "requirements": "R004-v1.3",
            "sections": ["10", "13.B", "13.D", "13.F", "13.I"],
        },
        "environment": {
            "os_product": "macOS",
            "os_version": "26.5.2",
            "os_build": "25F84",
            "arch": "arm64",
            "approved": False,
        },
        "hosts": {
            host: {
                "version": HARNESS.REQUIRED_HOST_VERSIONS[host],
                "required_version": HARNESS.REQUIRED_HOST_VERSIONS[host],
                "approved": False,
                "executed": True,
            }
            for host in HARNESS.HOSTS
        },
        "installation": {
            "mode": "wheelhouse_no_index",
            "protocol_version": "2025-06-18",
            "sdk_pins": {"mcp": "2.0.0", "mcp-types": "2.0.0"},
            "distributions": dict.fromkeys(HARNESS.DISTRIBUTIONS, "0.1.0"),
            "wheelhouse": {"wheel_count": 33, "digest": "fedcba9876543210"},
            "installed_not_source": True,
            "index_used_during_install": False,
        },
        "inventories": {
            host: {
                "profile": "authoring",
                "observation": "host_init_event",
                "tools": list(HARNESS.AUTHORING_TOOLS),
                "count": 11,
            }
            for host in HARNESS.HOSTS
        },
        "cases": cases,
        "verdict": "pass",
    }


def test_the_synthetic_baseline_passes_the_guard() -> None:
    assert HARNESS.verify_record(_record(), commit="a" * 40) == [], (
        "the baseline this file mutates is not itself green"
    )


#: One mutation each, and a word that must appear in the finding it produces. The
#: baseline above is green, so every entry here is the proof that one specific
#: guard fires -- without them a single always-true assertion would pass this
#: whole section.
_MUTATIONS: tuple[tuple[str, Any, str], ...] = (
    ("verdict", "fail", "verdict"),
    ("hosts.claude-code.executed", False, "execute"),
    ("hosts.codex.version", "0.147.0", "unapproved"),
    ("hosts.codex.required_version", "0.1.0", "required version"),
    ("installation.sdk_pins.mcp", "2.2.0", "mcp"),
    ("environment.os_build", "25F99", "baseline"),
    ("inventories.codex.observation", "model_said_so", "schema"),
    ("inventories.claude-code.count", 10, "schema"),
    ("installation.installed_not_source", False, "schema"),
    ("installation.index_used_during_install", True, "schema"),
    ("recorded_at", "yesterday", "schema"),
    ("commit", "not-a-commit", "schema"),
    ("format", "omnivia.something-else.v1", "schema"),
)


@pytest.mark.parametrize(("pointer", "value", "expected"), _MUTATIONS)
def test_one_mutation_at_a_time_is_caught(
    pointer: str, value: Any, expected: str
) -> None:
    document = _record()
    target: Any = document
    *parents, leaf = pointer.split(".")
    for step in parents:
        target = target[step]
    target[leaf] = value
    findings = HARNESS.verify_record(document, commit="a" * 40)
    assert findings, f"mutating {pointer} produced no finding"
    assert any(expected in finding for finding in findings), (
        f"mutating {pointer} produced {findings}, which does not name {expected}"
    )


def test_an_inventory_that_is_not_the_authoring_eleven_is_caught() -> None:
    """Eleven names is not the claim; *these* eleven names is the claim."""
    short = [tool for tool in HARNESS.AUTHORING_TOOLS if tool != "memory_create"]
    for wrong in (
        short,  # ten of them
        sorted([*short, "memory_search"]),  # eleven, one of them twice
        list(reversed(HARNESS.AUTHORING_TOOLS)),  # all eleven, out of order
    ):
        document = _record()
        document["inventories"]["codex"]["tools"] = wrong
        findings = HARNESS.verify_record(document, commit="a" * 40)
        assert findings, f"{wrong} passed as the authoring inventory"


def test_a_missing_case_and_a_failing_case_are_both_caught() -> None:
    document = _record()
    document["cases"] = [
        case for case in document["cases"] if case["case_id"] != "capture_created"
    ]
    findings = HARNESS.verify_record(document, commit="a" * 40)
    assert any("capture_created" in finding for finding in findings), findings

    document = _record()
    document["cases"][3]["verdict"] = "fail"
    findings = HARNESS.verify_record(document, commit="a" * 40)
    assert any("did not pass" in finding for finding in findings), findings


def test_a_record_from_another_commit_is_caught() -> None:
    findings = HARNESS.verify_record(_record(), commit="b" * 40)
    assert any("different commit" in finding for finding in findings), findings


def test_an_absent_record_is_a_finding_rather_than_a_pass() -> None:
    assert HARNESS.verify_record(None) == ["no qualification record"]


def test_an_unredacted_value_is_caught_even_where_the_schema_would_allow_it() -> None:
    """The second opinion fires on shapes the schema is not the only defence against."""
    for leaked in (
        "/Users/someone/omnivia",
        "unix:///tmp/s.sock",
        "bearer abc",
        "someone@example.test",
        "a sentence with spaces",
    ):
        assert HARNESS._redaction_findings({"anything": leaked}), (
            f"{leaked!r} passed the redaction check"
        )
    for key in (
        "evidence_id",
        "job_id",
        "workspace_id",
        "prompt",
        "stdout",
        "endpoint",
    ):
        assert HARNESS._redaction_findings({key: "0123456789abcdef"}), (
            f"a {key} field passed the redaction check"
        )
    assert HARNESS._redaction_findings({"tools": ["grant_issue"]}) == [], (
        "the fixed excluded-tool inventory is mistaken for leaked grant material"
    )
    assert HARNESS._redaction_findings({"anything": "grant_issue"}), (
        "grant-shaped material outside the fixed inventory passed the redaction check"
    )


# --- the case register is the specification of the run -------------------------


def test_every_registered_case_is_decided_and_every_decision_is_registered() -> None:
    """`CASE_IDS` and the calls that fill it in are one list, checked both ways.

    A case that stopped being recorded would otherwise shrink the evidence
    silently, and a case recorded under a name the register does not hold would
    never be required of the next run.
    """
    recorded: set[str] = set()
    for node in ast.walk(HARNESS_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            recorded.add(node.args[0].value)
    assert recorded == set(HARNESS.CASE_IDS), (
        f"registered but never decided: {sorted(set(HARNESS.CASE_IDS) - recorded)}; "
        f"decided but not registered: {sorted(recorded - set(HARNESS.CASE_IDS))}"
    )


def test_the_register_covers_every_workflow_the_sections_require() -> None:
    """Each required behaviour has a case, named so a reader can find it."""
    required = {
        "13.B inventory": "inventory_eleven_tools",
        "13.B capture": "capture_created",
        "13.B immediate search": "capture_searchable",
        "13.B proposed only": "memory_proposed_only",
        "13.B default view": "memory_default_view_hidden",
        "13.B candidate view": "memory_candidate_view_visible",
        "13.B stable replay": "capture_replay_stable",
        "13.B changed input": "capture_changed_input_conflict",
        "13.B service outlives": "service_healthy_after_session",
        "13.D one job": "import_started_one_job",
        "13.D replay": "import_replay_same_job",
        "13.D conflict": "import_changed_input_conflict",
        "13.D terminal": "job_get_terminal",
        "13.D pagination": "job_events_paginated_stable",
        "13.D created evidence": "import_evidence_retrievable",
        "13.D no cancel or retry": "excluded_undispatchable",
        "13.D revocation blocks": "revocation_blocks_host",
        "13.D revocation is not cancellation": "revocation_preserves_committed_job",
        "13.D owner path": "owner_cli_observes_job",
        "13.F ambiguous outcome": "ambiguous_capture_recovered",
        "13.I stdout": "stdout_protocol_only",
        "13.I restart": "restart_read_observation",
    }
    for section, case_id in required.items():
        assert case_id in HARNESS.CASE_IDS, f"{section} has no case"


def test_revocation_is_never_described_as_cancellation() -> None:
    for text, name in ((HARNESS_TEXT, "the harness"), (STAGING_TEXT, "the helper")):
        lowered = text.lower()
        for offence in ("revocation cancels", "revoke cancels", "cancelling the job"):
            assert offence not in lowered, f"{name} calls revocation cancellation"


# --- writing the record --------------------------------------------------------


def test_a_failed_run_never_overwrites_a_passing_record(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    passing = _record()
    HARNESS.write_record(passing, path)
    assert json.loads(path.read_text(encoding="utf-8"))["verdict"] == "pass"

    failed = _record()
    failed["verdict"] = "fail"
    failed["cases"][0]["verdict"] = "fail"
    with pytest.raises(HARNESS.QualificationError):
        HARNESS.write_record(failed, path)
    assert json.loads(path.read_text(encoding="utf-8"))["verdict"] == "pass", (
        "a failed run replaced the passing record"
    )


def test_a_record_that_would_not_pass_its_own_guard_is_not_written(
    tmp_path: Path,
) -> None:
    path = tmp_path / "record.json"
    document = _record()
    document["hosts"]["codex"]["executed"] = False
    with pytest.raises(HARNESS.QualificationError):
        HARNESS.write_record(document, path)
    assert not path.exists(), "a record that fails its own guard was still written"


def test_a_failed_run_may_replace_a_failed_record(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    failed = _record()
    failed["verdict"] = "fail"
    failed["cases"][0]["verdict"] = "fail"
    HARNESS.write_record(failed, path)
    HARNESS.write_record(copy.deepcopy(failed), path)
    assert json.loads(path.read_text(encoding="utf-8"))["verdict"] == "fail"


# --- the committed record, when there is one -----------------------------------


def test_any_committed_record_passes_its_own_guard() -> None:
    """Vacuous while section 13.I is unevidenced, and decisive the moment it is.

    The commit is not checked here: a record is produced at the commit it
    qualified and stays valid for it, while this test runs at every later one.
    Binding a record to the commit under test is the traceability guard's job,
    because that is where a green `HOST` row is claimed.
    """
    path = HARNESS.DEFAULT_EVIDENCE_PATH
    if not path.is_file():
        pytest.skip("no real-host qualification record is committed")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert HARNESS.verify_record(document) == [], "the committed record is not green"
