#!/usr/bin/env python3
"""Current-index resolver and installed-root smoke for the compatibility root.

This is a standalone acceptance gate, deliberately **outside pytest collection**.
It performs a resolving install that may contact the configured index, so it must
run exactly once per acceptance run, and the
only way to guarantee that while `python -m pytest tests/canonical_migration
tests/compatibility -q` and `python -m pytest -q` both run in the same job is to
keep it out of the trees those commands collect. ``tests/compatibility/
test_root_facade_distribution.py`` holds the fast, repository-local structural
tests that pin this file's workflow wiring and the behaviour below.

What it does, in order:

1. builds both wheels fresh from this checkout, outside the repository tree;
2. creates an isolated Python 3.11 virtual environment;
3. installs ``omnivia-memory`` into it *normally* -- no ``--no-deps``, no
   ``--no-index`` -- with the locally built Core wheel offered through
   ``--find-links`` and everything else resolved from the configured index, and
   asks pip for a machine-readable ``--report`` of what it resolved;
4. **proves provenance from that report**: the ``omnivia-core`` install entry's
   download URL must be a ``file://`` URL with a strictly empty authority -- the
   form ``Path.as_uri`` writes -- resolving to the exact Core wheel this run built,
   and its recorded archive hash must be present, be a well-formed SHA-256 digest,
   and match that file's bytes -- a missing or malformed hash fails closed rather
   than being waived. The empty authority is part of the identity, not a spelling
   detail: ``file://host/...`` names a file on another host while carrying the same
   path, so it is rejected instead of compared. Content equality alone would not do
   -- an index carrying a same-version wheel with identical content would satisfy it
   -- so the URL identity is the primary assertion and the recorded hash is a
   second, weaker corroboration that is nonetheless mandatory. The SQLAlchemy entry
   must be one pip's report attributed to an ``http``/``https`` candidate URL with a
   nonempty parsed host component, a usable port, and no whitespace or ASCII control
   characters in that host. That is a check on the *shape of the URL pip recorded*
   and nothing more: it does not contact the host, does not validate DNS
   resolvability, does not establish artifact identity, does not distinguish a wheel
   served from a local HTTP cache from a freshly downloaded one, and does not prove
   any network transfer happened on this run. What it does exclude is a report that
   attributes the dependency to a local file or to an unusable address: any
   ``file://`` wheel, empty URL, other
   non-``http(s)`` scheme, or authority that names no host (``https://user@/x``,
   ``https://:8080/x``), cannot be parsed, carries a nonnumeric or out-of-range port,
   or spells its host with whitespace or ASCII control characters fails closed;
5. re-reads the installed distribution metadata and checks the deprecation notice
   and the exact dependency contract survived the install round trip;
6. imports all 47 registered legacy route modules and their 47 canonical
   counterparts out of the installed tree and runs the root identity/export audit
   there -- exact ordered ``__all__``, the 182 advertised identities, the six
   collisions, the four hidden bindings, and a version-only canonical root.

**What this is, and is not.** It is a *current-index resolver smoke*: against
whatever the configured index resolves to today, installing ``omnivia-memory``
pulls in the compatible ``omnivia-core`` and its declared runtime dependency, and
the resulting installed tree serves the whole compatibility surface with
identities intact. The SQLAlchemy origin check says only this: pip's report
attributed that distribution to an ``http`` or ``https`` candidate URL with a
nonempty parsed host component, a usable port, and no host whitespace or ASCII
control characters. It does **not** validate that the host resolves in DNS or
exists at all, does **not** establish the artifact's identity, does **not**
distinguish a cache hit from a download, and does **not** prove a network transfer
occurred on this run; nor does it say anything about *which* artifact an index will
serve tomorrow. It is **not** a locked or reproducible supply-chain proof --
this repository has no Python lockfile or constraints file, and the range pins in
``pyproject.toml`` are ranges, not a lockfile. It is **not** evidence about
*published* artifacts: everything installed here is either built from this checkout
or supplied by pip from whatever candidate the configured index offers at run time.
A proof against published artifacts on a controlled staging index, with their final
metadata and signatures, is an external release gate.

It is fail-closed: there is no skip path, no offline fallback, and every
subprocess has an explicit timeout, so an environment that cannot resolve reports
a failure rather than a pass.

The oracle for the root audit is the frozen contract in this repository
(``baseline.facade_manifest``'s ``ROOT_FACADE_ALL`` and the leaf route map in
``baseline.inventory``). That is intentional and does not weaken the check: the
frozen registry is *the contract*, the installed artifacts cannot influence it,
and everything being checked -- the objects, their identities, and the paths they
resolved from -- comes only from the installed wheels.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# E402: these imports resolve only through the `sys.path` insert above, so they
# cannot move to the top of the file.
from baseline.facade_manifest import (  # noqa: E402
    ROOT_FACADE_ALL,
    ROOT_FACADE_HIDDEN_BINDINGS,
    load_manifest,
)
from baseline.inventory import FACADE_ROUTES  # noqa: E402

CORE_DIR = REPO_ROOT
MEMORY_DIR = REPO_ROOT / "services" / "omnivia-memory"

EXPECTED_VERSION = "0.1.0"
REQUIRED_CORE_DEPENDENCY = "omnivia-core>=0.1.0,<0.2.0"
REQUIRED_SQLALCHEMY_DEPENDENCY = "sqlalchemy>=2.0.0"

#: The migration URL the distribution must publish. Pinned as an exact literal and
#: screened for placeholder spellings; nothing here requests it, so its reachability
#: is a maintenance obligation rather than something this gate verifies.
EXPECTED_MIGRATION_URL = (
    "https://github.com/claytonread/omnivia-core/tree/main/services/omnivia-memory"
    "#migration"
)

#: Fragments the ``Summary`` must carry. Checked as fragments rather than as one
#: exact string so wording can be improved without loosening what it has to say:
#: that this distribution is deprecated, that it is a facade for ``omnivia-core``,
#: and what to import instead.
REQUIRED_SUMMARY_FRAGMENTS = (
    "DEPRECATED",
    "compatibility facade",
    "omnivia-core",
    "omnivia_core",
)

#: Guidance the shipped README (the metadata description) must contain. Each is a
#: distinct obligation from ADR-036's deprecation requirements, so they are listed
#: separately rather than as one blob.
REQUIRED_README_FRAGMENTS = (
    # The canonical dependency and import examples.
    'dependencies = ["omnivia-core>=0.1.0,<0.2.0"]',
    "from omnivia_core.knowledge import",
    "from omnivia_core.control_plane import",
    # The supported compatibility window and the removal rule.
    "at least two scheduled release trains",
    "removed only in a major release",
    # Consumer update guidance.
    "## Migration",
    "Updating a consumer",
)

#: The six export collisions, restated so the installed-artifact audit does not
#: have to break a tie by import order. Same six the source-side test pins.
ROOT_COLLISION_OWNERS = {
    "LifecycleState": "omnivia_core.control_plane.models",
    "ProvenanceRequirement": "omnivia_core.component_contract.models",
    "Source": "omnivia_core.provenance.models",
    "SourceRef": "omnivia_core.knowledge.models",
    "SourceType": "omnivia_core.provenance.models",
    "ValidationResult": "omnivia_core._shared.validation",
}

#: The four non-advertised compatibility bindings and the module that owns each.
#: Two canonical, two deliberately still legacy-owned.
ROOT_HIDDEN_OWNERS = {
    "Database": "omnivia_memory.persistence.database",
    "MemoryCreate": "omnivia_core.memory.models",
    "MemoryService": "omnivia_memory.memory.service",
    "MemoryUpdate": "omnivia_core.memory.models",
}


class ResolverSmokeError(RuntimeError):
    """A step this gate cannot continue past."""


def root_owner_map() -> dict[str, str]:
    """``advertised name -> canonical owner`` for the 182 portable root names."""
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for symbols in FACADE_ROUTES.values():
        for name, canonical_module in symbols.items():
            owners[name].add(canonical_module)
    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for name in ROOT_FACADE_ALL:
        if name == "__version__":
            continue
        if name in ROOT_COLLISION_OWNERS:
            mapping[name] = ROOT_COLLISION_OWNERS[name]
            continue
        candidates = owners.get(name, set())
        if len(candidates) != 1:
            unresolved.append(f"{name} ({sorted(candidates)})")
            continue
        mapping[name] = next(iter(candidates))
    if unresolved:
        raise ResolverSmokeError(
            "cannot resolve a canonical owner for: " + "; ".join(sorted(unresolved))
        )
    return mapping


def _child_env() -> dict[str, str]:
    """Drop ``PYTHONPATH`` for every child process, so builds and installed-artifact
    checks see only their own environment rather than this source tree."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _run(
    command: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=None if cwd is None else str(cwd),
        env=_child_env(),
    )


