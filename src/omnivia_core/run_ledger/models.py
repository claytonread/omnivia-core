"""Versioned run-ledger contract models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from omnivia_core.knowledge.models import ContractVersion

RUN_LEDGER_CONTRACT_VERSION = ContractVersion(1, 0)
RUN_LEDGER_PATH_ENV = "OMNIVIA_RUN_LEDGER_PATH"


class RunLedgerStatus(str, Enum):
    """Lifecycle states for a run-ledger entry."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class EvidenceFileRef:
    """A file reference that captures run evidence."""

    path: str
    kind: str = "artifact"
    description: str | None = None
    checksum: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "kind": self.kind,
            "description": self.description,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str | None]) -> EvidenceFileRef:
        return cls(
            path=data["path"] or "",
            kind=data.get("kind") or "artifact",
            description=data.get("description"),
            checksum=data.get("checksum"),
        )


@dataclass(frozen=True)
class RunLedgerProvenance:
    """Producer metadata for a run-ledger entry."""

    producer: str
    source_ref: str | None = None
    producer_version: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "producer": self.producer,
            "source_ref": self.source_ref,
            "producer_version": self.producer_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str | None]) -> RunLedgerProvenance:
        return cls(
            producer=data["producer"] or "",
            source_ref=data.get("source_ref"),
            producer_version=data.get("producer_version"),
        )


@dataclass(frozen=True)
class RunLedgerEntry:
    """A portable run-ledger entry shared across OmniVia tooling."""

    run_id: str
    task_id: str
    target_repo: str
    lane_id: str
    status: RunLedgerStatus
    started_at: str
    updated_at: str
    evidence_file_refs: list[EvidenceFileRef] = field(default_factory=list)
    provenance: RunLedgerProvenance = field(
        default_factory=lambda: RunLedgerProvenance(producer="unknown")
    )
    completed_at: str | None = None
    contract_version: ContractVersion = RUN_LEDGER_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "target_repo": self.target_repo,
            "lane_id": self.lane_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "contract_version": str(self.contract_version),
            "evidence_file_refs": [
                evidence_file_ref.to_dict()
                for evidence_file_ref in self.evidence_file_refs
            ],
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RunLedgerEntry:
        evidence_file_refs_data = data.get("evidence_file_refs", [])
        if not isinstance(evidence_file_refs_data, list):
            raise TypeError("evidence_file_refs must be a list")
        for item in evidence_file_refs_data:
            if not isinstance(item, dict):
                raise TypeError("evidence_file_refs entries must be objects")
        provenance_data = data.get("provenance", {})
        if not isinstance(provenance_data, Mapping):
            raise TypeError("provenance must be an object")
        return cls(
            run_id=str(data["run_id"]),
            task_id=str(data["task_id"]),
            target_repo=str(data["target_repo"]),
            lane_id=str(data["lane_id"]),
            status=RunLedgerStatus(str(data["status"])),
            started_at=str(data["started_at"]),
            updated_at=str(data["updated_at"]),
            completed_at=(
                None
                if data.get("completed_at") is None
                else str(data["completed_at"])
            ),
            contract_version=ContractVersion.parse(str(data["contract_version"])),
            evidence_file_refs=[
                EvidenceFileRef.from_dict(cast(dict[str, str | None], item))
                for item in evidence_file_refs_data
                if isinstance(item, dict)
            ],
            provenance=RunLedgerProvenance.from_dict(
                cast(dict[str, str | None], dict(provenance_data))
            ),
        )
