"""Coverage for the cross-platform Standard candidate matrix composer.

Every candidate here is a small synthetic directory tree, not a real build:
the real candidate builder is expensive and out of scope for this lane.  Each
test builds just enough of the checksum-verified shape the composer requires,
then breaks exactly one thing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER = REPO_ROOT / "scripts" / "compose-standard-compatibility-matrix.py"

_SYSTEMS = {
    "linux": {"machine": "x86_64", "runner": "ubuntu-latest"},
    "darwin": {"machine": "arm64", "runner": "macos-latest"},
    "windows": {"machine": "amd64", "runner": "windows-latest"},
}
_FIRST_PARTY = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-client",
    "omnivia-core-cli",
    "omnivia-core-mcp",
)
_REVISION = "deadbeef00000000000000000000000000000000"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("standard_matrix_composer", COMPOSER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _matrix_rows(system: str) -> list[dict[str, str]]:
    """The three rows `build-standard-candidate.py` writes on `system`."""
    return [
        {
            "system": name,
            "runner": profile["runner"],
            "python": "3.11",
            "status": "passed_in_this_candidate" if name == system else "required_in_separate_matrix_row",
        }
        for name, profile in _SYSTEMS.items()
    ]


def _versions(**overrides: Any) -> dict[str, Any]:
    versions: dict[str, Any] = {
        "first_party_distributions": {name: "1.0.0" for name in _FIRST_PARTY},
        "core": "1.0.0",
        "client": "1.0.0",
        "workspace_format": "1",
        "workspace_contract": "1",
        "protocol": "1",
        "api": {"minimum": "1", "maximum": "1"},
    }
    versions.update(overrides)
    return versions


def _reseal(directory: Path, line_ending: str = "\n") -> None:
    """Rewrite the checksum index so it matches whatever is on disk now."""
    checksum_path = directory / "checksums-sha256.txt"
    checksum_path.unlink(missing_ok=True)
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    lines = [f"{_digest(path.read_bytes())}  {path.relative_to(directory).as_posix()}" for path in files]
    checksum_path.write_text(line_ending.join(lines) + line_ending, encoding="utf-8", newline="")


def _mutate_metadata(directory: Path, name: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Edit one metadata file after the fact and reseal, so only content differs."""
    path = directory / "metadata" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    _write_json(path, document)
    _reseal(directory)


def _align_candidate_keys(directory: Path) -> None:
    """Copy the manifest's candidate_key into the other two metadata files.

    Used by tests that tamper with the manifest wheel list: without this the
    candidate-key recomputation fires first and hides the check under test.
    """
    manifest = json.loads((directory / "metadata" / "release-manifest.json").read_text(encoding="utf-8"))
    key = manifest["candidate_key"]
    for name in ("compatibility-matrix.json", "qualification-result.json"):
        _mutate_metadata(directory, name, lambda document: document.update({"candidate_key": key}))


def _write_candidate(
    directory: Path,
    *,
    system: str,
    revision: str = _REVISION,
    manifest_revision: str | None = None,
    epoch: int = 1700000000,
    dirty: bool = False,
    candidate_key: str | None = None,
    versions: dict | None = None,
    signed: bool = False,
    eligible: bool = False,
    skips: int = 0,
    xfails: int = 0,
    verdict: str = "pass",
    machine: str | None = None,
    runner: str | None = None,
    line_ending: str = "\n",
    extra_wheel_content: bytes = b"",
    wheel_version_overrides: dict[str, str] | None = None,
) -> None:
    """Build one checksum-verified synthetic candidate directory."""
    profile = _SYSTEMS[system]
    machine = machine if machine is not None else profile["machine"]
    runner = runner if runner is not None else profile["runner"]
    python_version = "3.11.9"
    versions = versions if versions is not None else _versions()

    directory.mkdir(parents=True)
    metadata = directory / "metadata"
    metadata.mkdir()
    wheels = directory / "wheels"
    wheels.mkdir()

    host = {
        "system": system,
        "machine": machine,
        "kernel_release": "1",
        "kernel_version": "1",
        "python_implementation": "CPython",
        "python_version": python_version,
        "sysconfig_platform": f"{system}-{machine}",
        "platform_description": f"{system}-{machine}-1",
        "product": {"source": "test", "name": "", "version": "", "build": ""},
    }

    overrides = wheel_version_overrides or {}
    wheel_entries = []
    for name in (*_FIRST_PARTY, "third-party-dependency"):
        # Platform-distinct bytes by default -- first-party wheel SHA-256
        # values are never compared across rows, only name/version must
        # agree with this row's own first_party_distributions mapping.
        content = name.encode() + system.encode() + extra_wheel_content
        version = overrides.get(name, "1.0.0")
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        (wheels / filename).write_bytes(content)
        wheel_entries.append(
            {
                "name": name,
                "version": version,
                "path": f"wheels/{filename}",
                "sha256": _digest(content),
                "bytes": len(content),
                "first_party": name in _FIRST_PARTY,
            }
        )

    # Derived exactly the way `scripts/build-standard-candidate.py` derives it.
    key = (
        candidate_key
        if candidate_key is not None
        else _digest("\n".join(str(entry["sha256"]) for entry in wheel_entries).encode())
    )

    _write_json(
        metadata / "build-provenance.json",
        {
            "format": "omnivia.standard-build-provenance.v1",
            "source": {"revision": revision, "dirty": dirty, "source_date_epoch": epoch},
            "host": host,
        },
    )
    _write_json(
        metadata / "compatibility-matrix.json",
        {
            "format": "omnivia.standard-compatibility-matrix.v1",
            "candidate_key": key,
            "rows": _matrix_rows(system),
            "current": {
                "system": system,
                "machine": machine,
                "python": python_version,
                "runner": runner,
                "host": host,
                "journey": "pass",
                "versions": versions,
            },
            "complete_matrix_claimed": False,
        },
    )
    _write_json(
        metadata / "qualification-result.json",
        {
            "format": "omnivia.standard-candidate-qualification.v1",
            "verdict": verdict,
            "candidate_key": key,
            "profile": list(_FIRST_PARTY),
            "wheel_count": len(wheel_entries),
            "offline_install": True,
            "skips": skips,
            "xfails": xfails,
        },
    )
    _write_json(
        metadata / "release-manifest.json",
        {
            "format": "omnivia.standard-release-manifest.v1",
            "candidate_status": "qualification_only",
            "candidate_key": key,
            "source_revision": manifest_revision if manifest_revision is not None else revision,
            "production_release_eligible": eligible,
            "profile": list(_FIRST_PARTY),
            "wheels": wheel_entries,
            "signing": {"status": "signed" if signed else "unsigned"},
        },
    )
    _write_json(
        metadata / "signature-verification.json",
        {
            "format": "omnivia.signature-verification.v1",
            "status": "signed" if signed else "unsigned",
            "verification_performed": signed,
            "production_release_eligible": eligible,
        },
    )

    _reseal(directory, line_ending)


