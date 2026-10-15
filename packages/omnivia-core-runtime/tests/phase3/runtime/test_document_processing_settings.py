"""`document.settings.get` / `document.settings.update` through the governed dispatcher.

The claim these two operations make is narrow and easy to break quietly, so each half of
it has a test here rather than a comment:

* the product defaults are what an unconfigured workspace reports, and revision zero says
  so rather than pretending a row exists;
* a change is durable -- read back from a *separate* read-only connection to the same
  file, which is what "survives a restart" means when the writing process is gone;
* the revision is a real compare-and-swap, not an echo;
* a selection naming a language the managed cache does not hold is refused at the
  settings surface, so a preference that could never take effect cannot be saved; and
* a capture freezes the preferences it was admitted under, and a later change does not
  reach it.

The last one is the whole reason the preferences are read once at admission, so it is
asserted against the stored capture row rather than against anything the handler returns.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_runtime.service import processing_settings as settings_store
from omnivia_core_runtime.service.document_processing import (
    APPROVED_PROFILE_REVISION,
    DEFAULT_PROFILE_REF,
)
from omnivia_core_runtime.service.handlers.document_capture import (
    DOCUMENT_CAPTURE_OPERATION,
)
from omnivia_core_runtime.service.handlers.document_settings import (
    DOCUMENT_SETTINGS_GET_OPERATION,
    DOCUMENT_SETTINGS_UPDATE_OPERATION,
)
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    OCR_RESOURCES_DIR,
    ManagedOcrResources,
)
from omnivia_core_runtime.service.ocr_resource_store import OcrResourceSetIdentity
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    TESSERACT_REVISION,
)
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from test_evidence_capture_vertical import Served, _metadata, build_dispatcher

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    RETRY_CLASS_NON_RETRYABLE,
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import (
    DocumentProcessingSettingsGetResult,
    DocumentProcessingSettingsUpdateResult,
)

pytest_plugins = ("test_evidence_capture_vertical",)

WORKSPACE = "ws-capture-0001"
DOCUMENT = "doc-settings-1"
#: Any body will do: `install` checks the identity's digest and length against the bytes,
#: never that they are a usable model, so a short deterministic payload is honest here.
MODEL_BODY = b"tessdata-stand-in"


# --- the service surface --------------------------------------------------------


def served(owned: Served, installation_root: Path) -> Any:
    """`owned`, plus the one installation fact the settings handlers read.

    The shared `Served` names only what the capture family touches. These handlers also
    reach `installation.root`, to ask the managed OCR cache what is installed, so the
    surface is widened here by exactly that and nothing else.
    """
    installation_root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        generation=owned.generation,
        layout=owned.layout,
        installation=SimpleNamespace(root=installation_root),
    )


def install_language(
    installation_root: Path, language: str, payload: Path, body: bytes = MODEL_BODY
) -> None:
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(body)
    ManagedOcrResources(installation_root / OCR_RESOURCES_DIR).install(
        payload,
        OcrResourceSetIdentity(
            language=language,
            engine_version=TESSERACT_REVISION,
            model_version="tessdata_fast-4.1.0",
            byte_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        ),
    )


def install_distinct(installation_root: Path, tmp_path: Path, *languages: str) -> None:
    """Each language installed with bytes of its own, as a real bundle would be.

    Distinct bodies matter wherever a model hash is asserted: identical bytes would
    collapse three pinned digests into one and hide which models were read.
    """
    for language in languages:
        install_language(
            installation_root,
            language,
            tmp_path / "downloads" / f"{language}.bin",
            body=MODEL_BODY + language.encode(),
        )


# --- requests -------------------------------------------------------------------


def get_request(
    *, request_id: str = "req-get-1", workspace_id: str = WORKSPACE, extra: Any = None
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_SETTINGS_GET_OPERATION)
    return RequestEnvelope(
        operation=DOCUMENT_SETTINGS_GET_OPERATION,
        metadata=_metadata(
            entry, request_id=request_id, key=None, workspace_id=workspace_id
        ),
        input={} if extra is None else dict(extra),
    )


def update_request(
    *, request_id: str, key: str, body: dict[str, Any], workspace_id: str = WORKSPACE
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_SETTINGS_UPDATE_OPERATION)
    return RequestEnvelope(
        operation=DOCUMENT_SETTINGS_UPDATE_OPERATION,
        metadata=_metadata(
            entry, request_id=request_id, key=key, workspace_id=workspace_id
        ),
        input=body,
    )


def capture_request(*, request_id: str, key: str, content: bytes) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_CAPTURE_OPERATION)
    return RequestEnvelope(
        operation=DOCUMENT_CAPTURE_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key=key),
        input={
            "document_id": DOCUMENT,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "media_type": "text/plain",
            "requested_source_identity": f"source-{key}",
            "intent_kind": "new_capture",
            "base": {"kind": "creation"},
            "target_binding_revision": "target-binding-1",
            "processing_profile": {
                "profile_ref": DEFAULT_PROFILE_REF,
                "revision": APPROVED_PROFILE_REVISION,
            },
            "original_binding": {
                "owner_ref": "access-settings-1",
                "generation": 1,
                "route_kind": "direct_file",
            },
        },
    )


# --- helpers --------------------------------------------------------------------


def answered(response: Any) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def refusal(response: Any) -> ErrorResponseEnvelope:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def read(dispatcher: Any, **kwargs: Any) -> Any:
    response = answered(dispatcher.dispatch(get_request(**kwargs)))
    return DocumentProcessingSettingsGetResult.from_wire(response.result).settings


def change(dispatcher: Any, **kwargs: Any) -> Any:
    response = answered(dispatcher.dispatch(update_request(**kwargs)))
    return DocumentProcessingSettingsUpdateResult.from_wire(response.result).settings


# --- the defaults ---------------------------------------------------------------


def test_an_unconfigured_workspace_reports_the_product_defaults(
    owned: Served, tmp_path: Path
) -> None:
    """Revision zero is the statement that nothing is stored, not a first version."""
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-a"), tag="set-default")
    settings = read(dispatcher)
    assert settings.mode == MODE_TEXT_AND_LOCAL_OCR
    assert settings.recognition_languages == ("eng",)
    assert settings.embedded_image_ocr is False
    assert settings.revision == 0


def test_the_read_refuses_a_selector_rather_than_answering_for_another_workspace(
    owned: Served, tmp_path: Path
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-b"), tag="set-closed")
    error = refusal(
        dispatcher.dispatch(get_request(extra={"workspace_id": "ws-other-00000002"}))
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST


# --- durability -----------------------------------------------------------------


def test_a_change_is_durable_and_readable_by_a_process_that_did_not_write_it(
    owned: Served, tmp_path: Path
) -> None:
    """What "survives a restart" means: the bytes are in the file, not in this process."""
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-c"), tag="set-durable")
    changed = change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "mode": MODE_TEXT_ONLY, "embedded_image_ocr": True},
    )
    assert changed.mode == MODE_TEXT_ONLY
    assert changed.embedded_image_ocr is True
    assert changed.revision == 1

    # The owning connection is exclusive, so a second reader can only exist once this
    # instance has gone -- which is exactly the restart this test is about.
    owned.connection.close()
    reader = open_database(owned.layout.database_path, OpenMode.READ_ONLY)
    try:
        stored, revision = settings_store.read_settings(reader, workspace_id=WORKSPACE)
    finally:
        reader.close()
    assert stored.mode == MODE_TEXT_ONLY
    assert stored.embedded_image_ocr is True
    assert stored.languages == ("eng",)
    assert revision == 1


def test_an_absent_member_is_held_rather_than_reset_to_its_default(
    owned: Served, tmp_path: Path
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-d"), tag="set-partial")
    change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "embedded_image_ocr": True},
    )
    after = change(
        dispatcher,
        request_id="req-u2",
        key="idem-u2",
        body={"revision": 1, "mode": MODE_TEXT_ONLY},
    )
    assert after.mode == MODE_TEXT_ONLY
    assert after.embedded_image_ocr is True
    assert after.revision == 2


# --- the compare-and-swap -------------------------------------------------------


def test_a_stale_revision_is_refused_rather_than_overwriting_the_change_it_missed(
    owned: Served, tmp_path: Path
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-e"), tag="set-cas")
    change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "mode": MODE_TEXT_ONLY},
    )
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u2",
                key="idem-u2",
                body={"revision": 0, "embedded_image_ocr": True},
            )
        )
    )
    assert error.error.code == ERROR_CODE_CONFLICT
    # The refused change left nothing behind: the revision and the values are the ones
    # the accepted change wrote, not a half-applied mixture of the two.
    settings = read(dispatcher, request_id="req-g2")
    assert settings.revision == 1
    assert settings.mode == MODE_TEXT_ONLY
    assert settings.embedded_image_ocr is False


def test_a_first_change_must_assert_that_nothing_is_stored(
    owned: Served, tmp_path: Path
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-f"), tag="set-first")
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u1", key="idem-u1", body={"revision": 3, "mode": MODE_TEXT_ONLY}
            )
        )
    )
    assert error.error.code == ERROR_CODE_CONFLICT


# --- idempotency ----------------------------------------------------------------


def test_a_same_key_replay_is_answered_from_the_settled_change(
    owned: Served, tmp_path: Path
) -> None:
    """A retried change answers the first attempt rather than advancing the revision.

    The distinction matters more here than for most mutations: a second application of
    the same change would move the revision, and the caller's next compare-and-swap --
    made against the revision the first answer reported -- would then be refused for a
    change it had itself already made.
    """
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-o"), tag="set-replay")
    body = {"revision": 0, "mode": MODE_TEXT_ONLY, "embedded_image_ocr": True}
    first = answered(
        dispatcher.dispatch(update_request(request_id="req-u1", key="idem-u1", body=dict(body)))
    )
    replay = answered(
        dispatcher.dispatch(update_request(request_id="req-u2", key="idem-u1", body=dict(body)))
    )
    assert replay.result == first.result
    assert replay.metadata.audit_reference == first.metadata.audit_reference
    # One application, not two: the stored revision is the one the first answer reported.
    assert read(dispatcher).revision == 1


def test_a_changed_body_under_a_settled_key_conflicts_rather_than_applying(
    owned: Served, tmp_path: Path
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-p"), tag="set-reuse")
    change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "mode": MODE_TEXT_ONLY},
    )
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u2",
                key="idem-u1",
                body={"revision": 0, "embedded_image_ocr": True},
            )
        )
    )
    assert error.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    settings = read(dispatcher)
    assert settings.revision == 1
    assert settings.embedded_image_ocr is False


# --- the retry contract ---------------------------------------------------------


def test_the_catalogue_states_what_each_operation_promises_a_retrying_caller() -> None:
    """The read is safe to retry; the change is not, and so demands a key.

    Asserted against the published catalogue because this is the pair of facts a host
    retries on, and the handlers' own behaviour below has to agree with them.
    """
    get_entry = get_operation_metadata(DOCUMENT_SETTINGS_GET_OPERATION)
    assert get_entry.idempotency.safe_to_retry is True
    assert get_entry.idempotency.supports_idempotency_key is False
    update_entry = get_operation_metadata(DOCUMENT_SETTINGS_UPDATE_OPERATION)
    assert update_entry.idempotency.safe_to_retry is False
    assert update_entry.idempotency.required is True
    # Neither operation takes a mutation precondition: the revision is the whole
    # concurrency story, and a second precondition could disagree with it.
    assert update_entry.precondition.supports_mutation_precondition is False


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(
            {"revision": 7, "mode": MODE_TEXT_ONLY}, ERROR_CODE_CONFLICT, id="conflict"
        ),
        pytest.param(
            {"revision": 0, "mode": "text_and_cloud_ocr"},
            ERROR_CODE_INVALID_REQUEST,
            id="invalid",
        ),
        pytest.param(
            {"revision": 0, "recognition_languages": ["deu"]},
            ERROR_CODE_INVALID_REQUEST,
            id="not-installed",
        ),
    ],
)
def test_a_refusal_tells_the_caller_not_to_retry_it_unchanged(
    owned: Served, tmp_path: Path, body: dict[str, Any], code: str
) -> None:
    """Every refusal this handler raises is the caller's to resolve, not the clock's.

    A stale revision, a malformed change and an uninstalled language are all answered
    `non_retryable`: each needs the caller to read again, fix the request or install
    something, and a host that retried any of them on a timer would spin.
    """
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-q"), tag="set-retry")
    error = refusal(
        dispatcher.dispatch(update_request(request_id="req-u1", key="idem-u1", body=body))
    )
    assert error.error.code == code
    assert error.error.retry_class == RETRY_CLASS_NON_RETRYABLE


# --- authorization --------------------------------------------------------------


@pytest.mark.parametrize("operation", ["get", "update"])
def test_a_workspace_the_session_does_not_grant_never_reaches_the_store(
    owned: Served, tmp_path: Path, operation: str
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-g"), tag="set-auth")
    request = (
        get_request(workspace_id="ws-other-00000002")
        if operation == "get"
        else update_request(
            request_id="req-u1",
            key="idem-u1",
            body={"revision": 0, "mode": MODE_TEXT_ONLY},
            workspace_id="ws-other-00000002",
        )
    )
    error = refusal(dispatcher.dispatch(request))
    assert error.error.code == ERROR_CODE_WORKSPACE_NOT_GRANTED


# --- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"revision": 0, "quality": "high"}, id="unknown-member"),
        pytest.param({"revision": 0, "mode": "text_and_cloud_ocr"}, id="unknown-mode"),
        pytest.param({"revision": -1, "mode": MODE_TEXT_ONLY}, id="negative-revision"),
        pytest.param({"revision": 0, "recognition_languages": []}, id="empty-selection"),
        pytest.param(
            {"revision": 0, "recognition_languages": ["eng", "eng"]}, id="duplicate"
        ),
        pytest.param(
            {"revision": 0, "recognition_languages": ["eng", "deu", "fra", "spa"]},
            id="over-the-limit",
        ),
        pytest.param({"revision": 0, "recognition_languages": ["osd"]}, id="orientation"),
        pytest.param(
            {"revision": 0, "recognition_languages": ["../eng"]}, id="outside-the-domain"
        ),
    ],
)
def test_a_malformed_change_is_refused_and_stores_nothing(
    owned: Served, tmp_path: Path, body: dict[str, Any]
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-h"), tag="set-invalid")
    error = refusal(
        dispatcher.dispatch(update_request(request_id="req-u1", key="idem-u1", body=body))
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST
    assert read(dispatcher, request_id="req-g1").revision == 0


# --- installed languages --------------------------------------------------------


def test_a_language_the_installation_has_not_installed_cannot_be_selected(
    owned: Served, tmp_path: Path
) -> None:
    """The managed cache is empty here, so every non-default selection is refused.

    Fail-closed is the point: a preference naming bytes the installation has not got
    could never take effect, and saving it would report a decision as applied that no
    extraction could honour.
    """
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-i"), tag="set-missing")
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u1",
                key="idem-u1",
                body={"revision": 0, "recognition_languages": ["deu"]},
            )
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST
    assert read(dispatcher).recognition_languages == ("eng",)


def test_the_product_default_is_selectable_even_with_nothing_installed(
    owned: Served, tmp_path: Path
) -> None:
    """Otherwise a fresh installation could not state the defaults it is already using."""
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-j"), tag="set-default-ok")
    changed = change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "recognition_languages": ["eng"]},
    )
    assert changed.recognition_languages == ("eng",)
    assert changed.revision == 1


def test_an_installed_language_is_selectable_in_the_order_it_was_named(
    owned: Served, tmp_path: Path
) -> None:
    root = tmp_path / "inst-k"
    service = served(owned, root)
    install_language(root, "deu", tmp_path / "downloads" / "deu.bin")
    install_language(root, "fra", tmp_path / "downloads" / "fra.bin")
    dispatcher = build_dispatcher(service, tag="set-installed")
    changed = change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "recognition_languages": ["fra", "deu"]},
    )
    assert changed.recognition_languages == ("fra", "deu")
    assert read(dispatcher).recognition_languages == ("fra", "deu")


def test_one_uninstalled_language_refuses_the_whole_selection(
    owned: Served, tmp_path: Path
) -> None:
    root = tmp_path / "inst-l"
    service = served(owned, root)
    install_language(root, "deu", tmp_path / "downloads" / "deu.bin")
    dispatcher = build_dispatcher(service, tag="set-partial-install")
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u1",
                key="idem-u1",
                body={"revision": 0, "recognition_languages": ["deu", "jpn"]},
            )
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST


def test_what_is_installed_is_the_exact_managed_identity_and_nothing_near_it(
    owned: Served, tmp_path: Path
) -> None:
    """One definition of "installed", and it is the whole five-field identity.

    The settings surface and the engine path both ask this question, and both must get
    the same answer, so the identity is reported entire rather than reduced to a
    language code: a language whose bytes were built for another engine revision is a
    different resource set, and offering it would present a model to an engine that may
    not read it.
    """
    root = tmp_path / "inst-r"
    install_language(root, "deu", tmp_path / "downloads" / "deu.bin")
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)
    installed = settings_store.installed_identities(resources)
    assert set(installed) == {"deu"}
    assert installed["deu"] == OcrResourceSetIdentity(
        language="deu",
        engine_version=TESSERACT_REVISION,
        model_version="tessdata_fast-4.1.0",
        byte_size=len(MODEL_BODY),
        sha256=hashlib.sha256(MODEL_BODY).hexdigest(),
    )


def test_a_resource_set_built_for_another_engine_revision_is_not_installed(
    owned: Served, tmp_path: Path
) -> None:
    """Present in the cache, absent from what may be selected -- and refused as missing.

    This is the case a language-code-only check would get wrong: the bytes are there,
    the cache reports them, and selecting them would hand a model built for one engine
    revision to another.
    """
    root = tmp_path / "inst-s"
    payload = tmp_path / "downloads" / "jpn-foreign.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(MODEL_BODY)
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)
    resources.install(
        payload,
        OcrResourceSetIdentity(
            language="jpn",
            engine_version=f"{TESSERACT_REVISION}-next",
            model_version="tessdata_fast-4.1.0",
            byte_size=len(MODEL_BODY),
            sha256=hashlib.sha256(MODEL_BODY).hexdigest(),
        ),
    )
    assert any(
        resource.identity.language == "jpn" for resource in resources.inventory().resources
    )
    assert settings_store.installed_identities(resources) == {}

    dispatcher = build_dispatcher(served(owned, root), tag="set-foreign-engine")
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u1",
                key="idem-u1",
                body={"revision": 0, "recognition_languages": ["jpn"]},
            )
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST


def test_a_refused_selection_substitutes_nothing_and_installs_nothing(
    owned: Served, tmp_path: Path
) -> None:
    """The refusal is the whole outcome: no English stand-in, no fetch, no stored row.

    A substitution would be the worst of the three -- the caller asked for one language,
    was told the change succeeded, and would be recognised in another.
    """
    root = tmp_path / "inst-t"
    dispatcher = build_dispatcher(served(owned, root), tag="set-no-substitute")
    error = refusal(
        dispatcher.dispatch(
            update_request(
                request_id="req-u1",
                key="idem-u1",
                body={"revision": 0, "recognition_languages": ["deu"]},
            )
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST
    # Nothing stored at all, so the reported defaults are the product's rather than a
    # saved English selection standing in for the German one.
    assert read(dispatcher).revision == 0
    assert (
        settings_store.installed_identities(ManagedOcrResources(root / OCR_RESOURCES_DIR))
        == {}
    )


# --- what the managed cache can serve the engine --------------------------------


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(("fra", "deu"), id="two"),
        pytest.param(("deu", "fra", "ita"), id="three"),
    ],
)
def test_an_ordered_selection_of_installed_languages_is_served_together(
    tmp_path: Path, order: tuple[str, ...]
) -> None:
    """The managed layout installs each language as its own identity in its own
    directory, and the engine takes one model directory. A selection of two or three
    is served from a projected directory holding all of them -- not from the first
    language's own directory with the rest reported missing, which is what a
    single-directory catalogue would have had to do.
    """
    root = tmp_path / "inst-multi"
    install_distinct(root, tmp_path, "deu", "fra", "ita")
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)

    catalogue = settings_store.managed_catalogue(resources, order)
    resolved = catalogue.resolve(order)

    assert resolved.available
    # The requested order, preserved: the engine is invoked once with it.
    assert resolved.languages == order
    assert resolved.tesseract_lang == "+".join(order)
    assert len(resolved.model_hashes) == len(order)
    assert resolved.tessdata_dir is not None
    served = Path(resolved.tessdata_dir)
    assert served.parent == resources.selections_root
    assert sorted(child.name for child in served.iterdir()) == sorted(
        f"{language}.traineddata" for language in order
    )
    # And each language is still installed under its own identity, so retaining two
    # model revisions of one language stays possible.
    installed = settings_store.installed_identities(resources)
    assert len(
        {resources.resource_directory(installed[language]) for language in order}
    ) == len(order)


def test_an_uninstalled_language_is_reported_as_exactly_itself(tmp_path: Path) -> None:
    """Not "nothing is configured", and not the whole order: the one language whose
    bytes are absent, so the recovery an operator is shown is installing that one.
    """
    root = tmp_path / "inst-partial"
    install_distinct(root, tmp_path, "deu")
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)

    resolved = settings_store.managed_catalogue(resources, ("deu", "jpn")).resolve(
        ("deu", "jpn")
    )

    assert not resolved.available
    assert resolved.reason == "needs_ocr_language"
    assert resolved.languages == ("deu", "jpn")
    assert resolved.unavailable_models == ("jpn",)
    assert resolved.model_hashes == ()


def test_an_empty_cache_serves_nothing_rather_than_substituting(tmp_path: Path) -> None:
    root = tmp_path / "inst-empty"
    root.mkdir(parents=True)
    resources = ManagedOcrResources(root / OCR_RESOURCES_DIR)

    catalogue = settings_store.managed_catalogue(resources, ("deu",))

    assert catalogue.installed == {}
    assert catalogue.tessdata_dir is None
    assert not catalogue.resolve(("deu",)).available


def test_an_installation_resolver_asks_the_managed_cache_of_that_root(
    tmp_path: Path,
) -> None:
    """The one join between an installation root and a frozen selection, made once."""
    root = tmp_path / "inst-resolver"
    install_distinct(root, tmp_path, "deu", "fra")

    resolver = settings_store.installation_catalogue(root)

    assert resolver is not None
    assert resolver(("fra", "deu")).resolve(("fra", "deu")).available
    # No root, and a root no managed cache may live under, both fail closed to the
    # packaged catalogue rather than searching the host for models.
    assert settings_store.installation_catalogue(None) is None
    assert settings_store.installation_catalogue(Path("relative/root")) is None


# --- the closed pair of modes ---------------------------------------------------


@pytest.mark.parametrize("mode", [MODE_TEXT_AND_LOCAL_OCR, MODE_TEXT_ONLY])
def test_each_mode_the_contract_publishes_is_selectable(
    owned: Served, tmp_path: Path, mode: str
) -> None:
    """Both halves of the closed pair, so a refusal of the unknown mode is not a refusal
    of everything but the default.
    """
    assert settings_store.PROCESSING_MODES == (MODE_TEXT_AND_LOCAL_OCR, MODE_TEXT_ONLY)
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-u"), tag="set-modes")
    changed = change(
        dispatcher, request_id="req-u1", key="idem-u1", body={"revision": 0, "mode": mode}
    )
    assert changed.mode == mode
    assert read(dispatcher).mode == mode


# --- the embedded-image preference ----------------------------------------------


def test_the_embedded_image_flag_moves_in_both_directions(
    owned: Served, tmp_path: Path
) -> None:
    """On and off are both changes, so turning it back off cannot be read as "unset"."""
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-v"), tag="set-embedded")
    on = change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "embedded_image_ocr": True},
    )
    assert on.embedded_image_ocr is True
    off = change(
        dispatcher,
        request_id="req-u2",
        key="idem-u2",
        body={"revision": 1, "embedded_image_ocr": False},
    )
    assert off.embedded_image_ocr is False
    assert off.revision == 2
    assert read(dispatcher).embedded_image_ocr is False


def test_the_embedded_image_preference_applies_only_to_sources_that_carry_images() -> None:
    """Scoped once, where the frozen profile is built, so no profile claims a pass that
    could not run: a PDF or an image capture is covered by the mode, not by this flag.
    """
    settings = settings_store.ProcessingSettings(embedded_image_ocr=True)
    assert settings.embedded_image_ocr_for(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert settings.embedded_image_ocr_for("text/html")
    assert not settings.embedded_image_ocr_for("application/pdf")
    assert not settings.embedded_image_ocr_for("image/png")
    assert not settings.embedded_image_ocr_for("text/plain")
    off = settings_store.ProcessingSettings(embedded_image_ocr=False)
    assert not off.embedded_image_ocr_for("text/html")


# --- what a capture freezes -----------------------------------------------------


def test_a_capture_records_the_preferences_it_was_admitted_under(
    owned: Served, tmp_path: Path
) -> None:
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-m"), tag="set-capture")
    change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "mode": MODE_TEXT_ONLY, "embedded_image_ocr": True},
    )
    answered(
        dispatcher.dispatch(
            capture_request(request_id="req-c1", key="idem-c1", content=b"body one")
        )
    )
    history = store.read_capture_history(
        owned.connection, workspace_id=WORKSPACE, document_id=DOCUMENT, offset=0, limit=10
    )
    assert len(history) == 1
    assert json.loads(history[0].capture.processing_settings_json or "") == {
        "embeddedImageOcr": True,
        "languages": ["eng"],
        "mode": MODE_TEXT_ONLY,
    }


def test_a_later_change_does_not_reach_a_capture_already_admitted(
    owned: Served, tmp_path: Path
) -> None:
    """The reason the preferences are read once, at admission, instead of at execution."""
    dispatcher = build_dispatcher(served(owned, tmp_path / "inst-n"), tag="set-frozen")
    answered(
        dispatcher.dispatch(
            capture_request(request_id="req-c1", key="idem-c1", content=b"body one")
        )
    )
    change(
        dispatcher,
        request_id="req-u1",
        key="idem-u1",
        body={"revision": 0, "mode": MODE_TEXT_ONLY, "embedded_image_ocr": True},
    )
    history = store.read_capture_history(
        owned.connection, workspace_id=WORKSPACE, document_id=DOCUMENT, offset=0, limit=10
    )
    frozen = json.loads(history[0].capture.processing_settings_json or "")
    assert frozen == {
        "embeddedImageOcr": False,
        "languages": ["eng"],
        "mode": MODE_TEXT_AND_LOCAL_OCR,
    }
    # And the live preference did move, so the test is about the capture rather than
    # about an update that silently did nothing.
    assert read(dispatcher).mode == MODE_TEXT_ONLY
