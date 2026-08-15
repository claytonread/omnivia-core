"""Tests for generate.py, imported by file path with no package markers."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

_DIR = Path(__file__).resolve().parents[1]
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _load(module_name: str, file_name: str) -> Any:
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _DIR / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


generate: Any = _load("q1_generate_under_test", "generate.py")


def _params(**overrides: Any) -> Any:
    values = {
        "root_seed": generate.ROOT_SEED,
        "contract_id": generate.CONTRACT_ID,
        "dataset_schema_id": generate.DATASET_SCHEMA_ID,
        "fixture_policy_id": generate.FIXTURE_POLICY_ID,
        "policy_set_id": generate.POLICY_SET_ID,
        "profile_id": generate.PROFILE_ID,
        "dimensions": generate.DIMENSIONS,
        "vector_dtype": generate.VECTOR_DTYPE,
        "metric": generate.METRIC,
        "normalization": generate.NORMALIZATION,
        "active_records": 5,
        "batch_size": 2,
        "k": 10,
        "operation": "evidence.search",
        "purpose": generate.PURPOSE,
        "scope": generate.SCOPE,
        "output_mode": generate.OUTPUT_MODE,
    }
    values.update(overrides)
    return generate.CampaignParameters(**values)


def test_generate_uses_no_language_rng() -> None:
    assert not hasattr(generate, "random")
    assert not hasattr(generate, "make_rng")


def test_counter_stream_is_deterministic_and_reproducible() -> None:
    first = generate.CounterStream("record", "evidence.search", "000000")
    second = generate.CounterStream("record", "evidence.search", "000000")
    draws_a = [first.next_unit_float() for _ in range(16)]
    draws_b = [second.next_unit_float() for _ in range(16)]
    assert draws_a == draws_b


def test_counter_stream_differs_by_label() -> None:
    a = generate.CounterStream("record", "evidence.search", "000000")
    b = generate.CounterStream("record", "memory.search", "000000")
    assert a.next_unit_float() != b.next_unit_float()


def test_counter_stream_draws_stay_in_bounds() -> None:
    stream = generate.CounterStream("record", "evidence.search", "000000")
    for _ in range(256):
        value = stream.next_unit_float()
        assert -1.0 <= value < 1.0


def test_counter_stream_rejects_empty_label() -> None:
    with pytest.raises(ValueError):
        generate.CounterStream("record", "")
    with pytest.raises(ValueError):
        generate.CounterStream()


def test_counter_stream_rejects_separator_injection() -> None:
    with pytest.raises(ValueError):
        generate.CounterStream("record", "evidence.search\x1f000000")


def test_counter_stream_rejects_counter_overflow() -> None:
    stream = generate.CounterStream("record", "evidence.search", "000000")
    stream._counter = generate.COUNTER_MAX + 1
    with pytest.raises(OverflowError):
        stream.next_unit_float()


def test_counter_stream_accepts_max_counter() -> None:
    stream = generate.CounterStream("record", "evidence.search", "000000")
    stream._counter = generate.COUNTER_MAX
    stream.next_unit_float()
    with pytest.raises(OverflowError):
        stream.next_unit_float()


def test_zero_padded_ordinal_format() -> None:
    assert generate.zero_padded_ordinal(0) == "0" * generate.INDEX_WIDTH
    assert generate.zero_padded_ordinal(7) == "0" * (generate.INDEX_WIDTH - 1) + "7"
    assert all(ch.isascii() and ch.isdigit() for ch in generate.zero_padded_ordinal(42))


def test_zero_padded_ordinal_rejects_negative() -> None:
    with pytest.raises(ValueError):
        generate.zero_padded_ordinal(-1)


def test_zero_padded_ordinal_rejects_overflow() -> None:
    with pytest.raises(ValueError):
        generate.zero_padded_ordinal(10**generate.INDEX_WIDTH)


@pytest.mark.parametrize("width", [0, -1, True])
def test_zero_padded_ordinal_rejects_invalid_width(width: int) -> None:
    with pytest.raises(ValueError):
        generate.zero_padded_ordinal(1, width)


@pytest.mark.parametrize(
    "active_records,batch_size,k,operation",
    [
        (0, 1, 1, "evidence.search"),
        (generate.MAX_ACTIVE_RECORDS + 1, 1, 1, "evidence.search"),
        (1, 0, 1, "evidence.search"),
        (1, generate.BATCH_SIZE_MAX + 1, 1, "evidence.search"),
        (1, 1, 2, "evidence.search"),
        (1, 1, True, "evidence.search"),
        (1, 1, 1, "unknown.op"),
    ],
)
def test_validate_campaign_params_rejects_out_of_bounds(
    active_records: int, batch_size: int, k: int, operation: str
) -> None:
    with pytest.raises(ValueError):
        generate.validate_campaign_params(
            _params(
                active_records=active_records,
                batch_size=batch_size,
                k=k,
                operation=operation,
            )
        )


def test_validate_campaign_params_accepts_bounds() -> None:
    generate.validate_campaign_params(
        _params(active_records=1, batch_size=1, k=1, operation="evidence.search")
    )
    generate.validate_campaign_params(
        _params(
            active_records=generate.MAX_ACTIVE_RECORDS,
            batch_size=generate.BATCH_SIZE_MAX,
            k=50,
            operation="knowledge.search",
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("root_seed", "wrong-seed"),
        ("contract_id", "wrong-contract"),
        ("dataset_schema_id", "wrong-schema"),
        ("fixture_policy_id", "wrong-policy-schema"),
        ("policy_set_id", "wrong-policy-set"),
        ("profile_id", "wrong-profile"),
        ("dimensions", 384),
        ("vector_dtype", "float64-le"),
        ("metric", "dot_product"),
        ("normalization", "none"),
        ("purpose", "background"),
        ("scope", "knowledge:read"),
        ("output_mode", "canonical"),
    ],
)
def test_every_frozen_campaign_parameter_is_explicit_and_validated(
    field: str, value: Any
) -> None:
    with pytest.raises(ValueError):
        generate.validate_campaign_params(_params(**{field: value}))


def test_campaign_parameters_have_no_defaults_or_unknown_fields() -> None:
    with pytest.raises(TypeError):
        generate.CampaignParameters()
    with pytest.raises(TypeError):
        generate.CampaignParameters(**{**_params().__dict__, "unknown": "value"})


def test_generate_record_is_reproducible_and_unit_length() -> None:
    params = _params(operation="memory.search")
    first = generate.generate_record(params, 3)
    second = generate.generate_record(params, 3)
    assert first == second
    norm = sum(v * v for v in first["vector"]) ** 0.5
    assert abs(norm - 1.0) < 1e-5
    assert first["noncanonical"] is True


def test_generate_record_differs_by_index() -> None:
    params = _params(operation="memory.search")
    a = generate.generate_record(params, 0)
    b = generate.generate_record(params, 1)
    assert a["vector"] != b["vector"]
    assert a["record_id"] != b["record_id"]


def test_generate_record_uses_frozen_metric_and_byte_order() -> None:
    record = generate.generate_record(_params(operation="memory.search"), 0)
    assert record["metric"] == "cosine_similarity"
    assert record["dimensions"] == 768
    assert record["vector_dtype"] == "float32-le"
    data = struct.pack(f"<{len(record['vector'])}f", *record["vector"])
    assert struct.unpack(f"<{len(record['vector'])}f", data) == tuple(record["vector"])


def test_first_vector_has_frozen_cross_platform_byte_digest() -> None:
    record = generate.generate_record(_params(active_records=1, batch_size=1, k=1), 0)
    data = generate.canonical.float32_le_bytes(record["vector"])
    assert hashlib.sha256(data).hexdigest() == (
        "ed010b38727a42ae6ae83bce664cd50260771baef634ebf175cd6fd023ecb5f4"
    )


def test_generate_record_id_is_fixed_width_ascii_ordinal() -> None:
    record = generate.generate_record(_params(operation="memory.search"), 7)
    assert record["record_id"] == "q1-record-memory-000007"
    assert record["record_id"].isascii()


def test_generate_campaign_id_is_sha256_reference() -> None:
    campaign = generate.generate_campaign(_params(active_records=2, batch_size=1, k=1))
    assert _SHA256_REF_RE.fullmatch(campaign["campaign_id"])


def test_generate_campaign_reproducible_end_to_end() -> None:
    params = _params()
    first = generate.generate_campaign(params)
    second = generate.generate_campaign(params)
    assert first == second
    assert len(first["records"]) == 5
