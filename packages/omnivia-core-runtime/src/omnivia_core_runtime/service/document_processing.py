"""Bounded processing of durable document-capture outbox obligations.

Every obligation executes from its own capture row's two frozen documents and
from nothing process-global: ``processing_settings_json`` (migration 0071), which
decides the mode, the ordered recognition languages and whether embedded raster
images are examined, and ``processing_resources_json`` (migration 0072), which
names the exact ordered OCR resource-set identities the capture was admitted
*against*. A preference changed, a model updated, a language removed, or the
service restarted between admission and execution therefore cannot move what a
queued or active obligation does -- the obligation is replayed from the same row,
the profile it resolves to is the same profile, and the operation key derived from
that profile's digest is the same key.

**Execution resolves no language against the installation.** There is no code
path here that asks "which models are installed for these languages"; that
question was asked once, at admission, and its answer is on the row.
:meth:`~omnivia_core_runtime.service.processing_settings.ManagedSelectionAuthority.frozen_resources`
is handed the identities and serves those or reports a condition, so a newer model
revision of the same language -- installed while the obligation waited -- is a
different identity and is never consulted. That is what makes substitution
impossible rather than merely unlikely, and it is why the resource half of a
capture can only change through an explicit reprocess, which is a new decision.

**What a queued obligation needs, it durably pins; what an executing one needs,
it also holds.** The two cover different windows and neither is the other. The pin
is a record in the managed cache's state document, written at admission and
released only once the obligation reaches a terminal disposition, so the bytes
survive an unbounded queue wait and a restart. The lease is an OS lock that dies
with the process, so it covers the minutes between resolving a projection and
finishing with the engine over it.
:meth:`~omnivia_core_runtime.service.ocr_resource_lifecycle.ManagedOcrResources.pinned_selection`
takes both, *checking* the pin before it resolves -- read-only, so a pin that is
not there pauses the obligation instead of being written back under it, and an
obligation never invokes the engine over bytes nothing holds.

**And the identity is compared, not merely derived.** The profile digest one
capture was admitted under is stored on its row (migration 0072) and recomputed
here from the same two frozen documents before any extraction revision is
admitted. The inputs cannot have moved, so a difference is this build resolving
them differently from the build that admitted them -- a changed adapter, decoder
build or engine revision -- and the obligation pauses. Settling under the new
digest would record an extraction under an identity nobody was offered and
abandon the operation key the admitted one derives.

**A condition is settled; the absence of one is not.** Resources that are
demonstrably not there -- an identity no longer installed, bytes that no longer
match it -- are a resource condition, recorded against the extraction with the
recovery that fixes it, and the obligation is terminal. A managed cache that
could not be *read* is not a condition but the lack of one, and settling an
extraction on it would burn a capture over a lock another administrator happened
to be holding. Such an obligation is left exactly as it was: nothing resolved,
no extraction revision admitted, nothing settled, and the same row replayed under
the same key on a later pass.

**Two null columns are the compatibility paths, and both are deterministic.** A
null settings snapshot means what migration 0071 says: a capture admitted before
the column existed, which executes the behaviour that applied then -- the mode its
profile reference names, no embedded-image pass, and an *unconfigured* language
request. A null resource snapshot means what 0072 says, and resolves against the
packaged fail-closed catalogue, which installs no model and names no model
directory. Neither invents an identity, and in particular neither consults the
installation's current cache, so a model installed after such a capture was
admitted cannot change what it does. A non-null column that does not decode is
never reinterpreted as its own absence: it settles as a failed extraction naming
:data:`PROCESSING_SETTINGS_INVALID` or :data:`PROCESSING_RESOURCES_INVALID`,
because reading an unreadable decision as an absent one would run something
nobody asked for under the key of something they did.

**Crash recovery is by reclamation, not by trust.** A pin is written before the
capture that owns it is durable and released after that capture's obligation has
settled -- the only order with no instant in which the bytes are removable while
the obligation still needs them, and therefore an order whose two crash
boundaries both leave a pin that no obligation holds. :func:`reclaim_capture_pins`
takes those back: it computes the *exact* set of unsettled obligations, under the
SQLite gate every admission also runs inside, and releases every pin of this
workspace that is not in it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import document_intake
from omnivia_core_runtime.service.document_extractors import (
    HTML_MEDIA_TYPES,
    MAX_HTML_SOURCE_BYTES,
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
)
from omnivia_core_runtime.service.document_knowledge import (
    DocumentKnowledgeRefused,
    derive_document_knowledge,
)
from omnivia_core_runtime.service.document_worker import (
    DocumentWorkerFailed,
)
from omnivia_core_runtime.service.document_worker import (
    # The production extraction route is the confined child process (DOC-16),
    # not the in-process adapters. It is bound to the adapters' own name because
    # it is the same seam: same signature, same outcome, same two refusals --
    # only the process it runs in is different.
    extract_document_isolated as extract_document,
)
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    OcrResourceLifecycleRefused,
)
from omnivia_core_runtime.service.ocr_resource_store import OcrResourceStoreError
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    OCR_SECONDS_PER_DOCUMENT,
    PACKAGED_OCR_CATALOGUE,
    REASON_NEEDS_LANGUAGE,
    REASON_UNCONFIGURED,
    TESSERACT_ENGINE,
    TESSERACT_REVISION,
    ExtractionSettings,
    OcrResources,
    OcrSelectionInvalid,
    TrustedOcrCatalogue,
    validated_languages,
)
from omnivia_core_runtime.service.processing_settings import (
    DEFAULT_SETTINGS,
    OCR_MEDIA_TYPES,
    RESOURCES_FROZEN,
    RESOURCES_NONE,
    RESOURCES_UNAVAILABLE,
    TEXT_ONLY_PROFILE_REF,
    AdmittedResources,
    CaptureNotExecutable,
    ProcessingResourcesInvalid,
    ProcessingSettings,
    ProcessingSettingsInvalid,
    ResourceStateIndeterminate,
    capture_pin,
    capture_pin_prefix,
    managed_resources,
    resolves_resources,
)
from omnivia_core_runtime.service.raster_extractors import (
    DEDUPLICATION_VERSION,
    IMAGE_DECODER_BUILD,
    MAX_FRAME_PIXELS,
    MAX_SOURCE_BYTES,
    MAX_WORKING_PIXELS,
    PDF_DECODER_BUILD,
    READING_ORDER_VERSION,
)
from omnivia_core_runtime.service.raster_extractors import (
    MAX_DECODE_SECONDS as RASTER_DECODE_SECONDS,
)
from omnivia_core_runtime.service.raster_extractors import (
    MAX_TEXT_BYTES as RASTER_TEXT_BYTES,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.storage import document_intake as store

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_PROCESSING_BUDGET = 4
#: The profile reference that selects the product default: Text and local OCR where the
#: media type qualifies for it, and direct text everywhere else. Named here, rather than
#: left to whatever string a caller sends, because `document.history` reports the exact
#: approved selections a reprocess may name and a caller must not synthesise its own.
DEFAULT_PROFILE_REF = "document-default"
#: The one approved revision of both references in this build. It is part of the profile's
#: content address through `decoderBuild`, so changing it is a new profile identity.
APPROVED_PROFILE_REVISION = "1"
#: Bumped with the raster/OCR adapter's geometry, dedup and coverage contract.
_ADAPTER_VERSION = "2"
#: The limitation code a Needs OCR language outcome carries.
NEEDS_OCR_LANGUAGE = REASON_NEEDS_LANGUAGE
#: The limitation code a capture whose frozen snapshot does not decode settles with.
#: Distinct from every resource condition: the preferences this capture was admitted
#: under cannot be established at all, so there is no requested profile to keep and
#: nothing about the installation to recover by installing.
PROCESSING_SETTINGS_INVALID = "processing_settings_invalid"
#: The same for the resource snapshot, and deliberately its own code: an operator
#: reading one of these has to know which of the capture's two frozen documents is
#: unreadable, and the recovery differs from every resource condition -- installing
#: or reinstalling a model cannot repair a record of which models were named.
PROCESSING_RESOURCES_INVALID = "processing_resources_invalid"

#: Resolves the trusted catalogue for one ordered *language* selection, at the
#: moment the work runs. A callable rather than a catalogue because the managed
#: cache publishes one directory per selection, so there is no single catalogue
#: that could serve every selection -- see
#: :func:`~omnivia_core_runtime.service.processing_settings.installation_catalogue`.
#:
#: This is the reprocess side. A reprocess is an explicit new decision resolved
#: against the workspace's current preferences and executed inside one request, so
#: it does have languages to resolve; a queued obligation does not, and asks
#: :class:`FrozenResourceSource` for the identities it was admitted against
#: instead.
CatalogueFor = Callable[[Sequence[str]], TrustedOcrCatalogue]


@runtime_checkable
class SelectionLeaseSource(Protocol):
    """An installation that can *hold* the resources one resolution pins.

    The installation's managed cache is one; the packaged fail-closed catalogue is
    not, because it serves nothing there would be anything to hold. Structural
    rather than nominal so this module depends on the behaviour and not on which
    module supplies it.
    """

    def lease(self) -> AbstractContextManager[CatalogueFor]: ...


@runtime_checkable
class FrozenResourceSource(Protocol):
    """An installation that can serve one capture's *frozen* resource identities.

    The worker's only route to OCR resources. It deliberately takes identities
    rather than languages: a source that could be asked "what serves these
    languages" is a source that could answer with a model installed since
    admission, and there is no such question here to answer wrongly.
    """

    def frozen_resources(
        self,
        *,
        operation_id: str,
        admitted: AdmittedResources,
        requested: Sequence[str],
    ) -> AbstractContextManager[OcrResources]: ...


#: Either way of naming where OCR resources come from: a plain resolver, which
#: holds nothing because there is nothing installation-local to hold, or an
#: installation that leases what it resolves.
CatalogueSource = CatalogueFor | SelectionLeaseSource


@contextmanager
def held_selection(source: CatalogueSource | None) -> Iterator[CatalogueFor | None]:
    """Hold whatever one unit of work resolves, for the whole of that work.

    Resolving a selection and invoking the engine over it are two moments. Between
    them the operator may remove a language, a reconciliation may quarantine one,
    and the managed cache's own pruning may discard the projected directory -- any
    of which would leave an extraction pointed at a directory that is no longer
    there. An installation that can lease is asked for a fresh hold here, per unit
    of work, so two units never share one and the end of one cannot release the
    other's.

    A plain resolver and ``None`` pass straight through: there is nothing
    installation-local to hold, and the lease is not a second gate on resolution.
    """
    if isinstance(source, SelectionLeaseSource):
        with source.lease() as catalogue_for:
            yield catalogue_for
        return
    yield source


@dataclass
class DocumentProcessingExecutor:
    """Rate-limited service-owned document processing.

    The service tick calls this frequently. The executor keeps each pass bounded and
    shares the runner's SQLite gate with request transports; extraction itself runs
    outside the gate so a large document cannot monopolise request admission.
    """

    runner: ServiceRunner
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    #: Where this installation's OCR resources come from. The executor holds no
    #: language and resolves none: which identities an obligation runs against is
    #: its own capture row, frozen and pinned at admission, and this is only asked
    #: whether it can still serve those exact identities -- under a re-asserted pin
    #: and a lease that lasts the whole extraction. It is also what the pins of
    #: abandoned obligations are reclaimed through. Anything that cannot serve a
    #: frozen selection, ``None`` included, leaves such a capture reporting
    #: unavailable resources rather than resolving a substitute.
    catalogue_for: CatalogueSource | None = None
    _next_poll: float = 0.0
    #: Whether this instance has already taken back the durable resource pins no
    #: obligation holds. Once per instance, which is once per service start: the
    #: pins that need reclaiming are the ones a previous process left behind, and
    #: a sweep is a whole-workspace read that has no business running on a tick.
    _reclaimed: bool = False

    def run_pending(
        self,
        *,
        budget: int = DEFAULT_PROCESSING_BUDGET,
        force: bool = False,
    ) -> list[store.StoredResult]:
        if budget <= 0:
            return []
        now = self.runner.clock.monotonic()
        if not force and now < self._next_poll:
            return []
        self._next_poll = now + max(self.poll_interval_seconds, 0.0)
        self._reclaim_once()
        try:
            return process_capture_outbox(
                self.runner, limit=budget, catalogue_for=self.catalogue_for
            )
        except sqlite3.Error:
            # SQLite contention and lost ownership leave the durable outbox intact.
            # A later bounded pass replays the same operation key.
            return []

    def _reclaim_once(self) -> None:
        """Take back the pins of the obligations a previous process left, once.

        Before the first pass processes anything, so an abandoned pin is released
        at the earliest moment this instance could know it was abandoned rather
        than at the end of whatever backlog it inherited.

        A sweep that could not run is simply not marked done and is attempted
        again on the next pass -- a contended cache or an unreadable outbox is a
        reason to try later, never a reason to fail the processing pass this was
        a tidy-up before, and never a reason to go on believing the pins were
        reclaimed.
        """
        if self._reclaimed:
            return
        try:
            reclaim_capture_pins(self.runner, self.catalogue_for)
        except (
            OcrResourceLifecycleRefused,
            OcrResourceStoreError,
            OSError,
            sqlite3.Error,
            store.DocumentIntakeInvalid,
        ):
            return
        self._reclaimed = True


def resolve_settings(
    *,
    media_type: str,
    profile_ref: str,
    settings: ProcessingSettings | None = DEFAULT_SETTINGS,
    catalogue_for: CatalogueFor | None = None,
    resources: OcrResources | None = None,
) -> ExtractionSettings:
    """The trusted processing decisions for one capture.

    Nothing here reads the document. `settings` is the capture's own frozen
    snapshot -- its mode, its ordered recognition languages and its
    embedded-image preference -- and it is the only source of all three.

    Two mutually exclusive ways to name the OCR resources, because the two
    callers have them at different moments. `catalogue_for` resolves the
    snapshot's languages against an installation *now*, which is what a reprocess
    does: it is an explicit new decision, taken inside one request, and the
    workspace's current preferences are the approved ones. `resources` is already
    resolved -- the capture's frozen identities, served or truthfully unserved --
    which is what the worker passes, because a queued obligation's resources were
    decided at admission and resolving them again is the one thing it must not
    do. Given both, `resources` wins; given neither, the packaged fail-closed
    catalogue answers, and it serves nothing.

    A missing or unverified model does not change the selected mode and never
    quietly becomes English: the returned settings keep the Text-and-local-OCR
    identity and the requested order, and say that the resources were not served.
    A malformed selection is a configuration fault and is raised.

    ``settings=None`` is the documented compatibility path for a capture admitted
    before migration 0071, which recorded no selection: the mode is the one its
    profile reference names, no embedded-image pass runs, and the language request
    is *empty* -- an unconfigured installation, which is what such a capture was
    admitted under.
    """
    languages: Sequence[str] = () if settings is None else settings.languages
    if not resolves_resources(
        media_type=media_type, profile_ref=profile_ref, settings=settings
    ):
        # Returned before the catalogue is asked anything: a text-only capture
        # resolves no OCR resource, takes no lock on the managed cache and pins
        # no model it would never read.
        return ExtractionSettings(processing_mode=MODE_TEXT_ONLY)
    if resources is None:
        catalogue = (
            PACKAGED_OCR_CATALOGUE
            if catalogue_for is None
            else catalogue_for(languages)
        )
        resources = catalogue.resolve(languages)
    return ExtractionSettings(
        processing_mode=MODE_TEXT_AND_LOCAL_OCR,
        ocr=resources,
        embedded_image_ocr=(
            settings is not None and settings.embedded_image_ocr_for(media_type)
        ),
    )


def _frozen_ocr_resources(
    settings: ProcessingSettings | None, admitted: AdmittedResources | None
) -> OcrResources | None:
    """The OCR resources one capture row names *by itself*, asking nothing.

    The installation-free half of :func:`capture_resources`, and the same three
    branches read from the same two frozen documents: a capture that resolves no
    recognition names none, an unavailable snapshot names the condition it
    recorded, and a frozen selection names the resources its admission resolved.
    What is missing is the only part an installation is needed for -- the
    projected directory the engine opens -- which no profile carries.

    That is what makes an admission-time profile digest computable at admission,
    where nothing is projected, and recomputable at execution without resolving
    anything: both ends run this over the same row and get the same answer.
    """
    if admitted is None or admitted.kind == RESOURCES_NONE:
        return None
    requested = () if settings is None else settings.languages
    if admitted.kind == RESOURCES_UNAVAILABLE:
        return admitted.unserved(requested)
    return admitted.served(requested)


def admitted_profile_digest(
    *,
    media_type: str,
    profile_ref: str,
    revision: str,
    settings: ProcessingSettings | None,
    admitted: AdmittedResources | None,
) -> str:
    """The ExtractionProfileV1 digest one capture's frozen decision resolves to.

    Computed at admission from the two documents that are about to be written and
    stored on the row beside them (`processing_profile_digest`, migration 0072),
    and computed again at execution from the row itself. The inputs are the same
    both times and nothing mutable is consulted either time -- not the live
    preferences, not the installation's cache -- so the two agree unless *this
    build* resolves the same frozen decision to a different profile than the
    build that admitted it did: a changed adapter, decoder build, engine
    revision, parser feature or limit.

    When they disagree, the capture's identity would move. The profile digest is
    the input to the extraction's operation key, so settling under the new one
    would both record an extraction nobody was offered and break the replay of
    the one that was. :func:`_process_obligation` therefore pauses the obligation
    rather than admitting a revision under it, which is the one answer that keeps
    the stored digest meaningful.

    ``settings=None`` and ``admitted=None`` are the pre-0071 and pre-0072
    compatibility rows, which carry no stored digest to compare against; the
    value this returns for them is still the profile they execute under.
    """
    return store.profile_digest(
        portable_profile(
            media_type=media_type,
            profile_ref=profile_ref,
            revision=revision,
            settings=resolve_settings(
                media_type=media_type,
                profile_ref=profile_ref,
                settings=settings,
                resources=_frozen_ocr_resources(settings, admitted),
            ),
        )
    )


def frozen_resources(capture: store.StoredCapture) -> AdmittedResources | None:
    """The resources this capture was admitted against, decoded from its own row.

    ``None`` only for a capture admitted before migration 0072, which recorded no
    resource selection at all; :func:`capture_resources` documents what that
    executes as. A stored document that is present but does not decode raises,
    for the reason :func:`frozen_settings` raises: reading it as the legacy "no
    selection" case would reinterpret an unreadable decision as an absent one,
    and the absent one resolves -- which is exactly the resolution this capture
    was admitted to avoid.
    """
    stored = capture.processing_resources_json
    if stored is None:
        return None
    try:
        return AdmittedResources.from_value(json.loads(stored))
    except (TypeError, ValueError) as error:
        raise ProcessingResourcesInvalid(
            f"the frozen OCR resources of {capture.capture_id} do not decode: "
            f"{error}"
        ) from error


@contextmanager
def capture_resources(
    capture: store.StoredCapture,
    *,
    settings: ProcessingSettings | None,
    admitted: AdmittedResources | None,
    source: CatalogueSource | None,
    operation_id: str,
) -> Iterator[OcrResources | None]:
    """The OCR resources this capture executes with, held for the caller's block.

    Every branch is decided by what the capture row *recorded*, never by what the
    installation holds now:

    ``None`` recorded (pre-0072) resolves against the packaged fail-closed
    catalogue, which installs no model and names no model directory. That is the
    behaviour such a capture was admitted under, it invents no identity, and it
    is the one answer a model installed since cannot change.

    :data:`RESOURCES_NONE` yields ``None``: this capture resolves no recognition,
    so there is nothing to serve and nothing was ever pinned.

    :data:`RESOURCES_UNAVAILABLE` replays the condition recorded at admission,
    exactly as it was recorded. It is deliberately not re-resolved: a selection
    the installation could not serve then must not become servable because a
    language was installed while the obligation waited, because the capture's
    profile digest -- and the operation key derived from it -- would move with it.
    The recovery is an explicit reprocess.

    :data:`RESOURCES_FROZEN` is served by the installation, from the exact
    identities, under a re-asserted pin and a live lease. An installation that is
    no longer there cannot serve them, which is the missing-language condition
    with the unconfigured installation as the operator's detail -- the same answer
    the packaged catalogue gives for an explicit request, so what a user sees does
    not depend on which of the two it was.
    """
    requested = () if settings is None else settings.languages
    if admitted is None:
        yield resolve_settings(
            media_type=capture.media_type,
            profile_ref=capture.processing_profile_ref,
            settings=settings,
        ).ocr
        return
    if admitted.kind == RESOURCES_NONE:
        yield None
        return
    if admitted.kind == RESOURCES_UNAVAILABLE:
        yield admitted.unserved(requested)
        return
    assert admitted.kind == RESOURCES_FROZEN
    if not isinstance(source, FrozenResourceSource):
        yield OcrResources(
            engine=TESSERACT_ENGINE,
            engine_revision=TESSERACT_REVISION,
            languages=validated_languages(requested),
            available=False,
            reason=REASON_NEEDS_LANGUAGE,
            unavailable_models=admitted.languages,
            resource_detail=REASON_UNCONFIGURED,
        )
        return
    with source.frozen_resources(
        operation_id=operation_id, admitted=admitted, requested=requested
    ) as resources:
        yield resources


def reclaim_capture_pins(
    runner: ServiceRunner, source: CatalogueSource | None
) -> tuple[str, ...]:
    """Release every durable pin of this workspace that no obligation holds.

    The recovery half of pinning at admission. Because the pin is written before
    the capture row is durable and released after that capture's obligation has
    settled -- the only order that leaves no instant in which the bytes are
    removable while the obligation still needs them -- a crash on either side of
    the work leaves a pin behind: one taken for an admission that then rolled
    back, and one whose obligation settled before the release landed. Neither is
    distinguishable from a live claim by looking at the state document, which is
    what a durable record *is*, so the component that wrote them says.

    The live set is read whole rather than as a page, and read under
    ``runner.sqlite_gate`` which is held across the reclamation itself. That is
    what makes this safe against a capture being admitted at the same moment:
    every request handler runs inside that gate, so an admission cannot write its
    pin and commit its row around a sweep that has already decided its pin is
    nobody's. The lock order is the same one admission takes -- the gate first,
    then the managed cache's coordination -- so the two serialise rather than
    deadlock.

    Scoped to this workspace's own pins by prefix: one installation's managed
    cache may serve more than one workspace, and a sweep computed from one
    workspace's obligations must not be able to reach another's.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    resources = managed_resources(source)
    if resources is None:
        return ()
    workspace_id = runner.workspace_id
    with runner.sqlite_gate:
        live = {
            capture_pin(workspace_id, capture_id)
            for capture_id in store.unsettled_capture_ids(
                runner.connection, workspace_id=workspace_id
            )
        }
        return resources.reclaim(capture_pin_prefix(workspace_id), live)


