"""Phase 6B: the installed MCP authority, reached over the live local endpoint.

The authority's own suite proves what it decides. This one proves the wiring:
that a bearer presented on the OVC1 socket is resolved against durable state on
*every* call, that administration is the service's administrator and not the
caller's claim, that a follower reaches the owner instead of the database, and
that none of it changed what the existing unauthenticated CLI and operator path
already does on the same socket.

POSIX-only by the socket, not by the rules: the wire admission is proved
platform-neutrally in ``test_local_control_codec.py`` and the Windows pipe
abstraction is untouched by anything here -- the server takes a `LocalEndpoint`
and this suite hands it a Unix one.
"""

from __future__ import annotations

import hashlib
import socket
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.dispatch import Dispatcher, Grant
from omnivia_core_runtime.service.installation_host import (
    AUTHORITY_DESCRIPTOR_NAME,
    AUTHORITY_FAILOVER_TIMEOUT_SECONDS,
    AUTHORITY_RUNTIME_DIRECTORY,
    InstallationAuthorityCoordinator,
)
from omnivia_core_runtime.service.installed_mcp import (
    InstalledMcpAuthority,
    InstalledMcpSecret,
)
from omnivia_core_runtime.service.local_control import (
    LOCAL_CONTROL_FIELD,
    LOCAL_CONTROL_RESULT_FIELD,
    LOCAL_CONTROL_VERSION,
    LocalControlError,
    LocalControlKind,
    LocalControlRefusal,
)
from omnivia_core_runtime.service.mcp_control import (
    AuthenticatedApplicationDispatch,
    ControlExchange,
    OwnedInstalledMcp,
    ProxiedInstalledMcp,
    setup_view,
)
from omnivia_core_runtime.service.mutation import (
    INSTALLATION_ADMINISTRATOR_ROLE,
    KNOWLEDGE_REVIEWER_ROLE,
    WORKSPACE_CONTRIBUTOR_ROLE,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS, success
from omnivia_core_runtime.service.ovc1 import HEADER_BYTES, decode_frame, encode_frame
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import (
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
    LocalSocketTransport,
)
from omnivia_core_runtime.storage.backup import RUNTIME_DIR
from omnivia_core_runtime.storage.installation_store import (
    InstallationStore,
    McpHost,
    McpProfile,
    NewInstallationAllocation,
    open_installation_store,
)

from omnivia_core.contracts.v1 import (
    RequestEnvelope,
    ServiceProbeResult,
    SuccessResponseEnvelope,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)

INSTALLATION_ID = "inst-local-control"
OBSERVED_AT = "2026-09-12T00:00:00Z"


# --- the installation, built the ordinary way ---------------------------------


class TickClock:
    def __init__(self, start: int = 1_800_000_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1
        return self.value


def administrator(
    *, roles: frozenset[str] = frozenset({INSTALLATION_ADMINISTRATOR_ROLE})
) -> AuthenticatedSession:
    return AuthenticatedSession(
        principal_id="local-owner", roles=roles, installations=frozenset({INSTALLATION_ID})
    )


def register_workspace(store: InstallationStore, suffix: str) -> str:
    """One workspace in the authorised inventory, through allocation and settlement."""
    workspace_id = f"ws-{suffix}"
    minted = NewInstallationAllocation(
        audit_ref=f"audit-{suffix}",
        claim_id=f"claim-{suffix}",
        allocation_id=f"allocation-{suffix}",
        target_workspace_id=workspace_id,
        target_path=(store.installation_root / "workspaces" / workspace_id).resolve(),
    )
    store.claim_allocation(
        store.authority,
        principal_id="local-owner",
        operation="workspace.create",
        purpose="workspace_provisioning",
        idempotency_key=f"key-{suffix}",
        request_digest="sha256:" + hashlib.sha256(suffix.encode()).hexdigest(),
        identity_factory=lambda: minted,
    )
    store.settle_allocation_success(
        store.authority,
        allocation_id=minted.allocation_id,
        workspace_label=None,
        outcome_id=f"outcome-{suffix}",
        outcome_json="{}",
        outcome_digest="sha256:" + hashlib.sha256(b"outcome").hexdigest(),
        execution_id=f"execution-{suffix}",
        grant_id=f"grant-{suffix}",
        required_role=INSTALLATION_ADMINISTRATOR_ROLE,
        settlement_guard=lambda: None,
    )
    return workspace_id


# --- a dispatcher that records the session it was actually given --------------


@dataclass
class RecordingDispatch:
    """Stands in for the application surface, and records who each call ran as.

    The only fact this suite needs from the application path is *which session
    reached it*, which is exactly what an end-to-end dispatcher would bury. A real
    `ApplicationDispatcher` is exercised by its own suite; this one answers the
    question this one asks.
    """

    sessions: list[AuthenticatedSession]

    def dispatch_for_session(
        self, request: RequestEnvelope, session: AuthenticatedSession
    ) -> SuccessResponseEnvelope:
        self.sessions.append(session)
        response = success(request, {"principal": session.principal_id})
        assert isinstance(response, SuccessResponseEnvelope)
        return response

    def __call__(self, request: RequestEnvelope) -> SuccessResponseEnvelope:
        """The unauthenticated path: this service's own principal, as before."""
        response = success(request, {"principal": "local-owner"})
        assert isinstance(response, SuccessResponseEnvelope)
        return response


@dataclass
class CountingAuthority:
    """Wraps the real authority and counts resolutions, to prove nothing is cached."""

    inner: InstalledMcpAuthority
    resolutions: int = 0

    def authenticate(self, credential: str) -> object:
        self.resolutions += 1
        return self.inner.authenticate(credential)

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


@dataclass
class Harness:
    store: InstallationStore
    authority: InstalledMcpAuthority
    counting: CountingAuthority
    dispatch: RecordingDispatch
    endpoint: LocalEndpoint
    workspace_id: str

    def secret(self, *, profile: McpProfile = McpProfile.RESTRICTED) -> str:
        provisioning = self.authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id=self.workspace_id,
            profile=profile,
            authoring_intent=profile is McpProfile.AUTHORING,
        )
        assert isinstance(provisioning.secret, InstalledMcpSecret)
        return provisioning.secret.reveal()


def probe_router() -> ProbeRouter:
    return ProbeRouter(
        facts=lambda: ServiceFacts(
            observed_at=OBSERVED_AT,
            health_status="pass",
            readiness_status="pass",
            discovery_status="pass",
        ),
        capabilities=tuple,
        clock=lambda: 0,
    )


@contextmanager
def served(tmp_path: Path, *, workspaces: Sequence[str] = ("one",)) -> Iterator[Harness]:
    """One owning installation, serving the local endpoint the way `main` wires it."""
    store = open_installation_store(
        (tmp_path / "installation").resolve(),
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: INSTALLATION_ID,
    )
    directory = tempfile.TemporaryDirectory(prefix="lc-")
    try:
        for suffix in workspaces:
            register_workspace(store, suffix)
        authority = InstalledMcpAuthority(store)
        counting = CountingAuthority(inner=authority)
        dispatch = RecordingDispatch(sessions=[])
        seam = OwnedInstalledMcp(
            authority=counting,  # type: ignore[arg-type]
            administrator=administrator(),
        )
        endpoint = LocalEndpoint(EndpointScheme.UNIX, str(Path(directory.name) / "s.sock"))
        server = LocalSocketServer(
            router=DocumentRouter(probes=probe_router(), dispatch=dispatch),
            authenticated=AuthenticatedApplicationDispatch(
                seam=seam, dispatcher=dispatch
            ),
            mcp_administration=seam,
            endpoint=endpoint,
            timeout=2.0,
        )
        with server:
            yield Harness(
                store=store,
                authority=authority,
                counting=counting,
                dispatch=dispatch,
                endpoint=endpoint,
                workspace_id=f"ws-{workspaces[0]}",
            )
    finally:
        directory.cleanup()
        store.close()


# --- talking to it ------------------------------------------------------------


def exchange(endpoint: LocalEndpoint, document: Mapping[str, object]) -> dict[str, object]:
    return LocalSocketTransport(endpoint=endpoint, timeout=2.0).exchange(document)


def request_document(workspace_id: str, *, request_id: str = "req-1") -> dict[str, object]:
    return {
        "input": {},
        "metadata": {
            "api_version": "1.2",
            "client": {"id": "test-client", "version": "1.0.0"},
            "correlation_id": "corr-1",
            "purpose": "knowledge_retrieval",
            "request_id": request_id,
            "required_capabilities": [],
            "scopes": [],
            "trace_id": "trace-1",
            "workspace_id": workspace_id,
        },
        "operation": "memory.get",
    }


def call(endpoint: LocalEndpoint, credential: str, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
        "kind": LocalControlKind.APPLICATION_CALL.value,
        "credential": credential,
        "request": request_document("ws-one"),
    }
    document.update(overrides)
    return exchange(endpoint, document)


def administer(
    endpoint: LocalEndpoint, kind: LocalControlKind, arguments: Mapping[str, object]
) -> dict[str, object]:
    return exchange(
        endpoint,
        {
            LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
            "kind": kind.value,
            "arguments": dict(arguments),
        },
    )


def refusal(answer: Mapping[str, object]) -> str:
    error = answer.get("error")
    assert isinstance(error, dict), f"expected a refusal, got {answer!r}"
    code = error["code"]
    assert isinstance(code, str)
    return code


def result(answer: Mapping[str, object]) -> Mapping[str, object]:
    assert "error" not in answer, f"expected a result, got {answer!r}"
    value = answer["result"]
    assert isinstance(value, dict)
    return value


# --- the existing path, unchanged ---------------------------------------------


def test_an_unauthenticated_request_still_runs_as_the_service_itself(
    tmp_path: Path,
) -> None:
    """The compatibility rule, stated as the case that would have broken.

    A plain `RequestEnvelope` carries no control member, so it never reaches the
    wrapper at all -- it takes the router path it took before this existed, runs
    as the service's own principal, and resolves no credential on the way.
    """
    with served(tmp_path) as harness:
        answer = exchange(harness.endpoint, request_document("ws-one"))
        assert answer["result"] == {"principal": "local-owner"}
        assert "error" not in answer
        assert harness.counting.resolutions == 0
        assert harness.dispatch.sessions == []


def test_an_unauthenticated_probe_still_answers_before_any_authority(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        answer = exchange(
            harness.endpoint, {"probe": PROBE_HEALTH, "request_id": "req-probe"}
        )
        assert ServiceProbeResult.from_wire(answer).status == "pass"
        assert harness.counting.resolutions == 0


# --- authenticated dispatch ---------------------------------------------------


def test_a_presented_bearer_dispatches_under_the_authority_it_resolves_to(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        secret = harness.secret()
        answer = result(call(harness.endpoint, secret))
        response = answer["response"]
        assert isinstance(response, dict)
        assert "error" not in response
        ran_as = response["result"]
        assert isinstance(ran_as, dict)
        session = harness.dispatch.sessions[-1]
        assert ran_as == {"principal": session.principal_id}
        assert session.principal_id.startswith("mcp-claude-code-")
        assert session.workspaces == frozenset({"ws-one"})
        assert "evidence.search" in session.operations
        # A restricted principal holds no role at all, and no principal here holds
        # installation authority, so nothing it presents reaches administration.
        assert session.roles == frozenset()
        assert session.installations == frozenset()


def test_a_restricted_principal_is_authenticated_and_still_holds_no_mutation(
    tmp_path: Path,
) -> None:
    """Authentication is not admission: the profile is what bounds the surface."""
    with served(tmp_path) as harness:
        result(call(harness.endpoint, harness.secret()))
        session = harness.dispatch.sessions[-1]
        assert "evidence.capture" not in session.operations
        assert "memory.create" not in session.operations


def test_every_call_resolves_the_bearer_again(tmp_path: Path) -> None:
    """The whole no-cached-sessions claim, counted.

    Three identical calls are three resolutions. A transport that kept a session
    between connections -- or one that kept it for the life of a bearer -- would
    show fewer, and would still be serving a revoked principal on the next call.
    """
    with served(tmp_path) as harness:
        secret = harness.secret()
        for index in range(3):
            answer = call(
                harness.endpoint,
                secret,
                request=request_document("ws-one", request_id=f"req-{index}"),
            )
            assert "error" not in answer
        assert harness.counting.resolutions == 3


@pytest.mark.parametrize(
    "credential",
    [
        pytest.param("not-a-real-bearer", id="a bearer that was never minted"),
        pytest.param("omcp-0000", id="a reference used as a bearer"),
    ],
)
def test_a_bearer_that_does_not_resolve_is_refused(
    tmp_path: Path, credential: str
) -> None:
    with served(tmp_path) as harness:
        harness.secret()
        answer = call(harness.endpoint, credential)
        assert refusal(answer) == LocalControlError.UNAUTHENTICATED.value
        assert harness.dispatch.sessions == []


def test_a_missing_bearer_never_reaches_the_application_path(tmp_path: Path) -> None:
    with served(tmp_path) as harness:
        harness.secret()
        answer = exchange(
            harness.endpoint,
            {
                LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                "kind": LocalControlKind.APPLICATION_CALL.value,
                "request": request_document("ws-one"),
            },
        )
        assert refusal(answer) == LocalControlError.MALFORMED.value
        assert harness.dispatch.sessions == []


def test_a_rotated_bearer_stops_working_on_the_next_call(tmp_path: Path) -> None:
    """Rotation is immediate, and the new bearer is the only one that works."""
    with served(tmp_path) as harness:
        first = harness.secret()
        assert "error" not in call(harness.endpoint, first)
        second = harness.secret(profile=McpProfile.AUTHORING)
        assert second != first
        assert refusal(call(harness.endpoint, first)) == "unauthenticated"
        assert "error" not in call(harness.endpoint, second)


def test_revocation_blocks_the_next_call_and_the_same_key_replay(
    tmp_path: Path,
) -> None:
    """The replay case stated as a replay: byte-for-byte the same control.

    A revoked bearer must fail the *identical* document that just succeeded, with
    no interval and nothing else changed, because a same-key replay is exactly
    that document arriving a second time.
    """
    with served(tmp_path) as harness:
        secret = harness.secret()
        document: dict[str, object] = {
            LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
            "kind": LocalControlKind.APPLICATION_CALL.value,
            "credential": secret,
            "request": request_document("ws-one", request_id="req-replay"),
        }
        assert "error" not in exchange(harness.endpoint, document)
        administer(harness.endpoint, LocalControlKind.MCP_REVOKE, {"host": "claude-code"})
        assert refusal(exchange(harness.endpoint, document)) == "unauthenticated"
        assert len(harness.dispatch.sessions) == 1


# --- the administration family ------------------------------------------------


def test_configure_returns_its_new_bearer_exactly_once(tmp_path: Path) -> None:
    with served(tmp_path) as harness:
        first = result(
            administer(
                harness.endpoint,
                LocalControlKind.MCP_CONFIGURE,
                {
                    "host": "codex",
                    "workspace_id": "ws-one",
                    "profile": "restricted",
                    "authoring_intent": False,
                },
            )
        )
        assert first["rotated"] is True
        secret = first["secret"]
        assert isinstance(secret, str) and secret
        # The same request again changes nothing, so it mints nothing.
        again = result(
            administer(
                harness.endpoint,
                LocalControlKind.MCP_CONFIGURE,
                {
                    "host": "codex",
                    "workspace_id": "ws-one",
                    "profile": "restricted",
                    "authoring_intent": False,
                },
            )
        )
        assert again["rotated"] is False
        assert "secret" not in again
        assert "error" not in call(harness.endpoint, secret)


def test_status_and_revoke_have_no_branch_that_returns_a_secret(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        secret = harness.secret()
        status = result(administer(harness.endpoint, LocalControlKind.MCP_STATUS, {}))
        revoked = result(
            administer(
                harness.endpoint, LocalControlKind.MCP_REVOKE, {"host": "claude-code"}
            )
        )
        for answer in (status, revoked):
            assert secret not in repr(answer)
            assert "secret" not in repr(answer)
            assert "digest" not in repr(answer)
            assert "salt" not in repr(answer)


def test_a_redacted_setup_carries_no_field_a_secret_could_be_in(
    tmp_path: Path,
) -> None:
    """Redaction as the shape of the type: the members are enumerated, not filtered."""
    with served(tmp_path) as harness:
        harness.secret()
        setups = result(
            administer(harness.endpoint, LocalControlKind.MCP_STATUS, {})
        )["setups"]
        assert isinstance(setups, list)
        assert set(setups[0]) == {
            "setup_id",
            "host",
            "workspace_id",
            "principal_id",
            "profile",
            "authoring_intent",
            "credential_reference",
            "status",
            "setup_generation",
        }


def test_status_answers_for_one_host_or_for_all_of_them(tmp_path: Path) -> None:
    with served(tmp_path) as harness:
        harness.secret()
        administer(
            harness.endpoint,
            LocalControlKind.MCP_CONFIGURE,
            {
                "host": "codex",
                "workspace_id": "ws-one",
                "profile": "restricted",
                "authoring_intent": False,
            },
        )
        every = result(administer(harness.endpoint, LocalControlKind.MCP_STATUS, {}))
        one = result(
            administer(
                harness.endpoint, LocalControlKind.MCP_STATUS, {"host": "codex"}
            )
        )
        assert isinstance(every["setups"], list) and len(every["setups"]) == 2
        assert isinstance(one["setups"], list) and len(one["setups"]) == 1


def test_revoking_a_host_that_was_never_configured_is_already_that_state(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        answer = result(
            administer(harness.endpoint, LocalControlKind.MCP_REVOKE, {"host": "codex"})
        )
        assert answer["setup"] is None


def test_an_unknown_workspace_is_refused_rather_than_configured(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        answer = administer(
            harness.endpoint,
            LocalControlKind.MCP_CONFIGURE,
            {
                "host": "codex",
                "workspace_id": "ws-not-authorised",
                "profile": "restricted",
                "authoring_intent": False,
            },
        )
        assert refusal(answer) == LocalControlError.REFUSED.value


def test_administration_is_refused_when_the_service_holds_no_administrator(
    tmp_path: Path,
) -> None:
    """The authorization boundary, at the only place it can be crossed.

    A control carries no role, so the only way administration can be unauthorized
    is the service's own session -- and that is exactly what is varied here. A
    caller has no argument that could change this outcome.
    """
    with served(tmp_path) as harness:
        seam = OwnedInstalledMcp(
            authority=harness.authority, administrator=administrator(roles=frozenset())
        )
        with pytest.raises(LocalControlRefusal) as refused:
            seam.administer(
                _control(
                    LocalControlKind.MCP_STATUS,
                    arguments={},
                )
            )
        assert refused.value.code is LocalControlError.UNAUTHORIZED


def _control(
    kind: LocalControlKind,
    *,
    credential: str = "",
    arguments: Mapping[str, object] | None = None,
) -> object:
    from omnivia_core_runtime.service.local_control import LocalControlRequest

    return LocalControlRequest(kind=kind, credential=credential, arguments=arguments)


def _assert_closed_without_response(client: socket.socket, message: str) -> None:
    """Accept either Unix spelling of a close with unread inbound bytes."""
    try:
        response = client.recv(1)
    except ConnectionResetError:
        # Linux may send RST when the server closes while invalid bytes remain
        # unread. It is the same protocol result as EOF: no response was emitted.
        return
    assert response == b"", message


# --- the authoring admission check --------------------------------------------


def test_authoring_admission_reads_durable_state_and_answers_for_the_bearer(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        restricted = harness.secret()
        answer = result(
            exchange(
                harness.endpoint,
                {
                    LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                    "kind": LocalControlKind.MCP_AUTHORING_ADMISSION.value,
                    "credential": restricted,
                },
            )
        )
        assert answer["admitted"] is False
        assert answer["workspace_id"] == "ws-one"

        authoring = harness.secret(profile=McpProfile.AUTHORING)
        answer = result(
            exchange(
                harness.endpoint,
                {
                    LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                    "kind": LocalControlKind.MCP_AUTHORING_ADMISSION.value,
                    "credential": authoring,
                },
            )
        )
        assert answer["admitted"] is True

        administer(harness.endpoint, LocalControlKind.MCP_REVOKE, {"host": "claude-code"})
        assert (
            refusal(
                exchange(
                    harness.endpoint,
                    {
                        LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                        "kind": LocalControlKind.MCP_AUTHORING_ADMISSION.value,
                        "credential": authoring,
                    },
                )
            )
            == "unauthenticated"
        )


def test_admission_takes_no_principal_or_workspace_from_the_caller(
    tmp_path: Path,
) -> None:
    """The question is about the bearer presented, and there is no way to ask another."""
    with served(tmp_path) as harness:
        secret = harness.secret(profile=McpProfile.AUTHORING)
        answer = exchange(
            harness.endpoint,
            {
                LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                "kind": LocalControlKind.MCP_AUTHORING_ADMISSION.value,
                "credential": secret,
                "arguments": {"workspace_id": "ws-somebody-else"},
            },
        )
        assert refusal(answer) == LocalControlError.MALFORMED.value


# --- owner and proxy routing --------------------------------------------------


def test_a_follower_reaches_the_owner_and_never_the_database(tmp_path: Path) -> None:
    """The proxy seam against the live owner, over the real wire.

    The follower here holds nothing but an endpoint: no store, no authority, no
    connection. Everything it answers came back from the owning process, which is
    the property that makes a revocation take effect for followers at the same
    instant it takes effect for the owner.

    The authoring role crosses with the rest of the resolution: a follower that
    dropped it would hold `evidence.capture` and be refused for want of the role
    that operation requires, which is authority lost in transit rather than
    withheld on purpose.
    """
    with served(tmp_path) as harness:
        secret = harness.secret(profile=McpProfile.AUTHORING)
        follower = ProxiedInstalledMcp(endpoint=harness.endpoint, exchange=exchange)

        session = follower.authenticate(secret)
        assert session.workspaces == frozenset({"ws-one"})
        assert session.roles == frozenset({WORKSPACE_CONTRIBUTOR_ROLE})
        assert session.installations == frozenset()
        assert "evidence.capture" in session.operations

        # The same round trip for a restricted setup carries no role, because the
        # profile it resolves to holds none: what crosses is the resolution.
        restricted = harness.secret(profile=McpProfile.RESTRICTED)
        assert follower.authenticate(restricted).roles == frozenset()
        secret = harness.secret(profile=McpProfile.AUTHORING)

        status = follower.administer(_control(LocalControlKind.MCP_STATUS, arguments={}))  # type: ignore[arg-type]
        setups = status["setups"]
        assert isinstance(setups, list) and len(setups) == 1

        follower.administer(  # type: ignore[arg-type]
            _control(LocalControlKind.MCP_REVOKE, arguments={"host": "claude-code"})
        )
        with pytest.raises(LocalControlRefusal) as refused:
            follower.authenticate(secret)
        assert refused.value.code is LocalControlError.UNAUTHENTICATED


def test_a_follower_that_cannot_reach_the_owner_says_so(tmp_path: Path) -> None:
    """`unavailable`, never `unauthenticated`: the two send a human two ways.

    Collapsing an unreachable owner into a rejected bearer would send someone to
    rotate a credential that was never the problem.
    """
    with tempfile.TemporaryDirectory(prefix="lc-dead-") as directory:
        follower = ProxiedInstalledMcp(
            endpoint=LocalEndpoint(EndpointScheme.UNIX, f"{directory}/absent.sock"),
            exchange=exchange,
        )
        with pytest.raises(LocalControlRefusal) as refused:
            follower.authenticate("anything")
        assert refused.value.code is LocalControlError.UNAVAILABLE


SESSION_VIEW: Mapping[str, object] = {
    "principal_id": "mcp-claude-code-forged",
    "workspaces": ["ws-one"],
    "operations": ["workspace.create"],
    "scopes": [],
    "purposes": [],
    "roles": [],
    "capabilities": [],
}


def answering(result: Mapping[str, object], **overrides: object) -> ControlExchange:
    """A peer that answers one forwarded `mcp.authenticate` with chosen members."""

    def forged(
        _endpoint: LocalEndpoint, _document: Mapping[str, object]
    ) -> Mapping[str, object]:
        answer: dict[str, object] = {
            LOCAL_CONTROL_RESULT_FIELD: LOCAL_CONTROL_VERSION,
            "kind": LocalControlKind.MCP_AUTHENTICATE.value,
            "result": dict(result),
        }
        answer.update(overrides)
        return answer

    return forged


def following(exchange_fn: ControlExchange) -> ProxiedInstalledMcp:
    return ProxiedInstalledMcp(
        endpoint=LocalEndpoint(EndpointScheme.UNIX, "/nonexistent.sock"),
        exchange=exchange_fn,
    )


def test_a_forwarded_session_comes_back_holding_at_most_the_one_bounded_role() -> None:
    """An owner impersonator could answer anything; the widest it reaches is one role.

    `installations` is still not on this wire and cannot be put on it: the
    admitted key set is exact, so a reply naming it is refused outright. That is
    stronger than reading and discarding it -- a field that is merely ignored is a
    field the wrong peer still succeeded in placing on this wire, and the next
    build to read it inherits the hole.
    """
    with pytest.raises(LocalControlRefusal) as refused:
        following(
            answering({**SESSION_VIEW, "installations": [INSTALLATION_ID]})
        ).authenticate("anything")
    assert refused.value.code is LocalControlError.UNAVAILABLE

    # The one admitted role is admitted, and nothing else comes with it.
    session = following(
        answering({**SESSION_VIEW, "roles": [WORKSPACE_CONTRIBUTOR_ROLE]})
    ).authenticate("anything")
    assert session.roles == frozenset({WORKSPACE_CONTRIBUTOR_ROLE})
    assert session.installations == frozenset()
    assert session.workspaces == frozenset({"ws-one"})

    # An answer holding no role is admitted holding none: nothing is defaulted in.
    assert following(answering(SESSION_VIEW)).authenticate("anything").roles == frozenset()


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param([INSTALLATION_ADMINISTRATOR_ROLE], id="the installation administrator"),
        pytest.param([KNOWLEDGE_REVIEWER_ROLE], id="the knowledge reviewer"),
        pytest.param(
            [WORKSPACE_CONTRIBUTOR_ROLE, INSTALLATION_ADMINISTRATOR_ROLE],
            id="the admitted role with one smuggled beside it",
        ),
        pytest.param(["workspace_contributor "], id="a role that only looks like the one"),
        pytest.param(["root"], id="a role this build has never heard of"),
        pytest.param(
            [WORKSPACE_CONTRIBUTOR_ROLE, WORKSPACE_CONTRIBUTOR_ROLE],
            id="the admitted role twice, which the owner cannot send",
        ),
        pytest.param([WORKSPACE_CONTRIBUTOR_ROLE] * 5000, id="past the wire item bound"),
        pytest.param(["r" * 400], id="past the bound an identifier has"),
        pytest.param(WORKSPACE_CONTRIBUTOR_ROLE, id="a bare string where a list belongs"),
        pytest.param([None], id="a member that is not text at all"),
        pytest.param({"roles": [WORKSPACE_CONTRIBUTOR_ROLE]}, id="a mapping"),
    ],
)
def test_a_forged_role_on_the_wire_is_unavailable(roles: object) -> None:
    """Nothing off this wire is filtered down: a role list this build cannot admit
    refuses the whole resolution rather than yielding the part of it that was legal.
    """
    follower = following(answering({**SESSION_VIEW, "roles": roles}))
    with pytest.raises(LocalControlRefusal) as refused:
        follower.authenticate("anything")
    assert refused.value.code is LocalControlError.UNAVAILABLE


@pytest.mark.parametrize(
    ("result", "overrides"),
    [
        pytest.param(
            SESSION_VIEW, {"authority": "granted"}, id="a top-level member"
        ),
        pytest.param(
            {**SESSION_VIEW, "capabilities": [{"id": "c", "version": "1", "grants": []}]},
            {},
            id="a member inside one capability",
        ),
        pytest.param(
            {**SESSION_VIEW, "principal_id": "p" * 400},
            {},
            id="an identifier past any bound an identifier has",
        ),
        pytest.param(
            {key: value for key, value in SESSION_VIEW.items() if key != "scopes"},
            {},
            id="a member this build requires and the peer omitted",
        ),
        pytest.param(
            {key: value for key, value in SESSION_VIEW.items() if key != "roles"},
            {},
            id="roles omitted, which is now a member and not an absence",
        ),
    ],
)
def test_a_forwarded_reply_this_build_cannot_admit_is_unavailable(
    result: Mapping[str, object], overrides: Mapping[str, object]
) -> None:
    """Nothing off this wire is read-and-ignored, and nothing is defaulted."""
    follower = following(answering(result, **overrides))
    with pytest.raises(LocalControlRefusal) as refused:
        follower.authenticate("anything")
    assert refused.value.code is LocalControlError.UNAVAILABLE


# --- the wire itself ----------------------------------------------------------


def test_a_non_canonical_control_frame_is_refused_by_the_frame_rules(
    tmp_path: Path,
) -> None:
    """A control does not get a second, looser encoding: it is an OVC1 frame.

    Sent by hand, because the framing helper only produces canonical bytes -- the
    case worth proving is the one a hand-rolled or hostile client sends.
    """
    with served(tmp_path) as harness:
        body = (
            b'{"kind": "mcp.status", "local_control": "'
            + LOCAL_CONTROL_VERSION.encode()
            + b'", "arguments": {}}'
        )
        frame = b"OVC1" + len(body).to_bytes(4, "big") + body
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        try:
            client.connect(harness.endpoint.name)
            client.sendall(frame)
            _assert_closed_without_response(
                client, "non-canonical JSON received a response"
            )
        finally:
            client.close()


def test_a_control_followed_by_a_second_frame_is_refused(tmp_path: Path) -> None:
    """The unary boundary applies to a control exactly as it does to a request."""
    with served(tmp_path) as harness:
        document = {
            LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
            "kind": LocalControlKind.MCP_STATUS.value,
            "arguments": {},
        }
        frame = encode_frame(document)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        try:
            client.connect(harness.endpoint.name)
            client.sendall(frame + frame)
            _assert_closed_without_response(
                client, "pipelined traffic received a response"
            )
        finally:
            client.close()


def test_an_oversized_control_is_refused_before_it_is_buffered(
    tmp_path: Path,
) -> None:
    with served(tmp_path) as harness:
        header = b"OVC1" + (8 * 1024 * 1024).to_bytes(4, "big")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        try:
            client.connect(harness.endpoint.name)
            client.sendall(header)
            _assert_closed_without_response(
                client, "oversized traffic received a response"
            )
        finally:
            client.close()


def test_a_refusal_carried_on_the_wire_never_holds_the_bearer(
    tmp_path: Path,
) -> None:
    """The disclosure case, proved on the bytes that actually leave the process."""
    with served(tmp_path) as harness:
        secret = harness.secret()
        administer(harness.endpoint, LocalControlKind.MCP_REVOKE, {"host": "claude-code"})
        answer = call(harness.endpoint, secret)
        assert refusal(answer) == "unauthenticated"
        assert secret not in repr(answer)
        raw = encode_frame(answer)
        assert secret.encode() not in raw
        assert len(raw) > HEADER_BYTES
        assert decode_frame(raw) == answer


def test_a_server_wired_without_the_seams_serves_no_control(tmp_path: Path) -> None:
    """An endpoint that was not given the seams says so, and serves everything else.

    This is the shape every existing caller of `LocalSocketServer` still has, so
    the case is also the proof that adding the fields changed nothing for them.
    """
    with tempfile.TemporaryDirectory(prefix="lc-bare-") as directory:
        dispatch = RecordingDispatch(sessions=[])
        endpoint = LocalEndpoint(EndpointScheme.UNIX, f"{directory}/s.sock")
        with LocalSocketServer(
            router=DocumentRouter(probes=probe_router(), dispatch=dispatch),
            endpoint=endpoint,
            timeout=2.0,
        ):
            assert (
                refusal(
                    administer(endpoint, LocalControlKind.MCP_STATUS, {})
                )
                == LocalControlError.UNSUPPORTED.value
            )
            assert refusal(call(endpoint, "anything")) == "unsupported"
            plain = exchange(endpoint, request_document("ws-one"))
            assert plain["result"] == {"principal": "local-owner"}


def test_setup_view_is_the_one_place_wire_members_are_chosen(tmp_path: Path) -> None:
    """A field added to the store does not start travelling on its own."""
    with served(tmp_path) as harness:
        harness.secret()
        setup = harness.authority.status(administrator())[0]
        assert set(setup_view(setup)) < set(vars(setup)) | {"host", "profile", "status"}
        assert "credential_salt" not in setup_view(setup)


# --- the correlation invariant on the authenticated path ----------------------


@dataclass
class MisdirectedDispatch:
    """Answers with a response that does not correlate to the request it was given.

    The failure a real dispatcher would produce by wiring one request's envelope
    to another's answer. It cannot come from the ordinary router path, which
    already refuses it -- which is precisely why the authenticated path had to be
    given the same check rather than assumed to inherit it.
    """

    member: str

    def dispatch(
        self, credential: str, request: RequestEnvelope
    ) -> SuccessResponseEnvelope:
        response = success(request, {"ok": True})
        assert isinstance(response, SuccessResponseEnvelope)
        return replace(
            response,
            metadata=replace(response.metadata, **{self.member: "req-somebody-else"}),
        )


@contextmanager
def bare(**seams: object) -> Iterator[LocalEndpoint]:
    """One server with the given seams and nothing else wired to a store."""
    with tempfile.TemporaryDirectory(prefix="lc-bare-") as directory:
        endpoint = LocalEndpoint(EndpointScheme.UNIX, f"{directory}/s.sock")
        with LocalSocketServer(
            router=DocumentRouter(
                probes=probe_router(), dispatch=RecordingDispatch(sessions=[])
            ),
            endpoint=endpoint,
            timeout=2.0,
            **seams,  # type: ignore[arg-type]
        ):
            yield endpoint


@pytest.mark.parametrize("member", ["request_id", "correlation_id"])
def test_an_answer_that_does_not_correlate_to_its_request_never_reaches_a_caller(
    member: str,
) -> None:
    """The router's invariant, on the path that does not go through the router.

    A caller matches an answer to what it asked by `request_id` and
    `correlation_id`; an envelope carrying somebody else's is not an answer to
    this request, and forwarding it would have the caller correlate it as one.
    """
    with bare(authenticated=MisdirectedDispatch(member)) as endpoint:
        answer = call(endpoint, "anything")
        assert refusal(answer) == LocalControlError.MALFORMED.value
        # Fixed and non-disclosing: neither the request the caller sent nor the
        # identifier the faulty dispatch substituted is named back at it.
        rendered = repr(answer)
        assert "req-1" not in rendered
        assert "req-somebody-else" not in rendered
        assert "correlation" not in rendered


def test_a_correlating_answer_on_the_same_path_is_returned_untouched() -> None:
    """The control case for the one above: the check refuses nothing correct."""

    @dataclass
    class Answering:
        def dispatch(
            self, credential: str, request: RequestEnvelope
        ) -> SuccessResponseEnvelope:
            response = success(request, {"ok": True})
            assert isinstance(response, SuccessResponseEnvelope)
            return response

    with bare(authenticated=Answering()) as endpoint:
        response = result(call(endpoint, "anything"))["response"]
        assert isinstance(response, dict)
        assert response["result"] == {"ok": True}


# --- the coordinator seam: takeover, failover and close -----------------------


def coordinators(root: Path, *names: str) -> list[InstallationAuthorityCoordinator]:
    """Two or more services contending for one installation, wired as `main` does."""
    shared = {
        "installation_root": (root / "installation").resolve(),
        "workspace_storage_root": (root / "workspaces").resolve(),
        "core_version": "0.1.0",
        "clock": TickClock(),
        "principal_id": "local-owner",
        "probe": Dispatcher.for_service_operations(
            Grant(
                principal="local-owner",
                workspaces=frozenset(),
                operations=frozenset(SERVICE_OPERATIONS),
            )
        ),
        "facts": SimpleNamespace(
            probe_facts=lambda: ServiceFacts(
                observed_at=OBSERVED_AT,
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            )
        ),
    }
    return [
        InstallationAuthorityCoordinator(owner_instance_id=name, **shared)  # type: ignore[arg-type]
        for name in names
    ]


def descriptor_path(root: Path) -> Path:
    return (
        (root / "installation").resolve()
        / RUNTIME_DIR
        / AUTHORITY_RUNTIME_DIRECTORY
        / AUTHORITY_DESCRIPTOR_NAME
    )


def status_control() -> object:
    return _control(LocalControlKind.MCP_STATUS, arguments={})


def test_a_follower_takes_the_seam_over_when_the_published_owner_has_gone(
    tmp_path: Path,
) -> None:
    """A stale descriptor is a dead owner, not a permanent `unavailable`.

    The owner exits without clearing what it published -- a crash, a kill -- and
    the follower's next control finds a descriptor naming a socket nobody is
    listening on. The repair is the same bounded election installation requests
    already run: the follower wins the lifetime lock, and answers from the
    catalogue it now owns rather than refusing from the descriptor it read.
    """
    first, second = coordinators(tmp_path, "installation-host-a", "installation-host-b")
    try:
        first.start()
        second.start()
        # The follower reaches the live owner, over the real wire.
        assert second.administer(status_control())["setups"] == []  # type: ignore[arg-type]

        published = descriptor_path(tmp_path).read_bytes()
        first.close()
        # The descriptor an exiting owner would have left behind.
        descriptor_path(tmp_path).write_bytes(published)

        answered = second.administer(status_control())  # type: ignore[arg-type]
        assert answered["setups"] == []
        # It answers because it *became* the owner, not because it reached one:
        # the election ran, the lifetime lock was won, and the seam is local now.
        assert second._mcp is not None
    finally:
        first.close()
        second.close()


def test_a_taken_over_seam_administers_and_authenticates_from_its_own_catalogue(
    tmp_path: Path,
) -> None:
    """Takeover is not just reachability: the new owner writes and resolves."""
    first, second = coordinators(tmp_path, "installation-host-a", "installation-host-b")
    try:
        first.start()
        second.start()
        store = first._store
        assert store is not None
        register_workspace(store, "one")

        published = descriptor_path(tmp_path).read_bytes()
        first.close()
        descriptor_path(tmp_path).write_bytes(published)

        provisioned = second.administer(  # type: ignore[arg-type]
            _control(
                LocalControlKind.MCP_CONFIGURE,
                arguments={
                    "host": "claude-code",
                    "workspace_id": "ws-one",
                    "profile": "authoring",
                    "authoring_intent": True,
                },
            )
        )
        secret = provisioned["secret"]
        assert isinstance(secret, str) and secret
        session = second.authenticate(secret)
        assert session.workspaces == frozenset({"ws-one"})
        # The new owner resolves the stored rights, which for an authoring setup
        # include the one bounded role R004 section 9.1 requires -- and nothing a
        # role could be widened to.
        assert session.roles == frozenset({"workspace_contributor"})
        assert session.installations == frozenset()
    finally:
        first.close()
        second.close()


def test_an_unreachable_owner_that_cannot_be_replaced_refuses_within_the_window(
    tmp_path: Path,
) -> None:
    """Bounded, both ways: it retries for the window, and then it stops.

    The catalogue is held by somebody else, so the election cannot be won, and
    the descriptor names a socket nobody answers. The honest answer is
    `unavailable` -- reached after the same failover window installation requests
    get, not on the first observation and not never.
    """
    first, second = coordinators(tmp_path, "installation-host-a", "installation-host-b")
    blocker = None
    try:
        first.start()
        second.start()
        published = descriptor_path(tmp_path).read_bytes()
        first.close()
        blocker = open_installation_store(
            (tmp_path / "installation").resolve(), owner_instance_id="blocker"
        )
        descriptor_path(tmp_path).write_bytes(published)

        started = time.monotonic()
        with pytest.raises(LocalControlRefusal) as refused:
            second.administer(status_control())  # type: ignore[arg-type]
        elapsed = time.monotonic() - started
        assert refused.value.code is LocalControlError.UNAVAILABLE
        assert elapsed >= AUTHORITY_FAILOVER_TIMEOUT_SECONDS
        assert elapsed < AUTHORITY_FAILOVER_TIMEOUT_SECONDS + 10.0
    finally:
        if blocker is not None:
            blocker.close()
        first.close()
        second.close()


def test_the_owned_seam_is_cleared_before_the_catalogue_it_reads_is_closed(
    tmp_path: Path,
) -> None:
    """No internal call after `close` may reach a store this object has closed.

    `OwnedInstalledMcp` holds an authority built on the catalogue, so a seam left
    behind is one whose next call reads a closed database. Proved on the
    behaviour rather than the field: the refusal is `unavailable`, which is what
    a coordinator with no seam and no owner answers -- a retained one would have
    reached the store instead.
    """
    (only,) = coordinators(tmp_path, "installation-host-a")
    try:
        only.start()
        assert only.administer(status_control())["setups"] == []  # type: ignore[arg-type]
        only.close()
        for ask in (
            lambda: only.administer(status_control()),  # type: ignore[arg-type]
            lambda: only.authenticate("anything"),
        ):
            with pytest.raises(LocalControlRefusal) as refused:
                ask()
            assert refused.value.code is LocalControlError.UNAVAILABLE
        assert only._mcp is None
    finally:
        only.close()


def test_close_after_close_stays_fail_closed_and_elects_nothing(
    tmp_path: Path,
) -> None:
    """A closed coordinator does not touch the lifetime lock to answer a control."""
    (only,) = coordinators(tmp_path, "installation-host-a")
    only.start()
    only.close()
    only.close()
    started = time.monotonic()
    with pytest.raises(LocalControlRefusal):
        only.authenticate("anything")
    # Immediate: the closed check precedes the poll, so there is no window spent
    # waiting for an owner this process has already stopped being.
    assert time.monotonic() - started < AUTHORITY_FAILOVER_TIMEOUT_SECONDS
    assert only._mcp is None
    assert only._store is None
