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

What the identities decide, the digest seals.
:meth:`AdmittedResources.served` rebuilds the resources a frozen selection was
admitted with from the row alone -- no projection, no model read -- which is what
lets the admission's own profile digest be computed where nothing is projected
and recomputed at execution from the same row. The identities stay the authority
and the digest is never consulted to decide what to execute; it catches the one
input the row cannot hold, which is the build that reads it.

:class:`ManagedSelectionAuthority` also keeps its *language* resolution, used by
`document.history` and `document.reprocess`, because a reprocess is an explicit
new decision that resolves against the workspace's current preferences and runs
inside one request. Both resolutions hold what they resolve, because resolving a
selection and invoking the engine over it are two moments and the resources have
to survive both.

Two owners, one authority
-------------------------

§18.4 ships the engines and the default English resources as one signed
component set and installs *additional* languages into the managed cache beside
them, so an installation has two sources and :func:`installation_catalogue`
composes both from the one root this process was given. The bundled English and
orientation models are read straight out of the verified payload
(:mod:`omnivia_core_runtime.distribution.packaged_runtime`): they are identified
by the same five-field identity a managed set is, so a capture admitted against
them freezes, compares and replays through the machinery every other selection
uses, but nothing is pinned, projected or leased for them -- they are the
installation's bytes rather than the cache's, and no eviction can reach them.
Everything else is the managed path exactly as before, and either way the engine
invoked is the absolute executable the payload declares.

One Tesseract invocation takes one model directory, so a selection is served by
exactly one source: a mixed order falls to the managed cache, and a bundled
language the cache has not got is reported missing there. That is truthful and
its recovery is the documented one -- install the language through the managed
route -- rather than a projection mixing bytes from two owners under one digest.

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

from omnivia_core_runtime.distribution.packaged_runtime import (
    MODEL_IDENTITY,
    VerifiedPackagedRuntime,
    installed_runtime,
)
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    MAX_PROJECTED_MODELS,
    OCR_RESOURCES_DIR,
    REFUSAL_DIVERGENT,
    REFUSAL_NOT_INSTALLED,
    REFUSAL_UNPINNED,
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

#: Stands where the published projection's directory would be, in the
#: admission-time resources :meth:`AdmittedResources.served` rebuilds. A
#: projection is installation-local and is published at execution, so admission
#: has no directory to name -- and the profile does not carry one, which is why
#: naming nothing real here changes no identity. Not a path: nothing may open it,
#: and anything that tried would fail closed on a model it could not digest.
_UNPROJECTED_SELECTION: Final = "(selection not projected)"

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


