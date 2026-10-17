"""Refusal contracts for ``settings.list`` and ``settings.set`` (Core Settings v2).

The Application Contract publishes stored application settings as two typed operations, and
the CLI and the server-owned mutation maps already name them. Serving them is a separate
decision, so this build recognises each request and refuses it with the contract's
`dependency_unavailable`, the way ``analysis.start`` does. The registry must hold a handler
for every catalogue operation or the service does not start, and a refusal is the one handler
that claims nothing it cannot back.

Recognising a request is not serving it. Neither handler reads or writes storage, issues a
grant, claims an idempotency key or allocates an identifier, so a request that is understood
and one that is malformed differ only in the code they receive. The seam checks a request's
purpose against the session's purposes, not against the operation, so a request that states
the family's other purpose is not told apart here either; a handler that serves
``settings.set`` is policed by the mutation coordinator, which enforces ``MUTATION_PURPOSES``.

What a handler that serves them has to settle first, and this module does not:

* which keys the generic writer may touch. A typed owner such as ``document.settings.update``
  validates the value it stores under its key, and a generic writer over the same table would
  bypass that validation;
* what to do with a stored key that is not an ``OpenCode`` (a hyphen, or a segment that starts
  with a digit), because ``SettingRecord.key`` cannot carry it;
* the value bound. The contract counts characters in ``value_json``; the stored column counts
  bytes, so a value inside the first can be outside the second;
* whether ``value_json`` has to be JSON text. The contract states only its length. The store
  takes a decoded value, re-encodes it canonically, and refuses what does not encode (``NaN``,
  a lone surrogate) and a NUL, so text the contract admits can still be unstorable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, NoReturn

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    ContractSemanticError,
    SettingsListInput,
    SettingsSetInput,
    is_open_code,
)
from omnivia_core_runtime.service.operations import (
    OperationContext,
    OperationError,
    application_refusal,
)

SETTINGS_LIST_OPERATION: Final = "settings.list"
SETTINGS_SET_OPERATION: Final = "settings.set"
SETTINGS_FAMILY_OPERATIONS: Final = frozenset(
    {SETTINGS_LIST_OPERATION, SETTINGS_SET_OPERATION}
)

_MESSAGE_LIST_INVALID: Final = "the request payload is not a valid stored-settings read"
_MESSAGE_SET_INVALID: Final = "the request payload is not a valid stored-setting change"
_MESSAGE_LIST_UNAVAILABLE: Final = (
    "the settings read is understood, but this build does not serve stored application "
    "settings; nothing was read"
)
_MESSAGE_SET_UNAVAILABLE: Final = (
    "the settings change is understood, but this build does not serve stored application "
    "settings; nothing was changed"
)

#: Each operation's declared members. The generated decoder tolerates an unknown field so a
#: newer peer's additive release still decodes; these requests do not, because both schemas
#: close their members and an undeclared one is a request this build cannot have understood.
_LIST_KEYS: Final = frozenset({"prefix"})
_SET_KEYS: Final = frozenset({"key", "value_json"})

#: `SettingValueJson`'s declared bound, in characters.
_VALUE_MAX_LENGTH: Final = 16384


def _invalid(message: str) -> OperationError:
    return application_refusal(ERROR_CODE_INVALID_REQUEST, message)


def settings_list(context: OperationContext) -> NoReturn:
    """Recognise one ``settings.list`` request and refuse it. Reads nothing."""
    payload = context.request.input
    if not isinstance(payload, Mapping) or not set(payload) <= _LIST_KEYS:
        raise _invalid(_MESSAGE_LIST_INVALID)
    try:
        request = SettingsListInput.from_wire(payload)
    except (ContractDecodeError, ContractSemanticError) as error:
        raise _invalid(_MESSAGE_LIST_INVALID) from error
    if request.prefix is not None and not is_open_code(request.prefix):
        raise _invalid(_MESSAGE_LIST_INVALID)
    raise application_refusal(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_LIST_UNAVAILABLE)


def settings_set(context: OperationContext) -> NoReturn:
    """Recognise one ``settings.set`` request and refuse it. Writes nothing."""
    payload = context.request.input
    if not isinstance(payload, Mapping) or not set(payload) <= _SET_KEYS:
        raise _invalid(_MESSAGE_SET_INVALID)
    try:
        request = SettingsSetInput.from_wire(payload)
    except (ContractDecodeError, ContractSemanticError) as error:
        raise _invalid(_MESSAGE_SET_INVALID) from error
    if not is_open_code(request.key) or not 1 <= len(request.value_json) <= _VALUE_MAX_LENGTH:
        raise _invalid(_MESSAGE_SET_INVALID)
    raise application_refusal(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_SET_UNAVAILABLE)


__all__ = [
    "SETTINGS_FAMILY_OPERATIONS",
    "SETTINGS_LIST_OPERATION",
    "SETTINGS_SET_OPERATION",
    "settings_list",
    "settings_set",
]
