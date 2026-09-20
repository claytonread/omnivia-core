"""The curated exposure manifest is an allow-list, and these are its properties.

R004-06's requirements, each as a test that fails when it stops being true: the
surface is curated rather than derived from the catalogue, `tools/list` is
deterministic per profile, the advertised schemas are generated from the public
operation contracts rather than transcribed, and the operations named as never
model-callable are absent and uncallable.

Manifest version `2.0` is the two-profile surface: the read-only `restricted`
six, unchanged from `1.1`, and the `authoring` eleven that add exactly three
mutations and two job observations. `1.1` was the six alone and had no notion of
a profile; `1.0` advertised `workspace.inspect` alone with no output schema.
Everything below that reads as new coverage rather than as a rewrite is the
difference between those facts.
"""

from __future__ import annotations

import importlib.util
import json
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

# The exposed surface of each profile, restated here as a literal. The manifest
# is the allow-list and this is the review record of what was allowed: a twelfth
# tool, a reordered listing or a renamed operation has to change these lines,
# which is the point.
EXPECTED_RESTRICTED = (
    ("workspace_inspect", "workspace.inspect", "workspace_inspection"),
    ("evidence_search", "evidence.search", "knowledge_retrieval"),
    ("knowledge_search", "knowledge.search", "knowledge_retrieval"),
    ("memory_search", "memory.search", "knowledge_retrieval"),
    ("graph_traverse", "graph.traverse", "knowledge_retrieval"),
    ("context_pack_build", "context_pack.build", "knowledge_retrieval"),
)

EXPECTED_AUTHORING = EXPECTED_RESTRICTED + (
    ("memory_create", "memory.create", "memory_authoring"),
    ("evidence_capture", "evidence.capture", "content_ingestion"),
    ("import_start", "import.start", "content_ingestion"),
    ("job_get", "job.get", "job_observation"),
    ("job_events", "job.events", "job_observation"),
)

EXPECTED_SURFACES = {
    "restricted": EXPECTED_RESTRICTED,
    "authoring": EXPECTED_AUTHORING,
}

# The three mutations the authoring profile admits, and the only side-effecting
# operations any profile may name.
EXPECTED_MUTATIONS = frozenset({"memory.create", "evidence.capture", "import.start"})

