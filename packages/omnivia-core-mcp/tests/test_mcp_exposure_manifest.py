"""The curated exposure manifest is an allow-list, and these are its properties.

R004-06's requirements, each as a test that fails when it stops being true: the
surface is curated rather than derived from the catalogue, every entry is
read-only, `tools/list` is deterministic, the advertised schemas are generated
from the public operation contracts rather than transcribed, and the operations
R004-06 names as never model-callable are absent and uncallable.

Manifest version `1.1` is the six-operation surface. `1.0` advertised
`workspace.inspect` alone with no output schema and an input projector that
could only emit the empty payload; everything below that reads as new coverage
rather than as a rewrite is the difference between those two facts.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_mcp import manifest
from omnivia_core_mcp.generated_schema_projection import SCHEMAS

from omnivia_core.contracts.v1 import (
    OPERATION_CATALOGUE,
    ContractSemanticError,
    get_operation_metadata,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate-mcp-exposure-schemas.py"

# The exposed surface, restated here as a literal. The manifest is the allow-list
# and this is the review record of what was allowed: a seventh tool, a reordered
# listing or a renamed operation has to change this line, which is the point.
EXPECTED_SURFACE = (
    ("workspace_inspect", "workspace.inspect", "workspace_inspection"),
    ("evidence_search", "evidence.search", "knowledge_retrieval"),
    ("knowledge_search", "knowledge.search", "knowledge_retrieval"),
    ("memory_search", "memory.search", "knowledge_retrieval"),
    ("graph_traverse", "graph.traverse", "knowledge_retrieval"),
    ("context_pack_build", "context_pack.build", "knowledge_retrieval"),
)

# The operations R004-06 forbids as model-callable tools. Named literally,
# because the point is that a future edit that adds one has to delete a line here
# that says why it must not.
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

#: The absolute base every canonical Application Contract v1 reference carries,
#: and the one string that must not survive into an advertised schema: an MCP
#: host resolves nothing over the network, so a document still carrying one of
#: these is a document it cannot use.
CANONICAL_BASE = "https://contracts.omnivia.dev/"


def generator() -> Any:
    """The schema generator, imported from its hyphenated script path.

    `scripts/` is not an import package and the file name is not an identifier,
    so this is the only way to reach it. It is reached rather than reimplemented
    because the assertions below are about *that* script -- the one preflight and
    `Core acceptance` run -- and a second copy of its rules here would be a
    second thing to keep in step.
    """
    specification = importlib.util.spec_from_file_location(
        "_mcp_schema_generator", GENERATOR_PATH
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# --- the surface is curated ---------------------------------------------------


def test_the_manifest_is_curated_not_the_whole_catalogue() -> None:
    """The catalogue is a capability list; this is a security decision."""
    exposed = {entry.operation for entry in manifest.EXPOSURE_MANIFEST}
    catalogue = {entry.name for entry in OPERATION_CATALOGUE}
    assert exposed < catalogue, "the manifest must be a strict subset"
    assert len(catalogue) > len(exposed) + 1, (
        "the catalogue is a capability list of twenty operations; a manifest that "
        "had grown to nearly all of it would no longer be a curated surface"
    )


def test_the_exposed_surface_is_exactly_the_reviewed_six_in_order() -> None:
    """Tool name, operation and claimed purpose, all three, in manifest order.

    Order is asserted rather than membership: `tools/list` returns this sequence
    verbatim, and R004-06 requires that listing to be deterministic for a given
    package version.
    """
    assert (
        tuple(
            (entry.tool_name, entry.operation, entry.purpose)
            for entry in manifest.EXPOSURE_MANIFEST
        )
        == EXPECTED_SURFACE
    )


def test_the_manifest_version_names_this_surface() -> None:
    """A host that cached a `1.0` listing can tell it is stale.

    The distribution version moves for reasons that do not change the tool
    surface, so the surface carries its own. `1.0` was `workspace.inspect` alone
    with no output schema.
    """
    assert manifest.MANIFEST_VERSION == "1.1"


def test_the_purpose_vocabulary_is_the_two_the_surface_claims() -> None:
    """`workspace.inspect` retains `workspace_inspection`; the five reads share
    `knowledge_retrieval`. The purpose is a claim the request states and the
    service checks against its own grant, so the claim has to be the one the
    grant allows -- a third purpose invented here would be refused at the first
    call rather than caught by review."""
    purposes = {entry.operation: entry.purpose for entry in manifest.EXPOSURE_MANIFEST}
    assert purposes.pop("workspace.inspect") == "workspace_inspection"
    assert set(purposes.values()) == {"knowledge_retrieval"}


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
    assert operation not in {entry.operation for entry in manifest.EXPOSURE_MANIFEST}


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


def test_a_tool_name_absent_from_the_manifest_resolves_to_nothing() -> None:
    """The allow-list is the only lookup the call path has."""
    for tool_name, _, _ in EXPECTED_SURFACE:
        assert manifest.exposed_by_tool_name(tool_name) is not None
    for absent in (
        "workspace_create",
        "memory_create",
        "core_readiness",
        "evidence.search",  # the operation identifier is not a tool name
        "EvidenceSearch",
        "",
    ):
        assert manifest.exposed_by_tool_name(absent) is None


# --- the advertised schemas ---------------------------------------------------


def test_each_tool_advertises_both_schemas_from_the_generated_projection() -> None:
    """The schema is *reached* from the catalogue entry, never transcribed here.

    The path is the entry's own `input_schema_ref`/`result_schema_ref` -> the
    generated projection keyed by that exact reference. A contract renamed
    upstream therefore fails here rather than advertising a stale shape.

    `1.0`'s version of this test compared nothing to nothing: `input_schema`
    raised whenever the contract had fields, so on every input that reached the
    comparison both sides were `[]`, and it passed for every possible argument.
    Identity against the generated document is what makes it a check.
    """
    for tool, entry in zip(manifest.tools(), manifest.EXPOSURE_MANIFEST, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        assert tool.input_schema == SCHEMAS[catalogue.input_schema_ref]
        assert tool.output_schema == SCHEMAS[catalogue.result_schema_ref]
        assert manifest.input_schema(catalogue) == tool.input_schema
        assert manifest.output_schema(catalogue) == tool.output_schema


def test_every_tool_advertises_an_output_schema() -> None:
    """`1.0` advertised none, so a host could not validate what came back. The
    official client validates `structuredContent` against this document on every
    successful call, which is the whole reason it is here."""
    for tool in manifest.tools():
        assert tool.output_schema is not None
        assert tool.output_schema["type"] == "object"


def _local_refs(node: Any) -> list[str]:
    """Every `$ref` value anywhere under `node`, in document order."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_local_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_local_refs(item))
    return found


