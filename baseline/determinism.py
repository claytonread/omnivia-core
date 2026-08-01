"""Deterministic normalisation and redaction for baseline artifacts.

Baseline artifacts are checked in and compared byte-for-byte, so every captured
value passes through :class:`Normalizer` first. It rewrites the three sources of
nondeterminism Core actually produces:

- random UUID4 identifiers become stable ``<uuid-0001>`` tokens, numbered in
  first-appearance order so referential integrity between records survives
  (a workspace id and the same id embedded in its storage path get the same
  token);
- wall-clock ISO-8601 timestamps become a single ``<timestamp>`` token;
- absolute filesystem paths become a registered ``<root>`` token plus a POSIX
  relative tail, which is also what keeps machine-specific paths out of tracked
  files.

Content hashes are deliberately *not* redacted. They are derived from fixture
text that is checked in alongside the artifact, so they stay stable and are the
strongest behavioural signal the baseline has.

Values that are already deterministic (for example the fixed timestamp used by
``omnivia_memory.memory_graph.fixtures``) can be passed through untouched via
``preserve_values`` so the baseline keeps recording them literally.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
"""Matches any RFC 4122 style identifier, which is what Core's models generate."""

TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
"""Matches the ISO-8601 timestamps Core writes via ``datetime.isoformat()``."""

TIMESTAMP_TOKEN = "<timestamp>"

REDACTION_ABSOLUTE_PATH = "absolute-path"
REDACTION_RANDOM_UUID = "random-uuid"
REDACTION_WALL_CLOCK_TIMESTAMP = "wall-clock-timestamp"

#: Every redaction rule this module can apply, in reporting order.
REDACTION_RULES = (
    REDACTION_ABSOLUTE_PATH,
    REDACTION_RANDOM_UUID,
    REDACTION_WALL_CLOCK_TIMESTAMP,
)


class NormalizationError(ValueError):
    """Raised when a value cannot be normalised into a public-safe artifact."""


@dataclass
class PathRoot:
    """A filesystem root that is rewritten to a stable token.

    ``token`` is the artifact-facing name (``workspace-root`` renders as
    ``<workspace-root>``). Both the raw and the resolved form of ``path`` are
    registered because macOS resolves ``/tmp`` and ``/var`` through
    ``/private``, so a captured value may carry either spelling.
    """

    token: str
    path: Path

    def spellings(self) -> list[str]:
        """Return every absolute string form this root can appear as."""
        raw = str(self.path)
        resolved = str(self.path.resolve())
        return [raw] if raw == resolved else [raw, resolved]


