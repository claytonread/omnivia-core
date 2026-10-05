"""Trusted `symbol` and `source_span` selector attestations (migration 0065).

Core never parses source.  An installed Dev adapter, running as the authenticated
stream owner, states what one selector resolved to in one sealed snapshot, and the
evaluator compares those statements between a dependency's baseline and a covered
target.  These tests drive ingest through the production surface and the
evaluator through the same `Workspace.status` the whole-file tests use.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_engineering_captured_source_coverage as captured
import test_engineering_source_coverage as sc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.application import engineering_family_session
from omnivia_core_runtime.storage import engineering_source
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.portable import export_portable, verify_portable
from test_engineering_handoff_grants import over_the_wire

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
)

ADAPTER = {"adapter_id": "omnivia.dev.python", "adapter_version": "1.0.0"}
PATH = "src/auth.py"
SYMBOL = "src/auth.py::login"
SPAN = "src/auth.py:10-24"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


SYMBOL_V1 = _sha("login v1")
SYMBOL_V2 = _sha("login v2")
SPAN_V1 = _sha("span v1")
SPAN_V2 = _sha("span v2")


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = sc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _attestation(
    snapshot_id: str,
    file_digest: str,
    *,
    selector: str = SYMBOL,
    selector_type: str = "symbol",
    digest: str | None = SYMBOL_V1,
    state: str = "present",
    coverage: str = "complete",
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repository_id": sc.REPOSITORY,
        "stream_id": sc.STREAM,
        "snapshot_id": snapshot_id,
        "path": PATH,
        "file_digest": file_digest,
        "selector_type": selector_type,
        "selector": selector,
        "file_coverage": coverage,
        "selector_state": state,
        **ADAPTER,
    }
    if state == "present":
        payload["selector_digest"] = digest
    payload.update(overrides)
    return payload


def _attest(workspace: Any, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return workspace.ok("engineering.selector.attest", payload, **kwargs)


def _two_snapshots(workspace: Any) -> None:
    """A baseline `esnap-a` and a covered target `esnap-b` whose auth file changed."""
    workspace.record(sc._source(1, "esnap-a", sc.FILES_A), key="src-a")
    workspace.record(
        sc._source(
            2, "esnap-b", {**sc.FILES_A, PATH: sc.AUTH_V2}, predecessor="esnap-a"
        ),
        key="src-b",
    )


def _record(workspace: Any, dependencies: list[dict[str, Any]], title: str) -> dict[str, str]:
    return workspace.observe(
        sc._observation(sc._manifest("esnap-a", dependencies), title=title)
    )


def _symbol_dependency(
    digest: str | None = SYMBOL_V1,
    meaning: str = "must_match",
    selector: str = SYMBOL,
    selector_type: str = "symbol",
) -> dict[str, Any]:
    return sc._dependency(selector, digest, meaning, selector_type)


def _attest_pair(
    workspace: Any,
    *,
    selector: str = SYMBOL,
    selector_type: str = "symbol",
    baseline: str | None = SYMBOL_V1,
    target: str | None = SYMBOL_V1,
    target_state: str = "present",
    target_coverage: str = "complete",
    baseline_coverage: str = "complete",
    target_adapter: dict[str, str] | None = None,
) -> None:
    _attest(
        workspace,
        _attestation(
            "esnap-a",
            sc.AUTH_V1,
            selector=selector,
            selector_type=selector_type,
            digest=baseline,
            coverage=baseline_coverage,
        ),
    )
    _attest(
        workspace,
        _attestation(
            "esnap-b",
            sc.AUTH_V2,
            selector=selector,
            selector_type=selector_type,
            digest=target,
            state=target_state,
            coverage=target_coverage,
            **(target_adapter or {}),
        ),
    )


# --- evaluator ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector_type", "selector", "baseline", "target"),
    (
        ("symbol", SYMBOL, SYMBOL_V1, SYMBOL_V2),
        ("source_span", SPAN, SPAN_V1, SPAN_V2),
    ),
)
def test_equal_digests_match_and_different_digests_are_stale_for_both_selector_types(
    workspace: Any, selector_type: str, selector: str, baseline: str, target: str
) -> None:
    _two_snapshots(workspace)
    dependency = _symbol_dependency(baseline, selector=selector, selector_type=selector_type)
    matched = _record(workspace, [dependency], "matched")
    stale = _record(workspace, [dependency], "stale")
    # Before any attestation the dependency is recorded and nothing more: `unknown`.
    assert workspace.status(matched, "esnap-b") == "unknown"
    _attest_pair(workspace, selector=selector, selector_type=selector_type, baseline=baseline, target=baseline)
    # The whole file changed between the snapshots; the selector's digest did not.
    assert workspace.status(matched, "esnap-b") == "matched"
    assert workspace.status(stale, "esnap-a") == "matched"

    other_selector = selector + "#2"
    _attest(
        workspace,
        _attestation(
            "esnap-a",
            sc.AUTH_V1,
            selector=other_selector,
            selector_type=selector_type,
            digest=baseline,
        ),
    )
    _attest(
        workspace,
        _attestation(
            "esnap-b",
            sc.AUTH_V2,
            selector=other_selector,
            selector_type=selector_type,
            digest=target,
        ),
    )
    changed = _record(
        workspace,
        [_symbol_dependency(baseline, selector=other_selector, selector_type=selector_type)],
        "changed",
    )
    assert workspace.status(changed, "esnap-b") == "potentially_stale"


def test_an_explicitly_absent_selector_in_a_completely_analysed_file_is_invalid(
    workspace: Any,
) -> None:
    _two_snapshots(workspace)
    record = _record(workspace, [_symbol_dependency()], "absent")
    _attest_pair(workspace, target=None, target_state="absent")
    assert workspace.status(record, "esnap-b") == "invalid"


@pytest.mark.parametrize(
    "case",
    (
        "partial-target",
        "partial-baseline",
        "adapter-version-mismatch",
        "adapter-id-mismatch",
        "absent-target-partial",
        "no-target-attestation",
        "no-baseline-attestation",
        "claimed-digest-differs",
        "context-only-only",
    ),
)
def test_missing_partial_or_mismatched_evidence_is_unknown_never_adverse(
    workspace: Any, case: str
) -> None:
    _two_snapshots(workspace)
    claimed = SYMBOL_V2 if case == "claimed-digest-differs" else SYMBOL_V1
    record = _record(
        workspace,
        [
            _symbol_dependency(
                claimed, "context_only" if case == "context-only-only" else "must_match"
            )
        ],
        case,
    )
    if case == "partial-target":
        _attest_pair(workspace, target=SYMBOL_V2, target_coverage="partial")
    elif case == "partial-baseline":
        _attest_pair(workspace, target=SYMBOL_V2, baseline_coverage="partial")
    elif case == "adapter-version-mismatch":
        _attest_pair(workspace, target=SYMBOL_V2, target_adapter={"adapter_version": "2.0.0"})
    elif case == "adapter-id-mismatch":
        _attest_pair(workspace, target=SYMBOL_V2, target_adapter={"adapter_id": "other.adapter"})
    elif case == "absent-target-partial":
        _attest_pair(workspace, target=None, target_state="absent", target_coverage="partial")
    elif case == "no-target-attestation":
        _attest(workspace, _attestation("esnap-a", sc.AUTH_V1))
    elif case == "no-baseline-attestation":
        _attest(workspace, _attestation("esnap-b", sc.AUTH_V2, digest=SYMBOL_V2))
    else:
        _attest_pair(workspace, target=SYMBOL_V2)
    assert workspace.status(record, "esnap-b") == "unknown"


def test_selector_and_whole_file_dependencies_must_all_agree(workspace: Any) -> None:
    _two_snapshots(workspace)
    both = _record(
        workspace,
        [_symbol_dependency(), sc._dependency("src/util.py", sc.UTIL_V1)],
        "both",
    )
    _attest_pair(workspace, target=SYMBOL_V1)
    assert workspace.status(both, "esnap-b") == "matched"

    whole_file_changed = _record(
        workspace,
        [_symbol_dependency(), sc._dependency(PATH, sc.AUTH_V1)],
        "whole file changed",
    )
    # The selector still matches, but the required whole file changed.
    assert workspace.status(whole_file_changed, "esnap-b") == "potentially_stale"


def test_the_other_selector_types_stay_unknown_even_with_a_symbol_attestation(
    workspace: Any,
) -> None:
    _two_snapshots(workspace)
    _attest_pair(workspace, target=SYMBOL_V1)
    for selector_type in ("config_key", "schema_contract", "external_evidence"):
        record = _record(
            workspace,
            [_symbol_dependency(SYMBOL_V1, selector_type=selector_type)],
            selector_type,
        )
        assert workspace.status(record, "esnap-b") == "unknown"


def test_a_symbol_attestation_never_answers_a_source_span_dependency(workspace: Any) -> None:
    _two_snapshots(workspace)
    _attest_pair(workspace, target=SYMBOL_V1)
    record = _record(
        workspace,
        [_symbol_dependency(SYMBOL_V1, selector_type="source_span")],
        "span",
    )
    assert workspace.status(record, "esnap-b") == "unknown"


def _rewrite(workspace: Any, column: str, value: str) -> None:
    """Set one attestation column behind the service's back, then adopt the workspace again.

    The append-only guard is dropped on a private connection solely to create a state
    the service path and the insert trigger cannot produce.
    """
    workspace.holder.connection.close()
    raw = sqlite3.connect(str(workspace.holder.path))
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DROP TRIGGER IF EXISTS omnivia_guard_engineering_selector_attestations_update")
        raw.execute(
            f"UPDATE omnivia_engineering_selector_attestations SET {column} = ? "
            "WHERE snapshot_id = 'esnap-b'",
            (value,),
        )
        raw.commit()
    finally:
        raw.close()
    workspace.restart()


def test_an_attestation_whose_bindings_no_longer_hold_is_untrusted_evidence(
    workspace: Any,
) -> None:
    """Storage that disagrees with an attestation's bindings is `unknown`, never a verdict."""
    _two_snapshots(workspace)
    record = _record(workspace, [_symbol_dependency()], "tampered")
    _attest_pair(workspace, target=SYMBOL_V1)
    assert workspace.status(record, "esnap-b") == "matched"
    for column, broken, intact in (
        ("file_digest", _sha("not the captured digest"), sc.AUTH_V2),
        ("producer_principal_id", "someone-else", sc.PRINCIPAL),
        ("repository_id", "erepo-other", sc.REPOSITORY),
    ):
        _rewrite(workspace, column, broken)
        assert workspace.status(record, "esnap-b") == "unknown", column
        _rewrite(workspace, column, intact)
        assert workspace.status(record, "esnap-b") == "matched", column