def _write_three(tmp_path: Path, **overrides: dict) -> list[Path]:
    directories = []
    for system in ("linux", "darwin", "windows"):
        directory = tmp_path / f"candidate-{system}"
        _write_candidate(directory, system=system, **overrides.get(system, {}))
        directories.append(directory)
    return directories


def _write_one_broken(tmp_path: Path, system: str, break_it: Callable[[Path], None]) -> list[Path]:
    """Build a valid three-candidate set, then break exactly one directory."""
    directories = _write_three(tmp_path)
    broken = directories[("linux", "darwin", "windows").index(system)]
    break_it(broken)
    return directories


def test_compose_succeeds_with_shuffled_order_and_is_byte_identical(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)

    forward = module.compose(list(directories))
    shuffled = module.compose([directories[2], directories[0], directories[1]])

    assert json.dumps(forward, sort_keys=True) == json.dumps(shuffled, sort_keys=True)
    assert forward["format"] == "omnivia.standard-compatibility-matrix-composed.v1"
    assert forward["qualification_matrix"]["complete_matrix_claimed"] is True
    assert forward["signing"] == {"status": "unsigned", "verification_performed": False}
    assert forward["production_release_eligible"] is False
    rows = forward["qualification_matrix"]["rows"]
    systems = [row["system"] for row in rows]
    assert sorted(systems) == ["darwin", "linux", "windows"]
    assert "first_party_wheels" not in forward
    for row in rows:
        assert len(row["first_party_wheels"]) == 5
        assert {wheel["name"] for wheel in row["first_party_wheels"]} == set(_FIRST_PARTY)
    # Wheel bytes are platform-distinct by construction; each row keeps its
    # own SHA-256 values rather than being forced to agree with the others.
    windows_row = next(row for row in rows if row["system"] == "windows")
    linux_row = next(row for row in rows if row["system"] == "linux")
    windows_hashes = {wheel["name"]: wheel["sha256"] for wheel in windows_row["first_party_wheels"]}
    linux_hashes = {wheel["name"]: wheel["sha256"] for wheel in linux_row["first_party_wheels"]}
    assert windows_hashes != linux_hashes


def test_compose_output_is_identical_for_every_input_order(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)

    documents = [json.dumps(module.compose(list(order)), sort_keys=True) for order in itertools.permutations(directories)]

    assert len(set(documents)) == 1
    keys = {json.loads(document)["candidate_set_key"] for document in documents}
    assert len(keys) == 1


def test_compose_accepts_platform_distinct_wheel_hashes_with_agreeing_identities(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, windows={"extra_wheel_content": b"extra"})

    rows = module.compose(list(directories))["qualification_matrix"]["rows"]

    identities = [{(wheel["name"], wheel["version"]) for wheel in row["first_party_wheels"]} for row in rows]
    assert identities[0] == identities[1] == identities[2]
    assert len(identities[0]) == 5
    digests = [tuple(wheel["sha256"] for wheel in sorted(row["first_party_wheels"], key=lambda w: w["name"])) for row in rows]
    assert len(set(digests)) == 3


def test_compose_accepts_crlf_checksum_index(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, windows={"line_ending": "\r\n"})

    document = module.compose(list(directories))

    assert {row["system"] for row in document["qualification_matrix"]["rows"]} == {
        "linux",
        "darwin",
        "windows",
    }


def test_compose_refuses_a_missing_platform_row(tmp_path: Path) -> None:
    module = _module()
    linux_dir = tmp_path / "candidate-linux"
    _write_candidate(linux_dir, system="linux")
    darwin_dir = tmp_path / "candidate-darwin"
    _write_candidate(darwin_dir, system="darwin")
    darwin_dir_2 = tmp_path / "candidate-darwin-2"
    _write_candidate(darwin_dir_2, system="darwin")

    with pytest.raises(module.ComposeError, match="exactly one linux"):
        module.compose([linux_dir, darwin_dir, darwin_dir_2])


def test_compose_refuses_a_duplicate_platform_row(tmp_path: Path) -> None:
    module = _module()
    linux_dir = tmp_path / "candidate-linux"
    _write_candidate(linux_dir, system="linux")
    linux_dir_2 = tmp_path / "candidate-linux-2"
    _write_candidate(linux_dir_2, system="linux")
    windows_dir = tmp_path / "candidate-windows"
    _write_candidate(windows_dir, system="windows")

    with pytest.raises(module.ComposeError, match="exactly one linux"):
        module.compose([linux_dir, linux_dir_2, windows_dir])


