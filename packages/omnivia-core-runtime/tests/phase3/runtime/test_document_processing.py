from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from _trusted_ocr import catalogue_over, host_models, stand_in_models
from omnivia_core_runtime.service import document_intake
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
    OcrResourceSetIdentity,
    OcrResourceStoreError,
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
    ManagedSelectionAuthority,
    ProcessingSettings,
    capture_pin,
    installation_catalogue,
    installed_identities,
    read_settings,
    write_settings,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.storage import document_intake as store
from PIL import Image, ImageDraw, ImageFont

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
    authority = installation_catalogue(root)
    assert authority is not None
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
    authority = installation_catalogue(root)
    assert authority is not None
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
) -> store.StoredCapture:
    """One admitted capture, frozen and pinned against `resources`.

    `resources` is the installation's managed cache, and passing it is what makes
    the admission able to freeze exact identities: without one there is nothing
    installed to freeze, which the capture records as the condition it is.
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


def test_embedded_image_recognition_reaches_a_source_that_carries_images(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Carrier</w:t>'
            "</w:r></w:p></w:body></w:document>",
        )
    set_preferences(runner, languages=("deu",), embedded_image_ocr=True)
    authority, resources = managed_installation(
        tmp_path / "inst-embedded", tmp_path, "deu", "osd"
    )
    capture = admit(
        runner,
        document_id="doc-embedded-docx",
        content=buffer.getvalue(),
        media_type=DOCX_MEDIA_TYPE,
        resources=resources,
    )

    results = process_capture_outbox(runner, catalogue_for=authority)

    # A source that carries images qualifies, so the admission pinned for it.
    assert frozen(runner, capture.capture_id).languages == ("deu",)
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["embeddedImageOcr"] is True
    assert profile["ocr"]["languages"] == ["deu"]


# --- the compatibility path and its boundary ------------------------------------


def legacy_admission(monkeypatch: pytest.MonkeyPatch, *documents: str | None) -> None:
    """Admit as a build that recorded neither snapshot, or an unreadable one."""
    monkeypatch.setattr(
        document_intake,
        "_admitted_processing",
        lambda _runner, **_kwargs: documents,
    )


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


def test_service_executor_processes_rtf_through_the_retained_copy(
    runner: ServiceRunner,
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

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-rtf", query="RTF"
    )
    assert "Core RTF content" in " ".join(segment.text or "" for segment in projected)


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
