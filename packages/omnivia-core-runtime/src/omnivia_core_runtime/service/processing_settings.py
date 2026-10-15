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

Which resources, and when
-------------------------

A stored preference names *languages*. Which bytes serve them is a second
question, and §18.4 requires both halves to be frozen at the same instant: the
ordered resource-set identities an OCR capture will execute against are resolved
at admission, against the installation's managed cache, and recorded on the
capture row beside the preferences (`processing_resources_json`, migration 0072).
:func:`admitted_resources` is that resolution, and it *pins* what it resolves in
the same coordinated moment, so the bytes cannot be removed or evicted while the
obligation waits.

:meth:`ManagedSelectionAuthority.frozen_resources` is the other end: it serves
one capture's frozen identities and nothing else. A language whose identity is no
longer installed, or whose bytes no longer match it, is reported as the resource
condition it is -- never resolved to whatever model of that language is installed
by the time the obligation's turn comes, which is the substitution the whole
arrangement exists to make impossible.

:class:`ManagedSelectionAuthority` also keeps its *language* resolution, used by
`document.history` and `document.reprocess`, because a reprocess is an explicit
new decision that resolves against the workspace's current preferences and runs
inside one request. Both resolutions hold what they resolve, because resolving a
selection and invoking the engine over it are two moments and the resources have
to survive both.

What this module still does not decide
--------------------------------------

