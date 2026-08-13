"""The safe projection itself, without a service to run.

`test_lifecycle.py` drives the real commands against a real process and is the
only place "this is what `omnivia status --json` prints" is established. What it
cannot reach cheaply is the shape of the projection under the inputs that make it
fail closed -- a manifest that is a megabyte of nothing, a home reached through a
symlink, an incompatible owner -- so those live here, in-process and in
milliseconds.

**Nothing in this file imports the runtime.** It is the same boundary
`test_package_boundaries.py` enforces for the package under test, and a fixture
that reached across it to build a workspace would be proving the projection works
only where the runtime is installed.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli.lifecycle import Installation
from omnivia_core_cli.safe_status import (
    DISPLAY_NAME,
    ENDPOINT_PROFILE_REF_PREFIX,
    MAX_MANIFEST_BYTES,
    TARGET_REF_PREFIX,
    degraded_status,
    incompatible_status,
    live_status,
    normalized_lifecycle,
    resolve_local_target,
    stopped_status,
    unreachable_status,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CoreSafeStatusV1,
    decode_core_safe_status,
    to_canonical_json,
)

WORKSPACE_ID = "ws-safe-status-tests-01"

#: Every key the v1 document published that v2 must not, at any depth, plus the
#: free-form fields a future edit might reach for. A machine caller gets fixed
#: codes and the contract's own vocabulary, and nothing that was assembled here
#: from a path, an endpoint, a launcher result or an exception.
FORBIDDEN_KEYS = frozenset(
    {
        "child_output",
        "credentials",
        "details",
        "endpoint",
        "endpoint_uri",
        "error",
        "exception",
        "home",
        "message",
        "path",
        "pid",
        "process",
        "reason",
        "service",
        "service_instance_id",
        "stderr",
        "traceback",
        "unmet",
        "workspace_id",
    }
)


def _write_manifest(home: Path, body: str | bytes) -> None:
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    manifest = workspace / "workspace.json"
    if isinstance(body, bytes):
        manifest.write_bytes(body)
    else:
        manifest.write_text(body, encoding="utf-8")


def _bootstrap(home: Path, workspace_id: str = WORKSPACE_ID) -> Installation:
    """The two fields the projection reads, and nothing else.

    Deliberately not a real manifest: `resolve_local_target` opens the portable
    document and reads `manifest_version` and `workspace_id` from it. Writing a
    complete one here would suggest it reads more, and would put this file's
    fixture in the business of tracking a schema it does not use.
    """
    _write_manifest(home, json.dumps({"manifest_version": "1", "workspace_id": workspace_id}))
    return Installation(home=home)


def _keys(document: Any) -> Iterator[str]:
    """Every key in a decoded document, at every depth."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield key
            yield from _keys(value)
    elif isinstance(document, list):
        for item in document:
            yield from _keys(item)


def _strings(document: Any) -> Iterator[str]:
    if isinstance(document, dict):
        for value in document.values():
            yield from _strings(value)
    elif isinstance(document, list):
        for item in document:
            yield from _strings(item)
    elif isinstance(document, str):
        yield document


def _assert_publishable(document: Any, *, sentinels: tuple[str, ...] = ()) -> None:
    offending = FORBIDDEN_KEYS & set(_keys(document))
    assert not offending, offending
    for value in _strings(document):
        for sentinel in sentinels:
            assert sentinel not in value, (sentinel, value)


def test_normalized_lifecycle_narrows_every_runtime_state() -> None:
    """The runtime's nine states, and the tenth thing a newer one might send."""
    assert normalized_lifecycle("starting") == "starting"
    assert normalized_lifecycle("recovering") == "starting"
    assert normalized_lifecycle("migrating") == "starting"
    assert normalized_lifecycle("ready") == "running"
    assert normalized_lifecycle("running") == "running"
    assert normalized_lifecycle("maintenance") == "running"
    assert normalized_lifecycle("draining") == "stopping"
    assert normalized_lifecycle("stopped") == "stopped"
    assert normalized_lifecycle("failed") == "failed"
    assert normalized_lifecycle("quiescing") == "unknown"
    assert normalized_lifecycle(None) == "unknown"
    assert normalized_lifecycle(7) == "unknown"


