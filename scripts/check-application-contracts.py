#!/usr/bin/env python3
"""Verify the Application Contract v1 conformance gate (ADR-038).

This is the strict half of the pair described in
``scripts/generate-application-contracts.py``'s module docstring: that script
emits the *tolerant* production codec, this one enforces everything a
tolerant decoder deliberately does not.

Checks performed, entirely offline (no network access, no installed package
required beyond the ``jsonschema``/``referencing`` dev dependency):

- ``contracts/application/v1/schemas`` holds exactly the frozen schema
  documents -- none missing, and none extra that no check here would ever open
  but the wheel would still package;
- every schema document under ``contracts/application/v1/schemas`` is a valid
  Draft 2020-12 schema;
- the reference-only registry (``application-v1.schema.json``) publishes
  exactly the definitions the canonical source schemas declare, no more
  and no fewer;
- the fixture manifest's ``contract_version`` matches the registry's
  ``x-omnivia-contract-version`` exactly;
- every fixture listed in ``contracts/application/v1/fixtures/manifest.json``
  exists, matches its declared schema-validity, and satisfies its declared
  semantic expectation (version/capability negotiation math, retry
  fail-safety, tolerant decode of an otherwise-invalid document, and so on);
- the canonical ``x-omnivia-operation-catalogue`` annotation holds exactly the
  frozen 20 application operations, in the frozen order, each strictly valid
  against ``OperationMetadata``, binding resolvable in-contract payload
  references, and carrying exactly the frozen scope, capability, completion,
  pagination, idempotency, precondition, audit and allowed-error posture -- with
  the independent ``tests/contracts/fixtures/operation-catalogue-v1.json``
  regression oracle agreeing with it entry for entry;
- ``src/omnivia_core/contracts`` has no import outside the standard library
  or its own package -- no runtime, storage, HTTP, MCP, CLI, Platform, Dev,
  or validation-framework dependency;
- the generated Python and TypeScript artifacts are not out of date with the
  checked-in schemas (delegates to ``generate-application-contracts.py``'s
  own ``--check`` logic).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"
CONTRACTS_SRC = REPO_ROOT / "src" / "omnivia_core" / "contracts"
SRC_ROOT = REPO_ROOT / "src"
GENERATOR_SCRIPT = REPO_ROOT / "scripts" / "generate-application-contracts.py"
OPERATION_CATALOGUE_FIXTURE = (
    REPO_ROOT / "tests" / "contracts" / "fixtures" / "operation-catalogue-v1.json"
)
ADAPTER_CONFORMANCE_CORPUS = "application-wire-adapter-conformance-v1.json"

#: Packaged fixture files that are deliberately *not* canonical example wire
#: documents, and so are not governed by ``fixtures/manifest.json``.
#:
#: The manifest describes one wire envelope per entry -- which envelope, which
#: response branch, whether it is schema-valid, which semantic expectation it
#: carries. The adapter conformance corpus is a different kind of artifact: a
#: transcript of many recorded exchanges, with no single envelope, branch, or
#: semantic of its own. Registering it there would mean answering those questions
#: about a document they do not apply to. It ships in the same directory because
#: every adapter must be able to read it from an installed wheel, and it is
#: governed instead by ``check_adapter_conformance_corpus`` below -- so it is
#: checked as strictly as any manifest entry, just against the right rules.
_NON_WIRE_FIXTURE_FILES: frozenset[str] = frozenset(
    {"manifest.json", ADAPTER_CONFORMANCE_CORPUS}
)

SOURCE_SCHEMAS: tuple[str, ...] = (
    "common",
    "compatibility",
    "errors",
    "envelopes",
    "service",
    "records",
    "jobs",
    "operations",
    "workspace",
    "memory",
    "evidence",
    "knowledge",
    "graph",
    "context-pack",
    "compatibility-matrix",
)
REGISTRY_SCHEMA = "application-v1"
ALL_SCHEMAS: tuple[str, ...] = (*SOURCE_SCHEMAS, REGISTRY_SCHEMA)

BASE_URI = "https://contracts.omnivia.dev/application/v1/"
REQUEST_ENVELOPE_REF = f"{BASE_URI}envelopes.schema.json#/$defs/RequestEnvelope"
RESPONSE_ENVELOPE_REF = f"{BASE_URI}envelopes.schema.json#/$defs/ResponseEnvelope"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_ALLOWED_CONTRACTS_IMPORT_PREFIX = "omnivia_core.contracts"

# Frozen mapping of fixture id -> (file, semantic, schema_valid, tolerant_decode). Adding a
# new canonical fixture means adding an entry here in the same change: this is what makes
# deleting, renaming, or swapping an existing fixture's semantic assertion fail loudly
# instead of silently staying green.
FROZEN_FIXTURE_MAP: dict[str, tuple[str, str, bool, bool]] = {
    "compatible-negotiation": (
        "compatible-negotiation.json",
        "effective_capabilities_match",
        True,
        True,
    ),
    "capability-denial": ("capability-denial.json", "capability_denial", True, True),
    "incompatible-major": ("incompatible-major.json", "incompatible_major_version", True, True),
    "minimal-request": ("minimal-request.json", "minimal_required_fields_only", True, True),
    "retryable-mutation": (
        "retryable-mutation.json",
        "retryable_after_precondition_refresh",
        True,
        True,
    ),
    "success-response": (
        "success-response.json",
        "all_optional_response_metadata_present",
        True,
        True,
    ),
    "error-response": ("error-response.json", "plain_not_found", True, True),
    "additive-unknown-optional-field": (
        "additive-unknown-optional-field.json",
        "tolerant_decode_ignores_unknown_field",
        False,
        True,
    ),
    "unknown-open-value": (
        "unknown-open-value.json",
        "unrecognized_retry_class_fails_safe",
        True,
        True,
    ),
    "duplicate-capability-ids": (
        "duplicate-capability-ids.json",
        "duplicate_capability_id_rejected",
        True,
        False,
    ),
    "both-result-and-error": (
        "both-result-and-error.json",
        "ambiguous_response_branch_rejected",
        False,
        False,
    ),
    "neither-result-nor-error": (
        "neither-result-nor-error.json",
        "ambiguous_response_branch_rejected",
        False,
        False,
    ),
    "calendar-invalid-timestamp": (
        "calendar-invalid-timestamp.json",
        "calendar_invalid_timestamp_tolerated",
        False,
        True,
    ),
}


# Frozen negotiated facts the ``compatible-negotiation`` fixture exists to demonstrate.
# The fixture is the canonical worked example of a *successful* v1 negotiation, so every
# choice that makes it that example is frozen here, not just the capability intersection:
# a fixture that quietly drifted to a different selected version, a wider supported
# window, or a non-empty upgrade posture would otherwise keep passing under the same
# semantic key. Only the negotiated *choices* are frozen. Facts the fixture already
# determines from its own data -- above all the effective capability set, which must be
# exactly ``supported`` intersected with ``granted`` -- stay computed from the fixture, so
# this map never becomes a second, contradictable copy of them.
COMPATIBLE_NEGOTIATION_EXPECTED: dict[str, Any] = {
    "status": "compatible",
    "selected_api_version": "1.2",
    "selected_workspace_version": "1.0",
    # (minimum, maximum). This fixture is a response, so both windows are the ones the
    # serving peer publishes; the caller side of the negotiation appears as the selected
    # versions the server accepted and echoed back.
    "supported_api_versions": ("1.0", "1.3"),
    "supported_workspace_versions": ("1.0", "1.0"),
    "upgrade_state": "none",
}


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.schema.json"
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise TypeError(f"{path}: expected a JSON object at the document root")
    return document


def _build_registry(documents: dict[str, dict[str, Any]]) -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for document in documents.values():
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        if resource_id is None:
            raise ValueError(f"schema document is missing $id: {document!r}")
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


# --------------------------------------------------------------------------
# Schema-level checks
# --------------------------------------------------------------------------


def check_schema_directory_holds_exactly_the_frozen_schemas() -> list[str]:
    """The canonical schema directory must hold exactly :data:`ALL_SCHEMAS`, no more.

    Every other check in this file reads schemas *by name* out of
    :data:`ALL_SCHEMAS`, so a document that is not on that list is never opened:
    an added ``foo.schema.json`` would be validated by nothing here, yet the
    wheel force-includes the whole directory (``pyproject.toml``) and would ship
    it as a packaged, readable contract resource. A deleted document is caught
    loudly elsewhere, but is reported here too so the frozen set is stated in
    one place.

    This complements, and does not replace, the wheel resource-set check in
    ``scripts/check-package-builds.sh``: that one asserts the built wheel
    packages exactly what this directory holds, which is only a guarantee worth
    having once this check has established that the directory holds exactly the
    frozen set.
    """
    findings: list[str] = []
    expected = {f"{name}.schema.json" for name in ALL_SCHEMAS}
    on_disk = {path.name for path in SCHEMA_DIR.glob("*.schema.json")}
    missing = sorted(expected - on_disk)
    extra = sorted(on_disk - expected)
    if missing:
        findings.append(f"{SCHEMA_DIR}: frozen schema document(s) missing: {missing}")
    if extra:
        findings.append(
            f"{SCHEMA_DIR}: schema document(s) no check vouches for: {extra} (add them to "
            "ALL_SCHEMAS in this script if the addition is intentional)"
        )
    return findings


def check_schemas_are_valid_draft_2020_12() -> list[str]:
    findings: list[str] = []
    for name in ALL_SCHEMAS:
        path = SCHEMA_DIR / f"{name}.schema.json"
        try:
            document = _load_schema(name)
        except (OSError, json.JSONDecodeError, TypeError) as error:
            findings.append(f"{path}: could not load as JSON: {error}")
            continue
        try:
            Draft202012Validator.check_schema(document)
        except Exception as error:  # noqa: BLE001 - report any metaschema violation
            findings.append(f"{path}: not a valid Draft 2020-12 schema: {error}")
    return findings


def _iter_refs(node: object) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.append(ref)
        for value in node.values():
            refs.extend(_iter_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_iter_refs(item))
    return refs


def check_all_refs_resolve() -> list[str]:
    findings: list[str] = []
    documents = {name: _load_schema(name) for name in ALL_SCHEMAS}
    registry = _build_registry(documents)
    for name, document in documents.items():
        path = SCHEMA_DIR / f"{name}.schema.json"
        resource = Resource.from_contents(document)
        resolver = registry.resolver(base_uri=resource.id() or "")
        for ref in sorted(set(_iter_refs(document))):
            try:
                resolver.lookup(ref)
            except Unresolvable as error:
                findings.append(f"{path}: unresolvable $ref {ref!r}: {error}")
    return findings


def check_registry_matches_source_schemas() -> list[str]:
    """The registry must publish exactly the union of the source schemas' ``$defs``."""
    findings: list[str] = []
    registry_document = _load_schema(REGISTRY_SCHEMA)
    registry_defs = registry_document.get("$defs", {})
    if not isinstance(registry_defs, dict):
        return [f"{REGISTRY_SCHEMA}.schema.json: $defs must be an object"]

    expected: dict[str, str] = {}
    for source in SOURCE_SCHEMAS:
        document = _load_schema(source)
        defs = document.get("$defs", {})
        if not isinstance(defs, dict):
            findings.append(f"{source}.schema.json: $defs must be an object")
            continue
        for def_name in defs:
            if def_name in expected:
                findings.append(
                    f"{def_name!r} is defined in both {expected[def_name]}.schema.json and "
                    f"{source}.schema.json"
                )
            expected[def_name] = source

    registry_path = SCHEMA_DIR / f"{REGISTRY_SCHEMA}.schema.json"
    missing = sorted(set(expected) - set(registry_defs))
    extra = sorted(set(registry_defs) - set(expected))
    if missing:
        findings.append(f"{registry_path}: missing published definition(s): {missing}")
    if extra:
        findings.append(f"{registry_path}: publishes unknown definition(s): {extra}")

    for def_name, entry in registry_defs.items():
        owner = expected.get(def_name)
        if owner is None:
            continue
        expected_ref = f"{BASE_URI}{owner}.schema.json#/$defs/{def_name}"
        actual_ref = entry.get("$ref") if isinstance(entry, dict) else None
        if actual_ref != expected_ref:
            findings.append(
                f"{registry_path}: {def_name!r} should $ref {expected_ref!r}, found {actual_ref!r}"
            )
    return findings


def check_schema_identity() -> list[str]:
    """Every one of the frozen schema documents must declare the exact Draft 2020-12
    dialect URI and its own exact canonical ``$id``. A missing or drifted ``$id`` breaks
    every ``$ref`` in this contract silently (resolution falls back to a relative or
    default base), so it must be reported loudly instead.
    """
    findings: list[str] = []
    for name in ALL_SCHEMAS:
        path = SCHEMA_DIR / f"{name}.schema.json"
        document = _load_schema(name)
        dialect = document.get("$schema")
        if dialect != JSON_SCHEMA_DIALECT:
            findings.append(
                f"{path}: $schema must be exactly {JSON_SCHEMA_DIALECT!r}, found {dialect!r}"
            )
        expected_id = f"{BASE_URI}{name}.schema.json"
        actual_id = document.get("$id")
        if actual_id != expected_id:
            findings.append(f"{path}: $id must be exactly {expected_id!r}, found {actual_id!r}")
    return findings


