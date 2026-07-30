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

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACCEPTANCE_WORKFLOW = WORKFLOW_DIR / "core-acceptance.yml"
PERFORMANCE_WORKFLOW = WORKFLOW_DIR / "core-performance-report.yml"

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
    ("Verify Phase 0 baseline", "PYTHON=python scripts/check-core-baseline.sh"),
    # The full suite, on its own: an exact match rejects added filters.
    ("Run full repository test suite", "python -m pytest -q"),
    ("Run benchmark tests", "python -m pytest benchmarks/tests -q"),
)

# Lint steps whose command carries a target list; their scope gets its own test
# below, so they appear here only to pin their place in the gate order.
SCOPED_STEPS = ("Run Ruff", "Run strict mypy")

# Ruff's currently accepted clean scope: the canonical trees plus every
# converted legacy facade/barrel file. This list grows as leaves are converted;
# it is pinned exactly rather than counted, so an added target must be declared
# here and a dropped one fails.
REQUIRED_RUFF_TARGETS = (
    "src",
    "packages",
    "baseline",
    "tests",
    "scripts",
    "services/omnivia-memory/src/omnivia_memory/_shared/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/validation.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/models.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/validation.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/__init__.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/models.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/rules.py",
    "services/omnivia-memory/src/omnivia_memory/memory/models.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/provenance/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/validation.py",
)

REQUIRED_MYPY_TARGETS = (
    "src/omnivia_core",
    "packages/omnivia-core-runtime/src/omnivia_core_runtime",
    "packages/omnivia-core-mcp/src/omnivia_core_mcp",
    "packages/omnivia-core-cli/src/omnivia_core_cli",
    "baseline/facade_manifest.py",
    "scripts/check-facade-routes.py",
    # Every converted facade wrapper, plus the two strict-mypy consumer fixtures
    # that import them through their legacy paths: together they pin that
    # `omnivia-memory`'s `py.typed` surface still re-exports these names
    # explicitly and without `Any` leakage.
    "services/omnivia-memory/src/omnivia_memory/_shared/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/models.py",
    "services/omnivia-memory/src/omnivia_memory/app_shell_bridge/validation.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/models.py",
    "services/omnivia-memory/src/omnivia_memory/component_contract/validation.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/models.py",
    "services/omnivia-memory/src/omnivia_memory/lifecycle/rules.py",
    "services/omnivia-memory/src/omnivia_memory/memory/models.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/models.py",
    "services/omnivia-memory/src/omnivia_memory/module_manifest/validation.py",
    "services/omnivia-memory/src/omnivia_memory/provenance/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/models.py",
    "services/omnivia-memory/src/omnivia_memory/run_ledger/validation.py",
    "tests/typing/accepted_legacy_facade_consumer.py",
    "tests/typing/module_manifest_facade_consumer.py",
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

# The root package must be installed before the compatibility distribution,
# whose `omnivia-core>=0.1.0,<0.2.0` dependency this checkout satisfies.
REQUIRED_LOCAL_INSTALLS = (
    "python -m pip install -e .",
    'python -m pip install -e "services/omnivia-memory[dev]"',
)

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
    assert _entry(step, "uses") == "actions/checkout@v4"
    # Needed by both diff-check steps to resolve their commit ranges.
    assert _entry(_block(step, "with"), "fetch-depth") == "0"


def test_python_and_node_versions() -> None:
    steps = _steps()

    python_step = _step(steps, "Set up Python")
    assert _entry(python_step, "uses") == "actions/setup-python@v5"
    assert _entry(_block(python_step, "with"), "python-version") == '"3.11"'

    node_step = _step(steps, "Set up Node")
    assert _entry(node_step, "uses") == "actions/setup-node@v4"
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
    commands = _commands(_step(_steps(), "Install local packages"))
    positions = []
    for install in REQUIRED_LOCAL_INSTALLS:
        assert install in commands, f"missing local install: {install}"
        positions.append(commands.index(install))
    assert positions == sorted(positions), "the root package must be installed first"


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


def test_ruff_covers_the_accepted_clean_scope() -> None:
    commands = _commands(_step(_steps(), "Run Ruff"))
    assert len(commands) == 1, f"expected a single Ruff invocation, got: {commands}"
    assert commands[0].startswith("python -m ruff check ")
    targets = commands[0].split()
    for target in REQUIRED_RUFF_TARGETS:
        assert target in targets, f"Ruff scope is missing: {target}"


def test_mypy_runs_strict_over_canonical_and_distribution_sources() -> None:
    commands = _commands(_step(_steps(), "Run strict mypy"))
    assert len(commands) == 1, f"expected a single mypy invocation, got: {commands}"
    assert commands[0].startswith("python -m mypy --strict ")
    targets = commands[0].split()
    for target in REQUIRED_MYPY_TARGETS:
        assert target in targets, f"strict mypy scope is missing: {target}"


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
