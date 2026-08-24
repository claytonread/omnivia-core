#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_SRC="${REPO_ROOT}/src"
CORE_SRC="${REPO_ROOT}/services/omnivia-memory/src"
PYTHON_BIN="${OMNIVIA_BENCHMARK_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${REPO_SRC}:${CORE_SRC}:${PYTHONPATH}"
else
  export PYTHONPATH="${REPO_SRC}:${CORE_SRC}"
fi

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" -m benchmarks.runner.benchmark_runner "$@"
