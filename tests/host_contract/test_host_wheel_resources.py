"""Packaged-resource proof for the Host Contract v1 wheel path.

``tests/host_contract/test_host_resources.py`` exercises the accessor logic against
a substituted directory. This module proves the real packaged path, using only
locally built artifacts:

1. build the ``omnivia-core`` wheel from this checkout with build isolation
   disabled (no index, no environment provisioning);
2. read the wheel's own file list and require *exactly* the twenty-one approved
   Host Contract resources under
   ``omnivia_core/host_contract/v1/resources/`` -- none missing, none extra,
   nothing that is not one of those JSON files;
3. install that wheel alone into a throwaway virtual environment with
   ``--no-index --no-deps``;
4. in a child process inside that environment, with ``PYTHONPATH`` stripped and
   the working directory outside this repository, import the public surface out
   of the *installed* artifact, read and parse every packaged schema and
   fixture, confirm an unknown resource name is still refused, and confirm the
   governed byte-for-byte content survived packaging;
5. in the same environment, confirm the validation libraries the contract
   package is forbidden to depend on are absent, so the standard-library-only
   claim is proved against the installed artifact rather than asserted.

Every step is offline and fail-closed: no index access, no ``pytest.skip``
path. All work happens outside the repository in a temporary directory removed
on exit, and ``PYTHONPATH`` is stripped from the child environments so no
source tree leaks into the installed-artifact checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ROOT = REPO_ROOT / "contracts" / "host" / "v1"
RESOURCE_PREFIX = "omnivia_core/host_contract/v1/resources/"
PYTHON = sys.executable


def _child_env() -> dict[str, str]:
    """Drop ``PYTHONPATH`` so a child sees only its own environment."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _module_available(name: str) -> bool:
    result = subprocess.run([PYTHON, "-c", f"import {name}"], capture_output=True, check=False)
    return result.returncode == 0


