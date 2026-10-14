"""Deterministic, inert adapters for Core-owned document bytes.

These adapters receive admitted bytes only. They never open paths, resolve URLs,
run document content, or fetch references. Container-heavy and OCR formats are
refused until their qualified workers are available.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.service.document_intake import (
    ExtractedSegment,
    ExtractionOutcome,
)
from omnivia_core_runtime.service.ocr_resources import (
    REASON_NEEDS_LANGUAGE,
    RESOURCE_STATE_UNAVAILABLE,
    ExtractionSettings,
    local_ocr_settings,
)
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_docx,
    extract_pptx,
    extract_xlsx,
)
from omnivia_core_runtime.service.raster_extractors import (
    WORKER_BUILD as RASTER_BUILD,
)
from omnivia_core_runtime.service.raster_extractors import (
    RasterExtraction,
    RasterExtractionInvalid,
    extract_image,
    extract_pdf,
)
from omnivia_core_runtime.service.rtf_extractor import RTFExtractionInvalid, extract_rtf

WORKER_BUILD: Final = "core-portable-text-1"
#: The raster/OCR worker reports its own build, so a retried unit's result is
#: only reused when the build that produced it matches.
RASTER_WORKER_BUILD: Final = RASTER_BUILD
#: The document-level outcome of a source whose combined methods found no useful
#: searchable text: **Source captured; no reliable text found**.
DOCUMENT_NO_RELIABLE_TEXT: Final = "no_reliable_text_found"
MAX_TEXT_BYTES: Final = 1_048_576
MAX_RECORDS: Final = 100_000
MAX_FIELDS: Final = 1_000_000
#: HTML walk bounds: start tags read, open nesting depth, attributes on one
#: element, characters accumulated for one element's own text, characters of one
#: link or image target, and emitted segments. Every one of them refuses the
#: source when it is breached; none of them truncates prose to fit.
MAX_HTML_ELEMENTS: Final = 100_000
MAX_HTML_DEPTH: Final = 64
MAX_HTML_ATTRIBUTES: Final = 256
MAX_HTML_SEGMENT_CHARS: Final = 100_000
MAX_HTML_REFERENCE_CHARS: Final = 2048
MAX_HTML_SEGMENTS: Final = 50_000
DOCX_MEDIA_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MEDIA_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
PPTX_MEDIA_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
PDF_MEDIA_TYPE: Final = "application/pdf"
IMAGE_MEDIA_TYPES: Final = frozenset(
    {"image/png", "image/jpeg", "image/tiff", "image/heic", "image/heif", "image/webp"}
)
RTF_MEDIA_TYPES: Final = frozenset({"application/rtf", "text/rtf"})
#: Elements that carry no end tag, so the walk must not hold one open.
_HTML_VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
#: Elements whose text is never document body, whatever it contains.
_HTML_HIDDEN: Final = frozenset({"script", "style", "template"})
#: Elements that cannot be their own direct child, so a repeat implies an end.
_HTML_PEER_CLOSING: Final = frozenset({"li", "p", "td", "th"})
_REFERENCE_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{1,31}):")


class DocumentExtractionUnsupported(ValueError):
    """The admitted media type has no qualified adapter in this worker."""


class DocumentExtractionInvalid(ValueError):
    """The source cannot be decoded under the selected deterministic profile."""


def extract_document(
    content: bytes,
    *,
    media_type: str,
    capture_id: str,
    settings: ExtractionSettings | None = None,
) -> ExtractionOutcome:
    """Extract one admitted source with the deterministic adapter for ``media_type``.

    ``settings`` carries the trusted processing mode and, for a local-OCR
    profile, the pinned engine/model/language identity. It defaults to the
    packaged Text-and-local-OCR profile for PDF and image sources; nothing in it
    is ever derived from the document.
    """
    if media_type in {"text/plain", "text/markdown"}:
        text = _decode_utf8(content)
        role = "body"
        segments = (
            ()
            if not text
            else (
                ExtractedSegment(
                    role, _locator(capture_id, unit="document"), text=text
                ),
            )
        )
        return _outcome(segments, unit_type="document", known_empty=not bool(text))
    if media_type in {"text/csv", "text/tab-separated-values"}:
        delimiter = "," if media_type == "text/csv" else "\t"
        return _extract_delimited(content, capture_id=capture_id, delimiter=delimiter)
    if media_type == "text/html":
        return _extract_html(content, capture_id=capture_id)
    office_extractor = {
        DOCX_MEDIA_TYPE: extract_docx,
        XLSX_MEDIA_TYPE: extract_xlsx,
        PPTX_MEDIA_TYPE: extract_pptx,
    }.get(media_type)
    if office_extractor is not None:
        try:
            office_extraction = office_extractor(content, capture_id=capture_id)
        except OOXMLInvalid as error:
            raise DocumentExtractionInvalid(str(error)) from error
        return _outcome(
            office_extraction.segments,
            unit_type=office_extraction.unit_type,
            expected=office_extraction.expected,
            known_empty=not bool(office_extraction.segments),
            limitations=office_extraction.limitations,
        )
    if media_type == PDF_MEDIA_TYPE or media_type in IMAGE_MEDIA_TYPES:
        # Text and local OCR is the default for PDF and image sources; a
        # text-only profile is an explicit trusted choice, never a fallback.
        raster_settings = settings or local_ocr_settings()
        try:
            raster_extraction = (
                extract_pdf(content, capture_id=capture_id, settings=raster_settings)
                if media_type == PDF_MEDIA_TYPE
                else extract_image(
                    content,
                    capture_id=capture_id,
                    media_type=media_type,
                    settings=raster_settings,
                )
            )
        except RasterExtractionInvalid as error:
            raise DocumentExtractionInvalid(str(error)) from error
        return _raster_outcome(raster_extraction)
    if media_type in RTF_MEDIA_TYPES:
        try:
            rtf_extraction = extract_rtf(content, capture_id=capture_id)
        except RTFExtractionInvalid as error:
            raise DocumentExtractionInvalid(str(error)) from error
        return _outcome(
            rtf_extraction.segments,
            unit_type="rtf_block",
            expected=rtf_extraction.blocks,
            known_empty=not bool(rtf_extraction.segments),
            limitations=rtf_extraction.limitations,
        )
    raise DocumentExtractionUnsupported(
        "no qualified adapter for the admitted media type"
    )


def _decode_utf8(content: bytes) -> str:
    if len(content) > MAX_TEXT_BYTES:
        raise DocumentExtractionInvalid("decoded text exceeds the accepted bound")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentExtractionInvalid("text is not valid UTF-8") from error


def _decode_delimited(content: bytes) -> tuple[str, str]:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        return content.decode(encoding), encoding
    except UnicodeDecodeError as error:
        raise DocumentExtractionInvalid(
            "delimited text has an unsupported encoding"
        ) from error


def _extract_delimited(
    content: bytes, *, capture_id: str, delimiter: str
) -> ExtractionOutcome:
    text, encoding = _decode_delimited(content)
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"', strict=True
    )
    segments: list[ExtractedSegment] = []
    previous_line = 0
    field_count = 0
    try:
        for record, row in enumerate(reader, start=1):
            if record > MAX_RECORDS:
                raise DocumentExtractionInvalid("delimited text has too many records")
            field_count += len(row)
            if field_count > MAX_FIELDS:
                raise DocumentExtractionInvalid("delimited text has too many fields")
            start_line = previous_line + 1
            previous_line = reader.line_num
            for column, value in enumerate(row, start=1):
                segments.append(
                    ExtractedSegment(
                        "table_cell",
                        _locator(
                            capture_id,
                            unit="record",
                            record=record,
                            column=column,
                            physicalLineStart=start_line,
                            physicalLineEnd=reader.line_num,
                        ),
                        structured_value={"raw": value},
                    )
                )
    except csv.Error as error:
        raise DocumentExtractionInvalid("delimited text is malformed") from error
    return _outcome(
        tuple(segments),
        unit_type="record",
        expected=max(0, record if "record" in locals() else 0),
        known_empty=not bool(segments),
        limitations={
            "encoding": encoding,
            "delimiter": delimiter,
            "headerTreatment": "preserved_as_record",
        },
    )


@dataclass(slots=True)
class _OpenElement:
    """One open element's walk state: where it is, and what text is its own."""

    tag: str
    #: The element's DOM path, built from sibling indices among its parent's
    #: element children, so the same source always locates it the same way.
    path: str
    #: The segment role this element's own text carries, or ``None`` when the
    #: element is structure only and its text belongs to a descendant block.
    role: str | None
    hidden: bool
    element_id: str | None
    #: Element children opened so far, which is the next child's sibling index.
    children: int = 0
    #: Text runs already emitted for this element. A block that encloses another
    #: block emits its own words in more than one run, and the run index is what
    #: keeps those segments' locators distinct and ordered.
    runs: int = 0
    chunks: list[str] = field(default_factory=list)
    length: int = 0


