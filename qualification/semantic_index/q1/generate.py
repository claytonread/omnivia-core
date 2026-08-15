"""Deterministic, standard-library-only generator for the Q1 semantic-index campaign.

This module is the frozen, noncanonical synthetic campaign generator for the
Core semantic-index qualification. It produces parameterized vector-search
records reproducibly from a single root seed; it does not write fixtures,
define schemas, or assert acceptance criteria.

Loadable by file path with no package markers: `canonical.py` is loaded from
this file's own directory via `importlib`, not via a relative import, so
neither module needs an `__init__.py` to be imported on its own.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, cast


def _load_sibling(module_name: str, file_name: str) -> Any:
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).with_name(file_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {file_name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


canonical = _load_sibling("q1_canonical", "canonical.py")

# Frozen root seed. Every deterministic value this module produces is derived
# from this string; changing it changes the entire campaign.
ROOT_SEED = "omnivia-core-semantic-index-qualification-2026-08-02-v1"

# Frozen identities.
CONTRACT_ID = "omnivia.semantic-index.v1"
DATASET_SCHEMA_ID = "omnivia.semantic-index-dataset.v1"
FIXTURE_POLICY_ID = "omnivia.semantic-index.fixture-policy.v1"
POLICY_SET_ID = "q1.synthetic.current-canonical.v1"
PROFILE_ID = "q1.synthetic-768-cosine-v1"

# Frozen vector parameters.
DIMENSIONS = 768
VECTOR_DTYPE = "float32-le"
METRIC = "cosine_similarity"
NORMALIZATION = "core_l2_unit_v1"
MAX_ACTIVE_RECORDS = 10_000
BATCH_SIZE_MIN = 1
BATCH_SIZE_MAX = 1_000
K_VALUES = (1, 10, 50)

# Frozen operation lanes.
OPERATIONS = ("evidence.search", "memory.search", "knowledge.search")
PURPOSE = "user_initiated"
SCOPE = "memory:read"
NONCANONICAL = True
OUTPUT_MODE = "noncanonical"


# Fixed-width fields for the labelled SHA-256 counter stream: the per-draw
# counter, and any caller index folded into a stream label as a zero-padded
# ASCII ordinal.
COUNTER_WIDTH = 10
COUNTER_MAX = 10**COUNTER_WIDTH - 1
INDEX_WIDTH = 6

# The field separator joining the root seed, every label, and the zero-padded
# counter in a stream's preimage. Rejected inside any label so a label cannot
# be crafted to make two different (labels, counter) draws hash to the same
# preimage bytes.
_SEPARATOR = "\x1f"


class CounterStream:
    """Deterministic SHA-256 counter stream: no language RNG anywhere in this module.

    Each draw hashes `ROOT_SEED \x1f label \x1f ... \x1f zero-padded counter`
    with SHA-256 and advances the counter. The same root seed, labels, and
    draw order always reproduce byte-identical output; there is no seedable
    global state to leak between callers, unlike `random.Random`. Labels may
    not contain the separator (that would let a crafted label forge a
    different (labels, counter) split hashing to the same preimage bytes),
    and the counter is fixed-width: it fails closed instead of silently
    widening past `COUNTER_WIDTH` digits.
    """

    def __init__(self, *labels: str) -> None:
        if not labels or not all(isinstance(label, str) and label for label in labels):
            raise ValueError(
                f"CounterStream requires non-empty str labels, got {labels!r}"
            )
        if any(_SEPARATOR in label for label in labels):
            raise ValueError(
                f"CounterStream labels must not contain the separator: {labels!r}"
            )
        self._prefix = _SEPARATOR.join((ROOT_SEED, *labels))
        self._counter = 0

    def _next_digest(self) -> bytes:
        if self._counter > COUNTER_MAX:
            raise OverflowError(
                f"CounterStream counter exceeded {COUNTER_WIDTH}-digit width"
            )
        block = f"{self._prefix}{_SEPARATOR}{self._counter:0{COUNTER_WIDTH}d}".encode()
        self._counter += 1
        return hashlib.sha256(block).digest()

    def next_unit_float(self) -> float:
        """Next deterministic float, uniform over [-1.0, 1.0)."""
        fraction = int.from_bytes(self._next_digest()[:8], byteorder="big") / float(
            1 << 64
        )
        return fraction * 2.0 - 1.0


@dataclass(frozen=True)
class CampaignParameters:
    """Complete explicit Q1 campaign-defining parameter set; no defaults."""

    root_seed: str
    contract_id: str
    dataset_schema_id: str
    fixture_policy_id: str
    policy_set_id: str
    profile_id: str
    dimensions: int
    vector_dtype: str
    metric: str
    normalization: str
    active_records: int
    batch_size: int
    k: int
    operation: str
    purpose: str
    scope: str
    output_mode: str


def zero_padded_ordinal(index: int, width: int = INDEX_WIDTH) -> str:
    """Zero-padded ASCII ordinal for `index`, used to build ID/label components."""
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ValueError(f"width must be a positive int, got {width!r}")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError(
            f"zero_padded_ordinal requires a non-negative int, got {index!r}"
        )
    ordinal = str(index)
    if len(ordinal) > width:
        raise ValueError(f"index {index!r} does not fit in {width} zero-padded digits")
    return ordinal.zfill(width)


def validate_campaign_params(params: CampaignParameters) -> None:
    """Fail closed on any campaign parameter outside the frozen bounds."""
    if not isinstance(params, CampaignParameters):
        raise TypeError(f"params must be CampaignParameters, got {type(params)!r}")
    exact = {
        "root_seed": ROOT_SEED,
        "contract_id": CONTRACT_ID,
        "dataset_schema_id": DATASET_SCHEMA_ID,
        "fixture_policy_id": FIXTURE_POLICY_ID,
        "policy_set_id": POLICY_SET_ID,
        "profile_id": PROFILE_ID,
        "dimensions": DIMENSIONS,
        "vector_dtype": VECTOR_DTYPE,
        "metric": METRIC,
        "normalization": NORMALIZATION,
        "purpose": PURPOSE,
        "scope": SCOPE,
        "output_mode": OUTPUT_MODE,
    }
    for field, expected in exact.items():
        actual = getattr(params, field)
        if actual != expected or isinstance(actual, bool):
            raise ValueError(
                f"{field} must be the frozen value {expected!r}, got {actual!r}"
            )
    if not isinstance(params.active_records, int) or isinstance(
        params.active_records, bool
    ):
        raise TypeError(f"active_records must be an int, got {params.active_records!r}")
    if not (1 <= params.active_records <= MAX_ACTIVE_RECORDS):
        raise ValueError(
            f"active_records must be in [1, {MAX_ACTIVE_RECORDS}], "
            f"got {params.active_records!r}"
        )
    if not isinstance(params.batch_size, int) or isinstance(params.batch_size, bool):
        raise TypeError(f"batch_size must be an int, got {params.batch_size!r}")
    if not (BATCH_SIZE_MIN <= params.batch_size <= BATCH_SIZE_MAX):
        raise ValueError(
            f"batch_size must be in [{BATCH_SIZE_MIN}, {BATCH_SIZE_MAX}], "
            f"got {params.batch_size!r}"
        )
    if (
        not isinstance(params.k, int)
        or isinstance(params.k, bool)
        or params.k not in K_VALUES
    ):
        raise ValueError(f"k must be one of {K_VALUES}, got {params.k!r}")
    if params.operation not in OPERATIONS:
        raise ValueError(
            f"operation must be one of {OPERATIONS}, got {params.operation!r}"
        )


def normalize_l2_unit(vector: list[float]) -> list[float]:
    """Normalize per ``core_l2_unit_v1`` with deterministic decimal arithmetic."""
    rounded: list[float] = cast("list[float]", canonical.float32_le_round_trip(vector))
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_HALF_EVEN
        decimals = [Decimal.from_float(value) for value in rounded]
        norm_squared = sum((value * value for value in decimals), Decimal(0))
        if norm_squared.is_zero():
            raise ValueError("cannot L2-normalize a zero vector")
        norm = norm_squared.sqrt(context=context)
        unit = [float(value / norm) for value in decimals]
    return cast("list[float]", canonical.float32_le_round_trip(unit))


def generate_unit_vector(
    stream: CounterStream, dimensions: int = DIMENSIONS
) -> list[float]:
    """Generate a deterministic unit-length float32 vector from `stream`."""
    if (
        not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or dimensions <= 0
    ):
        raise ValueError(f"dimensions must be a positive int, got {dimensions!r}")
    raw = [stream.next_unit_float() for _ in range(dimensions)]
    return normalize_l2_unit(raw)


def external_record_id(operation: str, record_index: int) -> str:
    """Return the fixed-width external identity used for UTF-8 tie-breaking."""
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}, got {operation!r}")
    lane = operation.removesuffix(".search")
    return f"q1-record-{lane}-{zero_padded_ordinal(record_index)}"


def generate_record(params: CampaignParameters, record_index: int) -> dict[str, Any]:
    """Build one deterministic, noncanonical synthetic search record."""
    validate_campaign_params(params)
    if (
        not isinstance(record_index, int)
        or isinstance(record_index, bool)
        or record_index < 0
    ):
        raise ValueError(
            f"record_index must be a non-negative int, got {record_index!r}"
        )
    stream = CounterStream(
        "record", params.operation, zero_padded_ordinal(record_index)
    )
    vector = generate_unit_vector(stream, params.dimensions)
    record: dict[str, Any] = {
        "record_id": external_record_id(params.operation, record_index),
        "operation": params.operation,
        "purpose": params.purpose,
        "scope": params.scope,
        "record_index": record_index,
        "dimensions": params.dimensions,
        "vector_dtype": params.vector_dtype,
        "metric": params.metric,
        "normalization": params.normalization,
        "vector": vector,
        "noncanonical": NONCANONICAL,
    }
    return record


def generate_campaign(params: CampaignParameters) -> dict[str, Any]:
    """Build the deterministic, noncanonical synthetic campaign manifest."""
    validate_campaign_params(params)
    records = [generate_record(params, index) for index in range(params.active_records)]
    campaign: dict[str, Any] = {
        **asdict(params),
        "noncanonical": NONCANONICAL,
        "records": records,
    }
    campaign["campaign_id"] = canonical.canonical_sha256_ref(campaign)
    return campaign
