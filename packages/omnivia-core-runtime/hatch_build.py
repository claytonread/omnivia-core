"""Package the pinned TypeScript compiler beside the code-analysis analyser.

`omnivia_core_runtime.storage.code_analysis_typescript` runs its analyser on the
compiler at `storage/typescript_compiler/` of an installed distribution, never on a
project's or this repository's `node_modules`. This copies exactly what the analyser
can load -- the compiler module and its default library declarations -- plus the
package manifest the adapter reads the version from and the licence notices, from the
repository's `npm ci` install into the wheel.

The copy is verified here against the adapter's own `TYPESCRIPT_VERSION` and
`TYPESCRIPT_DIGEST`, read from its source text rather than imported, so a wheel cannot
be built around any other compiler. An editable install is left alone: it runs from the
source tree, where a caller names the `npm ci` install explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

TARGET = "omnivia_core_runtime/storage/typescript_compiler"
ADAPTER = "src/omnivia_core_runtime/storage/code_analysis_typescript.py"
LIB_NAME = re.compile(r"lib(\.[a-z0-9]+)*\.d\.ts")
NOTICES = ("LICENSE.txt", "ThirdPartyNoticeText.txt")


def _pinned(adapter: str, name: str) -> str:
    found: list[str] = re.findall(rf'^{name}: Final = "([^"]+)"$', adapter, re.MULTILINE)
    if len(found) != 1:
        raise RuntimeError(f"{ADAPTER} does not declare exactly one {name}")
    return found[0]


class CustomBuildHook(BuildHookInterface[BuilderConfig]):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        adapter = (root / ADAPTER).read_text(encoding="utf-8")
        compiler = root.parents[1] / "node_modules" / "typescript"
        try:
            package = json.loads((compiler / "package.json").read_text(encoding="utf-8"))
            libs = sorted(p.name for p in (compiler / "lib").iterdir() if LIB_NAME.fullmatch(p.name))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"the pinned TypeScript compiler is missing at {compiler}: run npm ci") from exc
        if package.get("version") != _pinned(adapter, "TYPESCRIPT_VERSION"):
            raise RuntimeError(f"{compiler} is TypeScript {package.get('version')}, not the pinned release")
        names = ["typescript.js", *libs]
        listing = "".join(
            f"{hashlib.sha256((compiler / 'lib' / name).read_bytes()).hexdigest()}  lib/{name}\n" for name in names
        )
        if "sha256:" + hashlib.sha256(listing.encode("utf-8")).hexdigest() != _pinned(adapter, "TYPESCRIPT_DIGEST"):
            raise RuntimeError(f"{compiler} does not hold the pinned compiler files")
        for name in ("package.json", *NOTICES, *(f"lib/{name}" for name in names)):
            build_data["force_include"][str(compiler / name)] = f"{TARGET}/{name}"
