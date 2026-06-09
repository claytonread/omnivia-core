"""Normalization helpers for portable knowledge contracts."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from omnivia_memory.knowledge.models import (
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_OBJECT_KINDS,
)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_NON_SAFE = re.compile(r"[^a-z0-9:._-]+")
_REPEATED_DASH = re.compile(r"-{2,}")
_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$")


def normalize_identifier(value: str) -> str:
    """Return a deterministic, cross-platform-safe identifier."""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL_CHARS.sub("", normalized)
    normalized = _WHITESPACE.sub("-", normalized.strip().lower())
    normalized = _NON_SAFE.sub("-", normalized)
    normalized = _REPEATED_DASH.sub("-", normalized).strip("-._:")
    if not normalized:
        raise ValueError("identifier must contain safe characters")
    return normalized


def normalize_space_id(value: str) -> str:
    return normalize_identifier(value)


def normalize_object_id(value: str) -> str:
    return normalize_identifier(value)


def normalize_graph_node_id(value: str) -> str:
    return normalize_identifier(value)


def normalize_graph_edge_id(value: str) -> str:
    return normalize_identifier(value)


def normalize_label(value: str) -> str:
    """Normalize labels while preserving readable Unicode text."""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL_CHARS.sub("", normalized)
    normalized = _WHITESPACE.sub(" ", normalized.strip())
    if not normalized:
        raise ValueError("label must contain visible characters")
    return normalized


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize and deduplicate tags while preserving order."""

    seen: set[str] = set()
    normalized_tags: list[str] = []
    for tag in tags:
        normalized = normalize_identifier(tag)
        if normalized not in seen:
            seen.add(normalized)
            normalized_tags.append(normalized)
    return normalized_tags


def normalize_source_path(value: str) -> str:
    """Normalize source paths to safe relative POSIX paths."""

    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/").strip()
    if not normalized:
        raise ValueError("source path is required")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("source path must be relative")

    parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("source path must not traverse upward")

    result = "/".join(parts)
    if not result:
        raise ValueError("source path is required")
    return result


def normalize_extension_value(
    value: str,
    *,
    builtins: frozenset[str],
) -> str:
    """Normalize a built-in or safely namespaced extension value."""

    stripped = normalize_label(value).lower().replace(" ", "_")
    if stripped in builtins:
        return stripped
    if ":" not in stripped:
        raise ValueError("extension values must be built-in or namespaced")

    namespace, local_name = stripped.split(":", maxsplit=1)
    namespace = normalize_identifier(namespace)
    local_name = normalize_identifier(local_name)
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError("extension namespace is invalid")
    return f"{namespace}:{local_name}"


def normalize_object_kind(value: str) -> str:
    return normalize_extension_value(value, builtins=BUILTIN_OBJECT_KINDS)


def normalize_graph_node_kind(value: str) -> str:
    return normalize_extension_value(value, builtins=BUILTIN_GRAPH_NODE_KINDS)


def normalize_graph_relation(value: str) -> str:
    return normalize_extension_value(value, builtins=BUILTIN_GRAPH_RELATIONS)
