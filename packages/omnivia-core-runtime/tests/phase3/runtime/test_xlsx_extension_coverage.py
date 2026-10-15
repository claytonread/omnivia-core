"""XLSX unsupported extensions: coverage entries, never cells.

Design §11.7 says to preserve unsupported extension payloads as coverage entries,
not as normal cells. ``extLst``/``ext`` is where a producer keeps what the
schema has no element for, and this reader understands none of it, so each entry
the worksheet or the workbook declares is reported as an entry: which part owns
it, where it sits in the authored list, and its URI where the URI is a GUID.

The contract is deliberately narrow. Only the two roots are read, directly, in
the root's own SpreadsheetML namespace; every other owner's ``extLst`` (a query
table's, a comment list's) is admitted and left alone where that owner is
parsed, and is not counted here. Nothing inside an entry is walked, so no body
text, attribute, formula, query, target or cached value ever leaves the part.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SHEET_PART = "xl/worksheets/sheet1.xml"

G1 = "{B025F937-C7B1-47D3-B67F-A62EFF666E3E}"
G2 = "{05C60535-1F16-4fd2-B633-F4F36F0B64E0}"
G3 = "{78C0D931-6437-407d-A8EE-F0AAD7539E65}"

# Everything an extension carries that a record may never repeat.
SECRETS = (
    "hunter2",
    "Web.Contents",
    "SELECT * FROM dbo.Margins",
    "https://example.invalid/secret",
    "token=abc123",
    "=SUM(A1:A9)",
    "Quarterly forecast",
    "cached-value-42",
)
LEAKY_BODY = (
    '<x14:conditionalFormattings xmlns:x14="urn:x14" credential="hunter2">'
    "<x14:f>=SUM(A1:A9)</x14:f><x14:note>Quarterly forecast</x14:note>"
    "</x14:conditionalFormattings>"
    '<mashup>let Source = Web.Contents("https://example.invalid/secret") in Source'
    " SELECT * FROM dbo.Margins</mashup><v>cached-value-42</v>"
)


def ext(uri: str | None = G1, body: str = "", extra: str = "") -> str:
    attribute = "" if uri is None else f' uri="{uri}"'
    return f"<ext{attribute}{extra}>{body}</ext>"


def ext_list(*entries: str) -> str:
    return "<extLst>" + "".join(entries) + "</extLst>"


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def xlsx(
    sheet_tail: str = "",
    *,
    workbook_tail: str = "",
    main: str = MAIN,
    state: str = "visible",
    sheet_head: str = "",
    sheets: int = 1,
    extra: dict[str, str] | None = None,
    rels: str = "",
) -> bytes:
    """``sheets`` sheets, each with one written cell; ``sheet_tail`` closes the root."""
    declared = "".join(
        f'<sheet name="S{n}" sheetId="{n}" state="{state if n == 1 else "visible"}"'
        f' r:id="rId{n}"/>'
        for n in range(1, sheets + 1)
    )
    members = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": (
            f'<workbook xmlns="{main}" xmlns:r="{RELNS}">'
            f"<sheets>{declared}</sheets>{workbook_tail}</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": "<Relationships>"
        + "".join(
            f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml"'
            f' Type="{RELNS}/worksheet"/>'
            for n in range(1, sheets + 1)
        )
        + "</Relationships>",
    }
    for n in range(1, sheets + 1):
        members[f"xl/worksheets/sheet{n}.xml"] = (
            f'<worksheet xmlns="{main}" xmlns:r="{RELNS}">{sheet_head}<sheetData>'
            '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
            f"</sheetData>{sheet_tail if n == 1 else ''}</worksheet>"
        )
    if rels:
        members["xl/worksheets/_rels/sheet1.xml.rels"] = rels
    return archive({**members, **(extra or {})})


def coverage(content: bytes) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Every extension coverage record, in the order it was reported."""
    return [
        (dict(item.source_locator), dict(item.structured_value or {}))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]


def uris(content: bytes) -> list[object]:
    return [record["uri"] for _locator, record in coverage(content)]


