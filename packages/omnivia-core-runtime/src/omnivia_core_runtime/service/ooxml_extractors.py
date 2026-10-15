"""Bounded, inert structural readers for admitted Office Open XML packages."""

from __future__ import annotations

import datetime
import io
import math
import posixpath
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Self
from xml.etree import ElementTree

from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ocr_resources import (
    ExtractionSettings,
    OcrEngine,
    run_tesseract,
)
from omnivia_core_runtime.service.raster_extractors import (
    EMBEDDED_MALFORMED,
    EMBEDDED_NOT_EMBEDDED,
    EmbeddedImagePass,
    EmbeddedImageResult,
    ExtractionBudget,
)

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
# The relationship and content types that together qualify a declared-table
# part: the one part a worksheet keeps a table's own geometry, identity and
# column headers in. As with every other type here, only the two URIs OOXML
# defines qualify -- Transitional and Strict -- so nothing reaches a table by
# ending the same way, and a query table, a pivot table or a single-cell table
# relationship reaches no declared table at all.
_TABLE_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/table",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/table",
    }
)
# Compared against ``declared_type``, which case-folds; the authored spelling is
# already folded. The content type is the same under both profiles.
_TABLE_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
)
# The two SpreadsheetML main namespaces OOXML defines, Transitional and Strict.
# The rest of the XLSX reader matches local names, because a sheet's vocabulary
# is settled by the part it was reached through; a table part is reached by its
# declared type, so the root is held to these as the one remaining check that
# the part holds the vocabulary the declaration promised.
_SPREADSHEETML: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "http://purl.oclc.org/ooxml/spreadsheetml/main",
    }
)
# A declared table's bounds are the reader's own. One sheet's tables are counted
# before any of their parts is opened, and a table's columns are bounded by its
# own declared count before the list is read, so a width nothing wrote is never
# expanded. The column bound is well under the grid's own width on purpose: the
# whole of a table's declared column metadata is reported in one inventory
# record, and that record has to stay under the stored segment value cap.
# ponytail: 256 columns and 16 KiB of declared metadata per table; a record per
# column if wider declared tables have to be accepted.
_MAX_XLSX_TABLES: Final = 256
_MAX_XLSX_TABLE_COLUMNS: Final = 256
_MAX_XLSX_TABLE_METADATA_CHARS: Final = 16 * 1024
# What one declared column records, defaulted so every column carries the same
# keys whether or not it declares a formula or a totals row. ``id`` and ``name``
# are the table's own authoritative header for that position -- the header a
# consumer reads instead of looking at the first row -- and the rest is declared
# markup kept exactly as written: a calculated-column formula and a totals-row
# formula are reported as text beside the column, never evaluated, and a
# totals-row function is the name the file wrote rather than an applied one.
_XLSX_TABLE_COLUMN: Final[dict[str, object]] = {
    "id": None,
    "name": None,
    "totalsRowLabel": None,
    "totalsRowFunction": None,
    "calculatedColumnFormula": None,
    "totalsRowFormula": None,
}
# A workbook's defined names are counted before any is read, and each one's
# expression is bounded on its own: the whole declaration is reported in one
# record, and the stored expression is the only part of it that is not already
# held to the metadata bound.
# ponytail: 4096 names and 8 KiB of expression each; a wider profile only if a
# real workbook is refused by these.
_MAX_XLSX_DEFINED_NAMES: Final = 4_096
_MAX_XLSX_DEFINED_NAME_CHARS: Final = 8 * 1024
# The boolean attributes a defined name carries, read in this order so every
# record carries the same keys. Each one is inert here: ``function`` says the
# name was declared as a function, ``vbProcedure`` and ``xlm`` say which
# vocabulary wrote it, and nothing is called, bound or run on account of any of
# them. SpreadsheetML defaults all six to false, so an absent one is a declared
# false rather than an unknown.
_XLSX_DEFINED_NAME_FLAGS: Final = (
    "hidden",
    "function",
    "vbProcedure",
    "xlm",
    "publishToServer",
    "workbookParameter",
)
# The content type that qualifies a part as a worksheet's classic comments
# ("notes") list. The relationship type is the pair OOXML defines for every
# comments part, shared with PresentationML -- see ``_COMMENTS_RELATIONSHIPS`` --
# so the declared content type is the whole of what keeps a SpreadsheetML
# comment list from being read as a slide's, and the other way round. Compared
# against ``declared_type``, which case-folds; the authored spelling is already
# folded.
_XLSX_COMMENTS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"
)
# One comments part's own bounds. A comment body is rich text, so its runs are
# work the part asks for whether or not any of them stores text; the run bound
# is taken across the whole part, for the reason the sheet-wide table bound is.
# ponytail: 10k comments and 10k authors, 20k runs across the whole part, and
# 100k characters per comment; widen only if a real workbook is refused by
# these.
_MAX_XLSX_COMMENTS: Final = 10_000
_MAX_XLSX_COMMENT_AUTHORS: Final = 10_000
_MAX_XLSX_COMMENT_RUNS: Final = 20_000
_MAX_XLSX_COMMENT_CHARS: Final = 100_000
# The relationship and content types that together qualify the two parts
# threaded comments live in: the workbook-wide person list every comment cites,
# and one worksheet's own comment list. Both are a Microsoft extension, so each
# type has one defined URI rather than a Strict/Transitional pair, and neither
# reaches -- nor is reached by -- the classic SpreadsheetML comment types above.
# The relationship path is matched exactly, so the capital in
# ``threadedComment`` is part of the route; the content types are compared
# against ``declared_type``, which case-folds, and are written folded here.
_XLSX_PERSONS_RELATIONSHIPS: Final = frozenset(
    {"http://schemas.microsoft.com/office/2017/10/relationships/person"}
)
_XLSX_PERSONS_CONTENT_TYPE: Final = "application/vnd.ms-excel.person+xml"
_XLSX_THREADED_RELATIONSHIPS: Final = frozenset(
    {"http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"}
)
_XLSX_THREADED_CONTENT_TYPE: Final = "application/vnd.ms-excel.threadedcomments+xml"
# The one namespace both parts are written in. As with every part reached by its
# declared type alone, the namespace and the root are the remaining check that
# the part holds the vocabulary the declaration promised, and everything below
# the root is held to that same namespace: a lookalike spelling one of these
# names in another namespace -- or in none -- declares nothing here.
_XLSX_THREADED_MAIN: Final = (
    "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
)
# One threaded part's own bounds: the records a list may declare, counted across
# the part, and one comment's text. A threaded comment's text is a plain string
# rather than the rich text a classic comment holds, so there are no runs to
# bound separately.
# ponytail: 10k persons and 10k comments per part, 100k characters per comment;
# widen only if a real workbook is refused by these.
_MAX_XLSX_THREADED_RECORDS: Final = 10_000
_MAX_XLSX_THREADED_CHARS: Final = 100_000
# The relationship and content types that together qualify a worksheet's
# drawing part: the one part a sheet anchors its graphic frames in. As with
# every other type here, only the two URIs OOXML defines qualify --
# Transitional and Strict -- so nothing reaches a drawing by ending the same
# way, and the legacy VML drawing a sheet names beside its comments carries
# another type entirely and reaches nothing here. The content type is compared
# against ``declared_type``, which case-folds; the authored spelling is already
# folded, and it is the same under both profiles.
_XLSX_DRAWING_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/drawing",
    }
)
_XLSX_DRAWING_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.drawing+xml"
)
# The two spreadsheet-drawing namespaces a worksheet's drawing part is rooted
# in. The part is reached by its declared type, so the root is the one remaining
# check that it holds the vocabulary that declaration promised; below the root
# anchors are read by local name, as the rest of the XLSX reader does.
_XLSX_DRAWING_NAMESPACES: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
        "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
    }
)
# The three kinds of anchor a drawing positions a frame with, each beside the
# placement name a record reports it as. All three are counted, so an anchor's
# reported index is its position among the anchors the drawing wrote rather than
# among the ones read.
_XLSX_ANCHORS: Final = {
    "twoCellAnchor": "twoCell",
    "oneCellAnchor": "oneCell",
    "absoluteAnchor": "absolute",
}
# One drawing's own bound: the anchors it may declare, counted as they are
# walked and before any frame below one is reached.
# ponytail: 4096 anchors per drawing; widen only if a real workbook is refused.
_MAX_XLSX_ANCHORS: Final = 4_096
# The relationship and content types that together qualify the workbook's
# connections part: the one part a workbook keeps its stored external data
# connections in. As with every other type here, only the two URIs OOXML
# defines qualify -- Transitional and Strict -- so nothing reaches the part by
# ending the same way, and an external-link, query-table or pivot-cache
# relationship reaches no connections list at all. The content type is compared
# against ``declared_type``, which case-folds; the authored spelling is already
# folded, and it is the same under both profiles.
_XLSX_CONNECTIONS_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/connections"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/connections",
    }
)
_XLSX_CONNECTIONS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml"
)
# The workbook's own bound: the connections one list may declare, counted
# before any of them is read. Every string a record keeps is clipped, so the
# count is the whole of what bounds the inventory.
# ponytail: 4096 connections per workbook; widen only if a real workbook is
# refused by it.
_MAX_XLSX_CONNECTIONS: Final = 4_096
# ``ST_CredMethod``: how the stored connection says it would authenticate if
# anything ever opened it. Nothing here does, and no credential, password or
# token is read, stored or resolved on account of any of these -- the mode is
# the author's declared policy and is reported as the word they wrote.
_XLSX_CREDENTIAL_METHODS: Final = frozenset({"integrated", "none", "stored", "prompt"})
# The connection's free-text attributes. ``sourceFile`` and ``odcFile`` name
# where the author said the connection came from: both are recorded verbatim as
# the text they are, and neither is ever opened, resolved against the
# filesystem, fetched or normalised into a path.
_XLSX_CONNECTION_TEXT: Final = (
    "name",
    "description",
    "sourceFile",
    "odcFile",
    "singleSignOnId",
)
# The connection's boolean attributes, read tri-state: SpreadsheetML defaults
# each to false, but a default is not a declaration, and a refresh policy a
# consumer has to reason about is clearer when it says which of the two the file
# actually wrote. Every one is inert here -- ``refreshOnLoad`` and
# ``background`` describe what Excel would do, not what this reader did.
_XLSX_CONNECTION_FLAGS: Final = (
    "keepAlive",
    "new",
    "deleted",
    "onlyUseConnectionFile",
    "background",
    "refreshOnLoad",
    "saveData",
)
# The connection's unsigned numeric attributes, each beside the bound of the
# type the schema gives it: ``type`` and ``reconnectionMethod`` are
# ``xsd:unsignedInt``, the two versions ``xsd:unsignedByte``.
_XLSX_CONNECTION_NUMBERS: Final = (
    ("type", 0xFFFF_FFFF),
    ("reconnectionMethod", 0xFFFF_FFFF),
    ("refreshedVersion", 0xFF),
    ("minRefreshableVersion", 0xFF),
)
# The direct child property sets a connection may carry, each one the payload
# of a kind of source: a database connection string and command, an OLAP or web
# query's own properties, a text import's settings, the query parameters, and
# the extension point. None of them is read. A record says which kinds were
# declared and how many parameters the parameter list held, and nothing inside
# any of them -- no connection string, no command text, no parameter value, no
# extension body -- is ever copied out.
_XLSX_CONNECTION_CHILDREN: Final = (
    "dbPr",
    "olapPr",
    "webPr",
    "textPr",
    "parameters",
    "extLst",
)
# The relationship and content types that together qualify one external-link
# part: the part a workbook keeps a single link to another workbook, to a DDE
# server or to an OLE server in. As with every other type here, only the two
# URIs OOXML defines qualify -- Transitional and Strict -- so nothing reaches one
# by ending the same way, and a connections, query-table or pivot-cache
# relationship reaches no external link at all. The content type is compared
# against ``declared_type``, which case-folds, so the type is written folded
# here rather than in OOXML's own camel case; it is the same under both
# profiles.
_XLSX_EXTERNAL_LINK_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/externalLink"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/externalLink",
    }
)
_XLSX_EXTERNAL_LINK_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.externallink+xml"
)
# ``EG_ExternalLinkChoice``: the three forms an external link takes, each beside
# the relationship types OOXML defines for the target it names and whether the
# schema requires the form to name one. An ``externalBook`` names the workbook
# it links to by an ``externalLinkPath`` relationship and an ``oleLink`` the
# server document it links to by an ``oleObject`` one; a ``ddeLink`` names a
# running service and topic by name and relates no target at all, which is why
# its entry is None rather than an empty set.
#
# ``CT_OleLink`` requires its ``r:id`` and ``CT_ExternalBook`` leaves it
# optional, so a cached-only workbook link -- one that stored sheet names and
# values but no path -- is the inventory it is rather than a refusal. Where a
# relationship is named it has to be external, because both name something
# outside the package by definition and an internal one contradicts the
# declaration rather than qualifying it. Nothing here opens, resolves, fetches,
# launches or refreshes either.
_XLSX_EXTERNAL_LINK_KINDS: Final[dict[str, tuple[frozenset[str], bool] | None]] = {
    "externalBook": (
        frozenset(
            {
                (
                    "http://schemas.openxmlformats.org/officeDocument/2006"
                    "/relationships/externalLinkPath"
                ),
                (
                    "http://purl.oclc.org/ooxml/officeDocument/relationships"
                    "/externalLinkPath"
                ),
            }
        ),
        False,
    ),
    "ddeLink": None,
    "oleLink": (
        frozenset(
            {
                (
                    "http://schemas.openxmlformats.org/officeDocument/2006"
                    "/relationships/oleObject"
                ),
                "http://purl.oclc.org/ooxml/officeDocument/relationships/oleObject",
            }
        ),
        True,
    ),
}
# The workbook's own bound: the external links it may declare, counted before
# any of their parts is opened. Every string a record keeps is clipped, so the
# count is the whole of what bounds the inventory.
# ponytail: 256 external links per workbook; widen only if a real workbook is
# refused by it.
_MAX_XLSX_EXTERNAL_LINKS: Final = 256
# What one external link's record carries, defaulted so a workbook link, a DDE
# link and an OLE link are shaped alike, exactly as the picture-media and
# legacy-object records are. ``target`` is the text the relationship holds and
# ``targetScheme`` the scheme that text names; reporting either opens nothing.
# A link that named no target -- which only an ``externalBook`` may do -- leaves
# the whole group absent rather than carrying an invented one.
#
# The three groups after it are the inert metadata each form declares: how many
# sheet names, defined names and cached sheet blocks a linked workbook's part
# stored, the service and topic a DDE link names, and the ``ProgID`` an OLE link
# names. Only counts come out of the groups -- the cached values under
# ``sheetDataSet`` are the other workbook's own content, and copying them would
# report data as though the link had been followed.
#
# ``refreshPerformed`` and ``externalAccessPerformed`` are the two standing
# claims on every record, for the reason a stored connection's record carries
# them: this reader read a declaration and did none of what it describes.
_XLSX_EXTERNAL_LINK_RECORD: Final[dict[str, object]] = {
    "kind": "externalLink",
    "linkKind": None,
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "targetRelationshipId": None,
    "targetRelationshipType": None,
    "target": None,
    "targetExternal": None,
    "targetScheme": None,
    "sheetNameCount": None,
    "definedNameCount": None,
    "cachedSheetCount": None,
    "ddeService": None,
    "ddeTopic": None,
    "ddeItemCount": None,
    "progId": None,
    "oleItemCount": None,
}
# The declared group each form holds its items in, beside the member counted
# inside it. One group of a kind per link, so a second leaves two at one
# position and nothing here picks between them.
_XLSX_EXTERNAL_LINK_GROUPS: Final[dict[str, tuple[tuple[str, str, str], ...]]] = {
    "externalBook": (
        ("sheetNameCount", "sheetNames", "sheetName"),
        ("definedNameCount", "definedNames", "definedName"),
        ("cachedSheetCount", "sheetDataSet", "sheetData"),
    ),
    "ddeLink": (("ddeItemCount", "ddeItems", "ddeItem"),),
    "oleLink": (("oleItemCount", "oleItems", "oleItem"),),
}
# The claims each form is required to declare, beside the field its record keeps
# them in. A DDE link names the service and the topic it would talk to and an
# OLE link the server that would open its document; all three are text the
# author wrote, and nothing here looks one up, resolves it or launches anything.
_XLSX_EXTERNAL_LINK_CLAIMS: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "externalBook": (),
    "ddeLink": (("ddeService", "ddeService"), ("ddeTopic", "ddeTopic")),
    "oleLink": (("progId", "progId"),),
}
# The three part types a pivot report is kept in, each beside the two URIs
# OOXML defines for its relationship -- Transitional and Strict -- and the
# content type its own part carries. As on every other route here, nothing
# qualifies by ending the same way: a connections, external-link or query-table
# relationship reaches no pivot part, and a pivot-cache relationship reaches no
# pivot table. The content types are compared against ``declared_type``, which
# case-folds, so they are written folded here rather than in OOXML's own camel
# case; each is the same under both profiles.
_XLSX_PIVOT_CACHE_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/pivotCacheDefinition"
        ),
        (
            "http://purl.oclc.org/ooxml/officeDocument/relationships"
            "/pivotCacheDefinition"
        ),
    }
)
_XLSX_PIVOT_CACHE_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ".pivotcachedefinition+xml"
)
_XLSX_PIVOT_RECORDS_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/pivotCacheRecords"
        ),
        (
            "http://purl.oclc.org/ooxml/officeDocument/relationships"
            "/pivotCacheRecords"
        ),
    }
)
_XLSX_PIVOT_RECORDS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ".pivotcacherecords+xml"
)
_XLSX_PIVOT_TABLE_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/pivotTable"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/pivotTable",
    }
)
_XLSX_PIVOT_TABLE_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.pivottable+xml"
)
# The bounds, each taken before the work it bounds: the caches a workbook may
# declare, the pivot tables one sheet may declare, and the fields one cache
# definition or one table definition may declare. Every string a record keeps is
# clipped and no cached value is ever read, so these counts are the whole of
# what bounds the inventory.
# ponytail: 256 caches per workbook, 256 tables per sheet, 16384 fields per
# definition -- the grid's own column ceiling; widen only if a real workbook is
# refused by one of them.
_MAX_XLSX_PIVOT_CACHES: Final = 256
_MAX_XLSX_PIVOT_TABLES: Final = 256
_MAX_XLSX_PIVOT_FIELDS: Final = 16_384
# ``ST_SourceType``: where the cache says its data was taken from when it was
# last saved. Closed, so a type outside the four is refused rather than read as
# whichever it resembles. Nothing here reaches any of them -- no worksheet is
# re-read, no consolidation is recomputed, no scenario is replayed and no
# external source is resolved.
_XLSX_PIVOT_SOURCE_TYPES: Final = frozenset(
    {"worksheet", "external", "consolidation", "scenario"}
)
# The cache's free-text attributes: who the file says last refreshed it and when
# it says that happened. Both are the author's own claim about a refresh that
# happened elsewhere, recorded as written; neither is verified, and
# ``refreshedDate`` is kept as the text it is rather than decoded into a date,
# because decoding a serial is a calculation this route does not perform.
_XLSX_PIVOT_CACHE_TEXT: Final = ("refreshedBy", "refreshedDate")
# The cache's boolean attributes, read tri-state for the reason a connection's
# are: SpreadsheetML defaults each, but a default is not a declaration, and a
# refresh policy a consumer has to reason about is clearer when it says which of
# the two the file actually wrote. Every one is inert here -- ``refreshOnLoad``
# and ``enableRefresh`` describe what Excel would do, not what this reader did.
_XLSX_PIVOT_CACHE_FLAGS: Final = (
    "invalid",
    "saveData",
    "refreshOnLoad",
    "optimizeMemory",
    "enableRefresh",
    "backgroundQuery",
    "upgradeOnRefresh",
    "tupleCache",
    "supportSubquery",
    "supportAdvancedDrill",
)
# The cache's unsigned numeric attributes, each beside the bound of the type the
# schema gives it: ``recordCount`` is ``xsd:unsignedInt`` and the three versions
# ``xsd:unsignedByte``. ``recordCount`` is the figure the cache stored, reported
# as stored and never checked against the records part, because agreeing the two
# would mean reading the records.
_XLSX_PIVOT_CACHE_NUMBERS: Final = (
    ("recordCount", 0xFFFF_FFFF),
    ("createdVersion", 0xFF),
    ("refreshedVersion", 0xFF),
    ("minRefreshableVersion", 0xFF),
)
# The table's free-text attributes: the captions the author wrote over the
# report. None of them is an identity, so each is clipped rather than refused
# for length, and one written empty stays the empty claim the file made.
_XLSX_PIVOT_TABLE_TEXT: Final = (
    "dataCaption",
    "rowHeaderCaption",
    "colHeaderCaption",
    "grandTotalCaption",
    "errorCaption",
    "missingCaption",
)
_XLSX_PIVOT_TABLE_NUMBERS: Final = (
    ("createdVersion", 0xFF),
    ("updatedVersion", 0xFF),
    ("minRefreshableVersion", 0xFF),
)
# The field lists a table definition declares, beside the member counted inside
# each. Only the counts come out: a field's own items are the cached data this
# route does not copy, so they are counted and never read. One list of a kind per
# table, so a second leaves two at one position and nothing here picks between
# them.
_XLSX_PIVOT_TABLE_GROUPS: Final = (
    ("pivotFieldCount", "pivotFields", "pivotField"),
    ("rowFieldCount", "rowFields", "field"),
    ("colFieldCount", "colFields", "field"),
    ("pageFieldCount", "pageFields", "pageField"),
    ("dataFieldCount", "dataFields", "dataField"),
)
# The relationship and content types that together qualify one query-table part:
# the part the saved state of a data query over a range is kept in. As on every
# other route here, nothing qualifies by ending the same way -- a connections,
# external-link or pivot-cache relationship reaches no query table -- and the
# content type is compared against ``declared_type``, which case-folds, so it is
# written folded here rather than in OOXML's own camel case. Both are the same
# under either profile apart from the two relationship URIs OOXML defines.
_XLSX_QUERY_TABLE_RELATIONSHIPS: Final = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            "/queryTable"
        ),
        "http://purl.oclc.org/ooxml/officeDocument/relationships/queryTable",
    }
)
_XLSX_QUERY_TABLE_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.querytable+xml"
)
# The bounds, each taken before the work it bounds: the query tables one sheet's
# own route and its declared tables may together reach, counted before any of
# their parts is opened, and the fields one saved refresh may declare. Every
# string a record keeps is clipped and no field name, parameter or cached value
# is ever read, so these counts are the whole of what bounds the inventory.
# ponytail: 256 query tables per sheet -- the sheet's own table ceiling -- and
# 16384 fields per refresh, the grid's own column ceiling; widen only if a real
# workbook is refused by one of them.
_MAX_XLSX_QUERY_TABLES: Final = 256
_MAX_XLSX_QUERY_TABLE_FIELDS: Final = 16_384
# ``ST_GrowShrinkType``: what the saved query says would happen to the range
# around it if a refresh ever returned a different number of rows. Closed, so a
# type outside the three is refused rather than read as whichever it resembles.
# Nothing here refreshes, so nothing is inserted, cleared or overwritten.
_XLSX_QUERY_GROW_SHRINK: Final = frozenset(
    {"insertDelete", "insertClear", "overwriteClear"}
)
# The query table's boolean attributes, read tri-state for the reason a
# connection's and a pivot cache's are: SpreadsheetML defaults each, but a
# default is not a declaration, and a refresh policy a consumer has to reason
# about is clearer when it says which of the two the file actually wrote.
#
# Every one is inert here. The first group is what Excel would do with the query
# and its results -- ``refreshOnLoad``, ``disableRefresh`` and
# ``removeDataOnSave`` describe its behaviour, not this reader's -- and the
# ``apply*Formats`` group is formatting policy for a refresh that never happens.
_XLSX_QUERY_TABLE_FLAGS: Final = (
    "headers",
    "rowNumbers",
    "disableRefresh",
    "backgroundRefresh",
    "firstBackgroundRefresh",
    "refreshOnLoad",
    "fillFormulas",
    "removeDataOnSave",
    "disableEdit",
    "preserveFormatting",
    "adjustColumnWidth",
    "intermediate",
    "applyNumberFormats",
    "applyBorderFormats",
    "applyFontFormats",
    "applyPatternFormats",
    "applyAlignmentFormats",
    "applyWidthHeightFormats",
)
# The field lists one saved refresh declares, beside the member counted inside
# each and the field its summary is reported in. Only the counts come out: a
# field's name is the query's own column header and a deleted field's is what
# the query returned before, so both are counted and neither is read.
_XLSX_QUERY_TABLE_GROUPS: Final = (
    ("refreshFieldCount", "queryTableFields", "queryTableField"),
    ("refreshDeletedFieldCount", "queryTableDeletedFields", "deletedField"),
)
# The extension entries one owner's own ``extLst`` may declare. Counted before a
# single record is built, and nothing inside an entry is ever walked, so the
# bound is the whole of what limits the inventory.
# ponytail: 256 entries per owner, the ceiling the sheet's tables share; widen
# only if a real workbook is refused by it.
_MAX_XLSX_EXTENSIONS: Final = 256
# The same count taken across every list one part holds, because a part with
# many owners (a table's columns, a pivot's fields) can hold one list apiece.
# ponytail: 4096 entries per part; widen only if a real workbook is refused.
_MAX_XLSX_PART_EXTENSIONS: Final = 4096
# Where each reached part kind may carry an ``extLst``, and so the only places a
# coverage entry is read from. A kind is (namespaces its root may be written in,
# owner paths): a path is the local names from the root down to an owner, and
# the empty path is the root itself. Every step is a direct child qualified in
# the root's own namespace; nothing else is visited, and no ``ext`` is entered.
#
# The paths are the Open XML SDK's SpreadsheetML schema model, checked against
# the parent inventory of Microsoft's ``Spreadsheet.ExtensionList`` page: every
# parent that page names which a part this reader opens can hold is a path
# below, and a path is a parent the schema places there. The parents it names
# that no opened part can hold -- ``bk``, ``rcc``, ``rdn``, ``rfmt``, ``rsnm``,
# ``header``, ``userInfo`` (revision parts), ``calcChain``, ``metadata`` and
# ``futureMetadata``, ``volTypes``, ``singleXmlCell``, ``xmlCellPr``, ``xmlPr``
# and the ``nc``/``oc``/``ndxf``/``odxf`` revision formats -- belong to parts
# the reader never opens. Only parts the reader opens are listed, and only the
# ones written in SpreadsheetML: a drawing and a chart keep their lists in
# DrawingML, so each has its own vocabulary in
# ``_XLSX_DRAWING_EXTENSION_VOCABULARY``.
#
# ``ignoredErrors`` is the owner, not its ``ignoredError`` members, and a
# worksheet's ``controls`` hold none. A sheet's ``pivotArea`` chain is reached
# below ``sheetView`` through ``pivotSelection``; the same ``autoFilter`` chain
# (the filter, its ``filterColumn`` and its ``sortState``) recurs under a table,
# a custom sheet view and a pivot table's ``filter``.


def _area(*prefix: str) -> tuple[tuple[str, ...], ...]:
    """A ``pivotArea`` owner below ``prefix`` and the ``reference``s inside it."""
    area = (*prefix, "pivotArea")
    return (area, (*area, "references", "reference"))


def _filter(*prefix: str) -> tuple[tuple[str, ...], ...]:
    """An ``autoFilter`` owner below ``prefix``, its columns and its sort state."""
    chain = (*prefix, "autoFilter")
    return (chain, (*chain, "filterColumn"), (*chain, "sortState"))


_XLSX_SHEET_VIEWS: Final = (
    ("sheetViews",),
    ("sheetViews", "sheetView"),
    *_area("sheetViews", "sheetView", "pivotSelection"),
)
_XLSX_CUSTOM_SHEET_VIEWS: Final = (
    ("customSheetViews", "customSheetView"),
    *_filter("customSheetViews", "customSheetView"),
)
_XLSX_EXTENSION_VOCABULARY: Final = {
    "workbook": (
        _SPREADSHEETML,
        (
            (),
            ("bookViews", "workbookView"),
            ("customWorkbookViews", "customWorkbookView"),
        ),
    ),
    "worksheet": (
        _SPREADSHEETML,
        (
            (),
            *_XLSX_SHEET_VIEWS,
            ("sheetData", "row"),
            ("sheetData", "row", "c"),
            *_filter(),
            ("sortState",),
            *_XLSX_CUSTOM_SHEET_VIEWS,
            ("conditionalFormatting",),
            ("conditionalFormatting", "cfRule"),
            ("conditionalFormatting", "cfRule", "colorScale", "cfvo"),
            ("conditionalFormatting", "cfRule", "dataBar", "cfvo"),
            ("conditionalFormatting", "cfRule", "iconSet", "cfvo"),
            ("ignoredErrors",),
        ),
    ),
    # A chart sheet and a dialog sheet are sheets the workbook may declare and
    # this reader opens like any other; their own roots and views are the
    # schema's, and a dialog sheet shares the worksheet's custom views.
    "chartsheet": (
        _SPREADSHEETML,
        ((), ("sheetViews",), ("sheetViews", "sheetView")),
    ),
    "dialogsheet": (
        _SPREADSHEETML,
        ((), *_XLSX_SHEET_VIEWS, *_XLSX_CUSTOM_SHEET_VIEWS),
    ),
    # ``CT_Sst`` holds its list directly; ``si`` and its runs hold none.
    "sharedStrings": (_SPREADSHEETML, ((),)),
    # ``CT_Connections`` has no list of its own; each connection has one.
    "connections": (_SPREADSHEETML, (("connection",),)),
    "externalLink": (_SPREADSHEETML, ((),)),
    "pivotCacheDefinition": (
        _SPREADSHEETML,
        (
            (),
            ("cacheSource",),
            ("cacheFields", "cacheField"),
            ("cacheHierarchies", "cacheHierarchy"),
            ("cacheHierarchies", "cacheHierarchy", "groupLevels", "groupLevel"),
            ("tupleCache",),
            ("calculatedItems", "calculatedItem"),
            *_area("calculatedItems", "calculatedItem"),
            ("calculatedMembers", "calculatedMember"),
        ),
    ),
    "pivotCacheRecords": (_SPREADSHEETML, ((),)),
    "pivotTable": (
        _SPREADSHEETML,
        (
            (),
            ("pivotFields", "pivotField"),
            *_area("pivotFields", "pivotField", "autoSortScope"),
            ("pageFields", "pageField"),
            ("dataFields", "dataField"),
            ("formats", "format"),
            *_area("formats", "format"),
            ("conditionalFormats", "conditionalFormat"),
            *_area("conditionalFormats", "conditionalFormat", "pivotAreas"),
            *_area("chartFormats", "chartFormat"),
            ("pivotHierarchies", "pivotHierarchy"),
            ("filters", "filter"),
            *_filter("filters", "filter"),
        ),
    ),
    "queryTable": (
        _SPREADSHEETML,
        (
            (),
            ("queryTableRefresh",),
            ("queryTableRefresh", "queryTableFields", "queryTableField"),
            ("queryTableRefresh", "sortState"),
        ),
    ),
    "table": (
        _SPREADSHEETML,
        (
            (),
            *_filter(),
            ("sortState",),
            ("tableColumns", "tableColumn"),
            ("tableColumns", "tableColumn", "xmlColumnPr"),
        ),
    ),
    "styles": (
        _SPREADSHEETML,
        (
            (),
            ("cellStyleXfs", "xf"),
            ("cellXfs", "xf"),
            ("cellStyles", "cellStyle"),
            ("dxfs", "dxf"),
        ),
    ),
    "comments": (_SPREADSHEETML, ((),)),
    "threadedComments": (frozenset({_XLSX_THREADED_MAIN}), ((), ("threadedComment",))),
    "persons": (frozenset({_XLSX_THREADED_MAIN}), ((), ("person",))),
}
# The threaded parts carry an ``xltc:extLst``, but the schema types it
# ``x:CT_ExtensionList``, so its entries are SpreadsheetML ``x:ext`` and not
# ``xltc:ext``. [MS-XLSX] binds ``x`` to the Transitional main namespace only,
# so no Strict spelling is taken here. Every other kind has its entries in the
# root's own namespace.
_XLSX_EXTENSION_ENTRY_NAMESPACES: Final = {
    "threadedComments": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "persons": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}
