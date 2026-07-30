"""Deprecation metadata for the compatibility distribution, and the structural
pins for the resolver smoke that deliberately runs *outside* pytest.

Two separate things live here, and both are deliberately distinct from
``tests/compatibility/test_facade_wheel_install.py``, whose offline
``--no-index --no-deps`` artifact install stays exactly what it was:

1. **Deprecation metadata (ADR-036).** ``omnivia-memory`` must announce that it
   is a deprecated compatibility facade for ``omnivia-core``, name the canonical
   ``omnivia_core`` import, and carry the pinned migration URL -- checked as an
   exact literal and screened for placeholder spellings, never requested -- in the
   built wheel's ``METADATA``, and in the README that ships as that metadata's
   description. And it must do all of that without any import-time behaviour: no
   ``warnings.warn``, no logging, no print, nothing on stdout or stderr.

2. **Structural pins for ``scripts/check-root-facade-resolver.py``.** The
   resolving install, the pip ``--report`` provenance proof, the installed
   metadata round trip and the installed-root audit all live in that standalone
   script, not in a test module. That is the point: it performs a resolving install
   that may contact the configured index, and the acceptance workflow runs ``python
   -m pytest tests/canonical_migration tests/compatibility -q`` *and* ``python -m
   pytest -q`` in the same job, so a pytest test would execute that install three
   times. Keeping it in
   ``scripts/`` -- outside ``testpaths``, under a filename no pytest pattern
   matches -- makes "exactly once, in its own fail-closed step" a structural fact
   rather than a convention, and the tests below pin exactly that.

   The script's own critical behaviour -- that it fails closed unless pip's report
   says the ``omnivia-core`` artifact it installed *is* the wheel that run built,
   named by a ``file://`` URL with a strictly empty authority and carrying a present,
   well-formed SHA-256 matching those bytes, and unless the report attributes
   SQLAlchemy to an ``http``/``https`` candidate URL with a nonempty parsed host
   component, a usable port and no host whitespace or ASCII control characters -- is
   unit-tested here against synthetic reports, so it is proved without a second
   resolving install.

   That last rule is syntactic, and the tests below claim no more than it does. They
   pin which URL spellings the check accepts and which it rejects; none of them
   assert -- and none could, against a synthetic report -- that the host resolves in
   DNS or exists, that the artifact installed was the right one, that it was not
   served from a cache, or that any network transfer took place.

   Those tests are adversarial on purpose: they include the reports that would slip
   past a check written against ``urlparse`` results too loosely -- a
   ``file://host/...`` URL whose path equals the local wheel's, an ``https`` URL
   whose authority is non-empty but names no host, one whose host parses cleanly
   while its port is nonnumeric or out of range (``hostname`` never looks at the
   port, so the ``ValueError`` lands outside a check that reads only the host), and
   one whose host is truthy but blank or spelled with control characters, which
   ``urllib`` permits.
   Each rule is pinned from both sides: the rejections, and the ordinary domains,
   IPv4 and IPv6 literals, authenticated URLs and valid ports that must still pass.

The gate itself is labelled a **current-index resolver smoke**, not a locked or
published-artifact supply-chain proof. The script's docstring states its limits in
full; this module restates only the ones its own unit tests could be misread as
establishing.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = REPO_ROOT
MEMORY_DIR = REPO_ROOT / "services" / "omnivia-memory"
RESOLVER_SCRIPT = REPO_ROOT / "scripts" / "check-root-facade-resolver.py"
PYTHON = sys.executable


def _load_resolver_module() -> ModuleType:
    """Load the gate script as a module -- its filename is not an identifier, so it
    cannot be imported normally.

    Loading it is what lets the constants below have a single home: the script
    owns them, this module reuses them, and the two cannot drift apart.
    """
    spec = importlib.util.spec_from_file_location(
        "check_root_facade_resolver", RESOLVER_SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resolver = _load_resolver_module()

EXPECTED_VERSION = resolver.EXPECTED_VERSION
REQUIRED_CORE_DEPENDENCY = resolver.REQUIRED_CORE_DEPENDENCY
REQUIRED_SQLALCHEMY_DEPENDENCY = resolver.REQUIRED_SQLALCHEMY_DEPENDENCY
EXPECTED_MIGRATION_URL = resolver.EXPECTED_MIGRATION_URL
REQUIRED_SUMMARY_FRAGMENTS = resolver.REQUIRED_SUMMARY_FRAGMENTS
REQUIRED_README_FRAGMENTS = resolver.REQUIRED_README_FRAGMENTS

#: Names that must not appear anywhere in the packaged root: a runtime
#: deprecation notice is exactly what ADR-036 forbids.
FORBIDDEN_ROOT_RUNTIME_TOKENS = ("warnings", "logging", "print(", "sys.stderr", "sys.stdout")

#: The bounded diagnostic the gate reports when pip's report does not attribute
#: SQLAlchemy to an ``http(s)`` candidate URL with a usable host. Pinned in one place
#: because the wording is the claim: it says what the report did not say, and nothing
#: about a fetch, a DNS resolution, an index being contacted, or bytes moving.
UNATTRIBUTED_SQLALCHEMY_PROBLEM = (
    "pip's report did not attribute SQLAlchemy to an http(s) candidate URL with a "
    "nonempty usable host component"
)


def _child_env() -> dict[str, str]:
    """The repo suite runs with ``PYTHONPATH`` pointing at this source tree. Drop
    it for every child process so builds see only their own environment."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _build_wheel(project_dir: Path, wheelhouse: Path) -> Path:
    before = set(wheelhouse.glob("*.whl"))
    result = subprocess.run(
        [
            PYTHON, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(wheelhouse), str(project_dir),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        f"failed to build a wheel for {project_dir}:\n{result.stdout}\n{result.stderr}"
    )
    (built,) = set(wheelhouse.glob("*.whl")) - before
    return built


def _metadata(wheel_path: Path) -> str:
    with zipfile.ZipFile(wheel_path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


def _headers(metadata: str) -> list[tuple[str, str]]:
    """``(name, value)`` for every metadata header, in order, stopping at the body."""
    headers: list[tuple[str, str]] = []
    for line in metadata.splitlines():
        if not line.strip():
            break
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
    return headers


def _header(metadata: str, name: str) -> list[str]:
    return [value for key, value in _headers(metadata) if key == name]


def _description(metadata: str) -> str:
    """The metadata body -- the shipped README."""
    _, _, body = metadata.partition("\n\n")
    return body


def _requires_dist(metadata: str) -> list[str]:
    return _header(metadata, "Requires-Dist")


class _Artifacts:
    """The two freshly built wheels."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.wheelhouse = workdir / "wheelhouse"
        self.wheelhouse.mkdir()
        self.core_wheel = _build_wheel(CORE_DIR, self.wheelhouse)
        self.memory_wheel = _build_wheel(MEMORY_DIR, self.wheelhouse)
        assert self.core_wheel.name.startswith("omnivia_core-")
        assert self.memory_wheel.name.startswith("omnivia_memory-")

    @property
    def memory_metadata(self) -> str:
        return _metadata(self.memory_wheel)


@pytest.fixture(scope="module")
def artifacts() -> Iterator[_Artifacts]:
    """Build both wheels once for this module, outside the repository tree."""
    with tempfile.TemporaryDirectory(prefix="omnivia-root-facade-dist-") as raw:
        yield _Artifacts(Path(raw))


# ---------------------------------------------------------------------------
# Deprecation metadata in the built wheel.
# ---------------------------------------------------------------------------


def test_wheel_summary_announces_the_deprecated_facade(artifacts: _Artifacts) -> None:
    """The ``Summary`` is what an index listing and ``pip show`` display, so it is
    where the notice has to be. It must say the distribution is deprecated, that it
    is a compatibility facade for ``omnivia-core``, and name the canonical
    ``omnivia_core`` import."""
    summaries = _header(artifacts.memory_metadata, "Summary")
    assert len(summaries) == 1, summaries
    summary = summaries[0]
    for fragment in REQUIRED_SUMMARY_FRAGMENTS:
        assert fragment in summary, f"Summary is missing {fragment!r}: {summary!r}"


def test_wheel_declares_the_pinned_migration_project_url(artifacts: _Artifacts) -> None:
    """The exact ``Project-URL`` the distribution must publish, pinned as a literal so
    the migration guidance is named rather than a placeholder. Nothing here requests
    the URL, so this fixes what is published, not that anything answers it."""
    urls = _header(artifacts.memory_metadata, "Project-URL")
    assert urls == [f"Migration, {EXPECTED_MIGRATION_URL}"], urls


def test_migration_url_is_not_a_placeholder() -> None:
    """Pinned as its own fact: a stand-in would satisfy "has a Project-URL" while
    telling a consumer nothing. This screens the spelling for known placeholder
    tokens; it does not check that the URL is live."""
    assert EXPECTED_MIGRATION_URL.startswith("https://github.com/claytonread/omnivia-core/")
    assert "services/omnivia-memory" in EXPECTED_MIGRATION_URL
    for placeholder in ("example.com", "TODO", "TBD", "localhost", "FIXME", "..."):
        assert placeholder not in EXPECTED_MIGRATION_URL


def test_wheel_retains_the_exact_declared_requirements(artifacts: _Artifacts) -> None:
    """The deprecation metadata must not have disturbed the dependency contract:
    exactly one ``omnivia-core`` requirement carrying the sanctioned range, exactly
    one ``sqlalchemy`` requirement, and everything else gated on an extra."""
    parsed = [Requirement(raw) for raw in _requires_dist(artifacts.memory_metadata)]
    runtime = [req for req in parsed if req.marker is None]
    for req in parsed:
        if req.marker is not None:
            assert "extra" in str(req.marker), str(req)
    assert sorted(req.name for req in runtime) == ["omnivia-core", "sqlalchemy"]
    for expected in (REQUIRED_CORE_DEPENDENCY, REQUIRED_SQLALCHEMY_DEPENDENCY):
        wanted = Requirement(expected)
        (actual,) = [req for req in runtime if req.name == wanted.name]
        assert actual.specifier == wanted.specifier, f"{actual} != {expected}"


def test_wheel_description_ships_the_migration_guidance(artifacts: _Artifacts) -> None:
    """The README is the distribution's description, so the migration guidance
    travels with the artifact rather than living only in the repository. Its
    obligations are checked one by one: canonical dependency and import examples, a
    support window of at least two scheduled release trains, removal only in a
    major release, and consumer update guidance."""
    description = _description(artifacts.memory_metadata)
    assert description.strip(), "the wheel ships no description"
    for fragment in REQUIRED_README_FRAGMENTS:
        assert fragment in description, f"the shipped README is missing {fragment!r}"


def test_packaged_root_has_no_import_time_notice(artifacts: _Artifacts) -> None:
    """The other half of ADR-036: the notice is metadata and guidance, never
    behaviour. Checked against the bytes actually packaged, so a runtime warning
    added after the source-side test would still be caught here."""
    with zipfile.ZipFile(artifacts.memory_wheel) as archive:
        source = archive.read("omnivia_memory/__init__.py").decode("utf-8")
    for token in FORBIDDEN_ROOT_RUNTIME_TOKENS:
        assert token not in source, f"the packaged root mentions {token!r}"
    assert "__all__" in source


# ---------------------------------------------------------------------------
# The resolver smoke lives outside pytest, exactly once.
# ---------------------------------------------------------------------------


def _resolver_source() -> str:
    return RESOLVER_SCRIPT.read_text(encoding="utf-8")


def _pytest_config() -> dict[str, object]:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tool = document.get("tool", {})
    assert isinstance(tool, dict)
    config = tool.get("pytest", {}).get("ini_options", {})
    assert isinstance(config, dict)
    return config


def test_resolver_smoke_is_a_standalone_script_outside_pytest_collection() -> None:
    """The structural argument that the network smoke cannot run more than once.

    Both broad pytest commands in the acceptance workflow are either ``pytest``
    with no arguments -- which collects ``testpaths`` -- or ``pytest`` pointed at
    directories under ``tests/``. So it is enough, and it is checked here, that:
    the gate is a script in ``scripts/``; ``scripts`` is not a ``testpaths`` entry
    and contains neither of those directories; the script's filename matches
    neither of pytest's default ``python_files`` patterns; and the project declares
    no ``python_files`` override that could widen them.
    """
    assert RESOLVER_SCRIPT.is_file()
    assert os.access(RESOLVER_SCRIPT, os.X_OK), "the gate script is not executable"

    config = _pytest_config()
    assert config["testpaths"] == ["services", "tests"]
    assert "python_files" not in config, (
        "a python_files override could make scripts/ collectable; the argument above "
        "assumes pytest's defaults"
    )
    for entry in config["testpaths"]:
        assert not str(RESOLVER_SCRIPT).startswith(str(REPO_ROOT / str(entry)))
    assert not RESOLVER_SCRIPT.name.startswith("test_")
    assert not RESOLVER_SCRIPT.name.endswith("_test.py")


def test_no_pytest_collected_test_performs_a_resolving_install() -> None:
    """The other half of "exactly once": nothing pytest *does* collect may reach an
    index either.

    Every ``pip install`` command literal in every collected test module must carry
    ``--no-index``. The offline artifact install in
    ``tests/compatibility/test_facade_wheel_install.py`` is the only thing this
    finds today, and it qualifies; a resolving install added to a test module later
    fails here rather than silently tripling the network work.
    """
    checked = 0
    offenders: list[str] = []
    for directory in ("tests", "services"):
        for path in sorted((REPO_ROOT / directory).rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                literals = [
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
                if "pip" not in literals or "install" not in literals:
                    continue
                checked += 1
                if "--no-index" not in literals:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == [], f"resolving pip installs inside pytest collection: {offenders}"
    assert checked == 2, (
        f"expected the two offline installs in test_facade_wheel_install.py, found "
        f"{checked} pip install command literals"
    )


def test_resolver_script_is_fail_closed_with_no_offline_fallback() -> None:
    """The gate must not be able to report a pass for a proof it did not run: no
    skip path, no ``--no-index``/``--no-deps`` fallback, and the resolving install
    is the only install it performs."""
    source = _resolver_source()
    tree = ast.parse(source, filename=str(RESOLVER_SCRIPT))

    install_literals = [
        [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
    ]
    installs = [
        literal
        for literal in install_literals
        if "pip" in literal and "install" in literal
    ]
    assert len(installs) == 1, installs
    (install,) = installs
    for forbidden in ("--no-index", "--no-deps"):
        assert forbidden not in install, forbidden
    for required in ("--find-links", "--report", "--timeout", "--retries"):
        assert required in install, required

    # No skip path, and no import of pytest at all: a gate that could be skipped is
    # not fail-closed.
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "pytest" not in imported
    for token in ("pytest.skip", "skipif", "importorskip"):
        assert token not in source, token


def test_every_resolver_subprocess_has_an_explicit_timeout() -> None:
    """A hung index or a wedged child must fail the gate rather than sit on the
    job's budget until the runner kills it, so every child process is bounded."""
    tree = ast.parse(_resolver_source(), filename=str(RESOLVER_SCRIPT))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "run")
            or (isinstance(node.func, ast.Name) and node.func.id == "_run")
        )
    ]
    assert calls, "the gate runs no subprocess at all"
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "timeout" in keywords, f"unbounded subprocess call at line {call.lineno}"


