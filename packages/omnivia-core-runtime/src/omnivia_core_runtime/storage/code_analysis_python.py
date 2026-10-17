"""A non-executing Python source analyser over one sealed captured snapshot.

What this is: the first real producer for the candidate contract in
:mod:`omnivia_core_runtime.storage.code_analysis_candidates`. It reads the paths and
content digests of one complete ``captured_v1`` snapshot, pulls the exact bytes those
digests name out of the workspace blob store, re-hashes them, parses them with the
standard library's own parser, and returns node and edge *candidates*. Nothing here
submits, approves, publishes, governs or writes a graph endpoint, and nothing here opens
a second database, server or API: the return value is values, and the caller decides.

**The analysed repository is never touched.** Not the checkout, not its configuration,
not its build. Every byte comes from `omnivia_engineering_snapshot_files` and the
content-addressed blob store, which is why a batch can be proven against a snapshot long
after the working tree it came from has moved on. The analyser imports no analysed
module, compiles no analysed source to code, runs no hook, reads no `pyproject.toml` by
executing it and resolves no name over a network. :func:`ast.parse` is a parser, not an
evaluator, and :func:`tokenize.detect_encoding` reads at most two lines looking for a
PEP 263 cookie. Those two are the whole analysis surface.

**Resolution is stated, never guessed.** An edge is `resolved` only when the analyser can
name the exact candidate in this same batch that the source refers to. Everything else
carries an explicit descriptor and one of `unresolved`, `ambiguous` or `external`: a name
no scope in the snapshot binds, a name some scope binds to a *value* -- a parameter, an
assignment, a loop or `with` or `except` or pattern binder, a lambda or comprehension
target -- a name two definitions in one scope bind, a callee that is not a name or an
attribute chain, a module outside the snapshot, a relative import that climbs past the
snapshot root, a wildcard import. There is no heuristic that promotes one of those to
`resolved`, because a graph that silently invents an edge is worse than one that says it
does not know. The single exception, and it is lexical rather than inferred, is a
method's receiver: the first parameter of a function written in a class body is that
class, until the body rebinds it.

**What fails closed, and what does not.** A snapshot that is absent, uncovered, not
`captured_v1` or not `complete`, a profile digest the caller did not expect, a stored path
that is not repository-relative, and a blob that is missing, unreadable, truncated or no
longer hashes to the digest the snapshot recorded all refuse the whole batch: each means
the batch would describe bytes nobody can point at. A limit the profile declares -- too
many files, too many bytes, too many candidates, too many file-level problems -- also
refuses the whole batch, because analysing a prefix of a repository produces a batch that
reads like a description of the snapshot while silently omitting the rest of it. So does a
`grammar_version` the running parser cannot actually be held to. Per-file trouble is the
opposite: an undecodable file, a syntactically invalid file, a file larger than the profile
admits, a file whose tree is bigger than the node budget and a file holding an identity the
contract cannot carry each get one bounded, source-located report, and every valid fact
from every other file survives. That outcome is all-or-nothing for the file it names: a
file that reports anything but `analysed` contributes no node and no edge at all, not even
the ones gathered before the trouble, and the rest of the snapshot stops resolving to it.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
import sys
import tokenize
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from io import BytesIO
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
    UnresolvedEndpoint,
    VerifiedCodeAnalysisBatch,
    verify_batch_binding,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.engineering_source import covered_snapshot, valid_path
from omnivia_core_runtime.workspace.blob_publication import (
    BlobPublicationRefused,
    _opened_blob,
    blob_path,
)

PRODUCER_ID: Final = "analyzer.python.stdlib"
PRODUCER_VERSION: Final = "1.0.0"
PROFILE_VERSION: Final = "python_analysis.v1"

#: How columns in every emitted locator are counted. CPython's `ast` reports
#: `col_offset` as a **UTF-8 byte** offset into the line, not a character offset, so a
#: locator on a line containing non-ASCII does not index that line's characters. Stating
#: the basis in the profile is what lets a consumer resolve a span against the exact
#: captured bytes instead of guessing which of the two conventions produced it.
LOCATOR_BASIS: Final = "utf8_byte_columns.v1"

#: One report per admitted file, always. `analysed` is the only outcome that contributes
#: candidates; the other five say precisely why a file contributed none.
FILE_OUTCOMES: Final = frozenset(
    {
        "analysed",
        "oversize",
        "decode_failed",
        "syntax_invalid",
        "ast_budget_exceeded",
        "identity_unsupported",
    }
)

#: The node kinds this producer emits. `module` is the file itself; the rest are the four
#: named lexical scopes Python's grammar has, split by whether a class body encloses them.
NODE_KINDS: Final = frozenset(
    {"module", "class", "function", "async_function", "method", "async_method"}
)

#: The relationship types this producer emits. `defines` is lexical containment and
#: definition in one edge: the parent scope is where the child is written and the thing
#: that introduces it.
RELATIONSHIP_TYPES: Final = frozenset({"defines", "imports", "calls"})

#: The newest grammar this interpreter's `ast.parse` can actually be held to. A profile
#: may declare an older one -- that is what `feature_version` is for -- but not a newer
#: one: `feature_version=(3, 12)` on a 3.11 parser is accepted silently and parses 3.11,
#: so a batch produced under it would claim a grammar nothing enforced.
_PARSER_GRAMMAR: Final = sys.version_info[:2]

_CHUNK: Final = 1024 * 1024
_MAX_DETAIL_CHARS: Final = 200
#: `UnresolvedEndpoint`'s own ceiling. A descriptor is composed from a logical path, and a
#: deeply nested definition under a long path can reach it, so every descriptor is bounded
#: on the way out rather than relying on the paths being short.
_MAX_DESCRIPTOR_CHARS: Final = 1024
#: How much of an edge's identity digest the key carries. 48 hex characters is 192 bits,
#: far inside the identifier budget and far past any collision this bounded a batch can
#: produce.
_EDGE_KEY_HEX: Final = 48
#: The ordinal separator for a logical path that more than one definition claims (two
#: `def f` in one scope, a `pkg.py` beside a `pkg/__init__.py`). The first keeps the bare
#: path; the n-th gets `+n`, which the identifier pattern admits.
_ORDINAL: Final = "+"
#: Separates a transient lookup scope -- a lambda's or a comprehension's -- from the named
#: scope that owns it. A NUL is deliberate: the contract's identifier pattern cannot admit
#: one, so a transient key can never be mistaken for, or collide with, a candidate key.
_TRANSIENT: Final = "\x00"
#: The expression scopes Python really has but never names, and so never keys.
_TRANSIENT_SCOPES: Final = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class PythonAnalysisRefused(RuntimeError):
    """The snapshot cannot be analysed inside the bounds this profile declares."""


@dataclass(frozen=True, slots=True)
class PythonAnalyserProfile:
    """The immutable, digestable identity of one analyser configuration.

    Two runs of the same `producer_version` over the same snapshot agree only if they also
    agree on the grammar, on which paths are source, and on every limit -- so all of that
    is in :attr:`digest`, and the digest travels on the batch. A caller that knows which
    profile it asked for passes `expected_profile_digest` and gets a refusal rather than a
    batch produced under some other configuration.
    """

    producer_id: str = PRODUCER_ID
    producer_version: str = PRODUCER_VERSION
    profile_version: str = PROFILE_VERSION
    #: The Python grammar `ast.parse` is held to, as `major.minor`. Passed through as
    #: `feature_version`, so a newer interpreter still refuses syntax this grammar has
    #: not got and the admitted language is a property of the profile rather than of
    #: whichever interpreter the service happens to be running. Bounded above by
    #: :data:`_PARSER_GRAMMAR`: a profile may ask for less than the running parser, never
    #: for more than it can give.
    grammar_version: str = "3.11"
    locator_basis: str = LOCATOR_BASIS
    include_suffixes: tuple[str, ...] = (".py", ".pyi")
    #: Path segments that take a whole subtree out of the analysis. Vendored,
    #: virtual-environment and cache trees are source files by suffix and are nobody's
    #: code: indexing them buries the repository's own definitions in its dependencies'.
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
    #: Why this ceiling and not a larger one: `verify_batch_binding` proves the batch by
    #: folding every distinct candidate path into one `IN (...)`, so the admitted file count
    #: is a host-parameter count. 500 stays inside SQLite's historic 999 ceiling -- below
    #: `sql_in.SQL_VARIABLE_CHUNK`, which is this repository's own stated safe fold -- on
    #: every build, without reaching into a shared read path to chunk it. A snapshot with
    #: more analysable files is refused, never analysed in part.
    #:
    #: It is rarely the bound that bites. Real modules run to roughly 250 candidates each,
    #: so `max_candidates` is exhausted at about 35-40 real files; this ceiling binds only
    #: on a repository of many small ones. Measured over this repository's own source --
    #: see the P7 evidence record.
    max_files: int = 500
    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 33_554_432
    max_ast_nodes_per_file: int = 200_000
    max_candidates: int = MAX_CANDIDATES
    #: How many admitted files may report something other than `analysed` before the batch
    #: is refused. A repository where most files do not parse is not a repository this
    #: producer understands, and a batch built from the remainder would read like one.
    max_file_reports: int = 250

    def __post_init__(self) -> None:
        for name in ("producer_id", "producer_version", "profile_version", "locator_basis"):
            value = getattr(self, name)
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise CodeAnalysisCandidateError(f"profile {name} is not a bounded label")
        feature = self.feature_version
        if feature is None:
            raise CodeAnalysisCandidateError("profile grammar_version is not `major.minor`")
        if feature[0] != 3 or feature > _PARSER_GRAMMAR:
            # `ast.parse` asserts its own `major == 3`, so a `2.7` profile reaches an
            # `AssertionError` rather than a refusal; and a minor past the running one is
            # accepted silently while parsing the grammar the interpreter actually has.
            # Neither is a configuration this producer can honour, so neither is admitted.
            raise CodeAnalysisCandidateError(
                "profile grammar_version is not a grammar this parser can provide"
            )
        if not self.include_suffixes or any(
            not suffix.startswith(".") or len(suffix) > 16 for suffix in self.include_suffixes
        ):
            raise CodeAnalysisCandidateError("profile include_suffixes are not bounded suffixes")
        limits = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_ast_nodes_per_file,
            self.max_candidates,
            self.max_file_reports,
        )
        if any(isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 for limit in limits):
            raise CodeAnalysisCandidateError("profile limits must be positive integers")
        if self.max_candidates > MAX_CANDIDATES or self.max_file_bytes > self.max_total_bytes:
            raise CodeAnalysisCandidateError("profile limits are outside the contract's bounds")

    @property
    def feature_version(self) -> tuple[int, int] | None:
        """`grammar_version` as `ast.parse`'s `feature_version`, or None if malformed."""
        major, dot, minor = self.grammar_version.partition(".")
        if dot != "." or not major.isdigit() or not minor.isdigit():
            return None
        return int(major), int(minor)

    def as_document(self) -> dict[str, object]:
        return {
            "profile_version": self.profile_version,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "grammar_version": self.grammar_version,
            "locator_basis": self.locator_basis,
            "include_suffixes": list(self.include_suffixes),
            "exclude_path_segments": list(self.exclude_path_segments),
            "exclude_path_prefixes": list(self.exclude_path_prefixes),
            "limits": {
                "max_files": self.max_files,
                "max_file_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes,
                "max_ast_nodes_per_file": self.max_ast_nodes_per_file,
                "max_candidates": self.max_candidates,
                "max_file_reports": self.max_file_reports,
            },
        }

    @property
    def digest(self) -> str:
        return content_digest(canonical_document(self.as_document()))

    def admits(self, path: str) -> bool:
        """Whether this profile treats one repository-relative path as analysable source."""
        if not path.endswith(self.include_suffixes):
            return False
        if any(segment in self.exclude_path_segments for segment in path.split("/")):
            return False
        return not (self.exclude_path_prefixes and path.startswith(self.exclude_path_prefixes))


