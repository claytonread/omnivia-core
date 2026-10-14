"""The DOCX semantic walker over minimal in-memory packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    _MAX_DOCX_EQUATION_BYTES,
    _MAX_DOCX_FIELD_DEPTH,
    _MAX_TARGET_CHARS,
    OOXMLInvalid,
    _FieldState,
    extract_docx,
)

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
        ("hidden_content", "nested", "hidden", "current", "0.v0"),
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
    content = imaged(
        body,
        IMAGE_RELATION,
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
        ("object_inventory", "word/header1.xml", "0.i0"),
    ]


def test_a_part_resolves_only_its_own_relationships() -> None:
    """The main document relating to the image says nothing about a header that
    does not: the id names no relationship there and nothing reaches past it."""
    header = f"<w:hdr {W}><w:p>{drawing()}</w:p></w:hdr>"

    with pytest.raises(OOXMLInvalid, match="names a missing image relationship"):
        extract_docx(
            imaged("", IMAGE_RELATION, **{"word/header1.xml": header}),
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
    """The member is declared an image and holds nothing that decodes as one.
    The record still reports the length the package measured when it validated
    the member, and says outright that it read no further."""
    content = imaged(f"<w:p>{drawing()}</w:p>")

    _, value = inventory(content)[0]
    assert (value["imageDecoded"], value["ocrPerformed"]) == (False, False)
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
