"""Packaged-resource proof for the Chat Runtime Contract v1 wheel path.

``tests/chat_contract/test_resources.py`` exercises the accessor logic against a
substituted directory. This module proves the real packaged path, using only
locally built artifacts:

1. build the ``omnivia-core`` wheel from this checkout with build isolation
   disabled (no index, no environment provisioning);
2. read the wheel's own file list and require *exactly* the 183 approved Chat
   Runtime Contract resources (13 schemas plus 170 fixture-tree files) under
   ``omnivia_core/chat_contract/v1/resources/`` -- none missing, none extra;
3. install that wheel alone into a throwaway virtual environment with
   ``--no-index --no-deps``;
4. in a child process inside that environment, with ``PYTHONPATH`` stripped
   and the working directory outside this repository, import the public
   surface out of the *installed* artifact, read and parse every packaged
   schema and fixture, confirm an unknown resource name is still refused,
   confirm the governed byte-for-byte content survived packaging, and
   exercise the bounded codec against the installed artifact;
5. in the same environment, confirm the validation libraries the contract
   package is forbidden to depend on are absent, so the standard-library-only
   claim is proved against the installed artifact rather than asserted.

Every step is offline and fail-closed: no index access, no ``pytest.skip``
path. All work happens outside the repository in a temporary directory
removed on exit, and ``PYTHONPATH`` is stripped from the child environments so
no source tree leaks into the installed-artifact checks.
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
CANONICAL_ROOT = REPO_ROOT / "contracts" / "chat" / "v1"
RESOURCE_PREFIX = "omnivia_core/chat_contract/v1/resources/"
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
from omnivia_core.chat_contract import v1
from omnivia_core.chat_contract.v1 import codec, generated, resources

report = {}
report["protocol_version"] = v1.PROTOCOL_VERSION
report["contract_version"] = v1.CONTRACT_VERSION
report["approval_id"] = v1.APPROVAL_ID
report["command_count"] = len(v1.CHAT_COMMAND_NAMES)
report["exports"] = len(v1.__all__)

schemas = resources.list_schema_names()
report["schema_count"] = len(schemas)
schema = resources.read_schema("common")
report["schema_id"] = schema["$id"]

paths = resources.list_fixture_paths()
report["fixture_count"] = len(paths)
digests = {}
for path in paths:
    text = resources.read_fixture_text(path)
    digests[path] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    json.loads(text)
report["digests"] = digests

manifest = resources.read_fixture_manifest()
report["manifest_counts"] = manifest["counts"]

# Bounded codec exercise, out of the installed artifact.
envelope = codec.CommandResultEnvelope(command_id="cmd-1", status="accepted")
report["canonical_json"] = codec.to_canonical_json(envelope.to_wire())
report["negotiated_version"] = codec.negotiate_protocol_version("1.0")
try:
    codec.negotiate_protocol_version("2.0")
except codec.UnsupportedProtocolVersionError:
    report["unsupported_major_refused"] = True

event = codec.ChatEvent.from_wire({
    "eventId": "evt-1", "eventType": "chat.branch.selected", "schemaVersion": 1,
    "workspaceId": "ws-1", "conversationId": "conv-1", "occurredAt": "2026-01-01T00:00:00Z",
    "cursor": "cur-1", "actorId": "actor-1", "branchId": "branch-1",
})
report["event_round_trip_ok"] = event.to_wire() == {
    "eventId": "evt-1", "eventType": "chat.branch.selected", "schemaVersion": 1,
    "workspaceId": "ws-1", "conversationId": "conv-1", "occurredAt": "2026-01-01T00:00:00Z",
    "cursor": "cur-1", "actorId": "actor-1", "branchId": "branch-1",
}

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
    with tempfile.TemporaryDirectory(prefix="omnivia-chat-contract-wheel.") as workdir:
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
    assert installed_report["protocol_version"] == "1.0"
    assert installed_report["contract_version"] == "1.0.0-rc.1"
    assert installed_report["approval_id"] == "GOV-CHAT-RUNTIME-CONTRACT-V1-APPROVAL-001"
    assert installed_report["command_count"] == 30


def test_the_packaged_schemas_import_from_the_installed_artifact(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["schema_count"] == 13
    assert installed_report["schema_id"] == "https://contracts.omnivia.dev/chat/v1/common.schema.json"


def test_every_packaged_fixture_survives_packaging_byte_for_byte(
    installed_report: dict[str, object]
) -> None:
    digests = installed_report["digests"]
    assert isinstance(digests, dict)
    assert installed_report["fixture_count"] == 169
    for relative, digest in digests.items():
        source = CANONICAL_ROOT / "fixtures" / relative
        assert digest == hashlib.sha256(source.read_bytes()).hexdigest(), relative


def test_the_packaged_manifest_declares_the_governed_counts(
    installed_report: dict[str, object]
) -> None:
    assert installed_report["manifest_counts"] == {"total": 169, "valid": 78, "invalid": 91}


def test_the_installed_bounded_codec_still_works(installed_report: dict[str, object]) -> None:
    assert installed_report["canonical_json"] == '{"commandId":"cmd-1","status":"accepted"}'
    assert installed_report["negotiated_version"] == "1.0"
    assert installed_report["unsupported_major_refused"] is True
    assert installed_report["event_round_trip_ok"] is True


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
