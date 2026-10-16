"""The process boundary one document extraction runs behind (DOC-16).

Four things are asserted here, and the containment and equivalence claims are
asserted against real child processes rather than against patched calls, because
an in-process assertion that nothing reached out is exactly the weaker claim
DOC-16 refuses:

* **Containment.** A process sealed by this module's own adapter cannot open a
  socket and cannot read a file outside the admitted input, the packaged code,
  the pinned model directory and its private scratch -- while it still reads
  everything inside that scope. The same process tries the same things *before*
  sealing itself, so ``denied`` is read as the seal and not as an accident of
  the host.
* **Failing closed.** A host where the seal cannot be established does not
  extract. There is no unconfined production route: the worker refuses, and the
  refusal reaches the production call sites as a terminal worker failure rather
  than as a verdict on the document.
* **Equivalence.** Where the seal does hold, the admitted bytes still extract,
  and the outcome a confined child returns is the one the in-process adapters
  produce, field for field.
* **Truthfulness.** A crash, a timeout, a resource-limit kill, an oversize or
  malformed reply and a refused seal are each their own typed failure. None of
  them can arrive as a successful extraction, and none is reported as a verdict
  on the source.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_processing, document_worker
from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)
from omnivia_core_runtime.service.document_worker import (
    CONFINEMENT_NONE,
    PROTOCOL_VERSION,
    REASON_CRASHED,
    REASON_INTERNAL,
    REASON_ISOLATION,
    REASON_PROTOCOL,
    REASON_RESOURCE_LIMIT,
    REASON_SOURCE_OVERSIZE,
    REASON_TIMEOUT,
    REQUIRED_CONFINEMENT,
    DocumentWorkerFailed,
    IsolationUnavailable,
    extract_document_isolated,
)
from omnivia_core_runtime.service.handlers import document_capture
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_AND_LOCAL_OCR,
    ExtractionSettings,
    OcrResources,
    text_only_settings,
)

CAPTURE: Final = "dcap-" + "w" * 64
#: Sources whose adapters are available wherever this suite runs, so the
#: equivalence assertion below needs no external binary.
PORTABLE_SOURCES: Final = (
    ("text/plain", b"the admitted bytes still extract"),
    ("text/markdown", b"# heading\n\nbody\n"),
    ("text/csv", b"name,value\nalpha,1\nbeta,2\n"),
    (
        "text/html",
        (
            b"<html><body><h1>Title</h1><p>Inert <b>markup</b></p>"
            b'<img src="https://example.invalid/never-fetched.png">'
            b"<table><tr><th>h</th><td>c</td></tr></table></body></html>"
        ),
    ),
)

#: What a sealed process is asked to try. It is a separate script rather than a
#: fixture inside the worker, so the thing under test is the real adapter and
#: the attempts are made by code the adapter has already sealed.
#:
#: Every attempt is made twice: once before :func:`confine_process` and once
#: after. The "before" half is the control, and it is taken in this same process
#: on this same host, so the assertions below never have to assume what an
#: unconfined connection or read would have done here.
PROBE: Final = '''
import json, os, socket, sys
from pathlib import Path

from omnivia_core_runtime.service.document_worker import _import_roots, confine_process

scratch, work, outside, model = (Path(argument) for argument in sys.argv[1:5])
# The topmost named ancestor of the packaged code, so the traversal metadata
# grant names it and no subpath grant covers it. Its *contents* are still
# nobody's business, which is what separates the narrow grant from the blanket
# one it replaced.
ancestor = _import_roots()[0].parents[-2]

def attempt(action):
    try:
        action()
    except PermissionError:
        return "denied"
    except OSError as error:
        return f"other:{type(error).__name__}"
    return "allowed"

def connect():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.settimeout(2)
        stream.connect(("127.0.0.1", 9))

def datagram():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
        stream.sendto(b"x", ("127.0.0.1", 9))

ATTEMPTS = {
    "tcp": connect,
    "udp": datagram,
    "outside_read": lambda: (outside / "secret.txt").read_bytes(),
    "etc_read": lambda: Path("/etc/hosts").read_bytes(),
    # Metadata is its own grant in SBPL, and an unrestricted one lets a
    # document map the host it cannot open. These are the probes for it.
    "outside_stat": lambda: os.stat(outside / "secret.txt"),
    "etc_stat": lambda: os.stat("/etc/hosts"),
    "etc_listdir": lambda: os.listdir("/etc"),
    "outside_listdir": lambda: os.listdir(outside),
    "ancestor_stat": lambda: os.stat(ancestor),
    "ancestor_listdir": lambda: os.listdir(ancestor),
    "scratch_stat": lambda: os.stat(scratch / "source.bin"),
    "outside_write": lambda: (outside / "written.txt").write_bytes(b"x"),
    "scratch_root_write": lambda: (scratch / "forged.txt").write_bytes(b"x"),
    "admitted_read": lambda: (scratch / "source.bin").read_bytes(),
    "model_read": lambda: (model / "eng.traineddata").read_bytes(),
    "work_write": lambda: (work / "spill.txt").write_bytes(b"x"),
    "package_import": lambda: __import__(
        "omnivia_core_runtime.service.ooxml_extractors"
    ),
}

before = {name: attempt(action) for name, action in ATTEMPTS.items()}
confinement = confine_process(scratch_root=scratch, work_dir=work, model_dirs=[model])
after = {name: attempt(action) for name, action in ATTEMPTS.items()}

print(json.dumps({
    "confinement": confinement,
    "ancestor": str(ancestor),
    "before": before,
    "after": after,
    "inherited_secret": os.environ.get("OMNIVIA_WORKER_PROBE_SECRET", "absent"),
}))
'''

#: The isolated route only extracts where a confinement adapter exists, so
#: everything that drives a real child end to end is scoped to that platform.
#: The complement -- that a platform without one refuses rather than extracting
#: unconfined -- is asserted in its own test below.
needs_confinement = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="only macOS ships a confinement adapter in this build",
)
needs_no_confinement = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="this host has a confinement adapter, so nothing here fails closed",
)


def _attempt(tmp_path: Path, content: bytes = b"source") -> dict[str, Any]:
    """One attempt directory laid out the way the parent lays one out."""
    scratch = tmp_path / "scratch"
    work = scratch / "work"
    work.mkdir(parents=True)
    (scratch / "source.bin").write_bytes(content)
    return {"scratch": scratch, "work": work, "source": scratch / "source.bin"}


class _RefusingShutil:
    """A ``shutil`` stand-in whose ``rmtree`` records the tree and refuses it.

    Bound over the module's own name rather than over ``shutil.rmtree``, so the
    real one stays available to the test that has to clear up afterwards.
    """

    def __init__(self) -> None:
        self.refused: list[Path] = []

    def rmtree(self, scratch: Path, *_: Any, **__: Any) -> None:
        self.refused.append(Path(scratch))
        raise OSError("the tree will not go")


def _refuse_removal(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    refusing = _RefusingShutil()
    monkeypatch.setattr(document_worker, "shutil", refusing)
    return refusing.refused


def _header(attempt: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The request header the parent would build for one attempt directory."""
    content = Path(attempt["source"]).read_bytes()
    header = {
        "protocol": PROTOCOL_VERSION,
        "mediaType": "text/plain",
        "captureId": CAPTURE,
        "settings": None,
        "scratchRoot": str(attempt["scratch"]),
        "workDir": str(attempt["work"]),
        "sourcePath": str(attempt["source"]),
        "sourceLength": len(content),
        "sourceDigest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "replyPath": str(attempt["work"] / "reply.json"),
        "wallSeconds": 30,
    }
    header.update(overrides)
    return header


