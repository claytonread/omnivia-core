"""The DOCX semantic walker over minimal in-memory packages."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from _trusted_ocr import catalogue_over, stand_in_models
from omnivia_core_runtime.service import ocr_resources, ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import (
    _MAX_DOCX_EQUATION_BYTES,
    _MAX_DOCX_FIELD_DEPTH,
    _MAX_DOCX_TABLE_COLUMNS,
    _MAX_DOCX_TABLE_ROWS,
    _MAX_META_CHARS,
    _MAX_TARGET_CHARS,
    MAX_SEGMENTS,
    OOXMLExtraction,
    OOXMLInvalid,
    _FieldState,
    extract_docx,
)
from omnivia_core_runtime.service.raster_extractors import (
    EMBEDDED_DISABLED,
    EMBEDDED_MALFORMED,
    EMBEDDED_NO_TEXT,
    EMBEDDED_NOT_EMBEDDED,
    EMBEDDED_RECOGNISED,
    EMBEDDED_RESOURCES_UNAVAILABLE,
    EMBEDDED_TRUNCATED,
    EMBEDDED_UNSUPPORTED,
    GEOMETRY_SPACE,
    ExtractionBudget,
)
from PIL import Image, ImageDraw

CAPTURE = "dcap-" + "a" * 64
W = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
STYLES_REL = (
    b'<Relationships><Relationship Id="rId1" Target="styles.xml" '
    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"/>'
    b"</Relationships>"
)
STYLES = (
    f"<w:styles {W}>"
    '<w:style w:styleId="Head"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:styleId="Sub"><w:basedOn w:val="Head"/></w:style>'
    '<w:style w:styleId="Outlined"><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
    '<w:style w:styleId="Bullet"><w:pPr><w:numPr><w:numId w:val="3"/></w:numPr></w:pPr></w:style>'
    "</w:styles>"
).encode()


HEAD_STYLE = '<w:pStyle w:val="Head"/>'


def p(text: str, ppr: str = "") -> str:
    return f"<w:p>{f'<w:pPr>{ppr}</w:pPr>' if ppr else ''}<w:r><w:t>{text}</w:t></w:r></w:p>"


def docx(
    body: str,
    parts: dict[str, str] | None = None,
    *,
    styles: bool = True,
    content_types: str = "",
    members: dict[str, bytes] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f"{content_types}</Types>",
        )
        archive.writestr(
            "word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>"
        )
        if styles:
            archive.writestr("word/_rels/document.xml.rels", STYLES_REL)
            archive.writestr("word/styles.xml", STYLES)
        for name, content in (parts or {}).items():
            archive.writestr(name, content)
        for name, raw in (members or {}).items():
            archive.writestr(name, raw)
    return buffer.getvalue()


def read(content: bytes) -> list[tuple[str, str, object]]:
    return [
        (item.semantic_role, item.text or "", item.source_locator["position"])
        for item in extract_docx(content, capture_id=CAPTURE).segments
    ]


def test_paragraphs_and_tables_follow_document_order_without_double_counting() -> None:
    body = (
        p("before")
        + "<w:tbl><w:tr><w:tc>"
        + p("a1")
        + p("a2")
        + "</w:tc><w:tc>"
        + p("b")
        + "<w:tbl><w:tr><w:tc>"
        + p("inner")
        + "</w:tc></w:tr></w:tbl>"
        + "</w:tc></w:tr></w:tbl>"
        + p("after")
    )

    result = extract_docx(docx(body), capture_id=CAPTURE)

    assert [(item.semantic_role, item.text) for item in result.segments] == [
        ("body", "before"),
        ("table_cell", "a1\na2"),
        ("table_cell", "b"),
        ("table_cell", "inner"),
        ("body", "after"),
    ]
    assert result.expected == 6
    assert [item.source_locator["position"] for item in result.segments] == [
        "0",
        "1.r0.c0",
        "1.r0.c1",
        "1.r0.c1.1.r0.c0",
        "2",
    ]


def test_headings_and_list_items_are_classified_from_styles_and_numbering() -> None:
    body = (
        p("Direct", '<w:pStyle w:val="Head"/>')
        + p("Inherited", '<w:pStyle w:val="Sub"/>')
        + p("Outline style", '<w:pStyle w:val="Outlined"/>')
        + p("Outline direct", '<w:outlineLvl w:val="0"/>')
        + p("Title", '<w:pStyle w:val="Title"/>')
        + p("Numbered", '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        + p("Style list", '<w:pStyle w:val="Bullet"/>')
        + p("Not a list", '<w:numPr><w:numId w:val="0"/></w:numPr>')
        + p("Body text", '<w:outlineLvl w:val="9"/>')
        + p(
            "Numbered heading",
            '<w:pStyle w:val="Head"/><w:numPr><w:numId w:val="1"/></w:numPr>',
        )
    )

    assert [(role, text) for role, text, _ in read(docx(body))] == [
        ("heading", "Direct"),
        ("heading", "Inherited"),
        ("heading", "Outline style"),
        ("heading", "Outline direct"),
        ("heading", "Title"),
        ("list_item", "Numbered"),
        ("list_item", "Style list"),
        ("body", "Not a list"),
        ("body", "Body text"),
        ("heading", "Numbered heading"),
    ]


def test_heading_style_ids_classify_without_a_styles_part() -> None:
    body = p("Plain heading", '<w:pStyle w:val="Heading2"/>') + p("Body")

    assert [role for role, _, _ in read(docx(body, styles=False))] == [
        "heading",
        "body",
    ]


def test_tabs_and_breaks_stay_text_separators() -> None:
    body = (
        "<w:p><w:r><w:t>one</w:t><w:tab/><w:t>two</w:t><w:br/><w:t>three</w:t></w:r>"
        "<w:r><w:cr/><w:t>four</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        "<w:r><w:t>x</w:t></w:r></w:p>"
    )

    assert [text for _, text, _ in read(docx(body))] == ["one\ttwo\nthree\nfour", "x"]


def test_repeat_extraction_is_identical_and_locators_are_unique() -> None:
    body = p("a") + "<w:tbl><w:tr><w:tc>" + p("b") + "</w:tc></w:tr></w:tbl>" + p("c")
    content = docx(body)

    first = extract_docx(content, capture_id=CAPTURE)
    second = extract_docx(content, capture_id=CAPTURE)

    assert first == second
    locators = [tuple(sorted(item.source_locator.items())) for item in first.segments]
    assert len(set(locators)) == len(locators)
    assert all(item.source_locator["captureId"] == CAPTURE for item in first.segments)
    assert {item.source_locator["part"] for item in first.segments} == {
        "word/document.xml"
    }


def test_annotation_stories_keep_their_roles_and_note_identity() -> None:
    parts = {
        "word/footnotes.xml": f'<w:footnotes {W}><w:footnote w:id="2">{p("Foot")}</w:footnote></w:footnotes>',
        "word/endnotes.xml": f'<w:endnotes {W}><w:endnote w:id="5">{p("End")}</w:endnote></w:endnotes>',
        "word/comments.xml": f'<w:comments {W}><w:comment w:id="9">{p("Said")}</w:comment></w:comments>',
    }

    result = extract_docx(docx(p("Body"), parts), capture_id=CAPTURE)

    assert [
        (
            item.semantic_role,
            item.text,
            item.visibility,
            item.source_locator.get("noteId"),
        )
        for item in result.segments
    ] == [
        ("body", "Body", "visible", None),
        ("comment", "Said", "annotation", "9"),
        ("note", "End", "annotation", "5"),
        ("note", "Foot", "annotation", "2"),
    ]


def run(text: str, tag: str = "t") -> str:
    return f"<w:r><w:{tag}>{text}</w:{tag}></w:r>"


def box(*paragraphs: str, fallback: bool = True) -> str:
    content = "".join(paragraphs)
    return (
        "<w:r><mc:AlternateContent><mc:Choice><w:drawing><wps:txbx><w:txbxContent>"
        f"{content}</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
        + (
            "<mc:Fallback><w:pict><v:textbox><w:txbxContent>"
            f"{content}</w:txbxContent></v:textbox></w:pict></mc:Fallback>"
            if fallback
            else ""
        )
        + "</mc:AlternateContent></w:r>"
    )


def revisions(content: bytes) -> list[tuple[str, str, str, str, object]]:
    return [
        (
            item.semantic_role,
            item.text or "",
            item.visibility,
            item.revision_state,
            item.source_locator["position"],
        )
        for item in extract_docx(content, capture_id=CAPTURE).segments
    ]


def test_revisions_split_inserted_prose_from_current_and_deleted_text() -> None:
    body = (
        "<w:p>"
        + run("Keep ")
        + f"<w:ins>{run('added ')}</w:ins>"
        + f"<w:moveTo>{run('moved in ')}</w:moveTo>"
        + f"<w:del>{run('gone', 'delText')}</w:del>"
        + f"<w:moveFrom>{run('moved out')}</w:moveFrom>"
        + run("tail")
        + f"<w:del><w:r><w:rPr><w:del/></w:rPr>{run('late', 'delText')[5:]}</w:del>"
        + "</w:p>"
        + p("kept")
    )

    # ``w:ins`` and ``w:moveTo`` are read as the page's own prose, so they keep
    # the paragraph's role and stay visible; only ``revision_state`` says the
    # author added them, and the boundary splits them off the text either side.
    assert revisions(docx(body)) == [
        ("body", "Keep ", "visible", "current", "0"),
        ("body", "added moved in ", "visible", "inserted", "0.t1"),
        ("deleted_text", "gonemoved out", "annotation", "deleted", "0.d0"),
        ("body", "tail", "visible", "current", "0.t2"),
        ("deleted_text", "late", "annotation", "deleted", "0.d1"),
        ("body", "kept", "visible", "current", "1"),
    ]


def test_deleted_stretches_split_around_current_text_and_keep_the_role() -> None:
    body = (
        f"<w:p><w:pPr>{HEAD_STYLE}</w:pPr>"
        f"<w:del>{run('a', 'delText')}</w:del>{run('Title')}"
        f"<w:del>{run('b', 'delText')}</w:del>{run('!')}"
        f"<w:del>{run('c', 'delText')}</w:del></w:p>"
        "<w:tbl><w:tr><w:tc>"
        f"<w:p>{run('cell ')}<w:del>{run('x', 'delText')}</w:del></w:p>"
        f"<w:p><w:del>{run('whole', 'delText')}</w:del></w:p>"
        "</w:tc></w:tr></w:tbl>"
    )

    assert revisions(docx(body)) == [
        ("deleted_text", "a", "annotation", "deleted", "0.d0"),
        ("heading", "Title", "visible", "current", "0"),
        ("deleted_text", "b", "annotation", "deleted", "0.d1"),
        ("heading", "!", "visible", "current", "0.t1"),
        ("deleted_text", "c", "annotation", "deleted", "0.d2"),
        ("table_cell", "cell ", "visible", "current", "1.r0.c0"),
        ("deleted_text", "x", "annotation", "deleted", "1.r0.c0.0.d0"),
        ("deleted_text", "whole", "annotation", "deleted", "1.r0.c0.1.d0"),
    ]


def test_insertions_later_deleted_stay_deleted_and_contradictions_are_refused() -> None:
    inserted_then_deleted = (
        f"<w:p><w:ins><w:del>{run('x', 'delText')}</w:del></w:ins></w:p>"
    )
    assert revisions(docx(inserted_then_deleted)) == [
        ("deleted_text", "x", "annotation", "deleted", "0.d0")
    ]

    with pytest.raises(OOXMLInvalid, match="contradict"):
        extract_docx(
            docx(f"<w:p><w:del><w:ins>{run('x')}</w:ins></w:del></w:p>"),
            capture_id=CAPTURE,
        )

    deep = run("x")
    for _ in range(5):
        deep = f"<w:ins>{deep}</w:ins>"
    with pytest.raises(OOXMLInvalid, match="nested too deeply"):
        extract_docx(docx(f"<w:p>{deep}</w:p>"), capture_id=CAPTURE)


def test_an_insertion_splits_a_cell_run_and_rides_over_what_stands_beside_it() -> None:
    """A cell joins the prose of one revision state only, so an insertion closes
    the unrevised run rather than being joined into one stretch the cell never
    held, and keeps joining across the paragraphs the insertion covers. The
    items ordered beside that prose -- an equation, a nested text box -- take the
    same state, so an insertion reaches them exactly as a deletion does."""
    cell = (
        p("plain")
        + f"<w:p>{run('kept ')}<w:ins>{run('added')}</w:ins></w:p>"
        + f"<w:ins><w:p>{run('whole')}{MATH}{box(p('boxed'))}</w:p></w:ins>"
    )

    assert revisions(docx(f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl>")) == [
        ("table_cell", "plain\nkept ", "visible", "current", "0.r0.c0"),
        ("table_cell", "added\nwhole", "visible", "inserted", "0.r0.c0.t1"),
        ("equation", "", "visible", "inserted", "0.r0.c0.2.e0"),
        ("body", "boxed", "visible", "inserted", "0.r0.c0.2.x0.0"),
    ]


def test_an_insertion_over_a_whole_table_reaches_its_runs_and_its_record() -> None:
    """A revision wrapping a table is the table's state, so the cell runs and
    the record a cell-less table takes instead both report it."""
    body = (
        f"<w:ins><w:tbl><w:tr><w:tc>{p('a')}</w:tc></w:tr></w:tbl></w:ins>"
        '<w:ins><w:tbl><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid></w:tbl></w:ins>'
    )

    assert revisions(docx(body)) == [
        ("table_cell", "a", "visible", "inserted", "0.r0.c0"),
        ("object_inventory", "", "visible", "inserted", "1.inventory"),
    ]


def test_move_revisions_over_a_block_reach_every_run_they_cover() -> None:
    """A move wrapping a whole block is that block's state. Text the author
    moved in reads as the page's own prose, text moved out reads as the
    deletion it is, and a table carries the state down to its cells, so the
    two halves of one move are never conflated into a single sentence."""
    body = (
        f"<w:moveTo><w:p>{run('arrived')}</w:p></w:moveTo>"
        f"<w:moveFrom><w:p>{run('departed', 'delText')}</w:p></w:moveFrom>"
        f"<w:moveTo><w:tbl><w:tr><w:tc>{p('cell in')}</w:tc></w:tr></w:tbl></w:moveTo>"
        f"<w:moveFrom><w:tbl><w:tr><w:tc>{p('cell out')}</w:tc></w:tr></w:tbl>"
        "</w:moveFrom>"
    )

    assert revisions(docx(body)) == [
        ("body", "arrived", "visible", "inserted", "0"),
        ("deleted_text", "departed", "annotation", "deleted", "1.d0"),
        ("table_cell", "cell in", "visible", "inserted", "2.r0.c0"),
        # The moved-out cell keeps no current prose, so it closes with the
        # empty record any cell takes when its text is all deleted.
        ("deleted_text", "cell out", "annotation", "deleted", "3.r0.c0.0.d0"),
        ("object_inventory", "", "annotation", "deleted", "3.r0.c0"),
    ]


def test_a_move_out_inside_a_move_in_stays_deleted_and_the_reverse_is_refused() -> None:
    """``w:moveFrom`` is a deletion and ``w:moveTo`` an insertion, so they
    nest exactly as ``w:del`` and ``w:ins`` do: text moved out of a moved-in
    passage stays deleted, and the contradictory pairing is refused rather
    than read as either state."""
    moved_in_then_out = (
        f"<w:p><w:moveTo><w:moveFrom>{run('x', 'delText')}</w:moveFrom></w:moveTo></w:p>"
    )
    assert revisions(docx(moved_in_then_out)) == [
        ("deleted_text", "x", "annotation", "deleted", "0.d0")
    ]

    for contradiction in (
        f"<w:p><w:moveFrom><w:moveTo>{run('x')}</w:moveTo></w:moveFrom></w:p>",
        f"<w:p><w:del><w:moveTo>{run('x')}</w:moveTo></w:del></w:p>",
    ):
        with pytest.raises(OOXMLInvalid, match="contradict"):
            extract_docx(docx(contradiction), capture_id=CAPTURE)


def test_move_range_markers_carry_no_prose_of_their_own() -> None:
    """The range markers that bracket a move are positions, not text. They
    neither contribute a word nor split the stretch they stand inside, so a
    moved passage is not reported twice over."""
    body = (
        '<w:moveFromRangeStart w:id="1" w:name="m"/>'
        f"<w:p>{run('before')}</w:p>"
        '<w:moveFromRangeEnd w:id="1"/>'
        f"<w:p>{run('plain')}"
        '<w:moveToRangeStart w:id="2" w:name="m"/>'
        f"{run(' after')}"
        '<w:moveToRangeEnd w:id="2"/></w:p>'
    )

    assert revisions(docx(body)) == [
        ("body", "before", "visible", "current", "0"),
        ("body", "plain after", "visible", "current", "1"),
    ]


def test_text_boxes_are_separate_blocks_read_once() -> None:
    body = (
        f"<w:p>{run('Anchor ')}"
        + box(p("Boxed one"), p("Boxed two", HEAD_STYLE))
        + f"{run('end')}</w:p>"
        + "<w:tbl><w:tr><w:tc>"
        + f"<w:p>{run('cell')}{box(p('In cell'))}</w:p>"
        + "</w:tc></w:tr></w:tbl>"
        + f"<w:p><w:del>{box(p('Boxed gone'))}</w:del></w:p>"
    )
    content = docx(body)

    assert revisions(content) == [
        ("body", "Anchor ", "visible", "current", "0"),
        ("body", "Boxed one", "visible", "current", "0.x0.0"),
        ("heading", "Boxed two", "visible", "current", "0.x0.1"),
        ("body", "end", "visible", "current", "0.t1"),
        ("table_cell", "cell", "visible", "current", "1.r0.c0"),
        ("body", "In cell", "visible", "current", "1.r0.c0.0.x0.0"),
        ("deleted_text", "Boxed gone", "annotation", "deleted", "2.x0.0.d0"),
    ]
    first = extract_docx(content, capture_id=CAPTURE)
    assert first == extract_docx(content, capture_id=CAPTURE)
    locators = [tuple(sorted(item.source_locator.items())) for item in first.segments]
    assert len(set(locators)) == len(locators)
    assert first.expected == 7


def hidden(text: str, properties: str = "<w:vanish/>") -> str:
    return f"<w:r><w:rPr>{properties}</w:rPr><w:t>{text}</w:t></w:r>"


def test_hidden_runs_split_visible_text_in_source_order() -> None:
    body = (
        "<w:p>"
        + run("visible ")
        + hidden("gone")
        + run("mid ")
        + hidden("web", "<w:webHidden/>")
        + hidden("shown", '<w:vanish w:val="false"/>')
        + run("tail")
        + "</w:p>"
    )

    segments = revisions(docx(body))

    assert segments == [
        ("body", "visible ", "visible", "current", "0"),
        ("hidden_content", "gone", "hidden", "current", "0.v0"),
        ("body", "mid ", "visible", "current", "0.t1"),
        ("hidden_content", "web", "hidden", "current", "0.v1"),
        ("body", "showntail", "visible", "current", "0.t2"),
    ]
    assert "gone" not in "".join(
        text for _, text, visibility, _, _ in segments if visibility == "visible"
    )


def test_hidden_runs_split_a_table_cell_without_joining_its_text() -> None:
    cell = p("a") + f"<w:p>{run('b ')}{hidden('x')}{run(' c')}</w:p>" + p("d")

    assert revisions(docx(f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl>")) == [
        ("table_cell", "a\nb ", "visible", "current", "0.r0.c0"),
        ("hidden_content", "x", "hidden", "current", "0.r0.c0.1.v0"),
        ("table_cell", " c\nd", "visible", "current", "0.r0.c0.t1"),
    ]


def test_hidden_runs_inside_a_text_box_stay_hidden_in_the_box_block() -> None:
    boxed = "<w:p>" + run("seen") + hidden("boxed") + "</w:p>"
    body = f"<w:p>{box(boxed)}</w:p>"

    assert revisions(docx(body)) == [
        ("body", "seen", "visible", "current", "0.x0.0"),
        ("hidden_content", "boxed", "hidden", "current", "0.x0.0.v0"),
    ]


def test_hidden_runs_stay_hidden_under_content_controls_and_revisions() -> None:
    body = (
        "<w:p>"
        + run("seen ")
        + f"<w:sdt><w:sdtContent><w:ins>{hidden('nested')}</w:ins></w:sdtContent></w:sdt>"
        + run("mid ")
        + f"<w:del>{hidden('gone')}</w:del>"
        + run("tail")
        + "</w:p>"
    )

    assert revisions(docx(body)) == [
        ("body", "seen ", "visible", "current", "0"),
        ("hidden_content", "nested", "hidden", "inserted", "0.v0"),
        ("body", "mid ", "visible", "current", "0.t1"),
        ("hidden_content", "gone", "hidden", "deleted", "0.v1"),
        ("body", "tail", "visible", "current", "0.t2"),
    ]


def order(content: bytes) -> list[tuple[str, str, str, object]]:
    return [
        (item.semantic_role, item.text or "", item.revision_state, pos)
        for item in extract_docx(content, capture_id=CAPTURE).segments
        for pos in [item.source_locator["position"]]
    ]


def test_interleaved_items_keep_source_order_and_unique_locators() -> None:
    interleaved = (
        f"<w:p>{run('one ')}<w:del>{run('two', 'delText')}</w:del>{run(' three')}"
        f"{box(p('boxed'))}{run('four')}<w:del>{run('five', 'delText')}</w:del></w:p>"
    )

    segments = order(docx(interleaved))

    assert segments == [
        ("body", "one ", "current", "0"),
        ("deleted_text", "two", "deleted", "0.d0"),
        ("body", " three", "current", "0.t1"),
        ("body", "boxed", "current", "0.x0.0"),
        ("body", "four", "current", "0.t2"),
        ("deleted_text", "five", "deleted", "0.d1"),
    ]
    assert len({pos for *_, pos in segments}) == len(segments)


def test_table_cells_keep_identity_and_order_around_interruptions() -> None:
    cell = (
        p("a")
        + p("b")
        + f"<w:p>{run('c ')}<w:del>{run('x', 'delText')}</w:del>{run(' d')}</w:p>"
        + p("e")
        + f"<w:p>{run('f')}{box(p('boxed'))}{run('g')}</w:p>"
        + "<w:tbl><w:tr><w:tc>"
        + p("inner")
        + "</w:tc></w:tr></w:tbl>"
        + p("h")
    )
    body = f"<w:tbl><w:tr><w:tc>{p('left')}</w:tc><w:tc>{cell}</w:tc></w:tr></w:tbl>"

    assert order(docx(body)) == [
        ("table_cell", "left", "current", "0.r0.c0"),
        ("table_cell", "a\nb\nc ", "current", "0.r0.c1"),
        ("deleted_text", "x", "deleted", "0.r0.c1.2.d0"),
        ("table_cell", " d\ne\nf", "current", "0.r0.c1.t1"),
        ("body", "boxed", "current", "0.r0.c1.4.x0.0"),
        ("table_cell", "g", "current", "0.r0.c1.t2"),
        ("table_cell", "inner", "current", "0.r0.c1.5.r0.c0"),
        ("table_cell", "h", "current", "0.r0.c1.t3"),
    ]


def test_annotation_stories_keep_order_and_roles_around_interruptions() -> None:
    pieces = (
        f"{run('one ')}<w:del>{run('two', 'delText')}</w:del>{run(' three')}"
        f"{box(p('boxed'))}{run('four')}"
    )
    parts = {
        "word/footnotes.xml": (
            f'<w:footnotes {W}><w:footnote w:id="2"><w:p>{pieces}</w:p></w:footnote>'
            "</w:footnotes>"
        ),
        "word/comments.xml": (
            f'<w:comments {W}><w:comment w:id="9"><w:tbl><w:tr><w:tc><w:p>{pieces}</w:p>'
            "</w:tc></w:tr></w:tbl></w:comment></w:comments>"
        ),
    }

    result = extract_docx(docx(p("Body"), parts), capture_id=CAPTURE)

    assert [
        (
            item.semantic_role,
            item.text,
            item.visibility,
            item.source_locator.get("noteId"),
            item.source_locator["position"],
        )
        for item in result.segments[1:]
    ] == [
        ("table_cell", "one ", "visible", "9", "0.r0.c0"),
        ("deleted_text", "two", "annotation", "9", "0.r0.c0.0.d0"),
        ("table_cell", " three", "visible", "9", "0.r0.c0.t1"),
        ("comment", "boxed", "annotation", "9", "0.r0.c0.0.x0.0"),
        ("table_cell", "four", "visible", "9", "0.r0.c0.t2"),
        ("note", "one ", "annotation", "2", "0"),
        ("deleted_text", "two", "annotation", "2", "0.d0"),
        ("note", " three", "annotation", "2", "0.t1"),
        ("note", "boxed", "annotation", "2", "0.x0.0"),
        ("note", "four", "annotation", "2", "0.t2"),
    ]


def test_text_box_nesting_is_bounded() -> None:
    body = p("deep")
    for _ in range(70):
        body = f"<w:p>{box(body, fallback=False)}</w:p>"

    with pytest.raises(OOXMLInvalid, match="nested too deeply"):
        extract_docx(docx(body), capture_id=CAPTURE)


def test_external_relationships_stay_inert_inventory() -> None:
    parts = {
        "word/_rels/document.xml.rels": (
            '<Relationships><Relationship Id="rId9" TargetMode="External" '
            'Target="https://example.invalid/x"/></Relationships>'
        )
    }

    result = extract_docx(docx(p("Linked"), parts, styles=False), capture_id=CAPTURE)

    assert [item.text for item in result.segments] == ["Linked"]
    assert result.limitations["externalReferences"] == 1


def test_table_nesting_is_bounded() -> None:
    body = p("deep")
    for _ in range(70):
        body = f"<w:tbl><w:tr><w:tc>{body}</w:tc></w:tr></w:tbl>"

    with pytest.raises(OOXMLInvalid, match="nested too deeply"):
        extract_docx(docx(body), capture_id=CAPTURE)


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def rels(*items: tuple[str, str, bool, str]) -> str:
    """Relationships as (id, target, external, type name)."""
    return (
        "<Relationships>"
        + "".join(
            f'<Relationship Id="{rid}" Type="{REL}{kind}" Target="{target}"'
            + (' TargetMode="External"' if external else "")
            + "/>"
            for rid, target, external, kind in items
        )
        + "</Relationships>"
    )


def link(rid: str, text: str, attrs: str = "") -> str:
    return f'<w:hyperlink r:id="{rid}"{attrs}>{run(text)}</w:hyperlink>'


def inventory(content: bytes) -> list[tuple[object, dict[str, object]]]:
    return [
        (item.source_locator["position"], item.structured_value)
        for item in extract_docx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]


def linked(
    body: str,
    *relations: tuple[str, str, bool, str],
    content_types: str = "",
    members: dict[str, bytes] | None = None,
    **parts: str,
) -> bytes:
    return docx(
        body,
        {"word/_rels/document.xml.rels": rels(*relations), **parts},
        styles=False,
        content_types=content_types,
        members=members,
    )


def test_hyperlinks_keep_text_in_order_and_inventory_each_target_inertly() -> None:
    body = (
        f"<w:p>{run('see ')}{link('rId1', 'site')}{run(' and ')}{link('rId2', 'rel')}"
        f"{link('rId3', 'file')}{link('rId4', 'part')}{run(' end')}</w:p>"
    )
    content = linked(
        body,
        ("rId1", "https://Example.invalid/a?b=1", True, "hyperlink"),
        ("rId2", "..\\other\\doc.docx", True, "hyperlink"),
        ("rId3", "file:///C:/secret.txt", True, "hyperlink"),
        ("rId4", "linked.xml", False, "hyperlink"),
        **{"word/linked.xml": "<x/>"},
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert [(i.semantic_role, i.text) for i in result.segments[:1]] == [
        ("body", "see site and relfilepart end")
    ]
    assert [
        (pos, v["scheme"], v["target"], v["resolved"], v["relationshipId"])
        for pos, v in inventory(content)
    ] == [
        ("0.h0", "https", "https://Example.invalid/a?b=1", False, "rId1"),
        ("0.h1", "", "..\\other\\doc.docx", False, "rId2"),
        ("0.h2", "file", "file:///C:/secret.txt", False, "rId3"),
        ("0.h3", None, "word/linked.xml", True, "rId4"),
    ]
    first = inventory(content)[0][1]
    assert first["relationshipType"] == REL + "hyperlink"
    assert all(i.extraction_method == "inventory" for i in result.segments[1:])
    assert result.limitations["externalReferences"] == 3


def test_hyperlink_inside_deleted_text_and_cells_keeps_order() -> None:
    body = (
        f'<w:p>{run("a ")}<w:del><w:hyperlink r:id="rId1">{run("gone", "delText")}'
        "</w:hyperlink></w:del></w:p>"
        f"<w:tbl><w:tr><w:tc><w:p>{run('cell ')}{link('rId1', 'x')}</w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = linked(body, ("rId1", "https://a.invalid/", True, "hyperlink"))

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (
            i.semantic_role,
            i.text,
            i.revision_state,
            i.visibility,
            i.source_locator["position"],
        )
        for i in result.segments
    ] == [
        ("body", "a ", "current", "visible", "0"),
        ("deleted_text", "gone", "deleted", "annotation", "0.d0"),
        ("object_inventory", None, "deleted", "annotation", "0.h0"),
        ("table_cell", "cell x", "current", "visible", "1.r0.c0"),
        ("object_inventory", None, "current", "visible", "1.r0.c0.0.h0"),
    ]


def test_anchor_only_hyperlink_has_no_target_and_long_targets_are_clipped() -> None:
    long = "https://a.invalid/" + "x" * 5000
    body = f'<w:p><w:hyperlink w:anchor="_Toc1">{run("toc")}</w:hyperlink>{link("rId1", "long")}</w:p>'
    content = linked(body, ("rId1", long, True, "hyperlink"))

    (_, anchor), (_, clipped) = inventory(content)

    assert anchor["anchor"] == "_Toc1"
    assert anchor["target"] is None and anchor["resolved"] is False
    assert clipped["target"] == long[:2048] and clipped["targetTruncated"] is True


@pytest.mark.parametrize(
    "body, relations, parts",
    [
        pytest.param(
            f"<w:p>{link('rId9', 'x')}</w:p>",
            [("rId1", "https://a.invalid/", True, "hyperlink")],
            {},
            id="missing-relationship",
        ),
        pytest.param(
            f"<w:p>{link('rId1', 'x')}</w:p>",
            [("rId1", "media.png", False, "image")],
            {"word/media.png": "x"},
            id="wrong-type",
        ),
        pytest.param(
            f"<w:p>{link('rId1', 'x')}</w:p>",
            [],
            {},
            id="no-relationships",
        ),
        pytest.param(
            "<w:p><w:hyperlink><w:r><w:t>x</w:t></w:r></w:hyperlink></w:p>",
            [],
            {},
            id="no-target",
        ),
        pytest.param(
            '<w:p><w:hyperlink r:id=""><w:r><w:t>x</w:t></w:r></w:hyperlink></w:p>',
            [("rId1", "https://a.invalid/", True, "hyperlink")],
            {},
            id="empty-id",
        ),
        pytest.param(
            '<w:p><w:hyperlink id="rId1"><w:r><w:t>x</w:t></w:r></w:hyperlink></w:p>',
            [("rId1", "https://a.invalid/", True, "hyperlink")],
            {},
            id="un-namespaced-id",
        ),
        pytest.param(
            f"<w:p>{link('rId1', 'x')}</w:p>",
            [("rId1", "https://a.invalid/", True, "hyperlink")] * 2,
            {},
            id="duplicate-relationship",
        ),
    ],
)
def test_bad_hyperlink_references_are_refused(
    body: str, relations: list[tuple[str, str, bool, str]], parts: dict[str, str]
) -> None:
    with pytest.raises(OOXMLInvalid):
        extract_docx(linked(body, *relations, **parts), capture_id=CAPTURE)


def test_hyperlink_resolves_only_against_its_own_part() -> None:
    footnotes = (
        f'<w:footnotes {W}><w:footnote w:id="2"><w:p>{link("rId1", "x")}</w:p>'
        "</w:footnote></w:footnotes>"
    )
    body = f'<w:p>{run("a")}<w:r><w:footnoteReference w:id="2"/></w:r></w:p>'
    relation = ("rId1", "https://a.invalid/", True, "hyperlink")

    with pytest.raises(OOXMLInvalid, match="missing relationship"):
        extract_docx(
            linked(body, relation, **{"word/footnotes.xml": footnotes}),
            capture_id=CAPTURE,
        )

    own = {
        "word/footnotes.xml": footnotes,
        "word/_rels/footnotes.xml.rels": rels(relation),
    }
    found = [
        (item.source_locator.get("noteId"), item.source_locator["position"])
        for item in extract_docx(linked(body, **own), capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
    ]
    assert found == [(None, "0.n0"), ("2", "0.h0")]


def ref(tag: str, note_id: str) -> str:
    return f'<w:{tag} w:id="{note_id}"/>'


NOTES = {
    "word/footnotes.xml": (
        f"<w:footnotes {W}>"
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r>'
        "<w:continuationSeparator/></w:r></w:p></w:footnote>"
        f'<w:footnote w:type="continuationNotice" w:id="1">{p("continued")}</w:footnote>'
        f'<w:footnote w:id="2">{p("Foot two")}</w:footnote></w:footnotes>'
    ),
    "word/endnotes.xml": (
        f'<w:endnotes {W}><w:endnote w:type="separator" w:id="-1">'
        f'<w:p><w:r><w:separator/></w:r></w:p></w:endnote><w:endnote w:id="3">{p("End three")}'
        "</w:endnote></w:endnotes>"
    ),
    "word/comments.xml": (
        f'<w:comments {W}><w:comment w:id="7" w:author="Ada L." w:date="2026-01-02T03:04:05Z"'
        f' w:initials="AL">{p("Comment seven")}</w:comment>'
        f'<w:comment w:id="8">{p("Comment eight")}</w:comment></w:comments>'
    ),
}


def noted(body: str, **extra: str) -> bytes:
    return docx(body, {**NOTES, **extra}, styles=False)


def test_note_references_link_by_story_and_id_without_duplicating_text() -> None:
    body = (
        f"<w:p>{run('a')}<w:r>{ref('footnoteReference', '2')}</w:r>"
        f"{run('b')}<w:r>{ref('footnoteReference', '2')}</w:r>"
        f"<w:r>{ref('endnoteReference', '3')}</w:r></w:p>"
    )
    content = noted(body)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text)
        for i in result.segments
        if i.text and i.semantic_role != "comment"
    ] == [
        ("body", "ab"),
        ("note", "End three"),
        ("note", "Foot two"),
    ]
    assert [
        (pos, v["annotation"], v["noteId"], v["occurrence"], v["targetPart"])
        for pos, v in inventory(content)
        if v["kind"] == "annotationReference"
    ] == [
        ("0.n0", "footnote", "2", 0, "word/footnotes.xml"),
        ("0.n1", "footnote", "2", 1, "word/footnotes.xml"),
        ("0.n2", "endnote", "3", 0, "word/endnotes.xml"),
    ]
    refs = [
        i for i in result.segments if i.source_locator["position"].startswith("0.n")
    ]
    assert {i.visibility for i in refs} == {"visible"}
    # The reference points at the same identity the note's own segments carry.
    note = next(i for i in result.segments if i.text == "Foot two")
    assert (note.source_locator["story"], note.source_locator["noteId"]) == (
        "footnote",
        "2",
    )


def test_separator_entries_are_excluded_inventory_never_note_prose() -> None:
    content = noted(p("Body"))

    result = extract_docx(content, capture_id=CAPTURE)

    separators = [
        (i.source_locator["story"], v["noteType"], v["noteId"], i.visibility)
        for i in result.segments
        if i.semantic_role == "object_inventory"
        for v in [i.structured_value]
        if v["kind"] == "noteSeparator"
    ]
    assert separators == [
        ("endnote", "separator", "-1", "annotation"),
        ("footnote", "separator", "-1", "annotation"),
        ("footnote", "continuationSeparator", "0", "annotation"),
        ("footnote", "continuationNotice", "1", "annotation"),
    ]
    assert "continued" not in {i.text for i in result.segments}
    assert result.expected == 5  # body, endnote, footnote and two comments


def test_comment_ranges_references_and_metadata() -> None:
    body = (
        f"<w:p>{ref('commentRangeStart', '7')}{run('anchored')}{ref('commentRangeEnd', '7')}"
        f"<w:r>{ref('commentReference', '7')}</w:r></w:p>"
        f"<w:p>{ref('commentRangeStart', '7')}{run('again')}"
        f"<w:del>{ref('commentRangeEnd', '7')}</w:del></w:p>"
    )
    content = noted(body)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (pos, v["marker"], v["occurrence"])
        for pos, v in inventory(content)
        if v["kind"] == "annotationReference"
    ] == [
        ("0.n0", "rangeStart", 0),
        ("0.n1", "rangeEnd", 0),
        ("0.n2", "reference", 0),
        ("1.n0", "rangeStart", 1),
        ("1.n1", "rangeEnd", 1),
    ]
    markers = [
        i
        for i in result.segments
        if i.structured_value and i.structured_value["kind"] == "annotationReference"
    ]
    assert {i.visibility for i in markers} == {"annotation"}
    assert markers[-1].revision_state == "deleted"
    metadata = [
        (i.source_locator["noteId"], i.structured_value)
        for i in result.segments
        if i.structured_value and i.structured_value["kind"] == "commentMetadata"
    ]
    assert metadata == [
        (
            "7",
            {
                "kind": "commentMetadata",
                "noteId": "7",
                "author": "Ada L.",
                "date": "2026-01-02T03:04:05Z",
                "initials": "AL",
            },
        )
    ]
    texts = [i.text for i in result.segments if i.semantic_role == "comment"]
    assert texts == ["Comment seven", "Comment eight"]


def test_comment_metadata_is_clipped() -> None:
    comments = (
        f'<w:comments {W}><w:comment w:id="1" w:author="{"a" * 600}">{p("c")}'
        "</w:comment></w:comments>"
    )
    content = docx(p("b"), {"word/comments.xml": comments}, styles=False)

    ((_, value),) = inventory(content)

    assert value == {"kind": "commentMetadata", "noteId": "1", "author": "a" * 256}


def test_range_markers_between_blocks_rows_and_cells_are_located() -> None:
    body = (
        f"{ref('commentRangeStart', '7')}{p('one')}"
        f"<w:tbl>{ref('commentRangeStart', '8')}<w:tr>{ref('commentRangeEnd', '8')}"
        f"<w:tc>{ref('commentRangeStart', '7')}{p('cell')}{ref('commentRangeEnd', '7')}</w:tc></w:tr></w:tbl>"
        f"{ref('commentRangeEnd', '7')}{p('two')}"
    )
    content = noted(body)

    assert [
        (pos, v["marker"], v["noteId"])
        for pos, v in inventory(content)
        if v["kind"] == "annotationReference"
    ] == [
        ("m0", "rangeStart", "7"),
        ("1.m0", "rangeStart", "8"),
        ("1.r0.m0", "rangeEnd", "8"),
        ("1.r0.c0.m0", "rangeStart", "7"),
        ("1.r0.c0.m1", "rangeEnd", "7"),
        ("m1", "rangeEnd", "7"),
    ]
    assert [
        i.text
        for i in extract_docx(content, capture_id=CAPTURE).segments
        if i.semantic_role in {"body", "table_cell"}
    ] == ["one", "cell", "two"]


@pytest.mark.parametrize(
    "body, parts",
    [
        pytest.param(
            f"<w:p><w:r>{ref('footnoteReference', '9')}</w:r></w:p>",
            {},
            id="unknown-footnote",
        ),
        pytest.param(
            f"<w:p><w:r>{ref('footnoteReference', '3')}</w:r></w:p>",
            {},
            id="wrong-story",
        ),
        pytest.param(
            f"<w:p><w:r>{ref('footnoteReference', '0')}</w:r></w:p>",
            {},
            id="separator-target",
        ),
        pytest.param(
            f"<w:p>{ref('commentRangeStart', '99')}</w:p>", {}, id="unknown-comment"
        ),
        pytest.param("<w:p><w:r><w:footnoteReference/></w:r></w:p>", {}, id="no-id"),
        pytest.param(
            f"<w:p><w:r>{ref('footnoteReference', 'x')}</w:r></w:p>", {}, id="bad-id"
        ),
        pytest.param(
            p("a"),
            {
                "word/footnotes.xml": f'<w:footnotes {W}><w:footnote w:id="2">{p("a")}</w:footnote><w:footnote w:id="2">{p("b")}</w:footnote></w:footnotes>'
            },
            id="repeated-note-id",
        ),
        pytest.param(
            p("a"),
            {
                "word/footnotes.xml": f"<w:footnotes {W}><w:footnote>{p('a')}</w:footnote><w:footnote>{p('b')}</w:footnote></w:footnotes>"
            },
            id="repeated-missing-note-id",
        ),
        pytest.param(
            p("a"),
            {
                "word/footnotes.xml": f'<w:footnotes {W}><w:footnote w:type="odd" w:id="2">{p("a")}</w:footnote></w:footnotes>'
            },
            id="unknown-note-type",
        ),
    ],
)
def test_ambiguous_annotation_references_are_refused(
    body: str, parts: dict[str, str]
) -> None:
    content = docx(body, {**NOTES, **parts} if not parts else parts, styles=False)

    with pytest.raises(OOXMLInvalid):
        extract_docx(content, capture_id=CAPTURE)


def test_links_and_references_are_stable_and_locators_unique() -> None:
    body = (
        f"<w:p>{ref('commentRangeStart', '7')}{link('rId1', 'a')}{link('rId1', 'b')}"
        f"<w:r>{ref('footnoteReference', '2')}</w:r>{ref('commentRangeEnd', '7')}</w:p>"
        f"<w:p>{link('rId1', 'c')}<w:r>{ref('footnoteReference', '2')}</w:r></w:p>"
    )
    content = linked(body, ("rId1", "https://a.invalid/", True, "hyperlink"), **NOTES)

    first = extract_docx(content, capture_id=CAPTURE)

    assert first == extract_docx(content, capture_id=CAPTURE)
    locators = [tuple(sorted(i.source_locator.items())) for i in first.segments]
    assert len(set(locators)) == len(locators)


def test_a_lone_note_without_an_id_is_read_but_is_no_reference_target() -> None:
    notes = {
        "word/footnotes.xml": f"<w:footnotes {W}><w:footnote>{p('Anon')}</w:footnote></w:footnotes>"
    }

    result = extract_docx(docx(p("Body"), notes, styles=False), capture_id=CAPTURE)

    assert [(i.text, i.source_locator.get("noteId")) for i in result.segments] == [
        ("Body", None),
        ("Anon", None),
    ]
    with pytest.raises(OOXMLInvalid, match="names no annotation"):
        extract_docx(
            docx(
                '<w:p><w:r><w:footnoteReference w:id="1"/></w:r></w:p>',
                notes,
                styles=False,
            ),
            capture_id=CAPTURE,
        )


def test_simple_field_is_inventoried_before_the_result_it_stored() -> None:
    body = (
        f"<w:p>{run('Printed ')}"
        '<w:fldSimple w:instr=" DATE \\@ &quot;d MMMM yyyy&quot; ">'
        f"{run('3 May 2026')}</w:fldSimple>{run(' sharp')}</w:p>"
    )
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Printed ", "0"),
        ("object_inventory", None, "0.f0"),
        ("body", "3 May 2026 sharp", "0.t1"),
    ]
    assert inventory(content) == [
        (
            "0.f0",
            {
                "kind": "wordField",
                "fieldForm": "simple",
                "instruction": ' DATE \\@ "d MMMM yyyy" ',
                "instructionTruncated": False,
                "executed": False,
                "refreshed": False,
                "savedResultPresent": True,
                "resultFreshness": "unknown",
            },
        )
    ]


def test_simple_field_naming_a_file_stays_inert_and_its_result_is_only_text() -> None:
    body = (
        '<w:p><w:fldSimple w:instr=" INCLUDETEXT &quot;C:\\\\secret\\\\x.docx&quot; ">'
        f"{run('Stored copy')}</w:fldSimple>"
        '<w:fldSimple w:instr=" PAGE "/>'
        f'<w:fldSimple w:instr=" TIME ">{run("")}</w:fldSimple></w:p>'
    )
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)
    include, page, time = (value for _, value in inventory(content))

    # The path keeps the doubled separators Word writes: exactly as stored.
    assert include["instruction"] == ' INCLUDETEXT "C:\\\\secret\\\\x.docx" '
    # Nothing the instruction names is read, resolved or counted as a reference.
    assert set(include) == set(page)
    assert "target" not in include and "relationshipId" not in include
    assert result.limitations["externalReferences"] == 0
    # An empty field saved no result, whether or not it holds a result run.
    assert [value["savedResultPresent"] for value in (include, page, time)] == [
        True,
        False,
        False,
    ]
    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", None, "0.f0"),
        ("body", "Stored copy", "0"),
        ("object_inventory", None, "0.f1"),
        ("object_inventory", None, "0.f2"),
    ]
    with pytest.raises(OOXMLInvalid, match="names no instruction"):
        extract_docx(
            docx("<w:p><w:fldSimple/></w:p>", styles=False), capture_id=CAPTURE
        )


def test_simple_field_in_a_cell_is_inventoried_before_the_run_it_opens() -> None:
    body = (
        f"<w:tbl><w:tr><w:tc><w:p>{run('Due ')}"
        '<w:fldSimple w:instr=" DATE ">'
        f"{run('3 May 2026')}</w:fldSimple>{run(' sharp')}</w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = docx(body, styles=False)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("table_cell", "Due ", "0.r0.c0"),
        ("object_inventory", None, "0.r0.c0.0.f0"),
        ("table_cell", "3 May 2026 sharp", "0.r0.c0.t1"),
    ]


def test_a_simple_field_instruction_is_bounded_and_says_when_it_was_clipped() -> None:
    at_limit = " REF " + "a" * (_MAX_TARGET_CHARS - 5)
    body = (
        f'<w:p><w:fldSimple w:instr="{at_limit}"/>'
        f'<w:fldSimple w:instr="{at_limit}b"/></w:p>'
    )

    fits, clipped = (value for _, value in inventory(docx(body, styles=False)))

    assert (fits["instruction"], fits["instructionTruncated"]) == (at_limit, False)
    assert clipped["instructionTruncated"] is True
    assert clipped["instruction"] == at_limit
    assert len(clipped["instruction"]) == _MAX_TARGET_CHARS  # type: ignore[arg-type]


def fld(kind: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def instr(text: str) -> str:
    return run(text, "instrText")


def test_complex_field_is_inventoried_before_the_result_it_saved() -> None:
    code = instr(" DATE \\@ &quot;d MMMM yyyy&quot; ")
    body = (
        f"<w:p>{run('Printed ')}{fld('begin')}{code}"
        f"{fld('separate')}{run('3 May 2026')}{fld('end')}{run(' sharp')}</w:p>"
    )
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Printed ", "0"),
        ("object_inventory", None, "0.f0"),
        ("body", "3 May 2026 sharp", "0.t1"),
    ]
    assert inventory(content) == [
        (
            "0.f0",
            {
                "kind": "wordField",
                "fieldForm": "complex",
                "instruction": ' DATE \\@ "d MMMM yyyy" ',
                "instructionTruncated": False,
                "executed": False,
                "refreshed": False,
                "savedResultPresent": True,
                "resultFreshness": "unknown",
            },
        )
    ]


def test_complex_field_naming_a_file_stays_inert_and_joins_its_fragments() -> None:
    path = instr("&quot;C:\\\\secret\\\\x.docx&quot;")
    body = (
        f"<w:p>{fld('begin')}{instr(' INCLUDE')}{instr('TEXT ')}{path}{instr(' ')}"
        f"{fld('separate')}{run('Stored copy')}{fld('end')}</w:p>"
    )
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)
    ((_, include),) = inventory(content)

    # Fragments join exactly in source order, separators doubled as stored.
    assert include["instruction"] == ' INCLUDETEXT "C:\\\\secret\\\\x.docx" '
    # Nothing the instruction names is read, resolved or counted as a reference.
    assert "target" not in include and "relationshipId" not in include
    assert result.limitations["externalReferences"] == 0
    # Only the saved result reads as text; no fragment of the code does.
    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", None, "0.f0"),
        ("body", "Stored copy", "0"),
    ]


def test_a_nested_field_result_inside_an_instruction_is_recorded_but_not_read() -> None:
    body = (
        f"<w:p>{fld('begin')}{instr(' IF ')}"
        f"{fld('begin')}{instr(' PAGE ')}{fld('separate')}{run('3')}{fld('end')}"
        f"{instr(' = 3 ')}{fld('separate')}{run('yes')}{fld('end')}</w:p>"
    )
    content = docx(body, styles=False)

    # Each record still sits at its own boundary, and the inner one's result is
    # the outer instruction's argument, which Word renders no more than the code
    # around it: it is recorded, and only the outer result is read as prose.
    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", None, "0.f0"),
        ("object_inventory", None, "0.f1"),
        ("body", "yes", "0"),
    ]
    inner, outer = (value for _, value in inventory(content))
    assert (inner["instruction"], outer["instruction"]) == (" PAGE ", " IF  = 3 ")
    assert [inner["savedResultPresent"], outer["savedResultPresent"]] == [True, True]


def test_a_nested_field_result_is_kept_out_of_every_reading_of_the_prose() -> None:
    inner = (
        f"{fld('begin')}{instr(' PAGE ')}{fld('separate')}"
        "<w:del><w:r><w:delText>2</w:delText></w:r></w:del>"
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t>quiet</w:t></w:r>"
        f"{run('3')}{fld('end')}"
    )
    body = (
        f"<w:p>{fld('begin')}{instr(' IF ')}{inner}"
        f"{instr(' = 3 ')}{fld('separate')}{run('yes')}{fld('end')}</w:p>"
    )
    content = docx(body, styles=False)

    # No deleted or hidden reading of the inner result survives either: inside
    # an open instruction there is no prose of any visibility to place it in.
    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", None, "0.f0"),
        ("object_inventory", None, "0.f1"),
        ("body", "yes", "0"),
    ]
    assert [value["savedResultPresent"] for _, value in inventory(content)] == [
        True,
        True,
    ]


def test_a_complex_field_without_a_separator_is_recorded_where_it_closes() -> None:
    body = (
        f"<w:p>{run('Page ')}{fld('begin')}{instr(' PAGE ')}"
        f"{fld('end')}{run(' of 9')}</w:p>"
    )
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)
    ((_, record),) = inventory(content)

    assert (record["instruction"], record["savedResultPresent"]) == (" PAGE ", False)
    # The record sits at the boundary it closed: there is no result to precede.
    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Page ", "0"),
        ("object_inventory", None, "0.f0"),
        ("body", " of 9", "0.t1"),
    ]


def test_a_complex_field_result_keeps_the_revision_and_hidden_reading_it_had() -> None:
    body = (
        f"<w:p>{fld('begin')}{instr(' DATE ')}{fld('separate')}"
        "<w:del><w:r><w:delText>old</w:delText></w:r></w:del>"
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t>quiet</w:t></w:r>"
        f"{run('new')}{fld('end')}</w:p>"
    )
    content = docx(body, styles=False)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", None, "0.f0"),
        ("deleted_text", "old", "0.d0"),
        ("hidden_content", "quiet", "0.v0"),
        ("body", "new", "0"),
    ]
    assert inventory(content)[0][1]["savedResultPresent"] is True


def test_a_complex_field_in_a_cell_is_inventoried_before_the_run_it_opens() -> None:
    body = (
        f"<w:tbl><w:tr><w:tc><w:p>{run('Due ')}{fld('begin')}{instr(' DATE ')}"
        f"{fld('separate')}{run('3 May 2026')}{fld('end')}{run(' sharp')}</w:p>"
        f"<w:p>{run('after')}</w:p></w:tc></w:tr></w:tbl>"
    )

    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(docx(body, styles=False), capture_id=CAPTURE).segments
    ] == [
        ("table_cell", "Due ", "0.r0.c0"),
        ("object_inventory", None, "0.r0.c0.0.f0"),
        ("table_cell", "3 May 2026 sharp\nafter", "0.r0.c0.t1"),
    ]


def test_a_complex_field_spans_the_paragraphs_between_its_own_boundaries() -> None:
    code = instr(" TOC \\o &quot;1-3&quot; ")
    body = (
        f"<w:p>{run('Contents')}{fld('begin')}{code}</w:p>"
        f"<w:p>{fld('separate')}{run('1 Opening')}</w:p>"
        f"<w:p>{run('2 Closing')}</w:p>"
        f"<w:p>{fld('end')}{run('after')}</w:p>"
    )
    content = docx(body, styles=False)

    # The record is ordered at the separator, so it still precedes every stored
    # result; each position is the block the item was read in and nothing else.
    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("body", "Contents", "0"),
        ("object_inventory", None, "1.f0"),
        ("body", "1 Opening", "1"),
        ("body", "2 Closing", "2"),
        ("body", "after", "3"),
    ]
    assert inventory(content) == [
        (
            "1.f0",
            {
                "kind": "wordField",
                "fieldForm": "complex",
                "instruction": ' TOC \\o "1-3" ',
                "instructionTruncated": False,
                "executed": False,
                "refreshed": False,
                "savedResultPresent": True,
                "resultFreshness": "unknown",
            },
        )
    ]


def test_a_field_boundary_may_not_cross_a_separately_walked_story() -> None:
    stray = box(f"<w:p>{fld('end')}{run('inner')}</w:p>")
    opened = box(f"<w:p>{fld('begin')}{instr(' PAGE ')}</w:p>")
    whole = (
        f"<w:p>{fld('begin')}{instr(' DATE ')}{fld('separate')}"
        f"{run('x')}{fld('end')}</w:p>"
    )
    note = (
        f'<w:footnotes {W}><w:footnote w:id="2"><w:p>{fld("end")}</w:p>'
        "</w:footnote></w:footnotes>"
    )
    cases = (
        # A text box is its own story: a field open around it is out of reach of
        # a boundary inside it, and one opened inside it may not outlive it.
        (
            docx(f"<w:p>{fld('begin')}{instr(' IF ')}{stray}</w:p>", styles=False),
            "opens no field",
        ),
        (docx(f"<w:p>{opened}</w:p>", styles=False), "is never closed"),
        # A note container is walked on its own too, so the body lends it nothing.
        (docx(whole, {"word/footnotes.xml": note}, styles=False), "opens no field"),
    )

    for content, refusal in cases:
        with pytest.raises(OOXMLInvalid, match=refusal):
            extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        (f"<w:p>{fld('separate')}</w:p>", "opens no field"),
        (f"<w:p>{fld('end')}</w:p>", "opens no field"),
        (
            f"<w:p>{fld('begin')}{fld('separate')}{fld('separate')}{fld('end')}</w:p>",
            "separates twice",
        ),
        (f"<w:p>{fld('begin')}{instr(' DATE ')}</w:p>", "is never closed"),
        (f"<w:p>{fld('begin')}{fld('separate')}{run('x')}</w:p>", "is never closed"),
        (f"<w:p>{fld('resume')}</w:p>", "names no kind"),
        ("<w:p><w:r><w:fldChar/></w:r></w:p>", "names no kind"),
        (
            (
                f"<w:p>{fld('begin') * (_MAX_DOCX_FIELD_DEPTH + 1)}"
                f"{fld('end') * (_MAX_DOCX_FIELD_DEPTH + 1)}</w:p>"
            ),
            "nested too deeply",
        ),
    ],
)
def test_a_malformed_field_sequence_is_refused_rather_than_guessed(
    body: str, refusal: str
) -> None:
    with pytest.raises(OOXMLInvalid, match=refusal):
        extract_docx(docx(body, styles=False), capture_id=CAPTURE)


def test_a_complex_field_instruction_is_bounded_across_its_fragments() -> None:
    fragment = "a" * 512
    fragments = 4 * (1 + _MAX_TARGET_CHARS // len(fragment))
    body = (
        f"<w:p>{fld('begin')}{instr(' REF ')}"
        f"{instr(fragment) * fragments}"
        f"{fld('separate')}{run('x')}{fld('end')}</w:p>"
    )

    ((_, record),) = inventory(docx(body, styles=False))

    # Far more source text arrives than the bound allows, and what comes out is
    # exactly the bounded prefix: nothing past the bound was ever held.
    assert record["instruction"] == (" REF " + fragment * fragments)[:_MAX_TARGET_CHARS]
    assert record["instructionTruncated"] is True
    assert len(record["instruction"]) == _MAX_TARGET_CHARS  # type: ignore[arg-type]


def test_a_field_state_never_holds_more_than_the_bound() -> None:
    state = _FieldState()

    for _ in range(100):
        state.extend("a" * _MAX_TARGET_CHARS)

    assert (len(state.instruction), state.truncated) == (_MAX_TARGET_CHARS, True)


MATH = "<m:oMath><m:r><m:t>x</m:t></m:r><m:r><m:t> + 1</m:t></m:r></m:oMath>"
# The writer's own prefixes, not the file's: what ``normalized`` serialization
# means. The subtree holds nothing outside the math namespace, so it is ``ns0``.
OMML = (
    '<ns0:oMath xmlns:ns0="http://schemas.openxmlformats.org/officeDocument/2006/math">'
    "<ns0:r><ns0:t>x</ns0:t></ns0:r><ns0:r><ns0:t> + 1</ns0:t></ns0:r></ns0:oMath>"
)


def math(text: str) -> str:
    return f"<m:oMath><m:r><m:t>{text}</m:t></m:r></m:oMath>"


def equations(content: bytes) -> list[tuple[object, dict[str, object]]]:
    return [
        (item.source_locator["position"], item.structured_value)
        for item in extract_docx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "equation"
    ]


def test_inline_math_is_recorded_whole_and_split_from_the_prose_around_it() -> None:
    body = (
        f"<w:p>{run('Solve ')}{MATH}{run(' today')}</w:p>"
        f"<w:p><w:del>{MATH}</w:del></w:p>"
    )
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"], i.revision_state)
        for i in result.segments
    ] == [
        ("body", "Solve ", "0", "current"),
        ("equation", None, "0.e0", "current"),
        ("body", " today", "0.t1", "current"),
        # A deletion over the math is kept as the revision it is.
        ("equation", None, "1.e0", "deleted"),
    ]
    assert [i.visibility for i in result.segments] == [
        "visible",
        "visible",
        "visible",
        "annotation",
    ]
    inline, removed = (value for _, value in equations(content))
    assert inline == {
        "format": "OMML",
        "serialization": "normalized",
        "sourceMarkup": OMML,
        # Only the stored ``m:t`` text, in source order: nothing is read out of
        # the structure and no reading is generated from it.
        "directText": "x + 1",
        "generatedReading": False,
    }
    # The same package always serializes to the same markup.
    assert removed == inline
    assert all("x + 1" != item.text for item in result.segments)


def test_math_splits_a_table_cell_rather_than_joining_its_prose() -> None:
    body = (
        f"<w:tbl><w:tr><w:tc><w:p>{run('Area ')}{MATH}{run(' exactly')}</w:p>"
        f"<w:p>{run('as printed')}</w:p></w:tc></w:tr></w:tbl>"
    )

    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(docx(body, styles=False), capture_id=CAPTURE).segments
    ] == [
        ("table_cell", "Area ", "0.r0.c0"),
        ("equation", None, "0.r0.c0.0.e0"),
        ("table_cell", " exactly\nas printed", "0.r0.c0.t1"),
    ]


def test_an_oversize_equation_is_refused_rather_than_partly_recorded() -> None:
    ((_, one),) = equations(docx(f"<w:p>{math('a')}</w:p>", styles=False))
    # The markup around one stored character, so the bound can be approached
    # from the fixture rather than from a copy of the serializer's own shape.
    overhead = len(str(one["sourceMarkup"]).encode()) - 1
    fits = (_MAX_DOCX_EQUATION_BYTES - overhead) // 2

    ((_, record),) = equations(docx(f"<w:p>{math('a' * fits)}</w:p>", styles=False))

    assert str(record["directText"]) == "a" * fits
    assert (
        len(str(record["sourceMarkup"]).encode())
        + len(str(record["directText"]).encode())
        <= _MAX_DOCX_EQUATION_BYTES
    )
    with pytest.raises(OOXMLInvalid, match="too large to record"):
        extract_docx(
            docx(f"<w:p>{math('a' * (fits + 2))}</w:p>", styles=False),
            capture_id=CAPTURE,
        )


def test_an_equation_is_the_saved_result_of_the_field_it_sits_under() -> None:
    body = (
        f"<w:p>{fld('begin')}{instr(' EQ ')}{fld('separate')}{MATH}{fld('end')}</w:p>"
    )
    content = docx(body, styles=False)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [("object_inventory", "0.f0"), ("equation", "0.e0")]
    # The field stored a result even though no prose was read under it.
    assert inventory(content)[0][1]["savedResultPresent"] is True


def test_math_inside_a_field_instruction_is_refused() -> None:
    body = (
        f"<w:p>{fld('begin')}{instr(' EQ ')}{MATH}{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds math"):
        extract_docx(docx(body, styles=False), capture_id=CAPTURE)


# One display block holding the two equations Word wrote into it.
DISPLAY = f"<m:oMathPara>{math('x')}{math('y')}</m:oMathPara>"
MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
DISPLAY_OMML = (
    f'<ns0:oMathPara xmlns:ns0="{MATH_NS}">'
    "<ns0:oMath><ns0:r><ns0:t>x</ns0:t></ns0:r></ns0:oMath>"
    "<ns0:oMath><ns0:r><ns0:t>y</ns0:t></ns0:r></ns0:oMath>"
    "</ns0:oMathPara>"
)
DISPLAY_RECORD = {
    "format": "OMML",
    "serialization": "normalized",
    "sourceMarkup": DISPLAY_OMML,
    # Every ``m:t`` the container stores directly, in source order.
    "directText": "xy",
    "generatedReading": False,
}


def test_display_math_is_one_block_equation_in_the_source_order_it_stands() -> None:
    body = f"<w:p>{run('Before')}</w:p>{DISPLAY}<w:p>{run('After')}</w:p>"
    content = docx(body, styles=False)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Before", "0"),
        # One equation for the whole container, in the block position it stands
        # in: neither ``oMath`` inside it is recorded again beside it.
        ("equation", None, "1"),
        ("body", "After", "2"),
    ]
    assert [value for _, value in equations(content)] == [DISPLAY_RECORD]
    # Display math is no paragraph, so it is counted as none.
    assert result.expected == 2


def test_display_math_anchored_inside_a_paragraph_is_still_one_equation() -> None:
    body = f"<w:p>{run('Solve ')}{DISPLAY}{run(' twice')}</w:p>"
    content = docx(body, styles=False)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("body", "Solve ", "0"),
        ("equation", None, "0.e0"),
        ("body", " twice", "0.t1"),
    ]
    assert [value for _, value in equations(content)] == [DISPLAY_RECORD]


def test_display_math_splits_a_cell_and_keeps_a_deletion_over_it() -> None:
    body = (
        f"<w:tbl><w:tr><w:tc><w:p>{run('Area')}</w:p>{DISPLAY}"
        f"<w:p>{run('as printed')}</w:p><w:del>{DISPLAY}</w:del>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = docx(body, styles=False)

    assert [
        (
            i.semantic_role,
            i.text,
            i.source_locator["position"],
            i.revision_state,
            i.visibility,
        )
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("table_cell", "Area", "0.r0.c0", "current", "visible"),
        ("equation", None, "0.r0.c0.1", "current", "visible"),
        # The math closed the run rather than trailing the prose it follows.
        ("table_cell", "as printed", "0.r0.c0.t1", "current", "visible"),
        ("equation", None, "0.r0.c0.3", "deleted", "annotation"),
    ]
    # The deletion changes the revision it is read as and nothing it records.
    assert [value for _, value in equations(content)] == [DISPLAY_RECORD] * 2


def test_display_math_is_the_saved_result_of_the_field_it_sits_under() -> None:
    body = (
        f"<w:p>{fld('begin')}{instr(' EQ ')}{fld('separate')}</w:p>"
        f"{DISPLAY}<w:p>{fld('end')}</w:p>"
    )
    content = docx(body, styles=False)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [("object_inventory", "0.f0"), ("equation", "1")]
    # The field stored a result even though no prose was read under it.
    assert inventory(content)[0][1]["savedResultPresent"] is True


def test_display_math_inside_a_field_instruction_is_refused() -> None:
    body = (
        f"<w:p>{fld('begin')}{instr(' EQ ')}</w:p>{DISPLAY}"
        f"<w:p>{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds math"):
        extract_docx(docx(body, styles=False), capture_id=CAPTURE)


def test_a_display_container_in_another_namespace_is_no_equation() -> None:
    other = (
        '<z:oMathPara xmlns:z="urn:example:other">'
        f"<w:p>{run('plain')}</w:p></z:oMathPara>"
    )

    assert read(docx(other, styles=False)) == [("body", "plain", "0")]


# A member that is declared an image and is not one: nothing here decodes a
# byte of it, so a package holding this still reads exactly as one holding a
# real picture would.
IMAGE_MEMBER = {"word/media/image1.png": b"\x89PNG\r\n\x1a\nnot really an image"}
PNG_DEFAULT = '<Default Extension="png" ContentType="image/png"/>'
IMAGE_RELATION = ("rId9", "media/image1.png", False, "image")
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
DOC_PR = ' id="7" name="Picture 7"'
# A header is reached only through the document's own section and relationship,
# so a story in one takes the three declarations Word writes for it.
HEADER_OVERRIDE = (
    '<Override PartName="/word/header1.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.header+xml"/>'
)
HEADER_RELATION = ("rId4", "header1.xml", False, "header")
HEADER_SECTION = '<w:sectPr><w:headerReference w:type="default" r:id="rId4"/></w:sectPr>'


def blip(embed: str | None = "rId9", target: str | None = None, extra: str = "") -> str:
    ids = "".join(
        f' r:{attribute}="{value}"'
        for attribute, value in (("embed", embed), ("link", target))
        if value is not None
    )
    return f"<a:blip{ids}{extra}/>"


def picture(fill: str) -> str:
    return f"<pic:pic><pic:blipFill>{fill}</pic:blipFill></pic:pic>"


def drawing(
    placement: str = "inline",
    *,
    doc_pr: str | None = DOC_PR,
    graphic: str | None = None,
) -> str:
    body = (
        f"<a:graphic><a:graphicData>{picture(blip())}</a:graphicData></a:graphic>"
        if graphic is None
        else graphic
    )
    properties = "" if doc_pr is None else f"<wp:docPr{doc_pr}/>"
    return (
        f"<w:r><w:drawing><wp:{placement}>{properties}{body}"
        f"</wp:{placement}></w:drawing></w:r>"
    )


def imaged(body: str, *relations: tuple[str, str, bool, str], **parts: str) -> bytes:
    return linked(
        body,
        *(relations or (IMAGE_RELATION,)),
        content_types=PNG_DEFAULT,
        members=IMAGE_MEMBER,
        **parts,
    )


def test_an_embedded_image_is_inventoried_where_it_sits_with_the_alt_text_after() -> (
    None
):
    """The record comes first and the author's own claims follow it, each under
    the attribute that supplied it."""
    body = (
        f"<w:p>{run('Before ')}"
        + drawing(doc_pr=f'{DOC_PR} title="Logo" descr="The company logo"')
        + f"{run(' after')}</w:p>"
    )
    content = imaged(body)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Before ", "0"),
        ("object_inventory", None, "0.i0"),
        ("image_alt", "Logo", "0.i0"),
        ("image_alt", "The company logo", "0.i0"),
        # The image closed the stretch it interrupted rather than joining it.
        ("body", " after", "0.t1"),
    ]
    assert [i.source_locator.get("altRole") for i in result.segments] == [
        None,
        None,
        "title",
        "description",
        None,
    ]
    assert inventory(content) == [
        (
            "0.i0",
            {
                "kind": "drawingImage",
                "placement": "inline",
                "docPrId": "7",
                # An autogenerated object name is not a description, so it is
                # recorded and never reported as text the author wrote.
                "shapeName": "Picture 7",
                "altTitle": "Logo",
                "altDescription": "The company logo",
                "imageEmbedRelationshipId": "rId9",
                "imageEmbedRelationshipType": REL + "image",
                "imagePart": "word/media/image1.png",
                "imageContentType": "image/png",
                "imageBytes": len(IMAGE_MEMBER["word/media/image1.png"]),
                "imageLinkRelationshipId": None,
                "imageLinkRelationshipType": None,
                "imageLinkTarget": None,
                "imageLinkScheme": None,
                "imageLinkExternal": None,
                "imageDecoded": False,
                "ocrPerformed": False,
                "ocrOutcome": EMBEDDED_DISABLED,
            },
        )
    ]
    assert [i.extraction_method for i in result.segments[1:2]] == ["inventory"]


def test_an_anchored_image_and_a_linked_one_are_each_recorded_as_declared() -> None:
    """Placement is the element's own, and a link is carried verbatim rather
    than resolved: no target is fetched and no local path is inspected."""
    body = (
        f"<w:p>{drawing('anchor')}</w:p>"
        + "<w:p><w:r><w:drawing><wp:anchor>"
        + f"<wp:docPr{DOC_PR}/><a:graphic><a:graphicData>"
        + picture(blip(embed=None, target="rId8"))
        + "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>"
    )
    content = imaged(
        body,
        IMAGE_RELATION,
        ("rId8", "file:///C:/Users/someone/Pictures/Chart.PNG", True, "image"),
    )

    placements = [
        (
            position,
            value["placement"],
            value["imagePart"],
            value["imageLinkTarget"],
            value["imageLinkScheme"],
            value["imageLinkExternal"],
            value["imageLinkRelationshipId"],
        )
        for position, value in inventory(content)
    ]
    assert placements == [
        ("0.i0", "anchor", "word/media/image1.png", None, None, None, None),
        (
            "1.i0",
            "anchor",
            None,
            "file:///C:/Users/someone/Pictures/Chart.PNG",
            "file",
            True,
            "rId8",
        ),
    ]
    assert extract_docx(content, capture_id=CAPTURE).limitations[
        "externalReferences"
    ] == 1


def test_an_image_whose_author_described_it_with_nothing_is_still_inventoried() -> None:
    """A claim nothing wrote and a claim written empty are different facts, and
    neither invents text -- least of all out of the object's name."""
    empty = drawing(doc_pr=DOC_PR + ' title="" descr=""')
    body = (
        f"<w:p>{drawing()}</w:p><w:p>{empty}</w:p>"
        f"<w:p>{drawing(doc_pr=None)}</w:p>"
    )
    content = imaged(body)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", "0.i0"),
        ("object_inventory", "1.i0"),
        ("object_inventory", "2.i0"),
    ]
    assert [
        (value["docPrId"], value["shapeName"], value["altTitle"], value["altDescription"])
        for _, value in inventory(content)
    ] == [
        ("7", "Picture 7", None, None),
        ("7", "Picture 7", "", ""),
        (None, None, None, None),
    ]