# ---------------------------------------------------------------------------
# The provenance proof, unit-tested against synthetic pip reports.
# ---------------------------------------------------------------------------


def _wheelhouse(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A wheelhouse holding a stand-in Core and compatibility wheel."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    core = wheelhouse / "omnivia_core-0.1.0-py3-none-any.whl"
    memory = wheelhouse / "omnivia_memory-0.1.0-py3-none-any.whl"
    core.write_bytes(b"core-wheel-bytes")
    memory.write_bytes(b"memory-wheel-bytes")
    return wheelhouse, core, memory


def _report(core_url: str, memory_url: str, sqlalchemy_url: str, **core_extra: object) -> dict:
    download: dict[str, object] = {"url": core_url}
    download.update(core_extra)
    return {
        "version": "1",
        "install": [
            {"metadata": {"name": "omnivia-core", "version": "0.1.0"}, "download_info": download},
            {
                "metadata": {"name": "omnivia_memory", "version": "0.1.0"},
                "download_info": {"url": memory_url},
            },
            {
                "metadata": {"name": "SQLAlchemy", "version": "2.0.36"},
                "download_info": {"url": sqlalchemy_url},
            },
        ],
    }


INDEX_URL = "https://files.pythonhosted.org/packages/ab/sqlalchemy-2.0.36-py3-none-any.whl"


def _good_core_hash(core: Path) -> dict[str, object]:
    """The ``archive_info`` block pip records for a local wheel it installed.

    Spelled once and reused, so a test that is *not* about the hash always supplies
    a correct one and can assert on the single problem it is actually probing.
    """
    return {"archive_info": {"hashes": {"sha256": resolver._sha256(core)}}}


def _with_authority(file_uri: str, authority: str) -> str:
    """``file_uri`` rewritten to carry ``authority``, keeping its path byte for byte.

    That is what makes the tests below adversarial rather than decorative: the path
    component still names the local wheel exactly, so a check that compared paths
    after parsing -- and never looked at the authority -- would accept every URL this
    produces. Only the authority distinguishes them from the one pip writes.
    """
    assert file_uri.startswith("file:///"), file_uri
    return "file://" + authority + file_uri.removeprefix("file://")


def test_report_provenance_accepts_the_locally_built_core_wheel(tmp_path: Path) -> None:
    """The passing shape, so the failures below are not vacuous: the ``omnivia-core``
    entry names the exact local wheel, its recorded hash matches those bytes, and the
    SQLAlchemy entry carries an ``http(s)`` candidate URL with a usable host."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(core.as_uri(), memory.as_uri(), INDEX_URL, **_good_core_hash(core))
    assert resolver._check_report_provenance(report, core, memory, wheelhouse) == []


def test_report_provenance_accepts_the_exact_path_uri_python_writes(
    tmp_path: Path,
) -> None:
    """The strict empty-authority rule is not stricter than reality: ``Path.as_uri``
    -- the form pip records for a wheel it took from ``--find-links`` -- writes an
    empty authority, so requiring one rejects nothing legitimate.

    Pinned separately from the passing shape above because it is the *reason* the
    rejections below are safe rather than a second assertion about the same report.
    """
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    for local_wheel in (core, memory):
        parsed = urlparse(local_wheel.as_uri())
        assert parsed.scheme == "file"
        assert parsed.netloc == "", parsed.netloc
    report = _report(core.as_uri(), memory.as_uri(), INDEX_URL, **_good_core_hash(core))
    assert resolver._check_report_provenance(report, core, memory, wheelhouse) == []
    assert resolver._url_path(core.as_uri()) == core.resolve()
    assert resolver._url_path(memory.as_uri()) == memory.resolve()


def test_report_provenance_rejects_a_same_version_core_wheel_from_an_index(
    tmp_path: Path,
) -> None:
    """The failure the byte comparison alone cannot catch. An index carrying a
    same-version ``omnivia-core`` wheel with identical content would install a root
    byte-identical to the local one; only the resolved *URL* distinguishes them."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(
        "https://files.pythonhosted.org/packages/cd/omnivia_core-0.1.0-py3-none-any.whl",
        memory.as_uri(),
        INDEX_URL,
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert "not a local file:// URL" in problems[0]


def test_report_provenance_rejects_a_different_local_core_wheel(tmp_path: Path) -> None:
    """A ``file://`` URL is not enough either: it must be *this* run's artifact, not
    a stale wheel left in some other directory."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    stale = tmp_path / "stale" / core.name
    stale.parent.mkdir()
    stale.write_bytes(core.read_bytes())
    report = _report(stale.as_uri(), memory.as_uri(), INDEX_URL)
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert "not the wheel this run built" in problems[0]


def test_report_provenance_rejects_a_remote_authority_core_file_url(
    tmp_path: Path,
) -> None:
    """The fail-open a path comparison alone would have: ``file://host/<exact path>``
    names a file on *another host*, yet its path component equals the local wheel's to
    the byte, so parsing it and comparing only the path would call a remote artifact
    "the wheel this run built". The authority has to be empty, and it is checked
    before the comparison rather than after it.
    """
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    remote = _with_authority(core.as_uri(), "wheels.example.invalid")
    assert urlparse(remote).path == urlparse(core.as_uri()).path
    report = _report(remote, memory.as_uri(), INDEX_URL, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert "not a local file:// URL" in problems[0]
    assert "empty authority" in problems[0]


def test_report_provenance_rejects_a_remote_authority_memory_file_url(
    tmp_path: Path,
) -> None:
    """The same fail-open on the compatibility wheel's entry, pinned separately: the
    two URLs are checked by the same helper but through different call sites, and an
    authority rule applied to only one of them would leave the other open."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    remote = _with_authority(memory.as_uri(), "wheels.example.invalid")
    assert urlparse(remote).path == urlparse(memory.as_uri()).path
    report = _report(core.as_uri(), remote, INDEX_URL, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert "not a local file:// URL" in problems[0]
    assert "empty authority" in problems[0]


@pytest.mark.parametrize(
    "authority",
    [
        pytest.param("localhost", id="localhost"),
        pytest.param("127.0.0.1", id="loopback-address"),
        pytest.param("localhost:8080", id="localhost-with-port"),
    ],
)
def test_report_provenance_rejects_a_localhost_authority_file_url(
    tmp_path: Path, authority: str
) -> None:
    """"Here, spelled differently" is rejected too. RFC 8089 does permit ``localhost``
    as a synonym for the local host, so this is a deliberate choice rather than an
    oversight: pip writes what ``Path.as_uri`` writes, an empty authority, so
    admitting a second spelling would widen the shapes a report can claim a local
    artifact in without proving anything more -- and admitting a *host* at all is the
    door the remote-authority cases above walk through.
    """
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(
        _with_authority(core.as_uri(), authority),
        memory.as_uri(),
        INDEX_URL,
        **_good_core_hash(core),
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert "not a local file:// URL" in problems[0]
    assert resolver._url_path(_with_authority(core.as_uri(), authority)) is None


def test_report_provenance_rejects_a_mismatched_recorded_hash(tmp_path: Path) -> None:
    """When pip records a hash for the artifact it installed, that hash has to be
    the local wheel's, so a report naming the right path but describing different
    bytes fails."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(
        core.as_uri(),
        memory.as_uri(),
        INDEX_URL,
        archive_info={"hashes": {"sha256": "0" * 64}},
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert "hashes to" in problems[0]


def test_report_provenance_rejects_a_missing_recorded_hash(tmp_path: Path) -> None:
    """The exact local wheel URL is the primary proof, but it is not the whole
    contract: a report that records no hash at all corroborated nothing about the
    bytes, so it fails closed rather than being waived as "pip did not say"."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(core.as_uri(), memory.as_uri(), INDEX_URL)
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert "no usable sha256" in problems[0]


@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param("0" * 63, id="too-short"),
        pytest.param("0" * 65, id="too-long"),
        pytest.param("z" * 64, id="non-hex"),
        pytest.param("", id="empty"),
        pytest.param(None, id="null"),
        pytest.param(0, id="not-a-string"),
    ],
)
def test_report_provenance_rejects_a_malformed_recorded_hash(
    tmp_path: Path, recorded: object
) -> None:
    """A hash field that is present but not a 64-character hexadecimal digest is
    unusable, and unusable is rejected: it is reported as an absent proof rather
    than compared and mis-reported as a byte mismatch."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(
        core.as_uri(),
        memory.as_uri(),
        INDEX_URL,
        archive_info={"hashes": {"sha256": recorded}},
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert "no usable sha256" in problems[0]


def test_report_provenance_accepts_an_upper_case_recorded_hash(tmp_path: Path) -> None:
    """The digest is a value, not a string. An upper-case spelling names the same
    bytes, so it passes -- pinned so the strictness above is understood to be about
    the hash being usable and matching, not about its casing."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(
        core.as_uri(),
        memory.as_uri(),
        INDEX_URL,
        archive_info={"hashes": {"sha256": resolver._sha256(core).upper()}},
    )
    assert resolver._check_report_provenance(report, core, memory, wheelhouse) == []


