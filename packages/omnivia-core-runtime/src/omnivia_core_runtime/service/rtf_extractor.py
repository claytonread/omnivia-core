"""Bounded RTF lexical scan plus fixed-argument Pandoc JSON extraction.

The scanner reads the control stream once and reports what the source carries.
It never decodes text, never accumulates document content beyond one bounded
field-keyword prefix and one picture payload at a time, never resolves a path or
a URL, and never executes a field or an embedded object: an external reference
is counted by kind and the path or URL itself is not even read out of the bytes.

The pinned reader extracts body text only, so the scan classifies every other
feature it finds as inventoried-but-disabled (recognised, deliberately not
extracted) or unsupported/unknown. Nothing here claims complete extraction.

The reader itself is the Pandoc 11.2 bundles with the distribution: an absolute
executable the verified installation payload declares
(:mod:`omnivia_core_runtime.distribution.packaged_runtime`), handed over in the
frozen settings. :func:`pinned_reader` refuses anything else, so an installation
with no verified payload reads no RTF rather than resolving a bare name against
whatever is on the worker's search path. The arguments beside it are the fixed
vector of 11.9 and are never derived from the source.

The one exception is a raster a ``\\pict`` group encodes inline, which the
trusted settings may ask to have recognised. The scanner stays authoritative for
it: the payload's extent is established by the same single pass that tracks
group nesting, escaped delimiters, Unicode fallback and length-delimited binary
sections, so the bytes handed to the image path are exactly the ones the group
delimits. A picture inside an object payload, an ``\\objdata`` blob, a field
instruction or a field result is never a candidate at all -- none of those is
executed, resolved or decoded -- and no resource beside the source is ever
opened, so an RTFD bundle's sidecar images are unreachable from here by
construction.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

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
    ExtractionBudget,
)

MAX_SOURCE_BYTES: Final = 20 * 1024 * 1024
MAX_AST_BYTES: Final = 128 * 1024 * 1024
MAX_GROUP_DEPTH: Final = 128
MAX_BLOCKS: Final = 100_000
#: The RTF specification's own control-word length limit.
MAX_CONTROL_WORD_LETTERS: Final = 32
#: Scan bound: digits in a control word's numeric parameter. The specification
#: gives a signed 32-bit value, so a longer run is malformed and is refused
#: before any integer conversion, which would otherwise raise on its own.
MAX_PARAMETER_DIGITS: Final = 10
#: Output bound: distinct unknown-destination names the inventory will name,
#: and distinct code pages the scan will report.
MAX_INVENTORY_NAMES: Final = 64
#: Output bound: bytes of one field instruction kept to read its leading keyword.
MAX_FIELD_PREFIX_BYTES: Final = 64
#: Output bound: picture groups the limitations describe one by one. The exact
#: count and the per-outcome counts are reported for every one of them, so this
#: caps the detail and never the truth.
MAX_PICTURE_RECORDS: Final = 256
#: AST walk bounds: nesting depth, total nodes visited, and characters of one
#: link or image target kept in its inventory segment.
MAX_AST_DEPTH: Final = 64
MAX_AST_NODES: Final = 4_000_000
MAX_TARGET_CHARS: Final = 2048
PANDOC_BUILD: Final = "pandoc-3.12.1-rtf-json"
#: The fixed reader arguments of 11.9, and the whole of them. No filter, no
#: template, no default file, no include path and no PDF engine is ever added,
#: and nothing here is derived from the source. The executable is not among them:
#: it is the absolute packaged binary :func:`pinned_reader` resolves.
PANDOC_ARGUMENTS: Final = ("--from=rtf", "--to=json", "--sandbox")

_SCHEME: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{1,31}):")
_FORMATTING: Final = frozenset(
    {
        "Emph",
        "Underline",
        "Strong",
        "Strikeout",
        "Superscript",
        "Subscript",
        "SmallCaps",
    }
)
#: Roles whose text is not document body, matching the OOXML extractor.
_ANNOTATION_ROLES: Final = frozenset({"note", "comment", "deleted_text"})
_HEX_DIGITS: Final = frozenset(b"0123456789abcdefABCDEF")
#: Declared character sets; a code page arrives separately as ``\ansicpgN``.
_CHARSETS: Final = frozenset({"ansi", "mac", "pc", "pca"})
#: Destinations recognised at group top, mapped onto the inventory key they
#: report under so related control words collapse into one count.
_INVENTORIED: Final = {
    "header": "header",
    "headerl": "header",
    "headerr": "header",
    "headerf": "header",
    "footer": "footer",
    "footerl": "footer",
    "footerr": "footer",
    "footerf": "footer",
    "footnote": "footnote",
    "annotation": "annotation",
    "atnid": "annotation",
    "atnauthor": "annotation",
    "atnref": "annotation",
    "atndate": "annotation",
    "atnparent": "annotation",
    "atnicn": "annotation",
    "comment": "comment",
    "revtbl": "revision",
    "field": "field",
    "fldinst": "field_instruction",
    "fldrslt": "field_result",
    "hlinkbase": "hyperlink_base",
    "pict": "picture",
    "shppict": "picture",
    "nonshppict": "picture",
    "object": "object",
    "objdata": "object_data",
    "objclass": "object_class",
    "objname": "object_name",
    "objalias": "object_alias",
    "objsect": "object_section",
    "nesttableprops": "nested_table",
    "datafield": "merge_data_field",
}
#: Character, row and object properties that are inventoried wherever they
#: appear, because they qualify content rather than open a destination.
_MARKERS: Final = {
    "revised": "revision_marked",
    "deleted": "deleted_content",
    "objautlink": "object_auto_link",
    "objlink": "object_linked",
    "objemb": "object_embedded",
    "objpub": "object_publisher",
    "objsub": "object_subscriber",
    "objhtml": "object_html",
    "objocx": "object_control",
    "nestcell": "nested_table",
    "nestrow": "nested_table",
    "flddirty": "field_dirty",
    "fldlock": "field_locked",
    "fldedit": "field_edited",
}
#: The picture types a ``\pict`` group may declare, mapped onto the media type
#: the raster path would admit the payload under. ``None`` is a type declared
#: and recognised as outside the accepted raster set -- a metafile, a
#: device-independent bitmap, a Mac picture -- which is refused as the type it
#: declared rather than guessed at or decoded.
_PICTURE_TYPES: Final[dict[str, str | None]] = {
    "pngblip": "image/png",
    "jpegblip": "image/jpeg",
    "emfblip": None,
    "wmetafile": None,
    "pmmetafile": None,
    "dibitmap": None,
    "wbitmap": None,
    "macpict": None,
}
#: Destinations whose content is an object payload or a field. A picture group
#: inside one of them is never a candidate: an OLE payload, an ``\objdata``
#: blob, a linked object's stand-in and the cached result of a field such as
#: ``INCLUDEPICTURE`` are references this adapter neither executes, resolves nor
#: decodes as an image, and the inventory already states that they are present.
#: The flag is inherited, so nesting cannot reach past it.
# ponytail: a field's whole result is disqualified rather than only the fields
# whose keyword names an external picture, so a picture inside a `\fldrslt` of
# an ordinary HYPERLINK field is inventoried and not read. Reading those needs
# the field keyword to reach this decision, which the scan only settles when the
# instruction group closes -- after the result group has already been walked.
_PICTURE_DISQUALIFIERS: Final = frozenset(
    {
        "object",
        "objdata",
        "objclass",
        "objname",
        "objalias",
        "objsect",
        "result",
        "field",
        "fldinst",
        "fldrslt",
    }
)
#: Destinations that open a story of their own, which a picture's locator names
#: so a reading can be traced to the part of the document that displayed it.
_STORY_WORDS: Final = frozenset(
    {
        "header",
        "headerl",
        "headerr",
        "headerf",
        "footer",
        "footerl",
        "footerr",
        "footerf",
        "footnote",
        "annotation",
        "comment",
    }
)
#: Whitespace that separates a hex payload's digits without being part of one.
_PAYLOAD_SPACE: Final = frozenset({0x00, 0x09, 0x0A, 0x0D, 0x20})
#: Field instruction keywords that name an external or executable reference.
#: The keyword is the whole risk signal kept; the operand is never read.
_FIELD_RISKS: Final = frozenset(
    {
        "HYPERLINK",
        "INCLUDE",
        "INCLUDEPICTURE",
        "INCLUDETEXT",
        "DDE",
        "DDEAUTO",
        "LINK",
        "IMPORT",
        "EMBED",
        "MACROBUTTON",
    }
)
#: Control words that legitimately open a group without being an unknown
#: destination: first the document, table and metadata destinations this scan
#: has nothing to say about, then the formatting words that commonly lead a
#: group.
# This is a name list, not a full control-word table. A formatting word absent
# here is reported as an unknown destination; extend the list if that is noisy.
_KNOWN_WORD_NAMES: Final = """
    rtf fonttbl fname falt panose colortbl stylesheet listtable
    listoverridetable list listlevel listoverride listname listpicture pntext
    pntxta pntxtb pn pnseclvl info title subject author operator manager
    company category keywords doccomm creatim revtim printim buptim userprops
    propname staticval generator latentstyles lsdlockedexcept themedata
    colorschememapping datastore xmlnstbl xmlns rsidtbl filetbl file pgdsctbl
    pgdsc pgdscno upr ud shp shpinst sp sn sv shptxt do dptxbxtext bkmkstart
    bkmkend tc tcn xe txe template formfield fldtype ffname ffdeftext ffformat
    ffhelptext ffstattext ftnsep ftnsepc ftncn aftnsep aftnsepc aftncn
    wgrffmtfilter mmath mmathpict factoidname nonesttables docvar

    f fs af afs b i ul ulnone ulw uld strike striked scaps caps outl shad embo
    impr plain cf cb chcfpat chcbpat highlight pard par sect sectd line page
    tab ql qc qr qj qd li ri fi sa sb sl slmult keep keepn widctlpar
    nowidctlpar intbl itap row cell trowd cellx trgaph clvertalt clbrdrt
    clbrdrl clbrdrb clbrdrr brdrs brdrw lang langnp langfe langfenp loch hich
    dbch ltrch rtlch ltrpar rtlpar super sub nosupersub up dn expnd expndtw
    kerning charscalex v animtext deff deflang deflangfe adeff adeflang uc u
    bin s cs ds ts tsrowd insrsid charrsid delrsid pararsid sectrsid tblrsid
    rsid chftn chftnsep chdate chtime chpgn fcharset fprq cpg ansicpg ansi mac
    pc pca nowwrap noproof hyphpar fittext shading