def test_a_worksheet_extension_is_reported_with_exact_provenance() -> None:
    [(where, record)] = coverage(xlsx(ext_list(ext(G1, LEAKY_BODY))))

    assert where == {
        "captureId": CAPTURE,
        "part": SHEET_PART,
        "extensionIndex": 0,
        "sheetId": "1",
        "sheet": "S1",
        "sheetState": "visible",
    }
    # Pinned by full equality so a field this route starts reporting -- or stops
    # -- fails here rather than passing unnoticed.
    assert record == {
        "kind": "unsupportedExtension",
        "owner": "worksheet",
        "ownerPart": SHEET_PART,
        "extensionIndex": 0,
        "uri": G1,
        "uriForm": "guid",
        "payloadInterpreted": False,
        "refreshPerformed": False,
        "externalAccessPerformed": False,
    }


def test_an_extension_is_a_coverage_entry_and_never_a_cell_or_content() -> None:
    plain = extract_xlsx(xlsx(), capture_id=CAPTURE)
    carrying = extract_xlsx(
        xlsx(ext_list(ext(G1, LEAKY_BODY), ext(G2, "<f>=SUM(A1:A9)</f><v>1</v>"))),
        capture_id=CAPTURE,
    )

    entries = [
        item
        for item in carrying.segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]
    assert len(entries) == 2
    for item in entries:
        assert item.semantic_role == "object_inventory"
        assert item.extraction_method == "inventory"
        assert item.text is None
        assert "cell" not in item.source_locator
    # Nothing but the entries differs: the one cell is still the only content,
    # no formula, saved value, chart or other record appeared, and the unit
    # type and the populated count are the plain workbook's.
    rest = [item for item in carrying.segments if item not in entries]
    assert rest == list(plain.segments)
    assert [item.semantic_role for item in rest] == ["table_cell"]
    assert carrying.unit_type == plain.unit_type == "workbook_cell"
    assert carrying.expected == plain.expected == 1


def test_the_extension_count_is_reported_in_the_extraction_metadata() -> None:
    plain = extract_xlsx(xlsx(), capture_id=CAPTURE).limitations
    content = xlsx(
        ext_list(ext(G1), ext(G2)),
        workbook_tail=ext_list(ext(G3)),
    )
    limitations = extract_xlsx(content, capture_id=CAPTURE).limitations

    assert plain["extensions"] == 0
    assert limitations["extensions"] == 3
    # Every other figure is the plain workbook's: nothing else moved.
    assert {k: v for k, v in limitations.items() if k != "extensions"} == {
        k: v for k, v in plain.items() if k != "extensions"
    }


def test_the_count_spans_every_sheet_and_the_workbook() -> None:
    content = xlsx(
        ext_list(ext(G1)), workbook_tail=ext_list(ext(G2), ext(G3)), sheets=2
    )
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == 3


def test_entries_keep_their_authored_order_not_a_sorted_one() -> None:
    assert uris(xlsx(ext_list(ext(G3), ext(G1), ext(G2)))) == [G3, G1, G2]
    indexes = [
        where["extensionIndex"]
        for where, _ in coverage(xlsx(ext_list(ext(G3), ext(G1), ext(G2))))
    ]
    assert indexes == [0, 1, 2]


def test_a_workbook_extension_is_reported_last_and_always_visible() -> None:
    content = xlsx(ext_list(ext(G1)), workbook_tail=ext_list(ext(G2)), state="hidden")
    result = extract_xlsx(content, capture_id=CAPTURE)
    entries = [
        item
        for item in result.segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]

    assert [item.visibility for item in entries] == ["hidden", "visible"]
    assert result.segments[-1] is entries[-1]
    assert dict(entries[-1].source_locator) == {
        "captureId": CAPTURE,
        "part": "xl/workbook.xml",
        "extensionIndex": 0,
    }
    assert entries[-1].structured_value["owner"] == "workbook"
    assert entries[-1].structured_value["ownerPart"] == "xl/workbook.xml"


@pytest.mark.parametrize("state", ["hidden", "veryHidden"])
def test_a_hidden_sheets_entries_are_hidden(state: str) -> None:
    [(where, _)] = coverage(xlsx(ext_list(ext(G1)), state=state))
    content = extract_xlsx(xlsx(ext_list(ext(G1)), state=state), capture_id=CAPTURE)
    [entry] = [
        item
        for item in content.segments
        if (item.structured_value or {}).get("kind") == "unsupportedExtension"
    ]
    assert entry.visibility == "hidden"
    assert where["sheetState"] == state


