"""Offline guards for the real-host MCP qualification foundation."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import importlib.util
import inspect
import io
import json
import os
import platform
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tomllib
import zipfile
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


def _wheel_entry(
    root: Path, name: str, filename: str, content: bytes, version: str, first_party: bool
) -> dict[str, Any]:
    """Write one wheel file and return its manifest entry, sized and digested as the builder records."""
    (root / "wheels" / filename).write_bytes(content)
    return {
        "name": name,
        "first_party": first_party,
        "path": f"wheels/{filename}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "version": version,
    }


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
        wheels.append(_wheel_entry(root, name, filename, f"wheel bytes for {name}".encode(), "0.1.0", True))
    for name, version in (("mcp", sdk[0]), ("mcp-types", sdk[1]), ("anyio", "4.14.2")):
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        wheels.append(_wheel_entry(root, name, filename, f"third-party bytes for {name}".encode(), version, False))
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
    assert loaded.closure_count == 8
    assert loaded.harness_sha256 == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    for digest in loaded.wheels.values():
        assert len(digest) == 64


def test_closure_binding_is_deterministic_and_covers_the_full_manifest(candidate: Path) -> None:
    first = q.load_candidate(candidate)
    _edit_json(_manifest(candidate), lambda document: document["wheels"].reverse())
    second = q.load_candidate(candidate)
    assert second.closure_count == first.closure_count == 8
    assert second.closure_sha256 == first.closure_sha256
    assert second.closure_sha256 not in first.wheels.values()


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


def test_os_identity_requires_the_frozen_version_and_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "arm64")

    def runner(build: str) -> Any:
        return lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, build + "\n", "")

    identity = q.os_identity(
        mac_version=lambda: (q.SUPPORTED_OS_VERSION, (0, 0, 0), ""),
        run=runner(q.SUPPORTED_OS_BUILD),
    )
    assert identity == q.OsIdentity(q.SUPPORTED_OS_VERSION, q.SUPPORTED_OS_BUILD, "arm64")
    for version, build in (("27.0.1", q.SUPPORTED_OS_BUILD), (q.SUPPORTED_OS_VERSION, "26A429")):
        with pytest.raises(q.QualificationError) as error:
            q.os_identity(
                mac_version=lambda version=version: (version, (0, 0, 0), ""),
                run=runner(build),
            )
        assert _code(error) is Reason.PLATFORM_UNSUPPORTED


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


def test_the_inventories_are_the_exact_stable_fourteen_and_twenty_five() -> None:
    authoring = _literal(AUTHORING_SCRIPT, "AUTHORING_TOOLS")
    assert q.AUTHORING_TOOLS == authoring
    assert len(q.AUTHORING_TOOLS) == q.AUTHORING_TOOL_COUNT == 25
    assert len(set(q.AUTHORING_TOOLS)) == 25
    assert len(q.RESTRICTED_TOOLS) == q.RESTRICTED_TOOL_COUNT == 14
    assert q.RESTRICTED_TOOLS == q.AUTHORING_TOOLS[:14]
    assert sorted(q.RESTRICTED_TOOLS) == _literal(BUILDER, "HOST_TOOLS")
    assert q.AUTHORING_TOOLS[14:] == (
        "memory_create",
        "evidence_capture",
        "import_start",
        "trigger_declare",
        "trigger_lifecycle",
        "trigger_ingest",
        "job_get",
        "job_events",
        "skills_draft_create",
        "skills_draft_update",
        "skills_proposal_submit",
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
    assert q.claude_mcp_config(entry) == {
        "mcpServers": {"omnivia-core": {**ENTRY, "env": {"CLAUDE_CODE_OAUTH_TOKEN": ""}}}
    }


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
        "schema_sha256",
        "os_identity",
        "host",
        "ledger",
        "started_at",
        "finished_at",
        "reason",
    }
    assert not {"output", "marker", "text", "prompt", "transcript", "response"} & parameters


# --- record construction ---------------------------------------------------

def _inputs(ledger: Any = None, host_version: str = "2.1.289", host: str = "claude-code") -> dict[str, Any]:
    return {
        "candidate": q.Candidate(
            REVISION,
            {name: f"{n:064x}" for n, name in enumerate(q.FIRST_PARTY, 1)},
            8,
            "a" * 64,
            "b" * 64,
        ),
        "schema_sha256": "c" * 64,
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
    assert record["profiles"]["restricted"]["tool_count"] == 14
    assert record["profiles"]["authoring"]["tool_count"] == 25
    assert record["profiles"]["authoring"]["tools"] == list(q.AUTHORING_TOOLS)
    assert record["sdk_versions"] == {"mcp": "2.0.0", "mcp-types": "2.0.0"}
    assert record["bindings"] == {
        "wheel_closure_count": 8,
        "wheel_closure_sha256": "a" * 64,
        "harness_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
    }
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
    "zero_closure": lambda r: r["bindings"].update(wheel_closure_count=0),
    "short_closure_digest": lambda r: r["bindings"].update(wheel_closure_sha256="0" * 63),
    "upper_harness_digest": lambda r: r["bindings"].update(harness_sha256="A" * 64),
    "path_schema_digest": lambda r: r["bindings"].update(schema_sha256="/tmp/schema"),
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


def _minimal_wheel(directory: Path) -> Path:
    """Create a valid local pure-Python wheel without build tools or a network."""
    directory.mkdir(parents=True)
    wheel = directory / "offline_probe-1.0.0-py3-none-any.whl"
    dist_info = "offline_probe-1.0.0.dist-info"
    files = {
        "offline_probe/__init__.py": "VALUE = 1\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: offline-probe\nVersion: 1.0.0\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: omnivia-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = "".join(f"{name},,\n" for name in files)
    record += f"{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(f"{dist_info}/RECORD", record)
    return wheel


def test_real_pip_installs_hashed_local_wheel_offline_from_a_path_with_spaces(
    tmp_path: Path,
) -> None:
    wheel = _minimal_wheel(tmp_path / "wheel house with spaces")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

    def install(requirement_digest: str, target: Path, requirements: Path) -> Any:
        requirements.write_text(
            f"{wheel.resolve().as_uri()} --hash=sha256:{requirement_digest}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-index",
                "--only-binary=:all:",
                "--require-hashes",
                "--no-deps",
                "--no-input",
                "--target",
                str(target),
                "--requirement",
                str(requirements),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=120,
        )

    accepted = install(digest, tmp_path / "accepted target", tmp_path / "accepted req.txt")
    assert accepted.returncode == 0
    assert (tmp_path / "accepted target" / "offline_probe" / "__init__.py").is_file()
    refused = install("0" * 64, tmp_path / "refused target", tmp_path / "refused req.txt")
    assert refused.returncode != 0
    assert not (tmp_path / "refused target" / "offline_probe" / "__init__.py").exists()


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
    assert {
        "--isolated",
        "--no-index",
        "--only-binary=:all:",
        "--require-hashes",
        "--no-input",
    } <= set(install)
    # No index and no find-links: the only installable inputs are the hashed closure lines.
    assert "--find-links" not in install and "--index-url" not in install
    requirements = Path(install[install.index("--requirement") + 1]).read_text(encoding="utf-8")
    lines = requirements.splitlines()
    manifest_wheels = json.loads(_manifest(candidate).read_text())["wheels"]
    assert len(lines) == len(manifest_wheels)
    assert {line.rsplit("sha256:", 1)[1] for line in lines} == {
        entry["sha256"] for entry in manifest_wheels
    }
    assert all(line.startswith("file://") for line in lines)
    assert calls[0][1] == {
        "PATH": q.SYSTEM_PATH,
        "HOME": str(tmp_path / "runtime" / "bootstrap-home"),
        "PYTHONNOUSERSITE": "1",
    }
    assert calls[1][0][1:4] == ["-I", "-c", q._PROBE]


def test_bootstrap_refuses_manifest_closure_drift_after_candidate_loading(
    candidate: Path, tmp_path: Path
) -> None:
    loaded = q.load_candidate(candidate)
    _edit_json(
        _manifest(candidate),
        lambda document: document["wheels"][-1].update(version="4.14.3"),
    )
    with pytest.raises(q.QualificationError) as error:
        q.bootstrap_candidate(
            candidate,
            loaded,
            tmp_path / "runtime",
            run=lambda *_args, **_kwargs: pytest.fail("pip ran after closure drift"),
            create_venv=lambda _path: None,
        )
    assert _code(error) is Reason.WHEEL_DIGEST_MISMATCH


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
    assert not relay.response(
        _frame({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": q.MCP_PROTOCOL_VERSION}})
    )
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
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "isError": False,
                    "content": [],
                    "structuredContent": {"workspace": {}},
                },
            }
        )
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
    withheld = {"evidence": [{"text": "withheld-secret"}], "page": {"continuation_token": "t"}}
    assert relay.response(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"isError": False, "content": [], "structuredContent": withheld},
            }
        )
    )
    with pytest.raises(q._Violation) as after_withheld:
        relay.response(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "result": {"isError": False, "structuredContent": {"late": True}},
                }
            )
        )
    assert after_withheld.value.kind == "frame_after_withheld"
    observer.close()
    summary = q.summarize_observation(q.read_observation(path))
    assert summary.initialized and summary.listed and summary.withheld and not summary.violation
    assert summary.listed_tools == ("workspace_inspect", "evidence_search")
    assert summary.called == ("workspace_inspect", "evidence_search")
    assert summary.responded == ("workspace_inspect",)
    assert summary.withheld_digest == q.canonical_result_digest(withheld)
    assert "withheld-secret" not in path.read_text(encoding="ascii")


def _withhold(directory: Path, result: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Relay one targeted call and its answer; return the summary and the raw events."""
    directory.mkdir()
    path = directory / "events.jsonl"
    observer = q._Observer(path)
    relay = q._Relay(observer, q.Interruption("evidence_capture", q.arguments_digest({})))
    relay.request(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "evidence_capture", "arguments": {}},
            }
        )
    )
    try:
        assert relay.response(_frame({"jsonrpc": "2.0", "id": 1, "result": result}))
    finally:
        observer.close()
    events = q.read_observation(path)
    return q.summarize_observation(events), events


def test_the_withheld_answer_is_kept_as_its_canonical_digest_alone(tmp_path: Path) -> None:
    structured = {"evidence_id": "evd-secret", "source": {"source_id": "private-source"}}
    summary, events = _withhold(
        tmp_path / "success",
        {"isError": False, "content": [{"type": "text", "text": "raw"}], "structuredContent": structured},
    )
    (withheld,) = [event for event in events if event["event"] == "response_withheld"]
    assert set(withheld) == {"event", "seq", "tool", "tool_error", "result_digest"}
    assert summary.withheld_digest == q.canonical_result_digest(structured)
    text = (tmp_path / "success" / "events.jsonl").read_text(encoding="ascii")
    for retained in ("evd-secret", "private-source", "raw"):
        assert retained not in text
    # A refusal is withheld too, but there is no successful answer to bind a replay to.
    refused, _ = _withhold(
        tmp_path / "refused", {"isError": True, "content": [{"type": "text", "text": "no"}]}
    )
    assert refused.withheld and refused.withheld_digest is None
    with pytest.raises(q._Violation) as error:
        _withhold(tmp_path / "malformed", {"isError": False, "content": []})
    assert error.value.kind == "invalid_tool_result"
    with pytest.raises(q.QualificationError):
        q.validate_event({**withheld, "result": structured})


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
        " elif method=='tools/call': result={'content':[],'isError':False,'structuredContent':{'workspace':{}}}\n"
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
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [],
                "isError": False,
                "structuredContent": {"workspace": {}},
            },
        },
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
    withheld = q.summarize_observation(q.read_observation(target))
    assert withheld.withheld
    assert withheld.withheld_digest == q.canonical_result_digest({"workspace": {}})


@posix_only
def test_proxy_never_forwards_a_child_frame_queued_after_the_withheld_answer(
    tmp_path: Path,
) -> None:
    child = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " first={'jsonrpc':'2.0','id':m['id'],'result':{'isError':False,'content':[],"
        "'structuredContent':{'workspace':{}}}}\n"
        " late={'jsonrpc':'2.0','id':999,'result':{'isError':False,'content':[],"
        "'structuredContent':{'late':True}}}\n"
        " print(json.dumps(first,separators=(',',':')),flush=True)\n"
        " print(json.dumps(late,separators=(',',':')),flush=True)\n"
    )
    observation = tmp_path / "events"
    spec = tmp_path / "spec"
    q.write_proxy_spec(
        spec,
        child=[sys.executable, "-u", "-c", child],
        observation=observation,
        interruption=q.Interruption("workspace_inspect", q.arguments_digest({})),
    )
    payload = _frame(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workspace_inspect", "arguments": {}},
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=payload,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == q.PROXY_WITHHELD_EXIT
    assert completed.stdout == b"" and completed.stderr == b""
    summary = q.summarize_observation(q.read_observation(observation))
    assert summary.withheld and not summary.violation


_LOGGING_CHILD = (
    "import json,sys\n"
    "log=open(sys.argv[1],'a',buffering=1)\n"
    "for line in sys.stdin:\n"
    " m=json.loads(line); log.write(m['method']+'\\n')\n"
    " print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'isError':False,'content':[],"
    "'structuredContent':{'workspace':{}}}},separators=(',',':')),flush=True)\n"
)

# A child that stops reading stdin: the host's flood fills the pipe and the proxy's
# write to the child blocks, while the child's own answer still has to get through.
_STALLED_INIT_CHILD = (
    "import json,sys,time\n"
    "m=json.loads(sys.stdin.readline())\n"
    "time.sleep(1)\n"
    "print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':'1999-01-01',"
    "'capabilities':{},'serverInfo':{'name':'test','version':'1'}}},separators=(',',':')),flush=True)\n"
    "time.sleep(60)\n"
)

_STALLED_WITHHELD_CHILD = (
    "import json,sys,time\n"
    "m=json.loads(sys.stdin.readline())\n"
    "time.sleep(1)\n"
    "print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'isError':False,'content':[],"
    "'structuredContent':{'workspace':{}}}},separators=(',',':')),flush=True)\n"
    "time.sleep(60)\n"
)


def _flood() -> bytes:
    """Frames large enough that a child which stopped reading blocks the proxy's write."""
    notification = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"pad": "x" * 900_000}}
    return b"".join(_frame(notification) for _ in range(4))


def _await_observed(path: Path, needle: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="ascii"):
            return
        time.sleep(0.01)
    pytest.fail(f"{needle} was never observed")


@posix_only
def test_a_host_frame_sent_after_the_withheld_answer_never_reaches_the_child(tmp_path: Path) -> None:
    observation = tmp_path / "events"
    spec = tmp_path / "spec"
    received = tmp_path / "received"
    q.write_proxy_spec(
        spec,
        child=[sys.executable, "-u", "-c", _LOGGING_CHILD, str(received)],
        observation=observation,
        interruption=q.Interruption("workspace_inspect", q.arguments_digest({})),
    )
    target = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "workspace_inspect", "arguments": {}},
    }
    late = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    proxy = subprocess.Popen(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proxy.stdin is not None
        proxy.stdin.write(_frame(target))
        proxy.stdin.flush()
        # Only once the withheld answer is observed is the late frame sent.
        _await_observed(observation, '"event":"response_withheld"')
        with contextlib.suppress(BrokenPipeError):
            proxy.stdin.write(_frame(late))
            proxy.stdin.flush()
        stdout, _ = proxy.communicate(timeout=20)
    finally:
        if proxy.poll() is None:
            proxy.kill()
            proxy.wait()
    assert proxy.returncode == q.PROXY_VIOLATION_EXIT
    assert stdout == b""
    assert received.read_text(encoding="ascii") == "tools/call\n"


@posix_only
def test_a_child_that_stops_reading_cannot_block_a_bad_initialize(tmp_path: Path) -> None:
    observation = tmp_path / "events"
    spec = tmp_path / "spec"
    q.write_proxy_spec(
        spec, child=[sys.executable, "-u", "-c", _STALLED_INIT_CHILD], observation=observation
    )
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    # A deadlock here times out instead of exiting: the test fails rather than hangs.
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=_frame(initialize) + _flood(),
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == q.PROXY_VIOLATION_EXIT
    assert completed.stdout == b""
    kinds = [e.get("kind") for e in q.read_observation(observation) if e["event"] == "protocol_violation"]
    assert kinds == ["invalid_initialize"]


@posix_only
def test_a_withheld_seal_whose_write_cannot_drain_fails_closed_and_bounded(tmp_path: Path) -> None:
    observation = tmp_path / "events"
    spec = tmp_path / "spec"
    q.write_proxy_spec(
        spec,
        child=[sys.executable, "-u", "-c", _STALLED_WITHHELD_CHILD],
        observation=observation,
        interruption=q.Interruption("workspace_inspect", q.arguments_digest({})),
    )
    target = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "workspace_inspect", "arguments": {}},
    }
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=_frame(target) + _flood(),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == q.PROXY_FAILED_EXIT
    assert completed.stdout == b""
    assert q.summarize_observation(q.read_observation(observation)).withheld


def test_cleanup_never_signals_an_already_exited_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exited:
        pid = 123

        @staticmethod
        def poll() -> int:
            return 0

    monkeypatch.setattr(q, "_group_running", lambda _group: False)
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("signalled an absent group"))
    assert q._kill_group(Exited())


def test_host_group_shutdown_escalates_and_proves_the_whole_group_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Running:
        pid = 4321

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(timeout: float) -> None:
            raise subprocess.TimeoutExpired("host", timeout)

    signals: list[tuple[int, signal.Signals]] = []
    waits = iter([False, True])
    monkeypatch.setattr(q, "_group_running", lambda _group: True)
    monkeypatch.setattr(q, "_wait_group_absent", lambda _group: next(waits))
    monkeypatch.setattr(q.os, "killpg", lambda group, sig: signals.append((group, sig)))
    assert q._kill_group(Running())
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_host_run_refuses_to_pass_when_process_group_cleanup_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Exited:
        pid = 4321
        returncode = 0

        @staticmethod
        def poll() -> int:
            return 0

    monkeypatch.setattr(q.subprocess, "Popen", lambda *_args, **_kwargs: Exited())
    monkeypatch.setattr(q, "_kill_group", lambda _process: False)
    with pytest.raises(q.QualificationError) as error:
        q.run_host(
            ["host"],
            env={},
            cwd=tmp_path,
            observation=tmp_path / "observation",
            marker="MARKER",
            timeout=1,
        )
    assert _code(error) is Reason.CLEANUP_INCOMPLETE