def test_compose_refuses_the_same_candidate_directory_twice(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)

    with pytest.raises(module.ComposeError, match="distinct and non-overlapping"):
        module.compose([directories[0], directories[0], directories[2]])


def test_compose_refuses_nested_candidate_directories(tmp_path: Path) -> None:
    module = _module()
    outer = tmp_path / "outer"
    _write_candidate(outer, system="linux")
    nested = outer / "nested"
    _write_candidate(nested, system="darwin")
    windows_dir = tmp_path / "candidate-windows"
    _write_candidate(windows_dir, system="windows")

    with pytest.raises(module.ComposeError, match="distinct and non-overlapping"):
        module.compose([outer, nested, windows_dir])


def test_compose_refuses_a_candidate_path_that_is_not_a_directory(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("nope\n", encoding="utf-8")

    with pytest.raises(module.ComposeError, match="not a candidate directory"):
        module.compose([directories[0], directories[1], not_a_directory])


def test_compose_refuses_mixed_source_revisions(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"revision": "otherrevision"})

    with pytest.raises(module.ComposeError, match="same source revision"):
        module.compose(list(directories))


def test_compose_refuses_mixed_source_epochs(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, darwin={"epoch": 1})

    with pytest.raises(module.ComposeError, match="same source revision"):
        module.compose(list(directories))


def test_compose_refuses_a_manifest_source_revision_that_contradicts_provenance(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"manifest_revision": "a" * 40})

    with pytest.raises(module.ComposeError, match="source_revision does not match build-provenance"):
        module.compose(list(directories))


def test_compose_refuses_a_missing_manifest_source_revision(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "windows", lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m.pop("source_revision"))
    )

    with pytest.raises(module.ComposeError, match="source_revision is missing"):
        module.compose(list(directories))


def test_compose_refuses_a_contradicting_source_date_epoch_outside_provenance(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "qualification-result.json", lambda q: q.update({"source_date_epoch": 1})),
    )

    with pytest.raises(module.ComposeError, match="source_date_epoch does not match build-provenance"):
        module.compose(list(directories))


def test_compose_refuses_a_dirty_build(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, windows={"dirty": True})

    with pytest.raises(module.ComposeError, match="dirty"):
        module.compose(list(directories))


def test_compose_refuses_a_candidate_key_that_is_not_the_manifest_wheel_digest(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"candidate_key": "0" * 64})

    with pytest.raises(module.ComposeError, match="candidate_key does not match the digest"):
        module.compose(list(directories))


def test_compose_refuses_inconsistent_candidate_keys(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m.update({"candidate_key": "1" * 64})),
    )

    with pytest.raises(module.ComposeError, match="candidate_key does not match the digest"):
        module.compose(list(directories))


@pytest.mark.parametrize("bad_key", [["not", "a", "string"], {"key": "value"}, 12345, None, "SHORT", "A" * 64])
def test_compose_refuses_a_malformed_candidate_key_without_a_traceback(tmp_path: Path, bad_key: object) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m.update({"candidate_key": bad_key})),
    )

    with pytest.raises(module.ComposeError, match="candidate_key is not a lowercase"):
        module.compose(list(directories))


def test_compose_refuses_a_manifest_sha256_that_disagrees_with_the_artifact(tmp_path: Path) -> None:
    module = _module()

    def _swap_digest(document: dict[str, Any]) -> None:
        document["wheels"][0]["sha256"] = "b" * 64
        # Keep the candidate key consistent with the tampered manifest so the
        # digest binding, not the key recomputation, is what fails.
        document["candidate_key"] = _digest(
            "\n".join(str(entry["sha256"]) for entry in document["wheels"]).encode()
        )

    directories = _write_one_broken(
        tmp_path, "linux", lambda d: _mutate_metadata(d, "release-manifest.json", _swap_digest)
    )
    _align_candidate_keys(directories[0])

    with pytest.raises(module.ComposeError, match="sha256 for wheels/.* does not match the verified artifact"):
        module.compose(list(directories))


def test_compose_refuses_an_uppercase_manifest_sha256(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][0].update({"sha256": m["wheels"][0]["sha256"].upper()})
        ),
    )

    with pytest.raises(module.ComposeError, match="sha256 is not a lowercase"):
        module.compose(list(directories))


def test_compose_refuses_a_manifest_wheel_path_absent_from_the_index(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][0].update({"path": "wheels/ghost.whl"})
        ),
    )

    with pytest.raises(module.ComposeError, match="is not in checksums-sha256.txt"):
        module.compose(list(directories))


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/etc/passwd",
        "../escape.whl",
        "wheels/../../escape.whl",
        "C:\\evil.whl",
        "wheels\\core.whl",
        "",
        "wheels/core\x00.whl",
        "wheels/core\x1f.whl",
        "wheels/core\x7f.whl",
    ],
)
def test_compose_refuses_an_unsafe_manifest_wheel_path(tmp_path: Path, unsafe_path: str) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][0].update({"path": unsafe_path})
        ),
    )

    with pytest.raises(module.ComposeError, match="unsafe path|path is missing"):
        module.compose(list(directories))


def test_compose_refuses_duplicate_manifest_wheel_paths(tmp_path: Path) -> None:
    module = _module()

    def _duplicate_path(document: dict[str, Any]) -> None:
        document["wheels"][1]["path"] = document["wheels"][0]["path"]
        document["wheels"][1]["sha256"] = document["wheels"][0]["sha256"]
        document["candidate_key"] = _digest(
            "\n".join(str(entry["sha256"]) for entry in document["wheels"]).encode()
        )

    directories = _write_one_broken(
        tmp_path, "darwin", lambda d: _mutate_metadata(d, "release-manifest.json", _duplicate_path)
    )
    _align_candidate_keys(directories[1])

    with pytest.raises(module.ComposeError, match="duplicate release-manifest.json wheel path"):
        module.compose(list(directories))


