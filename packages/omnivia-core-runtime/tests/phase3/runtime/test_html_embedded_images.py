"""HTML/XHTML embedded rasters: the one bounded pass that reads inline bytes.

The inert walk has always inventoried an image reference without resolving one.
This exercises the pass that reads the single case where the bytes are already
in hand: a bounded ``data:`` URL, in either of the scheme's two encodings,
written in an ``img``'s ``src`` or as the one unambiguous candidate of a
``source``'s ``srcset``. Everything else stays exactly as inert as it was -- a
relative path, an absolute one, ``file:``, ``http:``, ``blob:``, ``cid:``, a
fragment, a candidate list no reading can split without guessing, and the
nonstandard ``src`` a ``source`` has no business carrying -- and no profile can
make any of them a candidate, because nothing here opens, fetches, probes or
resolves a reference outside the supplied bytes.

The author's alt text stays the author's, published as its own direct segment
beside the reading rather than merged with it; a reading of an image the page
hides is hidden too; and nothing persisted holds a byte of the payload -- what
is inventoried is the declaration and how many characters followed it.

The recognition engine is injected throughout, so what is asserted is this
adapter's behaviour rather than whatever a host's Tesseract reads.
"""

from __future__ import annotations

import base64
import builtins
import io
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import pytest
from _trusted_ocr import catalogue_over, stand_in_models
from omnivia_core_runtime.service import (
    document_extractors,
    ocr_resources,
    raster_extractors,
)
from omnivia_core_runtime.service.document_extractors import (
    MAX_DATA_URL_CHARS,
    XHTML_MEDIA_TYPE,
    DocumentExtractionInvalid,
)
from omnivia_core_runtime.service.document_intake import (
    ExtractedSegment,
    ExtractionOutcome,
)
from omnivia_core_runtime.service.raster_extractors import (
    EMBEDDED_DISABLED,
    EMBEDDED_MALFORMED,
    EMBEDDED_NO_TEXT,
    EMBEDDED_RECOGNISED,
    EMBEDDED_RESOURCES_UNAVAILABLE,
    EMBEDDED_TRUNCATED,
    EMBEDDED_UNSUPPORTED,
    GEOMETRY_SPACE,
    ExtractionBudget,
)
from PIL import Image, ImageDraw

CAPTURE: Final = "dcap-" + "a" * 64
SIZE: Final = (120, 40)
#: References that name something outside the supplied bytes. Not one of them is
#: ever opened, fetched, probed or resolved, whatever the profile says.
EXTERNAL: Final = (
    "logo.png",
    "../../etc/logo.png",
    "/absolute/logo.png",
    "file:///etc/passwd",
    "https://example.invalid/logo.png",
    "http://169.254.169.254/latest/meta-data/",
    "blob:https://example.invalid/8f3d",
    "cid:part1.logo@example.invalid",
    "//example.invalid/logo.png",
    "#fragment",
)


