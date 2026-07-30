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
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"
CONTRACTS_SRC = REPO_ROOT / "src" / "omnivia_core" / "contracts"
SRC_ROOT = REPO_ROOT / "src"
GENERATOR_SCRIPT = REPO_ROOT / "scripts" / "generate-application-contracts.py"

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
        path.name for path in FIXTURES_DIR.glob("*.json") if path.name != "manifest.json"
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
    findings += check_contract_package_has_no_forbidden_imports()
    findings += check_generated_artifacts_match_schemas()
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