def test_compose_refuses_a_manifest_wheel_size_mismatch(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][0].update({"bytes": m["wheels"][0]["bytes"] + 1})
        ),
    )

    with pytest.raises(module.ComposeError, match="bytes does not match the artifact"):
        module.compose(list(directories))


@pytest.mark.parametrize("bad_size", [True, -1, 1.0, "12", [12]])
def test_compose_refuses_a_manifest_wheel_size_that_is_not_a_size(tmp_path: Path, bad_size: object) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][0].update({"bytes": bad_size})
        ),
    )

    with pytest.raises(module.ComposeError, match="bytes is not a size"):
        module.compose(list(directories))


def test_compose_refuses_an_invented_first_party_wheel_identity(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][0].update({"name": "omnivia-core-invented"})
        ),
    )

    with pytest.raises(module.ComposeError, match="first-party wheel set does not match"):
        module.compose(list(directories))


def test_compose_refuses_a_duplicate_first_party_wheel_name(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(
            d, "release-manifest.json", lambda m: m["wheels"][1].update({"name": m["wheels"][0]["name"]})
        ),
    )

    with pytest.raises(module.ComposeError, match="duplicate first-party wheel entry"):
        module.compose(list(directories))


@pytest.mark.parametrize("field", ["name", "version"])
def test_compose_refuses_a_non_string_manifest_wheel_field(tmp_path: Path, field: str) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m["wheels"][0].update({field: {"x": 1}})),
    )

    with pytest.raises(module.ComposeError, match="is missing or not a non-empty string"):
        module.compose(list(directories))


def test_compose_refuses_an_empty_manifest_wheel_list(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "linux", lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m.update({"wheels": []}))
    )

    with pytest.raises(module.ComposeError, match="wheels is malformed"):
        module.compose(list(directories))


def test_compose_refuses_a_provenance_current_mismatch(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(
            d, "compatibility-matrix.json", lambda m: m["current"].update({"machine": "mismatched-machine"})
        ),
    )

    with pytest.raises(module.ComposeError, match="current.machine"):
        module.compose(list(directories))


@pytest.mark.parametrize("field", ["system", "machine", "python"])
def test_compose_refuses_an_unhashable_current_scalar_without_a_traceback(tmp_path: Path, field: str) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m["current"].update({field: {}})),
    )

    with pytest.raises(module.ComposeError, match="is missing or not a non-empty string"):
        module.compose(list(directories))


def test_compose_refuses_a_wrong_runner_selector(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"runner": "windows-latest"})

    with pytest.raises(module.ComposeError, match="expected selector"):
        module.compose(list(directories))


def test_compose_refuses_divergent_compatibility_versions(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, darwin={"versions": _versions(protocol="2")})

    with pytest.raises(module.ComposeError, match="divergent compatibility versions"):
        module.compose(list(directories))


@pytest.mark.parametrize(
    ("left", "right"),
    [(1, True), (1, 1.0), (0, False), ("1", 1), (1.0, True)],
)
def test_compose_refuses_json_type_ambiguous_versions(tmp_path: Path, left: object, right: object) -> None:
    module = _module()
    directories = _write_three(
        tmp_path,
        linux={"versions": _versions(protocol=left)},
        darwin={"versions": _versions(protocol=right)},
        windows={"versions": _versions(protocol=left)},
    )

    with pytest.raises(module.ComposeError, match="divergent compatibility versions"):
        module.compose(list(directories))


def test_compose_refuses_a_missing_first_party_distribution(tmp_path: Path) -> None:
    module = _module()
    incomplete = _versions(
        first_party_distributions={name: "1.0.0" for name in _FIRST_PARTY if name != "omnivia-core-mcp"}
    )
    directories = _write_three(tmp_path, linux={"versions": incomplete})

    with pytest.raises(module.ComposeError, match="first_party_distributions does not match"):
        module.compose(list(directories))


def test_compose_refuses_a_malformed_first_party_distributions_mapping(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, windows={"versions": _versions(first_party_distributions=["omnivia-core"])})

    with pytest.raises(module.ComposeError, match="first_party_distributions is malformed"):
        module.compose(list(directories))


def test_compose_refuses_first_party_wheel_version_mismatch(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, windows={"wheel_version_overrides": {"omnivia-core": "1.0.1"}})

    with pytest.raises(module.ComposeError, match="omnivia-core.*does not match"):
        module.compose(list(directories))


def test_compose_refuses_a_failed_qualification(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"verdict": "fail"})

    with pytest.raises(module.ComposeError, match="verdict"):
        module.compose(list(directories))


def test_compose_refuses_nonzero_skips(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, darwin={"skips": 1})

    with pytest.raises(module.ComposeError, match="skips"):
        module.compose(list(directories))


def test_compose_refuses_nonzero_xfails(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, windows={"xfails": 1})

    with pytest.raises(module.ComposeError, match="xfails"):
        module.compose(list(directories))


def test_compose_refuses_a_signed_input(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"signed": True})

    with pytest.raises(module.ComposeError, match="signing"):
        module.compose(list(directories))


def test_compose_refuses_an_eligible_input(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path, darwin={"eligible": True})

    with pytest.raises(module.ComposeError, match="eligibility"):
        module.compose(list(directories))


