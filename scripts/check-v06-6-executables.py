#!/usr/bin/env python3
"""Qualify the V06-6 console executables from isolated wheels.

This is deliberately a Python driver rather than a shell script: the same file
runs in the Linux, macOS and Windows rows of the existing platform matrix.  It
builds the four distributions needed by the two entry points, stages the
reviewed MCP dependency closure, installs the CLI and MCP distributions into
separate clean virtual environments, and invokes the installed console scripts.

The gate fails unless the wheel and source surfaces are byte-identical, both
``--help`` paths succeed, CLI arguments survive the platform shell unchanged,
and both local-refusal paths preserve their stdout/stderr and exit contracts.
There is no skip or xfail path.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import venv
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CONSTRAINTS: Final = REPO_ROOT / "scripts" / "mcp-wheelhouse-constraints.txt"
PROJECTS: Final = (
    REPO_ROOT,
    REPO_ROOT / "packages" / "omnivia-core-client",
    REPO_ROOT / "packages" / "omnivia-core-cli",
    REPO_ROOT / "packages" / "omnivia-core-mcp",
)
SOURCE_PATHS: Final = (
    REPO_ROOT / "src",
    REPO_ROOT / "packages" / "omnivia-core-client" / "src",
    REPO_ROOT / "packages" / "omnivia-core-cli" / "src",
    REPO_ROOT / "packages" / "omnivia-core-mcp" / "src",
)

CLI_SURFACE: Final = r"""
import json
from omnivia_core.contracts.v1 import FROZEN_ERROR_CODES
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    PROBE_COMMANDS,
    exit_code_for,
)

print(json.dumps({
    "application": [
        {"path": list(command.path), "operation": command.operation}
        for command in APPLICATION_COMMANDS
    ],
    "probes": [
        {"path": list(command.path), "probe": command.probe}
        for command in PROBE_COMMANDS
    ],
    "exit_codes": {
        code: exit_code_for(code) for code in sorted(FROZEN_ERROR_CODES)
    },
}, sort_keys=True, separators=(",", ":")))
"""

MCP_SURFACE: Final = r"""
import json
from omnivia_core_mcp.manifest import MANIFEST_VERSION, tools

