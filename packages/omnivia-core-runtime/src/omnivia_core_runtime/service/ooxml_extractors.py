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
# The two run properties that hide a run's text: from the page and from the
# web view. Either one hides the run, so neither is read as visible text.
_HIDDEN_RUN_PROPERTIES: Final = frozenset({"vanish", "webHidden"})
_INSERTED: Final = frozenset({"ins", "moveTo"})
_REMOVED: Final = frozenset({"del", "moveFrom"})
_MAX_REVISION_DEPTH: Final = 4
# OmniVia's deterministic safety bound on complex field nesting: a deeper stack
# is refused rather than read, so one file cannot drive unbounded recursion.
_MAX_DOCX_FIELD_DEPTH: Final = 20
# Math is identified by namespace and never by local name, so an element merely
# called ``oMath`` or ``t`` in another vocabulary is not read as math.
_MATH_NAMESPACE: Final = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_MATH_OBJECT: Final = f"{{{_MATH_NAMESPACE}}}oMath"
_MATH_PARA: Final = f"{{{_MATH_NAMESPACE}}}oMathPara"
_MATH_TEXT: Final = f"{{{_MATH_NAMESPACE}}}t"
# Either form of math is one equation kept whole: an ``oMathPara`` is the
# display block Word wrote and holds the ``oMath`` children of that one
# equation, which are therefore never recorded again on their own.
_MATH_CONTENT: Final = frozenset({_MATH_OBJECT, _MATH_PARA})
# OmniVia's own bound on one equation's markup and direct text together, well
# under the 131072-byte stored segment value cap so JSON escaping of the markup
# cannot push the recorded value past it.
_MAX_DOCX_EQUATION_BYTES: Final = 32 * 1024
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
# The relationship and content types that together qualify the two parts
# classic slide comments live in: the per-slide comment list, and the
# presentation-wide author table it indexes into. As with the notes-slide type,
# only the two URIs OOXML itself defines qualify -- Transitional and Strict --
# so nothing reaches either part by ending the same way. Threaded ("modern")
# comments are a different relationship and content type entirely and so reach
# nothing here.
_COMMENTS_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/comments",
    }
)
_AUTHORS_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/commentAuthors"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/commentAuthors",
    }
)
# Compared against ``declared_type``, which case-folds, so both are written
# folded: the authored spelling is ``...presentationml.commentAuthors+xml``.
_COMMENTS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.comments+xml"
)
_AUTHORS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.commentauthors+xml"
)
# The PresentationML main namespaces the two comment parts are written in. The
# rest of this module reads local names only, but a comment part is reached by
# its declared type alone, so the namespace is the one remaining check that the
# part holds the vocabulary the declaration promised.
_PRESENTATIONML: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/presentationml/2006/main",
        "http://purl.oclc.org/ooxml/presentationml/main",
    }
)
# The relationship and content types that together qualify the two parts modern
# ("threaded") comments live in: the per-slide comment list, and the
# presentation-wide author table it cites. These are a Microsoft extension, so
# each type has one defined URI rather than a Strict/Transitional pair, and
# neither the classic types above nor these reach the other's parts.
#
# Unlike the notes and classic comment parts, the relationship alone does not
# reach the modern list: a slide has modern comments only where it says so in
# its own ``p:extLst``, in the extension below, and the relationship it names
# there is the one read. A slide that advertises nothing has no modern comments
# however it relates, and a slide that advertises the extension has to carry
# the one relationship it names.
_MODERN_COMMENTS_RELATIONSHIPS: Final = frozenset(
    {"http://schemas.microsoft.com/office/2018/10/relationships/comments"}
)
_MODERN_AUTHORS_RELATIONSHIPS: Final = frozenset(
    {"http://schemas.microsoft.com/office/2018/10/relationships/authors"}
)
_MODERN_COMMENTS_CONTENT_TYPE: Final = "application/vnd.ms-powerpoint.comments+xml"
_MODERN_AUTHORS_CONTENT_TYPE: Final = "application/vnd.ms-powerpoint.authors+xml"
# The one namespace both modern parts, and the slide extension that routes to
# the comment list, are written in. As with the classic parts, a part is
# reached by its declared type alone, so the namespace is the remaining check
# that it holds the vocabulary the declaration promised.
_MODERN_MAIN: Final = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
_MODERN_NAMESPACE: Final = frozenset({_MODERN_MAIN})
_MODERN_COMMENT_REL: Final = f"{{{_MODERN_MAIN}}}commentRel"
# The slide extension a modern comment list is named from, and the namespaces
# the relationship id inside it may be written in -- the Transitional and
# Strict relationships namespaces, the same pair every ``r:id`` in the package
# uses. The extension URI is an ``ST_Guid``, so it is matched case-folded for
# the reason an author id is: only its hex digits vary in case.
_MODERN_COMMENTS_EXTENSION: Final = "{6950bfc3-d8da-4a85-94f7-54da5524770b}"
_RELATIONSHIP_NAMESPACES: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)
# The task fields a modern comment raised as work carries, each kept exactly
# as written: a date stays a string, and a completion figure is not read as
# progress anything made. A reply is not work in its own right, so these are a
# comment's claims alone.
_MODERN_COMMENT_TASK_CLAIMS: Final = (
    "title",
    "assignedTo",
    "startDate",
    "dueDate",
    "complete",
)
# ``ST_CommentStatus`` is a closed enumeration, so a comment or reply claims
# one of exactly these three states or the file claims none. Unlike the free
# text above, a status outside the set is refused rather than carried through:
# anything reading status reads it to decide whether a comment is still open,
# and a state nothing defines would be answered with whatever that reader
# defaults to. An absent status stays absent, as the schema allows -- that says
# only that the file never claimed a state, not that it claimed "active".
_MODERN_COMMENT_STATUS: Final = frozenset({"active", "resolved", "closed"})
# ``CT_CommentReply`` is not ``CT_Comment``. A reply holds a body and the
# extension point, and carries its identity, the author it cites, the state it
# claims and when it claims to have been written. A position, an anchor, a
# thread of its own and the task fields are a comment's grammar appearing where
# the schema does not place it, so a reply writing any of them is refused
# rather than read as a comment -- reporting a reply's anchor or due date would
# be publishing a field the syntax never legally carried.
_MODERN_REPLY_CHILDREN: Final = frozenset({"txBody", "extLst"})
_MODERN_REPLY_ATTRIBUTES: Final = frozenset({"id", "authorId", "status", "created"})
# The anchors a modern comment may declare, each naming what kind of thing it
# is anchored to: a drawing element, a text range, a slide, the document, or --
# where the file itself cannot say -- nothing it can name. The monikers inside
# an anchor are not read: the kind is what places the comment, and descending
# into a moniker would mean resolving the element it names.
_MODERN_ANCHORS: Final = frozenset(
    {"unknownAnchor", "deMkLst", "txMkLst", "sldMkLst", "docMkLst"}
)
# Every author field defaulted, so a comment citing an author the table never
# defines carries the same keys as one whose author it does define.
_MODERN_AUTHOR_UNKNOWN: Final[dict[str, object]] = {
    "authorPart": None,
    "authorName": None,
    "authorInitials": None,
    "authorUserId": None,
    "authorProviderId": None,
    # Stated so no consumer reads the fields above as identity.
    "authorAuthenticated": False,
}
# The relationship and content types that together qualify a DrawingML chart
# part. As with the notes-slide and comment types, only the two URIs OOXML
# itself defines qualify -- Transitional and Strict -- so nothing reaches a
# chart by ending the same way.
_CHART_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/chart",
    }
)
_CHART_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
)
# The two DrawingML chart namespaces a chart part may be written in. The chart
# part also holds ``a:`` drawing vocabulary for its rich text, so only the root
# is held to these; everything below it is read by local name as elsewhere.
_CHART_NAMESPACES: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/chart",
        "http://purl.oclc.org/ooxml/drawingml/chart",
    }
)
# The place every chart record names, defaulted so each one carries the same
# keys whether or not the record it describes sits inside a series, a level or a
# cache.
_CHART_PLACE: Final[dict[str, object]] = {
    "seriesIndex": None,
    "seriesOrder": None,
    "categoryLevel": None,
    "pointIndex": None,
    "formula": None,
}
# ``c:tx`` is the series name, ``c:cat`` its categories, ``c:val`` its values.
_CHART_SLOTS: Final = (
    ("tx", "chart_series_name"),
    ("cat", "chart_category"),
    ("val", "chart_value"),
)
# The reference shapes a slot may hold, each mapped to the one cache element it
# keeps its stored points in. A reference names a source and caches what that
# source once held; the cache is the only place its values are read from, and a
# reference whose cache is of another kind is not the shape its name promised.
_CHART_REFERENCES: Final = {
    "strRef": "strCache",
    "numRef": "numCache",
    "multiLvlStrRef": "multiLvlStrCache",
}
# The literal shapes a slot may hold: a list of points written inline, naming no
# source at all. ``c:v`` on its own is the single literal a series name may be.
_CHART_LITERALS: Final = frozenset({"strLit", "numLit"})
_CHART_SOURCES: Final = frozenset({"v", *_CHART_REFERENCES, *_CHART_LITERALS})
# The chart bounds are the reader's own rather than any one format's: the same
# bounded reader serves a PPTX graphic frame's chart and a DOCX drawing's, so
# both are held to these.
_MAX_CHART_SERIES: Final = 1_000
# Counted across one chart part, over every cache and literal list it holds.
_MAX_CHART_POINTS: Final = 100_000
# A level is work a multi-level cache asks for whether or not it holds a point,
# so the levels are bounded before any of them is read.
_MAX_CHART_CATEGORY_LEVELS: Final = 256
_MAX_CHART_TITLE_RUNS: Final = 20_000
# The two DrawingML diagram namespaces SmartArt is written in. One of them is
# what a graphic frame's ``a:graphicData/@uri`` carries to say the graphic is
# SmartArt, and the same URI is the namespace the data model part is rooted in.
# Namespace URIs compare as strings under the XML Namespaces specification, so
# the match is exact and nothing reaches a diagram by resembling one.
_DIAGRAM_NAMESPACES: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/diagram",
        "http://purl.oclc.org/ooxml/drawingml/diagram",
    }
)
# The one route a graphic frame declares a graphic by: ``a:graphic`` directly
# below the frame, then the ``a:graphicData`` directly below that, both in the
# DrawingML main namespace -- Transitional or Strict. Matching the qualified
# tags means a ``graphicData`` written in some other vocabulary, or nested
# anywhere else below the frame (an alternate-content fallback, another frame's
# graphic carried along, a body of text naming one), is not on the route and
# declares nothing.
_DRAWING_GRAPHIC: Final = frozenset(
    f"{{{namespace}}}graphic"
    for namespace in (
        "http://schemas.openxmlformats.org/drawingml/2006/main",
        "http://purl.oclc.org/ooxml/drawingml/main",
    )
)
_DRAWING_GRAPHIC_DATA: Final = frozenset(
    tag + "Data" for tag in _DRAWING_GRAPHIC
)
# The one element a graphic names its chart on, in the two DrawingML chart
# namespaces OOXML defines -- the same pair a chart part is rooted in. A
# ``chart`` element written in any other vocabulary shares a local name and
# nothing else, and names no chart.
_DRAWING_CHART: Final = frozenset(
    f"{{{namespace}}}chart" for namespace in _CHART_NAMESPACES
)
# The relationship and content types that together qualify a diagram data part:
# the one part SmartArt keeps its points and connections in. As with the chart
# and comment types, only the two URIs OOXML itself defines qualify --
# Transitional and Strict -- so nothing reaches a data model by ending the same
# way. The layout, quick-style and colour relationships a ``dgm:relIds`` names
# beside the data are other types entirely and so reach nothing here.
_DIAGRAM_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/diagramData"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/diagramData",
    }
)
# Compared against ``declared_type``, which case-folds, so it is written folded:
# the authored spelling is ``...drawingml.diagramData+xml``.
_DIAGRAM_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.drawingml.diagramdata+xml"
)
# ``ST_PtType`` and ``ST_CxnType`` are closed enumerations, so a point or a
# connection claims one of exactly these kinds or the file claims none and the
# schema's own default stands. Unlike free text, a kind outside the set is
# refused rather than carried through: anything reading a kind reads it to
# decide what the record is, and a kind nothing defines would be answered with
# whatever that reader defaults to.
_DIAGRAM_POINT_TYPES: Final = frozenset(
    {"node", "parTrans", "sibTrans", "pres", "doc", "asst"}
)
_DIAGRAM_CONNECTION_TYPES: Final = frozenset(
    {"parOf", "presOf", "presParOf", "unknownRelationship"}
)
# Counted across one diagram data part, for the reason the slide-wide paragraph
# and run bounds are: a point's own indexes are local to it and bound nothing.
# The bounds are the reader's own rather than any one format's: the same bounded
# reader serves a PPTX graphic frame's SmartArt and a DOCX drawing's, so both
# are held to these.
_MAX_DIAGRAM_POINTS: Final = 10_000
_MAX_DIAGRAM_CONNECTIONS: Final = 20_000
_MAX_DIAGRAM_PARAGRAPHS: Final = 20_000
_MAX_DIAGRAM_RUNS: Final = 100_000
_MAX_DIAGRAM_CHARS: Final = 100_000
# The author's own ``cNvPr`` claims about a picture, each paired with the field
# the inventory record keeps it in. Three distinct claims, kept distinct: a
# ``name`` is what the authoring tool called the object and is usually one it
# generated, while a ``title`` and a ``descr`` are words someone wrote to
# describe the image, so neither stands in for the other.
_PICTURE_CLAIMS: Final = (
    ("name", "shapeName"),
    ("title", "altTitle"),
    ("descr", "altDescription"),
)
# The two of those claims that are alt text, each with the ``altRole`` the
# segment carrying it records, in the order they are reported. ``name`` is
# absent on purpose: an autogenerated shape name is not a description of
# anything, so it is never reported as text the author wrote.
_ALT_CLAIMS: Final = (("altTitle", "title"), ("altDescription", "description"))
# The relationship type that qualifies a part as the image a picture shows. As
# with every other type here, only the two URIs OOXML defines qualify --
# Transitional and Strict -- so nothing reaches an image by ending the same way,
# and a picture naming an OLE object, a media part or a hyperlink names no image.
_IMAGE_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/image",
    }
)
# The one element a picture names its image on, in the two DrawingML main
# namespaces OOXML defines. A lookalike in another vocabulary names nothing.
_DRAWING_BLIP: Final = frozenset(
    f"{{{namespace}}}blip"
    for namespace in (
        "http://schemas.openxmlformats.org/drawingml/2006/main",
        "http://purl.oclc.org/ooxml/drawingml/main",
    )
)
# An image part's content type is normally carried by its extension rather than
# named outright, so it is resolved through ``Default`` as well as ``Override``.
# What it then has to resolve to is an image: a blip that embeds an OLE object,
# a script or a document is not showing a picture, and inventorying it as one
# would say the package holds an image where it holds something else entirely.
# The subtype is not narrowed -- PNG, JPEG, GIF, TIFF, EMF, WMF and SVG are all
# legitimate here -- because nothing in this slice decodes the bytes, so the
# subtype is the file's own claim and is reported as written.
_IMAGE_CONTENT_PREFIX: Final = "image/"
# What a picture records about the image it names, defaulted so every picture's
# record carries the same keys whether it embeds an image, links one, does both
# or names none at all. The embedded half describes a part inside the package;
# the linked half describes a reference out of it, kept verbatim. ``None`` on
# ``imageLinkExternal`` says the picture declared no link, which is not the same
# as a link the package declared internal.
_DRAWING_IMAGE: Final[dict[str, object]] = {
    "imageEmbedRelationshipId": None,
    "imageEmbedRelationshipType": None,
    "imagePart": None,
    "imageContentType": None,
    "imageBytes": None,
    "imageLinkRelationshipId": None,
    "imageLinkRelationshipType": None,
    "imageLinkTarget": None,
    "imageLinkScheme": None,
    "imageLinkExternal": None,
}
# The two WordprocessingDrawing namespaces OOXML defines, Transitional and
# Strict. A drawing object is reached by its qualified name alone, so an
# element merely called ``inline``, ``anchor`` or ``docPr`` in some other
# vocabulary is not one and nothing is read off it.
_WORDPROCESSING_DRAWING: Final = (
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing",
)
# The two DrawingML objects a Word story anchors, each mapped to the placement
# it declares: ``wp:inline`` sits in the text flow, ``wp:anchor`` is positioned
# against the page and the text wraps around it. The placement is the element's
# own, never inferred from a wrap or a position child.
_DOCX_DRAWINGS: Final[dict[str, str]] = {
    f"{{{namespace}}}{element}": element
    for element in ("inline", "anchor")
    for namespace in _WORDPROCESSING_DRAWING
}
_DOCX_DOC_PR: Final = frozenset(
    f"{{{namespace}}}docPr" for namespace in _WORDPROCESSING_DRAWING
)
# The graphics a drawing object may declare, each keyed by the paragraph item
# kind it is walked as and named as a refusal calls it. Three distinct
# declarations on one route, so an object making two of them is refused by the
# pair it made, and a field instruction holding one is refused by its name.
_DOCX_GRAPHICS: Final[dict[str, str]] = {
    "g": "an image",
    "k": "a chart",
    "s": "SmartArt",
}
# The two WordprocessingML main namespaces OOXML defines, Transitional and
# Strict. The rest of the DOCX reader matches local names, because a story's own
# vocabulary is settled by the part it was reached through; the two legacy
# containers below are admitted on their qualified names alone, so an element
# merely called ``object`` or ``pict`` in another vocabulary declares nothing.
_WORDPROCESSINGML: Final = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
# The legacy object container.
_DOCX_OBJECTS: Final = frozenset(
    f"{{{namespace}}}object" for namespace in _WORDPROCESSINGML
)
# The legacy picture container. It is where Word writes a VML image that stands
# on its own rather than for an object, and it is also the wrapper a VML text
# box arrives in, so admitting the container is not yet admitting an image.
_DOCX_PICTURES: Final = frozenset(
    f"{{{namespace}}}pict" for namespace in _WORDPROCESSINGML
)
# The one element inside that container which declares the embedded or linked
# object. It is an Office drawing extension rather than part of either OOXML
# profile, so it has a single namespace and no Strict spelling: the same local
# name anywhere else is another vocabulary's element and declares no object.
_DOCX_OLE_OBJECT: Final = "{urn:schemas-microsoft-com:office:office}OLEObject"
# The legacy drawing layer's own vocabulary. It predates both OOXML profiles, so
# each element has a single namespace and no Strict spelling: the same local name
# anywhere else is another vocabulary's element and declares nothing.
#
# A ``v:shape`` is one legacy shape, and the ``v:imagedata`` inside it is the
# one place that shape names the image it displays.
_DOCX_VML_SHAPE: Final = "{urn:schemas-microsoft-com:vml}shape"
_DOCX_VML_IMAGEDATA: Final = "{urn:schemas-microsoft-com:vml}imagedata"
# The Office title an ``imagedata`` may carry about the image itself. It is an
# Office drawing extension rather than VML, and it is a third claim distinct
# from the shape's own two.
_DOCX_VML_TITLE: Final = "{urn:schemas-microsoft-com:office:office}title"
# The VML shape a legacy object carries its on-page stand-in as -- the same
# element a ``w:pict`` shows a standalone image through, reached by a route of
# its own. Below an object it is a picture *of* the object, not the object: it is
# recorded as declared and is never opened, decoded, resolved or inventoried as
# an image, because the image a story shows is the one its own ``w:pict`` names.
_DOCX_OLE_PREVIEW: Final = _DOCX_VML_SHAPE
# The one ``style`` declaration that moves a legacy shape off the text flow,
# written exactly as CSS writes it. Neither half is folded, trimmed to a prefix
# or otherwise interpreted: ``POSITION``, ``Absolute`` and ``absolutely`` are not
# these tokens, so a shape declaring one of those is the inline shape it is by
# default rather than an anchored one.
_DOCX_VML_POSITION: Final = "position"
_DOCX_VML_ABSOLUTE: Final = "absolute"
# What a standalone legacy image records, defaulted so an embedded part and an
# external link are shaped alike, exactly as a legacy object's record is. One
# ``imagedata`` names one relationship, so there is one reference here rather
# than the embed/link pair a DrawingML blip may write. ``scheme`` is the scheme
# a linked target's own text names; the three part fields describe a member the
# package holds, and the byte length is the figure the package measured while
# validating it. Reporting any of it opens nothing.
#
# The last five are the shape's own uninterpreted markup: the identifier the
# legacy drawing layer knew it by, which is resolved against nothing, the style
# it was drawn with, kept as written, and the three claims its author may have
# made about what the image shows, each kept apart from the others.
_DOCX_VML_RECORD: Final[dict[str, object]] = {
    "placement": None,
    "relationshipId": None,
    "relationshipType": None,
    "target": None,
    "external": None,
    "scheme": None,
    "imagePart": None,
    "contentType": None,
    "imageBytes": None,
    "shapeId": None,
    "shapeStyle": None,
    "shapeAlt": None,
    "shapeTitle": None,
    "imageTitle": None,
}
# Those three claims, each with the ``altRole`` the segment carrying it records
# and the element that declared it, in the order they are reported. The role and
# the source are both needed to tell them apart: a shape's ``title`` and an
# ``imagedata``'s Office title are both titles, written on different elements
# about different things, and neither stands in for the other or for the alt
# text beside it.
_DOCX_VML_ALT_CLAIMS: Final = (
    ("shapeTitle", "title", "shape"),
    ("shapeAlt", "description", "shape"),
    ("imageTitle", "title", "imagedata"),
)
# ``OLEObject/@Type``, the only thing that says whether the object is embedded
# or linked, mapped to the mode its record reports. The value is closed and
# case-sensitive, so one differing in case is not that value: the relationship
# does not say which it is, and neither does the target's name or the ``ProgID``.
_DOCX_OLE_MODES: Final[dict[str, str]] = {"Embed": "embed", "Link": "link"}
# The object's own unqualified claims, each paired with the field its record
# keeps it in. All five are text Word wrote and none is interpreted: a
# ``ProgID`` is Word's own name for the server that would open the object and
# nothing here looks one up, resolves it or launches anything; a ``ShapeID`` and
# an ``ObjectID`` are identifiers the legacy drawing layer knew the shape by and
# neither is resolved against anything; a ``DrawAspect`` and an ``UpdateMode``
# say how Word would render and refresh the object, and nothing here renders or
# refreshes one.
_DOCX_OLE_CLAIMS: Final = (
    ("ProgID", "progId"),
    ("ShapeID", "shapeId"),
    ("DrawAspect", "drawAspect"),
    ("ObjectID", "objectId"),
    ("UpdateMode", "updateMode"),
)
# What a legacy object's record carries, defaulted so an embedded part and an
# external link are shaped alike. ``scheme`` is the scheme a linked target's own
# text names; the three part fields describe a member the package holds, and the
# byte length is the figure the package measured while validating it. Reporting
# any of it opens nothing: no object is ever parsed, decoded, executed, launched
# or fetched, and an object never contributes text.
_DOCX_OLE_RECORD: Final[dict[str, object]] = {
    "mode": None,
    "relationshipId": None,
    "relationshipType": None,
    "target": None,
    "external": None,
    "scheme": None,
    "objectPart": None,
    "contentType": None,
    "objectBytes": None,
    "progId": None,
    "shapeId": None,
    "drawAspect": None,
    "objectId": None,
    "updateMode": None,
    "previewDeclared": None,
}
# The DrawingML picture element, in the two picture namespaces OOXML defines.
# It is the one shape that declares a displayed image, so a chart, a diagram or
# an embedded object under the same ``a:graphicData`` is not reached through it.
_DRAWING_PICTURE: Final = frozenset(
    f"{{{namespace}}}pic"
    for namespace in (
        "http://schemas.openxmlformats.org/drawingml/2006/picture",
        "http://purl.oclc.org/ooxml/drawingml/picture",
    )
)
# The two holders a picture's media reference is reached through, each in the
# PresentationML namespaces every other PresentationML element here is held to:
# the picture's own non-visual properties, and the application properties
# inside them. That is the one route the schema puts a media reference on, so
# it is the only one read -- a reference hung off a group above the picture, a
# shape beside it, an extension list or anything deeper is not this picture's
# own declaration and is passed over.
_PPTX_PICTURE_NON_VISUAL: Final = frozenset(
    f"{{{namespace}}}nvPicPr" for namespace in _PRESENTATIONML
)
_PPTX_APPLICATION_PROPERTIES: Final = frozenset(
    f"{{{namespace}}}nvPr" for namespace in _PRESENTATIONML
)
# The media references this slice inventories, each paired with the role its
# record reports it under. Only a direct ``audioFile`` or ``videoFile``
# qualifies: an audio CD, a WAV reference and a QuickTime file are other
# declarations entirely, and the modern ``p14:media`` extension is a different
# route that nothing here follows.
#
# Both elements are DrawingML -- ``a:audioFile`` and ``a:videoFile`` -- even
# though the holders above them are PresentationML, so only the two DrawingML
# main namespaces qualify: Transitional and Strict. Anything outside that pair,
# the PresentationML spelling of the same local name included, shares a local
# name and nothing else, and names no media.
_PPTX_MEDIA_ELEMENTS: Final[dict[str, str]] = {
    f"{{{namespace}}}{element}": role
    for element, role in (("audioFile", "audio"), ("videoFile", "video"))
    for namespace in (
        "http://schemas.openxmlformats.org/drawingml/2006/main",
        "http://purl.oclc.org/ooxml/drawingml/main",
    )
}
# The relationship type each of those references has to carry, keyed by the
# role declaring it. As everywhere else here only the two URIs OOXML defines
# qualify -- Transitional and Strict -- so nothing reaches a media part by
# ending the same way, and the audio type never qualifies a video reference or
# the reverse: an element says which of the two it is, and the relationship has
# to agree with it.
_PPTX_MEDIA_RELATIONSHIPS: Final[dict[str, frozenset[str]]] = {
    "audio": frozenset(
        {
            (
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                "/audio"
            ),
            "http://purl.oclc.org/ooxml/officeDocument/relationships/audio",
        }
    ),
    "video": frozenset(
        {
            (
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                "/video"
            ),
            "http://purl.oclc.org/ooxml/officeDocument/relationships/video",
        }
    ),
}
# What a picture's media record carries, defaulted so an internal part and an
# external link are shaped alike. The first four are what every reference
# declares; ``mediaScheme`` is the scheme an external target's own text names,
# and the last three describe a part the package holds. The byte length is the
# figure the package measured while validating the member, so reporting it
# opens nothing: no media is ever fetched, played, decoded, launched or read.
_PPTX_PICTURE_MEDIA: Final[dict[str, object]] = {
    "mediaRelationshipId": None,
    "mediaRelationshipType": None,
    "mediaTarget": None,
    "mediaExternal": None,
    "mediaScheme": None,
    "mediaPart": None,
    "mediaContentType": None,
    "mediaBytes": None,
}
# The one element an embedded or linked object is declared by, in the two
# PresentationML main namespaces OOXML defines -- Transitional and Strict. An
# ``oleObj`` written in any other vocabulary shares a local name and nothing
# else, and declares no object.
_PPTX_OLE_OBJECT: Final = frozenset(
    f"{{{namespace}}}oleObj" for namespace in _PRESENTATIONML
)
# The two direct children that say which kind of object it is, each mapped to
# the mode its record reports. An object is embedded or it is linked: the
# element is the only thing that says which, and the relationship then has to
# agree with it.
_PPTX_OLE_MODES: Final[dict[str, str]] = {
    f"{{{namespace}}}{element}": element
    for element in ("embed", "link")
    for namespace in _PRESENTATIONML
}
# The preview a declared object may carry. It is a picture *of* the object, not
# the object and not a picture of the slide's own: it is recorded as declared
# and never opened, decoded, resolved or inventoried as a picture.
_PPTX_OLE_PREVIEW: Final = frozenset(
    f"{{{namespace}}}pic" for namespace in _PRESENTATIONML
)
# The relationship types that qualify what an object names: a single OLE object,
# or a whole package embedded as one. The pair is the same wherever an object is
# declared, so a slide's ``p:oleObj`` and a story's ``o:OLEObject`` qualify
# against one set. As with every other type here only the URIs OOXML defines
# qualify -- Transitional and Strict for each -- so nothing reaches an object by
# ending the same way, and an image, a media or a hyperlink relationship reaches
# no object at all.
_OLE_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/oleObject"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/oleObject",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/package",
    }
)
# The object's own unqualified claims, each paired with the field its record
# keeps it in. All three are text the authoring application wrote and none is
# interpreted: a ``progId`` is the application's own name for the server that
# would open the object, and nothing here looks one up, resolves it or launches
# anything, while ``spid`` is the identifier the legacy drawing layer knew the
# shape by and is not resolved against anything either.
_PPTX_OLE_CLAIMS: Final = (
    ("name", "oleName"),
    ("progId", "oleProgId"),
    ("spid", "oleShapeId"),
)
# The two extents the object declares for the image standing in for it, each
# ``ST_PositiveCoordinate32``: unsigned, and bounded by the 32-bit signed
# maximum the type is derived from.
_PPTX_OLE_EXTENTS: Final = (("imgW", "oleImageWidth"), ("imgH", "oleImageHeight"))
_MAX_PPTX_OLE_COORDINATE: Final = 0x7FFF_FFFF
# What an object's record carries, defaulted so an embedded part and an external
# link are shaped alike. ``oleScheme`` is the scheme a linked target's own text
# names and ``oleUpdateAutomatic`` is a link's own flag; the three part fields
# describe a member the package holds, and the byte length is the figure the
# package measured while validating it. Reporting any of it opens nothing: no
# object is ever parsed, decoded, executed, launched or fetched, and an object
# never contributes text.
_PPTX_OLE_RECORD: Final[dict[str, object]] = {
    "oleMode": None,
    "oleRelationshipId": None,
    "oleRelationshipType": None,
    "oleTarget": None,
    "oleExternal": None,
    "oleScheme": None,
    "oleUpdateAutomatic": None,
    "olePart": None,
    "oleContentType": None,
    "oleBytes": None,
    "oleName": None,
    "oleProgId": None,
    "oleShapeId": None,
    "oleShowAsIcon": None,
    "oleImageWidth": None,
    "oleImageHeight": None,
    "olePreviewDeclared": None,
}
# The relationship and content types that together qualify the two parts a
# slide's inherited text lives in: the layout the slide itself names, and the
# master that layout itself names. As with the notes and comment types, only
# the two URIs OOXML defines for each qualify -- Transitional and Strict -- so
# nothing reaches either part by ending the same way, and neither type reaches
# the other's part.
_LAYOUT_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/slideLayout"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/slideLayout",
    }
)
_MASTER_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/slideMaster"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/slideMaster",
    }
)
# Compared against ``declared_type``, which case-folds, so both are written
# folded: the authored spellings are ``...presentationml.slideLayout+xml`` and
# ``...presentationml.slideMaster+xml``. The content type is the same under
# Transitional and Strict.
_LAYOUT_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.slidelayout+xml"
)
_MASTER_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.slidemaster+xml"
)
# A layout or master is read for its own text bodies only. A graphic frame
# there relates its table, chart or diagram through that part's relationships
# rather than the slide's, and a frame on a layout describes what a slide would
# place rather than material the slide shows, so an inherited frame is left
# alone -- as every picture and group already is.
_PPTX_INHERITED_SHAPES: Final = frozenset({"sp", "cxnSp"})
# ST_PlaceholderType in full. The type names which slot a placeholder is, so an
# unlisted one names no slot at all: it would match nothing on a layout and
# nothing on a master, and silently inheriting nothing is indistinguishable
# from a slot a part legitimately leaves empty. It is refused instead.
_PPTX_PLACEHOLDER_TYPES: Final = frozenset(
    {
        "title",
        "body",
        "ctrTitle",
        "subTitle",
        "dt",
        "ftr",
        "sldNum",
        "sldImg",
        "hdr",
        "obj",
        "chart",
        "tbl",
        "clipArt",
        "dgm",
        "media",
        "pic",
    }
)
# ST_Index's own default, which is the index every placeholder written without
# an ``idx`` sits at -- a title among them.
_PPTX_DEFAULT_PLACEHOLDER_INDEX: Final = 0
# The two roots the inherited parts declare themselves at, in the PresentationML
# namespaces every other PresentationML part here is held to.
_LAYOUT_ROOT: Final = "sldLayout"
_MASTER_ROOT: Final = "sldMaster"
# ``ST_TransitionSpeed`` is a closed enumeration, so a transition claims one of
# exactly these speeds or the slide claims none. A speed outside the set is
# refused rather than carried through, for the reason a comment status outside
# its own set is: anything reading it reads it as one of the three.
_PPTX_TRANSITION_SPEEDS: Final = frozenset({"slow", "med", "fast"})
# ``CT_SlideTransition``'s own attributes, which are the whole of what a
# transition declares about itself: the speed it claims, whether a click
# advances the slide, and the time after which it advances on its own. The set
# is closed, so an unqualified attribute outside it is markup no slide
# transition defines and the slide is refused rather than read past it. A
# namespace-qualified attribute is another vocabulary's extension and is
# recorded as one -- see ``transitionExtensionDeclared`` below.
_PPTX_TRANSITION_ATTRIBUTES: Final = frozenset({"spd", "advClick", "advTm"})
# The two children of a transition that are not the effect it draws.
# ``extLst`` is an extension point, recorded as one. ``sndAc`` is the sound a
# transition plays: audio is another slice's subject entirely, so it is neither
# read nor counted here, and nothing in this module reaches a media part.
_PPTX_TRANSITION_NON_EFFECT: Final = frozenset({"extLst", "sndAc"})
# An effect's own attributes are not one closed set -- each effect kind defines
# its own direction, orientation or pattern -- so they are inventoried by name
# and value rather than interpreted, and only their number and size are bounded.
_MAX_PPTX_TRANSITION_ATTRIBUTES: Final = 32
# A timing tree is an animation program. Nothing here runs, schedules or reads
# one: its nodes are counted by element name and nothing else about them is
# looked at, so a node's attributes -- the command a ``p:cmd`` names among them
# -- are never read. These bound the counting itself, which is the only work the
# tree asks for.
_MAX_PPTX_TIMING_NODES: Final = 20_000
_MAX_PPTX_TIMING_DEPTH: Final = 64
_MAX_PPTX_TIMING_KINDS: Final = 128
# What one slide records about the way it plays: the transition it declares
# between slides, and the animation tree it declares once it is up. Defaulted so
# a slide declaring one of them carries the same keys as a slide declaring both.
# ``None`` on a transition field says the slide declared no such value, which is
# not the same as a value the schema would default it to -- nothing here applies
# a schema default, because a default is what a player would do with the
# declaration and no player runs here.
_PPTX_SLIDE_PLAYBACK: Final[dict[str, object]] = {
    "kind": "slide_playback",
    "transitionDeclared": False,
    "transitionSpeed": None,
    "transitionAdvanceOnClick": None,
    "transitionAdvanceAfter": None,
    "transitionEffect": None,
    "transitionEffectAttributes": None,
    "transitionExtensionDeclared": False,
    "timingDeclared": False,
    "timingNodes": None,
    "timingNodeKinds": None,
    "timingDepth": None,
    "timingExtensionDeclared": False,
}
_MAX_PPTX_COMMENTS: Final = 10_000
_MAX_PPTX_COMMENT_AUTHORS: Final = 10_000
_MAX_PPTX_COMMENT_CHARS: Final = 100_000
# A modern comment body is rich text, so its runs are work the part asks for
# whether or not any of them holds text. Counted across one comment part, over
# every comment and reply in it, for the reason the slide-wide run bound is.
_MAX_PPTX_COMMENT_RUNS: Final = 20_000
# ST_Guid, kept exactly as written: the braces and the grouping are the form,
# and the hex digits are the only part of it a writer varies in case.
_GUID: Final = re.compile(r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}")
# ST_Index is an ``xsd:unsignedInt``; the value is kept as written, never
# renumbered, so only its range is checked.
_INDEX: Final = re.compile(r"[0-9]{1,10}")
_MAX_INDEX: Final = 0xFFFFFFFF
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
        # Case-folded extension -> the content type a ``Default`` declares for
        # every part carrying it that no ``Override`` names.
        self.default_types: dict[str, str] = {}
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
            if _local(element.tag) == "Default":
                # OPC declares each extension at most once too, so a repeated
                # declaration is refused for the reason a repeated ``Override``
                # is: nothing downstream reconciles two claims or picks one.
                extension = element.attrib.get("Extension", "").casefold().lstrip(".")
                if not extension:
                    continue
                if extension in self.default_types:
                    raise OOXMLInvalid(
                        "OOXML content-types part declares an extension twice"
                    )
                self.default_types[extension] = content_type
                continue
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

    def resolved_type(self, name: str) -> str:
        """Return the content type OPC resolves for one part, by both routes.

        An ``Override`` names a part outright and wins; a ``Default`` says what
        every part carrying one extension is, and stands only where no
        ``Override`` names the part. This is the resolution a part whose type is
        carried by its extension needs -- an image part normally is -- and it is
        deliberately not what ``declared_type`` does: a part that qualifies as a
        notes slide, a comment list or a chart has to be named outright, so a
        default for ``.xml`` can never qualify one.
        """
        override = self.content_types.get(name.casefold())
        if override is not None:
            return override
        extension = posixpath.splitext(name)[1].lstrip(".").casefold()
        return self.default_types.get(extension, "") if extension else ""

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
                walker.finish()
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
        # Each author table is the presentation's, not a slide's, so both are
        # resolved once and every slide's comments cite the same two. The
        # classic and modern forms carry their own tables, and a presentation
        # may hold either, both or neither.
        authors = _pptx_comment_authors(package)
        modern_authors = _pptx_modern_comment_authors(package)
        segments: list[ExtractedSegment] = []
        for ordinal, (slide_id, part, hidden) in enumerate(slides, start=1):
            walker = _PptxWalker(
                capture_id, part, slide_id, ordinal, hidden, segments, package
            )
            slide_root = package.xml(part)
            # The slide's own text first, so that what it shows itself is what
            # suppresses an exact repeat of it from behind. The walker carries
            # what it has already shown from one pass to the other.
            walker.walk(_pptx_tree(slide_root), (), (), (), 0)
            walker.inherited(slide_root)
            playback = _pptx_slide_playback(slide_root)
            if playback is not None:
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        _locator(
                            capture_id,
                            part=part,
                            slideId=slide_id,
                            slide=ordinal,
                            slideHidden=hidden,
                        ),
                        structured_value=playback,
                        extraction_method="inventory",
                        visibility="hidden" if hidden else "visible",
                    )
                )
                _bounded(segments)
            notes = _related_part(
                package, part, _NOTES_RELATIONSHIPS, _NOTES_CONTENT_TYPE, "notes"
            )
            if notes is not None:
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
            # Classic first, then modern: each form keeps its own source order,
            # and a slide holding both reports both rather than one standing in
            # for the other.
            for text, provenance in _pptx_comments(
                package, part, authors
            ) + _pptx_modern_comments(package, part, slide_root, modern_authors):
                segments.append(
                    ExtractedSegment(
                        "comment",
                        _locator(
                            capture_id,
                            **provenance,
                            slidePart=part,
                            slideId=slide_id,
                            slide=ordinal,
                            slideHidden=hidden,
                        ),
                        # A comment written with no text is still a comment the
                        # file claims, so it is reported rather than dropped.
                        # ``document_intake`` takes exactly one content
                        # representation and refuses empty text, so the empty
                        # claim is carried as the value it is; nothing is
                        # invented to stand in for text the file never wrote.
                        text=text or None,
                        structured_value=None if text else {"text": ""},
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


def _is_defined_relationship(kind: str, defined: frozenset[str]) -> bool:
    """Say whether one relationship type is one of the URIs OOXML defines for it.

    Scheme and host are case-insensitive under RFC 3986 and the path is not, so
    only those two are folded, and only in ASCII: every defined URI is ASCII, so
    a type carrying any other character is not one of them and no non-ASCII
    character can case-fold its way into a match.
    """
    if not kind.isascii():
        return False
    scheme, separator, rest = kind.partition("://")
    if not separator:
        return False
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}" in defined


def _related_part(
    package: _Package,
    source: str,
    defined: frozenset[str],
    content_type: str,
    what: str,
    owner: str = "slide",
) -> tuple[str, str] | None:
    """Return the (relationship id, part) ``source`` relates to as ``what``.

    The part is only ever reached through ``source``'s own relationships. No
    path is derived by substituting into ``source``'s file name, so a member
    that merely looks like the part is not it and is never read; and nothing
    here asks a convenience API for one, which would materialise a part the
    package does not hold.

    The relationship has to be internal, has to carry one of the URIs OOXML
    defines for the type, and has to resolve to a part the package declares with
    that type's own content type. Containment, path safety and existence were
    all settled when the relationship was resolved. Two qualifying
    relationships leave which part is meant ambiguous, so ``source`` is refused
    rather than read from whichever sorts first.
    """
    rels = posixpath.join(
        posixpath.dirname(source), "_rels", posixpath.basename(source) + ".rels"
    )
    named = sorted(
        relation_id
        for relation_id, kind in package.relationship_types.get(rels, {}).items()
        if _is_defined_relationship(kind, defined)
    )
    if not named:
        return None
    if len(named) > 1:
        raise OOXMLInvalid(f"PPTX {owner} names more than one {what} relationship")
    target, external = package.relationships[rels][named[0]]
    if external:
        raise OOXMLInvalid(f"PPTX {what} relationship is external")
    if package.declared_type(target) != content_type:
        raise OOXMLInvalid(f"PPTX {what} relationship does not name a {what} part")
    return named[0], target


def _pptx_comment_root(
    package: _Package,
    part: str,
    expected: str,
    namespaces: frozenset[str] = _PRESENTATIONML,
) -> tuple[ElementTree.Element, str]:
    """Parse one comment part, returning its root and the namespace it uses.

    The declared content type says what the part is; the root element is where
    the part says so itself. A root in any other namespace, or under any other
    name, is not the vocabulary the declaration promised, so the part is refused
    rather than read for whatever elements happen to be in it. The classic and
    modern parts each accept only their own namespaces, so neither is read as
    the other however it is declared.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    if namespace.lstrip("{") not in namespaces:
        raise OOXMLInvalid("PPTX comment part uses an unsupported namespace")
    if local != expected:
        raise OOXMLInvalid("PPTX comment part is not rooted at its own list")
    return root, namespace.lstrip("{")


def _pptx_comment_local(element: ElementTree.Element, namespace: str) -> str:
    """Return one comment element's local name, refusing a foreign namespace.

    Comment parts hold a closed vocabulary, so an element from somewhere else is
    content no reader can place rather than something to skip past.
    """
    prefix, _, local = element.tag.rpartition("}")
    if prefix.lstrip("{") != namespace:
        raise OOXMLInvalid("PPTX comment part uses an unsupported namespace")
    return local


def _pptx_comment_authors(package: _Package) -> dict[str, dict[str, object]]:
    """Index the presentation's classic comment authors by the id each claims.

    Every field is recorded exactly as the package wrote it, and nothing here
    checks that any of it names a real person: an author record is a claim the
    file makes about itself, never evidence of who commented. Two records
    claiming one id leave every comment citing it ambiguous, so the table is
    refused rather than read under one of them.
    """
    related = _related_part(
        package,
        "ppt/presentation.xml",
        _AUTHORS_RELATIONSHIPS,
        _AUTHORS_CONTENT_TYPE,
        "comment authors",
        owner="presentation",
    )
    if related is None:
        return {}
    _relation_id, part = related
    root, namespace = _pptx_comment_root(package, part, "cmAuthorLst")
    authors: dict[str, dict[str, object]] = {}
    for record in root:
        if _pptx_comment_local(record, namespace) != "cmAuthor":
            raise OOXMLInvalid("PPTX comment part holds an unsupported element")
        if len(authors) >= _MAX_PPTX_COMMENT_AUTHORS:
            raise OOXMLInvalid("PPTX comment author table is too large")
        author_id = _pptx_comment_id(record, "id")
        if author_id in authors:
            raise OOXMLInvalid("PPTX comment author id is repeated")
        authors[author_id] = {
            "authorPart": part,
            "authorName": _pptx_comment_claim(record, "name"),
            "authorInitials": _pptx_comment_claim(record, "initials"),
            "authorLastIndex": _pptx_comment_index(record, "lastIdx"),
            "authorColorIndex": _pptx_comment_index(record, "clrIdx"),
            # Stated so no consumer reads the fields above as identity.
            "authorAuthenticated": False,
        }
    return authors


def _pptx_comments(
    package: _Package, slide: str, authors: dict[str, dict[str, object]]
) -> list[tuple[str, dict[str, object]]]:
    """Return one slide's classic comments as (text, provenance), in source order.

    Order is the order the comment list wrote them in; nothing is sorted by id,
    author or timestamp. A classic comment's identity is its author's id paired
    with that author's own running index, which is why two authors may each hold
    an ``idx`` of 1 and why one author may not. A comment whose author the table
    never defines cites nobody at all, so the slide is refused rather than
    reported against an author that is not there.
    """
    related = _related_part(
        package, slide, _COMMENTS_RELATIONSHIPS, _COMMENTS_CONTENT_TYPE, "comments"
    )
    if related is None:
        return []
    relation_id, part = related
    root, namespace = _pptx_comment_root(package, part, "cmLst")
    result: list[tuple[str, dict[str, object]]] = []
    seen: set[tuple[str, str]] = set()
    for entry in root:
        if _pptx_comment_local(entry, namespace) != "cm":
            raise OOXMLInvalid("PPTX comment part holds an unsupported element")
        if len(result) >= _MAX_PPTX_COMMENTS:
            raise OOXMLInvalid("PPTX slide has too many comments")
        author_id = _pptx_comment_id(entry, "authorId")
        index = _pptx_comment_id(entry, "idx")
        if (author_id, index) in seen:
            raise OOXMLInvalid("PPTX comment id is repeated")
        seen.add((author_id, index))
        author = authors.get(author_id)
        if author is None:
            raise OOXMLInvalid("PPTX comment names no author")
        text, position = _pptx_comment_body(entry, namespace)
        result.append(
            (
                text,
                {
                    "part": part,
                    "relationshipId": relation_id,
                    "commentForm": "classic",
                    "commentId": f"{author_id}:{index}",
                    "commentIndex": index,
                    "authorId": author_id,
                    **author,
                    "timestamp": _pptx_comment_claim(entry, "dt"),
                    **position,
                },
            )
        )
    return result


def _pptx_comment_body(
    entry: ElementTree.Element, namespace: str
) -> tuple[str, dict[str, object]]:
    """Read one comment's text and anchor, refusing a malformed entry.

    ``pos`` and ``text`` each appear once: a second of either leaves the comment
    with two anchors or two bodies and nothing here picks between them. The text
    is an ``xsd:string``, so markup inside it is not a structure to descend
    into; a ``text`` element carrying one is refused rather than read down to
    its first child.
    """
    text: str | None = None
    position: dict[str, object] = {"x": None, "y": None}
    anchored = False
    for node in entry:
        local = _pptx_comment_local(node, namespace)
        if local == "pos":
            if anchored:
                raise OOXMLInvalid("PPTX comment declares more than one position")
            anchored = True
            position = {name: _pptx_comment_coordinate(node, name) for name in "xy"}
        elif local == "text":
            if text is not None or len(node):
                raise OOXMLInvalid("PPTX comment text is malformed")
            text = node.text or ""
        elif local != "extLst":
            raise OOXMLInvalid("PPTX comment part holds an unsupported element")
    if text is None:
        raise OOXMLInvalid("PPTX comment text is malformed")
    if len(text) > _MAX_PPTX_COMMENT_CHARS:
        raise OOXMLInvalid("PPTX comment text exceeds the accepted bound")
    return text, position


def _pptx_comment_index(element: ElementTree.Element, name: str) -> str | None:
    """Read one ``ST_Index`` attribute verbatim, refusing a malformed value.

    The value is kept as the package wrote it, never renumbered, so only its
    range is checked.
    """
    raw = element.attrib.get(name)
    if raw is not None and (_INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX):
        raise OOXMLInvalid("PPTX comment index is malformed")
    return raw


def _pptx_comment_id(element: ElementTree.Element, name: str) -> str:
    """Read an ``ST_Index`` the comment schema requires, refusing an absent one."""
    index = _pptx_comment_index(element, name)
    if index is None:
        raise OOXMLInvalid("PPTX comment index is missing")
    return index


def _pptx_comment_coordinate(element: ElementTree.Element, name: str) -> int:
    """Read one anchor coordinate, refusing a value that is not a coordinate."""
    raw = element.attrib.get(name)
    if raw is None or _EMU_OFFSET.fullmatch(raw) is None:
        raise OOXMLInvalid("PPTX comment position is malformed")
    return int(raw)


def _pptx_comment_claim(element: ElementTree.Element, name: str) -> str | None:
    """Return one free-text attribute clipped, or None where it is not written.

    Nothing here parses or resolves the value -- a timestamp stays the string
    the package wrote, never a moment anything happened.
    """
    value = element.attrib.get(name)
    return None if value is None else _clip(value, _MAX_META_CHARS)


def _pptx_comment_guid(element: ElementTree.Element, name: str) -> str:
    """Read one ``ST_Guid`` the modern schema requires, refusing a bad value.

    The value is kept exactly as written, never recased, rebraced or
    renumbered, so an id stays the one the file cited.
    """
    raw = element.attrib.get(name)
    if raw is None:
        raise OOXMLInvalid("PPTX modern comment id is missing")
    if _GUID.fullmatch(raw) is None:
        raise OOXMLInvalid("PPTX modern comment id is malformed")
    return raw


def _pptx_modern_comment_status(element: ElementTree.Element) -> str | None:
    """Return the state a modern comment or reply claims, refusing an undefined one.

    The value is matched as written against the closed enumeration, so a state
    differing only in case is not that state: ``ST_CommentStatus`` is
    case-sensitive, and folding one in would be deciding that a writer meant a
    value it did not write.
    """
    status = _pptx_comment_claim(element, "status")
    if status is not None and status not in _MODERN_COMMENT_STATUS:
        raise OOXMLInvalid("PPTX modern comment status is not a defined state")
    return status


def _pptx_modern_comment_authors(package: _Package) -> dict[str, dict[str, object]]:
    """Index the presentation's modern comment authors by the id each claims.

    Every field is recorded exactly as the package wrote it, and nothing here
    checks that any of it names a real person: an author record is a claim the
    file makes about itself, never evidence of who commented, and a user or
    provider id is no more than another such claim. Ids are indexed case-folded
    because only their hex digits vary in case, so two records whose ids differ
    in nothing else claim one identity and leave every comment citing it
    ambiguous; the table is refused rather than read under one of them.
    """
    related = _related_part(
        package,
        "ppt/presentation.xml",
        _MODERN_AUTHORS_RELATIONSHIPS,
        _MODERN_AUTHORS_CONTENT_TYPE,
        "modern comment authors",
        owner="presentation",
    )
    if related is None:
        return {}
    _relation_id, part = related
    root, namespace = _pptx_comment_root(package, part, "authorLst", _MODERN_NAMESPACE)
    authors: dict[str, dict[str, object]] = {}
    for record in root:
        local = _pptx_comment_local(record, namespace)
        if local == "extLst":
            continue
        if local != "author":
            raise OOXMLInvalid("PPTX comment part holds an unsupported element")
        if len(authors) >= _MAX_PPTX_COMMENT_AUTHORS:
            raise OOXMLInvalid("PPTX comment author table is too large")
        author_id = _pptx_comment_guid(record, "id").casefold()
        if author_id in authors:
            raise OOXMLInvalid("PPTX comment author id is repeated")
        authors[author_id] = {
            **_MODERN_AUTHOR_UNKNOWN,
            "authorPart": part,
            "authorName": _pptx_comment_claim(record, "name"),
            "authorInitials": _pptx_comment_claim(record, "initials"),
            "authorUserId": _pptx_comment_claim(record, "userId"),
            "authorProviderId": _pptx_comment_claim(record, "providerId"),
        }
    return authors


def _namespaced_ids(element: ElementTree.Element, name: str) -> list[str]:
    """Return every value of one relationship-namespaced attribute on ``element``.

    An id written in any other namespace, or in none at all, names no
    relationship: the attribute is defined in the relationships namespace, and
    the two URIs OOXML defines for that namespace -- Transitional and Strict --
    are the only ones that carry it.
    """
    return [
        value
        for key, value in element.attrib.items()
        if _local(key) == name
        and key.startswith("{")
        and key.rpartition("}")[0].lstrip("{") in _RELATIONSHIP_NAMESPACES
    ]


def _pptx_modern_comment_extension(slide_root: ElementTree.Element) -> str | None:
    """Return the relationship id the slide names its modern comment list by.

    The advertisement is the slide's own: an ``ext`` directly inside the slide
    element's own ``extLst``, both written in the same PresentationML namespace
    the slide element is. That is the one place the extension is defined to
    sit, so it is the only place read. An extension list hung off a shape
    somewhere down the tree is that shape's, not the slide's; and an ``ext``
    from another namespace is another vocabulary's element that happens to
    share a local name, whatever URI it carries. Neither routes to a comment
    list.

    ``None`` says the slide advertises no modern comments at all, which is not
    a fault: a slide relating to a comment part it never declares has none to
    read, and nothing falls back to the relationship as the route in.

    Where the slide does advertise the extension, everything it promises has to
    be there. Two advertisements, or an advertisement holding no ``commentRel``
    or more than one, leave which list the slide means ambiguous; an id written
    in any namespace but the relationships one names no relationship in the
    first place, and neither does a ``commentRel`` from somewhere other than
    the one namespace the extension is defined in. Each is refused rather than
    resolved to whichever reading happens to work.
    """
    namespace = slide_root.tag.rpartition("}")[0].lstrip("{")
    if namespace not in _PRESENTATIONML:
        return None
    advertised = [
        element
        for container in slide_root
        if container.tag == f"{{{namespace}}}extLst"
        for element in container
        if element.tag == f"{{{namespace}}}ext"
        and element.attrib.get("uri", "").casefold() == _MODERN_COMMENTS_EXTENSION
    ]
    if not advertised:
        return None
    if len(advertised) > 1:
        raise OOXMLInvalid("PPTX slide advertises modern comments more than once")
    related = [
        child for child in advertised[0] if child.tag == _MODERN_COMMENT_REL
    ]
    if len(related) != 1:
        raise OOXMLInvalid(
            "PPTX modern comment extension does not name exactly one relationship"
        )
    named = _namespaced_ids(related[0], "id")
    if not named:
        raise OOXMLInvalid("PPTX modern comment extension names no relationship id")
    if len(named) > 1:
        raise OOXMLInvalid(
            "PPTX modern comment extension names more than one relationship id"
        )
    return named[0]


def _pptx_modern_comments(
    package: _Package,
    slide: str,
    slide_root: ElementTree.Element,
    authors: dict[str, dict[str, object]],
) -> list[tuple[str, dict[str, object]]]:
    """Return one slide's modern comments as (text, provenance), in source order.

    The slide's own extension is the route in: it names the relationship the
    list is reached through, so a slide advertising nothing has no modern
    comments however it relates, and a slide advertising the extension has to
    carry the one relationship it named. A relationship the extension does not
    name is not the list the slide meant, and resolving the part from it anyway
    would read a part the slide never claimed as its comments.

    Order is the order the comment list wrote them in -- each comment, then its
    own replies in turn -- and nothing is sorted by id, author or timestamp. A
    reply is reported as the comment it is, citing the one it answers and the
    position it holds in that thread.

    A modern comment's identity is its own ``ST_Guid``, so unlike a classic
    comment it cites an author without indexing into a running table. An author
    the table never defines is kept as the id the file wrote, marked
    unresolved: a comment citing an id nobody defined is still the file's own
    claim, and no identity is invented to stand in for one.
    """
    extension_id = _pptx_modern_comment_extension(slide_root)
    if extension_id is None:
        return []
    related = _related_part(
        package,
        slide,
        _MODERN_COMMENTS_RELATIONSHIPS,
        _MODERN_COMMENTS_CONTENT_TYPE,
        "modern comments",
    )
    if related is None:
        raise OOXMLInvalid("PPTX modern comment extension names a missing relationship")
    relation_id, part = related
    if relation_id != extension_id:
        raise OOXMLInvalid("PPTX modern comment extension names another relationship")
    root, namespace = _pptx_comment_root(package, part, "cmLst", _MODERN_NAMESPACE)
    result: list[tuple[str, dict[str, object]]] = []
    seen: set[str] = set()
    runs = 0
    for entry in root:
        local = _pptx_comment_local(entry, namespace)
        if local == "extLst":
            continue
        if local != "cm":
            raise OOXMLInvalid("PPTX comment part holds an unsupported element")
        if len(result) >= _MAX_PPTX_COMMENTS:
            raise OOXMLInvalid("PPTX slide has too many comments")
        text, provenance, replies, runs = _pptx_modern_entry(
            entry, namespace, part, relation_id, authors, seen, runs
        )
        result.append((text, provenance))
        for index, reply in enumerate(replies):
            if _pptx_comment_local(reply, namespace) != "reply":
                raise OOXMLInvalid("PPTX comment part holds an unsupported element")
            if len(result) >= _MAX_PPTX_COMMENTS:
                raise OOXMLInvalid("PPTX slide has too many comments")
            reply_text, reply_where, _nested, runs = _pptx_modern_entry(
                reply,
                namespace,
                part,
                relation_id,
                authors,
                seen,
                runs,
                as_reply=True,
            )
            result.append(
                (
                    reply_text,
                    {
                        **reply_where,
                        "parentCommentId": provenance["commentId"],
                        "replyIndex": index,
                    },
                )
            )
    return result


def _pptx_modern_entry(
    entry: ElementTree.Element,
    namespace: str,
    part: str,
    relation_id: str,
    authors: dict[str, dict[str, object]],
    seen: set[str],
    runs: int,
    *,
    as_reply: bool = False,
) -> tuple[str, dict[str, object], list[ElementTree.Element], int]:
    """Read one modern comment or reply: its text, its claims and its replies.

    A comment and a reply are reported in the same shape, so both are read here
    and carry the same keys -- a reply simply holds nothing for the keys only a
    comment may claim. They are not read under the same grammar: ``as_reply``
    reads the narrower ``CT_CommentReply``, which admits only a body and the
    extension point, and only the four attributes a reply may carry. A comment's
    own children and claims written on a reply are refused there rather than
    read, so no reply-only field is ever reported from syntax the schema does
    not allow.

    Within a comment, ``pos``, the anchor, the body and the reply list each
    appear once -- a second of any of them leaves the comment with two of
    something nothing here picks between. Two entries claiming one id leave
    every reference to it ambiguous, so the part is refused rather than read
    under one of them.
    """
    if as_reply and not _MODERN_REPLY_ATTRIBUTES.issuperset(entry.attrib):
        raise OOXMLInvalid("PPTX modern comment reply holds an unsupported attribute")
    comment_id = _pptx_comment_guid(entry, "id")
    if comment_id.casefold() in seen:
        raise OOXMLInvalid("PPTX comment id is repeated")
    seen.add(comment_id.casefold())
    author_id = _pptx_comment_guid(entry, "authorId")
    author = authors.get(author_id.casefold())
    text: str | None = None
    position: dict[str, object] = {"x": None, "y": None}
    anchor: str | None = None
    replies: list[ElementTree.Element] = []
    anchored = False
    threaded = False
    for node in entry:
        local = _pptx_comment_local(node, namespace)
        if as_reply and local not in _MODERN_REPLY_CHILDREN:
            # A nested thread is called out for what it is -- a thread is one
            # level deep -- and everything else is simply an element the reply
            # grammar does not place here.
            raise OOXMLInvalid(
                "PPTX modern comment reply holds replies of its own"
                if local == "replyLst"
                else "PPTX comment part holds an unsupported element"
            )
        if local == "pos":
            if anchored:
                raise OOXMLInvalid("PPTX comment declares more than one position")
            anchored = True
            position = {name: _pptx_comment_coordinate(node, name) for name in "xy"}
        elif local in _MODERN_ANCHORS:
            if anchor is not None:
                raise OOXMLInvalid("PPTX modern comment declares more than one anchor")
            anchor = local
        elif local == "txBody":
            if text is not None:
                raise OOXMLInvalid("PPTX modern comment text is malformed")
            text, runs = _pptx_modern_text(node, runs)
        elif local == "replyLst":
            if threaded:
                raise OOXMLInvalid("PPTX modern comment declares more than one thread")
            threaded = True
            # The extension point is dropped here rather than skipped later, so
            # a reply's position in the thread counts replies and nothing else.
            replies = [
                item
                for item in node
                if _pptx_comment_local(item, namespace) != "extLst"
            ]
        elif local != "extLst":
            raise OOXMLInvalid("PPTX comment part holds an unsupported element")
    if text is None:
        raise OOXMLInvalid("PPTX modern comment text is malformed")
    return (
        text,
        {
            "part": part,
            "relationshipId": relation_id,
            # The id the slide's extension named, recorded beside the one the
            # part was reached through. The two are the same id: the extension
            # is the route in, and a slide naming any other relationship is
            # refused rather than read through either.
            "extensionRelationshipId": relation_id,
            "commentForm": "modern",
            "commentId": comment_id,
            "parentCommentId": None,
            "replyIndex": None,
            "authorId": author_id,
            "authorResolved": author is not None,
            **(_MODERN_AUTHOR_UNKNOWN if author is None else author),
            "created": _pptx_comment_claim(entry, "created"),
            "status": _pptx_modern_comment_status(entry),
            # The task claims resolve to None on a reply, which may not write
            # them at all: a reply carries the keys so both forms read alike,
            # and carries nothing in them.
            **{
                name: _pptx_comment_claim(entry, name)
                for name in _MODERN_COMMENT_TASK_CLAIMS
            },
            "anchorKind": anchor,
            **position,
        },
        replies,
        runs,
    )


def _pptx_modern_text(body: ElementTree.Element, runs: int) -> tuple[str, int]:
    """Join one modern comment body's own text, bounding the runs it asks for.

    The body is DrawingML rich text rather than the ``xsd:string`` a classic
    comment holds, so its runs are read by local name as the slide's own are,
    joined inside each paragraph and separated between them. Nothing in it is
    resolved or evaluated: a mention is the text the file wrote, never a person
    looked up, and a field is the value it already stored.
    """
    paragraphs: list[str] = []
    for paragraph in body:
        if _local(paragraph.tag) != "p":
            continue
        pieces: list[str] = []
        for node in paragraph.iter():
            if _local(node.tag) != "t":
                continue
            runs += 1
            if runs > _MAX_PPTX_COMMENT_RUNS:
                raise OOXMLInvalid("PPTX modern comments have too many runs")
            pieces.append(node.text or "")
        paragraphs.append("".join(pieces))
    text = "\n".join(paragraphs)
    if len(text) > _MAX_PPTX_COMMENT_CHARS:
        raise OOXMLInvalid("PPTX comment text exceeds the accepted bound")
    return text, runs


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


def _pptx_inherited_root(
    package: _Package, part: str, expected: str
) -> ElementTree.Element:
    """Parse one layout or master, refusing a root that is not its own.

    The declared content type says what the part is; the root element is where
    the part says so itself, in one of the PresentationML namespaces. Anything
    else is not the vocabulary the declaration promised, so the part is refused
    rather than read for whatever shapes happen to be in it -- and a layout is
    never read as a master, or the other way round, however it is declared.
    """
    root = package.xml(part)
    namespace = root.tag.rpartition("}")[0].lstrip("{")
    if namespace not in _PRESENTATIONML:
        raise OOXMLInvalid("PPTX inherited part uses an unsupported namespace")
    if _local(root.tag) != expected:
        raise OOXMLInvalid(f"PPTX inherited part is not rooted at its own {expected}")
    return root


def _pptx_inherited_tree(
    root: ElementTree.Element, what: str
) -> ElementTree.Element:
    """Return a layout or master's own shape tree, reached one hop at a time.

    The tree is the ``spTree`` directly below the part's own ``cSld``, which is
    the one place the schema puts it. Nothing is found by searching the part for
    a descendant that happens to be named one, so a shape tree written anywhere
    else -- inside an extension, or in another vocabulary's markup carried
    along -- is not read as this part's own declaration of what it shows.
    """
    common = next((node for node in root if _local(node.tag) == "cSld"), None)
    tree = (
        None
        if common is None
        else next((node for node in common if _local(node.tag) == "spTree"), None)
    )
    if tree is None:
        raise OOXMLInvalid(f"PPTX slide {what} has no shape tree")
    return tree


def _pptx_non_visual(shape: ElementTree.Element) -> ElementTree.Element | None:
    """Return the one ``cNvPr`` inside one shape's non-visual properties holder.

    Each shape kind names that holder after itself -- ``nvSpPr``, ``nvPicPr``
    and so on -- so the holder is matched on the prefix they share, and only one
    directly below the shape is read. Everything a shape claims about itself
    without drawing it lives there: its identity, and the name, title and
    description its author wrote.

    Two of them, in one holder or across two, leaves which set of claims is the
    shape's own ambiguous, so the shape is refused rather than read from
    whichever was written first. That is one refusal for every shape kind and
    every claim: a shape's identifier and its author's words come from the same
    element, so neither can be taken from a different one than the other.
    """
    properties = [
        node
        for holder in shape
        if _local(holder.tag).startswith("nv")
        for node in holder
        if _local(node.tag) == "cNvPr"
    ]
    if len(properties) > 1:
        raise OOXMLInvalid("PPTX shape declares its non-visual properties twice")
    return properties[0] if properties else None


def _pptx_shape_id(shape: ElementTree.Element, seen: set[str], what: str) -> str:
    """Return the shape's identifier, refusing a missing or repeated one."""
    properties = _pptx_non_visual(shape)
    shape_id = None if properties is None else properties.attrib.get("id")
    if shape_id is None or _SHAPE_ID.fullmatch(shape_id) is None:
        raise OOXMLInvalid("PPTX shape has no readable identifier")
    if shape_id in seen:
        raise OOXMLInvalid(f"PPTX shape identifier is repeated on one {what}")
    seen.add(shape_id)
    return shape_id


def _pptx_shows_master(element: ElementTree.Element) -> bool:
    """Whether a slide or layout shows the inherited shapes behind it.

    Unlike the SpreadsheetML flags, an absent attribute here means they are
    shown: ``showMasterSp`` is only ever written to turn them off.
    """
    value = element.attrib.get("showMasterSp")
    if value is None or value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise OOXMLInvalid("PPTX inherited shapes flag is malformed")


def _pptx_slide_playback(slide_root: ElementTree.Element) -> dict[str, object] | None:
    """Return what one slide declares about the way it plays, or ``None``.

    The transition and the timing tree are the slide element's own children, in
    the same PresentationML namespace -- Transitional or Strict -- the slide
    element itself is written in. That is the one place the schema puts either,
    so it is the only place read: a ``transition`` or ``timing`` from another
    vocabulary that happens to share the local name, or one nested anywhere
    below the slide (inside the shape tree, an extension, or carried-along
    markup), declares nothing about how this slide plays.

    ``None`` says the slide declares neither, which is no fault: a slide that
    never says how it plays has nothing to inventory. Where it does declare
    one, it declares it once -- two of either leave which one the slide means
    ambiguous, so the slide is refused rather than read from whichever was
    written first.
    """
    namespace = slide_root.tag.rpartition("}")[0].lstrip("{")
    if namespace not in _PRESENTATIONML:
        return None
    transitions = [
        child for child in slide_root if child.tag == f"{{{namespace}}}transition"
    ]
    timings = [child for child in slide_root if child.tag == f"{{{namespace}}}timing"]
    if len(transitions) > 1:
        raise OOXMLInvalid("PPTX slide declares its transition more than once")
    if len(timings) > 1:
        raise OOXMLInvalid("PPTX slide declares its timing more than once")
    if not transitions and not timings:
        return None
    record = dict(_PPTX_SLIDE_PLAYBACK)
    if transitions:
        record.update(_pptx_transition(transitions[0], namespace))
    if timings:
        record.update(_pptx_timing(timings[0], namespace))
    return record


def _pptx_transition(
    transition: ElementTree.Element, namespace: str
) -> dict[str, object]:
    """Inventory one slide transition, without drawing or advancing anything.

    The transition's own attributes are a closed set, so an unqualified one
    outside it is markup no slide transition defines; a qualified one is
    another vocabulary's extension and is recorded as one rather than read. The
    effect is the single child that is neither the extension point nor the
    sound a transition plays: the sound is never read, counted or resolved, so
    nothing here reaches a media part. The effect's own attributes are kept by
    the name the markup wrote them under -- qualified ones qualified, so no two
    vocabularies collide in one record -- and are never interpreted: what a
    direction or a pattern would do is what a player does with it.
    """
    extension = False
    for key in transition.attrib:
        if key.startswith("{"):
            extension = True
        elif key not in _PPTX_TRANSITION_ATTRIBUTES:
            raise OOXMLInvalid("PPTX slide transition declares an unknown attribute")
    speed = transition.attrib.get("spd")
    if speed is not None and speed not in _PPTX_TRANSITION_SPEEDS:
        raise OOXMLInvalid("PPTX slide transition speed is malformed")
    click = transition.attrib.get("advClick")
    if click is not None and click not in _TRUE_FLAGS | _FALSE_FLAGS:
        raise OOXMLInvalid("PPTX slide transition click flag is malformed")
    after = transition.attrib.get("advTm")
    if after is not None and (
        _INDEX.fullmatch(after) is None or int(after) > _MAX_INDEX
    ):
        raise OOXMLInvalid("PPTX slide transition advance time is malformed")
    effects = [
        child
        for child in transition
        if child.tag.startswith(f"{{{namespace}}}")
        and _local(child.tag) not in _PPTX_TRANSITION_NON_EFFECT
    ]
    if len(effects) > 1:
        raise OOXMLInvalid("PPTX slide transition declares more than one effect")
    if len(effects) == 1 and len(effects[0].attrib) > _MAX_PPTX_TRANSITION_ATTRIBUTES:
        raise OOXMLInvalid("PPTX slide transition effect declares too many attributes")
    return {
        "transitionDeclared": True,
        "transitionSpeed": speed,
        "transitionAdvanceOnClick": None if click is None else click in _TRUE_FLAGS,
        "transitionAdvanceAfter": None if after is None else int(after),
        "transitionEffect": _local(effects[0].tag) if effects else None,
        "transitionEffectAttributes": (
            {
                key: _clip(value, _MAX_META_CHARS)
                for key, value in sorted(effects[0].attrib.items())
            }
            if effects
            else None
        ),
        "transitionExtensionDeclared": extension
        or any(child.tag == f"{{{namespace}}}extLst" for child in transition),
    }


def _pptx_timing(timing: ElementTree.Element, namespace: str) -> dict[str, object]:
    """Count one slide's timing tree by element name, and nothing else about it.

    A timing tree is an animation program: a node names a trigger, a target and
    sometimes a command. None of that is read here. Every element below the
    timing element is counted under its local name and then left alone, so no
    attribute is read, no target is resolved and no command is executed,
    simulated or even inspected. The counts are returned in name order, so the
    same tree always produces the same record.

    The bounds are on the counting, which is the only work the tree asks for:
    a tree with more nodes, deeper nesting, or more distinct node kinds than
    one an author wrote is refused rather than walked.
    """
    kinds: dict[str, int] = {}
    nodes = 0
    deepest = 0
    pending = [(child, 1) for child in timing]
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_PPTX_TIMING_NODES:
            raise OOXMLInvalid("PPTX slide timing declares too many nodes")
        if depth > _MAX_PPTX_TIMING_DEPTH:
            raise OOXMLInvalid("PPTX slide timing is nested too deeply")
        deepest = max(deepest, depth)
        kind = _local(node.tag)
        if kind not in kinds and len(kinds) == _MAX_PPTX_TIMING_KINDS:
            raise OOXMLInvalid("PPTX slide timing declares too many node kinds")
        kinds[kind] = kinds.get(kind, 0) + 1
        pending.extend((child, depth + 1) for child in node)
    return {
        "timingDeclared": True,
        "timingNodes": nodes,
        "timingNodeKinds": dict(sorted(kinds.items())),
        "timingDepth": deepest,
        "timingExtensionDeclared": any(
            child.tag == f"{{{namespace}}}extLst" for child in timing
        ),
    }


@dataclass(frozen=True, slots=True)
class _PptxPrompt:
    """One placeholder shape on a layout or master, kept for applicability.

    ``slot`` is what its reference names, matched on by
    :func:`_pptx_applicable`; ``where`` is the provenance a segment sourced at
    this shape carries, exactly as the inherited pass built it; ``shape`` is the
    element itself, because what text the slot saved is read from it only if a
    slide's own placeholder applies to it.

    ``holds_text`` is whether it saved any text at all, which decides whether
    the slot falls through to the part behind this one. It is read once, where
    the shape was met, rather than again for each slot that resolves to it: the
    runs a prompt holds are never counted against the slide's own bounds unless
    they are emitted, so re-reading them per slot would let one part's size be
    multiplied by the number of placeholders a slide writes.
    """

    slot: tuple[str, int]
    where: dict[str, object]
    shape: ElementTree.Element
    holds_text: bool


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


def _pptx_frame_chart(frame: ElementTree.Element) -> str | None:
    """Return the relationship id the frame's chart reference names, or None.

    A frame holds one graphic object, so two chart references below one frame
    leave which chart the frame shows ambiguous and nothing here chooses
    between them. The id is read from the frame itself; no chart path is
    derived from the slide's own name.
    """
    charts = [node for node in frame.iter() if _local(node.tag) == "chart"]
    if not charts:
        return None
    if len(charts) > 1:
        raise OOXMLInvalid("PPTX graphic frame holds more than one chart reference")
    relation_id = _relationship_id(charts[0].attrib)
    if not relation_id:
        raise OOXMLInvalid("PPTX chart reference names no relationship")
    return relation_id


def _referenced_part(
    package: _Package,
    source: str,
    relation_id: str,
    defined: frozenset[str],
    content_type: str,
    what: str,
    owner: str = "PPTX",
) -> str:
    """Return the ``what`` part one reference's own relationship id names.

    The id is resolved only through the relationships of ``source``, the part
    that holds the reference, so containment, path safety and existence were all
    settled when that relationship was resolved and no member is reached by
    merely looking like the part. The relationship has to be internal, has to
    carry one of the URIs OOXML defines for the type, and has to resolve to a
    part the package declares with that type's own content type.

    ``owner`` is the format whose reference is being resolved, so a refusal
    names the file it came from rather than whichever format reached this code
    first.
    """
    rels = posixpath.join(
        posixpath.dirname(source), "_rels", posixpath.basename(source) + ".rels"
    )
    entry = package.relationships.get(rels, {}).get(relation_id)
    if entry is None:
        raise OOXMLInvalid(f"{owner} {what} reference names a missing relationship")
    kind = package.relationship_types.get(rels, {}).get(relation_id, "")
    if not _is_defined_relationship(kind, defined):
        raise OOXMLInvalid(
            f"{owner} {what} reference names a relationship of another type"
        )
    target, external = entry
    if external:
        raise OOXMLInvalid(f"{owner} {what} relationship is external")
    if package.declared_type(target) != content_type:
        raise OOXMLInvalid(f"{owner} {what} relationship does not name a {what} part")
    return target


def _diagram_reference(
    element: ElementTree.Element, owner: str, holder: str
) -> str | None:
    """Return the relationship id a holder's SmartArt data reference names, or None.

    A holder declares its graphic on exactly one route: the ``a:graphic``
    directly below it and the ``a:graphicData`` directly below that, both
    qualified in the DrawingML main namespace. Only graphic data on that route
    declares anything, so a lookalike nested further down -- in an
    alternate-content fallback, in a text body, inside another graphic -- or one
    written in a foreign namespace, is not the holder's declaration and routes
    nowhere.

    Graphic data on the route says the graphic it holds is SmartArt in its own
    ``uri``: that attribute names the vocabulary the graphic data is written in,
    and for SmartArt it is one of the two diagram namespaces OOXML defines. It
    is read unqualified, as the schema writes it. Graphic data carrying any
    other URI -- or none -- is some other kind of graphic, so its paragraphs
    stay the holder's own text and are never read as a diagram.

    The reference itself is the ``dgm:relIds`` the qualified graphic data holds
    as a direct child, written in the namespace that ``uri`` declared, and the
    only id read off it is a relationship-namespaced ``dm``: an id written in no
    namespace, or in another one, names no relationship, and the layout,
    quick-style and colour relationships beside it name parts nothing here
    opens. A holder shows one graphic object, so two qualifying graphic data
    elements leave which diagram it shows ambiguous; and graphic data that
    declares itself a diagram and then names no reference, or more than one, is
    refused rather than read for whatever text happens to be in it. The id is
    read from the holder itself; no diagram path is derived from the holding
    part's own name.

    A slide's graphic frame and a Word story's drawing object declare SmartArt
    on the one route, so both are read here. ``owner`` is the format the holder
    came from and ``holder`` is what that format calls it, carried through so a
    refusal names the file it came from and never the other format.
    """
    qualified = [
        node
        for graphic in element
        if graphic.tag in _DRAWING_GRAPHIC
        for node in graphic
        if node.tag in _DRAWING_GRAPHIC_DATA
        and node.attrib.get("uri", "") in _DIAGRAM_NAMESPACES
    ]
    if not qualified:
        return None
    if len(qualified) > 1:
        raise OOXMLInvalid(f"{owner} {holder} holds more than one SmartArt reference")
    namespace = qualified[0].attrib["uri"]
    named = [node for node in qualified[0] if node.tag == f"{{{namespace}}}relIds"]
    if len(named) != 1:
        raise OOXMLInvalid(
            f"{owner} SmartArt graphic data does not name exactly one diagram reference"
        )
    ids = _namespaced_ids(named[0], "dm")
    if not ids:
        raise OOXMLInvalid(f"{owner} SmartArt reference names no relationship")
    if len(ids) > 1:
        raise OOXMLInvalid(
            f"{owner} SmartArt reference names more than one relationship"
        )
    return ids[0]


def _pptx_frame_object(
    package: _Package, part: str, frame: ElementTree.Element
) -> dict[str, object] | None:
    """Inventory the embedded or linked object a graphic frame holds, inertly.

    The route in is the schema's own and every hop is direct: the ``a:graphic``
    below the frame, the ``a:graphicData`` below that -- both in the DrawingML
    main namespace, Transitional or Strict -- and a ``p:oleObj`` directly below
    that, in a PresentationML main namespace. An ``oleObj`` anywhere else is not
    this frame's declaration and declares nothing: one nested deeper, one inside
    an alternate-content fallback or another frame's graphic carried along, one
    below the preview picture, and one written in a vocabulary that merely shares
    the local name are all passed over, so a frame holding no object is reported
    as holding none rather than as holding something found elsewhere.

    A frame holds one graphic object, so two qualifying declarations leave which
    object the frame shows ambiguous and nothing here chooses between them.

    Which kind of object it is comes from one direct ``p:embed`` or ``p:link``
    and from nowhere else -- not from the relationship, not from the target's
    name, not from the ``progId``. Neither child, or both, or two of one, leaves
    that undeclared or contradicted, so the frame is refused rather than read as
    whichever was written first. The ``p:pic`` beside them is the preview image
    standing in for the object on the slide: it is recorded as declared and is
    never opened, decoded, resolved or mistaken for the object itself.

    The single relationship-namespaced ``r:id`` on the object element is the one
    reference read; an id written in no namespace, or in two of the namespaces
    that carry it, names no one relationship. It resolves only through the
    holding part's own relationships, so containment, path safety and existence
    were all settled when the package validated them -- which is why an embedded
    target is a member the archive holds and nothing re-checks it here -- and it
    has to carry one of the types OOXML defines for an object. The declaration
    and the relationship then have to agree: an embedded object is a part inside
    the package and a linked one is a reference out of it, so an embed naming an
    external target, or a link naming an internal part, is refused rather than
    reported as the other.

    Nothing here parses, decodes, executes, launches or fetches an object: an
    embedded one contributes its safe part name, the type the package declares
    for it and the already-measured byte length, a linked one is carried exactly
    as the relationship holds it beside the scheme its own text names, and the
    object's own bounded claims are reported as written and never interpreted.
    """
    declared = [
        node
        for graphic in frame
        if graphic.tag in _DRAWING_GRAPHIC
        for data in graphic
        if data.tag in _DRAWING_GRAPHIC_DATA
        for node in data
        if node.tag in _PPTX_OLE_OBJECT
    ]
    if not declared:
        return None
    if len(declared) > 1:
        raise OOXMLInvalid("PPTX graphic frame holds more than one embedded object")
    node = declared[0]
    modes = [
        (_PPTX_OLE_MODES[child.tag], child)
        for child in node
        if child.tag in _PPTX_OLE_MODES
    ]
    if len(modes) != 1:
        raise OOXMLInvalid(
            "PPTX embedded object does not declare exactly one of embed and link"
        )
    previews = [child for child in node if child.tag in _PPTX_OLE_PREVIEW]
    if len(previews) > 1:
        raise OOXMLInvalid("PPTX embedded object declares more than one preview")
    ids = _namespaced_ids(node, "id")
    if len(ids) > 1:
        raise OOXMLInvalid("PPTX embedded object names more than one relationship")
    if not ids:
        raise OOXMLInvalid("PPTX embedded object names no relationship")
    rels = posixpath.join(
        posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
    )
    entry = package.relationships.get(rels, {}).get(ids[0])
    if entry is None:
        raise OOXMLInvalid("PPTX embedded object names a missing relationship")
    kind = package.relationship_types.get(rels, {}).get(ids[0], "")
    if not _is_defined_relationship(kind, _OLE_RELATIONSHIPS):
        raise OOXMLInvalid("PPTX embedded object names a relationship of another type")
    mode, declaration = modes[0]
    target, external = entry
    if external != (mode == "link"):
        raise OOXMLInvalid("PPTX embedded object contradicts the relationship it names")
    record = dict(_PPTX_OLE_RECORD)
    record.update(
        {
            "oleMode": mode,
            "oleRelationshipId": _clip(ids[0], _MAX_META_CHARS),
            "oleRelationshipType": _clip(kind, _MAX_TARGET_CHARS),
            "oleTarget": _clip(target, _MAX_TARGET_CHARS),
            "oleExternal": external,
            "oleShowAsIcon": _pptx_ole_flag(node, "showAsIcon"),
            "olePreviewDeclared": bool(previews),
            **{
                field: _pptx_ole_extent(node, name)
                for name, field in _PPTX_OLE_EXTENTS
            },
            **{
                field: _ole_claim(node, name)
                for name, field in _PPTX_OLE_CLAIMS
            },
        }
    )
    if mode == "link":
        match = _SCHEME.match(target)
        record["oleScheme"] = match.group(1).casefold() if match else None
        record["oleUpdateAutomatic"] = _pptx_ole_flag(declaration, "updateAutomatic")
        return record
    record["olePart"] = _clip(target, _MAX_TARGET_CHARS)
    record["oleContentType"] = _clip(package.resolved_type(target), _MAX_META_CHARS)
    record["oleBytes"] = package.archive.getinfo(target).file_size
    return record


def _ole_claim(element: ElementTree.Element, name: str) -> str | None:
    """Return one of an object's unqualified text claims, clipped, or None.

    Read unqualified, as the schema writes it: the same local name carried in
    some other vocabulary's namespace is that vocabulary's extension and claims
    nothing about this object. A claim written empty stays the empty claim the
    file made rather than becoming an absent one. Nothing here interprets a
    claim, so this reads a slide's object and a story's object alike.
    """
    value = element.attrib.get(name)
    return None if value is None else _clip(value, _MAX_META_CHARS)


def _pptx_ole_flag(element: ElementTree.Element, name: str) -> bool | None:
    """Read one of an object's boolean attributes, or None where absent.

    ``None`` says the file declared nothing, which is not the same as a declared
    false, and no default is invented for it. The value is closed, so one
    outside the type is refused rather than read as whichever of the two it
    resembles.
    """
    value = element.attrib.get(name)
    if value is None:
        return None
    if value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise OOXMLInvalid("PPTX embedded object flag is malformed")


def _pptx_ole_extent(element: ElementTree.Element, name: str) -> int | None:
    """Read one of an object's image extents, or None where absent.

    The extent is kept exactly as written, in EMU, and is never scaled against
    the frame's own transform or anything else. It is unsigned, so a negative
    width or height is not a value the type has, and the range is the one the
    type is derived from.
    """
    raw = element.attrib.get(name)
    if raw is None:
        return None
    if _EMU_EXTENT.fullmatch(raw) is None or int(raw) > _MAX_PPTX_OLE_COORDINATE:
        raise OOXMLInvalid("PPTX embedded object image extent is malformed")
    return int(raw)


def _diagram_root(
    package: _Package, part: str, owner: str
) -> tuple[ElementTree.Element, str]:
    """Parse one diagram data part, refusing a root that is not a data model.

    The declared content type says what the part is; the root element is where
    the part says so itself, in one of the two namespaces OOXML defines for
    DrawingML diagrams. Anything else is not the vocabulary the declaration
    promised, so the part is refused rather than read for whatever happens to be
    in it. The namespace it is rooted in is returned so every structural element
    below it is matched in that one vocabulary: a lookalike from somewhere else
    is another vocabulary's element that happens to share a local name.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _DIAGRAM_NAMESPACES:
        raise OOXMLInvalid(f"{owner} diagram part uses an unsupported namespace")
    if local != "dataModel":
        raise OOXMLInvalid(f"{owner} diagram part is not rooted at its own data model")
    return root, namespace


def _diagram_list(
    root: ElementTree.Element, namespace: str, name: str, owner: str
) -> ElementTree.Element | None:
    """Return the data model's one point or connection list, or None for none.

    Each list is written once, so a second of either leaves the data model with
    two of something nothing here picks between.
    """
    found = [node for node in root if node.tag == f"{{{namespace}}}{name}"]
    if len(found) > 1:
        raise OOXMLInvalid(f"{owner} diagram declares more than one of a list")
    return found[0] if found else None


def _diagram_model_id(
    element: ElementTree.Element, name: str, owner: str, *, required: bool = True
) -> str | None:
    """Read one ``ST_ModelId`` verbatim, refusing a value that is not one.

    ``ST_ModelId`` is a union of ``ST_Guid`` and ``xsd:unsignedInt``, so either
    form is read, and the value is kept exactly as written -- never recased,
    rebraced or renumbered -- so an id stays the one the file cited.
    """
    raw = element.attrib.get(name)
    if raw is None:
        if required:
            raise OOXMLInvalid(f"{owner} diagram model id is missing")
        return None
    if _GUID.fullmatch(raw) is None and (
        _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX
    ):
        raise OOXMLInvalid(f"{owner} diagram model id is malformed")
    return raw


def _diagram_order(element: ElementTree.Element, name: str, owner: str) -> str:
    """Read one connection order verbatim, refusing a value that is not one.

    The order is the position the connection itself encodes, kept as written and
    never renumbered, so only its range is checked.
    """
    raw = element.attrib.get(name)
    if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid(f"{owner} diagram connection order is malformed")
    return raw


def _diagram_records(
    package: _Package, part: str, owner: str
) -> tuple[list[tuple[dict[str, object], str]], list[dict[str, object]]]:
    """Read one data model's stored point text and its encoded connections.

    Only what the part already wrote is reported. Every point's own ``dgm:t`` is
    read, in the order the point list wrote the points and the order the body
    wrote its paragraphs and runs, and the kind the point claims rides beside
    the text rather than deciding whether it is read: a layout point is reported
    as the ``pres`` point it says it is. Nothing is taken from the layout,
    quick-style or colour parts, from the drawing part a data model may ship
    beside it, or from any rendered geometry, so no label, hierarchy or reading
    order is inferred from how the diagram would look.

    Connections come back as the identity they encode and nothing more: the
    source and destination each connection names, the orders it claims, and the
    transitions it cites. They are reported, never walked, so nothing here
    assembles them into a tree or numbers anything from them. A connection
    citing a point the list never defines is kept as the claim the file made,
    marked unresolved, because no identity is invented to stand in for one.

    Two points, or two connections, claiming one model id leave every reference
    to it ambiguous, so the part is refused rather than read under one of them.
    Points and connections are separate collections, so one id in each is two
    records rather than a repeat.

    A diagram data part is a data model whichever format relates it, so a
    presentation and a document are read by this one reader under one set of
    bounds and one set of refusals. ``owner`` is the format that related it,
    carried through so a refusal names the file it came from and never the other
    format.
    """
    root, namespace = _diagram_root(package, part, owner)
    holder = _diagram_list(root, namespace, "ptLst", owner)
    if holder is None:
        raise OOXMLInvalid(f"{owner} diagram declares no point list")
    points: list[tuple[dict[str, object], str]] = []
    point_ids: set[str] = set()
    paragraphs = runs = characters = 0
    for index, node in enumerate(holder):
        if node.tag != f"{{{namespace}}}pt":
            raise OOXMLInvalid(
                f"{owner} diagram point list holds an unsupported element"
            )
        if index >= _MAX_DIAGRAM_POINTS:
            raise OOXMLInvalid(f"{owner} diagram has too many points")
        model_id = _diagram_model_id(node, "modelId", owner)
        assert model_id is not None
        if model_id.casefold() in point_ids:
            raise OOXMLInvalid(f"{owner} diagram model id is repeated")
        point_ids.add(model_id.casefold())
        point_type = node.attrib.get("type", "node")
        if point_type not in _DIAGRAM_POINT_TYPES:
            raise OOXMLInvalid(f"{owner} diagram point type is not a defined kind")
        place: dict[str, object] = {
            "diagramRole": "point_text",
            "modelId": model_id,
            "pointType": point_type,
            "pointIndex": index,
            # ``pt/@cxnId`` is the connection a transition point encodes; it is
            # recorded as written and never followed.
            "connectionModelId": _diagram_model_id(
                node, "cxnId", owner, required=False
            ),
        }
        bodies = [item for item in node if item.tag == f"{{{namespace}}}t"]
        if len(bodies) > 1:
            raise OOXMLInvalid(f"{owner} diagram point declares more than one text body")
        if not bodies:
            continue
        for paragraph_index, paragraph in enumerate(_pptx_paragraphs(bodies[0])):
            paragraphs += 1
            if paragraphs > _MAX_DIAGRAM_PARAGRAPHS:
                raise OOXMLInvalid(f"{owner} diagram has too many paragraphs")
            for run_index, text in enumerate(_pptx_runs(paragraph)):
                # Counted before the text is looked at: an empty run is work the
                # diagram asked for even though it reports nothing.
                runs += 1
                if runs > _MAX_DIAGRAM_RUNS:
                    raise OOXMLInvalid(f"{owner} diagram has too many runs")
                if not text:
                    # An empty run keeps its index; it carries no text to report.
                    continue
                characters += len(text)
                if characters > _MAX_DIAGRAM_CHARS:
                    raise OOXMLInvalid(
                        f"{owner} diagram text exceeds the accepted bound"
                    )
                points.append(
                    ({**place, "paragraph": paragraph_index, "run": run_index}, text)
                )
    connections: list[dict[str, object]] = []
    connection_ids: set[str] = set()
    linked = _diagram_list(root, namespace, "cxnLst", owner)
    for index, node in enumerate(() if linked is None else linked):
        if node.tag != f"{{{namespace}}}cxn":
            raise OOXMLInvalid(
                f"{owner} diagram connection list holds an unsupported element"
            )
        if index >= _MAX_DIAGRAM_CONNECTIONS:
            raise OOXMLInvalid(f"{owner} diagram has too many connections")
        model_id = _diagram_model_id(node, "modelId", owner)
        assert model_id is not None
        if model_id.casefold() in connection_ids:
            raise OOXMLInvalid(f"{owner} diagram model id is repeated")
        connection_ids.add(model_id.casefold())
        kind = node.attrib.get("type", "parOf")
        if kind not in _DIAGRAM_CONNECTION_TYPES:
            raise OOXMLInvalid(f"{owner} diagram connection type is not a defined kind")
        source = _diagram_model_id(node, "srcId", owner)
        destination = _diagram_model_id(node, "destId", owner)
        assert source is not None and destination is not None
        connections.append(
            {
                "kind": "diagramConnection",
                "modelId": model_id,
                "connectionIndex": index,
                "connectionType": kind,
                "sourceModelId": source,
                "destinationModelId": destination,
                "sourceOrder": _diagram_order(node, "srcOrd", owner),
                "destinationOrder": _diagram_order(node, "destOrd", owner),
                "parentTransitionModelId": _diagram_model_id(
                    node, "parTransId", owner, required=False
                ),
                "siblingTransitionModelId": _diagram_model_id(
                    node, "sibTransId", owner, required=False
                ),
                # Stated so no consumer reads an id above as a point that is
                # there. Ids are compared case-folded because only a GUID's hex
                # digits vary in case.
                "sourceResolved": source.casefold() in point_ids,
                "destinationResolved": destination.casefold() in point_ids,
            }
        )
    return points, connections


def _chart_root(package: _Package, part: str, owner: str) -> ElementTree.Element:
    """Parse one chart part, refusing a root that is not a chart space.

    The declared content type says what the part is; the root element is where
    the part says so itself, in one of the two namespaces OOXML defines for
    DrawingML charts. Anything else is not the vocabulary the declaration
    promised, so the part is refused rather than read for whatever happens to
    be in it.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    if namespace.lstrip("{") not in _CHART_NAMESPACES:
        raise OOXMLInvalid(f"{owner} chart part uses an unsupported namespace")
    if local != "chartSpace":
        raise OOXMLInvalid(f"{owner} chart part is not rooted at its own chart space")
    return root


def _chart_records(
    package: _Package, part: str, owner: str
) -> list[tuple[dict[str, object], str]]:
    """Read one chart's stored title and stored points as (place, value) records.

    Only what the package already stored is reported: the title text as
    written, and the caches and inline literal lists each series carries. The
    embedded workbook a chart may ship is never opened, an absent cache value is
    never invented, and nothing is evaluated, so a record's source formula is
    inert metadata beside the value rather than something resolved.

    Records come out in source order -- the title, then each series as the
    chart wrote it, and inside a series its name, categories and values, each
    level of a multi-level category axis in turn. A series index or order
    claimed twice leaves two series in one position, a slot written twice leaves
    a series with two of one thing, and a point index claimed twice leaves one
    position with two values, so the chart is refused rather than read under one
    of them.

    A chart part is a chart part whichever format relates it, so a presentation
    and a document are read by this one reader under one set of bounds and one
    set of refusals. ``owner`` is the format that related it, carried through so
    a refusal names the file it came from and never the other format.
    """
    root = _chart_root(package, part, owner)
    # Only the chart's own title: an axis or a legend carries a ``c:title`` of
    # its own further down, and neither is what the chart is called.
    chart = next((node for node in root if _local(node.tag) == "chart"), None)
    titles = [] if chart is None else [n for n in chart if _local(n.tag) == "title"]
    if len(titles) > 1:
        raise OOXMLInvalid(f"{owner} chart holds more than one title")
    records: list[tuple[dict[str, object], str]] = (
        [] if not titles else _chart_title(titles[0], owner)
    )
    indices: set[str] = set()
    orders: set[str] = set()
    points = 0
    for series in (node for node in root.iter() if _local(node.tag) == "ser"):
        if len(indices) >= _MAX_CHART_SERIES:
            raise OOXMLInvalid(f"{owner} chart has too many series")
        index = _chart_index(series, "idx", owner)
        order = _chart_index(series, "order", owner)
        if index in indices or order in orders:
            raise OOXMLInvalid(f"{owner} chart series index or order is repeated")
        indices.add(index)
        orders.add(order)
        for slot, role in _CHART_SLOTS:
            holders = [item for item in series if _local(item.tag) == slot]
            if len(holders) > 1:
                raise OOXMLInvalid(f"{owner} chart series holds more than one of a slot")
            if not holders:
                continue
            formula, cached = _chart_cache(holders[0], owner)
            points += len(cached)
            if points > _MAX_CHART_POINTS:
                raise OOXMLInvalid(f"{owner} chart has too many cached points")
            records.extend(
                (
                    {
                        **_CHART_PLACE,
                        "chartRole": role,
                        "seriesIndex": index,
                        "seriesOrder": order,
                        "categoryLevel": level,
                        "pointIndex": point,
                        "formula": formula,
                    },
                    value,
                )
                for level, point, value in cached
            )
    return records


def _chart_title(
    title: ElementTree.Element, owner: str
) -> list[tuple[dict[str, object], str]]:
    """Read the title text a chart stored, as rich runs or as a stored value.

    A rich title is the text the author typed, joined as written; a referenced
    title is the stored copy of what its source held, reported with the formula
    beside it. A title that holds neither leaves nothing to report, and nothing
    is taken from a layout or recomputed from the data.
    """
    holder = next((node for node in title if _local(node.tag) == "tx"), None)
    if holder is None:
        return []
    rich = next((node for node in holder if _local(node.tag) == "rich"), None)
    if rich is not None:
        runs = [node.text or "" for node in rich.iter() if _local(node.tag) == "t"]
        if len(runs) > _MAX_CHART_TITLE_RUNS:
            raise OOXMLInvalid(f"{owner} chart title has too many runs")
        return [({**_CHART_PLACE, "chartRole": "chart_title"}, "".join(runs))]
    formula, cached = _chart_cache(holder, owner)
    return [
        (
            {
                **_CHART_PLACE,
                "chartRole": "chart_title",
                "categoryLevel": level,
                "pointIndex": point,
                "formula": formula,
            },
            value,
        )
        for level, point, value in cached
    ]


def _chart_cache(
    holder: ElementTree.Element, owner: str
) -> tuple[str | None, list[tuple[str | None, str | None, str]]]:
    """Read one slot's source formula and the points it already stored.

    A slot holds one source: a reference, whose cache is the stored copy of what
    the source once held, or a value written inline -- a single literal, or a
    literal list of its own. A reference carrying no cache stored nothing, so it
    reports nothing rather than a value fetched from the source it names, and a
    reference's formula stays inert metadata either way. Two sources in one slot
    leave which one the slot holds ambiguous, so the chart is refused rather
    than read from whichever comes first.

    Each point comes back as the category level it sits at, its index and its
    value. Only a multi-level reference has levels, so every other source
    reports none, and a single literal names no index either.
    """
    sources = [node for node in holder if _local(node.tag) in _CHART_SOURCES]
    if len(sources) > 1:
        raise OOXMLInvalid(f"{owner} chart slot holds more than one data source")
    if not sources:
        return None, []
    source = sources[0]
    local = _local(source.tag)
    if local == "v":
        return None, [(None, None, source.text or "")]
    if local in _CHART_LITERALS:
        return None, _chart_points(source, _chart_declared(source, owner), owner)
    formula = next((node for node in source if _local(node.tag) == "f"), None)
    stored = None if formula is None else _clip(formula.text or "", _MAX_TARGET_CHARS)
    cache = next(
        (node for node in source if _local(node.tag) == _CHART_REFERENCES[local]), None
    )
    if cache is None:
        return stored, []
    if local == "multiLvlStrRef":
        return stored, _chart_levels(cache, owner)
    return stored, _chart_points(cache, _chart_declared(cache, owner), owner)


def _chart_levels(
    cache: ElementTree.Element, owner: str
) -> list[tuple[str | None, str | None, str]]:
    """Read a multi-level category cache, level by level in source order.

    Every level indexes the same points, so the level a point sits at is
    reported beside its index: without it one position in two levels would be
    two records in the same place. Levels are numbered as written and never
    reordered, because which end of the sequence is the outermost is the chart's
    own convention rather than anything restated here. ``ptCount`` sits on the
    cache, not on its levels, so it bounds the indices of all of them.
    """
    levels = [node for node in cache if _local(node.tag) == "lvl"]
    if len(levels) > _MAX_CHART_CATEGORY_LEVELS:
        raise OOXMLInvalid(f"{owner} chart has too many category levels")
    declared = _chart_declared(cache, owner)
    return [
        point
        for level, node in enumerate(levels)
        for point in _chart_points(node, declared, owner, str(level))
    ]


def _chart_declared(holder: ElementTree.Element, owner: str) -> int | None:
    """Read what a cache or a literal list claims its source held, if it says."""
    count = next((node for node in holder if _local(node.tag) == "ptCount"), None)
    return None if count is None else _chart_count(count, "val", owner)


def _chart_points(
    holder: ElementTree.Element,
    declared: int | None,
    owner: str,
    level: str | None = None,
) -> list[tuple[str | None, str | None, str]]:
    """Read one list's stored points in source order, refusing a malformed one.

    ``declared`` is what the list claims its source held, so it bounds the
    indices the list may use; nothing is materialised for an index the list left
    out, because a cache is sparse by design and a literal list is written the
    same way. A point without a value stored none, which is not the same as
    having stored an empty one, so it is refused rather than read as blank.
    """
    points: list[tuple[str | None, str | None, str]] = []
    seen: set[str] = set()
    for node in holder:
        if _local(node.tag) != "pt":
            continue
        index = _chart_count(node, "idx", owner)
        if declared is not None and index >= declared:
            raise OOXMLInvalid(f"{owner} chart cache point index is outside its count")
        if str(index) in seen:
            raise OOXMLInvalid(f"{owner} chart cache point index is repeated")
        seen.add(str(index))
        value = next((item for item in node if _local(item.tag) == "v"), None)
        if value is None or len(value):
            raise OOXMLInvalid(f"{owner} chart cache point value is malformed")
        points.append((level, str(index), value.text or ""))
    return points


def _chart_count(element: ElementTree.Element, name: str, owner: str) -> int:
    """Read one ``xsd:unsignedInt`` attribute, refusing a value that is not one."""
    raw = element.attrib.get(name)
    if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid(f"{owner} chart cache point count is malformed")
    return int(raw)


def _chart_index(series: ElementTree.Element, name: str, owner: str) -> str:
    """Read one series index or order verbatim, refusing a malformed value.

    The value is kept as the chart wrote it, never renumbered, so only its
    range is checked.
    """
    node = next((item for item in series if _local(item.tag) == name), None)
    if node is None:
        raise OOXMLInvalid(f"{owner} chart series index or order is malformed")
    raw = node.attrib.get("val")
    if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid(f"{owner} chart series index or order is malformed")
    return raw


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

    Z-order is not reading order, so each shape also carries the rank
    ``_pptx_reading_ranks`` gives it among its siblings -- ``readingOrder``,
    and ``readingPath`` for the path of ranks down to it. That is a documented
    heuristic recorded beside the source order, never in place of it: segments
    are emitted in source order, as they were, and both orders stay readable
    off every segment a shape emits.

    Each run of each paragraph becomes one segment, in source order, carrying
    its paragraph and run index inside the shape it belongs to. A graphic frame
    holding a table reports its cells as table cells instead, each at its own
    row and column in the declared grid; one referring to a chart reports that
    chart's stored title and stored points; one whose graphic data declares
    itself SmartArt reports that data model's stored point text and the
    connections it encodes. A picture is inventoried where it sits, with the
    name, title and description its author claimed kept apart, and each
    non-empty alt claim reported as text of its own, and the image it names
    recorded as the part or the link it is; the image itself is never opened or
    decoded. Geometry and
    the placeholder reference are recorded exactly as the shape encodes them;
    no value is resolved from a layout or master, and no group child-space
    mapping is applied, so a shape that encodes no transform carries none.

    ``inherited`` then reports the text the slide's layout and master show
    behind it, as text in its own right at the part that holds it. It is a
    pass of its own rather than part of the walk above, because what it reads
    is another part's shape tree and what suppresses a repeat is everything
    the walk already reported. It finishes by resolving the slide's own
    placeholders against those two parts: a slot the slide left empty shows the
    text the applicable placeholder saved, reported at the shape that saved it
    and at the slide shape applying it.

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
        package: _Package,
    ) -> None:
        self._capture_id = capture_id
        self._part = part
        self._slide_id = slide_id
        self._ordinal = ordinal
        self._hidden = hidden
        self._segments = segments
        self._package = package
        self._ids: set[str] = set()
        # Every visual identity this slide has already shown, in the order-free
        # form ``_visual`` builds. The inherited pass consults it; the slide's
        # own pass only adds to it.
        self._shown: set[tuple[str, str, tuple[tuple[str, int], ...] | None]] = set()
        # The slot each of the slide's own placeholders names, paired with the
        # slide shape naming it, for the ones that saved no text of their own.
        # Collected by the walk in its order and resolved once both inherited
        # parts are known.
        self._applying: list[tuple[tuple[str, int], dict[str, object]]] = []
        self.shapes = 0
        self.paragraphs = 0
        self.runs = 0
        self.cells = 0

    def walk(
        self,
        parent: ElementTree.Element,
        path: tuple[str, ...],
        z_path: tuple[int, ...],
        reading_path: tuple[int, ...],
        depth: int,
    ) -> None:
        # The level is read in full before any of it is reported, because the
        # reading-order heuristic ranks a shape against its own siblings and so
        # needs every sibling's transform. Counting, identity and geometry are
        # validated exactly as they were, in source order, and what is reported
        # afterwards is reported in source order too.
        level: list[tuple[ElementTree.Element, str, str, dict[str, int] | None]] = []
        for child in _pptx_children(parent):
            kind = _local(child.tag)
            if kind not in _PPTX_SHAPES:
                continue
            self.shapes += 1
            if self.shapes > _MAX_PPTX_SHAPES:
                raise OOXMLInvalid("PPTX slide has too many shapes")
            shape_id = _pptx_shape_id(child, self._ids, "slide")
            # Validated for every shape, including a group and a picture: a
            # malformed transform is malformed wherever it is written.
            level.append((child, kind, shape_id, _pptx_geometry(child)))
        ranks = _pptx_reading_ranks([geometry for *_, geometry in level])
        for position, (child, kind, shape_id, geometry) in enumerate(level):
            here = (*z_path, position)
            rank = (*reading_path, ranks[position])
            if kind == "grpSp":
                if depth >= _MAX_PPTX_GROUP_DEPTH:
                    raise OOXMLInvalid("PPTX group shapes are nested too deeply")
                self.walk(child, (*path, shape_id), here, rank, depth + 1)
                continue
            where = self._where(child, kind, shape_id, path, here, rank, geometry)
            frame = kind == "graphicFrame"
            table = _pptx_frame_table(child) if frame else None
            chart = _pptx_frame_chart(child) if frame else None
            diagram = (
                _diagram_reference(child, "PPTX", "graphic frame") if frame else None
            )
            ole = (
                _pptx_frame_object(self._package, self._part, child) if frame else None
            )
            if sum(held is not None for held in (table, chart, diagram, ole)) > 1:
                raise OOXMLInvalid("PPTX graphic frame holds more than one graphic")
            if chart is not None:
                self._chart(chart, where)
            elif diagram is not None:
                self._diagram(diagram, where)
            elif table is not None:
                self._table(table, where)
            elif ole is not None:
                self._object(ole, where)
            elif kind == "pic":
                self._picture(child, where)
            elif kind in _PPTX_TEXT_SHAPES:
                saved = self._emit(child, "body", where)
                # A slot the slide shows nothing of its own in is the one that
                # shows what the layout or master saved for it, so what the
                # shape itself just reported is what decides that.
                if kind in _PPTX_INHERITED_SHAPES and not saved:
                    self._applies(child, where)

    def _applies(
        self, shape: ElementTree.Element, where: dict[str, object]
    ) -> None:
        """Record an empty slide placeholder, and the slide shape naming it.

        Only a shape that is a placeholder at all: a shape holding no reference
        inherits into nothing. The slide shape is named alongside the slot
        because the text that applies here is the inherited part's, shown at a
        position and in a frame the slide's own shape supplies -- which is why
        the slide shape's reading-order rank rides along with its z-order: the
        slot is read where the slide places it, not where the layout wrote it.
        Both orders are kept: the inherited shape's own rank among its own
        part's siblings stays on the record, and the slide slot's rank is added
        beside it under its own ``slide``-prefixed names.
        """
        placeholder = _pptx_placeholder(shape)
        if placeholder is None:
            return
        self._applying.append(
            (
                _pptx_placeholder_slot(placeholder),
                {
                    "placeholderType": where["placeholderType"],
                    "placeholderIndex": where["placeholderIndex"],
                    "slideShapeId": where["shapeId"],
                    "slideShapeKind": where["shapeKind"],
                    "slideGroupPath": where["groupPath"],
                    "slideZOrder": where["zOrder"],
                    "slideZPath": where["zPath"],
                    "slideReadingOrder": where["readingOrder"],
                    "slideReadingPath": where["readingPath"],
                    "slideGeometry": where["geometry"],
                },
            )
        )

    def inherited(self, slide_root: ElementTree.Element) -> None:
        """Emit the authored text the slide's layout and master show behind it.

        Routing is exact and goes one hop at a time: the layout is the one part
        the slide itself relates as a layout, and the master is the one that
        layout itself relates as a master. Each relationship has to be
        internal, has to carry one of the two URIs OOXML defines for its type,
        and has to resolve to a part the package declares with that type's own
        content type and that is rooted at its own element in a PresentationML
        namespace. Nothing is reached by a part's name, by substituting into
        the slide's, or by scanning the package for a member that looks like
        one, and a slide relating no layout inherits nothing rather than being
        refused.

        ``showMasterSp`` is honoured where each part writes it. A slide that
        turns inherited shapes off shows neither its layout's nor its master's,
        so neither part is opened -- and with neither open there is nothing for
        the slide's placeholders to resolve against either, so nothing is
        inherited into them. A layout that turns them off shows its own but not
        the master's, and a slot its own placeholder leaves empty therefore
        falls through to nothing rather than to the master. The attribute is
        only ever written to turn them off, so an absent one shows them.
        """
        if not _pptx_shows_master(slide_root):
            return
        layout = _related_part(
            self._package,
            self._part,
            _LAYOUT_RELATIONSHIPS,
            _LAYOUT_CONTENT_TYPE,
            "slide layout",
        )
        if layout is None:
            return
        layout_id, layout_part = layout
        layout_root = _pptx_inherited_root(self._package, layout_part, _LAYOUT_ROOT)
        prompts = self._inherited(layout_part, layout_id, layout_root, "layout")
        master = (
            _related_part(
                self._package,
                layout_part,
                _MASTER_RELATIONSHIPS,
                _MASTER_CONTENT_TYPE,
                "slide master",
                owner="slide layout",
            )
            if _pptx_shows_master(layout_root)
            else None
        )
        beneath: list[_PptxPrompt] = []
        if master is not None:
            master_id, master_part = master
            beneath = self._inherited(
                master_part,
                master_id,
                _pptx_inherited_root(self._package, master_part, _MASTER_ROOT),
                "master",
            )
        self._applicable(prompts, beneath)

    def _applicable(
        self, layout: list[_PptxPrompt], master: list[_PptxPrompt]
    ) -> None:
        """Emit the saved placeholder text each empty slide slot shows.

        One hop at a time, as the routing to the parts themselves was: the
        slide's placeholder resolves to the applicable one on its layout, and
        that one's own slot resolves on to the master only when the layout
        saved no text for it. A layout that saved some is what the slide shows,
        so the master's prompt for the same slot is not reported behind it.
        Carrying the slide's slot on to the master is the same lookup as
        carrying the layout's: a stated index matched on the index alone, and
        the default one matched on a type the two already had to agree on.

        A slot no placeholder on the layout names inherits nothing. It is not
        looked up on the master either -- the chain to the master runs through
        the layout -- and it is never reported as text some non-matching
        placeholder happens to hold, because that is text authored for another
        slot and the slide shows none of it.

        Each record is emitted in the order the walk met the slide shape that
        applies it, after both inherited parts have reported their own
        non-placeholder text, and carries the provenance of the shape that
        saved the text together with the slide shape showing it. Suppression is
        the inherited pass's own: a prompt whose role, normalised text and
        encoded transform the slide already showed is not shown again.
        """
        for slot, applied in self._applying:
            prompt = _pptx_applicable(slot, layout, "layout")
            if prompt is not None and not prompt.holds_text:
                prompt = _pptx_applicable(slot, master, "master")
            if prompt is None:
                continue
            self._emit(
                prompt.shape, "body", {**prompt.where, **applied}, suppress=True
            )

    def _inherited(
        self,
        part: str,
        relation_id: str,
        root: ElementTree.Element,
        level: str,
    ) -> list[_PptxPrompt]:
        """Emit one inherited part's own authored text, in its declared order.

        Only a shape directly below that part's shape tree is read: a group is
        not entered, and a graphic frame or picture is left alone, so no table,
        chart or diagram is reached through a part whose own relationships
        nothing here resolves. Every shape in the tree still takes its place in
        the z-order and has its identity validated, exactly as on a slide, so
        skipping one never shifts another's position.

        A shape holding a placeholder reference is not reported as this part's
        own text whatever it holds: it describes a slot, and whether the slide
        shows what it saved there is placeholder applicability, which
        ``_applicable`` settles against both parts once each has been read.
        Each one is returned for that, in the order the tree declared it.

        The reading-order heuristic runs here too, and runs entirely inside
        this part. There is one shape-tree level to rank -- a group is not
        entered -- so these shapes are ranked against each other in the one
        coordinate space they share, this part's own, and never against the
        slide's: no layout or master offset is compared with a slide offset and
        no slide position is derived for a shape that holds none. ``zOrder``
        stays the scalar source position it was and the rank is recorded beside
        it, as ``readingOrder`` with the single-level ``readingPath`` down to
        it. Where the slide itself shows one of these shapes, through a
        placeholder, ``_applies`` adds the applying slide shape's own rank
        alongside, so such a record carries both parts' orders.

        An exact visual duplicate of something already shown is not shown
        twice. That is decided on the whole of what a run puts on the slide --
        its role, its text once whitespace is normalised, and the transform the
        shape holding it encodes, exactly as encoded -- never on the text
        alone. Two shapes carrying the same words at different positions are
        two authored things and both are reported; only the same words in the
        same place are the one thing seen twice. Suppression never moves an
        index, so each locator names where its run actually sits.
        """
        prompts: list[_PptxPrompt] = []
        seen: set[str] = set()
        # The one level is read in full before any of it is reported, because
        # the heuristic ranks a shape against its own siblings and so needs
        # every sibling's transform -- including the ones whose text is not
        # read, which hold a position in both orders just as they always did.
        # Counting, identity and geometry are validated in source order, and
        # what is reported afterwards is reported in source order too.
        siblings: list[tuple[ElementTree.Element, str, str, dict[str, int] | None]] = []
        for child in _pptx_children(_pptx_inherited_tree(root, level)):
            kind = _local(child.tag)
            if kind not in _PPTX_SHAPES:
                continue
            self.shapes += 1
            if self.shapes > _MAX_PPTX_SHAPES:
                raise OOXMLInvalid("PPTX slide has too many shapes")
            shape_id = _pptx_shape_id(child, seen, f"slide {level}")
            # Validated for every shape in the tree, including the ones whose
            # text is not read: a malformed transform is malformed wherever it
            # is written.
            siblings.append((child, kind, shape_id, _pptx_geometry(child)))
        ranks = _pptx_reading_ranks([geometry for *_, geometry in siblings])
        for here, (child, kind, shape_id, geometry) in enumerate(siblings):
            if kind not in _PPTX_INHERITED_SHAPES:
                continue
            where: dict[str, object] = {
                "part": part,
                "relationshipId": relation_id,
                "inheritedFrom": level,
                "slidePart": self._part,
                "slideId": self._slide_id,
                "slide": self._ordinal,
                "slideHidden": self._hidden,
                "shapeId": shape_id,
                "shapeKind": kind,
                "zOrder": here,
                "readingOrder": ranks[here],
                "readingPath": [ranks[here]],
                "geometry": geometry,
            }
            placeholder = _pptx_placeholder(child)
            if placeholder is not None:
                prompts.append(
                    _PptxPrompt(
                        _pptx_placeholder_slot(placeholder),
                        where,
                        child,
                        _pptx_holds_text(child),
                    )
                )
                continue
            self._emit(child, "body", where, suppress=True)
        return prompts

    def _where(
        self,
        shape: ElementTree.Element,
        kind: str,
        shape_id: str,
        path: tuple[str, ...],
        z_path: tuple[int, ...],
        reading_path: tuple[int, ...],
        geometry: dict[str, int] | None,
    ) -> dict[str, object]:
        """Return the slide and shape identity every segment of a shape carries.

        Source order and the reading-order heuristic are recorded side by side
        and neither stands in for the other: ``zOrder`` and ``zPath`` are the
        z-order the tree declared, ``readingOrder`` and ``readingPath`` the rank
        ``_pptx_reading_ranks`` gave the shape among its own siblings and at
        every level above it. Every segment one shape emits -- its runs, its
        table's cells, its chart's or diagram's records, its picture, media or
        object inventory -- carries this one identity, so all of them share a
        shape's rank just as they share its z-order.
        """
        placeholder = _pptx_placeholder(shape)
        placeholder_type, placeholder_index = placeholder or (None, None)
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
            "readingOrder": reading_path[-1],
            "readingPath": list(reading_path),
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

    def _chart(self, relation_id: str, where: dict[str, object]) -> None:
        """Emit one chart's stored title and stored series records.

        Every record carries the chart part and the relationship id that
        reached it, the slide, shape and group path of the frame showing it,
        and ``freshness`` of ``unknown``: the values are the copy the package
        stored, and nothing here opens the workbook behind them or recalculates
        anything, so how current they are is not knowable from the package.
        """
        part = _referenced_part(
            self._package,
            self._part,
            relation_id,
            _CHART_RELATIONSHIPS,
            _CHART_CONTENT_TYPE,
            "chart",
        )
        for place, value in _chart_records(self._package, part, "PPTX"):
            self._segments.append(
                ExtractedSegment(
                    "chart",
                    _locator(
                        self._capture_id,
                        **where,
                        chartPart=part,
                        relationshipId=relation_id,
                        **place,
                        freshness="unknown",
                    ),
                    # A cache that stored an empty value is still a record the
                    # chart claims, so it is reported as the empty claim it is;
                    # ``document_intake`` takes exactly one content
                    # representation and refuses empty text.
                    text=value or None,
                    structured_value=None if value else {"value": ""},
                    visibility="hidden" if self._hidden else "visible",
                )
            )
            _bounded(self._segments)

    def _diagram(self, relation_id: str, where: dict[str, object]) -> None:
        """Emit one SmartArt data model's stored text and encoded connections.

        Every segment carries the data model part and the relationship id that
        reached it, plus the slide, shape and group path of the frame showing
        it. Point text is body text, reported run by run at the point, paragraph
        and run that hold it; a connection is an inert inventory record of the
        identity it encodes. Nothing here opens the layout, quick-style, colour
        or drawing parts the diagram relates to, so no visual hierarchy, label
        or reading order is read off how the diagram would be drawn.
        """
        part = _referenced_part(
            self._package,
            self._part,
            relation_id,
            _DIAGRAM_RELATIONSHIPS,
            _DIAGRAM_CONTENT_TYPE,
            "diagram data",
        )
        points, connections = _diagram_records(self._package, part, "PPTX")
        visibility = "hidden" if self._hidden else "visible"
        provenance: dict[str, object] = {
            **where,
            "diagramPart": part,
            "relationshipId": relation_id,
        }
        for place, text in points:
            self._segments.append(
                ExtractedSegment(
                    "body",
                    _locator(self._capture_id, **provenance, **place),
                    text=text,
                    visibility=visibility,
                )
            )
            _bounded(self._segments)
        for connection in connections:
            self._segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(
                        self._capture_id,
                        **provenance,
                        diagramRole="connection",
                        modelId=connection["modelId"],
                        connectionIndex=connection["connectionIndex"],
                    ),
                    structured_value=connection,
                    extraction_method="inventory",
                    visibility=visibility,
                )
            )
            _bounded(self._segments)

    def _object(self, record: dict[str, object], where: dict[str, object]) -> None:
        """Inventory one embedded or linked object, without ever opening it.

        One record, at the frame's own place: the slide, the frame's identifier,
        the groups above it, its index at every level and the transform it
        encodes. Its ``kind`` is ``oleObject``, which is what tells it from the
        picture and diagram records already inventoried under the same role.

        What the record holds is the frame's own declaration and what the
        package already knows about the part or target it names. Nothing is
        parsed out of the object, no server is resolved or launched for its
        ``progId``, no linked target is fetched or probed, and the preview image
        is reported as declared rather than opened -- so an object never
        contributes text and nothing about it is read by executing it.
        """
        self._segments.append(
            ExtractedSegment(
                "object_inventory",
                _locator(self._capture_id, **where),
                structured_value={"kind": "oleObject", **record},
                extraction_method="inventory",
                visibility="hidden" if self._hidden else "visible",
            )
        )
        _bounded(self._segments)

    def _picture(self, pic: ElementTree.Element, where: dict[str, object]) -> None:
        """Inventory one picture, then report the alt text its author wrote.

        The inventory record comes first and is emitted for every picture,
        whether or not its author claimed anything, so a slide's pictures are
        inventoried in the z-order the tree declared them in. Where the picture
        sits is the ``where`` every one of this slide's shapes carries -- the
        slide, the shape's own identifier, the groups above it, its index at
        every level and the transform it encodes -- and the value's own ``kind``
        is ``picture``, which is what tells one of these records from the
        diagram connections already inventoried under the same role.

        The record also inventories the image the picture names -- the embedded
        part's relationship, name, declared type and validated byte length, and
        separately the target, scheme and external flag of a linked one. That is
        inventory about an image, not anything read out of one: nothing opens,
        decodes, renders, OCRs or fetches it, and no image ever contributes
        text.

        A picture that references audio or video gets one further record at the
        same place, told from the picture's own by its ``kind`` and from the
        other by the ``mediaRole`` its locator carries. It is inventory on the
        same terms: the relationship it names, the part or external target that
        resolves to, and what the package already knows about that part. No
        media is ever fetched, played, decoded, launched or inspected, and
        media never contributes text either.

        The claims then follow in one fixed order -- title, then description --
        each under ``image_alt`` with the claim that supplied it in ``altRole``,
        so two claims on one picture stay distinguishable and a reader can tell
        which attribute the words came from. They are the author's own words
        read straight out of the picture's non-visual properties, so they are
        reported by the method that found them, ``direct``, and never as
        anything read off an image: nothing here opens, decodes or resolves an
        image, there is no OCR, and no text is invented for a picture whose
        author described it with none.

        A claim written empty is reported by the record alone, which keeps it as
        the empty claim the file made rather than as text. Nothing is suppressed
        as a duplicate: two pictures claiming the same words are two objects the
        author placed and each is reported where it sits.

        The record's own copy of each claim is clipped, because it rides in a
        bounded JSON value beside the keys that locate the picture; the alt text
        is reported whole, as the run text of a shape is, because it is the
        content the author wrote rather than metadata about it.
        """
        visibility = "hidden" if self._hidden else "visible"
        claims = _pptx_picture_claims(pic)
        media = _pptx_picture_media(self._package, self._part, pic)
        self._segments.append(
            ExtractedSegment(
                "object_inventory",
                _locator(self._capture_id, **where),
                structured_value={
                    "kind": "picture",
                    **{
                        name: None if value is None else _clip(value, _MAX_META_CHARS)
                        for name, value in claims.items()
                    },
                    **_pptx_picture_image(self._package, self._part, pic),
                },
                extraction_method="inventory",
                visibility=visibility,
            )
        )
        _bounded(self._segments)
        if media is not None:
            kind, reference = media
            self._segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(self._capture_id, **where, mediaRole=kind),
                    structured_value={
                        "kind": "pictureMedia",
                        "media": kind,
                        **reference,
                    },
                    extraction_method="inventory",
                    visibility=visibility,
                )
            )
            _bounded(self._segments)
        for name, role in _ALT_CLAIMS:
            text = claims[name]
            if not text:
                continue
            self._segments.append(
                ExtractedSegment(
                    "image_alt",
                    _locator(self._capture_id, **where, altRole=role),
                    text=text,
                    visibility=visibility,
                )
            )
            _bounded(self._segments)

    def _visual(
        self, role: str, text: str, geometry: object
    ) -> tuple[str, str, tuple[tuple[str, int], ...] | None]:
        """Return what one run puts on the slide, as a comparable identity.

        The whole of it, never the text alone: the role the run is reported
        under, its text once whitespace is normalised, and the transform the
        shape holding it encodes. ``geometry`` is the locator's own
        ``geometry`` value -- which both the slide's walk and the inherited
        pass record -- taken exactly as the shape encoded it, in EMU, with
        nothing resolved or rounded; it is sorted into a tuple only because a
        dict's order follows the authored attribute order and says nothing
        about where the shape sits. A shape that encodes no transform carries
        no coordinates, so two such shapes are compared on role and text.

        The same words at two different transforms are therefore two authored
        things, and only the same words at the same transform are one thing
        seen twice.
        """
        coordinates = (
            tuple(sorted(geometry.items())) if isinstance(geometry, dict) else None
        )
        return role, " ".join(text.split()), coordinates

    def _emit(
        self,
        holder: ElementTree.Element,
        role: str,
        where: dict[str, object],
        *,
        suppress: bool = False,
    ) -> bool:
        """Emit one segment per non-empty run of ``holder``'s own paragraphs.

        Every run emitted records its visual identity on the walker, so a later
        pass over the same slide can tell what is already shown. ``suppress``
        then drops a run whose identity is one of those -- the inherited pass
        asks for that, the slide's own pass does not, so a slide's own text is
        always reported. Indexes are unaffected, so a suppressed run leaves the
        ones around it where they are.

        Returns whether anything was emitted. Nothing was when the shape wrote
        no text of its own, which on the slide's own pass -- where nothing is
        suppressed -- is what makes a placeholder an empty slot.
        """
        emitted = False
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
                identity = self._visual(role, text, where.get("geometry"))
                if suppress and identity in self._shown:
                    continue
                self._shown.add(identity)
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
                emitted = True
                _bounded(self._segments)
        return emitted


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