@pytest.mark.parametrize(
    ("name", "marker"),
    [
        ("build-provenance.json", "omnivia.standard-build-provenance.v1"),
        ("compatibility-matrix.json", "omnivia.standard-compatibility-matrix.v1"),
        ("qualification-result.json", "omnivia.standard-candidate-qualification.v1"),
        ("release-manifest.json", "omnivia.standard-release-manifest.v1"),
        ("signature-verification.json", "omnivia.signature-verification.v1"),
    ],
)
def test_compose_requires_every_metadata_format_marker(tmp_path: Path, name: str, marker: str) -> None:
    module = _module()
    expected = re.escape(f"{name} format is not {marker}")
    directories = _write_one_broken(
        tmp_path, "linux", lambda d: _mutate_metadata(d, name, lambda doc: doc.update({"format": f"{marker}x"}))
    )

    with pytest.raises(module.ComposeError, match=expected):
        module.compose(list(directories))

    # An absent marker is refused exactly like a wrong one.
    _mutate_metadata(directories[0], name, lambda doc: doc.pop("format"))
    with pytest.raises(module.ComposeError, match=expected):
        module.compose(list(directories))


@pytest.mark.parametrize("status", ["release", "qualification-only", "", None, True])
def test_compose_refuses_a_manifest_candidate_status_other_than_qualification_only(
    tmp_path: Path, status: object
) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m.update({"candidate_status": status})),
    )

    with pytest.raises(module.ComposeError, match="candidate_status is not 'qualification_only'"):
        module.compose(list(directories))


def test_compose_refuses_a_manifest_without_a_candidate_status(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m.pop("candidate_status")),
    )

    with pytest.raises(module.ComposeError, match="candidate_status is not 'qualification_only'"):
        module.compose(list(directories))


@pytest.mark.parametrize("name", ["release-manifest.json", "qualification-result.json"])
@pytest.mark.parametrize(
    "profile",
    [
        pytest.param([*_FIRST_PARTY[:4], _FIRST_PARTY[0]], id="duplicate"),
        pytest.param(list(_FIRST_PARTY[:4]), id="missing"),
        pytest.param([*_FIRST_PARTY, "omnivia-core-extra"], id="extra"),
        pytest.param([*_FIRST_PARTY[:4], 1], id="non-string"),
        pytest.param([*_FIRST_PARTY[:4], ["omnivia-core-mcp"]], id="nested"),
        pytest.param(list(reversed(_FIRST_PARTY)) + ["omnivia-core"], id="six-with-duplicate"),
        pytest.param({name: True for name in _FIRST_PARTY}, id="mapping"),
        pytest.param(",".join(_FIRST_PARTY), id="string"),
        pytest.param(None, id="null"),
    ],
)
def test_compose_refuses_a_profile_that_is_not_the_five_first_party_names(
    tmp_path: Path, name: str, profile: object
) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "linux", lambda d: _mutate_metadata(d, name, lambda doc: doc.update({"profile": profile}))
    )

    with pytest.raises(module.ComposeError, match=f"{re.escape(name)} profile is not the five Standard"):
        module.compose(list(directories))


@pytest.mark.parametrize("name", ["release-manifest.json", "qualification-result.json"])
def test_compose_refuses_an_absent_profile(tmp_path: Path, name: str) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "windows", lambda d: _mutate_metadata(d, name, lambda doc: doc.pop("profile"))
    )

    with pytest.raises(module.ComposeError, match=f"{re.escape(name)} profile is not the five Standard"):
        module.compose(list(directories))


@pytest.mark.parametrize("offline", [False, None, 1, "true", "yes", []])
def test_compose_refuses_a_qualification_that_is_not_offline_installed(tmp_path: Path, offline: object) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "qualification-result.json", lambda q: q.update({"offline_install": offline})),
    )

    with pytest.raises(module.ComposeError, match="offline_install is not exactly true"):
        module.compose(list(directories))


def test_compose_refuses_a_qualification_without_offline_install(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(d, "qualification-result.json", lambda q: q.pop("offline_install")),
    )

    with pytest.raises(module.ComposeError, match="offline_install is not exactly true"):
        module.compose(list(directories))


@pytest.mark.parametrize("wheel_count", [5, 7, 0, -6, 6.0, "6", None, [6]])
def test_compose_refuses_a_wheel_count_that_is_not_the_manifest_wheel_count(
    tmp_path: Path, wheel_count: object
) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(d, "qualification-result.json", lambda q: q.update({"wheel_count": wheel_count})),
    )

    with pytest.raises(module.ComposeError, match="wheel_count is not the manifest wheel count"):
        module.compose(list(directories))


def test_compose_refuses_a_boolean_wheel_count_even_when_it_equals_the_count(tmp_path: Path) -> None:
    module = _module()
    # `True == 1`, so a one-wheel manifest with `wheel_count: true` would pass a
    # plain equality check.  The type must be an int, not a bool that compares
    # equal to one.
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(d, "qualification-result.json", lambda q: q.update({"wheel_count": True})),
    )

    with pytest.raises(module.ComposeError, match="wheel_count is not the manifest wheel count"):
        module.compose(list(directories))


def test_compose_refuses_a_manifest_wheel_without_a_byte_count(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "release-manifest.json", lambda m: m["wheels"][0].pop("bytes")),
    )

    with pytest.raises(module.ComposeError, match="bytes is not a size"):
        module.compose(list(directories))


@pytest.mark.parametrize("claim", [True, None, "false", 0, []])
def test_compose_refuses_a_candidate_that_claims_a_complete_matrix(tmp_path: Path, claim: object) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(
            d, "compatibility-matrix.json", lambda m: m.update({"complete_matrix_claimed": claim})
        ),
    )

    with pytest.raises(module.ComposeError, match="complete_matrix_claimed is not exactly false"):
        module.compose(list(directories))


@pytest.mark.parametrize("journey", ["fail", "PASS", "", None, True])
def test_compose_refuses_a_current_row_whose_journey_did_not_pass(tmp_path: Path, journey: object) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "windows",
        lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m["current"].update({"journey": journey})),
    )

    with pytest.raises(module.ComposeError, match="current.journey is not 'pass'"):
        module.compose(list(directories))


