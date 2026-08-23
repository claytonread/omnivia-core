"""Runtime Execution Planes: the non-authoritative descriptors and their hashes.

This is the shared vocabulary layer of an isolated, package-neutral seam. It
holds no database, scheduler, recovery, transport, filesystem or Platform
handle, and it never writes canonical state. Everything here is a frozen value
plus bounded validation over closed vocabularies.

Three descriptors live here, and the split is the point:

- :class:`RuntimeProfileDescriptor` *describes* what an execution plane claims
  to offer -- its execution classes, capability sets, worker routes and session
  providers, its minimum isolation and its profile trust state.
- :class:`ExecutorDescriptor` describes one concrete executor build: its
  executor kind, the capabilities that build actually implements, the contract
  versions it speaks, the isolation it requires, how it reconciles, how it is
  removed, and the exact ``sha256`` build hash the trust state was granted
  against.
- :class:`SessionProviderDescriptor` describes one concrete session provider
  build: the session kinds it serves, whether it can suspend/resume and transfer
  control, its required isolation and its build hash.

An executor kind and an execution class are **different vocabularies** and are
never equated. An execution class says what shape of work a *profile* runs
(``AGENT``, ``DETERMINISTIC``, ``EFFECT``, ``WAIT``); an executor kind says what
a *build* is (``BROWSER``, ``INTEGRATION``, ``FILESYSTEM``, ``COMMAND``, ``GIT``,
``OTHER``). One ``COMMAND`` executor can serve capabilities a deterministic
profile and an effect profile both declare, so collapsing the two would make the
executor's own identity a routing decision it has no standing to make.

All three descriptors are deliberately **non-authoritative**: nothing in this
package treats a descriptor as a grant, and a descriptor that declares a
capability is a necessary, never a sufficient, condition for that capability to
run.

Identity is content-addressed. ``sealed()`` stamps a descriptor with the digest
of the RFC 8785 canonical bytes of every *other* field, so two logically
equivalent descriptors seal to the same hash and any material change seals to a
different one. The digest spelling is exactly ``sha256:<64 lowercase hex>``
everywhere in this seam -- content hash and build hash alike.

``source_id`` is carried by the two *registrable* descriptors and by no other,
because it is the registry's qualifier rather than part of what a build claims
about itself: two sources may publish the same ``executor_id``, and one must
never satisfy a request for the other. A profile is never registered, so it
carries no source.

**Lineage.** ``service.worker_adapter.HostLineage`` carries the same four
fields, and is deliberately not reused: that module is a private service port,
so importing it would make this package-neutral seam depend on the service
plane it is meant to sit beside. :class:`ExecutionLineage` is the minimal
local value instead, and the two stay structurally identical by convention
rather than by an import that would fix the dependency direction the wrong way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from hashlib import sha256
from typing import Final, Self

from omnivia_core.contracts.v1 import ContractSemanticError
from omnivia_core.contracts.v1.canonical_json import canonical_bytes

#: The one contract version this seam speaks. An executor build that does not
#: list it is refused, never negotiated down to.
CONTRACT_VERSION: Final = "1.0.0"

#: The four reference execution classes. ``AGENT`` is a model-driven plane,
#: ``DETERMINISTIC`` a pure function of its request, ``EFFECT`` a plane that
#: changes something outside the seam, and ``WAIT`` one that only blocks on an
#: external condition.
EXECUTION_CLASS_AGENT: Final = "AGENT"
EXECUTION_CLASS_DETERMINISTIC: Final = "DETERMINISTIC"
EXECUTION_CLASS_EFFECT: Final = "EFFECT"
EXECUTION_CLASS_WAIT: Final = "WAIT"

EXECUTION_CLASSES: Final[frozenset[str]] = frozenset(
    {
        EXECUTION_CLASS_AGENT,
        EXECUTION_CLASS_DETERMINISTIC,
        EXECUTION_CLASS_EFFECT,
        EXECUTION_CLASS_WAIT,
    }
)

#: What an executor build *is*, which is not what a profile *runs*. This
#: vocabulary is disjoint from :data:`EXECUTION_CLASSES` by construction.
EXECUTOR_KIND_BROWSER: Final = "BROWSER"
EXECUTOR_KIND_INTEGRATION: Final = "INTEGRATION"
EXECUTOR_KIND_FILESYSTEM: Final = "FILESYSTEM"
EXECUTOR_KIND_COMMAND: Final = "COMMAND"
EXECUTOR_KIND_GIT: Final = "GIT"
EXECUTOR_KIND_OTHER: Final = "OTHER"

EXECUTOR_KINDS: Final[frozenset[str]] = frozenset(
    {
        EXECUTOR_KIND_BROWSER,
        EXECUTOR_KIND_INTEGRATION,
        EXECUTOR_KIND_FILESYSTEM,
        EXECUTOR_KIND_COMMAND,
        EXECUTOR_KIND_GIT,
        EXECUTOR_KIND_OTHER,
    }
)

#: The closed set of stateful session kinds a provider may serve.
SESSION_KIND_BROWSER: Final = "BROWSER"
SESSION_KIND_TERMINAL: Final = "TERMINAL"
SESSION_KIND_ACP: Final = "ACP"
SESSION_KIND_REMOTE_EXECUTION: Final = "REMOTE_EXECUTION"
SESSION_KIND_OTHER: Final = "OTHER"

SESSION_KINDS: Final[frozenset[str]] = frozenset(
    {
        SESSION_KIND_BROWSER,
        SESSION_KIND_TERMINAL,
        SESSION_KIND_ACP,
        SESSION_KIND_REMOTE_EXECUTION,
        SESSION_KIND_OTHER,
    }
)

#: Isolation is an ordinal, not a name: 0 in-process, 1 subprocess, 2 sandboxed,
#: 3 remote. An integer because the only question ever asked of it is "is this
#: at least as isolated as required", which a closed set of names cannot answer.
ISOLATION_MIN: Final = 0
ISOLATION_MAX: Final = 3

#: Profile trust. ``DRAFT`` is authored but ungranted, ``APPROVED`` is routable,
#: ``QUARANTINED`` is terminal until replaced, ``DISABLED`` is switched off.
PROFILE_TRUST_DRAFT: Final = "DRAFT"
PROFILE_TRUST_APPROVED: Final = "APPROVED"
PROFILE_TRUST_QUARANTINED: Final = "QUARANTINED"
PROFILE_TRUST_DISABLED: Final = "DISABLED"

PROFILE_TRUST_STATES: Final[frozenset[str]] = frozenset(
    {
        PROFILE_TRUST_DRAFT,
        PROFILE_TRUST_APPROVED,
        PROFILE_TRUST_QUARANTINED,
        PROFILE_TRUST_DISABLED,
    }
)

#: Build trust, shared by executors and session providers because both are a
#: build identified by a ``sha256`` build hash, and trust is granted against
#: that build rather than against the name in front of it. ``PROTOTYPE`` is the
#: pre-grant state a profile's ``DRAFT`` corresponds to.
BUILD_TRUST_PROTOTYPE: Final = "PROTOTYPE"
BUILD_TRUST_APPROVED: Final = "APPROVED"
BUILD_TRUST_QUARANTINED: Final = "QUARANTINED"
BUILD_TRUST_DISABLED: Final = "DISABLED"

BUILD_TRUST_STATES: Final[frozenset[str]] = frozenset(
    {
        BUILD_TRUST_PROTOTYPE,
        BUILD_TRUST_APPROVED,
        BUILD_TRUST_QUARANTINED,
        BUILD_TRUST_DISABLED,
    }
)

HEALTH_CONFIGURED: Final = "CONFIGURED"
HEALTH_AUTHENTICATED: Final = "AUTHENTICATED"
HEALTH_COMPATIBLE: Final = "COMPATIBLE"
HEALTH_READY: Final = "READY"
HEALTH_RECOVERABLE: Final = "RECOVERABLE"
HEALTH_QUARANTINED: Final = "QUARANTINED"

#: ``CONFIGURED`` -> ``AUTHENTICATED`` -> ``COMPATIBLE`` -> ``READY`` is the
#: admission ladder an entry climbs; ``RECOVERABLE`` is a transient failure it
#: can climb back from; ``QUARANTINED`` is terminal until the entry is replaced.
#: The states name *posture*, never a credential: nothing here carries a secret.
HEALTH_STATES: Final[frozenset[str]] = frozenset(
    {
        HEALTH_CONFIGURED,
        HEALTH_AUTHENTICATED,
        HEALTH_COMPATIBLE,
        HEALTH_READY,
        HEALTH_RECOVERABLE,
        HEALTH_QUARANTINED,
    }
)

#: Only a fully ready entry is routable. Every other state fails closed.
ROUTABLE_HEALTH: Final[frozenset[str]] = frozenset({HEALTH_READY})

_MAX_COLLECTION_LENGTH: Final = 64
_UNSEALED_CONTENT_HASH: Final = "sha256:" + "0" * 64

#: Every string this seam carries is one of four shapes: a member of a closed
#: uppercase vocabulary above, a bounded lowercase identifier, a semantic
#: version, or a digest. There is no free-text field, so "carries no secret" is
#: a structural property rather than a pattern match.
_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SEMVER: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExecutionError(Exception):
    """Base error for the execution seam, carrying one stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


