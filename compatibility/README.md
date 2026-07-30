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
module and fails when the file contradicts its state:

- a `direct_facade`, `split_facade`, or `transitive_facade` whose source is not
  one;
- a `hybrid_facade` whose source is not one. This state has *mixed* semantics on
  purpose: the barrel is source-unchanged, every portable name it publishes must
  resolve transitively — through an already-converted routed child — to the exact
  canonical Core object, and every remaining name it publishes must be the exact
  *legacy* object imported from a descendant module named in the registry's own
  `runtime_only_modules`. Both halves are required. A barrel with no runtime
  block left is a `transitive_facade` and must say so; one whose portable half
  has been canonicalised, rerouted at `omnivia_core`, or hidden behind a
  `__getattr__`/`__dir__` hook fails;
- a `source_parity` or `canonical_subset` leaf that has quietly become either
  kind of leaf facade without its state being moved forward.

The `pending_*` states are **not** all blanket-skipped. `pending_direct_barrel`
and `pending_root` assert nothing about source, so there is nothing there to
contradict, and the loader skips them; what constrains them is the
state/shape/`pair_kind` combination table — `pending_root` only on the package
root. `pending_hybrid` (only ever valid on a hybrid barrel) is deliberately kept
in the pending set *and* source-inspected, because a pending claim stops being
truthful once the file has caught up with it: if every routed prerequisite child
of the barrel is converted **and** its unchanged source already qualifies as an
exact hybrid facade over those children and its declared runtime-only
descendants, the registry is understating the file and the loader demands the
state move forward. While either condition is unmet — an unconverted routed
child, or a source that still has hybrid defects — the route is legitimately
pending and nothing is reported.

`graph.search_models` is a converted `split_facade`, not a duplicate: its three
portable query/result records resolve to the exact
`omnivia_core.graph.search_models` identities, while the four runtime-owned
scoring helpers canonical Core deliberately omits stay defined on the legacy
leaf. That is the whole difference between `split_facade` and `canonical_subset`
— the latter is a leaf still duplicating its canonical counterpart while holding
extra runtime-owned symbols, and this leaf has left it.

The six hybrid barrels — `graph`, `ingestion`, `ingestion.watcher`, `memory`,
`memory_graph`, `workspace` — are recorded as `hybrid_facade`, which makes them
accepted and `is_converted` **for compatibility accounting**, and nothing more
than that. It does not move their runtime halves into Core. Read it as: "the
portable half of this barrel has finished converting, and its runtime half is
exactly, and only, the declared runtime-only surface." Those runtime-owned
descendants are not routes, can never become converted children, and are still
resolved locally at import time from `services/omnivia-memory`; Core does not own
them and does not package them. That is also why these six are not
`transitive_facade`s — a transitive facade may reach nothing but its converted
routed children.

`is_converted` should be read the same way generally: it means a route has
completed the contract of the state it declares, not that every name it
publishes is canonical.

**Current status.** Every leaf and every barrel route is converted under its own
accepted contract — `direct_facade`, `split_facade`, `transitive_facade`, or
`hybrid_facade`. `source_parity`, `canonical_subset`, `pending_direct_barrel` and
`pending_hybrid` are all empty. The package root `omnivia_memory` is the one
route left, and it is still `pending_root`. `migration_state` remains the
authority for this per route, and the checker prints the partition; read its
output rather than trusting this paragraph after a change.

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
  legacy leaves they route. The six `hybrid_facade` barrels are gated as a batch
  of their own in that module: each is pinned byte-for-byte by SHA-256 against
  its accepted source, held to its frozen import-block table and ordered
  `__all__` at the AST level, run through the loader's own
  `hybrid_facade_defects` policy, and checked export by export — portable names
  are the exact canonical objects, runtime names the exact legacy ones.
- **Typed consumers.** `tests/test_typed_facade_consumers.py` keeps the
  strict-mypy leaf fixtures in `tests/typing/*_facade_consumer.py` an exact
  partition of `FACADE_ROUTES`, so an accepted route always has a real typed
  caller importing it by its legacy path — and a split facade's retained helpers,
  not being routes, are kept out of legacy `from` imports. The six hybrid barrels
  are *module* routes with no per-symbol `FACADE_ROUTES` entry, so they are
  deliberately outside that partition and get a separate fixture,
  `tests/typing/hybrid_barrel_consumer.py`, audited against the six barrels'
  `__all__` surfaces: all 93 names (62 portable + 31 runtime), imported by legacy
  barrel path only, with nothing missing, nothing extra, no leaf or
  `omnivia_core` path, and no alias.
- **Installed layout.** `tests/compatibility/test_facade_wheel_install.py` builds
  both wheels and installs them offline, so the bounded
  `compatibility_dependency` recorded above is checked as real packaging
  metadata, and the Graph split, the ingestion pair and the workspace leaf are
  re-verified against the installed artifacts — as are all six hybrid barrels,
  whose 62 portable exports must be the exact canonical identities and whose 31
  runtime exports must be the exact legacy identities of their named legacy
  owners. The same run proves the canonical barrels import cleanly from a
  Core-only environment and that no runtime-owned module behind any of the six
  was packaged into the Core wheel at all — not the Graph, ingestion or workspace
  runtime, and not the memory or memory-graph runtime either.

None of this says the conversion is finished — the package root remains
`pending_root`. And "zero barrels remaining" does not mean Core has taken
ownership of anything it had not already: the 31 runtime exports the six hybrid
barrels publish are still legacy-owned, still resolved locally out of
`services/omnivia-memory`, and still absent from the Core wheel. What the gates
above say is that what has been converted is correct, under the contract it
actually claims, and cannot silently stop being correct. For what is left, read
the checker's output, not this file.