def test_a_resolved_target_is_local_locally_managed_and_named_for_neither(
    tmp_path: Path,
) -> None:
    target = resolve_local_target(_bootstrap(tmp_path / "home"))

    assert target is not None
    assert target.kind == "local"
    assert target.management == "locally_managed"
    assert target.display_name == DISPLAY_NAME
    assert target.workspace_ref == WORKSPACE_ID
    assert target.contract_version == CONTRACT_VERSION


def test_the_two_references_are_stable_opaque_and_distinct(tmp_path: Path) -> None:
    """Same installation, same references; and neither carries what made them."""
    home = tmp_path / "sentinel-home"
    installation = _bootstrap(home)

    first = resolve_local_target(installation)
    second = resolve_local_target(installation)
    assert first is not None and second is not None
    assert first == second

    assert first.target_ref.startswith(TARGET_REF_PREFIX)
    assert first.endpoint_profile_ref.startswith(ENDPOINT_PROFILE_REF_PREFIX)
    # Distinct by construction, not merely by prefix: strip both labels and the
    # digests still differ, which is what a shared digest domain would break.
    assert first.target_ref.removeprefix(TARGET_REF_PREFIX) != (
        first.endpoint_profile_ref.removeprefix(ENDPOINT_PROFILE_REF_PREFIX)
    )
    for reference in (first.target_ref, first.endpoint_profile_ref):
        assert "sentinel-home" not in reference
        assert str(home) not in reference

    other = resolve_local_target(_bootstrap(tmp_path / "other-home"))
    assert other is not None
    assert other.target_ref != first.target_ref
    assert other.endpoint_profile_ref != first.endpoint_profile_ref


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="no symlink support")
def test_two_aliases_of_one_home_resolve_to_one_target(tmp_path: Path) -> None:
    """The collision `validate_core_target_authorities` exists to refuse.

    `--home /a` and `--home /link-to-a` are one writable workspace. Two targets
    over it would be two authorities claiming one `workspace_ref`, which is
    exactly the set-level invariant the contract refuses -- so the selection is
    resolved through the link before it is digested.
    """
    home = tmp_path / "real-home"
    installation = _bootstrap(home)
    alias = tmp_path / "alias-home"
    try:
        alias.symlink_to(home, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform-dependent
        pytest.skip("this host cannot create a directory symlink")

    through_link = resolve_local_target(Installation(home=alias))
    direct = resolve_local_target(installation)

    assert direct is not None and through_link is not None
    assert through_link == direct


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json at all", id="unparseable"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('"a string"', id="not-a-mapping"),
        pytest.param(
            json.dumps({"manifest_version": "2", "workspace_id": WORKSPACE_ID}),
            id="another-manifest-version",
        ),
        pytest.param(json.dumps({"workspace_id": WORKSPACE_ID}), id="no-manifest-version"),
        pytest.param(json.dumps({"manifest_version": "1"}), id="no-workspace-id"),
        pytest.param(
            json.dumps({"manifest_version": "1", "workspace_id": "not a workspace id"}),
            id="malformed-workspace-id",
        ),
        pytest.param(
            json.dumps({"manifest_version": "1", "workspace_id": 17}), id="unstringly-id"
        ),
    ],
)
def test_a_manifest_that_is_not_one_resolves_no_target(tmp_path: Path, body: str) -> None:
    home = tmp_path / "home"
    _write_manifest(home, body)

    assert resolve_local_target(Installation(home=home)) is None


def test_a_missing_manifest_resolves_no_target(tmp_path: Path) -> None:
    (tmp_path / "home" / "workspace").mkdir(parents=True)

    assert resolve_local_target(Installation(home=tmp_path / "home")) is None


