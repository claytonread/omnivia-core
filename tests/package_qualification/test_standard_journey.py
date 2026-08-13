"""Static and executable guards for the isolated V06-7 Standard-profile journey."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from mcp.shared.exceptions import MCPError

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNEY = REPO_ROOT / "scripts" / "run-standard-journey.py"


def _tree() -> ast.Module:
    return ast.parse(JOURNEY.read_text(encoding="utf-8"), filename=str(JOURNEY))


def _constants() -> set[str]:
    return {
        node.value
        for node in ast.walk(_tree())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_standard_journey", JOURNEY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_journey_invokes_only_the_three_installed_product_executables() -> None:
    constants = _constants()
    assert {"omnivia-core-service", "omnivia", "omnivia-core-mcp"} <= constants
    imports = {
        alias.name
        for node in ast.walk(_tree())
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name.startswith("omnivia") for name in imports)


def test_journey_covers_initialization_governance_mcp_and_recovery() -> None:
    constants = _constants()
    assert {
        "--init",
        "--capture-source",
        "--source-id",
        "memory",
        "create",
        "governance",
        "propose",
        "approve",
        "knowledge_search",
        "context_pack_build",
        "managed-local crash recovery",
    } <= constants


def test_journey_has_no_skip_or_xfail_path() -> None:
    attributes = {
        node.attr for node in ast.walk(_tree()) if isinstance(node, ast.Attribute)
    }
    assert not {"skip", "skipif", "xfail", "importorskip"} & attributes


def _windows(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, *results: tuple[int, str]
) -> list[list[str]]:
    """Run the restriction as Windows, returning the commands it issued.

    `_IS_WINDOWS` is the seam rather than `os.name`, which is global and would
    change every other platform decision in the process running these tests.
    """
    commands: list[list[str]] = []
    pending = list(results)

    def _fake_run(arguments, *, input_text=None, timeout=None):
        commands.append(list(arguments))
        code, stdout = pending.pop(0)
        return subprocess.CompletedProcess(list(arguments), code, stdout, "")

    monkeypatch.setattr(module, "_IS_WINDOWS", True)
    monkeypatch.setattr(module, "_run", _fake_run)
    monkeypatch.setenv("SystemRoot", "D:\\Windows")
    return commands


def _system32(program: str) -> str:
    return str(Path("D:\\Windows", "System32", program))


def _whoami_row() -> str:
    return '"host\\user","S-1-5-21-1111111111-2222222222-3333333333-1001"\n'


def test_windows_restriction_issues_the_established_icacls_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    commands = _windows(
        module, monkeypatch, (0, _whoami_row()), (0, ""), (0, ""), (0, "")
    )
    config = tmp_path / "omnivia-mcp.json"
    config.write_text("{}", encoding="utf-8")

    module._restrict(config)

    sid = "S-1-5-21-1111111111-2222222222-3333333333-1001"
    icacls = _system32("icacls.exe")
    assert commands == [
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        [icacls, str(config), "/setowner", f"*{sid}", "/q"],
        [icacls, str(config), "/reset", "/q"],
        [icacls, str(config), "/inheritance:r", "/grant:r", f"*{sid}:F", "/q"],
    ]


def test_writing_the_mcp_configuration_restricts_it_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    commands = _windows(
        module, monkeypatch, (0, _whoami_row()), (0, ""), (0, ""), (0, "")
    )
    config = tmp_path / "omnivia-mcp.json"

    module._write_mcp_configuration(config, tmp_path / "installation-state", "ws-1")

    document = json.loads(config.read_text(encoding="utf-8"))
    assert document["format"] == "omnivia.mcp-config.v1"
    assert [command[0] for command in commands] == [
        _system32("whoami.exe"),
        _system32("icacls.exe"),
        _system32("icacls.exe"),
        _system32("icacls.exe"),
    ]


def test_windows_restriction_fails_closed_when_the_sid_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    commands = _windows(module, monkeypatch, (1, ""))
    config = tmp_path / "omnivia-mcp.json"
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(module.JourneyError) as excinfo:
        module._restrict(config)

    assert str(excinfo.value) == module._RESTRICTION_FAILED
    assert len(commands) == 1


def test_windows_restriction_fails_closed_when_a_step_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    commands = _windows(module, monkeypatch, (0, _whoami_row()), (0, ""), (5, ""))
    config = tmp_path / "omnivia-mcp.json"
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(module.JourneyError) as excinfo:
        module._restrict(config)

    assert str(excinfo.value) == module._RESTRICTION_FAILED
    # Stopped at the refused `/reset`; the grant that would have followed a
    # successful sequence never ran.
    assert len(commands) == 3


def test_windows_restriction_failure_reports_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    config = tmp_path / "secret-workspace" / "omnivia-mcp.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")

    def _leaking_run(arguments, *, input_text=None, timeout=None):
        raise OSError(
            f"{arguments!r} failed for {config} as "
            "S-1-5-21-4444444444 with api-key=sk-1234"
        )

    monkeypatch.setattr(module, "_IS_WINDOWS", True)
    monkeypatch.setattr(module, "_run", _leaking_run)
    monkeypatch.setenv("SystemRoot", "D:\\Windows")

    with pytest.raises(module.JourneyError) as excinfo:
        module._restrict(config)

    error = excinfo.value
    assert str(error) == "could not restrict the MCP configuration to its owner"
    # The caught OSError must be unreachable, not merely unquoted: a chained
    # exception keeps its text on `__cause__`/`__context__`, and every traceback
    # renderer prints those.
    assert error.__cause__ is None
    assert error.__context__ is None
    surfaces = (
        str(error),
        repr(error),
        repr(error.args),
        repr(error.__cause__),
        repr(error.__context__),
    )
    for secret in (
        str(config),
        "secret-workspace",
        "S-1-5-21-4444444444",
        "icacls",
        "whoami",
        "D:\\Windows",
        "api-key",
        "sk-1234",
    ):
        for surface in surfaces:
            assert secret not in surface


def test_the_posix_configuration_is_written_owner_read_write_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    modes: list[int] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module, "_run", lambda arguments, **_: commands.append(list(arguments)))
    # Recorded rather than read back from the filesystem: a Windows host cannot
    # store `0o600`, and the mode this asks for is what the assertion is about.
    monkeypatch.setattr(Path, "chmod", lambda self, mode: modes.append(mode))
    config = tmp_path / "omnivia-mcp.json"

    module._write_mcp_configuration(config, tmp_path / "installation-state", "ws-1")

    assert modes == [0o600]
    assert commands == []


class _FakeKernel32:
    """A fake kernel32, recording calls so the stop and wait paths are testable
    off Windows."""

    def __init__(
        self,
        *,
        handle: int = 4242,
        last_error: int = 0,
        wait_result: int = 0,
        close_result: int = 1,
        raise_from: str | None = None,
    ) -> None:
        self.handle = handle
        self.last_error = last_error
        self.wait_result = wait_result
        self.close_result = close_result
        self.raise_from = raise_from
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.calls.append(("OpenProcess", (access, inherit, pid)))
        if self.raise_from == "OpenProcess":
            raise OSError("boom")
        return self.handle

    def GetLastError(self) -> int:
        self.calls.append(("GetLastError", ()))
        return self.last_error

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        self.calls.append(("WaitForSingleObject", (handle, timeout_ms)))
        if self.raise_from == "WaitForSingleObject":
            raise OSError("boom")
        return self.wait_result

    def TerminateProcess(self, handle: int, exit_code: int) -> int:
        self.calls.append(("TerminateProcess", (handle, exit_code)))
        if self.raise_from == "TerminateProcess":
            raise OSError("boom")
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.calls.append(("CloseHandle", (handle,)))
        if self.raise_from == "CloseHandle":
            raise OSError("boom")
        return self.close_result


def _windows_wait(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, library: _FakeKernel32
) -> None:
    monkeypatch.setattr(module, "_IS_WINDOWS", True)
    monkeypatch.setattr(module, "_win32_kernel32_loader", lambda: library)


def test_windows_wait_for_exit_returns_when_signaled_and_the_handle_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(wait_result=module._WAIT_OBJECT_0, close_result=1)
    _windows_wait(module, monkeypatch, library)

    module._wait_for_exit(4321, timeout=1.0)

    assert [name for name, _ in library.calls] == [
        "OpenProcess",
        "WaitForSingleObject",
        "CloseHandle",
    ]
    assert library.calls[0][1] == (module._PROCESS_SYNCHRONIZE, False, 4321)
    assert library.calls[1][1] == (library.handle, 1000)
    assert library.calls[2][1] == (library.handle,)


def test_windows_wait_for_exit_treats_error_87_as_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(handle=0, last_error=module._ERROR_INVALID_PARAMETER)
    _windows_wait(module, monkeypatch, library)

    module._wait_for_exit(4321, timeout=1.0)

    assert [name for name, _ in library.calls] == ["OpenProcess", "GetLastError"]


def test_windows_wait_for_exit_fails_closed_on_a_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(wait_result=module._WAIT_TIMEOUT, close_result=1)
    _windows_wait(module, monkeypatch, library)

    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(4321, timeout=1.0)

    assert str(excinfo.value) == module._EXIT_WAIT_FAILED
    # Timed out, but the handle it opened was still closed.
    assert [name for name, _ in library.calls] == [
        "OpenProcess",
        "WaitForSingleObject",
        "CloseHandle",
    ]


def test_windows_wait_for_exit_fails_closed_on_an_unexpected_wait_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(wait_result=0xFFFFFFFF, close_result=1)  # WAIT_FAILED
    _windows_wait(module, monkeypatch, library)

    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(4321, timeout=1.0)

    assert str(excinfo.value) == module._EXIT_WAIT_FAILED


def test_windows_wait_for_exit_fails_closed_when_open_process_fails_for_another_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(handle=0, last_error=5)  # ERROR_ACCESS_DENIED
    _windows_wait(module, monkeypatch, library)

    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(4321, timeout=1.0)

    assert str(excinfo.value) == module._EXIT_WAIT_FAILED
    assert [name for name, _ in library.calls] == ["OpenProcess", "GetLastError"]


def test_windows_wait_for_exit_fails_closed_when_close_handle_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(wait_result=0, close_result=0)
    _windows_wait(module, monkeypatch, library)

    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(4321, timeout=1.0)

    assert str(excinfo.value) == module._EXIT_WAIT_FAILED


def test_windows_wait_for_exit_fails_closed_on_a_ctypes_or_os_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(raise_from="WaitForSingleObject")
    _windows_wait(module, monkeypatch, library)

    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(4321, timeout=1.0)

    assert str(excinfo.value) == module._EXIT_WAIT_FAILED
    # The handle opened before the failing call was still closed.
    assert [name for name, _ in library.calls] == [
        "OpenProcess",
        "WaitForSingleObject",
        "CloseHandle",
    ]


def test_windows_wait_for_exit_failure_is_sanitized_and_unchained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(raise_from="OpenProcess")
    _windows_wait(module, monkeypatch, library)

    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(999999, timeout=1.0)

    error = excinfo.value
    assert str(error) == "the service did not exit before the deadline"
    # No pid, handle or Win32 error code on any surface, and no chained cause:
    # a suppressed failure re-raised inside its own handler would keep the
    # original on `__cause__`/`__context__`, and that text can quote them.
    assert error.__cause__ is None
    assert error.__context__ is None
    surfaces = (
        str(error),
        repr(error),
        repr(error.args),
        repr(error.__cause__),
        repr(error.__context__),
    )
    for secret in ("999999", "4242", "boom"):
        for surface in surfaces:
            assert secret not in surface


def test_posix_wait_for_exit_continues_polling_on_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    attempts: list[int] = []

    def _fake_kill(pid: int, signum: int) -> None:
        attempts.append(pid)
        if len(attempts) < 3:
            raise PermissionError("access denied")
        raise ProcessLookupError("gone")

    monkeypatch.setattr(module.os, "kill", _fake_kill)

    module._wait_for_exit(4321, timeout=5.0)

    # A `PermissionError` on the first probes must not be accepted as "gone" --
    # only the later `ProcessLookupError` ends the wait.
    assert len(attempts) == 3


def _no_signals(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any signal delivery a hard failure.

    `os.kill(pid, CTRL_BREAK_EVENT)` on Windows is `GenerateConsoleCtrlEvent`,
    which addresses a process *group*. The recovered descriptor's PID is not
    guaranteed to be that group ID, so the event can reach the shared CI console.
    The Windows replacement stop must therefore never reach `os.kill` at all.
    """

    def _forbidden(pid: int, signum: int) -> None:
        raise AssertionError("the Windows replacement stop signalled a process")

    monkeypatch.setattr(module.os, "kill", _forbidden)


