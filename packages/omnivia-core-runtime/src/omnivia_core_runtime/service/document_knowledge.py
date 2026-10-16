"""Derive source evidence and one proposed candidate per extracted segment.

AT-46 and DOC-19 are one junction. A document that has been captured and
successfully extracted holds, today, two halves that never meet: the capture's
0008 evidence artifact (`source_capture._write_capture`) and the extraction's
0069 segments. Nothing addressed a segment *inside* that artifact, and nothing
turned a segment into a governed record, so "source evidence and any derived
knowledge retain correct attribution, and nothing is auto-approved" had no
subject in this build. This module is that subject, and nothing more.

What one call writes, per qualifying segment, in one fenced transaction:

1. one `omnivia_normalized_source_records` row -- the typed record parsed out of
   this exact evidence artifact, keyed to the artifact *and* the blob digest the
   parse read, so a record cannot be attributed to bytes it did not come from;
2. one `omnivia_normalized_source_spans` row -- where in that evidence the record
   came from, whose `span_pointer` is the segment's own source locator verbatim;
3. one sealed `layer='candidate'` version -- `authority_level='proposed'`, no
   governance disposition, `candidate_origin='extracted'`, correlated to the
   extraction revision, with one `candidate.extracted` event, one extraction
   lineage row and one evidence link naming the artifact, the record and the span.

**The attribution chain is exact and is made of rows, not of conventions.** The
candidate names its evidence link; the link names the evidence id, the normalized
record and the span; the span's pointer is the segment's locator; the record names
the capture's blob digest; the lineage's `extraction_run_id` and the assembly's
`correlation_id` are both the extraction revision id, and the candidate's content
repeats the document id, capture id, extraction revision id, profile digest and
segment ordinal. `storage/governed.py` already projects exactly that back out:
`provenance.sources` is the artifact's own source reference and the event's
`evidence` carries the span pointer, so retrieval needs nothing new.

**Document content stays inert data.** Nothing here reads a segment for meaning.
There is no branch on its text, no evaluation, no template, no tool, no workflow
and no provider: the segment's value is carried whole into `content_json` the way
`legacy_import` carries a legacy note, and a segment that reads as a prompt, an
instruction or a business claim is stored as a JSON string and is therefore a
value. The lineage says so structurally too -- `extraction_kind='deterministic'`
is the one variant 0009 *forbids* a model, a prompt template and a randomness
seed, so a model-backed derivation could not be recorded through this path at all.

**Nothing here can approve or admit anything.** A candidate is the only thing
written. It carries `authority_level='proposed'` and no `governance_disposition`,
which 0009's own CHECK ties together; it appears under the `candidates` view and
under no other; and the only route to `layer='governed'` is
`storage/governance.apply_governance_transition`, which needs an audit reference
from a governance operation and a sealed candidate predecessor. This module
writes no governed assembly, no supersession, no reviewer and no audit event, and
takes no parameter by which a caller could ask it to.

**Refusals, all fail-closed and all before the first write.** A revision this
workspace does not hold; a revision or capture whose document is not the one
named; a result that is absent or is not `succeeded`; a revision that is not the
document's current extraction; a segment whose source locator is absent, is not a
mapping, names another capture, or is too large to address; and two qualifying
segments sharing one locator. Each is a reason the attribution chain could not be
exact, and an inexact chain is worse than no chain.

Like `legacy_import` and `source_capture` this is a service-owned path, not an
application operation: it runs only while a `ServiceRunner` owns the workspace,
it is in no operation catalogue and on no model-facing surface, and no document
content, path or caller value appears in any refusal. Its one caller is
`document_processing._derive_knowledge`, which runs it for every succeeded,
promoted completion after the result is durable and before the outbox obligation
settles; that function holds the ordering and crash-recovery argument.

Replay is exact. Every identifier is derived from
`(workspace_id, extraction_revision_id, segment_ordinal)`, so a second call for
the same extraction finds its own rows and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import document_intake as store

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    # Deliberately type-checking only. `ServiceRunner` is the whole service, and
    # importing it would put every workflow, provider, agent and chat module in this
    # module's runtime import closure -- which is the boundary DOC-19 rests on and
    # which `test_document_knowledge_derivation` asserts. Nothing below calls a
    # runner method; it reads four attributes a started runner carries.
    from omnivia_core_runtime.service.runner import ServiceRunner

#: The governed domain these candidates belong to. Documents are their own domain:
#: `engineering.codebase` is the repository's, and filing a captured document under
#: it would make one domain's applicability rules answer for another's material.
DOMAIN_SCOPE: Final = "document.intake"

#: The one record type this path writes. A segment is an observation about a source
#: and nothing stronger, and `knowledge.finding` is the catalogue's type for exactly
#: that. Fixed rather than a parameter: choosing the record type from outside would
#: let a caller file inert document text as a `knowledge.decision`.
RECORD_TYPE: Final = "knowledge.finding"
CONTENT_SCHEMA_VERSION: Final = "1.0"

#: The normalized-record type and schema for one extracted segment.
SEGMENT_RECORD_TYPE: Final = "document.extracted_segment"
SEGMENT_RECORD_SCHEMA: Final = "1.0"
SEGMENT_SPAN_KIND: Final = "document_segment"

#: Who derived this: the service, in the one role it has here. Not the document's
#: author, whom no capture claims to know.
_ACTOR: Final = ("core-service", "service", "document_derivation")

#: The component the lineage names as the extractor: this build's document
#: extraction path, which is the thing a later defect would be traced to. Its
#: *version* is the worker build the result recorded, and the exact procedure it ran
#: is the profile's own adapter, recorded as the algorithm.
EXTRACTOR_ID: Final = "omnivia.document-extraction"

#: 0009's `Identifier` domain, applied here rather than left to the database. The
#: adapter identity and the worker build travel from a stored profile document and a
#: stored result, so they are values this path validates rather than trusts -- the
#: raster decoder builds, for one, carry `+` and are deliberately never used here.
_IDENTIFIER: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

#: 0009 bounds `span_pointer` at 2048 characters. A locator that cannot be written
#: into the span is a locator this path cannot address, and it is refused by name
#: rather than left to the database to reject mid-transaction.
MAX_SPAN_POINTER: Final = 2048

#: What one candidate's canonical content may occupy. 0009 permits a mebibyte; this
#: is the share of it one segment gets, so a single pathological segment cannot
#: become a megabyte of governed content.
MAX_CONTENT_BYTES: Final = 256 * 1024

#: The roles derived knowledge is drawn from by default: the document's own readable
#: content. `store.DEFAULT_RETRIEVAL_ROLES` is reused rather than restated, which
#: leaves comments, notes, hidden content, deleted text, field instructions and
#: object inventories out -- the roles whose whole point is that they are *not* the
#: document's prose, and the ones a prompt would most likely be hidden in.
DEFAULT_ROLES: Final = store.DEFAULT_RETRIEVAL_ROLES


class DocumentKnowledgeRefused(RuntimeError):
    """The attribution chain could not be made exact. Fixed text, nothing chained."""


@dataclass(frozen=True, slots=True)
class DerivedSegment:
    """One segment's source-evidence row and the candidate derived from it."""

    segment_ordinal: int
    semantic_role: str
    normalized_record_id: str
    normalized_span_id: str
    span_pointer: str
    governed_record_id: str
    governed_record_version_id: str
    assembly_id: str


