"""The DOCX semantic walker over minimal in-memory packages."""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    _MAX_DOCX_FIELD_DEPTH,
    _MAX_TARGET_CHARS,
    OOXMLInvalid,
    _FieldState,
    extract_docx,
)

CAPTURE = "dcap-" + "a" * 64
W = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
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


def linked(body: str, *relations: tuple[str, str, bool, str], **parts: str) -> bytes:
    return docx(
        body,
        {"word/_rels/document.xml.rels": rels(*relations), **parts},
        styles=False,
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