def test_images_keep_cell_order_and_a_deletion_over_one_is_preserved() -> None:
    sales = drawing(doc_pr=DOC_PR + ' descr="Sales"')
    gone = drawing(doc_pr=DOC_PR + ' descr="Gone"')
    body = (
        "<w:tbl><w:tr><w:tc>"
        f"<w:p>{run('Figure')}</w:p>"
        f"<w:p>{sales}{run(' caption')}</w:p>"
        f"<w:p><w:del>{gone}</w:del></w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = imaged(body)

    assert [
        (
            i.semantic_role,
            i.text,
            i.source_locator["position"],
            i.revision_state,
            i.visibility,
        )
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("table_cell", "Figure", "0.r0.c0", "current", "visible"),
        # The image closed the cell's run rather than trailing the prose it
        # precedes, exactly as an equation does.
        ("object_inventory", None, "0.r0.c0.1.i0", "current", "visible"),
        ("image_alt", "Sales", "0.r0.c0.1.i0", "current", "visible"),
        ("table_cell", " caption", "0.r0.c0.t1", "current", "visible"),
        ("object_inventory", None, "0.r0.c0.2.i0", "deleted", "annotation"),
        # The deletion is kept over the claim as well as over the record.
        ("image_alt", "Gone", "0.r0.c0.2.i0", "deleted", "annotation"),
    ]


def test_images_are_inventoried_in_every_story_the_walker_reaches() -> None:
    """Headers, notes, content controls and revisions all go through the common
    walker, and each part resolves only its own relationships."""
    body = (
        "<w:sdt><w:sdtContent>"
        + f"<w:p><w:ins>{drawing()}</w:ins></w:p>"
        + "</w:sdtContent></w:sdt>"
    )
    header = f"<w:hdr {W}><w:p>{drawing()}</w:p></w:hdr>"
    note = (
        f'<w:footnotes {W}><w:footnote w:id="2"><w:p>{drawing()}</w:p>'
        "</w:footnote></w:footnotes>"
    )
    content = linked(
        body + HEADER_SECTION,
        IMAGE_RELATION,
        HEADER_RELATION,
        content_types=PNG_DEFAULT + HEADER_OVERRIDE,
        members=IMAGE_MEMBER,
        **{
            "word/header1.xml": header,
            "word/_rels/header1.xml.rels": rels(IMAGE_RELATION),
            "word/footnotes.xml": note,
            "word/_rels/footnotes.xml.rels": rels(IMAGE_RELATION),
        },
    )

    assert [
        (i.semantic_role, i.source_locator["part"], i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", "word/document.xml", "0.i0"),
        ("object_inventory", "word/footnotes.xml", "0.i0"),
        ("object_inventory", "word/header1.xml", "inventory"),
        ("object_inventory", "word/header1.xml", "0.i0"),
    ]


def test_a_part_resolves_only_its_own_relationships() -> None:
    """The main document relating to the image says nothing about a header that
    does not: the id names no relationship there and nothing reaches past it."""
    header = f"<w:hdr {W}><w:p>{drawing()}</w:p></w:hdr>"

    with pytest.raises(OOXMLInvalid, match="names a missing image relationship"):
        extract_docx(
            linked(
                HEADER_SECTION,
                IMAGE_RELATION,
                HEADER_RELATION,
                content_types=PNG_DEFAULT + HEADER_OVERRIDE,
                members=IMAGE_MEMBER,
                **{"word/header1.xml": header},
            ),
            capture_id=CAPTURE,
        )


def test_a_drawing_that_displays_no_image_is_left_for_its_own_slice() -> None:
    """A chart, a diagram or an embedded object under the same anchor is not a
    picture, a picture fill elsewhere below the anchor is not the displayed
    image, and a text box is still the wrapper it has always been."""
    chart = (
        '<a:graphic><a:graphicData uri="http://example.test/chart">'
        '<c:chart xmlns:c="http://example.test/chart" r:id="rId9"/>'
        "</a:graphicData></a:graphic>"
    )
    # A text box shape whose own shape properties carry a picture fill: the
    # fill is not what the anchor displays, and the box still yields its prose.
    shape = (
        "<a:graphic><a:graphicData><wps:wsp>"
        f"<wps:spPr><a:blipFill>{blip()}</a:blipFill></wps:spPr>"
        f"<wps:txbx><w:txbxContent><w:p>{run('inside')}</w:p></w:txbxContent></wps:txbx>"
        "</wps:wsp></a:graphicData></a:graphic>"
    )
    body = (
        f"<w:p>{drawing(graphic=chart)}</w:p>"
        + "<w:p><w:r><w:drawing><wp:anchor>"
        + f"<wp:docPr{DOC_PR}/>{shape}</wp:anchor></w:drawing></w:r></w:p>"
    )

    assert read(imaged(body)) == [("body", "inside", "1.x0.0")]


def test_only_the_schema_qualified_route_reaches_an_image() -> None:
    """An element that shares a local name and nothing else declares nothing,
    whether it stands where the anchor, the graphic, the graphic data, the
    picture or the blip would."""
    other = 'xmlns:z="urn:example:other"'
    lookalikes = (
        (
            f"<z:inline {other}><a:graphic><a:graphicData>"
            f"{picture(blip())}</a:graphicData></a:graphic></z:inline>"
        ),
        (
            f"<wp:inline><wp:docPr{DOC_PR}/><z:graphic {other}><a:graphicData>"
            f"{picture(blip())}</a:graphicData></z:graphic></wp:inline>"
        ),
        (
            f"<wp:inline><wp:docPr{DOC_PR}/><a:graphic><z:graphicData {other}>"
            f"{picture(blip())}</z:graphicData></a:graphic></wp:inline>"
        ),
        (
            f"<wp:inline><wp:docPr{DOC_PR}/><a:graphic><a:graphicData>"
            f"<z:pic {other}><pic:blipFill>{blip()}</pic:blipFill></z:pic>"
            "</a:graphicData></a:graphic></wp:inline>"
        ),
        (
            f"<wp:inline><wp:docPr{DOC_PR}/><a:graphic><a:graphicData><pic:pic>"
            f'<pic:blipFill><z:blip {other} r:embed="rId9"/></pic:blipFill>'
            "</pic:pic></a:graphicData></a:graphic></wp:inline>"
        ),
    )

    for anchor in lookalikes:
        body = f"<w:p><w:r><w:drawing>{anchor}</w:drawing></w:r></w:p>"
        assert inventory(imaged(body)) == []

    # A ``docPr`` lookalike beside the real route claims nothing either, and is
    # no second declaration of the properties the drawing never wrote.
    claimed = (
        f'<wp:inline><z:docPr {other} id="8" descr="Invented"/><a:graphic>'
        f"<a:graphicData>{picture(blip())}</a:graphicData></a:graphic></wp:inline>"
    )
    content = imaged(f"<w:p><w:r><w:drawing>{claimed}</w:drawing></w:r></w:p>")
    assert [
        (value["docPrId"], value["altDescription"]) for _, value in inventory(content)
    ] == [(None, None)]


def test_no_image_byte_is_decoded_and_no_ocr_is_run() -> None:
    """The default profile examines no embedded image, so nothing opens the
    member -- which is declared an image and holds nothing that decodes as one.
    The record still reports the length the package measured when it validated
    the member, and says outright that it read no further and why."""
    content = imaged(f"<w:p>{drawing()}</w:p>")

    _, value = inventory(content)[0]
    assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
    assert value["ocrOutcome"] == EMBEDDED_DISABLED
    assert value["imageBytes"] == len(IMAGE_MEMBER["word/media/image1.png"])
    # Nothing was read out of the bytes: no width, no text, no reading of any
    # kind, and the alt text is the author's claim alone.
    assert set(value) == {
        "kind",
        "placement",
        "docPrId",
        "shapeName",
        "altTitle",
        "altDescription",
        "imageEmbedRelationshipId",
        "imageEmbedRelationshipType",
        "imagePart",
        "imageContentType",
        "imageBytes",
        "imageLinkRelationshipId",
        "imageLinkRelationshipType",
        "imageLinkTarget",
        "imageLinkScheme",
        "imageLinkExternal",
        "imageDecoded",
        "ocrPerformed",
        "ocrOutcome",
    }


def test_an_image_is_the_saved_result_of_the_field_it_sits_under() -> None:
    """A field whose stored result is an image alone stored a result, exactly as
    one whose result is an equation did."""
    body = (
        f"<w:p>{fld('begin')}{instr(' INCLUDEPICTURE ')}{fld('separate')}"
        f"{drawing()}{fld('end')}</w:p>"
    )
    content = imaged(body)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [("object_inventory", "0.f0"), ("object_inventory", "0.i0")]
    # The field stored a result even though no prose was read under it.
    assert inventory(content)[0][1]["savedResultPresent"] is True


def test_an_image_inside_a_field_instruction_is_refused() -> None:
    """An instruction is field code, which carries no image of its own, so one
    reached before the separator is refused rather than inventoried."""
    body = (
        f"<w:p>{fld('begin')}{instr(' INCLUDEPICTURE ')}{drawing()}"
        f"{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds an image"):
        extract_docx(imaged(body), capture_id=CAPTURE)


def test_a_reference_that_names_no_one_image_is_refused() -> None:
    """Each of these leaves which image the drawing displays, or whether it
    displays one at all, unanswerable -- and nothing here guesses."""
    strict = f' xmlns:s="{STRICT_REL_NS}" s:embed="rId9"'
    cases = (
        # An id the holding part relates to nothing names no image at all.
        (drawing(graphic=None).replace('r:embed="rId9"', 'r:embed="rId1"'),
         (IMAGE_RELATION,),
         "names a missing image relationship"),
        # A relationship of another type names something that is not an image.
        (drawing(), (("rId9", "media/image1.png", False, "oleObject"),),
         "image relationship of another type"),
        # Nor does a type reach an image by ending the same way as one.
        (drawing(), (("rId9", "media/image1.png", False, "x/image"),),
         "image relationship of another type"),
        # ``r:embed`` names a part of the package, so an external relationship
        # is not an embed and is not quietly recorded as a link.
        (drawing(), (("rId9", "https://example.invalid/logo.png", True, "image"),),
         "embedded image relationship is external"),
        # A drawing displays one image, so a second blip below it is ambiguous.
        (
            (
                f"<w:r><w:drawing><wp:inline><wp:docPr{DOC_PR}/><a:graphic>"
                f"<a:graphicData><pic:pic><pic:blipFill>{blip()}{blip()}"
                "</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
                "</wp:inline></w:drawing></w:r>"
            ),
            (IMAGE_RELATION,),
            "declares more than one image reference",
        ),
        # So is one attribute written in both relationships namespaces at once.
        (
            (
                f"<w:r><w:drawing><wp:inline><wp:docPr{DOC_PR}/><a:graphic>"
                f"<a:graphicData>{picture(blip(extra=strict))}</a:graphicData>"
                "</a:graphic></wp:inline></w:drawing></w:r>"
            ),
            (IMAGE_RELATION,),
            "names more than one image relationship",
        ),
        # A blip declaring neither attribute identifies no image.
        (
            (
                f"<w:r><w:drawing><wp:inline><wp:docPr{DOC_PR}/><a:graphic>"
                f"<a:graphicData>{picture(blip(embed=None))}</a:graphicData>"
                "</a:graphic></wp:inline></w:drawing></w:r>"
            ),
            (IMAGE_RELATION,),
            "names no image relationship",
        ),
        # Two ``wp:docPr`` leave the object's identity and its alt text
        # ambiguous, so nothing picks one of them.
        (
            (
                f"<w:r><w:drawing><wp:inline><wp:docPr{DOC_PR}/>"
                f'<wp:docPr id="8" name="Picture 8" descr="Other"/><a:graphic>'
                f"<a:graphicData>{picture(blip())}</a:graphicData></a:graphic>"
                "</wp:inline></w:drawing></w:r>"
            ),
            (IMAGE_RELATION,),
            "declares its properties twice",
        ),
    )

    for body, relations, message in cases:
        with pytest.raises(OOXMLInvalid, match=message):
            extract_docx(imaged(f"<w:p>{body}</w:p>", *relations), capture_id=CAPTURE)


def test_an_embedded_part_the_package_does_not_declare_an_image_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="not declared an image"):
        extract_docx(
            linked(
                f"<w:p>{drawing()}</w:p>",
                IMAGE_RELATION,
                content_types='<Default Extension="png" ContentType="text/plain"/>',
                members=IMAGE_MEMBER,
            ),
            capture_id=CAPTURE,
        )


def test_an_internal_image_target_the_package_does_not_hold_is_refused() -> None:
    """Containment and existence are settled when the package validates its
    relationships, so an image part that is not there never reaches a record."""
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_docx(
            linked(
                f"<w:p>{drawing()}</w:p>",
                IMAGE_RELATION,
                content_types=PNG_DEFAULT,
            ),
            capture_id=CAPTURE,
        )


# A chart part the package declares, reached by the one route a drawing names
# its chart on: the object's own ``a:graphic``, its direct ``a:graphicData``,
# and the ``c:chart`` below that. The values the part holds are the copy it
# already stored -- nothing here opens the workbook behind them.
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
CHART_PART = "word/charts/chart1.xml"
CHART_RELATION = ("rId7", "charts/chart1.xml", False, "chart")
CHART_REFERENCE = (
    "<a:graphic><a:graphicData>"
    f'<c:chart xmlns:c="{CHART_NS}" r:id="rId7"/>'
    "</a:graphicData></a:graphic>"
)
RICH_TITLE = (
    "<c:title><c:tx><c:rich><a:p>"
    "<a:r><a:t>Quarterly</a:t></a:r><a:r><a:t> revenue</a:t></a:r>"
    "</a:p></c:rich></c:tx></c:title>"
)
# One series, each slot holding a source formula and the single point it cached.
PLOTTED = (
    "<c:plotArea><c:barChart><c:ser>"
    '<c:idx val="0"/><c:order val="0"/>'
    '<c:tx><c:strRef><c:f>Sheet1!$B$1</c:f><c:strCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>Revenue</c:v></c:pt></c:strCache></c:strRef></c:tx>'
    '<c:cat><c:strRef><c:f>Sheet1!$A$2</c:f><c:strCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>Q1</c:v></c:pt></c:strCache></c:strRef></c:cat>'
    '<c:val><c:numRef><c:f>Sheet1!$B$2</c:f><c:numCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>12</c:v></c:pt></c:numCache></c:numRef></c:val>'
    "</c:ser></c:barChart></c:plotArea>"
)
# The provenance the title record carries. Every other record differs from it
# only in ``chartRole`` and the place inside the chart.
CHART_LOCATOR: dict[str, object] = {
    "captureId": CAPTURE,
    "part": "word/document.xml",
    "story": "body",
    "position": "0.c0",
    "chartPart": CHART_PART,
    "relationshipId": "rId7",
    "seriesIndex": None,
    "seriesOrder": None,
    "categoryLevel": None,
    "pointIndex": None,
    "formula": None,
    "chartRole": "chart_title",
    "freshness": "unknown",
}


CHART_DRAWING = drawing(graphic=CHART_REFERENCE)


def charted(chart: str, body: str | None = None) -> bytes:
    """A document whose one drawing names the part holding ``chart``.

    ``body`` defaults to that drawing alone, inline in a paragraph of its own; a
    test that places the same drawing elsewhere passes the body it wants.
    """
    space = (
        f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}">'
        f"<c:chart>{chart}</c:chart></c:chartSpace>"
    )
    return linked(
        f"<w:p>{CHART_DRAWING}</w:p>" if body is None else body,
        CHART_RELATION,
        content_types=f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>',
        **{CHART_PART: space},
    )


