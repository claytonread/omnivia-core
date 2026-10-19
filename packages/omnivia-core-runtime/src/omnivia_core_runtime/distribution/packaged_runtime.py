"""The packaged document runtime: the engines, models and notices Core ships.

Design 11.2 packages the RTF reader and the OCR engine *with the distribution*:
"no capture may run pip, download a parser/model, launch Office or fetch
document references", and 11.12.2 is explicit that "the engine/model directory
comes from the signed installation manifest, never from a document". This module
is the one place that layout, that manifest section and the verification of both
are written down, so the packager that produces a candidate and the service that
runs out of one cannot disagree about either.

**One manifest, checked in both directions.** The component-set manifest
``companion-manifest.json`` gains a ``resources`` member: one entry per packaged
file, carrying its role, its app-relative path, its exact SHA-256, its exact
size and the build identity the packager established for it.
:func:`resource_findings` checks that every declared entry is present at exactly
that path with exactly those bytes, *and* that every file under the packaged
runtime root is declared. Tampering, omission, an extra declared field, an extra
file, a symlink and a path escape are each a finding, and a finding is fatal:
there is no partial pass and nothing here rewrites a candidate.

**The payload is relocatable and offline.** :func:`stage_packaged_runtime`
copies the two executables and then the recursive closure of their non-system
Mach-O dependencies, rewrites every load command to ``@loader_path``-relative
form, deletes every ``LC_RPATH`` so nothing is looked up by search path at all,
and re-seals each rewritten file ad hoc. What is left pointing outside the
payload is exactly the Apple system libraries and frameworks, which are part of
the OS and are deliberately not vendored. Nothing resolves through Homebrew,
MacPorts, ``PATH``, ``DYLD_*`` or the network.

The ad-hoc seal is not a release signature and is never presented as one. On
Apple silicon a Mach-O whose load commands have been rewritten will not execute
until it carries *some* valid signature, so sealing is what makes the staged
bytes runnable at all -- the sources arrive ad-hoc sealed from their own build
for the same reason. The candidate stays ``qualification_only`` and unsigned in
the component-set sense: no designated identity, no notarisation, no release
eligibility. A later signing owner re-signs these same files.

**Resolution is from verified installation state, never from a document.**
:func:`installed_runtime` looks in one fixed place under the installation root
the service was started against, verifies every declared byte, and returns
absolute paths. Anything else -- an absent payload, a missing file, a digest
that moved -- is ``None``, and the caller keeps the fail-closed behaviour it had
before a payload existed.

**What the manifest is not.** 11.12.2 calls for a *signed* installation
manifest, and this one is not signed: the digests sit in plain JSON beside the
payload. That is enough to catch a corrupted or substituted file, and enough to
make a swap require rewriting the manifest too -- but an attacker who can write
both can declare whatever they install. Closing it needs the release owner WP-00
binding ledger row 13 leaves unbound, the same owner the signing identity in
``build-core-companion-app.py`` waits on;
:mod:`omnivia_core_runtime.distribution.model_manifest_trust` already holds the
Ed25519 verification that would be applied. Recorded in the DOC-13 ledger entry
rather than papered over here.

Standard library only, and no relative imports: the packaging script loads this
module by location, exactly as the bundle verifier loads the packaging script.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

#: The companion bundle this payload ships inside, and the component-set
#: manifest that binds it. Declared here rather than in the packaging script so
#: that the script, the bundle verifier and the service all read one spelling.
BUNDLE_NAME: Final = "OmniVia Core"
MANIFEST_NAME: Final = "companion-manifest.json"

#: The payload's root inside the companion bundle, and its four subdirectories.
#: Deterministic and app-relative: nothing in the manifest, and nothing the
#: service resolves, depends on where the candidate happens to sit.
RUNTIME_DIR: Final = "omnivia-runtime"
BIN_DIR: Final = "bin"
LIB_DIR: Final = "lib"
TESSDATA_DIR: Final = "tessdata"
NOTICE_DIR: Final = "licenses"
PAYLOAD_DIRS: Final = (BIN_DIR, LIB_DIR, TESSDATA_DIR, NOTICE_DIR)

#: Where an installer places the verified candidate, under the installation-state
#: root the service is started against (``--installation-state``). That root is
#: already the one trusted pointer this process is given; the payload is resolved
#: relative to it so no second configuration surface -- and no caller-supplied
#: engine path -- exists at all.
INSTALLED_CANDIDATE_DIR: Final = "companion"

PANDOC_EXECUTABLE: Final = "pandoc"
TESSERACT_EXECUTABLE: Final = "tesseract"
MODEL_SUFFIX: Final = ".traineddata"
#: English and the orientation/script data, which 11.2 and 17.3 bundle. Managed
#: additional languages are installed separately and are not packaged here.
REQUIRED_MODELS: Final = ("eng", "osd")
#: The packaged model family of 11.2. The exact identity of these bytes is their
#: digest, which the manifest pins and verification re-reads; this records which
#: family they were packaged from.
MODEL_IDENTITY: Final = "tessdata_fast"

ROLE_PANDOC: Final = "pandoc"
ROLE_TESSERACT: Final = "tesseract"
MODEL_ROLE: Final = "model:"
LIBRARY_ROLE: Final = "library:"
NOTICE_ROLE: Final = "notice:"

#: Every field a resource entry carries, and the only ones. A declaration with a
#: field outside this set did not come from this packager and is refused rather
#: than read for the parts of it that look familiar -- the same rule the
#: component-set manifest applies to itself (section 18.3).
RESOURCE_FIELDS: Final = frozenset({"identity", "path", "role", "sha256", "size"})

#: How the payload's Mach-O files reach each other once staged, and what seals
#: them. Both are recorded in the manifest so a reader knows what was done to
#: these bytes without inspecting them.
RELOCATION: Final = "loader_path_relative"
PAYLOAD_SEAL: Final = "ad_hoc"

#: Bounds. A real payload holds the two executables, a few dozen libraries, two
#: models and a handful of notices; these exist so an unreadable or hostile tree
#: cannot make verification run unbounded.
MAX_RESOURCES: Final = 512
MAX_IDENTITY_CHARS: Final = 200
MAX_PATH_CHARS: Final = 512
_DIGEST_BLOCK: Final = 1 << 20
#: How long one packaging or identity command may take before it is abandoned.
_COMMAND_TIMEOUT_SECONDS: Final = 300

#: Prefixes of the Apple libraries and frameworks that stay external. They are
#: part of the operating system, are not redistributable, and are the only
#: dependencies a staged file is allowed to resolve outside the payload.
SYSTEM_LIBRARY_PREFIXES: Final = ("/usr/lib/", "/System/")

_SHA256: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_IDENTITY: Final = re.compile(r"\A[\x20-\x7e]{1,200}\Z")
_LANGUAGE: Final = re.compile(r"\A[a-z]{3}(?:_[a-z]{2,5})?\Z")
_LEAF: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\Z")
#: ``otool -l`` writes an ``LC_RPATH``'s value as ``path <value> (offset N)``.
_RPATH_VALUE: Final = re.compile(r"\A\s*path (.+) \(offset \d+\)\s*\Z")


class PackagedRuntimeRefused(Exception):
    """The packaged runtime could not be staged, or could not be verified."""


@dataclass(frozen=True)
class PackagedResource:
    """One packaged file, exactly as the manifest declares it."""

    role: str
    path: str
    sha256: str
    size: int
    identity: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class VerifiedPackagedRuntime:
    """The packaged engines and models, as absolute paths over verified bytes.

    Every path here was read out of a manifest whose every declared byte had
    just been digested, so handing one to an engine invokes the bytes the
    installation shipped. ``models`` maps each packaged language to the size and
    digest the manifest pinned, which is what lets a recognition selection be
    identified without re-deriving it from a directory listing.
    """

    root: Path
    pandoc: Path
    tesseract: Path
    tessdata_dir: Path
    models: Mapping[str, tuple[int, str]]


def runtime_root(candidate: Path) -> Path:
    """The payload root inside one assembled candidate."""
    return (
        candidate / f"{BUNDLE_NAME}.app" / "Contents" / "Resources" / RUNTIME_DIR
    )


def _relative(*parts: str) -> str:
    """One payload path, relative to the candidate root, in POSIX spelling."""
    return "/".join(
        (f"{BUNDLE_NAME}.app", "Contents", "Resources", RUNTIME_DIR, *parts)
    )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_DIGEST_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Verification: the manifest section, checked in both directions
# ---------------------------------------------------------------------------


def _expected_path(role: str) -> str | None:
    """The one path a role may be declared at, or ``None`` if the role is unknown.

    A declared path that is not exactly this one is a finding, which is what
    makes a path escape unrepresentable rather than merely detected: there is no
    spelling of ``..`` that this function returns.
    """
    if role == ROLE_PANDOC:
        return _relative(BIN_DIR, PANDOC_EXECUTABLE)
    if role == ROLE_TESSERACT:
        return _relative(BIN_DIR, TESSERACT_EXECUTABLE)
    if role.startswith(MODEL_ROLE):
        language = role[len(MODEL_ROLE) :]
        if _LANGUAGE.match(language) is None:
            return None
        return _relative(TESSDATA_DIR, f"{language}{MODEL_SUFFIX}")
    if role.startswith(LIBRARY_ROLE):
        leaf = role[len(LIBRARY_ROLE) :]
        if _LEAF.match(leaf) is None or not leaf.endswith(".dylib"):
            return None
        return _relative(LIB_DIR, leaf)
    if role.startswith(NOTICE_ROLE):
        leaf = role[len(NOTICE_ROLE) :]
        if _LEAF.match(leaf) is None:
            return None
        return _relative(NOTICE_DIR, leaf)
    return None


def _entry_findings(entry: object, index: int) -> tuple[list[str], str | None]:
    """One declaration's own findings, and the path it claims when it is usable."""
    findings: list[str] = []
    if not isinstance(entry, Mapping):
        return [f"packaged resource declaration malformed: entry {index}"], None
    for field in sorted(set(entry) - RESOURCE_FIELDS):
        findings.append(
            f"packaged resource field not bound by this schema: entry {index}.{field}"
        )
    for field in sorted(RESOURCE_FIELDS - set(entry)):
        findings.append(f"packaged resource field missing: entry {index}.{field}")
    if findings:
        return findings, None
    role = entry["role"]
    expected = _expected_path(role) if isinstance(role, str) else None
    if expected is None:
        return [f"packaged resource role not managed: entry {index}"], None
    path = entry["path"]
    if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or path != expected:
        findings.append(f"packaged resource path mismatch: {role}")
    digest = entry["sha256"]
    if not isinstance(digest, str) or _SHA256.match(digest) is None:
        findings.append(f"packaged resource digest malformed: {role}")
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        findings.append(f"packaged resource size malformed: {role}")
    identity = entry["identity"]
    if not isinstance(identity, str) or _IDENTITY.match(identity) is None:
        findings.append(f"packaged resource identity malformed: {role}")
    return findings, (expected if not findings else None)