def frozen_settings(capture: store.StoredCapture) -> ProcessingSettings | None:
    """The preferences this capture was admitted under, decoded from its own row.

    ``None`` only for a capture admitted before migration 0071, which recorded no
    selection at all; :func:`resolve_settings` documents what that executes as. A
    stored document that is present but does not decode raises, because the one
    thing that must not happen is reading it as the legacy "no selection" case:
    that would reinterpret an unreadable decision as an absent one and run
    something nobody asked for under the key of something they did.
    """
    stored = capture.processing_settings_json
    if stored is None:
        return None
    try:
        return ProcessingSettings.from_value(json.loads(stored))
    except (TypeError, ValueError) as error:
        raise ProcessingSettingsInvalid(
            f"the frozen processing settings of {capture.capture_id} do not decode: "
            f"{error}"
        ) from error


def portable_profile(
    *,
    media_type: str,
    profile_ref: str,
    revision: str,
    settings: ExtractionSettings | None = None,
) -> dict[str, Any]:
    """The immutable profile for the portable direct-text and local-OCR worker."""
    adapter = {
        "text/plain": "plain-text",
        "text/markdown": "markdown-text",
        "text/csv": "delimited-text",
        "text/tab-separated-values": "delimited-text",
        "text/html": "inert-html",
        "application/xhtml+xml": "inert-html",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx-ooxml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx-ooxml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx-ooxml",
        "application/pdf": "pdfium-text-and-raster",
        "image/png": "pillow-image-ocr",
        "image/jpeg": "pillow-image-ocr",
        "image/tiff": "pillow-image-ocr",
        "image/heic": "pillow-image-ocr",
        "image/heif": "pillow-image-ocr",
        "image/webp": "pillow-image-ocr",
        "application/rtf": "pandoc-rtf-json",
        "text/rtf": "pandoc-rtf-json",
    }.get(media_type, "unsupported")
    settings = settings or resolve_settings(
        media_type=media_type, profile_ref=profile_ref
    )
    is_pdf = media_type == "application/pdf"
    is_image = media_type in OCR_MEDIA_TYPES and not is_pdf
    uses_ocr = settings.runs_ocr
    if is_pdf:
        decoder = PDF_DECODER_BUILD
    elif is_image:
        decoder = IMAGE_DECODER_BUILD
    else:
        decoder = f"{adapter}-{_ADAPTER_VERSION}"
    return {
        "schema": store.PROFILE_SCHEMA,
        "adapter": adapter,
        "adapterVersion": _ADAPTER_VERSION,
        # The caller's trusted profile reference plus the exact decoder build and
        # native binary identity the result was produced by.
        "decoderBuild": f"{profile_ref}:{revision}:{decoder}",
        "processingMode": settings.processing_mode,
        "ocr": (
            settings.ocr.to_profile()
            if settings.ocr is not None
            else {
                "engine": "none",
                "engineRevision": "none",
                "modelHashes": [],
                "languages": [],
            }
        ),
        "embeddedImageOcr": settings.embedded_image_ocr,
        "revisionView": "current_text_with_separate_annotations",
        "secondaryContentPolicy": "preserve_role_and_visibility",
        "formulaPolicy": "expression_and_saved_cache_without_calculation",
        "html": {"encoding": "utf-8"},
        "delimited": {
            "encoding": "bom_or_utf-8",
            "delimiter": "media_default",
            "quote": '"',
        },
        "limits": _profile_limits(
            media_type=media_type, is_pdf=is_pdf, is_image=is_image, uses_ocr=uses_ocr
        ),
        "readingOrderVersion": (
            READING_ORDER_VERSION if (is_pdf or is_image) else "source-order-1"
        ),
        "deduplicationVersion": DEDUPLICATION_VERSION if uses_ocr else "none-1",
        "parserFeatures": sorted(
            {
                "direct_text",
                "inert_references",
                "located_segments",
                *(["page_geometry", "pdf_raster"] if is_pdf else []),
                *(["frame_orientation", "frame_inventory"] if is_image else []),
                *(
                    [
                        "local_ocr",
                        "raw_confidence_observations",
                        "spatial_deduplication",
                    ]
                    if uses_ocr
                    else []
                ),
            }
        ),
        "unknownFeaturePolicy": "partial",
        "completenessPolicy": "explicit_unit_coverage",
    }


