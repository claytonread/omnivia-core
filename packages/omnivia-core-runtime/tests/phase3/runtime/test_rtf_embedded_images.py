"""RTF embedded rasters: the one bounded pass that reads a ``\\pict`` payload.

The bounded scan has always inventoried a picture group without reading one.
This exercises the pass that reads it: the candidate seam out of the scanner's
own single traversal, the aggregate budget shared by every picture of one
source, and the per-picture truth the limitations report when nothing was read.

The scanner stays authoritative for the payload's extent, so the cases here are
written against the lexical forms that decide it -- a hex payload, a
length-delimited ``\\bin`` payload, an escaped delimiter inside the payload, a
nested sub-destination beside it. Only a picture group the document itself
displays is a candidate: an OLE payload, an ``\\objdata`` blob, a linked
object's stand-in and the cached result of an ``INCLUDEPICTURE`` field are inert
here however the profile is set, and no path, URL or sidecar resource is ever
opened, so an RTFD bundle's images are unreachable by construction.

The pinned reader is fixed throughout so what is asserted is this adapter's
behaviour rather than whatever a host's Pandoc makes of a fixture, and the
recognition engine is injected for the same reason.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from _trusted_ocr import catalogue_over, reader_settings, stand_in_models
from omnivia_core_runtime.service import ocr_resources, rtf_extractor
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.raster_extractors import (
    EMBEDDED_DISABLED,
    EMBEDDED_MALFORMED,
    EMBEDDED_NO_TEXT,
    EMBEDDED_NOT_EMBEDDED,
    EMBEDDED_RECOGNISED,
    EMBEDDED_RESOURCES_UNAVAILABLE,
    EMBEDDED_TRUNCATED,
    EMBEDDED_UNSUPPORTED,
    GEOMETRY_SPACE,
    ExtractionBudget,
)
from omnivia_core_runtime.service.rtf_extractor import (
    MAX_PICTURE_RECORDS,
    RTFExtraction,
    extract_rtf,
)
from PIL import Image, ImageDraw

CAPTURE: Final = "dcap-" + "a" * 64
SIZE: Final = (120, 40)
FIXED_AST: Final = json.dumps(
    {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [{"t": "Para", "c": [{"t": "Str", "c": "Body"}]}],
    }
).encode()


def real_png(label: str = "INVOICE", size: tuple[int, int] = SIZE) -> bytes:
    """A small, genuinely decodable PNG, written by Pillow rather than by hand."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((4, 10), label, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def real_jpeg(size: tuple[int, int] = SIZE) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def pict(
    payload: bytes, *, declared: bytes = b"\\pngblip", extra: bytes = b""
) -> bytes:
    """One picture group: a declared type, the size words Word writes, a payload."""
    return (
        b"{\\pict"
        + declared
        + b"\\picw120\\pich40\\picwgoal1800\\pichgoal600"
        + extra
        + b" "
        + payload
        + b"}"
    )


def hex_payload(content: bytes) -> bytes:
    return content.hex().encode("ascii")


def binary_payload(content: bytes) -> bytes:
    return b"\\bin" + str(len(content)).encode("ascii") + b" " + content


def source(*body: bytes) -> bytes:
    """One RTF source whose body is the given fragments, with prose beside them.

    The prose is there so the structural extraction is visibly published beside
    whatever the image pass did or did not read.
    """
    return b"{\\rtf1\\ansi\\ansicpg1252 Invoice total\\par " + b"".join(body) + b"}"


