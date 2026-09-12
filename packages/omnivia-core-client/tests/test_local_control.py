"""The client half of the internal local-control wrapper.

What this package owes the wire is: build a document the service admits, admit
only a reply the service could have produced, keep the credential out of
everything a credential must stay out of, and use the endpoint, deadline and
cancellation conventions already here rather than a second set.

The service's own admission rules are proved in the runtime's suite. These cases
are about *this* side: the document built, the reply admitted, and the redaction.
They use a scripted peer rather than a live service so a reply this package must
refuse -- one no correct service would ever send -- can actually be sent.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from omnivia_core_client import (
    CLIENT_API_VERSION,
    LOCAL_CONTROL_VERSION,
    CancellationToken,
    Deadline,
    LocalControlRefused,
    LocalIpcTransport,
    OperationCancelledError,
    ProtocolError,
    TransportError,
    call_authenticated,
    canonical_json_bytes,
    encode_frame,
    local_control_transport,
    mcp_authoring_admission,
    mcp_configure,
    mcp_revoke,
    mcp_status,
)

from omnivia_core.contracts.v1 import (
    CapabilityRef,
    CapabilityRequirement,
    CapabilitySet,
    ClientIdentity,
    CompatibilityMetadata,
    GrantedAuthority,
    RequestEnvelope,
    RequestMetadata,
    ResponseMetadata,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
    codec,
    get_operation_metadata,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="these socket cases require AF_UNIX; the pipe transport has its own suite",
)

WORKSPACE_ID = "ws-local-control-01"
OPERATION = "workspace.inspect"
SECRET = "sJ8-bearer-value-that-must-not-appear-anywhere-else"
CALL_TIMEOUT = 10.0

SETUP: Mapping[str, object] = {
    "setup_id": "mcp-setup-1",
    "host": "claude-code",
    "workspace_id": WORKSPACE_ID,
    "principal_id": "mcp-claude-code-abc",
    "profile": "authoring",
    "authoring_intent": True,
    "credential_reference": "omcp-abc",
    "status": "active",
    "setup_generation": 2,
}


def request_envelope(request_id: str = "req-control-1") -> RequestEnvelope:
    entry = get_operation_metadata(OPERATION)
    required = entry.required_capability
    return RequestEnvelope(
        operation=OPERATION,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=request_id,
            trace_id=request_id,
            api_version=CLIENT_API_VERSION,
            client=ClientIdentity(id="omnivia-core-client-tests", version="0.1.0"),
            workspace_id=WORKSPACE_ID,
            scopes=tuple(entry.scope.required_scopes),
            purpose="workspace_inspection",
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=required.required,
                ),
            ),
        ),
        input={},
    )


def response_document(request: RequestEnvelope) -> dict[str, object]:
    """One response envelope the public codec will decode, built from the contract.

    Long because the contract is: a response carries a version-capability envelope
    and a granted authority, and a hand-trimmed one would be a document the codec
    refuses -- which is a different test from the one intended here.
    """
    refs: tuple[CapabilityRef, ...] = ()
    return codec.encode_response(
        SuccessResponseEnvelope(
            metadata=ResponseMetadata(
                request_id=request.metadata.request_id,
                correlation_id=request.metadata.correlation_id,
                version=VersionCapabilityEnvelope(
                    api_version=CLIENT_API_VERSION,
                    server_version="0.1.0",
                    workspace_format_version="1.0",
                    compatibility=CompatibilityMetadata(
                        selected_api_version=CLIENT_API_VERSION,
                        selected_workspace_version="1.0",
                        supported_api_versions=VersionWindow(
                            minimum=CLIENT_API_VERSION, maximum=CLIENT_API_VERSION
                        ),
                        supported_workspace_versions=VersionWindow(
                            minimum="1.0", maximum="1.0"
                        ),
                        status="compatible",
                        upgrade_state=UpgradeState(value="none"),
                        deprecations=(),
                    ),
                    capabilities=CapabilitySet(
                        supported=refs, granted=refs, effective=refs
                    ),
                ),
                authority=GrantedAuthority(
                    principal_id="mcp-claude-code-abc", roles=(), capabilities=refs
                ),
            ),
            result={"ok": True},
        )
    )


def reply(kind: str, result: Mapping[str, object]) -> bytes:
    return encode_frame(
        {
            "local_control_result": LOCAL_CONTROL_VERSION,
            "kind": kind,
            "result": dict(result),
        }
    )


def refusal(kind: str, code: str, message: str = "refused") -> bytes:
    return encode_frame(
        {
            "local_control_result": LOCAL_CONTROL_VERSION,
            "kind": kind,
            "error": {"code": code, "message": message},
        }
    )


class ScriptedPeer:
    """A real listening socket that answers one connection with chosen bytes.

    Records what it was sent, so what this package *put on the wire* can be
    asserted rather than inferred from what it returned.
    """

    def __init__(self, response: bytes) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="ovc-", dir=tempfile.gettempdir()))
        self.path = self.directory / "s.sock"
        self.received = b""
        self._response = response
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(1)
        self._server.settimeout(30)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def endpoint_uri(self) -> str:
        return f"unix://{self.path}"

    def sent(self) -> dict[str, object]:
        """The one document this package wrote, decoded from the real frame."""
        assert self.received[:4] == b"OVC1"
        length = int.from_bytes(self.received[4:8], "big")
        body = self.received[8 : 8 + length]
        document = json.loads(body.decode("utf-8"))
        assert isinstance(document, dict)
        # The service admits canonical bytes only, so a document that reached it
        # non-canonically would be refused by the frame rather than answered.
        assert canonical_json_bytes(document) == body
        return document

    def _serve(self) -> None:
        try:
            connection, _ = self._server.accept()
        except OSError:  # pragma: no cover - only on early teardown
            return
        with connection:
            try:
                connection.settimeout(0.5)
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    self.received += chunk
            except OSError:
                pass
            try:
                connection.sendall(self._response)
            except OSError:  # pragma: no cover - only on early teardown
                pass

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)


def transport(peer: ScriptedPeer) -> LocalIpcTransport:
    return LocalIpcTransport(endpoint_uri=peer.endpoint_uri)


def deadline() -> Deadline:
    return Deadline.after(CALL_TIMEOUT)


# --- what goes on the wire ----------------------------------------------------


def test_an_authenticated_call_wraps_the_envelope_and_presents_the_bearer() -> None:
    request = request_envelope()
    scripted = ScriptedPeer(
        reply("application.call", {"response": response_document(request)})
    )
    try:
        response = call_authenticated(
            transport(scripted), SECRET, request, deadline=deadline()
        )
        assert isinstance(response, SuccessResponseEnvelope)
        assert response.result == {"ok": True}
        sent = scripted.sent()
        assert sent["local_control"] == LOCAL_CONTROL_VERSION
        assert sent["kind"] == "application.call"
        assert sent["credential"] == SECRET
        assert sent["request"] == codec.encode_request(request)
    finally:
        scripted.close()


def test_an_administration_control_carries_no_credential_at_all() -> None:
    """Reaching the endpoint is the proof; there is no field to put a bearer in."""
    scripted = ScriptedPeer(reply("mcp.configure", {"setup": SETUP, "rotated": False}))
    try:
        mcp_configure(
            transport(scripted),
            host="claude-code",
            workspace_id=WORKSPACE_ID,
            profile="authoring",
            authoring_intent=True,
            deadline=deadline(),
        )
        sent = scripted.sent()
        assert set(sent) == {"local_control", "kind", "arguments"}
        assert sent["arguments"] == {
            "host": "claude-code",
            "workspace_id": WORKSPACE_ID,
            "profile": "authoring",
            "authoring_intent": True,
        }
    finally:
        scripted.close()


def test_status_omits_the_host_member_entirely_when_none_was_asked_for() -> None:
    scripted = ScriptedPeer(reply("mcp.status", {"setups": []}))
    try:
        assert mcp_status(transport(scripted), deadline=deadline()).setups == ()
        assert scripted.sent()["arguments"] == {}
    finally:
        scripted.close()


# --- what comes back ----------------------------------------------------------


def test_a_rotating_configure_hands_over_its_bearer_once_and_only_on_request() -> None:
    scripted = ScriptedPeer(
        reply("mcp.configure", {"setup": SETUP, "rotated": True, "secret": SECRET})
    )
    try:
        result = mcp_configure(
            transport(scripted),
            host="claude-code",
            workspace_id=WORKSPACE_ID,
            profile="authoring",
            authoring_intent=True,
            deadline=deadline(),
        )
        assert result.rotated is True
        assert result.reveal() == SECRET
        assert result.setup.principal_id == "mcp-claude-code-abc"
        assert result.setup.authoring_intent is True
    finally:
        scripted.close()


def test_a_configure_result_never_renders_the_bearer_it_holds() -> None:
    scripted = ScriptedPeer(
        reply("mcp.configure", {"setup": SETUP, "rotated": True, "secret": SECRET})
    )
    try:
        result = mcp_configure(
            transport(scripted),
            host="claude-code",
            workspace_id=WORKSPACE_ID,
            profile="authoring",
            authoring_intent=True,
            deadline=deadline(),
        )
        for rendered in (repr(result), str(result), f"{result}", repr([result])):
            assert SECRET not in rendered
        assert "redacted" in repr(result)
    finally:
        scripted.close()


def test_a_configure_that_rotated_nothing_has_no_bearer_to_give() -> None:
    scripted = ScriptedPeer(reply("mcp.configure", {"setup": SETUP, "rotated": False}))
    try:
        result = mcp_configure(
            transport(scripted),
            host="claude-code",
            workspace_id=WORKSPACE_ID,
            profile="restricted",
            authoring_intent=False,
            deadline=deadline(),
        )
        assert result.rotated is False
        assert result.reveal() is None
    finally:
        scripted.close()


def test_a_secret_on_a_result_that_rotated_nothing_is_refused() -> None:
    """A reply no correct service sends, refused rather than quietly accepted."""
    scripted = ScriptedPeer(
        reply("mcp.configure", {"setup": SETUP, "rotated": False, "secret": SECRET})
    )
    try:
        with pytest.raises(ProtocolError):
            mcp_configure(
                transport(scripted),
                host="claude-code",
                workspace_id=WORKSPACE_ID,
                profile="restricted",
                authoring_intent=False,
                deadline=deadline(),
            )
    finally:
        scripted.close()


def test_status_and_revoke_have_no_type_a_secret_could_travel_in() -> None:
    scripted = ScriptedPeer(reply("mcp.status", {"setups": [SETUP]}))
    try:
        status = mcp_status(transport(scripted), deadline=deadline())
        assert len(status.setups) == 1
        assert not hasattr(status.setups[0], "secret")
        assert SECRET not in repr(status)
    finally:
        scripted.close()
    scripted = ScriptedPeer(reply("mcp.revoke", {"setup": None}))
    try:
        assert mcp_revoke(transport(scripted), host="codex", deadline=deadline()).setup is None
    finally:
        scripted.close()


def test_an_admission_answer_names_the_principal_it_is_about() -> None:
    scripted = ScriptedPeer(
        reply(
            "mcp.authoring_admission",
            {
                "admitted": True,
                "principal_id": "mcp-claude-code-abc",
                "workspace_id": WORKSPACE_ID,
            },
        )
    )
    try:
        admission = mcp_authoring_admission(
            transport(scripted), SECRET, deadline=deadline()
        )
        assert admission.admitted is True
        assert admission.principal_id == "mcp-claude-code-abc"
        assert admission.workspace_id == WORKSPACE_ID
        assert scripted.sent()["credential"] == SECRET
    finally:
        scripted.close()


# --- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["unauthenticated", "unauthorized", "refused", "unavailable", "malformed"]
)
def test_the_service_s_refusal_arrives_with_its_own_code(code: str) -> None:
    scripted = ScriptedPeer(refusal("mcp.status", code))
    try:
        with pytest.raises(LocalControlRefused) as refused:
            mcp_status(transport(scripted), deadline=deadline())
        assert refused.value.code == code
    finally:
        scripted.close()


def test_a_refusal_code_this_build_does_not_know_is_still_a_refusal() -> None:
    """Inventing a success out of an unrecognised refusal is the worst reading.

    It arrives as `unknown` rather than under the peer's own spelling: a code is
    a string the peer chose, and any string the peer chose is somewhere a bearer
    could have been put.
    """
    scripted = ScriptedPeer(refusal("mcp.status", "something-later"))
    try:
        with pytest.raises(LocalControlRefused) as refused:
            mcp_status(transport(scripted), deadline=deadline())
        assert refused.value.code == "unknown"
        assert "something-later" not in str(refused.value)
    finally:
        scripted.close()


def test_a_refusal_message_is_this_package_s_own_and_never_the_peer_s() -> None:
    """Every known code renders the sentence in this module's table, not the wire's."""
    scripted = ScriptedPeer(refusal("mcp.status", "unauthorized", "peer wrote this"))
    try:
        with pytest.raises(LocalControlRefused) as refused:
            mcp_status(transport(scripted), deadline=deadline())
        assert refused.value.code == "unauthorized"
        assert "peer wrote this" not in str(refused.value)
        assert "local installation administrator" in str(refused.value)
    finally:
        scripted.close()


def test_a_peer_that_echoes_the_bearer_as_its_refusal_never_leaks_it() -> None:
    """The disclosure this admission exists for, on the value it would disclose.

    The peer has just been handed the bearer, so the one string it is certain to
    be able to echo is the credential itself. Checked on every place a refusal
    carries text -- including `__context__` and `__cause__`, which survive a
    `from None` and are one attribute access away from anything that logs the
    exception a caller caught.
    """
    scripted = ScriptedPeer(refusal("mcp.authoring_admission", "unauthenticated", SECRET))
    try:
        with pytest.raises(LocalControlRefused) as refused:
            mcp_authoring_admission(transport(scripted), SECRET, deadline=deadline())
        error = refused.value
        assert error.code == "unauthenticated"
        for rendered in (str(error), repr(error), repr(error.args), repr(vars(error))):
            assert SECRET not in rendered
        assert error.__context__ is None
        assert error.__cause__ is None
        # And the bearer it was *given* is not kept either: the credential is an
        # argument to one call, never a field on the failure it produced.
        assert SECRET not in repr(error.__dict__)
    finally:
        scripted.close()


def test_an_unbounded_refusal_message_is_replaced_rather_than_carried() -> None:
    """A message past any plausible bound is not a service reply at all."""
    scripted = ScriptedPeer(refusal("mcp.status", "x" * 200, "y" * 4000))
    try:
        with pytest.raises(ProtocolError) as refused:
            mcp_status(transport(scripted), deadline=deadline())
        assert "y" * 100 not in str(refused.value)
    finally:
        scripted.close()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            encode_frame({"local_control_result": "v2", "kind": "mcp.status", "result": {}}),
            id="a wrapper version this build does not speak",
        ),
        pytest.param(
            reply("mcp.revoke", {"setups": []}), id="a reply for another control"
        ),
        pytest.param(
            encode_frame({"local_control_result": LOCAL_CONTROL_VERSION, "kind": "mcp.status"}),
            id="a reply that is neither a result nor an error",
        ),
        pytest.param(reply("mcp.status", {"setups": "none"}), id="a non-list setups"),
        pytest.param(
            reply("mcp.status", {"setups": [{**SETUP, "setup_generation": "2"}]}),
            id="a setup member of the wrong type",
        ),
        pytest.param(
            reply("mcp.status", {"setups": [{"host": "codex"}]}),
            id="a setup missing its members",
        ),
        pytest.param(
            encode_frame(codec.encode_request(request_envelope())),
            id="an ordinary request envelope echoed back",
        ),
        pytest.param(
            encode_frame(
                {
                    "local_control_result": LOCAL_CONTROL_VERSION,
                    "kind": "mcp.status",
                    "result": {"setups": []},
                    "authority": "granted",
                }
            ),
            id="a member this build does not read at the top level",
        ),
        pytest.param(
            reply("mcp.status", {"setups": [], "roles": ["installation-admin"]}),
            id="a member this build does not read inside the result",
        ),
        pytest.param(
            reply("mcp.status", {"setups": [{**SETUP, "roles": ["admin"]}]}),
            id="a member this build does not read inside a setup",
        ),
        pytest.param(
            encode_frame(
                {
                    "local_control_result": LOCAL_CONTROL_VERSION,
                    "kind": "mcp.status",
                    "error": {"code": "refused"},
                }
            ),
            id="a refusal that is not shaped like one",
        ),
        pytest.param(
            reply("mcp.status", {"setups": [{**SETUP, "principal_id": "p" * 400}]}),
            id="an identifier past any bound an identifier has",
        ),
    ],
)
def test_a_reply_this_package_cannot_admit_is_refused(response: bytes) -> None:
    scripted = ScriptedPeer(response)
    try:
        with pytest.raises(ProtocolError):
            mcp_status(transport(scripted), deadline=deadline())
    finally:
        scripted.close()


def test_a_bad_admission_reply_is_refused_rather_than_read_as_admitted() -> None:
    """Fail closed, and fail loudly: never silently `False`, never silently `True`."""
    scripted = ScriptedPeer(reply("mcp.authoring_admission", {"admitted": "yes"}))
    try:
        with pytest.raises(ProtocolError):
            mcp_authoring_admission(transport(scripted), SECRET, deadline=deadline())
    finally:
        scripted.close()


def test_an_application_error_response_is_an_answer_and_not_a_refusal() -> None:
    """The same rule `ServiceClient.call` keeps: a peer that errored has answered."""
    request = request_envelope()
    document = codec.encode_response(
        codec.decode_response(response_document(request))
    )
    scripted = ScriptedPeer(reply("application.call", {"response": document}))
    try:
        response = call_authenticated(
            transport(scripted), SECRET, request, deadline=deadline()
        )
        assert response.metadata.request_id == request.metadata.request_id
    finally:
        scripted.close()


# --- the conventions already here ---------------------------------------------


def test_a_cancelled_control_never_reaches_the_endpoint() -> None:
    """Cancellation is checked before the deadline and before the connect.

    No listening peer, deliberately: the endpoint below does not exist, so a
    connect attempt would be a `TransportError`. Getting the cancellation instead
    is the proof that nothing was dialled.
    """
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelledError):
        mcp_status(
            LocalIpcTransport(endpoint_uri="unix:///nonexistent/control.sock"),
            deadline=deadline(),
            cancellation=token,
        )


def test_a_control_writes_exactly_one_frame_and_nothing_after_it() -> None:
    """The server refuses a connection carrying two, so this must never send two."""
    scripted = ScriptedPeer(reply("mcp.status", {"setups": []}))
    try:
        mcp_status(transport(scripted), deadline=deadline())
        length = int.from_bytes(scripted.received[4:8], "big")
        assert len(scripted.received) == 8 + length
    finally:
        scripted.close()


def test_controls_travel_the_local_endpoint_only() -> None:
    """An HTTP client is refused for the honest reason: this family has no HTTP form."""

    class NotLocal:
        transport = object()

    with pytest.raises(TransportError):
        local_control_transport(NotLocal())  # type: ignore[arg-type]


def test_a_local_client_hands_back_its_own_transport() -> None:
    class Local:
        def __init__(self, held: LocalIpcTransport) -> None:
            self.transport = held

    held = LocalIpcTransport(endpoint_uri="unix:///nonexistent/control.sock")
    assert local_control_transport(Local(held)) is held  # type: ignore[arg-type]
