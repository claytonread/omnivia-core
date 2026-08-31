"""T-0688 G3 clean-room RED baseline for the Workflow/Runtime/Gateway manifest.

Every test here is a characterization of what OmniVia Core does *today*. The
manifest describes fourteen invented conformance families that Core does not
implement; these tests pass by asserting the exact current rejection or the
exact current absence, never by asserting the planned product behavior. The
suite therefore goes green now and must be revisited -- not merely re-run --
when a family is actually implemented.

Nothing here imports, reads or executes any external project's source, tests,
schemas, fixtures, generated output or package contents. The only inputs are the
T-0688 manifest in this directory and the current `omnivia_core` contracts.
"""

from __future__ import annotations

import copy
import inspect
import ipaddress
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest

from omnivia_core.contracts.v1 import semantics_runtime as runtime
from omnivia_core.contracts.v1 import semantics_service as service
from omnivia_core.contracts.v1 import semantics_workflow as workflow
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

FIXTURE_PATH: Final = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "t0688-workflow-runtime-conformance-v1.json"
)

SCHEMA_VERSION: Final = "t0688-workflow-runtime-conformance/v1"

#: The exact fourteen families, in the exact manifest order.
EXPECTED_FAMILY_ORDER: Final = (
    "FX-WEFT-PROJECTION",
    "FX-WEFT-EDIT",
    "FX-WEFT-PORT",
    "FX-WEFT-BOUNDARY",
    "FX-WEFT-LOOP",
    "FX-WEFT-WAIT",
    "FX-WEFT-ACTIVITY",
    "FX-WEFT-BINDING",
    "FX-WEFT-BUNDLE",
    "FX-WEFT-POISON",
    "FX-WEFT-FENCE",
    "FX-WEFT-CORRUPTION",
    "FX-WEFT-HUMAN",
    "FX-WEFT-EGRESS",
)

#: The accepted canonical backlog work packages per family, in exact order.
EXPECTED_WORK_PACKAGES_BY_FAMILY: Final = MappingProxyType(
    {
        "FX-WEFT-PROJECTION": ("WEFT-BL-001", "WEFT-BL-016"),
        "FX-WEFT-EDIT": ("WEFT-BL-001", "WEFT-BL-009"),
        "FX-WEFT-PORT": ("WEFT-BL-001", "WEFT-BL-010"),
        "FX-WEFT-BOUNDARY": ("WEFT-BL-001", "WEFT-BL-011"),
        "FX-WEFT-LOOP": ("WEFT-BL-001", "WEFT-BL-012"),
        "FX-WEFT-WAIT": ("WEFT-BL-001", "WEFT-BL-014"),
        "FX-WEFT-ACTIVITY": ("WEFT-BL-001", "WEFT-BL-003"),
        "FX-WEFT-BINDING": ("WEFT-BL-001", "WEFT-BL-006"),
        "FX-WEFT-BUNDLE": ("WEFT-BL-001", "WEFT-BL-007"),
        "FX-WEFT-POISON": ("WEFT-BL-001", "WEFT-BL-002", "WEFT-BL-003"),
        "FX-WEFT-FENCE": ("WEFT-BL-001", "WEFT-BL-004", "WEFT-BL-017"),
        "FX-WEFT-CORRUPTION": ("WEFT-BL-001", "WEFT-BL-008"),
        "FX-WEFT-HUMAN": ("WEFT-BL-001", "WEFT-BL-014", "WEFT-BL-018"),
        "FX-WEFT-EGRESS": ("WEFT-BL-001", "WEFT-BL-004", "WEFT-BL-005"),
    }
)

_ENTRY_FIELDS: Final = frozenset(
    {
        "familyId",
        "workPackages",
        "domain",
        "plannedValidator",
        "behavioralRequirement",
        "inventedInput",
        "expectedBoundaryResult",
        "refusalCase",
        "canonicalOwner",
        "currentGapCode",
        "attestation",
    }
)

