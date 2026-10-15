#!/usr/bin/env python3
"""Verify an assembled companion candidate against its component-set manifest.

The companion and its two managed extensions update as one coherent set
(section 18.1 to 18.4, WP-14, NAT-16). This gate reads an assembled candidate
directory -- the output of `scripts/build-core-companion-app.py` -- and refuses
it unless every member is present exactly once, at its declared path, with the
declared identity, version, build version, minimum macOS and executable digest.

Fail-closed and bounded. Any finding, any unreadable input and any unknown
manifest schema exits non-zero. There is no skip path and no partial pass, and
nothing here rewrites the candidate or its manifest.

Signing and notarisation are reported only from evidence a caller supplies with
`--signature-evidence`: a JSON object mapping each managed bundle identifier to
the `authority`, `team_identifier`, `cdhash` and `notarized` record of a real
`codesign`/notarisation run. This gate runs neither tool. Without that evidence
the signature status is `unverified` and the candidate is never release
eligible; `--require-release-eligible` turns that into a finding, which is how
release qualification is run against the same gate used locally.

    scripts/verify-core-companion-bundle.py CANDIDATE
        [--signature-evidence FILE] [--require-release-eligible] [--json]

Exit codes: ``0`` clean, ``1`` findings or unusable input, ``2`` bad usage.
Standard library only, entirely offline.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

_BUILDER = Path(__file__).resolve().parent / "build-core-companion-app.py"


def _builder() -> ModuleType:
    """Load the packaging module by path.

    The policy, the manifest writer and the verifier are one module on purpose:
    a second copy of the bundle identifiers, paths or registrations is a second
    source of truth that would drift from the bundles actually built. Its file
    name is not an importable identifier, so it is loaded by location.
    """

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


def _evidence(path: Path) -> Mapping[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("signature evidence must be a JSON object")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--signature-evidence", type=Path)
    parser.add_argument("--require-release-eligible", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)

    evidence: Mapping[str, object] | None = None
    if arguments.signature_evidence is not None:
        try:
            evidence = _evidence(arguments.signature_evidence)
        except (OSError, TypeError, UnicodeDecodeError, ValueError) as error:
            print(f"signature evidence unreadable: {error}", file=sys.stderr)
            return 1

    report = _builder().verify_candidate(
        # Absolute, not resolved: resolving would follow a symlinked candidate
        # root, which the verifier exists to refuse.
        arguments.candidate.absolute(),
        signature_evidence=evidence,
        require_release_eligible=arguments.require_release_eligible,
    )
    if arguments.json:
        print(
            json.dumps(
                {
                    "findings": list(report.findings),
                    "release_eligible": report.release_eligible,
                    "signature_status": report.signature_status,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for finding in report.findings:
            print(finding, file=sys.stderr)
        print(
            f"signature: {report.signature_status}; "
            f"release eligible: {report.release_eligible}"
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