def _declared_files(root: Path) -> Iterator[Path]:
    """Every file under the payload root, following no link into one.

    A directory that is itself a link is yielded as the entry it is rather than
    descended into: it is not a payload directory and is refused as one.
    """
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in sorted(entries, key=lambda found: found.name):
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                else:
                    yield path


def resource_findings(candidate: Path, declared: object) -> list[str]:
    """Check one candidate's packaged payload against its declared resources.

    Fail-closed and bounded, and strict in both directions: a declared resource
    that is absent, is a link, has moved, or whose bytes or size differ is a
    finding, and so is a file inside the payload that nothing declares. An empty
    declaration is a candidate with no packaged runtime, which stays coherent --
    today's companion-only candidate -- and is refused the moment the payload
    root exists anyway.

    No finding reproduces bytes; each names a role or a relative path.
    """
    if not isinstance(declared, list):
        return ["manifest declares no packaged resource set"]
    if len(declared) > MAX_RESOURCES:
        return [f"packaged resource set exceeds {MAX_RESOURCES} entries"]
    findings: list[str] = []
    paths: dict[str, int] = {}
    roles: set[str] = set()
    for index, entry in enumerate(declared):
        entry_findings, path = _entry_findings(entry, index)
        findings.extend(entry_findings)
        if path is None:
            continue
        assert isinstance(entry, Mapping)
        role = str(entry["role"])
        if role in roles:
            findings.append(f"packaged resource declared twice: {role}")
            continue
        roles.add(role)
        paths[path] = index
        absolute = candidate / PurePosixPath(path)
        try:
            observed = absolute.lstat()
        except OSError:
            findings.append(f"packaged resource missing: {role}")
            continue
        if not stat.S_ISREG(observed.st_mode):
            # A link, a directory, a device or a FIFO. The bytes a digest was
            # taken over have to be the bytes the loader opens.
            findings.append(f"packaged resource is not a regular file: {role}")
            continue
        if observed.st_size != entry["size"]:
            findings.append(f"packaged resource size differs: {role}")
            continue
        if _digest(absolute) != entry["sha256"]:
            findings.append(f"packaged resource digest differs: {role}")

    root = runtime_root(candidate)
    if root.is_symlink():
        return [*findings, "packaged runtime root is a link"]
    if root.is_dir():
        try:
            observed_files = list(_declared_files(root))
            observed_dirs = sorted(
                entry.name
                for entry in os.scandir(root)
                if entry.is_dir(follow_symlinks=False)
            )
        except OSError:
            return [*findings, "packaged runtime root is unreadable"]
        if len(observed_files) > MAX_RESOURCES:
            return [*findings, f"packaged runtime holds over {MAX_RESOURCES} files"]
        for name in observed_dirs:
            if name not in PAYLOAD_DIRS:
                # An empty directory holds no file to be found undeclared, so the
                # shape of the payload is checked as well as its contents.
                findings.append(f"undeclared directory in the packaged runtime: {name}")
        for present in observed_files:
            relative = present.relative_to(candidate).as_posix()
            if relative not in paths:
                findings.append(f"undeclared file in the packaged runtime: {relative}")
    elif roles:
        findings.append("the declared packaged runtime root is absent")

    if roles:
        for required in (ROLE_PANDOC, ROLE_TESSERACT):
            if required not in roles:
                findings.append(f"packaged runtime declares no {required}")
        for language in REQUIRED_MODELS:
            if f"{MODEL_ROLE}{language}" not in roles:
                findings.append(f"packaged runtime declares no {language} model")
        if not any(role.startswith(NOTICE_ROLE) for role in roles):
            findings.append("packaged runtime declares no licence notice")
    return findings


