"""Bounded, inert structural readers for admitted Office Open XML packages."""

from __future__ import annotations

import datetime
import io
import posixpath
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final, Self
from xml.etree import ElementTree

from omnivia_core_runtime.service.document_intake import ExtractedSegment

MAX_SOURCE_BYTES: Final = 50 * 1024 * 1024
MAX_MEMBERS: Final = 10_000
MAX_EXPANDED_BYTES: Final = 512 * 1024 * 1024
MAX_MEMBER_BYTES: Final = 64 * 1024 * 1024
MAX_XML_BYTES: Final = 64 * 1024 * 1024
MAX_MEMBER_RATIO: Final = 200
MAX_RATIO_FLOOR: Final = 1024 * 1024
MAX_SEGMENTS: Final = 100_000
MAX_MERGED_CELLS: Final = 200_000
_CHUNK: Final = 256 * 1024
_ALLOWED_COMPRESSION: Final = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
# Encrypted, strongly encrypted, and masked-local-header entries all mean the
# directory cannot be trusted to describe what the stream holds.
_UNSAFE_FLAGS: Final = 0x1 | 0x40 | 0x2000
_ARCHIVE_MAGIC: Final = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
)
_MAGIC_BYTES: Final = max(len(magic) for magic in _ARCHIVE_MAGIC)
_FORBIDDEN_XML: Final = (b"<!DOCTYPE", b"<!ENTITY", b"<!NOTATION")
_RELATIONSHIP_MODES: Final = frozenset({"Internal", "External"})
_MAX_DOCX_DEPTH: Final = 64
_NOTE_TAGS: Final = frozenset({"footnote", "endnote", "comment"})
# Block-level properties, and the compatibility fallback that repeats the
# choice branch, never hold blocks of their own.
_NON_BLOCK_TAGS: Final = frozenset(
    {"pPr", "tblPr", "tblGrid", "trPr", "tcPr", "sectPr", "Fallback"}
)
# Run properties may carry an empty ``ins``/``del`` for the paragraph mark;
# text boxes are read as blocks of their own, never as part of their anchor.
_NON_RUN_TAGS: Final = frozenset({"pPr", "rPr", "Fallback"})
_INSERTED: Final = frozenset({"ins", "moveTo"})
_REMOVED: Final = frozenset({"del", "moveFrom"})
_MAX_REVISION_DEPTH: Final = 4
_HEADING_STYLE: Final = re.compile(r"title|heading[1-9]")
_NOTE_ID: Final = re.compile(r"-?[0-9]{1,9}")
# At least two characters, so a drive letter such as ``C:`` stays a path.
_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]+):")
_MAX_TARGET_CHARS: Final = 2048
_MAX_META_CHARS: Final = 256
_SEPARATOR_NOTES: Final = frozenset(
    {"separator", "continuationSeparator", "continuationNotice"}
)
# Element tag -> (annotation story it names, which end of the reference it is).
_ANNOTATION_MARKS: Final = {
    "footnoteReference": ("footnote", "reference"),
    "endnoteReference": ("endnote", "reference"),
    "commentReference": ("comment", "reference"),
    "commentRangeStart": ("comment", "rangeStart"),
    "commentRangeEnd": ("comment", "rangeEnd"),
}
# Range markers are also legal between blocks, rows and cells.
_RANGE_TAGS: Final = frozenset({"commentRangeStart", "commentRangeEnd"})
_INLINE_MARKS: Final = frozenset(_ANNOTATION_MARKS) | {"hyperlink"}
# ``f/@t``, which defaults to ``normal``, mapped to the kind recorded for it.
_FORMULA_KINDS: Final = {
    "normal": "ordinary",
    "shared": "shared",
    "array": "array",
    "dataTable": "dataTable",
}
_SHEET_STATES: Final = frozenset({"visible", "hidden", "veryHidden"})
# The grid SpreadsheetML itself accepts; a reference beyond it names no cell.
_MAX_COLUMN: Final = 16_384
_MAX_ROW: Final = 1_048_576
_CELL_REF: Final = re.compile(r"([A-Z]{1,3})([0-9]{1,7})")
_DIGITS: Final = re.compile(r"[0-9]{1,7}")
_TRUE_FLAGS: Final = frozenset({"1", "true", "on"})
_FALSE_FLAGS: Final = frozenset({"0", "false", "off"})
# The shape-tree children that carry an identity and a z-order position.
# ``contentPart`` carries neither, so it is not a shape here.
_PPTX_SHAPES: Final = frozenset({"sp", "grpSp", "graphicFrame", "cxnSp", "pic"})
# A picture holds no text of its own, and a group's text belongs to its members.
_PPTX_TEXT_SHAPES: Final = frozenset({"sp", "graphicFrame", "cxnSp"})
# A field is a run that renders a generated value; its text is still a run.
_PPTX_RUNS: Final = frozenset({"r", "fld"})
_MAX_PPTX_SHAPES: Final = 4_000
_MAX_PPTX_GROUP_DEPTH: Final = 16
# Counted across the whole slide, not per shape or per paragraph: a per-shape
# bound multiplies out by the shape bound, and an empty run emits no segment, so
# MAX_SEGMENTS does not bound the traversal either.
_MAX_PPTX_PARAGRAPHS: Final = 20_000
_MAX_PPTX_RUNS: Final = 100_000
_SHAPE_ID: Final = re.compile(r"[0-9]{1,10}")
# A table's grid is the work it asks for whether or not a cell holds text, so
# the area is bounded before any cell is read. Counted across the slide, for the
# same reason the paragraph and run bounds are.
_MAX_PPTX_TABLE_ROWS: Final = 4_000
_MAX_PPTX_TABLE_COLUMNS: Final = 1_024
_MAX_PPTX_TABLE_CELLS: Final = 200_000
_SPAN: Final = re.compile(r"[0-9]{1,4}")
# The relationship type and the content type that together qualify a part as a
# notes slide. Only the two URIs OOXML itself defines for the type qualify --
# Transitional and Strict -- so no other namespace reaches a notes part by
# ending the same way. The content type is identical under both.
_NOTES_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/notesSlide",
    }
)
_NOTES_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.notesslide+xml"
)
_MAX_PPTX_NOTE_RUNS: Final = 20_000
# ST_Coordinate is a signed EMU count; an offset may be negative, so the sign is
# accepted here and the value is kept exactly as the shape wrote it.
_EMU_OFFSET: Final = re.compile(r"-?[0-9]{1,20}")
# An extent is ST_PositiveCoordinate: a width or a height is never negative.
_EMU_EXTENT: Final = re.compile(r"[0-9]{1,20}")


class OOXMLInvalid(ValueError):
    """The package violates the bounded OOXML acceptance profile."""


@dataclass(frozen=True, slots=True)
class OOXMLExtraction:
    segments: tuple[ExtractedSegment, ...]
    unit_type: str
    expected: int
    limitations: dict[str, object]