def test_compose_refuses_a_matrix_without_a_current_journey(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m["current"].pop("journey")),
    )

    with pytest.raises(module.ComposeError, match="current.journey is not 'pass'"):
        module.compose(list(directories))


def _rows_replaced(mutate: Callable[[list[dict[str, Any]]], Any]) -> Callable[[dict[str, Any]], None]:
    def _apply(document: dict[str, Any]) -> None:
        rows = document["rows"]
        document["rows"] = mutate(rows)

    return _apply


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("dropped", lambda rows: rows[:2]),
        ("duplicated", lambda rows: [*rows, rows[0]]),
        ("extra", lambda rows: [*rows, {**rows[0], "system": "freebsd", "runner": "freebsd-latest"}]),
        ("wrong-python", lambda rows: [{**row, "python": "3.12"} for row in rows]),
        ("wrong-runner", lambda rows: [{**row, "runner": "self-hosted"} for row in rows]),
        ("all-passed", lambda rows: [{**row, "status": "passed_in_this_candidate"} for row in rows]),
        ("none-passed", lambda rows: [{**row, "status": "required_in_separate_matrix_row"} for row in rows]),
        ("extra-key", lambda rows: [{**row, "waived": True} for row in rows]),
        ("empty", lambda rows: []),
        ("not-a-list", lambda rows: {row["system"]: row for row in rows}),
        ("not-mappings", lambda rows: [row["system"] for row in rows]),
        ("nulls", lambda rows: [None, None, None]),
    ],
)
def test_compose_refuses_matrix_rows_that_are_not_the_three_required_rows(
    tmp_path: Path, label: str, mutate: Callable[[list[dict[str, Any]]], Any]
) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "linux", lambda d: _mutate_metadata(d, "compatibility-matrix.json", _rows_replaced(mutate))
    )

    with pytest.raises(module.ComposeError, match="rows are not the three required Python 3.11 platform rows"):
        module.compose(list(directories))


def test_compose_refuses_a_matrix_row_set_that_credits_the_wrong_platform(tmp_path: Path) -> None:
    module = _module()
    # A linux candidate carrying the darwin candidate's row set: every row is
    # individually well formed, and only the placement of the pass is wrong.
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m.update({"rows": _matrix_rows("darwin")})),
    )

    with pytest.raises(module.ComposeError, match="rows are not the three required Python 3.11 platform rows"):
        module.compose(list(directories))


def test_compose_accepts_matrix_rows_in_any_order(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    _mutate_metadata(
        directories[2], "compatibility-matrix.json", lambda m: m.update({"rows": list(reversed(m["rows"]))})
    )

    assert module.compose(list(directories))["format"] == "omnivia.standard-compatibility-matrix-composed.v1"


def test_compose_refuses_a_matrix_without_rows(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "windows", lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m.pop("rows"))
    )

    with pytest.raises(module.ComposeError, match="rows are not the three required Python 3.11 platform rows"):
        module.compose(list(directories))


def test_compose_refuses_a_checksum_mismatch(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "linux", lambda d: (d / "wheels" / "omnivia_core-1.0.0-py3-none-any.whl").write_bytes(b"tampered")
    )

    with pytest.raises(module.ComposeError, match="checksum mismatch"):
        module.compose(list(directories))


def test_compose_refuses_an_unindexed_file(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path, "linux", lambda d: (d / "wheels" / "unexpected.whl").write_bytes(b"surprise")
    )

    with pytest.raises(module.ComposeError, match="unindexed"):
        module.compose(list(directories))


def test_compose_refuses_a_malformed_checksum_line(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: (d / "checksums-sha256.txt").write_text("not-a-valid-checksum-line\n", encoding="utf-8"),
    )

    with pytest.raises(module.ComposeError, match="malformed"):
        module.compose(list(directories))


def test_compose_refuses_a_duplicate_checksum_path(tmp_path: Path) -> None:
    module = _module()

    def _duplicate_line(directory: Path) -> None:
        checksum_path = directory / "checksums-sha256.txt"
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
        checksum_path.write_text("\n".join(lines + [lines[0]]) + "\n", encoding="utf-8")

    directories = _write_one_broken(tmp_path, "linux", _duplicate_line)

    with pytest.raises(module.ComposeError, match="duplicate checksum path"):
        module.compose(list(directories))


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/etc/passwd",
        "../escape.txt",
        "wheels/../../escape.txt",
        "C:\\evil.txt",
        "wheels\\core.whl",
        "wheels/core\x00.txt",
        "wheels/core\x01.txt",
        "wheels/core\x1f.txt",
        "wheels/core\x7f.txt",
    ],
)
def test_compose_refuses_an_unsafe_checksum_path(tmp_path: Path, unsafe_path: str) -> None:
    module = _module()

    def _append_unsafe(directory: Path) -> None:
        checksum_path = directory / "checksums-sha256.txt"
        existing = checksum_path.read_text(encoding="utf-8")
        checksum_path.write_text(existing + f"{'0' * 64}  {unsafe_path}\n", encoding="utf-8")

    directories = _write_one_broken(tmp_path, "linux", _append_unsafe)

    with pytest.raises(module.ComposeError, match="unsafe path"):
        module.compose(list(directories))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevation on Windows")
def test_compose_refuses_a_symlinked_directory_that_escapes_the_candidate_root(tmp_path: Path) -> None:
    module = _module()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")

    def _link_outside(directory: Path) -> None:
        # Only the intermediate component is a symlink, so the indexed target
        # itself still looks like an ordinary regular file.
        (directory / "smuggled").symlink_to(outside, target_is_directory=True)
        checksum_path = directory / "checksums-sha256.txt"
        existing = checksum_path.read_text(encoding="utf-8")
        checksum_path.write_text(existing + f"{_digest(b'secret')}  smuggled/secret.txt\n", encoding="utf-8")

    directories = _write_one_broken(tmp_path, "linux", _link_outside)

    with pytest.raises(module.ComposeError, match="escapes the candidate root"):
        module.compose(list(directories))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevation on Windows")