@pytest.mark.parametrize("reference", sorted(SCHEMAS))
def test_every_advertised_schema_is_self_contained_and_needs_no_network(
    reference: str,
) -> None:
    """A `tools/list` schema has to stand on its own, offline, in the host's
    process. So every reference is local, every local reference resolves to a
    declared definition, no definition is declared that nothing points at, and
    the canonical `https://` base appears nowhere in the document -- including in
    the nested `$defs` closure, which is where an unrewritten reference would
    actually hide."""
    document = SCHEMAS[reference]
    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    serialised = repr(document)
    assert CANONICAL_BASE not in serialised, (
        "an advertised schema still carries an absolute canonical reference; an "
        "MCP host cannot resolve one"
    )

    declared = set(document.get("$defs", {}))
    referenced = set()
    for reference_value in _local_refs(document):
        assert reference_value.startswith("#/$defs/"), reference_value
        referenced.add(reference_value.removeprefix("#/$defs/"))
    assert referenced <= declared, f"unresolvable: {sorted(referenced - declared)}"
    assert declared <= referenced, f"unreferenced: {sorted(declared - referenced)}"


def test_the_advertised_payloads_are_closed() -> None:
    """Every input schema refuses a key it does not declare, which is what makes
    the server's own pre-flight refusal a shortcut rather than a second policy."""
    for entry in manifest.EXPOSURE_MANIFEST:
        catalogue = get_operation_metadata(entry.operation)
        schema = manifest.input_schema(catalogue)
        assert schema["unevaluatedProperties"] is False, entry.operation
        assert schema.get("additionalProperties") is not True, entry.operation


def test_an_operation_with_no_generated_schema_is_refused_not_guessed_at() -> None:
    """A reference the generator never emitted is a loud failure, not an empty
    object: advertising `{}` for a shape nothing vouches for is the "manually
    redefined" schema R004-06 rules out, wearing a projection's clothes."""
    with pytest.raises(ValueError, match="no generated schema projection"):
        manifest._projected(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/NotAThingThisContractDeclares"
        )


# --- annotations and provenance -----------------------------------------------