def test_host_version_accepts_only_one_pinned_native_identity(tmp_path: Path) -> None:
    def runner(output: bytes, status: int = 0) -> Any:
        return lambda *_: subprocess.CompletedProcess([], status, output, b"ignored")

    assert q.require_host_version(
        "claude-code", Path("/bin/claude"), {}, tmp_path, run=runner(b"2.1.289 (Claude Code)\n")
    ) == "2.1.289"
    assert q.require_host_version(
        "codex-cli", Path("/bin/codex"), {}, tmp_path, run=runner(b"codex-cli 0.146.0\n")
    ) == "0.146.0"
    for output in (b"2.1.289\n", b"Claude Code 2.1.288 2.1.289\n", b"Claude Code 2.1.288\n"):
        with pytest.raises(q.QualificationError) as error:
            q.require_host_version(
                "claude-code", Path("/bin/claude"), {}, tmp_path, run=runner(output)
            )
        assert _code(error) is Reason.HOST_VERSION_UNSUPPORTED


@posix_only
def test_host_authentication_is_proved_only_inside_the_isolated_home(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    layout = q.host_layout(tmp_path / "isolated", "codex-cli")
    binary = tmp_path / "codex-cli"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)

    def run(arguments: Any, environment: Any, cwd: Path, timeout: float) -> Any:
        assert layout.auth_destination.read_bytes() == SECRET
        assert environment["HOME"] == str(layout.home)
        assert cwd == layout.workspace and timeout == 60.0
        assert arguments[1] == "login"
        return subprocess.CompletedProcess(arguments, 0, b"", b"warning\nLogged in using ChatGPT\n")

    q.require_host_authentication("codex-cli", binary, layout, q.AuthFile(auth), run=run)


@posix_only
def test_an_unusable_copied_host_credential_fails_before_a_journey(tmp_path: Path) -> None:
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    with pytest.raises(q.QualificationError) as error:
        q.require_host_authentication(
            "claude-code",
            tmp_path / "claude",
            layout,
            q.AuthFile(_token_file(tmp_path)),
            run=lambda *_: subprocess.CompletedProcess(
                [], 0, b'{"loggedIn":false}', b"discarded"
            ),
        )
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE


# --- Claude token and Codex file credentials -------------------------------

TOKEN = "sk-ant-oat01-AbCdEf0123456789_-.~+/=DoNotLeakThisTokenValue"


def _token_file(
    tmp_path: Path, content: bytes | None = None, mode: int = 0o600, name: str = "claude-token"
) -> Path:
    source = tmp_path / name
    source.write_bytes(TOKEN.encode("ascii") + b"\n" if content is None else content)
    source.chmod(mode)
    return source


def _claude_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@posix_only
@pytest.mark.parametrize(
    "content", [TOKEN.encode("ascii") + b"\n", TOKEN.encode("ascii"), ("a" * 512).encode() + b"\n"]
)
def test_a_claude_token_file_yields_one_bounded_portable_token(tmp_path: Path, content: bytes) -> None:
    assert q.read_claude_token(_token_file(tmp_path, content)) == content.rstrip(b"\n").decode()


@posix_only
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n",
        b"short\n",
        b"a" * 513 + b"\n",
        b"a" * 2048,
        b"sk-ant-oat01-" + b"\xff" * 8,
        TOKEN.encode("ascii") + b"\nsecond-line\n",
        TOKEN.encode("ascii") + b"\r\n",
        TOKEN.encode("ascii") + b"\n\n",
        b" " + TOKEN.encode("ascii"),
        TOKEN.encode("ascii") + b" ",
        TOKEN.encode("ascii") + b"\x00",
        b"\t" + TOKEN.encode("ascii") + b"\n",
        TOKEN.encode("ascii") + b"\x1b[0m",
        TOKEN.encode("ascii") + "é".encode(),
    ],
)
def test_malformed_claude_token_files_are_refused_without_disclosure(
    tmp_path: Path, content: bytes
) -> None:
    source = _token_file(tmp_path, content)
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    calls: list[Any] = []

    with pytest.raises(q.QualificationError) as error:
        q.require_host_authentication(
            "claude-code",
            _claude_binary(tmp_path),
            layout,
            q.AuthFile(source),
            run=lambda *args: calls.append(args),
        )
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    assert calls == []
    text = repr(error.value) + str(error.value)
    assert "sk-ant" not in text and str(source) not in text
    with pytest.raises(q.QualificationError) as preflight:
        q.read_claude_token(source)
    assert _code(preflight) is Reason.AUTHENTICATION_UNAVAILABLE


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
def test_a_claude_token_file_with_group_or_world_bits_is_refused(tmp_path: Path, mode: int) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.read_claude_token(_token_file(tmp_path, mode=mode))
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    with pytest.raises(q.QualificationError):
        q.read_claude_token(tmp_path / "absent-token")


@posix_only
def test_claude_authentication_injects_only_the_token_into_the_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-ambient-value-000000")
    source = _token_file(tmp_path)
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(arguments: Any, environment: Any, cwd: Path, timeout: float) -> Any:
        seen.append((list(arguments), dict(environment)))
        return subprocess.CompletedProcess(arguments, 0, b'{"loggedIn":true}', b"")

    q.require_host_authentication(
        "claude-code", _claude_binary(tmp_path), layout, q.AuthFile(source), run=run
    )
    [(arguments, environment)] = seen
    assert arguments[1:] == ["auth", "status", "--json"]
    assert environment == {
        "PATH": f"{tmp_path}:{q.SYSTEM_PATH}",
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(layout.temporary),
        "HOME": str(layout.home),
        "CLAUDE_CONFIG_DIR": str(layout.config_dir),
        "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
    }
    assert not layout.auth_destination.exists()
    assert source.read_bytes() == TOKEN.encode("ascii") + b"\n"


@posix_only
def test_every_claude_session_receives_the_token_and_nothing_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _token_file(tmp_path)
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    installed = q.InstalledCandidate(
        tmp_path / "venv",
        tmp_path / "venv" / "python",
        tmp_path / "venv" / "service",
        tmp_path / "venv" / "omnivia",
        tmp_path / "venv" / "mcp",
    )
    sentinel = object()
    seen: dict[str, Any] = {}

    def fake_run_host(command: Any, **kwargs: Any) -> object:
        seen.update(command=list(command), **kwargs)
        return sentinel

    monkeypatch.setattr(q, "run_host", fake_run_host)
    result = q.run_host_session(
        host="claude-code",
        binary=_claude_binary(tmp_path),
        layout=layout,
        installed=installed,
        core_config=tmp_path / "core.json",
        auth=q.AuthFile(source),
        prompt="prompt-text",
        marker="marker-text",
        tools=q.RESTRICTED_TOOLS,
        timeout=1.0,
    )
    assert result is sentinel
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(layout.config_dir)
    assert seen["env"]["HOME"] == str(layout.home)
    assert "--strict-mcp-config" in seen["command"]
    config_text = (layout.root / "claude-mcp.json").read_text(encoding="utf-8")
    server = json.loads(config_text)["mcpServers"][q.SERVER_KEY]
    assert server["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": ""}
    assert TOKEN not in config_text
    assert not (layout.config_dir / ".credentials.json").exists()
    persisted = [path for path in layout.root.rglob("*") if path.is_file()]
    assert persisted and all(TOKEN.encode("ascii") not in path.read_bytes() for path in persisted)


@posix_only
def test_codex_keeps_its_copied_auth_file_and_never_receives_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _auth(tmp_path)
    layout = q.host_layout(tmp_path / "isolated", "codex-cli")
    binary = tmp_path / "codex-cli"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    seen: list[dict[str, str]] = []

    def run(arguments: Any, environment: Any, cwd: Path, timeout: float) -> Any:
        seen.append(dict(environment))
        return subprocess.CompletedProcess(arguments, 0, b"Logged in\n", b"")

    q.require_host_authentication("codex-cli", binary, layout, q.AuthFile(auth), run=run)
    assert layout.auth_destination.read_bytes() == SECRET
    assert stat.S_IMODE(layout.auth_destination.stat().st_mode) == 0o600
    assert set(seen[0]) == {"PATH", "LANG", "TMPDIR", "HOME", "CODEX_HOME"}
    config = q.write_host_config(layout, "codex-cli", ENTRY)
    assert config.read_text(encoding="utf-8") == q.codex_config_toml(ENTRY)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in config.read_text(encoding="utf-8")

    # Codex copies a token-shaped file verbatim; it is never read as a token.
    second = q.host_layout(tmp_path / "second", "codex-cli")
    token_shaped = _token_file(tmp_path, name="codex-token-shaped")
    assert q.provision_credential("codex-cli", second, q.AuthFile(token_shaped)) == {}
    assert second.auth_destination.read_bytes() == TOKEN.encode("ascii") + b"\n"


def test_the_token_reaches_no_run_outside_an_authenticated_claude_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-ambient-value-000000")
    layout = q.host_layout(tmp_path, "claude-code")
    binary = _claude_binary(tmp_path)
    assert q.host_environment(layout, binary, {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})[
        "CLAUDE_CODE_OAUTH_TOKEN"
    ] == TOKEN
    seen: list[Any] = []

    def runner(argv: Any, env: Any, cwd: Path, timeout: float) -> Any:
        seen.append(dict(env))
        return subprocess.CompletedProcess(argv, 0, b"2.1.289 (Claude Code)\n", b"")

    q.require_host_version("claude-code", binary, q.host_environment(layout, binary), tmp_path, run=runner)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen[0]
    codex = q.host_layout(tmp_path, "codex-cli")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in q.host_environment(
        codex, binary, q.provision_credential("codex-cli", codex, q.AuthFile(_auth(tmp_path)))
    )


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


def test_revocation_is_confirmed_by_the_redacted_owner_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = q.InstalledCandidate(
        tmp_path / "venv",
        tmp_path / "venv/bin/python",
        tmp_path / "venv/bin/service",
        tmp_path / "venv/bin/omnivia",
        tmp_path / "venv/bin/mcp",
    )
    context = q.CoreContext(
        tmp_path, tmp_path / "workspace", tmp_path / "installation", "workspace-1"
    )
    document = {
        "mcp_status_version": 1,
        "hosts": [
            {
                "host": "codex",
                "service": "reachable",
                "grant": "revoked",
                "credential": "absent",
                "configuration": "absent",
            }
        ],
    }
    monkeypatch.setattr(
        q,
        "_run_text",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps(document), ""
        ),
    )
    q.verify_revoked(installed, context, "codex-cli")
    document["hosts"][0]["grant"] = "active"
    with pytest.raises(q.QualificationError) as error:
        q.verify_revoked(installed, context, "codex-cli")
    assert _code(error) is Reason.GATE_FAILED


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
        q.AuthFile(tmp_path / "auth"),
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
        q.AuthFile(tmp_path / "auth"),
        object(),
        tmp_path / "core.json",
        tmp_path / "sessions",
        q.AUTHORING_TOOLS,
    )
    with pytest.raises(q.QualificationError) as error:
        driver.call(target, arguments)
    assert _code(error) is Reason.GATE_FAILED


def _probe_observer(specification: Path, tools: tuple[str, ...], excluded: tuple[str, ...], refusal: str) -> None:
    """Write the observation a real proxy would write for one excluded-name probe."""
    observer = q._Observer(Path(json.loads(specification.read_text(encoding="utf-8"))["observation"]))
    observer.emit("proxy_started")
    observer.emit("initialize_request")
    observer.emit("initialize_response", ok=True)
    observer.emit("tools_list_request")
    observer.emit("tools_list_response", ok=True, tool_count=len(tools), tool_names=list(tools))
    for name in excluded:
        observer.emit("tool_call_request", tool=name, arguments_digest=q.arguments_digest({}))
    for name in excluded:
        observer.emit(
            "tool_call_response",
            tool=name,
            ok=True,
            tool_error=True,
            result_digest=q.canonical_result_digest(None),
            refusal=refusal,
        )
    observer.close()


@pytest.mark.parametrize(
    ("profile", "tools"), [("restricted", q.RESTRICTED_TOOLS), ("authoring", q.AUTHORING_TOOLS)]
)
def test_the_excluded_sets_are_the_complete_normative_remainder(profile: str, tools: tuple[str, ...]) -> None:
    excluded = q.EXCLUDED_TOOLS[profile]
    assert len(set(excluded)) == len(excluded)
    assert set(excluded).isdisjoint(tools)
    # The restricted profile additionally excludes the eleven authoring-only tools.
    authoring_only = set(q.AUTHORING_TOOLS) - set(q.RESTRICTED_TOOLS)
    assert (authoring_only <= set(excluded)) is (profile == "restricted")
    # Every catalogue operation outside the manifest, and every section 7 category, is probed.
    assert len(q.UNEXPOSED_TOOLS) == 73 - q.AUTHORING_TOOL_COUNT
    assert set(q.UNEXPOSED_TOOLS) <= set(excluded)
    assert set(q.SECTION7_TOOLS) <= set(excluded)
    assert set(q.SECTION7_SENTINELS) == {
        "service_lifecycle_discovery",
        "bootstrap_workspace_selection",
        "grants",
        "filesystem_path_selection",
        "urls",
        "credentials",
        "connector_configuration",
        "administration_configuration",
        "connector_mutation",
    }
    assert len(q.SECTION7_TOOLS) == 18
    assert len(excluded) == (77 if profile == "restricted" else 66)
    assert "job_cancel" in excluded and "job_retry" in excluded


@pytest.mark.parametrize(("profile", "tools"), [("restricted", q.RESTRICTED_TOOLS), ("authoring", q.AUTHORING_TOOLS)])
@pytest.mark.parametrize(("refusal", "accepted"), [("not_exposed", True), ("other", False)])
def test_the_deterministic_excluded_probe_requires_the_servers_allow_list_refusal(
    tmp_path: Path, profile: str, tools: tuple[str, ...], refusal: str, accepted: bool
) -> None:
    excluded = q.EXCLUDED_TOOLS[profile]
    installed = q.InstalledCandidate(
        tmp_path / "venv",
        tmp_path / "venv/bin/python",
        tmp_path / "venv/bin/service",
        tmp_path / "venv/bin/omnivia",
        tmp_path / "venv/bin/mcp",
    )

    def run(argv: Any, payload: bytes, _env: Any, _cwd: Path, _timeout: float) -> Any:
        sent = [json.loads(line) for line in payload.splitlines()]
        assert [message["params"]["name"] for message in sent if message["method"] == "tools/call"] == list(excluded)
        _probe_observer(Path(argv[-1]), tools, excluded, refusal)
        return subprocess.CompletedProcess(argv, 0, b'{"jsonrpc":"2.0"}\n', b"")

    def call() -> None:
        q.probe_excluded_tools(
            installed,
            tmp_path / "core.json",
            tmp_path / "probe",
            "codex-cli",
            tools,
            excluded,
            run=run,
        )

    if accepted:
        call()
    else:
        with pytest.raises(q.QualificationError) as error:
            call()
        assert _code(error) is Reason.GATE_FAILED


def test_an_excluded_name_that_is_listed_or_answered_with_data_fails_the_probe(tmp_path: Path) -> None:
    excluded = q.EXCLUDED_TOOLS["authoring"]
    installed = q.InstalledCandidate(
        tmp_path / "venv",
        tmp_path / "venv/bin/python",
        tmp_path / "venv/bin/service",
        tmp_path / "venv/bin/omnivia",
        tmp_path / "venv/bin/mcp",
    )

    def listed_run(argv: Any, payload: bytes, _env: Any, _cwd: Path, _timeout: float) -> Any:
        _probe_observer(Path(argv[-1]), (*q.AUTHORING_TOOLS, excluded[0]), excluded, "not_exposed")
        return subprocess.CompletedProcess(argv, 0, b'{"jsonrpc":"2.0"}\n', b"")

    with pytest.raises(q.QualificationError) as error:
        q.probe_excluded_tools(
            installed, tmp_path / "core.json", tmp_path / "probe-listed", "codex-cli",
            q.AUTHORING_TOOLS, excluded, run=listed_run,
        )
    assert _code(error) is Reason.GATE_FAILED

    def missing_run(argv: Any, payload: bytes, _env: Any, _cwd: Path, _timeout: float) -> Any:
        _probe_observer(Path(argv[-1]), q.AUTHORING_TOOLS, excluded[:-1], "not_exposed")
        return subprocess.CompletedProcess(argv, 0, b'{"jsonrpc":"2.0"}\n', b"")

    with pytest.raises(q.QualificationError) as missing:
        q.probe_excluded_tools(
            installed, tmp_path / "core.json", tmp_path / "probe-missing", "codex-cli",
            q.AUTHORING_TOOLS, excluded, run=missing_run,
        )
    assert _code(missing) is Reason.GATE_FAILED


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
        q.AuthFile(tmp_path / "auth"),
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
    context = q.CoreContext(tmp_path, tmp_path / "workspace", tmp_path / "installation", "ws")
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


def test_durable_import_inspection_refuses_a_core_that_could_not_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = q.CoreContext(tmp_path, tmp_path / "workspace", tmp_path / "installation", "ws")

    def retain(actual: q.CoreContext) -> None:
        actual.retained = True

    monkeypatch.setattr(q, "stop_core", retain)
    monkeypatch.setattr(q, "imported_job", lambda _context: pytest.fail("inspected a live writer"))
    with pytest.raises(q.QualificationError) as error:
        q.inspect_settled_import_job(object(), context)  # type: ignore[arg-type]
    assert _code(error) is Reason.CLEANUP_INCOMPLETE


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


def _wal_database(directory: Path) -> Path:
    """A WAL database closed cleanly, as a stopped Core leaves it: no side file remains."""
    directory.mkdir(parents=True)
    database = directory / "workspace.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.execute("INSERT INTO t VALUES ('kept')")
        connection.commit()
    return database


