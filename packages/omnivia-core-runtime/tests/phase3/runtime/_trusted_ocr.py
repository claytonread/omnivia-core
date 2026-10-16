"""Trusted engine and model resources a test builds and owns.

Production fails closed: the packaged catalogue names no model directory and
installs no model, so recognition cannot run until an installation configures
one explicitly. Tests therefore never lean on an ambient ``TESSDATA_PREFIX`` or
on whatever languages a host happens to have. They build a directory of their
own, pin the real SHA-256 of the files they put in it, and hand the resulting
catalogue in - the same explicit path-plus-digest route an installation takes.

A test may trust its host explicitly, which is what :func:`host_models` is for:
it copies the host's installed models into the directory the test owns, so the
bytes that get digested are the bytes the engine will open.

The *engine* is pinned the same way. Production resolves an absolute executable
out of the verified installation payload and :func:`ocr.run_tesseract` refuses
anything less, so a catalogue a test builds names one too: the host's own
absolute binary where there is one, and otherwise an absolute path that does not
exist -- which is correct for the many selections whose engine is injected and
whose model bytes are stand-ins, because neither ever reaches a process.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from omnivia_core_runtime.distribution.packaged_runtime import VerifiedPackagedRuntime
from omnivia_core_runtime.service import ocr_resources as ocr

MODEL_SUFFIX = ".traineddata"
#: An absolute path no host has. A catalogue that names it resolves exactly as a
#: packaged one does and is never invoked, which is what the selections built
#: over stand-in models want.
ABSENT_ENGINE = "/nonexistent/omnivia-test/tesseract"


def host_tesseract() -> str | None:
    """The host's own absolute Tesseract executable, when it has one.

    Resolved once and by absolute path. It deliberately does not read
    ``pytesseract.pytesseract.tesseract_cmd``: production assigns that module
    global the packaged engine it was handed, so a test that has already run one
    recognition would otherwise discover whatever the last selection pinned.
    """
    found = shutil.which("tesseract")
    return None if found is None else str(Path(found).resolve())


HOST_TESSERACT = host_tesseract()

#: The same arrangement for the pinned RTF reader, which production also takes
#: as an absolute path out of the verified payload. A test that mocks the reader
#: only needs the path to be absolute; one that runs the real binary needs the
#: host's, and skips without it.
ABSENT_READER = "/nonexistent/omnivia-test/pandoc"


def host_pandoc() -> str | None:
    """The host's own absolute Pandoc executable, when it has one."""
    found = shutil.which("pandoc")
    return None if found is None else str(Path(found).resolve())


HOST_PANDOC = host_pandoc()


def reader_settings(
    settings: ocr.ExtractionSettings | None = None, *, pandoc_path: str | None = None
) -> ocr.ExtractionSettings:
    """``settings`` with an absolute pinned RTF reader bound, as production binds one.

    Defaults to the explicit text-only profile, which is what ``extract_rtf``
    itself defaults to, so a scan test keeps the behaviour it had and only gains
    the reader it now has to be given.
    """
    base = settings if settings is not None else ocr.text_only_settings()
    return replace(base, pandoc_path=pandoc_path or HOST_PANDOC or ABSENT_READER)


def host_runtime(
    tessdata_dir: Path, models: Mapping[str, tuple[int, str]] = {}
) -> VerifiedPackagedRuntime | None:
    """A payload over this host's own engines, as a verified manifest names one.

    Production reads these paths out of a candidate manifest whose every byte it
    has just digested. A test that needs real recognition or a real RTF read
    supplies the host's own binaries by the same absolute-path route, and
    returns ``None`` -- so the caller skips -- when the host has neither.

    ``models`` is empty by default, which is the installation whose *engines* are
    packaged and whose *languages* come from the managed cache: the packaged
    catalogue then serves no language and every selection goes through the
    cache's own projection, invoked through the packaged engine.
    """
    if HOST_TESSERACT is None or HOST_PANDOC is None:  # pragma: no cover - host
        return None
    return VerifiedPackagedRuntime(
        root=Path("/"),
        pandoc=Path(HOST_PANDOC),
        tesseract=Path(HOST_TESSERACT),
        tessdata_dir=tessdata_dir,
        models=models,
    )


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def catalogue_over(
    directory: Path, *names: str, tesseract_path: str | None = None
) -> ocr.TrustedOcrCatalogue:
    """A catalogue pinning the real digests of the named files in ``directory``."""
    return ocr.TrustedOcrCatalogue(
        installed={
            name: digest_of(directory / f"{name}{MODEL_SUFFIX}") for name in names
        },
        tessdata_dir=str(directory),
        tesseract_path=tesseract_path or HOST_TESSERACT or ABSENT_ENGINE,
    )


def stand_in_models(directory: Path, *names: str) -> Path:
    """Placeholder model bytes, for selections that must never reach the engine.

    Resource identity, verification and refusal are all byte-level, so stand-in
    bytes exercise them exactly. Only a test that recognises real text needs
    :func:`host_models`.
    """
    for name in names:
        (directory / f"{name}{MODEL_SUFFIX}").write_bytes(f"model-{name}".encode())
    return directory


def host_tessdata(*names: str) -> Path | None:
    """The host's own model directory, when it holds every named model.

    ``None`` means this host cannot run the real engine for those languages, and
    the caller skips rather than degrading into some weaker assertion.
    """
    if HOST_TESSERACT is None:  # pragma: no cover - host dependent
        return None
    try:
        listed = subprocess.run(
            [HOST_TESSERACT, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - host dependent
        return None
    found = re.search(r'"([^"]+)"', listed.stdout + listed.stderr)
    if found is None:  # pragma: no cover - host dependent
        return None
    directory = Path(found.group(1))
    if not all((directory / f"{name}{MODEL_SUFFIX}").is_file() for name in names):
        return None  # pragma: no cover - host dependent
    return directory


def host_models(directory: Path, *names: str) -> Path | None:
    """Copy the host's named models into ``directory``, or ``None`` without them.

    The copy is deliberate: a link is refused by verification, and the trusted
    directory has to hold the regular files whose digests the catalogue pins.
    """
    source = host_tessdata(*names)
    if source is None:  # pragma: no cover - host dependent
        return None
    for name in names:
        model = f"{name}{MODEL_SUFFIX}"
        shutil.copyfile(source / model, directory / model)
    return directory