def test_a_chart_is_inventoried_where_it_sits_with_its_stored_values_after() -> None:
    """The record comes first, then the title and the points the part stored, in
    source order. Each value names the chart part and the relationship that
    reached it, and each is ``unknown`` freshness: nothing was refreshed."""
    content = charted(RICH_TITLE + PLOTTED)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", None, "0.c0"),
        ("chart", "Quarterly revenue", "0.c0"),
        ("chart", "Revenue", "0.c0"),
        ("chart", "Q1", "0.c0"),
        ("chart", "12", "0.c0"),
    ]
    assert inventory(content) == [
        (
            "0.c0",
            {
                "kind": "drawingChart",
                "placement": "inline",
                "relationshipId": "rId7",
                "chartPart": CHART_PART,
                "refreshed": False,
                "embeddedWorkbookOpened": False,
                "storedRecords": 4,
            },
        )
    ]
    title, name, category, value = result.segments[1:]
    assert title.source_locator == CHART_LOCATOR
    assert [dict(item.source_locator) for item in (name, category, value)] == [
        CHART_LOCATOR
        | {
            "chartRole": role,
            "seriesIndex": "0",
            "seriesOrder": "0",
            "pointIndex": "0",
            "formula": formula,
        }
        for role, formula in (
            ("chart_series_name", "Sheet1!$B$1"),
            ("chart_category", "Sheet1!$A$2"),
            ("chart_value", "Sheet1!$B$2"),
        )
    ]


def test_a_chart_that_stored_nothing_is_still_inventoried_where_it_sits() -> None:
    """A chart part with no title and no series stored no value to report, and
    the object is still the one the story anchored, so the record stands alone
    and says outright that it holds nothing."""
    content = charted("")

    result = extract_docx(content, capture_id=CAPTURE)

    assert inventory(content) == [
        (
            "0.c0",
            {
                "kind": "drawingChart",
                "placement": "inline",
                "relationshipId": "rId7",
                "chartPart": CHART_PART,
                "refreshed": False,
                "embeddedWorkbookOpened": False,
                "storedRecords": 0,
            },
        )
    ]
    assert [item.semantic_role for item in result.segments] == ["object_inventory"]


def test_a_chart_splits_a_cell_rather_than_joining_the_prose_around_it() -> None:
    """The chart is ordered where it sits, so it closes the cell's open run
    rather than trailing the prose it interrupts: the text before it is reported
    first, the record and the stored values next, and the text after it starts a
    run of its own. Every locator is the position the XML put the value at, and
    the four records of one chart are told apart by the place inside it."""
    body = (
        "<w:tbl><w:tr><w:tc>"
        f"<w:p>{run('Figure')}{CHART_DRAWING}{run(' caption')}</w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = charted(RICH_TITLE + PLOTTED, body)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("table_cell", "Figure", "0.r0.c0"),
        ("object_inventory", None, "0.r0.c0.0.c0"),
        ("chart", "Quarterly revenue", "0.r0.c0.0.c0"),
        ("chart", "Revenue", "0.r0.c0.0.c0"),
        ("chart", "Q1", "0.r0.c0.0.c0"),
        ("chart", "12", "0.r0.c0.0.c0"),
        ("table_cell", " caption", "0.r0.c0.t1"),
    ]
    assert result.segments[2].source_locator == CHART_LOCATOR | {
        "position": "0.r0.c0.0.c0"
    }
    locators = [tuple(sorted(i.source_locator.items())) for i in result.segments]
    assert len(set(locators)) == len(locators)


def test_an_anchored_chart_is_recorded_as_the_object_declared_it() -> None:
    """Placement is the drawing element's own, so a chart anchored against the
    page is recorded as anchored rather than as part of the text flow."""
    anchored = f"<w:p>{drawing('anchor', graphic=CHART_REFERENCE)}</w:p>"

    assert inventory(charted(RICH_TITLE, anchored)) == [
        (
            "0.c0",
            {
                "kind": "drawingChart",
                "placement": "anchor",
                "relationshipId": "rId7",
                "chartPart": CHART_PART,
                "refreshed": False,
                "embeddedWorkbookOpened": False,
                "storedRecords": 1,
            },
        )
    ]


def test_a_deletion_over_a_chart_is_kept_over_its_record_and_its_values() -> None:
    """A deletion wrapped around the drawing is the revision it is, over the
    inventory record and over every value the chart part stored, exactly as it
    is over an image and the claims its author made about it."""
    deleted = f"<w:p><w:del>{CHART_DRAWING}</w:del></w:p>"

    result = extract_docx(charted(RICH_TITLE + PLOTTED, deleted), capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.visibility, i.revision_state) for i in result.segments
    ] == [("object_inventory", "annotation", "deleted")] + [
        ("chart", "annotation", "deleted")
    ] * 4


def test_a_chart_is_the_saved_result_of_the_field_it_sits_under() -> None:
    """A field whose stored result is a chart alone stored a result, exactly as
    one whose result is an image did. Before the separator the same chart is
    field code, which carries no chart of its own, so it is refused rather than
    inventoried as the field's result."""
    saved = (
        f"<w:p>{fld('begin')}{instr(' CHART ')}{fld('separate')}"
        f"{CHART_DRAWING}{fld('end')}</w:p>"
    )
    content = charted(RICH_TITLE, saved)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", "0.f0"),
        ("object_inventory", "0.c0"),
        ("chart", "0.c0"),
    ]
    assert inventory(content)[0][1]["savedResultPresent"] is True

    code = (
        f"<w:p>{fld('begin')}{instr(' CHART ')}{CHART_DRAWING}"
        f"{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds a chart"):
        extract_docx(charted(RICH_TITLE, code), capture_id=CAPTURE)


# The ``c:chart`` alone, for the drawings below that put a reference somewhere
# other than the single place ``CHART_REFERENCE`` puts it.
CHART_CHILD = f'<c:chart xmlns:c="{CHART_NS}" r:id="rId7"/>'
# The Strict half of each pair the route is held to. A reference written in
# these is the same declaration as one written in the Transitional pair, and a
# namespace that merely resembles either is another vocabulary.
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
STRICT_CHART_NS = "http://purl.oclc.org/ooxml/drawingml/chart"
STRICT_CHART_CHILD = f'<k:chart xmlns:k="{STRICT_CHART_NS}" r:id="rId7"/>'


def graphed(*children: str) -> str:
    """One ``a:graphic`` whose single ``a:graphicData`` holds ``children``."""
    return f"<a:graphic><a:graphicData>{''.join(children)}</a:graphicData></a:graphic>"


def test_only_the_schema_qualified_route_reaches_a_chart() -> None:
    """An element that shares a local name and nothing else declares no chart,
    whether it stands where the graphic, its data or the reference would, and a
    namespace that merely resembles one OOXML defines -- a character longer, a
    slash longer, one holding the real URI inside it -- is another vocabulary.
    A drawing that declares nothing is the transparent wrapper it has always
    been, so the prose beside it is still read and no chart is inventoried off
    it. The ``uri`` a graphic data declares is never read, so it names no chart
    either."""
    other = 'xmlns:z="urn:example:other"'
    lookalikes = (
        # The graphic, its data and the reference itself, each in turn.
        (
            f"<z:graphic {other}><a:graphicData>{CHART_CHILD}"
            "</a:graphicData></z:graphic>"
        ),
        (
            f"<a:graphic><z:graphicData {other}>{CHART_CHILD}"
            "</z:graphicData></a:graphic>"
        ),
        graphed(f'<z:chart {other} r:id="rId7"/>'),
        # A graphic data written in the chart namespace is not the graphic data
        # the route names, so nothing below it is on the route either.
        (
            f'<a:graphic><c:graphicData xmlns:c="{CHART_NS}">{CHART_CHILD}'
            "</c:graphicData></a:graphic>"
        ),
        # Namespaces compare as strings, so each of these is a chart element of
        # some other vocabulary that happens to read like the one OOXML defines.
        graphed(f'<c:chart xmlns:c="{CHART_NS}2" r:id="rId7"/>'),
        graphed(f'<c:chart xmlns:c="{CHART_NS}/" r:id="rId7"/>'),
        graphed(f'<c:chart xmlns:c="urn:example:{CHART_NS}" r:id="rId7"/>'),
        # The declared ``uri`` says chart and the element below it does not.
        (
            f'<a:graphic><a:graphicData uri="{CHART_NS}">'
            f'<z:chart {other} r:id="rId7"/></a:graphicData></a:graphic>'
        ),
    )

    for graphic in lookalikes:
        content = charted(
            RICH_TITLE + PLOTTED,
            p("Prose") + f"<w:p>{drawing(graphic=graphic)}</w:p>",
        )

        assert read(content) == [("body", "Prose", "0")]

    # Each step's Strict half is the same declaration as its Transitional one,
    # so the route above is refused for its namespaces and not for its shape.
    strict = (
        f'<s:graphic xmlns:s="{STRICT_DRAWING_NS}"><s:graphicData>'
        f"{STRICT_CHART_CHILD}</s:graphicData></s:graphic>"
    )
    whole = charted(RICH_TITLE, f"<w:p>{drawing(graphic=strict)}</w:p>")

    assert [
        (i.semantic_role, i.text)
        for i in extract_docx(whole, capture_id=CAPTURE).segments
    ] == [("object_inventory", None), ("chart", "Quarterly revenue")]