@dataclass
class Normalizer:
    """Rewrite captured values into deterministic, public-safe artifacts.

    A single instance is used for one artifact so UUID token numbering is
    scoped to that artifact and stays stable when other artifacts change.
    """

    path_roots: list[PathRoot] = field(default_factory=list)
    preserve_values: frozenset[str] = frozenset()
    _uuid_tokens: dict[str, str] = field(default_factory=dict, init=False)
    _applied: set[str] = field(default_factory=set, init=False)
    _scoped: set[str] = field(default_factory=set, init=False)

    def add_path_root(self, token: str, path: Path) -> None:
        """Register a filesystem root to rewrite as ``<token>``."""
        self.path_roots.append(PathRoot(token=token, path=path))

    def start_scope(self) -> None:
        """Begin recording redactions for a single artifact.

        One normalizer is shared across a capture session so identifier tokens
        stay consistent between artifacts. Scoping keeps each artifact's
        reported redactions honest about what that artifact actually needed.
        """
        self._scoped.clear()

    @property
    def applied_redactions(self) -> list[str]:
        """Redaction rule names applied since construction, in reporting order."""
        return [rule for rule in REDACTION_RULES if rule in self._applied]

    @property
    def scoped_redactions(self) -> list[str]:
        """Redaction rule names applied since the last :meth:`start_scope`."""
        return [rule for rule in REDACTION_RULES if rule in self._scoped]

    @property
    def uuid_token_count(self) -> int:
        """How many distinct random identifiers were tokenised."""
        return len(self._uuid_tokens)

    def normalize(self, value: Any) -> Any:
        """Return ``value`` with all nondeterministic content tokenised.

        Mappings are traversed in sorted-key order so UUID token numbering does
        not depend on dictionary insertion order.
        """
        if isinstance(value, Mapping):
            return {key: self.normalize(value[key]) for key in sorted(value, key=str)}
        if isinstance(value, (list, tuple)):
            return [self.normalize(item) for item in value]
        if isinstance(value, Path):
            return self.normalize_string(str(value))
        if isinstance(value, str):
            return self.normalize_string(value)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        raise NormalizationError(
            f"cannot normalize value of type {type(value).__name__!r}; "
            "convert it to a JSON-compatible primitive before capture"
        )

    def normalize_string(self, value: str) -> str:
        """Return ``value`` with paths, identifiers, and timestamps tokenised."""
        if value in self.preserve_values:
            return value
        result = self._redact_paths(value)
        result = self._tokenize_uuids(result)
        return self._redact_timestamps(result)

    def _redact_paths(self, value: str) -> str:
        # Longest spelling first so a nested root wins over its parent.
        spellings = sorted(
            (
                (spelling, root.token)
                for root in self.path_roots
                for spelling in root.spellings()
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        result = value
        for spelling, token in spellings:
            if spelling in result:
                result = result.replace(spelling, f"<{token}>")
                self._record(REDACTION_ABSOLUTE_PATH)
        return result

    def _tokenize_uuids(self, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            raw = match.group(0)
            token = self._uuid_tokens.get(raw)
            if token is None:
                token = f"<uuid-{len(self._uuid_tokens) + 1:04d}>"
                self._uuid_tokens[raw] = token
            self._record(REDACTION_RANDOM_UUID)
            return token

        return UUID_PATTERN.sub(replace, value)

    def _redact_timestamps(self, value: str) -> str:
        def replace(_match: re.Match[str]) -> str:
            self._record(REDACTION_WALL_CLOCK_TIMESTAMP)
            return TIMESTAMP_TOKEN

        return TIMESTAMP_PATTERN.sub(replace, value)

    def _record(self, rule: str) -> None:
        self._applied.add(rule)
        self._scoped.add(rule)


def find_absolute_paths(value: Any, *, path: str = "$") -> list[str]:
    """Return ``json-path: value`` entries that still look machine-specific.

    A tracked baseline artifact must never contain a machine-specific absolute
    path. This walks a normalised structure and reports any remaining string
    that starts with ``/`` (POSIX) or matches a Windows drive prefix, so the
    guard is a check rather than a convention.
    """
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            findings.extend(find_absolute_paths(value[key], path=f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(find_absolute_paths(item, path=f"{path}[{index}]"))
    elif isinstance(value, str) and (
        value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        findings.append(f"{path}: {value}")
    return findings


def dump_json(value: Any) -> str:
    """Serialise ``value`` to the one canonical JSON form baseline artifacts use."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_json(path: Path) -> Any:
    """Load a baseline artifact, with a precise message when it is missing."""
    if not path.exists():
        raise FileNotFoundError(
            f"baseline artifact {path} is missing; regenerate it with "
            "`python -m baseline capture`"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def write_artifact(path: Path, value: Any) -> None:
    """Write a baseline artifact in canonical JSON form."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json(value), encoding="utf-8")


def diff_json(expected: Any, actual: Any, *, path: str = "$") -> list[str]:
    """Return human-readable differences between two normalised structures.

    Baseline failures are only useful if they say exactly what moved, so this
    reports leaf-level paths rather than dumping both documents.
    """
    differences: list[str] = []
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        for key in sorted(set(expected) | set(actual), key=str):
            if key not in expected:
                differences.append(f"{path}.{key}: added ({_preview(actual[key])})")
            elif key not in actual:
                differences.append(f"{path}.{key}: removed ({_preview(expected[key])})")
            else:
                differences.extend(diff_json(expected[key], actual[key], path=f"{path}.{key}"))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            if index >= len(expected):
                differences.append(f"{path}[{index}]: added ({_preview(actual[index])})")
            elif index >= len(actual):
                differences.append(f"{path}[{index}]: removed ({_preview(expected[index])})")
            else:
                differences.extend(
                    diff_json(expected[index], actual[index], path=f"{path}[{index}]")
                )
        return differences
    if expected != actual:
        differences.append(f"{path}: expected {_preview(expected)}, got {_preview(actual)}")
    return differences


def format_differences(differences: Iterable[str], *, limit: int = 25) -> str:
    """Render a bounded, readable difference report."""
    items = list(differences)
    shown = items[:limit]
    lines = [f"  - {item}" for item in shown]
    if len(items) > limit:
        lines.append(f"  - ... and {len(items) - limit} more difference(s)")
    return "\n".join(lines)


def _preview(value: Any, *, limit: int = 120) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(text) > limit:
        return text[:limit] + "..."
    return text
