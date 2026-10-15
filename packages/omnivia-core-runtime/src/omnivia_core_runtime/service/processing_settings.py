"""The workspace's durable document-processing preferences, and what they bind.

Three preferences decide how a *future* capture is read: the processing mode,
the ordered recognition languages, and whether raster images embedded in an
Office, RTF or local HTML source are examined for text. They are stored as one
JSON value under one key in `omnivia_app_settings` (migration 0070), so they are
workspace-scoped, owner-authoritative, compare-and-swap updated and survive a
service restart, with no DDL of their own.

Future captures only
--------------------

A preference is read **once**, at the moment a capture is admitted, and the
result is written into that capture's own row (`processing_settings_json`,
migration 0071). Everything downstream -- the queued obligation, the immutable
profile the worker executes, the profile identity `document.history` reports --
is derived from that frozen snapshot and never from the live setting. Changing a
preference therefore cannot reach work already admitted, which is the whole
point: the alternative is a late global read that silently rewrites what a queued
capture will do, and an operation key that names one profile while another runs
beneath it.

An already captured retained source changes only through the explicit
`reprocess_captured_copy` action, which resolves its own options from the
*current* settings because it is a new, explicit user decision.

What this module does not decide
--------------------------------

Whether a selected language is actually installed. That is
:class:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue`'s
answer, resolved at execution against the managed cache, and an unavailable
selection keeps its identity and its requested order rather than quietly
becoming English. A stored preference is a statement of intent; it is not a
claim that the bytes to honour it are present.

:class:`ManagedSelectionAuthority` is where a frozen selection meets one
installation's managed cache. It resolves only inside a lease, because resolving
a selection and invoking the engine over it are two moments and the resources
have to survive both.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    OCR_RESOURCES_DIR,
    ManagedOcrResources,
    OcrResourceLifecycleRefused,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    OcrResourceSetIdentity,
    OcrResourceStoreError,
)
from omnivia_core_runtime.service.ocr_resources import (
    DEFAULT_LANGUAGE,
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    ORIENTATION_MODEL,
    TESSERACT_REVISION,
    OcrSelectionInvalid,
    TrustedOcrCatalogue,
    validated_languages,
)
from omnivia_core_runtime.storage import app_settings

#: The one settings key these preferences live under. Dotted so the namespace is
#: this feature's and a typo cannot land in another feature's keys.
SETTING_KEY: Final = "document-processing.profile"

#: What `updated_by` records. The component, not the user: a support question
#: about a surprising setting needs to know which code wrote it.
SETTING_WRITER: Final = "omnivia-core.document-processing-settings"

#: The closed pair of modes a selection may name, in the order the pane offers
#: them: the default first, the explicit faster alternate second.
PROCESSING_MODES: Final = (MODE_TEXT_AND_LOCAL_OCR, MODE_TEXT_ONLY)

#: The media types embedded-image recognition can apply to at all: a source that
#: carries images *inside* it. A PDF or an image capture is covered by `mode`,
#: and plain text and delimited text carry no images, so selecting the
#: preference for them would record a decision nothing could act on.
EMBEDDED_IMAGE_MEDIA_TYPES: Final = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/rtf",
        "text/rtf",
        "text/html",
        "application/xhtml+xml",
    }
)

#: The stored document's members. Closed: an unknown key means the value was
#: written by a build that knows something this one does not, and reading it as
#: if it were understood would silently drop that preference.
_VALUE_KEYS: Final = frozenset({"embeddedImageOcr", "languages", "mode"})


class ProcessingSettingsInvalid(ValueError):
    """A processing-settings document is malformed, or names an unknown value."""


@dataclass(frozen=True, slots=True)
class ProcessingSettings:
    """One complete, valid set of document-processing preferences.

    The defaults are the product defaults, so an unconfigured workspace and an
    explicitly-defaulted one are the same settings. ``languages`` is explicitly
    ``("eng",)`` rather than empty: an empty request is how
    :meth:`TrustedOcrCatalogue.resolve` is told "nothing is configured", and the
    product default is a *configured* selection of English, which is reported as
    a missing language when English is not installed rather than as an
    unconfigured installation.
    """

    mode: str = MODE_TEXT_AND_LOCAL_OCR
    languages: tuple[str, ...] = (DEFAULT_LANGUAGE,)
    embedded_image_ocr: bool = False

    def __post_init__(self) -> None:
        if self.mode not in PROCESSING_MODES:
            raise ProcessingSettingsInvalid(
                "processing mode must be 'text_and_local_ocr' or 'text_only'"
            )
        if not isinstance(self.embedded_image_ocr, bool):
            raise ProcessingSettingsInvalid("embedded-image OCR must be true or false")
        if not self.languages:
            raise ProcessingSettingsInvalid(
                "a recognition selection names at least one language"
            )
        try:
            validated_languages(self.languages)
        except OcrSelectionInvalid as error:
            raise ProcessingSettingsInvalid(str(error)) from error

    def embedded_image_ocr_for(self, media_type: str) -> bool:
        """Whether the preference applies to this media type at all.

        Scoped here, once, so the immutable profile records what the selection
        *does* for the source it was bound to rather than the selection in the
        abstract. A profile claiming embedded-image recognition for a PDF would
        be claiming a pass that could not run.
        """
        return self.embedded_image_ocr and media_type in EMBEDDED_IMAGE_MEDIA_TYPES

    def to_value(self) -> dict[str, Any]:
        """The stored JSON document."""
        return {
            "embeddedImageOcr": self.embedded_image_ocr,
            "languages": list(self.languages),
            "mode": self.mode,
        }

    def to_wire(self, revision: int) -> dict[str, Any]:
        """The `DocumentProcessingSettings` contract member for these settings."""
        return {
            "mode": self.mode,
            "recognition_languages": list(self.languages),
            "embedded_image_ocr": self.embedded_image_ocr,
            "revision": revision,
        }

    @classmethod
    def from_value(cls, value: object) -> ProcessingSettings:
        """The settings one stored document names, or the reason it is not one."""
        if not isinstance(value, Mapping):
            raise ProcessingSettingsInvalid("stored settings must be a JSON object")
        unknown = sorted(set(value) - _VALUE_KEYS)
        if unknown:
            raise ProcessingSettingsInvalid(
                f"stored settings carry unknown keys: {', '.join(unknown)}"
            )
        missing = sorted(_VALUE_KEYS - set(value))
        if missing:
            raise ProcessingSettingsInvalid(
                f"stored settings are missing keys: {', '.join(missing)}"
            )
        languages = value["languages"]
        if isinstance(languages, str) or not isinstance(languages, (list, tuple)):
            raise ProcessingSettingsInvalid("stored languages must be a JSON array")
        mode = value["mode"]
        if not isinstance(mode, str):
            raise ProcessingSettingsInvalid("stored mode must be a string")
        embedded = value["embeddedImageOcr"]
        if not isinstance(embedded, bool):
            raise ProcessingSettingsInvalid("stored embeddedImageOcr must be a boolean")
        return cls(
            mode=mode,
            languages=tuple(str(language) for language in languages),
            embedded_image_ocr=embedded,
        )

    def replacing(
        self,
        *,
        mode: str | None,
        languages: Sequence[str] | None,
        embedded_image_ocr: bool | None,
    ) -> ProcessingSettings:
        """These settings with the named members changed and the rest held.

        An absent member is deliberately *held* rather than defaulted: a partial
        update from a caller that does not know about a preference must not reset
        it, or a build that learns a new preference would have it silently
        cleared by every older caller.
        """
        return ProcessingSettings(
            mode=self.mode if mode is None else mode,
            languages=(
                self.languages if languages is None else tuple(str(x) for x in languages)
            ),
            embedded_image_ocr=(
                self.embedded_image_ocr
                if embedded_image_ocr is None
                else embedded_image_ocr
            ),
        )


#: The product defaults, as one value. Absence of a stored row means exactly
#: this, so a workspace that has never opened the pane and one that saved the
#: defaults behave identically.
DEFAULT_SETTINGS: Final = ProcessingSettings()


def read_settings(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[ProcessingSettings, int]:
    """The stored preferences and their compare-and-swap revision.

    Revision zero means nothing is stored and the returned settings are the
    product defaults. Needs no transaction and works on a `READ_ONLY`
    connection, because the settings pane must be able to show stored values it
    cannot currently change.
    """
    stored = app_settings.read_setting(
        connection, workspace_id=workspace_id, key=SETTING_KEY
    )
    if stored is None:
        return DEFAULT_SETTINGS, 0
    return ProcessingSettings.from_value(stored.value), stored.version


def apply_settings(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    settings: ProcessingSettings,
    expected_revision: int,
    now_us: int,
) -> int:
    """Store `settings` inside the caller's fenced transaction; the new revision.

    `expected_revision` is the compare-and-swap: it must equal the revision the
    caller read, so a change made since that read is refused rather than
    silently overwritten. Zero asserts nothing is stored yet.
    """
    stored = app_settings.apply_setting(
        connection,
        workspace_id=workspace_id,
        key=SETTING_KEY,
        value=settings.to_value(),
        updated_at_us=now_us,
        updated_by=SETTING_WRITER,
        expected_version=expected_revision,
    )
    return stored.version


def write_settings(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    settings: ProcessingSettings,
    expected_revision: int,
    now_us: int,
) -> int:
    """Store `settings` in a transaction of this function's own; the new revision."""
    stored = app_settings.write_setting(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        key=SETTING_KEY,
        value=settings.to_value(),
        updated_at_us=now_us,
        updated_by=SETTING_WRITER,
        expected_version=expected_revision,
    )
    return stored.version


