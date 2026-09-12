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
bundle already recorded under the same identifier replays as a no-op when its whole
canonical document is bit-for-bit the one already stored, and refuses with
`RT_BUNDLE_INTEGRITY_CONFLICT` when any of it differs -- the revision it expected and
the event it carries included, because agreeing on `payloadDigest` is not being the same
transition; a bundle expecting a revision other than
the Run's current produced head refuses with `RT_BUNDLE_REVISION_CONFLICT`. Every one of
those is a `TransitionBundleRefused` naming its own closed diagnostic, so a caller
asserts on the code rather than on a sentence.

*Parity is measured against the bytes, and only while there are two writers.* IP-07's
:meth:`WorkflowJournalGovernanceWriter.record_transition_parity_report` compares a state
digest the existing writer supplies against the digest of the stored bundle a verified
read recomputed, never one a caller states for it, and records one canonical report per
bundle. Only `R1` may record: `R0` has no bundle writer to disagree with and `R2` is no
longer dual-write, so both refuse rather than record a comparison that means nothing.
:func:`evaluate_parity_promotion` answers over a declared population of bundles and
never over "whatever happens to be reported": a missing report, a diverged one or an
empty population all block promotion.

*Integrity is verified, quarantined and never repaired.*
:meth:`WorkflowJournalGovernanceWriter.verify_journal_integrity` walks the sequence set
the persisted bundles imply -- not the events that happen to have survived -- so a
removed row is a `sequence_gap` at the sequence it is missing from, and a row that is
there but cannot be believed is an `integrity_failure` at its own. `R0` observes and
records; `R1` and `R2` append a quarantine disposition in the same fenced transaction as
the report. Nothing here writes a new public Workflow Run state, deletes, folds,
reconstructs or renumbers anything, and a held quarantine refuses with
`RT_JOURNAL_QUARANTINED` rather than skipping the history it cannot verify. It refuses
both directions: a resume, and -- in
:meth:`WorkflowTransitionWriter.apply_transition_bundle` and again in 0035's own insert
guards -- any write that would extend the chain the quarantine holds, because a hold
that only stops the reader is a hold a writer walks straight past. A release is
explicit, attributable and fenced; there is no statement that could release one Run's
quarantine from another Run's decision, and one decision discharges one hold, so a Run
held for two distinct findings stays held until both have been answered -- and stays
unwritable until then too. A decision carries its own identity, so a redelivered release
replays the one result it already produced instead of discharging the next hold down,
and a repeated identity on different terms refuses; 0035's unique index enforces the
same rule under any writer.

*A retention boundary records that a range is removable; it never removes it.*
:meth:`WorkflowJournalGovernanceWriter.record_retention_boundary` writes one boundary
row and no DELETE at all. A boundary that names a removed range must say the Run is not
resumable after it, and once any boundary has said so no later boundary restores
resumability -- :func:`read_journal_retention_posture` reads every boundary the Run
holds, not only the newest, so a resumable-looking boundary appended afterwards cannot
quietly undo the policy that was already recorded.

Storage only. No admission service, no execution, no scheduler, no Evidence writer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
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
_PARITY: Final = "omnivia_workflow_transition_parity_reports"
_INTEGRITY: Final = "omnivia_workflow_journal_integrity_reports"
_QUARANTINE: Final = "omnivia_workflow_journal_quarantine_events"
_RETENTION: Final = "omnivia_workflow_journal_retention_boundaries"
_AUDIT: Final = "omnivia_application_audit_events"

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


