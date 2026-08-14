"""One governed workspace, written the accepted way, for the MCP end-to-end suite.

**Why this file exists at all.** `test_mcp_stdio_end_to_end` calls six tools and
has to be able to say what each one should come back with. Nothing in the MCP
distribution can create that state -- it is read-only by construction -- so the
state has to be built by the runtime, in this process, before the service is
started. This module is the only place in this package's tests that imports
`omnivia_core_runtime`, and the MCP server under test never does: it runs as a
separate process reached only over a socket, which is the arrangement in which
"MCP does not import the runtime" is proven rather than asserted.

**Every write goes through the accepted fenced writer.** `fenced_transaction`
validates the lease, the generation and the mutation guard on entry and again
immediately before COMMIT, and the 0008/0009 triggers refuse a shape the
accepted writer could not produce -- an artifact with no capture event, a
governed version sealed without its predecessor, a relation whose endpoints are
not sealed governed accepted versions. So a row that lands here is a row the
service itself would have written, and a fixture that drifted from the schema
fails at seeding rather than at assertion time.

The seeded facts, and what each makes checkable:

* `evd-ovmcpseeded-alpha` -- one L0 artifact whose locator carries
  :data:`SEEDED_TOKEN`. The M2 chain's own `evd-0001` does not, so an
  `evidence.search` for that token has exactly one answer.
* `rec-ovmcpseeded-a` and `rec-ovmcpseeded-b` -- two sealed, accepted, canonical
  governed records whose content carries the same token, so `knowledge.search`
  and `memory.search` answer from governed truth rather than from evidence.
* `rel-ovmcpseeded-ab` -- a sealed governed `knowledge.relation` joining the two,
  so `graph.traverse` from `rec-ovmcpseeded-a` reaches `rec-ovmcpseeded-b` across
  a relation this fixture wrote.

The `evidence.search` projection is not built here. `service.main.serve` builds
and materialises it at startup, before the endpoint binds, and letting it do so
is what keeps this fixture a writer of workspace state rather than a second copy
of the service's own maintenance work.
"""

from __future__ import annotations