def _pptx_reading_ranks(geometries: list[dict[str, int] | None]) -> list[int]:
    """Rank one shape-tree level into a reading order, top edge then left edge.

    The tree's own order is z-order, back to front, which is not a reading
    order: a shape drawn last can be the first one read. This is the documented
    heuristic for the second one -- top edge first, then left edge -- recorded
    beside the source order rather than in place of it, and never used to decide
    what order segments are emitted in.

    One level at a time, which is the only space in which the shapes being
    compared share a coordinate system. A group is ranked among its siblings as
    the one positioned block it is, and its members are ranked among themselves
    inside that block, so a member's rank is read as the path of ranks down to
    it. No child-space mapping is composed and no absolute slide position is
    derived for a nested shape, because ``_pptx_geometry`` deliberately resolves
    neither: a coordinate the file does not hold is not invented to sort by.

    The order is total and stable, with source position as the documented
    fallback. A shape is ranked on the top edge its own transform encodes, then
    on the left edge; two shapes at the same point keep their source order, and
    a shape whose transform encodes no offset has no position to read at all
    and keeps its source order after every positioned sibling. ``off`` is read
    as a pair, so a transform either places a shape or does not -- an extent
    alone is a size without a position.
    """

    def key(position: int) -> tuple[int, int, int, int]:
        geometry = geometries[position]
        if geometry is None or "y" not in geometry:
            return (1, 0, 0, position)
        return (0, geometry["y"], geometry["x"], position)

    ranks = [0] * len(geometries)
    for rank, position in enumerate(sorted(range(len(geometries)), key=key)):
        ranks[position] = rank
    return ranks


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
) -> tuple[str, str | None] | None:
    """Return the (type, index) one shape's placeholder reference names.

    ``None`` when the shape holds no reference at all. The index is returned
    exactly as written, because a locator records the value the file gave and
    never a renumbering of it; applicability compares it as the number it is.
    An unstated type is ST_PlaceholderType's own default of ``body``, and both
    halves are checked against their types here -- the one place the reference
    is read -- so a slot named by a value neither the slide nor an inherited
    part could mean is refused rather than resolved against.
    """
    for holder in shape:
        if not _local(holder.tag).startswith("nv"):
            continue
        for properties in holder:
            if _local(properties.tag) != "nvPr":
                continue
            for node in properties:
                if _local(node.tag) != "ph":
                    continue
                kind = node.attrib.get("type", "body")
                if kind not in _PPTX_PLACEHOLDER_TYPES:
                    raise OOXMLInvalid("PPTX placeholder type is malformed")
                index = node.attrib.get("idx")
                if index is not None and (
                    _INDEX.fullmatch(index) is None or int(index) > _MAX_INDEX
                ):
                    raise OOXMLInvalid("PPTX placeholder index is malformed")
                return kind, index
    return None