def verified_runtime(candidate: Path, declared: object) -> VerifiedPackagedRuntime:
    """The payload of one candidate whose every declared byte has just been read.

    Raises :class:`PackagedRuntimeRefused` on the first thing that is not
    exactly as declared, carrying the findings rather than a partial answer.
    """
    findings = resource_findings(candidate, declared)
    if findings:
        raise PackagedRuntimeRefused("; ".join(findings[:8]))
    assert isinstance(declared, list)
    models: dict[str, tuple[int, str]] = {}
    for entry in declared:
        role = str(entry["role"])
        if role.startswith(MODEL_ROLE):
            models[role[len(MODEL_ROLE) :]] = (int(entry["size"]), str(entry["sha256"]))
    if not models:
        raise PackagedRuntimeRefused("the candidate packages no runtime")
    root = runtime_root(candidate)
    return VerifiedPackagedRuntime(
        root=root,
        pandoc=root / BIN_DIR / PANDOC_EXECUTABLE,
        tesseract=root / BIN_DIR / TESSERACT_EXECUTABLE,
        tessdata_dir=root / TESSDATA_DIR,
        models=models,
    )


def verify_candidate_runtime(candidate: Path) -> VerifiedPackagedRuntime:
    """Read one assembled candidate's manifest and verify the payload it declares.

    The manifest is read for the one member this module owns and nothing else:
    the component set, the versions and the signing state are the bundle
    verifier's business. Raises :class:`PackagedRuntimeRefused` for an
    unreadable manifest exactly as for a byte that moved -- an installation
    whose own record cannot be read has no verified payload.
    """
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        raise PackagedRuntimeRefused("the candidate root is not a usable directory")
    manifest_path = candidate / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PackagedRuntimeRefused("the candidate carries no component-set manifest")
    unreadable: str | None = None
    manifest: object = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        unreadable = "the component-set manifest is unreadable"
    if unreadable is not None:
        raise PackagedRuntimeRefused(unreadable)
    if not isinstance(manifest, dict):
        raise PackagedRuntimeRefused("the component-set manifest is unreadable")
    return verified_runtime(candidate, manifest.get("resources"))