def test_each_sheet_reports_its_own_entries_under_its_own_part() -> None:
    content = xlsx(ext_list(ext(G1)), sheets=2)
    assert [where["part"] for where, _ in coverage(content)] == [SHEET_PART]
    second = content_with_second_sheet_extension()
    assert [(w["part"], w["sheetId"], r["uri"]) for w, r in coverage(second)] == [
        (SHEET_PART, "1", G1),
        ("xl/worksheets/sheet2.xml", "2", G2),
    ]


def content_with_second_sheet_extension() -> bytes:
    base = xlsx(ext_list(ext(G1)), sheets=2)
    with zipfile.ZipFile(io.BytesIO(base)) as package:
        members = {name: package.read(name).decode() for name in package.namelist()}
    members["xl/worksheets/sheet2.xml"] = members["xl/worksheets/sheet2.xml"].replace(
        "</worksheet>", ext_list(ext(G2)) + "</worksheet>"
    )
    return archive(members)


@pytest.mark.parametrize(
    "tail",
    ["", "<extLst/>", "<extLst></extLst>", "<extLst><!-- none --></extLst>"],
)
def test_an_absent_or_empty_extension_list_reports_nothing(tail: str) -> None:
    result = extract_xlsx(xlsx(tail, workbook_tail=tail), capture_id=CAPTURE)

    assert coverage(xlsx(tail, workbook_tail=tail)) == []
    assert result.limitations["extensions"] == 0
    assert [item.semantic_role for item in result.segments] == ["table_cell"]


def test_an_entry_with_no_uri_is_still_an_entry() -> None:
    [(_, record)] = coverage(xlsx(ext_list(ext(None))))
    assert record["uri"] is None
    assert record["uriForm"] is None


def test_a_uri_that_is_not_a_guid_is_never_copied() -> None:
    content = xlsx(
        ext_list(
            ext("https://example.invalid/secret?token=abc123"),
            ext("urn:vendor:thing"),
            ext(G1),
            ext(""),
        )
    )
    records = [record for _, record in coverage(content)]

    assert [(r["uri"], r["uriForm"]) for r in records] == [
        (None, "other"),
        (None, "other"),
        (G1, "guid"),
        (None, "other"),
    ]
    assert "token=abc123" not in repr(records)


def test_a_uri_is_read_as_the_token_it_is() -> None:
    assert uris(xlsx(ext_list(ext(f"  {G1}\n")))) == [G1]


@pytest.mark.parametrize(
    "tail",
    [
        # Written in a foreign vocabulary.
        '<x:extLst xmlns:x="urn:foreign"><x:ext uri="{B025F937-C7B1-47D3-B67F-A62EFF666E3E}"/></x:extLst>',
        # Stripped of its namespace altogether.
        '<extLst xmlns=""><ext uri="{B025F937-C7B1-47D3-B67F-A62EFF666E3E}"/></extLst>',
        # Written in the other SpreadsheetML profile than the root's.
        f'<s:extLst xmlns:s="{STRICT_MAIN}"><s:ext uri="{G1}"/></s:extLst>',
        # An entry of a foreign vocabulary inside the right list.
        '<extLst><f:ext xmlns:f="urn:foreign" uri="{B025F937-C7B1-47D3-B67F-A62EFF666E3E}"/></extLst>',
        # Nested below something other than the root.
        f'<tableParts><extLst><ext uri="{G1}"/></extLst></tableParts>',
        f'<phoneticPr><extLst><ext uri="{G1}"/></extLst></phoneticPr>',
    ],
)
def test_lookalikes_are_not_entries(tail: str) -> None:
    assert coverage(xlsx(tail)) == []
    assert coverage(xlsx(workbook_tail=tail)) == []