# -- the production route ----------------------------------------------------


def test_the_production_extraction_route_is_the_isolated_worker() -> None:
    # Both production call sites -- the capture outbox and the reprocess
    # handler -- resolve their extraction seam to the confined child, so the
    # in-process adapters are reachable only from the adapter unit tests.
    assert document_processing.extract_document is extract_document_isolated
    assert document_capture.extract_document is extract_document_isolated


def test_the_child_environment_carries_nothing_the_service_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIVIA_WORKER_PROBE_SECRET", "must-not-travel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-travel")
    environment = document_worker._child_environment(tmp_path)
    assert set(environment) == {
        "PATH",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "HOME",
        "TMPDIR",
    }
    # The home and temporary directories point inside the private scratch, so a
    # library that insists on one cannot reach the real home through it.
    assert environment["HOME"] == str(tmp_path)
    assert environment["TMPDIR"] == str(tmp_path)


def test_the_child_search_path_is_a_constant_and_not_the_services_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The RTF reader is invoked as `pandoc` and the OCR engine as `tesseract`,
    # both resolved through PATH. Inheriting the service's PATH would let
    # whoever can set one variable on the service decide which binary either of
    # those actually is, which is exactly the arbitrary configuration path 11.5
    # says the worker does not receive.
    planted = tmp_path / "planted"
    planted.mkdir()
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}/usr/bin")
    path = document_worker._child_environment(tmp_path)["PATH"]
    assert str(planted) not in path
    assert path == os.pathsep.join(document_worker._TRUSTED_PATH_DIRS)
    # Every entry is absolute, so nothing is resolved relative to a working
    # directory a parser just wrote into.
    assert all(entry.startswith("/") for entry in path.split(os.pathsep))


def test_the_child_command_is_a_fixed_argv_with_no_shell() -> None:
    argv = document_worker._worker_argv()
    assert argv[0] == sys.executable
    # `-P` is what keeps the scratch off the import path, so nothing a parser
    # drops there can be imported afterwards.
    assert argv[1:] == [
        "-P",
        "-s",
        "-B",
        "-m",
        "omnivia_core_runtime.service.document_worker",
    ]


# -- containment, in a real sealed process -----------------------------------


