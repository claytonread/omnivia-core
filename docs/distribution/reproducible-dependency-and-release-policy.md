# Reproducible Dependency and Release Policy

## Scope

This document records the *current, as-built* state of dependency handling for
OmniVia Core's two ecosystems -- npm (the root `package.json`) and Python (the
root distribution plus the five `packages/` and `services/` distributions) --
and the one open question about release-time Python dependency resolution that
follows from it.

It is a description of what the repository does today, read from
`package.json`, `package-lock.json`, `pyproject.toml`, `uv.lock`, and the three
workflows under `.github/workflows/`. It does not prescribe changes to any of
that, and it does not select or create a Python lock or constraints artifact --
see "Open decision" below.

## npm dependency handling (current state)

- `package-lock.json` is committed at the repository root, alongside
  `package.json`. The only declared dependency is a single `devDependency`,
  `typescript`.
- `core-acceptance.yml` is the only workflow that installs Node dependencies.
  Its `Set up Node` step (`actions/setup-node@v7`) pins `node-version: "22"`
  and sets `cache: npm` with `cache-dependency-path: package-lock.json`, and
  its `Install Node dependencies` step runs exactly `npm ci` -- no other npm
  *install* invocation appears anywhere in the workflow (a later `Check
  generated TypeScript contracts` step runs `npm run
  check:application-contracts`, which invokes the already-installed `tsc`
  rather than installing anything).
- `npm ci` installs exactly what `package-lock.json` records: it fails instead
  of re-resolving if the lockfile and `package.json` disagree, and it does not
  write to the lockfile. Combined with the committed lockfile, this makes the
  npm install step reproducible: the same commit installs the same resolved
  versions on every run. `core-acceptance.yml`'s own header comment records
  this in one line: "Node tooling is reproducible: `npm ci` installs exactly
  `package-lock.json`."
- The `cache: npm` setting caches npm's download cache keyed on the lockfile
  hash. It speeds up the install; it is not what makes the install
  reproducible -- `npm ci` against a committed lockfile is what does that,
  independent of whether the cache hits.
- `core-performance-report.yml` and `phase2-platform.yml` do not install Node
  dependencies at all; only `core-acceptance.yml` touches npm.

## Python dependency handling (current state)

- The root `pyproject.toml` declares no runtime dependencies for
  `omnivia-core` itself (`src/omnivia_core` is standard-library only). Its
  `[dependency-groups] dev` group pins development-only tooling by version
  *range*, not by exact version: `build>=1.2,<2`, `hatchling>=1.26,<2`,
  `jsonschema[format]>=4.25,<5`, `types-jsonschema>=4.25,<5`.
- Every sibling distribution under `packages/` (`omnivia-core-runtime`,
  `omnivia-core-mcp`, `omnivia-core-cli`, `omnivia-core-client`) declares its
  dependency on `omnivia-core` the same way: as the range
  `omnivia-core>=0.1.0,<0.2.0`, enforced structurally by
  `scripts/check-package-boundaries.py` (`check_siblings_depend_on_core`). The
  compatibility distribution `services/omnivia-memory` declares the identical
  range, but `scripts/check-package-boundaries.py` never processes its
  manifest -- that pin is enforced instead by
  `tests/compatibility/test_facade_wheel_install.py`, which reads
  `Requires-Dist` out of the built compatibility wheel's own `METADATA` and
  checks it against the test module's own `REQUIRED_CORE_DEPENDENCY`
  constant (`tests/compatibility/test_facade_wheel_install.py`). None of
  these manifests pin an exact version of anything.
- `uv.lock` is committed at the repository root and resolves the root
  project's dependency groups for local development (`uv sync --frozen`
  installs exactly what it records, without updating it). No workflow under
  `.github/workflows/` invokes `uv` in any form -- there is no `uv sync`,
  `uv run`, or `uv export` step in `core-acceptance.yml`,
  `core-performance-report.yml`, or `phase2-platform.yml`. `uv.lock` is a
  local-development artifact only; CI does not read or otherwise depend on
  it.
- Instead, all three workflows install Python dependencies with plain
  `pip install`, in two forms that both run fresh on every job:
  - Tooling and test-only dependencies, written out as literal range
    specifiers directly in the workflow step (e.g.
    `core-acceptance.yml`'s `Install Python tooling and test-only
    dependencies` step installs `"jsonschema[format]>=4.25,<5"`,
    `"hatchling>=1.26,<2"`, and similar pins by hand -- these are not read
    from `pyproject.toml`, they are restated in the YAML and kept in sync
    with it manually).
  - Every local distribution, installed editable (`pip install -e .`,
    `pip install -e packages/omnivia-core-runtime`, etc.), in every job that
    needs it. `omnivia-core` is unpublished, so this checkout is the only
    thing that can satisfy the `omnivia-core>=0.1.0,<0.2.0` range the other
    distributions declare, and every install step orders the root checkout
    first for that reason.
- No workflow enables pip caching (`actions/setup-python`'s `cache` input is
  never set). `core-acceptance.yml`'s own header comment records why: the
  repository carries range pins and no Python lockfile or constraints file,
  so resolution is not deterministic in the first place, and pip's cache only
  caches downloaded artifacts, not a resolved dependency set -- caching it
  would not make an already-nondeterministic resolution reproducible, so it
  is left off.
- Net effect: unlike the npm install, every Python dependency resolution in
  CI -- tooling and (for anything with a non-empty dependency list) local
  distributions -- resolves fresh against the configured index on every run,
  constrained only by the declared ranges, not pinned to exact, previously
  resolved versions. Two runs of the same commit can legitimately install
  different exact versions of a range-pinned dependency if a new compatible
  release appears on the index between them.
- No workflow in this repository publishes or installs from a controlled
  staging index; every Python install in CI is either editable-from-checkout
  or resolved live against the public index. There is no dedicated
  release/publish workflow under `.github/workflows/` today.

## Open decision: Python resolution artifact for release

**Status: open, unresolved. No format is selected here, and no lockfile or
constraints file is created by this document or alongside it.**

The npm side has a committed lockfile consumed by `npm ci` in CI, which makes
its resolution reproducible. The Python side has no equivalent used in CI:
`uv.lock` exists and locks the *local development* environment, but no
workflow reads it, and the six locally-installed distributions on the Python
side depend on each other by range, not by an exact, release-time-resolved
set.

What Core's release process should use, if anything, as the resolved-artifact
record of exact Python dependency versions at release time -- a `uv.lock`-style
lockfile consumed in CI the way `package-lock.json` is, a generated
constraints file (e.g. `pip install -c constraints.txt`), some other format,
or a deliberate decision to keep ranges and no lock at release time -- is an
Architecture/Release decision that has not been made. This document
deliberately takes no position on it and defers it to that decision, tracked
separately from this packet.