class _Package:
    """The one validator every OOXML reader goes through.

    Every bound is settled before any part is parsed or handed out: the central
    directory, then each member as it streams, then the inertness of the
    content-types and relationship parts. Nothing here resolves a target on the
    filesystem or the network, and no member is ever expanded as an archive of
    its own.
    """

    def __init__(self, content: bytes) -> None:
        if len(content) > MAX_SOURCE_BYTES:
            raise OOXMLInvalid("OOXML source exceeds the accepted bound")
        if not content.startswith(b"PK\x03\x04"):
            # A package behind prepended bytes is a polyglot, not a package:
            # the directory a reader finds need not describe what a different
            # reader sees.
            raise OOXMLInvalid("OOXML source does not begin with a ZIP local header")
        self._buffer = io.BytesIO(content)
        try:
            self.archive = zipfile.ZipFile(self._buffer)
        except (zipfile.BadZipFile, OSError, NotImplementedError) as error:
            # Strong encryption and compressed-patch entries are refused by
            # ``zipfile`` itself while it reads the directory.
            raise OOXMLInvalid("OOXML source is not a valid ZIP package") from error
        self.names: frozenset[str] = frozenset()
        self.relationships: dict[str, dict[str, tuple[str, bool]]] = {}
        self.relationship_types: dict[str, dict[str, str]] = {}
        # (source part, relationship id, type, target): recorded verbatim, inert.
        self.external_targets: list[tuple[str, str, str, str]] = []
        self.external_relationships = 0
        # Case-folded part name -> the content type an ``Override`` declares.
        self.content_types: dict[str, str] = {}
        self._validate()

    def close(self) -> None:
        self.archive.close()
        self._buffer.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _validate(self) -> None:
        infos = self.archive.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise OOXMLInvalid(
                "OOXML package member count is outside the accepted bound"
            )
        if self.archive.comment:
            raise OOXMLInvalid("OOXML package carries a ZIP archive comment")
        identities: set[str] = set()
        names: set[str] = set()
        expanded = 0
        for info in infos:
            self._validate_entry(info, identities)
            names.add(info.filename)
            expanded += self._validate_stream(info, expanded)
        self.names = frozenset(names)
        if "[Content_Types].xml" not in names:
            raise OOXMLInvalid("OOXML content-types part is missing")
        for element in self.xml("[Content_Types].xml").iter():
            content_type = element.attrib.get("ContentType", "").casefold()
            part_name = element.attrib.get("PartName", "").casefold()
            if (
                "macroenabled" in content_type
                or "vbaproject" in content_type
                or "vbaproject" in part_name
            ):
                raise OOXMLInvalid("macro-enabled Office packages are not supported")
            if _local(element.tag) != "Override" or not part_name:
                continue
            # OPC requires each ``PartName`` to be declared at most once. A
            # repeated declaration is a malformed content-types part whether or
            # not the two agree, so nothing downstream reconciles or picks one.
            part_name = part_name.lstrip("/")
            if part_name in self.content_types:
                raise OOXMLInvalid("OOXML content-types part declares a part twice")
            self.content_types[part_name] = content_type
        self._validate_relationships()

    def _validate_entry(self, info: zipfile.ZipInfo, identities: set[str]) -> None:
        """Refuse a directory entry whose name or metadata is unsafe."""
        name = info.filename
        normal = unicodedata.normalize("NFC", name)
        # Case folding and compatibility folding together: two members that a
        # filesystem or a consumer could confuse are one ambiguous member.
        identity = unicodedata.normalize("NFKC", normal).casefold()
        if (
            not name
            or normal != name
            or "\x00" in name
            or "\\" in name
            or ":" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in normal.split("/"))
            or identity in identities
        ):
            raise OOXMLInvalid("OOXML package contains an unsafe or ambiguous member")
        identities.add(identity)
        stored = info.compress_type == zipfile.ZIP_STORED
        if (
            info.flag_bits & _UNSAFE_FLAGS
            or info.compress_type not in _ALLOWED_COMPRESSION
            or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}
            or info.volume
            or info.file_size > MAX_MEMBER_BYTES
            or (stored and info.compress_size != info.file_size)
            or (
                info.file_size > MAX_RATIO_FLOOR
                and info.file_size > max(info.compress_size, 1) * MAX_MEMBER_RATIO
            )
        ):
            raise OOXMLInvalid("OOXML package member metadata is outside the profile")
        year, month, day, hour, minute, second = info.date_time
        try:
            datetime.date(year, month, day)
        except ValueError as error:
            raise OOXMLInvalid("OOXML package member has invalid metadata") from error
        if hour > 23 or minute > 59 or second > 59:
            raise OOXMLInvalid("OOXML package member has invalid metadata")

    def _validate_stream(self, info: zipfile.ZipInfo, expanded: int) -> int:
        """Stream one member to its end, bounding what the stream produces.

        The directory is a claim, not a measurement: every bound below is taken
        against the bytes the decompressor actually yields, so a member that
        under-declares itself is refused mid-stream rather than expanded.
        """
        actual = 0
        head = b""
        try:
            with self.archive.open(info, "r") as member:
                while chunk := member.read(_CHUNK):
                    head = head or chunk[:_MAGIC_BYTES]
                    actual += len(chunk)
                    if (
                        actual > info.file_size
                        or actual > MAX_MEMBER_BYTES
                        or expanded + actual > MAX_EXPANDED_BYTES
                        or (
                            actual > MAX_RATIO_FLOOR
                            and actual > max(info.compress_size, 1) * MAX_MEMBER_RATIO
                        )
                    ):
                        raise OOXMLInvalid("OOXML member exceeded a streamed bound")
        except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as error:
            raise OOXMLInvalid("OOXML member could not be validated") from error
        if actual != info.file_size:
            raise OOXMLInvalid("OOXML member length does not match its directory entry")
        if head.startswith(_ARCHIVE_MAGIC):
            raise OOXMLInvalid("OOXML package member is itself an archive")
        return actual

    def _validate_relationships(self) -> None:
        """Resolve internal relationship targets and inventory external ones."""
        for name in sorted(name for name in self.names if name.endswith(".rels")):
            if posixpath.basename(posixpath.dirname(name)) != "_rels":
                raise OOXMLInvalid("OOXML relationship part is outside a _rels folder")
            base = posixpath.dirname(posixpath.dirname(name))
            entries: dict[str, tuple[str, bool]] = {}
            types: dict[str, str] = {}
            source = posixpath.join(base, posixpath.basename(name)[: -len(".rels")])
            for relation in self.xml(name).iter():
                if _local(relation.tag) != "Relationship":
                    continue
                relation_id = relation.attrib.get("Id")
                target = relation.attrib.get("Target")
                mode = relation.attrib.get("TargetMode", "Internal")
                if (
                    not relation_id
                    or not target
                    or mode not in _RELATIONSHIP_MODES
                    or relation_id in entries
                ):
                    raise OOXMLInvalid("OOXML relationship is malformed")
                if mode == "External":
                    # Recorded verbatim. Nothing opens, fetches or normalises it.
                    self.external_relationships += 1
                    entries[relation_id] = (target, True)
                    types[relation_id] = relation.attrib.get("Type", "")
                    self.external_targets.append(
                        (source, relation_id, types[relation_id], target)
                    )
                    continue
                joined = posixpath.normpath(
                    target[1:]
                    if target.startswith("/")
                    else posixpath.join(base, target)
                )
                if (
                    joined.startswith(("../", "/"))
                    or joined == ".."
                    or joined not in self.names
                ):
                    raise OOXMLInvalid(
                        "OOXML relationship escapes or misses the package"
                    )
                entries[relation_id] = (joined, False)
                types[relation_id] = relation.attrib.get("Type", "")
            self.relationships[name] = entries
            self.relationship_types[name] = types

    def declared_type(self, name: str) -> str:
        """Return the content type the package declares for one part, if any.

        Only an explicit ``Override`` counts. A default taken from the member's
        extension says what every ``.xml`` member is, which qualifies no part as
        anything in particular.
        """
        return self.content_types.get(name.casefold(), "")

    def bytes(self, name: str, *, limit: int = MAX_XML_BYTES) -> bytes:
        if name not in self.names:
            raise OOXMLInvalid("OOXML relationship names a missing package part")
        info = self.archive.getinfo(name)
        if info.file_size > limit:
            raise OOXMLInvalid("OOXML XML member exceeds the accepted bound")
        with self.archive.open(info, "r") as member:
            data = member.read(limit + 1)
        if len(data) > limit:
            raise OOXMLInvalid("OOXML XML member exceeds the accepted bound")
        return data

    def _checked_xml(self, name: str) -> bytes:
        data = self.bytes(name)
        upper = data.upper()
        if any(marker in upper for marker in _FORBIDDEN_XML):
            raise OOXMLInvalid("OOXML XML contains a forbidden declaration")
        return data

    def xml(self, name: str) -> ElementTree.Element:
        try:
            return ElementTree.fromstring(self._checked_xml(name))
        except ElementTree.ParseError as error:
            raise OOXMLInvalid("OOXML XML is malformed") from error

    def xml_with_prefixes(
        self, name: str
    ) -> tuple[ElementTree.Element, dict[str, str]]:
        """Parse a part and report its namespace prefixes.

        ElementTree drops prefixes, but markup-compatibility attributes name
        namespaces by prefix. A prefix bound to two namespaces in one part is
        ambiguous, so the part is refused rather than guessed at.
        """
        parser = ElementTree.XMLPullParser(events=("start-ns",))
        try:
            parser.feed(self._checked_xml(name))
            root = parser.close()
        except ElementTree.ParseError as error:
            raise OOXMLInvalid("OOXML XML is malformed") from error
        prefixes: dict[str, str] = {}
        for _event, (prefix, uri) in parser.read_events():
            if prefixes.setdefault(prefix, uri) != uri:
                raise OOXMLInvalid("OOXML XML rebinds a namespace prefix")
        return root, prefixes


def extract_docx(content: bytes, *, capture_id: str) -> OOXMLExtraction:
    with _Package(content) as package:
        if "word/document.xml" not in package.names:
            raise OOXMLInvalid("DOCX main document part is missing")
        parts = ["word/document.xml"]
        parts.extend(
            sorted(
                name
                for name in package.names
                if name.startswith("word/")
                and name.endswith(".xml")
                and posixpath.basename(name).startswith(
                    ("header", "footer", "footnotes", "endnotes", "comments")
                )
            )
        )
        styles = _docx_styles(package)
        # Annotation parts are read first so every reference in any story can
        # be checked against the notes and comments that actually exist.
        roots = {
            part: package.xml(part) for part in parts if _docx_story(part) in _NOTE_TAGS
        }
        notes = _annotation_index(roots)
        seen: dict[tuple[str, str, str], int] = {}
        segments: list[ExtractedSegment] = []
        blocks = 0
        for part in parts:
            root = roots[part] if part in roots else package.xml(part)
            story = _docx_story(part)
            containers = (
                [
                    (_attr(item, "id"), item)
                    for item in root
                    if _local(item.tag) in _NOTE_TAGS
                ]
                if story in _NOTE_TAGS
                else []
            ) or [(None, root)]
            for note_id, container in containers:
                walker = _DocxWalker(
                    capture_id,
                    part,
                    story,
                    note_id,
                    styles,
                    segments,
                    package,
                    notes,
                    seen,
                )
                if walker.inventory_note(container):
                    continue
                walker.walk(container, "", 0)
                blocks += walker.paragraphs
        return OOXMLExtraction(
            tuple(segments),
            "document_block",
            blocks,
            {
                "packageMembers": len(package.names),
                "externalReferences": package.external_relationships,
            },
        )