def installed_identities(
    resources: ManagedOcrResources,
) -> dict[str, OcrResourceSetIdentity]:
    """Each recognition language the managed cache can actually serve, by its identity.

    One definition of "installed", used both where a selection is *admitted* and
    where it is *resolved* for the engine. A language whose payload is absent or
    short, and a resource set recorded for an engine revision this build does not
    invoke, are both left out: presenting either would offer a language whose
    bytes cannot be handed to the engine that would have to read them.

    An unreadable or contended cache yields nothing, which is the truthful
    answer: Core could not establish what is installed, so it claims nothing is.
    """
    try:
        inventory = resources.inventory()
    except (OcrResourceLifecycleRefused, OcrResourceStoreError, OSError):
        return {}
    return {
        resource.identity.language: resource.identity
        for resource in inventory.resources
        if resource.bytes_present
        and resource.identity.engine_version == TESSERACT_REVISION
    }


def selected_identities(
    resources: ManagedOcrResources, requested: Sequence[str]
) -> list[OcrResourceSetIdentity]:
    """The installed identities one request names, with the orientation data after.

    The ordered request, narrowed to what the cache can actually serve, in the
    order it was asked for. Empty when the cache can serve none of it, which is
    the fail-closed answer: a selection nothing is installed for resolves as
    unavailable with its requested order intact rather than as a smaller one.
    """
    present = installed_identities(resources)
    selection = [
        present[language]
        for language in validated_languages(requested)
        if language in present
    ]
    if not selection:
        return []
    orientation = present.get(ORIENTATION_MODEL)
    if orientation is not None:
        selection.append(orientation)
    return selection


