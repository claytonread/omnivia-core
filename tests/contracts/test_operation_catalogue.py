"""Tests for the Application Contract v1 operation catalogue (A2.5, ADR-038/ADR-039).

The catalogue is stated four times over, deliberately: as the canonical
``x-omnivia-operation-catalogue`` annotation, as the generated Python
``OPERATION_CATALOGUE``, as the generated TypeScript constant, and as the
hand-transcribed ``fixtures/operation-catalogue-v1.json`` regression oracle. Only
the first is a source; the next two are emitted from it and the last exists to
disagree when the annotation is edited without intent.

These tests treat the *fixture* as the expectation. Asserting against the
canonical annotation would only prove the generator copied it faithfully, which
the drift check already establishes; asserting against a table written here would
add a fifth restatement nobody maintains. Reading the frozen values from the one
artifact whose whole job is to be independently transcribed is what makes an
accidental catalogue edit fail rather than propagate.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1 import generated as generated_module
from omnivia_core.contracts.v1 import semantics_operations
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    DEFAULT_RETRY_CLASSIFICATION,
    FROZEN_ERROR_CODES,
    IDEMPOTENCY_KEY_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPERATION_CATALOGUE,
    ApiError,
    CapabilityRequirement,
    ClientIdentity,
    MutationPrecondition,
    OperationMetadata,
    RequestMetadata,
)
from omnivia_core.contracts.v1.semantics_operations import (
    get_operation_metadata,
    validate_operation_error,
    validate_operation_request_metadata,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README_PATH = REPO_ROOT / "README.md"
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "operation-catalogue-v1.json"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"
GENERATED_TS = REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"

#: The runtime probes. They are a separate contract with no catalogue metadata,
#: and `job.resume` was never introduced -- A2.4 folded resume into `job.retry`.
NON_OPERATIONS = ("service.health", "service.readiness", "service.discover", "job.resume")


def _load_fixture() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        document = json.load(handle)
    assert document["format"] == "omnivia.operation-catalogue.v1"
    operations: list[dict[str, Any]] = document["operations"]
    return operations


def _load_schema(name: str) -> dict[str, Any]:
    with (SCHEMA_DIR / f"{name}.schema.json").open(encoding="utf-8") as handle:
        document: dict[str, Any] = json.load(handle)
    return document


FROZEN = _load_fixture()
FROZEN_NAMES = [entry["name"] for entry in FROZEN]
#: ``(name, expected wire object)`` for the data-driven per-operation tests, so a
#: failure names the operation rather than an index into a list of twenty.
FROZEN_CASES = list(zip(FROZEN_NAMES, FROZEN))
FROZEN_BY_NAME = dict(FROZEN_CASES)


def _catalogue_wire() -> list[dict[str, Any]]:
    return [entry.to_wire() for entry in OPERATION_CATALOGUE]


def _metadata(
    *,
    workspace_id: str | None = "workspace-1",
    scopes: tuple[str, ...] = (),
    capabilities: tuple[CapabilityRequirement, ...] = (),
    idempotency_key: str | None = None,
    mutation_precondition: MutationPrecondition | None = None,
) -> RequestMetadata:
    return RequestMetadata(
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        api_version="1.0",
        client=ClientIdentity(id="omnivia.cli", version="1.4.2"),
        workspace_id=workspace_id,
        scopes=scopes,
        purpose="user_initiated",
        required_capabilities=capabilities,
        idempotency_key=idempotency_key,
        mutation_precondition=mutation_precondition,
    )


def _metadata_with(metadata: RequestMetadata, **changes: Any) -> RequestMetadata:
    return dataclasses.replace(metadata, **changes)


def _valid_metadata_for(name: str, entry: dict[str, Any]) -> RequestMetadata:
    """The minimal `RequestMetadata` the catalogue accepts for `name`."""
    return _metadata(
        workspace_id=("workspace-1" if entry["scope"]["scope_kind"] == "workspace" else None),
        scopes=tuple(entry["scope"]["required_scopes"]),
        capabilities=(
            CapabilityRequirement(
                id=entry["required_capability"]["id"],
                minimum_version=entry["required_capability"]["minimum_version"],
                required=True,
            ),
        ),
        idempotency_key=("idem-1" if entry["idempotency"]["required"] else None),
        mutation_precondition=(
            MutationPrecondition(record_version="rv-1")
            if entry["precondition"]["required"]
            else None
        ),
    )


# --------------------------------------------------------------------------
# Catalogue membership and the four representations
# --------------------------------------------------------------------------


def test_the_catalogue_holds_exactly_the_frozen_twenty_operations_in_order() -> None:
    assert len(OPERATION_CATALOGUE) == 20
    assert [entry.name for entry in OPERATION_CATALOGUE] == FROZEN_NAMES
    assert FROZEN_NAMES == sorted(FROZEN_NAMES)
    assert len(set(FROZEN_NAMES)) == 20


def test_two_operations_are_installation_scoped_and_eighteen_are_workspace_scoped() -> None:
    installation = [e.name for e in OPERATION_CATALOGUE if e.scope.scope_kind == "installation"]
    workspace = [e.name for e in OPERATION_CATALOGUE if e.scope.scope_kind == "workspace"]
    assert installation == ["workspace.create", "workspace.list"]
    assert len(workspace) == 18
    assert len(installation) + len(workspace) == 20


@pytest.mark.parametrize("name", NON_OPERATIONS)
def test_runtime_probes_and_job_resume_are_not_catalogue_operations(name: str) -> None:
    """A probe is not a product operation, and no fallback posture is invented for one.

    Returning some default scope, capability and error set for a name the
    catalogue does not define would be stating a contract nothing stands behind.
    """
    assert name not in FROZEN_NAMES
    with pytest.raises(ContractSemanticError, match="unknown application operation"):
        get_operation_metadata(name)


def test_the_fixture_the_annotation_and_the_generated_python_agree_exactly() -> None:
    canonical = _load_schema("operations")["x-omnivia-operation-catalogue"]
    assert canonical == FROZEN
    assert _catalogue_wire() == FROZEN


def _emitted_typescript_catalogue() -> list[dict[str, Any]]:
    """Parse the emitted TypeScript catalogue literal into plain Python values.

    Checking only the sequence of emitted ``name:`` lines would pass against an
    artifact whose every other field had drifted -- and against a 21st entry
    appended after the frozen twenty. The literal is delimited exactly rather
    than sliced to end-of-file, so a truncated or unterminated artifact fails
    here instead of being silently accepted.
    """
    source = GENERATED_TS.read_text(encoding="utf-8")
    opening = "export const OPERATION_CATALOGUE: readonly OperationMetadata[] = ["
    assert opening in source, "the TypeScript artifact does not declare a readonly catalogue"
    start = source.index(opening) + len(opening) - 1
    terminator = "\n] as const;"
    end = source.index(terminator, start) + len(terminator) - len(" as const;")
    body = source[start:end]

    # Only whole-line comments are stripped: `//` also occurs inside every
    # `https://` schema reference, and removing those would truncate the strings.
    body = re.sub(r"^\s*//.*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    parsed: list[dict[str, Any]] = json.loads(body)
    return parsed


def test_the_generated_typescript_states_the_same_catalogue() -> None:
    """The TypeScript artifact must carry the catalogue by value, not just by name."""
    emitted = _emitted_typescript_catalogue()
    assert emitted == FROZEN
    assert [entry["name"] for entry in emitted] == FROZEN_NAMES


def test_the_generated_typescript_keeps_the_catalogue_readonly() -> None:
    source = GENERATED_TS.read_text(encoding="utf-8")
    assert "export const OPERATION_CATALOGUE: readonly OperationMetadata[] = [" in source
    assert "readonly allowed_errors: readonly ErrorCode[]" in source


# --------------------------------------------------------------------------
# Schema bindings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_every_payload_reference_resolves_to_its_exact_definition(
    name: str, entry: dict[str, Any]
) -> None:
    refs = [entry["input_schema_ref"], entry["result_schema_ref"]]
    if "terminal_result_schema_ref" in entry["job"]:
        refs.append(entry["job"]["terminal_result_schema_ref"])
    for ref in refs:
        assert ref.startswith(BASE_URI), f"{name}: {ref} is not an absolute in-contract reference"
        document, _, pointer = ref.partition("#/$defs/")
        schema_name = document[len(BASE_URI) :].removesuffix(".schema.json")
        defs = _load_schema(schema_name).get("$defs", {})
        assert pointer in defs, f"{name}: {ref} names no definition in {schema_name}.schema.json"


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_every_operation_matches_its_frozen_metadata_exactly(
    name: str, entry: dict[str, Any]
) -> None:
    """Whole-object equality, including the fully materialized allowed-error array."""
    assert get_operation_metadata(name).to_wire() == entry


# --------------------------------------------------------------------------
# Wire behaviour
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_catalogue_entries_round_trip_through_the_generated_codec(
    name: str, entry: dict[str, Any]
) -> None:
    metadata = get_operation_metadata(name)
    assert OperationMetadata.from_wire(metadata.to_wire()) == metadata


def test_an_unknown_optional_field_decodes_tolerantly() -> None:
    """ADR-038's tolerant-decode rule holds for `OperationMetadata` too.

    A future contract version may add an optional field to a catalogue entry. A
    build that predates it must ignore that field rather than refuse the
    document, or every consumer would have to upgrade in lockstep with the
    contract.
    """
    wire = dict(get_operation_metadata("memory.get").to_wire())
    wire["x_future_hint"] = {"added": "by a later contract version"}
    assert OperationMetadata.from_wire(wire) == get_operation_metadata("memory.get")


# --------------------------------------------------------------------------
# Posture invariants, stated over the whole catalogue
# --------------------------------------------------------------------------


def test_import_start_is_the_only_durable_job_starting_operation() -> None:
    asynchronous = [e for e in OPERATION_CATALOGUE if e.job.completion_mode != "synchronous"]
    assert [e.name for e in asynchronous] == ["import.start"]
    entry = asynchronous[0]
    assert entry.job.completion_mode == "always_returns_job"
    assert entry.job.job_kind == "ingestion.import"
    assert entry.job.terminal_result_schema_ref == (
        f"{BASE_URI}jobs.schema.json#/$defs/ImportCompletionResult"
    )


def test_may_return_job_does_not_occur_in_the_v1_catalogue() -> None:
    assert all(e.job.completion_mode != "may_return_job" for e in OPERATION_CATALOGUE)


def test_synchronous_operations_omit_both_optional_job_fields() -> None:
    for entry in OPERATION_CATALOGUE:
        if entry.job.completion_mode != "synchronous":
            continue
        assert entry.job.job_kind is None, entry.name
        assert entry.job.terminal_result_schema_ref is None, entry.name
        assert set(entry.to_wire()["job"]) == {"completion_mode"}, entry.name


def test_exactly_seven_operations_are_paginated_at_a_maximum_page_size_of_1000() -> None:
    paginated = [e for e in OPERATION_CATALOGUE if e.pagination.paginated]
    assert [e.name for e in paginated] == [
        "evidence.search",
        "graph.traverse",
        "job.events",
        "knowledge.search",
        "memory.list",
        "memory.search",
        "workspace.list",
    ]
    assert all(e.pagination.max_page_size == 1000 for e in paginated)
    for entry in OPERATION_CATALOGUE:
        if not entry.pagination.paginated:
            assert "max_page_size" not in entry.to_wire()["pagination"], entry.name


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_idempotency_posture_follows_the_side_effect(name: str, entry: dict[str, Any]) -> None:
    """A read is safe to retry and takes no key; a mutation requires one and is not.

    The two are not independent settings: a mutation that were "safe to retry"
    without a key would be a contract promising exactly the duplicate-write
    behaviour the key exists to prevent.
    """
    metadata = get_operation_metadata(name)
    read = entry["scope"]["side_effect"] == "none"
    assert metadata.idempotency.supports_idempotency_key is (not read)
    assert metadata.idempotency.required is (not read)
    assert metadata.idempotency.safe_to_retry is read
    if metadata.idempotency.required:
        assert metadata.idempotency.supports_idempotency_key
        assert not metadata.idempotency.safe_to_retry


def test_exactly_four_governance_transitions_require_a_mutation_precondition() -> None:
    supported = [e.name for e in OPERATION_CATALOGUE if e.precondition.supports_mutation_precondition]
    required = [e.name for e in OPERATION_CATALOGUE if e.precondition.required]
    expected = ["candidate.approve", "candidate.reject", "knowledge.propose", "record.supersede"]
    assert supported == expected
    assert required == expected


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_every_operation_is_audited_under_the_category_its_side_effect_implies(
    name: str, entry: dict[str, Any]
) -> None:
    metadata = get_operation_metadata(name)
    assert metadata.audit.audited is True
    expected = "read" if entry["scope"]["side_effect"] == "none" else "mutation"
    assert metadata.audit.audit_category == expected


# --------------------------------------------------------------------------
# Allowed errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_allowed_errors_are_sorted_unique_known_and_exactly_frozen(
    name: str, entry: dict[str, Any]
) -> None:
    allowed = list(get_operation_metadata(name).allowed_errors)
    assert allowed == entry["allowed_errors"]
    assert allowed == sorted(allowed)
    assert len(set(allowed)) == len(allowed)
    assert allowed, f"{name}: allowed_errors must not be empty"
    assert set(allowed) <= set(FROZEN_ERROR_CODES)
    assert "invalid_request" in allowed


def test_invalid_request_is_the_canonical_non_retryable_outcome() -> None:
    assert DEFAULT_RETRY_CLASSIFICATION["invalid_request"] == "non_retryable"
    assert all("invalid_request" in e.allowed_errors for e in OPERATION_CATALOGUE)


@pytest.mark.parametrize("name", ["job.cancel", "job.retry"])
def test_job_control_operations_do_not_allow_conflict(name: str) -> None:
    """State-based refusal is a successful disposition, not a conflict error.

    `job.cancel` on an already-terminal job returns `not_cancellable` with the
    unchanged handle. Reporting that as `conflict` would tell a caller to retry
    something that will never succeed.
    """
    assert "conflict" not in get_operation_metadata(name).allowed_errors


# --------------------------------------------------------------------------
# `validate_operation_request_metadata`
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_minimal_valid_request_metadata_is_accepted(name: str, entry: dict[str, Any]) -> None:
    assert validate_operation_request_metadata(name, _valid_metadata_for(name, entry)) is (
        get_operation_metadata(name)
    )


@pytest.mark.parametrize("name", ["workspace.create", "workspace.list"])
def test_installation_scoped_operations_must_not_carry_a_workspace_id(name: str) -> None:
    entry = FROZEN_BY_NAME[name]
    valid = _valid_metadata_for(name, entry)
    assert validate_operation_request_metadata(name, valid).name == name
    with pytest.raises(ContractSemanticError, match="must not carry"):
        validate_operation_request_metadata(
            name, _metadata_with(valid, workspace_id="workspace-1")
        )


@pytest.mark.parametrize("name", ["memory.get", "job.events", "context_pack.build"])
def test_workspace_scoped_operations_require_a_workspace_id(name: str) -> None:
    entry = FROZEN_BY_NAME[name]
    valid = _valid_metadata_for(name, entry)
    assert validate_operation_request_metadata(name, valid).name == name
    with pytest.raises(ContractSemanticError, match="require a non-empty"):
        validate_operation_request_metadata(name, _metadata_with(valid, workspace_id=None))


def test_a_missing_required_scope_is_rejected() -> None:
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    with pytest.raises(ContractSemanticError, match="requires scope"):
        validate_operation_request_metadata("memory.get", _metadata_with(valid, scopes=()))


def test_extra_scopes_do_not_widen_authority_and_are_left_alone() -> None:
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    widened = _metadata_with(valid, scopes=(*valid.scopes, "graph:read"))
    assert validate_operation_request_metadata("memory.get", widened).name == "memory.get"


def _capability(id_: str, version: str = "1.0", *, required: bool = True) -> CapabilityRequirement:
    return CapabilityRequirement(id=id_, minimum_version=version, required=required)


def test_a_missing_required_capability_declaration_is_rejected() -> None:
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    with pytest.raises(ContractSemanticError, match="exactly one"):
        validate_operation_request_metadata(
            "memory.get", _metadata_with(valid, required_capabilities=())
        )


def test_an_optional_capability_declaration_is_rejected() -> None:
    """Declaring the capability optional says the caller will degrade without it.

    For a catalogue-required capability there is no degraded mode to fall back
    to, so the claim is refused rather than quietly upgraded to required.
    """
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    with pytest.raises(ContractSemanticError, match="`required`"):
        validate_operation_request_metadata(
            "memory.get",
            _metadata_with(
                valid, required_capabilities=(_capability("memory.read", required=False),)
            ),
        )


def test_a_duplicate_capability_id_is_rejected() -> None:
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    with pytest.raises(ContractSemanticError, match="duplicate capability id"):
        validate_operation_request_metadata(
            "memory.get",
            _metadata_with(
                valid,
                required_capabilities=(
                    _capability("memory.read", "1.0"),
                    _capability("memory.read", "1.1"),
                ),
            ),
        )


def test_a_below_minimum_capability_version_is_rejected() -> None:
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    with pytest.raises(ContractSemanticError, match="or stricter"):
        validate_operation_request_metadata(
            "memory.get",
            _metadata_with(valid, required_capabilities=(_capability("memory.read", "0.9"),)),
        )


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.10", "2.0"])
def test_an_equal_or_stricter_capability_version_is_accepted(version: str) -> None:
    """Compared as contract versions, not lexically: `1.10` is above `1.9`, not below."""
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    accepted = _metadata_with(valid, required_capabilities=(_capability("memory.read", version),))
    assert validate_operation_request_metadata("memory.get", accepted).name == "memory.get"


def test_an_extra_uniquely_named_capability_requirement_is_left_in_place() -> None:
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    extended = _metadata_with(
        valid, required_capabilities=(*valid.required_capabilities, _capability("graph.read"))
    )
    assert validate_operation_request_metadata("memory.get", extended).name == "memory.get"


def test_a_required_idempotency_key_must_be_present() -> None:
    entry = FROZEN_BY_NAME["memory.create"]
    valid = _valid_metadata_for("memory.create", entry)
    with pytest.raises(ContractSemanticError, match="requires RequestMetadata.idempotency_key"):
        validate_operation_request_metadata(
            "memory.create", _metadata_with(valid, idempotency_key=None)
        )


def test_an_idempotency_key_supplied_to_a_read_is_rejected() -> None:
    """Silently ignoring it would let the caller believe the call is replay-safe."""
    entry = FROZEN_BY_NAME["memory.get"]
    valid = _valid_metadata_for("memory.get", entry)
    with pytest.raises(ContractSemanticError, match="does not honour"):
        validate_operation_request_metadata(
            "memory.get", _metadata_with(valid, idempotency_key="idem-1")
        )


def test_a_required_mutation_precondition_must_be_present() -> None:
    entry = FROZEN_BY_NAME["record.supersede"]
    valid = _valid_metadata_for("record.supersede", entry)
    with pytest.raises(ContractSemanticError, match="requires RequestMetadata.mutation_precondition"):
        validate_operation_request_metadata(
            "record.supersede", _metadata_with(valid, mutation_precondition=None)
        )


def test_a_mutation_precondition_supplied_to_a_non_supporting_operation_is_rejected() -> None:
    entry = FROZEN_BY_NAME["memory.create"]
    valid = _valid_metadata_for("memory.create", entry)
    with pytest.raises(ContractSemanticError, match="does not honour"):
        validate_operation_request_metadata(
            "memory.create",
            _metadata_with(valid, mutation_precondition=MutationPrecondition(record_version="rv-1")),
        )


# --------------------------------------------------------------------------
# `validate_operation_error`
# --------------------------------------------------------------------------


def _api_error(code: str) -> ApiError:
    return ApiError(code=code, message="boom", retry_class=DEFAULT_RETRY_CLASSIFICATION[code])


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_every_operation_accepts_each_error_on_its_allow_list(
    name: str, entry: dict[str, Any]
) -> None:
    for code in entry["allowed_errors"]:
        validate_operation_error(name, _api_error(code))


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_every_operation_rejects_a_known_but_disallowed_error(
    name: str, entry: dict[str, Any]
) -> None:
    disallowed = sorted(set(FROZEN_ERROR_CODES) - set(entry["allowed_errors"]))
    assert disallowed, f"{name}: needs at least one known code outside its allow-list"
    for code in disallowed:
        with pytest.raises(ContractSemanticError, match="not permitted to fail with"):
            validate_operation_error(name, _api_error(code))


def test_an_unknown_error_code_is_reported_against_the_operation_allow_list() -> None:
    """The allow-list is checked before the retry-class rule.

    A code this build has never seen is not a retry-class mismatch; it is an
    error the operation is not permitted to raise, and saying so is the more
    useful of the two reports.
    """
    error = ApiError(code="not_a_real_code", message="boom", retry_class="non_retryable")
    with pytest.raises(ContractSemanticError, match="not permitted to fail with"):
        validate_operation_error("memory.get", error)


def test_a_mismatched_retry_class_on_an_allowed_code_stays_a_retry_class_violation() -> None:
    """A distinct protocol violation from raising a disallowed error; not collapsed."""
    from omnivia_core.contracts.v1.codec import RetryClassMismatchError

    error = ApiError(code="not_found", message="boom", retry_class="retryable")
    with pytest.raises(RetryClassMismatchError):
        validate_operation_error("memory.get", error)


# --------------------------------------------------------------------------
# Direct-entry type guards
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 42, ["memory.get"], b"memory.get", True])
def test_a_wrongly_typed_operation_name_fails_semantically(value: object) -> None:
    with pytest.raises(ContractSemanticError):
        get_operation_metadata(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, 42, {"scopes": ()}, "not-metadata"])
def test_wrongly_typed_request_metadata_fails_semantically(value: object) -> None:
    """A hand-built value must not leak `AttributeError` or `TypeError` out of here."""
    with pytest.raises(ContractSemanticError):
        validate_operation_request_metadata("memory.get", value)


@pytest.mark.parametrize("value", [None, 42, {"code": "not_found"}, "not_found"])
def test_a_wrongly_typed_error_fails_semantically(value: object) -> None:
    with pytest.raises(ContractSemanticError):
        validate_operation_error("memory.get", value)


def test_unknown_operations_are_rejected_by_every_public_entry_point() -> None:
    metadata = _metadata(scopes=("memory:read",))
    with pytest.raises(ContractSemanticError, match="unknown application operation"):
        validate_operation_request_metadata("memory.no_such_operation", metadata)
    with pytest.raises(ContractSemanticError, match="unknown application operation"):
        validate_operation_error("memory.no_such_operation", _api_error("not_found"))


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------


def test_the_catalogue_is_exported_from_the_generated_module() -> None:
    assert "OPERATION_CATALOGUE" in generated_module.__all__
    assert isinstance(OPERATION_CATALOGUE, tuple)
    assert all(isinstance(entry, OperationMetadata) for entry in OPERATION_CATALOGUE)


def test_catalogue_collections_are_immutable() -> None:
    """Every collection field is a tuple, so no caller can edit the frozen catalogue."""
    for entry in OPERATION_CATALOGUE:
        assert isinstance(entry.allowed_errors, tuple), entry.name
        assert isinstance(entry.scope.required_scopes, tuple), entry.name


def test_lookup_returns_the_catalogue_object_itself_not_a_copy() -> None:
    """The index is an index, not a second catalogue that could drift from the first."""
    for entry in OPERATION_CATALOGUE:
        assert get_operation_metadata(entry.name) is entry


# --------------------------------------------------------------------------
# Schema value domains for the two values a request supplies itself
#
# `IdempotencyKey` and the precondition's `OpaqueToken` record version are the
# only catalogue-governed values a caller *writes*, and nothing upstream of
# `validate_operation_request_metadata` constrains either: `from_wire` is
# deliberately tolerant and checks only that a value is a string, and a
# hand-built frozen dataclass checks nothing whatsoever. A malformed value that
# passes here is one a server will reject later -- after the mutation has
# already been sent under a key that cannot match its own retry.
# --------------------------------------------------------------------------

#: `memory.create` requires an idempotency key and forbids a precondition;
#: `record.supersede` requires both. Together they exercise every path.
_KEYED_OPERATION = "memory.create"
_PRECONDITION_OPERATION = "record.supersede"


def _with_idempotency_key(key: object) -> RequestMetadata:
    valid = _valid_metadata_for(_KEYED_OPERATION, FROZEN_BY_NAME[_KEYED_OPERATION])
    return _metadata_with(valid, idempotency_key=key)


def _with_record_version(version: object) -> RequestMetadata:
    valid = _valid_metadata_for(_PRECONDITION_OPERATION, FROZEN_BY_NAME[_PRECONDITION_OPERATION])
    return _metadata_with(
        valid, mutation_precondition=MutationPrecondition(record_version=version)
    )


def test_the_value_domains_are_the_canonical_generated_ones() -> None:
    """Compiled from the generated pattern constants, not from a second copy.

    A regex re-typed into the semantics module would keep passing its own tests
    long after the canonical pattern moved, which is the drift these constants
    exist to make impossible.
    """
    assert semantics_operations._IDEMPOTENCY_KEY_RE.pattern == IDEMPOTENCY_KEY_PATTERN
    assert semantics_operations._OPAQUE_TOKEN_RE.pattern == OPAQUE_TOKEN_PATTERN

    common = _load_schema("common")["$defs"]
    assert semantics_operations._IDEMPOTENCY_KEY_MAX_LENGTH == common["IdempotencyKey"]["maxLength"]
    assert semantics_operations._OPAQUE_TOKEN_MAX_LENGTH == common["OpaqueToken"]["maxLength"]
    assert common["IdempotencyKey"]["pattern"] == IDEMPOTENCY_KEY_PATTERN
    assert common["OpaqueToken"]["pattern"] == OPAQUE_TOKEN_PATTERN


VALID_IDEMPOTENCY_KEYS: list[str] = [
    "a",
    "Z",
    "0",
    "idem-1",
    "idem.1",
    "idem_1",
    "idem:1",
    "A1.b_c:d-e",
    "0123456789",
    "a" * 128,  # exactly `maxLength`
]

INVALID_IDEMPOTENCY_KEYS: list[str] = [
    "",  # `minLength` 1
    " ",
    "   ",
    "\t",
    "\n",
    "-idem-1",  # the first character is drawn from a narrower class
    ".idem-1",
    "_idem-1",
    ":idem-1",
    "idem 1",  # an interior space
    "idem/1",
    "idem+1",
    "idem-1\n",  # `$` matches before a final newline, `fullmatch` does not
    "idem-1\r\n",
    "idem-1\r",
    "\nidem-1",
    "idem\n1",
    "idém-1",  # non-ASCII
    "idem-✓",
    " idem",  # a non-breaking space is not whitespace to `[A-Za-z0-9]`
    "a" * 129,  # one past `maxLength`
    "a" * 1000,
]


@pytest.mark.parametrize("key", VALID_IDEMPOTENCY_KEYS)
def test_a_schema_valid_idempotency_key_is_accepted(key: str) -> None:
    assert validate_operation_request_metadata(
        _KEYED_OPERATION, _with_idempotency_key(key)
    ).name == _KEYED_OPERATION


@pytest.mark.parametrize("key", INVALID_IDEMPOTENCY_KEYS, ids=[repr(k) for k in INVALID_IDEMPOTENCY_KEYS])
def test_an_idempotency_key_outside_the_schema_domain_is_rejected(key: str) -> None:
    """As a contract violation, never as an incidental regex or type error."""
    with pytest.raises(ContractSemanticError, match="is not a valid IdempotencyKey"):
        validate_operation_request_metadata(_KEYED_OPERATION, _with_idempotency_key(key))


VALID_RECORD_VERSIONS: list[str] = [
    "a",
    "!",  # the bottom of `[!-~]`
    "~",  # the top of it
    "rv-1",
    "eyJhIjoiYiJ9.=+/",  # an opaque server-issued token is not an identifier
    "0",
    "a" * 512,  # exactly `maxLength`
]

INVALID_RECORD_VERSIONS: list[str] = [
    "",
    " ",
    "   ",
    "\t",
    "\n",
    "rv 1",  # a space is outside `[!-~]`
    "rv-1\n",
    "rv-1\r\n",
    "rv-1\r",
    "\nrv-1",
    "rv\n1",
    "rv-é",
    "rv-✓",
    " rv",
    "a" * 513,  # one past `maxLength`
    "a" * 5000,
]


@pytest.mark.parametrize("version", VALID_RECORD_VERSIONS)
def test_a_schema_valid_record_version_is_accepted(version: str) -> None:
    assert validate_operation_request_metadata(
        _PRECONDITION_OPERATION, _with_record_version(version)
    ).name == _PRECONDITION_OPERATION


@pytest.mark.parametrize("version", INVALID_RECORD_VERSIONS, ids=[repr(v) for v in INVALID_RECORD_VERSIONS])
def test_a_record_version_outside_the_schema_domain_is_rejected(version: str) -> None:
    with pytest.raises(ContractSemanticError, match="is not a valid OpaqueToken"):
        validate_operation_request_metadata(_PRECONDITION_OPERATION, _with_record_version(version))


@pytest.mark.parametrize("value", [42, True, 1.5, None, b"idem-1", ["idem-1"], {"key": "idem-1"}])
def test_a_wrongly_typed_idempotency_key_fails_semantically(value: object) -> None:
    """A hand-built dataclass can hold anything; `None` here means "absent", and this
    operation requires a key, so every one of these is a `ContractSemanticError`.
    """
    with pytest.raises(ContractSemanticError):
        validate_operation_request_metadata(_KEYED_OPERATION, _with_idempotency_key(value))


@pytest.mark.parametrize("value", [42, True, 1.5, None, b"rv-1", ["rv-1"], {"v": "rv-1"}])
def test_a_wrongly_typed_record_version_fails_semantically(value: object) -> None:
    with pytest.raises(ContractSemanticError):
        validate_operation_request_metadata(_PRECONDITION_OPERATION, _with_record_version(value))


def test_a_malformed_key_is_rejected_even_where_the_operation_honours_no_key() -> None:
    """The value domain is judged before the posture.

    An operation that does not honour a key rejects the request either way, but
    reporting the malformed value first is the more useful of the two: the caller
    has written a key that no operation would ever accept.
    """
    valid = _valid_metadata_for("memory.get", FROZEN_BY_NAME["memory.get"])
    with pytest.raises(ContractSemanticError, match="is not a valid IdempotencyKey"):
        validate_operation_request_metadata(
            "memory.get", _metadata_with(valid, idempotency_key="idem 1")
        )


def test_a_malformed_record_version_is_rejected_even_where_unsupported() -> None:
    valid = _valid_metadata_for("memory.create", FROZEN_BY_NAME["memory.create"])
    with pytest.raises(ContractSemanticError, match="is not a valid OpaqueToken"):
        validate_operation_request_metadata(
            "memory.create",
            _metadata_with(valid, mutation_precondition=MutationPrecondition(record_version="rv 1")),
        )


@pytest.mark.parametrize(("name", "entry"), FROZEN_CASES, ids=FROZEN_NAMES)
def test_the_value_domains_hold_for_every_operation_that_takes_the_value(
    name: str, entry: dict[str, Any]
) -> None:
    """Not just the two representative operations: the rule belongs to the value, so
    every operation that can carry one must enforce it.
    """
    valid = _valid_metadata_for(name, entry)
    if entry["idempotency"]["supports_idempotency_key"]:
        with pytest.raises(ContractSemanticError, match="is not a valid IdempotencyKey"):
            validate_operation_request_metadata(
                name, _metadata_with(valid, idempotency_key="idem-1\n")
            )
    if entry["precondition"]["supports_mutation_precondition"]:
        with pytest.raises(ContractSemanticError, match="is not a valid OpaqueToken"):
            validate_operation_request_metadata(
                name,
                _metadata_with(
                    valid, mutation_precondition=MutationPrecondition(record_version="rv-1\n")
                ),
            )


def test_the_frozen_valid_metadata_still_passes_unchanged() -> None:
    """A regression guard for the value-domain rules themselves: they must reject
    malformed values without narrowing what the catalogue already accepts.
    """
    for name, entry in FROZEN_CASES:
        assert validate_operation_request_metadata(name, _valid_metadata_for(name, entry)).name == name


# --------------------------------------------------------------------------
# Documentation drift
# --------------------------------------------------------------------------


def _documented_operations(anchor: str) -> list[str]:
    """The names inside the fenced block the README publishes under `anchor`.

    Scoped to that one block deliberately. The surrounding prose names the
    runtime probes and `job.resume` precisely in order to say they are *not*
    catalogue operations, so a README-wide scan would either have to special-case
    them or would assert the opposite of what the document says.
    """
    text = README_PATH.read_text(encoding="utf-8")
    start = text.index(anchor)
    fence = text.index("```text", start) + len("```text")
    end = text.index("```", fence)
    return text[fence:end].split()


def test_the_readme_publishes_exactly_the_frozen_catalogue() -> None:
    """The README is the only place the operation list is written out in prose, so
    it is the one representation nothing else can catch drifting.
    """
    installation = _documented_operations("Two are installation-scoped:")
    workspace = _documented_operations("Eighteen are workspace-scoped:")
    documented = installation + workspace

    assert sorted(documented) == FROZEN_NAMES
    assert len(documented) == len(set(documented)) == 20
    assert installation == [
        entry.name for entry in OPERATION_CATALOGUE if entry.scope.scope_kind == "installation"
    ]
    assert sorted(workspace) == [
        entry.name for entry in OPERATION_CATALOGUE if entry.scope.scope_kind == "workspace"
    ]


@pytest.mark.parametrize("name", NON_OPERATIONS)
def test_the_readme_operation_list_names_no_probe_and_no_job_resume(name: str) -> None:
    documented = _documented_operations("Two are installation-scoped:") + _documented_operations(
        "Eighteen are workspace-scoped:"
    )
    assert name not in documented