def _pptx_placeholder_slot(
    placeholder: tuple[str, str | None],
) -> tuple[str, int]:
    """Return the slot one placeholder reference names, as it is matched on.

    The index is the number it stands for, so the same slot is the same slot
    however it is spelled, and an absent one is ST_Index's own default.
    """
    kind, index = placeholder
    return kind, _PPTX_DEFAULT_PLACEHOLDER_INDEX if index is None else int(index)


def _pptx_applicable(
    slot: tuple[str, int], prompts: list[_PptxPrompt], level: str
) -> _PptxPrompt | None:
    """Return the one placeholder on an inherited part a slide's own applies to.

    OOXML links a slide's placeholder to the one behind it by ``idx``: the index
    *is* the slot, so a stated one identifies the placeholder on its own and the
    type the two ends give that slot need not agree. Only at the default index
    -- where every placeholder a part writes without an ``idx`` sits, a title
    among them -- is the type what tells one from another, and there the two
    ends have to name the same one. A bare ``p:ph`` is therefore the body at the
    default index, which is what each of the two attributes defaults to.

    Nothing applies when no placeholder on the part matches, and more than one
    leaves which one is meant ambiguous, so the part is refused rather than read
    from whichever its tree happens to write first.
    """
    kind, index = slot
    matched = [
        prompt
        for prompt in prompts
        if prompt.slot[1] == index
        and (index != _PPTX_DEFAULT_PLACEHOLDER_INDEX or prompt.slot[0] == kind)
    ]
    if len(matched) > 1:
        raise OOXMLInvalid(
            f"PPTX slide {level} holds more than one applicable placeholder"
        )
    return matched[0] if matched else None


