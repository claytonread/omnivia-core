"""Guards for the installed-wheel MCP authoring qualification record."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNEY = REPO_ROOT / "scripts" / "run-mcp-authoring-qualification.py"
BUILDER = REPO_ROOT / "scripts" / "build-standard-candidate.py"
SCHEMA = (
    REPO_ROOT
    / "docs"
    / "distribution"
    / "schemas"
    / "mcp-authoring-qualification-record-v1.schema.json"
)
RETAINED_RECORD = (
    REPO_ROOT
    / "docs"
    / "development"
    / "qualification"
    / "mcp-authoring-installed-wheel-qualification-2026-10-03.json"
)


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record() -> dict[str, object]:
    return {
        "format": "omnivia.mcp-authoring-qualification.v1",
        "verdict": "pass",
        "profile": "authoring",
        "protocol_version": "2025-06-18",
        "tool_count": 29,
        "tools": [
            "workspace_inspect",
            "evidence_search",
            "knowledge_search",
            "memory_search",
            "graph_traverse",
            "context_pack_build",
            "engineering_search",
            "engineering_expand",
            "engineering_context_build",
            "decision_evaluate",
            "decision_record_get",
            "decision_record_list",
            "decision_status",
            "trigger_health",
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
            "knowledge_share_propose",
            "knowledge_share_decide",
            "knowledge_share_read",
            "knowledge_share_lineage",
        ],
        "sdk_versions": {"mcp": "2.0.0", "mcp-types": "2.0.0"},
        "environment": {
            "system": "darwin",
            "machine": "arm64",
            "release": "26.0.0",
            "python": "3.11.9",
        },
        "journeys": {
            "empty_workspace": {
                "empty_workspace": True,
                "tool_discovery": True,
                "capture_and_search": True,
                "proposed_memory": True,
                "candidate_visibility": True,
                "replay_and_conflict": True,
                "core_restart_recovery": True,
                "revocation_fail_closed": True,
                "service_healthy": True,
            },
            "import": {
                "trusted_staging": True,
                "import_start": True,
                "job_observation": True,
                "import_replay_and_conflict": True,
                "revocation_preserved_job": True,
                "service_healthy": True,
            },
        },
        "redaction": {
            "credentials_recorded": False,
            "private_paths_recorded": False,
            "private_identifiers_recorded": False,
            "submitted_content_recorded": False,
            "prompts_or_transcripts_recorded": False,
            "endpoints_or_processes_recorded": False,
            "stdio_recorded": False,
            "model_responses_recorded": False,
        },
    }


def test_journey_imports_only_the_runtime_process_evidence_and_uses_installed_entry_points() -> None:
    """Product behaviour is reached only through installed executables.  The one OmniVia
    import is the runtime's process-evidence reader, which proves a Core's identity
    before teardown signals it."""
    tree = ast.parse(JOURNEY.read_text(encoding="utf-8"), filename=str(JOURNEY))
    imports = {
        f"{node.module}.{alias.name}" if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert {name for name in imports if name.startswith("omnivia")} == {
        "omnivia_core_runtime.ownership.identity.SystemProcessEvidence"
    }
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"omnivia-core-service", "omnivia", "omnivia-core-mcp"} <= constants


def test_journey_names_every_required_authoring_and_recovery_step() -> None:
    source = JOURNEY.read_text(encoding="utf-8")
    for token in (
        "evidence_capture",
        "evidence_search",
        "memory_create",
        "view\": \"candidates",
        "import_start",
        "job_get",
        "job_events",
        "idempotency_conflict",
        "MCP authoring revoke",
        "managed-local restart",
    ):
        assert token in source


def test_closed_schema_and_builder_accept_the_exact_redacted_record() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    builder = _module(BUILDER, "build_standard_candidate_authoring_test")
    assert builder._require_authoring_qualification(_record()) == _record()


def test_retained_18_tool_record_is_closed_historical_and_expired_for_current_candidates() -> None:
    """The 2026-10-03 Phase 8 run is immutable evidence of an 18-tool inventory.

    It is closed and bound to the historical inventory, an ordered subset of the live
    25-tool list. The current builder refuses it only because the tool inventory has
    advanced; the refusal is the fixed, payload-free message.
    """
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    journey = _module(JOURNEY, "omnivia_authoring_retained_under_test")
    builder = _module(BUILDER, "build_standard_candidate_retained_authoring")
    retained = json.loads(RETAINED_RECORD.read_text(encoding="utf-8"))

    assert set(retained) == set(schema["required"])
    assert retained["tool_count"] == len(retained["tools"]) == 18
    current = iter(journey.AUTHORING_TOOLS)
    assert all(tool in current for tool in retained["tools"])
    violations = {error.path[0] for error in jsonschema.Draft202012Validator(schema).iter_errors(retained)}
    assert violations == {"tool_count", "tools"}

    with pytest.raises(builder.CandidateError) as refused:
        builder._require_authoring_qualification(retained)
    assert str(refused.value) == "the MCP authoring qualification record is not the accepted redacted shape"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((), {"workspace_id": "private"}),
        (("redaction",), {"credentials_recorded": True}),
        (("sdk_versions",), {"mcp": "2.2.0"}),
        (("environment",), {"workspace_path": "/Users/private/workspace"}),
        (("journeys", "empty_workspace"), {"model_response": "pass"}),
    ],
)
def test_builder_refuses_extra_sensitive_fields_and_false_redaction_claims(
    path: tuple[str, ...], value: dict[str, object]
) -> None:
    builder = _module(BUILDER, "build_standard_candidate_authoring_negative")
    record = deepcopy(_record())
    target: dict[str, object] = record
    for member in path:
        child = target[member]
        assert isinstance(child, dict)
        target = child
    target.update(value)

    with pytest.raises(builder.CandidateError, match="accepted redacted shape"):
        builder._require_authoring_qualification(record)


def test_record_contains_no_field_that_can_carry_private_run_material() -> None:
    record = _record()

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    assert not keys(record) & {
        "workspace_id",
        "principal_id",
        "credential_reference",
        "bearer",
        "grant",
        "endpoint",
        "process_id",
        "prompt",
        "transcript",
        "stdout",
        "stderr",
        "model_response",
    }
    serialized = json.dumps(record, sort_keys=True)
    for forbidden in ("/Users/", "C:\\\\Users\\\\"):
        assert forbidden not in serialized


def _refusal(text: str) -> dict[str, object]:
    return {"is_error": True, "structured_content": None, "content": [{"type": "text", "text": text}]}


def test_revocation_is_proven_only_by_the_installed_credential_store_message() -> None:
    journey = _module(JOURNEY, "omnivia_authoring_under_test")
    exact = "evidence_capture could not be called: this installation holds no credential for that reference"
    journey._blocked(_refusal(exact), "capture", "evidence_capture")
    journey._blocked(_refusal(exact.replace(" ", "  ")), "capture", "evidence_capture")
    for generic in (
        "evidence_capture could not be called: timed out",
        "evidence_capture could not be called",
        "job_get could not be called: this installation holds no credential for that reference",
    ):
        with pytest.raises(journey.QualificationError):
            journey._blocked(_refusal(generic), "capture", "evidence_capture")


def test_a_record_boolean_exists_only_when_its_check_completed() -> None:
    journey = _module(JOURNEY, "omnivia_authoring_checks_under_test")
    names = ("empty_workspace", "service_healthy")
    assert journey._checked({"empty_workspace": True, "service_healthy": True}, names) == {
        "empty_workspace": True,
        "service_healthy": True,
    }
    with pytest.raises(journey.QualificationError):
        journey._checked({"empty_workspace": True}, names)
    with pytest.raises(journey.QualificationError):
        journey._checked({"empty_workspace": True, "service_healthy": False}, names)


def test_staged_source_inspection_is_read_only_and_creates_nothing(tmp_path: Path) -> None:
    journey = _module(JOURNEY, "omnivia_authoring_read_only_under_test")
    database = tmp_path / "workspace.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.commit()
    with contextlib.closing(journey._read_only(database)) as connection:
        assert connection.execute("SELECT count(*) FROM t").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO t VALUES ('written')")
    assert [path.name for path in tmp_path.iterdir()] == ["workspace.sqlite"]
    (tmp_path / "workspace.sqlite-wal").write_bytes(b"")
    with pytest.raises(journey.QualificationError, match="not closed cleanly"):
        journey._read_only(database)


STAGED = {
    "staged_source_ref": "stg-1",
    "source_kind": "document",
    "content_checksum": "sha256:" + "c" * 64,
    "content_length_bytes": 24,
    "media_type": "text/plain",
}


def test_only_one_artifact_bound_to_the_run_is_imported_evidence() -> None:
    journey = _module(JOURNEY, "omnivia_authoring_imported_under_test")
    staging = {
        "source": {"kind": "document", "source_id": journey.STAGED_SOURCE_ID},
        "content_checksum": STAGED["content_checksum"],
        "media_type": STAGED["media_type"],
    }
    imported = {**staging, "source": {"kind": "document", "source_id": "imp-1"}, "import_run_id": "job-1"}

    def page(*artifacts: dict[str, object], **position: str) -> dict[str, object]:
        return {"evidence": list(artifacts), "page": position}

    journey._imported_artifact(page(staging, imported), "job-1", STAGED)
    for found, job_id in (
        (page(staging), "job-1"),  # the trusted staging capture alone
        (page(staging, imported), "job-2"),
        (page(imported, imported), "job-1"),
        (page(staging, imported, continuation_token="more"), "job-1"),
        (page(staging, {**imported, "content_checksum": "sha256:" + "d" * 64}), "job-1"),
        (page(staging, {**imported, "media_type": "text/html"}), "job-1"),
        (page(staging, {**imported, "source": {"kind": "other", "source_id": "imp-1"}}), "job-1"),
    ):
        with pytest.raises(journey.QualificationError):
            journey._imported_artifact(found, job_id, STAGED)


class _Process:
    """The Core a `_run_*` journey starts, reduced to what the journey asks of it."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


