"""PPTX embedded objects: the OLE and package objects a graphic frame declares.

A graphic frame may declare one object on the schema's own route -- its direct
``a:graphic``, the ``a:graphicData`` directly below that, and a ``p:oleObj``
directly below that. Routing is on the element, not on what ``graphicData``
names in its ``uri``, and only that one route declares anything: a lookalike
nested deeper, hung below the object's own preview, or written in a vocabulary
that merely shares a local name is passed over.

Whether the object is embedded or linked comes from one direct ``p:embed`` or
``p:link`` and from nothing else, and the relationship the object names has to
agree with it. The ``p:pic`` beside them is the preview image standing in for
the object on the slide; it is recorded as declared and never opened, resolved
or inventoried as a picture of its own.

The whole slice is inventory. Nothing parses, decodes, executes, launches or
fetches an object: an embedded one contributes its safe part name, the type the
package declares for it and the byte length the package already measured, a
linked one is carried exactly as written beside the scheme its own text names,
and a ``progId`` is the authoring application's claim rather than a server
anything here resolves.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The PresentationML main namespaces ``p:oleObj`` and its children are
# qualified in, the DrawingML ones the frame's graphic route is, and the
# relationships namespaces the object's ``r:id`` is. Each has a Transitional
# and a Strict spelling, and a package written to either profile reads the same.
PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
STRICT_PML = "http://purl.oclc.org/ooxml/presentationml/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
STRICT_DRAWING_NS = "http://purl.oclc.org/ooxml/drawingml/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
OLE_TYPE = REL_NS + "/oleObject"
STRICT_OLE_TYPE = STRICT_REL_NS + "/oleObject"
PACKAGE_TYPE = REL_NS + "/package"
STRICT_PACKAGE_TYPE = STRICT_REL_NS + "/package"
IMAGE_TYPE = REL_NS + "/image"
# What a real authoring tool writes on the ``graphicData`` holding an object.
# It is realism, not a route: the element below is what declares the object.
OLE_URI = "http://schemas.openxmlformats.org/presentationml/2006/ole"
# Bytes at the embedded targets that are not an OLE stream, a package, or
# anything any server would open -- and, for the package target, not an archive
# either. The proof that this slice never reaches an object is that these are
# never opened: only the length the package measured while validating the
# member is ever reported.
OBJECT_BYTES = "not an OLE compound stream"
PACKAGE_BYTES = "not a package and not an archive"
OBJECT_MEMBERS = {
    "ppt/embeddings/oleObject1.bin": OBJECT_BYTES,
    "ppt/embeddings/Workbook1.xlsx": PACKAGE_BYTES,
}
OBJECT_DEFAULTS = (
    '<Default Extension="bin" '
    'ContentType="application/vnd.openxmlformats-officedocument.oleObject"/>'
    '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sheet"/>'
)
# The package case-folds every content type it reads, as it does for an image
# or a media part, so a record reports the folded form of what was declared.
OLE_CT = "application/vnd.openxmlformats-officedocument.oleobject"
PACKAGE_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def relation(
    relation_id: str = "rId2",
    *,
    target: str = "../embeddings/oleObject1.bin",
    kind: str = OLE_TYPE,
    external: bool = False,
) -> str:
    """One ``Relationship`` of the slide's own relationship part."""
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{relation_id}" Target="{target}" Type="{kind}"{mode}/>'


OBJECT_RELATION = relation()
LINK_RELATION = relation(target="../../books/Budget.xlsx", external=True)
PACKAGE_RELATION = relation(target="../embeddings/Workbook1.xlsx", kind=PACKAGE_TYPE)


def obj(
    *,
    mode: str | None = "embed",
    mode_extra: str = "",
    previews: int = 0,
    relation_id: str | None = "rId2",
    pml: str = PML,
    rel: str = REL_NS,
    prefix: str = "p",
    extra: str = "",
    children: str = "",
) -> str:
    """One ``p:oleObj``, bound to ``pml`` whatever prefix spells it.

    The prefix is immaterial -- a name is the namespace it is bound to and its
    local part -- so the element qualifies whether a file spells it ``p:oleObj``
    or binds another prefix to the same namespace, and the same local name in
    another namespace is a different name entirely. ``children`` goes after the
    mode and the previews, which is where a lookalike can be placed inside the
    object without being one of its own declarations.
    """
    named = "" if relation_id is None else f' r:id="{relation_id}"'
    body = "" if mode is None else f"<{prefix}:{mode}{mode_extra}/>"
    body += f"<{prefix}:pic/>" * previews + children
    return (
        f'<{prefix}:oleObj xmlns:{prefix}="{pml}" xmlns:r="{rel}"{named}{extra}>'
        f"{body}</{prefix}:oleObj>"
    )