def test_report_provenance_rejects_a_locally_satisfied_sqlalchemy(tmp_path: Path) -> None:
    """A report that attributes SQLAlchemy to the local wheelhouse has not attributed
    it to an ``http(s)`` candidate URL with a usable host, which is the whole of what
    the check reads. The diagnostic names the wheelhouse file it recorded, so the
    failure is diagnosable, and claims nothing about what was or was not fetched."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    local_sqlalchemy = wheelhouse / "SQLAlchemy-2.0.36-py3-none-any.whl"
    local_sqlalchemy.write_bytes(b"sqlalchemy")
    report = _report(
        core.as_uri(), memory.as_uri(), local_sqlalchemy.as_uri(), **_good_core_hash(core)
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]
    assert "local wheelhouse file" in problems[0]


def test_report_provenance_rejects_a_local_sqlalchemy_outside_the_wheelhouse(
    tmp_path: Path,
) -> None:
    """The wheelhouse is not the only place a local wheel can live. A ``file://``
    SQLAlchemy from anywhere else is still not an ``http(s)`` candidate URL, so
    "outside the wheelhouse" must not be a way past this check -- and it is reported
    by the other branch, which quotes the recorded URL rather than calling it a
    wheelhouse file."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    local_sqlalchemy = elsewhere / "SQLAlchemy-2.0.36-py3-none-any.whl"
    local_sqlalchemy.write_bytes(b"sqlalchemy")
    report = _report(
        core.as_uri(), memory.as_uri(), local_sqlalchemy.as_uri(), **_good_core_hash(core)
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]
    assert "local wheelhouse file" not in problems[0]