def test_a_drawing_that_names_more_than_one_chart_is_refused() -> None:
    """One drawing object displays one graphic, so two schema-valid references
    below it leave which chart it shows unanswerable: under one graphic data,
    under two of them, under two graphics, or written one in each of the two
    namespaces OOXML defines while naming the one relationship. Nothing here
    chooses between them, and naming the same chart twice is still twice."""
    ambiguous = (
        graphed(CHART_CHILD, CHART_CHILD),
        (
            f"<a:graphic><a:graphicData>{CHART_CHILD}</a:graphicData>"
            f"<a:graphicData>{CHART_CHILD}</a:graphicData></a:graphic>"
        ),
        CHART_REFERENCE + CHART_REFERENCE,
        graphed(CHART_CHILD, STRICT_CHART_CHILD),
    )

    for graphic in ambiguous:
        with pytest.raises(
            OOXMLInvalid, match="declares more than one chart reference"
        ):
            extract_docx(
                charted(RICH_TITLE, f"<w:p>{drawing(graphic=graphic)}</w:p>"),
                capture_id=CAPTURE,
            )

    # A reference off that route is no second declaration: one below the
    # drawing but under no graphic, and one a step deeper than the graphic
    # data, are each passed over, so the chart the object does declare stays
    # the single chart it shows.
    for graphic in (
        CHART_CHILD + CHART_REFERENCE,
        graphed(f"<a:ext>{CHART_CHILD}</a:ext>", CHART_CHILD),
    ):
        content = charted(RICH_TITLE, f"<w:p>{drawing(graphic=graphic)}</w:p>")

        assert [
            i.semantic_role for i in extract_docx(content, capture_id=CAPTURE).segments
        ] == ["object_inventory", "chart"]


def mixed(graphic: str) -> bytes:
    """A package holding both the image a drawing may declare and the chart part
    it may name, so either reference below one drawing resolves on its own."""
    space = (
        f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}">'
        f"<c:chart>{RICH_TITLE}</c:chart></c:chartSpace>"
    )
    return linked(
        f"<w:p>{drawing(graphic=graphic)}</w:p>",
        CHART_RELATION,
        IMAGE_RELATION,
        content_types=(
            f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>'
            f"{PNG_DEFAULT}"
        ),
        members=IMAGE_MEMBER,
        **{CHART_PART: space},
    )


def test_a_drawing_that_declares_both_an_image_and_a_chart_is_refused() -> None:
    """A displayed image and a chart are different declarations on the one
    route, so an object making both leaves what it shows unanswerable: neither
    which came first in the XML nor which graphic data each sits under decides
    it. Both halves resolve here -- the package declares the image part and
    holds the chart part -- so what is refused is the pair and not either one."""
    both = (
        graphed(picture(blip()), CHART_CHILD),
        graphed(CHART_CHILD, picture(blip())),
        (
            f"<a:graphic><a:graphicData>{picture(blip())}</a:graphicData>"
            f"<a:graphicData>{CHART_CHILD}</a:graphicData></a:graphic>"
        ),
    )

    for graphic in both:
        with pytest.raises(OOXMLInvalid, match="declares both an image and a chart"):
            extract_docx(mixed(graphic), capture_id=CAPTURE)

    assert [
        i.semantic_role
        for i in extract_docx(mixed(CHART_REFERENCE), capture_id=CAPTURE).segments
    ] == ["object_inventory", "chart"]
    assert [
        i.semantic_role
        for i in extract_docx(
            mixed(graphed(picture(blip()))), capture_id=CAPTURE
        ).segments
    ] == ["object_inventory"]


# The whole package a schema-valid reference is read through: the chart space,
# and the declaration that makes the part a chart part. Each test below leaves
# the reference exactly as the drawing writes it and changes one step of the
# package around it.
CHART_SPACE = (
    f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}">'
    f"<c:chart>{RICH_TITLE}</c:chart></c:chartSpace>"
)
CHART_OVERRIDE = f'<Override PartName="/{CHART_PART}" ContentType="{CHART_CT}"/>'


def related(
    *relations: tuple[str, str, bool, str],
    content_types: str = CHART_OVERRIDE,
    members: dict[str, bytes] | None = None,
    **parts: str,
) -> bytes:
    """The one inline charted paragraph, over the package given around it."""
    return linked(
        f"<w:p>{CHART_DRAWING}</w:p>",
        *relations,
        content_types=content_types,
        members=members,
        **parts,
    )


def test_a_chart_reference_that_reaches_no_chart_relationship_is_refused() -> None:
    """The id is resolved through the holding part's own relationships or not at
    all: no chart path is derived from the story's name, so a chart part sitting
    in the package unrelated is never read. The relationship has to be there, it
    has to carry one of the URIs OOXML defines for a chart, and it has to be
    internal -- ``r:id`` names a part of the package, so an external target is
    no chart and is not quietly recorded as a link. Every refusal names the
    document the reference came from, not whichever format reached the shared
    reader first."""
    cases = (
        ((), "chart reference names a missing relationship"),
        # The package relates a chart, under an id this drawing does not name.
        (
            (("rId1", "charts/chart1.xml", False, "chart"),),
            "chart reference names a missing relationship",
        ),
        # A relationship of another type names something that is not a chart,
        # and no type reaches one by ending the same way as one.
        (
            (("rId7", "charts/chart1.xml", False, "oleObject"),),
            "chart reference names a relationship of another type",
        ),
        (
            (("rId7", "charts/chart1.xml", False, "x/chart"),),
            "chart reference names a relationship of another type",
        ),
        (
            (("rId7", "https://example.invalid/chart1.xml", True, "chart"),),
            "chart relationship is external",
        ),
    )

    for relations, message in cases:
        with pytest.raises(OOXMLInvalid, match=f"DOCX {message}"):
            extract_docx(
                related(*relations, **{CHART_PART: CHART_SPACE}), capture_id=CAPTURE
            )


def test_a_chart_part_the_package_does_not_hold_or_declare_is_refused() -> None:
    """Containment and existence are settled when the package validates its
    relationships, so a chart part that is not there never reaches a record.
    The target also has to be declared a chart part rather than merely named
    one: a part the package declares as something else, or declares as nothing
    at all, qualifies as no chart."""
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_docx(related(CHART_RELATION), capture_id=CAPTURE)

    styles = (
        f'<Override PartName="/{CHART_PART}" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    )
    for content_types in ("", styles):
        with pytest.raises(
            OOXMLInvalid, match="DOCX chart relationship does not name a chart part"
        ):
            extract_docx(
                related(
                    CHART_RELATION,
                    content_types=content_types,
                    **{CHART_PART: CHART_SPACE},
                ),
                capture_id=CAPTURE,
            )


def test_a_chart_part_rooted_in_another_namespace_is_refused() -> None:
    """The declared content type says what the part is; the root element is
    where the part says so itself. A root that merely reads like a chart space
    -- a namespace a character longer, one holding the real URI inside it, the
    drawing namespace the space's own prefixes bind -- is another vocabulary,
    and the reference that reached it stays the schema-valid reference it was.
    The Strict namespace is the same declaration as the Transitional one, so it
    is read, whichever half the reference naming it was written in."""
    for namespace in (f"{CHART_NS}2", f"urn:example:{CHART_NS}", DRAWING_NS, "urn:c"):
        space = CHART_SPACE.replace(f'xmlns:c="{CHART_NS}"', f'xmlns:c="{namespace}"')

        with pytest.raises(
            OOXMLInvalid, match="DOCX chart part uses an unsupported namespace"
        ):
            extract_docx(
                related(CHART_RELATION, **{CHART_PART: space}), capture_id=CAPTURE
            )

    strict = CHART_SPACE.replace(CHART_NS, STRICT_CHART_NS)

    assert [
        (i.semantic_role, i.text)
        for i in extract_docx(
            related(CHART_RELATION, **{CHART_PART: strict}), capture_id=CAPTURE
        ).segments
    ] == [("object_inventory", None), ("chart", "Quarterly revenue")]


# The workbook a chart may keep its data in: the member, the relationship the
# chart part itself holds to it, and the ``c:externalData`` the chart space
# names that relationship on. The member is declared nothing and is not a
# workbook at all, so a reader that opened it would fail rather than pass.
WORKBOOK_MEMBER = {"word/embeddings/book.xlsx": b"not a workbook anything here reads"}
WORKBOOK_RELATION = ("rId8", "../embeddings/book.xlsx", False, "package")
EXTERNAL_DATA = '<c:externalData r:id="rId8"><c:autoUpdate val="1"/></c:externalData>'


def test_an_embedded_workbook_behind_a_chart_is_never_opened() -> None:
    """The chart's own package relationships are not followed to its data: the
    workbook's bytes are never opened or interpreted, nothing is refreshed from
    them and no value is recalculated, however insistently ``c:autoUpdate``
    asks. What is reported is the copy the chart part had already cached, which
    reads exactly as it does in a package embedding no workbook at all, and the
    record says outright that the workbook stayed shut."""
    space = (
        f'<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="{DRAWING_NS}" '
        f'xmlns:r="{REL.rstrip("/")}">'
        f"<c:chart>{RICH_TITLE}{PLOTTED}</c:chart>{EXTERNAL_DATA}</c:chartSpace>"
    )
    content = related(
        CHART_RELATION,
        members=WORKBOOK_MEMBER,
        **{
            CHART_PART: space,
            "word/charts/_rels/chart1.xml.rels": rels(WORKBOOK_RELATION),
        },
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert (
        result.segments
        == extract_docx(charted(RICH_TITLE + PLOTTED), capture_id=CAPTURE).segments
    )
    assert [(i.semantic_role, i.text) for i in result.segments] == [
        ("object_inventory", None),
        ("chart", "Quarterly revenue"),
        ("chart", "Revenue"),
        ("chart", "Q1"),
        ("chart", "12"),
    ]
    assert inventory(content) == [
        (
            "0.c0",
            {
                "kind": "drawingChart",
                "placement": "inline",
                "relationshipId": "rId7",
                "chartPart": CHART_PART,
                "refreshed": False,
                "embeddedWorkbookOpened": False,
                "storedRecords": 4,
            },
        )
    ]


# A drawing object declares SmartArt on the one route a slide's graphic frame
# does: the object's own ``a:graphic``, the ``a:graphicData`` below it carrying
# a diagram namespace as its ``uri``, and the ``dgm:relIds`` naming the data
# model part. What is read off the data model is the copy it already stored --
# each point's own ``dgm:t`` and the identity each connection encodes -- and
# the layout, quick-style and colour ids named beside the data reach nothing.
DIAGRAM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
DIAGRAM_CT = "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml"
DIAGRAM_PART = "word/diagrams/data1.xml"
DIAGRAM_RELATION = ("rId4", "diagrams/data1.xml", False, "diagramData")
DIAGRAM_OVERRIDE = f'<Override PartName="/{DIAGRAM_PART}" ContentType="{DIAGRAM_CT}"/>'
# ``r:lo``, ``r:qs`` and ``r:cs`` name no relationship the package declares at
# all, so a reader that opened any of the three would refuse rather than pass.
RELIDS = (
    f'<dgm:relIds xmlns:dgm="{DIAGRAM_NS}"'
    ' r:dm="rId4" r:lo="rId20" r:qs="rId21" r:cs="rId22"/>'
)


def dgm_data(*children: str) -> str:
    """One ``a:graphicData`` whose ``uri`` declares its graphic SmartArt."""
    return f'<a:graphicData uri="{DIAGRAM_NS}">{"".join(children)}</a:graphicData>'


SMART_REFERENCE = f"<a:graphic>{dgm_data(RELIDS)}</a:graphic>"
SMART_DRAWING = drawing(graphic=SMART_REFERENCE)


def point(model_id: str, *texts: str) -> str:
    """One ``dgm:pt``, carrying a text body only where it stored a run."""
    stored = "".join(f"<a:r><a:t>{body}</a:t></a:r>" for body in texts)
    return (
        f'<dgm:pt modelId="{model_id}">'
        + (f"<dgm:t><a:p>{stored}</a:p></dgm:t>" if texts else "")
        + "</dgm:pt>"
    )


# Two points carrying one run of text each, and the one connection between
# them. Nothing in the model says which point is above the other: that is the
# layout's business, and the layout is never opened.
POINTS = point("1", "First") + point("2", "Second")
CONNECTION = '<dgm:cxn modelId="3" srcId="1" destId="2" srcOrd="0" destOrd="0"/>'


def model(points: str = POINTS, connections: str = CONNECTION) -> str:
    """One diagram data part holding the point list and the connection list."""
    return (
        f'<dgm:dataModel xmlns:dgm="{DIAGRAM_NS}" xmlns:a="{DRAWING_NS}">'
        f"<dgm:ptLst>{points}</dgm:ptLst>"
        f"<dgm:cxnLst>{connections}</dgm:cxnLst>"
        "</dgm:dataModel>"
    )


def smart(body: str | None = None, data: str | None = None) -> bytes:
    """A document whose one drawing names the part holding ``data``.

    ``body`` defaults to that drawing alone, inline in a paragraph of its own; a
    test that places the same drawing elsewhere passes the body it wants.
    """
    return linked(
        f"<w:p>{SMART_DRAWING}</w:p>" if body is None else body,
        DIAGRAM_RELATION,
        content_types=DIAGRAM_OVERRIDE,
        **{DIAGRAM_PART: model() if data is None else data},
    )


# The provenance the first point-text record carries. Every other record
# differs from it only in where inside the diagram it sits.
SMART_LOCATOR: dict[str, object] = {
    "captureId": CAPTURE,
    "part": "word/document.xml",
    "story": "body",
    "position": "0.d0",
    "diagramPart": DIAGRAM_PART,
    "relationshipId": "rId4",
    "diagramRole": "point_text",
    "modelId": "1",
    "pointType": "node",
    "pointIndex": 0,
    "connectionModelId": None,
    "paragraph": 0,
    "run": 0,
}
# A connection sits at no point, paragraph or run, so none of those keys is on
# its locator: what it carries is the identity it encodes and nothing more.
SMART_CONNECTED: dict[str, object] = {
    key: value
    for key, value in SMART_LOCATOR.items()
    if key
    not in {
        "diagramRole",
        "modelId",
        "pointType",
        "pointIndex",
        "connectionModelId",
        "paragraph",
        "run",
    }
} | {"diagramRole": "connection", "modelId": "3", "connectionIndex": 0}
# The record the inventory segment carries, and the one the connection does.
SMART_RECORD: dict[str, object] = {
    "kind": "smartArt",
    "placement": "inline",
    "relationshipId": "rId4",
    "diagramPart": DIAGRAM_PART,
    "storedPointTextRecords": 2,
    "storedConnections": 1,
    "hierarchyInferred": False,
    "relatedVisualPartsOpened": False,
}
SMART_CONNECTION: dict[str, object] = {
    "kind": "diagramConnection",
    "modelId": "3",
    "connectionIndex": 0,
    "connectionType": "parOf",
    "sourceModelId": "1",
    "destinationModelId": "2",
    "sourceOrder": "0",
    "destinationOrder": "0",
    "parentTransitionModelId": None,
    "siblingTransitionModelId": None,
    "sourceResolved": True,
    "destinationResolved": True,
}


def test_smartart_is_inventoried_where_it_sits_with_its_stored_text_after() -> None:
    """The record comes first and says what was and was not done: where the
    object sits, the relationship it named its data on, the part that reached,
    how much the part had stored, and that no hierarchy was inferred and no
    related visual part opened. The stored point text follows run by run under
    the ``body`` role, then each connection as the inert identity it encodes."""
    content = smart()

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", None, "0.d0"),
        ("body", "First", "0.d0"),
        ("body", "Second", "0.d0"),
        ("object_inventory", None, "0.d0"),
    ]
    assert inventory(content) == [("0.d0", SMART_RECORD), ("0.d0", SMART_CONNECTION)]
    record, first, second, link = result.segments
    assert dict(record.source_locator) == {
        key: SMART_LOCATOR[key] for key in ("captureId", "part", "story", "position")
    }
    assert (first.semantic_role, first.visibility, first.revision_state) == (
        "body",
        "visible",
        "current",
    )
    assert dict(first.source_locator) == SMART_LOCATOR
    assert dict(second.source_locator) == SMART_LOCATOR | {
        "modelId": "2",
        "pointIndex": 1,
    }
    assert (link.semantic_role, link.extraction_method, link.text) == (
        "object_inventory",
        "inventory",
        None,
    )
    assert dict(link.source_locator) == SMART_CONNECTED
    locators = [tuple(sorted(i.source_locator.items())) for i in result.segments]
    assert len(set(locators)) == len(locators)


def test_smartart_whose_points_stored_no_text_is_still_inventoried() -> None:
    """A data model may define its points and store not one run of text in them.
    The object is still the one the story anchored and the connections are still
    the identities they encode, so the record stands and says outright that no
    point text was stored."""
    content = smart(data=model(point("1") + point("2")))

    result = extract_docx(content, capture_id=CAPTURE)

    assert inventory(content) == [
        ("0.d0", SMART_RECORD | {"storedPointTextRecords": 0}),
        ("0.d0", SMART_CONNECTION),
    ]
    assert [i.semantic_role for i in result.segments] == [
        "object_inventory",
        "object_inventory",
    ]


def test_anchored_smartart_is_recorded_as_the_object_declared_it() -> None:
    """Placement is the drawing element's own, so a diagram anchored against the
    page is recorded as anchored rather than as part of the text flow."""
    anchored = f"<w:p>{drawing('anchor', graphic=SMART_REFERENCE)}</w:p>"

    assert inventory(smart(anchored)) == [
        ("0.d0", SMART_RECORD | {"placement": "anchor"}),
        ("0.d0", SMART_CONNECTION),
    ]


def test_a_deletion_over_smartart_is_kept_over_its_record_text_and_links() -> None:
    """A deletion wrapped around the drawing is the revision it is, over the
    inventory record, over every run of point text the data model stored and
    over every connection it encoded, exactly as it is over a chart and the
    values it cached."""
    deleted = f"<w:p><w:del>{SMART_DRAWING}</w:del></w:p>"

    result = extract_docx(smart(deleted), capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.visibility, i.revision_state) for i in result.segments
    ] == [
        ("object_inventory", "annotation", "deleted"),
        ("body", "annotation", "deleted"),
        ("body", "annotation", "deleted"),
        ("object_inventory", "annotation", "deleted"),
    ]


def test_smartart_is_the_saved_result_of_the_field_it_sits_under() -> None:
    """A field whose stored result is a diagram alone stored a result, exactly as
    one whose result is an image or a chart did. Before the separator the same
    drawing is field code, which carries no diagram of its own, so it is refused
    rather than inventoried as the field's result."""
    saved = (
        f"<w:p>{fld('begin')}{instr(' SMARTART ')}{fld('separate')}"
        f"{SMART_DRAWING}{fld('end')}</w:p>"
    )
    content = smart(saved)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("object_inventory", "0.f0"),
        ("object_inventory", "0.d0"),
        ("body", "0.d0"),
        ("body", "0.d0"),
        ("object_inventory", "0.d0"),
    ]
    assert inventory(content)[0][1]["savedResultPresent"] is True

    code = (
        f"<w:p>{fld('begin')}{instr(' SMARTART ')}{SMART_DRAWING}"
        f"{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds SmartArt"):
        extract_docx(smart(code), capture_id=CAPTURE)


def declared(graphic: str) -> bytes:
    """A package where the image, the chart and the diagram a drawing may name
    all resolve, so what a pairing below one drawing is refused for is the pair
    and not either half of it."""
    return linked(
        f"<w:p>{drawing(graphic=graphic)}</w:p>",
        CHART_RELATION,
        IMAGE_RELATION,
        DIAGRAM_RELATION,
        content_types=f"{CHART_OVERRIDE}{PNG_DEFAULT}{DIAGRAM_OVERRIDE}",
        members=IMAGE_MEMBER,
        **{CHART_PART: CHART_SPACE, DIAGRAM_PART: model()},
    )


def test_a_drawing_declaring_smartart_and_another_graphic_is_refused() -> None:
    """A displayed image, a chart and SmartArt are three declarations on the one
    route, so an object making two of them leaves what it shows unanswerable:
    neither which came first in the XML nor whether the two share a graphic data
    decides it. Every half resolves in this package, so what is refused is the
    pair, named by the two declarations it made."""
    for graphic, refused in (
        (f"<a:graphic>{dgm_data(RELIDS, picture(blip()))}</a:graphic>", "an image"),
        (
            (
                f"<a:graphic><a:graphicData>{picture(blip())}</a:graphicData>"
                f"{dgm_data(RELIDS)}</a:graphic>"
            ),
            "an image",
        ),
        (f"<a:graphic>{dgm_data(RELIDS, CHART_CHILD)}</a:graphic>", "a chart"),
        (
            (
                f"<a:graphic><a:graphicData>{CHART_CHILD}</a:graphicData>"
                f"{dgm_data(RELIDS)}</a:graphic>"
            ),
            "a chart",
        ),
    ):
        with pytest.raises(OOXMLInvalid, match=f"declares both {refused} and SmartArt"):
            extract_docx(declared(graphic), capture_id=CAPTURE)

    # Each half alone is the single graphic the object shows, read on its own
    # route through the very same package.
    assert [
        i.semantic_role
        for i in extract_docx(declared(SMART_REFERENCE), capture_id=CAPTURE).segments
    ] == ["object_inventory", "body", "body", "object_inventory"]
    assert [
        i.semantic_role
        for i in extract_docx(declared(CHART_REFERENCE), capture_id=CAPTURE).segments
    ] == ["object_inventory", "chart"]
    assert [
        i.semantic_role
        for i in extract_docx(
            declared(graphed(picture(blip()))), capture_id=CAPTURE
        ).segments
    ] == ["object_inventory"]


# The route a drawing object declares SmartArt on is the one a slide's graphic
# frame declares it on, so it is held to the same pair of namespaces at every
# step: the Transitional and the Strict halves of DrawingML main, of the diagram
# vocabulary, and of the relationships namespace an id is written in. The tests
# below leave the package whole -- the data part is declared and present, and
# the relationship reaches it -- and vary one step of the route itself.
STRICT_NS = "http://purl.oclc.org/ooxml/drawingml/diagram"
REL_NS = REL.removesuffix("/")


def routed(
    body: str,
    *,
    uri: str | None = DIAGRAM_NS,
    namespace: str = DRAWING_NS,
) -> str:
    """The ``a:graphic``/``a:graphicData`` route, each step's namespace its own."""
    attribute = "" if uri is None else f' uri="{uri}"'
    return (
        f'<a:graphic xmlns:a="{namespace}">'
        f"<a:graphicData{attribute}>{body}</a:graphicData></a:graphic>"
    )


def relids(
    *,
    dm: str | None = "rId4",
    namespace: str = DIAGRAM_NS,
    relationships: str = REL_NS,
    extra: str = "",
) -> str:
    """One ``dgm:relIds``, always naming the layout, style and colour parts too.

    Those three are the ids nothing here opens, and the package declares no
    relationship for any of them, so a reader that followed one would refuse
    rather than pass.
    """
    named = "" if dm is None else f' r:dm="{dm}"'
    return (
        f'<dgm:relIds xmlns:dgm="{namespace}" xmlns:r="{relationships}"'
        f'{named} r:lo="rId20" r:qs="rId21" r:cs="rId22"{extra}/>'
    )


def drawn(graphic: str, data: str | None = None) -> bytes:
    """The whole package, with ``graphic`` as the one declaration the drawing makes."""
    return smart(f"<w:p>{drawing(graphic=graphic)}</w:p>", data=data)


def roles(content: bytes) -> list[str]:
    """Each segment's role, in the order the walker emitted it."""
    return [i.semantic_role for i in extract_docx(content, capture_id=CAPTURE).segments]


def test_only_the_schema_qualified_route_reaches_smartart() -> None:
    """An element sharing a local name and nothing else declares no SmartArt,
    whether it stands where the graphic or its data would, and a ``uri`` that
    merely resembles a diagram namespace -- a character longer, a slash longer,
    one holding the real URI inside it -- names another vocabulary. The route is
    the drawing object's own children, so a qualified lookalike nested below it
    is not its declaration either. A drawing declaring none of the three
    graphics is the transparent wrapper it has always been, so the prose beside
    it is still read and nothing is inventoried off it."""
    other = 'xmlns:z="urn:example:other"'
    lookalikes = (
        # The graphic and its data, each in turn, written in a vocabulary that
        # shares the local name and nothing else.
        (
            f'<z:graphic {other}><a:graphicData uri="{DIAGRAM_NS}">'
            f"{RELIDS}</a:graphicData></z:graphic>"
        ),
        (
            f'<a:graphic><z:graphicData {other} uri="{DIAGRAM_NS}">'
            f"{RELIDS}</z:graphicData></a:graphic>"
        ),
        # A graphic data written in the diagram namespace is not the graphic
        # data the route names, so nothing below it is on the route either.
        (
            f'<a:graphic><dgm:graphicData xmlns:dgm="{DIAGRAM_NS}"'
            f' uri="{DIAGRAM_NS}">{RELIDS}</dgm:graphicData></a:graphic>'
        ),
        # Graphic and graphic data written in a vocabulary that is not DrawingML.
        routed(relids(), namespace="urn:a"),
        # Graphic data naming no vocabulary at all, and one naming another the
        # package itself defines.
        routed(relids(), uri=None),
        routed(relids(), uri=CHART_NS),
        # Namespace URIs compare as strings, so each of these says its graphic
        # is something other than SmartArt.
        routed(relids(), uri=DIAGRAM_NS + "Extra"),
        routed(relids(), uri=DIAGRAM_NS + "/"),
        routed(relids(), uri="urn:example:" + DIAGRAM_NS),
        routed(relids(), uri=DIAGRAM_NS.replace("diagram", "Diagram")),
        # A qualifying route nested below the drawing rather than on it: inside
        # a graphic data that declares nothing, inside the frame properties
        # beside it, and inside a compatibility fallback.
        routed(SMART_REFERENCE, uri=None),
        f"<wp:cNvGraphicFramePr>{SMART_REFERENCE}</wp:cNvGraphicFramePr>",
        (
            f"<mc:AlternateContent><mc:Fallback>{SMART_REFERENCE}"
            "</mc:Fallback></mc:AlternateContent>"
        ),
        # The reference itself, standing under no graphic at all.
        RELIDS,
    )

    for graphic in lookalikes:
        content = smart(p("Prose") + f"<w:p>{drawing(graphic=graphic)}</w:p>")

        assert read(content) == [("body", "Prose", "0")]

    # The same package, with the one qualifying route, does declare SmartArt, so
    # each refusal above is for the step it varied and not for the package.
    assert roles(drawn(routed(relids()))) == [
        "object_inventory",
        "body",
        "body",
        "object_inventory",
    ]


def test_a_drawing_naming_more_than_one_diagram_reference_is_refused() -> None:
    """A drawing object shows one graphic, so two qualifying graphic data
    elements below it leave which diagram it shows unanswerable -- under two
    graphics, under one, or written one in each of the two diagram namespaces
    OOXML defines while naming the one relationship. One qualifying graphic data
    holding two references is the same ambiguity a step lower down. Nothing here
    chooses between them, and naming the same data model twice is still twice."""
    for graphic in (
        SMART_REFERENCE + SMART_REFERENCE,
        f"<a:graphic>{dgm_data(RELIDS)}{dgm_data(RELIDS)}</a:graphic>",
        (
            f'<a:graphic>{dgm_data(RELIDS)}<a:graphicData uri="{STRICT_NS}">'
            f"{relids(namespace=STRICT_NS)}</a:graphicData></a:graphic>"
        ),
    ):
        with pytest.raises(
            OOXMLInvalid, match="drawing holds more than one SmartArt reference"
        ):
            extract_docx(drawn(graphic), capture_id=CAPTURE)

    with pytest.raises(
        OOXMLInvalid, match="does not name exactly one diagram reference"
    ):
        extract_docx(drawn(routed(relids() + relids())), capture_id=CAPTURE)

    # A reference off the route is no second declaration: one standing under no
    # graphic, and one nested under a graphic data that declares nothing, are
    # each passed over, so the diagram the object does declare stays the single
    # graphic it shows.
    for graphic in (
        RELIDS + SMART_REFERENCE,
        routed(SMART_REFERENCE, uri=None) + SMART_REFERENCE,
    ):
        assert roles(drawn(graphic)) == [
            "object_inventory",
            "body",
            "body",
            "object_inventory",
        ]


def test_a_missing_unqualified_or_foreign_data_reference_is_refused() -> None:
    """SmartArt is advertised, so the reference has to be there, written as a
    direct child in the namespace the ``uri`` declared, and naming its data
    model on a relationship-namespaced ``dm``. A ``relIds`` from another
    vocabulary, or one nested a step deeper, is not the declaration's own; and an
    id written in no namespace or in another one names no relationship, whatever
    the package would resolve it to."""
    for body, reason in (
        # Advertised and then naming nothing at all.
        ("", "does not name exactly one diagram reference"),
        # A ``relIds`` in the other diagram namespace, in a foreign one, and in
        # none, each share a local name with the reference and nothing else.
        (
            relids(namespace=STRICT_NS),
            "does not name exactly one diagram reference",
        ),
        (relids(namespace="urn:dgm"), "does not name exactly one diagram reference"),
        (
            '<relIds r:dm="rId4"/>',
            "does not name exactly one diagram reference",
        ),
        # On the route means a direct child, so one carried a step deeper is not
        # the graphic data's own reference.
        (
            f'<dgm:extLst xmlns:dgm="{DIAGRAM_NS}">{relids()}</dgm:extLst>',
            "does not name exactly one diagram reference",
        ),
        # The reference is there and names no data model, names one in no
        # namespace, or names one in a vocabulary that defines no such attribute.
        (relids(dm=None), "SmartArt reference names no relationship"),
        (
            relids(dm=None, extra=' dm="rId4"'),
            "SmartArt reference names no relationship",
        ),
        (relids(relationships="urn:r"), "SmartArt reference names no relationship"),
        (
            relids(dm=None, extra=' xmlns:z="urn:example:other" z:dm="rId4"'),
            "SmartArt reference names no relationship",
        ),
    ):
        with pytest.raises(OOXMLInvalid, match=reason):
            extract_docx(drawn(routed(body)), capture_id=CAPTURE)

    # One ``dm`` in each relationships namespace is two ids rather than a
    # repeat, even where both spell out the same relationship.
    doubled = relids(extra=f' xmlns:r2="{STRICT_REL_NS}" r2:dm="rId4"')
    with pytest.raises(
        OOXMLInvalid, match="SmartArt reference names more than one relationship"
    ):
        extract_docx(drawn(routed(doubled)), capture_id=CAPTURE)


def test_the_strict_namespaces_reach_the_same_diagram() -> None:
    """Strict and Transitional are the same declaration at every step of the
    route: the DrawingML main namespace the graphic and its data are qualified
    in, the diagram namespace the ``uri`` declares and the reference is written
    in, and the relationships namespace the ``dm`` carries. The data model part
    is rooted in the Strict vocabulary to match, and reaches the same records."""
    strict = drawn(
        routed(
            relids(namespace=STRICT_NS, relationships=STRICT_REL_NS),
            uri=STRICT_NS,
            namespace=STRICT_DRAWING_NS,
        ),
        data=model().replace(DIAGRAM_NS, STRICT_NS),
    )

    assert [
        (i.semantic_role, i.text) for i in extract_docx(strict, capture_id=CAPTURE).segments
    ] == [
        (i.semantic_role, i.text)
        for i in extract_docx(smart(), capture_id=CAPTURE).segments
    ]
    assert inventory(strict) == [("0.d0", SMART_RECORD), ("0.d0", SMART_CONNECTION)]
    assert (
        dict(extract_docx(strict, capture_id=CAPTURE).segments[1].source_locator)
        == SMART_LOCATOR
    )


