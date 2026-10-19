"""PPTX embedded rasters: the one bounded pass that reads a slide picture.

The structural slide reader never opens an image member. This exercises the
pass that does: the candidate seam out of the validated package, the aggregate
budget shared by every picture of one presentation, and the per-picture truth
the inventory record reports when nothing was read.

Only a validated raster a slide's own ``p:pic`` directly displays out of the
package is a candidate. A link, an external relationship, the audio or video a
picture references, an embedded object, a chart, a diagram, a table and every
non-picture shape are all inert here, and the engine is injected throughout, so
what is asserted is this adapter's behaviour rather than whatever a host's
Tesseract reads out of a fixture.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from _trusted_ocr import catalogue_over, stand_in_models
from omnivia_core_runtime.service import ocr_resources, ooxml_extractors
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLExtraction,
    extract_pptx,
)
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
from PIL import Image, ImageDraw

CAPTURE = "dcap-" + "a" * 64
PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
IMAGE_TYPE = REL_NS + "/image"
AUDIO_TYPE = REL_NS + "/audio"
SLIDE_PART = "ppt/slides/slide1.xml"
IMAGE_PART = "ppt/media/image1.png"
AUDIO_PART = "ppt/media/media1.m4a"
EXTERNAL_IMAGE = "https://example.invalid/brand/logo.png"
SIZE = (120, 40)


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


def pic(
    shape_id: str = "2",
    *,
    reference: str = ' r:embed="rIdI"',
    media: str = "",
    title: str = "Logo",
) -> str:
    """One ``p:pic``, with the claims its author wrote and the image it names."""
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{shape_id}" name="Picture {shape_id}"'
        f' title="{title}"/><p:cNvPicPr/><p:nvPr>{media}</p:nvPr></p:nvPicPr>'
        f"<p:blipFill><a:blip{reference}/><a:stretch/></p:blipFill>"
        "<p:spPr/></p:pic>"
    )


def group(shape_id: str, children: str) -> str:
    return (
        f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{shape_id}"/></p:nvGrpSpPr><p:grpSpPr/>'
        f"{children}</p:grpSp>"
    )


# A shape holding text, a connector and a graphic frame holding a table: none of
# them displays a raster, so none is ever a candidate however many image members
# the package holds. The shape's own text is the structural extraction beside
# whatever the image pass did or did not read.
SP = (
    '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Title 3"/><p:cNvSpPr/><p:nvPr/>'
    "</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>"
    "<a:p><a:r><a:t>Quarterly review</a:t></a:r></a:p></p:txBody></p:sp>"
)
CXN = (
    '<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="4" name="Connector 4"/>'
    "<p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr/></p:cxnSp>"
)
FRAME = (
    '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="Table 5"/>'
    "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/'
    'drawingml/2006/table"><a:tbl><a:tblGrid><a:gridCol w="100"/></a:tblGrid>'
    "<a:tr h=\"50\"><a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>Cell</a:t></a:r>"
    "</a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic>"
    "</p:graphicFrame>"
)
AUDIO = f'<a:audioFile xmlns:a="{DRAWING_NS}" xmlns:r="{REL_NS}" r:link="rIdA"/>'
EMBED_RELS = (
    f'<Relationship Id="rIdI" Target="../media/image1.png" Type="{IMAGE_TYPE}"/>'
)
LINK_RELS = (
    f'<Relationship Id="rIdL" Target="{EXTERNAL_IMAGE}" TargetMode="External"'
    f' Type="{IMAGE_TYPE}"/>'
)
AUDIO_RELS = f'<Relationship Id="rIdA" Target="../media/media1.m4a" Type="{AUDIO_TYPE}"/>'


def pptx(
    tree: str | None = None,
    *,
    hidden: bool = False,
    rels: str = EMBED_RELS,
    members: dict[str, bytes] | None = None,
    content_types: str = '<Default Extension="png" ContentType="image/png"/>',
) -> bytes:
    """One slide holding ``tree``, relating one image part.

    The text shape is there so the structural extraction is visibly published
    beside whatever the image pass did or did not read.
    """
    tree = SP + pic() if tree is None else tree
    shown = ' show="0"' if hidden else ""
    parts: dict[str, bytes] = {
        "[Content_Types].xml": f"<Types>{content_types}</Types>".encode(),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{PML}" xmlns:r="{REL_NS}"><p:sldIdLst>'
            f'<p:sldId id="256" r:id="rId1"{shown}/>'
            "</p:sldIdLst></p:presentation>"
        ).encode(),
        "ppt/_rels/presentation.xml.rels": (
            b'<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/>'
            b"</Relationships>"
        ),
        SLIDE_PART: (
            f'<p:sld xmlns:p="{PML}" xmlns:a="{DRAWING_NS}" xmlns:r="{REL_NS}">'
            f"<p:cSld><p:spTree>{tree}</p:spTree></p:cSld></p:sld>"
        ).encode(),
        "ppt/slides/_rels/slide1.xml.rels": (
            f"<Relationships>{rels}</Relationships>"
        ).encode(),
        **(members if members is not None else {IMAGE_PART: real_png()}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every package member whose content the reader actually asked for.

    ``_Package.bytes`` is the one seam a member's content comes out of, so a
    candidate that was never opened never appears here. The archive itself is no
    witness: validation streams every member to its end against its directory
    entry before any part is handed out.
    """
    read: list[str] = []
    original = ooxml_extractors._Package.bytes

    def spy(package: object, name: str, **kwargs: object) -> bytes:
        read.append(name)
        return original(package, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ooxml_extractors._Package, "bytes", spy)
    return read


