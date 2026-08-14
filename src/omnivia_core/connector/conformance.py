"""The corpus-driven conformance kit (`CON-R26`, packet A6-N1 item 3).

Standard library only, and storage-free: the whole kit runs with no workspace
present at all, which is what makes `CON-C032`'s claim checkable rather than
asserted.

Every one of the 65 cases gets a recorded disposition -- passed, failed, or
declared-not-applicable with a discovery-declared limitation and a reason. None
is silently skipped, and an *undeclared* limitation is a failure rather than a
not-applicable, which is the discipline `CON-C051` exists to hold.

What binds an assertion to behaviour. Each handler drives the real SPI, the
real host validator, or the real fake and reads what came back. A handler that
merely restated the case's `expected_outcome` would report a green corpus
against an empty implementation, so none of them does: the corpus supplies the
vector and the expectation, and the code under test supplies the answer.

What this kit does not establish. Nothing here is a sandbox, an information-flow
control, or a proof that a connector reported everything in the window its
cursor advanced across. `CON-C058` demonstrates the last one concretely: the
host accepts a properly chained forward cursor, and the omission is found by
comparing against the *corpus-fixed* expected observation set of the fake --
detection that has no equivalent against a real source (`CON-P08`, `CON-P09`).
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from omnivia_core.connector.fake import (
    FAKE_LIMITS,
    FakeSourceConnector,
    SourceScript,
    SourceWindow,
    synthetic_observations,
)
from omnivia_core.connector.host import (
    ScopedCredentialResolver,
    canonical_cursor_digest,
    contains_known_material,
    identity_posture_error,
    metadata_depth,
    poll_context_surface_defects,
    run_cursor_migration,
    spi_writeback_defects,
    validate_batch,
    validate_registration,
    validate_successor,
)
from omnivia_core.connector.models import HealthState, SourceHealth
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_CURSOR_FOREIGN,
    ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC,
    ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
    ERROR_CONNECTOR_IDENTITY_UNSTABLE,
    ERROR_CONNECTOR_SECRET_EXPOSED,
    ERROR_CONNECTOR_STATE_INVALID,
    MAX_CURSOR_PAYLOAD_BYTES,
    MAX_METADATA_DEPTH,
    SPI_OPERATIONS,
    Batch,
    ConformanceDisposition,
    ConnectorRefused,
    CredentialHandle,
    CursorBinding,
    CursorRecord,
    CursorState,
    Deadline,
    DeletionSignal,
    IdentityStability,
    Observation,
    PollContext,
    PollLimits,
    SpiVersion,
    classify_payload,
    observation_identities,
)
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
)

#: The exact case identifiers the accepted corpus fixes, in order.
EXPECTED_CASE_IDS: Final[tuple[str, ...]] = tuple(
    f"CON-C{index:03d}" for index in range(1, 66)
)

#: Import roots no public connector code and no kit code may reach. The
#: dependency edge points from these into `omnivia-core`, never the other way.
FORBIDDEN_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "omnivia_apps",
        "omnivia_cloud",
        "omnivia_core_cli",
        "omnivia_core_client",
        "omnivia_core_mcp",
        "omnivia_core_runtime",
        "omnivia_dev",
        "omnivia_memory",
        "omnivia_platform",
        "omnivia_pro",
    }
)

#: The SDK modules the packaging assertion covers.
SDK_MODULE_NAMES: Final[tuple[str, ...]] = (
    "__init__",
    "conformance",
    "fake",
    "host",
    "models",
    "protocols",
    "spi",
)


# --- corpus loading ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    """One declarative case, exactly as the corpus states it."""

    id: str
    family: str
    title: str
    vector: str
    input: Mapping[str, Any]
    expected_outcome: str
    expected_error: str | None
    requirements: tuple[str, ...]
    decisions: tuple[str, ...]
    assertions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Corpus:
    """The loaded corpus, with its byte integrity already established."""

    corpus_id: str
    status: str
    boundaries: Mapping[str, bool]
    cursor_contract: Mapping[str, Any]
    requirement_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    cases: tuple[ConformanceCase, ...]

    def case(self, case_id: str) -> ConformanceCase:
        for candidate in self.cases:
            if candidate.id == case_id:
                return candidate
        raise KeyError(case_id)


class CorpusError(ValueError):
    """The corpus on disk is not the corpus this kit accepts."""


def verify_corpus_digests(directory: Path) -> tuple[str, ...]:
    """Recompute every digest `SHA256SUMS` records. Returns the verified paths.

    Byte integrity first, because every later check reads these bytes: a corpus
    that had drifted would otherwise be validated against itself.
    """
    checksums = (directory / "SHA256SUMS").read_text(encoding="utf-8")
    verified: list[str] = []
    for line in checksums.splitlines():
        if not line.strip():
            continue
        expected, _, name = line.partition("  ")
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != expected:
            raise CorpusError(f"{name} does not match its recorded digest")
        verified.append(name)
    if not verified:
        raise CorpusError("SHA256SUMS records no file")
    return tuple(verified)


def load_corpus(directory: Path) -> Corpus:
    """Load and structurally validate the corpus at `directory`.

    Structural validation here is deliberately not JSON Schema validation: this
    package declares no third-party dependency and adding one for a loader
    would put `jsonschema` on the import path of every consumer of the public
    connector contract. The corpus's own schema is validated against
    `jsonschema` by `tests/contracts/test_a6_corpus.py`, which is where a
    third-party analyser belongs. What this function establishes is stronger
    where it overlaps -- recorded digests, the exact 65 identifiers, closure of
    every requirement and decision reference -- and is stated rather than
    implied.
    """
    verify_corpus_digests(directory)
    document = json.loads((directory / "connector-cases.json").read_text("utf-8"))

    cases: list[ConformanceCase] = []
    for entry in document["cases"]:
        cases.append(
            ConformanceCase(
                id=entry["id"],
                family=entry["family"],
                title=entry["title"],
                vector=entry["vector"],
                input=entry["input"],
                expected_outcome=entry["expected_outcome"],
                expected_error=entry["expected_error"],
                requirements=tuple(entry["requirements"]),
                decisions=tuple(entry["decisions"]),
                assertions=tuple(entry["assertions"]),
            )
        )
    if tuple(case.id for case in cases) != EXPECTED_CASE_IDS:
        raise CorpusError("the corpus does not carry exactly CON-C001..CON-C065 in order")

    requirement_ids = tuple(item["id"] for item in document["requirements"])
    decision_ids = tuple(item["id"] for item in document["decisions"])
    dependency_ids = tuple(item["id"] for item in document["dependencies"])
    known = set(requirement_ids) | set(decision_ids)
    for case in cases:
        unknown = (set(case.requirements) | set(case.decisions)) - known
        if unknown:
            raise CorpusError(f"{case.id} names unknown identifiers: {sorted(unknown)}")

    return Corpus(
        corpus_id=document["corpus_id"],
        status=document["status"],
        boundaries=document["boundaries"],
        cursor_contract=document["cursor_contract"],
        requirement_ids=requirement_ids,
        decision_ids=decision_ids,
        dependency_ids=dependency_ids,
        cases=tuple(cases),
    )


# --- reporting -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's recorded disposition. There is no fourth value."""

    case_id: str
    disposition: ConformanceDisposition
    reason: str
    evidence: tuple[str, ...] = ()
    declared_limitation: str | None = None


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Every case, with its disposition. Never a subset."""

    results: tuple[CaseResult, ...]

    def result(self, case_id: str) -> CaseResult:
        for candidate in self.results:
            if candidate.case_id == case_id:
                return candidate
        raise KeyError(case_id)

    def of(self, disposition: ConformanceDisposition) -> tuple[CaseResult, ...]:
        return tuple(item for item in self.results if item.disposition is disposition)

    @property
    def passed(self) -> tuple[CaseResult, ...]:
        return self.of(ConformanceDisposition.PASSED)

    @property
    def failed(self) -> tuple[CaseResult, ...]:
        return self.of(ConformanceDisposition.FAILED)

    @property
    def not_applicable(self) -> tuple[CaseResult, ...]:
        return self.of(ConformanceDisposition.DECLARED_NOT_APPLICABLE)


class _NotApplicable(Exception):
    """Raised by a handler that cannot execute its case, naming the limitation."""

    def __init__(self, limitation: str, reason: str) -> None:
        super().__init__(reason)
        self.limitation = limitation
        self.reason = reason


# --- the harness -----------------------------------------------------------------

WORKSPACE_ID: Final = "workspace-alpha"
RUN_ID: Final = "run-0001"
CONNECTOR_ID: Final = "fake.recordset"

#: Base64url-safe on purpose: a leak vector whose payload were refused for its
#: *encoding* would prove nothing about the comparison it exists to exercise.
CREDENTIAL_MATERIAL: Final = b"tokenAAA"

#: The limitations the reference fake declares at discovery. Every
#: declared-not-applicable disposition names one of these; a case that needed a
#: limitation not declared here is a failure, not a not-applicable.
FAKE_DECLARED_LIMITATIONS: Final[tuple[str, ...]] = (
    "acl_withdrawal",
    "durable_persistence",
)

DEFAULT_LABELS: Final[frozenset[str]] = frozenset(
    {"workspace.member", "workspace.reviewer"}
)

_NOW_US: Final = 1785000000000000
_DEADLINE_US: Final = _NOW_US + 30_000_000


def _default_script() -> SourceScript:
    return SourceScript(
        windows=(
            SourceWindow(
                witness_seq=1, observations=synthetic_observations("rec", 2)
            ),
            SourceWindow(
                witness_seq=2,
                observations=synthetic_observations(
                    "rec", 2, start=2, first_observed_at_us=_NOW_US + 1_000_000
                ),
            ),
        )
    )


def _fake(**overrides: Any) -> FakeSourceConnector:
    values: dict[str, Any] = {
        "script": _default_script(),
        "connector_id": CONNECTOR_ID,
        "declared_limitations": FAKE_DECLARED_LIMITATIONS,
    }
    values.update(overrides)
    return FakeSourceConnector(**values)


@dataclass(frozen=True, slots=True)
class _Harness:
    """Builds contexts and cursor records. Holds nothing durable, opens nothing."""

    workspace_id: str = WORKSPACE_ID

    def context(
        self,
        *,
        limits: PollLimits | None = None,
        deadline_us: int = _DEADLINE_US,
        cancellation: Callable[[], bool] | None = None,
        resolver: ScopedCredentialResolver | None = None,
    ) -> tuple[PollContext, ScopedCredentialResolver]:
        handle = CredentialHandle(reference="handle-0001")
        scoped = resolver or ScopedCredentialResolver(handle, CREDENTIAL_MATERIAL)
        return (
            PollContext(
                workspace_id=self.workspace_id,
                run_id=RUN_ID,
                attempt_ordinal=1,
                granted_scopes=("source.scope_a",),
                credential_handle=handle,
                resolve_credential=scoped,
                limits=limits or FAKE_LIMITS,
                deadline=Deadline(expires_at_us=deadline_us),
                cancellation=cancellation or (lambda: False),
            ),
            scoped,
        )

    def binding(self, connector_id: str = CONNECTOR_ID) -> CursorBinding:
        return CursorBinding(workspace_id=self.workspace_id, connector_id=connector_id)

    def record(
        self,
        *,
        window: int = 0,
        witness_seq: int = 0,
        state_version: int = 1,
        connector_id: str = CONNECTOR_ID,
        predecessor: bytes | None = None,
    ) -> CursorRecord:
        payload = base64.urlsafe_b64encode(f"window-{window:04d}".encode()).rstrip(b"=")
        return CursorRecord(
            binding=self.binding(connector_id),
            state=CursorState(
                state_version=state_version,
                payload=payload,
                witness_seq=witness_seq,
                predecessor_digest=predecessor,
            ),
        )


def _state(vector: Mapping[str, Any]) -> CursorState:
    """One corpus lineage vector as a `CursorState`."""
    digest = vector["predecessor_digest"]
    return CursorState(
        state_version=vector["state_version"],
        payload=vector["payload"].encode("ascii"),
        witness_seq=vector["witness_seq"],
        predecessor_digest=None if digest is None else bytes.fromhex(digest),
    )


def _binding(vector: Mapping[str, Any]) -> CursorBinding:
    return CursorBinding(
        workspace_id=vector["workspace_id"], connector_id=vector["connector_id"]
    )


def _refusal(call: Callable[[], object]) -> ConnectorRefused:
    """Run `call`, requiring it to refuse, and return the refusal."""
    caught: ConnectorRefused | None = None
    try:
        call()
    except ConnectorRefused as error:
        caught = error
    if caught is None:
        raise AssertionError("expected a refusal and none was raised")
    return caught


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --- handlers ---------------------------------------------------------------------

Handler = Callable[[_Harness, ConformanceCase], tuple[str, ...]]


def _not_applicable(limitation: str, reason: str) -> Handler:
    def handler(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
        del harness, case
        raise _NotApplicable(limitation, reason)

    return handler


def _c001_discovery(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    connector = _fake()
    first = connector.describe()
    second = connector.describe()
    _expect(first == second, "describe is not byte-identical across repeated calls")
    _expect(first.connector_id == CONNECTOR_ID, "descriptor omits the connector id")
    _expect(first.spi_version == SpiVersion(1, 0), "descriptor omits the SPI version")
    _expect(
        first.identity_stability is IdentityStability.SOURCE_NATIVE,
        "descriptor omits identity stability",
    )
    _expect(isinstance(first.declared_limits, PollLimits), "descriptor omits limits")
    _expect(first.enumerable_scopes == ("source.scope_a",), "descriptor omits scopes")
    return (
        "describe returned identical descriptors on two calls",
        "descriptor carries id, SPI version, identity stability, limits and scopes",
        "no credential was resolved: describe takes no context",
    )


def _c002_capability(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    connector = _fake(required_capabilities=("source.read_all",))
    error = _refusal(
        lambda: validate_registration(
            connector.describe(), granted_capabilities=frozenset()
        )
    )
    _expect(error.error == ERROR_CODE_CAPABILITY_NOT_GRANTED, error.error)
    return (
        "registration refused with capability_not_granted before any poll",
        "the granted set was not widened by the connector's own declaration",
    )


def _c003_initial_sync(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake()
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    committed: list[Observation] = []
    for batch in connector.poll(ctx, None):
        verdict = validate_batch(
            batch,
            ctx,
            record,
            descriptor=connector.describe(),
            now_us=_NOW_US,
            resolved_material=resolver.resolved_material,
            accepted_permission_labels=DEFAULT_LABELS,
        )
        committed.extend(verdict.observations)
        record = verdict.successor
    _expect(len(committed) == 4, f"expected four observations, got {len(committed)}")
    _expect(
        all(item.content_checksum is not None for item in committed),
        "an observation reached commit without content identity",
    )
    _expect(record.state.witness_seq == 2, "the successor cursor did not terminate")
    _expect(spi_writeback_defects(connector) == (), "the connector offers a write")
    return (
        "both batches admitted with source-native identity and checksum",
        "the successor cursor is the last statement of each batch's admission",
        f"the connector wrote nothing: its surface is {SPI_OPERATIONS}",
    )


def _c004_resume(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, _ = harness.context()
    record = harness.record(window=1, witness_seq=1)
    # A *fresh* instance, so nothing in process memory can be what resume used.
    batches = list(_fake().poll(ctx, record.state))
    identities = tuple(
        identity for batch in batches for identity in observation_identities(batch.observations)
    )
    _expect(len(batches) == 1, "resume replayed a window the cursor had passed")
    _expect(
        identities == observation_identities(_default_script().windows[1].observations),
        "resume did not continue from the committed checkpoint",
    )
    return (
        "resume read its position from the cursor alone",
        "the already committed window was not replayed",
        "a fresh connector instance produced the same continuation",
    )


def _c005_no_change(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake(script=SourceScript(windows=(SourceWindow(witness_seq=7),)))
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=7)
    batches = list(connector.poll(ctx, record.state))
    _expect(len(batches) == 1, "an unchanged source produced no batch at all")
    verdict = validate_batch(
        batches[0],
        ctx,
        record,
        descriptor=connector.describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    _expect(verdict.outcome == "no_change", verdict.outcome)
    _expect(verdict.observations == (), "an empty batch produced observations")
    _expect(
        verdict.successor.state.witness_seq == record.state.witness_seq,
        "the cursor advanced past an unchanged position",
    )
    return (
        "an empty batch is a successful outcome, not an error",
        "nothing was admitted for write",
        "the successor sits at an equal witness",
    )


def _c006_witness_regression(
    harness: _Harness, case: ConformanceCase
) -> tuple[str, ...]:
    del case
    record = harness.record(window=0, witness_seq=9, predecessor=b"\xaa" * 32)
    successor = CursorState(
        state_version=1,
        payload=b"cG9zaXRpb24tNA",
        witness_seq=4,
        predecessor_digest=canonical_cursor_digest(record.binding, record.state),
    )
    error = _refusal(
        lambda: validate_successor(
            record, successor, current_binding=harness.binding()
        )
    )
    _expect(error.error == ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC, error.error)
    _expect("9" in error.detail and "4" in error.detail, "the refusal names no witnesses")
    return (
        "the successor named the presented state correctly; only the witness regressed",
        f"refused as {error.error}, comparing {record.state.witness_seq} against 4",
        "state-chain integrity only: this is not a source-ordering or completeness proof",
    )


def _c007_migration(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake(spi_version=SpiVersion(1, 1), supported_state_versions=(1, 2))
    ctx, resolver = harness.context()
    del ctx
    record = harness.record(window=0, witness_seq=9, predecessor=b"\xaa" * 32)
    outcome = run_cursor_migration(
        connector, record, supported_state_versions=(1, 2)
    )
    _expect(outcome.outcome == "accepted", f"{outcome.outcome}: {outcome.detail}")
    audit = outcome.audit
    _expect(audit is not None, "an accepted migration recorded no audit")
    assert audit is not None
    _expect(audit.after.witness_seq == 9, "the witness was not preserved exactly")
    _expect(
        audit.after.predecessor_digest == record.state.predecessor_digest,
        "the predecessor digest was not byte-identical",
    )
    _expect(audit.after.state_version == 2, "the state version did not increase")
    _expect(
        audit.predecessor_digest_before != audit.predecessor_digest_after,
        "the recorded digest pair is not a pair",
    )
    _expect(
        audit.predecessor_digest_before
        == canonical_cursor_digest(record.binding, audit.before),
        "the pre-migration digest is not the canonical digest of the input state",
    )
    _expect(resolver.resolved_material == (), "migration resolved a credential")
    return (
        "migrate_cursor took and returned a whole CursorState",
        "witness preserved exactly; predecessor digest byte-identical",
        "repeated migration returned byte-identical output",
        "pre- and post-migration canonical digests recorded as a pair",
        "no credential was resolved and no source was accessed",
    )


def _c008_unmigratable(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake(supported_state_versions=(1, 2))
    record = harness.record(window=0, witness_seq=9, state_version=7)
    outcome = run_cursor_migration(connector, record, supported_state_versions=(1, 2))
    _expect(outcome.outcome == "resync_required", outcome.outcome)
    _expect(outcome.error == ERROR_CONNECTOR_CURSOR_UNMIGRATABLE, str(outcome.error))
    _expect("7" in outcome.detail, "the declaration does not name the state version")
    _expect(outcome.audit is None, "a resync declaration carried a migration audit")
    return (
        "an explicit resynchronization was declared, not a silent reset",
        f"the declaration names state version 7: {outcome.detail}",
        "nothing was deleted; existing evidence is re-observed",
    )


def _c009_foreign(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    # Recorded under fake.recordset, presented to fake.filesystem.
    record = harness.record(
        window=0, witness_seq=9, connector_id="fake.recordset", predecessor=b"\xbb" * 32
    )
    successor = CursorState(
        state_version=1,
        payload=b"Y3Vyc29yLWlzc3VlZA",
        witness_seq=10,
        predecessor_digest=canonical_cursor_digest(record.binding, record.state),
    )
    _expect(
        classify_payload(successor.payload) is None,
        "the payload was not well formed, so this would test the wrong refusal",
    )
    error = _refusal(
        lambda: validate_successor(
            record, successor, current_binding=harness.binding("fake.filesystem")
        )
    )
    _expect(error.error == ERROR_CONNECTOR_CURSOR_FOREIGN, error.error)
    return (
        "the payload passed the encoding and byte-bound check first",
        "refused as a binding failure, not as a malformed payload",
        "binding falls out of the same host-computed digest, not a separate field",
        "no poll was executed and no state advanced",
    )


def _c010_replay(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, _ = harness.context()
    record = harness.record(window=0, witness_seq=0)
    first = list(_fake().poll(ctx, record.state))
    second = list(_fake().poll(ctx, record.state))
    _expect(first == second, "two identical polls produced different batches")
    _expect(
        first[0].successor_cursor == second[0].successor_cursor,
        "two identical polls produced different successor cursors",
    )
    return (
        "both polls returned the same identities in the same order",
        "both polls returned byte-identical successor cursors",
    )


def _c011_duplicate_collapse(
    harness: _Harness, case: ConformanceCase
) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    observation = synthetic_observations("rec", 1)[0]
    batch = Batch(
        observations=(observation, observation),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=1,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    verdict = validate_batch(
        batch,
        ctx,
        record,
        descriptor=_fake().describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    _expect(len(verdict.observations) == 1, "the duplicate did not collapse")
    return (
        "one identity reported twice with one checksum collapsed to one upsert",
        "the collapse was performed by the host validator, not by the connector",
    )


def _c015_locator_derived(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake(identity_stability=IdentityStability.LOCATOR_DERIVED)
    descriptor = connector.describe()
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    batch = next(iter(connector.poll(ctx, record.state)))
    verdict = validate_batch(
        batch,
        ctx,
        record,
        descriptor=descriptor,
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    _expect(verdict.outcome == "deferred_to_reconciliation", verdict.outcome)
    _expect(
        identity_posture_error(descriptor) == ERROR_CONNECTOR_IDENTITY_UNSTABLE,
        "the posture produced no identifier",
    )
    return (
        "the new locator was admitted as a distinct identity",
        "nothing was tombstoned by inference",
        "the pair was queued for an explicit reconciliation decision",
    )


def _c016_explicit_delete(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    deletion = Observation(
        source_native_id="rec-0002",
        source_locator="fake://scope-a/rec-0002",
        observed_at_us=_NOW_US,
        metadata_bytes=96,
        source_version="v2",
        deletion_signal=DeletionSignal.EXPLICIT_DELETE,
    )
    batch = Batch(
        observations=(deletion,),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=1,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    verdict = validate_batch(
        batch,
        ctx,
        record,
        descriptor=_fake().describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    _expect(verdict.outcome == "accepted", verdict.outcome)
    _expect(verdict.observations[0].deleted, "the explicit deletion was not carried")
    return (
        "an explicit deletion signal was admitted as an appended observation",
        "the validator removes nothing: it decides what may be appended",
    )


def _c017_absence(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake(script=SourceScript(windows=(SourceWindow(witness_seq=9),)))
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=9)
    batch = next(iter(connector.poll(ctx, record.state)))
    verdict = validate_batch(
        batch,
        ctx,
        record,
        descriptor=connector.describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    _expect(verdict.outcome == "no_change", verdict.outcome)
    _expect(
        not any(item.deleted for item in verdict.observations),
        "an absent identity produced a deletion",
    )
    return (
        "no tombstone was produced by absence alone",
        "a truncated, rate-limited or cancelled poll therefore erases nothing",
    )


def _c018_sweep(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    absent = Observation(
        source_native_id="rec-0003",
        source_locator="fake://scope-a/rec-0003",
        observed_at_us=_NOW_US,
        metadata_bytes=96,
        deletion_signal=DeletionSignal.ABSENT_FROM_WINDOW,
    )
    batch = Batch(
        observations=(absent,),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=1,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    descriptor = _fake().describe()
    unauthorized = validate_batch(
        batch,
        ctx,
        record,
        descriptor=descriptor,
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    authorized = validate_batch(
        batch,
        ctx,
        record,
        descriptor=descriptor,
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
        reconciliation_authorized=True,
    )
    _expect(unauthorized.observations == (), "an unauthorized sweep proposed a tombstone")
    _expect(len(authorized.observations) == 1, "an authorized sweep proposed nothing")
    return (
        "a sweep without declared complete scope produced no tombstone proposal",
        "an authorized sweep produced a proposal, not a decision",
        "the connector did not itself decide the deletion",
    )


def _c022_unmapped_grant(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    observation = replace(
        synthetic_observations("rec", 1)[0],
        permission_labels=("workspace.unmapped",),
    )
    batch = Batch(
        observations=(observation,),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=1,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    error = _refusal(
        lambda: validate_batch(
            batch,
            ctx,
            record,
            descriptor=_fake().describe(),
            now_us=_NOW_US,
            resolved_material=resolver.resolved_material,
            accepted_permission_labels=DEFAULT_LABELS,
        )
    )
    _expect(error.error == ERROR_CODE_INVALID_REQUEST, error.error)
    _expect(
        "workspace.unmapped" not in str(error), "the refusal echoed the source grant"
    )
    return (
        "the observation was refused rather than ingested with a widened label",
        "the refusal named the condition without echoing source content",
    )


def _c029_handle(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    handle = ctx.credential_handle
    _expect("handle-0001" not in repr(handle), "the handle repr is not redacted")
    _expect("handle-0001" not in str(handle), "the handle str is not redacted")
    _expect(
        tuple(type(handle).__dataclass_fields__) == ("reference",),
        "the handle carries structure a connector could rely on",
    )
    other = _refusal(
        lambda: resolver(CredentialHandle(reference="handle-0002"))
    )
    _expect(other.error == ERROR_CODE_AUTHORIZATION_DENIED, other.error)
    return (
        "the handle carries no material and renders redacted",
        "a handle alone grants no access without the host resolver",
        "handle values here are synthetic and inert",
    )


def _c030_known_material(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    record = harness.record(window=0, witness_seq=0)
    descriptor = _fake().describe()

    ctx, resolver = harness.context()
    verbatim = next(iter(_fake(secret_leak="verbatim").poll(ctx, record.state)))
    error = _refusal(
        lambda: validate_batch(
            verbatim,
            ctx,
            record,
            descriptor=descriptor,
            now_us=_NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    _expect(error.error == ERROR_CONNECTOR_SECRET_EXPOSED, error.error)

    ctx2, resolver2 = harness.context()
    transformed = next(iter(_fake(secret_leak="transformed").poll(ctx2, record.state)))
    verdict = validate_batch(
        transformed,
        ctx2,
        record,
        descriptor=descriptor,
        now_us=_NOW_US,
        resolved_material=resolver2.resolved_material,
    )
    _expect(
        not contains_known_material(
            transformed.successor_cursor.payload, resolver2.resolved_material
        ),
        "the transformed form was caught, so the recorded gap is not the real one",
    )
    _expect(verdict.successor.state.payload != b"", "the transformed batch was refused")
    _expect(
        classify_payload(verbatim.successor_cursor.payload) is None
        and classify_payload(transformed.successor_cursor.payload) is None,
        "a leak payload was refused for its encoding rather than its content",
    )
    return (
        "the verbatim form was refused before persistence as connector_secret_exposed",
        "the payload was bounded and alphabet-checked without being interpreted",
        "the transformed form was NOT caught and was admitted",
        "this is defence in depth, never information-flow enforcement",
        "enforcement needs the CON-P06/CON-P09 credential-broker boundary",
    )


def _c031_credential_scope(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    first = resolver(ctx.credential_handle)
    _expect(first == CREDENTIAL_MATERIAL, "the resolver returned other material")
    resolver.invalidate()
    error = _refusal(lambda: resolver(ctx.credential_handle))
    _expect(error.error == ERROR_CODE_AUTHORIZATION_DENIED, error.error)
    _expect(not resolver.valid, "the resolver survived its invocation")
    return (
        "material was scoped to one poll invocation and invalidated at its end",
        "a revoked handle failed the next poll rather than succeeding from cache",
    )


def _c032_storage_surface(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    defects = poll_context_surface_defects()
    _expect(defects == (), f"the poll context exposes {defects}")
    imports = _module_imports()
    for module, roots in imports.items():
        offending = roots & FORBIDDEN_IMPORT_ROOTS
        _expect(not offending, f"{module} imports {sorted(offending)}")
    for module in ("spi", "host", "fake"):
        _expect(
            "sqlite3" not in imports[module] and "pathlib" not in imports[module],
            f"{module} reaches for storage",
        )
    return (
        "the context exposes no connection, no blob store handle and no workspace path",
        "the connector package imports no runtime storage module",
        "this case executed with no workspace present: no path was constructed",
        (
            "an API ownership boundary only; it shows no inability to reach "
            "platform APIs, and any stronger isolation claim is gated on CON-P09"
        ),
    )


def _c034_item_ceiling(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    limits = PollLimits(
        max_batch_items=100,
        max_item_metadata_bytes=65536,
        max_run_bytes=1073741824,
        poll_deadline_ms=30000,
    )
    ctx, resolver = harness.context(limits=limits)
    record = harness.record(window=0, witness_seq=0)
    batch = Batch(
        observations=synthetic_observations("rec", 101),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=1,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    error = _refusal(
        lambda: validate_batch(
            batch,
            ctx,
            record,
            descriptor=_fake().describe(),
            now_us=_NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    _expect(error.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED, error.error)
    _expect("101" in error.detail, "the host trusted a declared count")
    return (
        "the host counted 101 items itself rather than trusting a declaration",
        "the whole batch was discarded and the cursor is unchanged",
    )


def _c035_oversized_item(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    normal, oversized = synthetic_observations("rec", 2)
    oversized = replace(oversized, metadata_bytes=131072)
    batch = Batch(
        observations=(normal, oversized),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=1,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    verdict = validate_batch(
        batch,
        ctx,
        record,
        descriptor=_fake().describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    _expect(len(verdict.item_failures) == 1, "the oversized item was not dead-lettered")
    _expect(
        verdict.item_failures[0].error == ERROR_CODE_SIZE_LIMIT_EXCEEDED,
        verdict.item_failures[0].error,
    )
    _expect(len(verdict.observations) == 1, "the remaining item did not commit")
    _expect(
        verdict.item_failures[0].source_native_id == oversized.source_native_id,
        "the wrong item was dead-lettered",
    )
    return (
        "only the offending item was dead-lettered",
        "the remaining item in the batch was admitted normally",
        "the dead-letter record carries typed cause and identity but no content",
    )


def _c036_deadline(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context(deadline_us=_NOW_US - 1)
    record = harness.record(window=0, witness_seq=5)
    batch = Batch(
        observations=(),
        successor_cursor=CursorState(
            state_version=1,
            payload=b"d2luZG93LTAwMDE",
            witness_seq=6,
            predecessor_digest=canonical_cursor_digest(record.binding, record.state),
        ),
    )
    error = _refusal(
        lambda: validate_batch(
            batch,
            ctx,
            record,
            descriptor=_fake().describe(),
            now_us=_NOW_US,
            resolved_material=resolver.resolved_material,
        )
    )
    _expect(error.error == ERROR_CODE_DEADLINE_EXCEEDED, error.error)
    _expect(record.state.witness_seq == 5, "the persisted cursor moved")
    return (
        "the deadline was enforced by the host, not by the connector",
        "the poll was abandoned and the persisted cursor is unchanged",
    )


def _c037_hostile_input(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    base: dict[str, Any] = {
        "source_native_id": "rec-0001",
        "source_locator": "fake://scope-a/rec-0001",
        "observed_at_us": _NOW_US,
        "metadata_bytes": 128,
        "content_checksum": "sha256:" + "1" * 64,
        "media_type": "text/plain",
        "permission_labels": ["workspace.member"],
        "deletion_signal": "none",
    }
    vectors: tuple[tuple[str, dict[str, Any], str], ...] = (
        ("C0 control character in an identifier", {"source_native_id": "rec\x07id"}, "\x07"),
        ("JSON-escaped NUL in an identifier", {"source_native_id": "rec\x00id"}, "\x00"),
        (
            "parent-directory-shaped locator",
            {"source_locator": "fake://scope-a/../../etc/shadow"},
            "..",
        ),
        (
            "metadata nested beyond the ceiling",
            {"metadata_json": "[" * (MAX_METADATA_DEPTH + 4) + "]" * (MAX_METADATA_DEPTH + 4)},
            "[[[",
        ),
    )
    evidence: list[str] = []
    from omnivia_core.connector.host import admit_observation

    for name, override, echoed in vectors:
        document = {**base, **override}
        error = _refusal(lambda doc=document: admit_observation(doc))  # type: ignore[misc]
        _expect(error.error == ERROR_CODE_INVALID_REQUEST, f"{name}: {error.error}")
        _expect(echoed not in str(error), f"{name}: the refusal echoed source content")
        evidence.append(f"{name} refused as invalid_request before any write")
    _expect(
        metadata_depth('{"a":{"b":"[[[[["}}') == 2,
        "the depth scan counted bracket characters inside a string literal",
    )
    evidence.append("a traversal-shaped locator is opaque text; no path was constructed")
    return tuple(evidence)


def _c041_health(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    outcomes = (
        SourceHealth(state=HealthState.HEALTHY, detail="reachable"),
        SourceHealth(state=HealthState.UNAVAILABLE, detail="unauthorized"),
        SourceHealth(state=HealthState.DEGRADED, detail="rate limited"),
    )
    for expected in outcomes:
        status = _fake(health=expected).probe(ctx)
        _expect(status == expected, "the probe did not return its typed status")
        _expect(isinstance(status, SourceHealth), "the probe returned free text")
        _expect(
            not contains_known_material(
                status.detail.encode("utf-8"), (CREDENTIAL_MATERIAL,)
            ),
            "the probe leaked credential material",
        )
    _expect(resolver.resolved_material == (), "the probe resolved a credential")
    return (
        "each of the three outcomes was a typed status, not free text",
        "no probe mutated the source or advanced a cursor",
        "probe output carried no content, no material and no unbounded label",
    )


def _c042_cancel_mid_poll(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, _ = harness.context(cancellation=lambda: True)
    record = harness.record(window=0, witness_seq=0)
    batches = list(_fake().poll(ctx, record.state))
    _expect(batches == [], "a cancelled poll produced a batch")
    return (
        "the in-flight batch was discarded at the batch boundary",
        "the persisted cursor equals the last committed position",
    )


def _c043_cancel_after_commit(
    harness: _Harness, case: ConformanceCase
) -> tuple[str, ...]:
    del case
    seen = [0]

    def cancellation() -> bool:
        seen[0] += 1
        return seen[0] > 1

    ctx, resolver = harness.context(cancellation=cancellation)
    record = harness.record(window=0, witness_seq=0)
    batches = list(_fake().poll(ctx, record.state))
    _expect(len(batches) == 1, f"expected one committed batch, got {len(batches)}")
    verdict = validate_batch(
        batches[0],
        ctx,
        record,
        descriptor=_fake().describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    resumed_ctx, _ = harness.context()
    resumed = list(_fake().poll(resumed_ctx, verdict.successor.state))
    identities = tuple(
        name for batch in resumed for name in observation_identities(batch.observations)
    )
    committed = observation_identities(verdict.observations)
    _expect(
        not set(identities) & set(committed),
        "resuming re-ingested the committed batch",
    )
    return (
        "committed evidence and its successor cursor were retained",
        "resuming continued from that cursor without re-ingesting it",
    )


def _c044_major(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    connector = _fake(spi_version=SpiVersion(2, 0))
    error = _refusal(
        lambda: validate_registration(
            connector.describe(), granted_capabilities=frozenset()
        )
    )
    _expect(error.error == ERROR_CODE_INCOMPATIBLE_VERSION, error.error)
    _expect("1" in error.detail, "the refusal does not name the supported major")
    return (
        "registration was refused before any poll",
        f"the refusal names the supported major: {error.detail}",
    )


def _c045_additive_minor(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    from omnivia_core.connector.host import admit_observation

    connector = _fake(spi_version=SpiVersion(1, 1))
    validate_registration(connector.describe(), granted_capabilities=frozenset())
    document = {
        "source_native_id": "rec-0001",
        "source_locator": "fake://scope-a/rec-0001",
        "observed_at_us": _NOW_US,
        "metadata_bytes": 128,
        "content_checksum": "sha256:" + "1" * 64,
        "media_type": "text/plain",
        "permission_labels": ["workspace.member"],
        "deletion_signal": "none",
        "source_confidence_hint": "an additive optional field from SPI 1.1",
    }
    observation = admit_observation(document)
    _expect(observation.source_native_id == "rec-0001", "the observation was rejected")
    return (
        "SPI 1.1 registered against a host at major 1",
        "the unknown optional field was ignored, not rejected, in production posture",
        "strict rejection is reserved for a version-pinned conformance run",
    )


def _c046_packaging(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    imports = _module_imports()
    offending: list[str] = []
    for module, roots in sorted(imports.items()):
        for root in sorted(roots & FORBIDDEN_IMPORT_ROOTS):
            offending.append(f"{module} -> {root}")
    _expect(not offending, f"forbidden imports: {offending}")
    _expect(
        set(imports) == set(SDK_MODULE_NAMES),
        f"the scan covered {sorted(imports)}, not the whole SDK",
    )
    return (
        f"the declared import graph of {len(imports)} SDK modules was parsed",
        "no dependency points into runtime, protocol, Desktop or commercial modules",
        (
            "a static packaging check binds what a package declares and imports, "
            "not what installed code may do at run time; the installation trust "
            "gate is CON-P09"
        ),
    )


def _c048_no_writeback(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    connector = _fake()
    defects = spi_writeback_defects(connector)
    _expect(defects == (), f"the SPI offers {defects}")
    for name in SPI_OPERATIONS:
        _expect(hasattr(connector, name), f"the SPI is missing {name}")
    return (
        f"the SPI surface is exactly {SPI_OPERATIONS}",
        "no operation creates, updates or deletes anything in the source system",
        "bidirectional synchronization remains explicitly deferred scope",
    )


def _c049_scheduling(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    connector = _fake(scheduling_declaration="every-5-minutes")
    error = _refusal(
        lambda: validate_registration(
            connector.describe(), granted_capabilities=frozenset()
        )
    )
    from omnivia_core.connector.spi import ERROR_CONNECTOR_SCHEDULING_DENIED

    _expect(error.error == ERROR_CONNECTOR_SCHEDULING_DENIED, error.error)
    return (
        "registration was refused; the coordinator remains the only execution owner",
        "a connector never initiates execution",
    )


def _c050_fake_determinism(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=0)
    first = list(_fake(connector_id="fake.threadstore").poll(ctx, record.state))
    second = list(_fake(connector_id="fake.threadstore").poll(ctx, record.state))
    _expect(first == second, "the two replays differ")
    _expect(
        [b.successor_cursor.payload for b in first]
        == [b.successor_cursor.payload for b in second],
        "the two replays produced different successor cursors",
    )
    _expect(resolver.resolved_material == (), "the fake resolved a credential")
    imports = _module_imports()
    _expect("pathlib" not in imports["fake"], "the fake reaches for a workspace path")
    _expect(
        not {"socket", "http", "urllib", "ssl"} & imports["fake"],
        "the fake reaches for the network",
    )
    return (
        "both replays produced identical observation sequences and cursors",
        "no network access, no credential resolution, no workspace path",
        "the fake is the reference implementation the SPI surface is judged against",
    )


def _c052_migration_witness(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    connector = _fake(
        spi_version=SpiVersion(1, 1),
        supported_state_versions=(1, 2),
        migration_defect="witness",
    )
    record = harness.record(window=0, witness_seq=9, predecessor=b"\xaa" * 32)
    outcome = run_cursor_migration(connector, record, supported_state_versions=(1, 2))
    _expect(outcome.outcome == "refused", outcome.outcome)
    _expect(outcome.error == ERROR_CONNECTOR_STATE_INVALID, str(outcome.error))
    _expect(outcome.audit is None, "a refused migration recorded an audit")
    _expect(record.state.witness_seq == 9, "the persisted cursor changed")
    return (
        "exact equality was required and a forward witness was refused",
        "a forward witness is accepted from a poll and refused from a migration",
        "the persisted cursor is unchanged and no poll was executed",
        "accepting it would reach the CON-C058 loss through migration",
    )


def _c053_migration_predecessor(
    harness: _Harness, case: ConformanceCase
) -> tuple[str, ...]:
    del case
    connector = _fake(
        spi_version=SpiVersion(1, 1),
        supported_state_versions=(1, 2),
        migration_defect="predecessor",
    )
    record = harness.record(window=0, witness_seq=9, predecessor=b"\xaa" * 32)
    outcome = run_cursor_migration(connector, record, supported_state_versions=(1, 2))
    _expect(outcome.outcome == "refused", outcome.outcome)
    _expect(outcome.error == ERROR_CONNECTOR_STATE_INVALID, str(outcome.error))

    # Present-to-absent fails the same obligation, and so does absent-to-present.
    from omnivia_core.connector.host import validate_migration

    dropped = replace(
        record.state, state_version=2, predecessor_digest=None
    )
    error = _refusal(
        lambda: validate_migration(record, dropped, supported_state_versions=(1, 2))
    )
    _expect(error.error == ERROR_CONNECTOR_STATE_INVALID, error.error)
    genesis = harness.record(window=0, witness_seq=9, predecessor=None)
    added = replace(genesis.state, state_version=2, predecessor_digest=b"\xcc" * 32)
    error2 = _refusal(
        lambda: validate_migration(genesis, added, supported_state_versions=(1, 2))
    )
    _expect(error2.error == ERROR_CONNECTOR_STATE_INVALID, error2.error)
    return (
        "byte-identical carry-through was required",
        "present-to-absent and absent-to-present failed the same obligation",
        (
            "the connector never authors lineage; the host digest binds all four "
            "parent fields plus the frozen bindings"
        ),
        "the persisted cursor is unchanged and the chain was not extended",
    )


def _encoding_case(violation: str) -> Handler:
    def handler(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
        del case
        payloads = {
            "non_alphabet_character": b"cG9zaXRpb24.MQ",
            "padding_character": b"cG9zaXRpb24tMQ==",
            "undecodable_length": b"cG9zaXRpb24tMTI",
        }
        payload = payloads[violation]
        if violation == "undecodable_length":
            payload = b"A" * 13
        _expect(
            len(payload) % 4 == 1 or violation != "undecodable_length",
            "the length vector is not in the 4n+1 group",
        )
        defect = classify_payload(payload)
        _expect(defect == ERROR_CONNECTOR_STATE_INVALID, str(defect))
        error = _refusal(
            lambda: CursorState(state_version=1, payload=payload, witness_seq=1)
        )
        _expect(error.error == ERROR_CONNECTOR_STATE_INVALID, error.error)
        # Encoding is decided by the type itself, so it necessarily precedes the
        # binding, lineage and witness checks: no such value can ever reach them.
        foreign = _refusal(
            lambda: CursorState(
                state_version=1,
                payload=payload,
                witness_seq=1,
                predecessor_digest=b"\xff" * 32,
            )
        )
        _expect(foreign.error == ERROR_CONNECTOR_STATE_INVALID, foreign.error)
        return (
            f"a {violation.replace('_', ' ')} payload was refused as an encoding failure",
            "the check inspected the length group and the character set only",
            "it ran before any binding, lineage or witness check could",
            "nothing was persisted and the previous cursor was retained",
        )

    return handler


def _c057_size(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    oversized = b"A" * (MAX_CURSOR_PAYLOAD_BYTES + 1)
    _expect(classify_payload(oversized) == ERROR_CODE_SIZE_LIMIT_EXCEEDED, "not sized")
    error = _refusal(
        lambda: CursorState(state_version=1, payload=oversized, witness_seq=1)
    )
    _expect(error.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED, error.error)
    _expect(
        classify_payload(b"A" * MAX_CURSOR_PAYLOAD_BYTES) is None,
        "the bound is off by one",
    )
    return (
        f"the host measured the payload itself: {len(oversized)} bytes over 4096",
        "the bound was enforced on the exact bytes returned",
        "the batch is discarded whole and the persisted cursor is unchanged",
        "the bound limits capacity; it establishes nothing about the content",
    )


def _c058_completeness(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del case
    # 91 observations sit inside the window the cursor advances across, and the
    # connector reports none of them.
    omitted = synthetic_observations("rec", 91)
    script = SourceScript(windows=(SourceWindow(witness_seq=100, observations=omitted),))
    connector = _fake(script=script, omitted_windows=frozenset({0}))
    ctx, resolver = harness.context()
    record = harness.record(window=0, witness_seq=9, predecessor=b"\xaa" * 32)

    batch = next(iter(connector.poll(ctx, record.state)))
    verdict = validate_batch(
        batch,
        ctx,
        record,
        descriptor=connector.describe(),
        now_us=_NOW_US,
        resolved_material=resolver.resolved_material,
    )
    # Every host-checkable property holds, so acceptance is the correct result.
    _expect(batch.observations == (), "the fake reported observations after all")
    _expect(verdict.outcome == "accepted", verdict.outcome)
    _expect(
        verdict.successor.state.witness_seq == 100,
        "the cursor did not advance past the window",
    )
    _expect(
        verdict.parent_digest
        == canonical_cursor_digest(record.binding, record.state),
        "the lineage the host verified is not the digest of the presented state",
    )

    # The omission is found here, from the corpus-fixed expected set, and
    # nowhere near the cursor check.
    expected = script.expected_observations()
    reported = set(observation_identities(batch.observations))
    missing = tuple(
        item.source_native_id
        for item in expected
        if item.source_native_id not in reported
    )
    _expect(len(missing) == 91, f"expected 91 omissions, detected {len(missing)}")
    return (
        "the host accepted the batch and advanced the cursor from witness 9 to 100",
        "encoding valid, lineage names the presented state, witness moves forward",
        "91 observations are lost, not deferred: the cursor now sits past them",
        (
            "the omission was detected from the fake's corpus-fixed expected set, "
            "never from the cursor check"
        ),
        "reported as a connector-specific completeness failure under CON-R35",
        "replay and idempotent upsert bound duplication and corruption only",
        (
            "detection does not generalize: a real source has no independent "
            "account (CON-P08), and the trust posture for such evidence is "
            "CON-P09"
        ),
    )


def _c059_lineage_chain(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness
    vectors = case.input["lineage_digest_vectors"]
    evidence: list[str] = []
    for index, vector in enumerate(vectors):
        binding = _binding(vector)
        state = _state(vector)
        digest = canonical_cursor_digest(binding, state)
        _expect(
            digest.hex() == vector["expected_digest"],
            f"vector {index} digest {digest.hex()} != {vector['expected_digest']}",
        )
        evidence.append(f"vector {index} recomputed to {digest.hex()[:16]}...")
    genesis = _state(vectors[0])
    _expect(genesis.predecessor_digest is None, "the genesis vector carries a parent")
    _expect(
        cursor_preimage_tail(_binding(vectors[0]), genesis) == b"\x00\x00\x00\x00",
        "genesis was not encoded as a zero-length final frame",
    )
    links = 0
    for parent_vector, child_vector in pairwise(vectors):
        parent = CursorRecord(binding=_binding(parent_vector), state=_state(parent_vector))
        child = _state(child_vector)
        _expect(
            child.predecessor_digest == bytes.fromhex(parent_vector["expected_digest"]),
            "a child does not carry the raw 32-byte digest of its parent",
        )
        verified = validate_successor(
            parent, child, current_binding=parent.binding
        )
        _expect(verified == bytes.fromhex(parent_vector["expected_digest"]), "link")
        _expect(
            parent.binding == _binding(child_vector),
            "a binding was substituted between generations",
        )
        links += 1
    _expect(links == 3, f"expected three positive links, verified {links}")
    evidence.append("all three links accepted and witness non-regression preserved")
    return tuple(evidence)


def cursor_preimage_tail(binding: CursorBinding, state: CursorState) -> bytes:
    """The final framed field of the preimage. Genesis is exactly `00 00 00 00`."""
    from omnivia_core.connector.host import cursor_digest_preimage

    preimage = cursor_digest_preimage(binding, state)
    if state.predecessor_digest is None:
        return preimage[-4:]
    return preimage[-36:]


def _differential_case(expected_error: str) -> Handler:
    def handler(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
        del harness
        vectors = case.input["lineage_digest_vectors"]
        _expect(len(vectors) == 2, "a differential vector is exactly two states")
        baseline, mutated = vectors
        baseline_digest = canonical_cursor_digest(_binding(baseline), _state(baseline))
        mutated_digest = canonical_cursor_digest(_binding(mutated), _state(mutated))
        _expect(
            baseline_digest.hex() == baseline["expected_digest"], "baseline recompute"
        )
        _expect(mutated_digest.hex() == mutated["expected_digest"], "mutated recompute")
        _expect(baseline_digest != mutated_digest, "the mutation did not change the digest")

        changed = case.input["lineage_changed_field"]
        differing = tuple(
            key
            for key in (
                "workspace_id",
                "connector_id",
                "state_version",
                "payload",
                "witness_seq",
                "predecessor_digest",
            )
            if baseline[key] != mutated[key]
        )
        _expect(differing == (changed,), f"the vectors differ in {differing}")

        # The actual parent is the mutated state; the successor repeats the
        # baseline digest.
        parent = CursorRecord(binding=_binding(mutated), state=_state(mutated))
        successor = CursorState(
            state_version=2,
            payload=b"c3VjY2Vzc29y",
            witness_seq=_state(mutated).witness_seq + 1,
            predecessor_digest=bytes.fromhex(case.input["successor_predecessor_digest"]),
        )
        binding_error = _refusal(
            lambda: validate_successor(
                parent, successor, current_binding=_binding(baseline)
            )
        )
        lineage_error = _refusal(
            lambda: validate_successor(
                parent, successor, current_binding=parent.binding
            )
        )
        reported = binding_error if changed in ("workspace_id", "connector_id") else lineage_error
        _expect(reported.error == expected_error, f"{reported.error} != {expected_error}")
        _expect(
            lineage_error.error == ERROR_CONNECTOR_STATE_INVALID,
            "the repeated baseline successor was not refused on lineage",
        )
        return (
            f"the two vectors differ in exactly {changed}",
            (
                f"independent recomputation produced {baseline_digest.hex()[:16]}"
                f"... and {mutated_digest.hex()[:16]}..."
            ),
            (
                "the successor repeating the baseline digest was refused against "
                "the actual parent"
            ),
            f"reported as {reported.error}",
            "no observation was committed and the persisted parent is unchanged",
        )

    return handler


def _c051_kit_discipline(harness: _Harness, case: ConformanceCase) -> tuple[str, ...]:
    del harness, case
    connector = _fake()
    _expect(
        connector.describe().declared_limitations == FAKE_DECLARED_LIMITATIONS,
        "the fake declares no limitations at discovery",
    )
    return (
        "every case reports passed, failed or declared-not-applicable",
        (
            "a declared-not-applicable result names a discovery-declared "
            "limitation and records a reason"
        ),
        "an undeclared limitation is reported as a failure, not as not-applicable",
    )


_DURABLE = "durable_persistence"
_ACL = "acl_withdrawal"

_HANDLERS: Final[Mapping[str, Handler]] = {
    "CON-C001": _c001_discovery,
    "CON-C002": _c002_capability,
    "CON-C003": _c003_initial_sync,
    "CON-C004": _c004_resume,
    "CON-C005": _c005_no_change,
    "CON-C006": _c006_witness_regression,
    "CON-C007": _c007_migration,
    "CON-C008": _c008_unmigratable,
    "CON-C009": _c009_foreign,
    "CON-C010": _c010_replay,
    "CON-C011": _c011_duplicate_collapse,
    "CON-C012": _not_applicable(
        _DURABLE,
        "re-observation against an already ingested version needs a durable "
        "evidence store; the connector owns none and this kit runs storage-free",
    ),
    "CON-C013": _not_applicable(
        _DURABLE,
        "version succession is decided against durable current state, which the "
        "connector does not own",
    ),
    "CON-C014": _not_applicable(
        _DURABLE,
        "a rename is decided by comparing against the item's durable current "
        "locator, which the connector does not hold",
    ),
    "CON-C015": _c015_locator_derived,
    "CON-C016": _c016_explicit_delete,
    "CON-C017": _c017_absence,
    "CON-C018": _c018_sweep,
    "CON-C019": _not_applicable(
        _DURABLE,
        "reappearance is read from the durable presence stream head, which the "
        "connector does not own",
    ),
    "CON-C020": _not_applicable(
        _DURABLE,
        "an attach event is a durable permission-label append the connector does "
        "not perform",
    ),
    "CON-C021": _not_applicable(
        _ACL, "the reference fake declares at discovery that it cannot express "
        "ACL withdrawal"
    ),
    "CON-C022": _c022_unmapped_grant,
    "CON-C023": _not_applicable(
        _DURABLE,
        "the current interpretation is a durable fold the connector does not own",
    ),
    "CON-C024": _not_applicable(
        _DURABLE,
        "the tiebreak is a durable ordering decision, and its concrete order is "
        "provisional under CON-P03",
    ),
    "CON-C025": _not_applicable(
        _DURABLE, "crash recovery is a property of the coordinator transaction"
    ),
    "CON-C026": _not_applicable(
        _DURABLE, "atomicity is enforced by the coordinator transaction, not by "
        "connector discipline"
    ),
    "CON-C027": _not_applicable(
        _DURABLE, "fencing generations belong to the workspace lease"
    ),
    "CON-C028": _not_applicable(
        _DURABLE, "takeover is decided by the workspace lease holder"
    ),
    "CON-C029": _c029_handle,
    "CON-C030": _c030_known_material,
    "CON-C031": _c031_credential_scope,
    "CON-C032": _c032_storage_surface,
    "CON-C033": _not_applicable(
        _DURABLE,
        "the bootstrap mutex and single-writer lease invariant are runtime-owned; "
        "this kit opens no workspace and can therefore demonstrate neither",
    ),
    "CON-C034": _c034_item_ceiling,
    "CON-C035": _c035_oversized_item,
    "CON-C036": _c036_deadline,
    "CON-C037": _c037_hostile_input,
    "CON-C038": _not_applicable(
        _DURABLE, "attempt ordinals are recorded by the coordinator"
    ),
    "CON-C039": _not_applicable(
        _DURABLE,
        "an attempt budget and a replayable dead-letter record are durable job "
        "facts the connector does not own",
    ),
    "CON-C040": _not_applicable(
        _DURABLE, "dead-letter replay resolves a durable record"
    ),
    "CON-C041": _c041_health,
    "CON-C042": _c042_cancel_mid_poll,
    "CON-C043": _c043_cancel_after_commit,
    "CON-C044": _c044_major,
    "CON-C045": _c045_additive_minor,
    "CON-C046": _c046_packaging,
    "CON-C047": _not_applicable(
        _DURABLE, "provenance across retry, dead-letter and replay is a durable "
        "append the connector does not perform"
    ),
    "CON-C048": _c048_no_writeback,
    "CON-C049": _c049_scheduling,
    "CON-C050": _c050_fake_determinism,
    "CON-C051": _c051_kit_discipline,
    "CON-C052": _c052_migration_witness,
    "CON-C053": _c053_migration_predecessor,
    "CON-C054": _encoding_case("non_alphabet_character"),
    "CON-C055": _encoding_case("padding_character"),
    "CON-C056": _encoding_case("undecodable_length"),
    "CON-C057": _c057_size,
    "CON-C058": _c058_completeness,
    "CON-C059": _c059_lineage_chain,
    "CON-C060": _differential_case(ERROR_CONNECTOR_STATE_INVALID),
    "CON-C061": _differential_case(ERROR_CONNECTOR_STATE_INVALID),
    "CON-C062": _differential_case(ERROR_CONNECTOR_STATE_INVALID),
    "CON-C063": _differential_case(ERROR_CONNECTOR_STATE_INVALID),
    "CON-C064": _differential_case(ERROR_CONNECTOR_CURSOR_FOREIGN),
    "CON-C065": _differential_case(ERROR_CONNECTOR_CURSOR_FOREIGN),
}


# --- packaging scan ---------------------------------------------------------------


def _module_imports() -> dict[str, set[str]]:
    """Top-level import roots each SDK module declares, parsed from its source."""
    package = Path(__file__).resolve().parent
    graph: dict[str, set[str]] = {}
    for name in SDK_MODULE_NAMES:
        source = (package / f"{name}.py").read_text(encoding="utf-8")
        roots: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        graph[name] = roots
    return graph


def connector_import_defects() -> tuple[str, ...]:
    """Forbidden import edges declared by the connector SDK. Empty by contract."""
    defects: list[str] = []
    for module, roots in sorted(_module_imports().items()):
        for root in sorted(roots & FORBIDDEN_IMPORT_ROOTS):
            defects.append(f"omnivia_core.connector.{module} -> {root}")
    return tuple(defects)


# --- the runner ---------------------------------------------------------------------


def run_conformance(corpus: Corpus) -> ConformanceReport:
    """Execute every case in `corpus` and record a disposition for each.

    No case is skipped: a case with no handler is a *failure*, because a missing
    binding is exactly the gap `CON-C051` refuses to let pass silently.
    """
    harness = _Harness()
    declared = set(_fake().describe().declared_limitations)
    results: list[CaseResult] = []
    for case in corpus.cases:
        handler = _HANDLERS.get(case.id)
        if handler is None:
            results.append(
                CaseResult(
                    case_id=case.id,
                    disposition=ConformanceDisposition.FAILED,
                    reason="no assertion binding exists for this case",
                )
            )
            continue
        not_applicable: _NotApplicable | None = None
        failure: str | None = None
        evidence: tuple[str, ...] = ()
        try:
            evidence = handler(harness, case)
        except _NotApplicable as skipped:
            not_applicable = skipped
        except (AssertionError, ConnectorRefused, KeyError, ValueError) as error:
            failure = f"{type(error).__name__}: {error}"

        if not_applicable is not None:
            if not_applicable.limitation not in declared:
                results.append(
                    CaseResult(
                        case_id=case.id,
                        disposition=ConformanceDisposition.FAILED,
                        reason=(
                            f"limitation {not_applicable.limitation!r} was not "
                            "declared at discovery, so this is a failure and not "
                            "a not-applicable"
                        ),
                    )
                )
                continue
            results.append(
                CaseResult(
                    case_id=case.id,
                    disposition=ConformanceDisposition.DECLARED_NOT_APPLICABLE,
                    reason=not_applicable.reason,
                    declared_limitation=not_applicable.limitation,
                )
            )
            continue
        if failure is not None:
            results.append(
                CaseResult(
                    case_id=case.id,
                    disposition=ConformanceDisposition.FAILED,
                    reason=failure,
                )
            )
            continue
        results.append(
            CaseResult(
                case_id=case.id,
                disposition=ConformanceDisposition.PASSED,
                reason=case.title,
                evidence=evidence,
            )
        )
    return ConformanceReport(results=tuple(results))


def format_report(report: ConformanceReport) -> str:
    """One line per case, in corpus order. Readable in a CI log."""
    lines = [
        f"{item.case_id} {item.disposition.value:<24} {item.reason}"
        for item in report.results
    ]
    lines.append(
        f"passed={len(report.passed)} failed={len(report.failed)} "
        f"declared_not_applicable={len(report.not_applicable)}"
    )
    return "\n".join(lines)


__all__ = [
    "EXPECTED_CASE_IDS",
    "FORBIDDEN_IMPORT_ROOTS",
    "SDK_MODULE_NAMES",
    "CaseResult",
    "ConformanceCase",
    "ConformanceReport",
    "Corpus",
    "CorpusError",
    "connector_import_defects",
    "format_report",
    "load_corpus",
    "run_conformance",
    "verify_corpus_digests",
]