def _build_wheel(project_dir: Path, wheelhouse: Path) -> Path:
    before = set(wheelhouse.glob("*.whl"))
    result = _run(
        [
            sys.executable, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(wheelhouse), str(project_dir),
        ],
        timeout=180,
    )
    if result.returncode != 0:
        raise ResolverSmokeError(
            f"failed to build a wheel for {project_dir}:\n{result.stdout}\n{result.stderr}"
        )
    built = set(wheelhouse.glob("*.whl")) - before
    if len(built) != 1:
        raise ResolverSmokeError(f"expected exactly one new wheel, got {sorted(built)}")
    return built.pop()


#: The characters a hexadecimal SHA-256 digest may be spelled with. Both cases are
#: allowed because the digest is a value, not a string: ``pip`` writes it in lower
#: case, but an upper-case spelling of the same bytes is the same digest.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256_digest(value: object) -> bool:
    """Whether ``value`` is a well-formed SHA-256 digest: exactly 64 hex characters.

    Checked before comparing, so a truncated, non-hex or otherwise malformed field
    is reported as an unusable hash rather than silently failing the comparison for
    the wrong reason.
    """
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


def _report_entry(report: dict[str, Any], distribution: str) -> dict[str, Any]:
    """The single ``install`` entry pip recorded for ``distribution``."""
    matches = [
        entry
        for entry in report.get("install", [])
        if str(entry.get("metadata", {}).get("name", "")).lower().replace("_", "-")
        == distribution
    ]
    if len(matches) != 1:
        raise ResolverSmokeError(
            f"the pip report has {len(matches)} install entries for {distribution!r}; "
            f"it recorded "
            f"{sorted(e.get('metadata', {}).get('name') for e in report.get('install', []))}"
        )
    return matches[0]


