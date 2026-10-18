"""Bounded optional provider assessment for engineering relation candidates.

The deterministic discovery processor remains the source of candidates.  This
executor can add provider evidence when an embedder explicitly enables it.  It
stages an exact authorized input, closes the SQLite transaction, calls the
provider with a finite deadline, strictly validates the returned value, and then
appends a terminal reconciliation.  It owns no governance transition capability.
"""

from __future__ import annotations

import math
import queue
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.engineering_assessments import (
    ALLOWED_RELATIONS,
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    TOKENIZER_ID,
    RelationAssessmentInput,
    RelationAssessmentReconciliation,
    StagedRelationAssessment,
    append_assessment_result,
    build_authorized_assessment_input,
    canonical_input,
    content_digest,
    input_token_count,
    read_oldest_pending_assessment,
    stage_next_assessment,
)
from omnivia_core_runtime.storage.memory import random_identifier
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    local_owner_label_grant,
)

DEFAULT_PROMPT_VERSION: Final = "engineering.relation-assessment.prompt.v1"
DEFAULT_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_MAX_INPUT_BYTES: Final = 16_384
DEFAULT_MAX_INPUT_TOKENS: Final = 4_096
DEFAULT_MAXIMUM_CALLS: Final = 8
DEFAULT_MAXIMUM_CONCURRENCY: Final = 1
HARD_MAX_TIMEOUT_SECONDS: Final = 30.0
HARD_MAX_INPUT_BYTES: Final = 65_536
HARD_MAX_INPUT_TOKENS: Final = 16_384
HARD_MAXIMUM_CALLS: Final = 50
HARD_MAXIMUM_CONCURRENCY: Final = 1

_RESPONSE_KEYS: Final = frozenset(
    {
        "schema_version",
        "relation_candidate_id",
        "endpoint_a",
        "endpoint_b",
        "relation",
        "evidence_refs",
        "confidence",
    }
)
_ENDPOINT_KEYS: Final = frozenset(
    {"assembly_id", "record_id", "version", "content_digest"}
)