@pytest.fixture
def examining(tmp_path: Path) -> ocr_resources.ExtractionSettings:
    """Settings whose profile examines embedded images and whose models verify."""
    stand_in_models(tmp_path, "eng")
    return ocr_resources.ExtractionSettings(
        processing_mode=ocr_resources.MODE_TEXT_AND_LOCAL_OCR,
        ocr=catalogue_over(tmp_path, "eng").resolve(("eng",)),
        embedded_image_ocr=True,
    )


def examined(
    content: bytes,
    settings: ocr_resources.ExtractionSettings | None,
    engine: object,
    budget: ExtractionBudget | None = None,
) -> OOXMLExtraction:
    return extract_pptx(
        content,
        capture_id=CAPTURE,
        settings=settings,
        ocr_engine=engine,  # type: ignore[arg-type]
        budget=budget,
    )


def records(result: OOXMLExtraction) -> list[dict[str, object]]:
    """Every picture inventory record, in the order it was reported."""
    return [
        dict(item.structured_value or {})
        for item in result.segments
        if (item.structured_value or {}).get("kind") == "picture"
    ]


def readings(result: OOXMLExtraction) -> list[ExtractedSegment]:
    return [item for item in result.segments if item.extraction_method == "ocr"]


def test_an_embedded_raster_is_recognised_and_anchored_to_the_picture_showing_it(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The reading is a segment of its own, published after that picture's own
    inventory record and the alt text its author wrote, and anchored to the
    record's whole route -- the slide, its display ordinal, its visibility, the
    shape's own identifier and the groups above it -- plus the image part the
    bytes came from and the raster's own frame and line geometry. It is never
    joined to the slide's prose and nothing persisted holds a byte of the
    image."""
    engine = RecordingOcr()

    result = examined(pptx(), examining, engine)

    assert [
        (item.semantic_role, item.extraction_method, item.text)
        for item in result.segments
    ] == [
        ("body", "direct", "Quarterly review"),
        ("object_inventory", "inventory", None),
        ("image_alt", "direct", "Logo"),
        ("body", "ocr", "INVOICE 7"),
    ]
    reading = result.segments[3]
    assert reading.language == "eng"
    assert reading.confidence == pytest.approx(0.91)
    locator = reading.source_locator
    # The whole structural route the inventory record carries, unchanged.
    assert {key: locator[key] for key in result.segments[1].source_locator} == dict(
        result.segments[1].source_locator
    )
    assert (locator["part"], locator["slideId"], locator["slide"]) == (
        SLIDE_PART,
        "256",
        1,
    )
    assert locator["slideHidden"] is False
    assert (locator["shapeId"], locator["shapeKind"]) == ("2", "pic")
    assert (locator["groupPath"], locator["zPath"]) == ([], [1])
    # And the image the bytes came from, with the frame's own geometry.
    assert locator["imagePart"] == IMAGE_PART
    assert (locator["unit"], locator["frame"], locator["line"]) == ("image_frame", 1, 1)
    assert locator["space"] == GEOMETRY_SPACE
    assert (locator["sourceWidth"], locator["sourceHeight"]) == SIZE
    assert locator["ocrEngine"] == "tesseract-5.5.3"
    assert locator["ocrLanguages"] == ["eng"]
    assert locator["box"] == [4.0, 10.0, 58.0, 12.0]
    assert engine.sizes == [SIZE]
    for item in result.segments:
        assert not isinstance(item.structured_value, bytes)
        assert not any(
            isinstance(value, bytes) for value in item.source_locator.values()
        )

    record = records(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (True, True)
    assert record["ocrOutcome"] == EMBEDDED_RECOGNISED
    # The author's claims and the inert image inventory are untouched beside it.
    assert (record["shapeName"], record["altTitle"]) == ("Picture 2", "Logo")
    assert record["imagePart"] == IMAGE_PART
    assert result.limitations["embeddedImages"] == 1
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 1}
    assert (result.failed_units, result.limit_breach) == (0, None)


@pytest.mark.parametrize("off", [True, False], ids=["disabled", "no-settings"])
def test_a_profile_that_examines_no_embedded_image_opens_no_image_member(
    examining: ocr_resources.ExtractionSettings, opened: list[str], off: bool
) -> None:
    """The default is structural and the preference alone decides otherwise. The
    member here is a real PNG, so only the profile keeps it shut: no image
    member's bytes are read at all, the engine is never reached, and the record
    says the image was left unexamined rather than that it held nothing."""
    settings = replace(examining, embedded_image_ocr=False) if off else None

    result = examined(pptx(), settings, RefusingOcr())

    assert IMAGE_PART not in opened
    assert readings(result) == []
    record = records(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_DISABLED
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_DISABLED: 1}
    # Disabled recognition is not lost coverage: the profile asked for none.
    assert (result.failed_units, result.limit_breach) == (0, None)
    # The structural extraction is exactly what it always was.
    assert [item.semantic_role for item in result.segments] == [
        "body",
        "object_inventory",
        "image_alt",
    ]


def test_a_profile_whose_models_were_not_served_examines_nothing_and_says_so(
    examining: ocr_resources.ExtractionSettings, opened: list[str]
) -> None:
    """An unavailable selection keeps its identity, and the picture stays
    unexamined for the reason it really was: the pinned models were never
    served, which is not the preference being off."""
    unserved = replace(
        examining, ocr=ocr_resources.TrustedOcrCatalogue().resolve(("deu",))
    )

    result = examined(pptx(), unserved, RefusingOcr())

    assert IMAGE_PART not in opened
    record = records(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_RESOURCES_UNAVAILABLE
    assert (result.failed_units, result.limit_breach) == (0, None)


def test_a_linked_image_is_inventory_and_is_never_opened(
    examining: ocr_resources.ExtractionSettings, opened: list[str]
) -> None:
    """A picture naming an external reference embeds nothing, so there is no
    candidate: the reference is neither resolved nor fetched, the member the
    package happens to hold under another name is never opened, and the engine
    is never reached. The link stays in the record exactly as written."""
    result = examined(
        pptx(pic(reference=' r:link="rIdL"'), rels=LINK_RELS),
        examining,
        RefusingOcr(),
    )

    assert IMAGE_PART not in opened
    assert readings(result) == []
    record = records(result)[0]
    assert record["imageLinkTarget"] == EXTERNAL_IMAGE
    assert record["imageLinkExternal"] is True
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_NOT_EMBEDDED
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_NOT_EMBEDDED: 1}


def test_nothing_but_a_picture_is_ever_a_candidate(
    examining: ocr_resources.ExtractionSettings, opened: list[str]
) -> None:
    """A shape, a connector and a graphic frame display no raster, so none of
    them is handed to the pass however many image members the package holds and
    whatever the slide relates. A chart, a diagram and an embedded object reach
    their own routes before the picture one and are never candidates either. The
    presentation reports no embedded image at all, the image member the slide
    still relates is never opened, and the structural output is unchanged."""
    result = examined(pptx(SP + CXN + FRAME), examining, RefusingOcr())

    assert IMAGE_PART not in opened
    assert [item.semantic_role for item in result.segments] == ["body", "table_cell"]
    assert records(result) == []
    assert result.limitations["embeddedImages"] == 0
    assert result.limitations["embeddedImageOutcomes"] == {}


def test_the_media_a_picture_references_is_no_raster_candidate(
    examining: ocr_resources.ExtractionSettings, opened: list[str]
) -> None:
    """A picture that references audio displays a raster and references a media
    part, and only the raster is a candidate: the media part is inventoried
    beside the picture and never opened, decoded or recognised, and the
    presentation counts one embedded image rather than two."""
    result = examined(
        pptx(
            pic(media=AUDIO),
            rels=EMBED_RELS + AUDIO_RELS,
            members={IMAGE_PART: real_png(), AUDIO_PART: b"not audio either"},
            content_types=(
                '<Default Extension="png" ContentType="image/png"/>'
                '<Default Extension="m4a" ContentType="audio/mp4"/>'
            ),
        ),
        examining,
        RecordingOcr("MEDIA"),
    )

    assert AUDIO_PART not in opened
    assert opened.count(IMAGE_PART) == 1
    media = [
        item.structured_value
        for item in result.segments
        if (item.structured_value or {}).get("kind") == "pictureMedia"
    ]
    assert len(media) == 1
    assert media[0] is not None
    assert media[0]["mediaPart"] == AUDIO_PART
    assert not any(key.startswith("ocr") for key in media[0])
    assert result.limitations["embeddedImages"] == 1
    assert [item.text for item in readings(result)] == ["MEDIA"]


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(b"\x89PNG\r\n\x1a\nnot really an image", id="malformed"),
        pytest.param(real_jpeg(), id="mismatched-magic"),
    ],
)
def test_a_malformed_or_mismatched_raster_keeps_its_record_and_is_counted_failed(
    examining: ocr_resources.ExtractionSettings, member: bytes
) -> None:
    """Bytes the decoder refuses -- truncated, or a JPEG behind a ``.png``
    declaration -- are this picture's outcome and never the package's: the
    inventory record stands, states that nothing was decoded, names the refusal,
    and the slide's own text beside it is published unchanged."""
    result = examined(pptx(members={IMAGE_PART: member}), examining, RecordingOcr())

    assert [item.semantic_role for item in result.segments] == [
        "body",
        "object_inventory",
        "image_alt",
    ]
    record = records(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_MALFORMED
    assert record["imageBytes"] == len(member)
    assert result.failed_units == 1
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_MALFORMED: 1}


def test_an_image_type_outside_the_accepted_set_is_named_before_any_decode(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A GIF is a legitimate image part and is not in the accepted raster set,
    so its declared type settles the matter before any decode is attempted and
    the engine is never reached. EMF, WMF and SVG are refused the same way, and
    the picture is not counted failed: no coverage was lost to a fault, the
    subtype simply is not one this profile reads."""
    gif = "ppt/media/image1.gif"

    result = examined(
        pptx(
            rels=EMBED_RELS.replace("image1.png", "image1.gif"),
            members={gif: b"GIF89a" + b"\x00" * 16},
            content_types='<Default Extension="gif" ContentType="image/gif"/>',
        ),
        examining,
        RefusingOcr(),
    )

    record = records(result)[0]
    assert record["imageContentType"] == "image/gif"
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_UNSUPPORTED
    assert result.failed_units == 0
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_UNSUPPORTED: 1}


def test_two_pictures_of_one_image_part_stay_two_located_readings(
    examining: ocr_resources.ExtractionSettings, opened: list[str]
) -> None:
    """A duplicate reference is a second picture showing the same image, so each
    keeps its own shape identity and reading, and each pays the shared budget
    again. Nothing is cached: the member is read once per reference and
    discarded with it."""
    engine = RecordingOcr("TWICE")

    result = examined(pptx(pic() + pic("5")), examining, engine)

    assert [
        (item.semantic_role, item.source_locator["shapeId"])
        for item in result.segments
    ] == [
        ("object_inventory", "2"),
        ("image_alt", "2"),
        ("body", "2"),
        ("object_inventory", "5"),
        ("image_alt", "5"),
        ("body", "5"),
    ]
    # Two distinct pictures, one image part, two reads and two readings.
    assert opened.count(IMAGE_PART) == 2
    assert len(engine.sizes) == 2
    assert {item.source_locator["imagePart"] for item in readings(result)} == {
        IMAGE_PART
    }
    assert result.limitations["embeddedImages"] == 2
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 2}


def test_a_reading_inside_nested_groups_keeps_the_whole_recursive_path(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A picture's reading sits exactly where the picture does: the groups above
    it outermost first, the sibling index at every level down to it, and the
    shape's own identifier -- not the group's."""
    result = examined(
        pptx(group("5", pic("8") + group("6", pic("7")))),
        examining,
        RecordingOcr("GROUPED"),
    )

    assert [
        (
            item.source_locator["shapeId"],
            item.source_locator["groupPath"],
            item.source_locator["zPath"],
            item.source_locator["zOrder"],
        )
        for item in readings(result)
    ] == [("8", ["5"], [0, 0], 0), ("7", ["5", "6"], [0, 1, 0], 0)]
    assert [item.text for item in readings(result)] == ["GROUPED", "GROUPED"]


def test_a_hidden_slide_hides_what_was_read_on_it(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A reading is out of sight exactly where the record above it is: the
    slide's own visibility is reported in the locator and the reading takes it,
    so nothing read off a hidden slide is published as visible content."""
    result = examined(pptx(hidden=True), examining, RecordingOcr("HIDDEN"))

    assert [
        (item.semantic_role, item.visibility, item.source_locator["slideHidden"])
        for item in result.segments
    ] == [
        ("body", "hidden", True),
        ("object_inventory", "hidden", True),
        ("image_alt", "hidden", True),
        ("body", "hidden", True),
    ]
    assert result.segments[3].text == "HIDDEN"


def test_a_below_threshold_reading_is_retained_but_is_no_reliable_text(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Candidates the engine was not confident about are kept so a reviewer can
    trace the selection, in a role ordinary retrieval excludes -- and the
    picture reports that it reached no reliable text rather than some."""
    result = examined(pptx(), examining, RecordingOcr("MAYBE", confidence=9.0))

    assert [
        (item.semantic_role, item.visibility, item.text)
        for item in result.segments[1:]
    ] == [
        ("object_inventory", "visible", None),
        ("image_alt", "visible", "Logo"),
        ("hidden_content", "hidden", "MAYBE"),
    ]
    record = records(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (True, True)
    assert record["ocrOutcome"] == EMBEDDED_NO_TEXT


@pytest.mark.parametrize(
    ("budget", "breach"),
    [
        pytest.param(
            {"ocr_pixels": SIZE[0] * SIZE[1]},
            "ocr_pixel_budget_exhausted",
            id="ocr-pixels",
        ),
        pytest.param(
            {"decoded_pixels": SIZE[0] * SIZE[1]},
            "decoded_pixel_budget_exhausted",
            id="decoded-pixels",
        ),
    ],
)
def test_one_aggregate_budget_truncates_the_later_pictures_of_one_presentation(
    examining: ocr_resources.ExtractionSettings,
    opened: list[str],
    budget: dict[str, int],
    breach: str,
) -> None:
    """The ceiling is the presentation's and not one picture's or one slide's,
    and it bounds the work done rather than what is published. A budget paying
    for the first raster alone leaves the second to breach it and the third
    never opened at all: once the shared ceiling is breached, no later member's
    bytes are read. Each cut-short picture reports that it was, rather than
    being called a blank, and the presentation's own outcome says coverage was
    cut short."""
    engine = RecordingOcr("FIRST")

    result = examined(
        pptx(pic() + pic("5") + pic("9")),
        examining,
        engine,
        budget=ExtractionBudget(**budget),
    )

    # The first pays the ceiling, the second breaches it, the third is not read.
    assert opened.count(IMAGE_PART) == 2
    assert len(engine.sizes) == 1
    assert [
        (item.semantic_role, item.text, item.source_locator["shapeId"])
        for item in result.segments
    ] == [
        ("object_inventory", None, "2"),
        ("image_alt", "Logo", "2"),
        ("body", "FIRST", "2"),
        ("object_inventory", None, "5"),
        ("image_alt", "Logo", "5"),
        ("object_inventory", None, "9"),
        ("image_alt", "Logo", "9"),
    ]
    assert [record["ocrOutcome"] for record in records(result)] == [
        EMBEDDED_RECOGNISED,
        EMBEDDED_TRUNCATED,
        EMBEDDED_TRUNCATED,
    ]
    assert result.limit_breach == breach
    assert result.limitations["limitBreach"] == breach


def test_one_budget_is_shared_across_the_slides_of_one_presentation(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Two slides, one picture each, and one ceiling between them: the second
    slide's picture is cut short by what the first slide's spent, so the budget
    is the presentation's rather than being handed out per slide."""
    engine = RecordingOcr("FIRST")

    result = examined(
        two_slides(),
        examining,
        engine,
        budget=ExtractionBudget(ocr_pixels=SIZE[0] * SIZE[1]),
    )

    assert len(engine.sizes) == 1
    assert [
        (
            item.source_locator["slide"],
            (item.structured_value or {})["ocrOutcome"],
        )
        for item in result.segments
        if (item.structured_value or {}).get("kind") == "picture"
    ] == [(1, EMBEDDED_RECOGNISED), (2, EMBEDDED_TRUNCATED)]
    assert result.limit_breach == "ocr_pixel_budget_exhausted"


def two_slides() -> bytes:
    """Two slides, each showing the one image part the package holds."""
    slide = (
        f'<p:sld xmlns:p="{PML}" xmlns:a="{DRAWING_NS}" xmlns:r="{REL_NS}">'
        f"<p:cSld><p:spTree>{pic()}</p:spTree></p:cSld></p:sld>"
    ).encode()
    rels = f"<Relationships>{EMBED_RELS}</Relationships>".encode()
    parts = {
        "[Content_Types].xml": (
            b'<Types><Default Extension="png" ContentType="image/png"/></Types>'
        ),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{PML}" xmlns:r="{REL_NS}"><p:sldIdLst>'
            '<p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/>'
            "</p:sldIdLst></p:presentation>"
        ).encode(),
        "ppt/_rels/presentation.xml.rels": (
            b'<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/>'
            b'<Relationship Id="rId2" Target="slides/slide2.xml"/></Relationships>'
        ),
        SLIDE_PART: slide,
        "ppt/slides/slide2.xml": slide,
        "ppt/slides/_rels/slide1.xml.rels": rels,
        "ppt/slides/_rels/slide2.xml.rels": rels,
        IMAGE_PART: real_png(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()