def test_windows_replacement_stop_terminates_only_that_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32()
    _windows_wait(module, monkeypatch, library)
    _no_signals(module, monkeypatch)

    module._stop_replacement(4321)

    assert [name for name, _ in library.calls] == [
        "OpenProcess",
        "TerminateProcess",
        "CloseHandle",
    ]
    # `PROCESS_TERMINATE` on one handle: this cannot address a group or a console.
    assert library.calls[0][1] == (module._PROCESS_TERMINATE, False, 4321)
    assert library.calls[1][1] == (library.handle, 1)
    assert library.calls[2][1] == (library.handle,)


def test_windows_replacement_stop_is_a_noop_when_the_process_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(handle=0, last_error=module._ERROR_INVALID_PARAMETER)
    _windows_wait(module, monkeypatch, library)
    _no_signals(module, monkeypatch)

    module._stop_replacement(4321)

    assert [name for name, _ in library.calls] == ["OpenProcess"]


def test_windows_replacement_stop_closes_the_handle_when_terminate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(raise_from="TerminateProcess")
    _windows_wait(module, monkeypatch, library)
    _no_signals(module, monkeypatch)

    # Best effort: the stop stays silent, and the wait below is what fails closed.
    module._stop_replacement(4321)

    assert [name for name, _ in library.calls] == [
        "OpenProcess",
        "TerminateProcess",
        "CloseHandle",
    ]


