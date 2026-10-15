"""XLSX workbook connections: an inventory of what the file stored, inert.

A workbook may relate one connections part, and that part declares the external
data connections the author set up: a database query, an OLAP cube, a web query,
a text import. This route reports that they exist and what the author declared
about refreshing them. It does not perform any of it.

So the contract here is deliberately narrow. A record names the connection's
identity and index, the part and relationship the workbook reached the list by,
the author's own words and the files they named as the source, the declared
credential mode, and the refresh and lifecycle flags -- each preserved as
absent or as the value written, never as an invented default. For the property
sets that hold the actual payload -- ``dbPr`` and its connection string and
command, ``olapPr``, ``webPr``, ``textPr``, the query ``parameters`` and
``extLst`` -- only presence comes out, plus how many parameters the list held.
No connection string, command text, parameter value, password, token or
extension body is copied, nothing named by ``sourceFile`` or ``odcFile`` is
opened or fetched, no query is run, and every record says so by carrying
``refreshPerformed`` and ``externalAccessPerformed`` as false.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    OOXMLInvalid,
    extract_xlsx,
)

CAPTURE = "dcap-" + "a" * 64
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_RELNS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
CONNECTIONS_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml"
)
WORKBOOK_PART = "xl/workbook.xml"
CONNECTIONS_PART = "xl/connections.xml"
# A target nothing in this module ever opens. Every record that names it is
# asserted to carry ``externalAccessPerformed`` false, and the package holds no
# member by this name at all, so a reader that resolved it would fail outright.
SOURCE_FILE = r"\\reporting\warehouse\sales.accdb"
ODC_FILE = "https://example.invalid/odc/Sales%20Warehouse.odc"
# The payload this route must never copy out. It sits inside ``dbPr`` on the
# first connection, and no test expects any of it in a record.
SECRET_COMMAND = "EXEC dbo.LoadSales @Region = 'EMEA'"
SECRET_CONNECTION_STRING = (
    "Provider=SQLOLEDB;Data Source=warehouse;User ID=sa;Password=hunter2;"
)
SECRET_PARAMETER = "EMEA"
SECRETS = (
    SECRET_COMMAND,
    SECRET_CONNECTION_STRING,
    SECRET_PARAMETER,
    "hunter2",
    "Password",
    "ey.FAKE.token",
)
# A database connection declaring every field this route reports, so one record
# shows the whole surface. ``description`` is written empty on purpose: a
# description declared blank is not a description never declared. The ``dbPr``
# beside it carries the payload, and ``parameters`` carries a value, so a reader
# that copied either would show it.
DB = (
    "<connection"
    ' id="7"'
    ' name="Sales warehouse"'
    ' description=""'
    ' type="1"'
    f' sourceFile="{SOURCE_FILE}"'
    f' odcFile="{ODC_FILE}"'
    ' credentials="stored"'
    ' background="1"'
    ' deleted="0"'
    ' new="false"'
    ' refreshOnLoad="1"'
    ' saveData="0"'
    ' keepAlive="1"'
    ' onlyUseConnectionFile="0"'
    ' reconnectionMethod="2"'
    ' refreshedVersion="6"'
    ' minRefreshableVersion="3"'
    ' singleSignOnId="sso-emea"'
    ">"
    f'<dbPr connection="{SECRET_CONNECTION_STRING}" command="{SECRET_COMMAND}"'
    ' commandType="2"/>'
    '<parameters count="2">'
    f'<parameter name="Region" parameterType="prompt" string="{SECRET_PARAMETER}"/>'
    '<parameter name="Year" parameterType="value" integer="2026"/>'
    "</parameters>"
    "<extLst><ext uri=\"{FF2B5EF4-FFF2-40B4-BE49-F238E27FC236}\">"
    '<x:token xmlns:x="urn:fake" value="ey.FAKE.token"/></ext></extLst>'
    "</connection>"
)
# A web query declaring almost nothing, so the absent-versus-present split is
# visible beside the record above.
WEB = (
    '<connection id="9" name="Weekly feed">'
    '<webPr xml="1" sourceData="1"/>'
    "</connection>"
)
CONNECTIONS = f'<connections xmlns="{MAIN}">{DB}{WEB}</connections>'


def archive(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in members.items():
            package.writestr(name, content)
    return buffer.getvalue()


def xlsx(
    connections: str | None = CONNECTIONS,
    *,
    content_type: str = CONNECTIONS_CT,
    relationship: str = f"{RELNS}/connections",
    main: str = MAIN,
    rels: str | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """One sheet, and a workbook relating one connections part by default.

    ``connections`` of ``None`` relates no part at all, which is the absent case
    the empty-list case has to stay distinguishable from.
    """
    overrides = (
        ""
        if connections is None
        else f'<Override PartName="/{CONNECTIONS_PART}" ContentType="{content_type}"/>'
    )
    related = (
        ""
        if connections is None
        else (
            '<Relationship Id="rIdX" Target="connections.xml"'
            f' Type="{relationship}"/>'
        )
    )
    members = {
        "[Content_Types].xml": f"<Types>{overrides}</Types>",
        WORKBOOK_PART: (
            f'<workbook xmlns="{main}" xmlns:r="{RELNS}"><sheets>'
            '<sheet name="Sales" sheetId="7" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": rels
        or (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            f' Type="{RELNS}/worksheet"/>'
            f"{related}"
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{main}"><sheetData>'
            '<row r="1"><c r="A1" t="str"><v>Region</v></c></row>'
            "</sheetData></worksheet>"
        ),
        **(extra or {}),
    }
    if connections is not None:
        members[CONNECTIONS_PART] = connections
    return archive(members)


def inventory(content: bytes) -> list[tuple[dict[str, object], dict[str, object]]]:
    """Every connection inventory record, in the order it was reported."""
    return [
        (dict(item.source_locator), dict(item.structured_value or {}))
        for item in extract_xlsx(content, capture_id=CAPTURE).segments
        if item.semantic_role == "object_inventory"
        and (item.structured_value or {}).get("kind") == "connection"
    ]


# Where every record in this module says it came from. Records differ from this
# only in the connection's own index, id and name.
WHERE: dict[str, object] = {
    "captureId": CAPTURE,
    "part": WORKBOOK_PART,
    "connectionsPart": CONNECTIONS_PART,
    "connectionsRelationshipId": "rIdX",
    "connectionIndex": 0,
    "connectionId": 7,
    "connection": "Sales warehouse",
}
# Every field the database connection above declared, and the five sets it did
# not. Pinned by full equality, so a field this route starts reporting -- or
# stops -- fails here rather than passing unnoticed.
DB_RECORD: dict[str, object] = {
    "kind": "connection",
    "id": 7,
    "credentials": "stored",
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "name": "Sales warehouse",
    "description": "",
    "sourceFile": SOURCE_FILE,
    "odcFile": ODC_FILE,
    "singleSignOnId": "sso-emea",
    "keepAlive": True,
    "new": False,
    "deleted": False,
    "onlyUseConnectionFile": False,
    "background": True,
    "refreshOnLoad": True,
    "saveData": False,
    "type": 1,
    "reconnectionMethod": 2,
    "refreshedVersion": 6,
    "minRefreshableVersion": 3,
    "dbPrDeclared": True,
    "olapPrDeclared": False,
    "webPrDeclared": False,
    "textPrDeclared": False,
    "parametersDeclared": True,
    "extLstDeclared": True,
    "parameterCount": 2,
}
# The web query, which declared one flag's worth of nothing: every attribute it
# left out is absent rather than defaulted to the schema's false or to a
# version it never claimed.
WEB_RECORD: dict[str, object] = {
    "kind": "connection",
    "id": 9,
    "credentials": None,
    "refreshPerformed": False,
    "externalAccessPerformed": False,
    "name": "Weekly feed",
    "description": None,
    "sourceFile": None,
    "odcFile": None,
    "singleSignOnId": None,
    "keepAlive": None,
    "new": None,
    "deleted": None,
    "onlyUseConnectionFile": None,
    "background": None,
    "refreshOnLoad": None,
    "saveData": None,
    "type": None,
    "reconnectionMethod": None,
    "refreshedVersion": None,
    "minRefreshableVersion": None,
    "dbPrDeclared": False,
    "olapPrDeclared": False,
    "webPrDeclared": True,
    "textPrDeclared": False,
    "parametersDeclared": False,
    "extLstDeclared": False,
    "parameterCount": None,
}


def test_two_connections_are_inventoried_in_source_order() -> None:
    """Both connections, in the order the list wrote them, pinned exactly."""
    assert inventory(xlsx()) == [
        (WHERE, DB_RECORD),
        (
            {**WHERE, "connectionIndex": 1, "connectionId": 9, "connection": "Weekly feed"},
            WEB_RECORD,
        ),
    ]


def test_connections_are_inert_inventory_after_the_workbook_content() -> None:
    """Each is visible inventory, carries no text, and comes after the cells."""
    # The connection's own extension list is reported as coverage beside them,
    # which is not a connection and is pinned by the extension tests.
    segments = [
        item
        for item in extract_xlsx(xlsx(), capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") != "unsupportedExtension"
    ]
    roles = [item.semantic_role for item in segments]
    assert roles == ["table_cell", "object_inventory", "object_inventory"]
    for item in segments[1:]:
        assert item.extraction_method == "inventory"
        assert item.visibility == "visible"
        assert item.text is None


def test_the_connection_count_is_reported_in_the_extraction_metadata() -> None:
    """Counted beside ``sheets`` and ``definedNames``, the other two workbook-wide
    figures the extraction already reports."""
    assert extract_xlsx(xlsx(), capture_id=CAPTURE).limitations["connections"] == 2


def test_a_workbook_relating_no_connections_part_reports_none() -> None:
    """Absent is absent: no part, no records, and a count of zero."""
    extraction = extract_xlsx(xlsx(None), capture_id=CAPTURE)
    assert extraction.limitations["connections"] == 0
    assert inventory(xlsx(None)) == []


def test_an_empty_connections_list_reports_none_but_is_still_read() -> None:
    """Empty is not absent: the part is resolved, validated and found empty."""
    assert inventory(xlsx(f'<connections xmlns="{MAIN}"/>')) == []


def test_the_strict_profile_resolves_and_reads_the_same_list() -> None:
    """The Strict relationship type and Strict SpreadsheetML namespace qualify."""
    strict = (
        f'<connections xmlns="{STRICT_MAIN}">'
        '<connection id="7" name="Sales warehouse" credentials="stored"'
        f' sourceFile="{SOURCE_FILE}" odcFile="{ODC_FILE}" description=""'
        ' type="1" background="1" deleted="0" new="false" refreshOnLoad="1"'
        ' saveData="0" keepAlive="1" onlyUseConnectionFile="0"'
        ' reconnectionMethod="2" refreshedVersion="6" minRefreshableVersion="3"'
        ' singleSignOnId="sso-emea">'
        f'<dbPr connection="{SECRET_CONNECTION_STRING}" command="{SECRET_COMMAND}"/>'
        f'<parameters count="2"><parameter name="Region" string="{SECRET_PARAMETER}"/>'
        '<parameter name="Year" integer="2026"/></parameters>'
        '<extLst><ext uri="{FF2B5EF4-FFF2-40B4-BE49-F238E27FC236}"/></extLst>'
        "</connection></connections>"
    )
    assert inventory(
        xlsx(strict, relationship=f"{STRICT_RELNS}/connections")
    ) == [(WHERE, DB_RECORD)]


def test_a_deleted_or_hidden_policy_claim_stays_a_claim() -> None:
    """``deleted`` and ``new`` are the author's lifecycle words, not visibility.

    A connection the author marked deleted is still a thing the file stored, so
    it is reported, in sight, with the claim carried in the record. Nothing is
    hidden or dropped on account of it, and no refresh is attempted for the one
    that asks to refresh on load.
    """
    marked = (
        f'<connections xmlns="{MAIN}">'
        '<connection id="1" deleted="1" new="1" refreshOnLoad="1"'
        ' background="true" onlyUseConnectionFile="on"/>'
        "</connections>"
    )
    (locator, record), = inventory(xlsx(marked))
    # No ``name`` was written, so the locator carries no ``connection`` key at
    # all rather than an invented empty one.
    assert locator == {
        key: 1 if key == "connectionId" else value
        for key, value in WHERE.items()
        if key != "connection"
    }
    assert (record["deleted"], record["new"], record["refreshOnLoad"]) == (
        True,
        True,
        True,
    )
    assert (record["background"], record["onlyUseConnectionFile"]) == (True, True)
    assert record["refreshPerformed"] is False
    assert record["externalAccessPerformed"] is False


def test_every_record_states_that_nothing_was_refreshed_or_reached() -> None:
    """The two standing claims, on every record, whatever the connection asked."""
    records = inventory(xlsx())
    assert records
    for _locator, record in records:
        assert record["refreshPerformed"] is False
        assert record["externalAccessPerformed"] is False


def test_no_payload_or_credential_reaches_a_record() -> None:
    """Presence and a parameter count come out; the payloads never do.

    The database connection above stores a connection string with a password in
    it, a command, two parameter values and an extension body holding a token.
    None of it may appear anywhere in the extraction -- not in a record, not in
    a locator, not in a cell.
    """
    extraction = extract_xlsx(xlsx(), capture_id=CAPTURE)
    rendered = repr(
        [
            (item.source_locator, item.structured_value, item.text)
            for item in extraction.segments
        ]
    )
    for secret in SECRETS:
        assert secret not in rendered
    (_locator, record), _web = inventory(xlsx())
    assert record["dbPrDeclared"] is True
    assert record["parameterCount"] == 2


def test_each_declared_property_kind_is_reported_as_presence_only() -> None:
    """All six kinds at once: each declared, none of them read."""
    every = (
        f'<connections xmlns="{MAIN}">'
        '<connection id="1">'
        f'<dbPr command="{SECRET_COMMAND}"/><olapPr rowDrillCount="1000"/>'
        '<webPr url="https://example.invalid/feed"/><textPr fileType="win"/>'
        f'<parameters count="1"><parameter string="{SECRET_PARAMETER}"/></parameters>'
        "<extLst/>"
        "</connection></connections>"
    )
    (_locator, record), = inventory(xlsx(every))
    assert {key: record[key] for key in record if key.endswith("Declared")} == {
        "dbPrDeclared": True,
        "olapPrDeclared": True,
        "webPrDeclared": True,
        "textPrDeclared": True,
        "parametersDeclared": True,
        "extLstDeclared": True,
    }
    assert record["parameterCount"] == 1


def test_a_parameter_list_declaring_no_parameters_counts_zero() -> None:
    """Zero parameters inside a declared list, which is not an absent list."""
    (_locator, record), = inventory(
        xlsx(
            f'<connections xmlns="{MAIN}">'
            '<connection id="1"><parameters count="0"/></connection></connections>'
        )
    )
    assert (record["parametersDeclared"], record["parameterCount"]) == (True, 0)


def test_a_connections_list_at_the_bound_is_accepted() -> None:
    """4,096 connections resolve; the 4,097th is refused."""
    at_bound = "".join(f'<connection id="{index}"/>' for index in range(4_096))
    assert len(inventory(xlsx(f'<connections xmlns="{MAIN}">{at_bound}</connections>'))) == (
        4_096
    )


def test_a_connections_list_over_the_bound_is_refused() -> None:
    over = "".join(f'<connection id="{index}"/>' for index in range(4_097))
    with pytest.raises(OOXMLInvalid, match="too many connections"):
        extract_xlsx(
            xlsx(f'<connections xmlns="{MAIN}">{over}</connections>'),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    "connection",
    [
        pytest.param("<connection/>", id="missing"),
        pytest.param('<connection id=""/>', id="empty"),
        pytest.param('<connection id="-1"/>', id="negative"),
        pytest.param('<connection id="7.5"/>', id="fractional"),
        pytest.param('<connection id=" 7"/>', id="padded"),
        pytest.param('<connection id="id7"/>', id="not-a-number"),
        pytest.param('<connection id="4294967296"/>', id="over-the-type"),
    ],
)
def test_a_connection_without_a_readable_id_is_refused(connection: str) -> None:
    """The id is the identity a query table cites, so it is required and exact."""
    with pytest.raises(OOXMLInvalid, match="connection id is missing or malformed"):
        extract_xlsx(
            xlsx(f'<connections xmlns="{MAIN}">{connection}</connections>'),
            capture_id=CAPTURE,
        )


def test_a_repeated_connection_id_is_refused() -> None:
    """Two connections under one id leave a citation naming both."""
    with pytest.raises(OOXMLInvalid, match="connection id is repeated"):
        extract_xlsx(
            xlsx(
                f'<connections xmlns="{MAIN}">'
                '<connection id="7"/><connection id="7"/></connections>'
            ),
            capture_id=CAPTURE,
        )


def test_two_spellings_of_one_id_are_one_id() -> None:
    """The id is numeric, so ``07`` and ``7`` are the same connection."""
    with pytest.raises(OOXMLInvalid, match="connection id is repeated"):
        extract_xlsx(
            xlsx(
                f'<connections xmlns="{MAIN}">'
                '<connection id="7"/><connection id="07"/></connections>'
            ),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    "flag", ["keepAlive", "new", "deleted", "onlyUseConnectionFile", "background", "refreshOnLoad", "saveData"]
)
def test_a_malformed_boolean_is_refused(flag: str) -> None:
    """The type is closed, so an unreadable flag is not read as either value."""
    with pytest.raises(OOXMLInvalid, match="connection flag is malformed"):
        extract_xlsx(
            xlsx(
                f'<connections xmlns="{MAIN}">'
                f'<connection id="1" {flag}="yes"/></connections>'
            ),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("type", "query"),
        ("type", "-1"),
        ("type", "4294967296"),
        ("reconnectionMethod", "1.5"),
        ("refreshedVersion", "256"),
        ("minRefreshableVersion", "256"),
        ("minRefreshableVersion", "-3"),
    ],
)
def test_a_malformed_unsigned_integer_is_refused(attribute: str, value: str) -> None:
    """Each is bounded by the type the schema gives it, not clamped into it."""
    with pytest.raises(OOXMLInvalid, match="numeric attribute is malformed"):
        extract_xlsx(
            xlsx(
                f'<connections xmlns="{MAIN}">'
                f'<connection id="1" {attribute}="{value}"/></connections>'
            ),
            capture_id=CAPTURE,
        )


def test_an_undefined_credentials_mode_is_refused() -> None:
    """``ST_CredMethod`` is closed, so a mode outside it is not guessed at."""
    with pytest.raises(OOXMLInvalid, match="credentials mode is not a defined value"):
        extract_xlsx(
            xlsx(
                f'<connections xmlns="{MAIN}">'
                '<connection id="1" credentials="Stored"/></connections>'
            ),
            capture_id=CAPTURE,
        )


def test_a_repeated_property_set_is_refused() -> None:
    """The schema gives one of each, so nothing here picks between two."""
    with pytest.raises(OOXMLInvalid, match="declares a property set twice"):
        extract_xlsx(
            xlsx(
                f'<connections xmlns="{MAIN}">'
                '<connection id="1"><dbPr/><dbPr/></connection></connections>'
            ),
            capture_id=CAPTURE,
        )


def test_a_relationship_of_another_type_reaches_no_connections_list() -> None:
    """Only the two defined URIs qualify; a lookalike route resolves nothing."""
    assert (
        inventory(xlsx(relationship=f"{RELNS}/externalLink"))
        == []
    )


def test_a_part_declared_as_something_else_is_refused() -> None:
    """The declared content type is what qualifies the part the route reached."""
    with pytest.raises(
        OOXMLInvalid, match="connections relationship does not name a connections part"
    ):
        extract_xlsx(
            xlsx(content_type="application/xml"),
            capture_id=CAPTURE,
        )


def test_a_part_declared_only_by_its_extension_is_refused() -> None:
    """A default for ``.xml`` says what every part is, which qualifies none."""
    package = archive(
        {
            "[Content_Types].xml": (
                '<Types><Default Extension="xml" ContentType="application/xml"/>'
                "</Types>"
            ),
            WORKBOOK_PART: (
                f'<workbook xmlns="{MAIN}" xmlns:r="{RELNS}"><sheets>'
                '<sheet name="Sales" sheetId="7" r:id="rId1"/>'
                "</sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                "<Relationships>"
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                f' Type="{RELNS}/worksheet"/>'
                '<Relationship Id="rIdX" Target="connections.xml"'
                f' Type="{RELNS}/connections"/>'
                "</Relationships>"
            ),
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>',
            CONNECTIONS_PART: CONNECTIONS,
        }
    )
    with pytest.raises(
        OOXMLInvalid, match="connections relationship does not name a connections part"
    ):
        extract_xlsx(package, capture_id=CAPTURE)


def test_two_qualifying_relationships_are_ambiguous() -> None:
    """Which list the workbook meant is unsettled, so the workbook is refused."""
    with pytest.raises(
        OOXMLInvalid, match="names more than one connections relationship"
    ):
        extract_xlsx(
            xlsx(
                rels=(
                    "<Relationships>"
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                    f' Type="{RELNS}/worksheet"/>'
                    '<Relationship Id="rIdX" Target="connections.xml"'
                    f' Type="{RELNS}/connections"/>'
                    '<Relationship Id="rIdY" Target="connections.xml"'
                    f' Type="{STRICT_RELNS}/connections"/>'
                    "</Relationships>"
                )
            ),
            capture_id=CAPTURE,
        )


def test_an_external_connections_relationship_is_refused() -> None:
    """Nothing here leaves the package, so an external route is not followed."""
    with pytest.raises(OOXMLInvalid, match="connections relationship is external"):
        extract_xlsx(
            xlsx(
                rels=(
                    "<Relationships>"
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                    f' Type="{RELNS}/worksheet"/>'
                    '<Relationship Id="rIdX" TargetMode="External"'
                    f' Target="{ODC_FILE}" Type="{RELNS}/connections"/>'
                    "</Relationships>"
                )
            ),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    ("root", "message"),
    [
        pytest.param(
            '<connections xmlns="urn:fake"/>',
            "unsupported namespace",
            id="foreign-namespace",
        ),
        pytest.param(
            "<connections/>", "unsupported namespace", id="unqualified"
        ),
        pytest.param(
            f'<externalLink xmlns="{MAIN}"/>',
            "not rooted at its own list",
            id="wrong-root",
        ),
    ],
)
def test_a_part_whose_root_is_not_a_connections_list_is_refused(
    root: str, message: str
) -> None:
    """The declared type promised a vocabulary; the root is where it is kept."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_xlsx(xlsx(root), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            '<connection xmlns="urn:fake" id="1"/>', id="foreign-connection"
        ),
        pytest.param(
            '<x:connection xmlns:x="urn:fake" id="1"/>', id="foreign-prefix"
        ),
        pytest.param(
            '<connectionGroup><connection id="1"/></connectionGroup>', id="nested"
        ),
        pytest.param('<connexion id="1"/>', id="misspelled"),
    ],
)
def test_a_lookalike_connection_declares_nothing(body: str) -> None:
    """Off the schema's own route -- foreign, reprefixed or nested -- is transparent.

    The nested case carries a well-formed qualified ``connection`` one level too
    deep, so a reader descending by local name would report it.
    """
    assert inventory(xlsx(f'<connections xmlns="{MAIN}">{body}</connections>')) == []