def _url_path(url: str) -> Path | None:
    """The local filesystem path a ``file://`` URL names, or ``None`` if it does not
    name one.

    The authority must be *strictly empty* -- ``file:///path``, exactly the form
    ``Path.as_uri`` writes. This is not pedantry about spelling: ``file://host/path``
    names a file on ``host``, and its path component is byte-identical to the local
    wheel's, so returning a path for one would let a URL pointing somewhere else
    entirely satisfy the exact-path comparisons below. ``localhost`` is rejected with
    the rest: RFC 8089 permits it as a synonym for "here", but pip writes what
    ``Path.as_uri`` writes, so accepting a second spelling of "here" would only widen
    the shapes a report can claim a local artifact in without proving anything more.

    A URL ``urllib`` cannot parse -- a malformed authority such as an unterminated
    IPv6 literal raises ``ValueError`` -- is not a local path either, and is reported
    as such rather than escaping as an unhandled error: fail closed.
    """
    try:
        parsed = urlparse(url)
        authority = parsed.netloc
    except ValueError:
        return None
    if parsed.scheme != "file" or authority != "":
        return None
    return Path(url2pathname(unquote(parsed.path))).resolve()


def _index_hostname(url: str) -> str | None:
    """The host component an ``http``/``https`` URL spells, or ``None`` if it spells
    none usably.

    This is a purely syntactic read of the URL text. A returned host has not been
    resolved, contacted or validated in any way beyond the spelling rules below, so
    a return value says the URL is well formed and nothing about whether anything
    answers at that address.

    A non-empty ``netloc`` is not the test. ``https://user@/name.whl`` and
    ``https://:8080/name.whl`` both carry an authority while naming no host at all,
    so checking ``netloc`` alone would accept a URL that identifies no host
    whatsoever. Reading the host is what settles it, and because both parsing and
    that read raise on a malformed authority, the raise is caught and reported as
    "no host" rather than crashing the gate: fail closed.

    The port is read inside that same guard, and the read is the point rather than a
    formality. ``hostname`` never looks at the port, so ``https://host:notaport/x``
    and ``https://host:99999/x`` would otherwise return a perfectly good-looking host
    from here and raise ``ValueError`` from whatever read the port next -- an
    unhandled crash instead of a recorded failure, which in a run collecting problems
    for reporting loses them. Reading it here turns an unparsable or out-of-range
    port into "no host", on the same terms as an unparsable authority.

    A host must also be free of whitespace and ASCII control characters. ``urllib``
    deletes tab, newline and carriage return from a URL before parsing, but it passes
    every other control character and every space straight through: ``https://   /x``,
    ``https://a b.example/x`` and a host carrying a NUL or DEL byte all parse to a
    *truthy* ``hostname`` that is not a usable host name at all. A blank host is the
    same fail-open the empty-authority cases close, so it is
    rejected on the same terms. Ordinary domains, IPv4 and IPv6 literals, userinfo
    and valid ports contain none of these characters and are unaffected.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        hostname = parsed.hostname
        # Deliberate: this read is what makes a nonnumeric or out-of-range port fail
        # closed here, inside the guard, rather than raise outside it.
        _ = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in hostname
    ):
        return None
    return hostname


def _installed_report_script() -> str:
    return """
