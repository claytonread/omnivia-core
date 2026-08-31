"""T-0688 IP-06: the Workflow Run definition-binding repository (migration 0035).

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
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

_RUNS: Final = "omnivia_workflow_runs"
_PLANS: Final = "omnivia_workflow_plans"
_BINDINGS: Final = "omnivia_workflow_runtime_bindings"

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


def _instant_us(value: str) -> int:
    """The public `boundAt` timestamp as the exact microsecond column it is stored in.

    Two ways this conversion can be inexact, and both fail closed rather than storing a
    different instant than the binding names. `Timestamp` allows up to nine fractional
    digits while the column holds microseconds, and `datetime.fromisoformat` truncates
    the excess silently rather than refusing it; and 0035 requires a positive instant,
    which the epoch itself is not.

    Only ever called on a timestamp `validate_runtime_definition_binding` has already
    accepted, on both the write and the read path, so the calendar is known good by the
    time it arrives and the parse itself cannot fail.
    """
    fraction = value.partition(".")[2].removesuffix("Z")
    if any(digit != "0" for digit in fraction[6:]):
        raise StorageError(
            f"a binding instant finer than a microsecond cannot be stored: {value!r}"
        )
    microseconds = (datetime.fromisoformat(value) - _EPOCH) // timedelta(microseconds=1)
    if microseconds <= 0:
        raise StorageError(f"a binding instant is not a storable instant: {value!r}")
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
    text = str(document)
    if not isinstance(digest, str) or _digest(text) != digest:
        raise StorageError(
            "a stored runtime binding does not match its recorded digest"
        )
    if not isinstance(byte_length, int) or byte_length != len(text.encode("utf-8")):
        raise StorageError(
            "a stored runtime binding does not match its recorded byte length"
        )
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise StorageError("a stored runtime binding is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise StorageError("a stored runtime binding is not a JSON object")
    try:
        canonical = canonicalize(decoded)
    except ContractSemanticError as error:
        # Valid JSON is not necessarily canonicalizable: OmniVia admits a strict subset
        # of the JSON value domain, and a document outside it -- an integer that does not
        # round trip through binary64, a lone surrogate -- has no canonical form to
        # compare against at all. Fail closed, like every other read-side disagreement.
        raise StorageError(
            "a stored runtime binding cannot be canonicalized"
        ) from error
    if canonical != text:
        raise StorageError("a stored runtime binding is not canonical JSON")
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


__all__ = [
    "BoundRunAdmission",
    "StoredRuntimeDefinitionBinding",
    "WorkflowBindingWriter",
    "admit_bound_run",
    "evaluate_binding_resume",
    "read_runtime_definition_binding",
    "read_runtime_definition_binding_projection",
    "reconcile_binding_decision",
    "transaction_local_binding_writer",
    "workflow_binding_writer",
]