@needs_confinement
def test_a_sealed_process_reaches_no_network_and_nothing_outside_its_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(tmp_path, b"admitted")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not for the worker")
    model = tmp_path / "models"
    model.mkdir()
    (model / "eng.traineddata").write_bytes(b"pinned model")
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    monkeypatch.setenv("OMNIVIA_WORKER_PROBE_SECRET", "must-not-travel")

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-s",
            "-B",
            str(probe),
            str(attempt["scratch"]),
            str(attempt["work"]),
            str(outside),
            str(model),
        ],
        env=document_worker._child_environment(attempt["work"]),
        cwd=attempt["work"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    report = json.loads(completed.stdout)
    before, after = report["before"], report["after"]

    # A seal this host refuses is not something to work around: it would mean
    # the production route refuses every document here, which is the whole
    # point of the next test, and this one has proved nothing.
    assert report["confinement"] == document_worker.CONFINEMENT_SEATBELT

    # Everything the seal has to deny was reachable, or reachably refused, in
    # this very process a moment earlier. That is what makes `denied` below the
    # sandbox: a closed local port answers an unconfined connection with
    # ECONNREFUSED, never with EPERM, and these files were plainly readable.
    assert before["tcp"] == "other:ConnectionRefusedError", before
    assert before["outside_read"] == "allowed", before
    assert before["etc_read"] == "allowed", before
    assert before["outside_stat"] == "allowed", before
    assert before["etc_stat"] == "allowed", before
    assert before["etc_listdir"] == "allowed", before
    assert before["outside_listdir"] == "allowed", before
    assert before["ancestor_listdir"] == "allowed", before
    assert before["outside_write"] == "allowed", before
    assert before["scratch_root_write"] == "allowed", before

    # Network: denied outright, for a stream and for a datagram alike.
    assert after["tcp"] == "denied"
    assert after["udp"] == "denied"
    # Filesystem: nothing outside the admitted input, the packaged code, the
    # pinned models and the private scratch -- reads or writes.
    assert after["outside_read"] == "denied"
    assert after["etc_read"] == "denied"
    assert after["outside_write"] == "denied"
    # Metadata too. A sealed process that could stat the whole filesystem would
    # still be able to map the host it cannot read, so the blanket
    # `file-read-metadata` grant is gone and only the ancestors of the paths
    # already in scope remain reachable.
    assert after["outside_stat"] == "denied"
    assert after["etc_stat"] == "denied"
    assert after["etc_listdir"] == "denied"
    assert after["outside_listdir"] == "denied"
    # The exception that remains is exactly what it says it is: an ancestor of
    # the packaged code is stattable, because macOS cannot resolve a path
    # without reading what stands along it, and its contents are not.
    assert after["ancestor_stat"] == "allowed", report["ancestor"]
    assert after["ancestor_listdir"] == "denied", report["ancestor"]
    # And the admitted source is still stattable, which is what the readers do
    # before they open it.
    assert after["scratch_stat"] == "allowed"
    # The scratch root holds the admitted source read-only; only the work
    # directory inside it is writable.
    assert after["scratch_root_write"] == "denied"
    # And everything inside the scope still works, so this is a boundary and
    # not a broken process.
    assert after["admitted_read"] == "allowed"
    assert after["model_read"] == "allowed"
    assert after["work_write"] == "allowed"
    assert after["package_import"] == "allowed"
    assert report["inherited_secret"] == "absent"


def test_the_profile_denies_the_network_and_scopes_every_grant(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path)
    model = tmp_path / "models"
    model.mkdir()
    profile = document_worker._seatbelt_profile(
        scratch_root=attempt["scratch"], work_dir=attempt["work"], model_dirs=[model]
    )
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert f'(subpath "{os.path.realpath(model)}")' in profile
    assert f'(subpath "{os.path.realpath(attempt["scratch"])}")' in profile
    # The one write grant is the work directory. The scratch root, which holds
    # the admitted source, is readable and never writable.
    write_rule = next(
        line for line in profile.splitlines() if line.startswith("(allow file-write*")
    )
    assert f'(subpath "{os.path.realpath(attempt["work"])}")' in write_rule
    assert os.path.realpath(attempt["scratch"]) not in write_rule.replace(
        os.path.realpath(attempt["work"]), ""
    )


def test_the_metadata_grant_is_the_ancestors_in_scope_and_not_the_filesystem(
    tmp_path: Path,
) -> None:
    # The unrestricted `(allow file-read-metadata)` this profile used to carry
    # let a sealed document stat every path on the host. What is left is the
    # named ancestors of the paths already in scope, which is what macOS needs
    # to resolve the `/var` symlink every scratch directory sits behind.
    attempt = _attempt(tmp_path)
    profile = document_worker._seatbelt_profile(
        scratch_root=attempt["scratch"], work_dir=attempt["work"], model_dirs=()
    )
    assert "(allow file-read-metadata)" not in profile
    rule = next(
        line
        for line in profile.splitlines()
        if line.startswith("(allow file-read-metadata")
    )
    # Literals only: a subpath here would hand back the whole subtree.
    assert "(subpath" not in rule
    assert '(literal "/var")' in rule or '(literal "/private")' in rule
    # And every literal really is an ancestor of something in scope, rather
    # than an unrelated directory that happened to be listed.
    in_scope = (
        *document_worker._import_roots(),
        attempt["scratch"],
        attempt["work"],
    )
    for literal in rule.split('(literal "')[1:]:
        ancestor = literal.split('")')[0]
        assert any(
            os.path.realpath(path).startswith(ancestor)
            or os.path.abspath(path).startswith(ancestor)
            for path in in_scope
        ), ancestor


def test_a_path_that_cannot_be_written_into_a_profile_refuses_the_seal() -> None:
    # A control character cannot be written into an SBPL string at all, so the
    # seal is refused rather than built from a profile that means something
    # else than it reads.
    with pytest.raises(IsolationUnavailable):
        document_worker._sbpl_path("/tmp/a\nb")


# -- failing closed ----------------------------------------------------------


def test_a_platform_with_no_adapter_refuses_instead_of_running_unconfined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # There is one confinement this build establishes. Asked to seal a process
    # anywhere else, the adapter refuses: it never hands back an unconfined
    # process for a caller whose next act is to read a hostile document.
    monkeypatch.setattr(sys, "platform", "some-other-platform")
    with pytest.raises(IsolationUnavailable) as refusal:
        document_worker.confine_process(
            scratch_root=tmp_path, work_dir=tmp_path, model_dirs=()
        )
    assert "no confinement adapter" in str(refusal.value)


def test_an_unconfined_child_that_extracted_anyway_is_not_believed() -> None:
    # The parent's half of failing closed. A child that reported a real outcome
    # while naming no confinement is refused at the protocol boundary, so a
    # worker built or patched to skip the seal cannot deliver an extraction.
    outcome = extract_document(
        b"extracted without a seal", media_type="text/plain", capture_id=CAPTURE
    )
    reply = {
        "protocol": PROTOCOL_VERSION,
        "confinement": CONFINEMENT_NONE,
        "limits": [],
        "status": document_worker.STATUS_EXTRACTED,
        "outcome": document_worker._encode_outcome(outcome),
    }
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(json.dumps(reply).encode())


@needs_no_confinement
def test_the_production_route_refuses_outright_where_nothing_can_be_sealed() -> None:
    # On a host with no adapter the route produces no outcome at all. The
    # refusal is the worker's own typed reason, never one of the two refusals
    # that mean something about the document.
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"the admitted bytes", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_ISOLATION


def test_a_worker_failure_is_neither_of_the_two_document_verdicts() -> None:
    # What a caller must never do with a refused seal, a crash or a timeout is
    # record it as a verdict on the document, and the type system is the first
    # thing that stops it: `DocumentWorkerFailed` is neither refusal, so an
    # `except` clause for either one cannot catch it. What the production call
    # sites then do with it is asserted in `test_document_processing.py` and
    # `test_document_capture_vertical.py`, against a real settled obligation.
    failure = DocumentWorkerFailed(REASON_ISOLATION, "no adapter")
    assert not isinstance(
        failure, (DocumentExtractionInvalid, DocumentExtractionUnsupported)
    )
    assert failure.reason == REASON_ISOLATION


# -- equivalence, through a real child ---------------------------------------


@needs_confinement
@pytest.mark.parametrize(("media_type", "content"), PORTABLE_SOURCES)
def test_a_confined_child_returns_the_outcome_the_adapters_produce(
    media_type: str, content: bytes
) -> None:
    through_the_worker = extract_document_isolated(
        content, media_type=media_type, capture_id=CAPTURE
    )
    in_process = extract_document(content, media_type=media_type, capture_id=CAPTURE)
    # Field for field, including the located segments and the output digest:
    # the boundary is a transport and changes no extraction semantics.
    assert through_the_worker == in_process
    assert through_the_worker.segments
    assert through_the_worker.output_digest == in_process.output_digest


@needs_confinement
def test_an_unsupported_media_type_still_raises_its_own_refusal() -> None:
    with pytest.raises(DocumentExtractionUnsupported):
        extract_document_isolated(
            b"legacy", media_type="application/msword", capture_id=CAPTURE
        )


@needs_confinement
def test_an_undecodable_source_still_raises_its_own_refusal() -> None:
    with pytest.raises(DocumentExtractionInvalid):
        extract_document_isolated(
            b"\xff", media_type="text/plain", capture_id=CAPTURE
        )


def test_the_frozen_settings_cross_the_boundary_whole() -> None:
    for settings in (
        text_only_settings(),
        ExtractionSettings(
            processing_mode=MODE_TEXT_AND_LOCAL_OCR,
            ocr=OcrResources(
                engine="tesseract",
                engine_revision="5.5.3",
                languages=("eng", "deu"),
                model_hashes=("sha256:" + "a" * 64,),
                tessdata_dir="/opt/omnivia/tessdata",
                available=True,
            ),
            embedded_image_ocr=True,
        ),
        ExtractionSettings(
            processing_mode=MODE_TEXT_AND_LOCAL_OCR,
            ocr=OcrResources(
                engine="tesseract",
                engine_revision="5.5.3",
                languages=("fra",),
                available=False,
                reason="ocr_resources_unconfigured",
                unavailable_models=("fra",),
                resource_detail="no_trusted_model_directory",
            ),
        ),
    ):
        carried = document_worker._decode_settings(
            document_worker._encode_settings(settings)
        )
        assert carried == settings


def test_settings_carrying_a_field_this_protocol_never_sends_are_refused() -> None:
    # The settings are the frozen trusted decisions, the pinned model directory
    # among them. A settings object that is nearly this protocol's did not come
    # from it, and reading the parts of it that look familiar is how a drifted
    # or forged header gets partially honoured.
    encoded = document_worker._encode_settings(text_only_settings())
    assert encoded is not None
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_settings({**encoded, "tessdataDir": "/elsewhere"})
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_settings(
            {key: value for key, value in encoded.items() if key != "ocr"}
        )


def test_pinned_ocr_resources_carrying_an_unknown_field_are_refused() -> None:
    settings = ExtractionSettings(
        processing_mode=MODE_TEXT_AND_LOCAL_OCR,
        ocr=OcrResources(
            engine="tesseract",
            engine_revision="5.5.3",
            languages=("eng",),
            model_hashes=("sha256:" + "c" * 64,),
            tessdata_dir="/opt/omnivia/tessdata",
            available=True,
        ),
        embedded_image_ocr=True,
    )
    encoded = document_worker._encode_settings(settings)
    assert encoded is not None
    ocr = dict(encoded["ocr"])
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_settings(
            {**encoded, "ocr": {**ocr, "modelDir": "/elsewhere"}}
        )
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_settings(
            {
                **encoded,
                "ocr": {
                    key: value for key, value in ocr.items() if key != "modelHashes"
                },
            }
        )


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        # An outcome where the status does not carry one, and none where it
        # does. Both are a reply that is not this protocol's answer.
        (document_worker.STATUS_EXTRACTED, {}),
        (document_worker.STATUS_UNSUPPORTED, {"outcome": {}}),
        (document_worker.STATUS_UNSUPPORTED, {"detail": "a reason it never has"}),
        (document_worker.STATUS_INVALID, {}),
        (document_worker.STATUS_WORKER_ERROR, {}),
        (document_worker.STATUS_ISOLATION_REFUSED, {}),
        (document_worker.STATUS_EXTRACTED, {"outcome": {}, "unexpected": 1}),
        # A null where the reason belongs is a missing field wearing a key.
        (document_worker.STATUS_WORKER_ERROR, {"detail": None}),
    ],
)
def test_a_reply_that_is_not_its_own_status_shape_is_refused(
    status: str, extra: dict[str, Any]
) -> None:
    reply = {
        "protocol": PROTOCOL_VERSION,
        "confinement": CONFINEMENT_NONE
        if status == document_worker.STATUS_ISOLATION_REFUSED
        else REQUIRED_CONFINEMENT,
        "limits": [],
        "status": status,
        **extra,
    }
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(json.dumps(reply).encode())