@dataclass(frozen=True, slots=True)
class DocumentKnowledgeDerivation:
    """What one extraction's derivation produced, and the lineage it rests on."""

    status: str
    workspace_id: str
    document_id: str
    capture_id: str
    extraction_revision_id: str
    evidence_id: str
    source_digest: str
    profile_digest: str
    derived: tuple[DerivedSegment, ...]


@dataclass(frozen=True, slots=True)
class _Lineage:
    """The verified parent chain one derivation writes against."""

    workspace_id: str
    capture: store.StoredCapture
    revision: store.StoredRevision
    result: store.StoredResult
    #: The deterministic procedure the profile pins, and its version.
    algorithm_id: str
    algorithm_version: str
    #: The worker build the result recorded, as the extractor's version.
    extractor_version: str


@dataclass(frozen=True, slots=True)
class _Planned:
    """One qualifying segment with its derived identifiers resolved."""

    segment: store.StoredSegment
    locator: Mapping[str, Any]
    pointer: str
    normalized_record_id: str
    normalized_span_id: str
    governed_record_id: str
    governed_record_version_id: str
    assembly_id: str
    provenance_event_id: str
    seal_id: str
    content_json: str
    content_digest: str
    record_content_json: str
    record_content_digest: str


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _derived_id(prefix: str, workspace_id: str, revision_id: str, ordinal: int) -> str:
    """One identifier derived from the lineage it belongs to.

    Deterministic so that replay is recognition rather than a second write, and
    scoped by workspace and revision so two extractions of one capture cannot
    collide. 68 characters, inside 0009's and 0008's 128-character `Identifier`.
    """
    material = f"{workspace_id}\0{revision_id}\0{ordinal}".encode()
    return f"{prefix}-{hashlib.sha256(material).hexdigest()}"


