"""Semantic validation for public service endpoint descriptors."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts import v1
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    ServiceEndpointDescriptor,
    ServiceProbeResult,
    ServiceProcessEvidence,
    VersionWindow,
)
from omnivia_core.contracts.v1.semantics_service import (
    decode_service_endpoint_descriptor,
    decode_service_probe_result,
    encode_service_endpoint_descriptor,
    encode_service_probe_result,
    validate_service_endpoint_descriptor,
    validate_service_probe_result,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "contracts"
    / "fixtures"
    / "service-endpoint-uri-policy-v1.json"
)


def _fixture() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return document


def _descriptor_document(endpoint_uri: str) -> dict[str, Any]:
    return {
        "descriptor_version": "1.0",
        "workspace_id": "workspace-1",
        "service_instance_id": "instance-9c21",
        "installation_id": "installation-4b70",
        "endpoint_uri": endpoint_uri,
        "protocol_version": "1.2",
        "server_version": "1.2.5",
        "supported_api_versions": {"minimum": "1.0", "maximum": "1.2"},
        "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.1"},
        "workspace_format_version": "1.1",
        "ready": True,
        "lifecycle_state": "serving",
        "fencing_generation": 7,
        "published_at": "2026-07-30T00:00:00Z",
    }


def _descriptor_value(endpoint_uri: str) -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor(
        descriptor_version="1.0",
        workspace_id="workspace-1",
        service_instance_id="instance-9c21",
        installation_id="installation-4b70",
        endpoint_uri=endpoint_uri,
        protocol_version="1.2",
        server_version="1.2.5",
        supported_api_versions=VersionWindow(minimum="1.0", maximum="1.2"),
        supported_workspace_versions=VersionWindow(minimum="1.0", maximum="1.1"),
        workspace_format_version="1.1",
        ready=True,
        lifecycle_state="serving",
        fencing_generation=7,
        published_at="2026-07-30T00:00:00Z",
    )


def _probe_result_document(endpoint_uri: str) -> dict[str, Any]:
    return {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
        "descriptor": _descriptor_document(endpoint_uri),
    }


def test_service_endpoint_semantics_are_on_the_public_v1_surface() -> None:
    assert v1.decode_service_endpoint_descriptor is decode_service_endpoint_descriptor
    assert v1.decode_service_probe_result is decode_service_probe_result
    assert v1.encode_service_endpoint_descriptor is encode_service_endpoint_descriptor
    assert v1.encode_service_probe_result is encode_service_probe_result
    assert v1.validate_service_endpoint_descriptor is validate_service_endpoint_descriptor
    assert v1.validate_service_probe_result is validate_service_probe_result


@pytest.mark.parametrize("case", _fixture()["accepted"], ids=lambda case: case["id"])
def test_public_semantics_accept_approved_dialable_endpoint(case: dict[str, str]) -> None:
    endpoint_uri = case["endpoint_uri"]
    descriptor = decode_service_endpoint_descriptor(_descriptor_document(endpoint_uri))
    validate_service_endpoint_descriptor(descriptor)
    assert encode_service_endpoint_descriptor(descriptor)["endpoint_uri"] == endpoint_uri


@pytest.mark.parametrize("case", _fixture()["rejected"], ids=lambda case: case["id"])
def test_public_semantics_reject_unsafe_endpoint_without_echoing_it(
    case: dict[str, str],
) -> None:
    endpoint_uri = case["endpoint_uri"]
    with pytest.raises(ContractSemanticError) as decoded_error:
        decode_service_endpoint_descriptor(_descriptor_document(endpoint_uri))
    assert endpoint_uri not in str(decoded_error.value)

    descriptor = _descriptor_value(endpoint_uri)
    with pytest.raises(ContractSemanticError) as validation_error:
        validate_service_endpoint_descriptor(descriptor)
    assert endpoint_uri not in str(validation_error.value)

    with pytest.raises(ContractSemanticError) as encoded_error:
        encode_service_endpoint_descriptor(descriptor)
    assert endpoint_uri not in str(encoded_error.value)


@pytest.mark.parametrize("case", _fixture()["rejected"], ids=lambda case: case["id"])
def test_probe_result_semantics_reject_nested_unsafe_descriptor_without_echoing_it(
    case: dict[str, str],
) -> None:
    endpoint_uri = case["endpoint_uri"]
    document = _probe_result_document(endpoint_uri)
    with pytest.raises(ContractSemanticError) as decoded_error:
        decode_service_probe_result(document)
    assert endpoint_uri not in str(decoded_error.value)

    result = ServiceProbeResult.from_wire(document)
    with pytest.raises(ContractSemanticError) as validation_error:
        validate_service_probe_result(result)
    assert endpoint_uri not in str(validation_error.value)

    with pytest.raises(ContractSemanticError) as encoded_error:
        encode_service_probe_result(result)
    assert endpoint_uri not in str(encoded_error.value)


def test_process_evidence_spelling_is_not_constrained_by_endpoint_semantics() -> None:
    """P0 governs the endpoint URI only; process start-time spelling stays opaque.

    `start_time` is compared for equality against a later reading of the same pid
    and never parsed, so this layer must not start refusing whatever spelling a
    host platform happens to report.
    """
    descriptor = replace(
        _descriptor_value("unix:///run/omnivia/core.sock"),
        process=ServiceProcessEvidence(
            pid=4242,
            start_time="Mon Jul 27 09:14:02 2026",
            boot_id="boot-1",
        ),
    )
    validate_service_endpoint_descriptor(descriptor)
    assert encode_service_endpoint_descriptor(descriptor)["process"] == {
        "pid": 4242,
        "start_time": "Mon Jul 27 09:14:02 2026",
        "boot_id": "boot-1",
    }