@pytest.mark.parametrize(
    "status",
    [
        document_worker.STATUS_EXTRACTED,
        document_worker.STATUS_UNSUPPORTED,
        document_worker.STATUS_INVALID,
    ],
)
def test_no_statement_about_the_document_is_read_from_an_unsealed_child(
    status: str,
) -> None:
    # Extracted, unsupported and invalid are the three replies that say
    # something about the source. Each of them requires the seal; the worker's
    # own failures are terminal either way and are not held to it.
    outcome = extract_document(b"sealed or not", media_type="text/plain", capture_id=CAPTURE)
    reply: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "confinement": CONFINEMENT_NONE,
        "limits": [],
        "status": status,
    }
    if status == document_worker.STATUS_EXTRACTED:
        reply["outcome"] = document_worker._encode_outcome(outcome)
    if status == document_worker.STATUS_INVALID:
        reply["detail"] = "a verdict an unsealed process does not get to give"
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(json.dumps(reply).encode())


def test_only_the_pinned_model_directory_is_handed_to_the_seal() -> None:
    assert document_worker._model_dirs(None) == ()
    assert document_worker._model_dirs(text_only_settings()) == ()
    pinned = ExtractionSettings(
        processing_mode=MODE_TEXT_AND_LOCAL_OCR,
        ocr=OcrResources(
            engine="tesseract",
            engine_revision="5.5.3",
            languages=("eng",),
            model_hashes=("sha256:" + "b" * 64,),
            tessdata_dir="/opt/omnivia/tessdata",
        ),
    )
    assert document_worker._model_dirs(pinned) == (Path("/opt/omnivia/tessdata"),)


