"""XLSX embedded rasters: the one bounded pass that reads a worksheet picture.

The structural workbook reader never opens an image member. This exercises the
pass that does: the candidate seam out of the validated package, the aggregate
budget shared by every picture of one workbook, and the per-picture truth the
inventory record reports when nothing was read.

Only a validated raster a worksheet drawing's ``xdr:pic`` directly displays out
of the package is a candidate. A link, an external relationship, a chart, a
shape, a graphic frame, a connector, a content part and any other member the
package happens to hold are all inert here, and the engine is injected
throughout, so what is asserted is this adapter's behaviour rather than
whatever a host's Tesseract reads out of a fixture.
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
    extract_xlsx,
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
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SHEET_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
DRAWING_CT = "application/vnd.openxmlformats-officedocument.drawing+xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
DRAWING_PART = "xl/drawings/drawing1.xml"
IMAGE_PART = "xl/media/image1.png"
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


def two_cell(body: str, column: int = 0) -> str:
    """One two-cell anchor holding ``body``, two columns by three rows."""
    return (
        "<xdr:twoCellAnchor>"
        f"<xdr:from><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff>"
        "<xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{column + 2}</xdr:col><xdr:colOff>0</xdr:colOff>"
        "<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        f"{body}<xdr:clientData/></xdr:twoCellAnchor>"
    )


def pic(reference: str = '<a:blip r:embed="rIdI"/>', shape_id: str = "2") -> str:
    return (
        "<xdr:pic>"
        f'<xdr:nvPicPr><xdr:cNvPr id="{shape_id}" name="Picture {shape_id}"'
        ' title="Logo"/><xdr:cNvPicPr/></xdr:nvPicPr>'
        f"<xdr:blipFill>{reference}<a:stretch/></xdr:blipFill>"
        "<xdr:spPr/>"
        "</xdr:pic>"
    )


# A shape holding text, a connector and a content part: none of them displays a
# raster, so none is ever a candidate however many image members the package
# holds. The shape's own text is read by nothing here.
SP = (
    "<xdr:sp>"
    '<xdr:nvSpPr><xdr:cNvPr id="3" name="Rounded Rectangle 3"/>'
    "<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr/>"
    "<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>Not read here</a:t></a:r></a:p>"
    "</xdr:txBody>"
    "</xdr:sp>"
)
CXN = (
    "<xdr:cxnSp>"
    '<xdr:nvCxnSpPr><xdr:cNvPr id="4" name="Straight Arrow Connector 4"/>'
    '<xdr:cNvCxnSpPr><a:stCxn id="3" idx="2"/></xdr:cNvCxnSpPr></xdr:nvCxnSpPr>'
    "<xdr:spPr/>"
    "</xdr:cxnSp>"
)
CONTENT = '<xdr:contentPart r:id="rIdNowhere"/>'
EMBED_RELS = (
    "<Relationships>"
    f'<Relationship Id="rIdI" Target="../media/image1.png" Type="{RELNS}/image"/>'
    "</Relationships>"
)
LINK_RELS = (
    "<Relationships>"
    f'<Relationship Id="rIdL" Target="{EXTERNAL_IMAGE}" TargetMode="External"'
    f' Type="{RELNS}/image"/>'
    "</Relationships>"
)


def wsDr(anchors: str) -> str:
    return (
        f'<xdr:wsDr xmlns:xdr="{SHEET_DRAWING_NS}" xmlns:a="{DRAWING_NS}"'
        f' xmlns:r="{RELNS}">{anchors}</xdr:wsDr>'
    )


def xlsx(
    drawing: str = "",
    *,
    rels: str = EMBED_RELS,
    members: dict[str, bytes] | None = None,
    extension: str = "png",
    content_type: str = "image/png",
    state: str = "",
) -> bytes:
    """One sheet with one populated cell, relating one drawing.

    The cell is there so the structural extraction is visibly published beside
    whatever the image pass did or did not read.
    """
    drawing = drawing or wsDr(two_cell(pic()))
    parts: dict[str, bytes] = {
        "[Content_Types].xml": (
            "<Types>"
            f'<Default Extension="{extension}" ContentType="{content_type}"/>'
            f'<Override PartName="/{DRAWING_PART}" ContentType="{DRAWING_CT}"/>'
            "</Types>"
        ).encode(),
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
            f'<sheet name="Sales" sheetId="7" r:id="rId1"{state}/>'
            "</sheets></workbook>"
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            "</Relationships>"
        ).encode(),
        SHEET_PART: (
            f'<worksheet xmlns="{MAIN}" xmlns:r="{RELNS}"><sheetData>'
            '<row r="1"><c r="A1" t="str"><v>Totals</v></c></row>'
            '</sheetData><drawing r:id="rIdD"/></worksheet>'
        ).encode(),
        "xl/worksheets/_rels/sheet1.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rIdD" Target="../drawings/drawing1.xml"'
            f' Type="{RELNS}/drawing"/>'
            "</Relationships>"
        ).encode(),
        DRAWING_PART: drawing.encode(),
        "xl/drawings/_rels/drawing1.xml.rels": rels.encode(),
        **(members if members is not None else {IMAGE_PART: real_png()}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
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
    return extract_xlsx(
        content,
        capture_id=CAPTURE,
        settings=settings,
        ocr_engine=engine,  # type: ignore[arg-type]
        budget=budget,
    )


def records(result: OOXMLExtraction) -> list[dict[str, object]]:
    """Every drawing-object inventory record, in the order it was reported."""
    return [
        dict(item.structured_value or {})
        for item in result.segments
        if item.semantic_role == "object_inventory"
        and "drawingObjectType" in item.source_locator
    ]


def readings(result: OOXMLExtraction) -> list[ExtractedSegment]:
    return [item for item in result.segments if item.extraction_method == "ocr"]


def test_an_embedded_raster_is_recognised_and_anchored_to_the_picture_showing_it(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The reading is a segment of its own, published after that picture's own
    inventory record, and anchored to the record's whole route -- the sheet, the
    drawing, the anchor and the object -- plus the image part the bytes came
    from and the raster's own frame and line geometry. It is never joined to a
    cell and nothing persisted holds a byte of the image."""
    engine = RecordingOcr()

    result = examined(xlsx(), examining, engine)

    assert [
        (item.semantic_role, item.extraction_method, item.text)
        for item in result.segments
    ] == [
        ("table_cell", "direct", None),
        ("object_inventory", "inventory", None),
        ("body", "ocr", "INVOICE 7"),
    ]
    reading = result.segments[2]
    assert reading.language == "eng"
    assert reading.confidence == pytest.approx(0.91)
    locator = reading.source_locator
    # The whole structural route the inventory record carries, unchanged.
    assert {key: locator[key] for key in result.segments[1].source_locator} == dict(
        result.segments[1].source_locator
    )
    assert (locator["sheetPart"], locator["sheet"], locator["sheetId"]) == (
        SHEET_PART,
        "Sales",
        "7",
    )
    assert (locator["drawingPart"], locator["drawingRelationshipId"]) == (
        DRAWING_PART,
        "rIdD",
    )
    assert (locator["anchorIndex"], locator["anchorRange"]) == (0, "A1:C4")
    assert (locator["drawingObjectIndex"], locator["drawingObjectType"]) == (0, "pic")
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

    result = examined(xlsx(), settings, RefusingOcr())

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
        "table_cell",
        "object_inventory",
    ]