def test_inspection_opens_the_database_read_only_and_creates_nothing(tmp_path: Path) -> None:
    database = _wal_database(tmp_path / "workspace with space")
    assert [path.name for path in database.parent.iterdir()] == ["workspace.sqlite"]
    with contextlib.closing(q.read_only_database(database)) as connection:
        assert connection.execute("SELECT v FROM t").fetchall() == [("kept",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO t VALUES ('written')")
    # A plain `mode=ro` open of a WAL database would have left -wal and -shm here.
    assert [path.name for path in database.parent.iterdir()] == ["workspace.sqlite"]
    absent = tmp_path / "absent" / "workspace.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        q.read_only_database(absent)
    assert not absent.parent.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_inspection_refuses_a_database_its_writer_left_open(tmp_path: Path, suffix: str) -> None:
    database = _wal_database(tmp_path / "workspace")
    Path(f"{database}{suffix}").write_bytes(b"")
    with pytest.raises(q.QualificationError) as error:
        q.read_only_database(database)
    assert _code(error) is Reason.GATE_FAILED


def _sqlite_connect_owners(path: Path) -> list[str]:
    """The function enclosing each ``sqlite3.connect`` call in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owners = {
        id(node): function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
    }
    return [
        owners.get(id(node), "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "sqlite3.connect"
    ]


def test_every_harness_database_open_is_the_read_only_one() -> None:
    assert _sqlite_connect_owners(SCRIPT) == ["read_only_database"]
    assert _sqlite_connect_owners(AUTHORING_SCRIPT) == ["_read_only"]
    for script in (SCRIPT, AUTHORING_SCRIPT):
        assert "?mode=ro&immutable=1" in script.read_text(encoding="utf-8")


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
    auth = _token_file(tmp_path, name="auth.bin")
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
            for token in ((key,) if value is True else (key, str(value)))
        ]
        status = q.main(argv)
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    invoke.output = output  # type: ignore[attr-defined]
    return invoke


def _owned_runtime(monkeypatch: pytest.MonkeyPatch, parent: Path, receipt: object = None) -> Path:
    """A runtime root as the bootstrap leaves it: owner-only, directly under the
    harness parent, holding an owner-only receipt."""
    parent.mkdir(exist_ok=True)
    monkeypatch.setattr(q, "RUNTIME_PARENT", parent)
    root = parent / "ovmcp-real-test0001"
    (root / "candidate-venv" / "bin").mkdir(parents=True)
    root.chmod(0o700)
    q._write_private(root / q.RUNTIME_RECEIPT, json.dumps({} if receipt is None else receipt))
    return root


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
        q.require_owned_runtime(Path(argv[-1]))  # the root the child is handed proves ownership
        observed["owned"] = True
        raise q.QualificationError(Reason.ENTRYPOINT_UNRESOLVED)

    monkeypatch.setattr(q, "reexec_under_candidate", reexec)
    status, out, err = run()
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")
    assert observed["installed"] is installed
    assert observed["argv"][-2] == "--runtime-root"
    assert observed["owned"] is True
    assert Path(observed["argv"][-1]).parent == q.RUNTIME_PARENT
    assert not os.path.lexists(observed["argv"][-1])  # the parent's failure removed it
    assert observed["environ"]["PATH"] == q.SYSTEM_PATH
    assert set(observed["environ"]) == {"PATH", "LANG", "TMPDIR"}
    record = json.loads(run.output.read_text(encoding="utf-8"))
    assert (record["verdict"], record["reason_code"]) == (
        "fail",
        "entrypoint_unresolved",
    )


@posix_only
def test_bootstrap_permission_failure_removes_the_runtime_root(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "runtime-parent"
    parent.mkdir()
    monkeypatch.setattr(q, "RUNTIME_PARENT", parent)
    chmod = Path.chmod

    def refuse_runtime_mode(path: Path, mode: int, *args: Any, **kwargs: Any) -> None:
        if path.parent == parent and path.name.startswith(q.RUNTIME_PREFIX):
            raise PermissionError("mode refused")
        chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", refuse_runtime_mode)
    status, out, err = run()
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")
    assert list(parent.iterdir()) == []


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
    receipt = q.candidate_receipt(
        q.load_candidate(candidate), hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    )
    runtime = _owned_runtime(monkeypatch, tmp_path / f"parent-{host}", receipt)
    prefix = runtime / "candidate-venv"
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
def test_early_candidate_receipt_failure_still_removes_the_runtime(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _owned_runtime(monkeypatch, tmp_path / "parent", {"revision": REVISION})
    prefix = runtime / "candidate-venv"
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(q, "in_candidate_runtime", lambda *_: True)
    status, out, err = run(**{"--runtime-root": runtime})
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")
    assert not runtime.exists()


def _disown(root: Path, how: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Break one thing an owned runtime root proves; return the root then supplied."""
    receipt = root / q.RUNTIME_RECEIPT
    if how == "arbitrary_directory":
        receipt.unlink()
        return root.rename(root.with_name("project"))
    if how == "unprefixed_name":
        return root.rename(root.with_name("runtime-test0001"))
    if how == "nested_root":
        (root.parent / "nested").mkdir()
        return root.rename(root.parent / "nested" / root.name)
    if how == "working_directory":
        monkeypatch.chdir(root)
        return Path(".")
    if how == "missing_receipt":
        receipt.unlink()
    elif how == "readable_receipt":
        receipt.chmod(0o644)
    elif how == "shared_root":
        root.chmod(0o755)
    elif how == "symlinked_receipt":
        receipt.rename(root.with_name("receipt"))
        receipt.symlink_to(root.with_name("receipt"))
    elif how == "symlinked_root":
        root.symlink_to(root.rename(root.with_name("target")), target_is_directory=True)
    return root


@posix_only
@pytest.mark.parametrize(
    "how",
    [
        "arbitrary_directory",
        "unprefixed_name",
        "nested_root",
        "working_directory",
        "missing_receipt",
        "readable_receipt",
        "shared_root",
        "symlinked_receipt",
        "symlinked_root",
    ],
)
def test_a_runtime_root_the_harness_did_not_create_is_refused_and_never_deleted(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """A direct `--runtime-root` reaches no cleanup until it proves the bootstrap made it."""
    parent = tmp_path / "parent"
    root = _owned_runtime(monkeypatch, parent)
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    supplied = _disown(root, how, monkeypatch)
    before = sorted(str(path.relative_to(parent)) for path in parent.rglob("*"))
    status, out, err = run(**{"--runtime-root": supplied})
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")
    assert sorted(str(path.relative_to(parent)) for path in parent.rglob("*")) == before
    assert json.loads(run.output.read_text(encoding="utf-8"))["reason_code"] == "entrypoint_unresolved"


@posix_only
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("auth_file_changed", "authentication_unavailable"),
        ("candidate_changed", "wheel_digest_mismatch"),
        ("candidate_reload", "candidate_invalid"),
        ("schema_unreadable", "record_invalid"),
        ("not_the_candidate_runtime", "entrypoint_unresolved"),
    ],
)
def test_an_owned_runtime_root_is_removed_on_every_child_side_failure(
    run: Any,
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    code: str,
) -> None:
    """Preflight, the candidate reload and the schema digest are inside the cleanup boundary."""
    root = _owned_runtime(monkeypatch, tmp_path / "parent")
    overrides: dict[str, Any] = {"--runtime-root": root}
    if failure == "auth_file_changed":
        overrides["--auth-file"] = tmp_path / "absent-auth"
    elif failure == "candidate_changed":
        next(candidate.joinpath("wheels").glob("omnivia_core-*")).write_bytes(b"tampered")
    elif failure == "candidate_reload":
        loads: list[Path] = []
        load = q.load_candidate

        def reload(path: Path) -> Any:
            loads.append(path)
            if len(loads) > 1:
                raise q.QualificationError(Reason.CANDIDATE_INVALID)
            return load(path)

        monkeypatch.setattr(q, "load_candidate", reload)
    elif failure == "schema_unreadable":
        digest = q._file_digest

        def unreadable(path: Path) -> str:
            if path == SCHEMA:
                raise OSError("unreadable")
            return str(digest(path))

        monkeypatch.setattr(q, "_file_digest", unreadable)
    status, out, err = run(**overrides)
    assert (status, out, err) == (1, "", f"reason_code={code}\n")
    assert not os.path.lexists(root)


@posix_only
def test_a_child_side_failure_keeps_its_reason_when_the_owned_root_cannot_be_removed(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _owned_runtime(monkeypatch, tmp_path / "parent")

    def refuse(_path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(q.shutil, "rmtree", refuse)
    status, out, err = run(**{"--runtime-root": root, "--auth-file": tmp_path / "absent-auth"})
    assert (status, out) == (1, "")
    assert err == "reason_code=cleanup_incomplete\nreason_code=authentication_unavailable\n"
    record = json.loads(run.output.read_text(encoding="utf-8"))
    assert record["reason_code"] == "authentication_unavailable"


@posix_only
def test_a_malformed_claude_token_is_refused_and_never_echoed(
    run: Any, tmp_path: Path
) -> None:
    bad = _token_file(tmp_path, TOKEN.encode("ascii") + b"\nsecond-line\n", name="bad-token")
    status, out, err = run(**{"--auth-file": bad})
    assert (status, out, err) == (1, "", "reason_code=authentication_unavailable\n")
    record_text = run.output.read_text(encoding="utf-8")
    assert json.loads(record_text)["reason_code"] == "authentication_unavailable"
    assert "sk-ant" not in record_text and "second-line" not in record_text


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


# --- independent result, refusal and page observations ---------------------


def _outcome_result(
    requests: tuple[tuple[str, str], ...],
    outcomes: tuple[tuple[str, str, str], ...],
) -> Any:
    return q.HostRunResult(
        q.ObservationSummary(
            initialized=True,
            listed=True,
            listed_tools=q.AUTHORING_TOOLS,
            called=tuple(tool for tool, _ in requests),
            requests=requests,
            responded=tuple(tool for tool, _, _ in outcomes),
            succeeded=tuple(tool for tool, refusal, _ in outcomes if refusal == "none"),
            tool_errors=tuple(tool for tool, refusal, _ in outcomes if refusal != "none"),
            paused=False,
            withheld=False,
            violation=False,
            outcomes=outcomes,
        ),
        marker_seen=True,
        exited_cleanly=True,
        interrupted=False,
        paused=False,
    )


def _driver(tmp_path: Path, **kwargs: Any) -> Any:
    return q.HostDriver(
        "codex-cli",
        tmp_path / "codex",
        q.AuthFile(tmp_path / "auth"),
        object(),
        tmp_path / "core.json",
        tmp_path / "sessions",
        q.AUTHORING_TOOLS,
        **kwargs,
    )


def test_relay_keeps_result_digests_and_closed_refusal_classes_only(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    observer = q._Observer(path)
    relay = q._Relay(observer, None)

    def call(identifier: int, name: str) -> dict[str, Any]:
        arguments = {"input": {"secret": "do-not-retain"}}
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    def answer(identifier: int, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    conflict = {
        "isError": True,
        "content": [{"type": "text", "text": '{ "code" : "idempotency_conflict" }'}],
    }
    not_callable = {
        "isError": True,
        "content": [{"type": "text", "text": "The tool could not be called"}],
    }
    unknown = {"isError": True, "content": [{"type": "text", "text": "unrecognized detail"}]}
    captured = {
        "isError": False,
        "content": [],
        "structuredContent": {"evidence": [1], "page": {"continuation_token": "private-token"}},
    }
    for request, response in (
        (call(1, "evidence_capture"), answer(1, captured)),
        (call(2, "memory_create"), answer(2, conflict)),
        (call(3, "evidence_search"), answer(3, not_callable)),
        (call(4, "job_get"), answer(4, unknown)),
    ):
        relay.request(_frame(request))
        relay.response(_frame(response))
    observer.close()

    summary = q.summarize_observation(q.read_observation(path))
    assert summary.outcomes == (
        (
            "evidence_capture",
            "none",
            q.canonical_result_digest({"evidence": [1], "page": {"continuation_token": "other"}}),
        ),
        ("memory_create", "idempotency_conflict", q.canonical_result_digest(None)),
        ("evidence_search", "not_callable", q.canonical_result_digest(None)),
        ("job_get", "other", q.canonical_result_digest(None)),
    )
    text = path.read_text(encoding="ascii")
    for retained in ("do-not-retain", "private-token", "unrecognized", "could not be called"):
        assert retained not in text


def test_relay_refuses_a_success_without_structured_content(tmp_path: Path) -> None:
    observer = q._Observer(tmp_path / "events.jsonl")
    relay = q._Relay(observer, None)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "evidence_capture", "arguments": {}},
    }
    relay.request(_frame(request))
    with pytest.raises(q._Violation) as error:
        relay.response(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"isError": False, "content": []},
                }
            )
        )
    observer.close()
    assert error.value.kind == "invalid_tool_result"


@pytest.mark.parametrize(
    "result",
    [
        *(
            {"isError": True, "content": [], "structuredContent": value}
            for value in ({"code": "x"}, None, ["x"], "x", 0)
        ),
        {"isError": False, "content": []},
        *(
            {"isError": False, "content": [], "structuredContent": value}
            for value in (None, ["x"], "x", 0)
        ),
    ],
)
def test_relay_requires_structured_content_exactly_on_success(
    tmp_path: Path, result: dict[str, Any]
) -> None:
    observer = q._Observer(tmp_path / "events.jsonl")
    relay = q._Relay(observer, None)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "evidence_capture", "arguments": {}},
    }
    relay.request(_frame(request))
    with pytest.raises(q._Violation) as error:
        relay.response(_frame({"jsonrpc": "2.0", "id": 1, "result": result}))
    observer.close()
    assert error.value.kind == "invalid_tool_result"


def _refused(text: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def test_only_the_exact_installed_credential_message_is_credential_missing() -> None:
    assert (
        q.refusal_class(_refused("this  installation holds no\ncredential for that reference"))
        == "credential_missing"
    )
    # The server wrapper's real shape: the exact message wins over the generic phrase.
    wrapped = "evidence_capture could not be called: this installation holds no credential for that reference"
    assert q.refusal_class(_refused(wrapped)) == "credential_missing"
    assert q.refusal_class(_refused("The tool could not be called")) == "not_callable"
    for text in ("timed out", "transport closed", "request cancelled", "holds no credential"):
        assert q.refusal_class(_refused(text)) == "other"


def test_the_credential_missing_class_is_in_the_closed_vocabulary() -> None:
    event = {
        "event": "tool_call_response",
        "seq": 1,
        "tool": "job_get",
        "ok": True,
        "tool_error": True,
        "result_digest": "0" * 64,
        "refusal": "credential_missing",
    }
    assert q.validate_event(event) == event
    with pytest.raises(q.QualificationError):
        q.validate_event({**event, "refusal": "credential_gone"})


def test_a_retained_credential_missing_refusal_keeps_no_raw_text(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    observer = q._Observer(path)
    relay = q._Relay(observer, None)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "job_get", "arguments": {}},
    }
    message = "this installation holds no credential for that reference"
    relay.request(_frame(request))
    relay.response(_frame({"jsonrpc": "2.0", "id": 1, "result": _refused(message)}))
    observer.close()
    summary = q.summarize_observation(q.read_observation(path))
    assert summary.outcomes == (("job_get", "credential_missing", q.canonical_result_digest(None)),)
    assert "holds no credential" not in path.read_text(encoding="ascii")


_REVOKED_TOOL = "evidence_capture"
_REVOKED_ARGUMENTS = {"input": {}, "idempotency_key": "fixed"}
_REFUSED = q.canonical_result_digest(None)


@pytest.mark.parametrize(
    ("mode", "progress"),
    [
        ("exact", None),
        ("not_called", "host_refusal_mismatch"),
        ("other_tool", "host_refusal_mismatch"),
        ("extra_request", "host_refusal_mismatch"),
        ("wrong_arguments", "host_refusal_mismatch"),
        ("succeeded", "host_refusal_mismatch"),
        ("generic_error", "host_refusal_mismatch"),
        ("initialized_after_pause", "host_refusal_mismatch"),
        ("timed_out", "host_completion_mismatch"),
    ],
)
def test_refuse_revoked_requires_the_one_exact_request_refused_as_credential_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, progress: str | None
) -> None:
    digest = q.arguments_digest(_REVOKED_ARGUMENTS)
    other = q.arguments_digest({"input": {"other": True}, "idempotency_key": "fixed"})
    refused = ((_REVOKED_TOOL, "credential_missing", _REFUSED),)
    shapes = {
        "exact": (((_REVOKED_TOOL, digest),), refused),
        "not_called": ((), ()),
        "other_tool": ((("job_get", digest),), (("job_get", "credential_missing", _REFUSED),)),
        "extra_request": (((_REVOKED_TOOL, digest),) * 2, refused * 2),
        "wrong_arguments": (((_REVOKED_TOOL, other),), refused),
        "succeeded": (((_REVOKED_TOOL, digest),), ((_REVOKED_TOOL, "none", _REFUSED),)),
        "generic_error": (((_REVOKED_TOOL, digest),), ((_REVOKED_TOOL, "idempotency_conflict", _REFUSED),)),
        "initialized_after_pause": (((_REVOKED_TOOL, digest),), refused),
        "timed_out": (((_REVOKED_TOOL, digest),), refused),
    }
    requests, outcomes = shapes[mode]
    revoked: list[bool] = []

    def run(**kwargs: Any) -> Any:
        if mode == "not_called":
            return _outcome_result((), ())  # the request never reaches its pause
        kwargs["pause_before"].release.parent.mkdir(parents=True, exist_ok=True)
        kwargs["on_paused"]()
        result = _outcome_result(requests, outcomes)
        summary = dataclasses.replace(
            result.summary, paused=True, initialized_after_pause=mode == "initialized_after_pause"
        )
        timed_out = mode == "timed_out"
        return dataclasses.replace(
            result,
            summary=summary,
            paused=True,
            exited_cleanly=not timed_out,
            marker_seen=not timed_out,
        )

    monkeypatch.setattr(q, "run_host_session", run)
    log: list[str] = []
    driver = _driver(tmp_path, progress=log.append)
    if progress is None:
        driver.refuse_revoked(_REVOKED_TOOL, _REVOKED_ARGUMENTS, on_paused=lambda: revoked.append(True))
        assert revoked == [True]
        return
    with pytest.raises(q.QualificationError) as error:
        driver.refuse_revoked(_REVOKED_TOOL, _REVOKED_ARGUMENTS, on_paused=lambda: revoked.append(True))
    expected = Reason.HOST_OUTPUT_AMBIGUOUS if progress == "host_completion_mismatch" else Reason.GATE_FAILED
    assert _code(error) is expected
    assert log == [progress]


@pytest.mark.parametrize("drift", [None, "configuration", "principal"])
def test_a_regrant_keeps_the_configuration_path_and_rotates_the_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str | None
) -> None:
    """The service rotates the principal on every configure after a revoke (rotated=True)."""
    driver = _driver(tmp_path)
    config = tmp_path / "moved.json" if drift == "configuration" else driver.core_config
    # The stale principal is the one that was just revoked; a fresh grant must not reuse it.
    principal = "principal:revoked" if drift == "principal" else "principal:fresh"
    monkeypatch.setattr(q, "configure_profile", lambda *_args: config)
    monkeypatch.setattr(q, "configuration_principal", lambda _config: principal)
    if drift is None:
        assert q._regrant(object(), object(), "codex-cli", driver, "principal:revoked") == "principal:fresh"  # type: ignore[arg-type]
        return
    with pytest.raises(q.QualificationError) as error:
        q._regrant(object(), object(), "codex-cli", driver, "principal:revoked")  # type: ignore[arg-type]
    assert _code(error) is Reason.GATE_FAILED


