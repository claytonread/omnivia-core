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

Module-level facts only. This file is a route registry, not a per-symbol
namespace manifest:

- 47 routes — one per module suffix present in **both** package trees, plus the
  package root — each carrying its `pair_kind`, `shape`, and `migration_state`.
- 21 `runtime_only_modules`: legacy modules with no canonical counterpart,
  recorded so they cannot be mistaken for missing routes.
- The pinned partition sizes in `expected_counts`.

What the registry does **not** carry as fields: per-symbol namespaces, symbol
owners, and collision pins. Those are not unchecked — they are enforced as
repository invariants by `baseline/inventory.py` and the baseline and
canonical-migration suites rather than encoded here; see *Related acceptance
coverage* below. Canonical-only modules (the contract layer) sit outside the
route set entirely, and the checker counts them separately instead of pinning
them.

The source policy each declared `migration_state` implies *is* enforced — by the
loader rather than by the JSON. `validate_checkout` parses every routed legacy
module and fails when the file contradicts its state: a `direct_facade`,
`split_facade`, or `transitive_facade` whose source is not one, and equally a
`source_parity` or `canonical_subset` leaf that has quietly become either kind of
facade without its state being moved forward. The `pending_*` states assert
nothing about source, so there is nothing there to contradict; what constrains
them is the state/shape/`pair_kind` combination table — `pending_hybrid` only on
a hybrid barrel, `pending_root` only on the package root.

Do not read the registry as a claim that every route is converted.
`migration_state` records how far each route has actually moved, and the checker
prints what remains; read that, not this paragraph, for the current count.

`graph.search_models` is a converted `split_facade`, not a duplicate: its three
portable query/result records resolve to the exact
`omnivia_core.graph.search_models` identities, while the four runtime-owned
scoring helpers canonical Core deliberately omits stay defined on the legacy
leaf. That is the whole difference between `split_facade` and `canonical_subset`
— the latter is a leaf still duplicating its canonical counterpart while holding
extra runtime-owned symbols, and this leaf has left it.

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

## Related acceptance coverage

The registry answers "this import path exists and pairs with that one", and the
loader adds "and its source matches the state it claims". The facts it does not
encode are enforced by other gates in the same acceptance run:

- **Per-symbol ownership and collisions.** `baseline/inventory.py` holds
  `FACADE_ROUTES` — per legacy module, which canonical module owns the exact
  object behind each routed symbol — plus the exact, named descriptor and
  root-binding ownership rewrites a conversion is permitted to cause. Anything
  outside those narrow rewrites still fails the frozen Phase 0 export baseline.
  `baseline/tests/test_public_exports.py` attacks the collision cases directly:
  routing a name two domains both define to the wrong domain's owner must be
  rejected, not normalized away.
- **Converted-leaf and barrel identity.** `tests/canonical_migration` and
  `tests/compatibility/test_facade_foundation.py` check that a converted leaf
  exposes the exact canonical objects and declares no `__all__` of its own. What
  the leaf's own source is allowed to be depends on which kind it is: a *direct*
  facade must be a pure re-export at the AST level, while a *split* facade —
  `graph.search_models` today — must route its whole portable namespace to those
  same exact canonical identities and may keep only an explicitly pinned set of
  legacy-owned definitions, whose ownership, signatures, and behavior are checked
  in place. Under either shape, barrels stay identity-preserving through the
  legacy leaves they route.
- **Typed consumers.** `tests/test_typed_facade_consumers.py` keeps the
  strict-mypy fixtures in `tests/typing/*_facade_consumer.py` a partition of
  `FACADE_ROUTES`, so an accepted route always has a real typed caller importing
  it by its legacy path — and a split facade's retained helpers, not being
  routes, are kept out of legacy `from` imports.
- **Installed layout.** `tests/compatibility/test_facade_wheel_install.py` builds
  both wheels and installs them offline, so the bounded
  `compatibility_dependency` recorded above is checked as real packaging
  metadata, and the Graph split, the ingestion pair and the workspace leaf are
  re-verified against the installed artifacts — including that none of the
  Graph, ingestion or workspace runtime was ever packaged into the Core wheel.

None of this says the conversion is finished — the six hybrid barrels and the
root remain. It says that what has been converted is correct and cannot silently
stop being correct. For what is left, read the checker's output, not this file.
