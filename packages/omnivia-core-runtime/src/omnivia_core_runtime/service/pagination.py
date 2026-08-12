"""Server-owned, opaque continuation tokens shared by production read handlers."""

from __future__ import annotations

import base64
import binascii
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

from omnivia_core.contracts.v1.canonical_json import canonicalize, parse_json_document


class ContinuationTokenCodec(Protocol):
    """The narrow signing seam used by stateful production reads."""

    def encode(self, payload: Mapping[str, Any]) -> str: ...
    def decode(self, token: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HmacContinuationTokenCodec:
    """Canonical JSON authenticated with a process-owned HMAC key."""

    secret: bytes

    @classmethod
    def secure(cls) -> HmacContinuationTokenCodec:
        return cls(secrets.token_bytes(32))

    def encode(self, payload: Mapping[str, Any]) -> str:
        document = canonicalize(dict(payload)).encode("utf-8")
        signature = hmac.new(self.secret, document, sha256).digest()
        encoded_document = base64.urlsafe_b64encode(document).rstrip(b"=").decode()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{encoded_document}.{encoded_signature}"

    def decode(self, token: str) -> Mapping[str, Any]:
        try:
            encoded_document, encoded_signature = token.split(".")
            document = base64.b64decode(
                encoded_document + "=" * (-len(encoded_document) % 4),
                altchars=b"-_",
                validate=True,
            )
            signature = base64.b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error, UnicodeEncodeError) as error:
            raise ValueError("invalid continuation encoding") from error
        expected = hmac.new(self.secret, document, sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid continuation signature")
        value = parse_json_document(document)
        if (
            not isinstance(value, dict)
            or canonicalize(value).encode("utf-8") != document
        ):
            raise ValueError("invalid continuation document")
        return value


def token_digest(value: Any) -> str:
    """A compact digest for binding a token to a request or frozen snapshot."""

    document = canonicalize(value).encode("utf-8")
    return base64.urlsafe_b64encode(sha256(document).digest()).rstrip(b"=").decode()


# One key for the lifetime of the serving process. Tokens die with that process and each
# handler additionally binds them to principal, workspace, operation and request fields.
PROCESS_CONTINUATION_TOKENS: ContinuationTokenCodec = HmacContinuationTokenCodec.secure()


__all__ = [
    "PROCESS_CONTINUATION_TOKENS",
    "ContinuationTokenCodec",
    "HmacContinuationTokenCodec",
    "token_digest",
]
