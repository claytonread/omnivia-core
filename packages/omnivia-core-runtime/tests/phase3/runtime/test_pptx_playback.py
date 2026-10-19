"""PPTX slide playback: what one slide declares about the way it plays.

The transition between slides and the animation tree a slide runs once it is up
are the slide element's own children, in the PresentationML namespace the slide
itself is written in. That is the one place either is read from: a lookalike in
another vocabulary, or one nested anywhere below the slide, declares nothing
about how this slide plays.

Both are inventoried and only inventoried. The transition's effect is kept by
name with its own attributes carried verbatim, never interpreted; the timing
tree is counted by element name and nothing else about it is looked at, so no
trigger is read, no target resolved and no command inspected. The sound a
transition plays is another slice's subject: it is never read, counted or
resolved, and nothing here opens the media part it names.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_pptx

CAPTURE = "dcap-" + "a" * 64
# The two PresentationML main namespaces a slide may be written in, and so the
# two a transition or timing element has to be written in to be the slide's.
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
STRICT_NS = "http://purl.oclc.org/ooxml/presentationml/main"
# A vocabulary that is neither, for the lookalikes that qualify as nothing.
OTHER_NS = "urn:x-other-vocabulary"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
AUDIO_TYPE = REL_NS + "/audio"
# A media part holding bytes that are not audio and are not XML either. The
# proof that nothing here opens the part is that extraction succeeds with these
# in it: anything that read them would refuse them.
AUDIO_BYTES = "not audio anything here decodes"
AUDIO_MEMBER = {"ppt/media/audio1.wav": AUDIO_BYTES}
AUDIO_RELATION = (
    f'<Relationship Id="rId9" Type="{AUDIO_TYPE}" Target="../media/audio1.wav"/>'
)
# The sound a transition plays, naming the media part through the slide's own
# relationship. Never read, so the record below says nothing about it.
SOUND = (
    '<p:sndAc><p:stSnd><p:snd r:embed="rId9" name="applause.wav"/></p:stSnd>'
    "</p:sndAc>"
)
# One direct transition: a speed, both advance claims, and one effect carrying
# an attribute of its own.
TRANSITION = (
    '<p:transition spd="slow" advClick="1" advTm="5000"><p:fade thruBlk="1"/>'
    "</p:transition>"
)
# One timing tree, five nodes deep in a single chain, holding the ``p:cmd`` a
# player would act on and nothing here even reads.
TIMING = (
    '<p:timing><p:tnLst><p:par><p:cTn id="1" dur="indefinite"><p:childTnLst>'
    '<p:cmd type="call" cmd="playFrom(0.0)"/>'
    "</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>"
)


def slide_xml(
    playback: str = "", *, namespace: str = PRESENTATION_NS, nested: str = ""
) -> str:
    """One slide part, written in the given PresentationML namespace.

    ``playback`` is written as the slide element's own children, after
    ``p:cSld``, which is where the schema puts a transition and a timing tree.
    ``nested`` is written inside the shape tree and inside ``p:cSld`` instead,
    neither of which is a place either declaration belongs.
    """
    shape = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr><p:spPr/><p:txBody>'
        "<a:p><a:r><a:t>slide text</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    return (
        f'<p:sld xmlns:p="{namespace}" xmlns:a="urn:a" xmlns:r="{REL_NS}">'
        f"<p:cSld><p:spTree>{shape}{nested}</p:spTree>{nested}</p:cSld>"
        f"{playback}</p:sld>"
    )


def pptx(
    playback: str = "",
    *,
    hidden: bool = False,
    namespace: str = PRESENTATION_NS,
    nested: str = "",
    slide_rels: str | None = None,
    members: dict[str, str] | None = None,
) -> bytes:
    """Build a one-slide presentation whose slide declares the given playback."""
    shown = ' show="0"' if hidden else ""
    parts = {
        "[Content_Types].xml": "<Types/>",
        "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r">'
        f'<p:sldIdLst><p:sldId id="256" r:id="rId1"{shown}/>'
        "</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": "<Relationships>"
        '<Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": slide_xml(
            playback, namespace=namespace, nested=nested
        ),
    }
    if slide_rels is not None:
        parts["ppt/slides/_rels/slide1.xml.rels"] = (
            f"<Relationships>{slide_rels}</Relationships>"
        )
    parts.update(members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def records(content: bytes) -> list[ExtractedSegment]:
    """Every playback segment the extraction produced, in emitted order."""
    return [
        item
        for item in extract_pptx(content, capture_id=CAPTURE).segments
        if (item.structured_value or {}).get("kind") == "slide_playback"
    ]


def chain(depth: int) -> str:
    """A timing tree that is one chain of ``depth`` nodes below ``p:timing``."""
    inner = ""
    for _ in range(depth):
        inner = f"<p:cTn>{inner}</p:cTn>"
    return f"<p:timing>{inner}</p:timing>"


def spread(count: int, *, distinct: bool = False) -> str:
    """``count`` sibling nodes below ``p:timing``, of one kind or of ``count``."""
    nodes = "".join(f"<p:cTn{index if distinct else ''}/>" for index in range(count))
    return f"<p:timing>{nodes}</p:timing>"


def effect(count: int = 1, *, value: str = "1") -> str:
    """One transition whose effect carries ``count`` attributes of its own."""
    attributes = "".join(f' a{index}="{value}"' for index in range(count))
    return f"<p:transition><p:fade{attributes}/></p:transition>"


# The provenance one playback record carries: the slide it belongs to, and
# nothing about any shape, because the declaration is the slide's own.
EXPECTED: dict[str, object] = {
    "captureId": CAPTURE,
    "part": "ppt/slides/slide1.xml",
    "slideId": "256",
    "slide": 1,
    "slideHidden": False,
}
# What the default fixture declares, read back. Every other record below
# differs from it only in the fields the fixture changed.
DECLARED: dict[str, object] = {
    "kind": "slide_playback",
    "transitionDeclared": True,
    "transitionSpeed": "slow",
    "transitionAdvanceOnClick": True,
    "transitionAdvanceAfter": 5000,
    "transitionEffect": "fade",
    "transitionEffectAttributes": {"thruBlk": "1"},
    "transitionExtensionDeclared": False,
    "timingDeclared": True,
    "timingNodes": 5,
    "timingNodeKinds": {
        "cTn": 1,
        "childTnLst": 1,
        "cmd": 1,
        "par": 1,
        "tnLst": 1,
    },
    "timingDepth": 5,
    "timingExtensionDeclared": False,
}
# The same keys, each at the value that says the slide declared no such thing.
# A record for a slide declaring only one of the two carries this for the other.
UNDECLARED: dict[str, object] = {
    key: False if key.endswith("Declared") else None for key in DECLARED
} | {"kind": "slide_playback"}


def test_a_slide_declaring_neither_inventories_no_playback() -> None:
    """A slide that never says how it plays has nothing to inventory, which is
    no fault: the rest of its text is reported as it always was."""
    content = pptx()

    assert records(content) == []
    assert [
        item.text for item in extract_pptx(content, capture_id=CAPTURE).segments
    ] == ["slide text"]


def test_a_transition_and_timing_are_inventoried_at_the_slide() -> None:
    """One record for the slide, carrying what each declaration says and the
    slide identity it was declared at."""
    content = pptx(TRANSITION + TIMING)

    (record,) = records(content)
    assert record.structured_value == DECLARED
    assert dict(record.source_locator) == EXPECTED
    assert (record.semantic_role, record.extraction_method) == (
        "object_inventory",
        "inventory",
    )
    assert (record.visibility, record.text) == ("visible", None)


def test_a_hidden_slide_marks_its_playback_record_hidden() -> None:
    """A transition declared on a slide nobody is shown is still declared, and
    is reported as the hidden slide's."""
    (hidden,) = records(pptx(TRANSITION + TIMING, hidden=True))

    assert hidden.visibility == "hidden"
    assert dict(hidden.source_locator) == EXPECTED | {"slideHidden": True}
    assert hidden.structured_value == DECLARED