def installed_runtime(installation_root: Path | None) -> VerifiedPackagedRuntime | None:
    """This installation's verified packaged runtime, or ``None`` when it has none.

    The one resolution production performs, and it consults exactly one place:
    :data:`INSTALLED_CANDIDATE_DIR` under the installation-state root the service
    was started against. No ``PATH`` is searched, no environment variable is
    read, no caller nominates a path and nothing a document carries is involved.

    ``None`` is every case other than a payload whose every declared byte was
    just verified -- no installation root, no candidate placed there, a manifest
    that does not decode, a file that is absent or a digest that moved. The
    caller keeps the fail-closed behaviour it has without a payload: no engine
    is invoked and no model hash is claimed.
    """
    if installation_root is None:
        return None
    candidate = Path(installation_root).absolute() / INSTALLED_CANDIDATE_DIR
    try:
        return verify_candidate_runtime(candidate)
    except (PackagedRuntimeRefused, OSError):
        return None


# ---------------------------------------------------------------------------
# Packaging: stage the closure, relocate it, seal it, declare it
# ---------------------------------------------------------------------------


def _run(command: Sequence[str]) -> str:
    """One packaging tool, or a refusal. Every tool used here ships with macOS."""
    failure: str | None = None
    output = ""
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            failure = f"{command[0]} refused {command[-1]}"
        output = completed.stdout
    except (OSError, subprocess.SubprocessError):
        failure = f"{command[0]} could not be run"
    if failure is not None:
        raise PackagedRuntimeRefused(failure)
    return output