def _annotation_index(
    roots: dict[str, ElementTree.Element],
) -> dict[tuple[str, str], str]:
    """Map (story, id) to the part holding each readable note or comment.

    A malformed or repeated identifier, a second entry without one, or an
    unknown note type leaves references ambiguous, so the package is refused.
    A lone entry without an identifier is still read but can be no target, and
    separator entries are accepted but never become reference targets.
    """
    index: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, str | None]] = set()
    for part, root in roots.items():
        story = _docx_story(part)
        for item in root:
            if _local(item.tag) not in _NOTE_TAGS:
                continue
            note_id = _attr(item, "id")
            if note_id is not None and _NOTE_ID.fullmatch(note_id) is None:
                raise OOXMLInvalid("DOCX annotation has a malformed id")
            # Without an id the locator carries no identity, so only one entry
            # per story can go without.
            if (story, note_id) in seen:
                raise OOXMLInvalid("DOCX annotation id is repeated")
            seen.add((story, note_id))
            note_type = _attr(item, "type") or "normal"
            if story != "comment" and note_type not in _SEPARATOR_NOTES | {"normal"}:
                raise OOXMLInvalid("DOCX note has an unknown type")
            if note_id is not None and (story == "comment" or note_type == "normal"):
                index[(story, note_id)] = part
    return index


def extract_xlsx(content: bytes, *, capture_id: str) -> OOXMLExtraction:
    with _Package(content) as package:
        if "xl/workbook.xml" not in package.names:
            raise OOXMLInvalid("XLSX workbook part is missing")
        shared = _shared_strings(package)
        sheets = _xlsx_sheets(package)
        segments: list[ExtractedSegment] = []
        populated = 0
        for sheet_name, sheet_id, part, state in sheets:
            root = package.xml(part)
            masters = _shared_masters(root)
            merged = _merged_ranges(root)
            hidden_columns = _hidden_columns(root)
            sheet_hidden = state != "visible"
            for cell, row_hidden in _xlsx_cells(root):
                coordinate = cell.attrib.get("r")
                if not coordinate:
                    raise OOXMLInvalid("XLSX cell has no coordinate")
                position = _cell_index(coordinate)
                formula = next((node for node in cell if _local(node.tag) == "f"), None)
                value = next(
                    (node.text or "" for node in cell if _local(node.tag) == "v"), None
                )
                inline = "".join(
                    node.text or "" for node in cell.iter() if _local(node.tag) == "t"
                )
                if formula is None and value is None and not inline:
                    continue
                populated += 1
                column_hidden = position[0] in hidden_columns
                locator = _locator(
                    capture_id,
                    part=part,
                    sheetId=sheet_id,
                    sheet=sheet_name,
                    cell=coordinate,
                    sheetState=state,
                    rowHidden=row_hidden,
                    columnHidden=column_hidden,
                )
                membership = merged.get(position)
                if membership is not None:
                    locator["mergedRange"], locator["mergedAnchor"] = membership
                visibility = (
                    "hidden"
                    if sheet_hidden or row_hidden or column_hidden
                    else "visible"
                )
                cell_type = cell.attrib.get("t")
                if formula is not None:
                    segments.append(
                        ExtractedSegment(
                            "formula",
                            locator,
                            structured_value=_xlsx_formula(formula, masters, value),
                            visibility=visibility,
                        )
                    )
                    if value is not None:
                        segments.append(
                            ExtractedSegment(
                                "saved_value",
                                locator,
                                structured_value={
                                    "raw": value,
                                    "cellType": cell_type,
                                    # An error cache is the recorded outcome of
                                    # the last calculation, never a text value.
                                    "error": cell_type == "e",
                                    "freshness": "unknown",
                                },
                                visibility=visibility,
                            )
                        )
                else:
                    decoded = _xlsx_value(cell_type, value, inline, shared)
                    segments.append(
                        ExtractedSegment(
                            "table_cell",
                            locator,
                            structured_value={
                                "raw": value,
                                "decoded": decoded,
                                "cellType": cell_type,
                            },
                            visibility=visibility,
                        )
                    )
                _bounded(segments)
            for reference, link in _xlsx_hyperlinks(package, part, root):
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        _locator(
                            capture_id,
                            part=part,
                            sheetId=sheet_id,
                            sheet=sheet_name,
                            cell=reference,
                            sheetState=state,
                        ),
                        structured_value=link,
                        extraction_method="inventory",
                        visibility="hidden" if sheet_hidden else "visible",
                    )
                )
                _bounded(segments)
        return OOXMLExtraction(
            tuple(segments),
            "workbook_cell",
            populated,
            {
                "sheets": len(sheets),
                "packageMembers": len(package.names),
                "externalReferences": package.external_relationships,
            },
        )


def extract_pptx(content: bytes, *, capture_id: str) -> OOXMLExtraction:
    with _Package(content) as package:
        if "ppt/presentation.xml" not in package.names:
            raise OOXMLInvalid("PPTX presentation part is missing")
        slides = _pptx_slides(package)
        segments: list[ExtractedSegment] = []
        for ordinal, (slide_id, part, hidden) in enumerate(slides, start=1):
            walker = _PptxWalker(capture_id, part, slide_id, ordinal, hidden, segments)
            walker.walk(_pptx_tree(package.xml(part)), (), (), 0)
            notes = _pptx_notes(package, part)
            if notes is None:
                continue
            relation_id, notes_part = notes
            note_text = _pptx_note_text(package, notes_part)
            if note_text:
                segments.append(
                    ExtractedSegment(
                        "note",
                        _locator(
                            capture_id,
                            part=notes_part,
                            relationshipId=relation_id,
                            slidePart=part,
                            slideId=slide_id,
                            slide=ordinal,
                            slideHidden=hidden,
                        ),
                        text=note_text,
                        visibility="annotation",
                    )
                )
                _bounded(segments)
        return OOXMLExtraction(
            tuple(segments),
            "slide",
            len(slides),
            {
                "packageMembers": len(package.names),
                "externalReferences": package.external_relationships,
            },
        )


def _xlsx_sheets(package: _Package) -> list[tuple[str, str, str, str]]:
    """List (name, sheetId, part, visibility state) for each declared sheet.

    The state is kept as the workbook wrote it, so a very hidden sheet stays
    distinguishable from one a reader can unhide. The vocabulary is closed, so
    anything else is refused rather than read as one of them.
    """
    relations = package.relationships.get("xl/_rels/workbook.xml.rels", {})
    result: list[tuple[str, str, str, str]] = []
    for sheet in package.xml("xl/workbook.xml").iter():
        if _local(sheet.tag) != "sheet":
            continue
        relation_id = _relationship_id(sheet.attrib)
        relation = relations.get(relation_id)
        if relation is None or relation[1]:
            raise OOXMLInvalid("XLSX sheet relationship is missing or external")
        state = sheet.attrib.get("state", "visible")
        if state not in _SHEET_STATES:
            raise OOXMLInvalid("XLSX sheet has an unknown visibility state")
        result.append(
            (
                sheet.attrib.get("name", ""),
                sheet.attrib.get("sheetId", ""),
                relation[0],
                state,
            )
        )
    if len(result) > 100:
        raise OOXMLInvalid("XLSX has too many sheets")
    return result


def _xlsx_cells(
    root: ElementTree.Element,
) -> Iterator[tuple[ElementTree.Element, bool]]:
    """Yield each written cell with whether the row holding it is hidden.

    Only cells inside ``sheetData`` rows are read, and only where one is
    written: ``dimension`` is a claim about extent rather than content, so
    nothing here consults it and no absent cell is materialised.
    """
    for row in next((node for node in root if _local(node.tag) == "sheetData"), ()):
        if _local(row.tag) != "row":
            continue
        hidden = _flag(row, "hidden")
        for cell in row:
            if _local(cell.tag) == "c":
                yield cell, hidden


def _hidden_columns(root: ElementTree.Element) -> frozenset[int]:
    """Collect the column indices one sheet marks hidden.

    A span is only read when it is hidden, and is bounded by the grid itself,
    so the set can hold no more entries than there are columns.
    """
    hidden: set[int] = set()
    for node in root.iter():
        if _local(node.tag) != "col" or not _flag(node, "hidden"):
            continue
        first, last = _column_span(node)
        hidden.update(range(first, last + 1))
    return frozenset(hidden)


