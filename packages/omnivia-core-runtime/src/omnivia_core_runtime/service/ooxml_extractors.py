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
        for sheet_name, sheet_id, part, hidden in sheets:
            root = package.xml(part)
            for cell in (item for item in root.iter() if _local(item.tag) == "c"):
                coordinate = cell.attrib.get("r")
                if not coordinate:
                    raise OOXMLInvalid("XLSX cell has no coordinate")
                formula = next(
                    (node.text or "" for node in cell if _local(node.tag) == "f"), None
                )
                value = next(
                    (node.text or "" for node in cell if _local(node.tag) == "v"), None
                )
                inline = "".join(
                    node.text or "" for node in cell.iter() if _local(node.tag) == "t"
                )
                if formula is None and value is None and not inline:
                    continue
                populated += 1
                locator = _locator(
                    capture_id,
                    part=part,
                    sheetId=sheet_id,
                    sheet=sheet_name,
                    cell=coordinate,
                )
                visibility = "hidden" if hidden else "visible"
                if formula is not None:
                    segments.append(
                        ExtractedSegment(
                            "formula",
                            locator,
                            structured_value={
                                "expression": formula,
                                "cacheFreshness": "unknown",
                            },
                            visibility=visibility,
                        )
                    )
                    if value is not None:
                        segments.append(
                            ExtractedSegment(
                                "saved_value",
                                locator,
                                structured_value={"raw": value, "freshness": "unknown"},
                                visibility=visibility,
                            )
                        )
                else:
                    decoded = _xlsx_value(cell.attrib.get("t"), value, inline, shared)
                    segments.append(
                        ExtractedSegment(
                            "table_cell",
                            locator,
                            structured_value={
                                "raw": value,
                                "decoded": decoded,
                                "cellType": cell.attrib.get("t"),
                            },
                            visibility=visibility,
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
            root = package.xml(part)
            shape_ordinal = 0
            for shape in (
                item
                for item in root.iter()
                if _local(item.tag) in {"sp", "graphicFrame"}
            ):
                text = "".join(
                    node.text or "" for node in shape.iter() if _local(node.tag) == "t"
                )
                if not text:
                    continue
                shape_ordinal += 1
                segments.append(
                    ExtractedSegment(
                        "body",
                        _locator(
                            capture_id,
                            part=part,
                            slideId=slide_id,
                            slide=ordinal,
                            shape=shape_ordinal,
                        ),
                        text=text,
                        visibility="hidden" if hidden else "visible",
                    )
                )
                _bounded(segments)
            notes = part.replace("slides/slide", "notesSlides/notesSlide")
            if notes in package.names:
                note_text = "".join(
                    node.text or ""
                    for node in package.xml(notes).iter()
                    if _local(node.tag) == "t"
                )
                if note_text:
                    segments.append(
                        ExtractedSegment(
                            "note",
                            _locator(
                                capture_id, part=notes, slideId=slide_id, slide=ordinal
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


def _xlsx_sheets(package: _Package) -> list[tuple[str, str, str, bool]]:
    relations = package.relationships.get("xl/_rels/workbook.xml.rels", {})
    result: list[tuple[str, str, str, bool]] = []
    for sheet in package.xml("xl/workbook.xml").iter():
        if _local(sheet.tag) != "sheet":
            continue
        relation_id = _relationship_id(sheet.attrib)
        relation = relations.get(relation_id)
        if relation is None or relation[1]:
            raise OOXMLInvalid("XLSX sheet relationship is missing or external")
        result.append(
            (
                sheet.attrib.get("name", ""),
                sheet.attrib.get("sheetId", ""),
                relation[0],
                sheet.attrib.get("state", "visible") != "visible",
            )
        )
    if len(result) > 100:
        raise OOXMLInvalid("XLSX has too many sheets")
    return result


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


def _shared_strings(package: _Package) -> list[str]:
    if "xl/sharedStrings.xml" not in package.names:
        return []
    return [
        "".join(node.text or "" for node in item.iter() if _local(node.tag) == "t")
        for item in package.xml("xl/sharedStrings.xml")
        if _local(item.tag) == "si"
    ]


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
        relation_id = ids[0][1]
        entry = self._relations.get(relation_id)
        if entry is None:
            raise OOXMLInvalid("DOCX hyperlink names a missing relationship")
        relation_type = self._relation_types.get(relation_id, "")
        if not relation_type.endswith("/hyperlink"):
            raise OOXMLInvalid("DOCX hyperlink names a relationship of another type")
        target, external = entry
        value.update(
            relationshipId=_clip(relation_id, _MAX_META_CHARS),
            relationshipType=_clip(relation_type, _MAX_TARGET_CHARS),
            target=_clip(target, _MAX_TARGET_CHARS),
            targetTruncated=len(target) > _MAX_TARGET_CHARS,
        )
        if external:
            # Verbatim and inert: never fetched, opened or normalised.
            match = _SCHEME.match(target)
            value["scheme"] = match.group(1).casefold() if match else ""
        else:
            value["resolved"] = True
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
