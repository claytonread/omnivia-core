#!/usr/bin/env python3
"""Verify the frozen compatibility facade route registry against this checkout.

Runs two independent gates over ``compatibility/facade-routes.v1.json``:

1. **The schema.** ``compatibility/facade-routes.schema.json`` is
   meta-validated as a Draft 2020-12 schema and then applied to the committed
   document with ``jsonschema``. This is a real gate, not documentation: the
   structural half of the registry's contract is checked by a third-party
   implementation of a published standard rather than by the same code the
   loader uses, so a bug in one is not hidden by the other.
2. **The loader.** ``baseline/facade_manifest.py`` then validates the document
   strictly and rediscovers both package trees from source paths to confirm the
   frozen routes, runtime-only modules, shapes, and per-route migration states
   still describe reality.

Exits non-zero with one diagnostic per difference found, in a deterministic
order.

``jsonschema`` is the only third-party import, and it is deliberately confined
to this script: the loader stays standard-library only so it can run before
anything is installed. Neither this script nor the loader ever imports
``omnivia_core`` or ``omnivia_memory`` -- discovery walks files -- so no gate here
can be satisfied by a stale installed copy of either package.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import jsonschema

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from baseline.facade_manifest import (
    MANIFEST_PATH,
    SCHEMA_PATH,
    FacadeManifestError,
    MigrationState,
    PairKind,
    Shape,
    load_manifest,
    validate_checkout,
)


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def check_schema() -> list[str]:
    """Meta-validate the schema and validate the committed document against it.

    Returns one diagnostic per problem found, sorted, or an empty list.
    """
    try:
        schema = cast("bool | Mapping[str, Any]", _read_json(SCHEMA_PATH))
    except (OSError, json.JSONDecodeError) as error:
        return [f"cannot read {_relative(SCHEMA_PATH)}: {error}"]
    try:
        document = _read_json(MANIFEST_PATH)
    except (OSError, json.JSONDecodeError) as error:
        return [f"cannot read {_relative(MANIFEST_PATH)}: {error}"]

    validator_class = jsonschema.validators.Draft202012Validator
    try:
        validator_class.check_schema(schema)
    except jsonschema.exceptions.SchemaError as error:
        return [f"{_relative(SCHEMA_PATH)} is not a valid Draft 2020-12 schema: {error.message}"]

    return [
        f"{_relative(MANIFEST_PATH)} at ${''.join(f'[{part!r}]' for part in error.absolute_path)}"
        f": {error.message}"
        for error in sorted(
            validator_class(schema).iter_errors(document),
            key=lambda error: (list(map(str, error.absolute_path)), error.message),
        )
    ]


def main() -> int:
    schema_errors = check_schema()
    if schema_errors:
        print(f"FAIL: {_relative(MANIFEST_PATH)} does not match its schema:")
        for diagnostic in schema_errors:
            print(f"  - {diagnostic}")
        return 1

    try:
        manifest = load_manifest()
        modules = validate_checkout(REPO_ROOT, manifest)
    except FacadeManifestError as error:
        print(f"FAIL: {_relative(MANIFEST_PATH)} does not hold:")
        for diagnostic in error.errors:
            print(f"  - {diagnostic}")
        print(
            "\nEither the package trees changed or the registry drifted. Update "
            "compatibility/facade-routes.v1.json deliberately, with its "
            "expected_counts, and say why in the commit."
        )
        return 1

    counts = manifest.expected_counts
    print(f"OK: {_relative(SCHEMA_PATH)} meta-validates and the document matches it")
    print(f"OK: {_relative(MANIFEST_PATH)}")
    print(
        f"  routes: {counts.routes} "
        f"({counts.direct} direct, {counts.hybrid_barrel} hybrid_barrel, "
        f"{counts.root} root)"
    )
    print(
        f"  shapes: {counts.leaf} leaf, {counts.barrel} barrel, {counts.root} root"
    )
    for state in MigrationState:
        print(f"  {state.value}: {len(manifest.by_state(state))}")
    print(f"  runtime_only_modules: {counts.runtime_only_modules}")
    print(
        f"  checkout: {len(modules.shared)} shared, "
        f"{len(modules.legacy_only)} legacy-only, "
        f"{len(modules.canonical_only)} canonical-only (not routed at this checkpoint)"
    )
    unconverted = [
        route
        for route in manifest.routes
        if not route.is_converted and route.pair_kind is not PairKind.ROOT
    ]
    leaves_left = [route for route in unconverted if route.shape is Shape.LEAF]
    print(
        f"  remaining: {len(leaves_left)} leaves and "
        f"{len(unconverted) - len(leaves_left)} barrels still to convert"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
