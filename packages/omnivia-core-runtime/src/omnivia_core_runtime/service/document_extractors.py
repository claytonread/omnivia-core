"""Deterministic, inert adapters for Core-owned document bytes.

These adapters receive admitted bytes only. They never open paths, resolve URLs,
run document content, or fetch references. Container-heavy and OCR formats are
refused until their qualified workers are available.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from html.parser import HTMLParser
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.service.document_intake import (
    ExtractedSegment,
    ExtractionOutcome,
)
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_docx,
    extract_pptx,
    extract_xlsx,
)
from omnivia_core_runtime.service.raster_extractors import (
    RasterExtractionInvalid,
    extract_image,
    extract_pdf,
)
from omnivia_core_runtime.service.rtf_extractor import RTFExtractionInvalid, extract_rtf

WORKER_BUILD: Final = "core-portable-text-1"
MAX_TEXT_BYTES: Final = 1_048_576
MAX_RECORDS: Final = 100_000
MAX_FIELDS: Final = 1_000_000
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


class DocumentExtractionUnsupported(ValueError):
    """The admitted media type has no qualified adapter in this worker."""


class DocumentExtractionInvalid(ValueError):
    """The source cannot be decoded under the selected deterministic profile."""


def extract_document(
    content: bytes, *, media_type: str, capture_id: str
) -> ExtractionOutcome:
    """Extract one admitted source with the deterministic adapter for ``media_type``."""
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
        try:
            raster_extraction = (
                extract_pdf(content, capture_id=capture_id)
                if media_type == PDF_MEDIA_TYPE
                else extract_image(content, capture_id=capture_id)
            )
        except RasterExtractionInvalid as error:
            raise DocumentExtractionInvalid(str(error)) from error
        no_reliable_text = max(
            0,
            raster_extraction.expected
            - raster_extraction.useful_units
            - raster_extraction.failed_units,
        )
        failed = raster_extraction.failed_units + no_reliable_text
        state = (
            "succeeded"
            if failed == 0
            else "partial"
            if raster_extraction.segments
            else "failed"
        )
        limitations = {
            **raster_extraction.limitations,
            "noReliableTextUnits": no_reliable_text,
        }
        return _outcome(
            raster_extraction.segments,
            unit_type=raster_extraction.unit_type,
            expected=raster_extraction.expected,
            useful=raster_extraction.useful_units,
            failed=failed,
            state=state,
            limitations=limitations,
        )
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


class _InertHTMLParser(HTMLParser):
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
        "code": "body",
        "caption": "body",
        "th": "table_cell",
        "td": "table_cell",
    }

    def __init__(self, capture_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.capture_id = capture_id
        self.stack: list[tuple[str, str | None, list[str], dict[str, str]]] = []
        self.segments: list[ExtractedSegment] = []
        self.ordinal = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        role = self.block_roles.get(tag)
        if role is not None or tag in {"script", "style", "template"}:
            self.stack.append((tag, role, [], attributes))
        if tag == "img" and attributes.get("alt"):
            self.ordinal += 1
            self.segments.append(
                ExtractedSegment(
                    "image_alt",
                    _locator(
                        self.capture_id,
                        unit="element",
                        element=tag,
                        ordinal=self.ordinal,
                    ),
                    text=attributes["alt"],
                )
            )

    def handle_data(self, data: str) -> None:
        for _, _, chunks, _ in self.stack:
            chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        index = next(
            (i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag),
            None,
        )
        if index is None:
            return
        opened, role, chunks, attributes = self.stack.pop(index)
        text = "".join(chunks)
        if not text:
            return
        self.ordinal += 1
        hidden = opened in {"script", "style", "template"} or "hidden" in attributes
        self.segments.append(
            ExtractedSegment(
                "hidden_content" if hidden else role or "body",
                _locator(
                    self.capture_id,
                    unit="element",
                    element=opened,
                    ordinal=self.ordinal,
                    elementId=attributes.get("id"),
                ),
                text=text,
                visibility="hidden" if hidden else "visible",
            )
        )


def _extract_html(content: bytes, *, capture_id: str) -> ExtractionOutcome:
    text = _decode_utf8(content)
    parser = _InertHTMLParser(capture_id)
    try:
        parser.feed(text)
        parser.close()
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
    identity = [
        {
            "role": segment.semantic_role,
            "locator": dict(segment.source_locator),
            "text": segment.text,
            "value": segment.structured_value,
            "visibility": segment.visibility,
        }
        for segment in segments
    ]
    digest = (
        "sha256:"
        + hashlib.sha256(to_canonical_json({"segments": identity}).encode()).hexdigest()
    )
    return ExtractionOutcome(
        state=state,
        output_digest=digest,
        coverage=coverage,
        limitations=limitations or {},
        worker_build=WORKER_BUILD,
        segments=segments,
    )
