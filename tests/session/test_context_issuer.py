"""Tests for the ShellContext issuer.

The issuer is a *holder*, not a second rulebook, so these tests are mostly about
proving two things:

- what it does is decided by the frozen codec (``validate_context_rotation``,
  ``changed_authority_fields``, ``is_handle_valid``), not re-derived here; and
- the one rule it adds of its own -- refusing an Organisation change -- is
  visibly a Core scope gate rather than a governed one, which is shown by
  demonstrating that the codec itself would have accepted the same rotation.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract.v1 import codec
from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.identity.models import IdentityProjection
from omnivia_core.session.context import (
    ContextIssuer,
    ContextIssuerError,
    new_context_id,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHELL_CONTEXT = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures" / "valid" / "shell-context.json"

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def wire() -> dict[str, Any]:
    return json.loads(SHELL_CONTEXT.read_text(encoding="utf-8"))


def context(**overrides: Any) -> gen.ShellContext:
    document = wire()
    document.update(overrides)
    return gen.ShellContext.from_wire(document)


def issuer() -> ContextIssuer:
    return ContextIssuer(current=context())


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_an_issuer_holds_the_context_it_was_given() -> None:
    current = context()
    assert issuer().current == current
    assert current.context_id == "ctx-001"


def test_an_issuer_refuses_anything_that_is_not_a_shell_context() -> None:
    with pytest.raises(ContextIssuerError):
        ContextIssuer(current=wire())  # type: ignore[arg-type]


def test_minted_context_ids_are_admissible_and_not_guessable_from_each_other() -> None:
    first, second = new_context_id(), new_context_id()
    assert first != second
    # Admissible where the contract expects an Identifier: a context built with
    # it validates, which is the only definition of "admissible" that matters.
    context(contextId=first, contextVersion=2).validate()


# --------------------------------------------------------------------------
# Workspace switching -- the one switchable authority in this lane
# --------------------------------------------------------------------------


def test_switching_workspace_rotates_the_context() -> None:
    before = issuer()
    after = before.switch_workspace("workspace-002", issued_at=NOW.isoformat())

    assert after.current.workspace_ref == "workspace-002"
    assert after.current.context_id != before.current.context_id
    assert after.current.context_version == before.current.context_version + 1
    assert after.changed_since(before.current) == ("workspaceRef",)


def test_the_rotation_the_issuer_produces_is_one_the_codec_accepts() -> None:
    """Not a restatement of the rule -- the codec is asked directly."""
    before = issuer()
    after = before.switch_workspace("workspace-002", issued_at=NOW.isoformat())
    codec.validate_context_rotation(before.current, after.current)


def test_a_handle_bound_to_the_superseded_context_stops_working() -> None:
    before = issuer()
    assert before.handle_is_valid("ctx-001") is True

    after = before.switch_workspace("workspace-002", issued_at=NOW.isoformat())
    assert after.handle_is_valid("ctx-001") is False
    assert after.handle_is_valid(after.current.context_id) is True


def test_switching_to_the_workspace_already_selected_is_a_no_op() -> None:
    """A redundant switch must not rotate: rotation invalidates every live
    handle, and there is no change of authority to justify that."""
    before = issuer()
    after = before.switch_workspace("workspace-001", issued_at=NOW.isoformat())
    assert after is before
    assert after.handle_is_valid("ctx-001") is True


def test_a_caller_supplied_context_id_is_used_verbatim() -> None:
    after = issuer().switch_workspace(
        "workspace-002", issued_at=NOW.isoformat(), context_id="ctx-002"
    )
    assert after.current.context_id == "ctx-002"


def test_switching_refuses_a_non_string_workspace_reference() -> None:
    with pytest.raises(ContextIssuerError):
        issuer().switch_workspace(2, issued_at=NOW.isoformat())  # type: ignore[arg-type]


def test_a_switch_that_would_produce_an_invalid_context_is_refused_by_the_codec() -> None:
    """Refused by the contract, not by Core: the error type is the governed one,
    which is how a reader can tell the two kinds of refusal apart."""
    with pytest.raises(codec.HostAuthorityError, match="invalid trusted record"):
        issuer().switch_workspace("not a valid ref", issued_at=NOW.isoformat())


# --------------------------------------------------------------------------
# Identity -- the optional account session, projected onto existing fields
# --------------------------------------------------------------------------


def test_applying_an_account_identity_rotates_the_two_projected_fields() -> None:
    before = issuer()
    after = before.apply_identity(
        IdentityProjection(
            principal_ref="principal-account-001",
            entitlement_summary_ref="entitlements-account-001",
        ),
        issued_at=NOW.isoformat(),
    )
    assert after.changed_since(before.current) == ("principalRef", "entitlementSummaryRef")
    assert after.handle_is_valid("ctx-001") is False


def test_applying_the_identity_already_in_force_is_a_no_op() -> None:
    before = issuer()
    unchanged = before.apply_identity(
        IdentityProjection(
            principal_ref=before.current.principal_ref,
            entitlement_summary_ref=before.current.entitlement_summary_ref,
        ),
        issued_at=NOW.isoformat(),
    )
    assert unchanged is before


def test_apply_identity_refuses_anything_that_is_not_a_projection() -> None:
    with pytest.raises(ContextIssuerError):
        issuer().apply_identity(
            {"principal_ref": "principal-account-001"},  # type: ignore[arg-type]
            issued_at=NOW.isoformat(),
        )


def test_the_issued_context_carries_no_account_session_state() -> None:
    """Sign-in changes two references and nothing else. Anything about the
    session itself stays outside the frozen record."""
    before = issuer()
    after = before.apply_identity(
        IdentityProjection(
            principal_ref="principal-account-001",
            entitlement_summary_ref="entitlements-account-001",
        ),
        issued_at=NOW.isoformat(),
    )
    emitted = after.current.to_wire()
    assert set(emitted) <= set(gen.ShellContext.WIRE_FIELDS)
    assert emitted["organisationRef"] == before.current.organisation_ref


# --------------------------------------------------------------------------
# Expiry is carried forward, never quietly dropped
# --------------------------------------------------------------------------


def test_a_rotation_inherits_the_current_expiry() -> None:
    expiring = ContextIssuer(current=context(expiresAt=LATER.isoformat()))
    after = expiring.switch_workspace("workspace-002", issued_at=NOW.isoformat())
    assert after.current.expires_at == LATER.isoformat()


def test_an_explicit_expiry_overrides_the_inherited_one() -> None:
    expiring = ContextIssuer(current=context(expiresAt=LATER.isoformat()))
    sooner = (NOW + timedelta(minutes=5)).isoformat()
    after = expiring.switch_workspace(
        "workspace-002", issued_at=NOW.isoformat(), expires_at=sooner
    )
    assert after.current.expires_at == sooner


def test_no_handle_survives_an_expired_context() -> None:
    expired = ContextIssuer(current=context(expiresAt=NOW.isoformat()))
    assert expired.handle_is_valid("ctx-001", now=NOW) is False


# --------------------------------------------------------------------------
# The Organisation scope gate (decision D2)
# --------------------------------------------------------------------------


def test_an_organisation_change_is_refused_by_core() -> None:
    candidate = context(contextId="ctx-002", contextVersion=2, organisationRef="org-002")
    with pytest.raises(ContextIssuerError, match="out of scope"):
        issuer().rotate(candidate)


def test_the_refusal_is_a_core_scope_gate_and_not_a_contract_rule() -> None:
    """The distinction the issuer's docstring claims, asserted: the frozen codec
    accepts exactly the rotation Core declines, so the gate is Core's decision to
    withdraw and not a governed one to work around."""
    before = issuer()
    candidate = context(contextId="ctx-002", contextVersion=2, organisationRef="org-002")

    codec.validate_context_rotation(before.current, candidate)  # the contract is content
    assert "organisationRef" not in codec.CONTEXT_AUTHORITY_FIELDS

    with pytest.raises(ContextIssuerError):
        before.rotate(candidate)


def test_an_organisation_change_riding_along_with_a_workspace_switch_is_refused() -> None:
    """The gate is on the field, not on the verb, so it cannot be bypassed by
    bundling the Organisation change into a rotation that has another reason."""
    before = issuer()
    candidate = replace(
        before.current,
        context_id="ctx-002",
        context_version=2,
        workspace_ref="workspace-002",
        organisation_ref="org-002",
    )
    with pytest.raises(ContextIssuerError):
        before.rotate(candidate)


# --------------------------------------------------------------------------
# Delegation: the codec's rules still bite through the issuer
# --------------------------------------------------------------------------


def test_a_changed_context_reusing_the_same_id_is_refused_by_the_codec() -> None:
    before = issuer()
    with pytest.raises(codec.HostAuthorityError, match="contextId"):
        before.rotate(context(workspaceRef="workspace-002"))


def test_a_rotation_that_does_not_advance_the_version_is_refused_by_the_codec() -> None:
    before = issuer()
    with pytest.raises(codec.HostAuthorityError, match="contextVersion"):
        before.rotate(context(contextId="ctx-002", workspaceRef="workspace-002"))


def test_re_presenting_an_identical_record_is_accepted() -> None:
    before = issuer()
    assert before.rotate(context()).current == before.current


def test_changed_since_is_the_codec_answer() -> None:
    before = issuer()
    after = before.switch_workspace("workspace-002", issued_at=NOW.isoformat())
    assert after.changed_since(before.current) == codec.changed_authority_fields(
        before.current, after.current
    )


def test_rotate_refuses_anything_that_is_not_a_shell_context() -> None:
    with pytest.raises(ContextIssuerError):
        issuer().rotate(wire())  # type: ignore[arg-type]


def test_the_issuer_is_immutable_so_the_old_authority_still_has_a_moment() -> None:
    before = issuer()
    after = before.switch_workspace("workspace-002", issued_at=NOW.isoformat())
    assert after is not before
    assert before.current.workspace_ref == "workspace-001"
    with pytest.raises(FrozenInstanceError):
        before.current = after.current  # type: ignore[misc]


def test_a_retired_context_id_cannot_be_re_issued() -> None:
    """Replaying a retired ID revives every handle bound to it.

    `is_handle_valid` compares a handle's context ID to the published one, and
    `validate_context_rotation` only ever compares the outgoing record to the
    incoming one -- so a replay two switches later is lawful to the codec and
    catastrophic in effect: a handle admitted under Workspace A becomes valid
    again under Workspace C.
    """
    opened = issuer()
    first = opened.current.context_id

    second = opened.switch_workspace("workspace-002", issued_at=LATER.isoformat())
    assert not second.handle_is_valid(first, now=LATER)

    with pytest.raises(ContextIssuerError, match="retired context ID"):
        second.switch_workspace("workspace-003", issued_at=LATER.isoformat(), context_id=first)

    # And the session is unchanged by the refusal.
    assert second.current.workspace_ref == "workspace-002"
    assert not second.handle_is_valid(first, now=LATER)


def test_a_caller_supplied_context_id_still_works_when_it_is_fresh() -> None:
    # The parameter exists for determinism; refusing replays must not refuse it.
    rotated = issuer().switch_workspace(
        "workspace-002", issued_at=LATER.isoformat(), context_id="ctx-chosen"
    )
    assert rotated.current.context_id == "ctx-chosen"