def _catalogue_over(
    selection: Sequence[OcrResourceSetIdentity], directory: Path
) -> TrustedOcrCatalogue:
    """The trusted catalogue one published projection of `selection` serves."""
    return TrustedOcrCatalogue(
        installed={identity.language: identity.sha256 for identity in selection},
        tessdata_dir=str(directory),
    )


class _HeldSelections:
    """Every selection one resolution leased, released together at its end.

    One resolution may ask for the same selection more than once -- a history
    page offers approved profiles per media type, and every media type of one
    workspace names the same recognition order -- so an answer already resolved
    is reused rather than re-projected. That is also what makes the digest a
    caller is offered and the digest that executes one resolution rather than
    two.
    """

    def __init__(self, resources: ManagedOcrResources) -> None:
        self._resources = resources
        self._stack = ExitStack()
        self._resolved: dict[tuple[str, ...], TrustedOcrCatalogue] = {}

    def catalogue(self, requested: Sequence[str] = ()) -> TrustedOcrCatalogue:
        """Hold what this installation can serve of `requested`, and name it.

        The bridge between the two halves the installation already has: the managed
        cache owns the bytes, their five-field identities, the 512 MiB capacity rule
        and the one directory an ordered selection is published into, and
        :class:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue`
        owns what the engine may be invoked with. Nothing is downloaded or installed
        here, and no second resource store is introduced: the cache's own inventory
        is the only input.

        The whole installed part of the ordered selection is projected and held, so
        two or three installed languages are served together and in the order they
        were asked for, with the orientation data alongside them when the cache holds
        it. Digest verification is preserved: the bytes the projection links are
        bytes the cache has just re-digested against their declared identity, and
        :meth:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue.resolve`
        digests them once more before the engine sees them. A requested language the
        cache has not got is absent from `installed`, so `resolve` reports exactly it
        as missing rather than the whole selection.

        Fails closed. An unreadable or contended cache, a projection that cannot be
        published, and a hold that cannot be taken all yield the empty catalogue --
        serving a projection this call could not keep would be the defect the lease
        exists to remove. The selection then resolves as unavailable with its
        requested order intact and nothing substituted.
        """
        key = tuple(requested)
        held = self._resolved.get(key)
        if held is not None:
            return held
        try:
            selection = selected_identities(self._resources, requested)
            if selection:
                directory = self._stack.enter_context(
                    self._resources.leased_selection(selection)
                )
                answer = _catalogue_over(selection, directory)
            else:
                answer = TrustedOcrCatalogue()
        except (OcrResourceLifecycleRefused, OcrResourceStoreError, OSError):
            answer = TrustedOcrCatalogue()
        self._resolved[key] = answer
        return answer

    def release(self) -> None:
        """Drop every hold this resolution took.

        Never raises: it runs after the work it was holding for has reached its
        terminal handling, and a failure to tidy up must not become that work's
        outcome. A hold whose release failed is a lock no longer held, which the
        next managed operation discards as abandoned.
        """
        with suppress(OcrResourceLifecycleRefused, OcrResourceStoreError, OSError):
            self._stack.close()


