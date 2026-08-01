"""Tests for pure version and capability compatibility semantics (ADR-038)."""

from __future__ import annotations

import pytest

from omnivia_core.contracts.v1 import compatibility as compat
from omnivia_core.contracts.v1.generated import (
    CapabilityRef,
    CapabilityRequirement,
    CapabilitySet,
    CompatibilityMetadata,
    GrantedAuthority,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.10", (1, 10)),
        ("0.0", (0, 0)),
        ("0.1", (0, 1)),
        ("10.0", (10, 0)),
        ("123.456", (123, 456)),
    ],
)
def test_parse_contract_version(version: str, expected: tuple[int, int]) -> None:
    assert compat.parse_contract_version(version) == expected


def test_parse_contract_version_rejects_malformed_input() -> None:
    with pytest.raises(ValueError, match="is not a `major.minor` contract version"):
        compat.parse_contract_version("1")


# Every spelling here is outside `CONTRACT_VERSION_PATTERN`
# (`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`) while still being something a looser
# parser would read a number out of: `int` accepts leading zeros, a sign,
# surrounding whitespace, and non-ASCII decimal digits, and `str.isdigit` accepts
# the Unicode ones too. Two peers that disagree on whether `"01.2"` and `"1.2"`
# name the same version cannot negotiate, so none of these is a ContractVersion.
NONCANONICAL_VERSIONS: tuple[str, ...] = (
    "01.2",  # leading zero, major
    "1.02",  # leading zero, minor
    "00.0",
    "0001.0002",
    "١.٢",  # Arabic-Indic digits
    "1.٢",  # mixed ASCII and Arabic-Indic digits
    "१.२",  # Devanagari digits
    "１.２",  # fullwidth digits
    "1.2 ",
    " 1.2",
    "1 . 2",
    "\n1.2",
    "1.2\n",
    "+1.2",
    "1.+2",
    "-1.2",
    "1_0.2",
)


@pytest.mark.parametrize("version", NONCANONICAL_VERSIONS)
def test_parse_contract_version_rejects_a_noncanonical_spelling(version: str) -> None:
    with pytest.raises(ValueError, match="is not a `major.minor` contract version"):
        compat.parse_contract_version(version)


@pytest.mark.parametrize("version", NONCANONICAL_VERSIONS)
def test_compare_contract_versions_rejects_a_noncanonical_spelling(version: str) -> None:
    """Comparison inherits the parser's grammar: a noncanonical spelling is never quietly
    ordered against a canonical one.
    """
    with pytest.raises(ValueError, match="is not a `major.minor` contract version"):
        compat.compare_contract_versions(version, "1.2")


@pytest.mark.parametrize(
    ("a", "b", "expected_sign"),
    [
        ("1.2", "1.10", -1),  # numeric, not lexicographic, comparison
        ("1.10", "1.2", 1),
        ("2.0", "1.99", 1),
        ("1.5", "1.5", 0),
    ],
)
def test_compare_contract_versions(a: str, b: str, expected_sign: int) -> None:
    result = compat.compare_contract_versions(a, b)
    assert (result > 0) - (result < 0) == expected_sign


def test_version_in_window() -> None:
    window = VersionWindow(minimum="1.0", maximum="1.5")
    assert compat.version_in_window("1.0", window)
    assert compat.version_in_window("1.5", window)
    assert compat.version_in_window("1.3", window)
    assert not compat.version_in_window("1.6", window)
    assert not compat.version_in_window("0.9", window)


@pytest.mark.parametrize(
    ("version", "expected_status"),
    [
        ("1.3", "compatible"),
        ("1.0", "compatible"),
        ("1.5", "compatible"),
        ("1.9", "upgrade_required"),
        ("2.0", "incompatible"),
        ("0.9", "incompatible"),
    ],
)
def test_classify_version_compatibility(version: str, expected_status: str) -> None:
    window = VersionWindow(minimum="1.0", maximum="1.5")
    assert compat.classify_version_compatibility(version, window) == expected_status


