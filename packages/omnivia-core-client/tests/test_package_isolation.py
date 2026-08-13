"""The boundary this distribution is allowed to sit on, enforced by reading the source.

``omnivia-core-client`` depends on the public ``omnivia-core`` contracts and on
nothing else. Every check here parses the package's own modules with ``ast``
rather than importing them and inspecting ``sys.modules``: a scan of the source
is deterministic and complete, while what is in ``sys.modules`` depends on what
else the test session happened to import first.

The import allowlist is *exact*, not a denylist with a few known-bad names on
it. A denylist only catches the forbidden imports somebody thought of; pinning
the whole import surface means any new dependency at all has to be added here
deliberately, which is the point at which somebody looks at whether it belongs.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "packages" / "omnivia-core-client"
SOURCE_ROOT = PACKAGE_ROOT / "src" / "omnivia_core_client"
MANIFEST_PATH = PACKAGE_ROOT / "pyproject.toml"

MODULES = sorted(SOURCE_ROOT.glob("*.py"))

#: Every top-level module this package's source is permitted to import. Standard
#: library only, plus the one distribution it depends on.
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "ctypes",
        "dataclasses",
        "http",
        "ipaddress",
        "json",
        "math",
        "omnivia_core",
        "omnivia_core_client",
        "os",
        "pathlib",
        "re",
        "socket",
        "ssl",
        "stat",
        "threading",
        "time",
        "typing",
        "urllib",
    }
)

#: The one module allowed to import ``socket``, and the only reason ``socket`` is
#: on the allowlist above at all.
#:
#: Owner resolution 005 R005-01 placed the concrete local transport in this
#: package. That admits exactly one socket-opening module, and this pins it to
#: that module by name rather than letting the ban lapse for all nine: a
#: ``socket`` import appearing in ``framing.py`` or ``discovery.py`` would still
#: be a protocol foundation quietly becoming a transport, which is the thing the
#: original ban was protecting and the thing the resolution did not change.
#:
#: What the resolution *did* change is the reasoning that put the transport in the
#: CLI. The boundary that defines this distribution is its dependency edge --
#: still exactly ``omnivia-core``, asserted below and by
#: ``scripts/check-package-boundaries.py`` -- not its standard-library surface.
SOCKET_MODULE = "local_ipc.py"

#: The one module allowed to import ``http``, ``ipaddress``, ``ssl`` and
#: ``urllib``, and the only reason those four are on the allowlist above.
#:
#: The authenticated HTTP transport needs all four and no other module needs any
#: of them: ``http.client`` for a unary request, ``ssl`` for the verified default
#: context, ``urllib.parse.urlsplit`` for the endpoint, and ``ipaddress`` for the
#: loopback rule that decides whether cleartext is admissible. Every one of them
#: is a standard library facility, and each was chosen over writing the same
#: thing here -- a hand-written URL splitter, a hand-written loopback test.
#:
#: Pinned by name for the same reason ``socket`` is. Admitting ``ssl`` for the
#: whole package would let a TLS stack appear in ``discovery.py``; admitting
#: ``urllib`` would let a URL fetcher appear anywhere. A third-party HTTP client
#: stays forbidden outright -- see :data:`FORBIDDEN_IMPORTS` -- because the point
#: was never that this package cannot speak HTTP, it is that it has exactly one
#: dependency.
HTTP_MODULE = "http_transport.py"

#: What :data:`HTTP_MODULE` alone may import.
HTTP_ONLY_IMPORTS = ("http", "ipaddress", "ssl", "urllib")

#: Named explicitly so a failure says *what* leaked rather than only that the
#: allowlist was exceeded. Every one of these is forbidden for a stated reason:
#: a sibling distribution would invert the package topology; a database driver,
#: a workspace store, or a lease belongs to the runtime that owns the workspace;
#: a pipe or subprocess would make this foundation a process manager; a
#: third-party HTTP or web framework would be a dependency this distribution does
#: not have.
#:
#: ``socket`` is *not* here: it is confined to :data:`SOCKET_MODULE` by
#: :func:`test_only_the_local_ipc_module_opens_a_socket`, which is a stricter
#: statement than this tuple can make. ``http``, ``ipaddress``, ``ssl`` and
#: ``urllib`` are absent for the same reason and no other -- they are confined to
#: :data:`HTTP_MODULE` by :func:`test_only_the_http_module_reaches_for_http`.
#: A *third-party* HTTP client is still forbidden everywhere, and that is the
#: distinction that matters: this package speaks HTTP with the standard library
#: because it has exactly one dependency and adds none.
FORBIDDEN_IMPORTS = (
    "aiohttp",
    "asyncio",
    "fastapi",
    "flask",
    "httpx",
    "jsonschema",
    "multiprocessing",
    "omnivia_core_cli",
    "omnivia_core_mcp",
    "omnivia_core_runtime",
    "omnivia_memory",
    "pydantic",
    "requests",
    "shlex",
    "shutil",
    "sqlite3",
    "subprocess",
    "urllib3",
)


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names one source file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # pragma: no cover - none exist, and the test below says so
                roots.add(".")
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


ALL_IMPORTS = {root for path in MODULES for root in _imported_roots(path)}


def test_the_package_has_the_modules_this_packet_defines() -> None:
    assert {path.name for path in MODULES} == {
        "__init__.py",
        "compatibility.py",
        "credentials.py",
        "deadline.py",
        "discovery.py",
        "errors.py",
        "framing.py",
        "http_transport.py",
        "local_ipc.py",
        "transport.py",
        "windows_pipe.py",
    }


def test_the_import_surface_is_exactly_the_allowlist() -> None:
    assert ALL_IMPORTS <= ALLOWED_IMPORTS, sorted(ALL_IMPORTS - ALLOWED_IMPORTS)


def test_only_the_local_ipc_module_opens_a_socket() -> None:
    """One socket-opening module, named. The other eight stay a pure foundation.

    Dropping ``socket`` from the allowlist ban to admit the transport would have
    lifted it for every module at once. This keeps the ban everywhere except the
    one file owner resolution 005 R005-01 put it in, so the next ``import
    socket`` anywhere else in this package still fails.

    The HTTP transport is not an exception to this. It opens its connection
    through ``http.client``, which owns the socket underneath, and never imports
    ``socket`` itself.
    """
    importers = sorted(
        path.name for path in MODULES if "socket" in _imported_roots(path)
    )
    assert importers == [SOCKET_MODULE]


def test_only_the_windows_pipe_module_reaches_for_ctypes() -> None:
    """Native pipe access is confined to the one Windows transport module."""
    importers = sorted(
        path.name for path in MODULES if "ctypes" in _imported_roots(path)
    )
    assert importers == ["discovery.py", "windows_pipe.py"]


@pytest.mark.parametrize("module_name", HTTP_ONLY_IMPORTS)
def test_only_the_http_module_reaches_for_http(module_name: str) -> None:
    """One module speaks HTTP, TLS, URLs and IP addresses. The other eight do not.

    The same shape as the ``socket`` pin above and for the same reason: admitting
    these four for the whole package would let a TLS stack into ``discovery.py``
    or a URL parser into ``framing.py``, which is a protocol foundation quietly
    becoming a fetcher. Parameterized so a failure names *which* of the four
    leaked and into what.
    """
    importers = sorted(
        path.name for path in MODULES if module_name in _imported_roots(path)
    )
    assert importers == [HTTP_MODULE]


def test_the_credential_module_needs_no_privileged_import() -> None:
    """The credential seam is pure standard library and pure computation.

    It resolves nothing itself: no filesystem, no network, no process, no
    platform call. Asserting that here rather than only in prose is what stops a
    later change from quietly making this module a credential *source*, which is
    the one thing it must never become.
    """
    roots = _imported_roots(SOURCE_ROOT / "credentials.py")
    assert roots <= {
        "__future__",
        "collections",
        "omnivia_core_client",
        "math",
        "re",
        "threading",
        "time",
        "typing",
    }, sorted(roots)


@pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
def test_a_forbidden_module_is_not_imported_anywhere(forbidden: str) -> None:
    offenders = [path.name for path in MODULES if forbidden in _imported_roots(path)]
    assert offenders == []


def test_no_sibling_distribution_is_named_anywhere_in_the_source() -> None:
    """Not even in a string: a deferred or dynamic import would evade the AST scan.

    Matched on whole names, so this package's own ``omnivia_core_client`` is not
    read as the ``omnivia_core_cli`` it happens to start with.
    """
    for path in MODULES:
        source = path.read_text(encoding="utf-8")
        for sibling in ("omnivia_core_runtime", "omnivia_core_cli", "omnivia_core_mcp"):
            found = re.search(rf"\b{sibling}\b", source)
            assert found is None, (path.name, sibling)


def test_nothing_is_imported_dynamically_past_the_ast_scan() -> None:
    """`importlib`, `__import__`, `eval` and `exec` would all defeat the checks above."""
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called.isdisjoint({"__import__", "eval", "exec", "compile"}), path.name


def test_relative_imports_are_not_used() -> None:
    """Absolute imports only, so a module's provenance is readable at the import line."""
    assert "." not in ALL_IMPORTS