def _merged_ranges(
    root: ElementTree.Element,
) -> dict[tuple[int, int], tuple[str, bool]]:
    """Map each merged position to (the range that covers it, is it the anchor).

    Membership is indexed, never materialised as cells: a position the sheet
    never writes a cell for yields nothing at all. Two ranges over one position
    leave that cell in two merges, so the sheet is refused rather than read
    under one of them.
    """
    # ponytail: positions indexed directly, bounded; an interval index only if
    # whole-column merges have to be accepted.
    members: dict[tuple[int, int], tuple[str, bool]] = {}
    for node in root.iter():
        if _local(node.tag) != "mergeCell":
            continue
        reference = node.attrib.get("ref")
        if not reference:
            raise OOXMLInvalid("XLSX merged range has no reference")
        first_column, first_row, last_column, last_row = _cell_range(reference)
        area = (last_column - first_column + 1) * (last_row - first_row + 1)
        if len(members) + area > MAX_MERGED_CELLS:
            raise OOXMLInvalid("XLSX merged ranges exceed the accepted bound")
        for column in range(first_column, last_column + 1):
            for row in range(first_row, last_row + 1):
                anchor = (column, row) == (first_column, first_row)
                if members.setdefault((column, row), (reference, anchor)) != (
                    reference,
                    anchor,
                ):
                    raise OOXMLInvalid("XLSX merged ranges overlap")
    return members


def _xlsx_hyperlinks(
    package: _Package, part: str, root: ElementTree.Element
) -> list[tuple[str, dict[str, object]]]:
    """Inventory one sheet's hyperlinks without resolving any of them.

    Each is recorded against the coordinate or range it sits on. An internal
    jump names only a location inside the workbook; an external one carries its
    relationship target verbatim. Nothing is opened, fetched or normalised, and
    a link that names neither leaves nothing to record, so it is refused.
    """
    rels = posixpath.join(
        posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
    )
    relations = package.relationships.get(rels, {})
    types = package.relationship_types.get(rels, {})
    result: list[tuple[str, dict[str, object]]] = []
    for node in root.iter():
        if _local(node.tag) != "hyperlink":
            continue
        reference = node.attrib.get("ref")
        if not reference:
            raise OOXMLInvalid("XLSX hyperlink has no reference")
        # Validated as a coordinate or range, then kept exactly as written.
        _cell_range(reference)
        relation_id = _relationship_id(node.attrib)
        location = node.attrib.get("location")
        value: dict[str, object] = {
            "kind": "hyperlink",
            "cell": reference,
            "relationshipId": None,
            "relationshipType": None,
            "target": None,
            "external": False,
            "scheme": None,
            "resolved": False,
            "targetTruncated": False,
            "location": _clip(location, _MAX_TARGET_CHARS) if location else None,
        }
        if not relation_id and not location:
            raise OOXMLInvalid("XLSX hyperlink names no relationship or location")
        if relation_id:
            relation, external = _hyperlink_relation(
                relations, types, relation_id, "XLSX"
            )
            value.update(relation, external=external)
        result.append((reference, value))
    return result


def _hyperlink_relation(
    relations: dict[str, tuple[str, bool]],
    types: dict[str, str],
    relation_id: str,
    label: str,
) -> tuple[dict[str, object], bool]:
    """Describe one hyperlink relationship inertly, refusing a bad one.

    An external target is recorded verbatim beside the scheme its own text
    names; nothing opens, fetches or normalises it. A relationship of another
    type would make the link name something that is not a hyperlink at all.
    """
    entry = relations.get(relation_id)
    if entry is None:
        raise OOXMLInvalid(f"{label} hyperlink names a missing relationship")
    relation_type = types.get(relation_id, "")
    if not relation_type.endswith("/hyperlink"):
        raise OOXMLInvalid(f"{label} hyperlink names a relationship of another type")
    target, external = entry
    match = _SCHEME.match(target)
    return {
        "relationshipId": _clip(relation_id, _MAX_META_CHARS),
        "relationshipType": _clip(relation_type, _MAX_TARGET_CHARS),
        "target": _clip(target, _MAX_TARGET_CHARS),
        "targetTruncated": len(target) > _MAX_TARGET_CHARS,
        "scheme": (match.group(1).casefold() if match else "") if external else None,
        "resolved": not external,
    }, external


def _cell_index(coordinate: str) -> tuple[int, int]:
    """Return the (column, row) one A1 coordinate names, or refuse it."""
    match = _CELL_REF.fullmatch(coordinate)
    if match is None:
        raise OOXMLInvalid("XLSX cell coordinate is malformed")
    column = 0
    for letter in match.group(1):
        column = column * 26 + ord(letter) - 64
    row = int(match.group(2))
    if not 1 <= column <= _MAX_COLUMN or not 1 <= row <= _MAX_ROW:
        raise OOXMLInvalid("XLSX cell coordinate is outside the sheet")
    return column, row


def _cell_range(reference: str) -> tuple[int, int, int, int]:
    """Return the (column, row) bounds of ``A1`` or ``A1:B2``, or refuse it.

    A range whose end precedes its start describes no rectangle, so it is
    refused rather than reordered into the one it was probably meant to be.
    """
    start, _, end = reference.partition(":")
    first_column, first_row = _cell_index(start)
    last_column, last_row = _cell_index(end) if end else (first_column, first_row)
    if last_column < first_column or last_row < first_row:
        raise OOXMLInvalid("XLSX cell range is malformed")
    return first_column, first_row, last_column, last_row


def _column_span(element: ElementTree.Element) -> tuple[int, int]:
    """Return the inclusive column span one ``col`` names, or refuse it."""
    first, last = element.attrib.get("min"), element.attrib.get("max")
    if (
        first is None
        or last is None
        or _DIGITS.fullmatch(first) is None
        or _DIGITS.fullmatch(last) is None
        or not 1 <= int(first) <= int(last) <= _MAX_COLUMN
    ):
        raise OOXMLInvalid("XLSX column span is malformed")
    return int(first), int(last)


def _flag(element: ElementTree.Element, name: str) -> bool:
    """Read one SpreadsheetML boolean attribute, refusing an unreadable value."""
    value = element.attrib.get(name)
    if value is None or value in _FALSE_FLAGS:
        return False
    if value in _TRUE_FLAGS:
        return True
    raise OOXMLInvalid("XLSX boolean attribute is malformed")


def _pptx_slides(package: _Package) -> list[tuple[str, str, bool]]:
    relations = package.relationships.get("ppt/_rels/presentation.xml.rels", {})
    result: list[tuple[str, str, bool]] = []
    for slide in package.xml("ppt/presentation.xml").iter():
        if _local(slide.tag) != "sldId":
            continue
        relation = relations.get(_relationship_id(slide.attrib))
        if relation is None or relation[1]:
            raise OOXMLInvalid("PPTX slide relationship is missing or external")
        result.append(
            (slide.attrib.get("id", ""), relation[0], slide.attrib.get("show") == "0")
        )
    if len(result) > 500:
        raise OOXMLInvalid("PPTX has too many slides")
    return result


def _is_notes_relationship(kind: str) -> bool:
    """Say whether one relationship type is the OOXML notes-slide type.

    Scheme and host are case-insensitive under RFC 3986 and the path is not, so
    only those two are folded, and only in ASCII: both defined URIs are ASCII,
    so a type carrying any other character is not one of them and no non-ASCII
    character can case-fold its way into a match.
    """
    if not kind.isascii():
        return False
    scheme, separator, rest = kind.partition("://")
    if not separator:
        return False
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}" in _NOTES_RELATIONSHIPS


def _pptx_notes(package: _Package, slide: str) -> tuple[str, str] | None:
    """Return the (relationship id, part) of one slide's own notes slide.

    The notes part is only ever reached through the slide's own relationships.
    No path is derived by substituting into the slide's file name, so a
    notes-looking member that nothing relates to is not this slide's notes and
    is never read; and nothing here asks a convenience API for a notes slide,
    which would materialise one the package does not hold.

    The relationship has to be internal, has to carry the notes-slide type, and
    has to resolve to a part the package declares with the notes-slide content
    type. Two notes relationships leave which part holds the notes ambiguous,
    so the slide is refused rather than read from whichever sorts first.
    """
    rels = posixpath.join(
        posixpath.dirname(slide), "_rels", posixpath.basename(slide) + ".rels"
    )
    named = sorted(
        relation_id
        for relation_id, kind in package.relationship_types.get(rels, {}).items()
        if _is_notes_relationship(kind)
    )
    if not named:
        return None
    if len(named) > 1:
        raise OOXMLInvalid("PPTX slide names more than one notes relationship")
    target, external = package.relationships[rels][named[0]]
    if external:
        raise OOXMLInvalid("PPTX notes relationship is external")
    if package.declared_type(target) != _NOTES_CONTENT_TYPE:
        raise OOXMLInvalid("PPTX notes relationship does not name a notes part")
    return named[0], target


def _pptx_note_text(package: _Package, part: str) -> str:
    """Join the notes part's own text, bounding the runs it asks to be read."""
    runs = 0
    pieces: list[str] = []
    for node in package.xml(part).iter():
        if _local(node.tag) != "t":
            continue
        runs += 1
        if runs > _MAX_PPTX_NOTE_RUNS:
            raise OOXMLInvalid("PPTX notes slide has too many runs")
        pieces.append(node.text or "")
    return "".join(pieces)


def _pptx_tree(root: ElementTree.Element) -> ElementTree.Element:
    """Return the slide's shape tree, refusing a slide that declares none.

    The tree is the slide's own declaration of what it holds and in what order,
    so it is the only place shapes are read from. A slide without one names no
    shapes at all, which is not a slide anyone authored.
    """
    tree = next((node for node in root.iter() if _local(node.tag) == "spTree"), None)
    if tree is None:
        raise OOXMLInvalid("PPTX slide has no shape tree")
    return tree