def test_smartart_in_a_table_cell_splits_the_run_around_it_in_source_order() -> None:
    """The diagram closes the cell's current run where it sits rather than
    trailing the prose it precedes, exactly as a chart or an image does, and the
    text after it opens a later run of the same cell. The record, the point text
    and the connection all sit at the diagram's own position, and each carries a
    locator no other segment carries."""
    cell = f"<w:p>{run('Before ')}{SMART_DRAWING}{run(' after')}</w:p>"
    content = smart(f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl>")

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("table_cell", "Before ", "0.r0.c0"),
        ("object_inventory", None, "0.r0.c0.0.d0"),
        ("body", "First", "0.r0.c0.0.d0"),
        ("body", "Second", "0.r0.c0.0.d0"),
        ("object_inventory", None, "0.r0.c0.0.d0"),
        ("table_cell", " after", "0.r0.c0.t1"),
    ]
    assert inventory(content) == [
        ("0.r0.c0.0.d0", SMART_RECORD),
        ("0.r0.c0.0.d0", SMART_CONNECTION),
    ]
    locators = [tuple(sorted(i.source_locator.items())) for i in result.segments]
    assert len(set(locators)) == len(locators)


# The reference above is schema-valid at every step of the route, so what the
# tests below vary is the package it resolves against: the relationship the
# ``r:dm`` names, the member that relationship reaches, and what that member
# says it is. ``smart()`` is the whole package intact, so each refusal here is
# for the one step it varied.
DIAGRAM_TYPE = REL + "diagramData"
DATA: dict[str, str] = {DIAGRAM_PART: model()}


def packaged(
    *relations: tuple[str, str, bool, str],
    content_types: str = DIAGRAM_OVERRIDE,
    members: dict[str, bytes] | None = None,
    **parts: str,
) -> bytes:
    """The one inline SmartArt paragraph, over the package given around it."""
    return linked(
        f"<w:p>{SMART_DRAWING}</w:p>",
        *relations,
        content_types=content_types,
        members=members,
        **parts,
    )


def typed(kind: str) -> dict[str, str]:
    """The data relationship retyped: target and part stay the real ones."""
    return {
        "word/_rels/document.xml.rels": rels(DIAGRAM_RELATION).replace(
            DIAGRAM_TYPE, kind
        )
    }


def test_a_smartart_reference_that_reaches_no_diagram_relationship_is_refused() -> None:
    """The id is resolved through the holding part's own relationships or not at
    all: no diagram path is derived from the story's name, so a data part sitting
    in the package under another id is never read. The relationship has to carry
    one of the URIs OOXML defines for diagram data, and nothing reaches one by
    ending the same way or by being another member of the diagram set. It also
    has to be internal -- ``r:dm`` names a part of the package -- so an external
    target is no data model and is not quietly recorded as a link. Every refusal
    names the document the reference came from, not whichever format reached the
    shared reader first."""
    for relations, message in (
        ((), "reference names a missing relationship"),
        # The package relates the very same data part, under an id this drawing
        # does not name.
        (
            (("rId1", "diagrams/data1.xml", False, "diagramData"),),
            "reference names a missing relationship",
        ),
        (
            (("rId4", "https://example.invalid/data1.xml", True, "diagramData"),),
            "relationship is external",
        ),
    ):
        with pytest.raises(OOXMLInvalid, match=f"DOCX diagram data {message}"):
            extract_docx(packaged(*relations, **DATA), capture_id=CAPTURE)

    for kind in (
        # The defined URI with something appended, and with a segment inserted
        # before its last one.
        DIAGRAM_TYPE + "Extra",
        REL + "x/diagramData",
        # The path is case-sensitive, so a folded path names another type.
        REL + "diagramdata",
        # The same tail under another host.
        (
            "http://diagrams.example.invalid/officeDocument/2006/relationships"
            "/diagramData"
        ),
        # The rest of the diagram set: each names a part nothing here opens.
        REL + "diagramLayout",
        REL + "diagramQuickStyle",
        REL + "diagramColors",
        REL + "diagramDrawing",
    ):
        with pytest.raises(
            OOXMLInvalid,
            match="DOCX diagram data reference names a relationship of another type",
        ):
            extract_docx(packaged(**typed(kind), **DATA), capture_id=CAPTURE)


def test_a_diagram_part_the_package_does_not_hold_or_declare_is_refused() -> None:
    """Containment and existence are settled when the package validates its
    relationships, so a data part that is not there never reaches a record. The
    target also has to be declared diagram data rather than merely named it: a
    part the package declares as another member of the diagram set, as nothing
    at all, or only through the default its extension carries, qualifies as no
    data model."""
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_docx(packaged(DIAGRAM_RELATION), capture_id=CAPTURE)

    layout = (
        f'<Override PartName="/{DIAGRAM_PART}" ContentType="application/vnd.'
        'openxmlformats-officedocument.drawingml.diagramLayout+xml"/>'
    )
    default = f'<Default Extension="xml" ContentType="{DIAGRAM_CT}"/>'
    for content_types in ("", layout, default):
        with pytest.raises(
            OOXMLInvalid,
            match="DOCX diagram data relationship does not name a diagram data part",
        ):
            extract_docx(
                packaged(DIAGRAM_RELATION, content_types=content_types, **DATA),
                capture_id=CAPTURE,
            )


def test_a_diagram_part_rooted_elsewhere_is_refused_and_strict_is_read() -> None:
    """The declared content type says what the part is; the root element is
    where the part says so itself. A root that merely reads like a data model --
    a namespace a character longer, a slash longer, one holding the real URI
    inside it, one recased, the DrawingML main namespace the model's own
    prefixes bind, or no namespace at all -- is another vocabulary, and a root
    in the right vocabulary at the wrong element is another part of the set. The
    reference that reached it stays the schema-valid reference it was. The
    Strict namespace is the same declaration as the Transitional one, so a
    Strict data model reached by a Transitional reference is read."""
    for namespace in (
        DIAGRAM_NS + "2",
        DIAGRAM_NS + "/",
        f"urn:example:{DIAGRAM_NS}",
        DIAGRAM_NS.replace("diagram", "Diagram"),
        DRAWING_NS,
        "urn:dgm",
    ):
        part = model().replace(f'"{DIAGRAM_NS}"', f'"{namespace}"')

        with pytest.raises(
            OOXMLInvalid, match="DOCX diagram part uses an unsupported namespace"
        ):
            extract_docx(
                packaged(DIAGRAM_RELATION, **{DIAGRAM_PART: part}), capture_id=CAPTURE
            )

    with pytest.raises(
        OOXMLInvalid, match="DOCX diagram part uses an unsupported namespace"
    ):
        extract_docx(
            packaged(DIAGRAM_RELATION, **{DIAGRAM_PART: "<dataModel/>"}),
            capture_id=CAPTURE,
        )

    # Rooted in the right vocabulary at the wrong element: the point list the
    # model holds, the layout part's own root, and the reference that reached it.
    for root in ("ptLst", "layoutDef", "relIds"):
        part = model().replace("dgm:dataModel", f"dgm:{root}")

        with pytest.raises(
            OOXMLInvalid, match="DOCX diagram part is not rooted at its own data model"
        ):
            extract_docx(
                packaged(DIAGRAM_RELATION, **{DIAGRAM_PART: part}), capture_id=CAPTURE
            )

    strict = packaged(
        DIAGRAM_RELATION, **{DIAGRAM_PART: model().replace(DIAGRAM_NS, STRICT_NS)}
    )

    assert (
        extract_docx(strict, capture_id=CAPTURE).segments
        == extract_docx(smart(), capture_id=CAPTURE).segments
    )
    assert inventory(strict) == [("0.d0", SMART_RECORD), ("0.d0", SMART_CONNECTION)]


# The rest of the diagram set, named from both of the places that name it: the
# layout, quick-style and colour relationships the ``dgm:relIds`` already cites
# beside the data, and the drawing part the data model's own extension cites on
# a relationship of the data part itself -- the route a renderer takes to the
# saved geometry. Every member is present, declared nothing, and not a part
# anything here could parse, so a reader that opened one would fail rather than
# quietly pass.
VISUALS: dict[str, str] = {
    f"word/diagrams/{name}.xml": "not a part anything here reads"
    for name in ("layout1", "quickStyle1", "colors1", "drawing1")
}
SIBLINGS = (
    ("rId20", "diagrams/layout1.xml", False, "diagramLayout"),
    ("rId21", "diagrams/quickStyle1.xml", False, "diagramQuickStyle"),
    ("rId22", "diagrams/colors1.xml", False, "diagramColors"),
    ("rId23", "diagrams/drawing1.xml", False, "diagramDrawing"),
)
DSP_NS = "http://schemas.microsoft.com/office/drawing/2008/diagram"
MODEL_EXT = (
    f'<dgm:extLst><a:ext uri="{DSP_NS}">'
    f'<dsp:dataModelExt xmlns:dsp="{DSP_NS}" relId="rId30" minVer="12.0"/>'
    "</a:ext></dgm:extLst>"
)


def test_the_rest_of_the_diagram_set_stays_shut() -> None:
    """Only the data reference is followed. The layout, quick-style and colour
    parts named beside the data are there and malformed, the drawing part the
    data model's own extension names is there and malformed, and the data part
    carries its own relationship to it -- and the stored point text and
    connection come back exactly as they do from a package holding none of them.
    The record says so outright: no related visual part was opened and no
    hierarchy was inferred, so no label, order or shape was read off how the
    diagram would be drawn."""
    content = packaged(
        DIAGRAM_RELATION,
        *SIBLINGS,
        **{
            DIAGRAM_PART: model().replace(
                "</dgm:dataModel>", f"{MODEL_EXT}</dgm:dataModel>"
            ),
            "word/diagrams/_rels/data1.xml.rels": rels(
                ("rId30", "drawing1.xml", False, "diagramDrawing")
            ),
            **VISUALS,
        },
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert result.segments == extract_docx(smart(), capture_id=CAPTURE).segments
    assert [(i.semantic_role, i.text) for i in result.segments] == [
        ("object_inventory", None),
        ("body", "First"),
        ("body", "Second"),
        ("object_inventory", None),
    ]
    assert inventory(content) == [("0.d0", SMART_RECORD), ("0.d0", SMART_CONNECTION)]


# The legacy object container, in the markup Word actually writes: the VML
# shape standing in for the object on the page, then the ``o:OLEObject``
# directly below the container naming the part or the link. ``o:`` is bound on
# the declaration itself because nothing else in this file needs it.
OFFICE = 'xmlns:o="urn:schemas-microsoft-com:office:office"'
# Bytes at the embedded targets that are not an OLE stream, not a package, and
# -- for the package target -- not an archive either, which the package would
# refuse outright. The proof that this slice never reaches an object is that
# these are never opened: only the length the package measured is reported.
OBJECT_MEMBERS = {
    "word/embeddings/oleObject1.bin": b"not an OLE compound stream",
    "word/embeddings/Workbook1.xlsx": b"not a package and not an archive",
}
OBJECT_DEFAULTS = (
    '<Default Extension="bin" '
    'ContentType="application/vnd.openxmlformats-officedocument.oleObject"/>'
    '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sheet"/>'
)
OBJECT_RELATION = ("rId9", "embeddings/oleObject1.bin", False, "oleObject")
PACKAGE_RELATION = ("rId9", "embeddings/Workbook1.xlsx", False, "package")
LINK_RELATION = ("rId9", "HTTPS://files.example/Budget.XLSX", True, "oleObject")
# The object's own claims, as Word writes them on an embedded object. None is
# interpreted: the ``ProgID`` names the server that would open the object and
# nothing here looks one up, and the two identifiers are the legacy drawing
# layer's own and are resolved against nothing.
CLAIMS = (
    ' ProgID="Excel.Sheet.12" ShapeID="_x0000_i1025" DrawAspect="Content"'
    ' ObjectID="_1543581977"'
)
# The one claim Word writes only on a link: how it would refresh the object,
# which nothing here refreshes.
LINK_CLAIMS = CLAIMS + ' UpdateMode="Always"'


def ole(mode: str = "Embed", *, claims: str = CLAIMS) -> str:
    """One ``w:object``, inside the run that anchors it."""
    return (
        f'<w:r><w:object><v:shape id="_x0000_i1025" style="width:96pt;height:72pt"/>'
        f'<o:OLEObject {OFFICE} Type="{mode}" r:id="rId9"{claims}/>'
        "</w:object></w:r>"
    )


def objected(body: str, relation: tuple[str, str, bool, str] = OBJECT_RELATION) -> bytes:
    """A document whose one object names ``relation``."""
    return linked(
        body, relation, content_types=OBJECT_DEFAULTS, members=OBJECT_MEMBERS
    )


# What the default fixture's record says about the object part it embeds. The
# content type is the folded form of what the package declared, as it is for an
# image, and the byte length is the figure the package already measured.
EMBEDDED: dict[str, object] = {
    "kind": "oleObject",
    "mode": "embed",
    "relationshipId": "rId9",
    "relationshipType": REL + "oleObject",
    "target": "word/embeddings/oleObject1.bin",
    "external": False,
    "scheme": None,
    "objectPart": "word/embeddings/oleObject1.bin",
    "contentType": "application/vnd.openxmlformats-officedocument.oleobject",
    "objectBytes": len(OBJECT_MEMBERS["word/embeddings/oleObject1.bin"]),
    "progId": "Excel.Sheet.12",
    "shapeId": "_x0000_i1025",
    "drawAspect": "Content",
    "objectId": "_1543581977",
    "updateMode": None,
    "previewDeclared": True,
}
# What a record says about the package embedded as one object, and about a
# target outside the package: the reference itself, and nothing invented for it.
PACKAGED: dict[str, object] = {
    "relationshipType": REL + "package",
    "target": "word/embeddings/Workbook1.xlsx",
    "objectPart": "word/embeddings/Workbook1.xlsx",
    "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "objectBytes": len(OBJECT_MEMBERS["word/embeddings/Workbook1.xlsx"]),
}
OUTSIDE: dict[str, object] = {
    "objectPart": None,
    "contentType": None,
    "objectBytes": None,
}


def test_an_embedded_object_is_inventoried_where_it_sits_and_holds_no_text() -> None:
    """One record, where the story anchors the object, carrying the declaration's
    own claims, the VML stand-in as declared, and what the package already knows
    about the part it names. Nothing is read out of the object: the record is the
    paragraph's only segment, and the ``.bin`` behind it is never opened."""
    content = objected(f"<w:p>{ole()}</w:p>")

    result = extract_docx(content, capture_id=CAPTURE)

    assert [(i.semantic_role, i.text) for i in result.segments] == [
        ("object_inventory", None)
    ]
    assert inventory(content) == [("0.o0", EMBEDDED)]


def test_a_package_embedded_as_one_object_reports_what_the_package_measured() -> None:
    """A ``package`` relationship qualifies as an object exactly as an
    ``oleObject`` one does, and the member behind it is not an archive at all --
    so the record can only be reporting the name, the declared type and the
    length the package already had, rather than anything read by opening it."""
    content = objected(f"<w:p>{ole()}</w:p>", PACKAGE_RELATION)

    assert inventory(content) == [("0.o0", EMBEDDED | PACKAGED)]


def test_a_linked_object_carries_its_target_and_scheme_without_part_metadata() -> None:
    """``Type="Link"`` says the object is a reference out of the package, and the
    reference is inventory: it is carried exactly as the relationship holds it,
    beside the scheme its own text names, and nothing fetches, probes or
    normalises it. There is no part inside the package, so the three part fields
    stay absent rather than being invented for it."""
    content = objected(
        f"<w:p>{ole('Link', claims=LINK_CLAIMS)}</w:p>",
        LINK_RELATION,
    )

    assert inventory(content) == [
        (
            "0.o0",
            EMBEDDED
            | OUTSIDE
            | {
                "mode": "link",
                "target": "HTTPS://files.example/Budget.XLSX",
                "external": True,
                "scheme": "https",
                "updateMode": "Always",
            },
        )
    ]


def test_objects_keep_cell_order_and_a_deletion_over_one_is_preserved() -> None:
    """An object closes the cell's current run where it sits, so the prose
    before it and the prose after it stay separate cell runs rather than joining
    across it, and the record carries its own ``.o0`` between them. A deletion
    wrapped around a container is kept as the revision it is."""
    body = (
        "<w:tbl><w:tr><w:tc>"
        f"<w:p>{run('Attached ')}{ole()}{run(' see above')}</w:p>"
        f"<w:p><w:del>{ole()}</w:del></w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = objected(body)

    assert [
        (
            i.semantic_role,
            i.text,
            i.source_locator["position"],
            i.revision_state,
            i.visibility,
        )
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [
        ("table_cell", "Attached ", "0.r0.c0", "current", "visible"),
        ("object_inventory", None, "0.r0.c0.0.o0", "current", "visible"),
        ("table_cell", " see above", "0.r0.c0.t1", "current", "visible"),
        ("object_inventory", None, "0.r0.c0.1.o0", "deleted", "annotation"),
    ]


def test_two_objects_in_one_paragraph_are_numbered_in_the_order_they_stand() -> None:
    """The counter is the paragraph's own, so each container gets its own
    locator in source order rather than both landing on the same one."""
    content = objected(f"<w:p>{ole()}{run(' and ')}{ole()}</w:p>")

    assert [position for position, _ in inventory(content)] == ["0.o0", "0.o1"]


def test_an_object_is_the_saved_result_of_the_field_it_sits_under() -> None:
    """A field whose stored result is an object alone stored a result, exactly as
    one whose result is an image did."""
    body = (
        f"<w:p>{fld('begin')}{instr(' EMBED Excel.Sheet.12 ')}{fld('separate')}"
        f"{ole()}{fld('end')}</w:p>"
    )
    content = objected(body)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [("object_inventory", "0.f0"), ("object_inventory", "0.o0")]
    assert inventory(content)[0][1]["savedResultPresent"] is True


def test_an_object_inside_a_field_instruction_is_refused() -> None:
    """An instruction is field code, which carries no object of its own, so one
    reached before the separator is refused rather than inventoried."""
    body = (
        f"<w:p>{fld('begin')}{instr(' EMBED ')}{ole()}"
        f"{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds an embedded object"):
        extract_docx(objected(body), capture_id=CAPTURE)


def test_an_object_claim_longer_than_the_bound_is_clipped() -> None:
    """The claims ride in a bounded JSON record, so each is clipped rather than
    carried at whatever length the file wrote it."""
    long = "P" * (_MAX_META_CHARS + 144)
    content = objected(f"""<w:p>{ole(claims=f' ProgID="{long}"')}</w:p>""")

    assert inventory(content)[0][1]["progId"] == "P" * _MAX_META_CHARS


def test_an_object_without_a_stand_in_declares_no_preview() -> None:
    """The VML shape is optional markup, and its absence is reported as the
    absence it is rather than left unsaid."""
    content = objected(
        f'<w:p><w:r><w:object><o:OLEObject {OFFICE} Type="Embed" r:id="rId9"'
        f"{CLAIMS}/></w:object></w:r></w:p>"
    )

    assert inventory(content) == [("0.o0", EMBEDDED | {"previewDeclared": False})]


# The Strict spelling of the container's own namespace, the WordprocessingML
# main one. ``o:OLEObject`` has no counterpart here: it is an Office drawing
# extension outside both OOXML profiles, so the one Office namespace carries it
# in a Strict package exactly as in a Transitional one.
STRICT_MAIN_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
OTHER = 'xmlns:z="urn:example:other"'
# Prose sitting beside a declaration that was not admitted. A container that
# declared no object is the transparent wrapper it has always been, so this is
# still read -- which is what says the declaration beside it was passed over
# rather than quietly turned into a record.
CAPTION = "<w:t>stand-in caption</w:t>"


def holder(inside: str, name: str = "w:object", attrs: str = "") -> str:
    """One container of the given qualified name, inside the run anchoring it."""
    return f"<w:r><{name}{attrs}>{inside}</{name}></w:r>"


def oleobj(
    name: str = f"o:OLEObject {OFFICE}",
    *,
    kind: str = ' Type="Embed"',
    ident: str = ' r:id="rId9"',
) -> str:
    """One self-closing declaration, with each piece that admits it open."""
    return f"<{name}{kind}{ident}{CLAIMS}/>"


def test_only_the_two_wordprocessing_namespaces_admit_an_object_container() -> None:
    """The container is admitted on its qualified name alone, unlike the local
    names the rest of the story is read by. An element merely called ``object``
    in another vocabulary declares nothing whatever it holds: it stays the
    transparent wrapper every unrecognised element is, so the prose inside it is
    still read and no record is taken from the declaration it carries."""
    content = objected(
        "<w:p>" + holder(oleobj() + CAPTION, "z:object", " " + OTHER) + "</w:p>"
    )

    assert read(content) == [("body", "stand-in caption", "0")]
    assert inventory(content) == []


def test_the_strict_container_and_a_strict_relationship_id_reach_the_same_record() -> (
    None
):
    """Strict is the same declaration at every step that has a Strict spelling:
    the container in the Strict WordprocessingML main namespace, and the id in
    the Strict relationships namespace. The declaration itself keeps its one
    Office namespace either way, and the record is the Transitional one
    exactly."""
    strict_id = ' r2:id="rId9" xmlns:r2="' + STRICT_REL_NS + '"'
    inside = '<v:shape id="_x0000_i1025"/>' + oleobj(ident=strict_id)
    content = objected(
        "<w:p>"
        + holder(inside, "ws:object", ' xmlns:ws="' + STRICT_MAIN_NS + '"')
        + "</w:p>"
    )

    assert inventory(content) == [("0.o0", EMBEDDED)]


def test_only_a_direct_office_namespaced_declaration_declares_an_object() -> None:
    """The route in is one hop and no deeper, and the Office namespace is the one
    that carries the declaration. A declaration nested inside the VML shape
    standing in for the object, one carried under an alternate-content choice,
    one sharing the local name in another vocabulary, one in the VML namespace
    the stand-in shape uses, and one in the Strict spelling of the Office
    namespace that OOXML never defines are each passed over -- so the container
    reports no object rather than one found elsewhere, and stays the transparent
    wrapper it is today, prose and all."""
    for inside in (
        f'<v:shape id="_x0000_i1025">{oleobj()}</v:shape>',
        "<mc:AlternateContent><mc:Choice>"
        + oleobj()
        + "</mc:Choice></mc:AlternateContent>",
        oleobj("z:OLEObject " + OTHER),
        oleobj("v:OLEObject"),
        oleobj('q:OLEObject xmlns:q="http://purl.oclc.org/ooxml/office/office"'),
    ):
        content = objected("<w:p>" + holder(inside + CAPTION) + "</w:p>")

        assert read(content) == [("body", "stand-in caption", "0")]
        assert inventory(content) == []


def test_a_container_declaring_two_objects_is_refused() -> None:
    """A container shows one object, so two declarations directly below it leave
    which one it shows unanswerable, and nothing here chooses between them even
    where both spell out the same object."""
    content = objected("<w:p>" + holder(oleobj() + oleobj()) + "</w:p>")

    with pytest.raises(OOXMLInvalid, match="declares more than one embedded object"):
        extract_docx(content, capture_id=CAPTURE)


def test_the_declared_type_is_required_and_is_exactly_embed_or_link() -> None:
    """``Type`` is the only thing that says which of the two kinds the object is,
    and the value is closed and case-sensitive: one missing, one differing in
    case, one that merely begins the same way, and one padded with space are each
    refused rather than read as whichever kind they resemble. The attribute is
    unqualified, so the same name in another vocabulary is that vocabulary's
    extension and declares nothing here either."""
    for kind in (
        "",
        ' Type="embed"',
        ' Type="EMBED"',
        ' Type="Embedded"',
        ' Type="Embed "',
        ' Type="link"',
        ' Type="Linked"',
        " " + OTHER + ' z:Type="Embed"',
    ):
        content = objected("<w:p>" + holder(oleobj(kind=kind)) + "</w:p>")

        with pytest.raises(
            OOXMLInvalid, match="embedded object does not declare embed or link"
        ):
            extract_docx(content, capture_id=CAPTURE)


def test_the_relationship_id_must_be_one_relationship_namespaced_attribute() -> None:
    """The id is defined in the relationships namespace, so one written in no
    namespace at all, or in a vocabulary that defines no such attribute, names no
    relationship whatever the package would resolve the text to. One written in
    each of the two namespaces that do carry it is two ids rather than a repeat,
    even where both spell out the same relationship."""
    for ident, reason in (
        ("", "names no relationship"),
        (' id="rId9"', "names no relationship"),
        (" " + OTHER + ' z:id="rId9"', "names no relationship"),
        (
            ' r:id="rId9" r2:id="rId9" xmlns:r2="' + STRICT_REL_NS + '"',
            "names more than one relationship",
        ),
    ):
        content = objected("<w:p>" + holder(oleobj(ident=ident)) + "</w:p>")

        with pytest.raises(OOXMLInvalid, match="embedded object " + reason):
            extract_docx(content, capture_id=CAPTURE)


# The package boundary the object's one reference crosses, and everything that
# is ever read across it: a name, the type the package declares for it and the
# length the package measured while validating the member. Nothing on the far
# side is opened, parsed, decoded, executed, launched or fetched, which is why
# every fixture below puts bytes no server would open behind the reference.
def qualified(
    kind: str,
    target: str = "embeddings/oleObject1.bin",
    *,
    external: bool = False,
    mode: str = "Embed",
) -> bytes:
    """The default object document, whose one relationship is exactly ``kind``.

    ``rels`` names the types OOXML defines by their last segment; a type is
    admitted or refused on the whole URI it was written as, so these are spelled
    out in full instead.
    """
    return docx(
        f"<w:p>{ole(mode)}</w:p>",
        {
            "word/_rels/document.xml.rels": (
                f'<Relationships><Relationship Id="rId9" Type="{kind}" '
                f'Target="{target}"'
                + (' TargetMode="External"' if external else "")
                + "/></Relationships>"
            )
        },
        styles=False,
        content_types=OBJECT_DEFAULTS,
        members=OBJECT_MEMBERS,
    )


# A target outside the package written as a relative reference. It names no
# scheme, and nothing here invents one, resolves it against the package or the
# document's own location, or goes looking for what it points at.
RELATIVE_LINK = ("rId9", "../../books/Budget.xlsx", True, "oleObject")
# Bytes at an embedded target that announce themselves as an OLE compound
# stream, are not one, and hold readable prose nobody authored into the story.
# The prose is the proof: it never reaches a segment and never reaches a record.
PAYLOAD = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 PAYLOAD PROSE \x00 not a stream at all"
# The five claims, as the schema spells them -- unqualified, so the same local
# name carried in another vocabulary is that vocabulary's extension.
CLAIM_NAMES = ("ProgID", "ShapeID", "DrawAspect", "ObjectID", "UpdateMode")
CLAIM_FIELDS = ("progId", "shapeId", "drawAspect", "objectId", "updateMode")


@pytest.mark.parametrize(
    "kind",
    [
        REL + "oleObject",
        STRICT_REL_NS + "/oleObject",
        REL + "package",
        STRICT_REL_NS + "/package",
    ],
)
def test_both_profiles_of_both_object_relationship_types_qualify(kind: str) -> None:
    """A single OLE object and a whole package embedded as one are the two types
    OOXML defines for an object, each in a Transitional and a Strict spelling,
    and all four reach the same record. The type is reported as the file wrote
    it rather than folded into one of them."""
    assert inventory(qualified(kind)) == [
        ("0.o0", EMBEDDED | {"relationshipType": kind})
    ]


@pytest.mark.parametrize(
    ("kind", "mode", "target", "external"),
    [
        # Another type entirely reaches no object at all, inside the package or
        # out of it, whatever the target behind it holds.
        (REL + "image", "Embed", "embeddings/oleObject1.bin", False),
        (REL + "hyperlink", "Link", "https://files.example/Budget.XLSX", True),
        (REL + "externalLink", "Link", "https://files.example/Budget.XLSX", True),
        # A type that merely ends the same way is not one OOXML defines: the
        # last segment is not the type, the whole URI is.
        ("http://example.invalid/relationships/oleObject", "Embed", "", False),
        ("http://example.invalid/relationships/package", "Embed", "", False),
        # The defined URI under another scheme is another URI. Scheme and host
        # fold under RFC 3986 and the path does not, so a path differing in case
        # is a different type rather than the same one spelled loudly.
        (
            (
                "https://schemas.openxmlformats.org/officeDocument/2006"
                "/relationships/oleObject"
            ),
            "Embed",
            "",
            False,
        ),
        (REL + "OLEObject", "Embed", "", False),
        # Ending in the defined last segment with no authority at all.
        ("urn:schemas-microsoft-com:office:oleObject", "Embed", "", False),
    ],
)
def test_a_relationship_of_another_type_names_no_object(
    kind: str, mode: str, target: str, external: bool
) -> None:
    """The relationship has to be one OOXML defines for an object. Nothing
    reaches an object by ending the same way, and an image, a hyperlink or an
    external-link relationship reaches no object at all -- the object is refused
    rather than read through a reference that qualifies it as something else."""
    content = qualified(
        kind,
        target or "embeddings/oleObject1.bin",
        external=external,
        mode=mode,
    )

    with pytest.raises(
        OOXMLInvalid, match="embedded object names a relationship of another type"
    ):
        extract_docx(content, capture_id=CAPTURE)


def test_an_object_names_its_relationship_by_id_and_never_by_target() -> None:
    """The id is the whole reference. A package holding the very part the object
    would embed, under another id, holds no relationship the object names:
    nothing searches the story's relationships by target, content type or
    resemblance, so the object is refused rather than resolved to the match
    sitting beside it."""
    content = objected(
        f"<w:p>{ole()}</w:p>", ("rId3", "embeddings/oleObject1.bin", False, "oleObject")
    )

    with pytest.raises(
        OOXMLInvalid, match="embedded object names a missing relationship"
    ):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("mode", "claims", "relation"),
    [
        # An embedded object is a part inside the package.
        ("Embed", CLAIMS, LINK_RELATION),
        # A linked one is a reference out of it.
        ("Link", LINK_CLAIMS, OBJECT_RELATION),
    ],
)
def test_the_declaration_and_the_relationship_have_to_agree(
    mode: str, claims: str, relation: tuple[str, str, bool, str]
) -> None:
    """``Type`` says which kind of object it is and the relationship says which
    side of the package boundary the reference lands on. Where the two
    contradict each other the object is refused rather than reported as whichever
    one of them is believed: neither is read as correcting the other."""
    content = objected(f"<w:p>{ole(mode, claims=claims)}</w:p>", relation)

    with pytest.raises(
        OOXMLInvalid, match="embedded object contradicts the relationship it names"
    ):
        extract_docx(content, capture_id=CAPTURE)


def test_an_embedded_target_is_a_member_before_this_slice_sees_it() -> None:
    """Containment, path safety and existence are all settled while the package
    is validated, which is why nothing re-checks them here: an object can never
    reach a part the archive does not hold, so the reference is refused at the
    package boundary rather than reported with a length nothing measured."""
    content = objected(
        f"<w:p>{ole()}</w:p>", ("rId9", "embeddings/absent.bin", False, "oleObject")
    )

    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_docx(content, capture_id=CAPTURE)


def test_a_relative_external_target_names_no_scheme_and_is_never_fetched() -> None:
    """A link out of the package is carried exactly as the relationship holds
    it. A relative reference names no scheme, so the record says none rather
    than inventing one, and it is not resolved against the package, the member
    it sits beside or the document's own location. It is counted as the external
    reference it is, which is the whole of what happens to it: nothing fetches,
    probes or normalises it, and there is no part inside the package, so the
    three part fields stay absent rather than being invented for it."""
    content = objected(f"<w:p>{ole('Link', claims=LINK_CLAIMS)}</w:p>", RELATIVE_LINK)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [(i.semantic_role, i.text) for i in result.segments] == [
        ("object_inventory", None)
    ]
    assert inventory(content) == [
        (
            "0.o0",
            EMBEDDED
            | OUTSIDE
            | {
                "mode": "link",
                "target": "../../books/Budget.xlsx",
                "external": True,
                "scheme": None,
                "updateMode": "Always",
            },
        )
    ]
    assert result.limitations["externalReferences"] == 1


def test_malformed_object_bytes_are_measured_and_never_opened() -> None:
    """The member behind the reference is not the stream its first bytes claim,
    and holds prose besides. Only the name, the type the package declares and
    the length it already measured are reported: nothing parses, decodes or
    executes the payload, so the prose inside it reaches neither a segment nor
    the record, and the malformed stream refuses nothing."""
    content = linked(
        f"<w:p>{ole()}</w:p>",
        OBJECT_RELATION,
        content_types=OBJECT_DEFAULTS,
        members=OBJECT_MEMBERS | {"word/embeddings/oleObject1.bin": PAYLOAD},
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert [(i.semantic_role, i.text) for i in result.segments] == [
        ("object_inventory", None)
    ]
    assert inventory(content) == [("0.o0", EMBEDDED | {"objectBytes": len(PAYLOAD)})]
    assert "PAYLOAD" not in repr(inventory(content))


def test_no_text_from_either_payload_ever_becomes_a_segment() -> None:
    """Both embedded targets hold readable prose, one reached as a single OLE
    object and one as a whole package. The story's own text is the only text
    there is: each object contributes a record carrying no text, and neither
    member is opened for what it holds."""
    body = (
        f"<w:p>{run('Attached ')}{ole()}"
        + holder(oleobj(ident=' r:id="rId8"'))
        + "</w:p>"
    )
    content = linked(
        body,
        OBJECT_RELATION,
        ("rId8", "embeddings/Workbook1.xlsx", False, "package"),
        content_types=OBJECT_DEFAULTS,
        members=dict.fromkeys(OBJECT_MEMBERS, PAYLOAD),
    )

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Attached ", "0"),
        ("object_inventory", None, "0.o0"),
        ("object_inventory", None, "0.o1"),
    ]
    assert "PAYLOAD" not in repr([i.structured_value for i in result.segments])


def test_every_claim_is_clipped_into_the_bounded_record_and_kept_as_written() -> None:
    """All five claims ride in the one bounded JSON record, so each is clipped
    rather than carried at whatever length the file wrote it, and each is kept
    apart from the others rather than joined. A claim written empty stays the
    empty claim the file made rather than becoming an absent one."""
    long = "C" * (_MAX_META_CHARS + 144)
    content = objected(
        f"""<w:p>{ole(claims=(
            f' ProgID="{long}" ShapeID="" DrawAspect="{long}"'
            f' ObjectID="" UpdateMode="{long}"'
        ))}</w:p>"""
    )

    assert inventory(content) == [
        (
            "0.o0",
            EMBEDDED
            | {
                "progId": "C" * _MAX_META_CHARS,
                "shapeId": "",
                "drawAspect": "C" * _MAX_META_CHARS,
                "objectId": "",
                "updateMode": "C" * _MAX_META_CHARS,
            },
        )
    ]


def test_a_claim_in_another_vocabulary_claims_nothing_about_this_object() -> None:
    """The claims are unqualified, as the schema writes them, so the same local
    name carried in another vocabulary is that vocabulary's extension and says
    nothing here. Each field is reported absent rather than filled from the
    foreign attribute sitting beside it, and the object itself still resolves:
    an extension nobody here reads refuses nothing."""
    foreign = " " + OTHER + "".join(f' z:{name}="claimed"' for name in CLAIM_NAMES)
    content = objected(f"<w:p>{ole(claims=foreign)}</w:p>")

    assert inventory(content) == [
        ("0.o0", EMBEDDED | dict.fromkeys(CLAIM_FIELDS, None))
    ]


# The legacy picture container, in the markup Word actually writes: the VML
# shape directly below ``w:pict``, and the one ``v:imagedata`` below that shape
# naming the image. ``v:`` is bound on the document root with the rest; ``o:``
# is bound on the ``imagedata`` itself, because the Office title is the only
# thing here that needs it.
SHAPE_ID = "_x0000_i1026"
VML_STYLE = "width:96pt;height:72pt"
# The one style that moves the shape off the text flow, among the declarations
# Word writes beside it, none of which is read.
ANCHOR_STYLE = "position:absolute;margin-left:0;margin-top:9pt;z-index:1"
# A target outside the package, carried in the case the relationship holds it.
VML_LINK = ("rId9", "HTTPS://files.example/Logo.PNG", True, "image")


def pict(
    *,
    shape_id: str | None = SHAPE_ID,
    style: str | None = VML_STYLE,
    shape: str = "",
    data: str = "",
) -> str:
    """One ``w:pict`` showing one image, inside the run that anchors it."""
    named = "" if shape_id is None else f' id="{shape_id}"'
    styled = "" if style is None else f' style="{style}"'
    return (
        f"<w:r><w:pict><v:shape{named}{styled}{shape}>"
        f'<v:imagedata r:id="rId9"{data}/>'
        "</v:shape></w:pict></w:r>"
    )


def placements(content: bytes) -> list[tuple[object, object, object]]:
    """Each legacy image's position, where it sits and the style it said so with."""
    return [
        (position, value["placement"], value["shapeStyle"])
        for position, value in inventory(content)
    ]


# What the default fixture's record says about the image part it embeds. The
# relationship's target and the part behind it are the same member, because an
# embedded reference names one; the content type is the one the package declares
# for the extension, and the byte length is the figure the package already
# measured. ``imageDecoded``, ``ocrPerformed`` and ``ocrOutcome`` are what the
# record states about reading: under the default profile, which examines no
# embedded image, nothing opened the bytes and the outcome says so.
VML_IMAGE: dict[str, object] = {
    "kind": "vmlImage",
    "placement": "inline",
    "relationshipId": "rId9",
    "relationshipType": REL + "image",
    "target": "word/media/image1.png",
    "external": False,
    "scheme": None,
    "imagePart": "word/media/image1.png",
    "contentType": "image/png",
    "imageBytes": len(IMAGE_MEMBER["word/media/image1.png"]),
    "shapeId": "_x0000_i1026",
    "shapeStyle": VML_STYLE,
    "shapeAlt": None,
    "shapeTitle": None,
    "imageTitle": None,
    "imageDecoded": False,
    "ocrPerformed": False,
    "ocrOutcome": EMBEDDED_DISABLED,
}


def test_a_legacy_image_is_inventoried_where_it_sits_and_holds_no_text() -> None:
    """One record, where the story places the picture, naming the one
    relationship the ``v:imagedata`` referenced, what the package already knows
    about the part it reached, and the shape's own uninterpreted markup. The
    record states that nothing was decoded and no OCR was run, and it is the
    paragraph's only segment beyond the prose around it: the PNG member -- which
    is not a PNG at all -- never becomes text."""
    body = f"<w:p>{run('Before ')}{pict()}{run(' after')}</w:p>"
    content = imaged(body)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Before ", "0"),
        ("object_inventory", None, "0.v0"),
        # The picture closed the stretch it interrupted rather than joining it.
        ("body", " after", "0.t1"),
    ]
    assert inventory(content) == [("0.v0", VML_IMAGE)]
    assert [i.extraction_method for i in result.segments[1:2]] == ["inventory"]


def test_only_the_exact_absolute_declaration_moves_a_legacy_image_off_the_flow() -> (
    None
):
    """A legacy shape is in the text flow unless its own style says otherwise, so
    only the exact ``position:absolute`` pair anchors it: a style declaring sizes
    and no position, and no style at all, are both the inline shape the default
    is. The style rides along as written either way, and its absence is reported
    as the absence it is."""
    body = (
        f"<w:p>{pict(style=ANCHOR_STYLE)}</w:p>"
        f"<w:p>{pict()}</w:p>"
        f"<w:p>{pict(style=None)}</w:p>"
    )

    assert placements(imaged(body)) == [
        ("0.v0", "anchor", ANCHOR_STYLE),
        ("1.v0", "inline", VML_STYLE),
        ("2.v0", "inline", None),
    ]


def test_a_linked_legacy_image_carries_its_target_and_scheme_without_part_data() -> (
    None
):
    """An external relationship is a reference out of the package, and the
    reference is inventory: it is carried exactly as the relationship holds it,
    beside the scheme its own text names, and nothing fetches, probes or
    normalises it. There is no part inside the package, so the three part fields
    stay absent rather than being invented for it."""
    content = imaged(f"<w:p>{pict()}</w:p>", VML_LINK)

    assert inventory(content) == [
        (
            "0.v0",
            VML_IMAGE
            | {
                "target": "HTTPS://files.example/Logo.PNG",
                "external": True,
                "scheme": "https",
                "imagePart": None,
                "contentType": None,
                "imageBytes": None,
                # There is no part to examine, and the reference it named
                # instead is never opened.
                "ocrOutcome": EMBEDDED_NOT_EMBEDDED,
            },
        )
    ]
    assert extract_docx(content, capture_id=CAPTURE).limitations[
        "externalReferences"
    ] == 1


def test_the_three_claims_about_a_legacy_image_stay_three_claims() -> None:
    """The shape's title, the shape's alt text and the ``imagedata``'s Office
    title are three claims written on two elements about two things, so each is
    reported on its own, in one fixed order, after the record -- under the role
    it played and the element that declared it. Neither title stands in for the
    other, and neither stands in for the alt text beside it."""
    claimed = pict(
        shape=' title="Figure 3" alt="A bar chart of quarterly revenue"',
        data=f' {OFFICE} o:title="Revenue by quarter"',
    )
    content = imaged(f"<w:p>{claimed}</w:p>")

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (
            i.semantic_role,
            i.text,
            i.source_locator["position"],
            i.source_locator.get("altRole"),
            i.source_locator.get("altSource"),
        )
        for i in result.segments
    ] == [
        ("object_inventory", None, "0.v0", None, None),
        ("image_alt", "Figure 3", "0.v0", "title", "shape"),
        ("image_alt", "A bar chart of quarterly revenue", "0.v0", "description", "shape"),
        ("image_alt", "Revenue by quarter", "0.v0", "title", "imagedata"),
    ]
    assert inventory(content) == [
        (
            "0.v0",
            VML_IMAGE
            | {
                "shapeTitle": "Figure 3",
                "shapeAlt": "A bar chart of quarterly revenue",
                "imageTitle": "Revenue by quarter",
            },
        )
    ]