#: The Core modules this package may import from, exactly.
#:
#: The v1 barrel is the ordinary one. ``canonical_json`` is listed beside it
#: because it is a public module of the same contract -- it declares an
#: ``__all__``, it is the normative RFC 8785 implementation this package's frame
#: format is defined in terms of, and the barrel simply does not re-export
#: ``canonical_bytes`` or ``parse_json_document``. Reaching for it is not
#: reaching around the barrel for something private; the alternative would be a
#: second canonicalizer here, which is exactly what the format must not have.
ALLOWED_CORE_MODULES = frozenset(
    {
        "omnivia_core.contracts.v1",
        "omnivia_core.contracts.v1.canonical_json",
    }
)


def test_only_the_public_contract_surface_of_core_is_imported() -> None:
    """Never a private module of Core, and never a deep path around its barrel."""
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = getattr(node, "module", None)
            if (
                isinstance(node, ast.ImportFrom)
                and module
                and module.startswith("omnivia_core.")
            ):
                assert module in ALLOWED_CORE_MODULES, (path.name, module)


def test_no_private_module_of_core_is_imported() -> None:
    """An underscore-prefixed component anywhere in the path is private, always."""
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = getattr(node, "module", None)
            if (
                isinstance(node, ast.ImportFrom)
                and module
                and module.startswith("omnivia_core")
            ):
                assert not any(part.startswith("_") for part in module.split(".")), (
                    path.name,
                    module,
                )