# -- truthful failures -------------------------------------------------------


def test_a_refused_seal_refuses_the_extraction_before_a_byte_is_read(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path, b"never read")

    def refuse(**_: Any) -> str:
        raise IsolationUnavailable("the sandbox profile was refused by the host")

    reply = document_worker._run(
        _header(attempt), limits=(), confine=refuse
    )
    assert reply["status"] == document_worker.STATUS_ISOLATION_REFUSED
    # No outcome at all: a refused seal never reaches the extraction, so there
    # is nothing here that could be settled as one.
    assert "outcome" not in reply
    assert reply["confinement"] == CONFINEMENT_NONE
    with pytest.raises(DocumentWorkerFailed) as refusal:
        document_worker._outcome_from_reply(document_worker._decode_reply(
            json.dumps(reply).encode()
        ))
    assert refusal.value.reason == REASON_ISOLATION


def test_a_worker_that_exits_non_zero_is_a_crash_and_not_an_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [sys.executable, "-c", "raise SystemExit(3)"],
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_CRASHED
    assert failure.value.detail == "exit 3"


def test_a_worker_killed_by_a_signal_is_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
        ],
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_CRASHED
    assert failure.value.detail == f"signal {int(signal.SIGSEGV)}"


def test_a_worker_stopped_by_a_resource_ceiling_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CPU ceiling's own signal is a resource outcome, not a crash: the
    # worker was stopped by a limit this module applied to it.
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGXCPU)",
        ],
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_RESOURCE_LIMIT


def test_a_worker_that_outruns_its_budget_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_worker, "EXTRACTION_SECONDS", 1)
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [sys.executable, "-c", "import time; time.sleep(120)"],
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_TIMEOUT


