"""The boot identifier this host publishes, held to the public grammar.

The defect this closes was invisible to a POSIX-only suite because it needed two
things in one place: a platform-specific value *and* the public `Identifier`
grammar. On macOS `_boot_id()` returned the raw `sysctl kern.boottime` reading --
`{ sec = 1784972067, usec = 282194 } Sat Jul 25 19:34:27 2026` -- which the probe
router refuses, so a macOS Runtime could not answer `service.discover` at all.

Every grammar assertion here compiles the generated `IDENTIFIER_PATTERN` rather
than a regex written out by hand. That is what makes the hosted macOS row of
`phase2-platform.yml` evidence about the macOS value: the pattern under test is
the one the wire actually enforces, and it moves when the contract moves.

The two properties the field exists for are tested separately, because a
formatting repair can pass one and lose the other. Stability within a boot is read
off this host. Change across boots is proved by construction: a second boot cannot
be arranged inside a test run, so the kernel reading is injected through `_sysctl`
instead, and the platform branch is forced. Nothing here reboots anything.

Runtime-only, deliberately. `phase2-platform.yml` installs the runtime, the CLI and
MCP but not `omnivia-core-client`, so a module here that imported the client would
fail collection on all three rows. The client-level end-to-end lives in
`tests/phase3/protocol/test_client_endpoint_discovery.py`.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership import identity
from omnivia_core_runtime.ownership.identity import SystemProcessEvidence
from omnivia_core_runtime.service.probes import ProbeRouter
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import endpoint_for_path
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import IDENTIFIER_PATTERN, ServiceProbeRequest
from omnivia_core.workspace.manifest import WorkspaceManifest

from .conftest import SERVICE_INSTANCE

#: The grammar `process.boot_id` is held to, compiled from the generated pattern.
IDENTIFIER = re.compile(IDENTIFIER_PATTERN)

#: `Identifier.maxLength` in `contracts/application/v1/schemas/common.schema.json`,
#: which `service/probes.py` restates and enforces beside the pattern. Restated
#: again here because it is not importable: `omnivia_core.contracts.v1` exports the
#: pattern but no length constant to go with it.
#:
#: The assertion is not redundant beside `IDENTIFIER.fullmatch`, because the two
#: published forms of this grammar bound length differently. The generated *host
#: contract* pattern is `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` and bounds it
#: itself; the *application* pattern imported here, and its JSON-schema original,
#: are `[...]*` with the bound carried separately as `maxLength`. A 200-character
#: value fullmatches the pattern under test and is still refused on the wire, and
#: the only unbounded input that reaches a boot id is the host's own name.
IDENTIFIER_MAX_LENGTH = 128

#: A real `kern.bootsessionuuid` reading: the boot session's own UUID.
BOOT_SESSION = "AC3F1686-E4F5-43FC-B3F0-682E740EF3D4"

#: A real `kern.boottime` reading, in the layout macOS prints today.
BOOT_TIME = "{ sec = 1784972067, usec = 282194 } Sat Jul 25 19:34:27 2026"


def boot_id_from(
    monkeypatch: pytest.MonkeyPatch,
    *,
    system: str = "Darwin",
    readings: dict[str, str] | None = None,
    node: str = "host",
) -> str:
    """`_boot_id()` as it would answer on `system`, reading exactly `readings`.

    The injection point is `_sysctl` rather than `subprocess`, so what is faked is
    one kernel reading and not the parsing, the ordering or the grammar check.
    """
    given = readings or {}
    monkeypatch.setattr(identity.platform, "system", lambda: system)
    monkeypatch.setattr(identity.platform, "node", lambda: node)
    monkeypatch.setattr(identity, "_sysctl", lambda name: given.get(name))
    return identity._boot_id()


def test_the_boot_identifier_this_host_publishes_is_a_public_identifier() -> None:
    """The whole defect, on whichever platform this row is running.

    Unrepaired, this fails on macOS on the leading brace and passes on Linux, which
    is exactly why it had to be asserted on the host rather than reasoned about.
    """
    value = identity._boot_id()

    assert IDENTIFIER.fullmatch(value), f"not a public Identifier: {value!r}"
    assert len(value) <= IDENTIFIER_MAX_LENGTH, value


def test_two_readings_within_one_boot_agree() -> None:
    """Stability: the value must not move while the machine stays up.

    Two independently constructed evidence sources are compared as well as two bare
    calls. The comparison that matters happens between processes -- a launcher
    reading a descriptor against the service that wrote it -- and a value that were
    minted per call would satisfy neither.
    """
    assert identity._boot_id() == identity._boot_id()
    assert SystemProcessEvidence().current().boot_id == identity._boot_id()


def test_a_different_boot_session_yields_a_different_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Change across boots, by construction: a differing kernel reading, not a reboot."""
    first = boot_id_from(monkeypatch, readings={"kern.bootsessionuuid": BOOT_SESSION})
    again = boot_id_from(monkeypatch, readings={"kern.bootsessionuuid": BOOT_SESSION})
    rebooted = boot_id_from(
        monkeypatch, readings={"kern.bootsessionuuid": "0E1D2C3B-4A59-4687-9145-23AB45CD67EF"}
    )

    assert first == again, "the same boot session produced two identifiers"
    assert first != rebooted, "two boot sessions produced the same identifier"
    assert IDENTIFIER.fullmatch(rebooted)


