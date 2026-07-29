"""Packaging-metadata proof for the omnivia-memory compatibility distribution.

The production repair this test covers makes ``omnivia-memory`` declare
``omnivia-core`` as a real package dependency
(``services/omnivia-memory/pyproject.toml``), not only via a ``PYTHONPATH``
trick that happens to work inside this checkout. This module proves that the
declaration survives the whole build-and-install path, using only locally
built artifacts:

1. build the ``omnivia-core`` and ``omnivia-memory`` wheels from this checkout
   with build isolation disabled (no index, no environment provisioning);
2. read ``Requires-Dist`` out of the compatibility wheel's own ``METADATA``
   and require its unconditional requirements to be exactly two -- one
   ``omnivia-core>=0.1.0,<0.2.0`` and one ``sqlalchemy>=2.0.0`` -- with every
   other entry gated on an ``extra``;
3. create a throwaway virtual environment and install both locally built
   wheels out of the wheelhouse with ``--no-index --no-deps``;
4. inside that environment, use ``importlib.metadata`` to confirm both
   distributions are installed at 0.1.0 and that the *installed*
   ``omnivia-memory`` metadata still carries the exact Core requirement.

Scope limits, stated explicitly so this evidence is not over-read:

* This is **not** a dependency-resolution proof. ``--no-deps`` deliberately
  disables resolution, and ``--no-index`` means no index is consulted; step 3
  proves the two artifacts install, not that pip would resolve
  ``omnivia-core`` for a user installing ``omnivia-memory`` from an index.
  The test asserts that SQLAlchemy is absent from the environment to keep
  that boundary visible rather than implied.
* This is **not** a full runtime-root import proof. Nothing is imported from
  the installed environment, precisely because ``--no-deps`` leaves the
  SQLAlchemy dependency uninstalled. Facade import behaviour and exact
  canonical object identity are covered by
  ``tests/compatibility/test_facade_foundation.py``.

Every step is offline and fail-closed: no index access, no pip cache
dependency, no ``pytest.skip`` path. All work happens outside the repository,
in a temporary directory removed on exit (success or failure); nothing is
written under the repo tree, and ``PYTHONPATH`` is stripped from the child
environments so no source tree leaks into the installed-artifact checks.

This intentionally does not touch the four-package topology check in
``scripts/check-package-boundaries.py`` / ``scripts/check-package-builds.sh`` /
``tests/test_package_boundaries.py``: ``omnivia-memory`` is not one of the four
``omnivia-core``/``omnivia-core-runtime``/``omnivia-core-mcp``/``omnivia-core-cli``
distributions in that topology (ADR-036). It is the separate, transitional
compatibility distribution this task is repairing, so it gets its own,
narrowly scoped packaging proof here instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = REPO_ROOT
MEMORY_DIR = REPO_ROOT / "services" / "omnivia-memory"
PYTHON = sys.executable

EXPECTED_VERSION = "0.1.0"
REQUIRED_CORE_DEPENDENCY = "omnivia-core>=0.1.0,<0.2.0"
REQUIRED_SQLALCHEMY_DEPENDENCY = "sqlalchemy>=2.0.0"


def _module_available(name: str) -> bool:
    result = subprocess.run([PYTHON, "-c", f"import {name}"], capture_output=True, check=False)
    return result.returncode == 0


def _child_env() -> dict[str, str]:
    """The repo suite runs with ``PYTHONPATH=.:src:services/omnivia-memory/src``.
    Drop it for every child process here so builds and installed-artifact
    checks see only their own environment, never this source tree."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _build_wheel(project_dir: Path, wheelhouse: Path) -> Path:
    """Build ``project_dir``'s wheel into ``wheelhouse`` without build
    isolation, so no build environment is provisioned from an index."""
    before = set(wheelhouse.glob("*.whl"))
    if _module_available("build"):
        command = [
            PYTHON, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(wheelhouse), str(project_dir),
        ]
    else:
        command = [
            PYTHON, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(wheelhouse), str(project_dir),
        ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False, env=_child_env()
    )
    assert result.returncode == 0, (
        f"failed to build a wheel for {project_dir}:\n{result.stdout}\n{result.stderr}"
    )
    (built,) = set(wheelhouse.glob("*.whl")) - before
    return built


