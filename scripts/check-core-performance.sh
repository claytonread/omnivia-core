#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROFILE="${OMNIVIA_BENCHMARK_PROFILE:-tiny}"
BASELINE_DIR="${OMNIVIA_BENCHMARK_BASELINE_DIR:-${REPO_ROOT}/benchmarks/baselines}"
REPORT_DIR="${OMNIVIA_BENCHMARK_REPORT_DIR:-${REPO_ROOT}/benchmarks/reports}"
WARNING_THRESHOLD="${OMNIVIA_BENCHMARK_WARNING_THRESHOLD:-10}"
FAIL_THRESHOLD="${OMNIVIA_BENCHMARK_FAIL_THRESHOLD:-25}"
FAIL_ON_WARNING="${OMNIVIA_BENCHMARK_FAIL_ON_WARNING:-0}"

mkdir -p "${REPORT_DIR}"

echo "Running Core performance check"
echo "Profile: ${PROFILE}"
echo "Baseline directory: ${BASELINE_DIR}"
echo "Report directory: ${REPORT_DIR}"
echo "Warning threshold: ${WARNING_THRESHOLD}%"
echo "Fail threshold: ${FAIL_THRESHOLD}%"

"${SCRIPT_DIR}/run-core-benchmarks.sh" \
  --profile "${PROFILE}" \
  --format json \
  --output-dir "${REPORT_DIR}" \
  --quiet

LATEST_REPORT="$(
  find "${REPORT_DIR}" -maxdepth 1 -type f -name "benchmark_${PROFILE}_*.json" -print \
    | sort \
    | tail -n 1
)"

if [[ -z "${LATEST_REPORT}" ]]; then
  echo "No benchmark report was generated for profile '${PROFILE}'." >&2
  exit 1
fi

COMPARE_ARGS=(
  -m benchmarks.runner.benchmark_compare
  --profile "${PROFILE}"
  --baseline-dir "${BASELINE_DIR}"
  --latest "${LATEST_REPORT}"
  --latest-dir "${REPORT_DIR}"
  --warning-threshold "${WARNING_THRESHOLD}"
  --fail-threshold "${FAIL_THRESHOLD}"
  --output-dir "${REPORT_DIR}"
  --format all
)

if [[ "${FAIL_ON_WARNING}" == "1" || "${FAIL_ON_WARNING}" == "true" ]]; then
  COMPARE_ARGS+=(--fail-on-warning)
fi

CORE_SRC="${REPO_ROOT}/services/omnivia-memory/src"
if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${CORE_SRC}:${PYTHONPATH}"
else
  export PYTHONPATH="${CORE_SRC}"
fi

cd "${REPO_ROOT}"
python3 "${COMPARE_ARGS[@]}"
