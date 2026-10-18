"""Acceptance for the persisted-settings family: `settings.list`, `settings.set`.

The decision of 2026-10-18 (Option A) published two typed operations for the
Core Settings screen's stored preferences. What this file holds to is the part a
reader cannot take on trust from the two surfaces either side of it.

*The handler is reached, and the store is the one that already existed.* A
`settings.set` through the real dispatcher leaves a row in `omnivia_app_settings`
written by the service writer, and `settings.list` reads that row back. No
second settings store appears.

*The write is one fenced transaction, not two.* `write_setting` opens its own
fence; the mutation executor hands the handler a connection that is already
inside one. The handler therefore calls `write_setting_fenced`, and a nested
fence would raise rather than commit -- which is what the round trip below
proves did not happen.

*The mutation is idempotent and last-writer-wins.* The same key replays the
stored outcome without writing again; a different payload under that key is a
typed conflict. There is no mutation precondition, because a key with no stored
row has no version to cite.

*Every bound the contract publishes is refused at the boundary*, not by the
store's own constraint turning into an internal error: a key outside `OpenCode`,
an oversized value and a value that is not JSON are each `invalid_request`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    SETTINGS_FAMILY_PURPOSES,
    ApplicationDispatcher,
    build_settings_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.settings import (
    SETTINGS_FAMILY_OPERATIONS,
    SETTINGS_WRITER,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage.app_settings import read_setting
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER
from test_app_settings_migration import TABLE

from omnivia_core.contracts.v1 import (
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    OPAQUE_TOKEN_PATTERN,
    OPEN_CODE_PATTERN,
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
    is_timestamp,
)

WORKSPACE_ID = m1.WORKSPACE_ID
SETTLED_AT = datetime.fromtimestamp(1_900_000_000, tz=UTC)


@pytest.fixture
def owned(tmp_path: Path) -> Any:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def _dispatcher(
    owned: Any,
    *,
    allocator_tag: str = "settings",
    clock: FakeClock | None = None,
) -> ApplicationDispatcher:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{allocator_tag}-{counts[prefix]}"

    fallback = Dispatcher.for_service_operations(
        Grant(
            principal=CONFIGURED_LOCAL_OWNER,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )
    return build_settings_application_dispatcher(
        service=owned,
        principal_id=CONFIGURED_LOCAL_OWNER,
        installation_id=s0.INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=fallback,
        clock=FakeClock(wall=SETTLED_AT) if clock is None else clock,
        allocate_identifier=allocate,
    )


def _request(
    operation: str,
    operation_input: dict[str, object],
    *,
    request_id: str,
    idempotency_key: str | None = None,
) -> RequestEnvelope:
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=SETTINGS_FAMILY_PURPOSES[operation],
        workspace_id=WORKSPACE_ID,
    )


def _set(
    dispatcher: ApplicationDispatcher,
    key: str,
    value_json: str,
    *,
    request_id: str,
    idempotency_key: str,
) -> Any:
    return dispatcher.dispatch(
        _request(
            "settings.set",
            {"key": key, "value_json": value_json},
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
    )


def _list(dispatcher: ApplicationDispatcher, prefix: str | None = None) -> Any:
    operation_input: dict[str, object] = {} if prefix is None else {"prefix": prefix}
    return dispatcher.dispatch(
        _request("settings.list", operation_input, request_id="req-list")
    )


def _rows(owned: Any) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in owned.connection.execute(
            f"SELECT setting_key, value_json, version, updated_by FROM {TABLE} "
            "WHERE workspace_id = ? ORDER BY setting_key",
            (WORKSPACE_ID,),
        )
    ]


# --------------------------------------------------------------------------
# The family itself
# --------------------------------------------------------------------------


def test_the_family_grants_exactly_the_two_operations(owned: Any) -> None:
    dispatcher = _dispatcher(owned)
    assert dispatcher.session.operations == SETTINGS_FAMILY_OPERATIONS
    assert dispatcher.registry.operations == SETTINGS_FAMILY_OPERATIONS
    # Both scopes, because the family holds a read and a write. No existing
    # mutating family grants `workspace:write`, which is why this one exists.
    assert dispatcher.session.scopes == frozenset({"workspace:read", "workspace:write"})


# --------------------------------------------------------------------------
# A real write, read back
# --------------------------------------------------------------------------


def test_a_set_persists_through_the_fence_and_lists_back(owned: Any) -> None:
    dispatcher = _dispatcher(owned)
    response = _set(
        dispatcher,
        "backup.retention_days",
        "30",
        request_id="req-set-1",
        idempotency_key="idem-set-1",
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    # A mutation is audited, so the envelope carries the reference the executor
    # wrote in the same transaction as the row.
    assert response.metadata.audit_reference is not None

    setting = response.result["setting"]
    assert setting["key"] == "backup.retention_days"
    assert setting["value_json"] == "30"
    assert setting["record_version"] == "1"
    assert setting["updated_by"] == SETTINGS_WRITER

    # The row is really there, written by the service writer under the fence.
    assert _rows(owned) == [("backup.retention_days", "30", 1, SETTINGS_WRITER)]
    stored = read_setting(
        owned.connection, workspace_id=WORKSPACE_ID, key="backup.retention_days"
    )
    assert stored is not None and stored.value == 30

    listed = _list(dispatcher)
    assert isinstance(listed, SuccessResponseEnvelope), listed
    assert listed.result == {"settings": [setting]}
    # A read is audited but settles nothing, so it carries no audit reference.
    assert listed.metadata.audit_reference is None


def test_every_returned_field_matches_its_published_pattern(owned: Any) -> None:
    """The generated decoders check JSON types only, so this is checked or not at all."""
    dispatcher = _dispatcher(owned)
    response = _set(
        dispatcher, "capture.default_destination", '"inbox"',
        request_id="req-set-pattern", idempotency_key="idem-pattern",
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    setting = response.result["setting"]
    assert re.match(OPEN_CODE_PATTERN, setting["key"])
    assert re.match(OPAQUE_TOKEN_PATTERN, setting["record_version"])
    assert is_timestamp(setting["updated_at"])
    assert 0 < len(setting["value_json"]) <= 16384


def test_a_second_set_advances_the_record_version(owned: Any) -> None:
    dispatcher = _dispatcher(owned)
    first = _set(
        dispatcher, "processing.enabled", "true",
        request_id="req-first", idempotency_key="idem-first",
    )
    second = _set(
        dispatcher, "processing.enabled", "false",
        request_id="req-second", idempotency_key="idem-second",
    )
    assert isinstance(first, SuccessResponseEnvelope)
    assert isinstance(second, SuccessResponseEnvelope)
    assert first.result["setting"]["record_version"] == "1"
    assert second.result["setting"]["record_version"] == "2"
    assert second.result["setting"]["value_json"] == "false"
    assert _rows(owned) == [("processing.enabled", "false", 2, SETTINGS_WRITER)]


def test_a_value_is_canonicalised_rather_than_carried_verbatim(owned: Any) -> None:
    """Object keys come back sorted and the padding is gone.

    The store holds a value, not bytes, so the document is decoded in and
    re-encoded out. Stating it here stops a caller depending on byte equality.
    """
    dispatcher = _dispatcher(owned)
    response = _set(
        dispatcher, "capture.limits", '{ "b": 2,  "a": 1 }',
        request_id="req-canon", idempotency_key="idem-canon",
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result["setting"]["value_json"] == '{"a":1,"b":2}'


def test_a_prefix_narrows_to_one_namespace_on_the_boundary(owned: Any) -> None:
    """`backup` is the namespace, and `backups` is a different one.

    A caller cannot ask for `backup.`: `OpenCode` admits no trailing dot, so the
    namespace is named without one. A bare substring match would then have
    answered with `backups.elsewhere` too, which is why the handler matches on
    the boundary -- and why the exact key `backup` is in the answer.
    """
    dispatcher = _dispatcher(owned)
    keys = ("backup", "backup.schedule", "backup.retention_days", "backups.elsewhere")
    for index, key in enumerate(keys):
        _set(
            dispatcher, key, "1",
            request_id=f"req-prefix-{index}", idempotency_key=f"idem-prefix-{index}",
        )
    listed = _list(dispatcher, "backup")
    assert isinstance(listed, SuccessResponseEnvelope), listed
    assert [entry["key"] for entry in listed.result["settings"]] == [
        "backup",
        "backup.retention_days",
        "backup.schedule",
    ]


def test_nothing_stored_is_an_empty_answer_not_an_error(owned: Any) -> None:
    listed = _list(_dispatcher(owned))
    assert isinstance(listed, SuccessResponseEnvelope), listed
    assert listed.result == {"settings": []}


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_the_same_key_and_payload_replays_without_writing_again(owned: Any) -> None:
    dispatcher = _dispatcher(owned)
    first = _set(
        dispatcher, "processing.enabled", "true",
        request_id="req-replay-1", idempotency_key="idem-replay",
    )
    replay = _set(
        _dispatcher(owned, allocator_tag="replay"), "processing.enabled", "true",
        request_id="req-replay-2", idempotency_key="idem-replay",
    )
    assert isinstance(first, SuccessResponseEnvelope)
    assert isinstance(replay, SuccessResponseEnvelope)
    assert replay.result == first.result
    # Still version 1: the replay served the stored outcome rather than writing.
    assert _rows(owned) == [("processing.enabled", "true", 1, SETTINGS_WRITER)]


def test_a_different_payload_under_the_same_key_is_a_typed_conflict(owned: Any) -> None:
    dispatcher = _dispatcher(owned)
    _set(
        dispatcher, "processing.enabled", "true",
        request_id="req-conflict-1", idempotency_key="idem-conflict",
    )
    conflict = _set(
        _dispatcher(owned, allocator_tag="conflict"), "processing.enabled", "false",
        request_id="req-conflict-2", idempotency_key="idem-conflict",
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert _rows(owned) == [("processing.enabled", "true", 1, SETTINGS_WRITER)]


# --------------------------------------------------------------------------
# Refusals at the boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("Backup.Schedule", "uppercase is outside OpenCode"),
        ("backup-schedule", "the store allows a hyphen; this contract does not"),
        ("9.lives", "a segment must start with a letter"),
        ("", "a key is required"),
    ],
)
def test_a_key_the_contract_refuses_is_an_invalid_request(
    owned: Any, key: str, why: str
) -> None:
    refused = _set(
        _dispatcher(owned), key, "1",
        request_id="req-key", idempotency_key="idem-key",
    )
    assert isinstance(refused, ErrorResponseEnvelope), why
    assert refused.error.code == ERROR_CODE_INVALID_REQUEST, why
    assert _rows(owned) == []


def test_a_value_past_the_published_bound_is_an_invalid_request(owned: Any) -> None:
    oversized = '"' + "x" * 16384 + '"'
    refused = _set(
        _dispatcher(owned), "capture.note", oversized,
        request_id="req-big", idempotency_key="idem-big",
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_INVALID_REQUEST
    assert _rows(owned) == []


def test_a_value_the_contract_admits_and_the_store_refuses_is_invalid(owned: Any) -> None:
    """The two bounds are the same number counted differently.

    The contract bounds `value_json` in characters; the store bounds the stored
    document in UTF-8 bytes. A multibyte value can therefore pass the boundary
    and be refused inside the fence, which is the one path on which the store's
    own error becomes the caller's. It must be `invalid_request` -- the caller
    sent too much -- and it must leave nothing behind.
    """
    multibyte = '"' + "\u00fc" * 16000 + '"'
    assert len(multibyte) <= 16384 < len(multibyte.encode())
    refused = _set(
        _dispatcher(owned), "capture.note", multibyte,
        request_id="req-multibyte", idempotency_key="idem-multibyte",
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_INVALID_REQUEST
    assert _rows(owned) == []


def test_a_value_that_is_not_json_is_an_invalid_request(owned: Any) -> None:
    """The wire type is a JSON-encoded string, so an unencoded one is refused.

    Storing it as the string it happens to be would make the setting's type
    depend on whether the caller remembered to encode.
    """
    refused = _set(
        _dispatcher(owned), "capture.note", "not json",
        request_id="req-raw", idempotency_key="idem-raw",
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_INVALID_REQUEST
    assert _rows(owned) == []


def test_a_prefix_the_contract_refuses_is_an_invalid_request(owned: Any) -> None:
    refused = _list(_dispatcher(owned), "Backup.")
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_INVALID_REQUEST


def test_a_build_with_no_storage_refuses_rather_than_pretending(owned: Any) -> None:
    """A handler with no connection answers an internal fault, not an empty list."""

    class _NoStorage:
        connection = None
        identity = None

    dispatcher = _dispatcher(_NoStorage())
    assert isinstance(_list(dispatcher), ErrorResponseEnvelope)
    assert isinstance(
        _set(
            dispatcher, "a.b", "1",
            request_id="req-nostore", idempotency_key="idem-nostore",
        ),
        ErrorResponseEnvelope,
    )
