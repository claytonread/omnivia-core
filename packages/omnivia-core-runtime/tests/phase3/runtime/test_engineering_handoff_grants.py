"""Cross-principal continuity handoff grants (migration 0064), end to end.

Driven through the production application surface with three authenticated
contributors: an owner, a grantee and a bystander.  A grant is the single exception
to owner-only continuity: it lets one other existing principal read one exact
checkpoint's redacted handoff, by checkpoint id, through its own binding, while it
is unexpired, unrevoked and pinned to the checkpoint's unchanged digest.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_engineering_source_coverage as sc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import engineering_family_session
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.portable import export_portable, verify_portable
from test_engineering_continuity import (
    OTHER,
    OWNER,
    _append_input,
    _close_input,
    _register_input,
    _settled,
    _stated,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    decode_request,
    encode_request,
)

BYSTANDER = engineering_family_session(
    principal_id="bystander",
    installation_id=s0.INSTALLATION_ID,
    workspace_id=sc.WORKSPACE_ID,
)
GRANTEE = OTHER

_GRANT_TABLES = (
    "omnivia_engineering_handoff_grants",
    "omnivia_engineering_handoff_grant_revocations",
)
_WALL = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return s0.clock_at(wall=_WALL)


@pytest.fixture
def workspace(tmp_path: Any, clock: FakeClock) -> Any:
    opened = sc.Workspace(tmp_path, clock=clock)
    yield opened
    opened.holder.connection.close()


def _owner_checkpoint(workspace: Any, *, close: bool = False) -> dict[str, Any]:
    """The owner registers, appends one checkpoint and optionally closes."""
    session_id = str(
        workspace.ok("continuity.session.register", _register_input(), session=OWNER)[
            "session"
        ]["session_id"]
    )
    receipt = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(session_id),
        session=OWNER,
        **_stated("seq-0"),
    )["receipt"]
    if close:
        workspace.ok(
            "continuity.session.close",
            _close_only(session_id),
            session=OWNER,
            **_stated("seq-1"),
        )
    return {**receipt, "session_id": session_id}


def _close_only(session_id: str) -> dict[str, Any]:
    payload = _close_input(session_id, expected_sequence=1)
    del payload["final_checkpoint"]
    return payload


def _register(workspace: Any, session: Any) -> str:
    return str(
        workspace.ok("continuity.session.register", _register_input(), session=session)[
            "session"
        ]["session_id"]
    )


def _grant_input(receipt: dict[str, Any], grantee: str = "intruder", **extra: Any) -> dict[str, Any]:
    return {
        "checkpoint_id": receipt["checkpoint_id"],
        "checkpoint_digest": receipt["content_digest"],
        "grantee_principal_id": grantee,
        "ttl_seconds": 3600,
        **extra,
    }


def _grant(workspace: Any, receipt: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    session = kwargs.pop("session", OWNER)
    grantee = kwargs.pop("grantee", "intruder")
    return dict(
        workspace.ok(
            "continuity.handoff.grant",
            _grant_input(receipt, grantee, **kwargs),
            session=session,
        )["grant"]
    )


def _read(workspace: Any, receipt: dict[str, Any], session: Any) -> Any:
    return workspace.call(
        "continuity.handoff.read", {"checkpoint_id": receipt["checkpoint_id"]}, session=session
    )


def _refusal(workspace: Any, payload: dict[str, Any], session: Any) -> tuple[str, str, str]:
    return workspace.refused("continuity.handoff.read", payload, session=session)


def _missing(workspace: Any, session: Any) -> tuple[str, str, str]:
    """What every hidden checkpoint must look like: a checkpoint that does not exist."""
    return _refusal(workspace, {"checkpoint_id": "eck-nowhere"}, session)


def test_a_grantee_reads_the_redacted_handoff_by_checkpoint_id_only(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace, close=True)
    _register(workspace, GRANTEE)
    before = _refusal(workspace, {"checkpoint_id": receipt["checkpoint_id"]}, GRANTEE)
    assert before == _missing(workspace, GRANTEE)
    assert before[0] == ERROR_CODE_NOT_FOUND

    grant = _grant(workspace, receipt)
    assert grant["checkpoint_id"] == receipt["checkpoint_id"]
    assert grant["checkpoint_digest"] == receipt["content_digest"]
    assert grant["grantee_principal_id"] == GRANTEE.principal_id

    view = workspace.ok(
        "continuity.handoff.read", {"checkpoint_id": receipt["checkpoint_id"]}, session=GRANTEE
    )["handoff"]
    assert view["format_version"] == "continuity_handoff.v1"
    assert view["checkpoint_id"] == receipt["checkpoint_id"]
    assert view["redacted"] is True
    assert view["content_digest"] != receipt["content_digest"]
    # Working context only: no session, principal, grant or sender-run identity.
    assert set(view) <= {
        "format_version",
        "checkpoint_id",
        "objective",
        "applicability",
        "unresolved_work",
        "next_actions",
        "omissions",
        "redacted",
        "content_digest",
    }

    # Session plus sequence stays owner-only, exactly as a missing session.
    by_sequence = {"session_id": receipt["session_id"], "sequence": receipt["sequence"]}
    assert _refusal(workspace, by_sequence, GRANTEE) == _refusal(
        workspace, {"session_id": "esess-nowhere", "sequence": 1}, GRANTEE
    )


def test_a_grant_changes_nothing_about_the_owners_own_reads(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    owner_view = workspace.ok(
        "continuity.handoff.read", {"checkpoint_id": receipt["checkpoint_id"]}, session=OWNER
    )["handoff"]
    _grant(workspace, receipt)
    grantee_view = workspace.ok(
        "continuity.handoff.read", {"checkpoint_id": receipt["checkpoint_id"]}, session=GRANTEE
    )["handoff"]
    assert grantee_view == owner_view
    assert (
        workspace.ok(
            "continuity.handoff.read", {"checkpoint_id": receipt["checkpoint_id"]}, session=OWNER
        )["handoff"]
        == owner_view
    )


def test_every_path_that_is_not_a_live_grant_is_one_indistinguishable_not_found(
    workspace: Any, clock: FakeClock
) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    _register(workspace, BYSTANDER)
    grant = _grant(workspace, receipt, ttl_seconds=120)
    key = {"checkpoint_id": receipt["checkpoint_id"]}
    hidden = _missing(workspace, GRANTEE)
    assert hidden[0] == ERROR_CODE_NOT_FOUND

    # The wrong grantee: a registered bystander the owner never named.
    assert _refusal(workspace, key, BYSTANDER) == _missing(workspace, BYSTANDER) == hidden
    assert _refusal(workspace, {"checkpoint_id": "eck-other"}, GRANTEE) == hidden

    # A grantee binding that is no longer current reads as no grant, not as a
    # distinct refusal: its session closed under it.
    workspace.ok(
        "continuity.session.close",
        {"session_id": workspace.binding_for(GRANTEE.principal_id).session_id},
        session=GRANTEE,
        **_stated("seq-0"),
    )
    assert _refusal(workspace, key, GRANTEE) == hidden

    # Revoked, on the next read.
    fresh = engineering_family_session(
        principal_id="late-grantee",
        installation_id=s0.INSTALLATION_ID,
        workspace_id=sc.WORKSPACE_ID,
    )
    _register(workspace, fresh)
    _grant(workspace, receipt, grantee="late-grantee")
    assert workspace.ok("continuity.handoff.read", key, session=fresh)["handoff"]
    revoked = workspace.ok(
        "continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=OWNER
    )
    assert revoked["grant_id"] == grant["grant_id"]

    # Expired: the second grant's two minutes pass.
    expiring = engineering_family_session(
        principal_id="expiring-grantee",
        installation_id=s0.INSTALLATION_ID,
        workspace_id=sc.WORKSPACE_ID,
    )
    _register(workspace, expiring)
    _grant(workspace, receipt, grantee="expiring-grantee", ttl_seconds=60)
    assert workspace.ok("continuity.handoff.read", key, session=expiring)["handoff"]
    clock.advance_wall(61)
    assert _refusal(workspace, key, expiring) == hidden
    # ...and the still-live grant to `late-grantee` is unaffected by the other's expiry.
    assert workspace.ok("continuity.handoff.read", key, session=fresh)["handoff"]


def test_a_grantee_without_a_registered_session_is_refused_before_any_grant_is_consulted(
    workspace: Any,
) -> None:
    """No continuity binding is an authority failure for every checkpoint id alike, so
    it discloses nothing about whether a grant exists."""
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    _grant(workspace, receipt)
    unbound = engineering_family_session(
        principal_id="intruder",
        installation_id=s0.INSTALLATION_ID,
        workspace_id=sc.WORKSPACE_ID,
    )
    # A fresh workspace-side binding map would have none; strip the retained one.
    workspace._continuity_bindings.pop("intruder")
    named = _refusal(workspace, {"checkpoint_id": receipt["checkpoint_id"]}, unbound)
    assert named == _missing(workspace, unbound)
    assert named[0] == ERROR_CODE_AUTHORIZATION_DENIED


def test_revocation_applies_on_the_next_read_and_is_idempotent(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    grant = _grant(workspace, receipt)
    key = {"checkpoint_id": receipt["checkpoint_id"]}
    assert workspace.ok("continuity.handoff.read", key, session=GRANTEE)["handoff"]

    first = workspace.ok(
        "continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=OWNER
    )
    assert _refusal(workspace, key, GRANTEE) == _missing(workspace, GRANTEE)
    again = workspace.ok(
        "continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=OWNER
    )
    assert again == first
    rows = workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_handoff_grant_revocations"
    ).fetchone()
    assert rows == (1,)
    # The grant row itself is history, never deleted.
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_handoff_grants"
    ).fetchone() == (1,)

    # Only the grantor revokes: the grantee and a bystander see `not_found`, as for
    # a grant that does not exist.
    absent = workspace.refused(
        "continuity.handoff.revoke", {"grant_id": "ehg-nowhere"}, session=OWNER
    )
    assert absent[0] == ERROR_CODE_NOT_FOUND
    for outsider in (GRANTEE, BYSTANDER):
        assert (
            workspace.refused(
                "continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=outsider
            )
            == absent
        )


def test_closing_the_owning_session_does_not_revoke_a_grant(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    _grant(workspace, receipt)
    workspace.ok(
        "continuity.session.close",
        _close_only(receipt["session_id"]),
        session=OWNER,
        **_stated("seq-1"),
    )
    assert workspace.ok(
        "continuity.handoff.read", {"checkpoint_id": receipt["checkpoint_id"]}, session=GRANTEE
    )["handoff"]
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_handoff_grant_revocations"
    ).fetchone() == (0,)


def test_a_grantee_cannot_grant_regrant_or_revoke(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    _register(workspace, BYSTANDER)
    grant = _grant(workspace, receipt)
    before = _settled(workspace)
    # The grantee names the checkpoint and digest it can read, and a third principal.
    regrant = workspace.refused(
        "continuity.handoff.grant",
        _grant_input(receipt, "bystander"),
        session=GRANTEE,
    )
    assert regrant[0] == ERROR_CODE_NOT_FOUND
    assert regrant == workspace.refused(
        "continuity.handoff.grant",
        _grant_input(
            {"checkpoint_id": "eck-nowhere", "content_digest": receipt["content_digest"]},
            "bystander",
        ),
        session=GRANTEE,
    )
    assert workspace.refused(
        "continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=GRANTEE
    )[0] == ERROR_CODE_NOT_FOUND
    assert _read(workspace, receipt, BYSTANDER).__class__.__name__ == "ErrorResponseEnvelope"
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_handoff_grants"
    ).fetchone() == (1,)
    assert _settled(workspace)[0] == before[0]


def test_a_grant_names_an_owned_checkpoint_its_digest_and_an_existing_grantee(
    workspace: Any,
) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    stranger = workspace.refused(
        "continuity.handoff.grant", _grant_input(receipt, "never-registered"), session=OWNER
    )
    wrong_digest = workspace.refused(
        "continuity.handoff.grant",
        _grant_input({**receipt, "content_digest": "sha256:" + "0" * 64}),
        session=OWNER,
    )
    other_owner = workspace.refused(
        "continuity.handoff.grant", _grant_input(receipt, "bystander"), session=GRANTEE
    )
    nonexistent = workspace.refused(
        "continuity.handoff.grant",
        _grant_input({**receipt, "checkpoint_id": "eck-nowhere"}),
        session=OWNER,
    )
    assert {stranger[0], wrong_digest[0], other_owner[0], nonexistent[0]} == {ERROR_CODE_NOT_FOUND}
    assert stranger == wrong_digest == other_owner == nonexistent

    invalid = [
        _grant_input(receipt, OWNER.principal_id),
        _grant_input(receipt, ttl_seconds=59),
        _grant_input(receipt, ttl_seconds=604801),
        _grant_input(receipt, ttl_seconds=True),
        _grant_input(receipt, workspace_id=sc.WORKSPACE_ID),
        _grant_input(receipt, installation_id="inst"),
        _grant_input(receipt, session_id=receipt["session_id"]),
        _grant_input(receipt, checkpoint_digest="not-a-digest"),
        {k: v for k, v in _grant_input(receipt).items() if k != "ttl_seconds"},
    ]
    for payload in invalid:
        assert workspace.refused("continuity.handoff.grant", payload, session=OWNER)[0] == (
            ERROR_CODE_INVALID_REQUEST
        ), payload
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_handoff_grants"
    ).fetchone() == (0,)


def test_a_second_live_grant_conflicts_until_the_first_ends(
    workspace: Any, clock: FakeClock
) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    first = _grant(workspace, receipt, ttl_seconds=60)
    assert (
        workspace.refused("continuity.handoff.grant", _grant_input(receipt), session=OWNER)[0]
        == ERROR_CODE_CONFLICT
    )
    clock.advance_wall(61)
    second = _grant(workspace, receipt)
    assert second["grant_id"] != first["grant_id"]
    workspace.ok("continuity.handoff.revoke", {"grant_id": second["grant_id"]}, session=OWNER)
    third = _grant(workspace, receipt)
    assert len({first["grant_id"], second["grant_id"], third["grant_id"]}) == 3


def test_a_replayed_grant_key_returns_the_original_grant(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    payload = _grant_input(receipt)
    one = workspace.ok(
        "continuity.handoff.grant", payload, session=OWNER, key="idem-grant"
    )
    two = workspace.ok(
        "continuity.handoff.grant", payload, session=OWNER, key="idem-grant"
    )
    assert one == two
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_handoff_grants"
    ).fetchone() == (1,)


def _fenced(workspace: Any) -> Any:
    return fenced_transaction(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=sc.WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
    )


def test_the_database_holds_grant_ownership_audit_and_append_only_itself(
    workspace: Any,
) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    grant = _grant(workspace, receipt)
    connection = workspace.holder.connection
    stored = connection.execute("SELECT * FROM omnivia_engineering_handoff_grants")
    columns = [d[0] for d in stored.description]
    template = dict(zip(columns, stored.fetchone(), strict=True))
    grant_audit = template["audit_ref"]

    def forged_grant(**changes: Any) -> None:
        values = {**template, "grant_id": "ehg-forged", **changes}
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with _fenced(workspace) as fenced:
            fenced.execute(
                f"INSERT INTO omnivia_engineering_handoff_grants ({names}) VALUES ({marks})",
                tuple(values.values()),
            )

    # Inside the fenced writer the triggers still hold ownership, the pinned digest,
    # an existing grantee, one live grant per checkpoint and grantee, and the audit.
    for changes in (
        {},
        {"grantor_principal_id": "intruder", "grantee_principal_id": "local-user"},
        {"checkpoint_digest": "sha256:" + "1" * 64},
        {"grantee_principal_id": "never-registered"},
        {"grantee_principal_id": template["grantor_principal_id"]},
        {"expires_at_us": template["granted_at_us"] + 1},
        {"audit_ref": "audit-missing"},
    ):
        with pytest.raises(sqlite3.DatabaseError):
            forged_grant(**changes)
    # Outside it, no direct write is possible at all.
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("DELETE FROM omnivia_engineering_handoff_grants")

    workspace.ok("continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=OWNER)
    audits = dict(
        connection.execute(
            "SELECT operation, outcome_class FROM omnivia_application_audit_events "
            "WHERE operation LIKE 'continuity.handoff.%'"
        ).fetchall()
    )
    assert audits == {
        "continuity.handoff.grant": "succeeded",
        "continuity.handoff.revoke": "succeeded",
    }
    revoked_at = connection.execute(
        "SELECT revoked_at_us FROM omnivia_engineering_handoff_grant_revocations"
    ).fetchone()[0]
    # A revocation must carry the grantor's own revoke audit, not the grant's.
    with pytest.raises(sqlite3.DatabaseError), _fenced(workspace) as fenced:
        fenced.execute(
            "INSERT INTO omnivia_engineering_handoff_grant_revocations "
            "(workspace_id, grant_id, revoked_at_us, audit_ref) VALUES (?, ?, ?, ?)",
            (sc.WORKSPACE_ID, "ehg-forged", revoked_at, grant_audit),
        )
    for statement in (
        "UPDATE omnivia_engineering_handoff_grants SET expires_at_us = expires_at_us + 1",
        "DELETE FROM omnivia_engineering_handoff_grants",
        "UPDATE omnivia_engineering_handoff_grant_revocations SET revoked_at_us = 1",
        "DELETE FROM omnivia_engineering_handoff_grant_revocations",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), _fenced(
            workspace
        ) as fenced:
            fenced.execute(statement)


def test_a_checkpoint_whose_digest_no_longer_matches_the_pin_is_hidden_from_the_grantee(
    workspace: Any,
) -> None:
    """The grant is pinned to the digest it was issued for. Storage corruption that
    changes the checkpoint's recorded digest -- which the service path can never do;
    the test drops the append-only guard solely to create that state -- ends the
    grant's effect with the same `not_found` any hidden checkpoint gives."""
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    _grant(workspace, receipt)
    key = {"checkpoint_id": receipt["checkpoint_id"]}
    assert workspace.ok("continuity.handoff.read", key, session=GRANTEE)["handoff"]

    workspace.holder.connection.close()
    tampered = sqlite3.connect(str(workspace.holder.path))
    try:
        tampered.execute("DROP TRIGGER omnivia_guard_omnivia_engineering_checkpoints_update")
        tampered.execute(
            "UPDATE omnivia_engineering_checkpoints SET content_digest = ? "
            "WHERE workspace_id = ? AND checkpoint_id = ?",
            ("sha256:" + "2" * 64, sc.WORKSPACE_ID, receipt["checkpoint_id"]),
        )
        tampered.commit()
    finally:
        tampered.close()
    workspace.restart()
    workspace._continuity_bindings.clear()
    _register(workspace, GRANTEE)
    assert _refusal(workspace, key, GRANTEE) == _missing(workspace, GRANTEE)