class ExecutionContractError(ExecutionError):
    """A value failed bounds, vocabulary, shape or hash validation."""


class ExecutionRefused(ExecutionError):
    """A fail-closed refusal: the input was well formed and not admitted."""


def require_identifier(field_name: str, value: str) -> str:
    """Return ``value`` if it is a bounded lowercase identifier, else refuse it.

    Identifiers are the *open* vocabularies -- capability names, worker routes,
    provider ids, holders. The closed vocabularies are uppercase and are checked
    for membership by :func:`require_vocabulary` instead, so the two can never be
    confused for one another.

    Public because :mod:`planes` and :mod:`registry` validate their own inputs
    against exactly this shape; a second spelling of "identifier" in this seam
    would be a second, quietly different trust boundary.
    """
    if _IDENTIFIER.fullmatch(value) is None:
        raise ExecutionContractError(
            "invalid_identifier",
            f"{field_name} is not a bounded lowercase identifier",
        )
    return value


def require_version(field_name: str, value: str) -> str:
    """Return ``value`` if it is an exact three-part version, else refuse it."""
    if _SEMVER.fullmatch(value) is None:
        raise ExecutionContractError(
            "invalid_version", f"{field_name} is not an exact three-part version"
        )
    return value


def require_isolation(field_name: str, value: int) -> int:
    """Return ``value`` if it is an isolation ordinal in ``0..3``, else refuse it."""
    if not ISOLATION_MIN <= value <= ISOLATION_MAX:
        raise ExecutionContractError(
            "invalid_isolation",
            f"{field_name} must be an integer in {ISOLATION_MIN}..{ISOLATION_MAX}",
        )
    return value