import importlib.metadata as md
import json

installed = {}
for name in ("omnivia-core", "omnivia-memory", "sqlalchemy"):
    try:
        installed[name] = md.version(name)
    except md.PackageNotFoundError:
        installed[name] = None

memory = md.metadata("omnivia-memory")
print(json.dumps({
    "versions": installed,
    "summary": memory["Summary"],
    "project_urls": memory.get_all("Project-URL") or [],
    "requires": md.requires("omnivia-memory"),
    "description": memory.get_payload() if memory.get_payload() else (
        memory.get("Description") or ""
    ),
}))
"""


def installed_root_audit_script() -> str:
    """The root identity/export audit, run against the installed wheels only."""
    manifest = load_manifest()
    legacy_modules = list(manifest.legacy_modules)
    canonical_modules = list(manifest.canonical_modules)
    return f"""
import importlib
import sys

ROOT_ALL = {list(ROOT_FACADE_ALL)!r}
ROOT_OWNERS = {sorted(root_owner_map().items())!r}
HIDDEN_OWNERS = {sorted(ROOT_HIDDEN_OWNERS.items())!r}
HIDDEN = {list(ROOT_FACADE_HIDDEN_BINDINGS)!r}
COLLISIONS = {sorted(ROOT_COLLISION_OWNERS.items())!r}
LEGACY_MODULES = {legacy_modules!r}
CANONICAL_MODULES = {canonical_modules!r}
CHECKOUT = {str(REPO_ROOT)!r}

assert len(LEGACY_MODULES) == 47, len(LEGACY_MODULES)
assert len(CANONICAL_MODULES) == 47, len(CANONICAL_MODULES)

# Every registered route module, both sides, imported out of the installed
# artifacts -- and proved to have resolved from site-packages rather than from the
# checkout this wheel was built from.
loaded = []
for name in LEGACY_MODULES + CANONICAL_MODULES:
    loaded.append(importlib.import_module(name))
for module in loaded:
    path = getattr(module, "__file__", None)
    assert path is not None, module.__name__
    assert "site-packages" in path, (module.__name__, path)
    assert not path.startswith(CHECKOUT), (module.__name__, path)

root = importlib.import_module("omnivia_memory")
canonical_root = importlib.import_module("omnivia_core")

# The advertised contract: exact ordered __all__, and the star surface is exactly
# that set.
assert list(root.__all__) == ROOT_ALL, "installed __all__ drifted"
namespace = {{}}
exec("from omnivia_memory import *", namespace)
namespace.pop("__builtins__", None)
assert set(namespace) == set(ROOT_ALL), sorted(set(namespace) ^ set(ROOT_ALL))

# Every advertised name is the exact canonical object.
checked = 0
for name, owner in ROOT_OWNERS:
    owner_module = importlib.import_module(owner)
    assert getattr(root, name) is getattr(owner_module, name), (name, owner)
    checked += 1
assert checked == 182, checked

