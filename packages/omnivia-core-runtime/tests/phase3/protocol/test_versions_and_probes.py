"""P2.1: the version foundation and the canonical probe router.

Two things are proved here. First, that the versions this build answers with are
*derived* -- from the public contract and from the package's own release version --
rather than transcribed, and that the one notation that genuinely differs (the
private workspace-format ordinal) is translated at a single strict boundary that
fails closed. That boundary knows one pairing, ordinal `1` to `1.0`; a well-formed
ordinal it has no pairing for is refused rather than continued into a version no
release ever defined.

Second, that the probe router answers exactly the three canonical probe kinds from
injected facts: no legacy or shortened spelling is accepted, discovery advertises
only capabilities a registered handler actually backs, health and readiness never
publish an endpoint descriptor, a successful answer publishes the frozen public
status facts and nothing a Runtime happened to attach to them, and nothing here
reaches for storage, lease or transport code to answer.
"""

from __future__ import annotations

import ast
import json
import re
import traceback
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from omnivia_core_runtime import __version__ as PACKAGE_VERSION
from omnivia_core_runtime.service import operations
from omnivia_core_runtime.service.operations import (
    ApplicationOperationRegistry,
    response_metadata,
)
from omnivia_core_runtime.service.probes import (
    CANONICAL_PROBES,
    PROBE_DISCOVER,
    PROBE_HEALTH,
    PROBE_READINESS,
    PUBLIC_COMPONENT_FIELDS,
    REQUEST_ID_DETAIL,
    ProbeDeadlineExceeded,
    ProbeError,
    ProbeRouter,
    ServiceFacts,
)
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    SUPPORTED_WORKSPACE_ORDINALS,
    WorkspaceVersionError,
    build_version_window,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)
from referencing import Registry, Resource

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CONTRACT_VERSION_PATTERN,
    OPERATION_CATALOGUE,
    RELEASE_VERSION_PATTERN,
    CapabilityRef,
    ClientIdentity,
    ContractSemanticError,
    RequestEnvelope,
    RequestMetadata,
    ServiceComponentStatus,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProcessEvidence,
    VersionWindow,
    validate_service_endpoint_descriptor,
    validate_version_capability_envelope,
    validate_version_window,
    version_in_window,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"

#: Values planted in the injected facts, standing in for what a real Runtime puts on
#: a component it is diagnosing: where its backend lives, who holds the lease, and
#: the exception it caught. None of them may reach an unauthenticated caller --
#: neither in an error message nor, which is the harder case, in a successful answer.
SECRET_PATH = "/Users/someone/Library/omnivia/workspace.sqlite3"
SECRET_TOKEN = "s3cr3t-lease-token"
SECRET_MESSAGE = f"OperationalError: unable to open database file {SECRET_PATH}"
SECRET_DETAILS = {
    "database": SECRET_PATH,
    "lease_token": SECRET_TOKEN,
    "holder_pid": 4242,
}

#: Every planted value, as it would appear inside a rendered wire document.
PLANTED = (
    SECRET_PATH,
    SECRET_TOKEN,
    SECRET_MESSAGE,
    "sqlite3",
    "lease_token",
    "OperationalError",
    "4242",
)

#: The socket a descriptor publishes. Also a filesystem path, and published on
#: purpose: P0 froze `endpoint_uri` as the coordination fact a client needs to reach
#: the instance at all. Kept distinct from `SECRET_PATH` so a leak assertion is not
#: quietly satisfied -- or quietly broken -- by the one path that is meant to travel.
#: The distinction is now the policy's too: `SECRET_PATH` names a workspace database
#: and is refused as an endpoint for exactly that reason, while this one is the
#: bounded `.sock` transport address `ServiceEndpointUri` permits a `unix:///` value
#: to be.
#:
#: Percent-encoded, because the accepted pattern admits a space only as `%20`. The
#: unencoded spelling is what a Runtime building this by string concatenation would
#: produce, and `test_an_endpoint_uri_that_is_not_a_uri_is_refused` is where it is
#: refused.
ENDPOINT_URI = "unix:///Users/someone/Library/Application%20Support/omnivia/core.sock"

#: The same socket, spelled the way a naive `f"unix://{path}"` would spell it.
UNENCODED_ENDPOINT_URI = (
    "unix:///Users/someone/Library/Application Support/omnivia/core.sock"
)

MS = 1_000_000  # nanoseconds per millisecond


class FakeClock:
    """A monotonic clock in nanoseconds, driven by the test rather than the OS."""

    def __init__(self, *readings: int) -> None:
        self._readings = list(readings) or [0]
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if len(self._readings) > 1:
            return self._readings.pop(0)
        return self._readings[0]


def _descriptor(workspace_ordinal: object = "1") -> ServiceEndpointDescriptor:
    """A P0 endpoint descriptor, built from the derived versions under test.

    `workspace_ordinal` is a parameter so a test can try to build a descriptor for a
    workspace format this build does not know, and find that it cannot.
    """
    return ServiceEndpointDescriptor(
        descriptor_version=API_VERSION,
        workspace_id="ws-probe-fixture",
        service_instance_id="svc-instance-1",
        installation_id="install-1",
        endpoint_uri=ENDPOINT_URI,
        protocol_version=PROTOCOL_VERSION,
        server_version=SERVER_VERSION,
        supported_api_versions=supported_api_versions(),
        supported_workspace_versions=supported_workspace_versions(workspace_ordinal),
        workspace_format_version=workspace_contract_version(workspace_ordinal),
        ready=True,
        lifecycle_state="ready",
        fencing_generation=7,
        published_at=OBSERVED_AT,
    )


def _facts(**overrides: object) -> ServiceFacts:
    """The default snapshot, whose one component is deliberately full of secrets.

    A real Runtime writes exactly this kind of thing on a component it is diagnosing.
    Making it the default rather than a special fixture means every assertion in this
    file that renders a result is also, incidentally, a leak check.
    """
    defaults: dict[str, object] = {
        "observed_at": OBSERVED_AT,
        "health_status": "pass",
        "readiness_status": "warn",
        "discovery_status": "pass",
        "components": (
            ServiceComponentStatus(
                id="storage",
                status="pass",
                observed_at=OBSERVED_AT,
                message=SECRET_MESSAGE,
                details=SECRET_DETAILS,
            ),
        ),
    }
    defaults.update(overrides)
    return ServiceFacts(**defaults)  # type: ignore[arg-type]


def _inventory(registry: ApplicationOperationRegistry) -> tuple[CapabilityRef, ...]:
    """The capability inventory a registry actually backs, at the API version."""
    return tuple(
        CapabilityRef(id=name, version=API_VERSION)
        for name in sorted(registry.operations)
    )


def _router(
    facts: ServiceFacts | None = None,
    *,
    capabilities: Sequence[CapabilityRef] = (),
    clock: FakeClock | None = None,
) -> ProbeRouter:
    snapshot = _facts() if facts is None else facts
    return ProbeRouter(
        facts=lambda: snapshot,
        capabilities=lambda: capabilities,
        clock=FakeClock(0) if clock is None else clock,
    )


# --- the canonical schema, as the arbiter of what may be published ------------
#
# The router's own tests cannot be the whole proof that what it publishes is
# publishable: they were written by whoever wrote the projection, and they agreed
# with it. `contracts/application/v1/schemas` is the checked-in canonical copy
# (ADR-038) and it did not, so every result accepted below is also validated
# against it -- strictly, with `unevaluatedProperties: false` refusing a field the
# contract does not define and `format_checker` refusing a `Timestamp` that names
# no instant and an `endpoint_uri` that is not a URI.

REPO_ROOT = Path(__file__).resolve().parents[5]
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
SCHEMA_BASE_URI = "https://contracts.omnivia.dev/application/v1/"
_SCHEMA_NAMES = ("common", "compatibility", "service")

#: The accepted `ServiceEndpointUri` corpus, checked in beside the contract that
#: froze it: which endpoints publish, and which are refused and why.
#:
#: Read rather than restated. The generated pattern is the single authority for the
#: policy, and a Runtime test spelling out its own approved and refused lists would
#: be a second one -- free to drift, and drifting silently, since nothing would
#: compare the two. Driving the regressions below from this file means Runtime is
#: asserted to publish exactly what Core accepts, case for case.
ENDPOINT_POLICY: dict[str, Any] = json.loads(
    (
        REPO_ROOT
        / "tests"
        / "contracts"
        / "fixtures"
        / "service-endpoint-uri-policy-v1.json"
    ).read_text(encoding="utf-8")
)


def _policy(group: str) -> list[Any]:
    """The fixture's `accepted` or `rejected` endpoints, as named test cases."""
    cases = ENDPOINT_POLICY[group]
    assert cases, f"the endpoint policy fixture names no {group} case"
    return [pytest.param(case["endpoint_uri"], id=case["id"]) for case in cases]


#: The whole of what a caller is told about an endpoint that will not publish.
#:
#: Fixed, and pinned here rather than matched loosely, because a fixed string is the
#: proof that nothing was echoed: the refused value may be the credential, query or
#: workspace database this boundary exists to keep private, and Core's own refusal
#: -- which is a library's message to its caller, not this boundary's to an
#: unauthenticated one -- is not passed on either.
ENDPOINT_REFUSAL = (
    "service facts descriptor endpoint_uri is not an approved transport endpoint"
)