OBJECT = obj()
LINKED = obj(mode="link")


def graphic(
    body: str, *, namespace: str = DRAWING_NS, uri: str | None = OLE_URI
) -> str:
    """The ``a:graphic``/``a:graphicData`` route a frame declares a graphic on."""
    attribute = "" if uri is None else f' uri="{uri}"'
    return (
        f'<a:graphic xmlns:a="{namespace}">'
        f"<a:graphicData{attribute}>{body}</a:graphicData></a:graphic>"
    )


def frame(body: str, shape_id: str = "3") -> str:
    """One ``p:graphicFrame`` holding the given markup."""
    return (
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{shape_id}"/>'
        f"</p:nvGraphicFramePr>{body}</p:graphicFrame>"
    )


def group(shape_id: str, children: str) -> str:
    return (
        f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{shape_id}"/></p:nvGrpSpPr><p:grpSpPr/>'
        f"{children}</p:grpSp>"
    )


def pptx(
    tree: str,
    *,
    hidden: bool = False,
    slide_rels: str = OBJECT_RELATION,
    members: dict[str, str] | None = None,
    content_types: str = OBJECT_DEFAULTS,
) -> bytes:
    """Build a one-slide presentation holding the given shape tree."""
    shown = ' show="0"' if hidden else ""
    parts = {
        "[Content_Types].xml": f"<Types>{content_types}</Types>",
        "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r">'
        f'<p:sldIdLst><p:sldId id="256" r:id="rId1"{shown}/>'
        "</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": "<Relationships>"
        '<Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{PML}"><p:cSld>'
        f"<p:spTree>{tree}</p:spTree></p:cSld></p:sld>",
        "ppt/slides/_rels/slide1.xml.rels": (
            f"<Relationships>{slide_rels}</Relationships>"
        ),
    }
    parts.update(OBJECT_MEMBERS if members is None else members)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def records(content: bytes) -> list[ExtractedSegment]:
    """Every object record the frame route produced, in emission order."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") == "oleObject"
    ]


def segments(content: bytes) -> list[tuple[str, object]]:
    """The (role, kind) of every segment, so a record is seen in context."""
    return [
        (item.semantic_role, (item.structured_value or {}).get("kind"))
        for item in extract_pptx(content, capture_id=CAPTURE).segments
    ]


# The provenance the default fixture's record carries, which is the frame's own
# shape identity on the slide.
EXPECTED = {
    "captureId": CAPTURE,
    "part": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
    "shapeId": "3",
    "shapeKind": "graphicFrame",
    "groupPath": [],
    "zOrder": 0,
    "zPath": [0],
    "geometry": None,
    "placeholderType": None,
    "placeholderIndex": None,
}
# What the default fixture's record says about the object part it embeds.
EMBEDDED = {
    "kind": "oleObject",
    "oleMode": "embed",
    "oleRelationshipId": "rId2",
    "oleRelationshipType": OLE_TYPE,
    "oleTarget": "ppt/embeddings/oleObject1.bin",
    "oleExternal": False,
    "oleScheme": None,
    "oleUpdateAutomatic": None,
    "olePart": "ppt/embeddings/oleObject1.bin",
    "oleContentType": OLE_CT,
    "oleBytes": len(OBJECT_BYTES),
    "oleName": None,
    "oleProgId": None,
    "oleShapeId": None,
    "oleShowAsIcon": None,
    "oleImageWidth": None,
    "oleImageHeight": None,
    "olePreviewDeclared": False,
}
# What a record says about a target outside the package: the reference itself,
# and nothing invented for it.
OUTSIDE = {
    "olePart": None,
    "oleContentType": None,
    "oleBytes": None,
}
# What a record says about the package embedded as one object.
PACKAGED = EMBEDDED | {
    "oleRelationshipType": PACKAGE_TYPE,
    "oleTarget": "ppt/embeddings/Workbook1.xlsx",
    "olePart": "ppt/embeddings/Workbook1.xlsx",
    "oleContentType": PACKAGE_CT,
    "oleBytes": len(PACKAGE_BYTES),
}


def test_an_embedded_object_is_inventoried_at_its_frame() -> None:
    """One record, at the frame's own place, carrying the declaration and what
    the package already knows about the part it names."""
    content = pptx(frame(graphic(OBJECT)))

    record = records(content)[0]
    assert (record.semantic_role, record.extraction_method) == (
        "object_inventory",
        "inventory",
    )
    assert record.visibility == "visible"
    assert dict(record.source_locator) == EXPECTED
    assert record.structured_value == EMBEDDED


def test_an_object_contributes_no_text() -> None:
    """The frame's one segment is the inventory record: nothing is read out of
    the object, and the frame carries no text of its own instead."""
    assert segments(pptx(frame(graphic(OBJECT)))) == [
        ("object_inventory", "oleObject")
    ]


def test_a_linked_object_carries_its_target_and_scheme_verbatim() -> None:
    """``p:link`` says the object is a reference out of the package, and the
    reference is inventory: nothing fetches, probes or normalises it."""
    content = pptx(
        frame(graphic(obj(mode="link"))),
        slide_rels=relation(target="HTTPS://files.example/Budget.XLSX", external=True),
    )

    assert records(content)[0].structured_value == EMBEDDED | OUTSIDE | {
        "oleMode": "link",
        "oleTarget": "HTTPS://files.example/Budget.XLSX",
        "oleExternal": True,
        "oleScheme": "https",
    }


def test_a_relative_link_target_names_no_scheme() -> None:
    content = pptx(frame(graphic(LINKED)), slide_rels=LINK_RELATION)

    assert records(content)[0].structured_value == EMBEDDED | OUTSIDE | {
        "oleMode": "link",
        "oleTarget": "../../books/Budget.xlsx",
        "oleExternal": True,
        "oleScheme": None,
    }


@pytest.mark.parametrize(
    ("declared", "expected"), [("1", True), ("true", True), ("0", False)]
)
def test_a_links_update_flag_is_kept_as_declared(declared: str, expected: bool) -> None:
    """The flag says whether a player would refresh the link. It is recorded,
    never acted on, and an absent one is reported absent rather than defaulted."""
    content = pptx(
        frame(graphic(obj(mode="link", mode_extra=f' updateAutomatic="{declared}"'))),
        slide_rels=LINK_RELATION,
    )

    assert records(content)[0].structured_value["oleUpdateAutomatic"] is expected


def test_an_embedded_package_qualifies_as_the_object() -> None:
    """A whole package embedded as one object is reached by the ``package``
    relationship type, and is reported by name, type and length alone."""
    content = pptx(frame(graphic(OBJECT)), slide_rels=PACKAGE_RELATION)

    assert records(content)[0].structured_value == PACKAGED


def test_the_objects_own_claims_are_reported_as_written() -> None:
    """Three distinct claims, kept distinct and uninterpreted: no server is
    looked up for the ``progId`` and the extents are not scaled against
    anything."""
    content = pptx(
        frame(
            graphic(
                obj(
                    extra=' name="Budget" progId="Excel.Sheet.12" spid="_x0000_s1026"'
                    ' showAsIcon="1" imgW="2743200" imgH="1143000"'
                )
            )
        )
    )

    assert records(content)[0].structured_value == EMBEDDED | {
        "oleName": "Budget",
        "oleProgId": "Excel.Sheet.12",
        "oleShapeId": "_x0000_s1026",
        "oleShowAsIcon": True,
        "oleImageWidth": 2743200,
        "oleImageHeight": 1143000,
    }


def test_the_claims_are_clipped_into_the_bounded_record() -> None:
    """They ride in a bounded JSON value beside the keys that locate the frame,
    so each is clipped rather than carried at whatever length it was written."""
    content = pptx(frame(graphic(obj(extra=f' progId="{"P" * 400}"'))))

    assert records(content)[0].structured_value["oleProgId"] == "P" * 256


def test_the_strict_profile_is_read_the_same_way() -> None:
    """Strict PresentationML, Strict DrawingML, Strict relationships and the
    Strict relationship type all qualify, and resolve to the same record."""
    content = pptx(
        frame(
            graphic(
                obj(pml=STRICT_PML, rel=STRICT_REL_NS, prefix="s"),
                namespace=STRICT_DRAWING_NS,
            )
        ),
        slide_rels=relation(
            kind=STRICT_PACKAGE_TYPE, target="../embeddings/Workbook1.xlsx"
        ),
    )

    assert records(content)[0].structured_value == PACKAGED | {
        "oleRelationshipType": STRICT_PACKAGE_TYPE
    }


def test_the_element_declares_the_object_whatever_the_uri_says() -> None:
    """The route is matched on the elements it is made of, so graphic data that
    names no vocabulary at all still holds the object below it."""
    content = pptx(frame(graphic(OBJECT, uri=None)))

    assert records(content)[0].structured_value == EMBEDDED


def test_a_declared_preview_is_recorded_and_never_opened() -> None:
    """The preview is a picture of the object. It is reported as declared, is
    not inventoried as a picture of its own, and the image it names is never
    resolved -- the relationship below it does not even exist. An ``oleObj``
    carried below it is not on the frame's route either, so the preview adds no
    second declaration."""
    preview = (
        f'<p:pic xmlns:p="{PML}"><p:nvPicPr><p:cNvPr id="4"/><p:cNvPicPr/>'
        f'<p:nvPr/></p:nvPicPr><p:blipFill><a:blip xmlns:a="{DRAWING_NS}" '
        f'xmlns:r="{REL_NS}" r:embed="rId404"/></p:blipFill>{OBJECT}</p:pic>'
    )
    content = pptx(frame(graphic(obj(children=preview))))

    assert segments(content) == [("object_inventory", "oleObject")]
    assert records(content)[0].structured_value == EMBEDDED | {
        "olePreviewDeclared": True
    }


def test_a_frame_declaring_no_object_is_inventoried_as_none() -> None:
    """Nothing invents an object for a frame that declares none, and the frame
    still reads as whatever it is."""
    assert records(pptx(frame(graphic("")))) == []


def test_a_grouped_object_on_a_hidden_slide_keeps_the_frames_provenance() -> None:
    """The record sits where the frame does -- group path, per-level index,
    z-order and the slide's own visibility -- and never anywhere else."""
    content = pptx(
        frame(graphic(""), "2") + group("5", frame(graphic(OBJECT), "8")),
        hidden=True,
    )

    record = records(content)[0]
    assert record.visibility == "hidden"
    assert dict(record.source_locator) == EXPECTED | {
        "slideHidden": True,
        "shapeId": "8",
        "groupPath": ["5"],
        "zOrder": 0,
        "zPath": [1, 0],
    }


