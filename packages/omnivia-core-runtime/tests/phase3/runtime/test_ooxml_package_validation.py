"""The bounded, inert OOXML package profile.

Every case here is a synthetic package aimed at one rejection in the validator
that fronts the DOCX, XLSX and PPTX readers. The reader bodies are not the
subject: each test either proves a package is refused before any part is parsed,
or proves an ordinary package still reads.
"""

from __future__ import annotations

import builtins
import io
import os
import socket
import stat
import zipfile
from typing import Any

import pytest
from omnivia_core_runtime.service import ooxml_extractors
from omnivia_core_runtime.service.ooxml_extractors import OOXMLInvalid, extract_docx

CAPTURE = "dcap-" + "a" * 64
CONTENT_TYPES = (
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)
DOCUMENT = (
    b'<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Body</w:t>'
    b"</w:r></w:p></w:body></w:document>"
)
# Offsets into a central-directory entry: modification date, uncompressed size,
# and the starting disk number. Python exposes all three, so the validator can
# refuse what they say.
CENTRAL_FLAGS = 8
CENTRAL_DATE = 14
CENTRAL_SIZE = 24
CENTRAL_DISK = 34


def package(
    extra: dict[str, bytes] | None = None,
    *,
    document: bytes = DOCUMENT,
    compression: int = zipfile.ZIP_DEFLATED,
    entries: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
    comment: bytes = b"",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
        for info, content in entries:
            archive.writestr(info, content)
        archive.comment = comment
    return buffer.getvalue()


def entry(name: str, content: bytes = b"<x/>", **attributes: Any) -> bytes:
    info = zipfile.ZipInfo(name)
    for key, value in attributes.items():
        setattr(info, key, value)
    return package(entries=((info, content),))


def patch_central(content: bytes, offset: int, value: bytes) -> bytes:
    """Rewrite a field of the first central-directory entry."""
    index = content.index(b"PK\x01\x02")
    return content[: index + offset] + value + content[index + offset + len(value) :]


def rels(body: bytes, *, name: str = "word/_rels/document.xml.rels") -> bytes:
    return package({name: b"<Relationships>" + body + b"</Relationships>"})


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_valid_stored_and_deflated_packages_still_read(compression: int) -> None:
    result = extract_docx(package(compression=compression), capture_id=CAPTURE)

    assert [item.text for item in result.segments] == ["Body"]
    # The document is written in a foreign vocabulary, so it declares no
    # section of its own and reaches no header or footer.
    assert result.limitations == {
        "packageMembers": 2,
        "externalReferences": 0,
        "documentSections": 0,
        # The document declares no drawing and no legacy picture, so the
        # embedded-image pass had no candidate to examine either way.
        "embeddedImages": 0,
        "embeddedImageOutcomes": {},
    }


def test_package_absolute_relationship_target_resolves_inside_the_package() -> None:
    result = extract_docx(
        rels(b'<Relationship Id="rId1" Target="/word/document.xml"/>'),
        capture_id=CAPTURE,
    )

    assert [item.text for item in result.segments] == ["Body"]


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"../escape.xml": b"<x/>"}, id="traversal"),
        pytest.param({"word/../../escape.xml": b"<x/>"}, id="interior-traversal"),
        pytest.param({"word/./here.xml": b"<x/>"}, id="dot-component"),
        pytest.param({"word//here.xml": b"<x/>"}, id="empty-component"),
        pytest.param({"/absolute.xml": b"<x/>"}, id="absolute"),
        pytest.param({"C:/drive.xml": b"<x/>"}, id="drive-letter"),
        pytest.param({"\\\\host\\share\\unc.xml": b"<x/>"}, id="unc"),
        pytest.param({"word\\back.xml": b"<x/>"}, id="backslash"),
        pytest.param(
            {"word/nul\x00one.xml": b"<x/>", "word/nul\x00two.xml": b"<x/>"},
            # Python truncates a name at its first NUL, so two distinct
            # entries arrive as one name.
            id="nul-truncation-collision",
        ),
        pytest.param({"word/Document.xml": b"<x/>"}, id="case-fold-collision"),
        pytest.param({"word/document.xml": b"<x/>"}, id="duplicate"),
        pytest.param({"wo\u0308rd/here.xml": b"<x/>"}, id="not-nfc"),
        pytest.param(
            {"word/file.xml": b"<x/>", "word/\ufb01le.xml": b"<x/>"},
            id="compatibility-collision",
        ),
    ],
)
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_rejects_unsafe_or_ambiguous_member_names(extra: dict[str, bytes]) -> None:
    with pytest.raises(OOXMLInvalid, match="unsafe or ambiguous"):
        extract_docx(package(extra), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            patch_central(package(), CENTRAL_FLAGS, b"\x01\x00"), id="encrypted"
        ),
        pytest.param(
            patch_central(package(), CENTRAL_FLAGS, b"\x00\x20"), id="masked-header"
        ),
        pytest.param(
            patch_central(package(), CENTRAL_FLAGS, b"\x40\x00"),
            id="strong-encryption",
        ),
        pytest.param(
            entry("word/odd.xml", compress_type=zipfile.ZIP_BZIP2), id="bzip2"
        ),
        pytest.param(
            entry("word/link", external_attr=(stat.S_IFLNK | 0o777) << 16),
            id="symlink",
        ),
        pytest.param(
            entry("word/pipe", external_attr=(stat.S_IFIFO | 0o644) << 16),
            id="fifo",
        ),
        pytest.param(
            patch_central(package(), CENTRAL_DATE, b"\x00\x00"), id="invalid-date"
        ),
        pytest.param(
            patch_central(package(), CENTRAL_DISK, b"\x01\x00"), id="other-disk"
        ),
    ],
)
def test_rejects_entry_metadata_outside_the_profile(content: bytes) -> None:
    with pytest.raises(OOXMLInvalid, match="metadata"):
        extract_docx(content, capture_id=CAPTURE)