_ATTESTATION_TRUE: Final = ("independentlyAuthored",)
_ATTESTATION_FALSE: Final = (
    "derivedFromExternalSource",
    "copiedExternalSchema",
    "copiedExternalFixtureData",
    "copiedExternalTestCase",
    "copiedExternalGeneratedOutput",
    "translatedExternalCode",
    "inspectedExternalPackageContents",
    "containsCredentials",
    "performsNetworkCalls",
)
_ATTESTATION_FIELDS: Final = frozenset(
    ("statement", *_ATTESTATION_TRUE, *_ATTESTATION_FALSE)
)

_DOMAIN_MODULES: Final = {"workflow": workflow, "runtime": runtime, "gateway": service}
_PROBE_KINDS: Final = frozenset(
    {"workflow_unknown_contract", "workflow_unknown_fields", "planned_validator_absent"}
)

#: Documentation-only address space: RFC 5737 v4 blocks and the RFC 3849 v6 block.
_DOCUMENTATION_NETWORKS: Final = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)

_URL_RE: Final = re.compile(r"([a-z][a-z0-9+.\-]*)://([^/\s\"]+)")
_IPV4_RE: Final = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

#: Markers that would indicate a credential or live network activity in the data.
_CREDENTIAL_MARKERS: Final = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "begin rsa",
    "begin private key",
    "authorization:",
    "bearer ",
    "ssh-rsa",
    "akia",
)
_NETWORK_MARKERS: Final = ("http://", "ftp://", "ws://", "wss://", "socket", "curl ")

#: Markers that would indicate prohibited reuse of an external source.
_REUSE_MARKERS: Final = (
    "copied from",
    "copied verbatim",
    "derived from an external",
    "ported from",
    "adapted from",
    "transcribed from",
    "verbatim",
    "site-packages",
    "node_modules",
    "vendored",
    "upstream package",
    "external package",
    "decompiled",
)

_PACKAGE_ROOT: Final = Path(workflow.__file__).resolve().parents[2]


def _package_source() -> str:
    """Every Python and JSON source line the installed Core package ships."""
    chunks = [
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(_PACKAGE_ROOT.rglob("*"))
        if path.suffix in {".py", ".json"} and path.is_file()
    ]
    return "\n".join(chunks)


PACKAGE_SOURCE: Final = _package_source()


def _manifest() -> dict[str, Any]:
    parsed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


MANIFEST: Final = _manifest()
FIXTURES: Final = MANIFEST["fixtures"]
RED_IDS: Final = [f"RED-{entry['familyId']}" for entry in FIXTURES]


def _entry(family_id: str) -> dict[str, Any]:
    for entry in FIXTURES:
        if entry["familyId"] == family_id:
            return copy.deepcopy(entry)
    raise AssertionError(f"unknown family {family_id!r}")