def test_a_list_that_wrote_no_namespace_declares_nothing_rather_than_guessing() -> None:
    """An unqualified list is refused at the root, never read by local name."""
    with pytest.raises(OOXMLInvalid, match="unsupported namespace"):
        extract_xlsx(
            xlsx('<connections><connection id="1"/></connections>'),
            capture_id=CAPTURE,
        )


def test_a_qualified_connection_beside_a_lookalike_is_still_reported() -> None:
    """The real one comes through, and its index counts only the qualified ones."""
    mixed = (
        f'<connections xmlns="{MAIN}">'
        '<x:connection xmlns:x="urn:fake" id="99"/>'
        '<connection id="7" name="Sales warehouse"/>'
        "</connections>"
    )
    (locator, record), = inventory(xlsx(mixed))
    assert locator == WHERE
    assert record["id"] == 7


def test_over_long_free_text_is_clipped_rather_than_refused() -> None:
    """None of it is an identity, so it is cut to the bound instead of refused."""
    long = "p" * 400
    (_locator, record), = inventory(
        xlsx(
            f'<connections xmlns="{MAIN}">'
            f'<connection id="1" name="{long}" description="{long}"'
            f' sourceFile="{long}" odcFile="{long}" singleSignOnId="{long}"/>'
            "</connections>"
        )
    )
    for field in ("name", "description", "sourceFile", "odcFile", "singleSignOnId"):
        assert record[field] == "p" * 256


def test_nothing_outside_the_package_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proof by refusal: every way out of the process is made to raise.

    The connection above names a UNC path and an HTTPS ODC file, asks to
    refresh on load and stores a command. If any of that were acted on, one of
    these would fire. The extraction has to complete and still report both
    records.
    """
    import builtins
    import socket
    import subprocess
    import urllib.request

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the connections inventory reached outside the package")

    # The package is already a ``BytesIO``, so nothing legitimate opens a file.
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    records = inventory(xlsx())
    assert [record["id"] for _locator, record in records] == [7, 9]
    assert records[0][1]["sourceFile"] == SOURCE_FILE
    assert records[0][1]["odcFile"] == ODC_FILE