def test_classify_version_compatibility_below_window_same_major_needs_upgrade() -> None:
    window = VersionWindow(minimum="1.2", maximum="1.5")
    assert compat.classify_version_compatibility("1.0", window) == "upgrade_required"


def test_effective_capabilities_is_the_id_and_version_intersection() -> None:
    supported = (
        CapabilityRef(id="memory.read", version="1.0"),
        CapabilityRef(id="memory.write", version="1.0"),
    )
    granted = (CapabilityRef(id="memory.read", version="1.0"),)
    assert compat.effective_capabilities(supported, granted) == (
        CapabilityRef(id="memory.read", version="1.0"),
    )


def test_effective_capabilities_requires_exact_version_match() -> None:
    supported = (CapabilityRef(id="memory.read", version="1.1"),)
    granted = (CapabilityRef(id="memory.read", version="1.0"),)
    assert compat.effective_capabilities(supported, granted) == ()


def test_effective_capabilities_is_never_wider_than_either_input() -> None:
    supported = (CapabilityRef(id="memory.read", version="1.0"),)
    granted = (
        CapabilityRef(id="memory.read", version="1.0"),
        CapabilityRef(id="memory.write", version="1.0"),
    )
    assert compat.effective_capabilities(supported, granted) == supported


def test_duplicate_capability_ids_detects_repeats() -> None:
    refs = (
        CapabilityRef(id="memory.search", version="1.0"),
        CapabilityRef(id="memory.read", version="1.0"),
        CapabilityRef(id="memory.search", version="1.1"),
    )
    assert compat.duplicate_capability_ids(refs) == ("memory.search",)


def test_duplicate_capability_ids_empty_when_all_unique() -> None:
    refs = (
        CapabilityRef(id="memory.search", version="1.0"),
        CapabilityRef(id="memory.read", version="1.0"),
    )
    assert compat.duplicate_capability_ids(refs) == ()


def test_resolve_capability_requirements_all_satisfied() -> None:
    effective = (CapabilityRef(id="memory.read", version="1.2"),)
    requirements = (CapabilityRequirement(id="memory.read", minimum_version="1.0", required=True),)
    resolution = compat.resolve_capability_requirements(requirements, effective)
    assert resolution.satisfied
    assert resolution.unmet_required == ()
    assert resolution.unmet_optional == ()


def test_resolve_capability_requirements_unmet_required_fails() -> None:
    effective = ()
    requirement = CapabilityRequirement(id="memory.export", minimum_version="1.0", required=True)
    resolution = compat.resolve_capability_requirements((requirement,), effective)
    assert not resolution.satisfied
    assert resolution.unmet_required == (requirement,)


def test_resolve_capability_requirements_unmet_optional_degrades_not_fails() -> None:
    effective = ()
    requirement = CapabilityRequirement(id="memory.export", minimum_version="1.0", required=False)
    resolution = compat.resolve_capability_requirements((requirement,), effective)
    assert resolution.satisfied
    assert resolution.unmet_optional == (requirement,)


def test_resolve_capability_requirements_version_below_minimum_is_unmet() -> None:
    effective = (CapabilityRef(id="memory.read", version="1.0"),)
    requirement = CapabilityRequirement(id="memory.read", minimum_version="1.5", required=True)
    resolution = compat.resolve_capability_requirements((requirement,), effective)
    assert not resolution.satisfied


def test_resolve_capability_requirements_same_major_higher_minor_is_met() -> None:
    effective = (CapabilityRef(id="memory.read", version="1.7"),)
    requirement = CapabilityRequirement(id="memory.read", minimum_version="1.2", required=True)
    resolution = compat.resolve_capability_requirements((requirement,), effective)
    assert resolution.satisfied


def test_resolve_capability_requirements_higher_major_fails_safe_as_unmet() -> None:
    """A higher major is a breaking change by definition: it must never be assumed to satisfy
    a requirement pinned to a lower major, even though it numerically compares greater.
    """
    effective = (CapabilityRef(id="memory.read", version="2.0"),)
    requirement = CapabilityRequirement(id="memory.read", minimum_version="1.2", required=True)
    resolution = compat.resolve_capability_requirements((requirement,), effective)
    assert not resolution.satisfied
    assert resolution.unmet_required == (requirement,)


