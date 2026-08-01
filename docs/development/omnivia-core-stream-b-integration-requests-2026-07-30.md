# OmniVia Core Stream B — Integration Requests

Date: 2026-07-30
From: Claude (Stream B implementation agent)
To: integration controller / Stream A
Branch carrying the work: `agent/omnivia-core-stream-b`

Prepared as requests rather than applied, per plan §8 rule 6: shared-file edits are
prepared and applied by the integration controller. Each item names the file, why it
is not mine, the exact change, and what breaks if it is skipped.

**IR-1 is the one that matters most: 286 Phase 2 tests currently exist and nothing
runs them.**

---

## IR-1 — Collect the Phase 2 suite (root `pyproject.toml`)

Owner: integration controller / Stream A (§7.2)
Severity: **the Phase 2 suite is not gated at all**

`[tool.pytest.ini_options] testpaths = ["services", "tests"]`. The runtime package's
tests live at `packages/omnivia-core-runtime/tests`, which is outside both, so
`python -m pytest -q` does not collect them. CI runs exactly that command, so all
286 tests are invisible to the gate. They pass — but only because I invoke them by
path.

```diff
 [tool.pytest.ini_options]
-testpaths = ["services", "tests"]
+testpaths = ["services", "tests", "packages/omnivia-core-runtime/tests"]
 pythonpath = ["src"]
```

`pythonpath` also needs the runtime and client sources, or the collected tests cannot
import what they test:

```diff
-pythonpath = ["src"]
+pythonpath = [
+    "src",
+    "services/omnivia-memory/src",
+    "packages/omnivia-core-runtime/src",
+    "packages/omnivia-core-cli/src",
+    "packages/omnivia-core-mcp/src",
+    "packages/omnivia-core-runtime/tests",
+]
```

Adding `services/omnivia-memory/src` also fixes the isolation problem in IR-4: a
`pythonpath` entry precedes site-packages, so it shadows the editable install without
anyone needing to remember an environment variable.

**Verify after applying:** `python -m pytest -q` should report **2,390** tests
(2,104 existing + 286 Phase 2) rather than 2,104.

**IR-1 must land together with IR-1b, or CI goes red.** I built and ran the combined
suite before writing this: it collects cleanly, and with IR-1b applied the combined
run is 2,389 passed and 1 skipped. Without IR-1b there is exactly one failure.
I applied IR-1b locally to prove it, then reverted it — it is Stream A's file.

Two problems surfaced from actually trying it, rather than from reasoning about it:

*Fixed on my branch already.* I had created
`packages/omnivia-core-runtime/tests/__init__.py`, which claims the top-level package
name `tests` — already owned by `services/omnivia-memory/tests`. With both on the
path, 24 service test modules failed to import as `tests.test_*`. The file is removed;
`phase2/__init__.py` remains, so pytest inserts the tests directory itself and
relative imports still work. No action needed from you, but it is why the count above
is trustworthy and my earlier estimate was not.

---

## IR-1b — Record the workspace barrel's incidental children

Owner: Stream A / migration gate (`tests/canonical_migration/test_pure_contract_barrels.py`)
Severity: **blocks IR-1** — one test fails without it

`test_barrel_namespace_matches_expected_names_exactly[workspace]` pins the exact
public namespace of `omnivia_core.workspace`. My new submodules are not inert: once
anything in the process imports `omnivia_core.workspace.manifest` or
`.compatibility`, ordinary Python import machinery binds `manifest` and
`compatibility` as attributes of the parent package, so the namespace grows by two
names.

This is the situation the test already documents and handles for
`omnivia_core.ingestion.watcher`, via `incidental_children`. The same treatment
applies:

```diff
         "children": {"models": "omnivia_core.workspace.models"},
-        "incidental_children": {},
+        "incidental_children": {
+            "manifest": "omnivia_core.workspace.manifest",
+            "compatibility": "omnivia_core.workspace.compatibility",
+        },
```

The failure appears **only** when the Phase 2 and canonical-migration suites run in
one process, which is precisely what IR-1 causes — so it is invisible today and would
show up as a red build the moment IR-1 lands. That is why the two are one change.

Worth noting for the F-06 discussion in IR-3: this is concrete evidence that adding
modules to a public package is observable behaviour, not a private implementation
detail, even before anything is exported from the barrel.

---

## IR-2 — Add a Windows job to CI (`.github/workflows/core-acceptance.yml`)

Owner: integration controller / Stream A (§7.2)
Severity: two exit-gate rows cannot pass without it

The T-0629 exit gate requires "platform-specific lock suites pass in CI". The
workflow runs `ubuntu-latest` only, so FL-02 (Windows two-process exclusion) and
FM-14 (Windows stock-SQLite exclusion) are skipped forever. GitHub provides
`windows-latest`, so this needs no provisioning — only the job.

Proposed job, alongside the existing one:

```yaml
  windows-locks:
    name: Windows lock semantics
    runs-on: windows-latest
    timeout-minutes: 20
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dev dependencies
        run: python -m pip install --disable-pip-version-check jsonschema[format]
      - name: Windows lock and fencing suites
        env:
          PYTHONPATH: >-
            src;services/omnivia-memory/src;packages/omnivia-core-runtime/src;packages/omnivia-core-cli/src;packages/omnivia-core-mcp/src;packages/omnivia-core-runtime/tests
        run: |
          python -m pytest -q packages/omnivia-core-runtime/tests/phase2/test_filesystem_locking.py packages/omnivia-core-runtime/tests/phase2/test_fencing_mutation.py
```