@dataclass(frozen=True, slots=True)
class ReprocessProfileOption:
    """One approved profile selection, resolved exactly once.

    `settings` and `profile` are the *same* objects the reprocess handler executes and
    records, not a second resolution of the same reference: the digest a caller is offered
    is therefore the digest of the settings that ran, and a text-only selection cannot be
    reported as text-only and then executed under the default. The digest is not a promise
    about resources -- whether the pinned OCR models were served is recorded in the profile
    itself and reported in the extraction's own outcome.
    """

    profile_ref: str
    revision: str
    processing_mode: str
    profile_digest: str
    settings: ExtractionSettings
    profile: Mapping[str, Any]


def approved_reprocess_profiles(
    *,
    media_type: str,
    settings: ProcessingSettings = DEFAULT_SETTINGS,
    catalogue_for: CatalogueFor | None = None,
) -> tuple[ReprocessProfileOption, ...]:
    """The profile selections Core will accept for a reprocess of this media type.

    A reprocess is an explicit new decision, so it resolves against the *current*
    workspace preferences rather than against the frozen snapshot the original capture
    executed under -- which is why `settings` is a parameter here and the capture row is
    the authority in :func:`process_capture_outbox`. The caller reads the preferences once
    and passes them in, so the options it offers and the option it later executes are one
    resolution. `settings` defaults to the product defaults, which is what an unconfigured
    workspace stores.

    One option for an ordinary document, because Text-only and the default resolve to the
    same processing for it and offering two identical choices would be a false choice. Two
    where the default would actually recognise something -- a PDF or image, or a source
    carrying embedded images the preference applies to -- which is the product's stated
    pair: Text and local OCR as the default, Text only as the explicit alternate. A
    workspace already set to Text-only therefore offers one option, because under those
    preferences the explicit alternate *is* the default. An unqualified format has no
    option at all rather than one that would be refused.

    This is the single place a reprocess profile is resolved. `document.history` reports what
    it returns and `document.reprocess` executes what it returns, so neither can name a
    selection the other would resolve differently.
    """
    references = (
        (DEFAULT_PROFILE_REF, TEXT_ONLY_PROFILE_REF)
        if settings.recognises(media_type)
        else (DEFAULT_PROFILE_REF,)
    )
    options: list[ReprocessProfileOption] = []
    for reference in references:
        resolved = resolve_settings(
            media_type=media_type,
            profile_ref=reference,
            settings=settings,
            catalogue_for=catalogue_for,
        )
        profile = portable_profile(
            media_type=media_type,
            profile_ref=reference,
            revision=APPROVED_PROFILE_REVISION,
            settings=resolved,
        )
        if profile["adapter"] == "unsupported":
            return ()
        options.append(
            ReprocessProfileOption(
                profile_ref=reference,
                revision=APPROVED_PROFILE_REVISION,
                processing_mode=str(profile["processingMode"]),
                profile_digest=store.profile_digest(profile),
                settings=resolved,
                profile=profile,
            )
        )
    return tuple(options)