def test_the_stand_in_an_object_carries_is_not_inventoried_as_a_legacy_image() -> None:
    """The picture a ``w:object`` shows is a picture *of* the object, reached by
    a route of its own, so it is recorded as declared and never a second time as
    an image. Its ``v:imagedata`` here names a genuine image relationship the
    package holds, which would resolve if anything read it: the one record the
    container produces is what says nothing did."""
    preview = (
        f'<w:r><w:object><v:shape id="_x0000_i1025" style="{VML_STYLE}">'
        '<v:imagedata r:id="rId8"/></v:shape>'
        f'<o:OLEObject {OFFICE} Type="Embed" r:id="rId9"{CLAIMS}/>'
        "</w:object></w:r>"
    )
    content = linked(
        f"<w:p>{preview}</w:p>",
        OBJECT_RELATION,
        ("rId8", "media/image1.png", False, "image"),
        content_types=OBJECT_DEFAULTS + PNG_DEFAULT,
        members=OBJECT_MEMBERS | IMAGE_MEMBER,
    )

    assert inventory(content) == [("0.o0", EMBEDDED)]


def test_a_legacy_image_keeps_cell_order_and_a_deletion_over_one_is_preserved() -> None:
    """A picture closes the cell's current run where it sits, so the prose before
    it and the prose after it stay separate cell runs rather than joining across
    it, and the record and the claims it carried stand between them under the
    picture's own ``.v0``. A deletion wrapped around the container is kept over
    the record and over every claim it carried. Every segment keeps a locator of
    its own: a claim shares the record's position and is told apart by the role
    it played and the element that declared it."""
    shown = pict(shape=' title="Figure 3" alt="Quarterly revenue"')
    gone = pict(shape=' alt="Gone"')
    body = (
        "<w:tbl><w:tr><w:tc>"
        f"<w:p>{run('Attached ')}{shown}{run(' see above')}</w:p>"
        f"<w:p><w:del>{gone}</w:del></w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    content = imaged(body)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [
        (
            i.semantic_role,
            i.text,
            i.source_locator["position"],
            i.source_locator.get("altRole"),
            i.revision_state,
            i.visibility,
        )
        for i in result.segments
    ] == [
        ("table_cell", "Attached ", "0.r0.c0", None, "current", "visible"),
        ("object_inventory", None, "0.r0.c0.0.v0", None, "current", "visible"),
        ("image_alt", "Figure 3", "0.r0.c0.0.v0", "title", "current", "visible"),
        (
            "image_alt",
            "Quarterly revenue",
            "0.r0.c0.0.v0",
            "description",
            "current",
            "visible",
        ),
        ("table_cell", " see above", "0.r0.c0.t1", None, "current", "visible"),
        ("object_inventory", None, "0.r0.c0.1.v0", None, "deleted", "annotation"),
        ("image_alt", "Gone", "0.r0.c0.1.v0", "description", "deleted", "annotation"),
    ]
    locators = [tuple(sorted(i.source_locator.items())) for i in result.segments]
    assert len(set(locators)) == len(locators)


def test_a_legacy_image_is_the_saved_result_of_the_field_it_sits_under() -> None:
    """A field whose stored result is a legacy picture alone stored a result,
    exactly as one whose result is a DrawingML image did."""
    body = (
        f"<w:p>{fld('begin')}{instr(' INCLUDEPICTURE &quot;Logo.PNG&quot; ')}"
        f"{fld('separate')}{pict()}{fld('end')}</w:p>"
    )
    content = imaged(body)

    assert [
        (i.semantic_role, i.source_locator["position"])
        for i in extract_docx(content, capture_id=CAPTURE).segments
    ] == [("object_inventory", "0.f0"), ("object_inventory", "0.v0")]
    assert inventory(content)[0][1]["savedResultPresent"] is True


def test_a_legacy_image_inside_a_field_instruction_is_refused() -> None:
    """An instruction is field code, which shows no picture of its own, so one
    reached before the separator is refused rather than inventoried."""
    body = (
        f"<w:p>{fld('begin')}{instr(' INCLUDEPICTURE ')}{pict()}"
        f"{fld('separate')}{fld('end')}</w:p>"
    )

    with pytest.raises(OOXMLInvalid, match="instruction holds a legacy image"):
        extract_docx(imaged(body), capture_id=CAPTURE)


def test_two_legacy_images_in_one_paragraph_are_numbered_as_they_stand() -> None:
    """The counter is the paragraph's own, so each container gets its own locator
    in source order rather than both landing on the same one."""
    content = imaged(f"<w:p>{pict()}{run(' and ')}{pict()}</w:p>")

    assert [position for position, _ in inventory(content)] == ["0.v0", "0.v1"]


# A claim and a style longer than the field each rides in. The style declares no
# position, so the shape it describes is the inline one it is by default however
# much of the declaration the record keeps.
LONG_CLAIM = "C" * (_MAX_META_CHARS + 144)
LONG_STYLE = "width:" + "9" * (_MAX_TARGET_CHARS + 144) + "pt"


def test_every_legacy_claim_is_clipped_at_the_bound_its_own_field_has() -> None:
    """The shape's identifier and style and all three claims ride in the one
    bounded JSON record, so each is clipped at the bound its own field has -- the
    style at the target bound it shares with a reference, the rest at the
    metadata bound -- rather than carried at whatever length the file wrote it.
    Each stays apart from the others rather than joining, and the style is still
    read whole for where the shape sits before the record keeps a prefix of
    it."""
    shape = f' alt="{LONG_CLAIM}" title="{LONG_CLAIM}"'
    data = f' {OFFICE} o:title="{LONG_CLAIM}"'
    content = imaged(
        f"<w:p>{pict(shape_id=LONG_CLAIM, style=LONG_STYLE, shape=shape, data=data)}"
        "</w:p>"
    )

    assert inventory(content) == [
        (
            "0.v0",
            VML_IMAGE
            | {
                "placement": "inline",
                "shapeStyle": LONG_STYLE[:_MAX_TARGET_CHARS],
                "shapeId": "C" * _MAX_META_CHARS,
                "shapeAlt": "C" * _MAX_META_CHARS,
                "shapeTitle": "C" * _MAX_META_CHARS,
                "imageTitle": "C" * _MAX_META_CHARS,
            },
        )
    ]


def test_a_legacy_claim_written_empty_stays_in_the_record_and_invents_no_text() -> None:
    """A claim nothing wrote and a claim written empty are different facts, and
    the record keeps them apart rather than folding one into the other. Neither
    is text about the image, so neither becomes an ``image_alt`` segment: no text
    is invented for an image whose author described it with none, least of all
    out of the shape's identifier."""
    empty = pict(shape=' alt="" title=""', data=f' {OFFICE} o:title=""')
    content = imaged(f"<w:p>{pict()}</w:p><w:p>{empty}</w:p>")

    result = extract_docx(content, capture_id=CAPTURE)

    assert [(i.semantic_role, i.source_locator["position"]) for i in result.segments] == [
        ("object_inventory", "0.v0"),
        ("object_inventory", "1.v0"),
    ]
    assert [
        (value["shapeAlt"], value["shapeTitle"], value["imageTitle"])
        for _, value in inventory(content)
    ] == [(None, None, None), ("", "", "")]


def test_a_legacy_claim_in_another_vocabulary_claims_nothing_about_this_image() -> None:
    """The shape's four declarations are unqualified, as VML writes them, and the
    ``imagedata``'s title is Office-qualified, as the drawing extension writes
    it. So the same local name carried in another vocabulary is that vocabulary's
    extension and claims nothing here: each field is reported absent rather than
    filled from the foreign attribute sitting beside it -- the style included, so
    a foreign ``position`` does not move the shape off the text flow -- no claim
    becomes a segment, and the image itself still resolves, because an extension
    nobody here reads refuses nothing."""
    foreign = (
        f" {OTHER} z:id=\"claimed\" z:style=\"position:absolute\""
        ' z:alt="claimed" z:title="claimed"'
    )
    claimed = pict(shape_id=None, style=None, shape=foreign, data=' z:title="claimed"')
    content = imaged(f"<w:p>{claimed}</w:p>")

    result = extract_docx(content, capture_id=CAPTURE)

    assert [i.semantic_role for i in result.segments] == ["object_inventory"]
    assert inventory(content) == [
        (
            "0.v0",
            VML_IMAGE
            | {
                "placement": "inline",
                "shapeId": None,
                "shapeStyle": None,
                "shapeAlt": None,
                "shapeTitle": None,
                "imageTitle": None,
            },
        )
    ]


def shaped(
    inside: str = '<v:imagedata r:id="rId9"/>',
    name: str = "v:shape",
    attrs: str = "",
) -> str:
    """One shape of the given qualified name, holding the given markup."""
    return f"<{name}{attrs}>{inside}</{name}>"


def vml_typed(kind: str, body: str | None = None) -> bytes:
    """The default picture document, whose one relationship is exactly ``kind``.

    ``rels`` names the types OOXML defines by their last segment; a type is
    admitted or refused on the whole URI it was written as, so these are spelled
    out in full instead.
    """
    return docx(
        body if body is not None else f"<w:p>{pict()}</w:p>",
        {
            "word/_rels/document.xml.rels": (
                '<Relationships><Relationship Id="rId9" '
                f'Type="{kind}" Target="media/image1.png"/></Relationships>'
            )
        },
        styles=False,
        content_types=PNG_DEFAULT,
        members=IMAGE_MEMBER,
    )


def test_only_the_two_wordprocessing_namespaces_admit_a_picture_container() -> None:
    """The container is admitted on its qualified name alone, unlike the local
    names the rest of the story is read by. An element merely called ``pict`` in
    another vocabulary, or in none at all, shows nothing whatever it holds: it
    stays the transparent wrapper every unrecognised element is, so the prose
    inside it is still read and the shape it carries reaches no record."""
    for name, attrs in (("z:pict", " " + OTHER), ("pict", "")):
        content = imaged("<w:p>" + holder(shaped() + CAPTION, name, attrs) + "</w:p>")

        assert read(content) == [("body", "stand-in caption", "0")]
        assert inventory(content) == []


def test_the_strict_container_reference_and_type_reach_the_same_record() -> None:
    """Strict is the same declaration at every step that has a Strict spelling:
    the container in the Strict WordprocessingML main namespace, the id in the
    Strict relationships namespace, and the relationship type in the Strict
    spelling of the one type OOXML defines for an image. The shape itself keeps
    its one VML namespace either way, and the record is the Transitional one
    exactly, down to the type being reported as the file wrote it."""
    strict_id = f' r2:id="rId9" xmlns:r2="{STRICT_REL_NS}"'
    inside = shaped(
        f"<v:imagedata{strict_id}/>", attrs=f' id="{SHAPE_ID}" style="{VML_STYLE}"'
    )
    wrapper = holder(inside, "ws:pict", f' xmlns:ws="{STRICT_MAIN_NS}"')
    body = f"<w:p>{wrapper}</w:p>"
    kind = STRICT_REL_NS + "/image"

    assert inventory(vml_typed(kind, body)) == [
        ("0.v0", VML_IMAGE | {"relationshipType": kind})
    ]


def test_only_the_two_direct_hops_reach_a_standalone_legacy_image() -> None:
    """The route in is the ``v:shape`` directly below the container and the
    ``v:imagedata`` directly below that shape, and no deeper. A shape inside a
    group, one carried under an alternate-content choice, one sharing the local
    name in another vocabulary or in none, another VML element entirely, an
    image reference with no shape above it, one a step below the shape rather
    than directly under it, and one sharing the reference's local name
    elsewhere are each passed over -- so the container shows no standalone image
    rather than one found elsewhere, and stays the transparent wrapper it is
    today, prose and all."""
    for inside in (
        f"<v:group>{shaped()}</v:group>",
        f"<mc:AlternateContent><mc:Choice>{shaped()}</mc:Choice></mc:AlternateContent>",
        shaped(name="z:shape", attrs=" " + OTHER),
        shaped(name="shape"),
        shaped(name="v:rect"),
        '<v:imagedata r:id="rId9"/>',
        shaped('<v:fill><v:imagedata r:id="rId9"/></v:fill>'),
        shaped(f'<z:imagedata {OTHER} r:id="rId9"/>'),
        shaped('<imagedata r:id="rId9"/>'),
    ):
        content = imaged("<w:p>" + holder(inside + CAPTION, "w:pict") + "</w:p>")

        assert read(content) == [("body", "stand-in caption", "0")]
        assert inventory(content) == []


def test_a_picture_showing_two_shapes_or_two_images_is_refused() -> None:
    """A container shows one image, so two shapes directly below it, or two
    image references directly below one shape, leave which image it shows
    unanswerable -- and nothing here chooses between them even where both spell
    out the same image."""
    for inside, reason in (
        (shaped() + shaped(), "picture declares more than one shape"),
        (
            shaped('<v:imagedata r:id="rId9"/><v:imagedata r:id="rId9"/>'),
            "picture shape declares more than one image",
        ),
    ):
        content = imaged("<w:p>" + holder(inside, "w:pict") + "</w:p>")

        with pytest.raises(OOXMLInvalid, match="DOCX " + reason):
            extract_docx(content, capture_id=CAPTURE)


def test_the_image_reference_must_be_one_relationship_namespaced_attribute() -> None:
    """The id is defined in the relationships namespace, so one written in no
    namespace at all, or in a vocabulary that defines no such attribute, names
    no relationship whatever the package would resolve the text to -- and image
    data naming none declares an image it does not identify. One written in each
    of the two namespaces that do carry it is two ids rather than a repeat, even
    where both spell out the same relationship."""
    for ident, reason in (
        ("", "names no image relationship"),
        (' id="rId9"', "names no image relationship"),
        (" " + OTHER + ' z:id="rId9"', "names no image relationship"),
        (
            f' r:id="rId9" r2:id="rId9" xmlns:r2="{STRICT_REL_NS}"',
            "names more than one image relationship",
        ),
    ):
        content = imaged(
            "<w:p>" + holder(shaped(f"<v:imagedata{ident}/>"), "w:pict") + "</w:p>"
        )

        with pytest.raises(OOXMLInvalid, match="DOCX VML image " + reason):
            extract_docx(content, capture_id=CAPTURE)


def test_a_legacy_image_names_its_relationship_by_id_and_never_by_target() -> None:
    """The id is the whole reference. A package holding the very image the shape
    displays, under another id, holds no relationship the shape names: nothing
    searches the story's relationships by target, content type or resemblance,
    so the image is refused rather than resolved to the match sitting beside
    it."""
    content = imaged(
        f"<w:p>{pict()}</w:p>", ("rId3", "media/image1.png", False, "image")
    )

    with pytest.raises(
        OOXMLInvalid, match="DOCX VML image names a missing image relationship"
    ):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    "kind",
    [
        # Another type entirely reaches no image at all, whatever the target
        # behind it holds: an OLE object, a media part and a hyperlink are each
        # something that is not the image a shape displays.
        REL + "oleObject",
        REL + "media",
        REL + "hyperlink",
        # A type that merely ends the same way is not the one OOXML defines:
        # the last segment is not the type, the whole URI is.
        "http://example.invalid/relationships/image",
        # The defined URI under another scheme is another URI, and the path does
        # not fold under RFC 3986, so a segment differing in case is a different
        # type rather than the same one spelled loudly.
        "https://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        REL + "Image",
    ],
)
def test_a_relationship_of_another_type_names_no_legacy_image(kind: str) -> None:
    """The relationship has to be one of the two URIs OOXML defines for an
    image. The image is refused rather than read through a reference that
    qualifies the part it reaches as something else."""
    with pytest.raises(
        OOXMLInvalid, match="DOCX VML image names an image relationship of another type"
    ):
        extract_docx(vml_typed(kind), capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("content_types", "members", "reason"),
    [
        # Containment and existence are settled while the package is validated,
        # which is why nothing re-checks them here.
        (PNG_DEFAULT, None, "escapes or misses the package"),
        # A part the package declares nothing about is not a part it declares an
        # image.
        ("", IMAGE_MEMBER, "DOCX VML image part is not declared an image"),
        (
            '<Default Extension="png" ContentType="text/plain"/>',
            IMAGE_MEMBER,
            "DOCX VML image part is not declared an image",
        ),
    ],
)
def test_an_internal_target_has_to_be_a_member_the_package_declares_an_image(
    content_types: str, members: dict[str, bytes] | None, reason: str
) -> None:
    """An embedded reference names a part inside the package, so the part has to
    be there and has to be declared an image: inventorying anything else as one
    would say the package holds an image where it holds something else. Neither
    is reported with a type nothing declared or a length nothing measured."""
    content = linked(
        f"<w:p>{pict()}</w:p>",
        IMAGE_RELATION,
        content_types=content_types,
        members=members,
    )

    with pytest.raises(OOXMLInvalid, match=reason):
        extract_docx(content, capture_id=CAPTURE)


# A target outside the package written as a relative reference, spelled exactly
# like the member the package holds. External is external: it names no scheme,
# and nothing here invents one, resolves it against the package or reads the
# member whose name it happens to share.
RELATIVE_IMAGE_LINK = ("rId9", "media/image1.png", True, "image")


def test_a_relative_external_target_names_no_scheme_and_is_never_opened() -> None:
    """A reference out of the package is carried exactly as the relationship
    holds it and is counted as the external reference it is, which is the whole
    of what happens to it: nothing fetches, probes or normalises it, and it is
    not resolved against the package even where a member of that very name sits
    inside. There is no part inside the package, so the three part fields stay
    absent rather than being filled from the member sharing the name."""
    content = imaged(f"<w:p>{pict()}</w:p>", RELATIVE_IMAGE_LINK)

    result = extract_docx(content, capture_id=CAPTURE)

    assert [(i.semantic_role, i.text) for i in result.segments] == [
        ("object_inventory", None)
    ]
    assert inventory(content) == [
        (
            "0.v0",
            VML_IMAGE
            | {
                "target": "media/image1.png",
                "external": True,
                "scheme": None,
                "imagePart": None,
                "contentType": None,
                "imageBytes": None,
                "ocrOutcome": EMBEDDED_NOT_EMBEDDED,
            },
        )
    ]
    assert result.limitations["externalReferences"] == 1


def test_a_shape_declaring_two_different_positions_is_refused() -> None:
    """A style declaring ``position`` twice with two different values leaves
    where the shape sits unanswerable, and nothing here chooses between them or
    takes whichever came last. The same value declared twice contradicts
    nothing, so it is the one position it states."""
    conflicting = "position:absolute;position:relative"

    with pytest.raises(
        OOXMLInvalid, match="DOCX VML image declares more than one position"
    ):
        extract_docx(
            imaged(f"<w:p>{pict(style=conflicting)}</w:p>"), capture_id=CAPTURE
        )

    repeated = "position:absolute;position:absolute"
    assert placements(imaged(f"<w:p>{pict(style=repeated)}</w:p>")) == [
        ("0.v0", "anchor", repeated)
    ]


# A table's grid is the table's own claim: the declared ``tblGrid``, each row's
# ``gridSpan`` and each cell's ``vMerge``. The helpers below write exactly those
# three and nothing else, so what a test varies is the geometry under test.
STRICT_W = "http://purl.oclc.org/ooxml/wordprocessingml/main"
RESTART = '<w:vMerge w:val="restart"/>'
CONTINUE = '<w:vMerge w:val="continue"/>'
GRID_KEYS = (
    "tablePath",
    "tableColumns",
    "tableGridWidths",
    "sourceRow",
    "sourceCell",
    "gridRow",
    "gridColumn",
    "rowSpan",
    "columnSpan",
    "mergeState",
    "mergeAnchor",
)


def span(columns: int | str) -> str:
    return f'<w:gridSpan w:val="{columns}"/>'


def tc(text: str | None = None, properties: str = "") -> str:
    """One cell: its declared properties, then its prose, or no prose at all."""
    body = "<w:p/>" if text is None else p(text)
    declared = f"<w:tcPr>{properties}</w:tcPr>" if properties else ""
    return f"<w:tc>{declared}{body}</w:tc>"


def tr(*cells: str) -> str:
    return f"<w:tr>{''.join(cells)}</w:tr>"


def tbl(*rows: str, columns: int | None = None, grid: str | None = None) -> str:
    declared = grid if grid is not None else _declared(columns)
    return f"<w:tbl>{declared}{''.join(rows)}</w:tbl>"


def _declared(columns: int | None) -> str:
    if columns is None:
        return ""
    return f"<w:tblGrid>{'<w:gridCol/>' * columns}</w:tblGrid>"


def cells(content: bytes) -> list[tuple[str, object, dict[str, object]]]:
    """Each table-cell run as its text, its position and the grid it carries."""
    return [
        (
            item.text or "",
            item.source_locator["position"],
            {key: item.source_locator[key] for key in GRID_KEYS},
        )
        for item in extract_docx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "table_cell"
    ]


def at(
    row: int,
    cell: int,
    column: int,
    *,
    columns: int | None,
    spans: tuple[int, int] = (1, 1),
    state: str = "none",
    anchor: tuple[int, int] | None = None,
    path: str = "0",
    widths: tuple[int | None, ...] | None = None,
) -> dict[str, object]:
    """The grid a cell at these coordinates is expected to carry."""
    anchored = anchor if anchor is not None else (row, cell)
    return {
        "tablePath": path,
        "tableColumns": columns,
        "tableGridWidths": (
            widths
            if widths is not None
            else None
            if columns is None
            else (None,) * columns
        ),
        "sourceRow": row,
        "sourceCell": cell,
        "gridRow": row,
        "gridColumn": column,
        "rowSpan": spans[0],
        "columnSpan": spans[1],
        "mergeState": state,
        "mergeAnchor": f"{path}.r{anchored[0]}.c{anchored[1]}",
    }