@pytest.mark.parametrize(
    "sqlalchemy_url",
    [
        pytest.param("", id="empty"),
        pytest.param("ftp://example.invalid/sqlalchemy-2.0.36-py3-none-any.whl", id="ftp"),
        pytest.param("git+ssh://example.invalid/sqlalchemy.git", id="vcs"),
        pytest.param("sqlalchemy-2.0.36-py3-none-any.whl", id="bare-path"),
    ],
)
def test_report_provenance_rejects_a_non_http_sqlalchemy_url(
    tmp_path: Path, sqlalchemy_url: str
) -> None:
    """Anything that is not an ``http``/``https`` candidate fails closed, including
    an empty URL: the only thing this check claims is that pip's report attributed
    the dependency to an ``http(s)`` candidate URL, and none of these are one."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(core.as_uri(), memory.as_uri(), sqlalchemy_url, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]


def test_report_provenance_rejects_an_http_sqlalchemy_url_without_a_host(
    tmp_path: Path,
) -> None:
    """``http:///name.whl`` parses with the right scheme and no network location at
    all, so the scheme alone is not the check: a host is required too."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(
        core.as_uri(),
        memory.as_uri(),
        "https:///sqlalchemy-2.0.36-py3-none-any.whl",
        **_good_core_hash(core),
    )
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]


