"""T-0688 IP-06/IP-07: the Workflow Run binding and transition repositories (0035).

One thing, said four ways.

*A bound Run is one write.* A new Workflow Run and its complete, immutable
`RuntimeDefinitionBinding` are admitted together or not at all:
:meth:`WorkflowBindingWriter.admit_bound_run` issues both inserts into one caller
transaction, so a binding that is malformed, incomplete, non-canonical, or mismatched
against the exact Workflow Version, definition digest or plan the run names leaves
neither row behind. There is no second entry point that writes the run alone, which is
what makes "no run-only residue" a property of the seam rather than a convention.

The mismatch checks are 0035's own INSERT trigger, not a re-implementation here. The
trigger joins `omnivia_workflow_runs` and `omnivia_workflow_plans` and refuses a
binding whose `workflowId`, `workflowVersion` or `definitionDigest` disagrees with what
this same transaction is making durable. A Python copy of that join would be a second
place for the same rule to drift, and it would not be the one that runs.

*A Run admitted before this lane existed is a Legacy Run, and stays one.* Nothing here
writes, backfills or synthesises a binding for a run that has none.
:func:`read_runtime_definition_binding_projection` labels it -- `legacyBinding: true`,
no `bindingRef` -- and the only historical reference it names is the exact release pin
the run row itself already proves. A binding invented on a run's behalf would be a
fabricated execution history, which is exactly the defect the binding exists to prevent.

*The record is the bytes.* A binding is stored as the complete RFC 8785 (JCS) canonical
document produced by :func:`canonicalize`, beside the `sha256:` digest of exactly those
bytes and their exact UTF-8 length. Every read recomputes all three, re-canonicalises,
re-validates through the public contract validator and checks the indexed columns
against the document itself, so a file edited outside this database fails closed with a
`StorageError` rather than returning something that merely parses.

*A resume decides; it never rebinds.* :func:`evaluate_binding_resume` and
:func:`reconcile_binding_decision` take no connection at all. They compare a current
exact resolution against a binding already read and return the public
`RuntimeBindingResumeDecision` for the owning Evidence seam to record. Every decision
names the Evidence reference its caller supplies and is validated by the public
validator before it is returned; this module keeps no Evidence store of its own and has
no statement that could change a stored binding even if it wanted one.

*A transition is one write too, and it is fenced by the revision it expected.* IP-07's
:meth:`WorkflowTransitionWriter.apply_transition_bundle` records one
`RuntimeTransitionBundle` and the single `RuntimeJournalEvent` it carries as one
deferred-foreign-key pair, against a `StoredRuntimeDefinitionBinding` that a verified
read produced -- so the workspace, the Run and the binding a bundle is attributed to are
the ones already proven durable, never three identifiers a caller states alongside. The
writer is fail-safe and closed by rollout stage: `R0` and any stage outside the closed
set refuse before a statement is issued, `R1` may record a dual-write candidate this
repository claims no authority or parity for, and only `R2` records as authoritative
storage.

*The chain is recomputed, never accepted.* The exact per-Run genesis link is derived
here, from one declared preimage, by :func:`journal_genesis_link`; every sequence after
zero must name the full canonical event document digest of its persisted predecessor.
0035 checks digest shape, contiguity and predecessor equality, and cannot derive the
genesis value or hash a document at all, so a wrong link, a wrong genesis or a missing
predecessor is refused here with `RT_JOURNAL_INTEGRITY_FAILURE` before any insert. A
bundle already recorded under the same identifier replays as a no-op when its public
`payloadDigest` is bit-for-bit the one already stored, and refuses with
`RT_BUNDLE_INTEGRITY_CONFLICT` when it is not; a bundle expecting a revision other than
the Run's current produced head refuses with `RT_BUNDLE_REVISION_CONFLICT`. Every one of
those is a `TransitionBundleRefused` naming its own closed diagnostic, so a caller
asserts on the code rather than on a sentence.

Storage only. No admission service, no execution, no scheduler, no Evidence writer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.semantics_workflow import (
    COMPONENT_IMPLEMENTATION_BINDING_STATES,
    RUNTIME_BINDING_RECONCILE_OUTCOMES,
    validate_runtime_binding_resume_decision,
    validate_runtime_definition_binding,
    validate_runtime_definition_binding_projection,
    validate_runtime_journal_event,
    validate_transition_bundle,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

_RUNS: Final = "omnivia_workflow_runs"
_PLANS: Final = "omnivia_workflow_plans"
_BINDINGS: Final = "omnivia_workflow_runtime_bindings"
_BUNDLES: Final = "omnivia_workflow_transition_bundles"
_JOURNAL: Final = "omnivia_workflow_runtime_journal_events"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_NO_MATERIAL_STATES: Final[Mapping[str, str]] = MappingProxyType({})

#: The one Component implementation state a resume may proceed under. Every other member
#: of the public closed vocabulary -- `missing`, `revoked`, `incompatible` -- is bound
#: material the caller has explicitly reported as no longer usable, and refuses.
_AVAILABLE: Final = "available"

#: What a resume actually compares: every content-addressed pin and exact reference the
#: binding names, and nothing else. `bindingId`, `bindingSchemaVersion`, `boundAt` and
#: `boundBy` are excluded deliberately -- they record when and by whom this binding was
#: written, not what the Run executes against, so a re-resolution that restates the same
#: material at a new instant has not drifted. The two optional model-policy fields are
#: compared by presence as well as by value: the contract requires them together, so one
#: side naming a model policy the other does not is drift, not a missing field.
_BOUND_MATERIAL_FIELDS: Final = (
    "workflowId",
    "workflowVersion",
    "releaseRef",
    "definitionDigest",
    "executionProfileDigest",
    "effectivePolicyDigest",
    "componentImplementationDigests",
    "resourceBindingSnapshots",
    "modelPolicySnapshotRef",
    "modelPolicySnapshotDigest",
)

#: One binding, and the exact Run and plan facts it has to still agree with. The join is
#: 0035's own INSERT-trigger join, re-run on the read side: the trigger proved these
#: relations at admission, and a file edited outside this database can contradict them
#: afterwards. `LEFT JOIN`, deliberately -- a binding row whose run or plan relation has
#: gone missing must reach the checks below and fail closed, not vanish from the result
#: set and read as a Legacy Run that never had a binding at all.
_BINDING_QUERY: Final = f"""
SELECT b.binding_id, b.binding_schema_version, b.binding_json, b.binding_digest,
       b.binding_byte_length, b.bound_at_us,
       r.workflow_id, r.workflow_version, r.bound_at_us, p.definition_hash
