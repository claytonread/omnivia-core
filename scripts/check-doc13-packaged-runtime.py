#!/usr/bin/env python3
"""Qualify the packaged document runtime on a real macOS host (DOC-13, DOC-16).

`produce` assembles one candidate from explicitly supplied trusted engine inputs,
verifies the exact bytes it just wrote, and then proves the three things a
packaged runtime has to be able to do with no package manager, no search path and
no network in reach:

1. every staged Mach-O resolves inside the payload or inside Apple's own
   libraries, and holds no ``LC_RPATH`` at all;
2. both engines run from an empty environment whose ``PATH`` is the operating
   system's own, loading nothing from a package-manager prefix;
3. the *production* path -- the installation authority, the frozen resource
   identities and the confined worker of DOC-16 -- recognises a generated English
   image and reads a small RTF out of the packaged engines alone.

It then tampers with one packaged model byte and requires the verification that
passed a moment earlier to refuse, because a check that only ever sees good bytes
has not shown that it would notice bad ones.

Nothing is discovered: the engine, model and notice inputs are absolute paths the
caller supplies, and production code is never given one of them -- it resolves
the payload from the installation state the candidate was assembled into, exactly
as an installed product does.

    scripts/check-doc13-packaged-runtime.py produce
        --installation-state DIR --output DIR --version X.Y.Z
        --companion-executable P --finder-extension-executable P
        --share-extension-executable P
        --pandoc P --tesseract P --tessdata-eng P --tessdata-osd P
        --license P [--license P ...]

    scripts/check-doc13-packaged-runtime.py verify --record FILE

`produce` runs only on macOS and writes one record; `verify` re-reads a record
anywhere, offline, and refuses it unless every gate passed. Exit codes: ``0``
clean, ``1`` findings or unusable input, ``2`` bad usage.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
_BUILDER: Final = REPO_ROOT / "scripts" / "build-core-companion-app.py"

RECORD_FORMAT: Final = "omnivia.doc13-packaged-runtime.v1"
RECORD_NAME: Final = "doc13-packaged-runtime.json"

#: Prefixes a staged Mach-O may still resolve against: Apple's own libraries and
#: frameworks, which are the operating system and are deliberately not vendored.
SYSTEM_PREFIXES: Final = ("/usr/lib/", "/System/")
#: Prefixes that must appear nowhere -- not in a load command, not in a loader
#: trace. These are the package-manager roots a packaged runtime replaces.
PACKAGE_MANAGER_PREFIXES: Final = ("/opt/homebrew/", "/opt/local/", "/usr/local/")
#: The whole environment the engines are run under for the offline proof: the
#: operating system's own executable directories and nothing else. No `DYLD_*`,
#: no `PATH` entry a package manager owns, no inherited variable.
OFFLINE_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"

#: The word drawn into the generated raster. Recognising it is what shows the
#: packaged English model actually read pixels, which a blank image cannot.
PROBE_WORD: Final = "INVOICE"
PROBE_RTF: Final = rb"{\rtf1\ansi\deff0 Packaged reader total\par}"
PROBE_CAPTURE: Final = "dcap-" + "d" * 64

#: Every gate, closed. A gate that was never reached stays false.
GATES: Final = (
    "candidate_assembled",
    "bundle_coherent",
    "payload_verified",
    "payload_resolves_inside_itself",
    "engines_run_without_package_manager_path",
    "english_recognised_offline",
    "rtf_read_offline",
    "tampered_payload_refused",
)

_COMMAND_TIMEOUT_SECONDS: Final = 300


def _builder() -> ModuleType:
    """Load the packaging module by location, as the bundle verifier does."""
    spec = importlib.util.spec_from_file_location("build_core_companion_app", _BUILDER)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable on disk
        raise SystemExit(f"cannot load {_BUILDER}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` under `from __future__ import
    # annotations` resolves string annotations through `sys.modules`, so an
    # unregistered module cannot define one.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(command: Sequence[str], *, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        env=None if env is None else dict(env),
    )
    return completed.returncode, completed.stdout + completed.stderr


def _mach_o_files(root: Path) -> list[Path]:
    return sorted(
        path
        for directory in ("bin", "lib")
        for path in (root / directory).iterdir()
        if path.is_file()
    )


def _payload_resolves_inside_itself(root: Path, findings: list[str]) -> bool:
    """Every staged load command points inside the payload or into Apple's own.

    This is the relocation claim, read off the files rather than assumed from
    the packaging step: a reference to a package-manager prefix, an unresolved
    ``@rpath`` and a surviving ``LC_RPATH`` are each a finding, because each
    would leave the payload asking the host where a library is.
    """
    clean = True
    for path in _mach_o_files(root):
        status, listed = _run(["otool", "-L", str(path)])
        if status != 0:
            findings.append(f"load commands unreadable: {path.name}")
            clean = False
            continue
        for line in listed.splitlines()[1:]:
            name = line.strip().split(" (")[0]
            if not name or name.startswith(SYSTEM_PREFIXES):
                continue
            if not name.startswith("@loader_path/"):
                findings.append(f"{path.name} loads {name}")
                clean = False
        status, commands = _run(["otool", "-l", str(path)])
        if status != 0 or "cmd LC_RPATH" in commands:
            findings.append(f"{path.name} still carries a runtime search path")
            clean = False
    return clean


def _engines_run_offline(
    runtime: Any, findings: list[str]
) -> tuple[bool, dict[str, str]]:
    """Both engines start from an empty environment and load nothing foreign.

    ``DYLD_PRINT_LIBRARIES`` makes the loader name every image it maps, so the
    proof is what was actually loaded and not only what the load commands say.
    """
    reported: dict[str, str] = {}
    clean = True
    for name, executable in (("pandoc", runtime.pandoc), ("tesseract", runtime.tesseract)):
        status, output = _run(
            [str(executable), "--version"], env={"PATH": OFFLINE_PATH}
        )
        if status != 0:
            findings.append(f"{name} did not run from an empty environment")
            clean = False
            continue
        reported[name] = (output.splitlines() or [""])[0].strip()
        _status, trace = _run(
            [str(executable), "--version"],
            env={"PATH": OFFLINE_PATH, "DYLD_PRINT_LIBRARIES": "1"},
        )
        foreign = sorted(
            {
                prefix
                for prefix in PACKAGE_MANAGER_PREFIXES
                if prefix in trace
            }
        )
        if foreign:
            findings.append(f"{name} loaded images from {', '.join(foreign)}")
            clean = False
    return clean, reported


def _legible_png() -> bytes:
    """A raster a qualified engine can genuinely read, drawn here and nowhere else."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 55), PROBE_WORD, fill="black", font=ImageFont.load_default(size=92)
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