def test_lookalikes_inside_the_grid_or_an_entry_are_not_entries() -> None:
    inside_grid = xlsx(sheet_head="")
    with zipfile.ZipFile(io.BytesIO(inside_grid)) as package:
        members = {name: package.read(name).decode() for name in package.namelist()}
    members[SHEET_PART] = (
        members[SHEET_PART]
        .replace("</row>", f'<c r="B1"><extLst><ext uri="{G2}"/></extLst></c></row>')
        .replace("</sheetData>", f'<extLst><ext uri="{G3}"/></extLst></sheetData>')
    )
    assert coverage(archive(members)) == []

    # A list nested in an entry's own body is payload, not a second entry.
    nested = ext_list(ext(G1, ext_list(ext(G2), ext(G3))))
    assert uris(xlsx(nested)) == [G1]


def test_known_owners_extension_lists_are_not_double_counted() -> None:
    """A query table's ``extLst`` is admitted by its own owner, not reported here."""
    query = (
        f'<queryTable xmlns="{MAIN}" connectionId="3" name="Q">'
        + ext_list(ext("urn:test:mashup", LEAKY_BODY))
        + "</queryTable>"
    )
    content = xlsx(
        ext_list(ext(G1)),
        rels=(
            "<Relationships>"
            '<Relationship Id="rIdQ" Target="../queryTables/queryTable1.xml"'
            f' Type="{RELNS}/queryTable"/></Relationships>'
        ),
        extra={"xl/queryTables/queryTable1.xml": query},
    )
    # The package validates the declared type, so declare it.
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        members = {name: package.read(name).decode() for name in package.namelist()}
    members["[Content_Types].xml"] = (
        '<Types><Override PartName="/xl/queryTables/queryTable1.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml'
        '.queryTable+xml"/></Types>'
    )
    result = extract_xlsx(archive(members), capture_id=CAPTURE)

    assert result.limitations["queryTables"] == 1
    assert result.limitations["extensions"] == 1
    assert uris(archive(members)) == [G1]


def test_a_second_extension_list_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="worksheet holds more than one extension"):
        extract_xlsx(xlsx(ext_list(ext(G1)) + ext_list(ext(G2))), capture_id=CAPTURE)
    with pytest.raises(OOXMLInvalid, match="workbook holds more than one extension"):
        extract_xlsx(
            xlsx(workbook_tail="<extLst/>" + ext_list(ext(G2))), capture_id=CAPTURE
        )


@pytest.mark.parametrize(
    ("repeat", "expected"),
    [(G1, G1), (G1.lower(), G1.lower()), (f" {G1}\n", G1)],
    ids=["exact", "case-variant", "whitespace-normalized"],
)
def test_a_repeated_uri_in_one_list_is_each_its_own_record(
    repeat: str, expected: str
) -> None:
    """The schema does not make ``uri`` unique, so a repeat is not a fault."""
    content = xlsx(ext_list(ext(G1), ext(repeat), ext(G2), ext(repeat)))
    records = coverage(content)

    assert [where["extensionIndex"] for where, _ in records] == [0, 1, 2, 3]
    assert [record["extensionIndex"] for _, record in records] == [0, 1, 2, 3]
    assert [record["uri"] for _, record in records] == [G1, expected, G2, expected]
    assert {record["uriForm"] for _, record in records} == {"guid"}
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == 4


def test_repeated_uris_keep_every_record_in_the_workbook_list_too() -> None:
    content = xlsx(workbook_tail=ext_list(ext(G1), ext(G1.lower()), ext(f" {G1} ")))
    records = coverage(content)

    assert [where["extensionIndex"] for where, _ in records] == [0, 1, 2]
    assert [record["owner"] for _, record in records] == ["workbook"] * 3
    assert [record["uri"] for _, record in records] == [G1, G1.lower(), G1]
    assert extract_xlsx(content, capture_id=CAPTURE).limitations["extensions"] == 3


def test_a_repeated_non_guid_uri_is_repeated_without_being_copied() -> None:
    other = "https://example.invalid/secret?token=abc123"
    records = coverage(xlsx(ext_list(ext(other), ext(other.upper()), ext(other))))

    assert [(r["uri"], r["uriForm"]) for _, r in records] == [(None, "other")] * 3
    assert [where["extensionIndex"] for where, _ in records] == [0, 1, 2]
    assert "token=abc123" not in repr(records).lower()