import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from omnivia_core_runtime.ownership.discovery import discover
from omnivia_core_runtime.ownership.fencing import fenced_transaction, open_guard
from omnivia_core_runtime.ownership.identity import (
    ProcessEvidence,
    ServiceInstanceIdentity,
    SystemClock,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.transport import endpoint_for_path
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

#: The workspace every MCP call in the suite is answered from.
WORKSPACE_ID = "ws-mcp-end-to-end-01"
WORKSPACE_NAME = "MCP end to end"

#: The workspace's creation instant *as the manifest stores it*: the offset form
#: `datetime.now(UTC).isoformat()` writes, which is what `workspace.create` puts in
#: every real workspace's `workspace.json`. The fixture deliberately stores that
#: spelling rather than the canonical one, because a fixture written with a literal
#: `Z` is a workspace this build never creates -- and while it was written that way,
#: the suite could not see that a real installed wheel emits `+00:00` in structured
#: content and is refused by the official client against the advertised
#: `workspace.inspect` output schema, whose `Timestamp` is a pattern over the string.
WORKSPACE_CREATED_AT = "2026-08-07T00:00:00+00:00"

#: The same instant in the Application Contract's own spelling -- what
#: `workspace.inspect` must answer with, having canonicalized the manifest's value at
#: the application boundary. Two constants rather than one on purpose: an assertion
#: against a single constant would follow the manifest wherever it was respelled and
#: would stop being an assertion about the wire.
WORKSPACE_CREATED_AT_CANONICAL = "2026-08-07T00:00:00Z"
SERVICE_INSTANCE = "svc-mcp-1"
SEED_INSTANCE = "svc-mcp-seed-1"
INSTALLATION_ID = "inst-mcp-e2e"

#: The one token every seeded fact carries and nothing else in the workspace
#: does. One token rather than three: the same query then reaches evidence,
#: governed records and the context pack, so a tool that answered from the wrong
#: place would have to answer with the wrong identifiers rather than merely with
#: nothing. `unicode61` splits on the non-alphanumerics around it, so it is one
#: FTS token in the locator below and one substring in the record content.
SEEDED_TOKEN = "ovmcpseeded"

EVIDENCE_ID = f"evd-{SEEDED_TOKEN}-alpha"
EVIDENCE_LOCATOR = f"archive://{SEEDED_TOKEN}-alpha.md"

SOURCE_RECORD_ID = f"rec-{SEEDED_TOKEN}-a"
TARGET_RECORD_ID = f"rec-{SEEDED_TOKEN}-b"
RELATION_RECORD_ID = f"rel-{SEEDED_TOKEN}-ab"


#: `ver-<record id>` for every record this fixture seals, because it seals one
#: governed version per record. A caller naming a start point needs the exact
#: version, so the rule is stated once here rather than spelled out per call.
def version_of(record_id: str) -> str:
    """The one sealed governed version id this fixture writes for a record."""
    return f"ver-{record_id}"


#: `reconciliation.supports` on purpose: 0009 requires canonical source-before-
#: target ordering for every relation type but this one, and this fixture has no
#: reason to also be a demonstration of that ordering rule.
RELATION_TYPE = "reconciliation.supports"

RECORD_TYPE = "knowledge.claim"
RELATION_RECORD_TYPE = "knowledge.relation"
DOMAIN_SCOPE = "product.core"

#: Base instant every seeded row is stamped from. Fixed rather than "now": the
#: 0009 seal trigger compares a version's own instants with its seal's, so the
#: whole lineage has to be orderable by construction.
BASE_US = 1_700_000_000_000_000

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64

# --- table names, as 0008 and 0009 declare them -------------------------------

BLOBS = "omnivia_blob_objects"
INTEGRITY = "omnivia_blob_integrity_events"
STAGED = "omnivia_staged_sources"
EVIDENCE = "omnivia_evidence_artifacts"
LABELS = "omnivia_evidence_permission_labels"
PROVENANCE = "omnivia_evidence_provenance_events"
EVENT_REFERENCES = "omnivia_evidence_event_references"
NORMALIZED_RECORDS = "omnivia_normalized_source_records"
NORMALIZED_SPANS = "omnivia_normalized_source_spans"

AUDIT_EVENTS = "omnivia_application_audit_events"
GOVERNED_RECORDS = "omnivia_governed_records"
ASSEMBLIES = "omnivia_governed_version_assemblies"
GOVERNED_EVENTS = "omnivia_governed_provenance_events"
EVIDENCE_LINKS = "omnivia_governed_version_evidence_links"
RELATION_ENDPOINTS = "omnivia_governed_relation_endpoints"
SEALS = "omnivia_governed_version_seals"


@dataclass(frozen=True)
class GovernedWorkspace:
    """A seeded workspace on disk, and the facts a caller needs to serve it."""

    workspace: WorkspaceLayout
    installation: InstallationLayout
    workspace_id: str


@dataclass(frozen=True)
class _Holder:
    """The authority tuple every fenced write below is validated against."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int


def _insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def _take_ownership(path: Path) -> _Holder:
    """Open the workspace as its owner: lease first, then the mutation guard.

    The same order `ServiceRunner` uses, and for the same reason -- ADR-037
    invariant 17 refuses a generation committed without the storage lock, and
    `open_guard` refuses to install a guard for an identity that does not hold
    the lease. A later `serve` simply acquires the next generation and replaces
    both rows, so this seeding pass leaves nothing behind for it to trip on.
    """
    connection = open_database(path, OpenMode.SERVICE_OWNED)
    identity = ServiceInstanceIdentity(
        service_instance_id=SEED_INSTANCE,
        installation_id=INSTALLATION_ID,
        process=ProcessEvidence(
            pid=1, start_time="0", boot_id="boot-mcp-seed", os_principal="seed"
        ),
    )
    lease = acquire_lease(
        connection,
        identity,
        clock=SystemClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=SystemClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return _Holder(connection, identity, lease.fencing_generation)


# --- L0 evidence ---------------------------------------------------------------


def _seed_evidence_chain(holder: _Holder) -> None:
    """The blob/staging/evidence chain 0009's governed evidence links require.

    0009 links a sealed governed version to `evd-0001`, `nrc-0001` and
    `nsp-0001` by foreign key, so no governed version can be sealed in a
    workspace that does not hold them. This is that chain, written as one unit
    because a repository inserts an artifact and its capture event together.
    """
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        _insert(
            holder.connection,
            BLOBS,
            {
                "workspace_id": WORKSPACE_ID,
                "content_digest": DIGEST_A,
                "content_length_bytes": 1024,
                "created_at_us": BASE_US,
                "verified_at_us": BASE_US + 1,
            },
        )
        _insert(
            holder.connection,
            INTEGRITY,
            {
                "integrity_event_id": "bie-0001",
                "workspace_id": WORKSPACE_ID,
                "content_digest": DIGEST_A,
                "integrity_sequence": 1,
                "outcome": "verified",
                "checked_at_us": BASE_US + 2,
            },
        )
        _insert(
            holder.connection,
            STAGED,
            {
                "staged_source_ref": "stg-0001",
                "workspace_id": WORKSPACE_ID,
                "source_kind": "filesystem.archive",
                "declared_checksum": DIGEST_A,
                "content_length_bytes": 1024,
                "media_type": "application/zip",
                "computed_checksum": DIGEST_A,
                "original_metadata_json": '{"kind":"archive"}',
                "original_metadata_digest": DIGEST_C,
                "staging_outcome": "verified",
                "blob_workspace_id": WORKSPACE_ID,
                "blob_content_digest": DIGEST_A,
                "recorded_at_us": BASE_US + 3,
            },
        )
        _artifact(
            holder,
            "evd-0001",
            native_id="doc-1",
            locator="archive://doc.md",
            at=BASE_US + 4,
        )
        _insert(
            holder.connection,
            NORMALIZED_RECORDS,
            {
                "normalized_record_id": "nrc-0001",
                "evidence_id": "evd-0001",
                "workspace_id": WORKSPACE_ID,
                "evidence_blob_digest": DIGEST_A,
                "record_sequence": 1,
                "record_type": "message",
                "schema_version": "1",
                "content_json": '{"body":"hello"}',
                "content_digest": DIGEST_D,
                "parser_id": "parser.markdown",
                "parser_version": "1.0.0",
                "recorded_at_us": BASE_US + 8,
            },
        )
        _insert(
            holder.connection,
            NORMALIZED_SPANS,
            {
                "normalized_span_id": "nsp-0001",
                "normalized_record_id": "nrc-0001",
                "evidence_id": "evd-0001",
                "workspace_id": WORKSPACE_ID,
                "span_sequence": 1,
                "span_kind": "byte_range",
                "span_pointer": "/body/0",
                "span_start_offset": 0,
                "span_end_offset": 10,
                "recorded_at_us": BASE_US + 9,
            },
        )
        _artifact(
            holder,
            EVIDENCE_ID,
            native_id=EVIDENCE_ID,
            locator=EVIDENCE_LOCATOR,
            at=BASE_US + 10,
        )


def _artifact(
    holder: _Holder, evidence_id: str, *, native_id: str, locator: str, at: int
) -> None:
    """One artifact and its capture event, inside the caller's fenced transaction.

    The provenance row is not decoration: `validate_evidence_artifact` refuses an
    artifact whose provenance history is empty, so an artifact seeded without one
    could never be returned and every claim about it would be vacuous.
    """
    _insert(
        holder.connection,
        EVIDENCE,
        {
            "evidence_id": evidence_id,
            "workspace_id": WORKSPACE_ID,
            "source_kind": "filesystem.archive",
            "source_native_id": native_id,
            "source_locator": locator,
            "source_retrieved_at_us": BASE_US,
            "event_at_us": BASE_US - 10,
            "observed_at_us": BASE_US - 5,
            "ingested_at_us": at - 1,
            "recorded_at_us": at,
            "content_checksum": DIGEST_A,
            "blob_content_digest": DIGEST_A,
            "media_type": "text/markdown",
            "original_metadata_json": '{"title":"doc"}',
            "original_metadata_digest": DIGEST_C,
            "sensitivity": "internal",
            "parser_status": "parsed",
            "ingestion_status": "ingested",
            "staged_source_ref": "stg-0001",
            "import_run_id": None,
        },
    )
    _insert(
        holder.connection,
        PROVENANCE,
        {
            "provenance_event_id": f"prv-{evidence_id}-1",
            "evidence_id": evidence_id,
            "workspace_id": WORKSPACE_ID,
            "provenance_sequence": 1,
            "actor_id": "actor-1",
            "actor_kind": "service",
            "action": "captured",
            "occurred_at_us": at,
            "source_kind": "filesystem.archive",
            "source_native_id": native_id,
        },
    )
    _insert(
        holder.connection,
        EVENT_REFERENCES,
        {
            "event_reference_id": f"ref-{evidence_id}-1",
            "provenance_event_id": f"prv-{evidence_id}-1",
            "evidence_id": evidence_id,
            "workspace_id": WORKSPACE_ID,
            "reference_ordinal": 1,
            "source_kind": "filesystem.archive",
            "source_native_id": native_id,
            "span_pointer": "/body/0",
            "span_start_offset": 0,
            "span_end_offset": 10,
        },
    )
    _insert(
        holder.connection,
        LABELS,
        {
            "label_event_id": f"lbl-{evidence_id}-1",
            "evidence_id": evidence_id,
            "workspace_id": WORKSPACE_ID,
            "label_sequence": 1,
            "label_action": "attached",
            "permission_label": "group.engineering",
            "recorded_at_us": at,
        },
    )


# --- L2 governed truth ---------------------------------------------------------


def _audit_row(audit_ref: str) -> dict[str, object]:
    """The M1 audit event one sealed lineage correlates to.

    0009's requirement rather than this file's: the seal trigger refuses an
    assembly whose correlation names no audit row.
    """
    return {
        "audit_ref": audit_ref,
        "workspace_id": WORKSPACE_ID,
        "principal_id": "principal-1",
        "operation": "knowledge.record",
        "purpose": "governance",
        "request_id": f"request-{audit_ref}",
        "correlation_id": f"correlation-{audit_ref}",
        "trace_id": f"trace-{audit_ref}",
        "granted_authority_json": '{"roles":["reviewer"]}',
        "outcome_class": "succeeded",
        "recorded_at_us": BASE_US,
    }


def _assembly_row(
    assembly_id: str,
    version_id: str,
    record_id: str,
    *,
    record_type: str,
    audit_ref: str,
    ordinal: int,
    governed: bool,
    content: str,
    digest: str,
    recorded_at_us: int,
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "assembly_id": assembly_id,
        "governed_record_id": record_id,
        "governed_record_version_id": version_id,
        "record_type": record_type,
        "domain_scope": DOMAIN_SCOPE,
        "layer": "governed" if governed else "candidate",
        "authority_level": "canonical" if governed else "proposed",
        "governance_disposition": "accepted" if governed else None,
        "candidate_origin": None if governed else "human_proposed",
        "extraction_kind": None,
        "decision_source_kind": "human_reviewer" if governed else None,
        "decision_source_id": "reviewer-1" if governed else None,
        "authority_policy_id": "authority-rule" if governed else None,
        "authority_policy_version": "1" if governed else None,
        "policy_decision_ref": "decision-ref" if governed else None,
        "content_schema_version": "1.0",
        "content_json": content,
        "content_digest": digest,
        "evidence_disposition": "available",
        "confidence_ppm": 900000,
        "assertion_actor_id": "principal-1",
        "assertion_actor_kind": "human",
        "assertion_actor_role": "author",
        "reason_code": "governance.reviewed" if governed else None,
        "reason_comment": None,
        "valid_from_us": -1,
        "valid_to_us": None,
        "recorded_at_us": recorded_at_us,
        "append_ordinal": ordinal,
        "correlation_kind": "m1_audit",
        "correlation_id": audit_ref,
        "audit_ref": audit_ref,
    }


def _event_row(
    event_id: str,
    assembly_id: str,
    version_id: str,
    action: str,
    *,
    audit_ref: str,
    sequence: int = 1,
    reviewer: bool = False,
    predecessor_record_id: str | None = None,
    predecessor_version_id: str | None = None,
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "provenance_event_id": event_id,
        "assembly_id": assembly_id,
        "governed_record_version_id": version_id,
        "provenance_sequence": sequence,
        "action": action,
        "actor_id": "reviewer-1" if reviewer else "principal-1",
        "actor_kind": "human",
        "actor_role": "reviewer" if reviewer else "author",
        "policy_id": None,
        "policy_version": None,
        "occurred_at_us": BASE_US + sequence,
        "recorded_at_us": BASE_US + sequence + 1,
        "reason_code": "governance.reviewed"
        if action.startswith("governance.")
        else None,
        "reason_comment": None,
        "audit_ref": audit_ref,
        "correlation_kind": "m1_audit",
        "correlation_id": audit_ref,
        "predecessor_record_id": predecessor_record_id,
        "predecessor_version_id": predecessor_version_id,
        "evidence_disposition": "available",
    }


def _link_row(event_id: str, assembly_id: str, ordinal: int = 1) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "assembly_id": assembly_id,
        "provenance_event_id": event_id,
        "link_ordinal": ordinal,
        "evidence_id": "evd-0001",
        "normalized_record_id": "nrc-0001",
        "normalized_span_id": "nsp-0001",
        "recorded_at_us": BASE_US + ordinal + 10,
    }


def _seal_row(
    assembly_id: str, version_id: str, *, audit_ref: str, sealed_at_us: int
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "seal_id": f"seal-{assembly_id}",
        "assembly_id": assembly_id,
        "governed_record_version_id": version_id,
        "correlation_kind": "m1_audit",
        "correlation_id": audit_ref,
        "sealed_at_us": sealed_at_us,
    }


def _seal(
    holder: _Holder,
    *,
    record_id: str,
    audit_ref: str,
    content: str,
    record_type: str,
    recorded_at_us: int,
    endpoint: tuple[str, str] | None = None,
) -> None:
    """One record's whole accepted lineage: a sealed candidate, then the sealed,
    accepted, canonical governed version promoted from it.

    Both layers carry the same content, because that is what a promotion looks
    like. `endpoint` makes this a `knowledge.relation` carrying a relation
    endpoint row on *both* layers, which is what 0009 requires of one.
    """
    candidate_assembly = f"asm-{record_id}-cand"
    candidate_version = f"ver-{record_id}-cand"
    governed_assembly = f"asm-{record_id}"
    governed_version = version_of(record_id)

    def write_endpoint(assembly_id: str, event_id: str) -> None:
        assert endpoint is not None
        source_id, target_id = endpoint
        _insert(
            holder.connection,
            RELATION_ENDPOINTS,
            {
                "workspace_id": WORKSPACE_ID,
                "assembly_id": assembly_id,
                "provenance_event_id": event_id,
                "correlation_kind": "m1_audit",
                "correlation_id": audit_ref,
                "relation_type": RELATION_TYPE,
                "source_record_id": source_id,
                "source_version_id": version_of(source_id),
                "target_record_id": target_id,
                "target_version_id": version_of(target_id),
                "recorded_at_us": recorded_at_us,
            },
        )

    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        _insert(
            holder.connection,
            GOVERNED_RECORDS,
            {
                "workspace_id": WORKSPACE_ID,
                "governed_record_id": record_id,
                "record_type": record_type,
                "domain_scope": DOMAIN_SCOPE,
                "recorded_at_us": BASE_US + 1,
            },
        )

        _insert(
            holder.connection,
            ASSEMBLIES,
            _assembly_row(
                candidate_assembly,
                candidate_version,
                record_id,
                record_type=record_type,
                audit_ref=audit_ref,
                ordinal=1,
                governed=False,
                content=content,
                digest=DIGEST_A,
                recorded_at_us=BASE_US + 2,
            ),
        )
        proposed = f"ev-{candidate_assembly}-proposed"
        _insert(
            holder.connection,
            GOVERNED_EVENTS,
            _event_row(
                proposed,
                candidate_assembly,
                candidate_version,
                "candidate.human_proposed",
                audit_ref=audit_ref,
            ),
        )
        _insert(
            holder.connection, EVIDENCE_LINKS, _link_row(proposed, candidate_assembly)
        )
        if endpoint is not None:
            asserted = f"ev-{candidate_assembly}-asserted"
            _insert(
                holder.connection,
                GOVERNED_EVENTS,
                _event_row(
                    asserted,
                    candidate_assembly,
                    candidate_version,
                    "relation.asserted",
                    audit_ref=audit_ref,
                    sequence=2,
                ),
            )
            _insert(
                holder.connection,
                EVIDENCE_LINKS,
                _link_row(asserted, candidate_assembly, 2),
            )
            write_endpoint(candidate_assembly, asserted)
        _insert(
            holder.connection,
            SEALS,
            _seal_row(
                candidate_assembly,
                candidate_version,
                audit_ref=audit_ref,
                sealed_at_us=BASE_US + 50,
            ),
        )

        _insert(
            holder.connection,
            ASSEMBLIES,
            _assembly_row(
                governed_assembly,
                governed_version,
                record_id,
                record_type=record_type,
                audit_ref=audit_ref,
                ordinal=2,
                governed=True,
                content=content,
                digest=DIGEST_B,
                recorded_at_us=recorded_at_us,
            ),
        )
        accepted = f"ev-{governed_assembly}-accepted"
        _insert(
            holder.connection,
            GOVERNED_EVENTS,
            _event_row(
                accepted,
                governed_assembly,
                governed_version,
                "governance.accepted",
                audit_ref=audit_ref,
                reviewer=True,
                predecessor_record_id=record_id,
                predecessor_version_id=candidate_version,
            ),
        )
        _insert(
            holder.connection, EVIDENCE_LINKS, _link_row(accepted, governed_assembly)
        )
        if endpoint is not None:
            asserted = f"ev-{governed_assembly}-asserted"
            _insert(
                holder.connection,
                GOVERNED_EVENTS,
                _event_row(
                    asserted,
                    governed_assembly,
                    governed_version,
                    "relation.asserted",
                    audit_ref=audit_ref,
                    sequence=2,
                    reviewer=True,
                ),
            )
            _insert(
                holder.connection,
                EVIDENCE_LINKS,
                _link_row(asserted, governed_assembly, 2),
            )
            write_endpoint(governed_assembly, asserted)
        _insert(
            holder.connection,
            SEALS,
            _seal_row(
                governed_assembly,
                governed_version,
                audit_ref=audit_ref,
                sealed_at_us=max(recorded_at_us, BASE_US + 50) + 1,
            ),
        )


def _statement(suffix: str) -> str:
    return json.dumps({"statement": f"{SEEDED_TOKEN} governed statement {suffix}"})


def _seed_governed_truth(holder: _Holder) -> None:
    """Two sealed governed records and the sealed relation that joins them."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        for number in range(1, 4):
            _insert(holder.connection, AUDIT_EVENTS, _audit_row(f"audit-{number}"))

    _seal(
        holder,
        record_id=SOURCE_RECORD_ID,
        audit_ref="audit-1",
        content=_statement("a"),
        record_type=RECORD_TYPE,
        recorded_at_us=BASE_US + 21,
    )
    _seal(
        holder,
        record_id=TARGET_RECORD_ID,
        audit_ref="audit-2",
        content=_statement("b"),
        record_type=RECORD_TYPE,
        recorded_at_us=BASE_US + 22,
    )
    _seal(
        holder,
        record_id=RELATION_RECORD_ID,
        audit_ref="audit-3",
        content=_statement("relation"),
        record_type=RELATION_RECORD_TYPE,
        recorded_at_us=BASE_US + 23,
        endpoint=(SOURCE_RECORD_ID, TARGET_RECORD_ID),
    )


# --- the whole workspace -------------------------------------------------------


def build(root: Path) -> GovernedWorkspace:
    """Migrate a workspace under `root` and seed it, then hand it back closed.

    The connection is closed before this returns: the workspace has exactly one
    exclusive writer, and the next one is the service the caller is about to
    start.
    """
    legacy = root / "legacy" / "source.sqlite"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(legacy)

    workspace = WorkspaceLayout(root=root / "workspace")
    installation = InstallationLayout(root=root / "installation-state")
    installation.create(WORKSPACE_ID)
    migrate_legacy_database(
        legacy,
        workspace,
        installation,
        WorkspaceManifest(
            workspace_id=WORKSPACE_ID,
            created_at=WORKSPACE_CREATED_AT,
            name=WORKSPACE_NAME,
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
        service_instance_id=SERVICE_INSTANCE,
    )

    holder = _take_ownership(workspace.database_path)
    try:
        _seed_evidence_chain(holder)
        _seed_governed_truth(holder)
    finally:
        holder.connection.close()

    return GovernedWorkspace(
        workspace=workspace, installation=installation, workspace_id=WORKSPACE_ID
    )


# --- the service that owns it --------------------------------------------------


@dataclass(frozen=True)
class GovernedService:
    """A running `omnivia-core-service` and the facts a caller needs to reach it.

    Three, and no object with behaviour: starting the service is here rather
    than in the test module because the runtime import has to stay in one file
    -- a test that reached for `endpoint_for_path` or `discover` itself would
    import `omnivia_core_runtime` beside the MCP server under test, and the
    boundary those tests assert would be the fixture's arrangement rather than
    the package's property.

    `installation_state` is the root this service published its descriptor
    under, and it is what a `managed_local` MCP configuration names. The MCP
    server reads that descriptor through `omnivia-core-client`, so the test
    hands over a directory rather than an endpoint: nothing on the MCP side is
    told where the socket is.

    `process` and `log` are here so a failing assertion can say what became of
    the service rather than only that a call did not succeed. Every test in the
    module shares this one process, so "the tool answered with an error" and
    "the service this module started is no longer answering anyone" are the same
    observation over the wire, and only the two fields below can tell them apart.
    """

    endpoint_uri: str
    workspace_id: str
    installation_state: Path
    process: subprocess.Popen[bytes]
    log: Path

    def diagnosis(self) -> str:
        """What the service is doing now, and everything it has ever written.

        For an assertion message. A service that stopped says so in its own
        words on the way out -- a lease it could no longer show current, an
        endpoint it refused to bind -- and a service that is still running while
        answering nobody is a different failure with a different cause. Reading
        both here is what makes a hosted failure diagnosable from the log
        instead of only reproducible.
        """
        state = (
            "still running"
            if self.process.poll() is None
            else f"exited with {self.process.returncode}"
        )
        said = self.log.read_text(encoding="utf-8", errors="replace")
        return f"the service is {state}; it wrote {said!r}"


@contextmanager
def serving() -> Iterator[GovernedService]:
    """Seed a governed workspace, serve it, and tear both down.

    The service is the workspace's exclusive writer from here on, and it is the
    authoritative answerer for every call made against the yielded endpoint:
    nothing in this module answers a request, and no MCP-side double exists to.
    `serve` also builds and activates the `evidence.search` FTS projection before
    the endpoint binds, which is why seeding writes rows and not a projection.
    """
    root = Path(tempfile.mkdtemp(prefix="ovm-workspace-"))
    # Outside `tmp_path`: R004-15 caps a local endpoint at 86 encoded bytes and
    # pytest's `tmp_path` nests deep enough to exceed it.
    socket_directory = Path(tempfile.mkdtemp(prefix="ovm-", dir=tempfile.gettempdir()))
    built = build(root)
    endpoint = endpoint_for_path(socket_directory / "s.sock")

    # A file, not two pipes, and for the same two reasons `managed_start._spawn`
    # gives its own child one. Nothing here reads a pipe: the service outlives
    # every call in the module, so a `PIPE` nobody drains is a write that blocks
    # the whole process once the kernel buffer fills -- and a blocked service
    # still holds the workspace lease and the storage lock, so a later
    # `connect` cannot start a replacement either and spends its entire
    # `MANAGED_START_TIMEOUT_SECONDS` budget failing to. `process.wait()` below
    # is the same hazard at teardown, where the standard library documents it.
    # The second reason is the one that made this failure unreadable in CI: the
    # service's own diagnostic -- the sentence it writes when it stops -- went
    # into a pipe that was closed unread, so a hosted run could say a call had
    # failed and never say why.
    log_path = root / "service.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnivia_core_runtime.service.main",
                "--workspace",
                str(built.workspace.root),
                "--installation-state",
                str(built.installation.root),
                "--endpoint",
                endpoint.url,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    service = GovernedService(
        endpoint.url,
        built.workspace_id,
        built.installation.root,
        process,
        log_path,
    )
    try:
        deadline = time.monotonic() + 60
        found = None
        while time.monotonic() < deadline:
            assert process.poll() is None, (
                f"the service exited instead of serving: {service.diagnosis()}"
            )
            found = discover(built.installation.runtime_for(built.workspace_id))
            if found is not None and found.ready:
                break
            time.sleep(0.05)
        assert found is not None and found.ready, (
            f"the service never became ready: {service.diagnosis()}"
        )
        yield service
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
                process.kill()
                process.wait(timeout=10)
        shutil.rmtree(socket_directory, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