class RelationAssessmentProvider(Protocol):
    """A data-only provider seam with no storage or authority handle."""

    def __call__(
        self,
        request: RelationAssessmentInput,
        *,
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...


class RelationAssessmentProviderUnavailable(RuntimeError):
    """The configured provider route could not be reached."""


class RelationAssessmentResponseInvalid(ValueError):
    """A provider response failed the closed schema or semantic checks."""


class _ProviderCapacityExhausted(RuntimeError):
    """The prior timed-out provider call still owns the one execution slot."""


@dataclass(frozen=True, slots=True)
class RelationAssessmentPolicy:
    """Service-owned controls; the default instance keeps the feature disabled."""

    enabled: bool = False
    provider_id: str = "unconfigured"
    model_id: str = "unconfigured"
    prompt_version: str = DEFAULT_PROMPT_VERSION
    response_schema_version: str = RESPONSE_SCHEMA_VERSION
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    maximum_calls: int = DEFAULT_MAXIMUM_CALLS
    maximum_concurrency: int = DEFAULT_MAXIMUM_CONCURRENCY

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean")
        for label, value, maximum in (
            ("provider_id", self.provider_id, 128),
            ("model_id", self.model_id, 128),
            ("prompt_version", self.prompt_version, 64),
            ("response_schema_version", self.response_schema_version, 64),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{label} must be text")
            if not 1 <= len(value) <= maximum or "\x00" in value:
                raise ValueError(f"{label} is outside its bound")
        if self.response_schema_version != RESPONSE_SCHEMA_VERSION:
            raise ValueError("the response schema version is unsupported")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0 < float(self.timeout_seconds) <= HARD_MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is outside its finite bound")
        _bounded_integer(
            "max_input_bytes", self.max_input_bytes, HARD_MAX_INPUT_BYTES
        )
        _bounded_integer(
            "max_input_tokens", self.max_input_tokens, HARD_MAX_INPUT_TOKENS
        )
        _bounded_integer("maximum_calls", self.maximum_calls, HARD_MAXIMUM_CALLS)
        _bounded_integer(
            "maximum_concurrency",
            self.maximum_concurrency,
            HARD_MAXIMUM_CONCURRENCY,
        )

    @property
    def timeout_ms(self) -> int:
        return max(1, int(float(self.timeout_seconds) * 1000))

    @property
    def configuration_digest(self) -> str:
        document = to_canonical_json(
            {
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "prompt_version": self.prompt_version,
                "request_schema_version": REQUEST_SCHEMA_VERSION,
                "response_schema_version": self.response_schema_version,
                "tokenizer_id": TOKENIZER_ID,
                "timeout_ms": self.timeout_ms,
                "max_input_bytes": self.max_input_bytes,
                "max_input_tokens": self.max_input_tokens,
                "maximum_calls": self.maximum_calls,
                "maximum_concurrency": self.maximum_concurrency,
            }
        )
        return content_digest(document)


@dataclass(frozen=True, slots=True)
class ValidatedRelationAssessment:
    relation: str
    evidence_refs: tuple[str, ...]
    self_reported_confidence_ppm: int
    response_digest: str


def _bounded_integer(label: str, value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{label} is outside its bound")
    return value


def _endpoint_response(
    raw: object,
    *,
    expected_assembly_id: str,
    expected_record_id: str,
    expected_version: str,
    expected_digest: str,
) -> None:
    if not isinstance(raw, Mapping) or set(raw) != _ENDPOINT_KEYS:
        raise RelationAssessmentResponseInvalid("endpoint schema is invalid")
    expected = {
        "assembly_id": expected_assembly_id,
        "record_id": expected_record_id,
        "version": expected_version,
        "content_digest": expected_digest,
    }
    if any(not isinstance(raw[key], str) or raw[key] != value for key, value in expected.items()):
        raise RelationAssessmentResponseInvalid("an endpoint identity changed")


def validate_relation_assessment_response(
    request: RelationAssessmentInput,
    response: object,
) -> ValidatedRelationAssessment:
    """Validate an exact closed response and return only non-authoritative evidence."""

    if not isinstance(response, Mapping) or set(response) != _RESPONSE_KEYS:
        raise RelationAssessmentResponseInvalid("response schema is invalid")
    if response["schema_version"] != request.response_schema_version:
        raise RelationAssessmentResponseInvalid("response schema version changed")
    if response["relation_candidate_id"] != request.relation_candidate_id:
        raise RelationAssessmentResponseInvalid("relation candidate identity changed")
    _endpoint_response(
        response["endpoint_a"],
        expected_assembly_id=request.endpoint_a.assembly_id,
        expected_record_id=request.endpoint_a.record_id,
        expected_version=request.endpoint_a.version,
        expected_digest=request.endpoint_a.content_digest,
    )
    _endpoint_response(
        response["endpoint_b"],
        expected_assembly_id=request.endpoint_b.assembly_id,
        expected_record_id=request.endpoint_b.record_id,
        expected_version=request.endpoint_b.version,
        expected_digest=request.endpoint_b.content_digest,
    )
    relation = response["relation"]
    if not isinstance(relation, str) or relation not in ALLOWED_RELATIONS:
        raise RelationAssessmentResponseInvalid("relation verb is unsupported")

    raw_refs = response["evidence_refs"]
    if not isinstance(raw_refs, list) or not all(
        isinstance(item, str) for item in raw_refs
    ):
        raise RelationAssessmentResponseInvalid("evidence references are invalid")
    refs = tuple(raw_refs)
    if len(refs) != len(set(refs)) or not set(refs) <= set(
        request.allowed_evidence_refs
    ):
        raise RelationAssessmentResponseInvalid("an evidence reference was invented")

    raw_confidence = response["confidence"]
    if isinstance(raw_confidence, bool) or not isinstance(
        raw_confidence, (int, float)
    ):
        raise RelationAssessmentResponseInvalid("confidence is not numeric")
    try:
        finite = math.isfinite(float(raw_confidence))
        scaled = Decimal(str(raw_confidence)) * Decimal(1_000_000)
    except (OverflowError, ValueError, InvalidOperation):
        finite = False
        scaled = Decimal(-1)
    if (
        not finite
        or scaled < 0
        or scaled > 1_000_000
        or scaled != scaled.to_integral_value()
    ):
        raise RelationAssessmentResponseInvalid(
            "confidence must be finite, bounded, and exact to six decimal places"
        )

    canonical_response = to_canonical_json(dict(response))
    return ValidatedRelationAssessment(
        relation=relation,
        evidence_refs=tuple(sorted(refs)),
        self_reported_confidence_ppm=int(scaled),
        response_digest=content_digest(canonical_response),
    )


@dataclass(frozen=True, slots=True)
class EngineeringRelationAssessmentExecutor:
    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Clock
    policy: RelationAssessmentPolicy = RelationAssessmentPolicy()
    provider: RelationAssessmentProvider | None = None
    allocate_identifier: Callable[[str], str] = random_identifier
    _provider_slot: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(value=1),
        init=False,
        repr=False,
        compare=False,
    )

    def _now_us(self, *, floor: int = 1) -> int:
        return max(floor, int(self.clock.wall_time().timestamp() * 1_000_000))

    def _current_input(
        self, request: StagedRelationAssessment
    ) -> RelationAssessmentInput | None:
        return build_authorized_assessment_input(
            self.connection,
            workspace_id=self.workspace_id,
            assessment_request_id=request.assessment_request_id,
            relation_candidate_id=request.relation_candidate_id,
            prompt_version=request.prompt_version,
            response_schema_version=request.response_schema_version,
            resolution_instant_us=self._now_us(floor=request.requested_at_us),
            label_grant=local_owner_label_grant(
                principal_id=CONFIGURED_LOCAL_OWNER,
                workspace_id=self.workspace_id,
                granted_workspace=self.workspace_id,
            ),
        )

    def _terminal(
        self,
        request: StagedRelationAssessment,
        *,
        status: str,
        verdict: ValidatedRelationAssessment | None = None,
        failure_code: str | None = None,
    ) -> RelationAssessmentReconciliation:
        return append_assessment_result(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            request=request,
            status=status,
            assessed_relation=None if verdict is None else verdict.relation,
            evidence_refs=() if verdict is None else verdict.evidence_refs,
            self_reported_confidence_ppm=(
                None if verdict is None else verdict.self_reported_confidence_ppm
            ),
            response_digest=None if verdict is None else verdict.response_digest,
            failure_code=failure_code,
            allocate_identifier=self.allocate_identifier,
            occurred_at_us=self._now_us(floor=request.requested_at_us),
        )

    def _call_provider(self, input_value: RelationAssessmentInput) -> object:
        """Invoke the data-only provider with a caller-enforced hard deadline.

        Python cannot safely stop arbitrary embedder code.  A timed-out daemon
        call may therefore finish later, but it retains the executor's only
        provider slot until then.  Later maintenance passes defer their staged
        request instead of exceeding the declared live-concurrency ceiling.
        """

        provider = self.provider
        assert provider is not None
        if not self._provider_slot.acquire(blocking=False):
            raise _ProviderCapacityExhausted
        outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        slot = self._provider_slot
        timeout_seconds = self.policy.timeout_ms / 1000.0

        def invoke() -> None:
            try:
                value = provider(input_value, timeout_seconds=timeout_seconds)
            # Transport process-level interruption back to the service thread so
            # the staged request remains durable and restartable in the same way
            # as a direct provider call.
            except BaseException as error:  # noqa: BLE001
                outcome.put((False, error))
            else:
                outcome.put((True, value))
            finally:
                slot.release()

        threading.Thread(
            target=invoke,
            name="omnivia-engineering-relation-assessor",
            daemon=True,
        ).start()
        provider_outcome: tuple[bool, object] | None = None
        try:
            provider_outcome = outcome.get(timeout=timeout_seconds)
        except queue.Empty:
            pass
        if provider_outcome is None:
            raise TimeoutError("relation assessment provider deadline exceeded")
        succeeded, value = provider_outcome
        if not succeeded:
            assert isinstance(value, BaseException)
            raise value
        return value

    def _process(
        self, request: StagedRelationAssessment
    ) -> RelationAssessmentReconciliation:
        if (
            request.input_byte_count > self.policy.max_input_bytes
            or request.input_token_count > self.policy.max_input_tokens
        ):
            return self._terminal(
                request, status="failed", failure_code="input_limit"
            )
        input_value = self._current_input(request)
        if input_value is None:
            return self._terminal(
                request,
                status="unavailable",
                failure_code="authorization_changed",
            )
        document = canonical_input(input_value)
        if (
            content_digest(document) != request.input_digest
            or len(document.encode("utf-8")) != request.input_byte_count
            or input_token_count(document) != request.input_token_count
            or request.tokenizer_id != TOKENIZER_ID
        ):
            return self._terminal(
                request, status="failed", failure_code="input_changed"
            )
        if self.provider is None:
            return self._terminal(
                request,
                status="unavailable",
                failure_code="provider_unavailable",
            )
        if self.connection.in_transaction:
            raise StorageError("a provider call cannot run inside a SQLite transaction")
        try:
            raw = self._call_provider(input_value)
        except _ProviderCapacityExhausted:
            raise
        except TimeoutError:
            return self._terminal(
                request, status="unavailable", failure_code="provider_timeout"
            )
        except RelationAssessmentProviderUnavailable:
            return self._terminal(
                request,
                status="unavailable",
                failure_code="provider_unavailable",
            )
        # The provider is an embedder-owned boundary. Its concrete adapters do not
        # share one exception hierarchy, and raw exception text must never cross into
        # durable state, so every ordinary adapter failure maps to one stable code.
        except Exception:  # noqa: BLE001
            return self._terminal(
                request, status="failed", failure_code="provider_error"
            )
        try:
            verdict = validate_relation_assessment_response(input_value, raw)
        except (RelationAssessmentResponseInvalid, TypeError, ValueError):
            return self._terminal(
                request, status="failed", failure_code="invalid_response"
            )
        return self._terminal(request, status="assessed", verdict=verdict)

    def run_pending(
        self, *, budget: int | None = None
    ) -> tuple[RelationAssessmentReconciliation, ...]:
        """Process a bounded serial batch; live provider concurrency is one."""

        if not self.policy.enabled:
            return ()
        call_budget = self.policy.maximum_calls
        if budget is not None:
            call_budget = min(
                call_budget,
                _bounded_integer("budget", budget, HARD_MAXIMUM_CALLS),
            )
        completed: list[RelationAssessmentReconciliation] = []
        try:
            while len(completed) < call_budget:
                request = read_oldest_pending_assessment(
                    self.connection,
                    workspace_id=self.workspace_id,
                    configuration_digest=self.policy.configuration_digest,
                )
                if request is None:
                    request = stage_next_assessment(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        configuration_digest=self.policy.configuration_digest,
                        provider_id=self.policy.provider_id,
                        model_id=self.policy.model_id,
                        prompt_version=self.policy.prompt_version,
                        response_schema_version=self.policy.response_schema_version,
                        timeout_ms=self.policy.timeout_ms,
                        maximum_calls=self.policy.maximum_calls,
                        maximum_concurrency=self.policy.maximum_concurrency,
                        label_grant=local_owner_label_grant(
                            principal_id=CONFIGURED_LOCAL_OWNER,
                            workspace_id=self.workspace_id,
                            granted_workspace=self.workspace_id,
                        ),
                        allocate_identifier=self.allocate_identifier,
                        occurred_at_us=self._now_us(),
                    )
                if request is None:
                    break
                try:
                    completed.append(self._process(request))
                except _ProviderCapacityExhausted:
                    # A timed-out daemon call may still be unwinding.  Leave this
                    # durable staged request pending for the next maintenance pass.
                    break
        except (StorageError, sqlite3.Error):
            # Fence loss and contention leave the staged request for the next pass.
            pass
        return tuple(completed)


__all__ = [
    "DEFAULT_MAXIMUM_CALLS",
    "HARD_MAXIMUM_CALLS",
    "HARD_MAXIMUM_CONCURRENCY",
    "EngineeringRelationAssessmentExecutor",
    "RelationAssessmentPolicy",
    "RelationAssessmentProvider",
    "RelationAssessmentProviderUnavailable",
    "RelationAssessmentResponseInvalid",
    "ValidatedRelationAssessment",
    "validate_relation_assessment_response",
]
