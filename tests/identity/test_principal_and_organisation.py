"""Tests for the local principal, the optional account session, and Organisation.

Three things are being pinned here, in order of how much they matter:

1. The account session never becomes a ``ShellContext`` field. What it produces
   is an :class:`IdentityProjection` carrying exactly ``principalRef`` and
   ``entitlementSummaryRef`` -- both already governed rotation triggers -- and
   nothing else. A test that only checked "sign-in works" would pass just as
   happily if the seam had grown a third field.
2. Losing an account session falls back to local authority rather than leaving
   an installation holding entitlements it can no longer prove.
3. Organisation has a lifecycle and no switch verb, and there is a test that
   fails if a switch verb appears.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.identity import lifecycle, models

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)
LATER = NOW + timedelta(hours=1)


def instant(moment: datetime) -> str:
    return moment.isoformat()


def local_principal() -> models.LocalPrincipal:
    return models.LocalPrincipal(
        principal_ref="principal-local-001",
        entitlement_summary_ref="entitlements-local-001",
        display_name="This installation",
        created_at=instant(EARLIER),
    )


def account_session(**overrides: object) -> models.AccountSession:
    values: dict[str, object] = {
        "session_id": "session-001",
        "account_ref": "account-001",
        "local_principal_ref": "principal-local-001",
        "principal_ref": "principal-account-001",
        "entitlement_summary_ref": "entitlements-account-001",
        "issued_at": instant(EARLIER),
    }
    values.update(overrides)
    return models.AccountSession(**values)  # type: ignore[arg-type]


def organisation(**overrides: object) -> models.Organisation:
    values: dict[str, object] = {
        "organisation_ref": "org-001",
        "display_name": "Example Org",
        "created_at": instant(EARLIER),
    }
    values.update(overrides)
    return models.Organisation(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The reference shape Core mints against
# --------------------------------------------------------------------------


def test_identity_ref_pattern_is_the_host_contract_identifier_pattern() -> None:
    """The two spellings must stay equal, or Core could mint a principal or
    entitlement reference the frozen ``ShellContext`` would then refuse."""
    assert models.IDENTITY_REF_PATTERN == gen.IDENTIFIER_PATTERN


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        "-leading-hyphen",
        "has space",
        "has/slash",
        "x" * 129,
    ],
)
def test_a_reference_the_host_contract_would_refuse_is_refused_here(bad_ref: str) -> None:
    with pytest.raises(models.IdentityError):
        replace(local_principal(), principal_ref=bad_ref)


def test_a_record_cannot_be_constructed_in_an_invalid_state() -> None:
    """Validation is on construction, not on demand: there is no window in which
    an invalid authority record exists and could be read as valid."""
    with pytest.raises(models.IdentityError):
        models.LocalPrincipal(
            principal_ref="principal-local-001",
            entitlement_summary_ref="entitlements-local-001",
            display_name="   ",
            created_at=instant(EARLIER),
        )


@pytest.mark.parametrize(
    "created_at",
    ["2026-08-04T12:00:00", "not-a-timestamp", "2026-08-04"],
)
def test_a_timestamp_without_a_time_zone_is_refused(created_at: str) -> None:
    with pytest.raises(models.IdentityError):
        replace(local_principal(), created_at=created_at)


def test_a_session_cannot_expire_before_it_was_issued() -> None:
    with pytest.raises(models.IdentityError):
        account_session(expires_at=instant(EARLIER - timedelta(minutes=1)))


# --------------------------------------------------------------------------
# The projection seam: two fields, and only two
# --------------------------------------------------------------------------


def test_the_projection_carries_exactly_the_two_existing_authority_fields() -> None:
    """The whole point of the seam. ``IdentityProjection`` may name only fields
    ``ShellContext`` already declares *and* that the codec already treats as
    rotation triggers -- a third field would be the first step toward adding one
    to the byte-frozen record."""
    from omnivia_core.host_contract.v1 import codec

    projected = tuple(field.name for field in fields(models.IdentityProjection))
    assert projected == ("principal_ref", "entitlement_summary_ref")

    # Every projected name is a field ShellContext already declares...
    context_fields = {field.name for field in fields(gen.ShellContext)}
    assert set(projected) <= context_fields

    # ...and every one of them is a governed rotation trigger, so applying a
    # projection rotates through the frozen rule rather than a Core-invented one.
    snake = {
        "principalRef": "principal_ref",
        "workspaceRef": "workspace_ref",
        "entitlementSummaryRef": "entitlement_summary_ref",
        "permissionSummaryRef": "permission_summary_ref",
        "runtimeId": "runtime_id",
        "environmentId": "environment_id",
    }
    triggers = {snake[name] for name in codec.CONTEXT_AUTHORITY_FIELDS}
    assert set(projected) <= triggers


def test_shell_context_gained_no_account_session_field() -> None:
    """The account session lives outside the contract record. If a future change
    smuggled it in, these names would appear here."""
    context_fields = {field.name for field in fields(gen.ShellContext)}
    session_fields = {field.name for field in fields(models.AccountSession)}
    assert context_fields & session_fields == {
        "principal_ref",
        "entitlement_summary_ref",
        "issued_at",
        "expires_at",
    }
    assert "session_id" not in context_fields
    assert "account_ref" not in context_fields
    assert "state" not in context_fields


def test_no_session_projects_the_local_principal() -> None:
    principal = local_principal()
    assert lifecycle.project_identity(principal, now=NOW) == models.IdentityProjection(
        principal_ref="principal-local-001",
        entitlement_summary_ref="entitlements-local-001",
    )


def test_an_active_session_projects_account_authority() -> None:
    assert lifecycle.project_identity(
        local_principal(), account_session(), now=NOW
    ) == models.IdentityProjection(
        principal_ref="principal-account-001",
        entitlement_summary_ref="entitlements-account-001",
    )


@pytest.mark.parametrize(
    "session",
    [
        account_session(state=models.AccountSessionState.SIGNED_OUT),
        account_session(state=models.AccountSessionState.EXPIRED),
        account_session(expires_at=instant(NOW)),  # the boundary instant itself
        account_session(expires_at=instant(NOW - timedelta(seconds=1))),
    ],
    ids=["signed-out", "expired-state", "expiry-boundary", "expiry-past"],
)
def test_a_finished_session_falls_back_to_local_authority(
    session: models.AccountSession,
) -> None:
    """Fail-safe direction: an installation that can no longer prove account
    entitlements drops to the local ones rather than keeping them."""
    assert lifecycle.project_identity(local_principal(), session, now=NOW) == (
        models.IdentityProjection(
            principal_ref="principal-local-001",
            entitlement_summary_ref="entitlements-local-001",
        )
    )


def test_a_session_bound_to_another_principal_is_refused() -> None:
    other = account_session(local_principal_ref="principal-local-002")
    with pytest.raises(models.IdentityError, match="not bound"):
        lifecycle.project_identity(local_principal(), other, now=NOW)
    with pytest.raises(models.IdentityError, match="not bound"):
        lifecycle.bind_session(local_principal(), other)


def test_projection_requires_a_time_zone_aware_instant() -> None:
    with pytest.raises(models.IdentityError, match="time-zone-aware"):
        lifecycle.project_identity(
            local_principal(), account_session(), now=NOW.replace(tzinfo=None)
        )


def test_an_unexpiring_session_stays_active_indefinitely() -> None:
    session = account_session(expires_at=None)
    assert lifecycle.is_session_active(session, now=LATER) is True


def test_a_session_is_active_until_its_own_expiry() -> None:
    session = account_session(expires_at=instant(LATER))
    assert lifecycle.is_session_active(session, now=NOW) is True
    assert lifecycle.is_session_active(session, now=LATER) is False


# --------------------------------------------------------------------------
# Session transitions
# --------------------------------------------------------------------------


def test_sign_out_and_expiry_are_terminal() -> None:
    signed_out = lifecycle.sign_out(account_session())
    assert signed_out.state is models.AccountSessionState.SIGNED_OUT
    with pytest.raises(models.IdentityError):
        lifecycle.sign_out(signed_out)
    with pytest.raises(models.IdentityError):
        lifecycle.expire_session(signed_out)

    expired = lifecycle.expire_session(account_session())
    assert expired.state is models.AccountSessionState.EXPIRED
    with pytest.raises(models.IdentityError):
        lifecycle.sign_out(expired)


def test_a_transition_returns_a_new_record_and_leaves_the_original_alone() -> None:
    original = account_session()
    assert lifecycle.sign_out(original) is not original
    assert original.state is models.AccountSessionState.ACTIVE


def test_session_transition_table_covers_every_state() -> None:
    assert set(lifecycle.SESSION_TRANSITIONS) == set(models.AccountSessionState)


# --------------------------------------------------------------------------
# Organisation: lifecycle only, and provably no switch
# --------------------------------------------------------------------------


def test_organisation_suspension_is_reversible_and_archival_is_not() -> None:
    active = organisation()
    suspended = lifecycle.suspend(active)
    assert suspended.state is models.OrganisationState.SUSPENDED
    assert lifecycle.restore(suspended).state is models.OrganisationState.ACTIVE

    archived = lifecycle.archive(suspended)
    assert archived.state is models.OrganisationState.ARCHIVED
    for verb in (lifecycle.suspend, lifecycle.restore, lifecycle.archive):
        with pytest.raises(models.IdentityError):
            verb(archived)


def test_organisation_transition_table_covers_every_state() -> None:
    assert set(lifecycle.ORGANISATION_TRANSITIONS) == set(models.OrganisationState)


def test_organisation_ref_is_not_a_governed_rotation_trigger() -> None:
    """The reason decision D2 defers Organisation switching, asserted rather
    than described: the contract grants an Organisation change no invalidation
    semantics, so Core has nothing lawful to build a switch on."""
    from omnivia_core.host_contract.v1 import codec

    # It is a real ShellContext wire field -- it is simply not a rotation
    # trigger, which is the whole distinction the deferral rests on.
    assert "organisationRef" in gen.ShellContext.WIRE_FIELDS
    assert "organisationRef" not in codec.CONTEXT_AUTHORITY_FIELDS


def test_the_identity_surface_exposes_no_organisation_switch_verb() -> None:
    """A guard against the deferred capability appearing by accident. If an
    Organisation switch is ever designed, its invalidation semantics land in the
    contract first and this test is updated deliberately."""
    import omnivia_core.identity as identity_package

    exported = set(identity_package.__all__) | set(lifecycle.__all__)
    offending = {
        name
        for name in exported
        if "switch" in name.lower() or "activate" in name.lower()
    }
    assert offending == set()
