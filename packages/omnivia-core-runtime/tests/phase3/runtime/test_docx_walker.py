"""The DOCX semantic walker over minimal in-memory packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_docx

CAPTURE = "dcap-" + "a" * 64
W = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
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
    body: str, parts: dict[str, str] | None = None, *, styles: bool = True
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>"
        )
        if styles:
            archive.writestr("word/_rels/document.xml.rels", STYLES_REL)
            archive.writestr("word/styles.xml", STYLES)
        for name, content in (parts or {}).items():
            archive.writestr(name, content)
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
        "word/header1.xml": f"<w:hdr {W}>{p('Head', HEAD_STYLE)}</w:hdr>",
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
        ("body", "Head", "visible", None),
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


def test_revisions_split_current_text_from_deleted_annotations() -> None:
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

    assert revisions(docx(body)) == [
        ("body", "Keep added moved in ", "visible", "current", "0"),
        ("deleted_text", "gonemoved out", "annotation", "deleted", "0.d0"),
        ("body", "tail", "visible", "current", "0.t1"),
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
