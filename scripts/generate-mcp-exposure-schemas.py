#!/usr/bin/env python3
"""Generate the self-contained JSON Schemas the MCP exposure manifest advertises.

``contracts/application/v1/schemas`` is the canonical Application Contract v1
(ADR-038). Its documents reference each other by absolute
``https://contracts.omnivia.dev/...`` URI, which an MCP host cannot resolve: a
`tools/list` schema has to stand on its own, offline, in whatever process the
host runs. So each advertised schema is emitted here as one closed document --
the referenced definition verbatim, plus its complete transitive ``$defs``
closure, with every canonical ref rewritten to a local ``#/$defs/...`` one.

**Why a generator rather than a projector inside the MCP package.** The wheel
force-includes the canonical schemas under
``omnivia_core/contracts/v1/resources`` and the editable install this repository
develops against does not have them, so reading them at import time would make
`tools/list` depend on how Core was installed. A checked-in generated module is
present in both, byte-identical, and ``--check`` is what keeps it honest: the
canonical schemas move, this gate fails, and the regenerated module is a
reviewable diff rather than a runtime surprise.

**What is deliberately not generated.** The exposure set. This script reads its
operation names from :data:`EXPOSED_OPERATIONS` below -- a mirror of
``omnivia_core_mcp.manifest.EXPOSURE_MANIFEST``, held to it by
``test_the_generator_projects_exactly_the_exposed_operations`` -- and never from
the catalogue. Deriving it from the catalogue would make registering a Core
operation enough to advertise it to a model, which is the one thing the curated
manifest exists to prevent. What *is* read from the catalogue is each operation's
schema references, so a contract renamed upstream fails here rather than
projecting a stale shape.

Refusals, all of them loud:

* a ``$ref`` that is not exactly
  ``https://contracts.omnivia.dev/application/v1/<document>.schema.json#/$defs/<Name>``
  -- an unsupported form, or a reference outside Application Contract v1;
* a reference to a document or definition that does not exist;
* two distinct definitions that would generate one key;
* a generated document that is not closed (a local ref with no definition) or
  that carries a definition nothing references.

Standard library only, and nothing here writes to the canonical schemas, the
generated public bindings or the operation catalogue.

Regenerate: python scripts/generate-mcp-exposure-schemas.py
Verify:     python scripts/generate-mcp-exposure-schemas.py --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
TARGET = (
    REPO_ROOT
    / "packages"
    / "omnivia-core-mcp"
    / "src"
    / "omnivia_core_mcp"
    / "generated_schema_projection.py"
)

sys.path.insert(0, str(REPO_ROOT / "src"))

from omnivia_core.contracts.v1 import get_operation_metadata  # noqa: E402

#: The operations the curated MCP exposure manifest advertises, in its order.
#: A mirror, not a source: the manifest is the allow-list, and the test named in
#: this module's docstring fails if the two ever disagree.
EXPOSED_OPERATIONS: tuple[str, ...] = (
    "workspace.inspect",
    "evidence.search",
    "knowledge.search",
    "memory.search",
    "graph.traverse",
    "context_pack.build",
)

#: The only ref form this generator resolves. Anything else -- a relative ref, a
#: pointer that is not a ``$defs`` member, a host that is not the Application
#: Contract v1 base, a fragment-less document ref -- fails to match and is
#: refused rather than guessed at.
CANONICAL_REF = re.compile(
    r"^https://contracts\.omnivia\.dev/application/v1/"
    r"(?P<document>[a-z0-9]+(?:-[a-z0-9]+)*)\.schema\.json"
    r"#/\$defs/(?P<name>[A-Za-z][A-Za-z0-9]*)$"
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

HEADER = '''\
# Generated file. Do not edit.
#
# Source of truth:
#   contracts/application/v1/schemas (Application Contract v1, ADR-038)
# Generator:
#   scripts/generate-mcp-exposure-schemas.py
#
# Regenerate: python scripts/generate-mcp-exposure-schemas.py
# Verify:     python scripts/generate-mcp-exposure-schemas.py --check

"""Self-contained JSON Schemas for the MCP exposure manifest.

One entry per schema reference the curated manifest advertises, keyed by the
canonical Application Contract v1 reference the operation catalogue names. Each
value is a closed JSON Schema 2020-12 document: the referenced definition
verbatim, plus its complete transitive ``$defs`` closure, with every canonical
absolute reference rewritten to a local ``#/$defs/...`` one. Nothing here needs
network resolution, and nothing here is hand-written.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["SCHEMAS"]