FROM {_BINDINGS} b
LEFT JOIN {_RUNS} r
       ON r.workspace_id = b.workspace_id AND r.run_id = b.run_id
LEFT JOIN {_PLANS} p
       ON p.workspace_id = r.workspace_id
      AND p.workflow_id = r.workflow_id
      AND p.workflow_version = r.workflow_version
      AND p.plan_hash = r.plan_hash
WHERE b.workspace_id = ? AND b.run_id = ?
"""


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def _canonical_document(value: Mapping[str, object]) -> tuple[str, str, int]:
    """The JCS canonical bytes of one binding, their digest and their exact length.

    RFC 8785 through this repository's own :func:`canonicalize`, not a local
    `json.dumps` convention: the digest addresses these bytes, and bytes only another
    JCS implementation can reproduce are what make that address mean anything.
    """
    document = canonicalize(dict(value))
    return document, _digest(document), len(document.encode("utf-8"))


def _verified_document(
    text: object, digest: object, byte_length: object, subject: str
) -> dict[str, Any]:
    """One stored document, believed only after its whole address is recomputed.

    The bytes, the digest, the length and the canonical spelling, in that order, then the
    JSON value domain -- everything that has to hold before a caller may look at a member
    at all. `subject` names the record in the refusal, because the same recomputation
    guards a binding, a bundle, a journal event and that event's payload, and a reader
    that cannot tell which of the four disagreed is a reader that cannot act on it.
    """
    if not isinstance(text, str):
        raise StorageError(f"a stored {subject} is not text")
    if not isinstance(digest, str) or _digest(text) != digest:
        raise StorageError(f"a stored {subject} does not match its recorded digest")
    if not isinstance(byte_length, int) or byte_length != len(text.encode("utf-8")):
        raise StorageError(
            f"a stored {subject} does not match its recorded byte length"
        )
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise StorageError(f"a stored {subject} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise StorageError(f"a stored {subject} is not a JSON object")
    try:
        canonical = canonicalize(decoded)
    except ContractSemanticError as error:
        # Valid JSON is not necessarily canonicalizable: OmniVia admits a strict subset
        # of the JSON value domain, and a document outside it -- an integer that does not
        # round trip through binary64, a lone surrogate -- has no canonical form to
        # compare against at all. Fail closed, like every other read-side disagreement.
        raise StorageError(f"a stored {subject} cannot be canonicalized") from error
    if canonical != text:
        raise StorageError(f"a stored {subject} is not canonical JSON")
    return decoded


def _instant_us(value: str) -> int:
    """One public `Timestamp` as the exact microsecond column it is stored in.

    Two ways this conversion can be inexact, and both fail closed rather than storing a
    different instant than the record names. `Timestamp` allows up to nine fractional
    digits while the column holds microseconds, and `datetime.fromisoformat` truncates
    the excess silently rather than refusing it; and 0035 requires a positive instant,
    which the epoch itself is not.

    Only ever called on a timestamp a public validator has already accepted, on both the
    write and the read path, so the calendar is known good by the time it arrives and the
    parse itself cannot fail.
    """
    fraction = value.partition(".")[2].removesuffix("Z")
    if any(digit != "0" for digit in fraction[6:]):
        raise StorageError(
            f"an instant finer than a microsecond cannot be stored: {value!r}"
        )
    microseconds = (datetime.fromisoformat(value) - _EPOCH) // timedelta(microseconds=1)
    if microseconds <= 0:
        raise StorageError(f"{value!r} is not a storable instant")
    return microseconds


def _bound_material(binding: Mapping[str, object]) -> str:
    """The canonical bytes of exactly the material a resume compares."""
    return canonicalize(
        {key: binding[key] for key in _BOUND_MATERIAL_FIELDS if key in binding}
    )


@dataclass(frozen=True, slots=True)
class BoundRunAdmission:
    """One new Workflow Run and the complete binding it is admitted with.

    Immutable, and complete: everything the run row states and the whole binding
    document, because both are written once and neither is ever amended. `binding` is
    the public `RuntimeDefinitionBinding` wire mapping -- a raw mapping rather than a
    generated dataclass, because the contract has no generated type for it and
    publishing a second wire shape here would be this module inventing one.

    `bound_at_us` is the run's own admission instant. The binding's instant is not
    stated twice: it is `binding["boundAt"]`, converted exactly, and 0035 refuses a
    binding that predates the run it binds.
    """

    run_id: str
    workflow_id: str
    workflow_version: str
    plan_hash: str
    bound_at_us: int
    binding: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StoredRuntimeDefinitionBinding:
    """One `RuntimeDefinitionBinding` and the address of the bytes it was stored as.

    The same shape, and for the same reason, as `agent_runtime.StoredPolicySnapshot`:
    the address and the length are properties of the storage rather than of the public
    contract, so they live here instead of widening the wire record with two fields
    nobody publishes. `workspace_id` and `run_id` are storage metadata for the same
    reason -- they are the primary key this document was read under, not wire members --
    and they are here so a decision made about this record cannot be attributed to a
    different Run: :func:`evaluate_binding_resume` takes its `runId` from `run_id`
    rather than from a caller who might name another one.
    """

    binding: Mapping[str, object]
    content_address: str
    content_length_bytes: int
    workspace_id: str
    run_id: str


# --- writes -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowBindingWriter:
    """The binding writes, issued into a transaction that is already open.

    Not usefully constructible on its own: :func:`workflow_binding_writer` and
    :func:`transaction_local_binding_writer` are what hand one out, exactly as
    `agent_runtime` does for the canonical Runtime records, and neither issues a
    statement outside a fenced transaction. The workspace is bound at construction
    because a run and its binding are one workspace's work.
    """

    connection: sqlite3.Connection
    workspace_id: str

    def admit_bound_run(
        self, admission: BoundRunAdmission
    ) -> StoredRuntimeDefinitionBinding:
        """Record one Workflow Run and its complete binding, in the caller's transaction.

        Both statements land together or neither does. The binding is validated through
        the public contract validator and canonicalised before any statement is issued,
        so a malformed or incomplete one refuses with the contract's own
        `ContractSemanticError` and writes nothing at all; a binding that disagrees with
        the run or the plan is refused by 0035's trigger, which rolls the run row back
        with it.

        Returns what was stored, so the caller has the content address its Evidence
        record needs without reading the row back.
        """
        validate_runtime_definition_binding(admission.binding)
        document, digest, byte_length = _canonical_document(admission.binding)
        binding = dict(admission.binding)
        bound_at_us = _instant_us(str(binding["boundAt"]))
        self.connection.execute(
            f"INSERT INTO {_RUNS} (workspace_id, run_id, workflow_id, workflow_version, "
            "plan_hash, bound_at_us) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                admission.run_id,
                admission.workflow_id,
                admission.workflow_version,
                admission.plan_hash,
                admission.bound_at_us,
            ),
        )
        self.connection.execute(
            f"INSERT INTO {_BINDINGS} (workspace_id, run_id, binding_id, "
            "binding_schema_version, binding_json, binding_digest, binding_byte_length, "
            "bound_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                admission.run_id,
                binding["bindingId"],
                int(str(binding["bindingSchemaVersion"]).split(".", 1)[0]),
                document,
                digest,
                byte_length,
                bound_at_us,
            ),
        )
        return StoredRuntimeDefinitionBinding(
            binding, digest, byte_length, self.workspace_id, admission.run_id
        )


def transaction_local_binding_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> WorkflowBindingWriter:
    """The binding writes, for a caller that already holds a fenced transaction.

    The narrow companion to :func:`workflow_binding_writer`, for the seam that has to
    admit a bound run alongside its own statements -- a claim settlement, a command's
    audit event -- in one transaction, and cannot call the standalone wrapper because
    `BEGIN IMMEDIATE` does not nest.

    This weakens no fencing. It opens no transaction and validates no authority, so
    everything it issues is covered by the entry and pre-commit validation of the
    transaction the caller opened; 0035's triggers refuse an unguarded insert regardless
    of which Python object issued it.
    """
    return WorkflowBindingWriter(connection, workspace_id)


@contextmanager
def workflow_binding_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[WorkflowBindingWriter]:
    """One fenced transaction, and the binding writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_binding_writer(connection, workspace_id=workspace_id)


