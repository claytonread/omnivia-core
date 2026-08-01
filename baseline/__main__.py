"""Allow ``python -m baseline`` to run the Phase 0 baseline commands."""

from __future__ import annotations

import sys

from baseline.cli import main

if __name__ == "__main__":
    sys.exit(main())
