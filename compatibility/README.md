# Compatibility

`services/omnivia-memory` is being turned into a compatibility facade over
`src/omnivia_core`. Every legacy `omnivia_memory.*` import path must keep
working while the implementation moves to the canonical package, and that only
stays reviewable leaf by leaf if the *set* of paths involved is frozen first.

`facade-routes.v1.json` is that frozen set, and it is the authority for it.

## Authority

- The registry is the single source of truth for **which** legacy import paths
  exist, which canonical path each one pairs with, and which legacy modules
  deliberately have no canonical counterpart. Any other list of facade paths —
  in a plan, a task packet, a docstring, a review comment — is a copy and loses
  to this file.
- `baseline/facade_manifest.py` is the only loader. It enforces the cross-field
  invariants a JSON Schema cannot: suffix agreement between the paired paths,
  route ordering, path uniqueness, a single package root, parent/child topology
  matching each declared shape, the meaningful `pair_kind` / `shape` /
  `migration_state` combinations, and `expected_counts` agreeing with the
  `routes` array.
- `facade-routes.schema.json` is the structural half: keys, types, enums, and
  object-level uniqueness. The checker meta-validates and executes it before
  the loader. Passing it is necessary, never sufficient — the loader enforces
  the cross-field and checkout invariants.
- Neither the registry nor the loader imports `omnivia_core` or
  `omnivia_memory`. Checkout discovery walks files and reads names off the
  filesystem, so the registry can be validated before either package is
  installed and can never be satisfied by a stale installed copy.

## What v1 freezes

Module-level facts only:

- 47 routes — one per module suffix present in **both** package trees, plus the
  package root — each carrying its `pair_kind`, `shape`, and `migration_state`.
- 21 `runtime_only_modules`: legacy modules with no canonical counterpart,
  recorded so they cannot be mistaken for missing routes.
- The pinned partition sizes in `expected_counts`.

Deliberately **not** frozen at this checkpoint: per-symbol namespaces, symbol
owners, collision pins, source policies, and consumer migration. Canonical-only
modules (the contract layer) are reported by the checker but not pinned.

Do not read the registry as a claim that every route is converted.
`migration_state` records how far each route has actually moved; most routes are
still duplicated. `graph.search_models` is explicitly `canonical_subset`
because its legacy leaf retains four runtime-owned scoring helpers that
canonical Core deliberately omits.

## Updating the registry

Both files are **hand-maintained**. There is no generator, and adding one is not
the fix for a failing check — a registry regenerated from the tree it is meant
to constrain checks nothing.

The checker fails whenever the package trees and the registry disagree. That is
the intended signal, and it has exactly two honest resolutions:

1. **The tree changed by accident.** Fix the tree.
2. **The tree changed on purpose** — a module was added, removed, renamed, or a
   route was converted. Then, in the same commit:
   - edit `facade-routes.v1.json` by hand,
   - keep `routes` ordered by `legacy_module` ascending and
     `runtime_only_modules` ordered ascending,
   - update every affected number in `expected_counts`,
   - re-run the checker,
   - and say in the commit message *why* the set changed.

Never relax `baseline/facade_manifest.py` or its tests to make a check pass.
A loosened invariant silently un-freezes the registry for every future change,
not just the one in front of you.

Bump `format_version` (and add `facade-routes.v2.json`) only if the on-disk
*shape* changes. Adding routes or moving a `migration_state` forward is a v1
edit.

## Checking

```bash
PYTHONPATH=.:src:services/omnivia-memory/src .venv/bin/python scripts/check-facade-routes.py
```

Exits 0 with a partition summary, or non-zero with one diagnostic per
difference found, in a deterministic order.

`baseline/tests/test_facade_manifest.py` covers the loader independently: it
pins the expected route and runtime-only sets as literals rather than deriving
them from the registry, so a change to the JSON and a matching change to the
loader cannot agree with each other unnoticed.

## Next checkpoint

Per-symbol ownership and consumer migration:

- which symbol each legacy module re-exports, and which package owns it;
- collision pins where both trees define the same name;
- source policy per symbol;
- and the migration of consumers off the legacy import paths.

Until that lands, "this import path exists and pairs with that one" is the only
question this directory answers.