def test_an_excluded_tool_refusal_has_its_own_closed_class() -> None:
    result = {
        "isError": True,
        "content": [
            {"type": "text", "text": "'job_cancel' is not a tool this server exposes."}
        ],
    }
    assert q.refusal_class(result) == "not_exposed"


def test_only_the_continuation_token_is_outside_the_result_digest() -> None:
    page = {"events": [{"sequence": 0}], "job_id": "job-1", "snapshot_event_count": 2}
    one_principal = {**page, "page": {"continuation_token": "token-for-one-principal"}}
    other_principal = {**page, "page": {"continuation_token": "token-for-the-owner"}}
    assert q.canonical_result_digest(one_principal) == q.canonical_result_digest(other_principal)
    # Every other page field, and every other result field, stays in the digest.
    assert q.canonical_result_digest(one_principal) != q.canonical_result_digest(
        {**page, "page": {"continuation_token": "t", "total": 3}}
    )
    assert q.canonical_result_digest({**page, "page": {"total": 3}}) != q.canonical_result_digest(
        {**page, "page": {"total": 4}}
    )
    assert q.canonical_result_digest({**page, "page": {}}) != q.canonical_result_digest(
        {**page, "page": {"total": 3}}
    )
    assert q.canonical_result_digest(page) != q.canonical_result_digest({**page, "page": {}})
    assert q.canonical_result_digest(one_principal) != q.canonical_result_digest(
        {**one_principal, "events": []}
    )
    assert q.canonical_result_digest(one_principal) != q.canonical_result_digest(
        {**one_principal, "page": "a-string-page"}
    )


def test_token_presence_is_in_the_digest_but_its_value_is_not() -> None:
    page = {"events": [{"sequence": 0}], "job_id": "job-1", "snapshot_event_count": 2}
    continuing = {**page, "page": {"continuation_token": "token-for-one-principal", "total": 2}}
    other = {**page, "page": {"continuation_token": "token-for-the-owner", "total": 2}}
    exhausted_none = {**page, "page": {"continuation_token": None, "total": 2}}
    exhausted_empty = {**page, "page": {"continuation_token": "", "total": 2}}
    exhausted_omitted = {**page, "page": {"total": 2}}
    assert q.canonical_result_digest(continuing) == q.canonical_result_digest(other)
    for exhausted in (exhausted_none, exhausted_empty, exhausted_omitted):
        assert q.canonical_result_digest(continuing) != q.canonical_result_digest(exhausted)
    assert q.canonical_result_digest(exhausted_none) == q.canonical_result_digest(exhausted_empty)


@pytest.mark.parametrize(
    "drift",
    [
        lambda d: {**d, "page": {**d["page"], "snapshot": 9}},
        lambda d: {**d, "events": [{**d["events"][0], "state": "failed"}]},
        lambda d: {**d, "snapshot_event_count": 3},
        lambda d: {k: v for k, v in d.items() if k != "page"},
    ],
)
def test_any_drift_outside_the_token_changes_the_digest(drift: Any) -> None:
    base = {
        "events": [{"sequence": 0, "state": "running"}],
        "job_id": "job-1",
        "snapshot_event_count": 2,
        "page": {"continuation_token": "owner-token", "total": 2},
    }
    other_token = {**base, "page": {**base["page"], "continuation_token": "host-token"}}
    assert q.canonical_result_digest(base) == q.canonical_result_digest(other_token)
    assert q.canonical_result_digest(drift(base)) != q.canonical_result_digest(base)


def test_refusal_classes_are_a_closed_vocabulary() -> None:
    event = {
        "event": "tool_call_response",
        "seq": 1,
        "tool": "job_get",
        "ok": True,
        "tool_error": True,
        "result_digest": "0" * 64,
        "refusal": "the service said no",
    }
    with pytest.raises(q.QualificationError) as error:
        q.validate_event(event)
    assert _code(error) is Reason.HOST_OUTPUT_AMBIGUOUS


@pytest.mark.parametrize(
    ("observed", "accepted"),
    [("idempotency_conflict", True), ("other", False), ("not_callable", False), ("none", False)],
)
def test_an_expected_refusal_must_be_the_observed_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed: str, accepted: bool
) -> None:
    tool = "evidence_capture"
    arguments = {"input": {}, "idempotency_key": "fixed"}
    digest = q.arguments_digest(arguments)
    outcome = (tool, observed, q.canonical_result_digest(None))
    monkeypatch.setattr(
        q,
        "run_host_session",
        lambda **_kwargs: _outcome_result(((tool, digest),), (outcome,)),
    )
    driver = _driver(tmp_path)
    if accepted:
        driver.call(tool, arguments, expected_error=True, refusal="idempotency_conflict")
        return
    with pytest.raises(q.QualificationError) as error:
        driver.call(tool, arguments, expected_error=True, refusal="idempotency_conflict")
    assert _code(error) is Reason.GATE_FAILED


_TRAVERSAL_TOOL = "job_events"
_TRAVERSAL_ARGUMENTS = {"job_id": "job-1", "limit": 1}


def _owner_pages(count: int) -> list[dict[str, Any]]:
    """Owner pages whose continuation tokens chain: page n is followed by token ``t{n}``."""
    return [
        {"events": [n], "page": {"continuation_token": f"t{n}"} if n < count - 1 else {}}
        for n in range(count)
    ]


def _chained_session(pages: list[dict[str, Any]], kind: str = "exact") -> Any:
    """One host session: its one call, then the proxy's pages, each as the owner page answers.

    ``kind`` names the way that session can be wrong.
    """
    base = dict(_TRAVERSAL_ARGUMENTS)
    requests: tuple[tuple[str, str], ...] = ((_TRAVERSAL_TOOL, q.arguments_digest(base)),)
    outcomes: tuple[tuple[str, str, str], ...] = (
        (_TRAVERSAL_TOOL, "none", q.canonical_result_digest(pages[0])),
    )
    chained = [(_TRAVERSAL_TOOL, q.canonical_result_digest(page)) for page in pages[1:]]
    if kind == "wrong_arguments":
        requests = ((_TRAVERSAL_TOOL, q.arguments_digest({**base, "limit": 2})),)
    elif kind == "extra_host_call":
        requests, outcomes = requests * 2, outcomes * 2
    elif kind == "tool_error":
        outcomes = ((_TRAVERSAL_TOOL, "idempotency_conflict", q.canonical_result_digest(None)),)
    elif kind == "wrong_first_page":
        outcomes = ((_TRAVERSAL_TOOL, "none", q.canonical_result_digest({"events": [99]})),)
    elif kind == "wrong_digest":
        chained[-1] = (_TRAVERSAL_TOOL, q.canonical_result_digest({"events": [99]}))
    elif kind == "short":
        chained = chained[:-1]
    elif kind == "extra":
        chained = [*chained, chained[-1]]
    result = _outcome_result(requests, outcomes)
    summary = dataclasses.replace(
        result.summary, chained=tuple(chained), violation=kind == "violation"
    )
    result = dataclasses.replace(result, summary=summary)
    if kind == "completion":
        return dataclasses.replace(result, exited_cleanly=False, marker_seen=False)
    return result


def _missing_session() -> Any:
    """A host session whose one call never happened: the target is not among ``called``."""
    return _outcome_result((), ())


def test_the_owner_pages_are_read_by_one_session_whose_proxy_chains_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _owner_pages(3)
    prompts: list[str] = []
    chains: list[Any] = []

    def run(**kwargs: Any) -> Any:
        prompts.append(kwargs["prompt"])
        chains.append(kwargs["chain"])
        return _chained_session(pages)

    monkeypatch.setattr(q, "run_host_session", run)
    driver = _driver(tmp_path)
    q.host_read_pages(driver, _TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages)
    assert driver.sequence == 1
    assert chains == [q.PageChain(_TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, 3)]
    # The host's prompt names only the first call: no continuation token, owner or host.
    payload = json.dumps(_TRAVERSAL_ARGUMENTS, sort_keys=True, separators=(",", ":"))
    assert "Call exactly one tool named job_events" in prompts[0]
    assert prompts[0].endswith(f"JSON:{payload}")
    assert "continuation" not in prompts[0]


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("wrong_arguments", Reason.GATE_FAILED),
        ("extra_host_call", Reason.GATE_FAILED),
        ("tool_error", Reason.GATE_FAILED),
        ("wrong_first_page", Reason.GATE_FAILED),
        ("wrong_digest", Reason.GATE_FAILED),
        ("short", Reason.GATE_FAILED),
        ("extra", Reason.GATE_FAILED),
        ("completion", Reason.HOST_OUTPUT_AMBIGUOUS),
        ("violation", Reason.PROTOCOL_VIOLATION),
    ],
)
def test_a_wrong_traversal_fails_closed_in_its_one_session_without_a_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    reason: Reason,
) -> None:
    pages = _owner_pages(3)
    monkeypatch.setattr(q, "run_host_session", lambda **_kwargs: _chained_session(pages, kind))
    log: list[str] = []
    driver = _driver(tmp_path, progress=log.append)
    with pytest.raises(q.QualificationError) as error:
        q.host_read_pages(driver, _TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages)
    assert _code(error) is reason
    assert driver.sequence == 1
    assert "host_target_call_missing" not in log


@pytest.mark.parametrize("bad_attempts", [1, 2])
def test_a_missing_target_is_retried_within_the_bound_before_any_page_is_chained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_attempts: int
) -> None:
    pages = _owner_pages(2)
    sessions = iter([_missing_session()] * bad_attempts + [_chained_session(pages)])
    monkeypatch.setattr(q, "run_host_session", lambda **_kwargs: next(sessions))
    log: list[str] = []
    driver = _driver(tmp_path, progress=log.append)
    q.host_read_pages(driver, _TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages)
    assert driver.sequence == bad_attempts + 1
    assert log == ["host_target_call_missing"] * bad_attempts


def test_a_missing_target_is_refused_after_three_sessions_without_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _owner_pages(2)
    monkeypatch.setattr(q, "run_host_session", lambda **_kwargs: _missing_session())
    log: list[str] = []
    driver = _driver(tmp_path, progress=log.append)
    with pytest.raises(q.QualificationError) as error:
        q.host_read_pages(driver, _TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages)
    assert _code(error) is Reason.GATE_FAILED
    assert driver.sequence == 3
    assert log == ["host_target_call_missing"] * 3


def test_a_page_that_answers_with_the_wrong_digest_is_named_as_a_page_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _owner_pages(2)
    monkeypatch.setattr(
        q, "run_host_session", lambda **_kwargs: _chained_session(pages, "wrong_digest")
    )
    log: list[str] = []
    driver = _driver(tmp_path, progress=log.append)
    with pytest.raises(q.QualificationError) as error:
        q.host_read_pages(driver, _TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages)
    assert _code(error) is Reason.GATE_FAILED
    assert log == ["host_page_mismatch"]


def test_core_health_is_checked_after_the_one_host_session_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _owner_pages(2)
    monkeypatch.setattr(q, "run_host_session", lambda **_kwargs: _chained_session(pages))
    driver = _driver(tmp_path, healthy=lambda: False)
    with pytest.raises(q.QualificationError) as error:
        q.host_read_pages(driver, _TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages)
    assert _code(error) is Reason.GATE_FAILED
    assert driver.sequence == 1


@pytest.mark.parametrize(
    ("arguments", "pages"),
    [
        ({"job_id": "job-1", "page": {}}, 2),
        ({"job_id": "job-1"}, 0),
        ({"job_id": "job-1"}, True),
        ({"job_id": "job-1"}, 1.0),
        ({"job_id": "job-1"}, q.MAX_EVENT_PAGES + 1),
    ],
)
def test_a_page_chain_refuses_a_token_bearing_base_or_an_unbounded_count(
    arguments: dict[str, Any], pages: Any
) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.PageChain(_TRAVERSAL_TOOL, arguments, pages)
    assert _code(error) is Reason.RECORD_INVALID


def test_the_private_spec_names_the_base_arguments_and_count_and_never_a_token(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "spec.json"
    chain = q.PageChain(_TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, 3)
    q.write_proxy_spec(spec, child=["server"], observation=tmp_path / "events", chain=chain)
    assert json.loads(spec.read_text(encoding="utf-8"))["chain"] == {
        "tool": _TRAVERSAL_TOOL,
        "arguments": _TRAVERSAL_ARGUMENTS,
        "pages": 3,
    }
    assert "continuation" not in spec.read_text(encoding="utf-8")
    assert q._load_spec(spec)[4] == chain


def _page_result(page: dict[str, Any]) -> dict[str, Any]:
    return {"isError": False, "content": [], "structuredContent": page}


def _page_frame(identifier: Any, result: dict[str, Any]) -> bytes:
    return _frame({"jsonrpc": "2.0", "id": identifier, "result": result})


def _call_frame(identifier: int) -> bytes:
    return _frame(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": _TRAVERSAL_TOOL, "arguments": _TRAVERSAL_ARGUMENTS},
        }
    )


def _paged_relay(directory: Path, pages: int) -> tuple[Any, Any, Path]:
    directory.mkdir()
    path = directory / "events.jsonl"
    observer = q._Observer(path)
    relay = q._Relay(observer, None, chain=q.PageChain(_TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, pages))
    return relay, observer, path


def _hold_first(relay: Any, sink: io.BytesIO, first: dict[str, Any]) -> None:
    """The host's one call is written to the child; its first page is answered and held."""
    relay.request(_call_frame(7), sink)
    assert not relay.response(_page_frame(7, _page_result(first)))
    assert relay.holding


def test_the_proxy_chains_each_page_on_the_connection_with_the_token_copied_verbatim(
    tmp_path: Path,
) -> None:
    pages = [
        {"events": [0], "page": {"continuation_token": "secret-token-one"}},
        {"events": [1], "page": {"continuation_token": "secret-token-two"}},
        {"events": [2], "page": {}},
    ]
    relay, observer, path = _paged_relay(tmp_path / "exact", 3)
    child_in = io.BytesIO()
    _hold_first(relay, child_in, pages[0])
    child_out = io.BytesIO(
        _page_frame("omnivia-page-1", _page_result(pages[1]))
        + _page_frame("omnivia-page-2", _page_result(pages[2]))
    )
    relay.chain_pages(child_in, child_out)
    relay.release()
    observer.close()
    assert not relay.holding
    sent = [json.loads(line) for line in child_in.getvalue().splitlines()]
    assert [message["id"] for message in sent] == [7, "omnivia-page-1", "omnivia-page-2"]
    assert sent[1]["params"]["arguments"] == {
        **_TRAVERSAL_ARGUMENTS,
        "page": {"continuation_token": "secret-token-one"},
    }
    assert sent[2]["params"]["arguments"] == {
        **_TRAVERSAL_ARGUMENTS,
        "page": {"continuation_token": "secret-token-two"},
    }
    events = q.read_observation(path)
    assert [event["event"] for event in events] == [
        "tool_call_request",
        "tool_call_response",
        "page_request",
        "page_response",
        "page_request",
        "page_response",
    ]
    # Tokens and page bodies are never retained: only closed names and canonical digests.
    assert "secret-token" not in path.read_text(encoding="ascii")
    summary = q.summarize_observation(events)
    assert summary.called == (_TRAVERSAL_TOOL,) and not summary.violation
    assert summary.chained == (
        (_TRAVERSAL_TOOL, q.canonical_result_digest(pages[1])),
        (_TRAVERSAL_TOOL, q.canonical_result_digest(pages[2])),
    )


