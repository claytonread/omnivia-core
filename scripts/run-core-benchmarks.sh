#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CORE_SRC="${REPO_ROOT}/services/omnivia-memory/src"

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${CORE_SRC}:${PYTHONPATH}"
else
  export PYTHONPATH="${CORE_SRC}"
fi

cd "${REPO_ROOT}"
exec python3 -m benchmarks.runner.benchmark_runner "$@"