def _strings(node: Any) -> list[str]:
    """Every string in the manifest tree, keys and values alike."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        found = list(node)
        for value in node.values():
            found.extend(_strings(value))
        return found
    if isinstance(node, list):
        return [text for item in node for text in _strings(item)]
    return []


# --------------------------------------------------------------------------
# Checkers. These are functions rather than inline assertions so the mutation
# tests at the bottom can prove each one fails closed on a tampered manifest.
# --------------------------------------------------------------------------


def _check_family_order(manifest: dict[str, Any]) -> None:
    families = [entry["familyId"] for entry in manifest["fixtures"]]
    assert len(families) == len(set(families)), "family ids must be unique"
    assert tuple(families) == EXPECTED_FAMILY_ORDER, (
        "family set and order must be exact"
    )


def _check_attestation(entry: dict[str, Any]) -> None:
    attestation = entry["attestation"]
    assert set(attestation) == _ATTESTATION_FIELDS, "attestation must be complete"
    assert (
        isinstance(attestation["statement"], str) and attestation["statement"].strip()
    )
    for field in _ATTESTATION_TRUE:
        assert attestation[field] is True, f"{field} must be true"
    for field in _ATTESTATION_FALSE:
        assert attestation[field] is False, f"{field} must be false"


def _check_unique_input_ids(manifest: dict[str, Any]) -> None:
    input_ids = [entry["inventedInput"]["inputId"] for entry in manifest["fixtures"]]
    assert len(input_ids) == len(set(input_ids)), "invented input ids must be unique"
    for input_id in input_ids:
        assert input_id.startswith("IN-T0688-"), (
            f"{input_id} is not an invented T-0688 id"
        )


def _check_no_prohibited_reuse(manifest: dict[str, Any]) -> None:
    for text in _strings(manifest):
        lowered = text.lower()
        for marker in _REUSE_MARKERS:
            assert marker not in lowered, (
                f"prohibited reuse marker {marker!r} in {text!r}"
            )


def _check_no_credentials_or_network(manifest: dict[str, Any]) -> None:
    for text in _strings(manifest):
        lowered = text.lower()
        for marker in (*_CREDENTIAL_MARKERS, *_NETWORK_MARKERS):
            assert marker not in lowered, f"prohibited marker {marker!r} in {text!r}"


def _check_documentation_only_endpoints(manifest: dict[str, Any]) -> None:
    for text in _strings(manifest):
        for scheme, authority in _URL_RE.findall(text):
            assert scheme == "https", f"non-https endpoint {text!r}"
            host = authority.split("@")[-1]
            # An IPv6 literal is bracketed; anything else ends at the port colon.
            host = (
                host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
            )
            _check_host(host, text)
        for literal in _IPV4_RE.findall(text):
            _check_host(literal, text)


def _check_host(host: str, context: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        assert host.endswith(".test"), (
            f"{host!r} is not a fake .test hostname ({context!r})"
        )
        return
    assert any(address in network for network in _DOCUMENTATION_NETWORKS), (
        f"{host!r} is not a documentation-only address ({context!r})"
    )


# --------------------------------------------------------------------------
# Manifest shape
# --------------------------------------------------------------------------


def test_red_baseline_manifest_top_level_shape() -> None:
    assert set(MANIFEST) == {"schemaVersion", "description", "provenance", "fixtures"}
    assert MANIFEST["schemaVersion"] == SCHEMA_VERSION
    assert isinstance(MANIFEST["description"], str) and MANIFEST["description"].strip()
    provenance = MANIFEST["provenance"]
    assert provenance["task"] == "T-0688 G3"
    assert provenance["repository"] == "omnivia-core"
    assert provenance["authoringMode"] == "clean-room"
    assert provenance["networkAccess"] == "none"
    assert provenance["credentials"] == "none"
    assert "expected-red" in provenance["baselineIntent"]
    assert isinstance(FIXTURES, list) and len(FIXTURES) == 14


def test_red_baseline_family_set_and_order_is_exact() -> None:
    _check_family_order(MANIFEST)


def test_red_baseline_input_ids_are_unique_and_invented() -> None:
    _check_unique_input_ids(MANIFEST)


def test_red_baseline_endpoints_are_documentation_only() -> None:
    _check_documentation_only_endpoints(MANIFEST)


def test_red_baseline_carries_no_credentials_or_network_activity() -> None:
    _check_no_credentials_or_network(MANIFEST)


def test_red_baseline_carries_no_prohibited_reuse_markers() -> None:
    _check_no_prohibited_reuse(MANIFEST)


@pytest.mark.parametrize("entry", FIXTURES, ids=RED_IDS)
def test_red_baseline_entry_shape(entry: dict[str, Any]) -> None:
    assert set(entry) == _ENTRY_FIELDS
    assert entry["domain"] in _DOMAIN_MODULES
    assert isinstance(entry["behavioralRequirement"], str)
    assert len(entry["behavioralRequirement"].split()) >= 20
    assert entry["canonicalOwner"].strip()
    assert entry["currentGapCode"].startswith("gap.t0688.")
    assert entry["plannedValidator"].startswith("validate_")

    packages = entry["workPackages"]
    assert packages and len(packages) == len(set(packages))
    assert tuple(packages) == EXPECTED_WORK_PACKAGES_BY_FAMILY[entry["familyId"]], (
        "work packages must be the exact canonical backlog ids, in order"
    )

    probe = entry["inventedInput"]["probe"]
    assert probe["kind"] in _PROBE_KINDS

    boundary = entry["expectedBoundaryResult"]
    assert set(boundary) == {
        "redClassification",
        "currentRejectionReason",
        "productBehaviorImplemented",
    }
    assert boundary["redClassification"] == probe["kind"]
    assert boundary["productBehaviorImplemented"] is False
    assert boundary["currentRejectionReason"].strip()

    refusal = entry["refusalCase"]
    assert set(refusal) == {
        "refusalId",
        "description",
        "expectedRefusalCode",
        "enforcedToday",
    }
    assert refusal["refusalId"].startswith("RF-T0688-")
    assert refusal["expectedRefusalCode"].startswith("omnivia.t0688.")
    assert refusal["enforcedToday"] is False


@pytest.mark.parametrize("entry", FIXTURES, ids=RED_IDS)
def test_red_baseline_attestation_is_complete(entry: dict[str, Any]) -> None:
    _check_attestation(entry)


# --------------------------------------------------------------------------
# RED characterizations against current OmniVia semantic entry points
# --------------------------------------------------------------------------


def _required_context(function: Callable[..., Any], value: Any) -> dict[str, Any]:
    """Fill every required keyword-only context parameter with `value`."""
    return {
        name: value
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
    }


@pytest.mark.parametrize("entry", FIXTURES, ids=RED_IDS)
def test_red_baseline_probe_is_refused_or_absent_today(entry: dict[str, Any]) -> None:
    probe = entry["inventedInput"]["probe"]
    expected_reason = entry["expectedBoundaryResult"]["currentRejectionReason"]
    kind = probe["kind"]

    if kind == "workflow_unknown_contract":
        # The Workflow oracle rejects the invented contract outright: it is not
        # in the published T-0679 registry, so no validator exists to run.
        assert probe["oracle"] == "validate_workflow_record"
        payload = probe["payload"]
        assert payload["contractName"] not in workflow.WORKFLOW_RECORD_VALIDATORS
        with pytest.raises(ContractSemanticError) as raised:
            workflow.validate_workflow_record(payload)
        assert str(raised.value) == expected_reason

    elif kind == "workflow_unknown_fields":
        # The current, narrower validator is real and accepts today's record --
        # and rejects the T-0688 delta fields as unknown. That rejection is the
        # RED baseline: Core has no field vocabulary for this family yet.
        oracle = getattr(workflow, probe["oracle"])
        base = probe["baseRecord"]
        oracle(copy.deepcopy(base))
        with pytest.raises(ContractSemanticError) as raised:
            oracle({**copy.deepcopy(base), **probe["deltaFields"]})
        assert str(raised.value) == expected_reason
        for field in probe["deltaFields"]:
            assert repr(field) in str(raised.value)

    else:
        # The planned validator does not exist anywhere in the v1 semantic
        # surface, and the narrower validator the manifest names cannot answer
        # this family's question: it refuses the scenario on shape alone and
        # never reaches the family's refusal code.
        module = _DOMAIN_MODULES[probe["module"]]
        planned = entry["plannedValidator"]
        assert not hasattr(module, planned)
        for candidate in _DOMAIN_MODULES.values():
            assert not hasattr(candidate, planned)
        assert module.__name__ in expected_reason
        assert planned in expected_reason

        narrower = getattr(module, probe["currentNarrowerValidator"])
        scenario = probe["scenario"]
        with pytest.raises(ContractSemanticError) as raised:
            narrower(scenario, **_required_context(narrower, scenario))
        assert entry["refusalCase"]["expectedRefusalCode"] not in str(raised.value)


def test_red_baseline_loop_family_exercises_the_current_loop_plan_validator() -> None:
    """The Loop family's oracle is the real, narrower LoopPlan validator today."""
    probe = _entry("FX-WEFT-LOOP")["inventedInput"]["probe"]
    base = probe["baseRecord"]

    # What the current validator does enforce.
    workflow.validate_loop_plan(copy.deepcopy(base))
    with pytest.raises(ContractSemanticError, match="frozenAtRunStart must be true"):
        workflow.validate_loop_plan({**copy.deepcopy(base), "frozenAtRunStart": False})
    with pytest.raises(
        ContractSemanticError, match="iterationLedgerRequired must be true"
    ):
        workflow.validate_loop_plan(
            {**copy.deepcopy(base), "iterationLedgerRequired": False}
        )

    # What it does not: nothing about iteration identity, atomic launch or crash
    # replay. Each new field is refused one at a time as unknown.
    for field, value in probe["deltaFields"].items():
        with pytest.raises(
            ContractSemanticError, match=f"unknown fields \\['{field}'\\]"
        ):
            workflow.validate_loop_plan({**copy.deepcopy(base), field: value})