"""
_KNOWN_WORDS: Final = frozenset(_KNOWN_WORD_NAMES.split())


class RTFExtractionInvalid(ValueError):
    """The RTF is malformed, unsupported, or beyond an extraction bound."""


class RTFReaderUnavailable(RTFExtractionInvalid):
    """This installation packages no RTF reader, so it qualifies no RTF adapter.

    Its own class because it is the one refusal on this path that says nothing
    about the source. A well-formed RTF read by an installation with no verified
    payload is not an invalid document, and settling it as one would record a
    verdict on bytes that are fine; :func:`document_extractors.extract_document`
    turns this into the unsupported-format outcome instead, whose recovery is
    repairing the installation and reprocessing the retained copy.
    """


def pinned_reader(settings: ExtractionSettings | None) -> str:
    """The absolute packaged Pandoc this extraction invokes, or a refusal.

    11.2 bundles the RTF reader with the distribution and 11.5 gives the worker
    no arbitrary configuration paths, so the reader is the executable the
    verified installation payload declares and nothing else: not a bare name
    resolved against a search path, not an environment variable, not a value a
    caller or a document supplied.

    An installation with no verified packaged reader therefore reads no RTF.
    That is a refusal of the *source* in the only sense this adapter can report
    one -- there is no qualified reader for it here -- and it is deliberately
    not a silent fall back to whatever binary a host happens to have on
    ``PATH``, whose identity no manifest pins.
    """
    reader = None if settings is None else settings.pandoc_path
    if not reader or not reader.startswith("/"):
        raise RTFReaderUnavailable(
            "no absolute packaged RTF reader is bound to this extraction"
        )
    return reader


@dataclass(frozen=True, slots=True)
class RTFExtraction:
    segments: tuple[ExtractedSegment, ...]
    blocks: int
    limitations: dict[str, object]
    #: Embedded rasters whose bytes were opened and could not be read. The
    #: structural extraction around them stands, so these degrade the document's
    #: outcome rather than refusing the source.
    failed_units: int = 0
    #: The aggregate budget the bounded embedded-image pass exhausted, if any.
    limit_breach: str | None = None


def extract_rtf(
    content: bytes,
    *,
    capture_id: str,
    settings: ExtractionSettings | None = None,
    ocr_engine: OcrEngine = run_tesseract,
    budget: ExtractionBudget | None = None,
) -> RTFExtraction:
    """Scan one admitted RTF source, read its body text, and only then its rasters.

    The scan and the pinned reader are what they have always been and are the
    whole extraction by default: ``settings`` defaults to the explicit text-only
    profile, so a caller that hands none keeps no picture payload at all.

    A ``\\pict`` group's raster is examined only where the trusted settings say
    to and their pinned models were served. Every candidate of one source shares
    one aggregate execution budget, and a candidate that is refused,
    unsupported, malformed or cut short keeps the structural record it already
    had and reports exactly why nothing was read out of it.

    ``budget`` is that aggregate ceiling, injectable so a breach can be
    exercised with a small budget rather than a real allocation.
    """
    images = EmbeddedImagePass.over(settings, ocr_engine=ocr_engine, budget=budget)
    scan = _scan_rtf(content, capture_id=capture_id, images=images)
    reader = pinned_reader(settings)
    try:
        completed = subprocess.run(
            [reader, *PANDOC_ARGUMENTS],
            input=content,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RTFExtractionInvalid("the pinned RTF reader is unavailable") from error
    if completed.returncode != 0:
        raise RTFExtractionInvalid("RTF was refused by the pinned reader")
    if len(completed.stdout) > MAX_AST_BYTES:
        raise RTFExtractionInvalid("RTF structured output exceeds the accepted bound")
    try:
        document = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RTFExtractionInvalid(
            "RTF reader returned malformed structured output"
        ) from error
    blocks = document.get("blocks") if isinstance(document, dict) else None
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise RTFExtractionInvalid("RTF block count is outside the accepted bound")
    walker = _AstWalker(capture_id)
    try:
        walker.blocks(blocks, "blocks", "body", 0)
    except RecursionError as error:
        raise RTFExtractionInvalid(
            "RTF structure exceeds the accepted bound"
        ) from error
    limitations: dict[str, object] = {
        **scan.to_limitations(),
        **walker.limitations(),
        "readerBuild": PANDOC_BUILD,
        "astLocatorsAreDerived": True,
    }
    if images.limit_breach is not None:
        limitations["limitBreach"] = images.limit_breach
    return RTFExtraction(
        # The picture readings follow the body text rather than being woven into
        # it: a reading is anchored to the scanner's own group path and byte
        # interval, and the body text to the reader's derived AST path, so there
        # is no one order the two coordinate systems share.
        (*walker.segments, *scan.segments),
        len(blocks),
        limitations,
        failed_units=images.failed_units,
        limit_breach=images.limit_breach,
    )


@dataclass(slots=True)
class _Group:
    """One open RTF group's scoped state."""

    #: ``\ucN``: fallback characters to skip after a ``\uN``; scoped, inherited.
    uc: int
    #: Inside a ``\*`` destination, so its content is not document body text.
    ignorable: bool
    #: The bounded keyword prefix of the enclosing field instruction, if any.
    field_prefix: bytearray | None
    #: The group's first control word, which names its destination.
    word: str | None = None
    #: A ``\*`` was seen, so the destination word that follows is ignorable.
    starred: bool = False
    #: Still waiting for the first token, which decides the destination.
    expect_word: bool = True
    #: The group or a descendant carried substantive content.
    text: bool = False
    #: The offset of this group's own ``{``, which starts its byte interval.
    start: int = 0
    #: The group's place among its parent's groups, built from sibling indices
    #: so the same source always locates the same group the same way.
    path: str = ""
    #: Groups opened in this one so far, which is the next child's index.
    children: int = 0
    #: The story this group's content belongs to; scoped, inherited.
    story: str = "body"
    #: Inside an object payload or a field, so no picture here is a candidate;
    #: scoped, inherited, so nesting cannot reach past it.
    disqualified: bool = False
    #: The picture this group is, when ``\pict`` opened it.
    picture: _Picture | None = None