def admit_bound_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    admission: BoundRunAdmission,
) -> StoredRuntimeDefinitionBinding:
    """Record one Workflow Run and its binding, in its own fenced transaction."""
    with workflow_binding_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.admit_bound_run(admission)


# --- reads --------------------------------------------------------------------------


def read_runtime_definition_binding(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> StoredRuntimeDefinitionBinding | None:
    """The binding a run was admitted with, recomputed before it is believed.

    `None` is the honest answer for exactly one case: no binding row at all, which is a
    Legacy Run -- a run durable before 0035, with nothing here to derive a binding from.
    A binding row that exists and cannot be believed is never one of those. Every other
    failure is a `StorageError` -- the bytes, the digest, the length, the canonical form,
    the public contract, the columns the row is indexed by, and the Run and plan facts
    0035's INSERT trigger checked this document against all have to still agree, because
    a row edited outside this database's own guards is the case this recomputation exists
    for.
    """
    row = connection.execute(_BINDING_QUERY, (workspace_id, run_id)).fetchone()
    if row is None:
        return None
    (
        binding_id,
        schema_version,
        document,
        digest,
        byte_length,
        bound_at_us,
        run_workflow_id,
        run_workflow_version,
        run_bound_at_us,
        plan_definition_hash,
    ) = row
    decoded = _verified_document(document, digest, byte_length, "runtime binding")
    try:
        validate_runtime_definition_binding(decoded)
    except ContractSemanticError as error:
        raise StorageError(
            "a stored runtime binding is not a valid RuntimeDefinitionBinding"
        ) from error
    if (
        decoded["bindingId"] != binding_id
        or int(str(decoded["bindingSchemaVersion"]).split(".", 1)[0]) != schema_version
        or _instant_us(str(decoded["boundAt"])) != bound_at_us
    ):
        raise StorageError(
            "a stored runtime binding disagrees with the columns it is indexed by"
        )
    if run_workflow_id is None or plan_definition_hash is None:
        raise StorageError(
            "a stored runtime binding names a run or plan that is no longer there"
        )
    if (
        decoded["workflowId"] != run_workflow_id
        or decoded["workflowVersion"] != run_workflow_version
        or decoded["definitionDigest"] != plan_definition_hash
        or bound_at_us < run_bound_at_us
    ):
        raise StorageError(
            "a stored runtime binding disagrees with the run and plan it binds"
        )
    return StoredRuntimeDefinitionBinding(
        decoded, digest, byte_length, workspace_id, run_id
    )


def read_runtime_definition_binding_projection(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> dict[str, Any] | None:
    """How a Workflow Run's binding reads, current or legacy.

    `None` means this workspace holds no such run at all, which is not the same fact as
    a run that holds no binding. A run that holds none is a Legacy Run and is labelled
    as one: `legacyBinding: true`, no `bindingRef`, and the single historical reference
    the run row itself directly proves -- the exact workflow, version and plan digest it
    is bound to. Nothing is read from, or written to, the binding table for it.
    """
    row = connection.execute(
        f"SELECT workflow_id, workflow_version, plan_hash FROM {_RUNS} "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    stored = read_runtime_definition_binding(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    projection: dict[str, Any]
    if stored is None:
        projection = {
            "runId": run_id,
            "legacyBinding": True,
            "historicalExactRefs": [
                {
                    "workflowId": str(row[0]),
                    "workflowVersion": str(row[1]),
                    "planDigest": str(row[2]),
                }
            ],
        }
    else:
        projection = {
            "runId": run_id,
            "legacyBinding": False,
            "bindingRef": {
                "bindingId": stored.binding["bindingId"],
                "bindingDigest": stored.content_address,
            },
        }
    validate_runtime_definition_binding_projection(projection)
    return projection


# --- decisions ----------------------------------------------------------------------


def evaluate_binding_resume(
    *,
    stored: StoredRuntimeDefinitionBinding,
    resolved: Mapping[str, object],
    evidence: Mapping[str, object],
    material_states: Mapping[str, str] = _NO_MATERIAL_STATES,
) -> dict[str, Any]:
    """Decide whether a Run may resume against the binding already recorded for it.

    Pure. It takes no connection, writes nothing, and cannot replace the binding it
    judges: `stored` is the verified read's own record and `resolved` is the caller's
    current exact resolution of the same subject.

    The decision names `stored.run_id`, and there is no parameter that could name a
    different one. A binding's bytes are valid for exactly the Run they were read under,
    so a caller holding one Run's verified binding cannot obtain an allow -- or a refusal
    -- issued against another Run by supplying its identifier alongside.

    Three outcomes, in this order. Bound material the caller has explicitly reported as
    revoked, missing or incompatible refuses with `RT_BINDING_REVOKED` first, because
    that is a more specific fact about the same material than any difference in what it
    resolves to now. Otherwise any digest, reference or content difference across
    :data:`_BOUND_MATERIAL_FIELDS` refuses with `RT_BINDING_DRIFT`. An exact match
    allows. `reconcile` is never reached from here: it is a decision a person makes,
    which is why it has its own constructor and requires an actor and a reason.

    `material_states` maps whatever the caller names its bound material by -- Component
    identifiers, resource requirement identifiers -- to a state from the public
    `COMPONENT_IMPLEMENTATION_BINDING_STATES` vocabulary. A state outside it is a
    `StorageError`: an unknown state is not evidence that anything is available.
    """
    validate_runtime_definition_binding(stored.binding)
    validate_runtime_definition_binding(resolved)
    for subject, state in material_states.items():
        if state not in COMPONENT_IMPLEMENTATION_BINDING_STATES:
            raise StorageError(
                f"bound material {subject!r} names an unknown state {state!r}"
            )

    decision: dict[str, Any] = {"runId": stored.run_id, "evidence": evidence}
    if any(state != _AVAILABLE for state in material_states.values()):
        decision |= {"decision": "refuse", "diagnostic": "RT_BINDING_REVOKED"}
    elif _bound_material(stored.binding) != _bound_material(resolved):
        decision |= {"decision": "refuse", "diagnostic": "RT_BINDING_DRIFT"}
    else:
        decision["decision"] = "allow"
    validate_runtime_binding_resume_decision(decision)
    return decision


def reconcile_binding_decision(
    *,
    run_id: str,
    evidence: Mapping[str, object],
    deciding_actor: Mapping[str, object],
    reason: str,
    outcome: str,
) -> dict[str, Any]:
    """One explicit, attributable reconciliation of a Run against its stored binding.

    Also pure, and deliberately so: reconciling is a decision about what to do next --
    restore the exact material, terminate or refuse the run, or start a governed new
    subject -- and none of the three is a licence to rewrite the binding that was
    recorded. The actor and the reason are required by the public contract, which is
    what makes the decision attributable rather than merely recorded.
    """
    if outcome not in RUNTIME_BINDING_RECONCILE_OUTCOMES:
        raise StorageError(f"{outcome!r} is not a reconciliation outcome")
    decision: dict[str, Any] = {
        "decision": "reconcile",
        "runId": run_id,
        "evidence": evidence,
        "decidingActor": deciding_actor,
        "reason": reason,
        "outcome": outcome,
    }
    validate_runtime_binding_resume_decision(decision)
    return decision


# --- T-0688 IP-07: transition bundles and the journal they extend -------------------

#: The declared sequence-zero preimage. A Run's first journal link is the `sha256:`
#: digest of exactly the JCS canonical UTF-8 bytes of this separator beside that Run's
#: identifier, and of nothing else -- which is what makes it derivable by any other
#: implementation, and different for every Run, without a stored seed to go missing.
_GENESIS_DOMAIN_SEPARATOR: Final = "omnivia.workflow-runtime.journal.genesis.v1"

#: 0035's closed rollout vocabulary, and the subset this writer may record under. `R0` is
#: the disabled stage; `R1` is the dual-write candidate, which this repository records
#: without claiming authority or parity for it; `R2` is authoritative storage. A stage
#: outside the vocabulary refuses for the same reason `R0` does -- a stage that cannot be
#: recognised has enabled nothing -- which is what makes the gate closed rather than a
#: list of stages that happen to be off.
TRANSITION_ROLLOUT_STAGES: Final = ("R0", "R1", "R2")
_RECORDING_STAGES: Final = frozenset({"R1", "R2"})

#: What a caller asserts on, instead of reading a sentence.
TRANSITION_BUNDLE_DISPOSITIONS: Final = ("applied", "replayed")
TRANSITION_BUNDLE_DIAGNOSTICS: Final = (
    "RT_BUNDLE_WRITER_DISABLED",
    "RT_BUNDLE_INTEGRITY_CONFLICT",
    "RT_BUNDLE_REVISION_CONFLICT",
    "RT_JOURNAL_INTEGRITY_FAILURE",
)

#: One bundle, its journal event, and the binding row the bundle's `binding_id` names.
#: `LEFT JOIN` for both, deliberately: a bundle whose paired event or whose binding
#: relation has gone missing must reach the checks below and fail closed, not vanish from
#: the result set and read as a bundle this workspace never recorded.
_BUNDLE_QUERY: Final = f"""
SELECT b.binding_id, b.expected_revision, b.produced_revision, b.payload_digest,
       b.bundle_json, b.bundle_digest, b.bundle_byte_length, b.recorded_at_us,
       e.event_json, e.sequence, n.binding_id
FROM {_BUNDLES} b
LEFT JOIN {_JOURNAL} e
       ON e.workspace_id = b.workspace_id AND e.run_id = b.run_id
      AND e.bundle_id = b.bundle_id
LEFT JOIN {_BINDINGS} n
       ON n.workspace_id = b.workspace_id AND n.run_id = b.run_id
WHERE b.workspace_id = ? AND b.run_id = ? AND b.bundle_id = ?
"""

#: One Run's journal in sequence order, each event beside the bundle it belongs to.
_JOURNAL_QUERY: Final = f"""
SELECT e.event_id, e.bundle_id, e.sequence, e.previous_link_digest, e.payload_digest,
       e.event_json, e.event_digest, e.event_byte_length,
       e.event_payload_json, e.event_payload_digest, e.event_payload_byte_length,
       e.recorded_at_us, b.expected_revision
FROM {_JOURNAL} e
LEFT JOIN {_BUNDLES} b
       ON b.workspace_id = e.workspace_id AND b.run_id = e.run_id
      AND b.bundle_id = e.bundle_id
WHERE e.workspace_id = ? AND e.run_id = ?
ORDER BY e.sequence
"""


def journal_genesis_link(run_id: str) -> str:
    """The exact `previousIntegrityLink` this Run's sequence-zero journal event names.

    Pure, and the only derivation of that value in this repository. SQL checks the shape
    of a link and the equality of every link after the first; it cannot hash a document,
    so the genesis value is computed here and recomputed here, from one declared preimage
    -- the canonical JSON of the domain separator and this Run's identifier -- rather than
    being read back from a column that could be edited to whatever a forged first event
    happens to name.
    """
    return _digest(
        canonicalize({"domainSeparator": _GENESIS_DOMAIN_SEPARATOR, "runId": run_id})
    )


class TransitionBundleRefused(StorageError):
    """A refusal that carries the closed diagnostic naming why, not only prose.

    A `StorageError`, so a caller that already fails closed on storage refusals keeps
    doing so; `diagnostic` is what a caller that wants to distinguish a replay conflict
    from a revision conflict from a broken chain reads instead of matching text. This is
    deliberately storage-local: it is an exception attribute, not a second wire contract
    beside the public `RuntimeTransitionBundle`.
    """

    def __init__(self, diagnostic: str, detail: str) -> None:
        super().__init__(f"{detail} ({diagnostic})")
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class TransitionBundleOutcome:
    """What one accepted `apply_transition_bundle` did, and to which revision.

    `disposition` is `applied` for a bundle this call made durable and `replayed` for one
    already durable under the same identifier and the same public `payloadDigest`, whose
    revision is returned unchanged and whose journal gains no second event.
    """

    disposition: str
    bundle_id: str
    produced_revision: int
    payload_digest: str
    content_address: str


@dataclass(frozen=True, slots=True)
class StoredTransitionBundle:
    """One `RuntimeTransitionBundle` and the address of the bytes it was stored as.

    The same shape, and for the same reason, as :class:`StoredRuntimeDefinitionBinding`:
    the address, the length and the keys the document was read under are properties of
    the storage rather than members of the public contract.
    """

    bundle: Mapping[str, object]
    content_address: str
    content_length_bytes: int
    workspace_id: str
    run_id: str
    binding_id: str
    produced_revision: int


@dataclass(frozen=True, slots=True)
class StoredJournalEvent:
    """One `RuntimeJournalEvent`, the payload its `payloadDigest` names, and both addresses."""

    event: Mapping[str, object]
    payload: Mapping[str, object]
    content_address: str
    content_length_bytes: int
    workspace_id: str
    run_id: str
    bundle_id: str


@dataclass(frozen=True, slots=True)
class WorkflowTransitionWriter:
    """The transition writes, issued into a transaction that is already open.

    The companion to :class:`WorkflowBindingWriter`, and deliberately a second type
    rather than a third method on that one: admitting a bound Run and applying a
    transition to one are different writes with different fencing, and a writer named for
    bindings that also recorded journal events would misname both.
    """

    connection: sqlite3.Connection
    workspace_id: str

    def apply_transition_bundle(
        self,
        *,
        binding: StoredRuntimeDefinitionBinding,
        bundle: Mapping[str, object],
        event_payload: Mapping[str, object],
        rollout_stage: str,
    ) -> TransitionBundleOutcome:
        """Record one bundle and the journal event it carries, in the caller's transaction.

        `binding` is a verified read's own record, and it -- not the caller -- is the
        source of the workspace, the Run and the binding identifier this bundle is
        attributed to. `event_payload` is the mapping whose canonical digest the nested
        event's `payloadDigest` names; it is stored beside the event, and a payload whose
        digest is not exactly that value is refused rather than stored under a digest it
        does not have.

        Every refusal below happens before a statement is issued, and the two inserts are
        one deferred-foreign-key pair, so a refused or half-failed application leaves no
        bundle without its event and no event without its bundle.
        """
        if rollout_stage not in _RECORDING_STAGES:
            raise TransitionBundleRefused(
                "RT_BUNDLE_WRITER_DISABLED",
                f"the transition bundle writer records nothing at stage {rollout_stage!r}",
            )
        validate_transition_bundle(bundle)
        event = dict(_mapping_member(bundle, "event"))
        run_id = binding.run_id
        binding_id = str(binding.binding["bindingId"])
        if binding.workspace_id != self.workspace_id:
            raise StorageError(
                "a transition bundle cannot be applied against another workspace's binding"
            )
        if bundle["runId"] != run_id:
            raise StorageError(
                "a transition bundle names a run other than the one its binding was read for"
            )

        payload_document, payload_digest, payload_length = _canonical_document(
            event_payload
        )
        if event["payloadDigest"] != payload_digest:
            raise StorageError(
                "a journal event's payloadDigest is not the digest of the payload supplied"
            )
        expected_revision = int(str(bundle["expectedAggregateRevision"]))
        sequence = int(str(event["sequence"]))
        if sequence != expected_revision:
            raise StorageError(
                "a journal event's sequence is not the revision its bundle expected"
            )

        bundle_id = str(bundle["bundleId"])
        recorded = self.connection.execute(
            f"SELECT payload_digest, produced_revision, bundle_digest FROM {_BUNDLES} "
            "WHERE workspace_id = ? AND run_id = ? AND bundle_id = ?",
            (self.workspace_id, run_id, bundle_id),
        ).fetchone()
        if recorded is not None:
            if recorded[0] != bundle["payloadDigest"]:
                raise TransitionBundleRefused(
                    "RT_BUNDLE_INTEGRITY_CONFLICT",
                    f"transition bundle {bundle_id!r} is already recorded with a "
                    "different payload digest",
                )
            return TransitionBundleOutcome(
                "replayed",
                bundle_id,
                int(recorded[1]),
                str(recorded[0]),
                str(recorded[2]),
            )

        head = int(
            self.connection.execute(
                f"SELECT COALESCE(MAX(produced_revision), 0) FROM {_BUNDLES} "
                "WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id),
            ).fetchone()[0]
        )
        if expected_revision != head:
            raise TransitionBundleRefused(
                "RT_BUNDLE_REVISION_CONFLICT",
                f"transition bundle {bundle_id!r} expected revision {expected_revision} "
                f"and this run has produced {head}",
            )

        required_link = self._required_link(run_id, sequence)
        if event["previousIntegrityLink"] != required_link:
            raise TransitionBundleRefused(
                "RT_JOURNAL_INTEGRITY_FAILURE",
                f"the journal event at sequence {sequence} does not name the integrity "
                "link its run requires",
            )

        document, digest, byte_length = _canonical_document(bundle)
        event_document, event_digest, event_length = _canonical_document(event)
        recorded_at_us = _instant_us(str(event["recordedAt"]))
        self.connection.execute(
            f"INSERT INTO {_BUNDLES} (workspace_id, run_id, bundle_id, binding_id, "
            "expected_revision, produced_revision, payload_digest, bundle_json, "
            "bundle_digest, bundle_byte_length, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                bundle_id,
                binding_id,
                expected_revision,
                int(str(bundle["producedAggregateRevision"])),
                bundle["payloadDigest"],
                document,
                digest,
                byte_length,
                recorded_at_us,
            ),
        )
        self.connection.execute(
            f"INSERT INTO {_JOURNAL} (workspace_id, run_id, bundle_id, event_id, "
            "sequence, previous_link_digest, payload_digest, event_json, event_digest, "
            "event_byte_length, event_payload_json, event_payload_digest, "
            "event_payload_byte_length, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                bundle_id,
                str(event["eventId"]),
                sequence,
                required_link,
                payload_digest,
                event_document,
                event_digest,
                event_length,
                payload_document,
                payload_digest,
                payload_length,
                recorded_at_us,
            ),
        )
        return TransitionBundleOutcome(
            "applied",
            bundle_id,
            int(str(bundle["producedAggregateRevision"])),
            str(bundle["payloadDigest"]),
            digest,
        )

    def _required_link(self, run_id: str, sequence: int) -> str:
        """The one link this sequence may name, recomputed rather than read from the event.

        Sequence zero is the derived per-Run genesis link. Every sequence after it is the
        digest of the whole canonical event document already persisted at `sequence - 1`,
        recomputed from those bytes here rather than trusted from the predecessor's own
        `event_digest` column, so a chain anchored to an edited column is not a chain.
        """
        if sequence == 0:
            return journal_genesis_link(run_id)
        row = self.connection.execute(
            f"SELECT event_json, event_digest, event_byte_length FROM {_JOURNAL} "
            "WHERE workspace_id = ? AND run_id = ? AND sequence = ?",
            (self.workspace_id, run_id, sequence - 1),
        ).fetchone()
        if row is None:
            raise TransitionBundleRefused(
                "RT_JOURNAL_INTEGRITY_FAILURE",
                f"this run holds no journal event at sequence {sequence - 1} to link to",
            )
        _verified_document(row[0], row[1], row[2], "journal event")
        return str(row[1])


def _mapping_member(record: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = record[key]
    if not isinstance(value, Mapping):
        raise StorageError(f"{key!r} is not a mapping")
    return value


def transaction_local_transition_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> WorkflowTransitionWriter:
    """The transition writes, for a caller that already holds a fenced transaction.

    The narrow companion to :func:`workflow_transition_writer`, and it weakens no fencing
    for the same reason :func:`transaction_local_binding_writer` does not: it opens no
    transaction and validates no authority, so everything it issues is covered by the
    entry and pre-commit validation of the transaction the caller opened.
    """
    return WorkflowTransitionWriter(connection, workspace_id)


@contextmanager
def workflow_transition_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[WorkflowTransitionWriter]:
    """One fenced transaction, and the transition writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_transition_writer(connection, workspace_id=workspace_id)


def apply_transition_bundle(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    binding: StoredRuntimeDefinitionBinding,
    bundle: Mapping[str, object],
    event_payload: Mapping[str, object],
    rollout_stage: str,
) -> TransitionBundleOutcome:
    """Record one transition bundle and its journal event, in its own fenced transaction."""
    with workflow_transition_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.apply_transition_bundle(
            binding=binding,
            bundle=bundle,
            event_payload=event_payload,
            rollout_stage=rollout_stage,
        )


def read_transition_bundle(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str, bundle_id: str
) -> StoredTransitionBundle | None:
    """One recorded bundle, recomputed before it is believed.

    `None` means this workspace holds no such bundle for this Run. Everything else is a
    `StorageError`: the bytes, the digest, the length, the canonical form, the public
    contract, the columns the row is indexed by, the binding the bundle names, and the
    journal event the bundle carries -- which must be the event actually persisted for it,
    at the sequence the bundle's expected revision states.
    """
    row = connection.execute(
        _BUNDLE_QUERY, (workspace_id, run_id, bundle_id)
    ).fetchone()
    if row is None:
        return None
    (
        row_binding_id,
        expected_revision,
        produced_revision,
        payload_digest,
        document,
        digest,
        byte_length,
        recorded_at_us,
        event_document,
        event_sequence,
        held_binding_id,
    ) = row
    decoded = _verified_document(document, digest, byte_length, "transition bundle")
    try:
        validate_transition_bundle(decoded)
    except ContractSemanticError as error:
        raise StorageError(
            "a stored transition bundle is not a valid RuntimeTransitionBundle"
        ) from error
    event = _mapping_member(decoded, "event")
    if (
        decoded["bundleId"] != bundle_id
        or decoded["runId"] != run_id
        or decoded["expectedAggregateRevision"] != expected_revision
        or decoded["producedAggregateRevision"] != produced_revision
        or decoded["payloadDigest"] != payload_digest
        or _instant_us(str(event["recordedAt"])) != recorded_at_us
    ):
        raise StorageError(
            "a stored transition bundle disagrees with the columns it is indexed by"
        )
    if held_binding_id is None or held_binding_id != row_binding_id:
        raise StorageError(
            "a stored transition bundle names a binding its run does not hold"
        )
    if event_document is None or event_sequence != expected_revision:
        raise StorageError(
            "a stored transition bundle is not paired with the journal event it carries"
        )
    if canonicalize(dict(event)) != event_document:
        raise StorageError(
            "a stored transition bundle disagrees with the journal event recorded for it"
        )
    return StoredTransitionBundle(
        decoded,
        digest,
        byte_length,
        workspace_id,
        run_id,
        str(row_binding_id),
        int(produced_revision),
    )


def read_runtime_journal_events(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[StoredJournalEvent, ...]:
    """One Run's journal in sequence order, with the whole chain recomputed.

    An empty result means this workspace holds no journal for this Run. Anything present
    is verified whole before any of it is returned: both documents against their own
    digests and lengths, the event against the public contract and against the columns it
    is indexed by, the payload against the digest the event names for it, the bundle
    relation the event belongs to, contiguity from zero, and every integrity link --
    genesis at sequence zero, and the predecessor's full canonical event digest after it.
    """
    events: list[StoredJournalEvent] = []
    link = journal_genesis_link(run_id)
    for sequence, row in enumerate(
        connection.execute(_JOURNAL_QUERY, (workspace_id, run_id))
    ):
        (
            event_id,
            bundle_id,
            row_sequence,
            previous_link,
            row_payload_digest,
            document,
            digest,
            byte_length,
            payload_document,
            payload_digest,
            payload_length,
            recorded_at_us,
            expected_revision,
        ) = row
        decoded = _verified_document(document, digest, byte_length, "journal event")
        payload = _verified_document(
            payload_document, payload_digest, payload_length, "journal event payload"
        )
        try:
            validate_runtime_journal_event(decoded, run_id=run_id)
        except ContractSemanticError as error:
            raise StorageError(
                "a stored journal event is not a valid RuntimeJournalEvent"
            ) from error
        if (
            decoded["eventId"] != event_id
            or decoded["sequence"] != row_sequence
            or decoded["previousIntegrityLink"] != previous_link
            or decoded["payloadDigest"] != payload_digest
            or row_payload_digest != payload_digest
            or _instant_us(str(decoded["recordedAt"])) != recorded_at_us
        ):
            raise StorageError(
                "a stored journal event disagrees with the columns it is indexed by"
            )
        if expected_revision is None or expected_revision != row_sequence:
            raise StorageError(
                "a stored journal event is not paired with the bundle whose revision it produced"
            )
        if row_sequence != sequence or previous_link != link:
            raise StorageError(
                "a stored journal event does not continue its run's integrity chain"
            )
        link = digest
        events.append(
            StoredJournalEvent(
                decoded,
                payload,
                digest,
                byte_length,
                workspace_id,
                run_id,
                str(bundle_id),
            )
        )
    return tuple(events)


__all__ = [
    "TRANSITION_BUNDLE_DIAGNOSTICS",
    "TRANSITION_BUNDLE_DISPOSITIONS",
    "TRANSITION_ROLLOUT_STAGES",
    "BoundRunAdmission",
    "StoredJournalEvent",
    "StoredRuntimeDefinitionBinding",
    "StoredTransitionBundle",
    "TransitionBundleOutcome",
    "TransitionBundleRefused",
    "WorkflowBindingWriter",
    "WorkflowTransitionWriter",
    "admit_bound_run",
    "apply_transition_bundle",
    "evaluate_binding_resume",
    "journal_genesis_link",
    "read_runtime_definition_binding",
    "read_runtime_definition_binding_projection",
    "read_runtime_journal_events",
    "read_transition_bundle",
    "reconcile_binding_decision",
    "transaction_local_binding_writer",
    "transaction_local_transition_writer",
    "workflow_binding_writer",
    "workflow_transition_writer",
]