def test_resolve_capability_requirements_lower_major_fails_safe_as_unmet() -> None:
    effective = (CapabilityRef(id="memory.read", version="1.9"),)
    requirement = CapabilityRequirement(id="memory.read", minimum_version="2.0", required=True)
    resolution = compat.resolve_capability_requirements((requirement,), effective)
    assert not resolution.satisfied


def test_validate_version_window_accepts_a_well_formed_window() -> None:
    compat.validate_version_window(VersionWindow(minimum="1.0", maximum="1.5"))  # must not raise


def test_validate_version_window_accepts_a_single_point_window() -> None:
    compat.validate_version_window(VersionWindow(minimum="1.5", maximum="1.5"))  # must not raise


def test_validate_version_window_rejects_a_reversed_window() -> None:
    with pytest.raises(compat.ContractSemanticError, match="reversed"):
        compat.validate_version_window(VersionWindow(minimum="1.5", maximum="1.0"))


@pytest.mark.parametrize(
    ("minimum", "maximum", "expected_bound"),
    [
        ("one", "1.5", "minimum"),
        ("1.0", "latest", "maximum"),
        ("1", "1.5", "minimum"),
        ("", "1.5", "minimum"),
    ],
)
def test_validate_version_window_rejects_a_bound_that_is_not_a_contract_version(
    minimum: str, maximum: str, expected_bound: str
) -> None:
    """A bound that does not parse makes every membership query unanswerable, so it is a
    semantic rejection, not a raw ``ValueError`` escaping from an internal helper.
    """
    with pytest.raises(compat.ContractSemanticError, match=f"version window {expected_bound}"):
        compat.validate_version_window(VersionWindow(minimum=minimum, maximum=maximum))


def test_version_in_window_rejects_a_reversed_window() -> None:
    with pytest.raises(compat.ContractSemanticError, match="reversed"):
        compat.version_in_window("1.2", VersionWindow(minimum="1.5", maximum="1.0"))


def test_classify_version_compatibility_rejects_a_reversed_window() -> None:
    with pytest.raises(compat.ContractSemanticError, match="reversed"):
        compat.classify_version_compatibility("1.2", VersionWindow(minimum="1.5", maximum="1.0"))


def _capability_set(
    supported: tuple[CapabilityRef, ...],
    granted: tuple[CapabilityRef, ...],
    effective: tuple[CapabilityRef, ...],
) -> CapabilitySet:
    return CapabilitySet(supported=supported, granted=granted, effective=effective)


def test_validate_capability_set_accepts_the_exact_intersection() -> None:
    read = CapabilityRef(id="memory.read", version="1.0")
    write = CapabilityRef(id="memory.write", version="1.0")
    capabilities = _capability_set(supported=(read, write), granted=(read,), effective=(read,))
    compat.validate_capability_set(capabilities)  # must not raise


def test_validate_capability_set_rejects_effective_wider_than_the_intersection() -> None:
    """A claimed capability neither side actually agrees on must never appear in `effective`:
    this is the authority-widening case a client must never be able to induce.
    """
    read = CapabilityRef(id="memory.read", version="1.0")
    write = CapabilityRef(id="memory.write", version="1.0")
    capabilities = _capability_set(supported=(read,), granted=(read,), effective=(read, write))
    with pytest.raises(compat.ContractSemanticError, match="effective"):
        compat.validate_capability_set(capabilities)


def test_validate_capability_set_rejects_effective_narrower_than_the_intersection() -> None:
    read = CapabilityRef(id="memory.read", version="1.0")
    capabilities = _capability_set(supported=(read,), granted=(read,), effective=())
    with pytest.raises(compat.ContractSemanticError, match="effective"):
        compat.validate_capability_set(capabilities)


def test_validate_capability_set_rejects_duplicate_ids_in_supported() -> None:
    refs = (
        CapabilityRef(id="memory.read", version="1.0"),
        CapabilityRef(id="memory.read", version="1.1"),
    )
    capabilities = _capability_set(supported=refs, granted=(), effective=())
    with pytest.raises(compat.ContractSemanticError, match="duplicate"):
        compat.validate_capability_set(capabilities)


