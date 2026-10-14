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

from lxml import etree  # type: ignore[import-untyped]
from lxml import html as lxml_html

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
#: The source bound for markup, which the published profile pins at 20 MiB for
#: these media types. Plain text and Markdown keep ``MAX_TEXT_BYTES``, so the
#: two bounds are separate rather than one shared ceiling raised for everyone.
MAX_HTML_SOURCE_BYTES: Final = 20 * 1_048_576
#: Delimited bounds: records in one source, columns in one record, decoded
#: UTF-8 bytes in one field, fields in the whole source, and decoded characters
#: in the whole source. Every one of them refuses the source when it is
#: breached; none of them drops a record or truncates a field to fit.
MAX_RECORDS: Final = 250_000
MAX_COLUMNS: Final = 1_024
MAX_FIELD_BYTES: Final = 1_048_576
MAX_FIELDS: Final = 1_000_000
MAX_DELIMITED_CHARS: Final = 64 * 1_048_576
#: HTML walk bounds: start tags read, open nesting depth, attributes on one
#: element, characters accumulated for one element's own text, characters of one
#: link or image target or one table attribute, and emitted segments. Every one
#: of them refuses the source when it is breached; none of them truncates prose
#: to fit.
MAX_HTML_ELEMENTS: Final = 100_000
MAX_HTML_DEPTH: Final = 64
MAX_HTML_ATTRIBUTES: Final = 256
MAX_HTML_SEGMENT_CHARS: Final = 100_000
MAX_HTML_REFERENCE_CHARS: Final = 2048
MAX_HTML_SEGMENTS: Final = 50_000
#: Columns one table row may reach, counting the columns a cell covers with
#: ``colspan`` and the ones a cell from an earlier row still occupies. The
#: element bound already limits how many rows and cells a source may hold, but
#: nothing in it limits the column a span can reach, so the grid needs its own
#: bound; together the two bound the whole traversal.
MAX_HTML_TABLE_COLUMNS: Final = 1024
#: The largest ``rowspan``/``colspan`` a cell may declare, which is the bound
#: HTML itself puts on ``colspan``. A document cell never spans this far, so the
#: bound exists to keep one attribute from describing a grid nothing wrote.
MAX_HTML_TABLE_SPAN: Final = 1000
#: Grid slots one source may charge across every table in it, counting each
#: column a cell's placement scan examines, each column it then claims, and each
#: carried span a row start reconsiders. The column and span bounds hold one
#: cell, and the element bound holds the cell count, but their product is a
#: hundred million slots — so a source of many wide cells over a sparse carried
#: grid is bounded by this aggregate rather than by either of them. It is set a
#: hundredfold below that product: a real table of a thousand rows by twenty
#: columns charges tens of thousands of slots, not millions.
MAX_HTML_TABLE_GRID_SLOTS: Final = 1_000_000
#: Header cells one ``headers`` attribute may claim. The attribute's own length
#: is bounded by ``MAX_HTML_REFERENCE_CHARS``, which bounds the token count with
#: it, but a cell governed by more header cells than a document table has rows
#: describes no table anyone wrote, so the count is bounded in its own right.
MAX_HTML_TABLE_HEADER_REFS: Final = 64
#: Distinct libxml2 conditions reported from one admission parse. A warning list
#: is diagnostics, not prose, so this one is a cap rather than a refusal.
MAX_HTML_PARSER_WARNINGS: Final = 32
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
XHTML_MEDIA_TYPE: Final = "application/xhtml+xml"
#: The markup media types this adapter reads. Both are read by the same inert
#: walk; only the admission parse in front of it differs between them.
HTML_MEDIA_TYPES: Final = frozenset({"text/html", XHTML_MEDIA_TYPE})
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
#: Row groups, which name the section a row sits in. A row outside all of them
#: sits directly in its table.
_HTML_TABLE_SECTIONS: Final = frozenset({"thead", "tbody", "tfoot"})
#: A span attribute's whole accepted syntax: an unsigned ASCII decimal, short
#: enough that the value is read before its magnitude is judged.
_SPAN_VALUE: Final = re.compile(r"[0-9]{1,4}")
#: The ``scope`` keywords a header cell may declare. ``row`` and ``col`` name a
#: reach this walk can establish from the cell's own place; ``rowgroup`` and
#: ``colgroup`` name one it cannot, so they are reported and nothing is inferred
#: from them.
_HTML_TABLE_SCOPES: Final = frozenset({"row", "col", "rowgroup", "colgroup"})
#: The ASCII whitespace an IDREF list is separated by. HTML separates on these
#: five characters only, so an id holding a non-breaking space is one token here
#: as it is there, rather than two.
_ASCII_WHITESPACE: Final = re.compile(r"[ \t\n\f\r]+")
#: A ``headers`` attribute anywhere in the source, which is what decides whether
#: the header-id prescan is worth a second walk. The attribute name cannot be
#: spelled with a character reference, so a source with no literal occurrence of
#: it has no cell that claims explicit headers.
_HEADERS_ATTRIBUTE: Final = re.compile(r"(?i)headers\s*=")
_REFERENCE_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{1,31}):")
#: An XML declaration at the head of the source declares the same thing, so
#: XML-declared XHTML served as ``text/html`` is still read as XML.
_XML_DECLARATION: Final = re.compile(rb"\s*<\?xml[\s?]")
#: Byte-order marks that declare a delimited source's encoding, each with the
#: codec it declares and the name the outcome reports.
_DELIMITED_BOMS: Final = (
    (b"\xef\xbb\xbf", "utf-8", "utf-8-bom"),
    (b"\xff\xfe", "utf-16-le", "utf-16-le-bom"),
    (b"\xfe\xff", "utf-16-be", "utf-16-be-bom"),
)
# csv's process-wide field limit is well below our field bound, so without this
# a field this adapter accepts would be refused by the reader instead.
csv.field_size_limit(MAX_FIELD_BYTES)


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
    if media_type in HTML_MEDIA_TYPES:
        return _extract_html(content, capture_id=capture_id, media_type=media_type)
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