# --- ingest -------------------------------------------------------------------------


def test_ingest_stores_an_immutable_bound_statement_and_redelivery_is_idempotent(
    workspace: Any,
) -> None:
    _two_snapshots(workspace)
    first = _attest(workspace, _attestation("esnap-a", sc.AUTH_V1), key="attest-1")
    assert first["disposition"] == "recorded"
    assert _attest(workspace, _attestation("esnap-a", sc.AUTH_V1), key="attest-1") == first
    again = _attest(workspace, _attestation("esnap-a", sc.AUTH_V1), key="attest-2")
    assert again == {"attestation_id": first["attestation_id"], "disposition": "already_recorded"}

    # A different statement for the same selector in the same snapshot is a conflict,
    # whichever key it arrives under, and never replaces the stored one.
    different = _attestation("esnap-a", sc.AUTH_V1, digest=SYMBOL_V2)
    assert workspace.refused("engineering.selector.attest", different)[0] == ERROR_CODE_CONFLICT
    row = workspace.holder.connection.execute(
        "SELECT selector_digest, producer_principal_id, installation_id, "
        "repository_id, stream_id, path, file_digest, adapter_id, adapter_version "
        "FROM omnivia_engineering_selector_attestations"
    ).fetchall()
    assert row == [
        (
            SYMBOL_V1,
            sc.PRINCIPAL,
            workspace.holder.identity.installation_id,
            sc.REPOSITORY,
            sc.STREAM,
            PATH,
            sc.AUTH_V1,
            ADAPTER["adapter_id"],
            ADAPTER["adapter_version"],
        )
    ]


