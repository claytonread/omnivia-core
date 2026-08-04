"""M2 evidence: the mode of the descriptor the Runtime publishes.

Five facts about bytes on disk that the existing suites cannot establish.

The permission assertions read the real `st_mode` of the real published file and of
the directory chain above it. "Not world readable" would pass on `0640`, which the
accepted client refuses; only the exact mode is evidence.

The umask-hostile case publishes under `umask(0o000)`. Without it the whole repair
passes on any machine whose umask happens to be restrictive and fails in the field,
because the defect was never the mode that was asked for -- it was that no mode was
asked for at all.

The no-window case proves the mode is on the bytes *before* they are reachable at
the published path. A `chmod` after the rename would satisfy every assertion above
and still leave the descriptor world-readable for as long as the two syscalls are
apart.

The stale-temporary case is the only one that reaches the explicit `chmod` on the
temporary file, and the created-ancestor case is the only one that reaches the
directories `publish()` builds above the two the reader validates.

The end-to-end proof that closes M2 -- the real writer's file read and accepted by
the real P1b client -- is deliberately *not* here. It imports
`omnivia_core_client`, which `phase2-platform.yml` does not install, so it lives in
`tests/phase3/protocol/test_client_endpoint_discovery.py` where the workflow that
collects it installs the client. Nothing in this module may import that package:
this is the one suite the platform matrix runs, and an import it cannot satisfy
fails collection for every case in the file, not just the one that needed it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.discovery import (
    DESCRIPTOR_MODE,
    DESCRIPTOR_NAME,
    RUNTIME_DIRECTORY_MODE,
    descriptor_path,
    publish,
)
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)

from omnivia_core.contracts.v1 import (
    ServiceEndpointDescriptor,
    ServiceProcessEvidence,
)

from .conftest import SERVICE_INSTANCE, WORKSPACE_ID

WORKSPACE_FORMAT_ORDINAL = "1"

#: The mode the installation layout leaves the runtime chain at under a default
#: `022` umask, and the one the accepted client refuses.
INHERITED_DIRECTORY_MODE = 0o755


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def descriptor() -> ServiceEndpointDescriptor:
    """A valid descriptor: these cases are about the file's mode, not its contents."""
    return ServiceEndpointDescriptor(
        descriptor_version=API_VERSION,
        workspace_id=WORKSPACE_ID,
        service_instance_id=SERVICE_INSTANCE,
        installation_id="inst-m2",
        endpoint_uri="unix:///tmp/omnivia-m2.sock",
        protocol_version=PROTOCOL_VERSION,
        server_version=SERVER_VERSION,
        supported_api_versions=supported_api_versions(),
        supported_workspace_versions=supported_workspace_versions(
            WORKSPACE_FORMAT_ORDINAL
        ),
        workspace_format_version=workspace_contract_version(WORKSPACE_FORMAT_ORDINAL),
        ready=True,
        lifecycle_state="ready",
        fencing_generation=2,
        published_at="2026-08-04T00:00:00Z",
        process=ServiceProcessEvidence(
            pid=os.getpid(), start_time="100", boot_id="boot-a"
        ),
    )