def _registry() -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for name in _SCHEMA_NAMES:
        document = json.loads(
            (SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


PROBE_RESULT_VALIDATOR = Draft202012Validator(
    {"$ref": f"{SCHEMA_BASE_URI}service.schema.json#/$defs/ServiceProbeResult"},
    registry=_registry(),
    format_checker=Draft202012Validator.FORMAT_CHECKER,
)


def _schema_errors(document: object) -> list[str]:
    """Every way `document` fails the canonical `ServiceProbeResult` schema."""
    return [
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in PROBE_RESULT_VALIDATOR.iter_errors(document)
    ]


def _published(
    facts: ServiceFacts | None = None,
    *,
    probe: str = PROBE_HEALTH,
    capabilities: Sequence[CapabilityRef] = (),
    request_id: str | None = None,
) -> dict[str, Any]:
    """Answer one probe and return the wire document, proven schema-valid.

    Every accepted answer in this file goes through here, so "the router published
    it" and "the contract permits it" are never two separate claims.
    """
    result = _router(facts, capabilities=capabilities).route(
        ServiceProbeRequest(probe=probe, request_id=request_id)
    )
    wire: dict[str, Any] = result.to_wire()
    assert _schema_errors(wire) == []
    return wire


#: A bounded value at exactly its `maxLength`, and one character past it. The
#: schema is read for the bound rather than the number being written down twice.
def _max_length(document: str, definition: str) -> int:
    schemas = json.loads(
        (SCHEMA_DIR / f"{document}.schema.json").read_text(encoding="utf-8")
    )
    bound = schemas["$defs"][definition]["maxLength"]
    assert isinstance(bound, int)
    return bound


# --- derived versions ---------------------------------------------------------


def test_api_version_is_derived_from_the_public_contract() -> None:
    assert API_VERSION == CONTRACT_VERSION
    assert re.fullmatch(CONTRACT_VERSION_PATTERN, API_VERSION)
    # The literal the earlier runtime hard-coded. Derivation is the point: the
    # contract has since moved to 1.2 and a transcribed copy would still say 1.0.
    assert API_VERSION != "1.0"


def test_server_version_is_derived_from_the_package_release_version() -> None:
    assert SERVER_VERSION == PACKAGE_VERSION
    assert re.fullmatch(RELEASE_VERSION_PATTERN, SERVER_VERSION)


def test_protocol_version_is_exactly_one_point_zero_and_not_the_api_version() -> None:
    assert PROTOCOL_VERSION == "1.0"
    # Independent by design: the OVC1 framing does not move because the
    # application contract released an additive minor.
    assert PROTOCOL_VERSION != API_VERSION


# --- workspace ordinal translation, and what it refuses -----------------------


@pytest.mark.parametrize(("ordinal", "expected"), [("1", "1.0"), (1, "1.0")])
def test_the_one_frozen_workspace_ordinal_translates_to_its_frozen_version(
    ordinal: object, expected: str
) -> None:
    assert workspace_contract_version(ordinal) == expected


def test_exactly_one_workspace_format_pairing_is_frozen() -> None:
    """The table is the claim; a formula would be an open-ended one."""
    assert dict(SUPPORTED_WORKSPACE_ORDINALS) == {1: "1.0"}


@pytest.mark.parametrize("ordinal", ["2", 2, "3", 3, "12", 12, "99", 99, 2**31])
def test_unknown_positive_ordinals_fail_closed_rather_than_extrapolating(
    ordinal: object,
) -> None:
    """`2` is a well-formed ordinal and still names no workspace format.

    The arithmetic translation would answer `"2.0"` here: a version no release has
    defined, arrived at by continuing a pattern rather than by knowing a fact. A
    build that publishes it is claiming to speak a workspace format that does not
    exist, and a peer negotiating against the claim gets no warning.
    """
    with pytest.raises(WorkspaceVersionError, match="not a format this build knows"):
        workspace_contract_version(ordinal)
    with pytest.raises(WorkspaceVersionError):
        supported_workspace_versions(ordinal)


@pytest.mark.parametrize(
    "ordinal",
    [
        "1.0",  # already a contract version, so not an ordinal
        "1.2",
        "01",  # noncanonical spelling of 1
        "0",
        0,
        "-1",
        -1,
        "",
        " 1",
        "1 ",
        "+1",
        "1\n",
        "١",  # non-ASCII digit int() would accept
        "one",
        True,  # would otherwise translate to "1.0"
        False,
        1.0,
        None,
        b"1",
        ("1",),
    ],
)
def test_malformed_workspace_ordinals_are_refused_rather_than_guessed(
    ordinal: object,
) -> None:
    with pytest.raises(WorkspaceVersionError):
        workspace_contract_version(ordinal)


def test_workspace_version_error_is_a_value_error() -> None:
    assert issubclass(WorkspaceVersionError, ValueError)


def test_an_unknown_ordinal_cannot_reach_a_descriptor_or_a_probe() -> None:
    """The refusal holds where it matters: nothing unsupported is ever published.

    Two links in the same chain. A descriptor for an unknown workspace format is
    never constructed, so discovery has nothing invented to publish; and the
    descriptor discovery *does* publish states the frozen `1.0` on both fields that
    carry a workspace version, rather than whatever an ordinal was continued into.
    """
    for unknown in ("2", 2, "99"):
        with pytest.raises(WorkspaceVersionError):
            _descriptor(workspace_ordinal=unknown)

    published = _router(_facts(descriptor=_descriptor())).route(
        ServiceProbeRequest(probe=PROBE_DISCOVER)
    )
    descriptor = published.to_wire()["descriptor"]
    assert descriptor["workspace_format_version"] == "1.0"
    assert descriptor["supported_workspace_versions"] == {
        "minimum": "1.0",
        "maximum": "1.0",
    }


# --- version windows ----------------------------------------------------------


def test_version_window_construction_uses_public_shapes_and_is_inclusive() -> None:
    window = build_version_window("1.0", "1.2")
    assert isinstance(window, VersionWindow)
    assert (window.minimum, window.maximum) == ("1.0", "1.2")
    assert version_in_window("1.0", window)
    assert version_in_window("1.2", window)
    assert not version_in_window("1.3", window)


def test_reversed_and_cross_major_windows_are_refused() -> None:
    with pytest.raises(ContractSemanticError, match="reversed"):
        build_version_window("1.2", "1.0")
    with pytest.raises(ContractSemanticError, match="spans majors"):
        build_version_window("1.2", "2.0")
    with pytest.raises(ContractSemanticError):
        build_version_window("1", "1.2")


def test_supported_windows_are_derived_not_transcribed() -> None:
    assert supported_api_versions() == VersionWindow(
        minimum=CONTRACT_VERSION, maximum=CONTRACT_VERSION
    )
    assert supported_workspace_versions("1") == VersionWindow(
        minimum="1.0", maximum="1.0"
    )
    with pytest.raises(WorkspaceVersionError):
        supported_workspace_versions("1.0")


# --- the versions a response states -------------------------------------------


def _request(api_version: str) -> RequestEnvelope:
    """A request claiming `api_version`, built without a client package.

    Assembled from the public contract types directly: this is a phase2/phase3
    protocol module, and the point is what a *server* asserts in reply to an
    arbitrary claim, not what any particular client happens to send.
    """
    return RequestEnvelope(
        operation="core.health",
        metadata=RequestMetadata(
            request_id="req-versions",
            correlation_id="req-versions",
            trace_id="req-versions",
            api_version=api_version,
            client=ClientIdentity(id="probe-test", version="0.1.0"),
            workspace_id="ws-versions-0001",
            scopes=(),
            purpose="test",
            required_capabilities=(),
        ),
        input={},
    )


def test_a_response_states_derived_versions_not_the_callers_claim() -> None:
    """The response's version facts are anchored to the same sources the descriptor
    and the probes are anchored to.

    Anchored to `CONTRACT_VERSION` and the package's own `__version__` rather than
    to the runtime helpers that produce them, so this cannot be satisfied by a
    response and a helper agreeing with each other about a wrong value. The claim is
    a version this build does not serve, which is what the old code echoed into all
    of these fields.
    """
    metadata = response_metadata(_request("1.0"), principal="p", granted=("core.health",))
    envelope = metadata.version
    assert envelope.compatibility.supported_api_versions == VersionWindow(
        minimum=CONTRACT_VERSION, maximum=CONTRACT_VERSION
    )
    assert envelope.api_version == CONTRACT_VERSION
    assert envelope.compatibility.selected_api_version == CONTRACT_VERSION
    # `server_version` was a transcribed `"0.1.0"` in the response builder while the
    # descriptor derived its own from the package. Two literals that happened to
    # match is not the same as one source.
    assert envelope.server_version == PACKAGE_VERSION
    assert [ref.version for ref in envelope.capabilities.supported] == [CONTRACT_VERSION]
    assert envelope.workspace_format_version == SUPPORTED_WORKSPACE_ORDINALS[1]


def test_the_selected_version_is_negotiated_against_the_window_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selected version comes from a different source than the served window.

    This build serves a one-version window, so every claim resolves to the same
    string and the negotiation rule cannot be told apart from a constant. Widening
    the window this build reports is the only way to make the rule observable, so
    that is what this does -- and with it observable, both halves hold: a claim the
    server serves is the version it applies, and a claim it does not serve is
    replaced by the best it does, never carried through.

    That distinction is the whole repair. While the window, the selected version and
    the in-force version were all computed from one claimed value, they agreed no
    matter what was claimed, and `validate_version_capability_envelope` -- which
    checks that the selected version falls inside the published window -- could not
    fail. Here the two sides are independent, so the assertion below is a real one.
    """
    wide = build_version_window("1.0", "1.3")
    monkeypatch.setattr(operations, "supported_api_versions", lambda: wide)

    for claim, selected, status in (
        ("1.0", "1.0", "compatible"),
        ("1.3", "1.3", "compatible"),
        # Same major, above the window: no revision the caller named exists here.
        ("1.4", "1.3", "upgrade_required"),
        # A major that can never be negotiated.
        ("2.0", "1.3", "incompatible"),
    ):
        envelope = response_metadata(
            _request(claim), principal="p", granted=()
        ).version
        assert envelope.compatibility.selected_api_version == selected, claim
        assert envelope.api_version == selected, claim
        assert envelope.compatibility.status == status, claim
        assert envelope.compatibility.supported_api_versions == wide, claim
        # The rule the validator enforces, now that it has two sources to compare.
        validate_version_capability_envelope(envelope)
        assert version_in_window(
            envelope.compatibility.selected_api_version,
            envelope.compatibility.supported_api_versions,
        ), claim


# --- the three canonical probes ----------------------------------------------


@pytest.mark.parametrize("probe", sorted(CANONICAL_PROBES))
def test_every_canonical_probe_is_answered_with_derived_versions(probe: str) -> None:
    result = _router().route(ServiceProbeRequest(probe=probe))
    assert result.probe == probe
    assert result.server_version == SERVER_VERSION
    assert result.api_version == API_VERSION
    assert result.observed_at == OBSERVED_AT
    assert result.to_wire()["probe"] == probe


def test_canonical_probes_are_exactly_the_three_frozen_kinds() -> None:
    assert CANONICAL_PROBES == frozenset(
        {"service.health", "service.readiness", "service.discover"}
    )
    assert (PROBE_HEALTH, PROBE_READINESS, PROBE_DISCOVER) == (
        "service.health",
        "service.readiness",
        "service.discover",
    )


@pytest.mark.parametrize(
    "probe",
    [
        # The names the earlier in-process operation registry used. They were
        # operations dispatched through the application path, not probe kinds.
        "core.health",
        "core.readiness",
        "core.discovery",
        # Shortened spellings.
        "health",
        "readiness",
        "discover",
        "discovery",
        # Near misses on the canonical spelling.
        "service.discovery",
        "service.health.v1",
        "service_health",
        "Service.Health",
        "SERVICE.HEALTH",
        "service.health ",
        " service.health",
        "",
    ],
)
def test_legacy_short_and_near_miss_probe_spellings_are_refused(probe: str) -> None:
    with pytest.raises(ProbeError, match="unknown probe"):
        _router().route(ServiceProbeRequest(probe=probe))


def test_health_and_readiness_report_typed_components_and_their_own_status() -> None:
    router = _router()
    health = router.route(ServiceProbeRequest(probe=PROBE_HEALTH))
    readiness = router.route(ServiceProbeRequest(probe=PROBE_READINESS))

    assert health.status == "pass"
    assert readiness.status == "warn"
    for result in (health, readiness):
        assert result.components is not None
        assert all(
            isinstance(component, ServiceComponentStatus)
            for component in result.components
        )
        assert result.components[0].id == "storage"


def test_health_and_readiness_publish_no_descriptor_and_no_capabilities() -> None:
    """A descriptor in the facts is still discovery's answer, not health's."""
    router = _router(
        _facts(descriptor=_descriptor()),
        capabilities=(CapabilityRef(id="memory.search", version=API_VERSION),),
    )
    for probe in (PROBE_HEALTH, PROBE_READINESS):
        result = router.route(ServiceProbeRequest(probe=probe))
        assert result.descriptor is None
        assert result.supported_capabilities is None
        assert "descriptor" not in result.to_wire()
        assert "supported_capabilities" not in result.to_wire()


def test_discovery_publishes_the_descriptor_when_one_is_available() -> None:
    descriptor = _descriptor()
    result = _router(_facts(descriptor=descriptor)).route(
        ServiceProbeRequest(probe=PROBE_DISCOVER)
    )
    assert result.descriptor is descriptor
    assert result.to_wire()["descriptor"]["fencing_generation"] == 7


def test_discovery_omits_the_descriptor_when_none_is_published_yet() -> None:
    result = _router().route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    assert result.descriptor is None
    assert "descriptor" not in result.to_wire()
    # Still a real answer: an instance without a published descriptor is
    # discoverable in every other respect.
    assert result.status == "pass"


def test_discovery_reports_no_components() -> None:
    """Components describe how a process is doing, not what it is."""
    result = _router().route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    assert result.components is None


# --- supported capabilities ---------------------------------------------------


def test_discovery_advertises_only_capabilities_a_handler_actually_backs() -> None:
    catalogue_names = sorted(entry.name for entry in OPERATION_CATALOGUE)
    registered = catalogue_names[:2]

    registry = ApplicationOperationRegistry()
    for name in registered:
        registry.register(name, lambda _context: {})

    result = _router(capabilities=_inventory(registry)).route(
        ServiceProbeRequest(probe=PROBE_DISCOVER)
    )

    assert result.supported_capabilities is not None
    advertised = sorted(ref.id for ref in result.supported_capabilities)
    assert advertised == registered
    # The catalogue is far wider than what has handlers; advertising all of it
    # would claim support this build does not have.
    assert len(advertised) < len(catalogue_names)
    assert set(advertised) < set(catalogue_names)


def test_an_empty_registry_advertises_nothing_rather_than_the_catalogue() -> None:
    inventory = _inventory(ApplicationOperationRegistry())
    result = _router(capabilities=inventory).route(
        ServiceProbeRequest(probe=PROBE_DISCOVER)
    )
    assert result.supported_capabilities == ()
    assert result.to_wire()["supported_capabilities"] == []


def test_discovery_never_states_granted_or_effective_authority() -> None:
    router = _router(
        _facts(descriptor=_descriptor()),
        capabilities=(CapabilityRef(id="memory.search", version=API_VERSION),),
    )
    wire = router.route(ServiceProbeRequest(probe=PROBE_DISCOVER)).to_wire()
    assert set(wire) <= {
        "probe",
        "status",
        "server_version",
        "api_version",
        "observed_at",
        "components",
        "supported_capabilities",
        "descriptor",
        "details",
    }
    rendered = repr(wire)
    for forbidden in ("granted", "effective", "authority", "principal"):
        assert forbidden not in rendered


def test_a_duplicated_or_mistyped_capability_inventory_is_refused() -> None:
    duplicated = (
        CapabilityRef(id="memory.search", version=API_VERSION),
        CapabilityRef(id="memory.search", version="1.1"),
    )
    with pytest.raises(ProbeError, match="more than once"):
        _router(capabilities=duplicated).route(
            ServiceProbeRequest(probe=PROBE_DISCOVER)
        )

    mistyped: tuple[object, ...] = ("memory.search",)
    with pytest.raises(ProbeError, match="not a capability reference"):
        _router(capabilities=mistyped).route(  # type: ignore[arg-type]
            ServiceProbeRequest(probe=PROBE_DISCOVER)
        )


# --- deadlines ----------------------------------------------------------------


def test_a_probe_answered_inside_its_budget_is_returned() -> None:
    clock = FakeClock(0, 5 * MS)
    result = _router(clock=clock).route(
        ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=10)
    )
    assert result.status == "pass"
    assert clock.calls == 2


def test_a_probe_that_outran_its_budget_is_refused() -> None:
    clock = FakeClock(0, 25 * MS)
    with pytest.raises(ProbeDeadlineExceeded, match="10ms budget"):
        _router(clock=clock).route(
            ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=10)
        )


@pytest.mark.parametrize("deadline_ms", [0, -1])
def test_a_probe_arriving_with_no_budget_is_refused_before_any_work(
    deadline_ms: int,
) -> None:
    def unusable() -> ServiceFacts:  # pragma: no cover - must never be called
        raise AssertionError("facts were gathered despite an expired budget")

    router = ProbeRouter(facts=unusable, capabilities=tuple, clock=FakeClock(0))
    with pytest.raises(ProbeDeadlineExceeded, match="no time budget"):
        router.route(ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=deadline_ms))


def test_a_probe_without_a_deadline_reads_the_clock_once_and_answers() -> None:
    clock = FakeClock(0, 10**12)
    result = _router(clock=clock).route(ServiceProbeRequest(probe=PROBE_HEALTH))
    assert result.status == "pass"
    assert clock.calls == 1


def test_deadline_exceeded_is_a_probe_error() -> None:
    assert issubclass(ProbeDeadlineExceeded, ProbeError)


# --- request id echo ----------------------------------------------------------


def test_a_request_id_is_echoed_where_the_accepted_dto_allows_it() -> None:
    result = _router().route(
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id="req-42")
    )
    assert result.details is not None
    assert result.details[REQUEST_ID_DETAIL] == "req-42"
    # The accepted DTO has no top-level request_id, and none is invented.
    assert "request_id" not in result.to_wire()


def test_no_details_are_fabricated_when_there_is_nothing_to_report() -> None:
    result = _router(_facts(components=())).route(
        ServiceProbeRequest(probe=PROBE_HEALTH)
    )
    assert result.details is None
    assert "details" not in result.to_wire()


@pytest.mark.parametrize("probe", sorted(CANONICAL_PROBES))
def test_details_carry_the_echo_and_nothing_the_server_observed(probe: str) -> None:
    """`details` is an echo channel, not an observation channel."""
    result = _router(_facts(descriptor=_descriptor())).route(
        ServiceProbeRequest(probe=probe, request_id="req-42")
    )
    assert result.details == {REQUEST_ID_DETAIL: "req-42"}


def test_service_facts_have_nowhere_to_put_free_form_details() -> None:
    """The escape hatch is removed, not filtered.

    A snapshot with a details field is one refactor away from carrying a connection
    string, and no reviewer of that refactor would be looking at this file. There is
    no field to fill, so there is no such refactor.
    """
    assert not hasattr(_facts(), "details")
    with pytest.raises(TypeError):
        ServiceFacts(  # type: ignore[call-arg]
            observed_at=OBSERVED_AT,
            health_status="pass",
            readiness_status="pass",
            discovery_status="pass",
            details={"database": SECRET_PATH},
        )


# --- the display-safe public projection ---------------------------------------


@pytest.mark.parametrize("probe", [PROBE_HEALTH, PROBE_READINESS])
def test_a_successful_answer_publishes_only_the_frozen_component_fields(
    probe: str,
) -> None:
    """The hard case: a probe that *succeeds*, from facts full of secrets.

    Refusing to leak on the error path is the easy half, and the earlier candidate
    already did it. This is the half that was open: the component is `pass`, nothing
    is wrong, the answer goes out -- carrying, until now, the backend path and lease
    token a Runtime had attached to it for its own logs.
    """
    result = _router().route(ServiceProbeRequest(probe=probe, request_id="req-42"))
    wire = result.to_wire()

    assert wire["components"] == [
        {"id": "storage", "status": "pass", "observed_at": OBSERVED_AT}
    ]
    assert wire["details"] == {REQUEST_ID_DETAIL: "req-42"}

    rendered = repr(wire)
    for planted in PLANTED:
        assert planted not in rendered


def test_the_published_component_fields_are_exactly_the_allowlist() -> None:
    """Pinned as a set of field names, because that is what the guarantee is.

    Not "no value that looks like a secret": which values are dangerous cannot be
    recognized from the value. Which fields are safe to publish can be written down,
    and this is the writing down.
    """
    assert PUBLIC_COMPONENT_FIELDS == ("id", "status", "observed_at")

    wire = _router().route(ServiceProbeRequest(probe=PROBE_HEALTH)).to_wire()
    for component in wire["components"]:
        assert tuple(component) == PUBLIC_COMPONENT_FIELDS


def test_the_injected_component_is_projected_rather_than_forwarded() -> None:
    """Rebuilt, not the same object with its risky fields ignored downstream."""
    injected = _facts().components[0]
    result = _router().route(ServiceProbeRequest(probe=PROBE_HEALTH))

    assert result.components is not None
    published = result.components[0]
    assert published is not injected
    assert (published.id, published.status, published.observed_at) == (
        injected.id,
        injected.status,
        injected.observed_at,
    )
    assert published.message is None
    assert published.details is None
    # The snapshot itself is untouched: the Runtime still has its own diagnostics.
    assert injected.message == SECRET_MESSAGE


def test_a_successful_discovery_answer_publishes_no_component_free_text() -> None:
    """Discovery reports no components at all, so there is nothing to project."""
    result = _router(_facts(descriptor=_descriptor())).route(
        ServiceProbeRequest(probe=PROBE_DISCOVER, request_id="req-42")
    )
    assert result.components is None

    rendered = repr(result.to_wire())
    for planted in PLANTED:
        assert planted not in rendered
    # What discovery *does* publish is the descriptor P0 froze, endpoint included.
    assert result.to_wire()["descriptor"]["endpoint_uri"] == ENDPOINT_URI


# --- malformed facts ----------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"observed_at": "yesterday"}, "observed_at"),
        ({"observed_at": ""}, "observed_at"),
        ({"health_status": "PASS"}, "health_status"),
        ({"readiness_status": ""}, "readiness_status"),
        ({"discovery_status": "not a status"}, "discovery_status"),
        ({"components": ("storage",)}, "not a component status"),
        ({"descriptor": {"endpoint_uri": "unix:///tmp/x"}}, "endpoint descriptor"),
    ],
)
def test_malformed_injected_facts_raise_a_typed_probe_error(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ProbeError, match=re.escape(message)):
        _router(_facts(**overrides)).route(ServiceProbeRequest(probe=PROBE_HEALTH))


def test_a_malformed_component_is_reported_by_position_not_by_content() -> None:
    facts = _facts(
        components=(
            ServiceComponentStatus(
                id="lease",
                status="FAILED",
                observed_at=OBSERVED_AT,
                message=f"lease at {SECRET_PATH} is held by pid 4242",
            ),
        )
    )
    with pytest.raises(ProbeError) as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_READINESS))
    assert "component 0" in str(raised.value)
    assert SECRET_PATH not in str(raised.value)


@pytest.mark.parametrize(
    "probe", ["service.health", "service.readiness", "service.discover", "core.health"]
)
def test_probe_errors_leak_no_paths_secrets_or_lease_internals(probe: str) -> None:
    facts = _facts(observed_at="not-a-timestamp", descriptor=_descriptor())
    with pytest.raises(ProbeError) as raised:
        _router(facts, capabilities=()).route(ServiceProbeRequest(probe=probe))

    message = str(raised.value)
    for leaked in (*PLANTED, ENDPOINT_URI, "fencing"):
        assert leaked not in message


def test_the_public_fencing_generation_is_still_publishable_on_a_descriptor() -> None:
    """The one internal-sounding fact discovery *may* state, because P0 accepted it."""
    result = _router(_facts(descriptor=_descriptor())).route(
        ServiceProbeRequest(probe=PROBE_DISCOVER)
    )
    assert result.descriptor is not None
    assert result.descriptor.fencing_generation == 7


# --- every accepted answer is one the contract permits -------------------------


@pytest.mark.parametrize("probe", sorted(CANONICAL_PROBES))
def test_an_accepted_answer_validates_against_the_canonical_schema(probe: str) -> None:
    """The result the router publishes, checked by the schema rather than by us.

    Strict: `unevaluatedProperties: false` refuses a field the contract does not
    define, and the format checker refuses a `Timestamp` that names no instant and
    an `endpoint_uri` that is not a URI. A projection can be perfectly consistent
    with itself and still publish something no conforming client can read, and this
    is the check that would notice.
    """
    wire = _published(
        _facts(
            descriptor=replace(
                _descriptor(),
                process=ServiceProcessEvidence(
                    pid=4242, start_time="1728000000", boot_id="boot-1"
                ),
            )
        ),
        probe=probe,
        capabilities=(CapabilityRef(id="memory.search", version=API_VERSION),),
        request_id="req-42",
    )
    assert wire["probe"] == probe


def test_the_accepted_snapshot_keeps_its_private_fields_out_of_the_answer() -> None:
    """Schema-valid and still narrow: the projection is not widened to validate."""
    wire = _published(request_id="req-42")

    assert wire["components"] == [
        {"id": "storage", "status": "pass", "observed_at": OBSERVED_AT}
    ]
    for component in wire["components"]:
        assert "message" not in component
        assert "details" not in component
    assert wire["details"] == {REQUEST_ID_DETAIL: "req-42"}
    rendered = repr(wire)
    for planted in PLANTED:
        assert planted not in rendered


# --- provider values that are not what the field holds -------------------------


@pytest.mark.parametrize(
    ("component_id", "why"),
    [
        (SECRET_PATH, "a database path"),
        ("storage; PRAGMA key='s3cr3t'", "an injected statement"),
        ("lease_token=s3cr3t-lease-token", "a credential"),
        ("stor\nage", "an embedded newline"),
        ("storage\x00", "a NUL"),
        ("storage\x1b[31m", "an ANSI escape"),
        ("storage ", "a trailing space"),
        ("storage\n", "a trailing newline the `$` anchor alone would accept"),
        ("_storage", "a leading underscore the grammar does not admit"),
        ("", "nothing at all"),
        ("a" * 129, "one character more than Identifier admits"),
        ("a" * 4096, "a value sized to fill a caller's log"),
    ],
)
def test_a_component_id_that_is_not_an_identifier_is_never_published(
    component_id: str, why: str
) -> None:
    """The gap that mattered: `id` was published verbatim and never checked.

    A subsystem that names itself with the path to the database it could not open
    was telling an unauthenticated caller where that database lives -- through the
    allowlist, not around it, because `id` is on the allowlist and nothing asked
    whether what arrived in it was an `Identifier`.
    """
    facts = _facts(
        components=(
            ServiceComponentStatus(
                id=component_id, status="pass", observed_at=OBSERVED_AT
            ),
        )
    )
    with pytest.raises(ProbeError) as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_HEALTH))

    # Asserted whole rather than by substring: the guarantee is that the message
    # is *this and nothing else*, and a substring check would still pass if the
    # refused value were appended to it.
    assert str(raised.value) == "service facts component 0 id is malformed", why


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"health_status": "a" * 129}, "health_status"),
        ({"readiness_status": "pass\n"}, "readiness_status"),
        ({"discovery_status": "pass "}, "discovery_status"),
        ({"observed_at": "2026-13-45T99:99:99Z"}, "observed_at"),
        ({"observed_at": "2026-02-30T00:00:00Z"}, "observed_at"),
        ({"observed_at": "2026-08-02T00:00:60Z"}, "observed_at"),
        ({"observed_at": "2026-08-02T00:00:00z"}, "observed_at"),
        ({"observed_at": "2026-08-02T00:00:00Z\n"}, "observed_at"),
    ],
)
def test_a_pattern_valid_but_schema_invalid_fact_is_refused(
    overrides: dict[str, object], label: str
) -> None:
    """A pattern is not a calendar, and a `$` anchor is not the end of input.

    `2026-13-45T99:99:99Z` matches `TIMESTAMP_PATTERN` character for character and
    names no moment that has ever existed; a conforming client validating against
    `format: date-time` would reject the answer this build had already sent.
    """
    with pytest.raises(ProbeError, match=re.escape(label)):
        _router(_facts(**overrides)).route(ServiceProbeRequest(probe=PROBE_HEALTH))


@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-08-02T00:00:00Z",
        "2026-08-02T00:00:00.1Z",
        "2026-08-02T00:00:00.123456789Z",
    ],
)
def test_every_timestamp_spelling_the_contract_allows_is_still_answered(
    observed_at: str,
) -> None:
    """Fail-closed, not fail-narrow: the accepted language is not quietly shrunk."""
    wire = _published(_facts(observed_at=observed_at, components=()))
    assert wire["observed_at"] == observed_at


def test_a_snapshot_naming_more_components_than_the_contract_publishes_is_refused() -> (
    None
):
    """Refused, not truncated: a shortened list reads as a complete one."""
    component = ServiceComponentStatus(
        id="storage", status="pass", observed_at=OBSERVED_AT
    )
    at_limit = _facts(components=tuple(component for _ in range(256)))
    assert len(_published(at_limit)["components"]) == 256

    with pytest.raises(ProbeError, match="more than 256 components"):
        _router(_facts(components=tuple(component for _ in range(257)))).route(
            ServiceProbeRequest(probe=PROBE_HEALTH)
        )


# --- the descriptor, which travels whole ---------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("descriptor_version", "not-a-version"),
        ("descriptor_version", "1"),
        ("descriptor_version", "1.02"),
        ("descriptor_version", f"1.{'1' * 32}"),
        ("workspace_id", "ws 1"),
        ("workspace_id", "ws-1\n"),
        ("workspace_id", ""),
        ("workspace_id", "a" * 129),
        ("service_instance_id", SECRET_PATH),
        ("service_instance_id", ""),
        ("installation_id", "install\x00"),
        ("installation_id", "a" * 129),
        ("protocol_version", "1.0.0"),
        ("server_version", "not-semver"),
        ("server_version", "0.1"),
        ("workspace_format_version", "latest"),
        ("lifecycle_state", "READY"),
        ("lifecycle_state", "ready "),
        ("lifecycle_state", ""),
        ("fencing_generation", 0),
        ("fencing_generation", -3),
        ("published_at", "yesterday"),
        ("published_at", "2026-13-45T99:99:99Z"),
    ],
)
def test_a_descriptor_field_outside_its_public_grammar_is_never_published(
    field: str, value: object
) -> None:
    facts = _facts(descriptor=replace(_descriptor(), **{field: value}))
    with pytest.raises(ProbeError) as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    assert str(raised.value) in {
        f"service facts descriptor {field} is malformed",
        f"service facts descriptor {field} is out of range",
    }


def _refused_endpoint(endpoint_uri: str) -> str:
    """Refuse `endpoint_uri` through the router, and return what the caller is told."""
    facts = _facts(descriptor=replace(_descriptor(), endpoint_uri=endpoint_uri))
    with pytest.raises(ProbeError) as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    return str(raised.value)


#: The refusals this file stands as a permanent regression for, and the transports
#: it proves still publish -- named by the fixture's own case ids.
#:
#: The parametrized tests take whatever the corpus holds, which is what keeps them
#: from restating the policy. It is also what would let coverage vanish silently: a
#: case dropped from the fixture simply produces one parametrization fewer, and no
#: test counts them. Pinning the ids is the other half. A rule this boundary was
#: repaired for cannot stop being exercised here without something failing.
REQUIRED_REJECTIONS = frozenset(
    {
        "authority-userinfo",
        "direct-storage-file-uri",
        "credential-bearing-query",
        "fragment",
        "unapproved-scheme",
        "missing-http-host",
        "unbalanced-ip-literal",
        "malformed-ip-literal-nine-groups",
        "non-numeric-port",
        "out-of-range-port",
        "unix-storage-path",
        "unix-dot-segment",
    }
)

REQUIRED_APPROVALS = frozenset(
    {
        "loopback-http",
        "network-https",
        "ipv6-http",
        "unix-domain-socket",
        "windows-named-pipe",
    }
)


def test_the_endpoint_policy_corpus_still_carries_every_case_regressed_on() -> None:
    """The corpus is read, so what it must keep carrying is written down."""
    for group, required in (
        ("rejected", REQUIRED_REJECTIONS),
        ("accepted", REQUIRED_APPROVALS),
    ):
        present = {case["id"] for case in ENDPOINT_POLICY[group]}
        assert required <= present, sorted(required - present)


@pytest.mark.parametrize("endpoint_uri", _policy("rejected"))
def test_an_endpoint_the_accepted_policy_refuses_is_never_published(
    endpoint_uri: str,
) -> None:
    """Every refusal the accepted policy names, refused here too, and told nothing.

    The corpus is Core's, so this is not a restatement of the rule but a check that
    Runtime publishes exactly what Core accepts: a credential in the authority, a
    `file:///` workspace database, a credential-bearing query, a fragment, a missing
    or malformed host, a malformed IPv6 literal, a port that is not one, a `..`
    segment in a socket path, and an unapproved scheme are each refused, and none of
    them is quoted back.

    The permissive rule this replaced published most of them. `file:///Users/alice/
    Library/OmniVia/workspace.sqlite` is a well-formed absolute URI with an empty
    authority, so a check asking only "is this a URI, and is there a `userinfo`"
    handed an unauthenticated caller the location of the workspace database.
    """
    message = _refused_endpoint(endpoint_uri)
    # Equality is what proves the refusal is fixed; the second assertion is what it
    # is fixed *for*. The refused value is the credential, the workspace database or
    # the token, so a message that named what it refused would publish it to exactly
    # the caller the refusal exists to withhold it from.
    assert message == ENDPOINT_REFUSAL
    assert endpoint_uri not in message


@pytest.mark.parametrize(
    ("endpoint_uri", "why"),
    [
        (UNENCODED_ENDPOINT_URI, "a raw space is not a URI character"),
        (SECRET_PATH, "a bare path names no scheme"),
        ("core.sock", "a relative reference is not absolute"),
        ("unix:///tmp/core.sock\n", "a trailing newline is a control character"),
        ("unix:///tmp/core.sock?token=x", "a query is not part of a dialable endpoint"),
        ("unix:///tmp/core", "a unix endpoint that is not a `.sock` address"),
        ("", "nothing at all"),
    ],
)
def test_an_endpoint_uri_that_is_not_a_uri_is_refused(
    endpoint_uri: str, why: str
) -> None:
    """The shapes the fixture does not carry, refused on the same terms.

    Distinct from the corpus above, which is the policy's own list of what publishes
    and what does not. These are the ways a Runtime *building* an endpoint gets one
    wrong -- concatenating an unescaped path, forgetting the scheme, letting a
    trailing newline through -- and they are here because the value under test is
    the one this file's own fixture would otherwise have produced.
    """
    assert _refused_endpoint(endpoint_uri) == ENDPOINT_REFUSAL


def test_an_endpoint_uri_that_carries_a_credential_is_refused() -> None:
    """`ServiceEndpointDescriptor` states it carries no bearer credential or token.

    Recognized by position, not by resemblance: `userinfo` is a component of an RFC
    3986 authority, so the accepted pattern refuses where a credential structurally
    *is*, and does not guess at which strings look like one. The refusal is fixed,
    so the token cannot come back out in the message that reports it.
    """
    message = _refused_endpoint(f"http://svc:{SECRET_TOKEN}@127.0.0.1:9000")
    assert message == ENDPOINT_REFUSAL
    assert SECRET_TOKEN not in message


@pytest.mark.parametrize(
    ("endpoint_uri", "where"),
    [
        (f"http://svc:{SECRET_TOKEN}@127.0.0.1:9000/", "the authority's userinfo"),
        (f"http://127.0.0.1:9000/?access_token={SECRET_TOKEN}", "the query"),
        (f"http://127.0.0.1:9000/#{SECRET_TOKEN}", "the fragment"),
        (f"file://{SECRET_PATH}", "a direct-storage scheme"),
        (f"unix://{SECRET_PATH}", "a socket path that is a database"),
        (f"http://127.0.0.1:9000/{SECRET_TOKEN}?db={SECRET_PATH}", "both at once"),
    ],
)
def test_a_refused_endpoint_echoes_back_no_value_token_or_path(
    endpoint_uri: str, where: str
) -> None:
    """The refusal is the one surface a rejected endpoint could still travel on.

    Every value here is refused, so none of them is published as `endpoint_uri` --
    which leaves the message that reports the refusal as the only thing the caller
    receives. A boundary that named what it would not publish would publish it, and
    the credential, workspace database and lease token planted above are exactly the
    values the field carries when a Runtime has built one wrong.

    So the refusal is asserted whole and then searched: the same fixed string every
    other refused endpoint gets, with no part of the value in it, whichever position
    the secret sat in.
    """
    message = _refused_endpoint(endpoint_uri)
    assert message == ENDPOINT_REFUSAL, where
    for leaked in (*PLANTED, endpoint_uri, "access_token", "svc"):
        assert leaked not in message


@pytest.mark.parametrize("endpoint_uri", _policy("accepted"))
def test_an_endpoint_the_accepted_policy_approves_is_published(
    endpoint_uri: str,
) -> None:
    """Every endpoint the policy approves -- HTTP, HTTPS, IPv6, socket, pipe.

    Fail-closed is only half of it. A boundary that refused the approved transports
    too would pass every leak assertion in this file and leave no way to dial the
    instance at all, which is the fact `endpoint_uri` exists to carry.
    """
    facts = _facts(descriptor=replace(_descriptor(), endpoint_uri=endpoint_uri))
    wire = _published(facts, probe=PROBE_DISCOVER)
    assert wire["descriptor"]["endpoint_uri"] == endpoint_uri


@pytest.mark.parametrize(
    "endpoint_uri",
    [
        ENDPOINT_URI,
        "unix:///tmp/omnivia/core.sock",
        "http://127.0.0.1:9000/ovc1",
        "https://[::1]:9000/",
        # `@` outside an authority is an ordinary path character. The userinfo rule
        # is positional, so these must stay answerable -- a rule that simply banned
        # `@` would refuse them and read as if it were the same check.
        "unix:///tmp/a@b.sock",
        "http://127.0.0.1:9000/x@y",
    ],
)
def test_every_endpoint_a_real_instance_publishes_is_still_answered(
    endpoint_uri: str,
) -> None:
    facts = _facts(descriptor=replace(_descriptor(), endpoint_uri=endpoint_uri))
    wire = _published(facts, probe=PROBE_DISCOVER)
    assert wire["descriptor"]["endpoint_uri"] == endpoint_uri


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", 0),
        ("pid", -1),
        ("pid", "4242"),
        ("start_time", ""),
        ("start_time", "1728000000\n"),
        ("start_time", "a" * 129),
        ("boot_id", SECRET_PATH),
        ("boot_id", ""),
    ],
)
def test_malformed_process_evidence_is_refused_rather_than_published(
    field: str, value: object
) -> None:
    evidence = ServiceProcessEvidence(
        pid=4242, start_time="1728000000", boot_id="boot-1"
    )
    facts = _facts(
        descriptor=replace(_descriptor(), process=replace(evidence, **{field: value}))
    )
    with pytest.raises(ProbeError) as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    assert str(raised.value) in {
        f"service facts descriptor process {field} is malformed",
        f"service facts descriptor process {field} is out of range",
        f"service facts descriptor process {field} is not an integer",
    }


def test_process_start_time_is_bounded_but_deliberately_not_patterned() -> None:
    """The one published string the frozen contract leaves without a grammar.

    `ServiceProcessEvidence.start_time` is opaque by definition -- its spelling is
    whatever the host platform reports, and the contract says it is never parsed --
    so there is no grammar to hold it to and none is invented here. What *is*
    enforced is what the schema states: a bounded, non-empty string, plus the
    refusal of control characters that has to hold for anything published.

    Stated as a test rather than left implicit, because it is a real limit: a
    Runtime that puts a path in this field publishes a path, and closing that would
    take a contract change, not a change here.
    """
    facts = _facts(
        descriptor=replace(
            _descriptor(),
            process=ServiceProcessEvidence(
                pid=4242, start_time=SECRET_PATH, boot_id="boot-1"
            ),
        )
    )
    published = _published(facts, probe=PROBE_DISCOVER)
    assert published["descriptor"]["process"]["start_time"] == SECRET_PATH


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [("1.2", "1.0"), ("1", "1.2"), ("1.2", "latest"), ("", "1.2"), ("1.0", "1" * 33)],
)
def test_a_descriptor_window_nothing_can_negotiate_is_refused(
    minimum: str, maximum: str
) -> None:
    """Refused with this module's own message, not the contract's.

    `validate_version_window` quotes the bounds it rejected, which is right for a
    library talking to its caller and wrong on a wire read before authentication.
    """
    facts = _facts(
        descriptor=replace(
            _descriptor(),
            supported_api_versions=VersionWindow(minimum=minimum, maximum=maximum),
        )
    )
    with pytest.raises(ProbeError, match="supported_api_versions") as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    assert minimum not in str(raised.value) or minimum == ""
    assert maximum not in str(raised.value)


# --- the capability inventory --------------------------------------------------


@pytest.mark.parametrize(
    ("capability_id", "why"),
    [
        (SECRET_PATH, "a database path"),
        ("memory", "no namespace at all"),
        ("Memory.Search", "an uppercase spelling"),
        ("memory.search\n", "a trailing newline"),
        ("memory..search", "an empty segment"),
        ("ab", "shorter than CapabilityId's minimum"),
        ("a." + "b" * 127, "one character past the 128 the schema allows"),
        ("", "nothing at all"),
    ],
)
def test_a_capability_id_outside_its_public_grammar_is_never_advertised(
    capability_id: str, why: str
) -> None:
    with pytest.raises(ProbeError) as raised:
        _router(
            capabilities=(CapabilityRef(id=capability_id, version=API_VERSION),)
        ).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
    assert str(raised.value) == "supported capability 0 id is malformed", why


@pytest.mark.parametrize(
    "version", ["latest", "1", "1.2.3", "1.02", "1." + "1" * 32, ""]
)
def test_a_capability_version_that_is_not_a_contract_version_is_refused(
    version: str,
) -> None:
    with pytest.raises(ProbeError, match="capability 0 version"):
        _router(
            capabilities=(CapabilityRef(id="memory.search", version=version),)
        ).route(ServiceProbeRequest(probe=PROBE_DISCOVER))


def test_an_inventory_larger_than_the_contract_publishes_is_refused() -> None:
    def inventory(count: int) -> tuple[CapabilityRef, ...]:
        return tuple(
            CapabilityRef(id=f"memory.search{index}", version=API_VERSION)
            for index in range(count)
        )

    at_limit = _published(probe=PROBE_DISCOVER, capabilities=inventory(256))
    assert len(at_limit["supported_capabilities"]) == 256

    with pytest.raises(ProbeError, match="more than 256 entries"):
        _router(capabilities=inventory(257)).route(
            ServiceProbeRequest(probe=PROBE_DISCOVER)
        )


# --- wrong scalar and collection types, on directly constructed DTOs -----------
#
# `ServiceFacts` and `ServiceProbeRequest` are plain frozen dataclasses. A caller
# that builds one rather than decoding it can put anything in any field, and every
# operation the router performs on them -- a set membership, an integer comparison,
# a regex, an iteration, a dict insertion inside the contract package -- has a
# wrong-type input that raises something other than a `ProbeError`. Those are the
# same refusal to the caller and a different exception to whatever sits above this.


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_at": 17},
        {"observed_at": None},
        {"observed_at": b"2026-08-02T00:00:00Z"},
        {"health_status": None},
        {"readiness_status": 1.5},
        {"discovery_status": ["pass"]},
        {"components": 7},
        {"components": "storage"},
        {"components": None},
        {
            "components": [
                ServiceComponentStatus(id="s", status="pass", observed_at=OBSERVED_AT)
            ]
        },
        {"components": ({"id": "storage"},)},
        {"descriptor": {"endpoint_uri": ENDPOINT_URI}},
        {"descriptor": "descriptor"},
    ],
)
def test_a_wrongly_typed_fact_raises_a_probe_error_not_a_type_error(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ProbeError):
        _router(_facts(**overrides)).route(ServiceProbeRequest(probe=PROBE_HEALTH))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ready", "true"),
        ("ready", 1),
        ("fencing_generation", "7"),
        ("fencing_generation", True),
        ("fencing_generation", 7.0),
        ("published_at", 0),
        ("endpoint_uri", None),
        ("supported_api_versions", {"minimum": "1.2", "maximum": "1.2"}),
        ("supported_workspace_versions", None),
        ("process", {"pid": 4242}),
    ],
)
def test_a_wrongly_typed_descriptor_field_raises_a_probe_error(
    field: str, value: object
) -> None:
    facts = _facts(descriptor=replace(_descriptor(), **{field: value}))
    with pytest.raises(ProbeError, match=re.escape(field)):
        _router(facts).route(ServiceProbeRequest(probe=PROBE_DISCOVER))


@pytest.mark.parametrize(
    "capabilities",
    [
        7,
        "memory.search",
        b"memory.search",
        None,
        (CapabilityRef(id=["memory.search"], version=API_VERSION),),
        (CapabilityRef(id={"memory.search"}, version=API_VERSION),),
        (CapabilityRef(id="memory.search", version=None),),
        (CapabilityRef(id=7, version=API_VERSION),),
    ],
)
def test_a_wrongly_typed_capability_inventory_raises_a_probe_error(
    capabilities: object,
) -> None:
    """An unhashable id reached `duplicate_capability_ids` as a dict key.

    That failed with a `TypeError` raised from inside the contract package, several
    frames below the boundary that is supposed to own every refusal here.
    """
    with pytest.raises(ProbeError):
        _router(capabilities=capabilities).route(  # type: ignore[arg-type]
            ServiceProbeRequest(probe=PROBE_DISCOVER)
        )


@pytest.mark.parametrize(
    "request_value",
    [
        {"probe": PROBE_HEALTH},
        PROBE_HEALTH,
        None,
        7,
        ServiceProbeRequest(probe=["service.health"]),
        ServiceProbeRequest(probe=None),
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id=17),
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id=""),
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id="req 42"),
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id="req-42\n"),
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id="r" * 129),
        ServiceProbeRequest(probe=PROBE_HEALTH, request_id={"database": SECRET_PATH}),
        ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms="10"),
        ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=1.5),
        ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=True),
        ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=86_400_001),
    ],
)
def test_a_malformed_request_raises_a_probe_error_not_a_type_error(
    request_value: object,
) -> None:
    with pytest.raises(ProbeError) as raised:
        _router().route(request_value)  # type: ignore[arg-type]
    assert SECRET_PATH not in str(raised.value)


def test_an_unknown_probe_kind_is_quoted_back_but_never_unbounded() -> None:
    """A refusal names the spelling it refused, and cannot be made arbitrarily long.

    The kind is the caller's own string returned to the caller it came from, which
    is what makes quoting it safe. Quoting an unbounded one is what makes it a
    lever: the sender chooses the length of the refusal.
    """
    with pytest.raises(ProbeError, match="unknown probe 'core.health'"):
        _router().route(ServiceProbeRequest(probe="core.health"))

    at_limit = "a" * _max_length("service", "ProbeKind")
    with pytest.raises(ProbeError, match="unknown probe") as quoted:
        _router().route(ServiceProbeRequest(probe=at_limit))
    assert at_limit in str(quoted.value)

    with pytest.raises(ProbeError) as refused:
        _router().route(ServiceProbeRequest(probe=at_limit + "a"))
    assert str(refused.value) == (
        "probe request names a probe kind that is out of range"
    )


def test_a_request_id_is_never_echoed_as_a_value_the_result_cannot_carry() -> None:
    """`details` is the one place a caller's value is republished.

    A non-string or an overlong id would put something on the wire that
    `ServiceProbeResult` does not describe -- and `details` is an open JSON object,
    so nothing downstream would notice.
    """
    with pytest.raises(ProbeError, match="request_id"):
        _router().route(
            ServiceProbeRequest(probe=PROBE_HEALTH, request_id="r" * 129)  # type: ignore[arg-type]
        )
    assert _published(request_id="r" * 128)["details"] == {REQUEST_ID_DETAIL: "r" * 128}


def test_a_wrongly_typed_request_is_refused_before_the_clock_is_read() -> None:
    """Nothing is measured for a value that was never a request."""
    clock = FakeClock(0)

    def unusable() -> ServiceFacts:  # pragma: no cover - must never be called
        raise AssertionError("facts were gathered for a malformed request")

    router = ProbeRouter(facts=unusable, capabilities=tuple, clock=clock)
    with pytest.raises(ProbeError, match="not a ServiceProbeRequest"):
        router.route({"probe": PROBE_HEALTH})  # type: ignore[arg-type]
    assert clock.calls == 0


# --- the bounds, read from the canonical schema rather than restated -----------


@pytest.mark.parametrize(
    ("document", "definition", "build"),
    [
        ("common", "Identifier", lambda size: "a" * size),
        ("service", "ProbeStatus", lambda size: "a" * size),
        ("common", "CapabilityId", lambda size: "a." + "b" * (size - 2)),
        ("common", "ContractVersion", lambda size: "1." + "1" * (size - 2)),
    ],
)
def test_the_length_bounds_are_the_ones_the_packaged_schema_declares(
    document: str, definition: str, build: object
) -> None:
    """Pinned against `contracts/application/v1/schemas`, not against a memory.

    The patterns are imported from the contract; the lengths are not exported
    beside them, so the router restates them. This is what keeps the restatement
    honest: a value at exactly the declared bound is answered and one character
    past it is refused, with the bound read from the canonical document.
    """
    assert callable(build)
    maximum = _max_length(document, definition)
    accepted, refused = build(maximum), build(maximum + 1)
    assert len(accepted) == maximum

    if definition == "ProbeStatus":
        assert _published(_facts(health_status=accepted, components=()))["status"] == (
            accepted
        )
        with pytest.raises(ProbeError):
            _router(_facts(health_status=refused)).route(
                ServiceProbeRequest(probe=PROBE_HEALTH)
            )
        return
    if definition == "CapabilityId":
        published = _published(
            probe=PROBE_DISCOVER,
            capabilities=(CapabilityRef(id=accepted, version=API_VERSION),),
        )
        assert published["supported_capabilities"][0]["id"] == accepted
        with pytest.raises(ProbeError):
            _router(
                capabilities=(CapabilityRef(id=refused, version=API_VERSION),)
            ).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
        return
    if definition == "ContractVersion":
        with pytest.raises(ProbeError):
            _router(
                capabilities=(CapabilityRef(id="memory.search", version=refused),)
            ).route(ServiceProbeRequest(probe=PROBE_DISCOVER))
        return

    component = ServiceComponentStatus(
        id=accepted, status="pass", observed_at=OBSERVED_AT
    )
    assert (
        _published(_facts(components=(component,)))["components"][0]["id"] == accepted
    )
    with pytest.raises(ProbeError):
        _router(_facts(components=(replace(component, id=refused),))).route(
            ServiceProbeRequest(probe=PROBE_HEALTH)
        )


def test_the_endpoint_uri_bound_is_the_one_the_packaged_schema_declares() -> None:
    """The bound is followed to where it is declared, not assumed to sit inline.

    `ServiceEndpointDescriptor.endpoint_uri` is a `$ref` to `ServiceEndpointUri`,
    which is where the accepted policy pins the pattern and the length together --
    so `properties.endpoint_uri.maxLength` is a key that is no longer there, and a
    test reading it would fail on the document rather than on the bound.

    Both witnesses are real `.sock` addresses, one character apart. The over-long
    one is lengthened in its path rather than past its suffix, so length is the only
    reason it is refused: appending to `accepted` would have produced `...socka`,
    which the policy refuses for not being a socket at all.
    """
    schema = json.loads(
        (SCHEMA_DIR / "service.schema.json").read_text(encoding="utf-8")
    )
    reference = schema["$defs"]["ServiceEndpointDescriptor"]["properties"][
        "endpoint_uri"
    ]["$ref"]
    _, _, pointer = reference.partition("#/")
    definition = schema
    for token in pointer.split("/"):
        definition = definition[token]
    maximum = definition["maxLength"]
    assert isinstance(maximum, int)

    def _socket(length: int) -> str:
        filler = length - len("unix:///") - len(".sock")
        return "unix:///" + "a" * filler + ".sock"

    accepted = _socket(maximum)
    assert len(accepted) == maximum

    facts = _facts(descriptor=replace(_descriptor(), endpoint_uri=accepted))
    assert _published(facts, probe=PROBE_DISCOVER)["descriptor"]["endpoint_uri"] == (
        accepted
    )
    assert _refused_endpoint(_socket(maximum + 1)) == ENDPOINT_REFUSAL


def test_the_published_array_bound_is_the_one_the_packaged_schema_declares() -> None:
    schema = json.loads(
        (SCHEMA_DIR / "service.schema.json").read_text(encoding="utf-8")
    )
    properties = schema["$defs"]["ServiceProbeResult"]["properties"]
    assert properties["components"]["maxItems"] == 256
    assert properties["supported_capabilities"]["maxItems"] == 256


def test_a_timestamps_length_is_bounded_by_its_grammar_not_by_its_maxlength() -> None:
    """Why there is no over-length `Timestamp` case above.

    `TIMESTAMP_PATTERN` admits at most nine fractional digits, so the longest
    string that can match is thirty characters -- ten short of the declared
    `maxLength`. The bound is real and unreachable, and a test pretending to
    exercise it would be testing nothing.
    """
    longest = "2026-08-02T00:00:00.123456789Z"
    assert len(longest) < _max_length("common", "Timestamp")
    assert _published(_facts(observed_at=longest, components=()))["observed_at"] == (
        longest
    )


# --- what a refusal is allowed to say ------------------------------------------


def test_no_refusal_repeats_the_value_it_refused() -> None:
    """Every malformed field, one corpus, one assertion.

    A message names the field. It does not name the value, its length, or the
    grammar it failed: a probe answers before authentication, and the value is
    exactly what cannot be vouched for.
    """
    poisoned = f"{SECRET_PATH}?token={SECRET_TOKEN}"
    corpus: tuple[tuple[ServiceFacts, str], ...] = (
        (_facts(observed_at=poisoned), PROBE_HEALTH),
        (_facts(health_status=poisoned), PROBE_HEALTH),
        (
            _facts(
                components=(
                    ServiceComponentStatus(
                        id=poisoned, status="pass", observed_at=OBSERVED_AT
                    ),
                )
            ),
            PROBE_READINESS,
        ),
        (
            _facts(
                components=(
                    ServiceComponentStatus(
                        id="storage", status=poisoned, observed_at=OBSERVED_AT
                    ),
                )
            ),
            PROBE_READINESS,
        ),
        (
            _facts(descriptor=replace(_descriptor(), workspace_id=poisoned)),
            PROBE_DISCOVER,
        ),
        (
            _facts(descriptor=replace(_descriptor(), endpoint_uri=poisoned)),
            PROBE_DISCOVER,
        ),
        (
            _facts(
                descriptor=replace(
                    _descriptor(),
                    process=ServiceProcessEvidence(
                        pid=4242, start_time="1728000000", boot_id=poisoned
                    ),
                )
            ),
            PROBE_DISCOVER,
        ),
    )

    for facts, probe in corpus:
        with pytest.raises(ProbeError) as raised:
            _router(facts).route(ServiceProbeRequest(probe=probe, request_id="req-42"))
        message = str(raised.value)
        for leaked in (*PLANTED, poisoned):
            assert leaked not in message


#: An endpoint the accepted policy refuses, carrying a credential in the position a
#: naive Runtime would put one. Distinct from `ENDPOINT_URI`, which is meant to travel.
CREDENTIALED_ENDPOINT_URI = f"http://127.0.0.1/?access_token={SECRET_TOKEN}"


def _nested_text(check: Callable[[], object]) -> str:
    """What the nested check actually says when it fails.

    Read off the real check rather than transcribed, so this stays a leak assertion
    if the contract or the interpreter rewords its message. A check that stopped
    failing would leave nothing to assert about, so that is a failure here too.
    """
    with pytest.raises(Exception) as raised:
        check()
    text = str(raised.value)
    assert text
    return text


#: Every nested check `_validate_facts` reaches through: the snapshot that fails it,
#: the fixed refusal that answers it, and the real exception it must not carry.
NESTED_CHECKS = [
    pytest.param(
        _facts(observed_at="2026-13-01T00:00:00Z"),
        "service facts observed_at is malformed",
        lambda: datetime.fromisoformat("2026-13-01T00:00:00+00:00"),
        id="timestamp",
    ),
    pytest.param(
        _facts(
            descriptor=replace(
                _descriptor(), endpoint_uri=CREDENTIALED_ENDPOINT_URI
            )
        ),
        "service facts descriptor endpoint_uri is not an approved transport endpoint",
        lambda: validate_service_endpoint_descriptor(
            replace(_descriptor(), endpoint_uri=CREDENTIALED_ENDPOINT_URI)
        ),
        id="endpoint",
    ),
    pytest.param(
        _facts(
            descriptor=replace(
                _descriptor(),
                supported_workspace_versions=VersionWindow(
                    minimum="9.9", maximum="1.0"
                ),
            )
        ),
        "service facts descriptor supported_workspace_versions is malformed",
        lambda: validate_version_window(VersionWindow(minimum="9.9", maximum="1.0")),
        id="version-window",
    ),
]


@pytest.mark.parametrize(("facts", "refusal", "check"), NESTED_CHECKS)
def test_a_refusal_carries_no_nested_exception_text(
    facts: ServiceFacts, refusal: str, check: Callable[[], object]
) -> None:
    """The nested errors quote what they refused; these do not pass it on.

    Not by any route, which is the whole of the point. `raise ... from None` at these
    three sites passed every assertion below but one: it clears `__cause__` and sets
    `__suppress_context__`, so `rendered` stays quiet while the original exception --
    and the frames of the validator that produced it, holding the descriptor -- stay
    on `__context__`, one attribute access away for anything that logs, serializes or
    reports the error an unauthenticated caller catches.
    """
    nested = _nested_text(check)
    with pytest.raises(ProbeError) as raised:
        _router(facts).route(ServiceProbeRequest(probe=PROBE_DISCOVER))

    error = raised.value
    assert str(error) == refusal
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for surface in (str(error), repr(error), repr(error.args), rendered):
        for leaked in (nested, *PLANTED):
            assert leaked not in surface
    assert error.__cause__ is None
    assert error.__context__ is None


# --- what an injected provider's own failure is allowed to say -----------------
#
# The projection above sits on published *fields*. A provider that raises reaches
# the caller by the one route a field allowlist does not cover, carrying the text
# a Runtime wrote for its own logs: a database path, a lease token, the driver's
# exception. So each injection point below is failed in turn, and the refusal is
# pinned to a fixed string that names only which provider did not answer.

#: What a Runtime's storage, lease or clock code raises when it cannot answer --
#: written for whoever reads this build's logs, and therefore full of exactly what
#: an unauthenticated probe caller may not be handed.
POISONED = f"{SECRET_MESSAGE} (lease {SECRET_TOKEN} held by pid 4242)"


def _failing(error: BaseException) -> Callable[[], Any]:
    """A provider that does not answer, the way a broken subsystem does not."""

    def provider() -> Any:
        raise error

    return provider


def _clock(*readings: object) -> Callable[[], Any]:
    """A clock returning each reading in turn; a reading that is an exception is raised.

    Distinct from `FakeClock`, which promises well-typed integers. This one is how
    an injected clock actually misbehaves: it raises, hands back something that is
    not an `int`, or reports an instant before the one it just reported.
    """
    remaining = list(readings)

    def read() -> Any:
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, BaseException):
            raise value
        return value

    return read


def _assert_nothing_of(original: BaseException, error: ProbeError) -> None:
    """The refusal keeps nothing of the exception it replaced, by any route.

    `raise ... from None` would pass every assertion but two: it clears
    `__cause__` and silences the *rendering* of `__context__`, leaving the
    original -- with its text -- one attribute access away for anything that logs
    or serializes the error a caller catches.
    """
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for surface in (str(error), repr(error), repr(error.args), rendered):
        for leaked in (*PLANTED, POISONED):
            assert leaked not in surface
    assert error.__cause__ is None
    assert error.__context__ is None
    assert original not in error.args


@pytest.mark.parametrize(
    ("build_router", "request_value", "refusal"),
    [
        pytest.param(
            lambda boom: ProbeRouter(
                facts=_failing(boom), capabilities=tuple, clock=FakeClock(0)
            ),
            ServiceProbeRequest(probe=PROBE_HEALTH),
            "service facts are unavailable",
            id="facts",
        ),
        pytest.param(
            lambda boom: ProbeRouter(
                facts=_facts, capabilities=_failing(boom), clock=FakeClock(0)
            ),
            ServiceProbeRequest(probe=PROBE_DISCOVER),
            "supported capabilities are unavailable",
            id="capabilities",
        ),
        pytest.param(
            lambda boom: ProbeRouter(
                facts=_facts, capabilities=tuple, clock=_failing(boom)
            ),
            ServiceProbeRequest(probe=PROBE_HEALTH),
            "the monotonic clock is unavailable",
            id="clock-first-reading",
        ),
        pytest.param(
            # The second reading is taken only once a deadline asked for one, and
            # only after the answer has been built -- so this is the provider
            # failure that happens with a result already in hand.
            lambda boom: ProbeRouter(
                facts=_facts, capabilities=tuple, clock=_clock(0, boom)
            ),
            ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=10),
            "the monotonic clock is unavailable",
            id="clock-second-reading",
        ),
    ],
)
def test_a_provider_that_raises_is_refused_with_a_fixed_message(
    build_router: Any, request_value: ServiceProbeRequest, refusal: str
) -> None:
    """Which provider did not answer, and nothing else it had to say."""
    boom = RuntimeError(POISONED)
    with pytest.raises(ProbeError) as raised:
        build_router(boom).route(request_value)

    assert str(raised.value) == refusal
    assert not isinstance(raised.value, ProbeDeadlineExceeded)
    _assert_nothing_of(boom, raised.value)


def test_a_provider_failure_keeps_its_text_out_of_a_rendered_traceback() -> None:
    """The whole traceback, not just the message a caller happens to print."""
    boom = RuntimeError(POISONED)
    router = ProbeRouter(facts=_failing(boom), capabilities=tuple, clock=FakeClock(0))
    with pytest.raises(ProbeError) as raised:
        router.route(ServiceProbeRequest(probe=PROBE_HEALTH, request_id="req-42"))

    rendered = "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )
    assert POISONED not in rendered
    assert "During handling of the above exception" not in rendered
    assert "direct cause" not in rendered


@pytest.mark.parametrize(
    "reading",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(1.0, id="float"),
        pytest.param("0", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_a_clock_reading_that_is_not_an_integer_is_refused(reading: object) -> None:
    """A reading is an operand, so a wrong type is refused before the arithmetic.

    `True` and `False` are `int` in Python and are not instants: a clock stubbed
    out to return `False` would otherwise read as `0` and make every deadline
    comparison quietly sound.
    """
    router = ProbeRouter(facts=_facts, capabilities=tuple, clock=_clock(reading))
    with pytest.raises(ProbeError) as raised:
        router.route(ServiceProbeRequest(probe=PROBE_HEALTH))

    assert str(raised.value) == "the monotonic clock did not read an integer"
    assert not isinstance(raised.value, ProbeDeadlineExceeded)


@pytest.mark.parametrize("reading", [pytest.param(True, id="true"), 1.0, "0"])
def test_a_second_clock_reading_that_is_not_an_integer_is_refused(
    reading: object,
) -> None:
    """The reading taken to close a budget is held to the same standard as the first."""
    router = ProbeRouter(facts=_facts, capabilities=tuple, clock=_clock(0, reading))
    with pytest.raises(ProbeError) as raised:
        router.route(ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=10))

    assert str(raised.value) == "the monotonic clock did not read an integer"


def test_a_clock_that_ran_backwards_is_refused_rather_than_read_as_timely() -> None:
    """A negative elapsed time is below every budget there is, and measured nothing.

    Refused as a broken clock rather than as a missed deadline: the caller's
    budget was never actually measured, so neither answer is available.
    """
    router = ProbeRouter(
        facts=_facts, capabilities=tuple, clock=_clock(10 * MS, 10 * MS - 1)
    )
    with pytest.raises(ProbeError) as raised:
        router.route(ServiceProbeRequest(probe=PROBE_HEALTH, deadline_ms=10))

    assert str(raised.value) == "the monotonic clock went backwards"
    assert not isinstance(raised.value, ProbeDeadlineExceeded)


def test_an_interpreter_shutdown_is_not_swallowed_as_a_provider_failure() -> None:
    """`BaseException` is deliberately outside the guard.

    `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are the interpreter
    unwinding this process, not a subsystem reporting that it is unwell.
    Catching them here would convert a shutdown into an answered request and
    leave the process serving, which is a worse failure than the leak the guard
    exists to close.
    """
    router = ProbeRouter(
        facts=_failing(KeyboardInterrupt()), capabilities=tuple, clock=FakeClock(0)
    )
    with pytest.raises(KeyboardInterrupt):
        router.route(ServiceProbeRequest(probe=PROBE_HEALTH))


def test_providers_that_answer_are_untouched_by_the_guard_around_them() -> None:
    """The other half of failing closed: nothing about a working probe moves.

    A guard that refused too much would be invisible to every test above, which
    only asks what happens when a provider fails. So all three probes are
    answered here from providers that work -- health and readiness read two, and
    discovery reads all three -- and each answer is checked field by field and
    against the canonical schema.
    """
    router = _router(
        _facts(descriptor=_descriptor()),
        capabilities=(CapabilityRef(id="memory.search", version=API_VERSION),),
    )
    health = router.route(ServiceProbeRequest(probe=PROBE_HEALTH, request_id="req-42"))
    readiness = router.route(ServiceProbeRequest(probe=PROBE_READINESS))
    discovery = router.route(ServiceProbeRequest(probe=PROBE_DISCOVER))

    assert (health.status, readiness.status, discovery.status) == (
        "pass",
        "warn",
        "pass",
    )
    assert health.to_wire()["components"] == [
        {"id": "storage", "status": "pass", "observed_at": OBSERVED_AT}
    ]
    assert health.details == {REQUEST_ID_DETAIL: "req-42"}
    assert readiness.descriptor is None
    assert readiness.supported_capabilities is None
    assert discovery.components is None
    assert discovery.descriptor is not None
    assert discovery.supported_capabilities == (
        CapabilityRef(id="memory.search", version=API_VERSION),
    )
    for result in (health, readiness, discovery):
        assert _schema_errors(result.to_wire()) == []


# --- forbidden imports --------------------------------------------------------

SERVICE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnivia_core_runtime" / "service"
)

#: Modules a version or probe answer must never need. Storage, ownership and
#: workspace code is what a probe *reports on*; importing it is how a health probe
#: ends up unable to answer precisely when the subsystem is broken.
FORBIDDEN_IMPORT_ROOTS = (
    "omnivia_core_runtime.storage",
    "omnivia_core_runtime.ownership",
    "omnivia_core_runtime.workspace",
    "omnivia_core_runtime.service.transport",
    "omnivia_core_runtime.service.lifecycle",
    "omnivia_core_runtime.service.runner",
    "omnivia_core_runtime.service.bootstrap",
    "omnivia_core_runtime.service.dispatch",
    "omnivia_core_runtime.service.jobs",
    "omnivia_core_runtime.service.main",
    "omnivia_core_runtime.service.authorization",
    "omnivia_core_runtime.service.operations",
    "socket",
    "sqlite3",
    "json",
    "http",
    "asyncio",
    "subprocess",
    "threading",
    "time",
)


def imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, read from its syntax tree."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", ["versions.py", "probes.py"])
def test_versions_and_probes_import_no_storage_or_transport_code(module: str) -> None:
    imported = imported_modules(SERVICE_DIR / module)
    for name in imported:
        root = name.split(".")[0]
        assert not any(
            name == forbidden or name.startswith(f"{forbidden}.") or root == forbidden
            for forbidden in FORBIDDEN_IMPORT_ROOTS
        ), f"{module} imports {name}"


def test_the_probe_router_owns_no_clock_of_its_own() -> None:
    """`time` is absent above; this pins *why* it is absent.

    `datetime` *is* imported, to prove a caller's `Timestamp` names a real instant.
    That is parsing, and it is one attribute away from being clock-reading, so the
    difference is pinned in the syntax tree rather than left to a reviewer noticing
    the import and having to guess which of the two it is for.
    """
    source = (SERVICE_DIR / "probes.py").read_text(encoding="utf-8")
    assert "time" not in imported_modules(SERVICE_DIR / "probes.py")

    read = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
    }
    assert read.isdisjoint({"now", "utcnow", "today", "monotonic", "monotonic_ns"})
    assert "fromisoformat" in read

    with pytest.raises(TypeError):
        ProbeRouter(facts=_facts, capabilities=tuple)  # type: ignore[call-arg]