def check_registry_sources_are_exact() -> list[str]:
    """The registry's ``x-omnivia-schema-sources`` annotation must list exactly the
    canonical source schemas' URIs, in the canonical order -- neither a stale entry left
    over from a renamed schema nor a source schema missing from the list.
    """
    registry_document = _load_schema(REGISTRY_SCHEMA)
    registry_path = SCHEMA_DIR / f"{REGISTRY_SCHEMA}.schema.json"
    sources = registry_document.get("x-omnivia-schema-sources")
    expected = [f"{BASE_URI}{name}.schema.json" for name in SOURCE_SCHEMAS]
    if not isinstance(sources, list):
        return [f"{registry_path}: x-omnivia-schema-sources must be an array of strings"]
    if sources != expected:
        message = f"{registry_path}: x-omnivia-schema-sources must be exactly {expected!r}, found {sources!r}"
        return [message]
    return []


# --------------------------------------------------------------------------
# Fixture-manifest checks
# --------------------------------------------------------------------------

_ENVELOPE_REFS: dict[str, str] = {
    "RequestEnvelope": REQUEST_ENVELOPE_REF,
    "ResponseEnvelope": RESPONSE_ENVELOPE_REF,
}


def _load_manifest() -> list[dict[str, Any]] | None:
    manifest_path = FIXTURES_DIR / "manifest.json"
    if not manifest_path.is_file():
        return None
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        return None
    return fixtures


def check_fixture_manifest_contract_version_matches_registry() -> list[str]:
    """The fixture manifest's ``contract_version`` must equal the registry's
    ``x-omnivia-contract-version`` exactly, so a contract version bump that forgets to
    touch the fixtures fails loudly instead of silently shipping fixtures stamped with a
    stale version.
    """
    manifest_path = FIXTURES_DIR / "manifest.json"
    if not manifest_path.is_file():
        return [f"{manifest_path}: missing"]
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest_version = manifest.get("contract_version")
    registry_document = _load_schema(REGISTRY_SCHEMA)
    registry_version = registry_document.get("x-omnivia-contract-version")
    if manifest_version != registry_version:
        return [
            (
                f"{manifest_path}: contract_version {manifest_version!r} does not match "
                f"registry x-omnivia-contract-version {registry_version!r}"
            )
        ]
    return []


def check_fixture_manifest_is_complete() -> list[str]:
    findings: list[str] = []
    manifest_path = FIXTURES_DIR / "manifest.json"
    fixtures = _load_manifest()
    if fixtures is None:
        return [f"{manifest_path}: missing or malformed ('fixtures' array required)"]

    listed_files: set[str] = set()
    for entry in fixtures:
        file_name = entry.get("file")
        if not isinstance(file_name, str):
            findings.append(f"{manifest_path}: fixture entry missing a string 'file': {entry!r}")
            continue
        if file_name in listed_files:
            findings.append(f"{manifest_path}: fixture file listed more than once: {file_name}")
        listed_files.add(file_name)
        if not (FIXTURES_DIR / file_name).is_file():
            findings.append(f"{manifest_path}: listed fixture file is missing: {file_name}")

    on_disk = {
        path.name
        for path in FIXTURES_DIR.glob("*.json")
        if path.name not in _NON_WIRE_FIXTURE_FILES
    }
    orphaned = sorted(on_disk - listed_files)
    if orphaned:
        findings.append(f"{manifest_path}: fixture file(s) on disk but not in manifest: {orphaned}")
    return findings


def check_fixture_manifest_entries_are_well_formed() -> list[str]:
    """Every manifest entry must carry a unique nonempty string ``id``, explicit boolean
    ``schema_valid`` and ``tolerant_decode`` flags, and a nonempty string ``semantic`` key.
    File uniqueness and existence are covered by :func:`check_fixture_manifest_is_complete`;
    whether ``semantic`` names a *known* check is covered by :func:`check_fixture_semantics`.
    """
    findings: list[str] = []
    fixtures = _load_manifest()
    if not fixtures:
        return findings

    seen_ids: set[str] = set()
    for entry in fixtures:
        fixture_id = entry.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            findings.append(f"fixture manifest entry missing a nonempty string 'id': {entry!r}")
            fixture_id = None
        elif fixture_id in seen_ids:
            findings.append(f"duplicate fixture id: {fixture_id!r}")
        else:
            seen_ids.add(fixture_id)

        label = fixture_id if fixture_id is not None else "<unknown>"
        for flag_name in ("schema_valid", "tolerant_decode"):
            if not isinstance(entry.get(flag_name), bool):
                findings.append(
                    f"fixture {label!r}: {flag_name!r} must be an explicit boolean, "
                    f"found {entry.get(flag_name)!r}"
                )

        semantic = entry.get("semantic")
        if not isinstance(semantic, str) or not semantic:
            findings.append(f"fixture {label!r}: 'semantic' must be a nonempty string")

    return findings


def check_fixture_manifest_matches_frozen_mapping() -> list[str]:
    """The manifest's id/file/semantic/schema_valid/tolerant_decode mapping must match
    :data:`FROZEN_FIXTURE_MAP` exactly, so deleting a fixture, renaming its file, or
    swapping which semantic assertion it exercises cannot silently stay green. Adding a new
    canonical fixture is done by adding a matching entry to that map in the same change.
    """
    findings: list[str] = []
    manifest_path = FIXTURES_DIR / "manifest.json"
    fixtures = _load_manifest()
    if not fixtures:
        return findings

    actual: dict[str, tuple[Any, Any, Any, Any]] = {}
    for entry in fixtures:
        fixture_id = entry.get("id")
        if isinstance(fixture_id, str) and fixture_id:
            actual[fixture_id] = (
                entry.get("file"),
                entry.get("semantic"),
                entry.get("schema_valid"),
                entry.get("tolerant_decode"),
            )

    missing = sorted(set(FROZEN_FIXTURE_MAP) - set(actual))
    if missing:
        findings.append(f"{manifest_path}: frozen fixture id(s) missing from the manifest: {missing}")
    extra = sorted(set(actual) - set(FROZEN_FIXTURE_MAP))
    if extra:
        findings.append(
            f"{manifest_path}: fixture id(s) not in FROZEN_FIXTURE_MAP: {extra} (add them to "
            "FROZEN_FIXTURE_MAP in this script if the addition is intentional)"
        )
    for fixture_id, expected in FROZEN_FIXTURE_MAP.items():
        got = actual.get(fixture_id)
        if got is not None and got != expected:
            findings.append(
                f"{manifest_path}: fixture {fixture_id!r} must declare "
                f"(file, semantic, schema_valid, tolerant_decode)={expected!r}, found {got!r}"
            )
    return findings


