"""Tests for baseline normalisation and redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from baseline.determinism import (
    NormalizationError,
    Normalizer,
    diff_json,
    find_absolute_paths,
    format_differences,
)


def test_identifiers_get_stable_tokens_in_first_appearance_order() -> None:
    """The same identifier keeps the same token so records stay linked."""
    normalizer = Normalizer()
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"

    result = normalizer.normalize(
        {"a": first, "b": second, "nested": {"parent": first}}
    )

    assert result["a"] == "<uuid-0001>"
    assert result["b"] == "<uuid-0002>"
    assert result["nested"]["parent"] == "<uuid-0001>"
    assert normalizer.uuid_token_count == 2


def test_identifier_numbering_ignores_dictionary_insertion_order() -> None:
    """Traversal is key-sorted, so token numbers do not depend on build order."""
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"

    forward = Normalizer().normalize({"alpha": first, "beta": second})
    reversed_build = Normalizer().normalize({"beta": second, "alpha": first})

    assert forward == reversed_build


def test_timestamps_are_tokenised_and_preserved_values_are_not() -> None:
    normalizer = Normalizer(preserve_values=frozenset({"2026-06-07T00:00:00+00:00"}))

    result = normalizer.normalize(
        {"wall_clock": "2026-07-29T11:22:33.456789+00:00", "fixed": "2026-06-07T00:00:00+00:00"}
    )

    assert result["wall_clock"] == "<timestamp>"
    assert result["fixed"] == "2026-06-07T00:00:00+00:00"


def test_nested_path_roots_win_over_their_parent() -> None:
    """Longest-first replacement keeps the most specific token."""
    normalizer = Normalizer()
    normalizer.add_path_root("session-root", Path("/base"))
    normalizer.add_path_root("workspace-root", Path("/base/workspace"))

    result = normalizer.normalize(
        {"doc": "/base/workspace/docs/a.md", "db": "/base/session.db"}
    )

    assert result["doc"] == "<workspace-root>/docs/a.md"
    assert result["db"] == "<session-root>/session.db"


def test_scoped_redactions_report_only_the_current_artifact() -> None:
    normalizer = Normalizer()
    normalizer.add_path_root("session-root", Path("/base"))

    normalizer.start_scope()
    normalizer.normalize({"path": "/base/a.md"})
    first_scope = normalizer.scoped_redactions

    normalizer.start_scope()
    normalizer.normalize({"plain": "no redaction needed"})
    second_scope = normalizer.scoped_redactions

    assert first_scope == ["absolute-path"]
    assert second_scope == []
    assert normalizer.applied_redactions == ["absolute-path"]


def test_unsupported_values_are_rejected_rather_than_stringified() -> None:
    with pytest.raises(NormalizationError, match="cannot normalize value"):
        Normalizer().normalize({"bad": object()})


def test_find_absolute_paths_reports_leaks_with_their_location() -> None:
    leaks = find_absolute_paths({"a": ["ok", "/Users/someone/secret.db"], "b": "C:\\data\\x"})

    assert leaks == ["$.a[1]: /Users/someone/secret.db", "$.b: C:\\data\\x"]


def test_diff_reports_leaf_paths_not_whole_documents() -> None:
    differences = diff_json(
        {"nodes": [{"id": "a", "state": "ready"}], "count": 1},
        {"nodes": [{"id": "a", "state": "stale"}], "count": 2},
    )

    assert differences == [
        "$.count: expected 1, got 2",
        '$.nodes[0].state: expected "ready", got "stale"',
    ]


def test_difference_formatting_is_bounded() -> None:
    rendered = format_differences([f"item {index}" for index in range(30)], limit=5)

    assert rendered.count("\n") == 5
    assert "and 25 more difference(s)" in rendered