@pytest.mark.parametrize(
    "sqlalchemy_url",
    [
        pytest.param(
            "https://user@/sqlalchemy-2.0.36-py3-none-any.whl", id="userinfo-only"
        ),
        pytest.param(
            "https://user:secret@/sqlalchemy-2.0.36-py3-none-any.whl",
            id="userinfo-with-password-only",
        ),
        pytest.param("https://:8080/sqlalchemy-2.0.36-py3-none-any.whl", id="port-only"),
        pytest.param(
            "https://user@:8080/sqlalchemy-2.0.36-py3-none-any.whl",
            id="userinfo-and-port-only",
        ),
    ],
)
def test_report_provenance_rejects_an_https_authority_that_names_no_host(
    tmp_path: Path, sqlalchemy_url: str
) -> None:
    """A non-empty authority is not a host, and this is the fail-open that follows from
    treating it as one: every URL here has a truthy ``netloc`` -- ``'user@'``,
    ``':8080'`` -- while ``hostname`` is ``None``, so none of them names a host at
    all. The check reads the parsed host, not the raw authority.
    """
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    parsed = urlparse(sqlalchemy_url)
    assert parsed.netloc, "this case is only interesting if the authority is non-empty"
    assert parsed.hostname is None
    report = _report(core.as_uri(), memory.as_uri(), sqlalchemy_url, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]