def check_fixtures_match_declared_schema_validity() -> list[str]:
    findings: list[str] = []
    fixtures = _load_manifest()
    if not fixtures:
        return findings

    documents = {name: _load_schema(name) for name in ALL_SCHEMAS}
    registry = _build_registry(documents)

    for entry in fixtures:
        fixture_id = entry.get("id", "<unknown>")
        file_name = entry.get("file")
        envelope = entry.get("envelope")
        expected_valid = entry.get("schema_valid")
        if not isinstance(file_name, str) or envelope not in _ENVELOPE_REFS:
            findings.append(f"fixture {fixture_id!r}: manifest entry is malformed")
            continue
        path = FIXTURES_DIR / file_name
        if not path.is_file():
            continue  # already reported by check_fixture_manifest_is_complete
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        validator = Draft202012Validator(
            {"$ref": _ENVELOPE_REFS[envelope]},
            registry=registry,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        errors = list(validator.iter_errors(document))
        actually_valid = not errors
        if actually_valid != expected_valid:
            findings.append(
                f"fixture {fixture_id!r} ({file_name}): manifest declares "
                f"schema_valid={expected_valid!r}, actual validation "
                f"{'found no errors' if actually_valid else f'found {len(errors)} error(s)'}"
            )
    return findings


def _fixture_document(file_name: str) -> Any:
    with (FIXTURES_DIR / file_name).open(encoding="utf-8") as handle:
        return json.load(handle)


def check_fixture_tolerant_decode_matches_declared() -> list[str]:
    """Actually run the tolerant production decoder over every fixture and compare
    success/failure against its declared ``tolerant_decode``, instead of trusting the flag.
    """
    findings: list[str] = []
    fixtures = _load_manifest()
    if not fixtures:
        return findings

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from omnivia_core.contracts.v1 import codec

    for entry in fixtures:
        fixture_id = entry.get("id", "<unknown>")
        file_name = entry.get("file")
        envelope = entry.get("envelope")
        expected_tolerant = entry.get("tolerant_decode")
        if not isinstance(file_name, str) or not isinstance(expected_tolerant, bool):
            continue
        if not (FIXTURES_DIR / file_name).is_file():
            continue  # already reported by check_fixture_manifest_is_complete
        document = _fixture_document(file_name)
        try:
            if envelope == "RequestEnvelope":
                codec.decode_request(document)
            else:
                codec.decode_response(document)
            actually_tolerant = True
        except codec.ContractDecodeError:
            actually_tolerant = False
        if actually_tolerant != expected_tolerant:
            findings.append(
                f"fixture {fixture_id!r} ({file_name}): manifest declares "
                f"tolerant_decode={expected_tolerant!r}, but the production decoder "
                f"{'succeeded' if actually_tolerant else 'raised ContractDecodeError'}"
            )
    return findings


def _semantic_checks(codec: ModuleType, compatibility: ModuleType) -> dict[str, Any]:
    def effective_capabilities_match(file_name: str) -> list[str]:
        """Assert every frozen fact of the canonical compatible negotiation.

        The semantic key names the capability intersection, but the fixture asserts a
        whole negotiation outcome; checking only the intersection would let the selected
        versions, the supported windows, the status, or the upgrade posture drift
        unnoticed. Each fact is reported independently so a failure names what moved.
        """
        expected = COMPATIBLE_NEGOTIATION_EXPECTED
        findings: list[str] = []
        try:
            envelope = codec.decode_success_response(_fixture_document(file_name))
        except codec.ContractDecodeError as error:
            return [f"{file_name}: expected a decodable successful response, raised {error}"]

        version = envelope.metadata.version
        negotiated = version.compatibility

        if negotiated.status != expected["status"]:
            findings.append(
                f"{file_name}: compatibility.status must be {expected['status']!r}, "
                f"found {negotiated.status!r}"
            )

        # The selected versions are the negotiation's outcome; the versions declared in
        # force on the envelope itself must be the same ones, or the response is telling
        # the caller two different things at once.
        for selected_field, in_force_field, in_force in (
            ("selected_api_version", "api_version", version.api_version),
            ("selected_workspace_version", "workspace_format_version", version.workspace_format_version),
        ):
            selected = getattr(negotiated, selected_field)
            if selected != expected[selected_field]:
                findings.append(
                    f"{file_name}: compatibility.{selected_field} must be "
                    f"{expected[selected_field]!r}, found {selected!r}"
                )
            if in_force != selected:
                findings.append(
                    f"{file_name}: {in_force_field} {in_force!r} disagrees with "
                    f"compatibility.{selected_field} {selected!r}"
                )

        # Both supported windows are frozen, and each selected version must actually
        # classify as compatible inside its own window -- the windows and the status are
        # then two statements of one fact rather than two independent claims.
        for window_field, selected_field in (
            ("supported_api_versions", "selected_api_version"),
            ("supported_workspace_versions", "selected_workspace_version"),
        ):
            window = getattr(negotiated, window_field)
            actual_window = (window.minimum, window.maximum)
            if actual_window != expected[window_field]:
                findings.append(
                    f"{file_name}: compatibility.{window_field} must be "
                    f"{expected[window_field]!r}, found {actual_window!r}"
                )
            classified = compatibility.classify_version_compatibility(
                getattr(negotiated, selected_field), window
            )
            if classified != expected["status"]:
                findings.append(
                    f"{file_name}: {selected_field} classifies as {classified!r} against "
                    f"{window_field}, expected {expected['status']!r}"
                )

        upgrade_state = negotiated.upgrade_state
        if upgrade_state.value != expected["upgrade_state"]:
            findings.append(
                f"{file_name}: compatibility.upgrade_state.value must be "
                f"{expected['upgrade_state']!r}, found {upgrade_state.value!r}"
            )
        if upgrade_state.target_version is not None:
            findings.append(
                f"{file_name}: a no-upgrade state must name no target_version, "
                f"found {upgrade_state.target_version!r}"
            )
        if negotiated.deprecations:
            findings.append(
                f"{file_name}: status {expected['status']!r} (not "
                "'compatible_with_deprecations') must carry no deprecations"
            )

        caps = version.capabilities
        for caps_field in ("supported", "granted", "effective"):
            duplicates = compatibility.duplicate_capability_ids(getattr(caps, caps_field))
            if duplicates:
                findings.append(
                    f"{file_name}: {caps_field!r} lists duplicate capability id(s) "
                    f"{list(duplicates)}"
                )
        computed = compatibility.effective_capabilities(caps.supported, caps.granted)
        if tuple(computed) != tuple(caps.effective):
            findings.append(f"{file_name}: stated 'effective' does not equal supported ∩ granted")

        return findings

    def capability_denial(file_name: str) -> list[str]:
        envelope = codec.decode_error_response(_fixture_document(file_name))
        findings = []
        if envelope.error.code != "capability_not_granted":
            findings.append(f"{file_name}: expected error.code 'capability_not_granted'")
        caps = envelope.metadata.version.capabilities
        denied_id = "memory.export"
        if denied_id not in {ref.id for ref in caps.supported}:
            findings.append(f"{file_name}: expected {denied_id!r} in 'supported'")
        if denied_id in {ref.id for ref in caps.effective}:
            findings.append(f"{file_name}: {denied_id!r} must not appear in 'effective'")
        return findings

    def incompatible_major_version(file_name: str) -> list[str]:
        envelope = codec.decode_error_response(_fixture_document(file_name))
        findings = []
        if envelope.error.code != "incompatible_version":
            findings.append(f"{file_name}: expected error.code 'incompatible_version'")
        details = envelope.error.details or {}
        requested = details.get("requested_api_version")
        window = envelope.metadata.version.compatibility.supported_api_versions
        if not isinstance(requested, str):
            findings.append(f"{file_name}: expected error.details.requested_api_version")
        elif compatibility.classify_version_compatibility(requested, window) != "incompatible":
            findings.append(f"{file_name}: requested version does not classify as incompatible")
        return findings

    def minimal_required_fields_only(file_name: str) -> list[str]:
        envelope = codec.decode_request(_fixture_document(file_name))
        metadata = envelope.metadata
        optional_fields = (
            metadata.deadline_ms,
            metadata.idempotency_key,
            metadata.mutation_precondition,
            metadata.principal_claim,
        )
        findings = []
        if any(field is not None for field in optional_fields):
            findings.append(f"{file_name}: expected every optional RequestMetadata field absent")
        if metadata.scopes or metadata.required_capabilities:
            findings.append(f"{file_name}: expected empty scopes and required_capabilities")
        return findings

    def retryable_after_precondition_refresh(file_name: str) -> list[str]:
        envelope = codec.decode_error_response(_fixture_document(file_name))
        findings = []
        if envelope.error.retry_class != "retryable_after_precondition_refresh":
            findings.append(f"{file_name}: expected retry_class 'retryable_after_precondition_refresh'")
        if codec.is_error_retryable(envelope.error):
            findings.append(f"{file_name}: this retry class must not be blindly retryable")
        return findings

    def all_optional_response_metadata_present(file_name: str) -> list[str]:
        envelope = codec.decode_success_response(_fixture_document(file_name))
        metadata = envelope.metadata
        required_optionals = (
            metadata.page,
            metadata.job,
            metadata.freshness,
            metadata.canonical_resolution_time,
            metadata.warnings,
            metadata.omissions,
            metadata.partial,
            metadata.audit_reference,
        )
        if any(field is None for field in required_optionals):
            return [f"{file_name}: expected every optional ResponseMetadata field present"]
        return []

    def plain_not_found(file_name: str) -> list[str]:
        envelope = codec.decode_error_response(_fixture_document(file_name))
        findings = []
        if envelope.error.code != "not_found":
            findings.append(f"{file_name}: expected error.code 'not_found'")
        if envelope.error.retry_class != "non_retryable":
            findings.append(f"{file_name}: expected retry_class 'non_retryable'")
        if envelope.error.retry_after_ms is not None or envelope.error.details is not None:
            findings.append(f"{file_name}: expected no optional ApiError fields")
        return findings

    def tolerant_decode_ignores_unknown_field(file_name: str) -> list[str]:
        document = _fixture_document(file_name)
        if "experimental_debug" not in document:
            return [f"{file_name}: expected an unknown top-level field to demonstrate tolerance"]
        try:
            codec.decode_success_response(document)
        except codec.ContractDecodeError as error:
            return [f"{file_name}: tolerant decode must ignore unknown fields, raised {error}"]
        return []

    def unrecognized_retry_class_fails_safe(file_name: str) -> list[str]:
        envelope = codec.decode_error_response(_fixture_document(file_name))
        error = envelope.error
        findings = []
        if error.code in codec.FROZEN_ERROR_CODES:
            findings.append(f"{file_name}: expected an error.code outside the frozen vocabulary")
        if error.retry_class in codec.FROZEN_RETRY_CLASSES:
            findings.append(f"{file_name}: expected a retry_class outside the frozen vocabulary")
        if codec.is_error_retryable(error):
            findings.append(f"{file_name}: an unrecognized retry class must fail safe as non-retryable")
        return findings

    def duplicate_capability_id_rejected(file_name: str) -> list[str]:
        # The production codec now rejects this fixture outright (tolerant_decode: false,
        # verified by check_fixture_tolerant_decode_matches_declared): decode_success_response
        # would raise before we ever reach the dataclass. Go through the structural generated
        # decoder directly to inspect the raw 'supported' list without running that semantic
        # validation.
        envelope = codec.response_envelope_from_wire(_fixture_document(file_name))
        supported = envelope.metadata.version.capabilities.supported
        if not compatibility.duplicate_capability_ids(supported):
            return [f"{file_name}: expected a duplicate capability id in 'supported'"]
        return []

    def ambiguous_response_branch_rejected(file_name: str) -> list[str]:
        try:
            codec.decode_response(_fixture_document(file_name))
        except codec.ContractDecodeError:
            return []
        return [f"{file_name}: expected decode_response to reject an ambiguous result/error branch"]

    def calendar_invalid_timestamp_tolerated(file_name: str) -> list[str]:
        envelope = codec.decode_response(_fixture_document(file_name))
        timestamp = envelope.metadata.canonical_resolution_time
        if timestamp is None:
            return [f"{file_name}: expected 'canonical_resolution_time' to be present"]
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            return []
        return [f"{file_name}: expected a calendar-invalid timestamp, {timestamp!r} parsed fine"]

    return {
        "effective_capabilities_match": effective_capabilities_match,
        "capability_denial": capability_denial,
        "incompatible_major_version": incompatible_major_version,
        "minimal_required_fields_only": minimal_required_fields_only,
        "retryable_after_precondition_refresh": retryable_after_precondition_refresh,
        "all_optional_response_metadata_present": all_optional_response_metadata_present,
        "plain_not_found": plain_not_found,
        "tolerant_decode_ignores_unknown_field": tolerant_decode_ignores_unknown_field,
        "unrecognized_retry_class_fails_safe": unrecognized_retry_class_fails_safe,
        "duplicate_capability_id_rejected": duplicate_capability_id_rejected,
        "ambiguous_response_branch_rejected": ambiguous_response_branch_rejected,
        "calendar_invalid_timestamp_tolerated": calendar_invalid_timestamp_tolerated,
    }


def check_fixture_semantics() -> list[str]:
    findings: list[str] = []
    fixtures = _load_manifest()
    if not fixtures:
        return findings

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from omnivia_core.contracts.v1 import codec, compatibility

    dispatch = _semantic_checks(codec, compatibility)
    for entry in fixtures:
        fixture_id = entry.get("id", "<unknown>")
        semantic = entry.get("semantic")
        file_name = entry.get("file")
        if not isinstance(semantic, str) or not isinstance(file_name, str):
            continue
        check = dispatch.get(semantic)
        if check is None:
            findings.append(f"fixture {fixture_id!r}: unknown semantic key {semantic!r}")
            continue
        findings.extend(check(file_name))
    return findings


# --------------------------------------------------------------------------
# Operation-catalogue checks
# --------------------------------------------------------------------------

#: The reusable allowed-error profiles A2.5 freezes. The names are specification
#: shorthand only -- each catalogue entry carries its profile fully materialized,
#: so nothing on the wire ever refers to one of these by name.
_BASE_INSTALL: tuple[str, ...] = (
    "authentication_required",
    "authorization_denied",
    "cancelled",
    "capability_not_granted",
    "deadline_exceeded",
    "dependency_unavailable",
    "incompatible_version",
    "internal_non_recoverable",
    "internal_recoverable",
    "invalid_purpose",
    "invalid_request",
    "rate_limited",
    "upgrade_required",
)
_BASE_WORKSPACE: tuple[str, ...] = tuple(
    sorted((*_BASE_INSTALL, "workspace_migration_required", "workspace_not_granted"))
)
_INSTALL_READ: tuple[str, ...] = tuple(sorted((*_BASE_INSTALL, "bootstrap_in_progress")))
_INSTALL_CREATE: tuple[str, ...] = tuple(
    sorted((*_INSTALL_READ, "conflict", "idempotency_conflict"))
)
_POINT_READ: tuple[str, ...] = tuple(sorted((*_BASE_WORKSPACE, "not_found")))
_PROJECTION_READ: tuple[str, ...] = tuple(
    sorted((*_BASE_WORKSPACE, "projection_unavailable", "stale_projection"))
)
_GRAPH_READ: tuple[str, ...] = tuple(sorted((*_PROJECTION_READ, "not_found", "size_limit_exceeded")))
_CONTEXT_READ: tuple[str, ...] = tuple(
    sorted((*_PROJECTION_READ, "size_limit_exceeded", "token_limit_exceeded"))
)
_CREATE_MUT: tuple[str, ...] = tuple(
    sorted(
        (
            *_BASE_WORKSPACE,
            "idempotency_conflict",
            "workspace_busy",
            "workspace_lease_unavailable",
        )
    )
)
_GOV_MUT: tuple[str, ...] = tuple(
    sorted((*_CREATE_MUT, "conflict", "mutation_precondition_failed", "not_found"))
)
_IMPORT_START: tuple[str, ...] = tuple(sorted((*_CREATE_MUT, "size_limit_exceeded")))
#: Deliberately excludes ``conflict``: a state-based cancel/retry refusal is a
#: successful explicit disposition returning the unchanged handle, not an error.
_JOB_CONTROL: tuple[str, ...] = tuple(sorted((*_CREATE_MUT, "not_found")))
_JOB_EVENTS: tuple[str, ...] = tuple(
    sorted((*_BASE_WORKSPACE, "not_found", "size_limit_exceeded"))
)

ERROR_PROFILES: dict[str, tuple[str, ...]] = {
    "BASE_INSTALL": _BASE_INSTALL,
    "BASE_WORKSPACE": _BASE_WORKSPACE,
    "INSTALL_READ": _INSTALL_READ,
    "INSTALL_CREATE": _INSTALL_CREATE,
    "WORKSPACE_READ": _BASE_WORKSPACE,
    "POINT_READ": _POINT_READ,
    "PROJECTION_READ": _PROJECTION_READ,
    "GRAPH_READ": _GRAPH_READ,
    "CONTEXT_READ": _CONTEXT_READ,
    "CREATE_MUT": _CREATE_MUT,
    "GOV_MUT": _GOV_MUT,
    "IMPORT_START": _IMPORT_START,
    "JOB_CONTROL": _JOB_CONTROL,
    "JOB_EVENTS": _JOB_EVENTS,
}

OPERATION_CATALOGUE_ANNOTATION = "x-omnivia-operation-catalogue"
OPERATION_METADATA_DEFINITION = "OperationMetadata"
FROZEN_PAGE_SIZE = 1000
CANONICAL_INVALID_REQUEST = "invalid_request"


class FrozenOperation(NamedTuple):
    """The part of one catalogue entry A2.5 states directly.

    Everything else an entry carries -- its idempotency, precondition and audit
    posture, its capability version, its pagination page size -- follows from the
    freeze's *common* rules, so it is derived in :func:`_expected_entry` rather
    than restated twenty times. A common rule that drifts therefore fails for
    every operation it governs at once, which is the point: those rules are the
    invariant, not a coincidence twenty entries happen to share.
    """

    scope_kind: str
    required_scopes: tuple[str, ...]
    side_effect: str
    capability: str
    document: str
    definition: str
    errors: str
    paginated: bool
    job_kind: str | None = None
    terminal_result: str | None = None


#: The exact 20 application operations, in the frozen code-point order. Runtime
#: probes (``service.health``, ``service.readiness``, ``service.discover``) are a
#: separate contract and are absent by construction; there is no ``job.resume``.
FROZEN_OPERATIONS: dict[str, FrozenOperation] = {
    "candidate.approve": FrozenOperation(
        "workspace", ("memory:write",), "update", "knowledge.govern",
        "knowledge", "CandidateApprove", "GOV_MUT", False,
    ),
    "candidate.reject": FrozenOperation(
        "workspace", ("memory:write",), "update", "knowledge.govern",
        "knowledge", "CandidateReject", "GOV_MUT", False,
    ),
    "context_pack.build": FrozenOperation(
        "workspace", ("memory:read",), "none", "context_pack.build",
        "context-pack", "ContextPackBuild", "CONTEXT_READ", False,
    ),
    "evidence.search": FrozenOperation(
        "workspace", ("memory:read",), "none", "evidence.read",
        "evidence", "EvidenceSearch", "PROJECTION_READ", True,
    ),
    "graph.traverse": FrozenOperation(
        "workspace", ("graph:read",), "none", "graph.read",
        "graph", "GraphTraversal", "GRAPH_READ", True,
    ),
    "import.start": FrozenOperation(
        "workspace", ("memory:write",), "create", "ingestion.import",
        "jobs", "ImportStart", "IMPORT_START", False,
        job_kind="ingestion.import", terminal_result="ImportCompletionResult",
    ),
    "job.cancel": FrozenOperation(
        "workspace", ("job:control",), "update", "job.control",
        "jobs", "JobCancel", "JOB_CONTROL", False,
    ),
    "job.events": FrozenOperation(
        "workspace", ("job:read",), "none", "job.read",
        "jobs", "JobEvents", "JOB_EVENTS", True,
    ),
    "job.get": FrozenOperation(
        "workspace", ("job:read",), "none", "job.read",
        "jobs", "JobGet", "POINT_READ", False,
    ),
    "job.retry": FrozenOperation(
        "workspace", ("job:control",), "update", "job.control",
        "jobs", "JobRetry", "JOB_CONTROL", False,
    ),
    "knowledge.propose": FrozenOperation(
        "workspace", ("memory:write",), "update", "knowledge.govern",
        "knowledge", "KnowledgePropose", "GOV_MUT", False,
    ),
    "knowledge.search": FrozenOperation(
        "workspace", ("memory:read",), "none", "knowledge.read",
        "knowledge", "KnowledgeSearch", "PROJECTION_READ", True,
    ),
    "memory.create": FrozenOperation(
        "workspace", ("memory:write",), "create", "memory.write",
        "memory", "MemoryCreate", "CREATE_MUT", False,
    ),
    "memory.get": FrozenOperation(
        "workspace", ("memory:read",), "none", "memory.read",
        "memory", "MemoryGet", "POINT_READ", False,
    ),
    "memory.list": FrozenOperation(
        "workspace", ("memory:read",), "none", "memory.read",
        "memory", "MemoryList", "WORKSPACE_READ", True,
    ),
    "memory.search": FrozenOperation(
        "workspace", ("memory:read",), "none", "memory.read",
        "memory", "MemorySearch", "PROJECTION_READ", True,
    ),
    "record.supersede": FrozenOperation(
        "workspace", ("memory:write",), "update", "knowledge.govern",
        "knowledge", "RecordSupersede", "GOV_MUT", False,
    ),
    "workspace.create": FrozenOperation(
        "installation", ("workspace:write",), "create", "workspace.write",
        "workspace", "WorkspaceCreate", "INSTALL_CREATE", False,
    ),
    "workspace.inspect": FrozenOperation(
        "workspace", ("workspace:read",), "none", "workspace.read",
        "workspace", "WorkspaceInspect", "POINT_READ", False,
    ),
    "workspace.list": FrozenOperation(
        "installation", ("workspace:read",), "none", "workspace.read",
        "workspace", "WorkspaceList", "INSTALL_READ", True,
    ),
}

#: The four governance transitions that support and require a mutation
#: precondition. Every other operation sets both precondition booleans false.
FROZEN_PRECONDITION_OPERATIONS: frozenset[str] = frozenset(
    {"candidate.approve", "candidate.reject", "knowledge.propose", "record.supersede"}
)


def _expected_entry(name: str, frozen: FrozenOperation) -> dict[str, Any]:
    """Materialize the complete entry the freeze requires for ``name``.

    The common rules live here once. A read is idempotency-key-free and safe to
    retry because it changes nothing; a mutation requires a key and is unsafe
    without one because a network-level repeat would otherwise become a second
    mutation. Audit category follows the side effect for the same reason: it is
    not an independent choice an entry gets to make.
    """
    read = frozen.side_effect == "none"
    job: dict[str, Any] = {"completion_mode": "synchronous"}
    if frozen.job_kind is not None:
        job = {
            "completion_mode": "always_returns_job",
            "job_kind": frozen.job_kind,
            "terminal_result_schema_ref": (
                f"{BASE_URI}{frozen.document}.schema.json#/$defs/{frozen.terminal_result}"
            ),
        }
    pagination: dict[str, Any] = {"paginated": frozen.paginated}
    if frozen.paginated:
        pagination["max_page_size"] = FROZEN_PAGE_SIZE
    return {
        "name": name,
        "scope": {
            "required_scopes": list(frozen.required_scopes),
            "side_effect": frozen.side_effect,
            "scope_kind": frozen.scope_kind,
        },
        "input_schema_ref": (
            f"{BASE_URI}{frozen.document}.schema.json#/$defs/{frozen.definition}Input"
        ),
        "result_schema_ref": (
            f"{BASE_URI}{frozen.document}.schema.json#/$defs/{frozen.definition}Result"
        ),
        "required_capability": {
            "id": frozen.capability,
            "minimum_version": "1.0",
            "required": True,
        },
        "job": job,
        "pagination": pagination,
        "idempotency": {
            "supports_idempotency_key": not read,
            "required": not read,
            "safe_to_retry": read,
        },
        "precondition": {
            "supports_mutation_precondition": name in FROZEN_PRECONDITION_OPERATIONS,
            "required": name in FROZEN_PRECONDITION_OPERATIONS,
        },
        "audit": {"audited": True, "audit_category": "read" if read else "mutation"},
        "allowed_errors": list(ERROR_PROFILES[frozen.errors]),
    }


def _catalogue_entries() -> tuple[list[Any], list[str]]:
    """Return the raw annotation entries, plus any finding about its own shape."""
    document = _load_schema("operations")
    entries = document.get(OPERATION_CATALOGUE_ANNOTATION)
    path = SCHEMA_DIR / "operations.schema.json"
    if not isinstance(entries, list):
        finding = (
            f"{path}: {OPERATION_CATALOGUE_ANNOTATION} must be an array, "
            f"found {type(entries).__name__}"
        )
        return [], [finding]
    return entries, []


def _entry_names(entries: list[Any]) -> list[Any]:
    """Return each object entry's ``name`` *as written*, well formed or not.

    The annotation is ordinary JSON, so a ``name`` can be absent, null, a number, a
    list, or an object. Those values are kept rather than filtered out here: a check
    that silently dropped them would report a catalogue as merely incomplete when it
    is actually malformed. Narrowing to the strings happens at each use, against
    :func:`_string_names`, so nothing downstream ever sorts, hashes, or looks up a
    value that cannot take part in those operations.
    """
    return [entry.get("name") for entry in entries if isinstance(entry, dict)]


def _string_names(names: list[Any]) -> list[str]:
    """The subset of ``names`` that are strings, in order."""
    return [name for name in names if isinstance(name, str)]


def check_operation_catalogue_shape() -> list[str]:
    """The annotation exists, is an array, and names exactly the frozen operations.

    Order is part of the freeze, not a formatting detail: the generated Python and
    TypeScript catalogues are emitted in annotation order, so a reordered
    annotation silently reorders two public artifacts.
    """
    entries, findings = _catalogue_entries()
    if findings:
        return findings

    if len(entries) != len(FROZEN_OPERATIONS):
        findings.append(
            f"{OPERATION_CATALOGUE_ANNOTATION}: must hold exactly "
            f"{len(FROZEN_OPERATIONS)} entries, found {len(entries)}"
        )
    non_objects = [index for index, entry in enumerate(entries) if not isinstance(entry, dict)]
    if non_objects:
        findings.append(
            f"{OPERATION_CATALOGUE_ANNOTATION}: entrie(s) at index {non_objects} are not objects"
        )
        return findings

    # A name that is absent, null, or not a string is reported as its own defect and
    # then left out of every set operation below. Sorting or hashing it would raise a
    # `TypeError` out of the gate instead of a finding, and a crashed gate reports
    # nothing at all -- including the defects it had already found.
    raw_names = _entry_names(entries)
    malformed = [
        f"index {index} ({name!r})"
        for index, name in enumerate(raw_names)
        if not isinstance(name, str)
    ]
    if malformed:
        findings.append(
            f"{OPERATION_CATALOGUE_ANNOTATION}: entrie(s) at {malformed} have a missing or "
            "non-string name"
        )

    names = _string_names(raw_names)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        findings.append(f"{OPERATION_CATALOGUE_ANNOTATION}: duplicate operation name(s) {duplicates}")

    expected = list(FROZEN_OPERATIONS)
    missing = sorted(set(expected) - set(names))
    unknown = sorted(set(names) - set(expected))
    if missing:
        findings.append(f"{OPERATION_CATALOGUE_ANNOTATION}: missing operation(s) {missing}")
    if unknown:
        findings.append(f"{OPERATION_CATALOGUE_ANNOTATION}: unknown operation(s) {unknown}")
    if not malformed and not missing and not unknown and names != expected:
        findings.append(
            f"{OPERATION_CATALOGUE_ANNOTATION}: entries must be sorted by name; "
            f"expected {expected}, found {names}"
        )

    forbidden = sorted(
        name for name in names if name.startswith("service.") or name == "job.resume"
    )
    if forbidden:
        findings.append(
            f"{OPERATION_CATALOGUE_ANNOTATION}: {forbidden} are not application operations "
            "(runtime probes and job.resume are outside this catalogue)"
        )
    return findings


def check_operation_catalogue_entries_are_valid() -> list[str]:
    """Every entry must validate strictly against ``OperationMetadata`` itself.

    The annotation lives in a schema document, which means nothing validates it
    by default: it is data sitting beside the definition it claims to
    materialize.
    """
    entries, findings = _catalogue_entries()
    if findings:
        return findings

    documents = {name: _load_schema(name) for name in ALL_SCHEMAS}
    registry = _build_registry(documents)
    validator = Draft202012Validator(
        {"$ref": f"{BASE_URI}operations.schema.json#/$defs/{OPERATION_METADATA_DEFINITION}"},
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    for index, entry in enumerate(entries):
        label = entry.get("name") if isinstance(entry, dict) else f"index {index}"
        for error in validator.iter_errors(entry):
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{label}]: not a valid "
                f"{OPERATION_METADATA_DEFINITION}: {error.message}"
            )
    return findings