def _profile_limits(
    *, media_type: str, is_pdf: bool, is_image: bool, uses_ocr: bool
) -> dict[str, int]:
    """The ceilings this profile was produced under, as actually enforced."""
    if media_type in {"text/plain", "text/markdown"}:
        source_bytes = 1024 * 1024
    elif media_type in HTML_MEDIA_TYPES:
        # Published from the constant the markup adapter enforces, so the two
        # cannot drift into a profile that claims a bound nothing applies.
        source_bytes = MAX_HTML_SOURCE_BYTES
    elif media_type in {"application/rtf", "text/rtf"}:
        source_bytes = 20 * 1024 * 1024
    else:
        source_bytes = MAX_SOURCE_BYTES
    if is_image:
        decoded_pixels = MAX_FRAME_PIXELS
    elif is_pdf:
        decoded_pixels = MAX_WORKING_PIXELS
    else:
        decoded_pixels = 1
    return {
        "sourceBytes": source_bytes,
        "decodedPixels": decoded_pixels,
        "containerEntries": 10_000,
        "expandedBytes": 512 * 1024 * 1024,
        "outputTextBytes": RASTER_TEXT_BYTES,
        "outputStructuredBytes": 128 * 1024 * 1024,
        "memoryBytes": 1024 * 1024 * 1024,
        "timeMs": (OCR_SECONDS_PER_DOCUMENT if uses_ocr else RASTER_DECODE_SECONDS)
        * 1000,
        "temporaryBytes": 2 * 1024 * 1024 * 1024,
    }


