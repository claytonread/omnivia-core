"""Bounded, inert structural readers for admitted Office Open XML packages."""

from __future__ import annotations

import datetime
import io
import posixpath
import stat
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
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
            self.relationships[name] = entries

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

    def xml(self, name: str) -> ElementTree.Element:
        data = self.bytes(name)
        upper = data.upper()
        if any(marker in upper for marker in _FORBIDDEN_XML):
            raise OOXMLInvalid("OOXML XML contains a forbidden declaration")
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError as error:
            raise OOXMLInvalid("OOXML XML is malformed") from error


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
        segments: list[ExtractedSegment] = []
        blocks = 0
        for part in parts:
            root = package.xml(part)
            story = _docx_story(part)
            for element in root.iter():
                if _local(element.tag) != "p":
                    continue
                blocks += 1
                text = "".join(
                    node.text or ""
                    for node in element.iter()
                    if _local(node.tag) in {"t", "tab", "br"}
                )
                if not text:
                    continue
                deleted = any(
                    _local(node.tag) in {"del", "delText", "moveFrom"}
                    for node in element.iter()
                )
                role = (
                    "deleted_text"
                    if deleted
                    else (
                        "note"
                        if story in {"footnote", "endnote"}
                        else "comment"
                        if story == "comment"
                        else "body"
                    )
                )
                segments.append(
                    ExtractedSegment(
                        role,
                        _locator(capture_id, part=part, story=story, block=blocks),
                        text=text,
                        visibility="annotation"
                        if role in {"deleted_text", "note", "comment"}
                        else "visible",
                        revision_state="deleted" if deleted else "current",
                    )
                )
                _bounded(segments)
        return OOXMLExtraction(
            tuple(segments),
            "document_block",
            blocks,
            {
                "packageMembers": len(package.names),
                "externalReferences": package.external_relationships,
            },
        )


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


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _locator(capture_id: str, **values: object) -> dict[str, object]:
    return {"captureId": capture_id, **values}


def _bounded(segments: Iterable[ExtractedSegment]) -> None:
    if isinstance(segments, list) and len(segments) > MAX_SEGMENTS:
        raise OOXMLInvalid("OOXML extraction has too many segments")
