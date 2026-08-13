"""Regression tests for the merge-blocking `Core acceptance` workflow.

These tests protect the stable identity of `.github/workflows/core-acceptance.yml`
(workflow name, job id, displayed check name, triggers) and the gate coverage a
reviewer relies on when the check is required by a branch rule or ruleset.

They do not use a YAML library: the repository has no YAML parser in its
dependency set, and adding one only to assert on CI configuration would be a new
dependency for no benefit. Instead the helpers below do the minimum structural
work that makes the assertions honest -- an unrestricted substring search over
the file text would pass just as happily if a required command were commented
out, or if a setting were moved into the wrong mapping.

What the helpers do:

* drop blank lines and whole-line comments (YAML comments outside `run:` blocks,
  shell comments inside them) so a commented-out command never counts as active;
* use indentation to extract a named key's value or nested block *at that
  block's own level*, so a setting only counts where it actually belongs;
* extract a step by its `- name:` line and read that step's inline `run:` value
  or `run: |` literal block, joining backslash continuations into one command.

What they deliberately do not do: this is not a YAML parser. It assumes the
fixed shape this one workflow already has -- block mappings and block sequences,
`- name:` as each step's first key, no flow collections, anchors, multi-line
folded scalars, quoted keys, or multiple documents -- and it does not strip
trailing inline comments. `test_helpers_ignore_commented_out_directives` pins
the comment behaviour against a fixture rather than the real workflow.

Note that this file cannot verify the check is actually merge-blocking. Requiring
`Core acceptance` is a GitHub branch protection / ruleset setting outside the
repository tree.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACCEPTANCE_WORKFLOW = WORKFLOW_DIR / "core-acceptance.yml"
PERFORMANCE_WORKFLOW = WORKFLOW_DIR / "core-performance-report.yml"
PHASE2_WORKFLOW = WORKFLOW_DIR / "phase2-platform.yml"
TLS_CONFORMANCE_WORKFLOW = WORKFLOW_DIR / "core-tls-conformance.yml"
CONFORMANCE_TREE = REPO_ROOT / "conformance"
TLS_SUITE = CONFORMANCE_TREE / "tls" / "test_tls_conformance.py"
TLS_HOST_LAUNCHER = CONFORMANCE_TREE / "tls" / "host.py"
COLLECTION_CHECKER = REPO_ROOT / "scripts" / "check-test-collection.py"

# Every way a pytest case stops being a pass without being a failure. Forbidden
# outright in `conformance/`, because a skipped test reads exactly like a passing
# one in a summary line and the owner's rule leaves no third outcome.
SKIP_CONSTRUCTS = frozenset({"skip", "skipif", "importorskip", "xfail"})

# (step name, exact command) for each single-command gate step, in the order the
# workflow must run them.
GATE_STEPS = (
    ("Check package boundaries", "python scripts/check-package-boundaries.py"),
    ("Run package boundary tests", "python -m pytest tests/test_package_boundaries.py -q"),
    ("Build and install-check all distributions", "PYTHON=python scripts/check-package-builds.sh"),
    ("Check application contracts", "python scripts/check-application-contracts.py"),
    # The MCP exposure manifest's advertised input and output schemas are
    # generated from the canonical Application Contract v1 documents and checked
    # in, because the canonical schemas are force-included into the
    # `omnivia-core` wheel and absent from an editable install. Without this gate
    # the committed module drifts from the schemas silently and `tools/list`
    # advertises a shape no contract vouches for.
    (
        "Check generated MCP exposure schemas",
        "python scripts/generate-mcp-exposure-schemas.py --check",
    ),
    ("Run application contract tests", "python -m pytest tests/contracts -q"),
    ("Check generated TypeScript contracts", "npm run check:application-contracts"),
    ("Check compatibility facade routes", "python scripts/check-facade-routes.py"),
    (
        "Run canonical migration and compatibility tests",
        "python -m pytest tests/canonical_migration tests/compatibility -q",
    ),
    # The resolving install / installed-root smoke. A standalone script rather
    # than a pytest module, so this step is the only place the network gate runs;
    # its explicit timeout is pinned below.
    (
        "Run compatibility root resolver and installed-root smoke",
        "python scripts/check-root-facade-resolver.py",
    ),
    ("Verify Phase 0 baseline", "PYTHON=python scripts/check-core-baseline.sh"),
    # The full suite, pinned exactly so a narrowed scope fails here. Naming paths
    # disables `testpaths` entirely, so `services` and `tests` are listed here
    # rather than inherited. `testpaths` used to be `["services", "tests"]`, which
    # is why a bare `pytest -q` reported a green full-repository run while
    # collecting nothing under `packages/`; it now names `packages` as well, and
    # `scripts/check-test-collection.py` keeps a bare run complete. This step
    # still names its paths so the gate's scope is visible in the gate itself
    # rather than depending on a configuration file it does not read. Each
    # distribution's whole `tests` tree is named rather than one phase inside it:
    # pinning `tests/phase2` kept the accepted Phase 3 authorization and protocol
    # suites off the gate even though they were committed. The five must stay one
    # invocation: several of these modules import public barrels, and splitting
    # the run is what hid the barrel-namespace drift.
    (
        "Run full repository test suite",
        (
            "python -m pytest services tests packages/omnivia-core-runtime/tests "
            "packages/omnivia-core-cli/tests packages/omnivia-core-client/tests "
            "packages/omnivia-core-mcp/tests -q"
        ),
    ),
    ("Run benchmark tests", "python -m pytest benchmarks/tests -q"),
)

# Lint steps whose command carries a target list; their scope gets its own test
# below, so they appear here only to pin their place in the gate order.
SCOPED_STEPS = ("Run Ruff", "Run strict mypy")

# The steps that execute the A2.7 accepted-checkpoint gate -- directly, or by
# collecting a pytest run that includes `tests/contracts` -- and so must be
# supplied the same repository-external anchor the checker itself refuses to
# substitute a local fallback for on a hosted run (see
# `_resolve_accepted_checkpoint` in `scripts/check-application-contracts.py`).
CONTRACT_CHECKPOINT_STEPS = (
    "Check application contracts",
    "Run application contract tests",
    "Run full repository test suite",
)

CONTRACT_CHECKPOINT_ENV_VALUE = "${{ vars.OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT }}"

# Ruff's currently accepted clean scope: the canonical trees plus every
# converted legacy facade/barrel file, and now the converted package root. This
# list is pinned exactly rather than counted, so an added target must be declared
# here and a dropped one fails.
REQUIRED_RUFF_TARGETS = (
    "src",
    "packages",
    "baseline",
    "tests",
    "scripts",
    "services/omnivia-memory/src/omnivia_memory/__init__.py",
    "services/omnivia-memory/src/omnivia_memory/_shared/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/validation.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/models.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/validation.py",
    "services/omnivia-memory/src/omnivia_memory/control_plane/imports.py",
    "services/omnivia-memory/src/omnivia_memory/control_plane/models.py",
    "services/omnivia-memory/src/omnivia_memory/control_plane/validation.py",
    "services/omnivia-memory/src/omnivia_memory/graph/models.py",
    "services/omnivia-memory/src/omnivia_memory/graph/search_models.py",
    "services/omnivia-memory/src/omnivia_memory/ingestion/models.py",
    "services/omnivia-memory/src/omnivia_memory/ingestion/watcher/models.py",
    "services/omnivia-memory/src/omnivia_memory/knowledge/models.py",
    "services/omnivia-memory/src/omnivia_memory/knowledge/normalize.py",
    "services/omnivia-memory/src/omnivia_memory/knowledge/validation.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/__init__.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/models.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/rules.py",
    "services/omnivia-memory/src/omnivia_memory/memory/models.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/assembly.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/fixtures.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/models.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/validation.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/provenance/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/validation.py",
    "services/omnivia-memory/src/omnivia_memory/workspace/models.py",
)

REQUIRED_MYPY_TARGETS = (
    "src/omnivia_core",
    "packages/omnivia-core-runtime/src/omnivia_core_runtime",
    "packages/omnivia-core-mcp/src/omnivia_core_mcp",
    "packages/omnivia-core-cli/src/omnivia_core_cli",
    "packages/omnivia-core-client/src/omnivia_core_client",
    "baseline/facade_manifest.py",
    "scripts/check-facade-routes.py",
    # Every converted facade wrapper -- the package root included, now that it is
    # a `root_facade` -- plus the nine strict-mypy consumer fixtures that import
    # them through their legacy paths: seven keyed on the leaf routes in
    # `baseline.inventory.FACADE_ROUTES`, `hybrid_barrel_consumer.py` keyed on the
    # six hybrid barrels' `__all__` tuples, and `root_facade_consumer.py` keyed on
    # the frozen root contract in `baseline.facade_manifest` (all three of those
    # are module routes, not symbol routes). Together they pin that
    # `omnivia-memory`'s `py.typed` surface still re-exports these names
    # explicitly and without `Any` leakage.
    "services/omnivia-memory/src/omnivia_memory/__init__.py",
    "services/omnivia-memory/src/omnivia_memory/_shared/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/validation.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/models.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/validation.py",
    "services/omnivia-memory/src/omnivia_memory/control_plane/imports.py",
    "services/omnivia-memory/src/omnivia_memory/control_plane/models.py",
    "services/omnivia-memory/src/omnivia_memory/control_plane/validation.py",
    "services/omnivia-memory/src/omnivia_memory/graph/models.py",
    "services/omnivia-memory/src/omnivia_memory/graph/search_models.py",
    "services/omnivia-memory/src/omnivia_memory/ingestion/models.py",
    "services/omnivia-memory/src/omnivia_memory/ingestion/watcher/models.py",
    "services/omnivia-memory/src/omnivia_memory/knowledge/models.py",
    "services/omnivia-memory/src/omnivia_memory/knowledge/normalize.py",
    "services/omnivia-memory/src/omnivia_memory/knowledge/validation.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/models.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/rules.py",
    "services/omnivia-memory/src/omnivia_memory/memory/models.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/assembly.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/fixtures.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/models.py",
    "services/omnivia-memory/src/omnivia_memory/memory_graph/validation.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/provenance/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/validation.py",
    "services/omnivia-memory/src/omnivia_memory/workspace/models.py",
    "tests/typing/accepted_legacy_facade_consumer.py",
    "tests/typing/graph_facade_consumer.py",
    "tests/typing/hybrid_barrel_consumer.py",
    "tests/typing/ingestion_models_facade_consumer.py",
    "tests/typing/knowledge_facade_consumer.py",
    "tests/typing/module_manifest_facade_consumer.py",
    "tests/typing/root_facade_consumer.py",
    "tests/typing/watcher_models_facade_consumer.py",
    "tests/typing/workspace_models_facade_consumer.py",
)

# Pinned tooling and test-only dependencies, quoted exactly as the workflow
# passes them to pip. `hatchling` is required because the wheel checks build with
# `python -m build --no-isolation`, which does not install the build backend.
REQUIRED_TOOLING_PINS = (
    '"build>=1.2,<2"',
    '"hatchling>=1.26,<2"',
    '"jsonschema[format]>=4.25,<5"',
    '"types-jsonschema>=4.25,<5"',
    '"pymupdf>=1.24,<2"',
    '"python-docx>=1.1,<2"',
)

# The root package must be installed before the compatibility distribution and
# the `packages/` distributions, all of whose `omnivia-core>=0.1.0,<0.2.0`
# dependency this checkout satisfies. Every distribution under `packages/` is
# listed: the acceptance suite imports each one, and an uninstalled distribution
# fails at collection rather than being skipped.
# The client now precedes the CLI, and that is a dependency edge rather than a
# preference: `omnivia-core-cli` declares `omnivia-core-client>=0.1.0,<0.2.0` for
# its concrete transport, and `omnivia-core-client` is as unpublished as
# `omnivia-core` is. Installing the CLI first sends pip to an index that has
# neither. Same rule as the root install below, one edge further down.
REQUIRED_LOCAL_INSTALLS = (
    "python -m pip install -e .",
    'python -m pip install -e "services/omnivia-memory[dev]"',
    "python -m pip install -e packages/omnivia-core-runtime",
    "python -m pip install -e packages/omnivia-core-client",
    "python -m pip install -e packages/omnivia-core-cli",
    "python -m pip install -e packages/omnivia-core-mcp",
)

# The same ordering constraint in the other two workflows, pinned because both
# broke on it. `omnivia-core` is unpublished, so every local distribution that
# depends on it -- `omnivia-memory` and the three `packages/` distributions --
# can only resolve against this checkout, and only if it is installed first.
# Installed the other way round, pip reaches the index and the job dies before
# it runs anything.
PHASE2_LOCAL_INSTALLS = (
    "python -m pip install -e .",
    'python -m pip install -e "services/omnivia-memory[dev]"',
    "python -m pip install -e packages/omnivia-core-runtime",
    # Packet section 17b.2: installed before the CLI dependency edge; the
    # named-pipe parity suite also imports it directly.
    "python -m pip install -e packages/omnivia-core-client",
    "python -m pip install -e packages/omnivia-core-cli",
    "python -m pip install -e packages/omnivia-core-mcp",
)

# The performance job invokes pip as `python3`, so the commands are pinned as it
# actually writes them rather than normalised.
PERFORMANCE_LOCAL_INSTALLS = (
    "python3 -m pip install -e .",
    'python3 -m pip install -e "services/omnivia-memory[dev]"',
)

PHASE2_JOB = "phase2-platform"
PHASE2_INSTALL_STEP = "Install Python tooling and local packages"
PHASE2_MATRIX = "[ubuntu-latest, macos-latest, windows-latest]"
V06_6_EXECUTABLE_CHECK = REPO_ROOT / "scripts" / "check-v06-6-executables.py"

# A workflow-shaped fixture for the helper meta-test. Every commented directive
# here must be invisible to the helpers; every uncommented one must be found.
COMMENTED_FIXTURE = """\
name: Fixture workflow
on:
  pull_request:

jobs:
  fixture:
    # continue-on-error: true
    # name: Commented job name
    name: Fixture job
    steps:
      - name: Run tests
        run: |
          # python -m pytest --exitfirst

          python -m pytest -q
"""

# A parsed line: its indentation, and its content with surrounding space removed.
Line = tuple[int, str]


def _significant(text: str) -> list[Line]:
    """Parse `text` into (indent, content), dropping blanks and whole-line comments.

    Whole-line comments are dropped whether they are YAML comments or shell
    comments inside a `run:` block; both are inactive. Trailing inline comments
    are left in place -- stripping them would need quote handling this workflow
    does not require.
    """
    lines: list[Line] = []
    for raw in text.splitlines():
        content = raw.strip()
        if not content or content.startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip()), content))
    return lines


def _own_indent(block: list[Line]) -> int:
    """The indentation of `block`'s own keys, i.e. its shallowest lines."""
    assert block, "cannot inspect an empty block"
    return min(indent for indent, _ in block)


def _children(block: list[Line], index: int) -> list[Line]:
    """The lines nested under `block[index]`, up to the next line at its level."""
    indent = block[index][0]
    nested: list[Line] = []
    for child_indent, content in block[index + 1 :]:
        if child_indent <= indent:
            break
        nested.append((child_indent, content))
    return nested


def _locate(block: list[Line], key: str) -> int | None:
    """The index of `key:` among `block`'s own keys, or None. Rejects duplicates."""
    indent = _own_indent(block)
    matches = [
        i
        for i, (line_indent, content) in enumerate(block)
        if line_indent == indent and (content == f"{key}:" or content.startswith(f"{key}: "))
    ]
    assert len(matches) <= 1, f"duplicate `{key}:` key in mapping"
    return matches[0] if matches else None


def _entry(block: list[Line], key: str) -> str | None:
    """The scalar value of `key:` among `block`'s own keys, or None if absent."""
    index = _locate(block, key)
    if index is None:
        return None
    return block[index][1][len(key) + 1 :].strip()


def _block(block: list[Line], key: str) -> list[Line]:
    """The lines nested under `key:` among `block`'s own keys."""
    index = _locate(block, key)
    assert index is not None, f"missing `{key}:` key"
    nested = _children(block, index)
    assert nested, f"`{key}:` has no nested lines"
    return nested


def _keys(block: list[Line]) -> list[str]:
    """The names of `block`'s own mapping keys."""
    indent = _own_indent(block)
    return [
        content.split(":", 1)[0]
        for line_indent, content in block
        if line_indent == indent and ":" in content and not content.startswith("- ")
    ]


def _step(steps: list[Line], name: str) -> list[Line]:
    """The lines of the step introduced by `- name: <name>`."""
    indent = _own_indent(steps)
    for index, (line_indent, content) in enumerate(steps):
        if line_indent == indent and content == f"- name: {name}":
            nested = _children(steps, index)
            assert nested, f"step `{name}` has no body"
            return nested
    raise AssertionError(f"missing step: {name}")


def _step_names(steps: list[Line]) -> list[str]:
    """Every step's name, in workflow order."""
    indent = _own_indent(steps)
    return [
        content[len("- name: ") :]
        for line_indent, content in steps
        if line_indent == indent and content.startswith("- name: ")
    ]


def _commands(step: list[Line]) -> tuple[str, ...]:
    """The active shell commands of a step's inline `run:` or `run: |` script.

    Blank and comment lines are already gone; backslash continuations are joined
    so a multi-line command is matched as the one command the shell would run.
    """
    index = _locate(step, "run")
    assert index is not None, "step has no `run:` script"
    inline = step[index][1][len("run:") :].strip()
    if inline not in {"|", "|-", "|+"}:
        return (inline,)

    commands: list[str] = []
    pending = ""
    for _, fragment in _children(step, index):
        if fragment.endswith("\\"):
            pending += f"{fragment[:-1].strip()} "
            continue
        commands.append(f"{pending}{fragment}".strip())
        pending = ""
    assert not pending, "`run:` script ends with a dangling continuation"
    return tuple(commands)


def _text() -> str:
    return ACCEPTANCE_WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> list[Line]:
    return _significant(_text())


def _job() -> list[Line]:
    return _block(_block(_workflow(), "jobs"), "core-acceptance")


def _steps() -> list[Line]:
    return _block(_job(), "steps")


def _job_of(workflow: Path, job: str) -> list[Line]:
    """The named job of any workflow, through the same structural helpers."""
    return _block(_block(_significant(workflow.read_text(encoding="utf-8")), "jobs"), job)


def _steps_of(workflow: Path, job: str) -> list[Line]:
    return _block(_job_of(workflow, job), "steps")


def _assert_install_order(commands: tuple[str, ...], required: tuple[str, ...]) -> None:
    """Every install is present, and they run in the order `required` lists them."""
    positions = []
    for install in required:
        assert install in commands, f"missing install: {install}"
        positions.append(commands.index(install))
    assert positions == sorted(positions), f"installs are out of order: {commands}"


def test_acceptance_workflow_exists() -> None:
    assert ACCEPTANCE_WORKFLOW.is_file(), f"missing workflow: {ACCEPTANCE_WORKFLOW}"


def test_stable_workflow_and_job_identity() -> None:
    workflow = _workflow()

    # Workflow name, at the top level.
    assert _entry(workflow, "name") == "Core acceptance"

    # Job id and its displayed name -- the string branch rules reference.
    jobs = _block(workflow, "jobs")
    assert _keys(jobs) == ["core-acceptance"], "the gate must be a single job"

    job = _block(jobs, "core-acceptance")
    assert _entry(job, "name") == "Core acceptance"
    assert _entry(job, "runs-on") == "ubuntu-latest"
    assert _entry(job, "timeout-minutes") == "30"
    assert _entry(_block(job, "permissions"), "contents") == "read"


def test_triggers_are_pull_request_and_manual_dispatch() -> None:
    triggers = _keys(_block(_workflow(), "on"))
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers


def test_no_pull_request_path_filtering() -> None:
    offenders = [
        content
        for _, content in _workflow()
        if content.startswith(("paths:", "paths-ignore:", "- paths"))
    ]
    assert not offenders, f"acceptance gate must run for every pull request, found: {offenders}"


def test_gate_is_not_informational() -> None:
    offenders = [content for _, content in _workflow() if content.startswith("continue-on-error")]
    assert not offenders, f"the gate must fail the check, found: {offenders}"


def test_checkout_fetches_full_history() -> None:
    step = _step(_steps(), "Check out repository")
    assert _entry(step, "uses") == "actions/checkout@v7"
    # Needed by both diff-check steps to resolve their commit ranges.
    assert _entry(_block(step, "with"), "fetch-depth") == "0"


def test_python_and_node_versions() -> None:
    steps = _steps()

    python_step = _step(steps, "Set up Python")
    assert _entry(python_step, "uses") == "actions/setup-python@v7"
    assert _entry(_block(python_step, "with"), "python-version") == '"3.11"'

    node_step = _step(steps, "Set up Node")
    assert _entry(node_step, "uses") == "actions/setup-node@v7"
    node_options = _block(node_step, "with")
    assert _entry(node_options, "node-version") == '"22"'
    assert _entry(node_options, "cache") == "npm"
    assert _entry(node_options, "cache-dependency-path") == "package-lock.json"


def test_python_tooling_installation_pins_the_build_backend() -> None:
    commands = _commands(_step(_steps(), "Install Python tooling and test-only dependencies"))
    assert "python -m pip install --upgrade pip" in commands

    pinned = [
        command
        for command in commands
        if command.startswith("python -m pip install ") and not command.endswith("--upgrade pip")
    ]
    assert len(pinned) == 1, f"expected one pinned dependency install, got: {pinned}"
    arguments = pinned[0].split()
    for pin in REQUIRED_TOOLING_PINS:
        assert pin in arguments, f"missing pinned dependency: {pin}"


def test_local_packages_install_root_before_compatibility_distribution() -> None:
    _assert_install_order(
        _commands(_step(_steps(), "Install local packages")), REQUIRED_LOCAL_INSTALLS
    )


def test_node_dependencies_use_the_lockfile() -> None:
    assert _commands(_step(_steps(), "Install Node dependencies")) == ("npm ci",)


def test_required_commands_run_in_their_own_steps_and_in_order() -> None:
    steps = _steps()
    for name, command in GATE_STEPS:
        assert command in _commands(_step(steps, name)), f"step `{name}` must run: {command}"

    order = _step_names(steps)
    expected = [name for name, _ in GATE_STEPS] + list(SCOPED_STEPS)
    positions = [order.index(name) for name in expected]
    assert positions == sorted(positions), f"gate steps are out of order: {order}"