def process_capture_outbox(
    runner: ServiceRunner,
    *,
    limit: int = 16,
    catalogue_for: CatalogueSource | None = None,
) -> list[store.StoredResult]:
    """Process the oldest capture obligations, settling each terminal result.

    Each obligation executes from its own capture row's two frozen snapshots.
    Nothing process-global decides a mode, a language order, an embedded-image
    pass or a model revision, so a preference changed, a language installed or
    removed, or a service restarted between admission and this pass reaches
    neither a queued obligation nor an active one: the row is the same row, the
    profile it resolves to is the same profile, and the operation key derived from
    that profile's digest is the same key -- which is what makes the replay after
    a restart idempotent rather than a second extraction under a second identity.

    An obligation that cannot be executed *as the obligation it was admitted as*
    is skipped whole: nothing is resolved, no extraction revision is admitted,
    nothing is settled and no pin is released, so a later pass replays the same
    row under the same key. Three things reach it -- a managed cache that could
    not be read, a durable pin the capture no longer holds, and a frozen decision
    this build resolves to a different profile digest than the admission recorded
    -- and none of them is an outcome: settling on any of them would be a terminal
    answer invented from a fact about the obligation rather than about the
    document. That is the one case in which this returns fewer results than it had
    obligations.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.identity is not None and runner.generation is not None
    with runner.sqlite_gate:
        obligations = store.pending_outbox(
            runner.connection, workspace_id=runner.workspace_id, limit=limit
        )
    completed: list[store.StoredResult] = []
    for obligation in obligations:
        with runner.sqlite_gate:
            capture = store.read_capture(
                runner.connection,
                workspace_id=runner.workspace_id,
                capture_id=obligation.capture_id,
            )
        if capture is None:
            continue
        try:
            completed.append(_process_obligation(runner, capture, catalogue_for))
        except (CaptureNotExecutable, ResourceStateIndeterminate):
            continue
    return completed


def _process_obligation(
    runner: ServiceRunner,
    capture: store.StoredCapture,
    source: CatalogueSource | None,
) -> store.StoredResult:
    """Decide one obligation's resources, execute it, and release what it held.

    The frozen documents are decoded first, because a capture whose decision
    cannot be read has no execution to arrange: it is admitted under the one
    profile that pins no OCR resources and settles as failed, naming which of the
    two documents was unreadable. It is deliberately *not* executed as the legacy
    no-selection case, which would resolve a profile nobody was offered.

    The pin is released once, after the obligation has reached a terminal
    disposition, on *every* path that reaches one -- the undecodable ones included,
    because a capture this build cannot read may well have been pinned by a build
    that could, and settling it without releasing would hold those bytes until the
    next start reclaimed them.

    The decoded row is then checked against the digest its admission recorded,
    before anything is leased: a build that resolves the same frozen decision to
    a different profile pauses here, because the alternative is settling the
    capture under an identity -- and an operation key -- nobody was offered.

    It is released only after :func:`_execute_obligation` has settled the outbox
    row, and therefore never on either path that pauses and never when settling
    itself failed. A release lost to a crash in that window leaves a pin
    :func:`reclaim_capture_pins` takes back, which is the recoverable direction;
    releasing before the settlement would leave the bytes removable while the
    obligation was still unresolved, which is not.
    """
    assert runner.workspace_id is not None
    operation_id = capture_pin(runner.workspace_id, capture.capture_id)
    unexecutable: str | None = None
    settings: ProcessingSettings | None = None
    admitted: AdmittedResources | None = None
    try:
        settings = frozen_settings(capture)
        admitted = frozen_resources(capture)
    except (ProcessingSettingsInvalid, OcrSelectionInvalid):
        # The language order is validated when a settings document is decoded, so
        # a malformed selection arrives as the same unreadable-preferences fact.
        unexecutable = PROCESSING_SETTINGS_INVALID
    except ProcessingResourcesInvalid:
        unexecutable = PROCESSING_RESOURCES_INVALID
    if unexecutable is not None:
        result = _execute_obligation(
            runner,
            capture,
            ExtractionSettings(processing_mode=MODE_TEXT_ONLY),
            unexecutable=unexecutable,
        )
    else:
        _admitted_digest_holds(capture, settings=settings, admitted=admitted)
        with capture_resources(
            capture,
            settings=settings,
            admitted=admitted,
            source=source,
            operation_id=operation_id,
        ) as resources:
            result = _execute_obligation(
                runner,
                capture,
                resolve_settings(
                    media_type=capture.media_type,
                    profile_ref=capture.processing_profile_ref,
                    settings=settings,
                    resources=resources,
                ),
            )
    _release(source, operation_id)
    return result


def _admitted_digest_holds(
    capture: store.StoredCapture,
    *,
    settings: ProcessingSettings | None,
    admitted: AdmittedResources | None,
) -> None:
    """Refuse unless this build still resolves the row to its admitted profile.

    The digest comparison, made before anything is leased and before any
    extraction revision is admitted, so a capture whose profile has moved beneath
    it is paused with its outbox row pending and its pins held rather than settled
    under an identity nobody was offered.

    A ``None`` stored digest is the pre-0072 capture, which recorded none. There
    is nothing to compare it with and nothing to be gained by inventing one: such
    a row executes the deterministic compatibility behaviour
    :func:`capture_resources` documents, exactly as it did before this column
    existed.
    """
    stored = capture.processing_profile_digest
    if stored is None:
        return
    resolved = admitted_profile_digest(
        media_type=capture.media_type,
        profile_ref=capture.processing_profile_ref,
        revision=capture.processing_profile_revision,
        settings=settings,
        admitted=admitted,
    )
    if resolved != stored:
        raise CaptureNotExecutable(
            f"{capture.capture_id} was admitted under the profile {stored} and "
            f"this build resolves its frozen decision to {resolved}"
        )


def _release(source: CatalogueSource | None, operation_id: str) -> None:
    """Drop one settled obligation's durable pin; never fail the work over it.

    The obligation is already terminal when this runs, so a release that cannot
    happen must not become its outcome -- and it does not need to, because a pin
    whose release failed is exactly the pin :func:`reclaim_capture_pins` exists to
    take back on the next start.
    """
    resources = managed_resources(source)
    if resources is None:
        return
    with suppress(OcrResourceLifecycleRefused, OcrResourceStoreError, OSError):
        resources.release(operation_id)


def _execute_obligation(
    runner: ServiceRunner,
    capture: store.StoredCapture,
    settings: ExtractionSettings,
    *,
    unexecutable: str | None = None,
) -> store.StoredResult:
    """Admit, extract and settle one capture obligation under decided settings.

    Called inside whatever hold its resources needed, so every byte the profile it
    settles under was derived from stays held until it returns -- including through
    the engine invocation and the terminal handling that records what the
    invocation produced.

    A completion that succeeded *and* promoted also derives its governed knowledge
    before the outbox row settles; :func:`_derive_knowledge` holds the ordering
    argument and the one reason a derivation never becomes this obligation's answer.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.identity is not None and runner.generation is not None
    profile = portable_profile(
        media_type=capture.media_type,
        profile_ref=capture.processing_profile_ref,
        revision=capture.processing_profile_revision,
        settings=settings,
    )
    with runner.sqlite_gate:
        profile_digest = document_intake.register_profile(runner, profile)
    operation_key = (
        "extract-"
        + hashlib.sha256(
            f"{capture.capture_id}\0{profile_digest}".encode()
        ).hexdigest()
    )
    with runner.sqlite_gate:
        revision = _admitted_revision(
            runner, capture, profile_digest=profile_digest, operation_key=operation_key
        )
        stored = store.read_result(
            runner.connection,
            workspace_id=runner.workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
        )
        if stored is not None:
            # A pass that completed this revision and died before settling. The
            # stored result is the immutable answer: extracting again could only
            # disagree with it (OCR need not repeat itself), and a disagreement is
            # refused by `complete_extraction`, stranding the obligation for good.
            _derive_knowledge(runner, capture, stored)
            _settle(runner, capture)
            return stored
    if unexecutable is not None:
        outcome = _terminal_without_segments("failed", unexecutable)
    else:
        try:
            with runner.sqlite_gate:
                content = document_intake.read_retained_source(
                    runner, capture_id=capture.capture_id
                )
            outcome = extract_document(
                content,
                media_type=capture.media_type,
                capture_id=capture.capture_id,
                settings=settings,
            )
        except DocumentExtractionUnsupported:
            outcome = _terminal_without_segments(
                "unsupported", "format_not_qualified"
            )
        except (DocumentExtractionInvalid, document_intake.DocumentIntakeRefused):
            outcome = _terminal_without_segments("failed", "source_invalid")
        except DocumentWorkerFailed as failure:
            # The worker produced no outcome: it crashed, outran its budget, was
            # stopped by a resource ceiling, could not be confined, or answered
            # off-protocol. None of that is a verdict on the source, so the
            # obligation settles as a failure under the worker's own typed
            # reason and the retained source stays retriable.
            outcome = _terminal_without_segments("failed", failure.reason)
    with runner.sqlite_gate:
        result = document_intake.complete_extraction(
            runner,
            extraction_revision_id=revision.extraction_revision_id,
            outcome=outcome,
        )
        _derive_knowledge(runner, capture, result)
        _settle(runner, capture)
    return result