def _decode_utf8(content: bytes, *, limit: int | None = None) -> str:
    if len(content) > (MAX_TEXT_BYTES if limit is None else limit):
        raise DocumentExtractionInvalid("decoded text exceeds the accepted bound")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentExtractionInvalid("text is not valid UTF-8") from error


def _decode_delimited(content: bytes) -> tuple[str, str]:
    """Decode one delimited source under its declared encoding, or refuse it.

    UTF-8 is the only encoding read without a declaration. UTF-16 is read only
    when a byte-order mark declares it, because undeclared UTF-16 is
    indistinguishable from UTF-8 that carries NUL characters, and choosing
    between those two readings is how a column silently becomes mojibake. A
    declared mark is consumed rather than kept, so the first field is the label
    the source wrote and not that label behind a stray U+FEFF.
    """
    codec, encoding = "utf-8", "utf-8"
    for mark, declared_codec, declared_name in _DELIMITED_BOMS:
        if content.startswith(mark):
            content = content[len(mark) :]
            codec, encoding = declared_codec, declared_name
            break
    try:
        text = content.decode(codec)
    except UnicodeDecodeError as error:
        raise DocumentExtractionInvalid(
            "delimited text has an unsupported encoding"
        ) from error
    if len(text) > MAX_DELIMITED_CHARS:
        raise DocumentExtractionInvalid("delimited text exceeds the accepted bound")
    if "\x00" in text:
        # A NUL is never delimited text; it is what undeclared UTF-16 and
        # UTF-32 look like once they have been read as something else.
        raise DocumentExtractionInvalid("delimited text contains NUL characters")
    return text, encoding


