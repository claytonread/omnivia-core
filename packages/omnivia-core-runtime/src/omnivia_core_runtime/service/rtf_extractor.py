"""Bounded RTF lexical scan plus fixed-argument Pandoc JSON extraction.

The scanner reads the control stream once and reports what the source carries.
It never decodes text, never accumulates document content beyond one bounded
field-keyword prefix, never resolves a path or a URL, and never executes a field
or an embedded object: an external reference is counted by kind and the path or
URL itself is not even read out of the bytes.

The pinned reader extracts body text only, so the scan classifies every other
feature it finds as inventoried-but-disabled (recognised, deliberately not
extracted) or unsupported/unknown. Nothing here claims complete extraction.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

from omnivia_core_runtime.service.document_intake import ExtractedSegment

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
PANDOC_BUILD: Final = "pandoc-3.12.1-rtf-json"

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


@dataclass(frozen=True, slots=True)
class RTFExtraction:
    segments: tuple[ExtractedSegment, ...]
    blocks: int
    limitations: dict[str, object]


def extract_rtf(content: bytes, *, capture_id: str) -> RTFExtraction:
    scan = _scan_rtf(content)
    try:
        completed = subprocess.run(
            ["pandoc", "--from=rtf", "--to=json", "--sandbox"],
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
    segments: list[ExtractedSegment] = []
    _walk_blocks(blocks, segments, capture_id=capture_id, path="blocks")
    return RTFExtraction(
        tuple(segments),
        len(blocks),
        {
            **scan,
            "readerBuild": PANDOC_BUILD,
            "astLocatorsAreDerived": True,
        },
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


@dataclass(slots=True)
class _Scan:
    counts: Counter[str] = field(default_factory=Counter)
    inventory: Counter[str] = field(default_factory=Counter)
    unknown: Counter[str] = field(default_factory=Counter)
    ignorable: Counter[str] = field(default_factory=Counter)
    field_risks: Counter[str] = field(default_factory=Counter)
    charsets: set[str] = field(default_factory=set)
    code_pages: set[int] = field(default_factory=set)


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


def _scan_rtf(content: bytes) -> dict[str, object]:
    """Read the control stream once and report bounded coverage for it.

    Group count and depth are tracked against the depth bound, the source bound
    is checked first, and the named parts of the result are capped, so neither
    the pass nor its output grows with the document's own content.
    """
    if len(content) > MAX_SOURCE_BYTES:
        raise RTFExtractionInvalid("RTF source exceeds the accepted bound")
    if not content.startswith(b"{\\rtf"):
        raise RTFExtractionInvalid("RTF signature is missing")
    scan = _Scan()
    # The first entry is the synthetic frame outside the root group, so the
    # current group is always ``stack[-1]`` and depth is ``len(stack) - 1``.
    stack = [_Group(uc=1, ignorable=False, field_prefix=None)]
    groups = 0
    maximum_depth = 0
    index = 0
    while index < len(content):
        group = stack[-1]
        byte = content[index]
        if byte == 0x7B:  # {
            groups += 1
            stack.append(
                _Group(
                    uc=group.uc,
                    ignorable=group.ignorable,
                    field_prefix=group.field_prefix,
                )
            )
            maximum_depth = max(maximum_depth, len(stack) - 1)
            if maximum_depth > MAX_GROUP_DEPTH:
                raise RTFExtractionInvalid("RTF nesting exceeds the accepted bound")
            index += 1
            continue
        if byte == 0x7D:  # }
            if len(stack) == 1:
                raise RTFExtractionInvalid("RTF groups are unbalanced")
            _close_group(stack.pop(), scan)
            stack[-1].text = stack[-1].text or group.text
            index += 1
            continue
        if byte != 0x5C:  # backslash
            # Plain text. CR and LF carry no content in RTF and NUL is padding.
            if byte not in {0x00, 0x0A, 0x0D}:
                group.text = True
                group.expect_word = False
                _keep_field_byte(group, byte)
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
            index += 1
            continue
        if byte == 0x27:  # \'xx
            index = _hex_escape_end(content, index)
            scan.counts["hexEscapes"] += 1
            group.text = True
            group.expect_word = False
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
        else:
            marker = _MARKERS.get(word)
            if marker is not None:
                scan.inventory[marker] += 1
        index = _apply_control_word(content, index, word, number, group, scan)
    if len(stack) != 1:
        raise RTFExtractionInvalid("RTF groups are unbalanced")
    return {
        "groups": groups,
        "maximumGroupDepth": maximum_depth,
        "declaredCharsets": sorted(scan.charsets),
        "declaredCodePages": sorted(scan.code_pages),
        "unnamedCodePages": scan.counts["unnamedCodePages"],
        "hexEscapes": scan.counts["hexEscapes"],
        "unicodeEscapes": scan.counts["unicodeEscapes"],
        "escapedLiterals": scan.counts["escapedLiterals"],
        "controlSymbols": scan.counts["controlSymbols"],
        "binarySections": scan.counts["binarySections"],
        "binaryBytes": scan.counts["binaryBytes"],
        "ignorableDestinationGroups": scan.counts["ignorableDestinationGroups"],
        "inventory": dict(sorted(scan.inventory.items())),
        # Control words are at most 32 ASCII letters, so a destination name is
        # a safe JSON member name even though the document chose it.
        "unknownDestinations": dict(sorted(scan.unknown.items())),
        "ignorableUnknownDestinations": dict(sorted(scan.ignorable.items())),
        "unnamedUnknownDestinations": scan.counts["unnamedUnknownDestinations"],
        "fieldRisks": dict(sorted(scan.field_risks.items())),
        "extractedScope": "pandoc_body_text_only",
        "coverage": {
            "supported": ["body_text"],
            "inventoriedDisabled": sorted(scan.inventory),
            "unsupported": sorted(set(scan.unknown) | set(scan.ignorable)),
        },
        "pathAndUrlHandling": "inventoried_without_resolution",
        "objectAndFieldHandling": "inventoried_without_execution",
    }


def _keep_field_byte(group: _Group, byte: int) -> None:
    """Keep the leading keyword of a field instruction, and nothing after it."""
    prefix = group.field_prefix
    if prefix is None or len(prefix) >= MAX_FIELD_PREFIX_BYTES:
        return
    if byte in {0x09, 0x0C, 0x20}:
        # The keyword has ended. The single trailing space closes the prefix, so
        # the operand that follows - a path, a URL, a command - is never read
        # out of the source, not merely withheld from the result.
        if prefix:
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


def _close_group(group: _Group, scan: _Scan) -> None:
    """Record what a closing group contributes to the inventory."""
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


def _walk_blocks(
    blocks: list[Any],
    segments: list[ExtractedSegment],
    *,
    capture_id: str,
    path: str,
    role: str = "body",
) -> None:
    for ordinal, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            continue
        block_type = block.get("t")
        content = block.get("c")
        block_path = f"{path}/{ordinal}:{block_type}"
        if block_type in {"Para", "Plain"} and isinstance(content, list):
            _append_text(
                segments, role, content, capture_id=capture_id, path=block_path
            )
        elif block_type == "Header" and isinstance(content, list) and len(content) == 3:
            inline = content[2]
            if isinstance(inline, list):
                _append_text(
                    segments, "heading", inline, capture_id=capture_id, path=block_path
                )
        elif block_type in {"BulletList", "OrderedList"}:
            items = (
                content
                if block_type == "BulletList"
                else content[1]
                if isinstance(content, list) and len(content) == 2
                else None
            )
            if isinstance(items, list):
                for item_ordinal, item in enumerate(items, start=1):
                    if isinstance(item, list):
                        _walk_blocks(
                            item,
                            segments,
                            capture_id=capture_id,
                            path=f"{block_path}/item/{item_ordinal}",
                            role="list_item",
                        )
        elif block_type == "BlockQuote" and isinstance(content, list):
            _walk_blocks(
                content, segments, capture_id=capture_id, path=block_path, role=role
            )
        if len(segments) > MAX_BLOCKS:
            raise RTFExtractionInvalid("RTF extraction has too many segments")


def _append_text(
    segments: list[ExtractedSegment],
    role: str,
    inline: list[Any],
    *,
    capture_id: str,
    path: str,
) -> None:
    text = _inline_text(inline).strip()
    if text:
        segments.append(
            ExtractedSegment(
                role,
                {"captureId": capture_id, "unit": "rtf_ast", "path": path},
                text=text,
            )
        )


def _inline_text(inline: list[Any]) -> str:
    chunks: list[str] = []
    for item in inline:
        if not isinstance(item, dict):
            continue
        item_type = item.get("t")
        content = item.get("c")
        if item_type == "Str" and isinstance(content, str):
            chunks.append(content)
        elif item_type in {"Space", "SoftBreak", "LineBreak"}:
            chunks.append(" " if item_type == "Space" else "\n")
        elif (
            item_type in {"Code", "Math"}
            and isinstance(content, list)
            and len(content) == 2
        ):
            chunks.append(str(content[1]))
        elif item_type in {
            "Emph",
            "Strong",
            "Strikeout",
            "Superscript",
            "Subscript",
            "SmallCaps",
        } and isinstance(content, list):
            chunks.append(_inline_text(content))
        elif item_type == "Link" and isinstance(content, list) and len(content) >= 2:
            label = content[1]
            if isinstance(label, list):
                chunks.append(_inline_text(label))
    return "".join(chunks)