def test_an_oversized_manifest_is_abandoned_rather_than_read(tmp_path: Path) -> None:
    """One byte past the bound, and the file is otherwise perfectly valid.

    The padding is inside the JSON, so a reader that ignored the bound would
    parse it, find a well-formed workspace id, and resolve a target. Failing
    closed here is what keeps a status command from pulling an arbitrary file
    into memory because it happened to be named `workspace.json`.
    """
    home = tmp_path / "home"
    document = {
        "manifest_version": "1",
        "workspace_id": WORKSPACE_ID,
        "padding": "x" * MAX_MANIFEST_BYTES,
    }
    body = json.dumps(document).encode("utf-8")
    assert len(body) > MAX_MANIFEST_BYTES
    _write_manifest(home, body)

    assert resolve_local_target(Installation(home=home)) is None


def _target(tmp_path: Path) -> Any:
    resolved = resolve_local_target(_bootstrap(tmp_path / "home"))
    assert resolved is not None
    return resolved


def test_a_live_running_service_offers_stop_and_only_stop(tmp_path: Path) -> None:
    status = live_status(
        _target(tmp_path),
        state="ready",
        ready=True,
        server_version="0.6.5",
        protocol_version="1.3",
    )

    assert status.lifecycle_state == "running"
    assert status.readiness_state == "ready"
    assert status.compatibility_state == "compatible"
    assert status.connection_state == "connected"
    assert status.warning_codes == ()
    assert status.permitted_actions == ("stop",)
    assert status.server_version == "0.6.5"
    assert status.protocol_version == "1.3"


def test_maintenance_is_running_but_never_ready(tmp_path: Path) -> None:
    status = live_status(_target(tmp_path), state="maintenance", ready=True)

    assert status.lifecycle_state == "running"
    assert status.readiness_state == "not_ready"
    assert status.warning_codes == ("degraded",)
    assert status.permitted_actions == ("stop",)


@pytest.mark.parametrize(
    ("state", "lifecycle"),
    [("draining", "stopping"), ("starting", "starting"), ("failed", "failed")],
)
def test_a_phase_that_is_neither_running_nor_stopped_offers_nothing(
    tmp_path: Path, state: str, lifecycle: str
) -> None:
    status = live_status(_target(tmp_path), state=state, ready=False)

    assert status.lifecycle_state == lifecycle
    assert status.permitted_actions == ()


def test_a_version_the_descriptor_did_not_publish_is_dropped(tmp_path: Path) -> None:
    """A malformed version is absent, never passed through as itself."""
    status = live_status(
        _target(tmp_path),
        state="ready",
        ready=True,
        server_version="whatever the service said",
        protocol_version=3,
    )

    assert status.server_version is None
    assert status.protocol_version is None


def test_a_stopped_target_offers_start_and_only_start(tmp_path: Path) -> None:
    status = stopped_status(_target(tmp_path))

    assert status.lifecycle_state == "stopped"
    assert status.readiness_state == "not_ready"
    assert status.connection_state == "disconnected"
    assert status.permitted_actions == ("start",)


def test_an_incompatible_owner_offers_no_action_at_all(tmp_path: Path) -> None:
    status = incompatible_status(_target(tmp_path))

    assert status.lifecycle_state == "unknown"
    assert status.readiness_state == "not_ready"
    assert status.compatibility_state == "incompatible"
    assert status.connection_state == "disconnected"
    assert status.warning_codes == ("version_incompatible",)
    assert status.permitted_actions == ()


def test_an_unreachable_target_never_offers_stop(tmp_path: Path) -> None:
    """Ownership was never established, so there is nothing to stop."""
    target = _target(tmp_path)

    assert unreachable_status(target, may_start=True).permitted_actions == ("start",)
    assert unreachable_status(target, may_start=False).permitted_actions == ()
    assert "stop" not in unreachable_status(target, may_start=True).permitted_actions


def test_a_degraded_target_offers_nothing(tmp_path: Path) -> None:
    status = degraded_status(_target(tmp_path), lifecycle_state="stopping")

    assert status.lifecycle_state == "stopping"
    assert status.warning_codes == ("degraded",)
    assert status.permitted_actions == ()