def test_contract_checkpoint_env_is_supplied_from_repository_vars_and_only_there() -> None:
    """Every step that runs the A2.7 accepted-checkpoint gate must read
    `OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT` from
    `vars.OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT` -- repository-external GitHub
    configuration, never the in-tree fallback constant the checker itself refuses
    on a hosted run.

    This has to be asserted structurally, at each step's own `env:` block: the
    same substring appears in this file's plain-prose comment describing why the
    anchor is external, and a naive `"OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT" in
    text` check would pass on that comment alone even if every step's `env:` were
    deleted. `_significant()` already drops whole-line comments before any of
    this file's helpers see them, so a commented-out `env:` entry cannot satisfy
    `_entry` either.

    The carrying steps are also compared as an exact set, not just checked for
    containment: a step that should not need the anchor picking it up anyway --
    or a renamed/new step silently missing it -- must fail here too, not only a
    known step losing it.
    """
    steps = _steps()
    carrying = []
    for name in _step_names(steps):
        step = _step(steps, name)
        if _locate(step, "env") is None:
            continue
        environment = _block(step, "env")
        value = _entry(environment, "OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT")
        if value is None:
            continue
        assert value == CONTRACT_CHECKPOINT_ENV_VALUE, name
        carrying.append(name)

    assert sorted(carrying) == sorted(CONTRACT_CHECKPOINT_STEPS)


def _audit_targets(command: str, prefix: str, required: tuple[str, ...]) -> None:
    """Assert `command` is exactly `prefix` followed by `required`, as a set.

    Set equality, not containment: a target list that only has to *contain* the
    required entries accepts extras -- a tree silently added to the linted scope,
    or a stray flag -- and an audit built from a set accepts duplicates, which
    hide a copy/paste that was meant to add a new target and did not. Both are
    reported by name. Order is deliberately not pinned; the workflow groups its
    targets for readability and reordering them changes nothing.
    """
    assert command.startswith(f"{prefix} "), f"expected the `{prefix}` prefix: {command}"
    targets = command[len(prefix) :].split()

    duplicates = sorted({target for target in targets if targets.count(target) > 1})
    assert not duplicates, f"`{prefix}` names these targets more than once: {duplicates}"

    actual = set(targets)
    expected = set(required)
    assert len(expected) == len(required), f"REQUIRED targets are not unique: {required}"
    assert actual == expected, (
        f"`{prefix}` scope drifted: missing={sorted(expected - actual)}, "
        f"extra={sorted(actual - expected)}"
    )


@pytest.mark.parametrize(
    ("targets", "pattern"),
    [
        (("src", "packages"), "missing="),
        (("src", "packages", "baseline", "extra"), "extra="),
        (("src", "packages", "baseline", "src"), "more than once"),
    ],
    ids=["missing", "extra", "duplicate"],
)
def test_target_audit_rejects_missing_extra_and_duplicate_targets(
    targets: tuple[str, ...], pattern: str
) -> None:
    """The audit itself, against a three-target requirement: dropping one, adding
    one, and repeating one must each fail, and the duplicate case must fail *as* a
    duplicate rather than being collapsed into an equal set."""
    command = "python -m ruff check " + " ".join(targets)
    with pytest.raises(AssertionError, match=pattern):
        _audit_targets(command, "python -m ruff check", ("src", "packages", "baseline"))


def test_target_audit_accepts_the_exact_required_set() -> None:
    """The defect-free base for the three rejections above."""
    _audit_targets(
        "python -m ruff check src packages baseline",
        "python -m ruff check",
        ("src", "packages", "baseline"),
    )


def test_ruff_covers_the_accepted_clean_scope() -> None:
    """The step prints the resolved linter, then runs it over the pinned scope.

    Exactly two commands, in that order -- not "the check is in there
    somewhere". The version print is evidence only if it comes from the same step
    and the same interpreter as the verdict it explains, and pinning the count is
    what keeps an unrelated command from joining a merge-blocking step unnoticed.
    """
    commands = _commands(_step(_steps(), "Run Ruff"))
    assert commands[:1] == ("python -m ruff --version",), (
        "the merge-blocking Ruff step must print its resolved version before it "
        f"runs, so the log records which linter produced the verdict: {commands}"
    )
    assert len(commands) == 2, f"expected a version print and one Ruff run, got: {commands}"
    _audit_targets(commands[1], "python -m ruff check", REQUIRED_RUFF_TARGETS)


def test_mypy_runs_strict_over_canonical_and_distribution_sources() -> None:
    """The step prints the resolved analyser, then runs it over the pinned scope.

    Exactly two commands, in that order -- not "the strict run is in there
    somewhere". The version print is evidence only if it comes from the same step
    and the same interpreter as the verdict it explains, and pinning the count is
    what keeps an unrelated command from joining a merge-blocking step unnoticed.
    """
    commands = _commands(_step(_steps(), "Run strict mypy"))
    assert commands[:1] == ("python -m mypy --version",), (
        "the merge-blocking mypy step must print its resolved version before it "
        f"runs, so the log records which analyser produced the verdict: {commands}"
    )
    assert len(commands) == 2, f"expected a version print and one mypy run, got: {commands}"
    _audit_targets(commands[1], "python -m mypy --strict", REQUIRED_MYPY_TARGETS)


def test_pull_request_range_diff_check() -> None:
    step = _step(_steps(), "Check pull-request diff for whitespace errors")
    assert _entry(step, "if") == "github.event_name == 'pull_request'"

    # Base and head come from the pull-request event via this step's environment.
    environment = _block(step, "env")
    assert _entry(environment, "BASE_SHA") == "${{ github.event.pull_request.base.sha }}"
    assert _entry(environment, "HEAD_SHA") == "${{ github.event.pull_request.head.sha }}"

    # Triple-dot: only changes introduced by the pull request.
    assert _commands(step) == ('git diff --check "${BASE_SHA}...${HEAD_SHA}"',)


def test_dispatch_diff_check_uses_the_default_branch_range() -> None:
    step = _step(_steps(), "Check dispatch diff for whitespace errors")
    assert _entry(step, "if") == "github.event_name != 'pull_request'"

    # A manual run has no pull-request range, so it compares against the
    # repository default branch instead of the (always clean) working tree.
    environment = _block(step, "env")
    assert _entry(environment, "DEFAULT_BRANCH") == "${{ github.event.repository.default_branch }}"
    assert _commands(step) == ('git diff --check "origin/${DEFAULT_BRANCH}...HEAD"',)


def test_helpers_ignore_commented_out_directives() -> None:
    """Commented directives must not be mistaken for active configuration."""
    fixture = _significant(COMMENTED_FIXTURE)
    job = _block(_block(fixture, "jobs"), "fixture")

    # A YAML comment neither supplies a value nor shadows the real one.
    assert _entry(job, "continue-on-error") is None
    assert _entry(job, "name") == "Fixture job"

    # A shell comment inside a `run: |` block is not an active command, and the
    # blank line between the two does not end the script.
    commands = _commands(_step(_block(job, "steps"), "Run tests"))
    assert commands == ("python -m pytest -q",)

    # For contrast: the naive substring check these helpers replace passes here.
    assert "python -m pytest --exitfirst" in COMMENTED_FIXTURE


def test_performance_workflow_stays_separate_and_informational() -> None:
    assert PERFORMANCE_WORKFLOW.is_file()
    assert PERFORMANCE_WORKFLOW != ACCEPTANCE_WORKFLOW

    performance_text = PERFORMANCE_WORKFLOW.read_text(encoding="utf-8")
    assert "name: Core Performance Report" in performance_text
    assert "continue-on-error" in performance_text, "performance report stays informational"
    assert "Core acceptance" not in performance_text

    # The acceptance gate must not delegate to, or reuse, the informational
    # performance workflow.
    assert "core-performance" not in _text()


def test_performance_workflow_installs_the_root_checkout_first() -> None:
    """The whole job died on this: `omnivia-memory` was installed on its own, pip
    went looking for the unpublished `omnivia-core` on the index, and the run
    failed at dependency resolution without reaching a benchmark."""
    steps = _steps_of(PERFORMANCE_WORKFLOW, "performance-report")
    step = _step(steps, "Install Core test dependencies")
    _assert_install_order(_commands(step), PERFORMANCE_LOCAL_INSTALLS)


def test_phase2_workflow_installs_the_root_checkout_before_its_dependents() -> None:
    """Root, then the compatibility distribution, then the three local packages.

    `services/omnivia-memory[dev]` was absent altogether, which is why FM-22's
    `import omnivia_memory` failed on the macOS and Windows rows; it is ordered
    after the root install for the same reason the acceptance job orders them.
    """
    step = _step(_steps_of(PHASE2_WORKFLOW, PHASE2_JOB), PHASE2_INSTALL_STEP)
    _assert_install_order(_commands(step), PHASE2_LOCAL_INSTALLS)


def test_phase2_install_commands_are_quoted_for_every_hosted_shell() -> None:
    """One script runs on bash and on PowerShell, so the arguments carrying shell
    metacharacters are quoted rather than relying on the runner's default shell
    treating `[`, `]`, `>` and `<` as literals."""
    step = _step(_steps_of(PHASE2_WORKFLOW, PHASE2_JOB), PHASE2_INSTALL_STEP)
    unquoted = [
        (command, argument)
        for command in _commands(step)
        for argument in command.split()
        if any(character in argument for character in "[]<>")
        and not (argument.startswith('"') and argument.endswith('"'))
    ]
    assert not unquoted, f"shell metacharacters left unquoted: {unquoted}"


def test_phase2_workflow_keeps_every_platform_row_and_stays_fail_closed() -> None:
    """The matrix and its qualification steps are the evidence this workflow exists to
    collect, so none of it may be narrowed, skipped or made informational to get a
    green run: a suppressed Windows row reports success for the one platform that
    has never proved anything."""
    job = _job_of(PHASE2_WORKFLOW, PHASE2_JOB)

    strategy = _block(job, "strategy")
    assert _entry(strategy, "fail-fast") == "false"
    assert _entry(_block(strategy, "matrix"), "os") == PHASE2_MATRIX

    offenders = [
        content
        for _, content in _significant(PHASE2_WORKFLOW.read_text(encoding="utf-8"))
        if content.startswith(("continue-on-error", "if:"))
    ]
    assert not offenders, f"a platform row must fail the workflow, found: {offenders}"

    steps = _block(job, "steps")
    # The whole Phase 2 directory, not a selected subset, and then the gate that
    # proves this platform's own lock case ran rather than skipped.
    assert (
        "python -m pytest packages/omnivia-core-runtime/tests/phase2 -q -rs"
        in _commands(_step(steps, "Run Phase 2 acceptance suite"))
    )
    assert (
        "python -m pytest "
        "packages/omnivia-core-runtime/tests/phase3/protocol/"
        "test_windows_named_pipe.py -q -rs"
        in _commands(_step(steps, "Run Windows named-pipe client parity"))
    )
    assert "python scripts/check-platform-lock-coverage.py" in _commands(
        _step(steps, "Assert this platform's lock case actually ran")
    )
    companion = " ".join(
        _commands(_step(steps, "Build and test the macOS status menu companion"))
    )
    assert 'if [ "${RUNNER_OS}" = "macOS" ]; then' in companion
    assert "swift build --package-path apps/core-status-menu-macos" in companion
    assert "swift test --package-path apps/core-status-menu-macos" in companion
    assert (REPO_ROOT / "scripts" / "check-platform-lock-coverage.py").is_file()

    # V06-6 requires the installed MCP and CLI console scripts to be invoked in
    # every row. An editable install or import check is explicitly insufficient.
    executable_step = _step(steps, "Qualify isolated-wheel MCP and CLI executables")
    assert _commands(executable_step) == (
        "python scripts/check-v06-6-executables.py",
    )
    assert V06_6_EXECUTABLE_CHECK.is_file()


BUILD_SCRIPT = REPO_ROOT / "scripts" / "check-package-builds.sh"
WHEELHOUSE_CONSTRAINTS = REPO_ROOT / "scripts" / "mcp-wheelhouse-constraints.txt"


