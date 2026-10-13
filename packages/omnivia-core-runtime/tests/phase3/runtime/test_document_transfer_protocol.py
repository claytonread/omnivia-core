"""The local binary document-stage protocol stays separate from frozen OVC1."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

from omnivia_core_client import Deadline, LocalIpcTransport
from omnivia_core_runtime.service.ovc1 import HEADER_BYTES, decode_frame, encode_frame
from omnivia_core_runtime.service.transport import LocalSocketServer


class Channel:
    def __init__(self, request: bytes) -> None:
        self.request = request
        self.sent: bytes | None = None

    def read_frame(self, *, limit: int = 0) -> bytes:
        del limit
        return self.request

    def ensure_unary_boundary(self) -> None:
        return None

    def send_frame(self, payload: bytes) -> None:
        self.sent = payload

    def close(self) -> None:
        return None


def request(content: bytes, *, checksum: str | None = None) -> bytes:
    digest = checksum or "sha256:" + hashlib.sha256(content).hexdigest()
    header = encode_frame(
        {
            "version": "1.0",
            "media_type": "application/pdf",
            "transfer_key": "capture-stage-1",
            "content_checksum": digest,
        }
    )[HEADER_BYTES:]
    return b"OVB1" + len(header).to_bytes(4, "big") + len(content).to_bytes(8, "big") + header + content


def server(callback: Any) -> LocalSocketServer:
    # No endpoint is opened in this unit test; path only satisfies construction.
    return LocalSocketServer(
        dispatcher=cast(Any, object()),
        path=Path("/private/tmp/unused-document-transfer.sock"),
        binary_stage=callback,
    )


def test_binary_stage_hands_exact_bytes_and_lineage_to_the_service() -> None:
    observed: list[tuple[bytes, str, str]] = []

    def stage(content: bytes, media_type: str, transfer_key: str) -> dict[str, Any]:
        observed.append((content, media_type, transfer_key))
        return {
            "staged_source_ref": "stg-document-1",
            "source_kind": "document",
            "content_checksum": "sha256:" + hashlib.sha256(content).hexdigest(),
            "content_length_bytes": len(content),
            "media_type": media_type,
        }

    channel = Channel(request(b"binary-pdf"))
    server(stage)._handle(cast(Any, channel))

    assert observed == [(b"binary-pdf", "application/pdf", "capture-stage-1")]
    assert channel.sent is not None
    answer = decode_frame(channel.sent)
    assert answer["ok"] is True
    assert answer["source"]["staged_source_ref"] == "stg-document-1"


def test_binary_stage_refuses_a_digest_mismatch_without_calling_service() -> None:
    called = False

    def stage(_content: bytes, _media_type: str, _transfer_key: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    channel = Channel(request(b"binary-pdf", checksum="sha256:" + "0" * 64))
    server(stage)._handle(cast(Any, channel))

    assert not called
    assert channel.sent is not None
    assert decode_frame(channel.sent) == {"ok": False, "code": "transfer_refused"}


def test_client_and_server_exchange_a_real_binary_stage() -> None:
    directory = Path(tempfile.mkdtemp(prefix="ovb-", dir="/private/tmp"))
    path = directory / "s.sock"
    observed: list[bytes] = []

    def stage(content: bytes, media_type: str, transfer_key: str) -> dict[str, Any]:
        observed.append(content)
        return {
            "staged_source_ref": "stg-document-real",
            "source_kind": "document",
            "content_checksum": "sha256:" + hashlib.sha256(content).hexdigest(),
            "content_length_bytes": len(content),
            "media_type": media_type,
        }

    local = LocalSocketServer(
        dispatcher=cast(Any, object()),
        path=path,
        binary_stage=stage,
    )
    try:
        local.start()
        descriptor = LocalIpcTransport(f"unix://{path}").stage_document(
            b"real-binary-transfer",
            media_type="application/pdf",
            transfer_key="capture-stage-real",
            deadline=Deadline.after(5),
        )
    finally:
        local.stop()
        shutil.rmtree(directory)

    assert observed == [b"real-binary-transfer"]
    assert descriptor["staged_source_ref"] == "stg-document-real"