def test_rejects_a_stored_member_whose_sizes_disagree() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", DOCUMENT)
    content = patch_central(buffer.getvalue(), CENTRAL_SIZE, b"\x08\x00\x00\x00")

    with pytest.raises(OOXMLInvalid, match="metadata is outside the profile"):
        extract_docx(content, capture_id=CAPTURE)


def test_rejects_a_member_that_under_declares_its_length() -> None:
    """A directory that lies about a deflated member is refused, not believed."""
    content = patch_central(package(), CENTRAL_SIZE, b"\x08\x00\x00\x00")

    with pytest.raises(OOXMLInvalid, match="could not be validated"):
        extract_docx(content, capture_id=CAPTURE)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"%PDF-1.7\n" + package(), id="polyglot-prefix"),
        pytest.param(b"PK\x03\x04 not a package", id="truncated"),
        pytest.param(package(comment=b"smuggled"), id="archive-comment"),
    ],
)
def test_rejects_a_source_that_is_not_a_plain_zip_package(content: bytes) -> None:
    with pytest.raises(OOXMLInvalid, match="ZIP"):
        extract_docx(content, capture_id=CAPTURE)


def test_rejects_a_package_without_a_content_types_part() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", DOCUMENT)

    with pytest.raises(OOXMLInvalid, match="content-types part is missing"):
        extract_docx(buffer.getvalue(), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "embedded",
    [
        pytest.param(b"PK\x03\x04" + b"\x00" * 64, id="zip"),
        pytest.param(b"\x1f\x8b\x08\x00" + b"\x00" * 64, id="gzip"),
        pytest.param(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 64, id="7z"),
    ],
)
def test_rejects_a_member_that_is_itself_an_archive(embedded: bytes) -> None:
    with pytest.raises(OOXMLInvalid, match="itself an archive"):
        extract_docx(package({"word/media/archive.bin": embedded}), capture_id=CAPTURE)


def test_an_embedded_ooxml_package_is_bounded_but_left_inert() -> None:
    """A real embedded workbook is legal content, never a recursive input."""
    embedded = package(document=b"<not-workbook>never parsed</not-workbook>")

    result = extract_docx(
        package({"word/embeddings/chart-data.xlsx": embedded}), capture_id=CAPTURE
    )

    assert [item.text for item in result.segments] == ["Body"]


def test_rejects_a_compression_bomb_at_the_shipped_limits() -> None:
    bomb = b"\x00" * (ooxml_extractors.MAX_RATIO_FLOOR + 1)
    content = package({"word/media/bomb.bin": bomb})

    with pytest.raises(OOXMLInvalid, match="metadata is outside the profile"):
        extract_docx(content, capture_id=CAPTURE)


def test_rejects_a_compression_bomb_at_a_low_ratio_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml_extractors, "MAX_RATIO_FLOOR", 1024)
    monkeypatch.setattr(ooxml_extractors, "MAX_MEMBER_RATIO", 2)

    with pytest.raises(OOXMLInvalid, match="outside the profile|streamed bound"):
        extract_docx(
            package({"word/media/bomb.bin": b"\x00" * 4096}), capture_id=CAPTURE
        )


def test_rejects_too_many_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ooxml_extractors, "MAX_MEMBERS", 2)

    with pytest.raises(OOXMLInvalid, match="member count"):
        extract_docx(package({"word/extra.xml": b"<x/>"}), capture_id=CAPTURE)