@dataclass(slots=True)
class _Picture:
    """One ``\\pict`` group's declared identity and its payload as scanned.

    ``digits`` and ``binary`` hold the payload for the duration of one
    recognition and are dropped with the group that closes: nothing persisted
    ever holds image bytes. They are filled only while ``keep`` is set, which is
    settled from the shared pass before the group's first payload byte is read,
    so a profile that examines no embedded image accumulates nothing. The two
    counters are kept either way, because how much payload a group declared is
    what tells an unexamined picture from one that embedded no bytes at all.
    """

    path: str
    story: str
    start: int
    keep: bool
    #: The control word that declared the type, exactly as written.
    declared: str | None = None
    #: The media type the raster path would admit this payload under, or
    #: ``None`` for a type outside the accepted set or none declared at all.
    media_type: str | None = None
    hex_digits: int = 0
    binary_bytes: int = 0
    digits: bytearray = field(default_factory=bytearray)
    binary: bytearray = field(default_factory=bytearray)
    #: The payload cannot be read as written: a byte that is no hex digit, an
    #: escape inside the payload, or an odd number of digits.
    malformed: bool = False


@dataclass(slots=True)
class _Scan:
    #: The one bounded embedded-raster pass this source's pictures share.
    images: EmbeddedImagePass
    capture_id: str = ""
    counts: Counter[str] = field(default_factory=Counter)
    inventory: Counter[str] = field(default_factory=Counter)
    unknown: Counter[str] = field(default_factory=Counter)
    ignorable: Counter[str] = field(default_factory=Counter)
    field_risks: Counter[str] = field(default_factory=Counter)
    charsets: set[str] = field(default_factory=set)
    code_pages: set[int] = field(default_factory=set)
    groups: int = 0
    maximum_depth: int = 0
    #: What was read out of the pictures, in source order.
    segments: list[ExtractedSegment] = field(default_factory=list)
    #: One bounded record per picture group, in source order.
    pictures: list[dict[str, object]] = field(default_factory=list)

    def to_limitations(self) -> dict[str, object]:
        return {
            "groups": self.groups,
            "maximumGroupDepth": self.maximum_depth,
            "declaredCharsets": sorted(self.charsets),
            "declaredCodePages": sorted(self.code_pages),
            "unnamedCodePages": self.counts["unnamedCodePages"],
            "hexEscapes": self.counts["hexEscapes"],
            "unicodeEscapes": self.counts["unicodeEscapes"],
            "escapedLiterals": self.counts["escapedLiterals"],
            "controlSymbols": self.counts["controlSymbols"],
            "binarySections": self.counts["binarySections"],
            "binaryBytes": self.counts["binaryBytes"],
            "ignorableDestinationGroups": self.counts["ignorableDestinationGroups"],
            "inventory": dict(sorted(self.inventory.items())),
            # Control words are at most 32 ASCII letters, so a destination name
            # is a safe JSON member name even though the document chose it.
            "unknownDestinations": dict(sorted(self.unknown.items())),
            "ignorableUnknownDestinations": dict(sorted(self.ignorable.items())),
            "unnamedUnknownDestinations": self.counts["unnamedUnknownDestinations"],
            "fieldRisks": dict(sorted(self.field_risks.items())),
            "extractedScope": "pandoc_body_text_only",
            "coverage": {
                "supported": ["body_text"],
                "inventoriedDisabled": sorted(self.inventory),
                "unsupported": sorted(set(self.unknown) | set(self.ignorable)),
            },
            "pathAndUrlHandling": "inventoried_without_resolution",
            "objectAndFieldHandling": "inventoried_without_execution",
            **self.images.to_limitations(),
            "pictureGroups": self.pictures,
        }