class _InertHTMLParser(HTMLParser):
    """Walk the start/end events of one HTML source and locate its text.

    Text belongs to the nearest enclosing block element only, so a nested
    block's text is never repeated in its ancestor's segment, and inline markup
    inside a block keeps the source order of the words around it. A block whose
    words are interrupted by a nested block emits them as ordered runs, so
    ``<p>outer<section><p>inner</p></section>after</p>`` reads ``outer``,
    ``inner``, ``after`` in that order and duplicates nothing. A link or image
    target is recorded as inert inventory text and is never opened, fetched or
    resolved. Every bound is a refusal: prose is never silently truncated to
    fit one.
    """

    #: Blocks whose own text is a segment, including the structural containers,
    #: which carry the plain body role: a container holds its own loose text and
    #: bounds attribution, so a block nested inside one never leaks its words
    #: into an ancestor block's segment.
    block_roles: Final = {
        "h1": "heading",
        "h2": "heading",
        "h3": "heading",
        "h4": "heading",
        "h5": "heading",
        "h6": "heading",
        "p": "body",
        "li": "list_item",
        "pre": "body",
        "caption": "body",
        "th": "table_cell",
        "td": "table_cell",
    } | dict.fromkeys(
        (
            "div",
            "section",
            "article",
            "nav",
            "aside",
            "header",
            "footer",
            "main",
            "blockquote",
            "ul",
            "ol",
            "table",
            "tr",
        ),
        "body",
    )

    def __init__(self, capture_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.capture_id = capture_id
        # The first entry is the synthetic frame outside the document's own
        # elements, so the current element is always ``stack[-1]``, the open
        # depth is ``len(stack) - 1``, and a stray end tag cannot pop past it.
        self.stack: list[_OpenElement] = [_OpenElement("", "", None, False, None)]
        self.segments: list[ExtractedSegment] = []
        self.elements = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements += 1
        if self.elements > MAX_HTML_ELEMENTS:
            raise DocumentExtractionInvalid("HTML has too many elements")
        if len(attrs) > MAX_HTML_ATTRIBUTES:
            raise DocumentExtractionInvalid("HTML element has too many attributes")
        if tag in _HTML_PEER_CLOSING and self.stack[-1].tag == tag:
            # ponytail: only the same-tag implied end tag, which is what
            # `<li>a<li>b` needs; full implied-end-tag inference belongs with
            # the rest of the HTML scope.
            self._close(len(self.stack) - 1)
        role = self.block_roles.get(tag) or (
            "hidden_content" if tag in _HTML_HIDDEN else None
        )
        if role is not None:
            # A block is starting, so the words the enclosing block has read so
            # far end here: emitting them now is what keeps the ancestor's
            # before-text ahead of this block's text in the segment order.
            self._flush_run()
        attributes = {key: value or "" for key, value in attrs}
        parent = self.stack[-1]
        parent.children += 1
        path = f"{parent.path}/{parent.children}:{tag}"
        hidden = parent.hidden or tag in _HTML_HIDDEN or "hidden" in attributes
        if tag == "img":
            alt = attributes.get("alt")
            if alt:
                self._emit(
                    ExtractedSegment(
                        "hidden_content" if hidden else "image_alt",
                        self._locator(tag, path, attributes.get("id")),
                        text=alt,
                        visibility="hidden" if hidden else "visible",
                    )
                )
            self._reference("image", tag, path, attributes.get("src"))
        elif tag == "a":
            self._reference("link", tag, path, attributes.get("href"))
        if tag in _HTML_VOID:
            return
        if len(self.stack) > MAX_HTML_DEPTH:
            raise DocumentExtractionInvalid("HTML nesting exceeds the accepted bound")
        self.stack.append(
            _OpenElement(tag, path, role, hidden, attributes.get("id"))
        )

    def handle_data(self, data: str) -> None:
        for element in reversed(self.stack):
            if element.role is None:
                continue
            element.chunks.append(data)
            element.length += len(data)
            if element.length > MAX_HTML_SEGMENT_CHARS:
                raise DocumentExtractionInvalid(
                    "HTML element text exceeds the accepted bound"
                )
            return

    def handle_endtag(self, tag: str) -> None:
        index = next(
            (i for i in range(len(self.stack) - 1, 0, -1) if self.stack[i].tag == tag),
            None,
        )
        if index is not None:
            self._close(index)

    def close(self) -> None:
        super().close()
        # Elements the source never closed still carry their own text.
        self._close(1)

    def _close(self, index: int) -> None:
        """Close the element at ``index`` and any descendant left open in it."""
        while len(self.stack) > index:
            self._emit_text(self.stack.pop())

    def _flush_run(self) -> None:
        """End the pending text run of the nearest enclosing block, if any."""
        for element in reversed(self.stack):
            if element.role is None:
                continue
            self._emit_text(element)
            element.chunks.clear()
            return

    def _emit_text(self, element: _OpenElement) -> None:
        text = "".join(element.chunks)
        if element.role is None or not text.strip():
            return
        # Only an emitted run takes an index, so a block's runs are numbered
        # from one with no gaps however much whitespace sits between them.
        element.runs += 1
        self._emit(
            ExtractedSegment(
                "hidden_content" if element.hidden else element.role,
                self._locator(
                    element.tag, element.path, element.element_id, element.runs
                ),
                text=text,
                visibility="hidden" if element.hidden else "visible",
            )
        )

    def _reference(self, kind: str, tag: str, path: str, target: str | None) -> None:
        """Inventory one link or image target as text, without resolving it."""
        if target is None:
            return
        if len(target) > MAX_HTML_REFERENCE_CHARS:
            raise DocumentExtractionInvalid("HTML reference exceeds the accepted bound")
        self._emit(
            ExtractedSegment(
                "object_inventory",
                self._locator(tag, path, None),
                structured_value={
                    "kind": kind,
                    "scheme": _reference_scheme(target),
                    "target": target,
                    "resolved": False,
                },
                extraction_method="inventory",
                visibility="annotation",
            )
        )

    def _emit(self, segment: ExtractedSegment) -> None:
        self.segments.append(segment)
        if len(self.segments) > MAX_HTML_SEGMENTS:
            raise DocumentExtractionInvalid("HTML extraction has too many segments")

    def _locator(
        self, tag: str, path: str, element_id: str | None, run: int | None = None
    ) -> dict[str, Any]:
        return _locator(
            self.capture_id,
            unit="element",
            element=tag,
            path=path,
            run=run,
            elementId=element_id,
        )


def _reference_scheme(target: str) -> str:
    """Name the reference kind of a target from its text alone."""
    if target.startswith("#"):
        return "fragment"
    match = _REFERENCE_SCHEME.match(target)
    return match.group(1).lower() if match else "relative"


def _extract_html(content: bytes, *, capture_id: str) -> ExtractionOutcome:
    text = _decode_utf8(content)
    parser = _InertHTMLParser(capture_id)
    try:
        parser.feed(text)
        parser.close()
    except DocumentExtractionInvalid:
        raise
    except (ValueError, RecursionError) as error:
        raise DocumentExtractionInvalid(
            "HTML is outside the accepted parser bounds"
        ) from error
    return _outcome(
        tuple(parser.segments),
        unit_type="element",
        known_empty=not bool(parser.segments),
    )


def _locator(capture_id: str, **values: Any) -> dict[str, Any]:
    return {
        "captureId": capture_id,
        **{key: value for key, value in values.items() if value is not None},
    }


def _segments_digest(segments: tuple[ExtractedSegment, ...]) -> str:
    """The output identity of one revision's segments.

    Every identity-bearing field of a segment is covered, including the three
    that distinguish one OCR observation from another over the same region: the
    method that read it, the language claimed for it and its confidence. Two
    results that differ in any of those are two different outputs.
    """
    identity = [
        {
            "role": segment.semantic_role,
            "locator": dict(segment.source_locator),
            "text": segment.text,
            "value": segment.structured_value,
            "visibility": segment.visibility,
            "method": segment.extraction_method,
            "language": segment.language,
            "confidence": segment.confidence,
        }
        for segment in segments
    ]
    return (
        "sha256:"
        + hashlib.sha256(to_canonical_json({"segments": identity}).encode()).hexdigest()
    )


def _raster_outcome(extraction: RasterExtraction) -> ExtractionOutcome:
    """Map exact per-unit raster/OCR counts onto one truthful terminal outcome.

    A limit breach or a failed unit is a partial outcome with the remaining
    coverage stated, never a silent truncation: a breach on the only unit is
    still a breach, so it can never be rounded up into a success. OCR resources
    that were not available are reported as the recoverable condition they are,
    with any embedded text the source did yield still published. A source whose
    combined methods produced no useful searchable text is reported as such
    instead of as a success that implies a searchable body.
    """
    limitations = dict(extraction.limitations)
    breached = limitations.get("limitBreach")
    unavailable_ocr = limitations.get("ocrResourceState") == RESOURCE_STATE_UNAVAILABLE
    complete = (
        extraction.failed_units == 0
        and extraction.unknown_remaining == 0
        and breached is None
        and not unavailable_ocr
    )
    if extraction.visited and extraction.failed_units == extraction.visited:
        state, document_outcome = "failed", "processing_failed"
    elif unavailable_ocr:
        # The requested recognition never ran, so a source whose embedded text
        # carried nothing is not a source that was read and found empty.
        state = "partial" if extraction.useful_units else "failed"
        document_outcome = str(limitations.get("code", REASON_NEEDS_LANGUAGE))
    elif breached is not None:
        state, document_outcome = "partial", "coverage_truncated_by_limit"
    elif extraction.useful_units == 0:
        state, document_outcome = "partial", DOCUMENT_NO_RELIABLE_TEXT
    elif complete:
        state, document_outcome = "succeeded", None
    else:
        state, document_outcome = "partial", "partial_coverage"
    if document_outcome is not None:
        limitations["documentOutcome"] = document_outcome
    # A non-publishable outcome may not carry segments; a wholly failed source
    # has none to carry.
    segments = () if state == "failed" else extraction.segments
    coverage = {
        "schema": "omnivia.extraction-coverage.v2",
        "units": [
            {
                "unitType": extraction.unit_type,
                "expected": extraction.expected,
                "visited": extraction.visited,
                "usefulText": extraction.useful_units,
                "knownEmpty": extraction.known_empty_units,
                "excludedByProfile": extraction.excluded_units,
                "unsupported": extraction.unsupported_units,
                "failed": extraction.failed_units,
                "unknownRemaining": extraction.unknown_remaining,
            }
        ],
        "roleCoverage": dict(Counter(segment.semantic_role for segment in segments)),
    }
    return ExtractionOutcome(
        state=state,
        output_digest=None if state == "failed" else _segments_digest(segments),
        coverage=coverage,
        limitations=limitations,
        worker_build=RASTER_WORKER_BUILD,
        segments=segments,
    )


def _outcome(
    segments: tuple[ExtractedSegment, ...],
    *,
    unit_type: str,
    expected: int | None = None,
    known_empty: bool = False,
    useful: int | None = None,
    failed: int = 0,
    state: str = "succeeded",
    limitations: dict[str, Any] | None = None,
) -> ExtractionOutcome:
    roles = dict(Counter(segment.semantic_role for segment in segments))
    count = expected if expected is not None else (1 if segments or known_empty else 0)
    useful_count = count if segments else 0 if useful is None else useful
    if useful is not None:
        useful_count = useful
    empty = count if known_empty else 0
    coverage = {
        "schema": "omnivia.extraction-coverage.v2",
        "units": [
            {
                "unitType": unit_type,
                "expected": count,
                "visited": count,
                "usefulText": useful_count,
                "knownEmpty": empty,
                "excludedByProfile": 0,
                "unsupported": 0,
                "failed": failed,
                "unknownRemaining": 0,
            }
        ],
        "roleCoverage": roles,
    }
    return ExtractionOutcome(
        state=state,
        output_digest=_segments_digest(segments),
        coverage=coverage,
        limitations=limitations or {},
        worker_build=WORKER_BUILD,
        segments=segments,
    )
