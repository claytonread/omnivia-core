"""Focused S3 import and durable-job family acceptance."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_blobs_staged_sources_and_evidence_migration as m2
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import fenced_transaction, open_guard
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    JOB_FAMILY_PURPOSES,
    ApplicationDispatcher,
    build_job_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.memory import HmacContinuationTokenCodec
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    HttpListener,
)
from omnivia_core_runtime.service.jobs import (
    JobError,
    acknowledge_application_job_cancellation,
    claim_application_job,
    complete_application_job,
    fail_application_job,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import LocalSocketServer, endpoint_for_path
from omnivia_core_runtime.storage.jobs import recover_stranded_application_jobs
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER
from test_v06_5_s2_memory_migration import _apply_through
from v06_5_c1_evidence import semantic_execution

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    decode_response,
    encode_request,
    get_operation_metadata,
    to_canonical_json,
)

MIGRATION_VERSION = 15
WORKSPACE_ID = m2.WORKSPACE_ID
INSTALLATION_ID = s0.INSTALLATION_ID
PRINCIPAL = CONFIGURED_LOCAL_OWNER
WALL = datetime(2026, 7, 30, tzinfo=UTC)
WALL_US = int(WALL.timestamp() * 1_000_000)
ADAPTERS = ("in-process", "local-ipc", "http")
RETRYABLE_ERROR: dict[str, object] = {
    "code": "internal_recoverable",
    "message": "transient",
    "retry_class": "retryable",
}


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    _apply_through(path, MIGRATION_VERSION, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


def _guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def _insert(
    connection: Any,
    table: str,
    row: Mapping[str, object],
) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def _source(*, source_version: str | None = "v3") -> dict[str, object]:
    value: dict[str, object] = {
        "staged_source_ref": "staged-s3",
        "source_kind": "filesystem.archive",
        "content_checksum": m2.DIGEST_A,
        "content_length_bytes": 1024,
        "media_type": "application/zip",
    }
    if source_version is not None:
        value["source_version"] = source_version
    return value


def _seed_staged_source(
    holder: m1.Owned,
    *,
    source_version: str | None = "v3",
    outcome: str = "verified",
    original_metadata_json: str = '{"kind":"archive"}',
    staged_source_ref: str = "staged-s3",
) -> dict[str, object]:
    descriptor = _source(source_version=source_version)
    descriptor["staged_source_ref"] = staged_source_ref
    blob = m2.row_for(m2.BLOBS)
    staged = m2.row_for(
        m2.STAGED,
        staged_source_ref=staged_source_ref,
        source_kind=descriptor["source_kind"],
        declared_checksum=descriptor["content_checksum"],
        content_length_bytes=descriptor["content_length_bytes"],
        media_type=descriptor["media_type"],
        source_version=source_version,
        original_metadata_json=original_metadata_json,
        staging_outcome=outcome,
        computed_checksum=(descriptor["content_checksum"] if outcome == "verified" else None),
        blob_workspace_id=(WORKSPACE_ID if outcome == "verified" else None),
        blob_content_digest=(descriptor["content_checksum"] if outcome == "verified" else None),
    )
    with _guarded(holder):
        if holder.connection.execute(
            "SELECT 1 FROM omnivia_blob_objects WHERE workspace_id = ? "
            "AND content_digest = ?",
            (WORKSPACE_ID, m2.DIGEST_A),
        ).fetchone() is None:
            _insert(holder.connection, m2.BLOBS, blob)
        _insert(holder.connection, m2.STAGED, staged)
    return descriptor


def _request(
    operation: str,
    operation_input: Mapping[str, object],
    *,
    request_id: str,
    idempotency_key: str | None = None,
    workspace_id: str = WORKSPACE_ID,
    purpose: str | None = None,
) -> RequestEnvelope:
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=(JOB_FAMILY_PURPOSES[operation] if purpose is None else purpose),
        workspace_id=workspace_id,
    )


def _router(dispatcher: ApplicationDispatcher) -> DocumentRouter:
    return DocumentRouter(
        probes=ProbeRouter(
            facts=lambda: ServiceFacts(
                observed_at="2026-08-12T00:00:00Z",
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            ),
            capabilities=tuple,
            clock=lambda: 0,
        ),
        dispatch=dispatcher.dispatch,
    )


def _unrecorded_transport_call(
    adapter: str,
    dispatcher: ApplicationDispatcher,
    request: RequestEnvelope,
) -> ResponseEnvelope:
    if adapter == "in-process":
        return dispatcher.dispatch(request)
    router = _router(dispatcher)
    if adapter == "local-ipc":
        with tempfile.TemporaryDirectory(prefix="ov-s3-", dir="/tmp") as directory:
            endpoint = endpoint_for_path(Path(directory) / "service.sock")
            server = LocalSocketServer(router=router, endpoint=endpoint)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(10)
                    client.connect(endpoint.address)
                    client.sendall(encode_frame(encode_request(request)))
                    header = b""
                    while len(header) < 8:
                        header += client.recv(8 - len(header))
                    length = int.from_bytes(header[4:], "big")
                    body = b""
                    while len(body) < length:
                        body += client.recv(length - len(body))
                return decode_response(decode_frame(header + body))
            finally:
                server.stop()

    assert adapter == "http"
    credential = "s3-production-credential"
    server = HttpListener(
        router=router,
        principal=dispatcher.session.principal_id,
        resolver=lambda value: dispatcher.session if value == credential else None,
        authenticated_dispatch=dispatcher.dispatch_for_session,
    )
    server.start()
    try:
        port = int(server.url.rsplit(":", 1)[1])
        body = json.dumps(
            encode_request(request), separators=(",", ":"), sort_keys=True
        ).encode()
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            client.request(
                "POST",
                APPLICATION_PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": CONTENT_TYPE,
                },
            )
            response = client.getresponse()
            response_body = response.read()
        finally:
            client.close()
        assert response.status == 200
        return decode_response(json.loads(response_body))
    finally:
        server.stop()


def _transport_call(
    adapter: str,
    dispatcher: ApplicationDispatcher,
    request: RequestEnvelope,
    *,
    case_id: str | None = None,
) -> ResponseEnvelope:
    return semantic_execution(
        case_id=case_id,
        adapter=adapter,
        route=dispatcher,
        request=request,
        invoke=lambda: _unrecorded_transport_call(adapter, dispatcher, request),
    )


def _fallback(principal: str = PRINCIPAL) -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal=principal,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )


def _allocator(tag: str) -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{tag}-{counts[prefix]}"

    return allocate


def _dispatcher(
    holder: m1.Owned,
    *,
    tag: str,
    principal: str = PRINCIPAL,
    workspace_id: str = WORKSPACE_ID,
    clock: FakeClock | None = None,
    token_codec: HmacContinuationTokenCodec | None = None,
) -> ApplicationDispatcher:
    return build_job_application_dispatcher(
        service=holder,
        principal_id=principal,
        installation_id=INSTALLATION_ID,
        workspace_id=workspace_id,
        fallback=_fallback(principal),
        clock=FakeClock(wall=WALL) if clock is None else clock,
        allocate_identifier=_allocator(tag),
        token_codec=(
            HmacContinuationTokenCodec(b"s3-production-token-secret")
            if token_codec is None
            else token_codec
        ),
    )


def _claim_job(
    holder: m1.Owned,
    clock: FakeClock,
    job_id: str,
) -> dict[str, object]:
    claimed = claim_application_job(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        clock=clock,
        job_id=job_id,
    )
    assert claimed is not None
    return claimed


def _fail_job(
    holder: m1.Owned,
    clock: FakeClock,
    job_id: str,
) -> dict[str, object]:
    return fail_application_job(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        job_id=job_id,
        fencing_generation=holder.generation,
        clock=clock,
        error=RETRYABLE_ERROR,
    )


def _complete_job(
    holder: m1.Owned,
    clock: FakeClock,
    job_id: str,
    source: Mapping[str, object],
) -> dict[str, object]:
    return complete_application_job(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        job_id=job_id,
        fencing_generation=holder.generation,
        clock=clock,
        result_kind="import_completion",
        result={
            "import_run_id": job_id,
            "source": dict(source),
            "discovered_items": 3,
            "evidence_records_created": 2,
            "skipped_items": 1,
            "failed_items": 0,
            "partial": False,
        },
    )


def _acknowledge_cancellation(
    holder: m1.Owned,
    clock: FakeClock,
    job_id: str,
    *,
    reason: str = "requested",
) -> dict[str, object]:
    return acknowledge_application_job_cancellation(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        job_id=job_id,
        fencing_generation=holder.generation,
        clock=clock,
        reason=reason,
    )


def _checkpoint(
    holder: m1.Owned,
    *,
    job_id: str,
    at_us: int,
    attempt_number: int = 1,
) -> None:
    document = '{"cursor":"checkpoint-s3"}'
    with _guarded(holder):
        _insert(
            holder.connection,
            "omnivia_job_checkpoints",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "checkpoint_sequence": 0,
                "attempt_number": attempt_number,
                "created_at_us": at_us,
                "checkpoint_kind": "import.resume",
                "checkpoint_json": document,
                "checkpoint_digest": "sha256:"
                + hashlib.sha256(document.encode()).hexdigest(),
            },
        )


def _takeover(holder: m1.Owned, *, clock: FakeClock) -> m1.Owned:
    successor = m1.make_identity(instance="svc-s3-successor", pid=5353)
    lease = acquire_lease(
        holder.connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=holder.identity.service_instance_id,
    )
    open_guard(
        holder.connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return m1.Owned(
        connection=holder.connection,
        identity=successor,
        generation=lease.fencing_generation,
        path=holder.path,
    )


def _cancel_request(job_id: str, *, request_id: str, key: str) -> RequestEnvelope:
    return _request(
        "job.cancel",
        {"job_id": job_id},
        request_id=request_id,
        idempotency_key=key,
    )


def _retry_request(job_id: str, *, request_id: str, key: str) -> RequestEnvelope:
    return _request(
        "job.retry",
        {"job_id": job_id},
        request_id=request_id,
        idempotency_key=key,
    )


def _fail_and_retry(
    holder: m1.Owned,
    *,
    tag: str,
    checkpoint: bool = False,
) -> tuple[
    FakeClock,
    ApplicationDispatcher,
    str,
    dict[str, object],
    SuccessResponseEnvelope,
]:
    clock = FakeClock(wall=WALL)
    source = _seed_staged_source(holder)
    dispatcher = _dispatcher(holder, tag=tag, clock=clock)
    _, _, started = _start_import(
        holder,
        tag=tag,
        source=source,
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    if checkpoint:
        _checkpoint(holder, job_id=job_id, at_us=WALL_US)
    clock.advance_wall(10)
    failed = _fail_job(holder, clock, job_id)
    clock.advance_wall(10)
    retried = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id=f"req-{tag}-retry",
            key=f"idem-{tag}-retry",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    assert retried.result["recovery_disposition"] == "retry_scheduled"
    return clock, dispatcher, job_id, failed, retried


def _count(holder: m1.Owned, table: str) -> int:
    row = holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _rows(holder: m1.Owned, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in holder.connection.execute(sql, parameters).fetchall()]


def _import_request(
    source: Mapping[str, object],
    *,
    request_id: str,
    idempotency_key: str,
) -> RequestEnvelope:
    return _request(
        "import.start",
        {"source": dict(source)},
        request_id=request_id,
        idempotency_key=idempotency_key,
    )


def _start_import(
    holder: m1.Owned,
    *,
    tag: str,
    source: Mapping[str, object] | None = None,
    adapter: str = "in-process",
    dispatcher: ApplicationDispatcher | None = None,
) -> tuple[ApplicationDispatcher, RequestEnvelope, SuccessResponseEnvelope]:
    accepted_source = _seed_staged_source(holder) if source is None else source
    application = _dispatcher(holder, tag=tag) if dispatcher is None else dispatcher
    request = _import_request(
        accepted_source,
        request_id=f"req-{tag}-import",
        idempotency_key=f"idem-{tag}-import",
    )
    response = _transport_call(adapter, application, request)
    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.metadata.audit_reference is not None
    assert response.metadata.job is not None
    assert response.result["job"]["identity"]["job_id"] == response.metadata.job.job_id
    assert response.result["job"]["identity"]["audit_reference"] == (
        response.metadata.audit_reference
    )
    return application, request, response


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_v06_5_s3_import_start_primary_replay_conflict(
    owned: m1.Owned,
    adapter: str,
) -> None:
    source = _seed_staged_source(owned)
    dispatcher = _dispatcher(owned, tag=f"parent-import-{adapter}")
    primary_request = _import_request(
        source,
        request_id=f"req-parent-import-primary-{adapter}",
        idempotency_key=f"idem-parent-import-{adapter}",
    )
    primary = _transport_call(
        adapter,
        dispatcher,
        primary_request,
        case_id="import.start/primary-success",
    )
    assert isinstance(primary, SuccessResponseEnvelope), primary
    assert primary.result["job"]["state"] == "running"
    assert primary.result["job"]["latest_attempt"] == {
        "attempt_number": 1,
        "started_at": "2026-07-30T00:00:00Z",
        "state": "running",
    }
    assert primary.metadata.job is not None
    job_id = primary.metadata.job.job_id

    replay = _transport_call(
        adapter,
        dispatcher,
        replace(
            primary_request,
            metadata=replace(
                primary_request.metadata,
                request_id=f"req-parent-import-replay-{adapter}",
            ),
        ),
        case_id="import.start/honest-replay",
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == primary.result
    assert replay.metadata.audit_reference == primary.metadata.audit_reference
    assert replay.metadata.job == primary.metadata.job

    conflicting_source = dict(source)
    conflicting_source["source_version"] = "v4"
    conflict = _transport_call(
        adapter,
        dispatcher,
        replace(
            primary_request,
            metadata=replace(
                primary_request.metadata,
                request_id=f"req-parent-import-conflict-{adapter}",
            ),
            input={"source": conflicting_source},
        ),
        case_id="import.start/idempotency-conflict",
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == "idempotency_conflict"
    assert conflict.metadata.audit_reference == primary.metadata.audit_reference
    assert conflict.metadata.job == primary.metadata.job
    assert _rows(
        owned,
        "SELECT job_id, state FROM omnivia_durable_jobs",
    ) == [(job_id, "claimed")]
    assert _count(owned, "omnivia_application_import_claims") == 1
    assert _count(owned, "omnivia_job_attempts") == 1
    assert _count(owned, "omnivia_job_events") == 1


def test_v06_5_s3_import_source_resolution_is_exact_and_null_safe(
    owned: m1.Owned,
) -> None:
    null_source = _seed_staged_source(
        owned,
        source_version=None,
        staged_source_ref="staged-null-version",
    )
    versioned_source = _seed_staged_source(
        owned,
        source_version="v3",
        staged_source_ref="staged-versioned",
    )
    dispatcher = _dispatcher(owned, tag="source-resolution")

    exact_null = dispatcher.dispatch(
        _import_request(
            null_source,
            request_id="req-source-exact-null",
            idempotency_key="idem-source-exact-null",
        )
    )
    assert isinstance(exact_null, SuccessResponseEnvelope), exact_null

    omitted_version = dict(versioned_source)
    omitted_version.pop("source_version")
    refused_null_mismatch = dispatcher.dispatch(
        _import_request(
            omitted_version,
            request_id="req-source-null-mismatch",
            idempotency_key="idem-source-null-mismatch",
        )
    )
    assert isinstance(refused_null_mismatch, ErrorResponseEnvelope)
    assert refused_null_mismatch.error.code == "dependency_unavailable"

    mismatches: tuple[tuple[str, object], ...] = (
        ("staged_source_ref", "staged-missing"),
        ("source_kind", "filesystem.other"),
        ("content_checksum", m2.DIGEST_B),
        ("content_length_bytes", 1025),
        ("media_type", "application/octet-stream"),
        ("source_version", "v4"),
    )
    for field, value in mismatches:
        mismatched = dict(versioned_source)
        mismatched[field] = value
        response = dispatcher.dispatch(
            _import_request(
                mismatched,
                request_id=f"req-source-mismatch-{field}",
                idempotency_key=f"idem-source-mismatch-{field}",
            )
        )
        assert isinstance(response, ErrorResponseEnvelope), (field, response)
        assert response.error.code == "dependency_unavailable"

    assert _count(owned, "omnivia_durable_jobs") == 1
    assert _count(owned, "omnivia_application_import_claims") == 1


def test_v06_5_s3_nonverified_and_hostile_staging_never_reaches_content(
    owned: m1.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = {
        "path": "/etc/passwd",
        "url": "http://127.0.0.1:9/private",
        "inline_archive": "UEsDBAoAAAAA",
        "command": ["sh", "-c", "touch /tmp/omnivia-s3-should-not-exist"],
    }
    verified = _seed_staged_source(
        owned,
        staged_source_ref="staged-hostile-verified",
        original_metadata_json=json.dumps(hostile, sort_keys=True),
    )
    unsafe = _seed_staged_source(
        owned,
        outcome="unsafe",
        staged_source_ref="staged-hostile-unsafe",
        original_metadata_json=json.dumps(hostile, sort_keys=True),
    )
    dispatcher = _dispatcher(owned, tag="hostile-staging")

    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)

    def refuse_path_open(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("import.start attempted to open staged metadata as a path")

    monkeypatch.setattr(Path, "open", refuse_path_open)
    try:
        accepted = dispatcher.dispatch(
            _import_request(
                verified,
                request_id="req-hostile-verified",
                idempotency_key="idem-hostile-verified",
            )
        )
        refused = dispatcher.dispatch(
            _import_request(
                unsafe,
                request_id="req-hostile-unsafe",
                idempotency_key="idem-hostile-unsafe",
            )
        )
    finally:
        owned.connection.set_trace_callback(None)
    assert isinstance(accepted, SuccessResponseEnvelope), accepted
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == "dependency_unavailable"
    assert not any("original_metadata_json" in statement for statement in statements)
    assert _count(owned, "omnivia_durable_jobs") == 1
    assert _count(owned, "omnivia_application_import_claims") == 1
    assert _count(owned, "omnivia_evidence_artifacts") == 0
    assert _count(owned, "omnivia_normalized_source_records") == 0


def test_v06_5_s3_hostile_import_remains_inert(
    owned: m1.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = _seed_staged_source(
        owned,
        staged_source_ref="staged-parent-hostile",
        original_metadata_json=(
            '{"archive_path":"../../etc/passwd","extract_to":"/tmp/escape",'
            '"postprocess":"curl http://127.0.0.1:9"}'
        ),
    )
    dispatcher = _dispatcher(owned, tag="parent-hostile")
    opened: list[tuple[object, ...]] = []
    original_socket = socket.socket

    def observe_socket(*args: Any, **kwargs: Any) -> socket.socket:
        opened.append(args)
        return original_socket(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", observe_socket)
    response = dispatcher.dispatch(
        _import_request(
            hostile,
            request_id="req-parent-hostile",
            idempotency_key="idem-parent-hostile",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    assert opened == []
    assert _count(owned, "omnivia_durable_jobs") == 1
    assert _count(owned, "omnivia_evidence_artifacts") == 0
    assert _count(owned, "omnivia_normalized_source_records") == 0


def test_v06_5_s3_import_claim_is_immutable_and_reconstructs_source(
    owned: m1.Owned,
) -> None:
    source = _seed_staged_source(owned)
    _, _, response = _start_import(
        owned,
        tag="immutable-claim",
        source=source,
    )
    job_id = response.result["job"]["identity"]["job_id"]
    row = owned.connection.execute(
        """
        SELECT audit_ref, staged_source_ref, source_kind, content_checksum,
               content_length_bytes, media_type, source_version, input_json,
               input_digest, input_byte_length, settled_at_us
        FROM omnivia_application_import_claims
        WHERE workspace_id = ? AND job_id = ?
        """,
        (WORKSPACE_ID, job_id),
    ).fetchone()
    assert row is not None
    input_json = to_canonical_json({"source": source})
    assert tuple(row[:7]) == (
        response.metadata.audit_reference,
        source["staged_source_ref"],
        source["source_kind"],
        source["content_checksum"],
        source["content_length_bytes"],
        source["media_type"],
        source["source_version"],
    )
    assert row[7] == input_json
    assert row[8] == "sha256:" + hashlib.sha256(input_json.encode()).hexdigest()
    assert row[9] == len(input_json.encode())
    assert row[10] == WALL_US
    assert json.loads(str(row[7]))["source"] == source

    with pytest.raises(sqlite3.DatabaseError, match="append-only"), _guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_application_import_claims SET media_type = ? "
            "WHERE workspace_id = ? AND job_id = ?",
            ("application/octet-stream", WORKSPACE_ID, job_id),
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), _guarded(owned):
        owned.connection.execute(
            "DELETE FROM omnivia_application_import_claims "
            "WHERE workspace_id = ? AND job_id = ?",
            (WORKSPACE_ID, job_id),
        )
    assert _count(owned, "omnivia_application_import_claims") == 1


def test_v06_5_s3_origin_audit_is_one_identity_across_metadata_and_job(
    owned: m1.Owned,
) -> None:
    dispatcher, _, started = _start_import(owned, tag="one-origin-audit")
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    observed = dispatcher.dispatch(
        _request(
            "job.get",
            {"job_id": job_id},
            request_id="req-one-origin-audit-get",
        )
    )
    assert isinstance(observed, SuccessResponseEnvelope), observed
    assert observed.metadata.audit_reference is None
    assert observed.metadata.job is None
    assert observed.result["job"]["identity"]["audit_reference"] == (
        started.metadata.audit_reference
    )
    assert _rows(
        owned,
        """
        SELECT a.audit_ref, m.audit_ref, c.audit_ref
        FROM omnivia_application_audit_events a
        JOIN omnivia_job_application_metadata m
          ON m.workspace_id = a.workspace_id AND m.audit_ref = a.audit_ref
        JOIN omnivia_application_import_claims c
          ON c.workspace_id = m.workspace_id AND c.job_id = m.job_id
        WHERE m.job_id = ?
        """,
        job_id,
    ) == [
        (
            started.metadata.audit_reference,
            started.metadata.audit_reference,
            started.metadata.audit_reference,
        )
    ]


def test_v06_5_s3_import_conflict_metadata_uses_original_immutable_job(
    owned: m1.Owned,
) -> None:
    source = _seed_staged_source(owned)
    dispatcher = _dispatcher(owned, tag="immutable-conflict-job")
    request = _import_request(
        source,
        request_id="req-immutable-conflict-primary",
        idempotency_key="idem-immutable-conflict",
    )
    primary = dispatcher.dispatch(request)
    assert isinstance(primary, SuccessResponseEnvelope), primary
    assert primary.metadata.job is not None
    original_job = primary.metadata.job
    original_audit = primary.metadata.audit_reference
    altered = dict(source)
    altered["content_length_bytes"] = 1025
    conflict = dispatcher.dispatch(
        replace(
            request,
            metadata=replace(
                request.metadata,
                request_id="req-immutable-conflict-second",
            ),
            input={"source": altered},
        )
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == "idempotency_conflict"
    assert conflict.metadata.audit_reference == original_audit
    assert conflict.metadata.job == original_job
    assert _rows(
        owned,
        "SELECT job_id, audit_ref FROM omnivia_application_import_claims",
    ) == [(original_job.job_id, original_audit)]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_v06_5_s3_job_get_running_failed_succeeded(
    owned: m1.Owned,
    adapter: str,
) -> None:
    clock = FakeClock(wall=WALL)
    source = _seed_staged_source(owned)
    dispatcher = _dispatcher(
        owned,
        tag=f"parent-get-{adapter}",
        clock=clock,
    )
    _, _, started = _start_import(
        owned,
        tag=f"parent-get-{adapter}",
        source=source,
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id

    running = _transport_call(
        adapter,
        dispatcher,
        _request(
            "job.get",
            {"job_id": job_id},
            request_id=f"req-parent-get-running-{adapter}",
        ),
        case_id="job.get/primary-success",
    )
    assert isinstance(running, SuccessResponseEnvelope), running
    assert running.metadata.job is None
    assert running.result["job"]["state"] == "running"
    assert running.result["job"]["latest_attempt"]["attempt_number"] == 1
    assert "terminal_result" not in running.result

    clock.advance_wall(60)
    failed_wire = _fail_job(owned, clock, job_id)
    assert failed_wire["job"]["state"] == "failed"
    failed = _transport_call(
        adapter,
        dispatcher,
        _request(
            "job.get",
            {"job_id": job_id},
            request_id=f"req-parent-get-failed-{adapter}",
        ),
        case_id="job.get/failed-observation",
    )
    assert isinstance(failed, SuccessResponseEnvelope), failed
    assert failed.result["job"]["state"] == "failed"
    assert failed.result["terminal_result"]["state"] == "failed"
    assert failed.result["terminal_result"]["error"] == RETRYABLE_ERROR
    assert [
        attempt["attempt_number"]
        for attempt in failed.result["terminal_result"]["attempts"]
    ] == [1]

    retried = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id=f"req-parent-get-retry-{adapter}",
            key=f"idem-parent-get-retry-{adapter}",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    assert retried.result["recovery_disposition"] == "retry_scheduled"
    clock.advance_wall(60)
    claimed = _claim_job(owned, clock, job_id)
    assert claimed["job"]["latest_attempt"]["attempt_number"] == 2
    clock.advance_wall(60)
    completed_wire = _complete_job(owned, clock, job_id, source)
    assert completed_wire["job"]["state"] == "succeeded"
    succeeded = _transport_call(
        adapter,
        dispatcher,
        _request(
            "job.get",
            {"job_id": job_id},
            request_id=f"req-parent-get-succeeded-{adapter}",
        ),
        case_id="job.get/succeeded-observation",
    )
    assert isinstance(succeeded, SuccessResponseEnvelope), succeeded
    assert succeeded.result["job"]["state"] == "succeeded"
    terminal = succeeded.result["terminal_result"]
    assert terminal["state"] == "succeeded"
    assert terminal["result_kind"] == "import_completion"
    assert terminal["result"]["source"] == source
    assert [attempt["attempt_number"] for attempt in terminal["attempts"]] == [1, 2]
    assert terminal["attempts"][-1]["state"] == "succeeded"
    assert terminal["identity"]["audit_reference"] == (
        started.metadata.audit_reference
    )


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_v06_5_s3_job_events_primary_and_page_2_ordered(
    owned: m1.Owned,
    adapter: str,
) -> None:
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(
        owned,
        tag=f"parent-events-{adapter}",
        clock=clock,
    )
    _, _, started = _start_import(
        owned,
        tag=f"parent-events-{adapter}",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    clock.advance_wall(10)
    retried = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id=f"req-parent-events-retry-{adapter}",
            key=f"idem-parent-events-retry-{adapter}",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    clock.advance_wall(10)
    _claim_job(owned, clock, job_id)

    first = _transport_call(
        adapter,
        dispatcher,
        _request(
            "job.events",
            {"job_id": job_id, "limit": 2},
            request_id=f"req-parent-events-primary-{adapter}",
        ),
        case_id="job.events/primary-success",
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    assert first.metadata.job is None
    assert first.result["snapshot_event_count"] == 4
    assert [event["sequence"] for event in first.result["events"]] == [0, 1]
    token = first.result["page"]["continuation_token"]
    second = _transport_call(
        adapter,
        dispatcher,
        _request(
            "job.events",
            {
                "job_id": job_id,
                "limit": 2,
                "page": {"continuation_token": token},
            },
            request_id=f"req-parent-events-page2-{adapter}",
        ),
        case_id="job.events/page-2",
    )
    assert isinstance(second, SuccessResponseEnvelope), second
    assert second.result["snapshot_event_count"] == 4
    assert [event["sequence"] for event in second.result["events"]] == [2, 3]
    assert second.result["page"] == {}
    assert _rows(
        owned,
        "SELECT sequence, state FROM omnivia_job_events "
        "WHERE workspace_id = ? AND job_id = ? ORDER BY sequence",
        WORKSPACE_ID,
        job_id,
    ) == [(0, "running"), (1, "failed"), (2, "queued"), (3, "running")]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_v06_5_s3_job_cancel_primary_replay_conflict(
    owned: m1.Owned,
    adapter: str,
) -> None:
    dispatcher, _, started = _start_import(
        owned,
        tag=f"parent-cancel-{adapter}",
        adapter=adapter,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    request = _cancel_request(
        job_id,
        request_id=f"req-parent-cancel-primary-{adapter}",
        key=f"idem-parent-cancel-{adapter}",
    )
    primary = _transport_call(
        adapter, dispatcher, request, case_id="job.cancel/primary-success"
    )
    assert isinstance(primary, SuccessResponseEnvelope), primary
    assert primary.result["cancellation_disposition"] == "cancellation_requested"
    assert primary.result["job"]["state"] == "running"
    assert primary.result["job"]["control"]["cancellation"] == (
        "cancellation_pending"
    )
    assert primary.metadata.job is None

    replay = _transport_call(
        adapter,
        dispatcher,
        replace(
            request,
            metadata=replace(
                request.metadata,
                request_id=f"req-parent-cancel-replay-{adapter}",
            ),
        ),
        case_id="job.cancel/honest-replay",
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == primary.result
    assert replay.metadata.audit_reference == primary.metadata.audit_reference

    conflict = _transport_call(
        adapter,
        dispatcher,
        replace(
            request,
            metadata=replace(
                request.metadata,
                request_id=f"req-parent-cancel-conflict-{adapter}",
            ),
            input={"job_id": f"{job_id}-different"},
        ),
        case_id="job.cancel/idempotency-conflict",
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == "idempotency_conflict"
    assert conflict.metadata.audit_reference == primary.metadata.audit_reference
    assert _count(owned, "omnivia_application_job_controls") == 1
    assert _count(owned, "omnivia_job_events") == 2
    assert _rows(
        owned,
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?",
        job_id,
    ) == [("claimed",)]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_v06_5_s3_job_retry_primary_replay_conflict(
    owned: m1.Owned,
    adapter: str,
) -> None:
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(
        owned,
        tag=f"parent-retry-{adapter}",
        clock=clock,
    )
    _, _, started = _start_import(
        owned,
        tag=f"parent-retry-{adapter}",
        adapter=adapter,
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    request = _retry_request(
        job_id,
        request_id=f"req-parent-retry-primary-{adapter}",
        key=f"idem-parent-retry-{adapter}",
    )
    primary = _transport_call(
        adapter, dispatcher, request, case_id="job.retry/primary-success"
    )
    assert isinstance(primary, SuccessResponseEnvelope), primary
    assert primary.result["recovery_disposition"] == "retry_scheduled"
    assert primary.result["job"]["state"] == "queued"
    assert primary.result["job"]["latest_attempt"]["attempt_number"] == 1

    replay = _transport_call(
        adapter,
        dispatcher,
        replace(
            request,
            metadata=replace(
                request.metadata,
                request_id=f"req-parent-retry-replay-{adapter}",
            ),
        ),
        case_id="job.retry/honest-replay",
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == primary.result
    assert replay.metadata.audit_reference == primary.metadata.audit_reference
    conflict = _transport_call(
        adapter,
        dispatcher,
        replace(
            request,
            metadata=replace(
                request.metadata,
                request_id=f"req-parent-retry-conflict-{adapter}",
            ),
            input={"job_id": f"{job_id}-different"},
        ),
        case_id="job.retry/idempotency-conflict",
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == "idempotency_conflict"
    assert conflict.metadata.audit_reference == primary.metadata.audit_reference
    assert _count(owned, "omnivia_application_job_controls") == 1
    assert _count(owned, "omnivia_job_terminal_observations") == 1
    assert _count(owned, "omnivia_job_attempts") == 1
    assert _rows(
        owned,
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?",
        job_id,
    ) == [("queued",)]


def test_v06_5_s3_cancel_request_is_append_only_and_preserves_running_state(
    owned: m1.Owned,
) -> None:
    dispatcher, _, started = _start_import(owned, tag="append-only-cancel")
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    response = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-append-only-cancel",
            key="idem-append-only-cancel",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result["job"]["state"] == "running"
    assert response.result["job"]["control"]["cancellation"] == (
        "cancellation_pending"
    )
    control = _rows(
        owned,
        """
        SELECT control_id, operation, disposition, source_state, resulting_state,
               audit_ref, fencing_generation
        FROM omnivia_application_job_controls
        WHERE workspace_id = ? AND job_id = ?
        """,
        WORKSPACE_ID,
        job_id,
    )
    assert len(control) == 1
    control_id = control[0][0]
    assert control[0][1:] == (
        "job.cancel",
        "cancellation_requested",
        "running",
        "running",
        response.metadata.audit_reference,
        owned.generation,
    )
    assert _rows(
        owned,
        "SELECT state, claimed_by_service_instance FROM omnivia_durable_jobs "
        "WHERE job_id = ?",
        job_id,
    ) == [("claimed", owned.identity.service_instance_id)]

    for statement in (
        (
            "UPDATE omnivia_application_job_controls SET disposition='cancelled' "
            "WHERE workspace_id=? AND control_id=?"
        ),
        (
            "DELETE FROM omnivia_application_job_controls "
            "WHERE workspace_id=? AND control_id=?"
        ),
    ):
        with (
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
            _guarded(owned),
        ):
            owned.connection.execute(statement, (WORKSPACE_ID, control_id))
    assert _count(owned, "omnivia_application_job_controls") == 1


def test_v06_5_s3_cancel_disposition_and_pending_availability_are_distinct(
    owned: m1.Owned,
) -> None:
    dispatcher, _, started = _start_import(owned, tag="cancel-vocabulary")
    assert started.metadata.job is not None
    result = dispatcher.dispatch(
        _cancel_request(
            started.metadata.job.job_id,
            request_id="req-cancel-vocabulary",
            key="idem-cancel-vocabulary",
        )
    )
    assert isinstance(result, SuccessResponseEnvelope), result
    assert result.result["cancellation_disposition"] == "cancellation_requested"
    assert result.result["job"]["control"] == {
        "cancellation": "cancellation_pending",
        "recovery": "not_retryable",
    }
    assert result.result["job"]["state"] == "running"


def test_v06_5_s3_import_control_is_not_narrowed_by_the_kind_allowlist(
    owned: m1.Owned,
) -> None:
    """The fail-closed kind policy preserves prior import controls."""
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(owned, tag="import-still-controllable", clock=clock)
    _, _, started = _start_import(
        owned,
        tag="import-still-controllable",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id

    running = dispatcher.dispatch(
        _request(
            "job.get",
            {"job_id": job_id},
            request_id="req-import-still-controllable-get",
        )
    )
    assert isinstance(running, SuccessResponseEnvelope), running
    assert running.result["job"]["identity"]["job_kind"] == "ingestion.import"
    assert running.result["job"]["control"]["cancellation"] == "cancellable"

    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    failed = dispatcher.dispatch(
        _request(
            "job.get",
            {"job_id": job_id},
            request_id="req-import-still-controllable-failed",
        )
    )
    assert isinstance(failed, SuccessResponseEnvelope), failed
    assert failed.result["job"]["control"]["recovery"] == "retryable"

    clock.advance_wall(10)
    retried = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-import-still-controllable-retry",
            key="idem-import-still-controllable-retry",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    assert retried.result["recovery_disposition"] == "retry_scheduled"
    assert retried.result["job"]["state"] == "queued"


def test_v06_5_s3_job_read_is_workspace_scoped_and_snapshot_coherent(
    owned: m1.Owned,
) -> None:
    dispatcher, _, started = _start_import(owned, tag="snapshot-read")
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)
    try:
        response = dispatcher.dispatch(
            _request(
                "job.get",
                {"job_id": job_id},
                request_id="req-snapshot-read",
            )
        )
    finally:
        owned.connection.set_trace_callback(None)
    assert isinstance(response, SuccessResponseEnvelope), response
    normalized = [" ".join(statement.split()) for statement in statements]
    assert sum(statement == "BEGIN" for statement in normalized) == 1
    assert sum(statement == "COMMIT" for statement in normalized) == 1
    begin_index = normalized.index("BEGIN")
    commit_index = normalized.index("COMMIT")
    reads = normalized[begin_index + 1 : commit_index]
    assert any("omnivia_durable_jobs" in statement for statement in reads)
    assert any("omnivia_job_application_metadata" in statement for statement in reads)
    assert any("omnivia_application_import_claims" in statement for statement in reads)
    assert any("omnivia_job_attempts" in statement for statement in reads)
    assert any("omnivia_job_events" in statement for statement in reads)

    foreign_dispatcher = _dispatcher(
        owned,
        tag="foreign-workspace-read",
        workspace_id="ws-foreign-s3",
    )
    foreign = foreign_dispatcher.dispatch(
        _request(
            "job.get",
            {"job_id": job_id},
            request_id="req-foreign-workspace-read",
            workspace_id="ws-foreign-s3",
        )
    )
    assert isinstance(foreign, ErrorResponseEnvelope), foreign
    assert foreign.error.code == "not_found"
    assert job_id not in foreign.error.message


def test_v06_5_s3_event_token_binds_principal_job_and_snapshot(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    codec = HmacContinuationTokenCodec(b"s3-shared-binding-secret")
    dispatcher = _dispatcher(
        owned,
        tag="event-token",
        clock=clock,
        token_codec=codec,
    )
    _, _, started = _start_import(
        owned,
        tag="event-token",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    clock.advance_wall(10)
    retried = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-event-token-retry",
            key="idem-event-token-retry",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    clock.advance_wall(10)
    _claim_job(owned, clock, job_id)
    first = dispatcher.dispatch(
        _request(
            "job.events",
            {"job_id": job_id, "limit": 2},
            request_id="req-event-token-first",
        )
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    assert first.result["snapshot_event_count"] == 4
    token = first.result["page"]["continuation_token"]
    assert len(token) <= 512

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    invalid = dispatcher.dispatch(
        _request(
            "job.events",
            {
                "job_id": job_id,
                "limit": 2,
                "page": {"continuation_token": tampered},
            },
            request_id="req-event-token-tampered",
        )
    )
    assert isinstance(invalid, ErrorResponseEnvelope)
    assert invalid.error.code == "invalid_request"

    wrong_job = dispatcher.dispatch(
        _request(
            "job.events",
            {
                "job_id": f"{job_id}-other",
                "limit": 2,
                "page": {"continuation_token": token},
            },
            request_id="req-event-token-wrong-job",
        )
    )
    assert isinstance(wrong_job, ErrorResponseEnvelope)
    assert wrong_job.error.code == "invalid_request"

    foreign_workspace = _dispatcher(
        owned,
        tag="event-token-foreign-workspace",
        workspace_id="ws-s3-foreign",
        clock=clock,
        token_codec=codec,
    )
    wrong_workspace = foreign_workspace.dispatch(
        _request(
            "job.events",
            {
                "job_id": job_id,
                "limit": 2,
                "page": {"continuation_token": token},
            },
            request_id="req-event-token-wrong-workspace",
            workspace_id="ws-s3-foreign",
        )
    )
    assert isinstance(wrong_workspace, ErrorResponseEnvelope)
    assert wrong_workspace.error.code == "invalid_request"

    foreign_principal = _dispatcher(
        owned,
        tag="event-token-foreign-principal",
        principal="principal-s3-foreign",
        clock=clock,
        token_codec=codec,
    )
    wrong_principal = foreign_principal.dispatch(
        _request(
            "job.events",
            {
                "job_id": job_id,
                "limit": 2,
                "page": {"continuation_token": token},
            },
            request_id="req-event-token-wrong-principal",
        )
    )
    assert isinstance(wrong_principal, ErrorResponseEnvelope)
    assert wrong_principal.error.code == "invalid_request"

    clock.advance_wall(10)
    _complete_job(owned, clock, job_id, _source())
    stable_page_2 = dispatcher.dispatch(
        _request(
            "job.events",
            {
                "job_id": job_id,
                "limit": 2,
                "page": {"continuation_token": token},
            },
            request_id="req-event-token-stable-page2",
        )
    )
    assert isinstance(stable_page_2, SuccessResponseEnvelope), stable_page_2
    assert stable_page_2.result["snapshot_event_count"] == 4
    assert [event["sequence"] for event in stable_page_2.result["events"]] == [2, 3]
    assert stable_page_2.result["page"] == {}


def test_v06_5_s3_response_job_metadata_is_async_only(owned: m1.Owned) -> None:
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(owned, tag="async-metadata", clock=clock)
    _, _, started = _start_import(
        owned,
        tag="async-metadata",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    synchronous: list[ResponseEnvelope] = [
        dispatcher.dispatch(
            _request(
                "job.get",
                {"job_id": job_id},
                request_id="req-async-metadata-get",
            )
        ),
        dispatcher.dispatch(
            _request(
                "job.events",
                {"job_id": job_id},
                request_id="req-async-metadata-events",
            )
        ),
        dispatcher.dispatch(
            _cancel_request(
                job_id,
                request_id="req-async-metadata-cancel",
                key="idem-async-metadata-cancel",
            )
        ),
    ]
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    synchronous.append(
        dispatcher.dispatch(
            _retry_request(
                job_id,
                request_id="req-async-metadata-retry",
                key="idem-async-metadata-retry",
            )
        )
    )
    assert all(isinstance(response, SuccessResponseEnvelope) for response in synchronous)
    assert all(response.metadata.job is None for response in synchronous)


def test_v06_5_s3_crash_resume_no_duplicate_commit(owned: m1.Owned) -> None:
    clock = FakeClock(wall=WALL)
    _, _, started = _start_import(
        owned,
        tag="parent-crash-recovery",
        dispatcher=_dispatcher(owned, tag="parent-crash-recovery", clock=clock),
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    successor = _takeover(owned, clock=clock)
    recovered = recover_stranded_application_jobs(
        successor.connection,
        successor.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=successor.generation,
        now_us=int(clock.wall_time().timestamp() * 1_000_000),
        clock=clock,
    )
    assert len(recovered) == 1
    assert recovered[0]["job"]["identity"]["job_id"] == job_id
    assert recovered[0]["job"]["state"] == "queued"
    assert recovered[0]["job"]["latest_attempt"]["state"] == "failed"
    assert recover_stranded_application_jobs(
        successor.connection,
        successor.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=successor.generation,
        now_us=int(clock.wall_time().timestamp() * 1_000_000),
        clock=clock,
    ) == []
    assert _count(successor, "omnivia_job_terminal_observations") == 1
    assert _count(successor, "omnivia_application_job_controls") == 1

    clock.advance_wall(10)
    claimed = _claim_job(successor, clock, job_id)
    assert claimed["job"]["latest_attempt"]["attempt_number"] == 2
    clock.advance_wall(10)
    completed = _complete_job(successor, clock, job_id, _source())
    assert completed["job"]["state"] == "succeeded"
    assert [
        attempt["attempt_number"]
        for attempt in completed["terminal_result"]["attempts"]
    ] == [1, 2]
    assert _count(successor, "omnivia_job_terminal_observations") == 2
    with pytest.raises((JobError, sqlite3.DatabaseError, RuntimeError, ValueError)):
        _complete_job(successor, clock, job_id, _source())
    assert _count(successor, "omnivia_job_terminal_observations") == 2


def test_v06_5_s3_recovery_terminalises_stranded_attempt_once(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    _, _, started = _start_import(
        owned,
        tag="stranded-once",
        dispatcher=_dispatcher(owned, tag="stranded-once", clock=clock),
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(15)
    successor = _takeover(owned, clock=clock)
    first = recover_stranded_application_jobs(
        successor.connection,
        successor.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=successor.generation,
        now_us=int(clock.wall_time().timestamp() * 1_000_000),
        clock=clock,
    )
    second = recover_stranded_application_jobs(
        successor.connection,
        successor.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=successor.generation,
        now_us=int(clock.wall_time().timestamp() * 1_000_000),
        clock=clock,
    )
    assert len(first) == 1
    assert second == []
    attempts = _rows(
        successor,
        "SELECT attempt_number, state, error_json FROM omnivia_job_attempts "
        "WHERE workspace_id=? AND job_id=?",
        WORKSPACE_ID,
        job_id,
    )
    assert len(attempts) == 1
    assert attempts[0][:2] == (1, "failed")
    recovery_error = json.loads(str(attempts[0][2]))
    assert recovery_error["code"] == "internal_recoverable"
    assert recovery_error["retry_class"] == "retryable"
    assert isinstance(recovery_error["message"], str)
    assert recovery_error["message"]
    assert _rows(
        successor,
        """
        SELECT terminal_observation_number, attempt_number, terminal_state,
               provenance_kind, fencing_generation
        FROM omnivia_job_terminal_observations
        WHERE workspace_id=? AND job_id=?
        """,
        WORKSPACE_ID,
        job_id,
    ) == [(1, 1, "failed", "service_committed", successor.generation)]
    assert _rows(
        successor,
        "SELECT sequence, state FROM omnivia_job_events "
        "WHERE workspace_id=? AND job_id=? ORDER BY sequence",
        WORKSPACE_ID,
        job_id,
    ) == [(0, "running"), (1, "failed"), (2, "queued")]


def test_v06_5_s3_crash_recovery_records_system_lineage_before_next_attempt(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    _, _, started = _start_import(
        owned,
        tag="system-lineage",
        dispatcher=_dispatcher(owned, tag="system-lineage", clock=clock),
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(20)
    successor = _takeover(owned, clock=clock)
    recovered = recover_stranded_application_jobs(
        successor.connection,
        successor.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=successor.generation,
        now_us=int(clock.wall_time().timestamp() * 1_000_000),
        clock=clock,
    )
    assert len(recovered) == 1
    assert _rows(
        successor,
        """
        SELECT control_kind, operation, disposition, source_state,
               resulting_state, source_terminal_observation_number, audit_ref,
               fencing_generation
        FROM omnivia_application_job_controls
        WHERE workspace_id=? AND job_id=?
        """,
        WORKSPACE_ID,
        job_id,
    ) == [
        (
            "system",
            "system.recovery",
            "recovery_requeued",
            "running",
            "queued",
            None,
            None,
            successor.generation,
        )
    ]
    before_claim = _count(successor, "omnivia_job_attempts")
    assert before_claim == 1
    clock.advance_wall(1)
    claimed = _claim_job(successor, clock, job_id)
    assert claimed["job"]["latest_attempt"]["attempt_number"] == 2
    assert _count(successor, "omnivia_job_attempts") == 2


def test_v06_5_s3_cancel_retry_generation_and_audit(owned: m1.Owned) -> None:
    old_clock = FakeClock(wall=WALL)
    old_dispatcher = _dispatcher(owned, tag="generation-old", clock=old_clock)
    _, _, started = _start_import(
        owned,
        tag="generation-old",
        dispatcher=old_dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    _checkpoint(owned, job_id=job_id, at_us=WALL_US)
    old_clock.advance_wall(10)
    cancelled = old_dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-generation-cancel",
            key="idem-generation-cancel",
        )
    )
    assert isinstance(cancelled, SuccessResponseEnvelope), cancelled
    old_clock.advance_wall(10)
    acknowledged = _acknowledge_cancellation(owned, old_clock, job_id)
    assert acknowledged["job"]["state"] == "cancelled"

    old_generation = owned.generation
    old_clock.advance_wall(10)
    successor = _takeover(owned, clock=old_clock)
    new_dispatcher = _dispatcher(
        successor,
        tag="generation-new",
        clock=old_clock,
    )
    retried = new_dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-generation-retry",
            key="idem-generation-retry",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    assert retried.result["recovery_disposition"] == "resume_scheduled"
    assert successor.generation > old_generation
    controls = _rows(
        successor,
        """
        SELECT operation, audit_ref, fencing_generation,
               source_terminal_observation_number
        FROM omnivia_application_job_controls
        WHERE workspace_id=? AND job_id=? AND control_kind='user'
        ORDER BY settled_at_us
        """,
        WORKSPACE_ID,
        job_id,
    )
    assert controls == [
        ("job.cancel", cancelled.metadata.audit_reference, old_generation, None),
        ("job.retry", retried.metadata.audit_reference, successor.generation, 1),
    ]
    assert _rows(
        successor,
        "SELECT attempt_number, state FROM omnivia_job_attempts "
        "WHERE workspace_id=? AND job_id=? ORDER BY attempt_number",
        WORKSPACE_ID,
        job_id,
    ) == [(1, "cancelled")]


def test_v06_5_s3_retry_uses_fresh_grant_generation_and_preserves_attempt_history(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    old_dispatcher = _dispatcher(owned, tag="fresh-retry-old", clock=clock)
    _, _, started = _start_import(
        owned,
        tag="fresh-retry-old",
        dispatcher=old_dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    origin_generation = owned.generation
    clock.advance_wall(10)
    successor = _takeover(owned, clock=clock)
    new_dispatcher = _dispatcher(successor, tag="fresh-retry-new", clock=clock)
    retried = new_dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-fresh-generation-retry",
            key="idem-fresh-generation-retry",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    assert retried.result["recovery_disposition"] == "retry_scheduled"
    assert successor.generation > origin_generation
    assert _rows(
        successor,
        """
        SELECT c.fencing_generation, e.fencing_generation,
               c.source_terminal_observation_number
        FROM omnivia_application_job_controls c
        JOIN omnivia_mutation_executions e
          ON e.workspace_id=c.workspace_id AND e.audit_ref=c.audit_ref
         AND e.operation=c.operation
        WHERE c.workspace_id=? AND c.job_id=? AND c.operation='job.retry'
        """,
        WORKSPACE_ID,
        job_id,
    ) == [(successor.generation, successor.generation, 1)]
    assert _rows(
        successor,
        "SELECT attempt_number, state, finished_at_us FROM omnivia_job_attempts "
        "WHERE workspace_id=? AND job_id=?",
        WORKSPACE_ID,
        job_id,
    )[0][:2] == (1, "failed")
    assert retried.result["job"]["latest_attempt"]["attempt_number"] == 1
    assert retried.result["job"]["latest_attempt"]["state"] == "failed"


def test_v06_5_s3_current_terminal_projection_disappears_while_recovered(
    owned: m1.Owned,
) -> None:
    _, dispatcher, job_id, failed, retried = _fail_and_retry(
        owned,
        tag="terminal-disappears",
    )
    assert failed["terminal_result"]["state"] == "failed"
    assert "terminal_result" not in retried.result
    observed = dispatcher.dispatch(
        _request(
            "job.get",
            {"job_id": job_id},
            request_id="req-terminal-disappears-get",
        )
    )
    assert isinstance(observed, SuccessResponseEnvelope), observed
    assert observed.result["job"]["state"] == "queued"
    assert observed.result["job"]["latest_attempt"]["state"] == "failed"
    assert "terminal_result" not in observed.result
    assert _count(owned, "omnivia_job_terminal_observations") == 1


def test_v06_5_s3_retry_binds_exact_latest_terminal_observation_once(
    owned: m1.Owned,
) -> None:
    clock, dispatcher, job_id, _, first_retry = _fail_and_retry(
        owned,
        tag="bind-terminal-once",
    )
    replay = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-bind-terminal-once-replay",
            key="idem-bind-terminal-once-retry",
        )
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first_retry.result
    assert _count(owned, "omnivia_application_job_controls") == 1

    clock.advance_wall(10)
    _claim_job(owned, clock, job_id)
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    clock.advance_wall(10)
    second_retry = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-bind-terminal-second-retry",
            key="idem-bind-terminal-second-retry",
        )
    )
    assert isinstance(second_retry, SuccessResponseEnvelope), second_retry
    assert second_retry.result["recovery_disposition"] == "retry_scheduled"
    assert _rows(
        owned,
        """
        SELECT source_terminal_observation_number
        FROM omnivia_application_job_controls
        WHERE workspace_id=? AND job_id=? AND operation='job.retry'
        ORDER BY settled_at_us
        """,
        WORKSPACE_ID,
        job_id,
    ) == [(1,), (2,)]


def test_v06_5_s3_failed_retry_attempt_2_succeeded_is_append_only(
    owned: m1.Owned,
) -> None:
    clock, _, job_id, _, _ = _fail_and_retry(
        owned,
        tag="attempt-two-success",
    )
    clock.advance_wall(10)
    _claim_job(owned, clock, job_id)
    clock.advance_wall(10)
    completed = _complete_job(owned, clock, job_id, _source())
    terminal = completed["terminal_result"]
    assert terminal["state"] == "succeeded"
    assert [attempt["state"] for attempt in terminal["attempts"]] == [
        "failed",
        "succeeded",
    ]
    assert [attempt["attempt_number"] for attempt in terminal["attempts"]] == [1, 2]
    observations = _rows(
        owned,
        """
        SELECT terminal_observation_number, attempt_number, terminal_state,
               provenance_kind, fencing_generation
        FROM omnivia_job_terminal_observations
        WHERE workspace_id=? AND job_id=? ORDER BY terminal_observation_number
        """,
        WORKSPACE_ID,
        job_id,
    )
    assert observations == [
        (1, 1, "failed", "service_committed", owned.generation),
        (2, 2, "succeeded", "service_committed", owned.generation),
    ]


def test_v06_5_s3_cancelled_resume_uses_resume_scheduled(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(owned, tag="cancelled-resume", clock=clock)
    _, _, started = _start_import(
        owned,
        tag="cancelled-resume",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    _checkpoint(owned, job_id=job_id, at_us=WALL_US)
    clock.advance_wall(10)
    cancel = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-cancelled-resume-cancel",
            key="idem-cancelled-resume-cancel",
        )
    )
    assert isinstance(cancel, SuccessResponseEnvelope), cancel
    clock.advance_wall(10)
    acknowledged = _acknowledge_cancellation(owned, clock, job_id)
    assert acknowledged["job"]["state"] == "cancelled"
    clock.advance_wall(10)
    retry = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-cancelled-resume-retry",
            key="idem-cancelled-resume-retry",
        )
    )
    assert isinstance(retry, SuccessResponseEnvelope), retry
    assert retry.result["recovery_disposition"] == "resume_scheduled"
    assert retry.result["job"]["state"] == "queued"
    assert _rows(
        owned,
        "SELECT disposition, source_terminal_observation_number "
        "FROM omnivia_application_job_controls "
        "WHERE workspace_id=? AND job_id=? AND operation='job.retry'",
        WORKSPACE_ID,
        job_id,
    ) == [("resume_scheduled", 1)]


def test_v06_5_s3_nonresumable_cancelled_job_is_not_recovered(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(owned, tag="cancelled-no-checkpoint", clock=clock)
    _, _, started = _start_import(
        owned,
        tag="cancelled-no-checkpoint",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    cancel = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-cancelled-no-checkpoint-cancel",
            key="idem-cancelled-no-checkpoint-cancel",
        )
    )
    assert isinstance(cancel, SuccessResponseEnvelope), cancel
    clock.advance_wall(10)
    _acknowledge_cancellation(owned, clock, job_id)
    clock.advance_wall(10)
    retry = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-cancelled-no-checkpoint-retry",
            key="idem-cancelled-no-checkpoint-retry",
        )
    )
    assert isinstance(retry, SuccessResponseEnvelope), retry
    assert retry.result["recovery_disposition"] == "not_retryable"
    assert retry.result["job"]["state"] == "cancelled"
    controls = _rows(
        owned,
        "SELECT operation, disposition FROM omnivia_application_job_controls "
        "WHERE workspace_id=? AND job_id=? ORDER BY settled_at_us",
        WORKSPACE_ID,
        job_id,
    )
    assert controls[0] == ("job.cancel", "cancellation_requested")
    assert not any(
        operation == "job.retry"
        and disposition in {"retry_scheduled", "resume_scheduled"}
        for operation, disposition in controls
    )


def test_v06_5_s3_queued_cancel_after_retry_has_attemptless_terminal_observation(
    owned: m1.Owned,
) -> None:
    # Amendment 005's final implementation correction supersedes the test name's
    # provisional representation: prior history requires a real next cancelled attempt.
    clock, dispatcher, job_id, _, _ = _fail_and_retry(
        owned,
        tag="queued-cancel-corrected",
    )
    clock.advance_wall(10)
    requested = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-queued-cancel-corrected",
            key="idem-queued-cancel-corrected",
        )
    )
    assert isinstance(requested, SuccessResponseEnvelope), requested
    assert requested.result["job"]["state"] == "queued"
    assert requested.result["job"]["control"]["cancellation"] == (
        "cancellation_pending"
    )
    clock.advance_wall(10)
    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)
    try:
        cancelled = _acknowledge_cancellation(owned, clock, job_id)
    finally:
        owned.connection.set_trace_callback(None)
    normalized = [" ".join(statement.split()) for statement in statements]
    assert sum(statement.startswith("BEGIN") for statement in normalized) == 1, normalized
    assert sum(statement == "COMMIT" for statement in normalized) == 1, normalized
    assert cancelled["job"]["state"] == "cancelled"
    assert cancelled["job"]["latest_attempt"]["attempt_number"] == 2
    assert cancelled["job"]["latest_attempt"]["state"] == "cancelled"
    assert _rows(
        owned,
        """
        SELECT terminal_observation_number, attempt_number, terminal_state
        FROM omnivia_job_terminal_observations
        WHERE workspace_id=? AND job_id=? ORDER BY terminal_observation_number
        """,
        WORKSPACE_ID,
        job_id,
    ) == [(1, 1, "failed"), (2, 2, "cancelled")]


def test_v06_5_s3_resume_after_attemptless_cancel_starts_next_contiguous_attempt(
    owned: m1.Owned,
) -> None:
    # The accepted correction means the queued acknowledgement owns attempt 2;
    # resumption therefore starts attempt 3, without rewriting either predecessor.
    clock, dispatcher, job_id, _, _ = _fail_and_retry(
        owned,
        tag="resume-corrected-cancel",
        checkpoint=True,
    )
    clock.advance_wall(10)
    requested = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-resume-corrected-cancel",
            key="idem-resume-corrected-cancel",
        )
    )
    assert isinstance(requested, SuccessResponseEnvelope), requested
    clock.advance_wall(10)
    _acknowledge_cancellation(owned, clock, job_id)
    clock.advance_wall(10)
    resumed = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-resume-corrected-retry",
            key="idem-resume-corrected-retry",
        )
    )
    assert isinstance(resumed, SuccessResponseEnvelope), resumed
    assert resumed.result["recovery_disposition"] == "resume_scheduled"
    clock.advance_wall(10)
    claimed = _claim_job(owned, clock, job_id)
    assert claimed["job"]["latest_attempt"]["attempt_number"] == 3
    assert _rows(
        owned,
        "SELECT attempt_number, state FROM omnivia_job_attempts "
        "WHERE workspace_id=? AND job_id=? ORDER BY attempt_number",
        WORKSPACE_ID,
        job_id,
    ) == [(1, "failed"), (2, "cancelled"), (3, "running")]
    assert _rows(
        owned,
        "SELECT source_terminal_observation_number FROM "
        "omnivia_application_job_controls WHERE workspace_id=? AND job_id=? "
        "AND operation='job.retry' ORDER BY settled_at_us",
        WORKSPACE_ID,
        job_id,
    ) == [(1,), (2,)]


def test_v06_5_s3_attemptless_cancel_cannot_claim_an_old_attempt(
    owned: m1.Owned,
) -> None:
    clock, dispatcher, job_id, _, _ = _fail_and_retry(
        owned,
        tag="cancel-does-not-claim-old",
    )
    clock.advance_wall(10)
    requested = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-cancel-does-not-claim-old",
            key="idem-cancel-does-not-claim-old",
        )
    )
    assert isinstance(requested, SuccessResponseEnvelope), requested
    clock.advance_wall(10)
    _acknowledge_cancellation(owned, clock, job_id)
    observations = _rows(
        owned,
        "SELECT terminal_observation_number, attempt_number "
        "FROM omnivia_job_terminal_observations "
        "WHERE workspace_id=? AND job_id=? ORDER BY terminal_observation_number",
        WORKSPACE_ID,
        job_id,
    )
    assert observations == [(1, 1), (2, 2)]
    assert len({attempt for _, attempt in observations}) == 2
    assert all(attempt is not None for _, attempt in observations)


def test_v06_5_s3_attemptless_terminal_requires_cancel_and_recovery_lineage(
    owned: m1.Owned,
) -> None:
    clock, dispatcher, job_id, _, _ = _fail_and_retry(
        owned,
        tag="cancel-lineage-required",
    )
    clock.advance_wall(10)
    with pytest.raises((JobError, sqlite3.DatabaseError, RuntimeError, ValueError)):
        _acknowledge_cancellation(owned, clock, job_id)
    assert _count(owned, "omnivia_job_attempts") == 1
    assert _count(owned, "omnivia_job_terminal_observations") == 1

    requested = dispatcher.dispatch(
        _cancel_request(
            job_id,
            request_id="req-cancel-lineage-required",
            key="idem-cancel-lineage-required",
        )
    )
    assert isinstance(requested, SuccessResponseEnvelope), requested
    clock.advance_wall(10)
    _acknowledge_cancellation(owned, clock, job_id)
    assert _count(owned, "omnivia_job_attempts") == 2
    assert _count(owned, "omnivia_job_terminal_observations") == 2
    assert _rows(
        owned,
        "SELECT operation, disposition FROM omnivia_application_job_controls "
        "WHERE workspace_id=? AND job_id=? ORDER BY settled_at_us",
        WORKSPACE_ID,
        job_id,
    ) == [
        ("job.retry", "retry_scheduled"),
        ("job.cancel", "cancellation_requested"),
    ]


def test_v06_5_s3_recovered_event_and_attempt_require_accepted_control(
    owned: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    dispatcher = _dispatcher(owned, tag="control-required", clock=clock)
    _, _, started = _start_import(
        owned,
        tag="control-required",
        dispatcher=dispatcher,
    )
    assert started.metadata.job is not None
    job_id = started.metadata.job.job_id
    clock.advance_wall(10)
    _fail_job(owned, clock, job_id)
    with _guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='queued', "
            "claimed_by_service_instance=NULL WHERE job_id=?",
            (job_id,),
        )
    clock.advance_wall(10)
    with pytest.raises(sqlite3.DatabaseError, match="recovery lineage"):
        claim_application_job(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            clock=clock,
            job_id=job_id,
        )
    assert _count(owned, "omnivia_job_attempts") == 1
    assert _count(owned, "omnivia_job_events") == 2

    with _guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state='failed' WHERE job_id=?",
            (job_id,),
        )
    retried = dispatcher.dispatch(
        _retry_request(
            job_id,
            request_id="req-control-required-retry",
            key="idem-control-required-retry",
        )
    )
    assert isinstance(retried, SuccessResponseEnvelope), retried
    clock.advance_wall(10)
    claimed = _claim_job(owned, clock, job_id)
    assert claimed["job"]["latest_attempt"]["attempt_number"] == 2


def test_v06_5_s3_terminal_observation_is_append_only_and_contiguous(
    owned: m1.Owned,
) -> None:
    clock, _, job_id, _, _ = _fail_and_retry(
        owned,
        tag="terminal-append-only",
    )
    clock.advance_wall(10)
    _claim_job(owned, clock, job_id)
    clock.advance_wall(10)
    _complete_job(owned, clock, job_id, _source())
    assert _rows(
        owned,
        "SELECT terminal_observation_number, attempt_number, terminal_state "
        "FROM omnivia_job_terminal_observations "
        "WHERE workspace_id=? AND job_id=? ORDER BY terminal_observation_number",
        WORKSPACE_ID,
        job_id,
    ) == [(1, 1, "failed"), (2, 2, "succeeded")]
    for statement in (
        (
            "UPDATE omnivia_job_terminal_observations SET terminal_state='cancelled' "
            "WHERE workspace_id=? AND job_id=? AND terminal_observation_number=1"
        ),
        (
            "DELETE FROM omnivia_job_terminal_observations WHERE workspace_id=? "
            "AND job_id=? AND terminal_observation_number=1"
        ),
    ):
        with (
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
            _guarded(owned),
        ):
            owned.connection.execute(statement, (WORKSPACE_ID, job_id))
    assert _count(owned, "omnivia_job_terminal_observations") == 2