def _active_build_script() -> str:
    """The build gate's executable lines, with comments dropped.

    Every assertion below is a substring search, and this file's own header
    explains why that is only honest against active lines: the script documents
    each flag in a comment directly above the line that carries it, so a plain
    search over the file text passes on the *explanation* after the flag itself
    has been deleted. That is not hypothetical -- the first version of
    `test_the_wheelhouse_closure_is_pinned_by_reviewed_constraints` stayed green
    with `--constraint` removed from the only command that used it.
    """
    return "\n".join(
        line
        for line in BUILD_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_the_wheelhouse_gate_requires_wheels_in_both_phases() -> None:
    """R005-03: "Require wheels. Do not silently fall back to source distributions."

    `--no-index --find-links` carries no wheel-only constraint of its own. With an
    sdist in the wheelhouse and a build backend beside it, pip builds from source
    fully offline and reports success -- observed, not theorised. `--only-binary`
    has to be on the acquisition download *and* on every isolated install, because
    the two close different halves: phase 1 staging an sdist, and phase 2 building
    one that reached the wheelhouse another way.
    """
    active = _active_build_script()

    # Exactly one of each, so "both invocations are wheel-only" is a countable
    # claim rather than a hope that the flag landed on the right one.
    assert active.count("pip download") == 1
    assert active.count("pip install") == 1
    assert active.count("--only-binary=:all:") == 2, (
        "both pip invocations must be wheel-only; --no-index --find-links alone "
        "builds an sdist from source, fully offline, and reports success"
    )
    assert "--no-index" in active, "the install phase must not reach an index"


def test_the_wheelhouse_closure_is_pinned_by_reviewed_constraints() -> None:
    """R005-03 Phase 1: resolve "against the repository's exact dependency lock or
    equivalent reviewed constraints", and its rejected alternative: "an unpinned,
    time-varying closure from the index is not accepted as deterministic evidence."

    `uv.lock` names none of this closure -- checked here rather than assumed, so
    that a future lock which *does* cover it is noticed rather than shadowed by a
    second source of truth. Until then these pins are the reviewed equivalent, and
    every one of them must be an exact `==`.
    """
    active = _active_build_script()
    assert "--constraint" in active
    assert WHEELHOUSE_CONSTRAINTS.name in active or "${CONSTRAINTS}" in active

    pins = [
        line.split("#", 1)[0].strip()
        for line in WHEELHOUSE_CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]
    assert pins, "the constraints file pins nothing"
    for pin in pins:
        assert "==" in pin, f"not an exact pin: {pin!r}"

    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "mcp"' not in lock, (
        "uv.lock now covers the MCP closure; resolve against the lock itself rather "
        "than keeping a second reviewed source of truth"
    )


def test_the_wheelhouse_gate_records_versions_and_hashes_not_a_listing() -> None:
    """R005-03 Phase 1 requires the staged names, versions *and hashes* as test
    evidence. `ls -1` recorded none of the three in a checkable form."""
    active = _active_build_script()
    assert "hashlib.sha256(" in active, "no hash is computed"
    assert "sha256:{digest}" in active, "the computed hash is not recorded"
    assert 'ls -1 "${WHEELHOUSE}"' not in active, "back to a bare listing"


def test_the_gate_does_not_claim_the_acquisition_phase_is_offline() -> None:
    """R005-03's required record correction: "Rename or document the gate so that it
    does not imply the acquisition phase is offline." The property being proven is
    that a prepared wheelhouse suffices for installation without an index -- so both
    the script and the README have to say that, in those terms."""
    claim = "a prepared wheelhouse is sufficient for installation without an index"
    for path in (BUILD_SCRIPT, REPO_ROOT / "README.md"):
        # The claim is line-wrapped in both files, and in the script each line
        # carries a `#` and in the README a `*`, so compare on normalised text.
        text = path.read_text(encoding="utf-8").lower().replace("#", " ").replace("*", " ")
        assert claim in " ".join(text.split()), (
            f"{path.name} does not state the property being proven"
        )


MCP_SCHEMA_GATE_STEP = "Check generated MCP exposure schemas"
MCP_SCHEMA_GENERATOR = "scripts/generate-mcp-exposure-schemas.py"
MCP_GENERATED_MODULE = (
    "packages/omnivia-core-mcp/src/omnivia_core_mcp/generated_schema_projection.py"
)
PREFLIGHT = REPO_ROOT / "scripts" / "preflight"