@dataclass(frozen=True, slots=True)
class ManagedSelectionAuthority:
    """One installation's managed OCR cache, as a source of *held* selections.

    The managed cache is installation-local state, and the lifecycle takes a root
    rather than choosing one, so the join between the two is made once, here, and
    handed to the executor and to the request handlers that resolve a profile.

    It is deliberately not itself a resolver. A resolution that is not held is
    the defect this class exists to prevent, so the only way to resolve through
    it is to take a lease first and resolve inside it.
    """

    resources: ManagedOcrResources

    @contextmanager
    def lease(self) -> Iterator[Callable[[Sequence[str]], TrustedOcrCatalogue]]:
        """Resolve frozen selections inside the block, holding each to its end.

        The yielded callable takes the *frozen* ordered selection and is called at
        the moment work runs, never at admission: it is what makes "resolved
        against trusted managed resources at execution" literal. Whatever it
        resolves stays held -- the exact resource identities and the projected
        directory -- until the block ends, which the caller places after its
        terminal handling of the work the resources were resolved for.

        Entering the block touches nothing and takes no lock, so a Text-only
        capture -- which returns before any catalogue is consulted -- leases
        nothing at all.
        """
        held = _HeldSelections(self.resources)
        try:
            yield held.catalogue
        finally:
            held.release()


def installation_catalogue(
    installation_root: Path | None,
) -> ManagedSelectionAuthority | None:
    """How one installation resolves a selection, or ``None`` when it cannot.

    ``None`` means this process has no installation root to ask, or the root is
    not one a managed cache may live under. The caller then falls back to the
    packaged catalogue, which serves nothing -- fail-closed, not host-discovered.
    """
    if installation_root is None:
        return None
    try:
        resources = ManagedOcrResources(Path(installation_root) / OCR_RESOURCES_DIR)
    except OcrResourceStoreError:
        return None
    return ManagedSelectionAuthority(resources)


__all__ = [
    "DEFAULT_SETTINGS",
    "EMBEDDED_IMAGE_MEDIA_TYPES",
    "PROCESSING_MODES",
    "SETTING_KEY",
    "SETTING_WRITER",
    "ManagedSelectionAuthority",
    "ProcessingSettings",
    "ProcessingSettingsInvalid",
    "apply_settings",
    "installation_catalogue",
    "installed_identities",
    "read_settings",
    "selected_identities",
    "write_settings",
]
