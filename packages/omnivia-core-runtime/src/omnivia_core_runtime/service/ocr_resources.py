"""Trusted selection of the packaged local OCR engine, models and languages.

Nothing here ever reads a document.  A language list arrives from trusted
configuration (an installation setting, or a verified model manifest's artifact
names) and is checked against what the signed installation actually installed.

The production default is **unconfigured and fails closed**: it installs no
model and names no model directory, so it can never claim a packaged model hash
while the engine resolves some arbitrary host ``tessdata``.  A usable catalogue
is built from an already-verified model manifest plus an explicit trusted
``tessdata`` directory, and every selected ``.traineddata`` file is checked -
present, a regular file, and SHA-256 equal to the manifest - *before* the engine
is invoked.  Only the selected names are touched: no directory is scanned for
whatever languages a host happens to have, and no model is ever downloaded.

A selection that cannot be served that way returns :class:`OcrResources` with
``available`` false, carrying the requested order and the exact reason, so the
caller can report **Needs OCR language** without substituting English and
without pretending unavailable model bytes were used. An *explicitly requested*
language is reported as the language it is missing even when nothing at all is
configured, because installing it is what recovers either case; the narrower
"no trusted model directory" fault is kept alongside as the operator's detail.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import pytesseract  # type: ignore[import-untyped]
from pytesseract import Output

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as PILImage

    from omnivia_core_runtime.distribution.model_manifest_trust import (
        VerifiedModelManifest,
    )

#: Processing modes of ExtractionProfileV1 (storage keeps the same closed pair).
MODE_TEXT_ONLY: Final = "text_only"
MODE_TEXT_AND_LOCAL_OCR: Final = "text_and_local_ocr"

#: The optional ExtractionProfileV1 ``ocr.resourceState`` member. It is absent
#: when the pinned models were verified, so a verified profile keeps the digest
#: it has always had, and present only to say the resources were *not* served.
RESOURCE_STATE_UNAVAILABLE: Final = "unavailable"

TESSERACT_ENGINE: Final = "tesseract"
TESSERACT_REVISION: Final = "5.5.3"
#: Tesseract's LSTM-only engine mode; the packaged models are tessdata_fast.
TESSERACT_OEM: Final = 1
DEFAULT_LANGUAGE: Final = "eng"
#: Orientation/script data. It is not a recognition language and cannot be selected as one.
ORIENTATION_MODEL: Final = "osd"
MAX_LANGUAGES: Final = 3

#: Why a selection could not be served, and how an operator recovers from it.
REASON_NEEDS_LANGUAGE: Final = "needs_ocr_language"
REASON_UNCONFIGURED: Final = "ocr_resources_unconfigured"
REASON_UNVERIFIED: Final = "ocr_model_unverified"
OCR_RECOVERY: Final[Mapping[str, str]] = {
    REASON_NEEDS_LANGUAGE: "install_language",
    REASON_UNCONFIGURED: "install_ocr_resources",
    REASON_UNVERIFIED: "reinstall_ocr_resources",
}

#: The deterministic first segmentation profile, and the one documented alternate
#: pass that is allowed after the first produces no reliable words.
PRIMARY_PSM: Final = 3
ALTERNATE_PSM: Final = 6

#: Word confidence at or above this value may enter OCR search segments.
WORD_CONFIDENCE_THRESHOLD: Final = 35.0
#: A unit is low-confidence when its accepted median is below this, or when more
#: than ``LOW_CONFIDENCE_SHARE`` of its nonempty candidates fall below it.
LOW_CONFIDENCE_THRESHOLD: Final = 60.0
LOW_CONFIDENCE_SHARE: Final = 0.20
#: Tesseract reports word confidence on a 0-100 scale, with -1 on container rows.
CONFIDENCE_SCALE: Final = "tesseract_0_100"

OCR_SECONDS_PER_UNIT: Final = 60
OCR_SECONDS_PER_DOCUMENT: Final = 1800
OCR_PIXELS_PER_DOCUMENT: Final = 8_000_000_000

#: A Tesseract recognition-language code: three letters with an optional script
#: or region suffix. Deliberately narrow, so nothing resembling a path, a glob,
#: an engine flag or a language concatenation can reach the command line.
_LANGUAGE_PATTERN: Final = re.compile(r"[a-z]{3}(?:_[a-z]{2,5})?\Z")
_MODEL_SUFFIX: Final = ".traineddata"
_DIGEST_BLOCK: Final = 1 << 20


class OcrResourceRefused(ValueError):
    """The requested OCR resources cannot be served from trusted configuration."""


class OcrSelectionInvalid(OcrResourceRefused):
    """The configured language selection is malformed, duplicated or over the limit.

    This is a configuration fault rather than a missing model, so it is raised
    instead of being degraded into an unavailable-resource outcome.
    """


@dataclass(frozen=True, slots=True)
class OcrResources:
    """The pinned engine, model and language identity one extraction runs under.

    ``available`` is false when the selection could not be served from verified
    model bytes. The requested order is still carried, ``model_hashes`` is empty
    because nothing was used, and ``reason``/``unavailable_models`` say exactly
    what is missing. Such resources are never handed to the engine.
    """

    engine: str
    engine_revision: str
    languages: tuple[str, ...]
    model_hashes: tuple[str, ...] = ()
    tessdata_dir: str | None = None
    available: bool = True
    reason: str | None = None
    unavailable_models: tuple[str, ...] = ()
    #: A narrower resource fault behind ``reason``, kept when the user-facing
    #: recoverable condition is the broader one. An explicitly requested language
    #: is reported as the language it is missing; that the installation has no
    #: trusted model directory at all is the operator's detail, not the user's.
    resource_detail: str | None = None

    def __post_init__(self) -> None:
        if self.available and (not self.tessdata_dir or not self.model_hashes):
            raise OcrResourceRefused(
                "verified OCR resources need a trusted model directory and model hashes"
            )
        if self.available != (self.reason is None):
            raise OcrResourceRefused("unavailable OCR resources must name their reason")

    @property
    def tesseract_lang(self) -> str:
        """The ordered ``+``-joined language argument for one bounded invocation."""
        return "+".join(self.languages)

    def to_profile(self) -> dict[str, Any]:
        """The ExtractionProfileV1 ``ocr`` member for these resources.

        The requested mode and ordered languages are preserved either way. An
        unavailable selection pins no model hash and says so, rather than
        claiming digests the invocation never used.
        """
        profile: dict[str, Any] = {
            "engine": self.engine,
            "engineRevision": self.engine_revision,
            "modelHashes": list(self.model_hashes),
            "languages": list(self.languages),
        }
        if not self.available:
            profile["resourceState"] = RESOURCE_STATE_UNAVAILABLE
        return profile

    def to_limitations(self) -> dict[str, Any]:
        """The exact recovery detail an unavailable selection reports."""
        assert self.reason is not None
        detail: dict[str, Any] = {
            "code": self.reason,
            "recovery": OCR_RECOVERY[self.reason],
            "requestedLanguages": list(self.languages),
        }
        if self.reason == REASON_NEEDS_LANGUAGE:
            detail["missingLanguages"] = list(self.unavailable_models)
        elif self.unavailable_models:
            detail["unverifiedModels"] = list(self.unavailable_models)
        if self.resource_detail is not None:
            detail["resourceDiagnostic"] = self.resource_detail
        return detail


def validated_languages(requested: Sequence[str] = ()) -> tuple[str, ...]:
    """The ordered recognition selection `requested` names, or the reason it is not one.

    One definition of a well-formed selection, used both where a selection is
    *stored* and where it is *resolved* against installed models. Keeping them
    apart would let the settings surface accept an order the engine path would
    then refuse -- a preference the user could save and never see take effect.

    English is the selection when nothing is requested, which is what makes an
    unconfigured installation ask for a language rather than for nothing.
    """
    languages = tuple(requested) or (DEFAULT_LANGUAGE,)
    if len(languages) > MAX_LANGUAGES:
        raise OcrSelectionInvalid(
            f"at most {MAX_LANGUAGES} recognition languages may be selected"
        )
    if len(set(languages)) != len(languages):
        raise OcrSelectionInvalid("the recognition language order repeats a language")
    for language in languages:
        if (
            not isinstance(language, str)
            or _LANGUAGE_PATTERN.fullmatch(language) is None
        ):
            raise OcrSelectionInvalid(
                "a recognition language is outside the accepted code domain"
            )
        if language == ORIENTATION_MODEL:
            raise OcrSelectionInvalid(
                "orientation/script data is not a recognition language"
            )
    return languages


def _model_digest(directory: str, name: str) -> str | None:
    """The SHA-256 of one *selected* model file, or ``None`` when it is unusable.

    Only the named file is touched, so no host language inventory is discovered
    here. A symbolic link, a directory or anything else that is not a regular
    file is unusable: the bytes a digest was taken over must be the bytes the
    engine opens.
    """
    path = Path(directory) / f"{name}{_MODEL_SUFFIX}"
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_DIGEST_BLOCK), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class TrustedOcrCatalogue:
    """What the signed installation actually installed, and nothing else.

    ``installed`` maps a model name to its hex SHA-256 and ``tessdata_dir`` is
    the one trusted directory those files live in. Both are built from the
    release/model manifest, never from a captured document, a filename hint or
    a directory listing a document could influence. The default carries neither,
    so it serves nothing until an installation configures it.
    """

    engine_revision: str = TESSERACT_REVISION
    installed: Mapping[str, str] = field(default_factory=dict)
    tessdata_dir: str | None = None

    @classmethod
    def from_model_manifest(
        cls,
        manifest: VerifiedModelManifest,
        *,
        tessdata_dir: str,
        engine_revision: str = TESSERACT_REVISION,
    ) -> TrustedOcrCatalogue:
        """Build the catalogue from an already-verified model manifest's artifacts.

        Verification is the caller's job and happens before this call: only the
        artifact names and digests of a manifest that passed
        ``verify_model_manifest`` are read here. ``tessdata_dir`` is required,
        because a manifest without the directory its bytes were installed into
        cannot be checked against anything.
        """
        installed = {
            artifact.name[: -len(_MODEL_SUFFIX)]: artifact.sha256
            for artifact in manifest.artifacts
            if artifact.name.endswith(_MODEL_SUFFIX)
        }
        return cls(
            engine_revision=engine_revision,
            installed=installed,
            tessdata_dir=tessdata_dir,
        )

    def resolve(self, requested: Sequence[str] = ()) -> OcrResources:
        """Pin the ordered language selection, or say truthfully that it is unavailable.

        English is the default when nothing is configured. Order is the
        configured order and is preserved; the engine is invoked once with that
        exact ordered list rather than retried per language. Every selected
        model file is read and digested before the engine sees it, so no
        unverified host model can be presented as a packaged one.
        """
        languages = validated_languages(requested)
        if self.tessdata_dir is None or not self.installed:
            # An explicitly requested language is absent here just as surely as
            # it is from a configured catalogue, and installing it is what
            # recovers either. The narrower fault stays as the detail.
            if requested:
                return self._unavailable(
                    languages,
                    REASON_NEEDS_LANGUAGE,
                    languages,
                    detail=REASON_UNCONFIGURED,
                )
            return self._unavailable(languages, REASON_UNCONFIGURED, ())
        missing = tuple(
            language for language in languages if language not in self.installed
        )
        if missing:
            return self._unavailable(languages, REASON_NEEDS_LANGUAGE, missing)
        names = [*languages]
        if ORIENTATION_MODEL in self.installed:
            names.append(ORIENTATION_MODEL)
        verified: list[str] = []
        for name in names:
            digest = _model_digest(self.tessdata_dir, name)
            if digest is None or digest != self.installed[name]:
                return self._unavailable(languages, REASON_UNVERIFIED, (name,))
            verified.append(digest)
        return OcrResources(
            engine=TESSERACT_ENGINE,
            engine_revision=self.engine_revision,
            languages=languages,
            model_hashes=tuple(sorted({f"sha256:{digest}" for digest in verified})),
            tessdata_dir=self.tessdata_dir,
        )

    def _unavailable(
        self,
        languages: tuple[str, ...],
        reason: str,
        models: tuple[str, ...],
        *,
        detail: str | None = None,
    ) -> OcrResources:
        return OcrResources(
            engine=TESSERACT_ENGINE,
            engine_revision=self.engine_revision,
            languages=languages,
            tessdata_dir=self.tessdata_dir,
            available=False,
            reason=reason,
            unavailable_models=models,
            resource_detail=detail,
        )


#: The production default: nothing installed and no trusted model directory, so
#: it fails closed. The signed model bundle that fills it is assembled in WP-14
#: and reaches this module through :meth:`TrustedOcrCatalogue.from_model_manifest`.
PACKAGED_OCR_CATALOGUE: Final = TrustedOcrCatalogue()


@dataclass(frozen=True, slots=True)
class ExtractionSettings:
    """The trusted per-extraction decisions a worker is handed.

    ``ocr`` is present for :data:`MODE_TEXT_AND_LOCAL_OCR`, including when its
    resources turned out to be unavailable: the selected profile keeps its
    identity and its requested languages either way. A text-only profile is an
    explicit trusted choice and carries no engine, model or language at all.
    """

    processing_mode: str = MODE_TEXT_AND_LOCAL_OCR
    ocr: OcrResources | None = None
    embedded_image_ocr: bool = False

    def __post_init__(self) -> None:
        if self.processing_mode not in {MODE_TEXT_ONLY, MODE_TEXT_AND_LOCAL_OCR}:
            raise OcrSelectionInvalid("unknown processing mode")
        if (self.ocr is None) != (self.processing_mode == MODE_TEXT_ONLY):
            raise OcrSelectionInvalid(
                "only a text-and-local-OCR profile may pin OCR resources"
            )

    @property
    def runs_ocr(self) -> bool:
        """Whether recognition may actually run, which unavailable resources never do."""
        return self.ocr is not None and self.ocr.available


def text_only_settings() -> ExtractionSettings:
    """The explicit faster profile: embedded text and geometry, no recognition."""
    return ExtractionSettings(processing_mode=MODE_TEXT_ONLY)


def local_ocr_settings(
    *,
    catalogue: TrustedOcrCatalogue | None = None,
    languages: Sequence[str] = (),
) -> ExtractionSettings:
    """The default profile for PDF and image sources, with resources pinned.

    The catalogue is read at call time, so an installation (or a test) that
    configures one is honoured rather than captured at import.
    """
    catalogue = catalogue if catalogue is not None else PACKAGED_OCR_CATALOGUE
    return ExtractionSettings(
        processing_mode=MODE_TEXT_AND_LOCAL_OCR, ocr=catalogue.resolve(languages)
    )


class OcrEngine(Protocol):
    """One bounded recognition invocation over an already-prepared working image."""

    def __call__(
        self,
        image: PILImage,
        *,
        resources: OcrResources,
        psm: int,
        timeout_seconds: float,
    ) -> Mapping[str, Sequence[Any]]: ...


def run_tesseract(
    image: PILImage,
    *,
    resources: OcrResources,
    psm: int,
    timeout_seconds: float = OCR_SECONDS_PER_UNIT,
) -> Mapping[str, Sequence[Any]]:
    """Run the packaged Tesseract once and return its raw TSV word rows.

    The model directory is the verified trusted one and is always passed, so the
    engine never searches the host for a model. Resources whose bytes were not
    verified are refused here rather than invoked. No argument is derived from
    source bytes.
    """
    if not resources.available or not resources.tessdata_dir:
        raise OcrResourceRefused(
            "OCR resources were not verified before the engine invocation"
        )
    config = (
        f"--oem {TESSERACT_OEM} --psm {int(psm)} "
        f"--tessdata-dir {shlex.quote(resources.tessdata_dir)}"
    )
    data: Mapping[str, Sequence[Any]] = pytesseract.image_to_data(
        image,
        lang=resources.tesseract_lang,
        config=config,
        output_type=Output.DICT,
        timeout=timeout_seconds,
    )
    return data


__all__ = [
    "ALTERNATE_PSM",
    "CONFIDENCE_SCALE",
    "DEFAULT_LANGUAGE",
    "LOW_CONFIDENCE_SHARE",
    "LOW_CONFIDENCE_THRESHOLD",
    "MAX_LANGUAGES",
    "MODE_TEXT_AND_LOCAL_OCR",
    "MODE_TEXT_ONLY",
    "OCR_PIXELS_PER_DOCUMENT",
    "OCR_RECOVERY",
    "OCR_SECONDS_PER_DOCUMENT",
    "OCR_SECONDS_PER_UNIT",
    "ORIENTATION_MODEL",
    "PACKAGED_OCR_CATALOGUE",
    "PRIMARY_PSM",
    "REASON_NEEDS_LANGUAGE",
    "REASON_UNCONFIGURED",
    "REASON_UNVERIFIED",
    "RESOURCE_STATE_UNAVAILABLE",
    "TESSERACT_ENGINE",
    "TESSERACT_REVISION",
    "WORD_CONFIDENCE_THRESHOLD",
    "ExtractionSettings",
    "OcrEngine",
    "OcrResourceRefused",
    "OcrResources",
    "OcrSelectionInvalid",
    "TrustedOcrCatalogue",
    "local_ocr_settings",
    "run_tesseract",
    "text_only_settings",
    "validated_languages",
]