def _mach_o_references(path: Path) -> tuple[str, ...]:
    """Every non-system library this Mach-O file loads, as it spells them.

    Apple's own libraries and frameworks are left out: they are the operating
    system, they are not vendored, and they are the only thing a staged file is
    allowed to resolve outside the payload.
    """
    lines = _run(["otool", "-L", str(path)]).splitlines()[1:]
    return tuple(
        name
        for name in (line.strip().split(" (")[0] for line in lines)
        if name and not name.startswith(SYSTEM_LIBRARY_PREFIXES)
    )


def _mach_o_rpaths(path: Path) -> tuple[str, ...]:
    lines = _run(["otool", "-l", str(path)]).splitlines()
    found: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for following in lines[index + 1 : index + 5]:
            value = _RPATH_VALUE.match(following)
            if value is not None:
                found.append(value.group(1))
                break
    return tuple(found)


def _resolved_reference(reference: str, referrer: Path) -> Path | None:
    """Where one load command actually points, from the referrer's own position.

    Resolution is always against the *source* file, never against a copy already
    staged: a copy's ``@loader_path`` means a different directory, and resolving
    there would silently miss a dependency or find the wrong one.
    """
    holder = str(referrer.parent)
    if reference.startswith("@rpath/"):
        leaf = reference[len("@rpath/") :]
        for rpath in _mach_o_rpaths(referrer):
            base = rpath.replace("@loader_path", holder).replace(
                "@executable_path", holder
            )
            candidate = Path(os.path.realpath(os.path.join(base, leaf)))
            if candidate.is_file():
                return candidate
        return None
    for anchor in ("@loader_path/", "@executable_path/"):
        if reference.startswith(anchor):
            candidate = Path(
                os.path.realpath(os.path.join(holder, reference[len(anchor) :]))
            )
            return candidate if candidate.is_file() else None
    candidate = Path(os.path.realpath(reference))
    return candidate if candidate.is_file() else None


def _library_closure(executables: Sequence[Path]) -> dict[str, tuple[Path, str]]:
    """Every non-system library the two executables need, transitively.

    Keyed by the leaf name the load commands use, which is the name the staged
    file is given, so a collision is two different builds claiming one name. It
    is detected by digest and refused: staging either one would leave the other
    executable loading bytes it was not built against.
    """
    closure: dict[str, tuple[Path, str]] = {}
    pending = [Path(os.path.realpath(executable)) for executable in executables]
    while pending:
        source = pending.pop()
        for reference in _mach_o_references(source):
            resolved = _resolved_reference(reference, source)
            if resolved is None:
                raise PackagedRuntimeRefused(
                    f"a dependency of {source.name} could not be resolved"
                )
            leaf = PurePosixPath(reference).name
            if _LEAF.match(leaf) is None or not leaf.endswith(".dylib"):
                raise PackagedRuntimeRefused(
                    f"a dependency of {source.name} is not a usable library name"
                )
            digest = _digest(resolved)
            held = closure.get(leaf)
            if held is not None:
                if held[1] != digest:
                    raise PackagedRuntimeRefused(
                        f"two different builds of {leaf} are needed at once"
                    )
                continue
            closure[leaf] = (resolved, digest)
            pending.append(resolved)
    return closure


