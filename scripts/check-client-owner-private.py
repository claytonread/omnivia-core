"""Run the client owner-private qualification appropriate to this platform."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    arguments = [
        sys.executable,
        "-m",
        "pytest",
        "packages/omnivia-core-client/tests/test_owner_private.py",
        "packages/omnivia-core-client/tests/test_installed_credentials.py",
    ]
    if os.name == "nt":
        # The remaining cases construct POSIX mode-only adversarial fixtures.
        # These four cases use the real Windows owner, DACL, and store paths.
        arguments.extend(("-k", "native_windows"))
    arguments.extend(("-q", "-rs"))
    return subprocess.run(arguments, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