#: Fake pids: the started Core, the deliberate replacement, and an unplanned one.
STARTED, REPLACEMENT, UNPLANNED = 4_000_000, 4_000_001, 4_000_002


class _ManagedCore:
    """A workspace's real descriptor file, and a managed-local client that replaces an exited Core."""

    def __init__(self, root: Path) -> None:
        self.descriptor = root / "installation-state" / "runtime" / "ws-1" / "service.json"
        self.descriptor.parent.mkdir(parents=True)
        self.process = _Process(STARTED)
        self.live: set[int] = set()
        self.after_revoke = 0
        self.publish(STARTED, "start-1")

    def publish(self, pid: int, start_time: str) -> None:
        self.live.add(pid)
        process = {"pid": pid, "start_time": start_time, "boot_id": "boot-1"}
        self.descriptor.write_text(json.dumps({"ready": True, "process": process}), encoding="utf-8")

    def health(self, *_arguments: object) -> bool:
        """`service health` through a managed-local client: an exited Core is replaced, healthily."""
        published = json.loads(self.descriptor.read_text(encoding="utf-8"))["process"]
        if published["pid"] == STARTED and self.process.returncode is not None:
            self.publish(REPLACEMENT, "start-2")
        return True


def _managed_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, _ManagedCore]:
    """The journey over a managed Core, with its own teardown left in place."""
    journey = _module(JOURNEY, "omnivia_authoring_continuity_under_test")
    core = _ManagedCore(tmp_path)
    monkeypatch.setattr(
        journey, "_initialize", lambda _service, root: (root / "workspace", root / "installation-state", "ws-1")
    )
    monkeypatch.setattr(journey.shared, "_endpoint", lambda _root: "unix://fake")
    monkeypatch.setattr(journey.shared, "_start_service", lambda *_args: core.process)
    monkeypatch.setattr(journey, "_configure", lambda *_args: tmp_path / "config.json")
    monkeypatch.setattr(journey, "_principal", lambda _config: "principal-1")
    monkeypatch.setattr(journey, "_health", core.health)
    monkeypatch.setattr(journey, "_alive", lambda pid: pid in core.live)
    monkeypatch.setattr(journey, "_owner_evidence_count", lambda *_args: core.after_revoke)
    return journey, core