def test_compose_refuses_an_unindexed_symlink(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "linux",
        lambda d: (d / "wheels" / "alias.whl").symlink_to(d / "wheels" / "omnivia_core-1.0.0-py3-none-any.whl"),
    )

    with pytest.raises(module.ComposeError, match="candidate tree contains a symlink: wheels/alias.whl"):
        module.compose(list(directories))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevation on Windows")
def test_compose_refuses_an_indexed_symlink(tmp_path: Path) -> None:
    module = _module()
    target = "wheels/omnivia_core-1.0.0-py3-none-any.whl"

    def _index_a_symlink(directory: Path) -> None:
        (directory / "wheels" / "alias.whl").symlink_to(directory / target)
        checksum_path = directory / "checksums-sha256.txt"
        digest = _digest((directory / target).read_bytes())
        checksum_path.write_text(
            checksum_path.read_text(encoding="utf-8") + f"{digest}  wheels/alias.whl\n", encoding="utf-8"
        )

    directories = _write_one_broken(tmp_path, "darwin", _index_a_symlink)

    with pytest.raises(module.ComposeError, match="not a regular file|contains a symlink"):
        module.compose(list(directories))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevation on Windows")
def test_compose_refuses_a_symlinked_directory_inside_the_candidate_root(tmp_path: Path) -> None:
    module = _module()
    # Points back inside the candidate, so nothing escapes the root: the
    # symlink itself is the thing refused, not where it happens to lead.
    directories = _write_one_broken(
        tmp_path, "windows", lambda d: (d / "mirror").symlink_to(d / "wheels", target_is_directory=True)
    )

    with pytest.raises(module.ComposeError, match="candidate tree contains a symlinked directory: mirror"):
        module.compose(list(directories))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevation on Windows")
def test_compose_refuses_a_symlinked_checksum_index(tmp_path: Path) -> None:
    module = _module()

    def _link_the_index(directory: Path) -> None:
        checksum_path = directory / "checksums-sha256.txt"
        real = tmp_path / "real-checksums.txt"
        real.write_bytes(checksum_path.read_bytes())
        checksum_path.unlink()
        checksum_path.symlink_to(real)

    directories = _write_one_broken(tmp_path, "linux", _link_the_index)

    with pytest.raises(module.ComposeError, match="checksums-sha256.txt is absent or not a regular file"):
        module.compose(list(directories))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX only")
def test_compose_refuses_a_special_file_below_the_candidate_root(tmp_path: Path) -> None:
    module = _module()
    directories = _write_one_broken(tmp_path, "darwin", lambda d: os.mkfifo(d / "wheels" / "pipe"))

    with pytest.raises(module.ComposeError, match="candidate tree contains a non-regular file: wheels/pipe"):
        module.compose(list(directories))


def test_compose_accepts_an_ordinary_nested_directory(tmp_path: Path) -> None:
    module = _module()
    # A real directory is the one non-file entry a candidate may contain, and
    # the builder emits several (metadata/, wheels/, licenses/<pkg>/).
    directories = _write_three(tmp_path)
    (directories[0] / "licenses" / "omnivia-core-suite").mkdir(parents=True)
    (directories[0] / "licenses" / "omnivia-core-suite" / "LICENSE").write_bytes(b"MIT\n")
    _reseal(directories[0])

    assert module.compose(list(directories))["format"] == "omnivia.standard-compatibility-matrix-composed.v1"


@pytest.mark.parametrize(
    "raw",
    [
        "wheels\\core.whl",
        "back\\slash",
        "wheels/core\x00.whl",
        "wheels/core\nx.whl",
        "wheels/core\rx.whl",
        "wheels/core\x0bx.whl",
        "wheels/core\x0cx.whl",
        "wheels/core\x1cx.whl",
        "wheels/core\x1fx.whl",
        "wheels/core\x7f.whl",
        "\x00",
    ],
)
def test_safe_relative_path_refuses_backslash_control_and_nul_paths(tmp_path: Path, raw: str) -> None:
    # Exercised through the helper: a path holding a newline or a vertical tab
    # cannot survive a round trip through the line-oriented checksum index, and
    # NUL bytes are not portably creatable on disk either.
    module = _module()

    with pytest.raises(module.ComposeError, match="unsafe path"):
        module._safe_relative_path(raw, tmp_path, "the checksum index")


def test_output_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "matrix.json"
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(module.ComposeError, match="already exists"):
        module._write_output(document, output, force=False, candidate_dirs=directories)

    module._write_output(document, output, force=True, candidate_dirs=directories)
    assert json.loads(output.read_text(encoding="utf-8")) == document


@pytest.mark.parametrize("relative", ["matrix.json", "metadata/matrix.json", "wheels/../matrix.json"])
def test_output_inside_a_candidate_root_is_refused_even_with_force(tmp_path: Path, relative: str) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = directories[0] / relative

    with pytest.raises(module.ComposeError, match="refusing to write over input evidence"):
        module._write_output(document, output, force=True, candidate_dirs=directories)

    assert not output.exists()


def test_cli_refuses_an_output_inside_a_candidate_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    output = directories[1] / "metadata" / "release-manifest.json"
    before = output.read_bytes()

    exit_code = module.main([str(d) for d in directories] + ["--output", str(output), "--force"])

    assert exit_code == 1
    assert "refusing to write over input evidence" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_output_contains_no_local_path(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)

    document = module.compose(list(directories))

    serialized = json.dumps(document)
    assert str(tmp_path) not in serialized
    for directory in directories:
        assert str(directory) not in serialized


