"""Service discovery and readiness advertisement (T-0629E, ADR-037).

Discovery lives in installation-local runtime state, never in the portable
workspace: an endpoint describes the machine, and a copied workspace must not
advertise the machine that made it.

Two properties matter more than the file format.

Publication is atomic. A partially written descriptor would let a client connect to
an endpoint that does not exist yet, so the descriptor is written to a temporary
file and renamed.

Cleanup is compare-by-instance. A failed startup must remove *its own* descriptor
and nothing else. Unconditional deletion is the bug this guards against: a service
that fails while another instance is already ready would otherwise un-advertise the
healthy owner (BD-10).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

DESCRIPTOR_NAME = "service.json"


class ReadinessState(str, Enum):
    """What a discovered service is advertising."""

    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"


@dataclass(frozen=True)
class ServiceDescriptor:
    """A discoverable service instance."""

    workspace_id: str
    service_instance_id: str
    installation_id: str
    fencing_generation: int
    endpoint: str
    readiness: str
    api_version: str
    workspace_format_version: str
    pid: int

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "service_instance_id": self.service_instance_id,
            "installation_id": self.installation_id,
            "fencing_generation": self.fencing_generation,
            "endpoint": self.endpoint,
            "readiness": self.readiness,
            "api_version": self.api_version,
            "workspace_format_version": self.workspace_format_version,
            "pid": self.pid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ServiceDescriptor:
        return cls(
            workspace_id=str(data["workspace_id"]),
            service_instance_id=str(data["service_instance_id"]),
            installation_id=str(data["installation_id"]),
            fencing_generation=int(str(data["fencing_generation"])),
            endpoint=str(data["endpoint"]),
            readiness=str(data["readiness"]),
            api_version=str(data["api_version"]),
            workspace_format_version=str(data["workspace_format_version"]),
            pid=int(str(data["pid"])),
        )

    @property
    def ready(self) -> bool:
        return self.readiness == ReadinessState.READY.value


def descriptor_path(runtime_directory: Path) -> Path:
    return runtime_directory / DESCRIPTOR_NAME


def publish(runtime_directory: Path, descriptor: ServiceDescriptor) -> Path:
    """Atomically publish a descriptor.

    Written to a uniquely named temporary file and renamed, so two instances
    publishing concurrently cannot interleave into one another's bytes and no
    reader ever sees a partial document.
    """
    runtime_directory.mkdir(parents=True, exist_ok=True)
    target = descriptor_path(runtime_directory)
    temporary = target.with_name(f".{DESCRIPTOR_NAME}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(descriptor.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return target


def discover(runtime_directory: Path) -> ServiceDescriptor | None:
    """Read the current descriptor, or None when absent or unreadable.

    A malformed descriptor is treated as absent rather than raising: a client's
    correct response to garbage is to start a service, not to crash.
    """
    path = descriptor_path(runtime_directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ServiceDescriptor.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def compare_and_clean(runtime_directory: Path, service_instance_id: str) -> bool:
    """Remove the descriptor only if it belongs to `service_instance_id`.

    Returns True when a descriptor was removed. A failing instance calls this during
    cleanup; because it compares first, a service that fails while a *different*
    instance is ready cannot un-advertise the healthy owner.
    """
    current = discover(runtime_directory)
    if current is None or current.service_instance_id != service_instance_id:
        return False
    try:
        descriptor_path(runtime_directory).unlink()
    except OSError:  # pragma: no cover - platform dependent
        return False
    return True


def is_compatible(
    descriptor: ServiceDescriptor,
    *,
    api_version: str,
    workspace_format_version: str,
) -> bool:
    """Whether a discovered service is one this client may use.

    A running service of the wrong API or workspace-format version is not a service
    this client can use, so discovery must not treat it as one — otherwise a client
    would connect and then fail on every call.
    """
    return (
        descriptor.api_version == api_version
        and descriptor.workspace_format_version == workspace_format_version
    )


__all__ = [
    "DESCRIPTOR_NAME",
    "ReadinessState",
    "ServiceDescriptor",
    "compare_and_clean",
    "descriptor_path",
    "discover",
    "is_compatible",
    "publish",
]
