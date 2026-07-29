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
# The omnivia-core wheel additionally gets the Application Contract v1 (ADR-038)
# packaging checks:
#
#   - the wheel's file listing must contain *exactly* the canonical schema and
#     fixture files force-included under omnivia_core/contracts/v1/resources
#     (pyproject.toml) -- none missing, none extra -- and no tests/scripts/cache
#     artifact;
#   - the wheel's METADATA must declare no Requires-Dist at all: Core is a
#     standard-library-only distribution;
#   - from an isolated venv in a temporary cwd with PYTHONPATH unset,
#     representative documented public omnivia_core.contracts.v1 symbols must
#     import, and the public resource API must enumerate, read and parse every
#     packaged resource while rejecting any name that is not a packaged one;
#   - that same Core-only environment must not be able to import the sibling,
#     legacy, or validation-library modules Core is forbidden to depend on.
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

CORE_WHEEL="$(ls "${WHEELHOUSE}"/omnivia_core-*.whl)"

echo "--- inspecting omnivia-core wheel resources ---"
"${PYTHON}" - "${CORE_WHEEL}" "${REPO_ROOT}" <<'PYEOF'
import sys
import zipfile
from pathlib import Path

wheel_path, repo_root = sys.argv[1], Path(sys.argv[2])
schemas_dir = repo_root / "contracts" / "application" / "v1" / "schemas"
fixtures_dir = repo_root / "contracts" / "application" / "v1" / "fixtures"

expected_schema_names = {path.name for path in schemas_dir.glob("*.schema.json")}
expected_fixture_names = {path.name for path in fixtures_dir.glob("*.json")}

with zipfile.ZipFile(wheel_path) as archive:
    names = archive.namelist()

resource_prefix = "omnivia_core/contracts/v1/resources/"
packaged_schema_names = {
    Path(name).name for name in names if name.startswith(f"{resource_prefix}schemas/")
}
packaged_fixture_names = {
    Path(name).name for name in names if name.startswith(f"{resource_prefix}fixtures/")
}

# The packaged resource set must be exact in both directions: a missing file breaks a
# documented read, and an extra one ships a resource no canonical source vouches for.
failures = []
for label, expected, packaged in (
    ("schema", expected_schema_names, packaged_schema_names),
    ("fixture", expected_fixture_names, packaged_fixture_names),
):
    missing = sorted(expected - packaged)
    extra = sorted(packaged - expected)
    if missing:
        failures.append(f"missing packaged {label}(s): {missing}")
    if extra:
        failures.append(f"packaged {label}(s) with no canonical source: {extra}")

# Nothing under the resource prefix may be anything but those JSON files.
non_json_resources = sorted(
    name
    for name in names
    if name.startswith(resource_prefix) and not name.endswith("/") and not name.endswith(".json")
)
if non_json_resources:
    failures.append(f"non-JSON file(s) under the resource prefix: {non_json_resources}")

rejected_markers = ("tests/", "scripts/", "__pycache__", ".pyc")
rejected = [name for name in names if any(marker in name for marker in rejected_markers)]
if rejected:
    failures.append(f"wheel contains disallowed test/script/cache artifact(s): {rejected}")

# Core is standard-library only, so its METADATA must carry no Requires-Dist line at all.
metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
if len(metadata_names) != 1:
    failures.append(f"expected exactly one dist-info/METADATA, found {metadata_names}")
else:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_text = archive.read(metadata_names[0]).decode("utf-8")
    requires_dist = [
        line
        for line in metadata_text.splitlines()
        if line.lower().startswith("requires-dist:")
    ]
    if requires_dist:
        failures.append(f"core wheel METADATA declares dependencies: {requires_dist}")

if failures:
    for failure in failures:
        print(failure, file=sys.stderr)
    sys.exit(1)

print(
    f"wheel packages exactly {len(packaged_schema_names)} schema(s) and "
    f"{len(packaged_fixture_names)} fixture file(s), no test/script/cache artifacts, "
    "and no Requires-Dist."
)
PYEOF
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