def test_each_tool_carries_annotations_read_off_the_catalogue() -> None:
    """Read-only and non-destructive are facts `_admit` already refused anything
    that would contradict; idempotency is read off the entry rather than
    asserted, and the world is closed because this server is attached to exactly
    one local workspace it cannot be told to leave."""
    for tool, entry in zip(manifest.tools(), manifest.EXPOSURE_MANIFEST, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint == catalogue.idempotency.safe_to_retry
        assert annotations.open_world_hint is False
        assert annotations.title == entry.title


def test_each_tool_records_the_contract_it_was_projected_from() -> None:
    for tool, entry in zip(manifest.tools(), manifest.EXPOSURE_MANIFEST, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        assert tool.meta == {
            "omnivia.manifestVersion": manifest.MANIFEST_VERSION,
            "omnivia.operation": catalogue.name,
            "omnivia.inputSchemaRef": catalogue.input_schema_ref,
            "omnivia.resultSchemaRef": catalogue.result_schema_ref,
        }


# --- the generator ------------------------------------------------------------


def test_the_generator_projects_exactly_the_exposed_operations() -> None:
    """The generator's operation list is a mirror of the manifest, and this is
    what holds it there.

    It is a mirror rather than a read of the manifest because the generator runs
    with only `src/` on its path -- the MCP distribution need not be installed to
    regenerate -- and it is deliberately not derived from the catalogue: deriving
    it would make registering a Core operation enough to advertise it to a model,
    which is the one thing the curated manifest exists to prevent.
    """
    assert generator().EXPOSED_OPERATIONS == tuple(
        entry.operation for entry in manifest.EXPOSURE_MANIFEST
    )


def test_the_committed_projection_is_exactly_what_the_generator_emits() -> None:
    """The `--check` gate, executed here as well as on `Core acceptance`.

    Byte equality against the committed module, so a hand-edit to
    `generated_schema_projection.py` fails in this suite rather than surviving
    until somebody runs the script. `tests/test_core_acceptance_workflow.py` pins
    the same command onto the gate and onto preflight.
    """
    module = generator()
    committed = module.TARGET.read_text(encoding="utf-8")
    assert committed == module.render(), (
        "generated_schema_projection.py is out of date or hand-edited; regenerate "
        "with: python scripts/generate-mcp-exposure-schemas.py"
    )


def test_the_generator_check_mode_reports_success_without_writing() -> None:
    module = generator()
    before = module.TARGET.read_bytes()
    assert module.main(["--check"]) == 0
    assert module.TARGET.read_bytes() == before


@pytest.mark.parametrize(
    "reference",
    [
        "#/$defs/Local",  # not absolute
        "memory.schema.json#/$defs/GovernedRecord",  # relative document
        "https://example.invalid/application/v1/memory.schema.json#/$defs/X",  # host
        "https://contracts.omnivia.dev/application/v1/memory.schema.json",  # no fragment
        "https://contracts.omnivia.dev/application/v1/memory.schema.json#/properties/x",
        "https://contracts.omnivia.dev/application/v2/memory.schema.json#/$defs/X",
    ],
    ids=["local", "relative", "host", "no-fragment", "not-a-def", "wrong-version"],
)
def test_the_generator_refuses_a_reference_it_cannot_resolve(reference: str) -> None:
    """Refused rather than guessed at. Each of these is a shape a canonical
    document could plausibly grow, and resolving any of them by approximation
    would put a schema on the wire that no contract vouches for."""
    module = generator()
    with pytest.raises(module.ProjectionError):
        module.resolve(module.load_documents(), reference)


def test_the_generator_refuses_a_reference_to_a_definition_that_is_not_there() -> None:
    module = generator()
    with pytest.raises(module.ProjectionError, match="declares no"):
        module.resolve(
            module.load_documents(),
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/NoSuchDefinition",
        )


def test_the_generated_keys_are_deterministic_and_namespaced_by_document() -> None:
    """Two documents may each declare a `PageMetadata`, and the closure holds one
    flat `$defs`. Namespacing by document is what keeps that injective, and
    `project` still checks for a collision rather than trusting the argument."""
    module = generator()
    assert module.generated_key("context-pack", "ContextPackMode") == (
        "context_pack__ContextPackMode"
    )
    assert module.generated_key("memory", "MemoryQuery") == "memory__MemoryQuery"


def _import_lines(source: str) -> list[str]:
    """Every `import`/`from` statement in `source`, stripped.

    Statements only. Every module in this repository explains its boundaries in
    prose directly above the code that keeps them, so a search over whole lines
    matches the explanation of a rule as readily as a violation of it.
    """
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def test_the_generator_reads_no_network_and_no_installed_resources() -> None:
    """It reads `contracts/application/v1/schemas` off the working tree and
    nothing else -- no HTTP client, no packaged-resource API, no distribution
    beyond `omnivia_core`'s own catalogue. Stated as a source-level fact because
    the failure it prevents is a generator that quietly starts depending on how
    Core was installed, which is exactly the dependency the checked-in module
    exists to remove."""
    imports = _import_lines(GENERATOR_PATH.read_text(encoding="utf-8"))
    for forbidden in (
        "urllib",
        "requests",
        "http",
        "socket",
        "omnivia_core_runtime",
        "omnivia_core_cli",
        "omnivia_core_client",
        "omnivia_core_mcp",
    ):
        for statement in imports:
            assert forbidden not in statement, f"{forbidden!r} in: {statement}"

    # The packaged-resource API is the other way this could start depending on
    # the install, and it is reached by name rather than by import.
    source = GENERATOR_PATH.read_text(encoding="utf-8")
    for reader in ("read_schema(", "read_schema_text(", "list_schema_names("):
        assert reader not in source, reader


# --- the transport seam -------------------------------------------------------


def test_the_default_transport_factory_builds_the_client_owned_transport() -> None:
    """R005-01 step 4: the real transport, from the client package, not a stand-in.

    Identity is asserted on the class and its defining module, so a local
    re-implementation with the same name in this package would fail here. The
    endpoint URI is carried through unchanged, because a factory that dialled
    somewhere other than the attachment would be the one bug this seam can hide.
    """
    from omnivia_core_client import LocalIpcTransport
    from omnivia_core_mcp.server import _default_transport_factory

    transport = _default_transport_factory("unix:///tmp/omnivia/run/s.sock")

    assert isinstance(transport, LocalIpcTransport)
    assert type(transport).__module__ == "omnivia_core_client.local_ipc"
    assert transport.endpoint_uri == "unix:///tmp/omnivia/run/s.sock"


def test_a_tool_call_against_an_absent_service_refuses_instead_of_crashing() -> None:
    """A dial that fails is an answer for the model, not an exception.

    The message proves the real transport genuinely tried: "could not be reached"
    is `LocalIpcTransport._connect`'s own wording for a refused socket. The
    diagnostic quotes no local path, which is the client's payload-free rule
    holding across the package boundary.
    """
    import mcp_types as types
    from omnivia_core_mcp.managed_start import Attachment
    from omnivia_core_mcp.server import _call_tool, _default_transport_factory

    absent = "unix:///tmp/omnivia-absent-service/s.sock"
    result = _call_tool(
        types.CallToolRequestParams(name="workspace_inspect", arguments={}),
        attachment=Attachment(
            status="attached", endpoint_uri=absent, workspace_id="ws-1"
        ),
        transport_factory=_default_transport_factory,
    )
    assert result.is_error is True
    assert result.structured_content is None
    message = result.content[0].text
    assert "could not be reached" in message
    assert absent not in message


def test_a_transport_failure_is_the_clients_own_error_type() -> None:
    """R005-01 step 6: `TransportError` stays canonical for local transport use.

    Raised by the client package, subclassing the client's `ClientError`, so the
    one handler in the call path catches every documented transport failure --
    and MCP declares no error type of its own for this.
    """
    from omnivia_core_client import ClientError, TransportError, socket_path_for

    with pytest.raises(TransportError):
        socket_path_for("pipe://omnivia-core")
    assert issubclass(TransportError, ClientError)
    assert TransportError.__module__ == "omnivia_core_client.errors"


# --- the call path's own refusals ---------------------------------------------


def _never_dialled(_endpoint_uri: str) -> object:
    raise AssertionError("a refused call must not construct a transport")


def test_a_guessed_tool_name_refuses_before_a_transport_exists() -> None:
    """R004-06: the allow-list is the only lookup, so absent means uncallable --
    and the refusal costs no dial. `workspace_create` is the bootstrap operation
    R004-06 forbids exposing, and it is in the catalogue, so this is a name that
    resolves to a real operation everywhere except here."""
    import mcp_types as types
    from omnivia_core_mcp.managed_start import Attachment
    from omnivia_core_mcp.server import _call_tool

    for guessed in ("workspace_create", "memory_create", "evidence.search", "search"):
        result = _call_tool(
            types.CallToolRequestParams(name=guessed, arguments={}),
            attachment=Attachment(
                status="attached", endpoint_uri="unix:///tmp/s.sock", workspace_id="ws-1"
            ),
            transport_factory=_never_dialled,  # type: ignore[arg-type]
        )
        assert result.is_error is True
        assert result.structured_content is None
        assert "is not a tool this server exposes" in result.content[0].text


def test_an_unadvertised_argument_is_refused_before_a_request_is_built() -> None:
    """The advertised closed payload is enforced, and was not.

    `_request` copied `params.arguments` into `RequestEnvelope.input` verbatim, so
    arbitrary JSON reached the envelope while `tools/list` advertised a closed
    object. Inert while the one exposed operation took no arguments -- but inert
    was a property of that manifest, not of the call path, and nothing tested it
    either way. Five of the six now take real arguments.
    """
    import mcp_types as types
    from omnivia_core_mcp.managed_start import Attachment
    from omnivia_core_mcp.server import _call_tool

    result = _call_tool(
        types.CallToolRequestParams(
            name="knowledge_search",
            arguments={"query": "anything", "workspace_id": "../other"},
        ),
        attachment=Attachment(
            status="attached", endpoint_uri="unix:///tmp/s.sock", workspace_id="ws-1"
        ),
        transport_factory=_never_dialled,  # type: ignore[arg-type]
    )
    assert result.is_error is True
    assert "accepts no argument named 'workspace_id'" in result.content[0].text


def test_the_refusal_is_read_off_the_advertised_schema_not_a_literal_list() -> None:
    """Every advertised key is accepted and everything else is refused, per
    operation. Checked against `input_schema` rather than against a list written
    here, so what `tools/list` says and what `_request` accepts stay one
    projection rather than two that agree by inspection."""
    from omnivia_core_mcp.server import _request

    for entry in manifest.EXPOSURE_MANIFEST:
        catalogue = get_operation_metadata(entry.operation)
        advertised = set(manifest.input_schema(catalogue)["properties"])

        for allowed in (None, {}):
            assert _request(entry, workspace_id="ws-1", arguments=allowed).input == {}

        if advertised:
            # Value-level validation is the service's; this only proves the key
            # survives the pre-flight refusal that an unadvertised one does not.
            payload = dict.fromkeys(advertised, None)
            assert (
                _request(entry, workspace_id="ws-1", arguments=payload).input == payload
            )

        with pytest.raises(ValueError, match="accepts no argument named"):
            _request(
                entry, workspace_id="ws-1", arguments={"not_a_declared_field": 1}
            )


def test_the_request_states_the_catalogue_entrys_own_authority() -> None:
    """Scopes, the capability and its minimum version are read off the frozen
    catalogue entry rather than transcribed, and the purpose is the manifest's
    claim. A model supplies none of them: there is no argument for a workspace,
    a scope, a capability or a purpose anywhere in this package."""
    from omnivia_core_mcp.server import CLIENT_NAME, _request

    for entry in manifest.EXPOSURE_MANIFEST:
        catalogue = get_operation_metadata(entry.operation)
        request = _request(entry, workspace_id="ws-1", arguments=None)

        assert request.operation == entry.operation
        assert request.metadata.workspace_id == "ws-1"
        assert request.metadata.purpose == entry.purpose
        assert request.metadata.scopes == tuple(catalogue.scope.required_scopes)
        assert request.metadata.client.id == CLIENT_NAME
        (required,) = request.metadata.required_capabilities
        assert required.id == catalogue.required_capability.id
        assert required.minimum_version == catalogue.required_capability.minimum_version
        assert required.required == catalogue.required_capability.required


def test_production_mcp_imports_neither_the_runtime_nor_the_cli() -> None:
    """ADR-036's boundary, both ways it can be broken.

    In a *subprocess*, because this suite's own end-to-end module imports the
    runtime in-process to build a workspace: asserting on `sys.modules` here
    would report whatever else pytest had already collected, and would pass or
    fail on test ordering rather than on the package. A fresh interpreter that
    imports only `omnivia_core_mcp.server` -- which is what an MCP host does --
    is the honest form of the question.

    Then the same boundary at source level over every production module, because
    a lazy import inside a handler survives the first check and fails the
    topology on first call.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import omnivia_core_mcp.server; "
                "print(sorted(n for n in sys.modules if n.split('.')[0] in "
                "{'omnivia_core_runtime', 'omnivia_core_cli'}))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip() == "[]", (
        f"importing the production MCP server reached a forbidden sibling: "
        f"{completed.stdout.strip()}"
    )

    # `\b` rather than a substring: `omnivia_core_cli` is a prefix of
    # `omnivia_core_client`, which this package imports and is meant to.
    forbidden = re.compile(r"\bomnivia_core_(?:runtime|cli)\b")
    package = Path(manifest.__file__).parent
    for module in sorted(package.glob("*.py")):
        for statement in _import_lines(module.read_text(encoding="utf-8")):
            assert not forbidden.search(statement), f"{module.name}: {statement}"