#: Canonical schema reference -> the self-contained document advertised for it.
SCHEMAS: Final[dict[str, dict[str, Any]]] = '''


class ProjectionError(Exception):
    """A canonical schema this generator cannot project, stated as a refusal."""


def load_documents() -> dict[str, dict[str, Any]]:
    """Every canonical schema document, keyed by the name a ref spells."""
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ProjectionError(f"{path.name}: expected a JSON object at the root")
        documents[path.name.removesuffix(".schema.json")] = parsed
    if not documents:
        raise ProjectionError(f"no canonical schema documents under {SCHEMA_DIR}")
    return documents


def generated_key(document: str, name: str) -> str:
    """The ``$defs`` key one canonical definition is namespaced under.

    Document and definition, joined by a separator neither can contain: a
    document name is lower-case with hyphens, a definition name is
    alphanumeric, and ``__`` appears in neither. So the mapping is injective by
    construction -- and :func:`project` still checks, because "by construction"
    is an argument and a collision is a fact.
    """
    return f"{document.replace('-', '_')}__{name}"


def resolve(documents: dict[str, dict[str, Any]], ref: str) -> tuple[str, str, Any]:
    """The (document, name, definition) one canonical ref points at."""
    matched = CANONICAL_REF.match(ref)
    if matched is None:
        raise ProjectionError(
            f"{ref!r} is not a canonical Application Contract v1 $defs reference; "
            "this generator resolves "
            "https://contracts.omnivia.dev/application/v1/<document>.schema.json"
            "#/$defs/<Name> and nothing else"
        )
    document, name = matched.group("document"), matched.group("name")
    if document not in documents:
        raise ProjectionError(f"{ref}: no canonical document named {document!r}")
    definitions = documents[document].get("$defs")
    if not isinstance(definitions, dict) or name not in definitions:
        raise ProjectionError(f"{ref}: {document}.schema.json declares no {name!r}")
    return document, name, definitions[name]


def rewrite(node: Any, mapping: dict[str, str]) -> Any:
    """A deep copy of `node` with every canonical ref rewritten to a local one.

    Copying rather than mutating: the loaded documents are shared across the
    twelve projections, and a rewrite in place would leave the second projection
    reading refs the first had already localised.
    """
    if isinstance(node, dict):
        rewritten: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                rewritten[key] = f"#/$defs/{mapping[value]}"
            else:
                rewritten[key] = rewrite(value, mapping)
        return rewritten
    if isinstance(node, list):
        return [rewrite(item, mapping) for item in node]
    return node


def canonical_refs(node: Any) -> list[str]:
    """Every ``$ref`` value anywhere under `node`, in document order."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(canonical_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(canonical_refs(item))
    return found


def local_refs(node: Any) -> set[str]:
    """Every local ``#/$defs/<key>`` target under `node`."""
    return {ref.removeprefix("#/$defs/") for ref in canonical_refs(node)}


def project(documents: dict[str, dict[str, Any]], root_ref: str) -> dict[str, Any]:
    """One canonical ref as a closed, self-contained JSON Schema document."""
    _, _, root = resolve(documents, root_ref)
    if isinstance(root, dict) and "$defs" in root:
        raise ProjectionError(
            f"{root_ref}: the referenced definition declares its own $defs, which "
            "would collide with the generated closure"
        )

    # Breadth-first over the closure, seeded from the root's own references. The
    # root itself joins `$defs` only if something in the closure refers back to
    # it, which is what keeps a recursive definition closed without duplicating a
    # definition nothing points at.
    mapping: dict[str, str] = {}
    closure: dict[str, Any] = {}
    pending = list(canonical_refs(root))
    while pending:
        ref = pending.pop(0)
        if ref in mapping:
            continue
        document, name, definition = resolve(documents, ref)
        key = generated_key(document, name)
        collision = next((other for other, k in mapping.items() if k == key), None)
        if collision is not None:
            raise ProjectionError(
                f"{ref} and {collision} both generate the $defs key {key!r}"
            )
        mapping[ref] = key
        closure[key] = definition
        pending.extend(canonical_refs(definition))

    projected: dict[str, Any] = {"$schema": JSON_SCHEMA_DIALECT}
    projected.update(rewrite(root, mapping))
    if closure:
        projected["$defs"] = {
            key: rewrite(closure[key], mapping) for key in sorted(closure)
        }

    declared = set(projected.get("$defs", {}))
    referenced = local_refs(projected)
    unclosed = sorted(referenced - declared)
    if unclosed:
        raise ProjectionError(f"{root_ref}: generated refs resolve to nothing: {unclosed}")
    unused = sorted(declared - referenced)
    if unused:
        raise ProjectionError(f"{root_ref}: generated definitions nothing references: {unused}")
    return projected


def projections() -> dict[str, dict[str, Any]]:
    """Every advertised schema, keyed by the canonical ref the catalogue names."""
    documents = load_documents()
    refs: list[str] = []
    for operation in EXPOSED_OPERATIONS:
        entry = get_operation_metadata(operation)
        refs.extend((entry.input_schema_ref, entry.result_schema_ref))
    return {ref: project(documents, ref) for ref in sorted(set(refs))}


def python_literal(value: Any, indent: int) -> str:
    """`value` as a deterministic Python literal, one mapping entry per line.

    Written out rather than delegated to ``pprint``, whose line-breaking is a
    presentation choice that has changed between interpreter versions: the
    ``--check`` gate compares bytes, so the rendering has to depend on the input
    alone.
    """
    pad = "    " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = "".join(
            f"{pad}    {json.dumps(key)}: {python_literal(item, indent + 1)},\n"
            for key, item in value.items()
        )
        return "{\n" + body + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        body = "".join(
            f"{pad}    {python_literal(item, indent + 1)},\n" for item in value
        )
        return "[\n" + body + pad + "]"
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, str):
        # JSON string escaping is a subset of Python's, and `ensure_ascii`
        # keeps the emitted module pure ASCII whatever the schemas hold.
        return json.dumps(value)
    if isinstance(value, int | float):
        return repr(value)
    raise ProjectionError(f"cannot emit {type(value).__name__} as a Python literal")


def render() -> str:
    return f"{HEADER}{python_literal(projections(), 0)}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed module instead of writing it",
    )
    args = parser.parse_args(argv)

    try:
        rendered = render()
    except ProjectionError as refusal:
        print(f"MCP exposure schema generation FAILED: {refusal}", file=sys.stderr)
        return 1

    relative = TARGET.relative_to(REPO_ROOT)
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else None
        if current != rendered:
            print(f"{relative} is out of date.", file=sys.stderr)
            print(
                "Regenerate with: python scripts/generate-mcp-exposure-schemas.py",
                file=sys.stderr,
            )
            return 1
        print(f"{relative} is up to date ({len(projections())} advertised schemas).")
        return 0

    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