@dataclass(frozen=True, slots=True)
class _PptxTable:
    """One validated table: its grid, its cells in source order, and its merges.

    ``merges`` holds every grid position, mapping it to the row span and column
    span the cell there declares and, when another cell's merge covers it, the
    (row, column) of that anchor. An unmerged cell spans one by one and is
    covered by nothing.
    """

    columns: int
    rows: list[list[ElementTree.Element]]
    merges: dict[tuple[int, int], tuple[int, int, tuple[int, int] | None]]


def _pptx_frame_table(frame: ElementTree.Element) -> _PptxTable | None:
    """Return the validated table a graphic frame holds, or None for no table.

    A frame holds one graphic object, so two tables below one frame is not a
    frame anyone authored and nothing here chooses between them.
    """
    tables = [node for node in frame.iter() if _local(node.tag) == "tbl"]
    if not tables:
        return None
    if len(tables) > 1:
        raise OOXMLInvalid("PPTX graphic frame holds more than one table")
    return _pptx_table(tables[0])


def _pptx_table(table: ElementTree.Element) -> _PptxTable:
    """Validate one table's grid and merges before any cell text is read.

    A DrawingML table writes a cell for every grid position, including the ones
    a merge covers, so a row that does not fill the declared grid describes no
    grid at all. Spans and continuation flags are then checked against each
    other: a span may not run past the grid, a cell may not both anchor and
    continue a merge on one axis, two merges may not claim one position, a
    covered position must declare the continuation its anchor implies, and a
    continuation flag that nothing anchors is refused rather than reported as a
    merge that is not there.
    """
    grid = next((node for node in table if _local(node.tag) == "tblGrid"), None)
    if grid is None:
        raise OOXMLInvalid("PPTX table declares no column grid")
    columns = sum(1 for node in grid if _local(node.tag) == "gridCol")
    rows = [
        [cell for cell in row if _local(cell.tag) == "tc"]
        for row in table
        if _local(row.tag) == "tr"
    ]
    if not 1 <= columns <= _MAX_PPTX_TABLE_COLUMNS:
        raise OOXMLInvalid("PPTX table column count is outside the accepted bound")
    if not 1 <= len(rows) <= _MAX_PPTX_TABLE_ROWS:
        raise OOXMLInvalid("PPTX table row count is outside the accepted bound")
    if any(len(row) != columns for row in rows):
        raise OOXMLInvalid("PPTX table row does not fill its declared grid")
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    declared: dict[tuple[int, int], tuple[bool, bool]] = {}
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            row_span = _pptx_span(cell, "rowSpan")
            column_span = _pptx_span(cell, "gridSpan")
            flags = (_pptx_merged(cell, "hMerge"), _pptx_merged(cell, "vMerge"))
            if (flags[0] and column_span > 1) or (flags[1] and row_span > 1):
                raise OOXMLInvalid("PPTX table cell both anchors and continues a merge")
            if column_index + column_span > columns or row_index + row_span > len(rows):
                raise OOXMLInvalid("PPTX table merge runs past the grid")
            spans[(row_index, column_index)] = (row_span, column_span)
            declared[(row_index, column_index)] = flags
    covered: dict[tuple[int, int], tuple[int, int]] = {}
    for (row_index, column_index), (row_span, column_span) in spans.items():
        for down in range(row_span):
            for across in range(column_span):
                if not down and not across:
                    continue
                position = (row_index + down, column_index + across)
                if spans[position] != (1, 1) or covered.setdefault(
                    position, (row_index, column_index)
                ) != (row_index, column_index):
                    raise OOXMLInvalid("PPTX table merges overlap")
                if declared[position] != (across > 0, down > 0):
                    raise OOXMLInvalid("PPTX table merge is not declared by its cell")
    merges: dict[tuple[int, int], tuple[int, int, tuple[int, int] | None]] = {}
    for position, (row_span, column_span) in spans.items():
        anchor = covered.get(position)
        if anchor is None and declared[position] != (False, False):
            raise OOXMLInvalid("PPTX table cell continues a merge that nothing anchors")
        merges[position] = (row_span, column_span, anchor)
    return _PptxTable(columns, rows, merges)


def _pptx_span(cell: ElementTree.Element, name: str) -> int:
    """Read one merge span, refusing a value that is not a count of cells."""
    raw = cell.attrib.get(name)
    if raw is None:
        return 1
    if _SPAN.fullmatch(raw) is None or int(raw) < 1:
        raise OOXMLInvalid("PPTX table merge span is malformed")
    return int(raw)


def _pptx_merged(cell: ElementTree.Element, name: str) -> bool:
    """Read one merge-continuation flag, refusing an unreadable value."""
    value = cell.attrib.get(name)
    if value is None or value in _FALSE_FLAGS:
        return False
    if value in _TRUE_FLAGS:
        return True
    raise OOXMLInvalid("PPTX table merge flag is malformed")


class _PptxWalker:
    """Walks one slide's shape tree in declared order, entering group shapes.

    The tree's child order *is* the z-order, back to front, so position is the
    index of a shape among its own siblings. A group is a level of its own:
    ``groupPath`` names the groups a shape sits inside, outermost first, and
    ``zPath`` gives the sibling index at every level down to the shape itself.
    A group's text is its members' text, read once where each member sits, so
    nothing is reported twice.

    Each run of each paragraph becomes one segment, in source order, carrying
    its paragraph and run index inside the shape it belongs to. A graphic frame
    holding a table reports its cells as table cells instead, each at its own
    row and column in the declared grid. Geometry and
    the placeholder reference are recorded exactly as the shape encodes them;
    nothing is inherited from a layout or master, and no group child-space
    mapping is applied, so a shape that encodes no transform carries none.

    Shapes, paragraphs and runs are counted across the whole slide, because the
    walker is the slide's scope: the indexes in a locator are local to the shape
    or paragraph that holds them and bound nothing on their own.
    """

    def __init__(
        self,
        capture_id: str,
        part: str,
        slide_id: str,
        ordinal: int,
        hidden: bool,
        segments: list[ExtractedSegment],
    ) -> None:
        self._capture_id = capture_id
        self._part = part
        self._slide_id = slide_id
        self._ordinal = ordinal
        self._hidden = hidden
        self._segments = segments
        self._ids: set[str] = set()
        self.shapes = 0
        self.paragraphs = 0
        self.runs = 0
        self.cells = 0

    def walk(
        self,
        parent: ElementTree.Element,
        path: tuple[str, ...],
        z_path: tuple[int, ...],
        depth: int,
    ) -> None:
        position = 0
        for child in _pptx_children(parent):
            kind = _local(child.tag)
            if kind not in _PPTX_SHAPES:
                continue
            self.shapes += 1
            if self.shapes > _MAX_PPTX_SHAPES:
                raise OOXMLInvalid("PPTX slide has too many shapes")
            shape_id = self._identity(child)
            # Validated for every shape, including a group and a picture: a
            # malformed transform is malformed wherever it is written.
            geometry = _pptx_geometry(child)
            here = (*z_path, position)
            position += 1
            if kind == "grpSp":
                if depth >= _MAX_PPTX_GROUP_DEPTH:
                    raise OOXMLInvalid("PPTX group shapes are nested too deeply")
                self.walk(child, (*path, shape_id), here, depth + 1)
                continue
            where = self._where(child, kind, shape_id, path, here, geometry)
            table = _pptx_frame_table(child) if kind == "graphicFrame" else None
            if table is not None:
                self._table(table, where)
            elif kind in _PPTX_TEXT_SHAPES:
                self._emit(child, "body", where)

    def _identity(self, shape: ElementTree.Element) -> str:
        """Return the shape's identifier, refusing a missing or repeated one."""
        properties = next(
            (
                node
                for holder in shape
                if _local(holder.tag).startswith("nv")
                for node in holder
                if _local(node.tag) == "cNvPr"
            ),
            None,
        )
        shape_id = None if properties is None else properties.attrib.get("id")
        if shape_id is None or _SHAPE_ID.fullmatch(shape_id) is None:
            raise OOXMLInvalid("PPTX shape has no readable identifier")
        if shape_id in self._ids:
            raise OOXMLInvalid("PPTX shape identifier is repeated on one slide")
        self._ids.add(shape_id)
        return shape_id

    def _where(
        self,
        shape: ElementTree.Element,
        kind: str,
        shape_id: str,
        path: tuple[str, ...],
        z_path: tuple[int, ...],
        geometry: dict[str, int] | None,
    ) -> dict[str, object]:
        """Return the slide and shape identity every segment of a shape carries."""
        placeholder_type, placeholder_index = _pptx_placeholder(shape)
        return {
            "part": self._part,
            "slideId": self._slide_id,
            "slide": self._ordinal,
            "slideHidden": self._hidden,
            "shapeId": shape_id,
            "shapeKind": kind,
            "groupPath": list(path),
            "zOrder": z_path[-1],
            "zPath": list(z_path),
            "geometry": geometry,
            "placeholderType": placeholder_type,
            "placeholderIndex": placeholder_index,
        }

    def _table(self, table: _PptxTable, where: dict[str, object]) -> None:
        """Emit each table cell's text at its own source row and column.

        Positions are the cell's own place in the declared grid, because a
        DrawingML table writes every covered cell out rather than omitting it.
        A merge's text is reported once, wherever the package wrote it: an
        anchor carries its spans, a covered cell cites the anchor covering it,
        and no text is copied from one to the other.
        """
        self.cells += len(table.rows) * table.columns
        if self.cells > _MAX_PPTX_TABLE_CELLS:
            raise OOXMLInvalid("PPTX slide has too many table cells")
        for row_index, row in enumerate(table.rows):
            for column_index, cell in enumerate(row):
                row_span, column_span, anchor = table.merges[(row_index, column_index)]
                self._emit(
                    cell,
                    "table_cell",
                    {
                        **where,
                        "tableRow": row_index,
                        "tableColumn": column_index,
                        "tableRows": len(table.rows),
                        "tableColumns": table.columns,
                        "rowSpan": row_span,
                        "columnSpan": column_span,
                        "mergeAnchor": None if anchor is None else list(anchor),
                    },
                )

    def _emit(
        self, holder: ElementTree.Element, role: str, where: dict[str, object]
    ) -> None:
        """Emit one segment per non-empty run of ``holder``'s own paragraphs."""
        for paragraph_index, paragraph in enumerate(_pptx_paragraphs(holder)):
            self.paragraphs += 1
            if self.paragraphs > _MAX_PPTX_PARAGRAPHS:
                raise OOXMLInvalid("PPTX slide has too many paragraphs")
            for run_index, text in enumerate(_pptx_runs(paragraph)):
                # Counted before the text is looked at: an empty run is work the
                # slide asked for even though it reports nothing.
                self.runs += 1
                if self.runs > _MAX_PPTX_RUNS:
                    raise OOXMLInvalid("PPTX slide has too many runs")
                if not text:
                    # An empty run keeps its index; it carries no text to report.
                    continue
                self._segments.append(
                    ExtractedSegment(
                        role,
                        _locator(
                            self._capture_id,
                            **where,
                            paragraph=paragraph_index,
                            run=run_index,
                        ),
                        text=text,
                        visibility="hidden" if self._hidden else "visible",
                    )
                )
                _bounded(self._segments)


