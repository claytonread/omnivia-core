"""The production code-analysis vertical (TECH-CORE-RETRIEVAL-CODE-001, W13-C; gate G8).

Everything here is the shipped path. A real installation (`initialise_workspace`), one real
`ServiceRunner` holding the workspace lease, a real git checkout registered through
`engineering.repository.register`, captured and sealed by the production capture writer and
committed through `engineering.source.capture.commit`. The acceptance policy is registered
through `code.policy.register`, the real Python producer runs inside `code.import`, the
binding is discovered through `code.status`, and every code answer comes from `code.query`
-- through the production dispatchers `service.main` composes, in process and over the real
local-IPC and HTTP listeners. No storage seam is called to import anything, and no receipt is
carried from the import to the reads: the reads find the binding themselves.

Two monkeypatches, both wrappers that still call the production function: one counts
producer runs, to show a replay never analyses again, and one slows the producer's per-file
decode, to make a request deadline pass mid-analysis.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.application import (
    ENGINEERING_FAMILY_OPERATIONS,
    LOCAL_TRANSPORT_ADAPTER,
    OPERATION_PURPOSES,
    ApplicationDispatcher,
    build_application_registry,
    build_engineering_application_dispatcher,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.mutation import MUTATION_PURPOSES, MUTATION_ROLES
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    capture_working_tree_snapshot_owned,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import code_analysis_python, code_analysis_typescript
from omnivia_core_runtime.storage.code_analysis_import import (
    FILE_EVIDENCE_KIND,
    file_evidence_id,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    MutationPrecondition,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

pytestmark = pytest.mark.skipif(
    not source_capture._NO_FOLLOW_WALK, reason="host lacks no-follow checkout walk"
)

PY = code_analysis_python.PRODUCER_ID
PY_PROFILE = code_analysis_python.DEFAULT_PYTHON_PROFILE.digest
TS = code_analysis_typescript.PRODUCER_ID
TS_PROFILE = code_analysis_typescript.DEFAULT_TYPESCRIPT_PROFILE.digest
REPO = "repo-vertical"
STREAM = "stream-vertical"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
ADAPTERS = ("in-process", "local-ipc", "http")

#: A second real principal of this endpoint. The configured local owner's label grant holds
#: every label and anyone else's holds none, so every confidentiality claim is made as this.
READER = "reader-principal"
RESTRICTED = "restricted"

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

_MODELS = b'''"""Models."""


class Record:
    def __init__(self, key: str) -> None:
        self.key = key


def make(key: str) -> Record:
    return Record(key)
'''

_SERVICE = b'''"""Service."""

from .models import make
from .vault import unseal


def run(key: str) -> object:
    record = make(key)
    unseal(key)
    return record


class Runner:
    def go(self, key: str) -> object:
        return make(key)
'''

#: The file the confidentiality cases restrict. It holds a resolved call into a visible
#: file, a dynamic call that can only be a diagnostic, and a method -- a node kind the
#: policy below does not accept -- whose call into `make` is therefore a refused candidate
#: anchored at a visible definition: exactly the diagnostic a file restriction must hide.
_VAULT = b'''"""Vault."""

from .models import make


def unseal(key: str) -> object:
    handler = getattr(key, "strip")
    handler()
    return make(key)


class Vault:
    def open(self, key: str) -> object:
        return make(key)
'''

REPOSITORY: dict[str, bytes] = {
    "pkg/__init__.py": b'"""Package."""\n',
    "pkg/models.py": _MODELS,
    "pkg/service.py": _SERVICE,
    "pkg/vault.py": _VAULT,
    # Admitted by the Python profile and not parseable: the analysis is partial.
    "pkg/broken.py": b"def broken(:\n    pass\n",
    # Read by no producer this service can run, or by none at all.
    "web/app.ts": b"export const answer = 42;\n",
    "README.md": b"# Vertical\n",
    "tools/build.rs": b"fn main() {}\n",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, env=_GIT_ENV, check=True, capture_output=True)


def _checkout(root: Path, files: Mapping[str, bytes]) -> Path:
    root.mkdir()
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return root


def digest_of(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def plain(value: Any) -> Any:
    """A decoded wire value (read-only mappings, tuples) as plain JSON values."""
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class Vertical:
    """One real installation, one live service runner, one registered checkout."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace_root = tmp_path / "workspace"
        self.installation_root = tmp_path / "installation-state"
        initialise_workspace(
            workspace_root=self.workspace_root,
            installation_root=self.installation_root,
            core_version=SERVER_VERSION,
        )
        self.runner = ServiceRunner(
            ServiceSettings(
                workspace_root=self.workspace_root,
                installation_root=self.installation_root,
                core_version=SERVER_VERSION,
                endpoint=None,
            )
        )
        report = self.runner.start()
        assert report.ready, report.reason
        assert self.runner.workspace_id is not None and self.runner.identity is not None
        self.workspace_id: str = self.runner.workspace_id
        self.installation_id: str = self.runner.identity.installation_id
        self.requests = 0
        self.checkouts: dict[str, Path] = {}
        self.sequences: dict[str, list[str]] = {}

    # --- the production dispatchers ---------------------------------------------------

    def _probe(self, principal: str) -> Dispatcher:
        return Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({self.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            self.runner,
        )

    def engineering(self, principal: str = LOCAL_PRINCIPAL) -> ApplicationDispatcher:
        return build_engineering_application_dispatcher(
            service=self.runner,
            principal_id=principal,
            installation_id=self.installation_id,
            workspace_id=self.workspace_id,
            fallback=self._probe(principal),
        )

    def reads(self, principal: str = LOCAL_PRINCIPAL) -> ApplicationDispatcher:
        registry = build_application_registry()
        return ApplicationDispatcher(
            registry=registry,
            session=local_owner_session(
                principal_id=principal,
                installation_id=self.installation_id,
                workspace_id=self.workspace_id,
                operations=frozenset(
                    name
                    for name in registry.operations
                    if get_operation_metadata(name).scope.side_effect == "none"
                ),
            ),
            binding=ServiceBinding(
                installation_id=self.installation_id, workspace_id=self.workspace_id
            ),
            supported_capabilities=server_capability_snapshot(registry),
            transport=LOCAL_TRANSPORT_ADAPTER,
            probe=self._probe(principal),
            record=None,
            service=self.runner,
        )

    def call(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        adapter: str = "in-process",
        principal: str = LOCAL_PRINCIPAL,
        key: str | None = None,
        precondition: str | None = None,
        deadline_ms: int | None = None,
    ) -> ResponseEnvelope:
        metadata = get_operation_metadata(operation)
        mutation = metadata.scope.side_effect != "none"
        self.requests += 1
        request_id = f"req-{self.requests}"
        request = RequestEnvelope(
            operation=operation,
            metadata=RequestMetadata(
                request_id=request_id,
                correlation_id=f"cor-{request_id}",
                trace_id=f"trc-{request_id}",
                api_version=CONTRACT_VERSION,
                client=CLIENT,
                workspace_id=self.workspace_id,
                scopes=metadata.scope.required_scopes,
                purpose=(MUTATION_PURPOSES if mutation else OPERATION_PURPOSES)[operation],
                required_capabilities=(
                    CapabilityRequirement(
                        id=metadata.required_capability.id, minimum_version="1.0", required=True
                    ),
                ),
                deadline_ms=deadline_ms,
                idempotency_key=(key or f"idem-{request_id}") if mutation else None,
                mutation_precondition=(
                    None if precondition is None else MutationPrecondition(record_version=precondition)
                ),
                principal_claim=None,
            ),
            input=dict(payload),
        )
        dispatcher = self.engineering(principal) if mutation else self.reads(principal)
        return s2._unrecorded_transport_call(adapter, dispatcher, request)

    def ok(self, operation: str, payload: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        response = self.call(operation, payload, **extra)
        assert isinstance(response, SuccessResponseEnvelope), response
        result: dict[str, Any] = plain(response.result)
        return result

    def refused(self, operation: str, payload: Mapping[str, Any], **extra: Any) -> Any:
        response = self.call(operation, payload, **extra)
        assert isinstance(response, ErrorResponseEnvelope), response
        return response.error

    # --- the repository, through the production source path ---------------------------

    def register(self, repository_id: str, files: Mapping[str, bytes]) -> Path:
        root = _checkout(self.tmp_path / repository_id, files)
        self.ok(
            "engineering.repository.register",
            {"repository_id": repository_id, "display_name": repository_id, "checkout_root": str(root)},
        )
        self.checkouts[repository_id] = root
        self.sequences[repository_id] = []
        return root

    def capture(self, repository_id: str, snapshot_id: str) -> str:
        """Seal the checkout as it stands and commit it as the stream's next event."""
        result = capture_working_tree_snapshot_owned(
            self.runner,
            repository_id=repository_id,
            checkout_root=self.checkouts[repository_id],
            snapshot_id=snapshot_id,
            renew_lease=self.runner.renew_lease_if_due,
        )
        assert (result.status, result.capture_status) == ("captured", "complete")
        recorded = self.sequences[repository_id]
        payload: dict[str, Any] = {
            "repository_id": repository_id,
            "stream_id": f"{STREAM}-{repository_id}",
            "sequence": len(recorded) + 1,
            "snapshot_id": snapshot_id,
            "expected_manifest_digest": result.manifest_digest,
        }
        if recorded:
            payload["predecessor"] = {"sequence": len(recorded), "snapshot_id": recorded[-1]}
        self.ok("engineering.source.capture.commit", payload, key=f"commit-{snapshot_id}")
        recorded.append(snapshot_id)
        return result.manifest_evidence_id

    def rows(self) -> dict[str, int]:
        """Every table an import, a policy or a refusal could touch."""
        connection = self.runner.connection
        assert connection is not None
        tables = (
            "omnivia_code_analysis_import_policies",
            "omnivia_code_analysis_imports",
            "omnivia_code_analysis_import_files",
            "omnivia_code_analysis_import_members",
            "omnivia_code_analysis_import_diagnostics",
            "omnivia_code_analysis_activations",
            "omnivia_governed_records",
            "omnivia_governed_version_assemblies",
            "omnivia_governed_version_evidence_links",
            "omnivia_evidence_artifacts",
            "omnivia_application_audit_events",
            "omnivia_idempotency_claims",
        )
        from omnivia_core_runtime.storage.connection import authorised

        with authorised(connection, mutations=False, ddl=False) as reader:
            return {
                table: int(reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    def restrict(self, evidence_id: str) -> None:
        """Attach a permission label to one artifact: 0008's own way to restrict it."""
        runner = self.runner
        assert runner.connection is not None and runner.identity is not None
        assert runner.generation is not None
        with fenced_transaction(
            runner.connection,
            runner.identity,
            workspace_id=self.workspace_id,
            fencing_generation=runner.generation,
        ) as fenced:
            fenced.execute(
                "INSERT INTO omnivia_evidence_permission_labels (label_event_id, evidence_id, "
                "workspace_id, label_sequence, label_action, permission_label, recorded_at_us) "
                "VALUES (?, ?, ?, 1, 'attached', ?, ?)",
                (f"lbl-{evidence_id}", evidence_id, self.workspace_id, RESTRICTED, time.time_ns() // 1000),
            )

    def close(self) -> None:
        self.runner.stop()


@pytest.fixture
def vertical(tmp_path: Path) -> Iterator[Vertical]:
    opened = Vertical(tmp_path)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def captured(vertical: Vertical) -> Vertical:
    """The repository registered and its first snapshot sealed and committed."""
    vertical.register(REPO, REPOSITORY)
    vertical.capture(REPO, "snap-1")
    return vertical


#: The owner's policy: every Python node kind but `method`, every Python relationship at
#: both strengths it may accept. Leaving methods out is what makes a method's call into a
#: visible function a refused candidate rather than a relation.
POLICY: dict[str, Any] = {
    "policy_id": "owner.code-import",
    "policy_version": "1",
    "producers": [{"producer_id": PY, "producer_version": "1.0.0", "profile_digest": PY_PROFILE}],
    "accepted_node_kinds": [
        {"producer_id": PY, "node_kind": kind}
        for kind in sorted(code_analysis_python.NODE_KINDS - {"method", "async_method"})
    ],
    "accepted_relationships": [
        {"producer_id": PY, "relationship": relationship, "evidence_strength": strength}
        for relationship in sorted(code_analysis_python.RELATIONSHIP_TYPES)
        for strength in ("resolved_static", "syntactic")
    ],
    "allow_reduced_coverage": False,
}


def import_request(snapshot_id: str = "snap-1", **overrides: Any) -> dict[str, Any]:
    return {
        "repository_id": REPO,
        "snapshot_id": snapshot_id,
        "producer_id": PY,
        "profile_digest": PY_PROFILE,
        "policy_id": "owner.code-import",
        "policy_version": "1",
        **overrides,
    }


def policy_registered(vertical: Vertical) -> None:
    vertical.ok("code.policy.register", POLICY, key="policy-1")


def imported(vertical: Vertical, snapshot_id: str = "snap-1", *, precondition: str = "0", key: str = "import-1") -> dict[str, Any]:
    return vertical.ok("code.import", import_request(snapshot_id), key=key, precondition=precondition)


def python_status(vertical: Vertical, *, principal: str = LOCAL_PRINCIPAL, **fields: Any) -> dict[str, Any]:
    status = vertical.ok("code.status", {"repository_id": REPO, **fields}, principal=principal)
    (entry,) = [item for item in status["analyses"] if item["producer_id"] == PY]
    return entry


def query(binding: Mapping[str, Any], intent: str, **fields: Any) -> dict[str, Any]:
    return {
        "intent": intent,
        "repository_id": binding["repository_id"],
        "snapshot_id": binding["snapshot_id"],
        "producer_id": binding["producer_id"],
        "profile_digest": binding["profile_digest"],
        **fields,
    }


def stable(result: Mapping[str, Any]) -> dict[str, Any]:
    """A result without its read instant: the one field two identical reads may differ in."""
    copied: dict[str, Any] = plain(result)
    copied.pop("as_of", None)
    if isinstance(copied.get("freshness"), dict):
        copied["freshness"].pop("as_of", None)
    return copied


def keys_of(items: list[dict[str, Any]]) -> list[str]:
    return [item["semantic_key"] for item in items]


def edges_of(result: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    return [(r["depth"], r["source_key"], r["relation_type"], r["target_key"]) for r in result["relations"]]


def members(vertical: Vertical, import_id: str) -> set[tuple[str, str]]:
    connection = vertical.runner.connection
    assert connection is not None
    from omnivia_core_runtime.storage.connection import authorised

    with authorised(connection, mutations=False, ddl=False) as reader:
        return {
            (str(record), str(version))
            for record, version in reader.execute(
                "SELECT governed_record_id, governed_record_version_id "
                "FROM omnivia_code_analysis_import_members WHERE import_id = ?",
                (import_id,),
            )
        }


def records_of(result: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (item["record"]["record_id"], item["record"]["version"])
        for name in ("definitions", "symbols", "relations")
        for item in result[name]
    }


# --- the shipped surface ------------------------------------------------------------


def test_the_three_operations_ship_where_their_authority_already_lives() -> None:
    """Policy and import join the engineering family's session; status reads beside query."""
    assert {"code.policy.register", "code.import"} <= ENGINEERING_FAMILY_OPERATIONS
    assert {"code.query", "code.status"}.isdisjoint(ENGINEERING_FAMILY_OPERATIONS)
    assert "code.status" in build_application_registry().operations
    assert MUTATION_ROLES["code.policy.register"] == "knowledge_reviewer"
    assert MUTATION_ROLES["code.import"] == "workspace_contributor"
    importing = get_operation_metadata("code.import")
    assert importing.precondition.required and importing.idempotency.required
    assert get_operation_metadata("code.status").scope.side_effect == "none"
    # MCP stays as it was: the two writes are mutations, the read is not allow-listed.
    ledger = json.loads(
        (Path(__file__).resolve().parents[5] / "tests/fixtures/service_conformance"
         / "operation-traceability-v1.json").read_text()
    )
    omitted = {entry["operation"]: entry["reason"] for entry in ledger["client_surfaces"]["mcp"]["omitted"]}
    assert omitted["code.policy.register"] == omitted["code.import"] == "mutation"
    assert omitted["code.status"] == "read_not_allow_listed"


def test_without_a_registered_policy_nothing_imports_and_nothing_is_consented(
    captured: Vertical,
) -> None:
    before = captured.rows()
    status = captured.ok("code.status", {"repository_id": REPO, "snapshot_id": "snap-1"})
    python, typescript = status["analyses"]
    assert (python["producer_id"], typescript["producer_id"]) == (PY, TS)
    assert python == {
        "producer_id": PY, "producer_version": "1.0.0", "profile_digest": PY_PROFILE,
        "languages": ["python"], "availability": "available", "readiness": "not_ready",
        "readiness_reason": "no_active_import", "activation_sequence": 0,
    }
    assert typescript["availability"] == "unavailable"
    assert typescript["availability_reason"] == "runtime_not_configured"
    assert typescript["profile_digest"] == TS_PROFILE
    error = captured.refused("code.import", import_request(), key="i-1", precondition="0")
    assert error.code == "not_found"
    assert captured.rows() == before


# --- policy ------------------------------------------------------------------------


def test_a_policy_is_owner_authorised_versioned_and_idempotent(captured: Vertical) -> None:
    first = captured.ok("code.policy.register", POLICY, key="policy-1", adapter="http")
    assert first["disposition"] == "registered"
    assert first["policy"]["policy_id"] == "owner.code-import"
    replayed = captured.ok("code.policy.register", POLICY, key="policy-1", adapter="local-ipc")
    assert replayed == first
    # Same rules under a new key: recorded once, reported as already there.
    again = captured.ok("code.policy.register", POLICY, key="policy-2")
    assert again["disposition"] == "already_registered"
    assert again["policy"] == first["policy"] and again["registered_at"] == first["registered_at"]
    changed = {**POLICY, "allow_reduced_coverage": True}
    assert captured.refused("code.policy.register", changed, key="policy-1").code == "idempotency_conflict"
    assert captured.refused("code.policy.register", changed, key="policy-3").code == "conflict"
    # A lock on a profile this service does not declare, or a kind no producer emits.
    for invalid in (
        {**POLICY, "policy_version": "2",
         "producers": [{"producer_id": PY, "producer_version": "1.0.0", "profile_digest": "sha256:" + "0" * 64}]},
        {**POLICY, "policy_version": "2",
         "accepted_node_kinds": [{"producer_id": PY, "node_kind": "teleporter"}]},
        {**POLICY, "policy_version": "2", "authorised_by": "someone-else"},
    ):
        assert captured.refused("code.policy.register", invalid, key="policy-x").code == "invalid_request"
    # The authoriser is the caller the envelope authenticated, bound to its own audit.
    connection = captured.runner.connection
    assert connection is not None
    from omnivia_core_runtime.storage.connection import authorised

    with authorised(connection, mutations=False, ddl=False) as reader:
        row = reader.execute(
            "SELECT p.authorised_by, e.principal_id, e.operation "
            "FROM omnivia_code_analysis_import_policies p "
            "JOIN omnivia_application_audit_events e ON e.audit_ref = p.audit_ref"
        ).fetchall()
    assert row == [(LOCAL_PRINCIPAL, LOCAL_PRINCIPAL, "code.policy.register")]


# --- the production import route, discovery and every query intent ------------------


def test_the_production_route_imports_and_discovery_finds_the_binding(captured: Vertical) -> None:
    policy_registered(captured)
    receipt = imported(captured)
    binding = receipt["binding"]
    assert (binding["repository_id"], binding["snapshot_id"], binding["producer_id"]) == (REPO, "snap-1", PY)
    assert binding["profile_digest"] == PY_PROFILE and binding["producer_version"] == "1.0.0"
    assert receipt["activation_sequence"] == 1 and receipt["coverage_decision"] == "initial"
    assert "previous_import_id" not in receipt
    assert receipt["freshness"] == {
        "state": "current", "snapshot_id": "snap-1", "newest_snapshot_id": "snap-1",
        "working_tree": "not_observed",
    }
    # Four covered files: broken.py contributed nothing, and no other language ran.
    assert receipt["file_count"] == 4 and receipt["entity_count"] > 0 and receipt["relation_count"] > 0

    status = captured.ok("code.status", {"repository_id": REPO, "snapshot_id": "snap-1"})
    python = status["analyses"][0]
    assert python["readiness"] == "ready" and "readiness_reason" not in python
    assert python["binding"] == binding
    assert python["activation_sequence"] == 1
    assert python["policy"] == {
        "policy_id": "owner.code-import", "policy_version": "1",
        "policy_digest": binding["policy_digest"],
    }
    assert python["coverage"] == {"state": "partial", "admitted_file_count": 5, "analysed_file_count": 4}
    assert python["freshness"]["state"] == "current"
    snapshot = status["snapshot"]
    assert snapshot["capture_status"] == "complete" and snapshot["freshness"]["state"] == "current"
    assert snapshot["unsupported"] == [{"suffix": ".md", "file_count": 1}, {"suffix": ".rs", "file_count": 1}]
    assert snapshot["unsupported_truncated"] is False
    # The read discovered without being told: no field of the binding came from the receipt.
    discovered = python["binding"]

    definition = captured.ok("code.query", query(discovered, "definition", semantic_key="pkg.models.make"))
    assert keys_of(definition["definitions"]) == ["pkg.models.make"]
    assert definition["freshness"]["stale"] is False
    callers = captured.ok("code.query", query(discovered, "callers", semantic_key="pkg.models.make"))
    assert edges_of(callers) == [
        (1, "pkg.service.run", "code.calls", "pkg.models.make"),
        (1, "pkg.vault.unseal", "code.calls", "pkg.models.make"),
    ]
    # The methods' calls into `make` were refused by the policy: coverage, never edges.
    assert sorted((d["source_key"], d["reason"]) for d in callers["diagnostics"]) == [
        ("pkg.service.Runner.go", "endpoint_not_accepted"),
        ("pkg.vault.Vault.open", "endpoint_not_accepted"),
    ]
    dependencies = captured.ok("code.query", query(discovered, "dependencies", semantic_key="pkg.service"))
    assert {edge[3] for edge in edges_of(dependencies)} == {"pkg.models.make", "pkg.vault.unseal"}
    references = captured.ok("code.query", query(discovered, "references", semantic_key="pkg.models.make"))
    assert references["relations"] == [] and references["coverage"]["unattributed_inbound"] is True
    context = captured.ok(
        "code.query",
        query(
            discovered, "code_context", semantic_key="pkg.service.run",
            context={"query": "make", "mode": "deterministic_view", "token_budget": 4000},
        ),
    )
    spans = {span["semantic_key"]: span for span in context["context"]["spans"]}
    assert "def run(key: str) -> object:" in spans["pkg.service.run"]["text"]
    assert {"pkg.models.make", "pkg.vault.unseal"} <= set(spans)

    # Every fact cites its own file's evidence beside the manifest, and the file artifacts
    # are in the search projection the import's barrier brought level.
    evidence = captured.ok("evidence.search", {"query": "vault", "limit": 50})
    found = {item["evidence_id"]: item for item in evidence["evidence"]}
    vault = file_evidence_id(REPO, "pkg/vault.py", digest_of(_VAULT))
    assert found[vault]["source"]["kind"] == FILE_EVIDENCE_KIND


@pytest.mark.parametrize("adapter", ("local-ipc", "http"))
def test_every_transport_serves_the_same_import_status_and_answers(
    captured: Vertical, adapter: str
) -> None:
    policy_registered(captured)
    receipt = captured.ok("code.import", import_request(), key="import-1", precondition="0", adapter=adapter)
    # A replay over the in-process path returns the receipt the wire path recorded.
    assert captured.ok("code.import", import_request(), key="import-1", precondition="0") == receipt
    status = {"repository_id": REPO, "snapshot_id": "snap-1"}
    assert stable(captured.ok("code.status", status, adapter=adapter)) == stable(captured.ok("code.status", status))
    binding = receipt["binding"]
    for body in (
        query(binding, "definition", name="make"),
        query(binding, "callers", semantic_key="pkg.models.make"),
        query(binding, "callees", semantic_key="pkg.service.run", depth=2),
        query(binding, "dependencies", semantic_key="pkg.service"),
        query(binding, "references", semantic_key="pkg.models.make"),
        query(binding, "definition", semantic_key="pkg.nothing"),
    ):
        local = captured.call("code.query", body)
        remote = captured.call("code.query", body, adapter=adapter)
        assert type(remote) is type(local)
        if isinstance(local, SuccessResponseEnvelope):
            assert isinstance(remote, SuccessResponseEnvelope)
            assert stable(remote.result) == stable(local.result)
        else:
            assert isinstance(remote, ErrorResponseEnvelope) and isinstance(local, ErrorResponseEnvelope)
            assert remote.error.to_wire() == local.error.to_wire()
    # The assembled context: the same code answer and the same captured spans. The
    # architecture component is content-addressed over the read instant, so only its
    # availability is compared here; its own parity is the context authority's test.
    context_body = query(
        binding, "code_context", semantic_key="pkg.service.run",
        context={"query": "make", "mode": "deterministic_view", "token_budget": 4000},
    )
    local_context = captured.ok("code.query", context_body)
    remote_context = captured.ok("code.query", context_body, adapter=adapter)
    for answer in (local_context, remote_context):
        assert {span["semantic_key"] for span in answer["context"]["spans"]} >= {
            "pkg.service.run", "pkg.models.make",
        }
    assert remote_context["context"]["spans"] == local_context["context"]["spans"]
    assert (
        remote_context["context"]["architecture"]["availability"]
        == local_context["context"]["architecture"]["availability"]
    )
    assert {key: value for key, value in stable(remote_context).items() if key != "context"} == {
        key: value for key, value in stable(local_context).items() if key != "context"
    }


# --- replay, conflicting replay, stale and cancelled attempts ------------------------


def test_a_replay_reconciles_and_a_conflicting_replay_refuses_without_analysing(
    captured: Vertical, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_registered(captured)
    runs: list[str] = []
    real = code_analysis_python.analyse_python_snapshot

    def counted(*args: Any, **kwargs: Any) -> Any:
        runs.append(kwargs["snapshot_id"])
        return real(*args, **kwargs)

    monkeypatch.setattr(code_analysis_python, "analyse_python_snapshot", counted)
    first = imported(captured)
    assert runs == ["snap-1"]
    after_first = captured.rows()
    replayed = captured.call("code.import", import_request(), key="import-1", precondition="0")
    assert isinstance(replayed, SuccessResponseEnvelope)
    assert plain(replayed.result) == first
    assert runs == ["snap-1"]
    rows = captured.rows()
    # A replay spends no row but the executor's own: it writes no claim, no audit, no fact.
    assert rows == after_first
    conflicting = captured.refused(
        "code.import", import_request(policy_version="2"), key="import-1", precondition="0"
    )
    assert conflicting.code == "idempotency_conflict"
    assert runs == ["snap-1"] and captured.rows() == after_first


def test_a_stale_attempt_is_fenced_and_a_cancelled_one_leaves_the_graph_intact(
    captured: Vertical, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_registered(captured)
    first = imported(captured)
    old = first["binding"]
    (captured.checkouts[REPO] / "pkg/models.py").write_bytes(_MODELS + b"\n\ndef extra() -> int:\n    return 1\n")
    captured.capture(REPO, "snap-2")
    before = captured.rows()
    answer = captured.ok("code.query", query(old, "callers", semantic_key="pkg.models.make"))

    # Planned against "no analysis yet", but one is active: refused, nothing written.
    stale = captured.refused("code.import", import_request("snap-2"), key="import-2", precondition="0")
    assert stale.code == "mutation_precondition_failed"
    assert captured.rows() == before

    # The deadline passes while the producer is still decoding files: abandoned whole.
    real_decode = code_analysis_python._decode

    def slow(content: bytes) -> Any:
        time.sleep(0.05)
        return real_decode(content)

    monkeypatch.setattr(code_analysis_python, "_decode", slow)
    cancelled = captured.refused(
        "code.import", import_request("snap-2"), key="import-2", precondition="1", deadline_ms=60
    )
    assert cancelled.code == "deadline_exceeded"
    assert captured.rows() == before
    # The previous analysis still answers, unchanged and now labelled stale.
    still = captured.ok("code.query", query(old, "callers", semantic_key="pkg.models.make"))
    assert still["relations"] == answer["relations"] and still["freshness"]["stale"] is True
    monkeypatch.setattr(code_analysis_python, "_decode", real_decode)
    # The same key, unhurried, is a fresh attempt: nothing of the cancelled one was claimed.
    second = captured.ok("code.import", import_request("snap-2"), key="import-2", precondition="1")
    assert second["previous_import_id"] == old["import_id"]
    assert second["activation_sequence"] == 2 and second["coverage_decision"] == "complete"


def test_a_newer_capture_marks_the_analysis_stale_and_cutover_never_mixes_revisions(
    captured: Vertical,
) -> None:
    policy_registered(captured)
    old = imported(captured)["binding"]
    (captured.checkouts[REPO] / "pkg/service.py").write_bytes(
        _SERVICE.replace(b"    unseal(key)\n", b"    unseal(key)\n    make(key)\n")
    )
    captured.capture(REPO, "snap-2")

    python = python_status(captured)
    assert python["binding"] == old and python["readiness"] == "ready"
    assert python["freshness"] == {
        "state": "stale", "snapshot_id": "snap-1", "newest_snapshot_id": "snap-2",
        "working_tree": "not_observed",
    }
    against_new = python_status(captured, snapshot_id="snap-2")
    assert (against_new["readiness"], against_new["readiness_reason"]) == ("not_ready", "snapshot_not_active")
    old_answer = captured.ok("code.query", query(old, "callees", semantic_key="pkg.service.run"))
    assert old_answer["freshness"]["stale"] is True
    assert [r["target_key"] for r in old_answer["relations"]] == ["pkg.models.make", "pkg.vault.unseal"]

    receipt = captured.ok(
        "code.import", import_request("snap-2"), key="import-2", precondition=str(python["activation_sequence"])
    )
    new = receipt["binding"]
    assert receipt["previous_import_id"] == old["import_id"] and new["snapshot_id"] == "snap-2"
    assert receipt["freshness"]["state"] == "current"
    retired = captured.refused("code.query", query(old, "callees", semantic_key="pkg.service.run"))
    assert retired.code == "projection_unavailable"
    assert dict(retired.details or {}) == {"readiness": "not_ready", "reason": "snapshot_not_active"}
    current = captured.ok("code.query", query(new, "callees", semantic_key="pkg.service.run"))
    assert current["freshness"]["stale"] is False
    assert [r["target_key"] for r in current["relations"]] == [
        "pkg.models.make", "pkg.models.make", "pkg.vault.unseal",
    ]
    assert records_of(current) <= members(captured, new["import_id"])
    assert not records_of(current) & members(captured, old["import_id"])


# --- unsupported languages ------------------------------------------------------------


def test_unsupported_languages_are_stated_never_hidden(captured: Vertical) -> None:
    policy_registered(captured)
    typescript = captured.refused(
        "code.import",
        import_request(producer_id=TS, profile_digest=TS_PROFILE),
        key="ts-1",
        precondition="0",
    )
    assert typescript.code == "dependency_unavailable"
    assert dict(typescript.details or {}) == {"reason": "runtime_not_configured"}
    unknown = captured.refused(
        "code.import", import_request(profile_digest="sha256:" + "0" * 64), key="x-1", precondition="0"
    )
    assert unknown.code == "invalid_request"

    other = "repo-rust"
    captured.register(other, {"src/main.rs": b"fn main() {}\n", "README.md": b"# Rust\n", "go/x.go": b"package x\n"})
    captured.capture(other, "snap-rust")
    before = captured.rows()
    refused = captured.refused(
        "code.import",
        {**import_request("snap-rust"), "repository_id": other},
        key="rust-1",
        precondition="0",
    )
    assert refused.code == "conflict"
    assert dict(refused.details or {}) == {"reason": "no_admitted_source"}
    assert captured.rows() == before
    status = captured.ok("code.status", {"repository_id": other, "snapshot_id": "snap-rust"})
    python = status["analyses"][0]
    assert (python["readiness"], python["readiness_reason"]) == ("not_ready", "no_admitted_source")
    assert status["snapshot"]["unsupported"] == [
        {"suffix": ".go", "file_count": 1}, {"suffix": ".md", "file_count": 1}, {"suffix": ".rs", "file_count": 1},
    ]
    # A snapshot of another repository is not this repository's.
    assert captured.refused("code.status", {"repository_id": REPO, "snapshot_id": "snap-rust"}).code == "not_found"


# --- one restricted file, two principals ------------------------------------------------


def test_one_restricted_file_hides_exactly_its_own_facts_from_another_principal(
    captured: Vertical,
) -> None:
    """Per-file visibility from production data: the file artifact the import wrote, one
    0008 label on it, and the production grant, filter and walk. Subtraction only."""
    policy_registered(captured)
    binding = imported(captured)["binding"]
    callers_body = query(binding, "callers", semantic_key="pkg.models.make")
    callees_body = query(binding, "callees", semantic_key="pkg.service.run")
    before = {
        "callers": captured.ok("code.query", callers_body, principal=READER),
        "callees": captured.ok("code.query", callees_body, principal=READER),
        "status": python_status(captured, principal=READER),
    }
    # Before the label the reader sees what the owner sees.
    assert stable(before["callers"]) == stable(captured.ok("code.query", callers_body))

    captured.restrict(file_evidence_id(REPO, "pkg/vault.py", digest_of(_VAULT)))

    # The falsifier the output revalidation cannot mask, asked first: the visible diagnostic
    # sorts first, so at a limit of one a hidden diagnostic that leaked into the selection
    # would be cut off rather than disclosed -- passing revalidation -- and would still say
    # `truncated`. Unlimited, the same leak is disclosed and revalidation refuses it.
    owner_order = [
        d["source_key"] for d in captured.ok("code.query", callers_body)["diagnostics"]
    ]
    assert owner_order == ["pkg.service.Runner.go", "pkg.vault.Vault.open"]
    limited = captured.ok("code.query", {**callers_body, "limit": 1}, principal=READER)
    assert limited["coverage"]["truncated"] is False
    callers = captured.ok("code.query", callers_body, principal=READER)
    assert edges_of(limited) == edges_of(callers) and limited["diagnostics"] == callers["diagnostics"]
    assert edges_of(callers) == [(1, "pkg.service.run", "code.calls", "pkg.models.make")]
    assert keys_of(callers["symbols"]) == ["pkg.service.run"]
    # The refused call read out of the hidden file is not coverage either; the one read out
    # of a visible file still is.
    assert [d["source_key"] for d in callers["diagnostics"]] == ["pkg.service.Runner.go"]
    assert "vault" not in json.dumps(callers)

    callees = captured.ok("code.query", callees_body, principal=READER)
    assert [r["target_key"] for r in callees["relations"]] == ["pkg.models.make"]
    expected = stable(before["callees"])
    expected["relations"] = [r for r in expected["relations"] if r["target_key"] != "pkg.vault.unseal"]
    expected["symbols"] = [s for s in expected["symbols"] if s["semantic_key"] != "pkg.vault.unseal"]
    assert stable(callees) == expected

    hidden = captured.refused("code.query", query(binding, "definition", semantic_key="pkg.vault.unseal"), principal=READER)
    absent = captured.refused("code.query", query(binding, "definition", semantic_key="pkg.nothing"), principal=READER)
    assert hidden.to_wire() == absent.to_wire()

    # Discovery counts only what the reader may see: the hidden file moves neither count.
    status = python_status(captured, principal=READER)
    assert status["coverage"] == {"state": "partial", "admitted_file_count": 4, "analysed_file_count": 3}
    assert before["status"]["coverage"] == {"state": "partial", "admitted_file_count": 5, "analysed_file_count": 4}
    # The owner still sees all of it, over every transport.
    for adapter in ADAPTERS:
        owner = captured.ok("code.query", callers_body, adapter=adapter)
        assert len(owner["relations"]) == 2 and len(owner["diagnostics"]) == 2
        reader = captured.ok("code.query", callers_body, adapter=adapter, principal=READER)
        assert stable(reader) == stable(callers)
    owner_search = captured.ok("evidence.search", {"query": "vault", "limit": 50})
    reader_search = captured.ok("evidence.search", {"query": "vault", "limit": 50}, principal=READER)
    vault = file_evidence_id(REPO, "pkg/vault.py", digest_of(_VAULT))
    assert vault in {item["evidence_id"] for item in owner_search["evidence"]}
    assert vault not in {item["evidence_id"] for item in reader_search["evidence"]}


def test_the_import_route_carries_no_path_principal_or_profile_body(captured: Vertical) -> None:
    policy_registered(captured)
    before = captured.rows()
    for smuggled in (
        {**import_request(), "workspace_id": "ws-other"},
        {**import_request(), "checkout_root": "/etc"},
        {**import_request(), "profile": {"include_suffixes": [".sh"]}},
        {**import_request(), "principal_id": "someone"},
    ):
        error = captured.refused("code.import", smuggled, key="smuggled-1", precondition="0")
        assert error.code == "invalid_request"
    assert captured.rows() == before


def test_a_hidden_snapshot_is_reported_exactly_as_one_that_does_not_exist(
    captured: Vertical,
) -> None:
    """A restricted manifest hides the whole analysis it backs, and a restricted newer
    capture neither names itself nor makes the visible analysis read as stale."""
    policy_registered(captured)
    binding = imported(captured)["binding"]
    (captured.checkouts[REPO] / "pkg/models.py").write_bytes(_MODELS + b"\n\ndef later() -> int:\n    return 2\n")
    newer_manifest = captured.capture(REPO, "snap-2")
    stale_view = python_status(captured, principal=READER)
    assert stale_view["freshness"]["newest_snapshot_id"] == "snap-2"

    captured.restrict(newer_manifest)
    current_view = python_status(captured, principal=READER)
    assert current_view["freshness"] == {
        "state": "current", "snapshot_id": "snap-1", "newest_snapshot_id": "snap-1",
        "working_tree": "not_observed",
    }
    answer = captured.ok("code.query", query(binding, "definition", name="make"), principal=READER)
    assert answer["freshness"]["stale"] is False
    hidden_snapshot = captured.refused(
        "code.status", {"repository_id": REPO, "snapshot_id": "snap-2"}, principal=READER
    )
    missing_snapshot = captured.refused(
        "code.status", {"repository_id": REPO, "snapshot_id": "snap-9"}, principal=READER
    )
    assert hidden_snapshot.to_wire() == missing_snapshot.to_wire()
    # The owner still sees the newer capture, and the analysis as stale.
    assert python_status(captured)["freshness"]["state"] == "stale"

    captured.restrict(binding["manifest_evidence_id"])
    absent = python_status(captured, principal=READER)
    assert absent == {
        "producer_id": PY, "producer_version": "1.0.0", "profile_digest": PY_PROFILE,
        "languages": ["python"], "availability": "available", "readiness": "not_ready",
        "readiness_reason": "no_active_import", "activation_sequence": 0,
        "policy": python_status(captured)["policy"],
    }
    # Answered as a scope nobody ever analysed, never as a subject that was not found there.
    hidden_analysis = captured.refused(
        "code.query", query(binding, "definition", name="make"), principal=READER
    )
    never_analysed = captured.refused(
        "code.query",
        {**query(binding, "definition", name="make"), "producer_id": TS, "profile_digest": TS_PROFILE},
        principal=READER,
    )
    assert hidden_analysis.to_wire() == never_analysed.to_wire()
    assert dict(hidden_analysis.details or {}) == {"readiness": "not_ready", "reason": "no_active_import"}