def test_windows_replacement_teardown_waits_and_fails_closed_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    library = _FakeKernel32(wait_result=module._WAIT_TIMEOUT)
    _windows_wait(module, monkeypatch, library)
    _no_signals(module, monkeypatch)

    module._stop_replacement(999999)
    with pytest.raises(module.JourneyError) as excinfo:
        module._wait_for_exit(999999, timeout=1.0)

    # The stop is still followed by the deterministic Win32 handle wait.
    assert [name for name, _ in library.calls] == [
        "OpenProcess",
        "TerminateProcess",
        "CloseHandle",
        "OpenProcess",
        "WaitForSingleObject",
        "CloseHandle",
    ]
    assert library.calls[3][1] == (module._PROCESS_SYNCHRONIZE, False, 999999)
    error = excinfo.value
    assert str(error) == module._EXIT_WAIT_FAILED
    assert error.__cause__ is None
    assert error.__context__ is None
    for secret in ("999999", "4242"):
        assert secret not in repr(error.args)


def test_posix_replacement_stop_stays_a_graceful_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        module.os, "kill", lambda pid, signum: signals.append((pid, signum))
    )

    module._stop_replacement(4321)

    assert signals == [(4321, module.signal.SIGTERM)]


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is absent")


def test_final_cleanup_routes_the_replacement_through_the_pid_only_stop() -> None:
    called = {
        node.func.id
        for node in ast.walk(_function("run"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_stop_replacement" in called
    # Names, not the source text: both docstrings name the call they replace.
    for name in ("_stop_replacement", "_terminate_windows"):
        attributes = {
            node.attr
            for node in ast.walk(_function(name))
            if isinstance(node, ast.Attribute)
        }
        assert not {"CTRL_BREAK_EVENT", "CTRL_C_EVENT"} & attributes
    assert "kill" not in {
        node.attr
        for node in ast.walk(_function("_terminate_windows"))
        if isinstance(node, ast.Attribute)
    }


def test_exception_class_names_flattens_nested_exception_groups() -> None:
    module = _module()
    nested = BaseExceptionGroup(
        "inner",
        [ValueError("secret-path/a"), TypeError("secret-path/b")],
    )
    outer = BaseExceptionGroup(
        "outer", [nested, KeyError("secret-path/c"), ValueError("secret-path/d")]
    )
    assert module._exception_class_names(outer) == ["KeyError", "TypeError", "ValueError"]


def test_exception_class_names_is_deterministic_regardless_of_nesting_order() -> None:
    module = _module()
    first = BaseExceptionGroup("g", [TypeError("x"), ValueError("y")])
    second = BaseExceptionGroup("g", [ValueError("y"), TypeError("x")])
    assert module._exception_class_names(first) == module._exception_class_names(second)


def test_exception_class_names_handles_a_plain_exception() -> None:
    module = _module()
    assert module._exception_class_names(RuntimeError("boom")) == ["RuntimeError"]


def test_mcp_error_codes_reads_a_direct_mcp_error() -> None:
    module = _module()
    error = MCPError(-32602, "/Users/someone/secret leaked", {"token": "sk-1234"})
    assert module._mcp_error_codes(error) == [-32602]


def test_mcp_error_codes_reads_nested_exception_group_leaves() -> None:
    module = _module()
    nested = BaseExceptionGroup(
        "inner",
        [MCPError(-32000, "leaked-content"), ValueError("/private/workspace")],
    )
    outer = BaseExceptionGroup(
        "outer", [nested, MCPError(-32601, "password=hunter2"), RuntimeError("boom")]
    )
    assert module._mcp_error_codes(outer) == [-32601, -32000]


def test_mcp_error_codes_sorts_and_deduplicates_deterministically() -> None:
    module = _module()
    first = BaseExceptionGroup(
        "g", [MCPError(-32000, "a"), MCPError(1, "b"), MCPError(-32000, "c")]
    )
    second = BaseExceptionGroup(
        "g", [MCPError(1, "c"), MCPError(-32000, "b"), MCPError(1, "a")]
    )
    assert module._mcp_error_codes(first) == [-32000, 1]
    assert module._mcp_error_codes(first) == module._mcp_error_codes(second)


def test_mcp_error_codes_is_empty_without_an_mcp_error() -> None:
    module = _module()
    assert module._mcp_error_codes(RuntimeError("boom")) == []


def test_mcp_failure_message_reports_codes_without_mcp_message_or_data() -> None:
    module = _module()
    error = BaseExceptionGroup(
        "task group failure",
        [
            MCPError(
                -32602,
                "/Users/someone/secret-workspace leaked-content",
                {"api-key": "sk-1234", "command": "omnivia-core-mcp --config"},
            )
        ],
    )
    message = module._mcp_failure_message("initialize", error, True)
    assert message == (
        "MCP standalone session did not complete: stage=initialize "
        "errors=['MCPError'] codes=[-32602] server_diagnostic=true"
    )
    for secret in (
        "secret-workspace",
        "leaked-content",
        "api-key",
        "sk-1234",
        "omnivia-core-mcp",
        "--config",
        "/Users",
    ):
        assert secret not in message


def test_mcp_failure_message_is_safe_and_deterministic() -> None:
    module = _module()
    error = RuntimeError("/Users/someone/secret-workspace api-key=sk-1234 leaked-content")
    message = module._mcp_failure_message("knowledge_search", error, True)
    assert message == (
        "MCP standalone session did not complete: stage=knowledge_search "
        "errors=['RuntimeError'] codes=[] server_diagnostic=true"
    )
    assert "secret-workspace" not in message
    assert "api-key" not in message
    assert "sk-1234" not in message
    assert "leaked-content" not in message
    assert "/Users" not in message


def test_mcp_failure_message_reports_no_server_diagnostic_when_false() -> None:
    module = _module()
    message = module._mcp_failure_message("initialize", ValueError("x"), False)
    assert "server_diagnostic=false" in message


def test_mcp_journey_reports_stage_and_error_class_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    async def _failing_session(executable, config, diagnostic, stage):
        stage[0] = "context_pack_build"
        diagnostic.write("raw mcp server stderr with a secret token\n")
        diagnostic.flush()
        raise BaseExceptionGroup(
            "task group failure",
            [ValueError("/private/workspace/leaked content and password=hunter2")],
        )

    monkeypatch.setattr(module, "_mcp_session", _failing_session)

    with pytest.raises(module.JourneyError) as excinfo:
        module._mcp_journey(Path("unused-executable"), Path("unused-config"))

    message = str(excinfo.value)
    assert message == (
        "MCP standalone session did not complete: stage=context_pack_build "
        "errors=['ValueError'] codes=[] server_diagnostic=true"
    )
    assert "password" not in message
    assert "hunter2" not in message
    assert "leaked content" not in message
    assert "/private/workspace" not in message
    assert excinfo.value.__cause__ is not None


def test_mcp_journey_reports_nested_mcp_error_codes_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    async def _failing_session(executable, config, diagnostic, stage):
        stage[0] = "initialize"
        diagnostic.write("raw mcp server stderr with secret token sk-1234\n")
        diagnostic.flush()
        raise BaseExceptionGroup(
            "task group failure",
            [
                BaseExceptionGroup(
                    "inner",
                    [MCPError(-32603, "/private/workspace password=hunter2")],
                ),
                MCPError(-32000, "leaked content", {"path": "/private/workspace"}),
            ],
        )

    monkeypatch.setattr(module, "_mcp_session", _failing_session)

    with pytest.raises(module.JourneyError) as excinfo:
        module._mcp_journey(Path("unused-executable"), Path("unused-config"))

    message = str(excinfo.value)
    assert message == (
        "MCP standalone session did not complete: stage=initialize "
        "errors=['MCPError'] codes=[-32603, -32000] server_diagnostic=true"
    )
    for secret in (
        "password",
        "hunter2",
        "leaked content",
        "/private/workspace",
        "raw mcp server stderr",
        "sk-1234",
    ):
        assert secret not in message


def test_mcp_journey_removes_its_temporary_diagnostic_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    captured_dirs: list[Path] = []
    real_temporary_directory = module.tempfile.TemporaryDirectory

    def _tracking_temporary_directory(*args, **kwargs):
        context = real_temporary_directory(*args, **kwargs)
        captured_dirs.append(Path(context.name))
        return context

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", _tracking_temporary_directory)

    async def _failing_session(executable, config, diagnostic, stage):
        stage[0] = "initialize"
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_mcp_session", _failing_session)

    with pytest.raises(module.JourneyError):
        module._mcp_journey(Path("unused-executable"), Path("unused-config"))

    assert captured_dirs
    assert not captured_dirs[0].exists()