echo "--- venv-core: documented public API and packaged resources (isolated cwd, PYTHONPATH unset) ---"
CORE_RESOURCE_CWD="${WORKDIR}/core-resource-check-cwd"
mkdir -p "${CORE_RESOURCE_CWD}"
(
  cd "${CORE_RESOURCE_CWD}"
  unset PYTHONPATH
  "${WORKDIR}/venv-core/bin/python" - <<'PYEOF'
# Representative documented public v1 symbols, imported from the one-stop package
# namespace exactly as the public docs tell a caller to import them.
from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    FROZEN_ERROR_CODES,
    SCHEMA_BASE_URI,
    CapabilityRef,
    ContractDecodeError,
    ContractSemanticError,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    VersionWindow,
    classify_version_compatibility,
    decode_response,
    decode_success_response,
    effective_capabilities,
    encode_response,
    is_retryable,
    list_fixture_files,
    list_schema_names,
    read_fixture,
    read_fixture_manifest,
    read_fixture_text,
    read_schema,
    read_schema_text,
    validate_version_capability_envelope,
    version_in_window,
)

assert CONTRACT_VERSION, 'CONTRACT_VERSION is empty'
assert SCHEMA_BASE_URI.startswith('https://'), SCHEMA_BASE_URI
assert 'not_found' in FROZEN_ERROR_CODES
assert version_in_window('1.2', VersionWindow(minimum='1.0', maximum='1.3'))
assert classify_version_compatibility('2.0', VersionWindow(minimum='1.0', maximum='1.3')) == 'incompatible'
assert effective_capabilities(
    (CapabilityRef(id='memory.read', version='1.0'),),
    (CapabilityRef(id='memory.read', version='1.0'),),
)
assert not is_retryable('non_retryable')

schema_names = list_schema_names()
assert schema_names, 'no packaged schemas found'
for name in schema_names:
    document = read_schema(name)
    assert isinstance(document, dict), name
    assert read_schema_text(name), name

fixture_files = list_fixture_files()
assert 'manifest.json' in fixture_files
manifest = read_fixture_manifest()
fixtures = manifest['fixtures']
assert fixtures, 'manifest has no fixtures'
for entry in fixtures:
    assert read_fixture(entry['file']) is not None, entry['file']
    assert read_fixture_text(entry['file']), entry['file']

# A caller-supplied name that is not a packaged one must never reach the filesystem.
for bad_name in ('..', '../common', '/etc/passwd', 'common.schema.json', 'no-such-schema'):
    try:
        read_schema_text(bad_name)
    except ValueError:
        continue
    raise AssertionError(f'read_schema_text accepted {bad_name!r}')
for bad_file in ('..', '../manifest.json', '/etc/passwd', 'no-such-fixture.json'):
    try:
        read_fixture_text(bad_file)
    except ValueError:
        continue
    raise AssertionError(f'read_fixture_text accepted {bad_file!r}')

# Decode a packaged fixture through the documented production codec and round-trip it.
document = read_fixture('compatible-negotiation.json')
envelope = decode_response(document)
assert isinstance(envelope, ResponseEnvelope)
success = decode_success_response(document)
assert isinstance(success, SuccessResponseEnvelope)
validate_version_capability_envelope(success.metadata.version)
assert encode_response(envelope) == document
assert issubclass(ContractSemanticError, ContractDecodeError)

print(
    f'public API OK; read and parsed {len(schema_names)} schema(s) and '
    f'{len(fixture_files)} fixture file(s)'
)
PYEOF
)
echo

echo "--- venv-core: sibling/legacy/validation modules must not be importable ---"
(
  cd "${CORE_RESOURCE_CWD}"
  unset PYTHONPATH
  "${WORKDIR}/venv-core/bin/python" - <<'PYEOF'
import importlib
import sys

# Core installs alone: no sibling distribution, no legacy runtime package, and no
# validation library (jsonschema is a conformance-only dev dependency, never a
# runtime one) may be reachable from a Core-only environment.
forbidden = (
    'omnivia_core_runtime',
    'omnivia_core_mcp',
    'omnivia_core_cli',
    'omnivia_memory',
    'jsonschema',
)
leaked = []
for name in forbidden:
    try:
        importlib.import_module(name)
    except ImportError:
        continue
    leaked.append(name)

if leaked:
    print(f'importable from a Core-only environment: {leaked}', file=sys.stderr)
    raise SystemExit(1)

print(f'none of {list(forbidden)} is importable')
PYEOF
)
echo

echo "All four distributions built and installed cleanly."