def _production_proofs(
    installation_state: Path, findings: list[str]
) -> tuple[bool, bool, dict[str, Any]]:
    """Drive the production path over the installed payload, and report what it did.

    The installation is composed the way the service composes it -- from the
    installation-state root and nothing else -- so every path the engines are
    invoked through was resolved and digested by production code. The extraction
    itself goes through the confined worker of DOC-16, which means the engines run
    inside the seal, with the packaged runtime root as the only directory beyond
    the admitted input, the packaged code and the private scratch.
    """
    from omnivia_core_runtime.service.document_processing import (
        held_selection,
        resolve_settings,
    )
    from omnivia_core_runtime.service.document_worker import (
        DocumentWorkerFailed,
        extract_document_isolated,
    )
    from omnivia_core_runtime.service.processing_settings import (
        DEFAULT_SETTINGS,
        admitted_resources,
        installation_catalogue,
        managed_resources,
        packaged_identities,
        packaged_runtime,
    )

    authority = installation_catalogue(installation_state)
    runtime = packaged_runtime(authority)
    if runtime is None:
        findings.append("production did not resolve the installed payload")
        return False, False, {}
    packaged = packaged_identities(runtime)
    admitted = admitted_resources(
        managed_resources(authority),
        pin="doc13-qualification",
        languages=DEFAULT_SETTINGS.languages,
        packaged=packaged,
    )
    evidence: dict[str, Any] = {
        "admitted_kind": admitted.kind,
        "packaged_languages": sorted(packaged),
    }
    assert authority is not None
    with authority.frozen_resources(
        operation_id="doc13-qualification",
        admitted=admitted,
        requested=DEFAULT_SETTINGS.languages,
    ) as resources:
        if not resources.available:
            findings.append(f"the frozen packaged selection is {resources.reason}")
            return False, False, evidence
        evidence["model_hashes"] = list(resources.model_hashes)
        evidence["tessdata_dir"] = resources.tessdata_dir
        evidence["tesseract_path"] = resources.tesseract_path
        image_settings = resolve_settings(
            media_type="image/png",
            profile_ref="document-default",
            settings=DEFAULT_SETTINGS,
            resources=resources,
            runtime=runtime,
        )
        recognised = False
        try:
            outcome = extract_document_isolated(
                _legible_png(),
                media_type="image/png",
                capture_id=PROBE_CAPTURE,
                settings=image_settings,
            )
        except DocumentWorkerFailed as failure:
            findings.append(f"the confined worker refused the image: {failure.reason}")
        else:
            read = " ".join(segment.text or "" for segment in outcome.segments)
            evidence["image_state"] = outcome.state
            evidence["recognised"] = PROBE_WORD in read.upper()
            recognised = outcome.state == "succeeded" and PROBE_WORD in read.upper()
            if not recognised:
                findings.append("the packaged English model recognised no probe word")

    with held_selection(authority) as catalogue_for:
        rtf_settings = resolve_settings(
            media_type="application/rtf",
            profile_ref="document-default",
            settings=DEFAULT_SETTINGS,
            catalogue_for=catalogue_for,
            runtime=runtime,
        )
    parsed = False
    try:
        outcome = extract_document_isolated(
            PROBE_RTF,
            media_type="application/rtf",
            capture_id=PROBE_CAPTURE,
            settings=rtf_settings,
        )
    except DocumentWorkerFailed as failure:
        findings.append(f"the confined worker refused the RTF: {failure.reason}")
    else:
        read = " ".join(segment.text or "" for segment in outcome.segments)
        evidence["rtf_state"] = outcome.state
        evidence["rtf_reader"] = rtf_settings.pandoc_path
        parsed = outcome.state == "succeeded" and "Packaged reader total" in read
        if not parsed:
            findings.append("the packaged RTF reader produced no body text")
    return recognised, parsed, evidence