def is_digest(value: str) -> bool:
    """Return whether ``value`` is spelled exactly ``sha256:<64 lowercase hex>``."""
    return _DIGEST.fullmatch(value) is not None


def require_digest(field_name: str, value: str) -> str:
    """Return ``value`` if it is spelled ``sha256:<64 lowercase hex>``, else refuse it."""
    if not is_digest(value):
        raise ExecutionContractError(
            "invalid_digest",
            f"{field_name} is not spelled sha256:<64 lowercase hex>",
        )
    return value


def require_vocabulary(field_name: str, value: str, allowed: frozenset[str]) -> str:
    """Return ``value`` if it is a member of ``allowed``, else refuse it.

    Membership is the whole check: the closed vocabularies are exact uppercase
    spellings, so ``"agent"`` is not a lenient spelling of ``AGENT`` -- it is a
    value that is not a member.
    """
    if value not in allowed:
        raise ExecutionContractError(
            "unknown_vocabulary_member",
            f"{field_name} {value!r} is not a member of its closed vocabulary",
        )
    return value


def require_collection(
    field_name: str,
    values: tuple[str, ...],
    *,
    required: bool,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Bound-check one declared collection: size, entry shape, vocabulary, duplicates.

    An entry is checked against the closed vocabulary when one is given and
    against the identifier shape otherwise -- never both, because the closed
    vocabularies are uppercase and would fail the identifier shape.
    """
    if required and not values:
        raise ExecutionContractError(
            "empty_collection", f"{field_name} must declare at least one entry"
        )
    if len(values) > _MAX_COLLECTION_LENGTH:
        raise ExecutionContractError(
            "collection_too_large", f"{field_name} exceeds its bound"
        )
    for value in values:
        if allowed is None:
            require_identifier(f"{field_name} entry", value)
        else:
            require_vocabulary(f"{field_name} entry", value, allowed)
    if len(set(values)) != len(values):
        raise ExecutionContractError(
            "duplicate_entry", f"{field_name} declares the same entry twice"
        )
    return values


def canonical_hash(value: object) -> str:
    """Return ``sha256:<hex>`` over the RFC 8785 canonical bytes of ``value``.

    The canonicalization is the public contract function, not a local
    ``sort_keys`` approximation: a digest two implementations must agree on has
    to be taken over the bytes the contract defines.
    """
    try:
        payload = canonical_bytes(value)
    except ContractSemanticError as error:
        raise ExecutionContractError(
            "not_canonicalizable", "value has no canonical JSON form"
        ) from error
    return f"sha256:{sha256(payload).hexdigest()}"


def derive_id(*parts: str) -> str:
    """Return a deterministic content-addressed identity over ``parts``."""
    return f"sha256:{sha256(chr(31).join(parts).encode('utf-8')).hexdigest()}"


class ContentAddressed:
    """Content-addressing shared by the three descriptors.

    One implementation rather than three, because the preimage rule -- *every
    field except the hash itself* -- is the part two implementations have to
    agree on, and three copies of it is three chances to disagree.
    """

    __slots__ = ()

    content_hash: str

    def canonical_preimage(self) -> dict[str, object]:
        """Return the hashed view of this descriptor: every field but the hash.

        Derived from the dataclass fields rather than transcribed, so a field
        added later is hashed by construction instead of silently excluded.
        """
        return {
            entry.name: getattr(self, entry.name)
            for entry in fields(self)  # type: ignore[arg-type]
            if entry.name != "content_hash"
        }

    def computed_content_hash(self) -> str:
        """Return the deterministic hash of this descriptor's canonical preimage."""
        return canonical_hash(self.canonical_preimage())

    def sealed(self) -> Self:
        """Return an equal descriptor stamped with its own content hash."""
        return replace(self, content_hash=self.computed_content_hash())  # type: ignore[type-var]

    @property
    def is_sealed(self) -> bool:
        return self.content_hash != _UNSEALED_CONTENT_HASH

    def verify_content_hash(self) -> None:
        """Raise unless this descriptor is sealed with its own canonical hash."""
        if not self.is_sealed:
            raise ExecutionContractError(
                "unsealed_descriptor", "descriptor carries no content hash"
            )
        if self.content_hash != self.computed_content_hash():
            raise ExecutionContractError(
                "content_hash_mismatch",
                "descriptor content hash does not match its canonical preimage",
            )


@dataclass(frozen=True, slots=True)
class ExecutionLineage:
    """The exact workspace/run/step/attempt an execution belongs to."""

    workspace_id: str
    run_id: str
    run_step_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        for name in ("workspace_id", "run_id", "run_step_id", "attempt_id"):
            require_identifier(f"lineage field {name!r}", getattr(self, name))

    @property
    def key(self) -> str:
        """A deterministic identity for this exact lineage tuple."""
        return derive_id(
            "lineage",
            self.workspace_id,
            self.run_id,
            self.run_step_id,
            self.attempt_id,
        )


@dataclass(frozen=True, slots=True)
class RuntimeProfileDescriptor(ContentAddressed):
    """A frozen, versioned, non-authoritative description of one execution plane.

    Construct it directly to get an *unsealed* descriptor, then call
    :meth:`sealed` for the copy carrying its own content hash. Registration and
    routing only accept a sealed descriptor whose hash verifies, so a hand-edited
    field cannot ride along under an identity it no longer matches.
    """

    profile_id: str
    version: str
    execution_classes: tuple[str, ...]
    worker_routes: tuple[str, ...]
    capability_sets: tuple[str, ...]
    minimum_isolation: int
    trust_state: str
    session_providers: tuple[str, ...] = ()
    policy_defaults_ref: str | None = None
    evidence_rules_ref: str | None = None
    completion_rule_ref: str | None = None
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("profile_id", self.profile_id)
        require_version("version", self.version)
        require_collection(
            "execution_classes",
            self.execution_classes,
            required=True,
            allowed=EXECUTION_CLASSES,
        )
        require_collection("worker_routes", self.worker_routes, required=True)
        require_collection("capability_sets", self.capability_sets, required=True)
        require_collection("session_providers", self.session_providers, required=False)
        require_isolation("minimum_isolation", self.minimum_isolation)
        require_vocabulary("trust_state", self.trust_state, PROFILE_TRUST_STATES)
        require_digest("content_hash", self.content_hash)
        for name in (
            "policy_defaults_ref",
            "evidence_rules_ref",
            "completion_rule_ref",
        ):
            value: str | None = getattr(self, name)
            if value is not None:
                require_digest(name, value)
        self._validate_posture()

    def _validate_posture(self) -> None:
        """Refuse the trust postures this seam will not describe as coherent."""
        agent = EXECUTION_CLASS_AGENT in self.execution_classes
        if agent != bool(self.session_providers):
            raise ExecutionContractError(
                "incoherent_posture",
                "session providers and the AGENT execution class must be "
                "declared together",
            )
        if EXECUTION_CLASS_EFFECT in self.execution_classes:
            if self.policy_defaults_ref is None:
                raise ExecutionContractError(
                    "incoherent_posture",
                    "an EFFECT profile must reference policy defaults",
                )
            if self.trust_state == PROFILE_TRUST_DRAFT:
                raise ExecutionContractError(
                    "incoherent_posture",
                    "a DRAFT profile must not declare the EFFECT execution class",
                )
        if (
            self.minimum_isolation == ISOLATION_MIN
            and self.trust_state == PROFILE_TRUST_DRAFT
        ):
            raise ExecutionContractError(
                "incoherent_posture",
                "a DRAFT profile must not declare in-process isolation",
            )


@dataclass(frozen=True, slots=True)
class ExecutorDescriptor(ContentAddressed):
    """A frozen description of one concrete executor build.

    The build hash is the load-bearing field: trust is granted against a build,
    so an entry whose ``build_hash`` moved is a different build wearing the same
    name, and the registry treats it as one.

    ``removal_instructions_ref`` is required rather than optional. A build that
    can be installed and cannot say how it is removed is the one an operator
    cannot get rid of, and "we will document removal later" is how that state is
    reached, so the field has no default to leave unset.
    """

    source_id: str
    executor_id: str
    version: str
    build_hash: str
    executor_kind: str
    capabilities: tuple[str, ...]
    supported_contract_versions: tuple[str, ...]
    required_isolation: int
    trust_state: str
    reconciliation_capabilities: tuple[str, ...]
    removal_instructions_ref: str
    supply_chain_ref: str | None = None
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("source_id", self.source_id)
        require_identifier("executor_id", self.executor_id)
        require_version("version", self.version)
        require_digest("build_hash", self.build_hash)
        require_vocabulary("executor_kind", self.executor_kind, EXECUTOR_KINDS)
        require_collection("capabilities", self.capabilities, required=True)
        require_collection(
            "supported_contract_versions",
            self.supported_contract_versions,
            required=True,
        )
        for supported in self.supported_contract_versions:
            require_version("supported_contract_versions entry", supported)
        require_isolation("required_isolation", self.required_isolation)
        require_vocabulary("trust_state", self.trust_state, BUILD_TRUST_STATES)
        require_collection(
            "reconciliation_capabilities",
            self.reconciliation_capabilities,
            required=False,
        )
        require_digest("removal_instructions_ref", self.removal_instructions_ref)
        if self.supply_chain_ref is not None:
            require_digest("supply_chain_ref", self.supply_chain_ref)
        require_digest("content_hash", self.content_hash)

    @property
    def key(self) -> tuple[str, str, str]:
        """The source-qualified exact-version key this build registers under."""
        return (self.source_id, self.executor_id, self.version)


@dataclass(frozen=True, slots=True)
class SessionProviderDescriptor(ContentAddressed):
    """A frozen description of one concrete stateful session provider build.

    Suspend/resume and control transfer are two declared booleans rather than
    entries in an open feature list: they are the two lifecycle operations
    :class:`~omnivia_core_runtime.execution.planes.StatefulSessionPlane` gates
    on, and a build either implements them or does not.
    """

    source_id: str
    provider_id: str
    version: str
    build_hash: str
    supported_kinds: tuple[str, ...]
    supports_suspend_resume: bool
    supports_control_transfer: bool
    required_isolation: int
    trust_state: str
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("source_id", self.source_id)
        require_identifier("provider_id", self.provider_id)
        require_version("version", self.version)
        require_digest("build_hash", self.build_hash)
        require_collection(
            "supported_kinds",
            self.supported_kinds,
            required=True,
            allowed=SESSION_KINDS,
        )
        require_isolation("required_isolation", self.required_isolation)
        require_vocabulary("trust_state", self.trust_state, BUILD_TRUST_STATES)
        require_digest("content_hash", self.content_hash)

    @property
    def key(self) -> tuple[str, str, str]:
        """The source-qualified exact-version key this build registers under."""
        return (self.source_id, self.provider_id, self.version)


__all__ = [
    "BUILD_TRUST_APPROVED",
    "BUILD_TRUST_DISABLED",
    "BUILD_TRUST_PROTOTYPE",
    "BUILD_TRUST_QUARANTINED",
    "BUILD_TRUST_STATES",
    "CONTRACT_VERSION",
    "EXECUTION_CLASSES",
    "EXECUTION_CLASS_AGENT",
    "EXECUTION_CLASS_DETERMINISTIC",
    "EXECUTION_CLASS_EFFECT",
    "EXECUTION_CLASS_WAIT",
    "EXECUTOR_KINDS",
    "EXECUTOR_KIND_BROWSER",
    "EXECUTOR_KIND_COMMAND",
    "EXECUTOR_KIND_FILESYSTEM",
    "EXECUTOR_KIND_GIT",
    "EXECUTOR_KIND_INTEGRATION",
    "EXECUTOR_KIND_OTHER",
    "HEALTH_AUTHENTICATED",
    "HEALTH_COMPATIBLE",
    "HEALTH_CONFIGURED",
    "HEALTH_QUARANTINED",
    "HEALTH_READY",
    "HEALTH_RECOVERABLE",
    "HEALTH_STATES",
    "ISOLATION_MAX",
    "ISOLATION_MIN",
    "PROFILE_TRUST_APPROVED",
    "PROFILE_TRUST_DISABLED",
    "PROFILE_TRUST_DRAFT",
    "PROFILE_TRUST_QUARANTINED",
    "PROFILE_TRUST_STATES",
    "ROUTABLE_HEALTH",
    "SESSION_KINDS",
    "SESSION_KIND_ACP",
    "SESSION_KIND_BROWSER",
    "SESSION_KIND_OTHER",
    "SESSION_KIND_REMOTE_EXECUTION",
    "SESSION_KIND_TERMINAL",
    "ContentAddressed",
    "ExecutionContractError",
    "ExecutionError",
    "ExecutionLineage",
    "ExecutionRefused",
    "ExecutorDescriptor",
    "RuntimeProfileDescriptor",
    "SessionProviderDescriptor",
    "canonical_hash",
    "derive_id",
    "is_digest",
    "require_collection",
    "require_digest",
    "require_identifier",
    "require_isolation",
    "require_version",
    "require_vocabulary",
]
