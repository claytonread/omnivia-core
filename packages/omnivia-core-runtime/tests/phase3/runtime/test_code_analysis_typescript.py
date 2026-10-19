"""The TypeScript/JavaScript code-analysis producer over one sealed captured snapshot
(TECH-CORE-RETRIEVAL-CODE-001, work item W11).

Every test runs the real packaged analyser on the real pinned compiler under Node's
permission model, against a real migrated workspace, a really sealed `captured_v1`
snapshot and really published blobs. As in the Python producer's suite there is no
checkout on disk: the fixture repository exists only as blob bytes and capture rows.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_engineering_captured_source_coverage as captured
import test_engineering_source_coverage as esc
from omnivia_core_runtime.storage import code_analysis_typescript as subject
from omnivia_core_runtime.storage.code_analysis_candidates import (
    CodeAnalysisBindingError,
    CodeEdgeCandidate,
    CodeNodeCandidate,
    to_memory_create_inputs,
)
from omnivia_core_runtime.storage.code_analysis_typescript import (
    ANALYSER_DIGEST,
    ANALYSER_PATH,
    DEFAULT_TYPESCRIPT_PROFILE,
    NODE_KINDS,
    PACKAGED_TYPESCRIPT_ROOT,
    RELATIONSHIP_TYPES,
    TYPESCRIPT_DIGEST,
    TypeScriptAnalysisRefused,
    TypeScriptAnalysisResult,
    TypeScriptRuntime,
    analyse_typescript_snapshot,
    analysis_manifest,
    compiler_digest,
)
from omnivia_core_runtime.workspace.blob_publication import publish_blob

WORKSPACE_ID = esc.WORKSPACE_ID
REPO_ROOT = Path(__file__).resolve().parents[5]
SENTINEL = "TYPESCRIPT_PRODUCER_EXECUTED_SENTINEL"

# --- the fixture repository ---------------------------------------------------------

_TSCONFIG = """{
  // JSONC: comments and trailing commas are configuration, not errors.
  "extends": "./config/base.json",
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@lib/*": ["src/lib/*"] },
    "jsx": "preserve",
    "allowJs": true,
    "checkJs": false,
    "plugins": [{ "name": "never-loaded" }],
  },
  "include": ["src"],
  "references": [{ "path": "./packages/shared" }, { "path": "./packages/absent" }],
}
"""

_BASE = """{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "allowImportingTsExtensions": true,
    "noEmit": true
  }
}
"""

_SHARED_TSCONFIG = """{
  "extends": "@tsconfig/never-installed/tsconfig.json",
  "compilerOptions": { "strict": true, "module": "ESNext", "moduleResolution": "Bundler" },
  "include": ["src"]
}
"""

_SHARED = """export function shared(): number {
  return 1;
}
"""

_UTIL = """export function f(x: number): number;
export function f(x: string): string;
export function f(x: number | string) {
  return x;
}

export class Base {
  #count = 0;
  static create(): Base {
    return new Base();
  }
  m(): number {
    return this.#count;
  }
  $read(): number {
    return this.#count;
  }
}

export interface Shape {
  area(): number;
}
export interface Shape {
  name: string;
}

export const arrow = (): number => 1;

export enum Colour {
  Red,
  Green,
}

export function merged(): void {}
export namespace merged {
  export const extra = 1;
}
"""

_MAIN = """import { f, Base, arrow, merged } from "@lib/util";
import type { Shape } from "./lib/util";
import * as util from "./lib/util";
import missing from "./does-not-exist";
import React from "react";
import { lit } from "literal-mod";
import { shared } from "../packages/shared/src/index";
import { Shape as ShapeByMeaning } from "./lib/util";
export { f as g } from "./lib/util";
export * from "./cycle_a";

class Derived extends Base {
  m(): number {
    return super.m();
  }
}

export function run(b: Base, u: Base | { m(): number }, cb: () => void, s: Shape): void {
  f(1);
  f("é😀");
  b.m();
  u.m();
  cb();
  arrow();
  Base.create();
  new Derived();
  s.area();
  util.f(2);
  (b as any).anything();
  merged();
  lit();
  shared();
  missing;
  React;
  void import("./lib/util");
  const spec = "./lib/" + "util";
  void import(spec);
}

export const view = <Panel />;

function Panel() {
  return null;
}
"""

_AMBIENT = """declare module "literal-mod" {
  export function lit(): void;
}
"""

_CYCLE_A = """import { helper as other } from "./cycle_b";

export function helper(): number {
  return other();
}

export function shadowed(helper: () => number): number {
  return helper();
}
"""

_CYCLE_B = """import { helper as first } from "./cycle_a";

export function helper(): number {
  return first();
}
"""

_LEGACY = """const util = require("./lib/util");
util.f(1);
"""

_VIEW = """import { arrow } from "./lib/util";

export function View() {
  return <div>{arrow()}</div>;
}
"""

_EXECUTES = """import * as fs from "fs";
fs.writeFileSync("TYPESCRIPT_PRODUCER_EXECUTED_SENTINEL", "ran");
throw new Error("TYPESCRIPT_PRODUCER_EXECUTED_SENTINEL");
"""

# CRLF line endings, a two-byte, a combining and an astral character before the
# declaration, so a UTF-16 offset, a code-point index and a byte offset all differ.
_UNICODE = (
    "// \u00e9 \u00f1 e\u0301 \U0001f600 \u2028 x\r\n"
    'export const label = "\U0001f600\u00e9";\r\n'
    "export function after() {}\r\n"
    "export function trailing(): string {\r\n"
    "  return label;\r\n"
    "}\r\n"
)

REPOSITORY: dict[str, bytes] = {
    "tsconfig.json": _TSCONFIG.encode(),
    "config/base.json": _BASE.encode(),
    "packages/shared/tsconfig.json": _SHARED_TSCONFIG.encode(),
    "packages/shared/src/index.ts": _SHARED.encode(),
    "src/lib/util.ts": _UTIL.encode(),
    "src/main.tsx": _MAIN.encode(),
    "src/ambient.d.ts": _AMBIENT.encode(),
    "src/cycle_a.ts": _CYCLE_A.encode(),
    "src/cycle_b.ts": _CYCLE_B.encode(),
    "src/legacy.js": _LEGACY.encode(),
    "src/view.jsx": _VIEW.encode(),
    "src/executes.ts": _EXECUTES.encode(),
    "src/unicode.ts": _UNICODE.encode(),
    "src/bom.ts": b"\xef\xbb\xbfexport const marked = 1;\n",
    "src/broken.ts": b"export function (\n",
    "src/latin1.ts": b"export const caf\xe9 = 1;\n",
    "src/odd name.ts": b"export const odd = 1;\n",
    "README.md": b"# not source\n",
    "node_modules/react/index.d.ts": b"export default 1;\n",
}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class Snapshot:
    """One sealed captured snapshot whose bytes are really in a blob store."""

    def __init__(
        self,
        workspace: Any,
        blobs_root: Path,
        runtime: TypeScriptRuntime,
        *,
        files: dict[str, bytes],
        snapshot_id: str = "snap-ts-1",
        capture_status: str = "complete",
        base_us: int = 1_700_000_000_000_000,
    ) -> None:
        self.workspace = workspace
        self.blobs_root = blobs_root
        self.runtime = runtime
        self.contents = dict(files)
        self.digests = {path: _digest(content) for path, content in files.items()}
        for path, content in files.items():
            publish_blob(blobs_root, self.digests[path], content)
        self.sealed = captured._seal(
            workspace,
            repository_id="repo-ts",
            stream_id="stream-ts",
            principal_id="core-service",
            checkout_id="checkout-ts",
            snapshot_id=snapshot_id,
            files=self.digests,
            capture_status=capture_status,
            base_us=base_us,
        )

    @property
    def connection(self) -> Any:
        return self.workspace.holder.connection

    def analyse(self, **overrides: Any) -> TypeScriptAnalysisResult:
        arguments: dict[str, Any] = {
            "workspace_id": WORKSPACE_ID,
            "repository_id": self.sealed.repository_id,
            "snapshot_id": self.sealed.snapshot_id,
            "blobs_root": self.blobs_root,
            "runtime": self.runtime,
        }
        arguments.update(overrides)
        return analyse_typescript_snapshot(self.connection, **arguments)


@pytest.fixture(scope="module")
def runtime() -> TypeScriptRuntime:
    node = shutil.which("node")
    root = REPO_ROOT / "node_modules" / "typescript"
    assert node is not None, "node is required for the TypeScript producer"
    assert (root / "package.json").is_file(), "run npm ci so the pinned TypeScript compiler is available"
    return TypeScriptRuntime(node_executable=Path(node).resolve(), typescript_root=root)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Any]:
    opened = esc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


@pytest.fixture
def blobs_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


@pytest.fixture
def snapshot(workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime) -> Snapshot:
    return Snapshot(workspace, blobs_root, runtime, files=REPOSITORY)


@pytest.fixture(scope="module")
def analysed(tmp_path_factory: pytest.TempPathFactory, runtime: TypeScriptRuntime) -> Iterator[Any]:
    """One analysis of the fixture repository, shared by the read-only assertions."""
    tmp = tmp_path_factory.mktemp("ts-analysed")
    opened = esc.Workspace(tmp)
    blobs = tmp / "blobs"
    blobs.mkdir()
    sealed = Snapshot(opened, blobs, runtime, files=REPOSITORY)
    yield sealed, sealed.analyse()
    opened.holder.connection.close()


def _nodes(result: TypeScriptAnalysisResult) -> dict[str, CodeNodeCandidate]:
    assert result.batch is not None
    return {node.semantic_key: node for node in result.batch.nodes}


def _edges(result: TypeScriptAnalysisResult) -> tuple[CodeEdgeCandidate, ...]:
    assert result.batch is not None
    return result.batch.edges


def _endpoint(edge: CodeEdgeCandidate) -> str:
    if edge.target_key is not None:
        return edge.target_key
    assert edge.unresolved_target is not None
    return f"{edge.resolution_status}/{edge.unresolved_target.descriptor}"


def _from(result: TypeScriptAnalysisResult, relationship: str, source_key: str) -> dict[str, CodeEdgeCandidate]:
    return {
        _endpoint(edge): edge
        for edge in _edges(result)
        if edge.relationship_type == relationship and edge.source_key == source_key
    }


def _outcomes(result: TypeScriptAnalysisResult) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for report in result.reports:
        found.setdefault(report.relative_path, set()).add(report.outcome)
    return found


# --- coverage and programs ----------------------------------------------------------


def test_every_admitted_file_has_one_explicit_outcome(analysed: Any) -> None:
    _snapshot, result = analysed
    outcomes = _outcomes(result)
    assert outcomes["src/main.tsx"] == {"analysed"}
    assert outcomes["src/legacy.js"] == {"analysed"}
    assert outcomes["src/view.jsx"] == {"analysed"}
    assert outcomes["src/broken.ts"] == {"syntax_invalid"}
    assert outcomes["src/latin1.ts"] == {"decode_failed"}
    assert outcomes["src/odd name.ts"] == {"path_unsupported"}
    assert "README.md" not in outcomes
    assert not any(path.startswith("node_modules/") for path in outcomes)
    assert result.excluded_file_count == 2
    keys = _nodes(result)
    assert not any(key.startswith(("src/broken.ts", "src/latin1.ts", "src/odd name.ts")) for key in keys)


def test_configuration_is_honoured_and_its_gaps_are_reported(analysed: Any) -> None:
    _snapshot, result = analysed
    outcomes = _outcomes(result)
    assert outcomes["tsconfig.json"] == {"config_ignored", "reference_missing"}
    shared = [r for r in result.reports if r.relative_path == "packages/shared/tsconfig.json"]
    assert [r.outcome for r in shared] == ["config_invalid"]
    assert shared[0].detail is not None and "TS6053" in shared[0].detail
    assert shared[0].locator is not None and shared[0].locator.start_byte is not None
    programs = dict(result.programs)
    assert programs["packages/shared/tsconfig.json"] == ("packages/shared/src/index.ts",)
    assert "src/main.tsx" in programs["tsconfig.json"]
    assert None not in programs
    # The base config's `lib`-free target and the root config's JSX/allowJs were applied.
    assert result.lib_files and all(name.startswith("lib.") for name in result.lib_files)


# --- declarations, identities and groups --------------------------------------------


def test_declarations_key_by_path_and_qualified_name(analysed: Any) -> None:
    _snapshot, result = analysed
    nodes = _nodes(result)
    kinds = {key: node.kind for key, node in nodes.items()}
    assert kinds["src/lib/util.ts"] == "module"
    assert kinds["src/lib/util.ts:f"] == "function"
    assert kinds["src/lib/util.ts:f+2"] == "function"
    assert kinds["src/lib/util.ts:f+3"] == "function"
    assert kinds["src/lib/util.ts:f@group"] == "overload_group"
    assert kinds["src/lib/util.ts:Base.#count"] == "property"
    assert kinds["src/lib/util.ts:Base.$read"] == "method"
    assert kinds["src/lib/util.ts:Base.static:create"] == "method"
    assert kinds["src/lib/util.ts:Shape@group"] == "merge_group"
    assert kinds["src/lib/util.ts:merged@group"] == "merge_group"
    assert kinds["src/lib/util.ts:merged.extra"] == "variable"
    assert kinds["src/lib/util.ts:Colour.Green"] == "enum_member"
    assert kinds["src/ambient.d.ts:module:literal-mod"] == "ambient_module"
    assert kinds["src/ambient.d.ts:module:literal-mod.lit"] == "function"
    assert set(kinds.values()) <= NODE_KINDS
    assert {edge.relationship_type for edge in _edges(result)} <= RELATIONSHIP_TYPES
    groups = _from(result, "groups", "src/lib/util.ts:f@group")
    assert set(groups) == {"src/lib/util.ts:f", "src/lib/util.ts:f+2", "src/lib/util.ts:f+3"}
    defines = _from(result, "defines", "src/lib/util.ts:Base")
    assert "src/lib/util.ts:Base.m" in defines


# --- imports, exports and modules --------------------------------------------------


def test_imports_resolve_through_aliases_paths_and_literal_modules(analysed: Any) -> None:
    _snapshot, result = analysed
    imports = _from(result, "imports", "src/main.tsx")
    assert imports["src/lib/util.ts:f@group"].resolution_status == "resolved"
    assert "src/lib/util.ts:Base" in imports
    assert "src/lib/util.ts" in imports  # the namespace import names the module
    assert "src/ambient.d.ts:module:literal-mod.lit" in imports
    assert "src/main.tsx" not in imports
    assert "unresolved/module-not-found:./does-not-exist#missing" in imports
    assert "external/package:react#React" in imports
    assert "packages/shared/src/index.ts:shared" in imports
    type_imports = _from(result, "type_imports", "src/main.tsx")
    # `import type { Shape }` by syntax, and `import { Shape as ShapeByMeaning }` because
    # an interface has no value meaning: both are elided at runtime, so neither is a
    # runtime edge.
    assert set(type_imports) == {"src/lib/util.ts:Shape@group"}
    assert sum(1 for edge in _edges(result) if edge.relationship_type == "type_imports" and edge.source_key == "src/main.tsx") == 2
    assert "src/lib/util.ts:Shape@group" not in imports
    reexports = _from(result, "reexports", "src/main.tsx")
    assert set(reexports) == {"src/lib/util.ts:f@group", "src/cycle_a.ts"}
    dynamic = _from(result, "dynamic_imports", "src/main.tsx:run")
    assert dynamic["src/lib/util.ts"].evidence_strength == "resolved_static"
    assert dynamic["unresolved/dynamic-module-specifier"].evidence_strength == "inferred_dynamic"
    legacy = _from(result, "imports", "src/legacy.js:util")
    assert set(legacy) == {"src/lib/util.ts"}
    assert "src/lib/util.ts:f" in _from(result, "calls", "src/legacy.js")


# --- calls --------------------------------------------------------------------------


def test_calls_are_precise_where_static_and_conservative_where_not(analysed: Any) -> None:
    _snapshot, result = analysed
    calls = [
        edge
        for edge in _edges(result)
        if edge.relationship_type in {"calls", "constructs"} and edge.source_key == "src/main.tsx:run"
    ]
    by_target: dict[str, list[CodeEdgeCandidate]] = {}
    for edge in calls:
        by_target.setdefault(_endpoint(edge), []).append(edge)
    # Each overload call binds to the exact overload signature the checker chose.
    assert len(by_target["src/lib/util.ts:f"]) == 2  # f(1) and util.f(2)
    assert len(by_target["src/lib/util.ts:f+2"]) == 1  # f("...")
    assert by_target["src/lib/util.ts:Base.m"][0].evidence_strength == "inferred_dynamic"
    assert by_target["src/lib/util.ts:Shape.area"][0].evidence_strength == "inferred_dynamic"
    assert by_target["src/lib/util.ts:Base.static:create"][0].evidence_strength == "resolved_static"
    assert by_target["src/lib/util.ts:arrow"][0].evidence_strength == "resolved_static"
    assert by_target["src/main.tsx:Derived"][0].relationship_type == "constructs"
    union = [key for key in by_target if key.startswith("ambiguous/union-dispatch:")]
    assert len(union) == 1 and "src/lib/util.ts:Base.m" in union[0]
    assert "unresolved/indirect-call:cb" in by_target
    assert "unresolved/untyped-call:(b as any).anything" in by_target
    assert "src/lib/util.ts:merged" in by_target
    assert "src/ambient.d.ts:module:literal-mod.lit" in by_target
    assert "packages/shared/src/index.ts:shared" in by_target
    super_call = _from(result, "calls", "src/main.tsx:Derived.m")
    assert super_call["src/lib/util.ts:Base.m"].evidence_strength == "resolved_static"
    assert "src/lib/util.ts:Base" in _from(result, "extends", "src/main.tsx:Derived")
    assert "src/main.tsx:Panel" in _from(result, "renders", "src/main.tsx:view")


def test_cycles_same_names_and_shadowing_resolve_by_binding_not_spelling(analysed: Any) -> None:
    _snapshot, result = analysed
    assert set(_from(result, "calls", "src/cycle_a.ts:helper")) == {"src/cycle_b.ts:helper"}
    assert set(_from(result, "calls", "src/cycle_b.ts:helper")) == {"src/cycle_a.ts:helper"}
    # The parameter shadows the module's own `helper`: a call through it is a value call.
    assert set(_from(result, "calls", "src/cycle_a.ts:shadowed")) == {"unresolved/indirect-call:helper"}


# --- exact positions ----------------------------------------------------------------


def test_spans_are_exact_bytes_of_the_captured_content(analysed: Any) -> None:
    sealed, result = analysed
    content = sealed.contents["src/unicode.ts"]
    nodes = _nodes(result)
    for key, expected in (
        ("src/unicode.ts:label", 'label = "😀é"'),
        ("src/unicode.ts:after", "function after() {}"),
        ("src/unicode.ts:trailing", "function trailing(): string {\r\n  return label;\r\n}"),
    ):
        locator = nodes[key].source.locator
        assert locator.start_byte is not None and locator.end_byte is not None
        assert content[locator.start_byte : locator.end_byte].decode("utf-8").endswith(expected)
    # A byte-order mark is three captured bytes before the first token, not zero.
    marked = nodes["src/bom.ts:marked"].source.locator
    assert marked.start_byte is not None and marked.end_byte is not None
    assert sealed.contents["src/bom.ts"][marked.start_byte : marked.end_byte] == b"marked = 1"
    assert (marked.start_line, marked.start_column) == (1, 17)
    trailing = nodes["src/unicode.ts:trailing"].source.locator
    # Line 1 holds a U+2028, which ECMAScript counts as a line break of its own.
    assert (trailing.start_line, trailing.start_column) == (5, 1)
    assert (trailing.end_line, trailing.end_column) == (7, 2)
    for edge in _edges(result):
        locator = edge.source.locator
        assert locator.start_byte is not None and locator.end_byte is not None
        span = sealed.contents[edge.source.relative_path][locator.start_byte : locator.end_byte]
        span.decode("utf-8")  # never splits a character


def test_utf16_positions_convert_and_refuse_split_or_outside_offsets() -> None:
    text = "a😀\r\nb c\rd́"
    positions = subject._positions(text, set(range(12)) - {2})
    assert positions[1] == (1, 1, 2)
    assert positions[3] == (5, 1, 6)  # the CR, after the 4-byte emoji
    assert positions[5] == (7, 2, 1)  # `b`: CRLF is one break
    assert positions[6] == (8, 2, 2)  # U+2028 itself, three bytes
    assert positions[7] == (11, 3, 1)  # `c`, after U+2028
    assert positions[9] == (13, 4, 1)  # `d`, after a bare CR
    assert positions[10] == (14, 4, 2)  # the combining mark, its own UTF-16 unit
    assert positions[11] == (16, 4, 4)  # the end
    with pytest.raises(TypeScriptAnalysisRefused):
        subject._positions(text, {2})
    with pytest.raises(TypeScriptAnalysisRefused):
        subject._positions(text, {len(text.encode("utf-16-le")) // 2 + 1})
    with pytest.raises(TypeScriptAnalysisRefused):
        subject._positions("ascii", {6})
    # The LF of a CRLF sits on the CR's line in both paths, as the compiler counts it.
    assert subject._positions("a\r\nb", {2})[2] == (2, 1, 3)
    assert subject._positions("\u00e9\r\nb", {2})[2] == (3, 1, 4)


# --- determinism and revision binding ----------------------------------------------


def test_reanalysis_is_deterministic_and_binds_the_snapshot(analysed: Any) -> None:
    sealed, result = analysed
    again = sealed.analyse()
    assert result.batch is not None and again.batch is not None
    assert again.batch.digest == result.batch.digest
    assert analysis_manifest(again) == analysis_manifest(result)
    assert result.batch.profile_digest == DEFAULT_TYPESCRIPT_PROFILE.digest
    assert result.verified is not None and result.verified.batch is result.batch
    inputs = to_memory_create_inputs(result.verified, actor_id="core-service", asserted_at="2026-10-18T00:00:00Z")
    assert len(inputs) == len(result.batch.nodes) + len(result.batch.edges)


def test_config_and_dependency_changes_re_resolve_the_whole_program(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime
) -> None:
    files = {
        "tsconfig.json": b'{"compilerOptions": {"baseUrl": ".", "paths": {"@x/*": ["one/*"]}}}',
        "one/dep.ts": b"export function dep(): number { return 1; }\n",
        "two/dep.ts": b"export function dep(): number { return 2; }\n",
        "use.ts": b'import { dep } from "@x/dep";\ndep();\n',
    }
    first = Snapshot(workspace, blobs_root, runtime, files=files, snapshot_id="snap-ts-a").analyse()
    remapped = dict(files)
    remapped["tsconfig.json"] = b'{"compilerOptions": {"baseUrl": ".", "paths": {"@x/*": ["two/*"]}}}'
    second = Snapshot(
        workspace, blobs_root, runtime, files=remapped, snapshot_id="snap-ts-b", base_us=1_700_000_100_000_000
    ).analyse()
    renamed = dict(files)
    renamed["one/dep.ts"] = b"export function other(): number { return 1; }\n"
    third = Snapshot(
        workspace, blobs_root, runtime, files=renamed, snapshot_id="snap-ts-c", base_us=1_700_000_200_000_000
    ).analyse()
    assert set(_from(first, "calls", "use.ts")) == {"one/dep.ts:dep"}
    assert set(_from(second, "calls", "use.ts")) == {"two/dep.ts:dep"}
    assert set(_from(third, "imports", "use.ts")) == {"unresolved/missing-export:dep"}
    assert first.batch is not None and second.batch is not None
    assert first.batch.snapshot_id != second.batch.snapshot_id


# --- confinement --------------------------------------------------------------------


def test_source_is_never_executed_and_nothing_outside_the_snapshot_is_read(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"compilerOptions": {"paths": {"@leak/*": ["leak/*"]}}}', encoding="utf-8")
    files = {
        "tsconfig.json": f'{{"extends": "{outside}", "compilerOptions": {{}}}}'.encode(),
        "nested/tsconfig.json": b'{"extends": "../../../../../etc/hosts"}',
        "nested/a.ts": b"export const a = 1;\n",
        "src/executes.ts": _EXECUTES.encode(),
    }
    result = Snapshot(workspace, blobs_root, runtime, files=files, snapshot_id="snap-ts-escape").analyse()
    invalid = {r.relative_path: r.detail or "" for r in result.reports if r.outcome == "config_invalid"}
    assert invalid["tsconfig.json"].startswith("TS5083"), invalid  # cannot read: not in the snapshot
    assert invalid["nested/tsconfig.json"].startswith("TS6053"), invalid
    assert not (ANALYSER_PATH.parent / SENTINEL).exists()
    assert not (Path("/") / SENTINEL).exists()
    imports = _from(result, "imports", "src/executes.ts")
    assert set(imports) == {"external/package:fs"}


def test_the_analyser_runs_confined_by_node_permissions(runtime: TypeScriptRuntime, tmp_path: Path) -> None:
    probe = tmp_path / "probe.cjs"
    probe.write_text(
        'try { require("fs").readFileSync("/etc/hosts"); console.log("read"); } catch (e) { console.log(e.code); }'
        'try { eval("1"); console.log("eval"); } catch (e) { console.log(e.name); }'
        'console.log(typeof fetch);',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(runtime.node_executable),
            "--permission",
            f"--allow-fs-read={probe}",
            "--disallow-code-generation-from-strings",
            "--no-addons",
            "--no-experimental-fetch",
            "--no-experimental-websocket",
            str(probe),
        ],
        capture_output=True,
        env={},
        check=True,
    )
    assert completed.stdout.decode().split() == ["ERR_ACCESS_DENIED", "EvalError", "undefined"]
    source = (ANALYSER_PATH.with_suffix(".cts")).read_text(encoding="utf-8")
    assert "ts.sys" not in source.replace("`ts.sys`", "")
    # The analyser loads the compiler and `fs`, and no other module: no `net`, `http`,
    # `child_process` or `worker_threads` is ever in reach of a request.
    assert re.findall(r"require\((.*?)\)", source.split("declare const require", 1)[1].split("\n", 1)[1]) == [
        'process.argv[2] + "/lib/typescript.js"',
        '"fs"',
    ]
    for forbidden in ("emit(", "transformers", "eval(", "new Function", "child_process", "fetch("):
        assert forbidden not in source


# --- fail closed --------------------------------------------------------------------


def test_profile_runtime_and_snapshot_mismatches_refuse(
    snapshot: Snapshot, runtime: TypeScriptRuntime, tmp_path: Path
) -> None:
    with pytest.raises(CodeAnalysisBindingError):
        snapshot.analyse(expected_profile_digest="sha256:" + "0" * 64)
    with pytest.raises(TypeScriptAnalysisRefused, match="pinned build"):
        snapshot.analyse(profile=dataclasses.replace(DEFAULT_TYPESCRIPT_PROFILE, analyser_digest="sha256:" + "1" * 64))
    fake = tmp_path / "typescript"
    (fake / "lib").mkdir(parents=True)
    (fake / "package.json").write_text('{"name": "typescript", "version": "5.0.0"}', encoding="utf-8")
    (fake / "lib" / "typescript.js").write_text("", encoding="utf-8")
    with pytest.raises(TypeScriptAnalysisRefused, match="pinned version"):
        snapshot.analyse(runtime=dataclasses.replace(runtime, typescript_root=fake))
    # The right version label on the wrong bytes is still not the pinned compiler.
    (fake / "package.json").write_text('{"name": "typescript", "version": "5.9.3"}', encoding="utf-8")
    with pytest.raises(TypeScriptAnalysisRefused, match="compiler files"):
        snapshot.analyse(runtime=dataclasses.replace(runtime, typescript_root=fake))
    # With no root named, the runtime is the distribution's packaged compiler.
    assert TypeScriptRuntime(node_executable=runtime.node_executable).typescript_root == PACKAGED_TYPESCRIPT_ROOT
    with pytest.raises(TypeScriptAnalysisRefused, match="absolute"):
        snapshot.analyse(runtime=TypeScriptRuntime(node_executable=Path("node"), typescript_root=runtime.typescript_root))
    with pytest.raises(TypeScriptAnalysisRefused, match="candidates"):
        snapshot.analyse(profile=dataclasses.replace(DEFAULT_TYPESCRIPT_PROFILE, max_candidates=20))
    with pytest.raises(TypeScriptAnalysisRefused, match="Programs"):
        snapshot.analyse(profile=dataclasses.replace(DEFAULT_TYPESCRIPT_PROFILE, max_programs=1))
    with pytest.raises(TypeScriptAnalysisRefused, match="analysable files"):
        snapshot.analyse(profile=dataclasses.replace(DEFAULT_TYPESCRIPT_PROFILE, max_files=3))


def test_an_incomplete_capture_refuses(workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime) -> None:
    sealed = Snapshot(
        workspace, blobs_root, runtime, files={"a.ts": b"export const a = 1;\n"}, capture_status="incomplete"
    )
    with pytest.raises(CodeAnalysisBindingError):
        sealed.analyse()


@pytest.mark.parametrize(
    "tamper",
    [
        lambda answer: answer.update(request_digest="sha256:" + "2" * 64),
        lambda answer: answer.update(typescript_version="5.0.0"),
        lambda answer: answer["nodes"].append({**answer["nodes"][0], "kind": "invented"}),
        lambda answer: answer["nodes"].append({**answer["nodes"][0], "key": "x:y", "path": "src/broken.ts"}),
        lambda answer: answer["edges"].append({**answer["edges"][0], "path": "elsewhere.ts"}),
        lambda answer: answer["edges"].append({**answer["edges"][0], "end": 10**9}),
        lambda answer: answer["reports"].append({**answer["reports"][0], "outcome": "analysed"}),
        lambda answer: answer.update(refused="anything"),
        lambda answer: answer["nodes"].append(dict(answer["nodes"][0])),  # a duplicate identity
        lambda answer: answer["nodes"].append({**answer["nodes"][0], "key": "src/main.tsx:a b"}),
        lambda answer: answer["edges"].append({**answer["edges"][0], "target": "src/main.tsx:invented"}),
        lambda answer: answer["programs"].pop(),
        lambda answer: answer["programs"].append(answer["programs"][0]),
        lambda answer: answer["lib_files"].append("../../etc/hosts"),
    ],
)
def test_a_malformed_or_mismatched_answer_refuses(
    snapshot: Snapshot, monkeypatch: pytest.MonkeyPatch, tamper: Any
) -> None:
    real = subject._run_analyser

    def tampered(*args: Any) -> dict[str, object]:
        answer = real(*args)
        tamper(answer)
        return answer

    monkeypatch.setattr(subject, "_run_analyser", tampered)
    with pytest.raises(TypeScriptAnalysisRefused):
        snapshot.analyse()


def test_a_snapshot_without_typescript_is_an_empty_answer(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime
) -> None:
    result = Snapshot(workspace, blobs_root, runtime, files={"a.py": b"x = 1\n"}).analyse()
    assert result.verified is None and result.reports == () and result.programs == ()


# --- the build ----------------------------------------------------------------------


def test_configuration_cycles_terminate_and_are_reported(
    workspace: Any, blobs_root: Path, runtime: TypeScriptRuntime
) -> None:
    files = {
        # An `extends` cycle, which the compiler itself detects and reports.
        "loop/tsconfig.json": b'{"extends": "./base.json", "include": ["*.ts"]}',
        "loop/base.json": b'{"extends": "./tsconfig.json"}',
        "loop/a.ts": b"export const a = 1;\n",
        # A project-reference cycle through a config that is not named tsconfig.json.
        "ref/tsconfig.json": b'{"include": ["*.ts"], "references": [{"path": "./build.json"}]}',
        "ref/build.json": b'{"files": [], "references": [{"path": "./tsconfig.json"}]}',
        "ref/b.ts": b"export const b = 1;\n",
    }
    result = Snapshot(workspace, blobs_root, runtime, files=files, snapshot_id="snap-ts-cycles").analyse()
    loop = [r.detail or "" for r in result.reports if r.relative_path.startswith("loop/") and r.outcome == "config_invalid"]
    assert any(detail.startswith("TS18000") for detail in loop), loop
    outcomes = _outcomes(result)
    assert outcomes["loop/a.ts"] == {"analysed"} and outcomes["ref/b.ts"] == {"analysed"}
    assert dict(result.programs)["ref/tsconfig.json"] == ("ref/b.ts",)


def test_the_committed_analyser_is_the_pinned_compile_of_its_source(runtime: TypeScriptRuntime) -> None:
    assert ANALYSER_DIGEST == "sha256:" + hashlib.sha256(ANALYSER_PATH.read_bytes()).hexdigest()
    assert compiler_digest(runtime.typescript_root) == TYPESCRIPT_DIGEST
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate-code-analysis-typescript.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_adapter_has_no_graph_or_governance_writer_imports() -> None:
    import ast

    tree = ast.parse(Path(subject.__file__).read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any("graph" in module or "governance" in module for module in modules)