def test_the_held_answer_waits_for_release_and_no_host_frame_is_admitted_meanwhile(
    tmp_path: Path,
) -> None:
    relay, observer, _path = _paged_relay(tmp_path / "held", 2)
    child_in = io.BytesIO()
    _hold_first(relay, child_in, {"events": [0], "page": {"continuation_token": "t0"}})
    with pytest.raises(q._Violation) as error:
        relay.request(_frame({"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}}), child_in)
    assert error.value.kind == "host_frame_during_injection"
    relay.chain_pages(child_in, io.BytesIO(_page_frame("omnivia-page-1", _page_result({"events": [1], "page": {}}))))
    assert relay.holding  # the chain alone never lifts the hold
    relay.release()
    observer.close()
    assert not relay.holding


def test_the_host_drain_waits_out_a_hold_before_closing_the_childs_input(tmp_path: Path) -> None:
    relay, observer, _path = _paged_relay(tmp_path / "drain", 1)
    _hold_first(relay, io.BytesIO(), {"events": [0], "page": {}})
    waiter = threading.Thread(target=relay.drain, args=(5,))
    waiter.start()
    waiter.join(0.2)
    assert waiter.is_alive()
    relay.chain_pages(io.BytesIO(), io.BytesIO())
    relay.release()
    waiter.join(5)
    observer.close()
    assert not waiter.is_alive()


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (_frame({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}), "interleaved_frame"),
        (_page_frame("omnivia-page-9", _page_result({"events": [1], "page": {"continuation_token": "t1"}})), "wrong_page_id"),
        (_frame({"jsonrpc": "2.0", "id": "omnivia-page-1", "error": {"code": -1, "message": "x"}}), "page_tool_error"),
        (_page_frame("omnivia-page-1", {**_page_result({"events": [1], "page": {}}), "isError": True}), "page_tool_error"),
        (_page_frame("omnivia-page-1", {"isError": False, "content": []}), "invalid_tool_result"),
        (_page_frame("omnivia-page-1", _page_result({"events": [1], "page": {}})), "page_token_missing"),
        (b"", "early_eof"),
        (b"not-json\n", "not_json"),
        (b"x" * (q.MAX_FRAME_BYTES + 1), "oversized_frame"),
    ],
)
def test_a_faulty_chained_reply_is_a_violation_and_the_held_answer_is_never_released(
    tmp_path: Path, reply: bytes, kind: str
) -> None:
    relay, observer, _path = _paged_relay(tmp_path / kind, 3)
    _hold_first(relay, io.BytesIO(), {"events": [0], "page": {"continuation_token": "t0"}})
    with pytest.raises(q._Violation) as error:
        relay.chain_pages(io.BytesIO(), io.BytesIO(reply))
    observer.close()
    assert error.value.kind == kind
    assert relay.holding


def test_the_final_chained_page_must_be_exhausted(tmp_path: Path) -> None:
    relay, observer, _path = _paged_relay(tmp_path / "final", 2)
    _hold_first(relay, io.BytesIO(), {"events": [0], "page": {"continuation_token": "t0"}})
    still_more = _page_frame("omnivia-page-1", _page_result({"events": [1], "page": {"continuation_token": "t1"}}))
    with pytest.raises(q._Violation) as error:
        relay.chain_pages(io.BytesIO(), io.BytesIO(still_more))
    observer.close()
    assert error.value.kind == "page_not_exhausted"


@pytest.mark.parametrize(
    ("frame", "pages", "kind"),
    [
        (_page_frame(7, _page_result({"events": [0], "page": {}})), 2, "page_token_missing"),
        (_page_frame(7, _page_result({"events": [0], "page": {"continuation_token": "t0"}})), 1, "page_not_exhausted"),
        (_page_frame(7, {"isError": False, "content": []}), 2, "invalid_tool_result"),
        (_page_frame(7, {"isError": True, "content": []}), 2, "page_tool_error"),
    ],
)
def test_the_first_page_must_carry_the_traversal_shape_before_any_hold(
    tmp_path: Path, frame: bytes, pages: int, kind: str
) -> None:
    relay, observer, _path = _paged_relay(tmp_path / kind, pages)
    relay.request(_call_frame(7), io.BytesIO())
    with pytest.raises(q._Violation) as error:
        relay.response(frame)
    observer.close()
    assert error.value.kind == kind
    assert not relay.holding


def test_a_second_host_request_pending_at_the_hold_is_an_interleaving(tmp_path: Path) -> None:
    relay, observer, _path = _paged_relay(tmp_path / "pending", 2)
    sink = io.BytesIO()
    relay.request(_call_frame(7), sink)
    relay.request(_frame({"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}}), sink)
    with pytest.raises(q._Violation) as error:
        relay.response(_page_frame(7, _page_result({"events": [0], "page": {"continuation_token": "t0"}})))
    observer.close()
    assert error.value.kind == "interleaving"


_PAGED_CHILD = r'''
import json, sys
mode, log_path = sys.argv[1], sys.argv[2]
pages = [
    {"events": [0], "page": {"continuation_token": "tok-1"}},
    {"events": [1], "page": {"continuation_token": "tok-2"}},
    {"events": [2], "page": {}},
]
log = open(log_path, "a")
def send(message):
    print(json.dumps(message, separators=(",", ":")), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method, identifier = message.get("method"), message.get("id")
    log.write(f"{method}:{identifier}\n"); log.flush()
    if identifier is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "job_events"}]}
    else:
        arguments = message["params"]["arguments"]
        index = 0 if "page" not in arguments else int(arguments["page"]["continuation_token"].split("-")[1])
        page = dict(pages[index])
        if mode == "token_missing" and index == 0:
            page = {"events": [0], "page": {}}
        if mode == "notify" and index == 1:
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
        reply_id = "other" if mode == "wrong_id" and index == 1 else identifier
        send({"jsonrpc": "2.0", "id": reply_id, "result": {"content": [], "isError": False, "structuredContent": page}})
        if mode == "eof" and index == 0:
            sys.exit(0)
        continue
    send({"jsonrpc": "2.0", "id": identifier, "result": result})
'''


def _paged_proxy(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[bytes], Path, Path]:
    directory = tmp_path / mode
    directory.mkdir()
    observation, spec, log = directory / "events", directory / "spec", directory / "child-log"
    q.write_proxy_spec(
        spec,
        child=[sys.executable, "-u", "-c", _PAGED_CHILD, mode, str(log)],
        observation=observation,
        chain=q.PageChain(_TRAVERSAL_TOOL, _TRAVERSAL_ARGUMENTS, 3),
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": _TRAVERSAL_TOOL, "arguments": _TRAVERSAL_ARGUMENTS}},
    ]
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=b"".join(_frame(message) for message in messages),
        capture_output=True,
        check=False,
        timeout=20,
    )
    return completed, observation, log


@posix_only
def test_the_proxy_pages_on_the_child_that_initialized_and_releases_the_first_answer_last(
    tmp_path: Path,
) -> None:
    completed, observation, log = _paged_proxy(tmp_path, "exact")
    assert completed.returncode == 0 and completed.stderr == b""
    host = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [message["id"] for message in host] == [1, 2, 3]
    assert host[2]["result"]["structuredContent"]["page"] == {"continuation_token": "tok-1"}
    # One child process: it initialized once, then served the host's call and both pages.
    assert log.read_text(encoding="utf-8").splitlines() == [
        "initialize:1",
        "notifications/initialized:None",
        "tools/list:2",
        "tools/call:3",
        "tools/call:omnivia-page-1",
        "tools/call:omnivia-page-2",
    ]
    summary = q.summarize_observation(q.read_observation(observation))
    assert not summary.violation and summary.called == (_TRAVERSAL_TOOL,)
    assert summary.chained == (
        (_TRAVERSAL_TOOL, q.canonical_result_digest({"events": [1], "page": {"continuation_token": "tok-2"}})),
        (_TRAVERSAL_TOOL, q.canonical_result_digest({"events": [2], "page": {}})),
    )
    assert "tok-" not in observation.read_text(encoding="ascii")


@posix_only
@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("notify", "interleaved_frame"),
        ("wrong_id", "wrong_page_id"),
        ("token_missing", "page_token_missing"),
        ("eof", None),
    ],
)
def test_a_faulty_traversal_never_releases_the_held_first_answer(
    tmp_path: Path, mode: str, kind: str | None
) -> None:
    completed, observation, _log = _paged_proxy(tmp_path, mode)
    host = [json.loads(line) for line in completed.stdout.splitlines()]
    # Only the two answers before the traversal reached the host; the held one never did.
    assert [message["id"] for message in host] == [1, 2]
    assert completed.returncode in {q.PROXY_VIOLATION_EXIT, q.PROXY_FAILED_EXIT}
    events = q.read_observation(observation)
    observed = [event["kind"] for event in events if event["event"] == "protocol_violation"]
    if kind is None:
        assert observed in ([], ["early_eof"])
    else:
        assert observed == [kind]


def _event(sequence: int) -> dict[str, Any]:
    return {"sequence": sequence, "occurred_at": "2026-10-03T09:00:00Z", "state": "running"}


def _page(events: list[dict[str, Any]], snapshot: int, token: str | None = None) -> dict[str, Any]:
    return {
        "job_id": "job-1",
        "events": events,
        "snapshot_event_count": snapshot,
        "page": {} if token is None else {"continuation_token": token},
    }


def test_verified_event_stream_accepts_one_contiguous_snapshot() -> None:
    pages = [_page([_event(0)], 2, "t1"), _page([_event(1)], 2)]
    assert [event["sequence"] for event in q.verified_event_stream(pages, 1)] == [0, 1]


@pytest.mark.parametrize(
    "pages",
    [
        [_page([_event(0)], 2, "t1"), _page([_event(2)], 2)],  # a gap
        [_page([_event(0)], 2, "t1"), _page([_event(0)], 2)],  # a repeat
        [_page([_event(0)], 2, "t1"), _page([_event(1)], 3)],  # the snapshot moved
        [_page([_event(0), _event(1)], 2)],  # more than the page limit
        [_page([_event(0)], 2)],  # short of the snapshot
    ],
)
def test_verified_event_stream_refuses_any_gap_or_drift(pages: list[dict[str, Any]]) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.verified_event_stream(pages, 1)
    assert _code(error) is Reason.GATE_FAILED


def test_owner_event_pages_follow_each_token_until_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any] | None] = []
    replies = [_page([_event(0)], 2, "t1"), _page([_event(1)], 2)]

    def owner(_installed: Any, _context: Any, _path: Any, payload: Any = None) -> Any:
        calls.append(payload)
        return replies.pop(0)

    monkeypatch.setattr(q, "owner_call", owner)
    pages = q.owner_event_pages(object(), object(), "job-1", 1)  # type: ignore[arg-type]
    assert len(pages) == 2
    assert calls == [
        {"job_id": "job-1", "limit": 1},
        {"job_id": "job-1", "limit": 1, "page": {"continuation_token": "t1"}},
    ]


# --- the real-host journey over an in-memory Core and host -----------------

JOB_EVENTS: list[dict[str, Any]] = [
    {"sequence": 0, "occurred_at": "2026-10-03T09:00:00Z", "state": "running"},
    {"sequence": 1, "occurred_at": "2026-10-03T09:00:01Z", "state": "succeeded"},
]
JOURNEY_STAGED: dict[str, Any] = {
    "staged_source_ref": "ref-1",
    "source_kind": "staged_file",
    "content_checksum": "c" * 64,
    "content_length_bytes": 24,
    "media_type": "text/plain",
}
MUTATIONS = frozenset({"evidence_capture", "memory_create", "import_start"})


class FakeCore:
    """The Core behaviour the journey depends on: keyed writes, replays, revocation and pages.

    Evidence rows are (searchable text, artifact).  The trusted staging capture and
    the import's own artifact are separate rows: both carry the staged bytes' kind,
    checksum and media type, and only the import's is bound to its run, under an
    identity of its own rather than the staged source id.
    """

    def __init__(self, tamper: str | None) -> None:
        self.tamper = tamper
        self.revoked = False
        self.keyed: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.evidence: list[tuple[str, dict[str, Any]]] = []
        self.memories: list[dict[str, Any]] = []
        self.jobs: list[str] = []
        #: The process evidence the descriptor publishes for the Core now serving.
        self.serving: dict[str, Any] | None = None
        self.generation = 0

    def replace(self) -> dict[str, Any]:
        """A new Core serves the workspace and publishes its own process evidence."""
        self.generation += 1
        self.serving = {"pid": 1000 + self.generation, "start_time": f"s{self.generation}", "boot_id": "b"}
        return dict(self.serving)

    def single_job(self) -> str:
        if len(self.jobs) != 1:
            raise q.QualificationError(Reason.GATE_FAILED)
        return self.jobs[0]

    def stage(self, staged: dict[str, Any]) -> dict[str, Any]:
        """The trusted staging capture, which is itself evidence of the staged bytes."""
        self._staged_artifact(staged, q.STAGED_SOURCE_ID)
        return staged

    def _staged_artifact(self, staged: dict[str, Any], source_id: str, **binding: str) -> None:
        kind = staged["source_kind"]
        artifact = {
            "source": {"kind": kind, "source_id": source_id},
            "content_checksum": staged["content_checksum"],
            "media_type": staged["media_type"],
            **binding,
        }
        self.evidence.append((f"{kind} {source_id} staged import {q.QUALIFICATION_TOKEN}", artifact))

    def _create(self, tool: str, payload: Any) -> dict[str, Any]:
        if tool == "evidence_capture":
            native = payload["source_native_id"]
            artifact = {"source": {"kind": "direct_submission", "source_id": native}}
            self.evidence.append((f"{native} {payload['text']}", artifact))
            return {"evidence": {"source_native_id": native}}
        if tool == "memory_create":
            self.memories.append({"fact": payload["content"]["fact"]})
            return {"record": {"fact": payload["content"]["fact"]}}
        job = f"job-{len(self.jobs) + 1}"
        self.jobs.append(job)
        source = dict(payload["source"])
        if self.tamper == "imported_evidence_other_bytes":
            source["content_checksum"] = "sha256:" + "d" * 64
        if self.tamper != "import_without_evidence":
            self._staged_artifact(source, f"imp-{job}", import_run_id=job)
        return {"job": {"job_id": job, "state": "succeeded"}}

    def mutate(self, tool: str, arguments: dict[str, Any]) -> tuple[str, Any]:
        if self.revoked and self.tamper != "revocation_ignored":
            return "credential_missing", None
        key, payload = arguments["idempotency_key"], arguments["input"]
        if key not in self.keyed:
            result = self._create(tool, payload)
            self.keyed[key] = (payload, result)
            return "none", result
        stored_payload, stored = self.keyed[key]
        if payload != stored_payload and self.tamper != "accept_conflict":
            return "idempotency_conflict", None
        if self.tamper == "replay_changes_result":
            return "none", {**stored, "replayed": True}
        if self.tamper == "duplicate_job" and tool == "import_start":
            return "none", self._create(tool, payload)
        return "none", stored

    def read(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool == "evidence_search":
            matched = [artifact for text, artifact in self.evidence if arguments["query"] in text]
            return {"evidence": matched[: arguments.get("limit", 1000)], "page": {}}
        if tool == "memory_search":
            return {"records": list(self.memories) if arguments.get("view") == "candidates" else []}
        if tool == "job_get":
            return {"job": {"job_id": arguments["job_id"], "state": "succeeded"}}
        if tool == "job_events":
            start = int(arguments["page"]["continuation_token"].split(":")[1]) if "page" in arguments else 0
            rows = JOB_EVENTS[start : start + arguments.get("limit", 1000)]
            if self.revoked and self.tamper == "owner_events_changed_after_revoke":
                rows = [{**row, "state": "failed"} for row in rows]
            next_at = start + len(rows)
            page = {"continuation_token": f"offset:{next_at}"} if next_at < len(JOB_EVENTS) else {}
            return {
                "job_id": arguments["job_id"],
                "events": rows,
                "snapshot_event_count": len(JOB_EVENTS),
                "page": page,
            }
        return {}

    def serve(self, tool: str, arguments: dict[str, Any]) -> tuple[str, Any]:
        if tool == "decision_evaluate":
            # No decision capability is granted: refused before any record is written.
            return "capability_not_granted", None
        if tool in MUTATIONS:
            return self.mutate(tool, arguments)
        if self.revoked and self.tamper != "revocation_ignored" and tool != "workspace_inspect":
            return "credential_missing", None
        return "none", self.read(tool, arguments)


def _in_order(log: list[str], needles: list[str]) -> bool:
    position = 0
    for needle in needles:
        if needle not in log[position:]:
            return False
        position = log.index(needle, position) + 1
    return True


def _tampered(
    tamper: str | None, steps: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, dict[str, Any]]]:
    """What the host serves of one admitted session's request, with post-revocation tampers applied."""
    name, arguments = steps[0]
    if tamper == "other_tool_called":
        return [("job_cancel", {})]
    if tamper == "extra_request":
        return [steps[0], steps[0]]
    if tamper == "changed_arguments":
        return [(name, {**arguments, "tampered": True})]
    return steps


def _journey(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    tamper: str | None = None,
    log: list[str] | None = None,
    ledger: q.GateLedger | None = None,
) -> tuple[q.GateLedger, list[str]]:
    cores: dict[Path, FakeCore] = {}
    log = [] if log is None else log
    # Each configure rotates the principal, as the service does; the path never moves.
    configures: dict[Path, int] = {}

    def initialize(_installed: Any, path: Path) -> Any:
        cores[path] = FakeCore(tamper)
        return q.CoreContext(path, path / "workspace", path / "installation", f"ws-{path.name}")

    def start(_installed: Any, context: Any) -> int:
        log.append(f"start:{context.root.name}")
        context.expected = cores[context.root].replace()
        return 1

    def stop(context: Any) -> None:
        log.append(f"stop:{context.root.name}")

    def restart(_installed: Any, context: Any) -> None:
        log.append(f"restart:{context.root.name}")
        context.expected = cores[context.root].replace()

    def revoke(_installed: Any, context: Any, _host: str) -> None:
        cores[context.root].revoked = True
        log.append(f"revoke:{context.root.name}")

    def owner(_installed: Any, context: Any, path: tuple[str, ...], payload: Any = None) -> dict[str, Any]:
        if path == ("service", "health"):
            status = (
                "fail"
                if cores[context.root].tamper == "unhealthy_after_host_exit"
                else "pass"
            )
            return {"status": status}
        if path == ("decisions", "status"):
            return {"enabled": False}
        if path == ("decisions", "records"):
            return {"records": []}
        names = {
            ("evidence", "search"): "evidence_search",
            ("memory", "search"): "memory_search",
            ("job", "get"): "job_get",
            ("job", "events"): "job_events",
        }
        return cores[context.root].read(names[path], dict(payload or {}))

    def run(self: Any, calls: Any, arguments: Any = None, *, interrupt: bool = False,
            on_withheld: Any = lambda: None, pause_before: bool = False, on_paused: Any = lambda: None,
            chain: Any = None) -> Any:
        tool, first = calls, arguments
        steps = [(tool, first)]
        core = cores[Path(self.core_config).parent]
        digest = q.arguments_digest(first)
        log.append(f"host:{tool}:{'interrupt' if interrupt else 'pause' if pause_before else 'call'}")
        if core.revoked:
            # Launch authority is gone: a process started now cannot initialize (the live failure).
            self.progress("host_initialize_missing")
            raise q.QualificationError(Reason.GATE_FAILED)
        if pause_before and core.tamper == "target_not_called":
            # The session never reaches its paused request, so no revocation can land.
            return q.HostRunResult(
                q.ObservationSummary(
                    initialized=True, listed=True, listed_tools=self.tools, called=(),
                    requests=(), responded=(), succeeded=(), tool_errors=(), paused=False,
                    withheld=False, violation=False,
                ),
                marker_seen=True, exited_cleanly=True, interrupted=False, paused=False,
            )
        if pause_before:
            on_paused()
        if interrupt:
            refusal, committed = core.mutate(tool, first)  # committed; the response is then withheld
            on_withheld()
            log.append(f"exit:{tool}")
            withheld = q.canonical_result_digest(committed) if refusal == "none" else None
            if core.tamper == "replay_differs_from_withheld":
                withheld = q.canonical_result_digest({**committed, "withheld": True})
            elif core.tamper == "withheld_answer_refused":
                withheld = None
            return q.HostRunResult(
                q.ObservationSummary(
                    initialized=True, listed=True, listed_tools=self.tools,
                    called=(tool,), requests=((tool, digest),), responded=(), succeeded=(),
                    tool_errors=(), paused=False, withheld=True, violation=False,
                    withheld_digest=withheld,
                ),
                marker_seen=False, exited_cleanly=False, interrupted=True, paused=False,
            )
        requests: list[tuple[str, str]] = []
        outcomes: list[tuple[str, str, str]] = []
        chained: list[tuple[str, str]] = []

        def served_result(name: str, args: dict[str, Any]) -> tuple[str, Any]:
            refusal, structured = core.serve(name, args)
            if core.tamper == "wrong_refusal" and refusal == "credential_missing":
                refusal = "idempotency_conflict"
            if refusal == "none" and name == "job_events" and core.tamper == "host_page_differs":
                structured = {**structured, "events": [{**structured["events"][0], "state": "failed"}, *structured["events"][1:]]}
            return refusal, structured

        # Only the paused post-revocation request is tampered; earlier sessions are honest.
        served = _tampered(core.tamper, steps) if pause_before else steps
        for name, args in served:
            log.append(f"call:{name}")
            requests.append((name, q.arguments_digest(args)))
            refusal, structured = served_result(name, args)
            outcomes.append((name, refusal, q.canonical_result_digest(structured)))
        if chain is not None:
            # The proxy's own pages, on the host's connection: each token copied from the page before.
            token = structured["page"].get("continuation_token")
            for _ in range(chain.pages - 1):
                log.append(f"call:{chain.tool}")
                refusal, page = served_result(chain.tool, {**chain.arguments, "page": {"continuation_token": token}})
                assert refusal == "none"
                chained.append((chain.tool, q.canonical_result_digest(page)))
                token = page["page"].get("continuation_token")
        if core.tamper == "core_replaced_unexpectedly" and tool == "evidence_capture":
            core.replace()  # Core exited mid-session; a managed-local client replaced it
        if not self.healthy():
            raise q.QualificationError(Reason.GATE_FAILED)
        timed_out = pause_before and core.tamper == "times_out"
        return q.HostRunResult(
            q.ObservationSummary(
                initialized=True, listed=True, listed_tools=self.tools,
                called=tuple(name for name, _ in requests), requests=tuple(requests),
                responded=tuple(name for name, _, _ in outcomes),
                succeeded=tuple(name for name, refusal, _ in outcomes if refusal == "none"),
                tool_errors=tuple(name for name, refusal, _ in outcomes if refusal != "none"),
                paused=pause_before, withheld=False, violation=False, outcomes=tuple(outcomes),
                initialized_after_pause=pause_before and core.tamper == "initialized_after_pause",
                chained=tuple(chained),
            ),
            marker_seen=not timed_out, exited_cleanly=not timed_out, interrupted=False,
            paused=pause_before,
        )

    monkeypatch.setattr(q, "initialize_core", initialize)
    monkeypatch.setattr(q, "start_core", start)
    monkeypatch.setattr(q, "stop_core", stop)
    monkeypatch.setattr(q, "restart_core", restart)
    # The descriptor check of the real `core_healthy`; its health probe is `owner` below.
    monkeypatch.setattr(q, "core_alive", lambda context: cores[context.root].serving == context.expected)
    monkeypatch.setattr(q, "revoke_authoring", revoke)

    def probe(*_args: Any) -> None:
        # A dispatched excluded name is refused by the deterministic probe, not by a model run.
        if tamper == "excluded_dispatched":
            raise q.QualificationError(Reason.GATE_FAILED)

    def configure(_installed: Any, context: Any, _host: str, profile: str) -> Path:
        # A configure grants fresh authority: the revoked state is cleared, the principal
        # rotates, and the configuration path stays the one this Core owns.
        cores[context.root].revoked = False
        configures[context.root] = configures.get(context.root, 0) + 1
        log.append(f"configure:{context.root.name}")
        return context.root / f"{profile}.json"

    def verify(_installed: Any, context: Any, _host: str) -> None:
        if not cores[context.root].revoked:
            raise q.QualificationError(Reason.GATE_FAILED)
        log.append(f"verified:{context.root.name}")

    monkeypatch.setattr(q, "probe_excluded_tools", probe)
    monkeypatch.setattr(q, "owner_call", owner)
    monkeypatch.setattr(
        q, "stage_source", lambda _installed, context: cores[context.root].stage(dict(JOURNEY_STAGED))
    )
    monkeypatch.setattr(q, "configure_profile", configure)
    monkeypatch.setattr(
        q,
        "configuration_principal",
        lambda config: f"principal:{config.stem}:{configures[config.parent]}",
    )
    monkeypatch.setattr(q, "verify_revoked", verify)
    monkeypatch.setattr(q, "imported_job", lambda context: cores[context.root].single_job())
    monkeypatch.setattr(q.HostDriver, "_run", run)
    ledger = q.GateLedger() if ledger is None else ledger
    q.qualify_host(
        host="codex-cli",
        binary=root / "codex",
        auth=q.AuthFile(root / "auth"),
        installed=object(),  # type: ignore[arg-type]
        run_root=root,
        progress=log.append,
        ledger=ledger,
    )
    return ledger, log