def _pptx_children(parent: ElementTree.Element) -> Iterator[ElementTree.Element]:
    """Yield one level's children, entering markup-compatibility wrappers.

    ``mc:AlternateContent`` is not a shape: it offers one shape twice, so the
    ``Choice`` branch is read and the ``Fallback`` repeat of it is skipped, and
    what the wrapper holds takes the wrapper's own place in the z-order.
    """
    stack = [iter(parent)]
    while stack:
        for child in stack[-1]:
            if _local(child.tag) != "AlternateContent":
                yield child
                continue
            branch = next(
                (node for node in child if _local(node.tag) == "Choice"),
                next((node for node in child if _local(node.tag) == "Fallback"), None),
            )
            if branch is not None:
                stack.append(iter(branch))
                break
        else:
            stack.pop()


def _pptx_paragraphs(
    shape: ElementTree.Element,
) -> Iterator[ElementTree.Element]:
    """Yield the shape's own paragraphs in source order.

    Any shape nested below this one is skipped: its text is read where that
    shape itself sits, so no paragraph is yielded twice. A compatibility
    fallback repeats the choice branch, so it is skipped for the same reason.
    A paragraph is not entered, so a body holds no body of its own.
    """
    stack = [iter(shape)]
    while stack:
        for node in stack[-1]:
            tag = _local(node.tag)
            if tag == "p":
                yield node
            elif tag not in _PPTX_SHAPES and tag != "Fallback":
                stack.append(iter(node))
                break
        else:
            stack.pop()


def _pptx_runs(paragraph: ElementTree.Element) -> Iterator[str]:
    """Yield each run's text in declared order, keeping a break a separator.

    Only the paragraph's own runs are read, so run order is exactly the order
    the paragraph wrote them in. Text inside a run is joined as written.
    """
    for node in paragraph:
        if _local(node.tag) not in _PPTX_RUNS:
            continue
        yield "".join(
            item.text or "" for item in node if _local(item.tag) == "t"
        )


def _pptx_geometry(shape: ElementTree.Element) -> dict[str, int] | None:
    """Return the transform one shape encodes, or None when it encodes none.

    Offsets and extents are kept verbatim in EMU. Nothing is inherited from a
    placeholder's layout and no group child-space mapping is applied, so an
    absent transform stays absent rather than being resolved into one.
    """
    for node in shape:
        tag = _local(node.tag)
        if tag not in {"spPr", "grpSpPr", "xfrm"}:
            continue
        transform = (
            node
            if tag == "xfrm"
            else next((item for item in node if _local(item.tag) == "xfrm"), None)
        )
        if transform is None:
            continue
        values: dict[str, int] = {}
        for item in transform:
            item_tag = _local(item.tag)
            if item_tag == "off":
                values.update(_emu(item, ("x", "y"), _EMU_OFFSET))
            elif item_tag == "ext":
                values.update(_emu(item, ("cx", "cy"), _EMU_EXTENT))
        return values or None
    return None


def _emu(
    element: ElementTree.Element, names: tuple[str, ...], pattern: re.Pattern[str]
) -> dict[str, int]:
    """Read one offset or extent, refusing a value that is not a coordinate.

    The pattern is the attribute's own type: an offset is signed, so a shape
    placed off the slide keeps its negative coordinate, while an extent has no
    signed form at all and a negative width or height is refused.
    """
    values: dict[str, int] = {}
    for name in names:
        raw = element.attrib.get(name)
        if raw is None or pattern.fullmatch(raw) is None:
            raise OOXMLInvalid("PPTX shape geometry is malformed")
        values[name] = int(raw)
    return values


def _pptx_placeholder(
    shape: ElementTree.Element,
) -> tuple[str | None, str | None]:
    """Return the (type, index) one shape's placeholder reference names.

    The reference is recorded, never followed: nothing here reads the layout or
    master it points at. DrawingML defaults an unstated type to ``body``.
    """
    for holder in shape:
        if not _local(holder.tag).startswith("nv"):
            continue
        for properties in holder:
            if _local(properties.tag) != "nvPr":
                continue
            for node in properties:
                if _local(node.tag) == "ph":
                    return node.attrib.get("type", "body"), node.attrib.get("idx")
    return None, None


def _shared_strings(package: _Package) -> list[str]:
    if "xl/sharedStrings.xml" not in package.names:
        return []
    return [
        "".join(node.text or "" for node in item.iter() if _local(node.tag) == "t")
        for item in package.xml("xl/sharedStrings.xml")
        if _local(item.tag) == "si"
    ]


def _shared_masters(root: ElementTree.Element) -> frozenset[str]:
    """Collect the shared-formula indices one sheet defines a master for.

    A shared formula is written out once, on the cell that carries the ``ref``
    range; every other cell in that range carries only the index. Two masters
    for one index leave those followers naming two expressions, so the sheet is
    refused rather than read under one of them. Indices are scoped to the
    sheet, so each sheet is collected on its own.
    """
    masters: set[str] = set()
    for node in root.iter():
        if _local(node.tag) != "f" or node.attrib.get("t") != "shared":
            continue
        index = node.attrib.get("si")
        if index is None or node.attrib.get("ref") is None:
            continue
        if index in masters:
            raise OOXMLInvalid("XLSX shared formula has more than one master")
        masters.add(index)
    return frozenset(masters)


def _xlsx_formula(
    element: ElementTree.Element, masters: frozenset[str], cached: str | None
) -> dict[str, object]:
    """Describe one ``f`` element without evaluating any part of it.

    The expression is kept exactly as written, including the empty text a
    shared-formula follower carries: deriving a follower's expression from its
    master would mean translating references, which is calculation. The index
    and range are recorded as attributes so the relationship stays readable
    without it. A follower whose master the sheet never defines points at no
    expression at all, so the package is refused.
    """
    kind = _FORMULA_KINDS.get(element.attrib.get("t", "normal"))
    if kind is None:
        raise OOXMLInvalid("XLSX formula has an unknown type")
    index = element.attrib.get("si")
    if kind == "shared" and (index is None or index not in masters):
        raise OOXMLInvalid("XLSX shared formula has no master")
    return {
        "expression": element.text or "",
        "kind": kind,
        "sharedIndex": index,
        "ref": element.attrib.get("ref"),
        # Nothing is calculated here, so an absent cache stays absent and a
        # present one is never checked against the expression.
        "cacheState": "missing" if cached is None else "present",
        "cacheFreshness": "unknown",
    }


def _xlsx_value(
    kind: str | None, raw: str | None, inline: str, shared: list[str]
) -> object:
    if kind == "inlineStr":
        return inline
    if kind == "s":
        try:
            return shared[int(raw or "")]
        except (ValueError, IndexError) as error:
            raise OOXMLInvalid("XLSX shared-string reference is invalid") from error
    if kind == "b":
        return raw == "1"
    return raw