def test_malformed_object_bytes_are_measured_and_never_opened() -> None:
    """The member holds bytes no server would open. Only the length the package
    already measured is reported, so nothing parses, decodes or executes them."""
    members = dict(OBJECT_MEMBERS) | {
        "ppt/embeddings/oleObject1.bin": "\xd0\xcf not a compound file after all"
    }
    content = pptx(frame(graphic(OBJECT)), members=members)

    assert records(content)[0].structured_value == EMBEDDED | {
        "oleBytes": len("\xd0\xcf not a compound file after all".encode())
    }


@pytest.mark.parametrize(
    "markup",
    [
        # Deeper than the route: inside a child of the graphic data.
        graphic(f'<p:extLst xmlns:p="{PML}">{OBJECT}</p:extLst>'),
        # Directly below the frame, with no graphic route above it at all.
        OBJECT,
        # The graphic route itself written in a foreign vocabulary.
        graphic(OBJECT, namespace="urn:someone-elses-drawing"),
        # A local name shared with another vocabulary declares no object, and
        # the DrawingML spelling of it is as foreign as any other.
        graphic(obj(pml="urn:someone-elses", prefix="x")),
        graphic(obj(pml=DRAWING_NS, prefix="a")),
    ],
)
def test_a_lookalike_declares_no_object(markup: str) -> None:
    assert records(pptx(frame(markup))) == []