def _extract_delimited(
    content: bytes, *, capture_id: str, delimiter: str
) -> ExtractionOutcome:
    """Locate every field of one delimited source exactly as the source holds it.

    The delimiter comes from the admitted media type and from nothing else: the
    bytes are never sniffed, so one source never parses two ways. The first
    record is data like any other, whatever its labels look like: a header row
    is never deleted, deduplicated, renamed or filled in, because nothing in
    the bytes says one exists. An empty trailing field, a newline inside a
    quoted field, a leading zero and a formula-like string are all carried
    through verbatim, so a value is what the source wrote and never what a
    spreadsheet would make of it.

    A record whose field count differs from the first non-empty record's is
    kept with its own count in every one of its locators and reported as a
    warning; it is never padded out, trimmed back or dropped. A blank line is a
    record with no fields, counted and reported as such so that it is not
    mistaken for a ragged one. Every bound is a refusal.
    """
    text, encoding = _decode_delimited(content)
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"', strict=True
    )
    segments: list[ExtractedSegment] = []
    previous_line = 0
    fields = 0
    records = 0
    baseline: int | None = None
    ragged = 0
    blank = 0
    try:
        for records, row in enumerate(reader, start=1):
            if records > MAX_RECORDS:
                raise DocumentExtractionInvalid("delimited text has too many records")
            if len(row) > MAX_COLUMNS:
                raise DocumentExtractionInvalid("delimited record has too many columns")
            fields += len(row)
            if fields > MAX_FIELDS:
                raise DocumentExtractionInvalid("delimited text has too many fields")
            if not row:
                blank += 1
            elif baseline is None:
                baseline = len(row)
            elif len(row) != baseline:
                ragged += 1
            start_line = previous_line + 1
            previous_line = reader.line_num
            for column, value in enumerate(row, start=1):
                # A character is at most four UTF-8 bytes, so only a field long
                # enough to reach the bound in the worst case is measured.
                if (
                    len(value) > MAX_FIELD_BYTES // 4
                    and len(value.encode()) > MAX_FIELD_BYTES
                ):
                    raise DocumentExtractionInvalid(
                        "delimited field exceeds the accepted bound"
                    )
                segments.append(
                    ExtractedSegment(
                        "table_cell",
                        _locator(
                            capture_id,
                            unit="record",
                            record=records,
                            column=column,
                            fieldCount=len(row),
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
        expected=records,
        known_empty=not bool(segments),
        limitations={
            "encoding": encoding,
            "delimiter": delimiter,
            "delimiterSource": "media_type",
            "headerTreatment": "preserved_as_record",
            "fieldCountBaseline": baseline or 0,
            "records": records,
            "fields": fields,
            "raggedRecords": ragged,
            "blankRecords": blank,
            "warnings": [
                name
                for name, count in (
                    ("ragged_records", ragged),
                    ("blank_records", blank),
                )
                if count
            ],
        },
    )


@dataclass(slots=True)
class _OpenTable:
    """One open table's own cell grid, which no table nested in it shares.

    The grid belongs to the ``table`` element that opened it, so it is popped
    with that element and an inner table's rows and cells advance only the inner
    grid's counters.
    """

    #: The table's ordinal among the tables of the whole source, in source
    #: order, which is what a cell and its caption are associated by.
    ordinal: int
    rows: int = 0
    #: The rightmost column the current row has filled, so the next cell of that
    #: row starts after it.
    column: int = 0
    #: Column -> rows a cell spanning down from an earlier row still occupies,
    #: so a later row's cell is placed in the column it really falls in rather
    #: than in its own position among its siblings.
    occupied: dict[int, int] = field(default_factory=dict)
    #: Column -> DOM path of the first ``th`` to occupy it, which heads that
    #: column for every row after it.
    column_headers: dict[int, str] = field(default_factory=dict)
    #: DOM path -> rows still covered, for a ``th`` that starts a body row and
    #: so heads the cells to its right.
    row_headers: dict[str, int] = field(default_factory=dict)


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
    #: The cell grid this element owns, set only on a ``table`` element.
    table: _OpenTable | None = None
    #: The element's frozen place in its table, merged into every locator it
    #: emits. It is fixed at the start tag, so a table nested between this
    #: element's start and end cannot move the element that encloses it.
    placement: dict[str, Any] = field(default_factory=dict)


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

    A table cell additionally keeps its place in its own table's grid: the
    table's ordinal in the source, the row and the column it really falls in,
    the row group it sits in, whether it is a header or a data cell, its
    validated spans, and the header cells that govern it. A caption keeps the
    ordinal of the table it captions. Each table owns its grid, so a table
    nested inside a cell advances none of the enclosing table's counters.

    A cell's governing header cells are read from its ``headers`` attribute when
    it carries one and from the grid's own geometry when it does not, and the
    two readings are told apart by ``headerRefs``: present, and the cell's
    ``headers`` are the ones its own markup claimed; absent, and they are the
    ones this walk inferred. An explicit claim is never mixed with an inferred
    one and never falls back to one, not even when it resolves to nothing, so a
    source that says which headers govern a cell is believed over this walk's
    reading of where the cell sits.
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

    def __init__(
        self,
        capture_id: str,
        *,
        header_ids: dict[tuple[int, str], str | None] | None = None,
        collect_only: bool = False,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.capture_id = capture_id
        # The first entry is the synthetic frame outside the document's own
        # elements, so the current element is always ``stack[-1]``, the open
        # depth is ``len(stack) - 1``, and a stray end tag cannot pop past it.
        self.stack: list[_OpenElement] = [_OpenElement("", "", None, False, None)]
        self.segments: list[ExtractedSegment] = []
        self.elements = 0
        #: Tables opened so far, which is the next table's ordinal.
        self.tables = 0
        #: Grid slots charged by every table in this source so far.
        self.grid_slots = 0
        #: (table ordinal, ``id``) -> the DOM path of the header cell declaring
        #: it, or ``None`` when more than one header cell of that table declares
        #: it. The collecting walk fills it and the locating walk only reads it,
        #: so a reference resolves the same way wherever in its table it sits.
        self.header_ids: dict[tuple[int, str], str | None] = header_ids or {}
        #: Whether this walk only indexes header ids, emitting no segment.
        self.collect_only = collect_only

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
        grid: _OpenTable | None = None
        if tag == "table":
            self.tables += 1
            grid = _OpenTable(self.tables)
            placement = {"table": grid.ordinal}
        else:
            placement = self._place(tag, path, attributes)
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
            _OpenElement(
                tag,
                path,
                role,
                hidden,
                attributes.get("id"),
                table=grid,
                placement=placement,
            )
        )

    def _enclosing_table(self) -> tuple[_OpenTable | None, str]:
        """The innermost open table's grid and the row group the walk is inside.

        The grid is read off the open stack rather than from a stack of its own,
        so it is always the one the current element really sits in and it cannot
        drift out of step with an element the source left unclosed.
        """
        section: str | None = None
        for element in reversed(self.stack):
            if element.table is not None:
                return element.table, section or "direct"
            if section is None and element.tag in _HTML_TABLE_SECTIONS:
                section = element.tag
        return None, "direct"

    def _charge(self, slots: int) -> None:
        """Charge the source's shared grid budget, or refuse the source.

        Every column a placement examines or claims is one slot, so the work the
        whole source asks of the grid is bounded in aggregate and not only one
        cell at a time. Breaching it refuses the source: a truncated grid would
        publish cells in columns the source never put them in.
        """
        self.grid_slots += slots
        if self.grid_slots > MAX_HTML_TABLE_GRID_SLOTS:
            raise DocumentExtractionInvalid(
                "HTML table grid exceeds the accepted work bound"
            )

    def _free_run(self, table: _OpenTable, start: int, span: int) -> int:
        """The first column of the first free run of ``span`` columns from ``start``.

        A cell covers contiguous columns, so a run interrupted by a column a
        cell from an earlier row still occupies cannot hold it: the search moves
        past the occupied column rather than writing over it. A run that would
        reach past the column bound refuses the source rather than overlapping a
        neighbour or being trimmed to fit.
        """
        first = start
        while True:
            if first + span - 1 > MAX_HTML_TABLE_COLUMNS:
                raise DocumentExtractionInvalid("HTML table row has too many columns")
            self._charge(span)
            # The rightmost occupied column in the run is what the next
            # candidate run must clear, so one scan rules out every earlier
            # start as well.
            blocked = max(
                (
                    column
                    for column in range(first, first + span)
                    if column in table.occupied
                ),
                default=None,
            )
            if blocked is None:
                return first
            first = blocked + 1

    def _headers(
        self,
        table: _OpenTable,
        path: str,
        columns: range,
        *,
        tag: str,
        refs: list[str] | None,
    ) -> list[str]:
        """The header cells that govern one cell, by the stronger of two readings.

        An explicit ``headers`` attribute is the cell's own claim about which
        header cells govern it, and it is read in the order it was written,
        deduplicated, and resolved only against the header cells of this same
        table. A reference that names nothing in this table, names a cell of
        another table, names the cell itself, or names an id more than one
        header cell of this table declares resolves to nothing: the claim is
        still published verbatim beside the resolution, so an unresolved
        reference is reported rather than invented or dropped in silence.

        Only a cell carrying no ``headers`` attribute at all is read from the
        grid's geometry instead. One that carries an empty or wholly unresolved
        attribute keeps no headers, because falling back to inference there
        would answer the source's own claim with this walk's guess.
        """
        if refs is not None:
            self._charge(len(refs))
            return list(
                dict.fromkeys(
                    found
                    for found in (
                        self.header_ids.get((table.ordinal, ref)) for ref in refs
                    )
                    # A cell is never its own header, however it is referenced.
                    if found is not None and found != path
                )
            )
        # The governing headers are read before this cell registers itself, so a
        # header cell is never its own header.
        headers = dict.fromkeys(
            header
            for header in (table.column_headers.get(column) for column in columns)
            if header is not None
        )
        if tag == "td":
            # A row's leading header heads that row's data, not the other
            # headers beside it: without this, the second ``th`` of a header row
            # in a table with no ``thead`` would read as headed by the first.
            headers.update(dict.fromkeys(table.row_headers))
        return list(headers)

    def _place(self, tag: str, path: str, attributes: dict[str, str]) -> dict[str, Any]:
        """Fix one table element's place in the grid of the table it sits in.

        A row takes the next ordinal of its own table and restarts that table's
        column cursor; a cell takes the first run of free columns wide enough to
        hold it, where free means no cell spanning down from an earlier row
        still occupies it. A cell outside any table keeps no place, because
        there is no grid for it to have one in.

        A header cell additionally keeps what its own markup says about it: the
        ``scope`` keyword it declares, its ``abbr`` label, and the ``id`` the
        locator already carries. A declared ``scope`` also narrows what this
        walk infers from the cell's position: ``col`` heads the columns it
        covers and not its row, ``row`` heads its row and not its columns, and
        ``rowgroup`` or ``colgroup`` heads nothing inferred at all, because this
        walk cannot establish the extent of a group from one cell's place in it.
        A ``scope`` this walk does not know is reported as declared and leaves
        the positional reading alone, which is all that is left to go on.
        """
        if tag not in {"caption", "tr", "th", "td"}:
            return {}
        table, section = self._enclosing_table()
        if table is None:
            return {}
        if tag == "caption":
            return {"table": table.ordinal}
        if tag == "tr":
            table.rows += 1
            table.column = 0
            # A span is consumed by the row it reaches, so every carried span
            # loses one row here and the exhausted ones stop occupying anything.
            self._charge(len(table.occupied) + len(table.row_headers))
            table.occupied = {
                column: rows - 1 for column, rows in table.occupied.items() if rows > 1
            }
            table.row_headers = {
                header: rows - 1
                for header, rows in table.row_headers.items()
                if rows > 1
            }
            return {"table": table.ordinal, "row": table.rows}
        if table.rows == 0:
            # A cell the source put outside any row still falls in a row; one
            # is opened for it rather than reporting it in row zero.
            table.rows = 1
        row_span = _table_span(attributes.get("rowspan"))
        column_span = _table_span(attributes.get("colspan"))
        refs = _header_refs(attributes.get("headers"))
        abbr = attributes.get("abbr") if tag == "th" else None
        scope, unsupported_scope = (
            _table_scope(attributes.get("scope")) if tag == "th" else (None, None)
        )
        first = self._free_run(table, table.column + 1, column_span)
        last = first + column_span - 1
        table.column = last
        columns = range(first, last + 1)
        # The header reads and the span registration below each walk the claimed
        # columns, so the claim is charged once for all of them.
        self._charge(column_span)
        headers = self._headers(table, path, columns, tag=tag, refs=refs)
        if tag == "th":
            if scope in {None, "col"}:
                for column in columns:
                    table.column_headers.setdefault(column, path)
            # ponytail: a header's inferred reach is its own columns and, for a
            # row header, its own rows. A `scope` of `rowgroup` or `colgroup`
            # therefore claims nothing inferred, so a multi-level hierarchy that
            # states its levels only by group scope reads as no relation rather
            # than as a wrong one; establishing a group's extent needs the
            # column/row-group model, which belongs with the rest of that scope.
            if scope == "row" or (scope is None and first == 1 and section != "thead"):
                table.row_headers[path] = row_span
            if self.collect_only:
                self._index_header(table, path, attributes.get("id"))
        if row_span > 1:
            for column in columns:
                table.occupied[column] = row_span
        return {
            "table": table.ordinal,
            "row": table.rows,
            "column": first,
            "section": section,
            # The element is already named by the locator's ``element``; the
            # kind is stated again so a consumer reading the grid need not know
            # that the two always agree for a cell.
            "cellKind": tag,
            "rowSpan": row_span,
            "colSpan": column_span,
            "headers": headers,
            # Absent unless the source wrote one, so that an absent attribute, a
            # declared keyword and a declaration this walk cannot read are three
            # distinct readings rather than one key that has to carry all three.
            "headerRefs": refs,
            "scope": scope,
            "scopeUnsupported": unsupported_scope,
            "abbr": None if abbr is None else _table_attribute(abbr),
        }

    def _index_header(
        self, table: _OpenTable, path: str, element_id: str | None
    ) -> None:
        """Index one header cell's ``id`` for the table it sits in.

        The id is indexed per table, so a nested table's ids are isolated from
        the ids of the table around it even when the two repeat. Two header
        cells of one table claiming one id make every reference to it
        ambiguous, and the conservative reading of an ambiguous reference is
        that it resolves to neither cell rather than to whichever the walk
        happened to see first.
        """
        if not element_id:
            return
        key = (table.ordinal, element_id)
        self.header_ids[key] = None if key in self.header_ids else path

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
                    element.tag,
                    element.path,
                    element.element_id,
                    element.runs,
                    element.placement,
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
        if self.collect_only:
            # The collecting walk is after header ids only; the located
            # segments are the locating walk's to publish.
            return
        self.segments.append(segment)
        if len(self.segments) > MAX_HTML_SEGMENTS:
            raise DocumentExtractionInvalid("HTML extraction has too many segments")

    def _locator(
        self,
        tag: str,
        path: str,
        element_id: str | None,
        run: int | None = None,
        placement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _locator(
            self.capture_id,
            unit="element",
            element=tag,
            path=path,
            run=run,
            elementId=element_id,
            **(placement or {}),
        )


def _table_span(value: str | None) -> int:
    """Read one validated ``rowspan``/``colspan``, or refuse the source.

    A span is an unsigned ASCII decimal from one to ``MAX_HTML_TABLE_SPAN``, and
    an absent attribute is one. A zero, a negative, a fraction, a non-ASCII
    digit, padding and an over-bound value are all refused rather than clamped
    or ignored: clamping is how a cell comes to cover a region the source never
    described, and HTML's own reading of ``rowspan="0"`` as *to the end of the
    section* is exactly the silent reach this adapter must not invent.
    """
    if value is None:
        return 1
    if _SPAN_VALUE.fullmatch(value) is None:
        raise DocumentExtractionInvalid("HTML table span is not a positive decimal")
    span = int(value)
    if not 1 <= span <= MAX_HTML_TABLE_SPAN:
        raise DocumentExtractionInvalid("HTML table span is outside the accepted bound")
    return span


def _table_attribute(value: str) -> str:
    """One table attribute value, or refuse a source that wrote an unbounded one.

    The bound is the one the walk already puts on a link or image target, and
    breaching it refuses the source: a truncated ``headers`` list would claim a
    relationship to a header the source never named.
    """
    if len(value) > MAX_HTML_REFERENCE_CHARS:
        raise DocumentExtractionInvalid(
            "HTML table attribute exceeds the accepted bound"
        )
    return value


def _header_refs(value: str | None) -> list[str] | None:
    """Read one cell's ``headers`` attribute as the ordered IDREF list it claims.

    Absent is distinct from empty: an absent attribute leaves the cell to the
    grid's geometry, and an attribute that names nothing leaves the cell with no
    headers at all. The tokens are kept verbatim and in source order, repeats
    included, because what the source claimed is reported whether or not it
    resolves to anything.
    """
    if value is None:
        return None
    refs = [
        token for token in _ASCII_WHITESPACE.split(_table_attribute(value)) if token
    ]
    if len(refs) > MAX_HTML_TABLE_HEADER_REFS:
        raise DocumentExtractionInvalid("HTML table cell references too many headers")
    return refs


def _table_scope(value: str | None) -> tuple[str | None, str | None]:
    """Read one header cell's ``scope`` as either a known keyword or a raw value.

    An absent attribute is neither; a keyword is matched the way HTML matches
    an enumerated attribute, which is ASCII case-insensitively, so a value that
    is not ASCII cannot be one of them and is reported as declared instead. A
    value this walk does not know is never mapped onto one it does.
    """
    if value is None:
        return None, None
    declared = _table_attribute(value)
    keyword = declared.lower() if declared.isascii() else declared
    return (keyword, None) if keyword in _HTML_TABLE_SCOPES else (None, declared)


def _header_ids(text: str) -> dict[tuple[int, str], str | None]:
    """Index the ``id`` of every header cell in one source, per table.

    The index is collected by a walk over the same bytes as the locating one,
    with the same grid rules, so the table ordinals and DOM paths it records are
    the ones the locating walk reports. Collecting it first is what lets a
    ``headers`` reference resolve to a header cell written later in its table,
    and what makes a duplicate id ambiguous for every reference to it rather
    than only for the ones that follow the duplicate.
    """
    parser = _InertHTMLParser("", collect_only=True)
    parser.feed(text)
    parser.close()
    return parser.header_ids


def _reference_scheme(target: str) -> str:
    """Name the reference kind of a target from its text alone."""
    if target.startswith("#"):
        return "fragment"
    match = _REFERENCE_SCHEME.match(target)
    return match.group(1).lower() if match else "relative"


def _admit_markup(content: bytes, *, media_type: str) -> dict[str, Any]:
    """Admit one HTML/XHTML source through a bounded, inert lxml parse.

    The parse resolves nothing: no DTD is loaded, no entity is expanded and no
    network fetch is attempted. The bytes are always handed over as bytes to
    ``fromstring``, never to ``parse``, so nothing the source contains is ever
    read as a path or a URL.

    XHTML — declared by the media type or by an XML declaration in the bytes —
    is read by the XML parser, which refuses a source carrying a DTD or an
    entity declaration rather than reading it with those declarations quietly
    ignored. Ordinary HTML is read by libxml2's recovering HTML parser, and what
    it had to recover from is reported rather than discarded.

    The parse is bounded by the source bound ``_decode_utf8`` has already
    enforced and by ``huge_tree=False``; the element and depth bounds stay with
    the inert walk, which reads the same bytes and refuses them there.
    """
    xml = media_type == XHTML_MEDIA_TYPE or _XML_DECLARATION.match(content) is not None
    kind = "lxml.etree.XMLParser" if xml else "lxml.html.HTMLParser"
    if not content.strip():
        return {"parser": kind, "parserRecovered": False, "parserWarnings": []}
    parser = (
        etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            dtd_validation=False,
            no_network=True,
            huge_tree=False,
        )
        if xml
        else lxml_html.HTMLParser(no_network=True, huge_tree=False)
    )
    try:
        root = etree.fromstring(content, parser)
    except etree.LxmlError as error:
        raise DocumentExtractionInvalid(
            "XHTML is not well formed"
            if xml
            else "HTML is outside the accepted parser bounds"
        ) from error
    if xml and root is not None:
        # An entity can only be declared inside a document type, so refusing
        # the one refuses the other: no declaration is read with its effect
        # quietly dropped, which is the reading that hides a billion laughs.
        docinfo = root.getroottree().docinfo
        if docinfo.internalDTD is not None or docinfo.externalDTD is not None:
            raise DocumentExtractionInvalid(
                "XHTML declares a document type or an entity"
            )
    warnings = sorted({entry.type_name for entry in parser.error_log})
    return {
        "parser": kind,
        "parserRecovered": bool(warnings),
        "parserWarnings": warnings[:MAX_HTML_PARSER_WARNINGS],
    }


def _extract_html(
    content: bytes, *, capture_id: str, media_type: str
) -> ExtractionOutcome:
    text = _decode_utf8(content, limit=MAX_HTML_SOURCE_BYTES)
    limitations = _admit_markup(content, media_type=media_type)
    # The admission parse decides whether the source is readable at all; the
    # segments are still located by the inert walk over the same bytes, so what
    # this adapter publishes does not move when libxml2's tree does.
    try:
        # The header-id index is only wanted by a cell claiming explicit
        # headers, and the attribute name cannot be written any other way, so a
        # source without it is spared the collecting walk entirely.
        header_ids = _header_ids(text) if _HEADERS_ATTRIBUTE.search(text) else {}
        parser = _InertHTMLParser(capture_id, header_ids=header_ids)
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
        limitations=limitations,
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