# The one form of extension URI that is kept: the braced GUID Office writes. Any
# other URI is a producer's free text and may carry a URL, a query string or a
# token, so it is reported as present and never copied.
_XLSX_EXTENSION_GUID: Final = re.compile(
    r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}"
)
# The relationship and content types that together qualify the workbook's own
# styles part, the one part the cell-format table lives in. As with every other
# type here, only the two URIs OOXML defines qualify -- Transitional and Strict
# -- and the content type is the same under both. Compared against
# ``declared_type``, which case-folds; the authored spelling is already folded.
_XLSX_STYLES_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/styles",
    }
)
_XLSX_STYLES_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
)
# The same pair for the shared strings, which the reader reads by its fixed name
# and whose extension list is covered only when the workbook declares that very
# member under both.
_XLSX_SHARED_STRINGS_RELATIONSHIPS: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/sharedStrings",
    }
)
# Written case-folded, as ``declared_type`` returns it.
_XLSX_SHARED_STRINGS_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedstrings+xml"
)
# The qualified spellings of the element the workbook's date system is declared
# on. ``xl/workbook.xml`` is reached by name rather than by a declared type, so
# there is no root check behind it and the namespace is the whole of what
# separates the declaration from a lookalike: an unqualified or foreign
# ``workbookPr`` declares nothing about which epoch the serials are counted
# from, and must never override or stand in for the qualified one.
_XLSX_WORKBOOK_PROPERTIES: Final = frozenset(
    f"{{{namespace}}}workbookPr" for namespace in _SPREADSHEETML
)
# One styles part's own bounds. The cell-format list is the work the part asks
# for whether or not any sheet cites an entry, so it is bounded as it is read;
# a format code is the one string kept from the part, so it is bounded on its
# own. Excel's own ceilings are about 65k cell formats, a few hundred custom
# formats and 255 characters of format code.
# ponytail: 65k cell formats, 4k custom formats and 256 characters of code;
# widen only if a real workbook is refused by these.
_MAX_XLSX_CELL_FORMATS: Final = 65_536
_MAX_XLSX_NUMBER_FORMATS: Final = 4_096
_MAX_XLSX_FORMAT_CODE_CHARS: Final = _MAX_META_CHARS
# The implied number formats whose meaning is a date, a time or an elapsed
# duration whatever locale reads the workbook, as OOXML spells them: ids 14-17
# carry a day, month or year, 18-21 and 45 and 47 carry a clock or a
# minute-and-second count, 22 carries both, and 46 is the one implied format
# written as an elapsed hour.
#
# Every other implied id is left out deliberately rather than by omission. The
# numeric and text ones -- General, the decimals, the percentages, the
# scientific and fraction forms, the accounting group and ``@`` -- hold no date
# at all, and ids 27-36 and 50-58 are the reserved locale-dependent block,
# whose format code is whatever the reading locale supplies: in some it is a
# date and in others it is not, so a serial under one of them is not
# demonstrably temporal and is not claimed to be.
_XLSX_BUILTIN_TEMPORAL_FORMATS: Final = {
    14: "date",
    15: "date",
    16: "date",
    17: "date",
    18: "time",
    19: "time",
    20: "time",
    21: "time",
    22: "datetime",
    45: "time",
    46: "elapsed_duration",
    47: "time",
}
# One bracketed elapsed unit, matched against already-lowered text. An elapsed
# bracket holds a run of a single unit letter and nothing else, so a mixed or
# decorated bracket -- a colour, a condition, a locale or a calendar directive,
# which is what nearly every bracket in a real format code is -- is not one.
_XLSX_ELAPSED_UNIT: Final = re.compile(r"h+|m+|s+")
# The two clock markers that may appear unquoted in a format code. They are
# recognised so that their letters are not read as a day, a month or a second,
# and they are evidence of nothing on their own: the hour they qualify is what
# carries the time.
_XLSX_CLOCK_MARKERS: Final = ("am/pm", "a/p")
# The whole finite part of the ``xsd:double`` lexical space a stored number is
# written in -- an integer, a decimal with or without a leading digit, or
# either carrying an exponent -- in Excel's own spelling and in anything else
# conformant. Neither the digits nor the exponent are bounded here, because the
# type does not bound them lexically: a conformant writer may spell any number
# of digits and any exponent, and a literal naming a magnitude no double can
# hold is a number out of range rather than text that is not a number, so it is
# converted and classified below instead of being refused as malformed. The
# same lexical space serves both OOXML profiles: Strict narrows which elements
# and attributes may appear, never how a double is spelled.
#
# Each repetition here is anchored on the literal that can follow it, so no two
# of them can claim the same character and a failing literal is refused in one
# pass over it. Spelling the integral form as digits and an optional
# point-and-digits group rather than as digits, an optional point and further
# digits is what buys that: the latter accepts the same strings, but leaves the
# split between its two digit runs free, so a long run of digits ending in a
# character the space does not have is retried once per split.
_XLSX_NUMBER: Final = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?"
)
# The four spellings in that lexical space which are not finite numbers. They
# are reported as the non-finite values they are rather than as text that
# failed to parse, because the file did write a double; it just wrote one no
# consumer can hold, count or serialise. XSD 1.1 also spells positive infinity
# ``+INF``.
_XLSX_NONFINITE: Final = frozenset({"INF", "+INF", "-INF", "NaN"})
# Past 2**53 an integer no longer round-trips through the double the file
# stored it in, nor through JSON's own number, so it is kept as the float it
# already was rather than reported as an exact integer it is not.
_MAX_EXACT_INTEGER: Final = 2**53
# A literal longer than this cannot name an integer inside that range, so the
# exact-integer conversion is never offered one -- which is also what keeps the
# decode resource-safe without bounding the lexical space, because ``int()`` is
# the one conversion whose cost grows with the digits and the one CPython
# refuses outright past 4300 of them. ``float()`` of any length is linear and
# cannot raise on text this expression already matched.
_MAX_EXACT_INTEGER_DIGITS: Final = len(str(_MAX_EXACT_INTEGER))
# The format kinds whose serial names a point in time, which are the only ones
# a serial is decoded under. ``elapsed_duration`` is deliberately absent: a
# count of hours is not a date, and converting one would invent an epoch its
# author never wrote.
_XLSX_TEMPORAL_KINDS: Final = frozenset({"date", "time", "datetime"})
# The day each date system counts from, as the day *before* its serial 1 --
# except that the 1900 system is written one day earlier than that, because
# Excel counts a 29th of February 1900 which never existed. Serials up to 59
# are the days the two readings disagree over, so they are shifted back onto
# the real calendar; serial 60 is the fictional day itself and is never put
# through this arithmetic; serial 61 onwards lands on the real date because the
# phantom day has by then been counted.
#
# The 1904 system counts its own serial 0 as the 1st of January 1904, so it
# needs no shift and has no phantom day: it is the epoch itself.
_XLSX_EPOCHS: Final = {
    "1900": datetime.date(1899, 12, 30),
    "1904": datetime.date(1904, 1, 1),
}
# The largest whole serial each system can name a date for. Derived from the
# last date the standard library can hold rather than written out, because that
# is the bound the conversion below actually has -- and for the 1900 system it
# is also the 2958465 that OOXML names as the upper limit of the serial space.
# Checked before any exact arithmetic is attempted, so a serial naming a
# magnitude far past the calendar costs one comparison rather than a conversion.
_XLSX_MAX_SERIAL: Final = {
    system: (datetime.date.max - epoch).days for system, epoch in _XLSX_EPOCHS.items()
}
# The serial the 1900 system reads as the 29th of February 1900, and the
# literal it is reported as. The date does not exist, so it is written out
# rather than computed: no Gregorian calendar can produce it, and coercing it to
# the 28th or the 1st of March would silently move every value stored under it.
_XLSX_PHANTOM_SERIAL: Final = 60
_XLSX_PHANTOM_DATE: Final = "1900-02-29"
# A day in microseconds, which is the resolution the decoded clock is reported
# at: the time of day is stored as a fraction of a day, so a fraction is scaled
# by this and rounded to the nearest microsecond. That is the finest resolution
# an ISO local time can carry here, and rounding to it is what lets the doubles
# a writer actually stores for a whole minute -- never exactly the fraction the
# minute is -- report as that whole minute.
_MICROSECONDS_PER_DAY: Final = 24 * 60 * 60 * 1_000_000
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
# The objects a spreadsheet-drawing anchor may hold, every one of them read as
# inventory. ``contentPart`` is one of them here, unlike in a slide's shape
# tree: it carries no identity and no z-order, but it is still an object the
# drawing anchored, and a sheet that shows one shows something.
_XLSX_DRAWING_OBJECTS: Final = frozenset(
    {"sp", "grpSp", "graphicFrame", "cxnSp", "pic", "contentPart"}
)
# The ``cNvPr`` claims a drawing object's author made about it, each beside the
# field the inventory record keeps it in. The identity comes from the same
# element as the words, which is why they are read together; the three
# descriptive claims are the picture reader's own, kept distinct for its
# reasons.
_XLSX_OBJECT_CLAIMS: Final = (("id", "shapeId"), *_PICTURE_CLAIMS)
# The one element each object kind holds its ``cNvPr`` in, named after the kind
# itself as the spreadsheet-drawing schema writes it. ``contentPart`` has no
# non-visual-properties holder of its own.
_XLSX_NON_VISUAL: Final = {
    "sp": "nvSpPr",
    "grpSp": "nvGrpSpPr",
    "graphicFrame": "nvGraphicFramePr",
    "cxnSp": "nvCxnSpPr",
    "pic": "nvPicPr",
}
# One drawing's second bound: the objects its anchors hold between them. The
# anchor bound alone leaves malformed input unbounded because one anchor can
# physically contain many object elements even though the grammar permits one.
_MAX_XLSX_DRAWING_OBJECTS: Final = 4_096
# The two elements a connector names its endpoints on, in the two DrawingML main
# namespaces OOXML defines, each beside the fields its claims are kept in. A
# lookalike in another vocabulary shares a local name and names no endpoint.
_DRAWING_MAIN: Final = (
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
)
_DRAWING_START_CONNECTION: Final = frozenset(
    f"{{{namespace}}}stCxn" for namespace in _DRAWING_MAIN
)
_DRAWING_END_CONNECTION: Final = frozenset(
    f"{{{namespace}}}endCxn" for namespace in _DRAWING_MAIN
)
_XLSX_CONNECTION_ENDS: Final = (
    (_DRAWING_START_CONNECTION, "connectionStartId", "connectionStartIndex"),
    (_DRAWING_END_CONNECTION, "connectionEndId", "connectionEndIndex"),
)
# What a connector records about the two ends it declares, defaulted so every
# connector's record carries the same keys whether it named both ends, one or
# none. ``None`` says the connector declared that end on nothing, which is not
# the same as an end attached to shape ``0`` at site ``0``.
_XLSX_CONNECTOR: Final[dict[str, object]] = {
    "connectionStartId": None,
    "connectionStartIndex": None,
    "connectionEndId": None,
    "connectionEndIndex": None,
}
# The namespace Excel's own 2010 drawing extensions are written in, and the one
# place it is read: the content part a spreadsheet drawing may anchor is typed
# from it, so the extension list that object carries is its, not OOXML's. There
# is no Strict spelling of a vocabulary OOXML never defined.
_XLSX_DRAWING_2010: Final = (
    "http://schemas.microsoft.com/office/excel/2010/spreadsheetDrawing"
)
# The elements of the DrawingML chart vocabulary the schema gives an extension
# list of that same vocabulary. ``c:spPr`` and ``c:clrMapOvr`` are chart
# elements too but are typed from DrawingML main, so their lists are that
# vocabulary's and they are named apart, below.
_XLSX_CHART_EXTENSION_OWNERS: Final = (
    "area3DChart",
    "areaChart",
    "backWall",
    "bar3DChart",
    "barChart",
    "bubbleChart",
    "catAx",
    "chart",
    "chartSpace",
    "dLbl",
    "dLbls",
    "dPt",
    "dTable",
    "dateAx",
    "dispUnits",
    "doughnutChart",
    "errBars",
    "floor",
    "layout",
    "legend",
    "legendEntry",
    "line3DChart",
    "lineChart",
    "manualLayout",
    "marker",
    "multiLvlStrCache",
    "multiLvlStrRef",
    "numCache",
    "numLit",
    "numRef",
    "ofPieChart",
    "pie3DChart",
    "pieChart",
    "pivotFmt",
    "pivotSource",
    "plotArea",
    "radarChart",
    "scaling",
    "scatterChart",
    "ser",
    "serAx",
    "sideWall",
    "stockChart",
    "strCache",
    "strLit",
    "strRef",
    "surface3DChart",
    "surfaceChart",
    "title",
    "trendline",
    "trendlineLbl",
    "upDownBars",
    "valAx",
    "view3D",
)
_DRAWING_MAIN_EXTENSION_OWNERS: Final = frozenset(
    (
        "audioCd",
        "audioFile",
        "backdrop",
        "blip",
        "bodyPr",
        "bodyStyle",
        "cell3D",
        "clrMap",
        "clrMapOvr",
        "clrScheme",
        "cNvCnPr",
        "cNvCxnSpPr",
        "cNvFrPr",
        "cNvGraphicFramePr",
        "cNvGrpSpPr",
        "cNvPicPr",
        "cNvPr",
        "cNvSpPr",
        "cxnSp",
        "cxnSpLocks",
        "defaultTextStyle",
        "defPPr",
        "defRPr",
        "docPr",
        "endParaRPr",
        "font",
        "fontScheme",
        "graphicFrame",
        "graphicFrameLocks",
        "gridCol",
        "grpSp",
        "grpSpLocks",
        "grpSpPr",
        "hiddenLine",
        "hiddenScene3d",
        "hiddenSp3d",
        "hlinkClick",
        "hlinkHover",
        "hlinkMouseOver",
        "ln",
        "lnB",
        "lnBlToTr",
        "lnDef",
        "lnL",
        "lnR",
        "lnT",
        "lnTlToBr",
        "lockedCanvas",
        "lstStyle",
        "lvl1pPr",
        "lvl2pPr",
        "lvl3pPr",
        "lvl4pPr",
        "lvl5pPr",
        "lvl6pPr",
        "lvl7pPr",
        "lvl8pPr",
        "lvl9pPr",
        "majorFont",
        "minorFont",
        "notesStyle",
        "objectDefaults",
        "otherStyle",
        "overrideClrMapping",
        "pic",
        "picLocks",
        "pPr",
        "quickTimeFile",
        "rPr",
        "scene3d",
        "snapshot",
        "sp",
        "sp3d",
        "spDef",
        "spLocks",
        "spPr",
        "tableStyle",
        "tblPr",
        "tblStyle",
        "tc",
        "tcBdr",
        "tcPr",
        "tcTxStyle",
        "theme",
        "themeElements",
        "titleStyle",
        "tr",
        "txDef",
        "txSp",
        "uLn",
        "videoFile",
    )
)
# Where each DrawingML part a worksheet reaches may carry an unsupported
# extension list, and so the only places a coverage entry is read from in one. A
# kind is (the namespaces its root may be written in, owner local name -> the
# namespaces that owner's ``extLst`` may be written in); ``None`` is the part's
# own validated namespace, and a frozenset is the vocabulary the schema types
# that owner's list from, which is another one wherever an element of the part's
# vocabulary is itself typed from DrawingML main -- most of a drawing, and two
# elements of a chart.
#
# Unlike the SpreadsheetML matrix above, these two vocabularies recurse: a group
# shape holds group shapes, a plot area holds charts holding series holding
# their own references. So an owner is named rather than placed, and the walk
# covers every position the schema puts that owner at. The structural path still
# says which of those positions a list was found at.
#
# The names are the Open XML SDK's schema models for the two part vocabularies.
# The walker also enters DrawingML main and uses the complete owner inventory
# above. It never enters a foreign vocabulary carried by ``a:graphicData`` --
# SmartArt and VML among them -- or any extension payload.
_XLSX_DRAWING_EXTENSION_VOCABULARY: Final[
    dict[str, tuple[frozenset[str], dict[str, frozenset[str] | None]]]
] = {
    "drawing": (
        _XLSX_DRAWING_NAMESPACES,
        {
            **dict.fromkeys(
                (
                    "cNvPr",
                    "cNvSpPr",
                    "cNvGrpSpPr",
                    "cNvPicPr",
                    "cNvCxnSpPr",
                    "cNvGraphicFramePr",
                    "spPr",
                    "grpSpPr",
                ),
                frozenset(_DRAWING_MAIN),
            ),
            "contentPart": frozenset({_XLSX_DRAWING_2010}),
        },
    ),
    "chart": (
        _CHART_NAMESPACES,
        {
            **dict.fromkeys(_XLSX_CHART_EXTENSION_OWNERS, None),
            **dict.fromkeys(("spPr", "clrMapOvr"), frozenset(_DRAWING_MAIN)),
        },
    ),
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
# The table-geometry elements, and the ``val`` that carries their one value,
# each by its qualified name in both profiles. A grid or a span is a structural
# claim about the table, so an element merely called ``tblGrid``, ``gridSpan``
# or ``vMerge`` in another vocabulary declares none of it, an unqualified or
# foreign ``val`` carries no value, and nothing is read off either.
_DOCX_TBL_GRID: Final = frozenset(
    f"{{{namespace}}}tblGrid" for namespace in _WORDPROCESSINGML
)
_DOCX_GRID_COL: Final = frozenset(
    f"{{{namespace}}}gridCol" for namespace in _WORDPROCESSINGML
)
_DOCX_TC_PR: Final = frozenset(
    f"{{{namespace}}}tcPr" for namespace in _WORDPROCESSINGML
)
_DOCX_GRID_SPAN: Final = frozenset(
    f"{{{namespace}}}gridSpan" for namespace in _WORDPROCESSINGML
)
_DOCX_V_MERGE: Final = frozenset(
    f"{{{namespace}}}vMerge" for namespace in _WORDPROCESSINGML
)
_DOCX_VAL: Final = tuple(f"{{{namespace}}}val" for namespace in _WORDPROCESSINGML)
_DOCX_WIDTH: Final = tuple(f"{{{namespace}}}w" for namespace in _WORDPROCESSINGML)
# ST_Merge: a vertical merge either restarts at this cell or continues the one
# the column above opened, and the element's own default is to continue.
_DOCX_MERGE_VALUES: Final = frozenset({"restart", "continue"})
# OmniVia's own practical bounds on one table's geometry, taken before any of
# its cells is read, so no single file drives an unbounded grid.
# ponytail: 256 columns and 10,000 rows per table; raise both together if a
# real source needs more.
_MAX_DOCX_TABLE_COLUMNS: Final = 256
_MAX_DOCX_TABLE_ROWS: Final = 10_000
_MAX_DOCX_COLUMN_TWIPS: Final = 1_000_000
# The section structure, each element by its qualified name in both profiles. A
# section break is a structural claim about the document, so a ``sectPr``,
# ``pPr``, ``p`` or ``body`` in another vocabulary declares none of it.
_DOCX_BODY: Final = frozenset(f"{{{namespace}}}body" for namespace in _WORDPROCESSINGML)
_DOCX_P: Final = frozenset(f"{{{namespace}}}p" for namespace in _WORDPROCESSINGML)
_DOCX_P_PR: Final = frozenset(f"{{{namespace}}}pPr" for namespace in _WORDPROCESSINGML)
_DOCX_SECT_PR: Final = frozenset(
    f"{{{namespace}}}sectPr" for namespace in _WORDPROCESSINGML
)
# The two references a section declares its page furniture by, mapped to the
# story each one names. Both are read only by their qualified names, so an
# element merely called ``headerReference`` elsewhere references nothing.
_DOCX_HDR_FTR_REFERENCES: Final[dict[str, str]] = {
    f"{{{namespace}}}{story}Reference": story
    for namespace in _WORDPROCESSINGML
    for story in ("header", "footer")
}
_DOCX_HDR_FTR_TYPE: Final = tuple(
    f"{{{namespace}}}type" for namespace in _WORDPROCESSINGML
)
# Each WordprocessingML profile and the one relationship-namespaced ``id`` that
# belongs with it, Transitional with Transitional and Strict with Strict. A
# reference's id is read only in its own profile, exactly as its ``type`` is: an
# id written in the other profile's relationships namespace mixes Transitional
# and Strict in a single declaration, so it names no relationship here.
_DOCX_HDR_FTR_ID: Final[dict[str, str]] = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main": (
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    ),
    "http://purl.oclc.org/ooxml/wordprocessingml/main": (
        "{http://purl.oclc.org/ooxml/officeDocument/relationships}id"
    ),
}
# ``ST_HdrFtr`` is a closed enumeration of exactly three kinds, and ``w:type``
# is required: a reference claiming any other kind, or none, places nothing.
_DOCX_HDR_FTR_KINDS: Final = frozenset({"default", "first", "even"})
# The relationship and content types that together qualify a header or a footer
# part. As everywhere else here, only the two URIs OOXML itself defines qualify
# -- Transitional and Strict -- so nothing is reached by ending the same way,
# and a header relationship never resolves to a footer part or the reverse.
_DOCX_HDR_FTR_RELATIONSHIPS: Final[dict[str, frozenset[str]]] = {
    "header": frozenset(
        {
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
            "http://purl.oclc.org/ooxml/officeDocument/relationships/header",
        }
    ),
    "footer": frozenset(
        {
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
            "http://purl.oclc.org/ooxml/officeDocument/relationships/footer",
        }
    ),
}
# The root element each resolved part has to be, by the story it was resolved
# as. A relationship and a content type say what Word wrote the part for, and
# the root says what the part is: a member reached as a header whose root is a
# footer, an unqualified ``hdr``, or any other element entirely, is not the
# story it was reached as, and none of its prose is read as that story's.
_DOCX_HDR_FTR_ROOTS: Final[dict[str, frozenset[str]]] = {
    story: frozenset(f"{{{namespace}}}{root}" for namespace in _WORDPROCESSINGML)
    for story, root in (("header", "hdr"), ("footer", "ftr"))
}
_DOCX_HDR_FTR_CONTENT_TYPES: Final[dict[str, str]] = {
    "header": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
    ),
    "footer": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
    ),
}
# OmniVia's own practical bound on how many sections one document declares,
# taken before any reference is resolved.
# ponytail: 10,000 sections per document; raise it if a real source needs more.
_MAX_DOCX_SECTIONS: Final = 10_000
# The classic comments story's own root, in both WordprocessingML profiles. A
# member is that story only where it is rooted here: Word names its
# ``commentsExtended`` and ``commentsIds`` parts ``comments...`` as well, and
# each holds another vocabulary entirely, so the file name qualifies nothing and
# none of their content is read as a comment.
_DOCX_COMMENTS: Final = frozenset(
    f"{{{namespace}}}comments" for namespace in _WORDPROCESSINGML
)
# Word's own extended-comment part, the one place a comment says whether it is a
# reply, which comment it replies to, and whether the thread was marked done. It
# is a Microsoft extension rather than part of either OOXML profile, so the
# relationship type, the content type and the vocabulary each have a single
# spelling and no Strict variant; the classic story they annotate stays
# Transitional-or-Strict and is read exactly as before.
#
# The part is reached only through the document's own relationships, by that one
# relationship type and the content type Word declares for it, so a member that
# merely looks like the part, an external target, and a part declared as
# anything else all reach nothing.
_DOCX_COMMENTS_EX_RELATIONSHIPS: Final = frozenset(
    {"http://schemas.microsoft.com/office/2011/relationships/commentsExtended"}
)
# Word declares the part as ``application/vnd.ms-word.commentsExtended+xml``.
# It is written folded here because a package's own declaration is folded when
# the content-types part is read, as every other content type in this module is.
_DOCX_COMMENTS_EX_CONTENT_TYPE: Final = "application/vnd.ms-word.commentsextended+xml"
_DOCX_W15: Final = "http://schemas.microsoft.com/office/word/2012/wordml"
_DOCX_COMMENTS_EX: Final = f"{{{_DOCX_W15}}}commentsEx"
_DOCX_COMMENT_EX: Final = f"{{{_DOCX_W15}}}commentEx"
_DOCX_COMMENT_EX_PARA_ID: Final = f"{{{_DOCX_W15}}}paraId"
_DOCX_COMMENT_EX_PARENT: Final = f"{{{_DOCX_W15}}}paraIdParent"
_DOCX_COMMENT_EX_DONE: Final = f"{{{_DOCX_W15}}}done"
# Every claim a record may make, each qualified. ``CT_CommentEx`` is an empty
# element carrying nothing but these three, so anything else written on one or
# below it is a claim this build cannot read rather than one it may ignore, and
# the record is refused instead of reported from the part of it that parsed.
# Namespace declarations are not attributes once the part is parsed, so a record
# carrying its own is held to the same three.
_DOCX_COMMENT_EX_ATTRIBUTES: Final = frozenset(
    {_DOCX_COMMENT_EX_PARA_ID, _DOCX_COMMENT_EX_PARENT, _DOCX_COMMENT_EX_DONE}
)
# The paragraph identity an extended record joins on. Word writes it on the
# paragraph itself, in the 2010 extension namespace rather than either OOXML
# profile, so an attribute merely called ``paraId`` identifies no paragraph.
_DOCX_PARA_ID: Final = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
# ``ST_LongHexNumber`` is four bytes written as eight hex digits, and MS-DOCX
# restricts a paragraph identity to ``0x00000001``..``0x7FFFFFFF``: zero and the
# high half are reserved, so neither identifies a paragraph and a value in
# either is refused rather than joined on.
_DOCX_PARA_ID_HEX: Final = re.compile(r"[0-9A-Fa-f]{8}")
_DOCX_PARA_ID_LIMIT: Final = 0x8000_0000
# OmniVia's own practical bound on the extended-comment list, and on how many
# paragraph identities one package's comments may declare, taken before any
# record is joined to a comment.
# ponytail: 10,000 of each; raise both together if a real source needs more.
_MAX_DOCX_COMMENT_EXTENSIONS: Final = 10_000
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
    #: Embedded rasters whose bytes were opened and could not be read. The
    #: structural extraction around them stands, so these degrade the document's
    #: outcome rather than refusing the source.
    failed_units: int = 0
    #: The aggregate budget a bounded embedded-image pass exhausted, if any.
    limit_breach: str | None = None


@dataclass
class _EmbeddedImages(EmbeddedImagePass):
    """The shared bounded embedded-raster pass, reading out of one package.

    The pass itself -- the gate, the one aggregate budget every container of the
    source shares, the outcome list and the per-image limitations -- is
    :class:`EmbeddedImagePass`. All this adds is where an OOXML candidate's
    bytes come from.
    """

    def member(
        self,
        package: _Package,
        part: object,
        media_type: object,
        locator: dict[str, object],
    ) -> EmbeddedImageResult:
        """Recognise the raster at ``part``, or say truthfully why nothing was.

        ``part`` and ``media_type`` are read straight out of the inventory
        record the container's own reader built, so a container that named only
        a reference out of the package -- a link, an external relationship, a
        picture naming no image at all -- carries no part here, and nothing
        opens, fetches or probes that reference. The gate is consulted before
        the member is read, so a profile that examines no embedded image never
        touches an image member, and a budget already breached stops before this
        candidate's member is read at all: the shared ceiling bounds the work
        done, not merely what is published.
        """
        if not isinstance(part, str) or not isinstance(media_type, str):
            return self.record(EMBEDDED_NOT_EMBEDDED)
        refused = self.refusal()
        if refused is not None:
            return refused
        try:
            content = package.bytes(part, limit=MAX_MEMBER_BYTES)
        except OOXMLInvalid:
            # The member is the package's own and was validated, so this is
            # an image too large for the image path rather than a package
            # fault: it is this image's outcome and not the document's.
            return self.record(EMBEDDED_MALFORMED)
        return self.read(content, media_type, part, locator)


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
        # Set by the XLSX reader alone: where the parts it reaches report the
        # extension lists they hold. Unset, nothing is collected.
        self.coverage: _XlsxCoverage | None = None
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