def test_only_the_authenticated_stream_owner_may_attest(workspace: Any) -> None:
    _two_snapshots(workspace)
    intruder = engineering_family_session(
        principal_id="intruder",
        installation_id=s0.INSTALLATION_ID,
        workspace_id=sc.WORKSPACE_ID,
    )
    refusal = workspace.refused(
        "engineering.selector.attest", _attestation("esnap-a", sc.AUTH_V1), session=intruder
    )
    assert refusal[0] == ERROR_CODE_AUTHORIZATION_DENIED
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_selector_attestations"
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"stream_id": "estream-nowhere"}, ERROR_CODE_NOT_FOUND),
        ({"snapshot_id": "esnap-nowhere"}, ERROR_CODE_NOT_FOUND),
        ({"repository_id": "erepo-other"}, ERROR_CODE_CONFLICT),
        ({"file_digest": _sha("not the captured digest")}, ERROR_CODE_CONFLICT),
        ({"path": "src/not-in-the-snapshot.py"}, ERROR_CODE_CONFLICT),
    ),
)
def test_every_exact_binding_must_validate_before_a_digest_is_stored(
    workspace: Any, change: dict[str, Any], code: str
) -> None:
    _two_snapshots(workspace)
    request = {**_attestation("esnap-a", sc.AUTH_V1), **change}
    refusal = workspace.refused("engineering.selector.attest", request)
    assert refusal[0] == code
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_selector_attestations"
    ).fetchone() == (0,)


