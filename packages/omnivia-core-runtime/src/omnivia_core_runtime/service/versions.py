"""The version facts this build answers protocol questions with (P2.1).

Three versions travel together and mean three different things, so they are kept
apart here rather than collapsed into one number:

* **API version** -- which revision of the public application contract this build
  implements. Derived from ``omnivia_core.contracts.v1.CONTRACT_VERSION``, never
  restated, because a transcribed copy is exactly how a build ends up claiming an
  API version it does not implement. It is currently ``1.2``; earlier runtime code
  hard-coded ``1.0``, which was already wrong by the time the contract moved.
* **Server version** -- the concrete build answering. Derived from
  ``omnivia_core_runtime.__version__``.
* **Protocol version** -- the OVC1 framing revision, which is exactly ``1.0`` and
  moves on its own schedule. Deliberately *not* derived from the API version: the
  framing can stay still while the application contract moves, and tying them
  together would force a framing bump for every additive contract release.

The workspace format is a fourth notation, and the only one that needs
translating. The private manifest records it as a single ordinal (``"1"``), while
every public shape that carries it -- ``ServiceEndpointDescriptor``,
``VersionCapabilityEnvelope``, ``VersionWindow`` -- requires a ``major.minor``
``ContractVersion``. :func:`workspace_contract_version` is the one boundary where
that translation happens, and it is a **lookup, not a formula**: ordinal ``1`` maps
to ``1.0`` because that pairing is frozen, and every other ordinal is refused. The
arithmetic spelling ``f"{ordinal}.0"`` reads like a translation but is really an
extrapolation -- it answers for ordinal ``2`` and ordinal ``99``, which name no
workspace format that exists, and publishes the invented version on a descriptor
where a peer would negotiate against it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ContractSemanticError,
    VersionWindow,
    parse_contract_version,
    validate_version_window,
)
from omnivia_core_runtime import __version__ as _BUILD_VERSION

#: The public application contract revision this build implements.
API_VERSION: Final[str] = CONTRACT_VERSION

#: The concrete server build answering. A `ReleaseVersion`, not a contract version.
SERVER_VERSION: Final[str] = _BUILD_VERSION

#: The OVC1 framing revision. Fixed at 1.0 and independent of :data:`API_VERSION`.
PROTOCOL_VERSION: Final[str] = "1.0"

#: A private workspace-format ordinal: a positive integer with no leading zeros and
#: no minor component. Written out rather than reused from the manifest module so
#: this boundary helper stays free of any storage-layer import.
_WORKSPACE_ORDINAL_RE: Final = re.compile(r"[1-9][0-9]*")

#: Every workspace format this build can translate, and the public contract version
#: each one is published as. One entry, because one pairing is frozen: the workspace
#: format recorded as ordinal ``1`` is published as ``1.0``.
#:
#: A table rather than the arithmetic ``f"{ordinal}.0"``. Which workspace formats
#: exist, and what public version each is known by, is a fact about releases that
#: have happened -- not a pattern that can be continued. A formula answers for every
#: positive integer, so a workspace stamped with an ordinal this build has never seen
#: would be translated into a confident-looking version rather than refused, and that
#: version would then travel on a descriptor and be negotiated against.
SUPPORTED_WORKSPACE_ORDINALS: Final[Mapping[int, str]] = MappingProxyType({1: "1.0"})


class WorkspaceVersionError(ValueError):
    """A private workspace-format ordinal could not be translated.

    Raised rather than returning a best guess, for two distinct refusals. An
    ordinal outside the grammar is not an ordinal at all: ``"1.0"`` is already a
    contract version, ``"01"`` is a noncanonical spelling of one, and ``"0"`` and
    ``"-1"`` name no workspace format that exists. An ordinal inside the grammar
    but outside :data:`SUPPORTED_WORKSPACE_ORDINALS` -- ``2``, ``99`` -- is
    well-formed and still untranslatable, because this build knows of no workspace
    format by that name. Accepting either would let this build publish a workspace
    version the workspace does not have.
    """


def workspace_contract_version(ordinal: object) -> str:
    """Translate a private workspace-format ordinal into a public contract version.

    ``ordinal`` is typed ``object`` on purpose: it arrives from the private
    manifest model, which may hand over either the stored string (``"1"``) or an
    integer, and neither is a type this module can vouch for. Everything outside
    the positive-integer grammar is refused -- booleans included, since ``True``
    would otherwise translate to ``"1.0"`` -- and so is every well-formed ordinal
    this build has no frozen translation for.
    """
    major = _workspace_ordinal(ordinal)
    version = SUPPORTED_WORKSPACE_ORDINALS.get(major)
    if version is None:
        known = ", ".join(str(entry) for entry in sorted(SUPPORTED_WORKSPACE_ORDINALS))
        raise WorkspaceVersionError(
            f"workspace format ordinal {major} is not a format this build knows: it "
            f"translates only {known}, and refuses to extrapolate the rest"
        )
    # Parsed rather than trusted: the result of this function is handed straight to
    # public shapes that require a real ContractVersion, so it is proven to be one
    # here rather than at the first transport that tries to encode it.
    parse_contract_version(version)
    return version


def _workspace_ordinal(ordinal: object) -> int:
    """Read ``ordinal`` as a positive integer, refusing every other spelling."""
    if isinstance(ordinal, bool):
        raise WorkspaceVersionError(
            "workspace format ordinal must be a positive integer, not a boolean"
        )
    if isinstance(ordinal, int):
        if ordinal < 1:
            raise WorkspaceVersionError(
                f"workspace format ordinal must be positive, got {ordinal!r}"
            )
        return ordinal
    if isinstance(ordinal, str):
        if _WORKSPACE_ORDINAL_RE.fullmatch(ordinal) is None:
            raise WorkspaceVersionError(
                f"{ordinal!r} is not a workspace format ordinal: expected a positive "
                "integer with no leading zeros and no minor component"
            )
        return int(ordinal)
    raise WorkspaceVersionError(
        "workspace format ordinal must be a string or an integer, got "
        f"{type(ordinal).__name__}"
    )


def build_version_window(minimum: str, maximum: str) -> VersionWindow:
    """Build an inclusive ``VersionWindow``, refusing a window nothing can negotiate.

    Two rules, both enforced with the public contract's own semantics rather than
    a local reimplementation. ``validate_version_window`` rejects a bound that is
    not a contract version and a window whose minimum exceeds its maximum. On top
    of that, the bounds must share a major: a major change is breaking by
    definition, so a window spanning two majors advertises a range within which
    negotiation is not actually possible.
    """
    window = VersionWindow(minimum=minimum, maximum=maximum)
    validate_version_window(window)
    lower_major, _ = parse_contract_version(window.minimum)
    upper_major, _ = parse_contract_version(window.maximum)
    if lower_major != upper_major:
        raise ContractSemanticError(
            f"version window spans majors: minimum {window.minimum!r} and maximum "
            f"{window.maximum!r} cannot both be negotiated"
        )
    return window


def supported_api_versions() -> VersionWindow:
    """The API contract window this build supports.

    One version wide today, because this build implements exactly one revision of
    the application contract and advertising a range it has not been tested
    against would be a claim, not a fact.
    """
    return build_version_window(API_VERSION, API_VERSION)


def supported_workspace_versions(ordinal: object) -> VersionWindow:
    """The workspace-format window for a workspace stored at ``ordinal``.

    Fails closed with the translation it is built on: an ordinal this build has no
    frozen version for produces no window at all, rather than a window advertising
    a workspace format that was invented one line earlier.
    """
    version = workspace_contract_version(ordinal)
    return build_version_window(version, version)


__all__ = [
    "API_VERSION",
    "PROTOCOL_VERSION",
    "SERVER_VERSION",
    "SUPPORTED_WORKSPACE_ORDINALS",
    "WorkspaceVersionError",
    "build_version_window",
    "supported_api_versions",
    "supported_workspace_versions",
    "workspace_contract_version",
]
