# Reproducible Dependency and Release Policy

## Scope

This document records the *current, as-built* state of dependency handling for
OmniVia Core's two ecosystems -- npm (the root `package.json`) and Python (the
root distribution plus the five `packages/` and `services/` distributions) --
including the resolved dependency policy for the V06-7 Standard-profile wheel
candidate.

It is a description of what the repository does today, read from
`package.json`, `package-lock.json`, `pyproject.toml`, `uv.lock`, and the three
workflows under `.github/workflows/`. The ordinary development environment and
the Standard candidate deliberately have different resolution postures, stated
below rather than conflated.

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
- Net effect for ordinary editable development and acceptance installs: unlike
  the npm install, Python tooling and local-distribution setup resolve fresh
  against the configured index on every run, constrained by declared ranges.
  That path remains intentionally separate from the release-candidate path.
- Net effect for the V06-7 Standard-profile candidate: the five first-party
  wheels are built from one revision with `SOURCE_DATE_EPOCH` set to that
  revision's commit time; `scripts/mcp-wheelhouse-constraints.txt` fixes the
  complete third-party MCP closure to exact versions; the resolved wheelhouse
  is checked back against those pins; and the Standard profile is installed in
  a clean environment with `--no-index --only-binary=:all:` before its public
  CLI/service/MCP journey runs. The candidate records wheel hashes, the
  constraints hash, installed distributions, provenance, SPDX SBOM, licenses,
  checksums, and its unsigned state.
- No workflow in this repository publishes or installs from a controlled
  staging index; every Python install in CI is either editable-from-checkout
  or resolved live against the public index. There is no dedicated
  release/publish workflow under `.github/workflows/` today.

## Decision: Python resolution artifact for the Standard candidate

**Status: resolved for the V06-7 wheels-only Standard channel.**

The reviewed release-resolution artifact is
`scripts/mcp-wheelhouse-constraints.txt`. It pins every third-party package in
the MCP closure exactly. First-party distributions remain version-range linked
in their published metadata, but the candidate wheelhouse must contain exactly
one wheel for each of Core, Runtime, Client, CLI, and MCP from the same source
revision. The builder rejects a missing, duplicate, additional, or differently
versioned closure member.

This decision does not turn `uv.lock` into a release lock and does not claim the
ordinary editable CI setup is reproducible. It selects a narrower artifact for
the first release channel actually being qualified. A future channel that adds
extras or platform integrations must extend or replace the reviewed constraints
as an explicit dependency change; it may not silently inherit this closure.

Candidate construction is not release authorization. The builder records
`status: unsigned` and `production_release_eligible: false`; production signing
remains a separate release act requiring an approved signing identity.