def _read_lineage(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    document_id: str,
    extraction_revision_id: str,
) -> _Lineage:
    """The capture, revision and result this derivation may rest on, or a refusal.

    Every read is keyed by the *owned* workspace, so a row belonging to another
    workspace is simply absent here rather than refused later: a foreign revision
    id and an unknown one are the same fact to this path, and both stop it.
    """
    revision = store.read_revision(
        connection,
        workspace_id=workspace_id,
        extraction_revision_id=extraction_revision_id,
    )
    if revision is None:
        raise DocumentKnowledgeRefused(
            "this workspace holds no such extraction revision"
        )
    if revision.document_id != document_id:
        raise DocumentKnowledgeRefused(
            "the extraction revision belongs to another document"
        )

    capture = store.read_capture(
        connection, workspace_id=workspace_id, capture_id=revision.capture_id
    )
    if capture is None:
        raise DocumentKnowledgeRefused("this workspace holds no such capture")
    if capture.document_id != document_id:
        raise DocumentKnowledgeRefused("the capture belongs to another document")

    result = store.read_result(
        connection,
        workspace_id=workspace_id,
        extraction_revision_id=extraction_revision_id,
    )
    if result is None:
        raise DocumentKnowledgeRefused("this extraction has recorded no result")
    if result.result_state != "succeeded":
        # `partial` is deliberately refused with the rest. A partial extraction is
        # publishable content and is not a *complete* account of the source, and a
        # candidate derived from an incomplete reading would carry an attribution
        # chain that reads as whole.
        raise DocumentKnowledgeRefused("this extraction did not succeed")

    head = store.read_head(
        connection, workspace_id=workspace_id, document_id=document_id
    )
    if head is None or head.current_extraction_id != extraction_revision_id:
        raise DocumentKnowledgeRefused(
            "this extraction is not the document's current extraction"
        )

    profile: dict[str, Any] | None
    unreadable = False
    try:
        profile = store.read_profile(
            connection, workspace_id=workspace_id, digest=revision.profile_digest
        )
    except store.DocumentIntakeInvalid:
        unreadable = True
        profile = None
    if unreadable or profile is None:
        raise DocumentKnowledgeRefused(
            "the extraction profile this revision names does not verify"
        )
    algorithm_id = profile.get("adapter")
    algorithm_version = profile.get("adapterVersion")
    identities = (algorithm_id, algorithm_version, result.worker_build)
    if not all(
        isinstance(value, str) and _IDENTIFIER.match(value) for value in identities
    ):
        raise DocumentKnowledgeRefused(
            "the extraction profile does not identify its extractor"
        )
    assert isinstance(algorithm_id, str) and isinstance(algorithm_version, str)

    return _Lineage(
        workspace_id=workspace_id,
        capture=capture,
        revision=revision,
        result=result,
        algorithm_id=algorithm_id,
        algorithm_version=algorithm_version,
        extractor_version=result.worker_build,
    )


def _segment_value(segment: store.StoredSegment) -> Any:
    """The segment's own value, verbatim and uninterpreted.

    0069 admits text *or* a structured value and never both. The structured half is
    already canonical JSON text, so it is decoded back to the value it encodes and
    re-encoded by the caller with the rest of the content -- never parsed for
    meaning, never inspected, never dispatched on.
    """
    if segment.text is not None:
        return segment.text
    if segment.structured_value_json is None:
        raise DocumentKnowledgeRefused("a segment carries neither text nor a value")
    decoded: Any
    failed = False
    try:
        decoded = json.loads(segment.structured_value_json)
    except ValueError:
        failed = True
    if failed:
        raise DocumentKnowledgeRefused("a segment's stored value does not decode")
    return decoded