def _every_status(target: Any) -> list[CoreSafeStatusV1]:
    return [
        live_status(target, state="ready", ready=True, server_version="0.6.5", protocol_version="1.3"),
        live_status(target, state="maintenance", ready=True),
        live_status(target, state="draining", ready=False),
        stopped_status(target),
        incompatible_status(target),
        unreachable_status(target, may_start=True),
        unreachable_status(target, may_start=False),
        degraded_status(target, lifecycle_state="running"),
        degraded_status(target, lifecycle_state="stopping"),
    ]


def test_every_published_status_survives_a_canonical_round_trip(tmp_path: Path) -> None:
    """Encoded, canonicalised, parsed and decoded back to the same value.

    The encoder is the only way any of these reach stdout, so this is the whole
    of what a caller can receive -- and `decode_core_safe_status` re-runs the
    contract's semantics on the way back, so an encode that satisfied the
    dataclass but not the contract is caught here rather than by a reader.
    """
    from omnivia_core.contracts.v1 import encode_core_safe_status

    for status in _every_status(_target(tmp_path)):
        payload = encode_core_safe_status(status)
        decoded = decode_core_safe_status(json.loads(to_canonical_json(payload)))
        assert decoded == status


def test_no_published_status_carries_a_forbidden_key_or_the_home_it_came_from(
    tmp_path: Path,
) -> None:
    from omnivia_core.contracts.v1 import encode_core_safe_status

    home = tmp_path / "sentinel-home"
    installation = _bootstrap(home, workspace_id=WORKSPACE_ID)
    target = resolve_local_target(installation)
    assert target is not None

    for status in _every_status(target):
        document = json.loads(json.dumps(encode_core_safe_status(status)))
        # `workspace_ref` is the contract's own field and is meant to be here;
        # `workspace_id`, the raw key the v1 document published, is not.
        _assert_publishable(
            document,
            sentinels=("sentinel-home", str(home), str(installation.socket_path)),
        )


def test_the_adapter_document_publishes_only_a_declared_code(tmp_path: Path) -> None:
    """An undeclared code lands on `internal_error` rather than on stdout.

    Every call site in `main.py` passes a literal from the set, so this is the
    guard against the one that will not: a code added to a branch and not to the
    set is published as the fail-closed value, not as itself.
    """
    from omnivia_core_cli import main as cli_main

    buffer = io.StringIO()
    original = cli_main.sys.stdout
    cli_main.sys.stdout = buffer
    try:
        cli_main._write_lifecycle_document(
            "status", ok=False, outcome="failed", code="a code nobody declared"
        )
    finally:
        cli_main.sys.stdout = original

    assert json.loads(buffer.getvalue()) == {
        "lifecycle_adapter_version": 2,
        "action": "status",
        "ok": False,
        "outcome": "failed",
        "code": "internal_error",
    }


def test_a_status_the_contract_refuses_is_omitted_rather_than_published(
    tmp_path: Path,
) -> None:
    """Fail closed: no `safe_status` beats an invalid one.

    A caller that receives no status offers no actions. A caller that receives
    one the contract would have refused might offer whatever it carried -- here,
    an action outside `CoreSafeAction` entirely.
    """
    from omnivia_core_cli import main as cli_main

    target = _target(tmp_path)
    refused = CoreSafeStatusV1(
        contract_version=target.contract_version,
        target=target,
        lifecycle_state="running",
        readiness_state="ready",
        compatibility_state="compatible",
        connection_state="connected",
        warning_codes=(),
        permitted_actions=("force_quit",),
    )

    buffer = io.StringIO()
    original = cli_main.sys.stdout
    cli_main.sys.stdout = buffer
    try:
        cli_main._write_lifecycle_document(
            "status", ok=True, outcome="running", code="status_running", safe_status=refused
        )
    finally:
        cli_main.sys.stdout = original

    assert "safe_status" not in json.loads(buffer.getvalue())