def real_png(label: str = "INVOICE", size: tuple[int, int] = SIZE) -> bytes:
    """A small, genuinely decodable PNG, written by Pillow rather than by hand."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((4, 10), label, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def noisy_png(size: tuple[int, int] = (80, 80)) -> bytes:
    """An incompressible raster, so its inline payload is genuinely long."""
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def real_jpeg(size: tuple[int, int] = SIZE) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def data_url(content: bytes, *, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64," + base64.b64encode(content).decode("ascii")


def percent_data_url(content: bytes, *, media_type: str = "image/png") -> str:
    """One non-base64 ``data:`` URL: the encoding the scheme defines by default.

    Commas are left literal, which a URL payload is free to do, so a reader that
    split a candidate list on them would cut these bytes in half.
    """
    return f"data:{media_type}," + quote(content, safe=",")


def page(*body: str) -> bytes:
    """One page whose prose brackets the given markup.

    The prose is there so the structural extraction is visibly published beside
    whatever the image pass did or did not read.
    """
    return ("<p>Invoice total</p>" + "".join(body) + "<p>Thank you</p>").encode()


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
    """A spy over the decoder, so an unopened payload is visibly unopened."""

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
    """A profile that asks for recognition whose pinned models were not served."""
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
    *,
    media_type: str = "text/html",
) -> ExtractionOutcome:
    return document_extractors._extract_html(
        content,
        capture_id=CAPTURE,
        media_type=media_type,
        settings=settings,
        ocr_engine=engine if engine is not None else RefusingOcr(),  # type: ignore[arg-type]
        budget=budget,
    )


def inventory(result: ExtractionOutcome) -> list[dict[str, object]]:
    return [
        dict(item.structured_value or {})
        for item in result.segments
        if item.semantic_role == "object_inventory"
    ]


def readings(result: ExtractionOutcome) -> list[ExtractedSegment]:
    return [item for item in result.segments if item.extraction_method == "ocr"]


def outcomes(result: ExtractionOutcome) -> list[object]:
    return [
        record["ocrOutcome"] for record in inventory(result) if "ocrOutcome" in record
    ]


def test_an_inline_payload_is_recognised_and_anchored_to_the_element_showing_it(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The reading is a segment of its own, published after the element's own
    inventory record and after the author's alt text, anchored to that element's
    DOM path plus the raster's own frame and line geometry. The prose around it
    keeps the order and the segments it always had."""
    member = real_png()
    engine = RecordingOcr("INVOICE", "7")
    content = page(f'<img alt="Scanned invoice" src="{data_url(member)}">')

    result = examined(content, examining, engine)

    assert result.state == "succeeded"
    assert [(item.semantic_role, item.text) for item in result.segments] == [
        ("body", "Invoice total"),
        ("image_alt", "Scanned invoice"),
        ("object_inventory", None),
        ("body", "INVOICE 7"),
        ("body", "Thank you"),
    ]
    # The author's claim and the reading are two segments: the alt text is
    # direct author-supplied text and the reading is recognised text.
    alt, reading = result.segments[1], result.segments[3]
    assert (alt.extraction_method, reading.extraction_method) == ("direct", "ocr")
    assert reading.language == "eng"
    locator = dict(reading.source_locator)
    assert locator["captureId"] == CAPTURE
    assert locator["element"] == "img"
    assert locator["path"] == "/2:img"
    assert locator["imagePart"] == "/2:img@src"
    assert locator["space"] == GEOMETRY_SPACE
    assert (locator["sourceWidth"], locator["sourceHeight"]) == SIZE
    assert (locator["frame"], locator["line"]) == (1, 1)
    assert dict(alt.source_locator)["path"] == locator["path"]

    [record] = inventory(result)
    assert record == {
        "kind": "image",
        "attribute": "src",
        "scheme": "data",
        # The declaration and nothing after it, so nothing persisted holds a
        # byte of the payload.
        "target": "data:image/png;base64,",
        "resolved": False,
        "mediaType": "image/png",
        "encoding": "base64",
        "payloadChars": len(base64.b64encode(member)),
        "imageDecoded": True,
        "ocrPerformed": True,
        "ocrOutcome": EMBEDDED_RECOGNISED,
    }
    assert result.limitations["embeddedImages"] == 1
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 1}
    serialised = json.dumps(
        [
            [dict(item.source_locator), item.text, item.structured_value]
            for item in result.segments
        ]
    )
    assert base64.b64encode(member)[:64].decode() not in serialised


