from __future__ import annotations

import warnings

import pytest

from benchmarks.registry import get_registry


EXPECTED_SCENARIOS = {
    "create_memory",
    "retrieve_memory",
    "update_memory",
    "delete_memory",
    "keyword_search",
    "tag_filter",
    "source_filter",
    "graph_linking",
    "graph_traversal_1_hop",
    "graph_traversal_2_hop",
    "import_json",
    "export_json",
    "mixed_workload",
}


def test_all_expected_scenarios_are_registered() -> None:
    import benchmarks.scenarios  # noqa: F401

    assert EXPECTED_SCENARIOS.issubset(set(get_registry().names()))


@pytest.mark.parametrize("scenario_name", sorted(EXPECTED_SCENARIOS))
def test_scenario_smoke_runs_on_synthetic_temp_data(scenario_name: str) -> None:
    import benchmarks.scenarios  # noqa: F401

    scenario = get_registry().get(scenario_name)
    assert scenario is not None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = scenario.func(":memory:", 5)

    assert result["item_count"] > 0
    assert result["duration"] >= 0
    assert result["ops_per_second"] >= 0
    assert result.get("error") is None