@pytest.fixture
def managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, _ManagedCore, list[str]]:
    journey, core = _managed_journey(tmp_path, monkeypatch)
    stopped: list[str] = []
    monkeypatch.setattr(journey, "_stop", lambda *_args: stopped.append("stopped"))
    return journey, core, stopped


def _run_empty(
    journey: ModuleType,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    during_journey: Any = lambda: None,
    during_restart: Any = lambda: None,
) -> Any:
    async def empty_journey(_mcp: Path, _config: Path, _principal: str, checks: dict[str, bool]) -> Any:
        for name in journey.EMPTY_CHECKS[:6]:
            checks[name] = True
        during_journey()
        return {
            "protocol": journey.PROTOCOL_VERSION,
            "server": journey.SERVER_NAME,
            "tools": list(journey.AUTHORING_TOOLS),
            "capture": {},
        }

    async def restart_journey(*_args: object) -> None:
        during_restart()

    monkeypatch.setattr(journey, "_empty_workspace_journey", empty_journey)
    monkeypatch.setattr(journey, "_restart_and_revoke_journey", restart_journey)
    return journey._run_empty(Path("service"), Path("cli"), Path("mcp"), root)


def test_the_empty_journey_passes_with_one_continuous_core_per_step(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, _core, stopped = managed
    result = _run_empty(journey, tmp_path, monkeypatch)
    assert {name: result[name] for name in journey.EMPTY_CHECKS} == dict.fromkeys(journey.EMPTY_CHECKS, True)
    assert stopped == ["stopped"]


def _exited(core: _ManagedCore, replaced: bool) -> Any:
    """The started Core exits; a managed-local client may already have replaced it."""

    def exit_now() -> None:
        core.process.returncode = 1
        core.live.discard(STARTED)
        if replaced:
            core.publish(UNPLANNED, "start-unplanned")

    return exit_now


@pytest.mark.parametrize("replaced", [True, False])
def test_an_unexpected_core_exit_is_not_hidden_by_a_managed_local_restart(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool
) -> None:
    journey, core, stopped = managed
    with pytest.raises(journey.QualificationError, match="exited before the deliberate restart"):
        _run_empty(journey, tmp_path, monkeypatch, during_journey=_exited(core, replaced))
    assert stopped == ["stopped"]


def test_a_replacement_that_is_replaced_again_is_not_continuous(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, core, _stopped = managed

    def replaced_again() -> None:
        core.live.discard(REPLACEMENT)
        core.publish(UNPLANNED, "start-unplanned")

    with pytest.raises(journey.QualificationError, match="exited during the post-restart journey"):
        _run_empty(journey, tmp_path, monkeypatch, during_restart=replaced_again)


def test_a_replacement_that_exited_unreplaced_is_not_continuous(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor still names the replacement, but no such process runs."""
    journey, core, _stopped = managed
    with pytest.raises(journey.QualificationError, match="exited during the post-restart journey"):
        _run_empty(journey, tmp_path, monkeypatch, during_restart=lambda: core.live.discard(REPLACEMENT))


def test_a_capture_that_settled_after_revocation_fails_the_owner_count(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, core, _stopped = managed

    def settled() -> None:
        core.after_revoke += 1

    with pytest.raises(journey.QualificationError, match="attempted after revocation settled"):
        _run_empty(journey, tmp_path, monkeypatch, during_restart=settled)


@pytest.mark.parametrize("replaced", [True, False])
def test_an_import_journey_core_exit_is_not_hidden_by_a_managed_local_restart(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool
) -> None:
    journey, core, stopped = managed
    exit_now = _exited(core, replaced)

    async def import_journey(*args: Any) -> str:
        checks = args[-1]
        for name in ("import_start", "import_replay_and_conflict", "job_observation"):
            checks[name] = True
        exit_now()
        return "job-1"

    monkeypatch.setattr(journey, "_stage_source", lambda *_args: dict(STAGED))
    monkeypatch.setattr(journey, "_import_journey", import_journey)
    monkeypatch.setattr(journey, "_owner_job", lambda *_args: True)
    with pytest.raises(journey.QualificationError, match="exited during the import journey"):
        journey._run_import(Path("service"), Path("cli"), Path("mcp"), tmp_path)
    assert stopped == ["stopped"]


def test_health_answered_by_a_replacement_is_not_the_expected_cores_health(
    managed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, core, _stopped = managed
    core.publish(REPLACEMENT, "start-2")
    expected = {"pid": REPLACEMENT, "start_time": "start-2", "boot_id": "boot-1"}
    assert journey._healthy(Path("cli"), Path("i"), "ws-1", core.descriptor, expected)

    def replaced_during_probe(*_arguments: object) -> bool:
        core.live.discard(REPLACEMENT)
        core.publish(UNPLANNED, "start-3")
        return True

    monkeypatch.setattr(journey, "_health", replaced_during_probe)
    assert not journey._healthy(Path("cli"), Path("i"), "ws-1", core.descriptor, expected)
    core.publish(REPLACEMENT, "start-2")
    assert not journey._serving(core.descriptor, {**expected, "start_time": "reused-pid"})
    # The started child exited, though the pid the descriptor names still answers.
    first = {"pid": STARTED, "start_time": "start-1", "boot_id": "boot-1"}
    core.publish(STARTED, "start-1")
    assert journey._serving(core.descriptor, first, core.process)
    core.process.returncode = 1
    assert not journey._serving(core.descriptor, first, core.process)


def test_a_cleanup_failure_never_replaces_the_original_failure(
    managed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journey, core, _stopped = managed

    def failing_stop(*_args: object) -> None:
        raise journey.shared.JourneyError("the service did not exit before the deadline")

    monkeypatch.setattr(journey, "_stop", failing_stop)
    with pytest.raises(journey.QualificationError, match="exited before the deliberate restart"):
        _run_empty(journey, tmp_path, monkeypatch, during_journey=_exited(core, None))
    assert capsys.readouterr().err == "MCP authoring qualification cleanup also failed\n"


def test_import_session_cleanup_never_replaces_the_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journey = _module(JOURNEY, "omnivia_authoring_import_cleanup_under_test")

    class Session:
        @staticmethod
        async def list_tools() -> object:
            raise journey.QualificationError("original import failure")

    class RetainedStack:
        @staticmethod
        async def aclose() -> None:
            raise RuntimeError("session cleanup failed")

    async def opened(*_args: object) -> tuple[None, Session, RetainedStack]:
        return None, Session(), RetainedStack()

    monkeypatch.setattr(journey, "_opened_session", opened)
    with pytest.raises(journey.QualificationError, match="original import failure"):
        journey.anyio.run(
            journey._import_journey,
            Path("mcp"),
            Path("config"),
            Path("cli"),
            Path("installation"),
            dict(STAGED),
            {},
        )
    assert capsys.readouterr().err == "MCP authoring qualification cleanup also failed\n"


def test_session_open_failure_closes_every_context_and_keeps_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journey = _module(JOURNEY, "omnivia_authoring_session_open_cleanup_under_test")
    closed: list[str] = []

    @contextlib.asynccontextmanager
    async def transport(_parameters: object) -> Any:
        try:
            yield object(), object()
        finally:
            closed.append("transport")

    class Session:
        @staticmethod
        async def send_request(*_args: object) -> object:
            raise journey.QualificationError("initialize failed")

    @contextlib.asynccontextmanager
    async def client(_read: object, _write: object) -> Any:
        try:
            yield Session()
        finally:
            closed.append("session")

    monkeypatch.setattr(journey, "stdio_client", transport)
    monkeypatch.setattr(journey, "ClientSession", client)
    with pytest.raises(journey.QualificationError, match="initialize failed"):
        journey.anyio.run(journey._opened_session, Path("mcp"), Path("config"))
    assert closed == ["session", "transport"]


def test_permission_denied_process_probe_is_alive_and_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journey = _module(JOURNEY, "omnivia_authoring_permission_probe_under_test")

    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(journey.os, "kill", denied)
    assert journey._alive(2_000_000_000) is True


def _evidence(pid: int, start_time: str, boot_id: str = "boot-1") -> dict[str, Any]:
    return {"pid": pid, "start_time": start_time, "boot_id": boot_id}


def _system(running: dict[int, dict[str, Any] | None]) -> Any:
    """The installed runtime's process-evidence reader over a fixed process table.

    A pid mapped to ``None`` is alive, but its evidence cannot be read.
    """

    def for_pid(pid: int) -> SimpleNamespace | None:
        evidence = running.get(pid)
        return None if evidence is None else SimpleNamespace(**evidence)

    return lambda: SimpleNamespace(for_pid=for_pid)


def _teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: dict[int, dict[str, Any] | None]
) -> tuple[ModuleType, _ManagedCore, list[int]]:
    """The real teardown, after the started Core exited, over a system process table."""
    journey = _module(JOURNEY, "omnivia_authoring_teardown_under_test")
    core = _ManagedCore(tmp_path)
    core.process.returncode = 0
    signalled: list[int] = []
    monkeypatch.setattr(journey, "SystemProcessEvidence", _system(running))
    monkeypatch.setattr(journey, "_alive", lambda pid: pid in running)
    monkeypatch.setattr(journey.shared, "_stop_replacement", signalled.append)
    monkeypatch.setattr(journey.shared, "_wait_for_exit", lambda _pid: None)
    return journey, core, signalled


def test_teardown_signals_a_named_core_only_once_its_whole_identity_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = {
        UNPLANNED: _evidence(UNPLANNED, "start-unplanned"),
        REPLACEMENT: _evidence(REPLACEMENT, "start-2"),
    }
    journey, core, signalled = _teardown(tmp_path, monkeypatch, running)
    journey._stop(core.process, core.descriptor)
    assert signalled == []  # the descriptor names only the started Core
    core.publish(UNPLANNED, "start-unplanned")
    journey._stop(core.process, core.descriptor)
    assert signalled == [UNPLANNED]
    core.publish(REPLACEMENT, "start-2")
    journey._stop(core.process, core.descriptor, _evidence(REPLACEMENT, "start-2"))
    assert signalled == [UNPLANNED, REPLACEMENT]  # once, as the planned replacement


@pytest.mark.parametrize(
    ("published", "current"),
    [
        pytest.param(
            _evidence(UNPLANNED, "start-unplanned"), _evidence(UNPLANNED, "start-reused"), id="reused-pid"
        ),
        pytest.param(
            _evidence(UNPLANNED, "start-unplanned"),
            _evidence(UNPLANNED, "start-unplanned", "boot-2"),
            id="other-boot",
        ),
        pytest.param(_evidence(UNPLANNED, "start-unplanned"), None, id="indeterminate"),
        pytest.param({"pid": UNPLANNED}, _evidence(UNPLANNED, "start-unplanned"), id="identity-absent"),
    ],
)
def test_teardown_never_signals_a_named_pid_whose_identity_is_not_proved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: dict[str, Any],
    current: dict[str, Any] | None,
) -> None:
    journey, core, signalled = _teardown(tmp_path, monkeypatch, {UNPLANNED: current})
    core.descriptor.write_text(json.dumps({"ready": True, "process": published}), encoding="utf-8")
    with pytest.raises(journey.QualificationError, match="identity was not proved"):
        journey._stop(core.process, core.descriptor)  # named by the descriptor
    with pytest.raises(journey.QualificationError, match="identity was not proved"):
        journey._stop(core.process, core.descriptor, published)  # the planned replacement
    assert signalled == []


def test_teardown_of_named_cores_that_already_exited_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, core, signalled = _teardown(tmp_path, monkeypatch, {})
    core.publish(UNPLANNED, "start-unplanned")
    journey._stop(core.process, core.descriptor, _evidence(REPLACEMENT, "start-2"))
    assert signalled == []


@pytest.mark.parametrize(
    "current", [pytest.param(_evidence(STARTED, "start-reused"), id="reused-pid"), pytest.param(None, id="indeterminate")]
)
def test_a_reaped_child_pid_held_by_another_live_process_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: dict[str, Any] | None
) -> None:
    # The descriptor names the original child's integer; that child is reaped and
    # the integer now runs another process whose identity cannot be proved.
    journey, core, signalled = _teardown(tmp_path, monkeypatch, {STARTED: current})
    with pytest.raises(journey.QualificationError, match="identity was not proved"):
        journey._stop(core.process, core.descriptor)
    assert signalled == []


def test_a_reaped_child_pid_that_is_still_proved_is_signalled_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, core, signalled = _teardown(tmp_path, monkeypatch, {STARTED: _evidence(STARTED, "start-1")})
    journey._stop(core.process, core.descriptor)
    assert signalled == [STARTED]


def test_a_reaped_child_pid_that_is_absent_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, core, signalled = _teardown(tmp_path, monkeypatch, {})
    journey._stop(core.process, core.descriptor)
    assert signalled == []


def test_the_empty_journey_teardown_stops_the_proved_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey, _core = _managed_journey(tmp_path, monkeypatch)
    signalled: list[int] = []
    monkeypatch.setattr(journey, "SystemProcessEvidence", _system({REPLACEMENT: _evidence(REPLACEMENT, "start-2")}))
    monkeypatch.setattr(journey.shared, "_stop_replacement", signalled.append)
    monkeypatch.setattr(journey.shared, "_wait_for_exit", lambda _pid: None)
    _run_empty(journey, tmp_path, monkeypatch)
    assert signalled == [REPLACEMENT]


def test_an_unproved_teardown_on_a_failing_path_keeps_the_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journey, core = _managed_journey(tmp_path, monkeypatch)
    signalled: list[int] = []
    # The unplanned replacement's pid now runs a process that started at another time.
    monkeypatch.setattr(journey, "SystemProcessEvidence", _system({UNPLANNED: _evidence(UNPLANNED, "start-reused")}))
    monkeypatch.setattr(journey.shared, "_stop_replacement", signalled.append)
    with pytest.raises(journey.QualificationError, match="exited before the deliberate restart"):
        _run_empty(journey, tmp_path, monkeypatch, during_journey=_exited(core, replaced=True))
    assert signalled == []
    assert capsys.readouterr().err == "MCP authoring qualification cleanup also failed\n"


def test_the_journey_builds_its_booleans_from_checks_not_constants() -> None:
    source = JOURNEY.read_text(encoding="utf-8")
    assert "_checked(checks, EMPTY_CHECKS)" in source
    assert "_checked(checks, IMPORT_CHECKS)" in source
    for name in ("empty_workspace", "revocation_fail_closed", "job_observation", "revocation_preserved_job"):
        assert f'checks["{name}"] = True' in source