@pytest.mark.skipif(os.name != "posix", reason="sessions and killpg are POSIX")
def test_a_timeout_takes_the_processes_the_child_started_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The budget bounds the tree, not just the one process that was spawned.

    A parser reaches Pandoc and Tesseract through a subprocess of its own, so a
    timeout that killed only the worker would leave the expensive half running.
    The child is given its own session for exactly this, and the stand-in here
    is a grandchild that would outlive its parent by two minutes.
    """
    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time;"
        " grandchild = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(600)']);"
        f" open({str(marker)!r}, 'w').write(str(grandchild.pid));"
        " time.sleep(600)"
    )
    monkeypatch.setattr(document_worker, "EXTRACTION_SECONDS", 3)
    monkeypatch.setattr(
        document_worker, "_worker_argv", lambda: [sys.executable, "-c", script]
    )

    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )

    assert failure.value.reason == REASON_TIMEOUT
    pid = int(marker.read_text())
    # The grandchild was never this process's child, so nothing here reaps it:
    # a signal-free `kill` is how its absence is observed, and the small wait is
    # for the reparenting that follows the session kill.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} outlived the worker's budget")


def test_a_worker_that_writes_nothing_is_not_an_empty_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        document_worker, "_worker_argv", lambda: [sys.executable, "-c", "pass"]
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_PROTOCOL
    assert failure.value.detail == "the worker wrote no reply"


def test_a_reply_that_is_not_this_protocol_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys;"
                " header = json.loads(sys.stdin.buffer.read());"
                " pathlib.Path(header['replyPath'])"
                ".write_bytes(b'<not json at all>')"
            ),
        ],
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_PROTOCOL


@needs_confinement
def test_an_oversize_reply_is_refused_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ceiling is checked against the reply's size on disk, so an oversize
    # reply is never decoded and never allocated.
    monkeypatch.setattr(document_worker, "MAX_REPLY_BYTES", 16)
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_PROTOCOL
    assert failure.value.detail.startswith("the reply is ")


def test_an_oversize_source_is_refused_before_a_child_is_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_worker, "MAX_SOURCE_BYTES", 4)

    def must_not_spawn() -> list[str]:
        raise AssertionError("an oversize source must never reach a child")

    monkeypatch.setattr(document_worker, "_worker_argv", must_not_spawn)
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"far too long", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_SOURCE_OVERSIZE


def test_a_reply_claiming_another_confinement_is_not_believed() -> None:
    reply = {
        "protocol": PROTOCOL_VERSION,
        "confinement": "a-sandbox-this-platform-does-not-have",
        "limits": [],
        "status": document_worker.STATUS_EXTRACTED,
    }
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(json.dumps(reply).encode())


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_a_reply_whose_version_is_not_this_exact_integer_is_refused(
    version: object,
) -> None:
    # `True == 1` and `1.0 == 1`, so the version check is a type check as well
    # as an equality: a reply has to name this protocol as the integer it is.
    reply = {
        "protocol": version,
        "confinement": REQUIRED_CONFINEMENT,
        "limits": [],
        "status": document_worker.STATUS_EXTRACTED,
    }
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(json.dumps(reply).encode())


def test_a_reply_nested_past_the_decoder_is_a_protocol_refusal() -> None:
    # Deep enough to exhaust the JSON scanner's own recursion before any depth
    # bound of this module's is consulted. That has to leave the boundary as a
    # typed protocol refusal and not as a bare RecursionError.
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(b"[" * 20_000 + b"]" * 20_000)


def test_a_scratch_directory_the_child_sealed_is_still_removed(
    tmp_path: Path,
) -> None:
    # A parser with write access to the work directory can leave a directory it
    # refuses to give up. The admitted bytes do not stay in the system
    # temporary directory because of it.
    scratch = tmp_path / "scratch"
    (scratch / "work" / "left").mkdir(parents=True)
    (scratch / "work" / "left" / "spill.bin").write_bytes(b"x")
    (scratch / "work" / "left").chmod(0o500)
    assert document_worker._remove_scratch(scratch) == ""
    assert not scratch.exists()


@pytest.mark.skipif(os.name != "posix", reason="symlinks and modes are POSIX here")
def test_cleanup_never_follows_a_link_the_child_aimed_outside_the_scratch(
    tmp_path: Path,
) -> None:
    """The escape this cleanup existed to avoid, driven the way a parser would.

    Making a left-behind directory removable means changing its mode, and
    ``os.walk`` lists a symlink to a directory among its directory names while
    refusing to descend into it. A ``chmod`` by name follows that link, so a
    child that dropped ``work/escape -> /some/host/directory`` had the *host's*
    directory remoded -- 0755 to 0700 -- by the cleanup of its own scratch.
    Nothing outside the scratch may be touched, and the scratch still goes.
    """
    scratch = tmp_path / "scratch"
    work = scratch / "work"
    work.mkdir(parents=True)
    (scratch / "source.bin").write_bytes(b"the admitted bytes")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "keep.txt").write_bytes(b"not the worker's to touch")
    outside.chmod(0o755)
    # Everything a hostile child could leave at a name the cleanup walks: a
    # link to a directory, a link to a file, and a dangling one.
    (work / "escape").symlink_to(outside, target_is_directory=True)
    (work / "escape-file").symlink_to(outside / "keep.txt")
    (work / "escape-nowhere").symlink_to(tmp_path / "never-existed")
    before = outside.stat().st_mode

    assert document_worker._remove_scratch(scratch) == ""

    # The copy of the admitted bytes is gone.
    assert not scratch.exists()
    # The link targets are untouched: same mode, same content, still there.
    assert outside.stat().st_mode == before
    assert oct(outside.stat().st_mode)[-3:] == "755"
    assert (outside / "keep.txt").read_bytes() == b"not the worker's to touch"
    assert sorted(entry.name for entry in outside.iterdir()) == ["keep.txt"]


@pytest.mark.skipif(os.name != "posix", reason="symlinks are POSIX here")
def test_cleanup_follows_no_link_standing_where_the_scratch_root_is(
    tmp_path: Path,
) -> None:
    # The same hazard one level up. `shutil.rmtree` refuses a symlinked root
    # outright, and that refusal has to arrive as a reported residue rather
    # than as a silently successful removal of somebody else's directory.
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True, mode=0o755)
    (outside / "keep.txt").write_bytes(b"still here")
    (outside / "inner").chmod(0o755)
    link = tmp_path / "scratch"
    link.symlink_to(outside, target_is_directory=True)
    assert document_worker._remove_scratch(link) != ""
    assert (outside / "keep.txt").read_bytes() == b"still here"
    # Not even remoded: the walk that restores modes never started.
    assert oct((outside / "inner").stat().st_mode)[-3:] == "755"


def test_a_cleanup_that_leaves_the_admitted_bytes_behind_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cleanup used to be `rmtree(ignore_errors=True)`, so a copy of the
    # admitted source could stay on the host while a successful extraction was
    # returned. A residue is its own terminal failure instead.
    left = _refuse_removal(monkeypatch)
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"the admitted bytes", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == document_worker.REASON_SCRATCH_RESIDUE
    assert failure.value.detail.endswith("OSError")
    # The copy of the admitted bytes really is still there, which is the whole
    # reason this cannot be returned as a success.
    assert left and (left[0] / "source.bin").read_bytes() == b"the admitted bytes"
    shutil.rmtree(left[0], ignore_errors=True)


def test_a_cleanup_failure_never_replaces_the_cause_the_worker_already_had(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A worker that crashed and a scratch that would not go is still a crash:
    # the residue is the lesser fact and must not overwrite the reason the
    # caller records against the obligation.
    left = _refuse_removal(monkeypatch)
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [sys.executable, "-c", "raise SystemExit(3)"],
    )
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"text", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_CRASHED
    shutil.rmtree(left[0], ignore_errors=True)


def test_the_cleanup_walk_is_bounded_and_says_so_rather_than_running_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Nothing bounds how many names a parser can create inside its own scratch.
    # The walk stops at the bound and reports a residue; it does not claim the
    # bytes are gone and it does not run until something else stops it.
    monkeypatch.setattr(document_worker, "MAX_SCRATCH_ENTRIES", 4)
    scratch = tmp_path / "scratch"
    work = scratch / "work"
    work.mkdir(parents=True)
    for index in range(8):
        (work / f"spill-{index}").write_bytes(b"x")
    assert "over 4 entries" in document_worker._remove_scratch(scratch)
    # Nothing was removed on the way to that answer, so the residue is honest.
    assert scratch.exists()


def test_a_reply_naming_another_protocol_version_is_refused() -> None:
    reply = {
        "protocol": PROTOCOL_VERSION + 1,
        "confinement": REQUIRED_CONFINEMENT,
        "limits": [],
        "status": document_worker.STATUS_EXTRACTED,
    }
    with pytest.raises(document_worker._ProtocolError):
        document_worker._decode_reply(json.dumps(reply).encode())


def test_transported_segments_that_are_not_the_reported_output_are_refused() -> None:
    outcome = extract_document(
        b"the real segments", media_type="text/plain", capture_id=CAPTURE
    )
    encoded = document_worker._encode_outcome(outcome)
    encoded["segments"][0]["text"] = "something else entirely"
    reply = {
        "protocol": PROTOCOL_VERSION,
        "confinement": REQUIRED_CONFINEMENT,
        "limits": [],
        "status": document_worker.STATUS_EXTRACTED,
        "outcome": encoded,
    }
    with pytest.raises(DocumentWorkerFailed) as failure:
        document_worker._outcome_from_reply(
            document_worker._decode_reply(json.dumps(reply).encode())
        )
    assert failure.value.reason == REASON_PROTOCOL
    assert failure.value.detail == "the transported segments are not the reported output"


def test_a_parser_defect_is_the_workers_own_failure_and_carries_no_source_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempt = _attempt(tmp_path, b"a secret phrase from the document")

    def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("a secret phrase from the document broke the parser")

    monkeypatch.setattr(document_worker, "extract_document", explode)
    reply = document_worker._run(
        _header(attempt), limits=(), confine=lambda **_: REQUIRED_CONFINEMENT
    )
    assert reply["status"] == document_worker.STATUS_WORKER_ERROR
    # A type and a code location, and nothing the source said.
    assert reply["detail"].startswith("RuntimeError@")
    assert "secret phrase" not in reply["detail"]
    with pytest.raises(DocumentWorkerFailed) as failure:
        document_worker._outcome_from_reply(
            document_worker._decode_reply(json.dumps(reply).encode())
        )
    assert failure.value.reason == REASON_INTERNAL


def test_a_handed_source_that_is_not_the_admitted_source_is_refused(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path, b"the admitted bytes")
    header = _header(attempt)
    Path(attempt["source"]).write_bytes(b"substituted after admission")
    reply = document_worker._run(
        header, limits=(), confine=lambda **_: REQUIRED_CONFINEMENT
    )
    assert reply["status"] == document_worker.STATUS_PROTOCOL_INVALID
    assert "outcome" not in reply


def test_the_worker_refuses_to_emit_more_than_the_protocol_admits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(document_worker, "MAX_REPLY_BYTES", 32)
    target = tmp_path / "reply.json"
    document_worker._write_reply(
        target,
        {
            "protocol": PROTOCOL_VERSION,
            "confinement": REQUIRED_CONFINEMENT,
            "limits": [],
            "status": document_worker.STATUS_EXTRACTED,
            "outcome": {"padding": "x" * 1024},
        },
    )
    written = json.loads(target.read_bytes())
    assert written["status"] == document_worker.STATUS_WORKER_ERROR
    assert written["detail"].startswith("the extraction output is ")


def test_a_reply_cannot_be_written_over_something_already_at_its_name(
    tmp_path: Path,
) -> None:
    # A parser that ran in this process had write access to this directory, so
    # the one thing the reply must not do is land wherever a name taken before
    # it points. The exclusive create refuses, and the parent then reports a
    # worker that produced no reply.
    target = tmp_path / "reply.json"
    target.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        document_worker._write_reply(target, {"protocol": PROTOCOL_VERSION})
    assert not (tmp_path / "elsewhere").exists()


def test_a_reply_read_through_a_planted_symlink_is_refused(tmp_path: Path) -> None:
    # The parent's half of the same hazard: the reply is opened without
    # following a link, so a symlink left at that name aims the read nowhere.
    (tmp_path / "elsewhere").write_bytes(b'{"protocol": 1}')
    target = tmp_path / "reply.json"
    target.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(DocumentWorkerFailed) as failure:
        document_worker._read_reply(target)
    assert failure.value.reason == REASON_PROTOCOL


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are POSIX")
def test_a_reply_that_is_a_fifo_returns_bounded_instead_of_waiting(
    tmp_path: Path,
) -> None:
    """The reply's name is a name a hostile parser chose what to put at.

    A FIFO with no writer is the worst of those choices: a plain blocking open
    of one never returns, so this thread would sit in the parent *after* the
    wall-clock budget that was supposed to bound the whole attempt had already
    been settled -- no timeout left to fire, and no process left to kill. The
    open refuses to block and the descriptor is required to be a regular file.
    """
    target = tmp_path / "reply.json"
    os.mkfifo(target)
    started = time.monotonic()
    with pytest.raises(DocumentWorkerFailed) as failure:
        document_worker._read_reply(target)
    elapsed = time.monotonic() - started
    assert failure.value.reason == REASON_PROTOCOL
    assert failure.value.detail == "the reply is not a regular file"
    # Bounded return, not a hang. The read is a handful of syscalls; anything
    # near a second means it waited on the FIFO.
    assert elapsed < 5, elapsed


def test_a_reply_that_is_a_directory_is_not_read_as_one(tmp_path: Path) -> None:
    target = tmp_path / "reply.json"
    target.mkdir()
    (target / "decoy.json").write_bytes(b'{"protocol": 1}')
    with pytest.raises(DocumentWorkerFailed) as failure:
        document_worker._read_reply(target)
    assert failure.value.reason == REASON_PROTOCOL
    assert failure.value.detail == "the reply is not a regular file"


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs a character device")
def test_a_reply_that_is_a_device_is_not_read_as_one() -> None:
    # `/dev/zero` is endless and `st_size` is 0 on it, so a size-bounded read
    # of a device is bounded by nothing the protocol decided.
    with pytest.raises(DocumentWorkerFailed) as failure:
        document_worker._read_reply(Path("/dev/zero"))
    assert failure.value.reason == REASON_PROTOCOL
    assert failure.value.detail == "the reply is not a regular file"


@pytest.mark.skipif(os.name != "posix", reason="`resource` is POSIX only")
def test_the_applied_resource_limits_are_named_and_not_overstated() -> None:
    # Applied in a child, because a CPU ceiling set on this process would end
    # the test session. Only limits the host actually binds may be named, so
    # RLIMIT_AS is absent on macOS by design rather than by omission.
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-s",
            "-B",
            "-c",
            (
                "import json, resource;"
                " from omnivia_core_runtime.service import document_worker as worker;"
                " names = worker._apply_resource_limits(120);"
                " print(json.dumps("
                "[list(names), resource.getrlimit(resource.RLIMIT_CPU)]))"
            ),
        ],
        env=document_worker._child_environment(Path(os.getcwd())),
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    names, cpu = json.loads(completed.stdout)
    assert "RLIMIT_CPU" in names
    assert "RLIMIT_CORE" in names
    assert "RLIMIT_AS" not in names
    assert cpu == [120, 125]