def extract_docx(
    content: bytes,
    *,
    capture_id: str,
    settings: ExtractionSettings | None = None,
    ocr_engine: OcrEngine = run_tesseract,
    budget: ExtractionBudget | None = None,
) -> OOXMLExtraction:
    """Read one admitted DOCX structurally, and only then its embedded rasters.

    The structural read is what it has always been and is the whole extraction
    by default: ``settings`` defaults to the explicit text-only profile, so a
    caller that hands none opens no image member at all.

    An embedded raster is examined only where the trusted settings say to and
    their pinned models were served. Every candidate of one document shares one
    aggregate execution budget, and a candidate that is refused, unsupported,
    malformed or cut short keeps the structural record it already had and
    reports exactly why nothing was read out of it.

    ``budget`` is that aggregate ceiling, injectable so a breach can be
    exercised with a small budget rather than a real allocation.
    """
    images = _EmbeddedImages.over(settings, ocr_engine=ocr_engine, budget=budget)
    settings = images.settings
    with _Package(content) as package:
        if "word/document.xml" not in package.names:
            raise OOXMLInvalid("DOCX main document part is missing")
        document = package.xml("word/document.xml")
        # Headers and footers are reached only through the document's own
        # sections and relationships, so a member that merely looks like one is
        # never read, and a part several sections share is read exactly once.
        furniture, section_count = _docx_header_footer_parts(package, document)
        stories = {
            name: _docx_story(name)
            for name in package.names
            if name.startswith("word/")
            and name.endswith(".xml")
            and posixpath.basename(name).startswith(
                ("footnotes", "endnotes", "comments")
            )
        }
        stories.update({part: story for part, (story, _) in furniture.items()})
        # Annotation parts are read first so every reference in any story can
        # be checked against the notes and comments that actually exist.
        roots = {
            part: package.xml(part)
            for part in sorted(stories)
            if stories[part] in _NOTE_TAGS
        }
        # A member named like the comments story holds it only where it is rooted
        # at the story's own element, so Word's own ``commentsExtended`` and
        # ``commentsIds`` parts are not read as classic comments and contribute
        # no prose, no annotation targets and no paragraph counts.
        for name in [
            part
            for part, root in roots.items()
            if stories[part] == "comment" and root.tag not in _DOCX_COMMENTS
        ]:
            del roots[name]
            del stories[name]
        parts = ["word/document.xml", *sorted(stories)]
        stories["word/document.xml"] = "body"
        styles = _docx_styles(package)
        notes = _annotation_index(roots)
        extensions = _docx_comment_extensions(package, roots)
        roots["word/document.xml"] = document
        seen: dict[tuple[str, str, str], int] = {}
        segments: list[ExtractedSegment] = []
        blocks = 0
        for part in parts:
            root = roots[part] if part in roots else package.xml(part)
            story = stories[part]
            if part in furniture and root.tag not in _DOCX_HDR_FTR_ROOTS[story]:
                raise OOXMLInvalid(f"DOCX {story} part is not a {story}")
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
                    extensions,
                    images,
                )
                if walker.inventory_note(container):
                    continue
                if part in furniture:
                    walker.inventory_applicability(furniture[part][1])
                walker.walk(container, "", 0)
                walker.finish()
                blocks += walker.paragraphs
        limitations: dict[str, object] = {
            "packageMembers": len(package.names),
            "externalReferences": package.external_relationships,
            "documentSections": section_count,
            **images.to_limitations(),
        }
        if images.limit_breach is not None:
            limitations["limitBreach"] = images.limit_breach
        return OOXMLExtraction(
            tuple(segments),
            "document_block",
            blocks,
            limitations,
            failed_units=images.failed_units,
            limit_breach=images.limit_breach,
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


def _docx_para_id(
    value: str | None, label: str, owner: str = "DOCX comment"
) -> str | None:
    """Validate one paragraph identity, keeping it exactly as written.

    A written value is held to ``ST_LongHexNumber`` and to the range MS-DOCX
    reserves for a paragraph identity, and is then kept as written -- never
    recased or renumbered -- so an identity stays the one the file declared and a
    reference to one stays the reference the file wrote. Joins compare case
    insensitively, because the digits are hex and the case of a hex digit is not
    part of the number.
    """
    if value is None:
        return None
    if (
        _DOCX_PARA_ID_HEX.fullmatch(value) is None
        or not 0 < int(value, 16) < _DOCX_PARA_ID_LIMIT
    ):
        raise OOXMLInvalid(f"{owner} {label} is malformed")
    return value


def _docx_comment_paragraphs(roots: dict[str, ElementTree.Element]) -> dict[str, str]:
    """Index every classic comment's paragraph identities by the id each claims.

    This is the join an extended record uses: Word identifies the comment a
    record annotates by the identity of one of the comment's own paragraphs, not
    by the comment's ``w:id`` and not by position in either list. Only
    paragraphs inside a classic comment are indexed, so a body, header, footnote
    or endnote paragraph carrying the same identity joins nothing.

    One identity claimed twice leaves every record naming it ambiguous, so the
    package is refused rather than joined under whichever comment came first. A
    comment without an id can be no target -- the walker has no identity to
    report it under -- so its paragraphs are indexed under no comment at all,
    though the work of reading them is work all the same: the bound counts every
    identity read, not just the ones an identified comment keeps.
    """
    owners: dict[str, str] = {}
    read = 0
    for part, root in roots.items():
        if _docx_story(part) != "comment":
            continue
        for entry in root:
            if _local(entry.tag) != "comment":
                continue
            note_id = _attr(entry, "id")
            for node in entry.iter():
                if node.tag not in _DOCX_P:
                    continue
                para_id = _docx_para_id(
                    node.attrib.get(_DOCX_PARA_ID), "paragraph identity"
                )
                if para_id is None:
                    continue
                if read >= _MAX_DOCX_COMMENT_EXTENSIONS:
                    raise OOXMLInvalid(
                        "DOCX comments declare too many paragraph identities"
                    )
                read += 1
                if para_id.casefold() in owners:
                    raise OOXMLInvalid("DOCX comment paragraph identity is repeated")
                if note_id is not None:
                    owners[para_id.casefold()] = note_id
    return owners


def _docx_comment_extensions(
    package: _Package, roots: dict[str, ElementTree.Element]
) -> dict[str, dict[str, object]]:
    """Return each classic comment's extended claims, by the comment's own id.

    The part is reached only through the document's own relationships, by the one
    relationship type Word defines for it and the content type it declares for
    the part, and it is read only where it is rooted at ``w15:commentsEx`` and
    holds nothing but ``w15:commentEx``. A member named ``commentsExtended.xml``
    that nothing relates to, a relationship of another type, a part declared as
    another content type and a part rooted at anything else therefore all
    contribute no metadata at all.

    Every record is a claim the file makes about its own comments. Nothing here
    invents a user, a thread root or a resolution: a record naming no parent is
    the thread root it says it is, a parent identity no comment claims is kept as
    the claim it is and resolved to nobody, and a thread the file never marked
    stays unmarked rather than folding into an unresolved one.

    A record has to name exactly one comment, so a record whose own identity no
    comment claims is refused rather than reported against a guess, as are two
    records for one comment -- whose parent claims nothing here can reconcile --
    a record parented to itself, and a chain of records that closes into a cycle.
    """
    related = _related_part(
        package,
        "word/document.xml",
        _DOCX_COMMENTS_EX_RELATIONSHIPS,
        _DOCX_COMMENTS_EX_CONTENT_TYPE,
        "extended comments",
        owner="document",
        label="DOCX",
    )
    if related is None:
        return {}
    _relation_id, part = related
    root = package.xml(part)
    if root.tag != _DOCX_COMMENTS_EX:
        raise OOXMLInvalid("DOCX extended comments part is not rooted at its own list")
    owners = _docx_comment_paragraphs(roots)
    # Every record by the folded identity it claims, each carrying the comment it
    # joined, its own identity as written, its parent claim and its done mark.
    claims: dict[str, tuple[str, str, str | None, bool | None]] = {}
    # The comments already annotated, so a second record for one of them is
    # refused before it is held rather than after one has already been kept.
    annotated: set[str] = set()
    for index, entry in enumerate(root):
        if entry.tag != _DOCX_COMMENT_EX:
            raise OOXMLInvalid(
                "DOCX extended comments part holds an unsupported element"
            )
        if not _DOCX_COMMENT_EX_ATTRIBUTES.issuperset(entry.attrib):
            raise OOXMLInvalid("DOCX extended comment holds an unsupported attribute")
        # The element is empty in the schema, so only the whitespace a pretty
        # printer leaves between records is content the record does not claim.
        if len(entry) or (entry.text or "").strip() or (entry.tail or "").strip():
            raise OOXMLInvalid("DOCX extended comment holds unsupported content")
        if index >= _MAX_DOCX_COMMENT_EXTENSIONS:
            raise OOXMLInvalid("DOCX declares too many extended comment records")
        para_id = _docx_para_id(
            entry.attrib.get(_DOCX_COMMENT_EX_PARA_ID),
            "paragraph identity",
            "DOCX extended comment",
        )
        if para_id is None:
            raise OOXMLInvalid("DOCX extended comment names no comment paragraph")
        note_id = owners.get(para_id.casefold())
        if note_id is None:
            raise OOXMLInvalid("DOCX extended comment names no comment")
        if para_id.casefold() in claims:
            raise OOXMLInvalid("DOCX extended comment paragraph identity is repeated")
        # One comment may claim several paragraph identities, so two records can
        # name one comment without repeating an identity. Their parent and done
        # claims are then two answers to one question, which nothing here can
        # reconcile, so the package is refused rather than annotated from one.
        if note_id in annotated:
            raise OOXMLInvalid("DOCX comment is annotated by more than one record")
        annotated.add(note_id)
        parent = _docx_para_id(
            entry.attrib.get(_DOCX_COMMENT_EX_PARENT),
            "parent paragraph identity",
            "DOCX extended comment",
        )
        if parent is not None and parent.casefold() == para_id.casefold():
            raise OOXMLInvalid("DOCX extended comment replies to itself")
        claims[para_id.casefold()] = (
            note_id,
            para_id,
            parent,
            _docx_comment_done(entry),
        )
    _docx_comment_threads(claims)
    records: dict[str, dict[str, object]] = {}
    for note_id, para_id, parent, done in claims.values():
        # Every key is written whatever the record claimed, so a reader never
        # has to tell a claim the file omitted from one this build left out:
        # ``None`` is the absence, and a thread the file marked unresolved stays
        # the ``False`` it was marked. The parent is reported as an identity
        # only where a comment claims it -- a record may outlive the comment it
        # replied to, and naming a comment that is not there would invent a
        # thread root -- while the parent paragraph stays the claim as written.
        records[note_id] = {
            "commentParaId": para_id,
            "isReply": parent is not None,
            "parentParaId": parent,
            "parentNoteId": None if parent is None else owners.get(parent.casefold()),
            "resolved": done,
        }
    return records


def _docx_comment_done(entry: ElementTree.Element) -> bool | None:
    """Whether a thread was marked done, or ``None`` where it says nothing.

    The mark is a tri-state claim rather than a boolean: a thread the file never
    marked is not the same claim as one the file marked unresolved, so an absent
    attribute stays absent rather than folding into a false. ``ST_OnOff`` is
    closed, so a value outside it is refused rather than read as whichever of the
    two it resembles.
    """
    value = entry.attrib.get(_DOCX_COMMENT_EX_DONE)
    if value is None:
        return None
    if value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise OOXMLInvalid("DOCX extended comment resolution mark is malformed")


def _docx_comment_threads(
    claims: dict[str, tuple[str, str, str | None, bool | None]],
) -> None:
    """Refuse a reply chain that closes into a cycle.

    The walk is bounded by the records on it: each step moves to a parent the
    list declares, and a parent no record declares ends the walk, so nothing
    runs past the records the package holds. A chain that reaches a record it has
    already crossed has no root, and a thread without a root is one nothing can
    read an order from, so the package is refused rather than reported with an
    invented root.
    """
    for start in sorted(claims):
        crossed = {start}
        walk = start
        while (parent := claims[walk][2]) is not None:
            walk = parent.casefold()
            if walk not in claims:
                break
            if walk in crossed:
                raise OOXMLInvalid("DOCX extended comment replies form a cycle")
            crossed.add(walk)


def _docx_sections(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Return every section's own ``sectPr`` in source order.

    A section break lives in the properties of the paragraph that ends the
    section, and the body's own trailing ``sectPr`` describes the section after
    the last break, so both routes are sections and the order they appear in is
    the order they are numbered in. Each is read only where WordprocessingML
    places it -- a qualified ``sectPr`` directly below the qualified body, or
    below the qualified ``pPr`` of a qualified paragraph directly below it -- so
    one nested in a table cell, a text box or another vocabulary declares no
    section and is never read as one.
    """
    body = next((item for item in root if item.tag in _DOCX_BODY), None)
    if body is None:
        return []
    sections: list[ElementTree.Element] = []
    for block in body:
        if block.tag in _DOCX_SECT_PR:
            sections.append(block)
        elif block.tag in _DOCX_P:
            properties = next((item for item in block if item.tag in _DOCX_P_PR), None)
            if properties is not None:
                sections.extend(
                    item for item in properties if item.tag in _DOCX_SECT_PR
                )
    if len(sections) > _MAX_DOCX_SECTIONS:
        raise OOXMLInvalid("DOCX declares too many sections")
    return sections


def _docx_header_footer_parts(
    package: _Package, root: ElementTree.Element
) -> tuple[dict[str, tuple[str, tuple[dict[str, object], ...]]], int]:
    """Resolve the header and footer part every section applies, with its sections.

    Each section's own references are read first, then Word's inheritance is
    applied: a kind a section never declares keeps whatever the section before
    it left in force, and a kind no section has declared yet applies nowhere.
    An inherited reference is the same relationship to the same part, so it is
    recorded as the relationship it is and flagged as inherited rather than
    re-derived.

    The result maps each part to its story and to every ``(section, kind)`` it
    applies at, so a part several sections share is read exactly once and
    carries all of its applicability. Only a part a reference actually resolves
    to is in the map, so a member that merely looks like a header or a footer is
    never read. The count beside it is how many sections the document declares,
    which stands whether or not any of them references anything.
    """
    resolved: dict[str, tuple[str, list[dict[str, object]]]] = {}
    # (story, kind) -> the (relationship id, part) the sections so far left in
    # force, which is what a section omitting that kind inherits.
    applied: dict[tuple[str, str], tuple[str, str]] = {}
    sections = _docx_sections(root)
    for ordinal, section in enumerate(sections):
        declared: dict[tuple[str, str], tuple[str, str]] = {}
        for element in section:
            story = _DOCX_HDR_FTR_REFERENCES.get(element.tag)
            if story is None:
                continue
            key = (story, _docx_header_footer_kind(element))
            if key in declared:
                raise OOXMLInvalid("DOCX section references one kind twice")
            relationship_id = _docx_header_footer_id(element)
            declared[key] = (
                relationship_id,
                _referenced_part(
                    package,
                    "word/document.xml",
                    relationship_id,
                    _DOCX_HDR_FTR_RELATIONSHIPS[story],
                    _DOCX_HDR_FTR_CONTENT_TYPES[story],
                    story,
                    "DOCX",
                ),
            )
        applied.update(declared)
        for (story, kind), (relationship_id, part) in sorted(applied.items()):
            resolved.setdefault(part, (story, []))[1].append(
                {
                    "section": ordinal,
                    "kind": kind,
                    "relationshipId": relationship_id,
                    "inherited": (story, kind) not in declared,
                }
            )
    return {
        part: (story, tuple(applies)) for part, (story, applies) in resolved.items()
    }, len(sections)


def _docx_header_footer_id(element: ElementTree.Element) -> str:
    """Read the relationship id one header or footer reference names.

    ``r:id`` is required and names exactly one relationship, and it is read only
    in the reference's own profile: the WordprocessingML namespace the reference
    element is written in settles which relationships namespace carries its id,
    so a Transitional reference takes a Transitional id and a Strict one takes a
    Strict id. An id written in the other profile, in no namespace, or in a
    foreign one is not the attribute at all, and a reference carrying both
    profiles' ids names two relationships rather than one; none of them places
    the reference anywhere.
    """
    namespace = element.tag.partition("}")[0].lstrip("{")
    relationship_id = element.attrib.get(_DOCX_HDR_FTR_ID[namespace])
    if relationship_id is None or len(_namespaced_ids(element, "id")) != 1:
        raise OOXMLInvalid("DOCX header or footer reference is malformed")
    return relationship_id


def _docx_header_footer_kind(element: ElementTree.Element) -> str:
    """Read the kind one header or footer reference declares.

    ``w:type`` is required and carries one of exactly three kinds, and it is
    read only in the reference's own profile: a Transitional reference whose
    type is written in the Strict namespace mixes two profiles in one
    declaration, and an unqualified or foreign ``type`` is not the attribute at
    all, so neither places the reference anywhere.
    """
    if len([name for name in _DOCX_HDR_FTR_TYPE if name in element.attrib]) > 1:
        raise OOXMLInvalid("DOCX header or footer reference declares two kinds")
    namespace = element.tag.partition("}")[0].lstrip("{")
    kind = element.attrib.get(f"{{{namespace}}}type")
    if kind not in _DOCX_HDR_FTR_KINDS:
        raise OOXMLInvalid("DOCX header or footer reference is malformed")
    return kind


def extract_xlsx(
    content: bytes,
    *,
    capture_id: str,
    settings: ExtractionSettings | None = None,
    ocr_engine: OcrEngine = run_tesseract,
    budget: ExtractionBudget | None = None,
) -> OOXMLExtraction:
    """Read one admitted XLSX structurally, and only then its embedded rasters.

    The structural read is what it has always been and is the whole extraction
    by default: ``settings`` defaults to the explicit text-only profile, so a
    caller that hands none opens no image member at all.

    An embedded raster is examined only where the trusted settings say to and
    their pinned models were served, and only one a worksheet drawing's picture
    directly displays out of the package is a candidate. Every candidate of one
    workbook shares one aggregate execution budget, and a candidate that is
    refused, unsupported, malformed or cut short keeps the inventory record it
    already had and reports exactly why nothing was read out of it.

    ``budget`` is that aggregate ceiling, injectable so a breach can be
    exercised with a small budget rather than a real allocation.
    """
    images = _EmbeddedImages.over(settings, ocr_engine=ocr_engine, budget=budget)
    settings = images.settings
    with _Package(content) as package:
        if "xl/workbook.xml" not in package.names:
            raise OOXMLInvalid("XLSX workbook part is missing")
        # Where every part the reader reaches reports its extension lists. Set
        # before the first of them is read, so the workbook's own parts are
        # collected under no sheet and each sheet's under its own.
        coverage = package.coverage = _XlsxCoverage()
        shared = _shared_strings(package)
        workbook = package.xml("xl/workbook.xml")
        # The workbook's own lists are bounded here, with every other part's, and
        # built and reported last of all.
        workbook_lists = coverage.claim("workbook", workbook)
        sheets = _xlsx_sheets(package, workbook)
        # Resolved against the sheet list before any sheet is read, so a name
        # scoped to a sheet the workbook never declared is refused rather than
        # reported after a package's worth of work.
        defined_names = _xlsx_defined_names(workbook, sheets)
        # The person list a threaded comment cites is the workbook's own part,
        # not a sheet's, so it is resolved once and every sheet's threads cite
        # the same table.
        persons = _xlsx_persons(package)
        # The stored data connections, likewise the workbook's own part rather
        # than a sheet's, and resolved here for the reason the defined names
        # are: a malformed list is refused before a package's worth of work.
        # Nothing it names is opened and no refresh is performed. The index is
        # what lets a sheet's query table name the connection it cites.
        connections, declared_connections = _xlsx_connections(package)
        # The external links, likewise the workbook's own declarations, and
        # resolved here for the same reason: a malformed link is refused before
        # a package's worth of work. Nothing it names is opened -- no linked
        # workbook, no DDE service, no OLE server -- and no cached value a
        # linked part stored is copied out.
        external_links = _xlsx_external_links(package, workbook)
        # The pivot caches, likewise the workbook's own declarations, resolved
        # here for the same reason and before any sheet is read for one more:
        # the index they return is what lets a sheet's pivot table name the
        # cache it cites. Nothing is refreshed, no source is reached and no
        # cached record is read -- only what the saved definition declared.
        pivot_caches, caches = _xlsx_pivot_caches(package, workbook)
        pivot_tables = 0
        query_tables = 0
        extensions = 0
        # The cell-format table and the date system the workbook counts its
        # serials from: both are the workbook's own declarations rather than a
        # sheet's, so they are resolved once and every cell of every sheet
        # cites the same provenance. Nothing here applies a format or decodes a
        # serial; the record says what the file declared and no more.
        formats = _xlsx_formats(package, workbook)
        segments: list[ExtractedSegment] = []
        populated = 0
        # Where each sheet's own extension records belong, noted as the sheet is
        # read and filled in only once every part's lists have been counted --
        # a later sheet's can still take the global bound over its limit, and no
        # record is constructed before that is settled.
        deferred: list[
            tuple[
                int,
                str,
                str,
                str,
                str,
                str,
                list[tuple[str, list[ElementTree.Element]]],
            ]
        ] = []
        for ordinal, (sheet_name, sheet_id, part, state) in enumerate(sheets):
            coverage.scope = ordinal
            root = package.xml(part)
            _prefix, _, sheet_kind = root.tag.rpartition("}")
            sheet_lists = (
                coverage.claim(sheet_kind, root)
                if sheet_kind in _XLSX_SHEET_OWNERS
                else []
            )
            masters = _shared_masters(root)
            merged = _merged_ranges(root)
            hidden_columns = _hidden_columns(root)
            # Declared tables are resolved before any cell is read, so a cell's
            # locator can name the table it already sits in. Nothing here
            # materialises a cell a table's range covers but the sheet never
            # wrote: the range is an extent, like ``dimension``, not content.
            tables, covered = _xlsx_tables(package, part, root)
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
                    node.text or ""
                    for node in _outside_extensions(cell)
                    if _local(node.tag) == "t"
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
                # One dict, built once for the cell and carried by the ordinary,
                # formula and saved-cache records alike: the three describe one
                # cell, so none of them may say a different thing about the
                # format it authored. It rides in each record's own value
                # because it is provenance for the value the record retains --
                # which epoch a stored serial is counted from, and which format
                # the author wrote over it -- and a reader holding one record
                # has the whole of that provenance without resolving a locator.
                provenance = formats.provenance(cell)
                membership = merged.get(position)
                if membership is not None:
                    locator["mergedRange"], locator["mergedAnchor"] = membership
                place = _xlsx_table_place(covered, position)
                if place is not None:
                    locator.update(place)
                visibility = (
                    "hidden"
                    if sheet_hidden or row_hidden or column_hidden
                    else "visible"
                )
                cell_type = cell.attrib.get("t")
                # The decoding of whatever number the cell stored, built once
                # for the same reason the provenance is: the ordinary record
                # and a formula's saved cache hold the same stored value, so
                # neither may read it as a different number -- or as a number
                # at all where the other found none. It decodes the value a
                # formula last saved, never the formula: the expression, its
                # cache state and its freshness are untouched, nothing is
                # evaluated, and a cache is still never checked against the
                # expression that wrote it.
                numeric = _xlsx_numeric(cell_type, value)
                # The temporal reading of that same number, built once for the
                # same reason and merged after it: the cell's own format says
                # whether its serial names a point in time, so the ordinary
                # record and a formula's saved cache read it alike. A formula's
                # expression is not a stored number and keeps only the
                # provenance it already had.
                temporal = _xlsx_temporal(provenance, numeric, value)
                if formula is not None:
                    segments.append(
                        ExtractedSegment(
                            "formula",
                            locator,
                            structured_value=_xlsx_formula(formula, masters, value)
                            | provenance,
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
                                    **provenance,
                                    **numeric,
                                    **temporal,
                                },
                                visibility=visibility,
                            )
                        )
                else:
                    decoded = _xlsx_value(cell_type, value, inline, shared)
                    if "numericAnomaly" in numeric:
                        # The lexical value is not a number this cell can have
                        # stored, so the record decodes nothing rather than
                        # passing its text off as the number it failed to be.
                        decoded = None
                    segments.append(
                        ExtractedSegment(
                            "table_cell",
                            locator,
                            structured_value={
                                "raw": value,
                                "decoded": decoded,
                                "cellType": cell_type,
                                **provenance,
                                **numeric,
                                **temporal,
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
            for table in tables:
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        _locator(
                            capture_id,
                            part=part,
                            sheetId=sheet_id,
                            sheet=sheet_name,
                            cell=table["ref"],
                            sheetState=state,
                        ),
                        structured_value=table,
                        extraction_method="inventory",
                        visibility="hidden" if sheet_hidden else "visible",
                    )
                )
                _bounded(segments)
            # A chart's values live in the chart part the sheet's drawing
            # reached, so the sheet is provenance for them rather than their
            # place: ``sheetPart`` names it, as an XLSX comment's locator
            # already does. ``freshness`` is unknown because the values are the
            # copy the package stored and nothing here opens the workbook
            # behind them, resolves a source formula or recalculates anything.
            for value, provenance in _xlsx_charts(package, part, root):
                segments.append(
                    ExtractedSegment(
                        "chart",
                        _locator(
                            capture_id,
                            sheetPart=part,
                            sheetId=sheet_id,
                            sheet=sheet_name,
                            sheetState=state,
                            **provenance,
                            freshness="unknown",
                        ),
                        # A cache that stored an empty value is still a record
                        # the chart claims, so it is reported as the empty claim
                        # it is; ``document_intake`` takes exactly one content
                        # representation and refuses empty text.
                        text=value or None,
                        structured_value=None if value else {"value": ""},
                        visibility="hidden" if sheet_hidden else "visible",
                    )
                )
                _bounded(segments)
            # What the same drawing anchors beside its charts, as inventory: the
            # objects' own metadata and the references they encode, never their
            # content. ``sheetPart`` names the sheet for the reason a chart's
            # locator does -- the object lives in the drawing part, and the
            # worksheet is provenance for it -- and the drawing part, the anchor
            # and the object's own index are all named beside it, so a record
            # says which object on which anchor of which drawing it describes.
            #
            # A picture is the one object kind that displays a raster, so it is
            # the one kind handed to the bounded embedded-image pass. Everything
            # else the drawing anchored -- a chart, a shape, a graphic frame, a
            # content part, a connector, an OLE stand-in -- is never a candidate
            # and is reported exactly as before. What the pass read follows the
            # record it belongs to, as the separate reading it is, anchored to
            # that object's own locator rather than joined to any cell.
            for provenance, record in _xlsx_drawing_objects(package, part, root):
                locator = _locator(
                    capture_id,
                    sheetPart=part,
                    sheetId=sheet_id,
                    sheet=sheet_name,
                    sheetState=state,
                    **provenance,
                )
                visibility = "hidden" if sheet_hidden else "visible"
                read = (
                    images.member(
                        package,
                        record.get("imagePart"),
                        record.get("imageContentType"),
                        locator,
                    )
                    if provenance["drawingObjectType"] == "pic"
                    else None
                )
                if read is not None:
                    record["imageDecoded"] = read.decoded
                    record["ocrPerformed"] = read.performed
                    record["ocrOutcome"] = read.outcome
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        locator,
                        structured_value=record,
                        extraction_method="inventory",
                        visibility=visibility,
                    )
                )
                _bounded(segments)
                for reading in read.segments if read is not None else ():
                    segments.append(
                        # A reading keeps the role, method, language and
                        # confidence the image path gave it, so an accepted line
                        # stays searchable text read by OCR and a below-threshold
                        # observation stays out of ordinary retrieval. A hidden
                        # sheet hides what was read on it exactly as it hides the
                        # record above.
                        replace(reading, visibility=visibility)
                        if sheet_hidden
                        else reading
                    )
                    _bounded(segments)
            # The sheet's pivot tables, as inventory. A pivot table lives in its
            # own part and the sheet declares it in ``pivotTableParts``, so
            # ``sheetPart`` is provenance for it exactly as it is for a chart.
            # The report is reported, never rebuilt: no cached record is read,
            # no source is reached and nothing is refreshed or recalculated.
            for provenance, record in _xlsx_pivot_tables(package, part, root, caches):
                pivot_tables += 1
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        _locator(
                            capture_id,
                            sheetPart=part,
                            sheetId=sheet_id,
                            sheet=sheet_name,
                            sheetState=state,
                            **provenance,
                        ),
                        structured_value=record,
                        extraction_method="inventory",
                        visibility="hidden" if sheet_hidden else "visible",
                    )
                )
                _bounded(segments)
            # The query tables the sheet and its declared tables reach, as
            # inventory. A query table has no declaration in the sheet's own
            # markup -- the qualifying relationship is the declaration -- so
            # ``sheetPart`` is provenance for it as it is for a pivot table, and
            # a record reached through a declared table names that table beside
            # it. The saved state is reported, never run: no query is executed,
            # no connection or Power Query source is opened, and nothing is
            # refreshed or recalculated.
            for provenance, record in _xlsx_query_tables(
                package, part, tables, declared_connections
            ):
                query_tables += 1
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        _locator(
                            capture_id,
                            sheetPart=part,
                            sheetId=sheet_id,
                            sheet=sheet_name,
                            sheetState=state,
                            **provenance,
                        ),
                        structured_value=record,
                        extraction_method="inventory",
                        visibility="hidden" if sheet_hidden else "visible",
                    )
                )
                _bounded(segments)
            # The extension entries the sheet's own ``extLst`` declares, as
            # coverage, belong here: the reader understands none of them, so
            # each is a record that one exists and where, never a cell or any
            # content. Only the place is kept now.
            if sheet_lists:
                deferred.append(
                    (
                        len(segments),
                        sheet_id,
                        sheet_name,
                        state,
                        part,
                        sheet_kind,
                        sheet_lists,
                    )
                )
            # A comment is anchored to a reference, not to a cell: nothing here
            # requires the sheet to have written anything at that position, and
            # no cell is materialised for one.
            #
            # The classic list first, then the threaded one: each is its own
            # part reached on its own route, and a workbook carrying threads
            # normally carries a classic placeholder beside each of them, so
            # both forms are reported and each says which it is.
            for text, provenance in (
                *_xlsx_comments(package, part),
                *_xlsx_threaded_comments(package, part, persons),
            ):
                segments.append(
                    ExtractedSegment(
                        "comment",
                        _locator(
                            capture_id,
                            **provenance,
                            sheetPart=part,
                            sheetId=sheet_id,
                            sheet=sheet_name,
                            sheetState=state,
                        ),
                        # A comment written with no text is still a comment the
                        # file claims, so it is reported rather than dropped.
                        # ``document_intake`` takes exactly one content
                        # representation and refuses empty text, so the empty
                        # claim is carried as the value it is; nothing is
                        # invented to stand in for text the file never wrote.
                        text=text or None,
                        structured_value=None if text else {"text": ""},
                        # A comment is out of the grid's flow wherever it sits,
                        # including on a hidden sheet: the sheet's state is
                        # reported in the locator rather than folded into this.
                        visibility="annotation",
                    )
                )
                _bounded(segments)
        # Every part's lists are counted now, so the sheets' own extension records
        # are built and put where each was noted, last place first so the earlier
        # places stay valid.
        for at, sheet_id, sheet_name, state, part, sheet_kind, sheet_lists in reversed(
            deferred
        ):
            built = [
                ExtractedSegment(
                    "object_inventory",
                    _locator(
                        capture_id,
                        sheetId=sheet_id,
                        sheet=sheet_name,
                        sheetState=state,
                        **provenance,
                    ),
                    structured_value=record | {"scope": "sheet"},
                    extraction_method="inventory",
                    visibility="hidden" if state != "visible" else "visible",
                )
                for provenance, record in _xlsx_extension_records(
                    part, sheet_kind, sheet_lists
                )
            ]
            extensions += len(built)
            segments[at:at] = built
        _bounded(segments)
        # After every sheet's own content, in the order the workbook declared
        # them: a defined name is the workbook's declaration rather than a
        # sheet's, even where it scopes itself to one.
        for defined in defined_names:
            segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(
                        capture_id,
                        part="xl/workbook.xml",
                        definedName=defined["name"],
                        scope=defined["scope"],
                    ),
                    structured_value=defined,
                    extraction_method="inventory",
                    # A name is out of sight where it hides itself or where the
                    # sheet it is scoped to is out of sight; a workbook-scoped
                    # name has no sheet to take visibility from.
                    visibility=(
                        "hidden"
                        if defined["hidden"]
                        or defined["sheetState"] not in (None, "visible")
                        else "visible"
                    ),
                )
            )
            _bounded(segments)
        # Last, after every sheet's content and the workbook's own names: a
        # stored connection is inert inventory, never content. It is always in
        # sight -- it belongs to the workbook rather than to a sheet, so there
        # is no sheet state to take visibility from, and ``deleted`` is the
        # author's own lifecycle claim in the record rather than a reason to
        # hide one. Nothing it names was opened and nothing was refreshed.
        for provenance, connection in connections:
            segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(capture_id, **provenance),
                    structured_value=connection,
                    extraction_method="inventory",
                    visibility="visible",
                )
            )
            _bounded(segments)
        # Last of the workbook's own declarations, after the connections for the
        # same reason they come after the sheets: an external link is inert
        # inventory, never content. It is always in sight -- it belongs to the
        # workbook rather than to a sheet, so there is no sheet state to take
        # visibility from. Nothing it names was opened and nothing was
        # refreshed, and each record says so.
        for provenance, link in external_links:
            segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(capture_id, **provenance),
                    structured_value=link,
                    extraction_method="inventory",
                    visibility="visible",
                )
            )
            _bounded(segments)
        # Last of all, after the links for the reason they come after the
        # connections: a pivot cache is the workbook's own declaration and inert
        # inventory, never content. It is always in sight -- it belongs to the
        # workbook rather than to a sheet, so there is no sheet state to take
        # visibility from, and a table on a hidden sheet citing it is hidden
        # where the table is rather than hiding the cache behind it.
        for provenance, cache in pivot_caches:
            segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(capture_id, **provenance),
                    structured_value=cache,
                    extraction_method="inventory",
                    visibility="visible",
                )
            )
            _bounded(segments)
        # The extension entries of every other part the reader reached, once per
        # part and in the order it first reached them: coverage, never content.
        coverage.scope = None
        for reached_part, held in coverage.reached.items():
            owners = sorted(n for n in held.scopes if n is not None)
            where: dict[str, object] = {}
            if None in held.scopes:
                scope = "workbook"
            elif len(owners) == 1:
                scope = "sheet"
                name, identity, sheet_part, sheet_state = sheets[owners[0]]
                where = {
                    "sheetPart": sheet_part,
                    "sheetId": identity,
                    "sheet": name,
                    "sheetState": sheet_state,
                }
            else:
                scope = "shared"
            # A sheet's state is claimed only for a part that one sheet reached;
            # a shared part is hidden only where every sheet reaching it is, and
            # the workbook's own is never hidden.
            hidden = scope != "workbook" and all(
                sheets[n][3] != "visible" for n in owners
            )
            for provenance, record in _xlsx_extension_records(
                reached_part, held.owner, held.lists
            ):
                extensions += 1
                segments.append(
                    ExtractedSegment(
                        "object_inventory",
                        _locator(capture_id, **provenance, **where),
                        structured_value=record | {"scope": scope},
                        extraction_method="inventory",
                        visibility="hidden" if hidden else "visible",
                    )
                )
                _bounded(segments)
        # Last of all: the workbook's own ``extLst`` entries, coverage and not
        # content, for the reason a sheet's are. Always in sight, as the other
        # workbook-level inventories are.
        for provenance, record in _xlsx_extension_records(
            "xl/workbook.xml", "workbook", workbook_lists
        ):
            extensions += 1
            segments.append(
                ExtractedSegment(
                    "object_inventory",
                    _locator(capture_id, **provenance),
                    structured_value=record | {"scope": "workbook"},
                    extraction_method="inventory",
                    visibility="visible",
                )
            )
            _bounded(segments)
        limitations: dict[str, object] = {
            "sheets": len(sheets),
            "definedNames": len(defined_names),
            "connections": len(connections),
            # The caches the workbook declared and the pivot tables its
            # sheets related: two figures because a cache may serve several
            # tables and a workbook may keep one no table cites.
            "pivotCaches": len(pivot_caches),
            "pivotTables": pivot_tables,
            # The query tables the sheets and their declared tables related,
            # which is not the ``connections`` figure beside it: a stored
            # connection may serve several query tables, or none at all.
            "queryTables": query_tables,
            # The unsupported extension entries every part the reader opened
            # declared, at every owner the schema places a list at in that
            # part: the SpreadsheetML roots and the shared strings, and the
            # worksheet drawings and charts in their own DrawingML
            # vocabularies. Coverage records, counted apart from the cells.
            # A part the reader never opens is not counted. DrawingML-main
            # properties inside an opened drawing or chart are counted;
            # foreign graphics a frame merely carries are not entered.
            "extensions": extensions,
            # The links the workbook declared, which is not the
            # ``externalReferences`` figure beside it: that one counts every
            # external relationship any part of the package holds, this one
            # the external links the workbook itself declared.
            "externalLinks": len(external_links),
            "packageMembers": len(package.names),
            "externalReferences": package.external_relationships,
            **images.to_limitations(),
        }
        if images.limit_breach is not None:
            limitations["limitBreach"] = images.limit_breach
        return OOXMLExtraction(
            tuple(segments),
            "workbook_cell",
            populated,
            limitations,
            failed_units=images.failed_units,
            limit_breach=images.limit_breach,
        )


def extract_pptx(
    content: bytes,
    *,
    capture_id: str,
    settings: ExtractionSettings | None = None,
    ocr_engine: OcrEngine = run_tesseract,
    budget: ExtractionBudget | None = None,
) -> OOXMLExtraction:
    """Read one admitted PPTX structurally, and only then its embedded rasters.

    The structural read is what it has always been and is the whole extraction
    by default: ``settings`` defaults to the explicit text-only profile, so a
    caller that hands none opens no image member at all.

    An embedded raster is examined only where the trusted settings say to and
    their pinned models were served. Every candidate of one presentation shares
    one aggregate execution budget -- across every slide, not one slide at a
    time -- and a candidate that is refused, unsupported, malformed or cut short
    keeps the structural record it already had and reports exactly why nothing
    was read out of it. A picture a slide's own shape tree displays is the only
    candidate: a layout's or master's shapes are never read as pictures at all,
    and a link, a media reference, an embedded object, a chart, a diagram and
    every non-picture shape display no raster of the package's own.

    ``budget`` is that aggregate ceiling, injectable so a breach can be
    exercised with a small budget rather than a real allocation.
    """
    images = _EmbeddedImages.over(settings, ocr_engine=ocr_engine, budget=budget)
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
                capture_id, part, slide_id, ordinal, hidden, segments, package, images
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
        limitations: dict[str, object] = {
            "packageMembers": len(package.names),
            "externalReferences": package.external_relationships,
            **images.to_limitations(),
        }
        if images.limit_breach is not None:
            limitations["limitBreach"] = images.limit_breach
        return OOXMLExtraction(
            tuple(segments),
            "slide",
            len(slides),
            limitations,
            failed_units=images.failed_units,
            limit_breach=images.limit_breach,
        )


def _xlsx_sheets(
    package: _Package, root: ElementTree.Element
) -> list[tuple[str, str, str, str]]:
    """List (name, sheetId, part, visibility state) for each declared sheet.

    The state is kept as the workbook wrote it, so a very hidden sheet stays
    distinguishable from one a reader can unhide. The vocabulary is closed, so
    anything else is refused rather than read as one of them.
    """
    relations = package.relationships.get("xl/_rels/workbook.xml.rels", {})
    result: list[tuple[str, str, str, str]] = []
    for sheet in _outside_extensions(root):
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


def _xlsx_defined_names(
    root: ElementTree.Element, sheets: list[tuple[str, str, str, str]]
) -> list[dict[str, object]]:
    """Inventory the workbook's defined names without resolving any of them.

    A defined name is reached on the schema's own route and no other: the
    ``definedNames`` directly below the workbook and a ``definedName`` directly
    below that, both qualified in the workbook root's own namespace, and that
    namespace has to be SpreadsheetML. An unqualified, foreign or further
    nested lookalike declares nothing and stays transparent, so a workbook that
    writes no namespace reports no defined names rather than being read for
    whatever spells the same local name.

    What comes back is what the workbook already stored. The name is kept
    exactly as written, so a built-in name keeps its ``_xlnm.`` prefix and its
    own casing, and the expression is carried as the text it is: nothing here
    evaluates it, resolves what it names, follows an external workbook
    reference, or materialises a cell for any range it covers. The rest of the
    record is the inert metadata the declaration carries beside it.

    A local scope is a zero-based ordinal into the workbook's own sheet list,
    resolved to that sheet's identity, part and visibility state; an ordinal
    naming no sheet scopes the name to nothing, so it is refused. One scope
    names a name once however it is cased, so a repeat inside one scope leaves
    two expressions under one name and is refused -- but one name in two
    different local scopes is two names, and both are kept.
    """
    namespace, _, _ = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        return []
    declared = [node for node in root if node.tag == f"{{{namespace}}}definedNames"]
    if not declared:
        return []
    if len(declared) > 1:
        raise OOXMLInvalid("XLSX workbook declares more than one defined-name list")
    # Counted before the first name is read: a workbook's declarations are the
    # work it asks for whether or not any of them resolves.
    member = f"{{{namespace}}}definedName"
    entries = [node for node in declared[0] if node.tag == member]
    if len(entries) > _MAX_XLSX_DEFINED_NAMES:
        raise OOXMLInvalid("XLSX workbook declares too many defined names")
    records: list[dict[str, object]] = []
    seen: set[tuple[int | None, str]] = set()
    for entry in entries:
        # An identity is never clipped: two over-long names cut to the same
        # text would be one name, so one outside the bound is refused.
        name = entry.attrib.get("name", "")
        if not name or len(name) > _MAX_META_CHARS:
            raise OOXMLInvalid("XLSX defined name is missing, empty or over-long")
        # The stored expression, exactly as written. A name with nothing stored
        # under it defines nothing, and an over-long one is refused rather than
        # cut down into an expression the workbook never wrote.
        expression = entry.text or ""
        if not expression:
            raise OOXMLInvalid("XLSX defined name declares no expression")
        if len(expression) > _MAX_XLSX_DEFINED_NAME_CHARS:
            raise OOXMLInvalid(
                "XLSX defined name expression exceeds the accepted bound"
            )
        local = entry.attrib.get("localSheetId")
        sheet: tuple[str, str, str, str] | None = None
        ordinal: int | None = None
        if local is not None:
            if _DIGITS.fullmatch(local) is None or int(local) >= len(sheets):
                raise OOXMLInvalid("XLSX defined name names no local sheet")
            ordinal = int(local)
            sheet = sheets[ordinal]
        if (ordinal, name.casefold()) in seen:
            raise OOXMLInvalid("XLSX defined name is repeated in one scope")
        seen.add((ordinal, name.casefold()))
        records.append(
            {
                "kind": "definedName",
                "name": name,
                "expression": expression,
                "scope": "workbook" if sheet is None else "sheet",
                "localSheetId": ordinal,
                "sheet": None if sheet is None else sheet[0],
                "sheetId": None if sheet is None else sheet[1],
                "sheetPart": None if sheet is None else sheet[2],
                "sheetState": None if sheet is None else sheet[3],
                # Free text the declaration carries, clipped rather than
                # refused: neither one is an identity, and nothing here reads
                # either as anything but the text it is.
                **{
                    key: None
                    if (value := entry.attrib.get(key)) is None
                    else _clip(value, _MAX_META_CHARS)
                    for key in ("comment", "description")
                },
                **{key: _flag(entry, key) for key in _XLSX_DEFINED_NAME_FLAGS},
            }
        )
    return records


def _xlsx_connections(
    package: _Package,
) -> tuple[
    list[tuple[dict[str, object], dict[str, object]]], dict[int, tuple[str, int]]
]:
    """Inventory the workbook's stored data connections without opening one.

    The connections list is the workbook's own part, reached only through the
    workbook's own relationships: one of the two URIs OOXML defines for the
    type, internal, and resolving to a part the package declares with the
    connections content type. No path is derived from the workbook's name, so a
    member that merely looks like the list is never read, and two qualifying
    relationships leave which list is meant ambiguous, so the workbook is
    refused rather than read from whichever sorts first. The root is then held
    to ``connections`` in one of the two SpreadsheetML namespaces, as the one
    remaining check that the part holds the vocabulary its declaration
    promised.

    A connection counts only where it is a direct child of that root and
    qualified in the root's own namespace. An unqualified, foreign or further
    nested lookalike declares nothing and stays transparent, exactly as it does
    on the defined-name and table routes, so a list that wrote no namespace
    reports no connections rather than being read for whatever spells the same
    local name. A list that declared none is the empty list it is, which stays
    distinguishable from a workbook that relates no part at all.

    What comes back is the authored metadata needed to understand what the
    workbook stored and what it says would happen on a refresh: the identity,
    the author's own words, the files the author named as the source, the
    declared credential mode, and the refresh and lifecycle flags. Nothing
    inside a property set is copied -- no connection string, command, parameter
    value, password, token or extension body -- only which kinds were declared
    and how many parameters the parameter list held.

    Nothing here performs a refresh or any external access, and every record
    says so outright. No ``sourceFile`` or ``odcFile`` is opened, resolved or
    fetched, no query is run, no network or filesystem target is reached past
    the package's own members, and nothing is recalculated.

    The index that comes back is where each declared id sits, so a sheet's query
    table can name the connection it cites without deriving a part from it. A
    workbook relating no connections part at all indexes nothing, which is what
    leaves a cited id explicitly unlinked rather than resolved by guesswork.
    """
    resolved = _related_part(
        package,
        "xl/workbook.xml",
        _XLSX_CONNECTIONS_RELATIONSHIPS,
        _XLSX_CONNECTIONS_CONTENT_TYPE,
        "connections",
        owner="workbook",
        label="XLSX",
    )
    if resolved is None:
        return [], {}
    relation_id, connections_part = resolved
    root = package.xml(connections_part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX connections part uses an unsupported namespace")
    if local != "connections":
        raise OOXMLInvalid("XLSX connections part is not rooted at its own list")
    # Counted before the first connection is read: a workbook's declarations
    # are the work it asks for whether or not any of them is usable.
    entries = [node for node in root if node.tag == f"{{{namespace}}}connection"]
    if len(entries) > _MAX_XLSX_CONNECTIONS:
        raise OOXMLInvalid("XLSX workbook declares too many connections")
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    declared: dict[int, tuple[str, int]] = {}
    seen: set[int] = set()
    for index, entry in enumerate(entries):
        # The id is the connection's identity: a query table or a pivot cache
        # cites it, so a missing one names nothing and a repeated one names two
        # connections. It is numeric, so the value rather than the spelling is
        # the identity and no two spellings of one number are two connections.
        raw = entry.attrib.get("id")
        if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
            raise OOXMLInvalid("XLSX connection id is missing or malformed")
        identity = int(raw)
        if identity in seen:
            raise OOXMLInvalid("XLSX connection id is repeated")
        seen.add(identity)
        declared[identity] = (connections_part, index)
        # Closed, so a mode outside the type is refused rather than read as
        # whichever of the four it resembles.
        credentials = entry.attrib.get("credentials")
        if credentials is not None and credentials not in _XLSX_CREDENTIAL_METHODS:
            raise OOXMLInvalid("XLSX connection credentials mode is not a defined value")
        name = entry.attrib.get("name")
        records.append(
            (
                {
                    "part": "xl/workbook.xml",
                    "connectionsPart": connections_part,
                    "connectionsRelationshipId": relation_id,
                    "connectionIndex": index,
                    "connectionId": identity,
                    **(
                        {}
                        if name is None
                        else {"connection": _clip(name, _MAX_META_CHARS)}
                    ),
                },
                {
                    "kind": "connection",
                    "id": identity,
                    "credentials": credentials,
                    # Stated outright on every record: this reader read a
                    # stored declaration and did nothing it describes.
                    "refreshPerformed": False,
                    "externalAccessPerformed": False,
                    # Free text, clipped rather than refused: none of it is an
                    # identity, and nothing here reads any of it as anything
                    # but the text it is. A field written empty stays the empty
                    # claim the file made rather than becoming an absent one.
                    **{
                        key: None
                        if (value := entry.attrib.get(key)) is None
                        else _clip(value, _MAX_META_CHARS)
                        for key in _XLSX_CONNECTION_TEXT
                    },
                    **{
                        key: _xlsx_connection_flag(entry, key)
                        for key in _XLSX_CONNECTION_FLAGS
                    },
                    **{
                        key: _xlsx_connection_number(entry, key, bound)
                        for key, bound in _XLSX_CONNECTION_NUMBERS
                    },
                    **_xlsx_connection_payloads(entry, namespace),
                },
            )
        )
    _xlsx_cover(package, connections_part, "connections", root)
    return records, declared


def _xlsx_connection_flag(entry: ElementTree.Element, name: str) -> bool | None:
    """Read one connection's boolean attribute, or None where absent.

    ``None`` says the file declared nothing, which is not the same as a
    declared false, and no default is invented for it. The value is closed, so
    one outside the type is refused rather than read as whichever of the two it
    resembles.
    """
    value = entry.attrib.get(name)
    if value is None:
        return None
    if value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise OOXMLInvalid("XLSX connection flag is malformed")


def _xlsx_connection_number(
    entry: ElementTree.Element, name: str, bound: int
) -> int | None:
    """Read one connection's unsigned numeric attribute, or None where absent.

    Absent stays absent: nothing invents the schema's default, because a
    version the file never wrote is not a version it claimed. The value is
    unsigned and bounded by its own type, so a negative or oversized one is not
    a value the type has and is refused rather than clamped into one.
    """
    raw = entry.attrib.get(name)
    if raw is None:
        return None
    if _INDEX.fullmatch(raw) is None or int(raw) > bound:
        raise OOXMLInvalid("XLSX connection numeric attribute is malformed")
    return int(raw)


def _xlsx_connection_payloads(
    entry: ElementTree.Element, namespace: str
) -> dict[str, object]:
    """Report which property sets one connection declared, and nothing in them.

    Each set is matched as a direct child qualified in the list's own namespace,
    so a lookalike in a foreign vocabulary, one stripped of its prefix and one
    nested deeper all declare nothing. The schema gives a connection one of each
    set, so a second leaves two sets of properties at one position and nothing
    here picks between them: it is refused rather than read from whichever was
    written first.

    Only presence comes out, plus how many parameters the parameter list held.
    The sets themselves are left closed -- which is what makes a second one a
    refusal rather than more of the same -- so no connection string, command,
    query, parameter value, credential or extension body is ever copied out.
    """
    held = {
        kind: [node for node in entry if node.tag == f"{{{namespace}}}{kind}"]
        for kind in _XLSX_CONNECTION_CHILDREN
    }
    if any(len(nodes) > 1 for nodes in held.values()):
        raise OOXMLInvalid("XLSX connection declares a property set twice")
    parameters = held["parameters"]
    return {
        **{f"{kind}Declared": bool(nodes) for kind, nodes in held.items()},
        # Counted, never read: a parameter's own value is exactly the kind of
        # payload this route does not copy.
        "parameterCount": None
        if not parameters
        else sum(
            1 for node in parameters[0] if node.tag == f"{{{namespace}}}parameter"
        ),
    }


def _xlsx_external_links(
    package: _Package, workbook: ElementTree.Element
) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Inventory the workbook's external links without following one.

    An external link is reached on the schema's own route and no other: the
    ``externalReferences`` directly below the workbook, an ``externalReference``
    directly below that, both qualified in the workbook root's own namespace,
    and that element's own relationship id. An unqualified, foreign or further
    nested lookalike declares nothing and stays transparent, exactly as it does
    on the defined-name, table and connections routes, so a workbook that wrote
    no namespace reports no external links rather than being read for whatever
    spells the same local name. No part path is derived from the workbook's own
    name, so a member that merely looks like an external-link part is never
    read.

    Each reference names its own part, so two references naming one relationship
    -- or two relationships resolving to one part -- leave one link declared
    twice and the workbook is refused rather than read with it counted once or
    twice arbitrarily.

    What comes back is the declaration and nothing it points at: the link's
    kind, the relationship and part the workbook reached it by, the target
    string exactly as the relationship holds it, and the inert metadata the form
    declares beside it. No external workbook is opened, no DDE service is
    contacted, no OLE server is launched, no cached value is copied out, and
    every record says so outright.
    """
    namespace, _, _ = workbook.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        return []
    declared = [
        node for node in workbook if node.tag == f"{{{namespace}}}externalReferences"
    ]
    if not declared:
        return []
    if len(declared) > 1:
        raise OOXMLInvalid(
            "XLSX workbook declares more than one external-reference list"
        )
    # Counted before the first part is opened: a workbook's declarations are the
    # work it asks for whether or not any of them resolves.
    member = f"{{{namespace}}}externalReference"
    entries = [node for node in declared[0] if node.tag == member]
    if len(entries) > _MAX_XLSX_EXTERNAL_LINKS:
        raise OOXMLInvalid("XLSX workbook declares too many external links")
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    named: set[str] = set()
    resolved: set[str] = set()
    for index, entry in enumerate(entries):
        relation_id = _relationship_id(entry.attrib)
        if not relation_id:
            raise OOXMLInvalid("XLSX external reference names no relationship")
        if relation_id in named:
            raise OOXMLInvalid("XLSX external reference is repeated")
        named.add(relation_id)
        part = _referenced_part(
            package,
            "xl/workbook.xml",
            relation_id,
            _XLSX_EXTERNAL_LINK_RELATIONSHIPS,
            _XLSX_EXTERNAL_LINK_CONTENT_TYPE,
            "external link",
            "XLSX",
        )
        if part in resolved:
            raise OOXMLInvalid("XLSX external link part is referenced twice")
        resolved.add(part)
        records.append(
            (
                {
                    "part": "xl/workbook.xml",
                    "externalLinkPart": part,
                    "externalLinkRelationshipId": _clip(relation_id, _MAX_META_CHARS),
                    "externalLinkIndex": index,
                },
                _xlsx_external_link(package, part),
            )
        )
    return records


def _xlsx_external_link(package: _Package, part: str) -> dict[str, object]:
    """Read one external-link part, reporting what it declares and no more.

    The declared content type says what the part is; the root is where the part
    says so itself, in one of the two SpreadsheetML namespaces, as the one
    remaining check that it holds the vocabulary that declaration promised.

    ``EG_ExternalLinkChoice`` gives the part exactly one link, so a part
    declaring none declares nothing a record could describe, and two -- two of a
    kind, or one of each -- leave which one the workbook links to ambiguous.
    Neither is guessed at. The choice is matched as a direct child qualified in
    the root's own namespace, so a lookalike in a foreign vocabulary, one
    stripped of its prefix and one nested deeper all declare nothing.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX external link part uses an unsupported namespace")
    if local != "externalLink":
        raise OOXMLInvalid("XLSX external link part is not rooted at its own link")
    kinds = {f"{{{namespace}}}{kind}": kind for kind in _XLSX_EXTERNAL_LINK_KINDS}
    declarations = [(kinds[node.tag], node) for node in root if node.tag in kinds]
    if not declarations:
        raise OOXMLInvalid("XLSX external link part declares no link")
    if len(declarations) > 1:
        raise OOXMLInvalid("XLSX external link part declares more than one link")
    kind, node = declarations[0]
    record = dict(_XLSX_EXTERNAL_LINK_RECORD)
    record["linkKind"] = kind
    relates = _XLSX_EXTERNAL_LINK_KINDS[kind]
    if relates is not None:
        defined, required = relates
        record.update(
            _xlsx_external_target(package, part, node, defined, required=required)
        )
    record.update(
        {
            field: _xlsx_external_claim(node, name)
            for field, name in _XLSX_EXTERNAL_LINK_CLAIMS[kind]
        }
    )
    record.update(
        {
            field: _xlsx_external_count(node, namespace, group, item)
            for field, group, item in _XLSX_EXTERNAL_LINK_GROUPS[kind]
        }
    )
    _xlsx_cover(package, part, "externalLink", root)
    return record


def _xlsx_external_target(
    package: _Package,
    part: str,
    node: ElementTree.Element,
    defined: frozenset[str],
    *,
    required: bool,
) -> dict[str, object]:
    """Report the external target one link names, without reaching it.

    ``CT_OleLink`` requires the id and ``CT_ExternalBook`` does not, so a
    workbook link that named no path declares a cached link and nothing more:
    the whole target group stays absent rather than being refused or filled
    with a path nothing wrote.

    Where an id is written it resolves only through the link part's own
    relationships, so nothing is derived from the part's name and no member is
    reached by merely looking like the target. The relationship has to carry one
    of the URIs OOXML defines for the link's own kind, and it has to be
    external: the target is another workbook or a server document by
    definition, so an internal one contradicts the declaration rather than
    qualifying it, and is refused instead of opened as a package member.

    What comes out is the relationship's own text, clipped to the bound every
    other target on every other route is clipped to, beside the scheme that text
    names. Nothing resolves it against the filesystem, normalises it into a
    path, mounts it, opens it, fetches it or refreshes anything from it, and the
    scheme is read off the string rather than used to reach anything.
    """
    relation_id = _relationship_id(node.attrib)
    if not relation_id:
        if not required:
            return {}
        raise OOXMLInvalid("XLSX external link names no target relationship")
    rels = posixpath.join(
        posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
    )
    entry = package.relationships.get(rels, {}).get(relation_id)
    if entry is None:
        raise OOXMLInvalid("XLSX external link names a missing target relationship")
    kind = package.relationship_types.get(rels, {}).get(relation_id, "")
    if not _is_defined_relationship(kind, defined):
        raise OOXMLInvalid(
            "XLSX external link names a target relationship of another type"
        )
    target, external = entry
    if not external:
        raise OOXMLInvalid("XLSX external link target relationship is not external")
    match = _SCHEME.match(target)
    return {
        "targetRelationshipId": _clip(relation_id, _MAX_META_CHARS),
        "targetRelationshipType": _clip(kind, _MAX_TARGET_CHARS),
        "target": _clip(target, _MAX_TARGET_CHARS),
        "targetExternal": True,
        "targetScheme": match.group(1).casefold() if match else None,
    }


def _xlsx_external_claim(element: ElementTree.Element, name: str) -> str:
    """Read one claim a DDE or OLE link is required to declare, clipped.

    The schema requires each, so a link that wrote none declares no service,
    topic or server and is refused rather than reported under a claim nothing
    made. An authored empty one is kept exactly as written, for the reason a
    person's empty display name is: the type is ``ST_Xstring``, so the empty
    string is a claim the file made and not an absent one.

    The value is not an identity anything here resolves, so it is clipped
    rather than refused for length.
    """
    value = element.attrib.get(name)
    if value is None:
        raise OOXMLInvalid("XLSX external link declaration is missing a required claim")
    return _clip(value, _MAX_META_CHARS)


def _xlsx_external_count(
    holder: ElementTree.Element, namespace: str, group: str, member: str
) -> int | None:
    """Count what one declared group holds, or None where none was declared.

    Presence and a count come out, and nothing inside the group does. The cached
    values under ``sheetDataSet`` are the linked workbook's own content, and a
    reader that copied them would be reporting the other workbook's data as
    though this one's link had been followed -- so they are counted, never read.

    The group is matched as a direct child qualified in the link part's own
    namespace, for the reason the link itself is, and the schema gives a link one
    group of a kind, so a second leaves two at one position and is refused rather
    than read from whichever was written first.
    """
    groups = [node for node in holder if node.tag == f"{{{namespace}}}{group}"]
    if len(groups) > 1:
        raise OOXMLInvalid("XLSX external link declares a group twice")
    if not groups:
        return None
    return sum(1 for node in groups[0] if node.tag == f"{{{namespace}}}{member}")


def _xlsx_pivot_caches(
    package: _Package, workbook: ElementTree.Element
) -> tuple[
    list[tuple[dict[str, object], dict[str, object]]], dict[int, tuple[str, str]]
]:
    """Inventory the workbook's pivot caches and index them by the id they claim.

    A cache is reached on the schema's own route and no other: the
    ``pivotCaches`` directly below the workbook, a ``pivotCache`` directly below
    that, both qualified in the workbook root's own namespace, and that
    element's own relationship id. An unqualified, foreign or further nested
    lookalike declares nothing and stays transparent, exactly as it does on the
    defined-name, table, connections and external-link routes. No part path is
    derived from the workbook's own name, so a member that merely looks like a
    cache definition is never read.

    ``cacheId`` is the identity a pivot table cites, so a missing one names
    nothing and a repeated one leaves every table citing it pointing at two
    caches. It is numeric, so the value rather than the spelling is the
    identity. Two declarations naming one relationship -- or two relationships
    resolving to one part -- leave one cache declared twice, and the workbook is
    refused rather than read with it counted once or twice arbitrarily.

    Nothing is refreshed. The definition is the state the file saved, the record
    says so outright, and the index that comes back beside the records is what
    lets a worksheet's pivot table name the cache it was built from without
    either part being read twice.
    """
    namespace, _, _ = workbook.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        return [], {}
    declared = [node for node in workbook if node.tag == f"{{{namespace}}}pivotCaches"]
    if not declared:
        return [], {}
    if len(declared) > 1:
        raise OOXMLInvalid("XLSX workbook declares more than one pivot-cache list")
    # Counted before the first definition is opened: a workbook's declarations
    # are the work it asks for whether or not any of them resolves.
    member = f"{{{namespace}}}pivotCache"
    entries = [node for node in declared[0] if node.tag == member]
    if len(entries) > _MAX_XLSX_PIVOT_CACHES:
        raise OOXMLInvalid("XLSX workbook declares too many pivot caches")
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    caches: dict[int, tuple[str, str]] = {}
    named: set[str] = set()
    resolved: set[str] = set()
    for index, entry in enumerate(entries):
        raw = entry.attrib.get("cacheId")
        if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
            raise OOXMLInvalid("XLSX pivot cache id is missing or malformed")
        cache_id = int(raw)
        if cache_id in caches:
            raise OOXMLInvalid("XLSX pivot cache id is repeated")
        relation_id = _relationship_id(entry.attrib)
        if not relation_id:
            raise OOXMLInvalid("XLSX pivot cache names no relationship")
        if relation_id in named:
            raise OOXMLInvalid("XLSX pivot cache relationship is repeated")
        named.add(relation_id)
        part = _referenced_part(
            package,
            "xl/workbook.xml",
            relation_id,
            _XLSX_PIVOT_CACHE_RELATIONSHIPS,
            _XLSX_PIVOT_CACHE_CONTENT_TYPE,
            "pivot cache",
            "XLSX",
        )
        if part in resolved:
            raise OOXMLInvalid("XLSX pivot cache part is referenced twice")
        resolved.add(part)
        caches[cache_id] = (part, _clip(relation_id, _MAX_META_CHARS))
        records.append(
            (
                {
                    "part": "xl/workbook.xml",
                    "pivotCachePart": part,
                    "pivotCacheRelationshipId": _clip(relation_id, _MAX_META_CHARS),
                    "pivotCacheIndex": index,
                    "pivotCacheId": cache_id,
                },
                _xlsx_pivot_cache(package, part, cache_id),
            )
        )
    return records, caches


def _xlsx_pivot_cache(
    package: _Package, part: str, cache_id: int
) -> dict[str, object]:
    """Read one pivot-cache definition, reporting the state it saved and no more.

    The declared content type says what the part is; the root is where the part
    says so itself, in one of the two SpreadsheetML namespaces, as the one
    remaining check that it holds the vocabulary that declaration promised.

    ``cacheSource`` is where the cache says its data came from, and the schema
    gives a definition exactly one: none describes no source a record could
    name, and two leave which one it was built from ambiguous. The field list is
    counted and never read -- a ``cacheField``'s ``sharedItems`` hold the cached
    values themselves, which is exactly the payload this route does not copy --
    and its declared count is bounded before the list is collected.

    ``freshness`` is unknown on every record and both standing claims are false,
    because this reader opened a saved declaration and did none of what it
    describes: nothing was refreshed, no source was reached, no value was
    recalculated and ``recordCount`` is reported as the file stored it rather
    than agreed against anything.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX pivot cache part uses an unsupported namespace")
    if local != "pivotCacheDefinition":
        raise OOXMLInvalid("XLSX pivot cache part is not rooted at its own definition")
    sources = [node for node in root if node.tag == f"{{{namespace}}}cacheSource"]
    if len(sources) != 1:
        raise OOXMLInvalid("XLSX pivot cache does not declare exactly one source")
    # Reported before the records part its definition relates is opened, so a
    # definition is always ahead of its records in the coverage order.
    _xlsx_cover(package, part, "pivotCacheDefinition", root)
    return {
        "kind": "pivotCache",
        "cacheId": cache_id,
        "definitionPart": part,
        # Stated outright on every record: this reader read a saved declaration
        # and did nothing it describes.
        "freshness": "unknown",
        "refreshPerformed": False,
        "externalAccessPerformed": False,
        **_xlsx_pivot_records(package, part, root),
        **_xlsx_pivot_source(sources[0], namespace),
        "fieldCount": _xlsx_pivot_fields(root, namespace),
        **{
            key: None
            if (value := root.attrib.get(key)) is None
            else _clip(value, _MAX_META_CHARS)
            for key in _XLSX_PIVOT_CACHE_TEXT
        },
        **{key: _xlsx_pivot_flag(root, key) for key in _XLSX_PIVOT_CACHE_FLAGS},
        **{
            key: _xlsx_pivot_number(root, key, bound)
            for key, bound in _XLSX_PIVOT_CACHE_NUMBERS
        },
    }


def _xlsx_pivot_fields(root: ElementTree.Element, namespace: str) -> int | None:
    """Count the fields one cache definition declared, or None where none is.

    ``cacheFields`` is optional in the schema, so a definition that wrote none
    declares no field list rather than an empty one, and the two stay
    distinguishable. Its ``count`` is optional too, so an absent one is the list
    the definition wrote rather than a refusal; where it is written it has to
    agree with that list, because a count and a list that disagree name two
    different widths and nothing here picks between them.

    The declared count is bounded before the list is collected, and the list
    again after, so neither a width nothing wrote nor a list nothing counted
    gets past the bound.
    """
    holders = [node for node in root if node.tag == f"{{{namespace}}}cacheFields"]
    if len(holders) > 1:
        raise OOXMLInvalid("XLSX pivot cache declares its field list twice")
    if not holders:
        return None
    count = holders[0].attrib.get("count")
    if count is not None and (
        _INDEX.fullmatch(count) is None or int(count) > _MAX_XLSX_PIVOT_FIELDS
    ):
        raise OOXMLInvalid("XLSX pivot cache declares too many fields or no count")
    # Counted, never read: a field's ``sharedItems`` hold the cached values.
    fields = [node for node in holders[0] if node.tag == f"{{{namespace}}}cacheField"]
    if len(fields) > _MAX_XLSX_PIVOT_FIELDS:
        raise OOXMLInvalid("XLSX pivot cache declares too many fields or no count")
    if count is not None and len(fields) != int(count):
        raise OOXMLInvalid("XLSX pivot cache field count does not match its field list")
    return len(fields)


def _xlsx_pivot_records(
    package: _Package, part: str, root: ElementTree.Element
) -> dict[str, object]:
    """Name the cache-records part one definition relates, and count what it stored.

    The relationship is optional -- a cache saved with ``saveData`` off keeps no
    records part at all -- so an absent id leaves the whole group absent rather
    than being refused or filled with a part nothing wrote. Where an id is
    written it resolves only through the definition part's own relationships, so
    nothing is derived from the part's name and no member is reached by merely
    looking like the records. The relationship has to be internal and to carry
    one of the URIs OOXML defines for the type: the records are a member of this
    package by definition, so an external one contradicts the declaration rather
    than qualifying it.

    What comes out of the part is its root's own ``count`` and nothing else. The
    ``r`` records below it are the cached data itself, so they are neither read
    nor counted here, and the stored count is reported as stored rather than
    agreed against them.
    """
    relation_id = _relationship_id(root.attrib)
    if not relation_id:
        return {
            "recordsPart": None,
            "recordsRelationshipId": None,
            "recordsPartCount": None,
        }
    records_part = _referenced_part(
        package,
        part,
        relation_id,
        _XLSX_PIVOT_RECORDS_RELATIONSHIPS,
        _XLSX_PIVOT_RECORDS_CONTENT_TYPE,
        "pivot cache records",
        "XLSX",
    )
    records = package.xml(records_part)
    namespace, _, local = records.tag.rpartition("}")
    if namespace.lstrip("{") not in _SPREADSHEETML:
        raise OOXMLInvalid(
            "XLSX pivot cache records part uses an unsupported namespace"
        )
    if local != "pivotCacheRecords":
        raise OOXMLInvalid(
            "XLSX pivot cache records part is not rooted at its own records"
        )
    count = _xlsx_pivot_number(records, "count", _MAX_INDEX)
    _xlsx_cover(package, records_part, "pivotCacheRecords", records)
    return {
        "recordsPart": records_part,
        "recordsRelationshipId": _clip(relation_id, _MAX_META_CHARS),
        "recordsPartCount": count,
    }


def _xlsx_pivot_source(
    source: ElementTree.Element, namespace: str
) -> dict[str, object]:
    """Report where one cache says its data came from, without going there.

    ``type`` is required and closed, so a source type outside the four OOXML
    defines is refused rather than read as whichever it resembles. A
    ``worksheetSource`` belongs to the worksheet type alone, so one written
    under another type contradicts the declaration, and the schema gives a
    source one of them, so a second leaves two at one position.

    The worksheet reference is reported only where it parses as a range, for the
    reason every other range on every other route is: a reference nothing can
    place names no rectangle. A source naming a defined name instead writes
    ``name`` and no ``ref``, which is the source it is rather than a refusal.

    ``connectionId`` and the worksheet source's own relationship id are kept as
    the inert identifiers they are. Neither is resolved: no connection is
    opened, no external workbook is reached, no query is run and nothing is
    refreshed.
    """
    kind = source.attrib.get("type")
    if kind not in _XLSX_PIVOT_SOURCE_TYPES:
        raise OOXMLInvalid("XLSX pivot cache source type is missing or undefined")
    record: dict[str, object] = {
        "sourceType": kind,
        "sourceConnectionId": _xlsx_pivot_number(source, "connectionId", _MAX_INDEX),
        "sourceSheet": None,
        "sourceName": None,
        "sourceRef": None,
        "sourceRelationshipId": None,
    }
    held = [node for node in source if node.tag == f"{{{namespace}}}worksheetSource"]
    if len(held) > 1:
        raise OOXMLInvalid("XLSX pivot cache declares its worksheet source twice")
    if not held:
        return record
    if kind != "worksheet":
        raise OOXMLInvalid(
            "XLSX pivot cache declares a worksheet source under another source type"
        )
    ref = held[0].attrib.get("ref")
    if ref is not None:
        _cell_range(ref)
    relation_id = _relationship_id(held[0].attrib)
    record.update(
        {
            "sourceSheet": None
            if (sheet := held[0].attrib.get("sheet")) is None
            else _clip(sheet, _MAX_META_CHARS),
            "sourceName": None
            if (name := held[0].attrib.get("name")) is None
            else _clip(name, _MAX_META_CHARS),
            "sourceRef": ref,
            "sourceRelationshipId": _clip(relation_id, _MAX_META_CHARS) or None,
        }
    )
    return record


def _xlsx_pivot_tables(
    package: _Package,
    part: str,
    root: ElementTree.Element,
    caches: dict[int, tuple[str, str]],
) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Inventory one sheet's pivot tables, linking each to the cache it cites.

    A pivot table is reached on the schema's own route and no other, exactly as
    a declared table is: the ``pivotTableParts`` directly below the worksheet, a
    ``pivotTablePart`` directly below that, both qualified in the worksheet's
    own SpreadsheetML namespace, and that element's own relationship id. An
    unqualified, foreign or further nested lookalike declares nothing, and no
    part path is derived from the sheet's own name. The ``.rels`` file is the
    resolver, never the route: a pivot-table relationship no ``pivotTablePart``
    names is inert, because a relationship is how the sheet reaches what it
    declared and not itself a declaration.

    Order is the order the sheet wrote its declarations, so the inventory is the
    author's own and the same package reports the same way every time.

    ``count`` is optional on the list, so an absent one is the list the sheet
    wrote rather than a refusal. Where it is written it is bounded before a
    single relationship is resolved -- a width nothing wrote is never opened --
    and then has to agree with the list, because a count and a list that
    disagree name two different numbers of reports and nothing here picks
    between them.

    Each declaration names one relationship: a missing id reaches nothing, a
    repeated id declares one report twice, and two ids resolving to one part
    leave one report declared twice as well.
    """
    # The rest of the sheet reader matches local names, because a sheet's
    # vocabulary is settled by the part it was reached through. This route opens
    # another part, so it is held to the sheet's own namespace and that namespace
    # has to be SpreadsheetML.
    namespace, _, _ = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        return []
    declared = [
        node for node in root if node.tag == f"{{{namespace}}}pivotTableParts"
    ]
    if not declared:
        return []
    if len(declared) > 1:
        raise OOXMLInvalid(
            "XLSX worksheet declares more than one pivot-table-part list"
        )
    count = declared[0].attrib.get("count")
    if count is not None and (
        _INDEX.fullmatch(count) is None or int(count) > _MAX_XLSX_PIVOT_TABLES
    ):
        raise OOXMLInvalid("XLSX worksheet declares too many pivot tables or no count")
    # Counted before the first part is opened: a sheet's declarations are the
    # work it asks for whether or not any of them resolves.
    member = f"{{{namespace}}}pivotTablePart"
    entries = [node for node in declared[0] if node.tag == member]
    if len(entries) > _MAX_XLSX_PIVOT_TABLES:
        raise OOXMLInvalid("XLSX worksheet declares too many pivot tables or no count")
    if count is not None and len(entries) != int(count):
        raise OOXMLInvalid(
            "XLSX worksheet pivot-table count does not match its part list"
        )
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    named: set[str] = set()
    resolved: set[str] = set()
    for index, entry in enumerate(entries):
        relation_id = _relationship_id(entry.attrib)
        if not relation_id:
            raise OOXMLInvalid("XLSX pivot table part declaration names no relationship")
        if relation_id in named:
            raise OOXMLInvalid("XLSX pivot table part declaration is repeated")
        named.add(relation_id)
        table_part = _referenced_part(
            package,
            part,
            relation_id,
            _XLSX_PIVOT_TABLE_RELATIONSHIPS,
            _XLSX_PIVOT_TABLE_CONTENT_TYPE,
            "pivot table",
            "XLSX",
        )
        if table_part in resolved:
            raise OOXMLInvalid("XLSX pivot table part is referenced twice")
        resolved.add(table_part)
        records.append(
            (
                {
                    "pivotTablePart": table_part,
                    "pivotTableRelationshipId": _clip(relation_id, _MAX_META_CHARS),
                    "pivotTableIndex": index,
                },
                _xlsx_pivot_table(package, table_part, caches),
            )
        )
    return records


def _xlsx_pivot_table(
    package: _Package, part: str, caches: dict[int, tuple[str, str]]
) -> dict[str, object]:
    """Read one pivot-table definition, reporting the report it saved, inert.

    The declared content type says what the part is; the root is where the part
    says so itself, in one of the two SpreadsheetML namespaces, as the one
    remaining check that it holds the vocabulary that declaration promised.

    ``name`` and ``cacheId`` are both required, so a definition missing either
    names no report and cites no cache. The cache is linked by the id the table
    cites, through the index the workbook's own declarations built: where the
    workbook declared no cache under that id, the link is absent rather than
    invented, and the table is still the inventory it is -- nothing here derives
    a cache part from a name or picks one that happens to be in the package.

    Everything else is the authored shape of the report and the counts of the
    field lists it declared. A field's own items are cached data, so they are
    counted and never read, and no value, caption text aside, comes out of the
    part. ``freshness`` is unknown and both standing claims are false for the
    reason they are on a cache: this reader read a saved declaration and
    refreshed, recalculated and reached nothing.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX pivot table part uses an unsupported namespace")
    if local != "pivotTableDefinition":
        raise OOXMLInvalid("XLSX pivot table part is not rooted at its own definition")
    name = root.attrib.get("name")
    if name is None:
        raise OOXMLInvalid("XLSX pivot table declares no name")
    raw = root.attrib.get("cacheId")
    if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid("XLSX pivot table cache id is missing or malformed")
    cache_id = int(raw)
    linked = caches.get(cache_id)
    # The schema gives a definition one location and requires its reference, but
    # the reference is the only thing read off it, so a definition that wrote
    # none reports no location rather than being refused for it.
    located = [node for node in root if node.tag == f"{{{namespace}}}location"]
    if len(located) > 1:
        raise OOXMLInvalid("XLSX pivot table declares its location twice")
    location = located[0].attrib.get("ref") if located else None
    if location is not None:
        _cell_range(location)
    record: dict[str, object] = {
        "kind": "pivotTable",
        "name": _clip(name, _MAX_META_CHARS),
        "cacheId": cache_id,
        "definitionPart": part,
        "cachePart": None if linked is None else linked[0],
        "cacheRelationshipId": None if linked is None else linked[1],
        "location": location,
        # Stated outright on every record, as on a cache's: this reader read a
        # saved declaration and did nothing it describes.
        "freshness": "unknown",
        "refreshPerformed": False,
        "externalAccessPerformed": False,
        **{
            field: _xlsx_pivot_count(root, namespace, group, item)
            for field, group, item in _XLSX_PIVOT_TABLE_GROUPS
        },
        **{
            key: None
            if (value := root.attrib.get(key)) is None
            else _clip(value, _MAX_META_CHARS)
            for key in _XLSX_PIVOT_TABLE_TEXT
        },
        **{
            key: _xlsx_pivot_number(root, key, bound)
            for key, bound in _XLSX_PIVOT_TABLE_NUMBERS
        },
    }
    _xlsx_cover(package, part, "pivotTable", root)
    return record


def _xlsx_pivot_count(
    holder: ElementTree.Element, namespace: str, group: str, member: str
) -> int | None:
    """Count what one declared field list holds, or None where none was declared.

    Presence and a count come out, and nothing inside the list does: a pivot
    field's items are the cache's own values, and a reader that copied them
    would be reporting cached data as though the report had been rebuilt.

    The list is matched as a direct child qualified in the definition's own
    namespace, so a lookalike in a foreign vocabulary, one stripped of its
    prefix and one nested deeper all declare nothing, and the schema gives a
    definition one list of a kind, so a second is refused rather than read from
    whichever was written first. The list is bounded for the reason the cache's
    fields are.
    """
    groups = [node for node in holder if node.tag == f"{{{namespace}}}{group}"]
    if len(groups) > 1:
        raise OOXMLInvalid("XLSX pivot table declares a field list twice")
    if not groups:
        return None
    count = sum(1 for node in groups[0] if node.tag == f"{{{namespace}}}{member}")
    if count > _MAX_XLSX_PIVOT_FIELDS:
        raise OOXMLInvalid("XLSX pivot table declares too many fields")
    return count


def _xlsx_pivot_flag(element: ElementTree.Element, name: str) -> bool | None:
    """Read one pivot declaration's boolean attribute, or None where absent.

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
    raise OOXMLInvalid("XLSX pivot flag is malformed")


def _xlsx_pivot_number(
    element: ElementTree.Element, name: str, bound: int
) -> int | None:
    """Read one pivot declaration's unsigned numeric attribute, or None if absent.

    Absent stays absent: nothing invents the schema's default, because a version
    or a count the file never wrote is not one it claimed. The value is unsigned
    and bounded by its own type, so a negative or oversized one is not a value
    the type has and is refused rather than clamped into one.
    """
    raw = element.attrib.get(name)
    if raw is None:
        return None
    if _INDEX.fullmatch(raw) is None or int(raw) > bound:
        raise OOXMLInvalid("XLSX pivot numeric attribute is malformed")
    return int(raw)


def _xlsx_query_table_routes(package: _Package, source: str) -> list[tuple[str, str]]:
    """Return every query-table route ``source`` relates, in the authored order.

    The singular ``_related_part`` is wrong for this one type: it refuses a
    second qualifying relationship, because the parts it resolves are the ones
    OOXML gives a source at most one of. A query table is not one of those --
    a worksheet carries one per range a query was written over, and a table
    definition may do the same -- so every qualifying relationship is a route.

    Order is the relationship part's own: the ids are taken in the order the
    part authored them and never sorted, so an owner whose ids happen to sort
    the other way still reports its query tables the way it wrote them. A
    repeated ``Id`` was already refused when the relationship part was
    validated, so the authored order holds one position per route.

    Each route is held to what the singular helper holds its one to: internal,
    carrying one of the URIs OOXML defines for the type, and resolving to a
    part the package declares as a query table. A relationship of another type
    is inert. All of it happens here, before the caller opens any part.
    """
    rels = posixpath.join(
        posixpath.dirname(source), "_rels", posixpath.basename(source) + ".rels"
    )
    routes: list[tuple[str, str]] = []
    for relation_id, kind in package.relationship_types.get(rels, {}).items():
        if not _is_defined_relationship(kind, _XLSX_QUERY_TABLE_RELATIONSHIPS):
            continue
        target, external = package.relationships[rels][relation_id]
        if external:
            raise OOXMLInvalid("XLSX query table relationship is external")
        if package.declared_type(target) != _XLSX_QUERY_TABLE_CONTENT_TYPE:
            raise OOXMLInvalid(
                "XLSX query table relationship does not name a query table part"
            )
        routes.append((relation_id, target))
    return routes


def _xlsx_query_tables(
    package: _Package,
    part: str,
    tables: list[dict[str, object]],
    connections: dict[int, tuple[str, int]],
) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Inventory the query tables one sheet reaches, linking each to its connection.

    A query table is the one XLSX route with no declaration in the worksheet's
    own markup: SpreadsheetML gives it no container beside ``tableParts`` or
    ``pivotTableParts``, so the qualifying relationship *is* the declaration.
    Two kinds of owner may hold them: the worksheet itself, for a query written
    over a range of the sheet, and a declared table definition, for a table
    whose rows a query returns. Either may hold several -- a sheet carries one
    query table per range a query was written over -- so every qualifying
    relationship an owner holds is a route, not only the first or the only one.

    All are reached on the owner's own relationships and no other route. A
    table definition counts only where the worksheet already declared it on its
    authored ``tableParts`` route, so a table part the sheet never declared
    reaches nothing here, and no part path is derived from a sheet or table
    name: a member that merely looks like a query table is inert however it is
    named or typed. An owner relating no qualifying relationship reaches no
    query table, which is the absent route rather than a refusal, and a
    relationship of another type qualifies as nothing, exactly as it does on the
    styles and connections routes.

    Order is the owner's: the sheet's own query tables first, then its declared
    tables' in the order the sheet wrote its ``tableParts``, and within one
    owner the order that owner's relationship part authored them in. Nothing
    sorts relationship ids to stand in for either, so the same package reports
    the same way every time.

    The invariant on a part two routes reach is that it is one query table, not
    two: the second route is refused, and refused while the routes are still
    being collected, so no query table is opened at all. One id cannot reach it
    twice -- a repeated ``Id`` is refused when the relationship part is
    validated -- so what this catches is two distinct routes naming one part.
    It is the sheet's own invariant, as a pivot table's duplicate check is -- a
    table definition belongs to the sheet that declared it -- so a part two
    *sheets* relate is reported once under each, and every record names the
    owner it was reached through.

    Nothing here runs a query, opens a connection, resolves a Power Query
    source, reaches a network or filesystem target past the package's own
    members, or refreshes or recalculates anything. Every record says so.
    """
    # The owner's relationships are the route, so there is no declaration in the
    # sheet's markup to hold to the sheet's namespace: the part's own root
    # carries that check instead, which is why an unqualified or foreign
    # worksheet reaches its query table the same way a conformant one does.
    owners: list[tuple[str, dict[str, object]]] = [(part, {})]
    for table in tables:
        owners.append(
            (
                str(table["tablePart"]),
                {
                    "tablePart": table["tablePart"],
                    "tableRelationshipId": table["relationshipId"],
                },
            )
        )
    routes: list[tuple[dict[str, object], str, str]] = []
    resolved: set[str] = set()
    for owner, provenance in owners:
        for relation_id, query_part in _xlsx_query_table_routes(package, owner):
            if query_part in resolved:
                raise OOXMLInvalid("XLSX query table part is referenced twice")
            resolved.add(query_part)
            routes.append((provenance, relation_id, query_part))
    # Counted before the first part is opened: what a sheet and its tables
    # relate is the work they ask for whether or not any of it is readable.
    if len(routes) > _MAX_XLSX_QUERY_TABLES:
        raise OOXMLInvalid("XLSX worksheet reaches too many query tables")
    return [
        (
            {
                **provenance,
                "queryTablePart": query_part,
                "queryTableRelationshipId": _clip(relation_id, _MAX_META_CHARS),
                "queryTableIndex": index,
            },
            _xlsx_query_table(package, query_part, connections),
        )
        for index, (provenance, relation_id, query_part) in enumerate(routes)
    ]


def _xlsx_query_table(
    package: _Package, part: str, connections: dict[int, tuple[str, int]]
) -> dict[str, object]:
    """Read one query table, reporting the state it saved and nothing it would do.

    The declared content type says what the part is; the root is where the part
    says so itself, in one of the two SpreadsheetML namespaces, as the one
    remaining check that it holds the vocabulary that declaration promised.

    ``connectionId`` is the one identity a query table has to declare -- it is
    the whole of what the saved query names, and a query table citing no
    connection references nothing -- so a definition missing or malforming it is
    refused. The connection is linked by that id through the index the
    workbook's own connections part built: where the workbook declared no
    connection under it, the link is absent rather than invented, and the query
    table is still the inventory it is. Nothing here derives a connections part
    from an id, picks a connection that happens to be in the package, or opens
    the one it linked.

    ``name`` is not an identity here -- nothing cites a query table by it -- so
    it is clipped as the free text it is, and one written empty stays the empty
    claim the file made.

    Everything else is declared policy for a refresh that does not happen, and
    ``freshness`` is unknown with both standing claims false for the reason they
    are on a connection: this reader read a saved declaration and did none of
    what it describes.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX query table part uses an unsupported namespace")
    if local != "queryTable":
        raise OOXMLInvalid("XLSX query table part is not rooted at its own query table")
    raw = root.attrib.get("connectionId")
    if raw is None or _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid("XLSX query table connection id is missing or malformed")
    connection_id = int(raw)
    linked = connections.get(connection_id)
    grow = root.attrib.get("growShrinkType")
    if grow is not None and grow not in _XLSX_QUERY_GROW_SHRINK:
        raise OOXMLInvalid("XLSX query table grow-shrink type is not a defined value")
    # ``xsd:unsignedInt``, and absent stays absent: a format index the file never
    # wrote is not one it claimed, and nothing is applied on account of it.
    auto_format = root.attrib.get("autoFormatId")
    if auto_format is not None and (
        _INDEX.fullmatch(auto_format) is None or int(auto_format) > 0xFFFF_FFFF
    ):
        raise OOXMLInvalid("XLSX query table numeric attribute is malformed")
    name = root.attrib.get("name")
    record: dict[str, object] = {
        "kind": "queryTable",
        "name": None if name is None else _clip(name, _MAX_META_CHARS),
        "connectionId": connection_id,
        "connectionsPart": None if linked is None else linked[0],
        "connectionIndex": None if linked is None else linked[1],
        "definitionPart": part,
        "growShrinkType": grow,
        "autoFormatId": None if auto_format is None else int(auto_format),
        # Stated outright on every record, as on a connection's and a pivot
        # report's: this reader read a saved declaration and did nothing it
        # describes. Power Query and data connections are never refreshed.
        "freshness": "unknown",
        "refreshPerformed": False,
        "externalAccessPerformed": False,
        **{key: _xlsx_query_flag(root, key) for key in _XLSX_QUERY_TABLE_FLAGS},
        **_xlsx_query_refresh(root, namespace),
    }
    _xlsx_cover(package, part, "queryTable", root)
    return record


def _xlsx_query_refresh(
    root: ElementTree.Element, namespace: str
) -> dict[str, object]:
    """Summarise the refresh state a query table saved, as presence and counts.

    The schema gives a query table one refresh state and one extension point, so
    a second of either leaves two at one position and nothing here picks between
    them. Both are matched as direct children qualified in the part's own
    namespace, so a lookalike written in a foreign vocabulary, one stripped of
    its prefix and one nested deeper all declare nothing, exactly as they do on
    the pivot and declared-table routes.

    Only presence and counts come out. Everything the refresh state actually
    holds is the query's own: the field names are the columns it returned, the
    sort state is the order it returned them in, and the extension payload is
    where a Power Query definition and its parameters live. None of it is read,
    so no query text, parameter, credential, external target, cached value or
    formula can leave the part through here.
    """
    declared: ElementTree.Element | None = None
    extension = False
    for node in root:
        if node.tag == f"{{{namespace}}}queryTableRefresh":
            if declared is not None:
                raise OOXMLInvalid("XLSX query table declares its refresh state twice")
            declared = node
        elif node.tag == f"{{{namespace}}}extLst":
            extension = _xlsx_extension_list(extension, "query table")
    if declared is None:
        return {
            "refreshDeclared": False,
            "refreshSortDeclared": None,
            **{field: None for field, _group, _member in _XLSX_QUERY_TABLE_GROUPS},
        }
    sorts = 0
    nested = False
    for node in declared:
        if node.tag == f"{{{namespace}}}sortState":
            sorts += 1
            if sorts > 1:
                raise OOXMLInvalid("XLSX query table declares its sort state twice")
        elif node.tag == f"{{{namespace}}}extLst":
            nested = _xlsx_extension_list(nested, "query table refresh")
    return {
        "refreshDeclared": True,
        "refreshSortDeclared": bool(sorts),
        **{
            field: _xlsx_query_count(declared, namespace, group, member)
            for field, group, member in _XLSX_QUERY_TABLE_GROUPS
        },
    }


def _xlsx_query_count(
    holder: ElementTree.Element, namespace: str, group: str, member: str
) -> int | None:
    """Count what one declared field list holds, or None where none was declared.

    The list is matched as a direct child qualified in the part's own namespace,
    for the reason the refresh state itself is, and the schema gives a refresh
    one list of a kind, so a second is refused rather than read from whichever
    was written first.

    ``count`` is optional, so an absent one is the list the file wrote. Where it
    is written it is bounded before the list is counted -- a width nothing wrote
    is never walked -- and then has to agree with the list, because a count and a
    list that disagree name two different numbers of fields and nothing here
    picks between them.
    """
    groups = [node for node in holder if node.tag == f"{{{namespace}}}{group}"]
    if len(groups) > 1:
        raise OOXMLInvalid("XLSX query table refresh declares a field list twice")
    if not groups:
        return None
    count = groups[0].attrib.get("count")
    if count is not None and (
        _INDEX.fullmatch(count) is None or int(count) > _MAX_XLSX_QUERY_TABLE_FIELDS
    ):
        raise OOXMLInvalid("XLSX query table declares too many fields or no count")
    held = sum(1 for node in groups[0] if node.tag == f"{{{namespace}}}{member}")
    if held > _MAX_XLSX_QUERY_TABLE_FIELDS:
        raise OOXMLInvalid("XLSX query table declares too many fields or no count")
    if count is not None and held != int(count):
        raise OOXMLInvalid("XLSX query table field count does not match its field list")
    return held


def _xlsx_query_flag(element: ElementTree.Element, name: str) -> bool | None:
    """Read one query table's boolean attribute, or None where absent.

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
    raise OOXMLInvalid("XLSX query table flag is malformed")


@dataclass(frozen=True, slots=True)
class _XlsxFormats:
    """One workbook's cell-format provenance, resolved once for every sheet.

    ``styles`` is the number-format id each authored style index names, in the
    order ``cellXfs`` declared them, or None where the workbook declares no
    cell-format table at all. ``codes`` holds only the custom formats the
    package wrote; a built-in id is reported as the id it is, because a
    built-in's code is the consumer's own locale table rather than anything the
    file declared.

    Nothing here applies a format, converts a serial to a date, assumes a
    timezone or claims what Excel would display: the record says which format
    the cell authored and which epoch the workbook counts from, and leaves the
    decoding to whoever has a locale and a calendar to do it with.
    """

    date_system: str
    styles: tuple[int, ...] | None
    codes: dict[int, str]

    def provenance(self, cell: ElementTree.Element) -> dict[str, object]:
        """Return one cell's format provenance, refusing a style it cannot name.

        An absent ``s`` is an absent authored index, reported as the None it
        is, and SpreadsheetML defaults it to 0, so the resolved id is the first
        declared format's. A cell that names an index the workbook declared no
        format for points at no format at all, so the package is refused rather
        than read under a default the file never wrote.
        """
        raw = cell.attrib.get("s")
        if raw is None:
            index = None
        elif _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
            raise OOXMLInvalid("XLSX cell style index is malformed")
        else:
            index = int(raw)
        if self.styles is None:
            if index is not None:
                raise OOXMLInvalid(
                    "XLSX cell names a style but the workbook declares no cell formats"
                )
            format_id = 0
        else:
            resolved = 0 if index is None else index
            if resolved >= len(self.styles):
                raise OOXMLInvalid(
                    "XLSX cell style index is outside the declared cell formats"
                )
            format_id = self.styles[resolved]
        return {
            "styleIndex": index,
            "numberFormatId": format_id,
            "numberFormatCode": self.codes.get(format_id),
            "dateSystem": self.date_system,
        }


def _xlsx_formats(package: _Package, workbook: ElementTree.Element) -> _XlsxFormats:
    """Resolve the workbook's number-format and date-system provenance.

    The styles part is reached only through the workbook's own qualifying
    styles relationship and the content type OOXML declares for it, so a member
    that merely looks like the part is never read, an external target is
    refused rather than fetched, and an unrelated relationship -- the shared
    strings or a theme -- reaches nothing. Two qualifying relationships leave
    which part is meant ambiguous, so the workbook is refused rather than read
    from whichever sorts first.

    A workbook with no styles part keeps the General, format 0 provenance that
    a cell writing no style index has anyway.
    """
    date_system = _xlsx_date_system(workbook)
    related = _related_part(
        package,
        "xl/workbook.xml",
        _XLSX_STYLES_RELATIONSHIPS,
        _XLSX_STYLES_CONTENT_TYPE,
        "styles",
        owner="workbook",
        label="XLSX",
    )
    if related is None:
        return _XlsxFormats(date_system, None, {})
    root = package.xml(related[1])
    prefix, _, local = root.tag.rpartition("}")
    namespace = prefix.lstrip("{")
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX styles part uses an unsupported namespace")
    if local != "styleSheet":
        raise OOXMLInvalid("XLSX styles part is not rooted at its own style sheet")
    # The profile the root declared is the one the part is read in. Both lists
    # below are matched by their qualified names in that namespace, so a
    # lookalike written in no namespace, in a foreign one, or in the other
    # profile is not the element at all and supplies no format.
    formats = _XlsxFormats(
        date_system,
        _xlsx_cell_formats(root, namespace),
        _xlsx_number_formats(root, namespace),
    )
    _xlsx_cover(package, related[1], "styles", root)
    return formats


def _xlsx_date_system(workbook: ElementTree.Element) -> str:
    """Say which epoch the workbook counts its date serials from.

    Only a qualified ``workbookPr`` written directly on the workbook declares
    one; SpreadsheetML defaults the flag to false, so an absent declaration is
    the 1900 system rather than an unknown. Two direct declarations leave the
    epoch ambiguous -- and every stored serial with it -- so the workbook is
    refused rather than read under one of them.
    """
    declared = [node for node in workbook if node.tag in _XLSX_WORKBOOK_PROPERTIES]
    if len(declared) > 1:
        raise OOXMLInvalid("XLSX workbook declares its properties more than once")
    return "1904" if declared and _flag(declared[0], "date1904") else "1900"


def _xlsx_cell_formats(
    root: ElementTree.Element, namespace: str
) -> tuple[int, ...] | None:
    """Read ``cellXfs`` as the number-format id each style index names.

    Both the list and its entries are matched by their qualified names in the
    namespace the root declared, so an unqualified or foreign ``cellXfs`` is
    not the list and an unqualified or foreign ``xf`` inside the real list
    occupies no style index: a cell's ``s`` indexes the entries the profile
    declared, and counting something else would shift every index past it.

    Only ``numFmtId`` is read. Everything else a format declares -- its font,
    fill, border and alignment, and the named cell style it is based on -- is
    display state resolving which would mean claiming display fidelity, so none
    of it is read or resolved here.

    An empty list names no format at all, so it reads as the absent table it
    is: a cell citing an index into it is refused either way.
    """
    lists = [node for node in root if node.tag == f"{{{namespace}}}cellXfs"]
    if len(lists) > 1:
        raise OOXMLInvalid("XLSX styles part declares its cell formats more than once")
    if not lists:
        return None
    formats: list[int] = []
    for entry in lists[0]:
        if entry.tag != f"{{{namespace}}}xf":
            continue
        if len(formats) >= _MAX_XLSX_CELL_FORMATS:
            raise OOXMLInvalid("XLSX styles part declares too many cell formats")
        declared = _xlsx_format_id(entry, "cell format")
        formats.append(0 if declared is None else declared)
    return tuple(formats) or None


def _xlsx_number_formats(root: ElementTree.Element, namespace: str) -> dict[int, str]:
    """Index the custom number formats a styles part declares, by their ids.

    The list and its entries are matched by their qualified names in the
    namespace the root declared, for the reason ``cellXfs`` is: a lookalike
    written elsewhere declares nothing about how the workbook's own formats are
    spelled, so it must never supply a code a cell is then reported under.

    A format code is kept exactly as written and never parsed, applied or
    matched against a locale. Both attributes are required, so a declaration
    missing either declares no format; two declarations of one id leave every
    cell citing it ambiguous, so the part is refused rather than read under one
    of them.
    """
    lists = [node for node in root if node.tag == f"{{{namespace}}}numFmts"]
    if len(lists) > 1:
        raise OOXMLInvalid(
            "XLSX styles part declares its number formats more than once"
        )
    codes: dict[int, str] = {}
    for entry in lists[0] if lists else ():
        if entry.tag != f"{{{namespace}}}numFmt":
            continue
        if len(codes) >= _MAX_XLSX_NUMBER_FORMATS:
            raise OOXMLInvalid("XLSX styles part declares too many number formats")
        format_id = _xlsx_format_id(entry, "number format")
        code = entry.attrib.get("formatCode")
        if format_id is None or code is None:
            raise OOXMLInvalid("XLSX number format declares no id or no format code")
        if len(code) > _MAX_XLSX_FORMAT_CODE_CHARS:
            raise OOXMLInvalid("XLSX number format code exceeds the accepted bound")
        if format_id in codes:
            raise OOXMLInvalid("XLSX number format id is repeated")
        codes[format_id] = code
    return codes


def _xlsx_format_id(element: ElementTree.Element, owner: str) -> int | None:
    """Read one ``numFmtId``, or None where absent, refusing anything else.

    The type is ``xsd:unsignedInt``, so a negative, signed or oversized value
    is not a value the type has and is refused rather than clamped into one.
    """
    raw = element.attrib.get("numFmtId")
    if raw is None:
        return None
    if _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid(f"XLSX {owner} number-format id is malformed")
    return int(raw)


def _xlsx_temporal_kind(format_id: int, code: str | None) -> str | None:
    """Say whether a number format demonstrably reads its serial as a time.

    The answer is one of ``date``, ``time``, ``datetime`` and
    ``elapsed_duration``, or None where the format is not demonstrably any of
    them. It is a reading of the format a cell resolved to and nothing else:
    nothing here decodes a serial, applies the format, consults the workbook's
    epoch or reports anything, and a None answer is "not shown to be temporal"
    rather than "shown to be numeric".

    A declared custom code is what the workbook spells, so it decides on its
    own; only a format that declares none falls back to the implied table,
    where an id outside it -- including every locale-dependent one -- is no
    answer rather than a guess at one. A code longer than the bound the styles
    part is read under is not classified at all, which keeps this bounded
    wherever it is called from rather than only behind that reader.

    Ambiguity is refused rather than resolved. ``m`` is a month in a date and a
    minute in a clock, and only its neighbours say which; where they do not,
    the whole code is unclassified, because reading a month as a minute would
    misdate every cell under it. An elapsed bracket outranks everything else it
    is written with: ``[h]`` is a count of hours, never an hour of a day, so a
    code carrying one is never read as a clock or a date.
    """
    if code is None:
        return _XLSX_BUILTIN_TEMPORAL_FORMATS.get(format_id)
    if len(code) > _MAX_XLSX_FORMAT_CODE_CHARS:
        return None
    return _xlsx_temporal_code_kind(code)


def _xlsx_temporal_code_kind(code: str) -> str | None:
    """Classify one format code by the date and time fields it writes.

    The code is read once, left to right, over its first section only: later
    sections format a negative, zero or text value, and the serial a temporal
    reading would decode is the positive one. Format codes are
    case-insensitive, so the whole code is lowered first and a run of one field
    letter is read as the single field it is.

    Everything that is not a field is skipped as the decoration it is -- a
    quoted literal, a backslash-escaped character, the character after an
    underscore or an asterisk, a digit, a separator, and every bracket
    directive that is not an elapsed unit. That is what keeps a literal
    ``d``, ``m`` or ``y`` inside ``"May"``, ``\\d`` or ``[Red]`` from being
    read as a field.

    Any other letter is left unclassified rather than ignored: an unrecognised
    letter is a field or a literal this reader does not know, and a code it
    cannot account for in full is not one it can claim is temporal.
    """
    tokens: list[tuple[str, int]] = []
    text = code.lower()
    index = 0
    while index < len(text):
        char = text[index]
        if char == ";":
            break
        if char == '"':
            closing = text.find('"', index + 1)
            if closing < 0:
                return None
            index = closing + 1
        elif char in "\\_*":
            index += 2
        elif char == "[":
            closing = text.find("]", index + 1)
            if closing < 0:
                return None
            if _XLSX_ELAPSED_UNIT.fullmatch(text[index + 1 : closing]) is not None:
                tokens.append(("elapsed", closing - index - 1))
            index = closing + 1
        elif char == "a":
            marker = next(
                (one for one in _XLSX_CLOCK_MARKERS if text.startswith(one, index)),
                None,
            )
            if marker is None:
                return None
            index += len(marker)
        elif char in "dmyhs":
            run = index
            while run < len(text) and text[run] == char:
                run += 1
            tokens.append((char, run - index))
            index = run
        elif char.isalpha():
            return None
        else:
            index += 1
    if any(kind == "elapsed" for kind, _ in tokens):
        return "elapsed_duration"
    date = clock = False
    for position, (kind, run) in enumerate(tokens):
        if kind in ("d", "y"):
            date = True
        elif kind in ("h", "s"):
            clock = True
        else:
            # The one ambiguous field. Three or more ``m`` spell a month name,
            # which no clock has; otherwise the adjacent fields decide, and an
            # ``m`` beside none of them is the ambiguity this refuses.
            before = tokens[position - 1][0] if position else ""
            after = tokens[position + 1][0] if position + 1 < len(tokens) else ""
            if run >= 3 or before in ("d", "y") or after in ("d", "y"):
                date = True
            elif before == "h" or after == "s":
                clock = True
            else:
                return None
    if date and clock:
        return "datetime"
    return "date" if date else "time" if clock else None


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
    for node in _outside_extensions(root):
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
    for node in _outside_extensions(root):
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


@dataclass(frozen=True, slots=True)
class _XlsxTable:
    """One validated declared table, reduced to what a cell's locator needs.

    The range is held as its four bounds beside the text the sheet wrote, so a
    position can be placed inside the table without the range being expanded.
    ``columns`` is the table's own header for each position in range order, so a
    cell's column identity is the one the table declares rather than whatever
    the first row happens to hold.
    """

    part: str
    table_id: str
    name: str
    display_name: str
    ref: str
    first_column: int
    first_row: int
    last_column: int
    last_row: int
    header_rows: int
    totals_rows: int
    columns: tuple[tuple[str, str], ...]

    def role(self, row: int) -> str:
        """Say what one row of this table is, from the declared counts alone.

        Nothing is inferred from what a row holds: a header row is a header row
        because the table declares one, and a row that looks like a total is
        data unless the table says otherwise.
        """
        if row - self.first_row < self.header_rows:
            return "header"
        if self.last_row - row < self.totals_rows:
            return "totals"
        return "data"

    def place(self, column: int, row: int) -> dict[str, object]:
        """Describe one in-range position as the table's own coordinates."""
        column_id, column_name = self.columns[column - self.first_column]
        return {
            "tablePart": self.part,
            "tableId": self.table_id,
            "tableName": self.name,
            "tableDisplayName": self.display_name,
            "tableRange": self.ref,
            "tableRow": row - self.first_row + 1,
            "tableColumn": column - self.first_column + 1,
            "tableColumnId": column_id,
            "tableColumnName": column_name,
            "tableRole": self.role(row),
        }


def _xlsx_tables(
    package: _Package, part: str, root: ElementTree.Element
) -> tuple[list[dict[str, object]], dict[int, tuple[_XlsxTable, ...]]]:
    """Inventory one sheet's declared tables and index what they cover.

    A table is reached on the schema's own route and no other: the
    ``tableParts`` directly below the worksheet, a ``tablePart`` directly below
    that, both qualified in the worksheet's own SpreadsheetML namespace, and
    that element's own relationship id. An unqualified, foreign or further
    nested lookalike declares nothing, and no table path is derived from the
    sheet's own name, so a member that merely looks like a table part is never
    read.

    Every table is counted before any part is opened, and each table's columns
    are bounded by its own declared count before the list is read, so a width
    nothing wrote is never expanded. Declared tables are rectangles over one
    grid, so two that intersect leave a cell in two tables and the sheet is
    refused rather than read under one of them. Identity is checked the same
    way, per sheet: a repeated id, name or display name leaves a cell citing a
    table that is two tables.

    The lookup that comes back is sparse by column -- each table is listed under
    the columns it covers, never expanded into positions -- so a table over a
    whole column costs one entry rather than a million.
    """
    # The rest of the sheet reader matches local names, because a sheet's
    # vocabulary is settled by the part it was reached through. This route opens
    # another part, so it is held to the sheet's own namespace and that namespace
    # has to be SpreadsheetML: an unqualified or foreign lookalike declares
    # nothing and stays transparent, exactly as a nested one does.
    namespace, _, _ = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        return [], {}
    declared = [node for node in root if node.tag == f"{{{namespace}}}tableParts"]
    if not declared:
        return [], {}
    if len(declared) > 1:
        raise OOXMLInvalid("XLSX worksheet declares more than one table-part list")
    # Counted before the first part is opened: a sheet's declarations are the
    # work it asks for whether or not any of them resolves.
    member = f"{{{namespace}}}tablePart"
    parts = [node for node in declared[0] if node.tag == member]
    if len(parts) > _MAX_XLSX_TABLES:
        raise OOXMLInvalid("XLSX worksheet declares too many tables")
    records: list[dict[str, object]] = []
    tables: list[_XlsxTable] = []
    named: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for node in parts:
        relation_id = _relationship_id(node.attrib)
        if not relation_id:
            raise OOXMLInvalid("XLSX table part declaration names no relationship")
        if relation_id in named:
            raise OOXMLInvalid("XLSX table part declaration is repeated")
        named.add(relation_id)
        table, record = _xlsx_table(
            package,
            _referenced_part(
                package,
                part,
                relation_id,
                _TABLE_RELATIONSHIPS,
                _TABLE_CONTENT_TYPE,
                "table",
                "XLSX",
            ),
            relation_id,
        )
        for claim in (
            ("id", table.table_id),
            # Folded: SpreadsheetML names a table once however it is cased, so
            # two spellings of one name are one name.
            ("name", table.name.casefold()),
            ("displayName", table.display_name.casefold()),
        ):
            if claim in identities:
                raise OOXMLInvalid("XLSX table identity is repeated")
            identities.add(claim)
        if any(
            table.first_column <= other.last_column
            and other.first_column <= table.last_column
            and table.first_row <= other.last_row
            and other.first_row <= table.last_row
            for other in tables
        ):
            raise OOXMLInvalid("XLSX declared tables overlap")
        tables.append(table)
        records.append(record)
    covered: dict[int, list[_XlsxTable]] = {}
    for table in tables:
        for column in range(table.first_column, table.last_column + 1):
            covered.setdefault(column, []).append(table)
    return records, {column: tuple(found) for column, found in covered.items()}


def _xlsx_table(
    package: _Package, part: str, relation_id: str
) -> tuple[_XlsxTable, dict[str, object]]:
    """Validate one table part, returning what it covers and what it declares.

    Everything reported is what the part already stored: the range as written,
    the table's own identity, the declared header and totals counts, and each
    column's header beside whatever calculated-column or totals-row markup it
    carries. A formula is reported as the text it is and never evaluated, a
    totals-row function is the name the file wrote rather than an applied one,
    and no cell is materialised for any position the range covers.

    The declared column count has to match both the list below it and the width
    of the range: a table whose header list and grid disagree declares no
    columns anyone can place a cell in. A repeated column id or name would
    leave two positions with one header, so the table is refused.
    """
    root = _xlsx_table_root(package, part)
    ref = root.attrib.get("ref")
    if not ref:
        raise OOXMLInvalid("XLSX table declares no range")
    first_column, first_row, last_column, last_row = _cell_range(ref)
    table_id = root.attrib.get("id", "")
    if _DIGITS.fullmatch(table_id) is None:
        raise OOXMLInvalid("XLSX table id is malformed")
    display_name = _xlsx_table_name(root, "displayName")
    if display_name is None:
        raise OOXMLInvalid("XLSX table declares no display name")
    # The schema defaults ``name`` to the display name, so an absent one is the
    # display name rather than a missing identity.
    name = _xlsx_table_name(root, "name") or display_name
    header_rows = _xlsx_table_rows(root, "headerRowCount", 1)
    totals_rows = _xlsx_table_rows(root, "totalsRowCount", 0)
    if header_rows + totals_rows > last_row - first_row + 1:
        raise OOXMLInvalid("XLSX table header and totals rows exceed its range")
    # The root carried the vocabulary check, so the list and its members are
    # held to that same namespace: a foreign holder or member spelling the same
    # local name declares nothing the table can be read from.
    scope = root.tag.removesuffix("table")
    holders = [node for node in root if node.tag == f"{scope}tableColumns"]
    if len(holders) != 1:
        raise OOXMLInvalid("XLSX table does not hold exactly one column list")
    count = holders[0].attrib.get("count", "")
    if _DIGITS.fullmatch(count) is None:
        raise OOXMLInvalid("XLSX table column list declares no readable count")
    if int(count) > _MAX_XLSX_TABLE_COLUMNS:
        raise OOXMLInvalid("XLSX table has too many columns")
    columns = [node for node in holders[0] if node.tag == f"{scope}tableColumn"]
    if len(columns) != int(count):
        raise OOXMLInvalid("XLSX table column count does not match its column list")
    if len(columns) != last_column - first_column + 1:
        raise OOXMLInvalid("XLSX table column list does not match its range")
    entries: list[dict[str, object]] = []
    headers: list[tuple[str, str]] = []
    identifiers: set[str] = set()
    names: set[str] = set()
    metadata = 0
    for column in columns:
        column_id = column.attrib.get("id", "")
        if _DIGITS.fullmatch(column_id) is None:
            raise OOXMLInvalid("XLSX table column id is malformed")
        column_name = _xlsx_table_name(column, "name")
        if column_name is None:
            raise OOXMLInvalid("XLSX table column declares no name")
        if column_id in identifiers or column_name.casefold() in names:
            raise OOXMLInvalid("XLSX table column id or name is repeated")
        identifiers.add(column_id)
        names.add(column_name.casefold())
        entry: dict[str, object] = {
            **_XLSX_TABLE_COLUMN,
            "id": column_id,
            "name": column_name,
            "totalsRowLabel": column.attrib.get("totalsRowLabel"),
            "totalsRowFunction": column.attrib.get("totalsRowFunction"),
            "calculatedColumnFormula": _xlsx_table_formula(
                column, f"{scope}calculatedColumnFormula"
            ),
            "totalsRowFormula": _xlsx_table_formula(column, f"{scope}totalsRowFormula"),
        }
        # One record carries the whole table's declared metadata, so the bound
        # is taken across the table rather than per column: a single oversized
        # formula and a thousand ordinary ones are both refused here.
        metadata += sum(len(v) for v in entry.values() if isinstance(v, str))
        if metadata > _MAX_XLSX_TABLE_METADATA_CHARS:
            raise OOXMLInvalid("XLSX table metadata exceeds the accepted bound")
        entries.append(entry)
        headers.append((column_id, column_name))
    _xlsx_cover(package, part, "table", root)
    return (
        _XlsxTable(
            part,
            table_id,
            name,
            display_name,
            ref,
            first_column,
            first_row,
            last_column,
            last_row,
            header_rows,
            totals_rows,
            tuple(headers),
        ),
        {
            "kind": "table",
            "relationshipId": _clip(relation_id, _MAX_META_CHARS),
            "tablePart": part,
            "id": table_id,
            "name": name,
            "displayName": display_name,
            "ref": ref,
            "headerRowCount": header_rows,
            "totalsRowCount": totals_rows,
            "columnCount": len(entries),
            "columns": entries,
        },
    )


def _xlsx_table_root(package: _Package, part: str) -> ElementTree.Element:
    """Parse one table part, refusing a root that is not a table.

    The declared content type says what the part is; the root element is where
    the part says so itself, in one of the two namespaces OOXML defines for
    SpreadsheetML. Anything else is not the vocabulary the declaration
    promised, so the part is refused rather than read for whatever happens to
    be in it.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    if namespace.lstrip("{") not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX table part uses an unsupported namespace")
    if local != "table":
        raise OOXMLInvalid("XLSX table part is not rooted at its own table")
    return root


def _xlsx_table_name(element: ElementTree.Element, attribute: str) -> str | None:
    """Read one table or column identity attribute, or None where absent.

    An identity is never clipped: two over-long names shortened to the same
    text would be one name, so one outside the bound is refused rather than cut
    down to fit. An empty one is no identity at all.
    """
    value = element.attrib.get(attribute)
    if value is None:
        return None
    if not value or len(value) > _MAX_META_CHARS:
        raise OOXMLInvalid("XLSX table name is empty or exceeds the accepted bound")
    return value


def _xlsx_table_rows(element: ElementTree.Element, attribute: str, default: int) -> int:
    """Read a declared header- or totals-row count, refusing anything else.

    SpreadsheetML defines both as 0 or 1, and a row's role is derived from
    these two alone, so a count the format does not define is refused rather
    than rounded into one it does.
    """
    value = element.attrib.get(attribute)
    if value is None:
        return default
    if value not in {"0", "1"}:
        raise OOXMLInvalid("XLSX table row count is not a defined value")
    return int(value)


def _xlsx_table_formula(column: ElementTree.Element, tag: str) -> str | None:
    """Return the declared formula text one column holds, exactly as written.

    The tag is qualified in the column's own namespace, so a foreign element
    spelling the same local name supplies no formula. A column declares each
    formula at most once, so two leave which one it calculates ambiguous.
    Nothing here evaluates the text or resolves anything it names, and an empty
    declaration stays distinguishable from none at all.
    """
    held = [node for node in column if node.tag == tag]
    if len(held) > 1:
        raise OOXMLInvalid("XLSX table column declares a formula twice")
    return (held[0].text or "") if held else None


def _xlsx_table_place(
    covered: dict[int, tuple[_XlsxTable, ...]], position: tuple[int, int]
) -> dict[str, object] | None:
    """Return the declared-table keys one written cell's position sits under.

    Only a position the sheet already wrote a cell for is ever asked about, and
    declared tables cannot overlap, so at most one table covers it.
    """
    column, row = position
    return next(
        (
            table.place(column, row)
            for table in covered.get(column, ())
            if table.first_row <= row <= table.last_row
        ),
        None,
    )


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
    for node in _outside_extensions(root):
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


def _xlsx_comments(
    package: _Package, part: str
) -> list[tuple[str, dict[str, object]]]:
    """Return one worksheet's classic comments as (text, provenance), in order.

    Order is the order the comment list wrote them in; nothing is sorted by
    anchor, author or position. The part is reached only through the
    worksheet's own relationships, by the one relationship type OOXML defines
    for a comments part and the content type SpreadsheetML declares for its
    own, so a slide's comment list, an external target and a member that merely
    looks like the part all reach nothing. Two qualifying relationships leave
    which list is meant ambiguous, so the sheet is refused.

    Nothing here opens the legacy drawing a worksheet names beside its
    comments: the VML it holds carries a comment's on-screen box, never its
    text, and a comment is placed by the ``ref`` it already declares. No shape,
    image, hyperlink or other external target is reached from here either.

    A comment on a hidden sheet is still a comment, so visibility is the
    caller's ``annotation`` either way and the sheet's own state is recorded in
    the locator rather than folded into it.
    """
    related = _related_part(
        package,
        part,
        _COMMENTS_RELATIONSHIPS,
        _XLSX_COMMENTS_CONTENT_TYPE,
        "comments",
        owner="worksheet",
        label="XLSX",
    )
    if related is None:
        return []
    relation_id, comments_part = related
    root = package.xml(comments_part)
    namespace, _, local = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    # The declared content type says what the part is; the root is where the
    # part says so itself, in one of the two namespaces OOXML defines for
    # SpreadsheetML. Everything below is then held to that same namespace, so a
    # lookalike spelling one of these local names in another namespace -- or in
    # none -- declares nothing the part can be read from.
    if namespace not in _SPREADSHEETML:
        raise OOXMLInvalid("XLSX comments part uses an unsupported namespace")
    if local != "comments":
        raise OOXMLInvalid("XLSX comments part is not rooted at its own list")
    scope = f"{{{namespace}}}"
    authors: list[str] | None = None
    listed: ElementTree.Element | None = None
    for node in root:
        if node.tag == f"{scope}authors":
            if authors is not None:
                raise OOXMLInvalid("XLSX comments part declares two author lists")
            authors = _xlsx_comment_authors(node, scope)
        elif node.tag == f"{scope}commentList":
            if listed is not None:
                raise OOXMLInvalid("XLSX comments part declares two comment lists")
            listed = node
        elif node.tag != f"{scope}extLst":
            raise OOXMLInvalid("XLSX comments part holds an unsupported element")
    # Every comment cites an author by position in this one list, so a part
    # without it reports authors nobody can resolve.
    if authors is None:
        raise OOXMLInvalid("XLSX comments part declares no author list")
    # SpreadsheetML requires the list itself, so a part declaring none -- or
    # declaring one only below an extension, which is not this element -- is not
    # a comments part the reader can read, however empty the list would be.
    if listed is None:
        raise OOXMLInvalid("XLSX comments part declares no comment list")
    result: list[tuple[str, dict[str, object]]] = []
    anchors: set[tuple[int, int, int, int]] = set()
    runs = 0
    for index, entry in enumerate(listed):
        if entry.tag != f"{scope}comment":
            raise OOXMLInvalid("XLSX comment list holds an unsupported element")
        if index >= _MAX_XLSX_COMMENTS:
            raise OOXMLInvalid("XLSX worksheet has too many comments")
        reference = entry.attrib.get("ref")
        if not reference:
            raise OOXMLInvalid("XLSX comment declares no reference")
        # Validated as a coordinate or range, then kept exactly as written. Two
        # comments over one anchor leave the anchor holding two comments and
        # nothing here picks between them; the bounds are what is compared, so
        # ``A1`` and ``A1:A1`` are the one anchor they name.
        anchor = _cell_range(reference)
        if anchor in anchors:
            raise OOXMLInvalid("XLSX comment anchor is repeated")
        anchors.add(anchor)
        author_id = entry.attrib.get("authorId", "")
        if _INDEX.fullmatch(author_id) is None or int(author_id) > _MAX_INDEX:
            raise OOXMLInvalid("XLSX comment author id is malformed")
        if int(author_id) >= len(authors):
            raise OOXMLInvalid("XLSX comment names no author")
        text, runs = _xlsx_comment_text(entry, scope, runs)
        result.append(
            (
                text,
                {
                    "part": comments_part,
                    "relationshipId": _clip(relation_id, _MAX_META_CHARS),
                    "commentForm": "classic",
                    # Zero-based, the position the list wrote it at: a
                    # SpreadsheetML comment carries no id of its own.
                    "commentIndex": index,
                    "cell": reference,
                    "authorId": int(author_id),
                    "author": authors[int(author_id)],
                    # Stated so no consumer reads the name above as identity.
                    "authorAuthenticated": False,
                },
            )
        )
    _xlsx_cover(package, comments_part, "comments", root)
    return result


def _xlsx_comment_authors(holder: ElementTree.Element, scope: str) -> list[str]:
    """Read a comments part's author list, in the order it declares them.

    SpreadsheetML gives an author no identifier of its own: an author is a bare
    string and a comment cites it by its zero-based position here, so there is
    no id to repeat and two entries spelling one name are two authors rather
    than a duplicate. Each name is a claim the file makes about itself, never
    evidence of who commented. An author is an ``xsd:string``, so markup inside
    one is not a structure to descend into.
    """
    authors: list[str] = []
    for node in holder:
        if node.tag != f"{scope}author" or len(node):
            raise OOXMLInvalid("XLSX comment author list holds an unsupported element")
        if len(authors) >= _MAX_XLSX_COMMENT_AUTHORS:
            raise OOXMLInvalid("XLSX comment author list is too large")
        authors.append(_clip(node.text or "", _MAX_META_CHARS))
    return authors


def _xlsx_comment_text(
    entry: ElementTree.Element, scope: str, runs: int
) -> tuple[str, int]:
    """Read one comment's rich text, concatenating its stored runs in order.

    A body is ``CT_Rst``: the string stored directly, or a run list each of
    whose runs stores its own piece of it, so the text that comes back is the
    pieces the part stored concatenated in the order it stored them. Nothing
    here reads formatting, and a phonetic run is a reading of the body rather
    than part of it, so it is not concatenated into one.

    A second body leaves the comment with two texts and nothing here picks
    between them. ``runs`` is carried in and out so the bound holds across the
    whole part rather than per comment.
    """
    body: ElementTree.Element | None = None
    for node in entry:
        if node.tag == f"{scope}text":
            if body is not None:
                raise OOXMLInvalid("XLSX comment declares two bodies")
            body = node
        elif node.tag != f"{scope}commentPr":
            raise OOXMLInvalid("XLSX comment holds an unsupported element")
    if body is None:
        raise OOXMLInvalid("XLSX comment declares no body")
    pieces: list[str] = []
    for node in body:
        if node.tag == f"{scope}t":
            pieces.append(node.text or "")
        elif node.tag == f"{scope}r":
            runs += 1
            if runs > _MAX_XLSX_COMMENT_RUNS:
                raise OOXMLInvalid("XLSX comment runs exceed the accepted bound")
            pieces.extend(child.text or "" for child in node if child.tag == f"{scope}t")
    text = "".join(pieces)
    if len(text) > _MAX_XLSX_COMMENT_CHARS:
        raise OOXMLInvalid("XLSX comment text exceeds the accepted bound")
    return text, runs


def _xlsx_threaded_root(
    package: _Package, part: str, expected: str
) -> ElementTree.Element:
    """Parse one threaded-comment part, refusing a root that is not its own.

    The declared content type says what the part is; the root element is where
    the part says so itself, in the one namespace the extension defines. A root
    in any other namespace -- the SpreadsheetML namespaces the classic list is
    written in among them -- or under any other name is not the vocabulary the
    declaration promised, so the part is refused rather than read for whatever
    elements happen to be in it.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    if namespace.lstrip("{") != _XLSX_THREADED_MAIN:
        raise OOXMLInvalid("XLSX threaded comment part uses an unsupported namespace")
    if local != expected:
        raise OOXMLInvalid("XLSX threaded comment part is not rooted at its own list")
    return root


def _xlsx_threaded_guid(
    element: ElementTree.Element, name: str, label: str
) -> str | None:
    """Return one ``ST_Guid`` attribute as written, or None where it is absent.

    A written value is validated and then kept exactly as written -- never
    recased, rebraced or renumbered -- so an id stays the one the file declared
    and a reference to one stays the reference the file wrote.

    The grammar carries four of these at three positions, so ``label`` names
    which one a refusal is about: a person's own id is not a comment's, and a
    comment citing a person is not the same claim as one naming its parent.
    """
    raw = element.attrib.get(name)
    if raw is not None and _GUID.fullmatch(raw) is None:
        raise OOXMLInvalid(f"XLSX {label} is malformed")
    return raw


def _xlsx_threaded_claim(element: ElementTree.Element, name: str) -> str | None:
    """Return one free-text attribute clipped, or None where it is not written.

    Nothing here parses or resolves the value -- a timestamp stays the string
    the package wrote, never a moment anything happened.
    """
    value = element.attrib.get(name)
    return None if value is None else _clip(value, _MAX_META_CHARS)


def _xlsx_threaded_done(entry: ElementTree.Element) -> bool | None:
    """Whether a thread was marked resolved, or ``None`` where it says nothing.

    The mark is a tri-state claim rather than a boolean: a thread the file never
    marked is not the same claim as one the file marked unresolved, so an absent
    attribute stays absent rather than folding into a false. The type is closed,
    so a value outside it is refused rather than read as whichever of the two it
    resembles.
    """
    value = entry.attrib.get("done")
    if value is None:
        return None
    if value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise OOXMLInvalid("XLSX threaded comment resolution mark is malformed")


def _xlsx_extension_list(seen: bool, owner: str) -> bool:
    """Admit one direct ``extLst`` below ``owner``, refusing a second.

    The schema gives each of the extension's own elements a single extension
    point, so a second one leaves two sets of extensions at one position and
    nothing here picks between them. The one is left alone -- nothing inside it
    is read -- which is what makes a second a refusal rather than more of the
    same. Each position is counted on its own: a point below a list says nothing
    about the points below its records.
    """
    if seen:
        raise OOXMLInvalid(f"XLSX {owner} holds more than one extension list")
    return True


def _xlsx_extension_lists(
    root: ElementTree.Element, owner: str
) -> list[tuple[str, list[ElementTree.Element]]]:
    """Find the extension lists a part may carry, as (owner path, entries).

    ``extLst`` is where a producer keeps what the schema has no element for --
    conditional-formatting extensions, sparklines, slicers, a Power Query
    definition -- and this reader understands none of it. This is the walk for
    the SpreadsheetML parts; a drawing and a chart are written in DrawingML and
    are walked by ``_xlsx_drawing_extension_lists`` instead, under the same
    bounds, the same refusal and the same path. Which positions of a
    part may hold one is ``_XLSX_EXTENSION_VOCABULARY``'s, per kind of part: the
    walk goes down only the named steps, each a direct child qualified in the
    root's own namespace, and an ``extLst`` is taken only directly below an owner
    the vocabulary names. Its entries are in the root's namespace too, except
    where ``_XLSX_EXTENSION_ENTRY_NAMESPACES`` says the schema types the list
    from another vocabulary. A foreign, unqualified or otherwise nested lookalike
    declares nothing, and no entry is ever entered, so what an extension holds
    cannot be reached from here.

    The path is the structural address of the owner: each step's local name and
    its zero-based ordinal among the like-named siblings qualified the same way,
    so two lists on two columns of one table, or on a column and the table, are
    told apart without a name, a formula or any other source text being copied.
    The root is ``/``. The lists come back in the order they are authored.

    The schema gives each owner one list, so a second is refused, and the
    entries are counted against the per-list and per-part bounds here, before
    any record is built.
    """
    namespaces, paths = _XLSX_EXTENSION_VOCABULARY[owner]
    namespace = root.tag.rpartition("}")[0].lstrip("{")
    if namespace not in namespaces:
        return []
    scope = f"{{{namespace}}}"
    entry_namespace = _XLSX_EXTENSION_ENTRY_NAMESPACES.get(owner, namespace)
    entry_tag = f"{{{entry_namespace}}}ext"
    wanted = frozenset(paths)
    onward = frozenset(path[: n + 1] for path in paths for n in range(len(path)))
    found: list[tuple[str, list[ElementTree.Element]]] = []
    total = 0
    list_tag = f"{scope}extLst"

    def visit(
        node: ElementTree.Element,
        steps: tuple[str, ...],
        where: tuple[tuple[str, int], ...],
    ) -> None:
        nonlocal total
        listed = False
        ordinals: dict[str, int] = {}
        for child in node:
            if child.tag == list_tag:
                if steps not in wanted:
                    continue
                listed = _xlsx_extension_list(
                    listed, " ".join((owner, *steps[-1:]))
                )
                entries = _xlsx_extension_entries(child, entry_tag, owner)
                total += len(entries)
                if total > _MAX_XLSX_PART_EXTENSIONS:
                    raise OOXMLInvalid(f"XLSX {owner} declares too many extensions")
                found.append(
                    ("".join(f"/{n}[{i}]" for n, i in where) or "/", entries)
                )
            elif child.tag.startswith(scope):
                name = child.tag[len(scope) :]
                if (*steps, name) in onward:
                    ordinal = ordinals.get(name, 0)
                    ordinals[name] = ordinal + 1
                    visit(child, (*steps, name), (*where, (name, ordinal)))

    visit(root, (), ())
    return found


def _xlsx_extension_entries(
    node: ElementTree.Element, entry_tag: str, owner: str
) -> list[ElementTree.Element]:
    """The entries one ``extLst`` declares, counted before any is reported.

    An entry is an ``ext`` qualified exactly as ``entry_tag``: the namespace the
    schema types the list's entries from, which is not always the list's own.
    Anything else inside the list belongs to another vocabulary and declares no
    entry. Nothing below an entry is looked at here or anywhere past here.
    """
    entries = [item for item in node if item.tag == entry_tag]
    if len(entries) > _MAX_XLSX_EXTENSIONS:
        raise OOXMLInvalid(f"XLSX {owner} declares too many extensions")
    return entries


@dataclass(slots=True)
class _XlsxDrawingScan:
    """One element of a DrawingML part being scanned for the list it may hold.

    ``lists`` is the qualified ``extLst`` names this element's own position
    admits, or None where the schema puts no list there; ``label`` is what a
    refusal calls the position, ``where`` is the structural path down to it,
    ``ordinals`` counts the like-named children already entered and ``listed``
    says whether the one list its position allows has been seen.
    """

    children: Iterator[ElementTree.Element]
    lists: frozenset[str] | None
    label: str
    where: tuple[tuple[str, int], ...]
    ordinals: dict[str, int] = field(default_factory=dict)
    listed: bool = False


def _xlsx_drawing_extension_lists(
    root: ElementTree.Element, owner: str
) -> list[tuple[str, list[ElementTree.Element]]]:
    """Find the extension lists one DrawingML part carries, as (owner path, entries).

    The worksheet-drawing and chart parts a sheet reaches are DrawingML rather
    than SpreadsheetML, and their vocabularies recurse -- a group shape holds
    group shapes, a plot area holds charts holding series holding their own
    references -- so an owner is named rather than placed, which is the one
    difference from the SpreadsheetML walk above. Children qualified in the
    part's own validated namespace or either DrawingML-main profile are
    descended, and an ``extLst`` is taken only directly below an element its
    qualified vocabulary names as an owner. A part-vocabulary owner uses the
    list namespace its schema type declares; a DrawingML-main owner uses an
    ``extLst`` in that owner's exact namespace. Foreign and unqualified children
    are never entered, so a SmartArt, VML or other payload carried by
    ``a:graphicData`` remains outside this walk. No ``extLst`` or ``ext`` is
    ever entered, so extension payloads remain opaque.

    The structural path, the one-list-per-position refusal and the per-list and
    per-part bounds are the SpreadsheetML walk's, for its reasons, and the lists
    come back where each was authored: an element's own trailing list after the
    ones its children hold, exactly as a table's does there.
    """
    namespaces, listed = _XLSX_DRAWING_EXTENSION_VOCABULARY[owner]
    namespace = root.tag.rpartition("}")[0].lstrip("{")
    if namespace not in namespaces:
        return []
    scope = f"{{{namespace}}}"
    owners = {
        f"{scope}{name}": frozenset(
            f"{{{uri}}}extLst"
            for uri in (group if group is not None else (namespace,))
        )
        for name, group in listed.items()
    }
    entered_namespaces = frozenset((namespace, *_DRAWING_MAIN))

    def extension_lists(tag: str) -> frozenset[str] | None:
        """Return the schema-qualified list this exact owner may carry."""
        prefix, brace, name = tag.rpartition("}")
        if not brace:
            return None
        owner_namespace = prefix.lstrip("{")
        if (
            owner_namespace in _DRAWING_MAIN
            and name in _DRAWING_MAIN_EXTENSION_OWNERS
        ):
            return frozenset((f"{{{owner_namespace}}}extLst",))
        return owners.get(tag)

    found: list[tuple[str, list[ElementTree.Element]]] = []
    total = 0
    # Scanned with an explicit stack rather than by recursion: both
    # vocabularies nest as deep as a producer writes them, and a part that
    # nested its groups past the interpreter's own limit would leave the walk
    # rather than the package refused.
    stack = [_XlsxDrawingScan(iter(root), extension_lists(root.tag), owner, ())]
    while stack:
        frame = stack[-1]
        for child in frame.children:
            if frame.lists is not None and child.tag in frame.lists:
                frame.listed = _xlsx_extension_list(frame.listed, frame.label)
                prefix, _, _ = child.tag.rpartition("}")
                entries = _xlsx_extension_entries(child, f"{prefix}}}ext", owner)
                total += len(entries)
                if total > _MAX_XLSX_PART_EXTENSIONS:
                    raise OOXMLInvalid(f"XLSX {owner} declares too many extensions")
                found.append(
                    ("".join(f"/{n}[{i}]" for n, i in frame.where) or "/", entries)
                )
            else:
                prefix, brace, name = child.tag.rpartition("}")
                if not brace or prefix.lstrip("{") not in entered_namespaces:
                    continue
                # Anything called an extension is left alone whether or not it
                # is the list this position allows. This also keeps the ordinary
                # extents ``a:xfrm/a:ext`` and ``xdr:oneCellAnchor/xdr:ext`` out
                # of extension-entry handling: an ``ext`` is an extension entry
                # only as a direct qualified child of a recognized list above.
                if name in ("extLst", "ext"):
                    continue
                ordinal = frame.ordinals.get(name, 0)
                frame.ordinals[name] = ordinal + 1
                stack.append(
                    _XlsxDrawingScan(
                        iter(child),
                        extension_lists(child.tag),
                        f"{owner} {name}",
                        (*frame.where, (name, ordinal)),
                    )
                )
                break
        else:
            stack.pop()
    return found


def _xlsx_extension_records(
    part: str, owner: str, lists: list[tuple[str, list[ElementTree.Element]]]
) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Build the coverage records for lists already found and already bounded.

    Each entry is reported as an entry and nothing more: the owning part, the
    structural path of its list, its authored position, and the URI that names
    the extension where that URI is a GUID. No attribute but ``uri`` is read,
    no child is visited, and no text, formula, query, target or cached value can
    leave through here, which is what keeps an entry from ever being a cell or
    any other searchable content.

    The schema allows any number of entries in any order and does not make
    ``uri`` unique, so a repeated URI -- exact, differing in case or in
    whitespace -- is not a fault: every entry is its own record, in the order
    authored, told apart by its own ``extensionIndex`` within its list.
    """
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    for path, entries in lists:
        for index, entry in enumerate(entries):
            raw = entry.attrib.get("uri")
            uri = None if raw is None else " ".join(raw.split())
            guid = uri is not None and _XLSX_EXTENSION_GUID.fullmatch(uri) is not None
            records.append(
                (
                    {"part": part, "ownerPath": path, "extensionIndex": index},
                    {
                        "kind": "unsupportedExtension",
                        "owner": owner,
                        "ownerPart": part,
                        "ownerPath": path,
                        "extensionIndex": index,
                        "uri": uri if guid else None,
                        "uriForm": None if uri is None else "guid" if guid else "other",
                        # Stated on every record, as on the other inventories:
                        # the payload was neither read nor run, and nothing was
                        # reached.
                        "payloadInterpreted": False,
                        "refreshPerformed": False,
                        "externalAccessPerformed": False,
                    },
                )
            )
    return records


# The roots a sheet part may be written at, each its own owner in the vocabulary.
# The sheet is reached by a workbook ``sheet`` declaration, which names a
# worksheet, a chart sheet or a dialog sheet alike; a root that is none of them
# is not a sheet's vocabulary and declares nothing.
_XLSX_SHEET_OWNERS: Final = frozenset({"worksheet", "chartsheet", "dialogsheet"})


@dataclass(slots=True)
class _XlsxReached:
    """One reached part's bounded extension lists, and every scope that reached it.

    Only the lists are held: a record is built when the part is reported, after
    every part's lists have been counted against the bounds.
    """

    owner: str
    lists: list[tuple[str, list[ElementTree.Element]]]
    scopes: set[int | None] = field(default_factory=set)


@dataclass(slots=True)
class _XlsxCoverage:
    """The extension entries of every part the reader reached, once per part.

    A part is reported where the reader that parses it has accepted it, after
    that reader's own validation, so only a part reached by its validated
    relationship ever lands here and a member that merely looks like one never
    does. ``scope`` is the sheet being read -- an index into the workbook's
    sheet list -- or None while the workbook's own parts are.

    A part is one physical set of entries however many times it is reached, so
    it is built on the first reach and later reaches only add their scope.
    That is what settles visibility: a part reached by the workbook is the
    workbook's and always in sight, a part reached by one sheet is that sheet's
    and takes its state, and a part two or more sheets share is shared and is
    hidden only where every sheet that reaches it is.
    """

    scope: int | None = None
    total: int = 0
    reached: dict[str, _XlsxReached] = field(default_factory=dict)

    def claim(
        self, owner: str, root: ElementTree.Element
    ) -> list[tuple[str, list[ElementTree.Element]]]:
        """Find and bound the lists one part declares, building nothing.

        A SpreadsheetML part is walked down its matrix of positions and a
        DrawingML one down the owners its vocabulary names, which is the one
        difference between them; a part of either kind lands here the same way.

        The per-list and per-part bounds are taken while the lists are found,
        and the global one here against the running total of every part -- a
        root, a nested owner, a drawing, a chart or the shared strings alike --
        so every bound is settled before a single record is constructed from any
        of them.
        """
        lists = (
            _xlsx_drawing_extension_lists(root, owner)
            if owner in _XLSX_DRAWING_EXTENSION_VOCABULARY
            else _xlsx_extension_lists(root, owner)
        )
        self.total += sum(len(entries) for _path, entries in lists)
        if self.total > MAX_SEGMENTS:
            raise OOXMLInvalid("OOXML extraction has too many segments")
        return lists

    def reach(self, part: str, owner: str, root: ElementTree.Element) -> None:
        held = self.reached.get(part)
        if held is None:
            held = self.reached[part] = _XlsxReached(owner, self.claim(owner, root))
        held.scopes.add(self.scope)


def _xlsx_cover(
    package: _Package, part: str, owner: str, root: ElementTree.Element
) -> None:
    """Report the extension lists one accepted part holds, if anything is listening."""
    if package.coverage is not None:
        package.coverage.reach(part, owner, root)


def _xlsx_persons(package: _Package) -> dict[str, dict[str, object]]:
    """Index the workbook's threaded-comment persons by the id each claims.

    The person list is the workbook's own part, reached only through the
    workbook's own relationships, so one table serves every sheet. Every field
    is recorded exactly as the package wrote it and nothing here checks that any
    of it names a real person: a person record is a claim the file makes about
    itself, never evidence of who commented, and a user or provider id is no
    more than another such claim.

    A ``displayName`` is the one field the schema requires, so a record without
    one declares no person at all and the table is refused rather than indexed
    under a name nothing wrote; an authored empty one is kept as written.

    Ids are indexed case-folded because only a GUID's hex digits vary in case,
    so two records whose ids differ in nothing else claim one identity and leave
    every comment citing it ambiguous; the table is refused rather than read
    under one of them.
    """
    related = _related_part(
        package,
        "xl/workbook.xml",
        _XLSX_PERSONS_RELATIONSHIPS,
        _XLSX_PERSONS_CONTENT_TYPE,
        "person",
        owner="workbook",
        label="XLSX",
    )
    if related is None:
        return {}
    relation_id, part = related
    root = _xlsx_threaded_root(package, part, "personList")
    scope = f"{{{_XLSX_THREADED_MAIN}}}"
    persons: dict[str, dict[str, object]] = {}
    extension = False
    for record in root:
        if record.tag == f"{scope}extLst":
            extension = _xlsx_extension_list(extension, "person list")
            continue
        if record.tag != f"{scope}person":
            raise OOXMLInvalid("XLSX person list holds an unsupported element")
        # A person is its attributes: the schema gives it no content but the one
        # extension point, so a person below a person declares no second person
        # and a lookalike left unqualified declares no point.
        person_extension = False
        for node in record:
            if node.tag != f"{scope}extLst":
                raise OOXMLInvalid("XLSX person holds an unsupported element")
            person_extension = _xlsx_extension_list(person_extension, "person")
        if len(persons) >= _MAX_XLSX_THREADED_RECORDS:
            raise OOXMLInvalid("XLSX person list is too large")
        person_id = _xlsx_threaded_guid(record, "id", "person id")
        if person_id is None:
            raise OOXMLInvalid("XLSX person declares no id")
        if person_id.casefold() in persons:
            raise OOXMLInvalid("XLSX person id is repeated")
        display_name = _xlsx_threaded_claim(record, "displayName")
        if display_name is None:
            raise OOXMLInvalid("XLSX person declares no display name")
        persons[person_id.casefold()] = {
            "personPart": part,
            "personRelationshipId": _clip(relation_id, _MAX_META_CHARS),
            "personDisplayName": display_name,
            "personUserId": _xlsx_threaded_claim(record, "userId"),
            "personProviderId": _xlsx_threaded_claim(record, "providerId"),
            # Stated so no consumer reads the claims above as identity.
            "authorAuthenticated": False,
        }
    _xlsx_cover(package, part, "persons", root)
    return persons


def _xlsx_threaded_comments(
    package: _Package, part: str, persons: dict[str, dict[str, object]]
) -> list[tuple[str, dict[str, object]]]:
    """Return one worksheet's threaded comments as (text, provenance), in order.

    Order is the order the part wrote them in -- a thread's root, then the
    replies that cite it -- and nothing is sorted by id, person or timestamp.
    The part is reached only through the worksheet's own relationships, by the
    one relationship type the extension defines and the content type it
    declares for its own part, so the classic list, an external target and a
    member that merely looks like the part all reach nothing.

    A threaded comment cites a person the workbook's own list has to define: a
    comment citing an id nobody declared names nobody at all, so the sheet is
    refused rather than reported against a person that is not there.

    A reply carries its thread's anchor rather than one of its own, so it
    inherits the anchor of the root its chain of parents ends at; a reply that
    writes a ``ref`` anyway is accepted only where it names that same cell or
    range, because a reply sitting somewhere other than its thread is a claim
    nothing here can place. ``parentId`` names a comment the part declares
    rather than one it has already written -- the schema orders nothing -- so
    the chains are walked only once the whole list is read, and a reply written
    before its root still resolves. What stays the part's order is the report.

    Nothing here conflates the two forms. A workbook carrying threaded comments
    normally carries a classic comment beside each thread as a placeholder for
    readers that do not know the extension; that is the classic part's own
    claim, read on its own route, and each form says which it is rather than
    either standing in for the other.
    """
    related = _related_part(
        package,
        part,
        _XLSX_THREADED_RELATIONSHIPS,
        _XLSX_THREADED_CONTENT_TYPE,
        "threaded comments",
        owner="worksheet",
        label="XLSX",
    )
    if related is None:
        return []
    relation_id, threaded_part = related
    root = _xlsx_threaded_root(package, threaded_part, "ThreadedComments")
    scope = f"{{{_XLSX_THREADED_MAIN}}}"
    result: list[tuple[str, dict[str, object]]] = []
    # Every comment the part declares, by the id it claims: what a ``parentId``
    # is resolved against, and the set a repeated id is found in.
    positions: dict[str, int] = {}
    extension = False
    for entry in root:
        if entry.tag == f"{scope}extLst":
            extension = _xlsx_extension_list(extension, "threaded comment list")
            continue
        if entry.tag != f"{scope}threadedComment":
            raise OOXMLInvalid(
                "XLSX threaded comment list holds an unsupported element"
            )
        if len(result) >= _MAX_XLSX_THREADED_RECORDS:
            raise OOXMLInvalid("XLSX worksheet has too many threaded comments")
        comment_id = _xlsx_threaded_guid(entry, "id", "threaded comment id")
        if comment_id is None:
            raise OOXMLInvalid("XLSX threaded comment declares no id")
        # Compared case-folded, as the person ids are and for the same reason.
        if comment_id.casefold() in positions:
            raise OOXMLInvalid("XLSX threaded comment id is repeated")
        person_id = _xlsx_threaded_guid(
            entry, "personId", "threaded comment person citation"
        )
        if person_id is None:
            raise OOXMLInvalid("XLSX threaded comment names no person")
        person = persons.get(person_id.casefold())
        if person is None:
            raise OOXMLInvalid("XLSX threaded comment names an undeclared person")
        parent_id = _xlsx_threaded_guid(entry, "parentId", "threaded comment parent id")
        reference = entry.attrib.get("ref") or None
        if reference is not None:
            # Validated as a coordinate or a range, then kept as written.
            _cell_range(reference)
        positions[comment_id.casefold()] = len(result)
        text, mentions, declared = _xlsx_threaded_text(entry, scope, persons)
        result.append(
            (
                text,
                {
                    "part": threaded_part,
                    "relationshipId": _clip(relation_id, _MAX_META_CHARS),
                    "commentForm": "threaded",
                    # Zero-based, counting comments: the extension point is not
                    # one, so it holds no position in the list.
                    "commentIndex": len(result),
                    # What this record itself claimed, which a reply need not
                    # claim at all.
                    "ref": reference,
                    "commentId": comment_id,
                    "parentCommentId": parent_id,
                    "personId": person_id,
                    **person,
                    # ``dT``, kept as the string the package wrote.
                    "timestamp": _xlsx_threaded_claim(entry, "dT"),
                    "done": _xlsx_threaded_done(entry),
                    # A declared and empty list and no list at all carry the
                    # same empty mentions, so whether one was written is its
                    # own claim.
                    "mentionsDeclared": mentions is not None,
                    "mentions": mentions or [],
                    # An absent ``text`` and an empty one carry the same empty
                    # text, so whether one was written is its own claim.
                    "textDeclared": declared,
                },
            )
        )
    # Then the anchors, now that every comment a ``parentId`` could name has
    # been read. Each record is left where the part wrote it.
    for index, (_, provenance) in enumerate(result):
        anchor = _xlsx_threaded_anchor(result, positions, index)
        claimed = provenance["ref"]
        # The bounds are what is compared, so ``A1`` and ``A1:A1`` are the one
        # anchor they name rather than a contradiction.
        if isinstance(claimed, str) and _cell_range(claimed) != _cell_range(anchor):
            raise OOXMLInvalid(
                "XLSX threaded comment reply contradicts its thread anchor"
            )
    _xlsx_cover(package, threaded_part, "threadedComments", root)
    return result


def _xlsx_threaded_anchor(
    comments: list[tuple[str, dict[str, object]]],
    positions: dict[str, int],
    index: int,
) -> str:
    """Return the anchor of the thread the comment at ``index`` sits in.

    A reply inherits from the root its chain of parents ends at rather than
    from its immediate parent, so an anchor carries down a chain of any depth.
    The walk is bounded by the comments on it: a comment that is its own
    parent, or any cycle of them, reaches no root, and so does a chain that
    runs past a comment the part never declared. Each anchor is settled onto
    every record the walk crossed, so no comment is walked more than once.
    """
    path = [index]
    crossed = {index}
    while True:
        record = comments[path[-1]][1]
        settled = record.get("cell")
        if isinstance(settled, str):
            anchor = settled
            break
        parent = record["parentCommentId"]
        if not isinstance(parent, str):
            # A thread root, which anchors itself and so has to say where.
            claimed = record["ref"]
            if not isinstance(claimed, str):
                raise OOXMLInvalid("XLSX threaded comment declares no reference")
            anchor = claimed
            break
        following = positions.get(parent.casefold())
        if following is None:
            raise OOXMLInvalid("XLSX threaded comment reply names no parent comment")
        if following in crossed:
            raise OOXMLInvalid("XLSX threaded comment reply reaches no thread root")
        path.append(following)
        crossed.add(following)
    for position in path:
        comments[position][1]["cell"] = anchor
    return anchor


def _xlsx_threaded_text(
    entry: ElementTree.Element, scope: str, persons: dict[str, dict[str, object]]
) -> tuple[str, list[dict[str, object]] | None, bool]:
    """Read a threaded comment's text and mentions, and whether text was written.

    The text is an ``xsd:string`` rather than the rich text a classic comment
    holds, so markup inside it is not a structure to descend into: a ``text``
    element carrying one is refused rather than read down to its first child. A
    second one leaves the comment with two texts and nothing here picks between
    them. The element itself is optional, so a comment carrying none at all
    makes the same empty claim an empty element does rather than being refused;
    the declaration is returned beside the text, because an empty element and no
    element at all are the same text but not the same claim.

    The one mentions list is accepted where the schema places it, directly
    below the comment and in the part's own namespace, and it is read only once
    the text is settled: a mention is a span of that text, so there is nothing
    to slice until it is known, and the schema orders the two elements either
    way. A comment declaring no list is returned a ``None``, which is not the
    claim a list declared empty makes.
    """
    text: str | None = None
    listed: ElementTree.Element | None = None
    extension = False
    for node in entry:
        if node.tag == f"{scope}text":
            if text is not None or len(node):
                raise OOXMLInvalid("XLSX threaded comment text is malformed")
            text = node.text or ""
        elif node.tag == f"{scope}mentions":
            if listed is not None:
                raise OOXMLInvalid("XLSX threaded comment declares two mention lists")
            listed = node
        elif node.tag == f"{scope}extLst":
            extension = _xlsx_extension_list(extension, "threaded comment")
        else:
            raise OOXMLInvalid("XLSX threaded comment holds an unsupported element")
    declared = text is not None
    text = text or ""
    if len(text) > _MAX_XLSX_THREADED_CHARS:
        raise OOXMLInvalid("XLSX threaded comment text exceeds the accepted bound")
    mentions = None if listed is None else _xlsx_mentions(listed, text, scope, persons)
    return text, mentions, declared


def _xlsx_mentions(
    listed: ElementTree.Element,
    text: str,
    scope: str,
    persons: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Read one comment's mentions in source order, as spans of its own text.

    A mention carries no text of its own: it names a position and a length in
    the comment's ``text``, so the slice reported is the file's own claim about
    what was mentioned. The offsets are character counts rather than byte ones,
    which is what Python's own indexing counts, so the slice is taken directly
    and reported exactly as written rather than clipped: a mention is evidence
    of what the file claimed, and a shortened slice is a different claim.
    Order is the order the file wrote them in -- nothing is sorted by offset --
    and overlapping spans are left as written, because a mention is a claim
    about the text rather than a partition of it.

    Reporting the slices whole lets a short comment emit far more text than it
    holds, because every span is sliced out of it again and overlapping ones
    are each their own output. The lengths are summed as they are accepted and
    held to the bound one comment's own text is held to, so a list of spans
    cannot amplify past what a comment may carry.

    Both identities a mention carries are ``ST_Guid``, kept exactly as written,
    and the person it cites has to be one the workbook's own list declares, as
    a comment's own author does; that record's claims about itself are merged
    in, still marked unauthenticated, because they are no more evidence of who
    was mentioned than they are of who commented. MS-XLSX spells the citation
    ``mentionpersonId`` -- a lower-case ``p`` where the rest of the grammar
    capitalises -- so it is read exactly as the schema declares it.

    Each offset is an ``xsd:unsignedInt``, so a value outside the type is
    refused rather than clamped into one inside it, and a span reaching past
    the end of the text slices nothing rather than slicing what is left. A
    repeated mention id claims one identity twice, refused as the grammar's
    other ids are. Nothing here checks that a span's text is the person's
    display name: what the text says is the file's claim, not a derivation.
    """
    mentions: list[dict[str, object]] = []
    seen: set[str] = set()
    sliced = 0
    for node in listed:
        # The schema gives the list nothing but qualified mentions, and gives a
        # mention no content at all, so a lookalike spelled elsewhere or left
        # unqualified declares nothing and neither does anything below one.
        if node.tag != f"{scope}mention":
            raise OOXMLInvalid("XLSX mention list holds an unsupported element")
        if len(node):
            raise OOXMLInvalid("XLSX mention holds an unsupported element")
        if len(mentions) >= _MAX_XLSX_THREADED_RECORDS:
            raise OOXMLInvalid("XLSX threaded comment has too many mentions")
        mention_id = _xlsx_threaded_guid(node, "mentionId", "mention id")
        if mention_id is None:
            raise OOXMLInvalid("XLSX mention declares no mention id")
        # Compared case-folded, as the grammar's other ids are: only a GUID's
        # hex letters vary in case, so two spellings claim one identity.
        if mention_id.casefold() in seen:
            raise OOXMLInvalid("XLSX mention id is repeated")
        seen.add(mention_id.casefold())
        person_id = _xlsx_threaded_guid(
            node, "mentionpersonId", "mention person citation"
        )
        if person_id is None:
            raise OOXMLInvalid("XLSX mention names no person")
        person = persons.get(person_id.casefold())
        if person is None:
            raise OOXMLInvalid("XLSX mention names an undeclared person")
        start = _xlsx_mention_offset(node, "startIndex", "start index")
        length = _xlsx_mention_offset(node, "length", "length")
        if start + length > len(text):
            raise OOXMLInvalid("XLSX mention span runs past the comment text")
        # Each span is inside the text, so the slice is exactly ``length``
        # characters and the running total is what the list will emit.
        sliced += length
        if sliced > _MAX_XLSX_THREADED_CHARS:
            raise OOXMLInvalid("XLSX mention spans exceed the accepted bound")
        mentions.append(
            {
                "mentionId": mention_id,
                "mentionPersonId": person_id,
                "startIndex": start,
                "length": length,
                "text": text[start : start + length],
                **person,
            }
        )
    return mentions


def _xlsx_mention_offset(element: ElementTree.Element, name: str, label: str) -> int:
    """Read one mention offset, refusing a value its type does not carry.

    Both offsets are ``xsd:unsignedInt`` and both are required, so an absent
    one is its own refusal rather than a zero.
    """
    raw = element.attrib.get(name)
    if raw is None:
        raise OOXMLInvalid(f"XLSX mention declares no {label}")
    if _INDEX.fullmatch(raw) is None or int(raw) > _MAX_INDEX:
        raise OOXMLInvalid(f"XLSX mention {label} is malformed")
    return int(raw)


def _xlsx_drawing_anchors(
    package: _Package, part: str, root: ElementTree.Element
) -> tuple[str, str, str, list[ElementTree.Element]] | None:
    """Resolve the one drawing a worksheet declares and return its anchors.

    A workbook reaches whatever a sheet displays the long way round: the
    worksheet relates one drawing part and the drawing anchors its objects.
    This is that route and nothing past it. Every step is walked and none is
    guessed -- the drawing is reached through the exact relationship id the
    ``drawing`` element names, held to the relationship and content types OOXML
    defines for a worksheet drawing -- so no part is read for merely looking
    like one, and no path is derived from the sheet's own name. A sheet displays
    one drawing, so two ``drawing`` elements leave which one it shows ambiguous
    and nothing here picks between them.

    The result is the drawing part, the relationship id the sheet reached it by,
    the drawing's own validated namespace and its anchors in source order, or
    ``None`` where the sheet declares no drawing at all. Each reader past this
    point resolves the route itself, so the part is parsed once per reader
    rather than cached between them: the cost is one parse of a bounded member,
    and the alternative is one function reading two unrelated things.
    """
    # As with the table-part route: the rest of the sheet reader matches local
    # names, because a sheet's vocabulary is settled by the part it was reached
    # through. This route opens another part, so the declaration is held to the
    # sheet's own namespace and that namespace has to be SpreadsheetML -- an
    # unqualified or foreign lookalike declares nothing and stays transparent,
    # exactly as a nested one does.
    namespace, _, _ = root.tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if namespace not in _SPREADSHEETML:
        return None
    named = [node for node in root if node.tag == f"{{{namespace}}}drawing"]
    if not named:
        return None
    if len(named) > 1:
        raise OOXMLInvalid("XLSX worksheet names more than one drawing")
    relation_id = _relationship_id(named[0].attrib)
    if not relation_id:
        raise OOXMLInvalid("XLSX drawing reference names no relationship")
    drawing_part = _referenced_part(
        package,
        part,
        relation_id,
        _XLSX_DRAWING_RELATIONSHIPS,
        _XLSX_DRAWING_CONTENT_TYPE,
        "drawing",
        "XLSX",
    )
    drawing = package.xml(drawing_part)
    drawing_namespace, _, local = drawing.tag.rpartition("}")
    drawing_namespace = drawing_namespace.lstrip("{")
    if drawing_namespace not in _XLSX_DRAWING_NAMESPACES:
        raise OOXMLInvalid("XLSX drawing part uses an unsupported namespace")
    if local != "wsDr":
        raise OOXMLInvalid("XLSX drawing part is not rooted at its own drawing")
    # The drawing's own vocabulary is settled by the namespace just validated,
    # so an anchor counts only where its qualified name is that namespace's:
    # an unqualified or foreign lookalike declares nothing and is transparent.
    anchor_names = {f"{{{drawing_namespace}}}{name}" for name in _XLSX_ANCHORS}
    anchors = [node for node in drawing if node.tag in anchor_names]
    if len(anchors) > _MAX_XLSX_ANCHORS:
        raise OOXMLInvalid("XLSX drawing has too many anchors")
    # The drawing's own unsupported extension lists, reported now that the part
    # has passed every check it is held to. Both readers past here resolve this
    # same route, so the part is reached twice and the coverage is built on the
    # first reach and left alone on the second: one physical part is one set of
    # entries however many readers open it.
    _xlsx_cover(package, drawing_part, "drawing", drawing)
    return drawing_part, relation_id, drawing_namespace, anchors


def _xlsx_charts(
    package: _Package, part: str, root: ElementTree.Element
) -> list[tuple[str, dict[str, object]]]:
    """Return the stored chart records a worksheet's drawing anchors, in order.

    The route in is the shared worksheet-drawing one, and the last step is this
    reader's own: the drawing anchors a graphic frame, and that frame's
    ``c:chart`` relates the chart part through the frame's own id from the
    drawing part's relationships, so no chart is read for merely looking like
    one.

    Anchors are walked in source order and each record names the anchor showing
    it, its kind, and where that kind places the frame: the cells it covers for
    the two cell-anchored kinds, and the stored geometry the sized kinds are
    placed by. What is read at the end of the route is
    the shared chart reader already serving PPTX frames and DOCX drawings, so
    the record vocabulary is theirs unchanged, and nothing behind the values is
    opened: neither the chart part's own relationships nor the workbook a chart
    may embed.
    """
    resolved = _xlsx_drawing_anchors(package, part, root)
    if resolved is None:
        return []
    drawing_part, relation_id, drawing_namespace, anchors = resolved
    result: list[tuple[str, dict[str, object]]] = []
    for index, anchor in enumerate(anchors):
        frames = [
            node
            for node in anchor
            if node.tag == f"{{{drawing_namespace}}}graphicFrame"
        ]
        if len(frames) > 1:
            raise OOXMLInvalid("XLSX anchor holds more than one graphic frame")
        if not frames:
            continue
        chart_id = _drawing_chart(frames[0], "XLSX", "graphic frame")
        if chart_id is None:
            continue
        chart_part = _referenced_part(
            package,
            drawing_part,
            chart_id,
            _CHART_RELATIONSHIPS,
            _CHART_CONTENT_TYPE,
            "chart",
            "XLSX",
        )
        where: dict[str, object] = {
            "drawingPart": drawing_part,
            "drawingRelationshipId": relation_id,
            "anchorIndex": index,
            **_xlsx_anchor_placement(anchor),
            "chartPart": chart_part,
            "relationshipId": chart_id,
        }
        result.extend(
            (value, {**where, **place})
            for place, value in _chart_records(package, chart_part, "XLSX")
        )
    return result


def _xlsx_drawing_objects(
    package: _Package, part: str, root: ElementTree.Element
) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Inventory the non-chart objects a worksheet's drawing anchors, in order.

    The route in is the shared worksheet-drawing one, so nothing is reached that
    the chart route would not reach either. An object counts only where it is a
    direct child of an anchor and its qualified name is the drawing's own
    validated namespace: a lookalike in a foreign vocabulary, one stripped of
    its prefix, and one nested further down -- inside a group's own shape list,
    in an alternate-content fallback, below a graphic -- are all transparent
    here, exactly as the anchors above them are.

    What comes out is metadata and encoded references, never content. Each
    record names the object's kind and the claims its author wrote about it, and
    for the two kinds that encode a reference it names that reference as
    written: a picture's embedded image part or external link target, and a
    connector's two endpoint claims. Nothing is opened, decoded, rendered,
    OCRed, fetched or resolved past the package's own relationships, no shape
    text is read, and no connection graph is inferred from the endpoints -- a
    claim about a shape id is a claim, not an edge. The caller may hand a
    picture's named embedded part to the bounded embedded-image pass afterwards;
    nothing on this route does, and nothing on it ever opens a member.

    A graphic frame showing a chart is passed over: the chart route already
    reports that frame, on this same anchor, as the chart records it holds, so
    inventorying it here would be the same object said twice. The object's index
    still counts it, so an index is the object's position among the ones the
    drawing wrote rather than among the ones reported.
    """
    resolved = _xlsx_drawing_anchors(package, part, root)
    if resolved is None:
        return []
    drawing_part, relation_id, namespace, anchors = resolved
    kinds = {f"{{{namespace}}}{kind}": kind for kind in _XLSX_DRAWING_OBJECTS}
    result: list[tuple[dict[str, object], dict[str, object]]] = []
    count = 0
    for index, anchor in enumerate(anchors):
        object_index = 0
        for node in anchor:
            kind = kinds.get(node.tag)
            if kind is None:
                continue
            current_object_index = object_index
            object_index += 1
            count += 1
            if count > _MAX_XLSX_DRAWING_OBJECTS:
                raise OOXMLInvalid("XLSX drawing has too many objects")
            if kind == "graphicFrame" and (
                _drawing_chart(node, "XLSX", "graphic frame") is not None
            ):
                continue
            record = _xlsx_object_claims(node, namespace, kind)
            if kind == "pic":
                record.update(
                    _xlsx_picture_image(package, drawing_part, node, namespace)
                )
            elif kind == "cxnSp":
                record.update(_xlsx_connector_ends(node, namespace))
            result.append(
                (
                    {
                        "drawingPart": drawing_part,
                        "drawingRelationshipId": relation_id,
                        "anchorIndex": index,
                        **_xlsx_anchor_placement(anchor),
                        "drawingObjectIndex": current_object_index,
                        "drawingObjectType": kind,
                    },
                    record,
                )
            )
    return result


def _xlsx_object_claims(
    obj: ElementTree.Element, namespace: str, kind: str
) -> dict[str, object]:
    """Return the ``cNvPr`` claims one drawing object's author made about it.

    Each object kind names its non-visual properties holder after itself --
    ``nvSpPr``, ``nvPicPr``, ``nvCxnSpPr`` and so on -- so the holder is matched
    on the prefix they share, qualified in the drawing's own namespace, and only
    one directly below the object is read. ``contentPart`` has no such holder at
    all, so it claims nothing and every field comes back absent.

    Two sets of properties, in one holder or across two, leaves which claims are
    the object's own ambiguous, so it is refused rather than read from whichever
    was written first: an object's identity and its author's words come from the
    same element, and neither can be taken from a different one than the other.

    Every field is present whatever the object wrote, and each claim is kept
    apart from the others: a claim nothing wrote is ``None``, and a claim
    written empty is the empty string, because a file that declared a
    description and left it blank said something a file that declared none did
    not. Values are the author's own, read as written and only bounded.
    """
    holder_name = _XLSX_NON_VISUAL.get(kind)
    properties = (
        []
        if holder_name is None
        else [
            node
            for holder in obj
            if holder.tag == f"{{{namespace}}}{holder_name}"
            for node in holder
            if node.tag == f"{{{namespace}}}cNvPr"
        ]
    )
    if len(properties) > 1:
        raise OOXMLInvalid("XLSX drawing object declares its claims twice")
    claims = properties[0].attrib if properties else {}
    return {
        field: None
        if attribute not in claims
        else _clip(claims[attribute], _MAX_META_CHARS)
        for attribute, field in _XLSX_OBJECT_CLAIMS
    }


def _xlsx_picture_image(
    package: _Package, part: str, pic: ElementTree.Element, namespace: str
) -> dict[str, object]:
    """Inventory the image one worksheet picture names, without reaching it.

    The route in is the picture's own direct ``xdr:blipFill`` child, qualified
    in the drawing's validated namespace, and that fill's own direct ``a:blip``
    child, qualified in a DrawingML main namespace. That is the one place a
    picture declares the image it displays, so a blip reached anywhere else
    below it -- in its shape properties, in a fill on some other element, in
    extension metadata -- is not the displayed image and is passed over. A
    picture shows one image, so two direct fills leave which one it shows
    ambiguous, and nothing here chooses between them.

    ``part`` is the drawing part, because that is the part holding the
    reference, so the ids resolve through its relationships or not at all.
    Everything past the blip is the shared inert image reader PPTX pictures and
    DOCX drawings already use, unchanged.
    """
    fills = [node for node in pic if node.tag == f"{{{namespace}}}blipFill"]
    if len(fills) > 1:
        raise OOXMLInvalid("XLSX picture declares more than one image reference")
    return _blip_image(
        package,
        part,
        [node for node in fills[0] if node.tag in _DRAWING_BLIP] if fills else [],
        "XLSX picture",
    )


def _xlsx_connector_ends(cxn: ElementTree.Element, namespace: str) -> dict[str, object]:
    """Return the two endpoint claims one connector wrote, as written.

    The route in is the schema's own: the connector's direct ``xdr:nvCxnSpPr``
    and that holder's direct ``xdr:cNvCxnSpPr``, both qualified in the drawing's
    validated namespace, then a direct ``a:stCxn`` or ``a:endCxn`` below it,
    qualified in a DrawingML main namespace. An endpoint written anywhere else,
    or in a vocabulary that merely shares the local name, declares nothing about
    this connector.

    A connector has one set of connector properties and one of each endpoint, so
    two of either leaves what it is attached to ambiguous and nothing here
    chooses. Each endpoint names both an element id and a connection site index
    and the schema requires both, so one missing or not a number leaves the
    endpoint unreadable rather than half-read. An endpoint the connector never
    wrote stays ``None``: a connector may be attached at one end, or at neither.

    The claims are the file's own and stay claims. The id is not resolved to the
    shape it names, the index is not resolved to a site on it, and no graph or
    hierarchy is built out of them -- which would be a reading of the drawing
    rather than an inventory of it.
    """
    properties = [
        node
        for holder in cxn
        if holder.tag == f"{{{namespace}}}nvCxnSpPr"
        for node in holder
        if node.tag == f"{{{namespace}}}cNvCxnSpPr"
    ]
    if len(properties) > 1:
        raise OOXMLInvalid("XLSX connector declares its connector properties twice")
    holder = properties[0] if properties else None
    record = dict(_XLSX_CONNECTOR)
    for names, id_field, index_field in _XLSX_CONNECTION_ENDS:
        ends = [] if holder is None else [n for n in holder if n.tag in names]
        if not ends:
            continue
        if len(ends) > 1:
            raise OOXMLInvalid("XLSX connector declares one of its endpoints twice")
        record[id_field] = _xlsx_connection_claim(ends[0], "id")
        record[index_field] = _xlsx_connection_claim(ends[0], "idx")
    return record


def _xlsx_connection_claim(end: ElementTree.Element, name: str) -> str:
    """Read one required unsigned endpoint attribute, or refuse the endpoint."""
    value = end.attrib.get(name)
    if (
        value is None
        or _SHAPE_ID.fullmatch(value) is None
        or int(value) > _MAX_INDEX
    ):
        raise OOXMLInvalid("XLSX connector endpoint is malformed")
    return value


def _xlsx_anchor_placement(anchor: ElementTree.Element) -> dict[str, object]:
    """Return the placement keys one anchor's own kind reports it by.

    The three kinds place a frame in three different ways, so each reports what
    it actually names and nothing it does not. A two-cell anchor names two
    corners, so it reports the cells between them and no geometry at all. A
    one-cell anchor names one corner and a size, so it reports that single cell
    and the stored extent. An absolute anchor is placed on the sheet rather than
    in its grid, so it covers no cells and reports a position beside its extent.

    Geometry is kept verbatim in EMU and never divided into cells: an extent is
    a size, and resolving one against column widths and row heights the sheet
    may not even declare would report a range the drawing never wrote.

    The anchor's own qualified name was already held to the drawing's validated
    namespace, so that namespace settles the vocabulary everything beneath it is
    read in, here as in the corner reader.
    """
    namespace, _, local = anchor.tag.rpartition("}")
    kind = _XLSX_ANCHORS[local]
    if kind == "twoCell":
        return {"anchorType": kind, "anchorRange": _xlsx_anchor_range(anchor)}
    extent = _xlsx_anchor_emu(
        anchor, f"{namespace}}}ext", ("cx", "cy"), _EMU_EXTENT, "extent"
    )
    if kind == "oneCell":
        column, row = _xlsx_anchor_corner(anchor, f"{namespace}}}from")
        return {
            "anchorType": kind,
            "anchorRange": f"{_column_name(column)}{row}",
            "anchorGeometry": extent,
        }
    position = _xlsx_anchor_emu(
        anchor, f"{namespace}}}pos", ("x", "y"), _EMU_OFFSET, "position"
    )
    return {
        "anchorType": kind,
        "anchorRange": None,
        "anchorGeometry": {**position, **extent},
    }


def _xlsx_anchor_emu(
    anchor: ElementTree.Element,
    tag: str,
    names: tuple[str, str],
    pattern: re.Pattern[str],
    label: str,
) -> dict[str, int]:
    """Read the EMU pair one anchor element carries, or refuse it.

    ``tag`` is the element's qualified name in the drawing's own namespace, so
    an unqualified or foreign lookalike names nothing the grammar declares and
    is as absent here as it is elsewhere on this route. A sized anchor carries
    exactly one of these, so one written twice -- or not at all -- leaves where
    the frame sits unanswerable. ``pattern`` is the attributes' own type: a
    position is signed, so a frame placed off the sheet keeps its negative
    coordinate, while an extent has no signed form and both values are
    required, so an absent one is its own refusal rather than a zero.
    """
    nodes = [node for node in anchor if node.tag == tag]
    if len(nodes) != 1:
        raise OOXMLInvalid(f"XLSX anchor does not name exactly one {label}")
    values: dict[str, int] = {}
    for name in names:
        raw = nodes[0].attrib.get(name)
        if raw is None or pattern.fullmatch(raw) is None:
            raise OOXMLInvalid(f"XLSX anchor {label} is malformed")
        values[name] = int(raw)
    return values


def _xlsx_anchor_range(anchor: ElementTree.Element) -> str:
    """Return the inclusive A1 range a two-cell anchor's two corners cover.

    A corner's ``xdr:col`` and ``xdr:row`` are zero-based, so each is reported
    as the A1 position the sheet calls it. The offsets beside them place the
    frame inside those cells and name no further cell, so they are not read, and
    the range covers both corners' own cells rather than the extent the frame is
    drawn to. A corner pair whose end precedes its start describes no rectangle,
    so it is refused rather than reordered into the one it was probably meant to
    be, as a cell range already is.

    The anchor's own qualified name was already held to the drawing's validated
    namespace, so that namespace settles the vocabulary its corners and their
    coordinates are read in: an unqualified or foreign lookalike declares
    nothing the grammar names and is transparent here too.
    """
    namespace, _, _ = anchor.tag.rpartition("}")
    first = _xlsx_anchor_corner(anchor, f"{namespace}}}from")
    last = _xlsx_anchor_corner(anchor, f"{namespace}}}to")
    if last[0] < first[0] or last[1] < first[1]:
        raise OOXMLInvalid("XLSX anchor range is malformed")
    return f"{_column_name(first[0])}{first[1]}:{_column_name(last[0])}{last[1]}"


def _xlsx_anchor_corner(anchor: ElementTree.Element, tag: str) -> tuple[int, int]:
    """Return the one-based (column, row) one anchor corner names, or refuse it.

    ``tag`` is the corner's qualified name in the drawing's own namespace. An
    anchor positions a frame by exactly two corners, so a corner written twice
    -- or not at all -- leaves where the frame sits unanswerable.
    """
    namespace, _, _ = tag.rpartition("}")
    corners = [node for node in anchor if node.tag == tag]
    if len(corners) != 1:
        raise OOXMLInvalid("XLSX anchor does not name exactly one of its corners")
    return (
        _xlsx_anchor_coordinate(corners[0], f"{namespace}}}col", _MAX_COLUMN),
        _xlsx_anchor_coordinate(corners[0], f"{namespace}}}row", _MAX_ROW),
    )


def _xlsx_anchor_coordinate(corner: ElementTree.Element, tag: str, bound: int) -> int:
    """Read one zero-based coordinate as the one-based position it names.

    ``tag`` is the coordinate's qualified name in the drawing's own namespace.
    The value is held to the grid SpreadsheetML itself accepts, so a coordinate
    beyond the sheet names no cell and is refused rather than reported as an
    anchor over one.
    """
    nodes = [node for node in corner if node.tag == tag]
    if len(nodes) != 1:
        raise OOXMLInvalid("XLSX anchor corner is malformed")
    raw = (nodes[0].text or "").strip()
    if _DIGITS.fullmatch(raw) is None or int(raw) >= bound:
        raise OOXMLInvalid("XLSX anchor coordinate is outside the sheet")
    return int(raw) + 1


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


def _column_name(column: int) -> str:
    """Return the A1 letters one one-based column index is written as."""
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


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
    label: str = "PPTX",
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

    ``label`` is the format whose relationship is being resolved and ``owner``
    the kind of part holding it, so a refusal names the file it came from
    rather than whichever format reached this code first.
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
        raise OOXMLInvalid(f"{label} {owner} names more than one {what} relationship")
    target, external = package.relationships[rels][named[0]]
    if external:
        raise OOXMLInvalid(f"{label} {what} relationship is external")
    if package.declared_type(target) != content_type:
        raise OOXMLInvalid(f"{label} {what} relationship does not name a {what} part")
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

    An accepted chart reports the unsupported extension lists it holds, which
    is where a workbook's chart coverage comes from: this is the one place a
    chart part is opened, whichever format related it, and only a format
    collecting coverage is listening.
    """
    root = package.xml(part)
    namespace, _, local = root.tag.rpartition("}")
    if namespace.lstrip("{") not in _CHART_NAMESPACES:
        raise OOXMLInvalid(f"{owner} chart part uses an unsupported namespace")
    if local != "chartSpace":
        raise OOXMLInvalid(f"{owner} chart part is not rooted at its own chart space")
    _xlsx_cover(package, part, "chart", root)
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

    An extension list is payload this reader does not understand -- it is
    reported as coverage where the format collects it -- so nothing below one is
    read as a series, a cache or a point. A filtered series a producer parked in
    ``c:extLst`` is a series the chart does not plot, and reading one as plotted
    would report values the chart never showed.
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
    for series in (
        node for node in _outside_extensions(root) if _local(node.tag) == "ser"
    ):
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
    is taken from a layout or recomputed from the data -- nor from an extension
    list below the body, which is payload rather than the title's own runs.
    """
    holder = next((node for node in title if _local(node.tag) == "tx"), None)
    if holder is None:
        return []
    rich = next((node for node in holder if _local(node.tag) == "rich"), None)
    if rich is not None:
        runs = [
            node.text or ""
            for node in _outside_extensions(rich)
            if _local(node.tag) == "t"
        ]
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
    recorded as the part or the link it is; the raster it embeds is examined
    only where the trusted settings say so, and what was recognised in it is
    published after the claims as a reading of its own. Geometry and
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
        images: _EmbeddedImages,
    ) -> None:
        self._capture_id = capture_id
        self._part = part
        self._slide_id = slide_id
        self._ordinal = ordinal
        self._hidden = hidden
        self._segments = segments
        self._package = package
        self._images = images
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
        inventory about an image rather than anything read out of one, and the
        record states exactly what was read: whether the bytes were decoded,
        whether the engine ran over them, and the one outcome that says why
        nothing was read where nothing was. A linked reference carries no part,
        so nothing of it is ever opened, resolved or fetched.

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
        anything read off an image: no text is invented for a picture whose
        author described it with none.

        Any text recognised in the raster the picture embeds follows the
        author's claims, as the separate reading it is: it is anchored to this
        picture's own place on the slide and to the image part the bytes came
        from, with the raster's own frame and line geometry, and it is never
        joined to the slide's prose or to the alt text the author wrote.

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
        locator = _locator(self._capture_id, **where)
        record: dict[str, object] = {
            "kind": "picture",
            **{
                name: None if value is None else _clip(value, _MAX_META_CHARS)
                for name, value in claims.items()
            },
            **_pptx_picture_image(self._package, self._part, pic),
        }
        # The candidate carries this picture's own locator, so two pictures
        # showing one image part stay two readings at two places on the slide.
        # ``imagePart`` and ``imageContentType`` come from the record just
        # built, so a picture naming only a reference out of the package carries
        # no part here and nothing of that reference is opened, fetched or
        # probed.
        read = self._images.member(
            self._package, record["imagePart"], record["imageContentType"], locator
        )
        self._segments.append(
            ExtractedSegment(
                "object_inventory",
                locator,
                structured_value={
                    **record,
                    "imageDecoded": read.decoded,
                    "ocrPerformed": read.performed,
                    "ocrOutcome": read.outcome,
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
        for reading in read.segments:
            # A reading keeps the role, method, language and confidence the
            # image path gave it, so an accepted line stays searchable text read
            # by OCR and a below-threshold observation stays out of ordinary
            # retrieval. A hidden slide hides what was read on it exactly as it
            # hides the record above.
            self._segments.append(
                replace(reading, visibility=visibility) if self._hidden else reading
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

    What the blip then declares is read by the shared blip reader.
    """
    fills = [node for node in pic if _local(node.tag) == "blipFill"]
    if len(fills) > 1:
        raise OOXMLInvalid("PPTX picture declares more than one image reference")
    return _blip_image(
        package,
        part,
        [node for node in fills[0] if node.tag in _DRAWING_BLIP] if fills else [],
        "PPTX picture",
    )


def _blip_image(
    package: _Package,
    part: str,
    blips: list[ElementTree.Element],
    what: str,
) -> dict[str, object]:
    """Describe the image the one blip a drawing object named declares, inertly.

    ``blips`` is every qualified blip on the object's own fill route, so an
    object showing one image named one and an object showing none named none.
    Two blips in one fill leave which image the object shows ambiguous -- a blip
    written beside the object's own is a second declaration, not a qualifier on
    the first -- and nothing here chooses between them.

    A blip declares its image on two attributes, each read in the relationships
    namespaces OOXML defines for it: ``r:embed`` names a part inside the
    package, ``r:link`` names a reference out of it. Both may be written, and
    each is recorded in its own fields so an embedded part is never read as a
    link or the reverse. One attribute written in both namespaces at once names
    two relationships, which is refused for the reason two blips are.

    The ids resolve only through ``part``'s own relationships -- the part holding
    the reference -- so containment, path safety and existence were all settled
    when the package validated them. Nothing here opens, decodes, renders, OCRs
    or fetches anything: an embedded part contributes its name, its declared
    type and the byte length the package already measured, and a linked target
    is carried exactly as written.
    """
    if not blips:
        return dict(_DRAWING_IMAGE)
    if len(blips) > 1:
        raise OOXMLInvalid(f"{what} declares more than one image reference")
    rels = posixpath.join(
        posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
    )
    relations = package.relationships.get(rels, {})
    types = package.relationship_types.get(rels, {})
    embedded = _blip_id(blips[0], "embed", what)
    linked = _blip_id(blips[0], "link", what)
    record = dict(_DRAWING_IMAGE)
    if embedded is not None:
        record.update(_embedded_image(package, relations, types, embedded, what))
    if linked is not None:
        record.update(_linked_image(relations, types, linked, what))
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
    """Read the shared strings by their fixed name, as the reader always has.

    Reading is deliberately unchanged. Coverage is not: the part reports its
    extension list only when the workbook itself declares it -- a qualifying
    shared-strings relationship that resolves to this very member, declared with
    the shared-strings content type -- and the root is an ``sst``, which the
    vocabulary then requires to be a SpreadsheetML one. A member that merely
    holds the name, a relationship to some other part, or a lookalike root is
    read for its strings as before and declares no extension.
    """
    part = "xl/sharedStrings.xml"
    if part not in package.names:
        return []
    root = package.xml(part)
    rels = "xl/_rels/workbook.xml.rels"
    targets = package.relationships.get(rels, {})
    if (
        root.tag.rpartition("}")[2] == "sst"
        and package.declared_type(part) == _XLSX_SHARED_STRINGS_CONTENT_TYPE
        and any(
            _is_defined_relationship(kind, _XLSX_SHARED_STRINGS_RELATIONSHIPS)
            and targets.get(relation_id) == (part, False)
            for relation_id, kind in package.relationship_types.get(rels, {}).items()
        )
    ):
        _xlsx_cover(package, part, "sharedStrings", root)
    return [
        "".join(
            node.text or ""
            for node in _outside_extensions(item)
            if _local(node.tag) == "t"
        )
        for item in root
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
    for node in _outside_extensions(root):
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


def _xlsx_numeric(kind: str | None, raw: str | None) -> dict[str, object]:
    """Decode one stored number, or say why its text is not a finite one.

    Only a cell that stored a number is read: SpreadsheetML defaults an absent
    ``t`` to ``n``, so an absent type and ``n`` are the one numeric kind, and
    every other kind -- a shared or inline string, a boolean, an error -- keeps
    the value it already reported and is not looked at here. An absent or empty
    ``v`` stored no number to decode, so a blank stays the blank it is rather
    than becoming a malformed number.

    What comes back is merged into the record the caller already built, so the
    decoded number replaces the lexical ``decoded`` an ordinary cell reports
    and is added beside the ``raw`` a saved cache keeps. Either way the exact
    text the file wrote stays in ``raw``: the decode is a reading of it, never
    a replacement for it, and a reader needing the authored digits -- their
    count, their trailing zeros, their exponent -- still has them.

    Text the type cannot hold is reported as the anomaly it is and nothing is
    decoded from it, so a malformed, non-finite or unrepresentable value can
    never be mistaken for a number the file stored. Nothing here applies a
    number format, converts a serial to a date or evaluates anything: an
    integer form inside the exactly representable range decodes to an integer,
    everything else to the double the file stored it as.
    """
    if raw is None or kind not in (None, "n"):
        return {}
    text = raw.strip()
    if not text:
        return {}
    if _XLSX_NUMBER.fullmatch(text) is None:
        return {
            "numericAnomaly": "nonfinite" if text in _XLSX_NONFINITE else "malformed"
        }
    if (
        "." not in text
        and "e" not in text
        and "E" not in text
        and len(text.lstrip("+-")) <= _MAX_EXACT_INTEGER_DIGITS
    ):
        number = int(text)
        if abs(number) <= _MAX_EXACT_INTEGER:
            return {"decoded": number}
    # Overflow is the one conversion that can still fail here, and it fails to
    # an infinity rather than an error, so the result is checked rather than
    # trusted: a lexically valid magnitude the type cannot hold is the
    # out-of-range number it is, not a malformed token. Underflow lands on
    # zero, which is the double the file stored.
    converted = float(text)
    return (
        {"decoded": converted}
        if math.isfinite(converted)
        else {"numericAnomaly": "nonfinite"}
    )


def _xlsx_temporal_serial(
    raw: str, decoded: float, kind: str, system: str
) -> dict[str, object]:
    """Read one stored serial as the point in time its number format writes.

    ``raw`` is the exact text the file wrote, ``decoded`` the JSON-safe number
    already read from it, ``kind`` the temporal reading the cell's format
    resolved to, and ``system`` the epoch its workbook counts from. Nothing
    here validates the text: this is reached only once it is known to be a
    finite number, so a malformed or non-finite value is the caller's refusal
    to make and never arrives.

    What comes back is merged into the record the caller already built. On a
    serial the calendar can hold, the number moves to ``serial`` and
    ``decoded`` becomes the local ISO literal it names, so the authored digits
    stay in ``raw`` and the number stays reportable beside the reading of it.
    A serial no calendar has decodes to nothing at all: only a
    ``temporalDecodeAnomaly`` comes back, which leaves the caller's numeric
    ``decoded`` exactly as it was rather than replacing it with a date that
    does not exist. A negative serial is one of those -- the systems count
    forwards from their epoch and nothing is written before it -- and so is a
    magnitude past the last day the calendar reaches.

    The literal is local and bears no timezone: a serial carries none, so
    claiming an offset -- or a ``Z`` -- would invent one the file never wrote.
    The arithmetic is exact, from the authored text rather than from the
    double, and the time of day is rounded to the nearest microsecond so the
    fraction a writer actually stores for a whole minute reports as that
    minute. A fraction rounding up to a whole day carries into the next day
    rather than naming a 24th hour no clock has.
    """
    # The magnitude is checked against the double first, before any exact
    # arithmetic: a serial naming a year past the calendar -- or an exponent
    # naming no year at all -- costs two comparisons here rather than parsing
    # and multiplying the digits behind it.
    limit = _XLSX_MAX_SERIAL[system]
    if decoded < 0:
        return {"temporalDecodeAnomaly": "negative"}
    if decoded > limit + 1:
        return {"temporalDecodeAnomaly": "out_of_range"}
    serial = Decimal(raw)
    whole = int(serial)
    microseconds = int(
        ((serial - whole) * _MICROSECONDS_PER_DAY).to_integral_value(ROUND_HALF_EVEN)
    )
    carry = microseconds == _MICROSECONDS_PER_DAY
    if carry:
        microseconds = 0
    seconds, microsecond = divmod(microseconds, 1_000_000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    clock = datetime.time(hour, minute, second, microsecond).isoformat()
    # A clock format names a time of day and no day, so the whole days the
    # serial carries are dropped rather than dated: the fraction is read modulo
    # one day, which is the reading that makes a stored 1.5 the half past
    # midnight it displays as.
    if kind == "time":
        return {"serial": decoded, "decoded": clock, "decodedKind": kind}
    # A date format names the day and nothing else, so it ignores the fraction
    # entirely -- including a fraction that rounded up to a whole day, which
    # would otherwise move the date onto the day after the one it stores.
    day = whole + (1 if carry and kind == "datetime" else 0)
    if day > limit:
        return {"temporalDecodeAnomaly": "out_of_range"}
    anomaly: dict[str, object] = {}
    if system == "1900" and day == _XLSX_PHANTOM_SERIAL:
        date = _XLSX_PHANTOM_DATE
        anomaly = {"temporalDecodeAnomaly": "excel_1900_leap_day"}
    else:
        # Below the phantom day the two readings are one day apart, so the
        # serial is shifted back onto the real calendar; at and above it the
        # epoch already absorbs the day Excel counted and nothing is shifted.
        shift = 1 if system == "1900" and day < _XLSX_PHANTOM_SERIAL else 0
        date = (_XLSX_EPOCHS[system] + datetime.timedelta(day + shift)).isoformat()
    return {
        "serial": decoded,
        "decoded": date if kind == "date" else f"{date}T{clock}",
        "decodedKind": kind,
        **anomaly,
    }


def _xlsx_temporal(
    provenance: dict[str, object], numeric: dict[str, object], raw: str | None
) -> dict[str, object]:
    """Read one cell's stored number as a time, where its format says to.

    This is the gate between the two readings the cell already has: the number
    decoded from its text and the format provenance resolved for it. All four
    conditions must hold before a serial is decoded at all -- a finite number
    was decoded, the file wrote the text it came from, the resolved format
    classifies as a date, a time or a datetime, and the workbook's epoch is one
    this reader counts from. Anything else is no temporal reading rather than a
    guessed one, which is what keeps a string, a boolean, an error, a malformed
    or non-finite value, an elapsed duration and every ambiguous, general or
    numeric format exactly as they already were.

    What comes back is merged after the numeric metadata, so a successful
    decode moves the number to ``serial`` and replaces ``decoded`` with the
    literal it names, while an anomaly-only result adds the anomaly and leaves
    the numeric ``decoded`` in place.
    """
    decoded = numeric.get("decoded")
    format_id = provenance["numberFormatId"]
    code = provenance["numberFormatCode"]
    system = provenance["dateSystem"]
    if (
        raw is None
        or not isinstance(decoded, int | float)
        or not isinstance(format_id, int)
        or not (code is None or isinstance(code, str))
        or not isinstance(system, str)
        or system not in _XLSX_EPOCHS
    ):
        return {}
    kind = _xlsx_temporal_kind(format_id, code)
    if kind is None or kind not in _XLSX_TEMPORAL_KINDS:
        return {}
    return _xlsx_temporal_serial(raw, decoded, kind, system)


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
    # Inventory segments wait for the run that holds their text, each beside the
    # identity of the paragraph it was read from so waiting does not lose it.
    marks: list[tuple[str, dict[str, object], str, str, str | None]] = field(
        default_factory=list
    )
    # Where this cell sits in its table's grid, carried by every run it emits.
    grid: dict[str, object] | None = None
    # The revision state the prose now waiting in ``parts`` stands in. A run
    # joins prose of one state only, so text whose state changes closes the run
    # it cannot belong to rather than being joined across the boundary.
    state: str = "current"


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
    ``object_inventory`` segment, the ``image_alt`` claims after it and, where
    the settings examine embedded rasters, whatever was recognised in the image
    after those),
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

    Every ``table_cell`` run also carries the grid its own table declared: the
    table's path in the story, the declared ``tblGrid`` column count, the cell's
    source row and cell ordinals, its logical grid coordinates, the merge's
    ``rowSpan`` and ``columnSpan``, whether the cell anchors that merge or is
    covered by one, and the path of the cell the merge is anchored at. The grid
    is planned before any cell is read, so a merged cell's text is emitted once
    at its anchor and no covered coordinate repeats it; a table is never
    flattened and no row is read as a header.

    A coordinate the grid declares is kept whether or not prose stands at it. A
    cell that emits no ``table_cell`` run -- it holds no prose, its prose is
    deleted or hidden, its content was never prose, or it is the covered
    continuation of a vertical merge -- closes with one ``object_inventory``
    segment at the cell's own position holding a ``docxTableCell`` record:
    ``empty`` and the cell's whole geometry, in the value and in the locator
    both. ``empty`` reports what the source cell holds, never the run count: a
    cell whose only content is deleted, hidden, an equation, a drawing, an
    embedded object, a field, an annotation, a text box or a nested table is
    recorded as the authored cell it is. A table holding no cell at all has no
    coordinate to report and takes one ``docxTable`` record at
    ``<table>.inventory`` instead, naming its declared grid, its row count and
    the cells it does not have; a table with cells takes no record of its own.

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

    Everything read out of one paragraph also carries that paragraph's own
    identity in ``paragraphId`` when it declared one: its current, deleted and
    hidden text, the hyperlinks, fields and annotation references standing in
    it, and the equations, images, charts, SmartArt, embedded objects and legacy
    images it anchors, down to the claims and values those records derive. The
    identity is the exact ``w14:paraId`` the paragraph wrote, held to the range
    MS-DOCX reserves and kept as authored, never recased; a written value
    outside that range is refused rather than reported. An attribute written in
    no namespace, in another namespace, or under a similar local name is not
    that attribute and is never read as one, and a paragraph that declares none
    carries no key -- the absent key is how "this file said nothing" is told
    apart from any identity, in every story.

    A paragraph's identity is scoped to that paragraph: a text box is walked as
    its own story, so the paragraphs inside it carry their own identities and the
    paragraph anchoring the box resumes carrying its own afterwards. What is not
    read out of a paragraph carries none -- display math standing between
    blocks, range markers between them, a table's own record, a cell's geometry
    record -- and neither does a ``table_cell`` run, which joins the current
    prose of every paragraph in its cell and so belongs to no one of them.
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
        extensions: dict[str, dict[str, object]],
        images: _EmbeddedImages,
    ) -> None:
        self._capture_id = capture_id
        self._images = images
        self._part = part
        self._story = story
        self._note_id = note_id
        self._styles = styles
        self._segments = segments
        self._package = package
        self._notes = notes
        self._seen = seen
        self._extensions = extensions
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
        # The identity of the paragraph being read, or None outside one, which is
        # where a block, a marker, a table's geometry and a cell's joined prose
        # all stand.
        self._para_id: str | None = None

    def walk(
        self,
        parent: ElementTree.Element,
        prefix: str,
        depth: int,
        state: str = "current",
    ) -> None:
        index = marks = 0
        for block, revised in _blocks(parent, state):
            tag = _local(block.tag)
            if tag in _RANGE_TAGS:
                self._mark(block, f"{prefix}m{marks}", revised, None)
                marks += 1
                continue
            position = f"{prefix}{index}"
            if tag == "p":
                self._paragraph(block, position, depth, revised)
            elif block.tag in _MATH_CONTENT:
                self._math(block, position, revised, None)
            else:
                self._table(block, position, depth, revised)
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
        and initials are kept as clipped strings and never resolved to anyone,
        and whatever the package's extended-comment part claimed about this
        comment -- its thread position and resolution -- is reported beside
        them, in the one record rather than a second one to correlate.
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
        extension = (
            {} if self._note_id is None else self._extensions.get(self._note_id, {})
        )
        if metadata or extension:
            self._inventory(
                "inventory",
                {
                    "kind": "commentMetadata",
                    "noteId": self._note_id,
                    **metadata,
                    **extension,
                },
                "annotation",
                False,
            )
        return False

    def inventory_applicability(
        self, applies: tuple[dict[str, object], ...]
    ) -> None:
        """Record every section and kind this header or footer part stands for.

        A part several sections share is read once, so its applicability is
        what says where its prose appears: one record per ``(section, kind)``
        in section order, each naming the relationship that placed it and
        whether that section declared it or inherited it from the one before.
        """
        self._inventory(
            "inventory",
            {"kind": "headerFooter", "story": self._story, "sections": applies},
            "visible",
            False,
        )

    def _inventory(
        self,
        position: str,
        value: dict[str, object],
        visibility: str,
        state: str,
        **extra: object,
    ) -> None:
        self._segments.append(
            ExtractedSegment(
                "object_inventory",
                self._locator(position, **extra),
                structured_value=value,
                extraction_method="inventory",
                visibility=_revised_visibility(state, visibility),
                revision_state=state,
            )
        )
        _bounded(self._segments)

    def _mark(
        self,
        element: ElementTree.Element,
        position: str,
        state: str,
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
            self._inventory(position, value, visibility, state)
        else:
            cell.marks.append((position, value, visibility, state, self._para_id))

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
        if self._para_id is not None:
            values["paragraphId"] = self._para_id
        return _locator(self._capture_id, **values, **extra)

    def _add(
        self,
        role: str,
        position: str,
        text: str,
        state: str,
        hidden: bool = False,
        extra: dict[str, object] | None = None,
    ) -> None:
        if hidden:
            # Hidden text keeps its own role and visibility in every story, so a
            # hidden note or cell is still hidden and never ordinary prose. A
            # revision over it is still recorded as the revision it is.
            semantic, visibility = "hidden_content", "hidden"
        elif state == "deleted":
            semantic, visibility = "deleted_text", "annotation"
        else:
            # Inserted text is text the author added, so it keeps the role and
            # the visibility the same words would have had unrevised; only its
            # ``revision_state`` says it was inserted.
            semantic = role
            visibility = "annotation" if role in {"note", "comment"} else "visible"
        self._segments.append(
            ExtractedSegment(
                semantic,
                self._locator(position, **(extra or {})),
                text=text,
                visibility=visibility,
                revision_state=state,
            )
        )
        _bounded(self._segments)

    def _paragraph(
        self,
        paragraph: ElementTree.Element,
        position: str,
        depth: int,
        state: str,
        cell: _CellRun | None = None,
    ) -> None:
        self.paragraphs += 1
        role = "table_cell" if cell else self._role(paragraph)
        # The identity belongs to this paragraph and to nothing around it: the
        # one standing here is held while its own items are read and put back in
        # the ``finally`` below, so a text box's paragraphs do not leak their
        # identities out to the paragraph anchoring the box, nor inherit its,
        # and a refusal raised mid-paragraph leaves no identity standing either.
        outer_para_id = self._para_id
        self._para_id = _docx_para_id(
            paragraph.attrib.get(_DOCX_PARA_ID), "identity", "DOCX paragraph"
        )
        try:
            current = removed = boxes = links = refs = fields = concealed = 0
            maths = images = charts = diagrams = objects = legacy = 0
            for kind, value, item_state in _paragraph_items(
                paragraph, state, self._fields
            ):
                if kind == "e":
                    assert isinstance(value, ElementTree.Element)
                    # Math is ordered where it sits, so in a cell it closes the
                    # current run rather than trailing the prose it interrupts.
                    self._flush(cell)
                    self._equation(value, f"{position}.e{maths}", item_state)
                    maths += 1
                elif kind == "g":
                    assert isinstance(value, ElementTree.Element)
                    # An image is ordered where it sits, for the reason math is.
                    self._flush(cell)
                    self._drawing(value, f"{position}.i{images}", item_state)
                    images += 1
                elif kind == "k":
                    assert isinstance(value, ElementTree.Element)
                    # A chart is ordered where it sits, for the reason an image is.
                    self._flush(cell)
                    self._chart(value, f"{position}.c{charts}", item_state)
                    charts += 1
                elif kind == "s":
                    assert isinstance(value, ElementTree.Element)
                    # SmartArt is ordered where it sits, for the reason a chart is.
                    self._flush(cell)
                    self._diagram(value, f"{position}.d{diagrams}", item_state)
                    diagrams += 1
                elif kind == "o":
                    assert isinstance(value, ElementTree.Element)
                    # An object is ordered where it sits, for the reason SmartArt is.
                    self._flush(cell)
                    self._object(value, f"{position}.o{objects}", item_state)
                    objects += 1
                elif kind == "p":
                    assert isinstance(value, ElementTree.Element)
                    # A legacy image is ordered where it sits, for the reason an
                    # object is.
                    self._flush(cell)
                    self._vml_image(value, f"{position}.v{legacy}", item_state)
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
                        self._mark(value, f"{position}.{label}", item_state, None)
                        continue
                    else:
                        label, refs = f"n{refs}", refs + 1
                    self._mark(value, f"{position}.{label}", item_state, cell)
                elif isinstance(value, dict):
                    label, fields = f"f{fields}", fields + 1
                    # A complex field is ordered at its own boundary, so in a cell it
                    # closes the current run for the same reason a simple field does.
                    self._flush(cell)
                    self._inventory(f"{position}.{label}", value, "visible", item_state)
                elif isinstance(value, ElementTree.Element):
                    self._flush(cell)
                    if depth >= _MAX_DOCX_DEPTH:
                        raise OOXMLInvalid("DOCX text boxes are nested too deeply")
                    # A text box is traversed as its own story, so its fields neither
                    # close nor are closed by the ones open around it: a boundary
                    # that tries to cross the box is refused, not leaked through.
                    outer, self._fields = self._fields, []
                    self.walk(value, f"{position}.x{boxes}.", depth + 1, item_state)
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
                        item_state,
                        hidden=True,
                    )
                    concealed += 1
                elif kind == "d":
                    self._flush(cell)
                    self._add("body", f"{position}.d{removed}", value, item_state)
                    removed += 1
                elif cell is not None:
                    # A run joins prose of one revision state, so text the
                    # author inserted closes the unrevised run before it rather
                    # than being joined into one stretch the cell never held.
                    if item_state != cell.state:
                        self._flush(cell)
                        cell.state = item_state
                    cell.parts.append(value)
                else:
                    # The first stretch read as prose keeps the paragraph's own
                    # position, whether the author wrote it or inserted it.
                    self._add(
                        role,
                        f"{position}.t{current}" if current else position,
                        value,
                        item_state,
                    )
                    current += 1
        finally:
            self._para_id = outer_para_id

    def _math(
        self,
        block: ElementTree.Element,
        position: str,
        state: str,
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
        self._equation(block, position, state)

    def _equation(
        self, element: ElementTree.Element, position: str, state: str
    ) -> None:
        """Record one equation as the markup it is, unread.

        The math is neither interpreted nor converted, so it is kept as its own
        segment and its text never joins the prose it interrupts. A revision
        wrapped around it is kept as the revision it is rather than dropped.
        """
        self._segments.append(
            ExtractedSegment(
                "equation",
                self._locator(position),
                structured_value=_docx_equation(element),
                visibility=_revised_visibility(state),
                revision_state=state,
            )
        )
        _bounded(self._segments)

    def _drawing(
        self, drawing: ElementTree.Element, position: str, state: str
    ) -> None:
        """Inventory one DrawingML image, then report the alt text its author wrote.

        The inventory record comes first and is emitted for every image the
        story anchors, whether or not its author claimed anything, so a story's
        images are inventoried where the XML placed them. It names where the
        object sits -- ``inline`` in the text flow or ``anchor`` against the
        page -- the claims ``wp:docPr`` carries, and the image the blip names:
        the embedded part's relationship, name, declared type and validated
        byte length, and separately the relationship, target, scheme and
        external flag of a linked one. That is inventory about an image rather
        than anything read out of one, and the record states exactly what was
        read: whether the bytes were decoded, whether the engine ran over them,
        and the one outcome that says why nothing was read where nothing was.

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

        Any text recognised in the image follows the author's claims, as the
        separate reading it is: it is anchored to this object's own position and
        to the image part it came from, and it is never joined to the prose
        around the drawing or to the alt text the author wrote.
        """
        blip = _docx_drawing_blip(drawing)
        assert blip is not None
        claims = _docx_doc_pr(drawing)
        record: dict[str, object] = {
            "kind": "drawingImage",
            "placement": _DOCX_DRAWINGS[drawing.tag],
            **{
                name: None if value is None else _clip(value, _MAX_META_CHARS)
                for name, value in claims.items()
            },
            **_docx_drawing_image(
                self._package, self._relations, self._relation_types, blip
            ),
        }
        result = self._embedded_image(
            position, record["imagePart"], record["imageContentType"]
        )
        self._inventory(
            position,
            {
                **record,
                "imageDecoded": result.decoded,
                "ocrPerformed": result.performed,
                "ocrOutcome": result.outcome,
            },
            "visible",
            state,
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
                    visibility=_revised_visibility(state),
                    revision_state=state,
                )
            )
            _bounded(self._segments)
        self._emit_embedded(result, state)

    def _chart(
        self, drawing: ElementTree.Element, position: str, state: str
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
        relation_id = _drawing_chart(drawing, "DOCX", "drawing")
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
            state,
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
                    visibility=_revised_visibility(state),
                    revision_state=state,
                )
            )
            _bounded(self._segments)

    def _diagram(
        self, drawing: ElementTree.Element, position: str, state: str
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

        A revision wrapped around the drawing is kept as the revision it is,
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
            state,
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
                    visibility=_revised_visibility(state),
                    revision_state=state,
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
                    visibility=_revised_visibility(state),
                    revision_state=state,
                )
            )
            _bounded(self._segments)

    def _object(
        self, element: ElementTree.Element, position: str, state: str
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

        A revision wrapped around the container is kept as the revision it is,
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
        self._inventory(position, record, "visible", state)

    def _vml_image(
        self, element: ElementTree.Element, position: str, state: str
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

        That is inventory about an image rather than anything read out of one,
        and the record states exactly what was read: whether the bytes were
        decoded, whether the engine ran over them, and the one outcome that says
        why nothing was read where nothing was. A reading of the image follows
        the author's claims as the separate reading it is, anchored to this
        shape's own position and to the image part it came from, and is never
        joined to the prose around the picture. An external reference is never
        opened, so it is never read.

        The shape's own markup rides along uninterpreted -- the identifier the
        legacy drawing layer knew it by, resolved against nothing, and the style
        it was drawn with, bounded and unparsed beyond the one declaration that
        says whether it is anchored.

        The author's claims then follow in one fixed order, each under
        ``image_alt`` with the role it played and the element that declared it, so
        the shape's title, the shape's alt text and the ``imagedata``'s Office
        title stay three distinguishable claims rather than one. A claim nothing
        wrote and a claim written empty are both reported by the record alone: no
        text is invented for an image whose author described it with none, least
        of all out of the shape's identifier.

        A revision wrapped around the container is kept as the revision it is,
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
        result = self._embedded_image(
            position, record["imagePart"], record["contentType"]
        )
        record["imageDecoded"] = result.decoded
        record["ocrPerformed"] = result.performed
        record["ocrOutcome"] = result.outcome
        self._inventory(position, record, "visible", state)
        for name, role, source in _DOCX_VML_ALT_CLAIMS:
            text = claims[name]
            if not text:
                continue
            self._segments.append(
                ExtractedSegment(
                    "image_alt",
                    self._locator(position, altRole=role, altSource=source),
                    text=text,
                    visibility=_revised_visibility(state),
                    revision_state=state,
                )
            )
            _bounded(self._segments)
        self._emit_embedded(result, state)

    def _embedded_image(
        self, position: str, part: object, media_type: object
    ) -> EmbeddedImageResult:
        """Read the raster this object embeds, under the document's one budget.

        The candidate carries this object's own locator, so two references to
        one image part stay two readings at two positions. ``part`` and
        ``media_type`` come from the record just built, so a container that
        named only a reference out of the package carries no part here and
        nothing of that reference is opened, fetched or probed.
        """
        return self._images.member(
            self._package, part, media_type, self._locator(position)
        )

    def _emit_embedded(self, result: EmbeddedImageResult, state: str) -> None:
        """Publish what was recognised in one embedded raster, as its own reading.

        Each reading keeps the role, method, language and confidence the image
        path gave it, so an accepted line stays searchable text read by OCR and
        a below-threshold observation stays out of ordinary retrieval. A
        revision wrapped around the container is kept over the readings exactly
        as it is over the record and the author's claims.
        """
        for segment in result.segments:
            self._segments.append(
                replace(
                    segment,
                    visibility=_revised_visibility(state, segment.visibility),
                    revision_state=state,
                )
            )
            _bounded(self._segments)

    def _flush(self, cell: _CellRun | None) -> None:
        """Close the cell's joined prose and release the marks waiting under it.

        The run joins the prose of every paragraph the cell holds that stands in
        one revision state, so it is sourced from no one of them and carries no
        ``paragraphId``: naming one
        would claim provenance the run does not have. Each waiting mark was read
        inside a single paragraph and is emitted under that paragraph's own
        identity, which is why the identity waited beside it. A flush stands
        inside the paragraph being read, so the identity it borrows is put back
        however the flush leaves -- a refusal raised here included.
        """
        if cell is None:
            return
        outer, self._para_id = self._para_id, None
        try:
            if cell.parts:
                position = (
                    f"{cell.position}.t{cell.runs}" if cell.runs else cell.position
                )
                self._add(
                    "table_cell",
                    position,
                    "\n".join(cell.parts),
                    cell.state,
                    extra=cell.grid,
                )
                cell.runs += 1
                cell.parts.clear()
            for position, value, visibility, state, para_id in cell.marks:
                self._para_id = para_id
                self._inventory(position, value, visibility, state)
            cell.marks.clear()
        finally:
            self._para_id = outer

    def _cell_inventory(
        self,
        position: str,
        grid: dict[str, object] | None,
        state: str,
        empty: bool,
    ) -> None:
        """Record one cell that emitted no ``table_cell`` run of its own.

        A cell holding no prose still has a place in the grid, and so does a
        vertical continuation whose text stands at its anchor. Either would
        otherwise leave no trace of a coordinate the table declared, so each
        closes with one record carrying the same geometry a text run would. The
        geometry rides in the locator as well, so the coordinate is addressable
        and not only readable out of the value.

        ``empty`` is a claim about the source cell, not about what the walk read
        out of it: a cell whose prose is deleted or hidden, or whose content was
        never prose -- an equation, a drawing, an embedded object, a field, an
        annotation, a text box, a nested table -- emits no run either, and
        calling that empty would report authored content as absent.
        """
        geometry = grid or {}
        self._inventory(
            position,
            {"kind": "docxTableCell", "empty": empty, **geometry},
            "visible",
            state,
            **geometry,
        )

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
        state: str,
    ) -> None:
        if depth > _MAX_DOCX_DEPTH:
            raise OOXMLInvalid("DOCX tables are nested too deeply")
        # The whole grid is planned, and refused if it is inconsistent, before
        # any of this table's text is read: a merge's extent is the plan's to
        # state, never something a cell infers from what has been seen so far.
        # ``position`` is the table's own path in the story, so a nested table
        # is identified by the cell path it hangs under and never by its shape.
        grid, plan = _docx_grid(table)
        located = {key: entry.located(position, grid) for key, entry in plan.items()}
        row_index = marks = 0
        for row in table:
            tag = _local(row.tag)
            if tag in _RANGE_TAGS:
                self._mark(row, f"{position}.m{marks}", state, None)
                marks += 1
            elif tag == "tr":
                self._row(
                    row, f"{position}.r{row_index}", depth, state, row_index, located
                )
                row_index += 1
        if not plan:
            # A table that declares a grid but holds no cell has no coordinate
            # to report, so the table itself is recorded once instead. A table
            # with cells reports them one by one and takes no record of its own.
            self._inventory(
                f"{position}.inventory",
                {
                    "kind": "docxTable",
                    "tablePath": position,
                    "tableColumns": None if grid is None else len(grid),
                    "tableGridWidths": grid,
                    "rowCount": row_index,
                    "cellCount": 0,
                },
                "visible",
                state,
            )

    def _row(
        self,
        row: ElementTree.Element,
        position: str,
        depth: int,
        state: str,
        row_index: int,
        located: dict[tuple[int, int], dict[str, object]],
    ) -> None:
        cell_index = marks = 0
        for cell in row:
            tag = _local(cell.tag)
            if tag in _RANGE_TAGS:
                self._mark(cell, f"{position}.m{marks}", state, None)
                marks += 1
                continue
            if tag != "tc":
                continue
            cell_position = f"{position}.c{cell_index}"
            grid = located.get((row_index, cell_index))
            cell_index += 1
            if grid is not None and grid["mergeState"] == "covered":
                if _docx_covered_cell_has_content(cell):
                    raise OOXMLInvalid(
                        "DOCX vertical merge continuation contains content"
                    )
                # The refusal above has just proved this continuation holds
                # nothing beyond its properties and Word's placeholder
                # paragraph, so its record is empty by construction.
                self._cell_inventory(cell_position, grid, state, True)
                continue
            run = _CellRun(cell_position, [], grid=grid, state=state)
            index = block_marks = 0
            for block, revised in _blocks(cell, state):
                block_tag = _local(block.tag)
                if block_tag in _RANGE_TAGS:
                    self._mark(block, f"{cell_position}.m{block_marks}", revised, run)
                    block_marks += 1
                    continue
                block_position = f"{cell_position}.{index}"
                index += 1
                if block_tag == "tbl":
                    self._flush(run)
                    self._table(block, block_position, depth + 1, revised)
                elif block.tag in _MATH_CONTENT:
                    self._math(block, block_position, revised, run)
                else:
                    self._paragraph(block, block_position, depth, revised, run)
            self._flush(run)
            if not run.runs:
                # Emitting no run is not the same as holding nothing, so what
                # the source cell was given is read off the cell itself by the
                # same predicate that refuses hidden content in a covered
                # continuation.
                self._cell_inventory(
                    cell_position,
                    grid,
                    state,
                    not _docx_covered_cell_has_content(cell),
                )


def _revised(tag: str, state: str, depth: int) -> tuple[str, int]:
    """Return the revision state and depth inside a ``tag`` element.

    The state is ``current`` outside every revision, ``inserted`` under a
    ``w:ins`` or ``w:moveTo``, and ``deleted`` under a ``w:del`` or
    ``w:moveFrom``. An insertion inside a deletion has no coherent reading, and
    an unbounded stack of wrappers is not a document anyone wrote, so both are
    refused. A deletion inside an insertion is how Word records
    inserted-then-deleted text and stays deleted.
    """
    if tag not in _INSERTED and tag not in _REMOVED:
        return state, depth
    if state == "deleted" and tag in _INSERTED:
        raise OOXMLInvalid("DOCX revisions contradict each other")
    if depth >= _MAX_REVISION_DEPTH:
        raise OOXMLInvalid("DOCX revisions are nested too deeply")
    return ("deleted" if tag in _REMOVED else "inserted"), depth + 1


def _revised_visibility(state: str, visible: str = "visible") -> str:
    """Say how content standing in revision ``state`` is read on the page.

    A deletion is not the page's content, so whatever it wrapped reads as
    annotation however the content would otherwise be read; an insertion is
    content the author added and reads exactly as unrevised content does. Every
    DOCX segment takes its visibility from here and its ``revision_state`` from
    the state itself, so a feature already handed the traversal state states an
    insertion correctly without knowing the rule.
    """
    return "annotation" if state == "deleted" else visible


def _blocks(
    parent: ElementTree.Element, state: str
) -> Iterator[tuple[ElementTree.Element, str]]:
    """Yield paragraphs, tables and display math below ``parent`` in document order.

    Comment range markers between blocks are yielded too. Each comes with the
    revision state it stands in. Transparent wrappers
    (``body``, content controls, revisions) are entered; a yielded paragraph,
    table or equation is not entered, so nothing is seen twice. Math is a block
    of its own where it stands between blocks rather than a wrapper, so its own
    structure is never mistaken for the story's.
    """
    stack = [(iter(parent), state, 0)]
    while stack:
        children, inherited, depth = stack[-1]
        for child in children:
            tag = _local(child.tag)
            if child.tag in _MATH_CONTENT or tag in {"p", "tbl"} or tag in _RANGE_TAGS:
                yield child, inherited
            elif tag not in _NON_BLOCK_TAGS:
                inner, level = _revised(tag, inherited, depth)
                stack.append((iter(child), inner, level))
                break
        else:
            stack.pop()


@dataclass(slots=True)
class _DocxCell:
    """One cell's place in its table's grid, as the table itself declares it.

    ``row`` and ``cell`` are the cell's own source ordinals, the ones its
    position is built from; ``column`` is where the declared spans put it in the
    grid. ``anchor`` is the source ordinal pair of the cell a merge is anchored
    at -- its own, unless a vertical continuation put it under another -- so a
    covered coordinate cites the one cell whose text stands for the merge
    instead of repeating it.
    """

    row: int
    cell: int
    column: int
    column_span: int
    row_span: int
    anchor: tuple[int, int]

    def located(
        self, path: str, grid: tuple[int | None, ...] | None
    ) -> dict[str, object]:
        """Say where this cell sits, in source and in grid coordinates."""
        covered = self.anchor != (self.row, self.cell)
        merged = self.row_span > 1 or self.column_span > 1
        return {
            "tablePath": path,
            "tableColumns": None if grid is None else len(grid),
            "tableGridWidths": grid,
            "sourceRow": self.row,
            "sourceCell": self.cell,
            "gridRow": self.row,
            "gridColumn": self.column,
            "rowSpan": self.row_span,
            "columnSpan": self.column_span,
            "mergeState": "covered" if covered else "anchor" if merged else "none",
            "mergeAnchor": f"{path}.r{self.anchor[0]}.c{self.anchor[1]}",
        }


def _docx_grid(
    table: ElementTree.Element,
) -> tuple[tuple[int | None, ...] | None, dict[tuple[int, int], _DocxCell]]:
    """Plan one table's grid before any of its cells is read.

    The declared ``tblGrid`` settles how wide the table says it is, and each
    row's own ``gridSpan`` and ``vMerge`` settle where its cells sit in that
    grid. Nothing is inferred: a vertical continuation must continue a merge the
    same column opened in the row directly above, at the same width, and no row
    may reach past the grid the table declared. The plan is finished before a
    cell is emitted, so a merge's extent is known at its anchor and no covered
    coordinate ever repeats the anchor's text.

    Rows and cells are counted exactly as the walk counts them, so every
    ``tc`` the walk reaches is planned and nothing a row holds beside its cells
    shifts a coordinate.
    """
    declared = _docx_table_grid(table)
    bound = _MAX_DOCX_TABLE_COLUMNS if declared is None else len(declared)
    plan: dict[tuple[int, int], _DocxCell] = {}
    above: dict[int, _DocxCell] = {}
    row_index = 0
    for row in table:
        if _local(row.tag) != "tr":
            continue
        if row_index >= _MAX_DOCX_TABLE_ROWS:
            raise OOXMLInvalid("DOCX table declares too many rows")
        opened: dict[int, _DocxCell] = {}
        column = cell_index = 0
        for cell in row:
            if _local(cell.tag) != "tc":
                continue
            # The plan is held whole, so it is bounded by what the extraction
            # could emit from it: a table with more cells than the segment cap
            # is refused here rather than planned and then refused on emission.
            if len(plan) >= MAX_SEGMENTS:
                raise OOXMLInvalid("DOCX table declares too many cells")
            span, merge = _docx_cell_span(cell)
            if column + span > bound:
                raise OOXMLInvalid("DOCX table row is wider than its grid")
            if merge == "continue":
                anchor = above.get(column)
                if anchor is None or anchor.column_span != span:
                    raise OOXMLInvalid("DOCX vertical cell merge continues nothing")
                anchor.row_span += 1
                entry = _DocxCell(row_index, cell_index, column, span, 1, anchor.anchor)
                opened[column] = anchor
            else:
                entry = _DocxCell(
                    row_index, cell_index, column, span, 1, (row_index, cell_index)
                )
                if merge == "restart":
                    opened[column] = entry
            plan[row_index, cell_index] = entry
            column += span
            cell_index += 1
        above = opened
        row_index += 1
    # The anchor's extent is the merge's, so every covered coordinate reports
    # the region it belongs to rather than the one row it occupies.
    for entry in plan.values():
        entry.row_span = plan[entry.anchor].row_span
    return declared, plan


def _docx_table_grid(table: ElementTree.Element) -> tuple[int | None, ...] | None:
    """Return the qualified column-width claims in this table's own grid."""
    grids = [node for node in table if node.tag in _DOCX_TBL_GRID]
    if len(grids) > 1:
        raise OOXMLInvalid("DOCX table declares more than one grid")
    if not grids:
        return None
    columns = [node for node in grids[0] if node.tag in _DOCX_GRID_COL]
    if len(columns) > _MAX_DOCX_TABLE_COLUMNS:
        raise OOXMLInvalid("DOCX table declares too many columns")
    widths: list[int | None] = []
    for column in columns:
        values = [(name, column.attrib[name]) for name in _DOCX_WIDTH if name in column.attrib]
        if len(values) > 1:
            raise OOXMLInvalid("DOCX table column declares two widths")
        if not values:
            widths.append(None)
            continue
        namespace = column.tag.partition("}")[0].lstrip("{")
        if values[0][0] != f"{{{namespace}}}w":
            raise OOXMLInvalid("DOCX table column width uses another profile")
        raw = values[0][1]
        if _DIGITS.fullmatch(raw) is None or int(raw) > _MAX_DOCX_COLUMN_TWIPS:
            raise OOXMLInvalid("DOCX table column width is malformed")
        widths.append(int(raw))
    return tuple(widths)


def _docx_covered_cell_has_content(cell: ElementTree.Element) -> bool:
    """Whether a cell stores anything beyond its properties and an empty paragraph.

    Word normally writes one empty paragraph in a covered continuation.  That
    placeholder is accepted; text, fields, annotations, drawings, math, nested
    tables, or any other authored block would otherwise be silently discarded,
    so the package is refused instead.

    The same question decides whether a cell that emitted no ``table_cell`` run
    is recorded as empty, which is why only the cell's own markup is read and
    never what the walk made of it.  Anything that is not a cell property, an
    empty paragraph or an empty run counts as content, so a transparent wrapper
    -- a revision, a content control -- is itself content rather than something
    this looks through: a wrapper can only ever overstate what a cell holds,
    never hide it.
    """
    for child in cell:
        if child.tag in _DOCX_TC_PR:
            continue
        if _local(child.tag) != "p":
            return True
        if child.text:
            return True
        for node in child:
            local = _local(node.tag)
            if local == "pPr":
                continue
            if local != "r" or node.text:
                return True
            if any(_local(item.tag) != "rPr" for item in node):
                return True
    return False


def _docx_cell_span(cell: ElementTree.Element) -> tuple[int, str | None]:
    """Read one cell's declared horizontal span and vertical merge state.

    Both are read off the cell's own properties and nowhere else, and a cell
    that declares either of them twice has no single geometry to read.
    """
    properties = [node for node in cell if node.tag in _DOCX_TC_PR]
    if len(properties) > 1:
        raise OOXMLInvalid("DOCX table cell declares its properties twice")
    span: int | None = None
    merge: str | None = None
    for node in properties[0] if properties else ():
        if node.tag in _DOCX_GRID_SPAN:
            if span is not None:
                raise OOXMLInvalid("DOCX table cell declares its span twice")
            span = _docx_span_value(node)
        elif node.tag in _DOCX_V_MERGE:
            if merge is not None:
                raise OOXMLInvalid("DOCX table cell declares its merge twice")
            merge = _docx_merge_value(node)
    return span if span is not None else 1, merge


def _docx_span_value(element: ElementTree.Element) -> int:
    """Read a ``gridSpan``, which the schema requires to carry its own count."""
    value = _docx_val(element)
    if value is None or _DIGITS.fullmatch(value) is None or int(value) < 1:
        raise OOXMLInvalid("DOCX horizontal cell span is malformed")
    if int(value) > _MAX_DOCX_TABLE_COLUMNS:
        raise OOXMLInvalid("DOCX horizontal cell span is too wide")
    return int(value)


def _docx_merge_value(element: ElementTree.Element) -> str:
    """Read a ``vMerge``, whose own default is to continue the merge above."""
    value = _docx_val(element)
    if value is None:
        return "continue"
    if value not in _DOCX_MERGE_VALUES:
        raise OOXMLInvalid("DOCX vertical cell merge is malformed")
    return value


def _docx_val(element: ElementTree.Element) -> str | None:
    """Read a ``w:val`` written in either profile, refusing one written twice."""
    values = [element.attrib[name] for name in _DOCX_VAL if name in element.attrib]
    if len(values) > 1:
        raise OOXMLInvalid("DOCX table property declares two values")
    if not values:
        return None
    namespace = element.tag.partition("}")[0].lstrip("{")
    if f"{{{namespace}}}val" not in element.attrib:
        raise OOXMLInvalid("DOCX table property value uses another profile")
    return values[0]


def _paragraph_items(
    paragraph: ElementTree.Element,
    state: str,
    fields: list[_FieldState],
) -> list[tuple[str, str | ElementTree.Element | dict[str, object], str]]:
    """Split a paragraph into ordered ``(kind, value, state)`` items.

    Each item carries the revision state it stands in -- ``current``,
    ``inserted`` or ``deleted`` -- and text is one stretch only while that
    state holds, so inserted text is never joined to the unrevised text beside
    it any more than deleted text is.

    Kinds are ``t`` text that is read as prose (current or inserted), ``d``
    deleted text, ``v`` text a run's own properties hide (carrying the revision
    state it stands in), ``x`` a text box,
    ``e`` an ``m:oMath`` or ``m:oMathPara`` equation, ``g`` a ``wp:inline`` or
    ``wp:anchor`` drawing object that displays an image, ``k`` one that names a
    chart, ``s`` one that names SmartArt, ``o`` a ``w:object`` declaring a
    legacy embedded or linked object, ``p`` a ``w:pict`` showing a standalone
    legacy image,
    ``m`` a hyperlink, simple field or annotation marker, and ``c`` an
    already recorded complex field.
    Adjacent text of one kind and one state is one stretch; tabs and breaks stay
    separators inside it. Hidden text is a stretch of its own, so it is never
    joined to the visible text around it.

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
    items: list[tuple[str, str | ElementTree.Element | dict[str, object], str]] = []
    pending: list[str] = []
    pending_kind = ""
    pending_state = "current"

    # Markers follow the stretch they sit in, so they never split the text.
    deferred: list[tuple[str, str | ElementTree.Element | dict[str, object], str]] = []

    def close() -> None:
        if pending:
            items.append((pending_kind, "".join(pending), pending_state))
            pending.clear()
        items.extend(deferred)
        deferred.clear()

    stack = [(iter(paragraph), state, 0, False)]
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
                marked = "d" if state == "deleted" else "t"
                if (marked, state) != (pending_kind, pending_state):
                    close()
                    pending_kind, pending_state = marked, state
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
                # ``delText`` is deleted text wherever it stands, so a run that
                # wrote it outside a ``w:del`` is still read as the deletion it
                # declares itself to be rather than as the state around it.
                text_state = "deleted" if tag == "delText" else state
                kind = "v" if hidden else "d" if text_state == "deleted" else "t"
                if (kind, text_state) != (pending_kind, pending_state):
                    close()
                    pending_kind, pending_state = kind, text_state
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


def _drawing_chart(element: ElementTree.Element, owner: str, holder: str) -> str | None:
    """Return the relationship id one graphic holder names its chart on, or None.

    The route in is the schema's own and nothing else: the holder's direct
    ``a:graphic``, that graphic's direct ``a:graphicData``, and the ``c:chart``
    directly below that. Every step is held to the qualified names OOXML
    defines for it -- the two DrawingML main namespaces for the graphic and its
    data, the two DrawingML chart namespaces for the reference -- so an element
    merely called ``graphic``, ``graphicData`` or ``chart`` in another
    vocabulary is not on the route and names nothing, whichever step it stands
    at. A reference reached anywhere else below the holder -- in an
    alternate-content fallback, inside a text box's own content, in an
    extension list, below another graphic carried along -- is not what this
    holder displays and is passed over.

    One holder displays one graphic, so two of anything along that route leaves
    which chart it shows unanswerable and nothing here chooses between them. A
    reference that names no relationship identifies no chart, and no chart path
    is derived from the holding part's own name: the id is resolved through that
    part's relationships or not at all.

    A Word story's drawing object and a worksheet drawing's graphic frame
    declare a chart on the one route, so both are read here. ``owner`` is the
    format the holder came from and ``holder`` is what that format calls it,
    carried through so a refusal names the file it came from and never the
    other format.
    """
    charts = [
        node
        for graphic in element
        if graphic.tag in _DRAWING_GRAPHIC
        for data in graphic
        if data.tag in _DRAWING_GRAPHIC_DATA
        for node in data
        if node.tag in _DRAWING_CHART
    ]
    if not charts:
        return None
    if len(charts) > 1:
        raise OOXMLInvalid(f"{owner} {holder} declares more than one chart reference")
    relation_id = _relationship_id(charts[0].attrib)
    if not relation_id:
        raise OOXMLInvalid(f"{owner} chart reference names no relationship")
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
            ("k", _drawing_chart(drawing, "DOCX", "drawing") is not None),
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
    """Name the annotation story one part holds, by the name Word gives it.

    Headers and footers are not named here: they are reached through the
    document's own sections and relationships, which say what each part is
    regardless of what it is called.
    """
    name = posixpath.basename(part)
    for prefix in ("footnote", "endnote", "comment"):
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


def _outside_extensions(root: ElementTree.Element) -> Iterator[ElementTree.Element]:
    """Yield every descendant of ``root`` in document order, never entering an extension.

    An ``extLst`` and an ``ext`` are payload the reader does not understand, and
    it reports each as coverage only, so nothing below one may be read as a cell,
    a formula, a row, a merge or any other structure -- whatever it is named.
    Nothing is stripped or copied: the subtree is simply not walked, and what it
    holds is never inspected.
    """
    stack = [iter(root)]
    while stack:
        for node in stack[-1]:
            if _local(node.tag) in ("extLst", "ext"):
                continue
            yield node
            stack.append(iter(node))
            break
        else:
            stack.pop()


def _locator(capture_id: str, **values: object) -> dict[str, object]:
    return {"captureId": capture_id, **values}


def _bounded(segments: Iterable[ExtractedSegment]) -> None:
    if isinstance(segments, list) and len(segments) > MAX_SEGMENTS:
        raise OOXMLInvalid("OOXML extraction has too many segments")
