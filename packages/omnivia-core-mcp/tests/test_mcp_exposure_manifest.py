"""The curated exposure manifest is an allow-list, and these are its properties.

R004-06's requirements, each as a test that fails when it stops being true:
the surface is curated rather than derived from the catalogue, every entry is
read-only, `tools/list` is deterministic, the schemas are projected from the
public operation contracts, and the operations R004-06 names as never
model-callable are absent and uncallable.
"""

from __future__ import annotations

import dataclasses

import pytest
from omnivia_core_mcp import manifest

from omnivia_core.contracts.v1 import (
    OPERATION_CATALOGUE,
    ContractSemanticError,
    get_operation_metadata,
)

# The operations R004-06 forbids as model-callable tools in the first slice.
# Named literally, because the point is that a future edit that adds one has to
# delete a line here that says why it must not.
FORBIDDEN = (
    "workspace.create",  # bootstrap / workspace initialisation
    "memory.create",  # persistent mutation
    "candidate.approve",  # persistent mutation
    "candidate.reject",  # persistent mutation
    "knowledge.propose",  # persistent mutation
    "record.supersede",  # destructive mutation
    "import.start",  # persistent mutation
    "job.cancel",  # mutation
    "job.retry",  # mutation
)


def test_the_manifest_is_curated_not_the_whole_catalogue() -> None:
    """The catalogue is a capability list; this is a security decision."""
    exposed = {entry.operation for entry in manifest.EXPOSURE_MANIFEST}
    catalogue = {entry.name for entry in OPERATION_CATALOGUE}
    assert exposed < catalogue, "the manifest must be a strict subset"
    assert exposed == {"workspace.inspect"}, (
        "the approved first surface is workspace.inspect alone: R004-06's "
        "retrieval and context-pack operations are not yet classified safe for "
        "MCP use, and this manifest does not make that classification for V06-3"
    )


def test_every_exposed_operation_is_in_the_landed_catalogue() -> None:
    """R004-06 forbids inventing identifiers, so each one must resolve."""
    for entry in manifest.EXPOSURE_MANIFEST:
        assert get_operation_metadata(entry.operation).name == entry.operation


def test_every_exposed_operation_is_read_only() -> None:
    for entry in manifest.EXPOSURE_MANIFEST:
        catalogue = get_operation_metadata(entry.operation)
        assert catalogue.scope.side_effect == "none", entry.operation
        assert catalogue.audit.audit_category == "read", entry.operation


def test_a_mutating_operation_cannot_be_admitted() -> None:
    """Read-first is enforced at import, not asserted in a comment.

    An editor who adds `memory.create` to the manifest does not ship a mutation
    tool with a reassuring docstring; the package refuses to import.
    """
    with pytest.raises(ValueError, match="side_effect"):
        manifest._admit(
            manifest.ExposedOperation(
                tool_name="memory_create",
                operation="memory.create",
                purpose="memory_write",
                title="",
                description="",
            )
        )


def test_an_operation_outside_the_catalogue_cannot_be_admitted() -> None:
    with pytest.raises(ContractSemanticError, match="unknown application operation"):
        manifest._admit(
            manifest.ExposedOperation(
                tool_name="invented",
                operation="context.retrieve_v2",
                purpose="retrieval",
                title="",
                description="",
            )
        )


@pytest.mark.parametrize("operation", FORBIDDEN)
def test_the_never_exposed_operations_are_absent(operation: str) -> None:
    assert operation not in {
        entry.operation for entry in manifest.EXPOSURE_MANIFEST
    }


def test_no_service_lifecycle_operation_is_exposed() -> None:
    """Start, stop, health, readiness, status and discovery are not tools.

    They are not in the catalogue at all -- the service dispatches them against
    its own grant -- so this asserts the stronger fact: no exposed tool names one,
    and `_admit` could not resolve one if it did.
    """
    exposed = {entry.operation for entry in manifest.EXPOSURE_MANIFEST}
    for lifecycle in (
        "core.readiness",
        "core.health",
        "core.status",
        "service.discover",
        "service.start",
        "service.stop",
    ):
        assert lifecycle not in exposed
        with pytest.raises(ContractSemanticError):
            get_operation_metadata(lifecycle)