def inherited_chain(tmp_path: Path) -> Path:
    """A runtime chain that already exists at `0755`, as the layout leaves it.

    `InstallationLayout.create` runs long before publication and creates these
    directories with the umask-derived mode. Publishing into a chain this test
    created itself at `0700` would prove only that `mkdir` was not consulted.
    """
    runtime = tmp_path / "installation-state" / "runtime" / WORKSPACE_ID
    runtime.mkdir(parents=True)
    os.chmod(runtime.parent, INHERITED_DIRECTORY_MODE)
    os.chmod(runtime, INHERITED_DIRECTORY_MODE)
    return runtime


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes; Windows ACLs are out of scope"
)
def test_the_descriptor_and_its_directory_chain_are_owner_only(tmp_path: Path) -> None:
    """Exact `st_mode`, on the file and on both directories the client validates."""
    runtime = inherited_chain(tmp_path)
    assert mode_of(runtime) == INHERITED_DIRECTORY_MODE, "the chain starts open"

    target = publish(runtime, descriptor())

    assert mode_of(target) == DESCRIPTOR_MODE == 0o600
    assert mode_of(runtime) == RUNTIME_DIRECTORY_MODE == 0o700
    assert mode_of(runtime.parent) == RUNTIME_DIRECTORY_MODE == 0o700
    # And nothing else is left behind at any mode: the temporary file is gone.
    assert [entry.name for entry in runtime.iterdir()] == ["service.json"]


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes; Windows ACLs are out of scope"
)
def test_a_permissive_umask_cannot_widen_the_publication(tmp_path: Path) -> None:
    """`umask(0o000)` is the field condition the developer machine hides.

    Both cases are covered: a chain that already exists at `0755`, and one this
    publication creates itself while nothing at all is being masked out.
    """
    existing = inherited_chain(tmp_path)
    fresh = tmp_path / "fresh-state" / "runtime" / WORKSPACE_ID

    previous = os.umask(0o000)
    try:
        existing_target = publish(existing, descriptor())
        fresh_target = publish(fresh, descriptor())
    finally:
        os.umask(previous)

    for target in (existing_target, fresh_target):
        assert mode_of(target) == 0o600
        assert mode_of(target.parent) == 0o700
        assert mode_of(target.parent.parent) == 0o700


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes; Windows ACLs are out of scope"
)
def test_the_descriptor_is_never_observable_at_a_wider_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is on the bytes before the rename that publishes them.

    `replace()` carries the temporary file's mode to the target, so the mode the
    rename is holding *is* the only mode the published path ever has. Observing it
    at the instant of the rename is therefore the whole of the question: a `chmod`
    moved to after the rename would leave `0644` here and still end at `0600`.
    """
    runtime = inherited_chain(tmp_path)
    target = descriptor_path(runtime)
    observed: dict[str, object] = {}
    original_replace = Path.replace

    def recording_replace(self: Path, other: Any) -> Path:
        observed["renamed_mode"] = mode_of(self)
        observed["target_existed"] = Path(other).exists()
        observed["directory_mode"] = mode_of(self.parent)
        return original_replace(self, other)

    monkeypatch.setattr(Path, "replace", recording_replace)

    previous = os.umask(0o000)
    try:
        publish(runtime, descriptor())
    finally:
        os.umask(previous)

    assert observed["renamed_mode"] == 0o600, "the rename carried a wider mode"
    assert observed["target_existed"] is False, "nothing was published before the mode"
    assert observed["directory_mode"] == 0o700, "the file was reachable before the mode"
    assert mode_of(target) == 0o600


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes; Windows ACLs are out of scope"
)
def test_a_stale_temporary_file_cannot_publish_its_own_mode(tmp_path: Path) -> None:
    """The one case the explicit `chmod` on the temporary file is there for.

    A umask cannot reach this line: it only ever clears bits, so it can never widen
    an `O_CREAT` of `0o600`, and every mode case above passes with the `chmod`
    deleted. A temporary file that already exists can: `O_CREAT` does not reset the
    mode of one and `O_TRUNC` replaces its contents, not its permissions.

    The scenario is reachable rather than theoretical. The temporary name carries
    the publishing process's pid, so a run killed between the open and the rename
    leaves one behind at exactly the path the next process given that pid writes to
    -- and the rename would then publish that file's mode.
    """
    runtime = inherited_chain(tmp_path)
    stale = runtime / f".{DESCRIPTOR_NAME}.{os.getpid()}.tmp"
    stale.write_text("{}", encoding="utf-8")
    os.chmod(stale, 0o666)

    target = publish(runtime, descriptor())

    assert mode_of(target) == 0o600, "the rename published the stale file's mode"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes; Windows ACLs are out of scope"
)
def test_every_directory_the_publication_creates_is_owner_only(tmp_path: Path) -> None:
    """`mkdir(parents=True)` builds ancestors at the umask-derived mode.

    Publishing into a not-yet-existing installation root under a permissive umask
    left that root at `0o777`. Nothing refuses it -- the client opens the
    installation root as a trusted root and checks only that it is a directory --
    but a world-writable root lets any local user rename the validated `runtime`
    directory out from under the chain, which defeats every mode below it.

    The pre-existing ancestor is asserted too, in the other direction: a directory
    this call did not create is not this call's to re-permission.
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    os.chmod(outer, INHERITED_DIRECTORY_MODE)
    root = outer / "installation-state"
    runtime = root / "runtime" / WORKSPACE_ID

    previous = os.umask(0o000)
    try:
        publish(runtime, descriptor())
    finally:
        os.umask(previous)

    for created in (root, root / "runtime", runtime):
        assert mode_of(created) == 0o700, f"{created} was created world-accessible"
    assert mode_of(outer) == INHERITED_DIRECTORY_MODE, (
        "a pre-existing ancestor was re-permissioned"
    )
