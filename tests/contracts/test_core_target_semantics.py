"""Schema and semantic tests for `CoreTargetV1` and `CoreSafeStatusV1` (ADR-038).

`CoreSafeStatusV1` is published pre-authentication and to callers regardless of
authority, so its strict schema shape and its semantic validator are held to the
same things here: it can never carry a secret-shaped field, it can never offer a
process-lifecycle action for a target this caller does not own, and no scalar it
publishes may fall outside the value domain the canonical schema declares -- the
semantic guards cover scalar bounds as well as closed vocabularies. The set-level
authority rule is held here too: two target authorities may not reuse one writable
workspace identity.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CoreTargetV1,
    is_contract_version,
    is_identifier,
    is_release_version,
    is_workspace_id,
)
from omnivia_core.contracts.v1.semantics_core_target import (
    decode_core_safe_status,
    decode_core_target,
    encode_core_safe_status,
    encode_core_target,
    validate_core_safe_status,
    validate_core_target,
    validate_core_target_authorities,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"
TYPESCRIPT_PATH = REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"

#: Field names a status or target must never be able to carry -- endpoint,
#: process, filesystem, credential, and free-form-reason shaped fields.
FORBIDDEN_FIELD_NAMES = (
    "endpoint_url",
    "endpoint_uri",
    "pid",
    "path",
    "credential",
    "token",
    "callback",
    "account",
    "entitlement",
    "vault_reference",
    "stack_trace",
    "raw_exception",
    "reason",
)


def _validator(def_name: str) -> Draft202012Validator:
    entries: list[tuple[str, Resource[Any]]] = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        resource = Resource.from_contents(json.loads(path.read_text(encoding="utf-8")))
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}service.schema.json#/$defs/{def_name}"},
        registry=Registry().with_resources(entries),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


TARGET_VALIDATOR = _validator("CoreTargetV1")
STATUS_VALIDATOR = _validator("CoreSafeStatusV1")


def _local_target_document() -> dict[str, Any]:
    return {
        "contract_version": "1.3",
        "target_ref": "target-local-1",
        "display_name": "Local Core",
        "kind": "local",
        "workspace_ref": "workspace-1",
        "management": "locally_managed",
        "endpoint_profile_ref": "profile-local-1",
    }


def _remote_target_document() -> dict[str, Any]:
    return {
        "contract_version": "1.3",
        "target_ref": "target-remote-1",
        "display_name": "Remote Core",
        "kind": "private_remote",
        "workspace_ref": "workspace-2",
        "management": "externally_managed",
        "endpoint_profile_ref": "profile-remote-1",
    }


def _status_document(
    target: dict[str, Any], permitted_actions: list[str]
) -> dict[str, Any]:
    return {
        "contract_version": "1.3",
        "target": target,
        "lifecycle_state": "running",
        "readiness_state": "ready",
        "compatibility_state": "compatible",
        "connection_state": "connected",
        "warning_codes": [],
        "permitted_actions": permitted_actions,
    }


# --------------------------------------------------------------------------
# Valid local target and status
# --------------------------------------------------------------------------


def test_valid_local_target_decodes_validates_and_round_trips() -> None:
    document = _local_target_document()
    assert TARGET_VALIDATOR.is_valid(document)
    target = decode_core_target(document)
    validate_core_target(target)
    assert encode_core_target(target) == document


def test_valid_local_status_decodes_validates_and_round_trips() -> None:
    document = _status_document(_local_target_document(), ["start", "stop", "restart", "open"])
    assert STATUS_VALIDATOR.is_valid(document)
    status = decode_core_safe_status(document)
    validate_core_safe_status(status)
    assert encode_core_safe_status(status) == document


# --------------------------------------------------------------------------
# Distinct local and remote workspace authorities
# --------------------------------------------------------------------------


def test_local_and_remote_targets_carry_distinct_workspace_authorities() -> None:
    local_target = decode_core_target(_local_target_document())
    remote_target = decode_core_target(_remote_target_document())

    assert local_target.workspace_ref != remote_target.workspace_ref
    assert local_target.kind != remote_target.kind
    assert local_target.management != remote_target.management

    local_status = decode_core_safe_status(
        _status_document(_local_target_document(), ["open"])
    )
    remote_status = decode_core_safe_status(
        _status_document(_remote_target_document(), ["open"])
    )
    assert local_status.target.workspace_ref != remote_status.target.workspace_ref


# --------------------------------------------------------------------------
# Remote / externally-managed local-action refusal
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize(
    "kind,management",
    [
        ("private_remote", "locally_managed"),
        ("cloud", "locally_managed"),
        ("local", "externally_managed"),
        ("private_remote", "externally_managed"),
        ("cloud", "externally_managed"),
    ],
)
def test_a_local_action_is_refused_unless_locally_managed_and_local(
    kind: str, management: str, action: str
) -> None:
    target = replace(
        decode_core_target(_local_target_document()), kind=kind, management=management
    )
    status = replace(
        decode_core_safe_status(_status_document(_local_target_document(), ["open"])),
        target=target,
        permitted_actions=(action, "open"),
    )
    with pytest.raises(ContractSemanticError):
        validate_core_safe_status(status)
    with pytest.raises(ContractSemanticError):
        encode_core_safe_status(status)


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_a_local_action_is_accepted_for_a_locally_managed_local_target(
    action: str,
) -> None:
    status = decode_core_safe_status(
        _status_document(_local_target_document(), [action, "open"])
    )
    validate_core_safe_status(status)
    assert action in encode_core_safe_status(status)["permitted_actions"]


def test_non_local_actions_are_never_refused_for_a_remote_target() -> None:
    status = decode_core_safe_status(
        _status_document(_remote_target_document(), ["reconnect", "open"])
    )
    validate_core_safe_status(status)


# --------------------------------------------------------------------------
# Strict additionalProperties blocks secret-like fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", FORBIDDEN_FIELD_NAMES)
def test_strict_status_schema_rejects_a_secret_shaped_field(field: str) -> None:
    document = _status_document(_local_target_document(), ["open"])
    document[field] = "leaked-value"
    assert not STATUS_VALIDATOR.is_valid(document)


@pytest.mark.parametrize("field", FORBIDDEN_FIELD_NAMES)
def test_strict_target_schema_rejects_a_secret_shaped_field(field: str) -> None:
    document = _local_target_document()
    document[field] = "leaked-value"
    assert not TARGET_VALIDATOR.is_valid(document)


def test_strict_status_schema_has_no_free_form_message_or_reason_field() -> None:
    document = _status_document(_local_target_document(), ["open"])
    for free_form_field in ("message", "reason", "details", "error"):
        mutated = dict(document)
        mutated[free_form_field] = "anything"
        assert not STATUS_VALIDATOR.is_valid(mutated)


# --------------------------------------------------------------------------
# Serialization cannot emit forbidden fields
# --------------------------------------------------------------------------


def test_encoded_status_never_carries_a_forbidden_field() -> None:
    status = decode_core_safe_status(
        _status_document(_local_target_document(), ["start", "stop", "restart", "open"])
    )
    encoded = encode_core_safe_status(status)
    assert set(encoded) <= {
        "contract_version",
        "target",
        "lifecycle_state",
        "readiness_state",
        "compatibility_state",
        "connection_state",
        "server_version",
        "protocol_version",
        "warning_codes",
        "permitted_actions",
    }
    for field in FORBIDDEN_FIELD_NAMES:
        assert field not in encoded
        assert field not in encoded["target"]


def test_encoded_status_warning_codes_and_actions_are_unique_and_enumerated() -> None:
    document = _status_document(_local_target_document(), ["open", "reconnect"])
    document["warning_codes"] = ["degraded", "endpoint_unreachable"]
    status = decode_core_safe_status(document)
    encoded = encode_core_safe_status(status)
    assert len(encoded["warning_codes"]) == len(set(encoded["warning_codes"]))
    assert len(encoded["permitted_actions"]) == len(set(encoded["permitted_actions"]))


# --------------------------------------------------------------------------
# Closed-vocabulary enforcement beyond structural decode
# --------------------------------------------------------------------------


def test_an_unknown_kind_is_refused_semantically_though_structurally_decodable() -> None:
    target = replace(decode_core_target(_local_target_document()), kind="on_prem")
    with pytest.raises(ContractSemanticError):
        validate_core_target(target)
    with pytest.raises(ContractSemanticError):
        encode_core_target(target)


def test_an_unknown_warning_code_is_refused_semantically() -> None:
    status = replace(
        decode_core_safe_status(_status_document(_local_target_document(), ["open"])),
        warning_codes=("disk_on_fire",),
    )
    with pytest.raises(ContractSemanticError):
        validate_core_safe_status(status)


def test_a_duplicate_permitted_action_is_refused() -> None:
    status = replace(
        decode_core_safe_status(_status_document(_local_target_document(), ["open"])),
        permitted_actions=("open", "open"),
    )
    with pytest.raises(ContractSemanticError):
        validate_core_safe_status(status)


# --------------------------------------------------------------------------
# Scalar value domains, taken from the generated guards
# --------------------------------------------------------------------------

#: Each target scalar, the generated guard that owns its value domain, and a value
#: that guard refuses. The expectation is derived -- every case is asserted against
#: the guard first, so a widened value domain fails here rather than silently
#: turning a refusal case into an accepted one.
TARGET_SCALAR_CASES = (
    ("contract_version", is_contract_version, "1"),
    ("contract_version", is_contract_version, "1.3.0"),
    ("target_ref", is_identifier, "-leading-dash"),
    ("target_ref", is_identifier, ""),
    ("target_ref", is_identifier, "x" * 129),
    ("workspace_ref", is_workspace_id, "workspace 1"),
    ("workspace_ref", is_workspace_id, ""),
    ("workspace_ref", is_workspace_id, "x" * 129),
    # An `endpoint_profile_ref` is opaque: a dialable URI or a filesystem path in
    # that slot is the disclosure this descriptor exists to prevent, and the
    # `Identifier` domain is what refuses one.
    ("endpoint_profile_ref", is_identifier, "https://core.example.com/profile"),
    ("endpoint_profile_ref", is_identifier, "/var/lib/omnivia/profile.json"),
    ("endpoint_profile_ref", is_identifier, "profile local 1"),
)


@pytest.mark.parametrize(
    "field,guard,value", TARGET_SCALAR_CASES, ids=lambda case: str(case)[:40]
)
def test_a_target_scalar_outside_its_generated_value_domain_is_refused(
    field: str, guard: Any, value: str
) -> None:
    assert guard(value) is False, "the generated guard already accepts this value"
    document = {**_local_target_document(), field: value}
    assert not TARGET_VALIDATOR.is_valid(document)
    with pytest.raises(ContractSemanticError) as refusal:
        decode_core_target(document)
    if value:
        assert value not in str(refusal.value), "a refusal echoed the rejected value"
    with pytest.raises(ContractSemanticError):
        encode_core_target(replace(decode_core_target(_local_target_document()), **{field: value}))


@pytest.mark.parametrize("display_name", ["", "x" * 257])
def test_an_out_of_bound_display_name_is_refused(display_name: str) -> None:
    document = {**_local_target_document(), "display_name": display_name}
    assert not TARGET_VALIDATOR.is_valid(document)
    with pytest.raises(ContractSemanticError):
        decode_core_target(document)


@pytest.mark.parametrize("display_name", ["x", "x" * 256])
def test_a_display_name_at_either_bound_is_accepted(display_name: str) -> None:
    document = {**_local_target_document(), "display_name": display_name}
    assert TARGET_VALIDATOR.is_valid(document)
    validate_core_target(decode_core_target(document))


@pytest.mark.parametrize(
    "field,guard,value",
    [
        ("contract_version", is_contract_version, "1.3.0"),
        ("server_version", is_release_version, "1.2"),
        ("server_version", is_release_version, "not-a-version"),
        ("protocol_version", is_contract_version, "1.3.0"),
        ("protocol_version", is_contract_version, "v1.3"),
    ],
)
def test_a_status_scalar_outside_its_generated_value_domain_is_refused(
    field: str, guard: Any, value: str
) -> None:
    assert guard(value) is False, "the generated guard already accepts this value"
    document = _status_document(_local_target_document(), ["open"])
    document[field] = value
    assert not STATUS_VALIDATOR.is_valid(document)
    with pytest.raises(ContractSemanticError) as refusal:
        decode_core_safe_status(document)
    assert value not in str(refusal.value)


@pytest.mark.parametrize(
    "field,value",
    [("server_version", "1.2.5"), ("protocol_version", "1.3")],
)
def test_a_well_formed_optional_status_version_is_accepted(field: str, value: str) -> None:
    document = _status_document(_local_target_document(), ["open"])
    document[field] = value
    assert STATUS_VALIDATOR.is_valid(document)
    assert encode_core_safe_status(decode_core_safe_status(document)) == document


def test_a_status_published_at_a_different_version_than_its_target_is_refused() -> None:
    """The agreement clause: one publication, one contract version."""
    document = _status_document(_local_target_document(), ["open"])
    document["contract_version"] = "1.2"
    with pytest.raises(ContractSemanticError) as refusal:
        decode_core_safe_status(document)
    assert "1.2" not in str(refusal.value)
    assert "1.3" not in str(refusal.value)


def test_an_oversized_warning_code_list_is_refused_under_its_own_message() -> None:
    status = replace(
        decode_core_safe_status(_status_document(_local_target_document(), ["open"])),
        warning_codes=tuple(f"code-{index}" for index in range(33)),
    )
    with pytest.raises(ContractSemanticError) as refusal:
        validate_core_safe_status(status)
    assert "more than 32" in str(refusal.value)


def test_an_oversized_permitted_action_list_is_refused_under_its_own_message() -> None:
    status = replace(
        decode_core_safe_status(_status_document(_local_target_document(), ["open"])),
        permitted_actions=tuple(f"action-{index}" for index in range(17)),
    )
    with pytest.raises(ContractSemanticError) as refusal:
        validate_core_safe_status(status)
    assert "more than 16" in str(refusal.value)


# --------------------------------------------------------------------------
# Set-level authority: one writable workspace identity, one target authority
# --------------------------------------------------------------------------


def test_distinct_local_and_remote_target_authorities_are_accepted() -> None:
    validate_core_target_authorities(
        [
            decode_core_target(_local_target_document()),
            decode_core_target(_remote_target_document()),
        ]
    )


def test_an_empty_or_single_target_set_is_accepted() -> None:
    validate_core_target_authorities([])
    validate_core_target_authorities([decode_core_target(_local_target_document())])


def test_a_repeated_target_ref_is_refused() -> None:
    duplicate = {**_remote_target_document(), "target_ref": _local_target_document()["target_ref"]}
    with pytest.raises(ContractSemanticError) as refusal:
        validate_core_target_authorities(
            [decode_core_target(_local_target_document()), decode_core_target(duplicate)]
        )
    assert "target_ref" in str(refusal.value)
    assert duplicate["target_ref"] not in str(refusal.value)


def test_a_workspace_shared_by_a_local_and_a_remote_authority_is_refused() -> None:
    """The handoff rule: two authorities may not claim one writable workspace."""
    shared = {
        **_remote_target_document(),
        "workspace_ref": _local_target_document()["workspace_ref"],
    }
    local = decode_core_target(_local_target_document())
    remote = decode_core_target(shared)
    # Each descriptor is individually valid; only the set sees the collision.
    validate_core_target(local)
    validate_core_target(remote)
    with pytest.raises(ContractSemanticError) as refusal:
        validate_core_target_authorities([local, remote])
    assert "workspace_ref" in str(refusal.value)
    assert shared["workspace_ref"] not in str(refusal.value)


def test_an_individually_invalid_target_fails_the_set_closed() -> None:
    invalid = replace(decode_core_target(_remote_target_document()), kind="on_prem")
    with pytest.raises(ContractSemanticError):
        validate_core_target_authorities(
            [decode_core_target(_local_target_document()), invalid]
        )
    with pytest.raises(ContractSemanticError):
        validate_core_target_authorities(["not a target"])


# --------------------------------------------------------------------------
# Schema vocabulary is the authority for the Python semantic sets
# --------------------------------------------------------------------------

#: Each normalized status field and the closed `$defs` vocabulary that governs it.
STATE_FIELDS = (
    ("lifecycle_state", "CoreLifecycleState"),
    ("readiness_state", "CoreReadinessState"),
    ("compatibility_state", "CoreCompatibilityState"),
    ("connection_state", "CoreConnectionState"),
)


def _enum(def_name: str) -> list[str]:
    document = json.loads((SCHEMA_DIR / "service.schema.json").read_text(encoding="utf-8"))
    values: list[str] = document["$defs"][def_name]["enum"]
    return values


@pytest.mark.parametrize("field,vocabulary", STATE_FIELDS)
def test_every_declared_state_value_is_accepted_by_schema_and_semantics(
    field: str, vocabulary: str
) -> None:
    for value in _enum(vocabulary):
        document = _status_document(_local_target_document(), ["open"])
        document[field] = value
        assert STATUS_VALIDATOR.is_valid(document), f"schema rejected {vocabulary} {value!r}"
        assert getattr(decode_core_safe_status(document), field) == value


@pytest.mark.parametrize("field,vocabulary", STATE_FIELDS)
def test_a_value_outside_a_declared_state_vocabulary_is_refused_by_both(
    field: str, vocabulary: str
) -> None:
    document = _status_document(_local_target_document(), ["open"])
    document[field] = "not_a_declared_value"
    assert not STATUS_VALIDATOR.is_valid(document)
    with pytest.raises(ContractSemanticError):
        decode_core_safe_status(document)


def test_every_declared_warning_code_and_action_is_accepted() -> None:
    document = _status_document(_local_target_document(), _enum("CoreSafeAction"))
    document["warning_codes"] = _enum("CoreSafeWarningCode")
    assert STATUS_VALIDATOR.is_valid(document)
    assert encode_core_safe_status(decode_core_safe_status(document)) == document


# --------------------------------------------------------------------------
# Real failure and normalized authentication-required connection state
# --------------------------------------------------------------------------


def test_a_failed_lifecycle_state_is_publishable_and_distinct_from_stopped() -> None:
    assert "failed" in _enum("CoreLifecycleState")
    document = _status_document(_local_target_document(), ["start", "open"])
    document["lifecycle_state"] = "failed"
    document["readiness_state"] = "not_ready"
    assert STATUS_VALIDATOR.is_valid(document)
    status = decode_core_safe_status(document)
    assert status.lifecycle_state == "failed"
    assert "stopped" in _enum("CoreLifecycleState")


def test_authentication_required_is_both_a_connection_state_and_a_warning_code() -> None:
    assert "authentication_required" in _enum("CoreConnectionState")
    assert "authentication_required" in _enum("CoreSafeWarningCode")
    document = _status_document(_local_target_document(), ["open"])
    document["connection_state"] = "authentication_required"
    document["warning_codes"] = ["authentication_required"]
    assert STATUS_VALIDATOR.is_valid(document)
    status = decode_core_safe_status(document)
    assert status.connection_state == "authentication_required"
    assert status.warning_codes == ("authentication_required",)


def test_the_connection_state_and_the_warning_code_stay_independent() -> None:
    """Neither implies the other: the state is normalized, the code is the advisory."""
    state_only = _status_document(_local_target_document(), ["open"])
    state_only["connection_state"] = "authentication_required"
    code_only = _status_document(_local_target_document(), ["open"])
    code_only["warning_codes"] = ["authentication_required"]
    for document in (state_only, code_only):
        assert STATUS_VALIDATOR.is_valid(document)
        validate_core_safe_status(decode_core_safe_status(document))


# --------------------------------------------------------------------------
# Cross-language corpus: Python and the generated TypeScript agree
# --------------------------------------------------------------------------


def _rejected(case_id: str, sentinel: str, **overrides: Any) -> dict[str, Any]:
    """One refusal case: `sentinel` is a value from the status no error may echo.

    For an undeclared value that is the offending value itself. For the local-action
    rule the offending values are declared vocabulary members that the rule's own
    sentence has to name, so the sentinel is the target reference instead -- the
    caller-identifying value a refusal published pre-authentication must not carry.
    """
    document = _status_document(_local_target_document(), ["open"])
    document.update(overrides)
    return {"id": case_id, "sentinel": sentinel, "status": document}


def _semantic_corpus() -> dict[str, list[dict[str, Any]]]:
    """The corpus both bindings are held to, built once so neither gets its own cases."""
    failed_local = _status_document(_local_target_document(), ["start", "stop", "restart", "open"])
    failed_local["lifecycle_state"] = "failed"
    failed_local["readiness_state"] = "not_ready"

    authenticating_remote = _status_document(_remote_target_document(), ["reconnect", "open"])
    authenticating_remote["connection_state"] = "authentication_required"
    authenticating_remote["warning_codes"] = ["authentication_required"]

    externally_managed_local = dict(_local_target_document())
    externally_managed_local["management"] = "externally_managed"

    return {
        "accepted": [
            {
                "id": "local-locally-managed-offers-lifecycle-actions",
                "status": _status_document(
                    _local_target_document(), ["start", "stop", "restart", "reconnect", "open"]
                ),
            },
            {
                "id": "remote-offers-only-transport-actions",
                "status": _status_document(_remote_target_document(), ["reconnect", "open"]),
            },
            {"id": "local-failed-lifecycle", "status": failed_local},
            {"id": "remote-authentication-required", "status": authenticating_remote},
            {
                "id": "cloud-with-no-permitted-actions",
                "status": _status_document(
                    {**_remote_target_document(), "kind": "cloud"}, []
                ),
            },
        ],
        "rejected": [
            _rejected(
                "unknown-kind",
                "on_prem",
                target={**_local_target_document(), "kind": "on_prem"},
            ),
            _rejected(
                "unknown-management",
                "vendor_managed",
                target={**_local_target_document(), "management": "vendor_managed"},
            ),
            _rejected("unknown-lifecycle-state", "combusting", lifecycle_state="combusting"),
            _rejected("unknown-readiness-state", "maybe_ready", readiness_state="maybe_ready"),
            _rejected(
                "unknown-compatibility-state", "probably_fine", compatibility_state="probably_fine"
            ),
            _rejected("unknown-connection-state", "half_open", connection_state="half_open"),
            _rejected("unknown-warning-code", "disk_on_fire", warning_codes=["disk_on_fire"]),
            _rejected("unknown-action", "delete_everything", permitted_actions=["delete_everything"]),
            _rejected(
                "duplicate-warning-code", "degraded", warning_codes=["degraded", "degraded"]
            ),
            _rejected("duplicate-action", "open", permitted_actions=["open", "open"]),
            _rejected(
                "remote-target-offers-start",
                _remote_target_document()["target_ref"],
                target=_remote_target_document(),
                permitted_actions=["start", "open"],
            ),
            _rejected(
                "cloud-target-offers-stop",
                _remote_target_document()["target_ref"],
                target={**_remote_target_document(), "kind": "cloud"},
                permitted_actions=["stop", "open"],
            ),
            _rejected(
                "externally-managed-local-target-offers-restart",
                externally_managed_local["target_ref"],
                target=externally_managed_local,
                permitted_actions=["restart", "open"],
            ),
            _rejected(
                "malformed-target-contract-version",
                "1.3.0",
                target={**_local_target_document(), "contract_version": "1.3.0"},
            ),
            _rejected(
                "malformed-target-ref",
                "-leading-dash",
                target={**_local_target_document(), "target_ref": "-leading-dash"},
            ),
            _rejected(
                "malformed-workspace-ref",
                "workspace 1",
                target={**_local_target_document(), "workspace_ref": "workspace 1"},
            ),
            _rejected(
                "endpoint-profile-ref-is-a-url",
                "https://core.example.com/profile",
                target={
                    **_local_target_document(),
                    "endpoint_profile_ref": "https://core.example.com/profile",
                },
            ),
            _rejected(
                "endpoint-profile-ref-is-a-path",
                "/var/lib/omnivia/profile.json",
                target={
                    **_local_target_document(),
                    "endpoint_profile_ref": "/var/lib/omnivia/profile.json",
                },
            ),
            _rejected(
                "overlong-display-name",
                "x" * 257,
                target={**_local_target_document(), "display_name": "x" * 257},
            ),
            _rejected(
                "empty-display-name",
                "the-empty-display-name-has-no-sentinel-to-echo",
                target={**_local_target_document(), "display_name": ""},
            ),
            _rejected("malformed-status-contract-version", "1.3.0", contract_version="1.3.0"),
            _rejected("malformed-server-version", "1.2", server_version="1.2"),
            _rejected("malformed-protocol-version", "v1.3", protocol_version="v1.3"),
            # The version-agreement clause: the status names a version the target
            # does not, so the sentinel is the disagreeing value itself.
            _rejected("status-target-version-disagreement", "1.2", contract_version="1.2"),
            _rejected(
                "oversized-warning-codes",
                "code-32",
                warning_codes=[f"code-{index}" for index in range(33)],
            ),
            _rejected(
                "oversized-permitted-actions",
                "action-16",
                permitted_actions=[f"action-{index}" for index in range(17)],
            ),
        ],
    }


#: Target *sets* both bindings are held to, for the invariant no single descriptor
#: can enforce. Same shape as the status corpus: accepted, then refused with a
#: sentinel no error may echo.
def _authority_corpus() -> dict[str, list[dict[str, Any]]]:
    local = _local_target_document()
    remote = _remote_target_document()
    return {
        "accepted": [
            {"id": "no-targets", "targets": []},
            {"id": "one-target", "targets": [local]},
            {"id": "distinct-local-and-remote-authorities", "targets": [local, remote]},
            {
                "id": "same-workspace-name-shape-but-distinct-identities",
                "targets": [local, {**remote, "workspace_ref": "workspace-3"}],
            },
        ],
        "rejected": [
            {
                "id": "duplicate-target-ref",
                "sentinel": local["target_ref"],
                "targets": [local, {**remote, "target_ref": local["target_ref"]}],
            },
            {
                "id": "duplicate-workspace-ref-across-local-and-remote",
                "sentinel": local["workspace_ref"],
                "targets": [local, {**remote, "workspace_ref": local["workspace_ref"]}],
            },
            {
                "id": "an-invalid-target-fails-the-whole-set",
                "sentinel": "on_prem",
                "targets": [local, {**remote, "kind": "on_prem"}],
            },
        ],
    }


CORPUS = _semantic_corpus()
AUTHORITY_CORPUS = _authority_corpus()


def test_the_cross_language_corpus_has_unique_case_ids() -> None:
    ids = [
        case["id"]
        for corpus in (CORPUS, AUTHORITY_CORPUS)
        for case in [*corpus["accepted"], *corpus["rejected"]]
    ]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "case", AUTHORITY_CORPUS["accepted"], ids=lambda case: case["id"]
)
def test_python_accepts_every_corpus_target_set_the_typescript_binding_accepts(
    case: dict[str, Any],
) -> None:
    validate_core_target_authorities(
        [CoreTargetV1.from_wire(document) for document in case["targets"]]
    )


@pytest.mark.parametrize(
    "case", AUTHORITY_CORPUS["rejected"], ids=lambda case: case["id"]
)
def test_python_refuses_every_corpus_target_set_the_typescript_binding_refuses(
    case: dict[str, Any],
) -> None:
    with pytest.raises(ContractSemanticError) as refusal:
        validate_core_target_authorities(
            [CoreTargetV1.from_wire(document) for document in case["targets"]]
        )
    assert case["sentinel"] not in str(refusal.value), "a refusal echoed the rejected value"


@pytest.mark.parametrize("case", CORPUS["accepted"], ids=lambda case: case["id"])
def test_python_accepts_every_corpus_status_the_typescript_binding_accepts(
    case: dict[str, Any],
) -> None:
    assert STATUS_VALIDATOR.is_valid(case["status"])
    validate_core_safe_status(decode_core_safe_status(case["status"]))


@pytest.mark.parametrize("case", CORPUS["rejected"], ids=lambda case: case["id"])
def test_python_refuses_every_corpus_status_the_typescript_binding_refuses(
    case: dict[str, Any],
) -> None:
    with pytest.raises(ContractSemanticError) as refusal:
        decode_core_safe_status(case["status"])
    assert case["sentinel"] not in str(refusal.value), "a refusal echoed the rejected value"


# --------------------------------------------------------------------------
# Executable TypeScript parity
# --------------------------------------------------------------------------


def _compile_generated_typescript(tmp_path: Path) -> tuple[str, Path]:
    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    node = shutil.which("node")
    assert tsc.is_file(), "run npm ci so the pinned TypeScript compiler is available"
    assert node is not None, "node is required for the executable TypeScript parity gate"
    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--skipLibCheck",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--outDir",
            str(tmp_path),
            str(TYPESCRIPT_PATH),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return node, tmp_path / "index.js"


def test_generated_typescript_exports_the_core_target_semantic_surface() -> None:
    typescript = TYPESCRIPT_PATH.read_text(encoding="utf-8")
    for export in (
        "export function isCoreTargetKind(",
        "export function isCoreTargetManagement(",
        "export function isCoreLifecycleState(",
        "export function isCoreReadinessState(",
        "export function isCoreCompatibilityState(",
        "export function isCoreConnectionState(",
        "export function isCoreSafeWarningCode(",
        "export function isCoreSafeAction(",
        "export function assertCoreTargetV1Semantics(",
        "export function isCoreTargetV1SemanticallyValid(",
        "export function assertCoreSafeStatusV1Semantics(",
        "export function isCoreSafeStatusV1SemanticallyValid(",
        "export function assertCoreTargetV1Authorities(",
        "export function areCoreTargetV1AuthoritiesValid(",
        "export const CORE_LOCAL_ONLY_ACTIONS = ",
    ):
        assert export in typescript, f"the generator stopped emitting `{export}`"
    # The aliases stay tolerant strings: a decoder must still preserve a value it
    # does not recognize, and refusing one is the guard's job, not the type's.
    assert "export type CoreLifecycleState = string;" in typescript
    assert "export type CoreSafeAction = string;" in typescript


def test_generated_typescript_returns_the_same_verdict_as_python(tmp_path: Path) -> None:
    node, compiled = _compile_generated_typescript(tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps({**CORPUS, "authorities": AUTHORITY_CORPUS}), encoding="utf-8"
    )
    vocabularies = {
        name: _enum(name)
        for name in (
            "CoreTargetKind",
            "CoreTargetManagement",
            "CoreLifecycleState",
            "CoreReadinessState",
            "CoreCompatibilityState",
            "CoreConnectionState",
            "CoreSafeWarningCode",
            "CoreSafeAction",
        )
    }
    vocabulary_path = tmp_path / "vocabularies.json"
    vocabulary_path.write_text(json.dumps(vocabularies), encoding="utf-8")

    runtime_check = """