def test_a_jpeg_payload_declared_as_one_is_recognised_too(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    result = examined(
        page(f'<img src="{data_url(real_jpeg(), media_type="image/jpeg")}">'),
        examining,
        RecordingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert inventory(result)[0]["mediaType"] == "image/jpeg"


def test_a_picture_source_carrying_one_inline_srcset_candidate_is_a_candidate_too(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """``srcset`` is where a ``source`` names its images, and one unambiguous
    inline candidate written there is read through the same one pass. Its
    locator names the attribute the bytes came out of."""
    result = examined(
        page(
            "<picture>"
            f'<source srcset="{data_url(real_png())}">'
            f'<img alt="Fallback" src="{data_url(real_png())}">'
            "</picture>"
        ),
        examining,
        RecordingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED, EMBEDDED_RECOGNISED]
    assert [
        (dict(item.source_locator)["element"], dict(item.source_locator)["imagePart"])
        for item in readings(result)
    ] == [
        ("source", "/2:picture/1:source@srcset"),
        ("img", "/2:picture/2:img@src"),
    ]
    assert [record["attribute"] for record in inventory(result)] == ["srcset", "src"]


@pytest.mark.parametrize("wrapper", ["picture", "video"])
def test_a_source_src_is_inventoried_and_never_read_as_an_image(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
) -> None:
    """``srcset`` is the only attribute by which a ``source`` names an image:
    inside a ``picture`` the element has no ``src`` at all, and inside a
    ``video`` its ``src`` names a media resource that is no image. Bytes written
    there are a candidate in neither case -- nothing is decoded, and the record
    says the declaration and why it was left alone. The page declares no
    embedded image at all."""
    decoder = CountingPillow(monkeypatch)

    result = examined(
        page(f'<{wrapper}><source src="{data_url(real_png())}"></{wrapper}>'), examining
    )

    assert result.state == "succeeded"
    assert decoder.opened == []
    assert result.limitations["embeddedImages"] == 0
    assert inventory(result) == [
        {
            "kind": "image",
            "attribute": "src",
            "resolved": False,
            "unresolvedReason": "not_an_image_source_attribute",
            "scheme": "data",
            "target": "data:image/png;base64,",
            "mediaType": "image/png",
            "encoding": "base64",
            "payloadChars": len(base64.b64encode(real_png())),
        }
    ]
    # Nothing was examined, so the record claims nothing about the payload.
    assert "ocrOutcome" not in inventory(result)[0]


def test_a_srcset_data_url_is_never_split_at_a_comma_in_its_own_payload(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A candidate's URL runs to the next ASCII whitespace, so the commas a
    percent-encoded payload is free to hold are the URL's own. A payload
    deliberately chosen to contain several is read as one whole candidate, and
    the one recognised reading proves the bytes were not cut at any of them."""
    member = real_png()
    url = percent_data_url(member)
    assert url.count(",") > 1

    result = examined(page(f'<source srcset="{url}">'), examining, RecordingOcr())

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    record = inventory(result)[0]
    assert (record["attribute"], record["encoding"]) == ("srcset", "percent")
    assert record["payloadChars"] == len(url) - len("data:image/png,")


@pytest.mark.parametrize(
    ("srcset", "reason", "why"),
    [
        pytest.param(
            "logo.png", "external_reference", "a name outside the bytes", id="external"
        ),
        pytest.param(
            "https://example.invalid/logo.png 2x",
            "multiple_candidates_or_descriptors",
            "a descriptor beside an external name",
            id="external-descriptor",
        ),
        pytest.param(
            "{inline} 1x",
            "multiple_candidates_or_descriptors",
            "a descriptor this adapter will not parse past",
            id="descriptor",
        ),
        pytest.param(
            "{inline}, logo.png 2x",
            "multiple_candidates_or_descriptors",
            "a second candidate",
            id="multiple",
        ),
        pytest.param(
            "logo.png 1x, {inline} 2x",
            "multiple_candidates_or_descriptors",
            "an inline candidate that is not the only one",
            id="multiple-inline-second",
        ),
        pytest.param(
            "{inline},",
            "ambiguous_candidate_separator",
            "a trailing comma whose removal would change the payload",
            id="trailing-comma",
        ),
        pytest.param("", "no_candidate", "an empty value", id="empty"),
        pytest.param("   ", "no_candidate", "whitespace alone", id="whitespace"),
    ],
)
def test_a_srcset_naming_no_one_inline_candidate_is_left_unresolved_and_inert(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
    srcset: str,
    reason: str,
    why: str,
) -> None:
    """None of these can be resolved without weakening a bound or guessing where
    one candidate ends, so each is inventoried as the unresolved reference it is
    and nothing is opened, fetched or decoded. The page stays a success: it
    declared no embedded image, rather than one that could not be read."""
    decoder = CountingPillow(monkeypatch)
    value = srcset.replace("{inline}", data_url(real_png()))

    result = examined(page(f'<source srcset="{value}">'), examining)

    assert result.state == "succeeded", why
    assert decoder.opened == [], why
    assert result.limitations["embeddedImages"] == 0, why
    [record] = inventory(result)
    assert (record["attribute"], record["resolved"]) == ("srcset", False)
    assert record["unresolvedReason"] == reason, why
    # A value that is only names is published as the text it is. One that is
    # exactly one inline URL publishes its declaration, as a resolved candidate
    # would. One that holds an inline payload among descriptors or other
    # candidates publishes neither the payload nor a length measuring them.
    if "{inline}" not in srcset:
        assert record["target"] == value, why
    elif " " in srcset:
        assert record == {
            "kind": "image",
            "attribute": "srcset",
            "resolved": False,
            "unresolvedReason": reason,
            "target": None,
            "valueChars": len(value),
        }
    else:
        assert record["target"] == "data:image/png;base64,", why
        assert record["payloadChars"] == len(value) - len("data:image/png;base64,")


def test_an_unresolved_srcset_locates_the_attribute_it_could_not_resolve(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """One ``source`` may write both attributes, and the two records are then
    told apart by their locators as well as by what they say."""
    result = examined(
        page('<source srcset="a.png 1x, b.png 2x" src="c.png">'), examining
    )

    assert [
        (dict(item.source_locator)["imagePart"], item.structured_value["attribute"])
        for item in result.segments
        if item.semantic_role == "object_inventory"
    ] == [("/2:source@srcset", "srcset"), ("/2:source@src", "src")]


def test_a_long_external_srcset_is_still_refused_by_the_reference_bound(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """An unresolved ``srcset`` of names is a reference and is bounded as one;
    only an inline payload is bounded as content."""
    with pytest.raises(DocumentExtractionInvalid, match="reference exceeds"):
        examined(page('<source srcset="' + "a" * 2049 + '.png">'), examining)


def test_a_profile_that_examines_no_embedded_image_decodes_no_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is structural: handed no settings the payload is neither
    decoded nor recognised, the declaration is still inventoried, and the page
    is a success because it asked for no coverage."""
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img alt="Alt" src="{data_url(real_png())}">'))

    assert result.state == "succeeded"
    assert outcomes(result) == [EMBEDDED_DISABLED]
    assert decoder.opened == []
    assert readings(result) == []
    record = inventory(result)[0]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    # The author's alt text never depended on the profile.
    assert [item.text for item in result.segments if item.semantic_role == "image_alt"]


def test_a_profile_whose_models_were_not_served_reads_nothing_and_says_so(
    unavailable: ocr_resources.ExtractionSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img src="{data_url(real_png())}">'), unavailable)

    assert result.state == "succeeded"
    assert outcomes(result) == [EMBEDDED_RESOURCES_UNAVAILABLE]
    assert decoder.opened == []


@pytest.mark.parametrize("target", EXTERNAL)
def test_a_reference_outside_the_supplied_bytes_is_never_resolved_or_read(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """Under a profile that examines embedded images, with the engine and the
    decoder both wired to fail if anything reaches them, every one of these
    stays the inert target text it has always been: no candidate is built, so
    the page declares no embedded image at all."""
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img alt="Logo" src="{target}">'), examining)

    assert result.state == "succeeded"
    assert decoder.opened == []
    assert result.limitations["embeddedImages"] == 0
    assert inventory(result) == [
        {
            "kind": "image",
            "scheme": document_extractors._reference_scheme(target),
            "target": target,
            "resolved": False,
        }
    ]


def test_nothing_is_fetched_or_opened_to_resolve_a_reference_that_cannot_be_read(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the profile that examines embedded images, with ``open``, the
    socket entry points and subprocess all wired to fail, a page naming a file,
    a host and a candidate list this adapter will not parse is still read: the
    one inline payload is recognised and every unresolved reference stays
    unresolved. There is no fetch and no filesystem fallback for any of them."""

    def refuse(*arguments: object, **keywords: object) -> Any:
        raise AssertionError("the image pass must take no external action")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    result = examined(
        page(
            '<img src="file:///etc/passwd">',
            '<img src="https://example.invalid/logo.png">',
            '<source srcset="https://example.invalid/a.png 1x, b.png 2x">',
            f'<img src="{percent_data_url(real_png())}">',
        ),
        examining,
        RecordingOcr("INVOICE"),
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert result.limitations["embeddedImages"] == 1
    assert all(record["resolved"] is False for record in inventory(result))
    assert [item.text for item in readings(result)] == ["INVOICE"]


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("data:image/png;base64,****", "base64 that is not base64"),
        ("data:image/png;base64,QUJD", "base64 that is not an image"),
        ("data:image/png,%89PNG%0D%0A", "percent escapes that are not an image"),
        ("data:image/png;base64", "a declaration that never ends"),
        ("data:image/png", "no declaration at all"),
    ],
)
def test_a_payload_that_cannot_be_read_as_written_is_this_image_s_own_outcome(
    examining: ocr_resources.ExtractionSettings, url: str, why: str
) -> None:
    """Each of these is reported truthfully and the page around it keeps every
    structural segment it had: the source is partial rather than refused."""
    result = examined(page(f'<img src="{url}">'), examining, RefusingOcr())

    assert result.state == "partial", why
    assert result.limitations["documentOutcome"] == "partial_coverage"
    assert [item.text for item in result.segments if item.text] == [
        "Invoice total",
        "Thank you",
    ]
    assert outcomes(result) == [EMBEDDED_MALFORMED], why


def test_a_percent_encoded_payload_is_read_as_the_bytes_it_encodes(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Base64 is one of a data URL's two encodings, not the only one: the
    scheme's default form is percent-encoded, and a bounded payload written that
    way is a candidate exactly as a base64 one is. The record says which
    encoding was read and the payload itself is still never published."""
    member = real_png()
    engine = RecordingOcr("INVOICE", "7")
    url = percent_data_url(member)

    result = examined(page(f'<img src="{url}">'), examining, engine)

    assert result.state == "succeeded"
    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert [item.text for item in readings(result)] == ["INVOICE 7"]
    # The engine was handed the raster the payload really encodes.
    assert engine.sizes == [SIZE]
    record = inventory(result)[0]
    assert (record["encoding"], record["mediaType"]) == ("percent", "image/png")
    assert record["target"] == "data:image/png,"
    assert record["payloadChars"] == len(url) - len("data:image/png,")
    # The escapes are the payload, so not one of them is published either.
    serialised = json.dumps(
        [[dict(item.source_locator), item.structured_value] for item in result.segments]
    )
    assert "%" not in serialised


def test_a_percent_encoded_jpeg_is_read_as_the_bytes_it_encodes_too(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The second accepted container, written in the second encoding: the two
    choices are independent, so neither is read as implying the other."""
    member = real_jpeg()
    result = examined(
        page(f'<img src="{percent_data_url(member, media_type="image/jpeg")}">'),
        examining,
        RecordingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    record = inventory(result)[0]
    assert (record["encoding"], record["mediaType"]) == ("percent", "image/jpeg")


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        pytest.param("%8", "an escape cut short by the end of the payload", id="short"),
        pytest.param("%8G%50", "an escape whose digits are not hexadecimal", id="hex"),
        pytest.param("%%50", "a percent that escapes nothing", id="bare"),
        pytest.param("%5", "a one-digit escape", id="one-digit"),
        pytest.param("%250", "an escape of the escape character", id="double"),
        pytest.param("%89PNG %0D", "a literal space inside the payload", id="space"),
        pytest.param("%89PNG\n%0D", "a literal newline inside it", id="newline"),
        pytest.param("%89PNG<%0D", "a character a URL may not hold", id="delimiter"),
        pytest.param("%89PNG\x89", "a byte outside ASCII", id="non-ascii"),
    ],
)
def test_a_malformed_percent_escape_refuses_the_payload_rather_than_repairing_it(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    why: str,
) -> None:
    """A payload that is not percent-encoded throughout is not the payload it
    claims to be. Each of these is refused before anything is decoded: the bad
    escape is never left standing as literal text, and the whitespace an author
    may wrap base64 on is never stripped out of these, because stripping it is
    how a broken escape would become a byte the source never wrote.

    ``%250`` is the test of that rule in the other direction: it is a perfectly
    well-formed escape of ``%`` followed by a literal ``0``, so it decodes once
    to the two bytes ``%0`` and reaches the decoder, which refuses it for not
    being an image. It is never decoded twice into ``P``."""
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img src="data:image/png,{payload}">'), examining)

    assert outcomes(result) == [EMBEDDED_MALFORMED], why
    assert result.state == "partial", why
    assert inventory(result)[0]["encoding"] == "percent"
    assert [item.text for item in result.segments if item.text] == [
        "Invoice total",
        "Thank you",
    ]
    # Only the one well-formed payload above reached the decoder at all.
    assert decoder.opened == ([2] if payload == "%250" else []), why


def test_a_plus_in_a_percent_encoded_payload_is_a_plus_and_never_a_space(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``+`` means space in a form body and nothing of the sort in a URL path.
    A payload whose bytes are read with that substitution is a different image,
    so the rule is pinned on bytes that decode either way: ``+`` stays 0x2B."""
    decoded: list[bytes] = []

    def capture(
        pass_: document_extractors.EmbeddedImagePass, content: bytes, *rest: Any
    ) -> Any:
        decoded.append(content)
        return pass_.record(EMBEDDED_MALFORMED)

    monkeypatch.setattr(document_extractors.EmbeddedImagePass, "read", capture)

    examined(page('<img src="data:image/png,%89PNG+a+b">'), examining)

    assert decoded == [b"\x89PNG+a+b"]


def test_an_oversized_percent_encoded_payload_is_bounded_before_it_is_decoded(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one inline ceiling bounds both encodings, and it bounds the encoded
    payload, which is what the source actually carries. It is the only ceiling
    either needs: percent-encoding writes at least one character per byte and
    base64 four per three, so neither can decode past the raster path's own
    source bound, which refuses anything that somehow did."""
    monkeypatch.setattr(document_extractors, "MAX_DATA_URL_CHARS", 32)
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img src="{percent_data_url(real_png())}">'), examining)

    assert outcomes(result) == [EMBEDDED_MALFORMED]
    assert decoder.opened == []
    assert [item.text for item in result.segments if item.text] == [
        "Invoice total",
        "Thank you",
    ]
    assert MAX_DATA_URL_CHARS <= raster_extractors.MAX_SOURCE_BYTES


def test_a_percent_encoded_payload_is_judged_by_its_bytes_and_its_declared_type(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The encoding changes how the bytes are written and nothing about how they
    are judged: the declared type is still checked against the accepted set
    before anything is opened, and the container's own magic still wins over
    what the URL declared."""
    mismatch = examined(
        page(f'<img src="{percent_data_url(real_jpeg())}">'), examining, RefusingOcr()
    )
    unsupported = examined(
        page(f'<img src="{percent_data_url(real_png(), media_type="image/gif")}">'),
        examining,
        RefusingOcr(),
    )

    assert outcomes(mismatch) == [EMBEDDED_MALFORMED]
    assert mismatch.state == "partial"
    assert outcomes(unsupported) == [EMBEDDED_UNSUPPORTED]
    assert unsupported.state == "succeeded"


def test_a_percent_encoded_payload_is_examined_only_under_settings_that_say_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is the settings' and reaches both encodings alike."""
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img src="{percent_data_url(real_png())}">'))

    assert result.state == "succeeded"
    assert outcomes(result) == [EMBEDDED_DISABLED]
    assert decoder.opened == []


def test_the_structural_text_around_every_inline_image_is_preserved(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Whatever the image pass does or does not read, the page's own structure is
    the inert walk's and is published unchanged: headings, table cells and the
    prose on both sides of each image keep the roles and the order they had."""
    content = page(
        "<h2>Receipts</h2>",
        f'<img alt="Scanned" src="{data_url(real_png())}">',
        f'<source srcset="{percent_data_url(real_png())} 2x">',
        '<img src="data:image/png,%8">',
        "<table><tr><th>Item</th><td>Paper</td></tr></table>",
    )

    result = examined(content, examining, RecordingOcr("TOTAL"))

    assert [
        (item.semantic_role, item.text) for item in result.segments if item.text
    ] == [
        ("body", "Invoice total"),
        ("heading", "Receipts"),
        ("image_alt", "Scanned"),
        ("body", "TOTAL"),
        ("table_cell", "Item"),
        ("table_cell", "Paper"),
        ("body", "Thank you"),
    ]
    assert outcomes(result) == [EMBEDDED_RECOGNISED, EMBEDDED_MALFORMED]
    assert result.limitations["embeddedImageOutcomes"] == {
        EMBEDDED_RECOGNISED: 1,
        EMBEDDED_MALFORMED: 1,
    }


def test_bytes_that_are_not_the_declared_container_are_refused_by_the_decoder(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A payload whose magic disagrees with the type the URL declared is
    refused: the declaration never wins over the actual container."""
    result = examined(
        page(f'<img src="{data_url(real_jpeg(), media_type="image/png")}">'),
        examining,
        RefusingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_MALFORMED]
    assert result.state == "partial"


@pytest.mark.parametrize(
    "media_type", ["image/gif", "image/svg+xml", "text/html", "application/pdf", ""]
)
def test_a_declared_type_outside_the_accepted_set_is_refused_as_declared(
    examining: ocr_resources.ExtractionSettings, media_type: str
) -> None:
    """The type is judged before anything is decoded, and the page stays a
    success: an unsupported type is coverage the profile never promised."""
    result = examined(
        page(f'<img src="{data_url(real_png(), media_type=media_type)}">'),
        examining,
        RefusingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_UNSUPPORTED]
    assert result.state == "succeeded"
    # A URL declaring no type at all declares none this adapter admits; it is
    # never guessed at from the bytes.
    assert inventory(result)[0]["mediaType"] == (media_type or None)


def test_an_oversized_inline_payload_is_bounded_without_refusing_the_page(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline bound is this payload's own, not the markup reference bound
    that refuses a page for an unbounded *name*: breaching it leaves the prose
    around the image published."""
    monkeypatch.setattr(document_extractors, "MAX_DATA_URL_CHARS", 32)
    decoder = CountingPillow(monkeypatch)

    result = examined(page(f'<img src="{data_url(real_png())}">'), examining)

    assert outcomes(result) == [EMBEDDED_MALFORMED]
    assert decoder.opened == []
    assert [item.text for item in result.segments if item.text] == [
        "Invoice total",
        "Thank you",
    ]
    assert MAX_DATA_URL_CHARS == 8 * 1_048_576


def test_a_long_inline_payload_no_longer_refuses_the_page_as_a_reference_would(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A real inline raster is far longer than one reference may be, so reading
    it at all depends on the inline bound being its own. A *name* that long is
    still a refusal."""
    member = noisy_png()
    assert len(data_url(member)) > document_extractors.MAX_HTML_REFERENCE_CHARS

    result = examined(
        page(f'<img src="{data_url(member)}">'), examining, RecordingOcr()
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    with pytest.raises(DocumentExtractionInvalid, match="reference exceeds"):
        examined(page('<img src="https://example.invalid/' + "a" * 2048 + '">'))


def test_a_reading_of_an_image_the_page_hides_is_hidden_too(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Hidden-source semantics are the element's, and they reach the reading as
    they reach the author's alt text: both stay out of the default retrieval
    roles rather than being dropped unread."""
    result = examined(
        page(
            '<div hidden><img alt="Hidden alt" '
            f'src="{data_url(real_png())}"></div>'
            '<img alt="Styled alt" style="display:none" '
            f'src="{data_url(real_png())}">'
        ),
        examining,
        RecordingOcr(),
    )

    assert [
        (item.semantic_role, item.visibility)
        for item in result.segments
        if item.semantic_role in {"hidden_content", "image_alt"}
        or item.extraction_method == "ocr"
    ] == [
        ("hidden_content", "hidden"),
        ("hidden_content", "hidden"),
        ("hidden_content", "hidden"),
        ("hidden_content", "hidden"),
    ]
    assert outcomes(result) == [EMBEDDED_RECOGNISED, EMBEDDED_RECOGNISED]


def test_an_image_inside_a_script_or_a_style_is_not_an_element_at_all(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Script and style content is text, never markup, so markup written inside
    one declares no image and nothing of it is decoded. The body stays the
    inert text it has always been."""
    decoder = CountingPillow(monkeypatch)
    url = data_url(real_png())

    result = examined(
        page(f"<script>var x = '<img src=\"{url}\">';</script>"), examining
    )

    assert decoder.opened == []
    assert result.limitations["embeddedImages"] == 0
    assert [item.semantic_role for item in result.segments] == [
        "body",
        "hidden_content",
        "body",
    ]


def test_a_template_s_inline_image_is_read_as_the_hidden_fragment_it_is(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A template's content is markup the document has not placed, so its image
    is declared and its reading is hidden."""
    result = examined(
        page(f'<template><img src="{data_url(real_png())}"></template>'),
        examining,
        RecordingOcr(),
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert [item.visibility for item in readings(result)] == ["hidden"]


def test_xhtml_reads_inline_bytes_through_the_same_pass_behind_the_strict_parser(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The admission parser is still the strict one for XHTML, and the inert
    walk behind it is the one walk, so the reading is the same."""
    member = real_png()
    content = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Invoice total</p>'
        f'<img alt="Scanned" src="{data_url(member)}"/></body></html>'
    ).encode()

    result = examined(content, examining, RecordingOcr(), media_type=XHTML_MEDIA_TYPE)

    assert result.limitations["parser"] == "lxml.etree.XMLParser"
    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert [item.text for item in readings(result)] == ["INVOICE 7"]


def test_xhtml_that_declares_a_document_type_is_still_refused(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Admission is unchanged by the image pass: a DTD is still a refusal, and
    an inline image inside such a source is never reached."""
    content = (
        '<?xml version="1.0"?><!DOCTYPE html [<!ENTITY a "x">]>'
        f'<html><body><img src="{data_url(real_png())}"/></body></html>'
    ).encode()

    with pytest.raises(DocumentExtractionInvalid, match="document type"):
        examined(content, examining, media_type=XHTML_MEDIA_TYPE)


def test_each_occurrence_of_one_payload_is_its_own_candidate_and_its_own_locator(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Two elements holding the same bytes are two occurrences: there is no
    cache keyed by the payload, each pays the aggregate ceiling again, and the
    two locators differ by the DOM path the page really gave them."""
    url = data_url(real_png())
    engine = RecordingOcr()

    result = examined(
        page(f'<img src="{url}">', f'<img src="{url}">'), examining, engine
    )

    assert outcomes(result) == [EMBEDDED_RECOGNISED, EMBEDDED_RECOGNISED]
    assert len(engine.sizes) == 2
    assert [dict(item.source_locator)["path"] for item in readings(result)] == [
        "/2:img",
        "/3:img",
    ]


def test_a_payload_carrying_no_reliable_text_is_read_and_reported_as_such(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    blank = io.BytesIO()
    Image.new("RGB", SIZE, "white").save(blank, format="PNG")

    result = examined(
        page(f'<img src="{data_url(blank.getvalue())}">'),
        examining,
        RecordingOcr(confidence=1.0),
    )

    assert outcomes(result) == [EMBEDDED_NO_TEXT]
    assert result.state == "succeeded"


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
def test_one_aggregate_budget_truncates_the_later_images_of_one_page(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
    budget: dict[str, int],
    breach: str,
) -> None:
    """The ceiling is the page's and not one image's, and it bounds the work
    done rather than what is published. A budget paying for the first raster
    alone leaves the second to breach it and the third never decoded at all.
    Each cut-short image reports that it was, and the page's own outcome says
    coverage was cut short while every structural segment stands."""
    decoder = CountingPillow(monkeypatch)
    url = data_url(real_png())
    engine = RecordingOcr("FIRST")

    result = examined(
        page(f'<img src="{url}">' * 3),
        examining,
        engine,
        ExtractionBudget(**budget),
    )

    assert outcomes(result) == [
        EMBEDDED_RECOGNISED,
        EMBEDDED_TRUNCATED,
        EMBEDDED_TRUNCATED,
    ]
    # The second payload is decoded -- the breach is only set once it asks for
    # the raster -- and the third is refused before it is decoded at all.
    assert len(decoder.opened) == 2
    assert len(engine.sizes) == 1
    assert result.state == "partial"
    assert result.limitations["documentOutcome"] == "coverage_truncated_by_limit"
    assert result.limitations["limitBreach"] == breach
    assert [item.text for item in result.segments if item.text] == [
        "Invoice total",
        "FIRST",
        "Thank you",
    ]


def test_a_cell_claiming_explicit_headers_does_not_double_the_image_pass(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A page with a ``headers`` attribute is walked twice: once to index the
    header ids and once to locate. The collecting walk locates nothing and
    opens nothing, so one image stays one candidate."""
    engine = RecordingOcr()
    content = page(
        '<table><tr><th id="h">Head</th></tr>'
        f'<tr><td headers="h"><img src="{data_url(real_png())}"></td></tr></table>'
    )

    result = examined(content, examining, engine)

    assert outcomes(result) == [EMBEDDED_RECOGNISED]
    assert len(engine.sizes) == 1
    assert result.limitations["embeddedImages"] == 1
