"""Reference provenance and non-disclosure for Host Contract v1 bindings.

An ``EnvironmentBinding`` carries logical references only. Proving that needs
two things the lexical shape of a string cannot supply:

- **provenance** -- every ``credentialRef``, ``integrationRef``, ``storageRef``
  and ``domainRef`` must resolve, in a trusted resolver the caller supplies, to
  an approved entry of the right kind, scoped to the binding's own Workspace,
  verified and not revoked. A token-shaped string is identifier-shaped, so
  shape alone admits exactly the thing the rule exists to keep out;
- **non-disclosure** -- a rejected reference may *be* the credential. No
  diagnostic, exception message, argument, attribute or traceback may reproduce
  it or any prefix of it. Refusals carry a fixed code plus the field name and
  index, and nothing else.

The governed wire shape is untouched: this is validation over the approved
record, not a change to it.
"""

from __future__ import annotations

import dataclasses
import json
import traceback
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import publication as pub

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"

#: Values that would be a catastrophe to echo. Each is identifier-shaped where
#: the contract's own pattern allows it, so none of them is kept out by shape.
SECRETS = (
    "sk_live_TOP_SECRET_ABC123",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
)


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def binding(**overrides: Any) -> gen.EnvironmentBinding:
    document = json.loads(json.dumps(load("valid/environment-binding.json")))
    document.update(overrides)
    return gen.EnvironmentBinding.from_wire(document)


def approved(reference_id: str, kind: str, **overrides: Any) -> pub.ApprovedReference:
    entry = pub.ApprovedReference(
        reference_id=reference_id, kind=kind, workspace_id="workspace-acme"
    )
    return dataclasses.replace(entry, **overrides)


def registry(*references: pub.ApprovedReference) -> pub.ReferenceRegistry:
    """Return the canonical fixture's resolver, with ``references`` substituted by ID."""
    base = {
        "credential.documents.storage": approved("credential.documents.storage", "credential"),
        "integration.documents.index": approved("integration.documents.index", "integration"),
        "storage.documents.primary": approved("storage.documents.primary", "storage"),
        "domain.documents.example": approved("domain.documents.example", "domain"),
        "policy.residency.au": approved(
            "policy.residency.au", "data_residency_policy"
        ),
        "limits.standard": approved("limits.standard", "runtime_limits"),
        "placement.eu-west": approved(
            "placement.eu-west", "execution_placement"
        ),
    }
    for reference in references:
        base[reference.reference_id] = reference
    return pub.ReferenceRegistry(references=tuple(base.values()))


TRUSTED = registry()


# --------------------------------------------------------------------------
# The resolver is mandatory, and the resolved case still passes
# --------------------------------------------------------------------------


def test_a_fully_resolved_binding_is_valid() -> None:
    pub.validate_environment_binding(binding(), TRUSTED)


def test_validation_has_no_resolver_free_form() -> None:
    """There is no call that decides a binding without a resolver to decide against."""
    with pytest.raises(TypeError):
        pub.validate_environment_binding(binding())  # type: ignore[call-arg]


def test_a_resolver_of_the_wrong_type_fails_closed() -> None:
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == "malformed_reference_registry"


def test_a_resolver_holding_a_repeated_entry_fails_closed() -> None:
    hostile = pub.ReferenceRegistry(
        references=(
            approved("credential.documents.storage", "credential"),
            approved("credential.documents.storage", "storage"),
        )
    )
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(), hostile)
    assert excinfo.value.code == "malformed_reference_registry"


def test_a_resolver_entry_of_an_ungoverned_kind_fails_closed() -> None:
    hostile = pub.ReferenceRegistry(references=(approved("credential.a", "sorcery"),))
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(), hostile)
    assert excinfo.value.code == "malformed_reference_registry"


# --------------------------------------------------------------------------
# Every way a reference can fail to be one
# --------------------------------------------------------------------------


def test_a_reference_no_resolver_knows_is_refused() -> None:
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(credentialRefs=["credential.unknown"]), TRUSTED)
    assert excinfo.value.code == "unresolved_reference"
    assert excinfo.value.field == "credentialRefs"
    assert excinfo.value.index == 0


def test_a_reference_of_the_wrong_kind_is_refused() -> None:
    """A storage reference in a credential field is not a credential reference,
    however well formed it is.
    """
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(
            binding(credentialRefs=["storage.documents.primary"]), TRUSTED
        )
    assert excinfo.value.code == "wrong_reference_kind"


def test_a_reference_owned_by_another_workspace_is_refused() -> None:
    other = registry(
        approved("credential.documents.storage", "credential", workspace_id="workspace-other")
    )
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(), other)
    assert excinfo.value.code == "cross_workspace_reference"


def test_an_unverified_reference_is_refused() -> None:
    unverified = registry(
        approved("credential.documents.storage", "credential", verified=False)
    )
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(), unverified)
    assert excinfo.value.code == "unverified_reference"


def test_a_revoked_reference_is_refused() -> None:
    revoked = registry(approved("credential.documents.storage", "credential", revoked=True))
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(), revoked)
    assert excinfo.value.code == "revoked_reference"


