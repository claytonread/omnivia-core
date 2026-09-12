"""One governed workspace, created and served the accepted way, for the MCP suite.

**Why this file exists at all.** `test_mcp_stdio_end_to_end` calls the exposed
tools and has to be able to say what each one should come back with. The MCP
distribution creates no workspace and seeds no fact -- it exposes reads and, in
the wider profile, a handful of writes, none of which can bootstrap an
installation -- so the state has to be built by the runtime, in this process,
before the service is started. This module is the only place in this package's
tests that imports `omnivia_core_runtime`, and the MCP server under test never
does: it runs as a separate process reached only over a socket, which is the
arrangement in which "MCP does not import the runtime" is proven rather than
asserted.

**The other caller wants the opposite, and gets it from the same three steps.**
`test_mcp_standalone_authoring_acceptance` runs R004 section 13.B's journey,
which forbids pre-seeded application data of any kind and begins with a host
nobody has configured. `serving(seed=False, configure=False)` is that: the same
registered workspace and the same real service, with the seeding pass and the
MCP provisioning below both skipped, so the only writer that workspace ever has
is the MCP surface under test.

**The workspace is registered, not invented.** It is created by dispatching the
canonical `workspace.create` request through a real
:class:`~omnivia_core_runtime.service.installation_host.InstallationAuthorityCoordinator`
-- the production installation authority, started here in-process and closed
again before anything else runs -- so the workspace this suite serves is one the
installation catalogue actually holds. That matters for more than tidiness:
`mcp.configure` refuses a workspace outside the installation's authorised
inventory, so a workspace merely migrated onto disk could never be given a
dedicated MCP principal, and every managed-local configuration below would have
to name a credential nothing issued.

**The MCP principal is issued by the service, never minted here.** Once the
service is ready, :func:`serving` connects to it as an ordinary client and calls
the public `mcp_configure` local control, exactly as `omnivia mcp configure`
does. The bearer that comes back is written straight into this installation's
:class:`~omnivia_core_client.InstalledCredentialStore` under the reference the
service chose, and dropped. What the yielded :class:`GovernedService` carries is
the redacted half of that setup -- the reference, the principal and the profile
-- which is all a trusted configuration document is allowed to hold.

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
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from omnivia_core_client import (
    Credential,
    CredentialReference,
    Deadline,
    InstallationServiceConfig,
    InstalledCredentialStore,
    McpSetupView,
    ServiceClient,
    local_control_transport,
    mcp_configure,
)
from omnivia_core_runtime.ownership.discovery import discover
from omnivia_core_runtime.ownership.fencing import fenced_transaction, open_guard
from omnivia_core_runtime.ownership.identity import (
    ProcessEvidence,
    ServiceInstanceIdentity,
    SystemClock,
)
from omnivia_core_runtime.ownership.lease import LeaseRecord, acquire_lease, read_lease
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.installation import WORKSPACE_CREATE_OPERATION
from omnivia_core_runtime.service.installation_host import (
    InstallationAuthorityCoordinator,
)
from omnivia_core_runtime.service.main import (
    LOCAL_PRINCIPAL,
    WORKSPACE_STORAGE_DIRECTORY,
)
from omnivia_core_runtime.service.mutation import WORKSPACE_ADMINISTRATION_PURPOSE
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.probes import ServiceFacts
from omnivia_core_runtime.service.transport import endpoint_for_path
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import read_manifest

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    ServiceEndpointDescriptor,
    SuccessResponseEnvelope,
    WorkspaceCreateResult,
    WorkspaceDescriptor,
    get_operation_metadata,
)

#: The display name `workspace.create` is asked for, and the one
#: `workspace.inspect` must answer with. The workspace *identifier* is not a
#: constant any more and must not become one: the installation mints it, and a
#: fixture that pinned it would be asserting against a value nothing registered.
WORKSPACE_NAME = "MCP end to end"

#: The MCP host this installation is configured for. One of the service's own
#: closed `McpHost` words -- there is no third, and a name outside that
#: vocabulary is refused before a principal is minted.
MCP_HOST = "claude-code"

#: The two profiles `mcp.configure` knows, spelled as the service's own
#: `McpProfile` spells them. `serving()` asks for the restricted one unless a
#: caller says otherwise: the restricted six are what every read-side test in
#: the suite expects, and the wider profile is an explicit act here for the same
#: reason it is one in production -- authoring intent is recorded, never
#: inferred.
RESTRICTED_PROFILE = "restricted"
AUTHORING_PROFILE = "authoring"

#: The whole budget for one local control, dialling included. Generous: these
#: run against a service that has only just reported ready, and a fixture that
#: failed on a slow host would look like a broken authority rather than a busy
#: one.
_CONTROL_TIMEOUT_SECONDS = 60.0

SERVICE_INSTANCE = "svc-mcp-1"
SEED_INSTANCE = "svc-mcp-seed-1"
CREATE_INSTANCE = "svc-mcp-create-1"

#: What the seeding pass names itself as in the workspace lease row. A workspace
#: fact rather than an installation one -- the catalogue's own installation id is
#: minted by the store and never appears here -- and the service replaces the
#: whole row at the next acquisition anyway.
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
    """A registered, seeded workspace on disk, and what a caller needs to serve it.

    `created_at` is the instant the manifest on disk stores -- the offset form
    `datetime.now(UTC).isoformat()` writes, which is what the production
    bootstrap puts in every real workspace's `workspace.json`.
    `created_at_canonical` is the Application Contract's own spelling of the same
    instant, read off the `workspace.create` answer. Both are carried rather than
    one derived from the other: `workspace.inspect` must canonicalize at the
    application boundary, and an assertion against a single value would follow
    the manifest wherever it was respelled and stop being an assertion about the
    wire.
    """

    workspace: WorkspaceLayout
    installation: InstallationLayout
    workspace_id: str
    created_at: str
    created_at_canonical: str


@dataclass(frozen=True)
class _Holder:
    """The authority tuple every fenced write below is validated against."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int
    workspace_id: str