def _settle(runner: ServiceRunner, capture: store.StoredCapture) -> None:
    """Settle one capture's outbox row; the caller holds ``runner.sqlite_gate``."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.identity is not None and runner.generation is not None
    with fenced_transaction(
        runner.connection,
        runner.identity,
        workspace_id=runner.workspace_id,
        fencing_generation=runner.generation,
    ):
        store.settle_outbox(
            runner.connection,
            workspace_id=runner.workspace_id,
            capture_id=capture.capture_id,
            fencing_generation=runner.generation,
            now_us=time.time_ns() // 1000,
        )


def _admitted_revision(
    runner: ServiceRunner,
    capture: store.StoredCapture,
    *,
    profile_digest: str,
    operation_key: str,
) -> store.StoredRevision:
    """This obligation's extraction revision, admitted only the first time.

    A revision already standing under this operation key is this obligation's own
    and nothing else's: the key is derived from the capture id and the profile
    digest, the revision id is derived from the key, and both are fixed for the life
    of the row. Recognising it *here* is the replay.

    It has to be recognised here rather than left to :func:`admit_extraction`'s own
    replay branch, which compares a request digest that includes the extraction head
    the admission expected to build on. A pass that completed and promoted before it
    crashed has moved that head to this very revision, so re-admitting against the
    head as it now stands hashes to a different request and is refused as a
    different request under the same key -- leaving an obligation that is terminal in
    every respect but its outbox row permanently unprocessable, and taking the whole
    processing pass down with it on every later attempt. That window exists wherever
    completion and settlement are two transactions, which they always were.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    existing = store.read_revision_by_key(
        runner.connection,
        workspace_id=runner.workspace_id,
        operation_key=operation_key,
    )
    if existing is not None:
        return existing
    head = store.read_head(
        runner.connection,
        workspace_id=runner.workspace_id,
        document_id=capture.document_id,
    )
    assert head is not None
    revision, _ = document_intake.admit_extraction(
        runner,
        capture_id=capture.capture_id,
        profile_digest=profile_digest,
        expected_base_extraction_id=head.current_extraction_id,
        operation_key=operation_key,
    )
    return revision