def _build_core_wheel(wheelhouse: Path) -> Path:
    before = set(wheelhouse.glob("*.whl"))
    if _module_available("build"):
        command = [
            PYTHON, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(wheelhouse), str(REPO_ROOT),
        ]
    else:
        command = [
            PYTHON, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(wheelhouse), str(REPO_ROOT),
        ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=300, check=False, env=_child_env()
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    (built,) = set(wheelhouse.glob("*.whl")) - before
    return built


#: Emitted inside the throwaway environment. Anything that fails raises, so a
#: broken packaged path surfaces as a non-zero exit with a traceback.
_PROBE = r"""
import hashlib, json
from dataclasses import replace
from omnivia_core.host_contract import v1
from omnivia_core.host_contract.v1 import codec, registration, resources

report = {}
report["version"] = v1.HOST_CONTRACT_VERSION
report["approval"] = v1.HOST_CONTRACT_APPROVAL_ID
report["namespaces"] = len(v1.HOST_NAMESPACES)
report["records"] = len(v1.HOST_CONTRACT_RECORDS)
report["exports"] = len(v1.__all__)

schemas = resources.list_schema_names()
report["schemas"] = list(schemas)
schema = resources.read_schema("host-contract-v1")
report["schema_id"] = schema["$id"]

paths = resources.list_fixture_paths()
report["fixture_count"] = len(paths)
digests = {}
for path in paths:
    text = resources.read_fixture_text(path)
    digests[path] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    json.loads(text)
report["digests"] = digests
report["expectations"] = len(resources.FIXTURE_EXPECTATIONS)

# Direct implementation-module imports expose no retired caller-mintable trust
# object, old admission class or process-global admission-token registry.
codec_retired = (
    "_mint_operation_admission",
    "OperationAdmission",
    "_OPERATION_ADMISSIONS",
)
registration_retired = (
    "_mint_registration_admission",
    "RegistrationAdmission",
    "_REGISTRATION_ADMISSIONS",
    "register_contributions",
)
report["retired_trust_surface_absent"] = (
    all(not hasattr(codec, name) for name in codec_retired)
    and all(not hasattr(registration, name) for name in registration_retired)
    and all("mint" not in name.casefold() and "admission" not in name.casefold()
            for module in (codec, registration) for name in vars(module))
)

def published(operation_id):
    return codec.PublishedOperation(
        namespace="workspace",
        operation_id=operation_id,
        request_schema_id="schema.workspace.get-active.request.v1",
        result_schema_id="schema.workspace.get-active.result.v1",
        effect_class="read",
        idempotency_required=False,
        required_capabilities=(),
        minimum_contract_minor=0,
    )

release_identity = v1.ReleasePackageIdentity.from_wire(
    resources.read_fixture("valid/release-package-identity.json")
)
verified_release = registration.RegistrationEvidence(
    release_identity=release_identity,
    verified_module_id="module.documents",
    verified_module_release_id=release_identity.release_id,
    recomputed_manifest_sha256=release_identity.manifest_sha256,
    recomputed_payload_sha256=release_identity.payload_sha256,
    publisher_verified=True,
    verified_publisher_id=release_identity.publisher_id,
    signing_key_verified=True,
    verified_signing_key_id=release_identity.signing_key_id,
    host_contract_version="1.0.0",
)

def new_bound_host(operation_id):
    return v1.BoundHost(
        operation_catalogue=codec.OperationCatalogue(operations=(published(operation_id),)),
        contribution_registry=registration.ContributionRegistry(
            available_payload_schema_ids=frozenset({"schema.documents.route.v1"}),
            available_capabilities=frozenset({"navigation.register", "diagnostics.trace"}),
            declared_entitlements=frozenset({"documents.use"}),
            host_target="desktop",
            trusted_publisher_ids=frozenset({release_identity.publisher_id}),
            trusted_signing_key_ids=frozenset({release_identity.signing_key_id}),
        ),
        registration_evidence=(verified_release,),
    )

authentic = new_bound_host("get_active")
caller_owned = new_bound_host("caller_operation")

# Every packaged fixture still decodes, denies or is refused as its governed
# expectation says, out of the installed artifact rather than the source tree.
request_document = resources.read_fixture("valid/host-request.json")
report["structural_request_namespace"] = v1.decode_host_request(request_document).namespace
request = authentic.admit_request(request_document)
report["request_namespace"] = request.namespace
result = v1.decode_host_result(resources.read_fixture("valid/host-result.json"))
report["result_branch"] = v1.result_branch(result)

hostile_v2 = resources.read_fixture("valid/host-request.json")
hostile_v2["contractVersion"] = "2.0.0"
try:
    v1.decode_host_request(hostile_v2)
except v1.HostContractDecodeError:
    report["v2_refused"] = True

hostile_negative = resources.read_fixture("valid/host-request.json")
hostile_negative["expectedStateVersion"] = -1
try:
    v1.decode_host_request(hostile_negative)
except v1.HostContractDecodeError:
    report["negative_refused"] = True

try:
    v1.encode_host_request(replace(request, expected_state_version=-1))
except v1.HostContractDecodeError:
    report["constructed_request_refused"] = True

try:
    v1.encode_host_result(replace(result, data=None))
except v1.HostContractDecodeError:
    report["constructed_result_refused"] = True

try:
    authentic.admit_request(resources.read_fixture("invalid/unknown-namespace.json"))
except v1.HostContractDecodeError as error:
    report["unknown_namespace"] = "unsupported_contract_value" in str(error)

unknown_operation = resources.read_fixture("valid/host-request.json")
unknown_operation["operation"] = "not_in_the_installed_catalogue"
try:
    authentic.admit_request(unknown_operation)
except v1.HostContractDecodeError as error:
    report["unknown_operation"] = "unsupported_contract_value" in str(error)

caller_request = resources.read_fixture("valid/host-request.json")
caller_request["operation"] = "caller_operation"
caller_admitted = caller_owned.admit_request(caller_request)
try:
    authentic.admit_request(caller_request)
except v1.HostContractDecodeError:
    authentic_refused_caller_operation = True
else:
    authentic_refused_caller_operation = False

declaration = v1.ModuleContributionDeclaration.from_wire(
    resources.read_fixture("valid/module-contribution.json")
)
caller_registration = caller_owned.register(declaration)
authentic_registration = authentic.register(declaration)
report["separate_boundary_isolated"] = (
    caller_owned is not authentic
    and caller_admitted.operation == "caller_operation"
    and authentic_refused_caller_operation
    and caller_registration.status == "accepted"
    and authentic_registration.status == "accepted"
)
report["registration_status"] = authentic_registration.status
report["registration_activated"] = list(authentic_registration.activated)

try:
    resources.read_fixture_text("valid/not-packaged.json")
except ValueError:
    report["unknown_name_refused"] = True

try:
    resources.read_schema_text("../../../etc/passwd")
except ValueError:
    report["traversal_refused"] = True

forbidden = []
for name in ("jsonschema", "referencing", "sqlalchemy", "omnivia_memory", "omnivia_core_runtime"):
    try:
        __import__(name)
    except ImportError:
        continue
    forbidden.append(name)
report["forbidden_importable"] = forbidden

print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def installed_report() -> dict[str, object]:
    """Build, package-inspect and install the Core wheel, then probe it."""
    with tempfile.TemporaryDirectory(prefix="omnivia-host-contract-wheel.") as workdir:
        root = Path(workdir)
        wheelhouse = root / "wheelhouse"
        wheelhouse.mkdir()
        wheel = _build_core_wheel(wheelhouse)

        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        packaged = sorted(
            name.removeprefix(RESOURCE_PREFIX)
            for name in names
            if name.startswith(RESOURCE_PREFIX) and not name.endswith("/")
        )
        expected = sorted(
            path.relative_to(CANONICAL_ROOT).as_posix() for path in CANONICAL_ROOT.rglob("*.json")
        )
        assert packaged == expected, f"packaged {packaged!r}, expected {expected!r}"

        env_dir = root / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        install = subprocess.run(
            [
                str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                "--disable-pip-version-check", str(wheel),
            ],
            capture_output=True, text=True, timeout=300, check=False, env=_child_env(),
        )
        assert install.returncode == 0, f"install failed:\n{install.stdout}\n{install.stderr}"

        probe_path = root / "probe.py"
        probe_path.write_text(_PROBE, encoding="utf-8")
        probe = subprocess.run(
            [str(python), str(probe_path)],
            capture_output=True, text=True, timeout=120, check=False,
            cwd=str(root), env=_child_env(),
        )
        assert probe.returncode == 0, f"probe failed:\n{probe.stdout}\n{probe.stderr}"
        report: dict[str, object] = json.loads(probe.stdout)
        return report


def test_the_installed_package_reports_the_governed_contract_identity(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["version"] == "1.0.0"
    assert installed_report["approval"] == "GOV-HOST-CONTRACT-APPROVAL-001"
    assert installed_report["namespaces"] == 17
    assert installed_report["records"] == 15


def test_the_installed_package_exposes_its_whole_public_surface(
    installed_report: dict[str, object]
) -> None:
    from omnivia_core.host_contract import v1

    assert installed_report["exports"] == len(v1.__all__)


def test_the_packaged_schema_imports_from_the_installed_artifact(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["schemas"] == ["host-contract-v1"]
    assert installed_report["schema_id"] == "urn:omnivia:host-contract:1.0.0"


def test_every_packaged_fixture_survives_packaging_byte_for_byte(
    installed_report: dict[str, object]
) -> None:
    digests = installed_report["digests"]
    assert isinstance(digests, dict)
    assert installed_report["fixture_count"] == 20
    for relative, digest in digests.items():
        source = CANONICAL_ROOT / "fixtures" / relative
        assert digest == hashlib.sha256(source.read_bytes()).hexdigest(), relative


def test_the_packaged_expectations_table_covers_every_packaged_fixture(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["expectations"] == installed_report["fixture_count"]


def test_the_installed_contract_still_decodes_and_fails_closed(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["structural_request_namespace"] == "workspace"
    assert installed_report["request_namespace"] == "workspace"
    assert installed_report["result_branch"] == "data"
    assert installed_report["unknown_namespace"] is True
    assert installed_report["v2_refused"] is True
    assert installed_report["negative_refused"] is True
    assert installed_report["constructed_request_refused"] is True
    assert installed_report["constructed_result_refused"] is True
    assert installed_report["unknown_operation"] is True


def test_the_installed_modules_expose_no_retired_caller_trust_surface(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["retired_trust_surface_absent"] is True


def test_a_separate_installed_boundary_cannot_alter_or_impersonate_the_authentic_host(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["separate_boundary_isolated"] is True


def test_the_installed_bound_host_accepts_a_valid_contribution_flow(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["registration_status"] == "accepted"
    assert installed_report["registration_activated"] == ["documents.route.home"]


def test_the_installed_resource_accessors_still_refuse_an_unknown_name(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["unknown_name_refused"] is True
    assert installed_report["traversal_refused"] is True


def test_the_installed_environment_has_no_validation_or_sibling_dependency(
    installed_report: dict[str, object]
) -> None:
    """Core is standard-library only; the isolated install proves it."""
    assert installed_report["forbidden_importable"] == []