def test_a_snapshot_of_another_stream_is_not_this_streams_to_attest(workspace: Any) -> None:
    _two_snapshots(workspace)
    workspace.record(
        sc._source(1, "esnap-other", sc.FILES_A, stream="estream-other"),
        key="src-other",
    )
    refusal = workspace.refused(
        "engineering.selector.attest", _attestation("esnap-other", sc.AUTH_V1)
    )
    assert refusal[0] == ERROR_CODE_NOT_FOUND


@pytest.mark.parametrize(
    "payload",
    (
        {"workspace_id": sc.WORKSPACE_ID},
        {"installation_id": "inst-forged"},
        {"principal_id": "someone"},
        {"purpose": "engineering_source"},
        {"scopes": ["engineering:source"]},
        {"capability": "engineering.source"},
        {"checkout_root": "/Users/dev/app"},
        {"source": "def login(): ..."},
        {"content": "def login(): ..."},
        {"selector_type": "config_key"},
        {"selector_type": "whole_file"},
        {"file_coverage": "unknown"},
        {"selector_state": "missing"},
        {"path": "/Users/dev/app/src/auth.py"},
        {"path": "..\\src\\auth.py"},
        {"path": "src/../auth.py"},
        {"selector": ""},
        {"selector": "login\nsource"},
        {"adapter_id": ""},
        {"adapter_version": "x" * 65},
        {"selector_digest": "sha256:short"},
        {"selector_digest": None},
        {"selector_state": "absent"},
    ),
)
def test_unknown_or_malformed_fields_fail_closed(workspace: Any, payload: dict[str, Any]) -> None:
    _two_snapshots(workspace)
    request = _attestation("esnap-a", sc.AUTH_V1)
    request.update(payload)
    if payload.get("selector_digest", "x") is None:
        request.pop("selector_digest")
    assert workspace.refused("engineering.selector.attest", request)[0] == (
        ERROR_CODE_INVALID_REQUEST
    )
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_selector_attestations"
    ).fetchone() == (0,)


