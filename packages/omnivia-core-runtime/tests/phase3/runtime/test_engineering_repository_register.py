"""`engineering.repository.register`: the production registration vertical.

The path under test is the production one: a real, filesystem-backed
installation (`initialise_workspace`), a real `ServiceRunner` acquiring the
workspace lease exactly as a fresh CLI invocation would, and the engineering
family's `ApplicationDispatcher` built exactly as `service.main.serve` builds
it -- one request envelope in, through the same authorization seam and the
same fenced mutation coordinator every other family uses, and out again as a
canonical response envelope. No storage function is ever called directly to
seed a repository or a checkout: every row this module inspects was written
by that one operation.

The claims exercised, per the registration packet:

- a real registration binds a checkout that `capture_working_tree_snapshot`
  can then use, and refuses to use one that was never bound (§6.2-§6.3);
- retrying the identical request under the identical idempotency key replays
  the same result, and repeating the same content under a fresh key is a
  content-level no-op -- neither writes a second row;
- two repositories may share a display name, and stay ambiguous by label
  alone (§6.2);
- naming an already-registered repository id under different metadata is a
  conflict, never a silent overwrite;
- re-registering an already-bound path under a different repository is an
  audited rebind, reported as such, never silent;
- a relative path, a `..`-bearing one, a missing directory and a symlinked
  one are all refused before anything is written;
- a payload cannot smuggle a workspace or an installation id: the row is
  always written under the authenticated context's own;
- the identity tables refuse any write that does not hold the service's
  fencing guard, exactly as every other table migration 0047 governs does.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.application import (
    build_engineering_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    capture_working_tree_snapshot,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import repository_identity

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    RequestEnvelope,
    RequestMetadata,
    SuccessResponseEnvelope,
)

pytestmark = pytest.mark.skipif(
    not source_capture._NO_FOLLOW_WALK, reason="host lacks no-follow checkout walk"
)

OPERATION = "engineering.repository.register"
SCOPE = "engineering:repository"
CAPABILITY = "engineering.repository"
PURPOSE = "engineering_repository"
PRINCIPAL = "local-user"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "a.py").write_bytes(b"print('a')\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, env=_GIT_ENV, check=True)
    return root


def _clone(source: Path, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", "--no-local", os.fspath(source), os.fspath(destination)],
        env=_GIT_ENV,
        check=True,
    )
    return destination


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _Env:
    """One real, filesystem-backed installation behind the production registry."""

    def __init__(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.installation = tmp_path / "installation-state"
        initialise_workspace(
            workspace_root=self.workspace,
            installation_root=self.installation,
            core_version=SERVER_VERSION,
        )
        self._requests = 0
        runner = self._runner()
        self.workspace_id = runner.workspace_id
        self.installation_id = runner.identity.installation_id
        runner.stop()

    def _runner(self) -> ServiceRunner:
        runner = ServiceRunner(
            ServiceSettings(
                workspace_root=self.workspace,
                installation_root=self.installation,
                core_version=SERVER_VERSION,
                endpoint=None,
            )
        )
        report = runner.start()
        assert report.ready, report.reason
        return runner

    def register(
        self,
        *,
        repository_id: str,
        display_name: str,
        checkout_root: str,
        provider_hint: str | None = None,
        idempotency_key: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        """Register through the real operation: one lease, one request, released.

        A fresh `ServiceRunner` per call, exactly as a fresh CLI invocation would
        acquire the workspace lease, dispatch one request envelope through the
        real authorization and fenced-mutation seam, and release it. Nothing here
        calls `storage.repository_identity` directly.
        """
        runner = self._runner()
        try:
            probe = Dispatcher.for_service_operations(
                Grant(
                    principal=PRINCIPAL,
                    workspaces=frozenset({self.workspace_id}),
                    operations=frozenset(SERVICE_OPERATIONS),
                ),
                None,
            )
            dispatcher = build_engineering_application_dispatcher(
                service=runner,
                principal_id=PRINCIPAL,
                installation_id=self.installation_id,
                workspace_id=self.workspace_id,
                fallback=probe,
            )
            self._requests += 1
            request_id = f"req-repo-register-{self._requests}"
            payload: dict[str, Any] = {
                "repository_id": repository_id,
                "display_name": display_name,
                "checkout_root": checkout_root,
            }
            if provider_hint is not None:
                payload["provider_hint"] = provider_hint
            if extra:
                payload.update(extra)
            envelope = RequestEnvelope(
                operation=OPERATION,
                metadata=RequestMetadata(
                    request_id=request_id,
                    correlation_id=f"cor-{request_id}",
                    trace_id=f"trc-{request_id}",
                    api_version=CONTRACT_VERSION,
                    client=CLIENT,
                    workspace_id=self.workspace_id,
                    scopes=(SCOPE,),
                    purpose=PURPOSE,
                    required_capabilities=(
                        CapabilityRequirement(
                            id=CAPABILITY, minimum_version="1.0", required=True
                        ),
                    ),
                    idempotency_key=idempotency_key or f"idem-{request_id}",
                    mutation_precondition=None,
                    principal_claim=None,
                ),
                input=payload,
            )
            return dispatcher.dispatch(envelope)
        finally:
            runner.stop()

    def snapshot(self, repository_id: str, checkout: Path, snapshot_id: str = "snap-1") -> Any:
        return capture_working_tree_snapshot(
            workspace_root=self.workspace,
            installation_root=self.installation,
            repository_id=repository_id,
            checkout_root=checkout,
            snapshot_id=snapshot_id,
            core_version=SERVER_VERSION,
        )

    def checkout_owner(self, checkout_root: str) -> str | None:
        connection = sqlite3.connect(self.workspace / "workspace.sqlite")
        try:
            row = connection.execute(
                "SELECT repository_id FROM omnivia_engineering_checkouts "
                "WHERE workspace_id = ? AND installation_id = ? AND checkout_hint = ?",
                (self.workspace_id, self.installation_id, checkout_root),
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            connection.close()

    def checkout_binding(self, checkout_root: str) -> tuple[str, str] | None:
        connection = sqlite3.connect(self.workspace / "workspace.sqlite")
        try:
            row = connection.execute(
                "SELECT repository_id, audit_ref FROM omnivia_engineering_checkouts "
                "WHERE workspace_id = ? AND installation_id = ? AND checkout_hint = ?",
                (self.workspace_id, self.installation_id, checkout_root),
            ).fetchone()
            return None if row is None else (str(row[0]), str(row[1]))
        finally:
            connection.close()

    def repository_metadata(self, repository_id: str) -> tuple[str, str | None] | None:
        connection = sqlite3.connect(self.workspace / "workspace.sqlite")
        try:
            row = connection.execute(
                "SELECT display_name, provider_hint FROM omnivia_engineering_repositories "
                "WHERE workspace_id = ? AND repository_id = ?",
                (self.workspace_id, repository_id),
            ).fetchone()
            return None if row is None else (str(row[0]), row[1])
        finally:
            connection.close()


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


def _result(response: Any) -> dict[str, Any]:
    assert isinstance(response, SuccessResponseEnvelope), response
    return dict(response.to_wire()["result"])


def _error(response: Any) -> tuple[str, str]:
    assert isinstance(response, ErrorResponseEnvelope), response
    return str(response.error.code), str(response.error.message)


def test_register_then_capture_working_tree_snapshot_end_to_end(
    env: _Env, tmp_path: Path
) -> None:
    checkout = _repo(tmp_path, "e2e-repo")
    result = _result(
        env.register(
            repository_id="erepo-e2e",
            display_name="Registration Vertical",
            checkout_root=os.fspath(checkout),
        )
    )
    assert result["repository_id"] == "erepo-e2e"
    assert result["repository_disposition"] == "registered"
    assert result["checkout_disposition"] == "bound"
    # The installation-local path is never echoed back in the result.
    assert "checkout_root" not in result and str(checkout) not in repr(result)
    assert env.checkout_owner(os.fspath(checkout)) == "erepo-e2e"

    outcome = env.snapshot("erepo-e2e", checkout)
    assert outcome.status == "captured"
    assert outcome.capture_status == "complete"
    assert outcome.file_count == 1


def test_capture_without_registration_is_refused(env: _Env, tmp_path: Path) -> None:
    checkout = _repo(tmp_path, "unregistered-repo")
    with pytest.raises(SourceCaptureRefused, match="repository is not registered"):
        env.snapshot("erepo-unregistered", checkout)


def test_replay_with_the_same_idempotency_key_returns_the_same_result(
    env: _Env, tmp_path: Path
) -> None:
    checkout = _repo(tmp_path, "replay-repo")
    first = _result(
        env.register(
            repository_id="erepo-replay",
            display_name="Replay",
            checkout_root=os.fspath(checkout),
            idempotency_key="idem-fixed-replay",
        )
    )
    second = _result(
        env.register(
            repository_id="erepo-replay",
            display_name="Replay",
            checkout_root=os.fspath(checkout),
            idempotency_key="idem-fixed-replay",
        )
    )
    assert first == second
    assert first["repository_disposition"] == "registered"
    assert first["checkout_disposition"] == "bound"


def test_repeating_registration_under_a_fresh_key_is_a_content_level_no_op(
    env: _Env, tmp_path: Path
) -> None:
    checkout = _repo(tmp_path, "noop-repo")
    first = _result(
        env.register(
            repository_id="erepo-noop",
            display_name="No-op",
            checkout_root=os.fspath(checkout),
        )
    )
    second = _result(
        env.register(
            repository_id="erepo-noop",
            display_name="No-op",
            checkout_root=os.fspath(checkout),
        )
    )
    assert first["repository_disposition"] == "registered"
    assert first["checkout_disposition"] == "bound"
    assert second["repository_disposition"] == "already_registered"
    assert second["checkout_disposition"] == "already_bound"
    assert first["checkout_id"] == second["checkout_id"]


def test_duplicate_basenames_stay_distinct_and_ambiguous_by_label_alone(
    env: _Env, tmp_path: Path
) -> None:
    checkout_a = _repo(tmp_path, "dup-a")
    checkout_b = _repo(tmp_path, "dup-b")
    _result(
        env.register(
            repository_id="erepo-dup-a",
            display_name="shared-name",
            checkout_root=os.fspath(checkout_a),
        )
    )
    _result(
        env.register(
            repository_id="erepo-dup-b",
            display_name="shared-name",
            checkout_root=os.fspath(checkout_b),
        )
    )
    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        with pytest.raises(repository_identity.RepositoryAmbiguous):
            repository_identity.resolve_repository(
                connection, workspace_id=env.workspace_id, label="shared-name"
            )
    finally:
        connection.close()


def test_clones_and_a_fork_require_an_explicit_authorized_reconciliation(
    env: _Env, tmp_path: Path
) -> None:
    """AC-011: shared Git metadata and history are discovery hints, not identity.

    Three independent materialisations share one origin and an initial commit.
    The fork then diverges. Registration keeps all three logical identities
    distinct because the service never infers authority from Git data. Only a
    later authorized registration request may reconcile one clone, and that
    change is returned as an audited rebind while the fork remains separate.
    """
    upstream = _repo(tmp_path, "upstream")
    clone_a = _clone(upstream, tmp_path / "clone-a")
    clone_b = _clone(upstream, tmp_path / "clone-b")
    fork = _clone(upstream, tmp_path / "fork")
    (fork / "fork_only.py").write_bytes(b"FORK = True\n")
    subprocess.run(["git", "add", "."], cwd=fork, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fork"],
        cwd=fork,
        env=_GIT_ENV,
        check=True,
    )

    origin = _git(clone_a, "config", "--get", "remote.origin.url")
    assert _git(clone_b, "config", "--get", "remote.origin.url") == origin
    assert _git(fork, "config", "--get", "remote.origin.url") == origin
    shared_commit = _git(clone_a, "rev-parse", "HEAD")
    assert _git(clone_b, "rev-parse", "HEAD") == shared_commit
    assert _git(fork, "rev-parse", "HEAD^") == shared_commit

    provider_hint = "provider://same-owner/same-repository"
    _result(
        env.register(
            repository_id="erepo-clone-a",
            display_name="shared-origin",
            provider_hint=provider_hint,
            checkout_root=os.fspath(clone_a),
        )
    )
    _result(
        env.register(
            repository_id="erepo-clone-b",
            display_name="shared-origin",
            provider_hint=provider_hint,
            checkout_root=os.fspath(clone_b),
        )
    )
    _result(
        env.register(
            repository_id="erepo-fork",
            display_name="shared-origin",
            provider_hint=provider_hint,
            checkout_root=os.fspath(fork),
        )
    )

    assert env.checkout_owner(os.fspath(clone_a)) == "erepo-clone-a"
    assert env.checkout_owner(os.fspath(clone_b)) == "erepo-clone-b"
    assert env.checkout_owner(os.fspath(fork)) == "erepo-fork"
    before = env.checkout_binding(os.fspath(clone_b))
    assert before is not None

    reconciled = _result(
        env.register(
            repository_id="erepo-clone-a",
            display_name="shared-origin",
            provider_hint=provider_hint,
            checkout_root=os.fspath(clone_b),
        )
    )
    assert reconciled["repository_disposition"] == "already_registered"
    assert reconciled["checkout_disposition"] == "rebound"
    after = env.checkout_binding(os.fspath(clone_b))
    assert after is not None
    assert after[0] == "erepo-clone-a"
    assert after[1] != before[1]
    assert env.checkout_owner(os.fspath(fork)) == "erepo-fork"


def test_reregistering_a_repository_id_under_different_metadata_is_a_conflict(
    env: _Env, tmp_path: Path
) -> None:
    checkout = _repo(tmp_path, "conflict-repo")
    _result(
        env.register(
            repository_id="erepo-conflict",
            display_name="Original Name",
            checkout_root=os.fspath(checkout),
        )
    )
    code, _message = _error(
        env.register(
            repository_id="erepo-conflict",
            display_name="Different Name",
            checkout_root=os.fspath(checkout),
        )
    )
    assert code == "conflict"
    assert env.repository_metadata("erepo-conflict") == ("Original Name", None)


def test_rebinding_an_already_bound_checkout_to_another_repository_is_an_audited_move(
    env: _Env, tmp_path: Path
) -> None:
    checkout = _repo(tmp_path, "move-repo")
    _result(
        env.register(
            repository_id="erepo-move-a",
            display_name="Move A",
            checkout_root=os.fspath(checkout),
        )
    )
    result = _result(
        env.register(
            repository_id="erepo-move-b",
            display_name="Move B",
            checkout_root=os.fspath(checkout),
        )
    )
    assert result["checkout_disposition"] == "rebound"
    assert env.checkout_owner(os.fspath(checkout)) == "erepo-move-b"


@pytest.mark.parametrize(
    "checkout_root",
    [
        "relative/checkout/path",
        "../etc",
        "/no/such/checkout/path/at/all",
    ],
    ids=["relative", "traversal", "missing"],
)
def test_malicious_or_invalid_checkout_hints_are_refused(
    env: _Env, checkout_root: str
) -> None:
    code, _message = _error(
        env.register(
            repository_id="erepo-bad-hint",
            display_name="Bad Hint",
            checkout_root=checkout_root,
        )
    )
    assert code == "invalid_request"
    assert env.repository_metadata("erepo-bad-hint") is None


def test_a_symlinked_checkout_root_is_refused(env: _Env, tmp_path: Path) -> None:
    real = _repo(tmp_path, "symlink-target")
    link = tmp_path / "symlink-checkout"
    link.symlink_to(real)
    code, _message = _error(
        env.register(
            repository_id="erepo-symlink",
            display_name="Symlink",
            checkout_root=os.fspath(link),
        )
    )
    assert code == "invalid_request"
    assert env.repository_metadata("erepo-symlink") is None


def test_workspace_and_installation_smuggled_in_the_payload_are_refused(
    env: _Env, tmp_path: Path
) -> None:
    """`workspace_id`/`installation_id` are not declared fields of the register
    input; the schema's `unevaluatedProperties: false` refuses them, and this
    operation validates the raw payload's keys itself rather than trusting the
    tolerant decoder to drop them, so the request is refused before anything is
    read from storage or written to it -- never silently accepted with the
    claimed identity ignored.
    """
    checkout = _repo(tmp_path, "no-smuggling")
    code, _message = _error(
        env.register(
            repository_id="erepo-no-smuggle",
            display_name="No Smuggling",
            checkout_root=os.fspath(checkout),
            extra={"workspace_id": "evil-workspace", "installation_id": "evil-installation"},
        )
    )
    assert code == "invalid_request"
    assert env.repository_metadata("erepo-no-smuggle") is None
    assert env.checkout_owner(os.fspath(checkout)) is None


def test_an_arbitrary_unknown_key_is_refused(env: _Env, tmp_path: Path) -> None:
    checkout = _repo(tmp_path, "unknown-key")
    code, _message = _error(
        env.register(
            repository_id="erepo-unknown-key",
            display_name="Unknown Key",
            checkout_root=os.fspath(checkout),
            extra={"some_unexpected_field": "anything"},
        )
    )
    assert code == "invalid_request"
    assert env.repository_metadata("erepo-unknown-key") is None
    assert env.checkout_owner(os.fspath(checkout)) is None


def test_no_direct_unguarded_write_reaches_the_identity_tables(env: _Env) -> None:
    """The adapter is the only writer: a direct storage call outside a fence is
    refused before it ever reaches a row, exactly as every other governed table
    migration 0047 covers is. The connection's own authorizer refuses the
    unguarded statement outright; a connection without one would still hit the
    migration's trigger and its `unguarded INSERT` abort -- either is a refusal,
    never a write.
    """
    runner = env._runner()
    try:
        with pytest.raises(
            sqlite3.DatabaseError,
            match="unguarded INSERT on omnivia_engineering_repositories|not authorized",
        ):
            repository_identity.register_repository(
                runner.connection,
                SimpleNamespace(audit_ref="aud-illegitimate"),
                workspace_id=env.workspace_id,
                repository_id="erepo-illegitimate",
                display_name="Illegitimate",
                provider_hint=None,
                registered_at_us=1,
            )
    finally:
        runner.stop()
    assert env.repository_metadata("erepo-illegitimate") is None
