"""The first-party deterministic fake connector (`CON-R25`, `CON-C050`).

Dependency-free: standard library, this package's contract, and a scripted
source state the caller supplies. No network, no credential resolution in its
default posture, no workspace path, and no instance state that resume depends
on -- position comes out of the cursor payload and nowhere else, so two runs
handed the same script and the same cursor produce byte-identical observations
and byte-identical successor cursors.

The misbehaviours are configuration, not accident. A conformance corpus that
only ever drove a correct connector could not tell a check that works from a
check that is never reached, so this fake can be told to omit a window's
observations while still advancing its cursor (`CON-C058`), to break each of
the migration obligations one at a time (`CON-C052`, `CON-C053`), to declare a
cursor state version it cannot migrate (`CON-C008`), and to place resolved
credential material in a cursor payload verbatim or transformed (`CON-C030`).
Each knob exists because a corpus case demands the behaviour it produces.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Final

from omnivia_core.connector.host import canonical_cursor_digest
from omnivia_core.connector.models import HealthState, SourceHealth
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
    Batch,
    ConnectorDescriptor,
    ConnectorRefused,
    CursorBinding,
    CursorState,
    DeletionSignal,
    HealthStatus,
    IdentityStability,
    ItemFailure,
    Observation,
    PollContext,
    PollLimits,
    SpiVersion,
)

#: The limits the fake declares at discovery. They are the corpus's own numbers,
#: so a case carrying `input.limits` and a case that does not are driven against
#: the same ceilings.
FAKE_LIMITS: Final[PollLimits] = PollLimits(
    max_batch_items=100,
    max_item_metadata_bytes=65536,
    max_run_bytes=1073741824,
    poll_deadline_ms=30000,
)


def _payload(window_index: int) -> bytes:
    """The fake's whole cursor language: which window comes next."""
    return base64.urlsafe_b64encode(f"window-{window_index:04d}".encode()).rstrip(b"=")


def _in_length_group(payload: bytes) -> bytes:
    """Pad with alphabet characters until the length could decode.

    A payload of length 4n+1 is refused for its *encoding* before any other
    check runs, so a leak vector that happened to land on one would be caught
    for the wrong reason and would prove nothing about the comparison it exists
    to exercise.
    """
    return payload + b"A" * (1 if len(payload) % 4 == 1 else 0)


def _window_index(payload: bytes) -> int:
    """Read a position back out of a payload this fake wrote.

    A payload it did not write is a resynchronization, not a guess: the fake
    reports position zero rather than inventing a meaning for bytes whose author
    it cannot identify.
    """
    padded = payload + b"=" * (-len(payload) % 4)
    decoded = b""
    try:
        decoded = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return 0
    text = decoded.decode("ascii", errors="replace")
    if not text.startswith("window-") or not text[7:].isdigit():
        return 0
    return int(text[7:])


def synthetic_observations(
    prefix: str,
    count: int,
    *,
    start: int = 0,
    first_observed_at_us: int = 1785000000000000,
) -> tuple[Observation, ...]:
    """`count` observations derived entirely from their own index.

    Derived rather than stored so that a scenario of any size stays inert and
    byte-identical between runs: `CON-C058` needs 91 of these and there is no
    reason to carry 91 literals to get them. `start` offsets the identities, so
    two windows of one script describe different items rather than the same
    items twice.
    """
    observations: list[Observation] = []
    for offset in range(count):
        index = start + offset
        digit = format(index % 16, "x")
        observations.append(
            Observation(
                source_native_id=f"{prefix}-{index:04d}",
                source_locator=f"fake://scope-a/{prefix}-{index:04d}",
                observed_at_us=first_observed_at_us + index * 100000,
                metadata_bytes=128,
                content_checksum="sha256:" + digit * 64,
                media_type="text/plain",
                source_version="v1",
                permission_labels=("workspace.member",),
                deletion_signal=DeletionSignal.NONE,
            )
        )
    return tuple(observations)