# The operations forbidden as model-callable tools, in every profile. Named
# literally, because the point is that a future edit that adds one has to delete
# a line here that says why it must not.
FORBIDDEN = (
    "workspace.create",  # bootstrap / workspace initialisation
    "workspace.list",  # installation-scoped enumeration
    "chat.command",  # persistent mutation
    "workflow.control",  # persistent mutation
    "workflow.start",  # persistent mutation
    "candidate.approve",  # governance decision
    "candidate.reject",  # governance decision
    "knowledge.propose",  # governance decision
    "record.supersede",  # destructive mutation
    "job.cancel",  # job control, not job observation
    "job.retry",  # job control, not job observation
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


@pytest.mark.parametrize("profile", ["restricted", "authoring"])
def test_the_manifest_is_curated_not_the_whole_catalogue(profile: str) -> None:
    """The catalogue is a capability list; this is a security decision."""
    exposed = {entry.operation for entry in manifest.exposure_manifest(profile)}
    catalogue = {entry.name for entry in OPERATION_CATALOGUE}
    assert exposed < catalogue, "the manifest must be a strict subset"
    assert len(catalogue) > len(exposed) + 1, (
        "the catalogue is a capability list of twenty-eight operations; a manifest "
        "that had grown to nearly all of it would no longer be a curated surface"
    )


@pytest.mark.parametrize("profile", ["restricted", "authoring"])
def test_the_exposed_surface_is_exactly_the_reviewed_inventory_in_order(
    profile: str,
) -> None:
    """Tool name, operation and claimed purpose, all three, in manifest order.

    Order is asserted rather than membership: `tools/list` returns this sequence
    verbatim, and R004-06 requires that listing to be deterministic for a given
    package version.
    """
    assert (
        tuple(
            (entry.tool_name, entry.operation, entry.purpose)
            for entry in manifest.exposure_manifest(profile)
        )
        == EXPECTED_SURFACES[profile]
    )


def test_the_two_profiles_are_exactly_six_and_eleven_tools() -> None:
    """The counts the requirements fix, asserted as counts as well as names: a
    listing that gained a tool and lost one would satisfy neither line."""
    assert len(manifest.exposure_manifest("restricted")) == 6
    assert len(manifest.exposure_manifest("authoring")) == 11
    assert len(manifest.tools("restricted")) == 6
    assert len(manifest.tools("authoring")) == 11


def test_the_authoring_profile_is_the_restricted_six_plus_five() -> None:
    """Concatenation, not a second listing of the shared six: the profiles cannot
    drift in a tool name, a title or a description they both advertise."""
    restricted = manifest.exposure_manifest("restricted")
    assert manifest.exposure_manifest("authoring")[:6] == restricted
    assert [
        entry.tool_name for entry in manifest.exposure_manifest("authoring")[6:]
    ] == ["memory_create", "evidence_capture", "import_start", "job_get", "job_events"]


def test_restricted_is_the_safe_default_for_a_caller_that_names_no_profile() -> None:
    """`EXPOSURE_MANIFEST`, `tools()` and `exposed_by_tool_name()` all answer with
    the read-only surface when nobody says otherwise. A caller written before
    profiles existed -- the server's `tools/list` handler among them -- advertises
    six read tools rather than eleven, which is the failure mode this default
    should have."""
    assert manifest.EXPOSURE_MANIFEST == manifest.exposure_manifest("restricted")
    assert manifest.tools() is manifest.tools("restricted")
    assert manifest.exposure_manifest() == manifest.exposure_manifest("restricted")
    assert manifest.exposed_by_tool_name("memory_create") is None
    assert manifest.exposed_by_tool_name("memory_create", "authoring") is not None


def test_an_unknown_profile_is_refused_rather_than_narrowed() -> None:
    """Not a silent fallback to `restricted`: an unrecognised profile is a
    configuration this module cannot serve, and serving the narrow surface
    instead would hide it until somebody wondered why a tool was missing."""
    assert manifest.PROFILES == ("restricted", "authoring")
    for unknown in ("", "READ_ONLY", "full", "admin", "Authoring"):
        with pytest.raises(ValueError, match="not an MCP exposure profile"):
            manifest.tools(unknown)
        with pytest.raises(ValueError, match="not an MCP exposure profile"):
            manifest.exposure_manifest(unknown)
        with pytest.raises(ValueError, match="not an MCP exposure profile"):
            manifest.exposed_by_tool_name("workspace_inspect", unknown)


OPERATION_TRACEABILITY = (
    REPO_ROOT / "tests" / "fixtures" / "service_conformance" / "operation-traceability-v1.json"
)


def test_the_operation_traceability_mcp_mapping_is_this_manifest() -> None:
    """The ledger's accepted MCP mapping is exactly this allow-list, nothing more.

    The service-conformance suite checks the ledger against the catalogue but
    may not import this package; this is the other half. Exposed operations and
    tool names in manifest order, the manifest version, and every catalogue
    operation outside the manifest recorded as an intentional omission.
    """
    mapping = json.loads(OPERATION_TRACEABILITY.read_text(encoding="utf-8"))[
        "client_surfaces"
    ]["mcp"]
    assert mapping["mapping_state"] == "accepted"
    assert mapping["mapping_source"]["manifest_version"] == manifest.MANIFEST_VERSION
    assert mapping["mapping_source"]["symbol"] == "EXPOSURE_MANIFEST"
    assert (REPO_ROOT / mapping["mapping_source"]["file"]).resolve() == (
        Path(manifest.__file__).resolve()
    )
    assert [
        (entry["operation"], entry["tool"]) for entry in mapping["exposed"]
    ] == [(entry.operation, entry.tool_name) for entry in manifest.EXPOSURE_MANIFEST]
    exposed = {entry.operation for entry in manifest.EXPOSURE_MANIFEST}
    assert [entry["operation"] for entry in mapping["omitted"]] == [
        entry.name for entry in OPERATION_CATALOGUE if entry.name not in exposed
    ]
    for entry in mapping["omitted"]:
        assert manifest.exposed_by_tool_name(entry["operation"].replace(".", "_")) is None


def test_the_manifest_version_names_this_surface() -> None:
    """A host that cached a `1.1` listing can tell it is stale.

    The distribution version moves for reasons that do not change the tool
    surface, so the surface carries its own. `1.1` was the six reads and no
    profile at all, which is why a second profile is a major bump rather than a
    minor one: a cached `1.1` listing is not a subset of what this advertises,
    it is the whole of one of two answers.
    """
    assert manifest.MANIFEST_VERSION == "2.0"


def test_the_purpose_vocabulary_is_the_services_own_per_operation() -> None:
    """The purpose is a claim the request states and the service checks against
    its own grant, so the claim has to be the one the grant allows -- a purpose
    invented here would be refused at the first call rather than caught by
    review. Five purposes across eleven tools, not one per operation."""
    purposes = {
        entry.operation: entry.purpose
        for entry in manifest.exposure_manifest("authoring")
    }
    assert purposes == {
        "workspace.inspect": "workspace_inspection",
        "evidence.search": "knowledge_retrieval",
        "knowledge.search": "knowledge_retrieval",
        "memory.search": "knowledge_retrieval",
        "graph.traverse": "knowledge_retrieval",
        "context_pack.build": "knowledge_retrieval",
        "memory.create": "memory_authoring",
        "evidence.capture": "content_ingestion",
        "import.start": "content_ingestion",
        "job.get": "job_observation",
        "job.events": "job_observation",
    }


def test_every_exposed_operation_is_in_the_landed_catalogue() -> None:
    """R004-06 forbids inventing identifiers, so each one must resolve."""
    for entry in manifest.exposure_manifest("authoring"):
        assert get_operation_metadata(entry.operation).name == entry.operation


def test_the_restricted_profile_is_read_only_throughout() -> None:
    for entry in manifest.exposure_manifest("restricted"):
        catalogue = get_operation_metadata(entry.operation)
        assert catalogue.scope.side_effect == "none", entry.operation
        assert catalogue.audit.audit_category == "read", entry.operation


def test_the_authoring_profile_adds_exactly_three_mutations_and_two_reads() -> None:
    """The exit criterion, read off the catalogue rather than off the tool names.

    Eight of the eleven declare no side effect and audit as reads; the other
    three are exactly the named mutations, each of which the catalogue agrees is
    a `create` audited as a `mutation`.
    """
    mutations, reads = set(), set()
    for entry in manifest.exposure_manifest("authoring"):
        catalogue = get_operation_metadata(entry.operation)
        if catalogue.scope.side_effect == "none":
            assert catalogue.audit.audit_category == "read", entry.operation
            reads.add(entry.operation)
        else:
            assert catalogue.scope.side_effect == "create", entry.operation
            assert catalogue.audit.audit_category == "mutation", entry.operation
            mutations.add(entry.operation)
    assert mutations == EXPECTED_MUTATIONS
    assert len(reads) == 8
    assert manifest.ADMITTED_MUTATIONS == EXPECTED_MUTATIONS


@pytest.mark.parametrize(
    "operation",
    ["record.supersede", "candidate.approve", "job.cancel", "workflow.start"],
)
def test_an_unreviewed_mutating_operation_cannot_be_admitted(operation: str) -> None:
    """Admission is a literal set of three, not a rule over catalogue metadata.

    An editor who adds `record.supersede` to a manifest does not ship a
    destructive tool with a reassuring docstring; the package refuses to import.
    Every one of these is a mutation the catalogue holds and no profile exposes.
    """
    with pytest.raises(ValueError, match="side_effect"):
        manifest._admit(
            manifest.ExposedOperation(
                tool_name=operation.replace(".", "_"),
                operation=operation,
                purpose="knowledge_governance",
                title="",
                description="",
            )
        )


@pytest.mark.parametrize("operation", sorted(EXPECTED_MUTATIONS))
def test_each_named_mutation_is_admitted_with_its_catalogue_entry(
    operation: str,
) -> None:
    """Admission returns the catalogue entry rather than a local opinion, so the
    scopes, capability, purpose and audit category a call is checked against are
    the catalogue's."""
    entry = manifest._admit(
        manifest.ExposedOperation(
            tool_name=operation.replace(".", "_"),
            operation=operation,
            purpose="content_ingestion",
            title="",
            description="",
        )
    )
    assert entry is get_operation_metadata(operation)
    assert entry.scope.side_effect != "none"


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
@pytest.mark.parametrize("profile", ["restricted", "authoring"])
def test_the_never_exposed_operations_are_absent(profile: str, operation: str) -> None:
    """Absent from both inventories and unreachable by tool name in either: the
    widest profile is still a curated eleven, not "everything but the worst"."""
    exposed = manifest.exposure_manifest(profile)
    assert operation not in {entry.operation for entry in exposed}
    assert manifest.exposed_by_tool_name(operation.replace(".", "_"), profile) is None


def test_every_catalogue_operation_outside_the_eleven_is_unreachable() -> None:
    """Stated over the whole catalogue rather than over a named list, so an
    operation registered after this was written is absent by default and has to
    be added to `EXPECTED_AUTHORING` to become callable."""
    exposed = {entry.operation for entry in manifest.exposure_manifest("authoring")}
    for entry in OPERATION_CATALOGUE:
        if entry.name in exposed:
            continue
        for profile in manifest.PROFILES:
            assert (
                manifest.exposed_by_tool_name(entry.name.replace(".", "_"), profile)
                is None
            ), entry.name


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


@pytest.mark.parametrize("profile", ["restricted", "authoring"])
def test_tools_list_is_deterministic_for_this_package_version(profile: str) -> None:
    """R004-06. Same object, same order, same content, on every call, and
    nothing about the listing read from the environment or from a call."""
    first, second = manifest.tools(profile), manifest.tools(profile)
    assert first is second
    assert [tool.name for tool in first] == [
        tool_name for tool_name, _, _ in EXPECTED_SURFACES[profile]
    ]
    assert [tool.model_dump(mode="json") for tool in first] == [
        tool.model_dump(mode="json") for tool in second
    ]


def test_the_two_listings_agree_byte_for_byte_on_the_tools_they_share() -> None:
    """An installation that turns authoring on does not silently redescribe the
    six read tools a host has already cached."""
    restricted = [tool.model_dump(mode="json") for tool in manifest.tools()]
    authoring = [tool.model_dump(mode="json") for tool in manifest.tools("authoring")]
    assert authoring[:6] == restricted


@pytest.mark.parametrize("profile", ["restricted", "authoring"])
def test_a_tool_name_absent_from_the_manifest_resolves_to_nothing(
    profile: str,
) -> None:
    """The allow-list is the only lookup the call path has."""
    for tool_name, _, _ in EXPECTED_SURFACES[profile]:
        assert manifest.exposed_by_tool_name(tool_name, profile) is not None
    for absent in (
        "workspace_create",
        "job_cancel",
        "core_readiness",
        "evidence.search",  # the operation identifier is not a tool name
        "EvidenceSearch",
        "",
    ):
        assert manifest.exposed_by_tool_name(absent, profile) is None


# --- the advertised schemas ---------------------------------------------------


def test_every_read_tool_advertises_the_canonical_input_schema_unwrapped() -> None:
    """The schema is *reached* from the catalogue entry, never transcribed here.

    The path is the entry's own `input_schema_ref` -> the generated projection
    keyed by that exact reference. A contract renamed upstream therefore fails
    here rather than advertising a stale shape. The eight reads -- `job_get` and
    `job_events` among them -- advertise that document directly: there is no
    wrapper on a read, because there is no idempotency key on one.

    `1.0`'s version of this test compared nothing to nothing: `input_schema`
    raised whenever the contract had fields, so on every input that reached the
    comparison both sides were `[]`, and it passed for every possible argument.
    Identity against the generated document is what makes it a check.
    """
    manifest_entries = manifest.exposure_manifest("authoring")
    for tool, entry in zip(manifest.tools("authoring"), manifest_entries, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        if entry.operation in EXPECTED_MUTATIONS:
            continue
        assert tool.input_schema == SCHEMAS[catalogue.input_schema_ref], entry.tool_name
        assert manifest.input_schema(catalogue) == tool.input_schema


def test_every_tool_advertises_the_canonical_result_schema() -> None:
    """`1.0` advertised no output schema at all, so a host could not validate what
    came back. The official client validates `structuredContent` against this
    document on every successful call, which is the whole reason it is here --
    and a mutation's result is the canonical operation result, unwrapped: the
    wrapper is a call shape, not a result shape."""
    manifest_entries = manifest.exposure_manifest("authoring")
    for tool, entry in zip(manifest.tools("authoring"), manifest_entries, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        assert tool.output_schema == SCHEMAS[catalogue.result_schema_ref]
        assert manifest.output_schema(catalogue) == tool.output_schema
        assert tool.output_schema is not None
        assert tool.output_schema["type"] == "object"


# --- the mutation wrapper -----------------------------------------------------


def _mutation_tools() -> list[Any]:
    by_name = {tool.name: tool for tool in manifest.tools("authoring")}
    return [
        by_name["memory_create"],
        by_name["evidence_capture"],
        by_name["import_start"],
    ]


@pytest.mark.parametrize("operation", sorted(EXPECTED_MUTATIONS))
def test_a_mutation_advertises_a_closed_two_field_wrapper(operation: str) -> None:
    """Exactly `input` and `idempotency_key`, both required, nothing else
    accepted. An unrecognised outer key is a caller trying to say something this
    seam does not accept -- an authority field, a workspace, a purpose -- and
    `additionalProperties: false` is what refuses it rather than ignoring it."""
    catalogue = get_operation_metadata(operation)
    schema = manifest.input_schema(catalogue)
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"input", "idempotency_key"}
    assert sorted(schema["required"]) == ["idempotency_key", "input"]
    assert schema["additionalProperties"] is False
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("operation", sorted(EXPECTED_MUTATIONS))
def test_the_wrapper_carries_the_generated_operation_input_under_input(
    operation: str,
) -> None:
    """`input` is the canonical operation input document and nothing else: the
    same generated projection a read would advertise directly, minus the two keys
    that cannot survive being nested -- its dialect declaration, which is a
    resource-root fact, and its `$defs` closure, which is hoisted to the wrapper
    root so the `#/$defs/...` references inside it keep resolving. Everything
    that describes the payload is identical, field for field."""
    catalogue = get_operation_metadata(operation)
    generated = SCHEMAS[catalogue.input_schema_ref]
    wrapper = manifest.input_schema(catalogue)

    assert wrapper["properties"]["input"] == {
        key: value
        for key, value in generated.items()
        if key not in {"$schema", "$defs"}
    }
    assert wrapper.get("$defs") == generated.get("$defs")
    assert "$schema" not in wrapper["properties"]["input"]
    assert "$defs" not in wrapper["properties"]["input"]


@pytest.mark.parametrize("operation", sorted(EXPECTED_MUTATIONS))
def test_the_wrapper_key_is_projected_from_the_canonical_contract(
    operation: str,
) -> None:
    """Not hand-copied. The advertised pattern, bounds and description are the
    request envelope's own `IdempotencyKey`, reached by the reference the
    manifest names and emitted by the generator like every other schema -- so a
    canonical loosening or tightening of the key reaches the wire through
    `--check` rather than through somebody remembering to retype it."""
    generated = SCHEMAS[manifest.IDEMPOTENCY_KEY_SCHEMA_REF]
    key = manifest.input_schema(get_operation_metadata(operation))["properties"][
        "idempotency_key"
    ]
    assert key == {
        name: value for name, value in generated.items() if name != "$schema"
    }
    assert key["type"] == "string"
    assert key["minLength"] >= 1
    assert "pattern" in key


def test_the_wrapper_is_closed_and_resolves_every_reference_locally() -> None:
    """The hoisted closure has to leave the document closed: every `#/$defs/...`
    a nested operation input carries must resolve at the wrapper's root, and the
    wrapper must declare no definition nothing points at."""
    for tool in _mutation_tools():
        schema = tool.input_schema
        declared = set(schema.get("$defs", {}))
        referenced = set()
        for reference in _local_refs(schema):
            assert reference.startswith("#/$defs/"), reference
            referenced.add(reference.removeprefix("#/$defs/"))
        assert referenced <= declared, f"{tool.name}: {sorted(referenced - declared)}"
        assert declared <= referenced, f"{tool.name}: {sorted(declared - referenced)}"
        assert CANONICAL_BASE not in repr(schema)


def test_a_mutation_wrapper_refuses_what_it_promises_to_refuse() -> None:
    """The advertised document, run by the same validator a host would use.

    A missing half, an empty key, and -- the ones that matter -- an outer
    authority field a caller has no business naming. `input` is checked against
    the operation's own schema through the hoisted closure, so an input that the
    contract requires fields of is refused when it is empty: that refusal is
    proof the nested references still resolve after the hoist.
    """
    jsonschema = pytest.importorskip("jsonschema")
    wrapper = manifest.input_schema(get_operation_metadata("import.start"))
    validator = jsonschema.Draft202012Validator(wrapper)
    for invalid in (
        {},  # neither property
        {"input": {}},  # no key
        {"idempotency_key": "import-001"},  # no input
        {"input": {}, "idempotency_key": ""},  # empty key
        {"input": {}, "idempotency_key": "k", "workspace_id": "ws_1"},  # extra outer
        {"input": {}, "idempotency_key": "k", "purpose": "content_ingestion"},
        {"input": "not-an-object", "idempotency_key": "k"},
        # `ImportStartInput` requires `source`; reaching that refusal means the
        # hoisted `$defs` closure resolved from the wrapper's root.
        {"input": {}, "idempotency_key": "import-001"},
    ):
        assert not validator.is_valid(invalid), invalid


def test_a_read_tool_advertises_no_idempotency_key() -> None:
    """The key belongs in the request envelope of a mutation. A read that
    advertised one would be inviting a caller to state something the operation
    does not support and the catalogue does not accept."""
    for entry in manifest.exposure_manifest("authoring"):
        if entry.operation in EXPECTED_MUTATIONS:
            continue
        schema = manifest.input_schema(get_operation_metadata(entry.operation))
        assert "idempotency_key" not in schema.get("properties", {}), entry.tool_name


def test_the_wrapper_is_deterministic() -> None:
    """Composed the same way every time: equal documents, key for key, in one
    order, so a host that diffs a cached listing sees a change only when the
    contract changed."""
    for operation in sorted(EXPECTED_MUTATIONS):
        catalogue = get_operation_metadata(operation)
        first = manifest.input_schema(catalogue)
        second = manifest.input_schema(catalogue)
        assert first == second
        assert list(first) == list(second)
        assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)


def test_composing_a_wrapper_does_not_mutate_the_generated_projection() -> None:
    """The generated module is shared by every tool and by the server's call-time
    validation; a wrapper that popped keys out of it in place would leave the
    second reader looking at a document missing its dialect and its closure."""
    before = json.dumps(SCHEMAS, sort_keys=True)
    for operation in sorted(EXPECTED_MUTATIONS):
        manifest.input_schema(get_operation_metadata(operation))
    assert json.dumps(SCHEMAS, sort_keys=True) == before


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
    the server's own pre-flight refusal a shortcut rather than a second policy.

    A read is closed the canonical document's way, with `unevaluatedProperties`;
    a mutation wrapper is closed the wrapper's way, with `additionalProperties`,
    and its nested `input` is still the canonical closed document.
    """
    for entry in manifest.exposure_manifest("authoring"):
        catalogue = get_operation_metadata(entry.operation)
        schema = manifest.input_schema(catalogue)
        if entry.operation in EXPECTED_MUTATIONS:
            assert schema["additionalProperties"] is False, entry.operation
            inner = schema["properties"]["input"]
            assert inner["unevaluatedProperties"] is False, entry.operation
            assert inner.get("additionalProperties") is not True, entry.operation
            continue
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
    """Every hint derived, none asserted.

    `readOnlyHint` is the catalogue's `side_effect == "none"` rather than a
    constant, so the three mutations say so. `destructiveHint` is false for all
    eleven, and truthfully: the three mutations create, and supersession and
    cancellation are not exposed at all. `idempotentHint` is the catalogue's
    proven `safe_to_retry` -- true for the eight reads, false for the three
    mutations, whose repeat is settled by the idempotency key rather than by the
    call being idempotent. The world is closed because this server is attached to
    exactly one local workspace it cannot be told to leave.
    """
    manifest_entries = manifest.exposure_manifest("authoring")
    for tool, entry in zip(manifest.tools("authoring"), manifest_entries, strict=True):
        catalogue = get_operation_metadata(entry.operation)
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.read_only_hint == (catalogue.scope.side_effect == "none")
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint == catalogue.idempotency.safe_to_retry
        assert annotations.open_world_hint is False
        assert annotations.title == entry.title


def test_the_annotations_land_where_the_requirements_say_they_must() -> None:
    """The same facts as literals, because "derived from the catalogue" is only
    reassuring if the values it derives are the reviewed ones: three mutations
    marked not read-only and not idempotent, eight reads marked read-only and
    idempotent, and nothing marked destructive."""
    hints = {
        tool.name: (
            tool.annotations.read_only_hint,
            tool.annotations.destructive_hint,
            tool.annotations.idempotent_hint,
        )
        for tool in manifest.tools("authoring")
        if tool.annotations is not None
    }
    assert len(hints) == 11
    mutations = {"memory_create", "evidence_capture", "import_start"}
    for mutation in mutations:
        assert hints[mutation] == (False, False, False), mutation
    for read in set(hints) - mutations:
        assert hints[read] == (True, False, True), read


def test_each_tool_records_the_contract_it_was_projected_from() -> None:
    manifest_entries = manifest.exposure_manifest("authoring")
    for tool, entry in zip(manifest.tools("authoring"), manifest_entries, strict=True):
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

    The *widest* profile, because the generated module is committed once and
    serves whichever profile an installation selects. It is a mirror rather than
    a read of the manifest because the generator runs with only `src/` on its
    path -- the MCP distribution need not be installed to regenerate -- and it is
    deliberately not derived from the catalogue: deriving it would make
    registering a Core operation enough to advertise it to a model, which is the
    one thing the curated manifest exists to prevent.
    """
    assert generator().EXPOSED_OPERATIONS == tuple(
        entry.operation for entry in manifest.exposure_manifest("authoring")
    )


def test_the_generator_projects_the_wrapper_key_the_manifest_names() -> None:
    """The second half of the same mirror. The idempotency key is the one
    advertised schema no operation names, so nothing else would make the
    generator emit it -- and the manifest looks it up by exactly this reference,
    so a mismatch is a `KeyError` at import rather than a wrapper with a
    hand-written key."""
    assert generator().WRAPPER_REFS == (manifest.IDEMPOTENCY_KEY_SCHEMA_REF,)
    assert manifest.IDEMPOTENCY_KEY_SCHEMA_REF in SCHEMAS


def test_the_generated_projection_covers_every_advertised_reference() -> None:
    """Nothing the eleven tools advertise is missing from the committed module,
    and nothing in it is advertised by no tool: a stale entry is as much a
    review problem as an absent one."""
    advertised = {manifest.IDEMPOTENCY_KEY_SCHEMA_REF}
    for entry in manifest.exposure_manifest("authoring"):
        catalogue = get_operation_metadata(entry.operation)
        advertised.update((catalogue.input_schema_ref, catalogue.result_schema_ref))
    assert set(SCHEMAS) == advertised


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


# --- the boundary this package exists inside ----------------------------------


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
