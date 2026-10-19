"""`engineering.source.capture.hint`: a trusted watcher's advisory wake for polling.

The hint carries two registered identities and nothing else. It wakes the next
service tick and puts the named checkout ahead of ordinary rotation, but Core's
periodic capture poll stays the durable source of truth, so every loss path here
(unregistered target, full set, restart, failing seam, unavailable checkout,
storage contention) is recovered by an unchanged poll and writes no verdict.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import test_working_tree_snapshot as snapshot_harness
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.application import (
    ENGINEERING_FAMILY_OPERATIONS,
    MUTATION_PURPOSES,
    OPERATION_PURPOSES,
    build_engineering_application_dispatcher,
    build_installation_application_dispatcher,
    engineering_family_session,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.engineering_source_capture_execution import (
    MAX_PENDING_HINTS,
    EngineeringSourceCaptureExecutor,
)
from omnivia_core_runtime.service.main import (
    LOCAL_PRINCIPAL,
    _build_production_application_surface,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.runner import ServiceRunner
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

_OPERATION = "engineering.source.capture.hint"
_PRINCIPAL = "local-user"
_ACK = {"acknowledged": True}


@dataclass
class _Live:
    env: snapshot_harness._Env
    runner: ServiceRunner
    executor: EngineeringSourceCaptureExecutor
    dispatcher: Any
    _count: int = 0

    def hint(
        self,
        payload: dict[str, object],
        *,
        scopes: tuple[str, ...] = ("engineering:source",),
        purpose: str = "engineering_source",
        capability: str = "engineering.source",
        dispatcher: Any = None,
    ) -> SuccessResponseEnvelope | ErrorResponseEnvelope:
        return (dispatcher or self.dispatcher).dispatch(self.request(
            payload, scopes=scopes, purpose=purpose, capability=capability
        ))

    def request(
        self,
        payload: dict[str, object],
        *,
        scopes: tuple[str, ...] = ("engineering:source",),
        purpose: str = "engineering_source",
        capability: str = "engineering.source",
    ) -> RequestEnvelope:
        self._count += 1
        request_id = f"hint-{self._count}"
        assert self.runner.workspace_id is not None
        return RequestEnvelope(
            operation=_OPERATION,
            metadata=RequestMetadata(
                request_id=request_id,
                correlation_id=f"cor-{request_id}",
                trace_id=f"trc-{request_id}",
                api_version=CONTRACT_VERSION,
                client=ClientIdentity(id="hint-test", version="1.0.0"),
                workspace_id=self.runner.workspace_id,
                scopes=scopes,
                purpose=purpose,
                required_capabilities=(
                    CapabilityRequirement(
                        id=capability, minimum_version="1.0", required=True
                    ),
                ),
                idempotency_key=None,
                mutation_precondition=None,
                principal_claim=None,
            ),
            input=payload,
        )

    def dispatcher_with(self, hint: Any) -> Any:
        return _dispatcher(self.runner, hint)

    def source_repositories(self) -> set[str]:
        assert self.runner.connection is not None
        return {
            str(row[0])
            for row in self.runner.connection.execute(
                "SELECT repository_id FROM omnivia_engineering_source_streams"
            )
        }

    def event_count(self) -> int:
        assert self.runner.connection is not None
        return int(
            self.runner.connection.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_source_events"
            ).fetchone()[0]
        )

    def audit_count(self) -> int:
        assert self.runner.connection is not None
        return int(
            self.runner.connection.execute(
                "SELECT COUNT(*) FROM omnivia_application_audit_events"
            ).fetchone()[0]
        )


def _dispatcher(runner: ServiceRunner, hint: Any) -> Any:
    assert runner.workspace_id is not None and runner.identity is not None
    fallback = Dispatcher.for_service_operations(
        Grant(
            principal=_PRINCIPAL,
            workspaces=frozenset({runner.workspace_id}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        None,
    )
    return build_engineering_application_dispatcher(
        service=runner,
        principal_id=_PRINCIPAL,
        installation_id=runner.identity.installation_id,
        workspace_id=runner.workspace_id,
        fallback=fallback,
        source_capture_hint=hint,
    )


@contextmanager
def _live(
    env: snapshot_harness._Env, *, poll_interval: float = 3600.0
) -> Iterator[_Live]:
    runner = env._runner()
    try:
        executor = EngineeringSourceCaptureExecutor(
            runner=runner,
            application=_dispatcher(runner, None),
            principal_id=_PRINCIPAL,
            poll_interval_seconds=poll_interval,
        )
        yield _Live(env, runner, executor, _dispatcher(runner, executor.hint))
    finally:
        runner.stop()


def _write(root: Path, text: str) -> None:
    (root / "a.py").write_bytes(text.encode())


def _ok(response: object) -> None:
    assert isinstance(response, SuccessResponseEnvelope), response
    assert dict(response.result) == _ACK


def _error_code(response: object) -> str:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response.error.code


def _pass(executor: EngineeringSourceCaptureExecutor, **kwargs: Any) -> tuple[int, int]:
    """`(captured, committed)`: the lanes also inspect their own settled seals."""
    result = executor.run_pending(**kwargs)
    return result.captured, result.committed


# --- request shape -------------------------------------------------------------


def test_the_request_refuses_extra_path_content_and_authority_fields(
    tmp_path: Path,
) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    base = {"repository_id": "repo-1", "checkout_id": "co-repo-1"}
    with _live(env) as live:
        for extra in (
            "checkout_root",
            "checkout_hint",
            "path",
            "file_path",
            "content",
            "bytes",
            "manifest",
            "command",
            "installation_id",
            "workspace_id",
            "principal_id",
            "purpose",
            "scopes",
            "roles",
            "capabilities",
            "payload",
        ):
            response = live.hint({**base, extra: str(root)})
            assert _error_code(response) == "invalid_request", extra
            assert str(root) not in json.dumps(response.to_wire())
        for incomplete in ({"repository_id": "repo-1"}, {"checkout_id": "co-repo-1"}, {}):
            assert _error_code(live.hint(incomplete)) == "invalid_request"
        for malformed in ("../repo", "", "a b", 7, None):
            assert (
                _error_code(live.hint({**base, "checkout_id": malformed}))
                == "invalid_request"
            )
        assert live.executor._hints == {}
        assert live.executor._wake is False


# --- authorization and route ---------------------------------------------------


def test_the_hint_needs_its_own_scope_purpose_capability_and_session_grant(
    tmp_path: Path,
) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    payload = {"repository_id": "repo-1", "checkout_id": "co-repo-1"}
    with _live(env) as live:
        assert live.runner.identity is not None and live.runner.workspace_id is not None
        _ok(live.hint(payload))
        assert live.executor._hints == {("repo-1", "co-repo-1"): None}
        live.executor._hints.clear()

        # Other grants a client might hold -- engineering reads, the repository
        # registration purpose, a different capability -- never cover this operation.
        refused = {
            "read scope": live.hint(payload, scopes=("engineering:read",)),
            "repository scope": live.hint(payload, scopes=("engineering:repository",)),
            "repository purpose": live.hint(payload, purpose="engineering_repository"),
            "search purpose": live.hint(payload, purpose="engineering_search"),
            "read capability": live.hint(payload, capability="engineering.read"),
        }
        for label, response in refused.items():
            assert isinstance(response, ErrorResponseEnvelope), label
        assert refused["repository purpose"].error.code == "invalid_purpose"
        assert refused["search purpose"].error.code == "invalid_purpose"
        assert live.executor._hints == {}

        # A transport-resolved session without the operation (an MCP, HTTP or other
        # non-local grant) is refused by the same seam; so is a missing session.
        family = engineering_family_session(
            principal_id=_PRINCIPAL,
            installation_id=live.runner.identity.installation_id,
            workspace_id=live.runner.workspace_id,
        )
        without = dataclasses.replace(
            family, operations=ENGINEERING_FAMILY_OPERATIONS - {_OPERATION}
        )
        denied = live.dispatcher.dispatch_for_session(live.request(payload), without)
        assert _error_code(denied) == "authorization_denied"
        unscoped = dataclasses.replace(
            family, scopes=family.scopes - {"engineering:source"}
        )
        assert isinstance(
            live.dispatcher.dispatch_for_session(live.request(payload), unscoped),
            ErrorResponseEnvelope,
        )
        anonymous = live.dispatcher.dispatch_without_session(live.request(payload))
        assert _error_code(anonymous) == "authentication_required"
        assert live.executor._hints == {}


def test_the_production_surface_routes_the_hint_to_the_executor(tmp_path: Path) -> None:
    """The composition `main()` uses: surface -> engineering route -> handler -> seam."""
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    with _live(env) as live:
        runner = live.runner
        assert runner.identity is not None and runner.workspace_id is not None
        probe = Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        installation_id = runner.identity.installation_id
        authority = type("_A", (), {"installation_id": installation_id})()
        surface = _build_production_application_surface(
            started=runner,
            probe=probe,
            installation=build_installation_application_dispatcher(
                service=type("_S", (), {"authority": authority})(),  # type: ignore[arg-type]
                principal_id=LOCAL_PRINCIPAL,
                fallback=probe,
            ),
            source_capture_hint=live.executor.hint,
        )
        _ok(surface.dispatch(live.request(
            {"repository_id": "repo-1", "checkout_id": "co-repo-1"}
        )))
        assert live.executor._hints == {("repo-1", "co-repo-1"): None}


def test_the_operation_is_a_read_class_purpose_not_a_mutation_purpose() -> None:
    assert _OPERATION in OPERATION_PURPOSES
    assert _OPERATION not in MUTATION_PURPOSES
    assert _OPERATION in ENGINEERING_FAMILY_OPERATIONS


# --- registration facts stay private -------------------------------------------


def test_unknown_and_foreign_targets_are_acknowledged_identically_and_not_forwarded(
    tmp_path: Path,
) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    env.register("repo-2", None)
    with _live(env) as live:
        runner = live.runner
        assert runner.connection and runner.identity and runner.generation
        ws = str(runner.workspace_id)
        # A checkout registered, but by another installation of the same workspace.
        with fenced_transaction(
            runner.connection,
            runner.identity,
            workspace_id=ws,
            fencing_generation=runner.generation,
        ) as c:
            audit = "aud-other-installation"
            c.execute(
                "INSERT INTO omnivia_application_audit_events (audit_ref, "
                "workspace_id, principal_id, operation, purpose, request_id, "
                "correlation_id, trace_id, granted_authority_json, outcome_class, "
                "error_code, recorded_at_us) VALUES (?, ?, 'p', 'o', 'p', 'r', "
                "'c', 't', '{}', 'succeeded', NULL, 1)",
                (audit, ws),
            )
            repository_identity.register_checkout(
                c,
                dataclasses.make_dataclass("S", [("audit_ref", str)])(audit),
                workspace_id=ws,
                checkout_id="co-foreign",
                repository_id="repo-1",
                installation_id="another-installation",
                checkout_hint="/somewhere/else",
                registered_at_us=1,
            )

        known = live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"})
        _ok(known)
        assert live.executor._hints == {("repo-1", "co-repo-1"): None}
        live.executor._hints.clear()

        audits_before = live.audit_count()
        events_before = live.event_count()
        unregistered = (
            {"repository_id": "repo-1", "checkout_id": "co-nope"},
            {"repository_id": "repo-nope", "checkout_id": "co-repo-1"},
            {"repository_id": "repo-nope", "checkout_id": "co-nope"},
            # Right pair of ids, wrong repository for that checkout.
            {"repository_id": "repo-2", "checkout_id": "co-repo-1"},
            # Registered on a different installation.
            {"repository_id": "repo-1", "checkout_id": "co-foreign"},
            # A registered repository whose checkout was never bound here.
            {"repository_id": "repo-2", "checkout_id": "co-repo-2"},
        )
        shapes = []
        for payload in unregistered:
            response = live.hint(payload)
            _ok(response)
            wire = json.dumps(response.to_wire(), sort_keys=True)
            assert str(root) not in wire and "/somewhere/else" not in wire
            shapes.append(dict(response.result))
        assert all(shape == dict(known.result) for shape in shapes)
        assert live.executor._hints == {}
        # Advisory only: no durable audit, source or failure row for any hint.
        assert live.audit_count() == audits_before
        assert live.event_count() == events_before


def test_a_missing_or_failing_seam_is_still_acknowledged(tmp_path: Path) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    payload = {"repository_id": "repo-1", "checkout_id": "co-repo-1"}
    calls: list[tuple[str, str]] = []

    def failing(repository_id: str, checkout_id: str) -> None:
        calls.append((repository_id, checkout_id))
        raise RuntimeError(f"boom {root}")

    with _live(env) as live:
        _ok(live.hint(payload, dispatcher=live.dispatcher_with(None)))
        response = live.hint(payload, dispatcher=live.dispatcher_with(failing))
        _ok(response)
        assert calls == [("repo-1", "co-repo-1")]
        assert str(root) not in json.dumps(response.to_wire())


# --- wake, coalescing, bound ---------------------------------------------------


def test_a_hint_wakes_the_next_pass_inside_the_poll_interval(tmp_path: Path) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    with _live(env, poll_interval=3600.0) as live:
        executor = live.executor
        assert _pass(executor) == (1, 1)  # first pass: nothing scheduled yet
        _write(root, "changed-one\n")
        assert _pass(executor) == (0, 0)  # inside the interval: still asleep
        scheduled = executor._next_poll

        _ok(live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"}))
        assert _pass(executor) == (1, 1)  # woken immediately, no force
        assert live.event_count() == 2
        # One-shot: the hint is consumed, and the poll schedule is untouched, so
        # the periodic poll keeps its own cadence.
        assert executor._hints == {}
        assert executor._next_poll == scheduled
        assert _pass(executor) == (0, 0)


def test_duplicate_bursts_coalesce_and_the_set_stays_bounded(tmp_path: Path) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    with _live(env) as live:
        executor = live.executor
        for _ in range(25):
            _ok(live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"}))
        assert executor._hints == {("repo-1", "co-repo-1"): None}

        for index in range(MAX_PENDING_HINTS * 3):
            executor.hint("repo-x", f"co-{index}")
        assert len(executor._hints) == MAX_PENDING_HINTS
        assert executor._wake is True  # a full set still wakes: polling recovers

        # Concurrent request threads cannot grow it past the bound or corrupt it.
        executor._hints.clear()
        errors: list[BaseException] = []

        def burst(offset: int) -> None:
            try:
                for index in range(300):
                    executor.hint("repo-t", f"co-{offset}-{index % 40}")
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        threads = [threading.Thread(target=burst, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert 0 < len(executor._hints) <= MAX_PENDING_HINTS


# --- prioritisation without starvation -----------------------------------------


def _two_checkouts(tmp_path: Path) -> tuple[snapshot_harness._Env, Path, Path]:
    env = snapshot_harness._Env(tmp_path)
    root_a = snapshot_harness._repo(tmp_path, "repo-a")
    root_b = snapshot_harness._repo(tmp_path, "repo-b")
    env.register("repo-1", root_a)
    env.register("repo-2", root_b)
    return env, root_a, root_b


def test_the_hinted_checkout_goes_before_rotation_and_rotation_is_not_starved(
    tmp_path: Path,
) -> None:
    env, _root_a, root_b = _two_checkouts(tmp_path)
    hint_b = {"repository_id": "repo-2", "checkout_id": "co-repo-2"}
    with _live(env, poll_interval=3600.0) as live:
        executor = live.executor
        # Rotation's first stop is checkout `co-repo-1`; the hint moves repo-2 first.
        _ok(live.hint(hint_b))
        assert executor.run_pending(budget=1).inspected == 1
        assert live.source_repositories() == {"repo-2"}

        # A hint burst on repo-2 cannot starve repo-1: the checkout lane alternates
        # a hinted unit with a rotation unit, so repo-1 is reached in two passes.
        for revision in range(2):
            _write(root_b, f"b-{revision}\n")
            _ok(live.hint(hint_b))
            _pass(executor, budget=1)
        assert live.source_repositories() == {"repo-1", "repo-2"}


def test_a_hint_never_takes_the_recovery_lanes_unit(tmp_path: Path) -> None:
    env, root_a, root_b = _two_checkouts(tmp_path)
    # A sealed, never-committed capture of repo-1 is recovery-lane work.
    env.snapshot("repo-1", root_a, "captured-sealed-a")
    _write(root_b, "b-hinted\n")
    with _live(env, poll_interval=3600.0) as live:
        _ok(live.hint({"repository_id": "repo-2", "checkout_id": "co-repo-2"}))
        result = live.executor.run_pending(budget=2)
        # Both lanes were served inside the one budget of two units: the sealed
        # capture was recovered and the hinted checkout was captured.
        assert result.inspected <= 2
        assert live.source_repositories() == {"repo-1", "repo-2"}


# --- recovery by the unchanged poll --------------------------------------------


def test_a_failing_seam_loses_the_hint_and_polling_still_captures(tmp_path: Path) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)

    def failing(repository_id: str, checkout_id: str) -> None:
        raise RuntimeError("seam down")

    with _live(env, poll_interval=0.0) as live:
        _ok(live.hint(
            {"repository_id": "repo-1", "checkout_id": "co-repo-1"},
            dispatcher=live.dispatcher_with(failing),
        ))
        assert live.executor._hints == {}
        assert _pass(live.executor) == (1, 1)  # the ordinary poll found it
        assert live.event_count() == 1


def test_a_restart_loses_pending_hints_and_the_poll_recovers(tmp_path: Path) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    with _live(env) as live:
        _ok(live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"}))
        assert live.executor._hints
    # The hint lived only in the dead process's memory; nothing durable names it.
    with _live(env) as restarted:
        assert restarted.executor._hints == {}
        assert restarted.event_count() == 0
        assert _pass(restarted.executor) == (1, 1)


def test_an_unavailable_checkout_writes_no_verdict_and_is_polled_later(
    tmp_path: Path,
) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    parked = tmp_path / "parked"
    with _live(env, poll_interval=3600.0) as live:
        shutil.move(root, parked)
        _ok(live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"}))
        audits = live.audit_count()
        assert _pass(live.executor, budget=2) == (0, 0)  # no capture, no raise
        assert live.event_count() == 0 and live.audit_count() == audits
        assert live.executor._hints == {}

        shutil.move(parked, root)
        assert _pass(live.executor, force=True) == (1, 1)
        assert live.event_count() == 1


def test_storage_contention_on_a_hinted_pass_is_recovered_by_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    with _live(env, poll_interval=3600.0) as live:
        executor = live.executor
        _ok(live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"}))

        def locked() -> None:
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as patch:
            patch.setattr(executor, "_take_hinted_checkout", locked)
            assert _pass(executor) == (0, 0)  # swallowed, not raised
        assert live.event_count() == 0
        assert _pass(executor, force=True) == (1, 1)


def test_a_full_hint_set_falls_back_to_polling_without_dropping_safety(
    tmp_path: Path,
) -> None:
    env = snapshot_harness._Env(tmp_path)
    root = snapshot_harness._repo(tmp_path)
    env.register("repo-1", root)
    with _live(env, poll_interval=3600.0) as live:
        executor = live.executor
        assert _pass(executor) == (1, 1)
        _write(root, "after-flood\n")
        for index in range(MAX_PENDING_HINTS):
            executor.hint("repo-x", f"co-{index}")
        # The real hint is dropped (the set is full), yet it still wakes a pass.
        _ok(live.hint({"repository_id": "repo-1", "checkout_id": "co-repo-1"}))
        assert ("repo-1", "co-repo-1") not in executor._hints
        assert len(executor._hints) == MAX_PENDING_HINTS
        # Bogus identities are discarded without spending units, and ordinary
        # rotation reaches the dropped hint's checkout in the same woken pass.
        assert _pass(executor) == (1, 1)
        assert executor._hints == {}
        assert live.event_count() == 2