def check_operation_catalogue_schema_refs() -> list[str]:
    """Every payload reference is an absolute in-contract reference that resolves.

    A relative or foreign reference would still *look* like a binding while
    naming nothing this contract publishes, so the form is checked as strictly as
    the target.
    """
    entries, findings = _catalogue_entries()
    if findings:
        return findings

    documents = {name: _load_schema(name) for name in ALL_SCHEMAS}
    registry = _build_registry(documents)
    resolver = registry.resolver(base_uri=f"{BASE_URI}operations.schema.json")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        frozen = FROZEN_OPERATIONS.get(name) if isinstance(name, str) else None
        raw_job = entry.get("job")
        job = raw_job if isinstance(raw_job, dict) else {}
        actual = {
            "input_schema_ref": entry.get("input_schema_ref"),
            "result_schema_ref": entry.get("result_schema_ref"),
        }
        if "terminal_result_schema_ref" in job:
            actual["terminal_result_schema_ref"] = job["terminal_result_schema_ref"]

        # A malformed name leaves nothing to compare the refs *against*, but they are
        # still checked for form and resolvability below -- an entry nobody can name
        # is not a reason to stop asking whether its bindings resolve.
        expected = (
            _expected_entry(name, frozen) if isinstance(name, str) and frozen is not None else None
        )
        for field_name, ref in actual.items():
            if not isinstance(ref, str) or not ref.startswith(BASE_URI) or "#/$defs/" not in ref:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].{field_name}: must be an absolute "
                    f"{BASE_URI}...#/$defs/... reference, found {ref!r}"
                )
                continue
            try:
                resolver.lookup(ref)
            except Unresolvable as error:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].{field_name}: "
                    f"unresolvable reference {ref!r}: {error}"
                )
                continue
            if expected is None:
                continue
            frozen_ref = (
                expected["job"].get(field_name)
                if field_name == "terminal_result_schema_ref"
                else expected.get(field_name)
            )
            if ref != frozen_ref:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].{field_name}: must bind "
                    f"{frozen_ref!r}, found {ref!r}"
                )
    return findings