def _insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def _take_ownership(path: Path, workspace_id: str) -> _Holder:
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
        workspace_id=workspace_id,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=SystemClock(),
        workspace_id=workspace_id,
        fencing_generation=lease.fencing_generation,
    )
    return _Holder(connection, identity, lease.fencing_generation, workspace_id)


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
        workspace_id=holder.workspace_id,
        fencing_generation=holder.generation,
    ):
        _insert(
            holder.connection,
            BLOBS,
            {
                "workspace_id": holder.workspace_id,
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
                "workspace_id": holder.workspace_id,
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
                "workspace_id": holder.workspace_id,
                "source_kind": "filesystem.archive",
                "declared_checksum": DIGEST_A,
                "content_length_bytes": 1024,
                "media_type": "application/zip",
                "computed_checksum": DIGEST_A,
                "original_metadata_json": '{"kind":"archive"}',
                "original_metadata_digest": DIGEST_C,
                "staging_outcome": "verified",
                "blob_workspace_id": holder.workspace_id,
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
                "workspace_id": holder.workspace_id,
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
                "workspace_id": holder.workspace_id,
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

    **No permission label is attached, and that absence is the point.** The
    dedicated MCP principal `mcp.configure` mints holds an empty evidence-label
    grant -- configuring a host grants no extra read authority, which is the
    whole of what makes it safe to hand to one -- so a labelled artifact would be
    filtered out of every answer this suite asks about, and the only way to see
    it would be to widen that principal. Label-based denial is the runtime ACL
    suites' subject, not this one's; what these tests have to be able to observe
    is that a legitimately readable artifact reaches the MCP client unchanged.
    """
    _insert(
        holder.connection,
        EVIDENCE,
        {
            "evidence_id": evidence_id,
            "workspace_id": holder.workspace_id,
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
            "workspace_id": holder.workspace_id,
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
            "workspace_id": holder.workspace_id,
            "reference_ordinal": 1,
            "source_kind": "filesystem.archive",
            "source_native_id": native_id,
            "span_pointer": "/body/0",
            "span_start_offset": 0,
            "span_end_offset": 10,
        },
    )


# --- L2 governed truth ---------------------------------------------------------


def _audit_row(workspace_id: str, audit_ref: str) -> dict[str, object]:
    """The M1 audit event one sealed lineage correlates to.

    0009's requirement rather than this file's: the seal trigger refuses an
    assembly whose correlation names no audit row.

    `workspace_id` is threaded through every row builder below rather than read
    off a module constant: the workspace is minted by the installation at
    creation time, so there is no identifier this file could have known in
    advance.
    """
    return {
        "audit_ref": audit_ref,
        "workspace_id": workspace_id,
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
    workspace_id: str,
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
        "workspace_id": workspace_id,
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
    workspace_id: str,
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
        "workspace_id": workspace_id,
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


def _link_row(
    workspace_id: str, event_id: str, assembly_id: str, ordinal: int = 1
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "assembly_id": assembly_id,
        "provenance_event_id": event_id,
        "link_ordinal": ordinal,
        "evidence_id": "evd-0001",
        "normalized_record_id": "nrc-0001",
        "normalized_span_id": "nsp-0001",
        "recorded_at_us": BASE_US + ordinal + 10,
    }


def _seal_row(
    workspace_id: str,
    assembly_id: str,
    version_id: str,
    *,
    audit_ref: str,
    sealed_at_us: int,
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
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
                "workspace_id": holder.workspace_id,
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
        workspace_id=holder.workspace_id,
        fencing_generation=holder.generation,
    ):
        _insert(
            holder.connection,
            GOVERNED_RECORDS,
            {
                "workspace_id": holder.workspace_id,
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
                holder.workspace_id,
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
                holder.workspace_id,
                proposed,
                candidate_assembly,
                candidate_version,
                "candidate.human_proposed",
                audit_ref=audit_ref,
            ),
        )
        _insert(
            holder.connection,
            EVIDENCE_LINKS,
            _link_row(holder.workspace_id, proposed, candidate_assembly),
        )
        if endpoint is not None:
            asserted = f"ev-{candidate_assembly}-asserted"
            _insert(
                holder.connection,
                GOVERNED_EVENTS,
                _event_row(
                    holder.workspace_id,
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
                _link_row(holder.workspace_id, asserted, candidate_assembly, 2),
            )
            write_endpoint(candidate_assembly, asserted)
        _insert(
            holder.connection,
            SEALS,
            _seal_row(
                holder.workspace_id,
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
                holder.workspace_id,
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
                holder.workspace_id,
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
            holder.connection,
            EVIDENCE_LINKS,
            _link_row(holder.workspace_id, accepted, governed_assembly),
        )
        if endpoint is not None:
            asserted = f"ev-{governed_assembly}-asserted"
            _insert(
                holder.connection,
                GOVERNED_EVENTS,
                _event_row(
                    holder.workspace_id,
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
                _link_row(holder.workspace_id, asserted, governed_assembly, 2),
            )
            write_endpoint(governed_assembly, asserted)
        _insert(
            holder.connection,
            SEALS,
            _seal_row(
                holder.workspace_id,
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
        workspace_id=holder.workspace_id,
        fencing_generation=holder.generation,
    ):
        for number in range(1, 4):
            _insert(
                holder.connection,
                AUDIT_EVENTS,
                _audit_row(holder.workspace_id, f"audit-{number}"),
            )

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


def _create_workspace(
    installation_root: Path, storage_root: Path
) -> WorkspaceDescriptor:
    """Create one registered workspace through the real installation authority.

    The production path, in this process and nothing simulated in it: the
    coordinator opens (and, first time, creates) the machine-local catalogue,
    takes its lifetime lock, elects itself the owner and serves the installation
    application surface, and the canonical `workspace.create` request below is
    dispatched through that surface. So the identifier, the target directory, the
    durable allocation claim, the bootstrap and the catalogue row are all the
    installation's own -- which is what later lets `mcp.configure` find this
    workspace in the authorised inventory it refuses to configure outside of.

    The coordinator is closed before this returns. It holds the catalogue lock
    for as long as it is open, and the service the caller is about to start has
    to be able to win that same election for itself.
    """
    coordinator = InstallationAuthorityCoordinator(
        installation_root=installation_root,
        workspace_storage_root=storage_root,
        core_version="0.1.0",
        clock=SystemClock(),
        owner_instance_id=CREATE_INSTANCE,
        principal_id=LOCAL_PRINCIPAL,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset(),
                operations=frozenset(SERVICE_OPERATIONS),
            )
        ),
        facts=SimpleNamespace(
            probe_facts=lambda: ServiceFacts(
                observed_at="2026-08-12T00:00:00Z",
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            )
        ),
    )
    try:
        entry = get_operation_metadata(WORKSPACE_CREATE_OPERATION)
        request_id = "req-mcp-fixture-create"
        response = coordinator.start().dispatch(
            RequestEnvelope(
                operation=WORKSPACE_CREATE_OPERATION,
                metadata=RequestMetadata(
                    request_id=request_id,
                    correlation_id=request_id,
                    trace_id=request_id,
                    api_version=CONTRACT_VERSION,
                    client=ClientIdentity(id="mcp-fixture", version="0.1.0"),
                    scopes=tuple(entry.scope.required_scopes),
                    purpose=WORKSPACE_ADMINISTRATION_PURPOSE,
                    idempotency_key="mcp-fixture-create-001",
                    required_capabilities=(
                        CapabilityRequirement(
                            id=entry.required_capability.id,
                            minimum_version=entry.required_capability.minimum_version,
                            required=True,
                        ),
                    ),
                ),
                input={"display_name": WORKSPACE_NAME},
            )
        )
    finally:
        coordinator.close()
    assert isinstance(response, SuccessResponseEnvelope), response
    return WorkspaceCreateResult.from_wire(response.result).workspace


def build(root: Path, *, seed: bool = True) -> GovernedWorkspace:
    """Create a registered workspace under `root` and seed it, then hand it back closed.

    The layout is the managed-local convention the service itself assumes:
    `installation-state/` is the trusted root, and a server-minted workspace
    lands under its sibling `workspaces/` -- which is exactly where
    `service.main` points its own installation authority, so the service the
    caller starts next joins the installation this workspace was created in
    rather than a second one beside it.

    Seeding happens here, while the workspace is offline and this process is its
    only writer, and the connection is closed before this returns: the workspace
    has exactly one exclusive writer, and the next one is that service.

    `seed=False` skips that pass entirely and returns the workspace exactly as
    the installation bootstrap left it: migrated, registered, and holding no
    evidence, no governed record and no job. That is the only state R004 section
    13.B's standalone journey may start from -- it forbids pre-seeding
    application data through any path at all -- so the flag is the whole of how
    this file stays out of that journey's way.
    """
    installation = InstallationLayout(root=(root / "installation-state").resolve())
    descriptor = _create_workspace(
        installation.root, (root / WORKSPACE_STORAGE_DIRECTORY).resolve()
    )
    workspace = WorkspaceLayout(
        root=(root / WORKSPACE_STORAGE_DIRECTORY / descriptor.workspace_id).resolve()
    )

    if seed:
        holder = _take_ownership(workspace.database_path, descriptor.workspace_id)
        try:
            _seed_evidence_chain(holder)
            _seed_governed_truth(holder)
        finally:
            holder.connection.close()

    return GovernedWorkspace(
        workspace=workspace,
        installation=installation,
        workspace_id=descriptor.workspace_id,
        created_at=read_manifest(workspace).created_at,
        created_at_canonical=descriptor.created_at,
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

    `credential_reference`, `principal_id` and `profile` are the redacted half of
    the setup this service issued for :data:`MCP_HOST` -- the three facts a
    trusted `omnivia.mcp-config.v1` document is built from. **The bearer is not
    here and there is no field it could be in.** It was handed over once, written
    into this installation's protected store, and dropped; what a configuration
    names is the reference, and the server reads the material for itself.

    All three are `None` when :func:`serving` was asked not to configure MCP at
    all. That is not a missing value: it is an installation on which no host has
    been set up, which is where the installed setup command's own journey has to
    begin.
    """

    endpoint_uri: str
    workspace_id: str
    installation_state: Path
    process: subprocess.Popen[bytes]
    log: Path
    database: Path
    #: The instant the workspace manifest on disk stores, and the same instant in
    #: the Application Contract's own spelling, as `workspace.create` answered.
    created_at: str
    created_at_canonical: str
    #: The opaque name the service filed this host's bearer under.
    credential_reference: str | None = None
    #: The dedicated principal that bearer resolves to. Nothing chose it here:
    #: `mcp.configure` mints it inside the write transaction.
    principal_id: str | None = None
    #: The exposure profile the setup records, and therefore the ceiling a
    #: configuration written from it may state.
    profile: str | None = None
    #: The authenticated loopback HTTP endpoint, when :func:`serving` was given
    #: an `http_credential`. `None` otherwise.
    http_endpoint: str | None = None

    def descriptor(self) -> ServiceEndpointDescriptor:
        """The descriptor the service currently publishes for its workspace."""
        found = discover(
            InstallationLayout(root=self.installation_state).runtime_for(
                self.workspace_id
            )
        )
        assert found is not None, f"no published descriptor: {self.diagnosis()}"
        return found

    def stop_and_read_lease(self) -> LeaseRecord:
        """Stop the service, then read the lease row it leaves behind.

        After, not during: the service holds the database in exclusive locking
        mode for its whole life, so no other process can read the row while it
        runs. What the stopped service leaves is still the whole ownership story
        -- the holder's instance, process evidence and fencing generation, which
        `acquire_lease` bumps on every acquisition.
        """
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # A hung service must not outlive the test: kill and reap it, then
                # fail with the original timeout rather than read a lease it never
                # released cleanly.
                self.process.kill()
                self.process.wait(timeout=10)
                raise
        connection = open_database(self.database, OpenMode.READ_ONLY)
        try:
            lease = read_lease(connection)
        finally:
            connection.close()
        assert lease is not None, f"no lease row: {self.diagnosis()}"
        return lease

    def diagnosis(self) -> str:
        """What the service is doing now, and everything it has ever written.

        For an assertion message. A service that stopped says so in its own
        words on the way out -- a lease it could no longer show current, an
        endpoint it refused to bind -- and a service that is still running while
        answering nobody is a different failure with a different cause. Reading
        both here is what makes a hosted failure diagnosable from the log
        instead of only reproducible.
        """
        return _diagnosis(self.process, self.log)


def _diagnosis(process: subprocess.Popen[bytes], log: Path) -> str:
    """:meth:`GovernedService.diagnosis`, before there is one to ask.

    The readiness wait and the provisioning call both happen before the yielded
    value exists, and both want the same sentence when they fail.
    """
    state = (
        "still running"
        if process.poll() is None
        else f"exited with {process.returncode}"
    )
    said = log.read_text(encoding="utf-8", errors="replace")
    return f"the service is {state}; it wrote {said!r}"


#: The read operations the HTTP embedder's session grants: the six the MCP
#: exposure manifest allow-lists, stated here rather than imported so this file
#: stays independent of the package under test.
_HTTP_GRANTED_OPERATIONS = (
    "workspace.inspect",
    "evidence.search",
    "knowledge.search",
    "memory.search",
    "graph.traverse",
    "context_pack.build",
)

#: A test-only embedder of the service's own `main()`. `omnivia-core-service`
#: supplies no credential resolver by design, so authenticated HTTP is reachable
#: only through an embedder that injects one (see `service/main.py`'s `main`).
#: This is the smallest such embedder: it accepts exactly one bearer secret,
#: `argv[1]`, and resolves it to the same `local_owner_session` shape the local
#: socket serves reads under, for the operations above. Everything else -- the
#: workspace, the lease, the router and the listener -- is the production path.
_HTTP_EMBEDDER = """
import sys
from pathlib import Path
from omnivia_core_runtime.ownership.discovery import discover
from omnivia_core_runtime.service.application import local_owner_session
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL, main

secret, runtime, workspace_id, operations, *argv = sys.argv[1:]

def resolve(presented):
    if presented != secret:
        return None
    descriptor = discover(Path(runtime))
    if descriptor is None:
        return None
    return local_owner_session(
        principal_id=LOCAL_PRINCIPAL,
        installation_id=descriptor.installation_id,
        workspace_id=workspace_id,
        operations=frozenset(operations.split(",")),
    )

sys.exit(main(argv, resolve_credential=resolve))
"""


def _free_loopback_port() -> int:
    """A port the kernel just handed out on 127.0.0.1, released for the service.

    The service's own `HttpBind` accepts port 0 but publishes no HTTP URL, so a
    caller that has to dial it must choose the port first.
    """
    # ponytail: another process can take the port between this release and the
    # service's bind; the service then exits and `serving` fails with its log.
    # Low-risk and test-only; a retry would mean relaunching on a fresh workspace.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _provision_mcp_principal(
    installation_state: Path, workspace_id: str, profile: str
) -> McpSetupView:
    """Ask the live service for this host's dedicated MCP principal, and file it.

    The production sequence, in the production order, through the public client:
    connect to the running service, call the `mcp.configure` local control, and
    write the bearer it hands back straight into this installation's
    :class:`~omnivia_core_client.InstalledCredentialStore` under the reference
    the service chose. Exactly what `omnivia mcp configure` does, minus the
    host-native snippet and the compensation ladder, neither of which a fixture
    has anything to do with.

    **The bearer is never returned, printed or kept.** It exists as a local name
    for the two statements between receiving it and storing it, and what goes
    back to the caller is the redacted setup view -- which has no field a secret
    could be in.

    Only the freshly provisioned branch is admitted: this runs once per service,
    against an installation created moments earlier, so a configure that found
    the requested state already live would mean something else had configured
    this host and there would be no material to file.
    """
    client = ServiceClient.connect(
        InstallationServiceConfig(
            installation_state=installation_state, workspace_id=workspace_id
        ),
        deadline=Deadline.after(_CONTROL_TIMEOUT_SECONDS),
    )
    result = mcp_configure(
        local_control_transport(client),
        host=MCP_HOST,
        workspace_id=workspace_id,
        profile=profile,
        # Derived from the profile rather than taken separately, for the reason
        # `omnivia mcp configure` derives it: choosing the authoring profile *is*
        # the explicit act, and the store refuses the two disagreeing anyway.
        authoring_intent=profile == AUTHORING_PROFILE,
        deadline=Deadline.after(_CONTROL_TIMEOUT_SECONDS),
    )
    secret = result.reveal()
    assert result.rotated and secret is not None, "configure minted no credential"
    InstalledCredentialStore(installation_state).store(
        CredentialReference(result.setup.credential_reference), Credential(secret)
    )
    del secret
    return result.setup


@contextmanager
def serving(
    *,
    http_credential: str | None = None,
    profile: str = RESTRICTED_PROFILE,
    seed: bool = True,
    configure: bool = True,
) -> Iterator[GovernedService]:
    """Create and seed a governed workspace, serve it, provision MCP, tear down.

    The service is the workspace's exclusive writer from here on, and it is the
    authoritative answerer for every call made against the yielded endpoint:
    nothing in this module answers a request, and no MCP-side double exists to.
    `serve` also builds and activates the `evidence.search` FTS projection before
    the endpoint binds, which is why seeding writes rows and not a projection.

    **The service owns the installation authority for the whole yielded
    lifetime.** Its normal startup elects itself owner of the catalogue this
    workspace was created in -- the temporary coordinator in :func:`build` is
    long closed by then -- so the `mcp.configure` call below is answered by the
    live authoritative process, and so is every authentication of the bearer it
    issues.

    `profile` is what that setup records: `restricted` by default, which is the
    six every read-side test expects, and `authoring` for the one test that
    needs the wider surface.

    `seed=False` serves the workspace exactly as the installation bootstrap left
    it, and `configure=False` provisions no MCP principal at all. Together they
    are the starting state R004 section 13.B requires -- an empty workspace on an
    installation where no host is set up -- so a caller can run the real
    `omnivia mcp configure` for itself and have that command be the thing under
    test rather than a step this fixture already took.

    With `http_credential`, the same process also serves authenticated HTTP on a
    loopback port through :data:`_HTTP_EMBEDDER`, so one service -- one lease,
    one workspace state -- answers both the local socket and HTTP.
    """
    root = Path(tempfile.mkdtemp(prefix="ovm-workspace-"))
    # Outside `tmp_path`: R004-15 caps a local endpoint at 86 encoded bytes and
    # pytest's `tmp_path` nests deep enough to exceed it.
    socket_directory = Path(tempfile.mkdtemp(prefix="ovm-", dir=tempfile.gettempdir()))
    built = build(root, seed=seed)
    endpoint = endpoint_for_path(socket_directory / "s.sock")
    service_argv = [
        "--workspace",
        str(built.workspace.root),
        "--installation-state",
        str(built.installation.root),
        "--endpoint",
        endpoint.url,
    ]
    http_endpoint = None
    if http_credential is None:
        command = [sys.executable, "-m", "omnivia_core_runtime.service.main"]
    else:
        http_endpoint = f"http://127.0.0.1:{_free_loopback_port()}"
        service_argv += ["--http-endpoint", http_endpoint]
        command = [
            sys.executable,
            "-c",
            _HTTP_EMBEDDER,
            http_credential,
            str(built.installation.runtime_for(built.workspace_id)),
            built.workspace_id,
            ",".join(_HTTP_GRANTED_OPERATIONS),
        ]

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
            [*command, *service_argv],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 60
        found = None
        while time.monotonic() < deadline:
            assert process.poll() is None, (
                "the service exited instead of serving: "
                f"{_diagnosis(process, log_path)}"
            )
            found = discover(built.installation.runtime_for(built.workspace_id))
            if found is not None and found.ready:
                break
            time.sleep(0.05)
        assert found is not None and found.ready, (
            f"the service never became ready: {_diagnosis(process, log_path)}"
        )
        # After readiness, because only the live service can mint authority, and
        # before the yield, because every managed-local configuration below names
        # the reference this returns.
        setup = (
            _provision_mcp_principal(
                built.installation.root, built.workspace_id, profile
            )
            if configure
            else None
        )
        yield GovernedService(
            endpoint_uri=endpoint.url,
            workspace_id=built.workspace_id,
            installation_state=built.installation.root,
            process=process,
            log=log_path,
            database=built.workspace.database_path,
            created_at=built.created_at,
            created_at_canonical=built.created_at_canonical,
            credential_reference=None if setup is None else setup.credential_reference,
            principal_id=None if setup is None else setup.principal_id,
            profile=None if setup is None else setup.profile,
            http_endpoint=http_endpoint,
        )
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