DEFAULT_PYTHON_PROFILE: Final = PythonAnalyserProfile()


@dataclass(frozen=True, slots=True)
class PythonFileReport:
    """What became of one admitted file, with a source location where there is one."""

    relative_path: str
    content_digest: str
    outcome: str
    detail: str | None = None
    locator: CodeLocator | None = None

    def __post_init__(self) -> None:
        if self.outcome not in FILE_OUTCOMES:
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
class PythonAnalysisResult:
    """One analysis of one snapshot: proven candidates, plus what was left out and why."""

    profile: PythonAnalyserProfile
    #: None exactly when the snapshot holds no admitted Python source at all. The
    #: candidate contract refuses an empty batch on purpose -- a batch with no nodes
    #: cannot be bound to anything -- and "this snapshot is not a Python repository" is a
    #: real answer rather than a failure, so it is spelled as an absent batch beside a
    #: `reports` tuple that is also empty.
    verified: VerifiedCodeAnalysisBatch | None
    reports: tuple[PythonFileReport, ...]
    indexed_file_count: int
    excluded_file_count: int
    analysed_bytes: int

    @property
    def batch(self) -> CodeAnalysisBatch | None:
        return None if self.verified is None else self.verified.batch


@dataclass(frozen=True, slots=True)
class _Binding:
    """What one name in one lexical scope statically names, as a logical dotted path.

    `ambiguous` is a binding in its own right: a scope that binds one name twice knows
    the name and cannot say which definition it means, which is a different and more
    useful fact than not knowing the name at all. So is `dynamic`: the scope binds the
    name to a value, and a producer with no value flow cannot say which.
    """

    target: str
    ambiguous: bool = False
    dynamic: bool = False