def _relocate(path: Path, *, prefix: str) -> None:
    """Point one staged file's load commands inside the payload, and seal it.

    Every non-system reference becomes ``@loader_path``-relative, every library
    is given its own relative identity, and every ``LC_RPATH`` is deleted: after
    this nothing is looked up by search path, so no Homebrew, MacPorts or
    ``DYLD_*`` directory can answer for a dependency.

    The seal comes last and is required rather than cosmetic. Rewriting load
    commands invalidates whatever signature the file arrived with, and on Apple
    silicon the loader refuses a Mach-O with an invalid one -- so without this
    the relocated payload would not run at all. It is an ad-hoc seal, which
    carries no identity and is not a release signature.
    """
    arguments: list[str] = []
    for reference in _mach_o_references(path):
        arguments += ["-change", reference, f"{prefix}/{PurePosixPath(reference).name}"]
    if prefix == "@loader_path":
        arguments += ["-id", f"@loader_path/{path.name}"]
    for rpath in _mach_o_rpaths(path):
        arguments += ["-delete_rpath", rpath]
    if arguments:
        _run(["install_name_tool", *arguments, str(path)])
    _run(["codesign", "--force", "--sign", "-", str(path)])


def _build_identity(executable: Path) -> str:
    """What one staged engine says it is, asked of the staged bytes themselves.

    Taken after relocation and sealing, so the identity recorded in the manifest
    belongs to the file that will actually run and not to the source it was
    copied from. A version line is bounded and stripped of anything outside
    printable ASCII, because it goes into a manifest read with ``ascii``.
    """
    first = (_run([str(executable), "--version"]).splitlines() or [""])[0]
    identity = "".join(
        character for character in first.strip() if "\x20" <= character <= "\x7e"
    )[:MAX_IDENTITY_CHARS]
    if not identity:
        raise PackagedRuntimeRefused(f"{executable.name} reports no build identity")
    return identity


def _staged(source: Path, target: Path, mode: int) -> None:
    """Copy one trusted input into the payload, following and creating no link."""
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise PackagedRuntimeRefused(f"packaging input refused: {target.name}")
    shutil.copyfile(source, target, follow_symlinks=False)
    os.chmod(target, mode)


