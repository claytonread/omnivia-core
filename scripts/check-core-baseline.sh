#!/usr/bin/env bash
#
# Verify the OmniVia Core Phase 0 baseline freeze (T-0627).
#
# Runs the baseline drift checks and the baseline test suite against this
# checkout. Exits non-zero on the first failure so it is safe to use as a gate.
#
# Environment:
#   PYTHON  interpreter to use (default: python3). Must be 3.11 or newer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CORE_SRC="${REPO_ROOT}/services/omnivia-memory/src"
PYTHON="${PYTHON:-python3}"

# Prepend the in-tree source so the baseline never describes an editable install
# or a stale worktree shadow.
if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${CORE_SRC}:${PYTHONPATH}"
else
  export PYTHONPATH="${CORE_SRC}"
fi

cd "${REPO_ROOT}"

echo "Verifying OmniVia Core Phase 0 baseline"
echo "Repository: ${REPO_ROOT}"
echo "Interpreter: $("${PYTHON}" --version 2>&1)"
echo

echo "--- baseline drift checks ---"
"${PYTHON}" -m baseline verify

echo
echo "--- baseline test suite ---"
"${PYTHON}" -m pytest baseline/tests -q
