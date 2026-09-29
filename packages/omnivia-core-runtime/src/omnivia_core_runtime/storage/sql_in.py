"""One host-parameter-safe ``IN (...)`` query, shared by every scaled read path.

SQLite's host-parameter ceiling (32 766 on current builds, historically 999) is an
implementation limit, not a design boundary: a 100 000-record workspace crosses it
the first time a frontier-sized id list is folded into a query. The qualification
lane found this twice -- first in the memory frontier's evidence fold, then in the
governed hydration read -- so the fix lives here once, and every caller issues its
id list in fixed chunks and re-sorts the merged rows by the statement's own
ORDER BY keys.

Re-sorting in Python reproduces the unchunked statement's rows in its order
exactly at any list size, because SQLite's BINARY collation on TEXT is code-point
order (and UTF-8 byte order, which the canonical-JSON rules elsewhere in this
package also rely on), the sort columns are non-null keys, and INTEGER order is
INTEGER order. A chunked read therefore answers with byte-identical material to
the single-statement read it replaced, at every scale, and the digest documents
built from those rows stay stable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from typing import Any, Final

#: Small enough to stay far under every SQLite build's ceiling, large enough that
#: a 100 000-id fold costs a bounded number of round trips rather than thousands.
SQL_VARIABLE_CHUNK: Final = 512


def execute_in_rows(
    connection: sqlite3.Connection,
    *,
    select: str,
    where_before: str,
    in_column: str,
    where_after: str = "",
    leading: tuple[object, ...] = (),
    ids: Sequence[str],
    trailing: tuple[object, ...] = (),
    order_key: Callable[[tuple[Any, ...]], tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    """One ``IN (...)`` query issued in host-parameter chunks, merged in order.

    ``select`` carries the projection and every join; ``where_before`` is the
    predicate before the ``IN`` (typically the workspace scope), ``where_after``
    any predicate after it. ``leading`` binds ``where_before``'s parameters,
    ``trailing`` ``where_after``'s, in statement order. The merged rows are
    re-sorted by ``order_key``, which must implement exactly the statement's
    ORDER BY; callers that do not care about order pass any total key over the
    primary identity column. Rows are ``tuple[Any, ...]`` -- exactly what
    ``fetchall`` returns -- so a call site that splats them into a dataclass is
    no looser than it was against the single-statement read it replaced.
    """
    rows: list[tuple[Any, ...]] = []
    for start in range(0, len(ids), SQL_VARIABLE_CHUNK):
        chunk = tuple(ids[start : start + SQL_VARIABLE_CHUNK])
        if not chunk:
            continue
        placeholders = ", ".join("?" for _ in chunk)
        statement = (
            f"{select} WHERE {where_before} "
            f"AND {in_column} IN ({placeholders}) {where_after}"
        )
        rows.extend(
            connection.execute(statement, (*leading, *chunk, *trailing)).fetchall()
        )
    rows.sort(key=order_key)
    return rows
