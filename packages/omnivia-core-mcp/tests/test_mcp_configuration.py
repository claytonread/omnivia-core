"""The fixed-principal MCP configuration fails closed before initialization."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client import CredentialReference, owner_private
from omnivia_core_mcp.configuration import (
    CONFIGURATION_FORMAT,
    MAXIMUM_CONFIGURATION_BYTES,
    McpConfiguration,
    McpConfigurationError,
    effective_profile,
    parse_configuration,
    read_configuration,
)

INSTALLATION_STATE = Path.cwd().resolve() / "installation-state"


def managed_document(**overrides: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": CONFIGURATION_FORMAT,
        "principal_id": "local-user",
        "allowed_workspace_ids": ["workspace-alpha"],
        "default_workspace_id": "workspace-alpha",
        "allowed_purposes": ["workspace_inspection", "knowledge_retrieval"],
        "mutation_enabled": False,
        "service_mode": "managed_local",
        "installation_state": str(INSTALLATION_STATE),
    }
    document.update(overrides)
    return document


def service_document(**overrides: object) -> dict[str, Any]:
    document = managed_document(
        service_mode="service_client",
        endpoint="https://Core.Example/",
        credential_reference="core.default",
    )
    del document["installation_state"]
    document.update(overrides)
    return document


def write_config(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    make_private(path)
    return path


def make_private(path: Path) -> None:
    """Apply the platform's owner-only test-file policy."""
    path.chmod(0o600)
    if os.name != "nt":
        return
    identity = subprocess.run(
        ["whoami"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:F",
            "/q",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def assert_payload_free(error: BaseException, *values: str) -> None:
    rendered = " ".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(error.__cause__),
            repr(error.__context__),
        )
    )
    for value in values:
        assert value not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_managed_configuration_is_immutable_and_selects_its_default() -> None:
    config = parse_configuration(managed_document())
    assert config.service_mode == "managed_local"
    assert config.installation_state == INSTALLATION_STATE
    assert config.selected_workspace_id == "workspace-alpha"
    assert config.mutation_enabled is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.principal_id = "other"  # type: ignore[misc]


def test_a_single_allowlisted_workspace_is_an_unambiguous_implicit_default() -> None:
    document = managed_document()
    del document["default_workspace_id"]
    assert parse_configuration(document).selected_workspace_id == "workspace-alpha"


def test_several_workspaces_without_a_default_select_nothing() -> None:
    document = managed_document(allowed_workspace_ids=["workspace-a", "workspace-b"])
    del document["default_workspace_id"]
    assert parse_configuration(document).selected_workspace_id is None


def test_service_configuration_uses_client_value_types_and_normalizes_origin() -> None:
    config = parse_configuration(service_document())
    assert config.endpoint == "https://core.example:443"
    assert config.credential_reference == CredentialReference("core.default")
    assert config.installation_state is None


@pytest.mark.parametrize(
    "change",
    [
        {"format": "omnivia.mcp-config.v2"},
        {"principal_id": ""},
        {"principal_id": "has space"},
        {"principal_id": "x" * 129},
        {"allowed_workspace_ids": []},
        {"allowed_workspace_ids": ["workspace-alpha", "workspace-alpha"]},
        {"allowed_workspace_ids": ["../escape"]},
        {"allowed_purposes": []},
        {"allowed_purposes": ["knowledge_retrieval", "knowledge_retrieval"]},
        {"allowed_purposes": ["has space"]},
        {"mutation_enabled": 1},
        {"service_mode": "ambient"},
        {"default_workspace_id": "workspace-other"},
        {"unknown": True},
    ],
)
def test_invalid_or_authority_widening_values_are_refused(
    change: dict[str, object],
) -> None:
    with pytest.raises(McpConfigurationError):
        parse_configuration(managed_document(**change))


@pytest.mark.parametrize(
    "missing",
    [
        "format",
        "principal_id",
        "allowed_workspace_ids",
        "allowed_purposes",
        "service_mode",
    ],
)
def test_required_fields_are_required(missing: str) -> None:
    document = managed_document()
    del document[missing]
    with pytest.raises(McpConfigurationError):
        parse_configuration(document)