def test_an_absent_selector_states_no_digest_and_a_present_one_requires_it(
    workspace: Any,
) -> None:
    _two_snapshots(workspace)
    assert (
        _attest(
            workspace, _attestation("esnap-b", sc.AUTH_V2, state="absent", digest=None)
        )["disposition"]
        == "recorded"
    )
    refusal = workspace.refused(
        "engineering.selector.attest",
        _attestation("esnap-b", sc.AUTH_V2, selector="other", state="absent")
        | {"selector_digest": SYMBOL_V1},
    )
    assert refusal[0] == ERROR_CODE_INVALID_REQUEST


def test_the_table_is_append_only_and_the_trigger_holds_every_binding(
    workspace: Any,
) -> None:
    _two_snapshots(workspace)
    _attest(workspace, _attestation("esnap-a", sc.AUTH_V1))
    connection = workspace.holder.connection
    stored = connection.execute("SELECT * FROM omnivia_engineering_selector_attestations")
    columns = [d[0] for d in stored.description]
    template = dict(zip(columns, stored.fetchone(), strict=True))

    def fenced() -> Any:
        return fenced_transaction(
            connection,
            workspace.holder.identity,
            workspace_id=sc.WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
        )

    for statement in (
        "UPDATE omnivia_engineering_selector_attestations SET selector_digest = NULL",
        "DELETE FROM omnivia_engineering_selector_attestations",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), fenced() as txn:
            txn.execute(statement)

    def forge(**changes: Any) -> None:
        values = {
            **template,
            "attestation_id": "esat-forged",
            "selector": "src/auth.py::other",
            **changes,
        }
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with fenced() as txn:
            txn.execute(
                f"INSERT INTO omnivia_engineering_selector_attestations ({names}) "
                f"VALUES ({marks})",
                tuple(values.values()),
            )

    # The statement is otherwise valid and carries the real audit, so each of these
    # is refused by exactly the binding it breaks.
    for changes in (
        {"producer_principal_id": "intruder"},
        {"repository_id": "erepo-other"},
        {"file_digest": _sha("not the captured digest")},
        {"path": "src/util.py"},
        {"stream_id": "estream-nowhere"},
        {"snapshot_id": "esnap-nowhere"},
        {"audit_ref": "audit-missing"},
        {"selector_type": "config_key"},
        {"selector_state": "absent"},
    ):
        with pytest.raises(sqlite3.DatabaseError):
            forge(**changes)
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("DELETE FROM omnivia_engineering_selector_attestations")


# --- a captured (indexed) snapshot ---------------------------------------------------


def _capture_workspace(tmp_path: Path) -> Any:
    opened = sc.Workspace(tmp_path)
    return opened