def test_validate_capability_set_rejects_duplicate_ids_in_granted() -> None:
    refs = (
        CapabilityRef(id="memory.read", version="1.0"),
        CapabilityRef(id="memory.read", version="1.1"),
    )
    capabilities = _capability_set(supported=refs, granted=refs, effective=())
    with pytest.raises(compat.ContractSemanticError, match="duplicate"):
        compat.validate_capability_set(capabilities)


# --------------------------------------------------------------------------
# GrantedAuthority: the one authority statement a client may trust must never
# exceed the authority the exchange actually negotiated.
# --------------------------------------------------------------------------

READ = CapabilityRef(id="memory.read", version="1.0")
WRITE = CapabilityRef(id="memory.write", version="1.0")


def _authority(*capabilities: CapabilityRef) -> GrantedAuthority:
    return GrantedAuthority(
        principal_id="user-1", roles=("member",), capabilities=capabilities
    )


def test_validate_granted_authority_accepts_authority_equal_to_effective() -> None:
    capabilities = _capability_set(
        supported=(READ, WRITE), granted=(READ, WRITE), effective=(READ, WRITE)
    )
    compat.validate_granted_authority(_authority(READ, WRITE), capabilities)  # must not raise


def test_validate_granted_authority_accepts_a_narrower_subset_of_effective() -> None:
    """Authority narrower than what was negotiated is legitimate: a principal need not hold
    everything the exchange made available.
    """
    capabilities = _capability_set(
        supported=(READ, WRITE), granted=(READ, WRITE), effective=(READ, WRITE)
    )
    compat.validate_granted_authority(_authority(READ), capabilities)  # must not raise


def test_validate_granted_authority_accepts_an_empty_authority_set() -> None:
    """Claiming nothing can never claim too much, whatever was negotiated."""
    capabilities = _capability_set(supported=(READ,), granted=(READ,), effective=(READ,))
    compat.validate_granted_authority(_authority(), capabilities)  # must not raise


def test_validate_granted_authority_accepts_an_empty_authority_against_empty_effective() -> None:
    compat.validate_granted_authority(
        _authority(), _capability_set(supported=(), granted=(), effective=())
    )  # must not raise


def test_validate_granted_authority_rejects_authority_wider_than_effective() -> None:
    """The authority-widening case: `effective` is internally consistent, and the widening
    happens one field over, in the authority a client is told it may trust.
    """
    capabilities = _capability_set(supported=(READ,), granted=(READ,), effective=(READ,))
    with pytest.raises(compat.ContractSemanticError, match="authority"):
        compat.validate_granted_authority(_authority(READ, WRITE), capabilities)


def test_validate_granted_authority_rejects_authority_at_a_version_never_effective() -> None:
    """Identity is the exact (id, version) pair: authority at a version the exchange never
    made effective is as unnegotiated as an id that never appeared.
    """
    capabilities = _capability_set(supported=(READ,), granted=(READ,), effective=(READ,))
    newer = CapabilityRef(id="memory.read", version="1.1")
    with pytest.raises(compat.ContractSemanticError, match="not effective"):
        compat.validate_granted_authority(_authority(newer), capabilities)


def test_validate_granted_authority_rejects_duplicate_authority_ids() -> None:
    capabilities = _capability_set(supported=(READ,), granted=(READ,), effective=(READ,))
    duplicated = CapabilityRef(id="memory.read", version="1.1")
    with pytest.raises(compat.ContractSemanticError, match="duplicate"):
        compat.validate_granted_authority(_authority(READ, duplicated), capabilities)


def test_validate_granted_authority_rejects_authority_against_empty_effective() -> None:
    """An exchange that negotiated nothing effective cannot have granted any authority."""
    with pytest.raises(compat.ContractSemanticError, match="not effective"):
        compat.validate_granted_authority(
            _authority(READ), _capability_set(supported=(READ,), granted=(), effective=())
        )