def test_a_profile_whose_models_were_not_served_examines_nothing_and_says_so(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """An unavailable selection keeps its identity, and the picture stays
    unexamined for the reason it really was: the pinned models were never
    served, which is not the preference being off."""
    unserved = replace(
        examining, ocr=ocr_resources.TrustedOcrCatalogue().resolve(("deu",))
    )

    result = examined(xlsx(), unserved, RefusingOcr())

    record = records(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_RESOURCES_UNAVAILABLE
    assert (result.failed_units, result.limit_breach) == (0, None)


def test_a_linked_image_is_inventory_and_is_never_opened(
    examining: ocr_resources.ExtractionSettings, opened: list[str]
) -> None:
    """A picture naming an external reference embeds nothing, so there is no
    candidate: the reference is neither resolved nor fetched, the member the
    package happens to hold under the same name is never opened, and the engine
    is never reached. The link stays in the record exactly as written."""
    result = examined(
        xlsx(wsDr(two_cell(pic('<a:blip r:link="rIdL"/>'))), rels=LINK_RELS),
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
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A shape, a connector and a content part display no raster, so none of
    them is handed to the pass however many image members the package holds and
    whatever reference a content part encodes. The workbook reports no embedded
    image at all."""
    drawing = wsDr(
        "".join(
            two_cell(body, column=index * 2)
            for index, body in enumerate((SP, CXN, CONTENT))
        )
    )

    result = examined(xlsx(drawing), examining, RefusingOcr())

    assert [record["shapeName"] for record in records(result)] == [
        "Rounded Rectangle 3",
        "Straight Arrow Connector 4",
        None,
    ]
    assert not any("ocrOutcome" in record for record in records(result))
    assert result.limitations["embeddedImages"] == 0
    assert result.limitations["embeddedImageOutcomes"] == {}


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
    and the cell beside it is published unchanged."""
    result = examined(xlsx(members={IMAGE_PART: member}), examining, RecordingOcr())

    assert [item.semantic_role for item in result.segments] == [
        "table_cell",
        "object_inventory",
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
    gif = "xl/media/image1.gif"

    result = examined(
        xlsx(
            rels=EMBED_RELS.replace("image1.png", "image1.gif"),
            members={gif: b"GIF89a" + b"\x00" * 16},
            extension="gif",
            content_type="image/gif",
        ),
        examining,
        RefusingOcr(),
    )

    record = records(result)[0]
    assert record["imageContentType"] == "image/gif"
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == EMBEDDED_UNSUPPORTED
    assert result.failed_units == 0


def test_two_pictures_of_one_image_part_stay_two_located_readings(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A duplicate reference is a second picture showing the same image, so each
    keeps its own anchor, object index and reading, and each pays the shared
    budget again. Nothing is cached: the member is read once per reference and
    discarded with it."""
    drawing = wsDr(
        two_cell(pic() + pic(shape_id="5")) + two_cell(pic(shape_id="9"), column=4)
    )
    engine = RecordingOcr("TWICE")

    result = examined(xlsx(drawing), examining, engine)

    assert [
        (
            item.semantic_role,
            item.source_locator["anchorIndex"],
            item.source_locator["drawingObjectIndex"],
        )
        for item in result.segments[1:]
    ] == [
        ("object_inventory", 0, 0),
        ("body", 0, 0),
        ("object_inventory", 0, 1),
        ("body", 0, 1),
        ("object_inventory", 1, 0),
        ("body", 1, 0),
    ]
    # Three distinct objects, one image part, three reads and three readings.
    assert len(engine.sizes) == 3
    assert {item.source_locator["imagePart"] for item in readings(result)} == {
        IMAGE_PART
    }
    assert result.limitations["embeddedImages"] == 3
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 3}


def test_a_hidden_sheet_hides_what_was_read_on_it(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A reading is out of sight exactly where the record above it is: the
    sheet's own state is reported in the locator and the reading takes the
    sheet's visibility, so nothing read off a hidden sheet is published as
    visible content."""
    result = examined(xlsx(state=' state="hidden"'), examining, RecordingOcr("HIDDEN"))

    assert [
        (item.semantic_role, item.visibility, item.source_locator["sheetState"])
        for item in result.segments
    ] == [
        ("table_cell", "hidden", "hidden"),
        ("object_inventory", "hidden", "hidden"),
        ("body", "hidden", "hidden"),
    ]
    assert result.segments[2].text == "HIDDEN"


def test_a_below_threshold_reading_is_retained_but_is_no_reliable_text(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Candidates the engine was not confident about are kept so a reviewer can
    trace the selection, in a role ordinary retrieval excludes -- and the
    picture reports that it reached no reliable text rather than some."""
    result = examined(xlsx(), examining, RecordingOcr("MAYBE", confidence=9.0))

    assert [
        (item.semantic_role, item.visibility, item.text) for item in result.segments[1:]
    ] == [
        ("object_inventory", "visible", None),
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
def test_one_aggregate_budget_truncates_the_later_pictures_of_one_workbook(
    examining: ocr_resources.ExtractionSettings,
    opened: list[str],
    budget: dict[str, int],
    breach: str,
) -> None:
    """The ceiling is the workbook's and not one picture's, and it bounds the
    work done rather than what is published. A budget paying for the first
    raster alone leaves the second to breach it and the third never opened at
    all: once the shared ceiling is breached, no later member's bytes are read.
    Each cut-short picture reports that it was, rather than being called a
    blank, and the workbook's own outcome says coverage was cut short."""
    drawing = wsDr(
        two_cell(pic())
        + two_cell(pic(shape_id="5"), column=4)
        + two_cell(pic(shape_id="9"), column=8)
    )
    engine = RecordingOcr("FIRST")

    result = examined(
        xlsx(drawing), examining, engine, budget=ExtractionBudget(**budget)
    )

    # The first pays the ceiling, the second breaches it, the third is not read.
    assert opened.count(IMAGE_PART) == 2
    assert len(engine.sizes) == 1
    assert [
        (item.semantic_role, item.text, item.source_locator["anchorIndex"])
        for item in result.segments[1:]
    ] == [
        ("object_inventory", None, 0),
        ("body", "FIRST", 0),
        ("object_inventory", None, 1),
        ("object_inventory", None, 2),
    ]
    assert [record["ocrOutcome"] for record in records(result)] == [
        EMBEDDED_RECOGNISED,
        EMBEDDED_TRUNCATED,
        EMBEDDED_TRUNCATED,
    ]
    assert result.limit_breach == breach
    assert result.limitations["limitBreach"] == breach
    # The cell the sheet wrote is published whatever the image pass spent.
    assert result.segments[0].semantic_role == "table_cell"