def test_the_boot_session_uuid_is_preferred_over_the_boot_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which candidate wins when both are readable, which is the usual case.

    Both readings are present on a healthy macOS and both yield a conforming
    `Identifier`, so the grammar check cannot separate them and every other case
    here passes whichever order they are tried in. The order is the correctness
    argument, though: `kern.bootsessionuuid` is a UUID minted per boot session,
    while `kern.boottime` is a calendar time XNU re-derives whenever the clock is
    set. Preferring the boot time would publish a value an NTP step can move
    mid-boot -- stable-looking, grammar-clean, and wrong in the direction that lets
    a live owner read as dead.

    `kern.bootuuid` is the neighbouring trap and is deliberately never read: it is
    equally UUID-shaped and equally conforming, and it identifies the boot *object*
    rather than the boot, so it does not change across reboots at all.
    """
    value = boot_id_from(
        monkeypatch,
        readings={"kern.bootsessionuuid": BOOT_SESSION, "kern.boottime": BOOT_TIME},
    )

    assert value == BOOT_SESSION, (
        "the clock-derived boot time was preferred over the boot session uuid"
    )


@pytest.mark.parametrize(
    "reading",
    [
        BOOT_TIME,
        "{sec=1784972067,usec=282194} Sat Jul 25 19:34:27 2026",
        "{ usec = 282194, sec = 1784972067 }",
        "{   sec   =   1784972067   }",
    ],
)
def test_the_boot_time_reading_is_read_by_name_and_not_by_layout(
    monkeypatch: pytest.MonkeyPatch, reading: str
) -> None:
    """The second choice, when no boot session uuid is available.

    That text is a human-readable rendering, not an interface, so field order and
    spacing are not depended on. `usec` is the trap: it contains `sec`, and a parser
    that matched it would publish a microsecond count as a boot identity and lose
    change-across-boots entirely -- two boots share a `usec` value routinely.
    """
    value = boot_id_from(monkeypatch, readings={"kern.boottime": reading})

    assert value == "boot-1784972067"
    assert IDENTIFIER.fullmatch(value)


def test_a_boot_time_reading_that_cannot_be_read_is_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reading the parser does not recognise falls back rather than escaping.

    Including the `usec`-only case: nothing here may answer `boot-282194`.
    """
    for reading in ("{ usec = 282194 } Sat Jul 25 19:34:27 2026", "sysctl: unknown oid"):
        value = boot_id_from(monkeypatch, readings={"kern.boottime": reading})
        assert value.startswith("unknown-boot:"), value
        assert IDENTIFIER.fullmatch(value)


