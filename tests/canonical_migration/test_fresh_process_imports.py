"""Every Slice A canonical leaf must import cleanly and in true isolation.

One subprocess per leaf, not a single shared subprocess importing every leaf
in a fixed order: a shared process hides a leaf that only imports cleanly
because an *earlier* leaf in the list happened to pull in a dependency it
silently relies on. Each subprocess also runs ``-I -S`` (isolated mode, no
``site`` initialization) so it starts with exactly ``sys.path == ['', <repo
src>]`` -- no ``PYTHONPATH``, no user site-packages, no third-party package
on the path a leaf could accidentally depend on -- and the only path entry
this test adds is the absolute repository ``src`` directory.

Beyond "it imports", each subprocess asserts every ``omnivia_core`` module
that ended up in ``sys.modules`` resolves to a file *beneath that exact src
directory* -- not a stale editable install, not a shadow copy elsewhere on
the machine -- so a green run means this checkout, not "a build of Core
somewhere on PYTHONPATH".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from _leaves import CANONICAL_LEAF_MODULES

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
#: The interpreter running this suite, not a hardcoded ``.venv/bin/python``.
#: The subprocess must be the same Python the test itself runs under, or a
#: green result says nothing about the environment under test -- and a
#: hardcoded venv path breaks outright under tox/uv/CI or any checkout whose
#: virtualenv lives elsewhere. ``-I -S`` below already strips this
#: interpreter's site configuration, so inheriting it costs no isolation.
PYTHON = sys.executable


def _isolated_import_script(leaf: str) -> str:
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"import {leaf}",
            f"core_src = pathlib.Path({str(CORE_SRC)!r}).resolve()",
            "outside = []",
            "for name, module in sorted(sys.modules.items()):",
            '    if name != "omnivia_core" and not name.startswith("omnivia_core."):',
            "        continue",
            '    path = getattr(module, "__file__", None)',
            "    if path is None:",
            "        continue",
            "    resolved = pathlib.Path(path).resolve()",
            "    try:",
            "        resolved.relative_to(core_src)",
            "    except ValueError:",
            '        outside.append(f"{name} -> {resolved}")',
            "if outside:",
            '    raise SystemExit("omnivia_core modules loaded from outside src: " + ", ".join(outside))',
            'forbidden = {"omnivia_memory", "sqlalchemy"}',
            'leaked = sorted(forbidden & {m.split(".")[0] for m in sys.modules})',
            "if leaked:",
            '    raise SystemExit(f"forbidden modules loaded: {leaked}")',
            'print("OK")',
        ]
    )


@pytest.mark.parametrize("leaf", CANONICAL_LEAF_MODULES, ids=lambda v: v)
def test_leaf_imports_in_isolation_from_src_alone(leaf: str) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", _isolated_import_script(leaf)],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated import of {leaf} failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"