#: The answer for a name some scope binds by anything other than a definition or an
#: import. One shared value: it carries no target and there is nothing to distinguish.
_DYNAMIC: Final = _Binding(target="", dynamic=True)


def _owner(scope_key: str) -> str:
    """The named scope an edge written in `scope_key` belongs to.

    A lambda and a comprehension are scopes for *lookup* and nothing else: they define
    nothing, so they are no candidate's key, and a call inside one is still written by the
    function or module that owns it. This is the only place the two meanings part.
    """
    return scope_key.partition(_TRANSIENT)[0]


def _bound_names(target: ast.expr) -> Iterator[str]:
    """Every bare name one assignment-like target binds.

    An attribute or a subscript target binds nothing -- `obj.x = 1` mutates an object and
    introduces no name -- so neither appears here, and a starred or nested target is
    unpacked rather than skipped.
    """
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _bound_names(element)


def _parameter_names(args: ast.arguments) -> tuple[str, ...]:
    """Every name one signature binds, positional-only through `**kwargs`."""
    named = (*args.posonlyargs, *args.args, *args.kwonlyargs)
    variadic = [argument for argument in (args.vararg, args.kwarg) if argument is not None]
    return tuple(argument.arg for argument in (*named, *variadic))


@dataclass(frozen=True, slots=True)
class _Reference:
    """One edge whose endpoint cannot be known until every file has been parsed.

    Deliberately not an AST node: trees are discarded as each file is parsed, so a
    snapshot's peak memory is its largest file rather than all of it at once. `parts`
    holds the dotted reference in source order; `mode` says how to start resolving it.

    `scope_key` is the scope the name is *looked up* in, which for a reference written
    inside a lambda or a comprehension is a transient scope and not a candidate key. The
    edge is attributed to :func:`_owner` of it: the named scope that owns the text.
    """

    scope_key: str
    source: SourceBinding
    relationship: str
    mode: str
    parts: tuple[str, ...] = ()
    descriptor: str = ""
    status: str = ""
    strength: str = ""


@dataclass(slots=True)
class _Scopes:
    """The snapshot-wide symbol tables pass one fills and pass two reads.

    `definitions` maps a logical dotted path to the candidate key that owns it, or to None
    when more than one definition claims it. `namespace` is every module path plus every
    package prefix of one, which is what separates "inside this snapshot but not found"
    from "outside this snapshot".
    """

    definitions: dict[str, str | None] = field(default_factory=dict)
    #: Logical paths a definition really occupies but no candidate can key, because the
    #: name is outside the contract's identifier bounds. Separate from an ambiguous entry:
    #: the source says exactly what it means and this batch cannot say it, which is an
    #: unresolved endpoint rather than an ambiguous one.
    unnameable: set[str] = field(default_factory=set)
    namespace: set[str] = field(default_factory=set)
    bindings: dict[tuple[str, str], _Binding] = field(default_factory=dict)
    #: `(scope, name)` pairs some binder that is not a definition or an import owns:
    #: a parameter, an assignment, an annotation, a loop or `with` or `except` alias, a
    #: walrus, a pattern capture, a `del`, a `global`. Python makes such a name local to
    #: the whole of its scope at compile time, so it shadows an outer definition of the
    #: same spelling wherever it appears -- and this producer, which follows no value,
    #: cannot say what it names.
    dynamic: set[tuple[str, str]] = field(default_factory=set)
    parents: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    wildcards: set[str] = field(default_factory=set)
    ordinals: dict[str, int] = field(default_factory=dict)

    def claim(self, logical: str) -> str:
        """The candidate key for one logical path, suffixed when it is already claimed.

        Called in a fixed order -- files by sorted path, definitions in source order -- so
        which redefinition gets the bare path is a property of the source, not of the run.
        """
        seen = self.ordinals.get(logical, 0) + 1
        self.ordinals[logical] = seen
        if seen == 1:
            self.definitions[logical] = logical
            return logical
        self.definitions[logical] = None
        return f"{logical}{_ORDINAL}{seen}"

    def shadow(self, scope_key: str, name: str) -> None:
        """Record that `scope_key` binds `name` to a value this producer cannot follow."""
        self.dynamic.add((scope_key, name))

    def bind(self, scope_key: str, name: str, target: str) -> None:
        existing = self.bindings.get((scope_key, name))
        if existing is None:
            self.bindings[(scope_key, name)] = _Binding(target)
        elif existing.target != target or existing.ambiguous:
            self.bindings[(scope_key, name)] = _Binding(existing.target, ambiguous=True)

    def lookup(self, scope_key: str, name: str) -> _Binding | None:
        """The innermost binding of `name` visible from `scope_key`, LEGB-style.

        A class body's names are visible inside that body and nowhere nested beneath it,
        which is Python's actual rule and not an approximation of it: a method does not
        see its class's attributes as bare names.

        :attr:`dynamic` is consulted first at every level, so a scope that binds a name to
        a value stops the walk instead of letting an outer definition answer for a local
        this producer cannot follow -- including when the same scope also defines that
        name, because a scope that both defines and rebinds a name statically names
        nothing.
        """
        key: str | None = scope_key
        innermost = True
        while key is not None:
            if innermost or self.kinds.get(key) != "class":
                if (key, name) in self.dynamic:
                    return _DYNAMIC
                binding = self.bindings.get((key, name))
                if binding is not None:
                    return binding
            innermost = False
            key = self.parents.get(key)
        return None

    def shadowed_by_wildcard(self, scope_key: str) -> bool:
        key: str | None = scope_key
        while key is not None:
            if key in self.wildcards:
                return True
            key = self.parents.get(key)
        return False

    def resolve_dotted(self, dotted: str) -> tuple[str | None, str, str, str]:
        """One dotted logical path as `(target_key, descriptor, status, strength)`."""
        if dotted in self.unnameable:
            return None, f"unsupported-identity:{dotted}", "unresolved", "syntactic"
        if dotted in self.definitions:
            key = self.definitions[dotted]
            if key is None:
                return None, f"ambiguous-definition:{dotted}", "ambiguous", "syntactic"
            return key, "", "resolved", "resolved_static"
        parts = dotted.split(".")
        if any(".".join(parts[:index]) in self.namespace for index in range(1, len(parts) + 1)):
            return None, f"unresolved-in-snapshot:{dotted}", "unresolved", "syntactic"
        return None, f"external-reference:{dotted}", "external", "syntactic"

    def resolve(self, reference: _Reference) -> tuple[str | None, str, str, str]:
        if reference.mode == "refused":
            return None, reference.descriptor, reference.status, reference.strength
        if reference.mode == "dotted":
            return self.resolve_dotted(".".join(reference.parts))
        name = reference.parts[0]
        binding = self.lookup(reference.scope_key, name)
        if binding is None:
            if self.shadowed_by_wildcard(reference.scope_key):
                return None, f"wildcard-shadowed-name:{name}", "ambiguous", "syntactic"
            return None, f"unbound-name:{name}", "unresolved", "syntactic"
        if binding.dynamic:
            return None, f"dynamic-binding:{name}", "unresolved", "inferred_dynamic"
        if binding.ambiguous:
            return None, f"ambiguous-name:{name}", "ambiguous", "syntactic"
        return self.resolve_dotted(".".join((binding.target, *reference.parts[1:])))