@pytest.mark.parametrize(
    "sqlalchemy_url",
    [
        pytest.param(
            "https://[::1/sqlalchemy-2.0.36-py3-none-any.whl", id="unterminated-ipv6"
        ),
        pytest.param(
            "https://[fe80::1%25eth0/sqlalchemy-2.0.36-py3-none-any.whl",
            id="unterminated-scoped-ipv6",
        ),
        pytest.param(
            "file://[::1/sqlalchemy-2.0.36-py3-none-any.whl", id="unterminated-ipv6-file"
        ),
    ],
)
def test_report_provenance_rejects_a_malformed_sqlalchemy_authority(
    tmp_path: Path, sqlalchemy_url: str
) -> None:
    """An authority ``urllib`` cannot parse must fail the gate, not crash it.

    ``urlparse`` raises ``ValueError`` on these, so an unguarded ``urlparse(...)`` or
    ``.hostname`` read would abort the whole provenance check with a traceback -- which
    is not the same as a recorded failure and, in a run that collected problems for
    reporting, loses them. The raise is caught and reported as "no host": fail closed.
    """
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    with pytest.raises(ValueError):
        urlparse(sqlalchemy_url)
    report = _report(core.as_uri(), memory.as_uri(), sqlalchemy_url, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]


@pytest.mark.parametrize(
    ("sqlalchemy_url", "reason"),
    [
        pytest.param(
            "https://index.example.com:notaport/sqlalchemy-2.0.36-py3-none-any.whl",
            "cast",
            id="nonnumeric-port",
        ),
        pytest.param(
            "https://index.example.com:99999/sqlalchemy-2.0.36-py3-none-any.whl",
            "out of range",
            id="out-of-range-port",
        ),
        pytest.param(
            "https://index.example.com:65536/sqlalchemy-2.0.36-py3-none-any.whl",
            "out of range",
            id="just-out-of-range-port",
        ),
    ],
)
def test_report_provenance_rejects_a_sqlalchemy_url_with_an_unusable_port(
    tmp_path: Path, sqlalchemy_url: str, reason: str
) -> None:
    """A bad port is a *delayed* raise, which is the trap: ``urlparse`` accepts these
    URLs and ``hostname`` returns ``'index.example.com'`` without ever looking at the
    port, so a check that read only the host would call each of them a usable index
    candidate -- and the ``ValueError`` would then surface from whatever read the port
    next, as an unhandled crash rather than a recorded problem. Reading the port
    inside the same guard is what makes them fail closed here.
    """
    parsed = urlparse(sqlalchemy_url)
    assert parsed.hostname == "index.example.com", (
        "this case is only interesting if the host alone looks perfectly usable"
    )
    with pytest.raises(ValueError, match=reason):
        _ = parsed.port

    assert resolver._index_hostname(sqlalchemy_url) is None

    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(core.as_uri(), memory.as_uri(), sqlalchemy_url, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]