# The collisions resolve to their named owner.
for name, owner in COLLISIONS:
    assert getattr(root, name) is getattr(importlib.import_module(owner), name), name

# The version is the canonical root's own object, not a copy.
assert root.__version__ is canonical_root.__version__
assert list(canonical_root.__all__) == ["__version__"]

# The four hidden bindings: importable, unadvertised, exact owners.
for name, owner in HIDDEN_OWNERS:
    assert hasattr(root, name), name
    assert name not in root.__all__, name
    assert getattr(root, name) is getattr(importlib.import_module(owner), name), name
public = {{
    name
    for name, value in vars(root).items()
    if not name.startswith("_") and type(value).__name__ != "module"
}}
advertised = {{name for name in ROOT_ALL if not name.startswith("__")}}
assert sorted(public - advertised) == sorted(HIDDEN), sorted(public - advertised)
assert "__getattr__" not in vars(root)
assert "__dir__" not in vars(root)

# The canonical root ships none of the legacy root's advertised contracts, which
# is why the legacy root imports from 11 owners rather than re-exporting one.
for name in advertised:
    assert not hasattr(canonical_root, name), name

print("OK")
"""


def _check_report_provenance(
    report: dict[str, Any], core_wheel: Path, memory_wheel: Path, wheelhouse: Path
) -> list[str]:
    """Fail unless pip's own report says the Core artifact it installed *is* the
    wheel this run built, and unless that same report attributes SQLAlchemy to an
    ``http``/``https`` candidate URL with a usable host.

    The URL identity is the assertion the byte comparison cannot make on its own:
    an index that happened to carry a same-version ``omnivia-core`` wheel with
    identical content would pass a content check while the resolver had never seen
    the local artifact at all.

    Both halves are fail-closed. The local URLs must carry a strictly empty
    authority, the form ``Path.as_uri`` writes, so a ``file://host/...`` URL whose
    path happens to equal the local wheel's is rejected before the comparison rather
    than passing it (see ``_url_path``). The recorded SHA-256 is required, not merely
    checked when present: a report that names the right path but records no usable
    hash is a report that never corroborated the bytes, and accepting it would
    contradict the contract stated above. SQLAlchemy's recorded URL is required to be
    an ``http``/``https`` one with a nonempty parsed host, a usable port, and no host
    whitespace or ASCII control characters -- a non-empty authority is not enough,
    since ``https://user@/x`` and ``https://:8080/x`` carry one while naming no host,
    and neither is a truthy parsed host, since a blank or control-character one is
    not a usable host either -- so a wheel taken from any local file (inside the
    wheelhouse or not), an empty URL, a malformed authority, an unusable port, or any
    other scheme is rejected rather than passing by not matching one narrow local
    pattern (see ``_index_hostname``).

    That SQLAlchemy half is a check on the recorded URL's shape, and claims nothing
    beyond it: it does not resolve or contact the host, does not validate DNS
    resolvability, does not establish which artifact was installed, does not tell a
    cache hit from a download, and does not prove a network transfer occurred. What
    it establishes is that the report did not attribute the dependency to a local
    file or to an unusable address.
    """
    problems: list[str] = []

    core = _report_entry(report, "omnivia-core")
    core_url = str(core.get("download_info", {}).get("url", ""))
    core_path = _url_path(core_url)
    if core_path is None:
        problems.append(
            f"pip resolved omnivia-core from {core_url!r}, which is not a local "
            f"file:// URL with an empty authority, so it did not install the wheel "
            f"this run built"
        )
    elif core_path != core_wheel.resolve():
        problems.append(
            f"pip resolved omnivia-core from {core_path}, not the wheel this run "
            f"built at {core_wheel.resolve()}"
        )
    else:
        archive_info = core.get("download_info", {}).get("archive_info", {})
        hashes = archive_info.get("hashes", {}) if isinstance(archive_info, dict) else {}
        recorded = hashes.get("sha256") if isinstance(hashes, dict) else None
        if not _is_sha256_digest(recorded):
            problems.append(
                f"pip recorded no usable sha256 for the omnivia-core artifact it "
                f"installed ({recorded!r}); a 64-character hexadecimal digest is "
                f"required, so an absent or malformed hash fails closed"
            )
        elif str(recorded).lower() != _sha256(core_wheel):
            problems.append(
                f"pip recorded sha256 {recorded} for the omnivia-core artifact it "
                f"installed, but the wheel this run built hashes to "
                f"{_sha256(core_wheel)}"
            )

    memory = _report_entry(report, "omnivia-memory")
    memory_url = str(memory.get("download_info", {}).get("url", ""))
    memory_path = _url_path(memory_url)
    if memory_path is None:
        problems.append(
            f"pip installed omnivia-memory from {memory_url!r}, which is not a local "
            f"file:// URL with an empty authority, so it did not install the wheel "
            f"this run built at {memory_wheel.resolve()}"
        )
    elif memory_path != memory_wheel.resolve():
        problems.append(
            f"pip installed omnivia-memory from {memory_path}, not the wheel this "
            f"run built at {memory_wheel.resolve()}"
        )

    sqlalchemy = _report_entry(report, "sqlalchemy")
    sqlalchemy_url = str(sqlalchemy.get("download_info", {}).get("url", ""))
    # A syntactic check on the URL pip recorded, not a network observation: passing
    # here means the report attributed SQLAlchemy to an http(s) candidate with a
    # usable host, not that the host was contacted, resolved in DNS, or served these
    # bytes on this run. The failure messages below are worded from the report's
    # point of view for the same reason.
    if _index_hostname(sqlalchemy_url) is None:
        sqlalchemy_path = _url_path(sqlalchemy_url)
        if sqlalchemy_path is not None and wheelhouse.resolve() in sqlalchemy_path.parents:
            problems.append(
                f"pip's report did not attribute SQLAlchemy to an http(s) candidate "
                f"URL with a nonempty usable host component: it recorded a local "
                f"wheelhouse file ({sqlalchemy_path})"
            )
        else:
            problems.append(
                f"pip's report did not attribute SQLAlchemy to an http(s) candidate "
                f"URL with a nonempty usable host component: it recorded "
                f"{sqlalchemy_url!r}"
            )

    return problems


def main() -> int:
    if sys.version_info[:2] != (3, 11):
        print(
            "FAIL: this gate requires the Python 3.11 the acceptance workflow pins; "
            f"got {sys.version_info[0]}.{sys.version_info[1]}",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="omnivia-root-facade-resolver-") as raw:
        workdir = Path(raw)
        wheelhouse = workdir / "wheelhouse"
        wheelhouse.mkdir()

        core_wheel = _build_wheel(CORE_DIR, wheelhouse)
        memory_wheel = _build_wheel(MEMORY_DIR, wheelhouse)
        if not core_wheel.name.startswith("omnivia_core-"):
            raise ResolverSmokeError(core_wheel.name)
        if not memory_wheel.name.startswith("omnivia_memory-"):
            raise ResolverSmokeError(memory_wheel.name)
        print(f"  built {core_wheel.name} and {memory_wheel.name}")

        venv_dir = workdir / "resolver-venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        venv_python = venv_dir / "bin" / "python"
        report_path = workdir / "install-report.json"

        # Deliberately no --no-deps and no --no-index: this is the step that
        # exercises resolution. --find-links makes the locally built Core wheel
        # available as a candidate; everything else resolves from the configured
        # index. --report is what makes the provenance of the chosen Core artifact
        # checkable rather than inferred.
        install = _run(
            [
                str(venv_python), "-m", "pip", "install",
                "--find-links", str(wheelhouse),
                "--report", str(report_path),
                "--timeout", "60",
                "--retries", "2",
                str(memory_wheel),
            ],
            timeout=600,
            cwd=workdir,
        )
        if install.returncode != 0:
            raise ResolverSmokeError(
                "normal (resolving) install of the compatibility wheel failed. This "
                "gate is fail-closed: it needs the configured index to be reachable "
                "and does not fall back to an offline install.\n"
                f"{install.stdout}\n{install.stderr}"
            )
        if not report_path.is_file():
            raise ResolverSmokeError("pip wrote no --report; provenance is unprovable")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        print("  resolving install completed; pip report written")

        problems.extend(
            _check_report_provenance(report, core_wheel, memory_wheel, wheelhouse)
        )

        result = _run(
            [str(venv_python), "-I", "-c", _installed_report_script()],
            timeout=120,
            cwd=workdir,
        )
        if result.returncode != 0:
            raise ResolverSmokeError(f"{result.stdout}\n{result.stderr}")
        installed = json.loads(result.stdout)

        # The declared runtime dependencies are present in the installed environment
        # at the versions the contract allows.
        for distribution in ("omnivia-memory", "omnivia-core"):
            if installed["versions"][distribution] != EXPECTED_VERSION:
                problems.append(
                    f"{distribution} installed at "
                    f"{installed['versions'][distribution]!r}, expected "
                    f"{EXPECTED_VERSION!r}"
                )
        sqlalchemy_version = installed["versions"]["sqlalchemy"]
        if sqlalchemy_version is None:
            problems.append(
                "SQLAlchemy was not installed, so dependency resolution did not happen"
            )
        elif not Requirement(REQUIRED_SQLALCHEMY_DEPENDENCY).specifier.contains(
            sqlalchemy_version
        ):
            problems.append(
                f"SQLAlchemy {sqlalchemy_version} is outside "
                f"{REQUIRED_SQLALCHEMY_DEPENDENCY}"
            )

        # Corroboration, not the provenance proof: the installed canonical root is
        # byte-identical to the one in the wheel this run built. The report check
        # above is what rules out an identical same-version artifact from an index.
        with zipfile.ZipFile(core_wheel) as archive:
            built_root = archive.read("omnivia_core/__init__.py")
        installed_roots = list(
            venv_dir.glob("lib/python3.*/site-packages/omnivia_core/__init__.py")
        )
        if len(installed_roots) != 1:
            problems.append(f"expected one installed omnivia_core root, got {installed_roots}")
        elif installed_roots[0].read_bytes() != built_root:
            problems.append("the installed omnivia_core is not the artifact this run built")

        # The installed metadata still carries the deprecation notice and the exact
        # requirements -- the wheel headers survived the install round trip.
        for fragment in REQUIRED_SUMMARY_FRAGMENTS:
            if fragment not in installed["summary"]:
                problems.append(
                    f"the installed Summary is missing {fragment!r}: "
                    f"{installed['summary']!r}"
                )
        if f"Migration, {EXPECTED_MIGRATION_URL}" not in installed["project_urls"]:
            problems.append(
                f"the installed metadata does not publish the migration URL: "
                f"{installed['project_urls']}"
            )
        runtime = [Requirement(raw) for raw in installed["requires"]]
        unconditional = [req for req in runtime if req.marker is None]
        if sorted(req.name for req in unconditional) != ["omnivia-core", "sqlalchemy"]:
            problems.append(
                f"the installed unconditional requirements are "
                f"{sorted(req.name for req in unconditional)}"
            )
        else:
            for expected_requirement in (
                REQUIRED_CORE_DEPENDENCY,
                REQUIRED_SQLALCHEMY_DEPENDENCY,
            ):
                wanted = Requirement(expected_requirement)
                (actual,) = [req for req in unconditional if req.name == wanted.name]
                if actual.specifier != wanted.specifier:
                    problems.append(f"{actual} != {expected_requirement}")
        for fragment in REQUIRED_README_FRAGMENTS:
            if fragment not in installed["description"]:
                problems.append(f"the installed description is missing {fragment!r}")
        print("  installed metadata and dependency contract checked")

        audit = _run(
            [str(venv_python), "-I", "-c", installed_root_audit_script()],
            timeout=300,
            cwd=workdir,
        )
        if audit.returncode != 0:
            raise ResolverSmokeError(f"{audit.stdout}\n{audit.stderr}")
        if audit.stdout.strip() != "OK" or audit.stderr != "":
            problems.append(
                f"the installed-root audit did not report OK: "
                f"{audit.stdout!r} / {audit.stderr!r}"
            )
        print("  47 legacy and 47 canonical route modules imported from the installed tree")
        print("  installed-root identity and export audit passed")

    if problems:
        print(
            f"\nFAIL: {len(problems)} problem(s) in the current-index resolver smoke:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        "OK: current-index resolver smoke passed (not a locked or published-artifact "
        "supply-chain proof)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ResolverSmokeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