def check_operation_catalogue_postures() -> list[str]:
    """Every non-error field of every entry equals the A2.5 freeze exactly.

    Reported field by field rather than object by object: "``memory.list``'s
    audit category is wrong" is actionable, "entry 15 differs" is not.
    """
    entries, findings = _catalogue_entries()
    if findings:
        return findings

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue  # malformed name, already reported by the shape check
        frozen = FROZEN_OPERATIONS.get(name)
        if frozen is None:
            continue  # unknown name, already reported by the shape check
        expected = _expected_entry(name, frozen)
        for field_name in ("scope", "required_capability", "job", "pagination",
                           "idempotency", "precondition", "audit"):
            if entry.get(field_name) != expected[field_name]:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].{field_name}: must be "
                    f"{expected[field_name]!r}, found {entry.get(field_name)!r}"
                )

    # Set-level invariants: stated once, over the whole catalogue, so an entry
    # that drifts *into* a set is caught as surely as one that drifts out of it.
    def _named(predicate: Callable[[dict[str, Any]], bool]) -> list[str]:
        return sorted(
            entry["name"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("name"), str) and predicate(entry)
        )

    def _sub(entry: dict[str, Any], key: str) -> dict[str, Any]:
        value = entry.get(key)
        return value if isinstance(value, dict) else {}

    sets: list[tuple[str, list[str], list[str]]] = [
        (
            "installation-scoped",
            _named(lambda e: _sub(e, "scope").get("scope_kind") == "installation"),
            sorted(n for n, f in FROZEN_OPERATIONS.items() if f.scope_kind == "installation"),
        ),
        (
            "paginated",
            _named(lambda e: _sub(e, "pagination").get("paginated") is True),
            sorted(n for n, f in FROZEN_OPERATIONS.items() if f.paginated),
        ),
        (
            "mutation-precondition",
            _named(lambda e: _sub(e, "precondition").get("supports_mutation_precondition") is True),
            sorted(FROZEN_PRECONDITION_OPERATIONS),
        ),
        (
            "durable-job-starting",
            _named(lambda e: _sub(e, "job").get("completion_mode") != "synchronous"),
            sorted(n for n, f in FROZEN_OPERATIONS.items() if f.job_kind is not None),
        ),
    ]
    for label, actual_set, expected_set in sets:
        if actual_set != expected_set:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}: the {label} operations must be exactly "
                f"{expected_set}, found {actual_set}"
            )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        job = _sub(entry, "job")
        pagination = _sub(entry, "pagination")
        idempotency = _sub(entry, "idempotency")
        precondition = _sub(entry, "precondition")
        mode = job.get("completion_mode")
        if mode == "may_return_job":
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].job: 'may_return_job' does not occur "
                "in the v1 catalogue"
            )
        if mode == "synchronous":
            present = sorted({"job_kind", "terminal_result_schema_ref"} & set(job))
            if present:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].job: a synchronous operation must "
                    f"omit {present}"
                )
        elif mode == "always_returns_job":
            absent = sorted({"job_kind", "terminal_result_schema_ref"} - set(job))
            if absent:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].job: an always_returns_job "
                    f"operation must declare {absent}"
                )
        if pagination.get("paginated") is False and "max_page_size" in pagination:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].pagination: a non-paginated operation "
                "must omit max_page_size"
            )
        if pagination.get("paginated") is True:
            # `type(...) is not int` rather than `!=`: JSON `1000.0` parses to a
            # float that Draft 2020-12 accepts as an integer and that compares
            # equal to 1000, so an equality test alone would let it through here.
            page_size = pagination.get("max_page_size")
            if type(page_size) is not int or page_size != FROZEN_PAGE_SIZE:
                findings.append(
                    f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].pagination: max_page_size must be "
                    f"the integer {FROZEN_PAGE_SIZE}, found {page_size!r}"
                )
        if idempotency.get("required") and not idempotency.get("supports_idempotency_key"):
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].idempotency: a required idempotency "
                "key must also be supported"
            )
        if idempotency.get("safe_to_retry") and idempotency.get("required"):
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].idempotency: an operation that requires "
                "an idempotency key is not safe to retry without one"
            )
        if precondition.get("required") and not precondition.get("supports_mutation_precondition"):
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].precondition: a required mutation "
                "precondition must also be supported"
            )
        if _sub(entry, "audit").get("audited") is not True:
            findings.append(f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].audit: must be audited")
    return findings


def check_operation_catalogue_errors() -> list[str]:
    """Allowed-error arrays are sorted, unique, known, and exactly the frozen profile."""
    entries, findings = _catalogue_entries()
    if findings:
        return findings

    catalogue = _load_schema("errors").get("x-omnivia-error-catalogue")
    known: dict[str, Any] = catalogue if isinstance(catalogue, dict) else {}
    if not known:
        findings.append("errors.schema.json: x-omnivia-error-catalogue must be an object")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        errors = entry.get("allowed_errors")
        if not isinstance(errors, list) or not errors:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: must be a non-empty array"
            )
            continue
        # Every rule below sorts the codes, hashes them, or looks them up in the
        # error catalogue, and none of those is defined for a null, a number, an
        # array, or an object. A non-string code is therefore reported as exactly
        # that, and this entry is judged no further: it cannot equal any frozen
        # profile whatever else it says, so a second finding would only bury the
        # one that names the real defect.
        non_strings = [code for code in errors if not isinstance(code, str)]
        if non_strings:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: every code must be a "
                f"string, found {non_strings!r}"
            )
            continue
        if errors != sorted(errors):
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: must be sorted"
            )
        duplicates = sorted({code for code in errors if errors.count(code) > 1})
        if duplicates:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: duplicate code(s) "
                f"{duplicates}"
            )
        unknown = sorted(code for code in errors if code not in known)
        if unknown:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: code(s) {unknown} do "
                "not resolve through x-omnivia-error-catalogue"
            )
        if CANONICAL_INVALID_REQUEST not in errors:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: every operation can "
                f"reject a malformed request, so {CANONICAL_INVALID_REQUEST!r} must be allowed"
            )
        frozen = FROZEN_OPERATIONS.get(name) if isinstance(name, str) else None
        if frozen is not None and errors != list(ERROR_PROFILES[frozen.errors]):
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: must be exactly the "
                f"{frozen.errors} profile {list(ERROR_PROFILES[frozen.errors])!r}, found {errors!r}"
            )

    for name in ("job.cancel", "job.retry"):
        entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
        if entry is None:
            continue  # missing operation, already reported by the shape check
        # Only an actual array is asked about. `in` over a scalar raises, and over a
        # string or an object it answers a different question entirely -- substring,
        # or key membership -- so a malformed `allowed_errors` would otherwise either
        # crash the gate or produce a finding about something nobody wrote. The loop
        # above has already reported it as not being an array of codes.
        allowed = entry.get("allowed_errors")
        if isinstance(allowed, list) and "conflict" in allowed:
            findings.append(
                f"{OPERATION_CATALOGUE_ANNOTATION}[{name}].allowed_errors: must not allow "
                "'conflict' -- a state-based cancel/retry refusal is a successful explicit "
                "disposition returning the unchanged handle, not a conflict the caller retries"
            )

    retry_class = known.get(CANONICAL_INVALID_REQUEST)
    if retry_class != "non_retryable":
        findings.append(
            f"errors.schema.json: {CANONICAL_INVALID_REQUEST!r} must have retry class "
            f"'non_retryable', found {retry_class!r}"
        )
    return findings


def check_operation_catalogue_fixture_matches() -> list[str]:
    """The independent fixture must equal the canonical annotation exactly.

    The fixture is hand-transcribed rather than generated, precisely so that it
    disagrees when the annotation is edited without intent. It is a regression
    oracle, not a second source: nothing generates from it.
    """
    entries, findings = _catalogue_entries()
    if findings:
        return findings

    if not OPERATION_CATALOGUE_FIXTURE.is_file():
        return [f"{OPERATION_CATALOGUE_FIXTURE}: missing operation-catalogue fixture"]
    with OPERATION_CATALOGUE_FIXTURE.open(encoding="utf-8") as handle:
        fixture = json.load(handle)

    expected_format = "omnivia.operation-catalogue.v1"
    if not isinstance(fixture, dict) or fixture.get("format") != expected_format:
        found = fixture.get("format") if isinstance(fixture, dict) else fixture
        finding = (
            f"{OPERATION_CATALOGUE_FIXTURE}: 'format' must be {expected_format!r}, "
            f"found {found!r}"
        )
        return [finding]
    operations = fixture.get("operations")
    if not isinstance(operations, list):
        return [f"{OPERATION_CATALOGUE_FIXTURE}: 'operations' must be an array"]
    if operations != entries:
        fixture_names = _entry_names(operations)
        canonical_names = _entry_names(entries)
        if fixture_names != canonical_names:
            findings.append(
                f"{OPERATION_CATALOGUE_FIXTURE}: operation names disagree with "
                f"{OPERATION_CATALOGUE_ANNOTATION}: fixture has {fixture_names}, canonical has "
                f"{canonical_names}"
            )
        else:
            # `_entry_names` skips non-object entries on both sides, so matching name
            # lists do not guarantee matching entry *shapes*: an annotation carrying a
            # bare number alongside a duplicated name reaches here. The label falls
            # back to the position, since a non-object entry has no name to quote.
            for index, (fixture_entry, entry) in enumerate(zip(operations, entries)):
                if fixture_entry != entry:
                    label = entry.get("name") if isinstance(entry, dict) else f"index {index}"
                    findings.append(
                        f"{OPERATION_CATALOGUE_FIXTURE}: {label!r} disagrees with "
                        f"{OPERATION_CATALOGUE_ANNOTATION}: fixture has {fixture_entry!r}, "
                        f"canonical has {entry!r}"
                    )
    return findings


# --------------------------------------------------------------------------
# Adapter conformance corpus checks
# --------------------------------------------------------------------------

_CORPUS_FORMAT = "omnivia.adapter-conformance.v1"
#: The frozen A2.4 replay classifications a corpus case may declare.
_CORPUS_IDEMPOTENCY_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"replay", "idempotency_conflict", "distinct"}
)


def _corpus_document() -> tuple[dict[str, Any], list[str]]:
    path = FIXTURES_DIR / ADAPTER_CONFORMANCE_CORPUS
    if not path.is_file():
        return {}, [f"{path}: missing adapter conformance corpus"]
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        return {}, [f"{path}: expected a JSON object at the document root"]
    if document.get("format") != _CORPUS_FORMAT:
        return {}, [
            f"{path}: 'format' must be {_CORPUS_FORMAT!r}, found {document.get('format')!r}"
        ]
    cases = document.get("cases")
    if not isinstance(cases, list):
        return {}, [f"{path}: 'cases' must be an array"]
    return document, []


class CorpusCase(NamedTuple):
    """One corpus case, after every consumed field has been type-checked.

    Coverage analysis runs over these rather than over raw JSON. A checker that
    reached into unvalidated documents could be crashed by a hostile shape -- an
    ``operation`` given as an array is unhashable, and a set membership test on it
    raises ``TypeError`` rather than reporting a finding -- and a gate that can be
    crashed is a gate that can be bypassed.
    """

    id: str
    operation: str
    request_id: str
    branch: str
    error_code: str | None
    replay_of: str | None
    idempotency: str | None
    page_of: str | None


def _corpus_str(
    container: dict[str, Any], key: str, label: str, findings: list[str], *, required: bool = True
) -> str | None:
    """Read one string field, reporting rather than raising on any other shape."""
    if key not in container:
        if required:
            findings.append(f"{label}: missing required field {key!r}")
        return None
    value = container[key]
    if not isinstance(value, str) or isinstance(value, bool) or not value.strip():
        findings.append(
            f"{label}.{key}: must be a non-empty, non-blank string, found {value!r}"
        )
        return None
    return value


def _validated_corpus_cases(
    cases: list[Any], path: Path
) -> tuple[list[CorpusCase], list[str]]:
    """Type-check every case, returning the well-formed ones and every finding.

    Every case is reported on, not just the first bad one: a corpus with three
    malformed entries should say so once rather than across three runs.
    """
    findings: list[str] = []
    validated: list[CorpusCase] = []

    for index, raw in enumerate(cases):
        label = f"{path}: cases[{index}]"
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            findings.append(f"{label}: must be an object, found {type(raw).__name__}")
            continue

        case_id = _corpus_str(raw, "id", label, findings)
        label = f"{path}: case {case_id!r}" if case_id else label
        operation = _corpus_str(raw, "operation", label, findings)
        _corpus_str(raw, "description", label, findings)

        request_id: str | None = None
        for key in ("request", "response"):
            envelope = raw.get(key)
            if not isinstance(envelope, dict) or not all(
                isinstance(name, str) for name in envelope
            ):
                findings.append(
                    f"{label}.{key}: must be an object, found {type(raw.get(key)).__name__}"
                )
                continue
            metadata = envelope.get("metadata")
            if not isinstance(metadata, dict) or not all(
                isinstance(name, str) for name in metadata
            ):
                findings.append(
                    f"{label}.{key}.metadata: must be an object, found "
                    f"{type(metadata).__name__}"
                )
                continue
            if key == "request":
                request_id = _corpus_str(metadata, "request_id", f"{label}.request.metadata",
                                         findings)

        expect = raw.get("expect")
        if not isinstance(expect, dict) or not all(isinstance(name, str) for name in expect):
            findings.append(
                f"{label}.expect: must be an object, found {type(expect).__name__}"
            )
            continue
        branch = _corpus_str(expect, "branch", f"{label}.expect", findings)
        if branch is not None and branch not in {"success", "error"}:
            findings.append(f"{label}.expect.branch: must be 'success' or 'error', found {branch!r}")
            branch = None
        optionals: dict[str, str | None] = {}
        for key in ("error_code", "retry_class", "replay_of", "idempotency", "page_of"):
            optionals[key] = (
                None if key not in expect
                else _corpus_str(expect, key, f"{label}.expect", findings)
            )

        idempotency = optionals["idempotency"]
        if idempotency is not None and idempotency not in _CORPUS_IDEMPOTENCY_CLASSIFICATIONS:
            findings.append(
                f"{label}.expect.idempotency: must be one of "
                f"{sorted(_CORPUS_IDEMPOTENCY_CLASSIFICATIONS)}, found {idempotency!r}"
            )
            optionals["idempotency"] = None
        if (optionals["replay_of"] is None) != (optionals["idempotency"] is None):
            findings.append(
                f"{label}.expect: 'replay_of' and 'idempotency' state one fact together and "
                "must both be present or both absent"
            )
        if branch == "success" and (
            optionals["error_code"] is not None or optionals["retry_class"] is not None
        ):
            findings.append(
                f"{label}.expect: a success case must not declare an error code or retry class"
            )
        if branch == "error" and optionals["error_code"] is None:
            findings.append(f"{label}.expect: an error case must declare its error code")

        trusted = raw.get("trusted", {})
        if not isinstance(trusted, dict) or not all(isinstance(name, str) for name in trusted):
            findings.append(
                f"{label}.trusted: must be an object, found {type(trusted).__name__}"
            )

        if None in (case_id, operation, request_id, branch):
            continue
        assert case_id is not None and operation is not None
        assert request_id is not None and branch is not None
        validated.append(
            CorpusCase(
                id=case_id,
                operation=operation,
                request_id=request_id,
                branch=branch,
                error_code=optionals["error_code"],
                replay_of=optionals["replay_of"],
                idempotency=optionals["idempotency"],
                page_of=optionals["page_of"],
            )
        )
    return validated, findings


