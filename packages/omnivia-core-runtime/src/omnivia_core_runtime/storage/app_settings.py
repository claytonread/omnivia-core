"""Stored application settings for the Core Settings screen.

A setting is one JSON value under one dotted key, scoped to a workspace. The
table is `omnivia_app_settings` (migration 0043), and like every guarded table
its triggers refuse a write that is not made by the owning service under a held
lease -- so this module is the only supported way in, and a foreign SQLite
client cannot write settings at all.

Reads are deliberately separate from writes: a read works on any connection,
including a `READ_ONLY` one opened while the service is stopped, because the
settings screen must be able to show stored values it cannot currently change.

What this module does not do
----------------------------

It does not know which keys are meaningful. The key's *shape* is enforced by the
schema; which keys exist, what their values mean and whether a value is valid
belong to the feature that owns the control. A settings store that also
validated every feature's value would have to be edited by every feature, which
is the coupling the key/value shape exists to avoid.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError, authorised

__all__ = [
    "AppSetting",
    "AppSettingsError",
    "SCHEMA_VERSION",
    "delete_setting",
    "list_settings",
    "read_setting",
    "write_setting",
]

SCHEMA_VERSION = 1

# Mirrors the CHECK in migration 0043. Kept here as well so a bad key is refused
# with a sentence naming the rule, rather than as a raw constraint failure.
_KEY = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$")
_MAX_KEY = 128
_MAX_VALUE_BYTES = 16384
_WRITER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_MAX_WRITER = 128


class AppSettingsError(StorageError):
    """A settings read or write that the store refused."""


@dataclass(frozen=True, slots=True)
class AppSetting:
    """One stored setting as it currently stands."""

    key: str
    value: Any
    version: int
    updated_at_us: int
    updated_by: str


def _checked_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise AppSettingsError("setting key must be a non-empty string")
    if len(key) > _MAX_KEY:
        raise AppSettingsError(f"setting key must be at most {_MAX_KEY} characters")
    if _KEY.match(key) is None:
        raise AppSettingsError(
            "setting key must be lowercase dotted segments of letters, digits, "
            "'_' or '-', starting with a letter"
        )
    return key


def _checked_writer(updated_by: str) -> str:
    if not isinstance(updated_by, str) or not updated_by:
        raise AppSettingsError("updated_by must name the writing component")
    if len(updated_by) > _MAX_WRITER or _WRITER.match(updated_by) is None:
        raise AppSettingsError(
            f"updated_by must be at most {_MAX_WRITER} characters of letters, "
            "digits, '.', '_', ':' or '-'"
        )
    return updated_by


def _encoded(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AppSettingsError("setting value must be JSON-encodable") from error
    if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise AppSettingsError(
            f"setting value must encode to at most {_MAX_VALUE_BYTES} bytes"
        )
    if "\x00" in encoded:
        raise AppSettingsError("setting value must not contain a NUL character")
    return encoded


def _decoded(key: str, encoded: str) -> Any:
    try:
        return json.loads(encoded)
    except ValueError as error:
        # The column is text and the writer encodes it, so this means the row was
        # written by something that bypassed this module. Say so rather than
        # returning a string that the caller would treat as the value.
        raise AppSettingsError(
            f"stored value for {key!r} is not valid JSON"
        ) from error


def _row(key: str, row: tuple[Any, ...]) -> AppSetting:
    return AppSetting(
        key=key,
        value=_decoded(key, row[0]),
        version=int(row[1]),
        updated_at_us=int(row[2]),
        updated_by=str(row[3]),
    )


def read_setting(
    connection: sqlite3.Connection, *, workspace_id: str, key: str
) -> AppSetting | None:
    """The stored setting, or None when nothing is stored for that key.

    None means "no stored preference, use the product default". It is not an
    error, and callers should not turn it into one.
    """
    checked = _checked_key(key)
    row = connection.execute(
        "SELECT value_json, version, updated_at_us, updated_by "
        "FROM omnivia_app_settings WHERE workspace_id = ? AND setting_key = ?",
        (workspace_id, checked),
    ).fetchone()
    return None if row is None else _row(checked, tuple(row))


def list_settings(
    connection: sqlite3.Connection, *, workspace_id: str, prefix: str = ""
) -> Iterator[AppSetting]:
    """Every stored setting for the workspace, in key order.

    `prefix` narrows to one namespace (``"backup."`` for the backup feature's
    keys). It is matched literally, so a key containing a GLOB metacharacter --
    which the schema forbids anyway -- cannot widen the match.
    """
    if prefix:
        rows = connection.execute(
            "SELECT setting_key, value_json, version, updated_at_us, updated_by "
            "FROM omnivia_app_settings "
            "WHERE workspace_id = ? AND substr(setting_key, 1, ?) = ? "
            "ORDER BY setting_key",
            (workspace_id, len(prefix), prefix),
        )
    else:
        rows = connection.execute(
            "SELECT setting_key, value_json, version, updated_at_us, updated_by "
            "FROM omnivia_app_settings WHERE workspace_id = ? ORDER BY setting_key",
            (workspace_id,),
        )
    for row in rows:
        yield _row(str(row[0]), tuple(row[1:]))


def write_setting(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    key: str,
    value: Any,
    updated_at_us: int,
    updated_by: str,
    expected_version: int | None = None,
) -> AppSetting:
    """Store `value` under `key`, returning the setting as it now stands.

    `expected_version` is optimistic concurrency: pass the version the caller
    read to have a concurrent change refused rather than silently overwritten,
    or None for last-writer-wins. Pass 0 to require that nothing is stored yet.
    """
    checked = _checked_key(key)
    writer = _checked_writer(updated_by)
    encoded = _encoded(value)
    if not isinstance(updated_at_us, int) or updated_at_us <= 0:
        raise AppSettingsError("updated_at_us must be a positive integer")

    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ):
        with authorised(connection, mutations=True):
            current = connection.execute(
                "SELECT version, updated_at_us FROM omnivia_app_settings "
                "WHERE workspace_id = ? AND setting_key = ?",
                (workspace_id, checked),
            ).fetchone()
            found = None if current is None else int(current[0])

            if expected_version is not None and expected_version != (found or 0):
                raise AppSettingsError(
                    f"setting {checked!r} changed since it was read "
                    f"(expected version {expected_version}, found {found or 0})"
                )

            if current is None:
                connection.execute(
                    "INSERT INTO omnivia_app_settings "
                    "(workspace_id, setting_key, value_json, schema_version, "
                    " version, updated_at_us, updated_by) VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (workspace_id, checked, encoded, SCHEMA_VERSION, updated_at_us, writer),
                )
                version = 1
                stamped = updated_at_us
            else:
                # The trigger refuses a regressing timestamp, which a clock that
                # stepped backwards would otherwise turn into a failed write for
                # a user who only clicked a toggle. Hold the previous instant.
                stamped = max(updated_at_us, int(current[1]))
                version = found + 1  # type: ignore[operator]
                connection.execute(
                    "UPDATE omnivia_app_settings SET value_json = ?, version = ?, "
                    "updated_at_us = ?, updated_by = ? "
                    "WHERE workspace_id = ? AND setting_key = ?",
                    (encoded, version, stamped, writer, workspace_id, checked),
                )

    return AppSetting(
        key=checked,
        value=json.loads(encoded),
        version=version,
        updated_at_us=stamped,
        updated_by=writer,
    )


def delete_setting(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    key: str,
) -> bool:
    """Forget any stored value for `key`, returning whether a row was removed.

    Removing the row restores the product default. Deleting a key that is not
    stored is not an error -- the caller asked for "no stored preference" and
    that is already true.
    """
    checked = _checked_key(key)
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ):
        with authorised(connection, mutations=True):
            cursor = connection.execute(
                "DELETE FROM omnivia_app_settings "
                "WHERE workspace_id = ? AND setting_key = ?",
                (workspace_id, checked),
            )
            return cursor.rowcount > 0


def as_mapping(settings: Iterator[AppSetting]) -> Mapping[str, Any]:
    """The plain key/value view, for a caller that wants only the values."""
    return {setting.key: setting.value for setting in settings}