def test_a_strict_package_declares_its_playback_the_same_way() -> None:
    """The Strict PresentationML namespace qualifies a slide's own transition
    and timing exactly as the Transitional one does."""
    (record,) = records(pptx(TRANSITION + TIMING, namespace=STRICT_NS))

    assert record.structured_value == DECLARED
    assert dict(record.source_locator) == EXPECTED


def test_a_nested_or_foreign_declaration_is_not_the_slides_own() -> None:
    """Only the slide element's own child in its own namespace declares how the
    slide plays. A lookalike from another vocabulary, or a real declaration
    nested below the slide, declares nothing -- so there is no record at all."""
    foreign = (
        f'<q:transition xmlns:q="{OTHER_NS}" spd="fast"><q:fade/></q:transition>'
        f'<q:timing xmlns:q="{OTHER_NS}"><q:tnLst/></q:timing>'
    )

    assert records(pptx(foreign)) == []
    assert records(pptx(nested=TRANSITION + TIMING)) == []
    # Neither route becomes the slide's own by being taken at the same time.
    assert records(pptx(foreign, nested=TRANSITION + TIMING)) == []


def test_a_command_and_a_transition_sound_stay_inert() -> None:
    """A timing node's attributes are never read, so the command one names is
    neither executed nor reported; and the sound a transition plays is never
    resolved, so the media part it relates to is never opened."""
    content = pptx(
        f'<p:transition spd="fast">{SOUND}<p:fade/></p:transition>' + TIMING,
        slide_rels=AUDIO_RELATION,
        members=AUDIO_MEMBER,
    )

    (record,) = records(content)
    # The sound is not the effect, and is not counted as a second one.
    assert record.structured_value == DECLARED | {
        "transitionSpeed": "fast",
        "transitionAdvanceOnClick": None,
        "transitionAdvanceAfter": None,
        "transitionEffectAttributes": {},
    }
    # Nothing the command, the sound or its relationship wrote rides anywhere
    # in the segment, and the undecodable media bytes were never looked at.
    written = str(record.structured_value) + str(dict(record.source_locator))
    for leaked in ("playFrom", "call", "rId9", "applause", "audio1", "sndAc"):
        assert leaked not in written