def test_the_generated_mcp_schema_gate_runs_locally_and_on_the_gate() -> None:
    """`--check` is on both the merge-blocking gate and `./scripts/preflight`.

    A generated artifact with no drift check is a stale artifact waiting to
    happen, and this one is what `tools/list` advertises: the canonical schemas
    move, the committed module does not, and every MCP host is then handed a
    shape no contract vouches for. `GATE_STEPS` pins the workflow step and its
    place in the order; this pins the other two halves -- that preflight runs the
    same command, so the failure is local rather than first seen on the pull
    request, and that both the generator and the module it owns exist.
    """
    assert (REPO_ROOT / MCP_SCHEMA_GENERATOR).is_file()
    assert (REPO_ROOT / MCP_GENERATED_MODULE).is_file()

    assert (MCP_SCHEMA_GATE_STEP, f"python {MCP_SCHEMA_GENERATOR} --check") in GATE_STEPS

    preflight = "\n".join(
        line
        for line in PREFLIGHT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert f"{MCP_SCHEMA_GENERATOR} --check" in preflight, (
        "preflight must run the same drift check the gate does"
    )


def _preflight_full_suite_command() -> str:
    """The `step "Run full repository test suite" ...` invocation, joined.

    Not a YAML step: `scripts/preflight` is a shell script, so this mirrors the
    workflow helpers' backslash-continuation joining rather than reusing
    `_commands`, which only reads workflow-shaped `run:` blocks.
    """
    lines = PREFLIGHT.read_text(encoding="utf-8").splitlines()
    prefix = 'step "Run full repository test suite" '
    start = next(index for index, line in enumerate(lines) if line.startswith(prefix))
    parts = [lines[start][len(prefix) :].rstrip("\\").strip()]
    index = start
    while lines[index].rstrip().endswith("\\"):
        index += 1
        parts.append(lines[index].rstrip("\\").strip())
    return " ".join(parts)


def test_preflight_full_suite_matches_the_workflow_path_set() -> None:
    """`./scripts/preflight` used to omit the CLI and MCP package test trees, so a
    green local run certified less than `Core acceptance`'s `Run full repository
    test suite` step actually covers. This pins the local invocation to the exact
    command the workflow runs, in the same order."""
    assert _preflight_full_suite_command() == dict(GATE_STEPS)[FULL_SUITE_STEP]


RESOLVER_SMOKE_STEP = "Run compatibility root resolver and installed-root smoke"


RESOLVER_SCRIPT = "scripts/check-root-facade-resolver.py"


def test_resolver_smoke_step_is_bounded_and_fail_closed() -> None:
    """The resolving install is the one gate step that reaches the network, so it
    carries its own explicit timeout rather than relying on the job's 30 minutes --
    a hung index would otherwise burn the whole budget before failing. It must also
    be a single command with no skip flag and no offline fallback: a step that
    quietly degraded to `--no-deps` or `--no-index` would report a pass for a proof
    it had not run.
    """
    step = _step(_steps(), RESOLVER_SMOKE_STEP)
    assert _entry(step, "timeout-minutes") == "12"
    assert _entry(step, "continue-on-error") is None
    assert _entry(step, "if") is None

    commands = _commands(step)
    assert commands == (f"python {RESOLVER_SCRIPT}",)
    for forbidden in ("--no-deps", "--no-index", "|| true", "continue-on-error", "-k "):
        assert forbidden not in commands[0], forbidden


def test_the_network_resolver_smoke_runs_exactly_once() -> None:
    """The gate is a script rather than a pytest module precisely so that this is
    provable from the workflow: the two broad pytest commands in this job collect
    `tests/` -- once as `tests/canonical_migration tests/compatibility` and once as
    the whole suite -- so a test module holding the resolving install would run the
    network smoke three times per job.

    Exactly one active step names the script, and no pytest command in the workflow
    names it or the module it used to live in.
    """
    steps = _steps()
    # `uses:` steps (checkout, setup-python, setup-node) have no `run:` script.
    by_step = {
        name: _commands(_step(steps, name))
        for name in _step_names(steps)
        if _locate(_step(steps, name), "run") is not None
    }
    naming = sorted(
        name
        for name, commands in by_step.items()
        if any(RESOLVER_SCRIPT in command for command in commands)
    )
    assert naming == [RESOLVER_SMOKE_STEP]

    assert (REPO_ROOT / RESOLVER_SCRIPT).is_file()
    # `scripts/` is outside pytest's `testpaths`, and the file matches no default
    # `python_files` pattern; `tests/compatibility/test_root_facade_distribution.py`
    # keeps only the offline metadata checks and the structural pins for the script.
    for commands in by_step.values():
        for command in commands:
            if "pytest" in command:
                assert RESOLVER_SCRIPT not in command, command


def test_resolver_smoke_runs_after_the_offline_wheel_proof() -> None:
    """The deterministic offline `--no-deps` artifact install keeps its own place in
    the compatibility suite; the resolving smoke is a separate, later step. Pinned
    so the two are never collapsed into one and neither replaces the other."""
    order = _step_names(_steps())
    assert order.index("Run canonical migration and compatibility tests") < order.index(
        RESOLVER_SMOKE_STEP
    )
    assert order.index(RESOLVER_SMOKE_STEP) < order.index("Verify Phase 0 baseline")
    # The offline proof is still reached by the compatibility suite step above.
    assert (REPO_ROOT / "tests" / "compatibility" / "test_facade_wheel_install.py").is_file()
    assert (
        REPO_ROOT / "tests" / "compatibility" / "test_root_facade_distribution.py"
    ).is_file()


def test_converted_package_root_is_in_both_lint_scopes() -> None:
    """The root is the last converted file, and it has to be held to the same two
    lint gates as every leaf before it -- pinned as its own fact so a future edit to
    either target list cannot drop it while the aggregate audits still pass."""
    root = "services/omnivia-memory/src/omnivia_memory/__init__.py"
    assert root in REQUIRED_RUFF_TARGETS
    assert root in REQUIRED_MYPY_TARGETS
    assert "tests/typing/root_facade_consumer.py" in REQUIRED_MYPY_TARGETS
    # Nine typed consumer fixtures now, and every one of them is a mypy target.
    fixtures = sorted(
        path.name for path in (REPO_ROOT / "tests" / "typing").glob("*_consumer.py")
    )
    assert len(fixtures) == 9
    for name in fixtures:
        assert f"tests/typing/{name}" in REQUIRED_MYPY_TARGETS, name


# --------------------------------------------------------------------------
# Distribution coverage, discovered from the tree rather than restated.
#
# Every pin above names its targets literally, which is what makes drift in a
# known target fail. It is the *unknown* one these cover: a distribution added
# under `packages/` and then left out of the install step, the broad pytest run,
# or strict mypy would leave every list above internally consistent and still be
# untested and untyped on the gate. The client is the case that motivated them.
# --------------------------------------------------------------------------

# The distribution that must be first-class on this gate alongside the original
# three, named here so its omission is a failure by name and not only by count.
CLIENT_DISTRIBUTION = "omnivia-core-client"

FULL_SUITE_STEP = "Run full repository test suite"


def _local_distributions() -> list[Path]:
    """Every distribution directory under `packages/`, discovered from the tree."""
    directories = sorted(path.parent for path in (REPO_ROOT / "packages").glob("*/pyproject.toml"))
    assert directories, "no distributions found under packages/"
    return directories


def _import_package(distribution: Path) -> str:
    """The single import package under a distribution's `src/`."""
    candidates = sorted(path.name for path in (distribution / "src").iterdir() if path.is_dir())
    assert len(candidates) == 1, f"{distribution.name}: expected one src package, got {candidates}"
    return candidates[0]


def _full_suite_command() -> str:
    commands = _commands(_step(_steps(), FULL_SUITE_STEP))
    assert len(commands) == 1, f"expected a single pytest invocation, got: {commands}"
    return commands[0]


def test_the_client_is_a_first_class_distribution_on_the_gate() -> None:
    """The five facts that make a distribution actually covered here: it exists,
    it is installed, its tests are collected, its sources are strict-typed, and
    its tree is Ruff-clean (via the `packages` target)."""
    assert (REPO_ROOT / "packages" / CLIENT_DISTRIBUTION / "pyproject.toml").is_file()
    assert f"python -m pip install -e packages/{CLIENT_DISTRIBUTION}" in REQUIRED_LOCAL_INSTALLS
    assert f"packages/{CLIENT_DISTRIBUTION}/tests" in _full_suite_command().split()
    assert (
        f"packages/{CLIENT_DISTRIBUTION}/src/omnivia_core_client" in REQUIRED_MYPY_TARGETS
    )
    assert "packages" in REQUIRED_RUFF_TARGETS


def test_every_local_distribution_is_installed_editable() -> None:
    commands = _commands(_step(_steps(), "Install local packages"))
    for distribution in _local_distributions():
        install = f"python -m pip install -e packages/{distribution.name}"
        assert install in commands, f"missing install: {install}"


def test_every_local_distribution_with_tests_is_in_the_broad_pytest_run() -> None:
    """Naming paths disables `testpaths`, so a distribution's tests are collected
    on the gate only if the broad run names them -- `testpaths` covering
    `packages` fixes a bare local run, not this step. The whole `tests` tree must
    be named, not a directory inside it: naming the runtime's `tests/phase2` kept
    every Phase 3 suite beside it off the gate while this file still read as if
    the runtime were covered."""
    targets = _full_suite_command().split()
    for distribution in _local_distributions():
        if not (distribution / "tests").is_dir():
            continue
        tests_tree = f"packages/{distribution.name}/tests"
        assert tests_tree in targets, (
            f"{distribution.name}: the broad pytest run must name {tests_tree}, not a "
            f"subdirectory of it; found {targets}"
        )


def test_every_local_distribution_source_tree_is_strict_typed() -> None:
    for distribution in _local_distributions():
        target = f"packages/{distribution.name}/src/{_import_package(distribution)}"
        assert target in REQUIRED_MYPY_TARGETS, f"missing strict-mypy target: {target}"


def test_the_client_compatibility_test_keeps_its_unique_basename() -> None:
    """The client's compatibility suite must stay `test_ovc1_compatibility.py`.

    The broad run above collects `tests` and `packages/omnivia-core-client/tests`
    in one invocation, and neither tree is an import package. Under pytest's
    default import mode a test module is imported under its bare basename, so a
    second `test_compatibility.py` collides with `tests/contracts/test_compatibility.py`
    and aborts collection for the whole run.
    """
    client_tests = REPO_ROOT / "packages" / CLIENT_DISTRIBUTION / "tests"
    assert (client_tests / "test_ovc1_compatibility.py").is_file()
    assert not (client_tests / "test_compatibility.py").exists(), (
        "the client compatibility suite must not reuse the "
        "tests/contracts/test_compatibility.py basename"
    )
    assert (REPO_ROOT / "tests" / "contracts" / "test_compatibility.py").is_file()


# --------------------------------------------------------------------------
# Ruff version coupling.
#
# `[tool.ruff] required-version` in the root pyproject aborts Ruff outright when
# the running version falls outside it: exit 2, before a single file is linted.
# That is safe only while the Ruff the gate installs is bounded to the same
# range, and nothing in the workflow mentions `required-version` -- the `Run Ruff`
# step just calls `python -m ruff`, and the version it gets was decided several
# steps earlier by a dependency of a different distribution.
#
# So the coupling is derived here rather than written down as a convention. The
# defect these exist for was real and shipped for one revision: the bound was
# declared in the root `[dependency-groups]`, which is PEP 735 and which pip
# cannot install, so CI's only Ruff came from an unbounded `ruff>=0.4.0` and the
# first release outside the required range would have aborted `Run Ruff` on every
# pull request.
# --------------------------------------------------------------------------

# Where a requirement's name ends and its version specifier begins.
_SPECIFIER_START = "<>=!~"

# Directory names never searched for a pyproject: virtual environments and
# dotted tool directories (all skipped by the leading-dot rule), plus installed
# or built trees that carry copies of this repository's own metadata.
_UNSEARCHED = {"node_modules", "build", "dist"}


def _repository_pyprojects() -> list[Path]:
    """Every `pyproject.toml` this repository owns, discovered from the tree."""
    found = [
        path
        for path in sorted(REPO_ROOT.glob("**/pyproject.toml"))
        if not any(
            part.startswith(".") or part in _UNSEARCHED
            for part in path.relative_to(REPO_ROOT).parts
        )
    ]
    assert found, "no pyproject.toml files found"
    return found


def _required_ruff_version() -> str:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = document["tool"]["ruff"]["required-version"]
    assert isinstance(required, str)
    return required


def _declarations(distribution: str) -> list[tuple[Path, str, str]]:
    """Every declared requirement on `distribution`, as
    (pyproject, how-pip-reaches-it, specifier).

    The middle element is the extra name pip would have to be given to install
    the requirement, or `""` for a PEP 735 dependency group -- which pip cannot
    install by any invocation, and which is therefore never reachable from CI.
    """
    declarations: list[tuple[Path, str, str]] = []
    for pyproject in _repository_pyprojects():
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        tables: list[tuple[str, object]] = [
            (name, table)
            for name, table in document.get("project", {}).get("optional-dependencies", {}).items()
        ]
        tables += [("", table) for table in document.get("dependency-groups", {}).values()]
        for extra, table in tables:
            assert isinstance(table, list)
            for requirement in table:
                assert isinstance(requirement, str)
                index = min(
                    (
                        requirement.index(character)
                        for character in _SPECIFIER_START
                        if character in requirement
                    ),
                    default=len(requirement),
                )
                if requirement[:index].strip().lower() != distribution:
                    continue
                declarations.append((pyproject, extra, requirement[index:].strip()))
    return declarations


def _ruff_declarations() -> list[tuple[Path, str, str]]:
    return _declarations("ruff")


def test_the_mypy_requirement_is_an_exact_pin() -> None:
    """Every declared `mypy` requirement is an exact `==` pin, and they agree.

    `Run strict mypy` is merge-blocking, and a range let it upgrade ambiently:
    the same commit got a different verdict depending on when the environment was
    resolved, and `main` would have begun failing on its own the first time a new
    mypy release added a check. An exact pin is what makes "repeated clean runs
    return the same result for the same commit" a property of the repository
    rather than of the day.

    This is the check that fails if the pin is relaxed back to a range, so the
    determinism claim is guarded rather than merely asserted in a comment.
    """
    declarations = _declarations("mypy")
    assert declarations, (
        "no `mypy` requirement is declared anywhere in the tree, so nothing bounds "
        "the version the merge-blocking strict-mypy gate installs"
    )
    for pyproject, _, specifier in declarations:
        assert specifier.startswith("=="), (
            f"{pyproject.relative_to(REPO_ROOT)}: declares mypy{specifier}. The strict-mypy "
            f"gate is merge-blocking, so its analyser must be pinned exactly (`==`) and "
            f"advanced only by a reviewed dependency change."
        )
    pinned = {specifier for _, _, specifier in declarations}
    assert len(pinned) == 1, f"declared mypy pins disagree: {sorted(pinned)}"


def test_the_mypy_pin_is_reachable_by_the_installs_the_gate_runs() -> None:
    """The pin is installed by the gate rather than merely declared somewhere.

    Same failure mode `test_the_ruff_bound_is_reachable_by_the_installs_the_gate_runs`
    covers: a pin in a PEP 735 group, or in an extra the workflow has stopped
    installing, leaves the gate running whatever pip resolves.
    """
    commands = _commands(_step(_steps(), "Install local packages"))
    reachable = [
        f"{pyproject.parent.relative_to(REPO_ROOT).as_posix()}[{extra}]"
        for pyproject, extra, _ in _declarations("mypy")
        if extra
    ]
    assert any(target in command for target in reachable for command in commands), (
        "the acceptance gate installs no distribution whose extra carries the pinned "
        f"`mypy` requirement, so the mypy it runs is whatever pip resolves. Declared "
        f"mypy requirements: {_declarations('mypy')}; install commands: {commands}"
    )


def test_the_ruff_requirement_is_an_exact_pin() -> None:
    """Every declared `ruff` requirement is an exact `==` pin, and they agree.

    `Run Ruff` is merge-blocking, and a range let it upgrade ambiently: the last
    green canonical run resolved 0.16.1 while a fresh local environment under
    `>=0.16.1,<0.17` resolved 0.16.2, so the same commit was linted by different
    Ruffs depending on when the environment was built. An exact pin is what
    makes "repeated runs of the same commit produce the same Ruff result" a
    property of the repository rather than of the day (R006-04).

    This is the check that fails if the pin is relaxed back to a range, so the
    determinism claim is guarded rather than merely asserted in a comment. Same
    guard `test_the_mypy_requirement_is_an_exact_pin` gives the strict-mypy gate.
    """
    declarations = _ruff_declarations()
    assert declarations, (
        "no `ruff` requirement is declared anywhere in the tree, so nothing bounds "
        "the version the merge-blocking Ruff gate installs"
    )
    for pyproject, _, specifier in declarations:
        assert specifier.startswith("=="), (
            f"{pyproject.relative_to(REPO_ROOT)}: declares ruff{specifier}. The Ruff "
            f"gate is merge-blocking, so its linter must be pinned exactly (`==`) and "
            f"advanced only by a reviewed dependency change."
        )
    pinned = {specifier for _, _, specifier in declarations}
    assert len(pinned) == 1, f"declared ruff pins disagree: {sorted(pinned)}"


def test_every_declared_ruff_version_matches_the_required_version() -> None:
    """Every `ruff` requirement in the tree is string-identical to
    `[tool.ruff] required-version`.

    String equality rather than specifier arithmetic: comparing PEP 440 ranges
    properly needs `packaging`, which this repository does not declare, and the
    invariant wanted here is stricter anyway. One declared range, restated
    verbatim wherever it appears, is the only shape in which "these move
    together" is checkable at all.
    """
    required = _required_ruff_version()
    declarations = _ruff_declarations()
    assert declarations, (
        "no `ruff` requirement is declared anywhere in the tree, so nothing bounds "
        f"the version the gate installs against required-version {required!r}"
    )
    for pyproject, _, specifier in declarations:
        assert specifier == required, (
            f"{pyproject.relative_to(REPO_ROOT)}: declares ruff{specifier}, but the root "
            f"[tool.ruff] required-version is {required!r}. Ruff aborts (exit 2, nothing "
            f"linted) on a version outside required-version, so every declared bound must "
            f"restate it verbatim."
        )


def test_the_ruff_bound_is_reachable_by_the_installs_the_gate_runs() -> None:
    """At least one bounded `ruff` requirement is actually installed by the gate.

    Matching ranges are worth nothing if pip never sees them. This walks the real
    path: the `Install local packages` step's commands, the extra each one asks
    for, and whether any of those extras is where a `ruff` bound is declared.

    It fails on both halves of the original defect -- a bound declared only in a
    PEP 735 group (pip cannot install those, so `extra` is empty and no command
    can match), and a bound declared in a real extra that the workflow has
    stopped installing.
    """
    commands = _commands(_step(_steps(), "Install local packages"))
    reachable = []
    for pyproject, extra, specifier in _ruff_declarations():
        if not extra:
            continue
        target = f"{pyproject.parent.relative_to(REPO_ROOT).as_posix()}[{extra}]"
        if any(target in command for command in commands):
            reachable.append((target, specifier))

    assert reachable, (
        "the acceptance gate installs no distribution whose extra carries a bounded "
        "`ruff` requirement, so the Ruff it runs is whatever pip resolves. Declared "
        f"ruff requirements: {_ruff_declarations()}; install commands: {commands}"
    )


# --------------------------------------------------------------------------
# The hosted TLS conformance lane (V06-4).
#
# `conformance/tls` dials a real provisioned host over TLS at a public IPv4
# literal. Nothing else in this repository runs it, and nothing else may: the
# guards below are what keep the tree from becoming either a suite no workflow
# collects or a suite that skips itself when its environment is absent. Both
# failure modes were hit in this repository, which is why the checks parse
# sources rather than trust conventions.
#
# The count is pinned here rather than in the suite. A test cannot assert how
# many tests there are, and the workflow's JUnit proof needs a number that a
# reviewer can see is the number the tree actually declares.
# --------------------------------------------------------------------------

#: The hosted suite's exact size. The workflow proves this many *passed* with no
#: other outcome; this proves the tree declares exactly this many cases, so the
#: two numbers cannot drift apart without one of them failing.
HOSTED_CASE_COUNT = 28

#: What `Run hosted TLS conformance` must run, spelled exactly. `-rs` makes any
#: skip visible in the log rather than folded into a dot, and the JUnit report is
#: what the proof step reads instead of the exit code. `--junitxml=` is attached
#: rather than detached because `scripts/check-import-install-alignment.py` reads
#: every non-flag token after `pytest` as a collection root.
HOSTED_PYTEST_COMMAND = (
    "python -m pytest conformance/tls -q -rs "
    '--junitxml="${RUNNER_TEMP}/tls-conformance-evidence/junit.xml"'
)

#: Every configuration value the hosted workflow supplies, and where from. The
#: bearer is the one secret; everything else is a repository/environment
#: variable, because none of it is confidential and all of it should be readable
#: in the run's configuration.
HOSTED_ENVIRONMENT = {
    "OMNIVIA_TLS_CONFORMANCE_HOST": "vars",
    "OMNIVIA_TLS_CONFORMANCE_PORT": "vars",
    "OMNIVIA_TLS_CONFORMANCE_OPERATION": "vars",
    "OMNIVIA_TLS_CONFORMANCE_PURPOSE": "vars",
    "OMNIVIA_TLS_CONFORMANCE_CHAIN_SHA256": "vars",
    "OMNIVIA_TLS_CONFORMANCE_BEARER": "secrets",
}

#: The dispatch input, spelled as the workflow expression. It may appear as a
#: `with:` or `env:` *value* and nowhere else: an expression substituted into a
#: `run:` script is expanded before the shell parses the line, so a dispatched
#: value carrying a quote, a `$(...)` or a newline would be script rather than
#: data -- and the only thing that validates this input is that same script.
CANDIDATE_INPUT = "${{ inputs.candidate_ref }}"

#: The one step `env:` name the input is allowed to arrive through, and the steps
#: that must supply it that way.
CANDIDATE_ENV = "CANDIDATE"
CANDIDATE_ENV_STEPS = ("Pin the candidate under test", "Prepare the evidence directory")

#: The artifact action this repository standardises on; `core-performance-report.yml`
#: already uses it.
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@v7"

#: Modules and names that mean "the standard service", not an approved embedder.
#: `host.py` reaching any of them would make a V06-4 GO readable as production
#: HTTP serving, which is exactly the boundary the lane exists inside.
PRODUCTION_SERVING_MODULES = (
    "omnivia_core_runtime.service.main",
    "omnivia_core_runtime.service.dispatch",
    "omnivia_core_runtime.service.runner",
    "omnivia_core_runtime.service.bootstrap",
    "omnivia_core_runtime.service.managed_start",
)
PRODUCTION_SERVING_NAMES = (
    "build_service_registry",
    "OperationRegistry",
    "Dispatcher",
)


def _code(module: Path) -> str:
    """`module`'s source with every docstring and comment removed.

    Every assertion below that searches text searches *this*, for the same reason
    the workflow helpers drop commented-out lines: a substring scan over raw
    source passes on a docstring that explains why a construct is absent, and
    then keeps passing after the construct itself comes back. `ast.unparse`
    renders only what executes.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _declared_cases(module: Path) -> dict[str, int]:
    """Each `test_*` in `module`, mapped to the number of cases it declares.

    A parametrized test declares as many cases as its `argvalues` list holds, and
    stacked parametrize decorators multiply. Counted from the tree rather than by
    running pytest, so the number is a property of the committed source and can
    be checked on a runner that has no conformance host.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    declared: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        cases = 1
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not isinstance(target, ast.Attribute) or target.attr != "parametrize":
                continue
            argvalues = decorator.args[1]
            assert isinstance(argvalues, ast.List), (
                f"{node.name}: parametrize argvalues must be a list literal so the "
                "declared case count is readable without executing the module"
            )
            cases *= len(argvalues.elts)
        declared[node.name] = cases
    return declared


def _hosted_steps() -> list[Line]:
    return _steps_of(TLS_CONFORMANCE_WORKFLOW, "core-tls-conformance")


def _hosted_text() -> str:
    return TLS_CONFORMANCE_WORKFLOW.read_text(encoding="utf-8")


def test_the_tls_conformance_workflow_exists_and_runs_the_conformance_tree() -> None:
    """The suite is collected by no other workflow, so without this file nothing
    runs it, and the tree would be committed code that never executes."""
    assert TLS_CONFORMANCE_WORKFLOW.is_file()
    commands = _commands(_step(_hosted_steps(), "Run hosted TLS conformance"))
    assert len(commands) == 1, f"expected a single pytest invocation, got: {commands}"
    assert commands[0] == HOSTED_PYTEST_COMMAND, (
        f"the hosted suite's invocation drifted: {commands[0]!r}"
    )


def test_the_tls_conformance_workflow_is_manual_only() -> None:
    """The reason this is a separate workflow at all.

    The conformance host is disposable. A `pull_request` trigger here would make
    every pull request in the repository depend on it being up -- that is not a
    gate, it is an outage -- and it could then be made a required check by a
    branch rule outside this tree.
    """
    triggers = _keys(_block(_significant(_hosted_text()), "on"))
    assert triggers == ["workflow_dispatch"], (
        f"the hosted TLS conformance workflow must be dispatch-only, found: {triggers}"
    )


def test_the_tls_conformance_workflow_has_safe_concurrency() -> None:
    """Two operators dispatching the same candidate, or one operator re-dispatching
    before the first run lands, both stack against the same disposable host. This
    pins the workflow-level `concurrency:` to the exact safe shape rather than
    leaving the lane to queue or collide silently."""
    concurrency = _block(_significant(_hosted_text()), "concurrency")
    assert _entry(concurrency, "group") == (
        "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
    )
    assert _entry(concurrency, "cancel-in-progress") == "true"


def test_the_hosted_run_is_pinned_to_the_exact_candidate() -> None:
    """The dispatch names a commit, and the checkout is checked against it.

    `candidate_identity_match` compares what the host attests with what the
    runner holds. That is only a statement about a known object if the runner's
    own checkout is a known object: a branch name would resolve to whatever it
    points at between dispatch and checkout, and the equality would then hold
    between two moving targets.
    """
    workflow = _significant(_hosted_text())
    inputs = _keys(_block(_block(_block(workflow, "on"), "workflow_dispatch"), "inputs"))
    assert inputs == ["candidate_ref"], f"unexpected dispatch inputs: {inputs}"

    checkout = _step(_hosted_steps(), "Check out the exact candidate")
    assert _entry(_block(checkout, "with"), "ref") == "${{ inputs.candidate_ref }}"

    pinned = " ".join(_commands(_step(_hosted_steps(), "Pin the candidate under test")))
    assert "git rev-parse HEAD" in pinned, "the checkout is never resolved"
    assert "-ne 40" in pinned, "a branch or short SHA would be accepted as a candidate"
    assert "*[!0-9a-f]*" in pinned, (
        "the candidate is no longer required to be lowercase hex, so a ref that is "
        "40 characters of anything would pass the length check"
    )
    assert '"${resolved}" != "${CANDIDATE}"' in pinned, (
        "the resolved checkout is no longer compared against the named candidate"
    )


def test_the_dispatch_input_never_reaches_a_shell_script() -> None:
    """The dispatch input is data, not script text.

    `${{ }}` is substituted into a `run:` block before the shell parses the line,
    so an input carrying a quote, a `$(...)` or a newline is executed rather than
    read -- and the only thing that constrains this input to 40 hex characters is
    a shell script that would already have been rewritten by the time it ran.
    Passing it through a step `env:` value makes it a variable the shell reads,
    which no expansion can turn into syntax.

    Checked in both directions: the expression appears in no command and on no
    line but the two that pass it as a value, and the two steps that need it are
    supplied it -- and read it, quoted -- through the one environment name.
    """
    steps = _hosted_steps()
    interpolated = []
    carriers: dict[str, str] = {}
    for name in _step_names(steps):
        step = _step(steps, name)
        if _locate(step, "run") is not None and any(
            CANDIDATE_INPUT in command for command in _commands(step)
        ):
            interpolated.append(name)
        if _locate(step, "env") is None:
            continue
        environment = _block(step, "env")
        for key in _keys(environment):
            if _entry(environment, key) == CANDIDATE_INPUT:
                carriers[name] = key
    assert not interpolated, (
        f"these steps interpolate the dispatch input into a shell script: "
        f"{interpolated}"
    )
    # Exactly these steps, through exactly this name. A third carrier would be a
    # second place the value has to be handled correctly, and the point of one
    # name is that there is one place to read.
    assert carriers == {name: CANDIDATE_ENV for name in CANDIDATE_ENV_STEPS}, (
        f"the dispatch input has unexpected environment carriers: {carriers}"
    )

    # `_significant` drops comment lines before `_commands` sees them, and a
    # commented-out interpolation is still an interpolation -- the expansion
    # happens first, and a value carrying a newline ends the comment. So the raw
    # file is scanned as well, and every line holding the expression has to be one
    # of the two structural forms that pass it as a value.
    permitted = {f"ref: {CANDIDATE_INPUT}", f"{CANDIDATE_ENV}: {CANDIDATE_INPUT}"}
    carrying = [
        line.strip() for line in _hosted_text().splitlines() if CANDIDATE_INPUT in line
    ]
    assert carrying, "the workflow no longer consumes the dispatch input at all"
    assert set(carrying) <= permitted, (
        "the dispatch input reaches somewhere other than a `ref:` or an `env:` "
        f"value: {sorted(set(carrying) - permitted)}"
    )

    reference = f"${{{CANDIDATE_ENV}}}"
    for name in CANDIDATE_ENV_STEPS:
        commands = _commands(_step(steps, name))
        # Every read is inside double quotes. An unquoted `${CANDIDATE}` is word-
        # split and glob-expanded by the shell, which is a smaller hole than
        # interpolation but the same kind: the value deciding how the line parses.
        read_at = [
            (command, index)
            for command in commands
            for index in range(len(command))
            if command.startswith(reference, index)
        ]
        assert read_at, (
            f"step {name!r} is supplied {CANDIDATE_ENV} and never reads it, so the "
            "value it acts on comes from somewhere else"
        )
        assert all(command[:index].count('"') % 2 == 1 for command, index in read_at), (
            f"step {name!r} reads {CANDIDATE_ENV} outside double quotes: {commands}"
        )


def test_the_hosted_workflow_supplies_exactly_one_secret_and_no_key_material() -> None:
    """No private key, no PKCS#12 bundle, and no CA bundle derived from the chain.

    An anchor built from the certificate under test would make the client's
    verdict circular, and a key on the runner would put the listener's identity
    somewhere it is not needed. The bearer is the only secret this lane has, so
    "exactly one" is a countable claim rather than a hope.
    """
    text = _hosted_text()
    referenced = {
        fragment.split("}}")[0].strip()
        for fragment in text.split("${{ secrets.")[1:]
    }
    assert referenced == {"OMNIVIA_TLS_CONFORMANCE_BEARER"}, (
        f"the hosted workflow consumes secrets beyond the bearer: {referenced}"
    )
    for forbidden in ("PRIVATE_KEY", "CA_BUNDLE", "cafile", ".p12", ".pfx", ".pem"):
        assert forbidden not in text, (
            f"the hosted workflow references {forbidden!r}; no private key and no "
            "listener-derived CA bundle may reach CI"
        )


def test_the_hosted_workflow_supplies_the_whole_environment_from_the_right_source() -> None:
    """Each value comes from `vars.` or `secrets.` exactly as it should.

    A confidential value in `vars.` publishes it; a public value in `secrets.`
    hides configuration a reviewer needs to read. Both are asserted here rather
    than left to whoever edits the workflow next.
    """
    step = _step(_hosted_steps(), "Run hosted TLS conformance")
    supplied = _block(step, "env")
    for variable, source in HOSTED_ENVIRONMENT.items():
        value = _entry(supplied, variable)
        assert value == f"${{{{ {source}.{variable} }}}}", (
            f"{variable} must come from `{source}.`, found: {value!r}"
        )
    assert _entry(_hosted_steps(), "environment") is None
    assert _entry(_job_of(TLS_CONFORMANCE_WORKFLOW, "core-tls-conformance"),
                  "environment") == "tls-conformance"


def test_the_hosted_lane_is_domain_free_and_reaches_a_public_ipv4() -> None:
    """No DNS name is issued for, resolved or verified anywhere in this lane.

    The certificate carries an `iPAddress` SAN, so a hostname in the host
    variable would be checked against a SAN that does not exist and the run would
    fail for a reason that says nothing about Core. The suite narrows the
    variable to a public IPv4 and asserts the presented SAN sets in both
    directions -- exactly the configured address, and no DNS SAN at all.
    """
    suite = _code(TLS_SUITE)
    assert "ipaddress.IPv4Address" in suite, (
        "the suite no longer narrows the conformance host to an IPv4 literal"
    )
    assert "not parsed.is_global or parsed.is_multicast" in suite, (
        "the suite must require a *usable public* IPv4 rather than merely one that "
        "is not private and not loopback: the weaker form admits 240.0.0.0/4, "
        "100.64.0.0/10, the unspecified address and every multicast group"
    )
    assert "1 <= port <= 65535" in suite, (
        "the suite no longer bounds the conformance port to the range a TCP port "
        "occupies, so `0` or a five-digit overflow would reach the socket"
    )
    assert "dns_san'] == []" in suite or 'dns_san"] == []' in suite, (
        "the suite no longer asserts that the presented chain carries no DNS SAN"
    )
    assert "ip_address_san'] == [address]" in suite or (
        'ip_address_san"] == [address]' in suite
    ), "the suite no longer asserts the exact iPAddress SAN"
    assert "verify_code == 64" in suite, (
        "the wrong-peer refusal must be OpenSSL's 64 (IP address mismatch); 62 is "
        "the hostname mismatch a DNS-named lane would see, and asserting it here "
        "would pass only if the address checking under test never ran"
    )


def test_the_hosted_launcher_is_an_embedder_and_not_the_standard_service() -> None:
    """`host.py` may construct the adapter. It may not become the service.

    A V06-4 GO is adapter conformance, not production HTTP serving. The console
    entry point supplies no credential resolver and exits 2 on every bind, and a
    launcher that reached around that -- by importing it, by standing up
    `Dispatcher`, or by building the accepted service registry -- would make the
    GO readable as something it is not.
    """
    assert TLS_HOST_LAUNCHER.is_file()
    tree = ast.parse(TLS_HOST_LAUNCHER.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = sorted(imported & set(PRODUCTION_SERVING_MODULES))
    assert not forbidden, f"the launcher imports the standard service: {forbidden}"

    code = _code(TLS_HOST_LAUNCHER)
    for name in PRODUCTION_SERVING_NAMES:
        assert name not in code, f"the launcher references {name!r}"

    # And it does construct the adapter directly, which is the other half: a
    # launcher that stopped doing so would pass every prohibition above while
    # proving nothing about `HttpListener`.
    assert "HttpListener(" in code and "HttpTls(" in code


def test_the_hosted_launcher_refuses_everything_it_is_required_to() -> None:
    """The refusals that have to happen before a socket exists.

    Each one is a state in which a publicly reachable listener must not come up:
    an address that is loopback or not a literal, TLS material that cannot be
    read, a bearer file anyone else can read, a workspace nobody marked
    synthetic, or a checkout with no git identity to attest. The sixth -- a
    checkout that is not clean -- is proved behaviourally by
    `test_the_hosted_launcher_refuses_to_serve_from_a_dirty_checkout`, because a
    substring for it would pass on the paragraph describing it.
    """
    code = _code(TLS_HOST_LAUNCHER)
    for required in (
        "is_loopback",
        "ipaddress.ip_address",
        "BEARER_FILE_MODE",
        "SYNTHETIC_MARKER",
        "MINIMUM_BEARER_LENGTH",
        "rev-parse",
    ):
        assert required in code, f"the launcher no longer checks {required}"

    # The identity is read before the bind, not after: a host that cannot say
    # what it is must never serve, and the order of these two lines is the whole
    # of that guarantee. The private key is read before it too -- it is read to
    # prove the launch record does not carry it, and a read that failed after the
    # bind would be a refusal with a public port already open.
    assert code.index("_identity()") < code.index("listener.start()")
    assert code.index("_text(key") < code.index("listener.start()"), (
        "the launcher reads the private key after binding"
    )

    # And what cannot be done before the bind is undone on failure. The record
    # reports the URL the socket actually bound, so the bind and the write are
    # one unit: a `start()` that raised, a record refused for carrying a withheld
    # value, and a write that failed must all leave nothing listening. Read off
    # `launch` itself rather than the module, so `main`'s own `stop()` in its
    # `finally` cannot satisfy this by accident.
    launching = ast.unparse(
        next(
            node
            for node in ast.parse(TLS_HOST_LAUNCHER.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef) and node.name == "launch"
        )
    )
    assert "except BaseException:" in launching and "listener.stop()" in launching, (
        "a failed bind or a failed launch-record write must retire the listener; "
        "without it a refusal can leave a publicly reachable port open"
    )


def _host_launcher() -> ModuleType:
    """`conformance/tls/host.py`, loaded by path.

    `conformance/` is outside every collection root and every package, so there
    is no import path to it. Loaded rather than read as source because the test
    below is about what `_identity` *does* against a real repository -- a
    substring scan for `--porcelain` cannot tell the check apart from a docstring
    that explains it, which is exactly the false confidence that let a
    committed-tree SHA be described as a working-state proof.
    """
    spec = importlib.util.spec_from_file_location(
        "conformance_tls_host", TLS_HOST_LAUNCHER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"git {' '.join(arguments)} failed in the fixture repository: "
        f"{completed.stderr.strip()}"
    )


def _one_commit_repository(root: Path) -> None:
    """A repository holding one committed file and nothing else."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "conformance@example.invalid")
    _git(root, "config", "user.name", "conformance")
    (root / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "candidate")


def test_the_hosted_launcher_refuses_to_serve_from_a_dirty_checkout(
    tmp_path: Path,
) -> None:
    """The attested pair is committed objects, so cleanliness is enforced separately.

    `tree_sha` is `HEAD^{tree}` -- the tree of the last commit. It is identical on
    a pristine checkout and on one somebody edited, so the runner's equality check
    proves the host is at the candidate *commit* and, on its own, nothing about
    the bytes being served. What closes that is a precondition: `_identity`
    refuses to return unless git reports the checkout clean, and it is called
    before the socket exists, so a host with local changes never binds at all.

    Exercised against real repositories rather than asserted from the source, and
    across all three ways a checkout is dirty. A staged change is invisible to a
    plain `git diff`, and an added file is invisible to both that and
    `git status --porcelain` without `--untracked-files=all` -- so a check that
    covered only the obvious one would pass a text scan and still let an edited
    host attest the candidate.
    """
    host = _host_launcher()

    clean = tmp_path / "clean"
    _one_commit_repository(clean)
    identity = host._identity(clean)
    assert set(identity) == {"commit_sha", "tree_sha"}, (
        f"the launcher no longer attests exactly the two objects: {sorted(identity)}"
    )

    planted = "planted-name-that-must-not-be-republished.txt"
    dirt = {
        "an untracked file": lambda root: (root / planted).write_text(
            "x\n", encoding="utf-8"
        ),
        "a staged addition": lambda root: (
            (root / planted).write_text("x\n", encoding="utf-8"),
            _git(root, "add", planted),
        ),
        "an unstaged edit": lambda root: (root / "tracked.txt").write_text(
            "edited\n", encoding="utf-8"
        ),
    }
    for label, plant in dirt.items():
        repository = tmp_path / label.replace(" ", "-")
        _one_commit_repository(repository)
        plant(repository)
        with pytest.raises(host.LaunchRefused) as refused:
            host._identity(repository)
        # And the refusal says what is wrong without saying where. This process
        # is about to be reachable from the public internet, and `git status`
        # names paths.
        message = str(refused.value)
        assert planted not in message and "tracked.txt" not in message, (
            f"the refusal for {label} republishes a path from the working tree"
        )
        assert str(repository) not in message

    # The check is only worth anything before the bind. Read structurally off
    # `launch`'s own statements rather than by substring position in the module:
    # `identity = _identity()` has to be a statement that completes before the
    # one holding `listener.start()`, which a moved call fails and a reworded
    # comment cannot satisfy.
    launching = next(
        node
        for node in ast.parse(TLS_HOST_LAUNCHER.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "launch"
    )

    identified: list[int] = []
    started: list[int] = []
    for index, statement in enumerate(launching.body):
        for call in ast.walk(statement):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name) and call.func.id == "_identity":
                identified.append(index)
            if isinstance(call.func, ast.Attribute) and call.func.attr == "start":
                started.append(index)
    assert identified and started, (
        "`launch` no longer both reads the identity and starts the listener"
    )
    assert max(identified) < min(started), (
        "`launch` binds before it establishes the checkout's identity, so a dirty "
        "or unidentifiable host would already be listening when it is refused"
    )


def test_both_attestation_sides_require_a_clean_checkout() -> None:
    """The attested tree is committed state, so cleanliness is a precondition.

    Parsed from the two functions that produce the compared identities. A module
    docstring mentioning ``git status`` cannot satisfy this, and neither can a
    call after the identity has already been returned.
    """
    host_tree = ast.parse(TLS_HOST_LAUNCHER.read_text(encoding="utf-8"))
    identity = next(
        node
        for node in host_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_identity"
    )
    host_status = next(
        node
        for node in identity.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "status" for target in node.targets)
    )
    assert isinstance(host_status.value, ast.Call)
    host_call = host_status.value
    assert isinstance(host_call.func, ast.Name) and host_call.func.id == "_git"
    assert len(host_call.args) == 4
    assert isinstance(host_call.args[0], ast.Name) and host_call.args[0].id == "checkout"
    assert [
        argument.value for argument in host_call.args[1:] if isinstance(argument, ast.Constant)
    ] == ["status", "--porcelain", "--untracked-files=all"]
    host_guards = [node for node in identity.body if isinstance(node, ast.If)]
    assert any(
        "status.strip()" in ast.unparse(guard.test)
        and any(isinstance(statement, ast.Raise) for statement in guard.body)
        for guard in host_guards
    ), "the host reads checkout status but does not refuse a dirty result"
    host_return = next(node for node in identity.body if isinstance(node, ast.Return))
    assert identity.body.index(host_status) < identity.body.index(host_return)
    assert all(identity.body.index(guard) < identity.body.index(host_return) for guard in host_guards)

    suite_tree = ast.parse(TLS_SUITE.read_text(encoding="utf-8"))
    candidate = next(
        node
        for node in suite_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_candidate"
    )
    runner_status = next(
        node
        for node in candidate.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "status" for target in node.targets)
    )
    assert isinstance(runner_status.value, ast.Call)
    runner_call = runner_status.value
    assert isinstance(runner_call.func, ast.Name) and runner_call.func.id == "_git"
    assert len(runner_call.args) == 3
    assert [
        argument.value for argument in runner_call.args if isinstance(argument, ast.Constant)
    ] == ["status", "--porcelain", "--untracked-files=all"]
    runner_guard = next(node for node in candidate.body if isinstance(node, ast.If))
    assert "status.strip()" in ast.unparse(runner_guard.test)
    assert any(isinstance(statement, ast.Raise) for statement in runner_guard.body), (
        "the runner reads checkout status but does not refuse a dirty result"
    )
    runner_return = next(node for node in candidate.body if isinstance(node, ast.Return))
    assert candidate.body.index(runner_status) < candidate.body.index(runner_guard)
    assert candidate.body.index(runner_guard) < candidate.body.index(runner_return)


def _collection_checker() -> ModuleType:
    """`scripts/check-test-collection.py`, loaded by path.

    Its filename is not a valid Python identifier, so it cannot be imported
    normally -- the same shape `tests/test_test_collection.py` uses. Loaded
    rather than grepped so the assertion below reads the constant the checker
    actually walks with, not a line of source that may no longer be used.
    """
    spec = importlib.util.spec_from_file_location("check_test_collection", COLLECTION_CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_conformance_tree_with_tests_is_named_by_that_workflow(tmp_path: Path) -> None:
    """The half that makes living outside `testpaths` safe.

    `conformance/` is outside `testpaths` and outside every other workflow's
    paths on purpose, and `scripts/check-test-collection.py` -- which otherwise
    requires every test file on disk to be reachable from a bare `pytest` run --
    exempts it for that reason and no other. The exemption and this workflow are
    two halves of one claim, so they are asserted together: what a bare run
    stops accounting for has to be exactly what this workflow runs. That is what
    keeps "outside every collection root" from meaning "run by nothing" -- a
    second tree added beside `conformance/tls` and named by no invocation fails
    right here.

    The exemption is also asserted to be root-bounded. Set equality alone would
    accept a rule keyed on the directory's *name*, which would silently excuse a
    `conformance` directory anywhere in the repository; that is a broadening with
    the same constant, so it is checked as behaviour against a temporary root.
    """
    checker = _collection_checker()
    assert checker.DISPATCH_ONLY_TREES == {CONFORMANCE_TREE.name}, (
        "the bare-run collection checker exempts trees beyond the one this "
        f"workflow runs: {sorted(checker.DISPATCH_ONLY_TREES)}"
    )

    nested = tmp_path / "packages" / "p" / CONFORMANCE_TREE.name
    nested.mkdir(parents=True)
    (nested / "test_elsewhere.py").write_text("", encoding="utf-8")
    # And one at the temporary root's own top level. Without it the walk could be
    # keyed on the *caller's* root rather than on `REPO_ROOT` -- `entry.parent ==
    # root` passes a nested-only plant identically -- and the exemption would then
    # follow any tree it was pointed at instead of naming this repository's.
    top = tmp_path / CONFORMANCE_TREE.name
    top.mkdir()
    (top / "test_top.py").write_text("", encoding="utf-8")
    assert checker.discover_test_files(tmp_path) == {
        f"packages/p/{CONFORMANCE_TREE.name}/test_elsewhere.py",
        f"{CONFORMANCE_TREE.name}/test_top.py",
    }, (
        "the exemption is keyed on the directory name, or on the root it was "
        "handed, rather than on the repository root"
    )

    assert CONFORMANCE_TREE.is_dir(), "the conformance tree is missing"
    command = _commands(_step(_hosted_steps(), "Run hosted TLS conformance"))[0]
    holding_tests = sorted(
        directory.relative_to(REPO_ROOT).as_posix()
        for directory in CONFORMANCE_TREE.rglob("*")
        if directory.is_dir() and any(directory.glob("test_*.py"))
    )
    assert holding_tests, "no test modules under conformance/; this guard means nothing"
    unnamed = [tree for tree in holding_tests if tree not in command]
    assert not unnamed, (
        f"these conformance trees hold tests that no workflow runs: {unnamed}"
    )


def test_the_hosted_suite_declares_exactly_the_cases_the_workflow_proves() -> None:
    """One number, held in two places that must agree.

    The workflow reads the JUnit record and requires exactly this many *passed*
    with no failure, error or skip. That proof is only as good as the claim that
    the tree declares this many cases in the first place -- otherwise a suite
    that lost half its tests and gained a parametrization would still report the
    expected total.
    """
    declared = _declared_cases(TLS_SUITE)
    total = sum(declared.values())
    assert total == HOSTED_CASE_COUNT, (
        f"the hosted suite declares {total} cases, not {HOSTED_CASE_COUNT}: "
        f"{declared}"
    )
    proof = "\n".join(
        content for _, content in _step(_hosted_steps(), "Prove 28 passed and no other outcome")
    )
    assert f"EXPECTED = {HOSTED_CASE_COUNT}" in proof, (
        "the workflow's JUnit proof does not require the declared number of cases"
    )
    for outcome in ("failures", "errors", "skipped"):
        assert outcome in proof, f"the proof step ignores {outcome}"


def test_the_evidence_is_always_uploaded_and_sanitized_before_it_leaves() -> None:
    """The artifact is published on every outcome, and scrubbed first.

    A guard that failed on a leak and then let an unconditional upload publish
    the file anyway would republish exactly what it caught, so the redaction step
    has to come before the upload in workflow order -- which is a property of the
    step list, not of the step's own text.
    """
    steps = _hosted_steps()
    names = _step_names(steps)
    redact = "Redact any credential that reached the evidence"
    upload = "Upload conformance evidence"
    assert names.index(redact) < names.index(upload), (
        "the evidence is uploaded before it is scrubbed"
    )
    for name in (redact, upload, "Prove 28 passed and no other outcome"):
        assert _entry(_step(steps, name), "if") == "always()", (
            f"step {name!r} does not run on every outcome"
        )
    published = _block(_step(steps, upload), "with")
    assert _entry(published, "name") == "core-tls-conformance-evidence"
    assert _entry(published, "if-no-files-found") == "error"
    # The version this repository already uses in `core-performance-report.yml`.
    # Pinned rather than left to whatever the action's default major is, so this
    # lane cannot sit a major behind the rest of the tree and pick up a different
    # artifact format or retention behaviour than every other uploaded evidence
    # artifact here.
    assert _entry(_step(steps, upload), "uses") == UPLOAD_ARTIFACT_ACTION


def test_the_hosted_suite_cannot_skip() -> None:
    """Zero skipped tests, held mechanically.

    An unavailable host or skipped run retains NO-GO. A skipped test reads
    exactly like a passing one in a summary line, so the constructs are forbidden
    outright rather than the count trusted. This reads the source because a
    convention nothing checks is a convention that lasts until the first
    inconvenient CI run.

    Parsed rather than grepped. A substring scan flags the suite's own docstrings
    for *explaining* why these constructs are absent, and the obvious fix for
    that -- rewording the prose -- leaves a guard that fires on whichever
    sentence someone writes next. `ast` sees only the constructs.
    """
    offenders: list[str] = []
    for module in sorted(CONFORMANCE_TREE.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        relative = module.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            found: str | None = None
            if isinstance(node, ast.Attribute) and node.attr in SKIP_CONSTRUCTS:
                found = node.attr
            elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
                imported = [
                    alias.name for alias in node.names if alias.name in SKIP_CONSTRUCTS
                ]
                found = ", ".join(imported) or None
            else:
                continue
            if found is not None:
                offenders.append(f"{relative}:{node.lineno}: {found}")
    assert not offenders, (
        "the hosted TLS conformance suite must not be able to skip; missing "
        f"configuration has to fail loudly instead. Found: {offenders}"
    )


def test_the_skip_guard_detects_each_construct_it_forbids() -> None:
    """The guard above, replayed against each construct, and against clean source.

    Without this the scan could stop detecting anything -- a typo in a member
    name, an `ast` node type that stopped matching -- and go on reporting a clean
    tree forever, which is the exact failure mode it was written to prevent.
    """
    planted = {
        "marker": "import pytest\n@pytest.mark.skipif(True, reason='')\ndef test_a(): ...\n",
        "call": "import pytest\ndef test_a(): pytest.skip('no host')\n",
        "import_or_skip": "import pytest\ndef test_a(): pytest.importorskip('ssl')\n",
        "from_import": "from pytest import skip\ndef test_a(): skip('no host')\n",
    }
    for label, source in planted.items():
        tree = ast.parse(source)
        detected = any(
            (isinstance(node, ast.Attribute) and node.attr in SKIP_CONSTRUCTS)
            or (
                isinstance(node, ast.ImportFrom)
                and node.module == "pytest"
                and any(alias.name in SKIP_CONSTRUCTS for alias in node.names)
            )
            for node in ast.walk(tree)
        )
        assert detected, f"the skip guard no longer detects the {label} form"

    clean = ast.parse(
        '"""A docstring that mentions skipif, importorskip and pytest.skip."""\n'
        "import pytest\ndef test_a(): assert pytest is not None\n"
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in SKIP_CONSTRUCTS
        for node in ast.walk(clean)
    ), "the skip guard fires on prose, which is what parsing was meant to fix"


def test_the_hosted_workflow_is_not_the_acceptance_gate_and_does_not_widen_phase2() -> None:
    """Two standing prohibitions, asserted rather than remembered.

    The hosted suite must not reach the required gate, and `phase2-platform.yml`
    must not be widened by this lane under any circumstances.
    """
    acceptance = _commands(_step(_steps(), "Run full repository test suite"))
    assert not any("conformance" in command for command in acceptance), (
        "the acceptance gate must not collect the hosted conformance suite"
    )
    assert "conformance" not in PHASE2_WORKFLOW.read_text(encoding="utf-8"), (
        "phase2-platform.yml must not be widened by the TLS conformance lane"
    )