def test_the_same_uri_under_two_owners_is_two_entries() -> None:
    content = xlsx(ext_list(ext(G1)), workbook_tail=ext_list(ext(G1)))
    assert [(r["owner"], r["uri"]) for _, r in coverage(content)] == [
        ("worksheet", G1),
        ("workbook", G1),
    ]


def test_entries_without_a_uri_may_repeat() -> None:
    assert uris(xlsx(ext_list(ext(None), ext(None)))) == [None, None]


def test_the_entries_are_bounded_before_any_record_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Repeated URIs count like any other entries: the bound is on entries.
    content = xlsx(ext_list(ext(G1), ext(G1), ext(G2)))

    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_EXTENSIONS", 2)
    with pytest.raises(OOXMLInvalid, match="worksheet declares too many extensions"):
        extract_xlsx(content, capture_id=CAPTURE)

    monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_EXTENSIONS", 3)
    assert len(coverage(content)) == 3

    with pytest.raises(OOXMLInvalid, match="workbook declares too many extensions"):
        monkeypatch.setattr(ooxml_extractors, "_MAX_XLSX_EXTENSIONS", 1)
        extract_xlsx(xlsx(workbook_tail=ext_list(ext(G1), ext(G2))), capture_id=CAPTURE)


def test_the_entries_count_toward_the_segment_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = xlsx(ext_list(ext(G1), ext(G2), ext(G3)))
    assert len(extract_xlsx(content, capture_id=CAPTURE).segments) == 4

    monkeypatch.setattr(ooxml_extractors, "MAX_SEGMENTS", 3)
    with pytest.raises(OOXMLInvalid, match="too many segments"):
        extract_xlsx(content, capture_id=CAPTURE)


def test_no_payload_attribute_or_secret_is_ever_reported() -> None:
    content = xlsx(
        ext_list(
            ext(
                G1,
                LEAKY_BODY,
                ' credential="hunter2" target="https://example.invalid/secret"',
            ),
            ext("https://example.invalid/secret?token=abc123", LEAKY_BODY),
        ),
        workbook_tail=ext_list(ext(G2, LEAKY_BODY)),
    )
    result = extract_xlsx(content, capture_id=CAPTURE)
    reported = repr(
        [
            (item.text, item.source_locator, item.structured_value)
            for item in result.segments
            if (item.structured_value or {}).get("kind") == "unsupportedExtension"
        ]
    )

    assert len(coverage(content)) == 3
    for secret in SECRETS:
        assert secret not in reported


def test_nothing_outside_the_package_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof by refusal: every way out of the process is made to raise."""
    import builtins
    import socket
    import subprocess
    import urllib.request

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the extension coverage reached outside the package")

    content = xlsx(
        ext_list(
            ext("https://example.invalid/secret", LEAKY_BODY), ext(G1, LEAKY_BODY)
        ),
        workbook_tail=ext_list(ext(G2)),
    )
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    assert len(coverage(content)) == 3


def test_a_strict_workbook_reports_what_a_transitional_one_does() -> None:
    def shape(main: str) -> list[object]:
        content = xlsx(
            ext_list(ext(G1), ext(G2)), workbook_tail=ext_list(ext(G3)), main=main
        )
        return [(dict(where), record) for where, record in coverage(content)]

    assert len(shape(STRICT_MAIN)) == 3
    assert shape(STRICT_MAIN) == shape(MAIN)
    limitations = extract_xlsx(
        xlsx(ext_list(ext(G1)), main=STRICT_MAIN), capture_id=CAPTURE
    ).limitations
    assert limitations["extensions"] == 1


def test_a_foreign_root_namespace_declares_nothing() -> None:
    assert coverage(xlsx(ext_list(ext(G1)), main="urn:foreign")) == []


def test_the_same_package_reports_identically_every_time() -> None:
    content = xlsx(ext_list(ext(G3), ext(None), ext("urn:x")), sheets=2)
    first = extract_xlsx(content, capture_id=CAPTURE)
    second = extract_xlsx(content, capture_id=CAPTURE)

    assert first.segments == second.segments
    assert first.limitations == second.limitations