@dataclass(slots=True)
class _CellRun:
    position: str
    parts: list[str]
    runs: int = 0
    # Inventory segments wait for the run that holds their text.
    marks: list[tuple[str, dict[str, object], str, bool]] = field(default_factory=list)


class _DocxWalker:
    """Walks one story's paragraphs and tables in XML document order.

    A position is the block index inside the story (or note), extended with
    row, cell and in-cell block indices for tables, so it depends only on the
    structure of the part. Inside a paragraph, items come out in source order:
    contiguous current text (the first stretch at the paragraph position, later
    ones at ``<paragraph>.t<n>``), deleted text (``<paragraph>.d<n>``) and text
    boxes (``<paragraph>.x<n>.<block>``) where they are anchored, and current
    text is never joined across one of them. A table cell reads its current
    paragraphs as one ``table_cell`` run, closed by any deletion, text box or
    nested table, and later runs sit at ``<cell>.t<n>``.

    Hyperlinks and annotation references stay out of the text: each becomes an
    inert ``object_inventory`` segment right after the stretch it sits in, at
    ``<paragraph>.h<n>`` (hyperlinks) or ``<paragraph>.n<n>`` (note and comment
    references), and range markers between blocks at ``<level>m<n>``. A
    reference names its target by (story, ``noteId``), the same identity the
    annotation's own segments carry, so repeating one reference repeats a
    pointer and never the annotation text.
    """

    def __init__(
        self,
        capture_id: str,
        part: str,
        story: str,
        note_id: str | None,
        styles: dict[str, tuple[str, str | None, bool, bool]],
        segments: list[ExtractedSegment],
        package: _Package,
        notes: dict[tuple[str, str], str],
        seen: dict[tuple[str, str, str], int],
    ) -> None:
        self._capture_id = capture_id
        self._part = part
        self._story = story
        self._note_id = note_id
        self._styles = styles
        self._segments = segments
        self._notes = notes
        self._seen = seen
        # Relationships resolve against the part that holds the reference, and
        # no other: a part without a relationships part has none to name.
        rels = posixpath.join(
            posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
        )
        self._relations = package.relationships.get(rels, {})
        self._relation_types = package.relationship_types.get(rels, {})
        self.paragraphs = 0

    def walk(
        self,
        parent: ElementTree.Element,
        prefix: str,
        depth: int,
        deleted: bool = False,
    ) -> None:
        index = marks = 0
        for block, removed in _blocks(parent, deleted):
            tag = _local(block.tag)
            if tag in _RANGE_TAGS:
                self._mark(block, f"{prefix}m{marks}", removed, None)
                marks += 1
                continue
            position = f"{prefix}{index}"
            if tag == "p":
                self._paragraph(block, position, depth, removed)
            else:
                self._table(block, position, depth, removed)
            index += 1

    def inventory_note(self, entry: ElementTree.Element) -> bool:
        """Inventory what is not note prose; True when the entry is excluded.

        Separator entries hold Word's own rule and notice, not text the author
        wrote, so they are recorded and never read. A comment's author, date
        and initials are kept as clipped strings and never resolved to anyone.
        """
        if self._story in {"footnote", "endnote"}:
            note_type = _attr(entry, "type") or "normal"
            if note_type == "normal":
                return False
            self._inventory(
                "inventory",
                {
                    "kind": "noteSeparator",
                    "noteId": self._note_id,
                    "noteType": note_type,
                    "excluded": True,
                },
                "annotation",
                False,
            )
            return True
        metadata = {
            key: _clip(value, _MAX_META_CHARS)
            for key in ("author", "date", "initials")
            if (value := _attr(entry, key))
        }
        if metadata:
            self._inventory(
                "inventory",
                {"kind": "commentMetadata", "noteId": self._note_id, **metadata},
                "annotation",
                False,
            )
        return False

    def _inventory(
        self,
        position: str,
        value: dict[str, object],
        visibility: str,
        deleted: bool,
    ) -> None:
        self._segments.append(
            ExtractedSegment(
                "object_inventory",
                self._locator(position),
                structured_value=value,
                extraction_method="inventory",
                visibility="annotation" if deleted else visibility,
                revision_state="deleted" if deleted else "current",
            )
        )
        _bounded(self._segments)

    def _mark(
        self,
        element: ElementTree.Element,
        position: str,
        deleted: bool,
        cell: _CellRun | None,
    ) -> None:
        """Inventory one hyperlink or annotation marker, refusing a bad one."""
        if _local(element.tag) == "hyperlink":
            value, visibility = self._hyperlink(element), "visible"
        else:
            value = self._annotation_reference(element)
            # Only a footnote or endnote reference is visible in the page.
            visibility = "annotation" if value["annotation"] == "comment" else "visible"
        if cell is None:
            self._inventory(position, value, visibility, deleted)
        else:
            cell.marks.append((position, value, visibility, deleted))

    def _hyperlink(self, element: ElementTree.Element) -> dict[str, object]:
        ids = [(key, val) for key, val in element.attrib.items() if _local(key) == "id"]
        anchor = _attr(element, "anchor")
        if (
            len(ids) > 1
            or any(not key.startswith("{") or not val for key, val in ids)
            or not (ids or anchor)
        ):
            raise OOXMLInvalid("DOCX hyperlink is malformed")
        value: dict[str, object] = {
            "kind": "hyperlink",
            "relationshipId": None,
            "relationshipType": None,
            "target": None,
            "scheme": None,
            "resolved": False,
            "targetTruncated": False,
            "anchor": _clip(anchor, _MAX_TARGET_CHARS) if anchor else None,
        }
        if not ids:
            return value
        relation, _external = _hyperlink_relation(
            self._relations, self._relation_types, ids[0][1], "DOCX"
        )
        value.update(relation)
        return value

    def _annotation_reference(self, element: ElementTree.Element) -> dict[str, object]:
        story, marker = _ANNOTATION_MARKS[_local(element.tag)]
        note_id = _attr(element, "id")
        target = self._notes.get((story, note_id or ""))
        if note_id is None or _NOTE_ID.fullmatch(note_id) is None or target is None:
            raise OOXMLInvalid("DOCX annotation reference names no annotation")
        key = (story, note_id, marker)
        occurrence = self._seen.get(key, 0)
        self._seen[key] = occurrence + 1
        return {
            "kind": "annotationReference",
            "annotation": story,
            "marker": marker,
            "noteId": note_id,
            "targetPart": target,
            "occurrence": occurrence,
        }

    def _locator(self, position: str) -> dict[str, object]:
        values: dict[str, object] = {
            "part": self._part,
            "story": self._story,
            "position": position,
        }
        if self._note_id is not None:
            values["noteId"] = self._note_id
        return _locator(self._capture_id, **values)

    def _add(self, role: str, position: str, text: str, deleted: bool) -> None:
        annotation = deleted or role in {"note", "comment"}
        self._segments.append(
            ExtractedSegment(
                "deleted_text" if deleted else role,
                self._locator(position),
                text=text,
                visibility="annotation" if annotation else "visible",
                revision_state="deleted" if deleted else "current",
            )
        )
        _bounded(self._segments)

    def _paragraph(
        self,
        paragraph: ElementTree.Element,
        position: str,
        depth: int,
        deleted: bool,
        cell: _CellRun | None = None,
    ) -> None:
        self.paragraphs += 1
        role = "table_cell" if cell else self._role(paragraph)
        current = removed = boxes = links = refs = 0
        for kind, value, box_deleted in _paragraph_items(paragraph, deleted):
            if kind == "m":
                assert isinstance(value, ElementTree.Element)
                if _local(value.tag) == "hyperlink":
                    label, links = f"h{links}", links + 1
                else:
                    label, refs = f"n{refs}", refs + 1
                self._mark(value, f"{position}.{label}", box_deleted, cell)
            elif isinstance(value, ElementTree.Element):
                self._flush(cell)
                if depth >= _MAX_DOCX_DEPTH:
                    raise OOXMLInvalid("DOCX text boxes are nested too deeply")
                self.walk(value, f"{position}.x{boxes}.", depth + 1, box_deleted)
                boxes += 1
            elif kind == "d":
                self._flush(cell)
                self._add("body", f"{position}.d{removed}", value, True)
                removed += 1
            elif cell is not None:
                cell.parts.append(value)
            else:
                # The first current stretch keeps the paragraph's own position.
                self._add(
                    role,
                    f"{position}.t{current}" if current else position,
                    value,
                    False,
                )
                current += 1

    def _flush(self, cell: _CellRun | None) -> None:
        if cell is None:
            return
        if cell.parts:
            position = f"{cell.position}.t{cell.runs}" if cell.runs else cell.position
            self._add("table_cell", position, "\n".join(cell.parts), False)
            cell.runs += 1
            cell.parts.clear()
        for mark in cell.marks:
            self._inventory(*mark)
        cell.marks.clear()

    def _role(self, paragraph: ElementTree.Element) -> str:
        if self._story == "footnote" or self._story == "endnote":
            return "note"
        if self._story == "comment":
            return "comment"
        if self._part == "word/document.xml":
            return _paragraph_role(paragraph, self._styles)
        return "body"

    def _table(
        self,
        table: ElementTree.Element,
        position: str,
        depth: int,
        deleted: bool,
    ) -> None:
        if depth > _MAX_DOCX_DEPTH:
            raise OOXMLInvalid("DOCX tables are nested too deeply")
        row_index = marks = 0
        for row in table:
            tag = _local(row.tag)
            if tag in _RANGE_TAGS:
                self._mark(row, f"{position}.m{marks}", deleted, None)
                marks += 1
            elif tag == "tr":
                self._row(row, f"{position}.r{row_index}", depth, deleted)
                row_index += 1

    def _row(
        self, row: ElementTree.Element, position: str, depth: int, deleted: bool
    ) -> None:
        cell_index = marks = 0
        for cell in row:
            tag = _local(cell.tag)
            if tag in _RANGE_TAGS:
                self._mark(cell, f"{position}.m{marks}", deleted, None)
                marks += 1
                continue
            if tag != "tc":
                continue
            cell_position = f"{position}.c{cell_index}"
            cell_index += 1
            run = _CellRun(cell_position, [])
            index = block_marks = 0
            for block, removed in _blocks(cell, deleted):
                block_tag = _local(block.tag)
                if block_tag in _RANGE_TAGS:
                    self._mark(block, f"{cell_position}.m{block_marks}", removed, run)
                    block_marks += 1
                    continue
                block_position = f"{cell_position}.{index}"
                index += 1
                if block_tag == "tbl":
                    self._flush(run)
                    self._table(block, block_position, depth + 1, removed)
                else:
                    self._paragraph(block, block_position, depth, removed, run)
            self._flush(run)


