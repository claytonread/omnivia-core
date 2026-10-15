from __future__ import annotations

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
)
from omnivia_core_runtime.service.document_intake import (
    capture_document_bytes,
    owned_workspace,
    read_extraction_projection,
)
from omnivia_core_runtime.service.document_processing import (
    NEEDS_OCR_LANGUAGE,
    PROCESSING_SETTINGS_INVALID,
    TEXT_ONLY_PROFILE_REF,
    DocumentProcessingExecutor,
    portable_profile,
    process_capture_outbox,
    resolve_settings,
)
from omnivia_core_runtime.service.ocr_resources import TrustedOcrCatalogue
from omnivia_core_runtime.service.processing_settings import (
    ProcessingSettings,
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
    runner: ServiceRunner, host_catalogue: TrustedOcrCatalogue
) -> None:
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 55),
        "Captured OCR",
        fill="black",
        font=ImageFont.load_default(size=92),
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    capture_document_bytes(
        runner,
        content=encoded.getvalue(),
        document_id="doc-processing-image",
        expected_base_capture_id=None,
        operation_key="capture-processing-image",
        media_type="image/png",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(
        runner, catalogue_for=serving(host_catalogue)
    ).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-image", query="Captured"
    )
    assert "Captured OCR" in " ".join(segment.text or "" for segment in projected)


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
) -> store.StoredCapture:
    captured = capture_document_bytes(
        runner,
        content=content,
        document_id=document_id,
        expected_base_capture_id=None,
        operation_key=f"capture-{document_id}",
        media_type=media_type,
        intent_kind=store.INTENT_NEW_CAPTURE,
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

    Two independent things are asserted together on purpose: the resolver is asked
    for exactly the frozen order, and the profile records exactly that order with one
    pinned hash per model it read -- so a profile could not claim an order the
    invocation did not use.
    """
    set_languages(runner, *order)
    catalogue = verified_catalogue(tmp_path, *order, "osd")
    resolver, seen = asked(catalogue)
    admit(
        runner,
        document_id="doc-order",
        content=image_bytes(),
        media_type="image/png",
    )

    results = process_capture_outbox(runner, catalogue_for=resolver)

    assert seen == [order]
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["languages"] == list(order)
    assert profile["ocr"]["modelHashes"] == sorted(
        f"sha256:{digest}" for digest in catalogue.installed.values()
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
    admit(
        runner,
        document_id="doc-race",
        content=image_bytes(),
        media_type="image/png",
    )
    # The preference moves after admission and before the bounded pass runs.
    set_languages(runner, "fra")
    catalogue = verified_catalogue(tmp_path, "deu", "fra", "osd")
    resolver, seen = asked(catalogue)

    results = process_capture_outbox(runner, catalogue_for=resolver)

    assert seen == [("deu",)]
    assert executed_profile(runner, results[0])["ocr"]["languages"] == ["deu"]
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
    admit(
        runner,
        document_id="doc-race-mode",
        content=image_bytes(),
        media_type="image/png",
    )
    set_preferences(runner, mode="text_only")
    resolver, seen = asked(verified_catalogue(tmp_path, "eng", "osd"))

    results = process_capture_outbox(runner, catalogue_for=resolver)

    assert seen == [("eng",)]
    assert executed_profile(runner, results[0])["processingMode"] == "text_and_local_ocr"


def test_a_restart_replays_the_same_frozen_work_under_the_same_key(
    paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """A real restart: the admitting process stops and another one picks the work up.

    Nothing in memory survives, so if the preference were read at execution the new
    process would read the *changed* one. It reads the capture row instead, which is
    why the profile, the extraction revision and the operation key the replay settles
    under are the ones the first process's admission determined.
    """
    catalogue = verified_catalogue(tmp_path, "deu", "eng", "osd")
    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as first:
        set_languages(first, "deu")
        admit(
            first,
            document_id="doc-restart",
            content=image_bytes(),
            media_type="image/png",
        )
        # Changed by the same process that admitted, after admission; the restart is
        # what proves no in-memory value carried the old one forward.
        set_languages(first, "eng")

    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as second:
        resolver, seen = asked(catalogue)
        results = DocumentProcessingExecutor(
            second, catalogue_for=resolver
        ).run_pending(budget=4, force=True)

        assert seen == [("deu",)]
        # Whether recognition then succeeds is this host's business -- these are
        # stand-in model bytes, so it does not. What the restart decides is which
        # profile the obligation settles under, and that is the admitted one.
        assert len(results) == 1
        assert executed_profile(second, results[0])["ocr"]["languages"] == ["deu"]
        settled = results[0].extraction_revision_id
        # A second pass after the restart adds nothing: the obligation is settled and
        # the capture is not processed again under a second identity.
        assert DocumentProcessingExecutor(second, catalogue_for=resolver).run_pending(
            budget=4, force=True
        ) == []
        assert seen == [("deu",)]
        assert runner_revisions(second, "doc-restart") == [settled]


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
    resolver, seen = asked(verified_catalogue(tmp_path, "deu", "osd"))
    admit(
        runner,
        document_id="doc-embedded-docx",
        content=buffer.getvalue(),
        media_type=DOCX_MEDIA_TYPE,
    )

    results = process_capture_outbox(runner, catalogue_for=resolver)

    assert seen == [("deu",)]
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["embeddedImageOcr"] is True
    assert profile["ocr"]["languages"] == ["deu"]


# --- the compatibility path and its boundary ------------------------------------


def test_a_capture_that_recorded_no_selection_runs_the_behaviour_it_was_admitted_under(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 0071's NULL: no recorded selection, so an *unconfigured* request.

    Deterministic and distinguishable: the resolver is asked for nothing rather than
    for English, and the outcome reports `ocr_resources_unconfigured` -- which says
    the installation has no resources, not that a language the user chose is missing.
    """
    monkeypatch.setattr(document_intake, "_admitted_settings", lambda _runner: None)
    # The workspace does hold a preference, which this capture must not pick up.
    set_languages(runner, "deu")
    capture = admit(
        runner,
        document_id="doc-legacy",
        content=image_bytes(),
        media_type="image/png",
    )
    assert capture.processing_settings_json is None
    resolver, seen = asked(TrustedOcrCatalogue())

    results = process_capture_outbox(runner, catalogue_for=resolver)

    assert seen == [()]
    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == "ocr_resources_unconfigured"
    profile = executed_profile(runner, results[0])
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["languages"] == ["eng"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"


def test_a_malformed_snapshot_is_refused_rather_than_reinterpreted(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot written by a build this one cannot read is not the legacy case.

    Reading it as "no selection" would run the product default under the key of a
    decision nobody can establish. It settles as failed, naming the one code that
    says the preferences themselves could not be read, and no recognition is
    attempted.
    """
    monkeypatch.setattr(
        document_intake,
        "_admitted_settings",
        lambda _runner: '{"mode": "text_and_future_ocr", "languages": ["eng"], '
        '"embeddedImageOcr": false}',
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
    catalogue = verified_catalogue(tmp_path, "deu", "eng", "osd")
    admit(
        runner,
        document_id="doc-no-substitute",
        content=image_bytes(),
        media_type="image/png",
    )

    results = process_capture_outbox(runner, catalogue_for=serving(catalogue))

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