def test_a_repeated_reference_is_refused_by_exact_record_validation() -> None:
    secret = "ghp_" + "r" * 36
    hostile = gen.EnvironmentBinding(
        binding_id="binding-production-1",
        binding_version=1,
        release_id="release-documents-1",
        environment_id="environment-production",
        workspace_id="workspace-acme",
        target="cloud",
        credential_refs=(secret, secret),
        integration_refs=("integration.documents.index",),
        storage_refs=("storage.documents.primary",),
        domain_refs=("domain.documents.example",),
        created_at="2026-08-03T00:00:00Z",
        actor_ref="principal-deployer",
    )
    with pytest.raises(gen.HostContractDecodeError) as excinfo:
        pub.validate_environment_binding(hostile, TRUSTED)
    assert str(excinfo.value) == "EnvironmentBinding.credentialRefs[1]: duplicate value"
    assert disclosure(excinfo.value, secret) == []


def test_every_governed_reference_field_is_resolved_not_just_credentials() -> None:
    for field_name in pub.BINDING_REFERENCE_FIELDS:
        hostile: str | list[str]
        hostile = "nobody.knows" if field_name.endswith("Ref") else ["nobody.knows"]
        with pytest.raises(pub.BindingReferenceError) as excinfo:
            pub.validate_environment_binding(binding(**{field_name: hostile}), TRUSTED)
        assert excinfo.value.field == field_name, field_name
        assert excinfo.value.code == "unresolved_reference", field_name


def test_the_binding_reference_kinds_cover_exactly_the_governed_fields() -> None:
    assert tuple(pub.BINDING_REFERENCE_KINDS) == pub.BINDING_REFERENCE_FIELDS
    assert set(pub.BINDING_REFERENCE_KINDS.values()) == set(pub.REFERENCE_KINDS)


def test_the_error_codes_are_a_fixed_declared_set() -> None:
    assert pub.BINDING_REFERENCE_ERROR_CODES == (
        "malformed_reference_registry",
        "unresolved_reference",
        "wrong_reference_kind",
        "cross_workspace_reference",
        "unverified_reference",
        "revoked_reference",
        "duplicate_reference",
    )


# --------------------------------------------------------------------------
# Non-disclosure: the rejected value never leaves the validator
# --------------------------------------------------------------------------


def disclosure(error: BaseException, secret: str) -> list[str]:
    """Return every rendering of ``error`` that reproduces ``secret`` or a prefix."""
    renderings = {
        "str": str(error),
        "repr": repr(error),
        "args": repr(error.args),
        "traceback": "".join(traceback.format_exception(error)),
    }
    # Six characters is already a usable prefix of a `sk_live_`-style token.
    probes = {secret, secret[:8], secret[:6]}
    return sorted(
        name
        for name, text in renderings.items()
        if any(probe and probe in text for probe in probes)
    )


@pytest.mark.parametrize("secret", SECRETS)
def test_a_token_shaped_reference_is_refused_and_never_echoed(secret: str) -> None:
    """The exact false pass the independent review executed: a token that is
    identifier-shaped, and so passes any lexical test.
    """
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(credentialRefs=[secret]), TRUSTED)
    assert excinfo.value.code == "unresolved_reference"
    assert disclosure(excinfo.value, secret) == []
    assert str(excinfo.value) == "credentialRefs[0]: unresolved_reference"


def test_a_private_key_block_is_refused_without_echoing_it() -> None:
    """A PEM block is refused by the contract record itself, one layer earlier.
    That refusal must be silent about the key too.
    """
    secret = "-----BEGIN PRIVATE KEY-----\nSUPER_SECRET_MATERIAL\n-----END PRIVATE KEY-----"
    document = json.loads(json.dumps(load("valid/environment-binding.json")))
    document["credentialRefs"] = [secret]
    with pytest.raises(gen.HostContractDecodeError) as excinfo:
        gen.EnvironmentBinding.from_wire(document)
    assert disclosure(excinfo.value, secret) == []
    assert "BEGIN" not in str(excinfo.value)


def test_no_rejected_reference_value_reaches_any_diagnostic() -> None:
    """Sweep every refusal path with a secret in it, not only the unresolved one."""
    secret = "sk_live_TOP_SECRET_ABC123"
    resolvers = {
        "wrong_reference_kind": registry(approved(secret, "storage")),
        "cross_workspace_reference": registry(
            approved(secret, "credential", workspace_id="workspace-other")
        ),
        "unverified_reference": registry(approved(secret, "credential", verified=False)),
        "revoked_reference": registry(approved(secret, "credential", revoked=True)),
    }
    for expected, resolver in resolvers.items():
        with pytest.raises(pub.BindingReferenceError) as excinfo:
            pub.validate_environment_binding(binding(credentialRefs=[secret]), resolver)
        assert excinfo.value.code == expected
        assert disclosure(excinfo.value, secret) == [], expected


def test_the_validator_exposes_no_value_echoing_helper() -> None:
    """The removed ``_elide`` helper truncated a rejected value into the message,
    which is how a private-key prefix ended up in a diagnostic.
    """
    assert not hasattr(pub, "_elide")