def test_an_unknown_grantee_key_never_reaches_storage(workspace: Any) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    before = _settled(workspace)
    payload = {**_grant_input(receipt), "capability": "engineering.write"}
    assert workspace.refused("continuity.handoff.grant", payload, session=OWNER)[0] == (
        ERROR_CODE_INVALID_REQUEST
    )
    assert _settled(workspace)[0] == before[0]


def test_a_portable_export_never_carries_cross_principal_read_authority(
    workspace: Any, tmp_path: Path
) -> None:
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    grant = _grant(workspace, receipt)
    workspace.ok("continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=OWNER)
    _grant(workspace, receipt)
    source = workspace.holder.connection
    assert [
        source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in _GRANT_TABLES
    ] == [2, 1]
    # The service owns the database exclusively; export reads it once it is released.
    source.close()

    artifact = tmp_path / "artifact"
    export_portable(workspace.holder.path, artifact, exported_at_us=1_800_000_000_000_000)
    verify_portable(artifact)
    exported = open_database(artifact / "workspace.sqlite", OpenMode.READ_ONLY)
    try:
        for table in _GRANT_TABLES:
            assert exported.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table
        # The checkpoint itself stays: identities and lineage survive a restore.
        assert exported.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkpoints WHERE checkpoint_id = ?",
            (receipt["checkpoint_id"],),
        ).fetchone() == (1,)
    finally:
        exported.close()
    # Exporting only read the source.
    workspace.restart()
    assert workspace.holder.connection.execute(
        f"SELECT COUNT(*) FROM {_GRANT_TABLES[0]}"
    ).fetchone() == (2,)