def test_a_platform_reading_that_is_not_an_identifier_never_reaches_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single grammar check, proved to be the thing that decides.

    A kernel that answered `kern.bootsessionuuid` with the old raw boot-time string
    is the defect exactly: the value is present, plausible and unpublishable.
    """
    value = boot_id_from(monkeypatch, readings={"kern.bootsessionuuid": BOOT_TIME})

    assert IDENTIFIER.fullmatch(value), f"an unpublishable reading was returned: {value!r}"
    assert value.startswith("unknown-boot:")


def test_a_platform_with_no_boot_source_still_yields_an_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows, and any platform with nothing to read: a value, never None.

    `ProcessEvidence` and the public `ServiceProcessEvidence` require the pid, the
    start time and the boot id together, so `None` here would omit the whole
    evidence block -- and `service/bootstrap.py::_live` reads a descriptor with no
    process block as not live, so every launcher would refuse to adopt a service
    that is running. The last resort says what it is instead of claiming a stability
    it does not have.
    """
    value = boot_id_from(monkeypatch, system="Windows", node="WIN-BUILD-01")

    assert value == "unknown-boot:WIN-BUILD-01"
    assert IDENTIFIER.fullmatch(value)


@pytest.mark.parametrize(
    "node",
    ["Clayton's MacBook Pro", "host name with spaces", "", "x" * 300],
)
def test_a_host_name_the_grammar_forbids_cannot_produce_an_invalid_identifier(
    monkeypatch: pytest.MonkeyPatch, node: str
) -> None:
    """The last resort is grammar-safe whatever the machine is called.

    A hostname may hold a space or an apostrophe and may be long; an `Identifier`
    may hold neither and is bounded at 128. Left as it was, the fallback would have
    taken discovery down on those hosts the same way macOS was already down.
    """
    value = boot_id_from(monkeypatch, system="Windows", node=node)

    assert IDENTIFIER.fullmatch(value), f"not a public Identifier: {value!r}"
    assert len(value) <= IDENTIFIER_MAX_LENGTH, value


@pytest.fixture
def migrated(
    workspace: WorkspaceLayout,
    installation: InstallationLayout,
    manifest: WorkspaceManifest,
    phase0_source: Path,
) -> tuple[WorkspaceLayout, InstallationLayout]:
    """A migrated workspace this Runtime can own and advertise."""
    migrate_legacy_database(
        phase0_source,
        workspace,
        installation,
        manifest,
        service_instance_id=SERVICE_INSTANCE,
    )
    return workspace, installation


def test_a_real_service_discover_probe_succeeds_on_this_host(
    migrated: tuple[WorkspaceLayout, InstallationLayout], tmp_path: Path
) -> None:
    """The reported symptom, closed: the probe answers rather than refusing.

    Nothing is normalised. The runner builds its identity from the real
    `SystemProcessEvidence`, so the `boot_id` under test is this host's own, and the
    router is composed exactly as `service/main.py::_router_for` composes it, so the
    validation the answer passes through is the production one. Unrepaired, this
    raises `ProbeError` on macOS: `service facts descriptor process boot_id`.
    """
    workspace, installation = migrated
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace.root,
            installation_root=installation.root,
            core_version="0.1.0",
            endpoint=endpoint_for_path(tmp_path / "boot-id.sock").url,
        )
    )
    report = runner.start()
    try:
        assert report.ready, report.to_dict()
        router = ProbeRouter(
            facts=runner.probe_facts, capabilities=tuple, clock=time.monotonic_ns
        )

        result = router.route(ServiceProbeRequest(probe="service.discover"))

        assert result.status == "pass"
        assert result.descriptor is not None
        assert result.descriptor.process is not None
        assert result.descriptor.process.boot_id == identity._boot_id()
        assert IDENTIFIER.fullmatch(result.descriptor.process.boot_id)
    finally:
        runner.stop()