def _wheel_metadata(wheel_path: Path) -> str:
    with zipfile.ZipFile(wheel_path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def _requires_dist(metadata: str) -> list[str]:
    return [
        line.removeprefix("Requires-Dist:").strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]


def _assert_declares_exactly_core_and_sqlalchemy(requires: list[str], *, source: str) -> None:
    """The *unconditional* requirements in ``requires`` must be exactly one
    ``omnivia-core`` requirement carrying the sanctioned range plus one
    ``sqlalchemy`` requirement -- compared by parsed name/specifier, so this is
    independent of however the build backend serialises a specifier, and closed
    against a second, looser Core pin being added alongside the first.

    Anything else in ``Requires-Dist`` must be gated on an ``extra`` (the
    project's ``dev`` optional-dependency group), so nothing that installs by
    default can hide behind a non-extra environment marker.
    """
    parsed = [Requirement(raw) for raw in requires]
    runtime = [req for req in parsed if req.marker is None]
    for req in parsed:
        if req.marker is None:
            continue
        assert "extra" in str(req.marker), (
            f"{source} declares {str(req)!r}, whose marker is not gated on an extra"
        )
    assert sorted(req.name for req in runtime) == ["omnivia-core", "sqlalchemy"], (
        f"{source} declares unconditional requirements {[str(r) for r in runtime]}, "
        "expected exactly one omnivia-core and one sqlalchemy requirement"
    )
    for expected in (REQUIRED_CORE_DEPENDENCY, REQUIRED_SQLALCHEMY_DEPENDENCY):
        wanted = Requirement(expected)
        (actual,) = [req for req in runtime if req.name == wanted.name]
        assert actual.specifier == wanted.specifier, (
            f"{source} declares {str(actual)!r}, expected {expected!r}"
        )


#: Emitted inside the throwaway environment. ``importlib.metadata.version``
#: raises ``PackageNotFoundError`` for a missing distribution, so a failed
#: install surfaces as a non-zero exit with a traceback rather than a pass.
INSTALLED_METADATA_SCRIPT = """
import importlib.metadata as md
import json

try:
    md.distribution("sqlalchemy")
    sqlalchemy_installed = True
except md.PackageNotFoundError:
    sqlalchemy_installed = False

print(json.dumps({
    "core_version": md.version("omnivia-core"),
    "memory_version": md.version("omnivia-memory"),
    "memory_requires": md.requires("omnivia-memory"),
    "sqlalchemy_installed": sqlalchemy_installed,
}))
"""


def test_compatibility_wheel_declares_core_requirement_and_both_wheels_install_offline() -> None:
    with tempfile.TemporaryDirectory(prefix="omnivia-facade-wheel-") as raw_workdir:
        workdir = Path(raw_workdir)
        wheelhouse = workdir / "wheelhouse"
        wheelhouse.mkdir()

        core_wheel = _build_wheel(CORE_DIR, wheelhouse)
        memory_wheel = _build_wheel(MEMORY_DIR, wheelhouse)
        assert core_wheel.name.startswith("omnivia_core-")
        assert memory_wheel.name.startswith("omnivia_memory-")

        _assert_declares_exactly_core_and_sqlalchemy(
            _requires_dist(_wheel_metadata(memory_wheel)),
            source=f"the built {memory_wheel.name} METADATA",
        )

        # Offline artifact install: a throwaway venv, no index, no resolution --
        # only the two wheels just built from this checkout.
        venv_dir = workdir / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        venv_python = venv_dir / "bin" / "python"

        install = subprocess.run(
            [
                str(venv_python), "-m", "pip", "install",
                "--no-index", "--no-deps", str(core_wheel), str(memory_wheel),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_child_env(),
        )
        assert install.returncode == 0, f"{install.stdout}\n{install.stderr}"

        # ``-I`` so the child ignores PYTHONPATH/user site and its own cwd:
        # what it reports comes from the installed dist-info, nothing else.
        report = subprocess.run(
            [str(venv_python), "-I", "-c", INSTALLED_METADATA_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(workdir),
            check=False,
            env=_child_env(),
        )
        assert report.returncode == 0, f"{report.stdout}\n{report.stderr}"
        installed = json.loads(report.stdout)

        assert installed["core_version"] == EXPECTED_VERSION
        assert installed["memory_version"] == EXPECTED_VERSION

        # The requirement survives the build-and-install round trip: the
        # installed metadata, not just the wheel's, still carries the pin.
        _assert_declares_exactly_core_and_sqlalchemy(
            installed["memory_requires"],
            source="the installed omnivia-memory metadata",
        )

        # Explicitly not a resolution proof: --no-deps left the declared
        # SQLAlchemy dependency uninstalled, which is why this test stops at
        # metadata and does not import from the installed environment.
        assert installed["sqlalchemy_installed"] is False
