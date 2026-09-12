"""`evidence.capture` end to end, through the production dispatch and nothing else.

The path exercised here is the one `service.main.serve` composes: a real migrated
workspace on a real `WorkspaceLayout`, a real lease and mutation guard, the real S3
content-ingestion session from `build_job_application_dispatcher`, the twelve checks of
`authorize_application_request`, the registered handler, the real mutation coordinator,
the real blob publication primitive and the real FTS5 projection. Nothing on it is a
fake handler, a composed stand-in registry or a hand-built context.

What this file is for is the one claim that is particular to this operation and that no
other test in this tree makes: **a capture may not report success until the content it
captured is findable by `evidence.search`.** The acceptance cases below are the ones
that pin it from both sides -- the barrier passing when the index really holds the
words, and refusing when it holds only the identity surface -- plus the durable,
idempotency and inertness properties a mutation in this build must have anyway.

The one fault this file injects is at a module boundary rather than inside the handler:
`test_...projection_failure_then_same_key_repair` replaces `build_search_projection` in
the handler module for exactly one attempt, because the recovery it proves -- commit
stands, barrier refuses, a same-key replay repairs and then succeeds -- has no other way
to be reached from outside. The handler, the coordinator, the projection and the search
that reads it are the production articles in that test as in every other one here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.fencing import (
    close_guard,
    fenced_transaction,
    open_guard,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    EVIDENCE_CAPTURE_OPERATION,
    EVIDENCE_SEARCH_OPERATION,
    JOB_FAMILY_PURPOSES,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    LOCAL_TRANSPORT_ADAPTER,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    build_job_application_dispatcher,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import evidence as evidence_handlers
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.storage.projections.fts import (
    ProjectionError,
    open_search_projection,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
    EVIDENCE_CAPTURE_SOURCE_KIND,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    EvidenceCaptureResult,
    EvidenceSearchResult,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
    validate_evidence_capture_result,
)

WORKSPACE_ID = "ws-capture-0001"
INSTALLATION_ID = "inst-capture-0001"
SERVICE_INSTANCE = "svc-capture-one"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
CAPTURE_ENTRY = get_operation_metadata(EVIDENCE_CAPTURE_OPERATION)
SEARCH_ENTRY = get_operation_metadata(EVIDENCE_SEARCH_OPERATION)

ARTIFACTS = "omnivia_evidence_artifacts"
BLOBS = "omnivia_blob_objects"
INTEGRITY = "omnivia_blob_integrity_events"
STAGED = "omnivia_staged_sources"
PROVENANCE = "omnivia_evidence_provenance_events"
AUDIT = "omnivia_application_audit_events"

#: A word that occurs in no identity surface this build writes, so a search matching it
#: matched the submitted bytes rather than the evidence id, the source id or the
#: metadata the row carries. Every visibility assertion below turns on that.
MARKER = "syzygial"


# --- a real, owned, migrated workspace on a real layout ------------------------


@dataclass(frozen=True)
class Served:
    """The service surface the two handlers read, and nothing else on it.

    `evidence.search` reaches `.connection`; `evidence.capture` reaches `.connection`,
    `.identity` and `.layout.blobs_path`. Presenting exactly those is what keeps this
    test honest about what a handler is allowed to touch.
    """

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int
    layout: WorkspaceLayout


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[Served]:
    """A workspace taken to the canonical schema through the real migrator."""
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    layout.root.mkdir()
    layout.blobs_path.mkdir()
    materialise_phase0_baseline(layout.database_path)
    maintenance = open_database(layout.database_path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = bootstrap_generation_one(
            maintenance,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=True,
            service_instance_id=SERVICE_INSTANCE,
        )
        apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        maintenance.close()

    identity = ServiceInstanceIdentity(
        service_instance_id=SERVICE_INSTANCE,
        installation_id=INSTALLATION_ID,
        process=ProcessEvidence(
            pid=4343, start_time="100", boot_id="boot-capture", os_principal="me"
        ),
    )
    connection = open_database(layout.database_path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    yield Served(
        connection=connection,
        identity=identity,
        generation=lease.fencing_generation,
        layout=layout,
    )
    connection.close()


# --- the production application path -------------------------------------------


def _allocator(tag: str) -> Any:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{tag}-{counts[prefix]}"

    return allocate


@pytest.fixture
def router(owned: Served) -> ApplicationDispatcher:
    """One router per test, for the reason production has one per service instance.

    The identifier allocator and every durable identity derived from it belong to the
    composed surface rather than to a call, so building a second one inside a test would
    hand two dispatchers the same audit references.
    """
    return build_dispatcher(owned)


def build_dispatcher(
    owned: Served, *, tag: str = "cap", principal: str = LOCAL_PRINCIPAL
) -> ApplicationDispatcher:
    """Capture and search behind one router, composed as `service.main.serve` does it.

    The reads dispatcher is the fallback of the ingestion one, which is the same
    chaining production uses, so a single `dispatch` call answers both operations and
    neither is served by a registry this test built for it.

    `principal` is a parameter for one test only -- the one that submits the same source
    as a second authenticated principal -- and both halves of the composition take it
    together, because `ApplicationDispatcher` refuses a wiring whose session and probe
    grant name different principals.
    """
    reads_registry = build_application_registry()
    reads = ApplicationDispatcher(
        registry=reads_registry,
        session=local_owner_session(
            principal_id=principal,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=frozenset(
                {WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}
            ),
        ),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(reads_registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            owned,
        ),
        record=None,
        service=owned,
    )
    return build_job_application_dispatcher(
        service=owned,
        principal_id=principal,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=reads,
        clock=FakeClock(),
        allocate_identifier=_allocator(tag),
    )


def _metadata(entry: Any, *, request_id: str, key: str | None) -> RequestMetadata:
    required = entry.required_capability
    return RequestMetadata(
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        api_version=CONTRACT_VERSION,
        client=CLIENT,
        workspace_id=WORKSPACE_ID,
        scopes=tuple(entry.scope.required_scopes),
        purpose=JOB_FAMILY_PURPOSES.get(entry.name, KNOWLEDGE_RETRIEVAL_PURPOSE),
        required_capabilities=(
            CapabilityRequirement(
                id=required.id,
                minimum_version=required.minimum_version,
                required=True,
            ),
        ),
        idempotency_key=key,
        mutation_precondition=None,
        principal_claim=None,
    )


def submission(**overrides: Any) -> dict[str, Any]:
    """One well-formed `evidence.capture` input, overridable field by field."""
    payload: dict[str, Any] = {
        "source_native_id": "note-1",
        "media_type": "text/markdown",
        "text": f"# Note\n\nThe {MARKER} alignment was recorded by hand.",
    }
    payload.update(overrides)
    return {name: value for name, value in payload.items() if value is not None}


def capture_request(*, request_id: str, key: str, **overrides: Any) -> RequestEnvelope:
    return RequestEnvelope(
        operation=EVIDENCE_CAPTURE_OPERATION,
        metadata=_metadata(CAPTURE_ENTRY, request_id=request_id, key=key),
        input=submission(**overrides),
    )


def search_request(query: str, *, request_id: str = "req-search-1") -> RequestEnvelope:
    return RequestEnvelope(
        operation=EVIDENCE_SEARCH_OPERATION,
        metadata=_metadata(SEARCH_ENTRY, request_id=request_id, key=None),
        input={"query": query},
    )


def answered(response: ResponseEnvelope) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def refusal(response: ResponseEnvelope) -> ErrorResponseEnvelope:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def captured(response: ResponseEnvelope) -> EvidenceCaptureResult:
    """The result as the contract's own value, validated by the contract's validator."""
    result = EvidenceCaptureResult.from_wire(answered(response).result)
    validate_evidence_capture_result(result)
    return result


def found(router: ApplicationDispatcher, query: str) -> tuple[str, ...]:
    """The evidence ids `evidence.search` answers `query` with, through dispatch."""
    response = answered(router.dispatch(search_request(query)))
    return tuple(
        item.evidence_id
        for item in EvidenceSearchResult.from_wire(response.result).evidence
    )


def rows(owned: Served, statement: str, *parameters: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in owned.connection.execute(statement, parameters)]


def count(owned: Served, table: str) -> int:
    return int(owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def blob_file(owned: Served, checksum: str) -> Path:
    return owned.layout.blobs_path / "sha256" / checksum.removeprefix("sha256:")


# --- the durable canonical write ------------------------------------------------


def test_capture_writes_the_canonical_rows_and_a_conformant_result(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """One capture, five durable rows, one blob object, one validated result."""
    text = submission()["text"]
    content = text.encode("utf-8")
    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"

    result = captured(
        router.dispatch(capture_request(request_id="req-1", key="idem-1"))
    )

    assert result.capture_disposition == "created"
    assert result.source.kind == EVIDENCE_CAPTURE_SOURCE_KIND
    assert result.source.source_id == "note-1"
    assert result.source.locator is None
    assert result.content_checksum == checksum
    assert result.content_length_bytes == len(content)

    # The bytes, at their own address, byte-identical to what was submitted.
    assert blob_file(owned, checksum).read_bytes() == content

    # The five rows, and the identity 0037 made unique: a direct submission carries no
    # locator and no retrieval instant, and that is what makes the source id its key.
    assert rows(
        owned,
        f"SELECT evidence_id, source_kind, source_native_id, source_locator, "
        f"source_retrieved_at_us, content_checksum, media_type, sensitivity, "
        f"ingestion_status FROM {ARTIFACTS}",
    ) == [
        (
            result.evidence_id,
            EVIDENCE_CAPTURE_SOURCE_KIND,
            "note-1",
            None,
            None,
            checksum,
            "text/markdown",
            "private",
            "ingested",
        )
    ]
    assert rows(owned, f"SELECT content_digest, content_length_bytes FROM {BLOBS}") == [
        (checksum, len(content))
    ]
    assert rows(
        owned,
        f"SELECT content_digest, integrity_sequence, outcome FROM {INTEGRITY}",
    ) == [(checksum, 1, "verified")]
    assert rows(
        owned,
        f"SELECT source_kind, staging_outcome, computed_checksum FROM {STAGED}",
    ) == [(EVIDENCE_CAPTURE_SOURCE_KIND, "verified", checksum)]
    assert rows(
        owned,
        f"SELECT evidence_id, provenance_sequence, action, actor_kind, source_kind "
        f"FROM {PROVENANCE}",
    ) == [(result.evidence_id, 1, "captured", "service", EVIDENCE_CAPTURE_SOURCE_KIND)]

    # The audit event the coordinator wrote, and the reference the caller was handed.
    assert count(owned, AUDIT) == 1


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"media_type": "text/plain", "text": f"plain {MARKER} text"}, b"plain"),
        (
            {"media_type": "text/markdown", "text": f"# {MARKER}\n\n- one\n"},
            b"# ",
        ),
        (
            {
                "text": None,
                "content_base64": base64.b64encode(
                    f"encoded {MARKER} bytes".encode()
                ).decode("ascii"),
            },
            b"encoded",
        ),
        (
            {"text": f"café naïve 漢字 {MARKER}"},
            "café".encode(),
        ),
    ],
    ids=["plain", "markdown", "base64", "utf8-multibyte"],
)
def test_capture_admits_each_accepted_content_form(
    owned: Served,
    router: ApplicationDispatcher,
    overrides: dict[str, Any],
    expected: bytes,
) -> None:
    """Plain, Markdown, base64 and multi-byte UTF-8, each stored as its own bytes.

    The length the service reports is the *decoded byte* length rather than a character
    count, which is the difference the multi-byte case exists to catch: a submission
    whose characters and bytes differ must be measured in bytes, because that is what
    the blob holds and what the ceiling is stated in.
    """
    result = captured(
        router.dispatch(capture_request(request_id="req-1", key="idem-1", **overrides))
    )
    content = blob_file(owned, result.content_checksum).read_bytes()
    assert expected in content
    assert len(content) == result.content_length_bytes
    assert f"sha256:{hashlib.sha256(content).hexdigest()}" == result.content_checksum
    assert found(router, MARKER) == (result.evidence_id,)


def test_capture_admits_one_mebibyte_and_refuses_the_byte_after_it(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """The ceiling is inclusive, and the first byte past it is a refusal."""
    at_bound = "a" * EVIDENCE_CAPTURE_MAX_CONTENT_BYTES
    result = captured(
        router.dispatch(
            capture_request(
                request_id="req-1",
                key="idem-1",
                media_type="text/plain",
                text=at_bound,
            )
        )
    )
    assert result.content_length_bytes == EVIDENCE_CAPTURE_MAX_CONTENT_BYTES

    over = refusal(
        router.dispatch(
            capture_request(
                request_id="req-2",
                key="idem-2",
                source_native_id="note-2",
                media_type="text/plain",
                text=at_bound + "a",
            )
        )
    )
    assert over.error.code == "invalid_request"

    empty = refusal(
        router.dispatch(
            capture_request(
                request_id="req-3",
                key="idem-3",
                source_native_id="note-3",
                media_type="text/plain",
                text="",
            )
        )
    )
    assert empty.error.code == "invalid_request"

    # An encoded payload that is not base64. The refusal must quote none of it: the
    # decoder's own error carries the rejected string, which here is the caller's
    # document.
    undecodable = refusal(
        router.dispatch(
            capture_request(
                request_id="req-4",
                key="idem-4",
                source_native_id="note-4",
                media_type="text/plain",
                text=None,
                content_base64=f"not-base64-{MARKER}!!",
            )
        )
    )
    assert undecodable.error.code == "invalid_request"
    assert MARKER not in json.dumps(undecodable.error.to_wire())

    # An encoded payload longer than any content within the ceiling could encode to,
    # refused on its encoded length alone rather than after it is allocated.
    oversized = refusal(
        router.dispatch(
            capture_request(
                request_id="req-5",
                key="idem-5",
                source_native_id="note-5",
                media_type="text/plain",
                text=None,
                content_base64=base64.b64encode(
                    b"a" * (EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 1)
                ).decode("ascii"),
            )
        )
    )
    assert oversized.error.code == "invalid_request"

    # No refusal wrote anything: every bound is applied before the durable path.
    assert count(owned, ARTIFACTS) == 1


# --- source identity: exact reuse, or a conflict --------------------------------


def test_an_exact_resubmission_reuses_the_source_and_a_changed_claim_conflicts(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """The same source under a new key is the same artifact; a changed claim is not.

    Reuse is not "the bytes look the same". Each of the three changes below leaves the
    source identity intact and alters something the stored artifact states about it, and
    returning the first artifact for any of them would silently discard the difference.
    """
    first = captured(
        router.dispatch(
            capture_request(request_id="req-1", key="idem-1", source_version="v1")
        )
    )
    assert first.capture_disposition == "created"

    # A different idempotency key, an identical submission: the same source.
    again = captured(
        router.dispatch(
            capture_request(request_id="req-2", key="idem-2", source_version="v1")
        )
    )
    assert again.capture_disposition == "already_captured"
    assert again.evidence_id == first.evidence_id
    assert count(owned, ARTIFACTS) == 1

    for index, changed in enumerate(
        (
            {"text": f"a different {MARKER} note"},
            {"media_type": "text/plain"},
            {"source_version": "v2"},
            {"event_at": "2026-07-30T00:00:00Z"},
        ),
        start=3,
    ):
        payload: dict[str, Any] = {"source_version": "v1"}
        payload.update(changed)
        conflict = refusal(
            router.dispatch(
                capture_request(
                    request_id=f"req-{index}", key=f"idem-{index}", **payload
                )
            )
        )
        assert conflict.error.code == "conflict", changed
        assert MARKER not in json.dumps(conflict.error.to_wire()), changed

    # Every conflict left the one captured artifact exactly as it was.
    assert count(owned, ARTIFACTS) == 1
    assert count(owned, PROVENANCE) == 1


def test_a_changed_observed_at_conflicts_and_the_stated_one_still_reuses(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """`observed_at` is a stated claim about the source, compared on both sides.

    The loop above changes `observed_at` only from absent to stated, which a handler that
    compared the *wrong* stored column would still refuse -- so this pins the two cases
    that separate a real comparison from an accidental one: an identical resubmission
    that states the same instant must reuse, and a later instant under the same source
    identity must conflict. Withdrawing the claim entirely is the third, because absent
    is a statement about the source too, not a request to keep what is stored.
    """
    first = captured(
        router.dispatch(
            capture_request(
                request_id="req-1", key="idem-1", observed_at="2026-07-30T00:00:00Z"
            )
        )
    )
    assert first.capture_disposition == "created"

    same = captured(
        router.dispatch(
            capture_request(
                request_id="req-2", key="idem-2", observed_at="2026-07-30T00:00:00Z"
            )
        )
    )
    assert same.capture_disposition == "already_captured"
    assert same.evidence_id == first.evidence_id

    for index, observed_at in enumerate(("2026-07-31T00:00:00Z", None), start=3):
        conflict = refusal(
            router.dispatch(
                capture_request(
                    request_id=f"req-{index}",
                    key=f"idem-{index}",
                    observed_at=observed_at,
                )
            )
        )
        assert conflict.error.code == "conflict", observed_at
        assert MARKER not in json.dumps(conflict.error.to_wire()), observed_at

    assert count(owned, ARTIFACTS) == 1
    assert count(owned, PROVENANCE) == 1


def test_a_second_principal_reuses_the_source_rather_than_forking_it(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """Who submitted is not part of what the source is.

    The second submission arrives through a separately composed dispatcher acting as a
    different authenticated principal, under a different idempotency key -- so it cannot
    be answered from the first attempt's stored outcome, which is keyed by principal as
    well as by key, and reaches the source lookup for real. That lookup is not scoped by
    principal on purpose: a source-relative-to-a-principal identity would let one source
    become two authoritative artifacts, and 0037's unique index would not stop it because
    the two rows would differ in nothing the index covers.
    """
    first = captured(router.dispatch(capture_request(request_id="req-1", key="idem-1")))

    other = build_dispatcher(owned, tag="two", principal="local-owner-two")
    assert other.session.principal_id != router.session.principal_id

    again = captured(other.dispatch(capture_request(request_id="req-2", key="idem-2")))
    assert again.capture_disposition == "already_captured"
    assert again.evidence_id == first.evidence_id

    # One artifact, one provenance event: the second principal's call recorded its own
    # audit event -- it was authorized and it settled -- and wrote no second source.
    assert count(owned, ARTIFACTS) == 1
    assert count(owned, PROVENANCE) == 1
    assert count(owned, AUDIT) == 2
    assert found(router, MARKER) == (first.evidence_id,)


def test_a_same_key_replay_is_answered_and_a_changed_body_conflicts(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """One key, one settled answer, and a different body under it is a typed conflict."""
    primary = answered(
        router.dispatch(capture_request(request_id="req-1", key="idem-1"))
    )

    replay = answered(
        router.dispatch(capture_request(request_id="req-2", key="idem-1"))
    )
    assert replay.result == primary.result
    assert replay.metadata.audit_reference == primary.metadata.audit_reference
    # The replay is answered from the settled outcome, so it is the *first* attempt's
    # disposition that is returned rather than a second `already_captured`.
    assert replay.result["capture_disposition"] == "created"

    conflict = refusal(
        router.dispatch(
            capture_request(
                request_id="req-3", key="idem-1", text=f"a different {MARKER} note"
            )
        )
    )
    assert conflict.error.code == "idempotency_conflict"
    assert count(owned, ARTIFACTS) == 1
    assert count(owned, AUDIT) == 1


# --- Gate A: findable before success --------------------------------------------


def test_captured_text_is_lexically_visible_when_the_capture_reports_success(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """The acceptance case: the words are in the index by the time the caller is told.

    `MARKER` occurs in the submitted content and in no identity surface this build
    writes, so a search matching it matched the *content* the projection composed --
    which is the thing the barrier exists to guarantee -- rather than the evidence id,
    the source id or the row's metadata.
    """
    result = captured(
        router.dispatch(capture_request(request_id="req-1", key="idem-1"))
    )

    assert found(router, MARKER) == (result.evidence_id,)
    projection = open_search_projection(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        blobs_root=owned.layout.blobs_path,
    )
    assert result.evidence_id in projection.content_indexed


def test_a_projection_failure_refuses_and_the_same_key_repairs_it(
    owned: Served, router: ApplicationDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit stands, barrier refuses, the same key replays and then succeeds.

    The failure is injected at the handler's own module boundary for exactly one
    attempt. What is asserted is the recovery the barrier's design rests on: nothing
    remembers that the first attempt failed, because the repair is derived from the
    database -- the replay resolves the stored outcome, runs the barrier again, and
    returns the settled result once the index is level.
    """
    attempts = {"count": 0}
    real = evidence_handlers.build_search_projection

    def failing(*args: Any, **keywords: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ProjectionError("the projection could not be built")
        return real(*args, **keywords)

    monkeypatch.setattr(evidence_handlers, "build_search_projection", failing)

    first = refusal(router.dispatch(capture_request(request_id="req-1", key="idem-1")))
    assert first.error.code in {"projection_unavailable", "stale_projection"}
    assert first.error.retry_class == RETRY_CLASS_RETRYABLE_AFTER_DELAY

    # The business commit stood: the evidence is durable even though the caller was
    # refused, which is the cost the barrier pays for never reporting a success the
    # search handler would contradict.
    durable = rows(owned, f"SELECT evidence_id FROM {ARTIFACTS}")
    assert len(durable) == 1
    assert count(owned, AUDIT) == 1

    repaired = captured(
        router.dispatch(capture_request(request_id="req-2", key="idem-1"))
    )
    assert repaired.evidence_id == durable[0][0]
    assert repaired.capture_disposition == "created"
    assert found(router, MARKER) == (repaired.evidence_id,)
    # One capture, still: the repair replayed the settled outcome rather than writing a
    # second artifact or a second audit event.
    assert count(owned, ARTIFACTS) == 1
    assert count(owned, AUDIT) == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink"])
def test_a_blob_that_is_not_the_content_cannot_pass_gate_a(
    owned: Served, router: ApplicationDispatcher, damage: str
) -> None:
    """None of the three damaged objects yields a content-indexed document.

    Gate A's check is the projection's own statement that *this* evidence id was
    composed from content. A reclaimed, rewritten or symlinked object each leaves the
    document indexed by its identity surface alone -- which still matches a query naming
    the source id, and would therefore satisfy a weaker barrier while every word the
    caller submitted answers "not found".
    """
    result = captured(
        router.dispatch(capture_request(request_id="req-1", key="idem-1"))
    )
    path = blob_file(owned, result.content_checksum)
    content = path.read_bytes()

    path.unlink()
    if damage == "corrupt":
        path.write_bytes(content.replace(MARKER.encode(), b"elsewhere"))
    elif damage == "symlink":
        target = owned.layout.root / "elsewhere"
        target.write_bytes(content)
        path.symlink_to(target)

    projection = open_search_projection(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        blobs_root=owned.layout.blobs_path,
    )
    assert result.evidence_id not in projection.content_indexed
    # The identity surface is still indexed, which is exactly why the barrier may not
    # be satisfied by an open that returned.
    assert found(router, "note-1") == (result.evidence_id,)
    assert found(router, MARKER) == ()


@pytest.mark.parametrize("damage", ["corrupt", "symlink"])
def test_a_capture_over_a_damaged_object_is_refused_rather_than_settled(
    owned: Served, router: ApplicationDispatcher, damage: str
) -> None:
    """Publication verifies what is already there, so the refusal comes before any row.

    The blob is published before the row that names it, and publication of content that
    is already present is a verification rather than an overwrite. A workspace holding
    something else at that address therefore refuses the capture -- retryably, since the
    object is reclaimable -- instead of committing evidence pointing at bytes that are
    not the ones submitted.
    """
    content = submission()["text"].encode("utf-8")
    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
    path = blob_file(owned, checksum)
    path.parent.mkdir(parents=True, exist_ok=True)
    if damage == "corrupt":
        path.write_bytes(b"not the submitted bytes")
    else:
        target = owned.layout.root / "elsewhere"
        target.write_bytes(content)
        path.symlink_to(target)

    response = refusal(
        router.dispatch(capture_request(request_id="req-1", key="idem-1"))
    )
    assert response.error.code == "internal_recoverable"
    assert str(owned.layout.root) not in json.dumps(response.error.to_wire())
    assert count(owned, ARTIFACTS) == 0
    assert count(owned, AUDIT) == 0


def test_a_replay_is_re_authorized_rather_than_served_from_the_stored_answer(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """Every attempt takes a fresh grant, replays included.

    The mutation guard is the live authority this service instance writes under, and it
    is re-read on the replay rather than carried over from the first attempt. Withdrawing
    it between two identical calls refuses the second, which is the property that makes
    a stored outcome an answer rather than a standing permission.
    """
    primary = answered(
        router.dispatch(capture_request(request_id="req-1", key="idem-1"))
    )
    close_guard(owned.connection)

    denied = refusal(router.dispatch(capture_request(request_id="req-2", key="idem-1")))
    assert denied.error.code == "internal_non_recoverable"

    # The settled outcome is untouched, so the capture is recoverable once authority is
    # restored rather than lost with the refusal.
    open_guard(
        owned.connection,
        owned.identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    )
    restored = answered(
        router.dispatch(capture_request(request_id="req-3", key="idem-1"))
    )
    assert restored.result == primary.result
    assert count(owned, ARTIFACTS) == 1


# --- what may not travel, and what may not be interpreted -----------------------


def test_no_refusal_or_audit_record_carries_the_submitted_text(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """The submitted document reaches the blob and the index, and nowhere else.

    A capture's payload is the caller's own document, so it is the one value in this
    operation that must not appear in a wire error a caller reads or in the audit trail
    a workspace keeps. The decode refusal is the sharpest case: the contract's own
    decode errors quote the payload they rejected, and the handler's sentinel-then-raise
    shape is what keeps that text out of `__context__`.
    """
    secret = f"{MARKER} do-not-echo"

    malformed = refusal(
        router.dispatch(
            RequestEnvelope(
                operation=EVIDENCE_CAPTURE_OPERATION,
                metadata=_metadata(CAPTURE_ENTRY, request_id="req-1", key="idem-1"),
                # Two content fields: structurally decodable, semantically refused, and
                # the refusal must not quote either of them.
                input={
                    "source_native_id": "note-1",
                    "media_type": "text/markdown",
                    "text": secret,
                    "content_base64": base64.b64encode(secret.encode()).decode("ascii"),
                },
            )
        )
    )
    assert malformed.error.code == "invalid_request"
    assert secret not in json.dumps(malformed.error.to_wire())

    result = captured(
        router.dispatch(capture_request(request_id="req-2", key="idem-2", text=secret))
    )

    # The audit trail, the idempotency record and the artifact's own metadata: the
    # document is in none of them. What the metadata carries is the source id the caller
    # chose, which is returned to every reader of the artifact.
    for table in (AUDIT, "omnivia_idempotency_claims", "omnivia_idempotency_outcomes"):
        recorded = json.dumps(rows(owned, f"SELECT * FROM {table}"), default=str)
        assert secret not in recorded, table
    metadata = rows(owned, f"SELECT original_metadata_json FROM {ARTIFACTS}")[0][0]
    assert secret not in metadata
    assert json.loads(metadata) == {
        "capture": EVIDENCE_CAPTURE_SOURCE_KIND,
        "source_id": "note-1",
    }
    assert result.content_length_bytes == len(secret.encode("utf-8"))


def test_hostile_looking_content_is_a_document_and_nothing_else(
    owned: Served, router: ApplicationDispatcher, tmp_path: Path
) -> None:
    """Paths, URLs, JSON and SQL in the submitted text are characters, not instructions.

    Nothing on this path opens a URL, resolves a path out of the content, evaluates it
    or reads a key from it. The assertion is the whole of that: the bytes come back
    byte-identical, the workspace root gained nothing but the one blob, and the words are
    findable the same way any other submission's are.
    """
    hostile = (
        f"file:///etc/passwd\n"
        f'{{"role": "admin", "grant": "*"}}\n'
        f"../../../../etc/passwd\n"
        f"'); DROP TABLE {ARTIFACTS}; --\n"
        f'{MARKER} NEAR/2 "quoted" OR *\n'
    )
    before = sorted(p.name for p in owned.layout.root.iterdir())

    result = captured(
        router.dispatch(
            capture_request(
                request_id="req-1",
                key="idem-1",
                media_type="text/plain",
                text=hostile,
            )
        )
    )

    assert blob_file(owned, result.content_checksum).read_bytes() == hostile.encode()
    assert sorted(p.name for p in owned.layout.root.iterdir()) == before
    assert not (tmp_path / "etc").exists()
    # The table the submission names is still there, with exactly the one row this
    # capture wrote.
    assert count(owned, ARTIFACTS) == 1
    assert found(router, MARKER) == (result.evidence_id,)


def test_a_capture_cannot_reach_another_workspace(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """The workspace is the authorized one; the payload carries no second opinion."""
    request = capture_request(request_id="req-1", key="idem-1")
    elsewhere = refusal(
        router.dispatch(
            replace(
                request,
                metadata=replace(request.metadata, workspace_id="ws-capture-0002"),
            )
        )
    )
    assert elsewhere.error.code == "workspace_not_granted"
    assert count(owned, ARTIFACTS) == 0


def test_the_ingestion_family_grant_covers_capture_without_widening(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """Capture joins the S3 family by one operation and one capability, and no more.

    The seven authority families are unchanged: this asserts the session the production
    constructor builds holds `evidence.capture` under the family's single
    `content_ingestion` purpose and single contributor role, and that its scope set is
    the one `import.start` already carried.
    """
    session = router.session
    assert EVIDENCE_CAPTURE_OPERATION in session.operations
    assert session.roles == frozenset({"workspace_contributor"})
    assert "memory:write" in session.scopes
    assert JOB_FAMILY_PURPOSES[EVIDENCE_CAPTURE_OPERATION] in session.purposes
    assert any(capability.id == "evidence.write" for capability in session.capabilities)

    # And the read-only local owner still cannot hold it: that constructor refuses any
    # operation declaring a side effect, which is what keeps the read family read-only.
    with pytest.raises(ValueError):
        local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=frozenset({EVIDENCE_CAPTURE_OPERATION}),
        )


def test_a_fenced_write_outside_the_handler_cannot_forge_a_second_source(
    owned: Served,
    router: ApplicationDispatcher,
) -> None:
    """0037's unique index is what makes the source identity a key rather than a habit."""
    captured(router.dispatch(capture_request(request_id="req-1", key="idem-1")))
    row = owned.connection.execute(f"SELECT * FROM {ARTIFACTS}").fetchone()
    columns = [
        description[0]
        for description in owned.connection.execute(
            f"SELECT * FROM {ARTIFACTS}"
        ).description
    ]
    duplicate = dict(zip(columns, row, strict=True))
    duplicate["evidence_id"] = "evd-forged-1"

    with (
        pytest.raises(sqlite3.IntegrityError),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        owned.connection.execute(
            f"INSERT INTO {ARTIFACTS} ({', '.join(duplicate)}) VALUES "
            f"({', '.join('?' for _ in duplicate)})",
            tuple(duplicate.values()),
        )
    assert count(owned, ARTIFACTS) == 1