def _tamper_is_refused(
    builder: ModuleType, candidate: Path, runtime: Any, findings: list[str]
) -> bool:
    """Flip one packaged model byte and require the verification to refuse it.

    Restored afterwards, and the restoration is itself checked: a run that left
    the candidate altered would make every byte this record pins a claim about
    something that no longer exists.
    """
    model = runtime.tessdata_dir / "eng.traineddata"
    original = model.read_bytes()
    flipped = bytearray(original)
    flipped[0] ^= 0xFF
    refused = False
    try:
        model.write_bytes(bytes(flipped))
        if builder.packaged_runtime.resource_findings(
            candidate, _declared(builder, candidate)
        ):
            refused = True
        else:
            findings.append("a tampered packaged model was not refused")
    finally:
        model.write_bytes(original)
    if builder.packaged_runtime.resource_findings(
        candidate, _declared(builder, candidate)
    ):
        findings.append("the candidate did not verify again after the tamper check")
        return False
    return refused


def _manifest_path(builder: ModuleType, candidate: Path) -> Path:
    """The component-set manifest, named by the module that writes it."""
    return candidate / str(builder.MANIFEST_NAME)


def _declared(builder: ModuleType, candidate: Path) -> list[object]:
    """The resource set the candidate's manifest declares, or an empty one."""
    manifest = json.loads(
        _manifest_path(builder, candidate).read_text(encoding="ascii")
    )
    declared = manifest.get("resources") if isinstance(manifest, dict) else None
    return declared if isinstance(declared, list) else []


def _digests(builder: ModuleType, paths: Mapping[str, Path]) -> dict[str, str]:
    return {name: builder._file_digest(path) for name, path in sorted(paths.items())}


