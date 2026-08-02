"""Version negotiation: what this build can talk to, and what it refuses.

The API version is derived from the public contract rather than restated here,
so these tests assert the *derivation* -- a test that hard-coded ``"1.2"`` would
pass for the wrong reason the day the contract moves.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client.compatibility import (
    CLIENT_API_VERSION,
    CLIENT_SUPPORTED_API_VERSIONS,
    SUPPORTED_DESCRIPTOR_VERSION,
    SUPPORTED_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    NegotiatedEndpoint,
    negotiate_endpoint,
    select_api_version,
    select_protocol_version,
    validate_descriptor_version,
)
from omnivia_core_client.errors import ClientError, CompatibilityError

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ContractDecodeError,
    ServiceEndpointDescriptor,
    VersionWindow,
    parse_contract_version,
    validate_version_window,
)

MANIFEST: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "ovc1-v1.json").read_text(
        encoding="utf-8"
    )
)
DESCRIPTOR_WIRE: dict[str, Any] = next(
    vector
    for vector in MANIFEST["vectors"]
    if vector["id"] == "probe.discover.result-with-descriptor"
)["payload"]["descriptor"]

CONTRACT_MAJOR, _ = parse_contract_version(CONTRACT_VERSION)


def window(minimum: str, maximum: str) -> VersionWindow:
    return VersionWindow(minimum=minimum, maximum=maximum)


def descriptor(**overrides: Any) -> ServiceEndpointDescriptor:
    """The accepted vector descriptor, with named fields replaced."""
    return dataclasses.replace(
        ServiceEndpointDescriptor.from_wire(DESCRIPTOR_WIRE), **overrides
    )


# --------------------------------------------------------------------------
# The client's own versions are derived, never restated
# --------------------------------------------------------------------------


def test_the_client_api_version_is_the_public_contract_version() -> None:
    assert CLIENT_API_VERSION == CONTRACT_VERSION


def test_the_descriptor_shape_version_is_the_public_contract_version() -> None:
    assert SUPPORTED_DESCRIPTOR_VERSION == CONTRACT_VERSION


def test_the_client_window_is_derived_from_the_contract_version() -> None:
    assert CLIENT_SUPPORTED_API_VERSIONS.maximum == CONTRACT_VERSION
    assert CLIENT_SUPPORTED_API_VERSIONS.minimum == f"{CONTRACT_MAJOR}.0"
    validate_version_window(CLIENT_SUPPORTED_API_VERSIONS)


def test_the_api_version_is_not_pinned_to_the_protocol_version() -> None:
    """The API version follows the contract; only the *protocol* version is a literal.

    The regression this guards is a client that negotiates the OVC1 protocol's
    ``1.0`` as its API version because the two were confused for one value.
    """
    # Widened from the contract's literal type on purpose: the point of the test
    # is that these two values are independent, which a literal-vs-literal
    # comparison would let a type checker rule out before it ever runs.
    contract_version: str = CONTRACT_VERSION
    protocol_version: str = SUPPORTED_PROTOCOL_VERSION

    assert (
        select_api_version(CLIENT_SUPPORTED_API_VERSIONS, CLIENT_SUPPORTED_API_VERSIONS)
        == contract_version
    )
    negotiated = negotiate_endpoint(
        ServiceEndpointDescriptor.from_wire(DESCRIPTOR_WIRE)
    )
    assert negotiated.protocol_version == protocol_version
    assert negotiated.api_version == contract_version
    assert contract_version != protocol_version


def test_ovc1_protocol_support_is_exactly_one_zero() -> None:
    assert SUPPORTED_PROTOCOL_VERSION == "1.0"
    assert SUPPORTED_PROTOCOL_VERSIONS == ("1.0",)


# --------------------------------------------------------------------------
# API version selection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "server", "expected"),
    [
        (("1.0", "1.3"), ("1.0", "1.3"), "1.3"),
        (("1.0", "1.2"), ("1.0", "1.3"), "1.2"),
        (("1.0", "1.3"), ("1.0", "1.1"), "1.1"),
        (("1.2", "1.5"), ("1.1", "1.4"), "1.4"),
        (("1.0", "1.0"), ("1.0", "1.4"), "1.0"),
        (("1.2", "1.2"), ("1.0", "1.9"), "1.2"),
        (("1.0", "1.10"), ("1.0", "1.9"), "1.9"),
        (("1.0", "1.10"), ("1.9", "1.20"), "1.10"),
    ],
)
def test_the_highest_mutually_supported_version_is_selected(
    client: tuple[str, str], server: tuple[str, str], expected: str
) -> None:
    assert select_api_version(window(*client), window(*server)) == expected


def test_selection_is_by_numeric_component_not_lexical_string_order() -> None:
    """`1.10` is above `1.9`; string comparison would say the opposite."""
    assert select_api_version(window("1.0", "1.10"), window("1.0", "1.10")) == "1.10"


def test_windows_touching_at_a_single_version_select_it() -> None:
    assert select_api_version(window("1.0", "1.2"), window("1.2", "1.5")) == "1.2"


def test_a_shared_major_with_disjoint_minors_reports_an_upgrade() -> None:
    with pytest.raises(CompatibilityError, match="one side must be upgraded"):
        select_api_version(window("1.0", "1.2"), window("1.5", "1.9"))


def test_no_major_in_common_says_so_rather_than_suggesting_an_upgrade() -> None:
    with pytest.raises(CompatibilityError, match="no API major version in common"):
        select_api_version(window("1.0", "1.9"), window("2.0", "2.4"))


def test_a_multi_major_window_still_selects_within_one_major() -> None:
    selected = select_api_version(window("1.0", "2.1"), window("1.5", "2.0"))
    assert selected == "2.0"
    assert parse_contract_version(selected)[0] == 2


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [("1.3", "1.0"), ("2.0", "1.9"), ("1.1", "1.0")],
)
def test_a_reversed_window_is_refused_rather_than_treated_as_empty(
    minimum: str, maximum: str
) -> None:
    with pytest.raises(CompatibilityError, match="reversed"):
        select_api_version(window(minimum, maximum), window("1.0", "1.3"))
    with pytest.raises(CompatibilityError, match="reversed"):
        select_api_version(window("1.0", "1.3"), window(minimum, maximum))


@pytest.mark.parametrize("bound", ["1", "latest", "01.2", "1.02", "١.٢", "", "1.2.3"])
def test_a_bound_that_is_not_a_contract_version_is_refused(bound: str) -> None:
    with pytest.raises(CompatibilityError, match="version window"):
        select_api_version(window(bound, "9.9"), window("1.0", "1.3"))


@pytest.mark.parametrize("value", [None, "1.0", ("1.0", "1.3"), 12])
def test_something_that_is_not_a_version_window_is_refused(value: object) -> None:
    with pytest.raises(CompatibilityError, match="expected a VersionWindow"):
        select_api_version(value, window("1.0", "1.3"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bound", [None, 1, 1.0, True, b"1.0", ["1.0"]])
def test_a_bound_that_is_not_a_string_is_a_compatibility_error_not_a_type_error(
    bound: object,
) -> None:
    """A `VersionWindow` is an ordinary dataclass and holds whatever it was given.

    The contract's own parser matches a regular expression, so a non-string
    bound comes back from it as a bare `TypeError` about `re` rather than
    anything about versions. A caller of this package catches `ClientError`.
    """
    with pytest.raises(CompatibilityError, match="version window.*must be a"):
        select_api_version(
            window(bound, "9.9"),  # type: ignore[arg-type]
            window("1.0", "1.3"),
        )
    with pytest.raises(CompatibilityError, match="version window.*must be a"):
        select_api_version(
            window("1.0", "1.3"),
            window("1.0", bound),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# Protocol version: not negotiated
# --------------------------------------------------------------------------


def test_the_only_accepted_protocol_version_is_one_zero() -> None:
    assert select_protocol_version("1.0") == SUPPORTED_PROTOCOL_VERSION


@pytest.mark.parametrize("advertised", ["2.0", "0.9", "3.1", "10.0"])
def test_another_protocol_major_fails_closed(advertised: str) -> None:
    with pytest.raises(CompatibilityError, match="protocol major"):
        select_protocol_version(advertised)


@pytest.mark.parametrize("advertised", ["1.1", "1.2", "1.99"])
def test_a_protocol_minor_this_build_does_not_implement_fails_closed(
    advertised: str,
) -> None:
    with pytest.raises(CompatibilityError, match="does not implement that minor"):
        select_protocol_version(advertised)


@pytest.mark.parametrize("advertised", ["", "1", "one.zero", "1.0.0", "v1.0", "01.0"])
def test_an_unparseable_protocol_version_fails_closed(advertised: str) -> None:
    with pytest.raises(CompatibilityError, match="not a `major.minor` version"):
        select_protocol_version(advertised)


@pytest.mark.parametrize("advertised", [None, 1.0, 10, True, b"1.0", ["1.0"], {}])
def test_a_protocol_version_that_is_not_a_string_fails_closed(
    advertised: object,
) -> None:
    """A descriptor field is only a string by convention, and this is a direct
    entry point: a hand-built or `dataclasses.replace`d descriptor can hold
    anything at all, and none of it may surface as a raw `TypeError`."""
    with pytest.raises(CompatibilityError, match="must be a `major.minor` string"):
        select_protocol_version(advertised)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Descriptor shape version: additive minors in, other majors out
# --------------------------------------------------------------------------


@pytest.mark.parametrize("minor", [0, 2, 9, 15, 100])
def test_any_same_major_descriptor_minor_is_readable(minor: int) -> None:
    version = f"{CONTRACT_MAJOR}.{minor}"
    assert validate_descriptor_version(version) == version


def test_another_descriptor_major_is_refused() -> None:
    others = sorted({0, 2, 3, 9, CONTRACT_MAJOR + 1} - {CONTRACT_MAJOR})
    assert others
    for major in others:
        with pytest.raises(CompatibilityError, match="descriptor shape version"):
            validate_descriptor_version(f"{major}.0")


@pytest.mark.parametrize("version", ["", "1", "1.x", "latest", "1.0.0"])
def test_an_unparseable_descriptor_version_is_refused(version: str) -> None:
    with pytest.raises(CompatibilityError, match="not a `major.minor` version"):
        validate_descriptor_version(version)


@pytest.mark.parametrize("version", [None, 1.2, 12, True, b"1.2", ["1.2"]])
def test_a_descriptor_version_that_is_not_a_string_is_refused(version: object) -> None:
    with pytest.raises(CompatibilityError, match="must be a `major.minor` string"):
        validate_descriptor_version(version)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("descriptor_version", None),
        ("descriptor_version", 1.2),
        ("protocol_version", None),
        ("protocol_version", 10),
        ("supported_api_versions", None),
        ("supported_api_versions", ("1.0", "1.3")),
    ],
)
def test_a_descriptor_field_of_the_wrong_type_is_a_compatibility_error(
    field: str, value: object
) -> None:
    """Every version this build reads off a descriptor, given the wrong type.

    Nothing here may escape as a `TypeError` from a regular expression or as a
    `ContractSemanticError` from the contract layer.
    """
    with pytest.raises(CompatibilityError):
        negotiate_endpoint(descriptor(**{field: value}))


# --------------------------------------------------------------------------
# Negotiating against a published descriptor
# --------------------------------------------------------------------------


def test_the_accepted_vector_descriptor_negotiates() -> None:
    negotiated = negotiate_endpoint(
        ServiceEndpointDescriptor.from_wire(DESCRIPTOR_WIRE)
    )
    assert negotiated == NegotiatedEndpoint(
        api_version=CONTRACT_VERSION,
        protocol_version="1.0",
        descriptor_version="1.2",
    )


def test_a_server_window_below_the_client_maximum_selects_the_server_maximum() -> None:
    negotiated = negotiate_endpoint(
        descriptor(
            supported_api_versions=window(f"{CONTRACT_MAJOR}.0", f"{CONTRACT_MAJOR}.0")
        )
    )
    assert negotiated.api_version == f"{CONTRACT_MAJOR}.0"


def test_a_caller_supplied_client_window_is_honoured() -> None:
    negotiated = negotiate_endpoint(
        ServiceEndpointDescriptor.from_wire(DESCRIPTOR_WIRE),
        client_api_versions=window("1.0", "1.1"),
    )
    assert negotiated.api_version == "1.1"


def test_a_descriptor_whose_shape_major_is_unknown_is_refused_first() -> None:
    """Shape first: a descriptor this build cannot read has no trustworthy fields."""
    with pytest.raises(CompatibilityError, match="descriptor shape version"):
        negotiate_endpoint(descriptor(descriptor_version="2.0", protocol_version="9.9"))


def test_a_descriptor_advertising_another_protocol_major_is_refused() -> None:
    with pytest.raises(CompatibilityError, match="protocol major"):
        negotiate_endpoint(descriptor(protocol_version="2.0"))


def test_a_descriptor_with_no_shared_api_version_is_refused() -> None:
    with pytest.raises(CompatibilityError, match="no mutually supported API version"):
        negotiate_endpoint(descriptor(supported_api_versions=window("9.0", "9.4")))


def test_a_descriptor_with_a_reversed_window_is_refused() -> None:
    with pytest.raises(CompatibilityError, match="reversed"):
        negotiate_endpoint(descriptor(supported_api_versions=window("1.3", "1.0")))


def test_readiness_is_not_a_version_question() -> None:
    """A not-yet-ready endpoint is still an endpoint this build can speak to."""
    negotiated = negotiate_endpoint(descriptor(ready=False, lifecycle_state="starting"))
    assert negotiated.api_version == CONTRACT_VERSION


@pytest.mark.parametrize("value", [None, DESCRIPTOR_WIRE, "descriptor"])
def test_something_that_is_not_a_descriptor_is_refused(value: object) -> None:
    with pytest.raises(
        CompatibilityError, match="expected a ServiceEndpointDescriptor"
    ):
        negotiate_endpoint(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Public DTO decoding is not weakened
# --------------------------------------------------------------------------


def test_an_unknown_optional_field_stays_the_decoders_concern() -> None:
    """A newer minor's additive field decodes as it always did, and negotiates."""
    forward = {**DESCRIPTOR_WIRE, "descriptor_version": "1.9", "experimental_hint": 7}
    negotiated = negotiate_endpoint(ServiceEndpointDescriptor.from_wire(forward))
    assert negotiated.descriptor_version == "1.9"
    assert negotiated.api_version == CONTRACT_VERSION