class RecordingOcr:
    """An injected engine returning one fixed line, in Tesseract's TSV shape."""

    def __init__(self, *words: str, confidence: float = 91.0) -> None:
        self.words = words or ("INVOICE", "7")
        self.confidence = confidence
        self.sizes: list[tuple[int, int]] = []

    def __call__(
        self,
        image: Image.Image,
        *,
        resources: ocr_resources.OcrResources,
        psm: int,
        timeout_seconds: float,
    ) -> dict[str, list[object]]:
        self.sizes.append(image.size)
        data: dict[str, list[object]] = {
            key: [] for key in ("text", "conf", "left", "top", "width", "height")
        }
        data.update({"block_num": [], "par_num": [], "line_num": []})
        for index, word in enumerate(self.words):
            data["text"].append(word)
            data["conf"].append(self.confidence)
            data["left"].append(4 + index * 30)
            data["top"].append(10)
            data["width"].append(28)
            data["height"].append(12)
            data["block_num"].append(1)
            data["par_num"].append(1)
            data["line_num"].append(1)
        return data


class RefusingOcr:
    """An engine that fails the test if anything invokes it."""

    def __call__(
        self, image: Image.Image, **_kwargs: object
    ) -> dict[str, list[object]]:
        raise AssertionError("the recognition engine must not have been invoked")


class CountingPillow:
    """A spy over the decoder, so an unopened payload is visibly unopened.

    ``Image.open`` is the one seam a payload's bytes are decoded through, so a
    candidate the pass refused before reading leaves no entry here even though
    the scanner walked straight over its bytes.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[int] = []
        original = Image.open

        def spy(source: Any, *args: Any, **kwargs: Any) -> Image.Image:
            self.opened.append(len(source.getvalue()))
            return original(source, *args, **kwargs)

        monkeypatch.setattr(
            "omnivia_core_runtime.service.raster_extractors.Image.open", spy
        )


@pytest.fixture
def reader() -> Iterator[list[tuple[Any, dict[str, Any]]]]:
    """Fix the pinned reader's result and record how it was called.

    Every test here is about the scan and the image pass beside it, both of
    which run around a reader whose body text is beside the point, so the reader
    returns the same AST for every source.
    """
    calls: list[tuple[Any, dict[str, Any]]] = []
    original = subprocess.run

    def run(arguments: Any, **options: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, FIXED_AST, b"")

    rtf_extractor.subprocess.run = run  # type: ignore[assignment]
    try:
        yield calls
    finally:
        rtf_extractor.subprocess.run = original  # type: ignore[assignment]


@pytest.fixture
def examining(tmp_path: Path) -> ocr_resources.ExtractionSettings:
    """Settings whose profile examines embedded images and whose models verify."""
    stand_in_models(tmp_path, "eng")
    return ocr_resources.ExtractionSettings(
        processing_mode=ocr_resources.MODE_TEXT_AND_LOCAL_OCR,
        ocr=catalogue_over(tmp_path, "eng").resolve(("eng",)),
        embedded_image_ocr=True,
    )


@pytest.fixture
def unavailable() -> ocr_resources.ExtractionSettings:
    """A profile that asks for recognition whose pinned models were not served.

    Production fails closed, so the packaged catalogue resolves to unavailable
    resources: the profile keeps its requested identity and nothing may run.
    """
    settings = ocr_resources.ExtractionSettings(
        processing_mode=ocr_resources.MODE_TEXT_AND_LOCAL_OCR,
        ocr=ocr_resources.TrustedOcrCatalogue().resolve(("eng",)),
        embedded_image_ocr=True,
    )
    assert settings.ocr is not None and not settings.ocr.available
    return settings


def examined(
    content: bytes,
    settings: ocr_resources.ExtractionSettings | None = None,
    engine: object | None = None,
    budget: ExtractionBudget | None = None,
) -> RTFExtraction:
    return extract_rtf(
        content,
        capture_id=CAPTURE,
        # Production hands the adapter the absolute packaged reader; the reader
        # itself is mocked here, so what matters is that one is bound at all.
        settings=reader_settings(settings),
        ocr_engine=engine if engine is not None else RefusingOcr(),  # type: ignore[arg-type]
        budget=budget,
    )


def pictures(result: RTFExtraction) -> list[dict[str, object]]:
    records = result.limitations["pictureGroups"]
    assert isinstance(records, list)
    return [dict(record) for record in records]


def readings(result: RTFExtraction) -> list[ExtractedSegment]:
    return [item for item in result.segments if item.extraction_method == "ocr"]


def outcomes(result: RTFExtraction) -> list[object]:
    return [record["ocrOutcome"] for record in pictures(result)]


def test_a_picture_payload_is_recognised_and_anchored_to_the_group_holding_it(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The reading is a segment of its own, published after the body text the
    reader extracted, and anchored to the group's own story, its stable path
    among its parent's groups, and the truthful byte interval its braces
    delimit -- plus the raster's own frame and line geometry. Nothing persisted
    holds a byte of the image."""
    member = real_png()
    content = source(pict(hex_payload(member)))
    engine = RecordingOcr("INVOICE", "7")

    result = examined(content, examining, engine)

    assert [item.semantic_role for item in result.segments] == ["body", "body"]
    assert result.segments[0].text == "Body"
    reading = readings(result)[0]
    assert reading.text == "INVOICE 7"
    assert reading.language == "eng"
    locator = dict(reading.source_locator)
    assert locator["captureId"] == CAPTURE
    assert locator["story"] == "body"
    assert locator["groupPath"] == "/1/1"
    # The interval is the group's own braces: the source really does open a
    # ``{`` there and close it there, and the payload sits between them.
    start, end = int(locator["byteStart"]), int(locator["byteEnd"])
    assert content[start] == 0x7B
    assert content[end - 1] == 0x7D
    assert content[start : start + 6] == b"{\\pict"
    assert locator["imagePart"] == f"pict:{start}-{end}"
    assert locator["space"] == GEOMETRY_SPACE
    assert (locator["sourceWidth"], locator["sourceHeight"]) == SIZE
    assert locator["frame"] == 1
    assert locator["line"] == 1

    [record] = pictures(result)
    assert record == {
        "unit": "rtf_picture",
        "story": "body",
        "groupPath": "/1/1",
        "byteStart": start,
        "byteEnd": end,
        "declaredType": "pngblip",
        "mediaType": "image/png",
        "hexDigits": len(member) * 2,
        "binaryBytes": 0,
        "imageDecoded": True,
        "ocrPerformed": True,
        "ocrOutcome": EMBEDDED_RECOGNISED,
    }
    assert result.limitations["embeddedImages"] == 1
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 1}
    # The structural scan is unchanged beside it: the group is still inventoried
    # as the picture destination it has always been.
    assert result.limitations["inventory"] == {"picture": 1}
    assert result.failed_units == 0
    assert result.limit_breach is None
    serialised = json.dumps(
        [
            [dict(item.source_locator), item.text, item.structured_value]
            for item in result.segments
        ]
        + pictures(result)
    )
    assert member.hex()[:64] not in serialised