def _locator(segment: store.StoredSegment, *, capture_id: str) -> Mapping[str, Any]:
    """The segment's source locator, proved to address this capture.

    0069 already requires a non-empty mapping carrying this capture's id, so this
    re-proves rather than discovers. It re-proves anyway: the locator is about to
    become the span pointer of a durable citation, and a citation whose addressing
    was taken on trust is the one failure this gate exists to prevent.
    """
    decoded: Any
    failed = False
    try:
        decoded = json.loads(segment.source_locator_json)
    except ValueError:
        failed = True
    if failed:
        raise DocumentKnowledgeRefused("a segment's source locator does not decode")
    if not isinstance(decoded, dict) or not decoded:
        raise DocumentKnowledgeRefused("a segment carries no source locator")
    if decoded.get("captureId") != capture_id:
        raise DocumentKnowledgeRefused(
            "a segment's source locator addresses another capture"
        )
    return decoded


def _candidate_content(
    *,
    lineage: _Lineage,
    segment: store.StoredSegment,
    locator: Mapping[str, Any],
    value: Any,
) -> dict[str, Any]:
    """One candidate's canonical content: the segment's value plus its attribution.

    `finding` is the catalogue's primary member for `knowledge.finding` and carries
    the segment whole. `document` is the attribution chain restated inside the
    content, so a record exported away from its own rows still says which document,
    capture, extraction and segment it came from.
    """
    capture, revision = lineage.capture, lineage.revision
    document: dict[str, Any] = {
        "documentId": revision.document_id,
        "captureId": capture.capture_id,
        "extractionRevisionId": revision.extraction_revision_id,
        "profileDigest": revision.profile_digest,
        "evidenceId": capture.evidence_id,
        "sourceDigest": capture.source_digest,
        "mediaType": capture.media_type,
        "segmentOrdinal": segment.segment_ordinal,
        "semanticRole": segment.semantic_role,
        "extractionMethod": segment.extraction_method,
        "visibility": segment.visibility,
        "revisionState": segment.revision_state,
        "sourceLocator": dict(locator),
    }
    if segment.language is not None:
        document["language"] = segment.language
    return {"kind": "document_segment", "finding": value, "document": document}


def _segment_record_content(
    *,
    lineage: _Lineage,
    segment: store.StoredSegment,
    value_digest: str,
) -> dict[str, Any]:
    """One normalized source record's content: the addressing, not the content.

    0008 bounds `content_json` here at 8192 bytes, which a segment's own text may
    exceed, so this row addresses the segment by digest rather than copying it. The
    text lives in `omnivia_extracted_segments`, the candidate's content carries it,
    and this row is what ties both to the exact evidence artifact.
    """
    capture, revision = lineage.capture, lineage.revision
    return {
        "schema": "omnivia.document-segment-record.v1",
        "documentId": revision.document_id,
        "captureId": capture.capture_id,
        "extractionRevisionId": revision.extraction_revision_id,
        "profileDigest": revision.profile_digest,
        "segmentOrdinal": segment.segment_ordinal,
        "semanticRole": segment.semantic_role,
        "extractionMethod": segment.extraction_method,
        "visibility": segment.visibility,
        "revisionState": segment.revision_state,
        "contentDigest": value_digest,
    }