def bound_material(binding: Mapping[str, object]) -> str:
    """The canonical bytes of exactly the material a resume compares.

    Public because admission has the same question as resume: two bindings under one
    Run identity are the same binding when this is equal, and a second answer to that
    question -- a field list copied into another module -- is a field list that drifts.
    """
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
    elif bound_material(stored.binding) != bound_material(resolved):
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
    "RT_JOURNAL_QUARANTINED",
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
    already durable under the same identifier and bit-for-bit the same canonical
    document, whose revision is returned unchanged and whose journal gains no second
    event.
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

        A Run holding a journal quarantine records nothing at all here. `RT_JOURNAL_QUARANTINED`
        was a read-only posture -- :func:`evaluate_journal_resume` reported it and a caller
        that did not ask advanced the chain anyway -- and an advisory hold on an
        unverifiable journal holds nothing. The refusal is checked here *and* enforced in
        0035's own insert guards, so it is not a check a lower-level caller can route
        around. It covers the replay path too: confirming a replay means reading the very
        chain the quarantine says nobody has been able to verify.

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
        if read_journal_quarantine(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        ).held:
            raise TransitionBundleRefused(
                "RT_JOURNAL_QUARANTINED",
                f"run {run_id!r} holds a journal quarantine and records no transition",
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
        document, digest, byte_length = _canonical_document(bundle)
        recorded = self.connection.execute(
            f"SELECT bundle_json, produced_revision FROM {_BUNDLES} "
            "WHERE workspace_id = ? AND run_id = ? AND bundle_id = ?",
            (self.workspace_id, run_id, bundle_id),
        ).fetchone()
        if recorded is not None:
            # The whole canonical document, not the two members a replay happens to be
            # keyed by. `payloadDigest` addresses the transition's payload and says
            # nothing about the revision the bundle expected or the event it carries, so
            # a second bundle agreeing on it and disagreeing on either of those is a
            # different transition arriving under a recorded identifier -- which is the
            # conflict this diagnostic exists for, not a replay.
            if recorded[0] != document:
                raise TransitionBundleRefused(
                    "RT_BUNDLE_INTEGRITY_CONFLICT",
                    f"transition bundle {bundle_id!r} is already recorded as a "
                    "different bundle",
                )
            return TransitionBundleOutcome(
                "replayed",
                bundle_id,
                int(recorded[1]),
                str(bundle["payloadDigest"]),
                digest,
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
        event, link = _verified_journal_row(
            row, workspace_id=workspace_id, run_id=run_id, sequence=sequence, link=link
        )
        events.append(event)
    return tuple(events)


def _verified_journal_row(
    row: tuple[Any, ...],
    *,
    workspace_id: str,
    run_id: str,
    sequence: int,
    link: str,
) -> tuple[StoredJournalEvent, str]:
    """One `_JOURNAL_QUERY` row, believed only after all of it is recomputed.

    Everything a caller of :func:`read_runtime_journal_events` relies on, in one place
    that the background verifier reaches too: the bytes and both addresses, the public
    contract, the columns the row is indexed by, the bundle relation the event belongs
    to, and the position and link this event must hold in its run's chain. Returns the
    verified event and the link the *next* sequence must name -- this event's whole
    canonical document digest, recomputed here rather than read from a column.
    """
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
    event = StoredJournalEvent(
        decoded, payload, digest, byte_length, workspace_id, run_id, str(bundle_id)
    )
    return event, str(digest)


# --- T-0688 IP-07: parity, integrity, quarantine and retention ----------------------

#: The one stage a parity report means anything at. `R0` runs no bundle writer, so there
#: is no second digest to disagree with; `R2` is authoritative rather than dual-write, so
#: a "parity" recorded there would compare the bundle against itself. Both refuse, and so
#: does any stage outside the closed vocabulary, for the same reason the bundle writer
#: refuses one: a stage that cannot be recognised has enabled nothing.
_DUAL_WRITE_STAGE: Final = "R1"

#: The stages at which an integrity finding quarantines rather than merely records.
#: Stated separately from :data:`_RECORDING_STAGES` although the members coincide: that
#: one is "may record a transition at all", this one is "must act on a finding", and a
#: later rollout may move one without the other.
_ENFORCING_STAGES: Final = frozenset({"R1", "R2"})

#: 0035's closed parity vocabulary.
TRANSITION_PARITY_STATUSES: Final = ("match", "diverged")

#: 0035's closed verification vocabulary, and the exact diagnostic each outcome pairs
#: with. `verified` names none, and a finding names its own code and never the other's.
JOURNAL_INTEGRITY_OUTCOMES: Final = ("verified", "sequence_gap", "integrity_failure")
JOURNAL_INTEGRITY_DIAGNOSTICS: Final[Mapping[str, str | None]] = MappingProxyType(
    {
        "verified": None,
        "sequence_gap": "RT_JOURNAL_SEQUENCE_GAP",
        "integrity_failure": "RT_JOURNAL_INTEGRITY_FAILURE",
    }
)

#: 0035's closed disposition vocabulary.
JOURNAL_QUARANTINE_ACTIONS: Final = ("quarantined", "released")

#: What a refused governance write says about itself, instead of prose a caller matches.
JOURNAL_GOVERNANCE_DIAGNOSTICS: Final = (
    "RT_PARITY_STAGE_NOT_DUAL_WRITE",
    "RT_PARITY_BUNDLE_MISSING",
    "RT_PARITY_REPORT_CONFLICT",
    "RT_JOURNAL_REPORT_CONFLICT",
    "RT_JOURNAL_NOT_QUARANTINED",
    "RT_JOURNAL_RELEASE_CONFLICT",
    "RT_RETENTION_RANGE_STILL_RESUMABLE",
)

#: Why a Run that holds durable history may still not resume. The two are separate facts
#: with separate remedies: a quarantine is released by an explicit decision, and a
#: retention boundary never is.
JOURNAL_RESUME_DIAGNOSTICS: Final = (
    "RT_JOURNAL_QUARANTINED",
    "RT_JOURNAL_RETENTION_BOUNDARY",
)

_PARITY_QUERY: Final = f"""
SELECT report_id, existing_writer_digest, bundle_derived_digest, status,
       report_json, report_digest, report_byte_length, recorded_at_us
FROM {_PARITY}
WHERE workspace_id = ? AND run_id = ? AND bundle_id = ?
"""

_INTEGRITY_QUERY: Final = f"""
SELECT rollout_stage, outcome, first_affected_sequence, diagnostic, observed_head,
       report_json, report_digest, report_byte_length, verified_at_us
FROM {_INTEGRITY}
WHERE workspace_id = ? AND run_id = ? AND report_id = ?
"""

#: One Run's dispositions in the order they were appended, each beside the journal event
#: it names. `LEFT JOIN`, deliberately: a disposition whose event has gone missing must
#: reach the checks below and fail closed, not vanish and read as no quarantine at all.
_QUARANTINE_QUERY: Final = f"""
SELECT q.disposition_sequence, q.event_id, q.action, q.integrity_report_id,
       q.diagnostic, q.deciding_actor, q.reason, q.decision_id, q.recorded_at_us,
       e.event_id
FROM {_QUARANTINE} q
LEFT JOIN {_JOURNAL} e
       ON e.workspace_id = q.workspace_id AND e.run_id = q.run_id
      AND e.event_id = q.event_id
WHERE q.workspace_id = ? AND q.run_id = ?
ORDER BY q.disposition_sequence
"""

#: One Run's recorded boundaries, oldest first, each beside the workspace Audit record it
#: names. `LEFT JOIN` for the same reason.
_RETENTION_QUERY: Final = f"""
SELECT b.boundary_id, b.first_removed_sequence, b.last_removed_sequence,
       b.resumable_after, b.policy_ref, b.evidence_ref, b.audit_ref, b.recorded_at_us,
       a.audit_ref
FROM {_RETENTION} b
LEFT JOIN {_AUDIT} a
       ON a.audit_ref = b.audit_ref AND a.workspace_id = b.workspace_id
WHERE b.workspace_id = ? AND b.run_id = ?
ORDER BY b.recorded_at_us, b.boundary_id
"""


class JournalGovernanceRefused(StorageError):
    """A refused governance write, carrying the closed diagnostic naming why.

    The same shape and the same reason as :class:`TransitionBundleRefused`, and one class
    for all four writers rather than four: a caller distinguishes a parity conflict from
    a report conflict from a release with nothing to release by reading `diagnostic`,
    which is exactly what four exception types would have it do anyway. A refusal 0035
    itself issues is not one of these -- it is a `StorageError` carrying the schema's own
    sentence, because inventing a diagnostic for it here would be this module restating a
    rule it does not own.
    """

    def __init__(self, diagnostic: str, detail: str) -> None:
        super().__init__(f"{detail} ({diagnostic})")
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class StoredTransitionParityReport:
    """One parity report, and the address of the bytes it was stored as."""

    report: Mapping[str, object]
    content_address: str
    content_length_bytes: int
    workspace_id: str
    run_id: str
    bundle_id: str
    report_id: str
    status: str
    existing_writer_digest: str
    bundle_derived_digest: str


@dataclass(frozen=True, slots=True)
class ParityPromotionEligibility:
    """Whether a declared population of bundles has proven parity, and what has not.

    The population is the caller's explicit declaration, never "the bundles that happen
    to hold a report": a promotion decided over whatever was reported would be satisfied
    by reporting nothing at all. `eligible` is true only when every declared bundle holds
    a verified, readable report whose status is `match`, which an empty declaration does
    not establish and never will.
    """

    run_id: str
    eligible: bool
    declared_bundle_ids: tuple[str, ...]
    matched_bundle_ids: tuple[str, ...]
    diverged_bundle_ids: tuple[str, ...]
    unreported_bundle_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JournalVerification:
    """What one accepted `verify_journal_integrity` recorded, and whether it quarantined.

    `disposition` is `recorded` for a pass this call made durable and `replayed` for one
    already durable under the same report identifier and bit-for-bit the same report,
    which appends nothing -- no second report, and no second disposition.
    """

    disposition: str
    report_id: str
    run_id: str
    rollout_stage: str
    outcome: str
    diagnostic: str | None
    first_affected_sequence: int | None
    observed_head: int
    quarantined: bool
    content_address: str


@dataclass(frozen=True, slots=True)
class StoredJournalIntegrityReport:
    """One integrity report, and the address of the bytes it was stored as."""

    report: Mapping[str, object]
    content_address: str
    content_length_bytes: int
    workspace_id: str
    run_id: str
    report_id: str
    rollout_stage: str
    outcome: str
    diagnostic: str | None
    first_affected_sequence: int | None
    observed_head: int


@dataclass(frozen=True, slots=True)
class JournalQuarantineProjection:
    """How a Run's journal quarantine currently stands, across its whole history.

    `held` is true while more quarantines have been appended than releases, so two
    findings need two decisions. `disposition_sequence` is the whole history's latest --
    what the next appended disposition must follow -- and is `-1` for a Run that has
    never held one. A held posture names the *outstanding* quarantine: the event it holds
    and the integrity report that found it, never one a release has already discharged. A
    released posture names the actor who decided it and their reason.
    Nothing here is a public Workflow Run state: this is a projection of an append-only
    disposition history, and reading it writes nothing.
    """

    run_id: str
    held: bool
    diagnostic: str | None
    event_id: str | None
    integrity_report_id: str | None
    deciding_actor: str | None
    reason: str | None
    disposition_sequence: int


@dataclass(frozen=True, slots=True)
class RetentionBoundaryRecord:
    """One recorded retention boundary, exactly as it was stored."""

    boundary_id: str
    run_id: str
    first_removed_sequence: int | None
    last_removed_sequence: int | None
    resumable_after: bool
    policy_ref: str
    evidence_ref: str
    audit_ref: str
    recorded_at_us: int


@dataclass(frozen=True, slots=True)
class JournalRetentionPosture:
    """Every boundary a Run holds, and whether any of them ended its resumability.

    `resumable` is false when *any* recorded boundary said so, not merely the newest:
    a boundary that removed history is a fact about what is no longer there, and a later
    boundary that removes nothing cannot put it back. `blocking_boundary_id` names the
    first boundary that said so, which is the policy decision an operator has to answer.
    """

    run_id: str
    resumable: bool
    blocking_boundary_id: str | None
    boundaries: tuple[RetentionBoundaryRecord, ...]


@dataclass(frozen=True, slots=True)
class JournalResumeEligibility:
    """Whether a Run may resume, and the one typed reason it may not.

    Pure projection. It mutates no public Run state, and it never answers by skipping,
    folding, inferring, reconstructing or deleting the history it could not verify.
    """

    run_id: str
    resumable: bool
    diagnostic: str | None
    integrity_report_id: str | None
    boundary_id: str | None


@dataclass(frozen=True, slots=True)
class _JournalInspection:
    """What a verification pass observed, before any of it is recorded."""

    outcome: str
    first_affected_sequence: int | None
    observed_head: int
    citable_event_id: str | None


def _stated_instant_us(value: str, subject: str) -> int:
    """One instant a caller stated, as the exact microsecond column it is stored in.

    :func:`_instant_us` is only ever called on a timestamp a public validator has already
    accepted. These entry points take one directly from a caller, so a spelling that is
    not a `Timestamp` at all reaches it, and `datetime.fromisoformat` raises `ValueError`
    for it rather than the `StorageError` every other refusal here is.
    """
    try:
        return _instant_us(value)
    except ValueError as error:
        raise StorageError(
            f"a {subject} names an instant that is not a Timestamp: {value!r}"
        ) from error


def _release_decision_id(run_id: str, deciding_actor: str, reason: str) -> str:
    """The canonical identity of one quarantine-release decision.

    Over the request and nothing else -- never over the hold the request would discharge.
    The hold moves as releases land, so an identity that included it would give a retry a
    *different* identity from the call it repeats, which is precisely how a redelivery
    ends up discharging the next hold down.
    """
    preimage = chr(31).join(("release", run_id, deciding_actor, reason))
    return sha256(preimage.encode("utf-8")).hexdigest()


#: The spellings 0035's own CHECK constraints require of these columns, recomputed on
#: the read side for exactly the reason the digests are. A file edited under `PRAGMA
#: ignore_check_constraints` holds rows the schema would have refused, so a reader that
#: trusted a column's shape because a CHECK exists for it would read those rows as facts.
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_REASON: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
_DIGEST_SPELLING: Final = re.compile(r"sha256:[0-9a-f]{64}")

#: What a parity report document holds, exactly: these members, these types, nothing
#: else. Recorded reports are this repository's own internal documents rather than
#: public wire records, so there is no published validator to hold one to -- and an
#: offline editor that recomputes `report_digest` and `report_byte_length` restores the
#: whole stored address of whatever it wrote. The shape is what is left to check.
_PARITY_SHAPE: Final[Mapping[str, type]] = MappingProxyType(
    {
        "reportId": str,
        "runId": str,
        "bundleId": str,
        "rolloutStage": str,
        "existingWriterDigest": str,
        "bundleDerivedDigest": str,
        "status": str,
        "recordedAt": str,
    }
)

#: The same, for an integrity report -- whose optional members are not optional at all:
#: a finding states both and `verified` states neither, so the outcome decides the whole
#: shape and a document holding the wrong one for its outcome was never written here.
_INTEGRITY_SHAPE: Final[Mapping[str, type]] = MappingProxyType(
    {
        "reportId": str,
        "runId": str,
        "rolloutStage": str,
        "outcome": str,
        "observedHead": int,
        "verifiedAt": str,
    }
)
_INTEGRITY_FINDING_SHAPE: Final[Mapping[str, type]] = MappingProxyType(
    {**_INTEGRITY_SHAPE, "firstAffectedSequence": int, "diagnostic": str}
)


def _spelled(pattern: re.Pattern[str], value: object, subject: str) -> str:
    """One stored scalar, held to the exact spelling its column is stored under."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or pattern.fullmatch(value) is None
    ):
        raise StorageError(
            f"a stored {subject} is not spelled as this schema stores it"
        )
    return value


def _counted(value: object, subject: str, *, minimum: int) -> int:
    """One stored integer, held to the range its column is stored under.

    `bool` is excluded explicitly: it is an `int` in Python and a distinct value domain
    in JSON, so a posture written where a sequence belongs would otherwise read as one.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StorageError(f"a stored {subject} is not a number this schema stores")
    return value


def _closed_shape(
    decoded: Mapping[str, object], shape: Mapping[str, type], subject: str
) -> None:
    """Exactly these members, each of exactly this type, and nothing else at all."""
    if set(decoded) != set(shape):
        raise StorageError(
            f"a stored {subject} does not hold exactly the members it is stored with"
        )
    for key, kind in shape.items():
        value = decoded[key]
        if not isinstance(value, kind) or isinstance(value, bool) is not (kind is bool):
            raise StorageError(
                f"a stored {subject} states {key!r} as a value of another type"
            )


def _record(
    connection: sqlite3.Connection,
    table: str,
    row: Mapping[str, object],
    *,
    subject: str,
) -> None:
    """One INSERT, with 0035's own refusal translated instead of leaked.

    Identifier shape, instant range, closed vocabularies and every relation this schema
    requires are checked by the schema, which is the copy that runs. What a caller gets
    back for breaking one of them is a `StorageError` naming what it was writing and what
    the schema said, rather than a bare `sqlite3.IntegrityError` from three frames down.
    """
    try:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
    except sqlite3.DatabaseError as error:
        raise StorageError(
            f"the workspace schema refused this {subject}: {error}"
        ) from error


@dataclass(frozen=True, slots=True)
class WorkflowJournalGovernanceWriter:
    """The parity, integrity, quarantine and retention writes, into an open transaction.

    The third writer in this module, and deliberately not a fourth set of methods on
    either of the other two: these record judgements *about* Runs already made durable,
    never the Run, the binding or the transition itself. Nothing here has a statement
    that could write a public Workflow Run state, and nothing here deletes.
    """

    connection: sqlite3.Connection
    workspace_id: str

    # --- parity against the existing writer ------------------------------------------

    def record_transition_parity_report(
        self,
        *,
        report_id: str,
        run_id: str,
        bundle_id: str,
        existing_writer_digest: str,
        rollout_stage: str,
        recorded_at: str,
    ) -> StoredTransitionParityReport:
        """Record what the two writers of one transition actually produced.

        The bundle-derived digest is recomputed from the stored bundle by
        :func:`read_transition_bundle` -- the bytes, their address, the public contract,
        the indexed columns and the binding and event relations -- and never taken from a
        caller, which is the whole point of the comparison. `existing_writer_digest` is
        the only side a caller supplies, because it is the only side this repository
        cannot recompute.

        Append-only and idempotent by bundle: the exact same report replays and returns
        what is already recorded, and a different identity or a different comparison for
        a bundle already reported refuses with `RT_PARITY_REPORT_CONFLICT` without
        touching the row that stands.
        """
        if rollout_stage != _DUAL_WRITE_STAGE:
            raise JournalGovernanceRefused(
                "RT_PARITY_STAGE_NOT_DUAL_WRITE",
                "a parity report compares two writers and stage "
                f"{rollout_stage!r} runs one",
            )
        stored = read_transition_bundle(
            self.connection,
            workspace_id=self.workspace_id,
            run_id=run_id,
            bundle_id=bundle_id,
        )
        if stored is None:
            raise JournalGovernanceRefused(
                "RT_PARITY_BUNDLE_MISSING",
                f"this run holds no transition bundle {bundle_id!r} to report parity for",
            )
        status = (
            "match" if existing_writer_digest == stored.content_address else "diverged"
        )
        report = {
            "reportId": report_id,
            "runId": run_id,
            "bundleId": bundle_id,
            "rolloutStage": rollout_stage,
            "existingWriterDigest": existing_writer_digest,
            "bundleDerivedDigest": stored.content_address,
            "status": status,
            "recordedAt": recorded_at,
        }
        document, digest, byte_length = _canonical_document(report)
        recorded_at_us = _stated_instant_us(recorded_at, "parity report")

        already = self.connection.execute(
            _PARITY_QUERY, (self.workspace_id, run_id, bundle_id)
        ).fetchone()
        if already is not None:
            if already[0] != report_id or already[4] != document:
                raise JournalGovernanceRefused(
                    "RT_PARITY_REPORT_CONFLICT",
                    f"transition bundle {bundle_id!r} already carries parity report "
                    f"{already[0]!r}",
                )
            return _verified_parity_report(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                bundle_id=bundle_id,
            )

        _record(
            self.connection,
            _PARITY,
            {
                "workspace_id": self.workspace_id,
                "report_id": report_id,
                "run_id": run_id,
                "bundle_id": bundle_id,
                "existing_writer_digest": existing_writer_digest,
                "bundle_derived_digest": stored.content_address,
                "status": status,
                "report_json": document,
                "report_digest": digest,
                "report_byte_length": byte_length,
                "recorded_at_us": recorded_at_us,
            },
            subject="transition parity report",
        )
        return StoredTransitionParityReport(
            report,
            digest,
            byte_length,
            self.workspace_id,
            run_id,
            bundle_id,
            report_id,
            status,
            existing_writer_digest,
            stored.content_address,
        )

    # --- background journal verification ---------------------------------------------

    def verify_journal_integrity(
        self,
        *,
        report_id: str,
        run_id: str,
        rollout_stage: str,
        verified_at: str,
    ) -> JournalVerification:
        """Verify one Run's journal against what its bundles say should be there.

        One report, one outcome, and at `R1` and `R2` the quarantine that outcome
        requires -- in this same transaction, so a finding that is durable is a finding
        that is acted on. `R0` observes: it records the identical report and appends no
        disposition, because the stage that enables nothing may not stop a Run either.

        The exact same pass under the same report identifier replays as a no-op; the same
        identifier over a different report refuses with `RT_JOURNAL_REPORT_CONFLICT`.
        """
        if rollout_stage not in TRANSITION_ROLLOUT_STAGES:
            raise StorageError(
                f"{rollout_stage!r} is not a rollout stage this schema records"
            )
        inspection = _inspect_journal(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )
        diagnostic = JOURNAL_INTEGRITY_DIAGNOSTICS[inspection.outcome]
        report: dict[str, Any] = {
            "reportId": report_id,
            "runId": run_id,
            "rolloutStage": rollout_stage,
            "outcome": inspection.outcome,
            "observedHead": inspection.observed_head,
            "verifiedAt": verified_at,
        }
        if inspection.first_affected_sequence is not None:
            report["firstAffectedSequence"] = inspection.first_affected_sequence
        if diagnostic is not None:
            report["diagnostic"] = diagnostic
        document, digest, byte_length = _canonical_document(report)
        verified_at_us = _stated_instant_us(verified_at, "journal integrity report")

        already = self.connection.execute(
            f"SELECT report_json FROM {_INTEGRITY} WHERE workspace_id = ? "
            "AND report_id = ?",
            (self.workspace_id, report_id),
        ).fetchone()
        if already is not None:
            if already[0] != document:
                raise JournalGovernanceRefused(
                    "RT_JOURNAL_REPORT_CONFLICT",
                    f"journal integrity report {report_id!r} is already recorded with a "
                    "different verification",
                )
            return self._verification(
                "replayed", report_id, run_id, rollout_stage, inspection, digest
            )

        _record(
            self.connection,
            _INTEGRITY,
            {
                "workspace_id": self.workspace_id,
                "report_id": report_id,
                "run_id": run_id,
                "rollout_stage": rollout_stage,
                "outcome": inspection.outcome,
                "first_affected_sequence": inspection.first_affected_sequence,
                "diagnostic": diagnostic,
                "observed_head": inspection.observed_head,
                "report_json": document,
                "report_digest": digest,
                "report_byte_length": byte_length,
                "verified_at_us": verified_at_us,
            },
            subject="journal integrity report",
        )
        if inspection.outcome != "verified" and rollout_stage in _ENFORCING_STAGES:
            self._quarantine(run_id, report_id, inspection, verified_at_us)
        return self._verification(
            "recorded", report_id, run_id, rollout_stage, inspection, digest
        )

    def _verification(
        self,
        disposition: str,
        report_id: str,
        run_id: str,
        rollout_stage: str,
        inspection: _JournalInspection,
        digest: str,
    ) -> JournalVerification:
        held = self.connection.execute(
            f"SELECT 1 FROM {_QUARANTINE} WHERE workspace_id = ? AND run_id = ? "
            "AND integrity_report_id = ?",
            (self.workspace_id, run_id, report_id),
        ).fetchone()
        return JournalVerification(
            disposition,
            report_id,
            run_id,
            rollout_stage,
            inspection.outcome,
            JOURNAL_INTEGRITY_DIAGNOSTICS[inspection.outcome],
            inspection.first_affected_sequence,
            inspection.observed_head,
            held is not None,
            digest,
        )

    def _quarantine(
        self,
        run_id: str,
        report_id: str,
        inspection: _JournalInspection,
        recorded_at_us: int,
    ) -> None:
        """Hold this Run's journal, citing the report that found the fault.

        The fault is cited by the event it is about where one survives, and by the Run's
        highest-sequence surviving event where the fault is that a row is missing. A Run
        whose journal is gone entirely has no event to name at all, and that is exactly
        the `sequence_gap` 0035 allows a disposition to hold without one -- so the worst
        journal fault is held and blocks resume like any other, rather than being the one
        an enforcing stage could not act on.
        """
        _record(
            self.connection,
            _QUARANTINE,
            {
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "disposition_sequence": self._next_disposition(run_id),
                "event_id": inspection.citable_event_id,
                "action": "quarantined",
                "integrity_report_id": report_id,
                "diagnostic": "RT_JOURNAL_QUARANTINED",
                "deciding_actor": None,
                "reason": None,
                "recorded_at_us": recorded_at_us,
            },
            subject="journal quarantine disposition",
        )

    def _next_disposition(self, run_id: str) -> int:
        return (
            int(
                self.connection.execute(
                    f"SELECT COALESCE(MAX(disposition_sequence), -1) FROM {_QUARANTINE} "
                    "WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id),
                ).fetchone()[0]
            )
            + 1
        )

    # --- release ---------------------------------------------------------------------

    def release_journal_quarantine(
        self,
        *,
        run_id: str,
        deciding_actor: str,
        reason: str,
        recorded_at: str,
        decision_id: str | None = None,
    ) -> JournalQuarantineProjection:
        """Release a held quarantine, attributably and exactly once per decision.

        A release is a person's decision, so it requires an actor and a reason and 0035
        refuses one without both. It is not a repair: it appends a disposition saying
        this Run may proceed, and changes no journal row, no bundle and no public Run
        state. The event released is the one currently held, read from the disposition
        history rather than named by the caller, so a release cannot be redirected onto
        an event -- or a Run -- other than the one actually quarantined.

        One decision discharges one hold: the latest outstanding. A Run quarantined by
        two distinct integrity reports is still held after this returns, and the
        projection it returns cites the older hold that is now the outstanding one --
        answering that finding is a second decision by a person who has seen it.

        **A decision has an identity, and a retry is not a second decision.** Stacked
        holds are where that matters: with two outstanding, the Run is still held after
        the first release, so a redelivered request -- the same actor, the same reason --
        used to read as a fresh decision and discharge the second hold, answering a
        finding the decider had never seen. Every release therefore carries a
        `decision_id`. Supplied, it is the caller's own idempotency key; omitted, it is
        derived canonically from the run, the actor and the reason, so an exact retry
        derives the same identity and replays rather than appends. A request repeating a
        recorded identity with different terms is a conflict and refuses with
        `RT_JOURNAL_RELEASE_CONFLICT`, because two decisions cannot share one identity.
        The uniqueness is 0035's, not this method's: the schema refuses a second append
        under a recorded identity even from a writer that never read this history.

        Answering a *second* finding is a second decision and needs a second identity --
        a distinct `decision_id`, stated by whoever saw the second finding. That is the
        intended cost: an unstated second release by the same actor for the same reason
        is exactly the redelivery this refuses to act on twice.

        Nothing to release refuses with `RT_JOURNAL_NOT_QUARANTINED`, unless this exact
        decision is the one already recorded, which replays.
        """
        if not deciding_actor or not reason:
            raise StorageError("a quarantine release names both an actor and a reason")
        decision = decision_id or _release_decision_id(run_id, deciding_actor, reason)
        recorded = self.connection.execute(
            f"SELECT deciding_actor, reason FROM {_QUARANTINE} "
            "WHERE workspace_id = ? AND run_id = ? AND decision_id = ?",
            (self.workspace_id, run_id, decision),
        ).fetchone()
        if recorded is not None:
            if tuple(recorded) != (deciding_actor, reason):
                raise JournalGovernanceRefused(
                    "RT_JOURNAL_RELEASE_CONFLICT",
                    f"release {decision!r} already stands on other terms",
                )
            return read_journal_quarantine(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
        current = read_journal_quarantine(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )
        if not current.held:
            raise JournalGovernanceRefused(
                "RT_JOURNAL_NOT_QUARANTINED",
                f"run {run_id!r} holds no quarantine to release",
            )
        _record(
            self.connection,
            _QUARANTINE,
            {
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "disposition_sequence": current.disposition_sequence + 1,
                "event_id": current.event_id,
                "action": "released",
                "integrity_report_id": None,
                "diagnostic": None,
                "deciding_actor": deciding_actor,
                "reason": reason,
                "decision_id": decision,
                "recorded_at_us": _stated_instant_us(recorded_at, "quarantine release"),
            },
            subject="journal quarantine release",
        )
        return read_journal_quarantine(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )

    # --- retention -------------------------------------------------------------------

    def record_retention_boundary(
        self,
        *,
        boundary_id: str,
        run_id: str,
        first_removed_sequence: int | None = None,
        last_removed_sequence: int | None = None,
        resumable_after: bool,
        policy_ref: str,
        evidence_ref: str,
        audit_ref: str,
        recorded_at: str,
    ) -> JournalRetentionPosture:
        """Record that a range of this Run's journal is now removable. Remove nothing.

        There is no DELETE in this method, in this class or in this module: a boundary is
        a recorded policy fact, and enacting it is somebody else's write against somebody
        else's authority. What it does decide is resumability -- a boundary that names a
        removed range must say the Run is not resumable after it, because a Run resumed
        across removed history would be resumed against a chain that no longer proves
        itself.

        The Audit record must already exist in this workspace; 0035's own trigger is what
        says so, and this module has no Audit writer that could satisfy it on a caller's
        behalf.
        """
        removes = (
            first_removed_sequence is not None or last_removed_sequence is not None
        )
        if removes and resumable_after:
            raise JournalGovernanceRefused(
                "RT_RETENTION_RANGE_STILL_RESUMABLE",
                f"retention boundary {boundary_id!r} removes journal history and cannot "
                "leave this run resumable",
            )
        _record(
            self.connection,
            _RETENTION,
            {
                "workspace_id": self.workspace_id,
                "boundary_id": boundary_id,
                "run_id": run_id,
                "first_removed_sequence": first_removed_sequence,
                "last_removed_sequence": last_removed_sequence,
                "resumable_after": int(resumable_after),
                "policy_ref": policy_ref,
                "evidence_ref": evidence_ref,
                "audit_ref": audit_ref,
                "recorded_at_us": _stated_instant_us(recorded_at, "retention boundary"),
            },
            subject="journal retention boundary",
        )
        return read_journal_retention_posture(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )


def transaction_local_journal_governance_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> WorkflowJournalGovernanceWriter:
    """The governance writes, for a caller that already holds a fenced transaction."""
    return WorkflowJournalGovernanceWriter(connection, workspace_id)


@contextmanager
def workflow_journal_governance_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[WorkflowJournalGovernanceWriter]:
    """One fenced transaction, and the governance writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_journal_governance_writer(
            connection, workspace_id=workspace_id
        )


def record_transition_parity_report(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    report_id: str,
    run_id: str,
    bundle_id: str,
    existing_writer_digest: str,
    rollout_stage: str,
    recorded_at: str,
) -> StoredTransitionParityReport:
    """Record one dual-write parity report, in its own fenced transaction."""
    with workflow_journal_governance_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.record_transition_parity_report(
            report_id=report_id,
            run_id=run_id,
            bundle_id=bundle_id,
            existing_writer_digest=existing_writer_digest,
            rollout_stage=rollout_stage,
            recorded_at=recorded_at,
        )


def verify_journal_integrity(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    report_id: str,
    run_id: str,
    rollout_stage: str,
    verified_at: str,
) -> JournalVerification:
    """Verify one Run's journal and record the pass, in its own fenced transaction."""
    with workflow_journal_governance_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.verify_journal_integrity(
            report_id=report_id,
            run_id=run_id,
            rollout_stage=rollout_stage,
            verified_at=verified_at,
        )


def release_journal_quarantine(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_id: str,
    deciding_actor: str,
    reason: str,
    recorded_at: str,
    decision_id: str | None = None,
) -> JournalQuarantineProjection:
    """Release one held journal quarantine, in its own fenced transaction."""
    with workflow_journal_governance_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.release_journal_quarantine(
            run_id=run_id,
            deciding_actor=deciding_actor,
            reason=reason,
            recorded_at=recorded_at,
            decision_id=decision_id,
        )


def record_retention_boundary(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    boundary_id: str,
    run_id: str,
    first_removed_sequence: int | None = None,
    last_removed_sequence: int | None = None,
    resumable_after: bool,
    policy_ref: str,
    evidence_ref: str,
    audit_ref: str,
    recorded_at: str,
) -> JournalRetentionPosture:
    """Record one journal retention boundary, in its own fenced transaction."""
    with workflow_journal_governance_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.record_retention_boundary(
            boundary_id=boundary_id,
            run_id=run_id,
            first_removed_sequence=first_removed_sequence,
            last_removed_sequence=last_removed_sequence,
            resumable_after=resumable_after,
            policy_ref=policy_ref,
            evidence_ref=evidence_ref,
            audit_ref=audit_ref,
            recorded_at=recorded_at,
        )


# --- governance reads -----------------------------------------------------------------


def _inspect_journal(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> _JournalInspection:
    """What this Run's journal actually holds, against what its bundles say it should.

    The expected sequence set comes from the persisted bundles -- every revision this Run
    has produced -- and not from the events that survived, because a verifier that
    enumerated the survivors would find every journal complete no matter how much of it
    had been removed. Walking that set from zero is also what separates the two findings
    a caller has to tell apart: a sequence with no row at all is a `sequence_gap` at that
    sequence, and a row that is there and cannot be believed is an `integrity_failure` at
    its own. The first affected sequence wins, and verification stops there.

    `observed_head` is the highest sequence a row was actually observed at, or `-1`. It
    describes what is there, so it is not derived from the bundles.
    """
    head = int(
        connection.execute(
            f"SELECT COALESCE(MAX(produced_revision), 0) FROM {_BUNDLES} "
            "WHERE workspace_id = ? AND run_id = ?",
            (workspace_id, run_id),
        ).fetchone()[0]
    )
    rows = {
        row[2]: row
        for row in connection.execute(_JOURNAL_QUERY, (workspace_id, run_id))
        if isinstance(row[2], int)
    }
    observed_head = max(rows, default=-1)
    surviving = str(rows[observed_head][0]) if rows else None

    link = journal_genesis_link(run_id)
    for sequence in range(max(head, observed_head + 1)):
        row = rows.get(sequence)
        if row is None:
            return _JournalInspection(
                "sequence_gap", sequence, observed_head, surviving
            )
        try:
            _, link = _verified_journal_row(
                row,
                workspace_id=workspace_id,
                run_id=run_id,
                sequence=sequence,
                link=link,
            )
        except StorageError:
            return _JournalInspection(
                "integrity_failure",
                sequence,
                observed_head,
                str(row[0]) if isinstance(row[0], str) else surviving,
            )
    return _JournalInspection("verified", None, observed_head, surviving)


def _verified_parity_report(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str, bundle_id: str
) -> StoredTransitionParityReport:
    stored = read_transition_parity_report(
        connection, workspace_id=workspace_id, run_id=run_id, bundle_id=bundle_id
    )
    if stored is None:  # pragma: no cover -- read inside the writing transaction
        raise StorageError("a parity report just recorded is no longer readable")
    return stored


def read_transition_parity_report(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str, bundle_id: str
) -> StoredTransitionParityReport | None:
    """The parity report recorded for one bundle, recomputed before it is believed.

    `None` means this workspace holds no report for this bundle of this Run. Everything
    else is a `StorageError`: the bytes, the digest, the length, the canonical form, the
    exact closed membership and scalar types of the document, the one stage a parity
    report is ever recorded at, the spelling of every indexed scalar, the columns the row
    is indexed by, the status against the two digests it claims to compare, and the
    bundle itself -- whose stored bytes are re-read and re-addressed, so
    a report that once matched a bundle edited afterwards does not keep saying `match`.
    """
    row = connection.execute(
        _PARITY_QUERY, (workspace_id, run_id, bundle_id)
    ).fetchone()
    if row is None:
        return None
    (
        report_id,
        existing_writer_digest,
        bundle_derived_digest,
        status,
        document,
        digest,
        byte_length,
        recorded_at_us,
    ) = row
    decoded = _verified_document(
        document, digest, byte_length, "transition parity report"
    )
    _closed_shape(decoded, _PARITY_SHAPE, "transition parity report")
    if decoded["rolloutStage"] != _DUAL_WRITE_STAGE:
        raise StorageError(
            "a stored parity report names a stage no parity is ever recorded at"
        )
    _spelled(_IDENTIFIER, report_id, "parity report identifier")
    _spelled(_DIGEST_SPELLING, existing_writer_digest, "parity report writer digest")
    _spelled(_DIGEST_SPELLING, bundle_derived_digest, "parity report bundle digest")
    _counted(recorded_at_us, "parity report instant", minimum=1)
    if (
        decoded.get("reportId") != report_id
        or decoded.get("runId") != run_id
        or decoded.get("bundleId") != bundle_id
        or decoded.get("status") != status
        or decoded.get("existingWriterDigest") != existing_writer_digest
        or decoded.get("bundleDerivedDigest") != bundle_derived_digest
        or _stated_instant_us(str(decoded.get("recordedAt")), "parity report")
        != recorded_at_us
    ):
        raise StorageError(
            "a stored parity report disagrees with the columns it is indexed by"
        )
    if status not in TRANSITION_PARITY_STATUSES or (status == "match") != (
        existing_writer_digest == bundle_derived_digest
    ):
        raise StorageError(
            "a stored parity report does not state what its digests show"
        )
    bundle = read_transition_bundle(
        connection, workspace_id=workspace_id, run_id=run_id, bundle_id=bundle_id
    )
    if bundle is None:
        raise StorageError(
            "a stored parity report names a bundle that is no longer there"
        )
    if bundle.content_address != bundle_derived_digest:
        raise StorageError(
            "a stored parity report disagrees with the bundle it was derived from"
        )
    return StoredTransitionParityReport(
        decoded,
        digest,
        byte_length,
        workspace_id,
        run_id,
        bundle_id,
        str(report_id),
        str(status),
        str(existing_writer_digest),
        str(bundle_derived_digest),
    )


def evaluate_parity_promotion(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    declared_bundle_ids: Sequence[str],
) -> ParityPromotionEligibility:
    """Whether every declared bundle has proven parity, and which have not.

    Pure read. A bundle with no readable report blocks promotion exactly as a diverged
    one does, and an empty declared population is never eligible -- promoting on the
    strength of having declared nothing is the one answer this query must never give.
    """
    declared = tuple(dict.fromkeys(declared_bundle_ids))
    matched: list[str] = []
    diverged: list[str] = []
    unreported: list[str] = []
    for bundle_id in declared:
        stored = read_transition_parity_report(
            connection, workspace_id=workspace_id, run_id=run_id, bundle_id=bundle_id
        )
        if stored is None:
            unreported.append(bundle_id)
        elif stored.status == "match":
            matched.append(bundle_id)
        else:
            diverged.append(bundle_id)
    return ParityPromotionEligibility(
        run_id,
        bool(declared) and not diverged and not unreported,
        declared,
        tuple(matched),
        tuple(diverged),
        tuple(unreported),
    )


def read_journal_integrity_report(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str, report_id: str
) -> StoredJournalIntegrityReport | None:
    """One recorded verification pass, recomputed before it is believed.

    `None` means this workspace holds no such report for this Run. Everything else is a
    `StorageError`: the bytes and their address, the canonical form, the closed outcome
    and its exact diagnostic pairing, the exact closed membership and scalar types the
    outcome requires of the document, the columns the row is indexed by, and the live
    journal -- a report cannot name a head higher than the journal now reaches, and
    :func:`_still_shows` re-checks the prefix it actually verified, which is how a report
    that outlived the rows it was made about fails closed instead of standing as evidence
    for them.
    """
    row = connection.execute(
        _INTEGRITY_QUERY, (workspace_id, run_id, report_id)
    ).fetchone()
    if row is None:
        return None
    (
        rollout_stage,
        outcome,
        first_affected_sequence,
        diagnostic,
        observed_head,
        document,
        digest,
        byte_length,
        verified_at_us,
    ) = row
    decoded = _verified_document(
        document, digest, byte_length, "journal integrity report"
    )
    if outcome not in JOURNAL_INTEGRITY_OUTCOMES or rollout_stage not in (
        TRANSITION_ROLLOUT_STAGES
    ):
        raise StorageError(
            "a stored integrity report names an outcome or stage 0035 closed"
        )
    if diagnostic != JOURNAL_INTEGRITY_DIAGNOSTICS[outcome] or (
        first_affected_sequence is None
    ) != (outcome == "verified"):
        raise StorageError(
            "a stored integrity report does not pair its outcome with its diagnostic"
        )
    _closed_shape(
        decoded,
        _INTEGRITY_SHAPE
        if decoded.get("outcome") == "verified"
        else _INTEGRITY_FINDING_SHAPE,
        "journal integrity report",
    )
    if (
        decoded.get("reportId") != report_id
        or decoded.get("runId") != run_id
        or decoded.get("rolloutStage") != rollout_stage
        or decoded.get("outcome") != outcome
        or decoded.get("observedHead") != observed_head
        or decoded.get("firstAffectedSequence") != first_affected_sequence
        or decoded.get("diagnostic") != diagnostic
        or _stated_instant_us(str(decoded.get("verifiedAt")), "integrity report")
        != verified_at_us
    ):
        raise StorageError(
            "a stored integrity report disagrees with the columns it is indexed by"
        )
    _spelled(_IDENTIFIER, report_id, "integrity report identifier")
    _counted(observed_head, "integrity report head", minimum=-1)
    _counted(verified_at_us, "integrity report instant", minimum=1)
    if first_affected_sequence is not None:
        _counted(first_affected_sequence, "integrity report finding", minimum=0)
    current = connection.execute(
        f"SELECT COALESCE(MAX(sequence), -1) FROM {_JOURNAL} "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()[0]
    if observed_head > int(current):
        raise StorageError(
            "a stored integrity report names a journal head this run no longer holds"
        )
    _still_shows(
        connection,
        workspace_id=workspace_id,
        run_id=run_id,
        outcome=str(outcome),
        first_affected_sequence=first_affected_sequence,
        observed_head=int(observed_head),
    )
    return StoredJournalIntegrityReport(
        decoded,
        digest,
        byte_length,
        workspace_id,
        run_id,
        str(report_id),
        str(rollout_stage),
        str(outcome),
        diagnostic,
        first_affected_sequence,
        int(observed_head),
    )


def _still_shows(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    outcome: str,
    first_affected_sequence: int | None,
    observed_head: int,
) -> None:
    """A recorded verification, re-checked against what the journal shows right now.

    A report is evidence about a prefix, and a prefix keeps changing underneath it: a
    Run that verified at head two and has since appended validly is still verified, so
    the report stays readable and later history is not held against it. What it may
    never do is outlive the prefix it actually verified -- a gap or an unbelievable row
    at any sequence up to its own observed head means the pass it recorded no longer
    describes anything, and this read fails closed rather than letting a stale `verified`
    stand as proof of rows that have since been edited away.

    A finding is held to more than that. It has to still be the same finding at the same
    sequence, because a report cited by a quarantine is the whole reason that quarantine
    holds: a fault silently repaired, moved or reclassified underneath it would otherwise
    keep a hold in place under evidence that no longer means what it says.
    """
    inspection = _inspect_journal(connection, workspace_id=workspace_id, run_id=run_id)
    if outcome == "verified":
        if (
            inspection.first_affected_sequence is not None
            and inspection.first_affected_sequence <= observed_head
        ):
            raise StorageError(
                "a stored integrity report verified a prefix this run no longer shows"
            )
        return
    if (inspection.outcome, inspection.first_affected_sequence) != (
        outcome,
        first_affected_sequence,
    ):
        raise StorageError(
            "a stored integrity report no longer states what this run's journal shows"
        )


def read_journal_quarantine(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> JournalQuarantineProjection:
    """How this Run's quarantine currently stands, from the whole disposition history.

    Holds stack, and each release discharges the one still outstanding when it was
    appended -- the latest, since that is the only one it could have been about. A Run
    whose journal was verified twice and found faulty twice holds two quarantines citing
    two distinct integrity reports; the first release answers the second finding and
    leaves this Run held, citing the first finding's report and event, and the second
    release answers that one. Reading only the latest disposition would let one decision
    silently discharge every fault recorded before it, including ones the decider never
    saw -- and would then cite the discharged hold as the reason this Run is still held.

    Every disposition is verified, not only the latest: contiguous from zero, each row in
    the closed form 0035 requires of its own action, each scalar spelled as its column
    stores it, the event it names still present in this Run's journal, and -- for every
    held one -- the integrity report it cites still readable and still a finding at the
    sequence it named. A release is a decision appended on top of that history rather
    than a replacement for it, so a latest row saying `released` must never be what stops
    an earlier quarantine, or the report that quarantine stands on, from being checked. A
    release with no hold left to discharge, or one citing an event that is not the hold it
    stands on, is a history 0035 refuses to write and this read refuses to believe. A Run
    with no disposition at all is not held, which is a different fact from one that was
    released.

    Fail-closed has a cost, and it is stated rather than hidden: verifying every held
    disposition's citation re-reads and re-addresses this Run's whole journal once per
    hold, so this read is O(holds * journal length * event size). Journals are bounded by
    a Run's transitions and holds are rare, and making it cheaper is a measured
    performance lane's work, not something to trade the verification for here.
    """
    rows = connection.execute(_QUARANTINE_QUERY, (workspace_id, run_id)).fetchall()
    latest: tuple[Any, ...] | None = None
    outstanding: list[tuple[Any, ...]] = []
    decisions: set[str] = set()
    for position, row in enumerate(rows):
        (
            disposition_sequence,
            event_id,
            action,
            integrity_report_id,
            diagnostic,
            deciding_actor,
            reason,
            decision_id,
            recorded_at_us,
            held_event_id,
        ) = row
        if disposition_sequence != position:
            raise StorageError(
                "a stored quarantine history is not contiguous from zero"
            )
        if action not in JOURNAL_QUARANTINE_ACTIONS:
            raise StorageError(
                "a stored quarantine disposition names an unknown action"
            )
        held = action == "quarantined"
        in_form = (
            (
                integrity_report_id is not None
                and diagnostic == "RT_JOURNAL_QUARANTINED"
                and deciding_actor is None
                and reason is None
                and decision_id is None
            )
            if held
            else (
                integrity_report_id is None
                and diagnostic is None
                and bool(deciding_actor)
                and bool(reason)
                and bool(decision_id)
            )
        )
        if not in_form:
            raise StorageError(
                "a stored quarantine disposition is not in the form its action requires"
            )
        _counted(recorded_at_us, "quarantine disposition instant", minimum=1)
        if not held:
            _spelled(_IDENTIFIER, deciding_actor, "quarantine deciding actor")
            _spelled(_REASON, reason, "quarantine release reason")
            _spelled(_IDENTIFIER, decision_id, "quarantine release decision")
            if decision_id in decisions:
                # The unique index below 0035 refuses this, so reaching it means the
                # history was written by something that bypassed the schema. Fail closed
                # rather than believe a run whose releases cannot be told apart.
                raise StorageError(
                    "a stored quarantine history records one release decision twice"
                )
            decisions.add(str(decision_id))
        if event_id is not None:
            _spelled(_IDENTIFIER, event_id, "quarantine event citation")
        if held_event_id != event_id:
            raise StorageError(
                "a stored quarantine disposition names an event its run no longer holds"
            )
        if held:
            _cited_finding(
                connection,
                workspace_id=workspace_id,
                run_id=run_id,
                report_id=str(integrity_report_id),
                event_id=event_id,
            )
            outstanding.append(row)
        elif not outstanding:
            raise StorageError(
                "a stored quarantine history releases a hold it does not hold"
            )
        elif outstanding.pop()[1] != event_id:
            raise StorageError(
                "a stored quarantine release does not cite the hold it discharges"
            )
        latest = row
    if latest is None:
        return JournalQuarantineProjection(
            run_id, False, None, None, None, None, None, -1
        )
    # The hold still outstanding while there is one -- the latest, since each release
    # discharged the latest standing when it was appended -- and the decision that
    # discharged the last of them once there is not. `disposition_sequence` stays the
    # whole history's, because that is what the next appended disposition has to follow.
    current = outstanding[-1] if outstanding else latest
    (
        _disposition_sequence,
        event_id,
        _action,
        integrity_report_id,
        diagnostic,
        deciding_actor,
        reason,
        _decision_id,
        _recorded_at_us,
        _held_event_id,
    ) = current
    return JournalQuarantineProjection(
        run_id,
        bool(outstanding),
        diagnostic,
        None if event_id is None else str(event_id),
        integrity_report_id,
        deciding_actor,
        reason,
        int(latest[0]),
    )


def _cited_finding(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    report_id: str,
    event_id: object,
) -> None:
    """The report one held disposition stands on, read and held to what it has to say.

    A quarantine is only as good as its citation, so the report is read whole -- bytes,
    address, shape, columns, and the live journal it describes -- and then held to the
    two things this disposition claims of it. It has to have found something, because a
    hold citing a pass that found nothing is a hold nobody recorded; and a disposition
    that names no event at all has to be citing the one fault that leaves none to name,
    which is a `sequence_gap`. An `integrity_failure` is about a row that is still there,
    so it always names it.
    """
    report = read_journal_integrity_report(
        connection, workspace_id=workspace_id, run_id=run_id, report_id=report_id
    )
    if report is None:
        raise StorageError(
            "a held quarantine cites an integrity report that is no longer there"
        )
    if report.outcome == "verified":
        raise StorageError(
            "a held quarantine cites an integrity report that found nothing"
        )
    if event_id is None and report.outcome != "sequence_gap":
        raise StorageError("a held quarantine names no event and cites no sequence gap")


def read_journal_retention_posture(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> JournalRetentionPosture:
    """Every retention boundary this Run holds, and what they leave of its resumability.

    Each row is re-checked against the rules it was stored under -- a range is both ends
    or neither and never reversed, a range means not resumable, the flag is a boolean,
    and the Audit record it names still belongs to this workspace -- so a file edited
    outside this database fails closed rather than reading as a Run that may resume.
    """
    boundaries: list[RetentionBoundaryRecord] = []
    resumable = True
    blocking: str | None = None
    for row in connection.execute(_RETENTION_QUERY, (workspace_id, run_id)):
        (
            boundary_id,
            first_removed_sequence,
            last_removed_sequence,
            resumable_after,
            policy_ref,
            evidence_ref,
            audit_ref,
            recorded_at_us,
            held_audit_ref,
        ) = row
        if not isinstance(resumable_after, int) or resumable_after not in (0, 1):
            raise StorageError(
                "a stored retention boundary does not state whether its run may resume"
            )
        _spelled(_IDENTIFIER, boundary_id, "retention boundary identifier")
        _spelled(_IDENTIFIER, policy_ref, "retention boundary policy reference")
        _spelled(_IDENTIFIER, evidence_ref, "retention boundary evidence reference")
        _spelled(_IDENTIFIER, audit_ref, "retention boundary audit reference")
        _counted(recorded_at_us, "retention boundary instant", minimum=1)
        for sequence in (first_removed_sequence, last_removed_sequence):
            if sequence is not None:
                _counted(sequence, "retention boundary sequence", minimum=0)
        if (first_removed_sequence is None) != (last_removed_sequence is None) or (
            first_removed_sequence is not None
            and (first_removed_sequence > last_removed_sequence or resumable_after != 0)
        ):
            raise StorageError(
                "a stored retention boundary does not state a removable range"
            )
        if held_audit_ref != audit_ref:
            raise StorageError(
                "a stored retention boundary names an audit record its workspace does "
                "not hold"
            )
        boundaries.append(
            RetentionBoundaryRecord(
                str(boundary_id),
                run_id,
                first_removed_sequence,
                last_removed_sequence,
                bool(resumable_after),
                str(policy_ref),
                str(evidence_ref),
                str(audit_ref),
                int(recorded_at_us),
            )
        )
        if not resumable_after and resumable:
            resumable = False
            blocking = str(boundary_id)
    return JournalRetentionPosture(run_id, resumable, blocking, tuple(boundaries))


def evaluate_journal_resume(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> JournalResumeEligibility:
    """Whether this Run may resume, and the one typed reason it may not.

    Two separate facts, checked in this order. A held quarantine refuses with
    `RT_JOURNAL_QUARANTINED`, because an unverifiable journal is a stronger statement
    about the same Run than a policy boundary is. A Run rendered non-resumable by a
    recorded retention boundary refuses with `RT_JOURNAL_RETENTION_BOUNDARY`.

    Read-only, in every sense: no public Run state moves, no history is skipped, folded,
    inferred, reconstructed or deleted, and either projection failing closed on a
    tampered file propagates rather than reading as an allow.
    """
    quarantine = read_journal_quarantine(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    if quarantine.held:
        return JournalResumeEligibility(
            run_id,
            False,
            "RT_JOURNAL_QUARANTINED",
            quarantine.integrity_report_id,
            None,
        )
    posture = read_journal_retention_posture(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    if not posture.resumable:
        return JournalResumeEligibility(
            run_id,
            False,
            "RT_JOURNAL_RETENTION_BOUNDARY",
            None,
            posture.blocking_boundary_id,
        )
    return JournalResumeEligibility(run_id, True, None, None, None)


__all__ = [
    "JOURNAL_GOVERNANCE_DIAGNOSTICS",
    "JOURNAL_INTEGRITY_DIAGNOSTICS",
    "JOURNAL_INTEGRITY_OUTCOMES",
    "JOURNAL_QUARANTINE_ACTIONS",
    "JOURNAL_RESUME_DIAGNOSTICS",
    "TRANSITION_BUNDLE_DIAGNOSTICS",
    "TRANSITION_BUNDLE_DISPOSITIONS",
    "TRANSITION_PARITY_STATUSES",
    "TRANSITION_ROLLOUT_STAGES",
    "BoundRunAdmission",
    "JournalGovernanceRefused",
    "JournalQuarantineProjection",
    "JournalResumeEligibility",
    "JournalRetentionPosture",
    "JournalVerification",
    "ParityPromotionEligibility",
    "RetentionBoundaryRecord",
    "StoredJournalEvent",
    "StoredJournalIntegrityReport",
    "StoredRuntimeDefinitionBinding",
    "StoredTransitionBundle",
    "StoredTransitionParityReport",
    "TransitionBundleOutcome",
    "TransitionBundleRefused",
    "WorkflowBindingWriter",
    "WorkflowJournalGovernanceWriter",
    "WorkflowTransitionWriter",
    "admit_bound_run",
    "apply_transition_bundle",
    "bound_material",
    "evaluate_binding_resume",
    "evaluate_journal_resume",
    "evaluate_parity_promotion",
    "journal_genesis_link",
    "read_journal_integrity_report",
    "read_journal_quarantine",
    "read_journal_retention_posture",
    "read_runtime_definition_binding",
    "read_runtime_definition_binding_projection",
    "read_runtime_journal_events",
    "read_transition_bundle",
    "read_transition_parity_report",
    "reconcile_binding_decision",
    "record_retention_boundary",
    "record_transition_parity_report",
    "release_journal_quarantine",
    "transaction_local_binding_writer",
    "transaction_local_journal_governance_writer",
    "transaction_local_transition_writer",
    "verify_journal_integrity",
    "workflow_binding_writer",
    "workflow_journal_governance_writer",
    "workflow_transition_writer",
]