def test_red_baseline_egress_uri_guard_cannot_see_a_connect_time_rebinding() -> None:
    """The current gateway guard is syntactic: it admits both bound addresses."""
    probe = _entry("FX-WEFT-EGRESS")["inventedInput"]["probe"]
    for uri in probe["syntacticallyAcceptedUris"]:
        service.validate_service_endpoint_uri(uri)
    scenario = probe["scenario"]
    assert scenario["admissionTimeAddress"] != scenario["connectTimeAddress"]
    assert scenario["simulationOnly"] is True


@pytest.mark.parametrize("entry", FIXTURES, ids=RED_IDS)
def test_red_baseline_refusal_and_gap_codes_are_unimplemented(
    entry: dict[str, Any],
) -> None:
    """No family's refusal or gap code appears anywhere in Core's source today."""
    for code in (entry["refusalCase"]["expectedRefusalCode"], entry["currentGapCode"]):
        assert code not in PACKAGE_SOURCE, f"{code} appears implemented"


# --------------------------------------------------------------------------
# Mutation tests: the checkers must fail closed on a tampered manifest.
# --------------------------------------------------------------------------


def test_red_baseline_mutation_missing_family_fails_closed() -> None:
    mutated = copy.deepcopy(MANIFEST)
    del mutated["fixtures"][7]
    with pytest.raises(AssertionError, match="family set and order must be exact"):
        _check_family_order(mutated)