def test_two_declarations_leave_the_object_ambiguous() -> None:
    """A frame holds one graphic object, so nothing chooses between two."""
    with pytest.raises(OOXMLInvalid, match="more than one embedded object"):
        extract_pptx(pptx(frame(graphic(OBJECT * 2))), capture_id=CAPTURE)


def test_an_object_beside_another_graphic_is_refused() -> None:
    """One frame, one graphic: an object written beside a table is a frame
    nobody authored, and nothing here reads it as either."""
    table = (
        f'<a:tbl xmlns:a="{DRAWING_NS}"><a:tblGrid><a:gridCol/></a:tblGrid>'
        "<a:tr><a:tc/></a:tr></a:tbl>"
    )

    with pytest.raises(OOXMLInvalid, match="more than one graphic"):
        extract_pptx(pptx(frame(graphic(OBJECT + table))), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "body",
    [
        # Neither child: nothing says whether the object is embedded or linked,
        # and no relationship, target or ``progId`` is read as saying so.
        obj(mode=None),
        # Both, and two of one: the declaration contradicts itself.
        obj(children="<p:link/>"),
        obj(children="<p:embed/>"),
        obj(mode="link", children="<p:link/>"),
        # The same child written in the other namespace that qualifies it.
        obj(children=f'<s:embed xmlns:s="{STRICT_PML}"/>'),
    ],
)
def test_the_kind_of_object_has_to_be_declared_exactly_once(body: str) -> None:
    with pytest.raises(OOXMLInvalid, match="exactly one of embed and link"):
        extract_pptx(pptx(frame(graphic(body))), capture_id=CAPTURE)