def test_a_declared_grid_gives_every_cell_source_and_logical_coordinates() -> None:
    """With nothing merged, the logical column follows the source cell, and each
    cell is its own segment carrying the column count the table declared. The
    locator keys are exactly these: no cell is flattened into a row, and the
    first row claims no header role the table never declared."""
    body = tbl(tr(tc("a"), tc("b")), tr(tc("c"), tc("d")), columns=2)

    result = extract_docx(docx(body), capture_id=CAPTURE)

    assert cells(docx(body)) == [
        ("a", "0.r0.c0", at(0, 0, 0, columns=2)),
        ("b", "0.r0.c1", at(0, 1, 1, columns=2)),
        ("c", "0.r1.c0", at(1, 0, 0, columns=2)),
        ("d", "0.r1.c1", at(1, 1, 1, columns=2)),
    ]
    assert set(result.segments[0].source_locator) == {
        "captureId",
        "part",
        "story",
        "position",
        *GRID_KEYS,
    }


def test_an_undeclared_grid_leaves_the_column_count_unclaimed() -> None:
    """A table without a ``tblGrid`` declares no width, so none is reported and
    none is inferred from the widest row. The cells still have their own grid
    coordinates, because those come from the spans the rows declare."""
    body = tbl(tr(tc("a"), tc("b")), tr(tc("c")))

    assert cells(docx(body)) == [
        ("a", "0.r0.c0", at(0, 0, 0, columns=None)),
        ("b", "0.r0.c1", at(0, 1, 1, columns=None)),
        ("c", "0.r1.c0", at(1, 0, 0, columns=None)),
    ]


def test_a_horizontal_merge_is_one_cell_over_two_grid_columns() -> None:
    """A ``gridSpan`` cell is emitted once, as the anchor of a merge two columns
    wide, and the next cell in its row starts past it. The text is not repeated
    at the covered column, and the row below is unaffected."""
    body = tbl(
        tr(tc("wide", span(2)), tc("right")),
        tr(tc("a"), tc("b"), tc("c")),
        columns=3,
    )

    assert cells(docx(body)) == [
        ("wide", "0.r0.c0", at(0, 0, 0, columns=3, spans=(1, 2), state="anchor")),
        ("right", "0.r0.c1", at(0, 1, 2, columns=3)),
        ("a", "0.r1.c0", at(1, 0, 0, columns=3)),
        ("b", "0.r1.c1", at(1, 1, 1, columns=3)),
        ("c", "0.r1.c2", at(1, 2, 2, columns=3)),
    ]


def test_a_vertical_merge_emits_its_text_once_at_the_restart() -> None:
    """The continuation holds no prose of its own, so the merged text appears
    once, at the cell that restarted the merge, and that cell reports the two
    rows the merge spans. Nothing is emitted at the covered coordinate."""
    body = tbl(
        tr(tc("tall", RESTART), tc("top")),
        tr(tc(None, CONTINUE), tc("bottom")),
        columns=2,
    )

    assert cells(docx(body)) == [
        ("tall", "0.r0.c0", at(0, 0, 0, columns=2, spans=(2, 1), state="anchor")),
        ("top", "0.r0.c1", at(0, 1, 1, columns=2)),
        ("bottom", "0.r1.c1", at(1, 1, 1, columns=2)),
    ]


def test_an_omitted_merge_value_continues_the_merge_above_it() -> None:
    """``vMerge`` defaults to continuing, which is how Word writes the covered
    rows of a vertical merge, so the bare element reads as the continuation it
    is rather than as a merge restarting at every row."""
    bare = tbl(
        tr(tc("tall", RESTART)), tr(tc(None, "<w:vMerge/>")), tr(tc(None, CONTINUE))
    )

    assert cells(docx(bare)) == [
        ("tall", "0.r0.c0", at(0, 0, 0, columns=None, spans=(3, 1), state="anchor"))
    ]


def test_empty_continuation_paragraph_properties_are_not_content() -> None:
    empty = (
        '<w:tc><w:tcPr><w:vMerge/></w:tcPr>'
        '<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr><w:r><w:rPr/></w:r></w:p>'
        "</w:tc>"
    )
    body = tbl(tr(tc("tall", RESTART)), tr(empty), columns=1)
    assert cells(docx(body)) == [
        ("tall", "0.r0.c0", at(0, 0, 0, columns=1, spans=(2, 1), state="anchor"))
    ]


def test_text_in_a_vertical_continuation_is_refused() -> None:
    """Covered content cannot be emitted twice or silently discarded."""
    body = tbl(
        tr(tc("tall", RESTART), tc("top")),
        tr(tc("stray", CONTINUE), tc("bottom")),
        columns=2,
    )

    with pytest.raises(OOXMLInvalid, match="continuation contains content"):
        extract_docx(docx(body), capture_id=CAPTURE)


def test_declared_grid_widths_are_retained_without_invention() -> None:
    body = tbl(
        tr(tc("a"), tc("b")),
        grid='<w:tblGrid><w:gridCol w:w="1440"/><w:gridCol/></w:tblGrid>',
    )
    assert cells(docx(body)) == [
        ("a", "0.r0.c0", at(0, 0, 0, columns=2, widths=(1440, None))),
        ("b", "0.r0.c1", at(0, 1, 1, columns=2, widths=(1440, None))),
    ]


@pytest.mark.parametrize("width", ["-1", "1.5", "wide", "1000001"])
def test_malformed_grid_width_is_refused(width: str) -> None:
    body = tbl(
        tr(tc("a")),
        grid=f'<w:tblGrid><w:gridCol w:w="{width}"/></w:tblGrid>',
    )
    with pytest.raises(OOXMLInvalid, match="column width is malformed"):
        extract_docx(docx(body), capture_id=CAPTURE)


def test_a_rectangular_merge_carries_both_spans_at_one_anchor() -> None:
    """A merge that is both wide and tall is one anchor over a rectangle: its
    text is emitted once, with the two spans the geometry declares, and the
    three covered coordinates repeat none of it."""
    body = tbl(
        tr(tc("block", RESTART + span(2)), tc("side")),
        tr(tc(None, CONTINUE + span(2)), tc("under")),
        tr(tc("a"), tc("b"), tc("c")),
        columns=3,
    )

    assert cells(docx(body)) == [
        ("block", "0.r0.c0", at(0, 0, 0, columns=3, spans=(2, 2), state="anchor")),
        ("side", "0.r0.c1", at(0, 1, 2, columns=3)),
        ("under", "0.r1.c1", at(1, 1, 2, columns=3)),
        ("a", "0.r2.c0", at(2, 0, 0, columns=3)),
        ("b", "0.r2.c1", at(2, 1, 1, columns=3)),
        ("c", "0.r2.c2", at(2, 2, 2, columns=3)),
    ]


def test_a_merge_ends_where_the_column_stops_continuing_it() -> None:
    """A column that holds an ordinary cell closes the merge above it, so the
    merge spans only the contiguous rows that continued it and the ordinary cell
    is a cell of its own rather than part of the region."""
    body = tbl(
        tr(tc("tall", RESTART)),
        tr(tc(None, CONTINUE)),
        tr(tc("own")),
        columns=1,
    )

    assert cells(docx(body)) == [
        ("tall", "0.r0.c0", at(0, 0, 0, columns=1, spans=(2, 1), state="anchor")),
        ("own", "0.r2.c0", at(2, 0, 0, columns=1)),
    ]


def test_a_nested_table_keeps_its_own_grid_under_its_cell_path() -> None:
    """The inner table's geometry is its own: its path is the block path of the
    cell it hangs under, so its anchor citations name its own cells and never
    the outer table's, and the outer merge is unaffected by what a cell holds."""
    inner = tbl(
        tr(tc("in", RESTART), tc("beside")),
        tr(tc(None, CONTINUE), tc("below")),
        columns=2,
    )
    body = tbl(
        tr(tc("outer", span(2)), f"<w:tc>{p('holder')}{inner}</w:tc>"),
        columns=3,
    )

    assert cells(docx(body)) == [
        ("outer", "0.r0.c0", at(0, 0, 0, columns=3, spans=(1, 2), state="anchor")),
        ("holder", "0.r0.c1", at(0, 1, 2, columns=3)),
        (
            "in",
            "0.r0.c1.1.r0.c0",
            at(0, 0, 0, columns=2, spans=(2, 1), state="anchor", path="0.r0.c1.1"),
        ),
        ("beside", "0.r0.c1.1.r0.c1", at(0, 1, 1, columns=2, path="0.r0.c1.1")),
        ("below", "0.r0.c1.1.r1.c1", at(1, 1, 1, columns=2, path="0.r0.c1.1")),
    ]


def test_a_strict_table_declares_the_same_grid_as_a_transitional_one() -> None:
    """The grid, the spans and the merge are read by qualified name in both
    WordprocessingML profiles, so a table written in the Strict vocabulary
    reaches exactly the geometry the Transitional one does."""
    rows = (
        tr(tc("block", RESTART + span(2)), tc("side")),
        tr(tc(None, CONTINUE + span(2)), tc("under")),
    )
    transitional = tbl(*rows, columns=3)
    strict = transitional.replace("<w:tbl>", f'<w:tbl xmlns:w="{STRICT_W}">', 1)

    assert cells(docx(strict)) == cells(docx(transitional))
    assert cells(docx(strict)) == [
        ("block", "0.r0.c0", at(0, 0, 0, columns=3, spans=(2, 2), state="anchor")),
        ("side", "0.r0.c1", at(0, 1, 2, columns=3)),
        ("under", "0.r1.c1", at(1, 1, 2, columns=3)),
    ]


def test_a_lookalike_grid_span_or_merge_declares_no_geometry() -> None:
    """Geometry is a structural claim about the table, so it is read by qualified
    name alone. An element merely called ``tblGrid``, ``gridSpan`` or ``vMerge``
    in another vocabulary declares nothing: the column count stays unclaimed,
    the span stays one, and the lookalike merge opens none -- so it is not even
    refused as the orphan continuation a real one would be."""
    foreign = 'xmlns:x="urn:omnivia-test:lookalike"'
    body = (
        f"<w:tbl {foreign}>"
        "<x:tblGrid><x:gridCol/><x:gridCol/><x:gridCol/></x:tblGrid>"
        + tr(
            f'<w:tc><w:tcPr><x:gridSpan x:val="2"/></w:tcPr>{p("a")}</w:tc>',
            f"<w:tc><w:tcPr><x:vMerge/></w:tcPr>{p('b')}</w:tc>",
        )
        + "</w:tbl>"
    )

    assert cells(docx(body)) == [
        ("a", "0.r0.c0", at(0, 0, 0, columns=None)),
        ("b", "0.r0.c1", at(0, 1, 1, columns=None)),
    ]


@pytest.mark.parametrize(
    ("properties", "message"),
    [
        ("<w:gridSpan/>", "horizontal cell span is malformed"),
        (span(0), "horizontal cell span is malformed"),
        (span(-1), "horizontal cell span is malformed"),
        (span("two"), "horizontal cell span is malformed"),
        (span(""), "horizontal cell span is malformed"),
        ('<w:gridSpan val="2"/>', "horizontal cell span is malformed"),
        (span(_MAX_DOCX_TABLE_COLUMNS + 1), "horizontal cell span is too wide"),
        ('<w:vMerge w:val="merge"/>', "vertical cell merge is malformed"),
        ('<w:vMerge w:val=""/>', "vertical cell merge is malformed"),
        (span(2) + span(2), "declares its span twice"),
        (RESTART + RESTART, "declares its merge twice"),
    ],
)
def test_a_malformed_span_or_merge_is_refused(properties: str, message: str) -> None:
    """A span that states no readable count and a merge that states no defined
    state are each refused rather than guessed at, defaulted to one, or taken
    from whichever declaration came last. The ``val`` is read by qualified name,
    so an unqualified one carries no count at all."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_docx(docx(tbl(tr(tc("a", properties)))), capture_id=CAPTURE)


def test_a_geometry_value_declared_in_both_profiles_is_refused() -> None:
    """One ``gridSpan`` carrying a Transitional and a Strict ``val`` states two
    counts, and nothing here chooses between them or takes whichever came
    last."""
    both = f'<w:gridSpan xmlns:s="{STRICT_W}" w:val="2" s:val="3"/>'

    with pytest.raises(OOXMLInvalid, match="declares two values"):
        extract_docx(docx(tbl(tr(tc("a", both)))), capture_id=CAPTURE)


def test_a_geometry_value_from_the_other_profile_is_refused() -> None:
    mismatched = f'<w:gridSpan xmlns:s="{STRICT_W}" s:val="2"/>'
    with pytest.raises(OOXMLInvalid, match="value uses another profile"):
        extract_docx(docx(tbl(tr(tc("a", mismatched)))), capture_id=CAPTURE)


def test_a_cell_or_table_declaring_its_geometry_twice_is_refused() -> None:
    """Two ``tcPr`` leave a cell with no single geometry and two ``tblGrid``
    leave a table with no single declared width; neither is resolved by order."""
    twice = f"<w:tc><w:tcPr>{span(2)}</w:tcPr><w:tcPr/>{p('a')}</w:tc>"
    with pytest.raises(OOXMLInvalid, match="declares its properties twice"):
        extract_docx(docx(tbl(tr(twice))), capture_id=CAPTURE)

    grids = _declared(2) + _declared(3)
    with pytest.raises(OOXMLInvalid, match="declares more than one grid"):
        extract_docx(docx(tbl(tr(tc("a")), grid=grids)), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "rows",
    [
        # A continuation in the first row continues nothing at all.
        (tr(tc(None, CONTINUE)),),
        # The cell above is an ordinary cell, so it opened no merge.
        (tr(tc("a")), tr(tc(None, CONTINUE))),
        # The merge the first row opened was closed by the row between.
        (tr(tc("a", RESTART)), tr(tc("b")), tr(tc(None, CONTINUE))),
        # The anchor above starts at column 0, so column 1 continues nothing.
        (tr(tc("a", RESTART + span(2))), tr(tc("b"), tc(None, CONTINUE))),
        # The anchor above is one column wide and this continuation is two.
        (tr(tc("a", RESTART)), tr(tc(None, CONTINUE + span(2)))),
        # The anchor above is two columns wide and this continuation is one.
        (tr(tc("a", RESTART + span(2))), tr(tc(None, CONTINUE))),
    ],
)
def test_a_continuation_without_a_matching_anchor_is_refused(
    rows: tuple[str, ...],
) -> None:
    """A vertical merge continues a merge the same column opened in the row
    directly above, at the same width, or it continues nothing. An orphan and an
    inconsistent overlap are both refused rather than anchored at whatever cell
    happens to be nearby."""
    with pytest.raises(OOXMLInvalid, match="vertical cell merge continues nothing"):
        extract_docx(docx(tbl(*rows, columns=3)), capture_id=CAPTURE)


def test_a_row_reaching_past_the_declared_grid_is_refused() -> None:
    """The declared ``tblGrid`` is the table's own statement of its width, so a
    row whose spans reach past it is inconsistent geometry rather than a wider
    grid to infer. A row that exactly fills the grid is not."""
    over = tbl(tr(tc("a"), tc("b", span(2))), columns=2)
    with pytest.raises(OOXMLInvalid, match="row is wider than its grid"):
        extract_docx(docx(over), capture_id=CAPTURE)

    exact = tbl(tr(tc("a"), tc("b", span(2))), columns=3)
    assert [text for text, _, _ in cells(docx(exact))] == ["a", "b"]


def test_table_geometry_is_bounded() -> None:
    """A table wider or longer than OmniVia reads is refused before any of its
    cells is read, and an undeclared grid is bounded the same way rather than
    left to grow with the rows."""
    wide = _declared(_MAX_DOCX_TABLE_COLUMNS + 1)
    with pytest.raises(OOXMLInvalid, match="declares too many columns"):
        extract_docx(docx(tbl(tr(tc("a")), grid=wide)), capture_id=CAPTURE)

    long = tbl(*("<w:tr/>",) * (_MAX_DOCX_TABLE_ROWS + 1))
    with pytest.raises(OOXMLInvalid, match="declares too many rows"):
        extract_docx(docx(long), capture_id=CAPTURE)

    undeclared = tbl(tr(tc("a", span(_MAX_DOCX_TABLE_COLUMNS)), tc("b")))
    with pytest.raises(OOXMLInvalid, match="row is wider than its grid"):
        extract_docx(docx(undeclared), capture_id=CAPTURE)


def test_a_table_holding_more_cells_than_the_segment_cap_is_refused() -> None:
    """The whole grid is planned before a cell is read, so the plan is bounded by
    what the extraction could emit from it. A cell holding no prose emits one
    empty record rather than nothing, so the plan's own bound and the segment
    cap now agree; the plan is still what refuses this table, before a single
    coordinate is walked."""
    row = tr(*("<w:tc/>",) * _MAX_DOCX_TABLE_COLUMNS)
    rows = MAX_SEGMENTS // _MAX_DOCX_TABLE_COLUMNS + 1

    with pytest.raises(OOXMLInvalid, match="DOCX table declares too many cells"):
        extract_docx(docx(tbl(*(row,) * rows)), capture_id=CAPTURE)


def test_a_merged_cell_keeps_its_grid_across_every_run_it_emits() -> None:
    """A merge's geometry belongs to the cell, not to one stretch of its prose,
    so every run the cell emits carries the same grid even where a deletion, a
    hidden stretch and an inventoried hyperlink split it. The interrupting
    segments keep their own locators and claim no grid of their own, and the
    covered row the merge reaches into closes with its own empty record rather
    than repeating any of the anchor's prose."""
    interrupted = (
        p("first") + f"<w:p>{run('kept ')}<w:del>{run('gone', 'delText')}</w:del>"
        f"{hidden('secret')}{run(' tail')}</w:p>"
    )
    body = tbl(
        tr(f"<w:tc><w:tcPr>{RESTART + span(2)}</w:tcPr>{interrupted}</w:tc>"),
        tr(tc(None, CONTINUE + span(2))),
        columns=2,
    )
    anchor = at(0, 0, 0, columns=2, spans=(2, 2), state="anchor")

    result = extract_docx(docx(body), capture_id=CAPTURE)

    assert cells(docx(body)) == [
        ("first\nkept ", "0.r0.c0", anchor),
        (" tail", "0.r0.c0.t1", anchor),
    ]
    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("table_cell", "first\nkept ", "0.r0.c0"),
        ("deleted_text", "gone", "0.r0.c0.1.d0"),
        ("hidden_content", "secret", "0.r0.c0.1.v0"),
        ("table_cell", " tail", "0.r0.c0.t1"),
        ("object_inventory", None, "0.r1.c0"),
    ]
    assert [
        sorted(set(i.source_locator) & set(GRID_KEYS)) for i in result.segments[1:3]
    ] == [[], []]


def test_a_merged_cell_keeps_its_grid_across_a_move_revision() -> None:
    """A move reaching into a merged cell is a revision like any other: the
    text moved in keeps the cell's role and its grid, the text moved out is
    reported as the deletion it is and claims no grid of its own, and the two
    halves never join into one sentence the cell never held."""
    moved = (
        p("stays")
        + f"<w:p>{run('kept ')}<w:moveTo>{run('arrived')}</w:moveTo>"
        f"<w:moveFrom>{run('departed', 'delText')}</w:moveFrom>{run(' tail')}</w:p>"
    )
    body = tbl(
        tr(f"<w:tc><w:tcPr>{RESTART + span(2)}</w:tcPr>{moved}</w:tc>"),
        tr(tc(None, CONTINUE + span(2))),
        columns=2,
    )
    anchor = at(0, 0, 0, columns=2, spans=(2, 2), state="anchor")

    assert cells(docx(body)) == [
        ("stays\nkept ", "0.r0.c0", anchor),
        ("arrived", "0.r0.c0.t1", anchor),
        (" tail", "0.r0.c0.t2", anchor),
    ]
    result = extract_docx(docx(body), capture_id=CAPTURE)
    assert [
        (i.semantic_role, i.text, i.revision_state, i.source_locator["position"])
        for i in result.segments
    ] == [
        ("table_cell", "stays\nkept ", "current", "0.r0.c0"),
        ("table_cell", "arrived", "inserted", "0.r0.c0.t1"),
        ("deleted_text", "departed", "deleted", "0.r0.c0.1.d0"),
        ("table_cell", " tail", "current", "0.r0.c0.t2"),
        ("object_inventory", None, "current", "0.r1.c0"),
    ]
    assert sorted(set(result.segments[2].source_locator) & set(GRID_KEYS)) == []


# A coordinate the grid declares is a fact about the table whether or not prose
# stands at it. A cell emitting no run closes with one empty record, and a table
# holding no cell at all takes one record of its own; the helper below reads
# exactly those two records and nothing else the walk inventories.
def structural(content: bytes) -> list[tuple[object, object]]:
    """Each table structure record as its position and its structured value."""
    return [
        (item.source_locator["position"], item.structured_value)
        for item in extract_docx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and item.structured_value["kind"] in {"docxTableCell", "docxTable"}
    ]


def cell_record(
    row: int, cell: int, column: int, empty: bool = True, **geometry: object
) -> dict[str, object]:
    """The structural record expected at these coordinates."""
    return {
        "kind": "docxTableCell",
        "empty": empty,
        **at(row, cell, column, **geometry),  # type: ignore[arg-type]
    }


def test_an_empty_cell_among_text_cells_keeps_its_own_coordinates() -> None:
    """A cell holding no prose used to vanish, leaving a row the grid declared
    with no trace of one of its columns. The text cells read exactly as before
    and the empty one is recorded beside them, in source order, at the position
    its own text would have stood at."""
    body = tbl(tr(tc("a"), tc(None)), tr(tc(None), tc("d")), columns=2)

    result = extract_docx(docx(body), capture_id=CAPTURE)

    assert [
        (item.semantic_role, item.text, item.source_locator["position"])
        for item in result.segments
    ] == [
        ("table_cell", "a", "0.r0.c0"),
        ("object_inventory", None, "0.r0.c1"),
        ("object_inventory", None, "0.r1.c0"),
        ("table_cell", "d", "0.r1.c1"),
    ]
    assert structural(docx(body)) == [
        ("0.r0.c1", cell_record(0, 1, 1, columns=2)),
        ("0.r1.c0", cell_record(1, 0, 0, columns=2)),
    ]


def test_a_table_whose_every_cell_is_empty_reports_every_coordinate() -> None:
    """Nothing in the table bears text, so the grid is the only thing it says --
    and the whole of it survives, cell by cell, rather than the table
    disappearing from the extraction. The table itself takes no record: it has
    coordinates to report and reports them."""
    body = tbl(tr(tc(), tc()), tr(tc(), tc()), columns=2)

    assert cells(docx(body)) == []
    assert structural(docx(body)) == [
        ("0.r0.c0", cell_record(0, 0, 0, columns=2)),
        ("0.r0.c1", cell_record(0, 1, 1, columns=2)),
        ("0.r1.c0", cell_record(1, 0, 0, columns=2)),
        ("0.r1.c1", cell_record(1, 1, 1, columns=2)),
    ]


def test_a_table_holding_no_cell_is_recorded_once_as_the_table_it_is() -> None:
    """A declared grid with no row under it, and a row with no cell in it, both
    leave no coordinate to report, so the table is recorded instead of lost. The
    declared grid, the rows it does have and the cells it does not are all the
    record claims."""
    declared = '<w:tblGrid><w:gridCol w:w="1440"/><w:gridCol/></w:tblGrid>'

    assert structural(docx(tbl(grid=declared))) == [
        (
            "0.inventory",
            {
                "kind": "docxTable",
                "tablePath": "0",
                "tableColumns": 2,
                "tableGridWidths": (1440, None),
                "rowCount": 0,
                "cellCount": 0,
            },
        )
    ]
    assert structural(docx(tbl("<w:tr/>", "<w:tr/>"))) == [
        (
            "0.inventory",
            {
                "kind": "docxTable",
                "tablePath": "0",
                "tableColumns": None,
                "tableGridWidths": None,
                "rowCount": 2,
                "cellCount": 0,
            },
        )
    ]


def test_a_covered_continuation_is_recorded_without_repeating_the_anchor() -> None:
    """The covered rows of a vertical merge are where text must not be read
    twice, so each is recorded as the covered coordinate it is -- carrying the
    merge's extent and the path of the anchor whose prose stands for it -- and
    the anchor's text is emitted exactly once."""
    body = tbl(
        tr(tc("tall", RESTART), tc("top")),
        tr(tc(None, CONTINUE), tc("middle")),
        tr(tc(None, CONTINUE), tc("bottom")),
        columns=2,
    )
    spanning = {"columns": 2, "spans": (3, 1), "state": "covered", "anchor": (0, 0)}

    assert cells(docx(body)) == [
        ("tall", "0.r0.c0", at(0, 0, 0, columns=2, spans=(3, 1), state="anchor")),
        ("top", "0.r0.c1", at(0, 1, 1, columns=2)),
        ("middle", "0.r1.c1", at(1, 1, 1, columns=2)),
        ("bottom", "0.r2.c1", at(2, 1, 1, columns=2)),
    ]
    assert structural(docx(body)) == [
        ("0.r1.c0", cell_record(1, 0, 0, **spanning)),
        ("0.r2.c0", cell_record(2, 0, 0, **spanning)),
    ]


def test_an_empty_nested_cell_names_the_table_it_hangs_under() -> None:
    """An empty coordinate is identified the same way a text one is: by the path
    of its own table, which for a nested table is the cell path it hangs under
    and never its shape."""
    inner = tbl(tr(tc(), tc("deep")), columns=2)
    body = tbl(tr(f"<w:tc>{p('outer')}{inner}</w:tc>", tc()), columns=2)

    assert structural(docx(body)) == [
        ("0.r0.c0.1.r0.c0", cell_record(0, 0, 0, columns=2, path="0.r0.c0.1")),
        ("0.r0.c1", cell_record(0, 1, 1, columns=2)),
    ]


def test_a_strict_table_records_the_same_empty_coordinates() -> None:
    """Geometry is read by qualified name in both profiles, so an empty cell in a
    Strict table reports exactly what a Transitional one does."""
    transitional = tbl(tr(tc("a"), tc(None, span(2)), tc()), columns=4)
    strict = transitional.replace("<w:tbl>", f'<w:tbl xmlns:w="{STRICT_W}">', 1)

    assert structural(docx(strict)) == structural(docx(transitional))
    assert structural(docx(strict)) == [
        ("0.r0.c1", cell_record(0, 1, 1, columns=4, spans=(1, 2), state="anchor")),
        ("0.r0.c2", cell_record(0, 2, 3, columns=4)),
    ]


def test_a_deleted_table_records_its_empty_cells_as_the_deletion_they_are() -> None:
    """A record stands or falls with the revision that wrote it, exactly as every
    other inventory does: the empty coordinates of a deleted table are kept, and
    kept as deleted, so a deletion is never read as current structure. A cell
    whose only prose is deleted emits no ``table_cell`` run either, so it is
    recorded alongside them -- but it is not empty: the prose was authored, and
    the record says so while the deletion stays the deletion it is."""
    body = f"<w:del>{tbl(tr(tc('gone'), tc()), tr(tc(), tc()), columns=2)}</w:del>"

    result = extract_docx(docx(body), capture_id=CAPTURE)

    assert [
        (
            item.semantic_role,
            item.source_locator["position"],
            item.visibility,
            item.revision_state,
        )
        for item in result.segments
    ] == [
        ("deleted_text", "0.r0.c0.0.d0", "annotation", "deleted"),
        ("object_inventory", "0.r0.c0", "annotation", "deleted"),
        ("object_inventory", "0.r0.c1", "annotation", "deleted"),
        ("object_inventory", "0.r1.c0", "annotation", "deleted"),
        ("object_inventory", "0.r1.c1", "annotation", "deleted"),
    ]
    assert structural(docx(body)) == [
        ("0.r0.c0", cell_record(0, 0, 0, False, columns=2)),
        ("0.r0.c1", cell_record(0, 1, 1, columns=2)),
        ("0.r1.c0", cell_record(1, 0, 0, columns=2)),
        ("0.r1.c1", cell_record(1, 1, 1, columns=2)),
    ]


def test_an_empty_record_carries_its_geometry_in_its_locator_too() -> None:
    """The point of the record is that the coordinate stays addressable, so the
    geometry a text cell puts in its locator is in the empty cell's locator as
    well -- the same keys, the same values -- and every locator in the document
    stays unique."""
    body = tbl(tr(tc("a"), tc(None, RESTART)), tr(tc(), tc(None, CONTINUE)), columns=2)

    result = extract_docx(docx(body), capture_id=CAPTURE)
    records = [
        item for item in result.segments if item.semantic_role == "object_inventory"
    ]

    assert [sorted(set(item.source_locator) & set(GRID_KEYS)) for item in records] == [
        sorted(GRID_KEYS)
    ] * 3
    assert [
        {key: item.source_locator[key] for key in GRID_KEYS} for item in records
    ] == [
        at(0, 1, 1, columns=2, spans=(2, 1), state="anchor"),
        at(1, 0, 0, columns=2),
        at(1, 1, 1, columns=2, spans=(2, 1), state="covered", anchor=(0, 1)),
    ]
    assert [
        item.structured_value == {"kind": "docxTableCell", "empty": True, **geometry}
        for item, geometry in zip(
            records,
            [{key: item.source_locator[key] for key in GRID_KEYS} for item in records],
        )
    ] == [True] * 3
    locators = [tuple(sorted(item.source_locator.items())) for item in result.segments]
    assert len(set(locators)) == len(locators)


def test_repeating_the_extraction_reports_the_same_records() -> None:
    """Nothing in a record comes from iteration order or from what was seen
    before it, so the same package reports the same coordinates every time."""
    body = tbl(tr(tc(), tc("a"), tc(None, RESTART)), tr(tc(), tc(), tc(None, CONTINUE)))

    assert structural(docx(body)) == structural(docx(body))