const fs = require("node:fs");
const contracts = require(process.argv[1]);
const corpus = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const vocabularies = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const fail = (message) => {
  console.error(message);
  process.exit(1);
};

for (const [name, values] of Object.entries(vocabularies)) {
  const guard = contracts["is" + name];
  if (typeof guard !== "function") fail("missing guard for " + name);
  for (const value of values) {
    if (!guard(value)) fail(name + " guard refused a declared value");
  }
  if (guard("not_a_declared_value")) fail(name + " guard accepted an undeclared value");
  if (guard(7) || guard(null) || guard(undefined)) fail(name + " guard accepted a non-string");
  const emitted = contracts[name.replace(/(?<!^)([A-Z])/g, "_$1").toUpperCase() + "_VALUES"];
  if (JSON.stringify([...emitted]) !== JSON.stringify(values)) {
    fail(name + " emitted values differ from the schema enum");
  }
}

for (const testCase of corpus.accepted) {
  if (!contracts.isCoreSafeStatusV1SemanticallyValid(testCase.status)) {
    fail("TypeScript refused an accepted case: " + testCase.id);
  }
  contracts.assertCoreSafeStatusV1Semantics(testCase.status);
  contracts.assertCoreTargetV1Semantics(testCase.status.target);
}

for (const testCase of corpus.rejected) {
  if (contracts.isCoreSafeStatusV1SemanticallyValid(testCase.status)) {
    fail("TypeScript accepted a rejected case: " + testCase.id);
  }
  let thrown = null;
  try {
    contracts.assertCoreSafeStatusV1Semantics(testCase.status);
  } catch (error) {
    thrown = error;
  }
  if (thrown === null) fail("assertion did not throw for: " + testCase.id);
  if (!(thrown instanceof TypeError)) fail("assertion threw a non-TypeError: " + testCase.id);
  if (String(thrown.message).includes(testCase.sentinel)) {
    fail("a refusal echoed the rejected value: " + testCase.id);
  }
}