def _pptx_holds_text(shape: ElementTree.Element) -> bool:
    """Whether one shape saved any text of its own.

    A shape whose paragraphs are all empty saved none: an empty run is a run
    the file wrote, not text it holds.
    """
    return any(
        text for paragraph in _pptx_paragraphs(shape) for text in _pptx_runs(paragraph)
    )


def _pptx_picture_claims(pic: ElementTree.Element) -> dict[str, str | None]:
    """Return the three ``cNvPr`` claims one picture's author made about it.

    Every field is present whatever the picture wrote, so one picture's record
    is shaped like every other's, and each claim is kept apart from the other
    two: a claim nothing wrote is ``None``, and a claim written empty is the
    empty string, because a file that declared a description and left it blank
    said something a file that declared none did not.

    Values are the author's own, read as written: nothing is trimmed,
    normalised, recased or substituted, and no claim is invented from another
    one or from the image the picture shows -- which is never opened here.
    """
    properties = _pptx_non_visual(pic)
    claims = {} if properties is None else properties.attrib
    return {field: claims.get(attribute) for attribute, field in _PICTURE_CLAIMS}


def _pptx_picture_image(
    package: _Package, part: str, pic: ElementTree.Element
) -> dict[str, object]:
    """Inventory the image one picture names, without ever reaching its bytes.

    The route in is the picture's own direct ``blipFill`` child and that fill's
    own direct ``a:blip`` child, qualified in a DrawingML main namespace. That
    is the one place a picture declares the image it displays, so a blip
    reached anywhere else below the picture -- in its shape properties, in a
    fill on some other element, in extension metadata -- is not the displayed
    image and is passed over. A picture shows one image, so two direct fills,
    or two direct blips in one fill, leave which one it shows ambiguous -- a
    fill written beside the picture's own is a second declaration, not a
    qualifier on the first -- and nothing here chooses between them.

    A blip declares its image on two attributes, each read in the relationships
    namespaces OOXML defines for it: ``r:embed`` names a part inside the
    package, ``r:link`` names a reference out of it. Both may be written, and
    each is recorded in its own fields so an embedded part is never read as a
    link or the reverse. One attribute written in both namespaces at once names
    two relationships, which is refused for the reason two blips are.

    The ids resolve only through the holding part's own relationships, so
    containment, path safety and existence were all settled when the package
    validated them. Nothing here opens, decodes, renders, OCRs or fetches
    anything: an embedded part contributes its name, its declared type and the
    byte length the package already measured, and a linked target is carried
    exactly as written.
    """
    fills = [node for node in pic if _local(node.tag) == "blipFill"]
    if len(fills) > 1:
        raise OOXMLInvalid("PPTX picture declares more than one image reference")
    blips = [node for node in fills[0] if node.tag in _DRAWING_BLIP] if fills else []
    if not blips:
        return dict(_DRAWING_IMAGE)
    if len(blips) > 1:
        raise OOXMLInvalid("PPTX picture declares more than one image reference")
    rels = posixpath.join(
        posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
    )
    relations = package.relationships.get(rels, {})
    types = package.relationship_types.get(rels, {})
    embedded = _blip_id(blips[0], "embed", "PPTX picture")
    linked = _blip_id(blips[0], "link", "PPTX picture")
    record = dict(_DRAWING_IMAGE)
    if embedded is not None:
        record.update(
            _embedded_image(package, relations, types, embedded, "PPTX picture")
        )
    if linked is not None:
        record.update(_linked_image(relations, types, linked, "PPTX picture"))
    return record