class _WireSurface:
    """The production surface, but every request first crosses the wire codec.

    The transports decode `input` into a read-only mapping, so a handler that only
    accepts a `dict` refuses all real traffic while dict-built tests still pass.
    """

    def __init__(self, surface: Any) -> None:
        self._surface = surface

    def __getattr__(self, name: str) -> Any:
        return getattr(self._surface, name)

    def dispatch(self, request: Any) -> Any:
        return self._surface.dispatch(decode_request(encode_request(request)))

    def dispatch_for_session(self, request: Any, session: Any) -> Any:
        return self._surface.dispatch_for_session(
            decode_request(encode_request(request)), session
        )


def over_the_wire(workspace: Any) -> Any:
    workspace.surface = _WireSurface(workspace.surface)
    return workspace


def test_grant_read_and_revoke_work_for_wire_decoded_read_only_input(workspace: Any) -> None:
    over_the_wire(workspace)
    receipt = _owner_checkpoint(workspace)
    _register(workspace, GRANTEE)
    grant = _grant(workspace, receipt)
    key = {"checkpoint_id": receipt["checkpoint_id"]}
    assert workspace.ok("continuity.handoff.read", key, session=GRANTEE)["handoff"]
    workspace.ok("continuity.handoff.revoke", {"grant_id": grant["grant_id"]}, session=OWNER)
    assert _refusal(workspace, key, GRANTEE) == _missing(workspace, GRANTEE)