for (const testCase of corpus.authorities.accepted) {
  if (!contracts.areCoreTargetV1AuthoritiesValid(testCase.targets)) {
    fail("TypeScript refused an accepted target set: " + testCase.id);
  }
  contracts.assertCoreTargetV1Authorities(testCase.targets);
}

for (const testCase of corpus.authorities.rejected) {
  if (contracts.areCoreTargetV1AuthoritiesValid(testCase.targets)) {
    fail("TypeScript accepted a rejected target set: " + testCase.id);
  }
  let thrown = null;
  try {
    contracts.assertCoreTargetV1Authorities(testCase.targets);
  } catch (error) {
    thrown = error;
  }
  if (thrown === null) fail("authority assertion did not throw for: " + testCase.id);
  if (!(thrown instanceof TypeError)) {
    fail("authority assertion threw a non-TypeError: " + testCase.id);
  }
  if (String(thrown.message).includes(testCase.sentinel)) {
    fail("an authority refusal echoed the rejected value: " + testCase.id);
  }
}
"""
    subprocess.run(
        [node, "-e", runtime_check, str(compiled), str(corpus_path), str(vocabulary_path)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_core_semantic_assertions_require_a_typed_dto(tmp_path: Path) -> None:
    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    assert tsc.is_file(), "run npm ci so the pinned TypeScript compiler is available"
    type_gate = tmp_path / "consumer.ts"
    module_specifier = json.dumps(str(TYPESCRIPT_PATH))
    type_gate.write_text(
        "\n".join(
            [
                "import {",
                "  assertCoreSafeStatusV1Semantics,",
                "  assertCoreTargetV1Semantics,",
                f"}} from {module_specifier};",
                "// @ts-expect-error semantic validation requires a structurally decoded target",
                'assertCoreTargetV1Semantics({ kind: "local" });',
                "// @ts-expect-error semantic validation requires a structurally decoded status",
                "assertCoreSafeStatusV1Semantics({});",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(tsc),
            "--strict",
            "--noEmit",
            "--skipLibCheck",
            # The consumer imports the generated source by absolute path, so the
            # specifier ends in `.ts`; without this tsc stops at TS5097 before it
            # ever evaluates the `@ts-expect-error` directives above.
            "--allowImportingTsExtensions",
            "--target",
            "ES2022",
            "--moduleResolution",
            "Bundler",
            "--module",
            "ESNext",
            str(type_gate),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