def _is_letter(byte: int) -> bool:
    return 0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A


def _hex_escape_end(content: bytes, index: int) -> int:
    """Validate the ``\\'xx`` escape at ``index`` and return the byte after it."""
    if index + 3 > len(content):
        raise RTFExtractionInvalid("RTF hex escape is truncated")
    if not all(byte in _HEX_DIGITS for byte in content[index + 1 : index + 3]):
        raise RTFExtractionInvalid("RTF hex escape is invalid")
    return index + 3


def _skip_fallback(content: bytes, index: int, count: int) -> int:
    """Skip the ``uc`` fallback characters that follow a ``\\uN``.

    A fallback character is a plain byte, a ``\\'xx`` escape or an escaped
    literal. A group boundary or a control word ends the run early, as a reader
    that honoured the Unicode value would also stop there.
    """
    while count > 0 and index < len(content):
        byte = content[index]
        if byte in {0x7B, 0x7D}:
            break
        if byte == 0x5C:
            if index + 1 >= len(content):
                raise RTFExtractionInvalid("RTF ends in an escape")
            following = content[index + 1]
            if following == 0x27:
                index = _hex_escape_end(content, index + 1)
            elif following in b"{}\\":
                index += 2
            else:
                break
        else:
            index += 1
        count -= 1
    return index


