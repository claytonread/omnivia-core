#!/usr/bin/env python3
"""Verify every workflow-collected module imports only what that workflow installs.

A GitHub workflow job states two things that have to agree and that nothing
currently compares: the distributions it installs, and the paths it hands to
``pytest``. When a module under one of those paths imports a distribution the
job did not install, pytest fails at *collection*, which takes down every case
in the file rather than the one that needed the import.

That is not hypothetical. ``phase2-platform.yml`` once installed the runtime, CLI
and MCP but not ``omnivia-core-client``, and the M2 lane wrote a phase2 module
that imported the client: green on every developer machine, red only in CI. This
script is that rule mechanised.

Packet section 17b.2 adds the client to that install list, closing that gap, and
17b.3 records the rule -- a guard is worth its weight only while the thing it
catches can still happen -- so the tests that pinned it are deleted, not replaced.
The check itself was never phase2-specific, and the reachable case today is
``core-performance-report.yml``, which installs the root distribution and
``omnivia-memory`` only while collecting ``benchmarks/tests``.

Both halves are derived from the tree, never hardcoded:

- the **install list** and the **test paths** come from parsing the workflow
  files themselves, per job, so renaming a path or moving an install line
  changes what this checks rather than quietly leaving it checking the old
  shape;
- the **distributions** come from walking the checkout for ``pyproject.toml``
  files that sit beside a ``src`` directory, and reading the import packages
  out of that ``src`` directory.

Imports are collected with ``ast.walk``, which descends into function bodies.
That is deliberate and is the whole reason the AST is used rather than a regex
over the first import block: the import that caused the original defect was
**function-local**, and pytest resolves it at collection time all the same.

Deliberate bounds, so a reader knows what a pass does and does not mean:

- Only *local* distributions are judged. A third-party import cannot be checked
  without a distribution-name-to-import-name map that is not derivable from
  this tree (``pymupdf`` imports as ``fitz``), and a hardcoded one is exactly
  the rot this script exists to avoid. Third-party gaps stay a review question.
- Every ``.py`` file under a named test path is scanned, not just the modules
  pytest itself collects, because a helper a test imports fails collection just
  as hard as the test module does. That is a superset, so it is strict rather
  than lenient.
- ``[tool.pytest.ini_options] pythonpath`` can make a distribution importable
  without being installed. Every workflow here installs the root distribution
  that the one configured entry serves, so this cannot fire today; if it ever
  does, the finding names the module and the workflow and reads as what it is.

No third-party dependency, and no YAML library: this parses the small, exact
slice of the workflow files it needs -- job boundaries by indentation, and
shell words by ``shlex`` -- so it runs before anything is installed.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIRECTORY = ".github/workflows"

SKIP_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__"})

# A job key: exactly two spaces of indentation, directly under a top-level
# `jobs:`. Steps, `with:` blocks and `strategy:` entries are all deeper than
# this, and `on:`'s children are at the same depth but not under `jobs:`.
_JOB_KEY = re.compile(r"^ {2}([A-Za-z0-9_.-]+):\s*$")


@dataclass(frozen=True)
class Distribution:
    """One locally installable distribution and the packages it provides."""

    install_target: str
    import_packages: frozenset[str]


@dataclass(frozen=True)
class Job:
    """One workflow job: what it installs, and which paths it collects."""

    workflow: str
    name: str
    install_targets: frozenset[str]
    test_paths: tuple[str, ...]
    missing_test_paths: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.workflow} [{self.name}]"


def _relative(path: Path, root: Path) -> str:
    """`path` as a repo-relative POSIX string, with the root itself as `.`."""
    text = path.relative_to(root).as_posix()
    return "." if text == "." else text


def _manifests(root: Path) -> list[Path]:
    """Every `pyproject.toml` in the checkout, skipping installed environments."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRECTORIES]
        if "pyproject.toml" in filenames:
            found.append(Path(dirpath) / "pyproject.toml")
    return sorted(found)


def python_files(base: Path) -> list[Path]:
    """Every `.py` file at or under `base`."""
    if base.is_file():
        return [base] if base.suffix == ".py" else []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRECTORIES]
        found.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py")
        )
    return sorted(found)


def local_distributions(root: Path) -> tuple[Distribution, ...]:
    """Every distribution this checkout can install, derived from the tree.

    A distribution is a directory holding a ``pyproject.toml`` beside a ``src``
    directory; the import packages it provides are the ``src`` subdirectories
    with an ``__init__.py``. Nothing is listed by name, so adding or renaming a
    distribution is picked up without editing this file.
    """
    found: list[Distribution] = []
    for manifest in _manifests(root):
        source_root = manifest.parent / "src"
        if not source_root.is_dir():
            continue
        packages = frozenset(
            entry.name
            for entry in source_root.iterdir()
            if (entry / "__init__.py").is_file()
        )
        if packages:
            found.append(Distribution(_relative(manifest.parent, root), packages))
    return tuple(found)