@pytest.mark.parametrize(
    ("playback", "message"),
    [
        (TRANSITION * 2, "declares its transition more than once"),
        (TIMING * 2, "declares its timing more than once"),
        # Nor does the other declaration standing between the two settle it.
        (TRANSITION + TIMING + TRANSITION, "transition more than once"),
        (TIMING + TRANSITION + TIMING, "timing more than once"),
    ],
    ids=["two-transitions", "two-timings", "transitions-apart", "timings-apart"],
)
def test_a_slide_declaring_either_twice_is_refused(playback: str, message: str) -> None:
    """A slide declares how it plays once. Two transitions or two timing trees
    leave which one the slide means ambiguous, so neither is read."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_pptx(pptx(playback), capture_id=CAPTURE)


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        # ``med`` is the enumerated middle speed; the English word is not.
        ('spd="medium"', "transition speed is malformed"),
        ('spd=""', "transition speed is malformed"),
        ('advClick="yes"', "transition click flag is malformed"),
        ('advTm="-1"', "transition advance time is malformed"),
        # Ten digits, so it reads as an index, and one past the largest.
        ('advTm="4294967296"', "transition advance time is malformed"),
        ('advAfter="5000"', "transition declares an unknown attribute"),
        # The schema's names are the schema's: a near miss is not one of them.
        ('advtm="5000"', "transition declares an unknown attribute"),
    ],
)
def test_a_malformed_or_unknown_transition_attribute_is_refused(
    attributes: str, message: str
) -> None:
    """The transition's own attributes are a closed set read as what they
    claim, so a value outside its enumeration and a name outside the set are
    both refused rather than carried through to whatever reads the record."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_pptx(pptx(f"<p:transition {attributes}/>"), capture_id=CAPTURE)


def test_a_transition_draws_one_effect_or_none() -> None:
    """Two effects leave which one the slide draws ambiguous. What is not an
    effect never becomes the second one: not the sound, not the extension
    point, and not a child from another vocabulary entirely."""
    with pytest.raises(OOXMLInvalid, match="declares more than one effect"):
        extract_pptx(
            pptx("<p:transition><p:fade/><p:wipe/></p:transition>"),
            capture_id=CAPTURE,
        )

    (beside,) = records(
        pptx(
            "<p:transition><p:sndAc/><p:extLst/><p:fade/>"
            f'<q:wipe xmlns:q="{OTHER_NS}"/></p:transition>'
        )
    )
    assert beside.structured_value == UNDECLARED | {
        "transitionDeclared": True,
        "transitionEffect": "fade",
        "transitionEffectAttributes": {},
        "transitionExtensionDeclared": True,
    }


def test_an_effects_attributes_are_bounded_in_number_and_in_size() -> None:
    """Each effect kind defines its own attributes, so they are inventoried by
    name and value rather than interpreted -- and only their number and their
    size are bounded, which is all the inventorying asks about them."""
    (most,) = records(pptx(effect(32)))
    assert most.structured_value == UNDECLARED | {
        "transitionDeclared": True,
        "transitionEffect": "fade",
        "transitionEffectAttributes": {f"a{index}": "1" for index in range(32)},
    }

    with pytest.raises(OOXMLInvalid, match="effect declares too many attributes"):
        extract_pptx(pptx(effect(33)), capture_id=CAPTURE)

    bound = ooxml_extractors._MAX_META_CHARS
    (clipped,) = records(pptx(effect(value="y" * (bound + 1))))
    assert clipped.structured_value == UNDECLARED | {
        "transitionDeclared": True,
        "transitionEffect": "fade",
        "transitionEffectAttributes": {"a0": "y" * bound},
    }


