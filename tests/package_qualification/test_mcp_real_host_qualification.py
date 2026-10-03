"""Offline guards for the real-host MCP qualification foundation."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import platform
import sqlite3
import stat
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run-mcp-real-host-qualification.py"
AUTHORING_SCRIPT = REPO_ROOT / "scripts" / "run-mcp-authoring-qualification.py"
BUILDER = REPO_ROOT / "scripts" / "build-standard-candidate.py"
SCHEMA = (
    REPO_ROOT
    / "docs"
    / "distribution"
    / "schemas"
    / "mcp-real-host-qualification-record-v1.schema.json"
)
AUTHORING_SCHEMA = SCHEMA.with_name("mcp-authoring-qualification-record-v1.schema.json")

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("omnivia_real_host_qualification", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


q = _load()
Reason = q.ReasonCode
REVISION = "f576ef3d66460933fbf8326bf35f91d2dd0abf8b"
START = datetime(2026, 10, 3, 9, 0, 0, tzinfo=UTC)
FINISH = START + timedelta(minutes=5)


def _schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SCHEMA.read_text(encoding="utf-8")))


def _code(error: pytest.ExceptionInfo[Any]) -> Any:
    assert isinstance(error.value, q.QualificationError)
    assert str(error.value) == error.value.code.value
    return error.value.code


# --- candidate fixtures ----------------------------------------------------


def _write_candidate(
    root: Path,
    *,
    dirty: bool = False,
    revision: str = REVISION,
    system: str = "darwin",
    sdk: tuple[str, str] = ("2.0.0", "2.0.0"),
) -> Path:
    (root / "metadata").mkdir(parents=True)
    (root / "wheels").mkdir()
    wheels: list[dict[str, Any]] = []
    for name in q.FIRST_PARTY:
        filename = f"{name.replace('-', '_')}-0.1.0-py3-none-any.whl"
        content = f"wheel bytes for {name}".encode()
        (root / "wheels" / filename).write_bytes(content)
        wheels.append(
            {
                "name": name,
                "first_party": True,
                "path": f"wheels/{filename}",
                "sha256": hashlib.sha256(content).hexdigest(),
                "version": "0.1.0",
            }
        )
    for name, version in (("mcp", sdk[0]), ("mcp-types", sdk[1]), ("anyio", "4.14.2")):
        wheels.append(
            {
                "name": name,
                "first_party": False,
                "path": f"wheels/{name.replace('-', '_')}-{version}-py3-none-any.whl",
                "sha256": "0" * 64,
                "version": version,
            }
        )
    (root / "metadata" / "release-manifest.json").write_text(
        json.dumps(
            {
                "format": q.MANIFEST_FORMAT,
                "source_revision": revision,
                "wheels": wheels,
            }
        ),
        encoding="utf-8",
    )
    (root / "metadata" / "build-provenance.json").write_text(
        json.dumps(
            {
                "format": q.PROVENANCE_FORMAT,
                "source": {"dirty": dirty, "revision": revision},
                "host": {"system": system, "machine": "arm64"},
                "dependency_resolution": {"exact_closure_verified": True},
            }
        ),
        encoding="utf-8",
    )
    return root


def _edit_json(path: Path, edit: Any) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def _manifest(root: Path) -> Path:
    return root / "metadata" / "release-manifest.json"


def _provenance(root: Path) -> Path:
    return root / "metadata" / "build-provenance.json"


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    return _write_candidate(tmp_path / "candidate")


# --- candidate, platform and pin validation --------------------------------


def test_a_clean_exact_candidate_is_accepted(candidate: Path) -> None:
    loaded = q.load_candidate(candidate)
    assert loaded.revision == REVISION
    assert list(loaded.wheels) == list(q.FIRST_PARTY)
    for digest in loaded.wheels.values():
        assert len(digest) == 64


def test_a_dirty_candidate_is_refused(tmp_path: Path) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(_write_candidate(tmp_path / "c", dirty=True))
    assert _code(error) is Reason.CANDIDATE_DIRTY


@pytest.mark.parametrize("revision", ["", "f576ef3d", REVISION.upper(), REVISION + "0", 7])
def test_a_non_exact_revision_is_refused(candidate: Path, revision: object) -> None:
    _edit_json(_provenance(candidate), lambda d: d["source"].update(revision=revision))
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


@pytest.mark.parametrize("dirty", ["false", 0, None])
def test_a_non_boolean_dirty_flag_is_refused(candidate: Path, dirty: object) -> None:
    _edit_json(_provenance(candidate), lambda d: d["source"].update(dirty=dirty))
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


def test_a_manifest_for_another_revision_is_refused(candidate: Path) -> None:
    _edit_json(_manifest(candidate), lambda d: d.update(source_revision="a" * 40))
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


def test_an_unverified_dependency_closure_is_refused(candidate: Path) -> None:
    _edit_json(
        _provenance(candidate),
        lambda d: d["dependency_resolution"].update(exact_closure_verified=False),
    )
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


@pytest.mark.parametrize("name", ["omnivia-core", "omnivia-core-mcp"])
def test_a_changed_wheel_is_a_digest_mismatch(candidate: Path, name: str) -> None:
    wheel = next(candidate.joinpath("wheels").glob(f"{name.replace('-', '_')}-0*"))
    wheel.write_bytes(wheel.read_bytes() + b"x")
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.WHEEL_DIGEST_MISMATCH


def test_a_malformed_manifest_digest_is_a_digest_mismatch(candidate: Path) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["wheels"][0]["sha256"] = document["wheels"][0]["sha256"].upper()

    _edit_json(_manifest(candidate), edit)
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.WHEEL_DIGEST_MISMATCH


def test_a_missing_or_symlinked_first_party_wheel_is_refused(candidate: Path) -> None:
    wheel = next(candidate.joinpath("wheels").glob("omnivia_core_cli-*"))
    target = candidate / "elsewhere.whl"
    target.write_bytes(wheel.read_bytes())
    wheel.unlink()
    with pytest.raises(q.QualificationError) as missing:
        q.load_candidate(candidate)
    assert _code(missing) is Reason.CANDIDATE_INVALID
    if os.name != "nt":
        wheel.symlink_to(target)
        with pytest.raises(q.QualificationError) as linked:
            q.load_candidate(candidate)
        assert _code(linked) is Reason.CANDIDATE_INVALID


@pytest.mark.parametrize("path", ["../x.whl", "wheels/../x.whl", "/abs/x.whl", "x.whl", "wheels/a/b.whl"])
def test_a_wheel_path_outside_the_wheelhouse_is_refused(candidate: Path, path: str) -> None:
    _edit_json(_manifest(candidate), lambda d: d["wheels"][0].update(path=path))
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


def test_the_first_party_set_must_be_exact(candidate: Path) -> None:
    def drop(document: dict[str, Any]) -> None:
        document["wheels"].pop(0)

    def duplicate(document: dict[str, Any]) -> None:
        document["wheels"].append(deepcopy(document["wheels"][0]))

    def stray(document: dict[str, Any]) -> None:
        document["wheels"].append(
            {"name": "omnivia-extra", "first_party": True, "path": "wheels/x.whl", "sha256": "0" * 64}
        )

    def unflagged(document: dict[str, Any]) -> None:
        document["wheels"][0]["first_party"] = False

    for edit in (drop, duplicate, stray, unflagged):
        scratch = _write_candidate(candidate.parent / f"scratch-{edit.__name__}")
        _edit_json(_manifest(scratch), edit)
        with pytest.raises(q.QualificationError) as error:
            q.load_candidate(scratch)
        assert _code(error) is Reason.CANDIDATE_INVALID


@pytest.mark.parametrize("sdk", [("2.0.1", "2.0.0"), ("2.0.0", "1.9.0")])
def test_sdk_pins_must_match_exactly(tmp_path: Path, sdk: tuple[str, str]) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(_write_candidate(tmp_path / "c", sdk=sdk))
    assert _code(error) is Reason.SDK_PIN_MISMATCH


@pytest.mark.parametrize(
    "wheels",
    [
        [],
        [{"name": "mcp", "version": "2.0.0"}],
        [{"name": "mcp", "version": "2.0.0"}, {"name": "mcp-types", "version": "2.0.0"}] * 2,
        [{"name": "mcp", "version": "2.0.0"}, {"name": "mcp_types", "version": "2.0.0.post1"}],
    ],
)
def test_sdk_pins_require_exactly_one_entry_each(wheels: list[dict[str, str]]) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.require_sdk_pins(wheels)
    assert _code(error) is Reason.SDK_PIN_MISMATCH


def test_normalized_sdk_names_are_accepted() -> None:
    q.require_sdk_pins(
        [{"name": "MCP", "version": "2.0.0"}, {"name": "mcp_types", "version": "2.0.0"}]
    )


def test_only_darwin_arm64_is_supported(tmp_path: Path) -> None:
    q.require_platform("darwin", "arm64")
    for system, machine in (("linux", "arm64"), ("darwin", "x86_64"), ("windows", "amd64"), (None, None)):
        with pytest.raises(q.QualificationError) as error:
            q.require_platform(system, machine)
        assert _code(error) is Reason.PLATFORM_UNSUPPORTED
    with pytest.raises(q.QualificationError) as built:
        q.load_candidate(_write_candidate(tmp_path / "c", system="linux"))
    assert _code(built) is Reason.PLATFORM_UNSUPPORTED


def test_unreadable_candidate_documents_are_refused(tmp_path: Path) -> None:
    for root in (tmp_path / "absent", tmp_path):
        with pytest.raises(q.QualificationError) as error:
            q.load_candidate(root)
        assert _code(error) is Reason.CANDIDATE_INVALID


# --- inventories -----------------------------------------------------------


def _literal(path: Path, name: str) -> Any:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            assert node.value is not None
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def test_the_inventories_are_the_exact_stable_thirteen_and_eighteen() -> None:
    authoring = _literal(AUTHORING_SCRIPT, "AUTHORING_TOOLS")
    assert q.AUTHORING_TOOLS == authoring
    assert len(q.AUTHORING_TOOLS) == q.AUTHORING_TOOL_COUNT == 18
    assert len(set(q.AUTHORING_TOOLS)) == 18
    assert len(q.RESTRICTED_TOOLS) == q.RESTRICTED_TOOL_COUNT == 13
    assert q.RESTRICTED_TOOLS == q.AUTHORING_TOOLS[:13]
    assert sorted(q.RESTRICTED_TOOLS) == _literal(BUILDER, "HOST_TOOLS")
    assert q.AUTHORING_TOOLS[13:] == (
        "memory_create",
        "evidence_capture",
        "import_start",
        "job_get",
        "job_events",
    )


def test_the_authoring_inventory_matches_the_installed_authoring_schema() -> None:
    schema = json.loads(AUTHORING_SCHEMA.read_text(encoding="utf-8"))
    pinned = [item["const"] for item in schema["properties"]["tools"]["prefixItems"]]
    assert tuple(pinned) == q.AUTHORING_TOOLS


def test_no_omnivia_package_is_imported() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not {name for name in imported if name.startswith("omnivia")}


# --- host commands and configuration ---------------------------------------

ENTRY = {"command": "/venv/bin/omnivia-core-mcp", "args": ["--config", "/state/mcp.json"]}


def test_the_stdio_entry_and_claude_config_have_the_native_shape() -> None:
    entry = q.mcp_server_entry(Path("/venv/bin/omnivia-core-mcp"), Path("/state/mcp.json"))
    assert entry == ENTRY
    assert q.claude_mcp_config(entry) == {"mcpServers": {"omnivia-core": ENTRY}}


def test_the_claude_command_is_strict_isolated_and_tool_limited() -> None:
    tools = q.AUTHORING_TOOLS[:2]
    command = q.claude_command(
        Path("/bin/claude"), mcp_config=Path("/c/mcp.json"), prompt="PROMPT", tools=tools
    )
    assert command == [
        "/bin/claude",
        "-p",
        "PROMPT",
        "--mcp-config",
        "/c/mcp.json",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--allowedTools",
        "mcp__omnivia-core__workspace_inspect,mcp__omnivia-core__evidence_search",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "project",
    ]


def test_the_codex_config_and_commands_have_the_native_shape() -> None:
    text = q.codex_config_toml(ENTRY)
    assert tomllib.loads(text) == {
        "approval_policy": "never",
        "mcp_servers": {"omnivia-core": ENTRY},
    }
    awkward = {"command": 'C:\\bin\\"mcp"', "args": ["--config", "/s p/é\n"]}
    assert tomllib.loads(q.codex_config_toml(awkward)) == {
        "approval_policy": "never",
        "mcp_servers": {"omnivia-core": awkward},
    }
    assert q.codex_mcp_add_command(Path("/bin/codex"), ENTRY) == [
        "/bin/codex",
        "mcp",
        "add",
        "omnivia-core",
        "--",
        "/venv/bin/omnivia-core-mcp",
        "--config",
        "/state/mcp.json",
    ]
    assert q.codex_command(
        Path("/bin/codex"), workspace=Path("/w"), prompt="PROMPT", last_message=Path("/o/last")
    ) == [
        "/bin/codex",
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--config",
        'approval_policy="never"',
        "--json",
        "--output-last-message",
        "/o/last",
        "--cd",
        "/w",
        "PROMPT",
    ]


def test_commands_use_only_their_explicit_inputs() -> None:
    first = q.claude_command(Path("/a"), mcp_config=Path("/b"), prompt="p", tools=("t",))
    assert first == q.claude_command(Path("/a"), mcp_config=Path("/b"), prompt="p", tools=("t",))
    assert str(Path.home()) not in " ".join(first)


def test_the_layout_redirects_each_host_into_the_temporary_root(tmp_path: Path) -> None:
    claude = q.host_layout(tmp_path, "claude-code")
    codex = q.host_layout(tmp_path, "codex-cli")
    assert claude.environment() == {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude"),
    }
    assert codex.environment() == {
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "home" / ".codex"),
    }
    assert claude.auth_destination == tmp_path / "home" / ".claude" / ".credentials.json"
    assert codex.auth_destination == tmp_path / "home" / ".codex" / "auth.json"
    for layout in (claude, codex):
        for path in (layout.home, layout.config_dir, layout.workspace, layout.auth_destination):
            assert tmp_path in path.parents
    with pytest.raises(q.QualificationError) as error:
        q.host_layout(tmp_path, "other")
    assert _code(error) is Reason.RECORD_INVALID


@posix_only
@pytest.mark.parametrize("host", ["claude-code", "codex-cli"])
def test_the_layout_is_created_private(tmp_path: Path, host: str) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    layout = q.host_layout(root, host)
    q.create_layout(layout)
    for path in (layout.home, layout.config_dir, layout.workspace):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


# --- authentication copy ---------------------------------------------------

SECRET = b"\x00\xff not json, not utf-8 \x80 sk-ant-DO-NOT-LEAK"


def _auth(tmp_path: Path, mode: int = 0o600, name: str = "source-auth") -> Path:
    source = tmp_path / name
    source.write_bytes(SECRET)
    source.chmod(mode)
    return source


@posix_only
def test_the_auth_file_is_copied_by_bytes_with_private_modes(tmp_path: Path) -> None:
    source = _auth(tmp_path)
    destination = tmp_path / "root" / "home" / ".claude" / ".credentials.json"
    assert q.copy_auth_file(source, destination) is None
    assert destination.read_bytes() == SECRET
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    for parent in (destination.parent, destination.parent.parent, destination.parent.parent.parent):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert source.read_bytes() == SECRET


@posix_only
def test_the_auth_copy_never_parses_or_discloses_its_source(tmp_path: Path) -> None:
    source = _auth(tmp_path, 0o600)
    destination = tmp_path / "out" / "auth"
    assert q.copy_auth_file(source, destination) is None
    # A refusal text carries only the stable code, never a path or content.
    with pytest.raises(q.QualificationError) as error:
        q.copy_auth_file(source, destination)
    text = repr(error.value) + str(error.value)
    assert str(source) not in text and "sk-ant" not in text
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE


@posix_only
def test_unsafe_auth_sources_are_refused_without_a_destination(tmp_path: Path) -> None:
    unreadable = _auth(tmp_path, 0o000, "unreadable")
    directory = tmp_path / "dir"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(_auth(tmp_path, 0o600, "target"))
    cases = [tmp_path / "absent", directory, link]
    if os.geteuid() != 0:
        cases.append(unreadable)
    for index, source in enumerate(cases):
        destination = tmp_path / f"dest-{index}" / "auth"
        with pytest.raises(q.QualificationError) as error:
            q.copy_auth_file(source, destination)
        assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
        assert not destination.exists()
        with pytest.raises(q.QualificationError):
            q.require_auth_file(source)


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660, 0o666, 0o610, 0o601])
def test_a_source_with_group_or_world_bits_is_refused(tmp_path: Path, mode: int) -> None:
    source = _auth(tmp_path, mode)
    destination = tmp_path / "dest" / "auth"
    with pytest.raises(q.QualificationError) as error:
        q.copy_auth_file(source, destination)
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    assert not destination.exists()
    with pytest.raises(q.QualificationError):
        q.require_auth_file(source)
    for accepted in (0o600, 0o400):
        source.chmod(accepted)
        q.require_auth_file(source)


@posix_only
def test_an_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "auth"
    destination.write_bytes(b"prior")
    with pytest.raises(q.QualificationError) as error:
        q.copy_auth_file(_auth(tmp_path, 0o600), destination)
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    assert destination.read_bytes() == b"prior"


# --- gate ledger -----------------------------------------------------------

ALL_CHECKS = [(gate, check) for gate, checks in q.GATES.items() for check in checks]
INDEPENDENT = q.Evidence.INDEPENDENT


def _passed_ledger() -> Any:
    ledger = q.GateLedger()
    for gate, check in ALL_CHECKS:
        ledger.record(gate, check, True, source=INDEPENDENT)
    return ledger


def test_gates_cover_i1_through_i8_and_start_closed() -> None:
    assert list(q.GATES) == [f"i{n}" for n in range(1, 9)]
    ledger = q.GateLedger()
    assert not ledger.all_passed()
    assert all(not ledger.gate_passed(gate) for gate in q.GATES)
    assert all(
        ledger.status(gate, check) is q.GateStatus.PENDING for gate, check in ALL_CHECKS
    )
    assert all(not value for checks in ledger.as_record().values() for value in checks.values())


def test_independent_observations_open_each_gate_in_turn() -> None:
    ledger = q.GateLedger()
    for gate, checks in q.GATES.items():
        for check in checks:
            assert not ledger.gate_passed(gate)
            ledger.record(gate, check, True, source=INDEPENDENT)
        assert ledger.gate_passed(gate)
    assert ledger.all_passed()
    assert all(all(checks.values()) for checks in ledger.as_record().values())


@pytest.mark.parametrize(("gate", "check"), ALL_CHECKS)
def test_one_unobserved_or_failed_check_keeps_the_ledger_closed(gate: str, check: str) -> None:
    ledger = q.GateLedger()
    for other_gate, other_check in ALL_CHECKS:
        if (other_gate, other_check) != (gate, check):
            ledger.record(other_gate, other_check, True, source=INDEPENDENT)
    assert not ledger.all_passed() and not ledger.gate_passed(gate)
    ledger.record(gate, check, False, source=INDEPENDENT)
    assert ledger.status(gate, check) is q.GateStatus.FAILED
    assert not ledger.all_passed()
    assert ledger.as_record()[gate][check] is False


def test_a_failed_observation_is_sticky_and_a_pass_can_be_downgraded() -> None:
    ledger = q.GateLedger()
    ledger.record("i8", "core_healthy", False, source=INDEPENDENT)
    ledger.record("i8", "core_healthy", True, source=INDEPENDENT)
    assert ledger.status("i8", "core_healthy") is q.GateStatus.FAILED
    ledger.record("i1", "candidate_installed", True, source=INDEPENDENT)
    ledger.record("i1", "candidate_installed", True, source=INDEPENDENT)
    assert ledger.status("i1", "candidate_installed") is q.GateStatus.PASSED
    ledger.record("i1", "candidate_installed", False, source=INDEPENDENT)
    assert ledger.status("i1", "candidate_installed") is q.GateStatus.FAILED


@pytest.mark.parametrize(("gate", "check"), ALL_CHECKS)
def test_model_evidence_can_never_set_a_gate(gate: str, check: str) -> None:
    ledger = q.GateLedger()
    for observed in (True, False):
        with pytest.raises(q.QualificationError) as error:
            ledger.record(gate, check, observed, source=q.Evidence.MODEL)
        assert _code(error) is Reason.MODEL_EVIDENCE_REJECTED
    assert ledger.status(gate, check) is q.GateStatus.PENDING


@pytest.mark.parametrize("claim", ["PASS", "OMNIVIA_OK", "true", 1, None, {"ok": True}])
def test_markers_and_non_boolean_claims_are_never_evidence(claim: object) -> None:
    ledger = q.GateLedger()
    with pytest.raises(q.QualificationError) as error:
        ledger.record("i3", "initialize_verified", claim, source=INDEPENDENT)
    assert _code(error) is Reason.MODEL_EVIDENCE_REJECTED
    assert ledger.status("i3", "initialize_verified") is q.GateStatus.PENDING


def test_unknown_gates_and_checks_are_refused() -> None:
    ledger = q.GateLedger()
    for gate, check in (("i9", "x"), ("i1", "x"), ("i2", "candidate_installed")):
        with pytest.raises(q.QualificationError) as error:
            ledger.record(gate, check, True, source=INDEPENDENT)
        assert _code(error) is Reason.RECORD_INVALID


def test_record_construction_accepts_no_model_text() -> None:
    parameters = set(inspect.signature(q.build_record).parameters)
    assert parameters == {
        "candidate",
        "os_identity",
        "host",
        "ledger",
        "started_at",
        "finished_at",
        "reason",
    }
    assert not {"output", "marker", "text", "prompt", "transcript", "response"} & parameters


# --- record construction ---------------------------------------------------

def _inputs(ledger: Any = None, host_version: str = "2.1.288", host: str = "claude-code") -> dict[str, Any]:
    return {
        "candidate": q.Candidate(REVISION, {name: f"{n:064x}" for n, name in enumerate(q.FIRST_PARTY, 1)}),
        "os_identity": q.OsIdentity("27.0", "26A428", "arm64"),
        "host": q.HostIdentity(host, host_version),
        "ledger": ledger if ledger is not None else _passed_ledger(),
        "started_at": START,
        "finished_at": FINISH,
    }


def _pass_record(**changes: Any) -> dict[str, Any]:
    return cast(dict[str, Any], q.build_record(**{**_inputs(), **changes}))


def test_the_schema_is_a_valid_closed_schema_matching_the_script() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    gates = schema["properties"]["gates"]["properties"]
    assert {gate: tuple(body["properties"]) for gate, body in gates.items()} == q.GATES
    assert schema["properties"]["reason_code"]["enum"] == [r.value for r in Reason]
    assert schema["properties"]["format"]["const"] == q.RECORD_FORMAT


@pytest.mark.parametrize(("host", "version"), sorted(q.HOST_VERSIONS.items()))
def test_a_passing_record_is_built_and_validates(host: str, version: str) -> None:
    record = q.build_record(**_inputs(host=host, host_version=version))
    q.validate_record(record, _schema())
    assert record["verdict"] == "pass" and record["reason_code"] == "none"
    assert record["source"] == {"revision": REVISION, "clean": True}
    assert record["profiles"]["restricted"]["tool_count"] == 13
    assert record["profiles"]["authoring"]["tool_count"] == 18
    assert record["profiles"]["authoring"]["tools"] == list(q.AUTHORING_TOOLS)
    assert record["sdk_versions"] == {"mcp": "2.0.0", "mcp-types": "2.0.0"}
    assert record["started_at"] == "2026-10-03T09:00:00Z"
    assert record["finished_at"] == "2026-10-03T09:05:00Z"
    assert list(record["wheels"]) == list(q.FIRST_PARTY)


def test_a_record_carries_no_run_material(tmp_path: Path) -> None:
    text = json.dumps(_pass_record())
    assert str(tmp_path) not in text and str(Path.home()) not in text


def test_an_unattempted_run_builds_a_valid_failure_record() -> None:
    record = q.build_record(**_inputs(ledger=q.GateLedger()), reason=Reason.LIVE_RUNNER_UNAVAILABLE)
    q.validate_record(record, _schema())
    assert record["verdict"] == "fail"
    assert record["reason_code"] == "live_runner_unavailable"
    assert not any(v for checks in record["gates"].values() for v in checks.values())


def test_a_preflight_failure_builds_a_minimal_closed_record() -> None:
    record = q.build_minimal_failure_record(
        Reason.AUTHENTICATION_UNAVAILABLE,
        started_at=START,
        finished_at=FINISH,
    )
    assert record == {
        "format": q.RECORD_FORMAT,
        "verdict": "fail",
        "reason_code": "authentication_unavailable",
        "started_at": "2026-10-03T09:00:00Z",
        "finished_at": "2026-10-03T09:05:00Z",
    }
    q.validate_record(record, _schema())


def test_the_verdict_is_derived_never_claimed() -> None:
    ledger = _passed_ledger()
    ledger.record("i6", "same_key_replayed", False, source=INDEPENDENT)
    record = q.build_record(**_inputs(ledger=ledger))
    assert (record["verdict"], record["reason_code"]) == ("fail", "gate_failed")
    q.validate_record(record, _schema())
    incomplete = q.GateLedger()
    for gate, check in ALL_CHECKS[:-1]:
        incomplete.record(gate, check, True, source=INDEPENDENT)
    record = q.build_record(**_inputs(ledger=incomplete))
    assert record["reason_code"] == "gate_failed"
    # An explicit reason wins, and "none" is not a reason.
    explicit = q.build_record(**_inputs(), reason=Reason.HOST_TIMEOUT)
    assert (explicit["verdict"], explicit["reason_code"]) == ("fail", "host_timeout")
    assert q.build_record(**_inputs(), reason=Reason.NONE)["verdict"] == "pass"
    q.validate_record(explicit, _schema())


def test_an_unpinned_host_version_fails_rather_than_passes() -> None:
    record = q.build_record(**_inputs(host_version="2.1.286"))
    assert (record["verdict"], record["reason_code"]) == ("fail", "host_version_unsupported")
    assert record["host"]["version"] == "2.1.286"
    q.validate_record(record, _schema())


def test_record_inputs_are_checked() -> None:
    for changes in (
        {"host": q.HostIdentity("gemini-cli", "1.0.0")},
        {"finished_at": START - timedelta(seconds=1)},
        {"started_at": datetime(2026, 10, 3, 9, 0, 0)},  # noqa: DTZ001
    ):
        with pytest.raises(q.QualificationError) as error:
            _pass_record(**changes)
        assert _code(error) is Reason.RECORD_INVALID


def test_timestamps_are_normalized_to_utc() -> None:
    zone = timezone(timedelta(hours=10))
    assert q.utc_timestamp(datetime(2026, 10, 3, 19, 0, 0, tzinfo=zone)) == "2026-10-03T09:00:00Z"


# --- schema: positive, negative, false claims ------------------------------


def _object_paths(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(value, dict):
        yield path
        for key, child in value.items():
            yield from _object_paths(child, (*path, key))


def _at(document: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        document = document[key]
    return document


FORBIDDEN = [
    "notes",
    "error",
    "message",
    "stderr",
    "stdout",
    "prompt",
    "transcript",
    "model_response",
    "token",
    "bearer",
    "grant",
    "credential",
    "auth_file",
    "path",
    "workspace_id",
    "principal_id",
    "job_id",
    "request_id",
    "endpoint",
    "pid",
    "content",
    "provider",
    "model",
]


def test_every_object_level_rejects_unknown_and_forbidden_fields() -> None:
    record = _pass_record()
    paths = list(_object_paths(record))
    assert () in paths and ("gates", "i6") in paths and ("profiles", "authoring") in paths
    assert len(paths) >= 17
    validator = jsonschema.Draft202012Validator(_schema())
    assert validator.is_valid(record)
    for path in paths:
        for name in ["unexpected", *FORBIDDEN]:
            mutated = deepcopy(record)
            _at(mutated, path)[name] = "x"
            assert not validator.is_valid(mutated), (path, name)


def test_every_field_is_required() -> None:
    record = _pass_record()
    validator = jsonschema.Draft202012Validator(_schema())
    for path in _object_paths(record):
        for name in list(_at(record, path)):
            mutated = deepcopy(record)
            del _at(mutated, path)[name]
            assert not validator.is_valid(mutated), (path, name)


def _mutate(change: Any) -> dict[str, Any]:
    record = _pass_record()
    change(record)
    return record


FALSE_CLAIMS = {
    "gate_false_on_pass": lambda r: r["gates"]["i7"].update(stdout_protocol_only=False),
    "gate_not_boolean": lambda r: r["gates"]["i1"].update(candidate_installed="PASS"),
    "gate_one": lambda r: r["gates"]["i1"].update(candidate_installed=1),
    "pass_with_reason": lambda r: r.update(reason_code="gate_failed"),
    "fail_without_reason": lambda r: r.update(verdict="fail"),
    "verdict_unknown": lambda r: r.update(verdict="passed"),
    "reason_unknown": lambda r: r.update(verdict="fail", reason_code="because it broke"),
    "unclean_source": lambda r: r["source"].update(clean=False),
    "short_revision": lambda r: r["source"].update(revision=REVISION[:12]),
    "upper_digest": lambda r: r["wheels"].update({"omnivia-core": "A" * 64}),
    "extra_wheel": lambda r: r["wheels"].update({"anyio": "0" * 64}),
    "wrong_os": lambda r: r["os"].update(product="Windows"),
    "x86": lambda r: r["os"].update(architecture="x86_64"),
    "bad_build": lambda r: r["os"].update(build="/Users/me"),
    "pass_old_claude": lambda r: r["host"].update(version="2.1.286"),
    "pass_old_codex": lambda r: r["host"].update(name="codex-cli", version="0.145.0"),
    "unknown_host": lambda r: r["host"].update(name="gemini-cli"),
    "mcp_pin": lambda r: r["sdk_versions"].update(mcp="2.0.1"),
    "types_pin": lambda r: r["sdk_versions"].update({"mcp-types": "1.0.0"}),
    "restricted_count": lambda r: r["profiles"]["restricted"].update(tool_count=12),
    "authoring_count": lambda r: r["profiles"]["authoring"].update(tool_count=19),
    "restricted_short": lambda r: r["profiles"]["restricted"]["tools"].pop(),
    "authoring_extra": lambda r: r["profiles"]["authoring"]["tools"].append("shell"),
    "authoring_swapped": lambda r: r["profiles"]["authoring"]["tools"].reverse(),
    "restricted_renamed": lambda r: r["profiles"]["restricted"]["tools"].__setitem__(0, "x"),
    "local_timestamp": lambda r: r.update(started_at="2026-10-03T09:00:00+01:00"),
    "date_only": lambda r: r.update(finished_at="2026-10-03"),
    "wrong_format": lambda r: r.update(format="omnivia.mcp-authoring-qualification.v1"),
    "list_record": lambda r: r.clear(),
}


@pytest.mark.parametrize("name", sorted(FALSE_CLAIMS))
def test_false_and_malformed_claims_are_rejected(name: str) -> None:
    mutated = _mutate(FALSE_CLAIMS[name])
    assert not jsonschema.Draft202012Validator(_schema()).is_valid(mutated)
    with pytest.raises(q.QualificationError) as error:
        q.validate_record(mutated, _schema())
    assert _code(error) is Reason.RECORD_INVALID


def test_a_failure_may_carry_partial_gates_but_never_a_pass_marker() -> None:
    record = q.build_record(**_inputs(ledger=q.GateLedger()), reason=Reason.HOST_TIMEOUT)
    validator = jsonschema.Draft202012Validator(_schema())
    assert validator.is_valid(record)
    record["verdict"] = "pass"
    assert not validator.is_valid(record)
    for non_object in cast(tuple[object, ...], (None, [], "pass", 1)):
        assert not validator.is_valid(non_object)


# --- atomic output ---------------------------------------------------------


def test_the_record_is_written_atomically_and_exactly(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "record.json"
    record = _pass_record()
    q.write_record(record, _schema(), output)
    assert json.loads(output.read_text(encoding="utf-8")) == record
    assert output.read_text(encoding="utf-8").endswith("}\n")
    assert [path.name for path in output.parent.iterdir()] == ["record.json"]


def test_an_invalid_record_writes_nothing(tmp_path: Path) -> None:
    output = tmp_path / "record.json"
    output.write_text("prior\n", encoding="utf-8")
    with pytest.raises(q.QualificationError) as error:
        q.write_record(_mutate(FALSE_CLAIMS["gate_false_on_pass"]), _schema(), output)
    assert _code(error) is Reason.RECORD_INVALID
    assert output.read_text(encoding="utf-8") == "prior\n"
    assert [path.name for path in tmp_path.iterdir()] == ["record.json"]


def test_an_interrupted_publish_leaves_the_prior_file_and_no_temporary(tmp_path: Path) -> None:
    output = tmp_path / "record.json"
    output.write_text("prior\n", encoding="utf-8")

    def explode(_source: Path, _target: Path) -> None:
        raise OSError("disk detail that must not escape")

    with pytest.raises(q.QualificationError) as error:
        q.write_record(_pass_record(), _schema(), output, replace=explode)
    assert _code(error) is Reason.RECORD_INVALID
    assert "disk detail" not in str(error.value)
    assert output.read_text(encoding="utf-8") == "prior\n"
    assert [path.name for path in tmp_path.iterdir()] == ["record.json"]


def test_a_new_record_replaces_the_old_one_whole(tmp_path: Path) -> None:
    output = tmp_path / "record.json"
    q.write_record(_pass_record(), _schema(), output)
    failed = q.build_record(**_inputs(ledger=q.GateLedger()), reason=Reason.HOST_TIMEOUT)
    q.write_record(failed, _schema(), output)
    assert json.loads(output.read_text(encoding="utf-8"))["reason_code"] == "host_timeout"


def test_schema_loading_is_closed_to_bad_input(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    for text in ("not json", "[]", '{"type": 12}'):
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(q.QualificationError) as error:
            q.load_schema(bad)
        assert _code(error) is Reason.RECORD_INVALID
    with pytest.raises(q.QualificationError):
        q.load_schema(tmp_path / "absent.json")


# --- installed-candidate bootstrap ----------------------------------------


def _fake_candidate_venv(path: Path) -> None:
    scripts = path / "bin"
    scripts.mkdir(parents=True)
    for name in ("python", *q.CONSOLE_SCRIPTS):
        entry = scripts / name
        entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        entry.chmod(0o755)


def test_candidate_bootstrap_is_offline_exact_and_non_editable(
    candidate: Path, tmp_path: Path
) -> None:
    loaded = q.load_candidate(candidate)
    calls: list[tuple[list[str], dict[str, str], Path, float]] = []

    def run(
        argv: Any, env: Any, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(argv), dict(env), cwd, timeout))
        if "pip" in argv:
            return subprocess.CompletedProcess(argv, 0, b"installed", b"")
        report = {
            "versions": q.SDK_PINS,
            "inside": True,
            "editable": False,
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(report).encode(), b"")

    installed = q.bootstrap_candidate(
        candidate, loaded, tmp_path / "runtime", run=run, create_venv=_fake_candidate_venv
    )
    assert installed.python == installed.venv / "bin" / "python"
    assert installed.service.name == "omnivia-core-service"
    assert installed.cli.name == "omnivia"
    assert installed.mcp.name == "omnivia-core-mcp"
    install = calls[0][0]
    assert install[:5] == [
        str(installed.python), "-I", "-m", "p" + "ip", "in" + "stall"
    ]
    assert install[5:7] == ["--isolated", "--no-index"]
    assert {"--isolated", "--no-index", "--only-binary=:all:", "--no-input"} <= set(
        install
    )
    assert [Path(value).name for value in install if value.endswith(".whl")] == [
        q._verified_wheels(json.loads(_manifest(candidate).read_text())["wheels"], candidate)[
            name
        ][0].name
        for name in q.FIRST_PARTY
    ]
    assert calls[0][1] == {
        "PATH": q.SYSTEM_PATH,
        "HOME": str(tmp_path / "runtime" / "bootstrap-home"),
        "PYTHONNOUSERSITE": "1",
    }
    assert calls[1][0][1:4] == ["-I", "-c", q._PROBE]


@pytest.mark.parametrize(
    ("install_status", "probe", "reason"),
    [
        (1, None, Reason.INSTALL_FAILED),
        (0, {"versions": {"mcp": "2.0.1", "mcp-types": "2.0.0"}, "inside": True, "editable": False}, Reason.SDK_PIN_MISMATCH),
        (0, {"versions": q.SDK_PINS, "inside": False, "editable": False}, Reason.INSTALL_FAILED),
        (0, {"versions": q.SDK_PINS, "inside": True, "editable": True}, Reason.INSTALL_FAILED),
    ],
)
def test_candidate_bootstrap_refuses_install_or_probe_drift(
    candidate: Path,
    tmp_path: Path,
    install_status: int,
    probe: dict[str, Any] | None,
    reason: Any,
) -> None:
    calls = 0

    def run(
        argv: Any, _env: Any, _cwd: Path, _timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(argv, install_status, b"", b"secret detail")
        return subprocess.CompletedProcess(argv, 0, json.dumps(probe).encode(), b"")

    with pytest.raises(q.QualificationError) as error:
        q.bootstrap_candidate(
            candidate,
            q.load_candidate(candidate),
            tmp_path / "runtime",
            run=run,
            create_venv=_fake_candidate_venv,
        )
    assert _code(error) is reason
    assert "secret detail" not in str(error.value)


def test_candidate_runtime_marker_and_reexec_are_exact(tmp_path: Path) -> None:
    root = tmp_path / "venv"
    package = root / "lib" / "python" / "site-packages" / "mcp" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")
    assert not q.in_candidate_runtime({}, str(root), mcp_origin=lambda: str(package))
    assert q.in_candidate_runtime(
        {q.CANDIDATE_MARKER: str(root)}, str(root), mcp_origin=lambda: str(package)
    )
    with pytest.raises(q.QualificationError) as mismatch:
        q.in_candidate_runtime(
            {q.CANDIDATE_MARKER: str(tmp_path / "other")},
            str(root),
            mcp_origin=lambda: str(package),
        )
    assert _code(mismatch) is Reason.ENTRYPOINT_UNRESOLVED

    installed = q.InstalledCandidate(
        root,
        root / "bin" / "python",
        root / "bin" / "omnivia-core-service",
        root / "bin" / "omnivia",
        root / "bin" / "omnivia-core-mcp",
    )
    observed: dict[str, Any] = {}

    def execve(program: str, argv: list[str], env: dict[str, str]) -> None:
        observed.update(program=program, argv=argv, env=env)

    q.reexec_under_candidate(installed, ["--host", "codex-cli"], environ={"LANG": "C"}, execve=execve)
    assert observed["program"] == str(installed.python)
    assert observed["argv"] == [
        str(installed.python),
        "-I",
        str(q.SCRIPT_PATH),
        "--host",
        "codex-cli",
    ]
    assert observed["env"] == {"LANG": "C", q.CANDIDATE_MARKER: str(root)}


# --- transparent proxy and host process -----------------------------------


def _frame(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode() + b"\n"


@pytest.mark.parametrize(
    ("frame", "kind"),
    [
        (b"", "malformed"),
        (b"{}", "malformed"),
        (b"not-json\n", "not_json"),
        (b"[]\n", "not_object"),
        (b'{"jsonrpc":"1.0"}\n', "not_jsonrpc_2_0"),
    ],
)
def test_proxy_frame_parser_rejects_every_non_jsonrpc_object(frame: bytes, kind: str) -> None:
    with pytest.raises(q._Violation) as error:
        q._parse_frame(frame)
    assert error.value.kind == kind


@posix_only
def test_observer_is_exclusive_private_closed_and_sequence_checked(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    observer = q._Observer(path)
    observer.emit("proxy_started")
    observer.emit("initialize_request")
    observer.close()
    observer.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert q.read_observation(path) == [
        {"event": "proxy_started", "seq": 1},
        {"event": "initialize_request", "seq": 2},
    ]
    with pytest.raises(FileExistsError):
        q._Observer(path)
    path.write_text('{"event":"proxy_started","seq":2}\n', encoding="ascii")
    with pytest.raises(q.QualificationError) as error:
        q.read_observation(path)
    assert _code(error) is Reason.HOST_OUTPUT_AMBIGUOUS


def test_relay_observes_exact_inventory_success_error_and_withholding(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    digest = q.arguments_digest({"query": "fixed"})
    observer = q._Observer(path)
    relay = q._Relay(observer, q.Interruption("evidence_search", digest))
    relay.request(_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    assert not relay.response(_frame({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}}))
    relay.request(_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
    assert not relay.response(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "workspace_inspect"}, {"name": "evidence_search"}]},
            }
        )
    )
    relay.request(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "workspace_inspect", "arguments": {}},
            }
        )
    )
    assert not relay.response(
        _frame({"jsonrpc": "2.0", "id": 3, "result": {"isError": False, "content": []}})
    )
    relay.request(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "evidence_search", "arguments": {"query": "fixed"}},
            }
        )
    )
    assert relay.response(
        _frame({"jsonrpc": "2.0", "id": 4, "result": {"isError": False, "content": []}})
    )
    observer.close()
    summary = q.summarize_observation(q.read_observation(path))
    assert summary.initialized and summary.listed and summary.withheld and not summary.violation
    assert summary.listed_tools == ("workspace_inspect", "evidence_search")
    assert summary.called == ("workspace_inspect", "evidence_search")
    assert summary.responded == ("workspace_inspect",)


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "BAD-NAME"}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
    ],
)
def test_relay_refuses_malformed_tool_calls(tmp_path: Path, message: dict[str, Any]) -> None:
    observer = q._Observer(tmp_path / "events")
    with pytest.raises(q._Violation) as error:
        q._Relay(observer, None).request(_frame(message))
    observer.close()
    assert error.value.kind == "invalid_tool_call"


def test_relay_refuses_duplicate_ids_and_malformed_inventory(tmp_path: Path) -> None:
    observer = q._Observer(tmp_path / "events")
    relay = q._Relay(observer, None)
    request = _frame({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    relay.request(request)
    with pytest.raises(q._Violation) as duplicate:
        relay.request(request)
    assert duplicate.value.kind == "duplicate_request"
    with pytest.raises(q._Violation) as inventory:
        relay.response(
            _frame({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "BAD"}]}})
        )
    observer.close()
    assert inventory.value.kind == "invalid_tool_inventory"


def _proxy_child() -> str:
    return (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line); method=m.get('method'); result={}\n"
        " if method=='initialize': result={'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'test','version':'1'}}\n"
        " elif method=='tools/list': result={'tools':[{'name':'workspace_inspect'}]}\n"
        " elif method=='tools/call': result={'content':[],'isError':False}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result},separators=(',',':')),flush=True)\n"
    )


@posix_only
def test_proxy_relays_exact_frames_and_withholds_only_the_target(tmp_path: Path) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "workspace_inspect", "arguments": {}},
        },
    ]
    payload = b"".join(_frame(message) for message in messages)
    observation = tmp_path / "normal-events"
    spec = tmp_path / "normal-spec"
    q.write_proxy_spec(
        spec, child=[sys.executable, "-u", "-c", _proxy_child()], observation=observation
    )
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=payload,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0 and completed.stderr == b""
    assert [json.loads(line) for line in completed.stdout.splitlines()] == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "workspace_inspect"}] }},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [], "isError": False}},
    ]
    assert not q.summarize_observation(q.read_observation(observation)).violation

    target = tmp_path / "target-events"
    target_spec = tmp_path / "target-spec"
    q.write_proxy_spec(
        target_spec,
        child=[sys.executable, "-u", "-c", _proxy_child()],
        observation=target,
        interruption=q.Interruption("workspace_inspect", q.arguments_digest({})),
    )
    interrupted = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(target_spec)],
        input=_frame(messages[-1]),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert interrupted.returncode == q.PROXY_WITHHELD_EXIT
    assert interrupted.stdout == b"" and interrupted.stderr == b""
    assert q.summarize_observation(q.read_observation(target)).withheld


def test_cleanup_never_signals_an_already_exited_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exited:
        pid = 123

        @staticmethod
        def poll() -> int:
            return 0

    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("signalled an exited group"))
    q._kill_group(Exited())


def test_host_version_accepts_only_one_pinned_native_identity(tmp_path: Path) -> None:
    def runner(output: bytes, status: int = 0) -> Any:
        return lambda *_: subprocess.CompletedProcess([], status, output, b"ignored")

    assert q.require_host_version(
        "claude-code", Path("/bin/claude"), {}, tmp_path, run=runner(b"2.1.288 (Claude Code)\n")
    ) == "2.1.288"
    assert q.require_host_version(
        "codex-cli", Path("/bin/codex"), {}, tmp_path, run=runner(b"codex-cli 0.146.0\n")
    ) == "0.146.0"
    for output in (b"2.1.288\n", b"Claude Code 2.1.288 2.1.289\n"):
        with pytest.raises(q.QualificationError) as error:
            q.require_host_version(
                "claude-code", Path("/bin/claude"), {}, tmp_path, run=runner(output)
            )
        assert _code(error) is Reason.HOST_VERSION_UNSUPPORTED


@posix_only
@pytest.mark.parametrize(
    ("host", "stdout", "stderr"),
    [
        ("claude-code", b'{"loggedIn":true,"email":"discarded"}\n', b""),
        ("codex-cli", b"", b"warning\nLogged in using ChatGPT\n"),
    ],
)
def test_host_authentication_is_proved_only_inside_the_isolated_home(
    host: str, stdout: bytes, stderr: bytes, tmp_path: Path
) -> None:
    auth = _auth(tmp_path)
    layout = q.host_layout(tmp_path / "isolated", host)
    binary = tmp_path / host
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)

    def run(arguments: Any, environment: Any, cwd: Path, timeout: float) -> Any:
        assert layout.auth_destination.read_bytes() == SECRET
        assert environment["HOME"] == str(layout.home)
        assert cwd == layout.workspace and timeout == 60.0
        expected = "auth" if host == "claude-code" else "login"
        assert arguments[1] == expected
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

    q.require_host_authentication(host, binary, layout, auth, run=run)


@posix_only
def test_an_unusable_copied_host_credential_fails_before_a_journey(tmp_path: Path) -> None:
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    with pytest.raises(q.QualificationError) as error:
        q.require_host_authentication(
            "claude-code",
            tmp_path / "claude",
            layout,
            _auth(tmp_path),
            run=lambda *_: subprocess.CompletedProcess(
                [], 0, b'{"loggedIn":false}', b"discarded"
            ),
        )
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE


@pytest.mark.parametrize("host", ["claude-code", "codex-cli"])
def test_configure_profile_accepts_each_hosts_native_document(
    host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    config = installation / "authoring.json"
    config.write_text('{"principal_id":"principal-1"}', encoding="utf-8")
    installed = q.InstalledCandidate(
        tmp_path / "venv",
        tmp_path / "venv/bin/python",
        tmp_path / "venv/bin/omnivia-core-service",
        tmp_path / "venv/bin/omnivia",
        tmp_path / "venv/bin/omnivia-core-mcp",
    )
    context = q.CoreContext(tmp_path, tmp_path / "workspace", installation, "workspace-1")
    entry = {
        "command": str(installed.mcp),
        "args": ["--config", str(config)],
    }
    if host == "claude-code":
        output = json.dumps({"mcpServers": {q.SERVER_KEY: entry}})
    else:
        output = q.codex_config_toml(entry)
    monkeypatch.setattr(
        q,
        "_run_text",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )
    assert q.configure_profile(installed, context, host, "authoring") == config
    assert q.configuration_principal(config) == "principal-1"


def _host_result(
    *,
    called: tuple[str, ...],
    requests: tuple[tuple[str, str], ...],
    succeeded: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
    paused: bool = False,
) -> Any:
    return q.HostRunResult(
        q.ObservationSummary(
            initialized=True,
            listed=True,
            listed_tools=q.AUTHORING_TOOLS,
            called=called,
            requests=requests,
            responded=(*succeeded, *errors),
            succeeded=succeeded,
            tool_errors=errors,
            paused=paused,
            withheld=False,
            violation=False,
        ),
        marker_seen=True,
        exited_cleanly=True,
        interrupted=False,
        paused=paused,
    )


def test_host_driver_retries_only_a_missing_target_and_allows_safe_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "memory_search"
    target_arguments = {"query": "fixed"}
    target_digest = q.arguments_digest(target_arguments)
    safe_digest = q.arguments_digest({})
    outcomes = [
        _host_result(
            called=("workspace_inspect",),
            requests=(("workspace_inspect", safe_digest),),
            succeeded=("workspace_inspect",),
        ),
        _host_result(
            called=("workspace_inspect", target, target),
            requests=(
                ("workspace_inspect", safe_digest),
                (target, target_digest),
                (target, target_digest),
            ),
            succeeded=("workspace_inspect", target, target),
        ),
    ]
    monkeypatch.setattr(q, "run_host_session", lambda **_kwargs: outcomes.pop(0))
    driver = q.HostDriver(
        "codex-cli",
        tmp_path / "codex",
        tmp_path / "auth",
        object(),
        tmp_path / "core.json",
        tmp_path / "sessions",
        q.AUTHORING_TOOLS,
    )
    driver.call(target, target_arguments)
    assert driver.sequence == 2 and outcomes == []


def test_host_driver_refuses_an_unexpected_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "memory_search"
    arguments = {"query": "fixed"}
    digest = q.arguments_digest(arguments)
    mutation_digest = q.arguments_digest({"input": {}})
    monkeypatch.setattr(
        q,
        "run_host_session",
        lambda **_kwargs: _host_result(
            called=("evidence_capture", target),
            requests=(("evidence_capture", mutation_digest), (target, digest)),
            succeeded=("evidence_capture", target),
        ),
    )
    driver = q.HostDriver(
        "codex-cli",
        tmp_path / "codex",
        tmp_path / "auth",
        object(),
        tmp_path / "core.json",
        tmp_path / "sessions",
        q.AUTHORING_TOOLS,
    )
    with pytest.raises(q.QualificationError) as error:
        driver.call(target, arguments)
    assert _code(error) is Reason.GATE_FAILED


def test_host_driver_releases_the_same_paused_request_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = "evidence_capture"
    arguments = {"input": {}, "idempotency_key": "fixed"}
    digest = q.arguments_digest(arguments)
    revoked: list[bool] = []

    def run(**kwargs: Any) -> Any:
        pause = kwargs["pause_before"]
        assert pause == q.PauseBefore(tool, digest, pause.release)
        pause.release.parent.mkdir(parents=True, exist_ok=True)
        kwargs["on_paused"]()
        assert pause.release.read_text(encoding="utf-8") == "release\n"
        return _host_result(
            called=(tool,),
            requests=((tool, digest),),
            errors=(tool,),
            paused=True,
        )

    monkeypatch.setattr(q, "run_host_session", run)
    driver = q.HostDriver(
        "codex-cli",
        tmp_path / "codex",
        tmp_path / "auth",
        object(),
        tmp_path / "core.json",
        tmp_path / "sessions",
        q.AUTHORING_TOOLS,
    )
    driver.call(
        tool,
        arguments,
        expected_error=True,
        pause_before=True,
        on_paused=lambda: revoked.append(True),
    )
    assert revoked == [True]


def test_durable_import_inspection_stops_then_restarts_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = object()
    context = object()
    events: list[str] = []
    monkeypatch.setattr(q, "stop_core", lambda actual: events.append(f"stop:{actual is context}"))
    monkeypatch.setattr(q, "imported_job", lambda actual: "job-1" if actual is context else "")
    monkeypatch.setattr(
        q,
        "start_core",
        lambda actual_installed, actual_context: events.append(
            f"start:{actual_installed is installed}:{actual_context is context}"
        ),
    )
    job_id = q.inspect_settled_import_job(installed, context, events.append)
    assert job_id == "job-1"
    assert events == [
        "stop:True",
        "import_core_stopped_for_inspection",
        "start:True:True",
        "import_core_restarted",
    ]


def test_import_job_database_connection_is_closed_after_inspection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = workspace / "workspace.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE omnivia_durable_jobs (job_id TEXT, state TEXT);"
            "CREATE TABLE omnivia_application_import_claims "
            "(job_id TEXT, workspace_id TEXT);"
            "INSERT INTO omnivia_durable_jobs VALUES ('job-1', 'succeeded');"
            "INSERT INTO omnivia_application_import_claims "
            "VALUES ('job-1', 'workspace-1');"
        )
    context = q.CoreContext(tmp_path, workspace, tmp_path / "installation", "workspace-1")
    assert q.imported_job(context) == "job-1"
    with sqlite3.connect(database, timeout=0) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()


# --- command line ----------------------------------------------------------


@pytest.fixture
def run(
    tmp_path: Path,
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Any:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    binary = tmp_path / "host-binary"
    binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    binary.chmod(0o755)
    auth = tmp_path / "auth.bin"
    auth.write_bytes(SECRET)
    auth.chmod(0o600)
    output = tmp_path / "out" / "record.json"

    def invoke(**overrides: Any) -> tuple[int, str, str]:
        output.unlink(missing_ok=True)
        arguments = {
            "--host": "claude-code",
            "--host-binary": binary,
            "--candidate": candidate,
            "--auth-file": auth,
            "--output": output,
            "--schema": SCHEMA,
        }
        arguments.update(overrides)
        argv = [
            token
            for key, value in arguments.items()
            if value is not None
            for token in (key, str(value))
        ]
        status = q.main(argv)
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    invoke.output = output  # type: ignore[attr-defined]
    return invoke


@posix_only
def test_qualification_bootstraps_then_reexecutes_under_the_candidate(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed = q.InstalledCandidate(
        tmp_path / "candidate-venv",
        tmp_path / "candidate-venv" / "bin" / "python",
        tmp_path / "candidate-venv" / "bin" / "omnivia-core-service",
        tmp_path / "candidate-venv" / "bin" / "omnivia",
        tmp_path / "candidate-venv" / "bin" / "omnivia-core-mcp",
    )
    observed: dict[str, Any] = {}
    monkeypatch.setattr(q, "bootstrap_candidate", lambda *_: installed)

    def reexec(candidate_install: Any, argv: list[str], *, environ: Any) -> None:
        observed.update(installed=candidate_install, argv=argv, environ=environ)
        raise q.QualificationError(Reason.ENTRYPOINT_UNRESOLVED)

    monkeypatch.setattr(q, "reexec_under_candidate", reexec)
    status, out, err = run()
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")
    assert observed["installed"] is installed
    assert observed["argv"][-2] == "--runtime-root"
    assert observed["environ"]["PATH"] == q.SYSTEM_PATH
    assert set(observed["environ"]) == {"PATH", "LANG", "TMPDIR"}
    record = json.loads(run.output.read_text(encoding="utf-8"))
    assert (record["verdict"], record["reason_code"]) == (
        "fail",
        "entrypoint_unresolved",
    )


@posix_only
@pytest.mark.parametrize(("host", "version"), sorted(q.HOST_VERSIONS.items()))
def test_candidate_runtime_executes_the_live_runner_and_writes_one_pass_record(
    run: Any,
    candidate: Path,
    host: str,
    version: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / f"runtime-{host}"
    prefix = runtime / "candidate-venv"
    (prefix / "bin").mkdir(parents=True)
    receipt = {"revision": REVISION, "wheels": q.load_candidate(candidate).wheels}
    (runtime / "bootstrap-receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    installed = q.InstalledCandidate(
        prefix,
        prefix / "bin" / "python",
        prefix / "bin" / "omnivia-core-service",
        prefix / "bin" / "omnivia",
        prefix / "bin" / "omnivia-core-mcp",
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(q, "in_candidate_runtime", lambda *_: True)
    monkeypatch.setattr(q, "installed_from_prefix", lambda *_: installed)
    monkeypatch.setattr(q, "require_host_version", lambda *_, **__: version)
    monkeypatch.setattr(q, "require_host_authentication", lambda *_, **__: None)
    monkeypatch.setattr(q, "qualify_host", lambda **_: _passed_ledger())
    monkeypatch.setattr(q, "os_identity", lambda: q.OsIdentity("27.0", "26A428", "arm64"))
    status, out, err = run(
        **{"--host": host, "--runtime-root": runtime}
    )
    assert (status, out, err) == (0, "qualification pass\n", "")
    record = json.loads(run.output.read_text(encoding="utf-8"))
    assert record["verdict"] == "pass"
    assert record["host"] == {"name": host, "version": version}
    assert record["source"] == {"revision": REVISION, "clean": True}
    assert not runtime.exists()


@posix_only
def test_preflight_refusals_carry_only_stable_codes(run: Any, tmp_path: Path, candidate: Path) -> None:
    cases = {
        "host_binary_unavailable": {"--host-binary": tmp_path / "absent"},
        "authentication_unavailable": {"--auth-file": tmp_path / "absent-auth"},
        "record_invalid": {"--schema": tmp_path / "absent-schema.json"},
        "candidate_invalid": {"--candidate": tmp_path / "absent-candidate"},
    }
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    cases["host_binary_unavailable"] = {"--host-binary": plain}
    for code, override in cases.items():
        status, out, err = run(**override)
        assert (status, out, err) == (1, "", f"reason_code={code}\n")
        if code == "record_invalid":
            assert not run.output.exists()
        else:
            record = json.loads(run.output.read_text(encoding="utf-8"))
            assert (record["verdict"], record["reason_code"]) == ("fail", code)
    wheel = next(candidate.joinpath("wheels").glob("omnivia_core-*"))
    wheel.write_bytes(b"tampered")
    assert run()[2] == "reason_code=wheel_digest_mismatch\n"
    assert json.loads(run.output.read_text(encoding="utf-8"))["reason_code"] == "wheel_digest_mismatch"
    _edit_json(_provenance(candidate), lambda d: d["source"].update(dirty=True))
    assert run()[2] == "reason_code=candidate_dirty\n"
    assert json.loads(run.output.read_text(encoding="utf-8"))["reason_code"] == "candidate_dirty"


def test_an_unsupported_live_platform_is_refused(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert run()[2] == "reason_code=platform_unsupported\n"
    assert json.loads(run.output.read_text(encoding="utf-8"))["reason_code"] == "platform_unsupported"


def test_missing_qualification_arguments_are_a_usage_error(run: Any) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run(**{"--auth-file": None})
    assert exit_info.value.code == 2


def test_the_validation_only_mode_checks_a_record(
    run: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.json"
    q.write_record(_pass_record(), _schema(), good)
    assert q.main(["--schema", str(SCHEMA), "--validate-record", str(good)]) == 0
    assert capsys.readouterr().out == "record valid\n"
    bad = tmp_path / "bad.json"
    document = json.loads(good.read_text(encoding="utf-8"))
    document["prompt"] = "leak"
    bad.write_text(json.dumps(document), encoding="utf-8")
    for target in (bad, tmp_path / "absent.json"):
        assert q.main(["--schema", str(SCHEMA), "--validate-record", str(target)]) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "reason_code=record_invalid\n")