def _scan_rtf(content: bytes, *, capture_id: str, images: EmbeddedImagePass) -> _Scan:
    """Read the control stream once and report bounded coverage for it.

    Group count and depth are tracked against the depth bound, the source bound
    is checked first, and the named parts of the result are capped, so neither
    the pass nor its output grows with the document's own content.

    This one pass is also what establishes a picture group's identity: its place
    among its parent's groups, the story it sits in, the byte interval its
    braces delimit, and the payload those braces hold. Each payload is kept only
    for the group it belongs to and is released when the group closes, so at
    most one picture's bytes are held at a time.
    """
    if len(content) > MAX_SOURCE_BYTES:
        raise RTFExtractionInvalid("RTF source exceeds the accepted bound")
    if not content.startswith(b"{\\rtf"):
        raise RTFExtractionInvalid("RTF signature is missing")
    scan = _Scan(images, capture_id)
    # The first entry is the synthetic frame outside the root group, so the
    # current group is always ``stack[-1]`` and depth is ``len(stack) - 1``.
    stack = [_Group(uc=1, ignorable=False, field_prefix=None)]
    index = 0
    while index < len(content):
        group = stack[-1]
        byte = content[index]
        if byte == 0x7B:  # {
            scan.groups += 1
            group.children += 1
            stack.append(
                _Group(
                    uc=group.uc,
                    ignorable=group.ignorable,
                    field_prefix=group.field_prefix,
                    start=index,
                    path=f"{group.path}/{group.children}",
                    story=group.story,
                    disqualified=group.disqualified,
                )
            )
            scan.maximum_depth = max(scan.maximum_depth, len(stack) - 1)
            if scan.maximum_depth > MAX_GROUP_DEPTH:
                raise RTFExtractionInvalid("RTF nesting exceeds the accepted bound")
            index += 1
            continue
        if byte == 0x7D:  # }
            if len(stack) == 1:
                raise RTFExtractionInvalid("RTF groups are unbalanced")
            _close_group(stack.pop(), scan, index + 1)
            stack[-1].text = stack[-1].text or group.text
            index += 1
            continue
        if byte != 0x5C:  # backslash
            # Plain text. CR and LF carry no content in RTF and NUL is padding.
            if byte not in {0x00, 0x0A, 0x0D}:
                group.text = True
                group.expect_word = False
                _keep_field_byte(group, byte)
            if group.picture is not None:
                # Inside a picture group the same bytes are its hex payload.
                _keep_picture_byte(group.picture, byte)
            index += 1
            continue
        index += 1
        if index >= len(content):
            raise RTFExtractionInvalid("RTF ends in an escape")
        byte = content[index]
        if byte in b"{}\\":  # an escaped literal brace or backslash
            scan.counts["escapedLiterals"] += 1
            group.text = True
            group.expect_word = False
            _keep_field_byte(group, byte)
            if group.picture is not None:
                # An escaped delimiter is no hex digit, so a payload holding one
                # is not the payload it claims to be.
                group.picture.malformed = True
            index += 1
            continue
        if byte == 0x27:  # \'xx
            index = _hex_escape_end(content, index)
            scan.counts["hexEscapes"] += 1
            group.text = True
            group.expect_word = False
            if group.picture is not None:
                group.picture.malformed = True
            continue
        if not _is_letter(byte):
            # A control symbol: one byte, no parameter and no delimiter.
            index += 1
            if byte == 0x2A:  # \*
                group.starred = True
            else:
                scan.counts["controlSymbols"] += 1
            continue
        start = index
        while index < len(content) and _is_letter(content[index]):
            index += 1
        if index - start > MAX_CONTROL_WORD_LETTERS:
            raise RTFExtractionInvalid("RTF control word exceeds the accepted bound")
        word = content[start:index].decode("ascii")
        sign = 1
        if index < len(content) and content[index] == 0x2D:
            sign = -1
            index += 1
        digits = index
        while index < len(content) and 0x30 <= content[index] <= 0x39:
            index += 1
        if index - digits > MAX_PARAMETER_DIGITS:
            raise RTFExtractionInvalid(
                "RTF control word parameter exceeds the accepted bound"
            )
        number = sign * int(content[digits:index]) if index > digits else None
        if index < len(content) and content[index] == 0x20:
            index += 1  # the single optional delimiter
        if group.expect_word:
            group.word = word
            group.ignorable = group.ignorable or group.starred
            group.expect_word = False
            destination = _INVENTORIED.get(word)
            if destination is not None:
                scan.inventory[destination] += 1
            if word == "fldinst":
                group.field_prefix = bytearray()
            if word in _STORY_WORDS:
                group.story = _INVENTORIED[word]
            if word in _PICTURE_DISQUALIFIERS:
                group.disqualified = True
            elif word == "pict" and not group.disqualified:
                group.picture = _Picture(
                    group.path, group.story, group.start, scan.images.may_read()
                )
        else:
            marker = _MARKERS.get(word)
            if marker is not None:
                scan.inventory[marker] += 1
            # The first type a group declares is the type it declared; a
            # second is never read as a correction of it.
            if (
                group.picture is not None
                and group.picture.declared is None
                and word in _PICTURE_TYPES
            ):
                group.picture.declared = word
                group.picture.media_type = _PICTURE_TYPES[word]
        index = _apply_control_word(content, index, word, number, group, scan)
    if len(stack) != 1:
        raise RTFExtractionInvalid("RTF groups are unbalanced")
    return scan


def _keep_picture_byte(picture: _Picture, byte: int) -> None:
    """Read one byte of a picture's hex payload, or mark the payload malformed.

    The payload is written as hex digits with whitespace free to appear between
    them. Any other byte means these are not the digits the group claims, which
    is this picture's own fault and never the document's.
    """
    if byte in _PAYLOAD_SPACE:
        return
    if byte not in _HEX_DIGITS:
        picture.malformed = True
        return
    picture.hex_digits += 1
    if picture.keep:
        picture.digits.append(byte)


