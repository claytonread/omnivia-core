"""Exact logical UTF-8 payload metering for bounded storage reads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


class PayloadBudgetExceeded(RuntimeError):
    """A payload would cross the caller's effective source-read byte limit."""


class PayloadLengthMismatch(RuntimeError):
    """Stored/prechecked byte metadata does not match the fetched UTF-8 payload."""


@dataclass(slots=True)
class PayloadReadBudget:
    """Meter logical payload bytes returned by SQLite to this build.

    A length precheck is intentionally separate from ``consume``: callers first
    prove that the complete next payload group fits, then issue the SELECT that
    returns bodies, and finally verify/count each fetched string.
    """

    limit: int
    source_bytes_read: int = 0
    _length_function: str | None = field(default=None, init=False, repr=False)

    @property
    def remaining(self) -> int:
        return self.limit - self.source_bytes_read

    def byte_length_sql(self, connection: sqlite3.Connection, column: str) -> str:
        """Return a trusted SQL expression for one base-table TEXT column."""
        encoding = connection.execute("PRAGMA encoding").fetchone()
        if encoding is None or str(encoding[0]).upper().replace("-", "") != "UTF8":
            raise PayloadLengthMismatch("the database encoding is not UTF-8")
        if self._length_function is None:
            try:
                probe = connection.execute("SELECT octet_length(?)", ("é",)).fetchone()
            except sqlite3.OperationalError:
                probe = None
            self._length_function = (
                "octet_length" if probe is not None and int(probe[0]) == 2 else "blob"
            )
        if self._length_function == "octet_length":
            return f"octet_length({column})"
        return f"length(CAST({column} AS BLOB))"

    def precheck(self, lengths: list[int] | tuple[int, ...]) -> None:
        if any(type(length) is not int or length < 0 for length in lengths):
            raise PayloadLengthMismatch("a payload byte length is invalid")
        if sum(lengths) > self.remaining:
            raise PayloadBudgetExceeded("the payload group exceeds the remaining budget")

    def consume(self, payload: str, expected_bytes: int) -> None:
        actual = len(payload.encode("utf-8"))
        if actual != expected_bytes:
            raise PayloadLengthMismatch("a fetched payload changed after its byte precheck")
        self.precheck([actual])
        self.source_bytes_read += actual


__all__ = [
    "PayloadBudgetExceeded",
    "PayloadLengthMismatch",
    "PayloadReadBudget",
]