Note `;` as the `PYTHONPATH` separator on Windows.

**Expected first result:** FL-02 and FM-14 execute for the first time. I have not
run them — `WindowsFileLock` is written against `msvcrt.locking` and is unexercised,
so treat a first-run failure there as expected work rather than as a regression. The
POSIX equivalents pass, which tells us the test shape is right, not that the Windows
implementation is.

**One transport caveat for Windows:** `LocalSocketServer` uses `AF_UNIX`. Windows 10+
supports it, but if the job fails there, exclude `test_transport_conformance.py` from
the Windows run rather than weakening the transport — the lock and fencing suites are
what the exit gate names.

---

## IR-3 — Wire the new public contracts into the workspace barrel

Owner: integration controller (`src/omnivia_core/workspace/__init__.py`, §7.2)
Severity: the new public contracts are importable by path but not exported

T-0629A added `manifest.py`, `compatibility.py` and
`schemas/workspace-manifest-v1.schema.json` under `src/omnivia_core/workspace/`.
Those files are Stream B owned; `__init__.py` is not, so I did not touch it. The
barrel therefore still exports only the legacy `Workspace` models.

See also IR-1b: importing these modules already changes the observable namespace of
`omnivia_core.workspace`, so the decision below is about the *exported* surface, not
about whether they are visible at all.

This is deliberately **not** a mechanical diff, because of finding F-06 in the B0
review. Exporting both `Workspace` and `WorkspaceManifest` from one barrel puts a
portable, path-free manifest next to a non-portable model whose identity defaults to
`~/.omnivia/workspaces/<id>` and which calls `.resolve()`. A consumer could
reasonably mix them.

Requested decision before wiring, one of:

1. export both and state the boundary explicitly in the barrel docstring;
2. export the manifest types and mark `Workspace` deprecated in favour of them;
3. leave `Workspace` unexported from the portable surface entirely.

I have no view I would defend strongly here — it is a public-API decision, which is
Stream A's. What I would avoid is exporting both silently.

---

## IR-4 — Decide the durable fix for worktree import isolation

Owner: integration controller
Severity: caused ten phantom test failures during B0

A git worktree does not isolate Python imports. The venv holds
`_editable_impl_omnivia_memory.pth` pointing at the primary checkout, so tests run
from any worktree import that checkout's source.

Mitigated two ways already: `PYTHONPATH` shadowing, and
`packages/omnivia-core-runtime/tests/conftest.py`, which now refuses to run and
prints the path to set. IR-1's `pythonpath` change would make it correct by default.

The remaining decision is whether each worktree gets its own venv. My view: IR-1 is
sufficient and cheaper, and the conftest guard catches the case where someone works
around it.

---

## IR-5 — Settle `ruff format` (B0 finding F-05)

Owner: Stream A
Severity: cosmetic, but it will bite when merge-blocking CI lands

`ruff check` is clean across everything I touched. `ruff format --check` reports 45
files needing reformatting repository-wide, and there is no `[tool.ruff]`
configuration, so formatting is evidently not an intended gate. Worth deciding
explicitly before A0 adds merge-blocking CI, so that "Ruff clean" means the same
thing in CI as it does in the evidence.

---

## Handback summary, for §8 rule 8 review

Stream A reviews every Stream B handback for public-contract and dependency impact.

**Public contract surface added** (`src/omnivia_core/workspace/`, Stream B owned per
§7.2, not yet exported from the barrel — see IR-3):

```text
manifest.py        WorkspaceManifest, CoreCompatibility, ProjectionDeclaration,
                   EncryptionMetadata, MigrationSummary, IntegrityMetadata
compatibility.py   CompatibilityOutcome, CompatibilityStatus, evaluate_compatibility,
                   compare_versions, parse_version
schemas/workspace-manifest-v1.schema.json
```

Both modules are pure: no filesystem access, no non-stdlib imports.

**Dependency direction:** unchanged. `omnivia-core-runtime`, `-cli` and `-mcp` each
still declare only `omnivia-core`. The CLI and MCP packages import no runtime module,
enforced by an AST test rather than by convention. No new third-party dependency was
added anywhere.

**Files touched outside Stream B's own packages** — the T-0629F cutover file list:

```text
services/omnivia-memory/src/omnivia_memory/persistence/database.py
services/omnivia-memory/tests/test_persistence.py
```

`get_database()` no longer defaults to `~/.omnivia/memories.db`; it raises
`ImplicitDatabasePathRefused`. The two tests that asserted the old behaviour now pin
the refusal. This is the mutation cutover T-0629F requires, and it is the only change
outside `packages/**` and `src/omnivia_core/workspace/**`.

**Entry points registered:**

```text
omnivia-core-service = omnivia_core_runtime.service.main:main
omnivia              = omnivia_core_cli.main:main
```

**Not requested, and deliberately not done:** any product operation contract. A2 owns
the operation catalogue. `test_the_registry_holds_no_product_operations` fails if one
is added to the runtime instead.