def _keep_picture_binary(picture: _Picture, payload: bytes) -> None:
    """Take one length-delimited binary payload as this picture's own bytes."""
    picture.binary_bytes += len(payload)
    if picture.keep:
        picture.binary += payload


def _picture_content(picture: _Picture) -> bytes:
    """The payload exactly as the group delimited it.

    A group that wrote both payload forms wrote neither correctly: there is no
    reading that says which of the two is the image, so the payload is
    malformed rather than resolved in favour of one. Checked here, where both
    counts are final, rather than while they are still being read.
    """
    if picture.hex_digits and picture.binary_bytes:
        picture.malformed = True
    elif picture.hex_digits % 2:
        # Half a byte at the end is not a byte; nothing is padded out to make
        # the payload decode.
        picture.malformed = True
    if picture.malformed:
        return b""
    if picture.binary_bytes:
        return bytes(picture.binary)
    return bytes.fromhex(picture.digits.decode("ascii"))


def _close_picture(picture: _Picture, scan: _Scan, end: int) -> None:
    """Settle one picture group: what it declared, and what was read out of it.

    The locator is the group's own: the story it sits in, its stable path among
    its parent's groups, and the truthful byte interval its braces delimit. Two
    groups holding the same bytes stay two occurrences at two intervals, and
    every reading of this one is anchored here.

    A refusal is this picture's outcome and never the source's: the body text
    the reader extracted stands whatever happened here.
    """
    images = scan.images
    # ``groupPath`` and not ``path``: the reader's derived AST path already
    # occupies that name, and these two coordinates are not the same thing.
    place: dict[str, object] = {
        "story": picture.story,
        "groupPath": picture.path,
        "byteStart": picture.start,
        "byteEnd": end,
    }
    locator = {"captureId": scan.capture_id, **place}
    if not picture.hex_digits and not picture.binary_bytes:
        # A picture group that holds a size, a type and no payload embeds no
        # image at all; there is nothing here to open.
        result = images.record(EMBEDDED_NOT_EMBEDDED)
    elif (refused := images.refusal()) is not None:
        # Nothing was examined, so nothing about the payload is claimed: the
        # reason it was not examined is the whole outcome.
        result = refused
    else:
        content = _picture_content(picture)
        result = (
            images.record(EMBEDDED_MALFORMED)
            if picture.malformed
            else images.read(
                content,
                picture.media_type or "",
                # Names where in the source the bytes came from, which is the
                # interval itself: an RTF payload has no part to name.
                f"pict:{picture.start}-{end}",
                locator,
            )
        )
    if len(scan.pictures) < MAX_PICTURE_RECORDS:
        scan.pictures.append(
            {
                "unit": "rtf_picture",
                **place,
                "declaredType": picture.declared,
                "mediaType": picture.media_type,
                "hexDigits": picture.hex_digits,
                "binaryBytes": picture.binary_bytes,
                "imageDecoded": result.decoded,
                "ocrPerformed": result.performed,
                "ocrOutcome": result.outcome,
            }
        )
    scan.segments.extend(result.segments)


def _keep_field_byte(group: _Group, byte: int) -> None:
    """Keep the leading keyword of a field instruction, and nothing after it."""
    prefix = group.field_prefix
    if prefix is None or len(prefix) >= MAX_FIELD_PREFIX_BYTES:
        return
    if byte in {0x09, 0x0C, 0x20}:
        # The keyword has ended. The single trailing space closes the prefix, so
        # the operand that follows - a path, a URL, a command - is never read
        # out of the source, not merely withheld from the result.
        if prefix and not prefix.endswith(b" "):
            prefix.append(0x20)
    elif not prefix.endswith(b" "):
        prefix.append(byte)


def _apply_control_word(
    content: bytes,
    index: int,
    word: str,
    number: int | None,
    group: _Group,
    scan: _Scan,
) -> int:
    """Apply the control words that change the scan itself; return the new index."""
    if word == "bin":
        # A binary section must declare its own non-negative length. Without one
        # the payload has no end, and its braces and backslashes would be read
        # as structure, so an absent or negative length is malformed.
        if number is None or number < 0 or index + number > len(content):
            raise RTFExtractionInvalid("RTF binary section is malformed")
        scan.counts["binarySections"] += 1
        scan.counts["binaryBytes"] += number
        if group.picture is not None:
            _keep_picture_binary(group.picture, content[index : index + number])
        # The payload is skipped as a block, so braces and backslashes inside
        # it are data and never read as structure.
        return index + number
    if word == "uc":
        group.uc = max(0, number or 0)
    elif word == "u":
        if number is None:
            raise RTFExtractionInvalid("RTF Unicode control word has no code point")
        scan.counts["unicodeEscapes"] += 1
        group.text = True
        return _skip_fallback(content, index, group.uc)
    elif word in {"ansicpg", "cpg"} and number is not None:
        # A code page is declared per document and per font, so the number of
        # distinct values a source can declare grows with the source itself.
        if len(scan.code_pages) < MAX_INVENTORY_NAMES or number in scan.code_pages:
            scan.code_pages.add(number)
        else:
            scan.counts["unnamedCodePages"] += 1
    elif word in _CHARSETS:
        scan.charsets.add(word)
    elif word == "itap" and number:
        scan.inventory["nested_table"] += 1
    return index