@pytest.mark.parametrize(
    "sqlalchemy_url",
    [
        pytest.param("https://   /sqlalchemy-2.0.36-py3-none-any.whl", id="blank-host"),
        pytest.param(
            "https://index example.com/sqlalchemy-2.0.36-py3-none-any.whl",
            id="embedded-space",
        ),
        pytest.param(
            "https://index\x0b.example.com/sqlalchemy-2.0.36-py3-none-any.whl",
            id="vertical-tab",
        ),
        pytest.param(
            "https://index\x00.example.com/sqlalchemy-2.0.36-py3-none-any.whl", id="nul"
        ),
        pytest.param(
            "https://index\x07.example.com/sqlalchemy-2.0.36-py3-none-any.whl", id="bell"
        ),
        pytest.param(
            "https://index\x1f.example.com/sqlalchemy-2.0.36-py3-none-any.whl",
            id="unit-separator",
        ),
        pytest.param(
            "https://index\x7f.example.com/sqlalchemy-2.0.36-py3-none-any.whl", id="del"
        ),
    ],
)
def test_report_provenance_rejects_a_blank_or_control_character_host(
    tmp_path: Path, sqlalchemy_url: str
) -> None:
    """``urllib`` permits these, which is the whole problem. It strips tab, newline
    and carriage return from a URL before parsing, so those never reach the host --
    but a space, a vertical tab, a NUL, a DEL or any other control byte is passed
    through untouched, and ``hostname`` hands back a *truthy* string that is not a
    usable host name. A whitespace-only host is the plainest case: it
    is the empty authority the checks above reject, wearing three spaces.
    """
    parsed = urlparse(sqlalchemy_url)
    assert parsed.hostname, "this case is only interesting if urllib permits the host"

    assert resolver._index_hostname(sqlalchemy_url) is None

    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(core.as_uri(), memory.as_uri(), sqlalchemy_url, **_good_core_hash(core))
    problems = resolver._check_report_provenance(report, core, memory, wheelhouse)
    assert len(problems) == 1, problems
    assert UNATTRIBUTED_SQLALCHEMY_PROBLEM in problems[0]