def test_a_captured_snapshot_is_attested_through_its_indexed_whole_file_digest(
    tmp_path: Path,
) -> None:
    """Storage-level, because the capture header and stream origin are installation
    bound: the stream owner, the installation and the indexed digest all have to
    agree, and a foreign installation is refused."""
    workspace = _capture_workspace(tmp_path)
    try:
        files = {PATH: sc.AUTH_V1, "README.md": sc.README_V1}
        sealed = captured._seal(
            workspace,
            repository_id="erepo-captured",
            stream_id="estream-captured",
            principal_id="capture-owner",
            checkout_id="co-captured",
            snapshot_id="csnap-1",
            files=files,
            base_us=20_000,
        )
        connection = workspace.holder.connection

        def attest(
            *, principal: str = "capture-owner", installation: str = sealed.installation_id,
            digest: str | None = SYMBOL_V1, file_digest: str = sc.AUTH_V1,
            audit_principal: str | None = None, now_us: int = 90_000,
        ) -> dict[str, Any]:
            request = engineering_source.parse_selector_attestation(
                _attestation(
                    "csnap-1",
                    file_digest,
                    repository_id="erepo-captured",
                    stream_id="estream-captured",
                    selector_digest=digest,
                )
            )
            with captured._fenced(workspace):
                audit = captured._audit(
                    connection,
                    principal_id=audit_principal or principal,
                    operation="engineering.selector.attest",
                    now_us=now_us,
                    ref=f"aud-attest-{principal}-{installation}-{file_digest[-6:]}-{now_us}",
                )
                return engineering_source.record_selector_attestation(
                    connection,
                    SimpleNamespace(audit_ref=audit, settled_at_us=now_us),
                    workspace_id=sc.WORKSPACE_ID,
                    principal_id=principal,
                    installation_id=installation,
                    request=request,
                    allocate_identifier=lambda prefix: f"{prefix}-captured-1",
                )

        with pytest.raises(engineering_source.CapturedSourceUnauthorized):
            attest(installation="inst-foreign", now_us=90_001)
        with pytest.raises(engineering_source.SourceStreamForeignPrincipal):
            attest(principal="intruder", now_us=90_002)
        with pytest.raises(engineering_source.SourceConflict):
            attest(file_digest=sc.AUTH_V2, now_us=90_003)
        assert attest()["disposition"] == "recorded"
        assert attest(now_us=90_004)["disposition"] == "already_recorded"

        # The trigger holds the installation binding a second time.
        with pytest.raises(sqlite3.DatabaseError), captured._fenced(workspace):
            audit = captured._audit(
                connection,
                principal_id="capture-owner",
                operation="engineering.selector.attest",
                now_us=90_010,
            )
            connection.execute(
                "INSERT INTO omnivia_engineering_selector_attestations "
                "(workspace_id, attestation_id, installation_id, "
                "producer_principal_id, repository_id, stream_id, snapshot_id, path, "
                "file_digest, selector_type, selector, file_coverage, selector_state, "
                "selector_digest, adapter_id, adapter_version, recorded_at_us, audit_ref) "
                "VALUES (?, 'esat-forged', 'inst-foreign', 'capture-owner', "
                "'erepo-captured', 'estream-captured', 'csnap-1', ?, ?, 'symbol', "
                "'other', 'complete', 'present', ?, 'a', '1', 90010, ?)",
                (sc.WORKSPACE_ID, PATH, sc.AUTH_V1, SYMBOL_V1, audit),
            )
    finally:
        workspace.holder.connection.close()


def test_a_portable_export_drops_installation_bound_attestations(
    workspace: Any, tmp_path: Path
) -> None:
    """An attestation names the attesting installation, so it is excluded like the
    capture headers and stream origins that name one; its snapshot and file index stay."""
    _two_snapshots(workspace)
    record = _record(workspace, [_symbol_dependency()], "portable")
    _attest_pair(workspace, target=SYMBOL_V1)
    assert workspace.status(record, "esnap-b") == "matched"

    workspace.holder.connection.close()
    artifact = tmp_path / "artifact"
    export_portable(workspace.holder.path, artifact, exported_at_us=1_800_000_000_000_000)
    verify_portable(artifact)
    exported = open_database(artifact / "workspace.sqlite", OpenMode.READ_ONLY)
    try:
        assert exported.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_selector_attestations"
        ).fetchone() == (0,)
        assert exported.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_source_events"
        ).fetchone() == (2,)
    finally:
        exported.close()
    workspace.restart()
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_engineering_selector_attestations"
    ).fetchone() == (2,)


def test_ingest_works_for_wire_decoded_read_only_input(workspace: Any) -> None:
    over_the_wire(workspace)
    _two_snapshots(workspace)
    first = _attest(workspace, _attestation("esnap-a", sc.AUTH_V1))
    assert first["disposition"] == "recorded"
    assert _attest(workspace, _attestation("esnap-a", sc.AUTH_V1), key="again")[
        "disposition"
    ] == "already_recorded"