def _close_group(group: _Group, scan: _Scan, end: int) -> None:
    """Record what a closing group contributes to the inventory."""
    if group.picture is not None:
        _close_picture(group.picture, scan, end)
    if group.word == "fldinst" and group.field_prefix is not None:
        # Only the leading keyword is classified; the operand path or URL is
        # never read out of the bytes, let alone resolved.
        tokens = bytes(group.field_prefix).split()
        keyword = tokens[0].decode("ascii", "replace").upper() if tokens else ""
        scan.field_risks[keyword if keyword in _FIELD_RISKS else "other"] += 1
    if group.ignorable:
        scan.counts["ignorableDestinationGroups"] += 1
    if group.word is None or not group.text:
        return
    if (
        group.word in _INVENTORIED
        or group.word in _MARKERS
        or group.word in _KNOWN_WORDS
    ):
        return
    names = scan.ignorable if group.ignorable else scan.unknown
    if len(names) < MAX_INVENTORY_NAMES or group.word in names:
        names[group.word] += 1
    else:
        scan.counts["unnamedUnknownDestinations"] += 1


def _malformed() -> RTFExtractionInvalid:
    return RTFExtractionInvalid("RTF reader returned a malformed document structure")


def _pair(value: Any, size: int) -> list[Any]:
    if not isinstance(value, list) or len(value) != size:
        raise _malformed()
    return value


def _scheme(target: str) -> str:
    """Name the reference kind of a link target from its text alone."""
    if target.startswith("#"):
        return "fragment"
    match = _SCHEME.match(target)
    return match.group(1).lower() if match else "relative"