def _revised(tag: str, deleted: bool, depth: int) -> tuple[bool, int]:
    """Return the deleted state and revision depth inside a ``tag`` element.

    An insertion inside a deletion has no coherent reading, and an unbounded
    stack of wrappers is not a document anyone wrote, so both are refused.
    A deletion inside an insertion is how Word records inserted-then-deleted
    text and stays deleted.
    """
    if tag not in _INSERTED and tag not in _REMOVED:
        return deleted, depth
    if deleted and tag in _INSERTED:
        raise OOXMLInvalid("DOCX revisions contradict each other")
    if depth >= _MAX_REVISION_DEPTH:
        raise OOXMLInvalid("DOCX revisions are nested too deeply")
    return tag in _REMOVED, depth + 1


def _blocks(
    parent: ElementTree.Element, deleted: bool
) -> Iterator[tuple[ElementTree.Element, bool]]:
    """Yield paragraphs and tables below ``parent`` in document order.

    Comment range markers between blocks are yielded too. Each comes with
    whether a deletion wrapped it. Transparent wrappers
    (``body``, content controls, revisions) are entered; a yielded paragraph or
    table is not entered, so nothing is seen twice.
    """
    stack = [(iter(parent), deleted, 0)]
    while stack:
        children, state, depth = stack[-1]
        for child in children:
            tag = _local(child.tag)
            if tag in {"p", "tbl"} or tag in _RANGE_TAGS:
                yield child, state
            elif tag not in _NON_BLOCK_TAGS:
                inner, level = _revised(tag, state, depth)
                stack.append((iter(child), inner, level))
                break
        else:
            stack.pop()


def _paragraph_items(
    paragraph: ElementTree.Element, deleted: bool
) -> list[tuple[str, str | ElementTree.Element, bool]]:
    """Split a paragraph into ordered ``(kind, value, deleted)`` items.

    Kinds are ``t`` current text, ``d`` deleted text, ``x`` a text box and ``m``
    a hyperlink or annotation marker (with whether a deletion wrapped it). Adjacent text of one kind is one stretch;
    tabs and breaks stay as separators inside it.
    """
    items: list[tuple[str, str | ElementTree.Element, bool]] = []
    pending: list[str] = []
    pending_kind = ""

    # Markers follow the stretch they sit in, so they never split the text.
    deferred: list[tuple[str, str | ElementTree.Element, bool]] = []

    def close() -> None:
        if pending:
            items.append((pending_kind, "".join(pending), False))
            pending.clear()
        items.extend(deferred)
        deferred.clear()

    stack = [(iter(paragraph), deleted, 0)]
    while stack:
        children, state, depth = stack[-1]
        for node in children:
            tag = _local(node.tag)
            if tag in _NON_RUN_TAGS:
                continue
            if tag == "txbxContent":
                close()
                items.append(("x", node, state))
                if len(items) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                continue
            if tag in _INLINE_MARKS:
                if ("d" if state else "t") != pending_kind:
                    close()
                    pending_kind = "d" if state else "t"
                deferred.append(("m", node, state))
                if len(items) + len(deferred) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                if tag != "hyperlink":
                    continue
            text = {"t": node.text, "delText": node.text, "tab": "\t"}.get(tag)
            if tag in {"br", "cr"}:
                text = "\n"
            if tag in {"t", "delText", "tab", "br", "cr"}:
                if not text:
                    continue
                kind = "d" if state or tag == "delText" else "t"
                if kind != pending_kind:
                    close()
                    pending_kind = kind
                    if len(items) > MAX_SEGMENTS:
                        raise OOXMLInvalid("OOXML extraction has too many segments")
                pending.append(text)
                continue
            inner, level = _revised(tag, state, depth)
            stack.append((iter(node), inner, level))
            break
        else:
            stack.pop()
    close()
    return items


def _docx_styles(package: _Package) -> dict[str, tuple[str, str | None, bool, bool]]:
    """Map style id to (name, basedOn, outline-level heading, numbered)."""
    part = "word/_rels/document.xml.rels"
    types = package.relationship_types.get(part, {})
    target = next(
        (
            path
            for relation_id, (path, external) in package.relationships.get(
                part, {}
            ).items()
            if not external and types.get(relation_id, "").endswith("/styles")
        ),
        None,
    )
    styles: dict[str, tuple[str, str | None, bool, bool]] = {}
    if target is None:
        return styles
    for style in package.xml(target).iter():
        style_id = _attr(style, "styleId")
        if _local(style.tag) != "style" or not style_id:
            continue
        name, based_on = style_id, None
        outline = numbered = False
        for child in style:
            tag = _local(child.tag)
            if tag == "name":
                name = _attr(child, "val") or name
            elif tag == "basedOn":
                based_on = _attr(child, "val")
            elif tag == "pPr":
                outline, numbered = _paragraph_properties(child)
        styles[style_id] = (name, based_on, outline, numbered)
    return styles


def _paragraph_properties(properties: ElementTree.Element) -> tuple[bool, bool]:
    """Return (outline-level heading, numbered) for one ``pPr``."""
    outline = numbered = False
    for item in properties:
        tag = _local(item.tag)
        if tag == "outlineLvl":
            level = _attr(item, "val") or ""
            outline = level.isdigit() and int(level) < 9
        elif tag == "numPr":
            numbered = any(
                _local(number.tag) == "numId"
                and _attr(number, "val") not in {None, "0"}
                for number in item
            )
    return outline, numbered


def _paragraph_role(
    paragraph: ElementTree.Element,
    styles: dict[str, tuple[str, str | None, bool, bool]],
) -> str:
    """Classify a main-document paragraph as heading, list item or body."""
    properties = next((item for item in paragraph if _local(item.tag) == "pPr"), None)
    if properties is None:
        return "body"
    outline, numbered = _paragraph_properties(properties)
    direct_outline = any(_local(item.tag) == "outlineLvl" for item in properties)
    style_id = next(
        (_attr(item, "val") for item in properties if _local(item.tag) == "pStyle"),
        None,
    )
    heading = outline
    seen: set[str] = set()
    while style_id and style_id not in seen and len(seen) < 16:
        seen.add(style_id)
        name, based_on, style_outline, style_numbered = styles.get(
            style_id, (style_id, None, False, False)
        )
        heading = heading or (
            not direct_outline
            and (
                style_outline
                or _HEADING_STYLE.fullmatch(name.casefold().replace(" ", ""))
                is not None
            )
        )
        numbered = numbered or style_numbered
        style_id = based_on
    if heading:
        return "heading"
    return "list_item" if numbered else "body"


def _attr(element: ElementTree.Element, name: str) -> str | None:
    return next(
        (value for key, value in element.attrib.items() if _local(key) == name), None
    )


def _docx_story(part: str) -> str:
    name = posixpath.basename(part)
    for prefix in ("header", "footer", "footnote", "endnote", "comment"):
        if name.startswith(prefix):
            return prefix
    return "body"


def _relationship_id(attributes: dict[str, str]) -> str:
    namespaced = next(
        (
            value
            for key, value in attributes.items()
            if key.startswith("{") and _local(key) == "id"
        ),
        None,
    )
    return namespaced or attributes.get("id", "")


def _clip(value: str, limit: int) -> str:
    return value[:limit]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _locator(capture_id: str, **values: object) -> dict[str, object]:
    return {"captureId": capture_id, **values}


def _bounded(segments: Iterable[ExtractedSegment]) -> None:
    if isinstance(segments, list) and len(segments) > MAX_SEGMENTS:
        raise OOXMLInvalid("OOXML extraction has too many segments")
