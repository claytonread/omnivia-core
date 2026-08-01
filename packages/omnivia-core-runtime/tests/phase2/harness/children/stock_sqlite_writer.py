#!/usr/bin/env python3
"""Child: a plain sqlite3 client that imports no OmniVia code at all.

FM-12/13/14 claim a second process using the *stock* SQLite VFS cannot write while
the service owns the workspace. Importing the runtime here would test the runtime's
own guards instead, which is the opposite of the claim.
"""
from __future__ import annotations

import json
import sqlite3
import sys


def main() -> int:
    path = sys.argv[1]
    statement = sys.argv[2]
    timeout_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    result: dict[str, object] = {"statement": statement}
    try:
        connection = sqlite3.connect(path, timeout=timeout_ms / 1000.0)
        try:
            connection.execute(statement)
            connection.commit()
            result["succeeded"] = True
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001
        result["succeeded"] = False
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)

    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
