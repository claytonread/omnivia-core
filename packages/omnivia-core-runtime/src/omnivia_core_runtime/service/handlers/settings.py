"""Production handlers for ``settings.list`` and ``settings.set``.

One stored JSON value under one namespaced key, scoped to the workspace the
authorization seam already decided this request runs in. The store beneath these
two handlers is `storage.app_settings` (migration 0043), unchanged: the decision
of 2026-10-18 was explicit that persisted settings reuse that table rather than
acquiring a second settings store.

Two things a reader should not have to infer.

**The workspace is the authorized one.** Both handlers read
`context.workspace_id`, which the seam took from the session's grant and the
endpoint binding after refusing every workspace they did not agree on. Neither
payload carries a workspace, by contract.

**`value_json` is canonicalised, not carried verbatim.** The contract's wire type
is a JSON-encoded string, and the store holds a value rather than bytes, so a
submitted document is decoded on the way in and re-encoded on the way out. A
caller therefore gets back a semantically equal document with sorted keys and no
insignificant whitespace, which is the same normalisation every other value in
this workspace goes through. What is *not* normalised is meaning: a payload that
is not valid JSON is refused rather than stored as a string.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    IDENTIFIER_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPEN_CODE_PATTERN,
    ContractDecodeError,
    ContractSemanticError,
    SettingRecord,
    SettingsListInput,
    SettingsListResult,
    SettingsSetInput,
    SettingsSetResult,
    idempotency_equivalence,
    is_timestamp,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.storage.app_settings import (
    AppSetting,
    AppSettingsError,
    list_settings,
    read_setting,
    write_setting_fenced,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator

SETTINGS_LIST_OPERATION: Final = "settings.list"
SETTINGS_SET_OPERATION: Final = "settings.set"
SETTINGS_FAMILY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {SETTINGS_LIST_OPERATION, SETTINGS_SET_OPERATION}
)

#: What the store records as the writing component. A constant rather than the
#: principal: the column names the component that wrote the row, and who asked for
#: it is recorded by the audit event the mutation seam writes in the same
#: transaction. Matches `Identifier`, which the result's `updated_by` must.
SETTINGS_WRITER: Final = "omnivia-core-service"

#: The `SettingValueJson` bound, restated from the schema rather than re-read from
#: it, because the store enforces the same bound on its own column and a payload
#: past it has to be refused as an invalid request rather than as a storage error.
_MAX_VALUE_JSON: Final = 16384

_OPEN_CODE: Final = re.compile(OPEN_CODE_PATTERN)
_OPAQUE_TOKEN: Final = re.compile(OPAQUE_TOKEN_PATTERN)
_IDENTIFIER: Final = re.compile(IDENTIFIER_PATTERN)

_MESSAGE_INVALID: Final = "the request payload is not valid for this settings operation"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"


def _timestamp(value_us: int) -> str:
    moment = datetime.fromtimestamp(value_us / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _record(setting: AppSetting) -> SettingRecord:
    """One stored setting as the contract's record.

    `record_version` is the store's integer rendered as the contract's opaque
    token. Opaque is the operative word: it is returned so a caller that re-reads
    can see the value changed under it, and nothing on this path parses one back.
    """
    return SettingRecord(
        key=setting.key,
        value_json=json.dumps(
            setting.value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ),
        record_version=str(setting.version),
        updated_at=_timestamp(setting.updated_at_us),
        updated_by=setting.updated_by,
    )


def _valid_record(record: SettingRecord) -> bool:
    """Whether a record is one this server will serve.

    Checked against the published patterns rather than trusted, because every
    field but `key` is derived from a stored row: a row written before a bound
    changed, or by a build that is not this one, must be refused rather than
    encoded into a document the caller's decoder rejects. The generated decoders
    check JSON types only, so the patterns are checked here or nowhere.
    """
    return (
        _OPEN_CODE.match(record.key) is not None
        and 0 < len(record.value_json) <= _MAX_VALUE_JSON
        and _OPAQUE_TOKEN.match(record.record_version) is not None
        and is_timestamp(record.updated_at)
        and _IDENTIFIER.match(record.updated_by) is not None
    )


@dataclass(frozen=True)
class SettingsHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator

    def settings_list(self, context: OperationContext) -> Mapping[str, Any]:
        request: SettingsListInput | None = None
        invalid_request = False
        try:
            request = SettingsListInput.from_wire(context.request.input)
            # The store matches a prefix literally, so an unchecked one cannot widen
            # the match; it is checked because the contract publishes the grammar and
            # a payload past it is an invalid request rather than an empty answer.
            if request.prefix is not None and _OPEN_CODE.match(request.prefix) is None:
                raise ContractSemanticError(_MESSAGE_INVALID)
        except (ContractDecodeError, ContractSemanticError):
            invalid_request = True
        if invalid_request:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None

        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        try:
            stored = self._stored(connection, context.workspace_id, request.prefix)
        except AppSettingsError as error:
            # The store refuses a row it cannot decode, which means something wrote
            # settings without going through it. That is this build's problem, not
            # the caller's, and it is not retryable.
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            ) from error

        records = tuple(_record(setting) for setting in stored)
        if any(not _valid_record(record) for record in records):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return SettingsListResult(settings=records).to_wire()

    def _stored(
        self, connection: Any, workspace_id: str, prefix: str | None
    ) -> tuple[AppSetting, ...]:
        """The stored settings, narrowed to one namespace when a prefix is given.

        A prefix is matched on the namespace boundary, not as a bare substring:
        `capture` returns `capture` itself and everything under `capture.`, and
        nothing from `captured`. That is what the contract's "narrows to one
        namespace" means, and it is the narrower of the two readings, so no
        caller can be surprised by an extra key.

        It is also the only reading `OpenCode` can express. The published
        pattern admits no trailing dot, so a caller cannot ask for `capture.`
        and has to name the namespace without it -- which under a bare substring
        match would have pulled in a neighbouring namespace that merely starts
        with the same letters. The schema's own wording is a raw-prefix sentence
        and should gain the boundary clause at the next revision of this
        document; the behaviour here is already the narrower one.

        Two reads rather than one because the store matches a literal prefix and
        the namespace is two disjoint literals: the key itself, and the keys
        below it. Key order survives the concatenation -- `.` sorts below every
        character a key segment may start with, so the exact key precedes every
        key under it.
        """
        if prefix is None:
            return tuple(
                list_settings(connection, workspace_id=workspace_id, prefix="")
            )
        exact = read_setting(connection, workspace_id=workspace_id, key=prefix)
        below = tuple(
            list_settings(connection, workspace_id=workspace_id, prefix=f"{prefix}.")
        )
        return below if exact is None else (exact, *below)

    def settings_set(self, context: OperationContext) -> AuditedOperationResult:
        request: SettingsSetInput | None = None
        value: Any = None
        invalid_request = False
        try:
            request = SettingsSetInput.from_wire(context.request.input)
            if (
                _OPEN_CODE.match(request.key) is None
                or not 0 < len(request.value_json) <= _MAX_VALUE_JSON
            ):
                raise ContractSemanticError(_MESSAGE_INVALID)
            value = json.loads(request.value_json)
        except (ContractDecodeError, ContractSemanticError, ValueError):
            invalid_request = True
        if invalid_request:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None

        if context.authorization is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            # `write_setting_fenced` rather than `write_setting`: the executor is
            # already inside `fenced_transaction`, and the fenced variant writes in
            # that transaction instead of trying to open a second one.
            #
            # `expected_version` is deliberately not passed. The catalogue entry
            # declares no mutation precondition for this operation -- a key with no
            # stored row has no version to cite -- so this is last-writer-wins, and
            # the executor's idempotency scope is what makes a retry safe.
            try:
                stored = write_setting_fenced(
                    fenced,
                    workspace_id=context.workspace_id,
                    key=request.key,
                    value=value,
                    updated_at_us=settlement.settled_at_us,
                    updated_by=SETTINGS_WRITER,
                )
            except AppSettingsError as error:
                # Every bound the store restates has been checked above, so what
                # reaches here is a value this contract admits and that store
                # refuses -- a key with a hyphen cannot, an oversized value cannot.
                raise OperationError(
                    ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID
                ) from error
            return SettingsSetResult(setting=_record(stored)).to_wire()

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = SettingsSetResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return _valid_record(result.setting)

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=valid_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)


__all__ = [
    "SETTINGS_FAMILY_OPERATIONS",
    "SETTINGS_LIST_OPERATION",
    "SETTINGS_SET_OPERATION",
    "SETTINGS_WRITER",
    "SettingsHandlers",
]
