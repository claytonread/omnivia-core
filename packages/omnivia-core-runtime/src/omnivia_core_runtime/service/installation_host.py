"""One fenced installation authority shared by all workspace service processes.

Workspace services remain independently leaseable: two workspaces in one
installation may be served at the same time. Installation mutations do not follow
that model. Exactly one process owns the machine-local installation catalogue,
its lifetime lock, fencing generation and private authority endpoint. Every
workspace service routes ``workspace.create`` and ``workspace.list`` through that
owner.

The first workspace service to acquire the catalogue lock hosts the authority
endpoint. A later service reads the bounded owner descriptor and becomes a proxy.
If the owner exits, the next proxied call re-runs the same lock election; only the
winner can advance the catalogue generation and publish a replacement endpoint.
No workspace service identity is treated as installation authority merely because
it participates in the election.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    RequestEnvelope,
    ResponseEnvelope,
)
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.application import (
    LOCAL_TRANSPORT_ADAPTER,
    ApplicationDispatcher,
    ApplicationFallback,
    build_installation_application_dispatcher,
    build_installation_registry,
    installation_owner_session,
)
from omnivia_core_runtime.service.authorization import ServiceBinding
from omnivia_core_runtime.service.handlers.workspace_family import (
    RemoteInstallationWorkspaceHandlers,
)
from omnivia_core_runtime.service.installation import InstallationApplicationService
from omnivia_core_runtime.service.operations import failure, server_capability_snapshot
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    EndpointScheme,
    LocalEndpoint,
    LocalSocketServer,
    LocalSocketTransport,
    TransportError,
    endpoint_for_path,
    parse_endpoint,
)
from omnivia_core_runtime.storage.backup import RUNTIME_DIR, InstallationLayout
from omnivia_core_runtime.storage.installation_store import (
    InstallationBusy,
    InstallationStore,
    open_installation_store,
)

AUTHORITY_DESCRIPTOR_FORMAT: Final = "omnivia.installation-authority.v1"
AUTHORITY_DESCRIPTOR_MAXIMUM_BYTES: Final = 16 * 1024
AUTHORITY_DESCRIPTOR_NAME: Final = "authority.json"
AUTHORITY_ENDPOINT_NAME: Final = "omnivia-ia"
AUTHORITY_RUNTIME_DIRECTORY: Final = "installation-authority"
AUTHORITY_START_TIMEOUT_SECONDS: Final = 15.0
AUTHORITY_FAILOVER_TIMEOUT_SECONDS: Final = 2.0
AUTHORITY_POLL_SECONDS: Final = 0.05
_UNAVAILABLE_MESSAGE: Final = "the installation authority is not available"


@dataclass(frozen=True)
class _AuthorityDescriptor:
    installation_id: str
    owner_instance_id: str
    endpoint: LocalEndpoint


class InstallationAuthorityFacts(Protocol):
    def probe_facts(self) -> ServiceFacts: ...


@dataclass
class InstallationAuthorityCoordinator:
    """Own or proxy the one installation application service for this process."""

    installation_root: Path
    workspace_storage_root: Path
    core_version: str
    clock: Clock
    owner_instance_id: str
    principal_id: str
    probe: ApplicationFallback
    facts: InstallationAuthorityFacts
    _mutex: threading.RLock = field(default_factory=threading.RLock, init=False)
    _store: InstallationStore | None = field(default=None, init=False)
    _local: ApplicationDispatcher | None = field(default=None, init=False)
    _server: LocalSocketServer | None = field(default=None, init=False)
    _descriptor: _AuthorityDescriptor | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    @property
    def _layout(self) -> InstallationLayout:
        return InstallationLayout(root=self.installation_root)

    @property
    def _descriptor_path(self) -> Path:
        return self._runtime_root / AUTHORITY_DESCRIPTOR_NAME

    @property
    def _endpoint(self) -> LocalEndpoint:
        # POSIX socket ceilings are much shorter than a valid installation path.
        # Hashing the normalized root gives every process the same short name
        # without advertising the path; the socket itself is mode 0600 and /tmp's
        # sticky bit prevents another user from replacing it. On Windows this
        # synthetic path is deterministically folded into the existing pipe name.
        if LOCAL_SCHEME is EndpointScheme.PIPE:
            return endpoint_for_path(
                self.installation_root / RUNTIME_DIR / AUTHORITY_ENDPOINT_NAME
            )
        key = os.path.normcase(os.path.abspath(str(self.installation_root)))
        uid = str(os.getuid()) if hasattr(os, "getuid") else "windows"
        digest = hashlib.sha256(f"{uid}\0{key}".encode()).hexdigest()[:24]
        return endpoint_for_path(
            Path("/tmp") / f"{AUTHORITY_ENDPOINT_NAME}-{digest}.sock"
        )

    @property
    def _runtime_root(self) -> Path:
        return self._layout.root / RUNTIME_DIR / AUTHORITY_RUNTIME_DIRECTORY

    def start(self) -> ApplicationDispatcher:
        """Join the current owner or become it, then return the proxy family."""
        deadline = time.monotonic() + AUTHORITY_START_TIMEOUT_SECONDS
        descriptor: _AuthorityDescriptor | None = None
        while descriptor is None and time.monotonic() < deadline:
            with self._mutex:
                if self._closed:
                    raise RuntimeError(_UNAVAILABLE_MESSAGE)
                if self._try_become_owner():
                    descriptor = self._descriptor
                else:
                    descriptor = self._read_descriptor()
            if descriptor is None:
                time.sleep(AUTHORITY_POLL_SECONDS)
        if descriptor is None:
            raise RuntimeError(_UNAVAILABLE_MESSAGE)
        return self._proxy_dispatcher(descriptor.installation_id)

    def forward(self, request: RequestEnvelope) -> ResponseEnvelope:
        """Send one authorized installation request to the current fenced owner."""
        deadline = time.monotonic() + AUTHORITY_FAILOVER_TIMEOUT_SECONDS
        while True:
            with self._mutex:
                local = self._local
                descriptor = self._descriptor or self._read_descriptor()
            if local is not None:
                return local.dispatch(request)
            response = self._remote_call(request, descriptor)
            if response is not None:
                return response

            # The published owner may have exited. Re-run the lock election; the
            # catalogue lock and generation decide the winner, not this
            # observation. A bounded retry bridges the old owner's short window
            # between stopping its endpoint and releasing the lifetime lock.
            with self._mutex:
                if not self._closed and self._try_become_owner():
                    local = self._local
                else:
                    local = self._local
            if local is not None:
                return local.dispatch(request)
            if self._closed or time.monotonic() >= deadline:
                break
            time.sleep(AUTHORITY_POLL_SECONDS)
        return failure(
            request,
            ERROR_CODE_DEPENDENCY_UNAVAILABLE,
            _UNAVAILABLE_MESSAGE,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_DEPENDENCY_UNAVAILABLE],
            principal=self.principal_id,
        )

    def close(self) -> None:
        """Stop the private endpoint before releasing catalogue authority."""
        with self._mutex:
            self._closed = True
            server = self._server
            store = self._store
            descriptor = self._descriptor
            self._server = None
            self._store = None
            self._local = None
            self._descriptor = None
        if server is not None:
            server.stop()
        if descriptor is not None:
            self._remove_descriptor(descriptor)
        if store is not None:
            store.close()

    def _try_become_owner(self) -> bool:
        if self._local is not None:
            return True
        try:
            store = open_installation_store(
                self.installation_root,
                owner_instance_id=self.owner_instance_id,
            )
        except InstallationBusy:
            return False

        service = InstallationApplicationService(
            store=store,
            installation_root=self.installation_root,
            workspace_storage_root=self.workspace_storage_root,
            core_version=self.core_version,
            clock=self.clock,
        )
        route = build_installation_application_dispatcher(
            service=service,
            principal_id=self.principal_id,
            fallback=self.probe,
        )
        router = DocumentRouter(
            probes=ProbeRouter(
                facts=self.facts.probe_facts,
                capabilities=tuple,
                clock=time.monotonic_ns,
            ),
            dispatch=route.dispatch,
        )
        server = LocalSocketServer(router=router, endpoint=self._endpoint)
        try:
            server.start()
            descriptor = _AuthorityDescriptor(
                installation_id=store.authority.installation_id,
                owner_instance_id=self.owner_instance_id,
                endpoint=self._endpoint,
            )
            self._write_descriptor(descriptor)
        except BaseException:
            server.stop()
            store.close()
            raise
        self._store = store
        self._local = route
        self._server = server
        self._descriptor = descriptor
        return True

    def _proxy_dispatcher(self, installation_id: str) -> ApplicationDispatcher:
        handlers = RemoteInstallationWorkspaceHandlers(forward=self.forward)
        registry = build_installation_registry(
            workspace_create=handlers.workspace_create,
            workspace_list=handlers.workspace_list,
        )
        session = installation_owner_session(
            principal_id=self.principal_id,
            installation_id=installation_id,
        )
        return ApplicationDispatcher(
            registry=registry,
            session=session,
            binding=ServiceBinding(installation_id=installation_id),
            supported_capabilities=server_capability_snapshot(registry),
            transport=LOCAL_TRANSPORT_ADAPTER,
            probe=self.probe,
            record=None,
            service=self,
        )

    @staticmethod
    def _remote_call(
        request: RequestEnvelope,
        descriptor: _AuthorityDescriptor | None,
    ) -> ResponseEnvelope | None:
        if descriptor is None:
            return None
        try:
            return LocalSocketTransport(endpoint=descriptor.endpoint).call(request)
        except TransportError:
            return None

    def _write_descriptor(self, descriptor: _AuthorityDescriptor) -> None:
        document = {
            "format": AUTHORITY_DESCRIPTOR_FORMAT,
            "installation_id": descriptor.installation_id,
            "owner_instance_id": descriptor.owner_instance_id,
            "endpoint_uri": descriptor.endpoint.url,
        }
        rendered = json.dumps(document, sort_keys=True, separators=(",", ":"))
        path = self._descriptor_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_descriptor(self) -> _AuthorityDescriptor | None:
        path = self._descriptor_path
        try:
            if (
                not path.is_file()
                or path.stat().st_size > AUTHORITY_DESCRIPTOR_MAXIMUM_BYTES
            ):
                return None
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) > AUTHORITY_DESCRIPTOR_MAXIMUM_BYTES:
            return None
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(document, dict) or set(document) != {
            "format",
            "installation_id",
            "owner_instance_id",
            "endpoint_uri",
        }:
            return None
        installation_id = document.get("installation_id")
        owner_instance_id = document.get("owner_instance_id")
        endpoint_uri = document.get("endpoint_uri")
        if (
            document.get("format") != AUTHORITY_DESCRIPTOR_FORMAT
            or not isinstance(installation_id, str)
            or not 1 <= len(installation_id) <= 128
            or not isinstance(owner_instance_id, str)
            or not 1 <= len(owner_instance_id) <= 128
            or not isinstance(endpoint_uri, str)
        ):
            return None
        endpoint = parse_endpoint(endpoint_uri)
        if endpoint is None or endpoint != self._endpoint:
            return None
        return _AuthorityDescriptor(
            installation_id=installation_id,
            owner_instance_id=owner_instance_id,
            endpoint=endpoint,
        )

    def _remove_descriptor(self, expected: _AuthorityDescriptor) -> None:
        current = self._read_descriptor()
        if current != expected:
            return
        try:
            self._descriptor_path.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "AUTHORITY_DESCRIPTOR_FORMAT",
    "AUTHORITY_DESCRIPTOR_MAXIMUM_BYTES",
    "AUTHORITY_DESCRIPTOR_NAME",
    "AUTHORITY_ENDPOINT_NAME",
    "AUTHORITY_FAILOVER_TIMEOUT_SECONDS",
    "AUTHORITY_RUNTIME_DIRECTORY",
    "InstallationAuthorityCoordinator",
    "InstallationAuthorityFacts",
]