def test_mode_fields_are_exact_and_mutually_exclusive() -> None:
    for document in (
        managed_document(
            endpoint="https://core.example", credential_reference="core.default"
        ),
        managed_document(installation_state="relative/path"),
        service_document(installation_state="/tmp/state"),
        service_document(endpoint="http://core.example:80"),
        service_document(endpoint="https://user:secret@core.example"),
        service_document(credential_reference="eyHeader.payload.signature"),
    ):
        with pytest.raises(McpConfigurationError):
            parse_configuration(document)


def test_direct_construction_and_dataclass_replacement_revalidate() -> None:
    config = parse_configuration(managed_document())
    with pytest.raises(McpConfigurationError):
        dataclasses.replace(config, allowed_workspace_ids=("../escape",))
    with pytest.raises(McpConfigurationError):
        dataclasses.replace(config, mutation_enabled=1)  # type: ignore[arg-type]


def test_configuration_repr_redacts_private_values() -> None:
    config = parse_configuration(service_document())
    rendered = repr(config)
    assert rendered == "McpConfiguration(<redacted>)"
    assert "core.example" not in rendered
    assert "core.default" not in rendered


# --- the effective exposure profile ---------------------------------------------
#
# Two conditions, both required and neither sufficient: `mutation_enabled` is the
# ceiling the public document sets, and the injected admission is the floor only
# protected state can raise. The seam has no implementation in this repository --
# it is what Phase 6's installed setup path must supply -- so every test here
# injects one, and the *absence* of an injection is itself a case below because
# that is what production is.

WORKSPACE = "workspace-alpha"
PRINCIPAL = "local-user"

#: Stands in for the connected `ServiceClient` `server.connect` hands the seam.
#:
#: A bare sentinel because this module's whole interest in it is that it arrives
#: unchanged: nothing in `effective_profile` reads it, dials it or unwraps it, and
#: a real client here would let an implementation that started touching it pass.
#: The authority suite is where the connection itself is under test.
CLIENT: Any = object()


def admits(answer: object) -> Any:
    """A protected admission that records what it was asked, and about whom."""

    def admission(client: Any, principal_id: str, workspace_id: str) -> Any:
        asked.append((client, principal_id, workspace_id))
        return answer

    asked: list[tuple[Any, str, str]] = []
    admission.asked = asked  # type: ignore[attr-defined]
    return admission


def test_mutation_enabled_false_or_absent_is_restricted_whatever_admission_says() -> (
    None
):
    """The ceiling is checked first and is never negotiated.

    A protected record of authoring intent does not widen a configuration that
    does not permit authoring, and an absent field is the same as a false one --
    which is what makes every existing installed configuration restricted
    without being rewritten. The admission is not even consulted: there is no
    question to ask once the ceiling has answered.
    """
    document = managed_document()
    del document["mutation_enabled"]
    for config in (
        parse_configuration(document),
        parse_configuration(managed_document()),
    ):
        admission = admits(True)
        assert (
            effective_profile(config, CLIENT, WORKSPACE, authoring_admission=admission)
            == "restricted"
        )
        assert admission.asked == []


def test_mutation_enabled_true_alone_is_still_restricted() -> None:
    """The upgrade rule, and the one that matters most: editing the public
    configuration file is not evidence of anything.

    This is also the production default. `server.main` injects no admission, so a
    legacy or hand-written `mutation_enabled: true` raises a ceiling over an empty
    room and the installed server advertises the read-only six.
    """
    config = parse_configuration(managed_document(mutation_enabled=True))
    assert config.mutation_enabled is True
    assert effective_profile(config, CLIENT, WORKSPACE) == "restricted"


def test_authoring_needs_the_ceiling_and_the_protected_admission_together() -> None:
    """Both, and the admission is asked with exactly three things: the connected
    client, this principal and this workspace -- not a name from a tool call,
    which is why the profile is settled at startup where no such name exists.

    The client goes first and arrives untouched. That is what lets Phase 6 read
    its protected record through the authority this session already established,
    instead of opening the installation database or dialling again."""
    config = parse_configuration(managed_document(mutation_enabled=True))
    admission = admits(True)
    assert (
        effective_profile(config, CLIENT, WORKSPACE, authoring_admission=admission)
        == "authoring"
    )
    assert admission.asked == [(CLIENT, PRINCIPAL, WORKSPACE)]


