"""The local-control wrapper's admission rules, with no socket and no platform.

Every case here is about the *document*: what makes one a control, what makes one
admissible, and what a refusal is allowed to say. It runs identically on POSIX and
Windows because nothing in it opens anything -- the socket-borne behaviour has its
own suite, and keeping the two apart is what stops the wire rules from being
provable on one platform only.
"""

from __future__ import annotations

import pytest
from omnivia_core_runtime.service.local_control import (
    LOCAL_CONTROL_FIELD,
    LOCAL_CONTROL_HOSTS,
    LOCAL_CONTROL_PROFILES,
    LOCAL_CONTROL_RESULT_FIELD,
    LOCAL_CONTROL_VERSION,
    MAXIMUM_CREDENTIAL_CHARACTERS,
    LocalControlError,
    LocalControlKind,
    LocalControlRefusal,
    control_error_document,
    control_result_document,
    decode_local_control,
    is_local_control,
)
from omnivia_core_runtime.service.ovc1 import decode_frame, encode_frame
from omnivia_core_runtime.storage.installation_store import McpHost, McpProfile

SECRET = "s3cr3t-bearer-value-that-must-never-be-echoed"


def application_call(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
        "kind": LocalControlKind.APPLICATION_CALL.value,
        "credential": SECRET,
        "request": {"operation": "memory.get"},
    }
    document.update(overrides)
    return document


def configure(**arguments: object) -> dict[str, object]:
    return {
        LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
        "kind": LocalControlKind.MCP_CONFIGURE.value,
        "arguments": {
            "host": "claude-code",
            "workspace_id": "ws-one",
            "profile": "restricted",
            "authoring_intent": False,
            **arguments,
        },
    }


def test_the_wire_spellings_are_the_store_s_own_and_cannot_drift() -> None:
    """The one duplication this module makes, pinned to what it duplicates.

    `local_control` states the admitted hosts and profiles itself so it can stay
    free of storage imports and off the transport's dependency edge. That is only
    safe while the two agree, so the agreement is asserted rather than assumed.
    """
    assert LOCAL_CONTROL_HOSTS == tuple(host.value for host in McpHost)
    assert LOCAL_CONTROL_PROFILES == tuple(profile.value for profile in McpProfile)


def test_only_the_control_member_makes_a_document_a_control() -> None:
    """The whole compatibility rule: an existing document takes the old path."""
    assert not is_local_control({"operation": "memory.get"})
    assert not is_local_control({"probe": "service.health"})
    assert not is_local_control({})
    # Presence alone, even with a wrong value: a document claiming to be a
    # control is refused as a malformed control, never forwarded to the router to
    # be refused as a malformed request.
    assert is_local_control({LOCAL_CONTROL_FIELD: "some.other.version"})


def test_an_admitted_application_call_carries_its_bearer_and_its_request() -> None:
    control = decode_local_control(application_call())
    assert control.kind is LocalControlKind.APPLICATION_CALL
    assert control.credential == SECRET
    assert control.request == {"operation": "memory.get"}
    assert control.arguments is None


def test_a_control_request_never_renders_its_bearer() -> None:
    """`repr` is the one that gets called by accident, so it is the one pinned."""
    control = decode_local_control(application_call())
    assert SECRET not in repr(control)
    assert SECRET not in str(control)
    assert "redacted" in repr(control)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"kind": "application.call"}, id="no version member"),
        pytest.param(
            application_call(**{LOCAL_CONTROL_FIELD: "omnivia.local-control.v2"}),
            id="a version this build does not speak",
        ),
        pytest.param(
            application_call(**{LOCAL_CONTROL_FIELD: 1}), id="a non-string version"
        ),
        pytest.param({LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION}, id="no kind"),
        pytest.param(
            {LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION, "kind": "mcp.rotate"},
            id="a kind this build does not serve",
        ),
        pytest.param(
            {LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION, "kind": 7},
            id="a non-string kind",
        ),
        pytest.param(application_call(extra=True), id="an unknown member"),
        pytest.param(
            application_call(arguments={"host": "codex"}),
            id="a member belonging to another kind",
        ),
        pytest.param(
            {
                LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                "kind": LocalControlKind.APPLICATION_CALL.value,
                "request": {"operation": "memory.get"},
            },
            id="an application call with no bearer",
        ),
        pytest.param(application_call(credential=""), id="an empty bearer"),
        pytest.param(application_call(credential=" " + SECRET), id="a padded bearer"),
        pytest.param(application_call(credential=42), id="a non-string bearer"),
        pytest.param(
            application_call(credential="x" * (MAXIMUM_CREDENTIAL_CHARACTERS + 1)),
            id="a bearer past the bound",
        ),
        pytest.param(application_call(request=["memory.get"]), id="a non-object request"),
        pytest.param(
            {
                LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                "kind": LocalControlKind.MCP_CONFIGURE.value,
                "credential": SECRET,
                "arguments": {},
            },
            id="an administration control presenting a bearer",
        ),
        pytest.param(configure(host="cursor"), id="a host this build does not serve"),
        pytest.param(configure(profile="admin"), id="a profile that is not one"),
        pytest.param(configure(authoring_intent="true"), id="a non-boolean intent"),
        pytest.param(configure(workspace_id=""), id="an empty workspace"),
        pytest.param(configure(workspace_id="w" * 129), id="a workspace past the bound"),
        pytest.param(configure(unexpected=1), id="an unknown administration argument"),
    ],
)
def test_every_inadmissible_control_is_refused(document: dict[str, object]) -> None:
    with pytest.raises(LocalControlRefusal) as refused:
        decode_local_control(document)
    assert refused.value.code is LocalControlError.MALFORMED