print(json.dumps({
    "manifest_version": MANIFEST_VERSION,
    "tools": [tool.model_dump(mode="json") for tool in tools()],
}, sort_keys=True, separators=(",", ":")))
"""


class QualificationError(RuntimeError):
    """A failed build, install, invocation, or parity assertion."""


def _display(command: Sequence[str] | str) -> str:
    return command if isinstance(command, str) else shlex.join(command)


def _run(
    command: Sequence[str] | str,
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    shell: bool = False,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {_display(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        shell=shell,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _require(
    completed: subprocess.CompletedProcess[str],
    *,
    status: int,
    stdout: str | None = None,
    stderr: str | None = None,
    label: str,
) -> None:
    failures: list[str] = []
    if completed.returncode != status:
        failures.append(f"exit {completed.returncode}, expected {status}")
    if stdout is not None and completed.stdout != stdout:
        failures.append("stdout differs from the required bytes")
    if stderr is not None and completed.stderr != stderr:
        failures.append("stderr differs from the required bytes")
    if failures:
        raise QualificationError(
            f"{label}: {'; '.join(failures)}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )


def _must_run(
    command: Sequence[str], *, cwd: Path, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    completed = _run(command, cwd=cwd, timeout=timeout)
    _require(completed, status=0, label=_display(command))
    return completed


def _build_wheel(project: Path, wheelhouse: Path) -> Path:
    before = set(wheelhouse.glob("*.whl"))
    _must_run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheelhouse),
            str(project),
        ],
        cwd=REPO_ROOT,
    )
    built = set(wheelhouse.glob("*.whl")) - before
    if len(built) != 1:
        raise QualificationError(
            f"{project}: expected one new wheel, found {sorted(built)}"
        )
    return built.pop()


def _python(virtual_environment: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    return virtual_environment / directory / name


def _entry_point(virtual_environment: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return virtual_environment / directory / f"{name}{suffix}"


def _wheel_environment(virtual_environment: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    directory = virtual_environment / ("Scripts" if os.name == "nt" else "bin")
    environment["PATH"] = os.pathsep.join(
        [str(directory), environment.get("PATH", "")]
    )
    return environment


def _source_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in SOURCE_PATHS)
    return environment


def _snapshot(
    python: Path, program: str, *, cwd: Path, env: Mapping[str, str]
) -> str:
    completed = _run([str(python), "-c", program], cwd=cwd, env=env)
    _require(completed, status=0, stderr="", label="surface snapshot")
    json.loads(completed.stdout)
    return completed.stdout


def _shell_command(arguments: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _install(
    virtual_environment: Path, distribution: str, wheelhouse: Path
) -> dict[str, str]:
    venv.EnvBuilder(with_pip=True, clear=True).create(virtual_environment)
    environment = _wheel_environment(virtual_environment)
    completed = _run(
        [
            str(_python(virtual_environment)),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--only-binary=:all:",
            "--find-links",
            str(wheelhouse),
            distribution,
        ],
        cwd=virtual_environment.parent,
        env=environment,
    )
    _require(completed, status=0, label=f"install {distribution}")
    return environment


def _qualify_cli(
    virtual_environment: Path,
    *,
    workdir: Path,
    source_env: Mapping[str, str],
    wheel_env: Mapping[str, str],
) -> dict[str, object]:
    executable = _entry_point(virtual_environment, "omnivia")
    if not executable.is_file():
        raise QualificationError(f"installed CLI entry point is absent: {executable}")

    source_surface = _snapshot(
        Path(sys.executable), CLI_SURFACE, cwd=REPO_ROOT, env=source_env
    )
    wheel_surface = _snapshot(
        _python(virtual_environment),
        CLI_SURFACE,
        cwd=workdir,
        env=wheel_env,
    )
    if wheel_surface != source_surface:
        raise QualificationError("CLI source and wheel surfaces differ")

    source_help = _run(
        [sys.executable, "-m", "omnivia_core_cli.main", "--help"],
        cwd=REPO_ROOT,
        env=source_env,
    )
    _require(source_help, status=0, stderr="", label="source omnivia --help")
    wheel_help = _run(
        [str(executable), "--help"], cwd=workdir, env=wheel_env
    )
    _require(
        wheel_help,
        status=0,
        stdout=source_help.stdout,
        stderr="",
        label="wheel omnivia --help",
    )

    sentinel = "shell-quoted $value & with spaces"
    installation = workdir / "installation state & shell quoting"
    arguments = [
        str(executable),
        "--installation-state",
        str(installation),
        "--workspace-id",
        "workspace with spaces & shell punctuation",
        "--timeout-ms",
        sentinel,
        "service",
        "health",
    ]
    refusal = _run(
        _shell_command(arguments),
        cwd=workdir,
        env=wheel_env,
        shell=True,
    )
    _require(refusal, status=2, stdout="", label="shell-quoted CLI refusal")
    leaked = [
        value
        for value in (sentinel, str(installation), arguments[4])
        if value in refusal.stdout or value in refusal.stderr
    ]
    if leaked:
        raise QualificationError(f"CLI refusal leaked caller values: {leaked}")
    if "the command arguments are not valid" not in refusal.stderr:
        raise QualificationError("CLI refusal omitted its fixed diagnostic")

    surface = json.loads(wheel_surface)
    return {
        "application_commands": len(surface["application"]),
        "probes": len(surface["probes"]),
        "exit_codes": len(surface["exit_codes"]),
        "help_exit": wheel_help.returncode,
        "quoted_refusal_exit": refusal.returncode,
    }


def _qualify_mcp(
    virtual_environment: Path,
    *,
    workdir: Path,
    source_env: Mapping[str, str],
    wheel_env: Mapping[str, str],
) -> dict[str, object]:
    executable = _entry_point(virtual_environment, "omnivia-core-mcp")
    if not executable.is_file():
        raise QualificationError(f"installed MCP entry point is absent: {executable}")

    source_surface = _snapshot(
        Path(sys.executable), MCP_SURFACE, cwd=REPO_ROOT, env=source_env
    )
    wheel_surface = _snapshot(
        _python(virtual_environment),
        MCP_SURFACE,
        cwd=workdir,
        env=wheel_env,
    )
    if wheel_surface != source_surface:
        raise QualificationError("MCP source and wheel tool documents differ")

    source_help = _run(
        [sys.executable, "-m", "omnivia_core_mcp.server", "--help"],
        cwd=REPO_ROOT,
        env=source_env,
    )
    _require(source_help, status=0, stderr="", label="source MCP --help")
    wheel_help = _run(
        [str(executable), "--help"], cwd=workdir, env=wheel_env
    )
    _require(
        wheel_help,
        status=0,
        stdout=source_help.stdout,
        stderr="",
        label="wheel MCP --help",
    )

    missing = workdir / "missing config & shell quoting.json"
    refusal = _run(
        _shell_command([str(executable), "--config", str(missing)]),
        cwd=workdir,
        env=wheel_env,
        shell=True,
    )
    _require(refusal, status=1, stdout="", label="shell-quoted MCP refusal")
    if not refusal.stderr or str(missing) in refusal.stderr:
        raise QualificationError(
            "MCP refusal must be nonempty, protocol-safe, and path-redacted"
        )

    surface = json.loads(wheel_surface)
    return {
        "manifest_version": surface["manifest_version"],
        "tools": len(surface["tools"]),
        "help_exit": wheel_help.returncode,
        "refusal_exit": refusal.returncode,
        "refusal_stdout_bytes": len(refusal.stdout.encode()),
    }


def main() -> int:
    print("V06-6 isolated-wheel executable qualification")
    print(f"platform: {platform.platform()}")
    print(f"python: {sys.version.split()[0]}")
    with tempfile.TemporaryDirectory(prefix="omnivia-v06-6-") as temporary:
        workdir = Path(temporary)
        wheelhouse = workdir / "wheelhouse"
        wheelhouse.mkdir()

        wheels = [_build_wheel(project, wheelhouse) for project in PROJECTS]
        mcp_wheel = next(path for path in wheels if path.name.startswith("omnivia_core_mcp-"))
        _must_run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--constraint",
                str(CONSTRAINTS),
                "--dest",
                str(wheelhouse),
                "--find-links",
                str(wheelhouse),
                str(mcp_wheel),
            ],
            cwd=REPO_ROOT,
        )

        cli_environment = workdir / "cli-environment"
        mcp_environment = workdir / "mcp-environment"
        cli_env = _install(cli_environment, "omnivia-core-cli", wheelhouse)
        mcp_env = _install(mcp_environment, "omnivia-core-mcp", wheelhouse)
        source_env = _source_environment()
        evidence = {
            "platform": platform.system().lower(),
            "machine": platform.machine().lower(),
            "python": sys.version.split()[0],
            "cli": _qualify_cli(
                cli_environment,
                workdir=workdir,
                source_env=source_env,
                wheel_env=cli_env,
            ),
            "mcp": _qualify_mcp(
                mcp_environment,
                workdir=workdir,
                source_env=source_env,
                wheel_env=mcp_env,
            ),
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
    print("V06-6 executable qualification passed with no skips or xfails.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationError, StopIteration) as error:
        print(f"qualification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