class _AstWalker:
    """Map one Pandoc JSON AST onto canonical segments, strictly and in order.

    Every node is validated against the Pandoc 1.23 shape it must have, and an
    unknown or misshapen node refuses the whole document rather than skipping
    it, so a partial walk is never reported as complete. A link or image target
    is recorded as text in an inventory segment and is never opened.
    """

    def __init__(self, capture_id: str) -> None:
        self.capture_id = capture_id
        self.segments: list[ExtractedSegment] = []
        self.counts: Counter[str] = Counter()
        self.schemes: Counter[str] = Counter()
        self.nodes = 0

    def limitations(self) -> dict[str, object]:
        return {
            "astInventory": dict(sorted(self.counts.items())),
            "referenceSchemes": dict(sorted(self.schemes.items())),
        }

    def _node(self, node: Any, depth: int) -> tuple[str, Any]:
        self.nodes += 1
        if self.nodes > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise RTFExtractionInvalid("RTF structure exceeds the accepted bound")
        if not isinstance(node, dict) or not isinstance(node.get("t"), str):
            raise _malformed()
        return node["t"], node.get("c")

    def _emit(self, segment: ExtractedSegment) -> None:
        self.segments.append(segment)
        if len(self.segments) > MAX_BLOCKS:
            raise RTFExtractionInvalid("RTF extraction has too many segments")

    def _locator(self, path: str) -> dict[str, str]:
        return {"captureId": self.capture_id, "unit": "rtf_ast", "path": path}

    def blocks(self, blocks: Any, path: str, role: str, depth: int) -> None:
        if not isinstance(blocks, list):
            raise _malformed()
        for ordinal, block in enumerate(blocks, start=1):
            kind, content = self._node(block, depth)
            self._block(kind, content, f"{path}/{ordinal}:{kind}", role, depth)

    def _block(self, kind: str, content: Any, path: str, role: str, depth: int) -> None:
        if kind in {"Para", "Plain"}:
            self._text(role, content, path, depth)
        elif kind == "Header":
            self._text("heading", _pair(content, 3)[2], path, depth)
        elif kind == "CodeBlock":
            code = _pair(content, 2)[1]
            if not isinstance(code, str):
                raise _malformed()
            self._text(role, [{"t": "Str", "c": code}], path, depth)
        elif kind == "LineBlock":
            if not isinstance(content, list):
                raise _malformed()
            lines: list[Any] = []
            for line in content:
                lines += [
                    *(line if isinstance(line, list) else [None]),
                    {"t": "LineBreak"},
                ]
            self._text(role, lines[:-1], path, depth)
        elif kind == "BulletList":
            self._items(content, path, depth)
        elif kind == "OrderedList":
            self._items(_pair(content, 2)[1], path, depth)
        elif kind == "DefinitionList":
            if not isinstance(content, list):
                raise _malformed()
            for number, entry in enumerate(content, start=1):
                term, definitions = _pair(entry, 2)
                self._text("list_item", term, f"{path}/term/{number}", depth + 1)
                if not isinstance(definitions, list):
                    raise _malformed()
                for ordinal, definition in enumerate(definitions, start=1):
                    self.blocks(
                        definition,
                        f"{path}/def/{number}.{ordinal}",
                        "list_item",
                        depth + 1,
                    )
        elif kind in {"BlockQuote", "Div"}:
            self.blocks(
                content if kind == "BlockQuote" else _pair(content, 2)[1],
                path,
                role,
                depth + 1,
            )
        elif kind == "Figure":
            _, caption, body = _pair(content, 3)
            self.blocks(_pair(caption, 2)[1], f"{path}/caption", "body", depth + 1)
            self.blocks(body, path, role, depth + 1)
        elif kind == "Table":
            self._table(content, path, depth)
        elif kind == "RawBlock":
            _pair(content, 2)
            self.counts["rawBlocks"] += 1
        elif kind == "HorizontalRule":
            pass
        else:
            raise _malformed()

    def _items(self, items: Any, path: str, depth: int) -> None:
        if not isinstance(items, list):
            raise _malformed()
        self.counts["lists"] += 1
        for ordinal, item in enumerate(items, start=1):
            self.blocks(item, f"{path}/item/{ordinal}", "list_item", depth + 1)

    def _table(self, content: Any, path: str, depth: int) -> None:
        _, caption, specs, head, bodies, foot = _pair(content, 6)
        if not isinstance(specs, list) or not isinstance(bodies, list):
            raise _malformed()
        self.counts["tables"] += 1
        self.blocks(_pair(caption, 2)[1], f"{path}/caption", "body", depth + 1)
        self._rows(_pair(head, 2)[1], f"{path}/head", depth)
        for number, body in enumerate(bodies, start=1):
            _, _, head_rows, body_rows = _pair(body, 4)
            self._rows(head_rows, f"{path}/body/{number}/head", depth)
            self._rows(body_rows, f"{path}/body/{number}", depth)
        self._rows(_pair(foot, 2)[1], f"{path}/foot", depth)

    def _rows(self, rows: Any, path: str, depth: int) -> None:
        if not isinstance(rows, list):
            raise _malformed()
        for row_number, row in enumerate(rows, start=1):
            cells = _pair(row, 2)[1]
            if not isinstance(cells, list):
                raise _malformed()
            for cell_number, cell in enumerate(cells, start=1):
                body = _pair(cell, 5)[4]
                cell_path = f"{path}/row/{row_number}/cell/{cell_number}"
                self.blocks(body, cell_path, "table_cell", depth + 1)

    def _text(self, role: str, inline: Any, path: str, depth: int) -> None:
        """Emit one text segment, then the side material found inside it."""
        side: list[tuple[str, Any]] = []
        text = self._inline(inline, depth, side).strip()
        if text:
            self._emit_text(role, text, path)
        ordinals: Counter[str] = Counter()
        for kind, payload in side:
            ordinals[kind] += 1
            self._side(kind, payload, f"{path}/{kind}/{ordinals[kind]}", depth + 1)

    def _emit_text(self, role: str, text: str, path: str) -> None:
        deleted = role == "deleted_text"
        self._emit(
            ExtractedSegment(
                role,
                self._locator(path),
                text=text,
                visibility="annotation" if role in _ANNOTATION_ROLES else "visible",
                revision_state="deleted" if deleted else "current",
            )
        )

    def _side(self, kind: str, payload: Any, path: str, depth: int) -> None:
        if kind == "note":
            self.counts["notes"] += 1
            self.blocks(payload, path, "note", depth)
        elif kind in {"deletion", "comment"}:
            self._text(
                "deleted_text" if kind == "deletion" else "comment",
                payload,
                path,
                depth,
            )
        else:  # link or image: the target is inert inventory, never dereferenced
            target, title = payload
            scheme = _scheme(target)
            self.schemes[
                scheme
                if len(self.schemes) < MAX_INVENTORY_NAMES or scheme in self.schemes
                else "other"
            ] += 1
            self._emit(
                ExtractedSegment(
                    "object_inventory",
                    self._locator(path),
                    structured_value={
                        "kind": kind,
                        "scheme": scheme,
                        "target": target[:MAX_TARGET_CHARS],
                        "targetTruncated": len(target) > MAX_TARGET_CHARS,
                        "title": title[:MAX_TARGET_CHARS],
                        "resolved": False,
                    },
                    extraction_method="inventory",
                    visibility="annotation",
                )
            )

    def _inline(self, inline: Any, depth: int, side: list[tuple[str, Any]]) -> str:
        if not isinstance(inline, list):
            raise _malformed()
        chunks: list[str] = []
        for item in inline:
            if item is None:  # a LineBlock line placeholder for an empty line
                continue
            kind, content = self._node(item, depth)
            if kind == "Str":
                if not isinstance(content, str):
                    raise _malformed()
                chunks.append(content)
            elif kind == "Space":
                chunks.append(" ")
            elif kind in {"SoftBreak", "LineBreak"}:
                chunks.append("\n")
            elif kind in {"Code", "Math"}:
                value = _pair(content, 2)[1]
                if not isinstance(value, str):
                    raise _malformed()
                chunks.append(value)
            elif kind == "RawInline":
                _pair(content, 2)
                self.counts["rawInlines"] += 1
            elif kind in _FORMATTING:
                chunks.append(self._inline(content, depth + 1, side))
            elif kind in {"Quoted", "Cite"}:
                inner = self._inline(_pair(content, 2)[1], depth + 1, side)
                if kind == "Cite":
                    chunks.append(inner)
                else:
                    quote = _pair(content, 2)[0]
                    if not isinstance(quote, dict):
                        raise _malformed()
                    marks = "“”" if quote.get("t") == "DoubleQuote" else "‘’"
                    chunks.append(f"{marks[0]}{inner}{marks[1]}")
            elif kind in {"Link", "Image"}:
                _, label, destination = _pair(content, 3)
                target, title = _pair(destination, 2)
                if not isinstance(target, str) or not isinstance(title, str):
                    raise _malformed()
                chunks.append(self._inline(label, depth + 1, side))
                side.append((kind.lower(), (target, title)))
                self.counts[kind.lower() + "s"] += 1
            elif kind == "Note":
                side.append(("note", content))
            elif kind == "Span":
                attributes, inner = _pair(content, 2)
                classes = _pair(attributes, 3)[1]
                if not isinstance(classes, list):
                    raise _malformed()
                if "deletion" in classes:
                    side.append(("deletion", inner))
                    self.counts["deletionSpans"] += 1
                elif "comment-start" in classes:
                    side.append(("comment", inner))
                    self.counts["commentSpans"] += 1
                elif "comment-end" not in classes:
                    chunks.append(self._inline(inner, depth + 1, side))
            else:
                raise _malformed()
        return "".join(chunks)
