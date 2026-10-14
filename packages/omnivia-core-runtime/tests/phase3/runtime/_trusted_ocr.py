"""Trusted OCR model directories a test builds and owns.

Production fails closed: the packaged catalogue names no model directory and
installs no model, so recognition cannot run until an installation configures
one explicitly. Tests therefore never lean on an ambient ``TESSDATA_PREFIX`` or
on whatever languages a host happens to have. They build a directory of their
own, pin the real SHA-256 of the files they put in it, and hand the resulting
catalogue in - the same explicit path-plus-digest route an installation takes.

A test may trust its host explicitly, which is what :func:`host_models` is for:
it copies the host's installed models into the directory the test owns, so the
bytes that get digested are the bytes the engine will open.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytesseract
from omnivia_core_runtime.service import ocr_resources as ocr

MODEL_SUFFIX = ".traineddata"


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def catalogue_over(directory: Path, *names: str) -> ocr.TrustedOcrCatalogue:
    """A catalogue pinning the real digests of the named files in ``directory``."""
    return ocr.TrustedOcrCatalogue(
        installed={
            name: digest_of(directory / f"{name}{MODEL_SUFFIX}") for name in names
        },
        tessdata_dir=str(directory),
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
    try:
        listed = subprocess.run(
            [pytesseract.pytesseract.tesseract_cmd, "--list-langs"],
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
