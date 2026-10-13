"""Bounded RTF scanner plus fixed-argument Pandoc JSON extraction."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_runtime.service.document_intake import ExtractedSegment

MAX_SOURCE_BYTES: Final = 20 * 1024 * 1024
MAX_AST_BYTES: Final = 128 * 1024 * 1024
MAX_GROUP_DEPTH: Final = 128
MAX_BLOCKS: Final = 100_000
PANDOC_BUILD: Final = "pandoc-3.12.1-rtf-json"


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


def _scan_rtf(content: bytes) -> dict[str, object]:
    if len(content) > MAX_SOURCE_BYTES:
        raise RTFExtractionInvalid("RTF source exceeds the accepted bound")
    if not content.startswith(b"{\\rtf"):
        raise RTFExtractionInvalid("RTF signature is missing")
    depth = 0
    maximum_depth = 0
    groups = 0
    binary_sections = 0
    destinations: dict[str, int] = {}
    index = 0
    while index < len(content):
        byte = content[index]
        if byte == 0x7B:  # {
            depth += 1
            groups += 1
            maximum_depth = max(maximum_depth, depth)
            if maximum_depth > MAX_GROUP_DEPTH:
                raise RTFExtractionInvalid("RTF nesting exceeds the accepted bound")
            index += 1
            continue
        if byte == 0x7D:  # }
            depth -= 1
            if depth < 0:
                raise RTFExtractionInvalid("RTF groups are unbalanced")
            index += 1
            continue
        if byte != 0x5C:  # backslash
            index += 1
            continue
        index += 1
        if index >= len(content):
            raise RTFExtractionInvalid("RTF ends in an escape")
        if content[index] in b"{}\\":
            index += 1
            continue
        if content[index] == 0x27:  # hex escape
            if index + 2 >= len(content):
                raise RTFExtractionInvalid("RTF hex escape is truncated")
            try:
                int(content[index + 1 : index + 3], 16)
            except ValueError as error:
                raise RTFExtractionInvalid("RTF hex escape is invalid") from error
            index += 3
            continue
        start = index
        while index < len(content) and 0x41 <= (content[index] | 0x20) <= 0x7A:
            index += 1
        word = content[start:index].decode("ascii", errors="strict")
        sign = 1
        if index < len(content) and content[index] == 0x2D:
            sign = -1
            index += 1
        number_start = index
        while index < len(content) and 0x30 <= content[index] <= 0x39:
            index += 1
        number = None
        if index > number_start:
            number = sign * int(content[number_start:index])
        if index < len(content) and content[index] == 0x20:
            index += 1
        if word == "bin":
            if number is None or number < 0 or index + number > len(content):
                raise RTFExtractionInvalid("RTF binary section is malformed")
            binary_sections += 1
            index += number
        if word in {
            "annotation",
            "comment",
            "footer",
            "footnote",
            "header",
            "object",
            "pict",
            "revised",
            "deleted",
        }:
            destinations[word] = destinations.get(word, 0) + 1
    if depth != 0:
        raise RTFExtractionInvalid("RTF groups are unbalanced")
    return {
        "groups": groups,
        "maximumGroupDepth": maximum_depth,
        "binarySections": binary_sections,
        "inventoriedDestinations": destinations,
    }


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