def _blip_id(blip: ElementTree.Element, attribute: str, what: str) -> str | None:
    """Return the one relationship id a blip names on ``attribute``, if any.

    Two ids -- the same attribute written in the Transitional and the Strict
    relationships namespace at once -- name two relationships, so which image
    the object declares is ambiguous and nothing here chooses.
    """
    ids = _namespaced_ids(blip, attribute)
    if len(ids) > 1:
        raise OOXMLInvalid(f"{what} names more than one image relationship")
    return ids[0] if ids else None


def _image_relation(
    relations: dict[str, tuple[str, bool]],
    types: dict[str, str],
    relation_id: str,
    what: str,
) -> tuple[str, bool, str]:
    """Return the (target, external, type) one blip's relationship id resolves to.

    An id the holding part relates to nothing names no image at all, and a
    relationship carrying another type names something that is not an image, so
    neither is read as one.
    """
    entry = relations.get(relation_id)
    if entry is None:
        raise OOXMLInvalid(f"{what} names a missing image relationship")
    kind = types.get(relation_id, "")
    if not _is_defined_relationship(kind, _IMAGE_RELATIONSHIPS):
        raise OOXMLInvalid(f"{what} names an image relationship of another type")
    target, external = entry
    return target, external, kind


def _embedded_image(
    package: _Package,
    relations: dict[str, tuple[str, bool]],
    types: dict[str, str],
    relation_id: str,
    what: str,
) -> dict[str, object]:
    """Describe the image part a drawing object embeds, inertly.

    ``r:embed`` names a part of the package, so an external relationship is not
    an embed at all and is refused rather than quietly recorded as a link. What
    it resolves to has to be a part the package holds and declares -- by
    ``Override`` or by the ``Default`` its extension carries -- as an image.

    The byte length is the one the package already validated: every member was
    streamed to its end against its directory entry and the profile's bounds
    before any part was handed out, so the figure is a measurement rather than
    the directory's claim, and reporting it opens nothing.
    """
    target, external, kind = _image_relation(relations, types, relation_id, what)
    if external:
        raise OOXMLInvalid(f"{what} embedded image relationship is external")
    if target not in package.names:
        raise OOXMLInvalid(f"{what} embedded image relationship names a missing part")
    content_type = package.resolved_type(target)
    if not content_type.startswith(_IMAGE_CONTENT_PREFIX):
        raise OOXMLInvalid(f"{what} embedded image part is not declared an image")
    return {
        "imageEmbedRelationshipId": _clip(relation_id, _MAX_META_CHARS),
        "imageEmbedRelationshipType": _clip(kind, _MAX_TARGET_CHARS),
        "imagePart": _clip(target, _MAX_TARGET_CHARS),
        "imageContentType": _clip(content_type, _MAX_META_CHARS),
        "imageBytes": package.archive.getinfo(target).file_size,
    }