def test_a_configure_missing_one_argument_is_refused_not_defaulted() -> None:
    """Nothing here is optional-by-omission. An absent intent is not a false one."""
    document = configure()
    arguments = dict(document["arguments"])  # type: ignore[call-overload]
    del arguments["authoring_intent"]
    document["arguments"] = arguments
    with pytest.raises(LocalControlRefusal):
        decode_local_control(document)


def test_status_admits_no_host_and_one_host_and_nothing_else() -> None:
    both = {LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION, "kind": "mcp.status"}
    assert decode_local_control({**both, "arguments": {}}).argument("host") is None
    assert (
        decode_local_control({**both, "arguments": {"host": "codex"}}).argument("host")
        == "codex"
    )
    with pytest.raises(LocalControlRefusal):
        decode_local_control({**both, "arguments": {"host": "codex", "profile": "x"}})


def test_no_refusal_can_carry_what_the_caller_wrote() -> None:
    """The property that matters most: a malformed control holds a bearer.

    Every refusal message is a frozen table entry chosen by a code, so there is no
    branch that could interpolate the document -- and the document is exactly
    where the credential is.
    """
    for document in (application_call(extra=SECRET), application_call(credential=42)):
        with pytest.raises(LocalControlRefusal) as refused:
            decode_local_control(document)
        assert SECRET not in str(refused.value)
        rendered = control_error_document(None, refused.value.code)
        assert SECRET not in repr(rendered)


def test_a_refusal_that_could_not_name_a_kind_does_not_invent_one() -> None:
    document = control_error_document(None, LocalControlError.MALFORMED)
    assert document["kind"] == ""
    assert document[LOCAL_CONTROL_RESULT_FIELD] == LOCAL_CONTROL_VERSION
    error = document["error"]
    assert isinstance(error, dict)
    assert error["code"] == "malformed"


def test_a_reply_is_never_replayable_as_a_request() -> None:
    """Different member names in each direction, so neither decodes as the other."""
    reply = control_result_document(LocalControlKind.MCP_STATUS, {"setups": []})
    assert LOCAL_CONTROL_FIELD not in reply
    assert not is_local_control(reply)


@pytest.mark.parametrize(
    "document",
    [
        control_result_document(LocalControlKind.MCP_STATUS, {"setups": []}),
        control_error_document(
            LocalControlKind.APPLICATION_CALL, LocalControlError.UNAUTHENTICATED
        ),
        control_error_document(None, LocalControlError.MALFORMED),
    ],
)
def test_every_reply_this_module_builds_survives_the_canonical_frame(
    document: dict[str, object],
) -> None:
    """A reply that cannot be framed is one a caller would never receive."""
    assert decode_frame(encode_frame(document)) == document


def test_every_refusal_code_has_its_own_frozen_sentence() -> None:
    sentences = {
        code: control_error_document(None, code)["error"] for code in LocalControlError
    }
    rendered = [entry["message"] for entry in sentences.values()]  # type: ignore[index]
    assert len(set(rendered)) == len(LocalControlError)
    assert all(isinstance(sentence, str) and sentence for sentence in rendered)