def _detail(text: str, limit: int = _MAX_DETAIL_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _coordinate(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return min(value + 1, MAX_LOCATOR_VALUE)


def _locator(node: ast.stmt | ast.expr) -> CodeLocator:
    """One node's exact span, clamped into the contract's coordinate bounds.

    Lines are already 1-based in `ast`; columns are 0-based UTF-8 byte offsets, so both
    column coordinates gain one. `end_column` therefore points one byte past the span's
    last byte, which is what makes a zero-width span impossible and the ordering invariant
    `(end_line, end_column) >= (start_line, start_column)` hold for every real node.
    """
    start_line = min(max(node.lineno, 1), MAX_LOCATOR_VALUE)
    start_column = _coordinate(node.col_offset, 1)
    end_line = min(max(node.end_lineno or start_line, start_line), MAX_LOCATOR_VALUE)
    end_column = _coordinate(node.end_col_offset, start_column)
    end_line, end_column = max((end_line, end_column), (start_line, start_column))
    return CodeLocator(
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
    )


def _module_locator(text: str) -> CodeLocator:
    """The whole file, as a span. Columns stay byte-based, matching every other locator."""
    lines = text.split("\n")
    end_line = min(max(len(lines), 1), MAX_LOCATOR_VALUE)
    end_column = min(len(lines[end_line - 1].encode("utf-8")) + 1, MAX_LOCATOR_VALUE)
    return CodeLocator(start_line=1, start_column=1, end_line=end_line, end_column=end_column)


def _module_path(relative_path: str) -> tuple[str, str]:
    """One file's `(module path, package path)`, both dotted and both derived only here.

    `pkg/mod.py` is the module `pkg.mod` inside the package `pkg`; `pkg/__init__.py` is
    the module `pkg` and *is* the package `pkg`, which is what makes `from . import x`
    mean `pkg.x` whether it is written in the package's `__init__` or in one of its
    siblings. A path segment that is not a Python identifier -- a
    directory with a dot or a dash in it -- still produces a stable, unique key for that
    file, and relative imports are resolved with this same function, so the convention is
    self-consistent even where it is not importable.
    """
    stem = relative_path.rpartition(".")[0] or relative_path
    segments = [segment for segment in stem.split("/") if segment]
    initialiser = bool(segments) and segments[-1] == "__init__"
    if initialiser:
        segments.pop()
    if not segments:
        return "__init__", ""
    module = ".".join(segments)
    return module, module if initialiser else ".".join(segments[:-1])


def _dotted(node: ast.expr) -> tuple[str, ...] | None:
    """A `Name`/`Attribute` chain flattened in source order, or None if it is neither."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _captured_index(
    connection: sqlite3.Connection, *, workspace_id: str, snapshot_id: str
) -> dict[str, str]:
    """Every path and content digest the sealed capture index records, and nothing else.

    A stored path that is not repository-relative refuses the batch rather than being
    skipped. The index is server-owned and sealed, so a path that fails
    :func:`valid_path` is not an input this producer may interpret -- it means the row
    this analysis would bind to is not the row the capture wrote.
    """
    rows = connection.execute(
        "SELECT path, content_digest FROM omnivia_engineering_snapshot_files "
        "WHERE workspace_id = ? AND snapshot_id = ?",
        (workspace_id, snapshot_id),
    ).fetchall()
    index: dict[str, str] = {}
    for raw_path, raw_digest in rows:
        path = str(raw_path)
        if not valid_path(path):
            raise CodeAnalysisBindingError("captured index holds a path outside the repository")
        index[path] = str(raw_digest)
    return index


def _captured_bytes(blobs_root: Path, digest: str, *, max_bytes: int) -> bytes | None:
    """The exact bytes one captured digest names, or None when the object is oversize.

    Opened with `O_NOFOLLOW` and re-checked through the descriptor, exactly as
    `blob_publication._verify` opens the same objects, so a symlink or a non-regular file
    standing where the object should be is refused rather than followed. The read is
    bounded by the initial `fstat` and again cumulatively, so an object that grows between
    the two cannot evade the ceiling.

    **The bytes are verified against the address they were read from**, independently of
    the snapshot that named it. A content-addressed store's whole claim is that the digest
    names the content; an object that no longer hashes to its own address is not the
    source this snapshot captured, whatever else it is, and parsing it would bind
    candidates to bytes that were never recorded. Every such failure refuses the batch --
    unlike the search projection, which may fall back to an identity-only document, there
    is no weaker correct answer here.
    """
    try:
        path = blob_path(blobs_root, digest)
        descriptor = _opened_blob(path)
    except BlobPublicationRefused as exc:
        raise CodeAnalysisBindingError("captured source blob cannot be opened") from exc
    try:
        status = os.fstat(descriptor)
        if status.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_CHUNK, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
    except OSError as exc:
        raise CodeAnalysisBindingError("captured source blob cannot be read") from exc
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if total != status.st_size or hashlib.sha256(content).hexdigest() != path.name:
        raise CodeAnalysisBindingError("captured source blob does not match its recorded digest")
    return content


def _decode(content: bytes) -> tuple[str, str | None, int]:
    """Captured bytes as source text, or the bounded reason they are not source text.

    The encoding comes from :func:`tokenize.detect_encoding`, which is the standard
    library's own PEP 263 reader: it honours a BOM and a `coding:` cookie in the first two
    lines and defaults to UTF-8. Using it rather than assuming UTF-8 is what lets a
    legitimately Latin-1-declared file be analysed instead of reported as undecodable.

    The third element is the one-based line the failure sits on, so the report can point at
    it. Failure text is composed here from fixed words plus an offset rather than
    interpolated from the exception, because a run over the same bytes has to produce the
    same report.
    """
    try:
        encoding, _consumed = tokenize.detect_encoding(BytesIO(content).readline)
    except SyntaxError:
        return "", "declared source encoding is invalid", 1
    except UnicodeDecodeError as exc:
        return "", f"undecodable byte in the encoding declaration at offset {exc.start}", 1
    try:
        return content.decode(encoding), None, 1
    except UnicodeDecodeError as exc:
        return (
            "",
            f"undecodable byte at offset {exc.start} for declared encoding {encoding}",
            content[: exc.start].count(b"\n") + 1,
        )
    except LookupError:
        return "", "declared source encoding is not available", 1


def _point(line: int, column: int = 1) -> CodeLocator:
    safe_line = min(max(line, 1), MAX_LOCATOR_VALUE)
    safe_column = min(max(column, 1), MAX_LOCATOR_VALUE)
    return CodeLocator(
        start_line=safe_line,
        start_column=safe_column,
        end_line=safe_line,
        end_column=safe_column,
    )


def _node_kind(node: ast.AST, *, parent_kind: str) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    inside_class = parent_kind == "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_method" if inside_class else "async_function"
    return "method" if inside_class else "function"


def _receiver_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The first positional parameter's name, which is how a method names its own class.

    Taken from the signature rather than assumed to be `self`: `cls` on a classmethod and
    any other spelling resolve the same way, and a function with no positional parameters
    binds nothing. This is the one place an attribute call becomes resolvable, and it is
    lexical -- the parameter belongs to a function written inside a class body -- not a
    guess about what the value will be at runtime.
    """
    positional = [*node.args.posonlyargs, *node.args.args]
    return positional[0].arg if positional else None


class _FileAnalyser:
    """One file's definitions, bindings and deferred references. Parses, never evaluates."""

    def __init__(
        self,
        scopes: _Scopes,
        *,
        relative_path: str,
        content_digest_value: str,
        module_path: str,
        package_path: str,
    ) -> None:
        self._scopes = scopes
        self._path = relative_path
        self._digest = content_digest_value
        self._module_path = module_path
        self._package_path = package_path
        self.nodes: list[CodeNodeCandidate] = []
        self.references: list[_Reference] = []
        self.containment: list[tuple[str, str, SourceBinding]] = []
        self.unsupported: list[tuple[str, CodeLocator]] = []
        #: Every logical path this file claimed, nameable or not. A file the contract
        #: cannot key is dropped whole, and these are what the rest of the snapshot must
        #: then stop resolving to.
        self.claimed: list[str] = []

    def _binding(self, locator: CodeLocator) -> SourceBinding:
        return SourceBinding(
            relative_path=self._path, content_digest=self._digest, locator=locator
        )

    def _define(
        self,
        logical: str,
        *,
        kind: str,
        display_name: str,
        locator: CodeLocator,
        parent: str | None,
    ) -> str | None:
        """Record one definition, or report the identity the contract cannot carry.

        The contract's identifier pattern admits Unicode word characters, so a `café` or a
        `日本語` definition keys by its own name. A name that still falls outside it -- an
        identifier NFKC leaves decomposed, or a logical path past the 256-character budget
        -- is reported rather than mangled into a key that no longer names the source.
        """
        key = self._scopes.claim(logical)
        self.claimed.append(logical)
        try:
            candidate = CodeNodeCandidate(
                semantic_key=key, kind=kind, display_name=display_name, source=self._binding(locator)
            )
        except CodeAnalysisCandidateError:
            self.unsupported.append((logical, locator))
            self._scopes.unnameable.add(logical)
            return None
        self.nodes.append(candidate)
        self._scopes.kinds[key] = kind
        if parent is not None:
            self._scopes.parents[key] = parent
            self.containment.append((parent, key, candidate.source))
        return key

    def analyse(self, tree: ast.Module, *, text: str) -> None:
        module_key = self._define(
            self._module_path,
            kind="module",
            display_name=self._path,
            locator=_module_locator(text),
            parent=None,
        )
        if module_key is None:
            return
        self._scopes.namespace.add(self._module_path)
        segments = self._module_path.split(".")
        for index in range(1, len(segments)):
            self._scopes.namespace.add(".".join(segments[:index]))
        self._descend(tree, scope_key=module_key, logical=self._module_path, kind="module")

    def _descend(self, node: ast.AST, *, scope_key: str, logical: str, kind: str) -> None:
        """Dispatch every child of `node` in this scope."""
        for child in ast.iter_child_nodes(node):
            self._dispatch(child, scope_key=scope_key, logical=logical, kind=kind)

    def _dispatch(self, node: ast.AST, *, scope_key: str, logical: str, kind: str) -> None:
        """One node, decided in this scope, then descended into.

        Every walk goes through here, including the one over a definition's own body --
        a body statement handed straight to :meth:`_descend` would have had its children
        examined and its own type never looked at.
        """
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            self._descend_definition(node, scope_key=scope_key, logical=logical, kind=kind)
            return
        if isinstance(node, _TRANSIENT_SCOPES):
            self._descend_transient(node, scope_key=scope_key, logical=logical, kind=kind)
            return
        if isinstance(node, ast.Import | ast.ImportFrom):
            self._record_import(node, scope_key=scope_key)
        elif isinstance(node, ast.Call):
            self._record_call(node, scope_key=scope_key)
        else:
            self._record_binders(node, scope_key=scope_key)
        self._descend(node, scope_key=scope_key, logical=logical, kind=kind)

    def _descend_definition(
        self,
        child: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        scope_key: str,
        logical: str,
        kind: str,
    ) -> None:
        """One nested scope, with its header read in the scope that encloses it.

        Decorators, base classes, default arguments and annotations are written inside the
        child but evaluate in the parent, so their calls belong to the parent. Only the
        body descends into the new scope. That is what makes a decorator's call an edge
        from the enclosing module rather than from the function it decorates.
        """
        enclosing = {"scope_key": scope_key, "logical": logical, "kind": kind}
        for decorator in child.decorator_list:
            self._dispatch(decorator, **enclosing)
        if isinstance(child, ast.ClassDef):
            for header in (*child.bases, *child.keywords):
                self._dispatch(header, **enclosing)
        else:
            self._dispatch(child.args, **enclosing)
            if child.returns is not None:
                self._dispatch(child.returns, **enclosing)
        child_logical = f"{logical}.{child.name}"
        child_kind = _node_kind(child, parent_kind=kind)
        child_key = self._define(
            child_logical,
            kind=child_kind,
            display_name=child.name,
            locator=_locator(child),
            parent=scope_key,
        )
        self._scopes.bind(scope_key, child.name, child_logical)
        if child_key is None:
            return
        if not isinstance(child, ast.ClassDef):
            # The receiver of a method written in a class body is the one parameter whose
            # value this producer can name lexically, so it keeps a static binding. Every
            # other parameter is a value, and a call through one is dynamic. If the body
            # then rebinds the receiver, `_record_binders` marks it dynamic too and the
            # dynamic table wins -- `self` after `self = other` is not this class.
            receiver = _receiver_name(child) if kind == "class" else None
            if receiver is not None:
                self._scopes.bind(child_key, receiver, logical)
            for name in _parameter_names(child.args):
                if name != receiver:
                    self._scopes.shadow(child_key, name)
        for statement in child.body:
            self._dispatch(statement, scope_key=child_key, logical=child_logical, kind=child_kind)

    def _descend_transient(
        self,
        node: ast.Lambda | ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        *,
        scope_key: str,
        logical: str,
        kind: str,
    ) -> None:
        """A lambda or a comprehension: a real Python scope that is never a named one.

        It binds names -- parameters, comprehension targets -- and those bind for the
        whole of it, so walking its body in the enclosing scope would resolve
        `[g() for g in generators]` to whichever outer `def g` happened to share the
        spelling. It defines nothing, so it is no candidate's key: it gets a transient
        lookup scope parented to this one, and the edges written inside it are attributed
        through :func:`_owner` to the named scope that owns them.
        """
        transient = f"{scope_key}{_TRANSIENT}{node.lineno}:{node.col_offset}"
        self._scopes.parents[transient] = scope_key
        self._scopes.kinds[transient] = "transient"
        inner = {"scope_key": transient, "logical": logical, "kind": kind}
        if isinstance(node, ast.Lambda):
            # Defaults are written inside the lambda and evaluate outside it, exactly as a
            # `def`'s do, so they stay in the enclosing scope.
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self._dispatch(default, scope_key=scope_key, logical=logical, kind=kind)
            for name in _parameter_names(node.args):
                self._scopes.shadow(transient, name)
            self._dispatch(node.body, **inner)
            return
        for generator in node.generators:
            for name in _bound_names(generator.target):
                self._scopes.shadow(transient, name)
        for child in ast.iter_child_nodes(node):
            self._dispatch(child, **inner)

    def _record_binders(self, node: ast.AST, *, scope_key: str) -> None:
        """Every name this node binds by something that is not a definition or an import.

        Python decides at compile time that a name assigned anywhere in a scope is local
        to the whole of that scope, so a call through such a name is never the outer
        definition it shares a spelling with -- whatever order the two appear in, which is
        why this records into a table the second pass reads rather than deciding in place.
        What the name holds would need value flow this producer deliberately has not got,
        so it is recorded as bound-and-unknown and every call through it says exactly that.
        """
        shadow = self._scopes.shadow
        if isinstance(node, ast.Assign | ast.Delete):
            targets: tuple[ast.expr, ...] = tuple(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.For | ast.AsyncFor):
            targets = (node.target,)
        elif isinstance(node, ast.With | ast.AsyncWith):
            targets = tuple(item.optional_vars for item in node.items if item.optional_vars)
        elif isinstance(node, ast.NamedExpr):
            # A walrus inside a comprehension binds in the scope that *contains* the
            # comprehension, so the owner is marked. For a walrus anywhere else the owner
            # is the scope itself, and marking the enclosing named scope is in every case
            # at least as conservative as marking the transient one.
            shadow(_owner(scope_key), node.target.id)
            return
        elif isinstance(node, ast.ExceptHandler):
            if node.name is not None:
                shadow(scope_key, node.name)
            return
        elif isinstance(node, ast.Global | ast.Nonlocal):
            # Not followed to the scope they name: a declared name is a name this scope
            # assigns somewhere, and an unresolved call is the honest answer.
            for declared in node.names:
                shadow(scope_key, declared)
            return
        elif isinstance(node, ast.match_case):
            # `case Point(x=a) | [b, *rest]`: a capture, a star and a mapping rest are all
            # plain names the pattern binds, wherever they sit in it.
            for element in ast.walk(node.pattern):
                captured = getattr(element, "rest", None) or getattr(element, "name", None)
                if isinstance(captured, str):
                    shadow(scope_key, captured)
            return
        else:
            return
        for target in targets:
            for name in _bound_names(target):
                shadow(scope_key, name)

    def _refused(
        self,
        *,
        scope_key: str,
        node: ast.stmt | ast.expr,
        relationship: str,
        descriptor: str,
        status: str,
        strength: str,
    ) -> None:
        self.references.append(
            _Reference(
                scope_key=scope_key,
                source=self._binding(_locator(node)),
                relationship=relationship,
                mode="refused",
                descriptor=_detail(descriptor),
                status=status,
                strength=strength,
            )
        )

    def _deferred(
        self,
        *,
        scope_key: str,
        node: ast.stmt | ast.expr,
        relationship: str,
        mode: str,
        parts: tuple[str, ...],
    ) -> None:
        self.references.append(
            _Reference(
                scope_key=scope_key,
                source=self._binding(_locator(node)),
                relationship=relationship,
                mode=mode,
                parts=parts,
            )
        )

    def _resolved_package(self, level: int) -> str | None:
        """The package a relative import's `level` dots name, or None past the root.

        One dot is the module's own package; each further dot drops one component. A level
        that climbs past the snapshot root resolves to nothing, which is a refusal rather
        than a silent fall back to the top of the tree.
        """
        if level < 1:
            return None
        segments = self._package_path.split(".") if self._package_path else []
        if level - 1 > len(segments):
            return None
        kept = segments[: len(segments) - (level - 1)]
        return ".".join(kept)

    def _record_import(self, node: ast.Import | ast.ImportFrom, *, scope_key: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = tuple(alias.name.split("."))
                # `import a.b` binds the top name `a`; `import a.b as c` binds `c` to the
                # full path. Both are what Python does, and the distinction is what makes
                # `a.b.f()` and `c.f()` resolve to the same definition.
                if alias.asname is None:
                    self._scopes.bind(scope_key, parts[0], parts[0])
                else:
                    self._scopes.bind(scope_key, alias.asname, alias.name)
                self._deferred(
                    scope_key=scope_key,
                    node=node,
                    relationship="imports",
                    mode="dotted",
                    parts=parts,
                )
            return
        base = self._resolved_package(node.level) if node.level else node.module
        if base is None:
            prefix = "." * node.level + (f"{node.module}." if node.module else "")
            for alias in node.names:
                self._refused(
                    scope_key=scope_key,
                    node=node,
                    relationship="imports",
                    descriptor=f"relative-import-beyond-root:{prefix}{alias.name}",
                    status="unresolved",
                    strength="syntactic",
                )
            return
        if node.level and node.module:
            base = f"{base}.{node.module}" if base else node.module
        for alias in node.names:
            if alias.name == "*":
                self._scopes.wildcards.add(scope_key)
                self._refused(
                    scope_key=scope_key,
                    node=node,
                    relationship="imports",
                    descriptor=f"wildcard-import:{base or '.'}",
                    status="unresolved",
                    strength="syntactic",
                )
                continue
            target = f"{base}.{alias.name}" if base else alias.name
            self._scopes.bind(scope_key, alias.asname or alias.name, target)
            self._deferred(
                scope_key=scope_key,
                node=node,
                relationship="imports",
                mode="dotted",
                parts=tuple(target.split(".")),
            )

    def _record_call(self, node: ast.Call, *, scope_key: str) -> None:
        """One call site, as an edge whose endpoint pass two resolves or labels.

        A bare name defers to the scope chain. An attribute chain defers to the chain's
        head, which the same lookup resolves -- so `c.f()` after `import a.b as c` and
        `self.m()` inside a method both become dotted logical paths without any
        value-flow reasoning. Anything else is labelled by the construct that produced it
        and is never resolved: a call through a subscript, a returned callable, a lambda
        or a `getattr` is dynamic, and the batch says so.
        """
        parts = _dotted(node.func)
        if parts is None:
            construct = type(node.func).__name__
            if isinstance(node.func, ast.Attribute):
                construct = f"{construct}.{node.func.attr}"
            self._refused(
                scope_key=scope_key,
                node=node,
                relationship="calls",
                descriptor=f"dynamic-call:{construct}",
                status="unresolved",
                strength="inferred_dynamic",
            )
            return
        self._deferred(
            scope_key=scope_key, node=node, relationship="calls", mode="name", parts=parts
        )


def _edge_key(
    *, relationship: str, source_key: str, target: str, source: SourceBinding
) -> str:
    document = {
        "relationship_type": relationship,
        "source_key": source_key,
        "target": target,
        "source": source.as_document(),
    }
    digest = hashlib.sha256(canonical_document(document).encode("utf-8")).hexdigest()
    return f"edge-{digest[:_EDGE_KEY_HEX]}"


def _edge(
    *,
    relationship: str,
    source_key: str,
    target_key: str | None,
    descriptor: str,
    status: str,
    strength: str,
    source: SourceBinding,
) -> CodeEdgeCandidate:
    endpoint = _detail(descriptor, _MAX_DESCRIPTOR_CHARS)
    return CodeEdgeCandidate(
        edge_key=_edge_key(
            relationship=relationship,
            source_key=source_key,
            target=target_key or endpoint,
            source=source,
        ),
        source_key=source_key,
        target_key=target_key,
        unresolved_target=None if status == "resolved" else UnresolvedEndpoint(endpoint),
        relationship_type=relationship,
        evidence_strength=strength,
        resolution_status=status,
        source=source,
    )


def _parsed(
    text: str, *, relative_path: str, profile: PythonAnalyserProfile
) -> tuple[ast.Module | None, str | None, CodeLocator | None]:
    """One file's tree, or the bounded reason it has none.

    `ast.parse` builds a tree; it does not import, execute, compile to code or consult the
    analysed project's configuration. `feature_version` holds the admitted grammar to the
    profile's, so the same bytes are accepted or refused the same way on a newer
    interpreter. The three failures are a syntax error, a `ValueError` (a NUL byte in the
    source is the usual one) and a `RecursionError` from a tree deeper than this
    interpreter will walk -- each one file's problem, never the batch's.
    """
    try:
        return ast.parse(text, filename=relative_path, feature_version=profile.feature_version), None, None
    except SyntaxError as exc:
        line = exc.lineno if isinstance(exc.lineno, int) else 1
        column = exc.offset if isinstance(exc.offset, int) else 1
        return None, "source is not valid Python for the profile's grammar", _point(line, column)
    except ValueError:
        return None, "source cannot be parsed as Python text", _point(1)
    except RecursionError:
        return None, "source nesting exceeds the analyser's parse depth", _point(1)


def analyse_python_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    snapshot_id: str,
    blobs_root: Path,
    profile: PythonAnalyserProfile = DEFAULT_PYTHON_PROFILE,
    expected_profile_digest: str | None = None,
    heartbeat: Callable[[], object] | None = None,
) -> PythonAnalysisResult:
    """Analyse one sealed captured snapshot's Python source into proven candidates.

    `heartbeat` is called once per admitted file, before that file's blob is opened, which
    is the granularity the capture writer's own `renew_lease` uses. It is also the
    cancellation point: a `heartbeat` that raises aborts the analysis where it stands, and
    because nothing here writes, an aborted analysis leaves nothing behind.

    The returned batch is proven by :func:`verify_batch_binding` before it is handed back,
    so a caller never holds an unproven batch: every path and digest in it is in the same
    sealed manifest, and the snapshot behind it is one complete `captured_v1` snapshot of
    the repository named.
    """
    if expected_profile_digest is not None and expected_profile_digest != profile.digest:
        raise CodeAnalysisBindingError("analyser profile digest is not the expected profile")
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

    index = _captured_index(connection, workspace_id=workspace_id, snapshot_id=snapshot_id)
    admitted = sorted(path for path in index if profile.admits(path))
    if len(admitted) > profile.max_files:
        raise PythonAnalysisRefused("snapshot holds more analysable files than the profile admits")

    scopes = _Scopes()
    reports: list[PythonFileReport] = []
    analysed: list[_FileAnalyser] = []
    analysed_bytes = 0
    for relative_path in admitted:
        if heartbeat is not None:
            heartbeat()
        digest = index[relative_path]
        content = _captured_bytes(blobs_root, digest, max_bytes=profile.max_file_bytes)
        if content is None:
            reports.append(
                PythonFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="oversize",
                    detail=f"captured object exceeds {profile.max_file_bytes} bytes",
                )
            )
            continue
        analysed_bytes += len(content)
        if analysed_bytes > profile.max_total_bytes:
            raise PythonAnalysisRefused("snapshot source exceeds the profile's total byte budget")
        text, decode_failure, failure_line = _decode(content)
        if decode_failure is not None:
            reports.append(
                PythonFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="decode_failed",
                    detail=_detail(decode_failure),
                    locator=_point(failure_line),
                )
            )
            continue
        tree, parse_failure, parse_locator = _parsed(
            text, relative_path=relative_path, profile=profile
        )
        if tree is None or parse_failure is not None:
            reports.append(
                PythonFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="syntax_invalid",
                    detail=_detail(parse_failure or "source cannot be parsed as Python text"),
                    locator=parse_locator,
                )
            )
            continue
        node_count = sum(1 for _ in ast.walk(tree))
        if node_count > profile.max_ast_nodes_per_file:
            reports.append(
                PythonFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="ast_budget_exceeded",
                    detail=f"tree holds {node_count} nodes, above {profile.max_ast_nodes_per_file}",
                )
            )
            continue
        module_path, package_path = _module_path(relative_path)
        analyser = _FileAnalyser(
            scopes,
            relative_path=relative_path,
            content_digest_value=digest,
            module_path=module_path,
            package_path=package_path,
        )
        analyser.analyse(tree, text=text)
        if analyser.unsupported:
            # All or nothing, per file. The analyser is not kept, so no node, no
            # containment edge and no reference from this file reaches the batch --
            # including the ones parsed before the name that broke, which would otherwise
            # describe a file the report says contributed nothing. Every logical path it
            # claimed joins `unnameable` instead, so another file's import of one of them
            # states `unsupported-identity` rather than resolving to a node that is not
            # there.
            scopes.unnameable.update(analyser.claimed)
            logical, locator = analyser.unsupported[0]
            reports.append(
                PythonFileReport(
                    relative_path=relative_path,
                    content_digest=digest,
                    outcome="identity_unsupported",
                    detail=_detail(
                        f"{len(analyser.unsupported)} definition identities are outside the "
                        f"contract's identifier bounds, first `{logical}`"
                    ),
                    locator=locator,
                )
            )
            continue
        analysed.append(analyser)
        reports.append(
            PythonFileReport(
                relative_path=relative_path, content_digest=digest, outcome="analysed"
            )
        )

    problems = sum(1 for report in reports if report.outcome != "analysed")
    if problems > profile.max_file_reports:
        raise PythonAnalysisRefused("snapshot holds more unanalysable files than the profile admits")
    nodes = tuple(node for analyser in analysed for node in analyser.nodes)
    if not nodes:
        return PythonAnalysisResult(
            profile=profile,
            verified=None,
            reports=tuple(reports),
            indexed_file_count=len(index),
            excluded_file_count=len(index) - len(admitted),
            analysed_bytes=analysed_bytes,
        )

    edges: dict[str, CodeEdgeCandidate] = {}
    for analyser in analysed:
        for parent_key, child_key, source in analyser.containment:
            candidate = _edge(
                relationship="defines",
                source_key=parent_key,
                target_key=child_key,
                descriptor="",
                status="resolved",
                strength="syntactic",
                source=source,
            )
            edges.setdefault(candidate.edge_key, candidate)
        for reference in analyser.references:
            target_key, descriptor, status, strength = scopes.resolve(reference)
            candidate = _edge(
                relationship=reference.relationship,
                source_key=_owner(reference.scope_key),
                target_key=target_key,
                descriptor=descriptor,
                status=status,
                strength=strength,
                source=reference.source,
            )
            edges.setdefault(candidate.edge_key, candidate)

    if len(nodes) + len(edges) > profile.max_candidates:
        raise PythonAnalysisRefused("snapshot produces more candidates than the profile admits")
    batch = CodeAnalysisBatch(
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        snapshot_kind=str(kind_row[0]),
        producer_id=profile.producer_id,
        producer_version=profile.producer_version,
        nodes=nodes,
        edges=tuple(edges.values()),
        profile_digest=profile.digest,
    )
    verified = verify_batch_binding(connection, workspace_id=workspace_id, batch=batch)
    return PythonAnalysisResult(
        profile=profile,
        verified=verified,
        reports=tuple(reports),
        indexed_file_count=len(index),
        excluded_file_count=len(index) - len(admitted),
        analysed_bytes=analysed_bytes,
    )


def analysis_manifest(result: PythonAnalysisResult) -> dict[str, object]:
    """One canonical, digestable document describing what an analysis produced.

    Not a candidate and not graph state: evidence. It names the profile, the snapshot, the
    batch digest and every file report, so two runs can be compared as documents rather
    than by walking two object graphs.
    """
    batch = result.batch
    return {
        "format": PROFILE_VERSION,
        "profile": result.profile.as_document(),
        "profile_digest": result.profile.digest,
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
        "indexed_file_count": result.indexed_file_count,
        "excluded_file_count": result.excluded_file_count,
        "analysed_bytes": result.analysed_bytes,
        "reports": [report.as_document() for report in result.reports],
    }


__all__ = [
    "DEFAULT_PYTHON_PROFILE",
    "FILE_OUTCOMES",
    "LOCATOR_BASIS",
    "NODE_KINDS",
    "PRODUCER_ID",
    "PRODUCER_VERSION",
    "PROFILE_VERSION",
    "RELATIONSHIP_TYPES",
    "PythonAnalyserProfile",
    "PythonAnalysisRefused",
    "PythonAnalysisResult",
    "PythonFileReport",
    "analyse_python_snapshot",
    "analysis_manifest",
]
