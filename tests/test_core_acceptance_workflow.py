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

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACCEPTANCE_WORKFLOW = WORKFLOW_DIR / "core-acceptance.yml"
PERFORMANCE_WORKFLOW = WORKFLOW_DIR / "core-performance-report.yml"
PHASE2_WORKFLOW = WORKFLOW_DIR / "phase2-platform.yml"

# (step name, exact command) for each single-command gate step, in the order the
# workflow must run them.
GATE_STEPS = (
    ("Check package boundaries", "python scripts/check-package-boundaries.py"),
    ("Run package boundary tests", "python -m pytest tests/test_package_boundaries.py -q"),
    ("Build and install-check all distributions", "PYTHON=python scripts/check-package-builds.sh"),
    ("Check application contracts", "python scripts/check-application-contracts.py"),
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
    # The full suite, pinned exactly so a narrowed scope fails here. It names its
    # paths because everything under `packages/` lives outside `testpaths`
    # (`services` and `tests`), so a bare `pytest -q` reported a green
    # full-repository run while collecting none of it. Each distribution's whole
    # `tests` tree is named rather than one phase inside it: pinning
    # `tests/phase2` kept the accepted Phase 3 authorization and protocol suites
    # off the gate even though they were committed. Naming paths disables
    # `testpaths`, so `services` and `tests` are listed explicitly rather than
    # inherited, and the four must stay one invocation: several of these modules
    # import public barrels, and splitting the run is what hid the barrel-namespace
    # drift.
    (
        "Run full repository test suite",
        (
            "python -m pytest services tests packages/omnivia-core-runtime/tests "
            "packages/omnivia-core-client/tests -q"
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
REQUIRED_LOCAL_INSTALLS = (
    "python -m pip install -e .",
    'python -m pip install -e "services/omnivia-memory[dev]"',
    "python -m pip install -e packages/omnivia-core-runtime",
    "python -m pip install -e packages/omnivia-core-cli",
    "python -m pip install -e packages/omnivia-core-mcp",
    "python -m pip install -e packages/omnivia-core-client",
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
    commands = _commands(_step(_steps(), "Run Ruff"))
    assert len(commands) == 1, f"expected a single Ruff invocation, got: {commands}"
    _audit_targets(commands[0], "python -m ruff check", REQUIRED_RUFF_TARGETS)


def test_mypy_runs_strict_over_canonical_and_distribution_sources() -> None:
    commands = _commands(_step(_steps(), "Run strict mypy"))
    assert len(commands) == 1, f"expected a single mypy invocation, got: {commands}"
    _audit_targets(commands[0], "python -m mypy --strict", REQUIRED_MYPY_TARGETS)


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
    """The matrix and its two steps are the evidence this workflow exists to
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
    assert "python scripts/check-platform-lock-coverage.py" in _commands(
        _step(steps, "Assert this platform's lock case actually ran")
    )
    assert (REPO_ROOT / "scripts" / "check-platform-lock-coverage.py").is_file()


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
    """`testpaths` covers `services` and `tests` only, so a distribution's tests are
    collected only if the broad run names them. The whole `tests` tree must be
    named, not a directory inside it: naming the runtime's `tests/phase2` kept
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