def produce(arguments: argparse.Namespace) -> int:
    gates = dict.fromkeys(GATES, False)
    findings: list[str] = []
    if sys.platform != "darwin":
        print("this lane produces evidence only on macOS", file=sys.stderr)
        return 1
    output = arguments.output.absolute()
    if output.exists() and any(output.iterdir()):
        print("the output directory is not empty", file=sys.stderr)
        return 1
    installation_state = arguments.installation_state.absolute()
    builder = _builder()
    candidate = installation_state / builder.packaged_runtime.INSTALLED_CANDIDATE_DIR

    runtime: Any = None
    engine_versions: dict[str, str] = {}
    production: dict[str, Any] = {}
    try:
        builder.build_companion_app(
            executable=arguments.companion_executable.absolute(),
            output=candidate,
            version=arguments.version,
            finder_extension_executable=arguments.finder_extension_executable.absolute(),
            share_extension_executable=arguments.share_extension_executable.absolute(),
            runtime=builder.RuntimeInputs(
                pandoc=arguments.pandoc.absolute(),
                tesseract=arguments.tesseract.absolute(),
                models={
                    "eng": arguments.tessdata_eng.absolute(),
                    "osd": arguments.tessdata_osd.absolute(),
                },
                notices={notice.name: notice.absolute() for notice in arguments.license},
            ),
        )
        gates["candidate_assembled"] = True
    except (OSError, builder.PackagingError, builder.packaged_runtime.PackagedRuntimeRefused) as refusal:
        findings.append(f"the candidate could not be assembled: {refusal}")

    if gates["candidate_assembled"]:
        report = builder.verify_candidate(candidate)
        gates["bundle_coherent"] = report.coherence_ok
        findings.extend(report.findings)
        try:
            runtime = builder.packaged_runtime.verify_candidate_runtime(candidate)
            gates["payload_verified"] = True
        except builder.packaged_runtime.PackagedRuntimeRefused as refusal:
            findings.append(f"the packaged payload was refused: {refusal}")

    if runtime is not None:
        gates["payload_resolves_inside_itself"] = _payload_resolves_inside_itself(
            runtime.root, findings
        )
        ran, engine_versions = _engines_run_offline(runtime, findings)
        gates["engines_run_without_package_manager_path"] = ran
        recognised, parsed, production = _production_proofs(installation_state, findings)
        gates["english_recognised_offline"] = recognised
        gates["rtf_read_offline"] = parsed
        gates["tampered_payload_refused"] = _tamper_is_refused(
            builder, candidate, runtime, findings
        )

    record = {
        "format": RECORD_FORMAT,
        "candidate": {
            "path": str(candidate),
            "manifest_sha256": (
                builder._file_digest(_manifest_path(builder, candidate))
                if _manifest_path(builder, candidate).is_file()
                else None
            ),
            "resource_count": (
                len(_declared(builder, candidate))
                if _manifest_path(builder, candidate).is_file()
                else 0
            ),
            "signature_status": builder.SIGNATURE_STATUS,
            "release_eligible": False,
        },
        "engine_versions": engine_versions,
        "environment": {
            "machine": platform.machine(),
            "offline_path": OFFLINE_PATH,
            "release": platform.release(),
            "system": platform.system(),
        },
        "findings": sorted(findings),
        "gates": gates,
        "production": production,
        "source_inputs": _digests(
            builder,
            {
                "pandoc": arguments.pandoc.absolute(),
                "tesseract": arguments.tesseract.absolute(),
                "tessdata_eng": arguments.tessdata_eng.absolute(),
                "tessdata_osd": arguments.tessdata_osd.absolute(),
            },
        ),
        "verdict": "pass" if all(gates.values()) and not findings else "fail",
    }
    output.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (output / RECORD_NAME).write_text(body, encoding="utf-8")
    print(body, end="")
    return 0 if record["verdict"] == "pass" else 1


def verify(arguments: argparse.Namespace) -> int:
    """Re-read one record offline and refuse it unless every gate passed."""
    try:
        record = json.loads(arguments.record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("the record is unreadable", file=sys.stderr)
        return 1
    findings: list[str] = []
    if not isinstance(record, dict) or record.get("format") != RECORD_FORMAT:
        print("the record names another format", file=sys.stderr)
        return 1
    gates = record.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(GATES):
        findings.append("the record does not carry this lane's gate set")
    else:
        for gate in GATES:
            if gates[gate] is not True:
                findings.append(f"gate not passed: {gate}")
    if record.get("findings"):
        findings.append("the record carries findings")
    if record.get("verdict") != "pass":
        findings.append("the record does not report a pass")
    candidate = record.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("release_eligible") is not False:
        findings.append("the record claims release eligibility")
    for finding in findings:
        print(finding, file=sys.stderr)
    print(f"doc13 packaged runtime: {'ok' if not findings else 'failed'}")
    return 0 if not findings else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    producer = commands.add_parser("produce")
    producer.add_argument("--installation-state", required=True, type=Path)
    producer.add_argument("--output", required=True, type=Path)
    producer.add_argument("--version", required=True)
    producer.add_argument("--companion-executable", required=True, type=Path)
    producer.add_argument("--finder-extension-executable", required=True, type=Path)
    producer.add_argument("--share-extension-executable", required=True, type=Path)
    producer.add_argument("--pandoc", required=True, type=Path)
    producer.add_argument("--tesseract", required=True, type=Path)
    producer.add_argument("--tessdata-eng", required=True, type=Path)
    producer.add_argument("--tessdata-osd", required=True, type=Path)
    producer.add_argument("--license", required=True, action="append", type=Path)
    verifier = commands.add_parser("verify")
    verifier.add_argument("--record", required=True, type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "produce":
        return produce(arguments)
    return verify(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