@pytest.mark.parametrize(
    "answer",
    [False, None, "true", 1, ["authoring"]],
    ids=["denied", "no-answer", "truthy-string", "truthy-int", "truthy-list"],
)
def test_anything_but_a_true_admission_is_a_denial(answer: object) -> None:
    """`is True`, not truthiness. A seam that answered with a record, a reason or
    a status code has not said yes, and reading a non-empty value as consent is
    how a protected boundary becomes an accident."""
    config = parse_configuration(managed_document(mutation_enabled=True))
    assert (
        effective_profile(config, CLIENT, WORKSPACE, authoring_admission=admits(answer))
        == "restricted"
    )


def test_an_admission_that_raises_fails_closed() -> None:
    """A protected authority that could not be consulted has confirmed nothing.

    Widening the surface because a lookup broke would widen it for exactly the
    reason it should not, so the refusal is silent here and loud nowhere: the
    server simply advertises the narrow inventory.
    """

    def broken(_client: Any, _principal_id: str, _workspace_id: str) -> bool:
        raise RuntimeError("/private/installation-state/mcp-principals.sqlite")

    config = parse_configuration(managed_document(mutation_enabled=True))
    assert (
        effective_profile(config, CLIENT, WORKSPACE, authoring_admission=broken)
        == "restricted"
    )


def test_a_private_regular_file_is_read_by_explicit_absolute_path(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    assert read_configuration(path).principal_id == "local-user"


def test_a_relative_path_is_not_a_configuration_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path / "mcp.json", managed_document())
    monkeypatch.chdir(tmp_path)
    with pytest.raises(McpConfigurationError):
        read_configuration(Path("mcp.json"))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
def test_posix_group_or_other_access_is_refused(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    path.chmod(mode)
    with pytest.raises(McpConfigurationError) as raised:
        read_configuration(path)
    assert_payload_free(raised.value, str(path), "local-user")


def test_a_non_regular_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(McpConfigurationError):
        read_configuration(tmp_path)


@pytest.mark.skipif(
    os.name == "nt", reason="creating a Windows symlink requires host privilege"
)
def test_a_symlink_is_refused(tmp_path: Path) -> None:
    target = write_config(tmp_path / "target.json", managed_document())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(McpConfigurationError):
        read_configuration(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership uses the effective uid")
def test_an_owner_mismatch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    real_effective_user = os.geteuid()
    monkeypatch.setattr(owner_private.os, "geteuid", lambda: real_effective_user + 1)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)


def test_a_replacement_during_the_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    original = owner_private.same_file
    comparisons = 0

    def disagree_after_open(first: os.stat_result, second: os.stat_result) -> bool:
        nonlocal comparisons
        comparisons += 1
        return comparisons == 1 and original(first, second)

    monkeypatch.setattr(owner_private, "same_file", disagree_after_open)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)


def test_windows_requires_an_owner_only_acl_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)
    monkeypatch.setattr(owner_private, "_windows_owner_only", lambda d: False)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)
    monkeypatch.setattr(owner_private, "_windows_owner_only", lambda d: True)
    assert read_configuration(path).service_mode == "managed_local"