def _normalize_target(token: str) -> str:
    """A `pip install -e` argument reduced to the path it names."""
    target = token.split("[", 1)[0].rstrip("/")
    return target or "."


def _commands(lines: list[str]) -> list[str]:
    """Job body lines as logical commands, with `\\` continuations joined.

    Full-line comments are dropped. YAML keys survive as lines too; they are
    harmless because nothing below matches them.
    """
    commands: list[str] = []
    pending = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1].strip() + " "
            continue
        commands.append((pending + stripped).strip())
        pending = ""
    if pending:
        commands.append(pending.strip())
    return commands


def _job_spans(text: str) -> list[tuple[str, list[str]]]:
    """Split a workflow into `(job name, body lines)` pairs."""
    spans: list[tuple[str, list[str]]] = []
    in_jobs = False
    name: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" "):
            if name is not None:
                spans.append((name, body))
                name, body = None, []
            in_jobs = stripped == "jobs:"
            continue
        match = _JOB_KEY.match(line)
        if in_jobs and match is not None:
            if name is not None:
                spans.append((name, body))
            name, body = match.group(1), []
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        spans.append((name, body))
    return spans


def _parse_job(workflow: str, name: str, body: list[str], root: Path) -> Job:
    installs: set[str] = set()
    test_paths: list[str] = []
    missing: list[str] = []
    for command in _commands(body):
        if "pytest" not in command and "install" not in command:
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            # Prose that merely contains one of those words -- an unbalanced
            # apostrophe in a step `name:`, say. Not a command.
            continue
        if "pip" in tokens and "install" in tokens:
            for index, token in enumerate(tokens[:-1]):
                if token == "-e":
                    installs.add(_normalize_target(tokens[index + 1]))
        for index, token in enumerate(tokens):
            if token != "pytest":
                continue
            for candidate in tokens[index + 1 :]:
                if candidate.startswith("-"):
                    continue
                if (root / candidate).exists():
                    test_paths.append(candidate)
                elif "/" in candidate or candidate.endswith(".py"):
                    # Looks like a path and is not one: a rename that left the
                    # workflow behind, which silently collects nothing.
                    missing.append(candidate)
            break
    return Job(
        workflow=workflow,
        name=name,
        install_targets=frozenset(installs),
        test_paths=tuple(dict.fromkeys(test_paths)),
        missing_test_paths=tuple(dict.fromkeys(missing)),
    )


def workflow_jobs(root: Path) -> list[Job]:
    """Every job in every workflow, with its install list and test paths."""
    directory = root / WORKFLOW_DIRECTORY
    if not directory.is_dir():
        return []
    jobs: list[Job] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8")
        workflow = _relative(path, root)
        jobs.extend(
            _parse_job(workflow, name, body, root) for name, body in _job_spans(text)
        )
    return jobs


def top_level_imports(path: Path) -> set[tuple[str, int]]:
    """Every top-level package name a module imports, with its line.

    ``ast.walk`` visits function and method bodies as well as module scope, so
    a function-local import is reported exactly like a module-level one. That
    is the case this check exists for. Relative imports are intra-package and
    cannot cross a distribution boundary, so they are skipped.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add((node.module.split(".")[0], node.lineno))
    return found


def run_checks(root: Path = REPO_ROOT) -> list[str]:
    """Findings for every workflow job that names test paths. Empty means clean."""
    findings: list[str] = []
    providers: dict[str, Distribution] = {
        package: distribution
        for distribution in local_distributions(root)
        for package in distribution.import_packages
    }
    for job in workflow_jobs(root):
        for missing in job.missing_test_paths:
            findings.append(
                f"{job.label}: pytest names {missing!r}, which does not exist"
            )
        modules: list[Path] = []
        for test_path in job.test_paths:
            modules.extend(python_files(root / test_path))
        for module in sorted(dict.fromkeys(modules)):
            for name, lineno in sorted(top_level_imports(module)):
                distribution = providers.get(name)
                if distribution is None:
                    continue
                if distribution.install_target in job.install_targets:
                    continue
                findings.append(
                    f"{_relative(module, root)}:{lineno}: imports {name!r} from "
                    f"distribution {distribution.install_target!r}, which "
                    f"{job.label} does not install"
                )
    return findings


def main() -> int:
    findings = run_checks()
    if findings:
        print("OmniVia Core import/install alignment check FAILED:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("OmniVia Core import/install alignment check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