def test_empty_cell_records_count_against_the_segment_cap() -> None:
    """A table's own plan is bounded by the segment cap, but a document may hold
    more than one table. The records are ordinary segments, so the cap stops the
    extraction where the per-table bound cannot."""
    half = tbl(*(tr(*("<w:tc/>",) * 250),) * (MAX_SEGMENTS // 500))

    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_docx(docx(half + half + tbl(tr("<w:tc/>"))), capture_id=CAPTURE)


def test_a_cell_whose_prose_is_only_revised_or_hidden_is_not_empty() -> None:
    """Emitting no ``table_cell`` run is not the same as holding nothing. Prose
    that was deleted, and prose the author hid, are both read where they stand
    and both leave the run count at zero -- so the coordinate is recorded, as it
    was before, but the record no longer claims the cell was given nothing."""
    removed = f"<w:tc><w:p><w:del>{run('gone', 'delText')}</w:del></w:p></w:tc>"
    secret = f"<w:tc><w:p>{hidden('secret')}</w:p></w:tc>"
    body = tbl(tr(tc(), removed, secret), columns=3)

    result = extract_docx(docx(body), capture_id=CAPTURE)

    assert cells(docx(body)) == []
    assert [
        (item.semantic_role, item.text, item.revision_state)
        for item in result.segments
    ] == [
        ("object_inventory", None, "current"),
        ("deleted_text", "gone", "deleted"),
        ("object_inventory", None, "current"),
        ("hidden_content", "secret", "current"),
        ("object_inventory", None, "current"),
    ]
    assert structural(docx(body)) == [
        ("0.r0.c0", cell_record(0, 0, 0, columns=3)),
        ("0.r0.c1", cell_record(0, 1, 1, False, columns=3)),
        ("0.r0.c2", cell_record(0, 2, 2, False, columns=3)),
    ]


def test_a_cell_holding_only_content_that_was_never_prose_is_not_empty() -> None:
    """A cell is not prose or nothing. An equation, a field, a text box and a
    nested table are each read as the thing they are rather than as cell text,
    so a cell holding only one of them emits no run -- and is recorded as the
    authored cell it is, geometry and all."""
    equation = f"<w:tc>{MATH}</w:tc>"
    field = '<w:tc><w:p><w:fldSimple w:instr=" TIME "/></w:p></w:tc>'
    boxed = f"<w:tc><w:p>{box(p('inside'))}</w:p></w:tc>"
    nested = f"<w:tc>{tbl(tr(tc('deep')), columns=1)}</w:tc>"
    body = tbl(tr(equation, field, boxed, nested), columns=4)

    assert cells(docx(body)) == [
        ("deep", "0.r0.c3.0.r0.c0", at(0, 0, 0, columns=1, path="0.r0.c3.0"))
    ]
    assert structural(docx(body)) == [
        ("0.r0.c0", cell_record(0, 0, 0, False, columns=4)),
        ("0.r0.c1", cell_record(0, 1, 1, False, columns=4)),
        ("0.r0.c2", cell_record(0, 2, 2, False, columns=4)),
        ("0.r0.c3", cell_record(0, 3, 3, False, columns=4)),
    ]


def test_a_wrapper_around_a_cell_s_content_cannot_make_it_look_empty() -> None:
    """A content control and a revision are walked through, not around, so the
    prose under them is read -- and the cell that holds nothing but such a
    wrapper is never mistaken for an empty one, whatever the wrapper encloses."""
    control = f"<w:tc><w:sdt><w:sdtContent>{p('governed')}</w:sdtContent></w:sdt></w:tc>"
    inserted = f"<w:tc><w:ins><w:p>{hidden('new')}</w:p></w:ins></w:tc>"
    body = tbl(tr(control, inserted), columns=2)

    assert cells(docx(body)) == [("governed", "0.r0.c0", at(0, 0, 0, columns=2))]
    assert structural(docx(body)) == [
        ("0.r0.c1", cell_record(0, 1, 1, False, columns=2))
    ]


W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
W15 = 'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"'
# Mixed case on purpose: the digits are hex and the identity is kept as the file
# wrote it, never recased into a canonical spelling.
PARA_ID = "12AB34cd"


def para_attr(value: str) -> str:
    """The one qualified attribute a paragraph declares its identity with."""
    return f'w14:paraId="{value}"'


PARA_ID_ATTR = para_attr(PARA_ID)


def identified(*children: str, attrs: str = PARA_ID_ATTR) -> str:
    """One paragraph declaring ``attrs``, holding ``children`` or a single run.

    Both namespaces Word carries a paragraph identity under are declared on the
    paragraph itself, so a test naming either prefix needs no other fixture.
    """
    return f"<w:p {W14} {W15} {attrs}>{''.join(children) or run('prose')}</w:p>"


def identities(content: bytes) -> list[tuple[str, object, object]]:
    """Each segment's role, position and the identity its locator carries."""
    return [
        (
            item.semantic_role,
            item.source_locator["position"],
            item.source_locator.get("paragraphId"),
        )
        for item in extract_docx(content, capture_id=CAPTURE).segments
    ]


def test_a_paragraph_identity_rides_with_every_stretch_read_out_of_it() -> None:
    """The identity is the paragraph's, so everything the paragraph sourced
    carries it: its current text, the stretches a deletion and a hidden run
    split out of it, and the equation standing between them. A paragraph that
    declared none carries no key at all, which is how "this file said nothing"
    stays distinguishable from any identity it could have written."""
    body = (
        identified(
            run("one "),
            f"<w:del>{run('two', 'delText')}</w:del>",
            hidden("three"),
            MATH,
            run("four"),
        )
        + p("plain")
    )
    content = docx(body, styles=False)

    assert identities(content) == [
        ("body", "0", PARA_ID),
        ("deleted_text", "0.d0", PARA_ID),
        ("hidden_content", "0.v0", PARA_ID),
        ("equation", "0.e0", PARA_ID),
        ("body", "0.t1", PARA_ID),
        ("body", "1", None),
    ]
    plain = extract_docx(content, capture_id=CAPTURE).segments[-1]
    assert "paragraphId" not in plain.source_locator


def test_an_inserted_stretch_keeps_its_paragraph_but_a_cell_run_omits_it() -> None:
    """An insertion splits the paragraph like any other revision, and every
    stretch it splits out was still read from that paragraph, so each names it.
    A cell run joins paragraphs, so it names none however it was revised."""
    inside = identified(
        run("one "),
        f"<w:ins>{run('two')}</w:ins>",
        f"<w:ins>{hidden('three')}</w:ins>",
        run("four"),
    )
    content = docx(inside, styles=False)

    assert identities(content) == [
        ("body", "0", PARA_ID),
        ("body", "0.t1", PARA_ID),
        ("hidden_content", "0.v0", PARA_ID),
        ("body", "0.t2", PARA_ID),
    ]
    cell = docx(f"<w:tbl><w:tr><w:tc><w:ins>{inside}</w:ins></w:tc></w:tr></w:tbl>")
    assert all(
        "paragraphId" not in item.source_locator
        for item in extract_docx(cell, capture_id=CAPTURE).segments
        if item.semantic_role == "table_cell"
    )


def test_a_paragraph_identity_rides_with_the_links_and_references_it_holds() -> None:
    """A hyperlink, a field and an annotation reference are inventoried out of
    the paragraph they stand in, so each names that paragraph too."""
    body = identified(
        run("a"),
        link("rId1", "linked"),
        '<w:fldSimple w:instr=" PAGE ">' + run("7") + "</w:fldSimple>",
        f"<w:r>{ref('footnoteReference', '2')}</w:r>",
    )
    content = docx(
        body,
        {
            **NOTES,
            "word/_rels/document.xml.rels": rels(
                ("rId1", "https://a.invalid/", True, "hyperlink")
            ),
        },
        styles=False,
    )

    # The hyperlink's mark follows the stretch it stands in, the field's is
    # ordered before the result stored under it, and the reference's follows the
    # result's stretch -- the identity rides with each wherever it lands.
    assert identities(content)[:5] == [
        ("body", "0", PARA_ID),
        ("object_inventory", "0.h0", PARA_ID),
        ("object_inventory", "0.f0", PARA_ID),
        ("body", "0.t1", PARA_ID),
        ("object_inventory", "0.n0", PARA_ID),
    ]


@pytest.mark.parametrize(
    ("content", "roles"),
    [
        pytest.param(
            lambda: imaged(identified(drawing(doc_pr=DOC_PR + ' descr="Chart of it"'))),
            ["object_inventory", "image_alt"],
            id="image",
        ),
        pytest.param(
            lambda: charted(RICH_TITLE + PLOTTED, identified(CHART_DRAWING)),
            ["object_inventory"] + ["chart"] * 4,
            id="chart",
        ),
        pytest.param(
            lambda: smart(identified(SMART_DRAWING)),
            ["object_inventory", "body", "body", "object_inventory"],
            id="smartart",
        ),
        pytest.param(
            lambda: objected(identified(ole())),
            ["object_inventory"],
            id="object",
        ),
        pytest.param(
            lambda: imaged(identified(pict(shape=' alt="Legacy"'))),
            ["object_inventory", "image_alt"],
            id="legacy-image",
        ),
    ],
)
def test_an_anchored_object_and_everything_derived_from_it_names_its_paragraph(
    content: object, roles: list[str]
) -> None:
    """An inline object is read out of the paragraph that anchors it, and so is
    every claim, point and value derived from the part behind it, so the record
    and all of its derivations name that one paragraph."""
    assert callable(content)
    found = identities(content())

    assert [role for role, _position, _identity in found] == roles
    assert {identity for _role, _position, identity in found} == {PARA_ID}


def test_a_text_box_scopes_its_own_identities_and_restores_the_one_outside() -> None:
    """A text box is walked as its own story, so the paragraphs inside it carry
    their own identities and the paragraph anchoring the box carries its own
    again on the other side -- at any nesting depth, and whether or not the
    paragraphs in between declared one."""
    inner = identified(run("deep"), attrs=para_attr("0000000F"))
    middle = identified(run("around "), box(inner), attrs=para_attr("7EADBEE1"))
    body = identified(run("before "), box(p("plain"), middle), run("after"))

    assert identities(docx(body, styles=False)) == [
        ("body", "0", PARA_ID),
        ("body", "0.x0.0", None),
        ("body", "0.x0.1", "7EADBEE1"),
        ("body", "0.x0.1.x0.0", "0000000F"),
        ("body", "0.t1", PARA_ID),
    ]


def test_every_story_reports_the_identities_its_own_paragraphs_declared() -> None:
    """A header, a footnote, an endnote and a comment are each walked as their
    own story, so an identity declared in one is reported there and nowhere
    else."""
    head, foot, end, said = (
        identified(run(text), attrs=para_attr(value))
        for text, value in (
            ("Header", "0000AAA1"),
            ("Foot", "0000AAA2"),
            ("End", "0000AAA3"),
            ("Said", "0000AAA4"),
        )
    )
    notes = {
        "word/footnotes.xml": (
            f'<w:footnotes {W}><w:footnote w:id="2">{foot}</w:footnote></w:footnotes>'
        ),
        "word/endnotes.xml": (
            f'<w:endnotes {W}><w:endnote w:id="3">{end}</w:endnote></w:endnotes>'
        ),
        "word/comments.xml": (
            f'<w:comments {W}><w:comment w:id="7">{said}</w:comment></w:comments>'
        ),
    }
    body = (
        identified(
            run("Body"),
            f"<w:r>{ref('footnoteReference', '2')}</w:r>",
            f"<w:r>{ref('endnoteReference', '3')}</w:r>",
            f"<w:r>{ref('commentReference', '7')}</w:r>",
        )
        + HEADER_SECTION
    )
    content = docx(
        body,
        {
            **notes,
            "word/header1.xml": f"<w:hdr {W}>{head}</w:hdr>",
            "word/_rels/document.xml.rels": rels(HEADER_RELATION),
        },
        styles=False,
        content_types=HEADER_OVERRIDE,
    )

    assert [
        (role, identity)
        for role, _position, identity in identities(content)
        if role != "object_inventory"
    ] == [
        ("body", PARA_ID),
        ("comment", "0000AAA4"),
        ("note", "0000AAA3"),
        ("note", "0000AAA2"),
        ("body", "0000AAA1"),
    ]


def test_a_cell_run_joining_several_paragraphs_claims_no_one_identity() -> None:
    """A ``table_cell`` run is the joined prose of every paragraph in its cell,
    so it belongs to no one of them and names none, even where every paragraph
    in the cell declared one. What the cell's paragraphs did source on their own
    -- a deletion, a hidden stretch, a link -- still names the paragraph it came
    from, and so does nothing structural about the table or the cell."""
    first = identified(run("a"), attrs=para_attr("0000BBB1"))
    second = identified(
        run("b"),
        f"<w:del>{run('gone', 'delText')}</w:del>",
        link("rId1", "linked"),
        attrs=para_attr("0000BBB2"),
    )
    body = tbl(tr(f"<w:tc>{first}{second}</w:tc>", tc()), columns=2)
    content = docx(
        body,
        {
            "word/_rels/document.xml.rels": rels(
                ("rId1", "https://a.invalid/", True, "hyperlink")
            )
        },
        styles=False,
    )

    assert identities(content) == [
        # The joined run of both paragraphs' current prose names neither.
        ("table_cell", "0.r0.c0", None),
        ("deleted_text", "0.r0.c0.1.d0", "0000BBB2"),
        ("table_cell", "0.r0.c0.t1", None),
        ("object_inventory", "0.r0.c0.1.h0", "0000BBB2"),
        # The empty cell's own geometry record is the table's, not a paragraph's.
        ("object_inventory", "0.r0.c1", None),
    ]


def test_a_block_equation_and_a_table_s_own_records_name_no_paragraph() -> None:
    """Display math standing between blocks is its own block and no paragraph's
    content, and neither is the record a table holding no cell takes, so neither
    carries an identity -- not even the one the paragraph before them wrote."""
    body = (
        identified(run("before"))
        + f'<m:oMathPara xmlns:m="{MATH_NS}">{MATH}</m:oMathPara>'
        + tbl(columns=2)
    )

    assert identities(docx(body, styles=False)) == [
        ("body", "0", PARA_ID),
        ("equation", "1", None),
        ("object_inventory", "2.inventory", None),
    ]


@pytest.mark.parametrize(
    "value",
    ["", "0000000", "000000000", "0000000g", "00000000", "80000000", "FFFFFFFF", " 1"],
)
def test_a_malformed_paragraph_identity_is_refused_rather_than_reported(
    value: str,
) -> None:
    """A written identity is held to ``ST_LongHexNumber`` and to the range
    MS-DOCX reserves, exactly as a comment's is: eight hex digits naming a
    number above zero and below ``0x80000000``. One outside that is not an
    identity to report under, so the package is refused."""
    body = identified(run("x"), attrs=para_attr(value))

    with pytest.raises(OOXMLInvalid, match="DOCX paragraph identity is malformed"):
        extract_docx(docx(body, styles=False), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param(f'paraId="{PARA_ID}"', id="unqualified"),
        pytest.param(f'w:paraId="{PARA_ID}"', id="wordprocessing-namespace"),
        pytest.param(f'w15:paraId="{PARA_ID}"', id="other-microsoft-namespace"),
        pytest.param(f'w14:paraid="{PARA_ID}"', id="other-case"),
        pytest.param(f'w14:textId="{PARA_ID}"', id="other-local-name"),
        pytest.param(f'w14:paraIdParent="{PARA_ID}"', id="longer-local-name"),
    ],
)
def test_only_the_one_qualified_attribute_declares_a_paragraph_identity(
    attrs: str,
) -> None:
    """The identity is the exact Word 2010 ``w14:paraId`` and nothing else: an
    attribute written in no namespace, in another namespace, or under a similar
    local name declares no identity, so it is neither reported as one nor held
    to the range one would be held to."""
    body = identified(
        run("x"), attrs=f"{attrs} {para_attr('0000CCC1')}"
    ) + identified(run("y"), attrs=attrs)

    assert identities(docx(body, styles=False)) == [
        ("body", "0", "0000CCC1"),
        ("body", "1", None),
    ]


def test_one_identity_written_in_two_stories_is_reported_in_each_of_them() -> None:
    """Nothing about reporting an identity makes it exclusive: a body paragraph
    and a comment paragraph may write the same one, and each is reported under
    its own story's own position without borrowing the other's. What the
    extended comments part joins on is the dedicated threaded-comment suite's,
    and reporting identities in the locator is no part of that join."""
    body = identified(run("Body"), f"<w:r>{ref('commentReference', '7')}</w:r>")
    said = identified(run("Said")) + identified(
        run("More"), attrs=para_attr("0000DDD1")
    )
    comments = f'<w:comments {W}><w:comment w:id="7">{said}</w:comment></w:comments>'
    content = docx(body, {"word/comments.xml": comments}, styles=False)

    assert identities(content) == [
        ("body", "0", PARA_ID),
        ("object_inventory", "0.n0", PARA_ID),
        ("comment", "0", PARA_ID),
        ("comment", "1", "0000DDD1"),
    ]


# --- embedded raster recognition -------------------------------------------------
#
# The structural reader above never opens an image. These exercise the one pass
# that does: the candidate seam out of the validated package, the aggregate
# budget over every image of one document, and the per-image truth the record
# reports when nothing is read. The engine is injected throughout, so what is
# asserted is this adapter's own behaviour rather than whatever a host's
# Tesseract reads out of a test fixture.


def real_png(label: str = "INVOICE", size: tuple[int, int] = (120, 40)) -> bytes:
    """A small, genuinely decodable PNG, written by Pillow rather than by hand."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((4, 10), label, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def real_jpeg(size: tuple[int, int] = (120, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="JPEG")
    return buffer.getvalue()


PNG_MEMBER = {"word/media/image1.png": real_png()}


class RecordingOcr:
    """An injected engine returning one fixed line, in Tesseract's TSV shape."""

    def __init__(self, *words: str, confidence: float = 91.0) -> None:
        self.words = words or ("INVOICE", "7")
        self.confidence = confidence
        self.sizes: list[tuple[int, int]] = []

    def __call__(
        self,
        image: Image.Image,
        *,
        resources: ocr_resources.OcrResources,
        psm: int,
        timeout_seconds: float,
    ) -> dict[str, list[object]]:
        self.sizes.append(image.size)
        data: dict[str, list[object]] = {
            key: [] for key in ("text", "conf", "left", "top", "width", "height")
        }
        data.update({"block_num": [], "par_num": [], "line_num": []})
        for index, word in enumerate(self.words):
            data["text"].append(word)
            data["conf"].append(self.confidence)
            data["left"].append(4 + index * 30)
            data["top"].append(10)
            data["width"].append(28)
            data["height"].append(12)
            data["block_num"].append(1)
            data["par_num"].append(1)
            data["line_num"].append(1)
        return data


class RefusingOcr:
    """An engine that fails the test if anything invokes it."""

    def __call__(self, image: Image.Image, **_kwargs: object) -> dict[str, list[object]]:
        raise AssertionError("the recognition engine must not have been invoked")


@pytest.fixture
def examining(tmp_path: Path) -> ocr_resources.ExtractionSettings:
    """Settings whose profile examines embedded images and whose models verify."""
    stand_in_models(tmp_path, "eng")
    return ocr_resources.ExtractionSettings(
        processing_mode=ocr_resources.MODE_TEXT_AND_LOCAL_OCR,
        ocr=catalogue_over(tmp_path, "eng").resolve(("eng",)),
        embedded_image_ocr=True,
    )


def examined(
    content: bytes,
    settings: ocr_resources.ExtractionSettings | None,
    engine: object,
    budget: ExtractionBudget | None = None,
) -> OOXMLExtraction:
    return extract_docx(
        content,
        capture_id=CAPTURE,
        settings=settings,
        ocr_engine=engine,  # type: ignore[arg-type]
        budget=budget,
    )


def test_an_embedded_raster_is_recognised_and_anchored_to_the_object_showing_it(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The reading is a segment of its own, after the record and the author's
    claims, anchored to the drawing's own position and to the image part the
    bytes came from. It carries the OCR line geometry, the frame space it was
    read in and the exact engine and languages that read it, and it is never
    joined to the prose around the drawing or to the alt text beside it."""
    body = (
        f"<w:p>{run('Before ')}"
        + drawing(doc_pr=f'{DOC_PR} descr="The invoice"')
        + f"{run(' after')}</w:p>"
    )
    content = linked(
        body, IMAGE_RELATION, content_types=PNG_DEFAULT, members=PNG_MEMBER
    )
    engine = RecordingOcr()

    result = examined(content, examining, engine)

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("body", "Before ", "0"),
        ("object_inventory", None, "0.i0"),
        ("image_alt", "The invoice", "0.i0"),
        ("body", "INVOICE 7", "0.i0"),
        ("body", " after", "0.t1"),
    ]
    reading = result.segments[3]
    assert reading.extraction_method == "ocr"
    assert reading.language == "eng"
    assert reading.confidence == pytest.approx(0.91)
    locator = reading.source_locator
    # Anchored to the DOCX object and to the part, and located inside the frame.
    assert locator["part"] == "word/document.xml"
    assert locator["story"] == "body"
    assert locator["imagePart"] == "word/media/image1.png"
    assert (locator["unit"], locator["frame"], locator["line"]) == ("image_frame", 1, 1)
    assert locator["space"] == GEOMETRY_SPACE
    assert (locator["sourceWidth"], locator["sourceHeight"]) == (120, 40)
    assert locator["ocrEngine"] == "tesseract-5.5.3"
    assert locator["ocrLanguages"] == ["eng"]
    assert locator["box"] == [4.0, 10.0, 58.0, 12.0]
    # The bytes reached the engine and nothing persisted holds any of them.
    assert engine.sizes == [(120, 40)]
    for item in result.segments:
        assert not isinstance(item.structured_value, bytes)
        assert not any(
            isinstance(value, bytes) for value in item.source_locator.values()
        )

    value = result.segments[1].structured_value
    assert (value["imageDecoded"], value["ocrPerformed"]) == (True, True)
    assert value["ocrOutcome"] == EMBEDDED_RECOGNISED
    assert result.limitations["embeddedImages"] == 1
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 1}
    assert (result.failed_units, result.limit_breach) == (0, None)


def test_a_legacy_embedded_raster_is_recognised_on_the_same_seam(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A ``w:pict`` reaches the one pass a drawing does, with its reading
    anchored to the legacy shape's own position."""
    content = linked(
        f"<w:p>{pict()}</w:p>",
        IMAGE_RELATION,
        content_types=PNG_DEFAULT,
        members=PNG_MEMBER,
    )

    result = examined(content, examining, RecordingOcr("LEGACY"))

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", None, "0.v0"),
        ("body", "LEGACY", "0.v0"),
    ]
    assert result.segments[1].source_locator["imagePart"] == "word/media/image1.png"
    value = result.segments[0].structured_value
    assert (value["imageDecoded"], value["ocrPerformed"]) == (True, True)
    assert value["ocrOutcome"] == EMBEDDED_RECOGNISED


def test_a_profile_that_examines_no_embedded_image_opens_nothing(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The preference alone decides it, and a profile with it off never reaches
    the engine, never decodes a byte and reports the image as unexamined. The
    member here is a real PNG, so only the profile is keeping it closed."""
    content = linked(
        f"<w:p>{drawing()}</w:p>",
        IMAGE_RELATION,
        content_types=PNG_DEFAULT,
        members=PNG_MEMBER,
    )
    off = replace(examining, embedded_image_ocr=False)

    for settings in (off, None):
        result = examined(content, settings, RefusingOcr())

        assert [i.semantic_role for i in result.segments] == ["object_inventory"]
        value = result.segments[0].structured_value
        assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
        assert value["ocrOutcome"] == EMBEDDED_DISABLED
        assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_DISABLED: 1}


def test_a_profile_whose_models_were_not_served_examines_nothing_and_says_so(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """An unavailable selection keeps its identity and its requested order, and
    the image stays unexamined for the reason it really was: the pinned models
    were never served, which is not the same as the preference being off."""
    content = linked(
        f"<w:p>{drawing()}</w:p>",
        IMAGE_RELATION,
        content_types=PNG_DEFAULT,
        members=PNG_MEMBER,
    )
    unserved = replace(
        examining, ocr=ocr_resources.TrustedOcrCatalogue().resolve(("deu",))
    )

    result = examined(content, unserved, RefusingOcr())

    value = result.segments[0].structured_value
    assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
    assert value["ocrOutcome"] == EMBEDDED_RESOURCES_UNAVAILABLE


def test_an_external_image_reference_is_never_opened(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The relationship names a path the package does hold as a member, so only
    its declared mode keeps it shut: an external reference is inventory, and the
    pass neither resolves it, reads the member behind it nor reaches the engine.
    A blip's ``r:link`` and a ``v:imagedata`` on an external relationship are the
    two ways a Word story names one, and neither yields a candidate."""
    external = ("rId9", "media/image1.png", True, "image")
    link_only = (
        "<a:graphic><a:graphicData>"
        + picture(blip(embed=None, target="rId9"))
        + "</a:graphicData></a:graphic>"
    )
    bodies = (
        f"<w:p>{drawing(graphic=link_only)}</w:p>",
        f"<w:p>{pict()}</w:p>",
    )
    for body in bodies:
        content = linked(
            body, external, content_types=PNG_DEFAULT, members=PNG_MEMBER
        )

        result = examined(content, examining, RefusingOcr())

        assert [i.semantic_role for i in result.segments] == ["object_inventory"]
        value = result.segments[0].structured_value
        assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
        assert value["ocrOutcome"] == EMBEDDED_NOT_EMBEDDED
        assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_NOT_EMBEDDED: 1}


def test_an_ole_object_and_its_preview_are_no_candidates(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The picture a ``w:object`` shows is a picture *of* the object, reached by
    a route of its own. It is not an embedded raster this pass examines, so the
    engine is never reached even though the stand-in names a real image part
    the package holds."""
    body = (
        f'<w:r><w:object><v:shape id="{SHAPE_ID}" style="{VML_STYLE}">'
        '<v:imagedata r:id="rId8"/></v:shape>'
        f'<o:OLEObject {OFFICE} Type="Embed" r:id="rId9"/>'
        "</w:object></w:r>"
    )
    content = linked(
        f"<w:p>{body}</w:p>",
        OBJECT_RELATION,
        ("rId8", "media/image1.png", False, "image"),
        content_types=OBJECT_DEFAULTS + PNG_DEFAULT,
        members=OBJECT_MEMBERS | PNG_MEMBER,
    )

    result = examined(content, examining, RefusingOcr())

    assert [i.structured_value["kind"] for i in result.segments] == ["oleObject"]
    assert result.limitations["embeddedImages"] == 0


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(b"\x89PNG\r\n\x1a\nnot really an image", id="malformed"),
        pytest.param(real_jpeg(), id="mismatched-magic"),
    ],
)
def test_a_malformed_or_mismatched_raster_keeps_its_record_and_is_not_examined(
    examining: ocr_resources.ExtractionSettings, member: bytes
) -> None:
    """Bytes the decoder refuses -- truncated, or a JPEG behind a ``.png``
    declaration -- are this image's outcome and never the package's: the
    structural record stands, states that nothing was decoded, and names the
    refusal. It is never reported as examined."""
    content = linked(
        f"<w:p>{drawing()}</w:p>",
        IMAGE_RELATION,
        content_types=PNG_DEFAULT,
        members={"word/media/image1.png": member},
    )

    result = examined(content, examining, RecordingOcr())

    assert [i.semantic_role for i in result.segments] == ["object_inventory"]
    value = result.segments[0].structured_value
    assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
    assert value["ocrOutcome"] == EMBEDDED_MALFORMED
    assert value["imageBytes"] == len(member)
    assert result.failed_units == 1
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_MALFORMED: 1}


def test_an_image_type_outside_the_accepted_set_is_named_before_any_decode(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A GIF is a legitimate image part and is not in the accepted raster set,
    so the declared type settles it before a byte is opened and the engine is
    never reached. EMF, WMF and SVG are refused the same way."""
    content = linked(
        f"<w:p>{drawing()}</w:p>",
        ("rId9", "media/image1.gif", False, "image"),
        content_types='<Default Extension="gif" ContentType="image/gif"/>',
        members={"word/media/image1.gif": b"GIF89a" + b"\x00" * 16},
    )

    result = examined(content, examining, RefusingOcr())

    value = result.segments[0].structured_value
    assert value["imageContentType"] == "image/gif"
    assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
    assert value["ocrOutcome"] == EMBEDDED_UNSUPPORTED


def test_two_references_to_one_image_part_stay_two_located_readings(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A duplicate reference is a second object showing the same picture, so it
    keeps its own locator and pays the shared budget again. Nothing is cached:
    the bytes are read for each reference and discarded with it."""
    body = f"<w:p>{drawing()}</w:p><w:p>{pict()}</w:p>"
    content = linked(
        body, IMAGE_RELATION, content_types=PNG_DEFAULT, members=PNG_MEMBER
    )
    engine = RecordingOcr("TWICE")

    result = examined(content, examining, engine)

    assert [
        (i.semantic_role, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", "0.i0"),
        ("body", "0.i0"),
        ("object_inventory", "1.v0"),
        ("body", "1.v0"),
    ]
    assert len(engine.sizes) == 2
    assert result.limitations["embeddedImageOutcomes"] == {EMBEDDED_RECOGNISED: 2}


def test_one_aggregate_budget_truncates_the_later_images_of_one_document(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """The ceiling is the document's and not one image's: a budget that pays for
    the first raster leaves the second truncated rather than silently skipped,
    and the truncated image reports what it published rather than being called a
    blank. The document's own outcome says coverage was cut short."""
    body = f"<w:p>{drawing()}</w:p><w:p>{drawing()}</w:p>"
    content = linked(
        body, IMAGE_RELATION, content_types=PNG_DEFAULT, members=PNG_MEMBER
    )
    engine = RecordingOcr("FIRST")

    result = examined(
        content, examining, engine, budget=ExtractionBudget(ocr_pixels=120 * 40)
    )

    assert [
        (i.semantic_role, i.text, i.source_locator["position"]) for i in result.segments
    ] == [
        ("object_inventory", None, "0.i0"),
        ("body", "FIRST", "0.i0"),
        ("object_inventory", None, "1.i0"),
    ]
    assert len(engine.sizes) == 1
    outcomes = [
        i.structured_value["ocrOutcome"]
        for i in result.segments
        if i.semantic_role == "object_inventory"
    ]
    assert outcomes == [EMBEDDED_RECOGNISED, EMBEDDED_TRUNCATED]
    assert result.limit_breach == "ocr_pixel_budget_exhausted"
    assert result.limitations["limitBreach"] == "ocr_pixel_budget_exhausted"


def test_the_shared_decoded_pixel_ceiling_stops_opening_later_image_members(
    examining: ocr_resources.ExtractionSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decoded source pixels are the document's aggregate ceiling, so two
    individually acceptable rasters can exhaust it between them. The image the
    ceiling runs out on is truncated, and once the shared budget is breached no
    later candidate's member is read or decoded at all: the bound limits the
    work done and not merely what is published."""
    read_parts: list[str] = []
    member_bytes = ooxml_extractors._Package.bytes

    def recording(self: ooxml_extractors._Package, name: str, **kwargs: int) -> bytes:
        read_parts.append(name)
        return member_bytes(self, name, **kwargs)

    monkeypatch.setattr(ooxml_extractors._Package, "bytes", recording)
    body = f"<w:p>{drawing()}</w:p><w:p>{drawing()}</w:p><w:p>{drawing()}</w:p>"
    content = linked(
        body, IMAGE_RELATION, content_types=PNG_DEFAULT, members=PNG_MEMBER
    )
    engine = RecordingOcr("FIRST")

    result = examined(
        content, examining, engine, budget=ExtractionBudget(decoded_pixels=120 * 40)
    )

    outcomes = [
        (i.structured_value["ocrOutcome"], i.structured_value["imageDecoded"])
        for i in result.segments
        if i.semantic_role == "object_inventory"
    ]
    assert outcomes == [
        (EMBEDDED_RECOGNISED, True),
        # Opened, then refused the pixels it needed before any further work.
        (EMBEDDED_TRUNCATED, True),
        # The breach already stood, so this member was never read.
        (EMBEDDED_TRUNCATED, False),
    ]
    assert read_parts.count("word/media/image1.png") == 2
    assert len(engine.sizes) == 1
    assert result.limit_breach == "decoded_pixel_budget_exhausted"
    assert result.limitations["embeddedImageOutcomes"] == {
        EMBEDDED_RECOGNISED: 1,
        EMBEDDED_TRUNCATED: 2,
    }


def test_a_below_threshold_reading_is_retained_but_is_no_reliable_text(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """Candidates the engine was not confident about are kept so a reviewer can
    trace the selection, in a role ordinary retrieval excludes -- and the image
    reports that it reached no reliable text rather than that it read some."""
    content = linked(
        f"<w:p>{drawing()}</w:p>",
        IMAGE_RELATION,
        content_types=PNG_DEFAULT,
        members=PNG_MEMBER,
    )

    result = examined(content, examining, RecordingOcr("MAYBE", confidence=9.0))

    assert [(i.semantic_role, i.visibility, i.text) for i in result.segments] == [
        ("object_inventory", "visible", None),
        ("hidden_content", "hidden", "MAYBE"),
    ]
    value = result.segments[0].structured_value
    assert (value["imageDecoded"], value["ocrPerformed"]) == (True, True)
    assert value["ocrOutcome"] == EMBEDDED_NO_TEXT


def test_a_deletion_over_a_drawing_is_kept_over_the_reading_it_carried(
    examining: ocr_resources.ExtractionSettings,
) -> None:
    """A revision wrapped around the container is kept over the reading exactly
    as it is over the record and the author's claims: a deleted image's text is
    not the page's content."""
    content = linked(
        f"<w:p><w:del>{drawing()}</w:del></w:p>",
        IMAGE_RELATION,
        content_types=PNG_DEFAULT,
        members=PNG_MEMBER,
    )

    result = examined(content, examining, RecordingOcr("GONE"))

    assert [
        (i.semantic_role, i.visibility, i.revision_state) for i in result.segments
    ] == [
        ("object_inventory", "annotation", "deleted"),
        ("body", "annotation", "deleted"),
    ]