def test_red_baseline_mutation_duplicate_input_id_fails_closed() -> None:
    mutated = copy.deepcopy(MANIFEST)
    mutated["fixtures"][1]["inventedInput"]["inputId"] = mutated["fixtures"][0][
        "inventedInput"
    ]["inputId"]
    with pytest.raises(AssertionError, match="invented input ids must be unique"):
        _check_unique_input_ids(mutated)


def test_red_baseline_mutation_false_independent_authorship_fails_closed() -> None:
    entry = _entry("FX-WEFT-PROJECTION")
    entry["attestation"]["independentlyAuthored"] = False
    with pytest.raises(AssertionError, match="independentlyAuthored must be true"):
        _check_attestation(entry)

    entry = _entry("FX-WEFT-PROJECTION")
    entry["attestation"]["derivedFromExternalSource"] = True
    with pytest.raises(AssertionError, match="derivedFromExternalSource must be false"):
        _check_attestation(entry)

    entry = _entry("FX-WEFT-PROJECTION")
    del entry["attestation"]["copiedExternalSchema"]
    with pytest.raises(AssertionError, match="attestation must be complete"):
        _check_attestation(entry)


def test_red_baseline_mutation_prohibited_reuse_fails_closed() -> None:
    mutated = copy.deepcopy(MANIFEST)
    mutated["fixtures"][0]["behavioralRequirement"] = (
        "Copied verbatim from an upstream package."
    )
    with pytest.raises(AssertionError, match="prohibited reuse marker"):
        _check_no_prohibited_reuse(mutated)

    mutated = copy.deepcopy(MANIFEST)
    mutated["fixtures"][0]["inventedInput"]["probe"]["payload"]["apiKey"] = "hunter2"
    with pytest.raises(AssertionError, match="prohibited marker"):
        _check_no_credentials_or_network(mutated)

    mutated = copy.deepcopy(MANIFEST)
    mutated["fixtures"][13]["inventedInput"]["probe"]["syntacticallyAcceptedUris"][
        0
    ] = "https://gateway-egress.example.com/dispatch"
    with pytest.raises(AssertionError, match="not a fake .test hostname"):
        _check_documentation_only_endpoints(mutated)