def check_adapter_conformance_corpus() -> list[str]:
    """The corpus must actually cover what A2.6 requires it to cover.

    Coverage is asserted against the *catalogue*, not against however the corpus
    happened to be written. A corpus that quietly stopped exercising an operation,
    an error code, or a replay pair would still let every adapter pass -- and the
    pass would mean less than it appeared to, which is the failure mode a shared
    conformance corpus exists to prevent.

    Shape is validated before coverage, and every field this reads is
    type-checked first: the check has to be total over any JSON a corpus file
    could contain, because a crash is not a finding.
    """
    document, findings = _corpus_document()
    if findings:
        return findings

    path = FIXTURES_DIR / ADAPTER_CONFORMANCE_CORPUS
    cases, findings = _validated_corpus_cases(
        cast("list[Any]", document["cases"]), path
    )
    catalogue = FROZEN_OPERATIONS

    ids = [case.id for case in cases]
    duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate_ids:
        findings.append(f"{path}: duplicate case id(s) {duplicate_ids}")

    request_ids = [case.request_id for case in cases]
    duplicates = sorted({r for r in request_ids if request_ids.count(r) > 1})
    if duplicates:
        findings.append(
            f"{path}: duplicate request id(s) {duplicates} -- an adapter keyed by "
            "request id could not tell the exchanges apart"
        )

    unknown = sorted({case.operation for case in cases if case.operation not in catalogue})
    if unknown:
        findings.append(f"{path}: case(s) name non-catalogue operation(s) {unknown}")

    by_id = {case.id: case for case in cases}
    for case in cases:
        for link, target_id in (("replay_of", case.replay_of), ("page_of", case.page_of)):
            if target_id is None:
                continue
            target = by_id.get(target_id)
            if target is None:
                findings.append(f"{path}: case {case.id!r} {link} names unknown case {target_id!r}")
            elif target.operation != case.operation:
                findings.append(
                    f"{path}: case {case.id!r} {link} names {target_id!r}, which is a "
                    f"{target.operation!r} exchange"
                )

    known = {case.operation for case in cases if case.operation in catalogue}
    successes = {
        case.operation
        for case in cases
        if case.branch == "success" and case.replay_of is None and case.page_of is None
    }
    missing_successes = sorted(set(catalogue) - successes)
    if missing_successes:
        findings.append(f"{path}: no primary success case for operation(s) {missing_successes}")

    mutations = sorted(n for n, f in catalogue.items() if f.side_effect != "none")
    # Exact triples, not label counts: a replay must be a success answering a
    # primary case, and a conflict must be an error carrying the conflict code.
    replayed = sorted({
        case.operation for case in cases
        if case.idempotency == "replay"
        and case.branch == "success"
        and case.replay_of is not None
    })
    conflicted = sorted({
        case.operation for case in cases
        if case.idempotency == "idempotency_conflict"
        and case.branch == "error"
        and case.error_code == "idempotency_conflict"
        and case.replay_of is not None
    })
    if replayed != mutations:
        findings.append(
            f"{path}: every mutation needs an honest-replay case answering a primary "
            f"exchange on the success branch; expected {mutations}, found {replayed}"
        )
    if conflicted != mutations:
        findings.append(
            f"{path}: every mutation needs an idempotency-conflict case refused on the "
            f"error branch with 'idempotency_conflict'; expected {mutations}, found "
            f"{conflicted}"
        )

    paginated = sorted(n for n, f in catalogue.items() if f.paginated)
    second_pages = sorted({
        case.operation for case in cases if case.page_of is not None and case.branch == "success"
    })
    if second_pages != paginated:
        findings.append(
            f"{path}: every paginated operation needs a two-page scenario; expected "
            f"{paginated}, found {second_pages}"
        )

    covered_errors: dict[str, str] = {}
    for case in cases:
        if case.error_code is not None and case.branch == "error":
            covered_errors.setdefault(case.error_code, case.operation)
    error_catalogue = _load_schema("errors").get("x-omnivia-error-catalogue")
    expected_codes = sorted(error_catalogue) if isinstance(error_catalogue, dict) else []
    uncovered = sorted(set(expected_codes) - set(covered_errors))
    if uncovered:
        findings.append(f"{path}: no case exercises error code(s) {uncovered}")

    for code, operation in sorted(covered_errors.items()):
        frozen = catalogue.get(operation)
        if frozen is not None and code not in ERROR_PROFILES[frozen.errors]:
            findings.append(
                f"{path}: error {code!r} is exercised on {operation!r}, which the catalogue "
                "does not permit to raise it"
            )

    # The one negative the corpus must never state, restated where it is cheap to check.
    for case in cases:
        if case.operation in {"job.cancel", "job.retry"} and case.error_code == "conflict":
            findings.append(
                f"{path}: case {case.id!r} has {case.operation!r} failing with 'conflict'; "
                "a state-based refusal is a successful disposition"
            )
    if not known:
        findings.append(f"{path}: the corpus names no catalogue operation at all")
    return findings


# --------------------------------------------------------------------------
# Dependency-boundary checks
# --------------------------------------------------------------------------


def _module_package_name(py_file: Path) -> str:
    """The dotted package ``py_file`` belongs to, for resolving its relative imports.

    A regular module's package is its parent directory; an ``__init__.py``'s package is the
    directory it lives in (itself). Both reduce to the same operation: drop the last dotted
    path component (the module's own name, or ``__init__``).
    """
    parts = list(py_file.relative_to(SRC_ROOT).with_suffix("").parts)
    return ".".join(parts[:-1])


def _resolve_relative_import(py_file: Path, level: int, module: str | None) -> str | None:
    """Resolve a relative import in ``py_file`` to an absolute dotted module name.

    Returns ``None`` when the import climbs above the source root entirely: that is always
    an escape, never a valid in-repo reference.
    """
    package = _module_package_name(py_file)
    parts = package.split(".") if package else []
    climb = level - 1
    if climb > len(parts):
        return None
    base_parts = parts[: len(parts) - climb] if climb else parts
    base = ".".join(base_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base or None


def _forbidden_module_reference(module: str, stdlib: frozenset[str]) -> str | None:
    """Return why ``module`` is forbidden to import from inside the contracts package, or
    ``None`` if it is allowed (standard library, ``__future__``, or ``omnivia_core.contracts``
    itself/a descendant).
    """
    top = module.split(".")[0]
    if top == "omnivia_core":
        if module != _ALLOWED_CONTRACTS_IMPORT_PREFIX and not module.startswith(
            f"{_ALLOWED_CONTRACTS_IMPORT_PREFIX}."
        ):
            return f"forbidden import {module!r}"
        return None
    if top not in stdlib:
        return f"forbidden non-stdlib import {module!r}"
    return None


# The two standard-library modules that hand out a by-name importer, and every
# dotted spelling of one that they publish. `importlib` re-exports `__import__`,
# so it appears under both.
_DYNAMIC_IMPORT_MODULES = frozenset({"importlib", "builtins"})
_DYNAMIC_IMPORT_FUNCTIONS = frozenset(
    {"importlib.import_module", "importlib.__import__", "builtins.__import__"}
)
# Bare spellings that are treated as a dynamic import even when nothing in the
# file binds them: `__import__` is a builtin, and an unbound `import_module`
# call spelled exactly like the escape hatch fails closed rather than being
# assumed harmless.
_BARE_DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "import_module"})

# Stands for "this name may be one of the above, and this checker cannot prove
# which binding is live here". It is not a valid dotted name, so it can never
# collide with a real reference, and it classifies as both a dynamic-import
# module and a dynamic-import function so an ambiguous rebinding fails closed.
_AMBIGUOUS_REFERENCE = "<ambiguous>"


def _import_alias_map(tree: ast.Module) -> dict[str, str]:
    """Map every name an ``import`` statement binds in this file to what it refers to.

    A dynamic import is only recognizable by name, and a name can be renamed at
    the import: ``import importlib as il`` makes ``il.import_module`` the very
    same escape hatch as ``importlib.import_module``, and ``from importlib
    import import_module as load`` -- or ``from builtins import __import__ as
    load`` -- makes a bare ``load(...)`` one. Matching the canonical spellings
    alone would let any of those renames walk straight past this gate, so the
    calls are checked against what each name was actually bound to.

    Values are dotted: a plain module for ``import`` (``il`` ->
    ``importlib``), and module-qualified for ``from`` (``load`` ->
    ``importlib.import_module``). Relative ``from`` imports are skipped: they
    can only name something inside this package, which is never a dynamic
    import into the standard library.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b` binds `a`; `import a.b as c` binds `c` to `a.b`.
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted_reference(node: ast.expr, references: dict[str, str]) -> str | None:
    """The dotted name a statically resolvable expression refers to, or ``None``.

    Exactly three expression forms resolve, and nothing here ever evaluates
    inspected code: a bare name (through ``references``), an attribute chain on
    one (``il.import_module``), and a literal ``getattr(<resolvable>,
    "<attr>")`` -- which is the same attribute access written as a call, and so
    must resolve to the same dotted name. Anything else -- a computed string, a
    subscript, the result of some other call -- is not statically resolvable and
    returns ``None``.

    An unbound bare name resolves to itself, so ``importlib.import_module(...)``
    is still recognized in a file whose import of ``importlib`` this checker
    never saw.
    """
    if isinstance(node, ast.Name):
        return references.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted_reference(node.value, references)
        if base is None or base == _AMBIGUOUS_REFERENCE:
            return base
        return f"{base}.{node.attr}"
    if isinstance(node, ast.Call):
        return _getattr_reference(node, references)
    return None


def _getattr_reference(node: ast.Call, references: dict[str, str]) -> str | None:
    """The dotted name a literal ``getattr(<resolvable>, "<attr>")`` call resolves to.

    ``getattr(importlib, "import_module")`` is ``importlib.import_module``
    spelled as a call, so it has to resolve the same way. A third positional
    argument (the default) does not change which attribute is being named, so it
    is tolerated. A rebound ``getattr`` needs no special case: the inner call is
    itself a call node this checker inspects.

    A computed attribute name is not statically knowable, so which member is
    being named cannot be proven. Off a dynamic-import module that is the same
    ambiguity a rebound alias creates and it fails closed the same way
    (:data:`_AMBIGUOUS_REFERENCE`); off anything else -- ``getattr(json,
    name)`` -- it is ordinary reflection this gate has no business flagging.
    """
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
        return None
    if len(node.args) not in (2, 3) or node.keywords:
        return None
    base = _dotted_reference(node.args[0], references)
    if base is None or base == _AMBIGUOUS_REFERENCE:
        return base
    attribute = node.args[1]
    if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
        return f"{base}.{attribute.value}"
    return _AMBIGUOUS_REFERENCE if _is_import_related(base) else None


def _is_import_related(reference: str) -> bool:
    """Whether a dotted reference names anything inside a dynamic-import module."""
    return (
        reference == _AMBIGUOUS_REFERENCE
        or reference.split(".")[0] in _DYNAMIC_IMPORT_MODULES
    )


def _names_a_dynamic_import_function(reference: str) -> bool:
    return reference in _DYNAMIC_IMPORT_FUNCTIONS or reference == _AMBIGUOUS_REFERENCE


def _assignment_bindings(tree: ast.Module) -> dict[str, list[ast.expr | None]]:
    """Map every name a simple assignment binds in this file to what it was assigned.

    ``None`` records a binding this checker cannot read: a tuple unpacking, a
    walrus in a comprehension, anything where the bound value is not a plain
    expression this file can follow. It is kept rather than dropped because a
    name bound *both* to an importer and to something unreadable is exactly the
    case that must fail closed (:func:`_resolve_references`).

    Bindings are collected file-wide with no flow analysis: a name assigned in
    two branches is treated as bound to both, which is what makes the
    fail-closed rule sound without this script having to reason about
    reachability.
    """
    bindings: dict[str, list[ast.expr | None]] = {}

    def bind(target: ast.expr, value: ast.expr | None) -> None:
        if isinstance(target, ast.Name):
            bindings.setdefault(target.id, []).append(value)
            return
        # A destructuring or attribute/subscript target: every plain name it
        # binds is bound to something this checker cannot read.
        for inner in ast.walk(target):
            if isinstance(inner, ast.Name):
                bindings.setdefault(inner.id, []).append(None)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
            bind(node.target, node.value)
        elif isinstance(node, ast.AugAssign):
            bind(node.target, None)
    return bindings


def _resolve_references(tree: ast.Module) -> dict[str, str]:
    """Resolve every name in this file that a call could reach a dynamic importer through.

    Starts from the import aliases and folds in simple assignment aliases --
    ``load = importlib.import_module`` binds ``load`` to the importer exactly as
    an ``as`` clause would -- repeating until nothing changes, so a chain
    (``first = importlib.import_module``, ``then = first``) resolves through as
    many links as the file actually has. The loop is bounded by the number of
    assignments, so it always terminates.

    A name bound more than once resolves only when every binding agrees. When
    they disagree, or when one of them is a value this checker cannot read, the
    name is *import-related if any binding is*, and then resolves to
    :data:`_AMBIGUOUS_REFERENCE`: an importer that gets rebound cannot be proven
    safe at the call, so it is treated as still being an importer. A name whose
    bindings have nothing to do with importing is simply left unresolved --
    ordinary rebinding of ordinary names is not this gate's business.
    """
    import_aliases = _import_alias_map(tree)
    assignments = _assignment_bindings(tree)
    resolved = dict(import_aliases)
    for _ in range(len(assignments) + 1):
        candidates = dict(import_aliases)
        for name, values in assignments.items():
            references = {
                _dotted_reference(value, resolved) if value is not None else None
                for value in values
            }
            if name in import_aliases:
                references.add(import_aliases[name])
            if len(references) == 1:
                only = next(iter(references))
                if only is not None:
                    candidates[name] = only
            elif any(
                reference is not None and _is_import_related(reference)
                for reference in references
            ):
                candidates[name] = _AMBIGUOUS_REFERENCE
        if candidates == resolved:
            break
        resolved = candidates
    return resolved


def _is_dynamic_import_call(node: ast.Call, references: dict[str, str]) -> bool:
    """Whether ``node`` calls one of the standard library's by-name importers --
    ``__import__`` or ``importlib.import_module`` -- however the module or the function
    reached the call site.

    ``references`` is this file's :func:`_resolve_references`, so every constant,
    statically resolvable spelling of the same escape hatch is recognized: the
    canonical calls, an ``as`` rename of either the module or the function, an
    ``__import__`` imported from ``builtins``, an assignment alias (including a
    chain of them), and a literal ``getattr`` of the function off its module.
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id in _BARE_DYNAMIC_IMPORT_NAMES:
        return True
    reference = _dotted_reference(func, references)
    return reference is not None and _names_a_dynamic_import_function(reference)