def test_an_initialize_after_the_pause_is_observed_as_a_substitution() -> None:
    before = [{"event": "initialize_request"}, {"event": "request_paused", "tool": "job_get"}]
    assert not q.summarize_observation(before).initialized_after_pause
    after = [*before, {"event": "initialize_request"}]
    assert q.summarize_observation(after).initialized_after_pause


def test_the_journey_proves_every_gate_in_the_pinned_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, log = _journey(monkeypatch, tmp_path)
    assert ledger.all_passed()
    # interrupt -> host exit -> Core restart -> same-key replay by a fresh host
    assert _in_order(
        log,
        [
            "host:evidence_capture:interrupt",
            "exit:evidence_capture",
            "restart:core",
            "host:evidence_capture:call",
        ],
    )
    # One admitted session per refused request: the paused request, its revocation, the
    # refused call, then a regrant before the next session is paused.  Nothing follows a
    # refusal inside the same session, so no multi-call dependency survives a refusal.
    authoring = log.index("host:evidence_capture:pause")
    assert log[authoring : authoring + 14] == [
        "host:evidence_capture:pause",
        "revoke:core",
        "verified:core",
        "call:evidence_capture",
        "configure:core",
        "host:evidence_capture:pause",
        "revoke:core",
        "verified:core",
        "call:evidence_capture",
        "configure:core",
        "host:memory_create:pause",
        "revoke:core",
        "verified:core",
        "call:memory_create",
    ]
    imported = log.index("host:job_get:pause")
    assert log[imported : imported + 14] == [
        "host:job_get:pause",
        "revoke:import-core",
        "verified:import-core",
        "call:job_get",
        "configure:import-core",
        "host:job_events:pause",
        "revoke:import-core",
        "verified:import-core",
        "call:job_events",
        "configure:import-core",
        "host:import_start:pause",
        "revoke:import-core",
        "verified:import-core",
        "call:import_start",
    ]
    # Both contexts end revoked and are re-verified after the final refusal, before shutdown.
    final = log.index("call:import_start", imported) + 1
    assert log[final : final + 2] == ["verified:core", "verified:import-core"]
    # The import's paged read is one host session: its one call, the proxy's own second page,
    # and the revocation probe's one call.  No page is read by a fresh host session.
    assert log.count("host:job_events:call") == 1
    assert log.count("call:job_events") == 3


@pytest.mark.parametrize(
    "tamper",
    [
        "accept_conflict",
        "replay_changes_result",
        "duplicate_job",
        "host_page_differs",
        "revocation_ignored",
        "excluded_dispatched",
        # Only the trusted staging capture exists: it must not pass as imported evidence.
        "import_without_evidence",
        "imported_evidence_other_bytes",
        # The same-key replay must answer exactly what the withheld response said.
        "replay_differs_from_withheld",
        "withheld_answer_refused",
    ],
)
def test_a_journey_that_violates_one_observation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    with pytest.raises(q.QualificationError) as error:
        _journey(monkeypatch, tmp_path, tamper)
    assert _code(error) is Reason.GATE_FAILED


@pytest.mark.parametrize(
    ("tamper", "progress"),
    [
        # The paused session never reaches its request, so nothing can be revoked under it.
        ("target_not_called", "host_refusal_mismatch"),
        # The session serves a different tool, or the target plus an extra request.
        ("other_tool_called", "host_refusal_mismatch"),
        ("extra_request", "host_refusal_mismatch"),
        # The request is served with arguments other than the exact planned ones.
        ("changed_arguments", "host_refusal_mismatch"),
        # The refusal is a different class than credential_missing.
        ("wrong_refusal", "host_refusal_mismatch"),
        # A second initialize after the pause means the process was substituted.
        ("initialized_after_pause", "host_refusal_mismatch"),
        # An accepted replay or read after revocation is a failure, not a pass.
        ("revocation_ignored", "host_refusal_mismatch"),
        # The paused session never completes; that is a completion failure, not a retry.
        ("times_out", "host_completion_mismatch"),
    ],
)
def test_a_post_revocation_session_that_deviates_is_refused_at_its_own_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str, progress: str
) -> None:
    log: list[str] = []
    with pytest.raises(q.QualificationError) as error:
        _journey(monkeypatch, tmp_path, tamper, log)
    expected = Reason.HOST_OUTPUT_AMBIGUOUS if progress == "host_completion_mismatch" else Reason.GATE_FAILED
    assert _code(error) is expected
    assert progress in log
    # The first paused session is the one that fails: no regrant, no later session.
    tail = log[log.index("host:evidence_capture:pause") :]
    assert [entry for entry in tail if entry.endswith(":pause")] == ["host:evidence_capture:pause"]
    assert not any(entry.startswith("configure:") for entry in tail)
    assert "host_initialize_missing" not in log


def test_a_fail_closed_gate_is_not_recorded_before_its_refusal_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate claims a refusal, so it is recorded only once that refusal has passed."""
    ledger = q.GateLedger()
    with pytest.raises(q.QualificationError):
        _journey(monkeypatch, tmp_path, "wrong_refusal", ledger=ledger)
    for check in ("mutation_fail_closed", "replay_fail_closed", "job_reads_fail_closed"):
        assert ledger.status("i8", check) is q.GateStatus.PENDING


@pytest.mark.parametrize(
    "tamper", ["owner_events_changed_after_revoke", "unhealthy_after_host_exit"]
)
def test_owner_observation_and_post_host_health_cannot_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    with pytest.raises(q.QualificationError) as error:
        _journey(monkeypatch, tmp_path, tamper)
    assert _code(error) is Reason.GATE_FAILED


def test_staging_evidence_alone_never_proves_an_imported_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staging capture shares the staged bytes' kind, checksum and media type."""
    ledger = q.GateLedger()
    with pytest.raises(q.QualificationError) as error:
        _journey(monkeypatch, tmp_path, "import_without_evidence", ledger=ledger)
    assert _code(error) is Reason.GATE_FAILED
    assert ledger.status("i5", "job_events_match_owner") is q.GateStatus.PASSED
    assert ledger.status("i5", "imported_evidence_retrieved") is q.GateStatus.PENDING


def test_only_one_artifact_bound_to_the_run_is_imported_evidence() -> None:
    staged = dict(JOURNEY_STAGED)
    staging = {
        "source": {"kind": staged["source_kind"], "source_id": q.STAGED_SOURCE_ID},
        "content_checksum": staged["content_checksum"],
        "media_type": staged["media_type"],
    }
    imported = {
        **staging,
        "source": {"kind": staged["source_kind"], "source_id": "imp-1"},
        "import_run_id": "job-1",
    }

    def page(*artifacts: dict[str, Any], **position: str) -> dict[str, Any]:
        return {"evidence": list(artifacts), "page": position}

    assert q.holds_imported_artifact(page(staging, imported), "job-1", staged)
    assert not q.holds_imported_artifact(page(staging), "job-1", staged)
    assert not q.holds_imported_artifact(page(staging, imported), "job-2", staged)
    assert not q.holds_imported_artifact(page(imported, imported), "job-1", staged)
    assert not q.holds_imported_artifact(
        page(staging, imported, continuation_token="more"), "job-1", staged
    )
    for field, value in (
        ("content_checksum", "sha256:" + "d" * 64),
        ("media_type", "text/html"),
        ("source", {"kind": "document", "source_id": "imp-1"}),
    ):
        assert not q.holds_imported_artifact(page(staging, {**imported, field: value}), "job-1", staged)


@pytest.mark.parametrize(("gate", "check"), ALL_CHECKS)
def test_a_pass_record_needs_every_observation_true(gate: str, check: str) -> None:
    record = _pass_record()
    record["gates"][gate][check] = False
    assert not jsonschema.Draft202012Validator(_schema()).is_valid(record)


# --- protocol negotiation, pagination and refusal classes ------------------


def _relay_initialize(result: Any, directory: Path) -> tuple[Any, bool]:
    directory.mkdir(parents=True)
    observer = q._Observer(directory / "events.jsonl")
    relay = q._Relay(observer, None)
    relay.request(_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    try:
        withheld = relay.response(_frame({"jsonrpc": "2.0", "id": 1, "result": result}))
    finally:
        observer.close()
    return q.summarize_observation(q.read_observation(directory / "events.jsonl")), withheld


def test_initialize_requires_the_expected_protocol_version(tmp_path: Path) -> None:
    summary, _ = _relay_initialize({"protocolVersion": q.MCP_PROTOCOL_VERSION}, tmp_path / "ok")
    assert summary.initialized


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "1999-01-01"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "no"}},
    ],
)
def test_initialize_mismatch_or_error_is_a_protocol_violation(
    tmp_path: Path, message: dict[str, Any]
) -> None:
    observer = q._Observer(tmp_path / "events.jsonl")
    relay = q._Relay(observer, None)
    relay.request(_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    with pytest.raises(q._Violation) as error:
        relay.response(_frame(message))
    observer.close()
    assert error.value.kind == "invalid_initialize"


@pytest.mark.parametrize("result", [{}, {"protocolVersion": 2025}, {"protocolVersion": None}])
def test_a_malformed_initialize_result_is_a_protocol_violation(tmp_path: Path, result: Any) -> None:
    observer = q._Observer(tmp_path / "events.jsonl")
    relay = q._Relay(observer, None)
    relay.request(_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    with pytest.raises(q._Violation) as error:
        relay.response(_frame({"jsonrpc": "2.0", "id": 1, "result": result}))
    observer.close()
    assert error.value.kind == "invalid_initialize"


def test_a_paginated_tool_inventory_is_refused(tmp_path: Path) -> None:
    observer = q._Observer(tmp_path / "events.jsonl")
    relay = q._Relay(observer, None)
    relay.request(_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
    with pytest.raises(q._Violation) as error:
        relay.response(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "workspace_inspect"}], "nextCursor": "more"},
                }
            )
        )
    observer.close()
    assert error.value.kind == "invalid_tool_inventory"


def test_a_capability_refusal_is_its_own_closed_class() -> None:
    refused = _refused(
        '{"error":{"code":"capability_not_granted","message":"not enabled"},"metadata":{}}'
    )
    assert q.refusal_class(refused) == "capability_not_granted"
    assert "capability_not_granted" in q.REFUSALS
    assert q.refusal_class(_refused('{"error":{"code":"capability_not_granted_extra"}}')) == "other"


# --- wheel closure: the whole closure is integrity-checked -----------------


def _third_party(candidate: Path, name: str) -> Path:
    return next(candidate.joinpath("wheels").glob(f"{name.replace('-', '_')}-*.whl"))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda path: path.write_bytes(path.read_bytes()[:-1] + b"Z"),  # same size, other bytes
        lambda path: path.write_bytes(path.read_bytes() + b"x"),  # longer than recorded
    ],
)
def test_a_tampered_third_party_wheel_is_a_digest_mismatch(candidate: Path, tamper: Any) -> None:
    tamper(_third_party(candidate, "anyio"))
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.WHEEL_DIGEST_MISMATCH


def test_the_recorded_size_of_each_dependency_is_enforced(candidate: Path) -> None:
    # Indices 0-4 are the five first-party wheels; 5-7 are the third-party closure.
    _edit_json(_manifest(candidate), lambda d: d["wheels"][5].update(bytes=1))
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.WHEEL_DIGEST_MISMATCH


def test_a_stray_wheel_outside_the_closure_is_refused(candidate: Path) -> None:
    (candidate / "wheels" / "stray-0.0.1-py3-none-any.whl").write_bytes(b"not in the manifest")
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda d: d["wheels"][5].update(path="wheels/../mcp.whl"), Reason.CANDIDATE_INVALID),
        (lambda d: d["wheels"][5].pop("bytes"), Reason.CANDIDATE_INVALID),
        (lambda d: d["wheels"][5].update(bytes="12"), Reason.CANDIDATE_INVALID),
        # A second mcp entry is an SDK pin ambiguity, refused before the closure is read.
        (lambda d: d["wheels"].append(deepcopy(d["wheels"][5])), Reason.SDK_PIN_MISMATCH),
        (lambda d: d["wheels"][5].update(first_party=True), Reason.CANDIDATE_INVALID),
    ],
)
def test_a_malformed_dependency_entry_is_refused(candidate: Path, edit: Any, reason: Any) -> None:
    _edit_json(_manifest(candidate), edit)
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is reason


def test_a_symlinked_dependency_is_refused(candidate: Path) -> None:
    if os.name == "nt":
        pytest.skip("symbolic links")
    wheel = _third_party(candidate, "mcp-types")
    target = candidate / "elsewhere.whl"
    target.write_bytes(wheel.read_bytes())
    wheel.unlink()
    wheel.symlink_to(target)
    with pytest.raises(q.QualificationError) as error:
        q.load_candidate(candidate)
    assert _code(error) is Reason.CANDIDATE_INVALID


def test_the_hashed_requirements_name_every_wheel_by_file_url_and_digest(candidate: Path) -> None:
    verified = q._verified_wheels(json.loads(_manifest(candidate).read_text())["wheels"], candidate)
    lines = q.hashed_requirements(verified).splitlines()
    assert len(lines) == len(verified) == 8  # five first-party and three third-party wheels
    for line in lines:
        url, _, hash_flag = line.partition(" --hash=sha256:")
        assert url.startswith("file://") and len(hash_flag) == 64


def test_a_space_in_the_candidate_path_is_carried_safely(tmp_path: Path) -> None:
    spaced = _write_candidate(tmp_path / "candidate with space")
    verified = q._verified_wheels(json.loads(_manifest(spaced).read_text())["wheels"], spaced)
    assert all(" " not in line.split(" --hash")[0] for line in q.hashed_requirements(verified).splitlines())


# --- the proxy: bounded frames, fail-closed observation, reaped children ----


def test_an_oversized_inbound_frame_is_refused_before_it_is_forwarded(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    observation = tmp_path / "events"
    q.write_proxy_spec(spec, child=[sys.executable, "-u", "-c", _proxy_child()], observation=observation)
    oversized = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"x":"' + b"a" * q.MAX_FRAME_BYTES + b'"}}\n'
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=oversized,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == q.PROXY_VIOLATION_EXIT
    assert completed.stdout == b""
    kinds = [event.get("kind") for event in q.read_observation(observation) if event["event"] == "protocol_violation"]
    assert kinds == ["oversized_frame"]


def test_an_oversized_outbound_frame_is_never_forwarded(tmp_path: Path) -> None:
    child = (
        "import sys\n"
        "sys.stdin.readline()\n"
        f"sys.stdout.write('{{' + 'a' * {q.MAX_FRAME_BYTES} + '}}\\n')\n"
        "sys.stdout.flush()\n"
    )
    spec = tmp_path / "spec"
    observation = tmp_path / "events"
    q.write_proxy_spec(spec, child=[sys.executable, "-u", "-c", child], observation=observation)
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), q.INTERNAL_PROXY, str(spec)],
        input=_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == q.PROXY_VIOLATION_EXIT
    assert completed.stdout == b""


def test_a_frame_within_the_bound_is_read_whole() -> None:
    frame = b"x" * (q.MAX_FRAME_BYTES - 1) + b"\n"
    assert q._read_frame(io.BytesIO(frame)) == frame
    with pytest.raises(q._Violation) as error:
        q._read_frame(io.BytesIO(b"x" * (q.MAX_FRAME_BYTES + 1) + b"\n"))
    assert error.value.kind == "oversized_frame"


