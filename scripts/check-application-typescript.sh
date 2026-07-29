#!/usr/bin/env bash
#
# Strict, no-emit TypeScript compile gate for the generated Application
# Contract v1 TypeScript module (ADR-038).
#
# The compiler is a repository-local, version-pinned dev dependency: TypeScript
# is declared at an exact version in `package.json` and locked in
# `package-lock.json`, so `npm ci` reproduces the same compiler every time. This
# repository never vendors, nor hard-codes a path into, any sibling repository's
# node_modules, and never requires a global install.
#
# This script resolves a `tsc` binary, in order:
#
#   1. the `TSC` environment variable, if set (explicit override);
#   2. the repository-local `node_modules/.bin/tsc` (the reproducible default);
#   3. `tsc` on PATH (whatever version happens to be installed there).
#
# and fails with a clear message if none resolves, rather than silently
# skipping the check.
#
# Setup and run (from the repository root):
#
#   npm ci
#   npm run check:application-contracts   # or: scripts/check-application-typescript.sh
#
# Environment:
#   TSC  path to a `tsc` binary (default: the repository-local one, then PATH)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET="${REPO_ROOT}/generated/typescript/application/v1/index.ts"
LOCAL_TSC="${REPO_ROOT}/node_modules/.bin/tsc"

if [[ -n "${TSC:-}" ]]; then
  TSC_BIN="${TSC}"
elif [[ -x "${LOCAL_TSC}" ]]; then
  TSC_BIN="${LOCAL_TSC}"
elif command -v tsc >/dev/null 2>&1; then
  TSC_BIN="$(command -v tsc)"
else
  echo "check-application-typescript.sh: no TypeScript compiler found." >&2
  echo "Install the pinned repository-local one with: npm ci" >&2
  echo "Or set TSC=/path/to/tsc explicitly." >&2
  exit 1
fi

if [[ ! -f "${TARGET}" ]]; then
  echo "check-application-typescript.sh: generated artifact is missing: ${TARGET}" >&2
  echo "Regenerate with: python scripts/generate-application-contracts.py" >&2
  exit 1
fi

echo "TypeScript compiler: ${TSC_BIN}"
echo "Target: ${TARGET}"
echo

"${TSC_BIN}" \
  --strict \
  --noEmit \
  --skipLibCheck \
  --target ES2022 \
  --module ESNext \
  --moduleResolution Bundler \
  "${TARGET}"

echo "TypeScript strict compile check passed."