Whether a resolved selection *succeeded*. An unavailable selection keeps its
identity and its requested order rather than quietly becoming English, and
:class:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue` is the
one place that digests the bytes before the engine sees them. A stored preference
is a statement of intent; a frozen identity is a claim about which bytes were
present when it was taken; neither is a claim that the engine ran.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    MAX_PROJECTED_MODELS,
    OCR_RESOURCES_DIR,
    REFUSAL_DIVERGENT,
    REFUSAL_NOT_INSTALLED,
    ManagedOcrInventory,
    ManagedOcrResources,
    OcrResourceLifecycleRefused,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    OcrResourceSetIdentity,
    OcrResourceStoreError,
    _identity_document,
    _identity_from_document,
)
from omnivia_core_runtime.service.ocr_resources import (
    DEFAULT_LANGUAGE,
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    ORIENTATION_MODEL,
    REASON_NEEDS_LANGUAGE,
    REASON_UNCONFIGURED,
    REASON_UNVERIFIED,
    TESSERACT_ENGINE,
    TESSERACT_REVISION,
    OcrResources,
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

#: The media types the engine reads whole, so `mode` alone decides recognition
#: for them. Here rather than beside the worker because admission and execution
#: have to answer "does this capture resolve OCR resources" identically, and two
#: copies of this set would be two chances for one of them to pin nothing and the
#: other to want resources.
OCR_MEDIA_TYPES: Final = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/heic",
        "image/heif",
        "image/webp",
    }
)

#: The profile reference that selects the explicit faster Text-only profile. Here
#: for the same reason :data:`OCR_MEDIA_TYPES` is.
TEXT_ONLY_PROFILE_REF: Final = "text-only"

#: The stored document's members. Closed: an unknown key means the value was
#: written by a build that knows something this one does not, and reading it as
#: if it were understood would silently drop that preference.
_VALUE_KEYS: Final = frozenset({"embeddedImageOcr", "languages", "mode"})

#: The schema token every capture resource snapshot carries. A document without
#: it, or with another, is refused rather than guessed at.
CAPTURE_RESOURCES_SCHEMA: Final = "omnivia.capture-ocr-resources.v1"

#: This capture resolves no OCR resources at all -- Text-only, or a media type
#: the preferences do not recognise. Recorded rather than left absent, because
#: "pinned nothing deliberately" and "recorded nothing at all" are different
#: facts and only the first one is a decision this build made.
RESOURCES_NONE: Final = "none"

#: Recognition was wanted and the installation could serve it: `selection` is the
#: exact ordered identities, pinned until the obligation settles.
RESOURCES_FROZEN: Final = "frozen"

#: Recognition was wanted and the installation could not serve it at admission.
#: Deliberately terminal for this capture: resolving it again later would be
#: resolving against whatever is installed by then, which is the substitution
#: §18.4 forbids. The recovery is an explicit reprocess, which is a new decision.
RESOURCES_UNAVAILABLE: Final = "unavailable"

_RESOURCE_KINDS: Final = (RESOURCES_NONE, RESOURCES_FROZEN, RESOURCES_UNAVAILABLE)

#: The snapshot document's members, all of them always present. A closed, total
#: key set makes the strict read trivial and leaves no member whose absence has
#: to be interpreted.
_RESOURCE_KEYS: Final = frozenset(
    {"detail", "kind", "reason", "schema", "selection", "unavailableModels"}
)

#: The reasons a snapshot may name, which are the catalogue's own: a snapshot
#: cannot invent a resource condition the rest of the system has no recovery for.
_RESOURCE_REASONS: Final = (
    REASON_NEEDS_LANGUAGE,
    REASON_UNCONFIGURED,
    REASON_UNVERIFIED,
)

#: What a capture's pins are named. The workspace digest scopes them: one
#: installation's managed cache may serve more than one workspace, and a sweep
#: of one workspace's abandoned pins must not be able to reach another's.
_PIN_NAMESPACE: Final = "capture"


class ProcessingSettingsInvalid(ValueError):
    """A processing-settings document is malformed, or names an unknown value."""


class ProcessingResourcesInvalid(ValueError):
    """A capture's frozen resource snapshot is malformed, or names unknown values.

    Distinct from :class:`ProcessingSettingsInvalid` because the two columns fail
    for different reasons and an operator reading one of them needs to know which
    document to look at: the preferences the capture was admitted under, or the
    resource identities it was admitted against.
    """


class ResourceStateIndeterminate(Exception):
    """The managed cache could not be read, so nothing about it may be decided.

    Not a resource condition. A condition is an answer -- this language is not
    installed, these bytes do not verify -- and it is recorded against the work it
    describes. This is the absence of an answer: the coordination lock was held
    elsewhere, the state document would not read, the filesystem refused. Spending
    it as "nothing is installed" would freeze a capture permanently out of
    recognition because an administrator happened to be installing a language, and
    spending it as "the resources are fine" would run the engine over bytes nobody
    established. So both callers refuse instead: admission declines the capture as
    retryable, and execution leaves the obligation pending for a later pass.
    """


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

    def recognises(self, media_type: str) -> bool:
        """Whether these preferences would run recognition over this media type.

        Two independent ways recognition can be wanted: the media type is one the
        engine reads whole (a PDF or an image), or the source carries raster
        images *inside* it and the embedded-image preference applies to it.
        Text-only wants neither.

        Decided in one place because four callers need the same answer -- what to
        freeze and pin at admission, what to resolve at execution, what limits to
        publish, and whether a reprocess has a real alternate to offer -- and four
        copies of it would be four chances for the capture that pinned no
        resources to be the capture that then wants them.
        """
        return self.mode == MODE_TEXT_AND_LOCAL_OCR and (
            media_type in OCR_MEDIA_TYPES or self.embedded_image_ocr_for(media_type)
        )

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


def resolves_resources(
    *, media_type: str, profile_ref: str, settings: ProcessingSettings | None
) -> bool:
    """Whether one capture's processing resolves OCR resources at all.

    The single predicate admission and execution both ask. Admission asks it to
    decide whether to freeze and pin identities; execution asks it to decide
    whether to look for them. The two are given the same three inputs from the
    same immutable capture row, so they cannot disagree -- and if they could, the
    disagreement would be exactly the dangerous shape: a capture that pinned
    nothing and then asked for resources, or one that pinned bytes it would never
    read and held them against the operator's own removal for the length of the
    queue.

    ``settings=None`` is the pre-0071 capture, which recorded no preferences. It
    is answered from the media type alone, which is the rule that applied when
    such a capture was admitted, and never by consulting a preference it does not
    have.
    """
    if profile_ref == TEXT_ONLY_PROFILE_REF:
        return False
    if settings is None:
        return media_type in OCR_MEDIA_TYPES
    return settings.recognises(media_type)


def capture_pin(workspace_id: str, capture_id: str) -> str:
    """The durable pin identifier one capture's processing obligation holds.

    Derived rather than stored, from the two values every caller that can reach a
    capture row already has. A stored identifier would be a second copy of a
    fact, and a capture whose row said one pin while the state document held
    another would have no way to tell which was the claim.

    Both halves are digested and fixed-width: the whole thing has to be a token a
    pin may carry -- bounded, no separator a stable key would reinterpret -- and
    neither a workspace identifier nor a capture identifier is bounded in a way
    that would guarantee that. The workspace half is a *prefix*, so
    :func:`capture_pin_prefix` can scope a sweep to one workspace's own pins
    without needing to reverse them.
    """
    workspace = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    capture = hashlib.sha256(capture_id.encode("utf-8")).hexdigest()[:32]
    return f"{_PIN_NAMESPACE}-{workspace}-{capture}"


def capture_pin_prefix(workspace_id: str) -> str:
    """What every pin of this workspace's captures begins with, and nothing else.

    The scope of a reclamation sweep. A pin written by another workspace's service
    instance, or by some later component with pins of its own, does not start with
    this and is never considered -- which is what makes a sweep computed from one
    workspace's obligations safe against an installation that serves several.
    """
    return f"{_PIN_NAMESPACE}-" + hashlib.sha256(
        workspace_id.encode("utf-8")
    ).hexdigest()[:16] + "-"


@dataclass(frozen=True, slots=True)
class AdmittedResources:
    """The OCR resources one capture was admitted against, as a frozen fact.

    One of three states, and which one is itself part of the record:

    :data:`RESOURCES_NONE` -- this capture resolves no OCR resources, so it pins
    none. Text-only, or a media type these preferences do not recognise.

    :data:`RESOURCES_FROZEN` -- ``selection`` is the exact ordered resource-set
    identities, every one of them installed and pinned at the instant of
    admission. Execution serves these and refuses anything else.

    :data:`RESOURCES_UNAVAILABLE` -- recognition was wanted and the installation
    could not serve the selection. ``reason`` is the catalogue's own condition,
    ``unavailable_models`` what it names, and ``detail`` the narrower operator
    fault where the user-facing condition is the broader one -- the same three
    values :meth:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue.resolve`
    would have produced, recorded at the moment they were true rather than
    recomputed later against a different installation.

    The coherence rules are enforced here rather than trusted, because this is
    read back from a database column: a frozen state with no selection, or an
    unavailable one carrying identities, would each let execution act on a
    snapshot that says two things at once.
    """

    kind: str
    selection: tuple[OcrResourceSetIdentity, ...] = ()
    reason: str | None = None
    unavailable_models: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _RESOURCE_KINDS:
            raise ProcessingResourcesInvalid(
                f"a capture resource snapshot names one of "
                f"{', '.join(_RESOURCE_KINDS)}"
            )
        if not all(
            isinstance(identity, OcrResourceSetIdentity) for identity in self.selection
        ):
            raise ProcessingResourcesInvalid(
                "a frozen selection names OcrResourceSetIdentity values"
            )
        if self.kind == RESOURCES_FROZEN:
            if not self.selection:
                raise ProcessingResourcesInvalid(
                    "a frozen resource selection names at least one identity"
                )
            if len(self.selection) > MAX_PROJECTED_MODELS:
                raise ProcessingResourcesInvalid(
                    f"a frozen resource selection names at most "
                    f"{MAX_PROJECTED_MODELS} identities"
                )
            languages = [identity.language for identity in self.selection]
            if len(set(languages)) != len(languages):
                raise ProcessingResourcesInvalid(
                    "a frozen resource selection names one identity per language"
                )
        elif self.selection:
            raise ProcessingResourcesInvalid(
                f"only a {RESOURCES_FROZEN} resource snapshot names identities"
            )
        if (self.reason is None) != (self.kind != RESOURCES_UNAVAILABLE):
            raise ProcessingResourcesInvalid(
                f"exactly a {RESOURCES_UNAVAILABLE} resource snapshot names a reason"
            )
        for name, value in (("reason", self.reason), ("detail", self.detail)):
            if value is not None and value not in _RESOURCE_REASONS:
                raise ProcessingResourcesInvalid(
                    f"a capture resource snapshot's {name} is not a known "
                    f"resource condition"
                )
        if self.unavailable_models and self.kind != RESOURCES_UNAVAILABLE:
            raise ProcessingResourcesInvalid(
                f"only a {RESOURCES_UNAVAILABLE} resource snapshot names "
                f"unavailable models"
            )

    @property
    def languages(self) -> tuple[str, ...]:
        """The recognition languages this frozen selection serves, in order.

        The orientation data is not one: it is loaded alongside the recognition
        models and cannot be selected, which
        :func:`~omnivia_core_runtime.service.ocr_resources.validated_languages`
        refuses outright.
        """
        return tuple(
            identity.language
            for identity in self.selection
            if identity.language != ORIENTATION_MODEL
        )

    def to_value(self) -> dict[str, Any]:
        """The stored JSON document, with every member present."""
        return {
            "detail": self.detail,
            "kind": self.kind,
            "reason": self.reason,
            "schema": CAPTURE_RESOURCES_SCHEMA,
            "selection": [_identity_document(identity) for identity in self.selection],
            "unavailableModels": list(self.unavailable_models),
        }

    @classmethod
    def from_value(cls, value: object) -> AdmittedResources:
        """The snapshot one stored document names, or the reason it is not one.

        Strict in every direction: the schema token, the exact key set, the type
        of each member, and then the identities themselves through the store's own
        constructor -- so a stored identity cannot be admitted by a looser rule
        than a resolved one. Nothing here is defaulted or repaired. A document
        this cannot read is a capture whose resource decision cannot be
        established, which is a terminal fact about that capture and not a licence
        to resolve a new one.
        """
        if not isinstance(value, Mapping):
            raise ProcessingResourcesInvalid(
                "a stored resource snapshot must be a JSON object"
            )
        if value.get("schema") != CAPTURE_RESOURCES_SCHEMA:
            raise ProcessingResourcesInvalid(
                f"a stored resource snapshot carries the schema "
                f"{CAPTURE_RESOURCES_SCHEMA}"
            )
        unknown = sorted(set(value) - _RESOURCE_KEYS)
        if unknown:
            raise ProcessingResourcesInvalid(
                f"a stored resource snapshot carries unknown keys: "
                f"{', '.join(unknown)}"
            )
        missing = sorted(_RESOURCE_KEYS - set(value))
        if missing:
            raise ProcessingResourcesInvalid(
                f"a stored resource snapshot is missing keys: {', '.join(missing)}"
            )
        kind = value["kind"]
        if not isinstance(kind, str):
            raise ProcessingResourcesInvalid(
                "a stored resource snapshot's kind must be a string"
            )
        selection = value["selection"]
        if not isinstance(selection, list):
            raise ProcessingResourcesInvalid(
                "a stored resource selection must be a JSON array"
            )
        models = value["unavailableModels"]
        if isinstance(models, str) or not isinstance(models, (list, tuple)):
            raise ProcessingResourcesInvalid(
                "stored unavailable models must be a JSON array"
            )
        for name in ("reason", "detail"):
            if value[name] is not None and not isinstance(value[name], str):
                raise ProcessingResourcesInvalid(
                    f"a stored resource snapshot's {name} must be a string or null"
                )
        try:
            identities = tuple(
                _identity_from_document(f"frozen identity {index}", identity)
                for index, identity in enumerate(selection)
            )
        except OcrResourceStoreError as error:
            raise ProcessingResourcesInvalid(
                f"a stored resource selection does not decode: {error}"
            ) from error
        return cls(
            kind=kind,
            selection=identities,
            reason=value["reason"],
            unavailable_models=tuple(str(model) for model in models),
            detail=value["detail"],
        )

    def unserved(self, requested: Sequence[str]) -> OcrResources:
        """What an unavailable snapshot reports, exactly as it was recorded.

        Built here rather than by re-resolving, which is the whole property: the
        condition an operator is shown, and the one the extraction's own outcome
        carries, is the condition that was true at admission -- not a second
        reading of an installation that has moved since.

        ``requested`` is the capture's own frozen recognition order, which is
        carried whole. The condition names which of it could not be served; the
        selection it asked for is not narrowed to that, and no model hash is
        pinned, because none was read.
        """
        assert self.kind == RESOURCES_UNAVAILABLE and self.reason is not None
        return OcrResources(
            engine=TESSERACT_ENGINE,
            engine_revision=TESSERACT_REVISION,
            languages=validated_languages(requested),
            available=False,
            reason=self.reason,
            unavailable_models=self.unavailable_models,
            resource_detail=self.detail,
        )


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


def _present(inventory: ManagedOcrInventory) -> dict[str, OcrResourceSetIdentity]:
    """Each recognition language one inventory can actually serve, by its identity.

    The one definition of "installed", used wherever a selection is admitted,
    resolved or offered. A language whose payload is absent or short, and a
    resource set recorded for an engine revision this build does not invoke, are
    both left out: presenting either would offer a language whose bytes cannot be
    handed to the engine that would have to read them.
    """
    return {
        resource.identity.language: resource.identity
        for resource in inventory.resources
        if resource.bytes_present
        and resource.identity.engine_version == TESSERACT_REVISION
    }


def installed_identities(
    resources: ManagedOcrResources,
) -> dict[str, OcrResourceSetIdentity]:
    """What the managed cache can serve, or nothing when that cannot be read.

    An unreadable or contended cache yields nothing, which is the truthful answer
    for the callers that have no way to refuse: Core could not establish what is
    installed, so it claims nothing is. The callers that *can* refuse -- admission
    and execution, which would otherwise freeze or run a capture on the strength
    of not having been able to look -- read the inventory directly and raise
    :class:`ResourceStateIndeterminate` instead.
    """
    try:
        return _present(resources.inventory())
    except (OcrResourceLifecycleRefused, OcrResourceStoreError, OSError):
        return {}


def admitted_resources(
    resources: ManagedOcrResources | None,
    *,
    pin: str,
    languages: Sequence[str],
) -> AdmittedResources:
    """Freeze and pin the exact resources one capture is admitted against.

    §18.4's admission moment. The ordered recognition selection is resolved
    against this installation's managed cache and, if the cache can serve it, the
    exact identities are *pinned* under ``pin`` before this returns -- both inside
    one coordination block, which is what makes the pair atomic. A removal
    arriving between the read and the pin would otherwise take away the bytes the
    capture had just been promised, and the capture row would name identities
    nothing held.

    The order of the two is the one that has no gap the other way round either.
    The pin is written *before* the capture row is durable, so there is never an
    instant in which an admitted capture's resources are unpinned and removable;
    the cost is that a capture whose transaction then rolls back leaves a pin no
    operation holds, which
    :meth:`~omnivia_core_runtime.service.ocr_resource_lifecycle.ManagedOcrResources.reclaim`
    takes back. A leaked pin is recoverable; a removal in the gap is not.

    Three answers, never a fourth. The selection is served and frozen; or the
    installation demonstrably cannot serve it, which is recorded as the condition
    it is and is final for this capture; or the cache could not be read at all,
    which is :class:`ResourceStateIndeterminate` and no answer -- the caller
    declines the admission rather than freezing a capture out of recognition
    because a lock was briefly held.

    ``resources=None`` is an installation with no managed cache to ask, which is
    the same determinate answer as a cache with nothing in it: the requested
    languages are not installed, with the narrower "nothing is configured at all"
    kept as the operator's detail. That is exactly what
    :meth:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue.resolve`
    answers for an explicit request against an unconfigured catalogue, so the
    condition a user sees does not depend on which of the two it was.
    """
    requested = validated_languages(languages)
    if resources is None:
        return AdmittedResources(
            RESOURCES_UNAVAILABLE,
            reason=REASON_NEEDS_LANGUAGE,
            unavailable_models=requested,
            detail=REASON_UNCONFIGURED,
        )
    try:
        with resources.coordinated(create=False) as coordinated:
            if not coordinated:
                # An absent cache is the whole snapshot, read from nothing, and
                # the root is deliberately not inspected after it: an install
                # arriving now leaves this an accurate *before* rather than a
                # view of a cache caught halfway into existence.
                return AdmittedResources(
                    RESOURCES_UNAVAILABLE,
                    reason=REASON_NEEDS_LANGUAGE,
                    unavailable_models=requested,
                    detail=REASON_UNCONFIGURED,
                )
            present = _present(resources.inventory())
            missing = tuple(
                language for language in requested if language not in present
            )
            if missing:
                # All or nothing, which is what `resolve` does with a partial
                # selection anyway. Pinning the installed half would hold bytes
                # against the operator's own removal for an extraction that
                # cannot run, and would pin models the profile never names.
                return AdmittedResources(
                    RESOURCES_UNAVAILABLE,
                    reason=REASON_NEEDS_LANGUAGE,
                    unavailable_models=missing,
                )
            selection = [present[language] for language in requested]
            orientation = present.get(ORIENTATION_MODEL)
            if orientation is not None:
                selection.append(orientation)
            resources.pin(pin, selection)
    except OcrResourceLifecycleRefused as refusal:
        if refusal.code in (REFUSAL_NOT_INSTALLED, REFUSAL_DIVERGENT):
            # Determinate, and only reachable from the pin itself: between the
            # inventory read and the claim nothing can have moved, because both
            # are inside one coordination block, so this is a cache whose state
            # document and bytes already disagree. Reconciliation is the repair
            # and the capture records what was true.
            return AdmittedResources(
                RESOURCES_UNAVAILABLE,
                reason=REASON_UNVERIFIED,
                unavailable_models=_refused_languages(refusal, requested),
            )
        raise ResourceStateIndeterminate(str(refusal)) from refusal
    except (OcrResourceStoreError, OSError) as error:
        raise ResourceStateIndeterminate(str(error)) from error
    return AdmittedResources(RESOURCES_FROZEN, selection=tuple(selection))


def _refused_languages(
    refusal: OcrResourceLifecycleRefused, requested: Sequence[str]
) -> tuple[str, ...]:
    """Which languages a refusal is actually about.

    The refusals raised over a named selection carry the identities they were
    raised for, so the condition can name exactly those. One that does not falls
    back to the whole requested order, which is truthful about the resources that
    could not be served even though it is less precise about why.
    """
    named = tuple(
        identity.language
        for identity in refusal.identities
        if identity.language != ORIENTATION_MODEL
    )
    return named or tuple(requested)


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

    @contextmanager
    def frozen_resources(
        self,
        *,
        operation_id: str,
        admitted: AdmittedResources,
        requested: Sequence[str],
    ) -> Iterator[OcrResources]:
        """Serve one capture's frozen identities, held for the caller's block.

        The execution end of :func:`admitted_resources`, and the narrow one: it is
        given the identities that capture was admitted against and it serves those
        or nothing. ``requested`` is the capture's own frozen recognition order,
        used for the order the resources report and never to look anything up --
        there is no language resolution here at all, which is what makes the
        substitution impossible rather than merely unlikely. A newer model
        revision of the same language, installed since, is a different identity
        and is not consulted.

        The claim is re-asserted and the bytes are held for the whole block, so
        the identities survive the invocation, and they are digested once more
        before the engine sees them -- by
        :meth:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue.resolve`,
        over the projected directory, exactly as every other resolution in this
        build is.

        What it yields when it cannot serve them is a resource *condition*, not an
        exception: an identity that is no longer installed is a missing language,
        and bytes that no longer match their digest are an unverified model. Both
        are recorded against the extraction and recovered by installing or
        reinstalling, and neither degrades into a smaller selection. A cache that
        could not be read at all is the one case that raises, because that is not
        a condition but the absence of one.
        """
        assert admitted.kind == RESOURCES_FROZEN
        order = validated_languages(requested)
        with ExitStack() as stack:
            try:
                directory = stack.enter_context(
                    self.resources.pinned_selection(
                        operation_id, admitted.selection
                    )
                )
            except OcrResourceLifecycleRefused as refusal:
                if refusal.code not in (REFUSAL_NOT_INSTALLED, REFUSAL_DIVERGENT):
                    raise ResourceStateIndeterminate(str(refusal)) from refusal
                yield OcrResources(
                    engine=TESSERACT_ENGINE,
                    engine_revision=TESSERACT_REVISION,
                    languages=order,
                    available=False,
                    reason=(
                        REASON_NEEDS_LANGUAGE
                        if refusal.code == REFUSAL_NOT_INSTALLED
                        else REASON_UNVERIFIED
                    ),
                    unavailable_models=_refused_languages(refusal, order),
                )
                return
            except (OcrResourceStoreError, OSError) as error:
                raise ResourceStateIndeterminate(str(error)) from error
            yield _catalogue_over(admitted.selection, directory).resolve(order)


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


def managed_resources(source: object) -> ManagedOcrResources | None:
    """The managed cache behind one resolution source, when there is one.

    Admission needs the cache itself rather than a resolution over it, because
    pinning is a write to the state document and no resolver exposes one. The
    source it is handed is whatever the installation was composed into -- an
    authority, or nothing -- and anything that is not an authority has no managed
    cache to pin against, which :func:`admitted_resources` records as the
    determinate "nothing is installed".

    Typed as :class:`object` on purpose: the union of the two shapes a resolution
    source may take is declared where the worker consumes it, and that module
    imports this one.
    """
    if isinstance(source, ManagedSelectionAuthority):
        return source.resources
    return None


__all__ = [
    "CAPTURE_RESOURCES_SCHEMA",
    "DEFAULT_SETTINGS",
    "EMBEDDED_IMAGE_MEDIA_TYPES",
    "OCR_MEDIA_TYPES",
    "PROCESSING_MODES",
    "RESOURCES_FROZEN",
    "RESOURCES_NONE",
    "RESOURCES_UNAVAILABLE",
    "SETTING_KEY",
    "SETTING_WRITER",
    "TEXT_ONLY_PROFILE_REF",
    "AdmittedResources",
    "ManagedSelectionAuthority",
    "ProcessingResourcesInvalid",
    "ProcessingSettings",
    "ProcessingSettingsInvalid",
    "ResourceStateIndeterminate",
    "admitted_resources",
    "apply_settings",
    "capture_pin",
    "capture_pin_prefix",
    "installation_catalogue",
    "installed_identities",
    "managed_resources",
    "read_settings",
    "resolves_resources",
    "selected_identities",
    "write_settings",
]