def test_rejects_a_member_over_the_per_member_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml_extractors, "MAX_MEMBER_BYTES", 64)

    with pytest.raises(OOXMLInvalid, match="metadata is outside the profile"):
        extract_docx(package(), capture_id=CAPTURE)


def test_aggregate_bound_is_enforced_from_the_stream_not_the_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate bound is measured, and measuring it stays bounded.

    The member is incompressible, so nothing in the directory is over a limit
    on its own; only the bytes the decompressor yields cross the aggregate
    bound. The byte count proves the refusal lands mid-member rather than after
    the whole member has been expanded.
    """
    limit = 512 * 1024
    content = package(
        {"word/media/large.bin": os.urandom(4 * 1024 * 1024)},
        compression=zipfile.ZIP_STORED,
    )
    served: list[int] = []
    opener = zipfile.ZipFile.open

    def counting(self: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> Any:
        handle = opener(self, name, *args, **kwargs)
        reader = handle.read

        def tracked(size: int | None = None) -> bytes:
            chunk = reader(size)
            served.append(len(chunk))
            return chunk

        handle.read = tracked  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(zipfile.ZipFile, "open", counting)
    monkeypatch.setattr(ooxml_extractors, "MAX_EXPANDED_BYTES", limit)

    with pytest.raises(OOXMLInvalid, match="streamed bound"):
        extract_docx(content, capture_id=CAPTURE)
    assert sum(served) <= limit + ooxml_extractors._CHUNK


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            b'<!DOCTYPE w:document SYSTEM "http://host.invalid/x.dtd">', id="doctype"
        ),
        pytest.param(
            b'<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>', id="entity"
        ),
        pytest.param(b'<!NOTATION gif SYSTEM "gif">', id="notation"),
    ],
)
def test_rejects_dtd_and_entity_declarations_before_parsing(part: bytes) -> None:
    with pytest.raises(OOXMLInvalid, match="forbidden declaration"):
        extract_docx(package(document=part + DOCUMENT), capture_id=CAPTURE)


def test_rejects_a_dtd_in_a_relationship_part() -> None:
    with pytest.raises(OOXMLInvalid, match="forbidden declaration"):
        extract_docx(
            package({"word/_rels/document.xml.rels": b"<!DOCTYPE r><Relationships/>"}),
            capture_id=CAPTURE,
        )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'<Relationship Target="document.xml"/>', id="no-id"),
        pytest.param(b'<Relationship Id="rId1"/>', id="no-target"),
        pytest.param(b'<Relationship Id="rId1" Target=""/>', id="empty-target"),
        pytest.param(
            b'<Relationship Id="rId1" Target="x" TargetMode="Bogus"/>', id="bad-mode"
        ),
        pytest.param(
            b'<Relationship Id="rId1" Target="x" TargetMode="external"/>',
            id="mode-case",
        ),
        pytest.param(
            b'<Relationship Id="rId1" Target="document.xml"/>'
            b'<Relationship Id="rId1" Target="document.xml"/>',
            id="duplicate-id",
        ),
    ],
)
def test_rejects_malformed_relationships(body: bytes) -> None:
    with pytest.raises(OOXMLInvalid, match="malformed"):
        extract_docx(rels(body), capture_id=CAPTURE)


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(b"../../outside.xml", id="traversal"),
        pytest.param(b"/etc/passwd", id="package-absolute-escape"),
        pytest.param(b"missing.xml", id="missing-part"),
    ],
)
def test_rejects_an_internal_target_outside_the_package(target: bytes) -> None:
    with pytest.raises(OOXMLInvalid, match="escapes or misses"):
        extract_docx(
            rels(b'<Relationship Id="rId1" Target="' + target + b'"/>'),
            capture_id=CAPTURE,
        )


def test_rejects_a_relationship_part_outside_a_rels_folder() -> None:
    with pytest.raises(OOXMLInvalid, match="outside a _rels folder"):
        extract_docx(
            package({"word/document.xml.rels": b"<Relationships/>"}),
            capture_id=CAPTURE,
        )


def test_external_relationships_are_inventoried_without_any_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External targets are counted and left alone.

    No file is opened and no socket is created for the whole extraction, so the
    inventory cannot be coming from a fetch, a stat or a DNS lookup.
    """
    content = rels(
        b'<Relationship Id="rId1" Target="https://host.invalid/remote.xml"'
        b' TargetMode="External"/>'
        b'<Relationship Id="rId2" Target="file:///etc/passwd" TargetMode="External"/>'
    )

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the extractor resolved an external target")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    result = extract_docx(content, capture_id=CAPTURE)

    assert result.limitations["externalReferences"] == 2
    assert [item.text for item in result.segments] == ["Body"]