def _plan(
    *, lineage: _Lineage, segments: Sequence[store.StoredSegment]
) -> tuple[_Planned, ...]:
    """Every qualifying segment resolved to the exact rows it would become.

    All of it before the first write, because a derivation that committed half an
    extraction would leave a document whose candidates are a silent subset of its
    content.
    """
    workspace_id = lineage.workspace_id
    revision_id = lineage.revision.extraction_revision_id
    planned: list[_Planned] = []
    pointers: dict[str, int] = {}
    for segment in segments:
        locator = _locator(segment, capture_id=lineage.capture.capture_id)
        pointer = to_canonical_json(dict(locator))
        if len(pointer) > MAX_SPAN_POINTER:
            raise DocumentKnowledgeRefused(
                "a segment's source locator is too large to address"
            )
        claimed = pointers.setdefault(pointer, segment.segment_ordinal)
        if claimed != segment.segment_ordinal:
            # Two segments addressing one place in the source. Whichever candidate a
            # reader followed would reach a citation that cannot distinguish them.
            raise DocumentKnowledgeRefused(
                "two qualifying segments share one source locator"
            )

        value = _segment_value(segment)
        value_json = to_canonical_json(value)
        content = _candidate_content(
            lineage=lineage, segment=segment, locator=locator, value=value
        )
        content_json = to_canonical_json(content)
        if len(content_json.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise DocumentKnowledgeRefused(
                "a segment exceeds the derivable content bound"
            )
        record_content_json = to_canonical_json(
            _segment_record_content(
                lineage=lineage, segment=segment, value_digest=_digest(value_json)
            )
        )
        ordinal = segment.segment_ordinal
        planned.append(
            _Planned(
                segment=segment,
                locator=locator,
                pointer=pointer,
                normalized_record_id=_derived_id(
                    "nsr", workspace_id, revision_id, ordinal
                ),
                normalized_span_id=_derived_id(
                    "nss", workspace_id, revision_id, ordinal
                ),
                governed_record_id=_derived_id(
                    "rec", workspace_id, revision_id, ordinal
                ),
                governed_record_version_id=_derived_id(
                    "ver", workspace_id, revision_id, ordinal
                ),
                assembly_id=_derived_id("asm", workspace_id, revision_id, ordinal),
                provenance_event_id=_derived_id(
                    "pev", workspace_id, revision_id, ordinal
                ),
                seal_id=_derived_id("seal", workspace_id, revision_id, ordinal),
                content_json=content_json,
                content_digest=_digest(content_json),
                record_content_json=record_content_json,
                record_content_digest=_digest(record_content_json),
            )
        )
    if not planned:
        raise DocumentKnowledgeRefused(
            "this extraction holds no segment in the requested roles"
        )
    return tuple(planned)


def _already_derived(
    connection: sqlite3.Connection, *, workspace_id: str, planned: tuple[_Planned, ...]
) -> bool:
    """Whether this exact derivation is already durable, refusing a half of one.

    One transaction writes the whole plan, so a mixed state is not reachable through
    this path. It is still checked, because the alternative to checking is writing
    on top of rows whose own append guards would refuse mid-transaction and leaving
    the caller to interpret an integrity error.
    """
    rows = connection.execute(
        "SELECT COUNT(*) FROM omnivia_normalized_source_records "
        "WHERE workspace_id = ? AND normalized_record_id IN "
        f"({', '.join('?' * len(planned))})",
        (workspace_id, *(item.normalized_record_id for item in planned)),
    ).fetchone()
    found = int(rows[0])
    if found == 0:
        return False
    if found == len(planned):
        return True
    raise DocumentKnowledgeRefused(
        "this extraction is partially derived and cannot be completed"
    )


def _write(
    fenced: sqlite3.Connection,
    *,
    lineage: _Lineage,
    planned: tuple[_Planned, ...],
    now_us: int,
) -> None:
    """The whole plan, inside the caller's fence.

    The order is the dependency order and nothing else: evidence addressing first,
    because a candidate's link may not name a span that does not yet exist, then the
    record, the assembly, its one event, its lineage, its link, and the seal that
    makes all of it immutable and is where 0009 checks every rule above.
    """
    workspace_id = lineage.workspace_id
    capture, revision = lineage.capture, lineage.revision
    actor_id, actor_kind, actor_role = _ACTOR
    # `record_sequence` is per evidence artifact and must strictly advance, and one
    # capture's artifact may already carry the records of an earlier extraction of
    # the same bytes. The stream's own maximum is therefore the base, read once.
    base = int(
        fenced.execute(
            "SELECT COALESCE(MAX(record_sequence), 0) "
            "FROM omnivia_normalized_source_records WHERE evidence_id = ?",
            (capture.evidence_id,),
        ).fetchone()[0]
    )
    for offset, item in enumerate(planned, 1):
        fenced.execute(
            "INSERT INTO omnivia_normalized_source_records "
            "(normalized_record_id, evidence_id, workspace_id, evidence_blob_digest, "
            "record_sequence, record_type, schema_version, content_json, "
            "content_digest, parser_id, parser_version, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.normalized_record_id,
                capture.evidence_id,
                workspace_id,
                capture.source_digest,
                base + offset,
                SEGMENT_RECORD_TYPE,
                SEGMENT_RECORD_SCHEMA,
                item.record_content_json,
                item.record_content_digest,
                EXTRACTOR_ID,
                lineage.extractor_version,
                now_us,
            ),
        )
        fenced.execute(
            "INSERT INTO omnivia_normalized_source_spans "
            "(normalized_span_id, normalized_record_id, evidence_id, workspace_id, "
            "span_sequence, span_kind, span_pointer, span_start_offset, "
            "span_end_offset, recorded_at_us) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, NULL, NULL, ?)",
            (
                item.normalized_span_id,
                item.normalized_record_id,
                capture.evidence_id,
                workspace_id,
                SEGMENT_SPAN_KIND,
                item.pointer,
                now_us,
            ),
        )
        fenced.execute(
            "INSERT INTO omnivia_governed_records "
            "(workspace_id, governed_record_id, record_type, domain_scope, "
            "recorded_at_us) VALUES (?, ?, ?, ?, ?)",
            (
                workspace_id,
                item.governed_record_id,
                RECORD_TYPE,
                DOMAIN_SCOPE,
                now_us,
            ),
        )
        # `layer='candidate'`, `authority_level='proposed'` and a NULL
        # `governance_disposition` are literals, not parameters: there is no value a
        # caller can pass that makes this row reviewed, canonical or accepted.
        fenced.execute(
            "INSERT INTO omnivia_governed_version_assemblies "
            "(workspace_id, assembly_id, governed_record_id, "
            "governed_record_version_id, record_type, domain_scope, layer, "
            "authority_level, governance_disposition, candidate_origin, "
            "extraction_kind, decision_source_kind, decision_source_id, "
            "authority_policy_id, authority_policy_version, policy_decision_ref, "
            "content_schema_version, content_json, content_digest, "
            "evidence_disposition, confidence_ppm, assertion_actor_id, "
            "assertion_actor_kind, assertion_actor_role, reason_code, "
            "reason_comment, valid_from_us, valid_to_us, recorded_at_us, "
            "append_ordinal, correlation_kind, correlation_id, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, 'candidate', 'proposed', NULL, 'extracted', "
            "'deterministic', NULL, NULL, NULL, NULL, NULL, ?, ?, ?, 'available', "
            "NULL, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, 'extraction_run', ?, NULL)",
            (
                workspace_id,
                item.assembly_id,
                item.governed_record_id,
                item.governed_record_version_id,
                RECORD_TYPE,
                DOMAIN_SCOPE,
                CONTENT_SCHEMA_VERSION,
                item.content_json,
                item.content_digest,
                actor_id,
                actor_kind,
                actor_role,
                now_us,
                now_us,
                item.segment.segment_ordinal,
                revision.extraction_revision_id,
            ),
        )
        fenced.execute(
            "INSERT INTO omnivia_governed_provenance_events "
            "(workspace_id, provenance_event_id, assembly_id, "
            "governed_record_version_id, provenance_sequence, action, actor_id, "
            "actor_kind, actor_role, policy_id, policy_version, occurred_at_us, "
            "recorded_at_us, reason_code, reason_comment, audit_ref, "
            "correlation_kind, correlation_id, predecessor_record_id, "
            "predecessor_version_id, evidence_disposition) "
            "VALUES (?, ?, ?, ?, 1, 'candidate.extracted', ?, ?, ?, NULL, NULL, ?, "
            "?, NULL, NULL, NULL, 'extraction_run', ?, NULL, NULL, 'available')",
            (
                workspace_id,
                item.provenance_event_id,
                item.assembly_id,
                item.governed_record_version_id,
                actor_id,
                actor_kind,
                actor_role,
                now_us,
                now_us,
                revision.extraction_revision_id,
            ),
        )
        # `deterministic` is the variant 0009 forbids a model, an inference
        # configuration, a prompt template and a randomness seed. The document
        # extractors are exactly that, and recording it here is what makes "no model
        # read this document" a stored fact rather than a claim in prose.
        fenced.execute(
            "INSERT INTO omnivia_governed_extraction_lineage "
            "(workspace_id, assembly_id, provenance_event_id, correlation_kind, "
            "correlation_id, extraction_kind, extraction_run_id, extractor_id, "
            "extractor_version, pipeline_version, configuration_version, "
            "configuration_digest, algorithm_id, algorithm_version, "
            "randomness_seed, model_id, model_version, "
            "inference_configuration_digest, prompt_template_id, "
            "prompt_template_version, prompt_template_digest, actor_id, actor_kind, "
            "actor_role, evidence_disposition, occurred_at_us, recorded_at_us, "
            "append_ordinal) "
            "VALUES (?, ?, ?, 'extraction_run', ?, 'deterministic', ?, ?, ?, ?, ?, "
            "?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, "
            "'available', ?, ?, ?)",
            (
                workspace_id,
                item.assembly_id,
                item.provenance_event_id,
                revision.extraction_revision_id,
                revision.extraction_revision_id,
                EXTRACTOR_ID,
                lineage.extractor_version,
                store.PROFILE_SCHEMA,
                revision.profile_digest,
                revision.profile_digest,
                lineage.algorithm_id,
                lineage.algorithm_version,
                actor_id,
                actor_kind,
                actor_role,
                now_us,
                now_us,
                item.segment.segment_ordinal,
            ),
        )
        fenced.execute(
            "INSERT INTO omnivia_governed_version_evidence_links "
            "(workspace_id, assembly_id, provenance_event_id, link_ordinal, "
            "evidence_id, normalized_record_id, normalized_span_id, recorded_at_us) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (
                workspace_id,
                item.assembly_id,
                item.provenance_event_id,
                capture.evidence_id,
                item.normalized_record_id,
                item.normalized_span_id,
                now_us,
            ),
        )
        fenced.execute(
            "INSERT INTO omnivia_governed_version_seals "
            "(workspace_id, seal_id, assembly_id, governed_record_version_id, "
            "correlation_kind, correlation_id, sealed_at_us) "
            "VALUES (?, ?, ?, ?, 'extraction_run', ?, ?)",
            (
                workspace_id,
                item.seal_id,
                item.assembly_id,
                item.governed_record_version_id,
                revision.extraction_revision_id,
                now_us,
            ),
        )