def test_a_descriptor_missing_a_required_field_still_fails_to_decode() -> None:
    """Accepting a newer shape version must not have loosened this."""
    without_endpoint = {
        key: value for key, value in DESCRIPTOR_WIRE.items() if key != "endpoint_uri"
    }
    with pytest.raises(ContractDecodeError):
        ServiceEndpointDescriptor.from_wire(without_endpoint)


def test_negotiated_versions_are_frozen() -> None:
    negotiated = negotiate_endpoint(
        ServiceEndpointDescriptor.from_wire(DESCRIPTOR_WIRE)
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        negotiated.api_version = "9.9"  # type: ignore[misc]


def test_every_compatibility_failure_is_a_client_error() -> None:
    assert issubclass(CompatibilityError, ClientError)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda value: select_protocol_version(value), id="protocol"),
        pytest.param(lambda value: validate_descriptor_version(value), id="descriptor"),
        pytest.param(
            lambda value: select_api_version(
                window(value, "9.9"), window("1.0", "1.3")
            ),
            id="window-minimum",
        ),
        pytest.param(
            lambda value: select_api_version(
                window("1.0", "1.3"), window("1.0", value)
            ),
            id="window-maximum",
        ),
        pytest.param(lambda value: negotiate_endpoint(value), id="descriptor-object"),
    ],
)
@pytest.mark.parametrize(
    "value",
    [None, 1, 1.5, True, b"1.0", ["1.0"], {}, object(), "", "latest", "1", "1.0.0"],
)
def test_no_raw_type_error_or_contract_exception_escapes_any_entry_point(
    call: Any, value: object
) -> None:
    """The whole surface, crossed with every shape of malformed direct value.

    Stated as one exhaustive test rather than as a note, because the failure it
    guards against is silent: a caller writes `except ClientError`, and one
    unguarded argument path raises something that sails straight past it.
    """
    with pytest.raises(CompatibilityError):
        call(value)