# --------------------------------------------------------------------------
# VersionCapabilityEnvelope: an envelope states each contract version twice
# (in force, and as negotiation selected it). The two must agree, must parse,
# and the selected one must fall inside the window the same envelope publishes.
# --------------------------------------------------------------------------


def _envelope(
    *,
    api_version: str = "1.2",
    workspace_format_version: str = "1.0",
    selected_api_version: str = "1.2",
    selected_workspace_version: str = "1.0",
    supported_api_versions: tuple[str, str] = ("1.0", "1.3"),
    supported_workspace_versions: tuple[str, str] = ("1.0", "1.0"),
    status: str = "compatible",
) -> VersionCapabilityEnvelope:
    """A well-formed envelope, with every version fact overridable one at a time."""
    return VersionCapabilityEnvelope(
        api_version=api_version,
        server_version="1.2.5",
        workspace_format_version=workspace_format_version,
        compatibility=CompatibilityMetadata(
            selected_api_version=selected_api_version,
            selected_workspace_version=selected_workspace_version,
            supported_api_versions=VersionWindow(
                minimum=supported_api_versions[0], maximum=supported_api_versions[1]
            ),
            supported_workspace_versions=VersionWindow(
                minimum=supported_workspace_versions[0], maximum=supported_workspace_versions[1]
            ),
            status=status,
            upgrade_state=UpgradeState(value="none"),
            deprecations=(),
        ),
        capabilities=CapabilitySet(supported=(), granted=(), effective=()),
    )


def test_validate_version_capability_envelope_accepts_a_consistent_envelope() -> None:
    compat.validate_version_capability_envelope(_envelope())  # must not raise


def test_validate_version_capability_envelope_accepts_versions_at_the_window_edges() -> None:
    compat.validate_version_capability_envelope(
        _envelope(api_version="1.0", selected_api_version="1.0")
    )
    compat.validate_version_capability_envelope(
        _envelope(api_version="1.3", selected_api_version="1.3")
    )


@pytest.mark.parametrize("malformed", ["9", "", "1.", ".1", "1.2.3", "v1.2", "latest", "1.x"])
def test_validate_version_capability_envelope_rejects_a_malformed_api_version(
    malformed: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="api_version"):
        compat.validate_version_capability_envelope(
            _envelope(api_version=malformed, selected_api_version=malformed)
        )


@pytest.mark.parametrize("malformed", ["9", "", "1.", "1.2.3", "latest"])
def test_validate_version_capability_envelope_rejects_a_malformed_workspace_version(
    malformed: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="workspace"):
        compat.validate_version_capability_envelope(
            _envelope(
                workspace_format_version=malformed, selected_workspace_version=malformed
            )
        )


@pytest.mark.parametrize("malformed", ["9", "", "1.", "1.2.3", "latest"])
def test_validate_version_capability_envelope_rejects_a_malformed_selected_api_version(
    malformed: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="selected_api_version"):
        compat.validate_version_capability_envelope(
            _envelope(selected_api_version=malformed)
        )


@pytest.mark.parametrize("malformed", ["9", "", "1.", "1.2.3", "latest"])
def test_validate_version_capability_envelope_rejects_a_malformed_selected_workspace_version(
    malformed: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="selected_workspace_version"):
        compat.validate_version_capability_envelope(
            _envelope(selected_workspace_version=malformed)
        )


def test_validate_version_capability_envelope_rejects_api_version_contradicting_selected() -> None:
    """The reported release blocker: an envelope claiming `api_version` 9.9 while
    negotiation selected 1.2 tells two callers two different things.
    """
    with pytest.raises(compat.ContractSemanticError, match="api_version '9.9' disagrees"):
        compat.validate_version_capability_envelope(_envelope(api_version="9.9"))


def test_validate_version_capability_envelope_rejects_a_selected_api_version_it_does_not_echo() -> (
    None
):
    """The same disagreement from the other side: the selected version moves, the version
    in force does not.
    """
    with pytest.raises(compat.ContractSemanticError, match="disagrees"):
        compat.validate_version_capability_envelope(_envelope(selected_api_version="1.3"))


