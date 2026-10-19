"""A non-executing TypeScript/JavaScript analyser over one sealed captured snapshot.

What this is: the second real producer for the candidate contract in
:mod:`omnivia_core_runtime.storage.code_analysis_candidates`, and a deliberately narrow
adapter. It reads the paths and content digests of one complete ``captured_v1`` snapshot,
pulls and re-hashes the exact bytes those digests name, hands their text to the packaged
analyser ``code_analysis_typescript.cjs`` in one bounded request, validates the answer
fail-closed, and returns node and edge *candidates* proven against that snapshot. Like the
Python producer it submits, approves, governs and writes nothing: the return value is
values, and the caller decides.

**Semantics come from the compiler, not from a re-implementation of it.** The analyser
builds TypeScript ``Program``\\ s with the pinned compiler (:data:`TYPESCRIPT_VERSION`) and
asks each ``TypeChecker`` what a name means, so aliases, re-exports, path mapping,
overloads, declaration merging, shadowing and cycles resolve the way ``tsc`` resolves
them. Every source file is analysed in exactly one Program -- its nearest including
``tsconfig.json``/``jsconfig.json`` project, else an inferred one -- and every run builds
every Program from scratch over the whole snapshot, so any change to a source, a config
or a dependency file is a new request and a full re-resolution. There is no cache to
reuse across revisions.

**The analysed repository is never executed and never read.** The analyser's
``CompilerHost`` is in-memory: it holds only this request's file texts plus the
compiler's own ``lib.*.d.ts`` declarations, and never consults ``ts.sys``. Nothing is
emitted, transformed, ``require``\\ d or evaluated; language-service plugins and project
JavaScript configuration are never loaded; package specifiers that the snapshot does not
contain are ``external``, never installed or fetched. Node itself runs under its
permission model with read access to exactly the analyser file and the compiler
directory, string code generation disabled, native addons, ``fetch`` and ``WebSocket``
off, child processes and workers denied, and an empty environment.

**The compiler is packaged, never borrowed.** A built wheel carries the pinned
compiler module and its default library declarations under
:data:`PACKAGED_TYPESCRIPT_ROOT` (``hatch_build.py`` copies them from the repository's
``npm ci`` install at build time), and :class:`TypeScriptRuntime` refuses any compiler
root whose files do not hash to :data:`TYPESCRIPT_DIGEST` -- the packaged one, or an
explicitly named install of the same release in a source checkout. Node is the one
thing the caller still supplies.

**Resolution is stated, never guessed.** An edge is ``resolved`` only when the checker
names one declaration this batch contains. A call through a union receiver is
``ambiguous``; a call through a value (a parameter, a ``let``, a function-typed property)
is ``unresolved`` as ``indirect-call``; a call the checker cannot type is
``dynamic-call``/``untyped-call``. A virtual call -- an overridable instance or interface
member -- is ``resolved`` to the statically bound signature with ``inferred_dynamic``
strength, because a subclass may still be what runs.

**Positions are exact.** The compiler counts UTF-16 code units; this adapter converts
every span against the exact decoded text into a half-open byte range of the captured
bytes, plus ECMAScript lines (LF, CR, CRLF, U+2028, U+2029) with UTF-8 byte columns. An
offset that falls inside a surrogate pair or past the text refuses the batch.

**What fails closed, and what does not**, mirroring the Python producer: a snapshot that
is not one complete sealed capture, an unexpected profile, an analyser file that is not
the pinned build, a compiler that is not the pinned version, any profile budget, a
timeout, and any malformed or mismatched answer refuse the whole batch. Per-file trouble
-- an oversize or undecodable file, invalid syntax, an over-budget tree, a path the
contract cannot key -- is one bounded report while every other file's facts survive.
Configuration trouble and declarations without a representable identity are reported
too, as explicit coverage entries beside the file outcomes.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from omnivia_core_runtime.storage.code_analysis_candidates import (
    MAX_CANDIDATES,
    MAX_LOCATOR_VALUE,
    CodeAnalysisBatch,
    CodeAnalysisBindingError,
    CodeAnalysisCandidateError,
    CodeEdgeCandidate,
    CodeLocator,
    CodeNodeCandidate,
    SourceBinding,
    VerifiedCodeAnalysisBatch,
    verify_batch_binding,
)
from omnivia_core_runtime.storage.code_analysis_python import (
    _captured_bytes,
    _captured_index,
    _detail,
    _edge,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.engineering_source import covered_snapshot

PRODUCER_ID: Final = "analyzer.typescript.checker"
PRODUCER_VERSION: Final = "1.0.0"
PROFILE_VERSION: Final = "typescript_analysis.v1"
TYPESCRIPT_VERSION: Final = "5.9.3"
#: The pinned compiler's behaviour-bearing files, as one digest: the SHA-256 of the
#: `sha256sum`-style listing `"<hex>  lib/<name>\n"` of `lib/typescript.js` and every
#: `lib/lib*.d.ts`, sorted by name. See :func:`compiler_digest`.
TYPESCRIPT_DIGEST: Final = "sha256:6ca61284adca4c30165a5df9e3c92ad5e83b506e13b5fa85b689895af6fef279"
#: Where a built wheel carries that compiler; absent from a source checkout.
PACKAGED_TYPESCRIPT_ROOT: Final = Path(__file__).with_name("typescript_compiler")
LOCATOR_BASIS: Final = "ecmascript_lines.utf8_byte_columns.v1"
REQUEST_FORMAT: Final = "typescript_analysis_request.v1"
RESPONSE_FORMAT: Final = "typescript_analysis_response.v1"

#: The compiled analyser this adapter runs, and the digest of the one build it accepts.
#: The digest is part of the profile, so a changed analyser is a changed profile and
#: re-identifies every batch it produces. `scripts/generate-code-analysis-typescript.py
#: --check` holds the file equal to the compiled `.cts`; this holds the file equal to
#: what the profile claims.
ANALYSER_PATH: Final = Path(__file__).with_name("code_analysis_typescript.cjs")
ANALYSER_DIGEST: Final = "sha256:fd3a6ba5483cedbae50a37b53f87c713ac2c83997d4bd0cc7f1021b1320c7eae"

#: One per admitted source file, always. Only `analysed` contributes candidates.
FILE_OUTCOMES: Final = frozenset(
    {"analysed", "oversize", "decode_failed", "syntax_invalid", "ast_budget_exceeded", "path_unsupported"}
)
#: Explicit coverage beside the file outcomes: what was read but not fully honoured.
COVERAGE_OUTCOMES: Final = frozenset(
    {
        "declaration_unsupported",
        "config_invalid",
        "config_empty",
        "config_ignored",
        "config_unsupported",
        "reference_missing",
    }
)
NODE_KINDS: Final = frozenset(
    {
        "module",
        "class",
        "interface",
        "type_alias",
        "enum",
        "enum_member",
        "function",
        "namespace",
        "ambient_module",
        "global_augmentation",
        "variable",
        "method",
        "property",
        "getter",
        "setter",
        "constructor",
        "overload_group",
        "merge_group",
    }
)
RELATIONSHIP_TYPES: Final = frozenset(
    {
        "defines",
        "groups",
        "imports",
        "type_imports",
        "dynamic_imports",
        "exports",
        "type_exports",
        "reexports",
        "type_reexports",
        "aliases",
        "calls",
        "constructs",
        "extends",
        "implements",
        "type_references",
        "references",
        "renders",
    }
)
_STATUSES: Final = frozenset({"resolved", "unresolved", "ambiguous", "external"})
_STRENGTHS: Final = frozenset({"syntactic", "resolved_static", "inferred_dynamic"})
_LIB_NAME: Final = re.compile(r"lib(\.[a-z0-9]+)*\.d\.ts")
_ECMASCRIPT_BREAK: Final = re.compile("\r\n|[\r\n\u2028\u2029]")


class TypeScriptAnalysisRefused(RuntimeError):
    """The snapshot cannot be analysed inside the bounds this profile declares."""


def compiler_digest(typescript_root: Path) -> str:
    """The :data:`TYPESCRIPT_DIGEST`-style digest of one compiler install's files.

    The files are exactly the ones the analyser can load: the compiler module and the
    `lib*.d.ts` names its in-memory host admits. A missing compiler module is no digest.
    """
    lib = typescript_root / "lib"
    try:
        names = ["typescript.js", *sorted(p.name for p in lib.iterdir() if _LIB_NAME.fullmatch(p.name))]
        listing = "".join(
            f"{hashlib.sha256((lib / name).read_bytes()).hexdigest()}  lib/{name}\n" for name in names
        )
    except OSError:
        return ""
    return "sha256:" + hashlib.sha256(listing.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TypeScriptAnalyserProfile:
    """The immutable, digestable identity of one analyser configuration."""

    producer_id: str = PRODUCER_ID
    producer_version: str = PRODUCER_VERSION
    profile_version: str = PROFILE_VERSION
    typescript_version: str = TYPESCRIPT_VERSION
    typescript_digest: str = TYPESCRIPT_DIGEST
    analyser_digest: str = ANALYSER_DIGEST
    locator_basis: str = LOCATOR_BASIS
    include_suffixes: tuple[str, ...] = (".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx")
    #: Read-only inputs: `tsconfig.json`/`jsconfig.json` projects, the files they
    #: `extends`, `package.json` module resolution and JSON imports. Never analysed.
    config_suffixes: tuple[str, ...] = (".json",)
    exclude_path_segments: tuple[str, ...] = (
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "venv",
    )
    exclude_path_prefixes: tuple[str, ...] = ()
    max_files: int = 500
    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 33_554_432
    max_ast_nodes_per_file: int = 200_000
    max_candidates: int = MAX_CANDIDATES
    max_file_reports: int = 250
    max_programs: int = 32
    max_extends_depth: int = 8
    max_references: int = 64
    timeout_seconds: int = 600
    max_heap_megabytes: int = 4096
    max_response_bytes: int = 268_435_456

    def __post_init__(self) -> None:
        for name in (
            "producer_id",
            "producer_version",
            "profile_version",
            "typescript_version",
            "typescript_digest",
            "analyser_digest",
            "locator_basis",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise CodeAnalysisCandidateError(f"profile {name} is not a bounded label")
        suffixes = self.include_suffixes + self.config_suffixes
        if not self.include_suffixes or any(
            not suffix.startswith(".") or len(suffix) > 16 for suffix in suffixes
        ):
            raise CodeAnalysisCandidateError("profile suffixes are not bounded suffixes")
        if set(self.include_suffixes) & set(self.config_suffixes):
            raise CodeAnalysisCandidateError("a suffix cannot be both source and configuration")
        limits = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_ast_nodes_per_file,
            self.max_candidates,
            self.max_file_reports,
            self.max_programs,
            self.max_extends_depth,
            self.max_references,
            self.timeout_seconds,
            self.max_heap_megabytes,
            self.max_response_bytes,
        )
        if any(isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 for limit in limits):
            raise CodeAnalysisCandidateError("profile limits must be positive integers")
        if self.max_candidates > MAX_CANDIDATES or self.max_file_bytes > self.max_total_bytes:
            raise CodeAnalysisCandidateError("profile limits are outside the contract's bounds")

    def as_document(self) -> dict[str, object]:
        return {
            "profile_version": self.profile_version,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "typescript_version": self.typescript_version,
            "typescript_digest": self.typescript_digest,
            "analyser_digest": self.analyser_digest,
            "locator_basis": self.locator_basis,
            "include_suffixes": list(self.include_suffixes),
            "config_suffixes": list(self.config_suffixes),
            "exclude_path_segments": list(self.exclude_path_segments),
            "exclude_path_prefixes": list(self.exclude_path_prefixes),
            "limits": {
                "max_files": self.max_files,
                "max_file_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes,
                "max_ast_nodes_per_file": self.max_ast_nodes_per_file,
                "max_candidates": self.max_candidates,
                "max_file_reports": self.max_file_reports,
                "max_programs": self.max_programs,
                "max_extends_depth": self.max_extends_depth,
                "max_references": self.max_references,
                "timeout_seconds": self.timeout_seconds,
                "max_heap_megabytes": self.max_heap_megabytes,
                "max_response_bytes": self.max_response_bytes,
            },
        }

    @property
    def digest(self) -> str:
        return content_digest(canonical_document(self.as_document()))

    def role(self, path: str) -> str | None:
        """`source`, `config` or None: what this profile reads one repository path as."""
        if any(segment in self.exclude_path_segments for segment in path.split("/")):
            return None
        if self.exclude_path_prefixes and path.startswith(self.exclude_path_prefixes):
            return None
        if path.endswith(self.include_suffixes):
            return "source"
        if path.endswith(self.config_suffixes):
            return "config"
        return None


DEFAULT_TYPESCRIPT_PROFILE: Final = TypeScriptAnalyserProfile()


@dataclass(frozen=True, slots=True)
class TypeScriptRuntime:
    """The Node executable and pinned compiler package the analyser runs on.

    Nothing here searches `PATH`, a project's `node_modules` or the environment. The
    compiler defaults to the one packaged in this distribution; a source checkout names
    its `npm ci` install instead. Either way it is verified against the profile's
    version and digest before anything runs.
    """

    node_executable: Path
    typescript_root: Path = PACKAGED_TYPESCRIPT_ROOT

    def resolved(self, profile: TypeScriptAnalyserProfile) -> tuple[Path, Path]:
        node = Path(self.node_executable)
        root = Path(self.typescript_root)
        if not node.is_absolute() or not root.is_absolute():
            raise TypeScriptAnalysisRefused("the analyser runtime paths must be absolute")
        node, root = node.resolve(), root.resolve()
        try:
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            package = None
        if not isinstance(package, dict) or package.get("name") != "typescript":
            raise TypeScriptAnalysisRefused("the compiler root is not a TypeScript package")
        if package.get("version") != profile.typescript_version:
            raise TypeScriptAnalysisRefused("the compiler is not the profile's pinned version")
        if not node.is_file() or not (root / "lib" / "typescript.js").is_file():
            raise TypeScriptAnalysisRefused("the analyser runtime is incomplete")
        if compiler_digest(root) != profile.typescript_digest:
            raise TypeScriptAnalysisRefused("the compiler files are not the profile's pinned build")
        return node, root


@dataclass(frozen=True, slots=True)
class TypeScriptFileReport:
    """What became of one admitted file, or one explicit gap in what it contributed."""

    relative_path: str
    content_digest: str
    outcome: str
    detail: str | None = None
    locator: CodeLocator | None = None

    def __post_init__(self) -> None:
        if self.outcome not in FILE_OUTCOMES | COVERAGE_OUTCOMES:
            raise CodeAnalysisCandidateError("unsupported file outcome")

    def as_document(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "outcome": self.outcome,
            "detail": self.detail,
            "locator": None if self.locator is None else self.locator.as_document(),
        }


@dataclass(frozen=True, slots=True)
class TypeScriptAnalysisResult:
    """One analysis of one snapshot: proven candidates, plus what was left out and why."""

    profile: TypeScriptAnalyserProfile
    #: None exactly when no admitted file contributed a node; see the Python producer.
    verified: VerifiedCodeAnalysisBatch | None
    reports: tuple[TypeScriptFileReport, ...]
    #: `(config path or None for the inferred Program, analysed paths)` per Program.
    programs: tuple[tuple[str | None, tuple[str, ...]], ...]
    lib_files: tuple[str, ...]
    request_digest: str
    indexed_file_count: int
    excluded_file_count: int
    analysed_bytes: int

    @property
    def batch(self) -> CodeAnalysisBatch | None:
        return None if self.verified is None else self.verified.batch


def _positions(text: str, offsets: Iterable[int]) -> dict[int, tuple[int, int, int]]:
    """UTF-16 offsets into `text` as `(byte offset, line, one-based UTF-8 byte column)`.

    Lines break the way ECMAScript's do -- LF, CR, CRLF, U+2028, U+2029 -- so a line
    number agrees with the compiler's own. An offset inside a surrogate pair or past the
    end is not a position in this text and refuses.
    """
    targets = sorted(set(offsets))
    if text.isascii():
        starts = [0] + [match.end() for match in _ECMASCRIPT_BREAK.finditer(text)]
        result: dict[int, tuple[int, int, int]] = {}
        for offset in targets:
            if not 0 <= offset <= len(text):
                raise TypeScriptAnalysisRefused("analyser position is outside its file")
            line = bisect.bisect_right(starts, offset)
            result[offset] = (offset, line, offset - starts[line - 1] + 1)
        return result
    result = {}
    index = unit = byte = line_start = 0
    line = 1
    for position, char in enumerate(text):
        while index < len(targets) and targets[index] == unit:
            result[unit] = (byte, line, byte - line_start + 1)
            index += 1
        if index < len(targets) and targets[index] < unit:
            raise TypeScriptAnalysisRefused("analyser position splits a surrogate pair")
        code = ord(char)
        unit += 2 if code > 0xFFFF else 1
        byte += 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
        # A CR that a LF follows is not a break yet: CRLF is one break, after the LF,
        # exactly as `_ECMASCRIPT_BREAK` and the compiler's own line starts count it.
        if char in "\n\u2028\u2029" or (char == "\r" and not text.startswith("\n", position + 1)):
            line += 1
            line_start = byte
    while index < len(targets) and targets[index] == unit:
        result[unit] = (byte, line, byte - line_start + 1)
        index += 1
    if index < len(targets):
        raise TypeScriptAnalysisRefused("analyser position is outside its file")
    return result


def _locator(positions: dict[int, tuple[int, int, int]], start: int, end: int) -> CodeLocator:
    start_byte, start_line, start_column = positions[start]
    end_byte, end_line, end_column = positions[end]
    return CodeLocator(
        start_line=min(start_line, MAX_LOCATOR_VALUE),
        start_column=min(start_column, MAX_LOCATOR_VALUE),
        end_line=min(end_line, MAX_LOCATOR_VALUE),
        end_column=min(end_column, MAX_LOCATOR_VALUE),
        start_byte=start_byte,
        end_byte=end_byte,
    )


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise TypeScriptAnalysisRefused("analyser answer holds a malformed string")
    return value


def _span(item: dict[str, object]) -> tuple[int, int]:
    start, end = item.get("start"), item.get("end")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not 0 <= start <= end
    ):
        raise TypeScriptAnalysisRefused("analyser answer holds a malformed span")
    return start, end


def _records(answer: dict[str, object], name: str) -> list[dict[str, object]]:
    value = answer.get(name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeScriptAnalysisRefused("analyser answer is malformed")
    return value


def _sealed_snapshot_kind(
    connection: sqlite3.Connection, *, workspace_id: str, repository_id: str, snapshot_id: str
) -> str:
    snapshot = covered_snapshot(
        connection,
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
    )
    if snapshot is None:
        raise CodeAnalysisBindingError("snapshot is not inside covered source history")
    if snapshot.representation != "captured_v1":
        raise CodeAnalysisBindingError("code analysis requires a sealed captured snapshot")
    if snapshot.capture_status != "complete":
        raise CodeAnalysisBindingError("source capture is incomplete")
    kind_row = connection.execute(
        "SELECT snapshot_kind FROM omnivia_engineering_snapshots "
        "WHERE workspace_id = ? AND snapshot_id = ? AND repository_id = ?",
        (workspace_id, snapshot_id, repository_id),
    ).fetchone()
    if kind_row is None:
        raise CodeAnalysisBindingError("snapshot kind is not recorded for this repository")
    return str(kind_row[0])


def _run_analyser(
    node: Path, typescript_root: Path, payload: bytes, profile: TypeScriptAnalyserProfile
) -> dict[str, object]:
    """Run the pinned analyser once, confined, and return its decoded answer."""
    analyser = ANALYSER_PATH.resolve()
    try:
        build = analyser.read_bytes()
    except OSError:
        build = b""
    if "sha256:" + hashlib.sha256(build).hexdigest() != profile.analyser_digest:
        raise TypeScriptAnalysisRefused("the analyser file is not the profile's pinned build")
    command = [
        str(node),
        "--permission",
        f"--allow-fs-read={analyser}",
        f"--allow-fs-read={typescript_root}",
        "--disallow-code-generation-from-strings",
        "--no-addons",
        "--no-experimental-fetch",
        "--no-experimental-websocket",
        f"--max-old-space-size={profile.max_heap_megabytes}",
        str(analyser),
        str(typescript_root),
    ]
    timed_out = False
    completed: subprocess.CompletedProcess[bytes] | None = None
    try:
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            timeout=profile.timeout_seconds,
            env={},
            cwd=analyser.parent,
            check=False,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out or completed is None:
        raise TypeScriptAnalysisRefused("the analyser exceeded the profile's time budget")
    if len(completed.stdout) > profile.max_response_bytes:
        raise TypeScriptAnalysisRefused("the analyser answer exceeds the profile's size budget")
    answer: object = None
    try:
        answer = json.loads(completed.stdout)
    except ValueError:
        answer = None
    if completed.returncode != 0 or not isinstance(answer, dict):
        raise TypeScriptAnalysisRefused("the analyser did not complete")
    return answer


def analyse_typescript_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    snapshot_id: str,
    blobs_root: Path,
    runtime: TypeScriptRuntime,
    profile: TypeScriptAnalyserProfile = DEFAULT_TYPESCRIPT_PROFILE,
    expected_profile_digest: str | None = None,
    heartbeat: Callable[[], object] | None = None,
) -> TypeScriptAnalysisResult:
    """Analyse one sealed captured snapshot's TypeScript and JavaScript into candidates.

    `heartbeat` is called once per admitted file before its blob is opened and once
    before the analyser starts; a `heartbeat` that raises aborts with nothing written.
    The analyser itself is bounded by the profile's `timeout_seconds`, not by heartbeats.
    """
    if expected_profile_digest is not None and expected_profile_digest != profile.digest:
        raise CodeAnalysisBindingError("analyser profile digest is not the expected profile")
    node, typescript_root = runtime.resolved(profile)
    snapshot_kind = _sealed_snapshot_kind(
        connection, workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id
    )
    index = _captured_index(connection, workspace_id=workspace_id, snapshot_id=snapshot_id)
    roles = {path: profile.role(path) for path in index}
    admitted = sorted(path for path, role in roles.items() if role is not None)
    if len(admitted) > profile.max_files:
        raise TypeScriptAnalysisRefused("snapshot holds more analysable files than the profile admits")

    reports: list[TypeScriptFileReport] = []
    texts: dict[str, str] = {}
    analysed_bytes = 0
    for relative_path in admitted:
        if heartbeat is not None:
            heartbeat()
        digest = index[relative_path]
        content = _captured_bytes(blobs_root, digest, max_bytes=profile.max_file_bytes)
        if content is None:
            reports.append(
                TypeScriptFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="oversize",
                    detail=f"captured object exceeds {profile.max_file_bytes} bytes",
                )
            )
            continue
        analysed_bytes += len(content)
        if analysed_bytes > profile.max_total_bytes:
            raise TypeScriptAnalysisRefused("snapshot source exceeds the profile's total byte budget")
        try:
            texts[relative_path] = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            line = content[: exc.start].count(b"\n") + 1
            reports.append(
                TypeScriptFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="decode_failed",
                    detail=f"undecodable UTF-8 byte at offset {exc.start}",
                    locator=CodeLocator(
                        start_line=line, start_column=1, end_line=line, end_column=1
                    ),
                )
            )

    sources = sorted(path for path in texts if roles[path] == "source")
    configs = sorted(path for path in texts if roles[path] == "config")
    request_digest = content_digest(
        canonical_document(
            {
                "format": REQUEST_FORMAT,
                "profile_digest": profile.digest,
                "sources": [[path, index[path]] for path in sources],
                "aux": [[path, index[path]] for path in configs],
            }
        )
    )
    request = {
        "format": REQUEST_FORMAT,
        "request_digest": request_digest,
        "typescript_version": profile.typescript_version,
        "limits": {
            "max_programs": profile.max_programs,
            "max_extends_depth": profile.max_extends_depth,
            "max_references": profile.max_references,
            "max_ast_nodes_per_file": profile.max_ast_nodes_per_file,
            "max_candidates": profile.max_candidates,
            "max_reports": profile.max_file_reports,
        },
        "sources": [{"path": path, "text": texts[path]} for path in sources],
        "aux": [{"path": path, "text": texts[path]} for path in configs],
    }
    if not sources:
        answer: dict[str, object] = {
            "format": RESPONSE_FORMAT,
            "request_digest": request_digest,
            "typescript_version": profile.typescript_version,
            "programs": [],
            "lib_files": [],
            "reports": [],
            "nodes": [],
            "edges": [],
        }
    else:
        if heartbeat is not None:
            heartbeat()
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        answer = _run_analyser(node, typescript_root, payload, profile)
    if answer.get("format") != RESPONSE_FORMAT or answer.get("request_digest") != request_digest:
        raise TypeScriptAnalysisRefused("the analyser answered a different request")
    refused = answer.get("refused")
    if refused is not None:
        raise TypeScriptAnalysisRefused(
            "the analyser refused: " + _detail(refused if isinstance(refused, str) else "unknown")
        )
    if answer.get("typescript_version") != profile.typescript_version:
        raise TypeScriptAnalysisRefused("the analyser ran a compiler that is not the pinned version")

    positions_cache: dict[str, dict[int, tuple[int, int, int]]] = {}
    raw_reports = _records(answer, "reports")
    raw_nodes = _records(answer, "nodes")
    raw_edges = _records(answer, "edges")
    wanted: dict[str, set[int]] = {}

    def want(path: str, start: int, end: int) -> None:
        wanted.setdefault(path, set()).update((start, end))

    for item in raw_reports:
        path = _text(item.get("path"), 4096)
        if path not in texts:
            raise TypeScriptAnalysisRefused("analyser report names a file it was not given")
        if item.get("start") is not None:
            want(path, *_span(item))
    for item in raw_nodes + raw_edges:
        path = _text(item.get("path"), 4096)
        if path not in texts:
            raise TypeScriptAnalysisRefused("analyser answer names a file it was not given")
        want(path, *_span(item))
    for path, offsets in wanted.items():
        positions_cache[path] = _positions(texts[path], offsets)

    file_outcomes: dict[str, str] = {}
    for item in raw_reports:
        path = str(item["path"])
        outcome = item.get("outcome")
        detail = item.get("detail")
        if outcome not in (FILE_OUTCOMES | COVERAGE_OUTCOMES) - {"oversize", "decode_failed"}:
            raise TypeScriptAnalysisRefused("analyser report holds an unknown outcome")
        if not isinstance(detail, str) or len(detail) > 1024:
            raise TypeScriptAnalysisRefused("analyser report detail is malformed")
        outcome = str(outcome)
        if outcome in FILE_OUTCOMES:
            if path not in sources or path in file_outcomes:
                raise TypeScriptAnalysisRefused("analyser file outcomes are not one per source")
            file_outcomes[path] = outcome
        locator = None
        if item.get("start") is not None:
            start, end = _span(item)
            locator = _locator(positions_cache[path], start, end)
        reports.append(
            TypeScriptFileReport(
                relative_path=path,
                content_digest=index[path],
                outcome=outcome,
                detail=_detail(detail) or None,
                locator=locator,
            )
        )
    if set(file_outcomes) != set(sources):
        raise TypeScriptAnalysisRefused("analyser file outcomes are not one per source")
    problems = sum(1 for report in reports if report.outcome != "analysed")
    if problems > profile.max_file_reports:
        raise TypeScriptAnalysisRefused("snapshot holds more unanalysable files than the profile admits")
    analysed = {path for path, outcome in file_outcomes.items() if outcome == "analysed"}

    def binding(item: dict[str, object]) -> SourceBinding:
        path = str(item["path"])
        if path not in analysed:
            raise TypeScriptAnalysisRefused("analyser candidate names a file that was not analysed")
        start, end = _span(item)
        return SourceBinding(
            relative_path=path,
            content_digest=index[path],
            locator=_locator(positions_cache[path], start, end),
        )

    programs: list[tuple[str | None, tuple[str, ...]]] = []
    for item in _records(answer, "programs"):
        config, files = item.get("config"), item.get("files")
        if (config is not None and config not in texts) or not isinstance(files, list):
            raise TypeScriptAnalysisRefused("analyser program record is malformed")
        if not all(isinstance(path, str) and path in texts for path in files):
            raise TypeScriptAnalysisRefused("analyser program record is malformed")
        programs.append((config if isinstance(config, str) else None, tuple(str(path) for path in files)))
    # Every source is analysed in exactly one Program, so the Programs partition them.
    if sorted(path for _config, files in programs for path in files) != sources:
        raise TypeScriptAnalysisRefused("analyser programs do not partition the sources")
    lib_files = answer.get("lib_files")
    if not isinstance(lib_files, list) or not all(
        isinstance(name, str) and _LIB_NAME.fullmatch(name) for name in lib_files
    ):
        raise TypeScriptAnalysisRefused("analyser answer is malformed")

    def finished(verified: VerifiedCodeAnalysisBatch | None) -> TypeScriptAnalysisResult:
        return TypeScriptAnalysisResult(
            profile=profile,
            verified=verified,
            reports=tuple(sorted(reports, key=lambda report: canonical_document(report.as_document()))),
            programs=tuple(programs),
            lib_files=tuple(str(name) for name in lib_files),
            request_digest=request_digest,
            indexed_file_count=len(index),
            excluded_file_count=len(index) - len(admitted),
            analysed_bytes=analysed_bytes,
        )

    try:
        nodes: list[CodeNodeCandidate] = []
        for item in raw_nodes:
            kind = item.get("kind")
            if kind not in NODE_KINDS:
                raise TypeScriptAnalysisRefused("analyser node holds an unknown kind")
            nodes.append(
                CodeNodeCandidate(
                    semantic_key=_text(item.get("key"), 256),
                    kind=str(kind),
                    display_name=_text(item.get("name"), 512),
                    source=binding(item),
                )
            )
        edges: dict[str, CodeEdgeCandidate] = {}
        for item in raw_edges:
            relationship, status, strength = item.get("relationship"), item.get("status"), item.get("strength")
            if relationship not in RELATIONSHIP_TYPES or status not in _STATUSES or strength not in _STRENGTHS:
                raise TypeScriptAnalysisRefused("analyser edge holds an unknown classification")
            target, descriptor = item.get("target"), item.get("descriptor")
            if status == "resolved":
                target_key: str | None = _text(target, 256)
                if descriptor is not None:
                    raise TypeScriptAnalysisRefused("analyser resolved edge carries a descriptor")
                endpoint = ""
            else:
                target_key = None
                if target is not None:
                    raise TypeScriptAnalysisRefused("analyser unresolved edge carries a target")
                endpoint = _text(descriptor, 1024)
            candidate = _edge(
                relationship=str(relationship),
                source_key=_text(item.get("source"), 256),
                target_key=target_key,
                descriptor=endpoint,
                status=str(status),
                strength=str(strength),
                source=binding(item),
            )
            edges.setdefault(candidate.edge_key, candidate)
        if not nodes:
            if edges:
                raise TypeScriptAnalysisRefused("analyser edges name no node")
            return finished(None)
        if len(nodes) + len(edges) > profile.max_candidates:
            raise TypeScriptAnalysisRefused("snapshot produces more candidates than the profile admits")
        batch = CodeAnalysisBatch(
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            snapshot_kind=snapshot_kind,
            producer_id=profile.producer_id,
            producer_version=profile.producer_version,
            nodes=tuple(nodes),
            edges=tuple(edges.values()),
            profile_digest=profile.digest,
        )
    except CodeAnalysisCandidateError as exc:
        # The answer named something the contract cannot hold -- a key, a duplicate, a
        # dangling edge. That is a malformed answer like any other, not a caller error.
        raise TypeScriptAnalysisRefused(f"analyser answer breaks the candidate contract: {exc}") from exc
    return finished(verify_batch_binding(connection, workspace_id=workspace_id, batch=batch))


def analysis_manifest(result: TypeScriptAnalysisResult) -> dict[str, object]:
    """One canonical, digestable evidence document describing what an analysis produced."""
    batch = result.batch
    return {
        "format": PROFILE_VERSION,
        "profile": result.profile.as_document(),
        "profile_digest": result.profile.digest,
        "request_digest": result.request_digest,
        "snapshot": None
        if result.verified is None or batch is None
        else {
            "repository_id": batch.repository_id,
            "snapshot_id": batch.snapshot_id,
            "snapshot_kind": batch.snapshot_kind,
            "stream_id": result.verified.stream_id,
            "sequence": result.verified.sequence,
            "manifest_digest": result.verified.manifest_digest,
            "manifest_evidence_id": result.verified.manifest_evidence_id,
        },
        "batch_digest": None if batch is None else batch.digest,
        "node_count": 0 if batch is None else len(batch.nodes),
        "edge_count": 0 if batch is None else len(batch.edges),
        "programs": [{"config": config, "files": list(files)} for config, files in result.programs],
        "lib_files": list(result.lib_files),
        "indexed_file_count": result.indexed_file_count,
        "excluded_file_count": result.excluded_file_count,
        "analysed_bytes": result.analysed_bytes,
        "reports": [report.as_document() for report in result.reports],
    }


__all__ = [
    "ANALYSER_DIGEST",
    "ANALYSER_PATH",
    "COVERAGE_OUTCOMES",
    "DEFAULT_TYPESCRIPT_PROFILE",
    "FILE_OUTCOMES",
    "LOCATOR_BASIS",
    "NODE_KINDS",
    "PACKAGED_TYPESCRIPT_ROOT",
    "PRODUCER_ID",
    "PRODUCER_VERSION",
    "PROFILE_VERSION",
    "RELATIONSHIP_TYPES",
    "TYPESCRIPT_DIGEST",
    "TYPESCRIPT_VERSION",
    "TypeScriptAnalyserProfile",
    "TypeScriptAnalysisRefused",
    "TypeScriptAnalysisResult",
    "TypeScriptFileReport",
    "TypeScriptRuntime",
    "analyse_typescript_snapshot",
    "analysis_manifest",
    "compiler_digest",
]