def test_two_previews_leave_the_preview_ambiguous() -> None:
    with pytest.raises(OOXMLInvalid, match="more than one preview"):
        extract_pptx(pptx(frame(graphic(obj(previews=2)))), capture_id=CAPTURE)


def test_an_id_in_two_namespaces_names_two_relationships() -> None:
    """``r:id`` in the Transitional and the Strict namespace at once names two
    relationships, and nothing here picks one."""
    markup = obj(extra=f' xmlns:t="{STRICT_REL_NS}" t:id="rId3"')

    with pytest.raises(OOXMLInvalid, match="more than one relationship"):
        extract_pptx(pptx(frame(graphic(markup))), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "markup",
    [
        obj(relation_id=None),
        # An id in no namespace at all names no relationship: the attribute is
        # defined in the relationships namespace.
        obj(relation_id=None, extra=' id="rId2"'),
    ],
)
def test_an_object_naming_no_relationship_is_refused(markup: str) -> None:
    with pytest.raises(OOXMLInvalid, match="names no relationship"):
        extract_pptx(pptx(frame(graphic(markup))), capture_id=CAPTURE)


def test_a_relationship_the_part_does_not_hold_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="missing relationship"):
        extract_pptx(
            pptx(frame(graphic(obj(relation_id="rId9")))), capture_id=CAPTURE
        )


@pytest.mark.parametrize(
    "kind",
    [
        # Another type entirely reaches no object at all.
        IMAGE_TYPE,
        # A type that merely ends the same way is not one OOXML defines.
        "http://example.invalid/relationships/oleObject",
    ],
)
def test_a_relationship_of_another_type_names_no_object(kind: str) -> None:
    with pytest.raises(OOXMLInvalid, match="relationship of another type"):
        extract_pptx(
            pptx(frame(graphic(OBJECT)), slide_rels=relation(kind=kind)),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    ("body", "rels"),
    [
        # An embedded object is a part inside the package.
        (OBJECT, LINK_RELATION),
        # A linked one is a reference out of it.
        (LINKED, OBJECT_RELATION),
    ],
)
def test_the_declaration_and_the_relationship_have_to_agree(
    body: str, rels: str
) -> None:
    with pytest.raises(OOXMLInvalid, match="contradicts the relationship"):
        extract_pptx(pptx(frame(graphic(body)), slide_rels=rels), capture_id=CAPTURE)


@pytest.mark.parametrize("declared", ["yes", "True", "2", ""])
def test_a_malformed_flag_is_refused(declared: str) -> None:
    """``showAsIcon`` is a closed value, so one outside it is refused rather
    than read as whichever of the two it resembles."""
    with pytest.raises(OOXMLInvalid, match="object flag is malformed"):
        extract_pptx(
            pptx(frame(graphic(obj(extra=f' showAsIcon="{declared}"')))),
            capture_id=CAPTURE,
        )


def test_a_malformed_link_update_flag_is_refused() -> None:
    with pytest.raises(OOXMLInvalid, match="object flag is malformed"):
        extract_pptx(
            pptx(
                frame(graphic(obj(mode="link", mode_extra=' updateAutomatic="maybe"'))),
                slide_rels=LINK_RELATION,
            ),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    "declared",
    [
        # An extent is unsigned, so a negative width has no value of the type.
        'imgW="-1"',
        'imgW="12.5"',
        'imgH="wide"',
        'imgH=""',
        # Past the range the type is derived from.
        'imgW="2147483648"',
    ],
)
def test_a_malformed_image_extent_is_refused(declared: str) -> None:
    with pytest.raises(OOXMLInvalid, match="image extent is malformed"):
        extract_pptx(
            pptx(frame(graphic(obj(extra=f" {declared}")))), capture_id=CAPTURE
        )


def test_an_embedded_target_is_a_member_before_this_slice_sees_it() -> None:
    """Containment and existence are settled while the package is validated, so
    an object can never reach a part the archive does not hold."""
    with pytest.raises(OOXMLInvalid, match="escapes or misses the package"):
        extract_pptx(
            pptx(
                frame(graphic(OBJECT)),
                slide_rels=relation(target="../embeddings/absent.bin"),
            ),
            capture_id=CAPTURE,
        )