# --------------------------------------------------------------------------
# The distribution manifest
# --------------------------------------------------------------------------


MANIFEST = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_distribution_identity() -> None:
    project = MANIFEST["project"]
    assert project["name"] == "omnivia-core-client"
    assert project["version"] == "0.1.0"
    assert project["requires-python"] == ">=3.11"


def test_the_import_package_version_matches_the_distribution() -> None:
    import omnivia_core_client

    assert omnivia_core_client.__version__ == MANIFEST["project"]["version"] == "0.1.0"


def test_the_only_runtime_dependency_is_the_public_core_contract() -> None:
    assert MANIFEST["project"]["dependencies"] == ["omnivia-core>=0.1.0,<0.2.0"]


def test_the_build_backend_is_the_accepted_pinned_hatchling() -> None:
    build_system = MANIFEST["build-system"]
    assert build_system["build-backend"] == "hatchling.build"
    assert build_system["requires"] == ["hatchling>=1.26,<2"]


def test_the_wheel_target_selects_this_packages_own_source() -> None:
    wheel = MANIFEST["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/omnivia_core_client"]


def test_no_console_entry_point_is_declared() -> None:
    """A client library is imported, not run: a script here would be a surface
    nothing in this packet asked for."""
    assert "scripts" not in MANIFEST["project"]
    assert "gui-scripts" not in MANIFEST["project"]
    assert "entry-points" not in MANIFEST["project"]


def test_the_typing_marker_ships() -> None:
    assert (SOURCE_ROOT / "py.typed").is_file()


def test_the_readme_marks_the_unimplemented_surfaces() -> None:
    """What is still absent, after the authenticated HTTP transport landed.

    ``local socket`` left this list when R005-01 landed the local transport;
    HTTP and Windows named-pipe transport have now left it too. Retry, managed
    startup and the high-level client remain their own packets.
    """
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
    not_implemented = readme.split("## Not implemented yet", 1)
    assert len(not_implemented) == 2, "README must state what is not implemented yet"
    for surface in (
        "retry",
        "managed service startup",
        "high-level client",
    ):
        assert surface in not_implemented[1], surface


def test_the_readme_no_longer_claims_http_or_credentials_are_absent() -> None:
    """The counterweight: a README that lists a shipped surface as missing is wrong.

    Asserted on the "not implemented" section specifically rather than on the
    whole file, because both words appear -- correctly -- in the sections that
    describe what the transport and the credential seam now do.
    """
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
    absent = readme.split("## Not implemented yet", 1)[1].lower()
    assert "http" not in absent
    assert "credential" not in absent
    assert "named-pipe" not in absent