def test_windows_acl_verifier_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    monkeypatch.setattr(owner_private, "_IS_WINDOWS", True)

    def broken(_descriptor: int) -> bool:
        raise RuntimeError("C:/private/credential-store")

    monkeypatch.setattr(owner_private, "_windows_owner_only", broken)
    with pytest.raises(McpConfigurationError) as raised:
        read_configuration(path)
    assert_payload_free(raised.value, "C:/private/credential-store", str(path))


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf{}",
        b"\xff",
        b"{} trailing",
        b'{"format":"a","format":"b"}',
        b'{"outer":{"field":1,"field":2}}',
        b"NaN",
        b"[]",
    ],
)
def test_malformed_documents_are_refused_without_payload_leakage(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(content)
    make_private(path)
    with pytest.raises(McpConfigurationError) as raised:
        read_configuration(path)
    assert_payload_free(raised.value, str(path), "trailing", "outer")


def test_the_byte_limit_is_checked_with_a_bounded_read(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(b" " * (MAXIMUM_CONFIGURATION_BYTES + 1))
    make_private(path)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)


def test_exactly_the_maximum_is_not_rejected_as_oversized(tmp_path: Path) -> None:
    encoded = json.dumps(managed_document()).encode("utf-8")
    path = tmp_path / "mcp.json"
    path.write_bytes(encoded + b" " * (MAXIMUM_CONFIGURATION_BYTES - len(encoded)))
    make_private(path)
    assert read_configuration(path).format == CONFIGURATION_FORMAT


def test_semantic_failures_drop_sensitive_values_and_parser_context() -> None:
    secret = "Bearer secret-that-must-not-render"
    with pytest.raises(McpConfigurationError) as raised:
        parse_configuration(service_document(credential_reference=secret))
    assert_payload_free(raised.value, secret, "core.example", "local-user")


def test_the_public_model_accepts_only_tuple_authority_sets() -> None:
    with pytest.raises(McpConfigurationError):
        McpConfiguration(
            format=CONFIGURATION_FORMAT,
            principal_id="local-user",
            allowed_workspace_ids=["workspace-alpha"],  # type: ignore[arg-type]
            default_workspace_id="workspace-alpha",
            allowed_purposes=("workspace_inspection",),
            mutation_enabled=False,
            service_mode="managed_local",
            installation_state=Path("/var/lib/omnivia/installation-state"),
            endpoint=None,
            credential_reference=None,
        )


# --- the managed-local credential reference ------------------------------------
#
# A `managed_local` configuration written by the installed setup path names the
# credential this installation filed for this host. It is a *name*: the document
# still carries no material, no store location and no path to one.


def test_a_managed_configuration_may_name_the_installed_credential() -> None:
    config = parse_configuration(
        managed_document(credential_reference="omcp-0123456789abcdef")
    )
    assert config.service_mode == "managed_local"
    assert config.credential_reference == CredentialReference("omcp-0123456789abcdef")
    assert config.installation_state == INSTALLATION_STATE
    assert config.endpoint is None


def test_a_managed_configuration_without_a_reference_is_still_accepted() -> None:
    """The shape every installation had before the setup path existed.

    Read, and reaching its service -- but authenticating nothing, which is why
    the profile rule below can never let it author.
    """
    assert parse_configuration(managed_document()).credential_reference is None


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "has space",
        "../../etc/passwd",
        "a/b",
        "..",
        "eyHeader.payload.signature",
        "x" * 300,
        7,
        None,
        ["omcp-0123456789abcdef"],
    ],
)
def test_an_unsafe_credential_reference_fails_closed_before_initialization(
    reference: object,
) -> None:
    """Unsafe credential configuration is refused where it is read.

    Not carried to a store lookup that would have to decide what an inadmissible
    name means, and not deferred to the first call: `read_configuration` is what
    runs before MCP initialization, so a document like this advertises no tool at
    all rather than advertising tools it cannot authenticate.
    """
    with pytest.raises(McpConfigurationError):
        parse_configuration(managed_document(credential_reference=reference))


def test_a_managed_configuration_still_refuses_an_endpoint() -> None:
    """A reference is admitted; a second service location is not."""
    with pytest.raises(McpConfigurationError):
        parse_configuration(
            managed_document(
                credential_reference="omcp-0123456789abcdef",
                endpoint="https://core.example",
            )
        )


def test_direct_construction_of_a_managed_reference_revalidates() -> None:
    config = parse_configuration(
        managed_document(credential_reference="omcp-0123456789abcdef")
    )
    assert (
        dataclasses.replace(config, credential_reference=None).credential_reference
        is None
    )
    with pytest.raises(McpConfigurationError):
        dataclasses.replace(config, credential_reference="omcp-plain-string")  # type: ignore[arg-type]
    with pytest.raises(McpConfigurationError):
        dataclasses.replace(config, endpoint="https://core.example:443")


def test_a_referenced_managed_configuration_still_redacts_its_repr() -> None:
    config = parse_configuration(
        managed_document(credential_reference="omcp-0123456789abcdef")
    )
    assert repr(config) == "McpConfiguration(<redacted>)"
    assert "omcp-0123456789abcdef" not in repr(config)