_PROXY_WRAPPER: str = """
import json, sys
from pathlib import Path
import importlib.util
spec = importlib.util.spec_from_file_location("rh", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["rh"] = module
spec.loader.exec_module(module)
mode = sys.argv[2]
spec_path = Path(sys.argv[3])
original_stop = module._stop
def recording_stop(child):
    original_stop(child)
    sys.stderr.write("reaped=%s\\n" % (child.returncode is not None))
module._stop = recording_stop
if mode == "observer":
    original_emit = module._Observer.emit
    def failing_emit(self, event, **fields):
        if event == "initialize_response":
            raise OSError("observation sink failed")
        return original_emit(self, event, **fields)
    module._Observer.emit = failing_emit
if mode == "qualification":
    def failing_response(self, frame):
        raise module.QualificationError(module.ReasonCode.HOST_OUTPUT_AMBIGUOUS)
    module._Relay.response = failing_response
raise SystemExit(module.run_proxy(spec_path))
"""


@pytest.mark.parametrize("mode", ["observer", "qualification"])
@posix_only
def test_a_failed_observation_stops_and_reaps_the_child(tmp_path: Path, mode: str) -> None:
    spec = tmp_path / "spec"
    observation = tmp_path / "events"
    q.write_proxy_spec(spec, child=[sys.executable, "-u", "-c", _proxy_child()], observation=observation)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PROXY_WRAPPER, str(SCRIPT), mode, str(spec)],
        input=_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == q.PROXY_FAILED_EXIT
    assert completed.stdout == b""
    assert b"reaped=True" in completed.stderr


# --- cleanup: verified removal, deterministic failure reporting -------------


def test_removing_the_runtime_proves_it_is_gone(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "credential").write_text("x", encoding="utf-8")
    q.remove_runtime(root)
    assert not os.path.lexists(root)
    q.remove_runtime(root)  # already gone is still gone


def test_a_nested_disappearance_that_leaves_the_root_is_cleanup_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "credential").write_text("secret", encoding="utf-8")

    def vanish(_path: Path) -> None:
        raise FileNotFoundError("nested entry")

    monkeypatch.setattr(q.shutil, "rmtree", vanish)
    with pytest.raises(q.QualificationError) as error:
        q.remove_runtime(root)
    assert _code(error) is Reason.CLEANUP_INCOMPLETE
    assert (root / "credential").exists()


def test_a_runtime_that_cannot_be_removed_is_cleanup_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()

    def refuse(_path: Path) -> None:
        raise PermissionError("no")

    monkeypatch.setattr(q.shutil, "rmtree", refuse)
    with pytest.raises(q.QualificationError) as error:
        q.remove_runtime(root)
    assert _code(error) is Reason.CLEANUP_INCOMPLETE


def test_a_failed_discard_on_a_failure_path_reports_but_keeps_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(_path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(q.shutil, "rmtree", refuse)
    (tmp_path / "runtime").mkdir()
    q.discard_runtime(tmp_path / "runtime")
    assert capsys.readouterr().err == "reason_code=cleanup_incomplete\n"


def test_a_core_process_that_survives_its_stop_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.replacement_pid = 2_000_000_000
    context.expected = _evidence(context.replacement_pid)
    monkeypatch.setattr(q, "_process_identity_matches", lambda _evidence: True)
    monkeypatch.setattr(q, "_terminate_core_group", lambda *_args: False)
    q.stop_core(context)
    assert context.retained is True


def test_a_core_process_that_is_absent_is_not_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.replacement_pid = 2_000_000_000
    context.expected = _evidence(context.replacement_pid)
    monkeypatch.setattr(q, "_process_identity_matches", lambda _evidence: False)
    monkeypatch.setattr(
        q, "_terminate_core_group", lambda *_args: pytest.fail("signalled an absent process")
    )
    q.stop_core(context)
    assert context.retained is False


class _Reaped:
    """A started Core whose child exited and was reaped; its integer may be reused."""

    returncode = 0

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int:
        return 0


def _probes(lstart: str, boot: str) -> Any:
    """The ``ps`` and ``sysctl`` identity probes, answering for one live process."""

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, (lstart if argv[0] == "/bin/ps" else boot) + "\n", "")

    return run


def test_a_reaped_child_pid_held_by_another_live_process_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 2_000_000_001
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.process = cast(Any, _Reaped(pid))
    _publish(context, _evidence(pid, "start-1"))
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(q.subprocess, "run", _probes("reused-start", "boot-1"))
    monkeypatch.setattr(q, "_terminate_core_group", lambda p, process=None: calls.append((p, process is not None)) or True)
    q.stop_core(context)
    assert calls == []  # an unproved live pid is never signalled, not even as the known group
    assert context.retained is True


def test_a_reaped_child_pid_live_without_descriptor_evidence_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 2_000_000_001
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.process = cast(Any, _Reaped(pid))
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(q, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(q.subprocess, "run", lambda *_a, **_k: pytest.fail("probed without evidence"))
    monkeypatch.setattr(q, "_terminate_core_group", lambda p, process=None: calls.append((p, process is not None)) or True)
    q.stop_core(context)
    assert calls == []
    assert context.retained is True


def test_a_reaped_pid_observed_live_then_absent_during_identity_proof_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 2_000_000_001
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.process = cast(Any, _Reaped(pid))
    _publish(context, _evidence(pid, "start-1"))
    alive = [True]

    def identity_probe(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # The reused process exits while the identity probe is running.
        alive[0] = False
        return subprocess.CompletedProcess(argv, 1, "", "")

    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q, "_pid_running", lambda _pid: alive[0])
    monkeypatch.setattr(q.subprocess, "run", identity_probe)
    monkeypatch.setattr(q, "_terminate_core_group", lambda p, process=None: calls.append((p, process is not None)) or True)
    monkeypatch.setattr(q.os, "killpg", lambda *_: pytest.fail("signalled a group"))
    monkeypatch.setattr(q.os, "kill", lambda *_: pytest.fail("signalled a pid"))
    q.stop_core(context)
    assert calls == []
    assert context.retained is False


def test_a_reaped_child_pid_proved_still_live_is_signalled_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 2_000_000_001
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.process = cast(Any, _Reaped(pid))
    _publish(context, _evidence(pid, "start-1"))
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(q.subprocess, "run", _probes("start-1", "boot-1"))
    monkeypatch.setattr(q, "_terminate_core_group", lambda p, process=None: calls.append((p, process is not None)) or True)
    q.stop_core(context)
    assert calls == [(pid, False)]  # the proved same-PID process, once, through the non-child path
    assert context.retained is False


def test_a_reaped_child_pid_that_is_absent_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 2_000_000_001
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.process = cast(Any, _Reaped(pid))
    _publish(context, _evidence(pid, "start-1"))
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(q, "_pid_running", lambda _pid: False)
    monkeypatch.setattr(q.subprocess, "run", lambda *_a, **_k: pytest.fail("probed an absent pid"))
    monkeypatch.setattr(q, "_terminate_core_group", lambda p, process=None: calls.append((p, process is not None)) or True)
    q.stop_core(context)
    assert calls == [(pid, True)]
    assert context.retained is False


@posix_only
def test_core_group_shutdown_escalates_and_proves_the_whole_group_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    waits = iter([False, True])
    monkeypatch.setattr(q, "_process_group", lambda _pid: 4321)
    monkeypatch.setattr(q, "_wait_group_absent", lambda _group: next(waits))
    monkeypatch.setattr(q.os, "killpg", lambda group, sig: signals.append((group, sig)))
    assert q._terminate_core_group(1234)
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


@posix_only
def test_core_group_shutdown_uses_the_known_group_after_its_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExitedLeader:
        pid = 4321

        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait(timeout: float) -> int:
            return 0

    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(q, "_wait_group_absent", lambda _group: True)
    monkeypatch.setattr(q.os, "killpg", lambda group, sig: signals.append((group, sig)))
    context = q.CoreContext(
        tmp_path,
        tmp_path / "workspace",
        tmp_path / "installation",
        "workspace-1",
        process=ExitedLeader(),
    )
    q.stop_core(context)
    assert signals == [(4321, signal.SIGTERM)]
    assert context.retained is False


def test_permission_denied_process_probe_is_present_and_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(q.os, "kill", denied)
    assert q._pid_running(2_000_000_000) is True


def test_qualify_host_stops_a_core_when_startup_fails_after_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = q.CoreContext(
        tmp_path / "core",
        tmp_path / "workspace",
        tmp_path / "installation",
        "workspace-1",
    )
    process = object()
    stopped: list[q.CoreContext] = []
    monkeypatch.setattr(q, "initialize_core", lambda *_args: context)

    def fail_start(_installed: object, actual: q.CoreContext) -> None:
        actual.process = process
        raise q.QualificationError(Reason.GATE_FAILED)

    monkeypatch.setattr(q, "start_core", fail_start)
    monkeypatch.setattr(q, "stop_core", stopped.append)
    with pytest.raises(q.QualificationError) as error:
        q.qualify_host(
            host="codex-cli",
            binary=tmp_path / "codex",
            auth=q.AuthFile(tmp_path / "auth"),
            installed=object(),  # type: ignore[arg-type]
            run_root=tmp_path,
        )
    assert _code(error) is Reason.GATE_FAILED
    assert context.process is process
    assert stopped == [context]


def test_a_core_exit_hidden_by_a_managed_local_replacement_fails_the_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health passes after the host exits, but another Core now serves: never a pass."""
    log: list[str] = []
    with pytest.raises(q.QualificationError) as error:
        _journey(monkeypatch, tmp_path, "core_replaced_unexpectedly", log)
    assert _code(error) is Reason.GATE_FAILED
    assert "capture_search_host_ok" not in log
    assert "restart:core" not in log


def _publish(context: Any, process: dict[str, Any], *, ready: bool = True) -> None:
    context.descriptor.parent.mkdir(parents=True, exist_ok=True)
    context.descriptor.write_text(json.dumps({"ready": ready, "process": process}), encoding="utf-8")


def _evidence(pid: int, start_time: str = "start-1") -> dict[str, Any]:
    return {"pid": pid, "start_time": start_time, "boot_id": "boot-1"}


def test_non_child_identity_requires_successful_process_and_boot_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="start-1\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="boot-1\n", stderr=""),
        ]
    )
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q.subprocess, "run", lambda *_args, **_kwargs: next(probes))
    assert q._process_identity_matches(_evidence(4242)) is True


def test_non_child_identity_fails_closed_when_process_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        q.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="start-1\n", stderr="probe failed"
        ),
    )
    monkeypatch.setattr(q, "_pid_running", lambda _pid: True)
    assert q._process_identity_matches(_evidence(4242)) is None


def test_non_child_identity_treats_an_absent_failed_process_probe_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        q.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr=""),
    )
    monkeypatch.setattr(q, "_pid_running", lambda _pid: False)
    assert q._process_identity_matches(_evidence(4242)) is False


def test_non_child_identity_fails_closed_when_boot_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="start-1\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="boot-1\n", stderr="probe failed"),
        ]
    )
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q.subprocess, "run", lambda *_args, **_kwargs: next(probes))
    assert q._process_identity_matches(_evidence(4242)) is None


@pytest.mark.parametrize("probe", ["start", "boot"])
def test_non_child_identity_mismatch_on_a_live_pid_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    probes = iter(
        [
            subprocess.CompletedProcess(
                [], 0, stdout="other\n" if probe == "start" else "start-1\n", stderr=""
            ),
            subprocess.CompletedProcess(
                [], 0, stdout="other\n" if probe == "boot" else "boot-1\n", stderr=""
            ),
        ]
    )
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q.subprocess, "run", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(q, "_pid_running", lambda _pid: True)
    assert q._process_identity_matches(_evidence(4242)) is None


def test_non_child_identity_mismatch_on_an_absent_pid_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="other\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="boot-1\n", stderr=""),
        ]
    )
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q.subprocess, "run", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(q, "_pid_running", lambda _pid: False)
    assert q._process_identity_matches(_evidence(4242)) is False


def test_a_mismatched_live_core_identity_is_retained_and_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.replacement_pid = 2_000_000_000
    context.expected = _evidence(context.replacement_pid)
    probes = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="reused\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="boot-1\n", stderr=""),
        ]
    )
    monkeypatch.setattr(q.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(q.subprocess, "run", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(q, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        q, "_terminate_core_group", lambda *_args: pytest.fail("signalled an uncertain process")
    )
    q.stop_core(context)
    assert context.retained is True


def _sleeper(**options: Any) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True, **options)


@posix_only
def test_core_alive_requires_the_expected_running_core_named_by_the_descriptor(
    tmp_path: Path,
) -> None:
    child = _sleeper()
    try:
        context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws", process=child)
        context.expected = _evidence(child.pid)
        _publish(context, _evidence(child.pid))
        assert q.core_alive(context)
        _publish(context, _evidence(child.pid), ready=False)
        assert not q.core_alive(context)
        _publish(context, _evidence(child.pid, "reused-pid"))
        assert not q.core_alive(context)
        _publish(context, _evidence(child.pid))
        child.kill()
        child.wait()
        assert not q.core_alive(context)  # exited; the descriptor still names it
        # A managed-local replacement now serves, and is healthy: still not the expected Core.
        _publish(context, _evidence(os.getpid(), "start-2"))
        assert not q.core_alive(context)
        # Only once the run itself moves the expected Core is the replacement accepted.
        context.expected = _evidence(os.getpid(), "start-2")
        assert q.core_alive(context)
    finally:
        child.kill()
        child.wait()


@posix_only
def test_health_is_never_read_from_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    context.expected = _evidence(os.getpid())
    _publish(context, _evidence(os.getpid()))
    probes: list[str] = []

    def health(_installed: Any, _context: Any, path: Any, payload: Any = None) -> dict[str, str]:
        assert path == ("service", "health")
        probes.append("health")
        return {"status": "pass"}

    monkeypatch.setattr(q, "owner_call", health)
    assert q.core_healthy(object(), context)

    def replacing(_installed: Any, _context: Any, path: Any, payload: Any = None) -> dict[str, str]:
        probes.append("replacing")  # this probe found Core gone and started another
        _publish(context, _evidence(os.getppid(), "start-2"))
        return {"status": "pass"}

    monkeypatch.setattr(q, "owner_call", replacing)
    assert not q.core_healthy(object(), context)
    probes.clear()
    assert not q.core_healthy(object(), context)
    assert probes == [], "health was probed after the expected Core was already gone"


@posix_only
def test_only_the_deliberate_restart_moves_the_expected_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _sleeper(start_new_session=True)
    try:
        context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws", process=child)
        context.expected = _evidence(child.pid)
        _publish(context, _evidence(child.pid))

        def managed_restart(_installed: Any, _context: Any, path: Any, payload: Any = None) -> Any:
            _publish(context, _evidence(os.getpid(), "start-2"))
            return {"status": "pass"}

        sent: list[int] = []
        killpg = os.killpg

        def record(group: int, sig: int) -> None:
            if sig:  # signal 0 is only the group-absence probe
                sent.append(sig)
            killpg(group, sig)

        monkeypatch.setattr(q.os, "killpg", record)
        monkeypatch.setattr(q, "owner_call", managed_restart)
        q.restart_core(object(), context)  # type: ignore[arg-type]
        # A crash, not a shutdown: SIGKILL to the whole group, no TERM first.
        assert sent == [signal.SIGKILL]
        assert child.poll() == -signal.SIGKILL
        assert context.retained is False
        assert context.expected == _evidence(os.getpid(), "start-2")
        assert context.replacement_pid == os.getpid()
        assert q.core_alive(context)
    finally:
        child.kill()
        child.wait()


@posix_only
def test_a_restart_never_hides_a_core_that_already_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _sleeper(start_new_session=True)
    child.kill()
    child.wait()
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws", process=child)
    context.expected = _evidence(child.pid)
    _publish(context, _evidence(child.pid))
    monkeypatch.setattr(q, "owner_call", lambda *_args, **_kwargs: pytest.fail("probed health"))
    with pytest.raises(q.QualificationError) as error:
        q.restart_core(object(), context)  # type: ignore[arg-type]
    assert _code(error) is Reason.GATE_FAILED
    assert context.expected == _evidence(child.pid)


class _CrashedCore:
    """A started Core whose crash the test decides: it dies of ``exit_code`` or survives."""

    pid = 2_000_000_003

    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("core", timeout or 0)
        self.returncode = self.exit_code
        return self.exit_code


@posix_only
@pytest.mark.parametrize(
    ("exit_code", "group_absent", "retained"),
    [
        pytest.param(None, False, True, id="group-survives-sigkill"),
        pytest.param(0, True, False, id="exited-not-crashed"),
    ],
)
def test_a_restart_fails_closed_unless_the_sigkill_crash_is_proved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int | None,
    group_absent: bool,
    retained: bool,
) -> None:
    core = _CrashedCore(exit_code)
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws", process=core)
    context.expected = _evidence(core.pid)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(q, "core_alive", lambda _context: True)
    monkeypatch.setattr(q, "_process_group", lambda _pid: 4321)
    monkeypatch.setattr(q.os, "killpg", lambda group, sig: sent.append((group, sig)))
    monkeypatch.setattr(q, "_wait_group_absent", lambda _group: group_absent)
    monkeypatch.setattr(q, "owner_call", lambda *_args, **_kwargs: pytest.fail("probed health"))
    with pytest.raises(q.QualificationError) as error:
        q.restart_core(object(), context)  # type: ignore[arg-type]
    assert _code(error) is Reason.GATE_FAILED
    assert sent == [(4321, signal.SIGKILL)]
    assert context.retained is retained
    assert context.expected == _evidence(core.pid)


@posix_only
def test_teardown_stops_an_unplanned_replacement_the_descriptor_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stray = 2_000_000_001
    stopped: list[int] = []
    monkeypatch.setattr(q, "_pid_running", lambda pid: pid == stray)
    monkeypatch.setattr(q, "_process_identity_matches", lambda _evidence: True)
    monkeypatch.setattr(
        q,
        "_terminate_core_group",
        lambda pid, _process=None: not stopped.append(pid),
    )
    context = q.CoreContext(tmp_path, tmp_path / "w", tmp_path / "i", "ws")
    _publish(context, _evidence(stray, "start-unplanned"))
    q.stop_core(context)
    assert stopped == [stray]
    assert context.retained is False


@pytest.mark.parametrize("nested_disappearance", [False, True])
def test_a_pass_is_never_published_over_retained_runtime(
    run: Any,
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    nested_disappearance: bool,
) -> None:
    runtime = _owned_runtime(
        monkeypatch,
        tmp_path / "parent",
        q.candidate_receipt(
            q.load_candidate(candidate), hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
        ),
    )
    prefix = runtime / "candidate-venv"
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
    monkeypatch.setattr(q, "require_host_version", lambda *_, **__: "2.1.289")
    monkeypatch.setattr(q, "require_host_authentication", lambda *_, **__: None)
    monkeypatch.setattr(q, "qualify_host", lambda **_: _passed_ledger())
    monkeypatch.setattr(q, "os_identity", lambda: q.OsIdentity("27.0", "26A428", "arm64"))

    if nested_disappearance:
        # A nested entry vanishes mid-delete: rmtree raises FileNotFoundError, but the
        # root and a credential-like file are still there.
        (runtime / "credential").write_text("secret", encoding="utf-8")

        def vanish(_root: Path) -> None:
            raise FileNotFoundError("nested entry")

        monkeypatch.setattr(q.shutil, "rmtree", vanish)
    else:

        def refuse(_root: Path) -> None:
            raise q.QualificationError(Reason.CLEANUP_INCOMPLETE)

        monkeypatch.setattr(q, "remove_runtime", refuse)
    status, out, err = run(**{"--runtime-root": runtime})
    assert (status, out) == (1, "")
    assert err.endswith("reason_code=cleanup_incomplete\n")
    record = json.loads(run.output.read_text(encoding="utf-8"))
    assert (record["verdict"], record["reason_code"]) == ("fail", "cleanup_incomplete")
    assert not nested_disappearance or (runtime / "credential").exists()


def test_the_unexposed_tools_are_exactly_the_catalogue_outside_the_manifest() -> None:
    # The harness keeps its probe list as a literal so it runs from an installed candidate;
    # this conformance test is what stops that list drifting from the catalogue and manifest.
    from omnivia_core_mcp import manifest

    entries = json.loads(
        (REPO_ROOT / "contracts" / "application" / "v1" / "schemas" / "operations.schema.json").read_text(
            encoding="utf-8"
        )
    )["x-omnivia-operation-catalogue"]
    catalogue = [entry["name"] for entry in entries]
    exposed = {entry.operation for entry in manifest.AUTHORING_MANIFEST}
    outside = sorted(set(catalogue) - exposed)
    assert len(catalogue) == 73 and len(outside) == 48
    assert sorted(name.replace(".", "_") for name in outside) == sorted(q.UNEXPOSED_TOOLS)
    assert {entry.tool_name for entry in manifest.AUTHORING_MANIFEST} == set(q.AUTHORING_TOOLS)


# --- existing host login (Claude only) --------------------------------------

AMBIENT_HOME = "/Users/operator"
AMBIENT_USER = "operator"
LOGIN = q.ExistingLogin(AMBIENT_HOME, AMBIENT_USER)


def _ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invoking process's profile values, as an existing-login run captures them."""
    monkeypatch.setenv("HOME", AMBIENT_HOME)
    monkeypatch.setenv("USER", AMBIENT_USER)


@pytest.mark.parametrize(
    ("host", "auth_file", "existing_login", "expected"),
    [
        ("claude-code", "file", False, q.AuthFile),
        ("codex-cli", "file", False, q.AuthFile),
        ("claude-code", None, True, q.ExistingLogin),
    ],
)
def test_auth_source_selects_exactly_one_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    auth_file: str | None,
    existing_login: bool,
    expected: type,
) -> None:
    _ambient(monkeypatch)
    path = tmp_path / "auth" if auth_file else None
    assert isinstance(q.auth_source(host, path, existing_login), expected)


