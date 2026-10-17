from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from _trusted_ocr import (
    MODEL_SUFFIX,
    catalogue_over,
    digest_of,
    host_models,
    host_runtime,
    stand_in_models,
)
from omnivia_core_runtime.distribution.model_manifest_trust import (
    DEV_TEST_ANCHOR_FLAG,
    dev_test_anchor,
    model_manifest_identity,
    sign_model_manifest,
    verify_model_manifest,
)
from omnivia_core_runtime.service import (
    document_intake,
    document_worker,
    processing_settings,
)
from omnivia_core_runtime.service import document_processing as processing
from omnivia_core_runtime.service.document_extractors import (
    DOCX_MEDIA_TYPE,
    XHTML_MEDIA_TYPE,
    DocumentExtractionInvalid,
)
from omnivia_core_runtime.service.document_intake import (
    DocumentIntakeRefused,
    capture_document_bytes,
    owned_workspace,
    read_extraction_projection,
    read_retained_source,
)
from omnivia_core_runtime.service.document_processing import (
    NEEDS_OCR_LANGUAGE,
    PROCESSING_RESOURCES_INVALID,
    PROCESSING_SETTINGS_INVALID,
    TEXT_ONLY_PROFILE_REF,
    DocumentProcessingExecutor,
    portable_profile,
    process_capture_outbox,
    reclaim_capture_pins,
    resolve_settings,
)
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    OCR_RESOURCES_DIR,
    REFUSAL_BUSY,
    REFUSAL_PINNED,
    ManagedOcrResources,
    OcrResourceLifecycleRefused,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
    OcrResourceSetIdentity,
    OcrResourceStoreError,
    OcrResourceStoreState,
)
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_ONLY,
    TESSERACT_REVISION,
    TrustedOcrCatalogue,
)
from omnivia_core_runtime.service.processing_settings import (
    RESOURCES_FROZEN,
    RESOURCES_NONE,
    RESOURCES_UNAVAILABLE,
    AdmittedResources,
    CaptureNotExecutable,
    LanguageStorageFull,
    ManagedSelectionAuthority,
    ProcessingSettings,
    capture_pin,
    declared_installable,
    installation_catalogue,
    installed_identities,
    packaged_identities,
    read_settings,
    write_settings,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.storage import document_intake as store
from PIL import Image, ImageDraw, ImageFont
from test_document_worker_isolation import needs_confinement

pytest_plugins = ("test_document_intake",)


@pytest.fixture(scope="module")
def host_catalogue(
    tmp_path_factory: pytest.TempPathFactory,
) -> TrustedOcrCatalogue:
    """A trusted catalogue over models this test copied in and digested itself.

    The packaged catalogue a production process starts with names no model
    directory, so nothing recognises anything until an installation configures
    one. A test that needs real recognition configures its own, by the same
    explicit directory-plus-verified-digest route and never by an ambient
    ``TESSDATA_PREFIX``.
    """
    directory = host_models(tmp_path_factory.mktemp("tessdata"), "eng", "osd")
    if directory is None:  # pragma: no cover - host dependent
        pytest.skip("no local OCR engine with the English and orientation models")
    return catalogue_over(directory, "eng", "osd")


def verified_catalogue(directory: Path, *names: str) -> TrustedOcrCatalogue:
    """A trusted catalogue whose models are never meant to reach the engine."""
    return catalogue_over(stand_in_models(directory, *names), *names)


def serving(catalogue: TrustedOcrCatalogue) -> Callable[[Sequence[str]], TrustedOcrCatalogue]:
    """An installation that answers every selection from one fixed catalogue.

    The production resolver publishes a directory per selection out of the managed
    cache. A test that is about what the *frozen snapshot* decides, rather than about
    the managed layout, supplies its own trusted directory instead -- the same explicit
    path-plus-digest route, with the selection it is asked for left to the catalogue.
    """
    return lambda _languages: catalogue


def set_preferences(
    runner: ServiceRunner,
    *,
    languages: Sequence[str] | None = None,
    mode: str | None = None,
    embedded_image_ocr: bool | None = None,
) -> None:
    """Store this workspace's document-processing preferences, as the pane would.

    Written straight to the store rather than through the dispatcher, because the
    settings surface refuses a language the managed cache has not installed and these
    tests are about what the *executor* then does with a stored selection.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.identity is not None and runner.generation is not None
    current, revision = read_settings(
        runner.connection, workspace_id=runner.workspace_id
    )
    write_settings(
        runner.connection,
        runner.identity,
        workspace_id=runner.workspace_id,
        fencing_generation=runner.generation,
        settings=current.replacing(
            mode=mode, languages=languages, embedded_image_ocr=embedded_image_ocr
        ),
        expected_revision=revision,
        now_us=1,
    )


def set_languages(runner: ServiceRunner, *languages: str) -> None:
    set_preferences(runner, languages=languages)


def refusing() -> Callable[[Sequence[str]], TrustedOcrCatalogue]:
    """An installation that fails the test if it is asked to resolve anything.

    Text-only does not "resolve resources and ignore them": it must not reach the
    managed cache at all, which is a property only a resolver that refuses can prove.
    """

    def never(languages: Sequence[str]) -> TrustedOcrCatalogue:
        raise AssertionError(f"a text-only capture resolved OCR resources for {languages}")

    return never


def asked(
    catalogue: TrustedOcrCatalogue,
) -> tuple[Callable[[Sequence[str]], TrustedOcrCatalogue], list[tuple[str, ...]]]:
    """A fixed-catalogue resolver and the record of every selection it was asked for."""
    seen: list[tuple[str, ...]] = []

    def resolver(languages: Sequence[str]) -> TrustedOcrCatalogue:
        seen.append(tuple(languages))
        return catalogue

    return resolver, seen


def _with_host_engines(
    authority: ManagedSelectionAuthority | None, tessdata_dir: Path
) -> ManagedSelectionAuthority:
    """The composed installation, with this host's engines as its payload.

    `installation_catalogue` resolves the packaged payload from the installation
    root, and a test's root holds no assembled candidate -- so the authority it
    returns names no engine and nothing can be invoked through it. These tests
    are about what the frozen snapshot decides and what the managed cache holds,
    not about the payload's own verification (which
    `test_packaged_runtime.py` owns), so the host's own absolute binaries stand
    in for the ones a manifest would declare.
    """
    assert authority is not None
    runtime = host_runtime(tessdata_dir)
    if runtime is None:  # pragma: no cover - host dependent
        pytest.skip("no host OCR engine and RTF reader to bind as the payload")
    return replace(authority, runtime=runtime)


def managed_installation(
    root: Path, tmp_path: Path, *languages: str
) -> tuple[ManagedSelectionAuthority, ManagedOcrResources]:
    """A real managed OCR cache with stand-in models, and the authority over it.

    The production join between an installation root and a frozen selection, rather
    than a fixed catalogue: the lease is the managed cache's own, so only a test that
    goes through it can prove what the hold excludes. The model bytes are stand-ins
    -- resource identity, verification and holding are all byte-level, so they
    exercise all three exactly, and nothing here reaches the engine.
    """
    downloads = tmp_path / "ocr-downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)
    for language in languages:
        body = f"model-{language}".encode()
        payload = downloads / f"{language}.bin"
        payload.write_bytes(body)
        resources.install(
            payload,
            OcrResourceSetIdentity(
                language=language,
                engine_version=TESSERACT_REVISION,
                model_version="tessdata_fast-4.1.0",
                byte_size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            ),
        )
    authority = _with_host_engines(installation_catalogue(root), downloads)
    return authority, resources


def managed_host_installation(
    root: Path, tmp_path: Path, catalogue: TrustedOcrCatalogue, *languages: str
) -> tuple[ManagedSelectionAuthority, ManagedOcrResources]:
    """A managed cache holding this host's own real models, by their real identity.

    The one shape in which recognition can actually run now that execution serves
    only what admission pinned: the bytes the engine opens have to be reachable
    through the managed cache, so a test that wants real text recognised installs
    the host's models into one instead of handing over a directory.
    """
    assert catalogue.tessdata_dir is not None
    source = Path(catalogue.tessdata_dir)
    downloads = tmp_path / "host-downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)
    for language in languages:
        body = (source / f"{language}.traineddata").read_bytes()
        payload = downloads / f"{language}.bin"
        payload.write_bytes(body)
        resources.install(
            payload,
            OcrResourceSetIdentity(
                language=language,
                engine_version=TESSERACT_REVISION,
                model_version="tessdata_fast-4.1.0",
                byte_size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            ),
        )
    authority = _with_host_engines(installation_catalogue(root), source)
    return authority, resources


def _payload_for(tmp_path: Path, language: str) -> Path:
    """The stand-in payload `managed_installation` installs one language from."""
    return tmp_path / "ocr-downloads" / f"{language}.bin"


def pinned_hashes(resources: ManagedOcrResources, *languages: str) -> list[str]:
    """The model hashes a profile pins for one managed selection."""
    present = installed_identities(resources)
    return sorted({f"sha256:{present[language].sha256}" for language in languages})


def frozen(runner: ServiceRunner, capture_id: str) -> AdmittedResources:
    """The resource snapshot one capture row recorded at admission."""
    assert runner.connection is not None and runner.workspace_id is not None
    capture = store.read_capture(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    assert capture is not None and capture.processing_resources_json is not None
    return AdmittedResources.from_value(json.loads(capture.processing_resources_json))


def executed_profile(runner: ServiceRunner, result: store.StoredResult) -> dict[str, Any]:
    assert runner.connection is not None and runner.workspace_id is not None
    profile = store.read_profile(
        runner.connection,
        workspace_id=runner.workspace_id,
        digest=_profile_digest(runner, result),
    )
    assert profile is not None
    return dict(profile)


@needs_confinement
def test_pending_text_capture_becomes_a_searchable_projection(
    runner: ServiceRunner,
) -> None:
    capture = capture_document_bytes(
        runner,
        content=b"One searchable sentence.\r\nSecond line.",
        document_id="doc-processing-1",
        expected_base_capture_id=None,
        operation_key="capture-processing-1",
        media_type="text/plain",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = process_capture_outbox(runner)

    assert [result.result_state for result in results] == ["succeeded"]
    assert results[0].publication_disposition == "promoted"
    projected = read_extraction_projection(
        runner, document_id="doc-processing-1", query="Second"
    )
    assert [segment.text for segment in projected] == [
        "One searchable sentence.\r\nSecond line."
    ]
    assert runner.connection is not None and runner.workspace_id is not None
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id,
        capture_id=capture.capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_SETTLED


def test_corrupt_pdf_is_preserved_and_settled_without_projection(
    runner: ServiceRunner,
) -> None:
    capture = capture_document_bytes(
        runner,
        content=b"%PDF-1.7\n",
        document_id="doc-processing-pdf",
        expected_base_capture_id=None,
        operation_key="capture-processing-pdf",
        media_type="application/pdf",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = process_capture_outbox(runner)

    assert [result.result_state for result in results] == ["failed"]
    assert read_extraction_projection(runner, document_id="doc-processing-pdf") == []
    assert runner.connection is not None and runner.workspace_id is not None
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id,
        capture_id=capture.capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_SETTLED


@pytest.mark.parametrize(
    "reason",
    [
        document_worker.REASON_ISOLATION,
        document_worker.REASON_TIMEOUT,
        document_worker.REASON_CRASHED,
        document_worker.REASON_RESOURCE_LIMIT,
    ],
)
def test_a_worker_that_produced_no_outcome_settles_under_its_own_reason(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """DOC-16. Extraction runs in a confined child, and a child that produced no
    outcome -- it could not be sealed, outran its budget, crashed, or was stopped
    by a resource ceiling -- is not a verdict on the document.

    So the obligation settles `failed` under the worker's own typed reason, never
    under `source_invalid`, and the retained bytes are untouched: the same capture
    can be reprocessed once the condition is gone.
    """
    capture = capture_document_bytes(
        runner,
        content=b"bytes the worker never got to.",
        document_id="doc-worker-failed",
        expected_base_capture_id=None,
        operation_key="capture-worker-failed",
        media_type="text/plain",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    def refuse(*_: Any, **__: Any) -> Any:
        raise document_worker.DocumentWorkerFailed(reason, "detail")

    monkeypatch.setattr(processing, "extract_document", refuse)

    results = process_capture_outbox(runner)

    assert [result.result_state for result in results] == ["failed"]
    assert json.loads(results[0].limitations_json)["code"] == reason
    assert read_extraction_projection(runner, document_id="doc-worker-failed") == []
    # Settled, not stranded, and the immutable source is still there to retry.
    assert runner.connection is not None and runner.workspace_id is not None
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id,
        capture_id=capture.capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_SETTLED
    assert read_retained_source(runner, capture_id=capture.capture.capture_id) == (
        b"bytes the worker never got to."
    )


@pytest.mark.production_document_worker
def test_the_production_extraction_seam_is_the_confined_worker() -> None:
    # DOC-16. The in-process adapters are not a production route: the name this
    # module's extraction goes through is the isolated worker's entry point.
    assert processing.extract_document is document_worker.extract_document_isolated


@needs_confinement
def test_service_executor_processes_a_bounded_capture_pass(
    runner: ServiceRunner,
) -> None:
    capture_document_bytes(
        runner,
        content=b"Processed by the service-owned executor.",
        document_id="doc-processing-executor",
        expected_base_capture_id=None,
        operation_key="capture-processing-executor",
        media_type="text/plain",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-executor", query="service-owned"
    )
    assert [segment.text for segment in projected] == [
        "Processed by the service-owned executor."
    ]


@needs_confinement
def test_service_executor_processes_a_retained_docx_package(
    runner: ServiceRunner,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>DOCX in Core</w:t></w:r></w:p></w:body></w:document>',
        )
    capture_document_bytes(
        runner,
        content=buffer.getvalue(),
        document_id="doc-processing-docx",
        expected_base_capture_id=None,
        operation_key="capture-processing-docx",
        media_type=DOCX_MEDIA_TYPE,
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-docx", query="DOCX"
    )
    assert [segment.text for segment in projected] == ["DOCX in Core"]


@needs_confinement
def test_service_executor_publishes_local_image_ocr(
    runner: ServiceRunner, tmp_path: Path, host_catalogue: TrustedOcrCatalogue
) -> None:
    """Real recognition, end to end through the identities admission pinned.

    The host's own models are installed into a managed cache, so the directory the
    engine is pointed at is the projection of the exact resource identities this
    capture froze -- not a directory a test handed the worker.
    """
    authority, resources = managed_host_installation(
        tmp_path / "inst-image", tmp_path, host_catalogue, "eng", "osd"
    )
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 55),
        "Captured OCR",
        fill="black",
        font=ImageFont.load_default(size=92),
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    capture = admit(
        runner,
        document_id="doc-processing-image",
        content=encoded.getvalue(),
        media_type="image/png",
        resources=resources,
    )

    results = DocumentProcessingExecutor(
        runner, catalogue_for=authority
    ).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-image", query="Captured"
    )
    assert "Captured OCR" in " ".join(segment.text or "" for segment in projected)
    # The hashes the profile pins are the identities frozen at admission, and the
    # orientation data the engine loads alongside them.
    snapshot = frozen(runner, capture.capture_id)
    assert snapshot.languages == ("eng",)
    assert executed_profile(runner, results[0])["ocr"]["modelHashes"] == sorted(
        {f"sha256:{identity.sha256}" for identity in snapshot.selection}
    )


@needs_confinement
def test_a_missing_ocr_language_settles_as_needs_ocr_language(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """DOC-14: the requested profile survives, and recovery is installing it.

    The installation here has a trusted model directory with verified English
    and orientation models, and the workspace's *stored* selection asks for one it
    does not hold. Nothing is downloaded and English is not substituted -- and the
    selection reaches the worker through the capture row rather than through an
    argument, which is the only route production has.
    """
    catalogue = verified_catalogue(tmp_path, "eng", "osd")
    set_languages(runner, "jpn")
    image = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(image).text(
        (20, 20), "Ignored", fill="black", font=ImageFont.load_default(size=60)
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    capture_document_bytes(
        runner,
        content=encoded.getvalue(),
        document_id="doc-processing-language",
        expected_base_capture_id=None,
        operation_key="capture-processing-language",
        media_type="image/png",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = process_capture_outbox(runner, catalogue_for=serving(catalogue))

    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == NEEDS_OCR_LANGUAGE
    assert limitations["missingLanguages"] == ["jpn"]
    assert limitations["recovery"] == "install_language"
    # No silent English substitution and no download: nothing was recognised.
    assert (
        read_extraction_projection(runner, document_id="doc-processing-language") == []
    )
    profile = store.read_profile(
        runner.connection,
        workspace_id=runner.workspace_id,
        digest=_profile_digest(runner, results[0]),
    )
    assert profile is not None
    # The requested profile keeps its identity: the mode that was selected and
    # the language that was asked for, pinning no model it never read.
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["languages"] == ["jpn"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"


def _profile_digest(runner: ServiceRunner, result: store.StoredResult) -> str:
    assert runner.connection is not None and runner.workspace_id is not None
    revision = store.read_revision(
        runner.connection,
        workspace_id=runner.workspace_id,
        extraction_revision_id=result.extraction_revision_id,
    )
    assert revision is not None
    return revision.profile_digest


def test_an_explicit_text_only_profile_reference_pins_no_ocr_resources(
    tmp_path: Path,
) -> None:
    profile = portable_profile(
        media_type="application/pdf",
        profile_ref=TEXT_ONLY_PROFILE_REF,
        revision="1",
    )

    assert profile["processingMode"] == "text_only"
    assert profile["ocr"] == {
        "engine": "none",
        "engineRevision": "none",
        "modelHashes": [],
        "languages": [],
    }
    assert profile["deduplicationVersion"] == "none-1"
    assert "local_ocr" not in profile["parserFeatures"]
    # The default reference keeps Text and local OCR for the same media type,
    # and an installation with no trusted model directory fails closed under it:
    # no recognition, no claimed model hash, and the state said out loud.
    unconfigured = portable_profile(
        media_type="application/pdf", profile_ref="default", revision="1"
    )
    assert unconfigured["processingMode"] == "text_and_local_ocr"
    assert unconfigured["ocr"]["languages"] == ["eng"]
    assert unconfigured["ocr"]["modelHashes"] == []
    assert unconfigured["ocr"]["resourceState"] == "unavailable"
    assert "local_ocr" not in unconfigured["parserFeatures"]
    # Configure that directory with verified digests and the same reference pins
    # the two models it actually read: English and the orientation data.
    catalogue = verified_catalogue(tmp_path, "eng", "osd")
    configured = portable_profile(
        media_type="application/pdf",
        profile_ref="default",
        revision="1",
        settings=resolve_settings(
            media_type="application/pdf",
            profile_ref="default",
            catalogue_for=serving(catalogue),
        ),
    )
    assert configured["ocr"]["languages"] == ["eng"]
    assert configured["ocr"]["modelHashes"] == sorted(
        f"sha256:{digest}" for digest in catalogue.installed.values()
    )
    assert "resourceState" not in configured["ocr"]
    assert "local_ocr" in configured["parserFeatures"]
    assert "pdfium-" in configured["decoderBuild"]


def test_an_ordered_language_selection_reaches_the_immutable_profile(
    tmp_path: Path,
) -> None:
    catalogue = verified_catalogue(tmp_path, "eng", "deu", "osd")

    profile = portable_profile(
        media_type="application/pdf",
        profile_ref="default",
        revision="1",
        settings=resolve_settings(
            media_type="application/pdf",
            profile_ref="default",
            settings=ProcessingSettings(languages=("deu", "eng")),
            catalogue_for=serving(catalogue),
        ),
    )

    assert profile["ocr"]["languages"] == ["deu", "eng"]
    # The digests pinned are the ones taken over the files just verified, with
    # the orientation data the profile always loads alongside them.
    assert profile["ocr"]["modelHashes"] == sorted(
        f"sha256:{digest}" for digest in catalogue.installed.values()
    )


# --- the frozen snapshot is what executes ---------------------------------------


def image_bytes(text: str = "Ignored") -> bytes:
    image = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(image).text(
        (20, 20), text, fill="black", font=ImageFont.load_default(size=60)
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


def admit(
    runner: ServiceRunner,
    *,
    document_id: str,
    content: bytes,
    media_type: str,
    resources: ManagedOcrResources | None = None,
    packaged: Mapping[str, OcrResourceSetIdentity] = {},
    installable: Mapping[str, OcrResourceSetIdentity] = {},
) -> store.StoredCapture:
    """One admitted capture, frozen and pinned against `resources`.

    `resources` is the installation's managed cache, and passing it is what makes
    the admission able to freeze exact identities: without one there is nothing
    installed to freeze, which the capture records as the condition it is.

    `packaged` is the installation's *bundled* set, which the admission asks
    first. A selection it covers whole is frozen against those identities and
    pinned nowhere, because the packaged bytes are not the managed cache's to
    evict -- so a test about that half passes it and leaves `resources` empty.

    `installable` is the resource owner's declared catalogue of sets that are not
    installed, which is what gives §18.4's capacity reservation a size. Left empty
    by every test about an installed selection, because such an admission needs no
    set installing and asks no capacity question at all.
    """
    captured = capture_document_bytes(
        runner,
        content=content,
        document_id=document_id,
        expected_base_capture_id=None,
        operation_key=f"capture-{document_id}",
        media_type=media_type,
        intent_kind=store.INTENT_NEW_CAPTURE,
        ocr_resources=resources,
        packaged_models=packaged,
        installable_models=installable,
    )
    return captured.capture


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(("deu", "eng"), id="two"),
        pytest.param(("fra", "deu", "eng"), id="three"),
    ],
)
def test_the_frozen_order_is_the_order_that_executes(
    runner: ServiceRunner, tmp_path: Path, order: tuple[str, ...]
) -> None:
    """The ordered selection reaches the engine and the immutable profile intact.

    Two independent things are asserted together on purpose: the capture froze
    exactly the identities for that order, in that order, and the profile records
    exactly that order with one pinned hash per model it read -- so a profile could
    not claim an order the invocation did not use.
    """
    set_languages(runner, *order)
    authority, resources = managed_installation(
        tmp_path / "inst-order", tmp_path, *order, "osd"
    )
    capture = admit(
        runner,
        document_id="doc-order",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )

    results = process_capture_outbox(runner, catalogue_for=authority)

    # Frozen at admission, in the requested order, with the orientation data last.
    assert frozen(runner, capture.capture_id).languages == order
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["languages"] == list(order)
    assert profile["ocr"]["modelHashes"] == pinned_hashes(
        resources, *order, "osd"
    )
    assert "resourceState" not in profile["ocr"]


def test_a_later_settings_change_does_not_reach_a_queued_capture(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The race the whole snapshot exists for.

    The capture is admitted under one selection and the preference is then changed
    while its obligation is still queued. A late global read would run the new
    selection -- and because the operation key is derived from the resolved profile's
    digest, it would settle the capture under a profile identity nobody was offered.
    """
    set_languages(runner, "deu")
    authority, resources = managed_installation(
        tmp_path / "inst-race", tmp_path, "deu", "fra", "osd"
    )
    capture = admit(
        runner,
        document_id="doc-race",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    # The preference moves after admission and before the bounded pass runs.
    set_languages(runner, "fra")

    results = process_capture_outbox(runner, catalogue_for=authority)

    # Both halves are the admitted ones: the language *and* the exact identity.
    assert frozen(runner, capture.capture_id).languages == ("deu",)
    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["languages"] == ["deu"]
    assert profile["ocr"]["modelHashes"] == pinned_hashes(resources, "deu", "osd")
    # And the live preference really did move, so this is about the capture rather
    # than about a change that silently did nothing.
    assert runner.connection is not None and runner.workspace_id is not None
    stored, _revision = read_settings(
        runner.connection, workspace_id=runner.workspace_id
    )
    assert stored.languages == ("fra",)


def test_a_mode_change_does_not_reach_a_queued_capture(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The same race over the mode: a capture admitted for recognition keeps it.

    Asserted the strict way round -- the capture was admitted under Text and local
    OCR and the workspace has since chosen Text-only, so a late read would downgrade
    an obligation the user had already asked to recognise.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-race-mode", tmp_path, "eng", "osd"
    )
    capture = admit(
        runner,
        document_id="doc-race-mode",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    set_preferences(runner, mode="text_only")

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert frozen(runner, capture.capture_id).languages == ("eng",)
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["modelHashes"] == pinned_hashes(resources, "eng", "osd")


def test_a_restart_replays_the_same_frozen_work_under_the_same_key(
    paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """A real restart: the admitting process stops and another one picks the work up.

    Nothing in memory survives, so if the preference were read at execution the new
    process would read the *changed* one. It reads the capture row instead, which is
    why the profile, the extraction revision and the operation key the replay settles
    under are the ones the first process's admission determined.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-restart", tmp_path, "deu", "eng", "osd"
    )
    installed = installed_identities(resources)
    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as first:
        set_languages(first, "deu")
        capture = admit(
            first,
            document_id="doc-restart",
            content=image_bytes(),
            media_type="image/png",
            resources=resources,
        )
        # Changed by the same process that admitted, after admission; the restart is
        # what proves no in-memory value carried the old one forward.
        set_languages(first, "eng")

    # The pin outlived the process that took it, which is the half a lease cannot
    # do: the obligation is still unresolved, so the bytes are still not removable.
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        resources.remove(installed["deu"])
    assert refused.value.code == REFUSAL_PINNED

    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as second:
        results = DocumentProcessingExecutor(
            second, catalogue_for=authority
        ).run_pending(budget=4, force=True)

        assert frozen(second, capture.capture_id).languages == ("deu",)
        # Whether recognition then succeeds is this host's business -- these are
        # stand-in model bytes, so it does not. What the restart decides is which
        # profile the obligation settles under, and that is the admitted one.
        assert len(results) == 1
        profile = executed_profile(second, results[0])
        assert profile["ocr"]["languages"] == ["deu"]
        assert profile["ocr"]["modelHashes"] == pinned_hashes(resources, "deu", "osd")
        settled = results[0].extraction_revision_id
        # A second pass after the restart adds nothing: the obligation is settled and
        # the capture is not processed again under a second identity.
        assert DocumentProcessingExecutor(second, catalogue_for=authority).run_pending(
            budget=4, force=True
        ) == []
        assert runner_revisions(second, "doc-restart") == [settled]
    # And settled means released: the operator's removal is accepted now.
    resources.remove(installed["deu"])


def runner_revisions(runner: ServiceRunner, document_id: str) -> list[str]:
    assert runner.connection is not None and runner.workspace_id is not None
    return [
        row.revision.extraction_revision_id
        for row in store.read_extraction_history(
            runner.connection,
            workspace_id=runner.workspace_id,
            document_id=document_id,
            offset=0,
            limit=10,
        )
    ]


# --- text-only never resolves OCR resources -------------------------------------


@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        pytest.param("image/png", image_bytes(), id="image"),
        pytest.param("application/pdf", b"%PDF-1.7\n", id="pdf"),
    ],
)
def test_a_text_only_capture_resolves_no_ocr_resource(
    runner: ServiceRunner, media_type: str, content: bytes
) -> None:
    """Not resolved-and-ignored: the managed cache is never consulted at all, so a
    text-only capture takes no coordination lock and pins no model.
    """
    set_preferences(runner, mode="text_only")
    admit(
        runner,
        document_id=f"doc-text-only-{media_type.replace('/', '-')}",
        content=content,
        media_type=media_type,
    )

    results = process_capture_outbox(runner, catalogue_for=refusing())

    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_only"
    assert profile["ocr"] == {
        "engine": "none",
        "engineRevision": "none",
        "modelHashes": [],
        "languages": [],
    }
    assert profile["embeddedImageOcr"] is False
    assert "local_ocr" not in profile["parserFeatures"]


def test_a_non_image_source_resolves_nothing_unless_its_images_are_examined(
    runner: ServiceRunner,
) -> None:
    """The embedded-image preference is media-qualified at execution.

    Plain text carries no images, so turning the preference on cannot make a
    recognition pass run for it and the profile must not claim one. The same
    preference over a source that does carry images reaches the profile, and only
    then is the cache asked for the selection.
    """
    set_preferences(runner, embedded_image_ocr=True)
    admit(
        runner,
        document_id="doc-embedded-text",
        content=b"No images in here at all.",
        media_type="text/plain",
    )

    results = process_capture_outbox(runner, catalogue_for=refusing())

    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_only"
    assert profile["embeddedImageOcr"] is False


DOCX_DRAWING_BODY: Final = (
    '<w:document xmlns:w="urn:w"'
    ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    "<w:body><w:p><w:r><w:t>Carrier</w:t></w:r></w:p>"
    "<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic>"
    '<pic:blipFill><a:blip r:embed="rId9"/></pic:blipFill>'
    "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    "</w:body></w:document>"
)


def docx_carrying_image(label: str) -> bytes:
    """A DOCX whose only picture is a real PNG holding legible text."""
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 55), label, fill="black", font=ImageFont.load_default(size=92)
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="png" ContentType="image/png"/></Types>',
        )
        archive.writestr("word/document.xml", DOCX_DRAWING_BODY)
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships><Relationship Id="rId9" Target="media/image1.png" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"'
            "/></Relationships>",
        )
        archive.writestr("word/media/image1.png", encoded.getvalue())
    return buffer.getvalue()


@needs_confinement
def test_embedded_image_recognition_reaches_a_source_that_carries_images(
    runner: ServiceRunner, tmp_path: Path, host_catalogue: TrustedOcrCatalogue
) -> None:
    """Real recognition of a raster embedded in a Word source, end to end.

    The preference qualifies the media type, the admission freezes the identities
    for it, and the executor then hands those frozen settings to the Word path --
    which opens the picture's own image part and runs the engine the profile
    pinned over it. What is asserted is the recognised text in the projection
    beside the document's own prose, so the profile metadata can no longer be the
    whole of it.
    """
    set_preferences(runner, embedded_image_ocr=True)
    authority, resources = managed_host_installation(
        tmp_path / "inst-embedded", tmp_path, host_catalogue, "eng", "osd"
    )
    capture = admit(
        runner,
        document_id="doc-embedded-docx",
        content=docx_carrying_image("Embedded OCR"),
        media_type=DOCX_MEDIA_TYPE,
        resources=resources,
    )

    results = process_capture_outbox(runner, catalogue_for=authority)

    # A source that carries images qualifies, so the admission pinned for it.
    assert frozen(runner, capture.capture_id).languages == ("eng",)
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["embeddedImageOcr"] is True
    assert profile["ocr"]["languages"] == ["eng"]
    # The picture's bytes reached the engine: the reading is in the projection,
    # located at the drawing that showed it rather than merged into the prose.
    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-embedded-docx", query="Embedded"
    )
    assert "Embedded OCR" in " ".join(segment.text or "" for segment in projected)
    recognised = [
        segment for segment in projected if segment.extraction_method == "ocr"
    ]
    assert recognised and all(
        json.loads(segment.source_locator_json)["imagePart"] == "word/media/image1.png"
        for segment in recognised
    )


def legible_png(label: str) -> bytes:
    """A raster holding text a real engine reads, as the fixtures above build one."""
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 55), label, fill="black", font=ImageFont.load_default(size=92)
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


@pytest.mark.parametrize(
    ("media_type", "document_id", "build", "part"),
    [
        pytest.param(
            "application/rtf",
            "doc-embedded-rtf",
            lambda member: b"{\\rtf1\\ansi Carrier\\par {\\pict\\pngblip\\picw1000"
            b"\\pich240 " + member.hex().encode("ascii") + b"}}",
            "pict:",
            id="rtf",
        ),
        pytest.param(
            "text/html",
            "doc-embedded-html",
            lambda member: b"<p>Carrier</p><img alt=\"Scan\" "
            b'src="data:image/png;base64,' + base64.b64encode(member) + b'">',
            "@src",
            id="html",
        ),
        pytest.param(
            "application/xhtml+xml",
            "doc-embedded-xhtml",
            lambda member: b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<p>Carrier</p>"
            b'<img alt="Scan" src="data:image/png;base64,'
            + base64.b64encode(member)
            + b'"/></body></html>',
            "@src",
            id="xhtml",
        ),
    ],
)
@needs_confinement
def test_embedded_image_recognition_reaches_an_rtf_or_markup_source(
    runner: ServiceRunner,
    tmp_path: Path,
    host_catalogue: TrustedOcrCatalogue,
    media_type: str,
    document_id: str,
    build: Callable[[bytes], bytes],
    part: str,
) -> None:
    """Real recognition of a raster an RTF or a markup source carries, end to end.

    The preference qualifies these media types exactly as it qualifies the Office
    ones, the admission freezes the identities for them, and the executor then
    hands those frozen settings to the RTF and markup paths -- which read the one
    payload the source carries inline and run the engine the profile pinned over
    it. What is asserted is the recognised text in the projection beside the
    source's own prose.
    """
    set_preferences(runner, embedded_image_ocr=True)
    authority, resources = managed_host_installation(
        tmp_path / f"inst-{document_id}", tmp_path, host_catalogue, "eng", "osd"
    )
    capture = admit(
        runner,
        document_id=document_id,
        content=build(legible_png("Embedded OCR")),
        media_type=media_type,
        resources=resources,
    )

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert frozen(runner, capture.capture_id).languages == ("eng",)
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["embeddedImageOcr"] is True
    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id=document_id, query="Embedded"
    )
    assert "Embedded OCR" in " ".join(segment.text or "" for segment in projected)
    recognised = [
        segment for segment in projected if segment.extraction_method == "ocr"
    ]
    assert recognised and all(
        part in json.loads(segment.source_locator_json)["imagePart"]
        for segment in recognised
    )


# --- the compatibility path and its boundary ------------------------------------


def legacy_admission(
    monkeypatch: pytest.MonkeyPatch,
    settings: str | None,
    resources: str | None,
    digest: str | None = None,
) -> None:
    """Admit as a build that recorded neither snapshot, or an unreadable one.

    The stored profile digest defaults to ``None`` with them: a build that
    recorded no decision recorded no digest of one either. A caller that freezes
    a resource snapshot must pass one, though -- 0072 pairs the two columns, so
    resources without a digest is not a row the database will hold -- and the
    value may be any well-formed digest, because the unreadable-snapshot path
    settles before the comparison that would read it.
    """
    monkeypatch.setattr(
        document_intake,
        "_admitted_processing",
        lambda _runner, **_kwargs: (settings, resources, digest),
    )


@needs_confinement
def test_a_capture_that_recorded_no_selection_runs_the_behaviour_it_was_admitted_under(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both NULLs: no recorded selection and no recorded resources.

    Deterministic, and in particular *not* resolved against the installation. The
    managed cache here holds German, which is also what the workspace now prefers,
    and neither reaches this capture: it reports `ocr_resources_unconfigured` --
    the installation has no resources it may use, rather than a language the user
    chose being missing -- and it pins no hash. A model installed after such a
    capture was admitted cannot change what it does.
    """
    legacy_admission(monkeypatch, None, None)
    authority, resources = managed_installation(
        tmp_path / "inst-legacy", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    # The workspace does hold a preference, which this capture must not pick up.
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-legacy",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert capture.processing_settings_json is None
    assert capture.processing_resources_json is None

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == "ocr_resources_unconfigured"
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["languages"] == ["eng"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"
    # Nothing was pinned for it either, so the installed German stays removable.
    resources.remove(installed["deu"])


def test_a_malformed_snapshot_is_refused_rather_than_reinterpreted(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot written by a build this one cannot read is not the legacy case.

    Reading it as "no selection" would run the product default under the key of a
    decision nobody can establish. It settles as failed, naming the one code that
    says the preferences themselves could not be read, and no recognition is
    attempted.
    """
    legacy_admission(
        monkeypatch,
        '{"mode": "text_and_future_ocr", "languages": ["eng"], '
        '"embeddedImageOcr": false}',
        None,
    )
    admit(
        runner,
        document_id="doc-malformed",
        content=image_bytes(),
        media_type="image/png",
    )

    results = process_capture_outbox(runner, catalogue_for=refusing())

    assert [result.result_state for result in results] == ["failed"]
    assert json.loads(results[0].limitations_json) == {
        "code": PROCESSING_SETTINGS_INVALID
    }
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_only"
    assert profile["ocr"]["languages"] == []
    assert read_extraction_projection(runner, document_id="doc-malformed") == []


def test_a_malformed_resource_snapshot_is_its_own_terminal_fact(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resource half of the same rule, with its own code.

    A resource snapshot this build cannot read is not the legacy absent one, and
    reading it as absent would resolve -- which is exactly what the capture was
    admitted to avoid. It settles naming the document that could not be read, and
    the installed model is never consulted.

    It is still released, though. A capture this build cannot read may well have
    been pinned by the build that wrote it, and a terminal settlement that skipped
    the release would hold those bytes against the operator until something
    restarted -- so the pin here is the one a readable admission would have left.
    """
    legacy_admission(
        monkeypatch,
        json.dumps(ProcessingSettings(languages=("deu",)).to_value()),
        '{"schema": "omnivia.capture-ocr-resources.v1", "kind": "frozen", '
        '"selection": [], "reason": null, "detail": null, "unavailableModels": []}',
        "sha256:" + "0" * 64,
    )
    authority, resources = managed_installation(
        tmp_path / "inst-bad-resources", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    capture = admit(
        runner,
        document_id="doc-malformed-resources",
        content=image_bytes(),
        media_type="image/png",
    )
    assert runner.workspace_id is not None
    resources.pin(
        capture_pin(runner.workspace_id, capture.capture_id), (installed["deu"],)
    )

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    assert json.loads(results[0].limitations_json) == {
        "code": PROCESSING_RESOURCES_INVALID
    }
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_only"
    assert profile["ocr"]["languages"] == []
    assert (
        read_extraction_projection(runner, document_id="doc-malformed-resources") == []
    )
    assert resources.store.load().pins == ()
    resources.remove(installed["deu"])


@needs_confinement
def test_a_missing_language_is_never_substituted_with_an_installed_one(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """Two installed languages and a third that is not: the whole selection refuses.

    The dangerous substitution is not only English. An installation holding German
    must not quietly recognise a document in German because the Japanese the user
    selected is absent, and it must not drop the absent member and recognise the
    rest either.
    """
    set_languages(runner, "deu", "jpn")
    authority, resources = managed_installation(
        tmp_path / "inst-no-substitute", tmp_path, "deu", "eng", "osd"
    )
    installed = installed_identities(resources)
    capture = admit(
        runner,
        document_id="doc-no-substitute",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    # Nothing partial was pinned either: an obligation that cannot run must not
    # hold the operator's German hostage for the length of the queue.
    snapshot = frozen(runner, capture.capture_id)
    assert snapshot.kind == RESOURCES_UNAVAILABLE
    assert snapshot.unavailable_models == ("jpn",)
    resources.remove(installed["deu"])
    resources.install(_payload_for(tmp_path, "deu"), installed["deu"])

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == NEEDS_OCR_LANGUAGE
    assert limitations["missingLanguages"] == ["jpn"]
    assert limitations["requestedLanguages"] == ["deu", "jpn"]
    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["languages"] == ["deu", "jpn"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"
    assert read_extraction_projection(runner, document_id="doc-no-substitute") == []


@needs_confinement
def test_service_executor_processes_rtf_through_the_retained_copy(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    capture_document_bytes(
        runner,
        content=rb"{\rtf1\ansi\deff0 Core RTF content\par}",
        document_id="doc-processing-rtf",
        expected_base_capture_id=None,
        operation_key="capture-processing-rtf",
        media_type="application/rtf",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    # The pinned RTF reader is the absolute executable the verified payload
    # declares, so the executor is composed with one; without a payload an RTF
    # capture settles as a refusal rather than reaching a host binary by name.
    results = DocumentProcessingExecutor(
        runner, catalogue_for=_with_host_engines(installation_catalogue(tmp_path), tmp_path)
    ).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-rtf", query="RTF"
    )
    assert "Core RTF content" in " ".join(segment.text or "" for segment in projected)


@needs_confinement
def test_service_executor_processes_xhtml_through_the_retained_copy(
    runner: ServiceRunner,
) -> None:
    capture_document_bytes(
        runner,
        content=(
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<h1>Core XHTML heading</h1><p>Core XHTML content</p>"
            b"</body></html>"
        ),
        document_id="doc-processing-xhtml",
        expected_base_capture_id=None,
        operation_key="capture-processing-xhtml",
        media_type=XHTML_MEDIA_TYPE,
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    # The XHTML media type is carried the whole way: a retained source reaches
    # the markup adapter, settles succeeded, and is projected as searchable text.
    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-xhtml", query="XHTML"
    )
    assert [segment.text for segment in projected] == [
        "Core XHTML heading",
        "Core XHTML content",
    ]


# --- what one obligation resolves, it holds -------------------------------------

HTML_SOURCE = b"<html><body><p>Core held selection content</p></body></html>"


def capture_html(
    runner: ServiceRunner,
    *,
    document_id: str,
    resources: ManagedOcrResources | None = None,
) -> store.StoredCapture:
    """One capture whose preferences resolve OCR resources but reach no engine.

    An HTML source with the embedded-image preference on qualifies for recognition,
    so the selection *is* frozen, pinned and held -- and it carries no raster image,
    so the extraction succeeds without invoking Tesseract over stand-in model bytes.
    """
    captured = capture_document_bytes(
        runner,
        content=HTML_SOURCE,
        document_id=document_id,
        expected_base_capture_id=None,
        operation_key=f"capture-{document_id}",
        media_type="text/html",
        intent_kind=store.INTENT_NEW_CAPTURE,
        ocr_resources=resources,
    )
    return captured.capture


def observing(
    monkeypatch: pytest.MonkeyPatch, observe: Callable[[Any], None]
) -> None:
    """Run `observe` inside the extraction, with the settings it is executing."""
    import omnivia_core_runtime.service.document_processing as module

    real = module.extract_document

    def wrapped(content: Any, **kwargs: Any) -> Any:
        observe(kwargs["settings"])
        return real(content, **kwargs)

    monkeypatch.setattr(module, "extract_document", wrapped)


@needs_confinement
def test_the_resolved_selection_is_held_for_the_whole_extraction(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair. The projection used to be resolved and then let go of, so a
    removal, a reconciliation or the cache's own pruning could discard the directory
    between resolving it and invoking the engine over it -- which the module's own
    comments admitted.

    Observed from *inside* the extraction, which is the only place the claim means
    anything: both resolved identities refuse removal, a concurrent reconciliation
    changes nothing, and the directory the invocation was handed is still there with
    both models in it.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-held", tmp_path, "deu", "eng", "osd"
    )
    installed = installed_identities(resources)
    set_preferences(runner, languages=("deu", "eng"), embedded_image_ocr=True)
    capture_html(runner, document_id="doc-held-1", resources=resources)
    observed: list[str] = []

    def probe(settings: Any) -> None:
        assert settings.ocr is not None and settings.ocr.available
        served = Path(settings.ocr.tessdata_dir)
        for language in ("deu", "eng"):
            with pytest.raises(OcrResourceLifecycleRefused) as refused:
                resources.remove(installed[language])
            assert refused.value.code == REFUSAL_PINNED
        assert resources.reconcile().changed is False
        assert sorted(child.name for child in served.iterdir()) == [
            "deu.traineddata",
            "eng.traineddata",
            "osd.traineddata",
        ]
        observed.append(settings.ocr.tessdata_dir)

    observing(monkeypatch, probe)

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["succeeded"]
    assert len(observed) == 1
    # Released only after the extraction was settled, so the hold lasts exactly as
    # long as the work it was taken for -- and no longer.
    assert list(resources.leases_root.iterdir()) == []
    resources.remove(installed["deu"])
    assert "deu" not in installed_identities(resources)


def test_the_hold_is_released_after_a_failed_extraction(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup is the release and not the success: an extraction that failed has
    finished with its resources exactly as one that succeeded has.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-failed", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_preferences(runner, languages=("deu",), embedded_image_ocr=True)
    capture_html(runner, document_id="doc-held-2", resources=resources)

    def failing(settings: Any) -> None:
        assert settings.ocr is not None and settings.ocr.available
        with pytest.raises(OcrResourceLifecycleRefused):
            resources.remove(installed["deu"])
        raise DocumentExtractionInvalid("the extraction failed")

    observing(monkeypatch, failing)

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    assert list(resources.leases_root.iterdir()) == []
    resources.remove(installed["deu"])


@needs_confinement
def test_a_text_only_capture_holds_nothing(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text-only does not "resolve resources and then hold them": it never reaches
    the managed cache, so there is no lease file at any point of the extraction and
    an operator's removal during it is accepted exactly as it would be otherwise.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-text-only", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_preferences(runner, mode=MODE_TEXT_ONLY, languages=("deu",))
    capture_html(runner, document_id="doc-held-3", resources=resources)

    def probe(settings: Any) -> None:
        assert settings.ocr is None
        assert not resources.leases_root.exists()
        resources.remove(installed["deu"])

    observing(monkeypatch, probe)

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["succeeded"]
    assert "deu" not in installed_identities(resources)
    assert not resources.leases_root.exists()


# --- END-02: the exact resources a queued obligation was admitted against -------


def install_revision(
    resources: ManagedOcrResources, tmp_path: Path, language: str, revision: str
) -> OcrResourceSetIdentity:
    """A second model revision of one language, installed beside the first.

    §17.3 keeps retained older managed model versions, so two revisions of one
    language coexist as two identities. That is what makes the substitution END-02
    forbids physically possible, and therefore worth a test.
    """
    body = f"model-{language}-{revision}".encode()
    payload = tmp_path / "ocr-downloads" / f"{language}-{revision}.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(body)
    identity = OcrResourceSetIdentity(
        language=language,
        engine_version=TESSERACT_REVISION,
        model_version=revision,
        byte_size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )
    resources.install(payload, identity)
    return identity


def digest_at_admission(
    runner: ServiceRunner,
    capture: store.StoredCapture,
    authority: ManagedSelectionAuthority,
) -> str:
    """The profile digest this capture's *admission* resolves to, computed now.

    Built from the capture row's own two frozen documents and the installation as
    it stands at the moment of the call, which is what makes it usable as a
    before-and-after: taken before a model is replaced and compared with what the
    worker settles under afterwards, it is the whole of END-02's "the digest
    offered or derived at admission remains unchanged".
    """
    assert runner.workspace_id is not None
    assert capture.processing_settings_json is not None
    settings = ProcessingSettings.from_value(
        json.loads(capture.processing_settings_json)
    )
    with authority.frozen_resources(
        operation_id=capture_pin(runner.workspace_id, capture.capture_id),
        admitted=frozen(runner, capture.capture_id),
        requested=settings.languages,
    ) as resolved:
        return store.profile_digest(
            portable_profile(
                media_type=capture.media_type,
                profile_ref=capture.processing_profile_ref,
                revision=capture.processing_profile_revision,
                settings=resolve_settings(
                    media_type=capture.media_type,
                    profile_ref=capture.processing_profile_ref,
                    settings=settings,
                    resources=resolved,
                ),
            )
        )


def test_a_queued_capture_pins_its_exact_identities_against_removal(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The window the durable pin exists for, and the lease never covered.

    The obligation is merely *queued* -- no extraction has started, no lease
    exists -- and the bytes it was admitted against are already immovable. Both
    members are held, the orientation data included, because the projection the
    invocation will open needs all of them.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-queued", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-queued",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )

    snapshot = frozen(runner, capture.capture_id)
    assert snapshot.kind == RESOURCES_FROZEN
    assert snapshot.selection == (installed["deu"], installed["osd"])
    # Queued, not executing: nothing holds a lease, and the pin is what refuses.
    assert not resources.leases_root.exists()
    for language in ("deu", "osd"):
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            resources.remove(installed[language])
        assert refused.value.code == REFUSAL_PINNED
    # A reconciliation keeps them too, rather than treating them as unreferenced.
    assert resources.reconcile().changed is False
    assert sorted(installed_identities(resources)) == ["deu", "osd"]

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    # Settled, so released: the operator's removal is accepted now.
    resources.remove(installed["deu"])
    assert "deu" not in installed_identities(resources)


def test_a_newer_model_revision_cannot_execute_beneath_an_admitted_capture(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The substitution END-02 exists to make impossible.

    The operator cannot take the admitted revision away while the obligation
    waits, so they install a newer one beside it -- which is exactly the state in
    which a late resolution would pick the newer model up and settle the capture
    under a profile digest nobody was offered. The execution pins the hashes of
    the revision that was admitted and names the newer one nowhere.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-newer", tmp_path, "deu", "osd"
    )
    admitted = installed_identities(resources)["deu"]
    orientation = installed_identities(resources)["osd"]
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-newer",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    expected = digest_at_admission(runner, capture, authority)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        resources.remove(admitted)
    assert refused.value.code == REFUSAL_PINNED
    newer = install_revision(resources, tmp_path, "deu", "tessdata_fast-4.2.0")
    # The newer revision really is what an admission *now* would resolve to, so
    # this is about the frozen capture and not about a change that did nothing.
    assert installed_identities(resources)["deu"] == newer

    results = process_capture_outbox(runner, catalogue_for=authority)

    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["languages"] == ["deu"]
    assert profile["ocr"]["modelHashes"] == sorted(
        {f"sha256:{admitted.sha256}", f"sha256:{orientation.sha256}"}
    )
    assert f"sha256:{newer.sha256}" not in profile["ocr"]["modelHashes"]
    assert f"sha256:{admitted.sha256}" in profile["ocr"]["modelHashes"]
    # And the identity the capture settles under is the admitted one, which is the
    # operation key's input: the digest did not move when the model did.
    assert _profile_digest(runner, results[0]) == expected


def test_a_settings_change_and_a_model_change_leave_the_admitted_digest_alone(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """Both halves of the identity at once, which is the whole of END-02's rule.

    The preference moves, a newer model revision is installed, and the digest the
    obligation settles under is the one its own admission resolved -- so the
    operation key derived from it is the key the replay after a restart uses, and
    the capture cannot settle twice under two identities.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-both", tmp_path, "deu", "fra", "osd"
    )
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-both",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    expected = digest_at_admission(runner, capture, authority)

    set_preferences(runner, languages=("fra",), embedded_image_ocr=True)
    install_revision(resources, tmp_path, "deu", "tessdata_fast-4.2.0")

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert _profile_digest(runner, results[0]) == expected
    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["languages"] == ["deu"]
    assert profile["embeddedImageOcr"] is False


@needs_confinement
def test_frozen_bytes_that_went_missing_are_a_condition_not_a_substitution(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """However the admitted identity came to be gone, nothing is served instead.

    A newer revision of the same language is installed and the capture still
    refuses: the condition names the language to install, pins no hash and says
    the resources were not served, and nothing is recognised.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-missing", tmp_path, "deu", "osd"
    )
    admitted = installed_identities(resources)["deu"]
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-missing",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    # The pin is the only thing standing between the operator and this removal,
    # so losing it -- an abandoned reclamation, an edited state document -- is the
    # one way this state is reachable at all.
    assert runner.workspace_id is not None
    resources.release(capture_pin(runner.workspace_id, capture.capture_id))
    resources.remove(admitted)
    newer = install_revision(resources, tmp_path, "deu", "tessdata_fast-4.2.0")

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == NEEDS_OCR_LANGUAGE
    assert limitations["missingLanguages"] == ["deu"]
    assert limitations["recovery"] == "install_language"
    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["languages"] == ["deu"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"
    assert f"sha256:{newer.sha256}" not in profile["ocr"]["modelHashes"]
    assert read_extraction_projection(runner, document_id="doc-missing") == []


@needs_confinement
def test_frozen_bytes_that_no_longer_verify_are_reported_as_unverified(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """Corrupt rather than absent, and a different condition for a different repair.

    The replacement is the same length, so the cheap check a pin makes passes and
    the digest the projection takes does not. Recovery is reinstalling the model,
    not installing a language, and the engine is never invoked.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-corrupt", tmp_path, "deu", "osd"
    )
    admitted = installed_identities(resources)["deu"]
    set_languages(runner, "deu")
    admit(
        runner,
        document_id="doc-corrupt",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    payload = resources.payload_path(admitted)
    payload.write_bytes(b"x" * admitted.byte_size)

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == "ocr_model_unverified"
    assert limitations["recovery"] == "reinstall_ocr_resources"
    assert limitations["unverifiedModels"] == ["deu"]
    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["languages"] == ["deu"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"
    assert read_extraction_projection(runner, document_id="doc-corrupt") == []


def test_a_cache_that_cannot_be_read_leaves_the_obligation_pending(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The absence of an answer is not an answer, so nothing is settled on it.

    A contended cache would otherwise burn a capture into a terminal resource
    failure over a lock another administrator held for a moment. The obligation is
    left exactly as it was -- still pending, no extraction revision admitted -- and
    the next pass, against a readable cache, settles it normally under the profile
    its admission resolved.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-busy", tmp_path, "deu", "osd"
    )
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-busy",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    expected = digest_at_admission(runner, capture, authority)

    def contended(*_args: Any, **_kwargs: Any) -> Any:
        raise OcrResourceLifecycleRefused(REFUSAL_BUSY, "another operation holds it")

    monkeypatch.setattr(ManagedOcrResources, "coordinated", contended)

    assert process_capture_outbox(runner, catalogue_for=authority) == []

    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_PENDING
    assert runner_revisions(runner, "doc-busy") == []

    monkeypatch.undo()
    results = process_capture_outbox(runner, catalogue_for=authority)

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == expected


def test_a_cache_that_cannot_be_read_refuses_the_admission_retryably(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule at the other end: freeze nothing rather than freeze a guess.

    Recording "nothing is installed" because the cache could not be read would
    freeze this capture out of recognition for good -- its resource half is final
    and only an explicit reprocess changes it. Refusing is recoverable: the same
    operation key resent completes the admission.
    """
    _authority, resources = managed_installation(
        tmp_path / "inst-unreadable", tmp_path, "deu", "osd"
    )
    set_languages(runner, "deu")

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise OcrResourceStoreError("the state document could not be read")

    monkeypatch.setattr(ManagedOcrResources, "inventory", unreadable)

    with pytest.raises(DocumentIntakeRefused):
        admit(
            runner,
            document_id="doc-unreadable",
            content=image_bytes(),
            media_type="image/png",
            resources=resources,
        )

    assert runner.connection is not None
    assert (
        runner.connection.execute(
            "SELECT COUNT(*) FROM omnivia_document_captures"
        ).fetchone()[0]
        == 0
    )


def test_a_text_only_capture_freezes_and_pins_nothing(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """Recorded as a decision, and holding nothing while it waits.

    "Pinned nothing deliberately" is a value on the row rather than an absent one,
    and the operator's removal is accepted while the obligation is still queued --
    a text-only capture is never a veto over the languages it will not read.
    """
    _authority, resources = managed_installation(
        tmp_path / "inst-text", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_preferences(runner, mode=MODE_TEXT_ONLY, languages=("deu",))
    capture = admit(
        runner,
        document_id="doc-text-pins",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )

    assert frozen(runner, capture.capture_id).kind == RESOURCES_NONE
    assert resources.store.load().pins == ()
    resources.remove(installed["deu"])


def test_an_abandoned_admission_leaves_a_pin_reclamation_takes_back(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The first crash boundary: pinned, and then the capture never became durable.

    The pin has to be written before the row is committed -- the other order leaves
    an instant in which an admitted capture's bytes are removable -- so a rolled
    back admission is the price, and reclamation is how it is paid. The sweep knows
    the pin is nobody's because no unsettled obligation names it.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-abandoned", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    assert runner.workspace_id is not None
    orphan = capture_pin(runner.workspace_id, "cap-never-committed")
    resources.pin(orphan, (installed["deu"],))
    with pytest.raises(OcrResourceLifecycleRefused):
        resources.remove(installed["deu"])

    # Through the executor, which is where this is wired: the sweep runs before the
    # first pass of a new instance, so the pins a previous process abandoned are
    # released at the earliest moment anything could know they were abandoned.
    assert DocumentProcessingExecutor(
        runner, catalogue_for=authority
    ).run_pending(budget=4, force=True) == []

    assert resources.store.load().pins == ()
    resources.remove(installed["deu"])


def test_a_release_lost_to_a_crash_leaves_a_pin_reclamation_takes_back(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The second crash boundary: settled, and then the release never landed.

    Re-pinning after the obligation has settled is exactly the state such a crash
    leaves, and it is why a pin is not permanent: the next start reclaims it.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-lost", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-lost-release",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert len(process_capture_outbox(runner, catalogue_for=authority)) == 1
    assert runner.workspace_id is not None
    stranded = capture_pin(runner.workspace_id, capture.capture_id)
    resources.pin(stranded, (installed["deu"],))

    assert reclaim_capture_pins(runner, authority) == (stranded,)

    resources.remove(installed["deu"])


def test_reclamation_keeps_the_pin_of_an_obligation_that_is_still_queued(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The property that makes the sweep safe rather than merely tidy.

    A pin whose obligation is unresolved is a live claim, and the sweep that would
    release it would hand the operator a removal that breaks a queued capture. The
    executor runs this before its first pass, so it is also the moment at which
    getting it wrong would be most expensive.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-live", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    admit(
        runner,
        document_id="doc-live-pin",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )

    assert reclaim_capture_pins(runner, authority) == ()
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        resources.remove(installed["deu"])
    assert refused.value.code == REFUSAL_PINNED


# --- the pin is asserted, and the admitted profile is compared ------------------


def stored_digest(runner: ServiceRunner, capture_id: str) -> str:
    """The profile digest one capture row recorded at admission."""
    assert runner.connection is not None and runner.workspace_id is not None
    capture = store.read_capture(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    assert capture is not None and capture.processing_profile_digest is not None
    return capture.processing_profile_digest


def paused(runner: ServiceRunner, capture: store.StoredCapture, document_id: str) -> None:
    """The whole of what pausing one obligation means, asserted in one place.

    Nothing settled, nothing admitted, and the obligation still owed: the outbox
    row is pending, so a later pass replays the same row under the same key, and
    no extraction revision exists to be the answer in the meantime.
    """
    assert runner.connection is not None
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_PENDING
    assert runner_revisions(runner, document_id) == []
    assert read_extraction_projection(runner, document_id=document_id) == []


def test_a_queued_capture_whose_pin_was_lost_is_paused_not_re_pinned(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The exact bytes are still there, and that is not permission to run.

    A durable claim that has gone -- a reclamation that swept a committed
    admission, an edited state document -- means those bytes were removable for
    the whole window nothing held them, and the engine must not be invoked over
    them on the strength of their happening to have survived. Execution asserts
    the claim rather than writing it: the obligation pauses, the pin stays absent
    rather than being recreated under it, and no extraction revision is admitted.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-unpinned", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-unpinned",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert frozen(runner, capture.capture_id).selection == (
        installed["deu"],
        installed["osd"],
    )
    assert runner.workspace_id is not None
    pin = capture_pin(runner.workspace_id, capture.capture_id)
    resources.release(pin)

    assert process_capture_outbox(runner, catalogue_for=authority) == []

    paused(runner, capture, "doc-unpinned")
    # The claim was not written back, so the operator's own removal is still
    # accepted -- the state document says what it said, not what the worker
    # needed it to say.
    assert resources.store.load().pins == ()
    # And the pause is not a dead end: the claim restored, the same row settles
    # under the digest its admission recorded.
    resources.pin(pin, (installed["deu"], installed["osd"]))

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == stored_digest(
        runner, capture.capture_id
    )


def test_a_pin_over_part_of_the_frozen_selection_is_not_the_claim(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """Exactly the frozen identities, as a set, or the obligation does not run.

    A claim over some of them is a claim about a different selection: the
    orientation data here is unheld, and it is a member of the projection the
    invocation would open. Nothing is served and nothing is written -- the partial
    claim is left exactly as it was found.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-partial", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-partial",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert runner.workspace_id is not None
    pin = capture_pin(runner.workspace_id, capture.capture_id)
    resources.pin(pin, (installed["deu"],))

    assert process_capture_outbox(runner, catalogue_for=authority) == []

    paused(runner, capture, "doc-partial")
    assert [held.identity for held in resources.store.load().pins] == [installed["deu"]]
    # Named for what it is, rather than as a resource condition: the bytes are
    # installed and verifiable, and no installation repairs a missing claim.
    with pytest.raises(CaptureNotExecutable), authority.frozen_resources(
        operation_id=pin,
        admitted=frozen(runner, capture.capture_id),
        requested=("deu",),
    ):
        raise AssertionError("an unheld selection was served")


def test_an_intact_pin_executes_under_the_admitted_profile(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The ordinary case the assertion must not break.

    The claim admission took is the claim execution finds, so the obligation
    settles normally -- under the digest stored on its own row -- and the pin is
    released by the settlement rather than left behind. The same property across
    a real process restart is
    `test_a_restart_replays_the_same_frozen_work_under_the_same_key`.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-intact", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-intact",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert runner.workspace_id is not None
    pin = capture_pin(runner.workspace_id, capture.capture_id)
    assert {held.identity for held in resources.store.load().pins} == {
        installed["deu"],
        installed["osd"],
    }

    results = DocumentProcessingExecutor(runner, catalogue_for=authority).run_pending(
        budget=4, force=True
    )

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == stored_digest(
        runner, capture.capture_id
    )
    assert pin not in {held.operation_id for held in resources.store.load().pins}
    resources.remove(installed["deu"])


def test_a_profile_that_moved_under_the_build_pauses_rather_than_settling(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one input the frozen row cannot hold: the build that reads it.

    The adapter version reaches `decoderBuild` and `adapterVersion`, so moving it
    is exactly what an upgraded adapter does to the profile a row resolves to.
    Both frozen documents are untouched and the identity the capture would settle
    under has still moved, which is what the stored digest is for: the obligation
    pauses with its outbox pending, its pins held and no extraction revision
    admitted, rather than settling under a digest -- and therefore an operation
    key -- nobody was offered.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-moved", tmp_path, "deu", "osd"
    )
    installed = installed_identities(resources)
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-moved",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    admitted = stored_digest(runner, capture.capture_id)

    monkeypatch.setattr(processing, "_ADAPTER_VERSION", "99")
    assert process_capture_outbox(runner, catalogue_for=authority) == []

    paused(runner, capture, "doc-moved")
    # The pins are held through the pause, because the obligation is still owed
    # and the bytes it was admitted against must still be there when it runs.
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        resources.remove(installed["deu"])
    assert refused.value.code == REFUSAL_PINNED
    # And the stored digest did not move with the build: the capture is still the
    # capture it was admitted as, and a build that resolves it to that profile
    # again settles it normally.
    assert stored_digest(runner, capture.capture_id) == admitted

    monkeypatch.undo()
    results = process_capture_outbox(runner, catalogue_for=authority)

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == admitted


def test_ordinary_preference_and_model_changes_leave_the_stored_digest_alone(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The digest comparison must not fire on the changes END-02 already absorbs.

    A preference rewritten and a newer model revision installed are both changes
    to things the capture row does not read, so the digest the obligation resolves
    to is the digest its admission stored and the work settles normally. If the
    comparison were made against anything live, this is the test it would fail.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-unmoved", tmp_path, "deu", "fra", "osd"
    )
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-unmoved",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    admitted = stored_digest(runner, capture.capture_id)

    set_preferences(runner, languages=("fra",), embedded_image_ocr=True)
    install_revision(resources, tmp_path, "deu", "tessdata_fast-4.2.0")

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == admitted
    assert stored_digest(runner, capture.capture_id) == admitted


def test_a_text_only_capture_also_carries_the_digest_it_was_admitted_under(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """A capture that pins nothing still has an identity that can move.

    Text-only resolves no resource and holds no pin, and its profile is as much
    this build's as a recognising one's -- the adapter, the decoder build, the
    limits. So the digest is stored for it too, and a build that resolves it
    differently pauses it rather than settling it under a new identity.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-text-digest", tmp_path, "deu", "osd"
    )
    set_preferences(runner, mode=MODE_TEXT_ONLY, languages=("deu",))
    capture = admit(
        runner,
        document_id="doc-text-digest",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert frozen(runner, capture.capture_id).kind == RESOURCES_NONE
    admitted = stored_digest(runner, capture.capture_id)

    results = process_capture_outbox(runner, catalogue_for=authority)

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == admitted


def test_an_unavailable_snapshot_carries_the_digest_of_the_condition_it_recorded(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The third kind of snapshot, and the one whose profile says so.

    Nothing was installed at admission, so the capture recorded the condition and
    pinned nothing; the profile it resolves to carries the requested order, no
    model hash and `resourceState: unavailable`. That profile has a digest like
    any other and the execution settles under exactly it, so the recorded
    condition cannot be re-resolved into a different identity later.
    """
    _authority, resources = managed_installation(
        tmp_path / "inst-unavailable-digest", tmp_path, "osd"
    )
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-unavailable-digest",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert frozen(runner, capture.capture_id).kind == RESOURCES_UNAVAILABLE
    admitted = stored_digest(runner, capture.capture_id)

    results = process_capture_outbox(runner, catalogue_for=installation_catalogue(None))

    assert [result.result_state for result in results] == ["failed"]
    assert _profile_digest(runner, results[0]) == admitted
    profile = executed_profile(runner, results[0])
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"


def test_identities_admitted_for_another_engine_revision_are_never_relabelled(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old snapshot is not served under the engine this build happens to invoke.

    Asserted against the authority directly, because this is the narrower of two
    refusals a worker would reach: the stored digest moves with the engine
    revision too, so `_process_obligation` pauses on the digest before it ever
    resolves resources. What this pins is that the resolution itself refuses as
    well -- the profile would otherwise claim the current engine revision over
    models admitted for another, which moves the capture's identity.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-engine", tmp_path, "deu", "osd"
    )
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-engine",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    snapshot = frozen(runner, capture.capture_id)
    assert snapshot.kind == RESOURCES_FROZEN
    assert runner.workspace_id is not None

    # The build moves on; the snapshot keeps the revision it was admitted for.
    monkeypatch.setattr(processing_settings, "TESSERACT_REVISION", "9.9.9")

    with pytest.raises(CaptureNotExecutable), authority.frozen_resources(
        operation_id=capture_pin(runner.workspace_id, capture.capture_id),
        admitted=snapshot,
        requested=("deu",),
    ):
        raise AssertionError("a foreign engine revision was served")


# --- the pending input itself, across both kinds of update (§18.4, END-02)


def extraction_key(capture_id: str, profile_digest: str) -> str:
    """The operation key one capture's extraction is admitted under.

    Derived here the way `_execute_obligation` derives it, from the capture
    identifier and the profile digest, because that is the point: the key is a
    function of the admitted digest, so a test that pins the digest has pinned
    the key only if it also shows the key the revision actually carries is this
    one.
    """
    return (
        "extract-"
        + hashlib.sha256(f"{capture_id}\0{profile_digest}".encode()).hexdigest()
    )


def pending_input(runner: ServiceRunner, capture_id: str) -> dict[str, Any]:
    """Everything one queued capture froze about its own input, read from the row.

    The admitted bytes and the three frozen documents in one value, so a
    before-and-after is one comparison rather than six that could each be
    forgotten. The bytes come through `read_retained_source`, which is the
    production reader and re-hashes the blob against the digest and length the
    row recorded -- so a row and a blob that had drifted apart could not both be
    read and compared here at all.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    capture = store.read_capture(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    assert capture is not None
    return {
        "bytes": read_retained_source(runner, capture_id=capture_id),
        "digest": capture.source_digest,
        "length": capture.source_length_bytes,
        "operation_key": capture.operation_key,
        "profile_digest": capture.processing_profile_digest,
        "resources": capture.processing_resources_json,
        "settings": capture.processing_settings_json,
    }


def test_a_profile_and_resource_update_leave_the_pending_input_untouched(
    paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """END-02's first clause over the input itself, not only over the identity.

    The capture is queued; the workspace's profile preferences are then rewritten
    and a newer model revision of its own language installed -- both kinds of
    update the gate names, at once -- and the admitting process stops so nothing
    in memory can carry the old answers forward.

    What is asserted is every part of the pending input: the retained bytes, the
    source digest and length the row recorded, the frozen preferences, the frozen
    resource identities and their digests, the admitted profile digest, and the
    capture's own operation key. Then the work runs, and the extraction's
    operation key is the key derived from that admitted digest -- so the identity
    the obligation settles under is the identity it was admitted with, and the
    updates reached neither half.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-frozen-input", tmp_path, "deu", "osd"
    )
    admitted_identity = installed_identities(resources)["deu"]
    source = image_bytes("Frozen")
    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as first:
        set_languages(first, "deu")
        capture = admit(
            first,
            document_id="doc-frozen-input",
            content=source,
            media_type="image/png",
            resources=resources,
        )
        admitted = pending_input(first, capture.capture_id)
        # Taken at admission, so what follows is compared against the row as it
        # was written rather than against a second reading of the same row.
        assert admitted["bytes"] == source
        assert admitted["digest"] == f"sha256:{hashlib.sha256(source).hexdigest()}"
        assert admitted["length"] == len(source)
        # The frozen resource identities, decoded: the exact identity and digest
        # the admission resolved, which is what the stored document has to keep.
        assert frozen(first, capture.capture_id).selection == (
            admitted_identity,
            installed_identities(resources)["osd"],
        )

    # Both updates the gate names, while the obligation is queued and unresolved.
    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as between:
        set_preferences(between, languages=("fra",), embedded_image_ocr=True)
    newer = install_revision(resources, tmp_path, "deu", "tessdata_fast-4.2.0")
    assert installed_identities(resources)["deu"] == newer

    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as second:
        # Nothing about the pending input moved: not one of the seven values.
        assert pending_input(second, capture.capture_id) == admitted

        results = DocumentProcessingExecutor(
            second, catalogue_for=authority
        ).run_pending(budget=4, force=True)

        assert len(results) == 1
        # The admitted digest, and therefore the admitted key. Looked up *by* the
        # derived key, so the revision that settled is the revision that key
        # names and not merely one with a digest that happens to match.
        assert _profile_digest(second, results[0]) == admitted["profile_digest"]
        assert second.connection is not None and second.workspace_id is not None
        assert admitted["profile_digest"] is not None
        keyed = store.read_revision_by_key(
            second.connection,
            workspace_id=second.workspace_id,
            operation_key=extraction_key(
                capture.capture_id, admitted["profile_digest"]
            ),
        )
        assert keyed is not None
        assert keyed.extraction_revision_id == results[0].extraction_revision_id
        # And the input is still the admitted input after the extraction read it.
        assert pending_input(second, capture.capture_id) == admitted
        profile = executed_profile(second, results[0])
        assert profile["ocr"]["modelHashes"] == sorted(
            {
                f"sha256:{admitted_identity.sha256}",
                f"sha256:{installed_identities(resources)['osd'].sha256}",
            }
        )
        assert f"sha256:{newer.sha256}" not in profile["ocr"]["modelHashes"]


# --- a full managed cache defers a new profile (§18.4, END-02)


def fill_to_the_cap(resources: ManagedOcrResources, language: str) -> None:
    """Account this cache to exactly the design's 512 MiB, without writing it.

    One declared entry takes up whatever the real installed sets leave, which is
    how `test_installing_exactly_up_to_the_cap_is_accepted` reaches the same
    figure: the cap the reservation runs against is the product's own rather than
    a shrunken one, and no test writes half a gigabyte. The filler has no bytes
    on disk, so it is never reclaimable and never serves a language either.
    """
    state = resources.store.load()
    resources.store.save(
        OcrResourceStoreState(
            entries=(
                *state.entries,
                OcrResourceSetIdentity(
                    language=language,
                    engine_version=TESSERACT_REVISION,
                    model_version="tessdata_fast-4.1.0",
                    byte_size=DEFAULT_OCR_RESOURCE_CAPACITY_BYTES
                    - sum(entry.byte_size for entry in state.entries),
                    sha256="e" * 64,
                ),
            ),
            pins=state.pins,
        )
    )
    assert resources.inventory().available_bytes == 0


def trusted_catalogue(
    monkeypatch: pytest.MonkeyPatch, sizes: Mapping[str, int]
) -> dict[str, OcrResourceSetIdentity]:
    """The resource owner's installable sets, through the real trust gate.

    §18.4 installs additional languages from the owner's declared sizes/digests,
    and the only authority this build accepts for one is a model manifest that
    `verify_model_manifest` has verified completely. So the catalogue these tests
    size a reservation against is built by signing a manifest, verifying it under
    a dev anchor, and mapping the verified artifacts -- not by handing
    `admitted_resources` a dictionary it could not have obtained. The anchor is a
    dev/test one behind its own flag and is never production trust; what it proves
    is that the mechanism carries declared sizes end to end.
    """
    monkeypatch.setenv(DEV_TEST_ANCHOR_FLAG, "1")
    moment = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    private_key, anchor = dev_test_anchor(
        not_before=moment - timedelta(days=1), not_after=moment + timedelta(days=365)
    )
    document: dict[str, Any] = {
        "schema_version": "model-manifest.v1",
        "manifest_identity": "",
        "identifier": "core.ocr-language-resources",
        "version": "tessdata_fast-4.1.0",
        "runtime_contract_version": "1.3",
        "artifacts": [
            {
                "name": f"{language}{MODEL_SUFFIX}",
                "sha256": hashlib.sha256(f"declared-{language}".encode()).hexdigest(),
                "size": size,
            }
            for language, size in sizes.items()
        ],
        "signing": {"key_id": anchor.key_id},
    }
    signature = sign_model_manifest(
        document, key_id=anchor.key_id, private_key=private_key
    )
    document["manifest_identity"] = model_manifest_identity(document)
    return declared_installable(
        verify_model_manifest(
            document,
            dict(signature),
            trust_anchors=[anchor],
            verification_time=moment,
        )
    )


def test_a_new_profile_meeting_a_full_cache_is_deferred_not_refused_as_missing(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18.4 defers a new-profile admission the cap leaves no room for.

    The managed cache is accounted to its full 512 MiB and every set with bytes
    is pinned by a capture whose obligation has not settled, so nothing is
    reclaimable and not one of the declared bytes could be installed. A second
    capture then asks for a language the cache has not got, and the owner's
    catalogue says what installing it would cost.

    Recording the missing-language condition would be terminal for that capture
    and would name an install the full cache must itself refuse. So the admission
    is deferred instead, with the required and available bytes stated: nothing is
    written, no pin is taken, the cap and the existing pins stand, the queued work
    is untouched -- and once that work settles and releases its claim, the same
    admission goes through.
    """
    authority, resources = managed_installation(
        tmp_path / "inst-cap-defer", tmp_path, "eng", "osd"
    )
    held = installed_identities(resources)
    # Declared at exactly what this cache's own installed sets could supply if
    # nothing held them, so the deferral and the later acceptance differ in one
    # thing only: whether the queued obligation still pins those bytes.
    declared = sum(identity.byte_size for identity in held.values())
    catalogue = trusted_catalogue(monkeypatch, {"deu": declared})
    set_languages(runner, "eng")
    queued = admit(
        runner,
        document_id="doc-cap-queued",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert frozen(runner, queued.capture_id).kind == RESOURCES_FROZEN
    fill_to_the_cap(resources, "jpn")
    before = resources.store.state_path.read_bytes()

    set_languages(runner, "deu")
    with pytest.raises(LanguageStorageFull) as deferred:
        admit(
            runner,
            document_id="doc-cap-new",
            content=image_bytes(),
            media_type="image/png",
            resources=resources,
            installable=catalogue,
        )

    assert deferred.value.code == "language_storage_full"
    # The capacity §18.4 requires to be visible, as typed facts rather than prose:
    # the declared size of the set this profile needs, the bytes the cache could
    # actually have supplied, and the exact identity it was asked for.
    assert deferred.value.required_bytes == declared
    assert deferred.value.available_bytes == 0
    assert deferred.value.languages == ("deu",)
    assert deferred.value.identities == (catalogue["deu"],)
    # Deferred, not admitted: no capture row, so no obligation and no pin.
    assert runner.connection is not None and runner.workspace_id is not None
    assert (
        store.read_capture_by_key(
            runner.connection,
            workspace_id=runner.workspace_id,
            operation_key="capture-doc-cap-new",
        )
        is None
    )
    # The cap, the pins and the existing queued work are exactly as they stood,
    # and the deferral installed and downloaded nothing.
    assert resources.store.state_path.read_bytes() == before
    inventory = resources.inventory()
    assert inventory.installed_bytes == DEFAULT_OCR_RESOURCE_CAPACITY_BYTES
    assert inventory.installed_bytes <= inventory.capacity_bytes
    assert installed_identities(resources) == held
    assert {pin.operation_id for pin in resources.store.load().pins} == {
        capture_pin(runner.workspace_id, queued.capture_id)
    }
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id,
        capture_id=queued.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_PENDING
    # And the operator's own install of that language is deferred too, against
    # this same real admission pin rather than a synthetic one: §18.4's "an
    # attempted install cannot cancel that pin" over the claim an admission
    # actually wrote.
    body = b"model-deu-arriving"
    arriving = tmp_path / "ocr-downloads" / "deu-arriving.bin"
    arriving.write_bytes(body)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        resources.install(
            arriving,
            OcrResourceSetIdentity(
                language="deu",
                engine_version=TESSERACT_REVISION,
                model_version="tessdata_fast-4.1.0",
                byte_size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            ),
        )
    assert refused.value.code == "language_storage_full"
    assert resources.store.state_path.read_bytes() == before

    # Deferred *until room exists*: the queued obligation settles, its pin goes,
    # and the very same admission is now accepted.
    assert len(process_capture_outbox(runner, catalogue_for=authority)) == 1
    assert resources.store.load().pins == ()

    readmitted = admit(
        runner,
        document_id="doc-cap-new",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
        installable=catalogue,
    )

    # Accepted, and honest about the language it still has not got: this is the
    # condition the deferral existed to avoid recording prematurely, recorded now
    # that installing the language is something the cache can actually do.
    snapshot = frozen(runner, readmitted.capture_id)
    assert snapshot.kind == RESOURCES_UNAVAILABLE
    assert snapshot.reason == NEEDS_OCR_LANGUAGE
    assert snapshot.unavailable_models == ("deu",)


def test_a_declared_requirement_no_reclaimable_set_covers_defers_the_admission(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reclaimable set is not capacity unless it covers what the profile needs.

    This is the defect the sized reservation exists to remove. The cache is at its
    cap with one *unpinned, verified* set of a few bytes beside the filler, so
    something is reclaimable -- and the language the new profile selects is
    declared at a megabyte. An admission that asked only whether some set could be
    evicted would admit this capture, whose install the same cache must then refuse
    for exactly the capacity it was just told about.

    So the comparison is made, the admission is deferred, and the figures are the
    real ones: the declared megabyte against the handful of bytes the cache could
    have reclaimed. Nothing is evicted to find out -- the reclaimable set is still
    installed with its bytes, because §18.4 reserves by refusing and a deferral
    deletes nothing.
    """
    _authority, resources = managed_installation(
        tmp_path / "inst-cap-small", tmp_path, "eng", "osd"
    )
    held = installed_identities(resources)
    reclaimable = sum(identity.byte_size for identity in held.values())
    catalogue = trusted_catalogue(monkeypatch, {"deu": 1 << 20})
    fill_to_the_cap(resources, "jpn")
    before = resources.store.state_path.read_bytes()

    set_languages(runner, "deu")
    with pytest.raises(LanguageStorageFull) as deferred:
        admit(
            runner,
            document_id="doc-cap-small",
            content=image_bytes(),
            media_type="image/png",
            resources=resources,
            installable=catalogue,
        )

    assert deferred.value.required_bytes == 1 << 20
    assert deferred.value.available_bytes == reclaimable
    assert 0 < reclaimable < (1 << 20)
    assert resources.store.state_path.read_bytes() == before
    assert installed_identities(resources) == held
    assert resources.inventory().installed_bytes == DEFAULT_OCR_RESOURCE_CAPACITY_BYTES

    # And the same cache admits the same profile the moment the requirement is one
    # it can cover: a set declared at what those reclaimable bytes can supply is
    # recorded as the missing language it is, with its install left possible.
    smaller = trusted_catalogue(monkeypatch, {"deu": reclaimable})
    admitted = admit(
        runner,
        document_id="doc-cap-small-fits",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
        installable=smaller,
    )

    snapshot = frozen(runner, admitted.capture_id)
    assert snapshot.kind == RESOURCES_UNAVAILABLE
    assert snapshot.reason == NEEDS_OCR_LANGUAGE
    assert snapshot.unavailable_models == ("deu",)
    assert resources.store.state_path.read_bytes() == before


def test_a_language_no_trusted_catalogue_declares_establishes_no_requirement(
    runner: ServiceRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undeclared language has no trusted size, so there is nothing to reserve.

    The cache is full and nothing is reclaimable, which is exactly the state that
    defers an admission for a *declared* set -- and this selection names one the
    owner's catalogue does not carry at all. Inventing a size for it would be
    reserving against a figure nobody declared, and refusing without one would
    defer a capture on no evidence, so the honest answer is the missing-language
    condition this installation has always recorded.

    The same for a selection the catalogue covers only in part: the admission is
    all or nothing, so a requirement assembled from half of it would be a figure
    for work that is not being done.
    """
    _authority, resources = managed_installation(
        tmp_path / "inst-cap-undeclared", tmp_path, "eng", "osd"
    )
    held = installed_identities(resources)
    queued = admit(
        runner,
        document_id="doc-cap-undeclared-queued",
        content=image_bytes(),
        media_type="image/png",
        resources=resources,
    )
    assert frozen(runner, queued.capture_id).kind == RESOURCES_FROZEN
    fill_to_the_cap(resources, "jpn")

    set_languages(runner, "deu", "fra")
    for name, catalogue in (
        ("doc-cap-undeclared", {}),
        ("doc-cap-partly-declared", trusted_catalogue(monkeypatch, {"deu": 4096})),
    ):
        admitted = admit(
            runner,
            document_id=name,
            content=image_bytes(),
            media_type="image/png",
            resources=resources,
            installable=catalogue,
        )
        snapshot = frozen(runner, admitted.capture_id)
        assert snapshot.kind == RESOURCES_UNAVAILABLE
        assert snapshot.reason == NEEDS_OCR_LANGUAGE
        assert snapshot.unavailable_models == ("deu", "fra")

    assert installed_identities(resources) == held


# --- a replaced packaged payload pauses rather than settling (§18.4, END-02)


def declared_models(directory: Path, *languages: str) -> dict[str, tuple[int, str]]:
    """The size and digest of each bundled model, as a verified manifest names it."""
    return {
        language: (
            (directory / f"{language}{MODEL_SUFFIX}").stat().st_size,
            digest_of(directory / f"{language}{MODEL_SUFFIX}"),
        )
        for language in languages
    }


def packaged_authority(
    root: Path, models: Path, *languages: str
) -> ManagedSelectionAuthority:
    """An installation whose selection is served by its *bundled* models.

    The managed cache at `root` is never created: a selection the packaged set
    covers whole is frozen against the installation's own identities and pinned
    nowhere, so there is nothing for the cache to hold.
    """
    runtime = host_runtime(models, declared_models(models, *languages))
    if runtime is None:  # pragma: no cover - host dependent
        pytest.skip("no host OCR engine and RTF reader to bind as the payload")
    return ManagedSelectionAuthority(
        ManagedOcrResources(root / OCR_RESOURCES_DIR), runtime=runtime
    )


def test_replaced_packaged_model_bytes_pause_the_obligation(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """§18.4's "pause with a specific compatibility/resource issue", for the
    bundled set.

    This capture was admitted wholly against the packaged English and orientation
    models, so no pin was ever taken for it -- those bytes are the installation's
    rather than the managed cache's. The installation is then replaced: the
    payload still declares both models, but not the exact bytes this obligation
    was admitted against.

    Settling it as an unverified model would make the capture terminal over a
    fact about the installation, and spend the only admission a repair could have
    served. So the obligation pauses: its outbox row stays pending, no extraction
    revision is admitted, and nothing is served in the admitted bytes' place.
    Pausing costs nothing here, because an obligation holding no managed pin
    occupies no managed capacity -- and it is not a dead end, which the restored
    payload shows.
    """
    models = tmp_path / "packaged-tessdata"
    models.mkdir()
    stand_in_models(models, "eng", "osd")
    original = (models / f"eng{MODEL_SUFFIX}").read_bytes()
    authority = packaged_authority(tmp_path / "inst-pkg", models, "eng", "osd")
    set_languages(runner, "eng")
    capture = admit(
        runner,
        document_id="doc-pkg-replaced",
        content=image_bytes(),
        media_type="image/png",
        packaged=packaged_identities(authority.runtime),
    )
    snapshot = frozen(runner, capture.capture_id)
    assert snapshot.kind == RESOURCES_FROZEN
    # Frozen against the bundled identities, and holding no managed claim.
    assert not (tmp_path / "inst-pkg" / OCR_RESOURCES_DIR).exists()

    # The installation is replaced: both models are still declared, and English
    # is not the bytes this capture was admitted against.
    (models / f"eng{MODEL_SUFFIX}").write_bytes(b"a-different-english-model")
    replaced = packaged_authority(tmp_path / "inst-pkg", models, "eng", "osd")

    assert process_capture_outbox(runner, catalogue_for=replaced) == []

    paused(runner, capture, "doc-pkg-replaced")
    # Restored, and the same row settles under the digest its admission recorded.
    (models / f"eng{MODEL_SUFFIX}").write_bytes(original)
    restored = packaged_authority(tmp_path / "inst-pkg", models, "eng", "osd")

    results = process_capture_outbox(runner, catalogue_for=restored)

    assert len(results) == 1
    assert _profile_digest(runner, results[0]) == stored_digest(
        runner, capture.capture_id
    )


def test_an_absent_packaged_payload_pauses_rather_than_reporting_unverified(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """The same answer when the payload is not there at all.

    An installation with no verified payload declares no model, so it cannot
    declare the ones this capture was admitted against either. That is the same
    unrecoverable-exact-resource fact as replaced bytes and takes the same pause,
    rather than becoming a missing-language condition the operator would try to
    repair by installing a managed language this capture would never consult.
    """
    models = tmp_path / "packaged-absent"
    models.mkdir()
    stand_in_models(models, "eng", "osd")
    authority = packaged_authority(tmp_path / "inst-absent", models, "eng", "osd")
    set_languages(runner, "eng")
    capture = admit(
        runner,
        document_id="doc-pkg-absent",
        content=image_bytes(),
        media_type="image/png",
        packaged=packaged_identities(authority.runtime),
    )
    assert frozen(runner, capture.capture_id).kind == RESOURCES_FROZEN

    gone = ManagedSelectionAuthority(
        ManagedOcrResources(tmp_path / "inst-absent" / OCR_RESOURCES_DIR)
    )

    assert process_capture_outbox(runner, catalogue_for=gone) == []

    paused(runner, capture, "doc-pkg-absent")