def _linked_image(
    relations: dict[str, tuple[str, bool]],
    types: dict[str, str],
    relation_id: str,
    what: str,
) -> dict[str, object]:
    """Describe the image a drawing object links to, without resolving it.

    The target is recorded verbatim beside the scheme its own text names, and
    whether the package declared the relationship external is recorded as the
    package declared it. Nothing opens, fetches, normalises or probes it, so a
    link is inventory and never a source of content: no local path is inspected
    and no reference is followed.
    """
    target, external, kind = _image_relation(relations, types, relation_id, what)
    match = _SCHEME.match(target)
    return {
        "imageLinkRelationshipId": _clip(relation_id, _MAX_META_CHARS),
        "imageLinkRelationshipType": _clip(kind, _MAX_TARGET_CHARS),
        "imageLinkTarget": _clip(target, _MAX_TARGET_CHARS),
        "imageLinkScheme": match.group(1).casefold() if match else None,
        "imageLinkExternal": external,
    }


def _pptx_picture_media(
    package: _Package, part: str, pic: ElementTree.Element
) -> tuple[str, dict[str, object]] | None:
    """Inventory the audio or video one picture references, inertly.

    The route in is the schema's own: the picture's direct ``p:nvPicPr``, that
    holder's direct ``p:nvPr``, and a direct DrawingML ``a:audioFile`` or
    ``a:videoFile`` below it. A lookalike anywhere else -- nested deeper, hung off a group or a
    neighbouring shape, or written in a vocabulary that merely shares the local
    name -- declares nothing about this picture and is passed over, so a
    picture that references no media is reported as referencing none rather
    than as referencing something found elsewhere.

    ``EG_Media`` is a choice, so a picture references one piece of media or
    none. Two declarations -- two of a kind, one of each, or the same element
    written in two of the namespaces that qualify it -- leave which one the
    picture references ambiguous, and nothing here chooses between them.

    The reference itself is a single ``r:link``, read in the relationships
    namespaces OOXML defines for it. The attribute is named ``link`` whether
    the relationship turns out to be internal or external, which is the
    schema's own spelling; an ``r:embed`` written beside it contradicts that
    declaration rather than qualifying it, and is refused instead of resolved.

    The id resolves only through the holding part's own relationships, so
    containment, path safety and existence were all settled when the package
    validated them -- which is why an internal target is a member the archive
    holds and nothing re-checks it here -- and the relationship has to carry
    the type the element's own kind names. Nothing here fetches, plays,
    decodes, launches or inspects media: an internal part contributes its safe
    part name, the type the package declares for it and the already-validated
    byte length it already measured, and an external target is
    carried exactly as the relationship holds it, beside the scheme its own
    text names.
    """
    declarations = [
        (_PPTX_MEDIA_ELEMENTS[node.tag], node)
        for holder in pic
        if holder.tag in _PPTX_PICTURE_NON_VISUAL
        for properties in holder
        if properties.tag in _PPTX_APPLICATION_PROPERTIES
        for node in properties
        if node.tag in _PPTX_MEDIA_ELEMENTS
    ]
    if not declarations:
        return None
    if len(declarations) > 1:
        raise OOXMLInvalid("PPTX picture declares more than one media reference")
    role, node = declarations[0]
    if _namespaced_ids(node, "embed"):
        raise OOXMLInvalid("PPTX picture media reference also names an embedded part")
    ids = _namespaced_ids(node, "link")
    if len(ids) > 1:
        raise OOXMLInvalid("PPTX picture names more than one media relationship")
    if not ids:
        raise OOXMLInvalid("PPTX picture media reference names no relationship")
    rels = posixpath.join(
        posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
    )
    entry = package.relationships.get(rels, {}).get(ids[0])
    if entry is None:
        raise OOXMLInvalid("PPTX picture names a missing media relationship")
    kind = package.relationship_types.get(rels, {}).get(ids[0], "")
    if not _is_defined_relationship(kind, _PPTX_MEDIA_RELATIONSHIPS[role]):
        raise OOXMLInvalid("PPTX picture names a media relationship of another type")
    target, external = entry
    record = dict(_PPTX_PICTURE_MEDIA)
    record.update(
        {
            "mediaRelationshipId": _clip(ids[0], _MAX_META_CHARS),
            "mediaRelationshipType": _clip(kind, _MAX_TARGET_CHARS),
            "mediaTarget": _clip(target, _MAX_TARGET_CHARS),
            "mediaExternal": external,
        }
    )
    if external:
        match = _SCHEME.match(target)
        record["mediaScheme"] = match.group(1).casefold() if match else None
        return role, record
    record["mediaPart"] = _clip(target, _MAX_TARGET_CHARS)
    record["mediaContentType"] = _clip(package.resolved_type(target), _MAX_META_CHARS)
    record["mediaBytes"] = package.archive.getinfo(target).file_size
    return role, record


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


@dataclass(slots=True)
class _FieldState:
    """One complex field held open by its ``fldChar begin``.

    ``instruction`` is the instruction so far in source order, never longer
    than ``_MAX_TARGET_CHARS`` so one field cannot hold an arbitrarily large
    string, ``truncated`` is whether any source character was dropped to keep
    it there, ``record`` is the field's metadata once ``separate`` has ordered
    it, and ``result`` is whether any text was stored under that separator.
    """

    instruction: str = ""
    truncated: bool = False
    result: bool = False
    record: dict[str, object] | None = None

    def extend(self, text: str) -> None:
        """Keep what still fits of one fragment, remembering what did not."""
        room = _MAX_TARGET_CHARS - len(self.instruction)
        self.instruction += text[:room]
        self.truncated = self.truncated or len(text) > room