@pytest.mark.parametrize(
    ("host", "auth_file", "existing_login"),
    [
        ("claude-code", None, False),
        ("codex-cli", None, False),
        ("claude-code", "file", True),
        ("codex-cli", "file", True),
        ("codex-cli", None, True),
    ],
)
def test_auth_source_refuses_every_ambiguous_or_misplaced_selection(
    tmp_path: Path, host: str, auth_file: str | None, existing_login: bool
) -> None:
    path = tmp_path / "auth" if auth_file else None
    with pytest.raises(ValueError):
        q.auth_source(host, path, existing_login)


@pytest.mark.parametrize(
    "overrides",
    [
        {"--use-existing-host-auth": True},
        {"--host": "codex-cli", "--auth-file": None, "--use-existing-host-auth": True},
    ],
)
def test_the_cli_refuses_an_ambiguous_auth_source_as_a_usage_error(
    run: Any, overrides: dict[str, Any]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run(**overrides)
    assert exit_info.value.code == 2
    assert not run.output.exists()


def test_an_existing_login_keeps_the_invoking_profile_and_redirects_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-ambient-value-000000")
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    binary = _claude_binary(tmp_path)
    assert q.host_environment(layout, binary, existing_login=LOGIN) == {
        "PATH": f"{binary.parent}:{q.SYSTEM_PATH}",
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(layout.temporary),
        "HOME": AMBIENT_HOME,
        "USER": AMBIENT_USER,
    }


def test_an_existing_login_captures_the_invoking_profile_values_only() -> None:
    environ = {"HOME": AMBIENT_HOME, "USER": AMBIENT_USER, "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert q.ExistingLogin.from_environ(environ) == LOGIN
    assert AMBIENT_HOME not in repr(LOGIN) and AMBIENT_USER not in repr(LOGIN)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"USER": AMBIENT_USER},
        {"HOME": AMBIENT_HOME},
        {"HOME": "", "USER": AMBIENT_USER},
        {"HOME": "relative/home", "USER": AMBIENT_USER},
        {"HOME": f"{AMBIENT_HOME}\n", "USER": AMBIENT_USER},
        {"HOME": AMBIENT_HOME, "USER": ""},
        {"HOME": AMBIENT_HOME, "USER": " operator"},
        {"HOME": AMBIENT_HOME, "USER": "op\x00erator"},
    ],
)
def test_a_missing_or_malformed_ambient_profile_refuses_without_echoing_it(
    environ: dict[str, str],
) -> None:
    with pytest.raises(q.QualificationError) as error:
        q.auth_source("claude-code", None, True, environ=environ)
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    assert str(error.value) == Reason.AUTHENTICATION_UNAVAILABLE.value


def test_an_existing_login_provisions_no_credential_and_copies_nothing(tmp_path: Path) -> None:
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    assert q.provision_credential("claude-code", layout, LOGIN) == {}
    assert not layout.auth_destination.exists()


@pytest.mark.parametrize("call", ["provision", "authenticate"])
def test_codex_fails_closed_if_an_existing_login_is_ever_paired_with_it(
    tmp_path: Path, call: str
) -> None:
    layout = q.host_layout(tmp_path / "isolated", "codex-cli")
    with pytest.raises(q.QualificationError) as error:
        if call == "provision":
            q.provision_credential("codex-cli", layout, LOGIN)
        else:
            q.require_host_authentication(
                "codex-cli",
                tmp_path,
                layout,
                LOGIN,
                run=lambda *_args, **_kwargs: pytest.fail("the host ran"),
            )
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    assert not layout.auth_destination.exists()


def test_the_mcp_child_redirects_the_profile_only_for_an_existing_login(tmp_path: Path) -> None:
    existing = q.host_layout(tmp_path / "existing", "claude-code")
    token = q.host_layout(tmp_path / "token", "claude-code")
    q.create_layout(existing)
    q.create_layout(token)
    existing_text = q.write_host_config(existing, "claude-code", ENTRY, existing_login=LOGIN).read_text(
        encoding="utf-8"
    )
    token_text = q.write_host_config(token, "claude-code", ENTRY).read_text(encoding="utf-8")
    assert json.loads(existing_text)["mcpServers"][q.SERVER_KEY]["env"] == {
        "HOME": str(existing.home),
        "CLAUDE_CONFIG_DIR": str(existing.config_dir),
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }
    assert json.loads(token_text)["mcpServers"][q.SERVER_KEY]["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": ""}
    assert AMBIENT_HOME not in existing_text and AMBIENT_USER not in existing_text


def test_the_existing_login_command_adds_only_the_safe_and_restricted_flags(tmp_path: Path) -> None:
    arguments = {"mcp_config": tmp_path / "m.json", "prompt": "p", "tools": q.RESTRICTED_TOOLS}
    token = q.claude_command(_claude_binary(tmp_path), **arguments)
    existing = q.claude_command(_claude_binary(tmp_path), **arguments, existing_login=True)
    assert "--safe-mode" not in token and "--restricted" not in token
    assert existing == [*token, "--safe-mode", "--restricted"]


def test_the_token_mode_keeps_its_redirects_and_its_command_without_the_hardening_flags(
    tmp_path: Path,
) -> None:
    layout = q.host_layout(tmp_path, "claude-code")
    binary = _claude_binary(tmp_path)
    environment = q.host_environment(layout, binary, {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
    assert environment["HOME"] == str(layout.home)
    assert environment["CLAUDE_CONFIG_DIR"] == str(layout.config_dir)
    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    command = q.claude_command(binary, mcp_config=tmp_path / "m.json", prompt="p", tools=q.RESTRICTED_TOOLS)
    assert "--safe-mode" not in command and "--restricted" not in command


def test_an_existing_login_session_runs_hardened_with_no_token_and_no_profile_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    installed = q.InstalledCandidate(
        tmp_path / "venv",
        tmp_path / "venv" / "python",
        tmp_path / "venv" / "service",
        tmp_path / "venv" / "omnivia",
        tmp_path / "venv" / "mcp",
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(q, "run_host", lambda command, **kwargs: seen.update(command=list(command), **kwargs))
    q.run_host_session(
        host="claude-code",
        binary=_claude_binary(tmp_path),
        layout=layout,
        installed=installed,
        core_config=tmp_path / "core.json",
        auth=LOGIN,
        prompt="prompt-text",
        marker="marker-text",
        tools=q.RESTRICTED_TOOLS,
        timeout=1.0,
    )
    assert seen["env"] == {
        "PATH": f"{tmp_path}:{q.SYSTEM_PATH}",
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(layout.temporary),
        "HOME": AMBIENT_HOME,
        "USER": AMBIENT_USER,
    }
    assert "CLAUDE_CONFIG_DIR" not in seen["env"]
    command = seen["command"]
    for flag in ("--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    config_text = (layout.root / "claude-mcp.json").read_text(encoding="utf-8")
    server = json.loads(config_text)["mcpServers"]
    assert server[q.SERVER_KEY]["env"] == {
        "HOME": str(layout.home),
        "CLAUDE_CONFIG_DIR": str(layout.config_dir),
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }
    assert AMBIENT_HOME not in config_text
    assert not (layout.config_dir / ".credentials.json").exists()


@posix_only
def test_an_existing_login_is_proved_only_by_auth_status_in_the_session_environment(
    tmp_path: Path,
) -> None:
    layout = q.host_layout(tmp_path / "isolated", "claude-code")
    binary = _claude_binary(tmp_path)
    seen: list[tuple[list[str], dict[str, str], Path, float]] = []

    def run(arguments: Any, environment: Any, cwd: Path, timeout: float) -> Any:
        seen.append((list(arguments), dict(environment), cwd, timeout))
        return subprocess.CompletedProcess(arguments, 0, b'{"loggedIn": true}', b"")

    q.require_host_authentication("claude-code", binary, layout, LOGIN, run=run)
    [(arguments, environment, cwd, timeout)] = seen
    assert arguments == [str(binary), "auth", "status", "--json"]
    assert environment == q.host_environment(layout, binary, existing_login=LOGIN)
    assert environment["HOME"] == AMBIENT_HOME and environment["USER"] == AMBIENT_USER
    assert cwd == layout.workspace and timeout == 60.0


@posix_only
@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, b'{"loggedIn": true}'),
        (0, b'{"loggedIn": false}'),
        (0, b'{"loggedIn": "true"}'),
        (0, b'["loggedIn"]'),
        (0, b"not json sk-ant-leak"),
        (0, b""),
    ],
)
def test_an_existing_login_fails_closed_unless_auth_status_reports_a_login(
    tmp_path: Path, returncode: int, stdout: bytes
) -> None:
    def run(arguments: Any, environment: Any, cwd: Path, timeout: float) -> Any:
        return subprocess.CompletedProcess(arguments, returncode, stdout, b"sk-ant-leak")

    with pytest.raises(q.QualificationError) as error:
        q.require_host_authentication(
            "claude-code",
            _claude_binary(tmp_path),
            q.host_layout(tmp_path / "isolated", "claude-code"),
            LOGIN,
            run=run,
        )
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE
    assert "sk-ant-leak" not in str(error.value)


@posix_only
def test_an_existing_login_whose_status_probe_cannot_run_is_refused(tmp_path: Path) -> None:
    def run(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("probe refused")

    with pytest.raises(q.QualificationError) as error:
        q.require_host_authentication(
            "claude-code",
            _claude_binary(tmp_path),
            q.host_layout(tmp_path / "isolated", "claude-code"),
            LOGIN,
            run=run,
        )
    assert _code(error) is Reason.AUTHENTICATION_UNAVAILABLE


def test_the_driver_carries_an_existing_login_into_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stop(Exception):
        pass

    seen: list[object] = []

    def capture(**kwargs: Any) -> object:
        seen.append(kwargs["auth"])
        raise Stop

    monkeypatch.setattr(q, "run_host_session", capture)
    driver = q.HostDriver(
        "claude-code",
        _claude_binary(tmp_path),
        LOGIN,
        object(),
        tmp_path / "core.json",
        tmp_path / "sessions",
        q.AUTHORING_TOOLS,
    )
    with pytest.raises(Stop):
        driver.call("memory_search", {"query": "fixed"})
    assert seen == [LOGIN]


def _existing_child(
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt: object = None,
) -> Path:
    """A child run handed an owned runtime, stubbed up to the host authentication step."""
    _ambient(monkeypatch)
    matching = q.candidate_receipt(
        q.load_candidate(candidate), hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    )
    runtime = _owned_runtime(
        monkeypatch, tmp_path / "parent", matching if receipt is None else receipt
    )
    prefix = runtime / "candidate-venv"
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
    monkeypatch.setattr(q, "require_host_version", lambda *_, **__: "2.1.289")
    monkeypatch.setattr(q, "require_host_authentication", lambda *_, **__: None)
    monkeypatch.setattr(q, "qualify_host", lambda **_: _passed_ledger())
    monkeypatch.setattr(q, "os_identity", lambda: q.OsIdentity("27.0", "26A428", "arm64"))
    return runtime


def test_the_child_hands_the_existing_login_to_the_auth_step_and_passes(
    run: Any, candidate: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _existing_child(candidate, monkeypatch, tmp_path)
    seen: dict[str, Any] = {}

    def version_step(host: str, binary: Path, env: Any, cwd: Path, **_: Any) -> str:
        seen["version_env"] = dict(env)
        return "2.1.289"

    def auth_step(host: str, binary: Path, layout: Any, auth: Any, **_: Any) -> None:
        seen["auth"] = auth

    monkeypatch.setattr(q, "require_host_version", version_step)
    monkeypatch.setattr(q, "require_host_authentication", auth_step)
    status, out, err = run(
        **{"--auth-file": None, "--use-existing-host-auth": True, "--runtime-root": runtime}
    )
    assert (status, out, err) == (0, "qualification pass\n", "")
    assert seen["auth"] == LOGIN
    assert set(seen["version_env"]) == {"PATH", "LANG", "TMPDIR", "HOME", "USER"}
    assert seen["version_env"]["HOME"] == AMBIENT_HOME
    assert seen["version_env"]["USER"] == AMBIENT_USER
    assert "CLAUDE_CONFIG_DIR" not in seen["version_env"]


def test_a_child_whose_receipt_differs_refuses_the_existing_login_before_authenticating(
    run: Any, candidate: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _existing_child(candidate, monkeypatch, tmp_path, receipt={})
    monkeypatch.setattr(
        q, "require_host_authentication", lambda *_, **__: pytest.fail("authenticated")
    )
    status, out, err = run(
        **{"--auth-file": None, "--use-existing-host-auth": True, "--runtime-root": runtime}
    )
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")


def test_an_existing_login_auth_failure_records_only_its_stable_code(
    run: Any, candidate: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _existing_child(candidate, monkeypatch, tmp_path)

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise q.QualificationError(Reason.AUTHENTICATION_UNAVAILABLE)

    monkeypatch.setattr(q, "require_host_authentication", refuse)
    status, out, err = run(
        **{"--auth-file": None, "--use-existing-host-auth": True, "--runtime-root": runtime}
    )
    assert (status, out, err) == (1, "", "reason_code=authentication_unavailable\n")
    record_text = run.output.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert (record["verdict"], record["reason_code"]) == ("fail", "authentication_unavailable")
    assert AMBIENT_HOME not in record_text and AMBIENT_USER not in record_text


@posix_only
def test_the_existing_login_flag_survives_the_candidate_reexec_without_any_auth_path(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ambient(monkeypatch)
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
        observed.update(argv=argv, environ=environ)
        raise q.QualificationError(Reason.ENTRYPOINT_UNRESOLVED)

    monkeypatch.setattr(q, "reexec_under_candidate", reexec)
    status, out, err = run(**{"--auth-file": None, "--use-existing-host-auth": True})
    assert (status, out, err) == (1, "", "reason_code=entrypoint_unresolved\n")
    argv = observed["argv"]
    assert "--use-existing-host-auth" in argv and "--auth-file" not in argv
    assert argv[-2] == "--runtime-root"
    assert "auth.bin" not in " ".join(argv)
    environ = observed["environ"]
    assert set(environ) == {"PATH", "LANG", "TMPDIR", "HOME", "USER"}
    assert (environ["HOME"], environ["USER"]) == (AMBIENT_HOME, AMBIENT_USER)
    assert AMBIENT_HOME not in " ".join(argv) and AMBIENT_USER not in " ".join(argv)
    assert "auth.bin" not in run.output.read_text(encoding="utf-8")
    assert AMBIENT_HOME not in run.output.read_text(encoding="utf-8")


def test_token_mode_reexec_carries_no_profile_values_even_when_the_profile_is_set(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ambient(monkeypatch)
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
        observed.update(environ=environ)
        raise q.QualificationError(Reason.ENTRYPOINT_UNRESOLVED)

    monkeypatch.setattr(q, "reexec_under_candidate", reexec)
    assert run() == (1, "", "reason_code=entrypoint_unresolved\n")
    assert set(observed["environ"]) == {"PATH", "LANG", "TMPDIR"}


def test_removing_an_existing_login_run_never_touches_the_real_profile(tmp_path: Path) -> None:
    profile = tmp_path / "real-home" / ".claude"
    profile.mkdir(parents=True)
    (profile / ".credentials.json").write_text("host", encoding="utf-8")
    root = tmp_path / "runtime"
    q.create_layout(q.host_layout(root, "claude-code"))
    q.remove_runtime(root)
    assert not root.exists()
    assert (profile / ".credentials.json").read_text(encoding="utf-8") == "host"
