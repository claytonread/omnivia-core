"""The provider-neutral wire adapter seam (A2.6, ADR-038/ADR-039).

One operation call crosses exactly one boundary: a request envelope mapping goes
out, a response envelope mapping comes back. :class:`ApplicationWireAdapter` is
that boundary and nothing else. It is deliberately stated in *wire* mappings
rather than decoded dataclasses, because the whole point of the seam is that an
in-process caller, a local IPC client, and an HTTP client are the same contract
seen through different pipes -- and only the encoded mapping is common to all
three. A protocol phrased in decoded envelopes would quietly assume the codec
runs on the caller's side of the wire, which for a real transport it does not.

:class:`InProcessFakeAdapter` is a deterministic transcript of recorded
exchanges, not a miniature server. It performs no dispatch, holds no state
between calls, stores nothing, authorizes nothing, and computes no result: it
matches a request to a recorded exchange and hands back exactly the response that
was recorded with it. Everything a real implementation would have to *decide* --
whether a replay is honest, whether a token still binds, whether a mutation may
proceed -- is a fact stated in the corpus, so that
:mod:`~omnivia_core.contracts.v1.conformance` is checking the contract rather
than checking a second implementation of the thing under test.

That is what makes the corpus reusable. The same recorded exchanges will be
driven through a real HTTP adapter later, and any adapter that cannot reproduce
them is not speaking this contract.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any, Final, Protocol, runtime_checkable

from omnivia_core.contracts.v1.compatibility import ContractSemanticError

__all__ = [
    "ApplicationWireAdapter",
    "InProcessFakeAdapter",
]


@runtime_checkable
class ApplicationWireAdapter(Protocol):
    """One provider-neutral application call.

    An implementation takes an encoded request envelope and returns an encoded
    response envelope. It may return either response branch -- a success or a
    typed error -- because a contract-level failure is a *result* of the call,
    not an exception: `not_found` and `workspace_migration_required` are things
    this contract says an operation may answer with, and turning them into
    exceptions would put them outside the envelope the contract defines them in.

    Raising is reserved for a genuine failure of the seam itself: the transport
    broke, or the peer returned something that is not a response envelope at all.
    """

    def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Perform one operation call and return the response envelope mapping."""
        ...


class InProcessFakeAdapter:
    """Replay recorded exchanges, keyed by the request id that recorded them.

    The key is `metadata.request_id` because it is the one field a caller
    controls, is unique per exchange, and is visible in the request alone -- the
    only thing this adapter is given. Keying on anything derived (the input, a
    fingerprint, an operation name) would mean *computing* which response is
    correct, which is precisely the judgement a fixture transcript must not make.

    An unrecorded request raises rather than improvising a plausible response.
    A fake that invented an answer for a request the corpus never recorded would
    make the corpus look more complete than it is, and the resulting conformance
    pass would be evidence of nothing.
    """

    def __init__(self, exchanges: Iterable[tuple[str, Mapping[str, Any]]]) -> None:
        recorded: dict[str, Mapping[str, Any]] = {}
        for request_id, response in exchanges:
            if not isinstance(request_id, str) or not request_id:
                raise ContractSemanticError(
                    "InProcessFakeAdapter: every exchange needs a non-empty request id, got "
                    f"{request_id!r}"
                )
            if not isinstance(response, Mapping) or not all(
                isinstance(key, str) for key in response
            ):
                raise ContractSemanticError(
                    f"InProcessFakeAdapter: the response recorded for {request_id!r} is not a "
                    f"response envelope mapping, got {type(response).__name__}"
                )
            if request_id in recorded:
                raise ContractSemanticError(
                    f"InProcessFakeAdapter: duplicate recorded request id {request_id!r}"
                )
            # Copied on the way in as well as on the way out. Storing the caller's
            # own mapping would leave the transcript editable from outside after
            # construction: a later mutation of that object would silently rewrite
            # what this adapter claims was recorded, and every conformance result
            # afterwards would be about a transcript nobody wrote.
            recorded[request_id] = copy.deepcopy(dict(response))
        self._recorded: Final[Mapping[str, Mapping[str, Any]]] = recorded

    def __len__(self) -> int:
        return len(self._recorded)

    def call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return a fresh copy of the response recorded for `request`'s request id.

        A copy, not the stored mapping: a caller that mutated a returned response
        would otherwise rewrite the transcript, and every later call in the same
        run would replay the corruption as though it had been recorded that way.
        A real transport hands back a freshly decoded document every time, and an
        adapter that is meant to stand in for one must not be quietly cheaper to
        misuse than the thing it stands in for.
        """
        if not isinstance(request, Mapping):
            raise ContractSemanticError(
                f"InProcessFakeAdapter: expected a request envelope mapping, "
                f"got {type(request).__name__}"
            )
        metadata = request.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ContractSemanticError(
                "InProcessFakeAdapter: request envelope has no 'metadata' object"
            )
        request_id = metadata.get("request_id")
        if not isinstance(request_id, str):
            raise ContractSemanticError(
                "InProcessFakeAdapter: request metadata has no string 'request_id'"
            )
        recorded = self._recorded.get(request_id)
        if recorded is None:
            raise ContractSemanticError(
                f"InProcessFakeAdapter: no recorded exchange for request id {request_id!r}"
            )
        return copy.deepcopy(dict(recorded))
