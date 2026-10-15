#!/usr/bin/env python3
"""Verify an assembled companion candidate against its component-set manifest.

The companion and its two managed extensions update as one coherent set
(section 18.1 to 18.4, WP-14, NAT-16). This gate reads an assembled candidate
directory -- the output of `scripts/build-core-companion-app.py` -- and refuses
it unless every member is present exactly once, at its declared path, with the
declared identity, version, build version, minimum macOS and executable digest.

Fail-closed and bounded. Any finding, any unreadable input, any unknown manifest
schema and any manifest field this schema does not bind exits non-zero. There is
no skip path and no partial pass, and nothing here rewrites the candidate or its
manifest.

Coherence and release eligibility are reported separately, because only the
first can be answered locally. Signing and notarisation are never read from a
file, and the identity a candidate is checked against is never taken from this
command line: it is `REPOSITORY_SIGNING_AUTHORITY` in the packaging module,
reviewed configuration owned by the repository. There is no flag by which a
caller nominates the Team ID or designated requirement its own candidate must
satisfy, because a caller-chosen expected identity verifies nothing.

That constant is `None` today -- no designated signing identity exists in this
repository, and no repository authority binds an owner for the app's update
(WP-00 binding ledger row 13). So the platform authorities are not run at all,
signing verification reports `unavailable`, and no candidate is release
eligible. A reviewed change binding both makes this gate run `codesign --verify
--deep --strict`, `codesign --verify -R=`, `codesign --display` on every
component and then `spctl --assess` and `xcrun stapler validate` on the
containing app; only all of those succeeding reports `verified`, and off macOS
or with a tool missing every one of them fails.

`--require-release-eligible` turns each remaining blocker into a finding, which
is how release qualification is run against the same gate used locally.

    scripts/verify-core-companion-bundle.py CANDIDATE
        [--require-release-eligible] [--json]

Exit codes: ``0`` clean, ``1`` findings or unusable input, ``2`` bad usage.
Standard library only; it reaches no network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--require-release-eligible", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)

    builder = _builder()
    report = builder.verify_candidate(
        # Absolute, not resolved: resolving would follow a symlinked candidate
        # root, which the verifier exists to refuse.
        arguments.candidate.absolute(),
        require_release_eligible=arguments.require_release_eligible,
    )
    if arguments.json:
        print(
            json.dumps(
                {
                    "coherence_ok": report.coherence_ok,
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
            f"coherence: {'ok' if report.coherence_ok else 'failed'}; "
            f"signature: {report.signature_status}; "
            f"release eligible: {report.release_eligible}"
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