def test_tools_list_is_deterministic_for_this_package_version() -> None:
    """R004-06. Same object, same order, same content, on every call."""
    first, second = manifest.tools(), manifest.tools()
    assert first is second
    assert [tool.name for tool in first] == [
        entry.tool_name for entry in manifest.EXPOSURE_MANIFEST
    ]
    assert [tool.model_dump(mode="json") for tool in first] == [
        tool.model_dump(mode="json") for tool in second
    ]


def test_each_tool_schema_is_projected_from_the_operation_contract() -> None:
    """The schema is *reached* from the catalogue entry, never transcribed here.

    The projection path is `input_schema_ref` -> the `#/$defs/<Name>` fragment ->
    the generated dataclass of that name in the public contracts package. A
    payload type renamed upstream therefore fails here rather than projecting a
    stale shape.
    """
    for entry in manifest.EXPOSURE_MANIFEST:
        catalogue = get_operation_metadata(entry.operation)
        contract = manifest._contract_type(catalogue.input_schema_ref)
        assert dataclasses.is_dataclass(contract)
        assert contract.__name__ == catalogue.input_schema_ref.rsplit("/", 1)[-1]
        projected = manifest._input_schema(catalogue)
        assert sorted(projected["properties"]) == sorted(
            field.name for field in dataclasses.fields(contract)
        )


def test_an_input_with_fields_is_refused_rather_than_guessed_at() -> None:
    """The projector projects the empty payload and refuses the rest, loudly.

    Exposing an operation that takes arguments is blocked on giving this package
    a real projector. That is the right place to be blocked: a hand-rolled
    dataclass-to-JSON-Schema guess would be the "manually redefined" schema
    R004-06 rules out, wearing a projection's clothes.
    """
    with pytest.raises(ValueError, match="project the empty input payload only"):
        manifest._input_schema(get_operation_metadata("memory.search"))


def test_each_tool_carries_annotations_read_off_the_catalogue() -> None:
    for tool in manifest.tools():
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.open_world_hint is False


def test_each_tool_records_the_contract_it_was_projected_from() -> None:
    for tool, entry in zip(manifest.tools(), manifest.EXPOSURE_MANIFEST, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        assert tool.meta == {
            "omnivia.manifestVersion": manifest.MANIFEST_VERSION,
            "omnivia.operation": catalogue.name,
            "omnivia.inputSchemaRef": catalogue.input_schema_ref,
            "omnivia.resultSchemaRef": catalogue.result_schema_ref,
        }


def test_a_tool_name_absent_from_the_manifest_resolves_to_nothing() -> None:
    """The allow-list is the only lookup the call path has."""
    assert manifest.exposed_by_tool_name("workspace_inspect") is not None
    for absent in ("workspace_create", "memory_create", "core_readiness", ""):
        assert manifest.exposed_by_tool_name(absent) is None


# --- the pending transport seam ----------------------------------------------


def test_the_default_transport_factory_refuses_as_a_transport_failure() -> None:
    """The one thing this package cannot do yet, and how it says so.

    `omnivia-core-client` exports no concrete local transport and this package may
    not import the CLI's, so the default factory cannot build one. It refuses with
    `TransportError` -- the client's own declared failure for "could not carry a
    call" -- which means the call path needs no special case for the pending
    decision, and the day a real transport lands only this function changes.
    """
    from omnivia_core_client import ClientError, TransportError
    from omnivia_core_mcp.server import _default_transport_factory

    with pytest.raises(TransportError, match="no concrete local transport"):
        _default_transport_factory("unix:///tmp/omnivia/run/s.sock")
    assert issubclass(TransportError, ClientError)


def test_a_tool_call_reports_the_pending_transport_instead_of_crashing() -> None:
    """A production server advertises honestly and refuses readably.

    Not an exception out of the handler: the model is told the tool is real, the
    service is attached, and only the dial is missing.
    """
    import mcp_types as types
    from omnivia_core_mcp.managed_start import Attachment
    from omnivia_core_mcp.server import _call_tool, _default_transport_factory

    result = _call_tool(
        types.CallToolRequestParams(name="workspace_inspect", arguments={}),
        attachment=Attachment(
            status="attached", endpoint_uri="unix:///tmp/s.sock", workspace_id="ws-1"
        ),
        transport_factory=_default_transport_factory,
    )
    assert result.is_error is True
    assert "no concrete local transport" in result.content[0].text
