"""ADR-040 conformance for Runtime's independent OVC1 implementation."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service.ovc1 import (
    HEADER_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    OVC1Error,
    canonical_json_bytes,
    decode_frame,
    encode_frame,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    codec,
    decode_evidence_capture_input,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.canonical_json import canonical_bytes

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "omnivia-core-client"
    / "tests"
    / "fixtures"
    / "ovc1-v1.json"
)
FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
ACCEPTED = FIXTURE["vectors"] + FIXTURE["canonicalization_vectors"]


def _frame(body: bytes, *, magic: bytes = MAGIC, declared: int | None = None) -> bytes:
    length = len(body) if declared is None else declared
    return magic + length.to_bytes(4, "big") + body


def test_runtime_recomputes_every_accepted_canonical_fixture_vector() -> None:
    assert MAGIC == b"OVC1"
    assert HEADER_BYTES == 8
    assert MAXIMUM_JSON_BYTES == 8 * 1024 * 1024

    for vector in ACCEPTED:
        payload = vector["payload"]
        body = canonical_json_bytes(payload)
        frame = encode_frame(payload)
        assert body == canonical_bytes(payload)
        assert body.hex() == vector["canonical_json_hex"], vector["id"]
        assert frame.hex() == vector["frame_hex"], vector["id"]
        assert decode_frame(frame) == payload, vector["id"]


def test_runtime_refuses_every_decode_rejection_fixture_vector() -> None:
    for vector in FIXTURE["rejected_vectors"]:
        with pytest.raises(OVC1Error), pytest.MonkeyPatch.context():
            decode_frame(bytes.fromhex(vector["frame_hex"]))


def test_runtime_refuses_every_encoder_rejection_fixture_vector() -> None:
    for vector in FIXTURE["encoder_rejected_vectors"]:
        with pytest.raises(OVC1Error):
            encode_frame(vector["payload"])


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"OVC1",
        b"NOPE\x00\x00\x00\x02{}",
        b"OVC1\x00\x00\x00\x00",
        b"OVC1\x00\x40\x00\x01",
        b"OVC1\x00\x00\x00\x02{",
        b"OVC1\x00\x00\x00\x02{}x",
    ],
)
def test_runtime_refuses_malformed_header_length_truncation_and_trailing_bytes(
    frame: bytes,
) -> None:
    with pytest.raises(OVC1Error):
        decode_frame(frame)


def test_eight_mibibyte_body_is_inclusive_and_one_byte_more_is_refused() -> None:
    maximum = {"a": "x" * (MAXIMUM_JSON_BYTES - len(b'{"a":""}'))}
    frame = encode_frame(maximum)
    assert len(frame) == HEADER_BYTES + MAXIMUM_JSON_BYTES
    assert decode_frame(frame) == maximum

    oversized = {"a": maximum["a"] + "x"}
    with pytest.raises(OVC1Error, match="maximum"):
        encode_frame(oversized)


def test_runtime_frames_the_worst_case_valid_capture_envelope() -> None:
    entry = get_operation_metadata("evidence.capture")
    required = entry.required_capability
    request = RequestEnvelope(
        operation=entry.name,
        metadata=RequestMetadata(
            request_id="req-runtime-frame-capacity",
            correlation_id="cor-runtime-frame-capacity",
            trace_id="trc-runtime-frame-capacity",
            api_version=CONTRACT_VERSION,
            client=ClientIdentity(id="runtime-frame-test", version="1.0.0"),
            workspace_id="ws-runtime-frame",
            scopes=tuple(entry.scope.required_scopes),
            purpose="content_ingestion",
            idempotency_key="idem-runtime-frame-capacity",
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=required.required,
                ),
            ),
        ),
        input={
            "source_native_id": "worst-json-escape",
            "media_type": "text/plain",
            "text": "\x00" * EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
        },
    )
    document = codec.encode_request(request)
    decode_evidence_capture_input(document["input"])

    frame = encode_frame(document)

    assert HEADER_BYTES + 6 * EVIDENCE_CAPTURE_MAX_CONTENT_BYTES < len(frame)
    assert len(frame) <= HEADER_BYTES + MAXIMUM_JSON_BYTES
    assert codec.decode_request(decode_frame(frame)) == request


def test_protocol_failures_do_not_reveal_payload_or_parser_exception_chains() -> None:
    secret = "credential=hunter2 /Users/alice/workspace.sqlite"
    malformed = _frame(('{"secret":"' + secret).encode())

    with pytest.raises(OVC1Error) as caught:
        decode_frame(malformed)

    rendered = "".join(
        (
            str(caught.value),
            repr(caught.value.args),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert secret not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