def derive_document_knowledge(
    runner: ServiceRunner,
    *,
    document_id: str,
    extraction_revision_id: str,
    roles: frozenset[str] = DEFAULT_ROLES,
) -> DocumentKnowledgeDerivation:
    """Derive source evidence and one proposed candidate per segment, or refuse.

    `roles` narrows which semantic roles become candidates and may only narrow: a
    role outside `store.SEGMENT_ROLES` is refused rather than silently ignored, so a
    typo cannot quietly derive nothing.
    """
    assert runner.connection is not None
    assert runner.identity is not None
    assert runner.generation is not None
    workspace_id = runner.workspace_id
    if workspace_id is None:
        raise DocumentKnowledgeRefused("this service instance owns no workspace")
    if not roles or not roles <= store.SEGMENT_ROLES:
        raise DocumentKnowledgeRefused("the requested semantic roles are not known")

    lineage = _read_lineage(
        runner.connection,
        workspace_id=workspace_id,
        document_id=document_id,
        extraction_revision_id=extraction_revision_id,
    )
    segments = [
        segment
        for segment in store.read_segments(
            runner.connection,
            workspace_id=workspace_id,
            extraction_revision_id=extraction_revision_id,
        )
        if segment.semantic_role in roles
    ]
    planned = _plan(lineage=lineage, segments=segments)

    status = "replayed"
    if not _already_derived(
        runner.connection, workspace_id=workspace_id, planned=planned
    ):
        status = "derived"
        with fenced_transaction(
            runner.connection,
            runner.identity,
            workspace_id=workspace_id,
            fencing_generation=runner.generation,
        ) as fenced:
            _write(
                fenced,
                lineage=lineage,
                planned=planned,
                now_us=time.time_ns() // 1000,
            )

    return DocumentKnowledgeDerivation(
        status=status,
        workspace_id=workspace_id,
        document_id=document_id,
        capture_id=lineage.capture.capture_id,
        extraction_revision_id=extraction_revision_id,
        evidence_id=lineage.capture.evidence_id,
        source_digest=lineage.capture.source_digest,
        profile_digest=lineage.revision.profile_digest,
        derived=tuple(
            DerivedSegment(
                segment_ordinal=item.segment.segment_ordinal,
                semantic_role=item.segment.semantic_role,
                normalized_record_id=item.normalized_record_id,
                normalized_span_id=item.normalized_span_id,
                span_pointer=item.pointer,
                governed_record_id=item.governed_record_id,
                governed_record_version_id=item.governed_record_version_id,
                assembly_id=item.assembly_id,
            )
            for item in planned
        ),
    )


__all__ = [
    "CONTENT_SCHEMA_VERSION",
    "DEFAULT_ROLES",
    "DOMAIN_SCOPE",
    "EXTRACTOR_ID",
    "MAX_CONTENT_BYTES",
    "MAX_SPAN_POINTER",
    "RECORD_TYPE",
    "SEGMENT_RECORD_SCHEMA",
    "SEGMENT_RECORD_TYPE",
    "SEGMENT_SPAN_KIND",
    "DerivedSegment",
    "DocumentKnowledgeDerivation",
    "DocumentKnowledgeRefused",
    "derive_document_knowledge",
]
