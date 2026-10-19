#!/usr/bin/env python3
"""Compile the TypeScript code-analysis analyser, or check the committed build is current.

`code_analysis_typescript.cts` is the canonical source; `code_analysis_typescript.cjs`
beside it is what the Python adapter runs, and `ANALYSER_DIGEST` in
`code_analysis_typescript.py` is the digest of the one build the adapter accepts. This
compiles the source with the repository's pinned compiler under `--strict` and writes
both (default) or compares both byte for byte with what is committed (`--check`).
Either way a type error fails the run.

The compiler is the repository-local `node_modules/typescript` that `npm ci` installs at
the exact version `package.json` pins; any other version is refused, because a different
compiler is free to emit different JavaScript from the same source.

Usage (from the repository root):

    npm ci
    python scripts/generate-code-analysis-typescript.py          # regenerate
    python scripts/generate-code-analysis-typescript.py --check  # drift gate
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "packages/omnivia-core-runtime/src/omnivia_core_runtime/storage"
SOURCE = STORAGE / "code_analysis_typescript.cts"
COMPILED = STORAGE / "code_analysis_typescript.cjs"
ADAPTER = STORAGE / "code_analysis_typescript.py"
DIGEST = re.compile(r'^(ANALYSER_DIGEST: Final = ")(sha256:[0-9a-f]{64})(")$', re.MULTILINE)
COMPILER = ROOT / "node_modules/typescript"
PINNED = "5.9.3"
FLAGS = (
    "--strict",
    "--noEmitOnError",
    "--target", "ES2022",
    "--lib", "es2022",
    "--module", "commonjs",
    "--moduleResolution", "node10",
    "--skipLibCheck",
    "--newLine", "lf",
)


def build(out_dir: Path) -> Path:
    """Compile the analyser into `out_dir` and return the compiled file's path."""
    node = shutil.which("node")
    package = COMPILER / "package.json"
    if node is None or not package.is_file():
        raise SystemExit("node and the pinned compiler are required: run npm ci")
    version = json.loads(package.read_text(encoding="utf-8")).get("version")
    if version != PINNED:
        raise SystemExit(f"compiler is TypeScript {version}, not the pinned {PINNED}")
    subprocess.run(
        [node, str(COMPILER / "lib/tsc.js"), *FLAGS, "--outDir", str(out_dir), str(SOURCE)],
        check=True,
    )
    return out_dir / COMPILED.name


def main(argv: list[str]) -> int:
    check = argv == ["--check"]
    if argv and not check:
        print(__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        built = build(Path(scratch)).read_bytes()
    adapter = ADAPTER.read_text(encoding="utf-8")
    if len(DIGEST.findall(adapter)) != 1:
        raise SystemExit(f"{ADAPTER.name} does not hold exactly one ANALYSER_DIGEST")
    pinned = DIGEST.sub(rf"\g<1>sha256:{hashlib.sha256(built).hexdigest()}\g<3>", adapter)
    if not check:
        COMPILED.write_bytes(built)
        ADAPTER.write_text(pinned, encoding="utf-8")
        print(f"wrote {COMPILED.relative_to(ROOT)} and its digest in {ADAPTER.name}")
        return 0
    if not COMPILED.is_file() or COMPILED.read_bytes() != built or pinned != adapter:
        print(
            f"{COMPILED.relative_to(ROOT)} or its ANALYSER_DIGEST is not the compiled form "
            f"of {SOURCE.name}; run python scripts/generate-code-analysis-typescript.py",
            file=sys.stderr,
        )
        return 1
    print("TypeScript code-analysis analyser is current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