@dataclass(frozen=True, slots=True)
class SourceWindow:
    """One poll's worth of scripted source state, and the witness it ends at."""

    witness_seq: int
    observations: tuple[Observation, ...] = ()
    item_failures: tuple[ItemFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceScript:
    """The scripted source. Fixed independently of the connector under test.

    That independence is the only reason omission is detectable at all: the kit
    compares what a poll reported against what this script says was there. No
    equivalent account exists for a real source, which is exactly why
    `CON-R35` is a connector obligation rather than a host check.
    """

    windows: tuple[SourceWindow, ...] = ()
    genesis_witness_seq: int = 0

    def expected_observations(self, *, from_window: int = 0) -> tuple[Observation, ...]:
        """Every observation the script holds from `from_window` onward."""
        return tuple(
            observation
            for window in self.windows[from_window:]
            for observation in window.observations
        )


@dataclass(frozen=True, slots=True)
class FakeSourceConnector:
    """A deterministic `SourceConnectorSpi` over a scripted source."""

    script: SourceScript
    connector_id: str = "fake.recordset"
    connector_version: str = "0.1.0"
    spi_version: SpiVersion = field(
        default_factory=lambda: SpiVersion(major=1, minor=0)
    )
    identity_stability: IdentityStability = IdentityStability.SOURCE_NATIVE
    state_version: int = 1
    supported_state_versions: tuple[int, ...] = (1, 2)
    enumerable_scopes: tuple[str, ...] = ("source.scope_a",)
    required_capabilities: tuple[str, ...] = ()
    declared_limitations: tuple[str, ...] = ()
    scheduling_declaration: str | None = None
    health: HealthStatus = field(
        default_factory=lambda: SourceHealth(state=HealthState.HEALTHY, detail="")
    )
    #: Windows whose observations this connector does not report while still
    #: advancing its cursor across them. The `CON-C058` defect, named.
    omitted_windows: frozenset[int] = frozenset()
    #: One of `None`, `"witness"`, `"predecessor"`, `"unmigratable"`,
    #: `"nondeterministic"`.
    migration_defect: str | None = None
    #: One of `None`, `"verbatim"`, `"transformed"`.
    secret_leak: str | None = None
    #: The only mutable state on this connector, and it is unreachable unless
    #: `migration_defect == "nondeterministic"`. A migration that depends on how
    #: many times it has been called is precisely the impurity the repetition
    #: check exists to catch, and it cannot be modelled with a clock or a random
    #: source without making the fake itself non-replayable.
    _migration_calls: list[int] = field(
        default_factory=list, repr=False, compare=False
    )

    # --- discovery ---------------------------------------------------------

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            spi_version=self.spi_version,
            identity_stability=self.identity_stability,
            declared_limits=FAKE_LIMITS,
            enumerable_scopes=self.enumerable_scopes,
            required_capabilities=self.required_capabilities,
            supported_state_versions=self.supported_state_versions,
            declared_limitations=self.declared_limitations,
            scheduling_declaration=self.scheduling_declaration,
        )

    # --- migration ---------------------------------------------------------

    def migrate_cursor(self, cursor: CursorState) -> CursorState:
        if self.migration_defect == "unmigratable" or (
            cursor.state_version not in self.supported_state_versions
        ):
            raise ConnectorRefused(
                ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
                f"state version {cursor.state_version} is not supported",
            )
        target = max(self.supported_state_versions)
        if target <= cursor.state_version:
            raise ConnectorRefused(
                ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
                f"state version {cursor.state_version} has no forward target",
            )
        migrated = replace(
            cursor,
            state_version=target,
            payload=_payload(_window_index(cursor.payload)),
        )
        if self.migration_defect == "witness":
            migrated = replace(migrated, witness_seq=cursor.witness_seq + 5)
        elif self.migration_defect == "predecessor":
            migrated = replace(migrated, predecessor_digest=b"\xbb" * 32)
        elif self.migration_defect == "nondeterministic":
            self._migration_calls.append(1)
            migrated = replace(migrated, payload=_payload(len(self._migration_calls)))
        return migrated

    # --- health ------------------------------------------------------------

    def probe(self, ctx: PollContext) -> HealthStatus:
        del ctx  # A probe reads no context: it mutates nothing and advances nothing.
        return self.health

    # --- polling -----------------------------------------------------------

    def poll(self, ctx: PollContext, cursor: CursorState | None) -> Iterator[Batch]:
        binding = CursorBinding(
            workspace_id=ctx.workspace_id, connector_id=self.connector_id
        )
        position = 0 if cursor is None else _window_index(cursor.payload)
        parent = (
            cursor
            if cursor is not None
            else CursorState(
                state_version=self.state_version,
                payload=_payload(0),
                witness_seq=self.script.genesis_witness_seq,
            )
        )
        for index in range(position, len(self.script.windows)):
            if ctx.cancellation():
                return
            window = self.script.windows[index]
            observations = (
                () if index in self.omitted_windows else window.observations
            )
            successor = CursorState(
                state_version=self.state_version,
                payload=self._successor_payload(ctx, index + 1),
                witness_seq=window.witness_seq,
                predecessor_digest=canonical_cursor_digest(binding, parent),
            )
            yield Batch(
                observations=observations[: ctx.limits.max_batch_items],
                successor_cursor=successor,
                item_failures=window.item_failures,
            )
            parent = successor

    def _successor_payload(self, ctx: PollContext, next_index: int) -> bytes:
        if self.secret_leak is None:
            return _payload(next_index)
        material = ctx.resolve_credential(ctx.credential_handle)
        # Sound only because the fixture material is itself base64url-safe: a
        # payload that were not would be refused for its encoding before the
        # comparison ever ran, which would test the wrong thing.
        if self.secret_leak == "verbatim":
            return _in_length_group(_payload(next_index) + material)
        return _in_length_group(_payload(next_index) + bytes(reversed(material)))


__all__ = [
    "FAKE_LIMITS",
    "FakeSourceConnector",
    "SourceScript",
    "SourceWindow",
    "synthetic_observations",
]