def test_cli_composes_and_writes_atomically(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    output = tmp_path / "out" / "matrix.json"
    output.parent.mkdir()

    exit_code = module.main([str(directories[1]), str(directories[2]), str(directories[0]), "--output", str(output)])

    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["format"] == "omnivia.standard-compatibility-matrix-composed.v1"
    # No stray temp file left behind beside the output.
    assert sorted(p.name for p in output.parent.iterdir()) == ["matrix.json"]


def test_cli_reports_a_concise_error_without_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _module()
    directories = _write_three(tmp_path, linux={"dirty": True})
    output = tmp_path / "matrix.json"

    exit_code = module.main([str(d) for d in directories] + ["--output", str(output)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "dirty" in captured.err
    assert not output.exists()


def _racing_tempfile(module: ModuleType, hook: Callable[[str], None]) -> SimpleNamespace:
    """A `tempfile` stand-in that runs `hook` once the temporary file exists."""
    real_mkstemp = module.tempfile.mkstemp

    def _mkstemp(**keywords: Any) -> tuple[int, str]:
        descriptor, name = real_mkstemp(**keywords)
        hook(name)
        return descriptor, name

    return SimpleNamespace(mkstemp=_mkstemp)


def test_output_never_clobbers_a_destination_created_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "matrix.json"
    # A second composer publishes after this one has already decided to write,
    # which is exactly the window an exists()-then-replace would lose.
    monkeypatch.setattr(
        module, "tempfile", _racing_tempfile(module, lambda _name: output.write_text("winner\n", encoding="utf-8"))
    )

    with pytest.raises(module.ComposeError, match="already exists"):
        module._write_output(document, output, force=False, candidate_dirs=directories)

    assert output.read_text(encoding="utf-8") == "winner\n"
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == ["matrix.json"]


def test_output_overwrites_a_concurrently_created_destination_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "matrix.json"
    monkeypatch.setattr(
        module, "tempfile", _racing_tempfile(module, lambda _name: output.write_text("loser\n", encoding="utf-8"))
    )

    module._write_output(document, output, force=True, candidate_dirs=directories)

    assert json.loads(output.read_text(encoding="utf-8")) == document
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == ["matrix.json"]


def test_output_leaves_no_temporary_file_behind_on_the_no_clobber_path(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "out" / "matrix.json"
    output.parent.mkdir()

    module._write_output(document, output, force=False, candidate_dirs=directories)

    assert json.loads(output.read_text(encoding="utf-8")) == document
    assert sorted(path.name for path in output.parent.iterdir()) == ["matrix.json"]


def test_output_refuses_an_absent_output_directory(tmp_path: Path) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "missing" / "matrix.json"

    with pytest.raises(module.ComposeError, match="the output directory .* does not exist"):
        module._write_output(document, output, force=True, candidate_dirs=directories)

    assert not output.parent.exists()


def test_output_reports_a_write_failure_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "matrix.json"
    real_mkstemp = module.tempfile.mkstemp

    def _dead_descriptor(**keywords: Any) -> tuple[int, str]:
        descriptor, name = real_mkstemp(**keywords)
        os.close(descriptor)
        return descriptor, name

    monkeypatch.setattr(module, "tempfile", SimpleNamespace(mkstemp=_dead_descriptor))

    with pytest.raises(module.ComposeError, match="writing the composed matrix failed"):
        module._write_output(document, output, force=False, candidate_dirs=directories)

    assert not output.exists()
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == []


def test_output_reports_a_temporary_file_creation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))

    def _refuse(**_keywords: Any) -> tuple[int, str]:
        raise PermissionError("no space for a temporary file")

    monkeypatch.setattr(module, "tempfile", SimpleNamespace(mkstemp=_refuse))

    with pytest.raises(module.ComposeError, match="cannot create a temporary file"):
        module._write_output(document, tmp_path / "matrix.json", force=True, candidate_dirs=directories)


@pytest.mark.parametrize(("force", "operation"), [(False, "link"), (True, "replace")])
def test_output_reports_a_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool, operation: str
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "matrix.json"

    def _refuse(*_arguments: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(module.os, operation, _refuse)

    with pytest.raises(module.ComposeError, match="publishing the composed matrix failed"):
        module._write_output(document, output, force=force, candidate_dirs=directories)

    assert not output.exists()
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == []


def test_output_cleanup_failure_does_not_mask_the_existing_output_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    document = module.compose(list(directories))
    output = tmp_path / "matrix.json"
    output.write_text("existing\n", encoding="utf-8")

    def _refuse(*_arguments: object) -> None:
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(module.os, "unlink", _refuse)

    with pytest.raises(module.ComposeError, match="already exists"):
        module._write_output(document, output, force=False, candidate_dirs=directories)

    assert output.read_text(encoding="utf-8") == "existing\n"


def test_cli_reports_an_absent_output_directory_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    output = tmp_path / "missing" / "matrix.json"

    exit_code = module.main([str(d) for d in directories] + ["--output", str(output)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "does not exist" in captured.err
    assert not output.parent.exists()


def test_cli_reports_an_existing_output_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    directories = _write_three(tmp_path)
    output = tmp_path / "matrix.json"
    output.write_text("existing\n", encoding="utf-8")

    exit_code = module.main([str(d) for d in directories] + ["--output", str(output)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "pass --force" in captured.err
    assert output.read_text(encoding="utf-8") == "existing\n"
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == ["matrix.json"]


def test_cli_reports_a_concise_error_for_malformed_scalar_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    directories = _write_one_broken(
        tmp_path,
        "darwin",
        lambda d: _mutate_metadata(d, "compatibility-matrix.json", lambda m: m.update({"candidate_key": []})),
    )
    output = tmp_path / "matrix.json"

    exit_code = module.main([str(d) for d in directories] + ["--output", str(output)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "candidate_key is not a lowercase" in captured.err
    assert not output.exists()