@pytest.mark.parametrize(
    ("playback", "nodes", "depth", "kinds"),
    [
        (chain(64), 64, 64, {"cTn": 64}),
        (spread(20_000), 20_000, 1, {"cTn": 20_000}),
        (
            spread(128, distinct=True),
            128,
            1,
            {f"cTn{index}": 1 for index in range(128)},
        ),
    ],
    ids=["deepest", "most-nodes", "most-kinds"],
)
def test_a_timing_tree_at_each_bound_is_still_counted(
    playback: str, nodes: int, depth: int, kinds: dict[str, int]
) -> None:
    """Counting is the only work the tree asks for, and a tree sitting exactly
    at a bound is counted rather than refused."""
    (record,) = records(pptx(playback))

    assert record.structured_value == UNDECLARED | {
        "timingDeclared": True,
        "timingNodes": nodes,
        "timingNodeKinds": kinds,
        "timingDepth": depth,
    }


@pytest.mark.parametrize(
    ("playback", "message"),
    [
        (chain(65), "timing is nested too deeply"),
        (spread(20_001), "timing declares too many nodes"),
        (spread(129, distinct=True), "timing declares too many node kinds"),
    ],
    ids=["too-deep", "too-many-nodes", "too-many-kinds"],
)
def test_a_timing_tree_past_any_bound_is_refused(playback: str, message: str) -> None:
    """One node past the count, one level past the depth, or one kind past the
    distinct names: the tree is refused rather than walked."""
    with pytest.raises(OOXMLInvalid, match=message):
        extract_pptx(pptx(playback), capture_id=CAPTURE)


def test_a_transition_extension_is_recorded_and_not_read() -> None:
    """A namespace-qualified attribute and the extension point itself are both
    another vocabulary's business. Each is recorded as declared and nothing in
    it is read -- not even where it borrows a name the schema does define."""
    (qualified,) = records(
        pptx(f'<p:transition xmlns:q="{OTHER_NS}" q:spd="instant"/>')
    )
    assert qualified.structured_value == UNDECLARED | {
        "transitionDeclared": True,
        "transitionExtensionDeclared": True,
    }

    (listed,) = records(
        pptx(
            '<p:transition><p:extLst><p:ext uri="urn:x-ext"/></p:extLst></p:transition>'
        )
    )
    assert listed.structured_value == UNDECLARED | {
        "transitionDeclared": True,
        "transitionExtensionDeclared": True,
    }
    for leaked in ("instant", "urn:x-ext", "uri"):
        assert leaked not in str(qualified.structured_value) + str(
            listed.structured_value
        )

    # The transition's own extension point, not one carried inside its effect.
    (nested,) = records(
        pptx("<p:transition><p:fade><p:extLst/></p:fade></p:transition>")
    )
    assert nested.structured_value == UNDECLARED | {
        "transitionDeclared": True,
        "transitionEffect": "fade",
        "transitionEffectAttributes": {},
    }


def test_a_timing_extension_is_recorded_and_not_read() -> None:
    """The timing tree's own extension point is recorded as declared. Its
    contents are counted as the nodes they are and read no further."""
    (record,) = records(
        pptx('<p:timing><p:extLst><p:ext uri="urn:x-ext"/></p:extLst></p:timing>')
    )

    assert record.structured_value == UNDECLARED | {
        "timingDeclared": True,
        "timingNodes": 2,
        "timingNodeKinds": {"ext": 1, "extLst": 1},
        "timingDepth": 2,
        "timingExtensionDeclared": True,
    }
    assert "urn:x-ext" not in str(record.structured_value)

    # One nested below a node is that node's extension point, not the tree's.
    (deeper,) = records(pptx("<p:timing><p:tnLst><p:extLst/></p:tnLst></p:timing>"))
    assert deeper.structured_value == UNDECLARED | {
        "timingDeclared": True,
        "timingNodes": 2,
        "timingNodeKinds": {"extLst": 1, "tnLst": 1},
        "timingDepth": 2,
    }