class CaptureNotExecutable(Exception):
    """This capture cannot be executed as the capture it was admitted as.

    Not a resource condition and not the absence of one: both of those are
    statements about the installation, and this is a statement about the
    obligation. Two things reach it, with the same remedy -- an operator looking,
    or an explicit reprocess -- and the same handling.

    Either the durable claim the capture was admitted with is not there, or is
    over another selection. The bytes may well still be installed, which is
    exactly why executing would be wrong: they were removable for the whole
    window nothing held them, and
    :meth:`~omnivia_core_runtime.service.ocr_resource_lifecycle.ManagedOcrResources.assert_pinned`
    refuses rather than writing the claim back.

    Or the frozen documents no longer produce the profile digest the admission
    recorded, which means this build resolves them to a different extraction
    profile than the build that admitted them did -- a changed adapter, decoder,
    engine revision or limit. Settling under the new digest would settle the
    capture under an identity nobody was offered, and under an operation key
    derived from it, so the obligation is paused with the digest it was admitted
    under intact.

    The obligation is left exactly as it was in both cases: its outbox row still
    pending, its durable pins still held, and no extraction revision admitted.
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

    def served(self, requested: Sequence[str]) -> OcrResources:
        """What a frozen selection serves, as the admission resolved it.

        The *admission-time* resources, rebuilt from the row alone: the engine
        this build invokes, the frozen recognition order, and one pinned model
        hash per identity the selection names -- which is what
        :meth:`~omnivia_core_runtime.service.ocr_resources.TrustedOcrCatalogue.resolve`
        produces over the same identities, because the digest it verifies the
        bytes against *is* the identity's. That is what lets the profile digest a
        capture is admitted under be computed without reading a model, and lets
        execution recompute the same digest from the same row.

        ``tessdata_dir`` is the one member that cannot be rebuilt, and is the one
        member the profile does not carry:
        :meth:`~omnivia_core_runtime.service.ocr_resources.OcrResources.to_profile`
        leaves it out, so a capture's identity does not depend on where an
        installation happens to publish a projection. The placeholder is
        therefore inert, and these resources are never handed to an engine -- the
        resources an extraction runs under come from
        :meth:`ManagedSelectionAuthority.frozen_resources`, over a directory that
        exists.
        """
        assert self.kind == RESOURCES_FROZEN
        return OcrResources(
            engine=TESSERACT_ENGINE,
            engine_revision=TESSERACT_REVISION,
            languages=validated_languages(requested),
            model_hashes=tuple(
                sorted({f"sha256:{identity.sha256}" for identity in self.selection})
            ),
            tessdata_dir=_UNPROJECTED_SELECTION,
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
    packaged: Mapping[str, OcrResourceSetIdentity] = {},
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

    ``packaged`` is this installation's bundled set, from
    :func:`packaged_identities` over a payload whose bytes were just verified,
    and it is asked **first**: 11.12.2 says the model directory comes from the
    signed installation manifest, so where the bundled set covers the whole
    selection it is the answer even if the managed cache also holds one of those
    languages. Such a selection is frozen against the packaged identities and
    **not** pinned -- a pin is a claim on the managed cache's bytes against the
    managed cache's own eviction, and these are neither its bytes nor evictable.
    There is therefore nothing to hold, nothing to reclaim and no coordination
    block to enter, which is also why a default English capture is admitted on an
    installation whose managed cache does not exist at all.
    """
    requested = validated_languages(languages)
    if packaged and all(language in packaged for language in requested):
        selection = [packaged[language] for language in requested]
        orientation = packaged.get(ORIENTATION_MODEL)
        if orientation is not None:
            selection.append(orientation)
        return AdmittedResources(RESOURCES_FROZEN, selection=tuple(selection))
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
    selection: Sequence[OcrResourceSetIdentity],
    directory: Path,
    tesseract_path: str | None,
) -> TrustedOcrCatalogue:
    """The trusted catalogue one published projection of `selection` serves."""
    return TrustedOcrCatalogue(
        installed={identity.language: identity.sha256 for identity in selection},
        tessdata_dir=str(directory),
        tesseract_path=tesseract_path,
    )


def packaged_identities(
    runtime: VerifiedPackagedRuntime | None,
) -> dict[str, OcrResourceSetIdentity]:
    """The bundled models, as the same five-field identities the cache uses.

    11.2 and 17.3 bundle English and the orientation data with the distribution,
    and §18.4 ships them as part of one signed component set. They are therefore
    identified exactly as a managed resource set is -- language, engine revision,
    model version, size and digest -- so a capture admitted against them freezes,
    compares and replays through the machinery every other selection already
    uses, and its profile pins the same kind of model hash.

    ``model_version`` is :data:`MODEL_IDENTITY`: the bundled set has one version,
    which is the bytes the installation shipped, and the digest beside it is what
    actually distinguishes them. The size and digest come from the verified
    manifest and from nowhere else, so an identity here is a claim the
    installation's own record just stood behind.
    """
    if runtime is None:
        return {}
    return {
        language: OcrResourceSetIdentity(
            language=language,
            engine_version=TESSERACT_REVISION,
            model_version=MODEL_IDENTITY,
            byte_size=size,
            sha256=digest,
        )
        for language, (size, digest) in runtime.models.items()
    }


def _packaged_catalogue(
    runtime: VerifiedPackagedRuntime | None,
) -> TrustedOcrCatalogue:
    """What the bundled models serve, read straight out of the packaged payload.

    The model directory is the one inside the verified candidate and the engine
    is the absolute executable beside it, so a default English recognition runs
    entirely out of bytes the installation manifest declared. No projection is
    published and nothing is leased: packaged bytes belong to the installation
    rather than to the managed cache, so there is no eviction for a lease to
    stand in the way of.

    An installation with no verified payload gets the empty catalogue, which
    serves nothing -- the fail-closed default this build has always had.
    """
    if runtime is None:
        return TrustedOcrCatalogue()
    return TrustedOcrCatalogue(
        installed={
            language: digest for language, (_size, digest) in runtime.models.items()
        },
        tessdata_dir=str(runtime.tessdata_dir),
        tesseract_path=str(runtime.tesseract),
    )