def _check_file_imports(py_file: Path, stdlib: frozenset[str]) -> list[str]:
    """Report every forbidden import boundary violation in one file: an absolute import
    outside the standard library or ``omnivia_core.contracts``, a relative import that
    resolves outside that namespace, or a constant-string dynamic import (``__import__`` /
    ``importlib.import_module``, under any statically resolvable alias) that escapes it.
    """
    findings: list[str] = []
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    references = _resolve_references(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                reason = _forbidden_module_reference(alias.name, stdlib)
                if reason:
                    findings.append(f"{py_file}: {reason}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    reason = _forbidden_module_reference(node.module, stdlib)
                    if reason:
                        findings.append(f"{py_file}: {reason}")
                continue
            resolved = _resolve_relative_import(py_file, node.level, node.module)
            if resolved is None:
                dots = "." * node.level
                findings.append(
                    f"{py_file}: relative import {dots}{node.module or ''} escapes above "
                    f"{_ALLOWED_CONTRACTS_IMPORT_PREFIX}"
                )
                continue
            reason = _forbidden_module_reference(resolved, stdlib)
            if reason:
                findings.append(f"{py_file}: forbidden relative import resolving to {resolved!r}")
        elif isinstance(node, ast.Call) and _is_dynamic_import_call(node, references):
            first_arg = node.args[0] if node.args else None
            if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
                findings.append(
                    f"{py_file}: dynamic import with a non-literal argument cannot be "
                    "verified to stay within omnivia_core.contracts"
                )
                continue
            reason = _forbidden_module_reference(first_arg.value, stdlib)
            if reason:
                findings.append(f"{py_file}: dynamic import escape, {reason}")
    return findings


def check_contract_package_has_no_forbidden_imports() -> list[str]:
    findings: list[str] = []
    stdlib = frozenset(sys.stdlib_module_names) | {"__future__"}
    for py_file in sorted(CONTRACTS_SRC.rglob("*.py")):
        findings.extend(_check_file_imports(py_file, stdlib))
    return findings


# --------------------------------------------------------------------------
# Regeneration-drift check
# --------------------------------------------------------------------------


def check_generated_artifacts_match_schemas() -> list[str]:
    generator = _load_module(GENERATOR_SCRIPT, "check_application_contracts_generator")
    try:
        artifacts = generator.render_all()
    except generator.UnsupportedSchemaError as error:
        return [f"{GENERATOR_SCRIPT}: {error}"]
    return cast("list[str]", generator.check_all(artifacts))


# --------------------------------------------------------------------------
# Scalar pattern compatibility classification (A2.7)
# --------------------------------------------------------------------------

PATTERN_BASELINE = REPO_ROOT / "contracts" / "application" / "v1" / "pattern-baseline.json"
PATTERN_BASELINE_FORMAT = "omnivia.application-pattern-baseline.v1"
SCHEMA_ROOT = "contracts/application/v1/schemas"

#: The accepted contract checkpoint the pattern baseline may name.
#:
#: The trust anchor, and deliberately *not* inferred from the candidate's own
#: history. Ancestry only proves ordering: every earlier commit on a branch is an
#: ancestor of its tip, so a candidate that commits its schemas and then names
#: that commit satisfies an ancestry test while certifying itself. Acceptance is
#: a fact about review, so it has to arrive from somewhere the change under
#: review does not author.
#:
#: **This constant is a local-development fallback, and nothing more.** It lives
#: in the tree the candidate is allowed to rewrite, so on its own it establishes
#: nothing: a candidate can commit its schemas, move this line to that commit,
#: and certify itself in one diff. It exists so the gate is runnable on a laptop
#: without repository configuration, and hosted acceptance refuses it outright --
#: see :data:`ACCEPTED_CHECKPOINT_ENV` and :func:`_resolve_accepted_checkpoint`.
#:
#: **Rollover rule.** This value moves only *after* a slice is independently
#: accepted and committed, in a change of its own, together with a baseline
#: re-captured from that commit. It is never advanced prospectively to a commit
#: belonging to the candidate it is meant to judge. Currently the accepted P0
#: service endpoint checkpoint, which publishes ``ServiceEndpointUri``.
ACCEPTED_CONTRACT_CHECKPOINT = "499e8b3667b707dca58f097cd099d27a23882026"

#: Where the *authoritative* anchor arrives: repository-external GitHub
#: configuration -- a repository or environment variable, set and audited outside
#: this tree -- passed into the gate process by the workflow. Hosted acceptance
#: reads the anchor from here and from nowhere else.
ACCEPTED_CHECKPOINT_ENV = "OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT"

#: Environment variables that mark a hosted run. GitHub Actions always sets both
#: to ``true``; other hosted runners set ``CI``. Any non-empty value counts,
#: including one this gate cannot interpret: reading an ambiguous marker as
#: hosted is the fail-closed direction, and the cost of being wrong is a run that
#: demands its anchor be configured rather than one that silently trusts the tree.
HOSTED_EXECUTION_MARKERS: tuple[str, ...] = ("GITHUB_ACTIONS", "CI")

#: Git's canonical object-id spelling: exactly 40 lowercase hex characters.
_FULL_OBJECT_ID = re.compile(r"[0-9a-f]{40}")

#: The one transformation this gate may classify below major.
TERMINAL_GUARD = "(?![\\s\\S])"

CLASSIFICATION_UNCHANGED = "unchanged"
CLASSIFICATION_PARITY = "patch / validator_parity_correction"
CLASSIFICATION_MAJOR = "major / manual_review"


def _patterned_nodes(document: Mapping[str, Any], source: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any, trail: list[str]) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("pattern"), str):
                found[f"{source}:{'.'.join(trail)}"] = node
            for key, value in node.items():
                walk(value, [*trail, key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, [*trail, str(index)])

    walk(document, [])
    return found


def _current_patterned_nodes() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        found.update(
            _patterned_nodes(json.loads(path.read_text(encoding="utf-8")), path.stem)
        )
    return found


def _corpus_probe_values() -> list[str]:
    """Every string the frozen fixtures carry, plus trailing-suffix variants.

    The classifier's evidence is drawn from the frozen corpus rather than from
    generated inputs, because the corpus is the language the contract actually
    publishes and is the same population every other gate reasons about.
    """
    strings: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, str):
            strings.add(node)

    for path in sorted(FIXTURES_DIR.glob("*.json")):
        collect(json.loads(path.read_text(encoding="utf-8")))
    probes = set(strings)
    for value in strings:
        for suffix in ("\n", "\r", "\r\n", " ", "\t", "\x00", "é"):
            probes.add(value + suffix)
    return sorted(probes)


def _ecmascript_accepts(patterns: Sequence[str], values: Sequence[str]) -> list[list[bool]] | None:
    """Ask Node which values each pattern accepts, or ``None`` when unavailable.

    ``None`` is not "assume it agrees": the caller turns it into an
    indeterminate classification, which fails. A parity claim that cannot be
    checked against the other runtime is not a parity claim.
    """
    if shutil.which("node") is None:
        return None
    script = (
        "const {patterns, values} = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        "const out = patterns.map(p => {\n"
        "  let re; try { re = new RegExp(p); } catch (e) { return null; }\n"
        "  return values.map(v => re.test(v));\n"
        "});\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    try:
        finished = subprocess.run(
            ["node", "-e", script],
            input=json.dumps({"patterns": list(patterns), "values": list(values)}),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if finished.returncode != 0:
        return None
    decoded: Any = json.loads(finished.stdout)
    return None if any(row is None for row in decoded) else cast("list[list[bool]]", decoded)


def _classify(
    name: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    values: Sequence[str],
    ecmascript: Mapping[str, list[bool]] | None,
) -> tuple[str, str | None]:
    """Classify one definition's change, returning (classification, why-not-patch).

    Everything defaults to major. The single exception is the exact terminal-guard
    transformation, and it has to earn its way past three separate conditions --
    the pattern differs *only* by the guard, every other keyword is untouched, and
    the values Python stops accepting were already refused by the semantic layer
    and by ECMAScript. A change that cannot be evaluated is indeterminate, never
    optimistic.
    """
    old_pattern, new_pattern = old["pattern"], new["pattern"]
    if old_pattern == new_pattern and old.get("keywords") == {
        k: v for k, v in new.items() if k != "pattern"
    }:
        return CLASSIFICATION_UNCHANGED, None

    if new_pattern != old_pattern + TERMINAL_GUARD:
        return CLASSIFICATION_MAJOR, "the pattern changed by more than the terminal guard"
    if old.get("keywords") != {k: v for k, v in new.items() if k != "pattern"}:
        return CLASSIFICATION_MAJOR, "another schema keyword changed alongside the pattern"

    # Condition: every value Python stops accepting was already rejected by the
    # public semantic layer, which uses `fullmatch`.
    #
    # Under the exact-guard precondition above this is provably satisfied -- if
    # `fullmatch(old, v)` succeeds then `old` spans the whole value, so nothing
    # follows the match and the guard holds -- and likewise nothing can be newly
    # admitted, since appending a guard only ever narrows. Both are kept as
    # executed checks rather than as a comment because they are what makes the
    # precondition *safe to loosen*: relax the exact-guard rule above and these
    # start doing real work in the same commit. They are stated here as vacuous
    # today so nobody reads them as the thing carrying the argument.
    dropped = [
        value
        for value in values
        if re.search(old_pattern, value) is not None and re.search(new_pattern, value) is None
    ]
    readmitted = [
        value
        for value in values
        if re.search(old_pattern, value) is None and re.search(new_pattern, value) is not None
    ]
    if readmitted:
        return CLASSIFICATION_MAJOR, f"the new pattern admits {readmitted[0]!r}, which the old refused"
    still_semantic = [value for value in dropped if re.fullmatch(old_pattern, value) is not None]
    if still_semantic:
        return (
            CLASSIFICATION_MAJOR,
            f"removes {still_semantic[0]!r}, which public semantics already accepted",
        )

    # Condition: the two ECMAScript languages agree over the frozen corpus.
    #
    # This is the condition that actually does the work. ECMAScript's `$` is
    # already strict, so appending the guard to an *anchored* pattern is a no-op
    # there -- which is precisely what makes it a parity correction rather than a
    # language change. Append it to an unanchored pattern and JavaScript's
    # accepted language really does shrink, the two runs disagree, and the change
    # is classified major. Nothing else here distinguishes those two cases.
    if ecmascript is None:
        return "indeterminate", "ECMAScript evaluation is unavailable"
    before, after = ecmascript.get(old_pattern), ecmascript.get(new_pattern)
    if before is None or after is None:
        return "indeterminate", "ECMAScript evaluation did not cover this pattern"
    if before != after:
        index = next(i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b)
        return (
            CLASSIFICATION_MAJOR,
            f"the ECMAScript languages differ at {values[index]!r}",
        )
    del name
    return CLASSIFICATION_PARITY, None


def _git(*arguments: str) -> tuple[int, str]:
    """Run one git command, reporting failure rather than raising.

    A missing or unusable git is a *finding*, not a traceback: this gate has to
    answer "verified" or "not verified" deterministically, and an exception
    escaping here would replace that answer with a crash the caller cannot
    classify.
    """
    try:
        finished = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 1, ""
    return finished.returncode, finished.stdout


def _definitions_at(revision: str) -> dict[str, dict[str, Any]] | None:
    """The patterned definitions recorded in the schemas at `revision`.

    ``None`` when the revision cannot be read at all, which the caller turns
    into a failure rather than a pass.
    """
    definitions: dict[str, dict[str, Any]] = {}
    # Enumerated from the revision, not from the working tree. What the baseline
    # claims is "this is what the accepted commit published", so the set of
    # documents to read is the commit's, and this check stays independent of
    # whatever the current schema directory happens to contain.
    code, listing = _git("ls-tree", "--name-only", revision, f"{SCHEMA_ROOT}/")
    if code != 0:
        return None
    for relative in sorted(line for line in listing.splitlines() if line.endswith(".schema.json")):
        code, blob = _git("show", f"{revision}:{relative}")
        if code != 0:
            return None
        stem = relative.rsplit("/", 1)[-1].removesuffix(".json")
        for name, node in _patterned_nodes(json.loads(blob), stem).items():
            definitions[name] = {
                "pattern": node["pattern"],
                "keywords": {k: v for k, v in node.items() if k != "pattern"},
            }
    return definitions


def _is_hosted_execution(environ: Mapping[str, str]) -> bool:
    """Whether this run is GitHub Actions or any other CI, per its own markers."""
    return any(environ.get(marker, "").strip() for marker in HOSTED_EXECUTION_MARKERS)


def _resolve_accepted_checkpoint(
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, list[str]]:
    """The trust anchor to judge the baseline against, or why there is not one.

    Hosted acceptance takes the anchor only from :data:`ACCEPTED_CHECKPOINT_ENV`,
    which the workflow fills from repository-external GitHub configuration. The
    constant in this file is not an acceptable hosted source and is not consulted
    there at all: it sits in the tree under review, so a candidate that moved it
    to a commit of its own would be certifying itself with the very artifact the
    anchor exists to be independent of.

    Everything short of a canonical object id fails closed rather than falling
    back -- unset, empty, whitespace, a symbolic ref, an abbreviation, uppercase
    hex, a revision expression. Falling back on any of those would turn a
    misconfigured repository variable into a silently self-anchored run, which is
    the same failure as having no anchor, only harder to notice.

    Local development keeps the constant, and only local development. Surrounding
    whitespace on a supplied value is stripped first: GitHub configuration
    routinely carries a trailing newline, and that is a transport artifact rather
    than a different object id.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(ACCEPTED_CHECKPOINT_ENV)
    supplied = "" if raw is None else raw.strip()

    if not supplied:
        if _is_hosted_execution(environ):
            return None, [
                (
                    f"{ACCEPTED_CHECKPOINT_ENV}: hosted acceptance must be supplied the accepted "
                    "contract checkpoint from repository-external GitHub configuration (a "
                    f"repository or environment variable), and it is "
                    f"{'unset' if raw is None else 'empty'}; the in-repository constant is a "
                    "non-authoritative local-development fallback and does not apply here"
                )
            ]
        return ACCEPTED_CONTRACT_CHECKPOINT, []

    if _FULL_OBJECT_ID.fullmatch(supplied) is None:
        return None, [
            (
                f"{ACCEPTED_CHECKPOINT_ENV}: the supplied accepted checkpoint {supplied!r} is "
                "not a full 40-character object id in canonical lowercase hex"
            )
        ]
    return supplied, []


def check_pattern_baseline_is_the_accepted_checkpoint() -> list[str]:
    """The baseline must be what the accepted checkpoint actually says.

    Without this the classifier is decorative. The baseline is an ordinary file
    in the tree, so the same commit that narrows a pattern can rewrite the
    baseline entry to match, and the comparison then finds "no change" and
    passes -- a breaking change published as compatible, which is precisely the
    outcome the gate exists to prevent.

    Anchoring it to a commit removes the option. A commit is immutable, so the
    baseline can only be moved forward to a state that has *already* been
    reviewed and committed, and the classifier's inputs stop being editable by
    the change under classification.

    An unreadable checkpoint is a failure, not a pass: a claim about a commit
    nobody can read is not evidence. If CI clones shallowly, deepen the fetch
    rather than skipping the check.

    The anchor it is compared against comes from
    :func:`_resolve_accepted_checkpoint`: repository-external GitHub
    configuration on a hosted run, the in-tree constant only on a laptop.
    """
    if not PATTERN_BASELINE.exists():
        return [f"{PATTERN_BASELINE.name}: missing; capture it from the accepted checkpoint"]
    baseline: Any = json.loads(PATTERN_BASELINE.read_text(encoding="utf-8"))
    checkpoint = baseline.get("accepted_checkpoint")

    # A canonical full object id, and nothing else. `HEAD`, a branch, a tag, an
    # abbreviation or any other revision expression is refused before it is
    # resolved -- because a symbolic name resolves to whatever the candidate
    # happens to be, and `"accepted_checkpoint": "HEAD"` would name the very
    # state this gate exists to judge.
    if not isinstance(checkpoint, str) or _FULL_OBJECT_ID.fullmatch(checkpoint) is None:
        return [
            (
                f"{PATTERN_BASELINE.name}: accepted_checkpoint must be a full 40-character "
                f"object id, got {checkpoint!r}; symbolic refs, abbreviations and revision "
                "expressions resolve against the candidate and cannot establish acceptance"
            )
        ]

    # Equality with a trust anchor obtained from outside this history. Ancestry
    # was the earlier rule and it is not acceptance: every earlier commit on a
    # branch is an ancestor, so a candidate could commit its own schemas, name
    # that commit, and certify itself. Acceptance is a fact about review, and the
    # only way to know it here is to be told by something the candidate does not
    # write. Hosted runs are told by repository-external GitHub configuration and
    # by nothing else; the in-tree constant answers only on a developer's laptop,
    # where the run certifies nothing anyway.
    expected, anchor_findings = _resolve_accepted_checkpoint()
    if expected is None:
        return anchor_findings
    if checkpoint != expected:
        return [
            (
                f"{PATTERN_BASELINE.name}: accepted_checkpoint {checkpoint[:7]} is not the "
                f"accepted contract checkpoint {expected[:7]}; the baseline may advance only "
                "to a separately accepted checkpoint, never to a commit authored by the "
                "change it is meant to judge"
            )
        ]

    if _git("cat-file", "-e", f"{checkpoint}^{{commit}}")[0] != 0:
        return [
            (
                f"{PATTERN_BASELINE.name}: accepted_checkpoint {checkpoint} is not a commit "
                "in this repository, so the baseline cannot be verified "
                "(deepen a shallow fetch)"
            )
        ]

    recorded = baseline.get("definitions", {})
    accepted = _definitions_at(checkpoint)
    if accepted is None:
        return [
            (
                f"{PATTERN_BASELINE.name}: the schemas cannot be read at {checkpoint}, "
                "so the baseline cannot be verified"
            )
        ]

    findings: list[str] = []
    for name in sorted(set(recorded) - set(accepted)):
        findings.append(
            f"{PATTERN_BASELINE.name}: records {name}, which does not exist at the accepted "
            f"checkpoint {checkpoint[:7]}"
        )
    for name in sorted(set(accepted) - set(recorded)):
        findings.append(
            f"{PATTERN_BASELINE.name}: omits {name}, which the accepted checkpoint "
            f"{checkpoint[:7]} publishes"
        )
    for name in sorted(set(recorded) & set(accepted)):
        if recorded[name] != accepted[name]:
            findings.append(
                f"{PATTERN_BASELINE.name}: {name} does not match the accepted checkpoint "
                f"{checkpoint[:7]}; the baseline may only be moved by capturing an accepted commit"
            )
    return findings


def check_scalar_pattern_compatibility() -> list[str]:
    """Classify every canonical pattern change against the accepted baseline.

    A pattern is the wire language. Narrowing one silently breaks producers that
    were conforming yesterday, so the default is major and manual. The one
    transformation this gate may wave through is the terminal end-of-input guard,
    because it removes only values the contract's own semantics and every
    ECMAScript client already refused -- a validator-parity correction rather than
    a language change.

    The baseline is a checked-in inventory captured from the last accepted
    checkpoint, so a change and its classification arrive in the same diff and a
    reviewer can see both.
    """
    if not PATTERN_BASELINE.exists():
        return [f"{PATTERN_BASELINE.name}: missing; capture it from the accepted checkpoint"]
    baseline: Any = json.loads(PATTERN_BASELINE.read_text(encoding="utf-8"))
    if baseline.get("format") != PATTERN_BASELINE_FORMAT:
        return [f"{PATTERN_BASELINE.name}: format must be {PATTERN_BASELINE_FORMAT!r}"]

    recorded: dict[str, Any] = baseline.get("definitions", {})
    current = _current_patterned_nodes()
    findings: list[str] = []

    for name in sorted(set(recorded) - set(current)):
        findings.append(
            f"{PATTERN_BASELINE.name}: {name} was removed; removing a published definition is "
            f"{CLASSIFICATION_MAJOR} and needs an accepted contract decision"
        )
    for name in sorted(set(current) - set(recorded)):
        findings.append(
            f"{PATTERN_BASELINE.name}: {name} is new and unrecorded; a new published pattern is "
            f"{CLASSIFICATION_MAJOR} and needs an accepted contract decision"
        )

    values = _corpus_probe_values()
    shared = sorted(set(recorded) & set(current))
    wanted: list[str] = []
    for name in shared:
        wanted.extend((recorded[name]["pattern"], current[name]["pattern"]))
    evaluated = _ecmascript_accepts(sorted(set(wanted)), values)
    ecmascript = (
        dict(zip(sorted(set(wanted)), evaluated, strict=True)) if evaluated is not None else None
    )

    for name in shared:
        classification, reason = _classify(name, recorded[name], current[name], values, ecmascript)
        if classification in {CLASSIFICATION_UNCHANGED, CLASSIFICATION_PARITY}:
            continue
        findings.append(f"{name}: classified {classification} -- {reason}")
    return findings


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_checks() -> list[str]:
    findings: list[str] = []
    findings += check_schema_directory_holds_exactly_the_frozen_schemas()
    findings += check_schemas_are_valid_draft_2020_12()
    findings += check_all_refs_resolve()
    findings += check_registry_matches_source_schemas()
    findings += check_schema_identity()
    findings += check_registry_sources_are_exact()
    findings += check_fixture_manifest_contract_version_matches_registry()
    findings += check_fixture_manifest_is_complete()
    findings += check_fixture_manifest_entries_are_well_formed()
    findings += check_fixture_manifest_matches_frozen_mapping()
    findings += check_fixtures_match_declared_schema_validity()
    findings += check_fixture_tolerant_decode_matches_declared()
    findings += check_fixture_semantics()
    findings += check_operation_catalogue_shape()
    findings += check_operation_catalogue_entries_are_valid()
    findings += check_operation_catalogue_schema_refs()
    findings += check_operation_catalogue_postures()
    findings += check_operation_catalogue_errors()
    findings += check_operation_catalogue_fixture_matches()
    findings += check_adapter_conformance_corpus()
    findings += check_contract_package_has_no_forbidden_imports()
    findings += check_generated_artifacts_match_schemas()
    findings += check_pattern_baseline_is_the_accepted_checkpoint()
    findings += check_scalar_pattern_compatibility()
    return findings


def main() -> int:
    findings = run_checks()
    if findings:
        print("Application contract conformance check FAILED:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("Application contract conformance check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