def test_a_jpeg_payload_declared_as_one_is_recognised_too(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """``\\jpegblip`` is the second accepted declaration, read through the same
    path with the same magic check."""
    result = examined(
        source(pict(hex_payload(real_jpeg()), declared=b"\\jpegblip")),
        examining,
        RecordingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert pictures(result)[0]["mediaType"] == "image/jpeg"


def test_a_length_delimited_binary_payload_is_the_picture_the_group_delimits(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """``\\binN`` hands the scanner a run of raw bytes whose braces and
    backslashes are data. The payload the image path receives is exactly that
    run, and the scan's own binary counters still describe it."""
    member = real_png()
    assert b"}" in member or b"\\" in member
    content = source(pict(binary_payload(member)))

    result = examined(content, examining, RecordingOcr())

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    record = pictures(result)[0]
    assert (record["hexDigits"], record["binaryBytes"]) == (0, len(member))
    assert result.limitations["binarySections"] == 1
    assert result.limitations["binaryBytes"] == len(member)


def test_a_sub_destination_beside_the_payload_is_not_part_of_it(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """``{\\*\\blipuid ...}`` is hex too, and it is a group of its own: its
    digits belong to it and never to the picture that encloses it."""
    member = real_png()
    inner = b"{\\*\\blipuid 0123456789abcdef0123456789abcdef}"
    content = source(pict(inner + hex_payload(member)))

    result = examined(content, examining, RecordingOcr())

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert pictures(result)[0]["hexDigits"] == len(member) * 2


def test_a_profile_that_examines_no_embedded_image_opens_no_payload(
    reader: list[tuple[Any, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is structural: handed no settings, the group is inventoried
    and its payload is neither kept nor decoded, and the source is still a
    success because it asked for no coverage."""
    decoder = CountingPillow(monkeypatch)

    result = examined(source(pict(hex_payload(real_png()))))

    assert outcomes(result) == [EMBEDDED_DISABLED]
    assert decoder.opened == []
    assert pictures(result)[0]["imageDecoded"] is False
    assert pictures(result)[0]["ocrPerformed"] is False
    assert readings(result) == []
    assert result.failed_units == 0
    assert result.limit_breach is None


def test_a_profile_whose_models_were_not_served_reads_nothing_and_says_so(
    reader: list[tuple[Any, dict[str, Any]]],
    unavailable: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable resources are never handed to the engine, and the payload is
    not opened to find that out: the refusal is a property of the settings."""
    decoder = CountingPillow(monkeypatch)

    result = examined(source(pict(hex_payload(real_png()))), unavailable)

    assert outcomes(result) == [EMBEDDED_RESOURCES_UNAVAILABLE]
    assert decoder.opened == []
    assert readings(result) == []


@pytest.mark.parametrize(
    ("payload", "declared", "why"),
    [
        (b"7d7g7h", b"\\pngblip", "a byte that is no hex digit"),
        (b"7d7d7", b"\\pngblip", "an odd number of digits"),
        (b"7d7d\\{7d", b"\\pngblip", "an escaped delimiter inside the payload"),
        (b"7d7d\\'417d", b"\\pngblip", "a hex escape inside the payload"),
    ],
)
def test_a_payload_that_is_not_the_payload_it_claims_is_malformed(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
    payload: bytes,
    declared: bytes,
    why: str,
) -> None:
    """Each of these is a lexical fault the scanner is authoritative for, so it
    is settled before any decode and reported as this picture's own outcome.
    The body text beside it is published unchanged and the source is partial
    rather than refused."""
    result = examined(source(pict(payload, declared=declared)), examining)

    assert outcomes(result) == [EMBEDDED_MALFORMED], why
    assert result.failed_units == 1
    assert [item.text for item in result.segments] == ["Body"]


def test_a_group_writing_both_payload_forms_is_malformed_rather_than_resolved(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """There is no reading that says which of the two is the image, so neither
    is chosen."""
    member = real_png()
    result = examined(
        source(pict(binary_payload(member) + hex_payload(member))), examining
    )

    assert outcomes(result) == [EMBEDDED_MALFORMED]
    record = pictures(result)[0]
    assert record["hexDigits"] and record["binaryBytes"]


def test_bytes_that_are_not_the_declared_container_are_refused_by_the_decoder(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A payload whose magic disagrees with the type the group declared is
    refused: the declaration never wins over the actual container."""
    result = examined(
        source(pict(hex_payload(real_jpeg()), declared=b"\\pngblip")),
        examining,
        RefusingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_MALFORMED]
    assert result.failed_units == 1


def test_a_payload_that_is_not_an_image_at_all_is_refused(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    result = examined(
        source(pict(hex_payload(b"\x89PNG\r\n\x1a\nnot really an image"))),
        examining,
        RefusingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_MALFORMED]


@pytest.mark.parametrize(
    "declared", [b"\\wmetafile8", b"\\emfblip", b"\\dibitmap", b"\\macpict", b""]
)
def test_a_type_outside_the_accepted_raster_set_is_refused_as_declared(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
    declared: bytes,
) -> None:
    """A metafile, a device-independent bitmap, a Mac picture and a group that
    declares no type at all are all outside the accepted set. The source stays
    a success: an unsupported type is coverage the profile never promised."""
    result = examined(
        source(pict(hex_payload(real_png()), declared=declared)),
        examining,
        RefusingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_UNSUPPORTED]
    assert pictures(result)[0]["imageDecoded"] is False
    assert result.failed_units == 0
    assert result.limit_breach is None


def test_a_picture_group_holding_no_payload_embeds_no_image(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A group with a type and a size but no bytes has nothing to open, which is
    a different reading from a payload that could not be decoded."""
    result = examined(source(pict(b"")), examining, RefusingOcr())

    assert outcomes(result) == [EMBEDDED_NOT_EMBEDDED]
    assert result.failed_units == 0


def test_a_raster_carrying_no_reliable_text_is_read_and_reported_as_such(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A blank raster is examined -- the bytes are decoded and the engine runs --
    and that it read nothing reliable is a reading of it, not a failure."""
    blank = io.BytesIO()
    Image.new("RGB", SIZE, "white").save(blank, format="PNG")

    result = examined(
        source(pict(hex_payload(blank.getvalue()))),
        examining,
        RecordingOcr(confidence=1.0),
    )

    assert outcomes(result) == [EMBEDDED_NO_TEXT]
    record = pictures(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (True, True)
    assert result.failed_units == 0


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (
            b"{\\object\\objemb{\\*\\objclass Excel.Sheet}"
            b"{\\*\\objdata " + hex_payload(real_png()) + b"}"
            b"{\\result " + pict(hex_payload(real_png())) + b"}}",
            "an embedded OLE object, its payload and its presentation picture",
        ),
        (
            b"{\\object\\objlink{\\*\\objclass Word.Document}"
            b"{\\*\\objdata " + hex_payload(real_png()) + b"}}",
            "a linked object",
        ),
        (
            b'{\\field{\\*\\fldinst INCLUDEPICTURE "/etc/logo.png" \\\\d}'
            b"{\\fldrslt " + pict(hex_payload(real_png())) + b"}}",
            "an INCLUDEPICTURE field and its cached result",
        ),
        (
            (
                b"{\\field{\\*\\fldinst INCLUDEPICTURE "
                b'"https://example.invalid/logo.png"}{\\fldrslt }}'
            ),
            "an INCLUDEPICTURE field naming a URL",
        ),
    ],
)
def test_an_object_payload_or_a_field_is_never_a_candidate(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    why: str,
) -> None:
    """None of these is executed, resolved or decoded as an image, under a
    profile that examines embedded images and with the engine wired to fail if
    it is called. The inventory still states that they are present."""
    decoder = CountingPillow(monkeypatch)

    result = examined(source(body), examining, RefusingOcr())

    assert pictures(result) == [], why
    assert result.limitations["embeddedImages"] == 0
    assert decoder.opened == []
    assert result.failed_units == 0
    inventory = result.limitations["inventory"]
    assert isinstance(inventory, dict)
    assert set(inventory) & {"object", "object_data", "field_instruction", "picture"}


def test_a_field_risk_keyword_is_still_named_without_its_operand(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The scan's existing reading of a field is unchanged by the image pass:
    the keyword is classified and the path after it is not read out of the
    bytes, let alone resolved."""
    result = examined(
        source(
            b'{\\field{\\*\\fldinst INCLUDEPICTURE "/private/secret.png"}{\\fldrslt }}'
        ),
        examining,
        RefusingOcr(),
    )

    assert result.limitations["fieldRisks"] == {"INCLUDEPICTURE": 1}
    assert "secret" not in json.dumps(dict(result.limitations))


def test_each_occurrence_of_one_image_is_its_own_candidate_and_its_own_locator(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Two groups holding the same bytes are two occurrences: there is no cache
    keyed by the payload, each pays the aggregate ceiling again, and the two
    locators differ by the interval and the path the source really gave them."""
    member = real_png()
    payload = hex_payload(member)
    engine = RecordingOcr()

    result = examined(
        source(pict(payload), b"{\\header " + pict(payload) + b"}"),
        examining,
        engine,
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED, EMBEDDED_RECOGNISED]
    assert len(engine.sizes) == 2
    first, second = pictures(result)
    assert first["story"] == "body"
    # The second sits inside the header destination, which is the story its
    # reading is traced to.
    assert second["story"] == "header"
    assert first["groupPath"] != second["groupPath"]
    assert (first["byteStart"], first["byteEnd"]) != (
        second["byteStart"],
        second["byteEnd"],
    )
    assert [dict(item.source_locator)["groupPath"] for item in readings(result)] == [
        first["groupPath"],
        second["groupPath"],
    ]


def test_one_aggregate_ocr_ceiling_is_shared_by_every_picture_of_one_source(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is the source's and not one picture's. The first picture
    spends it; the second asks for more than is left and is cut short; the third
    is never opened at all, so the breach bounds the work done and not merely
    what is published."""
    decoder = CountingPillow(monkeypatch)
    payload = hex_payload(real_png())
    engine = RecordingOcr()
    budget = ExtractionBudget(ocr_pixels=SIZE[0] * SIZE[1])

    result = examined(
        source(pict(payload), pict(payload), pict(payload)),
        examining,
        engine,
        budget,
    )

    assert outcomes(result) == [
        EMBEDDED_RECOGNISED,
        EMBEDDED_TRUNCATED,
        EMBEDDED_TRUNCATED,
    ]
    # The second payload is opened -- the breach is only set once it asks for
    # the raster -- and the third is refused before its bytes are read.
    assert len(decoder.opened) == 2
    assert len(engine.sizes) == 1
    assert result.limit_breach == "ocr_pixel_budget_exhausted"
    assert result.limitations["limitBreach"] == "ocr_pixel_budget_exhausted"


def test_the_decoded_pixel_ceiling_is_aggregate_too(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source pixels are charged per admitted frame against one ceiling, so a
    source of many individually acceptable rasters is bounded in aggregate."""
    decoder = CountingPillow(monkeypatch)
    payload = hex_payload(real_png())
    budget = ExtractionBudget(decoded_pixels=SIZE[0] * SIZE[1])

    result = examined(
        source(pict(payload), pict(payload), pict(payload)),
        examining,
        RecordingOcr(),
        budget,
    )

    assert outcomes(result)[0] == EMBEDDED_RECOGNISED
    assert outcomes(result)[1:] == [EMBEDDED_TRUNCATED, EMBEDDED_TRUNCATED]
    assert len(decoder.opened) == 2
    assert result.limit_breach == "decoded_pixel_budget_exhausted"


def test_the_picture_record_list_is_bounded_while_the_counts_stay_exact(
    reader: list[tuple[Any, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-picture detail is capped so the output cannot grow with whatever
    a source chose to hold, and the exact count is reported beside it."""
    monkeypatch.setattr(rtf_extractor, "MAX_PICTURE_RECORDS", 3)
    groups = [pict(b"") for _ in range(5)]

    result = examined(source(*groups))

    assert len(pictures(result)) == 3
    assert result.limitations["embeddedImages"] == 5
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_NOT_EMBEDDED: 5}
    assert MAX_PICTURE_RECORDS == 256


def test_the_scanner_still_refuses_a_malformed_source_before_the_reader_runs(
    reader: list[tuple[Any, dict[str, Any]]],
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A picture group does not weaken the lexical refusals around it: a source
    whose groups never close is still refused, the payload of the group left
    open is never read, and the reader never runs."""
    unclosed = pict(hex_payload(real_png()))[:-1]
    with pytest.raises(rtf_extractor.RTFExtractionInvalid, match="unbalanced"):
        examined(b"{\\rtf1\\ansi " + unclosed, examining, RefusingOcr())

    assert reader == []