def _serves_packaged(
    runtime: VerifiedPackagedRuntime | None, requested: Sequence[str]
) -> bool:
    """Whether the bundled models alone cover this whole ordered selection.

    All of it or none of it. One Tesseract invocation takes one model directory,
    so a selection mixing a bundled language with a managed one cannot be served
    out of the packaged directory; such a selection goes to the managed cache,
    and a language the cache has not got is reported as missing there. That is
    the truthful answer and its recovery is the documented one -- install the
    language through the managed route -- rather than a second projection that
    would mix bytes from two owners under one digest.
    """
    if runtime is None:
        return False
    return all(language in runtime.models for language in requested)


class _HeldSelections:
    """Every selection one resolution leased, released together at its end.

    One resolution may ask for the same selection more than once -- a history
    page offers approved profiles per media type, and every media type of one
    workspace names the same recognition order -- so an answer already resolved
    is reused rather than re-projected. That is also what makes the digest a
    caller is offered and the digest that executes one resolution rather than
    two.
    """

    def __init__(
        self,
        resources: ManagedOcrResources,
        runtime: VerifiedPackagedRuntime | None = None,
    ) -> None:
        self._resources = resources
        self._runtime = runtime
        self._packaged = _packaged_catalogue(runtime)
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
        if _serves_packaged(self._runtime, validated_languages(requested)):
            # The bundled models cover the whole selection, so it is served out
            # of the verified packaged directory. Nothing is projected and
            # nothing is leased: these bytes are the installation's own and no
            # managed eviction can reach them.
            self._resolved[key] = self._packaged
            return self._packaged
        try:
            selection = selected_identities(self._resources, requested)
            if selection:
                directory = self._stack.enter_context(
                    self._resources.leased_selection(selection)
                )
                answer = _catalogue_over(
                    selection, directory, self._packaged.tesseract_path
                )
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

    It also carries this installation's verified packaged runtime, because the
    two halves of §18.4 are one component set: the bundled English and
    orientation models and the engines that read them ship with the
    distribution, and additional languages are installed into the managed cache
    beside them. A selection the bundled models cover is served from the
    packaged directory with no lease at all; anything else is the managed path
    exactly as before, invoked through the same packaged engine.
    """

    resources: ManagedOcrResources
    #: The verified payload, or ``None`` for an installation with none -- which
    #: serves nothing and invokes no engine, the fail-closed default.
    runtime: VerifiedPackagedRuntime | None = None

    @property
    def packaged(self) -> TrustedOcrCatalogue:
        """What the bundled models serve, read out of the verified payload."""
        return _packaged_catalogue(self.runtime)

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
        held = _HeldSelections(self.resources, self.runtime)
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
        reinstalling, and neither degrades into a smaller selection. Two cases
        raise instead, because neither is a condition: a cache that could not be
        read at all, which is the absence of an answer, and a durable claim that
        is not there, which is :class:`CaptureNotExecutable` -- the bytes may be
        installed and verifiable, and what is missing is the record that this
        obligation held them, which no installation repairs and which this end
        deliberately does not write back.

        An identity admitted for an engine revision this build does not invoke
        raises too, before anything is projected. The alternative would be to
        serve those bytes and label the profile with the engine that actually
        ran, which moves the capture's digest -- and therefore its operation key
        -- away from the one its admission recorded, under an identity nobody was
        offered. There is no compatible way to serve it here, so it waits for an
        operator or an explicit reprocess.
        """
        assert admitted.kind == RESOURCES_FROZEN
        order = validated_languages(requested)
        foreign = tuple(
            identity.stable_key
            for identity in admitted.selection
            if identity.engine_version != TESSERACT_REVISION
        )
        if foreign:
            raise CaptureNotExecutable(
                f"this capture was admitted against OCR resources for another "
                f"engine revision than {TESSERACT_REVISION}: {', '.join(foreign)}"
            )
        bundled = tuple(
            identity
            for identity in admitted.selection
            if identity.model_version == MODEL_IDENTITY
        )
        if bundled and len(bundled) == len(admitted.selection):
            # Admitted wholly against the bundled set, which
            # :data:`~omnivia_core_runtime.distribution.packaged_runtime.MODEL_IDENTITY`
            # is the marker for: only the packaged path writes that model
            # version, and a managed installation that claimed it would be
            # misstating which bytes it installed.
            packaged = packaged_identities(self.runtime)
            if all(
                packaged.get(identity.language) == identity
                for identity in admitted.selection
            ):
                # The identities are compared whole -- size and digest included
                # -- against the payload just verified. No pin was ever taken
                # for these bytes and none is asserted: the managed cache does
                # not own them and cannot evict them.
                yield self.packaged.resolve(order)
                return
            # The payload is absent, or its bytes are not the ones this capture
            # was admitted against -- a reinstalled or replaced installation.
            # That is a resource *condition*, recorded against the extraction
            # and terminal: deferring to the managed path would ask it for a
            # durable pin nothing ever wrote, which pauses the obligation for
            # ever over bytes no installation will put back under that name.
            yield OcrResources(
                engine=TESSERACT_ENGINE,
                engine_revision=TESSERACT_REVISION,
                languages=order,
                available=False,
                reason=REASON_UNVERIFIED,
                unavailable_models=tuple(
                    identity.language
                    for identity in admitted.selection
                    if packaged.get(identity.language) != identity
                    and identity.language != ORIENTATION_MODEL
                )
                or order,
            )
            return
        with ExitStack() as stack:
            try:
                directory = stack.enter_context(
                    self.resources.pinned_selection(
                        operation_id, admitted.selection
                    )
                )
            except OcrResourceLifecycleRefused as refusal:
                if refusal.code == REFUSAL_UNPINNED:
                    raise CaptureNotExecutable(str(refusal)) from refusal
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
            yield _catalogue_over(
                admitted.selection, directory, self.packaged.tesseract_path
            ).resolve(order)