class _DocxWalker:
    """Walks one story's paragraphs, tables and display math in XML document order.

    A position is the block index inside the story (or note), extended with
    row, cell and in-cell block indices for tables, so it depends only on the
    structure of the part. Display math standing between blocks is one such
    block: it takes a position of its own and splits the cell prose around it,
    and the ``oMath`` children it holds are part of that one ``equation``
    segment rather than equations beside it. Inside a paragraph, items come out
    in source order:
    contiguous current text (the first stretch at the paragraph position, later
    ones at ``<paragraph>.t<n>``), deleted text (``<paragraph>.d<n>``), text
    hidden by a run's own properties (``<paragraph>.v<n>``, a
    ``hidden_content`` segment), inline equations (``<paragraph>.e<n>``, an
    ``equation`` segment), DrawingML images (``<paragraph>.i<n>``, an
    ``object_inventory`` segment and the ``image_alt`` claims after it),
    DrawingML charts (``<paragraph>.c<n>``, an ``object_inventory`` segment and
    the ``chart`` records the chart part stored after it), SmartArt
    (``<paragraph>.d<n>``, an ``object_inventory`` segment, then the ``body``
    point text the data model stored and an ``object_inventory`` segment per
    encoded connection) and
    text boxes (``<paragraph>.x<n>.<block>``)
    where they are anchored, and current text is never joined across one of
    them. A table cell reads its current paragraphs as one ``table_cell`` run,
    closed by any deletion, hidden stretch, equation, image, chart, diagram,
    text box, field or nested table, and later runs sit at ``<cell>.t<n>``.

    Hyperlinks, fields and annotation references stay out of the text: each
    becomes an inert ``object_inventory`` segment at ``<paragraph>.h<n>``
    (hyperlinks), ``<paragraph>.n<n>`` (note and comment references) or
    ``<paragraph>.f<n>`` (fields of either form, numbered together in source
    order), and range markers between blocks at ``<level>m<n>``. A link or
    reference follows the stretch it sits in; a field is inventoried first and
    the result stored under it then reads as the ordinary text it is. A complex
    field's boundaries belong to the story, not to a paragraph, so one spans the
    paragraphs between them; ``finish`` refuses one the story leaves open, and a
    text box is walked as its own story so no boundary crosses into it. A
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
        self._package = package
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
        # The complex fields open in this story. A field may span paragraphs, so
        # the stack belongs to the story and not to any one paragraph.
        self._fields: list[_FieldState] = []

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
            elif block.tag in _MATH_CONTENT:
                self._math(block, position, removed, None)
            else:
                self._table(block, position, depth, removed)
            index += 1

    def finish(self) -> None:
        """Close this story, refusing a complex field its walk left open.

        A field spans the paragraphs between its own boundaries, so nothing is
        decided at a paragraph's end; the story's own end is where an unclosed
        field becomes one, and a field that leaves its story is refused here.
        """
        if self._fields:
            raise OOXMLInvalid("DOCX field is never closed")

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
        """Inventory one hyperlink, field or annotation marker, refusing a bad one."""
        tag = _local(element.tag)
        if tag == "hyperlink":
            value, visibility = self._hyperlink(element), "visible"
        elif tag == "fldSimple":
            value, visibility = _docx_simple_field(element), "visible"
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

    def _locator(self, position: str, **extra: object) -> dict[str, object]:
        values: dict[str, object] = {
            "part": self._part,
            "story": self._story,
            "position": position,
        }
        if self._note_id is not None:
            values["noteId"] = self._note_id
        return _locator(self._capture_id, **values, **extra)

    def _add(
        self, role: str, position: str, text: str, deleted: bool, hidden: bool = False
    ) -> None:
        if hidden:
            # Hidden text keeps its own role and visibility in every story, so a
            # hidden note or cell is still hidden and never ordinary prose. A
            # deletion over it is still recorded as the revision it is.
            semantic, visibility = "hidden_content", "hidden"
        elif deleted:
            semantic, visibility = "deleted_text", "annotation"
        else:
            semantic = role
            visibility = "annotation" if role in {"note", "comment"} else "visible"
        self._segments.append(
            ExtractedSegment(
                semantic,
                self._locator(position),
                text=text,
                visibility=visibility,
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
        current = removed = boxes = links = refs = fields = concealed = 0
        maths = images = charts = diagrams = objects = legacy = 0
        for kind, value, box_deleted in _paragraph_items(
            paragraph, deleted, self._fields
        ):
            if kind == "e":
                assert isinstance(value, ElementTree.Element)
                # Math is ordered where it sits, so in a cell it closes the
                # current run rather than trailing the prose it interrupts.
                self._flush(cell)
                self._equation(value, f"{position}.e{maths}", box_deleted)
                maths += 1
            elif kind == "g":
                assert isinstance(value, ElementTree.Element)
                # An image is ordered where it sits, for the reason math is.
                self._flush(cell)
                self._drawing(value, f"{position}.i{images}", box_deleted)
                images += 1
            elif kind == "k":
                assert isinstance(value, ElementTree.Element)
                # A chart is ordered where it sits, for the reason an image is.
                self._flush(cell)
                self._chart(value, f"{position}.c{charts}", box_deleted)
                charts += 1
            elif kind == "s":
                assert isinstance(value, ElementTree.Element)
                # SmartArt is ordered where it sits, for the reason a chart is.
                self._flush(cell)
                self._diagram(value, f"{position}.d{diagrams}", box_deleted)
                diagrams += 1
            elif kind == "o":
                assert isinstance(value, ElementTree.Element)
                # An object is ordered where it sits, for the reason SmartArt is.
                self._flush(cell)
                self._object(value, f"{position}.o{objects}", box_deleted)
                objects += 1
            elif kind == "p":
                assert isinstance(value, ElementTree.Element)
                # A legacy image is ordered where it sits, for the reason an
                # object is.
                self._flush(cell)
                self._vml_image(value, f"{position}.v{legacy}", box_deleted)
                legacy += 1
            elif kind == "m":
                assert isinstance(value, ElementTree.Element)
                item = _local(value.tag)
                if item == "hyperlink":
                    label, links = f"h{links}", links + 1
                elif item == "fldSimple":
                    label, fields = f"f{fields}", fields + 1
                    # A field is inventoried where it sits, so in a cell it
                    # closes the current run rather than waiting for the end of
                    # the cell, where it would trail the result it precedes.
                    self._flush(cell)
                    self._mark(value, f"{position}.{label}", box_deleted, None)
                    continue
                else:
                    label, refs = f"n{refs}", refs + 1
                self._mark(value, f"{position}.{label}", box_deleted, cell)
            elif isinstance(value, dict):
                label, fields = f"f{fields}", fields + 1
                # A complex field is ordered at its own boundary, so in a cell it
                # closes the current run for the same reason a simple field does.
                self._flush(cell)
                self._inventory(f"{position}.{label}", value, "visible", box_deleted)
            elif isinstance(value, ElementTree.Element):
                self._flush(cell)
                if depth >= _MAX_DOCX_DEPTH:
                    raise OOXMLInvalid("DOCX text boxes are nested too deeply")
                # A text box is traversed as its own story, so its fields neither
                # close nor are closed by the ones open around it: a boundary
                # that tries to cross the box is refused, not leaked through.
                outer, self._fields = self._fields, []
                self.walk(value, f"{position}.x{boxes}.", depth + 1, box_deleted)
                self.finish()
                self._fields = outer
                boxes += 1
            elif kind == "v":
                # A hidden stretch closes the cell's current text, exactly as a
                # deletion does, so hidden text never joins the cell's prose.
                self._flush(cell)
                self._add(
                    role,
                    f"{position}.v{concealed}",
                    value,
                    box_deleted,
                    hidden=True,
                )
                concealed += 1
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

    def _math(
        self,
        block: ElementTree.Element,
        position: str,
        deleted: bool,
        cell: _CellRun | None,
    ) -> None:
        """Record math that stands between blocks rather than inside a paragraph.

        It is one block of its own, so it takes the block position a paragraph
        would have had there, and in a cell it closes the current run exactly as
        an inline equation does rather than trailing the prose it follows. The
        fields open around it count it as their stored result the same way too,
        because a block is no less that result than a run is.
        """
        _field_result(self._fields, "math")
        self._flush(cell)
        self._equation(block, position, deleted)

    def _equation(
        self, element: ElementTree.Element, position: str, deleted: bool
    ) -> None:
        """Record one equation as the markup it is, unread.

        The math is neither interpreted nor converted, so it is kept as its own
        segment and its text never joins the prose it interrupts. A deletion
        wrapped around it is kept as the revision it is rather than dropped.
        """
        self._segments.append(
            ExtractedSegment(
                "equation",
                self._locator(position),
                structured_value=_docx_equation(element),
                visibility="annotation" if deleted else "visible",
                revision_state="deleted" if deleted else "current",
            )
        )
        _bounded(self._segments)

    def _drawing(
        self, drawing: ElementTree.Element, position: str, deleted: bool
    ) -> None:
        """Inventory one DrawingML image, then report the alt text its author wrote.

        The inventory record comes first and is emitted for every image the
        story anchors, whether or not its author claimed anything, so a story's
        images are inventoried where the XML placed them. It names where the
        object sits -- ``inline`` in the text flow or ``anchor`` against the
        page -- the claims ``wp:docPr`` carries, and the image the blip names:
        the embedded part's relationship, name, declared type and validated
        byte length, and separately the relationship, target, scheme and
        external flag of a linked one. That is inventory about an image, not
        anything read out of one, and the record says so outright: no byte was
        decoded and no OCR was run. An image never contributes text.

        The claims then follow in one fixed order -- title, then description --
        each under ``image_alt`` with the claim that supplied it in ``altRole``,
        so two claims on one image stay distinguishable and a reader can tell
        which attribute the words came from. ``name`` is not among them: an
        autogenerated object name describes nothing, so it is reported by the
        record alone and never as text the author wrote. A claim written empty
        is reported by the record alone for the same reason -- it is the empty
        claim the file made, not text -- and no text is invented for an image
        whose author described it with none.

        A deletion wrapped around the drawing is kept as the revision it is,
        over the record and over every claim it carried. Text a run's own
        properties hide is not: ``vanish`` on the run holding a drawing is
        deliberately not resolved here, for the reason a style-hidden run is
        not, so an image inside a hidden run is reported as the visible image
        it is reported as today. That waits for its own slice.

        The record's own copy of each claim is clipped, because it rides in a
        bounded JSON value beside the keys that locate the image; the alt text
        is reported whole, as a paragraph's text is, because it is the content
        the author wrote rather than metadata about it.
        """
        blip = _docx_drawing_blip(drawing)
        assert blip is not None
        claims = _docx_doc_pr(drawing)
        self._inventory(
            position,
            {
                "kind": "drawingImage",
                "placement": _DOCX_DRAWINGS[drawing.tag],
                **{
                    name: None if value is None else _clip(value, _MAX_META_CHARS)
                    for name, value in claims.items()
                },
                **_docx_drawing_image(
                    self._package, self._relations, self._relation_types, blip
                ),
                "imageDecoded": False,
                "ocrPerformed": False,
            },
            "visible",
            deleted,
        )
        for name, role in _ALT_CLAIMS:
            text = claims[name]
            if not text:
                continue
            self._segments.append(
                ExtractedSegment(
                    "image_alt",
                    self._locator(position, altRole=role),
                    text=text,
                    visibility="annotation" if deleted else "visible",
                    revision_state="deleted" if deleted else "current",
                )
            )
            _bounded(self._segments)

    def _chart(
        self, drawing: ElementTree.Element, position: str, deleted: bool
    ) -> None:
        """Inventory one DrawingML chart, then report the values it stored.

        The inventory record comes first and is emitted for every chart the
        story anchors, whether or not the chart part stored a single value, so a
        story's charts are inventoried where the XML placed them. It names
        where the object sits -- ``inline`` in the text flow or ``anchor``
        against the page -- the relationship id it named its chart on, the part
        that relationship reached, and how many records the part had stored. It
        also says outright what was not done: the chart was not refreshed and
        the workbook it may embed was not opened.

        The stored records then follow in source order, each a ``chart``
        segment carrying the chart part, the relationship that reached it, the
        place inside the chart the shared reader reports, and a ``freshness``
        of ``unknown``: the values are the copy the package stored, and nothing
        here opens the workbook behind them, resolves a source formula or
        recalculates anything, so how current they are is not knowable from the
        package. A formula rides beside its value as the inert metadata it is.

        A deletion wrapped around the drawing is kept as the revision it is,
        over the record and over every value it carried, exactly as it is over
        an image and its claims.
        """
        relation_id = _docx_drawing_chart(drawing)
        assert relation_id is not None
        part = _referenced_part(
            self._package,
            self._part,
            relation_id,
            _CHART_RELATIONSHIPS,
            _CHART_CONTENT_TYPE,
            "chart",
            "DOCX",
        )
        records = _chart_records(self._package, part, "DOCX")
        self._inventory(
            position,
            {
                "kind": "drawingChart",
                "placement": _DOCX_DRAWINGS[drawing.tag],
                "relationshipId": _clip(relation_id, _MAX_META_CHARS),
                "chartPart": _clip(part, _MAX_TARGET_CHARS),
                "refreshed": False,
                "embeddedWorkbookOpened": False,
                "storedRecords": len(records),
            },
            "visible",
            deleted,
        )
        for place, value in records:
            self._segments.append(
                ExtractedSegment(
                    "chart",
                    self._locator(
                        position,
                        chartPart=part,
                        relationshipId=relation_id,
                        **place,
                        freshness="unknown",
                    ),
                    # A cache that stored an empty value is still a record the
                    # chart claims, so it is reported as the empty claim it is;
                    # ``document_intake`` takes exactly one content
                    # representation and refuses empty text.
                    text=value or None,
                    structured_value=None if value else {"value": ""},
                    visibility="annotation" if deleted else "visible",
                    revision_state="deleted" if deleted else "current",
                )
            )
            _bounded(self._segments)

    def _diagram(
        self, drawing: ElementTree.Element, position: str, deleted: bool
    ) -> None:
        """Inventory one SmartArt diagram, then report the text it stored.

        The inventory record comes first and is emitted for every diagram the
        story anchors, whether or not the data model stored a single point of
        text, so a story's diagrams are inventoried where the XML placed them.
        It names where the object sits -- ``inline`` in the text flow or
        ``anchor`` against the page -- the relationship id the drawing named its
        data model on, the part that relationship reached, and how many point
        text records and connections the part had stored. It also says outright
        what was not done: no hierarchy was inferred and none of the related
        visual parts was opened.

        The stored point text then follows in source order, each run a ``body``
        segment carrying the data model part, the relationship that reached it
        and the point, paragraph and run the shared reader reports it at. The
        connections follow as inert ``object_inventory`` records of the identity
        each one encodes. Nothing here opens the layout, quick-style, colour or
        drawing parts the ``dgm:relIds`` names beside the data, and no
        connection is walked, so no label, hierarchy or reading order is read
        off how the diagram would be drawn.

        A deletion wrapped around the drawing is kept as the revision it is,
        over the record and over every point and connection it carried, exactly
        as it is over an image and its claims or a chart and its values.
        """
        relation_id = _diagram_reference(drawing, "DOCX", "drawing")
        assert relation_id is not None
        part = _referenced_part(
            self._package,
            self._part,
            relation_id,
            _DIAGRAM_RELATIONSHIPS,
            _DIAGRAM_CONTENT_TYPE,
            "diagram data",
            "DOCX",
        )
        points, connections = _diagram_records(self._package, part, "DOCX")
        self._inventory(
            position,
            {
                "kind": "smartArt",
                "placement": _DOCX_DRAWINGS[drawing.tag],
                "relationshipId": _clip(relation_id, _MAX_META_CHARS),
                "diagramPart": _clip(part, _MAX_TARGET_CHARS),
                "storedPointTextRecords": len(points),
                "storedConnections": len(connections),
                "hierarchyInferred": False,
                "relatedVisualPartsOpened": False,
            },
            "visible",
            deleted,
        )
        provenance: dict[str, object] = {
            "diagramPart": part,
            "relationshipId": relation_id,
        }
        for place, text in points:
            self._segments.append(
                ExtractedSegment(
                    "body",
                    self._locator(position, **provenance, **place),
                    text=text,
                    visibility="annotation" if deleted else "visible",
                    revision_state="deleted" if deleted else "current",
                )
            )
            _bounded(self._segments)
        for connection in connections:
            self._segments.append(
                ExtractedSegment(
                    "object_inventory",
                    self._locator(
                        position,
                        **provenance,
                        diagramRole="connection",
                        modelId=connection["modelId"],
                        connectionIndex=connection["connectionIndex"],
                    ),
                    structured_value=connection,
                    extraction_method="inventory",
                    visibility="annotation" if deleted else "visible",
                    revision_state="deleted" if deleted else "current",
                )
            )
            _bounded(self._segments)

    def _object(
        self, element: ElementTree.Element, position: str, deleted: bool
    ) -> None:
        """Inventory one legacy embedded or linked object, inertly.

        One record is emitted for every object the story anchors, where the XML
        placed it, and an object contributes no text: its payload is never
        opened, parsed, decoded, executed, launched or fetched, whatever the
        ``ProgID`` beside it names.

        Which kind of object it is comes from the declaration's own ``Type`` and
        from nowhere else -- not from the relationship, not from the target's
        name, not from the ``ProgID``. A ``Type`` the schema does not define
        leaves that undeclared, so the object is refused rather than read as
        whichever of the two it resembles.

        The single relationship-namespaced ``r:id`` on the declaration is the one
        reference read; an id written in no namespace, or in two of the
        namespaces that carry it, names no one relationship. It resolves only
        through the holding story part's own relationships, so containment, path
        safety and existence were all settled when the package validated them --
        which is why an embedded target is a member the archive holds and nothing
        re-checks it here -- and it has to carry one of the types OOXML defines
        for an object. The declaration and the relationship then have to agree:
        an embedded object is a part inside the package and a linked one is a
        reference out of it, so an ``Embed`` naming an external target, or a
        ``Link`` naming an internal part, is refused rather than reported as the
        other.

        An embedded object contributes its safe part name, the type the package
        declares for it and the byte length the package already measured while
        validating the member; a linked one is carried exactly as the
        relationship holds it, beside the scheme its own text names. The VML
        shape standing in for the object on the page is recorded as declared and
        no more: it is never opened, decoded, resolved or inventoried as an
        image, because the image a story shows is the one its own ``w:pict``
        names and a stand-in for an object is not that.

        A deletion wrapped around the container is kept as the revision it is,
        exactly as it is over an image, a chart or a diagram.
        """
        declaration = _docx_ole_object(element)
        assert declaration is not None
        mode = _DOCX_OLE_MODES.get(declaration.attrib.get("Type", ""))
        if mode is None:
            raise OOXMLInvalid("DOCX embedded object does not declare embed or link")
        ids = _namespaced_ids(declaration, "id")
        if len(ids) > 1:
            raise OOXMLInvalid("DOCX embedded object names more than one relationship")
        if not ids:
            raise OOXMLInvalid("DOCX embedded object names no relationship")
        entry = self._relations.get(ids[0])
        if entry is None:
            raise OOXMLInvalid("DOCX embedded object names a missing relationship")
        kind = self._relation_types.get(ids[0], "")
        if not _is_defined_relationship(kind, _OLE_RELATIONSHIPS):
            raise OOXMLInvalid(
                "DOCX embedded object names a relationship of another type"
            )
        target, external = entry
        if external != (mode == "link"):
            raise OOXMLInvalid(
                "DOCX embedded object contradicts the relationship it names"
            )
        record = dict(_DOCX_OLE_RECORD)
        record.update(
            {
                "kind": "oleObject",
                "mode": mode,
                "relationshipId": _clip(ids[0], _MAX_META_CHARS),
                "relationshipType": _clip(kind, _MAX_TARGET_CHARS),
                "target": _clip(target, _MAX_TARGET_CHARS),
                "external": external,
                "previewDeclared": any(
                    node.tag == _DOCX_OLE_PREVIEW for node in element
                ),
                **{
                    field: _ole_claim(declaration, name)
                    for name, field in _DOCX_OLE_CLAIMS
                },
            }
        )
        if external:
            match = _SCHEME.match(target)
            record["scheme"] = match.group(1).casefold() if match else None
        else:
            record["objectPart"] = _clip(target, _MAX_TARGET_CHARS)
            record["contentType"] = _clip(
                self._package.resolved_type(target), _MAX_META_CHARS
            )
            record["objectBytes"] = self._package.archive.getinfo(target).file_size
        self._inventory(position, record, "visible", deleted)

    def _vml_image(
        self, element: ElementTree.Element, position: str, deleted: bool
    ) -> None:
        """Inventory one standalone legacy image, then report its author's claims.

        The inventory record comes first and is emitted for every image a
        ``w:pict`` shows, whether or not its author claimed anything, so a
        story's legacy images are inventoried where the XML placed them, exactly
        as its DrawingML ones are. It names where the shape sits, the one
        relationship its ``v:imagedata`` referenced, and what the package already
        knows about what that reached.

        The reference resolves only through the holding story part's own
        relationships, so containment, path safety and existence were all settled
        when the package validated them, and it has to carry the image type OOXML
        defines: an image reference that reaches an OLE object, a media part or a
        hyperlink reaches no image. An internal part contributes its safe name,
        the type the package declares for it -- which has to be an image, because
        inventorying anything else as one would say the package holds an image
        where it holds something else -- and the byte length the package already
        measured while validating the member. An external one is carried exactly
        as the relationship holds it, beside the scheme its own text names.

        That is inventory about an image and nothing read out of one: no byte was
        opened, decoded or rendered, no OCR was run, and an image never
        contributes text. The shape's own markup rides along uninterpreted -- the
        identifier the legacy drawing layer knew it by, resolved against nothing,
        and the style it was drawn with, bounded and unparsed beyond the one
        declaration that says whether it is anchored.

        The author's claims then follow in one fixed order, each under
        ``image_alt`` with the role it played and the element that declared it, so
        the shape's title, the shape's alt text and the ``imagedata``'s Office
        title stay three distinguishable claims rather than one. A claim nothing
        wrote and a claim written empty are both reported by the record alone: no
        text is invented for an image whose author described it with none, least
        of all out of the shape's identifier.

        A deletion wrapped around the container is kept as the revision it is,
        over the record and over every claim it carried, exactly as it is over a
        DrawingML image and its claims.
        """
        admitted = _docx_vml_image(element)
        assert admitted is not None
        shape, data, relation_id = admitted
        target, external, kind = _image_relation(
            self._relations, self._relation_types, relation_id, "DOCX VML image"
        )
        claims = {
            "shapeId": shape.attrib.get("id"),
            "shapeAlt": shape.attrib.get("alt"),
            "shapeTitle": shape.attrib.get("title"),
            "imageTitle": data.attrib.get(_DOCX_VML_TITLE),
        }
        style = shape.attrib.get("style")
        record = dict(_DOCX_VML_RECORD)
        record.update(
            {
                "kind": "vmlImage",
                "placement": _docx_vml_placement(style),
                "relationshipId": _clip(relation_id, _MAX_META_CHARS),
                "relationshipType": _clip(kind, _MAX_TARGET_CHARS),
                "target": _clip(target, _MAX_TARGET_CHARS),
                "external": external,
                "shapeStyle": None if style is None else _clip(style, _MAX_TARGET_CHARS),
                "imageDecoded": False,
                "ocrPerformed": False,
                **{
                    name: None if value is None else _clip(value, _MAX_META_CHARS)
                    for name, value in claims.items()
                },
            }
        )
        if external:
            match = _SCHEME.match(target)
            record["scheme"] = match.group(1).casefold() if match else None
        else:
            content_type = self._package.resolved_type(target)
            if not content_type.startswith(_IMAGE_CONTENT_PREFIX):
                raise OOXMLInvalid("DOCX VML image part is not declared an image")
            record["imagePart"] = _clip(target, _MAX_TARGET_CHARS)
            record["contentType"] = _clip(content_type, _MAX_META_CHARS)
            record["imageBytes"] = self._package.archive.getinfo(target).file_size
        self._inventory(position, record, "visible", deleted)
        for name, role, source in _DOCX_VML_ALT_CLAIMS:
            text = claims[name]
            if not text:
                continue
            self._segments.append(
                ExtractedSegment(
                    "image_alt",
                    self._locator(position, altRole=role, altSource=source),
                    text=text,
                    visibility="annotation" if deleted else "visible",
                    revision_state="deleted" if deleted else "current",
                )
            )
            _bounded(self._segments)

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
                elif block.tag in _MATH_CONTENT:
                    self._math(block, block_position, removed, run)
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
    """Yield paragraphs, tables and display math below ``parent`` in document order.

    Comment range markers between blocks are yielded too. Each comes with
    whether a deletion wrapped it. Transparent wrappers
    (``body``, content controls, revisions) are entered; a yielded paragraph,
    table or equation is not entered, so nothing is seen twice. Math is a block
    of its own where it stands between blocks rather than a wrapper, so its own
    structure is never mistaken for the story's.
    """
    stack = [(iter(parent), deleted, 0)]
    while stack:
        children, state, depth = stack[-1]
        for child in children:
            tag = _local(child.tag)
            if child.tag in _MATH_CONTENT or tag in {"p", "tbl"} or tag in _RANGE_TAGS:
                yield child, state
            elif tag not in _NON_BLOCK_TAGS:
                inner, level = _revised(tag, state, depth)
                stack.append((iter(child), inner, level))
                break
        else:
            stack.pop()


def _paragraph_items(
    paragraph: ElementTree.Element,
    deleted: bool,
    fields: list[_FieldState],
) -> list[tuple[str, str | ElementTree.Element | dict[str, object], bool]]:
    """Split a paragraph into ordered ``(kind, value, deleted)`` items.

    Kinds are ``t`` current text, ``d`` deleted text, ``v`` text a run's own
    properties hide (carrying whether a deletion wrapped it), ``x`` a text box,
    ``e`` an ``m:oMath`` or ``m:oMathPara`` equation, ``g`` a ``wp:inline`` or
    ``wp:anchor`` drawing object that displays an image, ``k`` one that names a
    chart, ``s`` one that names SmartArt, ``o`` a ``w:object`` declaring a
    legacy embedded or linked object, ``p`` a ``w:pict`` showing a standalone
    legacy image,
    ``m`` a hyperlink, simple field or annotation marker, and ``c`` an
    already recorded complex field (each carrying whether a deletion wrapped
    it).
    Adjacent text of one kind is one stretch; tabs and breaks stay separators
    inside it. Hidden text is a stretch of its own, so it is never joined to the
    visible text around it.

    ``fields`` is the story's stack of complex fields a ``fldChar`` has opened,
    carried in and left as this paragraph leaves it, because a field spans the
    paragraphs between its own boundaries. Text inside the innermost open
    field's instruction is field code, which Word does not render as prose: it
    joins that field's instruction in source order and never the paragraph's
    text. Text is read as the ordinary text it is only once every open field has
    separated, so a nested field's result is recorded under its own separator and
    still kept out of the prose while an outer instruction encloses it. An
    equation, an image, a chart and SmartArt each count as that result the same
    way, and one reached while any open field is still before its separator is
    refused rather than read or inventoried.
    """
    items: list[tuple[str, str | ElementTree.Element | dict[str, object], bool]] = []
    pending: list[str] = []
    pending_kind = ""
    pending_deleted = False

    # Markers follow the stretch they sit in, so they never split the text.
    deferred: list[tuple[str, str | ElementTree.Element | dict[str, object], bool]] = []

    def close() -> None:
        if pending:
            items.append((pending_kind, "".join(pending), pending_deleted))
            pending.clear()
        items.extend(deferred)
        deferred.clear()

    stack = [(iter(paragraph), deleted, 0, False)]
    while stack:
        children, state, depth, hidden = stack[-1]
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
            if node.tag in _MATH_CONTENT:
                # Math is kept whole and never entered, so none of its stored
                # text reaches the prose around it and no ``oMath`` inside it —
                # including each one an ``oMathPara`` holds — is seen a second
                # time. A run's hidden properties cannot reach it, because math
                # sits beside runs rather than inside one: hidden math waits for
                # its own slice.
                _field_result(fields, "math")
                close()
                items.append(("e", node, state))
                if len(items) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                continue
            if node.tag in _DOCX_DRAWINGS and (
                declared := _docx_drawing_object(node)
            ):
                # An image, a chart and SmartArt are each recorded where they
                # sit and hold no prose of their own in the story, so none is
                # entered: a diagram's text is the data model part's, read
                # there. A drawing object that declares none of the three -- the
                # one holding a text box above all -- is still the transparent
                # wrapper it is today and is walked for the content it carries.
                _field_result(fields, _DOCX_GRAPHICS[declared])
                close()
                items.append((declared, node, state))
                if len(items) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                continue
            if node.tag in _DOCX_OBJECTS and _docx_ole_object(node) is not None:
                # A legacy object is recorded where it sits and holds no prose of
                # its own in the story, so it is not entered: its payload is
                # never opened and the VML shape standing in for it on the page
                # is recorded as declared rather than read. A ``w:object``
                # declaring no object is the transparent wrapper it is today and
                # is walked for whatever content it carries.
                _field_result(fields, "an embedded object")
                close()
                items.append(("o", node, state))
                if len(items) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                continue
            if node.tag in _DOCX_PICTURES and _docx_vml_image(node) is not None:
                # A standalone legacy image is recorded where it sits and holds
                # no prose of its own, so the container is not entered: its bytes
                # are never opened and its shape is never drawn. A ``w:pict``
                # that shows no image of its own -- the one holding a VML text
                # box above all -- is the transparent wrapper it is today and is
                # walked for the content it carries.
                _field_result(fields, "a legacy image")
                close()
                items.append(("p", node, state))
                if len(items) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                continue
            if tag == "fldSimple":
                # The field's inventory is ordered before the result Word stored
                # under it, which then reads as the text it is: no ``continue``,
                # so the children are walked once as a transparent wrapper.
                close()
                items.append(("m", node, state))
                if len(items) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
            if tag == "fldChar":
                record = _docx_field_char(node, fields)
                if record is not None:
                    close()
                    items.append(("c", record, state))
                    if len(items) > MAX_SEGMENTS:
                        raise OOXMLInvalid("OOXML extraction has too many segments")
                continue
            if tag in _INLINE_MARKS:
                if ("d" if state else "t", state) != (pending_kind, pending_deleted):
                    close()
                    pending_kind, pending_deleted = "d" if state else "t", state
                deferred.append(("m", node, state))
                if len(items) + len(deferred) > MAX_SEGMENTS:
                    raise OOXMLInvalid("OOXML extraction has too many segments")
                if tag != "hyperlink":
                    continue
            text = {
                "t": node.text,
                "delText": node.text,
                "instrText": node.text,
                "tab": "\t",
            }.get(tag)
            if tag in {"br", "cr"}:
                text = "\n"
            if tag in {"t", "delText", "instrText", "tab", "br", "cr"}:
                if not text:
                    continue
                if fields and fields[-1].record is None:
                    fields[-1].extend(text)
                    continue
                if tag == "instrText":
                    # Instruction text reached outside the instruction it belongs
                    # to is still instruction, so it is still never prose.
                    continue
                if tag in {"t", "delText"}:
                    # A result read here is stored under every separator still
                    # open around it, exactly as a simple field counts its own.
                    for open_field in fields:
                        if open_field.record is not None:
                            open_field.result = True
                if any(open_field.record is None for open_field in fields):
                    # A nested field's result sits inside an outer field's
                    # instruction, and Word renders no more of that than of the
                    # code around it: the inner record keeps the result, the
                    # prose does not, until the outer field separates too.
                    continue
                removed = state or tag == "delText"
                kind = "v" if hidden else "d" if removed else "t"
                if (kind, removed) != (pending_kind, pending_deleted):
                    close()
                    pending_kind, pending_deleted = kind, removed
                    if len(items) > MAX_SEGMENTS:
                        raise OOXMLInvalid("OOXML extraction has too many segments")
                pending.append(text)
                continue
            inner, level = _revised(tag, state, depth)
            # Hidden state belongs to the run that declares it, and runs do not
            # nest, so it never leaks past the run it was read from.
            stack.append(
                (
                    iter(node),
                    inner,
                    level,
                    _docx_run_hidden(node) if tag == "r" else hidden,
                )
            )
            break
        else:
            stack.pop()
    close()
    return items


def _field_result(fields: list[_FieldState], held: str) -> None:
    """Count one non-text item as the result of each open field, or refuse it.

    An equation or an image under a separator is the result Word stored there,
    counted by every open field exactly as text is. An instruction is field
    code, which carries neither of its own, so one reached while any field is
    still before its separator has no one reading: it is neither the field's
    code nor the document's content, and it is refused rather than dropped,
    inventoried as content or read as prose.
    """
    if any(open_field.record is None for open_field in fields):
        raise OOXMLInvalid(f"DOCX field instruction holds {held}")
    for open_field in fields:
        open_field.result = True


def _docx_equation(element: ElementTree.Element) -> dict[str, object]:
    """Record one ``m:oMath`` or ``m:oMathPara`` as bounded, uninterpreted OMML.

    ``ElementTree`` writes the subtree deterministically for a given parse, so
    the same package always yields the same markup; prefixes are the writer's
    own rather than the file's, which is what ``normalized`` says. The element's
    tail is left out because that is the text after the math, not part of it.
    ``directText`` is only the stored ``m:t`` text in source order, so nothing is
    read out of the structure the markup carries and no reading is generated
    from it. An ``oMathPara`` is one equation, and its own ``oMath`` children are
    part of the markup and the text recorded here rather than equations of their
    own.

    The byte bound covers the markup and the direct text together, and an
    equation past it is refused: clipping either would hand on markup that no
    longer parses, or a reading the file does not carry.
    """
    tail, element.tail = element.tail, None
    try:
        markup = ElementTree.tostring(element, encoding="unicode")
    finally:
        element.tail = tail
    direct = "".join(node.text for node in element.iter(_MATH_TEXT) if node.text)
    if (
        len(markup.encode("utf-8")) + len(direct.encode("utf-8"))
        > _MAX_DOCX_EQUATION_BYTES
    ):
        raise OOXMLInvalid("DOCX equation is too large to record")
    return {
        "format": "OMML",
        "serialization": "normalized",
        "sourceMarkup": markup,
        "directText": direct,
        "generatedReading": False,
    }


def _docx_drawing_blip(drawing: ElementTree.Element) -> ElementTree.Element | None:
    """Return the ``a:blip`` one drawing object displays an image through.

    The route in is the schema's own and nothing else: the object's direct
    ``a:graphic``, that graphic's direct ``a:graphicData``, the DrawingML
    ``pic:pic`` it holds, that picture's direct ``blipFill``, and the blip
    inside it. Every step but the fill is held to the qualified names OOXML
    defines for it, so an element merely called ``graphic``, ``graphicData``,
    ``pic`` or ``blip`` in another vocabulary is not on the route and declares
    no image, whichever step it stands at. A blip reached anywhere
    else below the object -- a picture fill on a text box's shape properties, a
    fill carried in an extension list, the drawing a chart or a diagram holds
    of its own -- is not the image this object displays and is passed over, so
    a drawing that displays no image reports none rather than one found
    somewhere else. Charts, SmartArt, embedded objects and VML are other
    declarations entirely, each read on its own route.

    One drawing object displays one image, so two of anything along that route
    -- two graphics, two pictures, two fills, two blips -- leave which image it
    shows unanswerable, and nothing here chooses between them.
    """
    blips = [
        blip
        for graphic in drawing
        if graphic.tag in _DRAWING_GRAPHIC
        for data in graphic
        if data.tag in _DRAWING_GRAPHIC_DATA
        for picture in data
        if picture.tag in _DRAWING_PICTURE
        for fill in picture
        if _local(fill.tag) == "blipFill"
        for blip in fill
        if blip.tag in _DRAWING_BLIP
    ]
    if len(blips) > 1:
        raise OOXMLInvalid("DOCX drawing declares more than one image reference")
    return blips[0] if blips else None


def _docx_drawing_chart(drawing: ElementTree.Element) -> str | None:
    """Return the relationship id one drawing object names its chart on, or None.

    The route in is the schema's own and nothing else: the object's direct
    ``a:graphic``, that graphic's direct ``a:graphicData``, and the ``c:chart``
    directly below that. Every step is held to the qualified names OOXML
    defines for it -- the two DrawingML main namespaces for the graphic and its
    data, the two DrawingML chart namespaces for the reference -- so an element
    merely called ``graphic``, ``graphicData`` or ``chart`` in another
    vocabulary is not on the route and names nothing, whichever step it stands
    at. A reference reached anywhere else below the object -- in an
    alternate-content fallback, inside a text box's own content, in an
    extension list, below another graphic carried along -- is not what this
    object displays and is passed over.

    One drawing object displays one graphic, so two of anything along that
    route leaves which chart it shows unanswerable and nothing here chooses
    between them. A reference that names no relationship identifies no chart,
    and no chart path is derived from the story's own name: the id is resolved
    through the holding part's relationships or not at all.
    """
    charts = [
        node
        for graphic in drawing
        if graphic.tag in _DRAWING_GRAPHIC
        for data in graphic
        if data.tag in _DRAWING_GRAPHIC_DATA
        for node in data
        if node.tag in _DRAWING_CHART
    ]
    if not charts:
        return None
    if len(charts) > 1:
        raise OOXMLInvalid("DOCX drawing declares more than one chart reference")
    relation_id = _relationship_id(charts[0].attrib)
    if not relation_id:
        raise OOXMLInvalid("DOCX chart reference names no relationship")
    return relation_id


def _docx_drawing_object(drawing: ElementTree.Element) -> str | None:
    """Say which graphic a drawing object declares, by its item kind, or None.

    A displayed image, a chart and SmartArt are three different declarations on
    the same route, so an object making two of them leaves what it shows
    unanswerable and nothing here chooses between them: it is refused, named by
    the pair it made. An object making none is the transparent wrapper it has
    always been -- the one holding a text box above all -- and is walked for the
    content it carries. Embedded objects and VML are other declarations
    entirely, each reached by a container of its own.
    """
    declared = [
        kind
        for kind, present in (
            ("g", _docx_drawing_blip(drawing) is not None),
            ("k", _docx_drawing_chart(drawing) is not None),
            ("s", _diagram_reference(drawing, "DOCX", "drawing") is not None),
        )
        if present
    ]
    if len(declared) > 1:
        raise OOXMLInvalid(
            "DOCX drawing declares both "
            f"{_DOCX_GRAPHICS[declared[0]]} and {_DOCX_GRAPHICS[declared[1]]}"
        )
    return declared[0] if declared else None


def _docx_ole_object(element: ElementTree.Element) -> ElementTree.Element | None:
    """Return the object one ``w:object`` declares, or None where it declares none.

    The route in is one hop and no deeper: the ``o:OLEObject`` directly below
    the container. A declaration reached anywhere else is not this container's
    own -- one nested inside the VML shape standing in for the object, one
    carried in an alternate-content fallback, one below another object hung
    along, and one written in a vocabulary that merely shares the local name are
    all passed over -- so a container declaring no object is reported as
    declaring none rather than as declaring something found elsewhere, and stays
    the transparent wrapper it is today.

    A container holds one object, so two declarations leave which object it
    shows ambiguous and nothing here chooses between them.
    """
    declared = [node for node in element if node.tag == _DOCX_OLE_OBJECT]
    if len(declared) > 1:
        raise OOXMLInvalid("DOCX object declares more than one embedded object")
    return declared[0] if declared else None


def _docx_vml_image(
    element: ElementTree.Element,
) -> tuple[ElementTree.Element, ElementTree.Element, str] | None:
    """Return the shape, image data and relationship id one ``w:pict`` shows.

    The route in is two hops and no deeper: the ``v:shape`` directly below the
    container, and the ``v:imagedata`` directly below that shape. A shape or an
    image reference reached anywhere else is not this container's own -- one
    inside a group, one carried in an alternate-content fallback, one below a
    shape hung along, and one written in a vocabulary that merely shares the
    local name are all passed over -- so a container showing no standalone image
    is reported as showing none and stays the transparent wrapper it is today,
    which is what keeps a ``w:pict`` holding only a VML text box yielding its
    prose. The stand-in a ``w:object`` carries is reached by its own route and
    never this one, so it is still recorded as declared rather than inventoried
    here.

    A container shows one image, so two shapes, or two image references on one
    shape, leave which image it shows ambiguous and nothing here chooses between
    them. The single relationship-namespaced ``r:id`` on the image data is the
    one reference read: an id written in no namespace, or in two of the
    namespaces that carry it, names no one relationship, and image data naming
    none declares an image it does not identify.
    """
    shapes = [node for node in element if node.tag == _DOCX_VML_SHAPE]
    if len(shapes) > 1:
        raise OOXMLInvalid("DOCX picture declares more than one shape")
    if not shapes:
        return None
    data = [node for node in shapes[0] if node.tag == _DOCX_VML_IMAGEDATA]
    if len(data) > 1:
        raise OOXMLInvalid("DOCX picture shape declares more than one image")
    if not data:
        return None
    ids = _namespaced_ids(data[0], "id")
    if len(ids) > 1:
        raise OOXMLInvalid("DOCX VML image names more than one image relationship")
    if not ids:
        raise OOXMLInvalid("DOCX VML image names no image relationship")
    return shapes[0], data[0], ids[0]


def _docx_vml_placement(style: str | None) -> str:
    """Say where a legacy shape sits, from its own ``style`` and nothing else.

    A legacy shape is part of the text flow unless its style says otherwise, so
    ``inline`` is what a shape with no style, no ``position`` declaration or any
    other position declares. Only the exact ``position:absolute`` CSS pair moves
    it off the flow; whitespace around either half is CSS syntax rather than part
    of the token, so it is not read as part of either.

    Nothing else is consulted: no wrap, no offset, no anchor child and nothing
    the shape is drawn relative to. A style declaring ``position`` twice with two
    different values leaves where the shape sits unanswerable, and nothing here
    chooses between them.
    """
    positions = set()
    for declaration in (style or "").split(";"):
        name, colon, value = declaration.partition(":")
        if colon and name.strip() == _DOCX_VML_POSITION:
            positions.add(value.strip())
    if len(positions) > 1:
        raise OOXMLInvalid("DOCX VML image declares more than one position")
    return "anchor" if positions == {_DOCX_VML_ABSOLUTE} else "inline"


def _docx_doc_pr(drawing: ElementTree.Element) -> dict[str, str | None]:
    """Return the claims a drawing object's author made about it in ``wp:docPr``.

    Every field is present whatever the object wrote, so one image's record is
    shaped like every other's, and each claim is kept apart from the others: a
    claim nothing wrote is ``None``, and a claim written empty is the empty
    string, because a file that declared a description and left it blank said
    something a file that declared none did not.

    Values are the author's own, read as written: nothing is trimmed,
    normalised, recased or substituted, and no claim is invented from another
    one -- a ``name`` is what the authoring tool called the object, usually one
    it generated, and never stands in for a title or a description -- or from
    the image itself, which is never opened. ``wp:docPr`` is the one place a
    Word drawing carries these, so two of them leave the object's identity and
    its alt text ambiguous and nothing here picks one.
    """
    properties = [node for node in drawing if node.tag in _DOCX_DOC_PR]
    if len(properties) > 1:
        raise OOXMLInvalid("DOCX drawing declares its properties twice")
    claims = properties[0].attrib if properties else {}
    return {
        "docPrId": claims.get("id"),
        **{field: claims.get(attribute) for attribute, field in _PICTURE_CLAIMS},
    }


def _docx_drawing_image(
    package: _Package,
    relations: dict[str, tuple[str, bool]],
    types: dict[str, str],
    blip: ElementTree.Element,
) -> dict[str, object]:
    """Inventory the image one blip names, without ever reaching its bytes.

    A blip declares its image on two attributes, each read in the relationships
    namespaces OOXML defines for it: ``r:embed`` names a part inside the
    package, ``r:link`` names a reference out of it. Both may be written, and
    each is recorded in its own fields so an embedded part is never read as a
    link or the reverse. One attribute written in both namespaces at once names
    two relationships, which is refused for the reason two blips are, and a
    blip naming neither declares an image it does not identify -- there is
    nothing to inventory and nothing here invents one.

    The ids resolve only through the relationships of the part that holds the
    reference, and no other, so containment, path safety and existence were all
    settled when the package validated them. Nothing here opens, decodes,
    renders, OCRs or fetches anything: an embedded part contributes its name,
    its declared type and the byte length the package already measured, and a
    linked target is carried exactly as written, with no local path inspected
    and no reference followed.
    """
    embedded = _blip_id(blip, "embed", "DOCX drawing")
    linked = _blip_id(blip, "link", "DOCX drawing")
    if embedded is None and linked is None:
        raise OOXMLInvalid("DOCX drawing names no image relationship")
    record = dict(_DRAWING_IMAGE)
    if embedded is not None:
        record.update(
            _embedded_image(package, relations, types, embedded, "DOCX drawing")
        )
    if linked is not None:
        record.update(_linked_image(relations, types, linked, "DOCX drawing"))
    return record


def _docx_field_record(
    form: str, instruction: str, truncated: bool, saved_result: bool
) -> dict[str, object]:
    """Record one Word field, in either form, as inert bounded metadata.

    ``instruction`` already sits inside ``_MAX_TARGET_CHARS`` and ``truncated``
    is whether source characters were dropped to keep it there, so the caller
    that bounded it says so rather than the bound being rediscovered here. The
    instruction is never parsed, so the field is not executed and nothing it
    names is resolved. Only whether Word left a result behind is recorded:
    whether that result still answers the instruction cannot be known from the
    file, so its freshness is unknown.
    """
    return {
        "kind": "wordField",
        "fieldForm": form,
        "instruction": instruction,
        "instructionTruncated": truncated,
        "executed": False,
        "refreshed": False,
        "savedResultPresent": saved_result,
        "resultFreshness": "unknown",
    }


def _docx_simple_field(element: ElementTree.Element) -> dict[str, object]:
    """Inventory one ``fldSimple``, refusing one that names no instruction."""
    instruction = _attr(element, "instr")
    if instruction is None:
        raise OOXMLInvalid("DOCX simple field names no instruction")
    return _docx_field_record(
        "simple",
        _clip(instruction, _MAX_TARGET_CHARS),
        len(instruction) > _MAX_TARGET_CHARS,
        any(
            node.text for node in element.iter() if _local(node.tag) in {"t", "delText"}
        ),
    )


def _docx_field_char(
    node: ElementTree.Element, fields: list[_FieldState]
) -> dict[str, object] | None:
    """Advance ``fields`` over one ``fldChar``, returning a record to order here.

    A ``separate`` records the field before the result Word stored under it, so
    the record is returned at that boundary and its ``savedResultPresent`` is
    settled when the matching ``end`` is reached. A field that never separates
    stored no result, so it is recorded at its ``end`` instead: nothing is
    reordered, because there is no result for the record to precede.

    A boundary that opens no field, a second ``separate``, an unknown kind and a
    stack past the safety bound are all refused: none of them has one
    reading, and guessing one would invent structure the file does not carry.
    """
    kind = _attr(node, "fldCharType")
    if kind not in {"begin", "separate", "end"}:
        raise OOXMLInvalid("DOCX field boundary names no kind")
    if kind == "begin":
        if len(fields) >= _MAX_DOCX_FIELD_DEPTH:
            raise OOXMLInvalid("DOCX fields are nested too deeply")
        fields.append(_FieldState())
        return None
    if not fields:
        raise OOXMLInvalid("DOCX field boundary opens no field")
    if kind == "separate":
        if fields[-1].record is not None:
            raise OOXMLInvalid("DOCX field separates twice")
        fields[-1].record = _docx_field_record(
            "complex", fields[-1].instruction, fields[-1].truncated, False
        )
        return fields[-1].record
    closed = fields.pop()
    if closed.record is None:
        return _docx_field_record(
            "complex", closed.instruction, closed.truncated, False
        )
    closed.record["savedResultPresent"] = closed.result
    return None


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


def _docx_on_off(element: ElementTree.Element) -> bool:
    """Read one WordprocessingML on/off property, refusing an unreadable value.

    An absent ``val`` means the property is on, which is how Word writes the
    plain switch. The type is closed, so a value outside it is refused rather
    than read as whichever of the two it resembles.
    """
    value = _attr(element, "val")
    if value is None or value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise OOXMLInvalid("DOCX on/off property is malformed")


def _docx_run_hidden(run: ElementTree.Element) -> bool:
    """Whether a run's own properties hide its text.

    Only ``vanish``/``webHidden`` written directly on this run count. Hidden
    state inherited from a character, paragraph, table or document-default
    style is deliberately not resolved here: ``vanish`` is a toggle property,
    so an accurate answer would have to apply the toggle rules across the whole
    style hierarchy -- document defaults, table style conditional formatting,
    the paragraph style, numbering and the character style -- and a partial
    resolution would mislabel text either way. A style-hidden run therefore
    reads as ordinary visible text, as it does today. ``rPrChange`` holds the
    *previous* run properties, so a ``vanish`` inside it hides nothing now.
    """
    properties = next((item for item in run if _local(item.tag) == "rPr"), None)
    if properties is None:
        return False
    return any(
        _local(item.tag) in _HIDDEN_RUN_PROPERTIES and _docx_on_off(item)
        for item in properties
    )


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