def stage_packaged_runtime(
    candidate: Path,
    *,
    pandoc: Path,
    tesseract: Path,
    models: Mapping[str, Path],
    notices: Mapping[str, Path],
) -> tuple[PackagedResource, ...]:
    """Assemble the packaged runtime inside one candidate and declare every byte.

    The inputs are explicit trusted paths the packager was given; nothing is
    discovered, downloaded or searched for. Each is copied -- never linked --
    into its deterministic place, the Mach-O closure is staged and relocated
    beside the executables, and the returned declarations carry the role, path,
    digest, size and build identity of the files as they now stand.

    The digests are taken *after* relocation and sealing, so the manifest pins
    the bytes that run rather than the bytes that were copied.
    """
    missing = sorted(set(REQUIRED_MODELS) - set(models))
    if missing:
        raise PackagedRuntimeRefused(f"packaged models missing: {', '.join(missing)}")
    for language in models:
        if _LANGUAGE.match(language) is None:
            raise PackagedRuntimeRefused("a packaged model names no usable language")
    if not notices:
        raise PackagedRuntimeRefused("the packaged runtime needs its licence notices")
    for name in notices:
        if _LEAF.match(name) is None:
            raise PackagedRuntimeRefused("a licence notice names no usable file")

    root = runtime_root(candidate)
    for directory in PAYLOAD_DIRS:
        (root / directory).mkdir(parents=True, mode=0o755)

    staged_pandoc = root / BIN_DIR / PANDOC_EXECUTABLE
    staged_tesseract = root / BIN_DIR / TESSERACT_EXECUTABLE
    _staged(pandoc, staged_pandoc, 0o755)
    _staged(tesseract, staged_tesseract, 0o755)

    closure = _library_closure((pandoc, tesseract))
    if len(closure) + len(models) + len(notices) + 2 > MAX_RESOURCES:
        raise PackagedRuntimeRefused("the packaged runtime exceeds its resource bound")
    libraries: dict[str, Path] = {}
    for leaf, (source, _digest_of_source) in sorted(closure.items()):
        target = root / LIB_DIR / leaf
        _staged(source, target, 0o644)
        libraries[leaf] = target

    # Executables first: a library's own identity is rewritten in its own pass,
    # and an executable's references are rewritten against the names it used.
    for executable in (staged_pandoc, staged_tesseract):
        _relocate(executable, prefix=f"@loader_path/../{LIB_DIR}")
    for target in libraries.values():
        _relocate(target, prefix="@loader_path")

    declared: list[PackagedResource] = [
        PackagedResource(
            role=ROLE_PANDOC,
            path=_relative(BIN_DIR, PANDOC_EXECUTABLE),
            sha256=_digest(staged_pandoc),
            size=staged_pandoc.stat().st_size,
            identity=_build_identity(staged_pandoc),
        ),
        PackagedResource(
            role=ROLE_TESSERACT,
            path=_relative(BIN_DIR, TESSERACT_EXECUTABLE),
            sha256=_digest(staged_tesseract),
            size=staged_tesseract.stat().st_size,
            identity=_build_identity(staged_tesseract),
        ),
    ]
    for leaf, target in sorted(libraries.items()):
        declared.append(
            PackagedResource(
                role=f"{LIBRARY_ROLE}{leaf}",
                path=_relative(LIB_DIR, leaf),
                sha256=_digest(target),
                size=target.stat().st_size,
                # The upstream install name these bytes were built with, which is
                # the provenance of the build they came from.
                identity=_source_identity(closure[leaf][0]),
            )
        )
    for language, source in sorted(models.items()):
        leaf = f"{language}{MODEL_SUFFIX}"
        target = root / TESSDATA_DIR / leaf
        _staged(source, target, 0o644)
        declared.append(
            PackagedResource(
                role=f"{MODEL_ROLE}{language}",
                path=_relative(TESSDATA_DIR, leaf),
                sha256=_digest(target),
                size=target.stat().st_size,
                identity=MODEL_IDENTITY,
            )
        )
    for name, source in sorted(notices.items()):
        target = root / NOTICE_DIR / name
        _staged(source, target, 0o644)
        declared.append(
            PackagedResource(
                role=f"{NOTICE_ROLE}{name}",
                path=_relative(NOTICE_DIR, name),
                sha256=_digest(target),
                size=target.stat().st_size,
                identity="licence_notice",
            )
        )
    return tuple(declared)


def _source_identity(source: Path) -> str:
    """The install name one library was built with, bounded for the manifest."""
    name = _run(["otool", "-D", str(source)]).splitlines()
    identity = (name[-1].strip() if len(name) > 1 else source.name)[
        :MAX_IDENTITY_CHARS
    ]
    return identity or source.name


__all__ = [
    "BIN_DIR",
    "BUNDLE_NAME",
    "INSTALLED_CANDIDATE_DIR",
    "LIBRARY_ROLE",
    "LIB_DIR",
    "MANIFEST_NAME",
    "MAX_RESOURCES",
    "MODEL_IDENTITY",
    "MODEL_ROLE",
    "MODEL_SUFFIX",
    "NOTICE_DIR",
    "NOTICE_ROLE",
    "PANDOC_EXECUTABLE",
    "PAYLOAD_DIRS",
    "PAYLOAD_SEAL",
    "RELOCATION",
    "REQUIRED_MODELS",
    "RESOURCE_FIELDS",
    "ROLE_PANDOC",
    "ROLE_TESSERACT",
    "RUNTIME_DIR",
    "SYSTEM_LIBRARY_PREFIXES",
    "TESSDATA_DIR",
    "TESSERACT_EXECUTABLE",
    "PackagedResource",
    "PackagedRuntimeRefused",
    "VerifiedPackagedRuntime",
    "installed_runtime",
    "resource_findings",
    "runtime_root",
    "stage_packaged_runtime",
    "verified_runtime",
    "verify_candidate_runtime",
]