def installation_catalogue(
    installation_root: Path | None,
) -> ManagedSelectionAuthority | None:
    """How one installation resolves a selection, or ``None`` when it cannot.

    Both halves of §18.4 are composed here, from the one trusted pointer this
    process was given: the managed language cache under the installation-state
    root, and the verified packaged runtime an installer placed beneath it. The
    payload is re-verified on every composition -- every declared byte digested
    -- because a cached verdict cannot notice a file that moved since. It is a
    start-time cost and not a per-request one: this is called while the service
    is being composed, and the authority it returns is then held. Measured at
    0.16 s for a 286 MiB payload on Apple silicon, where SHA-256 is accelerated.

    ``None`` means this process has no installation root to ask, or the root is
    not one a managed cache may live under. The caller then falls back to the
    packaged fail-closed catalogue, which serves nothing -- fail-closed, not
    host-discovered. An installation with a cache but no verified payload is an
    authority that serves managed languages and invokes no engine, which is the
    same fail-closed answer one step further in.
    """
    if installation_root is None:
        return None
    try:
        resources = ManagedOcrResources(Path(installation_root) / OCR_RESOURCES_DIR)
    except OcrResourceStoreError:
        return None
    return ManagedSelectionAuthority(
        resources, runtime=installed_runtime(installation_root)
    )


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


def packaged_runtime(source: object) -> VerifiedPackagedRuntime | None:
    """The verified packaged runtime behind one resolution source, if there is one.

    The same shape as :func:`managed_resources` and for the same reason: the
    callers that need the packaged *paths* -- an admission that freezes the
    bundled identities, and an extraction that invokes the engines -- hold the
    composed source and not the payload, and the union of the shapes that source
    may take is declared in the module that consumes it.
    """
    if isinstance(source, ManagedSelectionAuthority):
        return source.runtime
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
    "CaptureNotExecutable",
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
    "packaged_identities",
    "packaged_runtime",
    "read_settings",
    "resolves_resources",
    "selected_identities",
    "write_settings",
]