def _derive_knowledge(
    runner: ServiceRunner,
    capture: store.StoredCapture,
    result: store.StoredResult,
) -> None:
    """Turn this extraction's segments into source evidence and proposed candidates.

    **Where it runs, and why there.** After :func:`complete_extraction`, because the
    derivation needs the succeeded result and the extraction head that only a
    promoted completion moves; before :func:`store.settle_outbox`, because that is
    what makes a crash in this window recoverable. The obligation is still pending
    while the derivation runs, so a process that dies here leaves the same row to be
    replayed under the same operation key: the revision is recognised, its stored
    result is reused without extracting again, and every identifier the
    derivation writes comes from (workspace, revision, segment ordinal), so the
    second pass finds its own rows and writes nothing. Deriving after settlement
    would put the only record of the outstanding work in a row that had just been
    removed, and the derivation would be lost with no trace that it was owed.

    The pin is untouched. It is released once the obligation is terminal, which is
    after this returns, and the derivation reads stored segments rather than source
    bytes, so it needs nothing the pin holds.

    **Only a succeeded and promoted result derives.** A failed, unsupported or
    partial extraction, and a succeeded one whose completion was superseded,
    historical, fenced or not published, moves no extraction head and gets no
    candidate -- so a stale or losing result cannot become an active proposal.
    :func:`derive_document_knowledge` re-checks the head itself, which is the gate
    that holds if this one is ever relaxed.

    **A refusal is not this obligation's outcome.** The extraction is already
    terminal and is correct; a refusal says only that an *exact* attribution chain
    could not be written for it, and the fail-closed answer to that is no candidate
    rather than a candidate whose citation is approximate. Raising instead would be
    strictly worse than fail-closed: the obligation would never settle and every
    later pass would re-extract the same capture, forever. Anything that is not a
    refusal -- a lost fence, a SQLite error -- does propagate, leaves the row
    pending, and is replayed.
    """
    if result.result_state != "succeeded":
        return
    if result.publication_disposition != "promoted":
        return
    with suppress(DocumentKnowledgeRefused):
        derive_document_knowledge(
            runner,
            document_id=capture.document_id,
            extraction_revision_id=result.extraction_revision_id,
        )


def _terminal_without_segments(
    state: str, limitation: str
) -> document_intake.ExtractionOutcome:
    return document_intake.ExtractionOutcome(
        state=state,
        coverage={
            "schema": "omnivia.extraction-coverage.v2",
            "units": [
                {
                    "unitType": "document",
                    "expected": 1,
                    "visited": 1,
                    "usefulText": 0,
                    "knownEmpty": 0,
                    "excludedByProfile": 0,
                    "unsupported": int(state == "unsupported"),
                    "failed": int(state == "failed"),
                    "unknownRemaining": 0,
                }
            ],
            "roleCoverage": {},
        },
        limitations={"code": limitation},
        worker_build="core-portable-text-1",
    )