@pytest.mark.parametrize(
    ("index_url", "expected_host"),
    [
        pytest.param(INDEX_URL, "files.pythonhosted.org", id="pypi-cdn"),
        pytest.param("http://index.example.com/x.whl", "index.example.com", id="no-port"),
        pytest.param("https://index.example.com:1/x.whl", "index.example.com", id="port-1"),
        pytest.param(
            "https://index.example.com:8443/x.whl", "index.example.com", id="port-8443"
        ),
        pytest.param(
            "https://index.example.com:65535/x.whl",
            "index.example.com",
            id="highest-valid-port",
        ),
        pytest.param(
            "https://token:x@index.example.com:8443/x.whl",
            "index.example.com",
            id="authenticated-with-port",
        ),
        pytest.param("https://192.0.2.10:8443/x.whl", "192.0.2.10", id="ipv4-with-port"),
        pytest.param(
            "https://[2606:4700::1]:8443/x.whl", "2606:4700::1", id="ipv6-with-port"
        ),
    ],
)
def test_index_hostname_accepts_well_formed_hosts_and_valid_ports(
    index_url: str, expected_host: str
) -> None:
    """The other side of the two rules above, asserted on the helper directly: the
    port read must not reject a *valid* port, and the character rule must not reject a
    host that legitimately contains none of the forbidden characters. Ordinary
    domains, IPv4 and IPv6 literals, userinfo, no port, the lowest and the highest
    valid port all still yield their host component -- so the strictness costs correct
    installs nothing.

    Accepting a URL here means it is well formed. These hosts are not looked up, and
    ``index.example.com`` is a documentation name precisely so that nothing about
    this test depends on any of them existing.
    """
    assert resolver._index_hostname(index_url) == expected_host


@pytest.mark.parametrize(
    "sqlalchemy_url",
    [
        pytest.param(INDEX_URL, id="pypi-cdn"),
        pytest.param(
            "http://index.example.com/sqlalchemy-2.0.36-py3-none-any.whl", id="plain-http"
        ),
        pytest.param(
            "https://index.example.com:8443/sqlalchemy-2.0.36-py3-none-any.whl",
            id="host-with-port",
        ),
        pytest.param(
            "https://token:x@index.example.com/sqlalchemy-2.0.36-py3-none-any.whl",
            id="authenticated-index",
        ),
        pytest.param(
            "https://[2606:4700::1]/sqlalchemy-2.0.36-py3-none-any.whl", id="ipv6-literal"
        ),
    ],
)
def test_report_provenance_accepts_index_urls_that_name_a_host(
    tmp_path: Path, sqlalchemy_url: str
) -> None:
    """The other side of the host rule: every URL shape an index candidate takes still
    passes. An authenticated private index carries userinfo *and* a host, a mirror may
    carry a port, and a literal address is a host -- rejecting any of these would make
    the gate fail for correct installs rather than for fail-open reports. Passing here
    is a statement about the recorded URL's shape, not about the host existing.
    """
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    assert urlparse(sqlalchemy_url).hostname
    report = _report(core.as_uri(), memory.as_uri(), sqlalchemy_url, **_good_core_hash(core))
    assert resolver._check_report_provenance(report, core, memory, wheelhouse) == []


def test_report_provenance_rejects_a_report_without_an_entry(tmp_path: Path) -> None:
    """Fail closed on a report that simply does not mention a required
    distribution: an absent entry must raise, never read as "nothing wrong"."""
    wheelhouse, core, memory = _wheelhouse(tmp_path)
    report = _report(core.as_uri(), memory.as_uri(), INDEX_URL, **_good_core_hash(core))
    report["install"] = [
        entry for entry in report["install"] if entry["metadata"]["name"] != "SQLAlchemy"
    ]
    with pytest.raises(resolver.ResolverSmokeError, match="install entries"):
        resolver._check_report_provenance(report, core, memory, wheelhouse)


# ---------------------------------------------------------------------------
# The contract the audit script embeds.
# ---------------------------------------------------------------------------


def test_the_route_registry_supplies_exactly_47_module_pairs() -> None:
    """The generated audit imports "all 47 registered route modules" on both sides
    and checks 182 advertised owners, so the counts it embeds are pinned here rather
    than left implicit in a generated string."""
    manifest = resolver.load_manifest()
    assert len(manifest.legacy_modules) == 47
    assert len(manifest.canonical_modules) == 47
    assert len(set(manifest.legacy_modules)) == 47
    assert len(set(manifest.canonical_modules)) == 47
    assert manifest.legacy_modules[0] == "omnivia_memory"
    assert manifest.canonical_modules[0] == "omnivia_core"
    assert len(resolver.root_owner_map()) == 182

    script = resolver.installed_root_audit_script()
    for fragment in (
        "assert len(LEGACY_MODULES) == 47",
        "assert len(CANONICAL_MODULES) == 47",
        "assert checked == 182",
        'assert list(canonical_root.__all__) == ["__version__"]',
    ):
        assert fragment in script, fragment
