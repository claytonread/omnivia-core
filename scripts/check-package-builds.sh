#!/usr/bin/env bash
#
# Build and install-check the OmniVia Core package topology (ADR-036, T-0628).
#
# Builds wheels for the four distributions (omnivia-core, omnivia-core-runtime,
# omnivia-core-mcp, omnivia-core-cli) into a temporary wheelhouse, then
# installs each into its own isolated temporary virtual environment and
# imports it:
#
#   a. omnivia-core alone
#   b. omnivia-core-runtime from the wheelhouse
#   c. omnivia-core-mcp from the wheelhouse
#   d. omnivia-core-cli from the wheelhouse
#
# All work happens outside the repository; nothing is written under the repo
# tree, and the workdir is removed on exit (success or failure).
#
# Environment:
#   PYTHON  interpreter to use (default: python3). Must be 3.11 or newer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-python3}"

CORE_DIR="${REPO_ROOT}"
RUNTIME_DIR="${REPO_ROOT}/packages/omnivia-core-runtime"
MCP_DIR="${REPO_ROOT}/packages/omnivia-core-mcp"
CLI_DIR="${REPO_ROOT}/packages/omnivia-core-cli"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/omnivia-core-build-check.XXXXXX")"
WHEELHOUSE="${WORKDIR}/wheelhouse"
mkdir -p "${WHEELHOUSE}"

cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

echo "OmniVia Core package build/install check"
echo "Repository: ${REPO_ROOT}"
echo "Interpreter: $("${PYTHON}" --version 2>&1)"
echo "Workdir: ${WORKDIR}"
echo

if "${PYTHON}" -c "import build" >/dev/null 2>&1; then
  BUILD_MODE="module"
  echo "Build tool: python -m build (module available)"
else
  BUILD_MODE="pip-wheel"
  echo "Build tool gap: the 'build' module is not installed for ${PYTHON}."
  echo "Falling back to: python -m pip wheel --no-deps --no-build-isolation"
fi
echo

build_wheel() {
  local project_dir="$1"
  local project_name
  project_name="$(basename "${project_dir}")"
  echo "--- building ${project_name} ---"
  if [[ "${BUILD_MODE}" == "module" ]]; then
    "${PYTHON}" -m build --wheel --no-isolation --outdir "${WHEELHOUSE}" "${project_dir}"
  else
    "${PYTHON}" -m pip wheel --no-deps --no-build-isolation \
      --wheel-dir "${WHEELHOUSE}" "${project_dir}"
  fi
  echo
}

build_wheel "${CORE_DIR}"
build_wheel "${RUNTIME_DIR}"
build_wheel "${MCP_DIR}"
build_wheel "${CLI_DIR}"

echo "--- wheelhouse contents ---"
ls -1 "${WHEELHOUSE}"
echo

install_and_import() {
  local venv_name="$1"
  local dist_name="$2"
  local import_name="$3"
  local venv_dir="${WORKDIR}/${venv_name}"

  echo "--- ${venv_name}: install ${dist_name}, import ${import_name} ---"
  "${PYTHON}" -m venv "${venv_dir}"
  "${venv_dir}/bin/python" -m pip install --no-index --find-links "${WHEELHOUSE}" "${dist_name}"
  "${venv_dir}/bin/python" -c "
import ${import_name}
print('${import_name}', ${import_name}.__version__, 'OK')
"
  echo
}

install_and_import "venv-core" "omnivia-core" "omnivia_core"
install_and_import "venv-runtime" "omnivia-core-runtime" "omnivia_core_runtime"
install_and_import "venv-mcp" "omnivia-core-mcp" "omnivia_core_mcp"
install_and_import "venv-cli" "omnivia-core-cli" "omnivia_core_cli"

echo "All four distributions built and installed cleanly."