def test_validate_version_capability_envelope_rejects_workspace_version_contradicting_selected() -> (
    None
):
    with pytest.raises(
        compat.ContractSemanticError, match="workspace_format_version '2.0' disagrees"
    ):
        compat.validate_version_capability_envelope(_envelope(workspace_format_version="2.0"))


@pytest.mark.parametrize("selected", ["1.4", "0.9", "2.0"])
def test_validate_version_capability_envelope_rejects_a_selected_api_version_outside_its_window(
    selected: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="falls outside the declared"):
        compat.validate_version_capability_envelope(
            _envelope(api_version=selected, selected_api_version=selected)
        )


@pytest.mark.parametrize("selected", ["1.1", "0.9", "2.0"])
def test_validate_version_capability_envelope_rejects_a_selected_workspace_version_outside_window(
    selected: str,
) -> None:
    with pytest.raises(
        compat.ContractSemanticError, match="supported_workspace_versions"
    ):
        compat.validate_version_capability_envelope(
            _envelope(workspace_format_version=selected, selected_workspace_version=selected)
        )


def test_validate_version_capability_envelope_rejects_a_reversed_supported_window() -> None:
    with pytest.raises(compat.ContractSemanticError, match="reversed"):
        compat.validate_version_capability_envelope(
            _envelope(supported_api_versions=("1.3", "1.0"))
        )


def test_validate_version_capability_envelope_rejects_a_malformed_window_bound() -> None:
    with pytest.raises(compat.ContractSemanticError, match="version window"):
        compat.validate_version_capability_envelope(
            _envelope(supported_api_versions=("1.0", "unbounded"))
        )


# The envelope-level counterpart of the parser tests at the top of this module:
# rejecting a noncanonical spelling in `parse_contract_version` only protects the
# envelope if every version field actually routes through it. Each field is
# mutated on its own so a field that quietly stopped being parsed is named.


@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_validate_version_capability_envelope_rejects_a_noncanonical_version_in_force(
    noncanonical: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="is not a `major.minor`"):
        compat.validate_version_capability_envelope(_envelope(api_version=noncanonical))


@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_validate_version_capability_envelope_rejects_a_noncanonical_selected_version(
    noncanonical: str,
) -> None:
    with pytest.raises(compat.ContractSemanticError, match="selected_api_version"):
        compat.validate_version_capability_envelope(
            _envelope(selected_api_version=noncanonical)
        )


@pytest.mark.parametrize("bound", [0, 1])
@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_validate_version_capability_envelope_rejects_a_noncanonical_window_bound(
    bound: int, noncanonical: str
) -> None:
    window = ["1.0", "1.3"]
    window[bound] = noncanonical
    expected = "minimum" if bound == 0 else "maximum"
    with pytest.raises(compat.ContractSemanticError, match=f"version window {expected}"):
        compat.validate_version_capability_envelope(
            _envelope(supported_api_versions=(window[0], window[1]))
        )


@pytest.mark.parametrize(
    "status",
    ["compatible", "compatible_with_deprecations", "a_status_this_build_has_never_seen"],
)
def test_validate_version_capability_envelope_leaves_the_open_status_vocabulary_alone(
    status: str,
) -> None:
    """`CompatibilityStatus` is an open vocabulary and no frozen invariant ties it to the
    version facts here, so an unknown value from a newer peer must still validate.
    """
    compat.validate_version_capability_envelope(_envelope(status=status))  # must not raise


def test_validate_version_capability_envelope_still_validates_the_capability_set() -> None:
    """The version checks are added to, not substituted for, the capability-set invariants."""
    read = CapabilityRef(id="memory.read", version="1.0")
    envelope = _envelope()
    contradictory = VersionCapabilityEnvelope(
        api_version=envelope.api_version,
        server_version=envelope.server_version,
        workspace_format_version=envelope.workspace_format_version,
        compatibility=envelope.compatibility,
        capabilities=CapabilitySet(supported=(), granted=(), effective=(read,)),
    )
    with pytest.raises(compat.ContractSemanticError, match="effective"):
        compat.validate_version_capability_envelope(contradictory)
