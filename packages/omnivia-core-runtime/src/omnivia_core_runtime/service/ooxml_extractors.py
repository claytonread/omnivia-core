"""Bounded, inert structural readers for admitted Office Open XML packages."""

from __future__ import annotations

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
MAX_XML_BYTES: Final = 64 * 1024 * 1024
MAX_MEMBER_RATIO: Final = 200
MAX_SEGMENTS: Final = 100_000
_ALLOWED_COMPRESSION: Final = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_FORBIDDEN_XML: Final = (b"<!DOCTYPE", b"<!ENTITY")


class OOXMLInvalid(ValueError):
    """The package violates the bounded OOXML acceptance profile."""


@dataclass(frozen=True, slots=True)
class OOXMLExtraction:
    segments: tuple[ExtractedSegment, ...]
    unit_type: str
    expected: int
    limitations: dict[str, object]


class _Package:
    def __init__(self, content: bytes) -> None:
        if len(content) > MAX_SOURCE_BYTES:
            raise OOXMLInvalid("OOXML source exceeds the accepted bound")
        self._buffer = io.BytesIO(content)
        try:
            self.archive = zipfile.ZipFile(self._buffer)
        except (zipfile.BadZipFile, OSError) as error:
            raise OOXMLInvalid("OOXML source is not a valid ZIP package") from error
        self.names = frozenset(self.archive.namelist())
        self.names = self._validate()

    def close(self) -> None:
        self.archive.close()
        self._buffer.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _validate(self) -> frozenset[str]:
        infos = self.archive.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise OOXMLInvalid(
                "OOXML package member count is outside the accepted bound"
            )
        identities: set[str] = set()
        names: set[str] = set()
        expanded = 0
        for info in infos:
            name = info.filename
            normal = unicodedata.normalize("NFC", name)
            identity = normal.casefold()
            parts = normal.split("/")
            mode = info.external_attr >> 16
            if (
                not name
                or "\x00" in name
                or "\\" in name
                or name.startswith("/")
                or normal != name
                or any(part in {"", ".", ".."} for part in parts)
                or identity in identities
                or info.flag_bits & 0x1
                or info.compress_type not in _ALLOWED_COMPRESSION
                or stat.S_ISLNK(mode)
            ):
                raise OOXMLInvalid(
                    "OOXML package contains an unsafe or ambiguous member"
                )
            identities.add(identity)
            names.add(name)
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise OOXMLInvalid("OOXML package expansion exceeds the accepted bound")
            if (
                info.file_size > 1024 * 1024
                and info.file_size > max(info.compress_size, 1) * MAX_MEMBER_RATIO
            ):
                raise OOXMLInvalid("OOXML package member expansion ratio is unsafe")
            actual = 0
            try:
                with self.archive.open(info, "r") as member:
                    while chunk := member.read(1024 * 1024):
                        actual += len(chunk)
                        if actual > info.file_size or actual > MAX_EXPANDED_BYTES:
                            raise OOXMLInvalid(
                                "OOXML member exceeded its declared bound"
                            )
            except (zipfile.BadZipFile, RuntimeError, OSError) as error:
                raise OOXMLInvalid("OOXML member could not be validated") from error
            if actual != info.file_size:
                raise OOXMLInvalid(
                    "OOXML member length does not match its directory entry"
                )
        if "[Content_Types].xml" not in names:
            raise OOXMLInvalid("OOXML content-types part is missing")
        content_types = self.xml("[Content_Types].xml")
        for element in content_types.iter():
            content_type = element.attrib.get("ContentType", "").casefold()
            part_name = element.attrib.get("PartName", "").casefold()
            if (
                "macroenabled" in content_type
                or "vbaproject" in content_type
                or "vbaproject" in part_name
            ):
                raise OOXMLInvalid("macro-enabled Office packages are not supported")
        return frozenset(names)

    def bytes(self, name: str, *, limit: int = MAX_XML_BYTES) -> bytes:
        if name not in self.names and name != "[Content_Types].xml":
            raise OOXMLInvalid("OOXML relationship names a missing package part")
        info = self.archive.getinfo(name)
        if info.file_size > limit:
            raise OOXMLInvalid("OOXML XML member exceeds the accepted bound")
        data = self.archive.read(info)
        if len(data) != info.file_size:
            raise OOXMLInvalid("OOXML member changed during validation")
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
                "externalReferences": _external_relationships(package),
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
                "externalReferences": _external_relationships(package),
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
                "externalReferences": _external_relationships(package),
            },
        )


def _relationships(
    package: _Package, name: str, base: str
) -> dict[str, tuple[str, bool]]:
    if name not in package.names:
        return {}
    result: dict[str, tuple[str, bool]] = {}
    for relation in package.xml(name).iter():
        if _local(relation.tag) != "Relationship":
            continue
        relation_id = relation.attrib.get("Id")
        target = relation.attrib.get("Target")
        external = relation.attrib.get("TargetMode") == "External"
        if not relation_id or target is None:
            raise OOXMLInvalid("OOXML relationship is malformed")
        if external:
            result[relation_id] = (target, True)
            continue
        joined = posixpath.normpath(posixpath.join(base, target))
        if joined.startswith(("../", "/")) or joined not in package.names:
            raise OOXMLInvalid("OOXML relationship escapes or misses the package")
        result[relation_id] = (joined, False)
    return result


def _external_relationships(package: _Package) -> int:
    total = 0
    for name in package.names:
        if not name.endswith(".rels"):
            continue
        data = package.bytes(name)
        upper = data.upper()
        if any(marker in upper for marker in _FORBIDDEN_XML):
            raise OOXMLInvalid(
                "OOXML relationship XML contains a forbidden declaration"
            )
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as error:
            raise OOXMLInvalid("OOXML relationship XML is malformed") from error
        total += sum(
            1
            for relation in root.iter()
            if _local(relation.tag) == "Relationship"
            and relation.attrib.get("TargetMode") == "External"
        )
    return total


def _xlsx_sheets(package: _Package) -> list[tuple[str, str, str, bool]]:
    relations = _relationships(package, "xl/_rels/workbook.xml.rels", "xl")
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
    relations = _relationships(package, "ppt/_rels/presentation.xml.rels", "ppt")
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
